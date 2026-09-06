from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class CodeIndexBundleService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def get_symbol_bundle(
        self,
        project_root: Path,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        definitions = self.store.search_symbols(project_root, query=symbol, limit=limit)
        if path is not None:
            definitions = [item for item in definitions if item["path"] == path]
        if kind is not None:
            definitions = [item for item in definitions if item["kind"] == kind]

        if not definitions:
            return {
                "symbol": symbol,
                "definitions": [],
                "references": [],
                "dependencies": [],
                "partials": [],
                "schema_entities": [],
                "schema_fields": [],
            }

        primary = definitions[0]
        definition_snippets = [
            self.store.get_symbol_snippet(
                project_root,
                path=str(item["path"]),
                symbol=str(item["symbol"]),
                kind=str(item["kind"]),
                line_number=int(item["line_number"]),
            )
            for item in definitions[: min(len(definitions), 8)]
        ]
        references = self.store.find_references(project_root, symbol=symbol, limit=limit)["matches"]
        dependencies = self.store.get_dependencies(project_root, str(primary["path"]))
        partials = self.store.find_partial_group(project_root, symbol=symbol, limit=limit)

        schema_entities = []
        schema_fields = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            schema_entities = schema.find_schema_entities(project_root, query=symbol, limit=limit)
            schema_fields = schema.find_schema_field(project_root, symbol, limit=limit)
        except Exception:
            pass

        return {
            "symbol": symbol,
            "definitions": definition_snippets,
            "references": references,
            "dependencies": dependencies,
            "partials": partials,
            "schema_entities": schema_entities,
            "schema_fields": schema_fields,
        }

    def get_subsystem_bundle(
        self,
        project_root: Path,
        concept: str,
        limit: int = 20,
    ) -> dict[str, object]:
        # Use smaller per-category limits for a concise summary
        cat_limit = min(limit, 8)

        domain_cluster = self.store.find_domain_clusters(
            project_root,
            concept=concept,
            limit=cat_limit,
        )
        touchpoints = self.store.find_ui_backend_touchpoints(
            project_root,
            concept=concept,
            limit=cat_limit,
        )
        policy = self.store.find_policy_surfaces(project_root, concept=concept, limit=cat_limit)
        transitions = self.store.find_transition_points(
            project_root,
            concept=concept,
            limit=cat_limit,
        )
        data_structures = self.store.find_data_structures(
            project_root,
            query=concept,
            limit=cat_limit,
        )
        entrypoints = self.store.find_entrypoints(project_root, concept=concept, limit=cat_limit)

        # Strip verbose snippets and low-relevance results
        min_score = 40

        def slim(
            matches: list[dict[str, object]],
            require_score: bool = True,
        ) -> list[dict[str, object]]:
            return [
                {k: v for k, v in m.items() if k != "snippet"}
                for m in (matches or [])
                if not require_score or int(m.get("score", 0)) >= min_score
            ]

        return {
            "domain_cluster": slim(domain_cluster.get("cluster", []), require_score=False),
            "touchpoints": slim(touchpoints.get("matches", [])),
            "policy_surfaces": slim(policy.get("matches", [])),
            "transition_points": slim(transitions.get("matches", [])),
            "data_structures": slim(
                data_structures
                if isinstance(data_structures, list)
                else data_structures.get("result", []),
                require_score=False,
            ),
            "entrypoints": slim(entrypoints.get("matches", [])),
        }

    # Lane bounds (#462 golden-road recharter): every aggregated evidence
    # lane is hard-capped so investigate stays a NAVIGATION GUIDE — counts
    # + top + next_tools per lane, never a data dump.
    _MEMORY_LANE_CAP = 5
    _PALACE_LANE_CAP = 5
    _BACKLOG_LANE_CAP = 3
    _TODO_LANE_CAP = 3
    _LSP_LANE_CAP = 3

    def investigate(
        self,
        project_root: Path,
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
        palace: object | None = None,
        hub_ctx: object | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """High-level investigation entry point. Returns a navigation guide, not full data.

        Runs quick probes across symbols, code files, schema, CSS, and modules,
        then returns a ranked summary of what was found and which tools to call next.

        #462 (golden-road recharter): the investigation pipeline converges INTO
        this probe fan. Beside the five code-index probes, bounded fail-quiet
        evidence lanes consult:
          * anchored memories for the surfaced leaves (memory_symbol_anchors);
          * kingdom + empire palace semantic drawers (rebuildable_projection,
            empire capped at 1/3 — the kingdom-protective interleave);
          * active backlog items plus unresolved active-session todos matching
            the concept ("there is already work about this");
          * LSP guest-oracle MATERIALIZED reference sites (code_edges
            semantic_ref rows, §XXXII — an absent guest costs zero).
        Empty lanes are omitted (no-padding law); a dead substrate never
        breaks the core probes.
        """
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"findings": [], "next_tools": []}

        depth_value = depth.strip().lower()
        focus_value = focus.strip().lower()
        if depth_value not in {"shallow", "standard", "deep"}:
            depth_value = "standard"
        if focus_value not in {"general", "workflow", "service", "schema", "ui", "backend"}:
            focus_value = "general"

        symbol_limit = (
            limit if depth_value == "shallow" else (limit * 2 if depth_value == "deep" else limit)
        )
        code_limit = (
            limit if depth_value == "shallow" else (limit * 2 if depth_value == "deep" else limit)
        )

        findings: list[dict[str, object]] = []
        next_tools: list[dict[str, str]] = []

        # 1. Symbol search — the tiered scorer (War AW, #462): consume
        # search_symbols_tiered so the probe rides the strong tier +
        # related_summary, never the raw related/fuzz flood.
        related_summary: dict[str, object] | None = None
        try:
            tiered = self.store.search_symbols_tiered(project_root, needle, limit=symbol_limit)
            symbols = list(tiered.get("results") or [])
            raw_summary = tiered.get("related_summary")
            related_summary = raw_summary if isinstance(raw_summary, dict) else None
        except Exception:
            symbols = self.store.search_symbols(project_root, needle, limit=symbol_limit)
        if symbols:
            top_kinds = list(dict.fromkeys(s["kind"] for s in symbols))[:3]
            preview_symbols: list[dict[str, object]] = []
            preview_symbols.extend(symbols[:3])
            for preferred_kind in ("method", "enum", "record", "class"):
                extra = next(
                    (
                        item
                        for item in symbols
                        if item["kind"] == preferred_kind and item not in preview_symbols
                    ),
                    None,
                )
                if extra is not None:
                    preview_symbols.append(extra)
            top = []
            for s in preview_symbols[:6]:
                item = {"symbol": s["symbol"], "kind": s["kind"], "path": s["path"]}
                if s.get("tier"):
                    item["tier"] = s["tier"]
                if s.get("namespace"):
                    item["namespace"] = s["namespace"]
                if s["kind"] == "method":
                    signature = self.store._extract_method_signature(
                        project_root,
                        str(s["path"]),
                        int(s["line_number"]),
                    )
                    if signature.get("signature"):
                        item["signature"] = signature["signature"]
                elif s["kind"] == "enum":
                    item["enum_values"] = self.store._enum_members_for_container(
                        project_root,
                        str(s["path"]),
                        str(s["symbol"]),
                    )[:8]
                top.append(item)
            symbol_finding: dict[str, object] = {
                "area": "symbols",
                "source": "outline_index",
                "count": len(symbols),
                "top": top,
                "kinds_found": top_kinds,
            }
            if related_summary is not None:
                # No-padding law: the related flood stays collapsed — only
                # its COUNT rides the guide (data loss zero, mugging zero).
                symbol_finding["related"] = {"count": related_summary.get("count", 0)}
            findings.append(symbol_finding)
            next_tools.append(
                {
                    "tool": "code_search_symbols",
                    "why": f"Found {len(symbols)} symbols — search for specific names/kinds",
                },
            )
            if any(s["kind"] in ("class", "interface", "struct", "record") for s in symbols):
                next_tools.append(
                    {"tool": "code_find_references", "why": "Trace where these types are used"},
                )
            if any(s["kind"] == "method" for s in symbols):
                next_tools.append(
                    {
                        "tool": "ai_get_symbol_info",
                        "why": "Read exact method params/returns before calling methods",
                    },
                )
            if any(str(s["symbol"]).endswith("Service") and s["kind"] == "class" for s in symbols):
                next_tools.append(
                    {
                        "tool": "ai_get_symbol_info",
                        "why": "Read all public method signatures for a service before writing workflow-heavy code or tests",
                    },
                )
            if any(s["kind"] == "enum" for s in symbols):
                next_tools.append(
                    {
                        "tool": "ai_get_symbol_info",
                        "why": "Read exact enum members before using enum values",
                    },
                )

            service_candidates = [
                s for s in symbols if s["kind"] == "class" and str(s["symbol"]).endswith("Service")
            ]
            if service_candidates:
                findings.append(
                    {
                        "area": "service_api_candidates",
                        "source": "outline_index",
                        "count": len(service_candidates),
                        "top": [
                            {
                                "service": item["symbol"],
                                "path": item["path"],
                                **(
                                    {"namespace": item["namespace"]}
                                    if item.get("namespace")
                                    else {}
                                ),
                            }
                            for item in service_candidates[:4]
                        ],
                    },
                )

        # 2. Code files — which files mention this concept?
        code_files = self.store.search_code(project_root, needle, limit=code_limit)
        if code_files:
            roles = list(dict.fromkeys(f["role"] for f in code_files))[:4]
            findings.append(
                {
                    "area": "files",
                    "source": "file_index",
                    "count": len(code_files),
                    "top": [
                        {
                            "path": f["path"],
                            "role": f["role"],
                            "language": f.get("language"),
                            "language_tier": f.get("language_tier"),
                            "language_source": f.get("language_source"),
                        }
                        for f in code_files[:3]
                    ],
                    "roles_found": roles,
                },
            )
            next_tools.append(
                {"tool": "ai_get_outline", "why": "Understand structure of the top files"},
            )

        # 3. Schema — any entities/fields?
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            entities = schema.find_schema_entities(
                project_root,
                query=needle,
                limit=limit if depth_value != "deep" else limit * 2,
            )
            fields = schema.find_schema_field(
                project_root,
                needle,
                limit=limit if depth_value != "deep" else limit * 2,
            )
            if entities:
                findings.append(
                    {
                        "area": "schema_entities",
                        "source": "schema_index",
                        "count": len(entities),
                        "top": [
                            {
                                "entity": e["entity_name"],
                                "source": e.get("source_path", "").split("/")[-1],
                            }
                            for e in entities[:3]
                        ],
                    },
                )
                next_tools.append(
                    {
                        "tool": "schema_get_entity",
                        "why": "Get fields and relationships for matched entities",
                    },
                )
                next_tools.append(
                    {
                        "tool": "ai_get_symbol_info",
                        "why": "kind=properties — lightweight property list for matched entities/DTOs",
                    },
                )
            if fields:
                findings.append(
                    {
                        "area": "schema_fields",
                        "source": "schema_index",
                        "count": len(fields),
                        "top": [
                            {"field": f["field_name"], "entity": f["entity_name"]}
                            for f in fields[:3]
                        ],
                    },
                )
                next_tools.append(
                    {
                        "tool": "schema_trace_relationship_path",
                        "why": "Trace FK paths between entities",
                    },
                )
        except Exception:
            pass

        # 4. CSS — any style definitions?
        with self.store.connect(project_root) as conn:
            css_rows = conn.execute(
                "SELECT symbol, path FROM code_outlines WHERE kind = 'css_class' AND symbol LIKE ? LIMIT ?",
                (f"%{needle}%", limit),
            ).fetchall()
        if css_rows and focus_value in {"general", "ui"}:
            findings.append(
                {
                    "area": "css",
                    "source": "outline_index",
                    "count": len(css_rows),
                    "top": [{"class": r["symbol"], "path": r["path"]} for r in css_rows[:3]],
                },
            )
            next_tools.append(
                {"tool": "ai_trace", "why": "Find CSS definitions + HTML/Razor template usages"},
            )

        # 5. Modules — which module owns this?
        modules = self.store.get_modules(project_root)
        matching_modules = [
            m
            for m in modules
            if needle.lower() in m["name"].lower()
            or needle.lower() in (m.get("description") or "").lower()
        ]
        if matching_modules:
            findings.append(
                {
                    "area": "modules",
                    "source": "module_index",
                    "count": len(matching_modules),
                    "top": [
                        {"module": m["module_path"], "kind": m["kind"], "files": m["file_count"]}
                        for m in matching_modules[:3]
                    ],
                },
            )
            next_tools.append(
                {"tool": "ai_get_module_files", "why": "List files in the matching module"},
            )

        multi_word = len([part for part in re.split(r"\s+", needle) if part.strip()]) >= 2
        if multi_word or focus_value in {"workflow", "backend"} or depth_value == "deep":
            try:
                touchpoints = self.store.find_ui_backend_touchpoints(
                    project_root,
                    concept=concept,
                    limit=limit,
                )
                tp_matches = touchpoints.get("matches", []) if isinstance(touchpoints, dict) else []
                if tp_matches:
                    if focus_value == "backend":
                        tp_matches = [
                            item
                            for item in tp_matches
                            if item.get("layer") in {"api", "logic", "data"}
                        ]
                    elif focus_value == "ui":
                        tp_matches = [item for item in tp_matches if item.get("layer") == "ui"]
                    findings.append(
                        {
                            "area": "workflow_touchpoints",
                            "source": "outline_index",
                            "count": len(tp_matches),
                            "top": [
                                {
                                    "path": item["path"],
                                    "layer": item.get("layer"),
                                    "symbol": item.get("symbol"),
                                    "kind": item.get("kind"),
                                }
                                for item in tp_matches[:4]
                            ],
                        },
                    )
                    next_tools.append(
                        {
                            "tool": "ai_trace",
                            "why": "Trace the workflow across UI, logic, API, and backend ownership points",
                        },
                    )
            except Exception:
                pass
            try:
                routes = self.find_routes(project_root, query=concept, limit=limit)
                route_matches = routes.get("matches", []) if isinstance(routes, dict) else []
                if route_matches and focus_value in {"general", "workflow", "backend"}:
                    findings.append(
                        {
                            "area": "routes",
                            "source": "outline_index",
                            "count": len(route_matches),
                            "top": [
                                {"path": item["path"], "layer": item.get("layer")}
                                for item in route_matches[:3]
                            ],
                        },
                    )
            except Exception:
                pass
            try:
                policy = self.store.find_policy_surfaces(project_root, concept=concept, limit=limit)
                policy_matches = policy.get("matches", []) if isinstance(policy, dict) else []
                if policy_matches and focus_value in {"general", "workflow", "backend", "service"}:
                    findings.append(
                        {
                            "area": "policy_surfaces",
                            "source": "outline_index",
                            "count": len(policy_matches),
                            "top": [
                                {
                                    "path": item["path"],
                                    "layer": item.get("layer"),
                                    "symbol": item.get("symbol"),
                                }
                                for item in policy_matches[:3]
                            ],
                        },
                    )
            except Exception:
                pass

        # ── #462 evidence lanes — bounded, ranked, fail-quiet ────────────
        # The concept's resolved leaves: (path, symbol) contexts from the
        # symbol + file probes, consumed by the memory and LSP lanes.
        leaf_contexts: list[tuple[str, str | None]] = []
        for f in findings:
            if f["area"] == "symbols":
                for item in f.get("top") or []:  # type: ignore[union-attr]
                    if item.get("path"):
                        leaf_contexts.append(
                            (str(item["path"]), str(item.get("symbol") or "") or None),
                        )
            elif f["area"] == "files":
                for item in f.get("top") or []:  # type: ignore[union-attr]
                    if item.get("path"):
                        leaf_contexts.append((str(item["path"]), None))

        # 6. MEMORY-ANCHORS LANE — memories anchored at the surfaced leaves
        # (same substrate + fail-quiet contract as the edit rail).
        try:
            self._memory_anchor_lane(project_root, leaf_contexts, findings, next_tools)
        except Exception:
            pass

        # 7. PALACE-SEMANTIC LANE — kingdom + empire drawers, tier-labeled,
        # empire capped at 1/3 (the kingdom-protective interleave).
        if palace is not None:
            try:
                self._palace_semantic_lane(needle, project_root, palace, hub_ctx, findings)
            except Exception:
                pass

        # 8. BACKLOG + TODO LANES — active project/session work about this
        # concept. Todos are session-scoped; callers without a resolved managed
        # session simply omit that lane (no-padding law).
        try:
            self._backlog_lane(project_root, needle, findings, next_tools)
        except Exception:
            pass
        if session_id:
            try:
                self._todo_lane(project_root, str(session_id), needle, findings, next_tools)
            except Exception:
                pass

        # 9. LSP-ORACLE LANE — materialized semantic_ref answers only
        # (§XXXII: the guest's durable trace; an absent guest costs zero).
        try:
            self._lsp_oracle_lane(project_root, leaf_contexts, findings, next_tools)
        except Exception:
            pass

        # Deduplicate next_tools by tool name
        seen_tools: set[str] = set()
        unique_tools: list[dict[str, str]] = []
        for t in next_tools:
            if t["tool"] not in seen_tools:
                seen_tools.add(t["tool"])
                unique_tools.append(t)

        return {
            "findings": findings,
            "next_tools": unique_tools,
            "summary": (
                "Found: "
                + ", ".join(str(f["area"]) + "(" + str(f["count"]) + ")" for f in findings)
            )
            if findings
            else "No matches found.",
        }

    # ── #462 lane helpers — each bounded, each omitted when empty ────────

    def _memory_anchor_lane(
        self,
        project_root: Path,
        leaf_contexts: list[tuple[str, str | None]],
        findings: list[dict[str, object]],
        next_tools: list[dict[str, str]],
    ) -> None:
        """Anchored memories for the concept's resolved leaves (bounded 5).

        Consumes the leaf-anchoring substrate (memory_symbol_anchors via
        semantic_enrichment.memories_for_touched_leaf) — the SAME fail-quiet
        reader the edit rail uses. Leaf granularity is cited per entry.
        """
        from . import semantic_enrichment as _se

        cap = self._MEMORY_LANE_CAP
        entries: list[dict[str, object]] = []
        seen_memories: set[str] = set()
        seen_ctx: set[tuple[str, str | None]] = set()
        for path, symbol in leaf_contexts[:6]:
            if len(entries) >= cap:
                break
            key = (path, symbol)
            if key in seen_ctx:
                continue
            seen_ctx.add(key)
            for mem in _se.memories_for_touched_leaf(project_root, path, symbol, cap=cap):
                mem_path = str(mem.get("memory_path") or "")
                if not mem_path or mem_path in seen_memories:
                    continue
                seen_memories.add(mem_path)
                granularity = str(mem.get("granularity") or "") or "file"
                entries.append(
                    {
                        "memory": mem_path,
                        "title": str(mem.get("title") or ""),
                        "leaf": path,
                        "granularity": granularity,
                        "why": f"memory anchored to this leaf at {granularity} granularity",
                        "source": "memory_symbol_anchors",
                        "confidence": "anchored",
                    },
                )
                if len(entries) >= cap:
                    break
        if not entries:
            return
        findings.append(
            {
                "area": "anchored_memories",
                "source": "memory_symbol_anchors",
                "count": len(entries),
                "top": entries,
            },
        )
        next_tools.append(
            {
                "tool": "ai_memory",
                "why": "mode=read — anchored memories for the surfaced leaves carry prior rulings",
            },
        )

    def _palace_semantic_lane(
        self,
        needle: str,
        project_root: Path,
        palace: object,
        hub_ctx: object | None,
        findings: list[dict[str, object]],
    ) -> None:
        """Kingdom + empire palace semantic drawers for the concept.

        Bounded 5, labeled rebuildable_projection per the knowledge_source
        contract. Two-lane discipline: empire hits are hard-capped at 1/3 of
        the lane (max(1, cap // 3) — the scroll-reserve doctrine, mirroring
        memory_discovery's kingdom-protective interleave) so machine-global
        drawers can never crowd kingdom truth out.
        """
        from . import memory_discovery as _md

        cap = self._PALACE_LANE_CAP
        kingdom = list(_md._semantic_memory_hits(needle, project_root, palace, hub_ctx))[:cap]
        empire_cap = max(1, cap // 3)
        empire = list(_md._empire_semantic_hits(needle, project_root, limit=empire_cap))
        n_empire = min(len(empire), empire_cap)
        interleaved = [*kingdom[: cap - n_empire], *empire[:n_empire]]
        entries: list[dict[str, object]] = []
        seen: set[str] = set()
        for hint in interleaved:
            path = str(getattr(hint, "path", "") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            entries.append(
                {
                    "memory": path,
                    "why": str(getattr(hint, "why", "") or "palace semantic recall"),
                    "source": "palace",
                    "tier": str(getattr(hint, "tier", "") or "kingdom"),
                    "confidence": str(getattr(hint, "confidence", "") or "semantic_guess"),
                },
            )
            if len(entries) >= cap:
                break
        if not entries:
            return
        findings.append(
            {
                "area": "palace_semantic",
                "source": "palace",
                "knowledge_source": "rebuildable_projection",
                "count": len(entries),
                "top": entries,
            },
        )

    def _backlog_lane(
        self,
        project_root: Path,
        needle: str,
        findings: list[dict[str, object]],
        next_tools: list[dict[str, str]],
    ) -> None:
        """Active backlog items whose title/body/tags resolve to the concept
        (bounded 3) — "there is existing work about this" is navigation gold.
        """
        from . import project_backlog_store as _bk
        from .symbol_ranking import tokenize

        tokens = {t for t in tokenize(needle) if len(t) >= 3}
        if not tokens:
            return
        matches: list[tuple[int, list[str], dict[str, object]]] = []
        active_statuses = {"open", "in_progress", "blocked"}
        for item in _bk.list_backlog(project_root, limit=200):
            if str(item.get("status") or "open") not in active_statuses:
                continue
            haystack = (
                str(item.get("title") or "")
                + "\n"
                + str(item.get("content") or "")
            ).lower()
            tag_set = {str(t).lower() for t in (item.get("tags") or [])}
            matched = sorted(t for t in tokens if t in haystack or t in tag_set)
            if matched:
                matches.append((len(matched), matched, item))
        if not matches:
            return
        priority_rank = {
            "critical": 0,
            "urgent": 1,
            "high": 2,
            "normal": 3,
            "medium": 3,
            "low": 4,
            "idea": 5,
        }
        matches.sort(
            key=lambda m: (
                -m[0],
                priority_rank.get(str(m[2].get("priority") or "normal"), 3),
                -int(m[2].get("id") or 0),
            ),
        )
        top = [
            {
                "id": item.get("id"),
                "title": str(item.get("title") or ""),
                "status": str(item.get("status") or "open"),
                "priority": str(item.get("priority") or "normal"),
                "why": "open backlog item — matched: " + ", ".join(matched),
                "source": "project_backlog",
                "confidence": "keyword",
            }
            for _, matched, item in matches[: self._BACKLOG_LANE_CAP]
        ]
        findings.append(
            {
                "area": "open_backlog",
                "source": "project_backlog",
                "count": len(matches),
                "top": top,
            },
        )
        next_tools.append(
            {
                "tool": "ai_backlog",
                "why": "mode=get — an open war exists about this concept; read the item bodies",
            },
        )


    def _todo_lane(
        self,
        project_root: Path,
        session_id: str,
        needle: str,
        findings: list[dict[str, object]],
        next_tools: list[dict[str, str]],
    ) -> None:
        """Unresolved todos in the active managed session matching concept.

        Bounded and navigation-shaped: the full unresolved count remains visible,
        while only the top three rows ride the guide. A session boundary is
        mandatory so unrelated work from another conductor never leaks in.
        """
        from . import task_todos_store as _todo
        from .symbol_ranking import tokenize

        tokens = {t for t in tokenize(needle) if len(t) >= 3}
        if not tokens or not session_id:
            return
        matches: list[tuple[int, dict[str, object]]] = []
        for item in _todo.list_for_session_unresolved(
            project_root,
            session_id=session_id,
        ):
            content = str(item.get("content") or "").lower()
            tags = {str(t).lower() for t in (item.get("tags") or [])}
            matched = sorted(t for t in tokens if t in content or t in tags)
            if matched:
                enriched = dict(item)
                enriched["_matched"] = matched
                matches.append((len(matched), enriched))
        if not matches:
            return
        urgency_rank = {
            "critical": 0,
            "urgent": 1,
            "high": 2,
            "normal": 3,
            "low": 4,
        }
        matches.sort(
            key=lambda row: (
                -row[0],
                urgency_rank.get(str(row[1].get("urgency") or "normal"), 3),
                int(row[1].get("id") or 0),
            ),
        )
        top = [
            {
                "id": item.get("id"),
                "task_id": str(item.get("task_id") or ""),
                "content": str(item.get("content") or ""),
                "status": str(item.get("status") or "open"),
                "urgency": str(item.get("urgency") or "normal"),
                "why": "unresolved session todo — matched: "
                + ", ".join(item.get("_matched") or []),
                "source": "task_todos",
                "confidence": "keyword",
            }
            for _, item in matches[: self._TODO_LANE_CAP]
        ]
        findings.append(
            {
                "area": "open_todos",
                "source": "task_todos",
                "count": len(matches),
                "top": top,
            },
        )
        next_tools.append(
            {
                "tool": "ai_task",
                "why": "mode=list scope=session — unresolved todos exist for this concept",
            },
        )
    def _lsp_oracle_lane(
        self,
        project_root: Path,
        leaf_contexts: list[tuple[str, str | None]],
        findings: list[dict[str, object]],
        next_tools: list[dict[str, str]],
    ) -> None:
        """LSP guest-oracle MATERIALIZED answers for the concept's top hits.

        Reads only the durable trace (code_edges kind='semantic_ref', written
        by lsp.materialize's drain — keep the answers, not the machine). No
        live guest is consulted here: an absent/disabled guest costs zero
        (§XXXII fail-open). Live on-demand materialization for investigate's
        top hits is a named #462 tranche item (RED-skip test).
        """
        from .lsp.materialize import _dotted_module

        modules: list[str] = []
        seen: set[str] = set()
        for path, _symbol in leaf_contexts[:6]:
            dotted = _dotted_module(path)
            if dotted and dotted not in seen:
                seen.add(dotted)
                modules.append(dotted)
        if not modules:
            return
        entries: list[dict[str, object]] = []
        with self.store.connect(project_root) as conn:
            for module in modules[: self._LSP_LANE_CAP]:
                rows = conn.execute(
                    "SELECT source_path FROM code_edges "
                    "WHERE kind = 'semantic_ref' AND target = ? "
                    "ORDER BY source_path LIMIT 50",
                    (module,),
                ).fetchall()
                if not rows:
                    continue
                entries.append(
                    {
                        "module": module,
                        "referenced_by": len(rows),
                        "top_referencers": [str(r["source_path"]) for r in rows[:3]],
                        "why": "LSP guest-oracle materialized reference sites for this module",
                        "source": "code_edges:semantic_ref",
                        "confidence": "materialized",
                    },
                )
        if not entries:
            return
        findings.append(
            {
                "area": "lsp_semantic_refs",
                "source": "code_edges:semantic_ref",
                "count": len(entries),
                "top": entries,
            },
        )
        next_tools.append(
            {
                "tool": "ai_find",
                "why": "mode=references — walk the materialized reference sites symbol-by-symbol",
            },
        )
