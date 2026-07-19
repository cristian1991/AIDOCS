from __future__ import annotations

from pathlib import Path
from typing import Any


class CodeIndexRouteQueryService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def find_routes(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = (query or "").strip()
        if needle:
            self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        route_tokens = ("route", "controller", "/api/", "endpoint", "page", "handler")
        symbol_matches = (
            self.store.search_symbols(
                project_root,
                needle or "controller",
                limit=max(limit * 3, 100),
            )
            if needle
            else self.store.search_symbols(project_root, "controller", limit=max(limit * 3, 100))
        )
        code_matches = (
            self.store.search_code(project_root, needle or "route", limit=max(limit * 3, 100))
            if needle
            else self.store.search_code(project_root, "route", limit=max(limit * 3, 100))
        )

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            if (
                needle
                and needle.lower() not in lower_symbol
                and needle.lower() not in lower_path
                and needle.lower() not in str(item.get("container") or "").lower()
            ):
                continue
            if not needle and not (
                any(token in lower_symbol for token in route_tokens)
                or any(token in lower_path for token in route_tokens)
            ):
                continue

            score = 0
            if needle:
                score += self.store._score_text_match(
                    needle,
                    symbol,
                    exact=110,
                    prefix=80,
                    contains=50,
                )
                score += self.store._score_text_match(
                    needle,
                    path,
                    exact=50,
                    prefix=30,
                    contains=20,
                )
            for token in route_tokens:
                if token in lower_symbol:
                    score += 25
                if token in lower_path:
                    score += 20
            score += self.store._path_weight(project_root, path)
            layer = self.store._infer_layer_from_path(path)
            if layer == "api":
                score += 30
            elif layer == "ui":
                score += 15
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "layer": layer,
                },
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if not needle and not any(token in lower_path for token in route_tokens):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            score = self.store._path_weight(project_root, path)
            if needle:
                score += self.store._score_text_match(
                    needle,
                    path,
                    exact=50,
                    prefix=30,
                    contains=20,
                )
            if (
                "/api/" in lower_path
                or lower_path.endswith("route.ts")
                or lower_path.endswith("route.js")
            ):
                score += 40
            if "controller" in lower_path:
                score += 35
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "layer": self.store._infer_layer_from_path(path),
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

    def trace_api_to_ui(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        query = concept.strip()
        service_name = ""
        method_name = ""
        if "." in query:
            left, right = query.split(".", 1)
            service_name = left.strip()
            method_name = right.strip()

        routes = self.store.find_routes(project_root, query=query, limit=limit)["matches"]
        touchpoints = self.store.find_ui_backend_touchpoints(
            project_root,
            concept=query,
            limit=limit,
        )["matches"]
        clusters = self.store.find_domain_clusters(project_root, concept=query, limit=limit)[
            "cluster"
        ]

        if method_name:
            refs = self.store.find_references(
                project_root,
                symbol=method_name,
                limit=limit * 3,
            ).get("matches", [])
            for ref in refs:
                path = str(ref.get("path") or "")
                layer = str(ref.get("layer") or self.store._infer_layer_from_path(path))
                touchpoints.append(
                    {
                        "score": 130 if layer == "api" else 90 if layer == "ui" else 75,
                        "path": path,
                        "layer": layer,
                        "symbol": method_name,
                        "kind": "reference",
                        "line_number": ref.get("line_number"),
                        "container": None,
                        "snippet": ref.get("line"),
                    },
                )
        if service_name:
            service_symbols = self.store.search_symbols(
                project_root,
                query=service_name,
                limit=limit * 3,
            )
            for item in service_symbols:
                path = str(item.get("path") or "")
                symbol = str(item.get("symbol") or "")
                container = str(item.get("container") or "")
                # Only include symbols that actually relate to the service
                text_match = self.store._score_text_match(
                    service_name,
                    symbol,
                    exact=80,
                    prefix=50,
                    contains=30,
                )
                container_match = self.store._score_text_match(
                    service_name,
                    container,
                    exact=60,
                    prefix=40,
                    contains=20,
                )
                if text_match == 0 and container_match == 0:
                    continue
                layer = self.store._infer_layer_from_path(path)
                base_score = max(text_match, container_match)
                layer_bonus = 30 if layer == "api" else 15 if layer == "logic" else 0
                touchpoints.append(
                    {
                        "score": base_score + layer_bonus,
                        "path": path,
                        "layer": layer,
                        "symbol": symbol,
                        "kind": item.get("kind"),
                        "line_number": item.get("line_number"),
                        "container": item.get("container"),
                        "snippet": None,
                    },
                )

        def dedupe(items: list[dict[str, object]]) -> list[dict[str, object]]:
            seen: set[tuple[str, str | None, int | None]] = set()
            ordered: list[dict[str, object]] = []
            for item in sorted(
                items,
                key=lambda x: (
                    -int(x.get("score", 0)),
                    str(x.get("path") or ""),
                    int(x.get("line_number") or 0),
                ),
            ):
                key = (
                    str(item.get("path") or ""),
                    str(item.get("symbol") or ""),
                    int(item.get("line_number") or 0) or None,
                )
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(item)
            return ordered

        api_side = dedupe(
            [item for item in routes + touchpoints + clusters if item.get("layer") == "api"],
        )
        ui_side = dedupe([item for item in touchpoints + clusters if item.get("layer") == "ui"])
        # Filter logic results by minimum score to suppress unrelated interface noise
        min_logic_score = 30
        logic_side = dedupe(
            [
                item
                for item in touchpoints + clusters
                if item.get("layer") == "logic" and int(item.get("score", 0)) >= min_logic_score
            ],
        )

        return {
            "api": api_side[:limit],
            "logic": logic_side[:limit],
            "ui": ui_side[:limit],
        }

    def find_ui_backend_touchpoints(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"matches": []}
        self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        symbol_matches = self.store.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.store.search_code(project_root, query=needle, limit=limit)

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            layer = self.store._infer_layer_from_path(path)
            if layer not in {"data", "logic", "api", "ui"}:
                continue
            kind = str(item["kind"])
            symbol = str(item["symbol"])
            container = str(item.get("container") or "")
            text_score = self.store._score_text_match(
                needle,
                symbol,
                exact=120,
                prefix=90,
                contains=60,
            )
            # Also check container match (e.g., query="CompleteItemAsync" in container "DocumentService")
            container_score = (
                self.store._score_text_match(needle, container, exact=80, prefix=50, contains=30)
                if container
                else 0
            )
            # Skip symbols with no relevance to the query — prevents logic noise
            if text_score == 0 and container_score == 0:
                continue
            score = max(text_score, container_score)
            if layer == "api":
                score += 25
            elif layer == "ui":
                score += 20
            elif layer == "logic":
                score += 18
            elif layer == "data":
                score += 15

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
                    "layer": layer,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                },
            )

        for item in code_matches:
            path = str(item["path"])
            layer = self.store._infer_layer_from_path(path)
            if layer not in {"data", "logic", "api", "ui"}:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            score = self.store._score_text_match(needle, path, exact=60, prefix=35, contains=20)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
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

    def find_policy_surfaces(
        self,
        project_root: Path,
        concept: str,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"matches": []}
        self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        symbol_matches = self.store.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.store.search_code(project_root, query=needle, limit=limit)

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        policy_tokens = (
            "policy",
            "permission",
            "role",
            "claim",
            "guard",
            "authorize",
            "auth",
            "middleware",
            "filter",
            "tenant",
            "scope",
            "isolation",
            "security",
            "require",
            "attribute",
            "handler",
            "interceptor",
            "validator",
        )

        for item in symbol_matches:
            path = str(item["path"])
            layer = self.store._infer_layer_from_path(path)
            symbol = str(item["symbol"])
            kind = str(item["kind"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()

            score = self.store._score_text_match(needle, symbol, exact=120, prefix=90, contains=60)
            if any(token in lower_symbol for token in policy_tokens):
                score += 50
            if any(token in lower_path for token in policy_tokens):
                score += 30
            if layer == "api":
                score += 25
            elif layer == "logic":
                score += 20
            elif layer == "ui":
                score += 12
            else:
                score += 5

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
                    "layer": layer,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                },
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if (
                not any(token in lower_path for token in policy_tokens)
                and needle.lower() not in lower_path
            ):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": 40 if needle.lower() in lower_path else 20,
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

    def find_entrypoints(
        self,
        project_root: Path,
        concept: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        self.store.init_db(project_root)
        needle = (concept or "").strip()
        if needle:
            self.store._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        patterns = (
            "start",
            "bootstrap",
            "init",
            "initialize",
            "setup",
            "register",
            "configure",
            "createapp",
            "main",
            "app",
            "provider",
        )

        symbol_matches = (
            self.store.search_symbols(project_root, needle or "init", limit=max(limit * 2, 50))
            if needle
            else []
        )
        code_matches = (
            self.store.search_code(project_root, needle or "main", limit=max(limit * 2, 50))
            if needle
            else []
        )

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            if needle:
                if (
                    needle.lower() not in lower_symbol
                    and needle.lower() not in lower_path
                    and needle.lower() not in str(item.get("container") or "").lower()
                ):
                    continue
            elif not any(token in lower_symbol for token in patterns):
                continue

            score = 0
            for token in patterns:
                if token in lower_symbol:
                    score += 25
            if str(item["kind"]) in {"initializer", "context_provider", "component", "function"}:
                score += 20
            score += self.store._path_weight(project_root, path)
            layer = self.store._infer_layer_from_path(path)
            if layer in {"api", "logic", "ui"}:
                score += 10
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item.get("container"),
                    "layer": layer,
                },
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if not any(token in lower_path for token in patterns):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": self.store._path_weight(project_root, path) + 20,
                    "path": path,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "layer": self.store._infer_layer_from_path(path),
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
        return {
            "concept": concept,
            "matches": merged[:limit],
        }
