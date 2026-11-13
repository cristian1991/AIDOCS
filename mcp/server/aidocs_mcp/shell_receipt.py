"""Batch 2.0-B0.1: native-execution completion RECEIPT + output proof.

PreToolUse only proves AIDOCS *allowed* native execution
(native_execution_allowed). It does NOT prove the command completed. This
module adds the PostToolUse half:

  * correlate a PostToolUse to a prior native_allow (command hash + provider
    + session), so only real native pilot runs get a completion receipt;
  * run the output guard over the result and record output bytes, truncation
    status, and redaction status — never raw command text or raw output;
  * record a native_completed receipt (separate from the PreToolUse allow);
  * REPLACE the output via CC PostToolUse updatedToolOutput (redacted +
    capped), proving output replacement applies to native pilot output;
  * if the output guard FAILS, withhold the output and emit a degraded
    security event — never mark the run cleanly guarded.

No raw command/output is stored in audit. ai_run remains canonical.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

# Cap native pilot output for model context + receipt accounting.
_MAX_OUTPUT_BYTES = 65536


def _cmd_hash(command: str) -> str:
    return hashlib.sha256((command or "").encode("utf-8")).hexdigest()

_WITHHELD_NOTICE = "[AIDOCS: native output guard failed; output withheld]"
_TRUNCATION_NOTICE = "\n[AIDOCS: native pilot output truncated]"


def _withheld_replacement(resp: object, notice: str) -> object:
    """SHAPE-PRESERVING withhold envelope body.

    Claude Code validates PostToolUse ``updatedToolOutput`` against the
    tool's own output schema — for Bash that is an OBJECT
    ({stdout, stderr, interrupted, ...}). Returning a bare string makes the
    host REJECT the replacement ("expected object, received string"),
    surface a "PostToolUse:Bash hook warning" on every governed call, and
    fall back to the ORIGINAL (unguarded) output. So: blank every text
    leaf, keep non-text leaves, and place the notice in the primary text
    field. Non-dict responses keep the plain-string behavior.
    """

    def _blank(node: object) -> object:
        if isinstance(node, str):
            return ""
        if isinstance(node, dict):
            return {k: _blank(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_blank(v) for v in node]
        return node

    if not isinstance(resp, dict):
        return notice
    new = {k: _blank(v) for k, v in resp.items()}
    if isinstance(new.get("stdout"), str):
        new["stdout"] = notice
    else:
        for k, v in new.items():
            if isinstance(v, str):
                new[k] = notice
                break
        else:
            new["stdout"] = notice
    return new


# Public name for the shape-preserving withhold (#401): the native receipt
# leg and the generic host-output leg must build byte-identical withholds,
# so there is ONE builder, not two. `_withheld_replacement` stays as the
# in-module name every existing caller already uses.
withheld_replacement = _withheld_replacement


def _cap_replacement(resp: object) -> tuple[object, bool]:
    """Cap the TOTAL text across all string leaves at ``_MAX_OUTPUT_BYTES``,
    preserving the response shape. Returns ``(capped, truncated)``."""
    remaining = _MAX_OUTPUT_BYTES
    truncated = False

    def _walk(node: object) -> object:
        nonlocal remaining, truncated
        if isinstance(node, str):
            enc = node.encode("utf-8")
            if len(enc) <= remaining:
                remaining -= len(enc)
                return node
            capped = enc[: max(remaining, 0)].decode("utf-8", errors="ignore")
            remaining = 0
            truncated = True
            return capped + _TRUNCATION_NOTICE
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(resp), truncated


def _join_text(resp: object) -> str:
    if isinstance(resp, str):
        return resp
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(resp)
    return "\n".join(parts)


def _has_native_allow(
    project_root: Path,
    command_hash: str,
    provider: str,
    session_id: str,
    host: str,
    tool_use_id: str,
) -> bool:
    """True iff a prior PreToolUse native_allow correlates to THIS call.

    Correlation is per-invocation: when a tool_use_id is available it is the
    REQUIRED key (binds an allow to its own completion — a later same-text
    command has a different tool_use_id and cannot correlate to a stale
    allow). When the host exposes no tool_use_id, fall back to
    command_hash + provider + host (+ session). No raw command text.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        events = ExecutionIndexStore().list_events(
            project_root,
            query=command_hash,
            limit=50,
        )
    except Exception:
        return False
    for e in events:
        if e.get("event_kind") != "shell_policy_enforced":
            continue
        if e.get("status") != "native_allow":
            continue
        p = e.get("payload") or {}
        if p.get("command_hash") != command_hash:
            continue
        if provider and p.get("provider") and p.get("provider") != provider:
            continue
        if host and p.get("host") and p.get("host") != host:
            continue
        ev_sid = str(e.get("session_id") or "")
        if session_id and ev_sid and ev_sid != session_id:
            continue
        # Per-invocation binding. If THIS call has a tool_use_id, the allow
        # MUST carry the same one — no stale/cross-run correlation.
        if tool_use_id:
            if str(p.get("tool_use_id") or "") != tool_use_id:
                continue
        return True
    return False


def _record(
    project_root: Path,
    *,
    event_kind: str,
    status: str,
    session_id: str,
    capability_name: str,
    payload: dict[str, Any],
) -> None:
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind=event_kind,
            source_kind="shell_receipt.native_2b0",
            session_id=session_id or None,
            capability_name=capability_name,
            action_kind="receipt",
            target_entity="shell_policy",
            status=status,
            payload=payload,
        )
    except Exception:
        pass


def native_post_receipt(
    project_root: Path,
    runtime: Any,
    *,
    host: str,
    tool_name: str,
    tool_input: Any,
    tool_response: Any,
    host_session_id: str,
    tool_use_id: str = "",
) -> dict[str, Any] | None:
    """Return a CC PostToolUse updatedToolOutput envelope for a correlated
    native run (output redacted + capped), or None when this is NOT a
    correlated native completion (caller continues its normal flow).

    Renamed 2026-06-05 from ``native_pilot_post_receipt`` — native execution
    is the normal governed shell surface now, not a pilot. The old name
    remains as a thin alias below for back-compat.
    """
    try:
        from .shell_envelope import (
            TRANSPORT_HOST_NATIVE,
            detect_provider_and_transport,
        )

        provider, transport = detect_provider_and_transport(tool_name)
    except Exception:
        return None
    if transport != TRANSPORT_HOST_NATIVE:
        return None
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "")
    if not command:
        return None
    chash = _cmd_hash(command)
    if not _has_native_allow(
        project_root,
        chash,
        provider,
        host_session_id,
        host,
        tool_use_id,
    ):
        # No correlated allow → not a native pilot completion. No receipt,
        # no false completion.
        return None

    text = _join_text(tool_response)
    output_bytes = len(text.encode("utf-8"))
    guard_status = "clean"
    redaction_count = 0
    categories: list[str] = []
    # SHAPE-PRESERVING replacement (2026-07-11): Claude Code validates
    # updatedToolOutput against the tool's output schema — Bash's is an
    # object, so the old flattened-string replacement was rejected on EVERY
    # governed native run ("PostToolUse:Bash hook warning") and the host
    # fell back to the ORIGINAL, unguarded output. redact_tool_response
    # keeps the response shape (str stays str, dict stays dict).
    replaced: object = tool_response

    try:
        from .output_guard import redact_tool_response

        replaced, redaction_count, categories = redact_tool_response(
            tool_response,
            redact=True,
        )
        redaction_count = int(redaction_count)
        categories = sorted(categories)
        guard_status = "redacted" if redaction_count else "clean"
    except Exception as exc:
        # Fail closed: cannot guard → withhold the raw output, mark degraded.
        # #372/#371 (WAR U): this is the ONLY withhold case (guard unavailable
        # — unknown != clean); say so loudly and carry the file-it-as-FP
        # affordance so a wrong withholding is reportable inline.
        guard_status = "degraded"
        notice = _WITHHELD_NOTICE + " — guard unavailable, unknown != clean"
        try:
            from .tool_gate_service import false_positive_affordance

            notice += "\n" + false_positive_affordance(
                "shell_receipt.native_output_withheld",
                project_root=project_root,
            )
        except Exception:
            pass
        replaced = _withheld_replacement(tool_response, notice)
        _record(
            project_root,
            event_kind="shell_native_output_guard_degraded",
            status="degraded",
            session_id=host_session_id,
            capability_name=tool_name,
            payload={
                "host": host,
                "provider": provider,
                "command_hash": chash,
                "error_type": type(exc).__name__,
            },
        )

    replaced, truncated = _cap_replacement(replaced)

    _record(
        project_root,
        event_kind="shell_policy_native_completed",
        status=guard_status,
        session_id=host_session_id,
        capability_name=tool_name,
        payload={
            "host": host,
            "provider": provider,
            "tool_use_id": tool_use_id,
            "command_hash": chash,
            "output_bytes": output_bytes,
            "truncated": truncated,
            "guard_status": guard_status,
            "redaction_count": redaction_count,
            "redaction_categories": categories,
            "native_completed": True,
            "output_replacement_applied": True,
        },
    )

    # Replace output before model context (proves redaction/cap applies).
    # The replacement MATCHES the original tool_response shape so the host
    # accepts it (no per-call hook warning, no silent fallback to raw output).
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replaced,
        },
    }


# Back-compat alias (renamed 2026-06-05). Callers/tests referencing the old
# pilot name continue to work; new code should use native_post_receipt.
native_pilot_post_receipt = native_post_receipt
