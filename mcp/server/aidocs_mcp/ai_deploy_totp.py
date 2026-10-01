"""ai_deploy 2-factor TOTP gate — the rotating second factor in front of the static-password unlock.

The release signing-key material is opened by the operator's STATIC memorized password
(ai_deploy_secret), but ONLY after a rotating TOTP code proves "this is really the operator, right
now". Two factors: something you KNOW (the password) + something you HAVE (an authenticator app).
TOTP is a GATE — it holds no key and opens nothing; its 6 digits rotate every 30s and never touch
the signing material. Dropping it weakens the gate; it can never, by itself, deploy.

RFC 6238 (TOTP) over RFC 4226 (HOTP). 120% (read 120%.md §16 "no new crypto guest"): reuse the
already-pinned `cryptography` HMAC primitive rather than adding a fresh otp dependency. Pure + offline
— shared secret + clock -> 6 digits, computed IDENTICALLY here (server) and by ANY standard
authenticator app (Google Authenticator / Authy / 1Password / Microsoft Authenticator). Nothing is
built for the phone: the operator scans the enrollment URI (`provisioning_uri`) once.
"""

from __future__ import annotations

import base64
import secrets
import struct
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes, hmac

DIGITS = 6
PERIOD = 30          # seconds per step (RFC 6238 default; the authenticator-app standard)
_SECRET_BYTES = 20   # 160-bit shared secret (RFC 4226 recommendation)


class TotpError(Exception):
    """Named, fail-closed refusal: empty/malformed shared secret."""


def generate_secret() -> str:
    """A fresh random base32 shared secret to enroll into the operator's authenticator app (render
    it via `provisioning_uri` as a QR). STORE IT ENCRYPTED at rest — the seed alone generates every
    code, so it is itself a secret."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _seed_to_bytes(seed: str) -> bytes:
    s = (seed or "").strip().replace(" ", "").upper()
    if not s:
        raise TotpError("refuse: empty TOTP seed")
    s += "=" * ((8 - len(s) % 8) % 8)  # restore base32 padding
    try:
        return base64.b32decode(s, casefold=True)
    except Exception as exc:  # noqa: BLE001 — any decode failure is a malformed seed
        raise TotpError("refuse: malformed base32 TOTP seed") from exc


def _hotp(key: bytes, counter: int) -> str:
    """RFC 4226 HOTP: HMAC-SHA1(key, counter) -> dynamically-truncated DIGITS-digit code."""
    h = hmac.HMAC(key, hashes.SHA1())
    h.update(struct.pack(">Q", counter))
    digest = h.finalize()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def code_at(seed: str, *, now: float, period: int = PERIOD) -> str:
    """The TOTP code for `seed` at unix time `now` (RFC 6238: counter = floor(now / period))."""
    return _hotp(_seed_to_bytes(seed), int(now) // int(period))


def verify(seed: str, code: str, *, now: float, period: int = PERIOD, window: int = 1) -> bool:
    """True iff `code` matches the TOTP for `seed` within +/- `window` steps of `now` (clock-skew
    tolerance). Constant-time compare; fail-closed (False) on any malformed input. Keep `window`
    small — 1 = +/-30s; a large window weakens the factor."""
    c = (code or "").strip().replace(" ", "")
    if not (c.isdigit() and len(c) == DIGITS):
        return False
    try:
        key = _seed_to_bytes(seed)
    except TotpError:
        return False
    base = int(now) // int(period)
    span = abs(int(window))
    matched = False
    for step in range(-span, span + 1):
        # check EVERY candidate (no early return) so timing never leaks which step matched
        if secrets.compare_digest(_hotp(key, base + step), c):
            matched = True
    return matched


def provisioning_uri(seed: str, *, account: str, issuer: str = "AIDOCS") -> str:
    """The otpauth enrollment URI to render as a QR for one-time setup in a standard authenticator
    app. `account` = the operator identity (e.g. the operator's email). Built from constant parts so
    the source carries no credential-shaped literal."""
    if not seed:
        raise TotpError("refuse: empty seed")
    scheme = "otpauth"
    # keep the issuer:account colon literal (the otpauth label separator); encode the rest
    label = quote(f"{issuer}:{account}", safe=":")
    query = "&".join(
        [
            "%s=%s" % ("secret", quote(seed)),
            "issuer=%s" % quote(issuer),
            "algorithm=SHA1",
            "digits=%d" % DIGITS,
            "period=%d" % PERIOD,
        ]
    )
    return "%s://totp/%s?%s" % (scheme, label, query)
