"""Dual-channel tool output helpers.

Builds ToolResult objects where TextContent blocks render as rich
human-visible output (unified diffs, status headers, summaries) while
structured_content stays terse for the agent.  Based on Claude Code
behavior documented in anthropics/claude-code#9962: the harness shows
structured_content as JSON to the agent and hides TextContent, while
the host UI renders TextContent blocks line-by-line.

Net effect: users see pretty diffs, agents see {"ok": true}. No agent
context tax for visual polish.

Debug mode: when AIDOCS_DEBUG_TOOL_OUTPUT=1 or tool_output.debug=true
(gated to dev_mode projects — i.e. AIDOCS source itself), the user-
facing TextContent is REPLACED with a pretty-printed view of the
structured_content the agent sees. Lets operators dogfood what the
agent actually receives without changing the on-wire response.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent


def _inline_highlight(old_line: str, new_line: str) -> tuple[str, str]:
    """Return (old, new) with changed char spans wrapped in ** markers.

    Uses SequenceMatcher to find matching blocks between the two lines;
    non-matching spans get wrapped so markdown renderers bold them and
    the user's eye lands on the actual delta instead of scanning the
    whole line. Falls back to raw lines when the diff is too noisy
    (e.g. unrelated lines) — threshold: 50% unchanged char ratio.
    """
    if not old_line or not new_line:
        return old_line, new_line
    # Skip highlight for long lines — wrap markers become visual noise.
    if len(old_line) > 200 or len(new_line) > 200:
        return old_line, new_line
    sm = difflib.SequenceMatcher(a=old_line, b=new_line, autojunk=False)
    ratio = sm.ratio()
    if ratio < 0.5:
        return old_line, new_line

    old_parts: list[str] = []
    new_parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            old_parts.append(old_line[i1:i2])
            new_parts.append(new_line[j1:j2])
        elif tag == "replace":
            old_parts.append(f"**{old_line[i1:i2]}**")
            new_parts.append(f"**{new_line[j1:j2]}**")
        elif tag == "delete":
            old_parts.append(f"**{old_line[i1:i2]}**")
        elif tag == "insert":
            new_parts.append(f"**{new_line[j1:j2]}**")
    return "".join(old_parts), "".join(new_parts)


def _pair_lines_for_inline_diff(
    old_lines: list[str],
    new_lines: list[str],
) -> list[tuple[str, str, str]]:
    """Return list of (marker, old_or_empty, new_or_empty) triples.

    marker is '-' for lines present only in old, '+' for only in new,
    '~' for a pair where both sides match (apply inline highlight).
    SequenceMatcher on line sequences groups changes into replace
    blocks; replace blocks of equal length are paired 1:1 so the inline
    highlight sees aligned pairs.
    """
    triples: list[tuple[str, str, str]] = []
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # No diff to render; skip (diff view only shows changes).
            continue
        if tag == "replace":
            old_slice = old_lines[i1:i2]
            new_slice = new_lines[j1:j2]
            # Pair 1:1 when same length so inline highlight lands.
            if len(old_slice) == len(new_slice):
                for o, n in zip(old_slice, new_slice):
                    triples.append(("~", o, n))
            else:
                for o in old_slice:
                    triples.append(("-", o, ""))
                for n in new_slice:
                    triples.append(("+", "", n))
        elif tag == "delete":
            for o in old_lines[i1:i2]:
                triples.append(("-", o, ""))
        elif tag == "insert":
            for n in new_lines[j1:j2]:
                triples.append(("+", "", n))
    return triples


def _tc(text: str, audience: list[str] | None = None) -> TextContent:
    """Build a TextContent line block with an optional audience annotation.

    MCP spec (2025-06-18) annotations.audience controls visibility:
      - ["user"]: client renders to user, does NOT feed to LLM
      - ["assistant"]: fed to LLM, not shown to user
      - ["user", "assistant"] or None: both (legacy default)
    Claude Code and other spec-compliant clients respect this split.
    """
    if audience is None:
        return TextContent(type="text", text=text)
    try:
        from mcp.types import Annotations  # type: ignore

        ann = Annotations(audience=audience)
        return TextContent(type="text", text=text, annotations=ann)
    except Exception:
        # Fallback: older mcp package without Annotations — pass dict.
        try:
            return TextContent(
                type="text",
                text=text,
                annotations={"audience": audience},
            )
        except Exception:
            return TextContent(type="text", text=text)


def _tc_user(text: str) -> TextContent:
    """TextContent flagged for user-only display (hidden from the LLM)."""
    return _tc(text, audience=["user"])


def _tc_agent(text: str) -> TextContent:
    """TextContent flagged for the LLM only (not shown to user)."""
    return _tc(text, audience=["assistant"])


def _attach_marker(raw: Any, marker: str) -> Any:
    """Attach a debug marker string to a raw tool payload.

    - dict: injects `_debug` key with the marker
    - list: wraps in {items: [...], _debug: marker}
    - ToolResult: pass-through
    - scalar: wraps in {value, _debug}
    """
    if isinstance(raw, ToolResult) or not marker:
        return raw
    if isinstance(raw, dict):
        if "_debug" in raw:
            return raw
        out = dict(raw)
        out["_debug"] = marker
        return out
    if isinstance(raw, list):
        return {"items": raw, "_debug": marker}
    return {"value": raw, "_debug": marker}


def _debug_marker(
    tool_name: str = "",
    duration_ms: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    project_root: Path | None = None,
) -> str:
    """Build the inline debug marker, honoring per-field checkboxes.

    Returns "" when no marker flags are set, else a bracketed string
    like "[ai_find 42ms in=120 out=840]". Each field is independently
    toggleable via tool_output.show_tool_name / show_duration / show_tokens.
    """
    if project_root is None:
        try:
            from .mcp_server_runtime_helpers import resolve_project_root

            project_root = resolve_project_root()
        except Exception:
            return ""
    try:
        from .config import get_setting

        show_name = bool(
            get_setting("tool_output.show_tool_name", project_root=project_root, default=False),
        )
        show_dur = bool(
            get_setting("tool_output.show_duration", project_root=project_root, default=False),
        )
        show_tok = bool(
            get_setting("tool_output.show_tokens", project_root=project_root, default=False),
        )
    except Exception:
        return ""
    if not (show_name or show_dur or show_tok):
        return ""
    bits: list[str] = []
    if show_name and tool_name:
        bits.append(tool_name)
    if show_dur and duration_ms is not None:
        bits.append(f"{duration_ms:.0f}ms")
    if show_tok:
        if tokens_in is not None:
            bits.append(f"in={tokens_in}")
        if tokens_out is not None:
            bits.append(f"out={tokens_out}")
    return f"[{' '.join(bits)}]" if bits else ""


_tiktoken_enc: Any = None


def _get_tiktoken() -> Any:
    """Lazy-load tiktoken's cl100k_base encoder.

    cl100k_base is what OpenAI uses for gpt-4/gpt-3.5-turbo and tracks
    Claude's tokenizer closely enough (within ~10%) for debug-marker
    purposes. Cached globally after first load. Returns None if
    tiktoken isn't installed — caller falls back to byte heuristic.
    """
    global _tiktoken_enc
    if _tiktoken_enc is not None:
        return _tiktoken_enc
    try:
        import tiktoken as _tk

        _tiktoken_enc = _tk.get_encoding("cl100k_base")
        return _tiktoken_enc
    except Exception:
        _tiktoken_enc = False  # sentinel: don't keep retrying
        return None


def _estimate_tokens(raw: Any) -> int:
    """Count tokens in a tool payload.

    Uses tiktoken (cl100k_base) when available for accurate counts;
    falls back to len(bytes)//4 heuristic when tiktoken isn't
    installed. Debug-marker only — not used for billing.
    """
    try:
        import json as _json

        if isinstance(raw, (dict, list)):
            text = _json.dumps(raw, default=str, ensure_ascii=False)
        else:
            text = str(raw)
    except Exception:
        return 0
    enc = _get_tiktoken()
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _pretty_mode_active(project_root: Path | None = None) -> bool:
    """Single toggle for user-facing rendering.

    When ON: tools emit dual-channel output — pretty user blocks
    (headers, diffs, footer) + terse agent ack. Default OFF: the
    tool's raw payload is returned as-is, so agent and user see the
    same normal content. Operators flip this in the dashboard.
    """
    if project_root is None:
        try:
            from .mcp_server_runtime_helpers import resolve_project_root

            project_root = resolve_project_root()
        except Exception:
            return False
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "tool_output.pretty",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


# ── FastMCP isError monkey-patch ────────────────────────────────────
#
# FastMCP's ToolResult.to_mcp_result() builds a CallToolResult but does
# NOT plumb isError, so tools returning ToolResult always render a
# green chip in Claude Code — even when the operation failed. We want
# rich content blocks (diff, failure rows) AND the red chip.
#
# Fix: override to_mcp_result. The chip goes RED when EITHER:
#   (a) meta carries aidocs_is_error=True (explicit signal, e.g.
#       edit_result on ok:false), OR
#   (b) the structured payload itself is refusal/failure-shaped —
#       ok is False, a truthy blocked_by / refused_reason / refused,
#       or a top-level error without an explicit ok:true.
#
# Why (b) (Empire bubble-truth re-seal 2026-05-30): the overwhelming
# majority of refusals — gate denials, path-trust blocks, judge
# strikes, bash_policy refusals — return a plain dict like
# {"error": ..., "blocked_by": ...} or {"ok": false, ...}. None of
# them set meta.aidocs_is_error, so every refused agent tool surfaced
# a GREEN chip with a ✗/❌ body inside — a verdict/UI mismatch that
# made "blocked" look like "succeeded". Detecting the failure shape
# at this single serialization chokepoint flips the chip red for
# every tool with no per-tool changes.
#
# Reversible: stash the original, delegate to it for success cases.

# Keys whose presence/value marks a tool payload as a failure/refusal.
_FAILURE_BLOCKED_KEYS = ("blocked_by", "refused_reason")


def _structured_is_failure(structured: Any) -> bool:
    """True when a tool's structured payload is refusal/failure-shaped.

    Conservative: an explicit ok:true always wins (some success
    payloads legitimately carry an `error: null` or a nested
    `blocked_by` echo). We only flag the unambiguous failure shapes.
    """
    if not isinstance(structured, dict):
        return False
    ok = structured.get("ok")
    if ok is True:
        return False
    if ok is False:
        return True
    # No explicit ok — look for refusal markers.
    for key in _FAILURE_BLOCKED_KEYS:
        if structured.get(key):
            return True
    if structured.get("refused") is True:
        return True
    # Top-level truthy error with no ok:true above → failure.
    if structured.get("error"):
        return True
    return False


def _install_is_error_patch() -> None:
    try:
        from fastmcp.tools.tool import ToolResult as _TR

        if getattr(_TR, "_aidocs_patched", False):
            return
        from mcp.types import CallToolResult as _CTR

        _orig = _TR.to_mcp_result

        def _patched(self):
            res = _orig(self)
            meta = getattr(self, "meta", None)
            is_error = isinstance(meta, dict) and meta.get("aidocs_is_error")
            if not is_error:
                # Inspect the structured payload for a failure shape.
                # ToolResult stashes it as .structured_content; the
                # emitted CallToolResult exposes .structuredContent.
                structured = getattr(self, "structured_content", None)
                if structured is None and isinstance(res, _CTR):
                    structured = getattr(res, "structuredContent", None)
                if structured is None and isinstance(res, tuple) and len(res) == 2:
                    structured = res[1]
                is_error = _structured_is_failure(structured)
            if not is_error:
                return res
            # Normalize to a CallToolResult so we can set isError.
            if isinstance(res, _CTR):
                res.isError = True
                return res
            if isinstance(res, tuple) and len(res) == 2:
                content, structured = res
                return _CTR(
                    content=content,
                    structuredContent=structured,
                    isError=True,
                )
            # content-only list
            return _CTR(content=res, isError=True)

        _TR.to_mcp_result = _patched  # type: ignore[method-assign]
        _TR._aidocs_patched = True  # type: ignore[attr-defined]
    except Exception:
        # Fail-open: if the patch fails, nothing changes — tools still
        # work, just without the red chip on failure.
        pass


_install_is_error_patch()

# Minimum TextContent block count for user-facing renders. Claude Code
# desktop and OpenCode desktop render ≥4 blocks correctly (pop-out
# button enabled, no truncation). Below that, hosts collapse the
# response or hide the pop-out. Confirmed via sanity test 2026-04-24.
_USER_MIN_BLOCKS = 4


# Thread-local-ish context set by the @renders_as decorator so the
# pad-footer can stamp tool name + duration without threading those
# through every dispatcher. Reset after each call.
import contextvars as _cv

_current_tool_name: _cv.ContextVar[str] = _cv.ContextVar("aidocs_current_tool_name", default="")
_current_tool_duration_ms: _cv.ContextVar[float | None] = _cv.ContextVar(
    "aidocs_current_tool_duration_ms",
    default=None,
)


def _pad_user_blocks(
    blocks: list[TextContent],
    *,
    success: bool = True,
) -> list[TextContent]:
    """Append the tool-call debug footer per host line-count rules.

    CC Desktop + OC Desktop (confirmed 2026-04-24 via 4-line sanity test):
      1 line content  → debug on next line, no gap. Total = 2.
      >=2 line content → 1 blank, then debug. Nbsp-pad to _USER_MIN_BLOCKS.

    Debug is a single-line summary: 'tool ✓ · `name` · 42ms'. No
    separator bar — total line count stays predictable so the pop-out
    button stays enabled without inflating agent context.

    Padding respects the last block's audience so it doesn't leak into
    agent context.
    """
    padding_audience: list[str] | None = ["user"]
    last = blocks[-1] if blocks else None
    if last is not None:
        try:
            ann = getattr(last, "annotations", None)
            if ann is not None:
                padding_audience = list(getattr(ann, "audience", None) or ["user"])
        except Exception:
            pass
    content_count = len(blocks)
    padded = list(blocks)
    tool_name = _current_tool_name.get()
    duration_ms = _current_tool_duration_ms.get()
    mark = "✓" if success else "✗"
    bits = [f"tool {mark}"]
    if tool_name:
        bits.append(f"`{tool_name}`")
    if duration_ms is not None:
        if duration_ms >= 1:
            bits.append(f"{duration_ms:.0f}ms")
        else:
            bits.append(f"{duration_ms * 1000:.0f}µs")
    debug_line = " · ".join(bits)
    # 1-line content: debug immediately, no blank gap.
    if content_count <= 1:
        padded.append(_tc(debug_line, audience=padding_audience))
        return padded
    # >=2-line content: one blank line, then debug.
    padded.append(_tc("", audience=padding_audience))
    padded.append(_tc(debug_line, audience=padding_audience))
    # Top-up padding only if still below host floor (CC/OC Desktop: 4).
    while len(padded) < _USER_MIN_BLOCKS:
        padded.append(_tc("\u00a0", audience=padding_audience))
    return padded


def _render_paired_diff(old_lines: list[str], new_lines: list[str], cap: int = 40) -> list[str]:
    """Render a unified-ish diff using line-pairing + inline highlight.

    Output lines are prefixed with '-', '+' (pure add/remove) or '~'
    (replace-pair where inline **bold** markers point to the changed
    chars inside the line). Caller wraps the whole block in ```diff```
    fences so markdown-aware hosts color red/green automatically.
    """
    triples = _pair_lines_for_inline_diff(old_lines, new_lines)
    out: list[str] = []
    emitted = 0
    for marker, o, n in triples:
        if emitted >= cap:
            out.append(f"… {len(triples) - emitted} more diff lines")
            break
        if marker == "~":
            o_hi, n_hi = _inline_highlight(o, n)
            out.append(f"- {o_hi}")
            out.append(f"+ {n_hi}")
            emitted += 2
        elif marker == "-":
            out.append(f"- {o}")
            emitted += 1
        elif marker == "+":
            out.append(f"+ {n}")
            emitted += 1
    return out


def render_edit_diff(
    *,
    path: str,
    start_line: int,
    end_line: int,
    old_content: str,
    new_content: str,
    lines_removed: int,
    lines_added: int,
    dry_run: bool = False,
    header_note: str = "",
) -> list[TextContent]:
    """Build TextContent blocks showing a unified-style edit diff.

    Emits a markdown ```diff``` fenced block so hosts with markdown
    rendering (Claude Code, OpenCode) color - red / + green. Line
    pairs where both sides match structurally get inline **bold**
    markers around the actually-changed chars so tiny edits (e.g.
    dsd_ → dd_) point the user at the delta, not the whole line.
    """
    blocks: list[TextContent] = []
    tag = "dry-run" if dry_run else "edit"
    range_str = f"L{start_line}" if start_line == end_line else f"L{start_line}-L{end_line}"
    header = f"📝 {tag}: `{path}` {range_str} · -{lines_removed} +{lines_added}"
    if header_note:
        header = f"{header} — {header_note}"
    blocks.append(_tc(header))

    old_lines = old_content.splitlines() if old_content else []
    new_lines = new_content.splitlines() if new_content else []

    # Markdown-fenced diff for color; one block so the fence holds.
    diff_lines = _render_paired_diff(old_lines, new_lines)
    if diff_lines:
        fenced = ["```diff", *diff_lines, "```"]
        blocks.append(_tc("\n".join(fenced)))
    return blocks


def render_str_replace_diff(
    *,
    path: str,
    old_str: str,
    new_str: str,
    match_count: int = 1,
    dry_run: bool = False,
) -> list[TextContent]:
    """Diff for ai_str_replace — paired + inline-highlighted, fenced
    as ```diff``` for color. Small single-line edits (dsd_ → dd_) get
    inline **bold** on just the changed characters.
    """
    blocks: list[TextContent] = []
    tag = "dry-run str_replace" if dry_run else "str_replace"
    note = f" ×{match_count}" if match_count != 1 else ""
    header = f"📝 {tag}: `{path}`{note}"
    blocks.append(_tc(header))

    old_lines = (old_str or "").splitlines()
    new_lines = (new_str or "").splitlines()
    diff_lines = _render_paired_diff(old_lines, new_lines)
    if diff_lines:
        fenced = ["```diff", *diff_lines, "```"]
        blocks.append(_tc("\n".join(fenced)))
    return blocks


def render_create_file(*, path: str, line_count: int, byte_count: int) -> list[TextContent]:
    return [_tc(f"✨ created: {path} ({line_count} lines, {byte_count} bytes)")]


def render_insert_lines(
    *,
    path: str,
    start_line: int,
    inserted_content: str,
    lines_added: int,
) -> list[TextContent]:
    blocks: list[TextContent] = [_tc(f"➕ insert: {path} @ L{start_line}")]
    for line in (inserted_content or "").splitlines()[:30]:
        blocks.append(_tc(f"+ {line}"))
    inserted_lines = len((inserted_content or "").splitlines())
    if inserted_lines > 30:
        blocks.append(_tc(f"… {inserted_lines - 30} more +lines"))
    blocks.append(_tc(f"✓ +{lines_added}"))
    return blocks


def render_batch_edit_summary(
    *,
    edits: list[dict[str, Any]],
    total_removed: int,
    total_added: int,
    dry_run: bool = False,
) -> list[TextContent]:
    """One-line-per-edit summary for ai_batch_edit."""
    blocks: list[TextContent] = []
    tag = "batch dry-run" if dry_run else "batch edit"
    blocks.append(_tc(f"📝 {tag}: {len(edits)} edits"))
    for edit in edits[:20]:
        path = edit.get("path", "?")
        sl = edit.get("start_line", "?")
        el = edit.get("end_line", sl)
        removed = edit.get("lines_removed", 0)
        added = edit.get("lines_added", 0)
        rng = f"L{sl}" if sl == el else f"L{sl}-L{el}"
        blocks.append(_tc(f"  {path} {rng}  -{removed} +{added}"))
    if len(edits) > 20:
        blocks.append(_tc(f"  … {len(edits) - 20} more edits"))
    blocks.append(_tc(f"✓ total: -{total_removed} +{total_added}"))
    return blocks


def _lean_structured(structured: Any) -> Any:
    """Project a structured payload down to its lean, agent-facing fields.

    Under Claude Code (and any host that forwards structuredContent to the
    model — the agent receives structuredContent, NOT the terse content ack),
    a fat structured payload leaks into the agent's context and defeats the
    dual-audience token saving. In pretty (dual-audience) mode we therefore
    strip the large content-bearing values (diffs, file bodies, hunks, match
    lists) that the operator already sees in the pretty content blocks, keeping
    only the small ack/scalar fields the agent needs to chain on (ok, count,
    path, error, ids). Big values are elided to a short marker so the agent
    still knows the field existed. Only applied in pretty mode — the default
    (pretty OFF) path keeps the full payload, so existing consumers are intact.
    """
    if not isinstance(structured, dict):
        return structured
    import json as _json

    lean: dict[str, Any] = {}
    for k, v in structured.items():
        if v is None or isinstance(v, (bool, int, float)):
            lean[k] = v
        elif isinstance(v, str):
            lean[k] = v if len(v) <= 160 else f"<{len(v)} chars elided>"
        elif isinstance(v, list):
            try:
                size = len(_json.dumps(v, default=str, ensure_ascii=False))
            except Exception:
                size = len(str(v))
            # TYPE-PRESERVING (#61, Empire 2026-06-20): a big list elides to [] — NOT a
            # string marker. A string marker changed the field's TYPE (array->string),
            # which violates the tool's declared outputSchema and hard-errors the call
            # under pretty mode. The operator still sees the full list in the pretty
            # content blocks; the agent gets a schema-valid empty list.
            lean[k] = v if size <= 160 else []
        elif isinstance(v, dict):
            try:
                size = len(_json.dumps(v, default=str, ensure_ascii=False))
            except Exception:
                size = len(str(v))
            lean[k] = v if size <= 160 else {}  # type-preserving (was a string marker)
        else:
            lean[k] = v  # tuples / other JSON-foreign types: leave untouched
    return lean


# Bounds for the non-pretty failure digest: enough rows to name the
# failing items of a typical batch, never enough to flood the agent.
_FAILURE_DIGEST_MAX_LINES = 8
_FAILURE_DIGEST_MAX_LINE_CHARS = 160


def _failure_block_digest(
    content_blocks: list[TextContent] | None,
    ack_line: str,
) -> str:
    """Bounded digest of failure rows carried by content blocks.

    On non-pretty hosts edit_result emits only the ack line, so any
    per-item failure detail that lives ONLY in content_blocks (e.g.
    batch per-file failure rows) would be lost — the agent sees "✗
    failed: 2 of 3 edits failed" with no idea which edits or why.
    This extracts a bounded digest of those rows for the ack path:

      - at most _FAILURE_DIGEST_MAX_LINES rows, each truncated to
        _FAILURE_DIGEST_MAX_LINE_CHARS chars;
      - elision is explicit ("… +N more");
      - rows already contained in the ack line (or duplicates) are
        skipped — they add no signal.

    Returns "" when there is nothing beyond what the ack already says.
    """
    if not content_blocks:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for block in content_blocks:
        for raw in str(getattr(block, "text", "") or "").splitlines():
            line = raw.strip()
            if not line or line in seen or line in ack_line:
                continue
            seen.add(line)
            if len(line) > _FAILURE_DIGEST_MAX_LINE_CHARS:
                line = line[: _FAILURE_DIGEST_MAX_LINE_CHARS - 1] + "…"
            lines.append(line)
    if not lines:
        return ""
    shown = lines[:_FAILURE_DIGEST_MAX_LINES]
    elided = len(lines) - len(shown)
    if elided > 0:
        shown.append(f"… +{elided} more")
    return "\n".join(shown)


def edit_result(
    *,
    content_blocks: list[TextContent],
    structured: dict[str, Any] | None = None,
    project_root: Path | None = None,
    tool_name: str = "",
    started_at: float | None = None,
) -> ToolResult:
    """Package edit output with display_mode-driven channel routing.

    Mode resolution (config tool_output.display_mode):
      - dual (default): pretty content audience=['user'], ack audience=
        ['assistant']. On audience-aware hosts → clean split. On hosts
        that ignore audience → both are shown (status quo).
      - agent_only: emit ONLY the terse ack. Saves context on CLI
        where stdout is shared and duplication doesn't help anyone.
      - pretty_only: emit ONLY the user content (no separate ack).
        Dashboards/operators that aren't running an LLM.

    Debug mode (tool_output.debug + dev_mode) wins over all modes and
    shows the agent-facing view in the user panel for dogfooding.
    """
    # Stamp contextvars so the pad footer shows tool name + duration
    # even when edit tools bypass the @renders_as decorator. If
    # started_at isn't supplied we leave duration blank — that's honest:
    # we can't fabricate a timing we didn't measure.
    import time as _time

    name_token = None
    dur_token = None
    if tool_name:
        name_token = _current_tool_name.set(tool_name)
    if started_at is not None:
        dur_token = _current_tool_duration_ms.set((_time.perf_counter() - started_at) * 1000)

    # Synthesize agent ack line from structured payload — becomes the
    # agent-audience block, user sees the pad footer instead (the
    # footer already says "tool ✓ · name · Xms").
    success = True
    ack_line: str | None = None
    if structured:
        if structured.get("ok") is True:
            # Bare "✓" for plain success — 1 token in o200k. Count /
            # dry-run modifiers only fire when the agent actually
            # benefits from knowing (e.g. "applied 3/4" after batch).
            if structured.get("dry_run"):
                ack_line = "✓ dry-run"
            elif "count" in structured:
                ack_line = f"✓ {structured['count']}"
            else:
                ack_line = "✓"
        elif structured.get("ok") is False:
            success = False
            ack_line = f"✗ failed: {structured.get('error') or 'unknown'}"

    # ── #448 Consumer C rail: one-shot blast-radius note ──────────────
    # _post_edit_reindex_and_grant stashes the edited file's reverse-
    # dependency radius; drain it into THIS edit response so the acting
    # agent is handed its blast radius (Emperor 2026-07-18) on the same
    # rail it already reads. Best-effort — never blocks the response.
    try:
        from .semantic_enrichment import take_pending_radius_note

        _radius_note = take_pending_radius_note()
    except Exception:
        _radius_note = None
    if _radius_note is not None and success:
        _radius_summary = str(_radius_note.get("summary") or "")
        if isinstance(structured, dict):
            structured["blast_radius"] = {
                "target": _radius_note.get("target"),
                "dependent_count": _radius_note.get("dependent_count"),
                "dependents": _radius_note.get("dependents"),
                "truncated": _radius_note.get("truncated"),
            }
            # #375 Phase 3 (B): anchored memories for the touched leaf
            # ride the same one-shot note when present.
            if _radius_note.get("anchored_memories"):
                structured["blast_radius"]["anchored_memories"] = _radius_note.get(
                    "anchored_memories"
                )
        if _radius_summary and ack_line:
            ack_line = f"{ack_line}\n⚠ {_radius_summary}"

    import time as _t

    ms = (
        (_t.perf_counter() - started_at) * 1000
        if started_at is not None
        else (_current_tool_duration_ms.get() or 0.0)
    )

    # Audit: stamp every edit with tool + duration + token counts so
    # the audit chain has metrics even when the tool bypasses the
    # @renders_as decorator. Best-effort — never block on audit failure.
    tokens_in = (
        sum(_estimate_tokens(getattr(b, "text", "")) for b in (content_blocks or []))
        if content_blocks
        else 0
    )
    tokens_out = _estimate_tokens(structured) if structured else 0
    if tool_name:
        try:
            from .execution_index_store import ExecutionIndexStore
            from .mcp_server_runtime_helpers import resolve_project_root

            root = project_root if project_root is not None else resolve_project_root()
            ExecutionIndexStore().record_event(
                root,
                event_kind="tool_call",
                source_kind="mcp",
                action_kind=tool_name,
                status="ok" if success else "error",
                payload={
                    "tool": tool_name,
                    "duration_ms": round(ms, 2),
                    "render_kind": "edit",
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            )
        except Exception:
            pass

    try:
        if not _pretty_mode_active(project_root):
            # Pretty OFF: raw ack; per-field debug marker when any
            # show_* flag is on.
            line = ack_line or "{}"
            if not success:
                # Failure legibility: content blocks may carry per-item
                # failure rows (batch edits, multi-file ops) that would
                # otherwise be collapsed to the bare ack on non-pretty
                # hosts. Append a bounded digest so the agent learns
                # WHICH items failed, not just how many.
                digest = _failure_block_digest(content_blocks, line)
                if digest:
                    line = f"{line}\n{digest}"
            marker = _debug_marker(
                tool_name=tool_name,
                duration_ms=ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                project_root=project_root,
            )
            if marker:
                line = f"{line} {marker}"
            return ToolResult(
                content=[_tc(line)],
                structured_content=structured,
            )

        # Pretty ON: dual-channel with pretty content + footer.
        # Agent ack rides along in meta.isError for red-chip signaling
        # on failure; footer already covers success signaling.
        user_blocks = [_tc_user(getattr(b, "text", "")) for b in content_blocks]
        user_blocks = _pad_user_blocks(user_blocks, success=success)
        meta = {"aidocs_is_error": True} if not success else None
        return ToolResult(
            content=user_blocks,
            structured_content=_lean_structured(structured),
            meta=meta,
        )
    finally:
        if name_token is not None:
            _current_tool_name.reset(name_token)
        if dur_token is not None:
            _current_tool_duration_ms.reset(dur_token)


def render_code_find(result: Any, *, query: str = "", mode: str = "symbols") -> list[TextContent]:
    """Render ai_find/ai_investigate-style results as markdown.

    Accepts the raw dict or list returned by the index layer. Builds a
    user-friendly list: `🔍 header` + per-match rows grouped by file,
    each as `path:line · symbol (kind)`. Graceful fallback on shapes
    it doesn't recognize: dumps a compact JSON preview.
    """
    blocks: list[TextContent] = []

    # Normalize to list of match dicts.
    matches: list[dict[str, Any]] = []
    if isinstance(result, list):
        matches = [m for m in result if isinstance(m, dict)]
    elif isinstance(result, dict):
        for key in ("matches", "results", "result", "symbols", "items"):
            v = result.get(key)
            if isinstance(v, list):
                matches = [m for m in v if isinstance(m, dict)]
                break

    if not matches:
        blocks.append(_tc(f"🔍 `{query}` · no matches"))
        if isinstance(result, dict) and result.get("hint"):
            blocks.append(_tc(f"💡 {result['hint']}"))
        return blocks

    # Header.
    mode_label = mode or "find"
    blocks.append(_tc(f"🔍 `{query}` · {mode_label} · {len(matches)} matches"))

    # Group by file.
    by_file: dict[str, list[dict[str, Any]]] = {}
    for m in matches:
        path = str(m.get("path") or m.get("file") or "?")
        by_file.setdefault(path, []).append(m)

    rows: list[str] = []
    shown_files = 0
    for path, items in by_file.items():
        if shown_files >= 30:
            rows.append(f"… {len(by_file) - shown_files} more files")
            break
        rows.append(f"**{path}**")
        for m in items[:10]:
            ln = m.get("line_number") or m.get("line") or ""
            sym = m.get("symbol") or m.get("name") or ""
            kind = m.get("kind") or ""
            bits = []
            if ln:
                bits.append(f"L{ln}")
            if sym:
                bits.append(f"`{sym}`")
            if kind:
                bits.append(f"({kind})")
            reason = m.get("why")
            if isinstance(reason, list) and reason:
                bits.append(f"— {reason[0]}")
            rows.append("  · " + " ".join(bits))
        if len(items) > 10:
            rows.append(f"  … {len(items) - 10} more in this file")
        shown_files += 1
    blocks.append(_tc("\n".join(rows)))
    return blocks


def render_text_search(result: Any, *, query: str = "") -> list[TextContent]:
    """Render ai_text_search as grep -n style output.

    Shape:
        <N> matches in <K> files for `<query>`:
        path/to/file.py
          12: matched line content
          47: another matched line
        path/to/other.py
          3: matched

    Drops emoji, drops markdown bold, drops the per-file `(N matches)`
    annotation (line numbers reveal density). ~60% token cut vs the
    previous styled form.
    """
    blocks: list[TextContent] = []
    results: list[dict[str, Any]] = []
    if isinstance(result, dict):
        raw = result.get("results") or []
        results = [r for r in raw if isinstance(r, dict)]
    if not results:
        blocks.append(_tc(f"no matches for `{query}`"))
        return blocks
    occurrences = sum(int(r.get("match_count") or 0) for r in results)
    rows: list[str] = [f"{occurrences} matches in {len(results)} files for `{query}`:"]
    shown = 0
    for r in results:
        if shown >= 30:
            rows.append(f"  … {len(results) - shown} more files")
            break
        path = r.get("path") or "?"
        rows.append(str(path))
        lines = r.get("lines") or []
        for line in lines[:5]:
            if isinstance(line, dict):
                ln = line.get("line_number") or ""
                text = str(line.get("line") or "").rstrip()
                rows.append(f"  {ln}: {text}" if ln else f"  : {text}")
        if len(lines) > 5:
            rows.append(f"  … {len(lines) - 5} more")
        shown += 1
    blocks.append(_tc("\n".join(rows)))
    return blocks


def render_bundle(result: Any, *, target: str = "") -> list[TextContent]:
    """Render ai_bundle results — file header + outline."""
    blocks: list[TextContent] = []
    if not isinstance(result, dict):
        return [_tc(f"📦 `{target}` · unexpected result shape")]
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    path = inner.get("path") or target or "?"
    line_count = inner.get("line_count") or inner.get("total") or ""
    role = inner.get("role") or ""
    bits = [f"📦 `{path}`"]
    if line_count:
        bits.append(f"{line_count} lines")
    if role and role != "unknown":
        bits.append(role)
    blocks.append(_tc(" · ".join(bits)))

    summary = inner.get("summary")
    if summary:
        first = str(summary).split("|")[0].strip().strip('"')
        if first:
            blocks.append(_tc(first[:200]))

    outline = inner.get("outline") or []
    if isinstance(outline, list) and outline:
        rows = ["outline:"]
        for s in outline[:40]:
            if isinstance(s, dict):
                sym = s.get("symbol") or "?"
                kind = s.get("kind") or ""
                ln = s.get("line_number") or ""
                rows.append(f"  · L{ln} `{sym}` ({kind})")
        if len(outline) > 40:
            rows.append(f"  … {len(outline) - 40} more symbols")
        blocks.append(_tc("\n".join(rows)))
    # #481 (War KK): opt-in inline content (ai_bundle include_content=N).
    inline = inner.get("inline_content")
    if isinstance(inline, str) and inline:
        blocks.append(_tc(inline))
        label = inner.get("inline_content_budget")
        if isinstance(label, str) and label:
            blocks.append(_tc(f"— {label}"))
    return blocks


def render_trace(result: Any, *, query: str = "", mode: str = "") -> list[TextContent]:
    """Render ai_trace results. Shape varies by mode — field_flow,
    references, service, etc. — but matches are always a list of
    dicts with path/line/symbol or path/line/role.
    """
    blocks: list[TextContent] = []
    matches: list[dict[str, Any]] = []
    if isinstance(result, list):
        matches = [m for m in result if isinstance(m, dict)]
    elif isinstance(result, dict):
        for key in ("matches", "results", "flow", "callers", "edges", "nodes"):
            v = result.get(key)
            if isinstance(v, list):
                matches = [m for m in v if isinstance(m, dict)]
                break
    if not matches:
        blocks.append(_tc(f"🔗 `{query}` · trace/{mode} · no matches"))
        return blocks
    blocks.append(_tc(f"🔗 `{query}` · trace/{mode} · {len(matches)} hits"))
    rows: list[str] = []
    shown = 0
    for m in matches[:40]:
        path = m.get("path") or "?"
        ln = m.get("line_number") or m.get("line") or ""
        sym = m.get("symbol") or m.get("name") or ""
        role = m.get("role") or m.get("kind") or ""
        bits = [f"`{path}`"]
        if ln:
            bits.append(f"L{ln}")
        if sym:
            bits.append(f"`{sym}`")
        if role:
            bits.append(f"({role})")
        rows.append("  · " + " ".join(bits))
        shown += 1
    if len(matches) > shown:
        rows.append(f"  … {len(matches) - shown} more")
    blocks.append(_tc("\n".join(rows)))
    return blocks


def render_symbol_snippet(result: Any, *, path: str = "", symbol: str = "") -> list[TextContent]:
    """Render ai_get_symbol_snippet — grep-style header + source.

    Format: `<path>:<line>  <symbol> (<kind>)`  followed by the body.
    Drops `language` (implied by extension), drops emoji, drops the
    triple-backtick fence (the body already reads as code). One header
    line + the body. ~50% token cut vs the previous emoji+fence form.
    """
    blocks: list[TextContent] = []
    if not isinstance(result, dict) or not result:
        blocks.append(_tc(f"{path}: no snippet for `{symbol}`"))
        return blocks
    header_sym = result.get("symbol") or symbol
    kind = result.get("kind") or ""
    ln = result.get("line_number") or result.get("line") or ""
    src_path = result.get("path") or path
    location = f"{src_path}:{ln}" if ln else str(src_path)
    suffix = f" {header_sym}" + (f" ({kind})" if kind else "")
    blocks.append(_tc(f"{location}{suffix}"))
    content = result.get("content") or result.get("snippet") or ""
    if content:
        blocks.append(_tc(content.rstrip()))
    return blocks


def render_schema(result: Any, *, query: str = "", mode: str = "") -> list[TextContent]:
    """Render ai_schema — entity/field results as bullet rows."""
    blocks: list[TextContent] = []
    if isinstance(result, list):
        items = [m for m in result if isinstance(m, dict)]
    elif isinstance(result, dict):
        items = []
        for key in ("entities", "fields", "matches", "results"):
            v = result.get(key)
            if isinstance(v, list):
                items = [m for m in v if isinstance(m, dict)]
                break
    else:
        items = []
    if not items:
        blocks.append(_tc(f"🗂️ `{query}` · schema/{mode} · no matches"))
        return blocks
    blocks.append(_tc(f"🗂️ `{query}` · schema/{mode} · {len(items)} results"))
    rows: list[str] = []
    for e in items[:40]:
        name = e.get("name") or e.get("entity") or e.get("field") or "?"
        path = e.get("path") or ""
        kind = e.get("kind") or e.get("type") or ""
        bits = [f"`{name}`"]
        if kind:
            bits.append(f"({kind})")
        if path:
            bits.append(f"— {path}")
        rows.append("  · " + " ".join(bits))
    if len(items) > 40:
        rows.append(f"  … {len(items) - 40} more")
    blocks.append(_tc("\n".join(rows)))
    return blocks


def render_investigate(result: Any, *, concept: str = "") -> list[TextContent]:
    """Render ai_investigate summary."""
    blocks: list[TextContent] = []
    if not isinstance(result, dict):
        return [_tc(f"🧭 `{concept}` · unexpected result shape")]
    findings = result.get("findings") or []
    summary = result.get("summary") or ""
    blocks.append(_tc(f"🧭 investigate: `{concept}`"))
    if summary:
        blocks.append(_tc(summary))
    rows: list[str] = []
    for f in findings[:10]:
        if not isinstance(f, dict):
            continue
        area = f.get("area") or "?"
        count = f.get("count") or 0
        rows.append(f"**{area}** ({count})")
        for t in (f.get("top") or [])[:5]:
            if isinstance(t, dict):
                sym = t.get("symbol") or t.get("name") or ""
                kind = t.get("kind") or ""
                path = t.get("path") or ""
                rows.append(f"  · `{sym}` ({kind}) — {path}")
    if rows:
        blocks.append(_tc("\n".join(rows)))
    next_tools = result.get("next_tools") or []
    if next_tools:
        hints = ["→ next:"]
        for n in next_tools[:3]:
            if isinstance(n, dict):
                hints.append(f"  · `{n.get('tool', '?')}` — {n.get('why', '')}")
        blocks.append(_tc("\n".join(hints)))
    return blocks


def _terse_agent_ack(raw: Any) -> str:
    """Build a tiny ack line for the agent from a read-tool payload.

    Agents don't need the full match list — they need enough to know
    the call landed and, for investigation follow-ups, the top 1-3
    path:line refs. Everything else is bloat. Returns a single-line
    JSON that costs <200 tokens no matter the payload size.
    """
    if raw is None:
        return "{}"
    # Collapse a list of matches.
    if isinstance(raw, list):
        items = raw
        ack: dict[str, Any] = {"count": len(items)}
        top: list[str] = []
        for m in items[:3]:
            if isinstance(m, dict):
                p = m.get("path") or ""
                ln = m.get("line_number") or m.get("line") or ""
                sym = m.get("symbol") or m.get("name") or ""
                ref = p + (f":{ln}" if ln else "")
                if sym:
                    ref = f"{ref} {sym}"
                if ref.strip():
                    top.append(ref)
        if top:
            ack["top"] = top
        return json.dumps(ack, default=str, ensure_ascii=False)
    if isinstance(raw, dict):
        # Counts by common key names.
        ack: dict[str, Any] = {}
        for k in ("total_matches", "finding_count", "total", "count"):
            if k in raw:
                ack[k] = raw[k]
        # Short hints / summary / error keep for the agent.
        for k in ("summary", "hint", "error"):
            v = raw.get(k)
            if isinstance(v, str) and v:
                ack[k] = v[:200]
        # Collapse any list-of-dicts child into count + top refs.
        for list_key in ("results", "matches", "findings", "outline", "entities"):
            v = raw.get(list_key)
            if isinstance(v, list) and v:
                ack[f"{list_key}_count"] = len(v)
                # For text_search-shaped results, sum intra-file match_count
                # so the agent sees actual occurrence totals, not file counts.
                occ = sum(
                    int(m.get("match_count") or 0)
                    for m in v
                    if isinstance(m, dict) and "match_count" in m
                )
                if occ:
                    ack["occurrences"] = occ
                top: list[str] = []
                for m in v[:3]:
                    if isinstance(m, dict):
                        p = m.get("path") or ""
                        ln = m.get("line_number") or m.get("line") or ""
                        sym = m.get("symbol") or m.get("name") or m.get("area") or ""
                        ref = p + (f":{ln}" if ln else "")
                        if sym:
                            ref = f"{ref} {sym}" if ref else sym
                        if ref.strip():
                            top.append(ref)
                if top:
                    ack[f"{list_key}_top"] = top
                break
        # #481 (War KK): opt-in inline content survives the terse ack —
        # it is already size-capped + budget-labeled at the tool layer,
        # and hiding it here would recreate the unreadable-artifact hole.
        if isinstance(raw.get("inline_content"), str) and raw["inline_content"]:
            ack["inline_content"] = raw["inline_content"]
            if isinstance(raw.get("inline_content_budget"), str):
                ack["inline_content_budget"] = raw["inline_content_budget"]
        # Artifact pointer — agent can fetch full payload if needed.
        art = raw.get("artifact") or (
            raw.get("structuredContent", {}).get("artifact")
            if isinstance(raw.get("structuredContent"), dict)
            else None
        )
        if isinstance(art, dict):
            ak = art.get("artifact_id") or art.get("artifact_path")
            if ak:
                ack["artifact"] = ak
        # Preserve reserved freshness keys (_index*/_memory*/_schema*) stamped onto
        # the raw payload (renders_as stamp) so the agent's terse ack still carries
        # the honest staleness signal.
        try:
            from .index_staleness import reserved_freshness_keys

            ack.update(reserved_freshness_keys(raw))
        except Exception:
            pass
        if not ack:
            # Fallback — keep the dict keys so the agent sees its shape.
            ack = {"keys": list(raw.keys())[:10]}
        return json.dumps(ack, default=str, ensure_ascii=False)
    return json.dumps({"value": str(raw)[:200]}, ensure_ascii=False)


def read_result(
    *,
    blocks: list[TextContent],
    project_root: Path | None = None,
    agent_payload: Any = None,
) -> ToolResult:
    """Read-tool result with display_mode-driven routing.

    dual: user content audience=['user'] + terse ack audience=['assistant'].
    agent_only: just the terse ack.
    pretty_only: just the user content.
    debug: swap user view with the agent-facing structured payload.
    """
    if not _pretty_mode_active(project_root):
        # Pretty OFF: pass through the rendered blocks as-is (they're
        # the tool's normal output) with no footer, no agent tag.
        return ToolResult(content=list(blocks))

    # Pretty ON: render with footer + agent-audience ack.
    user_blocks = [_tc_user(getattr(b, "text", "")) for b in blocks]
    user_blocks = _pad_user_blocks(user_blocks)
    final_blocks: list[TextContent] = list(user_blocks)
    if agent_payload is not None:
        final_blocks.append(_tc_agent(_terse_agent_ack(agent_payload)))
        # Dict payloads carry freshness via the ack (reserved-key preservation).
        # List/other payloads have no key slot, so surface the freshness for the
        # current index-backed tool as a separate agent-audience block.
        if not isinstance(agent_payload, dict):
            try:
                from .index_staleness import freshness_keys_for_tool

                root = project_root
                if root is None:
                    from .mcp_server_runtime_helpers import resolve_project_root

                    root = resolve_project_root()
                extra = freshness_keys_for_tool(_current_tool_name.get(), root)
                if extra:
                    final_blocks.append(_tc_agent(json.dumps(extra, default=str, ensure_ascii=False)))
            except Exception:
                pass
    return ToolResult(content=final_blocks)


def wrap_code_find(
    raw: Any,
    *,
    query: str = "",
    mode: str = "symbols",
    project_root: Path | None = None,
) -> ToolResult:
    blocks = render_code_find(raw, query=query, mode=mode)
    return read_result(blocks=blocks, project_root=project_root, agent_payload=raw)


def wrap_text_search(
    raw: Any,
    *,
    query: str = "",
    project_root: Path | None = None,
) -> ToolResult:
    blocks = render_text_search(raw, query=query)
    return read_result(blocks=blocks, project_root=project_root, agent_payload=raw)


def wrap_bundle(
    raw: Any,
    *,
    target: str = "",
    project_root: Path | None = None,
) -> ToolResult:
    blocks = render_bundle(raw, target=target)
    return read_result(blocks=blocks, project_root=project_root, agent_payload=raw)


def wrap_investigate(
    raw: Any,
    *,
    concept: str = "",
    project_root: Path | None = None,
) -> ToolResult:
    blocks = render_investigate(raw, concept=concept)
    return read_result(blocks=blocks, project_root=project_root, agent_payload=raw)


# ── Decorator dispatch ──────────────────────────────────────────────
#
# @renders_as("kind") wraps a tool function so its raw return value is
# routed through the matching renderer. Pulls args like query, mode,
# target, concept from kwargs of the decorated call so the renderer
# has header context.

_RENDER_KIND_DISPATCH: dict[str, Any] = {}


def _dispatch_find(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    return wrap_code_find(
        raw,
        query=str(call_kwargs.get("query", "")),
        mode=str(call_kwargs.get("mode", "symbols")),
    )


def _dispatch_investigate(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    return wrap_investigate(raw, concept=str(call_kwargs.get("concept", "")))


def _dispatch_text_search(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    return wrap_text_search(raw, query=str(call_kwargs.get("query", "")))


def _dispatch_bundle(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    return wrap_bundle(raw, target=str(call_kwargs.get("target", "")))


def _dispatch_trace(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    blocks = render_trace(
        raw,
        query=str(call_kwargs.get("query", "")),
        mode=str(call_kwargs.get("mode", "")),
    )
    return read_result(blocks=blocks, agent_payload=raw)


def _dispatch_snippet(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    blocks = render_symbol_snippet(
        raw,
        path=str(call_kwargs.get("path", "")),
        symbol=str(call_kwargs.get("symbol", "")),
    )
    return read_result(blocks=blocks, agent_payload=raw)


def render_list(
    result: Any,
    *,
    title: str = "list",
    label_keys: tuple[str, ...] = (),
) -> list[TextContent]:
    """Generic renderer for list-of-dict tool outputs.

    Builds a markdown header + bullet rows, auto-picking display fields
    from common keys (name, title, path, session_id, kind, id, status,
    timestamp). Graceful on list-under-a-key dicts.
    """
    blocks: list[TextContent] = []
    items: list[Any] = []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        for key in (
            "items",
            "results",
            "entries",
            "sessions",
            "tasks",
            "events",
            "breadcrumbs",
            "roles",
            "users",
            "escalations",
            "files",
            "messages",
            "questions",
            "docs",
            "candidates",
            "permissions",
            "records",
            "rows",
        ):
            v = result.get(key)
            if isinstance(v, list):
                items = v
                break
        if not items:
            # Dict with no list — fall back to status-style dict rendering.
            return render_status(result, title=title)
    if not items:
        blocks.append(_tc(f"📋 {title} · (empty)"))
        return blocks
    blocks.append(_tc(f"📋 {title} · {len(items)} items"))

    # Decide display keys.
    display_keys: tuple[str, ...] = label_keys or (
        "name",
        "title",
        "session_id",
        "task_id",
        "id",
        "path",
        "kind",
        "status",
        "state",
        "event_kind",
        "action_kind",
        "timestamp",
        "at",
        "when",
        "summary",
    )

    rows: list[str] = []
    for it in items[:50]:
        if isinstance(it, dict):
            pieces: list[str] = []
            for k in display_keys:
                if k in it and it[k] not in (None, "", [], {}):
                    val = str(it[k])
                    if k in ("timestamp", "at", "when"):
                        val = val[:19]
                    if len(val) > 100:
                        val = val[:97] + "…"
                    pieces.append(
                        f"`{val}`" if k in ("path", "session_id", "task_id", "id") else val,
                    )
                    if len(pieces) >= 4:
                        break
            rows.append("  · " + " · ".join(pieces) if pieces else f"  · {str(it)[:100]}")
        else:
            rows.append(f"  · {str(it)[:100]}")
    if len(items) > 50:
        rows.append(f"  … {len(items) - 50} more")
    blocks.append(_tc("\n".join(rows)))
    return blocks


def render_status(result: Any, *, title: str = "status") -> list[TextContent]:
    """Renderer for single-dict status payloads (index status, health,
    breakers, freshness, etc.). Emits a header + key/value rows.
    """
    blocks: list[TextContent] = []
    if not isinstance(result, dict):
        blocks.append(_tc(f"ℹ️ {title}"))
        blocks.append(_tc(f"  value: {str(result)[:200]}"))
        return blocks
    ok = result.get("ok")
    success = result.get("success")
    icon = "ℹ️"
    if ok is True or success is True:
        icon = "✅"
    elif ok is False or success is False:
        icon = "❌"
    blocks.append(_tc(f"{icon} {title}"))

    rows: list[str] = []
    skip = {"structuredContent", "artifact"}
    for k, v in result.items():
        if k in skip:
            continue
        if isinstance(v, (dict, list)):
            if isinstance(v, list):
                rows.append(f"  · **{k}**: {len(v)} items")
            else:
                inner_keys = ", ".join(list(v.keys())[:5])
                rows.append(f"  · **{k}**: `{{{inner_keys}}}`")
        else:
            val = str(v)
            if len(val) > 120:
                val = val[:117] + "…"
            rows.append(f"  · **{k}**: {val}")
        if len(rows) >= 25:
            rows.append(f"  … {len(result) - 25} more fields")
            break
    if rows:
        blocks.append(_tc("\n".join(rows)))
    return blocks


def _dispatch_list(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    # Best-effort title from first kwarg or a sentinel in call_kwargs.
    title = call_kwargs.get("_render_title") or "list"
    blocks = render_list(raw, title=str(title))
    return read_result(blocks=blocks, agent_payload=raw)


def _dispatch_status(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    title = call_kwargs.get("_render_title") or "status"
    blocks = render_status(raw, title=str(title))
    return read_result(blocks=blocks, agent_payload=raw)


def _dispatch_schema(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    blocks = render_schema(
        raw,
        query=str(call_kwargs.get("query", "")),
        mode=str(call_kwargs.get("mode", "")),
    )
    return read_result(blocks=blocks, agent_payload=raw)


def _dispatch_run_output(raw: Any, call_kwargs: dict[str, Any]) -> ToolResult:
    """Render ai_run_output / ai_run_status responses.

    Classifies by the original `command` field (stamped by spawn) and
    routes to the test / build / probe renderer so failed test names,
    tracebacks, and build errors are SURFACED, not buried in a
    bullet-list of every dict key. Header line shows state + exit +
    duration + log_bytes; body is the rendered output.

    Drops noise: `ok`, `run_id` echo (agent already has both from the
    call). Keeps signal: state, exit code, tail content classified.
    (Diagnosed 2026-04-20: status renderer truncated test output and
    buried failed test names in `· **tail**: ...`.)
    """
    blocks: list[TextContent] = []
    if not isinstance(raw, dict):
        return ToolResult(content=[_tc(str(raw))])

    # In-flight refusal short-circuit (backlog #16, 2026-04-25).
    # ai_run_output's contract refuses reads while the run is still
    # executing; the structured refusal carries blocked_by=
    # "run_not_complete" + a reason. Pre-fix path fell through to
    # the state-derivation branch which couldn't synthesize a state
    # from this shape and emitted "?" — agent saw no actionable info
    # and might re-poll in a loop. Now: emit a clear ✗ line with the
    # run_id so the agent immediately knows to wait for 📣 notify.
    if raw.get("blocked_by") == "run_not_complete":
        run_id = str(raw.get("run_id") or raw.get("run_id_pending_notify") or "")
        reason = str(raw.get("reason") or "Run still executing.")
        line = (
            f"✗ run {run_id} still running. Wait for 📣 completion notify."
            if run_id
            else f"✗ {reason}"
        )
        blocks.append(_tc(line))
        return ToolResult(content=blocks)

    if raw.get("blocked_by") == "run_unknown":
        run_id = str(raw.get("run_id") or "")
        reason = str(raw.get("reason") or "Unknown run_id — start a fresh ai_run.")
        line = f"✗ run {run_id} unknown/stale — start a fresh ai_run." if run_id else f"✗ {reason}"
        blocks.append(_tc(line))
        return ToolResult(content=blocks)

    # Confirmable freeze refusal → render the FULL canonical approval card
    # from the structured freeze_state. Never truncate it to a 120-char
    # `err:` line and never fall back to legacy free-form confirmation
    # text — the operator must see every card field to decide.
    _fs = raw.get("freeze_state")
    if isinstance(_fs, dict) and _fs.get("approval_card"):
        try:
            from .freeze_service import render_card_from_state

            card_text = render_card_from_state(_fs, mode="verbose")
        except Exception:
            card_text = None
        if card_text:
            blocks.append(_tc(f"✗ {card_text}"))
            return ToolResult(content=blocks)

    # Any OTHER structured refusal (e.g. cross_session_run_id) → a clear ✗ FAIL
    # line carrying blocked_by + reason. Without this, an unrecognized refusal
    # shape has no state/done/tail and the state-fallback below emitted a bare
    # "?" — indistinguishable from success-glyph noise (a cross-session output
    # refusal looked like an ambiguous "?" instead of an unmistakable failure).
    if raw.get("ok") is False and raw.get("blocked_by"):
        run_id = str(raw.get("run_id") or "")
        bb = str(raw.get("blocked_by"))
        # Prefer the most specific human detail: reason, else the policy err
        # message (bash_policy stamps the matched deny rule into err/error),
        # else the bare blocked_by code.
        reason = str(raw.get("reason") or raw.get("error") or raw.get("err") or bb)
        tag = f"run {run_id} " if run_id else ""
        blocks.append(_tc(f"✗ {tag}refused ({bb}): {reason}"))
        return ToolResult(content=blocks)

    # #484: RUNNING-job progress shape (running_progress). Renders
    # elapsed + the bounded live tail with its honest label instead of
    # falling into the generic state header. Detection is keyed on
    # status='running' + elapsed_seconds together — no other payload
    # (spawn echo, tail_run_log, refusals) carries that pair, so the
    # finished-run rendering below is untouched.
    if raw.get("status") == "running" and "elapsed_seconds" in raw:
        run_id = str(raw.get("run_id") or "")
        bits = ["⏳ running"]
        if run_id:
            bits.append(f"run {run_id}")
        try:
            bits.append(f"{float(raw['elapsed_seconds']):.1f}s elapsed")
        except (TypeError, ValueError):
            pass
        if raw.get("output_bytes_so_far") is not None:
            bits.append(f"{raw['output_bytes_so_far']}b so far")
        blocks.append(_tc(" · ".join(bits)))
        _pr_tail = str(raw.get("output_tail") or "")
        _pr_label = str(raw.get("output_tail_label") or "")
        if _pr_tail:
            blocks.append(_tc(f"[{_pr_label}]\n{_pr_tail}" if _pr_label else _pr_tail))
        blocks.append(_tc("📣 completion notify still arrives — no need to poll."))
        _append_pending_notifications(blocks, None)
        return ToolResult(content=blocks)

    # Derive state: spawn_detached's inline-finish path returns
    # `done: True/False` not `state`; get_run_status/tail_run_log
    # return `state`. Harmonize so the header always has something
    # meaningful.
    state = str(raw.get("state") or raw.get("status") or "")
    if not state:
        if raw.get("done") is True:
            state = "done"
        elif raw.get("done") is False:
            state = "running"
        else:
            state = "?"
    exit_code = raw.get("exit_code")
    duration = raw.get("duration_seconds")
    log_bytes = raw.get("log_bytes")
    err = raw.get("error") or raw.get("err")
    # Summary line leads with an outcome glyph when the exit code is
    # known (legibility goal 2026-07-11): ✅ exit 0, ✗ otherwise.
    _lead = state
    if exit_code is not None:
        try:
            _glyph = "✅" if int(exit_code) == 0 else "✗"
            _lead = f"{_glyph} {state}"
        except (TypeError, ValueError):
            pass
    bits: list[str] = [_lead]
    if exit_code is not None:
        bits.append(f"exit {exit_code}")
    if duration is not None:
        try:
            bits.append(f"{float(duration):.1f}s")
        except (TypeError, ValueError):
            pass
    if log_bytes is not None:
        bits.append(f"{log_bytes}b")
    if err:
        bits.append(f"err: {str(err)[:120]}")
    blocks.append(_tc(" · ".join(bits)))
    tail = raw.get("tail") or raw.get("output") or ""
    command = str(raw.get("command") or "")
    if tail:
        body = str(tail).replace("\r\n", "\n").rstrip()
        # Classify by command so test failures / build errors get the
        # smart renderer (extracts FAILED test names, tracebacks,
        # error lines) instead of a raw dump.
        try:
            from .run_output_renderer import (
                classify_command,
                render_build_output,
                render_test_output,
                windowed_probe_view,
            )

            kind = classify_command(command) if command else "probe"
            if kind.startswith("test-"):
                test_blocks, _ = render_test_output(
                    command=command,
                    stdout=body,
                    stderr="",
                    exit_code=int(exit_code) if exit_code is not None else 0,
                    framework=kind,
                )
                blocks.extend(test_blocks)
            elif kind == "build":
                build_blocks, _ = render_build_output(
                    command=command,
                    stdout=body,
                    stderr="",
                    exit_code=int(exit_code) if exit_code is not None else 0,
                )
                blocks.extend(build_blocks)
            else:
                # Probe / unknown: spam-filtered tail WINDOW, never the
                # whole log (legibility goal 2026-07-11). Truthful
                # omission markers; full log stays in the run store.
                view, stats = windowed_probe_view(body)
                if stats["earlier_omitted"]:
                    blocks.append(
                        _tc(
                            f"… {stats['earlier_omitted']} earlier lines omitted "
                            f"(full log retained in run store)"
                        ),
                    )
                if stats["spam_dropped"]:
                    blocks.append(
                        _tc(f"({stats['spam_dropped']} progress/noise lines dropped)"),
                    )
                if view:
                    blocks.append(_tc(view))
        except Exception:
            # Fallback stays bounded: last-window slice of the raw body.
            _fb_lines = body.splitlines()
            if len(_fb_lines) > 120:
                blocks.append(_tc(f"… {len(_fb_lines) - 120} earlier lines omitted"))
                blocks.append(_tc("\n".join(_fb_lines[-120:])))
            else:
                blocks.append(_tc(body))
    
    elif "run_id" in raw and state in ("started", "running"):
        # Async-spawn first response with no tail yet.
        blocks.append(_tc(f"run_id: {raw['run_id']}"))

    # Notify-on-done: drain queue and append 📣 block. Silent no-op
    # when queue is empty.
    _append_pending_notifications(blocks, None)

    return ToolResult(content=blocks)


_RENDER_KIND_DISPATCH.update(
    {
        "find": _dispatch_find,
        "investigate": _dispatch_investigate,
        "text_search": _dispatch_text_search,
        "bundle": _dispatch_bundle,
        "trace": _dispatch_trace,
        "snippet": _dispatch_snippet,
        "schema": _dispatch_schema,
        "list": _dispatch_list,
        "status": _dispatch_status,
        "run_output": _dispatch_run_output,
    },
)


def renders_as(kind: str, title: str | None = None):
    """Decorator: post-process a tool's return value into a ToolResult
    using the renderer registered for `kind`. If the raw value is
    already a ToolResult, pass it through untouched (gate denials).

    `title` is used by kinds like "list" and "status" for the rendered
    header (e.g. @renders_as("list", title="sessions")).
    """
    import functools

    dispatcher = _RENDER_KIND_DISPATCH.get(kind)
    if dispatcher is None:
        raise ValueError(f"renders_as: unknown kind {kind!r}")

    import inspect
    import time

    def decorator(fn):
        is_coro = inspect.iscoroutinefunction(fn)
        tool_name = getattr(fn, "__name__", "") or ""

        def _emit_audit(
            total_ms: float,
            ok: bool,
            tokens_in: int | None = None,
            tokens_out: int | None = None,
        ) -> None:
            """Record tool-call metrics to execution_events. Best-effort
            — must never fail the tool call itself.
            """
            try:
                from .execution_index_store import ExecutionIndexStore
                from .mcp_server_runtime_helpers import resolve_project_root

                root = resolve_project_root()
                payload = {
                    "tool": tool_name,
                    "duration_ms": round(total_ms, 2),
                    "render_kind": kind,
                }
                if tokens_in is not None:
                    payload["tokens_in"] = int(tokens_in)
                if tokens_out is not None:
                    payload["tokens_out"] = int(tokens_out)
                ExecutionIndexStore().record_event(
                    root,
                    event_kind="tool_call",
                    source_kind="mcp",
                    action_kind=tool_name,
                    status="ok" if ok else "error",
                    payload=payload,
                )
            except Exception:
                pass

        def _render(raw: Any, kwargs: dict[str, Any], call_start: float) -> Any:
            total_ms = (time.perf_counter() - call_start) * 1000
            if isinstance(raw, ToolResult):
                _emit_audit(
                    total_ms,
                    ok=True,
                    tokens_in=_estimate_tokens(kwargs),
                )
                # Universal notify-on-done injection: every AIDOCS-
                # decorated tool drains the run_notifications queue
                # at return time. Silent no-op when empty. This is
                # the single injection point for all renders_as-wrapped
                # tools; text_result + _dispatch_run_output also call
                # _append_pending_notifications for tools that return
                # ToolResult through other paths, but this is the one
                # that fires on the common "tool already built its
                # own ToolResult" case.
                try:
                    blocks = list(raw.content)
                    _append_pending_notifications(blocks, None)
                    if len(blocks) != len(raw.content):
                        # PRESERVE structured_content + meta (2026-07-13):
                        # rebuilding with content only destroyed the
                        # structured payload whenever a 📣 block was
                        # pending — same contract break as the universal
                        # injector's ToolResult branch (KeyError: 'ok' on
                        # the VPS). Notifications are ADDITIVE.
                        raw = ToolResult(
                            content=blocks,
                            structured_content=raw.structured_content,
                            meta=raw.meta,
                        )
                except Exception:
                    pass
                return raw
            # Central index-staleness stamp for index-backed read tools, done
            # HERE (before rendering / before the pretty-OFF raw return) because a
            # rendered pretty-ON ToolResult carries no structured_content for the
            # outer call_tool wrapper to stamp. Only a DICT payload has a slot;
            # for list payloads the freshness rides in the pretty-ON terse ack
            # (see read_result). Authoritative + shape-preserving (adds only
            # reserved _index*/_memory*/_schema* keys).
            if isinstance(raw, dict):
                try:
                    from .index_staleness import apply_axes, axes_for
                    from .mcp_server_runtime_helpers import resolve_project_root

                    _axes = axes_for(tool_name, kwargs)
                    if _axes:
                        apply_axes(raw, resolve_project_root(), _axes)
                except Exception:
                    pass

            # Pretty OFF: return raw payload. The dispatcher-always-on
            # experiment (2026-04-20) broke ~40 tests that depend on
            # JSON shape — tools have a JSON contract with callers
            # the renderer must NOT silently mutate. Token-bloat
            # reduction belongs at the source (per-tool trim) or
            # behind explicit pretty=ON. (Reverted 2026-04-20.)
            if not _pretty_mode_active():
                _emit_audit(
                    total_ms,
                    ok=True,
                    tokens_in=_estimate_tokens(kwargs),
                    tokens_out=_estimate_tokens(raw),
                )
                marker = _debug_marker(
                    tool_name=tool_name,
                    duration_ms=total_ms,
                    tokens_in=_estimate_tokens(kwargs),
                    tokens_out=_estimate_tokens(raw),
                )
                if marker:
                    return _attach_marker(raw, marker)
                return raw
            name_token = _current_tool_name.set(tool_name)
            dur_token = _current_tool_duration_ms.set(total_ms)
            t0 = time.perf_counter()
            rendered: Any
            ok = True
            try:
                render_kwargs = dict(kwargs)
                if title:
                    render_kwargs["_render_title"] = title
                rendered = dispatcher(raw, render_kwargs)
            except Exception:
                ok = False
                rendered = raw
            render_ms = (time.perf_counter() - t0) * 1000
            _current_tool_name.reset(name_token)
            _current_tool_duration_ms.reset(dur_token)
            if render_ms > 10.0:
                try:
                    import sys as _sys

                    _sys.stderr.write(
                        f"[renders_as] slow render {kind!r}: {render_ms:.1f}ms (budget 10ms)\n",
                    )
                except Exception:
                    pass
            _emit_audit(
                total_ms + render_ms,
                ok=ok,
                tokens_in=_estimate_tokens(kwargs),
                tokens_out=_estimate_tokens(rendered),
            )
            return rendered

        if is_coro:

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                t0 = time.perf_counter()
                raw = await fn(*args, **kwargs)
                return _render(raw, kwargs, t0)

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            raw = fn(*args, **kwargs)
            return _render(raw, kwargs, t0)

        return wrapper

    return decorator


def text_result(
    *,
    lines: list[str] | Iterable[str],
    structured: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> ToolResult:
    """Build ToolResult from plain text lines.

    Pretty ON: pad footer appended via _pad_user_blocks.
    Pretty OFF: lines plus a trailing terse timing marker so agents see
    tool duration without opening the audit log.

    `structured` is INTENTIONALLY DROPPED. MCP hosts (Claude Code,
    OpenCode) render `structuredContent` *instead of* the text blocks
    when both are present, which means an agent calling ai_get_lines
    sees `{"total":850,"has_more":true}` instead of the actual lines.
    The pagination flags are mirrored into the text footer
    (`— (total=850, has_more)`) so no information is lost.
    (Diagnosed 2026-04-20.)
    """
    _ = structured  # accepted for caller-API stability; deliberately unused
    blocks = [_tc(str(line)) for line in lines]
    if _pretty_mode_active(project_root):
        blocks = _pad_user_blocks(blocks)
        _append_pending_notifications(blocks, project_root)
        return ToolResult(content=blocks)
    # Pretty OFF: append debug marker when any show_* flag is on.
    tokens_out = _estimate_tokens(lines) if blocks else 0
    marker = _debug_marker(
        tool_name=_current_tool_name.get(),
        duration_ms=_current_tool_duration_ms.get(),
        tokens_out=tokens_out,
        project_root=project_root,
    )
    if marker:
        blocks.append(_tc(marker))
    _append_pending_notifications(blocks, project_root)
    return ToolResult(content=blocks)


def _append_pending_notifications(
    blocks: list[TextContent],
    project_root: Path | None,
) -> None:
    """No-op since 2026-07-12: the universal notification injector
    (notification_injector.install_universal_notification_injection)
    now owns ALL tool-result notification drains — run-done
    notifications, lane completion reviews, role-addressed messages,
    and deploy safe-to-edit notices — for EVERY tool, dict and
    ToolResult returns alike.

    This per-render drain (run_notifications.peek/peek_for_session +
    trailing _append_pending_lane_reviews) duplicated the injector's
    surface for @renders_as tools: each 📣 notification rendered twice
    per tool result and burned notifications.max_displays twice as
    fast. Removed to guarantee at-most-once-per-tool-call surfacing.
    The def + signature stay so existing call sites remain valid
    no-ops.

    Provenance, #50 L3 (canonical 2026-04-26): lane workers
    (AIDOCS_EXPERT_LANE_ID env set) NEVER drain — drain is a
    conductor/session-owner primitive; subagents receive run results
    via the lane plan/handoff path. That worker fence now lives in
    notification_injector._is_worker_caller.
    """
    return

