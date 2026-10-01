"""AIDOCS control-plane handlers for the AIDOCS application binding (S7).

A thin, TRANSPORT-FREE layer over :class:`BindingStore` and
:class:`PairingService`. The HTTP routes (separate control-plane routes, build
plan R6; never a ``/v1/mcp`` tool, hidden or advertised, RFC §5.1) are a later
wiring step (S18). Each handler takes an already-authenticated
:class:`ControlPlaneActor` and a JSON body and returns the wire §1.5 envelope
``{"ok": true, ...}`` or ``{"ok": false, "error": {code, message, retryable}}``.

* The admin-dashboard credential class is checked FIRST, before any body is
  read (R6: an MCP connector / OAuth credential is refused by construction).
* Pairing refusals use the sealed codes ``app_pairing_invalid`` /
  ``app_contract_mismatch`` (wire §1.5, O-1).
* [FLAGGED] Binding-act refusals (bind / activate / disable / flip) have no
  sealed code. They use the LOCAL code :data:`LOCAL_REFUSAL_CODE`, which is
  NOT an ``app_*`` platform code and is never added to
  ``boundary_audit.APP_PLATFORM_ERROR_CODES``.
* Messages are fixed and non-disclosing: they never say which check failed or
  whether a binding, offer or tenant exists (RFC §4.1).
* Reviewer ruling (feac1835-1f7, flag 7): §5.8 pairing issuance/redemption
  rate limits are NOT built here, so no production pairing route may be wired
  to these handlers until they land. The stores behind them are in-memory
  (a production HOLD).
"""
from __future__ import annotations

from typing import Any, Mapping

from .binding_store import (
    CREDENTIAL_ADMIN_DASHBOARD,
    BindingRecord,
    BindingRefused,
    BindingStore,
    ControlPlaneActor,
)
from .pairing import CODE_CONTRACT_MISMATCH, CODE_PAIRING_INVALID, PairingRefused, PairingService

__all__ = ["LOCAL_REFUSAL_CODE", "ControlPlaneHandlers"]

LOCAL_REFUSAL_CODE = "control_plane_refused"  # LOCAL, unsealed (reviewer: accepted, stays local)
_CREATE_MEMBERS = frozenset({"tenant_id", "pack_id", "contract_digest", "application_origin"})
_MESSAGES = {
    LOCAL_REFUSAL_CODE: "The control-plane request was refused.",
    CODE_PAIRING_INVALID: "The pairing request was refused.",
    CODE_CONTRACT_MISMATCH: "The application does not serve the pinned contract.",
}


def _error(code: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": _MESSAGES[code], "retryable": False}}


def _admin_credential(actor: Any) -> bool:
    return isinstance(actor, ControlPlaneActor) and actor.credential_class == CREDENTIAL_ADMIN_DASHBOARD


def _binding_view(rec: BindingRecord) -> dict:
    """Public facts only; trust shows kids and fingerprints, never key coordinates."""
    view = {
        "binding_id": rec.binding_id,
        "tenant_id": rec.tenant_id,
        "toolspace_id": rec.toolspace_id,
        "pack_id": rec.pack_id,
        "contract_digest": rec.contract_digest,
        "api_version": rec.api_version,
        "application_origin": rec.application_origin,
        "audience": rec.audience,
        "state": rec.state.value,
        "binding_version": rec.binding_version,
        "paired": rec.trust is not None,
    }
    if rec.trust is not None:
        view["aidocs_kid"] = rec.trust.aidocs_kid
        view["binding_control_kid"] = rec.trust.binding_control_key["kid"]
        view["entitlement_kids"] = [k["kid"] for k in rec.trust.entitlement_keys]
        view["entitlement_revocation_epoch"] = rec.trust.entitlement_revocation_epoch
    return view


def _str(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if type(value) is not str or not value:
        raise BindingRefused("invalid")
    return value


_UPGRADE_MEMBERS = frozenset({"binding_id", "to_digest"})


class ControlPlaneHandlers:
    def __init__(self, store: BindingStore, pairing: PairingService, *, upgrader: Any = None,
                 previewer: Any = None) -> None:
        self._store = store
        self._pairing = pairing
        # #1132: ``upgrader(actor, binding_id, to_digest) -> BindingRecord`` (the
        # runtime's transport-bearing upgrade) and ``previewer(...) -> dict`` (the
        # F -> T + tool/mode diff, no application contact); None = not offered.
        self._upgrader = upgrader
        self._previewer = previewer

    def preview_contract_upgrade(self, actor: ControlPlaneActor, body: Any) -> dict:
        """#1132: what an upgrade would change. Body exactly ``{binding_id, to_digest}``."""
        if not _admin_credential(actor) or not isinstance(body, Mapping) or self._previewer is None:
            return _error(LOCAL_REFUSAL_CODE)
        try:
            if set(body) != _UPGRADE_MEMBERS:
                raise BindingRefused("invalid")
            preview = self._previewer(actor, _str(body, "binding_id"), _str(body, "to_digest"))
        except BindingRefused:
            return _error(LOCAL_REFUSAL_CODE)
        return {"ok": True, "preview": dict(preview)}

    def upgrade_contract(self, actor: ControlPlaneActor, body: Any) -> dict:
        """#1132 in-place contract upgrade. Body exactly ``{binding_id, to_digest}``.
        A refusal stays non-disclosing about AIDOCS checks; only the APPLICATION's
        own declared refusal code is surfaced (``error.application_code``), so the
        operator can tell e.g. "the owner has not approved yet"."""
        if not _admin_credential(actor) or not isinstance(body, Mapping) or self._upgrader is None:
            return _error(LOCAL_REFUSAL_CODE)
        try:
            if set(body) != _UPGRADE_MEMBERS:
                raise BindingRefused("invalid")
            rec = self._upgrader(actor, _str(body, "binding_id"), _str(body, "to_digest"))
        except BindingRefused as exc:
            err = _error(LOCAL_REFUSAL_CODE)
            app_code = getattr(exc, "application_code", None)
            if app_code:
                err["error"]["application_code"] = app_code
            return err
        return {"ok": True, "binding": _binding_view(rec)}

    # -- binding acts ---------------------------------------------------------------

    def _binding_act(self, actor: Any, body: Any, fn) -> dict:
        if not _admin_credential(actor):
            return _error(LOCAL_REFUSAL_CODE)
        if not isinstance(body, Mapping):
            return _error(LOCAL_REFUSAL_CODE)
        try:
            return {"ok": True, "binding": _binding_view(fn(body))}
        except BindingRefused:
            return _error(LOCAL_REFUSAL_CODE)

    def create_binding(self, actor: ControlPlaneActor, body: Any) -> dict:
        """The body is exactly these members. ``toolspace_id`` is NOT one: it is
        derived from the pack namespace (S7-B1), so a body naming it is refused."""

        def run(b: Mapping[str, Any]) -> BindingRecord:
            if set(b) != _CREATE_MEMBERS:
                raise BindingRefused("invalid")
            return self._store.create_binding(
                actor,
                tenant_id=_str(b, "tenant_id"),
                pack_id=_str(b, "pack_id"),
                contract_digest=_str(b, "contract_digest"),
                application_origin=_str(b, "application_origin"),
            )

        return self._binding_act(actor, body, run)

    def activate_binding(self, actor: ControlPlaneActor, body: Any) -> dict:
        return self._binding_act(actor, body, lambda b: self._store.activate(actor, _str(b, "binding_id")))

    def disable_binding(self, actor: ControlPlaneActor, body: Any) -> dict:
        return self._binding_act(actor, body, lambda b: self._store.disable(actor, _str(b, "binding_id")))

    # -- pairing ---------------------------------------------------------------------

    def _pairing_act(self, actor: Any, body: Any, fn) -> dict:
        if not _admin_credential(actor) or not isinstance(body, Mapping):
            return _error(CODE_PAIRING_INVALID)
        try:
            return fn(body)
        except PairingRefused as exc:
            return _error(exc.code if exc.code in _MESSAGES else CODE_PAIRING_INVALID)
        except BindingRefused:
            return _error(CODE_PAIRING_INVALID)

    def issue_pairing_offer(self, actor: ControlPlaneActor, body: Any, *, now: int) -> dict:
        """``org_key_rotation`` (optional, strictly boolean) selects the §5.8
        rotation re-pair through the org's NEXT key (S7-B4)."""

        def run(b: Mapping[str, Any]) -> dict:
            rotation = b.get("org_key_rotation", False)
            if type(rotation) is not bool:
                raise PairingRefused(detail="org_key_rotation must be a boolean")
            offer = self._pairing.issue_offer(actor, _str(b, "binding_id"), now=now, org_key_rotation=rotation)
            return {"ok": True, "offer": offer}

        return self._pairing_act(actor, body, run)

    def redeem_pairing_response(self, actor: ControlPlaneActor, body: Any, *, now: int) -> dict:
        def run(b: Mapping[str, Any]) -> dict:
            r = self._pairing.redeem_response(actor, b, now=now)
            return {
                "ok": True,
                "review": {
                    "review_id": r.review_id,
                    "tenant_id": r.tenant_id,
                    "binding_id": r.binding_id,
                    "pack_id": r.pack_id,
                    "toolspace_id": r.toolspace_id,
                    "application_origin": r.application_origin,
                    "audience": r.audience,
                    "aidocs_fingerprint": r.aidocs_fingerprint,
                    "application_fingerprint": r.application_fingerprint,
                    "entitlement_fingerprints": list(r.entitlement_fingerprints),
                    "comparison_code": r.comparison_code,
                },
            }

        return self._pairing_act(actor, body, run)

    def confirm_pairing(self, actor: ControlPlaneActor, body: Any, *, now: int) -> dict:
        return self._pairing_act(
            actor, body, lambda b: {"ok": True, "confirmation": self._pairing.confirm(actor, _str(b, "review_id"), now=now)}
        )
