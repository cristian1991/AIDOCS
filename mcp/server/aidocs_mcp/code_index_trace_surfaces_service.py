from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class CodeIndexTraceSurfacesService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def find_mutation_points(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}

        self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        symbol_matches = self.store.search_symbols(project_root, query=needle, limit=limit)
        references = self.store.find_references(project_root, symbol=needle, limit=limit)["matches"]
        code_matches = self.store.search_code(project_root, query=needle, limit=limit)

        mutation_tokens = ("set", "update", "save", "create", "delete", "remove", "toggle", "apply", "sync", "write", "assign", "change", "complete")

        # Also search for methods INSIDE the queried container (e.g., query="CashFlowService" finds CreateAccountAsync inside it)
        with self.store.connect(project_root) as conn:
            mutation_like_clauses = " OR ".join(["LOWER(co.symbol) LIKE ?" for _ in mutation_tokens])
            mutation_like_params = [f"%{t}%" for t in mutation_tokens]
            container_methods = conn.execute(
                f"""
                SELECT co.path, co.symbol, co.kind, co.line_number, co.container
                FROM code_outlines co
                WHERE co.container = ? AND co.kind = 'method'
                  AND ({mutation_like_clauses})
                LIMIT ?
                """,
                [needle] + mutation_like_params + [limit * 2],
            ).fetchall()
            for row in container_methods:
                # Add as symbol match so the main loop processes it
                symbol_matches.append({
                    "path": row["path"], "symbol": row["symbol"],
                    "kind": row["kind"], "line_number": row["line_number"],
                    "container": row["container"],
                })
        factory_tokens = ("factory", "fixture", "testbase")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        lower_needle = needle.lower()
        # Extract concept tokens for container matching (e.g., "CashFlowService" -> "cashflowservice")
        needle_tokens = [t.lower() for t in re.split(r'[.\s]+', needle) if t]

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            container = str(item.get("container") or "")
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            lower_container = container.lower()
            score = self.store._score_text_match(needle, symbol, exact=90, prefix=60, contains=35)

            # Also check if the query matches the container (e.g., query="CashFlowService", container="CashFlowService")
            container_match = self.store._score_text_match(needle, container, exact=50, prefix=35, contains=20) if container else 0
            # Or if any needle token appears in the container/path
            context_match = any(t in lower_container or t in lower_path for t in needle_tokens)

            token_bonus = 0
            for token in mutation_tokens:
                if token in lower_symbol:
                    token_bonus += 25
            # Skip only if: no mutation token AND no concept match (symbol, container, or path)
            if token_bonus == 0 and score == 0 and container_match == 0 and not context_match:
                continue
            # If mutation token present but no direct symbol match, use container match as base
            if score == 0 and container_match > 0:
                score = container_match
            score += token_bonus
            layer = self.store._infer_layer_from_path(path)
            score += self.store._path_weight(project_root, path)
            if layer in {"logic", "api", "ui"}:
                score += 10
            if any(token in lower_path for token in factory_tokens) or any(token in lower_symbol for token in factory_tokens):
                score -= 25
            if "/test" in lower_path or "tests/" in lower_path:
                score -= 20
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            # Only fetch snippets for actual mutation methods (have token_bonus),
            # skip snippets for class definitions and context-only matches to reduce output size
            snippet = None
            kind_str = str(item["kind"])
            if token_bonus > 0 and kind_str in ("method", "function"):
                try:
                    snippet = self.store.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=kind_str, line_number=int(item["line_number"]))
                except FileNotFoundError:
                    snippet = None
            merged.append(
                {
                    "score": score,
                    "source": "symbol",
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        line_pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
        for item in references:
            path = str(item["path"])
            line = str(item["line"])
            lower_line = line.lower()
            lower_path = path.lower()
            token_bonus = 0
            for token in mutation_tokens:
                if token in lower_line:
                    token_bonus += 18
            if token_bonus == 0 or not line_pattern.search(line):
                continue
            score = 70 + token_bonus + self.store._path_weight(project_root, path)
            if any(token in lower_path for token in factory_tokens):
                score -= 20
            if "/test" in lower_path or "tests/" in lower_path:
                score -= 15
            key = (path, None, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "source": "reference",
                    "path": path,
                    "layer": item["layer"],
                    "symbol": None,
                    "kind": "reference",
                    "line_number": item["line_number"],
                    "container": None,
                    "snippet": line,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            lower_summary = str(item["summary"] or "").lower()
            token_hits = sum(1 for token in mutation_tokens if token in lower_summary)
            if token_hits == 0 and not any(token in lower_path for token in mutation_tokens):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            score = 15 + token_hits * 18 + self.store._path_weight(project_root, path)
            if any(token in lower_path for token in factory_tokens) or any(token in lower_summary for token in factory_tokens):
                score -= 25
            if "/test" in lower_path or "tests/" in lower_path:
                score -= 20
            merged.append(
                {
                    "score": score,
                    "source": "file_match",
                    "path": path,
                    "layer": self.store._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self.store._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        limited = merged[:limit]
        return {"concept": concept, "matches": limited, "confidence": self.store._trace_confidence(limited), "why": self.store._trace_summary(limited)}

    def find_validation_surfaces(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}

        self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        symbol_matches = self.store.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.store.search_code(project_root, query=needle, limit=limit)

        validation_tokens = ("validate", "validator", "validation", "required", "rule", "rules", "invalid", "error")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            score = self.store._score_text_match(needle, symbol, exact=100, prefix=70, contains=40)
            token_bonus = 0
            for token in validation_tokens:
                if token in lower_symbol:
                    token_bonus += 25
                if token in lower_path:
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            score += token_bonus
            layer = self.store._infer_layer_from_path(path)
            if layer in {"logic", "api", "ui", "data"}:
                score += 10
            score += self.store._path_weight(project_root, path)
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.store.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=str(item["kind"]), line_number=int(item["line_number"]))
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            score = self.store._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            token_bonus = 0
            for token in validation_tokens:
                if token in lower_path or token in str(item["summary"]).lower():
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score + token_bonus + self.store._path_weight(project_root, path),
                    "path": path,
                    "layer": self.store._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self.store._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        limited = merged[:limit]
        return {"concept": concept, "matches": limited, "confidence": self.store._trace_confidence(limited), "why": self.store._trace_summary(limited)}

    def find_async_boundaries(self, project_root: Path, concept: str | None = None, limit: int = 50) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = (concept or "").strip()
        if needle:
            self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        symbol_matches = self.store.search_symbols(project_root, needle or "async", limit=max(limit * 2, 100)) if needle else self.store.search_symbols(project_root, "async", limit=max(limit * 2, 100))
        code_matches = self.store.search_code(project_root, needle or "task", limit=max(limit * 2, 100)) if needle else self.store.search_code(project_root, "task", limit=max(limit * 2, 100))

        async_tokens = ("async", "await", "task", "promise", "deferred", "background", "queue", "schedule", "settimeout", "setinterval")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            score = 0
            if needle:
                score += self.store._score_text_match(needle, symbol, exact=90, prefix=60, contains=35)
                score += self.store._score_text_match(needle, path, exact=40, prefix=25, contains=15)
            token_bonus = 0
            for token in async_tokens:
                if token in lower_symbol:
                    token_bonus += 25
                if token in lower_path:
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            score += token_bonus
            score += self.store._path_weight(project_root, path)
            layer = self.store._infer_layer_from_path(path)
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.store.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=str(item["kind"]), line_number=int(item["line_number"]))
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            score = 0
            if needle:
                score += self.store._score_text_match(needle, path, exact=40, prefix=25, contains=15)
            token_bonus = 0
            for token in async_tokens:
                if token in lower_path or token in str(item["summary"]).lower():
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score + token_bonus + self.store._path_weight(project_root, path),
                    "path": path,
                    "layer": self.store._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self.store._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {"concept": concept, "matches": merged[:limit]}

