from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class CodeIndexSymbolSearchService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def search_symbols(
        self,
        project_root: Path,
        query: str,
        kind: str | None = None,
        role: str | None = None,
        limit: int = 25,
        modified_since_ns: int | None = None,
    ) -> list[dict[str, str | int | bool | None]]:
        self.store.init_db(project_root)
        needle = query.strip()

        # Allow searching by kind alone (no symbol name needed)
        if not needle and not kind and modified_since_ns is None:
            return []

        # mtime filter requires JOIN to code_files
        needs_cf_join = bool(role) or modified_since_ns is not None
        mtime_filter = ""
        mtime_params: list[object] = []
        if modified_since_ns is not None:
            mtime_filter = " AND cf.mtime_ns >= ?"
            mtime_params = [modified_since_ns]

        with self.store.connect(project_root) as conn:
            if needle:
                self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
                variants = self.store._concept_variants(needle)

                # Build multi-word CamelCase variants for priority matching
                needle_words = needle.split()
                priority_needles: list[str] = [needle]
                if len(needle_words) > 1:
                    priority_needles.append("".join(w.capitalize() for w in needle_words))
                    priority_needles.append(needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]))
                priority_needles = list(dict.fromkeys(priority_needles))  # dedupe preserving order

                join_clause = "JOIN code_files cf ON cf.path = co.path" if needs_cf_join else ""
                kind_filter = " AND co.kind = ?" if kind else ""
                role_filter = " AND cf.role = ?" if role else ""
                extra_params: list[object] = []
                if kind:
                    extra_params.append(kind)
                if role:
                    extra_params.append(role)
                extra_params.extend(mtime_params)

                # Phase 1: exact/prefix matches on original needle and CamelCase joins
                seen_keys: set[tuple[str, str, int]] = set()
                rows: list[sqlite3.Row] = []

                for pn in priority_needles:
                    pn_params: list[object] = [f"{pn}%", f"%{pn}%"]
                    pn_params.extend(extra_params)
                    phase1 = conn.execute(
                        f"""
                        SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                        FROM code_outlines co
                        {join_clause}
                        WHERE (co.symbol LIKE ? OR co.symbol LIKE ?){kind_filter}{role_filter}{mtime_filter}
                        LIMIT 100
                        """,
                        pn_params,
                    ).fetchall()
                    for r in phase1:
                        key = (r["path"], r["symbol"], r["line_number"])
                        if key not in seen_keys:
                            seen_keys.add(key)
                            rows.append(r)

                # Phase 2: broader variant matches to fill remaining slots
                broad_clauses = " OR ".join(["co.symbol LIKE ? OR COALESCE(co.container, '') LIKE ?" for _ in variants])
                broad_params: list[object] = []
                for variant in variants:
                    pattern = f"%{variant}%"
                    broad_params.extend([pattern, pattern])
                broad_params.extend(extra_params)

                phase2 = conn.execute(
                    f"""
                    SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                    FROM code_outlines co
                    {join_clause}
                    WHERE ({broad_clauses}){kind_filter}{role_filter}{mtime_filter}
                    LIMIT 500
                    """,
                    broad_params,
                ).fetchall()
                for r in phase2:
                    key = (r["path"], r["symbol"], r["line_number"])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        rows.append(r)
            else:
                join_clause = "JOIN code_files cf ON cf.path = co.path" if needs_cf_join else ""
                where = "1=1"
                params: list[object] = []
                if kind:
                    where += " AND co.kind = ?"
                    params.append(kind)
                if role:
                    where += " AND cf.role = ?"
                    params.append(role)
                if modified_since_ns is not None:
                    if not needs_cf_join:
                        join_clause = "JOIN code_files cf ON cf.path = co.path"
                    where += " AND cf.mtime_ns >= ?"
                    params.append(modified_since_ns)
                rows = conn.execute(
                    f"""
                    SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                    FROM code_outlines co
                    {join_clause}
                    WHERE {where}
                    ORDER BY co.path, co.line_number
                    LIMIT 500
                    """,
                    params,
                ).fetchall()
        namespace_cache: dict[str, str | None] = {}
        ranked = []
        kind_weight = {
            "class": 30,
            "record": 28,
            "struct": 26,
            "interface": 24,
            "type_alias": 23,
            "enum": 22,
            "function": 20,
            "component": 20,
            "hook": 18,
            "initializer": 18,
            "method": 14,
            "property": 12,
            "field": 10,
            "enum_member": 8,
        }
        # Pre-compute CamelCase variant of needle for multi-word scoring
        needle_words = needle.split()
        needle_variants_for_scoring: list[str] = [needle]
        if len(needle_words) > 1:
            needle_variants_for_scoring.append("".join(w.capitalize() for w in needle_words))
            needle_variants_for_scoring.append(needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]))
            needle_variants_for_scoring.append("_".join(w.lower() for w in needle_words))
        elif any(c.isupper() for c in needle[1:]):
            # CamelCase input — also score against space-separated words
            camel_split = re.sub(r'([a-z])([A-Z])', r'\1 \2', needle)
            needle_variants_for_scoring.append(camel_split.lower())

        for row in rows:
            score = 0
            reasons: list[str] = []
            # Score against all needle variants, take the best
            best_symbol_score = 0
            for nv in needle_variants_for_scoring:
                nv_reasons: list[str] = []
                s = self.store._score_text_match(nv, row["symbol"], exact=140, prefix=100, contains=70, reasons=nv_reasons, label="symbol")
                if s > best_symbol_score:
                    best_symbol_score = s
                    reasons = nv_reasons
            score += best_symbol_score
            score += self.store._score_text_match(needle, row["container"] or "", exact=35, prefix=20, contains=10, reasons=reasons, label="container")
            score += kind_weight.get(row["kind"], 0)
            if kind_weight.get(row["kind"], 0):
                reasons.append(f"kind_weight:{kind_weight.get(row['kind'], 0)}")
            score += 5 if row["is_partial"] else 0
            if row["is_partial"]:
                reasons.append("partial_bonus:5")
            path_weight = self.store._path_weight(project_root, str(row["path"]))
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")
            # For kind-only queries, boost by role relevance (services > utilities > tests)
            if not needle:
                role_boost = self.store._role_relevance_boost(project_root, str(row["path"]))
                score += role_boost
                if role_boost:
                    reasons.append(f"role_boost:{role_boost}")
            score -= row["path"].count("/")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"], int(item[1]["line_number"])))
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **(
                    {"namespace": namespace}
                    if (namespace := self.store._namespace_for_path(project_root, str(row["path"]), namespace_cache))
                    else {}
                ),
                **({"is_partial": True} if row["is_partial"] else {}),
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]

    def get_method_signature(
        self,
        project_root: Path,
        method_name: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        matches = self.search_symbols(project_root, query=method_name, kind="method", limit=max(limit * 3, 20))
        filtered = [m for m in matches if not container or str(m.get("container") or "") == container][:limit]
        signatures = []
        for item in filtered:
            signature = self.store._extract_method_signature(project_root, str(item["path"]), int(item["line_number"]))
            signatures.append(
                {
                    **item,
                    **signature,
                }
            )
        return {
            "method": method_name,
            "container": container,
            "matches": signatures,
        }

    def get_method_signatures(
        self,
        project_root: Path,
        methods: list[str],
        container: str | None = None,
        limit_per_method: int = 20,
    ) -> dict[str, object]:
        results = []
        for method in methods:
            if not method or not method.strip():
                continue
            payload = self.store.get_method_signature(
                project_root,
                method_name=method.strip(),
                container=None,
                limit=max(limit_per_method * 3, 20),
            )
            matches = payload.get("matches", []) if isinstance(payload, dict) else []
            if container:
                preferred = [item for item in matches if str(item.get("container") or "") == container]
                others = [item for item in matches if str(item.get("container") or "") != container]
                matches = preferred + others
            payload["container"] = container
            payload["matches"] = matches[:limit_per_method]
            results.append(payload)
        return {
            "container": container,
            "methods": results,
        }

    def get_enum_values(self, project_root: Path, enum_name: str, limit: int = 50, include_related: bool = False) -> dict[str, object]:
        self.store.init_db(project_root)
        enums = self.search_symbols(project_root, query=enum_name, kind="enum", limit=limit)
        exact = [item for item in enums if str(item.get("symbol") or "") == enum_name]
        fuzzy = [item for item in enums if str(item.get("symbol") or "") != enum_name]
        enums = exact + fuzzy if include_related else exact or fuzzy[:1]
        matches = []
        for enum_item in enums[:limit]:
            values = self.store._enum_members_for_container(project_root, str(enum_item["path"]), str(enum_item["symbol"]))
            matches.append({**enum_item, "values": values})
        return {
            "enum": enum_name,
            "include_related": include_related,
            "matches": matches,
        }

    def get_constructor_params(
        self,
        project_root: Path,
        type_name: str,
        limit: int = 20,
        include_related: bool = False,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        matches = self.search_symbols(project_root, query=type_name, kind="record", limit=max(limit * 2, 20))
        if not matches:
            matches = self.search_symbols(project_root, query=type_name, kind="class", limit=max(limit * 2, 20))
        exact = [item for item in matches if str(item.get("symbol") or "") == type_name]
        fuzzy = [item for item in matches if str(item.get("symbol") or "") != type_name]
        matches = exact + fuzzy if include_related else exact or fuzzy[:1]
        results = []
        for item in matches[:limit]:
            constructor = self.store._extract_constructor_params(project_root, str(item["path"]), str(item["symbol"]))
            results.append({**item, **constructor})
        return {
            "type": type_name,
            "include_related": include_related,
            "matches": results,
        }

    def get_constructor_params_batch(
        self,
        project_root: Path,
        types: list[str],
        include_related: bool = False,
        limit_per_type: int = 20,
    ) -> dict[str, object]:
        results = []
        for type_name in types:
            if not type_name or not type_name.strip():
                continue
            results.append(
                self.store.get_constructor_params(
                    project_root,
                    type_name=type_name.strip(),
                    limit=limit_per_type,
                    include_related=include_related,
                )
            )
        return {
            "types": results,
            "include_related": include_related,
        }

    def get_service_api(self, project_root: Path, service_name: str, limit: int = 100) -> dict[str, object]:
        self.store.init_db(project_root)
        service_matches = self.search_symbols(project_root, query=service_name, kind="class", limit=max(limit, 20))
        exact = next((item for item in service_matches if str(item.get("symbol") or "") == service_name), None)
        if not exact:
            return {"service": service_name, "match": None, "methods": [], "not_found": True}
        target = exact

        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT path, symbol, kind, line_number, container, is_partial FROM code_outlines WHERE kind = 'method' AND container = ? ORDER BY path, line_number LIMIT ?",
                (service_name, limit),
            ).fetchall()
        methods = []
        namespace_cache: dict[str, str | None] = {}
        for row in rows:
            base = {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **(
                    {"namespace": namespace}
                    if (namespace := self.store._namespace_for_path(project_root, str(row["path"]), namespace_cache))
                    else {}
                ),
            }
            methods.append({**base, **self.store._extract_method_signature(project_root, str(row["path"]), int(row["line_number"]))})
        if not methods or len({m["path"] for m in methods}) < 2:
            fallback_methods = self.store._extract_service_methods_from_declaring_files(project_root, service_name, limit=limit)
            seen = {(m.get("path"), m.get("symbol"), m.get("line_number")) for m in methods}
            for item in fallback_methods:
                key = (item.get("path"), item.get("symbol"), item.get("line_number"))
                if key not in seen:
                    seen.add(key)
                    methods.append(item)
        # Deduplicate: hoist common container/namespace to top level
        containers = {m.get("container") for m in methods if m.get("container")}
        namespaces = {m.get("namespace") for m in methods if m.get("namespace")}
        if len(containers) == 1:
            common_container = containers.pop()
            for m in methods:
                m.pop("container", None)
        else:
            common_container = None
        if len(namespaces) == 1:
            common_namespace = namespaces.pop()
            for m in methods:
                m.pop("namespace", None)
        else:
            common_namespace = None

        result: dict[str, object] = {"service": service_name, "match": target}
        if common_container:
            result["container"] = common_container
        if common_namespace:
            result["namespace"] = common_namespace
        result["method_count"] = len(methods)
        result["methods"] = methods
        return result

    def get_entity_properties(self, project_root: Path, entity_name: str) -> dict[str, object]:
        try:
            from .schema_index_store import SchemaIndexStore

            result = SchemaIndexStore().get_entity_properties(project_root, entity_name)
            if not result.get("properties"):
                result["note"] = "No class-style properties found. If this is a record or constructor-heavy type, use code_get_constructor_params."
            return result
        except Exception:
            return {
                "entity_name": entity_name,
                "properties": [],
                "note": "No class-style properties found. If this is a record or constructor-heavy type, use code_get_constructor_params.",
            }

    def find_references(self, project_root: Path, symbol: str, limit: int = 100) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = symbol.strip()
        if not needle:
            return {"symbol": symbol, "matches": []}

        pattern = re.compile(rf"\b{re.escape(needle)}\b")
        matches: list[dict[str, object]] = []

        with self.store.connect(project_root) as conn:
            rows = conn.execute("SELECT path, language FROM code_files ORDER BY path").fetchall()

        for row in rows:
            path = str(row["path"])
            abs_path = project_root / path
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not pattern.search(line):
                    continue
                matches.append(
                    {
                        "path": path,
                        "language": row["language"],
                        "line_number": line_number,
                        "line": line.strip(),
                        "layer": self.store._infer_layer_from_path(path),
                    }
                )

        ranked = []
        lower_symbol = needle.lower()
        for item in matches:
            score = 0
            line_lower = str(item["line"]).lower()
            path_lower = str(item["path"]).lower()
            if re.search(rf"\b{re.escape(lower_symbol)}\b", line_lower):
                score += 120
            if path_lower.endswith(f"{lower_symbol.lower()}.cs") or path_lower.endswith(f"{lower_symbol.lower()}.ts") or path_lower.endswith(f"{lower_symbol.lower()}.tsx"):
                score += 40
            score -= str(item["path"]).count("/")
            ranked.append((score, item))

        ranked.sort(key=lambda pair: (-pair[0], self.store._layer_rank(str(pair[1]["layer"])), str(pair[1]["path"]), int(pair[1]["line_number"])))
        return {
            "symbol": symbol,
            "source": "file_content",
            "matches": [item for _, item in ranked[:limit]],
        }
