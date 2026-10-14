"""#1134 Phase B3: per-request app-connector admission over the CodeNexus AppSeatBinding.

PLAN-1134 §0/§2 (B1, B3), WIRE-1134 §8.5, §9b, §9c. CodeNexus says WHO may exist
(installation, binding grant, AppSeatBinding); the CRM says WHAT the user may do
(its execution-time check stays FINAL); AIDOCS only proves THIS request.

AIDOCS keeps NO seat table, seat boolean, count or assign state. The only seat
facts it holds are REFERENCES on the OAuth token family (:class:`SeatFamily`):
the exact CodeNexus ``seat_binding_id`` + the binding generation it was admitted
under, and the CRM-owned ``crmSeatAssignmentId`` / ``crmSeatAssignmentVersion``
copied at admission. They are written once, at the connect ceremony's code
release (B2), and never edited: a re-seat is a NEW AppSeatBinding and a NEW
token family, so an old family can never revive (SPEC correction 3).

Every request, ALL must hold, else the request is refused (:func:`admit_request`):

1. the token family is seat-bound and the token's principal is exactly that
   family's connector principal (``app_seat_required``);
2. AIOS trust/security floor healthy (``app_binding_disabled``);
3. the CodeNexus installation is active/open AND its current AppBindingGrant is
   active, for exactly the family's installation/organization/binding, AND the
   installation's ``current_generation`` == the AIDOCS sealed generation == the
   family's generation (``app_binding_disabled``);
4. the LIVE CodeNexus seat read (§8.5) for exactly ``(seat_binding_id,
   installation_id, principal)``: ``active`` AND NOT ``provisional`` AND
   ``currentForPrincipal`` AND ``bindingGeneration`` == the family's generation
   AND ``crmSeatAssignmentId`` / ``crmSeatAssignmentVersion`` == the family's
   references (``app_seat_required``); an unreachable / malformed read is
   ``app_seat_service_unavailable`` (unknown freshness fails closed);
5. at tool-call time (runtime S9 step), the CRM entitlement attestation's
   ``seat_assignment_id`` / ``seat_assignment_version`` equal the family's
   references (:func:`attestation_seat_matches`; JSON integers, a type mismatch
   is a mismatch). The attestation is a bounded HINT; the CRM's execution-time
   check is the authority.

There is no alternate path: the transitional OWNER/ADMIN bridge (A7) is gone.
The seat read is the B5 ``SeatServiceClient.binding`` (typed ``SeatBinding``) or
the raw §8.5 JSON object; both are judged by the same closed rules.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

__all__ = [
    "APP_AUTHORITY_UNAVAILABLE",
    "APP_BINDING_DISABLED",
    "APP_SEAT_REQUIRED",
    "APP_SEAT_SERVICE_UNAVAILABLE",
    "PRINCIPAL_KIND_APP_CONNECTOR",
    "Admission",
    "CodenexusInstallationReader",
    "InstallationView",
    "SeatAdmission",
    "SeatBindingView",
    "SeatFamily",
    "admit_request",
    "attestation_seat_matches",
    "is_typed_refusal",
    "parse_seat_binding",
]

APP_SEAT_REQUIRED = "app_seat_required"
APP_BINDING_DISABLED = "app_binding_disabled"
APP_SEAT_SERVICE_UNAVAILABLE = "app_seat_service_unavailable"
APP_AUTHORITY_UNAVAILABLE = "app_authority_unavailable"

#: R-19: connector principals have NO TeamRole; minting and validation branch on this kind.
PRINCIPAL_KIND_APP_CONNECTOR = "app_connector"

_ACTIVE = "active"
_SEAT_BINDING_KEYS = frozenset(
    {
        "ok", "active", "provisional", "currentForPrincipal", "crmUserId",
        "crmSeatAssignmentId", "crmSeatAssignmentVersion", "bindingGeneration", "version",
    }
)


def _text(value: Any) -> bool:
    return type(value) is str and value != ""


def _uint(value: Any) -> bool:
    # bool is an int subclass: never accepted where a JSON integer is required.
    return type(value) is int and value >= 0


def is_typed_refusal(exc: BaseException) -> bool:
    """A B5 ``SeatServiceRefused``-shaped error: a CodeNexus BUSINESS answer
    (``seats_exhausted``, ``not_found``, ...). Everything else -- every
    ``SeatServiceUnavailable`` subclass, including the non-retryable
    unauthorized / bad-request ones, an unconfigured client or any other fault --
    is "could not ask" and fails closed as unavailable."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "SeatServiceUnavailable" in names or "SeatServiceUnconfigured" in names:
        return False
    if "SeatServiceRefused" in names:
        return True
    code = getattr(exc, "code", None)
    return bool(
        _text(code)
        and getattr(exc, "retryable", True) is False
        and (not code.startswith("app_seat_service_") or code == "app_seat_service_refused")
    )


# -- the references on the token family ----------------------------------------------------------


@dataclass(frozen=True)
class SeatFamily:
    """The seat REFERENCES carried by one OAuth token family (B1). Immutable."""

    connector_sub: str
    seat_binding_id: str
    binding_generation: str
    installation_id: str
    binding_id: str
    tenant_id: str
    organization_id: str
    crm_seat_assignment_id: str
    crm_seat_assignment_version: int

    def __post_init__(self) -> None:
        for name in (
            "connector_sub", "seat_binding_id", "binding_generation", "installation_id",
            "binding_id", "tenant_id", "organization_id", "crm_seat_assignment_id",
        ):
            if not _text(getattr(self, name)):
                raise ValueError(f"seat family {name} must be a non-empty string")
        if not _uint(self.crm_seat_assignment_version):
            raise ValueError("seat family crm_seat_assignment_version must be a non-negative integer")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: Any) -> Optional["SeatFamily"]:
        """A stored family, or None when absent / malformed (never repaired)."""
        if type(raw) is not str or not raw:
            return None
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or set(doc) != set(cls.__dataclass_fields__):
                return None
            return cls(**doc)
        except (ValueError, TypeError):
            return None


# -- the CodeNexus facts -------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallationView:
    """One AppInstallationProjection row (CN view contract, PLAN-1134 §5)."""

    installation_id: str
    organization_id: str
    binding_id: str
    installation_status: str
    grant_state: str
    ended_at: Any
    current_generation: Optional[str]


@dataclass(frozen=True)
class SeatBindingView:
    """The CodeNexus §8.5 seat-binding read, closed key set (CN main 530ba6c)."""

    active: bool
    provisional: bool
    current_for_principal: bool
    crm_user_id: str
    crm_seat_assignment_id: str
    crm_seat_assignment_version: int
    binding_generation: str
    version: int


_TYPED_FIELDS = (
    "active", "provisional", "current_for_principal", "crm_user_id", "crm_seat_assignment_id",
    "crm_seat_assignment_version", "binding_generation", "version",
)


def parse_seat_binding(doc: Any) -> Optional[SeatBindingView]:
    """The §8.5 answer (raw JSON object or the B5 typed ``SeatBinding``), or None
    when it is not exactly the closed success shape."""
    if isinstance(doc, Mapping):
        if set(doc) != _SEAT_BINDING_KEYS or doc.get("ok") is not True:
            return None
        values = (
            doc["active"], doc["provisional"], doc["currentForPrincipal"], doc["crmUserId"],
            doc["crmSeatAssignmentId"], doc["crmSeatAssignmentVersion"], doc["bindingGeneration"], doc["version"],
        )
    elif doc is not None and all(hasattr(doc, f) for f in _TYPED_FIELDS):
        values = tuple(getattr(doc, f) for f in _TYPED_FIELDS)
    else:
        return None
    active, provisional, current, user, sid, sver, gen, version = values
    if not all(type(v) is bool for v in (active, provisional, current)):
        return None
    if not (_text(user) and _text(sid) and _text(gen) and _uint(sver) and _uint(version)):
        return None
    return SeatBindingView(active, provisional, current, user, sid, sver, gen, version)


# -- the predicate -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str = ""
    family: Optional[SeatFamily] = None


def _refuse(reason: str) -> Admission:
    return Admission(False, reason)


def admit_request(
    *,
    family: Optional[SeatFamily],
    principal_user_id: Any,
    trust_healthy: bool,
    installation: Optional[InstallationView],
    sealed_generation: Any,
    seat_binding: Any,
) -> Admission:
    """B3 clauses 1-4 (module doc), in order. Pure: every fact is passed in."""
    if not isinstance(family, SeatFamily) or not _text(principal_user_id) or principal_user_id != family.connector_sub:
        return _refuse(APP_SEAT_REQUIRED)
    if trust_healthy is not True:
        return _refuse(APP_BINDING_DISABLED)
    inst = installation
    if (
        not isinstance(inst, InstallationView)
        or inst.installation_id != family.installation_id
        or inst.organization_id != family.organization_id
        or inst.binding_id != family.binding_id
        or inst.installation_status != _ACTIVE
        or inst.grant_state != _ACTIVE
        or inst.ended_at is not None
        or not _text(inst.current_generation)
        or inst.current_generation != family.binding_generation
        or not _text(sealed_generation)
        or sealed_generation != family.binding_generation
    ):
        return _refuse(APP_BINDING_DISABLED)
    if isinstance(seat_binding, Mapping) and seat_binding.get("ok") is False:
        return _refuse(APP_SEAT_REQUIRED)  # a typed CodeNexus refusal (unknown / not this principal)
    view = parse_seat_binding(seat_binding)
    if view is None:
        return _refuse(APP_SEAT_SERVICE_UNAVAILABLE)
    if not (
        view.active is True
        and view.provisional is False
        and view.current_for_principal is True
        and view.binding_generation == family.binding_generation
        and view.crm_seat_assignment_id == family.crm_seat_assignment_id
        and view.crm_seat_assignment_version == family.crm_seat_assignment_version
    ):
        return _refuse(APP_SEAT_REQUIRED)
    return Admission(True, "", family)


def attestation_seat_matches(family: Any, seat_assignment_id: Any, seat_assignment_version: Any) -> bool:
    """B3 clause 5: the CRM attestation's seat references equal the family's.

    Absent claims (None) never match: a seated connector's attestation must name
    its seat. ``seat_assignment_version`` must be a JSON integer (never a bool or
    a string); any type mismatch is a mismatch."""
    if isinstance(family, SeatFamily):
        want_id, want_version = family.crm_seat_assignment_id, family.crm_seat_assignment_version
    elif isinstance(family, tuple) and len(family) == 2:
        want_id, want_version = family
    else:
        return False
    return bool(
        _text(seat_assignment_id)
        and _uint(seat_assignment_version)
        and _text(want_id)
        and _uint(want_version)
        and seat_assignment_id == want_id
        and seat_assignment_version == want_version
    )


# -- the orchestrator over the live readers -------------------------------------------------------


class InstallationReader(Protocol):
    def by_binding(self, binding_id: str) -> Optional[InstallationView]: ...


class SeatBindingReader(Protocol):
    """The B5 ``SeatServiceClient.binding`` read; raises on unavailable / refusal."""

    def binding(self, *, seat_binding_id: str, installation_id: str, principal_id: str) -> Any: ...


class SeatAdmission:
    """Reads every B3 fact LIVE (no cache) and applies :func:`admit_request`.

    ``generation_of(tenant_id, binding_id)`` is the AIDOCS sealed generation (B6);
    ``trust_healthy(tenant_id, binding_id, sub)`` is the AIOS trust floor (slice 1:
    binding lifecycle + installed trust; no AIOS security-suspension producer exists yet).
    Any reader fault fails closed with a typed, non-disclosing reason."""

    def __init__(
        self,
        *,
        installations: InstallationReader,
        seats: SeatBindingReader,
        generation_of: Callable[[str, str], Optional[str]],
        trust_healthy: Callable[[str, str, str], bool],
    ) -> None:
        self._installations = installations
        self._seats = seats
        self._generation_of = generation_of
        self._trust_healthy = trust_healthy

    def admit(self, family: Optional[SeatFamily], principal_user_id: Any) -> Admission:
        if not isinstance(family, SeatFamily) or principal_user_id != family.connector_sub:
            return _refuse(APP_SEAT_REQUIRED)
        try:
            healthy = self._trust_healthy(family.tenant_id, family.binding_id, family.connector_sub) is True
            sealed = self._generation_of(family.tenant_id, family.binding_id)
            installation = self._installations.by_binding(family.binding_id)
        except Exception:  # noqa: BLE001 -- we could not ask: never admitted
            return _refuse(APP_AUTHORITY_UNAVAILABLE)
        try:
            seat = self._seats.binding(
                seat_binding_id=family.seat_binding_id,
                installation_id=family.installation_id,
                principal_id=family.connector_sub,
            )
        except Exception as exc:  # noqa: BLE001 -- unknown freshness fails closed
            seat = {"ok": False} if is_typed_refusal(exc) else None
        return admit_request(
            family=family,
            principal_user_id=principal_user_id,
            trust_healthy=healthy,
            installation=installation,
            sealed_generation=sealed,
            seat_binding=seat,
        )


# -- the CodeNexus AppInstallationProjection reader ------------------------------------------------

_PROJECTION_COLUMNS = (
    "installation_id, organization_id, binding_id, installation_status, grant_state, ended_at, current_generation"
)
_BY_BINDING_SQL = f'SELECT {_PROJECTION_COLUMNS} FROM "AppInstallationProjection" WHERE binding_id = %s LIMIT 2'


class CodenexusInstallationReader:
    """Reads the CN ``AppInstallationProjection`` view (aidocs_gate_ro) LIVE.

    Exactly one row -> an :class:`InstallationView`; none or more than one ->
    None. A datasource fault PROPAGATES (the caller fails closed as unavailable;
    an outage is never reported as "no installation")."""

    def __init__(self, dsn: str = "", connect: Optional[Callable[[], Any]] = None) -> None:
        self._dsn = dsn
        self._connect = connect

    def _rows(self, sql: str, params: tuple) -> list:
        if self._connect is not None:
            conn = self._connect()
        else:
            if not self._dsn:
                raise RuntimeError("no CodeNexus datasource configured")
            import psycopg2  # lazily: only the real reader needs it

            conn = psycopg2.connect(self._dsn)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return list(cur.fetchall() or [])
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _view(rows: list) -> Optional[InstallationView]:
        if len(rows) != 1:
            return None
        try:
            iid, org, bid, status, state, ended, gen = rows[0]
        except (TypeError, ValueError):
            return None
        if not all(_text(v) for v in (iid, org, bid, status)):
            return None
        return InstallationView(iid, org, bid, status, state or "", ended, gen)

    def by_binding(self, binding_id: str) -> Optional[InstallationView]:
        if not _text(binding_id):
            return None
        return self._view(self._rows(_BY_BINDING_SQL, (binding_id,)))
