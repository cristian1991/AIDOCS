"""Public execution-grant contract — GrantDecision + project_status + resolve_execution_grant.

These are general AIDOCS project-policy primitives, NOT the WebMCP gate moat:
- project_status reads a project's index state via outer_gate_executor (public).
- resolve_execution_grant is deny-by-default scope/role policy over a PASSED project dict
  (no store/DB access — the project is supplied by the caller).

The private gate (outer_gate_projects) re-exports these for backward compatibility; the public
tool taxonomy (outer_gate_catalog) imports them from HERE, so no public module depends on the
private gate. EDIT_ROLES is imported lazily from outer_gate_edit (also public).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


def project_status(root: Path) -> dict:
    from .outer_gate_executor import _index_db_path, _index_populated, _is_aidocs_project, _is_stale

    root = Path(root)
    aidocs = _is_aidocs_project(root)
    populated = _index_populated(root) if aidocs else False
    stale = _is_stale(root) if populated else False
    rows = 0
    if populated:
        try:
            con = sqlite3.connect(f"file:{_index_db_path(root)}?mode=ro", uri=True)
            try:
                rows = int(con.execute("SELECT COUNT(*) FROM code_files").fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error:
            rows = 0
    return {
        "is_aidocs": aidocs,
        "indexed": populated,
        "stale": stale,
        "code_files": rows,
        "ready": bool(aidocs and populated and not stale),
        # #190: `stale`/`ready` are the INDEX-freshness axis ONLY (index re-sync
        # recency) — they say NOTHING about whether the git checkout matches
        # origin. Name the GIT-currency axis SEPARATELY so a behind-origin clone
        # can never read as git-current just because its index is fresh. This is
        # network-free (hot-path safe): "unverified" means "not checked here —
        # run git_ops(op='status') or project refresh for the live origin delta".
        # The operator-driven git_ops status surface does the actual fetch-only
        # drift check and reports behind_origin / origin_check.
        "git_sync": "unverified",
    }


@dataclass(frozen=True)
class GrantDecision:
    allow: bool
    reason: str = ""


def resolve_execution_grant(*, principal: dict, project: dict | None, kind: str) -> GrantDecision:
    """Deny-by-default execution-grant resolution — the gate's dashboard-grant.
    A grant is the operator-minted scoped token (role + scope) resolved against the
    CURRENTLY-SELECTED, registered, ready project. Anything missing/expired/revoked/stale/
    untrusted/unindexed/wrong-scope => deny. ``kind`` in {read, edit, run}.

    (Token expiry/revocation is enforced upstream by OuterGateTokenStore.validate; a revoked/
    expired token never produces a principal, so it can never grant.)
    """
    if not isinstance(principal, dict) or not principal.get("user_id"):
        return GrantDecision(False, "no_principal")
    if project is None:
        return GrantDecision(False, "no_project_selected")
    scope = set(principal.get("scope") or [])
    role = str(principal.get("effective_role") or "").strip().lower()
    st = project_status(Path(project["root"]))
    if not st["is_aidocs"]:
        return GrantDecision(False, "project_untrusted")
    if not st["indexed"]:
        return GrantDecision(False, "project_unindexed")
    if st["stale"]:
        return GrantDecision(False, "project_index_stale")
    if kind == "read":
        if "tier_r_invoke" not in scope:
            return GrantDecision(False, "insufficient_scope")
        return GrantDecision(True)
    if kind == "edit":
        from .outer_gate_edit import EDIT_ROLES

        if "tier_m_edit" not in scope:
            return GrantDecision(False, "insufficient_scope")
        if role not in EDIT_ROLES:
            return GrantDecision(False, "edit_role_required")
        return GrantDecision(True)
    if kind == "run":
        # project_run only (NOT tier_m_edit / project_import). The shell tool's own law
        # (bash_policy/heuristic_judge/freeze/destructive floor) gates the COMMAND; this
        # gate gates ACCESS to the runner + project binding.
        if "project_run" not in scope:
            return GrantDecision(False, "insufficient_scope")
        return GrantDecision(True)
    return GrantDecision(False, "unknown_grant_kind")
