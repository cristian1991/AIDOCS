from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import Field

from . import structured_file_parsers
from .edit_memory_gate import check_edit_memory_gate
from .mcp_server_runtime_helpers import (
    require_active_task,
    resolve_project_root,
)
from .mode_schema import modes
from .tool_display import (
    edit_result,
    render_batch_edit_summary,
    render_create_file,
    render_edit_diff,
    render_insert_lines,
    render_str_replace_diff,
    text_result,
)


def register_code_edit_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any = None,
    require_indexed_read_gate: Any,
    post_edit_reindex_and_grant: Any,
    file_get_lines: Any,
    file_read_raw: Any,
    file_create_file: Any,
    file_edit_lines: Any,
    file_batch_edit: Any,
    file_str_replace: Any,
    file_batch_str_replace: Any,
    anchor_replace: Any,
    available_config_edit_modes: Any,
    self_edit_available_in_profile: Any,
) -> None:
    def _file_was_read(project_root: Path, path: str) -> tuple[bool, str]:
        """Looser sibling of _lines_were_read: any ai_get_lines /
        ai_get_symbol_snippet / ai_bundle on this path in the
        current session counts. Used by string-match edits where the
        line range isn't knowable up front (the engine matches
        old_str at runtime).
        """
        managed = hub.managed_mode.get_mode(project_root)
        if not managed.get("active"):
            return True, "unmanaged"
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return True, "no-session"
        target = path.replace("\\", "/").lstrip("/")
        try:
            # Query by path substring so reads from before MCP restart
            # still count — execution_events is persistent. Limit 200
            # is generous enough for long sessions while staying fast.
            events = hub.execution.list_events(
                project_root,
                query=target,
                session_id=session_id,
                limit=200,
            )
        except Exception:
            return True, "events-unavailable"
        _READ_TOOLS = {
            "ai_get_lines",
            "ai_get_symbol_snippet",
            "ai_bundle",
            "ai_slop",
            # File-level read evidence: ai_get_outline returns the
            # symbol+line index and ai_get_symbol_info returns
            # signatures/properties. Neither returns full body, so
            # they only count for file-level reads (the snippet
            # fallback path); they are NOT eligible to satisfy the
            # line-range coverage check above. (#13 item 1, #58.)
            "ai_get_outline",
            "ai_get_symbol_info",
            # #78 (Phoenix 2026-05-10): a successful edit/create
            # proves the agent has the content loaded — downstream
            # str_replace shouldn't have to re-read just to satisfy
            # the gate. "Wrote it = read it" semantics. Edit-evidence
            # covers FILE-LEVEL only; the line-range coverage check
            # (_lines_were_read) still requires an explicit
            # ai_get_lines for sparse range proofs.
            "ai_str_replace",
            "ai_anchor_replace",
            "ai_edit_lines",
            "ai_insert_lines",
            "ai_batch_str_replace",
            "ai_batch_edit",
            "ai_create_file",
            "ai_replace",
            # Legacy code_* aliases — audit rows may carry python
            # function names on older entries or internal call paths.
            "ai_insert_lines",
        }
        # 2026-05-17 fix (king Q5): the pre-tool audit middleware
        # records a `tool_call_started` event BEFORE the gate runs.
        # Without this filter the gate counts the in-flight call's
        # own started-event as proof the file was read → chicken-and-
        # egg, gate bypassed. Only events that report a completion
        # status (applied/completed/success) count as evidence —
        # 'started' events do NOT.
        _COMPLETED_STATUSES = {"applied", "completed", "success", "completed_ok"}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if str(ev.get("capability_name") or "") not in _READ_TOOLS:
                continue
            if str(ev.get("status") or "").lower() not in _COMPLETED_STATUSES:
                continue
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue
            ev_path = str(payload.get("target_path") or "").replace("\\", "/").lstrip("/")
            if ev_path == target:
                return True, "file was read this session"
        return False, (
            f"no read of `{target}` in this session. Read the symbol "
            f"first: ai_get_symbol_snippet(path='{target}', symbol=...) "
            f"— or ai_get_lines for non-symbol files. Edit must be "
            f"grounded in the actual content."
        )

    def _lines_were_read(project_root: Path, path: str, start: int, end: int) -> tuple[bool, str]:
        """Check execution_events for a recent ai_get_lines call on
        `path` whose range covers [start, end].

        Stops "read line 1, edit lines 50-300" attacks: edits must
        prove the agent has actually seen the content being replaced.
        Returns (covered, reason). Walks last 100 events for the
        active session; the read window is intentionally generous
        (per-turn semantics) so agents don't have to re-read between
        diagnose and patch.
        """
        managed = hub.managed_mode.get_mode(project_root)
        if not managed.get("active"):
            return True, "unmanaged"
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return True, "no-session"
        target = path.replace("\\", "/").lstrip("/")
        try:
            # Query by path substring so reads from before MCP restart
            # still count — execution_events is persistent. Limit 200
            # is generous enough for long sessions while staying fast.
            events = hub.execution.list_events(
                project_root,
                query=target,
                session_id=session_id,
                limit=200,
            )
        except Exception:
            return True, "events-unavailable"
        # Collect ranges from ai_get_lines calls on this path.
        # Payload shape (per mcp_server.payload_summary):
        #   target_path: "src/foo.py"
        #   line_range: "12-42"  (start-end string)
        # 2026-05-17 fix (king Q5): same status filter as _file_was_read —
        # pre-tool 'started' events must NOT count as evidence.
        _COMPLETED_STATUSES_LR = {"applied", "completed", "success", "completed_ok"}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if str(ev.get("capability_name") or "") != "ai_get_lines":
                continue
            if str(ev.get("status") or "").lower() not in _COMPLETED_STATUSES_LR:
                continue
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue
            ev_path = str(payload.get("target_path") or "").replace("\\", "/").lstrip("/")
            if ev_path != target:
                continue
            line_range = str(payload.get("line_range") or "")
            if not line_range:
                continue
            try:
                lo_str, hi_str = line_range.split("-", 1)
                lo = int(lo_str.strip())
                hi_clean = hi_str.strip().rstrip("?")
                hi = int(hi_clean) if hi_clean.isdigit() else lo
            except (ValueError, AttributeError):
                continue
            if lo <= start and hi >= end:
                return True, f"covered by ai_get_lines L{lo}-L{hi}"
        # Fallback: ai_get_symbol_snippet / code_extract_* /
        # ai_bundle(mode=file) all surface body content with line
        # attribution. They don't stamp line_range into the event
        # payload (they take symbol/path, not start/end), so the
        # exact-range check above misses them. But agents using the
        # index correctly READ a symbol via ai_get_symbol_snippet
        # then EDIT it — that's the canonical flow, not a gate
        # violation. Accept any of these as proof the file's content
        # has been seen (same loose rule as _file_was_read for
        # ai_str_replace). (Diagnosed 2026-04-20.)
        ok, _ = _file_was_read(project_root, path)
        if ok:
            return True, "covered by indexed read (snippet/bundle/extract)"
        return False, (
            f"no read of `{target}` covers L{start}-L{end} this session. "
            f"Read the symbol first: ai_get_symbol_snippet(path='{target}', "
            f"symbol=...) or ai_get_lines(path='{target}', "
            f"start_line={start}, count={end - start + 1})."
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Code Lines",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def ai_get_lines(
        path: str,
        start_line: int = 1,
        count: int = 30,
        known_exact_path: bool = False,
    ) -> ToolResult:
        """Fallback line-range read. PREFER indexed tools FIRST.

        When possible use: `ai_get_symbol_snippet` (symbol body with
        container context), `ai_bundle(mode="file")` (whole file with
        outline), `ai_investigate` (concept → ranked snippets), or
        `ai_find` + `ai_get_symbol_info`. These return targeted content
        with no line-number drift and fewer cold reads.

        Use `ai_get_lines` only when:
        - You genuinely need a specific line range (error line N, diff hunk)
        - The file has no indexed symbols (config, markdown, toml)
        - You've already narrowed via indexed discovery and need the exact slice

        Line numbers are included in the output.
        """
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        # Slice 1 (canonical 2026-04-29): line reads route through the
        # shared read pipeline in STRICT mode. file_get_lines is a
        # project/indexed reader and accepts only project-relative
        # paths. Strict mode rejects absolute input, `..` traversal,
        # and any zone outside PROJECT_INTERNAL / MEMORY_INTERNAL up
        # front — and the indexed-read gate still fires inside
        # _rp.gate via _indexed_read_block. Approved-external roots
        # are intentionally NOT supported here; an absolute-path
        # variant would be a separate tool surface.
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="strict",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            refusal = gate_result.refusal
            msg = (
                refusal.get("user_message")
                or refusal.get("error")
                or refusal.get("reason")
                or str(refusal)
            )
            return ToolResult(content=[TextContent(type="text", text=str(msg))])
        # Strict mode guarantees project_relative is set on allow
        # paths (path is always inside project_root). Defense-in-
        # depth: refuse rather than fall back to the raw user path.
        if gate_result.project_relative is None:
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "❌ ai_get_lines: gate produced no project-relative "
                            "form for an allowed path; refusing rather than "
                            "passing the raw user path to file_get_lines."
                        ),
                    ),
                ],
            )
        result = file_get_lines(
            project_root,
            gate_result.project_relative,
            start_line=start_line,
            count=count,
            show_line_numbers=True,
        )
        stale = (
            hub.code.is_file_stale(project_root, gate_result.project_relative)
            if isinstance(result, dict)
            else False
        )
        # Dual-channel: TextContent per line renders in host UI, thin
        # structured_content carries pagination/stale flags for agents.
        # Debug mode (tool_output.debug + dev_mode) swaps the content
        # with the agent-facing structured view for dogfooding.
        raw_lines = result.get("lines") if isinstance(result, dict) else None
        if not isinstance(raw_lines, list) or not raw_lines:
            text = result.get("content", "") if isinstance(result, dict) else ""
            raw_lines = [line for line in str(text).split("\n")]
        structured: dict[str, Any] = {}
        if isinstance(result, dict):
            if result.get("total") is not None:
                structured["total"] = result.get("total")
            if result.get("has_more"):
                structured["has_more"] = True
            if result.get("truncated"):
                structured["truncated"] = True
            if result.get("hidden_unicode_stripped"):
                structured["hidden_unicode_stripped"] = result["hidden_unicode_stripped"]
        if stale:
            structured["stale"] = True
        meta_bits: list[str] = []
        if "total" in structured:
            meta_bits.append(
                f"(total={structured['total']}"
                + (", has_more)" if structured.get("has_more") else ")"),
            )
        if structured.get("truncated"):
            meta_bits.append("truncated")
        if structured.get("hidden_unicode_stripped"):
            meta_bits.append(f"hidden_unicode_stripped={structured['hidden_unicode_stripped']}")
        if stale:
            meta_bits.append("stale: run ai_index_sync")
        if meta_bits:
            raw_lines = list(raw_lines) + ["— " + " ".join(meta_bits)]
        # Razor first-read summary (king directive 2026-05-28).
        # When reading a .cshtml / .razor file from line 1, prepend a
        # structural banner: total line count + every partial reference
        # with its resolved file path, line count, and invocation lines.
        # The agent gets in ONE round-trip what would otherwise take
        # 5-10 ai_find / read calls to discover.
        _razor_banner = ""
        if start_line <= 1:
            try:
                from .razor_summary import razor_first_read_summary

                _razor_banner = razor_first_read_summary(
                    project_root,
                    gate_result.project_relative,
                )
            except Exception:
                _razor_banner = ""
            if _razor_banner:
                raw_lines = _razor_banner.split("\n") + ["", *list(raw_lines)]
                structured = dict(structured or {})
                structured["razor_summary"] = True

        # #62 Phase 3: prepend DNT banner once-per-session per family.
        try:
            from .dnt_banner_injector import maybe_dnt_banner_for_read

            _banner = maybe_dnt_banner_for_read(project_root, gate_result.project_relative)
        except Exception:
            _banner = ""
        if _banner:
            raw_lines = _banner.split("\n") + ["", *list(raw_lines)]
            structured = dict(structured or {})
            structured["dnt_banner"] = True
        return text_result(
            lines=raw_lines,
            structured=structured or None,
            project_root=project_root,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Raw Bytes",
        },
    )
    def ai_read_raw(
        path: str,
        offset_bytes: int = 0,
        limit_bytes: int | None = None,
        encoding: str = "utf-8",
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Read a byte range of any file as text. Use for non-indexed text files (logs, CSVs, .csproj, .resx, plain configs) or when reading files too large for the line-based tools. For PDFs/Excel/docx, use the structured-parser tools. Soft cap 512KB per call; hard cap 8MB. For larger files, paginate via offset_bytes + limit_bytes."""
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="strict",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            return gate_result.refusal
        # Strict mode guarantees project_relative is set (path is
        # always inside project_root). file_ops.read_raw expects the
        # relative form per its existing contract.
        result = file_read_raw(
            project_root,
            gate_result.project_relative or path,
            offset_bytes=offset_bytes,
            limit_bytes=limit_bytes,
            encoding=encoding,
        )
        # #62 Phase 3 completion (Phoenix, 2026-05-07): the missing
        # third wire. ai_get_lines and ai_get_symbol_snippet have
        # carried the DNT banner since Phase 3; raw was the orphaned
        # tool — promised in the test docstring, never wired.
        try:
            from .dnt_banner_injector import maybe_dnt_banner_for_read

            _banner = maybe_dnt_banner_for_read(
                project_root,
                gate_result.project_relative,
            )
        except Exception:
            _banner = ""
        # Trim always: drop path/offset/limit echoes, keep payload +
        # pagination flags (content, total_bytes, has_more, truncated).
        if isinstance(result, dict):
            trimmed: dict[str, Any] = {}
            for k in ("content", "total_bytes", "total", "has_more", "truncated"):
                v = result.get(k)
                if v in (None, "", [], {}):
                    continue
                trimmed[k] = v
            trimmed["requested_path"] = gate_result.requested_path
            trimmed["resolved_path"] = str(gate_result.resolved_path)
            trimmed["zone"] = str(gate_result.zone)
            if _banner:
                trimmed["dnt_banner"] = _banner
            return trimmed or result
        return result

    def _structured_error(tool_name: str, exc: BaseException) -> dict[str, Any]:
        """Wrap parser exceptions in a structured payload. FileNotFoundError /
        ValueError / ModuleNotFoundError from the parser module carry
        actionable messages already — surface them verbatim.
        """
        return {
            "error": f"{tool_name}: {exc}",
            "error_type": type(exc).__name__,
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read PDF",
        },
    )
    def ai_read_pdf(
        path: str,
        pages: str = "",
        mode: str = "text",
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Extract text/tables from a PDF. mode: 'text' | 'text_and_tables'. pages: "1-5,8" or "" for all (max 50). Requires the 'office' extra."""
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="zoned",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            return gate_result.refusal
        try:
            out = structured_file_parsers.read_pdf(
                str(gate_result.resolved_path),
                pages=pages,
                mode=mode,
            )
            out["requested_path"] = gate_result.requested_path
            out["resolved_path"] = str(gate_result.resolved_path)
            out["zone"] = str(gate_result.zone)
            return out
        except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
            return _structured_error("ai_read_pdf", exc)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Excel",
        },
    )
    def ai_read_excel(
        path: str,
        mode: str = "outline",
        sheet: str = "",
        cell: str = "",
        query: str = "",
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Inspect an Excel workbook (read-only). Modes: 'outline' (sheets+headers), 'sheet' (rows, max 500, needs sheet), 'formulas' (needs sheet), 'trace' (parse formula at sheet!cell). Requires the 'office' extra."""
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="zoned",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            return gate_result.refusal
        try:
            out = structured_file_parsers.read_excel(
                str(gate_result.resolved_path),
                mode=mode,
                sheet=sheet,
                cell=cell,
                query=query,
            )
            out["requested_path"] = gate_result.requested_path
            out["resolved_path"] = str(gate_result.resolved_path)
            out["zone"] = str(gate_result.zone)
            return out
        except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
            return _structured_error("ai_read_excel", exc)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read DOCX",
        },
    )
    def ai_read_docx(
        path: str,
        sections: str = "",
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Extract paragraphs and tables from a .docx in document order. 'sections' is an optional "1-3" range limiting output to the first N Heading-1 sections. Requires the 'office' extra."""
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="zoned",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            return gate_result.refusal
        try:
            out = structured_file_parsers.read_docx(
                str(gate_result.resolved_path),
                sections=sections,
            )
            out["requested_path"] = gate_result.requested_path
            out["resolved_path"] = str(gate_result.resolved_path)
            out["zone"] = str(gate_result.zone)
            return out
        except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
            return _structured_error("ai_read_docx", exc)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read SQLite",
        },
    )
    def ai_read_sqlite(
        path: str,
        mode: str = "tables",
        table: str = "",
        query: str = "",
        limit: int = 100,
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Inspect or query a SQLite file (read-only). Modes: 'tables' (names+row counts), 'schema' (CREATE stmts, optionally filtered by table), 'query' (SELECT-only, capped at `limit`)."""
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="zoned",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            return gate_result.refusal
        try:
            out = structured_file_parsers.read_sqlite(
                str(gate_result.resolved_path),
                mode=mode,
                table=table,
                query=query,
                limit=limit,
            )
            out["requested_path"] = gate_result.requested_path
            out["resolved_path"] = str(gate_result.resolved_path)
            out["zone"] = str(gate_result.zone)
            return out
        except (FileNotFoundError, ValueError, ModuleNotFoundError) as exc:
            return _structured_error("ai_read_sqlite", exc)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read JSONL",
        },
    )
    def ai_read_jsonl(
        path: str,
        where: dict[str, Any] | None = None,
        select: list[str] | None = None,
        content_contains: str = "",
        offset: int = 0,
        limit: int = 50,
        known_exact_path: bool = False,
    ) -> dict[str, Any]:
        """Stream a JSONL file with field-level filter + projection.

        `where` is dotted-path=value exact-match pairs; a row must match ALL pairs.
        `select` projects specific dotted paths per row (e.g. 'message.role'); None returns full objects.
        `content_contains` is a pre-parse substring pre-filter for speed on big files.
        Returns {path, rows, row_count, scanned_lines, invalid_lines, matched_total, truncated, offset, limit}.
        Designed for Claude Code session jsonl (~/.claude/projects/.../*.jsonl) and MCP telemetry streams where blob reads would waste context.
        """
        from . import read_pipeline as _rp

        project_root = resolve_project_root()
        gate_result = _rp.gate(
            hub,
            project_root,
            path,
            mode="zoned",
            known_exact_path=known_exact_path,
        )
        if gate_result.refusal is not None:
            return gate_result.refusal
        try:
            out = structured_file_parsers.read_jsonl(
                str(gate_result.resolved_path),
                where=where,
                select=select,
                content_contains=content_contains or None,
                offset=offset,
                limit=limit,
            )
            out["requested_path"] = gate_result.requested_path
            out["resolved_path"] = str(gate_result.resolved_path)
            out["zone"] = str(gate_result.zone)
            return out
        except (FileNotFoundError, ValueError) as exc:
            return _structured_error("ai_read_jsonl", exc)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Create File",
        },
    )
    def ai_create_file(
        path: str,
        content: str,
        config_edit_mode: str | None = None,
        ack_memory_ids: list[int] | None = None,
        ack_drawer_ids: list[str] | None = None,
    ) -> Any:
        """Create a new file at a relative path with exact content."""
        _t0_create_file = time.perf_counter()
        project_root = resolve_project_root()
        gate = require_active_task(hub, project_root, "ai_create_file")
        if gate is not None:
            return gate
        mem_gate = _check_anchored_memory(
            project_root,
            paths=[path],
            ack_memory_ids=ack_memory_ids,
            ack_drawer_ids=ack_drawer_ids,
            tool_name="ai_create_file",
        )
        if mem_gate is not None:
            return mem_gate
        # Slice 1 (canonical 2026-04-29): MCP-direct enforcement.
        # Run AccessGate / tool_policy / heuristic_judge / freeze /
        # needs_confirmation via the shared cascade BEFORE the tool
        # touches disk. Without this, a non-CC host bypassed every
        # gate except require_active_task — the verification artifact
        # called this out as the highest-blast-radius edit gap.
        from .gate_tool import enforce_tool_call

        enforce = enforce_tool_call(
            hub,
            project_root,
            "ai_create_file",
            {"path": path, "content": content},
            fail_closed=True,
            include_freeze=True,
            runtime=runtime,
        )
        if enforce.refusal is not None:
            refusal = enforce.refusal
            blocked_by = str(refusal.get("blocked_by", "denied"))
            reason = str(refusal.get("reason", "tool refused"))
            return edit_result(
                content_blocks=[
                    TextContent(
                        type="text",
                        text=f"❌ create_file refused: {reason}",
                    ),
                ],
                structured={
                    "ok": False,
                    "error": reason,
                    "blocked_by": blocked_by,
                    "freeze_state": refusal.get("freeze_state"),
                    "path": path,
                },
                project_root=project_root,
                tool_name="ai_create_file",
                started_at=_t0_create_file,
            )
        result = file_create_file(
            project_root,
            path,
            content,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            reindex = post_edit_reindex_and_grant(
                hub,
                project_root,
                "ai_create_file",
                str(result.get("path") or path),
            )
            if not reindex.get("ok"):
                err = str(reindex.get("error") or "post-edit index refresh failed")
                return edit_result(
                    content_blocks=[
                        TextContent(
                            type="text",
                            text=f"❌ create_file: `{path}` — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                        ),
                    ],
                    structured={"ok": False, "error": err, "path": str(result.get("path") or path)},
                    project_root=project_root,
                    tool_name="ai_create_file",
                    started_at=_t0_create_file,
                )
            blocks = render_create_file(
                path=str(result.get("path") or path),
                line_count=len((content or "").splitlines()),
                byte_count=int(result.get("bytes_written") or len((content or "").encode("utf-8"))),
            )
            return edit_result(
                content_blocks=blocks,
                structured={"ok": True},
                project_root=project_root,
                tool_name="ai_create_file",
                started_at=_t0_create_file,
            )
        err = str(result.get("error") or "create_file failed")
        return edit_result(
            content_blocks=[TextContent(type="text", text=f"❌ create_file: `{path}` — {err}")],
            structured={"ok": False, "error": err, "path": path},
            project_root=project_root,
            tool_name="ai_create_file",
            started_at=_t0_create_file,
        )

    @server.tool(
        annotations={
            # Doctrine 2026-05-29 (king-directed seal): destructiveHint=
            # False — the carve-out that previously pinned this True
            # "until the gate-proof tests land" is now CLOSED. The
            # proofs the carve-out was waiting for now exist:
            #   - live read-pipeline proof that AccessGate /
            #     enforce_tool_call consults the .TRASH/ classifier on
            #     raw Read/Grep/Write/ai_get_lines paths;
            #   - native Claude PreToolUse trash-gate proof that the
            #     host-side hook refuses .TRASH/** before the call lands.
            # AIDOCS owns confirmation: trash-based recovery, quota
            # refusal (now fail-CLOSED, not (0,0)-open), idempotency,
            # forbidden-basename block, audit chain, and the registry-
            # layer confirm=TWO_PHASE with the path-bearing
            # "confirm-delete {path}" phrase the operator must echo.
            # Host annotations are UX hints only — see outer_gate_
            # catalog.py:_annotations and test_outer_gate_tools_list_
            # metadata for the surface-wide contract.
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Delete File",
        },
    )
    def ai_delete(
        path: Annotated[
            str,
            Field(
                description=(
                    "Project-relative path to delete. Absolute paths are "
                    "refused; the path resolves against the selected "
                    "project root. Single file only — no glob, no batch, "
                    "no directory tree."
                ),
            ),
        ],
        reason: Annotated[
            str,
            Field(
                description=(
                    "Why the file is being deleted (operator-visible in "
                    'the audit log). Should be specific — "dead import '
                    'after refactor" / "orphan generated artifact", '
                    'not "cleanup".'
                ),
            ),
        ],
    ) -> Any:
        """Delete a single project-relative file by moving it to the
        project's `.TRASH/` directory.

        Trash-based: the file lands at
        `.TRASH/<YYYY-MM-DD>/<hex>-<basename>` and is recoverable via
        `mv` (or `git checkout <path>` if the file was tracked).
        There is no programmatic `ai_trash_restore` tool — operator
        recovery is the filesystem command.

        Refuses with typed errors on:
          - absolute or ../-escape paths
          - reserved prefixes (.git/, .MEMORY/, .TRASH/, .aidocs/,
            .github/, .venv/, node_modules/)
          - forbidden basenames (.env*, credentials, id_rsa/ed25519,
            authorized_keys, AIDOCS release trust files)
          - directories (single-file invariant)
          - files over the size ceiling (25 MB default, configurable
            via `delete.max_trash_bytes`)

        Idempotent across kill+respawn within the configured window
        (15 min default, `delete.idempotency_window_minutes`): if the
        target is missing AND a recent matching `file_deleted` event
        exists, returns the prior trash_id deterministically.

        Built 2026-05-27 per the design at
        ``.MEMORY/sessions/2026-04-22-oc-plugin-testing/plans/lane-control-architecture.md``.
        """
        _t0_delete = time.perf_counter()
        project_root = resolve_project_root()
        gate = require_active_task(hub, project_root, "ai_delete")
        if gate is not None:
            return gate

        # Run the full gate cascade — protected_paths_classifier,
        # heuristic_judge, freeze, RBAC — before touching the file.
        # The service has its own typed refusals for design-specific
        # rules (size ceiling, reserved prefixes, forbidden basenames,
        # idempotency); the cascade catches everything else.
        from .gate_tool import enforce_tool_call

        enforce = enforce_tool_call(
            hub,
            project_root,
            "ai_delete",
            {"path": path, "reason": reason},
            fail_closed=True,
            include_freeze=True,
            runtime=runtime,
        )
        if enforce.refusal is not None:
            refusal = enforce.refusal
            blocked_by = str(refusal.get("blocked_by", "denied"))
            reason_text = str(refusal.get("reason", "tool refused"))
            return edit_result(
                content_blocks=[
                    TextContent(
                        type="text",
                        text=f"❌ ai_delete refused: {reason_text}",
                    ),
                ],
                structured={
                    "ok": False,
                    "error": reason_text,
                    "blocked_by": blocked_by,
                    "freeze_state": refusal.get("freeze_state"),
                    "path": path,
                },
                project_root=project_root,
                tool_name="ai_delete",
                started_at=_t0_delete,
            )

        # Resolve the managed session id (for audit + idempotency
        # window queries). On unmanaged projects this is empty — the
        # service handles the empty-session case gracefully (skips
        # idempotency check, still records refusal events).
        session_id = ""
        try:
            managed = hub.managed_mode.get_mode(project_root)
            if managed.get("active"):
                session_id = str(managed.get("session_id") or "").strip()
        except Exception:
            session_id = ""

        from .file_delete_service import FileDeleteService

        result = FileDeleteService(hub).delete(
            project_root=project_root,
            path=path,
            reason=reason,
            session_id=session_id,
            actor="agent",
            tool_name="ai_delete",
        )

        if not result.ok:
            err_msg = result.error_message or result.error or "delete refused"
            return edit_result(
                content_blocks=[
                    TextContent(
                        type="text",
                        text=f"❌ ai_delete: `{path}` — {err_msg}",
                    ),
                ],
                structured=result.to_dict(),
                project_root=project_root,
                tool_name="ai_delete",
                started_at=_t0_delete,
            )

        # On a successful delete the index drops the file. ai_create_file
        # uses post_edit_reindex_and_grant to keep the index in sync;
        # for delete we surface the gone-file event through the index
        # the same way. Best-effort — a stale index entry is a soft
        # inconvenience (the file is gone; ai_find returns a now-broken
        # pointer until next sync), not a security issue.
        try:
            post_edit_reindex_and_grant(
                hub,
                project_root,
                "ai_delete",
                result.rel_posix or path,
            )
        except Exception:
            pass

        if result.already_deleted:
            msg = (
                f"✓ ai_delete: `{result.rel_posix}` — already trashed "
                f"(idempotent repeat). prior trash_id={result.trash_id}"
            )
        else:
            msg = (
                f"✓ ai_delete: `{result.rel_posix}` → `{result.trash_id}` "
                f"({result.size_bytes} bytes). Recover via: "
                f"`mv {result.trash_id} {result.rel_posix}`"
            )
        return edit_result(
            content_blocks=[TextContent(type="text", text=msg)],
            structured=result.to_dict(),
            project_root=project_root,
            tool_name="ai_delete",
            started_at=_t0_delete,
        )

    def _check_turn_edited(project_root: Path, path: str, tool_name: str) -> dict[str, Any] | None:
        """Return a rejection result if this file was already line-edited this turn, else None.
        Sequential line-based edits to the same file corrupt line numbers. Force the agent
        to batch instead. ai_batch_edit, ai_str_replace, and ai_batch_str_replace
        bypass this check — they handle ordering internally or use line-independent matching.
        """
        try:
            managed = hub.managed_mode.get_mode(project_root)
            if not managed.get("active"):
                return None
            session_id = str(managed.get("session_id") or "").strip()
            if not session_id:
                return None
            canonical = str(path).replace("\\", "/").strip()
            newly_added = hub.query_gate.add_turn_edited_file(project_root, session_id, canonical)
            if newly_added:
                return None
            return {
                "success": False,
                "error": (
                    f"File `{canonical}` was already edited this turn via a line-based tool. "
                    f"Sequential {tool_name} calls on the same file corrupt line numbers — "
                    f"the second call's start_line/end_line refer to a stale view. "
                    f"Use `ai_batch_edit` with ALL your edits in one call (it sorts bottom-up internally), "
                    f"or use `ai_str_replace` / `ai_batch_str_replace` which match on content, not lines. "
                    f"This limit resets on the next user prompt."
                ),
                "path": canonical,
                "tool": tool_name,
            }
        except Exception:
            return None

    def _check_anchored_memory(
        project_root: Path,
        *,
        paths: list[str],
        symbol_name: str = "",
        ack_memory_ids: list[int] | None = None,
        ack_drawer_ids: list[str] | None = None,
        tool_name: str = "edit",
    ) -> dict[str, Any] | None:
        """Anchored-memory + palace gate. Returns a refusal dict if
        anchored memory_store routes OR palace drawer anchors exist on
        the target(s) and are not all acked; else returns None.

        RFC-4 Phase B: this wrapper now delegates to the unified
        ``check_edit_memory_gate(hub=...)``, which composes memory_store
        blockers with palace blockers (exact_symbol / operator_pinned
        confidence tiers). The hammer routes through the palace gate.

        Single ack_memory_ids list = ALL memory_store route ids across
        all paths; single ack_drawer_ids list = ALL palace drawer ids
        across all paths. The refusal payload always names the full
        id set so one retry with the right acks unblocks.
        """
        unique_paths = list(dict.fromkeys(p for p in (paths or []) if p))
        if not unique_paths and not symbol_name:
            return None

        primary = unique_paths[0] if unique_paths else ""
        per_path_refusals: list[tuple[str, dict[str, Any]]] = []
        combined_unacked_mem: list[int] = []
        combined_needed_drawers: list[str] = []
        combined_mem_paths: list[str] = []
        blocked_by_set: set[str] = set()

        for fp in unique_paths or [""]:
            sym = symbol_name if fp == primary else ""
            gr = check_edit_memory_gate(
                project_root,
                file_path=fp,
                symbol_name=sym,
                ack_memory_ids=ack_memory_ids,
                ack_drawer_ids=ack_drawer_ids,
                tool_name=tool_name,
                hub=hub,
            )
            if gr.allowed:
                continue
            r = gr.refusal or {}
            per_path_refusals.append((fp, dict(r)))
            blocked_by_set.add(str(r.get("blocked_by", "anchored_memory")))
            combined_unacked_mem.extend(r.get("unacked_memory_ids") or [])
            combined_needed_drawers.extend(r.get("ack_drawer_ids_needed") or [])
            combined_mem_paths.extend(r.get("memory_paths") or [])

        if not per_path_refusals:
            return None

        # Multi-path aggregate: rebuild the error TEXT so every refused
        # path's id surface appears in one message. Single-path keeps
        # the underlying gate's text verbatim.
        if len(per_path_refusals) == 1:
            error_text = str(per_path_refusals[0][1].get("error") or "")
        else:
            chunks = []
            for fp, r in per_path_refusals:
                err = str(r.get("error") or "").strip()
                if err:
                    chunks.append(f"[{fp}]\n{err}")
            error_text = "REFUSED across {n} paths:\n{body}".format(
                n=len(per_path_refusals),
                body="\n\n".join(chunks),
            )

        # blocked_by composition: if any path was palace-blocked, surface
        # that; otherwise the memory-store label wins.
        if "palace_anchored_memory" in blocked_by_set and "anchored_memory" in blocked_by_set:
            blocked_by = "anchored_memory+palace_anchored_memory"
        elif "palace_anchored_memory" in blocked_by_set:
            blocked_by = "palace_anchored_memory"
        else:
            blocked_by = "anchored_memory"

        refusal_dict: dict[str, Any] = {
            "ok": False,
            "success": False,
            "error": error_text,
            "blocked_by": blocked_by,
            "tool": tool_name,
        }
        if combined_unacked_mem:
            refusal_dict["unacked_memory_ids"] = sorted(set(combined_unacked_mem))
        if combined_needed_drawers:
            refusal_dict["ack_drawer_ids_needed"] = sorted(set(combined_needed_drawers))
        if combined_mem_paths:
            refusal_dict["memory_paths"] = sorted(set(combined_mem_paths))
        return refusal_dict

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Insert Lines",
        },
    )
    def ai_insert_lines(
        path: str,
        before_line: int,
        content: str,
        config_edit_mode: Literal["explicit_user_permitted"] | None = None,
        ack_memory_ids: list[int] | None = None,
        ack_drawer_ids: list[str] | None = None,
    ) -> Any:
        """Insert content before a specific line. Clearer than ai_edit_lines insert mode."""
        _t0_insert = time.perf_counter()
        project_root = resolve_project_root()
        gate = require_active_task(hub, project_root, "ai_insert_lines")
        if gate is not None:
            return gate
        rejection = _check_turn_edited(project_root, path, "ai_insert_lines")
        mem_gate = _check_anchored_memory(
            project_root,
            paths=[path],
            ack_memory_ids=ack_memory_ids,
            ack_drawer_ids=ack_drawer_ids,
            tool_name="ai_insert_lines",
        )
        if mem_gate is not None:
            return mem_gate
        if rejection is not None:
            return rejection
        result = file_edit_lines(
            project_root,
            path,
            start_line=before_line,
            end_line=before_line - 1,
            new_content=content,
            mode="insert",
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            # evict_known_path=True per king directive 2026-05-12: insert
            # shifts every line after `before_line`, so the agent's cached
            # known_exact_path is stale. Force a re-read before next line op.
            reindex = post_edit_reindex_and_grant(
                hub,
                project_root,
                "ai_insert_lines",
                str(result.get("path") or path),
                evict_known_path=True,
            )
            if not reindex.get("ok"):
                err = str(reindex.get("error") or "post-edit index refresh failed")
                return edit_result(
                    content_blocks=[
                        TextContent(
                            type="text",
                            text=f"❌ insert_lines: `{path}` @ L{before_line} — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                        ),
                    ],
                    structured={"ok": False, "error": err, "path": str(result.get("path") or path)},
                    project_root=project_root,
                    tool_name="ai_insert_lines",
                    started_at=_t0_insert,
                )
            blocks = render_insert_lines(
                path=str(result.get("path") or path),
                start_line=before_line,
                inserted_content=content or "",
                lines_added=int(result.get("lines_added") or 0),
            )
            return edit_result(
                content_blocks=blocks,
                structured={"ok": True},
                project_root=project_root,
                tool_name="ai_insert_lines",
                started_at=_t0_insert,
            )
        err = str(result.get("error") or "insert_lines failed")
        return edit_result(
            content_blocks=[
                TextContent(
                    type="text", text=f"❌ insert_lines: `{path}` @ L{before_line} — {err}"
                ),
            ],
            structured={"ok": False, "error": err, "path": path},
            project_root=project_root,
            tool_name="ai_insert_lines",
            started_at=_t0_insert,
        )

    # Internal helper. Tool surface removed — ai_replace(mode='lines')
    # (granted-only) and ai_replace(mode='symbol') (auto-derived span)
    # are the public doors. Body kept callable from ai_replace dispatch.
    def ai_edit_lines(
        path: str,
        start_line: int,
        end_line: int,
        new_content: str,
        expect: str | None = None,
        dry_run: bool = False,
        mode: str = "auto",
        config_edit_mode: str | None = None,
    ) -> Any:
        _t0_edit = time.perf_counter()
        """Last-resort line-range edit. PREFER content-matched edits FIRST.

        Better tools for most edits:
        - `ai_str_replace` / `ai_batch_str_replace` — match on content,
          not line numbers. Immune to line-drift, survives concurrent
          edits, works across turns.
        - `ai_anchor_replace` — content-addressed block replacement,
          ideal when the target is distinctive but larger than str_replace
          can handle cleanly.
        - `ai_batch_edit` — multiple line-ranged edits in one call,
          sorted bottom-up internally so line numbers stay consistent.

        Use `ai_edit_lines` only when:
        - Target is unambiguous by line range AND not by content
          (e.g., trailing whitespace cleanup, reformatting a known range)
        - None of str_replace / anchor_replace / batch_edit can express it
        - You've read the target range in this session

        HARD LIMITS: Only one `ai_edit_lines` / `ai_insert_lines` call
        per file per turn. Sequential line-based edits corrupt line
        numbers. Use `ai_batch_edit` for multi-edit runs. Resets on
        next user prompt. Requires prior read of the target range."""
        project_root = resolve_project_root()
        if not dry_run:
            gate = require_active_task(hub, project_root, "ai_edit_lines")
            if gate is not None:
                return gate
            rejection = _check_turn_edited(project_root, path, "ai_edit_lines")
            if rejection is not None:
                return rejection
            # Edit-must-have-read: refuse if the agent hasn't called
            # ai_get_lines covering [start_line, end_line] on this
            # file in the current session. Stops "read line 1, edit
            # lines 50-300" — agents can only modify what they've
            # actually seen. Dry runs exempt (they're previews).
            covered, reason = _lines_were_read(project_root, path, int(start_line), int(end_line))
            if not covered:
                return edit_result(
                    content_blocks=[
                        TextContent(
                            type="text",
                            text=f"❌ edit_lines refused: {reason}",
                        ),
                    ],
                    structured={"ok": False, "error": reason, "path": path},
                    project_root=project_root,
                    tool_name="ai_edit_lines",
                    started_at=_t0_edit,
                )
        result = file_edit_lines(
            project_root,
            path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content,
            expect=expect,
            dry_run=dry_run,
            mode=mode,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success") and not result.get("dry_run"):
            # evict_known_path=True per king directive 2026-05-12: line-range
            # edit shifts everything after start_line. Forces re-read.
            reindex = post_edit_reindex_and_grant(
                hub,
                project_root,
                "ai_edit_lines",
                str(result.get("path") or path),
                evict_known_path=True,
            )
            if not reindex.get("ok"):
                err = str(reindex.get("error") or "post-edit index refresh failed")
                return edit_result(
                    content_blocks=[
                        TextContent(
                            type="text",
                            text=f"❌ edit_lines: `{path}` L{start_line}-L{end_line} — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                        ),
                    ],
                    structured={"ok": False, "error": err, "path": str(result.get("path") or path)},
                    project_root=project_root,
                    tool_name="ai_edit_lines",
                    started_at=_t0_edit,
                )
        if not result.get("success"):
            err = str(result.get("error") or "edit_lines failed")
            fail_blocks: list[TextContent] = [
                TextContent(
                    type="text",
                    text=f"❌ edit_lines: `{path}` L{start_line}-L{end_line} — {err}",
                ),
            ]
            # If the engine returned a diff preview (expect mismatch), show it.
            if result.get("old_content"):
                fail_blocks.append(TextContent(type="text", text="current (expected mismatch):"))
                fail_blocks.append(TextContent(type="text", text="```"))
                for ln in str(result.get("old_content") or "").splitlines()[:20]:
                    fail_blocks.append(TextContent(type="text", text=ln))
                fail_blocks.append(TextContent(type="text", text="```"))
            return edit_result(
                content_blocks=fail_blocks,
                structured={"ok": False, "error": err, "path": path},
                project_root=project_root,
                tool_name="ai_edit_lines",
                started_at=_t0_edit,
            )
        # Dual-channel: TextContent blocks render the diff in the host
        # UI for the user; structured_content stays minimal so the
        # agent sees only an ok/dry_run ack.
        blocks = render_edit_diff(
            path=str(result.get("path") or path),
            start_line=int(result.get("start_line") or start_line),
            end_line=int(result.get("end_line") or end_line),
            old_content=str(result.get("old_content") or ""),
            new_content=str(result.get("new_content") or ""),
            lines_removed=int(result.get("lines_removed") or 0),
            lines_added=int(result.get("lines_added") or 0),
            dry_run=bool(result.get("dry_run")),
        )
        return edit_result(
            content_blocks=blocks,
            structured={"ok": True, "dry_run": bool(result.get("dry_run"))},
            project_root=project_root,
            tool_name="ai_edit_lines",
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Batch Edit",
        },
    )
    def ai_batch_edit(
        edits: list[dict[str, Any]],
        mode: Literal["line", "string"] = "line",
        dry_run: bool = False,
        atomic: bool = True,
        config_edit_mode: str | None = None,
        large_batch_confirm: bool = False,
        ack_memory_ids: list[int] | None = None,
        ack_drawer_ids: list[str] | None = None,
    ) -> Any:
        _t0_batch = time.perf_counter()
        """Apply multiple edits atomically across one or more files.

        mode='line'   — line-range edits: [{path, edits:[{start_line,...}]}].
        mode='string' — string-match replacements: [{path, old_string, new_string}].

        Batches above 10 edits require large_batch_confirm=True as a
        guard against typo-amplification (e.g. wrong search pattern
        affecting far more sites than intended).
        """
        # mode='string' reuses the string-match batch engine (was the standalone
        # ai_batch_str_replace tool, now folded in — one batch tool, two modes).
        if mode == "string":
            return ai_batch_str_replace(
                edits,
                atomic=atomic,
                config_edit_mode=config_edit_mode,
                large_batch_confirm=large_batch_confirm,
                ack_memory_ids=ack_memory_ids,
                ack_drawer_ids=ack_drawer_ids,
            )
        project_root = resolve_project_root()
        if not dry_run:
            gate = require_active_task(hub, project_root, "ai_batch_edit")
            if gate is not None:
                return gate
        if not dry_run:
            batch_paths = [str(e.get("path") or "") for e in (edits or []) if isinstance(e, dict)]
            mem_gate = _check_anchored_memory(
                project_root,
                paths=batch_paths,
                ack_memory_ids=ack_memory_ids,
                ack_drawer_ids=ack_drawer_ids,
                tool_name="ai_batch_edit",
            )
            if mem_gate is not None:
                return mem_gate
        result = file_batch_edit(
            project_root,
            edits,
            dry_run=dry_run,
            atomic=atomic,
            config_edit_mode=config_edit_mode,
            large_batch_confirm=large_batch_confirm,
        )
        if result.get("success") and not dry_run:
            for item in result.get("results", []):
                if isinstance(item, dict) and item.get("success"):
                    reindex = post_edit_reindex_and_grant(
                        hub,
                        project_root,
                        "ai_batch_edit",
                        str(item.get("path") or ""),
                    )
                    if not reindex.get("ok"):
                        err = str(reindex.get("error") or "post-edit index refresh failed")
                        return edit_result(
                            content_blocks=[
                                TextContent(
                                    type="text",
                                    text=f"❌ batch_edit: `{item.get('path') or ''!s}` — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                                ),
                            ],
                            structured={
                                "ok": False,
                                "error": err,
                                "path": str(item.get("path") or ""),
                            },
                            project_root=project_root,
                            tool_name="ai_batch_edit",
                            started_at=_t0_batch,
                        )
        items = result.get("results") or []

        items = result.get("results") or []
        failures = [
            {
                "path": str((it or {}).get("path") or ""),
                "err": str((it or {}).get("error") or "failed"),
            }
            for it in items
            if isinstance(it, dict) and not it.get("success")
        ]
        if result.get("success") and not failures:
            # Build per-edit summary for host UI, terse ack for agent.
            total_removed = sum(
                int((it or {}).get("lines_removed") or 0) for it in items if isinstance(it, dict)
            )
            total_added = sum(
                int((it or {}).get("lines_added") or 0) for it in items if isinstance(it, dict)
            )
            blocks = render_batch_edit_summary(
                edits=[it for it in items if isinstance(it, dict)],
                total_removed=total_removed,
                total_added=total_added,
                dry_run=dry_run,
            )
            return edit_result(
                content_blocks=blocks,
                structured={"ok": True, "count": len(items), "dry_run": dry_run},
                project_root=project_root,
                tool_name="ai_batch_edit",
            )
        # Failure path — render same way as success, just with ✗ header
        # and per-file failure rows. edit_result flips to ok=False when
        # structured.ok is False, so the pad footer shows "tool ✗ · ...".
        # When len(failures) == 0 and !success, surface real error string
        # instead of "0 of 0 edits failed" (atomic-rollback / syntax-check /
        # top-level rejection where backing fn returned no per-edit results).
        if not failures:
            top_error = str(result.get("error") or "batch rejected, no per-edit failures reported")
            stage = str(result.get("stage") or result.get("reason") or "batch.unknown")
            return edit_result(
                content_blocks=[
                    TextContent(
                        type="text",
                        text=f"❌ batch_edit: {top_error}",
                    ),
                ],
                structured={
                    "ok": False,
                    "stage": stage,
                    "reason": stage,
                    "error": top_error,
                },
                project_root=project_root,
                tool_name="ai_batch_edit",
                started_at=_t0_batch,
            )
        fail_blocks: list[TextContent] = [
            TextContent(
                type="text",
                text=f"❌ batch_edit: {len(failures)} of {len(items)} edits failed",
            ),
        ]
        for f in failures[:20]:
            fail_blocks.append(
                TextContent(
                    type="text",
                    text=f"  ✗ `{f.get('path', '?')}`: {f.get('err', 'unknown')}",
                ),
            )
        if len(failures) > 20:
            fail_blocks.append(
                TextContent(
                    type="text",
                    text=f"  … {len(failures) - 20} more failures",
                ),
            )
        return edit_result(
            content_blocks=fail_blocks,
            structured={
                "ok": False,
                "error": f"{len(failures)} of {len(items)} edits failed",
                "failures": failures[:10],
            },
            project_root=project_root,
            tool_name="ai_batch_edit",
            started_at=_t0_batch,
        )

    # Internal helper (king directive 2026-05-12: "no aliases").
    # Tool surface removed — ai_replace(mode='string') is the only door.
    # Body kept callable because ai_replace dispatches into it.
    def ai_str_replace(
        path: str,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        config_edit_mode: str | None = None,
        old_str: str | None = None,
        new_str: str | None = None,
    ) -> Any:
        _t0_sr = time.perf_counter()
        """String-match edit. Match on content (old_string), unique unless replace_all=True.

        old_string capped at 1000 chars for context hygiene; new_string is
        uncapped (replacing 30 lines with 1500 lines is one call). Prefer
        ai_edit_lines for blocks over the cap; ai_batch_str_replace
        when the SAME old_str needs replacing in many files.

        Parameter names old_string/new_string match the Edit tool. old_str/new_str
        accepted as legacy aliases so existing callers keep working.
        """
        from .mcp_server_runtime_helpers import resolve_project_root

        resolved_old = old_string if old_string is not None else old_str
        resolved_new = new_string if new_string is not None else new_str
        if resolved_old is None:
            return {"success": False, "path": path, "error": "old_string is required"}
        if resolved_new is None:
            return {"success": False, "path": path, "error": "new_string is required"}
        project_root = resolve_project_root()
        gate = require_active_task(hub, project_root, "ai_str_replace")
        if gate is not None:
            return gate
        # Edit-must-have-read (loose): the file must have been read in
        # this session via ai_get_lines / ai_get_symbol_snippet /
        # ai_bundle. Stops blind str-replace on files the agent has
        # never opened. The exact line range can't be checked here
        # (the engine matches old_string at runtime), so this is the
        # weaker sibling of _lines_were_read used by ai_edit_lines.
        covered, reason = _file_was_read(project_root, path)
        if not covered:
            return edit_result(
                content_blocks=[
                    TextContent(
                        type="text",
                        text=f"❌ str_replace refused: {reason}",
                    ),
                ],
                structured={"ok": False, "error": reason, "path": path},
                project_root=project_root,
                tool_name="ai_str_replace",
                started_at=_t0_sr,
            )
        result = file_str_replace(
            project_root,
            path,
            resolved_old,
            resolved_new,
            replace_all=replace_all,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            reindex = post_edit_reindex_and_grant(
                hub,
                project_root,
                "ai_str_replace",
                str(result.get("path") or path),
            )
            if not reindex.get("ok"):
                err = str(reindex.get("error") or "post-edit index refresh failed")
                return edit_result(
                    content_blocks=[
                        TextContent(
                            type="text",
                            text=f"❌ str_replace: `{path}` — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                        ),
                    ],
                    structured={"ok": False, "error": err, "path": str(result.get("path") or path)},
                    project_root=project_root,
                    tool_name="ai_str_replace",
                    started_at=_t0_sr,
                )
        if not result.get("success"):
            # Rich failure: show the failed old_str preview + reason
            err = str(result.get("error") or "match failed")
            preview_lines = (resolved_old or "").splitlines()[:10]
            fail_blocks: list[TextContent] = [
                TextContent(
                    type="text",
                    text=f"❌ str_replace: `{path}` — {err}",
                ),
                TextContent(type="text", text="old_str (no match):"),
                TextContent(type="text", text="```"),
            ]
            for ln in preview_lines:
                fail_blocks.append(TextContent(type="text", text=ln))
            if len((resolved_old or "").splitlines()) > 10:
                fail_blocks.append(
                    TextContent(
                        type="text",
                        text=f"… {len((resolved_old or '').splitlines()) - 10} more lines",
                    ),
                )
            fail_blocks.append(TextContent(type="text", text="```"))
            return edit_result(
                content_blocks=fail_blocks,
                structured={"ok": False, "error": err, "path": path},
                project_root=project_root,
                tool_name="ai_str_replace",
                started_at=_t0_sr,
            )
        blocks = render_str_replace_diff(
            path=str(result.get("path") or path),
            old_str=resolved_old or "",
            new_str=resolved_new or "",
            match_count=int(result.get("replacements") or result.get("match_count") or 1),
        )
        return edit_result(
            content_blocks=blocks,
            structured={"ok": True},
            project_root=project_root,
            tool_name="ai_str_replace",
            started_at=_t0_sr,
        )

    # Internal helper. Tool surface removed — ai_replace(mode='anchor')
    # is the only door. Body kept callable from ai_replace dispatch.
    def ai_anchor_replace(
        path: str,
        start_anchor: str,
        replacement: str,
        end_anchor: str,
        allow_partial_anchors: Annotated[
            bool,
            Field(description="expert/debug escape only"),
        ] = False,
        config_edit_mode: str | None = None,
    ) -> Any:
        _t0_ar = time.perf_counter()
        """Anchor-only span replace (king doctrine 2026-05-01).

        Replaces the content STRICTLY BETWEEN start_anchor and
        end_anchor with `replacement`. Anchors persist; middle is
        replaced. No `target` argument — the agent does not ship the
        old middle content. Bytes shipped: path + 2 anchors + new
        body, independent of replaced span size.

        Anchors must form exactly one unambiguous (start, end) pair in
        the file. Multiple pairings → refused; choose more specific
        anchor strings.

          Partial-line anchors are refused by default (2026-05-26) —
          each anchor must start and end at a line boundary.
          allow_partial_anchors=True is an expert/debug escape only —
          do NOT use for normal content edits; prefer mode='string' for
          inline tweaks.
          """
        from .mcp_server_runtime_helpers import resolve_project_root

        project_root = resolve_project_root()
        gate = require_active_task(hub, project_root, "ai_anchor_replace")
        if gate is not None:
            return gate
        covered, reason = _file_was_read(project_root, path)
        if not covered:
            return edit_result(
                content_blocks=[
                    TextContent(type="text", text=f"❌ anchor_replace refused: {reason}"),
                ],
                structured={"ok": False, "error": reason, "path": path},
                project_root=project_root,
                tool_name="ai_anchor_replace",
                started_at=_t0_ar,
            )
        result = anchor_replace(
            project_root,
            path,
            start_anchor=start_anchor,
            replacement=replacement,
            end_anchor=end_anchor,
            allow_partial_anchors=allow_partial_anchors,
            config_edit_mode=config_edit_mode,
        )
        if result.get("success"):
            reindex = post_edit_reindex_and_grant(
                hub,
                project_root,
                "ai_anchor_replace",
                str(result.get("path") or path),
            )
            if not reindex.get("ok"):
                err = str(reindex.get("error") or "post-edit index refresh failed")
                return edit_result(
                    content_blocks=[
                        TextContent(
                            type="text",
                            text=f"❌ anchor_replace: `{path}` — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                        ),
                    ],
                    structured={"ok": False, "error": err, "path": str(result.get("path") or path)},
                    project_root=project_root,
                    tool_name="ai_anchor_replace",
                    started_at=_t0_ar,
                )
        if not result.get("success"):
            err = str(result.get("error") or "anchor_replace failed")
            return edit_result(
                content_blocks=[
                    TextContent(type="text", text=f"❌ anchor_replace: `{path}` — {err}"),
                ],
                structured={"ok": False, "error": err, "path": path},
                project_root=project_root,
                tool_name="ai_anchor_replace",
                started_at=_t0_ar,
            )
        old_span = str(result.get("old_span") or "")
        blocks = render_str_replace_diff(
            path=str(result.get("path") or path),
            old_str=old_span or "<anchored span>",
            new_str=replacement,
            match_count=int(result.get("replacements") or result.get("match_count") or 1),
        )
        return edit_result(
            content_blocks=blocks,
            structured={"ok": True},
            project_root=project_root,
            tool_name="ai_anchor_replace",
            started_at=_t0_ar,
        )

    @modes(
        string={
            "required": ["old_string", "new_string"],
            "optional": ["replace_all", "config_edit_mode"],
            "desc": "old_string→new_string; old_string ≤200 chars (over that use anchor)",
        },
        anchor={
            "required": ["start_anchor", "end_anchor", "replacement"],
            "optional": ["config_edit_mode", "allow_partial_anchors"],
            "desc": "PRIMARY surgical edit: replaces BETWEEN two unique anchor strings (anchors stay in the file)",
        },
        symbol={
            "required": ["symbol", "new_body"],
            "optional": ["config_edit_mode"],
            "desc": "symbol='Class.method' → replace its whole body (index resolves the span)",
        },
        lines={
            "required": ["start_line", "end_line", "new_content"],
            "optional": ["config_edit_mode"],
            "desc": "replace start_line..end_line; file must be re-read before the next line edit",
        },
    )
    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Replace (Unified)",
        },
    )
    def ai_replace(
        mode: str,
        path: str,
        # mode="string" inputs (capped at 200 chars on old_string)
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        # mode="anchor" inputs (anchors-only span replace)
        start_anchor: str | None = None,
        replacement: str | None = None,
        end_anchor: str | None = None,
        allow_partial_anchors: Annotated[
            bool,
            Field(description="expert/debug escape only"),
        ] = False,
        # mode="symbol" inputs (index-resolved symbol body rewrite)
        symbol: str | None = None,
        new_body: str | None = None,
        # mode="lines" inputs (granted-only; line-range edit)
        start_line: int | None = None,
        end_line: int | None = None,
        new_content: str | None = None,
        # Common
        config_edit_mode: str | None = None,
        # Anchored-memory gate: ack list of route_ids known to apply
        # to this edit's target. The gate refuses if any anchored
        # memory on the symbol/file isn't in this list.
        ack_memory_ids: list[int] | None = None,
        # RFC-4 Phase B: palace drawer acks for exact_symbol /
        # operator_pinned anchors.
        ack_drawer_ids: list[str] | None = None,
    ) -> Any:
        """Unified replace — one tool, four modes (king doctrine 2026-05-01).

        PRIMARY CHOICE for surgical edits — mode='anchor':
          Best tool when used well. Args: start_anchor, replacement,
          end_anchor. Anchors persist (their text stays in the file),
          only the content BETWEEN them is replaced. Ships zero "old
          content" — bytes = path + 2 anchors + new body. Survives
          drift, no line numbers, no full-block paste. Pick stable
          anchor strings near the edit site (a unique import line
          before, a stable closing brace after, etc.). Partial-line
           anchors are refused by default; set allow_partial_anchors=True
           (expert/debug escape only) to bypass.

        For trivial tweaks (≤200 chars) — mode='string':
          Args: old_string, new_string, [replace_all]. Hard 200-char
          cap on old_string. Over that → use mode='anchor'.

        For replacing a whole symbol/function body — mode='symbol':
          Args: symbol (qualified, e.g. 'ClassName.method'), new_body.
          Index resolves the span; no anchors, no line numbers needed.

        For line-range edits — mode='lines':
          Args: start_line, end_line, new_content. After success the
          file is EVICTED from known_exact_paths, forcing a re-read
          before the next line operation (line numbers shift drastically).

        For MANY edits at once (one or several files):
          - ai_batch_str_replace([{path, old_string, new_string}, ...])
            — atomic, bottom-up. Best for cross-file token sweeps and
            many small in-file edits.
          - ai_batch_edit([{path, start_line, end_line, new_content}, ...])
            — atomic line edits, bottom-up.

                Less tools with more uses = happier populace.
        """
        from .mcp_server_runtime_helpers import resolve_project_root

        project_root = resolve_project_root()
        m = (mode or "").strip().lower()
        # Resolve symbol_name for memory gate: only meaningful for
        # mode='symbol'. Other modes gate by file_path alone.
        _gate_symbol = ""
        if m == "symbol" and symbol:
            _ts = symbol.strip()
            if "." in _ts:
                _gate_symbol = _ts.rsplit(".", 1)[1]
            elif "::" in _ts:
                _gate_symbol = _ts.rsplit("::", 1)[1]
            else:
                _gate_symbol = _ts
        mem_gate = _check_anchored_memory(
            project_root,
            paths=[path],
            symbol_name=_gate_symbol,
            ack_memory_ids=ack_memory_ids,
            ack_drawer_ids=ack_drawer_ids,
            tool_name="ai_replace",
        )
        if mem_gate is not None:
            return mem_gate

        if m == "string":
            return ai_str_replace(
                path=path,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
                config_edit_mode=config_edit_mode,
            )
        if m == "anchor":
            if not start_anchor or not end_anchor or replacement is None:
                return {
                    "ok": False,
                    "error": (
                        "ai_replace(mode='anchor') requires start_anchor, "
                        "end_anchor, and replacement"
                    ),
                }
            return ai_anchor_replace(
                path=path,
                start_anchor=start_anchor,
                replacement=replacement,
                end_anchor=end_anchor,
                allow_partial_anchors=allow_partial_anchors,
                config_edit_mode=config_edit_mode,
            )
        if m == "symbol":
            if not symbol or new_body is None:
                return {
                    "ok": False,
                    "error": (
                        "ai_replace(mode='symbol') requires symbol (qualified name) and new_body"
                    ),
                }
            # Resolve symbol span via the code index, then delegate to
            # line-range edit. Index is the source of truth for the
            # symbol's address - no anchors, no old content shipped.
            #
            # Bug history (2026-05-02 fix): the prior implementation
            # called hub.code.get_outline() and tried (outline or {}).get
            # ("symbols") - but get_outline returns list[dict] directly,
            # so non-empty results raised "list has no .get" AttributeError.
            # It also assumed dict fields named qualified_name / start_line
            # / end_line, none of which exist (real fields: symbol /
            # line_number / no end at all). Fixed by calling get_symbol_
            # snippet (correct API) and deriving end_line via AST for
            # Python or snippet-line-count for other languages.
            target_sym = (symbol or "").strip()
            # Qualified-name routing (2026-05-03 polish, king directive
            # "do more with less"): parse Class.method / ns::Class::method
            # into (qualifier, leaf), then filter the file's outline by
            # symbol == leaf AND container == qualifier. Eliminates the
            # prior pickaxe behavior of "strip to bare leaf, take first
            # match" — which silently chose the wrong symbol when the
            # leaf name (run, __init__, handle, etc.) repeated across
            # multiple containers in the same file.
            #
            # Parse: split on the last "." OR "::" boundary. Anything
            # before is the immediate container hint; the leaf is the
            # actual symbol name.
            if "." in target_sym:
                _qual, bare_sym = target_sym.rsplit(".", 1)
                qualifier = _qual.rsplit(".", 1)[-1].rsplit("::", 1)[-1].strip()
            elif "::" in target_sym:
                _qual, bare_sym = target_sym.rsplit("::", 1)
                qualifier = _qual.rsplit("::", 1)[-1].rsplit(".", 1)[-1].strip()
            else:
                bare_sym = target_sym
                qualifier = ""
            # Outline filter: collect all rows matching the leaf, then
            # narrow by container if qualifier was given.
            try:
                outline = hub.code.get_outline(project_root, path)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): could not read outline for {path}: {exc}"
                    ),
                }
            entries = [e for e in (outline or []) if str(e.get("symbol") or "").strip() == bare_sym]
            if not entries:
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): symbol "
                        f"'{target_sym}' not found in {path}. Use "
                        f"ai_get_outline or ai_find to discover the "
                        f"qualified name."
                    ),
                }
            if qualifier:
                container_matches = [
                    e for e in entries if str(e.get("container") or "").strip() == qualifier
                ]
                if not container_matches:
                    available_containers = sorted(
                        {str(e.get("container") or "<top-level>") for e in entries},
                    )
                    return {
                        "ok": False,
                        "error": (
                            f"ai_replace(mode='symbol'): '{target_sym}' "
                            f"not found in {path} - leaf '{bare_sym}' "
                            f"exists but not in container '{qualifier}'. "
                            f"Available containers: {available_containers}"
                        ),
                    }
                entries = container_matches
            if len(entries) > 1:
                line_numbers = sorted(int(e.get("line_number") or 0) for e in entries)
                disambig = (
                    f"qualify with container (e.g. '<container>.{bare_sym}')"
                    if not qualifier
                    else "leaf name repeats inside the same container"
                )
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): '{target_sym}' "
                        f"matches {len(entries)} symbols in {path} at "
                        f"lines {line_numbers}. Disambiguate: {disambig}."
                    ),
                }
            # Resolved to exactly one entry. Now fetch the snippet
            # (gives us start_line + language + body for AST span work).
            try:
                info = hub.code.get_symbol_snippet(
                    project_root,
                    path,
                    bare_sym,
                    line_number=int(entries[0].get("line_number") or 0),
                )
            except FileNotFoundError:
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): symbol "
                        f"'{target_sym}' resolved in outline but "
                        f"snippet fetch failed in {path}."
                    ),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): could not resolve "
                        f"'{target_sym}' in {path}: {exc}"
                    ),
                }
            sl = int(info.get("line_number") or 0)
            if sl <= 0:
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): index returned "
                        f"invalid line_number for '{target_sym}' in "
                        f"{path}; try ai_index_sync."
                    ),
                }
            language = str(info.get("language") or "").strip().lower()
            snippet = str(info.get("snippet") or "")
            el = sl  # default: single-line replacement
            # Tree-sitter span resolution: works for all 12 languages
            # the indexer supports (python, js/ts/jsx/tsx, c#, go, rust,
            # java, html, css, scss, dart). King directive 2026-05-02:
            # "AST should always hold their hand and point to the error" -
            # no language is left without span resolution.
            try:
                from pathlib import Path as _Path

                from .tree_sitter_service import find_symbol_span

                abs_path = project_root / path
                file_text = abs_path.read_text(encoding="utf-8", errors="ignore")
                ts_span = find_symbol_span(
                    _Path(path),
                    file_text,
                    bare_sym,
                    sl,
                )
                if ts_span is not None:
                    sl, el = ts_span
            except Exception:
                pass
            # Python builtin AST fallback (more battle-tested for Python
            # than tree-sitter for some edge cases like decorator stacks).
            # Only runs if tree-sitter didn't resolve the span.
            if el == sl and language == "python":
                try:
                    import ast as _ast

                    abs_path = project_root / path
                    file_text = abs_path.read_text(encoding="utf-8", errors="ignore")
                    tree = _ast.parse(file_text)
                    target_name = bare_sym
                    found_end = None
                    for node in _ast.walk(tree):
                        if isinstance(
                            node,
                            (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef),
                        ):
                            if node.name == target_name and getattr(node, "lineno", 0) == sl:
                                found_end = getattr(node, "end_lineno", None)
                                break
                    if found_end is None:
                        for node in _ast.walk(tree):
                            if isinstance(
                                node,
                                (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef),
                            ):
                                ln = getattr(node, "lineno", 0)
                                en = getattr(node, "end_lineno", ln)
                                if node.name == target_name and ln <= sl <= en:
                                    found_end = en
                                    sl = ln
                                    break
                    if found_end:
                        el = int(found_end)
                except SyntaxError:
                    pass
                except Exception:
                    pass
            # Last resort: derive from snippet line count (works for
            # languages without tree-sitter grammars + Python with
            # syntax errors that ast can't parse).
            if el == sl and snippet:
                snippet_lines = snippet.splitlines()
                if snippet_lines:
                    el = sl + len(snippet_lines) - 1
            if el < sl:
                return {
                    "ok": False,
                    "error": (
                        f"ai_replace(mode='symbol'): could not derive "
                        f"end_line for '{target_sym}' in {path}; index "
                        f"may need ai_index_sync."
                    ),
                }
            return ai_edit_lines(
                path=path,
                start_line=sl,
                end_line=el,
                new_content=new_body,
                config_edit_mode=config_edit_mode,
            )
        if m == "lines":
            if start_line is None or end_line is None or new_content is None:
                return {
                    "ok": False,
                    "error": (
                        "ai_replace(mode='lines') requires start_line, end_line, and new_content"
                    ),
                }
            return ai_edit_lines(
                path=path,
                start_line=start_line,
                end_line=end_line,
                new_content=new_content,
                config_edit_mode=config_edit_mode,
            )
        return {
            "ok": False,
            "error": (
                f"ai_replace: unknown mode '{mode}'. "
                f"Valid: 'string' | 'anchor' | 'symbol' | 'lines'."
            ),
        }

    # Folded into ai_batch_edit(mode="string"). no longer a standalone registered tool —
    # an internal helper ai_batch_edit dispatches to. The canonical surface is the
    # single ai_batch_edit tool (mode=line|string); no standalone advertisement.
    def ai_batch_str_replace(
        edits: list[dict[str, Any]],
        atomic: bool = True,
        config_edit_mode: str | None = None,
        large_batch_confirm: bool = False,
        ack_memory_ids: list[int] | None = None,
        ack_drawer_ids: list[str] | None = None,
    ) -> Any:
        _t0_bsr = time.perf_counter()
        """Multiple string-match replacements across files, atomic.

        Batches above 10 edits require large_batch_confirm=True as a
        guard against typo-amplification (wrong old_str catching far
        more occurrences than intended).
        """
        project_root = resolve_project_root()
        gate = require_active_task(hub, project_root, "ai_batch_str_replace")
        if gate is not None:
            return gate
        batch_paths = [str(e.get("path") or "") for e in (edits or []) if isinstance(e, dict)]
        mem_gate = _check_anchored_memory(
            project_root,
            paths=batch_paths,
            ack_memory_ids=ack_memory_ids,
            ack_drawer_ids=ack_drawer_ids,
            tool_name="ai_batch_str_replace",
        )
        if mem_gate is not None:
            return mem_gate
        result = file_batch_str_replace(
            project_root,
            edits,
            atomic=atomic,
            config_edit_mode=config_edit_mode,
            large_batch_confirm=large_batch_confirm,
        )
        if result.get("success"):
            for item in result.get("results", []):
                if isinstance(item, dict) and item.get("success"):
                    reindex = post_edit_reindex_and_grant(
                        hub,
                        project_root,
                        "ai_batch_str_replace",
                        str(item.get("path") or ""),
                    )
                    if not reindex.get("ok"):
                        err = str(reindex.get("error") or "post-edit index refresh failed")
                        return edit_result(
                            content_blocks=[
                                TextContent(
                                    type="text",
                                    text=f"❌ batch_str_replace: `{item.get('path') or ''!s}` — file modified. index refresh fail: {err}. Indexed reads may be stale.",
                                ),
                            ],
                            structured={
                                "ok": False,
                                "error": err,
                                "path": str(item.get("path") or ""),
                            },
                            project_root=project_root,
                            tool_name="ai_batch_str_replace",
                            started_at=_t0_bsr,
                        )
        # Same terse batch envelope as ai_batch_edit.
        items = result.get("results") or []
        failures = [
            {
                "path": str((it or {}).get("path") or ""),
                "err": str((it or {}).get("error") or "failed"),
            }
            for it in items
            if isinstance(it, dict) and not it.get("success")
        ]
        if result.get("success") and not failures:
            # Build per-edit diffs — each edit in the input carries the
            # old_str/new_str the user needs to see. Pair against the
            # successful results for match counts + canonical paths.
            blocks: list[TextContent] = [
                TextContent(
                    type="text",
                    text=f"📝 batch str_replace: {len(items)} edits",
                ),
            ]
            MAX_EDITS_TO_DIFF = 10
            for i, (inp, it) in enumerate(zip(edits, items)):
                if i >= MAX_EDITS_TO_DIFF:
                    break
                if not isinstance(inp, dict):
                    continue
                p = str((it or {}).get("path") or inp.get("path") or "?")
                old_s = str(inp.get("old_str") or inp.get("old_string") or "")
                new_s = str(inp.get("new_str") or inp.get("new_string") or "")
                reps = int((it or {}).get("replacements") or (it or {}).get("match_count") or 1)
                diff_blocks = render_str_replace_diff(
                    path=p,
                    old_str=old_s,
                    new_str=new_s,
                    match_count=reps,
                )
                blocks.extend(diff_blocks)
            if len(edits) > MAX_EDITS_TO_DIFF:
                blocks.append(
                    TextContent(
                        type="text",
                        text=f"… {len(edits) - MAX_EDITS_TO_DIFF} more edits (showing first {MAX_EDITS_TO_DIFF})",
                    ),
                )
            return edit_result(
                content_blocks=blocks,
                structured={"ok": True, "count": len(items)},
                project_root=project_root,
                tool_name="ai_batch_str_replace",
            )
        # Detect Phase-2 final-syntax failure vs Phase-1 match failures.
        is_final_syntax = (
            not result.get("success") and not failures and result.get("stage") == "final_syntax"
        )
        if is_final_syntax:
            err = result.get("error", "syntax validation failed")
            fail_blocks = [
                TextContent(
                    type="text",
                    text=f"❌ batch_str_replace: combined edits would create invalid final syntax — {err}\nAll edits rejected atomically.",
                ),
            ]
            return edit_result(
                content_blocks=fail_blocks,
                structured={
                    "ok": False,
                    "stage": "final_syntax",
                    "reason": "batch.syntax_final",
                    "error": err,
                },
                project_root=project_root,
                tool_name="ai_batch_str_replace",
                started_at=_t0_bsr,
            )

        # Phase-1 failure path — per-edit match failures (len(failures) > 0)
        # When len(failures) == 0 and !success, backing fn returned early
        # (atomic-rollback with no per-edit failures or other failure mode).
        # Surface the real error string instead of formatting "0 of 0 failed".
        if not failures:
            top_error = str(result.get("error") or "batch rejected, no per-edit failures reported")
            stage = str(result.get("stage") or result.get("reason") or "batch.unknown")
            return edit_result(
                content_blocks=[
                    TextContent(
                        type="text",
                        text=f"❌ batch_str_replace: {top_error}",
                    ),
                ],
                structured={
                    "ok": False,
                    "stage": stage,
                    "reason": stage,
                    "error": top_error,
                },
                project_root=project_root,
                tool_name="ai_batch_str_replace",
                started_at=_t0_bsr,
            )
        fail_blocks: list[TextContent] = [
            TextContent(
                type="text",
                text=f"❌ batch_str_replace: {len(failures)} of {len(items)} edits failed",
            ),
        ]
        for f in failures[:20]:
            fail_blocks.append(
                TextContent(
                    type="text",
                    text=f"  ✗ `{f.get('path', '?')}`: {f.get('err', 'unknown')}",
                ),
            )
        if len(failures) > 20:
            fail_blocks.append(
                TextContent(
                    type="text",
                    text=f"  … {len(failures) - 20} more failures",
                ),
            )
        return edit_result(
            content_blocks=fail_blocks,
            structured={
                "ok": False,
                "stage": "match",
                "reason": "batch.edit_match",
                "error": f"{len(failures)} of {len(items)} edits failed",
                "failures": failures[:10],
            },
            project_root=project_root,
            tool_name="ai_batch_str_replace",
            started_at=_t0_bsr,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Config Edit Policy",
        },
    )
    def config_edit_policy_get(profile: str = "release") -> dict[str, Any]:
        """Return the release-profile config edit policy visible to agents."""
        return {
            "profile": profile,
            "available_modes": available_config_edit_modes(profile),
            "security": {
                "self_edit_available": self_edit_available_in_profile(profile),
            },
        }

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Protect",
        },
    )
    def ai_protect(
        mode: str,
        path: str = "",
        paths: list[str] | None = None,
        why: str = "",
        pair_files: list[str] | None = None,
        dnt_id: str = "",
        with_dnt: bool = False,
    ) -> dict[str, Any]:
        """DO NOT TOUCH file protection — writes a sentinel header into
        the file AND records the protecting user's identity in the
        SQL registry so only the same user (or admin+) can remove it.

        Modes:
          - "add"    — single path. Writes header + registry row.
                       NO grant needed (2026-06-11): protection is
                       additive safety — the agent picks the file by
                       its own recognition and records `why`. The
                       header only SURFACES restrictions on read,
                       makes editors extra-careful, and fences
                       subagents. The gated direction is REMOVAL.
          - "remove" — single path. Identity-checks the registry:
                       match → strip header + drop registry row.
                       Mismatch → try RBAC permission
                       security.allow_protected_edit (admin-held).
                       Still denied → escalate via escalation_hook
                       so an admin can approve in dashboard.
          - "list"   — no grant needed (read-only). Joins the
                       filesystem sentinel scan with registry rows
                       so entries carry protected_by identity.
                       Pass with_dnt=True to also surface DNT family
                       info (dnt_id, dnt_role) per row.
          - "add_batch" — batch variant of "add" with `paths` list.
          - "sync"   — #62 Phase 2 (canonical 2026-04-28). Walks the
                       project tree, parses any structured DNT header
                       at file head (dnt-id key required), upserts
                       row via record_dnt_header. Returns counts:
                       synced (header found + upserted), skipped (no
                       header), errors (parse/IO failure). Read-only
                       on disk; only the registry mutates.
          - "get"    — #62 Phase 2. Returns full structured DNT
                       registry record. Pass either `path` (looks up
                       by file path) or `dnt_id` (looks up by family
                       id; returns the master row). Returns None when
                       not found.

        The file header is informational (human-readable why +
        pair_files). SQL registry is authoritative for identity
        checks — header is writable, DB isn't (within the managed
        gate). See protected_file_registry_store.py.
        """
        from .identity_resolver import current_user
        from .mcp_server_runtime_helpers import resolve_project_root
        from .protected_file_registry_store import ProtectedFileRegistryStore

        project_root = Path(resolve_project_root())
        m = (mode or "").strip().lower()
        registry = ProtectedFileRegistryStore()

        if m == "list":
            # Agents only need the paths to know what to avoid.
            # Operator-side (dashboard) shows owner / why / pair_files /
            # orphan drift — the registry join is inspectable through
            # the execution_events log, not the agent MCP surface.
            from .protected_file_ops import list_protected_files as _list

            fs_paths = [r["path"] for r in _list(project_root)]
            reg_paths = [r.path for r in registry.list_all(project_root)]
            paths_out = sorted(set(fs_paths) | set(reg_paths))
            if not with_dnt:
                return {"ok": True, "count": len(paths_out), "paths": paths_out}
            # #62 Phase 2: surface DNT family info per row when requested.
            # Each entry: {path, dnt_id, dnt_role}. Legacy ai_protect-add
            # rows return empty dnt_id (correct — they're not in any family).
            entries: list[dict[str, str]] = []
            for p in paths_out:
                fam = registry.find_family_by_path(project_root, p)
                entries.append(
                    {
                        "path": p,
                        "dnt_id": fam[0] if fam else "",
                        "dnt_role": fam[1] if fam else "",
                    },
                )
            return {"ok": True, "count": len(entries), "paths": paths_out, "entries": entries}

        if m == "add":
            if not path:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error("mode='add' requires path")
            from .protected_file_ops import protect_file as _protect

            result = _protect(project_root, path, why=why, pair_files=pair_files)
            if not result.get("success"):
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(result.get("error") or "add failed")
            user_id, _email, _ptype = current_user(project_root)
            machine_id = ""
            try:
                from .host_concurrency_store import machine_id as _m

                machine_id = str(_m() or "")
            except Exception:
                machine_id = ""
            registry.record(
                project_root,
                path=path,
                protected_by_user_id=user_id,
                why=why,
                pair_files=pair_files,
                machine_id=machine_id,
            )
            return {"ok": True, "path": path}

        if m == "remove":
            if not path:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error("mode='remove' requires path")
            # Identity check: current user vs registry protector.
            reg = registry.get(project_root, path)
            user_id, _email, _ptype = current_user(project_root)
            identity_match = reg is not None and reg.protected_by_user_id == user_id
            # Admin override: security.allow_protected_edit unlocks
            # cross-operator removes without escalation.
            has_admin_override = False
            try:
                from .rbac_store import RBACStore

                has_admin_override = RBACStore().user_has_permission(
                    project_root,
                    user_id=user_id,
                    permission="security.allow_protected_edit",
                    scope_type="global",
                    scope_id=None,
                )
            except Exception:
                has_admin_override = False
            if not (identity_match or has_admin_override or reg is None):
                # Mismatch AND no admin override AND registry row
                # exists → escalate. Registry-absent case (reg is None)
                # is a header-only orphan; we let the underlying
                # unprotect_file gate handle it via the per-turn
                # unprotect grant (no identity to check against).
                try:
                    from .escalation_hook import request_escalation

                    session_id = ""
                    try:
                        managed = hub.managed_mode.get_mode(project_root)
                        if managed.get("active"):
                            session_id = str(managed.get("session_id") or "")
                    except Exception:
                        pass
                    esc = request_escalation(
                        project_root,
                        gate_permission="security.allow_protected_edit",
                        gate_phrase=f"unprotect {path}",
                        requester_label=user_id or "operator",
                        requester_user_id=user_id or None,
                        session_id=session_id or None,
                        command_snippet=f"ai_protect(mode='remove', path='{path}')",
                        extra={
                            "path": path,
                            "protected_by_user_id": reg.protected_by_user_id,
                            "protected_at": reg.protected_at,
                        },
                    )
                    # Escalation is the agent's actionable signal —
                    # keep the escalation_id so the agent can relay it
                    # to the operator. Not a terse-ack because "you
                    # can't do this yet" needs the reference id.
                    from .mcp_server_runtime_helpers import _raise_tool_error

                    _raise_tool_error(f"pending_admin_approval (escalation {esc.request_id})")
                except Exception as exc:
                    from .mcp_server_runtime_helpers import _raise_tool_error

                    _raise_tool_error(f"cross-operator unprotect: escalation failed: {exc}")
            from .protected_file_ops import unprotect_file as _unprotect

            result = _unprotect(project_root, path)
            if not result.get("success"):
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(result.get("error") or "remove failed")
            registry.remove(project_root, path)
            return {"ok": True, "path": path, "removed": True}

        if m == "add_batch":
            if not paths:
                return {"ok": False, "err": "mode='add_batch' requires paths list"}
            from .protected_file_ops import protect_files as _batch

            result = _batch(project_root, paths, why=why, pair_files=pair_files)
            user_id, _email, _ptype = current_user(project_root)
            machine_id = ""
            try:
                from .host_concurrency_store import machine_id as _m

                machine_id = str(_m() or "")
            except Exception:
                machine_id = ""
            applied = 0
            refused = 0
            for per in result.get("results") or []:
                if not isinstance(per, dict):
                    continue
                if per.get("success") and per.get("status") == "protected":
                    registry.record(
                        project_root,
                        path=str(per.get("path") or ""),
                        protected_by_user_id=user_id,
                        why=why,
                        pair_files=pair_files,
                        machine_id=machine_id,
                    )
                    applied += 1
                elif not per.get("success"):
                    refused += 1
            # Terse batch ack: applied/refused counts only.
            # Per-file reasons are operator-side (dashboard).
            return {"ok": refused == 0, "applied": applied, "refused": refused}

        if m == "sync":
            # #62 Phase 2: walk project tree, parse structured DNT
            # header at file head, upsert via record_dnt_header.
            # Walker extracted to dnt_registry_sync.sync_dnt_registry
            # (Phoenix, 2026-05-07) so ai_index_sync can auto-trigger
            # it. Same body, single source of truth.
            from .dnt_registry_sync import sync_dnt_registry

            return sync_dnt_registry(project_root, registry=registry)

        if m == "get":
            # #62 Phase 2: lookup full registry record by path or dnt_id.
            def _serialize(r):
                return {
                    "path": r.path,
                    "dnt_id": r.dnt_id,
                    "dnt_role": r.dnt_role,
                    "pair_files": list(r.pair_files),
                    "forbid_list": list(r.forbid_list),
                    "allow_list": list(r.allow_list),
                    "incidents": list(r.incidents),
                    "baseline": r.baseline,
                    "cost": r.cost,
                    "full_header_text": r.full_header_text,
                    "why": r.why,
                    "protected_by_user_id": r.protected_by_user_id,
                    "protected_at": r.protected_at,
                }

            if path:
                rec = registry.get_full(project_root, path)
                if rec is None:
                    return {"ok": False, "err": f"path '{path}' not in registry"}
                return {"ok": True, "record": _serialize(rec)}
            if dnt_id:
                # Try master first (canonical). If absent but the family
                # has satellite rows, fall back to the deterministic-first
                # satellite — the family is real, just master-less. Phase 3
                # banner injection needs this so reads of satellite files
                # surface a banner even when the master file is outside
                # the indexed tree.
                master = registry.get_family_master(project_root, dnt_id)
                members = registry.list_family(project_root, dnt_id)
                if master is None and not members:
                    return {"ok": False, "found": False, "err": f"dnt_id '{dnt_id}' not found"}
                if master is not None:
                    canonical = master
                    master_present = True
                    warning = ""
                else:
                    # list_family orders master-first then path ASC, so
                    # members[0] is the deterministic-first satellite.
                    canonical = members[0]
                    master_present = False
                    warning = "DNT family has satellites but no indexed master"
                resp = {
                    "ok": True,
                    "found": True,
                    "master_present": master_present,
                    "canonical_row": _serialize(canonical),
                    "family_members": [_serialize(m) for m in members],
                    # Back-compat: callers reading the master case via
                    # `record` keep working.
                    "record": _serialize(canonical),
                }
                if warning:
                    resp["warning"] = warning
                return resp
            return {"ok": False, "err": "mode='get' requires path or dnt_id"}

        return {
            "ok": False,
            "err": f"unknown mode '{mode}'. valid: add, remove, list, add_batch, sync, get",
        }
