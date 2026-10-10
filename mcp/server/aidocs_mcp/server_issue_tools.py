"""ai_issues MCP tool registration (#449 War F).

Registration lives HERE (a ``server_*_tools.py`` module) because the outer-gate
manifest discovers tool registrations by the ``server_*_tools.py`` glob —
glob-not-manual-list by design (test_outer_gate_manifest). The service logic
(file_issue / list_issues / verify_issue_hash) stays in
``issue_filing_service.py``; this module is the thin registration shim.
"""

from __future__ import annotations

import time
from typing import Any

from .issue_filing_service import file_issue, list_issues


def register_issue_filing_tools(*, server: Any, hub: Any, runtime: Any) -> None:
    """Register the ai_issues MCP tool (pattern: register_todo_backlog_tools)."""
    del hub, runtime  # uniform registration signature; not needed by v1

    from .dual_audience import fail_edit as _fail_edit
    from .dual_audience import ok_edit as _ok_edit
    from .mcp_server_runtime_helpers import resolve_project_root

    @server.tool(
        annotations={"destructiveHint": False, "openWorldHint": False, "title": "Issue Filing"},
    )
    def ai_issues(
        mode: str,
        content: str = "",
        tags: list[str] | None = None,
        confirm: str = "",
    ) -> Any:
        """Immutable issue filing. Modes: file | list.

        file: writes ONE write-once .MEMORY/issues/<ts>-<hex>.json and
              git-commits ONLY that file on the current branch (no push —
              v1). DEMANDS confirm='file-issue' (literal); without it,
              returns the two-phase prompt and writes nothing.
        list: terse inventory [{issue_id, snippet, created_at, actor}].

        Deliberately requires NO active task: this is the refusal-report
        channel for callers the gate just refused.
        """
        t0 = time.perf_counter()
        project_root = resolve_project_root()

        if mode == "file":
            r = file_issue(project_root, content=content, tags=tags, confirm=confirm)
            if not r.get("ok"):
                extra = {k: v for k, v in r.items() if k not in {"ok", "error"}}
                return _fail_edit(
                    error=str(r.get("error") or "file failed"),
                    tool_name="ai_issues",
                    started_at=t0,
                    extra_structured=extra or None,
                )
            preview = content.strip()[:60]
            committed = "committed" if r.get("committed") else f"NOT committed: {r.get('commit_error', '')}"
            return _ok_edit(
                ack=f"✓ {r['issue_id']}",
                pretty_lines=[f'🧾 issue {r["issue_id"]} filed: "{preview}" [{committed}]'],
                structured={k: v for k, v in r.items() if k != "ok"},
                tool_name="ai_issues",
                started_at=t0,
            )

        if mode == "list":
            items = list_issues(project_root)
            return _ok_edit(
                ack=f"✓ {len(items)} issue(s)",
                pretty_lines=[
                    f"🧾 {it['issue_id']} [{it['created_at']}] {it['actor'] or '(unauthenticated)'}: {it['snippet']}"
                    for it in items
                ] or ["🧾 no issues filed"],
                structured={"issues": items, "count": len(items)},
                tool_name="ai_issues",
                started_at=t0,
            )

        return _fail_edit(
            error=f"unknown mode {mode!r}. Use: file|list",
            tool_name="ai_issues",
            started_at=t0,
        )
