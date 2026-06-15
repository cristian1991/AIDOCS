from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

_SPLIT_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT_PUNCT = re.compile(r"[_\-./]+")


def _normalize_for_fuzz(s: str) -> tuple[str, str]:
    """Return (token-joined, token-sorted) normalizations.

    - lowercase
    - split camelCase / snake_case / kebab-case / dotted
    - drop empty tokens
    The caller compares both forms against the query's pair and takes
    the max rapidfuzz score. Catches wrong-case, wrong-order, and
    punctuation-variant matches without letting fuzz outrank exact.
    """
    if not s:
        return "", ""
    stripped = _SPLIT_CAMEL.sub(r"\1 \2", s)
    stripped = _SPLIT_PUNCT.sub(" ", stripped)
    tokens = [t.lower() for t in stripped.split() if t]
    return " ".join(tokens), " ".join(sorted(tokens))

# Kinds that define a scope members live inside vs. kinds that are members.
# Used by decorate_hits to fold member rows under their container hit.
_CONTAINER_KINDS = {"class", "struct", "record", "interface", "enum", "component"}
_MEMBER_KINDS = {"method", "property", "field", "enum_member", "constant", "hook"}


class CodeIndexSymbolSearchService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def decorate_hits(
        self,
        project_root: Path,
        hits: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Agent-facing compaction of search_symbols output.

        1. ``line_end`` span per hit: for leaf symbols, the line before the
           next outline in the file; for containers, the line before the next
           outline that is NOT inside the container (else file line_count).
        2. Member folding: a property/field/method row whose container is
           itself a hit in this result set collapses into the parent's
           ``members`` list — one class with 30 properties costs one entry.
        3. ``container`` is dropped when identical to ``namespace`` (C#
           top-level types repeat the namespace in both fields).

        Internal consumers call search_symbols directly and never see this;
        only the ai_find tool layer applies it.
        """
        if not hits:
            return hits
        paths = sorted({str(h["path"]) for h in hits})
        qmarks = ", ".join("?" for _ in paths)
        outlines: dict[str, list[tuple[int, str]]] = {}
        line_counts: dict[str, int] = {}
        with self.store.connect(project_root) as conn:
            for r in conn.execute(
                f"SELECT path, line_number, COALESCE(container, '') "
                f"FROM code_outlines WHERE path IN ({qmarks}) "
                f"ORDER BY path, line_number",
                paths,
            ):
                outlines.setdefault(r[0], []).append((int(r[1]), r[2]))
            for r in conn.execute(
                f"SELECT path, COALESCE(line_count, 0) FROM code_files WHERE path IN ({qmarks})",
                paths,
            ):
                line_counts[r[0]] = int(r[1])

        for h in hits:
            start = int(h["line_number"])
            sym = str(h["symbol"])
            is_container = h["kind"] in _CONTAINER_KINDS
            end = max(line_counts.get(str(h["path"]), start), start)
            for ln, cont in outlines.get(str(h["path"]), []):
                if ln <= start:
                    continue
                if is_container and (
                    cont == sym or cont.endswith(f".{sym}") or cont.startswith(f"{sym}.")
                ):
                    continue  # next outline is still inside this container
                end = max(start, ln - 1)
                break
            h["line_end"] = end

        containers = {
            (str(h["path"]), str(h["symbol"])): h
            for h in hits
            if h["kind"] in _CONTAINER_KINDS
        }
        folded: list[dict[str, Any]] = []
        for h in hits:
            cont = str(h.get("container") or "")
            parent = containers.get((str(h["path"]), cont))
            if parent is None and "." in cont:
                parent = containers.get((str(h["path"]), cont.rsplit(".", 1)[-1]))
            if parent is not None and parent is not h and h["kind"] in _MEMBER_KINDS:
                parent.setdefault("members", []).append(
                    {
                        "symbol": h["symbol"],
                        "kind": h["kind"],
                        "line": h["line_number"],
                    },
                )
                continue
            folded.append(h)
        for h in folded:
            if h.get("container") and h.get("namespace") == h.get("container"):
                del h["container"]
        return folded

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
                    priority_needles.append(
                        needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]),
                    )
                priority_needles = list(dict.fromkeys(priority_needles))  # dedupe preserving order

                join_clause = "JOIN code_files cf ON cf.path = co.path" if needs_cf_join else ""
                # Kind expansion: agents searching `kind="function"`
                # almost always mean "any callable" (function OR
                # method). Indexer stores methods inside classes as
                # kind="method", so a strict equality filter would
                # silently drop all instance methods. Same for
                # `class` → any container kind. Keep strict matching
                # for everything else.
                _KIND_EXPAND = {
                    "function": ("function", "method"),
                    "method": ("function", "method"),
                    "callable": ("function", "method"),
                    "class": ("class", "struct", "record", "interface"),
                    "type": ("class", "struct", "record", "interface", "enum"),
                }
                kind_filter = ""
                kind_params: list[object] = []
                if kind:
                    expanded = _KIND_EXPAND.get(kind.strip().lower(), (kind,))
                    placeholders = ", ".join("?" for _ in expanded)
                    kind_filter = f" AND co.kind IN ({placeholders})"
                    kind_params = list(expanded)
                role_filter = " AND cf.role = ?" if role else ""
                extra_params: list[object] = []
                extra_params.extend(kind_params)
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
                broad_clauses = " OR ".join(
                    ["co.symbol LIKE ? OR COALESCE(co.container, '') LIKE ?" for _ in variants],
                )
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

                # Phase 3: edit-distance-1 fuzz. Fires only when
                # phases 1+2 returned too few hits to be useful. Covers
                # typos ("tooldecison" → "ToolDecision"), casing misses
                # that slipped the variant generator, and last-char
                # pluralization quirks the suffix handler missed.
                # Scoped to the top 2000 outlines matching any variant
                # LIKE-prefix so the pass stays sub-millisecond.
                if len(rows) < 3:
                    fuzz_params: list[object] = []
                    fuzz_clauses = []
                    for variant in variants[:40]:
                        fuzz_clauses.append("co.symbol LIKE ?")
                        fuzz_params.append(f"{variant[:3]}%")
                    fuzz_where = " OR ".join(fuzz_clauses) if fuzz_clauses else "1=1"
                    fuzz_params.extend(extra_params)
                    fuzz_rows = conn.execute(
                        f"""
                        SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                        FROM code_outlines co
                        {join_clause}
                        WHERE ({fuzz_where}){kind_filter}{role_filter}{mtime_filter}
                        LIMIT 2000
                        """,
                        fuzz_params,
                    ).fetchall()
                    # rapidfuzz on normalized tokens (2026-04-23 —
                    # replaced hand-rolled Damerau-Levenshtein). Compare
                    # both token-joined and token-sorted forms so
                    # wrong-case, wrong-order, and punctuation variants
                    # all match (TestNlpInstallTool ≈ "install tool
                    # nlp test"). Threshold 85 — below that the false-
                    # positive rate climbs sharply.
                    try:
                        from rapidfuzz import fuzz as _rf_fuzz
                    except ImportError:
                        _rf_fuzz = None
                    q_raw, q_sorted = _normalize_for_fuzz(needle)
                    for r in fuzz_rows:
                        key = (r["path"], r["symbol"], r["line_number"])
                        if key in seen_keys:
                            continue
                        sym = r["symbol"] or ""
                        s_raw, s_sorted = _normalize_for_fuzz(sym)
                        if not s_raw:
                            continue
                        if _rf_fuzz is not None:
                            score = max(
                                _rf_fuzz.WRatio(q_raw, s_raw),
                                _rf_fuzz.WRatio(q_sorted, s_sorted),
                            )
                            hit = score >= 85
                        else:
                            hit = self.store._inference._edit_distance_le_1(
                                needle.lower(),
                                sym.lower(),
                            )
                        if hit:
                            seen_keys.add(key)
                            rows.append(r)
            else:
                join_clause = "JOIN code_files cf ON cf.path = co.path" if needs_cf_join else ""
                where = "1=1"
                params: list[object] = []
                if kind:
                    _KIND_EXPAND_NN = {
                        "function": ("function", "method"),
                        "method": ("function", "method"),
                        "callable": ("function", "method"),
                        "class": ("class", "struct", "record", "interface"),
                        "type": ("class", "struct", "record", "interface", "enum"),
                    }
                    expanded = _KIND_EXPAND_NN.get(kind.strip().lower(), (kind,))
                    placeholders = ", ".join("?" for _ in expanded)
                    where += f" AND co.kind IN ({placeholders})"
                    params.extend(expanded)
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
            needle_variants_for_scoring.append(
                needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]),
            )
            needle_variants_for_scoring.append("_".join(w.lower() for w in needle_words))
        elif any(c.isupper() for c in needle[1:]):
            # CamelCase input — also score against space-separated words
            camel_split = re.sub(r"([a-z])([A-Z])", r"\1 \2", needle)
            needle_variants_for_scoring.append(camel_split.lower())

        for row in rows:
            score = 0
            reasons: list[str] = []
            # Score against all needle variants, take the best
            best_symbol_score = 0
            for nv in needle_variants_for_scoring:
                nv_reasons: list[str] = []
                s = self.store._score_text_match(
                    nv,
                    row["symbol"],
                    exact=140,
                    prefix=100,
                    contains=70,
                    reasons=nv_reasons,
                    label="symbol",
                )
                if s > best_symbol_score:
                    best_symbol_score = s
                    reasons = nv_reasons
            score += best_symbol_score
            score += self.store._score_text_match(
                needle,
                row["container"] or "",
                exact=35,
                prefix=20,
                contains=10,
                reasons=reasons,
                label="container",
            )
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
        # Strip internal ranker-trace fields from agent-facing output.
        # `why=[kind_weight:20, symbol:exact, path_weight:20]` is debug
        # noise that inflates every symbol search result. Opt in via
        # AIDOCS_SHOW_RANKER_TRACE=1 env for ops work (see backlog #13
        # item 7 — filed as agent DX complaint 2026-04-24).
        import os as _os_rt

        _show_trace = _os_rt.environ.get("AIDOCS_SHOW_RANKER_TRACE", "").strip() == "1"
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **(
                    {"namespace": namespace}
                    if (
                        namespace := self.store._namespace_for_path(
                            project_root,
                            str(row["path"]),
                            namespace_cache,
                        )
                    )
                    else {}
                ),
                **({"is_partial": True} if row["is_partial"] else {}),
                **({"why": reasons} if _show_trace else {}),
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
        # Callers say "signature" / "method" but the indexer tags
        # top-level Python functions as kind="function" and class
        # members as kind="method". Agents hitting this tool for
        # `ai_text_search` (a top-level function) got empty matches
        # because the filter was kind=method only. Accept both.
        scan_limit = max(limit * 3, 20)
        matches = list(
            self.search_symbols(project_root, query=method_name, kind="method", limit=scan_limit),
        )
        matches.extend(
            self.search_symbols(project_root, query=method_name, kind="function", limit=scan_limit),
        )
        # Dedup by (path, line_number) to avoid double-hits if a
        # symbol is indexed under both kinds somehow.
        seen: set[tuple[str, int]] = set()
        deduped = []
        for m in matches:
            key = (str(m.get("path") or ""), int(m.get("line_number") or 0))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)
        filtered = [
            m for m in deduped if not container or str(m.get("container") or "") == container
        ][:limit]
        signatures = []
        for item in filtered:
            signature = self.store._extract_method_signature(
                project_root,
                str(item["path"]),
                int(item["line_number"]),
            )
            signatures.append(
                {
                    **item,
                    **signature,
                },
            )
        return {"matches": signatures}

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
                preferred = [
                    item for item in matches if str(item.get("container") or "") == container
                ]
                others = [item for item in matches if str(item.get("container") or "") != container]
                matches = preferred + others
            payload["container"] = container
            payload["matches"] = matches[:limit_per_method]
            results.append(payload)
        return {"methods": results}

    def get_enum_values(
        self,
        project_root: Path,
        enum_name: str,
        limit: int = 50,
        include_related: bool = False,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        enums = self.search_symbols(project_root, query=enum_name, kind="enum", limit=limit)
        exact = [item for item in enums if str(item.get("symbol") or "") == enum_name]
        fuzzy = [item for item in enums if str(item.get("symbol") or "") != enum_name]
        enums = exact + fuzzy if include_related else exact or fuzzy[:1]
        matches = []
        for enum_item in enums[:limit]:
            values = self.store._enum_members_for_container(
                project_root,
                str(enum_item["path"]),
                str(enum_item["symbol"]),
            )
            matches.append({**enum_item, "values": values})
        return {"matches": matches}

    def get_constructor_params(
        self,
        project_root: Path,
        type_name: str,
        limit: int = 20,
        include_related: bool = False,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        matches = self.search_symbols(
            project_root,
            query=type_name,
            kind="record",
            limit=max(limit * 2, 20),
        )
        if not matches:
            matches = self.search_symbols(
                project_root,
                query=type_name,
                kind="class",
                limit=max(limit * 2, 20),
            )
        exact = [item for item in matches if str(item.get("symbol") or "") == type_name]
        fuzzy = [item for item in matches if str(item.get("symbol") or "") != type_name]
        matches = exact + fuzzy if include_related else exact or fuzzy[:1]
        results = []
        for item in matches[:limit]:
            constructor = self.store._extract_constructor_params(
                project_root,
                str(item["path"]),
                str(item["symbol"]),
            )
            results.append({**item, **constructor})
        return {"matches": results}

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
                ),
            )
        return {
            "types": results,
            "include_related": include_related,
        }

    def get_service_api(
        self,
        project_root: Path,
        service_name: str,
        limit: int = 100,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        service_matches = self.search_symbols(
            project_root,
            query=service_name,
            kind="class",
            limit=max(limit, 20),
        )
        exact = next(
            (item for item in service_matches if str(item.get("symbol") or "") == service_name),
            None,
        )
        if not exact:
            return {"match": None, "methods": [], "not_found": True}
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
                    if (
                        namespace := self.store._namespace_for_path(
                            project_root,
                            str(row["path"]),
                            namespace_cache,
                        )
                    )
                    else {}
                ),
            }
            methods.append(
                {
                    **base,
                    **self.store._extract_method_signature(
                        project_root,
                        str(row["path"]),
                        int(row["line_number"]),
                    ),
                },
            )
        # Fallback scans project_root.rglob("*") reading every .cs/.ts
        # /.js file to regex-find partial-class method declarations.
        # Only useful for C#/JS partial classes (where the indexer
        # misses declarations spread across files). For other targets
        # it's a guaranteed 10-30s walk that hangs the tool call.
        # Gate: fallback only if we have ZERO methods AND the target
        # language is one the fallback actually parses.
        target_path = str(target.get("path") or "")
        fallback_supported_suffixes = (".cs", ".ts", ".tsx", ".js", ".jsx")
        target_uses_fallback_lang = target_path.lower().endswith(fallback_supported_suffixes)
        if not methods and target_uses_fallback_lang:
            fallback_methods = self.store._extract_service_methods_from_declaring_files(
                project_root,
                service_name,
                limit=limit,
            )
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

        result: dict[str, object] = {"match": target}
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
        except Exception:
            result = {"entity_name": entity_name, "properties": []}

        if result.get("properties"):
            return result

        # Fallback: walk the code index for a class defn with this name
        # (Python @dataclass / NamedTuple / pydantic BaseModel / plain
        # class) and enumerate AnnAssign + Assign fields via AST. Pre-
        # fix we returned empty + a "use ai_get_constructor_params"
        # nudge even when the class genuinely had properties — agent
        # had to double-hop for data we could extract in one call.
        self.store.init_db(project_root)
        class_matches = self.search_symbols(
            project_root,
            query=entity_name,
            kind="class",
            limit=20,
        )
        record_matches = self.search_symbols(
            project_root,
            query=entity_name,
            kind="record",
            limit=20,
        )
        all_matches = [
            m
            for m in (*class_matches, *record_matches)
            if str(m.get("symbol") or "") == entity_name
        ]
        ast_props = self.store._extract_class_properties_ast(
            project_root,
            all_matches,
        )
        if ast_props:
            return {
                "entity_name": entity_name,
                "properties": ast_props["properties"],
                "path": ast_props.get("path"),
            }
        # Note: name found but no properties enumerable (e.g. C# record
        # primary constructor — SchemaIndexStore can't introspect; AST
        # fallback only handles Python). Point operator at
        # ai_get_symbol_info(kind="constructor") which CAN extract the
        # constructor signature for these cases. Pre-2026-04-25 this
        # hint pointed at the legacy ai_get_constructor_params tool;
        # collapsed into ai_get_symbol_info per backlog #30.
        return {
            "entity_name": entity_name,
            "properties": [],
            "note": (
                "No class/dataclass/schema-entity properties enumerable. "
                "If the type is constructor-shaped (e.g. C# record, "
                "Python @dataclass with positional-only params), try "
                'ai_get_symbol_info(symbol=..., kind="constructor") '
                "to extract the constructor signature. Otherwise check "
                "the name or widen with ai_find."
            ),
        }

    def find_references(
        self,
        project_root: Path,
        symbol: str,
        limit: int = 100,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = symbol.strip()
        if not needle:
            return {"matches": []}

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
                    },
                )

        ranked = []
        lower_symbol = needle.lower()
        for item in matches:
            score = 0
            line_lower = str(item["line"]).lower()
            path_lower = str(item["path"]).lower()
            if re.search(rf"\b{re.escape(lower_symbol)}\b", line_lower):
                score += 120
            if (
                path_lower.endswith(f"{lower_symbol.lower()}.cs")
                or path_lower.endswith(f"{lower_symbol.lower()}.ts")
                or path_lower.endswith(f"{lower_symbol.lower()}.tsx")
            ):
                score += 40
            score -= str(item["path"]).count("/")
            ranked.append((score, item))

        ranked.sort(
            key=lambda pair: (
                -pair[0],
                self.store._layer_rank(str(pair[1]["layer"])),
                str(pair[1]["path"]),
                int(pair[1]["line_number"]),
            ),
        )
        return {
            "symbol": symbol,
            "source": "file_content",
            "matches": [item for _, item in ranked[:limit]],
        }
