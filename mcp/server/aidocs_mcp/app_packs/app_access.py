"""Framework-independent S18 App-access control protocol.

CodeNexus platform calls and the pre-trust application pairing redemption are
structurally different credentials:

* platform operations use Authorization: CodeNexus-Platform <assertion>.
  The assertion authenticates the CodeNexus platform owner and binds the exact
  CodeNexus ``organization_id``, action and raw body. AIDOCS alone resolves the
  organization to its tenant (#1134; ``bind`` issues the mapping, every other
  act requires it). Tenant selection never comes from a body or a header.
* pairing redemption is the bootstrap application path. Before trust exists it
  has no org/platform credential; the one-time pairing secret plus application
  PoP are the ceremony's authentication. The transcript tenant is only a lookup
  hint to find that tenant's offer store, then PairingService verifies every
  transcript fact and proof.

Platform order (r0b2): server secret (503) -> scheme -> full credential
verification (header, MAC, claims, body digest, lifetime) -> organization
lookup -> body JSON shape + §5.8 rate (inside the verifier's ``before_replay``,
so neither spends the jti) -> mapping issue (bind) -> replay consume LAST ->
the S7 handler. Wire: every credential refusal is ONE non-disclosing 401; a
validly signed non-JSON body is 400; server faults are 503 with an EXPLICIT
retryable flag (only known transient infrastructure is retryable).

This module owns pre-state §5.8 rate admission. The HTTP transport only supplies
the trusted edge axis and exact raw request facts.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from .binding_store import CREDENTIAL_PAIRING_BOOTSTRAP, ControlPlaneActor
from .pairing import PairingDependencyUnavailable
from .pairing_rate import PairingRateLimiter
from .platform_control import (
    BODY_INVALID,
    MALFORMED,
    PLATFORM_CONTROL_RETRYABLE_CODES,
    PLATFORM_CONTROL_SERVER_FAULT_CODES,
    OrganizationTenantResolver,
    PlatformControlRefused,
    PlatformReplayStore,
    require_platform_secret,
    verify_platform_control_target,
)

__all__ = [
    "PLATFORM_AUTH_SCHEME",
    "AppAccessControl",
    "AppAccessResponse",
]

PLATFORM_AUTH_SCHEME = "CodeNexus-Platform"
_MAX_BODY_BYTES = 128 * 1024
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# RFC 9110 credentials: scheme, ONE OR MORE ASCII SP, then the visible-ASCII
# credential. No tab, newline, other control whitespace or edge whitespace.
_AUTHORIZATION_RE = re.compile(r"([!-~]+) +([!-~]+)")
_OPERATIONS = {
    "bind": ("bind", "ensure_binding"),
    "pair-offer": ("pair-offer", "issue_pairing_offer"),
    "pair-confirm": ("pair-confirm", "confirm_pairing"),
    # Read-only: same verifier, organization resolution, jti and replay; no state change.
    "pair-status": ("pair-status", "pairing_status"),
    "activate": ("activate", "ensure_active_binding"),
    "disable": ("disable", "ensure_disabled_binding"),
    # #1134 B6, read-only: the PUBLIC generation-attestation JWKS CodeNexus pins
    # (TOFU over this authenticated channel). Not a tenant handler.
    "attestation-keys": ("attestation-keys", "attestation_keys"),
    # #1134 B6 (WIRE §9d): sign the generation-switch attestation for CodeNexus.
    "generation-attest": ("generation-attest", "generation_attest"),
}
#: The only JWK members ever published (public EC P-256; never ``d``).
_PUBLIC_JWK_MEMBERS = ("kty", "crv", "kid", "x", "y")

_CONTROL_CODE = "control_plane_refused"
_PAIRING_CODE = "app_pairing_invalid"
_RATE_CODE = "app_pairing_rate_limited"
_INTERNAL_CODE = "app_internal"

_MESSAGES = {
    _CONTROL_CODE: "The control request was refused.",
    _PAIRING_CODE: "The pairing request was refused.",
    _RATE_CODE: "Too many pairing attempts.",
    _INTERNAL_CODE: "The control service is unavailable.",
}


@dataclass(frozen=True)
class AppAccessResponse:
    status: int
    body: dict[str, Any]
    headers: Mapping[str, str]
    # WIRE-1134 §10.1: the typed internal platform-control reason, for audit
    # and the shared vectors ONLY. It is never serialized onto the wire.
    refusal_code: str = ""


class _RateRefused(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("pairing rate limited")
        self.retry_after = max(1, int(retry_after))


def _error(
    status: int,
    code: str,
    *,
    retryable: bool,
    retry_after: int = 0,
    refusal_code: str = "",
) -> AppAccessResponse:
    """``retryable`` is decided by the CALL SITE, never derived from the status."""
    headers: dict[str, str] = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    return AppAccessResponse(
        status,
        {
            "ok": False,
            "error": {
                "code": code,
                "message": _MESSAGES.get(code, _MESSAGES[_CONTROL_CODE]),
                "retryable": retryable is True,
            },
        },
        headers,
        refusal_code,
    )


def _platform_refusal(exc: PlatformControlRefused) -> AppAccessResponse:
    """401 for every credential refusal; 400 for an authenticated non-JSON body; 503 for server faults."""
    if exc.code in PLATFORM_CONTROL_SERVER_FAULT_CODES:
        return _error(
            503,
            _INTERNAL_CODE,
            retryable=exc.code in PLATFORM_CONTROL_RETRYABLE_CODES,
            refusal_code=exc.code,
        )
    if exc.code == BODY_INVALID:
        return _error(400, _CONTROL_CODE, retryable=False, refusal_code=exc.code)
    return _error(401, _CONTROL_CODE, retryable=False, refusal_code=exc.code)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON member")
        out[key] = value
    return out


def _constant(_name: str) -> Any:
    raise ValueError("non-finite JSON number")


def _json_body(body: Any) -> dict[str, Any]:
    if type(body) is not bytes or not body or len(body) > _MAX_BODY_BYTES:
        raise ValueError("invalid JSON body")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid JSON body") from exc
    if type(value) is not dict:
        raise ValueError("invalid JSON body")
    return value


def _platform_token(authorization: Any) -> str:
    if type(authorization) is not str:
        raise PlatformControlRefused("platform control authorization is invalid", code=MALFORMED)
    match = _AUTHORIZATION_RE.fullmatch(authorization)
    if match is None or match.group(1).lower() != PLATFORM_AUTH_SCHEME.lower():
        raise PlatformControlRefused("platform control authorization is invalid", code=MALFORMED)
    return match.group(2)


class AppAccessControl:
    """One S18 control service over tenant-routed existing S7 handlers."""

    def __init__(
        self,
        *,
        secret: str,
        audience: str,
        organizations: OrganizationTenantResolver,
        control_plane_for_tenant: Callable[[str], Any],
        bootstrap_control_plane_for_tenant: Callable[[str], Any],
        replay_for_tenant: Callable[[str], Optional[PlatformReplayStore]],
        rate_limiter: PairingRateLimiter,
        attestation_keys: Optional[Callable[[], Mapping[str, Any]]] = None,
        generation_attest: Optional[Callable[..., str]] = None,
    ) -> None:
        self._attestation_keys = attestation_keys
        # ``(actor, organization_id=, request=, now=) -> compact JWS``; see
        # generation_attestation.generation_attest_handler.
        self._generation_attest = generation_attest
        self._secret = secret
        self._audience = audience
        self._organizations = organizations
        self._control_plane = control_plane_for_tenant
        # The redeem tenant id is an UNAUTHENTICATED hint: its resolver may only
        # select an existing authority, never create one (r0b2 e8697991).
        self._bootstrap_control_plane = bootstrap_control_plane_for_tenant
        self._replay = replay_for_tenant
        self._rate = rate_limiter

    def _rate_pair_issue(self, actor: ControlPlaneActor, client_axis: Any) -> None:
        ok, retry = self._rate.admit(
            "issue",
            (("actor", actor.user_id), client_axis),
        )
        if not ok:
            raise _RateRefused(retry)

    def platform(
        self,
        operation: str,
        authorization: Any,
        body: bytes,
        *,
        now: int,
        client_axis: Any,
    ) -> AppAccessResponse:
        spec = _OPERATIONS.get(operation)
        if spec is None:
            return _error(404, _CONTROL_CODE, retryable=False)
        expected_act, method = spec
        try:
            # A missing/weak secret is a deployment fault: 503 whatever arrived.
            require_platform_secret(self._secret)
            token = _platform_token(authorization)
        except PlatformControlRefused as exc:
            return _platform_refusal(exc)

        parsed: dict[str, Any] = {}

        def before_replay(actor: ControlPlaneActor) -> None:
            # Authenticated already (MAC + claims + body digest): only now is the
            # body's JSON shape examined, and a refusal here spends nothing.
            try:
                parsed.update(_json_body(body))
            except ValueError as exc:
                raise PlatformControlRefused("request body is not a JSON object", code=BODY_INVALID) from exc
            if operation == "pair-offer":
                self._rate_pair_issue(actor, client_axis)

        try:
            verified = verify_platform_control_target(
                token,
                secret=self._secret,
                expected_audience=self._audience,
                expected_action=expected_act,
                body=body,
                now=now,
                organizations=self._organizations,
                replay_for_tenant=self._replay,
                before_replay=before_replay,
            )
        except _RateRefused as exc:
            return _error(429, _RATE_CODE, retryable=True, retry_after=exc.retry_after)
        except PlatformControlRefused as exc:
            return _platform_refusal(exc)
        except Exception:
            return _error(503, _INTERNAL_CODE, retryable=False)
        actor = verified.actor
        if method == "attestation_keys":
            return self._public_attestation_keys()
        if method == "generation_attest":
            return self._attest(actor, verified.organization_id, parsed, now)

        try:
            handlers = self._control_plane(actor.authority_tenant_id)
            fn = getattr(handlers, method)
            if method == "pairing_status":
                # Called by name (not via fn) so the read-only handler has a visible call site.
                out = handlers.pairing_status(actor, parsed, now=now)
            elif method in {"issue_pairing_offer", "confirm_pairing"}:
                out = fn(actor, parsed, now=now)
            else:
                out = fn(actor, parsed)
        except PairingDependencyUnavailable:
            # A typed, TRANSIENT dependency outage (e.g. key custody while the
            # org signer is provisioned): nothing was pinned or offered; retry.
            return _error(503, _INTERNAL_CODE, retryable=True)
        except Exception:
            return _error(503, _INTERNAL_CODE, retryable=False)
        if not isinstance(out, Mapping):
            return _error(503, _INTERNAL_CODE, retryable=False)
        result = dict(out)
        if operation == "bind" and result.get("ok") is True:
            # The canonical bind receipt (r0b2): the established PAIR, closed
            # fields, and only when the binding really lives in that tenant.
            binding = result.get("binding")
            if not isinstance(binding, Mapping) or binding.get("tenant_id") != actor.authority_tenant_id:
                return _error(503, _INTERNAL_CODE, retryable=False)
            result["authority_mapping"] = {
                "organization_id": verified.organization_id,
                "aidocs_tenant_id": actor.authority_tenant_id,
            }
        return AppAccessResponse(
            200 if result.get("ok") is True else 400,
            result,
            {"Cache-Control": "no-store"},
        )

    def _attest(self, actor: ControlPlaneActor, organization_id: str, request: dict, now: int) -> AppAccessResponse:
        """``{"ok": true, "attestation": <JWS>}``; a refusal is 400 ``control_plane_refused``
        (authenticated, non-disclosing); a key / audit fault is 503 retryable."""
        from .generation_attestation import AttestationRefused, AttestationUnavailable

        if self._generation_attest is None:
            return _error(503, _INTERNAL_CODE, retryable=False)
        try:
            token = self._generation_attest(actor, organization_id=organization_id, request=request, now=now)
        except AttestationRefused:
            return _error(400, _CONTROL_CODE, retryable=False)
        except AttestationUnavailable:
            return _error(503, _INTERNAL_CODE, retryable=True)
        except Exception:  # noqa: BLE001
            return _error(503, _INTERNAL_CODE, retryable=False)
        if type(token) is not str or not token:
            return _error(503, _INTERNAL_CODE, retryable=False)
        return AppAccessResponse(200, {"ok": True, "attestation": token}, {"Cache-Control": "no-store"})

    def _public_attestation_keys(self) -> AppAccessResponse:
        """``{"ok": true, "keys": [...]}``: public members only, whatever the source holds."""
        if self._attestation_keys is None:
            return _error(503, _INTERNAL_CODE, retryable=False)
        try:
            jwks = self._attestation_keys()
            keys = []
            for k in jwks["keys"]:
                pub = {name: k[name] for name in _PUBLIC_JWK_MEMBERS}
                if not all(type(v) is str and v for v in pub.values()):
                    raise ValueError("jwk")
                keys.append(pub)
            if not keys:
                raise ValueError("no keys")
        except Exception:  # noqa: BLE001 -- key provisioning / custody fault: retry later
            return _error(503, _INTERNAL_CODE, retryable=True)
        return AppAccessResponse(200, {"ok": True, "keys": keys}, {"Cache-Control": "no-store"})

    def redeem(
        self,
        body: bytes,
        *,
        now: int,
        client_axis: Any,
    ) -> AppAccessResponse:
        try:
            ok, retry = self._rate.admit("redeem", (client_axis,))
        except Exception:
            return _error(503, _INTERNAL_CODE, retryable=False)
        if not ok:
            return _error(429, _RATE_CODE, retryable=True, retry_after=retry)

        try:
            parsed = _json_body(body)
            transcript = parsed.get("transcript")
            if not isinstance(transcript, Mapping):
                raise ValueError("missing transcript")
            tenant_id = transcript.get("tenant_id")
            if type(tenant_id) is not str or not _TENANT_RE.fullmatch(tenant_id):
                raise ValueError("invalid tenant hint")
            handlers = self._bootstrap_control_plane(tenant_id)
        except Exception:
            return _error(400, _PAIRING_CODE, retryable=False)

        actor = ControlPlaneActor(
            user_id="application-bootstrap",
            credential_class=CREDENTIAL_PAIRING_BOOTSTRAP,
        )
        try:
            out = handlers.redeem_pairing_response(actor, parsed, now=now)
        except Exception:
            return _error(400, _PAIRING_CODE, retryable=False)
        if not isinstance(out, Mapping):
            return _error(400, _PAIRING_CODE, retryable=False)
        if out.get("ok") is not True:
            error = out.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            if code == "app_contract_mismatch":
                # Wire §1.5/O-1 deliberately preserves this one real mismatch
                # while every other bootstrap refusal stays non-disclosing.
                return AppAccessResponse(400, dict(out), {"Cache-Control": "no-store"})
            return _error(400, _PAIRING_CODE, retryable=False)
        return AppAccessResponse(200, dict(out), {"Cache-Control": "no-store"})
