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

from ._sqlite_connect import connect as _canonical_connect

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
        from .enforcement import hard_protected_authority
        from .identity_resolver import current_user
        from .read_access_policy import evaluate_read

        rel = project_relative or ""
        user_id, _email, _ptype = current_user(Path(project_root))
        # Authority = project_authority via enforcement (#344): an
        # AUTHENTICATED operator holding security.hard_protected —
        # every flavor, #404 (no local-admin passthrough). The old
        # ghost rbac.py failed OPEN here (empty rbac_users → always
        # authority), so the deny-list never fired.
        is_authority = hard_protected_authority(Path(project_root))
        # ACL role names come from the REAL store (rbac_store); the audit
        # user_id is attribution only — it feeds the ACL convenience match,
        # never the authority above.
        roles: tuple[str, ...] = ()
        if user_id:
            try:
                from .rbac_store import RBACStore

                roles = RBACStore().get_user_permissions(Path(project_root), user_id).roles
            except Exception:
                roles = ()
        decision = None
        for role in roles or ("",):
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
            "blocked_by": (decision.blocked_by if decision else "") or "read_deny_list",
            "reason": (decision.reason if decision else "")
            or "Read denied by hard-protected read policy.",
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
    if project_relative is None or hub is None:
        # External (host-cache / approved) paths cannot be indexed by
        # definition; and a hub-less call (pure path classification, e.g. a
        # gate() probe with hub=None) has no managed-mode / index to consult.
        # Either way the indexed-read gate is a no-op — never dereference a
        # None hub (was: AttributeError 'NoneType' has no attribute
        # 'managed_mode' at mcp_server._require_indexed_read_gate, 2026-07-10).
        return None
    from .mcp_server import _require_indexed_read_gate

    return _require_indexed_read_gate(
        hub,
        project_root,
        exact_path=project_relative,
        known_exact_path=known_exact_path,
    )


# The table that HOLDS sovereign souls. A sqlite file carrying it IS an
# empire ledger, wherever it happens to sit on disk.
_SOVEREIGN_SOUL_TABLE = "empire_skills"


def sovereign_soul_db_block(resolved: Path) -> dict[str, Any] | None:
    """Refuse a sqlite read of the empire ledger. Returns a refusal dict, or
    None for every other database (#222 leak 2).

    WHY THIS IS NOT PATH-SHAPED. ai_read_sqlite's only protection used to be
    the ``~/.aidocs/`` rule in ``protected_paths_classifier`` — real, but it
    describes WHERE the ledger normally lives, and ``AIDOCS_EMPIRE_DB``
    relocates it. Point the override at an approved external root and the
    path fence is satisfied while the sovereign rows read out verbatim. So
    the guard asks what the FILE CARRIES: a database holding the sovereign
    soul container is refused wherever it is.

    Refusing the whole file rather than filtering rows is deliberate. Nothing
    in production reads the empire ledger through this tool — the seven
    surfaces that legitimately touch souls all go through the store's
    governed door — so a row filter would add a partial-exposure surface to
    maintain in exchange for a capability no caller wants. Refusing the file
    also declines to answer whether any souls exist.

    Only ``sqlite_master`` is consulted. No row, and no scroll, is read here.
    """
    try:
        from .skill_store import soul_chamber_paths

        target = str(resolved).replace("\\", "/").rstrip("/").lower()
        for chamber in soul_chamber_paths():
            c = str(chamber).replace("\\", "/").rstrip("/").lower()
            if c and (target == c or target.startswith(c + "/")):
                return _sovereign_soul_refusal("soul_chamber_path")
    except Exception:
        pass  # structural check unavailable — the content check still runs

    try:
        conn = _canonical_connect(Path(resolved), read_only=True, row_factory=False)
    except Exception:
        return None  # not openable as sqlite — the parser reports it honestly
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (_SOVEREIGN_SOUL_TABLE,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if row is None:
        return None
    return _sovereign_soul_refusal(_SOVEREIGN_SOUL_TABLE)


def _sovereign_soul_refusal(matched: str) -> dict[str, Any]:
    """The one refusal shape for a sovereign-soul sqlite read. Names the
    governed door, per law 311bf3e6 — a refusal without a way forward just
    moves the problem."""
    return {
        "error": (
            "Sqlite read refused: this database is the empire ledger, which "
            "carries sovereign soul scrolls. Souls are read through their "
            "governed door, which records the act: use "
            "ai_soul(mode='read', skill_id=...) for a sovereign scroll, or "
            "ai_skill(mode='read', ...) / ai_skill(mode='list') for public "
            "skills. Non-soul sqlite files are unaffected."
        ),
        "blocked_by": "sovereign_soul_store",
        "matched_pattern": matched,
    }


_HOST_DOCTRINE_MD_BASENAMES = ("agents.md", "claude.md")


def _host_doctrine_md_block(
    hub: Any,
    project_root: Path,
    *,
    project_relative: str | None,
) -> dict[str, Any] | None:
    """Backlog #302 caveat A (2026-07-12): in an AIDOCS-MANAGED project,
    a RAW read of a host-level doctrine markdown (project-root
    ``AGENTS.md`` / ``CLAUDE.md``) is refused and REDIRECTED to
    ai_skill — AIDOCS owns doctrine and the raw host md is stale.

    Scope (deliberately narrow):
      * managed mode ACTIVE only — an unmanaged/plain read keeps working;
      * project-ROOT basenames only (``docs/foo.md`` etc. untouched);
      * placed AFTER the sensitive-zone / secrets / hard-protected
        blocks (never weakens them) and BEFORE the indexed gate, whose
        ``.md`` asset-exemption would otherwise allow the read.
    """
    if hub is None or project_relative is None:
        return None
    if project_relative.lower() not in _HOST_DOCTRINE_MD_BASENAMES:
        return None
    try:
        from .managed_mode_service import resolve_managed_session

        managed_sid = resolve_managed_session(hub.managed_mode, project_root)
    except Exception:
        return None
    if not managed_sid:
        return None
    return {
        "error": (
            f"Raw read of host doctrine `{project_relative}` is refused in an "
            f"AIDOCS-managed project: AIDOCS owns doctrine and the raw host "
            f"markdown is stale. Read doctrine via ai_skill instead — e.g. "
            f"ai_skill('aidocs-doctrine') or ai_skill('empire-doctrine')."
        ),
        "blocked_by": "host_doctrine_md_redirect",
    }


def _is_host_cache_path(resolved: Path) -> bool:
    """True iff ``resolved`` is inside a recognized host-tool cache root
    (``<home>/.claude`` or ``<home>/.codex``).

    These are deliberately left UNKNOWN_EXTERNAL by the trust-zone classifier
    (see path_trust_zone: "host cache ... must be read only THROUGH governed
    AIDOCS tools") — read_pipeline is that governed host-cache read surface.

    NARROW by design (operator decision 2026-07-10): ONLY these two roots are
    reachable via the AIDOCS read tools. Generic external paths stay REFUSED —
    a deliberate divergence from the native Read tool, which allows all
    non-sensitive external reads (``external_governed_read``). Sensitive home
    subdirs (.ssh/.aws/.config/appdata/...) are classified
    BLOCKED_SENSITIVE_EXTERNAL upstream and can never reach here.
    """
    try:
        home = Path.home()
    except Exception:
        return False
    try:
        rp = resolved.resolve()
    except Exception:
        rp = resolved
    for name in (".claude", ".codex"):
        try:
            root = (home / name).resolve()
        except Exception:
            root = home / name
        if rp == root or root in rp.parents:
            return True
    return False


def _governed_external_read(
    *,
    requested_path: str,
    resolved: Path,
    zone: PathTrustZone,
    project_root: Path,
) -> ReadGateResult:
    """Return the ReadGateResult for an EXTERNAL path that policy allows to be
    read (host cache or an approved external workspace).

    Sensitive zones + ``..`` traversal are already hard-blocked upstream. The
    residual governance, applied here so BOTH strict (host-cache carve-out) and
    zoned reads share one code path:
      * name-based ``_secrets_block`` — a secret-SHAPED name (id_rsa / *.pem /
        .env) is refused even in an allowed external root; and
      * the CONTENT-secret sniff — an innocuously-named external file whose
        CONTENT is a credential (a pasted key in notes.txt) is refused,
        mirroring access_gate.host_read_decision's ``_content_is_secret``.
    The indexed / hard-protected gates are project-scoped no-ops for an external
    path (project_relative is None), so they are intentionally skipped.
    """
    secrets_refusal = _secrets_block(Path(project_root), resolved=resolved)
    if secrets_refusal is not None:
        secrets_refusal.setdefault("requested_path", requested_path)
        secrets_refusal.setdefault("zone", str(zone))
        return ReadGateResult(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=None,
            refusal=secrets_refusal,
        )
    try:
        from .access_gate import _content_is_secret as _ext_content_secret

        if _ext_content_secret(str(resolved)):
            return _refusal(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=zone,
                project_relative=None,
                blocked_by="sensitive_content_blocked",
                reason=(
                    f"Path `{requested_path}` contains credential material in "
                    f"its content (name-based classification missed it)."
                ),
            )
    except Exception:
        pass
    return ReadGateResult(
        requested_path=requested_path,
        resolved_path=resolved,
        zone=zone,
        project_relative=None,
        refusal=None,
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
            # Host-cache carve-out (2026-07-10): strict mode otherwise refuses
            # EVERY absolute path, but the AIDOCS read tools (ai_read_raw /
            # ai_get_lines) must be able to read the recognized host cache
            # (~/.claude, ~/.codex) — the narrow governed host-cache surface.
            # Sensitive home subdirs are BLOCKED_SENSITIVE_EXTERNAL upstream and
            # never satisfy _is_host_cache_path; generic external absolutes stay
            # refused below.
            if _is_host_cache_path(p_in):
                try:
                    _hc = p_in.resolve()
                except Exception:
                    _hc = p_in
                return _governed_external_read(
                    requested_path=requested_path,
                    resolved=_hc,
                    zone=PathTrustZone.UNKNOWN_EXTERNAL,
                    project_root=Path(project_root),
                )
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
        doctrine = _host_doctrine_md_block(
            hub,
            Path(project_root),
            project_relative=project_relative,
        )
        if doctrine is not None:
            doctrine.setdefault("requested_path", requested_path)
            doctrine.setdefault("zone", str(zone))
            return ReadGateResult(
                requested_path=requested_path,
                resolved_path=resolved,
                zone=zone,
                project_relative=project_relative,
                refusal=doctrine,
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
                f"`{requested_path}` is a credential/secret zone "
                f"(SSH keys / cloud creds / home config) — hard-blocked. "
                f"Read project config from the project tree or .MEMORY; "
                f"never from home credential paths."
            ),
        )
    # NARROW governed-external policy (operator 2026-07-10): an external path is
    # readable ONLY if it is a recognized host-cache root (~/.claude, ~/.codex)
    # or an operator-approved external workspace. Generic UNKNOWN_EXTERNAL is
    # REFUSED here — a DELIBERATE divergence from the native Read tool, which
    # governs-allows all non-sensitive external (access_gate
    # external_governed_read). This keeps the AIDOCS read tools scoped to: the
    # project tree, .MEMORY, approved external roots, and the host cache.
    # Sensitive zones (.ssh/.aws/home config) were already HARD-blocked above.
    if zone == PathTrustZone.UNKNOWN_EXTERNAL and not _is_host_cache_path(resolved):
        return _refusal(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=None,
            blocked_by="unknown_external_path",
            reason=(
                f"Path `{requested_path}` is outside the project and is not a "
                f"recognized host-cache root (~/.claude, ~/.codex) or an approved "
                f"external workspace — refused."
            ),
        )
    # Allowed external (host cache or approved workspace) falls through. The
    # remaining governance: _secrets_block refuses credential CONTENT/name in
    # an innocuously-named file; _hard_protected_read_block + _indexed_read_block
    # are project-scoped no-ops for an external path (project_relative is None).
    #
    # CONTENT-secret guard for GOVERNED EXTERNAL reads: _secrets_block below is
    # NAME-based (.env/*.pem). An external file with an INNOCUOUS name but
    # credential CONTENT (a pasted key in notes.txt, a transcript with a token)
    # must still be refused — matching host_read_decision._content_is_secret.
    # Scoped to external zones (project/memory are covered by the name + indexed
    # gates and content-sniffing every project read false-positives on fixtures).
    if zone in (
        PathTrustZone.UNKNOWN_EXTERNAL,
        PathTrustZone.APPROVED_EXTERNAL_WORKSPACE,
    ):
        try:
            from .access_gate import _content_is_secret as _ext_content_secret

            if _ext_content_secret(str(resolved)):
                return _refusal(
                    requested_path=requested_path,
                    resolved_path=resolved,
                    zone=zone,
                    project_relative=None,
                    blocked_by="sensitive_content_blocked",
                    reason=(
                        f"Path `{requested_path}` contains credential material in "
                        f"its content (name-based classification missed it)."
                    ),
                )
        except Exception:
            pass
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
    doctrine = _host_doctrine_md_block(
        hub,
        Path(project_root),
        project_relative=project_relative,
    )
    if doctrine is not None:
        doctrine.setdefault("requested_path", requested_path)
        doctrine.setdefault("zone", str(zone))
        return ReadGateResult(
            requested_path=requested_path,
            resolved_path=resolved,
            zone=zone,
            project_relative=project_relative,
            refusal=doctrine,
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
