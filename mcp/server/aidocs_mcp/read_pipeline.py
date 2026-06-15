"""Shared read-pipeline gate.

Single security envelope for every public read tool (ai_read_raw,
ai_read_pdf, ai_read_excel, ai_read_docx, ai_read_sqlite,
ai_read_jsonl). Closes the structural divergence where:

  - read_raw used file_ops._resolve_path (project-anchored, strict)
  - structured_file_parsers used a weaker resolver (cwd-relative,
    accepts absolute), causing the cross-project phantom-row bug

The gate produces ONE canonical absolute resolved path and a zone
classification. Readers receive the absolute path and never resolve
it themselves — they assert is_absolute() and open verbatim.

Two policy modes:

  strict — used by read_raw. Project-only.
    - rejects absolute input (no path can target outside project_root)
    - rejects `..` traversal / paths that escape project_root
    - rejects approved_external_workspace (raw byte reads do not need
      external access; if an operator wants external bytes, structured
      readers and zoned mode cover that)
    - allows project_internal, memory_internal

  zoned — used by structured readers. External-aware.
    - allows project_internal, memory_internal
    - allows approved_external_workspace
    - rejects blocked_sensitive_external (~/.ssh, ~/.aws, etc.)
    - rejects unknown_external (anything outside project + approved)

Indexed-read blocking (block raw-byte reads of files the index has
already structured) operates on the project-relative form and is
unchanged. The shared gate orchestrates it; the existing
_require_indexed_read_gate function is renamed in spirit (its scope
remains "indexed-file block" — the shared gate is the security
envelope around it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .path_trust_zone import PathTrustZone, resolve_under_project


@dataclass(frozen=True)
class ReadGateResult:
    """Outcome of the shared read gate.

    On allow: ``refusal is None`` and ``resolved_path`` is the
    absolute path the reader must open. On deny: ``refusal`` is the
    structured error dict to return verbatim from the wrapper, and
    ``resolved_path`` is set for telemetry but MUST NOT be used.
    """

    requested_path: str
    resolved_path: Path
    zone: PathTrustZone
    project_relative: str | None
    refusal: dict[str, Any] | None

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def _project_relative_form(
    resolved: Path,
    project_root: Path,
) -> str | None:
    """Return the project-relative POSIX form for indexed-read
    lookup, or None if the resolved path is outside project_root.
    """
    try:
        rel = resolved.relative_to(Path(project_root).resolve())
    except (ValueError, OSError):
        return None
    return str(rel).replace("\\", "/")


def _refusal(
    *,
    requested_path: str,
    resolved_path: Path,
    zone: PathTrustZone,
    project_relative: str | None,
    blocked_by: str,
    reason: str,
) -> ReadGateResult:
    return ReadGateResult(
        requested_path=requested_path,
        resolved_path=resolved_path,
        zone=zone,
        project_relative=project_relative,
        refusal={
            "error": reason,
            "blocked_by": blocked_by,
            "requested_path": requested_path,
            "zone": str(zone),
        },
    )


def _read_approved_external_roots(project_root: Path) -> list[str]:
    """Best-effort read of security.approved_external_roots from
    effective config. Returns empty list on any failure — fail-closed
    is the right default for an external-allow list.
    """
    try:
        from .config import get_setting

        raw = get_setting(
            "security.approved_external_roots",
            project_root=project_root,
            default=[],
        )
        if not isinstance(raw, list):
            return []
        return [str(r) for r in raw if str(r).strip()]
    except Exception:
        return []


_TRASH_HEX_PREFIX_RE = __import__("re").compile(r"^[0-9a-f]{8}-")


def _strip_trash_hex_prefix(basename: str) -> str:
    """Recover the original basename from a `.TRASH/<date>/<hex>-<base>`
    filename. ai_delete prefixes every trashed file with an 8-char hex
    token (secrets.token_hex(4)); stripping it yields the original
    filename for re-classification.
    """
    return _TRASH_HEX_PREFIX_RE.sub("", basename, count=1)


def _is_under_trash(resolved: Path, project_root: Path) -> bool:
    """True iff the resolved path is under `<project_root>/.TRASH/`."""
    try:
        rel = resolved.resolve().relative_to(project_root.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    return bool(parts) and parts[0] == ".TRASH"


def _hard_protected_read_block(
    project_root: Path,
    *,
    project_relative: str | None,
) -> dict[str, Any] | None:
    """Refuse a read iff the path is on the configured hard-protected read
    DENY-LIST and the resolved principal is neither the admin authority nor on
    the file's read ACL. No-op (returns None) unless security.read_deny is set,
    so default behavior — reads permitted — is unchanged.
    """
    from .config import GATE_READ_ACL, GATE_READ_DENY

    if not GATE_READ_DENY:
        return None
    try:
        from .identity_resolver import current_user
        from .rbac import RBACStore
        from .read_access_policy import evaluate_read

        rel = project_relative or ""
        user_id, _email, _ptype = current_user(Path(project_root))
        rbac = RBACStore()
        user = rbac.get_user(Path(project_root), user_id) if user_id else None
        role = user.role if user is not None else ""
        is_authority = rbac.check_permission(
            Path(project_root), user_id or None, "security.hard_protected"
        ).allowed
        decision = evaluate_read(
            rel,
            role=role,
            user=user_id,
            is_authority=is_authority,
            deny_globs=GATE_READ_DENY,
            acl=GATE_READ_ACL,
        )
        if decision.allowed:
            return None
        return {
            "blocked_by": decision.blocked_by or "read_deny_list",
            "reason": decision.reason or "Read denied by hard-protected read policy.",
        }
    except Exception:
        # Fail OPEN for reads — the deny-list is a tightening convenience, not a
        # safety boundary (edits are the fenced direction). A resolver error
        # must not break legitimate reads.
        return None


def _secrets_block(
    project_root: Path,
    *,
    resolved: Path,
) -> dict[str, Any] | None:
    """Refuse reads of secret-shaped paths (`.env`, `*.pem`, SSH/cloud
    cred files, etc.) unless the operator has opted in via
    security.allow_raw_read_of_secrets.

    Special-case for `.TRASH/` (2026-05-27 operator doctrine):
    reads INTO trash are allowed iff the original (pre-trash) basename
    would itself have been readable. Trashed source files (utils.py,
    config.json) can be rummaged + restored; trashed secrets (.env,
    id_rsa, anything matching the secrets-filename patterns) stay
    refused — deleting a secret should NOT create a back-door for
    reading it.

    In practice secrets-shaped basenames never reach .TRASH/ because
    ai_delete refuses them at the forbidden_basename gate. This recheck
    is defense in depth: any future PR that loosens that gate, or a
    pre-existing trash entry from before the gate was added, is still
    covered.
    """
    try:
        from .protected_paths_classifier import (
            CLASS_FORBIDDEN_AIDOCS,
            CLASS_SECRETS_GATED,
            classify_path,
        )

        cls = classify_path(str(resolved), project_root=project_root)
    except Exception:
        return None

    # CLASS_FORBIDDEN_AIDOCS includes .TRASH/ + .aidocs/ + .MEMORY/.
    # For .aidocs/ and .MEMORY/, refuse without any rummage path.
    # For .TRASH/, re-classify the ORIGINAL basename and let through
    # iff the original would have been allowed.
    if cls.classification == CLASS_FORBIDDEN_AIDOCS:
        if _is_under_trash(resolved, project_root):
            # Recover the original filename from the trash entry's
            # hex-prefixed name and re-classify what the original file
            # would have been. We classify a SYNTHETIC project-root
            # path so the .TRASH/ dir match doesn't recurse on us.
            original_basename = _strip_trash_hex_prefix(resolved.name)
            try:
                synthetic = project_root / original_basename
                original_cls = classify_path(
                    str(synthetic),
                    project_root=project_root,
                )
            except Exception:
                # Failed to re-classify → fall through to refusal
                # below (fail closed on uncertain trash reads).
                original_cls = None

            if original_cls is None or original_cls.classification in (
                CLASS_SECRETS_GATED,
                CLASS_FORBIDDEN_AIDOCS,
            ):
                return {
                    "error": (
                        f"Trash read refused — original file "
                        f"{original_basename!r} would have been a "
                        f"protected/secret path. Deleting a secret "
                        f"does not create a back-door for reading it."
                    ),
                    "blocked_by": "trash_secret_rehydration",
                    "matched_pattern": (
                        original_cls.matched_pattern if original_cls else "unclassified"
                    ),
                }
            # Original was not secret/forbidden — allow the rummage
            # for restore. Falls through to the rest of the read
            # pipeline (which may still reject for other reasons —
            # path outside project, etc.).
            return None

        # Non-trash forbidden_aidocs paths (.aidocs/, .MEMORY/, the
        # gate's own source) refuse outright — no rummage path here.
        return {
            "error": (
                f"Raw read refused on AIDOCS-protected path: "
                f"{cls.reason}. These are gate infrastructure / state "
                f"and have no operator-overridable read access."
            ),
            "blocked_by": "forbidden_aidocs_path",
            "matched_pattern": cls.matched_pattern,
        }

    if cls.classification != CLASS_SECRETS_GATED:
        return None
    try:
        from .config import get_setting

        allow = bool(
            get_setting(
                "security.allow_raw_read_of_secrets",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        allow = False
    if allow:
        return None
    return {
        "error": (
            f"Secret-shaped path refused: {cls.reason}. Set "
            f"security.allow_raw_read_of_secrets=true to override "
            f"(operator-only; the override is logged in audit)."
        ),
        "blocked_by": "secrets_gated_path",
        "matched_pattern": cls.matched_pattern,
    }


def _indexed_read_block(
    hub: Any,
    project_root: Path,
    *,
    project_relative: str | None,
    known_exact_path: bool,
) -> dict[str, Any] | None:
    """Delegate to the existing indexed-read block. Returns refusal
    dict or None. Operates on the project-relative form so the
    indexed-file lookup matches the existing index keying.
    """
    if project_relative is None:
        # External (approved) paths cannot be indexed by definition.
        return None
    from .mcp_server import _require_indexed_read_gate

    return _require_indexed_read_gate(
        hub,
        project_root,
        exact_path=project_relative,
        known_exact_path=known_exact_path,
    )


def gate(
    hub: Any,
    project_root: Path,
    path: str,
    *,
    mode: str,
    known_exact_path: bool = False,
) -> ReadGateResult:
    """Shared read-pipeline gate. Returns ReadGateResult.

    Caller pattern in a tool wrapper:

        result = read_pipeline.gate(hub, project_root, path,
                                    mode="zoned", known_exact_path=keb)
        if result.refusal:
            return result.refusal
        return parser(result.resolved_path, ...,
                      requested_path=path, zone=str(result.zone))
    """
    if mode not in ("strict", "zoned"):
        raise ValueError(f"invalid read-gate mode: {mode!r} (expected 'strict' or 'zoned')")
    requested_path = str(path) if path is not None else ""

    # Relative-traversal hard refusal (Slice 1, canonical 2026-04-29).
    # Applies to BOTH modes. Any relative input that contains a `..`
    # part is refused before resolve/classify — relative paths are
    # project-relative only, period. Approved-external reads in zoned
    # mode require an explicit absolute path (the existing
    # APPROVED_EXTERNAL_WORKSPACE classify rule). This closes the gap
    # where a literal `..` segment slipped past _path_contains in
    # zoned mode and got classified as PROJECT_INTERNAL.
    if requested_path and requested_path.strip():
        _stripped = requested_path.strip()
        _p_in = Path(_stripped)
        if not _p_in.is_absolute():
            if ".." in _p_in.parts:
                return _refusal(
                    requested_path=requested_path,
                    resolved_path=Path(project_root),
                    zone=PathTrustZone.UNKNOWN_EXTERNAL,
                    project_relative=None,
                    blocked_by="path_escapes_project_root",
                    reason=(
                        f"Relative paths must be project-internal; "
                        f"`..` traversal is not allowed: {requested_path}"
                    ),
                )

    # Strict mode: NO approved-external lookup, no zoned classification
    # outside project boundary. Reject absolute input up front so the
    # operator gets a clear failure mode.
    if mode == "strict":
        if not requested_path or not requested_path.strip():
            return _refusal(
                requested_path=requested_path,
                resolved_path=Path(project_root),
                zone=PathTrustZone.PROJECT_INTERNAL,
                project_relative=None,
                blocked_by="empty_path",
                reason="path is required",
            )
        p_in = Path(requested_path)
        if p_in.is_absolute():
            return _refusal(
                requested_path=requested_path,
                resolved_path=p_in,
                zone=PathTrustZone.UNKNOWN_EXTERNAL,
                project_relative=None,
                blocked_by="absolute_path_in_strict_mode",
                reason=(
                    f"Absolute paths are not allowed: {requested_path}. "
                    f"Use a path relative to the project root."
                ),
            )
        # Resolve under project_root (approved_external_roots NOT
        # supplied in strict mode). follow_symlinks=True so `..` is
        # collapsed before traversal/zone check, otherwise a literal
        # `..` segment slips past relative_to() on Windows.
        resolved, zone = resolve_under_project(
            requested_path,
            project_root=project_root,
            approved_external_roots=None,
            follow_symlinks=True,
        )
        # Traversal check: resolved path must remain inside project_root.
        try:
            resolved.relative_to(Path(project_root).resolve())
        except ValueError:
            return _refusal(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=PathTrustZone.UNKNOWN_EXTERNAL,
                project_relative=None,
                blocked_by="path_escapes_project_root",
                reason=(f"Path escapes project root: {requested_path}"),
            )
        # Zone must be PROJECT_INTERNAL or MEMORY_INTERNAL.
        if zone not in (
            PathTrustZone.PROJECT_INTERNAL,
            PathTrustZone.MEMORY_INTERNAL,
        ):
            return _refusal(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=zone,
                project_relative=None,
                blocked_by="strict_mode_external_path",
                reason=(
                    f"strict mode rejects zone={zone!s}; "
                    f"raw byte reads must stay inside project_root"
                ),
            )
        secrets_refusal = _secrets_block(
            Path(project_root),
            resolved=resolved,
        )
        if secrets_refusal is not None:
            secrets_refusal.setdefault("requested_path", requested_path)
            secrets_refusal.setdefault("zone", str(zone))
            return ReadGateResult(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=zone,
                project_relative=_project_relative_form(
                    resolved,
                    Path(project_root),
                ),
                refusal=secrets_refusal,
            )
        project_relative = _project_relative_form(
            resolved,
            Path(project_root),
        )
        hp_read = _hard_protected_read_block(
            Path(project_root),
            project_relative=project_relative,
        )
        if hp_read is not None:
            hp_read.setdefault("requested_path", requested_path)
            hp_read.setdefault("zone", str(zone))
            return ReadGateResult(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=zone,
                project_relative=project_relative,
                refusal=hp_read,
            )
        indexed = _indexed_read_block(
            hub,
            project_root,
            project_relative=project_relative,
            known_exact_path=known_exact_path,
        )
        if indexed is not None:
            indexed.setdefault("requested_path", requested_path)
            indexed.setdefault("zone", str(zone))
            return ReadGateResult(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=zone,
                project_relative=project_relative,
                refusal=indexed,
            )
        return ReadGateResult(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=project_relative,
            refusal=None,
        )

    # Zoned mode: full classify with approved_external_roots.
    if not requested_path or not requested_path.strip():
        return _refusal(
            requested_path=requested_path,
            resolved_path=Path(project_root),
            zone=PathTrustZone.PROJECT_INTERNAL,
            project_relative=None,
            blocked_by="empty_path",
            reason="path is required",
        )
    approved = _read_approved_external_roots(Path(project_root))
    resolved, zone = resolve_under_project(
        requested_path,
        project_root=project_root,
        approved_external_roots=approved,
    )
    if zone == PathTrustZone.BLOCKED_SENSITIVE_EXTERNAL:
        return _refusal(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=None,
            blocked_by="sensitive_path_blocked",
            reason=(
                f"Path `{requested_path}` is in a sensitive zone "
                f"(SSH/cloud creds/home config). Hard block."
            ),
        )
    if zone == PathTrustZone.UNKNOWN_EXTERNAL:
        return _refusal(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=None,
            blocked_by="unknown_external_path",
            reason=(
                f"Path `{requested_path}` is outside project root "
                f"and not in approved_external_roots. Add to "
                f"security.approved_external_roots (dashboard) or "
                f"request admin escalation."
            ),
        )
    secrets_refusal = _secrets_block(
        Path(project_root),
        resolved=resolved,
    )
    if secrets_refusal is not None:
        secrets_refusal.setdefault("requested_path", requested_path)
        secrets_refusal.setdefault("zone", str(zone))
        return ReadGateResult(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=_project_relative_form(
                resolved,
                Path(project_root),
            ),
            refusal=secrets_refusal,
        )
    project_relative = _project_relative_form(
        resolved,
        Path(project_root),
    )
    hp_read = _hard_protected_read_block(
        Path(project_root),
        project_relative=project_relative,
    )
    if hp_read is not None:
        hp_read.setdefault("requested_path", requested_path)
        hp_read.setdefault("zone", str(zone))
        return ReadGateResult(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=project_relative,
            refusal=hp_read,
        )
    indexed = _indexed_read_block(
        hub,
        project_root,
        project_relative=project_relative,
        known_exact_path=known_exact_path,
    )
    if indexed is not None:
        indexed.setdefault("requested_path", requested_path)
        indexed.setdefault("zone", str(zone))
        return ReadGateResult(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=project_relative,
            refusal=indexed,
        )
    return ReadGateResult(
        requested_path=requested_path,
        resolved_path=resolved,
        zone=zone,
        project_relative=project_relative,
        refusal=None,
    )
