from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .language_descriptors import load_index_config


class CodeIndexInferenceService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _score_text_match(
        self,
        needle: str,
        candidate: str,
        exact: int,
        prefix: int,
        contains: int,
        reasons: list[str] | None = None,
        label: str | None = None,
    ) -> int:
        if not candidate:
            return 0
        n = needle.lower()
        c = candidate.lower()
        if c == n:
            if reasons is not None and label is not None:
                reasons.append(f"{label}:exact")
            return exact
        if c.startswith(n):
            if reasons is not None and label is not None:
                reasons.append(f"{label}:prefix")
            return prefix
        if n in c:
            if reasons is not None and label is not None:
                reasons.append(f"{label}:contains")
            return contains
        return 0

    def _trace_confidence(self, matches: list[dict[str, object]]) -> str:
        if len(matches) >= 4:
            return "high"
        if len(matches) >= 2:
            return "medium"
        return "low"

    def _trace_summary(self, matches: list[dict[str, object]]) -> list[str]:
        if not matches:
            return ["matches:0"]
        layers: dict[str, int] = {}
        for item in matches:
            layer = str(item.get("layer") or "unknown")
            layers[layer] = layers.get(layer, 0) + 1
        summary = [f"matches:{len(matches)}"]
        summary.extend(f"layer:{layer}:{count}" for layer, count in sorted(layers.items()))
        return summary

    def _concept_variants(self, concept: str) -> list[str]:
        raw = concept.strip()
        if not raw:
            return []

        variants: set[str] = set()
        suffixes = (
            "Dto",
            "Model",
            "ViewModel",
            "Entity",
            "Service",
            "Controller",
            "Settings",
            "Options",
            "Request",
            "Response",
            "Id",
        )

        # Split multi-word concepts into individual words and generate variants for each
        words = raw.split()
        tokens = [raw] if len(words) <= 1 else words + [raw]

        # For multi-word queries, generate CamelCase/PascalCase/snake_case joins
        if len(words) > 1:
            # PascalCase: "create sql package" -> "CreateSqlPackage"
            pascal = "".join(w.capitalize() for w in words)
            variants.add(pascal)
            # camelCase: "create sql package" -> "createSqlPackage"
            camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
            variants.add(camel)
            # snake_case: "create sql package" -> "create_sql_package"
            snake = "_".join(w.lower() for w in words)
            variants.add(snake)
            # kebab-case: "create sql package" -> "create-sql-package"
            kebab = "-".join(w.lower() for w in words)
            variants.add(kebab)
            # Also add partial CamelCase combos for subsets
            for i in range(len(words)):
                for j in range(i + 2, len(words) + 1):
                    sub = "".join(w.capitalize() for w in words[i:j])
                    variants.add(sub)

        # For CamelCase input, also split into words for broader matching
        if len(words) == 1 and any(c.isupper() for c in raw[1:]):
            # Split CamelCase: "CreateSqlPackage" -> ["Create", "Sql", "Package"]
            camel_words = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw).split()
            if len(camel_words) > 1:
                for cw in camel_words:
                    variants.add(cw)
                    variants.add(cw.lower())
                # Also add partial CamelCase combos
                for i in range(len(camel_words)):
                    for j in range(i + 2, len(camel_words) + 1):
                        sub = "".join(camel_words[i:j])
                        variants.add(sub)
                        variants.add(sub.lower())

        # Single-token snake_case / kebab-case → generate CamelCase +
        # camelCase variants (2026-04-21 fuzz-match bug fix).
        # Agents often search `heuristic_judge` when the symbol is
        # `HeuristicJudge`; prior to this branch the input had only
        # one "word" so the multi-word branch above didn't fire,
        # and the separator-only rewrites below only stripped `_`
        # without re-casing to PascalCase.
        if len(words) == 1 and ("_" in raw or "-" in raw):
            parts = re.split(r"[_\-]+", raw)
            parts = [p for p in parts if p]
            if len(parts) > 1:
                variants.add("".join(p.capitalize() for p in parts))  # PascalCase
                variants.add(
                    parts[0].lower() + "".join(p.capitalize() for p in parts[1:]),
                )  # camelCase
                for p in parts:
                    variants.add(p)
                    variants.add(p.lower())
                    variants.add(p.capitalize())

        for token in tokens:
            variants.add(token)
            variants.add(token.lower())

            # Separator-normalized siblings — "my-tool" ↔ "my_tool" is a
            # constant source of zero-hit searches in mixed Python/JS
            # repos where the casing convention differs by language.
            if "_" in token:
                variants.add(token.replace("_", "-"))
                variants.add(token.replace("_", ""))
            if "-" in token:
                variants.add(token.replace("-", "_"))
                variants.add(token.replace("-", ""))
            if "." in token:
                variants.add(token.replace(".", "_"))
                variants.add(token.replace(".", "-"))

            if token.endswith("s") and len(token) > 3:
                variants.add(token[:-1])
            else:
                variants.add(token + "s")

            for suffix in suffixes:
                if token.endswith(suffix) and len(token) > len(suffix):
                    variants.add(token[: -len(suffix)])
                variants.add(token + suffix)

            if token.startswith("Is") and len(token) > 2:
                variants.add(token[2:])
            elif len(token) > 1:
                variants.add("Is" + token[:1].upper() + token[1:])

            if token.startswith("Has") and len(token) > 3:
                variants.add(token[3:])
            elif len(token) > 1:
                variants.add("Has" + token[:1].upper() + token[1:])

        return [item for item in variants if item]

    @staticmethod
    def _edit_distance_le_1(a: str, b: str) -> bool:
        """Return True when `a` and `b` differ by at most one edit.

        Covers insert, delete, substitute, AND adjacent transpositions
        (Damerau-Levenshtein) — transpositions account for roughly
        40 % of real-world typos and standard Levenshtein would charge
        them as 2 edits. Skips the general DP because we only care
        about the threshold, not the exact distance.
        """
        if a == b:
            return True
        la, lb = len(a), len(b)
        if abs(la - lb) > 1:
            return False
        if la == lb:
            # Detect exactly-one transposition: the first mismatch
            # position must swap with its neighbor and the rest match.
            diff_positions = [i for i in range(la) if a[i] != b[i]]
            if len(diff_positions) == 2:
                i, j = diff_positions
                if j == i + 1 and a[i] == b[j] and a[j] == b[i]:
                    return True
            diff = 0
            for ca, cb in zip(a, b):
                if ca != cb:
                    diff += 1
                    if diff > 1:
                        return False
            return diff <= 1
        short, long = (a, b) if la < lb else (b, a)
        i = j = 0
        seen_gap = False
        while i < len(short) and j < len(long):
            if short[i] != long[j]:
                if seen_gap:
                    return False
                seen_gap = True
                j += 1
            else:
                i += 1
                j += 1
        return True

    def _path_weight(self, project_root: Path, path: str) -> int:
        lower = path.lower()
        score = 0
        config = load_index_config()
        positive_tokens = config.get(
            "path_weight_positive",
            (
                "/src/",
                "/app/",
                "/web/",
                "/components/",
                "/services/",
                "/controllers/",
                "/models/",
                "/domain/",
                "/infrastructure/",
                "/application/",
            ),
        )
        negative_tokens = config.get(
            "path_weight_negative",
            (
                "/test/",
                "/tests/",
                "/fixture/",
                "/fixtures/",
                "/mock/",
                "/mocks/",
                "/example/",
                "/examples/",
                "/template/",
                "/templates/",
                "/generated/",
                "/snapshot/",
                "/assets/",
                "/pwaassets/",
                "/wwwroot/lib/",
                "/static/",
            ),
        )
        pos_score = config.get("path_weight_positive_score", 20)
        neg_score = config.get("path_weight_negative_score", -35)
        for token in positive_tokens:
            if token in lower:
                score += pos_score
        for token in negative_tokens:
            if token in lower:
                score += neg_score
        hints = self._load_indexing_hints(project_root)
        for root in hints["preferred_roots"]:
            if lower.startswith(root):
                score += 40
        for root in hints["avoid_roots"]:
            if lower.startswith(root):
                score -= 60
        return score

    _ROLE_RELEVANCE_DEFAULT: dict[str, int] = {
        "service": 25,
        "controller": 20,
        "page-model": 18,
        "page-view": 15,
        "data-model": 15,
        "policy": 15,
        "partial-view": 12,
        "abstraction": 10,
        "configuration": 8,
        "utility": 5,
        "script": 3,
        "resource": 2,
        "asset-style": 1,
        "asset-style-source": 1,
    }

    @property
    def _ROLE_RELEVANCE(self) -> dict[str, int]:
        config = load_index_config()
        config_relevance = config.get("role_relevance")
        if config_relevance and isinstance(config_relevance, dict):
            return {k: int(v) for k, v in config_relevance.items()}
        return self._ROLE_RELEVANCE_DEFAULT

    def _role_relevance_boost(self, project_root: Path, path: str) -> int:
        """Score boost based on file role — services and controllers rank highest."""
        with self.store.connect(project_root) as conn:
            row = conn.execute(
                "SELECT role FROM code_files WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
        if not row or not row["role"]:
            return 0
        return self._ROLE_RELEVANCE.get(row["role"], 0)

    def _load_indexing_hints(self, project_root: Path) -> dict[str, list[str]]:
        cache_key = str(project_root)
        if cache_key in self.store._indexing_hint_cache:
            return self.store._indexing_hint_cache[cache_key]

        hints = {"preferred_roots": [], "avoid_roots": []}
        config_path = project_root / ".MEMORY" / "config" / "indexing.md"
        if not config_path.is_file():
            self.store._indexing_hint_cache[cache_key] = hints
            return hints

        current = None
        for raw in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                current = line[3:].strip().lower()
                continue
            if not line.startswith("-"):
                continue
            value = line[1:].strip().strip("`").replace("\\", "/").strip().lower().lstrip("/")
            if not value:
                continue
            if current == "preferred roots":
                hints["preferred_roots"].append(value)
            elif current == "avoid roots":
                hints["avoid_roots"].append(value)

        self.store._indexing_hint_cache[cache_key] = hints
        return hints

    def _ensure_parsed_candidates(self, project_root: Path, query: str, limit: int = 100) -> int:
        needle = query.strip()
        if not needle:
            return 0
        # Split multi-word queries so each word matches independently
        words = needle.split()
        if len(words) > 1:
            clauses = " OR ".join(["(path LIKE ? OR summary LIKE ?)" for _ in words])
            params: list[object] = []
            for word in words:
                pattern = f"%{word}%"
                params.extend([pattern, pattern])
            params.append(limit)
            sql = f"SELECT path FROM code_files WHERE parsed = 0 AND ({clauses}) ORDER BY path ASC LIMIT ?"
        else:
            pattern = f"%{needle}%"
            params = [pattern, pattern, limit]
            sql = "SELECT path FROM code_files WHERE parsed = 0 AND (path LIKE ? OR summary LIKE ?) ORDER BY path ASC LIMIT ?"
        with self.store.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        paths = [row["path"] for row in rows]
        if not paths:
            return 0
        return self.store.sync_code_files(project_root, paths=paths)

    def _infer_plugin_structure_role(self, project_root: Path, path: str) -> str | None:
        rel_path = Path(path)
        name = rel_path.stem.lower()
        suffix = rel_path.suffix.lower()
        if suffix not in {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
            return None
        candidate = rel_path.parent
        checked = 0
        while True:
            if checked > 4:
                break
            abs_candidate = project_root / candidate
            if not abs_candidate.exists():
                break
            has_package = (abs_candidate / "package.json").is_file()
            has_templates = (abs_candidate / "templates").is_dir()
            has_prisma = (abs_candidate / "prisma").is_dir()
            has_generator = any(
                (abs_candidate / f"generator{ext}").is_file()
                for ext in (".js", ".ts", ".mjs", ".cjs")
            )
            marker_count = sum(
                1 for flag in (has_package, has_templates, has_prisma, has_generator) if flag
            )
            if marker_count >= 2 or (
                has_package and (has_templates or has_generator or has_prisma)
            ):
                if name == "generator":
                    return "plugin-generator"
                if name == "index":
                    return "plugin-module"
                if "hooks" in rel_path.parts and (name.startswith("use") or "hook" in name):
                    return "hook-module"
                if "components" in rel_path.parts or self.store._looks_like_component_name(
                    rel_path.stem,
                ):
                    return "component"
                if "middleware" in rel_path.parts:
                    return "middleware"
                if "templates" in rel_path.parts:
                    return "plugin-template-module"
                if name in {
                    "types",
                    "type",
                    "storage",
                    "registry",
                    "constants",
                    "page-key",
                    "evidence",
                }:
                    return "utility-module"
                return None
            if candidate == Path() or candidate == candidate.parent:
                break
            candidate = candidate.parent
            checked += 1
        return None
