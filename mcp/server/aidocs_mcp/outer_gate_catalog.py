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
        # king 2026-06-20: never agent-facing on EITHER surface (local stdio or gate).
        # Host/desktop UI tools the agent cannot drive + operator-only authority an
        # agent must never self-serve (self-approving an escalation / granting itself
        # membership is the control-plane breach to prevent). The operator drives these
        # from the dashboard, not the agent tool surface. Complements the PROJECT_TOOLS
        # exclusion (_NOT_AGENT_ADVERTISED) so both surfaces drop them.
        "tauri_todo_list",
        "tauri_backlog_list",
        "approve_escalation",
        "deny_escalation",
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
        # two-phase confirm (king 2026-06-20). The local stdio host-config writer is a
        # separate @server.tool. notifications_clear stays host-local operator UX.
        "notifications_clear",  # operator UX on the host
        # ai_backlog promoted to the gate (king 2026-06-20) — read-only backlog list.
    },
)


def is_local_only(name: str) -> bool:
    """True iff `name` is registered for the local agent but must be
    refused on the outer-gate's remote surface. See `LOCAL_ONLY` for the
    rationale per entry. Distinct from `local_hidden` (which hides on
    BOTH surfaces).
    """
    return name in LOCAL_ONLY


def local_hidden(name: str) -> bool:
    """True iff `name` must be hidden from the LOCAL agent's tool list too (the
    single invisible-to-both carve-out). Local shows everything else it registers
    (full-trust); the gate applies its own remote classification on top.
    """
    return name in HIDDEN_EVERYWHERE


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

    Doctrine 2026-05-29 (king re-seal): when the tool is registry-
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
                        "Required to actually apply. The speakable phrase "
                        "'confirm select project' (case/punctuation/space-"
                        "insensitive). Omit on the first call to receive the "
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
    "config_set": {
        "desc": (
            "OWNER/ADMIN only: set a bound-org config value. Two-phase confirm "
            "(confirm_token='confirm config set'); tenant-scoped + audited."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "setting_path": {"type": "string"},
                "value": {},
                "scope": {"type": "string"},
                "scope_key": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["setting_path", "value"],
        },
        "cls": CLASS_SELECTOR,
    },
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
        "only via CodeNexus credentials.",
        "schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "ref": {"type": "string"}},
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
# with surface="both" and cls=CLASS_SELECTOR / CLASS_IMPORT becomes a
# project-tool. Migrated entries take precedence over the hand-rolled
# specs above with the same name (so a future commit can fold the
# hand-rolled ones into the registry without touching this code).
def _extend_project_specs_from_registry() -> None:
    from . import tool_interface as _ti

    for name, spec in _ti.REGISTRY.items():
        if spec.surface != "both":
            continue
        if spec.cls not in (CLASS_SELECTOR, CLASS_IMPORT):
            continue
        PROJECT_TOOL_SPECS[name] = {
            "desc": spec.description,
            "schema": _ti.schema_for(spec),
            "cls": spec.cls,
        }


_extend_project_specs_from_registry()

# NOT agent-advertised (king 2026-06-20): host/desktop tools + operator-only
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
        "project_grant_member",
        "project_revoke_member",
        "project_acl_list",
    }
)
PROJECT_READ_TOOLS = frozenset(
    n
    for n, s in PROJECT_TOOL_SPECS.items()
    if s["cls"] == CLASS_SELECTOR and n not in _NOT_AGENT_ADVERTISED
)
PROJECT_EDIT_TOOLS = frozenset(
    n
    for n, s in PROJECT_TOOL_SPECS.items()
    if s["cls"] == CLASS_IMPORT and n not in _NOT_AGENT_ADVERTISED
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


def _manifest_index():
    from . import outer_gate_manifest as man

    return {e["name"]: e for e in man.build_manifest()}


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
    # HIDDEN_EVERYWHERE wins over EVERYTHING: a tool the king ruled off BOTH agent
    # surfaces (host/desktop tauri_*, escalation approve/deny, membership grant/
    # revoke/ACL) is INTERNAL regardless of registry surface or any — possibly
    # rebuilt — allowlist membership. This is the SERVING-surface guard: the live
    # gate's host_compat patch rebuilds the project allowlists, and must never be
    # able to resurface a hidden tool into the advertised/routable set.
    if name in HIDDEN_EVERYWHERE:
        return CLASS_INTERNAL
    # Registry (new single source of truth) wins over legacy allowlists.
    # tool_interface.REGISTRY entries declare both their surface and
    # their class; the legacy CLASS_*_ALLOWLISTS below remain as the
    # fallthrough for tools that haven't been migrated yet.
    from . import tool_interface as _ti

    spec = _ti.get(name)
    if spec is not None:
        if spec.surface == "local_only":
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


# king 2026-06-28: tools that stay gate-advertised (surface=BOTH) but are ABSENT from
# the catalog (in_catalog=False) for every NON-super_admin principal — hidden, not merely
# scope-blocked — so only the operator's own super_admin account ever sees them in
# tool_catalog. Invoke authority is independently enforced in outer_gate.execute()
# (e.g. ai_deploy → super_admin + AIDOCS_PRIVATE + ref allowlist); this set governs
# only catalog VISIBILITY, never execution.
SUPER_ADMIN_ONLY_CATALOG: frozenset[str] = frozenset({"ai_deploy"})


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
    """
    from .outer_gate_grants import resolve_execution_grant

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
    cls = classify(name, manifest_entry, read_names)
    spec = PROJECT_TOOL_SPECS.get(name, {})

    def out(
        *,
        tier,
        visible,
        executable,
        blocked_by,
        grant,
        schema,
        desc,
        in_catalog=True,
        annotations=None,
    ):
        # `advertise`: the STABLE tools/list superset — every call-path class
        # (read/edit/run/selector/import) is ALWAYS advertised when the server
        # supports it, INDEPENDENT of the caller's current scope, so an MCP host
        # (ChatGPT) registers a stable resource list and a call without scope
        # fails insufficient_scope (NOT "Resource not found"). `visible` stays the
        # scope-permitted view (what the caller may execute now); tools/call +
        # executable_now remain the scope/project/readiness authority. Discoverable
        # (no remote call path) and internal-only are never advertised.
        advertise = cls in (CLASS_READ, CLASS_EDIT, CLASS_RUN, CLASS_SELECTOR, CLASS_IMPORT)
        # king 2026-06-28: super-admin-only tools (ai_deploy) are absent from tools/list too
        # (not just tool_catalog) for non-super_admin — a controlled exception to the stable-
        # advertise doctrine: these tools must never appear to a non-super_admin principal on
        # ANY surface. The operator's own super_admin still gets the stable advertised entry.
        if name in SUPER_ADMIN_ONLY_CATALOG and not _is_super_admin_principal:
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
            name in SUPER_ADMIN_ONLY_CATALOG and not _is_super_admin_principal
        )
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
            "outputSchema": _OUTPUT_OBJ,
            "annotations": ann,
            "description": desc,
        }

    if cls == CLASS_READ:
        g = resolve_execution_grant(principal=principal, project=project, kind="read")
        # SINGLE SOURCE OF TRUTH (same doctrine as the EDIT branch below): a READ
        # tool DECLARED in tool_interface carries its real typed schema (built
        # from the @tool fn signature by _ti.schema_for), description, and MCP
        # annotations. RELAY them — the catalog only surfaces, never re-authors.
        # Only a read NOT yet migrated into the registry falls back to the
        # manifest's AST-inspected param list, normalized to valid JSON Schema.
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
    if cls == CLASS_EDIT:
        g = resolve_execution_grant(principal=principal, project=project, kind="edit")
        # ── Registry-backed EDIT tools (ai_lane / ai_plan / ai_worker
        #    + every future BOTH/GATE_ONLY EDIT) carry their OWN
        #    schema, description, and MCP annotations on the @tool
        #    decorator. Surfacing the legacy _EDIT_SCHEMA (path /
        #    old_string / new_string) for these would be doctrinally
        #    wrong: hosts (ChatGPT, Claude) would see an `action=`-shaped
        #    tool advertised as a string-replacer, get past
        #    insufficient_scope, then hit a schema mismatch at
        #    tools/call. Legacy ai_str_replace (the only NON-registry
        #    EDIT) stays on _EDIT_SCHEMA + the generic description.
        # Doctrine (2026-05-29): the per-tool schema/description/
        # annotations live with the @tool declaration; the catalog
        # only RELAYS them. One source of truth.
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
            # Doctrine 2026-05-29 (king-directed re-seal — clean-VPS
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
    if cls == CLASS_SELECTOR:
        vis = "catalog" in scope
        return out(
            tier="R",
            visible=vis,
            executable=vis,
            blocked_by=("" if vis else "insufficient_scope"),
            grant="catalog",
            schema=spec.get("schema", _S_OBJ),
            desc=spec.get("desc", name),
        )
    if cls == CLASS_RUN:
        # DISTINCT grant: detached shell is gated by project_run (NOT tier_m_edit
        # / project_import). The command itself is still gated by the canonical
        # ai_run law (bash_policy/heuristic_judge/freeze/destructive floor).
        from .outer_gate_grants import resolve_execution_grant as _g

        vis = "project_run" in scope
        gd = _g(principal=principal, project=project, kind="run")
        return out(
            tier="M",
            visible=vis,
            executable=gd.allow,
            blocked_by=("" if gd.allow else gd.reason),
            grant="project_run",
            schema=_RUN_SCHEMAS.get(name, _S_OBJ),
            desc=_RUN_DESC.get(name, name),
        )
    if cls == CLASS_IMPORT:
        # DISTINCT grant: project import/sync is gated by project_import, NOT
        # tier_m_edit. blocked_by names the precise grant required.
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
    if cls == CLASS_DISCOVERABLE:
        e = manifest_entry or {}
        tier = e.get("tier", "?")
        # Eligible but not invokable here: Tier-A (phase not built) or an eligible
        # read not wired into the executor. NOT advertised invokable; shown in the
        # full catalog with an accurate reason.
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
