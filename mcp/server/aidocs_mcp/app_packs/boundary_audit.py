"""Application-boundary audit metadata and the generic platform error set.

#1096, RFC 0003 §16.1 / §16.2 / §16.3. Retention for the audit kinds lives in
``execution_event_retention`` (the one registry); this module holds what that
registry does not: what a generic audit row may CARRY, the closed kind <->
status matrix (:data:`KIND_STATUS`), and the platform error codes.

Audit metadata is an ALLOWLIST, deny by default (§16.2), and it is typed and
bounded PER FIELD. A key not listed here never reaches the generic audit, and a
listed key only carries a value of that field's exact shape -- digests are the
64-hex form ``jcs_sha256_hex`` emits, codes and statuses come from closed
vocabularies, ids / versions / tool / mode are short tokens with no spaces and
no ``@``. So a credential, a stack trace, business prose, an email address or
an oversized blob placed UNDER an allowed key is dropped too: no allowed field
is a free-prose tunnel. A value that fails its shape is DROPPED, never coerced.

``app_principal_id`` / ``app_entity_id`` (§16.2 "opaque app principal/entity
ids") are deliberately ABSENT: the generic boundary cannot tell an opaque id
from PII by shape, so they wait for #1091's provenance-safe representation.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

__all__ = [
    "APP_PLATFORM_ERROR_CODES",
    "AUDIT_METADATA_ALLOWED_FIELDS",
    "AUDIT_METADATA_STATUSES",
    "AUDIT_METADATA_VALIDATORS",
    "BINDING_STATES",
    "HOST_FILE_PROVIDER_CLASSES",
    "CONTROL_REFUSAL_CODES",
    "KIND_STATUS",
    "LEGACY_DECISION_KINDS",
    "MAX_BATCH_COUNT",
    "MAX_SAFE_INT",
    "audit_metadata",
    "is_platform_error_code",
    "kind_status_admissible",
]

#: §16.3 generic platform errors, at minimum. Distinct semantics stay distinct
#: codes: "names may be normalized, semantics may not be silently collapsed".
#: Exactly the sealed list: the application path has no project refusal and no
#: approval error in v1 (§16.3 / §16.4, §10).
APP_PLATFORM_ERROR_CODES: frozenset[str] = frozenset(
    {
        "app_pack_unavailable",
        "app_capability_required",
        "app_identity_unlinked",
        "app_seat_host_mismatch",
        "app_user_disabled",
        "app_backend_unavailable",
        "app_backend_invalid_response",
        "app_contract_mismatch",
        "app_validation_failed",
        "app_rate_limited",
        "app_mutation_audit_failed",
        "app_outcome_unknown",
        "app_mutation_not_found",
        "app_conversation_context_unavailable",
        "app_internal",
        "host_file_unresolvable",
        "host_file_provider_not_allowed",
        "host_file_redirect_refused",
        "host_file_address_refused",
        "host_file_too_large",
        # #1134 R-5 erratum (WIRE-1134 §10): sealed, never aliased to an existing code.
        # Served on the app connector (B3 admission), at token admission and as a
        # connect-ceremony outcome.
        "app_seat_required",
        "app_binding_disabled",
    }
)

#: HISTORICAL-ACCEPTED decision kinds (r0b2 1f36de37-024): old durable rows keep them and
#: still read / verify (their retention class stays registered); they are NEVER emitted
#: again and are not on the admissible :data:`KIND_STATUS` matrix. legacy -> successor.
LEGACY_DECISION_KINDS: Mapping[str, str] = MappingProxyType(
    {"app_binding_disabled": "app_binding_transitioned_disabled"}
)

#: Closed vocabulary for ``status`` -- one per boundary outcome. Operation words
#: (flipped, confirmed, ...) are never a status: the kind names the act, the
#: status names the outcome, details go in typed metadata.
AUDIT_METADATA_STATUSES: frozenset[str] = frozenset(
    {
        "admitted",
        "refused",
        "completed",
        "applied",
        "outcome_unknown",
        "invalid_response",
        "refreshed",
        "linked",
        "failed",
    }
)

#: The closed kind <-> status matrix (S12-B5). A row whose (kind, status) pair
#: is not here is not admissible. Covers the sealed §16.1 kinds plus the
#: implementation kinds §16.1 leaves to the implementation (spelling per
#: §22.5): the pre-call ``app_call_admitted`` (§9 / §14.1) and the S7
#: control-plane acts. Their retention class lives in the one registry,
#: ``execution_event_retention``. These are AUDIT kinds, never error codes.
KIND_STATUS: Mapping[str, str] = MappingProxyType(
    {
        # §16.1 sealed table
        "app_call_refused": "refused",
        "app_read_completed": "completed",
        "app_mutation_applied": "applied",
        "app_mutation_outcome_unknown": "outcome_unknown",
        "app_backend_invalid_response": "invalid_response",
        "app_entitlement_refreshed": "refreshed",
        "app_identity_linked": "linked",
        # §9 / §14.1 pre-call: the durable authorization-to-egress decision
        "app_call_admitted": "admitted",
        # §16.1 control-plane transitions (S7)
        "app_binding_created": "completed",
        "app_binding_trust_installed": "completed",
        "app_binding_activated": "completed",
        # r0b2 1f36de37-024: every DISABLED transition (admin + S18 ensure_disabled).
        # Formerly ``app_binding_disabled`` (now LEGACY_DECISION_KINDS), renamed so no
        # emitted kind shares a spelling with a §16.3 platform error code.
        "app_binding_transitioned_disabled": "completed",
        "app_binding_contract_upgraded": "completed",
        "app_pairing_offered": "completed",
        "app_pairing_responded": "completed",
        "app_pairing_confirmed": "completed",
        "app_pairing_refused": "refused",
        "app_org_signer_flipped": "completed",
        # #1134: the org's FIRST request-signing key pinned (kid only, never key material)
        "app_org_signer_provisioned": "completed",
        "app_control_plane_refused": "refused",
        # §3.6 R-c / R-h control-plane acts (S11): admin clear of an AppScope /
        # toolspace / tenant freeze, and a tenant policy tightening (§12.5).
        "app_scope_freeze_cleared": "completed",
        "app_tenant_policy_tightened": "completed",
        # §2.5a / §16.1 control-plane act (S6): admin reset of a seat pin.
        "app_seat_pin_reset": "completed",
        # §8.2 / §16.1 control-plane act (S8): the human confirmed the link on
        # /apps/link/authorize and AIDOCS issued the one-time code. The link
        # itself is the sealed ``app_identity_linked`` row, written on redeem.
        "app_link_code_issued": "completed",
        # #1134 B4/B6 (WIRE-1134 §9a): the ONE-TIME binding-authority seal --
        # opaque binding_generation + authority digest + owner baseline.
        "app_binding_authority_sealed": "completed",
        # #1134 B6 (WIRE-1134 §9d): AIDOCS attested a generation switch to CodeNexus.
        "app_generation_attested": "completed",
        # #1134 B2 (WIRE-1134 §3/§7, C11): the connect-ceremony authority transitions.
        # Each row is written only AFTER its effect is durable (truth before green).
        "app_connect_authorize_created": "completed",
        "app_connect_asserted": "completed",
        "app_seat_admitted": "completed",
        "app_link_finalized": "completed",
        "app_seat_confirmed": "completed",
        "app_connect_code_released": "completed",
        "app_connect_refused": "refused",
        "app_connect_abandoned": "completed",
        "app_link_abandon_sent": "completed",
        # #1134 fix 6 (WIRE-1134 §5): CN freed the seat for a CRM seat_revoke.
        "app_seat_revoked": "completed",
        # #1134 slice 2 (CAT wire §1(d)): the CRM owner_epoch_update moved the owner authority.
        "app_owner_epoch_updated": "completed",
    }
)

#: The ONE spelling the SEALED RFC itself puts in both tables: ``app_backend_invalid_response``
#: is a sealed §16.1 audit kind AND a sealed §16.3 platform code (RFC 0003 v4.6.1). It predates
#: #1134 and is not an implementation choice; every implementation-chosen kind is disjoint.
SEALED_KIND_CODE_SPELLINGS: frozenset[str] = frozenset({"app_backend_invalid_response"})

if (
    set(KIND_STATUS) & APP_PLATFORM_ERROR_CODES != SEALED_KIND_CODE_SPELLINGS
    or set(KIND_STATUS) & set(LEGACY_DECISION_KINDS)
):  # pragma: no cover
    raise RuntimeError("an emitted audit kind collides with a platform error code or a legacy kind")
if not set(KIND_STATUS.values()) <= AUDIT_METADATA_STATUSES:  # pragma: no cover
    raise RuntimeError("KIND_STATUS uses a status outside AUDIT_METADATA_STATUSES")

MAX_BATCH_COUNT = 100_000
#: Upper bound for the control-plane counters: the largest integer every
#: RFC 8785 / I-JSON consumer represents exactly (2**53 - 1).
MAX_SAFE_INT = 2**53 - 1

#: CLOSED enum of the AIDOCS control-plane refusal reasons that are NOT §16.3
#: platform codes: ``app_pairing_invalid`` (wire §1.5) plus every LOCAL code the
#: S7 control plane (binding_store / pairing / control_plane) can raise. Carried
#: as ``control_refusal_code``; ``refusal_code`` stays §16.3-only.
CONTROL_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "app_pairing_invalid",
        "forbidden",
        "namespace_collision",
        "namespace_reserved",
        "tool_name_collision",
        "active_binding_exists",
        "not_paired",
        "pack_unknown",
        "origin_invalid",
        "version_conflict",
        "invalid_transition",
        "epoch_regressed",
        "keyset_version_not_bumped",
        "revocation_epoch_not_bumped",
        "flip_no_next_key",
        "flip_bindings_unprovisioned",
        "audit_unavailable",
        "key_reuse",
        "trust_invalid",
        "control_plane_refused",
        "key_invalid",
        "entitlement_keyset_invalid",
        "binding_unknown",
        "binding_unavailable",
        "binding_ambiguous",
        "binding_id_collision",
        "invalid",
        # S11 R-h: a tenant policy that would loosen a §12.5 ceiling or the
        # tenant's current tightening.
        "policy_loosening",
        # S8 (wire §1.5, ruling O-1): the AIDOCS-only link-flow refusals.
        "app_link_code_invalid",
        "app_control_assertion_invalid",
        # In-place contract upgrade (operator 2026-10-01).
        "contract_unchanged",
        "contract_incompatible",
        "contract_unsealed",
        "upgrade_proof_invalid",
        "upgrade_application_refused",
        # #1134 B4/B6 seal (WIRE-1134 §9a, §10) and the §9d attestation.
        "app_binding_already_sealed",
        "binding_seal_stale",
        "app_seal_rate_limited",
        "app_seal_unavailable",
        "generation_attestation_refused",
        # #1134 Phase B: the externally served connect / seat control-plane refusals
        # (WIRE-1134 §3/§4/§7/§10) and the CN outcomes relayed to the CRM result page.
        "app_binding_generation_mismatch",
        "app_connect_request_invalid",
        "app_owner_epoch_stale",
        "app_connect_conflict",
        "app_seat_service_unavailable",
        "app_link_retired",
        "seats_exhausted",
        "app_seat_conflict",
        # #1134 fix 6: seat_revoke (WIRE-1134 §5).
        "app_event_conflict",
        "app_seat_identity_conflict",
        "app_seat_revoke_refused",
    }
)

if CONTROL_REFUSAL_CODES & APP_PLATFORM_ERROR_CODES:  # pragma: no cover
    raise RuntimeError("a control-plane refusal code collides with a §16.3 platform code")

#: CLOSED vocabulary of host-file provider classes (r0b2): which egress authority
#: an upload's host-file fetch ran under. Mirrors host_file_ingress's provenance
#: constants (a test pins the equality); never a URL or host.
HOST_FILE_PROVIDER_CLASSES: frozenset[str] = frozenset({"generic", "chatgpt_host_file"})

#: CLOSED vocabulary of #1134 connect-ceremony outcomes (CRM /connect/result codes +
#: the AIDOCS abandon reasons, WIRE-1134 §7.3/§7.5).
CONNECT_OUTCOMES: frozenset[str] = frozenset(
    {
        "seats_exhausted", "app_seat_required", "app_binding_disabled", "app_identity_unlinked",
        "app_seat_conflict", "finalize_refused", "expired",
    }
)

#: CLOSED enum of AIDOCS application-binding states (S7 ``BindingState``).
BINDING_STATES: frozenset[str] = frozenset({"pending_pairing", "paired", "active", "disabled"})

# fullmatch + explicit ASCII classes: no trailing-newline or Unicode-digit slack.
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KID = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_GENERATION = re.compile(r"gen_[A-Za-z0-9_-]{43}")
_EVENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_VERSION = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,3}(?:-[0-9A-Za-z.]{1,32})?")
#: CLOSED allowlist of host ``_meta`` key NAMES a refusal row may list (r0b2: a
#: host-chosen name is never written). Any other name is only counted.
HOST_META_KEY_NAMES: frozenset[str] = frozenset(
    {
        "openai/subject", "openai/session", "openai/organization", "openai/locale",
        "openai/userAgent", "openai/userLocation", "aidocs/hostSession", "aidocs/hostKind",
    }
)


def _host_meta_keys(value: Any) -> bool:
    """A sorted, duplicate-free, comma-joined subset of :data:`HOST_META_KEY_NAMES`."""
    if type(value) is not str or not value:
        return False
    names = value.split(",")
    return all(n in HOST_META_KEY_NAMES for n in names) and names == sorted(set(names))


#: CLOSED vocabulary: why the seat pin refused (diagnostic only; the caller still
#: sees the bare ``app_seat_host_mismatch``).
SEAT_PIN_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "meta_not_mapping", "subject_absent", "subject_not_string", "subject_empty", "subject_padded",
        "subject_too_long", "subject_control_chars", "seat_pinned_to_other_subject",
        "subject_pinned_to_other_seat", "seat_and_subject_pinned_elsewhere", "mismatch_direction_unknown",
    }
)


def _matches(pattern: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _member_of(vocabulary: frozenset[str]) -> Callable[[Any], bool]:
    return lambda value: type(value) is str and value in vocabulary


def _bounded_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_BATCH_COUNT


def _int_in(low: int, high: int) -> Callable[[Any], bool]:
    # ``type(...) is int``: a bool is never an int here.
    return lambda value: type(value) is int and low <= value <= high


def _boolean(value: Any) -> bool:
    return type(value) is bool


_digest = _matches(_DIGEST)
_id = _matches(_ID)
_name = _matches(_NAME)
_version = _matches(_VERSION)
_platform_code = _member_of(APP_PLATFORM_ERROR_CODES)

#: §16.2 "May include", each with its shape. The ONLY source of the allowlist.
AUDIT_METADATA_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "pack_id": _id,
    "binding_id": _id,
    # S12-B2: bounded opaque id -- row evidence only, NEVER a chain key (§3.6 R-d)
    "connector_id": _id,
    "tool": _name,
    "mode": _name,
    # tenant_id / toolspace_id / sub ids; session / actor ids as provenance
    "tenant_id": _id,
    "toolspace_id": _id,
    "session_id": _id,
    "sub": _id,
    "actor_id": _id,
    "receipt_id": _id,
    # versions
    "pack_version": _version,
    "contract_version": _version,
    # status/refusal code
    "status": _member_of(AUDIT_METADATA_STATUSES),
    "refusal_code": _platform_code,
    "error_code": _platform_code,
    # digests: exactly what jcs_sha256_hex emits
    "contract_digest": _digest,
    "grants_digest": _digest,
    "response_digest": _digest,
    "input_digest": _digest,
    # batch counts
    "batch_count": _bounded_count,
    # typed control-plane evidence (reviewer ruling 7db9a488): closed and
    # typed, never free text. The row status stays the KIND_STATUS value.
    "control_refusal_code": _member_of(CONTROL_REFUSAL_CODES),
    "binding_version": _int_in(1, MAX_SAFE_INT),
    "binding_state": _member_of(BINDING_STATES),
    "entitlement_keyset_version": _int_in(1, MAX_SAFE_INT),
    "entitlement_revocation_epoch": _int_in(0, MAX_SAFE_INT),
    "org_key_rotation": _boolean,
    # #1132 in-place contract upgrade evidence (typed, never prose)
    "from_digest": _digest,
    "to_digest": _digest,
    "proof_jti": _id,
    # host-file egress authority of an upload (closed; r0b2 f43e9a7a0 review)
    "provider_class": _member_of(HOST_FILE_PROVIDER_CLASSES),
    # #1134: the PUBLIC kid of a provisioned org request signer (wire kid alphabet)
    "signer_kid": _matches(_KID),
    # #1134 B4/B6 seal + §9d attestation evidence (WIRE-1134 §10). The opaque
    # generation is AIDOCS-random (``gen_`` + 43 base64url); never a CRM user id.
    "binding_generation": _matches(_GENERATION),
    "authority_sha256": _digest,
    "owner_epoch": _int_in(1, MAX_SAFE_INT),
    "event_id": _matches(_EVENT_ID),
    "installation_id": _id,
    "attestation_kid": _matches(_KID),
    # #1134 B2 connect-chain evidence (C11): ids, digests, integers; never a raw CRM
    # user id (R-11), a code, a token, a cookie or the raw authz_request_id.
    "authz_request_sha256": _digest,
    "organization_id": _id,
    "seat_binding_id": _id,
    "crm_seat_assignment_id": _id,
    "crm_seat_assignment_version": _int_in(0, MAX_SAFE_INT),
    "link_state_id": _digest,
    "connect_outcome": _member_of(CONNECT_OUTCOMES),
    "issue_attempt": _int_in(1, MAX_SAFE_INT),
    # #1134 fix 6: the seat_revoke reason (CAT closed enum) and CN's terminal answer.
    "revoke_reason": _member_of(frozenset({"unseated", "user_disabled", "owner_revoke_all"})),
    "seat_revoke_state": _member_of(frozenset({"revoked", "no_active_binding"})),
    # seat-pin refusal diagnostics: the reason (closed), the ALLOWLISTED host _meta
    # key names present, and counts (total; unlisted openai/*; unlisted other).
    # Never a value and never a non-allowlisted name.
    "seat_pin_refusal_reason": _member_of(SEAT_PIN_REFUSAL_REASONS),
    "host_meta_keys": _host_meta_keys,
    "host_meta_key_count": _bounded_count,
    "host_meta_unlisted_openai": _bounded_count,
    "host_meta_unlisted_other": _bounded_count,
}

AUDIT_METADATA_ALLOWED_FIELDS: frozenset[str] = frozenset(AUDIT_METADATA_VALIDATORS)


def audit_metadata(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the entries whose key is allowed AND whose value has that
    field's exact shape. Everything else is dropped, never coerced."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        validator = AUDIT_METADATA_VALIDATORS.get(key) if type(key) is str else None
        if validator is not None and validator(value):
            out[key] = value
    return out


def is_platform_error_code(code: str) -> bool:
    return code in APP_PLATFORM_ERROR_CODES


def kind_status_admissible(kind: str, status: str) -> bool:
    """True only for a (kind, status) pair on the closed :data:`KIND_STATUS`
    matrix. An unknown kind, or a known kind with any other status, is not."""
    return type(kind) is str and type(status) is str and KIND_STATUS.get(kind) == status
