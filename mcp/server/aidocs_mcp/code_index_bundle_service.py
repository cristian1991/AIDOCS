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

    def get_subsystem_bundle(self, project_root: Path, concept: str, limit: int = 20) -> dict[str, object]:
        # Use smaller per-category limits for a concise summary
        cat_limit = min(limit, 8)

        domain_cluster = self.store.find_domain_clusters(project_root, concept=concept, limit=cat_limit)
        touchpoints = self.store.find_ui_backend_touchpoints(project_root, concept=concept, limit=cat_limit)
        policy = self.store.find_policy_surfaces(project_root, concept=concept, limit=cat_limit)
        transitions = self.store.find_transition_points(project_root, concept=concept, limit=cat_limit)
        data_structures = self.store.find_data_structures(project_root, query=concept, limit=cat_limit)
        entrypoints = self.store.find_entrypoints(project_root, concept=concept, limit=cat_limit)

        # Strip verbose snippets and low-relevance results
        min_score = 40
        def slim(matches: list[dict[str, object]], require_score: bool = True) -> list[dict[str, object]]:
            return [
                {k: v for k, v in m.items() if k != "snippet"}
                for m in (matches or [])
                if not require_score or int(m.get("score", 0)) >= min_score
            ]

        return {
            "concept": concept,
            "domain_cluster": slim(domain_cluster.get("cluster", []), require_score=False),
            "touchpoints": slim(touchpoints.get("matches", [])),
            "policy_surfaces": slim(policy.get("matches", [])),
            "transition_points": slim(transitions.get("matches", [])),
            "data_structures": slim(data_structures if isinstance(data_structures, list) else data_structures.get("result", []), require_score=False),
            "entrypoints": slim(entrypoints.get("matches", [])),
        }

    def investigate(
        self,
        project_root: Path,
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
    ) -> dict[str, object]:
        """High-level investigation entry point. Returns a navigation guide, not full data.

        Runs quick probes across symbols, code files, schema, CSS, and modules,
        then returns a ranked summary of what was found and which tools to call next.
        """
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "findings": [], "next_tools": []}

        depth_value = depth.strip().lower()
        focus_value = focus.strip().lower()
        if depth_value not in {"shallow", "standard", "deep"}:
            depth_value = "standard"
        if focus_value not in {"general", "workflow", "service", "schema", "ui", "backend"}:
            focus_value = "general"

        symbol_limit = limit if depth_value == "shallow" else (limit * 2 if depth_value == "deep" else limit)
        code_limit = limit if depth_value == "shallow" else (limit * 2 if depth_value == "deep" else limit)

        findings: list[dict[str, object]] = []
        next_tools: list[dict[str, str]] = []

        # 1. Symbol search — are there named symbols?
        symbols = self.store.search_symbols(project_root, needle, limit=symbol_limit)
        if symbols:
            top_kinds = list(dict.fromkeys(s["kind"] for s in symbols))[:3]
            preview_symbols: list[dict[str, object]] = []
            preview_symbols.extend(symbols[:3])
            for preferred_kind in ("method", "enum", "record", "class"):
                extra = next((item for item in symbols if item["kind"] == preferred_kind and item not in preview_symbols), None)
                if extra is not None:
                    preview_symbols.append(extra)
            top = []
            for s in preview_symbols[:6]:
                item = {"symbol": s["symbol"], "kind": s["kind"], "path": s["path"]}
                if s.get("namespace"):
                    item["namespace"] = s["namespace"]
                if s["kind"] == "method":
                    signature = self.store._extract_method_signature(project_root, str(s["path"]), int(s["line_number"]))
                    if signature.get("signature"):
                        item["signature"] = signature["signature"]
                elif s["kind"] == "enum":
                    item["enum_values"] = self.store._enum_members_for_container(project_root, str(s["path"]), str(s["symbol"]))[:8]
                top.append(item)
            findings.append({
                "area": "symbols",
                "source": "outline_index",
                "count": len(symbols),
                "top": top,
                "kinds_found": top_kinds,
            })
            next_tools.append({"tool": "code_search_symbols", "why": f"Found {len(symbols)} symbols — search for specific names/kinds"})
            if any(s["kind"] in ("class", "interface", "struct", "record") for s in symbols):
                next_tools.append({"tool": "code_find_references", "why": "Trace where these types are used"})
            if any(s["kind"] == "method" for s in symbols):
                next_tools.append({"tool": "code_get_method_signature", "why": "Read exact method params/returns before calling methods"})
            if any(str(s["symbol"]).endswith("Service") and s["kind"] == "class" for s in symbols):
                next_tools.append({"tool": "code_get_service_api", "why": "Read all public method signatures for a service before writing workflow-heavy code or tests"})
            if any(s["kind"] == "enum" for s in symbols):
                next_tools.append({"tool": "code_get_enum_values", "why": "Read exact enum members before using enum values"})

            service_candidates = [s for s in symbols if s["kind"] == "class" and str(s["symbol"]).endswith("Service")]
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
                                **({"namespace": item["namespace"]} if item.get("namespace") else {}),
                            }
                            for item in service_candidates[:4]
                        ],
                    }
                )

        # 2. Code files — which files mention this concept?
        code_files = self.store.search_code(project_root, needle, limit=code_limit)
        if code_files:
            roles = list(dict.fromkeys(f["role"] for f in code_files))[:4]
            findings.append({
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
            })
            next_tools.append({"tool": "code_get_outline", "why": "Understand structure of the top files"})

        # 3. Schema — any entities/fields?
        try:
            from .schema_index_store import SchemaIndexStore
            schema = SchemaIndexStore()
            entities = schema.find_schema_entities(project_root, query=needle, limit=limit if depth_value != "deep" else limit * 2)
            fields = schema.find_schema_field(project_root, needle, limit=limit if depth_value != "deep" else limit * 2)
            if entities:
                findings.append({
                    "area": "schema_entities",
                    "source": "schema_index",
                    "count": len(entities),
                    "top": [{"entity": e["entity_name"], "source": e.get("source_path", "").split("/")[-1]} for e in entities[:3]],
                })
                next_tools.append({"tool": "schema_get_entity", "why": "Get fields and relationships for matched entities"})
                next_tools.append({"tool": "code_get_entity_properties", "why": "Read a lightweight property list for matched entities or DTOs"})
            if fields:
                findings.append({
                    "area": "schema_fields",
                    "source": "schema_index",
                    "count": len(fields),
                    "top": [{"field": f["field_name"], "entity": f["entity_name"]} for f in fields[:3]],
                })
                next_tools.append({"tool": "schema_trace_relationship_path", "why": "Trace FK paths between entities"})
        except Exception:
            pass

        # 4. CSS — any style definitions?
        with self.store.connect(project_root) as conn:
            css_rows = conn.execute(
                "SELECT symbol, path FROM code_outlines WHERE kind = 'css_class' AND symbol LIKE ? LIMIT ?",
                (f"%{needle}%", limit),
            ).fetchall()
        if css_rows and focus_value in {"general", "ui"}:
            findings.append({
                "area": "css",
                "source": "outline_index",
                "count": len(css_rows),
                "top": [{"class": r["symbol"], "path": r["path"]} for r in css_rows[:3]],
            })
            next_tools.append({"tool": "code_trace_css_class", "why": "Find CSS definitions + HTML/Razor template usages"})

        # 5. Modules — which module owns this?
        modules = self.store.get_modules(project_root)
        matching_modules = [m for m in modules if needle.lower() in m["name"].lower() or needle.lower() in (m.get("description") or "").lower()]
        if matching_modules:
            findings.append({
                "area": "modules",
                "source": "module_index",
                "count": len(matching_modules),
                "top": [{"module": m["module_path"], "kind": m["kind"], "files": m["file_count"]} for m in matching_modules[:3]],
            })
            next_tools.append({"tool": "code_get_module_files", "why": "List files in the matching module"})

        multi_word = len([part for part in re.split(r"\s+", needle) if part.strip()]) >= 2
        if multi_word or focus_value in {"workflow", "backend"} or depth_value == "deep":
            try:
                touchpoints = self.store.find_ui_backend_touchpoints(project_root, concept=concept, limit=limit)
                tp_matches = touchpoints.get("matches", []) if isinstance(touchpoints, dict) else []
                if tp_matches:
                    if focus_value == "backend":
                        tp_matches = [item for item in tp_matches if item.get("layer") in {"api", "logic", "data"}]
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
                        }
                    )
                    next_tools.append({"tool": "code_trace_api_to_ui", "why": "Trace the workflow across UI, logic, API, and backend ownership points"})
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
                            "top": [{"path": item["path"], "layer": item.get("layer")} for item in route_matches[:3]],
                        }
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
                            "top": [{"path": item["path"], "layer": item.get("layer"), "symbol": item.get("symbol")} for item in policy_matches[:3]],
                        }
                    )
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
            "concept": concept,
            "depth": depth_value,
            "focus": focus_value,
            "findings": findings,
            "next_tools": unique_tools,
            "summary": ("Found: " + ", ".join(str(f["area"]) + "(" + str(f["count"]) + ")" for f in findings)) if findings else "No matches found.",
        }
