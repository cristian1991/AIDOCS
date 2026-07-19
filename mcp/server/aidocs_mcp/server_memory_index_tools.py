from __future__ import annotations

from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as
from .unified_recall import unified_memory_search


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
    # #202 canonical-flip read half, LIVE wiring: hand read_memory a
    # drawer-content adapter so the palace drawer is consulted FIRST and
    # the sqlite body serves as fallback. Only when the palace is wired
    # (hub.palace) and not operator-disabled; any failure degrades to the
    # sqlite-only read (never weaker than before).
    palace_reader = None
    try:
        if getattr(hub, "palace", None) is not None:
            control = getattr(hub, "palace_control", None)
            disabled = False
            if control is not None:
                disabled = bool(control.is_palace_disabled(project_root))
            if not disabled:
                from .memory_sqlite_store import PalaceDrawerReader

                palace_reader = PalaceDrawerReader(project_root)
    except Exception:
        palace_reader = None
    return hub.memory.read_memory(
        project_root,
        targets,
        include_inactive=effective_inactive,
        palace=palace_reader,
    )


def merged_memory_search(
    hub: Any,
    project_root: Path,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """memory_search = memory_index rows + PUBLIC scroll pointers (#341).

    Scroll doctrine/runbook content lives in the per-operator empire skills
    store, invisible to the per-project memory_index — which made the castle
    §XII drain precondition ('verify discovery from the new home before
    stubbing the old') unsatisfiable and blocked rule-file drains (#142).
    This read-side federation queries the skills store alongside and merges:

    - memory_index rows keep rank priority (they lead the list);
    - matching scrolls are guaranteed at least a small reserve of slots
      (max(1, limit//3)) so memory rows filling the limit can never squeeze
      a scroll hit out entirely;
    - fail-quiet: a missing/erroring skills store degrades to plain
      memory_index results — search never errors over scrolls.
    """
    rows = list(hub.index.search_memory(project_root, query=query, limit=limit))
    scrolls: list[dict[str, Any]] = []
    try:
        skills = getattr(hub, "skills", None)
        if skills is not None:
            scrolls = list(skills.search_public_scrolls(query, limit=limit))
    except Exception:
        scrolls = []  # non-weakening: scroll lookup can never break search
    if not scrolls:
        return rows[:limit]
    reserve = min(len(scrolls), max(1, limit // 3))
    kept = rows[: max(0, limit - reserve)]
    return kept + scrolls[: limit - len(kept)]


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
        """Unified memory search: exact keyword rows lead, then law/route,
        KG, and palace-semantic lane hits (each row carries its `lane`)."""
        # Concrete list|dict annotation (same as ai_search) so FastMCP emits the
        # structured_content={"result": [...]} sidecar for the bare-list return —
        # the outer call_tool wrapper then stamps memory freshness ALONGSIDE the
        # list items (never mutating them), sealing the pretty-OFF list path.
        # Memory-war unify (2026-07-16): route through the ONE retrieval core.
        # #341 scroll federation now runs inside the core's exact lane
        # (merged_memory_search stays as that lane's implementation).
        result = unified_memory_search(hub, resolve_project_root(), query, limit=limit)
        # LOUD projection staleness (2026-07-17 stall): if queued palace
        # projections have sat past the threshold, the semantic lane is
        # silently incomplete — say so instead of returning quietly-thin hits.
        try:
            from .server_code_tools import _palace_queue_health

            _health = _palace_queue_health()
            if _health.get("stale") and isinstance(result, list):
                result = [
                    *result,
                    {
                        "lane": "palace_health",
                        "palace_status": "stale",
                        "oldest_queued_age_s": _health.get("oldest_age_s"),
                        "note": (
                            "palace projection queue is STALLED — semantic-lane "
                            "results may be missing recent captures (canonical "
                            "sqlite rows remain durable)."
                        ),
                        **(
                            {"last_error": _health["last_error"]}
                            if "last_error" in _health
                            else {}
                        ),
                    },
                ]
        except Exception:  # noqa: BLE001 — health stamp must never break search
            pass
        return result

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
            "title": "Project Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("status", title="memory")
    def ai_memory(
        mode: str,
        kind: str = "",
        content: str = "",
        target_hint: str = "",
        keywords: list[str] | None = None,
        severity: str = "normal",
        trigger: str = "topic",
        priority: str = "normal",
        injection_mode: str = "pointer",
        anchor_symbols: list[dict[str, str]] | None = None,
        skip_fit_check: bool = False,
        targets: list[str] | None = None,
        include_inactive: bool = False,
        query: str = "",
        limit: int = 10,
        law_id: str = "",
        reason: str = "",
        content_hash: str = "",
        verbose: bool = False,
    ) -> Any:
        """Unified project memory — mode='capture'|'read'|'search'|'promote'|'candidates'.

        capture: persist a durable fact (FIT-checked; portable rules are
          flagged for operator promotion to the EMPIRE tier). Lean receipt
          by default; verbose=True returns the full one (#429).
        read:    read entries by target path(s) under .MEMORY/.
        search:  unified retrieval — exact keyword rows lead, then law/route,
          KG, and palace-semantic lane hits.
        promote: promote a kingdom entry into the EMPIRE tier — by content,
          or verbatim-by-reference via content_hash= (#451).
        candidates: list pending empire-candidate rows promotable by hash.
        """
        # One dispatch for BOTH surfaces: the registry consolidator routes
        # each mode to the internal memory_* impls via _delegate — this
        # stdio impl is the same call the gate's registry EDIT path makes.
        from .tool_interface import ai_memory as _registry_ai_memory

        return _registry_ai_memory(
            mode=mode,
            kind=kind,
            content=content,
            target_hint=target_hint,
            keywords=keywords,
            severity=severity,
            trigger=trigger,
            priority=priority,
            injection_mode=injection_mode,
            anchor_symbols=anchor_symbols,
            skip_fit_check=skip_fit_check,
            targets=targets,
            include_inactive=include_inactive,
            query=query,
            limit=limit,
            law_id=law_id,
            reason=reason,
            content_hash=content_hash,
            verbose=verbose,
        )

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
        # #75 (Empire 2026-06-20): a manual ai_index_sync DID reconcile the index — record
        # it so the freshness window stops emitting no_reconcile_yet on a fresh index.
        # Previously the only writer of last_reconcile_at was the sitter's poll, so a
        # just-completed manual sync still stamped 'unknown' (the lying marker). Combined
        # with a running sitter (#12) this flips the marker to 'fresh'. Best-effort;
        # never fail the sync over a freshness bookkeeping error.
        try:
            from .project_index_sitter import _record_reconcile_time

            _record_reconcile_time(project_root)
        except Exception:
            pass
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
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List indexed source files belonging to a specific module. Page with limit/offset; oversized results are trimmed with an in-band 'showing N of M' label (never a silent 74k dump). Use modified_since to filter by recency: "today", "1h", "24h", "7d", or ISO datetime."""
        from .code_index_store import parse_modified_since
        from .session_response_ledger import apply_listing_budget

        mtime_ns = parse_modified_since(modified_since)
        offset = max(0, int(offset or 0))
        limit = max(1, int(limit or 200))
        # Fetch one row past the requested page so the label can say there
        # is more beyond it (#474 budget honesty — no silent caps).
        rows = hub.code.get_module_files(
            resolve_project_root(),
            module_path=module_path,
            limit=offset + limit + 1,
            modified_since_ns=mtime_ns,
        )
        page = rows[offset : offset + limit]
        return apply_listing_budget(
            page,
            total=len(rows) - offset if len(rows) > offset else len(page),
            page_hint="page with limit/offset (defaults limit=200, offset=0)",
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
