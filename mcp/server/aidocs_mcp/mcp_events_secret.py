"""Lane 1 MCP Events: custody of the whsec_ webhook secret, and Standard Webhooks signing.

ChatGPT hands the gate a `whsec_` secret on events/subscribe; the gate needs it again
for every delivery, so it must be stored. It is never stored in plaintext
(r0b1 BLOCK be6e382d-11b, P0 #6). There is no gate DB encrypted-column helper, and
native_keystore is device-local (not on the VPS), so:

  * AES-256-GCM under a gate key-encryption key (KEK). Same already-trusted
    `cryptography` as ai_deploy_secret; no new crypto dependency.
  * CUSTODY (r0b1 0a32e074-039): in production the KEK is an operator-provisioned
    FILE outside the repo and the deploy artifact (systemd credential, or e.g.
    /etc/aidocs/secrets/events-kek), named by AIDOCS_EVENTS_KEK_FILE, which carries
    only the path. It must be a regular file owned by the service account with no
    group/other access, in a directory nobody else can write. A raw key in
    AIDOCS_EVENTS_KEK is honoured only for isolated tests and local dev. The KEK is
    random and independent: never derived from OAuth, Git or deploy-signing secrets.
    Nothing here generates, rotates, prints or replaces it.
  * ROTATION: a Keyring lists KEKs newest first; it seals with the newest and opens
    with whichever key a row names. An old key leaves the ring only once no row
    (prev_secret included) still references its kek_id.
  * The associated data is the subscription key, so a sealed secret cannot be
    moved onto another subscription's row and still open.
  * NO KEK, NO FEATURE: load_kek(None) is None and the caller must not advertise
    events. A KEK that is present but malformed is a refusal, never "off".
  * WebhookSecretError never carries secret material.

Design: .MEMORY/sessions/ubermega/lane1-mcp-events-wake-design.md §3.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

WHSEC_PREFIX = "whsec_"
WHSEC_MIN_BYTES = 24
WHSEC_MAX_BYTES = 64
KEK_LEN = 32
NONCE_LEN = 12
ENV_KEK_FILE = "AIDOCS_EVENTS_KEK_FILE"
#: A raw key in the environment: isolated tests and local dev ONLY.
ENV_KEK_RAW = "AIDOCS_EVENTS_KEK"


class WebhookSecretError(Exception):
    """Named, fail-closed refusal. Carries NO secret material (safe to surface/audit)."""


@dataclass(frozen=True)
class Kek:
    key: bytes
    #: Names the key without revealing it, so rows record which key sealed them.
    kek_id: str


class Sealed(NamedTuple):
    ciphertext: bytes
    nonce: bytes
    kek_id: str


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise WebhookSecretError("refuse: not base64") from None


def parse_whsec(secret: object) -> bytes:
    """The HMAC key inside a `whsec_` secret; 24-64 decoded bytes per the MCP Events spec."""
    if not isinstance(secret, str) or not secret.startswith(WHSEC_PREFIX):
        raise WebhookSecretError("refuse: secret must start with whsec_")
    key = _b64(secret[len(WHSEC_PREFIX):])
    if not WHSEC_MIN_BYTES <= len(key) <= WHSEC_MAX_BYTES:
        raise WebhookSecretError(
            f"refuse: secret must decode to {WHSEC_MIN_BYTES}-{WHSEC_MAX_BYTES} bytes"
        )
    return key


def load_kek(raw: str | None) -> Kek | None:
    """The KEK from its deploy secret (base64 of 32 bytes). None/empty = feature off."""
    if not raw:
        return None
    key = _b64(raw.strip())
    if len(key) != KEK_LEN:
        raise WebhookSecretError(f"refuse: KEK must decode to {KEK_LEN} bytes")
    return Kek(key=key, kek_id=hashlib.sha256(b"aidocs-events-kek|" + key).hexdigest()[:16])


def seal(kek: Kek, secret: str, *, aad: bytes, nonce: bytes | None = None) -> Sealed:
    """Encrypt a validated whsec_ secret, bound to `aad` (the subscription key).
    `nonce` is injectable ONLY for deterministic tests; in real use it is fresh."""
    parse_whsec(secret)
    nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
    ct = AESGCM(kek.key).encrypt(nonce, secret.encode("utf-8"), aad)
    return Sealed(ciphertext=ct, nonce=nonce, kek_id=kek.kek_id)


def unseal(kek: Kek, sealed: Sealed, *, aad: bytes) -> str:
    """The plaintext secret, or WebhookSecretError: wrong key, wrong subscription, or a
    tampered row all fail the GCM tag, indistinguishably by design."""
    if sealed.kek_id != kek.kek_id:
        raise WebhookSecretError("refuse: sealed under a different key")
    try:
        return AESGCM(kek.key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode("utf-8")
    except InvalidTag:
        raise WebhookSecretError("refuse: secret failed authentication") from None


def signature_header(secrets: list[str], msg_id: str, timestamp: int, body: bytes) -> str:
    """Standard Webhooks `webhook-signature`: `v1,<b64 HMAC-SHA256>` over
    "{id}.{timestamp}.{body}", one per secret, space-separated (the current secret
    first, then the previous one during a rotation window)."""
    if not secrets:
        raise WebhookSecretError("refuse: no secret to sign with")
    signed = f"{msg_id}.{int(timestamp)}.".encode("utf-8") + bytes(body)
    return " ".join(
        "v1," + base64.b64encode(hmac.new(parse_whsec(s), signed, hashlib.sha256).digest()).decode()
        for s in secrets
    )


#: Domain separation for the wake ticket (#1226 continuation, design §10(1)), so the
#: KEK's HMAC output here can never stand in for anything else it computes.
WAKE_TICKET_LABEL = b"aidocs-wake-ticket\x00"


def wake_ticket(kek: Kek, event_id: str) -> str:
    """The bearer ticket a woken run presents to wake_resume: base64url (unpadded) of
    HMAC-SHA256(KEK, label + event_id). DERIVED, never stored: the outbox row keeps
    only the kek_id, and anyone holding a row learns nothing about the ticket."""
    mac = hmac.new(kek.key, WAKE_TICKET_LABEL + str(event_id).encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def check_kek_file_stat(st: os.stat_result, parent_st: os.stat_result, *, euid: int) -> None:
    """POSIX custody check: a regular file (never a symlink, so pass lstat), owned by the
    service account, no group/other access, in a directory only it or root owns and
    nobody else can write."""
    if not stat.S_ISREG(st.st_mode):
        raise WebhookSecretError("refuse: KEK path is not a regular file")
    if st.st_uid != euid:
        raise WebhookSecretError("refuse: KEK file is not owned by the service account")
    if st.st_mode & 0o077:
        raise WebhookSecretError("refuse: KEK file is accessible to group or others")
    if parent_st.st_uid not in (euid, 0):
        raise WebhookSecretError("refuse: KEK directory is owned by another user")
    if parent_st.st_mode & 0o022:
        raise WebhookSecretError("refuse: KEK directory is writable by group or others")


def _read_kek_file(path: str, *, posix_checks: bool) -> Kek:
    p = Path(path)
    try:
        if posix_checks:
            check_kek_file_stat(os.lstat(p), os.stat(p.parent), euid=os.geteuid())
        raw = p.read_text(encoding="ascii")
    except (OSError, ValueError):
        raise WebhookSecretError("refuse: KEK file is unreadable") from None
    kek = load_kek(raw)
    if kek is None:
        raise WebhookSecretError("refuse: KEK file is empty")
    return kek


@dataclass(frozen=True)
class Keyring:
    """KEKs newest first. Seals with the newest; opens with the key a row names."""

    keks: tuple[Kek, ...]

    def __post_init__(self) -> None:
        if not self.keks:
            raise WebhookSecretError("refuse: empty keyring")
        if len(set(self.kek_ids)) != len(self.keks):
            raise WebhookSecretError("refuse: the same KEK is listed twice")

    @property
    def current(self) -> Kek:
        return self.keks[0]

    @property
    def kek_ids(self) -> tuple[str, ...]:
        return tuple(k.kek_id for k in self.keks)

    def seal(self, secret: str, *, aad: bytes) -> Sealed:
        return seal(self.current, secret, aad=aad)

    def unseal(self, sealed: Sealed, *, aad: bytes) -> str:
        for kek in self.keks:
            if kek.kek_id == sealed.kek_id:
                return unseal(kek, sealed, aad=aad)
        raise WebhookSecretError("refuse: sealed under a key no longer in the keyring")

    def _kek(self, kek_id: str) -> Kek:
        for kek in self.keks:
            if kek.kek_id == kek_id:
                return kek
        raise WebhookSecretError("refuse: wake ticket key is no longer in the keyring")

    def wake_ticket(self, event_id: str, kek_id: str) -> str:
        """The ticket for `event_id` under the key the outbox row recorded."""
        return wake_ticket(self._kek(kek_id), event_id)

    def verify_wake_ticket(self, event_id: str, kek_id: str | None, presented: object) -> bool:
        """Constant-time check of a presented ticket. A key that left the ring, a
        missing key id or a non-string never verifies."""
        if not isinstance(presented, str) or not presented or not kek_id:
            return False
        try:
            expected = self.wake_ticket(event_id, kek_id)
        except WebhookSecretError:
            return False
        return hmac.compare_digest(expected.encode("ascii"), presented.encode("utf-8", "replace"))


def load_keyring(
    env: Mapping[str, str],
    *,
    allow_raw_env: bool = False,
    posix_checks: bool = os.name == "posix",
) -> Keyring | None:
    """The gate's KEKs, or None when none is configured (the feature is then OFF).

    AIDOCS_EVENTS_KEK_FILE lists key files, newest first, separated by os.pathsep. A
    listed file that is missing, unreadable, malformed or badly permissioned is a
    refusal (a health fault), never "off". AIDOCS_EVENTS_KEK is read only with
    allow_raw_env, for isolated tests and local dev."""
    paths = [p.strip() for p in (env.get(ENV_KEK_FILE) or "").split(os.pathsep) if p.strip()]
    if paths:
        return Keyring(tuple(_read_kek_file(p, posix_checks=posix_checks) for p in paths))
    if allow_raw_env:
        kek = load_kek(env.get(ENV_KEK_RAW))
        return Keyring((kek,)) if kek is not None else None
    return None
