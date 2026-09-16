"""THE DECLARED GATE CLASS of a tool (#650).

The universal task gate (2026-05-17) fires on EVERY tool call and refuses
anything without an open task. Exemptions lived — and only could live — in
``mcp_server_runtime_helpers._TASK_GATE_EXEMPT``, a hand-maintained set of
literal tool NAMES. Twice now that shape has produced the same defect:

  * #601 — ``ai_issues`` documented itself as "deliberately requires NO active
    task: the refusal-report channel for callers the gate just refused", and
    the gate refused it anyway, because nobody added the name.
  * #650/#640 — every READ tool and ``ai_msg`` were gated. Six lanes plus a
    conductor share one session; one actor's ``task_complete`` clears the
    session's single task slot, and the next ``ai_find`` from any other actor
    was refused. Four lanes ended up frozen AND mute: blocked, and unable to
    report the block, because the reporting channel was gated by the very
    thing it reports.

Adding names to a frozenset cures one artefact at a time and ships the next
read tool gated again. So the class is DECLARED here, once, and the gate
CONSULTS it:

    tool_gate_class(name) -> "read" | "report" | "gated"

MEMBERSHIP RULE (first match wins):

  1. A declared ``tool_interface`` ToolSpec is the authority. ``cls == "read"``
     and ``tier in {"R", "L"}`` → READ. Any OTHER declared spec → GATED.
     Declared metadata always wins, so a Tier-M edit, a Tier-A admin tool, or
     a Tier-R *selector* (``project_select`` — Tier R, but it BINDS a project)
     can never fall through into the read class on tier alone.
  2. A declared REPORTING SURFACE (``_REPORT_SURFACES``) → REPORT. This is
     #601's principle stated as a class instead of three name-adds: recording
     and reporting are never gated. These tools carry their OWN authority
     checks (seat binding, XAACP route binding, and — for their task-owned
     writes — ``require_active_task_strict`` in-handler), so ungating the
     lifecycle check grants no capability that a task-holder did not have.
  3. The audit action taxonomy's ``read`` bucket
     (``tool_gate_service.TOOL_ACTION_BUCKETS``) → READ. That taxonomy already
     exists, already has ONE home, and is already the vocabulary every host
     adapter records ``action_kind`` with. A read tool declares itself read
     THERE; the gate does not keep a second copy of the list.
  4. Everything else, and any classification failure → GATED.

Rule 4 is the load-bearing half: **FAIL CLOSED ON THE GRANT.** An unknown,
new, or unclassifiable name is never granted read status — the class can only
free a tool that has DECLARED itself read somewhere auditable.

WHY READS BELONG WITH REPORTS. The universal gate's stated purpose is
ATTRIBUTION, not protection: "an agent could read or spawn shell processes
without an active task, and any work it did via those channels was
unattributable." That argument is sound for shell and for writes, where the
unattributed thing is a CHANGE. A read changes nothing; the cost of refusing
it is that an agent which has lost its task slot cannot even look at the code
to find out why. Reads are still recorded — ``tool_call_started`` /
``tool_call_completed`` rows are written by the middleware regardless of this
class, so the audit trail is not thinned by one row.

WHAT THIS MODULE MUST NEVER TOUCH:
  * ``shell_egress_lifecycle_preflight`` — strict, name-exemption ONLY. A
    shell command is not a read, and its validator deliberately fails CLOSED.
    This class is not consulted there.
  * ``require_active_task_strict`` — the #601 self-gating entry point. The
    class is honored only on the universal (``honor_name_exemption=True``)
    path, so ``ai_backlog(mode='add')`` and ``ai_task`` writes keep refusing
    without a task even though their tools are class REPORT.
  * mutating tools of any kind, and every operator gate.

Pinned by ``mcp/tests/security/test_read_gate_class_650.py``.
"""

from __future__ import annotations

GATE_CLASS_READ = "read"
GATE_CLASS_REPORT = "report"
GATE_CLASS_GATED = "gated"

# ToolSpec tiers that are read-only in the WebMCP manifest vocabulary:
# R = read, L = selector/list. (M = surgical edit, A = admin.)
_READ_TIERS = frozenset({"R", "L"})

# THE REPORTING CLASS (#601, generalized). A caller the gate just refused must
# be able to say so. Membership rule: a tool whose PURPOSE is to record or
# transmit a statement about work — not to perform work. Each one enforces its
# own authority independently of the task lifecycle:
#   ai_issues  — write-once refusal reports; already name-exempt pre-#650.
#   ai_backlog — filing surface; its WRITES self-gate via the strict gate.
#   ai_msg     — seat + XAACP messaging; role/route binding is its authority.
#   ai_gate_msg — the gate-side twin of ai_msg.
# A tool does NOT join this class because it is convenient; it joins because
# refusing it would leave a blocked caller with no way to report the block.
_REPORT_SURFACES = frozenset(
    {
        "ai_issues",
        "ai_backlog",
        "ai_msg",
        "ai_gate_msg",
    },
)

_HOST_PREFIXES = ("mcp__aidocs__", "mcp__playwright__", "mcp__")


def normalize_tool_name(tool_name: object) -> str:
    """Strip host prefixes and casefold — same normalization the audit
    classifier and the intent-audit tier resolver use, so all three agree on
    what ``mcp__aidocs__ai_find`` is called."""
    name = str(tool_name or "").strip().lower()
    for prefix in _HOST_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def tool_gate_class(tool_name: object) -> str:
    """Resolve the declared gate class of ``tool_name``.

    Returns ``"read"``, ``"report"`` or ``"gated"``. Never raises: any lookup
    failure resolves ``"gated"`` (fail closed on the grant).
    """
    bare = normalize_tool_name(tool_name)
    if not bare:
        return GATE_CLASS_GATED

    # 1. Declared ToolSpec wins outright, in both directions.
    try:
        from .tool_interface import _TOOLS

        spec = _TOOLS.get(bare)
        if spec is not None:
            declared_cls = str(getattr(spec, "cls", "") or "").strip().lower()
            declared_tier = str(getattr(spec, "tier", "") or "").strip().upper()
            if declared_cls == GATE_CLASS_READ and declared_tier in _READ_TIERS:
                return GATE_CLASS_READ
            if bare in _REPORT_SURFACES:
                return GATE_CLASS_REPORT
            return GATE_CLASS_GATED
    except Exception:
        return GATE_CLASS_GATED

    # 2. Declared reporting surface.
    if bare in _REPORT_SURFACES:
        return GATE_CLASS_REPORT

    # 3. The audit taxonomy's read bucket — the existing ONE HOME for
    #    "this tool is a read".
    try:
        from .tool_gate_service import classify_tool_action

        if classify_tool_action(bare) == GATE_CLASS_READ:
            return GATE_CLASS_READ
    except Exception:
        return GATE_CLASS_GATED

    # 4. Undeclared → gated.
    return GATE_CLASS_GATED


def tool_is_task_gate_free(tool_name: object) -> bool:
    """True when the UNIVERSAL task gate must let ``tool_name`` through with
    no open task, because it is a declared read or a declared reporting
    surface. Consulted ONLY by ``require_active_task`` on its
    ``honor_name_exemption=True`` path — never by the shell-egress preflight,
    never by ``require_active_task_strict``."""
    return tool_gate_class(tool_name) in (GATE_CLASS_READ, GATE_CLASS_REPORT)
