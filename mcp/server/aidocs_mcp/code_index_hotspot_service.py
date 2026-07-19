from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class CodeIndexHotspotService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def find_hotspots(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 30,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = (query or "").strip()
        if needle:
            self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 6)

        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT cf.path, cf.language, cf.line_count, cf.summary, cf.role,
                       COUNT(DISTINCT co.symbol) AS outline_count,
                       COUNT(DISTINCT ce.target) AS dependency_count
                FROM code_files cf
                LEFT JOIN code_outlines co ON co.path = cf.path
                LEFT JOIN code_edges ce ON ce.source_path = cf.path
                GROUP BY cf.path, cf.language, cf.line_count, cf.summary, cf.role
                ORDER BY cf.path ASC
                """,
            ).fetchall()

        hotspots: list[dict[str, object]] = []
        signal_tokens = (
            "legacy",
            "migration",
            "adapter",
            "compat",
            "validator",
            "validate",
            "policy",
            "permission",
            "async",
            "queue",
            "builder",
        )
        for row in rows:
            path = str(row["path"])
            lower_path = path.lower()
            lower_summary = str(row["summary"] or "").lower()
            score = 0
            reasons: list[str] = []

            if needle:
                score += self.store._score_text_match(
                    needle,
                    path,
                    exact=60,
                    prefix=35,
                    contains=20,
                    reasons=reasons,
                    label="path",
                )
                score += self.store._score_text_match(
                    needle,
                    str(row["summary"] or ""),
                    exact=25,
                    prefix=15,
                    contains=8,
                    reasons=reasons,
                    label="summary",
                )

            outline_count = int(row["outline_count"] or 0)
            dependency_count = int(row["dependency_count"] or 0)
            line_count = int(row["line_count"] or 0)

            score += min(outline_count, 20) * 4
            score += min(dependency_count, 20) * 5
            score += min(line_count // 40, 20) * 2
            if outline_count:
                reasons.append(f"outline_count:{outline_count}")
            if dependency_count:
                reasons.append(f"dependency_count:{dependency_count}")
            if line_count:
                reasons.append(f"line_count:{line_count}")

            role = row["role"] or "unknown"
            role_bonus = {
                "service": 25,
                "controller": 20,
                "context-provider": 20,
                "component": 15,
                "page": 18,
                "layout": 12,
            }.get(role, 0)
            score += role_bonus
            if role_bonus:
                reasons.append(f"role:{role}")

            token_bonus = 0
            for token in signal_tokens:
                if token in lower_path or token in lower_summary:
                    token_bonus += 8
            score += token_bonus
            if token_bonus:
                reasons.append(f"signal_bonus:{token_bonus}")

            score += self.store._path_weight(project_root, path)
            # Always emit per-match `why` (canonical 2026-04-30): the
            # ranking reasons are the operator's primary debugging
            # signal for "why did this file rank #N?" Pre-fix the
            # field was gated behind AIDOCS_SHOW_RANKER_TRACE=1 env
            # var, which made it useless for normal calls. The field
            # is small (a handful of short strings); top-level
            # `why` (trace summary) stays as the result-wide signal.
            hotspot_row: dict[str, object] = {
                "path": path,
                "language": row["language"],
                "role": role,
                "line_count": line_count,
                "outline_count": outline_count,
                "dependency_count": dependency_count,
                "score": score,
                "why": reasons,
            }
            hotspots.append(hotspot_row)

        hotspots.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
        limited = hotspots[:limit]
        return {
            "matches": limited,
            "confidence": self.store._trace_confidence(limited),
            "why": self.store._trace_summary(limited),
        }

    def find_query_hotspots(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 30,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = (query or "").strip()
        if needle:
            self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT path, language, line_count, summary, role FROM code_files ORDER BY path",
            ).fetchall()

        results: list[dict[str, object]] = []
        for row in rows:
            path = str(row["path"])
            abs_path = project_root / path
            if not abs_path.is_file():
                continue
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            lower = text.lower()
            lower_path = path.lower()
            line_count = len(text.splitlines())

            score = 0
            reasons: list[str] = []
            if needle:
                score += self.store._score_text_match(
                    needle,
                    path,
                    exact=50,
                    prefix=30,
                    contains=20,
                    reasons=reasons,
                    label="path",
                )
                score += self.store._score_text_match(
                    needle,
                    str(row["summary"] or ""),
                    exact=20,
                    prefix=10,
                    contains=5,
                    reasons=reasons,
                    label="summary",
                )

            include_count = lower.count(".include(") + lower.count(".theninclude(")
            join_count = (
                lower.count(".join(")
                + lower.count(".groupjoin(")
                + lower.count(" left join ")
                + lower.count(" right join ")
                + lower.count(" inner join ")
            )
            projection_count = lower.count(".select(") + lower.count(".selectmany(")
            filter_count = (
                lower.count(".where(") + lower.count(".orderby(") + lower.count(".groupby(")
            )
            sql_count = lower.count("fromsql") + lower.count("select *")

            if include_count:
                score += include_count * 12
                reasons.append(f"includes:{include_count}")
            if join_count:
                score += join_count * 15
                reasons.append(f"joins:{join_count}")
            if projection_count:
                score += projection_count * 8
                reasons.append(f"projections:{projection_count}")
            if filter_count:
                score += min(filter_count, 20) * 4
                reasons.append(f"filters:{filter_count}")
            if sql_count:
                score += sql_count * 18
                reasons.append(f"raw_sql:{sql_count}")

            if line_count:
                line_bonus = min(line_count // 60, 15) * 3
                score += line_bonus
                reasons.append(f"line_count:{line_count}")

            role = row["role"] or "unknown"
            role_bonus = {
                "service": 20,
                "controller": 15,
                "data-model": 10,
            }.get(role, 0)
            if role_bonus:
                score += role_bonus
                reasons.append(f"role:{role}")

            path_weight = self.store._path_weight(project_root, path)
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")

            if (
                row["language"] == "javascript"
                and role == "unknown"
                and any(
                    token in lower_path
                    for token in ("/assets/", "/pwaassets/", "/vendor/", "/vendors/")
                )
            ):
                score -= 250
                reasons.append("third_party_asset_penalty:250")

            if score <= 0:
                continue

            # Always emit per-match `why` — see find_hotspots above
            # for the rationale (operator's primary "why did this
            # rank #N?" debugging signal; cost is small).
            result_row: dict[str, object] = {
                "path": path,
                "language": row["language"],
                "role": role,
                "line_count": line_count,
                "score": score,
                "why": reasons,
            }
            results.append(result_row)

        results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
        limited = results[:limit]
        return {
            "matches": limited,
            "confidence": self.store._trace_confidence(limited),
            "why": self.store._trace_summary(limited),
        }

    def trace_component_usage(
        self,
        project_root: Path,
        component_name: str,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = component_name.strip()
        if not needle:
            return {"definitions": [], "references": [], "neighbors": []}

        self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        definitions = [
            item
            for item in self.find_frontend_symbols(project_root, query=needle, limit=limit)
            if str(item.get("symbol") or "") == needle
        ]
        references = self.store.find_references(project_root, symbol=needle, limit=limit)["matches"]

        neighbors: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for definition in definitions[: min(len(definitions), 5)]:
            path = str(definition["path"])
            tree = self.store.get_component_tree(project_root, path=path, depth=1, limit=limit)
            for node in tree.get("nodes", []):
                node_path = str(node["path"])
                if node_path == path or node_path in seen_paths:
                    continue
                seen_paths.add(node_path)
                neighbors.append(node)

        match_count = len(definitions) + len(references) + len(neighbors[:limit])
        return {
            "definitions": definitions,
            "references": references,
            "neighbors": neighbors[:limit],
            "confidence": "high" if match_count >= 4 else "medium" if match_count >= 2 else "low",
            "why": [
                f"definitions:{len(definitions)}",
                f"references:{len(references)}",
                f"neighbors:{len(neighbors[:limit])}",
            ],
        }

    def find_state_model_mismatch(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"matches": []}

        symbol_matches = self.store.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.store.search_code(project_root, query=needle, limit=limit)
        lower_concept = needle.lower()

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            kind = str(item["kind"])
            lower_symbol = symbol.lower()

            mismatch_type = None
            score = 0
            if kind == "enum":
                mismatch_type = "enum_state_model"
                score = 120
            elif lower_symbol.startswith("is") and len(symbol) > 2:
                mismatch_type = "boolean_flag_model"
                score = 110
            elif any(
                token in lower_symbol
                for token in ("status", "state", "type", "mode", "kind", "flag")
            ):
                mismatch_type = "named_state_field"
                score = 90
            elif lower_concept in lower_symbol:
                mismatch_type = "concept_match"
                score = 70

            if mismatch_type is None:
                continue

            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.store.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=kind,
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None

            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "layer": self.store._infer_layer_from_path(path),
                    "mismatch_type": mismatch_type,
                    "snippet": snippet["snippet"] if snippet else None,
                },
            )

        for item in code_matches:
            path = str(item["path"])
            key = (path, None, None)
            if key in seen:
                continue
            lower_path = path.lower()
            mismatch_type = None
            score = 0
            if any(token in lower_path for token in ("enum", "status", "state", "type", "flag")):
                mismatch_type = "file_state_hint"
                score = 50
            elif lower_concept in lower_path:
                mismatch_type = "concept_file_match"
                score = 40
            if mismatch_type is None:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "layer": self.store._infer_layer_from_path(path),
                    "mismatch_type": mismatch_type,
                    "snippet": item["summary"],
                },
            )

        merged.sort(
            key=lambda item: (
                -int(item["score"]),
                self.store._layer_rank(str(item["layer"])),
                str(item["path"]),
                item["line_number"] or 0,
            ),
        )
        return {"matches": merged[:limit]}

    def find_routes(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        return self.store.find_routes(project_root, query=query, limit=limit)

    def trace_api_to_ui(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        return self.store.trace_api_to_ui(project_root, concept, limit=limit)

    def find_ui_backend_touchpoints(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        return self.store.find_ui_backend_touchpoints(project_root, concept, limit=limit)

    def find_policy_surfaces(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        return self.store.find_policy_surfaces(project_root, concept, limit=limit)

    def find_entrypoints(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        return self.store.find_entrypoints(project_root, concept, limit=limit)

    def find_factories(
        self,
        project_root: Path,
        query: str,
        include_tests: bool = True,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        if include_tests:
            self.store.sync_code_files(project_root, include_tests=True)
        needle = query.strip()
        symbol_matches = []
        seen: set[tuple[str, str, int]] = set()
        for term in [needle, "Create", "Factory"]:
            if not term:
                continue
            for item in self.store.search_symbols(project_root, term, limit=max(limit * 3, 50)):
                key = (
                    str(item.get("path")),
                    str(item.get("symbol")),
                    int(item.get("line_number") or 0),
                )
                if key not in seen:
                    seen.add(key)
                    symbol_matches.append(item)

        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE kind IN ('method', 'function', 'class', 'record')
                  AND (symbol LIKE 'Create%' OR symbol LIKE '%Factory%')
                ORDER BY path, line_number
                LIMIT ?
                """,
                (max(limit * 4, 50),),
            ).fetchall()
        for row in rows:
            item = {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
            }
            key = (
                str(item.get("path")),
                str(item.get("symbol")),
                int(item.get("line_number") or 0),
            )
            if key not in seen:
                seen.add(key)
                symbol_matches.append(item)
        factory_matches = []
        for item in symbol_matches:
            symbol = str(item.get("symbol") or "")
            path = str(item.get("path") or "")
            if not symbol:
                continue
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            if (
                lower_symbol.startswith("create")
                or "factory" in lower_symbol
                or "factory" in lower_path
                or "/test" in lower_path
                or "tests/" in lower_path
            ):
                score = 0
                if needle:
                    lower_needle = needle.lower()
                    if lower_needle in lower_symbol:
                        score += 100
                    if lower_needle in lower_path:
                        score += 60
                if lower_symbol.startswith("create"):
                    score += 20
                if "factory" in lower_symbol:
                    score += 20
                if "factory" in lower_path:
                    score += 10
                if "/test" in lower_path or "tests/" in lower_path:
                    score += 5
                factory_matches.append({**item, "score": score})
        if len(factory_matches) < limit:
            file_matches = []
            for term in [needle, "Create", "Factory"]:
                if not term:
                    continue
                for item in self.store.search_code(project_root, term, limit=max(limit * 3, 50)):
                    path = str(item.get("path") or "")
                    lower_path = path.lower()
                    summary = str(item.get("summary") or "").lower()
                    if (
                        "factory" in lower_path
                        or "factory" in summary
                        or "tests/" in lower_path
                        or "/test" in lower_path
                        or "create" in summary
                    ):
                        score = 0
                        if needle:
                            lower_needle = needle.lower()
                            if lower_needle in lower_path:
                                score += 60
                            if lower_needle in summary:
                                score += 40
                        if "factory" in lower_path or "factory" in summary:
                            score += 20
                        if "create" in summary:
                            score += 10
                        file_matches.append(
                            {
                                "path": path,
                                "symbol": None,
                                "kind": "file_match",
                                "line_number": None,
                                "why": ["factory_file_fallback"],
                                "score": score,
                            },
                        )
            for item in file_matches:
                key = (
                    str(item.get("path")),
                    str(item.get("symbol")),
                    int(item.get("line_number") or 0),
                )
                if key not in seen:
                    seen.add(key)
                    factory_matches.append(item)
        factory_matches.sort(
            key=lambda item: (
                -int(item.get("score", 0)),
                str(item.get("path") or ""),
                str(item.get("symbol") or ""),
            ),
        )
        return {"matches": factory_matches[:limit]}

    def find_partial_consumers(
        self,
        project_root: Path,
        partial_name: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """Find all files that reference a partial (via partial_ref outline kind)."""
        self.store.init_db(project_root)
        needle = partial_name.strip().lstrip("_")
        if not needle:
            return []
        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT co.path, co.symbol, co.line_number, cf.role
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                WHERE co.kind = 'partial_ref' AND co.symbol LIKE ?
                ORDER BY co.path, co.line_number
                LIMIT ?
                """,
                (f"%{needle}%", limit),
            ).fetchall()
        return [
            {
                "path": r["path"],
                "symbol": r["symbol"],
                "line_number": r["line_number"],
                "role": r["role"],
            }
            for r in rows
        ]

    def find_api_consumers(
        self,
        project_root: Path,
        endpoint: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """Find all files that call an API endpoint (via api_call outline kind)."""
        self.store.init_db(project_root)
        needle = endpoint.strip()
        if not needle:
            return []
        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT co.path, co.symbol, co.line_number, cf.role
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                WHERE co.kind = 'api_call' AND co.symbol LIKE ?
                ORDER BY co.path, co.line_number
                LIMIT ?
                """,
                (f"%{needle}%", limit),
            ).fetchall()
        return [
            {
                "path": r["path"],
                "endpoint": r["symbol"],
                "line_number": r["line_number"],
                "role": r["role"],
            }
            for r in rows
        ]

    def trace_css_class_usage(
        self,
        project_root: Path,
        class_name: str,
        limit: int = 50,
    ) -> dict[str, object]:
        """Find CSS class definitions AND HTML/Razor template files that likely use this class."""
        self.store.init_db(project_root)
        needle = class_name.strip()
        if not needle:
            return {"class_name": class_name, "definitions": [], "usages": []}

        with self.store.connect(project_root) as conn:
            # Definitions: from CSS outlines
            def_rows = conn.execute(
                "SELECT co.path, co.symbol, co.line_number FROM code_outlines co WHERE co.kind = 'css_class' AND co.symbol = ? ORDER BY co.path LIMIT ?",
                (needle, limit),
            ).fetchall()

            # Usages: search actual file content for the class name in template files
            template_rows = conn.execute(
                """
                SELECT path, language, role
                FROM code_files
                WHERE language IN ('razor', 'html', 'jsx', 'tsx', 'vue', 'svelte', 'javascript', 'typescript')
                ORDER BY path
                """,
            ).fetchall()

        usages: list[dict[str, object]] = []
        class_pattern = re.compile(
            rf"(?:class(?:Name)?=[\"\'][^\"\']*\b{re.escape(needle)}\b|@class\([^)]*\b{re.escape(needle)}\b|\bAddCssClass\([^)]*{re.escape(needle)})",
        )
        for row in template_rows:
            if len(usages) >= limit:
                break
            abs_path = project_root / row["path"]
            if not abs_path.is_file():
                continue
            try:
                text = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lines_found: list[int] = []
            for line_num, line in enumerate(text.splitlines(), start=1):
                if needle in line:
                    # Match: class attributes, querySelector, string references, or CSS selectors
                    if (
                        class_pattern.search(line)
                        or f'"{needle}"' in line
                        or f"'{needle}'" in line
                        or f".{needle}" in line
                        or f"#{needle}" in line
                    ):
                        lines_found.append(line_num)
                        if len(lines_found) >= 3:
                            break
            if lines_found:
                usages.append(
                    {
                        "path": row["path"],
                        "role": row["role"],
                        "language": row["language"],
                        "lines": lines_found,
                        "count": sum(1 for line in text.splitlines() if needle in line),
                    },
                )

        # Sort by usage count descending
        usages.sort(key=lambda u: -u.get("count", 0))

        return {
            "class_name": class_name,
            "definitions": [
                {"path": r["path"], "symbol": r["symbol"], "line_number": r["line_number"]}
                for r in def_rows
            ],
            "usages": usages[:limit],
        }

    def find_domain_clusters(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"cluster": []}
        self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        code_matches = self.store.search_code(project_root, needle, limit=limit)
        symbol_matches = self.store.search_symbols(project_root, needle, limit=limit)
        schema_entities = []
        schema_fields = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            schema_entities = schema.find_schema_entities(project_root, query=needle, limit=limit)
            schema_fields = schema.find_schema_field(project_root, needle, limit=limit)
        except Exception:
            pass

        cluster: list[dict[str, object]] = []
        seen: set[tuple[str, str | None]] = set()

        for item in symbol_matches:
            key = (str(item["path"]), str(item["symbol"]))
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "symbol",
                    "path": item["path"],
                    "layer": self.store._infer_layer_from_path(str(item["path"])),
                    "symbol": item["symbol"],
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                },
            )

        for item in code_matches:
            key = (str(item["path"]), None)
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "file",
                    "path": item["path"],
                    "layer": self.store._infer_layer_from_path(str(item["path"])),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                },
            )

        for item in schema_entities:
            key = (str(item["path"]), str(item["entity_name"]))
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "schema_entity",
                    "path": item["path"],
                    "layer": self.store._infer_layer_from_path(str(item["path"])),
                    "symbol": item["entity_name"],
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                },
            )

        for item in schema_fields:
            key = (str(item["path"]), str(item["field_name"]))
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "schema_field",
                    "path": item["path"],
                    "layer": self.store._infer_layer_from_path(str(item["path"])),
                    "symbol": item["field_name"],
                    "kind": item.get("kind") or item.get("field_kind", ""),
                    "line_number": item["line_number"],
                },
            )

        cluster.sort(
            key=lambda item: (
                self.store._layer_rank(str(item["layer"])),
                str(item["path"]),
                str(item["symbol"] or ""),
            ),
        )
        return {"cluster": cluster[:limit]}

    def find_duplicate_structures(
        self,
        project_root: Path,
        role_filter: str | None = None,
        kind_filter: str | None = None,
        min_shared: int = 3,
        limit: int = 30,
    ) -> dict[str, object]:
        """Find files with overlapping outline symbols — candidates for extraction into shared partials/components.

        Groups files by shared symbol fingerprints (same symbol name + kind appearing in multiple files).
        Returns clusters of files that share enough structure to warrant extraction.

        Args:
            role_filter: Only consider files with this role (e.g., "page-view", "partial-view").
            kind_filter: Only consider outline symbols of this kind (e.g., "translation_key", "partial_ref", "js_function").
            min_shared: Minimum number of files sharing a symbol to be considered duplicate (default 3).
            limit: Maximum number of clusters to return.

        """
        self.store.init_db(project_root)

        with self.store.connect(project_root) as conn:
            # Step 1: Find symbols that appear in multiple files
            kind_clause = ""
            params: list[object] = [min_shared]
            if kind_filter:
                kind_clause = "AND co.kind = ?"
                params.insert(0, kind_filter)

            role_clause = ""
            if role_filter:
                role_clause = "AND cf.role = ?"
                params.insert(0, role_filter)

            shared_symbols = conn.execute(
                f"""
                SELECT co.symbol, co.kind, COUNT(DISTINCT co.path) AS file_count,
                       GROUP_CONCAT(DISTINCT co.path) AS files
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                WHERE 1=1 {role_clause} {kind_clause}
                GROUP BY co.symbol, co.kind
                HAVING COUNT(DISTINCT co.path) >= ?
                ORDER BY file_count DESC
                LIMIT 200
                """,
                params,
            ).fetchall()

            # FP suppression (2026-07-16, narrow by design): minifier output
            # pairs bundles on 1-2 char symbols (i, s, t...) and generated /
            # vendored assets are not authored code. Real first-party
            # duplication always survives this filter.
            from .slop_ignore import is_ignored_path, looks_minified_symbol

            filtered = []
            for row in shared_symbols:
                if looks_minified_symbol(str(row["symbol"])):
                    continue
                files = [
                    f.strip()
                    for f in str(row["files"]).split(",")
                    if f.strip() and not is_ignored_path(f.strip())
                ]
                if len(files) < min_shared:
                    continue
                filtered.append(
                    {
                        "symbol": row["symbol"],
                        "kind": row["kind"],
                        "file_count": len(files),
                        "files": ",".join(files),
                    }
                )
            shared_symbols = filtered

            if not shared_symbols:
                return {
                    "clusters": [],
                    "summary": "No duplicate structures found with the given filters.",
                }

            # Step 2: Build file → shared-symbols map for clustering
            file_symbols: dict[str, list[dict[str, object]]] = {}
            for row in shared_symbols:
                files = str(row["files"]).split(",")
                for f in files:
                    f = f.strip()
                    if f not in file_symbols:
                        file_symbols[f] = []
                    file_symbols[f].append(
                        {
                            "symbol": row["symbol"],
                            "kind": row["kind"],
                            "shared_with": int(row["file_count"]),
                        },
                    )

            # Step 3: Find file pairs/groups with high overlap
            pair_scores: dict[tuple[str, str], list[str]] = {}
            for row in shared_symbols:
                files = [f.strip() for f in str(row["files"]).split(",")]
                for i in range(len(files)):
                    for j in range(i + 1, len(files)):
                        pair = (files[i], files[j]) if files[i] < files[j] else (files[j], files[i])
                        if pair not in pair_scores:
                            pair_scores[pair] = []
                        pair_scores[pair].append(f"{row['kind']}:{row['symbol']}")

            # Step 4: Sort pairs by overlap count, build clusters
            sorted_pairs = sorted(pair_scores.items(), key=lambda x: len(x[1]), reverse=True)

            clusters: list[dict[str, object]] = []
            for (file_a, file_b), shared in sorted_pairs[:limit]:
                # Get roles for context
                role_a = conn.execute(
                    "SELECT role FROM code_files WHERE path = ?",
                    (file_a,),
                ).fetchone()
                role_b = conn.execute(
                    "SELECT role FROM code_files WHERE path = ?",
                    (file_b,),
                ).fetchone()

                # Categorize shared symbols by kind
                by_kind: dict[str, list[str]] = {}
                for s in shared:
                    kind, sym = s.split(":", 1)
                    if kind not in by_kind:
                        by_kind[kind] = []
                    by_kind[kind].append(sym)

                clusters.append(
                    {
                        "files": [file_a, file_b],
                        "roles": [
                            role_a["role"] if role_a else None,
                            role_b["role"] if role_b else None,
                        ],
                        "shared_count": len(shared),
                        "shared_by_kind": {
                            k: v
                            for k, v in sorted(
                                by_kind.items(),
                                key=lambda x: len(x[1]),
                                reverse=True,
                            )
                        },
                    },
                )

            # Step 5: Also report the most duplicated individual symbols
            top_symbols = [
                {
                    "symbol": row["symbol"],
                    "kind": row["kind"],
                    "file_count": int(row["file_count"]),
                    "files": [f.strip() for f in str(row["files"]).split(",")],
                }
                for row in shared_symbols[:20]
            ]

        return {
            "clusters": clusters,
            "top_shared_symbols": top_symbols,
            "summary": f"Found {len(clusters)} file pairs with shared structures, {len(shared_symbols)} symbols appearing in {min_shared}+ files.",
        }

    def find_transition_points(
        self,
        project_root: Path,
        concept: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = (concept or "").strip()
        dot_parts = [part.strip() for part in needle.split(".") if part.strip()] if needle else []
        compound_terms = dot_parts if len(dot_parts) >= 2 else []
        seed_query = dot_parts[0] if compound_terms else (needle or "legacy")
        if needle:
            self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        code_matches = self.store.search_code(project_root, seed_query, limit=max(limit * 3, 100))
        symbol_matches = self.store.search_symbols(
            project_root,
            seed_query,
            limit=max(limit * 3, 100),
        )

        transition_tokens = (
            "legacy",
            "migration",
            "migrate",
            "adapter",
            "compat",
            "compatibility",
            "bridge",
            "shim",
            "deprecated",
            "fallback",
            "transitional",
        )

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            score = 0
            if needle:
                score += self.store._score_text_match(
                    needle,
                    symbol,
                    exact=100,
                    prefix=70,
                    contains=40,
                )
                score += self.store._score_text_match(
                    needle,
                    path,
                    exact=50,
                    prefix=30,
                    contains=20,
                )
            if compound_terms:
                term_hits = 0
                for term in compound_terms:
                    lower_term = term.lower()
                    if (
                        lower_term in lower_symbol
                        or lower_term in lower_path
                        or lower_term in str(item.get("container") or "").lower()
                    ):
                        term_hits += 1
                if term_hits:
                    score += term_hits * 35
            for token in transition_tokens:
                if token in lower_symbol:
                    score += 35
                if token in lower_path:
                    score += 20
            if score <= 0:
                continue
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.store.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=str(item["kind"]),
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": self.store._infer_layer_from_path(path),
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                },
            )

        broad_single = bool(needle and len([p for p in re.split(r"\s+", needle) if p.strip()]) == 1)
        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            lower_summary = str(item["summary"] or "").lower()
            score = 0
            if needle:
                score += self.store._score_text_match(
                    needle,
                    path,
                    exact=50,
                    prefix=30,
                    contains=20,
                )
            if compound_terms:
                term_hits = 0
                for term in compound_terms:
                    lower_term = term.lower()
                    if lower_term in lower_path or lower_term in lower_summary:
                        term_hits += 1
                if term_hits:
                    score += term_hits * 25
            for token in transition_tokens:
                if token in lower_path:
                    score += 30
                if token in lower_summary:
                    score += 15
            if score <= 0:
                continue
            if broad_single and score < 45:
                continue
            if compound_terms and not any(
                term.lower() in lower_path or term.lower() in lower_summary
                for term in compound_terms
            ):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": self.store._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                },
            )

        merged.sort(
            key=lambda item: (
                -int(item["score"]),
                self.store._layer_rank(str(item["layer"])),
                str(item["path"]),
                item["line_number"] or 0,
            ),
        )
        return {"matches": merged[:limit]}

    def get_outline(self, project_root: Path, path: str) -> list[dict[str, str | int | bool]]:
        self.store.init_db(project_root)
        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE path = ?
                ORDER BY line_number ASC, symbol ASC
                """,
                (path,),
            ).fetchall()
        return [
            {
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
            }
            for row in rows
        ]

    def find_partial_group(
        self,
        project_root: Path,
        symbol: str,
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        self.store.init_db(project_root)
        if symbol.strip():
            self.store._ensure_parsed_candidates(project_root, symbol, limit=limit * 4)
        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE symbol = ? AND is_partial = 1
                ORDER BY path ASC, line_number ASC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
            }
            for row in rows
        ]

    def find_data_structures(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        self.store.init_db(project_root)
        if query and query.strip():
            self.store._ensure_parsed_candidates(project_root, query, limit=limit * 4)
        params: list[object] = []
        sql = """
            SELECT path, symbol, kind, line_number, container, is_partial
            FROM code_outlines
            WHERE kind IN ('class', 'record', 'struct', 'interface', 'type_alias', 'enum', 'property', 'field', 'enum_member')
        """
        if query and query.strip():
            needle = f"%{query.strip()}%"
            sql += " AND (symbol LIKE ? OR COALESCE(container, '') LIKE ? OR path LIKE ?)"
            params.extend([needle, needle, needle])
        sql += " ORDER BY path ASC, line_number ASC LIMIT ?"
        params.append(limit)
        with self.store.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
            }
            for row in rows
        ]

    def find_frontend_symbols(
        self,
        project_root: Path,
        query: str | None = None,
        kinds: tuple[str, ...] = (
            "component",
            "context_provider",
            "hook",
            "function",
            "initializer",
        ),
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        self.store.init_db(project_root)
        if query and query.strip():
            self.store._ensure_parsed_candidates(project_root, query, limit=limit * 4)
        placeholders = ", ".join("?" for _ in kinds)
        sql = f"""
            SELECT path, symbol, kind, line_number, container, is_partial
            FROM code_outlines
            WHERE kind IN ({placeholders})
        """
        params: list[object] = list(kinds)
        if query and query.strip():
            needle = f"%{query.strip()}%"
            sql += " AND (symbol LIKE ? OR COALESCE(container, '') LIKE ? OR path LIKE ?)"
            params.extend([needle, needle, needle])
        sql += " LIMIT 500"
        with self.store.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        ranked = []
        needle_text = (query or "").strip()
        kind_weight = {
            "component": 30,
            "context_provider": 28,
            "hook": 24,
            "initializer": 18,
            "function": 12,
        }
        for row in rows:
            score = kind_weight.get(row["kind"], 0)
            reasons: list[str] = []
            if kind_weight.get(row["kind"], 0):
                reasons.append(f"kind_weight:{kind_weight.get(row['kind'], 0)}")
            if needle_text:
                score += self.store._score_text_match(
                    needle_text,
                    row["symbol"],
                    exact=140,
                    prefix=100,
                    contains=70,
                    reasons=reasons,
                    label="symbol",
                )
                score += self.store._score_text_match(
                    needle_text,
                    row["container"] or "",
                    exact=35,
                    prefix=20,
                    contains=10,
                    reasons=reasons,
                    label="container",
                )
            path_weight = self.store._path_weight(project_root, str(row["path"]))
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")
            score -= str(row["path"]).count("/")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"], int(item[1]["line_number"])))
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]
