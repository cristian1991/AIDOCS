"""The ONE-TIME binding-authority SEAL -- #1134 B4/B6 (WIRE-1134 §9a; CAT wire §1(e)/(f)).

Operator option A (R-17): an already-paired ACTIVE binding gains its opaque
``binding_generation`` + owner baseline through a signed SUCCESSOR step, never
an edit of its v1 transcript and never a re-pair. Two CRM -> AIDOCS control
calls, both signed by the binding's pinned BINDING-CONTROL key (WIRE §2, C6):

1. ``POST /v1/control/app-bindings/seal-challenge`` -- purpose
   ``binding_authority_seal_challenge``, body ``{binding_id}``. AIDOCS names the
   unsealed binding, its CAS version V, its ``transcript_sha256`` and
   ``contract_digest``, and a fresh AIDOCS challenge; it PRE-MINTS the random
   opaque generation (``gen_`` + 32 random bytes base64url) held with the
   challenge. Binding-scoped and rate limited; a new challenge supersedes the
   previous one.
2. ``POST /v1/control/app-bindings/seal`` -- purpose ``binding_authority_seal``,
   body ``{binding_id, expected_binding_version, transcript_sha256,
   contract_digest, owner_crm_user_id, owner_epoch, challenge, event_id}``.
   AIDOCS commits ATOMICALLY (one authority-DB transaction): the generation +
   ``authority_sha256`` into the binding trust, V -> V+1 (CAS), the
   ``app_binding_authority_sealed`` DECISION row, the owner baseline (a KEYED
   DIGEST of the approving owner + epoch E, never the raw CRM user id), the
   challenge and jti spend, and the receipt keyed by ``event_id``.

The two seal purposes carry NO ``binding_generation`` claim (CAT wire §0); the
claim set is otherwise the WIRE §2 envelope. Every failure before
authentication is ONE non-disclosing ``app_control_assertion_invalid`` (401),
including an unknown, other-tenant, unpaired, paired-but-inactive or disabled
binding. After authentication: ``app_binding_already_sealed`` (409; a lost-ACK
retry of the SAME event + body returns the SAME receipt instead),
``binding_seal_stale`` (409; version moved, transcript/digest differ, or the
challenge is unknown, spent, superseded, expired or of another binding),
``app_seal_rate_limited`` (429) and ``app_seal_unavailable`` (503, retryable:
key or store fault). The jti is consumed only after every envelope check, so a
``body_sha256`` mismatch never spends it.

r0b2 b78c8c86-c65: ``binding_version`` (the BindingRecord CAS counter) and
``binding_generation`` (the sealed trust generation) are separate domains; the
seal bumps the version (V -> V+1, the receipt says so) and never derives one
from the other. A contract upgrade X -> Y makes the seal NON-CURRENT (the trust
loses its generation; DECISION rows and receipts stay as history) until Y is
freshly sealed, which mints a NEW generation; a re-seal never regresses the
owner epoch. The seal is a TRUST transition only: it grants no seat and
admits no user.

Route wiring (HTTP, Bearer extraction, tenant resolution of the per-org
authority DB) is the transport's; this module exposes
:class:`BindingSealService` and its two calls.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Any, Callable, Mapping, Optional, Protocol

from . import jws
from .boundary_audit import audit_metadata
from .binding_store import (
    CREDENTIAL_BINDING_CONTROL,
    BindingRecord,
    BindingRefused,
    BindingState,
    BindingStore,
    ControlPlaneActor,
)
from .jcs import JcsError, jcs_sha256_hex

__all__ = [
    "AUTHORITY_FORMAT",
    "CHALLENGE_RATE_MAX",
    "CHALLENGE_RATE_WINDOW_S",
    "CHALLENGE_TTL_S",
    "OWNER_EPOCH_ACKED",
    "OWNER_EPOCH_APPLIED",
    "OWNER_EPOCH_CONFLICT",
    "OWNER_EPOCH_GENERATION_MISMATCH",
    "OWNER_EPOCH_STALE",
    "OWNER_EPOCH_UNSEALED",
    "PURPOSE_SEAL",
    "PURPOSE_SEAL_CHALLENGE",
    "ROUTE_SEAL",
    "ROUTE_SEAL_CHALLENGE",
    "SEAL_REFUSAL_HTTP",
    "BindingSealService",
    "MemorySealState",
    "SealRefused",
    "SealState",
    "SqliteSealState",
    "authority_sha256",
    "owner_digest_key_from_store",
    "owner_digest_key_store",
    "owner_principal_digest",
]

PURPOSE_SEAL_CHALLENGE = "binding_authority_seal_challenge"
PURPOSE_SEAL = "binding_authority_seal"
ROUTE_SEAL_CHALLENGE = "/v1/control/app-bindings/seal-challenge"
ROUTE_SEAL = "/v1/control/app-bindings/seal"
AUTHORITY_FORMAT = "aidocs.binding-authority/v1"
GENERATION_PREFIX = "gen_"
MAX_ASSERTION_LIFETIME_S = 60
JTI_MIN_KEEP_S = 75  # WIRE C4: kept until max(exp + 10, now + 75)
CHALLENGE_TTL_S = 300
CHALLENGE_RATE_MAX = 10
CHALLENGE_RATE_WINDOW_S = 3600
_OWNER_DIGEST_DOMAIN = b"aidocs/binding-owner-digest/v1"
_MIN_DIGEST_KEY_BYTES = 32

INVALID = "app_control_assertion_invalid"
ALREADY_SEALED = "app_binding_already_sealed"
STALE = "binding_seal_stale"
RATE_LIMITED = "app_seal_rate_limited"
UNAVAILABLE = "app_seal_unavailable"
#: ``apply_owner_epoch`` outcomes (slice 2 owner_epoch_update; the caller maps them to the wire).
OWNER_EPOCH_APPLIED = "applied"
OWNER_EPOCH_ACKED = "acked"
OWNER_EPOCH_CONFLICT = "event_conflict"
OWNER_EPOCH_STALE = "stale"
OWNER_EPOCH_UNSEALED = "unsealed"
OWNER_EPOCH_GENERATION_MISMATCH = "generation_mismatch"
#: code -> (HTTP status, retryable). The wire envelope is C8:
#: ``{"ok": false, "error": {"code", "message", "retryable"}}``.
SEAL_REFUSAL_HTTP: Mapping[str, tuple[int, bool]] = {
    INVALID: (401, False),
    ALREADY_SEALED: (409, False),
    STALE: (409, False),
    RATE_LIMITED: (429, True),
    UNAVAILABLE: (503, True),
}

_CLAIMS = frozenset({"iss", "aud", "iat", "exp", "jti", "tenant_id", "binding_id", "purpose", "body_sha256"})
_OPTIONAL_CLAIMS = frozenset({"nbf"})
_CHALLENGE_BODY = frozenset({"binding_id"})
_SEAL_BODY = frozenset({
    "binding_id", "expected_binding_version", "transcript_sha256", "contract_digest",
    "owner_crm_user_id", "owner_epoch", "challenge", "event_id",
})
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_JTI_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_GENERATION_RE = re.compile(r"gen_[A-Za-z0-9_-]{43}")
_OWNER_RE = re.compile(r"[!-~]{1,256}")  # opaque CRM principal: printable ASCII, no space
_EVENT_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_MAX_SAFE_INT = 2**53 - 1


class SealRefused(Exception):
    """A seal call is refused. ``code`` is the wire code; ``reason`` is an
    internal diagnostic only (never on the wire, never model-facing)."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason or code
        self.http_status, self.retryable = SEAL_REFUSAL_HTTP[code]


#: r0b2 f52809cd-58b: the owner-digest key is a DEDICATED random 32-byte HMAC
#: key, auto-provisioned in the gate key store -- never derived from the S2S
#: secret (rotating that credential must not change a stored owner digest).
OWNER_DIGEST_KEY_PURPOSE = "aidocs/binding-owner-digest/v1"
OWNER_DIGEST_KEY_VERSION = 1
OWNER_DIGEST_KEY_FILE = "binding-owner-digest-v1.json"


def _load_owner_master(store: Any) -> bytes:
    rec = {
        "purpose": OWNER_DIGEST_KEY_PURPOSE,
        "key_version": OWNER_DIGEST_KEY_VERSION,
        "key": jws.b64url_encode(secrets.token_bytes(32)),
    }
    raw = store.create_if_absent(OWNER_DIGEST_KEY_FILE,
                                 json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("ascii"))
    doc = json.loads(raw)
    if doc.get("purpose") != OWNER_DIGEST_KEY_PURPOSE or doc.get("key_version") != OWNER_DIGEST_KEY_VERSION:
        raise ValueError("owner digest key record")
    key = jws.b64url_decode(doc["key"])
    if len(key) != 32:
        raise ValueError("owner digest key length")
    return key


def owner_digest_key_from_store(store: Any) -> Callable[[str], bytes]:
    """Production wiring: ``owner_digest_key=owner_digest_key_from_store(GateKeyStore())``.

    ``tenant_id -> HMAC-SHA256(master, "aidocs/binding-owner-digest/v1|" + tenant_id)``
    where ``master`` is the dedicated store key (created on first use, atomic).
    Any store fault raises; the seal then fails closed ``app_seal_unavailable``."""

    def derive(tenant_id: str) -> bytes:
        if type(tenant_id) is not str or not _ID_RE.fullmatch(tenant_id):
            raise ValueError("tenant_id")
        msg = OWNER_DIGEST_KEY_PURPOSE.encode("ascii") + b"|" + tenant_id.encode("utf-8")
        return hmac.new(_load_owner_master(store), msg, hashlib.sha256).digest()

    return derive


def owner_digest_key_store(state_dir: Any = None) -> Callable[[str], bytes]:
    """THE production factory: ``BindingSealService(..., owner_digest_key=owner_digest_key_store())``.

    ``state_dir`` defaults to the gate key store (``$AIDOCS_APP_KEY_DIR`` or
    ``~/.aidocs/trust/app_keys``). Replaces the retired ``owner_digest_key_from_env``."""
    from .gate_key_store import GateKeyStore

    return owner_digest_key_from_store(GateKeyStore(state_dir))


def owner_principal_digest(key: bytes, tenant_id: str, crm_user_id: str) -> str:
    """The keyed digest of an opaque CRM owner principal (WIRE §1, R-11): HMAC-SHA256
    under a server-held per-org key over a domain tag and the length-prefixed
    tenant and principal. The raw principal is never stored."""
    if type(key) is not bytes or len(key) < _MIN_DIGEST_KEY_BYTES:
        raise ValueError("owner digest key unavailable")
    t, u = tenant_id.encode("utf-8"), crm_user_id.encode("utf-8")
    msg = _OWNER_DIGEST_DOMAIN + len(t).to_bytes(4, "big") + t + len(u).to_bytes(4, "big") + u
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def authority_sha256(*, tenant_id: str, binding_id: str, binding_version: int, transcript_sha256: str,
                     contract_digest: str, binding_generation: str, owner_epoch: int) -> str:
    """WIRE §9a step 3: ``sha256(JCS({"format": "aidocs.binding-authority/v1", ...}))``."""
    return jcs_sha256_hex({
        "format": AUTHORITY_FORMAT,
        "tenant_id": tenant_id,
        "binding_id": binding_id,
        "binding_version": binding_version,
        "transcript_sha256": transcript_sha256,
        "contract_digest": contract_digest,
        "binding_generation": binding_generation,
        "owner_epoch": owner_epoch,
    })


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _refuse_dup(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate member")
        out[k] = v
    return out


def _refuse_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _strict_body(raw: Any) -> dict:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 16 * 1024:
        raise ValueError("body")
    value = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_refuse_dup, parse_constant=_refuse_constant)
    if type(value) is not dict:
        raise ValueError("body is not an object")
    return value


def _int(value: Any, low: int) -> bool:
    return type(value) is int and low <= value <= _MAX_SAFE_INT


def _str(value: Any, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _check_challenge_body(body: dict) -> None:
    if set(body) != _CHALLENGE_BODY or not _str(body["binding_id"], _ID_RE):
        raise ValueError("challenge body")


def _check_seal_body(body: dict) -> None:
    if set(body) != _SEAL_BODY:
        raise ValueError("seal body members")
    ok = (
        _str(body["binding_id"], _ID_RE)
        and _int(body["expected_binding_version"], 1)
        and _str(body["transcript_sha256"], _HEX64)
        and _str(body["contract_digest"], _HEX64)
        and _str(body["owner_crm_user_id"], _OWNER_RE)
        and _int(body["owner_epoch"], 1)
        and _str(body["challenge"], _CHALLENGE_RE)
        and _str(body["event_id"], _EVENT_RE)
    )
    if not ok:
        raise ValueError("seal body shape")


# -- state ------------------------------------------------------------------------------------------------


class SealState(Protocol):
    """Durable seal state; every write joins the ambient authority transaction."""

    def transaction(self) -> Any: ...

    def consume_jti(self, key_sha256: str, *, keep_until: int, now: int) -> bool: ...

    def add_challenge(self, row: Mapping[str, Any], *, now: int) -> None: ...

    def recent_challenges(self, binding_id: str, *, since: int) -> int: ...

    def challenge(self, challenge_sha256: str) -> Optional[dict]: ...

    def spend_challenge(self, challenge_sha256: str) -> bool: ...

    def put_owner(self, binding_id: str, owner_digest: str, owner_epoch: int, sealed_at: int) -> None: ...

    def owner(self, binding_id: str) -> Optional[tuple[str, int]]: ...

    def put_receipt(self, binding_id: str, binding_generation: str, event_id: str, body_sha256: str,
                    receipt: Mapping[str, Any]) -> None: ...

    def receipt(self, binding_id: str, binding_generation: str) -> Optional[dict]: ...

    def replace_owner(self, binding_id: str, expected: tuple[str, int], owner_digest: str, owner_epoch: int,
                      changed_at: int) -> bool: ...

    def owner_event_receipt(self, event_id: str) -> Optional[dict]: ...

    def put_owner_event_receipt(self, event_id: str, binding_id: str, body_sha256: str,
                                ack: Mapping[str, Any]) -> None: ...


class MemorySealState:
    """In-process twin (tests). No rollback: the durable state is the production path."""

    def __init__(self) -> None:
        self._jtis: dict[str, int] = {}
        self._challenges: dict[str, dict] = {}
        self._owners: dict[str, tuple[str, int]] = {}
        self._receipts: dict[str, dict] = {}
        self._owner_events: dict[str, dict] = {}
        self._lock = threading.RLock()

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    def consume_jti(self, key_sha256: str, *, keep_until: int, now: int) -> bool:
        with self._lock:
            for k in [k for k, until in self._jtis.items() if until < now]:
                del self._jtis[k]
            if key_sha256 in self._jtis:
                return False
            self._jtis[key_sha256] = keep_until
            return True

    def add_challenge(self, row: Mapping[str, Any], *, now: int) -> None:
        with self._lock:
            for k in [k for k, r in self._challenges.items()
                      if r["issued_at"] < now - CHALLENGE_RATE_WINDOW_S and r["expires_at"] < now]:
                del self._challenges[k]
            for r in self._challenges.values():
                if r["binding_id"] == row["binding_id"]:
                    r["spent"] = True
            self._challenges[row["challenge_sha256"]] = {**row, "spent": False}

    def recent_challenges(self, binding_id: str, *, since: int) -> int:
        with self._lock:
            return sum(r["binding_id"] == binding_id and r["issued_at"] > since for r in self._challenges.values())

    def challenge(self, challenge_sha256: str) -> Optional[dict]:
        with self._lock:
            r = self._challenges.get(challenge_sha256)
            return dict(r) if r is not None else None

    def spend_challenge(self, challenge_sha256: str) -> bool:
        with self._lock:
            r = self._challenges.get(challenge_sha256)
            if r is None or r["spent"]:
                return False
            r["spent"] = True
            return True

    def put_owner(self, binding_id: str, owner_digest: str, owner_epoch: int, sealed_at: int) -> None:
        with self._lock:
            self._owners[binding_id] = (owner_digest, owner_epoch)

    def owner(self, binding_id: str) -> Optional[tuple[str, int]]:
        return self._owners.get(binding_id)

    def put_receipt(self, binding_id: str, binding_generation: str, event_id: str, body_sha256: str,
                    receipt: Mapping[str, Any]) -> None:
        with self._lock:
            key = (binding_id, binding_generation)
            if key in self._receipts:
                raise ValueError("receipt already written")
            self._receipts[key] = {"event_id": event_id, "body_sha256": body_sha256, "receipt": dict(receipt)}

    def receipt(self, binding_id: str, binding_generation: str) -> Optional[dict]:
        r = self._receipts.get((binding_id, binding_generation))
        return {**r, "receipt": dict(r["receipt"])} if r is not None else None

    def replace_owner(self, binding_id: str, expected: tuple[str, int], owner_digest: str, owner_epoch: int,
                      changed_at: int) -> bool:
        with self._lock:
            if self._owners.get(binding_id) != tuple(expected):
                return False
            self._owners[binding_id] = (owner_digest, owner_epoch)
            return True

    def owner_event_receipt(self, event_id: str) -> Optional[dict]:
        r = self._owner_events.get(event_id)
        return {**r, "ack": dict(r["ack"])} if r is not None else None

    def put_owner_event_receipt(self, event_id: str, binding_id: str, body_sha256: str,
                                ack: Mapping[str, Any]) -> None:
        with self._lock:
            if event_id in self._owner_events:
                raise ValueError("owner event receipt already written")
            self._owner_events[event_id] = {"binding_id": binding_id, "body_sha256": body_sha256, "ack": dict(ack)}


class SqliteSealState:
    """Durable seal state over :class:`~.authority_db.AppAuthorityDB` (``db.seal_state()``)."""

    def __init__(self, db: Any) -> None:
        from .authority_db import AppAuthorityDB

        if not isinstance(db, AppAuthorityDB):
            raise TypeError("a durable seal state needs an AppAuthorityDB")
        self.db = db

    def transaction(self) -> Any:
        return self.db.transaction()

    def consume_jti(self, key_sha256: str, *, keep_until: int, now: int) -> bool:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM control_assertion_jtis WHERE keep_until < ?", (now,))
            cur = conn.execute(
                "INSERT OR IGNORE INTO control_assertion_jtis (key_sha256, keep_until) VALUES (?, ?)",
                (key_sha256, keep_until),
            )
            return cur.rowcount == 1

    def add_challenge(self, row: Mapping[str, Any], *, now: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM binding_seal_challenges WHERE issued_at < ? AND expires_at < ?",
                (now - CHALLENGE_RATE_WINDOW_S, now),
            )
            conn.execute("UPDATE binding_seal_challenges SET spent = 1 WHERE binding_id = ?", (row["binding_id"],))
            conn.execute(
                "INSERT INTO binding_seal_challenges (challenge_sha256, binding_id, binding_version,"
                " transcript_sha256, contract_digest, binding_generation, issued_at, expires_at, spent)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (row["challenge_sha256"], row["binding_id"], row["binding_version"], row["transcript_sha256"],
                 row["contract_digest"], row["binding_generation"], row["issued_at"], row["expires_at"]),
            )

    def recent_challenges(self, binding_id: str, *, since: int) -> int:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM binding_seal_challenges WHERE binding_id = ? AND issued_at > ?",
                (binding_id, since),
            ).fetchone()
        return int(row[0])

    def challenge(self, challenge_sha256: str) -> Optional[dict]:
        with self.db.reading() as conn:
            r = conn.execute(
                "SELECT challenge_sha256, binding_id, binding_version, transcript_sha256, contract_digest,"
                " binding_generation, issued_at, expires_at, spent FROM binding_seal_challenges"
                " WHERE challenge_sha256 = ?",
                (challenge_sha256,),
            ).fetchone()
        if r is None:
            return None
        keys = ("challenge_sha256", "binding_id", "binding_version", "transcript_sha256", "contract_digest",
                "binding_generation", "issued_at", "expires_at", "spent")
        out = dict(zip(keys, r))
        out["spent"] = bool(out["spent"])
        return out

    def spend_challenge(self, challenge_sha256: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE binding_seal_challenges SET spent = 1 WHERE challenge_sha256 = ? AND spent = 0",
                (challenge_sha256,),
            )
            return cur.rowcount == 1

    def put_owner(self, binding_id: str, owner_digest: str, owner_epoch: int, sealed_at: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO binding_owner_authority (binding_id, owner_digest, owner_epoch, sealed_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (binding_id) DO UPDATE SET owner_digest = excluded.owner_digest,"
                " owner_epoch = excluded.owner_epoch, sealed_at = excluded.sealed_at",
                (binding_id, owner_digest, owner_epoch, sealed_at),
            )

    def owner(self, binding_id: str) -> Optional[tuple[str, int]]:
        with self.db.reading() as conn:
            r = conn.execute(
                "SELECT owner_digest, owner_epoch FROM binding_owner_authority WHERE binding_id = ?", (binding_id,)
            ).fetchone()
        return (str(r[0]), int(r[1])) if r else None

    def put_receipt(self, binding_id: str, binding_generation: str, event_id: str, body_sha256: str,
                    receipt: Mapping[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO binding_seal_receipts (binding_id, binding_generation, event_id, body_sha256,"
                " receipt_json) VALUES (?, ?, ?, ?, ?)",
                (binding_id, binding_generation, event_id, body_sha256,
                 json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"))),
            )

    def receipt(self, binding_id: str, binding_generation: str) -> Optional[dict]:
        with self.db.reading() as conn:
            r = conn.execute(
                "SELECT event_id, body_sha256, receipt_json FROM binding_seal_receipts"
                " WHERE binding_id = ? AND binding_generation = ?",
                (binding_id, binding_generation),
            ).fetchone()
        return {"event_id": r[0], "body_sha256": r[1], "receipt": json.loads(r[2])} if r else None

    def replace_owner(self, binding_id: str, expected: tuple[str, int], owner_digest: str, owner_epoch: int,
                      changed_at: int) -> bool:
        """Compare-and-set of the owner authority (slice 2 owner_epoch_update)."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE binding_owner_authority SET owner_digest = ?, owner_epoch = ?, sealed_at = ?"
                " WHERE binding_id = ? AND owner_digest = ? AND owner_epoch = ?",
                (owner_digest, owner_epoch, changed_at, binding_id, expected[0], expected[1]),
            )
            return cur.rowcount == 1

    def owner_event_receipt(self, event_id: str) -> Optional[dict]:
        with self.db.reading() as conn:
            r = conn.execute(
                "SELECT binding_id, body_sha256, ack_json FROM binding_owner_event_receipts WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return {"binding_id": r[0], "body_sha256": r[1], "ack": json.loads(r[2])} if r else None

    def put_owner_event_receipt(self, event_id: str, binding_id: str, body_sha256: str,
                                ack: Mapping[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute(  # a plain INSERT: a racing duplicate fails the whole transaction
                "INSERT INTO binding_owner_event_receipts (event_id, binding_id, body_sha256, ack_json)"
                " VALUES (?, ?, ?, ?)",
                (event_id, binding_id, body_sha256, json.dumps(dict(ack), sort_keys=True, separators=(",", ":"))),
            )


# -- the service ----------------------------------------------------------------------------------------------


class BindingSealService:
    """The seal-challenge + seal calls over one org's binding store and seal state.

    ``owner_digest_key(tenant_id) -> bytes`` is the server-held per-org key of
    the owner-principal digest (production custody: the platform vault, like
    the seat-pin key); a failure fails closed. ``token_factory(nbytes)`` mints
    the challenge and the generation (``secrets.token_urlsafe``).
    """

    def __init__(
        self,
        store: BindingStore,
        state: SealState,
        *,
        owner_digest_key: Callable[[str], bytes],
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._store = store
        self._state = state
        self._owner_key = owner_digest_key
        self._tokens = token_factory

    # -- reads ----------------------------------------------------------------------------------------

    def owner_authority(self, binding_id: str) -> Optional[tuple[str, int]]:
        """``(owner keyed digest, owner_epoch)`` sealed for the binding, or None."""
        return self._state.owner(binding_id)

    def sealed_generation(self, binding_id: str) -> Optional[str]:
        """The binding's sealed ``binding_generation`` (None = unsealed or unknown)."""
        try:
            rec = self._store.get(binding_id)
        except BindingRefused:
            return None
        return rec.trust.binding_generation if rec.trust is not None else None

    # -- slice 2: owner_epoch_update (CAT wire §1(d)) --------------------------------------------------

    def apply_owner_epoch(
        self, *, tenant_id: str, binding_id: str, binding_generation: str, old_epoch: int, new_owner_epoch: int,
        new_owner_crm_user_id: str, event_id: str, body_sha256: str, now: int, decision: Callable[[], None],
    ) -> tuple[str, Optional[dict]]:
        """Move the owner authority for an AUTHENTICATED ``owner_epoch_update``.

        ONE authority transaction (``BEGIN IMMEDIATE`` on the org file: the writer lock
        a reseal or contract upgrade also takes). FIRST, before any receipt or state
        logic, the CURRENT binding record is re-read inside it: ACTIVE, trust present,
        the right tenant and the sealed generation == ``binding_generation`` (the one the
        envelope admitted), else :data:`OWNER_EPOCH_GENERATION_MISMATCH` with nothing
        written -- an event admitted under G1 never moves the owner under G2, and a
        stored G1 receipt is never served once the generation moved (r0b2 738a6e82-d23).
        Then the per-event receipt is read (same body ->
        the stored ACK; another body -> :data:`OWNER_EPOCH_CONFLICT`, even when stale);
        then ``old == stored and new == stored + 1`` CAS-replaces the owner authority
        with the KEYED digest of the new owner (the same digest ``owner_matches`` /
        connect compares), calls ``decision()`` (the typed DECISION row, which joins
        this transaction on the same authority file) and stores the receipt;
        ``new <= stored`` (already applied, or superseded by a later seal) stores the
        receipt with no state change; anything else is :data:`OWNER_EPOCH_STALE`;
        no owner authority is :data:`OWNER_EPOCH_UNSEALED`. Returns ``(outcome, ack)``;
        a store, key or row fault RAISES and the transaction rolls back (no ACK).
        Seats are untouched: this is an authority transition only."""
        ack = {"ok": True, "acked": True, "event_id": event_id}
        with self._state.transaction():
            try:
                rec = self._store.get(binding_id)
            except BindingRefused:
                rec = None
            trust = getattr(rec, "trust", None)
            if (
                rec is None
                or rec.tenant_id != tenant_id
                or rec.state is not BindingState.ACTIVE
                or trust is None
                or type(binding_generation) is not str
                or trust.binding_generation != binding_generation
            ):
                return OWNER_EPOCH_GENERATION_MISMATCH, None
            prior = self._state.owner_event_receipt(event_id)
            if prior is not None:
                if prior["binding_id"] != binding_id or prior["body_sha256"] != body_sha256:
                    return OWNER_EPOCH_CONFLICT, None
                return OWNER_EPOCH_ACKED, dict(prior["ack"])
            current = self._state.owner(binding_id)
            if current is None:
                return OWNER_EPOCH_UNSEALED, None
            stored = current[1]
            if new_owner_epoch <= stored:
                self._state.put_owner_event_receipt(event_id, binding_id, body_sha256, ack)
                return OWNER_EPOCH_ACKED, ack
            if old_epoch != stored or new_owner_epoch != stored + 1:
                return OWNER_EPOCH_STALE, None
            digest = owner_principal_digest(self._owner_key(tenant_id), tenant_id, new_owner_crm_user_id)
            if not self._state.replace_owner(binding_id, current, digest, new_owner_epoch, now):
                raise RuntimeError("the owner authority moved inside its transaction")
            decision()
            self._state.put_owner_event_receipt(event_id, binding_id, body_sha256, ack)
        return OWNER_EPOCH_APPLIED, ack

    # -- the two calls --------------------------------------------------------------------------------

    def issue_challenge(self, *, assertion: str, body: bytes, tenant_id: str, now: int) -> dict:
        """``seal-challenge``: returns ``{ok, binding_id, binding_version, transcript_sha256,
        contract_digest, challenge}`` or raises :class:`SealRefused`."""
        try:
            with self._store.locked_transaction():
                rec, _body, _claims, actor, _sha = self._authenticate(
                    assertion, body, purpose=PURPOSE_SEAL_CHALLENGE, check=_check_challenge_body,
                    tenant_id=tenant_id, now=now,
                )
                if rec.trust.binding_generation is not None:
                    raise self._refuse(actor, rec, ALREADY_SEALED)
                if self._state.recent_challenges(rec.binding_id, since=now - CHALLENGE_RATE_WINDOW_S) \
                        >= CHALLENGE_RATE_MAX:
                    raise self._refuse(actor, rec, RATE_LIMITED)
                challenge = self._tokens(32)
                generation = GENERATION_PREFIX + self._tokens(32)
                if not _str(challenge, _CHALLENGE_RE) or not _str(generation, _GENERATION_RE):
                    raise self._refuse(actor, rec, UNAVAILABLE, "token_factory_shape")
                self._state.add_challenge(
                    {
                        "challenge_sha256": _sha256_text(challenge),
                        "binding_id": rec.binding_id,
                        "binding_version": rec.binding_version,
                        "transcript_sha256": rec.trust.transcript_sha256,
                        "contract_digest": rec.contract_digest,
                        "binding_generation": generation,
                        "issued_at": now,
                        "expires_at": now + CHALLENGE_TTL_S,
                    },
                    now=now,
                )
        except SealRefused:
            raise
        except Exception:  # noqa: BLE001 -- a store fault: the transaction rolled back
            raise SealRefused(UNAVAILABLE, "store_fault") from None
        return {
            "ok": True,
            "binding_id": rec.binding_id,
            "binding_version": rec.binding_version,
            "transcript_sha256": rec.trust.transcript_sha256,
            "contract_digest": rec.contract_digest,
            "challenge": challenge,
        }

    def seal(self, *, assertion: str, body: bytes, tenant_id: str, now: int) -> dict:
        """``seal``: returns ``{ok, event_id, binding_generation, binding_version, owner_epoch}``
        (the SAME receipt for a lost-ACK retry) or raises :class:`SealRefused`."""
        try:
            with self._store.locked_transaction():
                rec, b, claims, actor, body_sha = self._authenticate(
                    assertion, body, purpose=PURPOSE_SEAL, check=_check_seal_body, tenant_id=tenant_id, now=now,
                )
                if rec.trust.binding_generation is not None:
                    prior = self._state.receipt(rec.binding_id, rec.trust.binding_generation)
                    if prior is not None and prior["event_id"] == b["event_id"] and prior["body_sha256"] == body_sha:
                        return dict(prior["receipt"])
                    raise self._refuse(actor, rec, ALREADY_SEALED)
                ch_sha = _sha256_text(b["challenge"])
                ch = self._state.challenge(ch_sha)
                if ch is None or ch["spent"] or ch["binding_id"] != rec.binding_id or now > ch["expires_at"]:
                    raise self._refuse(actor, rec, STALE, "challenge")
                if (ch["binding_version"], ch["transcript_sha256"], ch["contract_digest"]) != (
                    b["expected_binding_version"], b["transcript_sha256"], b["contract_digest"]
                ):
                    raise self._refuse(actor, rec, STALE, "challenge_facts")
                if (rec.binding_version, rec.trust.transcript_sha256, rec.contract_digest) != (
                    b["expected_binding_version"], b["transcript_sha256"], b["contract_digest"]
                ):
                    raise self._refuse(actor, rec, STALE, "binding_moved")
                try:
                    digest = owner_principal_digest(self._owner_key(rec.tenant_id), rec.tenant_id,
                                                    b["owner_crm_user_id"])
                except Exception:  # noqa: BLE001 -- key custody fault fails closed
                    raise self._refuse(actor, rec, UNAVAILABLE, "owner_digest_key") from None
                generation = ch["binding_generation"]
                epoch = b["owner_epoch"]
                baseline = self._state.owner(rec.binding_id)
                if baseline is not None and epoch < baseline[1]:
                    # a re-seal after a contract upgrade never regresses the owner epoch
                    raise self._refuse(actor, rec, STALE, "owner_epoch_regressed")
                auth = authority_sha256(
                    tenant_id=rec.tenant_id, binding_id=rec.binding_id, binding_version=rec.binding_version,
                    transcript_sha256=rec.trust.transcript_sha256, contract_digest=rec.contract_digest,
                    binding_generation=generation, owner_epoch=epoch,
                )
                receipt = {
                    "ok": True,
                    "event_id": b["event_id"],
                    "binding_generation": generation,
                    "binding_version": rec.binding_version + 1,
                    "owner_epoch": epoch,
                }
                meta = {"owner_epoch": epoch, "event_id": b["event_id"],
                        **audit_metadata({"proof_jti": claims["jti"]})}
                try:
                    self._store.commit_authority_seal(
                        actor, rec, binding_generation=generation, authority_sha256=auth, **meta,
                    )
                except BindingRefused as exc:
                    code = {ALREADY_SEALED: ALREADY_SEALED, "version_conflict": STALE,
                            "invalid_transition": STALE}.get(exc.code, UNAVAILABLE)
                    raise SealRefused(code, exc.code) from None
                if not self._state.spend_challenge(ch_sha):
                    raise self._refuse(actor, rec, STALE, "challenge_race")
                self._state.put_owner(rec.binding_id, digest, epoch, now)
                self._state.put_receipt(rec.binding_id, generation, b["event_id"], body_sha, receipt)
        except SealRefused:
            raise
        except Exception:  # noqa: BLE001 -- a store fault: the ONE transaction rolled back
            self._store.audit_refusal(None, tenant_id if type(tenant_id) is str and tenant_id else "<unknown>",
                                      None, UNAVAILABLE)
            raise SealRefused(UNAVAILABLE, "store_fault") from None
        return receipt

    # -- internals ------------------------------------------------------------------------------------

    def _refuse(self, actor: Any, rec: BindingRecord, code: str, reason: str = "") -> SealRefused:
        self._store.audit_refusal(actor, rec.tenant_id, rec.binding_id, code, toolspace_id=rec.toolspace_id)
        return SealRefused(code, reason)

    def _authenticate(
        self, assertion: Any, body: Any, *, purpose: str, check: Callable[[dict], None], tenant_id: str, now: int,
    ) -> tuple[BindingRecord, dict, dict, ControlPlaneActor, str]:
        """WIRE §2 steps 1-11 for the two seal purposes (no generation claim).
        Every failure is ONE non-disclosing ``app_control_assertion_invalid``."""

        def bad(reason: str) -> SealRefused:
            return SealRefused(INVALID, reason)

        if type(assertion) is not str or not assertion or type(now) is not int or type(tenant_id) is not str:
            raise bad("framing")
        try:
            parsed = _strict_body(body)
            check(parsed)
            body_sha = jcs_sha256_hex(parsed)
        except (ValueError, UnicodeDecodeError, RecursionError, JcsError):
            raise bad("body") from None
        try:
            rec = self._store.get(parsed["binding_id"])
        except BindingRefused:
            raise bad("binding_unknown") from None
        trust = rec.trust
        if rec.tenant_id != tenant_id or rec.state is not BindingState.ACTIVE or trust is None:
            raise bad("binding_not_servable")
        try:
            verified = jws.verify_compact(assertion, expected_typ=jws.TYP_CONTROL, keyset=trust.binding_control_keyset)
        except jws.JwsError:
            raise bad("jose") from None
        p = verified.payload
        if not (_CLAIMS <= set(p) <= _CLAIMS | _OPTIONAL_CLAIMS):
            raise bad("claim_members")
        try:
            jws.check_lifetime(p, now=now, max_lifetime_s=MAX_ASSERTION_LIFETIME_S)
        except jws.JwsError:
            raise bad("lifetime") from None
        if "nbf" in p and (type(p["nbf"]) is not int or p["nbf"] > now + jws.MAX_CLOCK_SKEW_S):
            raise bad("nbf")
        expected = {
            "iss": rec.pack_id,
            "aud": trust.aidocs_origin,
            "tenant_id": rec.tenant_id,
            "binding_id": rec.binding_id,
            "purpose": purpose,
        }
        for name, want in expected.items():
            if type(p[name]) is not str or p[name] != want:
                raise bad(f"{name}_mismatch")
        if not _str(p["jti"], _JTI_RE):
            raise bad("jti")
        if type(p["body_sha256"]) is not str or p["body_sha256"] != body_sha:
            raise bad("body_sha256")  # BEFORE the jti consume: a mismatch never spends it
        key = jcs_sha256_hex([p["iss"], verified.header["kid"], p["jti"]])
        try:
            fresh = self._state.consume_jti(key, keep_until=max(p["exp"] + jws.MAX_CLOCK_SKEW_S, now + JTI_MIN_KEEP_S),
                                            now=now)
        except Exception:  # noqa: BLE001 -- replay store fault fails closed
            raise SealRefused(UNAVAILABLE, "jti_store") from None
        if not fresh:
            raise bad("jti_replay")
        actor = ControlPlaneActor(user_id="binding-control:" + verified.header["kid"],
                                  credential_class=CREDENTIAL_BINDING_CONTROL)
        return rec, parsed, dict(p), actor, body_sha
