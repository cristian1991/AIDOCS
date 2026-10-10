"""AIDOCS client of the CodeNexus seat service -- #1134 Phase B (WIRE-1134 §8).

CodeNexus is the seat authority (AppSeatPlan / AppSeatBinding). AIDOCS keeps NO
seat table, count or assign state: this client is STATELESS and caches nothing.
The connect builder (``outer_gate_transport``) composes it; it owns no route.

CodeNexus internal routes on ``AIDOCS_CODENEXUS_INTERNAL_URL``:

* ``POST /api/internal/app-seats/admit``   (MAC, purpose ``seat_admit``)
* ``POST /api/internal/app-seats/confirm`` (MAC, purpose ``seat_confirm``)
* ``POST /api/internal/app-seats/revoke``  (MAC, purpose ``seat_revoke``)
* ``POST /api/internal/app-seats/abandon`` (MAC, purpose ``seat_abandon``)
* ``GET  /api/internal/app-seats/quota?installationId``
* ``GET  /api/internal/app-seats/binding?seatBindingId&installationId&principalId``

Every call carries ``Authorization: Bearer $AIDOCS_INTERNAL_S2S_SECRET``
(transport admission ONLY, R-14). The four writes also carry
``AIDOCS-Seat-Service: <token>``, a body-bound HS256 MAC:

* key = HMAC-SHA256(key = UTF-8(S2S value), msg = ``aidocs/aidocs-seat-service/v1``);
* header = JCS ``{"alg":"HS256","typ":"aidocs-seat-service+jwt"}``;
* claims = JCS of exactly ``{iss:"aidocs", aud:"codenexus-seat-service", method,
  path, purpose, iat, exp = iat + 60, jti (32 lowercase hex, fresh per call),
  installation_id, organization_id, body_sha256}`` where ``body_sha256`` is the
  lowercase hex SHA-256 of the RAW body bytes sent (the body is JCS);
* token = b64url(header) "." b64url(claims) "." b64url(MAC over the first two).

CodeNexus consumes the jti LAST. Shared vectors:
``tests/app_packs/fixtures/seat-service-mac-vectors-v1.json`` (README §7).

Fail closed, always (answers as CN's live source defines them, 2026-10-05):

* HTTP 200 ``{"ok":true,...}`` in the route's CLOSED shape is success;
* HTTP 200 exactly ``{"ok":false,"code":<code>}`` (no other member) is a
  business refusal, :class:`SeatServiceRefused` (not retryable), carrying a
  code from the closed :data:`KNOWN_REFUSAL_CODES`, else the generic
  ``app_seat_service_refused``; CodeNexus text is never reflected;
* HTTP 401 exactly ``{"ok":false,"code":"unauthorized"}`` (any auth failure,
  incl. a replayed jti) is :class:`SeatServiceUnauthorized` and HTTP 400
  exactly ``{"ok":false,"code":"invalid_request"}`` (a client bug) is
  :class:`SeatServiceBadRequest`: both NOT retryable;
* everything else -- 503 ``unavailable``, any other status or shape, a
  transport fault, an oversize / non-JSON / duplicate-member / non-finite
  answer, a member of the wrong type -- is :class:`SeatServiceUnavailable`
  (``app_seat_service_unavailable``, retryable);
* unconfigured (no URL / weak secret) is :class:`SeatServiceUnconfigured`
  (not retryable); an invalid request is :class:`SeatServiceRequestInvalid`;
  neither makes an exchange;
* :meth:`SeatServiceClient.quota` turns EVERY failure into
  :class:`SeatQuotaUnknown` (``seat_quota_unknown``): quota is known only when
  CodeNexus answered it authoritatively;
* the egress chokepoint (purpose ``app_seat_service``) is asked before any
  exchange, allowlisted to the configured host only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from ..governed_egress import EgressRefused, assert_egress_allowed
from .jcs import JcsError, canonicalize
from .vault_key_source import HttpCall, INTERNAL_URL_ENV, S2S_SECRET_ENV

__all__ = [
    "CLAIM_MEMBERS",
    "EGRESS_PURPOSE",
    "INTERNAL_URL_ENV",
    "KNOWN_REFUSAL_CODES",
    "MAC_DOMAIN",
    "MAX_RESPONSE_BYTES",
    "S2S_SECRET_ENV",
    "SEAT_SERVICE_HEADER",
    "WRITE_PATHS",
    "WRITE_PURPOSES",
    "SeatAbandonment",
    "SeatAdmission",
    "SeatBinding",
    "SeatConfirmation",
    "SeatQuota",
    "SeatQuotaUnknown",
    "SeatRevocation",
    "SeatServiceBadRequest",
    "SeatServiceClient",
    "SeatServiceError",
    "SeatServiceUnauthorized",
    "SeatServiceRefused",
    "SeatServiceRequestInvalid",
    "SeatServiceUnavailable",
    "SeatServiceUnconfigured",
    "decode_unverified_claims",
]

EGRESS_PURPOSE = "app_seat_service"
MAC_DOMAIN = "aidocs/aidocs-seat-service/v1"
SEAT_SERVICE_HEADER = "AIDOCS-Seat-Service"
TYP = "aidocs-seat-service+jwt"
ISS = "aidocs"
AUD = "codenexus-seat-service"
LIFETIME_S = 60
MAX_RESPONSE_BYTES = 16 * 1024
_TIMEOUT_S = 8.0
_MIN_SECRET_BYTES = 32
_MAX_ID_CHARS = 300
_MAX_SAFE_INT = 2**53 - 1

_PREFIX = "/api/internal/app-seats/"
WRITE_PATHS = {verb: _PREFIX + verb for verb in ("admit", "confirm", "revoke", "abandon")}
WRITE_PURPOSES = {verb: "seat_" + verb for verb in WRITE_PATHS}
QUOTA_PATH = _PREFIX + "quota"
BINDING_PATH = _PREFIX + "binding"
CLAIM_MEMBERS = frozenset({"iss", "aud", "method", "path", "purpose", "iat", "exp", "jti",
                           "installation_id", "organization_id", "body_sha256"})

#: CodeNexus business refusals (WIRE-1134 §8.1-8.5). Anything else is generic.
KNOWN_REFUSAL_CODES = frozenset({
    "app_binding_disabled", "app_seat_required", "app_seat_conflict", "seats_exhausted",
    "app_seat_identity_conflict", "not_found",
})
UNAVAILABLE = "app_seat_service_unavailable"
UNAUTHORIZED = "app_seat_service_unauthorized"
BAD_REQUEST = "app_seat_service_invalid_request"
REFUSED_GENERIC = "app_seat_service_refused"
REQUEST_INVALID = "app_seat_service_request_invalid"
QUOTA_UNKNOWN = "seat_quota_unknown"
#: JS ``Date.prototype.toISOString()`` exactly: UTC, ``Z``, 3 fractional digits.
_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_JTI_RE = re.compile(r"^[0-9a-f]{32}$")
#: The only non-200 answers with a meaning; anything else is unavailable.
_NON_200 = {401: "unauthorized", 400: "invalid_request"}

PlanVersion = int


def _urllib_http(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes],
                 timeout: float) -> tuple[int, bytes]:
    """The default transport. Unlike custody's, it reads a BOUNDED error body: CN's
    401 / 400 / 503 bodies decide retryability."""
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310 -- fixed internal host from operator env, egress-gated by the caller
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200) or 200), resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp is not None else b""
        except Exception:  # noqa: BLE001 -- an unreadable error body is just an empty one
            raw = b""
        return int(exc.code), raw if type(raw) is bytes else b""


_default_http = _urllib_http


# -- errors --------------------------------------------------------------------------


class SeatServiceError(RuntimeError):
    """A seat-service call failed closed. ``code`` is non-disclosing; ``retryable`` is explicit."""

    code: str = UNAVAILABLE
    retryable: bool = False

    def __init__(self, code: Optional[str] = None, *, retryable: Optional[bool] = None) -> None:
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        super().__init__(f"seat service: {self.code}")


class SeatServiceUnavailable(SeatServiceError):
    """No authoritative answer (transport, status, size, shape). Retryable."""

    code = UNAVAILABLE
    retryable = True


class SeatServiceUnconfigured(SeatServiceUnavailable):
    """The internal URL or S2S value is absent / weak: a deployment fault a retry cannot fix."""

    retryable = False


class SeatServiceUnauthorized(SeatServiceUnavailable):
    """CN answered 401 ``unauthorized`` (bearer, MAC, claims or a replayed jti). Not retryable."""

    code = UNAUTHORIZED
    retryable = False


class SeatServiceBadRequest(SeatServiceUnavailable):
    """CN answered 400 ``invalid_request``: a client bug. Not retryable."""

    code = BAD_REQUEST
    retryable = False


class SeatServiceRefused(SeatServiceError):
    """CodeNexus answered ``ok:false``: a typed business refusal. Not retryable."""

    retryable = False


class SeatServiceRequestInvalid(SeatServiceError):
    """The caller's request is not wire-valid; nothing was sent. Not retryable."""

    code = REQUEST_INVALID
    retryable = False


class SeatQuotaUnknown(SeatServiceError):
    """Quota could not be obtained authoritatively: fail closed (``seat_quota_unknown``).
    ``retryable`` is the cause's."""

    code = QUOTA_UNKNOWN


# -- results (immutable, no state kept) ----------------------------------------------


@dataclass(frozen=True)
class SeatAdmission:
    seat_binding_id: str
    connector_principal_id: str
    created: bool
    plan_version: PlanVersion


@dataclass(frozen=True)
class SeatConfirmation:
    state: str  # "confirmed"


@dataclass(frozen=True)
class SeatRevocation:
    seat_binding_id: Optional[str]
    state: str  # "revoked" | "no_active_binding"


@dataclass(frozen=True)
class SeatAbandonment:
    state: str  # "abandoned" | "not_owned" | "already_abandoned"


@dataclass(frozen=True)
class SeatQuota:
    purchased: int
    active: int  # counts provisional bindings too (§8.4)
    plan_version: PlanVersion
    as_of: str

    @property
    def available(self) -> int:
        """Never negative: an overbooked plan (after a decrease) has 0 available."""
        return max(self.purchased - self.active, 0)

    def crm_answer(self) -> dict:
        """The CRM ``seat_quota`` success body (WIRE-1134-CAT §1(b)), from CN's authoritative facts."""
        return {"ok": True, "purchased": self.purchased, "active": self.active, "available": self.available,
                "plan_version": self.plan_version, "as_of": self.as_of}


@dataclass(frozen=True)
class SeatBinding:
    active: bool
    provisional: bool
    current_for_principal: bool
    crm_user_id: str
    crm_seat_assignment_id: str
    crm_seat_assignment_version: int
    binding_generation: str
    version: int


# -- strict parsing ------------------------------------------------------------------


def _pairs(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate member")
        out[key] = value
    return out


def _no_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _is_str(v: Any) -> bool:
    return type(v) is str and 0 < len(v) <= _MAX_ID_CHARS


def _is_count(v: Any) -> bool:
    return type(v) is int and 0 <= v <= _MAX_SAFE_INT


def _is_bool(v: Any) -> bool:
    return type(v) is bool


def _is_positive(v: Any) -> bool:
    return _is_count(v) and v >= 1


_is_plan_version = _is_positive  # CN: planVersion is an integer >= 1 (admit, quota)


def _is_as_of(v: Any) -> bool:
    if type(v) is not str or not _AS_OF_RE.fullmatch(v):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def _is_refusal(answer: dict, code: Optional[str] = None) -> bool:
    """Exactly ``{"ok": false, "code": <str>}`` (and that code, when given)."""
    return (set(answer) == {"ok", "code"} and answer["ok"] is False and type(answer["code"]) is str
            and (code is None or answer["code"] == code))


def _closed(answer: dict, shape: Mapping[str, Callable[[Any], bool]]) -> dict:
    """``answer`` must be exactly ``{"ok": true} + shape`` with every member of its type."""
    if set(answer) != {"ok", *shape} or answer.get("ok") is not True:
        raise SeatServiceUnavailable()
    for member, check in shape.items():
        if not check(answer[member]):
            raise SeatServiceUnavailable()
    return answer


def decode_unverified_claims(token: str) -> dict:
    """The claims of a seat-service token, NOT verified (diagnostics and tests only)."""
    seg = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# -- the client ----------------------------------------------------------------------


class SeatServiceClient:
    """Stateless client of the CodeNexus seat service (see module doc)."""

    __slots__ = ("_http", "_base", "_secret", "_timeout", "_clock", "_jti")

    def __init__(
        self,
        *,
        http: Optional[HttpCall] = None,
        base_url: Optional[str] = None,
        secret: Optional[str] = None,
        timeout_s: float = _TIMEOUT_S,
        clock: Optional[Callable[[], int]] = None,
        jti_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._http = http or _default_http
        self._base = base_url  # None: read the gate env on every call
        self._secret = secret  # None: read the gate env on every call
        self._timeout = float(timeout_s)
        self._clock = clock or (lambda: int(time.time()))
        self._jti = jti_factory or (lambda: secrets.token_hex(16))

    def __repr__(self) -> str:
        return "<SeatServiceClient>"

    # -- transport --------------------------------------------------------------------

    def _config(self) -> tuple[str, str]:
        base = (self._base if self._base is not None else os.environ.get(INTERNAL_URL_ENV, "")).strip()
        secret = (self._secret if self._secret is not None else os.environ.get(S2S_SECRET_ENV, "")).strip()
        if not base or len(secret.encode("utf-8")) < _MIN_SECRET_BYTES:
            raise SeatServiceUnconfigured()
        return base.rstrip("/"), secret

    def _token(self, secret: str, *, verb: str, body: bytes, installation_id: str, organization_id: str) -> str:
        iat = self._clock()
        jti = self._jti()
        if type(iat) is not int or type(jti) is not str or not _JTI_RE.fullmatch(jti):
            raise SeatServiceRequestInvalid()
        claims = {
            "iss": ISS, "aud": AUD, "method": "POST", "path": WRITE_PATHS[verb], "purpose": WRITE_PURPOSES[verb],
            "iat": iat, "exp": iat + LIFETIME_S, "jti": jti, "installation_id": installation_id,
            "organization_id": organization_id, "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        signing_input = (_b64(canonicalize({"alg": "HS256", "typ": TYP})) + "." + _b64(canonicalize(claims))).encode("ascii")
        key = hmac.new(secret.encode("utf-8"), MAC_DOMAIN.encode("ascii"), hashlib.sha256).digest()
        return signing_input.decode("ascii") + "." + _b64(hmac.new(key, signing_input, hashlib.sha256).digest())

    def _exchange(self, method: str, path: str, *, query: Optional[Mapping[str, str]] = None,
                  body: Optional[Mapping[str, Any]] = None, mac: Optional[Mapping[str, str]] = None) -> dict:
        base, secret = self._config()
        url = base + path + ("?" + urllib.parse.urlencode(list(query.items())) if query else "")
        try:  # the one in-process egress chokepoint; only the operator-configured host
            assert_egress_allowed(url, purpose=EGRESS_PURPOSE, allow_hosts=[urllib.parse.urlsplit(base).hostname or ""])
        except EgressRefused:
            raise SeatServiceUnavailable() from None
        headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json", "Cache-Control": "no-store"}
        data: Optional[bytes] = None
        if body is not None:
            try:
                data = canonicalize(dict(body))
            except JcsError:
                raise SeatServiceRequestInvalid() from None
            headers["Content-Type"] = "application/json"
            if mac is not None:
                headers[SEAT_SERVICE_HEADER] = self._token(secret, body=data, **mac)
        failed = False
        status, raw = 0, b""
        try:
            status, raw = self._http(method, url, headers, data, self._timeout)
        except Exception:  # noqa: BLE001 -- unreachable seat service fails closed; text dropped
            failed = True
        if failed or type(status) is not int or type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise SeatServiceUnavailable()
        try:
            parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_no_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            parsed = None
        if type(parsed) is not dict:
            raise SeatServiceUnavailable()
        if status != 200:
            if status in _NON_200 and _is_refusal(parsed, _NON_200[status]):
                raise SeatServiceUnauthorized() if status == 401 else SeatServiceBadRequest()
            raise SeatServiceUnavailable()  # 503 "unavailable" and every other status / shape
        if parsed.get("ok") is False:
            if not _is_refusal(parsed):
                raise SeatServiceUnavailable()
            code = parsed["code"]
            raise SeatServiceRefused(code if code in KNOWN_REFUSAL_CODES else REFUSED_GENERIC)
        return parsed

    def _write(self, verb: str, body: Mapping[str, Any], *, installation_id: str, organization_id: str) -> dict:
        _require(_is_str(organization_id) and _is_str(installation_id))
        return self._exchange("POST", WRITE_PATHS[verb], body=body, mac={
            "verb": verb, "installation_id": installation_id, "organization_id": organization_id})

    # -- writes (MAC) -----------------------------------------------------------------

    def admit(self, *, organization_id: str, installation_id: str, binding_id: str, binding_generation: str,
              ceremony_id: str, crm_user_id: str, crm_seat_assignment_id: str, seat_version: int,
              assigned_by_crm_user_id: str) -> SeatAdmission:
        """§8.1. The resulting binding is PROVISIONAL until :meth:`confirm`."""
        _require(all(_is_str(v) for v in (binding_id, binding_generation, ceremony_id, crm_user_id,
                                          crm_seat_assignment_id, assigned_by_crm_user_id)) and _is_count(seat_version))
        a = _closed(self._write("admit", {
            "installationId": installation_id, "bindingId": binding_id, "bindingGeneration": binding_generation,
            "ceremonyId": ceremony_id, "crmUserId": crm_user_id, "crmSeatAssignmentId": crm_seat_assignment_id,
            "seatVersion": seat_version, "assignedByCrmUserId": assigned_by_crm_user_id,
        }, installation_id=installation_id, organization_id=organization_id), {
            "seatBindingId": _is_str, "connectorPrincipalId": _is_str, "created": _is_bool,
            "planVersion": _is_plan_version,
        })
        return SeatAdmission(a["seatBindingId"], a["connectorPrincipalId"], a["created"], a["planVersion"])

    def confirm(self, *, organization_id: str, installation_id: str, ceremony_id: str,
                seat_binding_id: str) -> SeatConfirmation:
        """§8.1a: mark this ceremony's binding non-provisional. Idempotent."""
        _require(_is_str(ceremony_id) and _is_str(seat_binding_id))
        a = _closed(self._write("confirm", {
            "installationId": installation_id, "ceremonyId": ceremony_id, "seatBindingId": seat_binding_id,
        }, installation_id=installation_id, organization_id=organization_id),
            {"state": lambda v: v == "confirmed"})
        return SeatConfirmation(a["state"])

    def revoke(self, *, organization_id: str, installation_id: str, crm_user_id: str, seat_assignment_id: str,
               seat_version: int, event_id: str, reason: str) -> SeatRevocation:
        """§8.2: exact identity, terminal, tombstoning."""
        _require(all(_is_str(v) for v in (crm_user_id, seat_assignment_id, event_id, reason))
                 and _is_count(seat_version))
        a = _closed(self._write("revoke", {
            "installationId": installation_id, "crmUserId": crm_user_id, "seatAssignmentId": seat_assignment_id,
            "seatVersion": seat_version, "eventId": event_id, "reason": reason,
        }, installation_id=installation_id, organization_id=organization_id), {
            "seatBindingId": lambda v: v is None or _is_str(v),
            "state": lambda v: v in ("revoked", "no_active_binding"),
        })
        # CN: "revoked" ALWAYS names the binding (incl. an idempotent re-revoke); only
        # "no_active_binding" carries null.
        if (a["state"] == "revoked") != (a["seatBindingId"] is not None):
            raise SeatServiceUnavailable()
        return SeatRevocation(a["seatBindingId"], a["state"])

    def abandon(self, *, organization_id: str, installation_id: str, ceremony_id: str,
                seat_binding_id: str) -> SeatAbandonment:
        """§8.3: revoke ONLY a provisional binding created by this ceremony."""
        _require(_is_str(ceremony_id) and _is_str(seat_binding_id))
        a = _closed(self._write("abandon", {
            "installationId": installation_id, "ceremonyId": ceremony_id, "seatBindingId": seat_binding_id,
        }, installation_id=installation_id, organization_id=organization_id),
            {"state": lambda v: v in ("abandoned", "not_owned", "already_abandoned")})
        return SeatAbandonment(a["state"])

    # -- reads (bearer only) ------------------------------------------------------------

    def quota(self, *, installation_id: str) -> SeatQuota:
        """§8.4, CN's authoritative quota. ANY failure is :class:`SeatQuotaUnknown`."""
        try:
            _require(_is_str(installation_id))
            a = _closed(self._exchange("GET", QUOTA_PATH, query={"installationId": installation_id}), {
                "purchased": _is_count, "active": _is_count, "planVersion": _is_plan_version, "asOf": _is_as_of,
            })
        except SeatServiceError as exc:
            raise SeatQuotaUnknown(retryable=exc.retryable) from None
        return SeatQuota(a["purchased"], a["active"], a["planVersion"], a["asOf"])

    def binding(self, *, seat_binding_id: str, installation_id: str, principal_id: str) -> SeatBinding:
        """§8.5 (R-16): the per-request tool-call seat read. Closed key set; the CRM
        seat-assignment version must be a JSON integer."""
        _require(_is_str(seat_binding_id) and _is_str(installation_id) and _is_str(principal_id))
        a = _closed(self._exchange("GET", BINDING_PATH, query={
            "seatBindingId": seat_binding_id, "installationId": installation_id, "principalId": principal_id,
        }), {
            "active": _is_bool, "provisional": _is_bool, "currentForPrincipal": _is_bool, "crmUserId": _is_str,
            "crmSeatAssignmentId": _is_str, "crmSeatAssignmentVersion": _is_count, "bindingGeneration": _is_str,
            "version": _is_positive,  # CN: integer >= 1
        })
        return SeatBinding(a["active"], a["provisional"], a["currentForPrincipal"], a["crmUserId"],
                           a["crmSeatAssignmentId"], a["crmSeatAssignmentVersion"], a["bindingGeneration"],
                           a["version"])


def _require(ok: bool) -> None:
    if not ok:
        raise SeatServiceRequestInvalid()
