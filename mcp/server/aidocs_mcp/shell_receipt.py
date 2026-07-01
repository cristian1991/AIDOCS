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
    out_text = text

    try:
        from .output_guard import scan_text

        gr = scan_text(text, redact=True)
        if gr.redacted_text is not None:
            out_text = gr.redacted_text
        redaction_count = int(gr.redaction_count)
        categories = sorted({f.category for f in gr.findings})
        guard_status = "redacted" if redaction_count else "clean"
    except Exception as exc:
        # Fail closed: cannot guard → withhold the raw output, mark degraded.
        guard_status = "degraded"
        out_text = "[AIDOCS: native output guard failed; output withheld]"
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

    truncated = False
    enc = out_text.encode("utf-8")
    if len(enc) > _MAX_OUTPUT_BYTES:
        out_text = (
            enc[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
            + "\n[AIDOCS: native pilot output truncated]"
        )
        truncated = True

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
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": out_text,
        },
    }


# Back-compat alias (renamed 2026-06-05). Callers/tests referencing the old
# pilot name continue to work; new code should use native_post_receipt.
native_pilot_post_receipt = native_post_receipt
