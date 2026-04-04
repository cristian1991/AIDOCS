from __future__ import annotations

from pathlib import Path
from typing import Any

from .language_descriptors import (
    descriptor_match_summary,
    descriptor_registry_summary,
    descriptor_semantics_summary,
    validate_language_descriptors,
)


def register_code_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_tool: Any,
    timed_sync: Any,
    grant_indexed_read_gate: Any,
    grant_known_exact_path_read: Any,
    post_edit_reindex_and_grant: Any,
    require_indexed_read_gate: Any,
    apply_trace_depth: Any,
    resolve_related_root: Any,
    file_extract_block: Any,
    file_get_lines: Any,
    file_create_file: Any,
    file_edit_lines: Any,
    file_batch_edit: Any,
    file_str_replace: Any,
    file_batch_str_replace: Any,
    available_config_edit_modes: Any,
    self_edit_available_in_profile: Any,
    registered_tools: Any,
    all_procedures: Any,
    all_capabilities: Any,
) -> None:

    def _grant_paths_from_result(result: Any, tool_name: str, root: Any) -> None:
        """Extract file paths from any tool result and grant read access."""
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("path"):
                    grant_known_exact_path_read(hub, root, tool_name, str(item["path"]))
        elif isinstance(result, dict):
            for item in result.get("matches", result.get("results", [])):
                if isinstance(item, dict) and item.get("path"):
                    grant_known_exact_path_read(hub, root, tool_name, str(item["path"]))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_search(root: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the derived code index by file path and lightweight summary."""
        root = Path(root)
        result = hub.code.search_code(root, query=query, limit=limit)
        for item in result:
            grant_known_exact_path_read(hub, root, "code_search", str(item.get("path", "")))
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Text Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_text_search(
        root: str,
        text: str,
        glob: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        limit: int = 50,
        include_tests: bool = False,
    ) -> dict[str, Any]:
        """Literal text search across indexed files. Use | or OR between terms for multi-match. Set regex=true for pattern matching."""
        root = Path(root)
        matches = hub.code.search_text(
            root,
            text,
            glob=glob,
            case_sensitive=case_sensitive,
            regex=regex,
            limit=limit,
            include_tests=include_tests,
        )
        for match in matches:
            grant_known_exact_path_read(hub, root, "code_text_search", str(match.get("path", "")))
        return {"total_matches": len(matches), "results": matches}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Stale Reference Scan",
        },
    )
    def code_find_stale_references(
        root: str,
        symbols: list[str],
        exclude_path: str | None = None,
        include_tests: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """After renaming/deleting symbols, find remaining references that need updating."""
        root = Path(root)
        results = hub.code.find_stale_references(
            root,
            symbols,
            exclude_path=exclude_path,
            include_tests=include_tests,
            limit=limit,
        )
        return {"total_stale": len(results), "results": results}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Find Dead Code",
        },
    )
    def code_find_dead_code(
        root: str,
        path: str,
    ) -> dict[str, Any]:
        """Find dead imports and unused locals in a file. Use after refactors to clean up."""
        return hub.code.find_dead_code(Path(root), path)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Extract Block",
        }
    )
    def code_extract_block(
        root: str,
        source_path: str,
        start_line: int,
        end_line: int,
        target_path: str,
        target_position: str = "append",
        target_line: int | None = None,
        remove_from_source: bool = True,
    ) -> dict[str, Any]:
        """Move a code block from source to target file. Atomic: extracts lines, places in target, removes from source. Use for refactoring large files into modules."""
        root = Path(root)
        result = file_extract_block(
            root,
            source_path,
            start_line,
            end_line,
            target_path,
            target_position=target_position,
            target_line=target_line,
            remove_from_source=remove_from_source,
        )
        if result.get("success"):
            post_edit_reindex_and_grant(
                hub,
                root,
                "code_extract_block",
                str(result.get("source_path") or source_path),
            )
            post_edit_reindex_and_grant(
                hub,
                root,
                "code_extract_block",
                str(result.get("target_path") or target_path),
            )
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Find Symbol Range",
        },
    )
    def code_find_symbol_range(
        root: str,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """Find start and end line of a symbol using the index. Use before extract_block to avoid manual line counting."""
        return hub.code.find_symbol_range(
            Path(root), path, symbol, kind=kind, line_number=line_number
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Preview Extraction Dependencies",
        },
    )
    def code_preview_extraction_deps(
        root: str,
        path: str,
        start_line: int,
        end_line: int,
    ) -> dict[str, Any]:
        """Before extracting a block, show what imports and helpers it depends on that won't come with it."""
        return hub.code.preview_extraction_deps(Path(root), path, start_line, end_line)


    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Extract Symbol",
        },
    )
    def code_extract_symbol(
        root: str,
        source_path: str,
        symbol: str,
        target_path: str,
        kind: str | None = None,
        target_position: str = "append",
        remove_from_source: bool = True,
    ) -> dict[str, Any]:
        """Move a symbol (function/class/method) from source to target file by name. No line numbers needed — uses the index to find boundaries."""
        rng = hub.code.find_symbol_range(Path(root), source_path, symbol, kind=kind)
        if "error" in rng:
            return {"success": False, "error": rng["error"]}
        result = file_extract_block(
            Path(root),
            source_path,
            int(rng["start"]),
            int(rng["end"]),
            target_path,
            target_position=target_position,
            remove_from_source=remove_from_source,
        )
        if result.get("success"):
            post_edit_reindex_and_grant(
                hub, Path(root), "code_extract_symbol", source_path
            )
            post_edit_reindex_and_grant(
                hub, Path(root), "code_extract_symbol", target_path
            )
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Suggest Extractions",
        },
    )
    def code_suggest_extractions(
        root: str,
        path: str,
        min_lines: int = 20,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Show the largest symbols in a file that are good extraction candidates. Use to plan deslopification."""
        candidates = hub.code.suggest_extractions(
            Path(root), path, min_lines=min_lines, limit=limit
        )
        return {"path": path, "candidates": candidates, "total": len(candidates)}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Refactor Extract",
        },
    )
    def code_refactor_extract(
        root: str,
        source_path: str,
        symbol: str,
        target_path: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Full refactor pipeline: find symbol → extract to target → reindex both → detect stale references + dead code. Returns extraction result plus cleanup suggestions."""
        r = Path(root)

        # 1. Find symbol range
        rng = hub.code.find_symbol_range(r, source_path, symbol, kind=kind)
        if "error" in rng:
            return {"success": False, "step": "find_range", "error": rng["error"]}

        # 2. Extract
        extract_result = file_extract_block(
            r,
            source_path,
            int(rng["start"]),
            int(rng["end"]),
            target_path,
            target_position="append",
            remove_from_source=True,
        )
        if not extract_result.get("success"):
            return {
                "success": False,
                "step": "extract",
                "error": extract_result.get("error"),
            }

        # 3. Reindex both files
        post_edit_reindex_and_grant(hub, r, "code_refactor_extract", source_path)
        post_edit_reindex_and_grant(hub, r, "code_refactor_extract", target_path)

        # 4. Find stale references to the moved symbol
        stale = hub.code.find_stale_references(
            r, [symbol], exclude_path=target_path, limit=20
        )

        # 5. Find dead code in source (imports that became unused after extraction)
        dead = hub.code.find_dead_code(r, source_path)

        return {
            "success": True,
            "extracted": {
                "symbol": symbol,
                "source": source_path,
                "target": target_path,
                "lines": rng["lines"],
            },
            "stale_references": stale,
            "dead_code": {
                "dead_imports": dead.get("dead_imports", []),
                "unused_locals": dead.get("unused_locals", []),
            },
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Dependencies",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_dependencies(root: str, path: str) -> list[dict[str, str]]:
        """Return lightweight dependency edges for one indexed code file."""
        return hub.code.get_dependencies(Path(root), path=path)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Symbol Snippet",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_symbol_snippet(
        root: str,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """Return an exact code snippet for an indexed outline symbol."""
        root = Path(root)
        result = hub.code.get_symbol_snippet(
            root,
            path=path,
            symbol=symbol,
            kind=kind,
            line_number=line_number,
        )
        if result:
            grant_known_exact_path_read(hub, root, "code_get_symbol_snippet", path)
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Method Signature",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_method_signature(
        root: str,
        method: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return exact method signatures so agents can call methods correctly without reading whole files."""
        root = Path(root)
        result = hub.code.get_method_signature(
            root, method_name=method, container=container, limit=limit
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Method Signature",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_method_signature(
        root: str,
        method: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return exact method signatures so agents can call methods correctly without reading whole files."""
        root = Path(root)
        result = hub.code.get_method_signature(
            root, method_name=method, container=container, limit=limit
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Method Signatures",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_method_signatures(
        root: str,
        methods: list[str],
        container: str | None = None,
        limit_per_method: int = 20,
    ) -> dict[str, Any]:
        """Return exact signatures for multiple methods in one call."""
        root = Path(root)
        result = hub.code.get_method_signatures(
            root,
            methods=methods,
            container=container,
            limit_per_method=limit_per_method,
        )
        if result.get("methods"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Enum Values",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_enum_values(
        root: str,
        enum_name: str,
        limit: int = 50,
        include_related: bool = False,
    ) -> dict[str, Any]:
        """Return indexed enum definitions with their enum members."""
        root = Path(root)
        result = hub.code.get_enum_values(
            root, enum_name=enum_name, limit=limit, include_related=include_related
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Constructor Params",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_constructor_params(
        root: str,
        type_name: str,
        limit: int = 20,
        include_related: bool = False,
    ) -> dict[str, Any]:
        """Return constructor or record positional parameter information for a type."""
        root = Path(root)
        result = hub.code.get_constructor_params(
            root, type_name=type_name, limit=limit, include_related=include_related
        )
        if result.get("matches"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Constructor Params Batch",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_constructor_params_batch(
        root: str,
        types: list[str],
        include_related: bool = False,
        limit_per_type: int = 20,
    ) -> dict[str, Any]:
        """Return constructor or record positional parameter information for multiple types."""
        root = Path(root)
        result = hub.code.get_constructor_params_batch(
            root,
            types=types,
            include_related=include_related,
            limit_per_type=limit_per_type,
        )
        if result.get("types"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Service API",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_service_api(
        root: str,
        service_name: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return all indexed public method signatures for a service-like class."""
        root = Path(root)
        result = hub.code.get_service_api(root, service_name=service_name, limit=limit)
        if result.get("methods"):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Entity Properties",
        },
        meta={"anthropic/searchHint": True},
    )
    def code_get_entity_properties(
        root: str,
        entity_name: str,
    ) -> dict[str, Any]:
        """Return a lightweight property list for an entity or DTO."""
        root = Path(root)
        result = hub.code.get_entity_properties(root, entity_name=entity_name)
        if result.get("entity_name") and (
            result.get("properties") or result.get("note")
        ):
            pass  # Precision tool - no blanket read grant
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Investigate",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_investigate(
        root: str,
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """START HERE — investigate a concept, feature, or bug area.

        Returns a navigation guide: what was found across symbols, files, schema, CSS, and modules,
        plus which specific tools to call next with reasons why.

        Use this FIRST when you don't know where to start. It replaces guessing with Grep.

        Args:
            concept: The thing to investigate (e.g., "PDF generation", "authorization", "field-input", "Patient").
            depth: `shallow`, `standard`, or `deep`.
            focus: `general`, `workflow`, `service`, `schema`, `ui`, or `backend`.
        """
        root = Path(root)
        result = hub.code.investigate(
            root, concept=concept, limit=limit, depth=depth, focus=focus
        )
        for finding in (result.get("findings") or []):
            for item in (finding.get("top") or []):
                p = item.get("path")
                if p:
                    grant_known_exact_path_read(hub, root, "code_investigate", str(p))
        findings = result.get("findings") or []
        next_tools = result.get("next_tools") or []
        compact = runtime.build_artifact_backed_result(
            root,
            inline_summary=(
                f"Investigation for `{concept}` found {len(findings)} finding(s) and {len(next_tools)} suggested next tool(s)."
            ),
            payload=result,
            artifact_name=f"code-investigate-{concept}",
            structured_summary={
                "concept": concept,
                "finding_count": len(findings),
                "next_tool_count": len(next_tools),
                "depth": depth,
                "focus": focus,
            },
        )
        result.update(compact)
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Unified Tools (v1.1.0) — prefer these over granular tools below
    # ═══════════════════════════════════════════════════════════════════════

    _FIND_MODES = {
        "symbols": "Search symbols by name, kind, or role",
        "references": "Find all usages of a symbol across the codebase",
        "routes": "Find API endpoints, page routes, controllers",
        "hotspots": "Find complex files (high symbol count, deep nesting)",
        "query_hotspots": "Find files with heavy DB query patterns",
        "entrypoints": "Find bootstrap, main, provider-like entry symbols",
        "duplicates": "Find structurally similar code across files",
        "partial_group": "Find all partial class files for a C# type",
        "partial_consumers": "Find pages/views referencing a Razor partial",
        "api_consumers": "Find pages/scripts calling an API endpoint",
        "frontend_symbols": "Find components, hooks, providers by name",
        "data_structures": "Find classes, records, enums with their members",
        "initializers": "Find DOMContentLoaded, document.ready, window.onload",
        "mutations": "Find create/update/delete flows for a concept",
        "validation": "Find validation logic, required fields, validators",
        "async": "Find async boundaries, deferred execution, Task patterns",
        "policy": "Find authorization, RBAC, permission checks",
        "touchpoints": "Find UI↔backend connection points for a concept",
        "mismatches": "Find state/model representation conflicts",
        "clusters": "Find cross-layer grouping for a domain concept",
        "transitions": "Find migration seams, adapters, compatibility layers",
        "factories": "Find Create* helpers, factory-style methods, and setup helpers",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Find",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_find(
        root: str,
        query: str,
        mode: str = "symbols",
        kind: str | None = None,
        role: str | None = None,
        include_tests: bool = False,
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Unified find tool — replaces all code_find_* and code_search_* tools.

        Modes: symbols, references, routes, hotspots, query_hotspots, entrypoints,
        duplicates, partial_group, partial_consumers, api_consumers, frontend_symbols,
        data_structures, initializers, mutations, validation, async, policy,
        touchpoints, mismatches, clusters, transitions, factories.

        Args:
            query: What to find (symbol name, concept, endpoint, class name, etc.).
            mode: Which find mode to use (see above).
            kind: Filter by symbol kind (only for mode=symbols).
            role: Filter by file role (only for mode=symbols).
            include_tests: Include test/fixture files in search. Default False. Auto-enabled for mode=factories.
        """
        root = Path(root)
        m = mode.strip().lower()

        # Auto-enable include_tests for modes that commonly need test/fixture content
        if m in ("factories",) and not include_tests:
            include_tests = True

        # If include_tests requested, ensure test files are indexed
        if include_tests:
            hub.code.sync_code_files(root, include_tests=True)

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "code_find", root)
                return result
            if any(result.get(key) for key in ("matches", "cluster")):
                _grant_paths_from_result(result, "code_find", root)
            return result

        if m == "symbols":
            return _grant(
                hub.code.search_symbols(
                    root, query=query, kind=kind, role=role, limit=limit
                )
            )
        if m == "references":
            return _grant(hub.code.find_references(root, symbol=query, limit=limit))
        if m == "routes":
            return _grant(hub.code.find_routes(root, query=query, limit=limit))
        if m == "hotspots":
            return _grant(hub.code.find_hotspots(root, query=query, limit=limit))
        if m == "query_hotspots":
            return _grant(hub.code.find_query_hotspots(root, query=query, limit=limit))
        if m == "entrypoints":
            return _grant(hub.code.find_entrypoints(root, concept=query, limit=limit))
        if m == "duplicates":
            return _grant(
                hub.code.find_duplicate_structures(
                    root, role_filter=query or None, limit=limit
                )
            )
        if m == "partial_group":
            return _grant(hub.code.find_partial_group(root, symbol=query, limit=limit))
        if m == "partial_consumers":
            return _grant(
                hub.code.find_partial_consumers(root, partial_name=query, limit=limit)
            )
        if m == "api_consumers":
            return _grant(
                hub.code.find_api_consumers(root, endpoint=query, limit=limit)
            )
        if m == "frontend_symbols":
            return _grant(
                hub.code.find_frontend_symbols(root, query=query, limit=limit)
            )
        if m == "data_structures":
            return _grant(hub.code.find_data_structures(root, query=query, limit=limit))
        if m == "initializers":
            return _grant(
                hub.code.find_initializers(
                    root, path=query if query.strip() else None, limit=limit
                )
            )
        if m == "mutations":
            return _grant(
                hub.code.find_mutation_points(root, concept=query, limit=limit)
            )
        if m == "validation":
            return _grant(
                hub.code.find_validation_surfaces(root, concept=query, limit=limit)
            )
        if m == "async":
            return _grant(
                hub.code.find_async_boundaries(root, concept=query or None, limit=limit)
            )
        if m == "policy":
            return _grant(
                hub.code.find_policy_surfaces(root, concept=query, limit=limit)
            )
        if m == "touchpoints":
            return _grant(
                hub.code.find_ui_backend_touchpoints(root, concept=query, limit=limit)
            )
        if m == "mismatches":
            return _grant(
                hub.code.find_state_model_mismatch(root, concept=query, limit=limit)
            )
        if m == "clusters":
            return _grant(
                hub.code.find_domain_clusters(root, concept=query, limit=limit)
            )
        if m == "transitions":
            return _grant(
                hub.code.find_transition_points(root, concept=query, limit=limit)
            )
        if m == "factories":
            return _grant(hub.code.find_factories(root, query=query, limit=limit))
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(create_server._FIND_MODES.keys()),
        }

    _TRACE_MODES = {
        "field_flow": "Trace a field across model→service→UI layers",
        "service": "Find where a service is injected and consumed",
        "model": "Trace a DTO/entity through the full stack",
        "component": "Trace component imports and usage",
        "api_to_ui": "Trace from API endpoint through to UI",
        "css_class": "Find CSS definitions AND HTML/template usages",
        "query_shape": "Trace query patterns + schema relationships",
        "setting": "Trace a configuration setting across layers",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Trace",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_trace(
        root: str,
        query: str,
        mode: str = "field_flow",
        limit: int = 50,
        max_depth: int | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Unified trace tool — replaces all code_trace_* tools.

        Modes: field_flow, service, model, component, api_to_ui, css_class, query_shape, setting.

        Args:
            query: What to trace (field name, service name, component name, CSS class, etc.).
            mode: Which trace mode to use.
        """
        root = Path(root)
        m = mode.strip().lower()

        def _grant(result: dict[str, Any]) -> dict[str, Any]:
            if any(result.get(key) for key in ("matches", "api", "logic", "ui")):
                _grant_paths_from_result(result, "code_trace", root)
            return result

        if m == "field_flow":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_field_flow(root, field_name=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        if m == "service":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_service_usage(root, service_name=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        if m == "model":
            return _grant(
                hub.code.trace_model_usage(root, model_name=query, limit=limit)
            )
        if m == "component":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_component_usage(
                        root, component_name=query, limit=limit
                    ),
                    m,
                    max_depth,
                )
            )
        if m == "api_to_ui":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_api_to_ui(root, concept=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        if m == "css_class":
            return _grant(
                hub.code.trace_css_class_usage(root, class_name=query, limit=limit)
            )
        if m == "query_shape":
            return _grant(hub.code.trace_query_shape(root, path=query, limit=limit))
        if m == "setting":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_setting_usage(root, setting_name=query, limit=limit),
                    m,
                    max_depth,
                )
            )
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(create_server._TRACE_MODES.keys()),
        }

    _BUNDLE_MODES = {
        "file": "Full file context: outline + deps + schema hints",
        "service": "Service file + related backend neighbors",
        "component": "Component + imported frontend neighbors",
        "query": "Query hotspot + schema hints + relationship paths",
        "subsystem": "Broad concept analysis across all layers",
        "dependency": "File + resolved dependency chain",
        "partial": "All partial class definitions for a C# type",
        "symbol": "Symbol definition + references + schema matches",
        "style": "CSS selector matches for class names",
        "session": "Session-guided code bundle from context targets",
        "context": "Session-guided ranked context bundle",
        "preset": "Preconfigured bundle (csharp-partial, data-structure, etc.)",
        "tree": "Recursive component import tree",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Bundle",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_tool
    def code_bundle(
        root: str,
        target: str,
        mode: str = "file",
        session_id: str | None = None,
        limit: int = 20,
        timeout: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Unified bundle tool — replaces all code_get_*_bundle tools.

        Modes: file, service, component, query, subsystem, dependency, partial,
        symbol, style, session, context, preset, tree.

        Args:
            target: File path, symbol name, concept, CSS class, or preset spec depending on mode.
            mode: Which bundle mode to use.
            session_id: Required for session/context modes.
        """
        root = Path(root)
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "code_bundle", root)
                return result
            if any(
                result.get(key)
                for key in (
                    "primary_files",
                    "related_files",
                    "files",
                    "symbols",
                    "matches",
                )
            ):
                _grant_paths_from_result(result, "code_bundle", root)
            bundle_type = m
            file_count = (
                len(result.get("files") or []) if isinstance(result, dict) else 0
            )
            symbol_count = (
                len(result.get("symbols") or []) if isinstance(result, dict) else 0
            )
            if not file_count and isinstance(result, dict):
                file_count = len(result.get("primary_files") or []) + len(
                    result.get("related_files") or []
                )
            summary_bits: list[str] = []
            if file_count:
                summary_bits.append(f"files={file_count}")
            if symbol_count:
                summary_bits.append(f"symbols={symbol_count}")
            if result.get("missing"):
                inline_summary = f"Bundle `{bundle_type}` for `{target}` is missing."
            else:
                suffix = f" ({', '.join(summary_bits)})" if summary_bits else ""
                inline_summary = (
                    f"Bundle `{bundle_type}` prepared for `{target}`{suffix}."
                )
            compact = runtime.build_artifact_backed_result(
                root,
                inline_summary=inline_summary,
                payload=result,
                artifact_name=f"code-bundle-{bundle_type}-{target}",
                structured_summary={
                    "mode": bundle_type,
                    "target": target,
                    "missing": bool(result.get("missing")),
                    "file_count": file_count,
                    "symbol_count": symbol_count,
                },
            )
            result.update(compact)
            return result

        if m == "file":
            return _grant(hub.code.get_file_bundle(root, path=target))
        if m == "service":
            return _grant(hub.code.get_service_bundle(root, path=target, limit=limit))
        if m == "component":
            return _grant(hub.code.get_component_bundle(root, path=target, limit=limit))
        if m == "query":
            return _grant(hub.code.get_query_bundle(root, path=target, limit=limit))
        if m == "subsystem":
            return _grant(
                hub.code.get_subsystem_bundle(root, concept=target, limit=limit)
            )
        if m == "dependency":
            return _grant(
                hub.code.get_dependency_bundle(root, path=target, limit=limit)
            )
        if m == "partial":
            return _grant(hub.code.get_partial_bundle(root, symbol=target, limit=limit))
        if m == "symbol":
            return _grant(hub.code.get_symbol_bundle(root, symbol=target, limit=limit))
        if m == "style":
            # Accept comma/space separated class names
            if isinstance(target, str):
                class_names = [
                    s.strip() for s in re.split(r"[,\s]+", target) if s.strip()
                ]
            else:
                class_names = target
            return _grant(
                hub.code.get_style_bundle(root, class_names=class_names, limit=limit)
            )
        if m == "session":
            if not session_id:
                return {"error": "session_id is required for session mode"}
            return _grant(hub.code.get_session_code_bundle(root, session_id=session_id))
        if m == "context":
            if not session_id:
                return {"error": "session_id is required for context mode"}
            return _grant(
                hub.code.get_context_bundle(root, session_id=session_id, limit=limit)
            )
        if m == "preset":
            # target format: "preset_name:value" e.g. "csharp-partial:FormPdfService"
            parts = target.split(":", 1)
            preset = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            return _grant(
                hub.code.get_preset_bundle(
                    root, preset=preset, value=value, limit=limit
                )
            )
        if m == "tree":
            return _grant(hub.code.get_component_tree(root, path=target, limit=limit))
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(create_server._BUNDLE_MODES.keys()),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Schema Query",
        },
        meta={"anthropic/searchHint": True},
    )
    @timed_tool
    def schema_query(
        root: str,
        query: str,
        mode: str = "entities",
        limit: int = 50,
        include_related: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Unified schema tool — replaces all schema_find_*, schema_get_*, schema_trace_* tools.

        Modes: entities, entity, field, trace_flow, trace_path.

        Args:
            query: Entity name, field name, or "source→target" for trace_path mode.
            mode: Which schema operation to run.
        """
        root = Path(root)
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "schema_query", root)
                return result
            if any(
                result.get(key)
                for key in ("entities", "fields", "matches", "properties")
            ):
                _grant_paths_from_result(result, "schema_query", root)
            return result

        if m == "entities":
            return _grant(
                hub.schema.find_schema_entities(root, query=query or None, limit=limit)
            )
        if m == "entity":
            return _grant(hub.schema.get_schema_entity(root, entity_name=query))
        if m == "batch_entity":
            names = [
                part.strip() for part in re.split(r"[\n,]+", query) if part.strip()
            ]
            return _grant(
                hub.schema.get_schema_entities_batch(root, entity_names=names)
            )
        if m == "field":
            return _grant(
                hub.schema.find_schema_field(root, field_name=query, limit=limit)
            )
        if m == "constructor":
            return _grant(
                hub.schema.get_constructor_params(
                    root, entity_name=query, include_related=include_related
                )
            )
        if m == "properties":
            return _grant(hub.schema.get_entity_properties(root, entity_name=query))
        if m == "trace_flow":
            return _grant(
                hub.schema.trace_entity_flow(root, entity_name=query, limit=limit)
            )
        if m == "trace_path":
            # Accept "Source→Target" or "Source -> Target" or "Source,Target"
            parts = re.split(r"[→\->,]+", query, maxsplit=1)
            if len(parts) < 2:
                return {
                    "error": "trace_path requires 'Source→Target' format",
                    "query": query,
                }
            return _grant(
                hub.schema.trace_relationship_path(
                    root,
                    source_entity=parts[0].strip(),
                    target_entity=parts[1].strip(),
                    limit=limit,
                )
            )
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": [
                "entities",
                "entity",
                "field",
                "trace_flow",
                "trace_path",
            ],
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Legacy Tools (deprecated — use unified tools above)
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Capture Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    def memory_capture(
        root: str,
        kind: str,
        content: str,
        target_hint: str | None = None,
    ) -> dict[str, str]:
        """Capture a durable fact/rule into canonical memory.

        Args:
            kind: Memory category — 'rule', 'feedback', 'domain', 'project', 'user', 'reference', 'system'.
            content: The fact/rule to persist (any language).
            target_hint: Target filename or path. Use this to route to the right file:
        - 'workflow' → rules/workflow-rules.md (git, deploy, task lifecycle, CI rules)
                - 'coding-standards' → rules/coding-standards.md (code style, naming, patterns)
                - 'communication' → rules/communication.md (response style, verbosity, tone)
                - 'design' → rules/design.md (UI, colors, themes, layout preferences)
                - 'security' → rules/security.md (auth, permissions, credentials)
                - 'project-state' → domains/project-state.md (current project status/decisions)
                - 'user-profile' → domains/user-profile.md (user role, expertise, preferences)
                - 'references' → domains/references.md (external system pointers)
                - Or any path like 'domains/accounting.md' for topic-specific files.
                If omitted, AIDOCS uses content-based keyword routing as fallback (English-only, less reliable).
                Prefer providing target_hint for accurate routing.
        """
        result = hub.memory.capture_memory(
            Path(root),
            kind=kind,
            content=content,
            target_hint=target_hint,
        )
        return {
            "target_file": str(result.target_file),
            "content": result.content,
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Initialize Project",
        }
    )
    @timed_sync
    def project_init(
        root: str,
        init_git: bool = True,
        create_remote: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Initialize AIDOCS structure on a new project — creates .MEMORY/, AGENTS.md/CLAUDE.md, and templates.

        Creates the full AIDOCS directory structure directly (no shell scripts).
        Safe to call on already-initialized projects (idempotent).
        Also ensures the project has a .mcp.json with the aidocs MCP server entry for Claude Code.

        Args:
            init_git: If True (default), initialize a git repo if none exists.
            create_remote: If True, create a private GitHub repo using `gh` CLI. Default: False (opt-in).
        """
        return runtime.project_init(
            Path(root), init_git=init_git, create_remote=create_remote
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Ensure MCP Config",
        }
    )
    def project_ensure_mcp_config(root: str) -> dict[str, Any]:
        """Ensure the target project has a .mcp.json with the aidocs MCP server entry for Claude Code.

        Idempotent — safe to call repeatedly. Creates or updates .mcp.json as needed.
        Preserves any existing non-aidocs MCP server entries.
        """
        return runtime.ensure_claude_mcp_config(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Check Project",
        }
    )
    def project_check(root: str) -> dict[str, Any]:
        """Run strict session-era structural check on a project."""
        return hub.updater.run_check(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Check Project (Legacy)",
        }
    )
    def project_check_legacy(root: str) -> dict[str, Any]:
        """Run legacy-compatible structural check on a project."""
        return hub.updater.run_check_legacy(Path(root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Fix Project",
        }
    )
    def project_fix(root: str) -> dict[str, Any]:
        """Run safe deterministic structural fixes on a project."""
        return hub.updater.run_fix(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Inspect Legacy",
        }
    )
    def project_inspect_legacy(root: str) -> dict[str, Any]:
        """Inspect whether legacy runtime files/folders are still present."""
        return hub.updater.inspect_legacy_runtime(Path(root))

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Project Indexes",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def project_sync_indexes(
        root: str, include_tests: bool = False, timeout: int | None = None
    ) -> dict[str, Any]:
        """Refresh all derived indexes for a project in one call."""
        root = Path(root)
        capability_count = hub.capabilities.sync_capabilities(root, registered_tools())
        workflow_sync = hub.workflow.compile_project_rules(root)
        procedure_count = hub.procedures.sync_procedures(
            root, hub.workflow.read_compiled(root)
        )
        link_count = hub.procedure_links.sync_links(
            root, all_procedures(root), all_capabilities(root)
        )
        code_processed = hub.code.sync_code_files(root, include_tests=include_tests)
        code_status = hub.code.code_status(root)
        return {
            "memory": hub.index.sync_all(root),
            "capabilities": {"capability_definitions": capability_count},
            "code_manifest": {
                "processed_code_files": code_processed,
                "code_files": code_status.get("code_files"),
                "parsed_code_files": code_status.get("parsed_code_files"),
            },
            "schema": hub.schema.sync_schema(root),
            "workflow": workflow_sync,
            "procedures": {"procedure_definitions": procedure_count},
            "procedure_capability_links": {"links": link_count},
            "execution": hub.execution.execution_status(root),
            "execution_pruning": hub.execution.prune_old_events(root),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Status",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status(root: str) -> dict[str, Any]:
        """Return a consolidated status view for memory, code, and schema indexes."""
        root = Path(root)
        return {
            "origins": runtime.project_origins(root),
            "repo_summary": runtime.repo_summary(root),
            "memory": hub.index.status(root),
            "capabilities": hub.capabilities.capability_status(root),
            "code": hub.code.code_status(root),
            "schema": hub.schema.schema_status(root),
            "workflow": hub.workflow.status(root),
            "procedures": hub.procedures.procedure_status(root),
            "procedure_capability_links": hub.procedure_links.link_status(root),
            "execution": hub.execution.execution_status(root),
            "legacy": hub.updater.inspect_legacy_runtime(root),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Origins",
        }
    )
    def project_origins_get(root: str) -> dict[str, Any]:
        """Return git remote/origin context, including private/public split hints."""
        root = Path(root)
        return runtime.project_origins(root)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptors",
        }
    )
    def index_language_descriptors_get(root: str) -> dict[str, Any]:
        """Return the active built-in + project-local language descriptor registry summary."""
        return descriptor_registry_summary(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Validate Language Descriptors",
        }
    )
    def index_language_descriptors_validate(root: str) -> dict[str, Any]:
        """Validate built-in and project-local TOML language descriptors."""
        return validate_language_descriptors(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptor Semantics",
        }
    )
    def index_language_descriptor_semantics_get() -> dict[str, Any]:
        """Return the available built-in descriptor semantic families/tags."""
        return descriptor_semantics_summary()

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptor Match",
        }
    )
    def index_language_descriptor_match_get(
        root: str, relative_path: str
    ) -> dict[str, Any]:
        """Show which descriptor would classify a given project-relative path."""
        return descriptor_match_summary(Path(root), relative_path)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Capability Index Status",
        }
    )
    def capability_index_status(root: str) -> dict[str, Any]:
        """Return current MCP capability index status for a project."""
        return hub.capabilities.capability_status(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Capability Definitions",
        },
        meta={"anthropic/searchHint": True},
    )
    def capability_definitions_get(
        root: str, query: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Return indexed MCP capability definitions, optionally filtered by query."""
        root = Path(root)
        result = hub.capabilities.find_capabilities(root, query=query, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} capability definition(s).",
            payload=result,
            artifact_name="capability-definitions",
            structured_summary={
                "count": len(result),
                "query": query,
                "limit": limit,
            },
        )
