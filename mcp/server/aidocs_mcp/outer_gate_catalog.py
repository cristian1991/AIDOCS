"""Single canonical tool catalog/resolver for local MCP + the Outer Gate.

ONE source of truth. tools/list, tool_catalog, tool_capabilities, and the
tools/call routing all derive visibility / executable_now / blocked_by /
grant_required from `resolve()` here, so the advertised remote surface can never
drift from what is actually callable:

  * the executor read set (READ_EXEC_ALLOWLIST) — what the gate can really run —
    is THE advertised-invokable read set (a manifest-eligible tool that is NOT
    wired into the executor is shown in the full catalog but NOT advertised as
    invokable, with blocked_by=tool_not_exec_allowlisted — honest, not hidden);
  * surgical edit tools (EDIT_ALLOWLIST) are class=edit (tier_m_edit grant);
  * project/session selectors are class=selector (catalog scope);
  * GitHub import/sync are class=import (tier_m_edit grant);
  * remote-eligible Tier-A and unwired reads are DISCOVERABLE (in the full
    catalog) but not invokable, with an accurate blocked_by;
  * everything else (not remote-eligible) is class=internal — never advertised.

Execution still flows through role/scope/grant/project-binding/audit/confirm/
reindex law; this module only decides truthful catalog metadata.
"""

from __future__ import annotations

from .outer_gate_edit import EDIT_ALLOWLIST
from .outer_gate_executor import READ_EXEC_ALLOWLIST, RUN_ALLOWLIST

# ── classes ──────────────────────────────────────────────────────────────────
CLASS_READ = "read"  # executor-wired Tier-R read (invokable)
CLASS_EDIT = "edit"  # surgical Tier-M edit (two-phase, tier_m_edit)
CLASS_RUN = "run"  # detached shell via canonical ai_run (project_run)
CLASS_SELECTOR = "selector"  # project/session/catalog read tool
CLASS_IMPORT = "import"  # GitHub register/sync (tier_m_edit)
CLASS_DISCOVERABLE = "discoverable"  # remote-eligible but not invokable here
CLASS_INTERNAL = "internal"  # not remote-eligible — never advertised remotely

# The ONE carve-out invisible to BOTH local MCP and the gate: @server.tool
# functions that are folded-into-dispatcher helpers / never agent-facing. This is
# the single owner — mcp_server imports it (no separate hand-maintained list), so
# local list_tools and the gate share the same hidden set. NOTE: this is NOT the
# same as CLASS_INTERNAL — control/config/lane tools are internal to the REMOTE
# surface but remain fully visible+usable LOCALLY (local is full-trust); only
# HIDDEN_EVERYWHERE is hidden on both.
HIDDEN_EVERYWHERE = frozenset(
    {
        "ai_find_symbol_range",
        "ai_extract_block",
        "ai_extract_symbol",
        "ai_preview_extraction_deps",
        "ai_refactor_extract",
        "ai_suggest_extractions",
        # Empire 2026-06-20: never agent-facing on EITHER surface (local stdio or gate).
        # Host/desktop UI tools the agent cannot drive + operator-only authority an
        # agent must never self-serve (self-approving an escalation / granting itself
        # membership is the control-plane breach to prevent). The operator drives these
        # from the dashboard, not the agent tool surface. Complements the PROJECT_TOOLS
        # exclusion (_NOT_AGENT_ADVERTISED) so both surfaces drop them.
        "tauri_todo_list",
        "tauri_backlog_list",
        "approve_escalation",
        "deny_escalation",
        # War 0 (2026-07-13): freeze recovery + surfacing are operator dashboard
        # ops — same doctrine as escalation approve/deny (an agent must never
        # see or self-serve the lockdown controls it may itself be under).
        "clear_freeze",
        "freeze_list",
        "escalation_list",
        "project_grant_member",
        "project_revoke_member",
        "project_acl_list",
    },
)

# LOCAL-ONLY: registered as `@server.tool` (so the local stdio agent — Claude
# Code etc. — can call them under full-trust), but explicitly REFUSED on the
# outer gate's `tools/list` and `tools/call`. Distinct from `HIDDEN_EVERYWHERE`
# (which hides on both surfaces) and from `CLASS_INTERNAL` (which is the
# fallthrough verdict for anything we forgot to class). Rationale per bucket
# below; every entry is non-negotiable for the gate surface.
LOCAL_ONLY = frozenset(
    {
        # ── Admin recovery ────────────────────────────────────────────────
        # A remote OAuth-authenticated token holder MUST NOT be able to clear
        # their own freeze or reconnect-lock; those are physical-access
        # operator escapes by design. `admin_clear_freeze` additionally
        # requires a two-phase confirm phrase even LOCALLY (see cli.py).
        "admin_clear_freeze",
        "admin_clear_reconnect",
        # ── Gate / IPC plumbing (not really "tools") ──────────────────────
        # Helpers the gate itself calls internally; exposing them remotely is
        # noise and a footgun (e.g. a remote actor clearing locks).
        "ai_gate_msg",  # gate↔gate message bus
        "ai_concurrency_reset",  # clears live locks; misuse = invariant break
        "bump_agent_memory_epoch",  # cache-bust primitive
        "ai_preflight",  # internal pre-call probe
        "ai_resolve_backend",  # internal helper
        "ai_resolve_scope",  # internal helper
        "verification_gate",  # internal contract check
        # ── Skill subsystem internals ────────────────────────────────────
        # `ai_skill` (invocation) IS the agent surface and remains gate-
        # exposed; the registry/scan/state probes are infra.
        "skill_registry_get",
        "skill_scan",
        "skill_trigger_state_get",
        # ── Host-local UX ────────────────────────────────────────────────
        # config_get (read-only) + config_set are gate-exposed CONTROL-PLANE ops
        # (handle_project_tool): config_set is op #3 — org OWNER/ADMIN/super_admin +
        # two-phase confirm (Empire 2026-06-20). The local stdio host-config writer is a
        # separate @server.tool. ai_notifications_clear stays host-local operator UX.
        "ai_notifications_clear",  # operator UX on the host
        # ai_backlog promoted to the gate (Empire 2026-06-20) — read-only backlog list.
    },
)

# Dashboard Tauri IPC (backlog #288, conductor increment): registered ONLY on
# the dashboard profile (`@_dash_tool` in mcp_server.py, dashboard_mode=True)
# so the dashboard can drive the conductor CLI subprocess via tools/call — but
# they are NOT agent tools (not in tool_interface's ai_*(mode) catalog) and
# must never appear in ANY agent-facing tools/list, dashboard profile included.
# LIST-hidden only: putting them in HIDDEN_EVERYWHERE would make mcp_server's
# _taxonomy_tool SKIP their registration entirely and break the dashboard IPC.
# Mirrors outer_gate_manifest.REGISTRY_ONLY_TOOLS (Tier-L local_only);
# test_conductor_ipc_hidden pins the two sets against drift.
DASHBOARD_IPC_TOOLS = frozenset(
    {
        "conductor_start",
        "conductor_send",
        "conductor_stop",
        "conductor_output",
    },
)


def is_local_only(name: str) -> bool:
    """True iff `name` is registered for the local agent but must be
    refused on the outer-gate's remote surface. See `LOCAL_ONLY` for the
    rationale per entry. Distinct from `local_hidden` (which hides on
    BOTH surfaces).
    """
    return name in LOCAL_ONLY


# Memory-war unify (2026-07-16): registered @server.tool impls whose AGENT
# surface is GATE_ONLY. They stay REGISTERED (the gate's read executor +
# the dashboard tools/call route keep working) but drop off the local stdio
# tools/list — on local, memory retrieval is automagic (UPS surfacer +
# ai_recall/ai_memory); a palace retrieval tool on the local list would be
# a second, contradicting surface for the same internal projection.
GATE_SERVED_ONLY_IMPLS = frozenset(
    {
        "ai_palace_search",
        "ai_palace_status",
    }
)

# Memory-war unify (2026-07-16): the four legacy memory_* impls stay
# REGISTERED as internal _delegate targets of the ai_memory consolidator but
# are NOT part of any agent surface — hidden from the agent (full-profile)
# tools/list; the gate never advertises/executes them directly (dropped from
# the registry + allowlists in the same campaign).
CONSOLIDATOR_DELEGATE_IMPLS = frozenset(
    {
        "memory_capture",
        "memory_read",
        "memory_search",
        "memory_promote",
        # SSOT-05 #386/#359 (2026-07-18): the lane/plan deprecation window
        # CLOSED. The 8 ai_lane_* + 13 ai_plan_* legacy siblings stay
        # REGISTERED as internal _delegate targets of the ai_lane / ai_plan
        # consolidators (C.20 direct dispatch + dashboard tools/call keep
        # working) but drop off the agent (full-profile) tools/list — the
        # consolidators are the ONLY advertised surface for these concerns.
        # Gate posture is unchanged: these names were never gate-advertised
        # (CLASS_INTERNAL fallthrough; see DOCTRINE.md tool table).
        "ai_lane_agents",
        "ai_lane_control",
        "ai_lane_exit",
        "ai_lane_grant",
        "ai_lane_inbox",
        "ai_lane_send",
        "ai_lane_state",
        "ai_lane_summary",
        "ai_plan_create",
        "ai_plan_dispatch",
        "ai_plan_expand",
        "ai_plan_graph",
        "ai_plan_mark_ready",
        "ai_plan_overlap",
        "ai_plan_pause",
        "ai_plan_reopen",
        "ai_plan_report",
        "ai_plan_resume",
        "ai_plan_signal",
        "ai_plan_status",
        "ai_plan_template",
    }
)


def _registry_hidden_surface(name: str) -> bool:
    """True iff the tool_interface registry declares `name` with
    surface=HIDDEN — the SSOT expression of "registered but invisible on
    every agent surface" (#386/#288: internal plumbing must not leak into
    agent-facing tools/list). Lazy import mirrors classify()'s pattern."""
    from . import tool_interface as _ti

    spec = _ti.get(name)
    return spec is not None and spec.surface == _ti.HIDDEN


def agent_list_hidden(name: str) -> bool:
    """Hidden from the AGENT'S (full-profile) stdio tools/list specifically:
    everything local_hidden hides, plus gate-served-only impls, the
    consolidators' internal delegate targets, and any registry spec declared
    surface=HIDDEN (#288 visibility law). The gate's READ-EXECUTOR build
    (read_only profile) deliberately does NOT apply this — it must list what
    the gate executes (e.g. ai_palace_search for the dashboard route)."""
    return (
        local_hidden(name)
        or name in GATE_SERVED_ONLY_IMPLS
        or name in CONSOLIDATOR_DELEGATE_IMPLS
        or _registry_hidden_surface(name)
    )


def local_hidden(name: str) -> bool:
    """True iff `name` must be hidden from the LOCAL agent's tool list too:
    the single invisible-to-both carve-out (HIDDEN_EVERYWHERE), plus the
    dashboard Tauri IPC set (DASHBOARD_IPC_TOOLS — list-hidden even on the
    dashboard profile while STAYING registered so the dashboard's tools/call
    keeps working). Agent-surface-only hides (GATE_SERVED_ONLY_IMPLS +
    CONSOLIDATOR_DELEGATE_IMPLS) live in agent_list_hidden — the gate's
    read-executor list must keep them. Local shows everything else it
    registers (full-trust); the gate applies its own remote classification.
    """
    return name in HIDDEN_EVERYWHERE or name in DASHBOARD_IPC_TOOLS


# ── MCP tool annotations per class ──────────────────────────────────────────
# DOCTRINE (2026-05-26): host annotations are UX HINTS ONLY — AIDOCS remains the
# authority. The host (ChatGPT/OpenAI) must NOT pop a "destructive, click to
# confirm" card for every powerful tool: actual dangerous effects are enforced at
# tools/call by scope + selected-project binding + heuristic judge + bash_policy +
# confirm-token + audit + reindex + T0/catch-confirmable gates. So `destructiveHint`
# is reserved for tools that are INHERENTLY destructive at the host-annotation
# level — none of the advertised classes are. A truly dangerous `ai_run` command
# (rm -rf, curl|sh, git reset --hard) is still refused/confirm-gated by the runtime
# judge regardless of this hint. See the MCP spec on annotations for `tools/list`.
def _annotations(cls: str, name: str) -> dict:
    """Return MCP tool annotations dict for a tool's class and name.

    Doctrine 2026-05-29 (Empire re-seal): when the tool is registry-
    backed (in tool_interface._TOOLS) AND its @tool decorator declared
    a `title`, surface that title alongside the class-default hint
    keys. The registry is the single source of truth for human-
    readable labels; the helper just plumbs it through so callers
    that compare catalog.annotations to helper(class,name) see
    the same shape. Non-registry tools and registry tools without
    titles return the legacy 4-key dict unchanged.
    """
    base = _annotations_for_class(cls, name)
    try:
        from . import tool_interface as _ti  # local import to dodge cycles

        _spec = _ti._TOOLS.get(name)
    except Exception:
        _spec = None
    if _spec is not None and _spec.annotations:
        # SSOT-04 (2026-07-15, completes the 2026-05-29 title plumb): a
        # gate-visible registry row's DECLARED annotations override the
        # class defaults key-by-key — the @tool decorator is the single
        # source for hints and title alike, and helper == catalog holds by
        # construction. Class defaults remain the base for keys a row omits
        # and the full answer for undeclared (legacy static) tools.
        if _spec.surface in ("both", "gate_only"):
            base = {**base, **_spec.annotations}
        else:
            _title = _spec.annotations.get("title")
            if _title:
                base = {**base, "title": _title}
    return base


def _annotations_for_class(cls: str, name: str) -> dict:
    """Class-default MCP toolAnnotations — the legacy helper. Kept
    as an inner function so _annotations() can layer registry-derived
    extras on top without duplicating the per-class branches.
    """
    if cls == CLASS_READ:
        return {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    if cls == CLASS_SELECTOR:
        # project_select / session_select are the "connect" actions: they BIND the
        # project/session context that every later tool call operates against.
        # These are the ONLY tools that request host confirmation
        # (destructiveHint=true) — a connection change is a deliberate operator
        # act. Every OTHER tool is non-destructive at the host level; AIDOCS
        # checks real danger internally at tools/call, so nothing else should
        # pop a confirmation card.
        is_connect = name in ("project_select", "session_select")
        # session_create writes (scaffolds a session dir) so it is NOT read-only,
        # but it is idempotent and never overwrites — so not destructive and no
        # host confirmation card (unlike the connect/bind actions above).
        is_write = name == "session_create"
        return {
            "readOnlyHint": not (is_connect or is_write),
            "destructiveHint": is_connect,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    if cls == CLASS_EDIT:
        # ai_str_replace: NOT read-only, but destructiveHint=false — AIDOCS's
        # two-phase confirm-token owns edit confirmation (a propose returns a diff
        # + token; the commit needs the exact token). Closed-world (bounded to the
        # selected project), so openWorldHint=false.
        return {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    if cls == CLASS_RUN:
        # ai_run/_output/_kill: NOT read-only, destructiveHint=false — the runtime
        # judge / bash_policy / T0 confirm-gates own dangerous-command refusal, so
        # the host should not blanket-confirm every run. openWorldHint=true (a shell
        # command can reach outside the indexed corpus).
        return {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    if cls == CLASS_IMPORT:
        # project_import/_sync: clone+index a repo — powerful but not destructive at
        # the annotation level (project_import grant + readiness gate enforce).
        return {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    # Fallback (discoverable/internal — NOT advertised in tools/list, so this never
    # reaches a host card): conservative non-destructive default.
    return {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }


# ── Better descriptions for well-known tools ─────────────────────────────────
_READ_DESC: dict[str, str] = {
    "ai_get_outline": "Read a structured outline of a source file in the "
    "selected AIDOCS project. No file changes.",
    "ai_get_symbol_info": "Read metadata for a symbol from the selected "
    "indexed project. No file changes.",
    "ai_get_symbol_snippet": "Read the source snippet for a symbol from the "
    "selected indexed project. No file changes.",
    "ai_investigate": "Read-only investigation helper over the selected "
    "project index. No file changes.",
    "ai_find": "Search the selected project codebase by symbol, text, "
    "pattern, or structure. No file changes.",
    "ai_bundle": "File/subsystem structural overview over the selected project. No file changes.",
    "ai_search": "Find files by name or content summary in the selected project. No file changes.",
    "ai_text_search": "Full-text search over the indexed project source code. No file changes.",
    "ai_trace": "Trace field, service, or component flow across the "
    "selected project. No file changes.",
    "ai_get_dependencies": "Read import/dependency edges for a file in "
    "the selected project. No file changes.",
}

# ── project/session selector + GitHub tool specs (moved here to be the single
#    owner; the transport imports these) ───────────────────────────────────────
_S_OBJ = {"type": "object", "properties": {}}
_OUTPUT_OBJ = {"type": "object", "additionalProperties": True}


def _read_inputschema(raw: dict | None) -> dict:
    """Normalize a manifest read-tool schema into VALID JSON Schema.

    The manifest records read params as ``{"params": [names]}`` (AST-inspected,
    types unknown — see outer_gate_manifest._entry). That shape is NOT valid
    JSON Schema, so an MCP host (ChatGPT) registers the tool but cannot build a
    correct ``tools/call`` — it can't see that the input is an object or which
    keys exist. Wrap the param names as permissive object properties so the host
    gets a real object schema (types stay open via additionalProperties). A
    schema that is ALREADY a proper object schema is passed through untouched.
    """
    if isinstance(raw, dict) and raw.get("type") == "object":
        return raw
    names = []
    if isinstance(raw, dict):
        names = [n for n in (raw.get("params") or []) if isinstance(n, str)]
    return {
        "type": "object",
        "properties": {n: {} for n in names},
        "additionalProperties": True,
    }
PROJECT_TOOL_SPECS: dict[str, dict] = {
    "project_list": {
        "desc": "List gate projects (id, name, source, default, current).",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "project_current": {
        "desc": "Show the token's selected project + status.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "dashboard_snapshot": {
        "desc": (
            "Full dashboard snapshot (overview, sessions, execution, tool calls, "
            "monitoring) for the gate's project — read-only. Powers the desktop "
            "dashboard's WebMCP (cloud) scope."
        ),
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "org_list": {
        "desc": (
            "List the orgs you belong to (org_id, role in that org, webmcp-entitled). "
            "These are the choices for org_select — which org this token acts as."
        ),
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "org_select": {
        "desc": (
            "Bind this token to one of YOUR orgs (the tenant it acts as). All project "
            "reads/edits/registry + per-org config then scope to that org. A user may "
            "belong to several orgs (owner of one, member of another); this picks which "
            "one. Two-phase confirm: first call WITHOUT confirm_token returns "
            "confirm_required + the exact confirm_token to echo; the assistant relays "
            "it to the user and only proceeds with the token after they agree."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "org_id": {"type": "string"},
                "confirm_token": {
                    "type": "string",
                    "description": (
                        "Required to apply. The speakable phrase 'confirm select org' "
                        "(case/punctuation/space-insensitive). Omit on the first call to "
                        "receive it in the confirm_required response. org_id also accepts "
                        "the human org NAME."
                    ),
                },
            },
            "required": ["org_id"],
        },
        "cls": CLASS_SELECTOR,
    },
    "project_select": {
        "desc": (
            "Bind this token to a registered project. CHANGES the "
            "execution context that every later tool call operates "
            "against (file reads, search, exec, edits all retarget). "
            "Two-phase confirm: first call WITHOUT confirm_token "
            "returns confirm_required + the exact confirm_token to "
            "echo; the assistant MUST relay that prompt to the user "
            "verbatim and only proceed with the confirm_token after "
            "the user agrees."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "confirm_token": {
                    "type": "string",
                    "description": (
                        "Required to apply. Echo the exact opaque token returned "
                        "by the first call after the user approves. Never guess it; "
                        "omit confirm_token on the first call to receive the "
                        "token in the confirm_required response."
                    ),
                },
            },
            # project_id accepts the human project NAME (resolved + disambiguated)
            # or a raw ogp_<hex> id.
            "required": ["project_id"],
        },
        "cls": CLASS_SELECTOR,
    },
    "project_status": {
        "desc": "Trust/index status of the current/given project.",
        "schema": {"type": "object", "properties": {"project_id": {"type": "string"}}},
        "cls": CLASS_SELECTOR,
    },
    "project_index_status": {
        "desc": "Index readiness (code_files, stale).",
        "schema": {"type": "object", "properties": {"project_id": {"type": "string"}}},
        "cls": CLASS_SELECTOR,
    },
    # M3 intra-org project allowlists — OWNER/ADMIN-only authority tools. The
    # handler hard-gates these on org_role (OWNER/ADMIN or super_admin); a
    # non-admin member is refused org_admin_required. Classified SELECTOR so they
    # need only the basic `catalog` scope every connected operator holds — the org
    # ROLE, not the scope, is the authority here.
    "project_grant_member": {
        "desc": (
            "OWNER/ADMIN only: grant an org member access to a project (adds it to "
            "their allowlist so project_list/project_select surface it). The member "
            "must belong to THIS org (cross-org grants are refused)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "member_user_id": {"type": "string"},
                "project_id": {"type": "string"},
            },
            "required": ["member_user_id", "project_id"],
        },
        "cls": CLASS_SELECTOR,
    },
    "project_revoke_member": {
        "desc": (
            "OWNER/ADMIN only: revoke an org member's access to a project (removes it "
            "from their allowlist)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "member_user_id": {"type": "string"},
                "project_id": {"type": "string"},
            },
            "required": ["member_user_id", "project_id"],
        },
        "cls": CLASS_SELECTOR,
    },
    "project_acl_list": {
        "desc": (
            "OWNER/ADMIN only: list the org's project allowlist grants "
            "(member_user_id x project_id), with project names; stale grants are "
            "pruned on read."
        ),
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "config_view": {
        "desc": (
            "OWNER/ADMIN only: view the bound org's config settings (read-only, with "
            "scope + metadata). Tenant-scoped + audited."
        ),
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "dashboard_view": {
        "desc": (
            "OWNER/ADMIN only: read a control-plane panel view of the bound org "
            "(view in rbac | bash_policy | config). Tenant-scoped + audited; read-only."
        ),
        "schema": {
            "type": "object",
            "properties": {"view": {"type": "string"}},
            "required": ["view"],
        },
        "cls": CLASS_SELECTOR,
    },
    # #768: execution routing remains in PROJECT_TOOLS while the public
    # schema/description/annotations are owned by tool_interface.config_set.
    "config_set": {"cls": CLASS_SELECTOR},
    "session_delete": {
        "desc": (
            "OWNER/ADMIN only: permanently delete a session workspace in the bound "
            "project. Two-phase confirm (confirm_token='confirm session delete'); "
            "path-confined + audited."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["session_id"],
        },
        "cls": CLASS_SELECTOR,
    },
    # ── Web-dashboard read panels (project/tenant-scoped, read-only) ──
    "skill_scan_results": {
        "desc": "Read the bound project's skills with per-skill security scan + activation tags.",
        "schema": {"type": "object", "properties": {"sessionId": {"type": "string"}}},
        "cls": CLASS_SELECTOR,
    },
    "list_mcp_servers": {
        "desc": "List MCP servers installed in the bound project.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "mcp_registry_search": {
        "desc": "Search the public MCP registry for installable servers.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "cursor": {"type": "string"},
            },
        },
        "cls": CLASS_SELECTOR,
    },
    "vocab_list_kinds": {
        "desc": "List the available intent-vocab kinds.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "vocab_list_langs": {
        "desc": "List the languages present in the bound tenant's intent vocab.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "vocab_get_grouped": {
        "desc": "Read the bound tenant's intent-vocab groups for a (kind, lang).",
        "schema": {
            "type": "object",
            "properties": {"kind": {"type": "string"}, "lang": {"type": "string"}},
            "required": ["kind", "lang"],
        },
        "cls": CLASS_SELECTOR,
    },
    "tauri_backlog_list": {
        "desc": "List the bound project's backlog items.",
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        "cls": CLASS_SELECTOR,
    },
    "tauri_todo_list": {
        "desc": "List the bound project's todos for a session or task.",
        "schema": {
            "type": "object",
            "properties": {"sessionId": {"type": "string"}, "taskId": {"type": "string"}},
        },
        "cls": CLASS_SELECTOR,
    },
    "broken_references": {
        "desc": (
            "Ref-integrity report for the bound project: references whose token has no "
            "resolving definition (heuristic, regex-extracted, truth-labeled)."
        ),
        "schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
        "cls": CLASS_SELECTOR,
    },
    "lane_scope": {
        "desc": (
            "Per-lane conductor scope for the lane-detail accordion: the effective "
            "allowed tools + the lane's owned files."
        ),
        "schema": {
            "type": "object",
            "properties": {"sessionId": {"type": "string"}, "laneId": {"type": "string"}},
        },
        "cls": CLASS_SELECTOR,
    },
    # ── Memory knowledge-graph page (dashboard-war d, #200) ──
    # 2026-07-03 regression fix: these were handled in handle_project_tool but
    # never cataloged — tools/call only routes `name in PROJECT_TOOLS` there,
    # so the live Memory page failed with unknown_tool.
    "memory_kg_graph": {
        "desc": "Read the bound project's memory knowledge graph (memories, code units, keywords, edges).",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "memory_kg_get": {
        "desc": "Read ONE memory's full body (title/kind/content) by its canonical path.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "cls": CLASS_SELECTOR,
    },
    "dashboard_memory_capture": {
        "desc": (
            "Org OWNER/ADMIN only: governed memory write for the dashboard capture "
            "form — delegates to the SAME MemoryStore.capture_memory doctrine path as "
            "the memory_capture agent tool (durability rubric, sovereign guard, "
            "sqlite-canonical). Two-phase confirm (confirm_token='confirm capture'); "
            "intent audited before mutation. Agents use memory_capture under "
            "tier_m_edit instead — this surface never widens that boundary."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "content": {"type": "string"},
                "target_hint": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["kind", "content"],
        },
        "cls": CLASS_SELECTOR,
    },
    "approve_escalation": {
        "desc": (
            "OWNER/ADMIN only: approve a pending escalation (MINTS a grant) and clear "
            "its session freeze. Approver = the verified principal; two-phase confirm "
            "(confirm_token='confirm approve'); audited."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "requestId": {"type": "string"},
                "reason": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["requestId"],
        },
        "cls": CLASS_SELECTOR,
    },
    "deny_escalation": {
        "desc": (
            "OWNER/ADMIN only: deny a pending escalation and clear its session freeze. "
            "Approver = the verified principal; two-phase confirm "
            "(confirm_token='confirm deny'); audited."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "requestId": {"type": "string"},
                "reason": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["requestId"],
        },
        "cls": CLASS_SELECTOR,
    },
    "clear_freeze": {
        "desc": (
            "OWNER/ADMIN only: clear ONE session freeze by its exact freeze_id "
            "(no grant minted) through the canonical ClearFreezeService. Requires "
            "rbac.admin_clear_freeze on the project, a non-empty reason, and a "
            "CONSUMABLE confirmation bound to (user, org, project, session, "
            "record, reason). Repeating a successful clear is harmless "
            "('no active record remains'). Audited."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "freeze_id": {"type": "string"},
                "reason": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["freeze_id", "reason"],
        },
        "cls": CLASS_SELECTOR,
    },
    "freeze_list": {
        "desc": (
            "OWNER/ADMIN only: list the bound project's ACTIVE session freezes "
            "for the dashboard control panel. Read-only, fail-closed {ok, items}."
        ),
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "escalation_list": {
        "desc": (
            "OWNER/ADMIN only: list the bound project's PENDING escalation "
            "requests for the dashboard approval queue. Read-only, fail-closed "
            "{ok, items}."
        ),
        "schema": {
            "type": "object",
            "properties": {"sessionId": {"type": "string"}},
        },
        "cls": CLASS_SELECTOR,
    },
    "session_list": {
        "desc": "List sessions in the selected project.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "session_create": {
        "desc": (
            "Create a new session in the currently selected project "
            "(scaffolds .MEMORY/sessions/<id>/ with SESSION.md, context, "
            "plans/) and BIND this token to it. Idempotent: an existing "
            "session id is bound, not overwritten. Provide session_id "
            "(a slug) or omit it to auto-generate one; optional goal seeds "
            "the session header."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": (
                        "New session slug ([A-Za-z0-9._-], <=128 chars). "
                        "Auto-generated when omitted."
                    ),
                },
                "goal": {
                    "type": "string",
                    "description": "Optional one-line goal for the new session.",
                },
            },
        },
        "cls": CLASS_SELECTOR,
    },
    "session_current": {
        "desc": "Show the token's current session id.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "session_select": {
        "desc": (
            "Bind this token to a session id within the currently "
            "selected project. CHANGES the session-scoped audit + "
            "memory context that every later tool call writes/reads "
            "against. Two-phase confirm: first call WITHOUT "
            "confirm_token returns confirm_required + the exact "
            "confirm_token to echo; the assistant MUST relay that "
            "prompt to the user verbatim and only proceed with the "
            "confirm_token after the user agrees."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "confirm_token": {
                    "type": "string",
                    "description": (
                        "Required to actually apply. The speakable phrase "
                        "'confirm select session' (case/punctuation/space-"
                        "insensitive). Omit on the first call to receive the "
                        "token in the confirm_required response."
                    ),
                },
            },
            "required": ["session_id"],
        },
        "cls": CLASS_SELECTOR,
    },
    "tool_catalog": {
        "desc": "The canonical tool catalog with honest metadata.",
        "schema": _S_OBJ,
        "cls": CLASS_SELECTOR,
    },
    "tool_capabilities": {
        "desc": "Tier/binding/schema + executable_now for a tool.",
        "schema": {
            "type": "object",
            "properties": {"tool": {"type": "string"}},
            "required": ["tool"],
        },
        "cls": CLASS_SELECTOR,
    },
    "project_register_from_github_url": {
        "desc": "Import a PUBLIC github.com repo (clone+bootstrap+index); private "
        "only via CodeNexus credentials. Optional org_id (name or id, from org_list) "
        "targets which of your orgs to create it in; omitted = your own org. AIDOCS "
        "refuses if you lack the create right in the named org.",
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "ref": {"type": "string"},
                "org_id": {"type": "string"},
            },
            "required": ["url"],
        },
        "cls": CLASS_IMPORT,
    },
    "project_sync": {
        "desc": "Fetch+reset+re-index a github-sourced project.",
        "schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
        "cls": CLASS_IMPORT,
    },
}


# Extend PROJECT_TOOL_SPECS from the new registry: every registry entry
# with a gate-visible surface and cls=CLASS_SELECTOR / CLASS_IMPORT becomes a
# project-tool. Migrated entries take precedence over the hand-rolled
# specs above with the same name (so a future commit can fold the
# hand-rolled ones into the registry without touching this code).
def _extend_project_specs_from_registry() -> None:
    from . import tool_interface as _ti

    for name, spec in _ti.REGISTRY.items():
        if spec.surface not in (_ti.BOTH, _ti.GATE_ONLY):
            continue
        if spec.cls not in (CLASS_SELECTOR, CLASS_IMPORT):
            continue
        PROJECT_TOOL_SPECS[name] = {
            "desc": spec.description,
            "schema": _ti.public_schema(spec),
            "cls": spec.cls,
        }


_extend_project_specs_from_registry()

# NOT agent-advertised (Empire 2026-06-20): host/desktop tools + operator-only
# authority/control-plane. Never advertised on the agent tools/list (gate OR stdio):
# tauri_* are desktop-host tools the agent cannot call; escalation approve/deny +
# membership grant/revoke + ACL-list are operator authority an agent must never
# self-serve (an agent approving its own escalation / granting itself membership is
# the exact control-plane breach to prevent). The operator drives these from the
# dashboard, not the agent tool surface. Excluded at the SINGLE SOURCE so both the
# outer-gate advertised() and the local stdio tools/list drop them together.
_NOT_AGENT_ADVERTISED = frozenset(
    {
        "tauri_todo_list",
        "tauri_backlog_list",
        "approve_escalation",
        "deny_escalation",
        # War 0: freeze recovery + surfacing (operator dashboard only).
        "clear_freeze",
        "freeze_list",
        "escalation_list",
        "project_grant_member",
        "project_revoke_member",
        "project_acl_list",
        # SSOT-05 WAR5 P1 (2026-07-15): dashboard-client / ruled-off tools that
        # were still leaking onto the agent tool surface. dashboard_snapshot,
        # dashboard_view, dashboard_memory_capture, and config_view are
        # org-admin dashboard-client tools that power the web dashboard UI, not
        # agent work. memory_kg_get and memory_kg_graph are dashboard KG-page
        # plumbing with no local (agent-side) tool equivalent. vocab_list_kinds,
        # vocab_list_langs, and vocab_get_grouped were walled off from agents by
        # a 2026-05-16 Empire directive (see test_ai_vocab_mcp_tool_removed.py);
        # the read triplet still being agent-advertised was a partial
        # regression of that directive. All nine remain GATE_ONLY in
        # tool_interface.py and stay reachable via the dashboard/org-admin
        # tools/call path -- only the agent discovery surface (gate advertised()
        # and stdio tools/list) drops them.
        "dashboard_snapshot",
        "dashboard_view",
        "dashboard_memory_capture",
        "config_view",
        "memory_kg_get",
        "memory_kg_graph",
        "vocab_list_kinds",
        "vocab_list_langs",
        "vocab_get_grouped",
        # ── FOLDED INTO THE TWINS (operator ruling 2026-08-28) ─────────────
        # "the old tools should not be accessible or visible to agents (save
        # context space). they can be internal calls."
        #
        # ai_project and ai_session are the ONE tool per noun, surface=BOTH.
        # Each name below is answered by a mode on its twin, so advertising it
        # costs a schema on every tools/list and buys nothing:
        #     project_select  -> ai_project(mode='connect')
        #     project_list    -> ai_project(mode='list')
        #     project_current -> ai_project(mode='status')
        #     session_select  -> ai_session(mode='connect')
        #     session_list    -> ai_session(mode='list')
        #
        # ALL FIVE ARE ALREADY GATE_ONLY in tool_interface, so this changes
        # nothing for the local stdio agent — the context they burn is on the
        # WEB surface, where the connector advertised the whole family
        # alongside the twins that replace it.
        #
        # STILL REGISTERED AND CALLABLE, per the ruling's second half — but by
        # IN-PROCESS DISPATCH, not by a tool calling a tool. tool_interface
        # ._delegate:549 takes the C.20 fast path: when an impl has called
        # register_impl(name, fn) the closure is invoked directly, "skipping
        # server build + fastmcp routing + thread hop". That path does not
        # consult this allowlist at all, which is exactly why dropping a name
        # here costs discovery and nothing else.
        #
        # (The SSOT-05 note above says its nine stay "reachable via the
        # dashboard/org-admin tools/call path". Do not copy that phrasing
        # forward — I did, and the operator corrected it: there is no
        # tool-that-calls-tools dispatcher any more, and _delegate's own
        # docstring records call_tool as the LEGACY fallback for closures C.20
        # has not lifted yet.)
        #
        # Deliberately NOT HIDDEN_EVERYWHERE: that skips registration outright
        # (see :120), which would break the very delegation this preserves.
        #
        # test_project_session_family_folds_into_twins pins BOTH sides.
        "project_select",
        "project_list",
        "project_current",
        "session_select",
        "session_list",
        # ── SECOND WAVE, 2026-08-28: the modes now EXIST ───────────────────
        # The block above used to end "NOT FOLDED, each for a measured reason —
        # project_status and project_index_status take a project_id that
        # ai_project(status) has no parameter for; session_current has no
        # ai_session mode; and project_list_sessions has none either. Those need
        # modes ADDED first: capability removal is not consolidation." That was
        # the correct order of operations, and this is the other half of it —
        # the modes were built first, then these names dropped:
        #     project_status        -> ai_project(mode='status', project_id=…)
        #     project_list_sessions -> ai_project(mode='sessions')
        #     session_current       -> ai_session(mode='status')
        #     session_create        -> ai_session(mode='create')
        #     session_delete        -> ai_session(mode='delete')
        "project_status",
        "project_list_sessions",
        "session_current",
        "session_create",
        "session_delete",
        # INDEX STATUS IS NOT A PROJECT MODE (operator ruling 2026-08-28:
        # "index status should be ai_index tool"). I had proposed folding it
        # into ai_project(mode='index_status'); it belongs with the index
        # family, and ai_index_status is already surface=both and advertised.
        "project_index_status",
        #
        # STILL ADVERTISED, AND DELIBERATELY: project_sync and
        # project_register_from_github_url. Both are cls=IMPORT under
        # scope='project_import', while ai_project is scope='tier_m_edit', and
        # ToolSpec carries ONE scope for the whole spec — there is no per-mode
        # scope. Folding them would therefore let any caller holding
        # tier_m_edit sync or register a project WITHOUT project_import: a
        # privilege ESCALATION, not a consolidation. Folding the reads above is
        # safe because it narrows rather than grants, and the original names
        # stay callable by in-process dispatch either way.

        # Memory-war unify (2026-07-16, operator): palace is an internal
        # projection, not an agent store — agent retrieval is unified under
        # ai_recall/ai_memory(search) (index-hit spine -> anchored drawers ->
        # KG -> palace semantic, see unified_recall.py). The palace retrieval/
        # status tools drop off BOTH agent surfaces (GATE_ONLY in
        # tool_interface + this exclusion); the dashboard keeps reaching them
        # via tools/call (hidden, not dead).
        "ai_palace_search",
        "ai_palace_status",
    }
)

# War 0 (2026-07-13): the OPERATOR DASHBOARD's control-plane ops — hidden from
# every agent surface (HIDDEN_EVERYWHERE + _NOT_AGENT_ADVERTISED above) yet
# ROUTABLE over tools/call for an org OWNER/ADMIN principal ONLY. The transport
# dispatch checks `is_org_admin(principal)` BEFORE routing any of these to
# handle_project_tool; a non-admin caller falls through to the ordinary
# execute() path, which refuses them as internal (fail-closed — the op stays
# undiscoverable AND uninvokable for ordinary agents). This is what lets the
# authenticated operator see freezes/escalations and clear/approve/deny from
# the web dashboard while the agent tool surface never widens.
#
# SSOT-05 WAR5 P1 (2026-07-15): dashboard_snapshot, dashboard_view, and
# config_view join this set too. Each already self-enforces `is_org_admin`
# INSIDE its own handle_project_tool branch (see outer_gate_transport.py), so
# gating their ROUTE the same way is a pure belt-and-suspenders match to that
# existing internal check — not a new authority restriction. This is what
# keeps them dispatchable for the real operator dashboard even after
# PROJECT_TOOLS drops them below. (dashboard_memory_capture is ALSO
# org-admin-only internally, but stays a direct PROJECT_TOOLS member instead
# — see _ORDINARY_DASHBOARD_READS_KEPT_ROUTABLE below — because
# test_outer_gate_dashboard_reads.py pins it there explicitly; both routes
# reach the same internal is_org_admin refusal either way.)
ORG_ADMIN_DASHBOARD_TOOLS: frozenset[str] = frozenset(
    {
        "approve_escalation",
        "deny_escalation",
        "clear_freeze",
        "freeze_list",
        "escalation_list",
        "tauri_backlog_list",
        "tauri_todo_list",
        "dashboard_snapshot",
        "dashboard_view",
        "config_view",
    }
)
# SSOT-05 WAR5 P1 (2026-07-15): PROJECT_READ_TOOLS/PROJECT_EDIT_TOOLS feed
# PROJECT_TOOLS, which is the transport's actual tools/call DISPATCH table
# (outer_gate_transport: `_route_project_tool = name in PROJECT_TOOLS`) --
# not just an advertised-surface source. Excluding a name here removes it
# from ordinary dispatch entirely; it only stays callable if it is ALSO in
# ORG_ADMIN_DASHBOARD_TOOLS (admin-gated bypass route).
#
# Of the nine names added to _NOT_AGENT_ADVERTISED above, three
# (dashboard_snapshot, dashboard_view, config_view) are already org-admin-only
# by internal enforcement, so excluding them here and adding them to
# ORG_ADMIN_DASHBOARD_TOOLS above is a no-op for real authority — same admin
# gate, one layer earlier (fail-closed defense in depth, matching the
# existing tauri_*/escalation pattern).
#
# The other six (dashboard_memory_capture, memory_kg_get, memory_kg_graph,
# vocab_list_kinds, vocab_list_langs, vocab_get_grouped) stay direct
# PROJECT_TOOLS members instead of moving behind the org-admin-only bypass:
# dashboard_memory_capture is pinned there explicitly by
# test_outer_gate_dashboard_reads.py::test_memory_tools_are_dispatchable_project_tools
# (it still self-enforces is_org_admin internally either way); the other five
# are ORDINARY catalog-scoped project reads with NO admin restriction at all
# -- any authenticated project member's dashboard session must keep reaching
# them. Routing them through the org-admin-only bypass would silently narrow
# who can use the Memory/Vocab dashboard pages -- an authority change, not a
# mechanical agent-surface hide. So they are carved OUT of the PROJECT_TOOLS
# exclusion (kept in ordinary dispatch) even though they are also in
# _NOT_AGENT_ADVERTISED (still hidden from advertised()/tools/list directly,
# see advertised() below).
_ORDINARY_DASHBOARD_READS_KEPT_ROUTABLE = frozenset(
    {
        "dashboard_memory_capture",
        "memory_kg_get",
        "memory_kg_graph",
        "vocab_list_kinds",
        "vocab_list_langs",
        "vocab_get_grouped",
    }
)
#: THE FOLDED TWIN FAMILY — hidden from discovery, STILL ROUTABLE.
#:
#: CAUGHT BY GATE 2b, 2026-08-28, and it was a real regression rather than a
#: stale pin. PROJECT_TOOLS is DERIVED by subtracting _NOT_AGENT_ADVERTISED, so
#: adding these names there did two things at once: it dropped them from
#: discovery (intended) AND from `_route_project_tool = name in PROJECT_TOOLS`
#: in the mcp tools/call path (NOT intended). A direct tools/call for
#: project_list then stopped reaching handle_project_tool at all and fell
#: through to `no_project_selected`.
#:
#: That contradicted the promise both fold commits make in as many words —
#: "STILL REGISTERED AND CALLABLE ... only the agent discovery surface drops
#: them" — and it is the same distinction the carve-out above already draws for
#: the dashboard reads: hiding a name from a catalog must not narrow WHO CAN
#: ROUTE to it, because that is "an authority change, not a mechanical
#: agent-surface hide".
#:
#: The twins themselves are unaffected either way: _ogt_pt_ai_project and
#: _ogt_pt_ai_session call these handlers DIRECTLY as delegates, and that path
#: never consults this set. What broke was only the old names' own dispatch —
#: which is exactly the capability the fold promised to preserve.
_FOLDED_TWIN_FAMILY_KEPT_ROUTABLE = frozenset(
    {
        "project_select",
        "project_list",
        "project_current",
        "project_status",
        "project_index_status",
        # project_list_sessions is deliberately ABSENT: it is cls=READ, not
        # SELECTOR/IMPORT, so it was never a PROJECT_TOOLS member and this
        # subtraction never touched it. It reaches its handler by the ordinary
        # surface=both registry path. Naming it here would be inert and would
        # imply a routing it never had.
        "session_select",
        "session_list",
        "session_current",
        "session_create",
        "session_delete",
    }
)
_NOT_PROJECT_TOOLS_ROUTABLE = _NOT_AGENT_ADVERTISED - (
    _ORDINARY_DASHBOARD_READS_KEPT_ROUTABLE | _FOLDED_TWIN_FAMILY_KEPT_ROUTABLE
)
PROJECT_READ_TOOLS = frozenset(
    n
    for n, s in PROJECT_TOOL_SPECS.items()
    if s["cls"] == CLASS_SELECTOR and n not in _NOT_PROJECT_TOOLS_ROUTABLE
)
PROJECT_EDIT_TOOLS = frozenset(
    n
    for n, s in PROJECT_TOOL_SPECS.items()
    if s["cls"] == CLASS_IMPORT and n not in _NOT_PROJECT_TOOLS_ROUTABLE
)
PROJECT_TOOLS = PROJECT_READ_TOOLS | PROJECT_EDIT_TOOLS

# YOLO-with-law: ai_str_replace is a SINGLE-CALL edit. The agent submits intent
# once (path/old_string/new_string[/replace_all]); AIDOCS carries the judge,
# binding, protected-path law, audit, and reindex INTERNALLY and applies a safe
# edit in one call (or hard-refuses). There is NO public confirmation field — no
# confirm_token, no edit_confirmation_id — so the model never receives/resends
# token-like material (which MCP hosts misclassified as an AccessToken). Legacy
# confirm_token is still ACCEPTED on commit for old clients but never advertised.
_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
    "required": ["path", "old_string", "new_string"],
    "additionalProperties": False,
}

_RUN_SCHEMAS = {
    "ai_run": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
            "foreground": {"type": "boolean"},
        },
        "required": ["command"],
    },
    "ai_run_output": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "tail_bytes": {"type": "integer"},
            "raw_output": {"type": "boolean"},
        },
        "required": ["run_id"],
    },
    "ai_run_kill": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
}
_RUN_DESC = {
    "ai_run": "Run a shell command in the selected project (detached; enforces "
    "bash_policy + heuristic_judge + freeze + destructive floor).",
    "ai_run_output": "Read the tail of a completed run's output by run_id.",
    "ai_run_kill": "Kill a running command by run_id.",
}


# Process-lifetime memo for _manifest_index (PERFORMANCE_DOCTRINE Art VII).
# build_manifest is a pure function of the package SOURCE, and a process's
# source identity is fixed at import — so re-deriving the manifest per call
# bought no freshness while every catalog()/advertised()/_default_read_names()
# call paid a ~0.9s AST scan (the resolution-pin test matrix makes ~22 such
# calls ≈ 20s of pure waste; operator 2026-07-16). The FIRST build still goes
# through the fingerprint-bound build_manifest_cached, so the law-reviewed
# cross-process invalidation doctrine (content fingerprint keys the disk
# cache; source change ⇒ recompute) holds unchanged. Tests that need a fresh
# derivation reset `_MANIFEST_INDEX_MEMO = None`.
_MANIFEST_INDEX_MEMO: dict | None = None


def _manifest_index():
    global _MANIFEST_INDEX_MEMO
    if _MANIFEST_INDEX_MEMO is None:
        from . import outer_gate_manifest as man

        _MANIFEST_INDEX_MEMO = {e["name"]: e for e in man.build_manifest_cached()}
    return _MANIFEST_INDEX_MEMO


def _default_read_names() -> frozenset[str]:
    """READ_EXEC tools that are genuinely gate-invokable per the canonical
    manifest (remote-eligible + invokable tier). In production the gate uses this
    same manifest, so advertised reads == executor-runnable == gate-invokable.
    """
    from .outer_gate import INVOKABLE_TIERS

    idx = _manifest_index()
    inv = {
        n for n, e in idx.items() if e.get("remote_eligible") and e.get("tier") in INVOKABLE_TIERS
    }
    return frozenset(inv & set(READ_EXEC_ALLOWLIST))


def classify(
    name: str,
    manifest_entry: dict | None = None,
    read_names: frozenset[str] | None = None,
) -> str:
    # HIDDEN_EVERYWHERE wins over EVERYTHING: a tool the Empire ruled off BOTH agent
    # surfaces (host/desktop tauri_*, escalation approve/deny, membership grant/
    # revoke/ACL) is INTERNAL regardless of registry surface or any — possibly
    # rebuilt — allowlist membership. This is the SERVING-surface guard: even
    # though the host_compat serve-time rebuild is retired (#292) and the static
    # catalog now holds the _NOT_AGENT_ADVERTISED exclusion by construction, this
    # classify() floor stays as belt-and-suspenders — no allowlist rebuild could
    # ever resurface a hidden tool into the advertised/routable set.
    if name in HIDDEN_EVERYWHERE:
        return CLASS_INTERNAL
    # Shell-trio floor (Empire ruling 2026-06-20, tool_surface_waivers permanent
    # entries): the detached-shell dispatch is structurally sealed — the RUN
    # class comes from the bare RUN_ALLOWLIST frozenset and a registry row
    # must never re-route ai_run/_output/_kill through another class branch
    # (e.g. a rogue selector row would swap the project_run grant for
    # catalog). Same belt-and-suspenders posture as the HIDDEN_EVERYWHERE
    # floor above.
    if name in RUN_ALLOWLIST:
        return CLASS_RUN
    # Registry (new single source of truth) wins over legacy allowlists.
    # tool_interface.REGISTRY entries declare both their surface and
    # their class; the legacy CLASS_*_ALLOWLISTS below remain as the
    # fallthrough for tools that haven't been migrated yet.
    from . import tool_interface as _ti

    spec = _ti.get(name)
    if spec is not None:
        # local_only: gate refuses by contract. hidden (#386/#288): invisible
        # on EVERY agent surface — the gate verdict must stay CLASS_INTERNAL
        # (never the spec's cls) so hiding a tool can never WIDEN gate access.
        if spec.surface in ("local_only", "hidden"):
            return CLASS_INTERNAL
        return spec.cls or CLASS_DISCOVERABLE
    reads = read_names if read_names is not None else READ_EXEC_ALLOWLIST
    if name in reads:
        return CLASS_READ
    if name in RUN_ALLOWLIST:
        return CLASS_RUN
    if name in EDIT_ALLOWLIST:
        return CLASS_EDIT
    if name in PROJECT_READ_TOOLS:
        return CLASS_SELECTOR
    if name in PROJECT_EDIT_TOOLS:
        return CLASS_IMPORT
    if manifest_entry is not None and manifest_entry.get("remote_eligible"):
        return CLASS_DISCOVERABLE
    return CLASS_INTERNAL


# Empire 2026-06-28: tools that stay gate-advertised (surface=BOTH) but are ABSENT from
# the catalog (in_catalog=False) for every NON-super_admin principal — hidden, not merely
# scope-blocked — so only the operator's own super_admin account ever sees them in
# tool_catalog. Invoke authority is independently enforced in outer_gate.execute()
# (e.g. ai_deploy → super_admin + AIDOCS_PRIVATE + ref allowlist); this set governs
# only catalog VISIBILITY, never execution.
SUPER_ADMIN_ONLY_CATALOG: frozenset[str] = frozenset({"ai_deploy"})

# Empire 2026-07-06: dashboard-CLIENT tools that are ABSENT from the catalog +
# tools/list for every NON-org-admin principal — hidden, not merely
# scope-blocked. dashboard_snapshot returns the full operator dashboard
# (exec stream, tokens, lanes, config, gate verdicts); it powers the cloud
# DASHBOARD UI, not agent work. A connected co-conductor agent should not
# see it. Invoke authority is independently enforced in the transport handler
# (is_org_admin), mirroring config_view/dashboard_view. Kept catalog-visible
# ONLY for the operator's org-admin (the dashboard's own client).
ORG_ADMIN_ONLY_CATALOG: frozenset[str] = frozenset({"dashboard_snapshot"})


# Standalone-tool law: dispatcher/carrier tools are architectural bugs. Even if
# one is accidentally reintroduced into a registry or stale manifest, every
# WebMCP projection must hide it and treat it as internal-only.
FORBIDDEN_DISPATCHER_CARRIERS: frozenset[str] = frozenset({"aidocs_call"})


# ── Staged binding law (Empire /goal 2026-07-06 — WebMCP seal) ──────────────────
# ONE policy source for BOTH visibility (resolve() → tools/list, tool_catalog,
# tool_capabilities) and invocation (outer_gate.execute + direct standalone
# transport handlers). The tenant hierarchy is
# org ← project ← session; a tenant-bound caller sees and calls tools by
# BINDING STAGE:
#   pre-project   → org/project discovery + project lifecycle only
#   project-only  → + project status + session lifecycle
#   bound         → the rest of the allowed surface
# Local/legacy principals (no tenant_id) are unstaged. FAIL CLOSED: any tool
# not named in a lifecycle set requires the FULL binding — a new tool is
# session-scoped by default, never pre-session-callable by omission.

STAGE_LIFECYCLE_PRE_PROJECT: frozenset[str] = frozenset({
    "org_list", "org_select",
    "project_list", "project_current", "project_select",
    "project_status", "project_index_status",
    "project_register_from_github_url", "project_sync",
    "project_grant_member", "project_revoke_member", "project_acl_list",
    "tool_catalog", "tool_capabilities",
    # ai_version answers "WHICH BUILD IS THIS?" — three axes read from an
    # artefact stamp and the authority. It touches no project, no session and
    # no tenant data, so the binding hierarchy has nothing to say about it.
    #
    # MEASURED 2026-08-27, web surface: the connector exposed 19 tools — this
    # set plus session lifecycle and the control-plane panels — and ai_version
    # was NOT among them. So the operator could not ask which build he was
    # talking to while diagnosing why the surface was misbehaving. "Which code
    # is running" is the first question of any diagnosis, and it sat behind the
    # binding whose failure he was diagnosing. Law 311bf3e6 wearing a different
    # costume: the instrument that explains the block was behind the block.
    #
    # Same shape as the ai_session admission below, and the same fail-closed
    # default caught both: "session-scoped by default, never pre-session-
    # callable by omission" is right as a general rule and wrong for a tool
    # whose answer PRECEDES binding entirely.
    #
    # A STAGE ADMISSION, NOT AN AUTHORIZATION: ai_version still faces the
    # project ACL, RBAC and every other rung, is readOnlyHint, and returns no
    # project or session data.
    "ai_version",
    # #935, and the SAME argument one step further. ai_version answers "which
    # build is this"; ai_whoami answers "who does this surface think I am, and
    # what did my client actually send" -- the second question of any binding
    # diagnosis, and the operator asked for it by name: "i need whoami on web,
    # to see if a tool call actually sends the binding ids".
    #
    # PRE-PROJECT IS THE WHOLE POINT, not a convenience. This tool exists to
    # explain a binding that FAILED, so gating it behind a successful binding
    # is law 311bf3e6 exactly -- the instrument that explains the block sitting
    # behind the block. It reads no project and no session data; every value it
    # returns is about THIS REQUEST's identity channels.
    "ai_whoami",
    # #537, operator ruling 2026-08-27: "it's not local only, it's the
    # session_select twin but for projects". A PROJECT-BINDING tool must be
    # callable BEFORE a project is bound, for the same reason project_select
    # sits here — law 311bf3e6, a named remedy must be reachable. Gating the
    # bind door behind the bind is the trap ai_session was found in.
    "ai_project",
})

STAGE_LIFECYCLE_SESSION: frozenset[str] = frozenset({
    "session_list", "session_create", "session_select",
    "session_current", "session_delete", "project_list_sessions",
    # ai_session IS session lifecycle -- the dual-surface twin of the six names
    # above (connect / list / create / claim / release / update / resume /
    # skills). It was missing, and the fail-closed default documented above
    # ("session-scoped by default, never pre-session-callable by omission") is
    # exactly what caught it: right as a general rule, wrong for the ONE tool
    # whose job is to bind the session that rule demands.
    #
    # MEASURED 2026-08-26, web agent, project bound and NO session selected:
    #     ai_session(connect, ubermega) -> no_session_selected
    #     ai_find(...)                  -> no_session_selected
    # The second is CORRECT: a work tool needs a bound session. The first is a
    # trap -- `no_session_selected` names connect as its remedy, and connect was
    # refused by the very condition it exists to clear (law 311bf3e6: a named
    # remedy must be reachable).
    #
    # It stayed hidden because a session was usually ALREADY selected, so the
    # stage gate never fired on it. A fresh conversation is the state that
    # exposes it.
    #
    # A STAGE ADMISSION, NOT AN AUTHORIZATION. stage_gate answers only "how far
    # is this caller bound": ai_session still faces the project ACL, RBAC,
    # managed mode, and (since #916) the two-phase confirm on connect/bind. It
    # stays dark PRE-PROJECT because the project check above returns first -- a
    # session belongs to a project. The set already carries session_create and
    # session_delete, which MUTATE; admitting the lifecycle dispatcher beside
    # them grants no authority they do not already have.
    "ai_session",
})

# Org-admin CONTROL-PLANE panels — the web dashboard's client path, each
# self-gated on is_org_admin (+ two-phase confirm for config_set) inside the
# handler. These are org/project-scoped authority surfaces, NOT agent work
# tools: they stage at PROJECT-ONLY (blocked pre-project; a session is never
# required to administer the org's own panels). Empire-doctrine §XIX: the
# dashboard is another client to the same policy core — it must not go dark
# behind an agent-session requirement.
STAGE_ORG_ADMIN_CONTROL_PLANE: frozenset[str] = frozenset({
    "config_view", "config_set", "dashboard_view", "dashboard_snapshot",
    # War 0: freeze recovery + escalation decisions must be reachable WITHOUT
    # a bound work session — the work session is exactly what is frozen; an
    # operator recovering a locked-down session cannot be required to bind one.
    "clear_freeze", "freeze_list", "escalation_list",
    "approve_escalation", "deny_escalation",
})


def stage_gate(
    name: str,
    *,
    principal: dict | None,
    project_selected: bool,
    session_selected: bool,
) -> tuple[bool, str]:
    """(allowed, blocked_by) under the staged binding law. Tenant-only; a
    local/legacy principal (no tenant_id) is never staged here."""
    if not str((principal or {}).get("tenant_id") or ""):
        return True, ""
    if name in STAGE_LIFECYCLE_PRE_PROJECT:
        return True, ""
    if not project_selected:
        return False, "no_project_selected"
    if name in STAGE_LIFECYCLE_SESSION or name in STAGE_ORG_ADMIN_CONTROL_PLANE:
        return True, ""
    if not session_selected:
        return False, "no_session_selected"
    return True, ""


def _catalog_row(
    *,
    name: str,
    cls: str,
    is_super_admin_principal: bool,
    is_org_admin_principal: bool,
    declared_output: dict | None,
    stage_ok: bool,
    stage_block: str,
    tier,
    visible,
    executable,
    blocked_by,
    grant,
    schema,
    desc,
    in_catalog=True,
    annotations=None,
) -> dict:
    """Build one truthful catalog row (was resolve()'s ``out`` closure — #413).

    `advertise`: the STABLE tools/list superset — every call-path class
    (read/edit/run/selector/import) is ALWAYS advertised when the server
    supports it, INDEPENDENT of the caller's current scope, so an MCP host
    (ChatGPT) registers a stable resource list and a call without scope
    fails insufficient_scope (NOT "Resource not found"). `visible` stays the
    scope-permitted view (what the caller may execute now); tools/call +
    executable_now remain the scope/project/readiness authority. Discoverable
    (no remote call path) and internal-only are never advertised.
    """
    advertise = cls in (CLASS_READ, CLASS_EDIT, CLASS_RUN, CLASS_SELECTOR, CLASS_IMPORT)
    # Empire 2026-06-28: super-admin-only tools (ai_deploy) are absent from tools/list too
    # (not just tool_catalog) for non-super_admin — a controlled exception to the stable-
    # advertise doctrine: these tools must never appear to a non-super_admin principal on
    # ANY surface. The operator's own super_admin still gets the stable advertised entry.
    if name in SUPER_ADMIN_ONLY_CATALOG and not is_super_admin_principal:
        advertise = False
    # org-admin-only dashboard-client tools are absent from tools/list too
    # for a non-org-admin principal (hidden on every surface, not just
    # scope-blocked). The operator's org-admin still gets the entry.
    if name in ORG_ADMIN_ONLY_CATALOG and not is_org_admin_principal:
        advertise = False
    # Doctrine (2026-05-29): callers MAY override the default
    # classification-based annotations dict — e.g. registry-backed
    # EDIT tools carry their own MCP annotations (title, hints) in
    # the @tool decorator. Fall back to the class-default when no
    # override is supplied.
    ann = annotations if annotations is not None else _annotations(cls, name)
    # super-admin-only tools are HIDDEN from the catalog for non-super_admin
    # principals (absent, not scope-blocked); the operator's super_admin still sees them.
    _in_catalog = in_catalog and not (
        (name in SUPER_ADMIN_ONLY_CATALOG and not is_super_admin_principal)
        or (name in ORG_ADMIN_ONLY_CATALOG and not is_org_admin_principal)
    )
    # Staged binding law: out-of-stage tools are not visible, not in the
    # catalog and NOT EXECUTABLE — the visibility law and the invocation law
    # read the same stage_gate().
    #
    # BUT `advertise` IS DELIBERATELY LEFT ALONE (2026-08-29, #935). It used to
    # be forced False here too, and that broke a promise made in two other
    # places:
    #
    #   * advertised()'s own contract — "the STABLE call-path superset ...
    #     IDENTICAL REGARDLESS OF THE CALLER'S SCOPE OR CURRENT PROJECT
    #     READINESS. An MCP host (ChatGPT) thus registers a stable resource
    #     list; a call without the required scope then fails insufficient_scope
    #     at tools/call (NOT 'Resource not found')."
    #   * the transport's own initialize response, which declares
    #     `capabilities.tools.listChanged = False` — i.e. it tells every client
    #     "this list never changes, do not re-fetch".
    #
    # MEASURED LIVE 2026-08-29: a ChatGPT session listed tools BEFORE a session
    # was bound, cached the resulting ELEVEN tools, and — honouring
    # listChanged=False — never re-listed. Binding a session moved the
    # server-side set from 11 to 75, and the client could not learn it. So
    # ai_find reported executable_now=true (the CALL path stages correctly) while
    # never being offered as a callable function. The exact 11-tool surface the
    # operator saw is reproduced by advertised(project=<no session_id>), which is
    # how this was traced.
    #
    # A STAGE IS AN ORDERING, NOT AN AUTHORIZATION. Nothing here grants
    # execution: visible / in_catalog / executable_now all stay False and
    # blocked_by still names the stage, so tool_capabilities keeps telling the
    # truth and tools/call still refuses with `no_session_selected` — a refusal
    # that NAMES ai_session as its remedy, which the caller can actually reach.
    # An absent tool cannot say that; it just looks broken.
    #
    # The alternative fix — emit notifications/tools/list_changed on every
    # selection change — needs a server->client channel this transport does not
    # have (listChanged is declared False for that reason). If that channel is
    # ever built, hiding could return; until then, honesty about a stable list
    # beats a list that silently goes stale.
    if not stage_ok:
        visible = False
        _in_catalog = False
        executable = False
        blocked_by = stage_block
    return {
        "name": name,
        "class": cls,
        "tier": tier,
        "visible": visible,
        "advertise": advertise,
        "in_catalog": _in_catalog,
        "executable_now": executable,
        "blocked_by": blocked_by,
        "grant_required": grant,
        "inputSchema": schema,
        "outputSchema": declared_output or _OUTPUT_OBJ,
        "annotations": ann,
        "description": desc,
    }


def _resolve_read_row(name, out, *, principal, project, manifest_entry) -> dict:
    """CLASS_READ branch of resolve() (#413 extraction — behavior unchanged).

    SINGLE SOURCE OF TRUTH (same doctrine as the EDIT branch): a READ
    tool DECLARED in tool_interface carries its real typed schema (built
    from the @tool fn signature by _ti.schema_for), description, and MCP
    annotations. RELAY them — the catalog only surfaces, never re-authors.
    Only a read NOT yet migrated into the registry falls back to the
    manifest's AST-inspected param list, normalized to valid JSON Schema.
    """
    from .outer_gate_grants import resolve_execution_grant

    g = resolve_execution_grant(principal=principal, project=project, kind="read")
    try:
        from . import tool_interface as _ti

        reg_spec = _ti._TOOLS.get(name)
    except Exception:
        reg_spec = None
    if (
        reg_spec is not None
        and reg_spec.cls == _ti.READ
        and reg_spec.surface in (_ti.BOTH, _ti.GATE_ONLY)
    ):
        reg_ann = dict(reg_spec.annotations) if reg_spec.annotations else None
        return out(
            tier="R",
            visible=True,
            executable=g.allow,
            blocked_by=("" if g.allow else g.reason),
            grant="tier_r_invoke",
            schema=_ti.schema_for(reg_spec),
            desc=reg_spec.description or _READ_DESC.get(name, f"Tier-R read ({name})."),
            annotations=reg_ann,
        )
    sch = _read_inputschema((manifest_entry or {}).get("schema"))
    return out(
        tier="R",
        visible=True,
        executable=g.allow,
        blocked_by=("" if g.allow else g.reason),
        grant="tier_r_invoke",
        schema=sch,
        desc=_READ_DESC.get(name, f"Tier-R read ({name})."),
    )


def _resolve_edit_row(name, out, *, principal, project, scope) -> dict:
    """CLASS_EDIT branch of resolve() (#413 extraction — behavior unchanged).

    ── Registry-backed EDIT tools (ai_lane / ai_plan / ai_worker
       + every future BOTH/GATE_ONLY EDIT) carry their OWN
       schema, description, and MCP annotations on the @tool
       decorator. Surfacing the legacy _EDIT_SCHEMA (path /
       old_string / new_string) for these would be doctrinally
       wrong: hosts (ChatGPT, Claude) would see an `action=`-shaped
       tool advertised as a string-replacer, get past
       insufficient_scope, then hit a schema mismatch at
       tools/call. Legacy ai_str_replace (the only NON-registry
       EDIT) stays on _EDIT_SCHEMA + the generic description.
    Doctrine (2026-05-29): the per-tool schema/description/
    annotations live with the @tool declaration; the catalog
    only RELAYS them. One source of truth.
    """
    from .outer_gate_grants import resolve_execution_grant

    g = resolve_execution_grant(principal=principal, project=project, kind="edit")
    try:
        from . import tool_interface as _ti

        reg_spec = _ti._TOOLS.get(name)
    except Exception:
        reg_spec = None
    if (
        reg_spec is not None
        and reg_spec.cls == _ti.EDIT
        and reg_spec.surface in (_ti.BOTH, _ti.GATE_ONLY)
    ):
        reg_schema = _ti.schema_for(reg_spec)
        reg_desc = reg_spec.description or f"Tier-M edit ({name}); two-phase confirm."
        # Doctrine 2026-05-29 (Empire-directed re-seal — clean-VPS
        # Gate 2b cluster): pass the FULL spec annotations dict
        # through, including the per-tool human-readable `title`
        # authored on the @tool decorator. MCP hosts (ChatGPT,
        # Claude) read title from tools/list as the label —
        # test_consolidator_annotations_title_comes_from_decorator
        # pins this contract. The _annotations() class-default
        # helper is enriched in parallel to ALSO return title for
        # registry-backed tools, so
        # test_annotations_match_annotations_helper sees catalog
        # == helper at the dict level. One source of truth (the
        # @tool decorator), surfaced through both reader paths.
        reg_ann = dict(reg_spec.annotations) if reg_spec.annotations else None
        return out(
            tier="M",
            visible=("tier_m_edit" in scope),
            executable=g.allow,
            blocked_by=("" if g.allow else g.reason),
            grant="tier_m_edit",
            schema=reg_schema,
            desc=reg_desc,
            annotations=reg_ann,
        )
    return out(
        tier="M",
        visible=("tier_m_edit" in scope),
        executable=g.allow,
        blocked_by=("" if g.allow else g.reason),
        grant="tier_m_edit",
        schema=_EDIT_SCHEMA,
        desc=f"Tier-M surgical edit ({name}); two-phase confirm.",
    )


def _resolve_selector_row(name, out, *, scope, spec) -> dict:
    """CLASS_SELECTOR branch of resolve() (#413 extraction — behavior unchanged).

    SSOT-04 phase A (2026-07-15): SAME relay doctrine as the READ/EDIT
    branches — a selector DECLARED in tool_interface carries its
    real typed schema (schema_for over the @tool fn signature),
    description, and MCP annotations; the catalog only surfaces, never
    re-authors. The hand-rolled PROJECT_TOOL_SPECS entry stays only as
    the fallback for selectors not yet migrated into the registry.
    """
    vis = "catalog" in scope
    try:
        from . import tool_interface as _ti

        reg_spec = _ti._TOOLS.get(name)
    except Exception:
        reg_spec = None
    if (
        reg_spec is not None
        and reg_spec.cls == CLASS_SELECTOR
        and reg_spec.surface in ("both", "gate_only")
    ):
        return out(
            tier="R",
            visible=vis,
            executable=vis,
            blocked_by=("" if vis else "insufficient_scope"),
            grant="catalog",
            schema=_ti.schema_for(reg_spec),
            desc=reg_spec.description or spec.get("desc", name),
            annotations=(
                dict(reg_spec.annotations) if reg_spec.annotations else None
            ),
        )
    return out(
        tier="R",
        visible=vis,
        executable=vis,
        blocked_by=("" if vis else "insufficient_scope"),
        grant="catalog",
        schema=spec.get("schema", _S_OBJ),
        desc=spec.get("desc", name),
    )


def _resolve_run_row(name, out, *, principal, project, scope) -> dict:
    """CLASS_RUN branch: sealed execution rail, registry-owned metadata.

    RUN_ALLOWLIST remains the non-overridable class/dispatch floor and
    project_run remains the grant. Registry metadata replaces only the
    legacy schema/description tables.
    """
    from .outer_gate_grants import resolve_execution_grant as _g

    schema = _RUN_SCHEMAS.get(name, _S_OBJ)
    desc = _RUN_DESC.get(name, name)
    annotations = None
    try:
        from . import tool_interface as _ti
        reg_spec = _ti.get(name)
    except Exception:
        reg_spec = None
    if reg_spec is not None and reg_spec.cls == CLASS_RUN and reg_spec.surface in ("both", "gate_only"):
        schema = _ti.schema_for(reg_spec)
        desc = reg_spec.description or desc
        annotations = dict(reg_spec.annotations) if reg_spec.annotations else None

    vis = "project_run" in scope
    gd = _g(principal=principal, project=project, kind="run")
    return out(
        tier="M",
        visible=vis,
        executable=gd.allow,
        blocked_by=("" if gd.allow else gd.reason),
        grant="project_run",
        schema=schema,
        desc=desc,
        annotations=annotations,
    )


def _resolve_import_row(name, out, *, scope, spec) -> dict:
    """CLASS_IMPORT branch of resolve() (#413 extraction — behavior unchanged).

    DISTINCT grant: project import/sync is gated by project_import, NOT
    tier_m_edit. blocked_by names the precise grant required.
    """
    vis = "project_import" in scope
    return out(
        tier="M",
        visible=vis,
        executable=vis,
        blocked_by=("" if vis else "insufficient_scope"),
        grant="project_import",
        schema=spec.get("schema", _S_OBJ),
        desc=spec.get("desc", name),
    )


def _resolve_discoverable_row(name, out, *, manifest_entry) -> dict:
    """CLASS_DISCOVERABLE branch of resolve() (#413 extraction — unchanged).

    Eligible but not invokable here: Tier-A (phase not built) or an eligible
    read not wired into the executor. NOT advertised invokable; shown in the
    full catalog with an accurate reason.
    """
    e = manifest_entry or {}
    tier = e.get("tier", "?")
    reason = "tier_not_invokable" if tier == "A" else "tool_not_exec_allowlisted"
    return out(
        tier=tier,
        visible=False,
        executable=False,
        blocked_by=reason,
        grant="",
        schema=e.get("schema") or _S_OBJ,
        desc=f"{tier} {name} — discoverable, not remotely invokable.",
    )


def resolve(
    name: str,
    *,
    principal: dict,
    project: dict | None,
    manifest_entry: dict | None = None,
    read_names: frozenset[str] | None = None,
) -> dict:
    """Truthful catalog metadata for ONE tool given the caller + selected project.
    Returns class, tier, visible (advertise in tools/list?), in_catalog (show in
    tool_catalog?), executable_now, blocked_by, grant_required, schema, description.

    Decomposed 2026-07-19 (#413 tranche D): the prelude computes the
    caller/stage facts once, ``out`` is a partial over ``_catalog_row``
    (the former closure), and each tool class has its own
    ``_resolve_*_row`` branch handler. Dispatch order is pinned here:
    READ → EDIT → SELECTOR → RUN → IMPORT → DISCOVERABLE → internal.
    """
    from functools import partial

    scope = set((principal or {}).get("scope") or [])
    # super-admin-only catalog VISIBILITY (not execution): a SUPER_ADMIN_ONLY_CATALOG tool
    # is absent from the catalog unless THIS principal is the operator's super_admin.
    _is_super_admin_principal = False
    if name in SUPER_ADMIN_ONLY_CATALOG:
        try:
            from .ai_deploy_authority import is_super_admin as _is_super_admin

            _is_super_admin_principal = _is_super_admin(principal)
        except Exception:
            _is_super_admin_principal = False
    # org-admin-only catalog VISIBILITY (not execution): dashboard-client tools
    # are absent from the catalog unless THIS principal is an org admin
    # (is_org_admin includes super_admin, so the operator still sees them).
    _is_org_admin_principal = False
    if name in ORG_ADMIN_ONLY_CATALOG:
        try:
            from .outer_gate_project_acl import is_org_admin as _is_org_admin

            _is_org_admin_principal = _is_org_admin(principal)
        except Exception:
            _is_org_admin_principal = False
    cls = (
        CLASS_INTERNAL
        if name in FORBIDDEN_DISPATCHER_CARRIERS
        else classify(name, manifest_entry, read_names)
    )
    spec = PROJECT_TOOL_SPECS.get(name, {})
    # SSOT-07 (2026-07-15): a gate-visible registry row may DECLARE its result
    # shape (ToolSpec.output); when declared it replaces the universal
    # permissive _OUTPUT_OBJ for this tool. Undeclared rows keep the
    # permissive object — a wrong outputSchema is worse than a permissive one
    # (hosts validate results against it; phantom attributes crash UIs).
    _declared_output: dict | None = None
    try:
        from . import tool_interface as _ti_out

        _reg_out = _ti_out._TOOLS.get(name)
        if _reg_out is not None and _reg_out.surface in ("both", "gate_only"):
            _declared_output = getattr(_reg_out, "output", None)
    except Exception:
        _declared_output = None
    # Staged binding law — same policy source the invocation paths call. The
    # `project` dict is the token's server-resolved selection (GateProjectStore
    # .current()) and carries session_id; None ⇒ pre-project stage.
    _stage_ok, _stage_block = stage_gate(
        name,
        principal=principal,
        project_selected=bool(project),
        session_selected=bool(str((project or {}).get("session_id") or "").strip()),
    )
    out = partial(
        _catalog_row,
        name=name,
        cls=cls,
        is_super_admin_principal=_is_super_admin_principal,
        is_org_admin_principal=_is_org_admin_principal,
        declared_output=_declared_output,
        stage_ok=_stage_ok,
        stage_block=_stage_block,
    )

    if cls == CLASS_READ:
        return _resolve_read_row(
            name, out, principal=principal, project=project, manifest_entry=manifest_entry
        )
    if cls == CLASS_EDIT:
        return _resolve_edit_row(name, out, principal=principal, project=project, scope=scope)
    if cls == CLASS_SELECTOR:
        return _resolve_selector_row(name, out, scope=scope, spec=spec)
    if cls == CLASS_RUN:
        return _resolve_run_row(name, out, principal=principal, project=project, scope=scope)
    if cls == CLASS_IMPORT:
        return _resolve_import_row(name, out, scope=scope, spec=spec)
    if cls == CLASS_DISCOVERABLE:
        return _resolve_discoverable_row(name, out, manifest_entry=manifest_entry)
    # internal-only: never advertised, never in the remote catalog.
    return out(
        tier=(manifest_entry or {}).get("tier", "?"),
        visible=False,
        executable=False,
        blocked_by="internal_only",
        grant="",
        schema=_S_OBJ,
        desc=f"{name} — internal-only (not remote).",
        in_catalog=False,
    )


def catalog(
    *,
    principal: dict,
    project: dict | None,
    gate_invokable: frozenset[str] | None = None,
) -> list[dict]:
    """The full canonical remote catalog (honest flags). Union of: every
    remote-eligible manifest tool + the executor read set + edit + selector +
    import tools + every tool_interface registry entry whose surface puts
    it on the gate (BOTH or GATE_ONLY). Internal-only tools are
    excluded (in_catalog=False).

    Doctrine (2026-05-29): `tool_interface.gate_advertised_names()` is
    folded in explicitly. Without this union, a GATE_ONLY tool of a
    class that doesn't fall under EDIT/READ/RUN static allowlists
    would be invisible to the catalog even though surface=GATE_ONLY
    says it should be gate-callable. The static allowlists and the
    registry are now both sources, unioned.

    `gate_invokable` (the live gate's invokable_now set) ∩ READ_EXEC_ALLOWLIST is
    the advertised read set — so advertised==callable-by-THIS-gate. Defaults to the
    canonical manifest's invokable reads (production == this).
    """
    idx = _manifest_index()
    if gate_invokable is not None:
        read_names = frozenset(set(gate_invokable) & set(READ_EXEC_ALLOWLIST))
    else:
        read_names = _default_read_names()
    # Pull registry gate-advertised names (BOTH ∪ GATE_ONLY) so
    # GATE_ONLY consolidators are visible even if a future class
    # isn't covered by the static allowlists. Best-effort import
    # so a registry-load failure doesn't brick the catalog endpoint.
    try:
        from . import tool_interface as _ti

        registry_gate = set(_ti.gate_advertised_names())
    except Exception:
        registry_gate = set()
    names = (
        set(idx)
        | set(read_names)
        | set(EDIT_ALLOWLIST)
        | set(RUN_ALLOWLIST)
        | set(PROJECT_TOOLS)
        | registry_gate
    )
    names.difference_update(FORBIDDEN_DISPATCHER_CARRIERS)
    # Same exclusion advertised() applies twenty lines below, for the same
    # reason and against the same leak (2026-08-15). registry_gate is unioned
    # in raw from tool_interface.gate_advertised_names() (BOTH u GATE_ONLY),
    # which does not know about this module's _NOT_AGENT_ADVERTISED set, so a
    # GATE_ONLY registry tool re-enters `names` here. advertised() was patched
    # for exactly this; catalog(), building the identical union, was not.
    #
    # MEASURED before the fix: ten of the twenty-one excluded names reached the
    # agent-facing catalog -- ai_palace_search/status (ruled off BOTH agent
    # surfaces 2026-07-16, retrieval unified under ai_recall/ai_memory),
    # vocab_list_kinds/langs/get_grouped (walled off by the 2026-05-16 Empire
    # directive, whose own note already records one partial regression),
    # dashboard_view, dashboard_memory_capture, config_view, memory_kg_get,
    # memory_kg_graph. None was callable -- executable_now False, unadvertised,
    # no tools/call branch -- but a remote agent asked "what tools exist"
    # reported the dashboard's internal plumbing back to its operator, and
    # _ogt_pt_tool_catalog (the project tool served from here) documents itself
    # as "Internal-only (dashboard) tools are excluded".
    #
    # CLI commands are deliberately NOT affected: they are kind=cli_command,
    # "discoverable, not remotely invokable", and they are how an agent NAMES a
    # remedy it cannot run (tenant-reconcile is the live example). Pinned by
    # test_catalog_honours_agent_exclusion.
    names.difference_update(_NOT_AGENT_ADVERTISED)
    rows = []
    for n in sorted(names):
        r = resolve(
            n,
            principal=principal,
            project=project,
            manifest_entry=idx.get(n),
            read_names=read_names,
        )
        if r["in_catalog"]:
            rows.append(r)
    return rows


def advertised(
    *,
    principal: dict,
    project: dict | None,
    gate_invokable: frozenset[str] | None = None,
) -> list[dict]:
    """The tools/list set: the STABLE call-path superset — every read/edit/run/
    selector/import tool the server supports, classified from the STATIC
    allowlists (NOT the live-invokable subset) so the advertised resource list is
    identical regardless of the caller's scope or current project readiness. An
    MCP host (ChatGPT) thus registers a stable resource list; a call without the
    required scope then fails insufficient_scope at tools/call (NOT "Resource not
    found"). Every advertised tool HAS a real tools/call branch; discoverable (no
    remote call path) + internal-only are never advertised.
    tool_capabilities/executable_now (see catalog()/resolve()) stays the
    scope/project/readiness authority — `gate_invokable` is accepted for
    signature compatibility but never SHRINKS the advertised set.
    """
    idx = _manifest_index()
    read_names = frozenset(READ_EXEC_ALLOWLIST)
    # Pull registry gate-advertised names (BOTH ∪ GATE_ONLY). Same
    # doctrine as in catalog() — the advertised set must include
    # GATE_ONLY consolidators (ai_lane/ai_plan/ai_worker) so the
    # tools/list emission matches the surface=GATE_ONLY claim.
    try:
        from . import tool_interface as _ti

        registry_gate = set(_ti.gate_advertised_names())
    except Exception:
        registry_gate = set()
    names = (
        set(read_names)
        | set(EDIT_ALLOWLIST)
        | set(RUN_ALLOWLIST)
        | set(PROJECT_TOOLS)
        | registry_gate
    )
    names.difference_update(FORBIDDEN_DISPATCHER_CARRIERS)
    # SSOT-05 WAR5 P1 (2026-07-15): registry_gate is unioned in raw from
    # tool_interface.gate_advertised_names() (BOTH ∪ GATE_ONLY), which does
    # NOT know about this module's _NOT_AGENT_ADVERTISED exclusion -- a
    # GATE_ONLY registry tool (e.g. dashboard_snapshot, memory_kg_get,
    # vocab_list_kinds) re-enters `names` here even after PROJECT_TOOLS
    # already dropped it. Apply the exclusion once more at the union so the
    # tools/list surface actually honors it for registry-sourced tools, not
    # just the legacy CLASS_SELECTOR/CLASS_IMPORT allowlist path.
    names.difference_update(_NOT_AGENT_ADVERTISED)
    rows = []
    for n in sorted(names):
        r = resolve(
            n,
            principal=principal,
            project=project,
            manifest_entry=idx.get(n),
            read_names=read_names,
        )
        if r["advertise"]:
            rows.append(r)
    return rows
