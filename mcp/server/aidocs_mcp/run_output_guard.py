"""Command output guard — scans ai_run stdout/stderr before it reaches the agent.

Pre-execution path gating (``command_read_intent``) catches obvious sensitive
reads. This is the complementary CONTENT defense (spec C): a credential that
leaks through an OTHERWISE-SAFE file's bytes, or through arbitrary command
output (``env``, ``printenv``, a script that echoes a token).

Unlike host Read (where the MCP server cannot replace what the HOST already
put in context), ``ai_run`` output is returned BY this MCP server — we own
the payload, so redaction here is REAL pre-context protection. The guard
reuses ``output_guard.scan_text`` (no second regex set) and is applied at
EVERY return path, including ``ai_run_output(raw_output=True)`` so the raw
view cannot bypass it.

Config:
  - ``security.tool_output_secret_policy`` (redact | report_only | allow_raw)
    decides whether we modify the text (shared with host output guard).
  - ``security.require_output_redaction_for_run`` (bool, default True):
    when the policy says redact but redaction cannot be applied to a payload
    that DID contain findings (text field missing / guard error), fail closed
    — replace the tail with a refusal marker. When False, mark the payload
    degraded and let the (still-findings-bearing) text through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Payload keys that carry returnable command output text.
# "output_tail" is the live tail of a RUNNING job (#484) — command
# output like the rest, scanned on both renderer and raw paths.
_TEXT_FIELDS: tuple[str, ...] = ("tail", "output", "stdout", "stderr", "output_tail")

_FAILSAFE_MARKER = (
    "[OUTPUT WITHHELD: secret detected and redaction could not be applied "
    "to this payload; security.require_output_redaction_for_run=true]"
)


def _record(hub: Any, root: Path, **kw: Any) -> None:
    try:
        hub.execution.record_event(root, **kw)
    except Exception:
        pass


def guard_run_output(
    payload: Any,
    *,
    hub: Any,
    project_root: Path,
    session_id: str | None = None,
    run_id: str = "",
    command: str = "",
) -> Any:
    """Scan/redact command output in ``payload`` in place; audit findings.

    ``payload`` is the dict returned by ``spawn_detached`` /
    ``tail_run_log``. The text-bearing fields (tail/output/stdout/stderr)
    are scanned. Returns the same (mutated) payload, or a fail-closed
    refusal payload when redaction is required but unavailable.
    """
    if not isinstance(payload, dict):
        return payload

    try:
        from .config import get_setting

        policy = (
            str(
                get_setting(
                    "security.tool_output_secret_policy",
                    project_root=project_root,
                    default="redact",
                )
                or "redact",
            )
            .strip()
            .lower()
        )
    except Exception:
        policy = "redact"

    if policy == "allow_raw":
        return payload

    try:
        require_redact = bool(
            get_setting(
                "security.require_output_redaction_for_run",
                project_root=project_root,
                default=True,
            ),
        )
    except Exception:
        require_redact = True

    from .output_guard import scan_text

    total_findings = 0
    total_redactions = 0
    categories: set[str] = set()
    redact = policy == "redact"
    redaction_failed = False

    for fld in _TEXT_FIELDS:
        text = payload.get(fld)
        if not isinstance(text, str) or not text:
            continue
        try:
            guard = scan_text(text, redact=redact)
        except Exception:
            # Guard itself failed on non-empty output → cannot certify.
            redaction_failed = True
            continue
        if guard.clean:
            continue
        total_findings += len(guard.findings)
        for f in guard.findings:
            categories.add(f.category)
        if redact:
            if guard.redaction_count and guard.redacted_text is not None:
                payload[fld] = guard.redacted_text
                total_redactions += guard.redaction_count
            elif guard.redaction_count == 0:
                # Findings that are not credential-redactable (injection /
                # info-level sensitive) — flagged, never silently dropped.
                pass

    if total_findings == 0 and not redaction_failed:
        return payload

    status = (
        "applied"
        if total_redactions
        else ("degraded" if (policy == "report_only" or not redact) else "flagged")
    )
    _record(
        hub,
        project_root,
        event_kind="run_output_guard_finding",
        source_kind="ai_run_output_guard",
        session_id=session_id or None,
        capability_name="ai_run",
        action_kind="redact" if total_redactions else "flag",
        target_entity=(run_id or command)[:200],
        status=status,
        payload={
            "run_id": run_id,
            "finding_count": total_findings,
            "redaction_count": total_redactions,
            "categories": sorted(categories),
            "policy": policy,
        },
    )

    # Fail-closed: policy wants redaction, findings exist, but we could not
    # redact (guard error). Withhold the offending text rather than leak it.
    if redact and require_redact and redaction_failed:
        # #371 (WAR U): the withheld stub is a refusal surface — carry the
        # file-it-as-FP affordance (additive; the withhold still holds).
        marker = _FAILSAFE_MARKER
        try:
            from .tool_gate_service import false_positive_affordance

            marker += "\n" + false_positive_affordance(
                "run_output_guard.failed_closed", project_root=project_root
            )
        except Exception:
            pass
        for fld in _TEXT_FIELDS:
            if isinstance(payload.get(fld), str) and payload[fld]:
                payload[fld] = marker
        payload["output_guard"] = "failed_closed"
        _record(
            hub,
            project_root,
            event_kind="run_output_guard_failed_closed",
            source_kind="ai_run_output_guard",
            session_id=session_id or None,
            capability_name="ai_run",
            action_kind="withhold",
            target_entity=(run_id or command)[:200],
            status="blocked",
            payload={"run_id": run_id, "policy": policy},
        )
        return payload

    if redaction_failed:
        # require_output_redaction_for_run=false (or report_only): let it
        # through but mark degraded so the bypass is visible in audit.
        payload["output_guard"] = "degraded"
    elif total_redactions:
        payload["output_guard"] = "redacted"
    else:
        payload["output_guard"] = "flagged"
    return payload
