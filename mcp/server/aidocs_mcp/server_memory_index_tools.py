from __future__ import annotations

from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def _inactive_read_authorized(
    project_root: Path,
    session_id: str | None = None,
) -> tuple[bool, str]:
    """True iff an explicit, operator-deliberate authority is active that
    permits reading retired (superseded/removed) memory through the
    agent-facing memory_read tool.

    The conductor agent runs under the operator's identity (principal_type
    'human') and the RBAC role falls back to super_admin on solo installs,
    so neither principal nor role can distinguish "operator deliberately
    auditing" from "ordinary agent flow". The gate therefore requires a
    DASHBOARD-ONLY signal an ordinary agent flow never carries:
      - security.allow_inactive_memory_read (audit/admin unlock), OR
      - dev.dev_mode (debug authority).

    ``security.allow_inactive_memory_read`` is scope=[global,project,session]
    so a session-scoped dashboard unlock is honored — ``session_id`` is
    threaded into its resolution. ``dev.dev_mode`` is project-scoped, so it
    is resolved WITHOUT a session layer (passing one would be meaningless).
    """
    from .config import get_setting

    try:
        if bool(
            get_setting(
                "security.allow_inactive_memory_read",
                project_root=project_root,
                session_id=session_id or None,
                default=False,
            ),
        ):
            return True, "allow_inactive_memory_read"
    except Exception:
        pass
    try:
        from .enforcement import is_dev_flavor

        if bool(is_dev_flavor(project_root)):
            return True, "dev_mode"
    except Exception:
        pass
    return False, ""


def gated_memory_read(
    hub: Any,
    project_root: Path,
    targets: list[str],
    *,
    include_inactive: bool = False,
    session_id: str | None = None,
) -> dict[str, str]:
    """Agent-facing memory read with the authority-gated include_inactive.

    Superseded / removed memory is NEVER returned to an ordinary agent.
    ``include_inactive=True`` is honored only when an operator-deliberate
    authority is active for this ``session_id`` (see
    :func:`_inactive_read_authorized`). Otherwise the flag is ignored
    (retired memory stays suppressed) and an
    ``attempted_inactive_memory_read`` audit event is recorded. The raw
    primitive ``MemoryStore.read_memory(include_inactive=True)`` is the
    ungated internal path; this wrapper is the gate.
    """
    effective_inactive = False
    if include_inactive:
        authorized, _via = _inactive_read_authorized(project_root, session_id)
        if authorized:
            effective_inactive = True
        else:
            try:
                hub.execution.record_event(
                    project_root,
                    event_kind="attempted_inactive_memory_read",
                    source_kind="memory_read",
                    session_id=session_id or None,
                    capability_name="memory_read",
                    action_kind="read",
                    target_entity=",".join(str(t) for t in targets)[:300],
                    status="refused",
                    payload={
                        "targets": [str(t) for t in targets][:50],
                        "include_inactive_requested": True,
                        "reason": "no_audit_authority",
                    },
                )
            except Exception:
                pass
    return hub.memory.read_memory(
        project_root,
        targets,
        include_inactive=effective_inactive,
    )


def register_memory_index_tools(
    *,
    server: Any,
    hub: Any,
    timed_sync: Any,
    timed_indexer: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Read Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("status", title="memory")
    def memory_read(targets: list[str], include_inactive: bool = False) -> Any:
        """Read canonical memory files by target path.

        Superseded / removed memory is NEVER returned to an ordinary agent.
        ``include_inactive=true`` is AUTHORITY-GATED: it is honored only
        when an operator-deliberate signal is active
        (security.allow_inactive_memory_read or dev.dev_mode). Without that
        authority the flag is IGNORED (retired memory stays suppressed) and
        an attempted_inactive_memory_read audit event is recorded.
        """
        root = resolve_project_root()
        # Resolve the current managed session so a SESSION-scoped
        # allow_inactive_memory_read unlock is honored (the flag is
        # scope=[global,project,session]).
        session_id = None
        try:
            managed = hub.managed_mode.get_mode(root)
            if managed.get("active"):
                session_id = str(managed.get("session_id") or "").strip() or None
        except Exception:
            session_id = None
        return gated_memory_read(
            hub,
            root,
            targets,
            include_inactive=include_inactive,
            session_id=session_id,
        )

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Memory Index",
        },
    )
    @timed_indexer
    def index_sync(timeout: int | None = None) -> dict[str, int]:
        """Rebuild the derived SQLite memory/session index from files."""
        return hub.index.sync_all(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Memory Index Status",
        },
    )
    @renders_as("status", title="memory index status")
    def index_status() -> Any:
        """Report current derived index status for the project."""
        return hub.index.status(resolve_project_root())

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Schema Index",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_indexer
    def schema_index_sync(timeout: int | None = None) -> dict[str, int]:
        """Rebuild the derived schema catalog from code and SQL files."""
        return hub.schema.sync_schema(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Search Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("list", title="memory search")
    def memory_search(query: str, limit: int = 10) -> list[dict[str, Any]] | dict[str, Any]:
        """Search the derived memory index by path, title, or body text."""
        # Concrete list|dict annotation (same as ai_search) so FastMCP emits the
        # structured_content={"result": [...]} sidecar for the bare-list return —
        # the outer call_tool wrapper then stamps memory freshness ALONGSIDE the
        # list items (never mutating them), sealing the pretty-OFF list path.
        return hub.index.search_memory(resolve_project_root(), query=query, limit=limit)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Code Index",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_indexer
    def ai_index_sync(include_tests: bool = False, timeout: int | None = None) -> dict[str, Any]:
        """Rebuild the derived code file manifest and summary index."""
        from .language_descriptors import validate_language_descriptors

        try:
            project_root = resolve_project_root()
        except ValueError as exc:
            return {
                "error": str(exc),
                "hint": "Pass root=<absolute path> or call a read tool first to establish a default project root.",
            }
        validation = validate_language_descriptors(project_root)
        descriptor_issues = validation.get("issues", [])
        result: dict[str, Any] = {}

        import sqlite3

        db_path = str(hub.code.db_path(project_root))

        def _durable_code_files_count() -> int:
            try:
                c = sqlite3.connect(db_path)
                row = c.execute("SELECT count(*) FROM code_files").fetchone()
                c.close()
                return row[0] if row else -1
            except Exception:
                return -1

        sync_ret = hub.code.sync_code_files(project_root, include_tests=include_tests)
        durable_after_sync = _durable_code_files_count()
        if sync_ret != durable_after_sync:
            result["error"] = (
                f"index_sync_postcondition_failed: sync_code_files returned {sync_ret} "
                f"but durable DB has {durable_after_sync} code_files at {db_path}"
            )
            return result

        modules_ret = hub.code.sync_modules(project_root)
        durable_after_modules = _durable_code_files_count()
        if durable_after_sync != durable_after_modules:
            result["error"] = (
                f"index_sync_postcondition_failed: sync_code_files wrote {durable_after_sync} "
                f"code_files but after sync_modules durable count is {durable_after_modules} "
                f"(possible peer index-sitter race). Re-run ai_index_sync."
            )
            return result

        result["code_files"] = sync_ret
        result["modules"] = modules_ret
        # #79 (Phoenix, 2026-05-07): auto-populate the DNT registry
        # from on-disk sentinel-marked files. Closes the gap where
        # ai_protect mode='sync' was manual-only and operators
        # forgot to run it. The on-disk truth and SQL registry must
        # not diverge — the read-tool DNT banner depends on the SQL
        # row to fire. Idempotent; cheap (bounded extensions, top
        # 16KB per file). Per backlog #79 + dental witness.
        try:
            from .dnt_registry_sync import sync_dnt_registry

            result["dnt_registry"] = sync_dnt_registry(project_root)
        except Exception as exc:
            result["dnt_registry"] = {
                "ok": False,
                "error": f"dnt sync failed: {exc!r}",
            }
        if descriptor_issues:
            result["descriptor_issues"] = descriptor_issues
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Modules",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def ai_get_modules(kind: str | None = None) -> list[dict[str, Any]]:
        """List detected project modules (workspaces, subprojects, informal modules)."""
        return hub.code.get_modules(resolve_project_root(), kind=kind)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Module Files",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def ai_get_module_files(
        module_path: str,
        modified_since: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List all indexed source files belonging to a specific module. Use modified_since to filter by recency: "today", "1h", "24h", "7d", or ISO datetime."""
        from .code_index_store import parse_modified_since

        mtime_ns = parse_modified_since(modified_since)
        return hub.code.get_module_files(
            resolve_project_root(),
            module_path=module_path,
            limit=limit,
            modified_since_ns=mtime_ns,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Index Status",
        },
    )
    def ai_index_status() -> dict[str, Any]:
        """Report current derived code index status for the project."""
        return hub.code.code_status(resolve_project_root())
