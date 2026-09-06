"""Operation-class registry — doctrine-driven gate behavior.

Castle / Empire doctrine 2026-05-04:
    Remedial operations (clear a freeze, approve/deny an escalation,
    kill a runaway run) are CLEANUP, not work. They cannot be locked
    behind the very freezes they manage.

This registry maps tool/CLI/dashboard-action names to an operation
class. Gates consult the registry to decide which gates apply. The
binding is HARDCODED here — not a caller-passed flag. Callers cannot
escalate their own privilege by passing `include_freeze=False`; they
must be in the registry.

Operation classes:

  - remedial_freeze_management
        Bypasses the FREEZE gate only.
        Still requires kill_switch+dev OR RBAC admin.
        Audit always.
        Examples: admin_clear_freeze, rbac_approve_escalation,
                  rbac_deny_escalation.

  - destructive_cleanup
        Bypasses the FREEZE gate only.
        For terminal-cleanup ops on running processes.
        Examples: ai_run_kill (kill a runaway).

  - normal
        Default. All gates apply.

If a tool is not in the registry, it is `normal`. Adding a tool to
a non-normal class is a doctrine decision; lint should require a
matching entry in `enforcement-controller-doctrine.md` § IV.

The registry is read-only; do not mutate at runtime.
"""

from __future__ import annotations

from enum import Enum


class OperationClass(str, Enum):
    NORMAL = "normal"
    REMEDIAL_FREEZE_MANAGEMENT = "remedial_freeze_management"
    DESTRUCTIVE_CLEANUP = "destructive_cleanup"


# Hardcoded mapping. Tool names are matched after stripping the
# "mcp__aidocs__" / "mcp__" namespace prefix so both bare and
# namespaced calls resolve to the same class.
_TOOL_OPERATION_CLASS: dict[str, OperationClass] = {
    "admin_clear_freeze": OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
    "rbac_approve_escalation": OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
    "rbac_deny_escalation": OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
    "ai_run_kill": OperationClass.DESTRUCTIVE_CLEANUP,
}

# #588 D4. Kept in a SEPARATE table from _TOOL_OPERATION_CLASS so that
# `bypasses_freeze_gate` — what the ai_run/enforce path uses to skip the
# freeze check on remedial WORK — keeps exactly its old meaning. These
# names are consulted by the PRE-TOOL freeze gate only.
#
# Law 311bf3e6 — a named remedy must be REACHABLE. The remedy for a
# freeze was itself inside the freeze's blast radius:
# `admin_clear_freeze` bypassed the freeze gate by class, but the agent
# could not FETCH ITS SCHEMA (ToolSearch refused), could not ask the
# operator (ai_qa refused), and could not tell anyone anything (the
# notification path refused). A bypass you cannot reach is not a bypass.
#
# This table is deliberately NOT "read-only tools" and NOT "safe tools".
# Membership requires all three:
#   1. the tool performs NO work in the project — it neither runs a
#      command nor writes a byte of project state;
#   2. it is on the shortest path from "I am frozen" to either clearing
#      the freeze or telling a human about it;
#   3. passing this gate grants it NO authority — every exempted surface
#      still faces its own authorization (admin_clear_freeze still goes
#      through ClearFreezeService's capability gate and
#      freeze_clear_ladder_block, which refuse a self-clear).
# Discovery tools (ai_find/ai_bundle/ai_get_lines) are NOT here: they are
# how an agent keeps WORKING, and working is what a freeze is for.
_REMEDY_REACHABILITY_TOOLS: frozenset[str] = frozenset(
    {
        # Schema discovery. Without it the agent cannot even LOAD the
        # signature of admin_clear_freeze or ai_qa — the measured
        # catch-22. Returns tool definitions; touches no project state.
        "toolsearch",
        # The documented in-band substitute for the hidden
        # admin_clear_freeze: ask the operator / conductor for a verdict.
        # It writes a question, not a change.
        "ai_qa",
        # The "tell a human something is wrong" path. A freeze that
        # silences the alarm about itself is how a session dies unseen.
        "pushnotification",
        # The map of what a freeze IS (2026-08-25). ai_gate_explain reads
        # code-level tables (ladder, taxonomy, strike families, freeze kinds,
        # this very list) and returns them; it runs nothing, writes nothing,
        # and its only use is the moment after a refusal — which for a freeze
        # is exactly the moment every other tool is refused. All three
        # membership conditions above hold.
        "ai_gate_explain",
    },
)

#: Disarm-only exemption. ScheduleWakeup(stop=True) is the conductor
#: STOPPING its own timer — refusing it left the loop firing into a wall
#: for hours. ARMING a wakeup schedules future work and stays frozen, so
#: this exemption is on the ARGUMENT, not on the tool name.
_DISARM_ONLY_TOOLS: dict[str, str] = {"schedulewakeup": "stop"}

#: Freeze-gate EXEMPTION REASONS — the vocabulary ``freeze_remedy_reachable``
#: returns. One definition each so the producer and every caller/assertion
#: name the same string instead of re-typing it (same convention as
#: ``session_freeze_store.EXPIRY_REASON_TTL``).
#:
#: Deliberately NOT members of ``OperationClass``. That enum is the tool-CLASS
#: registry ``operation_class_for`` resolves against, and these are REASONS a
#: gate did not fire — a different vocabulary with its own ``freeze_`` prefix
#: (``tool_gate_service`` adds ``freeze_out_of_jurisdiction`` to it). #588 D4
#: shipped a fourth enum member, ``REMEDY_REACHABILITY = "remedy_reachability"``,
#: that ``operation_class_for`` could never return — it was deliberately kept
#: out of ``_TOOL_OPERATION_CLASS`` — and whose value did not even match the
#: string the code returns. It was removed rather than wired: an enum member
#: its own resolver cannot produce is a lie in the type.
REASON_REMEDY_REACHABILITY = "freeze_remedy_reachability"
REASON_REMEDIAL_OPERATION = "freeze_remedial_operation"
REASON_DISARM_ONLY = "freeze_disarm_only"
REASON_REPORT_MODE = "freeze_report_mode"

#: #640 — REPORT-MODE reachability. Measured: four lanes were frozen AND MUTE.
#: The standing operator instruction was "on any freeze or strike, ai_msg the
#: conductor immediately"; the freeze gate refused ai_msg, so the instruction
#: was unexecutable in the only situation it was written for. A smoke alarm
#: wired to the circuit it watches is not a smoke alarm.
#:
#: WHY THIS IS NOT `_REMEDY_REACHABILITY_TOOLS`. That table is keyed on the
#: TOOL NAME, and ai_msg is not one thing: it carries a reporting channel AND
#: mutating coordination verbs. Name-exempting it would un-gate
#: ``xaacp_cancel`` — retracting a message another actor is waiting on — which
#: is WORK, and stopping work is exactly what a freeze is for. So the
#: exemption is on the ARGUMENT, the shape ``ScheduleWakeup(stop=True)``
#: already set: only the modes below, and only when the mode is legible.
#:
#: MEMBERSHIP, per mode. A mode joins when it REPORTS a state or READS the
#: caller's own mail, and leaves the project's work untouched:
#:   send / reply        — say "I am blocked" to the seats. Nothing runs.
#:   inbox               — hear the answer. A one-way alarm is half a channel.
#:   xaacp_send          — the same report, actor-routed (a lane has no seat).
#:   xaacp_inbox         — read one's own routed mail.
#:   xaacp_directory     — "handles only, no authority conferred"; without it
#:                         a lane cannot name who to report TO.
#: DELIBERATELY EXCLUDED — these stay frozen:
#:   xaacp_reply         — records a DECISION on a pending message and resumes
#:                         another actor's blocked ``xaacp_wait``. It drives
#:                         someone else's control flow. ``decision='blocked'``
#:                         is report-SHAPED, which makes this the one genuinely
#:                         ambiguous mode; ambiguity resolves toward GATED,
#:                         and the caller still has send / xaacp_send /
#:                         ai_issues to report the same fact.
#:   xaacp_cancel        — retracts a delivered message: mutation of shared
#:                         coordination state, and abusable as a side channel.
#:   xaacp_wait          — parks the caller until a terminal decision. Not a
#:                         report; and an agent silently blocking inside a
#:                         freeze is the very silence #640 was filed about.
#:   wait_next           — parks the caller until a new inbox delivery. Same
#:                         classification: receive primitive, not block report.
#:
#: NO NEW AUTHORITY. Passing this gate grants none: an unmapped caller is still
#: refused ``send``/``reply`` by ``ai_msg_dispatch``'s seat check, XAACP still
#: routes from the caller's OWN canonical actor
#: (``xaacp_resolve_caller_route``), and the middleware still writes
#: ``tool_call_started`` / ``tool_call_completed`` for every one of these
#: calls. Task-free and freeze-reachable, never attribution-free.
_REPORT_MODE_TOOLS: dict[str, frozenset[str]] = {
    "ai_msg": frozenset(
        {
            "send",
            "reply",
            "inbox",
            "xaacp_send",
            "xaacp_inbox",
            "xaacp_directory",
        },
    ),
}


def _bare(tool_name: str) -> str:
    norm = str(tool_name or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if norm.startswith(prefix):
            return norm[len(prefix) :]
    return norm


def freeze_remedy_reachable(tool_name: str, tool_input: object = None) -> str:
    """Name the reason this tool may run WHILE a freeze is in force.

    Returns an empty string when the freeze applies — the default for
    everything. Never consults caller-supplied flags: membership is
    hardcoded here, exactly like ``bypasses_freeze_gate``, so a caller
    cannot talk its way past a freeze by passing an argument.
    """
    bare = _bare(tool_name)
    if not bare:
        return ""
    if bare in _REMEDY_REACHABILITY_TOOLS:
        return REASON_REMEDY_REACHABILITY
    if bypasses_freeze_gate(bare):
        # admin_clear_freeze / rbac_(approve|deny)_escalation / ai_run_kill:
        # doctrine-exempt at the ai_run layer since 2026-05-04. The
        # PRE-TOOL gate refused them anyway, which is precisely what made
        # the documented remedy unreachable in practice.
        return REASON_REMEDIAL_OPERATION
    disarm_key = _DISARM_ONLY_TOOLS.get(bare)
    if disarm_key and isinstance(tool_input, dict) and bool(tool_input.get(disarm_key)):
        return REASON_DISARM_ONLY
    return report_mode_grant(tool_name, tool_input)


def report_mode_grant(tool_name: str, tool_input: object = None) -> str:
    """#640 — ONE HOME for "this call is a BLOCK REPORT and nothing else".

    Returns ``REASON_REPORT_MODE`` when ``tool_name`` is a declared report
    surface AND the payload legibly names one of its report-only modes;
    ``""`` otherwise. Membership lives in ``_REPORT_MODE_TOOLS`` above,
    which documents why each mode is in or out.

    Split out of ``freeze_remedy_reachable`` because the freeze gate is NOT
    the only refusal that can silence a blocked agent. ``ToolGate`` runs
    ``reconnect_required`` BEFORE ``session_freeze_pretool``, so an agent that
    is frozen AND reconnect-flagged never reached the freeze exemption at
    all — the fresh-CLI refusal ate the report first, and the freeze card's
    "ai_msg the conductor" instruction was unexecutable again, one gate up.
    Both gates now consult THIS function, so the report-only mode set is
    declared once. A second hand-maintained table is precisely the shape that
    produced #601 and #650.

    Keyed on the tool NAME first, so ``mode='send'`` passed to ``Bash`` means
    nothing. FAIL CLOSED ON THE GRANT: a missing payload, a non-dict payload,
    an absent mode or an unlisted mode is not a report and gets no passage.
    Granting passage is NOT granting authority — every caller still meets the
    seat / XAACP route checks inside ``ai_msg_dispatch``, which fail closed on
    their own, and the middleware still writes the audit rows.
    """
    bare = _bare(tool_name)
    if not bare:
        return ""
    report_modes = _REPORT_MODE_TOOLS.get(bare)
    if not report_modes or not isinstance(tool_input, dict):
        return ""
    mode = str(tool_input.get("mode") or "").strip().lower()
    if mode in report_modes:
        return REASON_REPORT_MODE
    return ""


def operation_class_for(tool_name: str) -> OperationClass:
    """Resolve a tool's operation class. Default is NORMAL.

    Strips the standard MCP namespace prefixes so bare and namespaced
    forms collapse to the same class.
    """
    if not tool_name:
        return OperationClass.NORMAL
    norm = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if norm.startswith(prefix):
            norm = norm[len(prefix) :]
            break
    return _TOOL_OPERATION_CLASS.get(norm, OperationClass.NORMAL)


def bypasses_freeze_gate(tool_name: str) -> bool:
    """True if the tool is hardcoded as bypassing the freeze gate.

    Used by gate_tool.enforce_tool_call and ai_run_kill. The
    bypass is doctrine-bound, not caller-controlled.
    """
    cls = operation_class_for(tool_name)
    return cls in (
        OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
        OperationClass.DESTRUCTIVE_CLEANUP,
    )
