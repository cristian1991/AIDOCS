"""Application-boundary audit metadata and the generic platform error set.

#1096, RFC 0003 §16.2 / §16.3. Retention for the §16.1 event kinds lives in
``execution_event_retention`` (the one registry); this module holds the two
things that registry does not: what a generic audit row may CARRY, and the
platform error codes.

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
from typing import Any

__all__ = [
    "APP_PLATFORM_ERROR_CODES",
    "AUDIT_METADATA_ALLOWED_FIELDS",
    "AUDIT_METADATA_STATUSES",
    "AUDIT_METADATA_VALIDATORS",
    "MAX_BATCH_COUNT",
    "audit_metadata",
    "is_platform_error_code",
]

#: §16.3 generic platform errors, at minimum. Distinct semantics stay distinct
#: codes: "names may be normalized, semantics may not be silently collapsed".
APP_PLATFORM_ERROR_CODES: frozenset[str] = frozenset(
    {
        "app_pack_unavailable",
        "app_project_not_bound",
        "app_capability_required",
        "app_identity_unlinked",
        "app_user_disabled",
        "app_backend_unavailable",
        "app_backend_invalid_response",
        "app_contract_mismatch",
        "app_validation_failed",
        "app_rate_limited",
        "app_mutation_audit_failed",
        "app_outcome_unknown",
        "app_confirmation_required",
        "app_internal",
        "host_file_unresolvable",
        "host_file_provider_not_allowed",
        "host_file_redirect_refused",
        "host_file_address_refused",
        "host_file_too_large",
    }
)

#: Closed vocabulary for ``status`` -- one per §16.1 boundary outcome.
AUDIT_METADATA_STATUSES: frozenset[str] = frozenset(
    {
        "refused",
        "completed",
        "proposed",
        "consumed",
        "applied",
        "outcome_unknown",
        "invalid_response",
        "refreshed",
        "linked",
        "failed",
    }
)

MAX_BATCH_COUNT = 100_000

# fullmatch + explicit ASCII classes: no trailing-newline or Unicode-digit slack.
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,3}(?:-[0-9A-Za-z.]{1,32})?")


def _matches(pattern: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _member_of(vocabulary: frozenset[str]) -> Callable[[Any], bool]:
    return lambda value: type(value) is str and value in vocabulary


def _bounded_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_BATCH_COUNT


_digest = _matches(_DIGEST)
_id = _matches(_ID)
_name = _matches(_NAME)
_version = _matches(_VERSION)
_platform_code = _member_of(APP_PLATFORM_ERROR_CODES)

#: §16.2 "May include", each with its shape. The ONLY source of the allowlist.
AUDIT_METADATA_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "pack_id": _id,
    "binding_id": _id,
    "tool": _name,
    "mode": _name,
    # tenant/project/session/sub/actor ids
    "tenant_id": _id,
    "project_id": _id,
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
    "preview_digest": _digest,
    "grants_digest": _digest,
    "response_digest": _digest,
    "input_digest": _digest,
    # batch counts
    "batch_count": _bounded_count,
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
