"""Retention classification for every ``execution_events`` event_kind.

WHY THIS EXISTS (measured 2026-08-23). Retention on the audit ledger was a
hardcoded tuple::

    event_kind IN ('tool_call_started', 'tool_call_completed', 'tool_call_failed')

That matched 40,918 of the 235,307 rows then in the table -- 17.4%. The six
largest kinds matched no rule at all, the ledger had never been pruned in 27
days, and at 701.7 MB the write lock saturated hard enough that governed tool
calls started executing with NO result audit written.

Extending the tuple would only re-arm the trap for the next emitter. The
emitter set is the authority instead: every kind carries a classification,
and :func:`unclassified_event_kinds` (enforced by
``tests/audit/test_execution_event_retention_registry.py``) fails the build
when a kind is emitted anywhere in the server package without one.

THE DISTINCTION THE CLASSES ENCODE. An audit trail records DECISIONS,
REFUSALS and PRIVILEGED ACTS. It does not record the heartbeat of a
background reconciler. High-frequency MECHANICAL events are telemetry that
happens to be written to an audit table; they are trimmed hard. DECISION and
FORENSIC rows are the reason the table exists, and retention cannot reach the
forensic ones at all.

RUNTIME vs BUILD TIME. At runtime an unregistered kind resolves to
:data:`RetentionClass.OPERATIONAL` -- bounded by the ordinary horizon and by
the count cap. That is the deliberate INVERSION of the 2026-08-23 failure,
where an unclassified kind was retained forever. Governance-shaped names
(:data:`_FORENSIC_PREFIXES`) resolve to FORENSIC even when unregistered, so
the fallback can never silently shorten the horizon on a security record.
Declaration is enforced by the test, not by dropping audit rows on the floor.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RetentionClass(str, Enum):
    """How long a kind of audit row earns its space."""

    #: Heartbeat / telemetry of a mechanism. Emitted per-occurrence by a
    #: background loop or a renderer, carries no decision, and duplicates
    #: something already recorded. Trimmed hardest.
    MECHANICAL = "mechanical"
    #: The ordinary lifecycle of work: tool calls, lane state, service
    #: bookkeeping. Real signal for "what happened this week", worthless a
    #: year later. Standard horizon, participates in the count cap.
    OPERATIONAL = "operational"
    #: A gate decision, refusal, grant, or state transition someone may have
    #: to justify. Long horizon, never count-capped -- a burst of chatter
    #: must not be able to evict a refusal.
    DECISION = "decision"
    #: Security and governance record. Never pruned, by age or by count.
    FORENSIC = "forensic"


@dataclass(frozen=True)
class RetentionPolicy:
    #: Age horizon in days. 0 means "never age-pruned".
    keep_days: int
    #: Whether the class participates in the newest-N count cap.
    count_capped: bool


#: Defaults. ``ExecutionIndexStore.auto_prune`` overrides the day counts from
#: config (``execution.retention.*``); FORENSIC is deliberately not
#: configurable, so no setting can turn retention into a shredder.
DEFAULT_POLICY: dict[RetentionClass, RetentionPolicy] = {
    RetentionClass.MECHANICAL: RetentionPolicy(keep_days=1, count_capped=True),
    RetentionClass.OPERATIONAL: RetentionPolicy(keep_days=7, count_capped=True),
    RetentionClass.DECISION: RetentionPolicy(keep_days=90, count_capped=False),
    RetentionClass.FORENSIC: RetentionPolicy(keep_days=0, count_capped=False),
}

#: SAFETY NET for a kind that reaches the runtime unregistered. A name shaped
#: like a governance record is treated as one; anything else falls to
#: OPERATIONAL, which is bounded. Not a substitute for declaring the kind --
#: the registry test still fails until it is in EVENT_KIND_RETENTION.
_FORENSIC_PREFIXES: tuple[str, ...] = (
    "security_violation",
    "freeze_",
    "rbac_",
    "anticoup_",
    "escalation_",
    "memory_promoted_to_empire",
    "global_law_",
    "soul_act",
    "governed_deletion",
    "webmcp_clear_freeze",
    "host_binding_",
    "operator_out_of_band_grant",
)

_M = RetentionClass.MECHANICAL
_O = RetentionClass.OPERATIONAL
_D = RetentionClass.DECISION
_F = RetentionClass.FORENSIC

#: The authority. Every event_kind emitted anywhere under
#: ``mcp/server/aidocs_mcp`` must appear here -- enforced by
#: ``tests/audit/test_execution_event_retention_registry.py``.
EVENT_KIND_RETENTION: dict[str, RetentionClass] = {
    # ── MECHANICAL ──────────────────────────────────────────────────
    # Host-hook intercepts. `record_hook_event` writes one row per hook
    # firing with status='observed' and no reader anywhere in the tree.
    # PreToolUse ALSO produces a native_tool_use row through
    # tool_gate_service.record_pretool_audit, so `pretooluse` is a second
    # observation of an event already audited. 32,823 rows on 2026-08-23.
    "pretooluse": _M,
    "posttooluse": _M,
    "sessionstart": _M,
    "sessionend": _M,
    "userpromptsubmit": _M,
    "precompact": _M,
    "postcompact": _M,
    "subagentstart": _M,
    "subagentstop": _M,
    "stop": _M,
    "notification": _M,
    "permissionrequest": _M,
    "prompt_classified": _M,
    # The renderer's own duration/token stamp — a THIRD row for a call that
    # already has tool_call_started + tool_call_completed carrying the same
    # numbers, with no reader. 21,610 rows on 2026-08-23.
    "tool_call": _M,
    # Background reconciler heartbeats.
    "index_sitter_reconcile": _M,  # RETIRED — see RETIRED_EVENT_KINDS
    "backlog_autosync": _M,
    "tenant_clone_reconcile": _M,
    "workflow_trigger_evaluated": _M,
    "shell_provider_resolved": _M,
    "assistant_turn_end": _M,
    "palace.call": _M,
    # Compaction bookkeeping, drained from LifecycleResult.audit_events by
    # hook_pipeline.on_post_compact. The SUCCESS row fires once per compaction
    # and has the same shape as the hook observations above — mechanical.
    #
    # #954: `compaction_epoch_bumped` was emitted for MONTHS while absent from
    # this registry, and the registry never noticed. Its static scan reads
    # `record_event(...)` call sites; these kinds arrived as
    # `audit_events.append((kind, payload))` tuples, which it cannot see. The
    # blind spot was harmless only because the consumer discarded the tuples —
    # wiring them (#622) made the kinds real, which is what forced this entry.
    "compaction_epoch_bumped": _M,
    # ── OPERATIONAL ─────────────────────────────────────────────────
    # The governed tool-call lifecycle. The only kinds the OLD filter
    # covered -- 17.4% of the table.
    "tool_call_started": _O,
    "tool_call_completed": _O,
    "tool_call_failed": _O,
    "tool_edit_completed": _O,
    # The host-side twin: the ONLY audit record that a native (non-MCP)
    # tool ran. LOAD-BEARING — prompt_context_service reads it for the
    # session's already-used-tools hint and server_plan_task_tools sums it
    # for task activity, both session/recency scoped.
    "native_tool_use": _O,
    "conductor_comms": _O,
    "lane_mailbox": _O,
    "lane_worker_state": _O,
    "session_scaffold": _O,
    "session.context.updated": _O,
    "session.handoff.updated": _O,
    "session.plan.section.replaced": _O,
    "session.plan.step.toggled": _O,
    "new_cli_session": _O,
    "user_prompt_received": _O,
    "stop_capture_memory": _O,
    "outer_gate_transport": _O,
    "future_sight_preflight": _O,
    "daemon_unreachable": _O,
    "deprecated_setting_used": _O,
    "dev_ai_run_bash_path_set": _O,
    "notify_orphan_gc": _O,
    "checkpoint_gc": _O,
    "test_runner_invocation": _O,
    "test_retry_reset": _O,
    "test_retry_block": _O,
    "palace.decision": _O,
    "palace_maintenance_backfill": _O,
    "palace_maintenance_mine": _O,
    "memory_anchor_lag": _O,
    "memory_export_lag": _O,
    "memory_palace_lag": _O,
    "memory_route_lag": _O,
    "palace_retirement_legacy_lookup_lag": _O,
    "memory_index_empty_content_degraded": _O,
    # ── DECISION ────────────────────────────────────────────────────
    # Refusals, blocks and grants. Never count-capped: a burst of chatter
    # must not be able to evict the record of a refusal.
    "project_boundary": _D,
    "lane_tool_refused": _D,
    "lane_write_refused": _D,
    "lane_tool_block": _D,
    "lane_msg_read_block": _D,
    "lane_exit_grant": _D,
    "lane_raw_tool_grant": _D,
    "lane_dispatch_refused": _D,
    "lane_dispatched": _D,
    "lane_review_verdict": _D,
    "lane_file_conflict": _D,
    "lane_auto_bound": _D,
    "raw_tool_block": _D,
    "raw_tool_grant": _D,
    "tool_policy_block": _D,
    "tool_call_refused": _D,
    "tool_decision_trace": _D,
    "sleep_spawn_refused": _D,
    "bash_policy_block": _D,
    "bash_allowlist_block": _D,
    "bash_denylist_block": _D,
    "judge_advisory": _D,
    "judge_block": _D,
    "judge_confirmable_no_intent_block": _D,
    "judge_malicious_forbidden_block": _D,
    "evaluate_tool_action_failed": _D,
    "gate_degraded": _D,
    "friction_routing_block": _D,
    # Output guard: what was redacted or withheld from a context.
    "output_guard_finding": _D,
    "output_guard_scan_failed_closed": _D,
    "run_output_guard_finding": _D,
    "run_output_guard_failed_closed": _D,
    "host_read_output_guard_finding": _D,
    "host_read_output_redacted": _D,
    "host_output_redacted": _D,
    "host_output_withheld": _D,
    # Shell: what actually executed, under which policy.
    "shell_egress_executed": _D,
    "shell_egress_refused": _D,
    "shell_policy_enforced": _D,
    "shell_policy_native_completed": _D,
    "shell_policy_shadow": _D,
    "shell_policy_shadow_error": _D,
    "shell_native_receipt_failed": _D,
    "shell_native_output_guard_degraded": _D,
    "in_process_egress": _D,
    # Durable state someone may have to justify changing.
    "config_set": _D,
    "config_write_internal": _D,
    "config_delete_internal": _D,
    "control_plane_mutation": _D,
    "control_plane_migration": _D,
    "todo_mutation": _D,
    "backlog_mutation": _D,
    # #885. The APPEND-ONLY replacement for the token-usage DELETE: an
    # operator hid figures from a report. DECISION, so a burst of chatter can
    # never count-cap away the watermark the token queries floor on -- losing
    # one would silently un-hide the numbers it was written to hide.
    "token_usage_reset": _D,
    "backlog_lww_superseded": _D,
    "skill_write": _D,
    "memory_capture": _D,
    "restore_facade": _D,
    "deslop_apply_intent": _D,
    "deslop_apply_result": _D,
    "file_deleted": _D,
    "file_hard_deleted": _D,
    "file_delete_refused": _D,
    "file_delete_noop": _D,
    "file_restored": _D,
    "file_restore_refused": _D,
    # DECISION, matching file_deleted beside it: both answer "who moved this,
    # and why" long after the fact, which is the question a reader brings to a
    # path that is no longer there. A rename is the quieter of the two — a
    # delete leaves a .TRASH entry, a rename leaves nothing but this row.
    "file_renamed": _D,
    "workflow_definition_add_initiated": _D,
    "workflow_definition_added": _D,
    "workflow_definition_update_initiated": _D,
    "workflow_definition_updated": _D,
    "workflow_definition_remove_initiated": _D,
    "workflow_definition_removed": _D,
    "workflow_definition_mutation_failed": _D,
    "workflow_definition_audit_repair_needed": _D,
    "workflow_definitions_migrated": _D,
    "workflow_procedure_markdown_not_authority": _D,
    # Session authority + managed-mode transitions.
    "managed_mode": _D,
    "managed_mode_auto_activated": _D,
    "admin_clear_reconnect": _D,
    "session.privilege_restored": _D,
    "session.degraded_set": _D,
    "session.degraded_cleared": _D,
    "session.reconnect_cleared": _D,
    "sticky_grant_registered": _D,
    "sticky_grant_refused_already_callable": _D,
    "sticky_grant_silently_dropped": _D,
    "turn_sealed": _D,
    "turn_seal_failed": _D,
    "operator_intent": _D,
    "dashboard_memory_capture_intent": _D,
    "attempted_inactive_memory_read": _D,
    "attempted_palace_maintenance": _D,
    "agent_handoff": _D,
    "worker_spawned": _D,
    "worker_killed": _D,
    "worker_exited": _D,
    "machine_concurrency_reset": _D,
    "daemon_lifecycle_requested": _D,
    "daemon_lifecycle_refused": _D,
    "daemon_stop_unattributed": _D,
    "prompt_mutation_failed": _D,
    "prompt_mutation_rolled_back": _D,
    "prompt_submit_transaction_degraded": _D,
    "prompt_submit_post_commit_degraded": _D,
    "preflight_degraded": _D,
    # ── FORENSIC ────────────────────────────────────────────────────
    # Never pruned, by age or by count.
    #
    # Compaction sub-effect FAILURES (#622). Three reasons they are forensic
    # rather than mechanical, and the volume argument is the weakest of them:
    #   1. They fire ONLY on failure, so the row cost is negligible — unlike
    #      `compaction_epoch_bumped` above, which fires every compaction.
    #   2. Each records a bookkeeping guarantee that SILENTLY DID NOT HAPPEN.
    #      `strike_reset` is the sharpest: lifecycle_service:1253 states the
    #      directive ("a compacted agent is effectively a fresh mind, so the
    #      freeze threshold starts over"), so a swallowed failure means the
    #      agent keeps its strike count and freezes earlier than the law says.
    #      That is security evidence and belongs beside the rows above it.
    #   3. THE DEFECT BEING FIXED IS "THIS WAS INVISIBLE". Pruning these would
    #      restore the invisibility on a delay — the question would be
    #      answerable for a while and then silently stop being answerable,
    #      which is worse than never having the row, because the absence would
    #      read as "no failure".
    # #949 depends on (3): its unexplained postcompact-row gap is asked against
    # exactly this evidence.
    "compaction_token_reset_failed": _F,
    "compaction_epoch_bump_failed": _F,
    "compaction_strike_reset_failed": _F,
    "security_violation_recorded": _F,
    "security_violation_strike": _F,
    "security_violation_strike_voided": _F,
    "security_violation_reset": _F,
    "security_violation_freeze_created": _F,
    "security_violation_threshold_warning": _F,
    "freeze_admin_clear_ambiguous": _F,
    "freeze_admin_clear_tier_refused": _F,
    "webmcp_clear_freeze_refused": _F,
    "webmcp_clear_freeze_noop": _F,
    "stewardship_deferred_frozen": _F,
    "rbac_denied": _F,
    "rbac_bootstrap_bypass": _F,
    "tenant_rbac_bootstrapped": _F,
    "tenant_rbac_adopted_existing": _F,
    "tenant_rbac_bootstrap_skipped": _F,
    "escalation_requested": _F,
    "escalation_approved": _F,
    "escalation_denied": _F,
    "escalation_consumed": _F,
    "escalation_grant_consumed": _F,
    "anticoup_verdict": _F,
    "global_law_retire_requested": _F,
    "global_law_retired": _F,
    "memory_promoted_to_empire": _F,
    "soul_act": _F,
    "governed_deletion": _F,
    "host_binding_approve": _F,
    "host_binding_revoke": _F,
    "operator_out_of_band_grant": _F,
    "self_approve_mint_failed": _F,
    # A LOST audit row is the one fact that must never itself be pruned.
    "audit_emit_failed": _F,
}


def classify(event_kind: str) -> RetentionClass:
    """Resolve a kind's retention class. Never raises, never returns None.

    NOT on the deletion path. Retention builds its SQL from
    :func:`kinds_in_class` and :func:`forensic_prefixes`, both of which read
    source literals, so nothing here decides what gets deleted -- this is
    the answer to "what class is this kind", for callers and for tests.
    The normalisation below is therefore defence in depth for a direct
    caller, NOT the guard that protects a stored row.

    That guard is in two other places, and it exists because the conductor
    mutation gate (2026-08-23, mutant R10) asked whether a padded kind was
    reachable: the write boundary strips (``record_event_on_connection``)
    and the retention SQL reads through ``_KIND_EXPR``'s TRIM for the rows
    written before it did. Removing the strip here changes no deletion
    outcome; removing either of those two silently gives a security record
    a seven-day horizon.
    """
    kind = str(event_kind or "").strip()
    hit = EVENT_KIND_RETENTION.get(kind)
    if hit is not None:
        return hit
    if kind.startswith(_FORENSIC_PREFIXES):
        return RetentionClass.FORENSIC
    return RetentionClass.OPERATIONAL


def kinds_in_class(retention_class: RetentionClass) -> tuple[str, ...]:
    return tuple(
        sorted(k for k, v in EVENT_KIND_RETENTION.items() if v is retention_class)
    )


def forensic_prefixes() -> tuple[str, ...]:
    return _FORENSIC_PREFIXES


# ── Emitter discovery ───────────────────────────────────────────────────
# A static scan, on purpose. The point is to fail a BUILD when someone adds
# an emitter, which means it must work without importing (and therefore
# without running) the module that holds the new emitter.

_DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parent

#: Positional index of `event_kind` for the two positional-friendly writers.
_POSITIONAL_EVENT_KIND: dict[str, int] = {
    "record_event": 1,  # record_event(project_root, event_kind, source_kind)
    "record_event_on_connection": 2,  # (conn, project_root, event_kind, ...)
}

#: Dynamic emitter sites that CANNOT be resolved statically, each with the
#: kinds it may emit. Keyed by "<relative path>::<kwarg>=<expression>" so the
#: declaration survives the line moving. A site that reaches
#: :func:`undeclared_dynamic_sites` fails the registry test until it is
#: declared here -- an unreadable emitter is exactly how the drift started.
DECLARED_DYNAMIC_SOURCES: dict[str, tuple[str, ...]] = {
    # hook_pipeline.record_hook_event: `event_kind = event_name.lower()`.
    # The host hook vocabulary AIDOCS installs (claude_hooks_install) plus
    # the events other hosts dispatch through the same entrypoint.
    "hook_pipeline.py::phase=event_kind": (
        "sessionstart",
        "userpromptsubmit",
        "pretooluse",
        "posttooluse",
        "permissionrequest",
        "precompact",
        "postcompact",
        "stop",
        "subagentstart",
        "subagentstop",
        "notification",
        "sessionend",
    ),
    # _compute_row_hash(event_kind=row["event_kind"]) — a HASH input over an
    # already-written row, not an emitter. Declared so the scan stays honest
    # rather than carrying a name-based denylist.
    "execution_index_store.py::event_kind=row['event_kind']": (),
    # hook_pipeline.on_post_compact drains LifecycleResult.audit_events —
    # `for _kind, _payload in ...: record_event(project_root, _kind, ...)`. The
    # kind is a LOOP VARIABLE, so no static scan can read it.
    #
    # #954: this whole CHANNEL was invisible to the scanner before the drain
    # existed. The tuples were built by lifecycle_service and DISCARDED by this
    # caller, so `compaction_epoch_bumped` was emitted for months while absent
    # from EVENT_KIND_RETENTION and nothing detected it. The registry was
    # accidentally correct because the channel was accidentally inert. Wiring
    # it (#622) is what made the declaration necessary — and this entry is the
    # mechanism working as designed, not an exception to it.
    "hook_pipeline.py::event_kind=_kind": (
        "compaction_epoch_bumped",
        "compaction_token_reset_failed",
        "compaction_epoch_bump_failed",
        "compaction_strike_reset_failed",
    ),
    # tool_call_log.record: `event_kind = _PHASE_EVENT_KIND.get(phase, phase)`.
    # Every kind it can write is already counted -- either as a value of the
    # phase mapping or at the literal `phase=` call site it forwards.
    "tool_call_log.py::event_kind=event_kind": (),
    # mcp_server._record_tool_execution_state: `phase =
    # _EVENT_KIND_TO_PHASE.get(event_kind, event_kind)` — the inverse of the
    # same mapping, over an event_kind its callers pass as a literal.
    "mcp_server.py::phase=phase": (),
    # judge_overrides: forwards a kind resolved from the judge decision it
    # was handed; the literals live at the gate_tool call sites.
    "judge_overrides.py::event_kind=event_kind": (),
}

#: Kinds no longer emitted by this tree but still present in databases on
#: disk. Retention must keep classifying them or those rows become the next
#: unprunable stratum. Removing an emitter does not remove its rows.
RETIRED_EVENT_KINDS: frozenset[str] = frozenset(
    {
        # Collapsed 2026-08-23 into the O(1) `index_reconcile_state`
        # heartbeat row. 30,202 per-occurrence rows were in the saturated
        # table; historical ones still need an age horizon.
        "index_sitter_reconcile",
    }
)


def _module_constants(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """Module-level ``NAME = "literal"`` bindings, pooled across the package.

    Pooled rather than per-file because emitters routinely import their kind
    constant from the module that owns the feature.
    """
    consts: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            else:
                continue
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    consts.setdefault(target.id, set()).add(value.value)
    return consts


def _phase_map(trees: dict[Path, ast.Module]) -> dict[str, str]:
    """The ``*_PHASE_EVENT_KIND`` phase -> event_kind mapping.

    ``tool_call_log.record(phase=...)`` writes the MAPPED string, so a
    ``phase="guard_finding"`` call site emits ``output_guard_finding``. Every
    value in the table is reachable, so each is an emitted kind whether or
    not a literal call site for it exists today.
    """
    found: dict[str, str] = {}
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
                value = node.value
            else:
                continue
            if not any("PHASE_EVENT_KIND" in n.upper() for n in names):
                continue
            if not isinstance(value, ast.Dict):
                continue
            for key, item in zip(value.keys, value.values, strict=False):
                if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                    continue
                phase = (
                    key.value
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    else item.value
                )
                found[phase] = item.value
    return found


def _local_kind_positions(
    trees: dict[Path, ast.Module],
) -> dict[Path, dict[str, int]]:
    """Per-file positional index of the event-kind argument for a LOCALLY
    DEFINED helper that shadows one of the known positional writer names.

    ``server_code_tools`` defines its own ``record_event(kind, status,
    payload)`` -- a different signature from the store's
    ``record_event(project_root, event_kind, source_kind)``. Reading index 1
    for both harvests the STATUS string as an event kind. The definition is
    in the same file as the call, so read it instead of guessing.

    Deliberately narrow on both axes: only names the scanner already treats
    as positional writers, and only within the defining file. Matching every
    function with a ``kind`` parameter turned symbol-search, judge-taxonomy
    and intent-token helpers into ~40 phantom emitters.
    """
    positions: dict[Path, dict[str, int]] = {}
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name not in _POSITIONAL_EVENT_KIND:
                continue
            names = [
                a.arg
                for a in (*node.args.posonlyargs, *node.args.args)
                if a.arg not in ("self", "cls")
            ]
            for wanted in ("event_kind", "kind"):
                if wanted in names:
                    positions.setdefault(path, {})[node.name] = names.index(wanted)
                    break
    return positions


def _param_names(tree: ast.Module) -> dict[int, set[str]]:
    """Parameter names in scope at each function's body, keyed by the
    function node's id. A wrapper forwarding its own ``event_kind`` parameter
    is a PASS-THROUGH: the literal lives at its callers, which are scanned."""
    scopes: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            names = {
                a.arg
                for a in (
                    *args.posonlyargs,
                    *args.args,
                    *args.kwonlyargs,
                )
            }
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
            scopes[id(node)] = names
    return scopes


def _parse_tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None


def _scan(source_root: Path | None) -> tuple[dict[str, list[str]], list[str]]:
    """Return (kind -> emitter sites, unresolved dynamic sites).

    Cached per resolved root for the life of the process: a full parse of
    the server package costs ~20s, and the registry test asks three separate
    questions of the same answer.
    """
    root = Path(source_root) if source_root is not None else _DEFAULT_SOURCE_ROOT
    key = str(root.resolve())
    hit = _SCAN_CACHE.get(key)
    if hit is None:
        hit = _scan_uncached(root)
        _SCAN_CACHE[key] = hit
    kinds, dynamic = hit
    return {k: list(v) for k, v in kinds.items()}, list(dynamic)


_SCAN_CACHE: dict[str, tuple[dict[str, list[str]], list[str]]] = {}


def _scan_uncached(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    trees: dict[Path, ast.Module] = {}
    for py in sorted(root.rglob("*.py")):
        tree = _parse_tree(py)
        if tree is not None:
            trees[py] = tree

    consts = _module_constants(trees)
    phases = _phase_map(trees)
    local_positions = _local_kind_positions(trees)
    kinds: dict[str, list[str]] = {}
    dynamic: list[str] = []

    for kind in set(phases.values()):
        kinds.setdefault(kind, []).append("<phase mapping>")

    for py, tree in trees.items():
        try:
            rel = py.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - defensive
            rel = py.name
        params = _param_names(tree)
        enclosing: dict[int, set[str]] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in ast.walk(fn):
                    enclosing.setdefault(id(inner), set()).update(params[id(fn)])

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            callee = (
                fn.attr
                if isinstance(fn, ast.Attribute)
                else (fn.id if isinstance(fn, ast.Name) else "")
            )
            candidates: list[tuple[str, ast.expr]] = [
                (kw.arg, kw.value)
                for kw in node.keywords
                if kw.arg in ("event_kind", "phase")
            ]
            # A bare-Name call resolves against the LOCAL definition, whose
            # signature may differ from the store's; an attribute call is the
            # store/hub API.
            idx = (
                local_positions.get(py, {}).get(callee)
                if isinstance(fn, ast.Name)
                else None
            )
            if (
                idx is None
                and isinstance(fn, ast.Name)
                and callee in _POSITIONAL_EVENT_KIND
                and callee in enclosing.get(id(node), set())
            ):
                # INJECTED callback: `record_event` arrives as a parameter,
                # so its signature is the INJECTOR's, not the store's. Both
                # shapes are in this tree -- server_code_tools injects
                # `(event_kind, status, payload)` while empire_soul_gate and
                # skill_store inject the store's own root-first method. Take
                # the FIRST position that reads as a string: index 1 alone
                # harvested the STATUS ('intent', 'applied', 'rolled_back')
                # and lost deslop_apply_intent/_result entirely.
                for probe in (0, 1):
                    if len(node.args) > probe and _resolve(node.args[probe], consts):
                        idx = probe
                        break
            if idx is None and not (
                isinstance(fn, ast.Name) and callee in enclosing.get(id(node), set())
            ):
                idx = _POSITIONAL_EVENT_KIND.get(callee)
            if idx is not None and len(node.args) > idx:
                candidates.append(("event_kind", node.args[idx]))

            for kwarg, value in candidates:
                site = f"{rel}:{node.lineno}"
                resolved = _resolve(value, consts)
                if resolved is not None:
                    for kind in resolved:
                        # `record(phase=...)` writes the MAPPED kind; an
                        # unmapped phase passes through to the column as-is.
                        final = phases.get(kind, kind) if kwarg == "phase" else kind
                        kinds.setdefault(final, []).append(site)
                    continue
                if isinstance(value, ast.Name) and value.id in enclosing.get(
                    id(node), set()
                ):
                    # Pass-through wrapper parameter; its callers carry the
                    # literal and are scanned in this same pass.
                    continue
                dynamic.append(f"{rel}::{kwarg}={ast.unparse(value)}")

    return kinds, sorted(set(dynamic))


def _resolve(value: ast.expr, consts: dict[str, set[str]]) -> set[str] | None:
    if isinstance(value, ast.Constant):
        return {value.value} if isinstance(value.value, str) else None
    if isinstance(value, ast.Name):
        hit = consts.get(value.id)
        return set(hit) if hit else None
    if isinstance(value, ast.IfExp):
        left = _resolve(value.body, consts)
        right = _resolve(value.orelse, consts)
        if left and right:
            return left | right
        return None
    return None


def discover_emitted_event_kinds(
    source_root: Path | None = None,
) -> dict[str, list[str]]:
    """Every ``event_kind`` this source tree can write, mapped to its sites.

    Includes the kinds named by :data:`DECLARED_DYNAMIC_SOURCES`, so a
    declared dynamic emitter still has to classify what it emits.
    """
    kinds, _ = _scan(source_root)
    for site, declared in DECLARED_DYNAMIC_SOURCES.items():
        for kind in declared:
            kinds.setdefault(kind, []).append(f"{site} (declared)")
    return kinds


def discover_dynamic_emitter_sites(source_root: Path | None = None) -> list[str]:
    _, dynamic = _scan(source_root)
    return dynamic


def undeclared_dynamic_sites(source_root: Path | None = None) -> list[str]:
    """Dynamic sites with no declared kind set -- an emitter nobody can read."""
    declared = tuple(DECLARED_DYNAMIC_SOURCES)
    return [
        site
        for site in discover_dynamic_emitter_sites(source_root)
        if not any(site.endswith(key) for key in declared)
    ]


def unclassified_event_kinds(
    source_root: Path | None = None,
) -> dict[str, list[str]]:
    """Emitted kinds with no entry in :data:`EVENT_KIND_RETENTION`."""
    return {
        kind: sites
        for kind, sites in discover_emitted_event_kinds(source_root).items()
        if kind not in EVENT_KIND_RETENTION
    }
