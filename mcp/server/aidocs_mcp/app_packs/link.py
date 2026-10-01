"""The HUMAN-ONLY link flow, AIDOCS side -- RFC 0003 v4.6.1 §8.1-§8.3; wire rev 8 §8 (S8).

Transport-free handlers. The HTTP routes are wired elsewhere; each handler
takes an already-authenticated caller and the raw request parts and returns a
typed result (:class:`LinkPage` for the browser page, :class:`LinkResponse`
for the control-plane API).

1. ``GET /apps/link/authorize?binding_id&state&redirect_uri`` ->
   :meth:`LinkService.authorize_page`. Only the logged-in HUMAN's AIDOCS
   browser session (credential class :data:`LINK_BROWSER_CREDENTIAL`) is
   accepted; a connector / OAuth MCP credential is refused by construction
   (§8.1). The binding must be ACTIVE and paired. ``redirect_uri`` is checked
   in the two wire §8.2 steps: its canonical origin (§1.6) must exactly equal
   the binding's ``application_origin``, then the whole URI must equal the
   transcript's ``link_return_uri`` byte for byte. Anything else gets an error
   page and never a redirect. The seat is ``(binding.tenant_id, sub)`` where
   ``sub`` is the HUMAN's seat in that org (:class:`SeatDirectory`); nothing
   about identity is read from the query (§8.2). The page shows the exact org,
   binding / pack and seat and carries a single-use confirm ticket bound to the
   human, the binding, the seat, the state and the return URI.
2. ``POST /apps/link/authorize`` (form ``ticket``, ``decision``) ->
   :meth:`LinkService.authorize_confirm`. The ticket is spent; every fact is
   re-checked; a one-time code (>= 32 random bytes, <= 120 s, ruling O-6) is
   issued, bound server-side to ``(tenant_id, binding_id, sub)``, under a
   WRITE-AHEAD ``app_link_code_issued`` DECISION row; then ``302`` to
   ``<link_return_uri>?code=<code>&state=<state>`` (wire §8.3).
3. ``POST /v1/control/app-links/redeem`` -> :meth:`LinkService.redeem`,
   server-to-server from the application. The ``Authorization: Bearer`` JWS
   (``typ`` ``aidocs-control+jwt``) is verified ONLY against the binding's
   pinned binding-control key (an entitlement key or the AIDOCS org key is
   refused: wire §1.3, §8.4); claims exactly as wire §8.4; ``body_sha256``
   checked BEFORE the ``(iss, kid, jti)`` consume, so a mismatch does not
   spend the jti (RFC §12.2); then the code is spent on the first attempt that
   reaches it, success or failure. The link ``(tenant_id, binding_id, sub)`` is
   recorded under a WRITE-AHEAD ``app_identity_linked`` DECISION row (§16.1).

There is no other link writer: no admin act, no dashboard API, no tool (§8.1,
§8.4). Refusals use the wire §1.5 AIDOCS-only codes ``app_control_assertion_invalid``
/ ``app_link_code_invalid``; messages are fixed and non-disclosing; refusal
rows carry no code, state, jti or token.

[FLAGGED] AIDOCS does not learn ``app_principal_id`` at redeem (the wire §8.4
body and response carry none; the application records the principal side,
RFC §8.2 step 6). The AIDOCS link row is ``(tenant_id, binding_id, sub)``.
[FLAGGED] §8.2 "rate-limited" is not built here: issuance and redemption rate
limits must land with the route wiring. Code, ticket, replay and link state
are in-memory behind Protocols (a production HOLD until durable).
"""
from __future__ import annotations

import contextlib
import hashlib
import html
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import quote

from . import jws
from ..execution_event_retention import EVENT_KIND_RETENTION, RetentionClass
from .binding_store import (
    CREDENTIAL_ADMIN_DASHBOARD,
    RETENTION_DECISION,
    BindingRecord,
    BindingRefused,
    BindingState,
    ControlPlaneActor,
    ControlPlaneAuditSink,
    refusal_code_metadata,
)
from .boundary_audit import KIND_STATUS, audit_metadata, kind_status_admissible
from .jcs import JcsError, canonicalize
from .origin import PRODUCTION_ORIGIN_POLICY, OriginPolicy, OriginRefused, parse_origin, parse_url, same_origin

__all__ = [
    "CODE_CONTROL_ASSERTION_INVALID",
    "CODE_LINK_CODE_INVALID",
    "LINKED_KIND",
    "LINK_BROWSER_CREDENTIAL",
    "LINK_CODE_KIND",
    "LINK_CODE_LIFETIME_S",
    "LINK_CONTROL_KINDS",
    "MAX_CONTROL_LIFETIME_S",
    "PURPOSE_LINK_REDEEM",
    "InMemoryJtiReplayStore",
    "InMemoryLinkStore",
    "InMemoryOneShotStore",
    "JtiReplayStore",
    "LinkBindingReader",
    "LinkControlEvent",
    "LinkPage",
    "LinkRecord",
    "LinkResponse",
    "LinkService",
    "LinkStore",
    "OneShotStore",
    "SeatDirectory",
    "redeem_body_sha256",
]

CODE_LINK_CODE_INVALID = "app_link_code_invalid"
CODE_CONTROL_ASSERTION_INVALID = "app_control_assertion_invalid"
PURPOSE_LINK_REDEEM = "link_redeem"
#: Ruling O-6: the one-time code is valid for at most 120 s.
LINK_CODE_LIFETIME_S = 120
#: Wire §1.4 / §8.4: ``exp - iat`` <= 60 s for a control assertion.
MAX_CONTROL_LIFETIME_S = 60
#: Wire §1.4: a consumed ``(iss, kid, jti)`` is kept >= 75 s.
JTI_RETENTION_S = 75
#: The confirm ticket (the page -> POST correlation; also the CSRF token).
TICKET_LIFETIME_S = 600
#: The AIDOCS dashboard's HUMAN browser session credential class. The route sets
#: it from the dashboard session cookie only -- never from a Bearer / MCP token.
LINK_BROWSER_CREDENTIAL = CREDENTIAL_ADMIN_DASHBOARD
CODE_BYTES = 32
MIN_STATE_BYTES = 32
MAX_STATE_CHARS = 256
MAX_CODE_CHARS = 512

LINK_CODE_KIND = "app_link_code_issued"
LINKED_KIND = "app_identity_linked"
REFUSED_KIND = "app_control_plane_refused"
LINK_CONTROL_KINDS = frozenset({LINK_CODE_KIND, LINKED_KIND, REFUSED_KIND})

_QUERY_MEMBERS = frozenset({"binding_id", "state", "redirect_uri"})
_FORM_MEMBERS = frozenset({"ticket", "decision"})
_BODY_MEMBERS = frozenset({"binding_id", "code", "redirect_uri"})
_CLAIMS_REQUIRED = frozenset(
    {"iss", "aud", "iat", "exp", "jti", "tenant_id", "binding_id", "purpose", "body_sha256"}
)
_CLAIMS_OPTIONAL = frozenset({"nbf"})
_JTI_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MESSAGES = {
    CODE_CONTROL_ASSERTION_INVALID: "The control request was refused.",
    CODE_LINK_CODE_INVALID: "The link code was refused.",
}
_STATUS = {CODE_CONTROL_ASSERTION_INVALID: 401, CODE_LINK_CODE_INVALID: 400}
_PAGE_HEADERS = MappingProxyType(
    {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
)
_CSP_ORIGIN_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?")

for _code in (CODE_LINK_CODE_INVALID, CODE_CONTROL_ASSERTION_INVALID):  # vocabulary self-check
    refusal_code_metadata(_code)


# -- seams ------------------------------------------------------------------------------


class SeatDirectory(Protocol):
    """The HUMAN's seat in an org: ``sub`` of ``(tenant_id, sub)``, or None.

    Sourced server-side from the AIDOCS seat directory (CodeNexus DB, build plan
    R8), keyed by the authenticated browser user. Never read from a request."""

    def seat_sub(self, user_id: str, tenant_id: str) -> Optional[str]: ...


class LinkBindingReader(Protocol):
    """The one S7 read the link flow needs (``BindingStore.get``)."""

    def get(self, binding_id: str) -> BindingRecord: ...


class OneShotStore(Protocol):
    """Single-use secrets keyed by their SHA-256 (codes, confirm tickets).
    ``take`` is atomic: it returns the value at most once, ever."""

    def put(self, key: str, value: Any, *, expires_at: int) -> None: ...

    def take(self, key: str, *, now: int) -> Any: ...


class JtiReplayStore(Protocol):
    """Atomic ``(iss, kid, jti)`` consume (wire §1.4). True only the first time."""

    def consume(self, key: tuple[str, str, str], *, keep_until: int, now: int) -> bool: ...


@dataclass(frozen=True)
class LinkRecord:
    """AIDOCS's side of RFC §8.3 ``(tenant_id, binding_id, sub) <-> app_principal_id``.
    The principal is the application's to record (§8.2 step 6)."""

    tenant_id: str
    binding_id: str
    sub: str
    linked_at: int


class LinkStore(Protocol):
    def record_link(self, link: LinkRecord) -> None: ...

    def get(self, tenant_id: str, binding_id: str, sub: str) -> Optional[LinkRecord]: ...


class InMemoryOneShotStore:
    """Thread-safe in-process one-shot store (production HOLD until durable)."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[Any, int]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: Any, *, expires_at: int) -> None:
        with self._lock:
            if key in self._items:
                raise ValueError("one-shot key collision")
            self._items[key] = (value, expires_at)

    def take(self, key: str, *, now: int) -> Any:
        with self._lock:
            for k in [k for k, (_, exp) in self._items.items() if exp < now - JTI_RETENTION_S]:
                del self._items[k]
            item = self._items.pop(key, None)
        return None if item is None else item[0]


class InMemoryJtiReplayStore:
    def __init__(self) -> None:
        self._seen: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()

    def consume(self, key: tuple[str, str, str], *, keep_until: int, now: int) -> bool:
        with self._lock:
            for k in [k for k, until in self._seen.items() if until < now]:
                del self._seen[k]
            if key in self._seen:
                return False
            self._seen[key] = keep_until
            return True


class InMemoryLinkStore:
    """Thread-safe in-process link rows (production HOLD until durable)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], LinkRecord] = {}
        self._lock = threading.Lock()

    def record_link(self, link: LinkRecord) -> None:
        if type(link) is not LinkRecord:
            raise TypeError("record_link takes a LinkRecord")
        with self._lock:
            self._rows[(link.tenant_id, link.binding_id, link.sub)] = link

    def get(self, tenant_id: str, binding_id: str, sub: str) -> Optional[LinkRecord]:
        with self._lock:
            return self._rows.get((tenant_id, binding_id, sub))

    def records(self) -> tuple[LinkRecord, ...]:
        with self._lock:
            return tuple(self._rows.values())


# -- audit -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkControlEvent:
    """A §16.1 DECISION-class link control-plane event. Same contract as the S7
    ``ControlPlaneEvent`` (same ``ControlPlaneAuditSink.emit`` seam): registered
    kind, ``status`` derived from the closed matrix, allowlisted typed metadata
    only -- anything else raises."""

    kind: str
    tenant_id: str
    actor_user_id: str
    binding_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retention: str = RETENTION_DECISION
    status: str = field(init=False)

    def __post_init__(self) -> None:
        if self.retention != RETENTION_DECISION:
            raise ValueError("link control-plane events are DECISION-class")
        if self.kind not in LINK_CONTROL_KINDS or self.kind not in KIND_STATUS:
            raise ValueError(f"unregistered link audit kind {self.kind!r}")
        if EVENT_KIND_RETENTION.get(self.kind) is not RetentionClass.DECISION:
            raise ValueError(f"{self.kind!r} is not DECISION in the retention registry")
        if "status" in self.metadata:
            raise ValueError("status is derived from the kind, never supplied")
        status = KIND_STATUS[self.kind]
        md = {**dict(self.metadata), "status": status}
        if audit_metadata(md) != md:
            raise ValueError(f"metadata outside the audit allowlist or shape: {sorted(md)}")
        if not kind_status_admissible(self.kind, status):  # pragma: no cover -- matrix self-check
            raise ValueError("kind/status pair is off the closed matrix")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", MappingProxyType(md))


# -- results ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkPage:
    """A browser response: ``status``, headers (``Location`` only on 302), HTML.
    ``ticket`` is set only on the confirm page (it is also inside the HTML form)."""

    status: int
    html: str
    headers: Mapping[str, str]
    ticket: Optional[str] = None


@dataclass(frozen=True)
class LinkResponse:
    """A control-plane API response: HTTP status and the JSON body (wire §1.5, §8.4)."""

    status: int
    body: Mapping[str, Any]


@dataclass(frozen=True)
class _Ticket:
    user_id: str
    tenant_id: str
    binding_id: str
    sub: str
    state: str
    redirect_uri: str
    transcript_sha256: str
    expires_at: int


@dataclass(frozen=True)
class _Grant:
    user_id: str
    tenant_id: str
    binding_id: str
    sub: str
    redirect_uri: str
    transcript_sha256: str
    expires_at: int


class _Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code + (f": {detail}" if detail else ""))
        self.code = code


# -- pure helpers --------------------------------------------------------------------------


def redeem_body_sha256(body: Mapping[str, Any]) -> str:
    """Wire §8.4 ``body_sha256``: lowercase hex SHA-256 of the JCS bytes of the body."""
    return hashlib.sha256(canonicalize(dict(body))).hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _single(value: Any) -> Optional[str]:
    """A query/form value: a str, or a one-element list of str (parse_qs shape)."""
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            return None
        value = value[0]
    return value if type(value) is str and value else None


def _random_token(value: str, *, min_bytes: int, max_chars: int) -> bool:
    if len(value) > max_chars:
        return False
    try:
        return len(jws.b64url_decode(value)) >= min_bytes
    except jws.JwsError:
        return False


def _refuse_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate member name")
        out[key] = value
    return out


def _refuse_constant(_name: str) -> Any:
    raise ValueError("non-finite number literal")


def _page(status: int, title: str, body_html: str, *, ticket: Optional[str] = None,
          location: Optional[str] = None, form_target_origin: Optional[str] = None) -> LinkPage:
    headers = dict(_PAGE_HEADERS)
    if form_target_origin is not None:
        # form-action also governs the redirect a submission leads to: the confirm
        # POST 302s to link_return_uri, so the pinned application origin must be
        # named or the browser refuses that hop. A value that is not a bare
        # https origin is never spliced into the header.
        if not _CSP_ORIGIN_RE.fullmatch(form_target_origin):
            raise ValueError("application origin is not a CSP-safe https origin")
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "form-action 'self'", f"form-action 'self' {form_target_origin}"
        )
    if location is not None:
        headers["Location"] = location
    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title></head><body>"
        f"<main><h1>{html.escape(title)}</h1>{body_html}</main></body></html>"
    )
    return LinkPage(status=status, html=doc, headers=MappingProxyType(headers), ticket=ticket)


def _error_page(status: int) -> LinkPage:
    # Fixed and non-disclosing (RFC §4.1): never says which check failed.
    return _page(status, "Link not possible",
                 "<p>This link request cannot be completed. Nothing was linked. "
                 "Start again from the application.</p>")


# -- the service ---------------------------------------------------------------------------


class LinkService:
    """The §8.2 human link flow (see module doc). No other link writer exists."""

    def __init__(
        self,
        bindings: LinkBindingReader,
        seats: SeatDirectory,
        links: LinkStore,
        audit_sink: ControlPlaneAuditSink,
        *,
        codes: Optional[OneShotStore] = None,
        tickets: Optional[OneShotStore] = None,
        replay: Optional[JtiReplayStore] = None,
        origin_policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY,
        code_lifetime_s: int = LINK_CODE_LIFETIME_S,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        transaction: Optional[Callable[[str], Any]] = None,
    ) -> None:
        if type(code_lifetime_s) is not int or not 0 < code_lifetime_s <= LINK_CODE_LIFETIME_S:
            raise ValueError("the link code lifetime must be 1..120 s (ruling O-6)")
        self._bindings = bindings
        self._seats = seats
        self._links = links
        self._sink = audit_sink
        self._codes = codes if codes is not None else InMemoryOneShotStore()
        self._tickets = tickets if tickets is not None else InMemoryOneShotStore()
        self._replay = replay if replay is not None else InMemoryJtiReplayStore()
        self._origin_policy = origin_policy
        self._code_lifetime = code_lifetime_s
        self._random = random_bytes
        # r0b2 B1: ``transaction(tenant_id)`` opens the ONE authority-DB transaction
        # that the linked DECISION and the link row both join, so they commit or
        # roll back together. None = no shared transaction (in-memory stores).
        self._transaction = transaction

    # -- audit ---------------------------------------------------------------------

    @staticmethod
    def _facts(rec: Optional[BindingRecord], sub: Optional[str] = None) -> dict:
        if rec is None:
            return {}
        md = {
            "tenant_id": rec.tenant_id,
            "binding_id": rec.binding_id,
            "toolspace_id": rec.toolspace_id,
            "pack_id": rec.pack_id,
            "contract_digest": rec.contract_digest,
        }
        if sub is not None:
            md["sub"] = sub
        return audit_metadata(md)  # a sub of an unauditable shape is dropped, never coerced

    def _emit(self, kind: str, actor_id: str, rec: BindingRecord, sub: str) -> None:
        """Write-ahead: raises if the DECISION row cannot be written."""
        self._sink.emit(LinkControlEvent(kind, rec.tenant_id, actor_id, rec.binding_id, self._facts(rec, sub)))

    def _audit_refusal(self, actor_id: str, rec: Optional[BindingRecord], code: str) -> None:
        event = LinkControlEvent(
            REFUSED_KIND,
            rec.tenant_id if rec is not None else "<unknown>",
            actor_id,
            rec.binding_id if rec is not None else None,
            {**self._facts(rec), **refusal_code_metadata(code)},
        )
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 -- the refusal stands even if its audit write fails
            pass

    # -- shared checks ---------------------------------------------------------------

    def _active_binding(self, binding_id: Any) -> BindingRecord:
        if type(binding_id) is not str or not binding_id:
            raise _Refused("invalid")
        try:
            rec = self._bindings.get(binding_id)
        except BindingRefused as exc:
            raise _Refused("binding_unknown") from exc
        if rec.state is not BindingState.ACTIVE or rec.trust is None:
            raise _Refused("binding_unavailable")
        return rec

    def _pinned_return(self, rec: BindingRecord, uri: Any) -> str:
        """Wire §8.2: canonical origin == application_origin, THEN byte equality
        with the transcript's link_return_uri. Never prefix matching."""
        try:
            origin, _path = parse_url(uri, policy=self._origin_policy)
            pinned = parse_origin(rec.application_origin, policy=self._origin_policy)
        except OriginRefused as exc:
            raise _Refused("origin_invalid") from exc
        if not same_origin(origin, pinned):
            raise _Refused("origin_invalid")
        if uri != rec.trust.link_return_uri:
            raise _Refused("origin_invalid")
        return uri

    @staticmethod
    def _is_human_session(actor: Any) -> bool:
        return (
            isinstance(actor, ControlPlaneActor)
            and actor.credential_class == LINK_BROWSER_CREDENTIAL
            and type(actor.user_id) is str
            and bool(actor.user_id)
        )

    def _seat(self, user_id: str, tenant_id: str) -> str:
        try:
            sub = self._seats.seat_sub(user_id, tenant_id)
        except Exception as exc:  # noqa: BLE001 -- directory failure fails closed
            raise _Refused("forbidden") from exc
        if type(sub) is not str or not sub:
            raise _Refused("forbidden")
        return sub

    # -- 1. GET /apps/link/authorize ----------------------------------------------------

    def authorize_page(self, actor: Any, query: Mapping[str, Any], *, now: int) -> LinkPage:
        user = actor.user_id if isinstance(actor, ControlPlaneActor) else "<unknown>"
        if not self._is_human_session(actor):
            self._audit_refusal(user, None, "forbidden")
            return _error_page(403)
        rec: Optional[BindingRecord] = None
        try:
            if not isinstance(query, Mapping) or set(query) != _QUERY_MEMBERS or type(now) is not int:
                raise _Refused("invalid")
            binding_id, state, redirect_uri = (_single(query[k]) for k in ("binding_id", "state", "redirect_uri"))
            if state is None or not _random_token(state, min_bytes=MIN_STATE_BYTES, max_chars=MAX_STATE_CHARS):
                raise _Refused("invalid")
            rec = self._active_binding(binding_id)
            redirect_uri = self._pinned_return(rec, redirect_uri)
            if not _CSP_ORIGIN_RE.fullmatch(rec.application_origin):  # named in form-action below
                raise _Refused("invalid")
            sub = self._seat(actor.user_id, rec.tenant_id)
        except _Refused as exc:
            self._audit_refusal(user, rec, exc.code)
            return _error_page(403 if exc.code == "forbidden" else 400)
        ticket = jws.b64url_encode(self._random(CODE_BYTES))
        self._tickets.put(
            _sha(ticket),
            _Ticket(actor.user_id, rec.tenant_id, rec.binding_id, sub, state, redirect_uri,
                    rec.trust.transcript_sha256, now + TICKET_LIFETIME_S),
            expires_at=now + TICKET_LIFETIME_S,
        )
        e = html.escape
        body = (
            "<p>An application asks to link <strong>your AIDOCS seat</strong> to your account there. "
            "Confirm only if you started this from the application yourself.</p>"
            "<dl>"
            f"<dt>Organisation</dt><dd><code>{e(rec.tenant_id)}</code></dd>"
            f"<dt>Application</dt><dd><code>{e(rec.pack_id)}</code> at <code>{e(rec.application_origin)}</code></dd>"
            f"<dt>Binding</dt><dd><code>{e(rec.binding_id)}</code> (toolspace <code>{e(rec.toolspace_id)}</code>)</dd>"
            f"<dt>Seat</dt><dd><code>{e(sub)}</code> (signed in as <code>{e(actor.user_id)}</code>)</dd>"
            f"<dt>Return to</dt><dd><code>{e(redirect_uri)}</code></dd>"
            "</dl>"
            "<form method=\"post\" action=\"/apps/link/authorize\">"
            f"<input type=\"hidden\" name=\"ticket\" value=\"{e(ticket)}\">"
            "<button type=\"submit\" name=\"decision\" value=\"confirm\">Link this seat</button> "
            "<button type=\"submit\" name=\"decision\" value=\"cancel\">Cancel</button>"
            "</form>"
        )
        return _page(200, "Link your AIDOCS seat", body, ticket=ticket,
                     form_target_origin=rec.application_origin)

    # -- 2. POST /apps/link/authorize ----------------------------------------------------

    def authorize_confirm(self, actor: Any, form: Mapping[str, Any], *, now: int) -> LinkPage:
        user = actor.user_id if isinstance(actor, ControlPlaneActor) else "<unknown>"
        if not self._is_human_session(actor):
            self._audit_refusal(user, None, "forbidden")
            return _error_page(403)
        rec: Optional[BindingRecord] = None
        try:
            if not isinstance(form, Mapping) or set(form) != _FORM_MEMBERS or type(now) is not int:
                raise _Refused("invalid")
            ticket_text, decision = _single(form["ticket"]), _single(form["decision"])
            if ticket_text is None or not ticket_text.isascii() or decision not in ("confirm", "cancel"):
                raise _Refused("invalid")
            ticket = self._tickets.take(_sha(ticket_text), now=now)  # spent by this attempt
            if not isinstance(ticket, _Ticket) or ticket.user_id != actor.user_id or now > ticket.expires_at:
                raise _Refused("invalid")
            if decision == "cancel":
                return _page(200, "Nothing was linked", "<p>You cancelled. Nothing was linked.</p>")
            rec = self._active_binding(ticket.binding_id)
            if rec.tenant_id != ticket.tenant_id or rec.trust.transcript_sha256 != ticket.transcript_sha256:
                raise _Refused("invalid_transition")
            self._pinned_return(rec, ticket.redirect_uri)
            if self._seat(actor.user_id, rec.tenant_id) != ticket.sub:
                raise _Refused("forbidden")
        except _Refused as exc:
            self._audit_refusal(user, rec, exc.code)
            return _error_page(403 if exc.code == "forbidden" else 400)
        code = jws.b64url_encode(self._random(CODE_BYTES))
        grant = _Grant(actor.user_id, rec.tenant_id, rec.binding_id, ticket.sub, ticket.redirect_uri,
                       rec.trust.transcript_sha256, now + self._code_lifetime)
        try:
            self._emit(LINK_CODE_KIND, actor.user_id, rec, ticket.sub)  # write-ahead
        except Exception:  # noqa: BLE001 -- no audit, no code
            return _error_page(503)
        self._codes.put(_sha(code), grant, expires_at=grant.expires_at)
        location = f"{ticket.redirect_uri}?code={quote(code, safe='')}&state={quote(ticket.state, safe='')}"
        return _page(302, "Returning to the application",
                     "<p>Returning you to the application.</p>", location=location)

    # -- 3. POST /v1/control/app-links/redeem ----------------------------------------------

    def redeem(self, authorization: Optional[str], body: bytes, *, now: int) -> LinkResponse:
        rec: Optional[BindingRecord] = None
        actor_id = "<application>"
        try:
            token, parsed, rec = self._authenticate(authorization, body, now)
            actor_id = f"application:{rec.pack_id}"
            grant = self._spend_code(rec, parsed, now)
            return self._record(rec, grant, actor_id, now)
        except _Refused as exc:
            code = exc.code if exc.code in _MESSAGES else CODE_CONTROL_ASSERTION_INVALID
            self._audit_refusal(actor_id, rec, code)
            return LinkResponse(
                _STATUS[code], {"ok": False, "error": {"code": code, "message": _MESSAGES[code], "retryable": False}}
            )

    def _authenticate(self, authorization: Optional[str], body: bytes, now: int):
        bad = CODE_CONTROL_ASSERTION_INVALID
        if type(now) is not int or type(authorization) is not str or not authorization.startswith("Bearer "):
            raise _Refused(bad, "no bearer")
        token = authorization[len("Bearer "):]
        if type(body) is not bytes:
            raise _Refused(bad, "body")
        try:
            parsed = json.loads(body.decode("utf-8"), object_pairs_hook=_refuse_duplicates,
                                parse_constant=_refuse_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise _Refused(bad, "body json") from exc
        if type(parsed) is not dict or set(parsed) != _BODY_MEMBERS or any(type(v) is not str for v in parsed.values()):
            raise _Refused(bad, "body members")
        try:
            rec = self._active_binding(parsed["binding_id"])
        except _Refused as exc:
            raise _Refused(bad, "binding") from exc
        trust = rec.trust
        try:
            # ONLY the pinned binding-control key: an entitlement key's kid is not in this keyset.
            verified = jws.verify_compact(token, expected_typ=jws.TYP_CONTROL, keyset=trust.binding_control_keyset)
            claims = verified.payload
            if not _CLAIMS_REQUIRED <= set(claims) <= _CLAIMS_REQUIRED | _CLAIMS_OPTIONAL:
                raise _Refused(bad, "claim members")
            jws.check_lifetime(claims, now=now, max_lifetime_s=MAX_CONTROL_LIFETIME_S)
        except jws.JwsError as exc:
            raise _Refused(bad, "jws") from exc
        if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now + jws.MAX_CLOCK_SKEW_S):
            raise _Refused(bad, "nbf")
        expected = {
            "iss": rec.pack_id,
            "aud": trust.aidocs_origin,
            "tenant_id": rec.tenant_id,
            "binding_id": rec.binding_id,
            "purpose": PURPOSE_LINK_REDEEM,
        }
        for name, value in expected.items():
            if type(claims[name]) is not str or claims[name] != value:
                raise _Refused(bad, f"claim {name}")
        if parsed["binding_id"] != rec.binding_id:
            raise _Refused(bad, "body binding")
        jti = claims["jti"]
        if type(jti) is not str or not _JTI_RE.fullmatch(jti):
            raise _Refused(bad, "jti")
        digest = claims["body_sha256"]
        try:
            actual = redeem_body_sha256(parsed)
        except (JcsError, TypeError) as exc:
            raise _Refused(bad, "body jcs") from exc
        if type(digest) is not str or not _HEX64.fullmatch(digest) or digest != actual:
            raise _Refused(bad, "body_sha256")  # refused WITHOUT consuming the jti (RFC §12.2)
        keep = max(claims["exp"] + jws.MAX_CLOCK_SKEW_S, now + JTI_RETENTION_S)
        if not self._replay.consume((claims["iss"], verified.header["kid"], jti), keep_until=keep, now=now):
            raise _Refused(bad, "jti replay")
        return token, parsed, rec

    def _spend_code(self, rec: BindingRecord, body: Mapping[str, str], now: int) -> _Grant:
        bad = CODE_LINK_CODE_INVALID
        code = body["code"]
        if not code.isascii() or not _random_token(code, min_bytes=CODE_BYTES, max_chars=MAX_CODE_CHARS):
            raise _Refused(bad, "code shape")
        grant = self._codes.take(_sha(code), now=now)  # SPENT by this attempt, success or failure
        if not isinstance(grant, _Grant):
            raise _Refused(bad, "code unknown or spent")
        if now > grant.expires_at:
            raise _Refused(bad, "code expired")
        if (grant.binding_id, grant.tenant_id, grant.transcript_sha256) != (
            rec.binding_id, rec.tenant_id, rec.trust.transcript_sha256
        ):
            raise _Refused(bad, "code for another binding or trust")
        try:
            uri = self._pinned_return(rec, body["redirect_uri"])
        except _Refused as exc:
            raise _Refused(bad, "redirect_uri") from exc
        if uri != grant.redirect_uri:
            raise _Refused(bad, "redirect_uri")
        try:
            if self._seat(grant.user_id, rec.tenant_id) != grant.sub:
                raise _Refused(bad, "seat changed")
        except _Refused as exc:
            raise _Refused(bad, "seat") from exc
        return grant

    def _record(self, rec: BindingRecord, grant: _Grant, actor_id: str, now: int) -> LinkResponse:
        # ONE transaction (r0b2 B1): the linked DECISION and the durable link row
        # commit together or not at all. A failure after the DECISION rolls it
        # back; an audit failure writes no row.
        tx = self._transaction(rec.tenant_id) if self._transaction is not None else contextlib.nullcontext()
        try:
            with tx:
                self._emit(LINKED_KIND, actor_id, rec, grant.sub)  # write-ahead: no audit, no link
                self._links.record_link(LinkRecord(rec.tenant_id, rec.binding_id, grant.sub, now))
        except Exception as exc:  # noqa: BLE001
            raise _Refused(CODE_LINK_CODE_INVALID, "audit or store unavailable") from exc
        return LinkResponse(200, {"ok": True, "tenant_id": rec.tenant_id, "binding_id": rec.binding_id, "sub": grant.sub})
