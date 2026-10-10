"""#1134 Phase B2: the ChatGPT connect ceremony, AIDOCS side.

WIRE-1134 §2, §3, §4, §7 (r0b2 rulings R-1..R-19) and the CRM side as implemented
(``.MEMORY/sessions/ubermega/WIRE-1134-CAT-for-phaseB.md``, which supersedes CAT's
own file). Transport-free: the gate transport owns every route and calls these
methods with the raw request parts.

The ceremony STARTS AT CHATGPT:

1. ChatGPT ``GET /oauth/authorize`` for ``/v1/apps/<cid>/mcp`` ->
   :meth:`ConnectService.start`: the binding must be ACTIVE and SEALED (B6
   generation). A single-use authz txn (default 300 s, hard max 600 s) and a
   same-browser cookie ``__Host-aidocs_atxn`` are created; ``303`` top-level to
   ``<application_origin><internal_endpoints.chatgpt_connect>?req=<authz_request_id>``.
   ``req`` is a challenge, never a bearer authority (R-1).
2. CRM consent, then the CRM POSTs ``chatgpt_connect`` S2S ->
   :meth:`ConnectService.chatgpt_connect`: the shared control-assertion envelope
   (§2: binding-control key ONLY, exact claims, 60 s, body_sha256 over JCS
   checked BEFORE the durable jti consume, generation checked only after
   authentication), then the purpose checks (§3). Answer ``{"ok": true,
   "continue_url": "<aidocs_origin>/apps/connect/return?ret=..."}``.
3. ``GET /apps/connect/return?ret=`` (:meth:`connect_return`): the cookie's txn,
   a live single-use ``ret`` (<= 60 s) -> ``303 /apps/connect/complete``.
4. ``GET /apps/connect/complete`` (:meth:`connect_complete`): re-check the binding,
   generation and owner epoch; CN seat ``admit`` (B5; ``ceremonyId`` = the txn's
   ``authz_request_sha256``); ``link_finalize`` to the CRM (org-key control JWS,
   sealed generation); after the ACK, CN ``confirm``; then and only then the
   OAuth code, bound to the EXACT seat references (:class:`SeatFamily`).
   No ACK, no code. A definitive finalize refusal or the txn's terminal expiry
   abandons ONLY this ceremony's provisional seat (CN ``abandon``) and sends the
   ``link_abandon`` compensation; refusals return top-level to the CRM's
   ``connect_result`` page with a non-secret ``outcome``.

``seat_quota`` (§4) relays the CodeNexus numbers through the same envelope.

Interfaces assumed from the parallel builders (the conductor reconciles):

* B5 seat client (``aidocs_mcp.app_packs.seat_service_client.SeatServiceClient``):
  ``admit(...) -> .seat_binding_id/.connector_principal_id``, ``confirm(...)``,
  ``abandon(...)``, ``quota(installation_id=) -> .crm_answer()``; a CN business
  refusal raises ``SeatServiceRefused`` (``.code``), anything else is unavailable.
* B6 binding authority: ``generation(tenant_id, binding_id) -> Optional[str]``
  (the SEALED generation) and ``owner_matches(tenant_id, binding_id, owner_epoch,
  crm_user_id) -> bool`` (the sealed owner baseline + epoch updates) and
  ``owner_epoch_update(...)`` (slice 2: CAT wire §1(d), served by :meth:`ConnectService.owner_epoch`).

HOLDS (named, not hidden): the ``link_abandon`` compensation is sent inline and
best-effort (the durable outbox + periodic reconciliation of §7.5 step 3/4 are
not built here); the C11 DECISION rows for these transitions need registered
audit kinds and are not written yet.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import urlencode

from types import MappingProxyType

from .. import _sqlite_connect
from ..execution_event_retention import EVENT_KIND_RETENTION, RetentionClass
from . import jws
from .binding_store import RETENTION_DECISION, BindingState
from .boundary_audit import KIND_STATUS, audit_metadata, kind_status_admissible
from .jcs import JcsError, canonicalize
from .seat_admission import SeatFamily, is_typed_refusal

__all__ = [
    "COOKIE_NAME",
    "CRM_OUTCOMES",
    "FINALIZE_ATTEMPTS",
    "RET_TTL_S",
    "TXN_TTL_S",
    "AuthzTxn",
    "BrowserResult",
    "ConnectService",
    "ConnectTarget",
    "ControlResult",
    "RuntimeCrmLinkClient",
    "SqliteConnectStore",
    "classify_crm_ack",
    "link_control_claims",
]

TXN_TTL_S = 300  # R-1: default 300 s, hard max 600 s
RET_TTL_S = 60  # R-1: ret <= 60 s, single-use, non-authoritative
FINALIZE_ATTEMPTS = 2  # the exact same body, fresh jti each (R-12)
CONTROL_LIFETIME_S = 60
JTI_RETENTION_S = 75
COOKIE_NAME = "__Host-aidocs_atxn"
RETURN_PATH = "/apps/connect/return"
COMPLETE_PATH = "/apps/connect/complete"

#: The CRM's /connect/result outcome vocabulary (CAT wire §3).
CRM_OUTCOMES = frozenset(
    {"seats_exhausted", "app_seat_required", "app_binding_disabled", "app_identity_unlinked", "app_seat_conflict"}
)
#: Definitive CRM finalize/abandon refusals (CAT wire §2): retrying cannot help.
_CRM_DEFINITIVE = frozenset(
    {
        "crm_link_state_invalid", "crm_binding_generation_mismatch", "crm_seat_assignment_inactive",
        "crm_user_disabled", "crm_validation_failed",
    }
)

CODE_ASSERTION_INVALID = "app_control_assertion_invalid"
CODE_GENERATION_MISMATCH = "app_binding_generation_mismatch"
CODE_REQUEST_INVALID = "app_connect_request_invalid"
CODE_OWNER_STALE = "app_owner_epoch_stale"
CODE_CONFLICT = "app_connect_conflict"
CODE_SEAT_REQUIRED = "app_seat_required"
CODE_SEAT_UNAVAILABLE = "app_seat_service_unavailable"
CODE_INTERNAL = "app_internal"
CODE_EVENT_CONFLICT = "app_event_conflict"
CODE_IDENTITY_CONFLICT = "app_seat_identity_conflict"
CODE_REVOKE_REFUSED = "app_seat_revoke_refused"
_MESSAGES = {
    CODE_EVENT_CONFLICT: "The event conflicts with an earlier one.",
    CODE_IDENTITY_CONFLICT: "The seat identity does not match the active seat.",
    CODE_REVOKE_REFUSED: "The seat service refused the revoke.",
    CODE_ASSERTION_INVALID: "The control request was refused.",
    CODE_GENERATION_MISMATCH: "The binding generation does not match.",
    CODE_REQUEST_INVALID: "The connect request was refused.",
    CODE_OWNER_STALE: "The owner epoch is not current.",
    CODE_CONFLICT: "The connect request conflicts with an earlier one.",
    CODE_SEAT_REQUIRED: "An active seat is required.",
    CODE_SEAT_UNAVAILABLE: "The seat service is unavailable.",
    CODE_INTERNAL: "Internal error.",
}
_RETRYABLE = frozenset({CODE_SEAT_UNAVAILABLE, CODE_INTERNAL})

PENDING, ASSERTED, ADMITTED, FINALIZED = "pending", "asserted", "admitted", "finalized"
#: NON-terminal: the code is being issued (attempt n). A failure here is retryable;
#: each attempt bumps ``issue_attempt`` by CAS, so at most one attempt is current.
ISSUING = "issuing"
#: NON-terminal: the current attempt won the right to write the released rows.
RELEASING = "releasing"
#: States that hold an admitted CN seat and MAY be abandoned (refusal / expiry). RELEASING is
#: deliberately absent (r0b2 42ea2c57-089 C): once acquired, only its holder moves it.
SEATED = frozenset({"admitted", "finalized", ISSUING})
CODE_RELEASED, REFUSED, ABANDONED, EXPIRED = "code_released", "refused", "abandoned", "expired"
TERMINAL = frozenset({CODE_RELEASED, REFUSED, ABANDONED, EXPIRED})

_REQ_RE = re.compile(r"[A-Za-z0-9._~-]{16,200}")
_JTI_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_TEXT = 300
_CLAIMS_BASE = frozenset({"iss", "aud", "iat", "exp", "jti", "tenant_id", "binding_id", "purpose", "body_sha256"})
_REL_PATH = re.compile(r"/[A-Za-z0-9._~/-]*")

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


# -- results + seams --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectTarget:
    """Everything the ceremony needs about ONE active binding, resolved server-side."""

    binding: Any  # BindingRecord (duck-typed: binding_id, tenant_id, pack_id, state, trust, ...)
    organization_id: str
    installation_id: str
    connector_id: str
    resource: str
    endpoints: Mapping[str, str]  # the bundle's closed internal_endpoints (relative paths)


@dataclass(frozen=True)
class BrowserResult:
    status: int
    location: Optional[str] = None
    html: str = ""
    set_cookie: Optional[str] = None
    reason: str = ""
    headers: Mapping[str, str] = field(default_factory=lambda: dict(_PAGE_HEADERS))


@dataclass(frozen=True)
class ControlResult:
    status: int
    body: Mapping[str, Any]
    reason: str = ""


class ConnectDirectory(Protocol):
    def for_connector(self, connector_id: str, resource: str) -> Optional[ConnectTarget]: ...

    def for_binding(self, binding_id: str) -> Optional[ConnectTarget]: ...


class BindingAuthority(Protocol):
    """B6: the sealed generation + owner baseline."""

    def generation(self, tenant_id: str, binding_id: str) -> Optional[str]: ...

    def owner_matches(self, tenant_id: str, binding_id: str, owner_epoch: int, crm_user_id: str) -> bool: ...

    def owner_epoch_update(self, tenant_id: str, binding_id: str, *, update: Mapping[str, Any], body_sha256: str,
                           now: int, decision: Callable[[], None]) -> tuple[str, Optional[dict]]:
        """B6 ``BindingSealService.apply_owner_epoch`` for the binding's org."""
        ...


class CrmLinkClient(Protocol):
    """AIDOCS -> CRM link_finalize / link_abandon: ``acked`` | ``refused`` | ``unavailable``."""

    def finalize(self, target: ConnectTarget, *, generation: str, sub: str, body: Mapping[str, Any]) -> str: ...

    def link_abandon(self, target: ConnectTarget, *, generation: str, sub: str, body: Mapping[str, Any]) -> str: ...


# -- the txn ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthzTxn:
    authz_request_sha256: str
    cookie_sha256: str
    oauth_request_sha256: str
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    scope: tuple
    resource: str
    connector_id: str
    tenant_id: str
    organization_id: str
    installation_id: str
    binding_id: str
    binding_generation: str
    status: str
    created_at: int
    expires_at: int
    kind: str = "oauth"
    asserted: Optional[Mapping[str, Any]] = None
    ret_sha256: Optional[str] = None
    ret_expires_at: int = 0
    returned: bool = False
    seat_binding_id: Optional[str] = None
    connector_sub: Optional[str] = None
    finalize_body: Optional[Mapping[str, Any]] = None
    issue_attempt: int = 0

    def to_json(self) -> str:
        doc = asdict(self)
        doc["scope"] = list(self.scope)
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "AuthzTxn":
        doc = json.loads(raw)
        doc["scope"] = tuple(doc.get("scope") or ())
        return cls(**doc)


#: The actor of every AIDOCS-driven connect transition (the CRM's own call is the
#: authenticated ``chatgpt_connect``; its row carries the same actor for one chain).
CONNECT_ACTOR = "aidocs_connect_ceremony"
#: C11: the connect-ceremony DECISION kinds (registered in boundary_audit.KIND_STATUS and
#: execution_event_retention, exactly like the S7 / B6 control-plane kinds).
CONNECT_CONTROL_KINDS = frozenset(
    {
        "app_connect_authorize_created", "app_connect_asserted", "app_seat_admitted", "app_link_finalized",
        "app_seat_confirmed", "app_connect_code_released", "app_connect_refused", "app_connect_abandoned",
        "app_link_abandon_sent", "app_seat_revoked", "app_owner_epoch_updated",
    }
)


@dataclass(frozen=True)
class ConnectControlEvent:
    """A §16.1 DECISION-class connect transition: registered kind, ``status`` derived
    from the closed matrix, allowlisted typed metadata only -- anything else raises."""

    kind: str
    tenant_id: str
    actor_user_id: str
    binding_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retention: str = RETENTION_DECISION
    status: str = field(init=False)

    def __post_init__(self) -> None:
        if self.kind not in CONNECT_CONTROL_KINDS or self.kind not in KIND_STATUS:
            raise ValueError(f"unregistered connect audit kind {self.kind!r}")
        if EVENT_KIND_RETENTION.get(self.kind) is not RetentionClass.DECISION:
            raise ValueError(f"{self.kind!r} is not DECISION in the retention registry")
        status = KIND_STATUS[self.kind]
        md = {k: v for k, v in {**dict(self.metadata), "status": status}.items() if v is not None}
        if audit_metadata(md) != md:
            raise ValueError(f"metadata outside the audit allowlist or shape: {sorted(md)}")
        if not kind_status_admissible(self.kind, status):  # pragma: no cover -- matrix self-check
            raise ValueError("kind/status pair is off the closed matrix")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", MappingProxyType(md))


class SqliteConnectStore:
    """Durable, gate-wide authz txn store (one SQLite file).

    The txn is keyed by ``sha256(authz_request_id)`` and its cookie by
    ``sha256(cookie)``: neither raw value is stored (C16). Transitions are
    compare-and-set on the whole row. The control-assertion jti replay set is NOT
    here: it is the per-org ``control_assertion_jtis`` table (WIRE §0, B6), shared
    by every CRM -> AIDOCS purpose and reached through ``consume_jti``."""

    def __init__(self, path: Any) -> None:
        self._path = Path(path)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app_authz_txns ("
                "authz_request_sha256 TEXT PRIMARY KEY, cookie_sha256 TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL, expires_at INTEGER NOT NULL, doc TEXT NOT NULL)"
            )
            conn.commit()

    def _conn(self):
        return _sqlite_connect.connect(self._path, durability=_sqlite_connect.Durability.AUDIT, row_factory=False)

    def create(self, txn: AuthzTxn) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO app_authz_txns (authz_request_sha256, cookie_sha256, status, expires_at, doc) "
                "VALUES (?,?,?,?,?)",
                (txn.authz_request_sha256, txn.cookie_sha256, txn.status, txn.expires_at, txn.to_json()),
            )
            conn.commit()

    def _one(self, column: str, value: str) -> Optional[AuthzTxn]:
        with self._conn() as conn:
            row = conn.execute(f"SELECT doc FROM app_authz_txns WHERE {column} = ?", (value,)).fetchone()
        return AuthzTxn.from_json(row[0]) if row else None

    def by_request(self, authz_request_sha256: str) -> Optional[AuthzTxn]:
        return self._one("authz_request_sha256", authz_request_sha256)

    # -- fix 6: seat_revoke receipts (WIRE-1134 §5 app_seat_event_receipts), per tenant + event_id --

    def _receipts(self, conn) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_seat_event_receipts (tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, "
            "body_sha256 TEXT NOT NULL, ack_json TEXT NOT NULL, PRIMARY KEY (tenant_id, event_id))"
        )

    def event_receipt(self, tenant_id: str, event_id: str) -> Optional[tuple[str, dict]]:
        with self._conn() as conn:
            self._receipts(conn)
            row = conn.execute(
                "SELECT body_sha256, ack_json FROM app_seat_event_receipts WHERE tenant_id = ? AND event_id = ?",
                (tenant_id, event_id),
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def put_event_receipt(self, tenant_id: str, event_id: str, body_sha256: str, ack: Mapping[str, Any]) -> None:
        with self._conn() as conn:
            self._receipts(conn)
            conn.execute(
                "INSERT OR IGNORE INTO app_seat_event_receipts (tenant_id, event_id, body_sha256, ack_json) "
                "VALUES (?,?,?,?)",
                (tenant_id, event_id, body_sha256, json.dumps(dict(ack), sort_keys=True)),
            )
            conn.commit()

    def by_cookie(self, cookie_sha256: str) -> Optional[AuthzTxn]:
        return self._one("cookie_sha256", cookie_sha256)

    def cas(self, old: AuthzTxn, new: AuthzTxn) -> bool:
        """Replace ``old`` with ``new`` iff the stored row is still exactly ``old``."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE app_authz_txns SET status = ?, doc = ? WHERE authz_request_sha256 = ? AND doc = ?",
                (new.status, new.to_json(), old.authz_request_sha256, old.to_json()),
            )
            conn.commit()
            return cur.rowcount == 1



# -- pure wire helpers ---------------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any, *, max_len: int = _MAX_TEXT) -> bool:
    return type(value) is str and 0 < len(value) <= max_len


def _uint(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _rfc3339(now: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def link_control_claims(
    binding: Any, *, purpose: str, generation: str, sub: str, body: Mapping[str, Any], now: int, jti: str,
    call_id: str,
) -> dict:
    """WIRE §7.4 / C7 exact claim set for ``link_finalize`` / ``link_abandon``
    (AIDOCS org key; the sealed ``binding_generation`` is REQUIRED)."""
    if purpose not in ("link_finalize", "link_abandon"):
        raise ValueError("not a link control purpose")
    if not (_text(generation) and _text(sub) and _text(jti) and _text(call_id)) or type(now) is not int:
        raise ValueError("link control facts malformed")
    return {
        "iss": binding.trust.aidocs_issuer,
        "aud": binding.audience,
        "iat": now,
        "exp": now + CONTROL_LIFETIME_S,
        "jti": jti,
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "pack_id": binding.pack_id,
        "contract_digest": binding.contract_digest,
        "binding_version": binding.binding_version,
        "binding_generation": generation,
        "purpose": purpose,
        "sub": sub,
        "call_id": call_id,
        "body_sha256": hashlib.sha256(canonicalize(dict(body))).hexdigest(),
    }


def classify_crm_ack(status: Any, body: Any, *, link_state_id: str) -> str:
    """CAT wire §2: ``acked`` only for the exact idempotent ACK echoing this
    ``link_state_id``; ``refused`` for a definitive CRM code; else ``unavailable``."""
    if not isinstance(body, Mapping):
        return "unavailable"
    if status == 200 and body.get("ok") is True and body.get("acked") is True:
        return "acked" if body.get("link_state_id") == link_state_id else "unavailable"
    err = body.get("error")
    if isinstance(status, int) and 400 <= status < 500 and isinstance(err, Mapping):
        if err.get("code") in _CRM_DEFINITIVE:
            return "refused"
    return "unavailable"


def _strict_json(body: Any) -> Any:
    def pairs(items: list) -> dict:
        out: dict = {}
        for k, v in items:
            if k in out:
                raise ValueError("duplicate member")
            out[k] = v
        return out

    def constant(_name: str) -> Any:
        raise ValueError("non-finite number")

    if type(body) is not bytes:
        raise ValueError("body is not bytes")
    return json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def _connect_body_ok(p: Mapping[str, Any]) -> bool:
    keys = {
        "crm_user_id", "seat_assignment_id", "seat_version", "assigned_by_owner_crm_user_id", "owner_epoch",
        "authz_request_id", "link_state_id", "binding_id", "binding_generation",
    }
    return (
        set(p) == keys
        and _text(p["crm_user_id"])
        and type(p["seat_assignment_id"]) is str and len(p["seat_assignment_id"]) <= _MAX_TEXT
        and _uint(p["seat_version"])
        and _text(p["assigned_by_owner_crm_user_id"])
        and _uint(p["owner_epoch"], 1)
        and type(p["authz_request_id"]) is str and _REQ_RE.fullmatch(p["authz_request_id"]) is not None
        and _text(p["link_state_id"])
        and _text(p["binding_id"])
        and _text(p["binding_generation"])
    )


_REVOKE_REASONS = frozenset({"unseated", "user_disabled", "owner_revoke_all"})
_EVENT_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _revoke_body_ok(p: Mapping[str, Any]) -> bool:
    keys = {
        "crm_user_id", "seat_assignment_id", "seat_version", "reason", "revoked_by_crm_user_id", "revoked_at",
        "event_id", "binding_id", "binding_generation",
    }
    return (
        set(p) == keys
        and _text(p["crm_user_id"])
        and _text(p["seat_assignment_id"])
        and _uint(p["seat_version"])
        and p["reason"] in _REVOKE_REASONS
        and _text(p["revoked_by_crm_user_id"])
        and _text(p["revoked_at"])
        and type(p["event_id"]) is str and _EVENT_RE.fullmatch(p["event_id"]) is not None
        and _text(p["binding_id"])
        and _text(p["binding_generation"])
    )


_OWNER_RE = re.compile(r"[!-~]{1,256}")  # the seal's opaque CRM principal shape (binding_seal._OWNER_RE)
_MAX_SAFE_INT = 2**53 - 1


def _epoch(value: Any) -> bool:
    return type(value) is int and 1 <= value <= _MAX_SAFE_INT


def _owner_epoch_body_ok(p: Mapping[str, Any]) -> bool:
    """CAT wire §1(d): the closed owner_epoch_update body; ``event_id`` is ``owner_epoch_<new>``."""
    keys = {
        "old_epoch", "new_owner_epoch", "new_owner_crm_user_id", "changed_at", "event_id", "binding_id",
        "binding_generation",
    }
    return (
        set(p) == keys
        and _epoch(p["old_epoch"])
        and _epoch(p["new_owner_epoch"])
        and type(p["new_owner_crm_user_id"]) is str and _OWNER_RE.fullmatch(p["new_owner_crm_user_id"]) is not None
        and _text(p["changed_at"])
        and p["event_id"] == f"owner_epoch_{p['new_owner_epoch']}"
        and _text(p["binding_id"])
        and _text(p["binding_generation"])
    )


def _quota_body_ok(p: Mapping[str, Any]) -> bool:
    return set(p) == {"binding_id", "binding_generation"} and _text(p["binding_id"]) and _text(p["binding_generation"])


def _quota_answer(answer: Any) -> Optional[dict]:
    keys = {"ok", "purchased", "active", "available", "plan_version", "as_of"}
    if not isinstance(answer, Mapping) or set(answer) != keys or answer.get("ok") is not True:
        return None
    if not all(_uint(answer[k]) for k in ("purchased", "active", "available")):
        return None
    if not (_uint(answer["plan_version"]) or _text(answer["plan_version"])) or not _text(answer["as_of"]):
        return None
    return dict(answer)


def _rel_endpoint(endpoints: Mapping[str, Any], name: str) -> Optional[str]:
    value = endpoints.get(name) if isinstance(endpoints, Mapping) else None
    if type(value) is not str or not _REL_PATH.fullmatch(value) or "//" in value:
        return None
    return value


class _Refused(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def _control_error(code: str, status: int) -> ControlResult:
    return ControlResult(
        status, {"ok": False, "error": {"code": code, "message": _MESSAGES[code], "retryable": code in _RETRYABLE}},
        code,
    )


def _page(status: int, reason: str, title: str = "Connection not possible") -> BrowserResult:
    # Fixed and non-disclosing: never says which check failed.
    html = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>"
        f"{title}</title></head><body><main><h1>{title}</h1><p>This connection cannot be completed. "
        "Start again from ChatGPT.</p></main></body></html>"
    )
    return BrowserResult(status, html=html, reason=reason)


# -- the service ---------------------------------------------------------------------------------------


class ConnectService:
    """The B2 ceremony (see module doc)."""

    def __init__(
        self,
        *,
        store: SqliteConnectStore,
        directory: ConnectDirectory,
        authority: BindingAuthority,
        seats: Any,
        crm: CrmLinkClient,
        issue_code: Callable[[AuthzTxn, SeatFamily], str],
        consume_jti: Callable[[str, str, int, int], bool],
        void_codes: Callable[..., None],
        audit: Callable[[Any], None],
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        txn_ttl_s: int = TXN_TTL_S,
    ) -> None:
        if type(txn_ttl_s) is not int or not 0 < txn_ttl_s <= 600:
            raise ValueError("the authz txn lifetime is 1..600 s (R-1)")
        self._store = store
        self._directory = directory
        self._authority = authority
        self._seats = seats
        self._crm = crm
        self._issue_code = issue_code
        # (tenant_id, key_sha256, keep_until, now) -> fresh? The ONE per-org replay
        # store of every CRM -> AIDOCS control purpose (B6 SqliteSealState.consume_jti).
        self._consume_jti = consume_jti
        # (txn, attempt=None) -> spend the ceremony's undelivered codes (all, or one attempt).
        self._void_codes = void_codes
        # C11: the binding's own org DECISION sink (AppAuthorityDB.emit_decision commits on write).
        self._audit = audit
        self._random = random_bytes
        self._ttl = txn_ttl_s

    def _emit(self, kind: str, txn: AuthzTxn, **extra: Any) -> None:
        """Write ONE typed DECISION row for ``txn``. Raises when it cannot be written.
        Rows carry the ids that rebuild the chain, never a secret, code or raw CRM user id."""
        md = {
            "authz_request_sha256": txn.authz_request_sha256, "tenant_id": txn.tenant_id,
            "organization_id": txn.organization_id, "installation_id": txn.installation_id,
            "binding_id": txn.binding_id, "binding_generation": txn.binding_generation,
            "connector_id": txn.connector_id,
            # CRM / CN-supplied opaque evidence is kept only when it has its allowlisted shape.
            **{k: v for k, v in extra.items() if v is not None and audit_metadata({k: v})},
        }
        self._audit(ConnectControlEvent(kind, txn.tenant_id, CONNECT_ACTOR, txn.binding_id, md))

    def _emit_best_effort(self, kind: str, txn: AuthzTxn, **extra: Any) -> None:
        try:
            self._emit(kind, txn, **extra)
        except Exception:  # noqa: BLE001 -- a refusal/compensation row never changes the outcome
            pass

    def _seat_facts(self, txn: AuthzTxn) -> dict:
        a = txn.asserted or {}
        return {
            "seat_binding_id": txn.seat_binding_id, "sub": txn.connector_sub,
            "crm_seat_assignment_id": a.get("seat_assignment_id"),
            "crm_seat_assignment_version": a.get("seat_version"),
        }

    # -- helpers -------------------------------------------------------------------------------------

    def _token(self) -> str:
        return jws.b64url_encode(self._random(32))

    @staticmethod
    def _active(target: Any) -> bool:
        rec = getattr(target, "binding", None)
        return rec is not None and rec.state is BindingState.ACTIVE and rec.trust is not None

    def _sealed(self, tenant_id: str, binding_id: str) -> Optional[str]:
        gen = self._authority.generation(tenant_id, binding_id)
        return gen if _text(gen) else None

    def _target(self, binding_id: str) -> Optional[ConnectTarget]:
        try:
            target = self._directory.for_binding(binding_id)
        except Exception:  # noqa: BLE001 -- we could not resolve it: never a binding
            return None
        return target if isinstance(target, ConnectTarget) and self._active(target) else None

    # -- 1. start (from /oauth/authorize) --------------------------------------------------------------

    def start(self, *, connector_id: str, resource: str, request: Any, now: int) -> BrowserResult:
        try:
            target = self._directory.for_connector(connector_id, resource)
        except Exception:  # noqa: BLE001
            target = None
        if not isinstance(target, ConnectTarget) or not self._active(target):
            return _page(403, "app_binding_disabled")
        rec = target.binding
        try:
            gen = self._sealed(rec.tenant_id, rec.binding_id)
        except Exception:  # noqa: BLE001
            gen = None
        connect_path = _rel_endpoint(target.endpoints, "chatgpt_connect")
        if gen is None or connect_path is None or _rel_endpoint(target.endpoints, "connect_result") is None:
            return _page(403, "app_binding_disabled")  # unsealed, or a bundle without the seat endpoints
        scope = tuple(sorted(str(s) for s in (request.scope or ())))
        oauth_request_sha256 = hashlib.sha256(canonicalize({
            "client_id": request.client_id, "redirect_uri": request.redirect_uri, "state": request.state,
            "code_challenge": request.code_challenge, "code_challenge_method": "S256",
            "scope": " ".join(scope), "resource": request.resource,
        })).hexdigest()
        authz_request_id, cookie = self._token(), self._token()
        txn = AuthzTxn(
            authz_request_sha256=_sha(authz_request_id), cookie_sha256=_sha(cookie),
            oauth_request_sha256=oauth_request_sha256, client_id=request.client_id,
            redirect_uri=request.redirect_uri, state=request.state, code_challenge=request.code_challenge,
            scope=scope, resource=request.resource, connector_id=target.connector_id, tenant_id=rec.tenant_id,
            organization_id=target.organization_id, installation_id=target.installation_id,
            binding_id=rec.binding_id, binding_generation=gen, status=PENDING, created_at=now,
            expires_at=now + self._ttl,
        )
        self._store.create(txn)
        try:  # the txn is durable: now its row (no row, no redirect: the txn stays inert)
            self._emit("app_connect_authorize_created", txn)
        except Exception:  # noqa: BLE001
            return _page(503, CODE_INTERNAL)
        return BrowserResult(
            303,
            location=f"{rec.application_origin}{connect_path}?req={authz_request_id}",
            set_cookie=f"{COOKIE_NAME}={cookie}; Max-Age={self._ttl}; Path=/; Secure; HttpOnly; SameSite=Lax",
        )

    # -- the shared CRM -> AIDOCS envelope (§2) ------------------------------------------------------------

    def _envelope(self, authorization: Any, body: Any, now: int, *, purpose: str,
                  schema: Callable[[Mapping[str, Any]], bool]) -> tuple[ConnectTarget, dict]:
        bad = _Refused(CODE_ASSERTION_INVALID, 401)
        if type(now) is not int or type(authorization) is not str or not authorization.startswith("Bearer "):
            raise bad
        token = authorization[len("Bearer "):]
        try:
            parsed = _strict_json(body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise bad from None
        if type(parsed) is not dict or not schema(parsed):
            raise bad
        target = self._target(parsed["binding_id"])
        if target is None:
            raise bad  # unknown, disabled or unpaired: one non-enumerating answer (R-10)
        rec = target.binding
        try:
            verified = jws.verify_compact(token, expected_typ=jws.TYP_CONTROL, keyset=rec.trust.binding_control_keyset)
            claims = verified.payload
            required = _CLAIMS_BASE | {"binding_generation"}
            if not required <= set(claims) <= required | {"nbf"}:
                raise bad
            jws.check_lifetime(claims, now=now, max_lifetime_s=CONTROL_LIFETIME_S)
        except jws.JwsError:
            raise bad from None
        if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now + jws.MAX_CLOCK_SKEW_S):
            raise bad
        expected = {
            "iss": rec.pack_id, "aud": rec.trust.aidocs_origin, "tenant_id": rec.tenant_id,
            "binding_id": rec.binding_id, "purpose": purpose,
        }
        for name, value in expected.items():
            if type(claims[name]) is not str or claims[name] != value:
                raise bad
        if type(claims["binding_generation"]) is not str or claims["binding_generation"] != parsed["binding_generation"]:
            raise bad
        jti = claims["jti"]
        if type(jti) is not str or not _JTI_RE.fullmatch(jti):
            raise bad
        try:
            actual = hashlib.sha256(canonicalize(parsed)).hexdigest()
        except (JcsError, TypeError):
            raise bad from None
        digest = claims["body_sha256"]
        if type(digest) is not str or not _HEX64.fullmatch(digest) or digest != actual:
            raise bad  # refused WITHOUT consuming the jti
        key = hashlib.sha256(canonicalize([claims["iss"], verified.header["kid"], jti])).hexdigest()
        keep = max(claims["exp"] + jws.MAX_CLOCK_SKEW_S, now + JTI_RETENTION_S)
        try:
            fresh = self._consume_jti(rec.tenant_id, key, keep, now) is True
        except Exception:  # noqa: BLE001 -- a replay-store fault fails closed
            raise _Refused(CODE_INTERNAL, 503) from None
        if not fresh:
            raise bad
        # -- authenticated from here on (step 12) --
        try:
            sealed = self._sealed(rec.tenant_id, rec.binding_id)
        except Exception:  # noqa: BLE001
            raise _Refused(CODE_INTERNAL, 503) from None
        if sealed is None or sealed != parsed["binding_generation"]:
            raise _Refused(CODE_GENERATION_MISMATCH, 409)
        return target, parsed

    # -- 2. chatgpt_connect -------------------------------------------------------------------------------

    def chatgpt_connect(self, authorization: Any, body: Any, *, now: int) -> ControlResult:
        try:
            target, p = self._envelope(authorization, body, now, purpose="chatgpt_connect", schema=_connect_body_ok)
            return self._assert(target, p, now)
        except _Refused as exc:
            return _control_error(exc.code, exc.status)

    def _assert(self, target: ConnectTarget, p: dict, now: int) -> ControlResult:
        rec = target.binding
        txn = self._store.by_request(_sha(p["authz_request_id"]))
        if (
            txn is None
            or txn.status not in (PENDING, ASSERTED)
            or now > txn.expires_at
            or (txn.tenant_id, txn.binding_id, txn.binding_generation)
            != (rec.tenant_id, rec.binding_id, p["binding_generation"])
        ):
            raise _Refused(CODE_REQUEST_INVALID, 400)
        if p["seat_assignment_id"] == "":
            raise _Refused(CODE_SEAT_REQUIRED, 409)
        try:
            owner_ok = self._authority.owner_matches(
                rec.tenant_id, rec.binding_id, p["owner_epoch"], p["assigned_by_owner_crm_user_id"]
            ) is True
        except Exception:  # noqa: BLE001
            raise _Refused(CODE_INTERNAL, 503) from None
        if not owner_ok:
            raise _Refused(CODE_OWNER_STALE, 409)
        body_sha = hashlib.sha256(canonicalize(p)).hexdigest()
        if txn.status == ASSERTED and (txn.asserted or {}).get("body_sha256") != body_sha:
            raise _Refused(CODE_CONFLICT, 409)
        ret = self._token()
        new = replace(
            txn, status=ASSERTED, asserted={**p, "body_sha256": body_sha}, ret_sha256=_sha(ret),
            ret_expires_at=now + RET_TTL_S,
        )
        if not self._store.cas(txn, new):
            raise _Refused(CODE_CONFLICT, 409)
        try:  # durable state first, then its row; no row -> no continue_url (the CRM retries the same body)
            self._emit("app_connect_asserted", new, link_state_id=p["link_state_id"] if _HEX64.fullmatch(
                p["link_state_id"]) else None, crm_seat_assignment_id=p["seat_assignment_id"],
                crm_seat_assignment_version=p["seat_version"], owner_epoch=p["owner_epoch"])
        except Exception:  # noqa: BLE001
            raise _Refused(CODE_INTERNAL, 503) from None
        return ControlResult(200, {"ok": True, "continue_url": f"{rec.trust.aidocs_origin}{RETURN_PATH}?ret={ret}"})

    # -- seat_quota (§4) ------------------------------------------------------------------------------------

    def quota(self, authorization: Any, body: Any, *, now: int) -> ControlResult:
        try:
            target, _p = self._envelope(authorization, body, now, purpose="seat_quota", schema=_quota_body_ok)
        except _Refused as exc:
            return _control_error(exc.code, exc.status)
        try:
            res = self._seats.quota(installation_id=target.installation_id)
            answer = _quota_answer(res.crm_answer() if hasattr(res, "crm_answer") else res)
        except Exception:  # noqa: BLE001 -- seat_quota_unknown / unavailable: the CRM keeps refusing seats
            answer = None
        if answer is None:
            return _control_error(CODE_SEAT_UNAVAILABLE, 503)
        return ControlResult(200, answer)

    # -- seat_revoke (WIRE-1134 §5; CAT wire §1(c)) -- fix 6, pulled into slice 1 --------------------------

    def revoke(self, authorization: Any, body: Any, *, now: int) -> ControlResult:
        """Free the CN seat for a CRM seat removal. ACK only after CN answers ``revoked``
        or ``no_active_binding`` AND the DECISION row is durable; idempotent per
        ``event_id`` (same body -> the stored ACK, no CN call; different body -> conflict).
        Existing token families die by B3 (CN's live binding read is now inactive)."""
        try:
            target, p = self._envelope(authorization, body, now, purpose="seat_revoke", schema=_revoke_body_ok)
        except _Refused as exc:
            return _control_error(exc.code, exc.status)
        rec = target.binding
        body_sha = hashlib.sha256(canonicalize(p)).hexdigest()
        try:
            prior = self._store.event_receipt(rec.tenant_id, p["event_id"])
        except Exception:  # noqa: BLE001
            return _control_error(CODE_INTERNAL, 503)
        if prior is not None:
            if prior[0] != body_sha:
                return _control_error(CODE_EVENT_CONFLICT, 409)
            return ControlResult(200, prior[1])
        try:
            res = self._seats.revoke(
                organization_id=target.organization_id, installation_id=target.installation_id,
                crm_user_id=p["crm_user_id"], seat_assignment_id=p["seat_assignment_id"],
                seat_version=p["seat_version"], event_id=p["event_id"], reason=p["reason"],
            )
        except Exception as exc:  # noqa: BLE001
            if is_typed_refusal(exc):
                code = exc.code if exc.code == CODE_IDENTITY_CONFLICT else CODE_REVOKE_REFUSED
                return _control_error(code, 409)
            return _control_error(CODE_SEAT_UNAVAILABLE, 503)  # the CRM outbox retries
        state = getattr(res, "state", None)
        if state not in ("revoked", "no_active_binding"):
            return _control_error(CODE_SEAT_UNAVAILABLE, 503)
        seat_binding_id = getattr(res, "seat_binding_id", None)
        md = {
            "event_id": p["event_id"], "tenant_id": rec.tenant_id, "organization_id": target.organization_id,
            "installation_id": target.installation_id, "binding_id": rec.binding_id,
            "binding_generation": p["binding_generation"], "connector_id": target.connector_id,
            "crm_seat_assignment_id": p["seat_assignment_id"], "crm_seat_assignment_version": p["seat_version"],
            "revoke_reason": p["reason"], "seat_revoke_state": state,
            "seat_binding_id": seat_binding_id if _text(seat_binding_id) else None,
        }
        md = {k: v for k, v in md.items() if v is not None and audit_metadata({k: v})}
        ack = {"ok": True, "acked": True, "event_id": p["event_id"]}
        try:  # truth before green: CN confirmed -> durable row -> receipt -> ACK
            self._audit(ConnectControlEvent("app_seat_revoked", rec.tenant_id, CONNECT_ACTOR, rec.binding_id, md))
            self._store.put_event_receipt(rec.tenant_id, p["event_id"], body_sha, ack)
        except Exception:  # noqa: BLE001 -- no row/receipt, no ACK: the retry re-asks CN (idempotent)
            return _control_error(CODE_INTERNAL, 503)
        return ControlResult(200, ack)

    # -- owner_epoch_update (CAT wire §1(d)) -- slice 2 ------------------------------------------------------

    def owner_epoch(self, authorization: Any, body: Any, *, now: int) -> ControlResult:
        """Move the binding's owner authority for a CRM owner change. The shared envelope
        (binding-control key, sealed generation, durable jti), then B6
        ``apply_owner_epoch`` in ONE authority transaction (receipt first; CAS of the
        keyed owner digest + epoch; the ``app_owner_epoch_updated`` row; the receipt).
        ACK only after that commits; any fault is a retryable 503 with no ACK. The CRM
        outbox sends this head-of-line per org, so a definitive refusal is final.
        No seat is admitted or revoked here."""
        try:
            target, p = self._envelope(authorization, body, now, purpose="owner_epoch_update",
                                       schema=_owner_epoch_body_ok)
        except _Refused as exc:
            return _control_error(exc.code, exc.status)
        rec = target.binding
        body_sha = hashlib.sha256(canonicalize(p)).hexdigest()
        md = {
            "event_id": p["event_id"], "tenant_id": rec.tenant_id, "organization_id": target.organization_id,
            "installation_id": target.installation_id, "binding_id": rec.binding_id,
            "binding_generation": p["binding_generation"], "connector_id": target.connector_id,
            "owner_epoch": p["new_owner_epoch"],
        }
        md = {k: v for k, v in md.items() if audit_metadata({k: v})}  # never the raw CRM user id (R-11)

        def decision() -> None:
            self._audit(ConnectControlEvent("app_owner_epoch_updated", rec.tenant_id, CONNECT_ACTOR,
                                            rec.binding_id, md))

        try:
            outcome, ack = self._authority.owner_epoch_update(
                rec.tenant_id, rec.binding_id, update=p, body_sha256=body_sha, now=now, decision=decision,
            )
        except Exception:  # noqa: BLE001 -- nothing committed: the outbox retries the same body
            return _control_error(CODE_INTERNAL, 503)
        if outcome in ("applied", "acked") and isinstance(ack, Mapping) and ack.get("event_id") == p["event_id"]:
            return ControlResult(200, dict(ack))
        if outcome == "event_conflict":
            return _control_error(CODE_EVENT_CONFLICT, 409)
        if outcome == "stale":
            return _control_error(CODE_OWNER_STALE, 409)
        if outcome in ("unsealed", "generation_mismatch"):  # nothing written (incl. no receipt)
            return _control_error(CODE_GENERATION_MISMATCH, 409)
        return _control_error(CODE_INTERNAL, 503)

    # -- 3. the return leg ------------------------------------------------------------------------------------

    def connect_return(self, cookie: Any, ret: Any, *, now: int) -> BrowserResult:
        txn = self._store.by_cookie(_sha(cookie)) if _text(cookie, max_len=512) else None
        if txn is None or txn.status != ASSERTED or now > txn.expires_at or not txn.ret_sha256:
            return _page(400, CODE_REQUEST_INVALID)
        valid = _text(ret, max_len=512) and _sha(ret) == txn.ret_sha256 and now <= txn.ret_expires_at
        spent = replace(txn, ret_sha256=None, ret_expires_at=0, returned=txn.returned or bool(valid))
        if not self._store.cas(txn, spent) or not valid:  # spent by this attempt, whatever the outcome
            return _page(400, CODE_REQUEST_INVALID)
        return BrowserResult(303, location=COMPLETE_PATH)

    # -- 4. completion (the saga) ----------------------------------------------------------------------------

    def connect_complete(self, cookie: Any, *, now: int) -> BrowserResult:
        txn = self._store.by_cookie(_sha(cookie)) if _text(cookie, max_len=512) else None
        if txn is None or txn.status in TERMINAL:
            return _page(400, CODE_REQUEST_INVALID)
        if txn.status == RELEASING:  # exclusive to its holder: never abandoned, expired or refused here
            return _page(409, CODE_CONFLICT)
        if now > txn.expires_at:
            if txn.status in SEATED:
                self._abandon(txn, "expired", now)
            else:
                self._store.cas(txn, replace(txn, status=EXPIRED))
            return _page(400, "app_connect_expired")
        if txn.status == PENDING or (txn.status == ASSERTED and not txn.returned):
            return _page(400, CODE_REQUEST_INVALID)
        target = self._target(txn.binding_id)
        try:
            gen = self._sealed(txn.tenant_id, txn.binding_id) if target is not None else None
        except Exception:  # noqa: BLE001
            return _page(503, CODE_INTERNAL)
        if (
            target is None
            or target.binding.tenant_id != txn.tenant_id
            or target.installation_id != txn.installation_id
            or target.organization_id != txn.organization_id
            or gen != txn.binding_generation
        ):
            return self._refuse(txn, target, "app_binding_disabled", now)
        a = txn.asserted or {}
        try:
            owner_ok = self._authority.owner_matches(
                txn.tenant_id, txn.binding_id, a.get("owner_epoch"), a.get("assigned_by_owner_crm_user_id")
            ) is True
        except Exception:  # noqa: BLE001
            return _page(503, CODE_INTERNAL)
        if not owner_ok:
            return self._refuse(txn, target, CODE_SEAT_REQUIRED, now)
        if txn.status == ASSERTED:
            txn, out = self._admit(txn, target, now)
            if out is not None:
                return out
        if txn.status == ADMITTED:
            txn, out = self._finalize(txn, target, now)
            if out is not None:
                return out
        if txn.status in (FINALIZED, ISSUING):
            return self._release(txn, target)
        return _page(400, CODE_REQUEST_INVALID)

    def _result_page(self, target: Optional[ConnectTarget], outcome: str) -> BrowserResult:
        path = _rel_endpoint(target.endpoints, "connect_result") if target is not None else None
        if path is None or outcome not in CRM_OUTCOMES:
            return _page(403, outcome)  # R-9 fallback: a generic AIDOCS page
        origin = target.binding.application_origin
        return BrowserResult(303, location=f"{origin}{path}?{urlencode({'outcome': outcome})}",
                             set_cookie=_clear_cookie(), reason=outcome)

    def _refuse(self, txn: AuthzTxn, target: Optional[ConnectTarget], outcome: str, now: int) -> BrowserResult:
        if txn.status in SEATED:
            self._abandon(txn, "finalize_refused", now, target=target)
        elif self._store.cas(txn, replace(txn, status=REFUSED)):
            self._emit_best_effort("app_connect_refused", txn, connect_outcome=outcome)
        return self._result_page(target, outcome)

    def _admit(self, txn: AuthzTxn, target: ConnectTarget, now: int):
        a = txn.asserted or {}
        try:
            res = self._seats.admit(
                organization_id=txn.organization_id, installation_id=txn.installation_id,
                binding_id=txn.binding_id, binding_generation=txn.binding_generation,
                ceremony_id=txn.authz_request_sha256, crm_user_id=a["crm_user_id"],
                crm_seat_assignment_id=a["seat_assignment_id"], seat_version=a["seat_version"],
                assigned_by_crm_user_id=a["assigned_by_owner_crm_user_id"],
            )
        except Exception as exc:  # noqa: BLE001
            if is_typed_refusal(exc):
                code = exc.code if exc.code in CRM_OUTCOMES else CODE_SEAT_REQUIRED
                if self._store.cas(txn, replace(txn, status=REFUSED)):
                    self._emit_best_effort("app_connect_refused", txn, connect_outcome=code)
                return txn, self._result_page(target, code)
            return txn, _page(503, CODE_SEAT_UNAVAILABLE)  # no code; the cookie may retry /complete
        seat_binding_id = getattr(res, "seat_binding_id", None)
        sub = getattr(res, "connector_principal_id", None)
        if not (_text(seat_binding_id) and _text(sub)):
            return txn, _page(503, CODE_SEAT_UNAVAILABLE)
        new = replace(txn, status=ADMITTED, seat_binding_id=seat_binding_id, connector_sub=sub)
        try:  # CN's admission is durable at CN: its row BEFORE we move on (a retry re-admits idempotently)
            self._emit("app_seat_admitted", new, **self._seat_facts(new))
        except Exception:  # noqa: BLE001
            return txn, _page(503, CODE_INTERNAL)
        if not self._store.cas(txn, new):
            return txn, _page(409, CODE_CONFLICT)
        return new, None

    def _finalize(self, txn: AuthzTxn, target: ConnectTarget, now: int):
        a = txn.asserted or {}
        body = txn.finalize_body
        if body is None:  # built ONCE; every retry sends the exact same receipt (R-12)
            body = {
                "authz_request_id": a["authz_request_id"], "oauth_request_sha256": txn.oauth_request_sha256,
                "link_state_id": a["link_state_id"], "binding_id": txn.binding_id,
                "binding_generation": txn.binding_generation, "crm_user_id": a["crm_user_id"],
                "connector_sub": txn.connector_sub, "seat_assignment_id": a["seat_assignment_id"],
                "seat_binding_id": txn.seat_binding_id, "completed_at": _rfc3339(now),
            }
            stamped = replace(txn, finalize_body=body)
            if not self._store.cas(txn, stamped):
                return txn, _page(409, CODE_CONFLICT)
            txn = stamped
        verdict = "unavailable"
        for _ in range(FINALIZE_ATTEMPTS):
            try:
                verdict = self._crm.finalize(target, generation=txn.binding_generation, sub=txn.connector_sub,
                                             body=dict(body))
            except Exception:  # noqa: BLE001
                verdict = "unavailable"
            if verdict in ("acked", "refused"):
                break
        if verdict == "refused":
            self._abandon(txn, "finalize_refused", now, target=target)
            return txn, self._result_page(target, "app_identity_unlinked")
        if verdict != "acked":
            return txn, _page(503, "app_link_finalize_unavailable")  # no ACK, no code
        new = replace(txn, status=FINALIZED)
        try:  # the CRM's link is durable (ACK): its row, then our state (a retry re-sends the same receipt)
            self._emit("app_link_finalized", new, link_state_id=a.get("link_state_id"), **self._seat_facts(new))
        except Exception:  # noqa: BLE001
            return txn, _page(503, CODE_INTERNAL)
        if not self._store.cas(txn, new):
            return txn, _page(409, CODE_CONFLICT)
        return new, None

    def _void(self, txn: AuthzTxn, attempt: Optional[int] = None) -> None:
        try:
            self._void_codes(txn, attempt)
        except Exception:  # noqa: BLE001 -- the code was never delivered; a re-issue voids lower attempts too
            pass

    def _release(self, txn: AuthzTxn, target: ConnectTarget) -> BrowserResult:
        """Issue -> CN confirm -> rows -> terminal; never wedged, at most one live code.

        Every attempt bumps ``issue_attempt`` by CAS (FINALIZED|ISSUING(n) -> ISSUING(n+1)),
        so concurrent /complete calls serialize; the code is keyed on (ceremony, attempt)
        and its insert voids lower attempts. Only the CAS winner of ISSUING(n) -> RELEASING(n)
        writes the confirmed/released rows; a failed row moves it back to ISSUING (retryable).
        The code is delivered only after the terminal CAS."""
        a = txn.asserted or {}
        try:
            family = SeatFamily(
                connector_sub=txn.connector_sub, seat_binding_id=txn.seat_binding_id,
                binding_generation=txn.binding_generation, installation_id=txn.installation_id,
                binding_id=txn.binding_id, tenant_id=txn.tenant_id, organization_id=txn.organization_id,
                crm_seat_assignment_id=a.get("seat_assignment_id"),
                crm_seat_assignment_version=a.get("seat_version"),
            )
        except ValueError:
            return _page(500, CODE_INTERNAL)
        attempt = txn.issue_attempt + 1
        issuing = replace(txn, status=ISSUING, issue_attempt=attempt)
        if not self._store.cas(txn, issuing):
            return _page(409, CODE_CONFLICT)
        try:
            code = self._issue_code(issuing, family)
        except Exception:  # noqa: BLE001 -- txn stays ISSUING: a retry issues attempt n+1 and voids this one
            self._void(issuing, attempt)
            return _page(503, CODE_INTERNAL)
        try:  # CN confirm is idempotent per (ceremonyId, seatBindingId) (WIRE §8.1a): safe on every retry
            self._seats.confirm(organization_id=txn.organization_id, installation_id=txn.installation_id,
                                ceremony_id=txn.authz_request_sha256, seat_binding_id=txn.seat_binding_id)
        except Exception:  # noqa: BLE001 -- a provisional seat never serves (B3): no code yet, retry /complete
            self._void(issuing, attempt)
            return _page(503, CODE_SEAT_UNAVAILABLE)
        releasing = replace(issuing, status=RELEASING)
        if not self._store.cas(issuing, releasing):  # a newer attempt, or an abandon, won: ours never leaves
            self._void(issuing, attempt)
            return _page(409, CODE_CONFLICT)
        # RELEASING is exclusive (no other request mutates it), so the terminal CAS is ours.
        released = replace(releasing, status=CODE_RELEASED)
        if not self._store.cas(releasing, released):  # store fault only: nothing written, nothing delivered
            self._void(issuing, attempt)
            return _page(503, CODE_INTERNAL)
        # Completed rows only AFTER the terminal CAS won. The code has not left the process yet:
        # a lost row reverts the holder's own terminal state (no other request mutates it).
        try:
            self._emit("app_seat_confirmed", released, **self._seat_facts(released))
            self._emit("app_connect_code_released", released, issue_attempt=attempt, **self._seat_facts(released))
        except Exception:  # noqa: BLE001 -- no row, no code: back to ISSUING (retryable)
            self._void(issuing, attempt)
            self._store.cas(released, issuing)
            return _page(503, CODE_INTERNAL)
        sep = "&" if "?" in txn.redirect_uri else "?"
        location = f"{txn.redirect_uri}{sep}{urlencode({'code': code, 'state': txn.state})}"
        return BrowserResult(302, location=location, set_cookie=_clear_cookie())

    def _abandon(self, txn: AuthzTxn, reason: str, now: int, *, target: Optional[ConnectTarget] = None) -> None:
        """Abandon ONLY this ceremony's provisional seat, void its undelivered codes, then
        the link_abandon compensation."""
        if txn.seat_binding_id:
            try:
                self._seats.abandon(organization_id=txn.organization_id, installation_id=txn.installation_id,
                                    ceremony_id=txn.authz_request_sha256, seat_binding_id=txn.seat_binding_id)
            except Exception:  # noqa: BLE001 -- TODO(#1134 §7.5 step 4): periodic reconciliation backs this up
                pass
        self._void(txn)  # every attempt: no code of an abandoned ceremony stays exchangeable
        done = replace(txn, status=ABANDONED if reason == "finalize_refused" else EXPIRED)
        if self._store.cas(txn, done):
            self._emit_best_effort("app_connect_abandoned", done, connect_outcome=reason, **self._seat_facts(done))
        if target is None:
            target = self._target(txn.binding_id)
        a = txn.asserted or {}
        if target is not None and txn.seat_binding_id and txn.connector_sub:
            body = {
                "authz_request_id": a.get("authz_request_id"), "oauth_request_sha256": txn.oauth_request_sha256,
                "link_state_id": a.get("link_state_id"), "binding_id": txn.binding_id,
                "binding_generation": txn.binding_generation, "crm_user_id": a.get("crm_user_id"),
                "seat_assignment_id": a.get("seat_assignment_id"), "seat_binding_id": txn.seat_binding_id,
                "reason": reason, "abandoned_at": _rfc3339(now),
            }
            try:
                # Slice-1 debt (DEBT-1134-phaseB-slice1): inline, not a durable outbox.
                verdict = self._crm.link_abandon(target, generation=txn.binding_generation, sub=txn.connector_sub,
                                                 body=body)
            except Exception:  # noqa: BLE001
                verdict = "unavailable"
            if verdict == "acked":
                self._emit_best_effort("app_link_abandon_sent", done, connect_outcome=reason,
                                       link_state_id=a.get("link_state_id"), **self._seat_facts(done))


def _clear_cookie() -> str:
    return f"{COOKIE_NAME}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"


# -- production CRM link client -------------------------------------------------------------------------


class RuntimeCrmLinkClient:
    """``link_finalize`` / ``link_abandon`` over the org runtime's pinned-origin
    control transport (the SAME egress / signer as entitlements and receipt
    lookup): ``rt.transport.control(...)`` signs the wire §5 claims with the
    org's CURRENT request key and posts the JCS body to
    ``<application_origin><internal_endpoints.link_*>``. A 3xx is never followed."""

    def __init__(self, runtime_for_tenant: Callable[[str], Any]) -> None:
        self._runtime_for = runtime_for_tenant

    def _post(self, target: ConnectTarget, purpose: str, generation: str, sub: str, body: Mapping[str, Any]) -> str:
        endpoint = _rel_endpoint(target.endpoints, purpose)
        if endpoint is None:
            return "unavailable"
        try:
            rt = self._runtime_for(target.binding.tenant_id)
            claims = link_control_claims(
                target.binding, purpose=purpose, generation=generation, sub=sub, body=body,
                now=int(rt.transport.now()), jti="jti_" + secrets.token_urlsafe(24),
                call_id="call_" + secrets.token_urlsafe(16),
            )
            status, parsed = rt.transport.control(
                tenant_id=target.binding.tenant_id, application_origin=target.binding.application_origin,
                endpoint=endpoint, body=canonicalize(dict(body)), claims=claims,
            )
        except Exception:  # noqa: BLE001 -- timeout / 5xx / transport refusal: retryable
            return "unavailable"
        return classify_crm_ack(status, parsed, link_state_id=str(body.get("link_state_id") or ""))

    def finalize(self, target: ConnectTarget, *, generation: str, sub: str, body: Mapping[str, Any]) -> str:
        return self._post(target, "link_finalize", generation, sub, body)

    def link_abandon(self, target: ConnectTarget, *, generation: str, sub: str, body: Mapping[str, Any]) -> str:
        return self._post(target, "link_abandon", generation, sub, body)
