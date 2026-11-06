"""The ONE classifier of a finished tool's response (r0b on 45b872974/82a6a61a4).

Consumed by BOTH LifecycleService (PostToolUse audit status) and
agent_orchestrator.record_edit_outcome (test-retry unlock), so the audit row and
the unlock decision can never disagree about whether a call failed.

classify_tool_response(resp, tool_name) -> "failed" | "succeeded" | "unknown"
  failed    - any failure marker anywhere it is looked for: ok:false,
              success:false, isError/is_error, error, refusal/refused, a failure
              status, meta/_meta.aidocs_is_error, a text line leading with
              ✗/❌/error/failed/..., or the pretty footer 'tool ✗'. Failure wins
              over every success proof.
  succeeded - POSITIVE proof only: structured ok:true (top level, inside an MCP
              wrapper key, or a JSON text block), the edit_result renderer's own
              success ack ('✓', '✓ 3') / pretty footer ('tool ✓ · ...'), or - for
              raw host edit tools only - the host result dict naming the file.
  unknown   - nothing proven either way (fail closed for unlock purposes).

is_dry_run(resp) -> True when ANY dry-run evidence is present (dry_run:true at
any walked level, the '✓ dry-run' ack, or the pretty '📝 dry-run' /
'📝 dry-run str_replace' / '📝 batch dry-run' headers). A dry run mutates
nothing, so it never unlocks.
"""

from __future__ import annotations

import json
import re
from typing import Literal

Verdict = Literal["failed", "succeeded", "unknown"]

_FAILURE_STATUSES = frozenset({"refused", "failed", "failure", "blocked", "error", "denied"})
# MCP result wrapper keys a host may nest the tool's structured payload under.
_WRAPPER_KEYS = ("structuredContent", "structured_content", "result", "toolResult")
_META_KEYS = ("meta", "_meta")
_FAILURE_LEADS = ("✗", "❌", "error", "failed", "failure", "refused", "blocked", "denied", "tool ✗")
# EXACT renderer success forms only (tool_display.edit_result / _pad_user_blocks):
# the non-pretty ack '✓' | '✓ dry-run' | '✓ <count>' (optionally carrying the
# '[name 12ms in=.. out=..]' debug marker), and the pretty footer
# 'tool ✓' | 'tool ✓ · `name` · 12ms'. A free-form line starting with '✓ ' is
# not proof.
_SUCCESS_ACK = re.compile(r"^✓(?: dry-run| \d+)?(?: \[[^\]\n]*\])?$")
_SUCCESS_FOOTER = re.compile(r"^tool ✓(?: · \S.*)?$")
# Explicit zero-delta markers an engine/tool may report. An extra guard only:
# the authority for mutation is the pre/post checksum witness in
# agent_orchestrator (edit_mutation_witnessed).
_ZERO_DELTA_TEXT = re.compile(r"^(?:no changes?\b|nothing to change\b|unchanged\b|no-op\b)", re.IGNORECASE)
_DRY_RUN_TEXT = re.compile(r"^(?:✓ dry-run\b|📝 (?:batch )?dry-run\b)", re.IGNORECASE)
# Raw host edit tools return a host dict naming the file, never ok:true.
RAW_HOST_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_RAW_HOST_EDIT_PATH_KEYS = ("filePath", "file_path", "notebook_path")
_MAX_DEPTH = 6


def _bare(tool_name: object) -> str:
    name = str(tool_name or "").strip()
    if name.lower().startswith("mcp__") and "__" in name[5:]:
        return name.rsplit("__", 1)[-1]
    return name


class _Facts:
    __slots__ = ("failed", "proven", "dry_run", "zero_delta")

    def __init__(self) -> None:
        self.failed = False
        self.proven = False
        self.dry_run = False
        self.zero_delta = False


def _walk_text(text: str, facts: _Facts, depth: int) -> None:
    stripped = text.strip()
    if not stripped:
        return
    if stripped[0] in "{[":
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            _walk(parsed, facts, depth + 1)
            return
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    for line in lines:
        low = line.lower()
        if low.startswith(_FAILURE_LEADS):
            facts.failed = True
        if _DRY_RUN_TEXT.match(line):
            facts.dry_run = True
        if _ZERO_DELTA_TEXT.match(line):
            facts.zero_delta = True
        if _SUCCESS_FOOTER.match(line):
            facts.proven = True
    if lines and _SUCCESS_ACK.match(lines[0]):
        facts.proven = True


def _walk_dict(d: dict, facts: _Facts, depth: int) -> None:
    if d.get("is_error") or d.get("isError") or d.get("error") or d.get("refusal"):
        facts.failed = True
    if d.get("refused") is True or d.get("ok") is False or d.get("success") is False:
        facts.failed = True
    if str(d.get("status") or "").strip().lower() in _FAILURE_STATUSES:
        facts.failed = True
    if d.get("dry_run") is True or d.get("dryRun") is True:
        facts.dry_run = True
    if d.get("ok") is True:
        facts.proven = True
    for zk in ("changed", "replacements", "bytes_changed", "applied_count"):
        if zk in d and not isinstance(d[zk], str) and d[zk] is not None and not d[zk]:
            facts.zero_delta = True
    if d.get("no_op") is True or d.get("noop") is True or d.get("unchanged") is True:
        facts.zero_delta = True
    for mk in _META_KEYS:
        meta = d.get(mk)
        if isinstance(meta, dict):
            if meta.get("aidocs_is_error") or meta.get("isError"):
                facts.failed = True
    for wk in _WRAPPER_KEYS:
        inner = d.get(wk)
        if isinstance(inner, (dict, str)):
            _walk(inner, facts, depth + 1)
    content = d.get("content")
    if isinstance(content, list):
        _walk(content, facts, depth + 1)
    text = d.get("text")
    if isinstance(text, str):
        _walk_text(text, facts, depth + 1)


def _walk(obj: object, facts: _Facts, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(obj, str):
        _walk_text(obj, facts, depth)
    elif isinstance(obj, dict):
        _walk_dict(obj, facts, depth)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (str, dict)):
                _walk(item, facts, depth + 1)
            else:
                t = getattr(item, "text", None)
                if isinstance(t, str):
                    _walk_text(t, facts, depth + 1)


def _facts(resp: object) -> _Facts:
    facts = _Facts()
    _walk(resp, facts)
    return facts


def classify_tool_response(resp: object, tool_name: str = "") -> Verdict:
    facts = _facts(resp)
    if facts.failed:
        return "failed"
    if facts.proven:
        return "succeeded"
    if _bare(tool_name) in RAW_HOST_EDIT_TOOLS and isinstance(resp, dict):
        if any(isinstance(resp.get(k), str) and resp[k].strip() for k in _RAW_HOST_EDIT_PATH_KEYS):
            return "succeeded"
    return "unknown"


def is_dry_run(resp: object) -> bool:
    return _facts(resp).dry_run


def is_zero_delta(resp: object) -> bool:
    """An explicit no-change report (changed/replacements == 0, no_op, 'no changes')."""
    return _facts(resp).zero_delta


def edit_unlocks(resp: object, tool_name: str = "") -> bool:
    """RESPONSE-side precondition for an unlock: a proven success that is neither
    a dry run nor an explicit zero-delta. NOT sufficient on its own - the unlock
    also needs the pre/post checksum mutation witness (record_edit_outcome)."""
    facts = _facts(resp)
    return (
        classify_tool_response(resp, tool_name) == "succeeded"
        and not facts.dry_run
        and not facts.zero_delta
    )
