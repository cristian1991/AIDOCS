"""Outer Gate canonical LOCAL manifest — a CADASTRAL DEED (P1 hardening, NO net).

Every MCP ``@server.tool`` and every CLI command is a numbered stone with a
legally-binding boundary. The manifest is built from AUTHORITATIVE discovery (a
glob of the tool modules — never a hand-maintained file list that can silently
omit a module) and is cross-checkable against the live FastMCP registry. A tool
may enter the REMOTE surface only by passing every boundary check; the defaults
are fail-closed.

Per-stone fields:
  name, kind, source(file:line)
  tier            R | M | A | L | unknown
  enforcement_binding + binding_evidence
  verified        read | decorator | inspection | cli_class | none
  schema          {params}
  duplicated      True if the name is registered more than once (collapse hazard)
  needs_review    True if remote-ineligible pending proof (R without line-read,
                  unknown tier, or a duplicate)
  remote_eligible BOUNDARY: may a gateway EVER serve this? (static contract)
  requires_auth / requires_project_binding / requires_audit / data_scope
                  invoke-time preconditions a gateway MUST enforce (distinct from
                  remote_eligible: eligibility is the deed, these are the covenants)

remote_eligible rule (``compute_remote_eligible``), fail-closed:
  not duplicated AND not needs_review AND (
     (tier R AND verified == "read")            # read-only PROVEN by handler read
     OR (tier in {M,A} AND binding ∈ PROVEN)    # mutation/admin with a real gate
  )
  L / unknown / duplicate / unproven-R → False.

NO HTTP listener, token issuer, or transport here. This is the contract a future
gateway consults; these tests are the surveyor's seal.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

PKG = Path(__file__).resolve().parent

TIER_R = "R"
TIER_M = "M"
TIER_A = "A"
TIER_L = "L"
TIER_UNKNOWN = "unknown"
VALID_TIERS = frozenset({TIER_R, TIER_M, TIER_A, TIER_L, TIER_UNKNOWN})
VALID_DATA_SCOPES = frozenset({"project", "global", "session", "none"})

PROVEN_BINDINGS = frozenset({"cascade", "rbac", "operator_auth"})

# Acknowledged duplicate tool names. EMPTY as of 2026-05-24: the lone real
# collision (execution_prune — a shadowed second registration in
# server_project_admin_tools.py) was removed. The duplicate-detection machinery
# stays live: any NEW duplicate now violates ``set(duplicate_tool_names()) <=
# KNOWN_DUPLICATES`` and fails the contract test.
KNOWN_DUPLICATES: frozenset[str] = frozenset()

# FastMCP registers some defs under a name != the Python def name. Reconciled
# against the LIVE registry (discover_registered_tools): the ``aidocs_`` prefix
# is stripped at registration, so the manifest must use the REGISTERED name.
_REGISTER_PREFIX_STRIP = "aidocs_"

# ``@server.tool``-shaped Python defs that SKIP MCP registration (hidden helpers,
# folded into other tools — mcp_server.py:1418). Verified registry-absent; they
# are NOT attack surface and are excluded from the manifest.
HIDDEN_UNREGISTERED = frozenset(
    {
        "ai_extract_block",
        "ai_extract_symbol",
        "ai_find_symbol_range",
        "ai_preview_extraction_deps",
        "ai_refactor_extract",
        "ai_suggest_extractions",
    },
)

# Tools present in the LIVE registry but registered PROGRAMMATICALLY (no
# ``@server.tool def`` the AST can see — e.g. dashboard conductor IPC). Each is
# numbered + classified here so the deed stays complete. (tier, binding,
# evidence, verified). All dashboard/local → Tier-L, remote-refused.
REGISTRY_ONLY_TOOLS: dict[str, tuple[str, str, str, str]] = {
    "conductor_start": (TIER_L, "local_only", "dashboard Tauri IPC (local)", "inspection"),
    "conductor_stop": (TIER_L, "local_only", "dashboard Tauri IPC (local)", "inspection"),
    "conductor_send": (TIER_L, "local_only", "dashboard Tauri IPC (local)", "inspection"),
    "conductor_output": (TIER_L, "local_only", "dashboard Tauri IPC (local)", "inspection"),
}


# ── authored classification for the 66 neither-hint MCP tools + overrides ──
# (tier, binding, evidence, verified). verified=="read" means the HANDLER was
# read first-hand and confirmed; only read-proven R tools can be remote-eligible.
MCP_TIER_OVERRIDES: dict[str, tuple[str, str, str, str]] = {
    # ── READ, handler read first-hand (eligible) ──
    "git_diag": (TIER_R, "read_verified", "server_legacy_git_tools.py:488 git read", "read"),
    "git_fork_status": (TIER_R, "read_verified", "server_legacy_git_tools.py:554 git read", "read"),
    "git_upstream_changes": (
        TIER_R,
        "read_verified",
        "server_legacy_git_tools.py:754 git read",
        "read",
    ),
    "git_conflict_analysis": (
        TIER_R,
        "read_verified",
        "server_legacy_git_tools.py:836 git read",
        "read",
    ),
    "git_merge_plan": (TIER_R, "read_verified", "server_legacy_git_tools.py:920 git read", "read"),
    "execution_usage_by_identity": (
        TIER_R,
        "read_verified",
        "mcp_server.py:4846 usage_by_* reads",
        "read",
    ),
    # ── code discovery / review reads, line-audited 2026-05-24 ──
    # Pure index reads (no side effect, project-scoped):
    "ai_get_dependencies": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:746 get_dependencies; pure index read",
        "read",
    ),
    "ai_get_symbol_info": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:828 signatures/params, no file read; pure",
        "read",
    ),
    # (ai_find_symbol_range / ai_preview_extraction_deps are HIDDEN_UNREGISTERED —
    #  not in the live registry, so not classified here.)
    # Index reads whose ONLY side-effect is a session-scoped known_exact_path
    # read-grant over already-surfaced INDEXED paths (the indexer excludes
    # sensitive files, so the grant can never reach a secret; no file/index/
    # control mutation — audited):
    "ai_find": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:997 symbol/text/file search; scoped read-grant only",
        "read",
    ),
    "ai_search": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:283 search_code; scoped read-grant only",
        "read",
    ),
    "ai_text_search": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:314 search_text; scoped read-grant only",
        "read",
    ),
    "ai_investigate": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:886 hub.code.investigate; scoped read-grant only",
        "read",
    ),
    "ai_trace": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:1223 hub.code.trace_*; scoped read-grant only",
        "read",
    ),
    "ai_bundle": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:1383 structural bundle; scoped read-grant only",
        "read",
    ),
    "ai_get_outline": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:758 indexed outline; scoped read-grant only",
        "read",
    ),
    "ai_get_symbol_snippet": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:792 indexed snippet; scoped read-grant only",
        "read",
    ),
    # ── READ by name/docstring only — NOT line-proven → needs_review (refused) ──
    "circuit_breaker_status": (TIER_R, "read_inspection", "get_state read", "inspection"),
    "mcp_registry_search": (TIER_R, "read_inspection", "registry SELECT", "inspection"),
    "mcp_registry_get": (TIER_R, "read_inspection", "registry SELECT", "inspection"),
    "metrics_snapshot": (TIER_R, "read_inspection", "metrics read", "inspection"),
    "metrics_prometheus": (TIER_R, "read_inspection", "metrics read", "inspection"),
    "edit_history_list": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2336 list_edits; content is non-sensitive edited code (AIDOCS refuses sensitive edits)",
        "read",
    ),
    "edit_history_grep": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2354 grep_edits; same non-sensitive content scope",
        "read",
    ),
    "files_touched": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2384 edit-history path summary; pure, no content",
        "read",
    ),
    "mcp_tool_catalog": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2397 registered-tool metadata only; pure",
        "read",
    ),
    "session_status_badge": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2440 session-state badge; pure",
        "read",
    ),
    "semantic_index_status": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2526 index_status; pure",
        "read",
    ),
    "semantic_search": (
        TIER_R,
        "read_verified",
        "mcp_server.py:2492 vector query over indexed code; pure",
        "read",
    ),
    "ai_resolve_scope": (TIER_R, "read_inspection", "scope resolution read", "inspection"),
    "ai_resolve_backend": (TIER_R, "read_inspection", "backend resolution read", "inspection"),
    "ai_preflight": (TIER_R, "read_inspection", "prompt evaluation read", "inspection"),
    "ai_gate_msg": (TIER_R, "read_inspection", "gate message read", "inspection"),
    "context_budget_check": (TIER_R, "read_inspection", "budget read", "inspection"),
    "skill_scan": (TIER_R, "read_inspection", "skill scan read", "inspection"),
    "ai_slop": (
        TIER_R,
        "read_verified",
        "server_code_tools.py:618 SCAN/READ only after split 2026-05-24 (mutating modes → ai_deslop_apply); pure index reads, no grant",
        "read",
    ),
    # ── ADMIN, self-gated, PROVEN binding (eligible) ──
    "rbac_approve_escalation": (
        TIER_A,
        "rbac",
        "server_rbac_tools.py:123 rbac.has_permission",
        "read",
    ),
    "rbac_deny_escalation": (
        TIER_A,
        "rbac",
        "server_rbac_tools.py:123 rbac.has_permission",
        "read",
    ),
    "admin_clear_freeze": (TIER_A, "rbac", "server_rbac_tools.py:506 rbac.has_permission", "read"),
    "workflow_definition_add": (
        TIER_A,
        "operator_auth",
        "server_plan_task_tools.py:1609-1636",
        "read",
    ),
    "workflow_definition_update": (
        TIER_A,
        "operator_auth",
        "server_plan_task_tools.py:1609-1636",
        "read",
    ),
    "workflow_definition_remove": (
        TIER_A,
        "operator_auth",
        "server_plan_task_tools.py:1609-1636",
        "read",
    ),
    "ai_palace_diary_write": (TIER_A, "operator_auth", "server_palace_tools.py:395-436", "read"),
    # ── ADMIN/SENSITIVE, NO auth gate — verified UNBOUND → refused ──
    # AUDEL: gated locally by audit_deletion_law (operator-auth + admin.manage_config
    # + non-subagent + reason + audit-of-audit). `audit_deletion_law` is a LOCAL
    # binding — deliberately NOT in PROVEN_BINDINGS, so these stay remote-REFUSED
    # until/unless explicitly promoted for the remote path.
    "execution_prune": (
        TIER_A,
        "audit_deletion_law",
        "audit_deletion_law.run_audit_deletion: operator-auth+admin+non-subagent+reason+audit-of-audit; local-only, remote-refused",
        "read",
    ),
    "execution_clear_token_usage": (
        TIER_A,
        "audit_deletion_law",
        "audit_deletion_law.run_audit_deletion: operator-auth+admin+non-subagent+reason+audit-of-audit; local-only, remote-refused",
        "read",
    ),
    "execution_clear_tool_calls": (
        TIER_A,
        "audit_deletion_law",
        "audit_deletion_law.run_audit_deletion: operator-auth+admin+non-subagent+reason+audit-of-audit; local-only, remote-refused",
        "read",
    ),
    "circuit_breaker_reset": (TIER_L, "local_only", "mcp_server.py:2225 op reset, no auth", "read"),
    # registered name (def aidocs_admin_clear_reconnect, aidocs_ prefix stripped)
    "admin_clear_reconnect": (
        TIER_L,
        "local_only",
        "mcp_server.py:3107 conductor-env-only",
        "read",
    ),
    # runtime routing / managed-mode entrypoints (def aidocs_*, prefix stripped) —
    # host/session bootstrap routing, local-only standalone, remote-refused.
    "orchestrate": (
        TIER_L,
        "local_only",
        "server_session_tools.py:217 runtime routing entry",
        "read",
    ),
    "handle_prompt": (
        TIER_L,
        "local_only",
        "server_session_tools.py:352 prompt handling entry",
        "inspection",
    ),
    "route_prompt": (TIER_L, "local_only", "runtime prompt routing entry", "inspection"),
    "classify_prompt": (TIER_L, "local_only", "runtime prompt classify entry", "inspection"),
    "mode_get": (TIER_L, "local_only", "managed-mode read (session-bound)", "inspection"),
    "mode_set": (TIER_L, "local_only", "managed-mode bind (session-bound)", "inspection"),
    "mode_clear": (TIER_L, "local_only", "managed-mode unbind (session-bound)", "inspection"),
    # ── MUTATIONS / mixed, no proven remote binding → refused ──
    "db_query": (
        TIER_M,
        "self_flag_dry_run",
        "server_legacy_git_tools.py:338 allow_dml/dry_run, no AIDOCS auth",
        "read",
    ),
    "edit_rollback": (TIER_L, "local_only", "mcp_server.py:2534 local fs restore, no gate", "read"),
    "git_ops": (
        TIER_M,
        "code_runner_destructive_floor",
        "mcp_server.py → code_runner.ai_run, sealed by bash_policy.evaluate_destructive_floor (rm/sudo/dd/… + curl|sh) at _run_process; Tier-M, not remote-eligible",
        "read",
    ),
    "bump_agent_memory_epoch": (
        TIER_L,
        "local_only",
        "server_session_tools.py:250 host-plugin callback",
        "read",
    ),
    "ai_protect": (TIER_M, "none", "server_code_edit_tools.py protect-set mutation", "inspection"),
    "semantic_index_sync": (TIER_M, "none", "writes vector index", "inspection"),
    "context_compact": (TIER_L, "local_only", "host context compaction", "inspection"),
    "notifications_clear": (TIER_L, "local_only", "mcp_server.py:2236 host UI notif clear", "read"),
    "related_project_register": (TIER_A, "none", "cross-project register", "inspection"),
    "handoff_create": (TIER_M, "none", "handoff write", "inspection"),
    "session_start": (TIER_L, "local_only", "bootstrap probe", "inspection"),
    "ai_plan_pause": (TIER_M, "none", "plan state mutation", "inspection"),
    "ai_plan_expand": (TIER_M, "none", "plan state mutation", "inspection"),
    "ai_plan_template": (TIER_M, "none", "plan template read/write mixed", "inspection"),
    "workflow_action_satisfy": (TIER_M, "none", "workflow action mutation", "inspection"),
    "ai_todo": (TIER_M, "none", "todo mutation", "inspection"),
    "ai_backlog": (TIER_M, "none", "backlog mutation", "inspection"),
    "ai_lane_control": (
        TIER_L,
        "local_only",
        "conductor lane control (session-bound)",
        "inspection",
    ),
    "ai_lane_state": (TIER_L, "local_only", "conductor lane state (session-bound)", "inspection"),
    "ai_lane_exit": (TIER_L, "local_only", "conductor lane exit (session-bound)", "inspection"),
    # ── Registry-backed EDIT consolidators & bulk edits (2026-05-29
    #    king seal of edit-tool destructiveHint=false doctrine). These
    #    tools were previously destructiveHint=True at the host-hint
    #    level; after the carve-out closure they're destructiveHint=
    #    false and read-only=false → 'neither hint', which requires
    #    an explicit classification in this table so
    #    test_outer_gate_manifest::test_every_neither_hint_tool_has_
    #    explicit_override_evidence and ::test_no_tool_or_command_is_
    #    unclassified pass. AIDOCS still owns confirmation via the
    #    registry layer's confirm=TWO_PHASE + exact phrase.
    "ai_lane": (
        TIER_M,
        "registry_two_phase_confirm",
        "tool_interface.py:525 ai_lane consolidator; action-routed mutation; AIDOCS gate enforces tier_m_edit scope",
        "inspection",
    ),
    "ai_plan": (
        TIER_M,
        "registry_two_phase_confirm",
        "tool_interface.py:682 ai_plan consolidator; action-routed mutation; AIDOCS gate enforces tier_m_edit scope",
        "inspection",
    ),
    "ai_worker": (
        TIER_M,
        "registry_two_phase_confirm",
        "tool_interface.py:894 ai_worker consolidator; action-routed mutation; AIDOCS gate enforces tier_m_edit scope",
        "inspection",
    ),
    "ai_batch_edit": (
        TIER_M,
        "registry_two_phase_confirm",
        "tool_interface.py:1972 ai_batch_edit; confirm=TWO_PHASE phrase=confirm-batch-edit",
        "inspection",
    ),
    "ai_delete": (
        TIER_M,
        "registry_two_phase_confirm",
        "tool_interface.py:2029 ai_delete; confirm=TWO_PHASE phrase=confirm-delete {path}; trash-recovery + quota refusal + audit chain",
        "inspection",
    ),
    "ai_palace_maintenance": (
        TIER_M,
        "registry_two_phase_confirm",
        "tool_interface.py:2092 ai_palace_maintenance; confirm=TWO_PHASE phrase=confirm-palace-maintenance {mode}; dashboard-admin only",
        "inspection",
    ),
    "ai_skill": (TIER_M, "none", "skill manage mixed", "inspection"),
    "ai_soul": (TIER_M, "none", "soul/persona manage mixed", "inspection"),
    "ai_seat": (TIER_M, "none", "seat/identity manage mixed", "inspection"),
    "ai_qa": (TIER_M, "none", "qa/verify mixed (may mutate gates)", "inspection"),
    "ai_failures": (TIER_M, "none", "failure-stewardship list/dispose (mutates ledger)", "inspection"),
    "ai_test": (TIER_M, "none", "language-agnostic test runner (argv-only, shell=False; subagent-safe)", "inspection"),
    "ai_msg": (TIER_M, "none", "inter-agent message write", "inspection"),
    "ai_session": (TIER_M, "none", "session lifecycle mixed", "inspection"),
    "ai_project": (
        TIER_M,
        "none",
        "host_session→project binding lifecycle (bind/unbind/status/list); "
        "RBAC-gated, local, cross-user isolated — remote-refused",
        "inspection",
    ),
}

# data_scope exceptions (default is "project"); drives requires_project_binding.
_DATA_SCOPE: dict[str, str] = {
    "rbac_approve_escalation": "global",
    "rbac_deny_escalation": "global",
    "admin_clear_freeze": "session",
    "workflow_definition_add": "global",
    "workflow_definition_update": "global",
    "workflow_definition_remove": "global",
    "execution_prune": "project",
    "execution_clear_token_usage": "session",
    "execution_clear_tool_calls": "session",
    "execution_usage_by_identity": "project",
    "circuit_breaker_status": "global",
    "circuit_breaker_reset": "global",
    "mcp_registry_search": "global",
    "mcp_registry_get": "global",
    "metrics_snapshot": "global",
    "metrics_prometheus": "global",
    "related_project_register": "global",
    "aidocs_admin_clear_reconnect": "session",
    "bump_agent_memory_epoch": "session",
    "session_start": "session",
    "ai_lane_control": "session",
    "ai_lane_state": "session",
    "ai_lane_exit": "session",
    "orchestrate": "session",
    "handle_prompt": "session",
    "route_prompt": "session",
    "classify_prompt": "none",
    "mode_get": "session",
    "mode_set": "session",
    "mode_clear": "session",
    "admin_clear_reconnect": "session",
    "conductor_start": "session",
    "conductor_stop": "session",
    "conductor_send": "session",
    "conductor_output": "session",
    # promoted read tools whose scope is not the project tree
    "mcp_tool_catalog": "none",
    "files_touched": "session",
    "session_status_badge": "session",
    "edit_history_list": "session",
    "edit_history_grep": "session",
}

_DESTRUCTIVE_DEFAULT = (
    "destructive_unverified",
    "destructiveHint=True; remote binding not yet proven",
)

_CLI_CLASS_MAP: dict[str, tuple[str, str]] = {
    "read_only": (TIER_R, "read_inspection"),  # CLI reads: not line-proven here
    "user_safe": (TIER_M, "cli_self_gated"),
    "authenticated_user": (TIER_A, "operator_auth"),
    "admin_only": (TIER_A, "operator_auth"),
    "mixed_self_gated": (TIER_A, "operator_auth"),
}


# ── the rules (single source of truth) ─────────────────────────────────────
def compute_needs_review(tier: str, binding: str, verified: str, duplicated: bool) -> bool:
    if duplicated or tier == TIER_UNKNOWN:
        return True
    if tier == TIER_R and verified != "read":
        return True  # readOnlyHint / inspection overtrust — unproven
    return False


def compute_remote_eligible(tier: str, binding: str, verified: str, duplicated: bool) -> bool:
    if duplicated or compute_needs_review(tier, binding, verified, duplicated):
        return False
    if tier == TIER_R:
        return verified == "read"
    if tier in (TIER_M, TIER_A):
        return binding in PROVEN_BINDINGS
    return False  # L, unknown


# ── authoritative discovery (glob, not a manual list) ─────────────────────
def _mcp_tool_files(base: Path) -> list[Path]:
    files = [base / "mcp_server.py"]
    files += sorted(base.glob("server_*_tools.py"))
    return [f for f in files if f.is_file()]


def _decorator_is_server_tool(dec: ast.expr) -> tuple[bool, dict]:
    call = dec if isinstance(dec, ast.Call) else None
    func = call.func if call else dec
    is_tool = (
        isinstance(func, ast.Attribute)
        and func.attr == "tool"
        and isinstance(func.value, ast.Name)
        and func.value.id == "server"
    )
    anns: dict[str, Any] = {}
    if is_tool and call:
        for kw in call.keywords:
            if kw.arg == "annotations" and isinstance(kw.value, ast.Dict):
                for k, v in zip(kw.value.keys, kw.value.values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        anns[str(k.value)] = v.value
    return is_tool, anns


def discover_mcp_tools_raw(pkg: Path | None = None) -> list[dict]:
    """EVERY ``@server.tool`` occurrence (duplicates included)."""
    base = pkg or PKG
    out: list[dict] = []
    for path in _mcp_tool_files(base):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            anns: dict[str, Any] = {}
            is_tool = False
            for dec in node.decorator_list:
                ok, a = _decorator_is_server_tool(dec)
                if ok:
                    is_tool = True
                    anns = a or anns
            if not is_tool:
                continue
            name = node.name
            if name in HIDDEN_UNREGISTERED:
                continue  # @server.tool-shaped but not registered
            name = name.removeprefix(_REGISTER_PREFIX_STRIP)  # registered name
            out.append(
                {
                    "name": name,
                    "file": path.name,
                    "line": node.lineno,
                    "readonly": bool(anns.get("readOnlyHint")),
                    "destructive": bool(anns.get("destructiveHint")),
                    "params": [a.arg for a in node.args.args if a.arg != "self"],
                },
            )
    return out


def duplicate_tool_names(pkg: Path | None = None) -> dict[str, list[str]]:
    """Name → [file:line, …] for every name registered more than once."""
    seen: dict[str, list[str]] = {}
    for r in discover_mcp_tools_raw(pkg):
        seen.setdefault(r["name"], []).append(f"{r['file']}:{r['line']}")
    return {n: locs for n, locs in seen.items() if len(locs) > 1}


def discover_mcp_tools(pkg: Path | None = None) -> dict[str, dict]:
    """Deduped name → info (last occurrence wins, matching FastMCP). Use
    ``duplicate_tool_names`` to detect the collapse this hides.
    """
    out: dict[str, dict] = {}
    for r in discover_mcp_tools_raw(pkg):
        out[r["name"]] = r
    return out


def discover_registered_tools() -> set[str] | None:
    """Best-effort cross-check against the LIVE FastMCP registry. Returns the
    registered tool-name set, or None if the server can't be built offline.
    Lets a deps-complete CI assert AST-discovery ⊇ registry (no decorator the
    AST scan would miss).
    """
    try:
        from .mcp_server import create_server
        from .mcp_server_runtime_helpers import registered_tools

        server = create_server(dashboard_mode=True)
        names = set()
        for comp in registered_tools(server) or []:
            n = getattr(comp, "name", None) or getattr(comp, "key", None)
            if n:
                names.add(str(n))
        return names or None
    except Exception:
        return None


def discover_cli_commands() -> dict[str, str]:
    from .cli import COMMANDS
    from .operator_auth_service import CLI_COMMAND_CLASS

    out: dict[str, str] = {}
    for name in COMMANDS:
        if name in ("--version", "-v"):
            continue
        out[name] = CLI_COMMAND_CLASS.get(name, TIER_UNKNOWN)
    return out


# ── covenants (invoke-time preconditions) ─────────────────────────────────
def _covenants(tier: str, name: str, remote_eligible: bool) -> dict:
    scope = _DATA_SCOPE.get(name, "project")
    mutating_or_admin = tier in (TIER_M, TIER_A)
    return {
        "requires_auth": bool(remote_eligible or mutating_or_admin),
        "requires_audit": bool(remote_eligible or mutating_or_admin),
        "requires_project_binding": scope == "project",
        "data_scope": scope,
    }


# ── manifest assembly ─────────────────────────────────────────────────────
def _mcp_entry(name: str, info: dict, duplicated: bool) -> dict:
    src = f"{info['file']}:{info['line']}"
    if name in MCP_TIER_OVERRIDES:
        tier, binding, evidence, verified = MCP_TIER_OVERRIDES[name]
    elif info["readonly"]:
        tier, binding, evidence, verified = (
            TIER_R,
            "decorator_readonly",
            "readOnlyHint=True",
            "decorator",
        )
    elif info["destructive"]:
        tier, binding, evidence, verified = (
            TIER_M,
            _DESTRUCTIVE_DEFAULT[0],
            _DESTRUCTIVE_DEFAULT[1],
            "decorator",
        )
    else:
        tier, binding, evidence, verified = (TIER_UNKNOWN, "none", "no hint, no override", "none")
    needs_review = compute_needs_review(tier, binding, verified, duplicated)
    eligible = compute_remote_eligible(tier, binding, verified, duplicated)
    return {
        "name": name,
        "kind": "mcp_tool",
        "source": src,
        "tier": tier,
        "enforcement_binding": binding,
        "binding_evidence": evidence,
        "verified": verified,
        "schema": {"params": info["params"]},
        "duplicated": duplicated,
        "needs_review": needs_review,
        "remote_eligible": eligible,
        **_covenants(tier, name, eligible),
    }


def _cli_entry(name: str, klass: str) -> dict:
    tier, binding = _CLI_CLASS_MAP.get(klass, (TIER_UNKNOWN, "none"))
    verified = "cli_class"
    needs_review = compute_needs_review(tier, binding, verified, False)
    eligible = compute_remote_eligible(tier, binding, verified, False)
    return {
        "name": name,
        "kind": "cli_command",
        "source": "cli.py:COMMANDS",
        "tier": tier,
        "enforcement_binding": binding,
        "binding_evidence": f"CLI_COMMAND_CLASS={klass}",
        "verified": verified,
        "cli_class": klass,
        "schema": {},
        "duplicated": False,
        "needs_review": needs_review,
        "remote_eligible": eligible,
        **_covenants(tier, name, eligible),
    }


def _registry_only_entry(name: str) -> dict:
    tier, binding, evidence, verified = REGISTRY_ONLY_TOOLS[name]
    needs_review = compute_needs_review(tier, binding, verified, False)
    eligible = compute_remote_eligible(tier, binding, verified, False)
    return {
        "name": name,
        "kind": "mcp_tool",
        "source": "programmatic_registration",
        "tier": tier,
        "enforcement_binding": binding,
        "binding_evidence": evidence,
        "verified": verified,
        "schema": {"params": []},
        "duplicated": False,
        "needs_review": needs_review,
        "remote_eligible": eligible,
        **_covenants(tier, name, eligible),
    }


def build_manifest(pkg: Path | None = None) -> list[dict]:
    dups = duplicate_tool_names(pkg)
    entries = [
        _mcp_entry(n, info, n in dups) for n, info in sorted(discover_mcp_tools(pkg).items())
    ]
    entries += [_registry_only_entry(n) for n in sorted(REGISTRY_ONLY_TOOLS)]
    entries += [_cli_entry(n, k) for n, k in sorted(discover_cli_commands().items())]
    return entries


# ── law-reviewed proof cache for the manifest (see PERFORMANCE_DOCTRINE Art VII)
# build_manifest is a pure function of the package SOURCE; an AST scan of ~300
# files costs ~951 ms (MEASURED). It may be cached ONLY under a content
# fingerprint of that source, so a source change invalidates and no stale
# manifest can become Outer-Gate authority. build_manifest itself stays uncached
# (default behavior unchanged); callers opt in via build_manifest_cached.
from .proof_cache import FingerprintCache  # noqa: E402

_MANIFEST_CACHE = FingerprintCache("outer_gate_manifest")


def manifest_source_fingerprint() -> str | None:
    """Content fingerprint of the package source the manifest is derived from.
    None ⇒ cannot fingerprint ⇒ caller must recompute (fail-closed).
    """
    try:
        from .package_integrity import compute_package_fingerprint

        return compute_package_fingerprint().get("fingerprint")
    except Exception:
        return None


def build_manifest_cached(
    pkg: Path | None = None,
    *,
    fingerprint: str | None = "__auto__",
) -> list[dict]:
    """Cached `build_manifest`, keyed by the package SOURCE fingerprint. Returns
    a cached manifest ONLY on an exact fingerprint match; any source change (or
    an unfingerprintable source) recomputes. Verdict-identical to build_manifest
    — the cache key IS the content of the inputs. A custom `pkg` is never cached
    (its fingerprint is not the running package's).
    """
    if pkg is not None:
        return build_manifest(pkg)
    fp = manifest_source_fingerprint() if fingerprint == "__auto__" else fingerprint
    return _MANIFEST_CACHE.get_or_compute(fp, lambda: build_manifest(pkg))


def manifest_mcp_names(pkg: Path | None = None) -> set[str]:
    """The registered MCP tool name set the deed claims to cover (AST-discovered
    + programmatic registry-only). Compared to the live registry for parity.
    """
    return set(discover_mcp_tools(pkg)) | set(REGISTRY_ONLY_TOOLS)


def remote_manifest(pkg: Path | None = None) -> list[dict]:
    return [e for e in build_manifest(pkg) if e["remote_eligible"]]


def needs_review_manifest(pkg: Path | None = None) -> list[dict]:
    return [e for e in build_manifest(pkg) if e["needs_review"]]


def unclassified(pkg: Path | None = None) -> list[dict]:
    return [e for e in build_manifest(pkg) if e["tier"] == TIER_UNKNOWN]


# ── promoted remote-path invariants (local, tested; NO network) ───────────
def resolve_remote_principal(identity: dict | None) -> dict:
    """Fail-closed identity for the remote path. The local audit fallback
    (execution_index_store.py:516-519) DEFAULTS unresolved identity to
    operator/super_admin; the remote path MUST refuse instead.
    """
    if not isinstance(identity, dict) or not identity:
        return {"ok": False, "reason": "no_authenticated_identity"}
    uid = str(identity.get("user_id") or "").strip()
    role = str(identity.get("effective_role") or "").strip()
    if not uid or not role:
        return {"ok": False, "reason": "incomplete_identity"}
    if role == "super_admin" and not identity.get("authenticated"):
        return {"ok": False, "reason": "super_admin_requires_authentication"}
    return {"ok": True, "user_id": uid, "role": role}


def audit_or_refuse(record_callable: Any) -> dict:
    """Remote-path invariant: audit is MANDATORY. If recording raises, REFUSE
    rather than proceed unaudited.
    """
    try:
        record_callable()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"audit_failed:{type(exc).__name__}"}
