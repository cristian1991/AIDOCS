from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .symbol_ranking import (
    DIRECT_TIERS,
    TIER_FUZZY,
    TIER_RELATED,
    fuzz_score,
    kind_rank,
    locality_rank,
    majority_top_package,
    score_symbol_row,
)

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


# Leading declaration keywords stripped from a symbol query so the SYMBOL NAME hits the
# exact/prefix path. "async def" is listed before "def" so the longer form wins (#78).
_DECL_KEYWORDS = (
    "async def", "def", "class", "struct", "record", "interface",
    "enum", "function", "func", "fn", "type", "trait", "impl",
)


def _strip_decl_keyword(query: str) -> str:
    """Strip a leading declaration keyword: 'def reconcile' -> 'reconcile', 'class Foo'
    -> 'Foo', 'async def run' -> 'run'. Without this, a natural query keeps the keyword,
    so the exact/prefix phase matches 'def reconcile%' (nothing) and falls through to
    fuzzy concept ranking — which returns unrelated noise (#78). Only strips when a SPACE
    plus a following name exists, so real identifiers ('default_config', 'classifier',
    'functional', or a bare 'def') are left untouched.
    """
    q = (query or "").strip()
    low = q.lower()
    for kw in _DECL_KEYWORDS:
        if low.startswith(kw + " "):
            rest = q[len(kw):].strip()
            if rest:
                return rest
    return q


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
            end = max(line_counts.get(str(h["path"]), start), start)
            for ln, cont in outlines.get(str(h["path"]), []):
                if ln <= start:
                    continue
                # #478 (War S specimen): skip outlines still INSIDE this
                # symbol's scope — for ANY kind, not just container kinds.
                # Nested closures are indexed as first-class rows whose
                # container chain names the enclosing function (`resolve` /
                # `resolve.out`), and pre-fix they truncated the enclosing
                # function's span at the first nested def (reported 1364
                # while the function ended at 1636).
                if cont and sym in cont.split("."):
                    continue  # next outline is still inside this scope
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
        strict: bool = False,
        fuzz: bool = False,
    ) -> list[dict[str, str | int | bool | None]]:
        """Ranked symbol search. Every row carries {score, tier}.

        Candidate collection keeps the historical phase SQL (exact/prefix,
        broad variants, gated fuzz) — ranking is pure-python over the SAME
        candidate union via symbol_ranking.score_symbol_row (War AW: phase
        membership is no longer the rank).

        strict=True  → exact+strong tiers only.
        fuzz=True    → force the fuzz phase regardless of direct-hit count
                       (the old permissive flood, for whoever wants it).
        """
        self.store.init_db(project_root)
        needle = _strip_decl_keyword(query)  # #78: 'def reconcile' -> 'reconcile'

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
                # (path, symbol, line) → fuzz score for rows admitted by
                # the fuzz phase; scored via fuzz_score (capped at 84).
                fuzz_keys: dict[tuple[str, str, int], int] = {}

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
                # No-padding law (War AW): fuzz never fires when phases
                # 1+2 already produced candidates, unless fuzz=True
                # forces the old permissive behavior. strict=True skips
                # fuzz entirely.
                if not strict and (fuzz or len(rows) < 3):
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
                            wratio = max(
                                _rf_fuzz.WRatio(q_raw, s_raw),
                                _rf_fuzz.WRatio(q_sorted, s_sorted),
                            )
                            hit = wratio >= 85
                        else:
                            hit = self.store._inference._edit_distance_le_1(
                                needle.lower(),
                                sym.lower(),
                            )
                            wratio = 100 if hit else 0
                        if hit:
                            seen_keys.add(key)
                            rows.append(r)
                            fuzz_keys[key] = fuzz_score(wratio)
            else:
                fuzz_keys = {}
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

        # ── Unified scorer (War AW): one module-level score function over
        # the SAME candidate union the phase SQL produced. Phase
        # membership is no longer the rank.
        if needle:
            needle_words = needle.split()
            scoring_variants: list[str] = []
            if len(needle_words) > 1:
                scoring_variants.append("".join(w.capitalize() for w in needle_words))
                scoring_variants.append(
                    needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]),
                )
                scoring_variants.append("_".join(w.lower() for w in needle_words))
            elif any(c.isupper() for c in needle[1:]):
                camel_split = re.sub(r"([a-z])([A-Z])", r"\1 \2", needle)
                scoring_variants.append(camel_split.lower())

            scored: list[tuple[int, str, sqlite3.Row]] = []
            for row in rows:
                key = (row["path"], row["symbol"], row["line_number"])
                if key in fuzz_keys:
                    scored.append((fuzz_keys[key], TIER_FUZZY, row))
                    continue
                score, tier = score_symbol_row(
                    needle,
                    row["symbol"] or "",
                    container=row["container"] or "",
                    variants=scoring_variants,
                )
                scored.append((score, tier, row))

            if strict:
                scored = [item for item in scored if item[1] in DIRECT_TIERS]

            # Tie-breaks: kind preference (container/callable over
            # variable), then path locality — same top-level package as
            # the direct-hit majority sorts first.
            majority = majority_top_package(
                [str(row["path"]) for _, tier, row in scored if tier in DIRECT_TIERS],
            )
            scored.sort(
                key=lambda item: (
                    -item[0],
                    kind_rank(item[2]["kind"]),
                    locality_rank(str(item[2]["path"]), majority),
                    item[2]["path"],
                    int(item[2]["line_number"]),
                ),
            )
            ranked = scored
        else:
            # Kind-only listing: no needle to score against — keep the
            # role-relevance ordering, stamp tier=related.
            listed: list[tuple[int, str, sqlite3.Row]] = []
            for row in rows:
                score = self.store._role_relevance_boost(project_root, str(row["path"]))
                score += self.store._path_weight(project_root, str(row["path"]))
                score -= row["path"].count("/")
                listed.append((score, TIER_RELATED, row))
            listed.sort(
                key=lambda item: (-item[0], item[2]["path"], int(item[2]["line_number"])),
            )
            ranked = listed

        # Ranker trace stays opt-in via AIDOCS_SHOW_RANKER_TRACE=1 (DX
        # doctrine — debug noise must not inflate routine results).
        import os as _os_rt

        _show_trace = _os_rt.environ.get("AIDOCS_SHOW_RANKER_TRACE", "").strip() == "1"
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "score": score,
                "tier": tier,
                **(
                    {"why": [f"score:{score}", f"tier:{tier}", f"kind_rank:{kind_rank(row['kind'])}"]}
                    if _show_trace
                    else {}
                ),
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
            }
            for score, tier, row in ranked[:limit]
        ]

    # Minimum direct (exact+strong) hits before the no-padding law
    # collapses the related tier to a summary and vetoes fuzz.
    _NO_PADDING_DIRECT_MIN = 3
    _RELATED_SUMMARY_TOP = 5

    def search_symbols_tiered(
        self,
        project_root: Path,
        query: str,
        kind: str | None = None,
        role: str | None = None,
        limit: int = 25,
        modified_since_ns: int | None = None,
        strict: bool = False,
        fuzz: bool = False,
    ) -> dict[str, object]:
        """Agent-facing tiered search — the NO-PADDING LAW (War AW).

        Default: exact+strong rows in full; when there are >= 3 of them
        the related tier collapses to {count, top} under
        ``related_summary`` and fuzz never fires. When < 3, related rows
        fill up to limit and fuzz may fire (as before).

        strict=True → exact+strong only. fuzz=True → the old permissive
        flood (no collapse). Returns {"results": rows[, "related_summary"]}.
        """
        rows = self.search_symbols(
            project_root,
            query,
            kind=kind,
            role=role,
            # Over-fetch so the direct/related split sees the full
            # candidate field even when direct hits alone fill the limit.
            limit=max(limit * 4, 100) if not strict else limit,
            modified_since_ns=modified_since_ns,
            strict=strict,
            fuzz=fuzz,
        )
        if strict or fuzz:
            return {"results": rows[:limit]}
        direct = [r for r in rows if r.get("tier") in DIRECT_TIERS]
        rest = [r for r in rows if r.get("tier") not in DIRECT_TIERS]
        if len(direct) >= self._NO_PADDING_DIRECT_MIN:
            out: dict[str, object] = {"results": direct[:limit]}
            if rest:
                out["related_summary"] = {
                    "count": len(rest),
                    "top": [
                        {
                            "symbol": r["symbol"],
                            "kind": r["kind"],
                            "path": r["path"],
                            "score": r["score"],
                        }
                        for r in rest[: self._RELATED_SUMMARY_TOP]
                    ],
                    "hint": "related-tier hits collapsed (no-padding law); "
                    "pass fuzz=true for the full permissive list",
                }
            return out
        return {"results": rows[:limit]}

    def resolve_symbol(
        self,
        project_root: Path,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
    ) -> dict[str, str | int | bool | None] | None:
        """ONE-RESOLVER seam (War AW): resolve a symbol query to a single
        indexed outline row using the SAME scorer as ai_find.

        Used by get_symbol_snippet when the exact triple lookup misses —
        a symbol findable by ai_find is snippet-resolvable with the same
        query string. Only direct (exact/strong) hits resolve; related
        and fuzzy hits never silently substitute a different symbol.
        """
        hits = self.search_symbols(project_root, symbol, kind=kind, limit=10)
        if path:
            hits = [h for h in hits if str(h.get("path")) == path]
        for h in hits:
            if h.get("tier") in DIRECT_TIERS:
                return h
        return None

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
        budget_seconds: float | None = None,
    ) -> dict[str, object]:
        """Sweep indexed files for usage lines of ``symbol``.

        #482 truthfulness contract — an empty result is NEVER ambiguous:
          * empty_reason="symbol_not_indexed" — no code_outlines row for
            the symbol (spelling / stale index);
          * empty_reason="no_references"      — the symbol IS indexed but
            zero usage lines exist;
          * empty_reason="timed_out"          — the budget expired before
            any match. When the budget expires AFTER matches were found,
            the partial matches are returned with timed_out+partial flags
            instead (never a bare empty).
        ``budget_seconds`` bounds the file sweep (None = unbounded); the
        ai_find tool passes ~90% of its outer timeout so partials always
        beat the hard tool kill.
        """
        import time

        self.store.init_db(project_root)
        needle = symbol.strip()
        if not needle:
            return {
                "matches": [],
                "empty_reason": "no_match",
                "next_action": "Pass a symbol name to search references for.",
            }

        # perf_counter, not monotonic: Windows' monotonic ticks at ~15.6ms,
        # which silently swallows sub-tick budgets (the zero-budget honesty
        # pin flaked on warm runs — release-red 2026-07-19). perf_counter is
        # equally monotonic with sub-microsecond resolution, so a tiny
        # budget expires deterministically at the first per-file check.
        deadline = (
            time.perf_counter() + budget_seconds
            if budget_seconds is not None and budget_seconds > 0
            else None
        )
        pattern = re.compile(rf"\b{re.escape(needle)}\b")
        matches: list[dict[str, object]] = []

        with self.store.connect(project_root) as conn:
            rows = conn.execute("SELECT path, language FROM code_files ORDER BY path").fetchall()

        timed_out = False
        files_scanned = 0
        for row in rows:
            if deadline is not None and time.perf_counter() >= deadline:
                timed_out = True
                break
            files_scanned += 1
            path = str(row["path"])
            abs_path = project_root / path
            try:
                text = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                # Indexed row for a file that vanished on disk — skip;
                # staleness is the sitter's problem, not a crash here.
                continue
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
        out: dict[str, object] = {
            "symbol": symbol,
            "source": "file_content",
            "matches": [item for _, item in ranked[:limit]],
        }
        if timed_out:
            out["timed_out"] = True
            out["files_scanned"] = files_scanned
            out["files_total"] = len(rows)
            if matches:
                out["partial"] = True
                out["next_action"] = (
                    f"Partial results: reference sweep hit its time budget after "
                    f"{files_scanned}/{len(rows)} files — retry with a larger "
                    "timeout= or narrow the symbol."
                )
            else:
                out["empty_reason"] = "timed_out"
                out["next_action"] = (
                    f"Reference sweep timed out after {files_scanned}/{len(rows)} "
                    "files with no match yet — retry with timeout=60 (or higher) "
                    "or narrow the symbol."
                )
        elif not matches:
            with self.store.connect(project_root) as conn:
                defined = conn.execute(
                    "SELECT path, line_number FROM code_outlines "
                    "WHERE symbol = ? ORDER BY path LIMIT 1",
                    (needle,),
                ).fetchone()
            if defined is not None:
                out["empty_reason"] = "no_references"
                out["defined_at"] = f"{defined['path']}:{defined['line_number']}"
                out["next_action"] = (
                    "Symbol IS indexed (definition at "
                    f"{defined['path']}:{defined['line_number']}) but zero usage "
                    "lines were found — it genuinely has no references."
                )
            else:
                out["empty_reason"] = "symbol_not_indexed"
                out["next_action"] = (
                    "No code_outlines row for this symbol — check the spelling "
                    "with ai_find mode=symbols, or refresh the index with "
                    "ai_index_sync."
                )
        return out
