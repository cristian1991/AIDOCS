from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .frontend_ast import classify_frontend_symbol_kind
from .language_descriptors import (
    descriptor_for_language,
    extractor_family_for_language,
    line_patterns_for_language,
    outline_family_for_language,
    outline_patterns_for_language,
    reference_patterns_for_language,
)
from .outline_extractors import (
    extract_csharp_outline,
    extract_css_outline,
    extract_generic_outline,
    extract_line_patterns,
    extract_python_outline,
    extract_python_references,
    extract_resx_outline,
    generic_outline_patterns,
    outline_family_patterns,
)

# Extractor families whose rich rows are AUTHORITATIVE and may overlap the
# descriptor line-pattern rows appended after them — these get the
# reconciliation pass below. Scoped to the C#/Razor families that emit
# namespace-container rows; the language-neutral line-pattern path (modules,
# generic patterns, etc.) is intentionally excluded so its behavior is intact.
_RICH_EXTRACTOR_FAMILIES = {"csharp_rich", "razor_rich"}

# Outline kinds that name an AMBIENT CONTAINER — a scope resolved via the
# `container` field + the independent _namespace_for_path search path — rather
# than a navigable declaration. A standalone row of such a kind is redundant
# when its symbol is already some member row's container.
_AMBIENT_CONTAINER_KINDS = {"namespace"}


def _reconcile_rich_outline(
    rows: list[tuple[str, str, int, str | None, bool]],
) -> list[tuple[str, str, int, str | None, bool]]:
    """Deterministic merge for rich-extractor outlines.

    1. Drop an ambient-container row (e.g. ``namespace``) whose symbol is
       already the ``container`` of another row — it's captured as metadata and
       stays searchable via _namespace_for_path; an EMPTY namespace (container
       of nothing) is kept so it remains navigable.
    2. Collapse rows describing the SAME construct — colliding on
       ``(symbol, kind, line_number)`` — to the richest variant: one carrying a
       ``container`` or ``is_partial`` beats a bare one (e.g. a rich C# class
       row wins over a line-pattern row for the same class). DISTINCT kinds at
       the same symbol/line are preserved (e.g. Razor's ``razor_model`` and a
       descriptor ``model_binding`` are different facets, both kept). Insertion
       order (rich rows first) is preserved.
    """
    containers = {container for (_s, _k, _ln, container, _p) in rows if container}

    def _richness(row: tuple[str, str, int, str | None, bool]) -> int:
        _symbol, _kind, _line, container, is_partial = row
        return (1 if container else 0) + (1 if is_partial else 0)

    best: dict[tuple[str, str, int], tuple[str, str, int, str | None, bool]] = {}
    order: list[tuple[str, str, int]] = []
    for row in rows:
        symbol, kind, line_number, _container, _is_partial = row
        if kind in _AMBIENT_CONTAINER_KINDS and symbol in containers:
            continue
        key = (symbol, kind, line_number)
        if key not in best:
            best[key] = row
            order.append(key)
        elif _richness(row) > _richness(best[key]):
            best[key] = row
    return [best[k] for k in order]


class CodeIndexOutlineService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _summarize(self, text: str, file_name: str, max_lines: int = 8) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()][:max_lines]
        if not lines:
            return file_name
        return " | ".join(lines)[:400]

    def _extract_outline(
        self,
        project_root: Path,
        text: str,
        code_language: str,
    ) -> list[tuple[str, str, int, str | None, bool]]:
        # Prefer the richer fallback extractors for languages where the index relies on
        # extra semantics beyond raw symbol discovery (methods vs functions, initializers,
        # provider/hook/component classification, enum members, partials, etc.).
        if code_language not in {
            "python",
            "javascript",
            "typescript",
            "jsx",
            "tsx",
            "csharp",
            "go",
            "rust",
        }:
            try:
                from .tree_sitter_service import extract_outline as ts_outline

                ext_map = {
                    "java": ".java",
                    "dart": ".dart",
                    "html": ".html",
                    "css": ".css",
                }
                ext = ext_map.get(code_language)
                if ext:
                    from pathlib import Path as P

                    ts_result = ts_outline(P(f"file{ext}"), text)
                    if ts_result:
                        return list(dict.fromkeys(ts_result))
            except ImportError:
                pass

        # Fallback: existing regex/AST pipeline
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        patterns: list[tuple[str, str]] = []
        line_patterns = line_patterns_for_language(project_root, code_language)
        extractor_family = extractor_family_for_language(project_root, code_language)
        if extractor_family == "python_ast":
            outlines.extend(extract_python_outline(text))
        elif extractor_family in {
            "javascript_ast",
            "typescript_ast",
            "jsx_ast",
            "tsx_ast",
        }:
            ast_outline = self.store.frontend_ast.extract_outline(text, code_language)
            if ast_outline is not None:
                for symbol, kind, line_number, container, is_partial in ast_outline:
                    js_kind = classify_frontend_symbol_kind(symbol, kind)
                    outlines.append((symbol, js_kind, line_number, container, is_partial))
                for line_number, line in enumerate(text.splitlines(), start=1):
                    initializer = self._extract_js_initializer(line)
                    if initializer is not None:
                        outlines.append((initializer, "initializer", line_number, None, False))
                self._extract_component_semantics(
                    project_root,
                    code_language,
                    text,
                    ast_outline,
                    outlines,
                )
            else:
                patterns = [
                    (r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
                    (
                        r"^\s*(?:export\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)",
                        "interface",
                    ),
                    (
                        r"^\s*(?:export\s+)?type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=",
                        "type_alias",
                    ),
                    (r"^\s*(?:export\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)", "enum"),
                    (
                        r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)",
                        "function",
                    ),
                    (r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
                    (
                        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(",
                        "function",
                    ),
                    (
                        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?[A-Za-z_][A-Za-z0-9_]*\s*=>",
                        "function",
                    ),
                    (r"^\s*window\.([A-Za-z_][A-Za-z0-9_]*)\s*=", "namespace"),
                    (
                        r"^\s*([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s+)?function",
                        "method",
                    ),
                    (r"""fetch\(\s*['"`](/api/[^'"`]+)['"`]""", "api_call"),
                ]
        elif extractor_family == "csharp_rich":
            outlines.extend(extract_csharp_outline(text))
        elif extractor_family == "razor_rich":
            # Calls the rich Razor extractor (regex AIDOCS-domain patterns
            # + Roslyn merge for @page/@model/@inject/@using/@code members).
            # Without this branch, .cshtml falls through to generic
            # line-patterns and loses every @code/@functions block member.
            outlines.extend(self._extract_razor_outline(text))
        elif extractor_family == "resx_rich":
            outlines.extend(extract_resx_outline(text))
        elif extractor_family == "css_rich":
            outlines.extend(extract_css_outline(text))
        else:
            patterns = outline_patterns_for_language(project_root, code_language)
            if not patterns:
                family = outline_family_for_language(project_root, code_language)
                if family:
                    patterns = outline_family_patterns(family)
            if not patterns:
                patterns = generic_outline_patterns(code_language)

        for symbol, kind, line_number, container, is_partial in extract_generic_outline(
            text,
            patterns,
        ):
            js_kind = (
                classify_frontend_symbol_kind(symbol, kind)
                if code_language in {"javascript", "typescript", "jsx", "tsx"}
                else kind
            )
            outlines.append((symbol, js_kind, line_number, container, is_partial))
        for symbol, kind, line_number, container, is_partial in extract_line_patterns(
            text,
            line_patterns,
        ):
            outlines.append((symbol, kind, line_number, container, is_partial))
        if code_language in {"javascript", "typescript", "jsx", "tsx"}:
            for line_number, line in enumerate(text.splitlines(), start=1):
                initializer = self._extract_js_initializer(line)
                if initializer is not None:
                    outlines.append((initializer, "initializer", line_number, None, False))
        # Reconciliation (2026-06-04): a RICH extractor (C#/Razor/etc.) emits the
        # full typed outline AND may overlap with the descriptor line-pattern
        # rows appended above. Where both describe the same (symbol, line), the
        # richer row (carrying container / is_partial / a specific kind) wins.
        # A standalone AMBIENT-CONTAINER row (a `namespace` that is already the
        # `container` of a member row) is dropped from the OUTLINE — it's
        # captured as container metadata, and namespace *discoverability* lives
        # in the independent _namespace_for_path search path, not a bare outline
        # entry. An empty namespace (container of nothing) is kept, so it stays
        # navigable. This pass runs ONLY for rich families; the language-neutral
        # line-pattern path keeps its existing exact-dedup behavior intact.
        if extractor_family in _RICH_EXTRACTOR_FAMILIES:
            return _reconcile_rich_outline(outlines)
        return list(dict.fromkeys(outlines))

    # Bounded reference extraction (Empire goal 2026-06-20). These bounds ARE
    # the runtime cage for descriptor-supplied regex — Python `re` has no
    # execution timeout, so we scan per-line, clamp line length, and cap total
    # matches. A malformed pattern is dropped, never crashes the sync.
    _REF_MAX_LINE_LEN = 2000
    _REF_MAX_MATCHES = 5000

    def _extract_references(
        self,
        project_root: Path,
        text: str,
        code_language: str,
    ) -> list[tuple[str, int, str, str, str]]:
        """Return (token, line_number, kind, raw, enclosing) reference rows.

        AST-sourced rows come first (Python only, today): real call sites and
        function-local imports, each carrying the enclosing symbol. Those are
        strictly code — a token in a comment or a string literal cannot reach
        this table, which is the whole point of parsing instead of scanning.

        Descriptor `reference_patterns` rows follow. Those ARE heuristic regex
        (labeled as such in the ref-integrity report), carry no enclosing
        symbol, and stay bounded by the caps above.
        """
        rows: list[tuple[str, int, str, str, str]] = []
        if code_language == "python":
            rows.extend(extract_python_references(text))
        patterns = reference_patterns_for_language(project_root, code_language)
        if not patterns:
            return rows
        compiled: list[tuple[Any, str, int]] = []
        for pattern, kind, capture in patterns:
            try:
                compiled.append((re.compile(pattern), kind, capture))
            except re.error:
                continue
        if not compiled:
            return rows
        pattern_rows = 0
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line[: self._REF_MAX_LINE_LEN]
            for regex, kind, capture in compiled:
                for match in regex.finditer(line):
                    try:
                        token = match.group(capture) if capture else match.group(0)
                    except (IndexError, re.error):
                        continue
                    if not token:
                        continue
                    token = token.strip()
                    if not token:
                        continue
                    rows.append((token, line_number, kind, line.strip()[:200], ""))
                    pattern_rows += 1
                    if pattern_rows >= self._REF_MAX_MATCHES:
                        return rows
        return rows

    def _extract_csharp_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """C# outline — the TRUSTED Roslyn outliner only (no regex twin).

        A second, hand-written regex C# extractor used to live here; it is
        gone with the one in outline_extractors.csharp. Raises
        OutlinerUnavailable when the outliner cannot be used."""
        return extract_csharp_outline(text)

    def _extract_python_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return outlines

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                outlines.append((node.name, "class", node.lineno, None, False))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        outlines.append((child.name, "method", child.lineno, node.name, False))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "function"
                if node.name.startswith("use") and len(node.name) > 3 and node.name[3:4].isupper():
                    kind = "hook"
                outlines.append((node.name, kind, node.lineno, None, False))

        return outlines

    def _extract_component_semantics(
        self,
        project_root: Path,
        code_language: str,
        text: str,
        ast_outline: list[tuple[str, str, int, str | None, bool]],
        outlines: list[tuple[str, str, int, str | None, bool]],
    ) -> None:
        """Scan component/hook function bodies for semantic sub-blocks defined in TOML."""
        import re as _re

        descriptor = descriptor_for_language(project_root, "", f".{code_language}")
        if descriptor is None:
            return
        semantics = descriptor.component_semantics
        if not semantics:
            return

        lines = text.splitlines()
        # Only scan inside component and hook functions
        component_symbols = [
            (name, kind, line_num)
            for name, kind, line_num, _container, _partial in ast_outline
            if kind in {"component", "hook", "context_provider"}
        ]

        for comp_name, _comp_kind, comp_start in component_symbols:
            # Find end of component using brace balance
            start_idx = comp_start - 1
            comp_end = self._find_brace_end(lines, start_idx)

            for category, patterns in semantics.items():
                for line_num in range(comp_start, min(comp_end + 1, len(lines) + 1)):
                    line = lines[line_num - 1] if line_num <= len(lines) else ""
                    for pattern in patterns:
                        if pattern.startswith("^"):
                            if _re.search(pattern, line):
                                outlines.append(
                                    (
                                        f"{category}@L{line_num}",
                                        category,
                                        line_num,
                                        comp_name,
                                        False,
                                    ),
                                )
                                break
                        elif pattern in line:
                            outlines.append(
                                (
                                    f"{category}@L{line_num}",
                                    category,
                                    line_num,
                                    comp_name,
                                    False,
                                ),
                            )
                            break

    @staticmethod
    def _find_brace_end(lines: list[str], start_idx: int) -> int:
        """Find the closing brace line for a function starting at start_idx."""
        balance = 0
        seen_open = False
        for i in range(start_idx, len(lines)):
            balance += lines[i].count("{") - lines[i].count("}")
            if lines[i].count("{") > 0:
                seen_open = True
            if seen_open and balance <= 0:
                return i + 1
        return min(start_idx + 50, len(lines))

    def _extract_js_initializer(self, line: str) -> str | None:
        checks = [
            (
                r"document\.addEventListener\(\s*['\"]DOMContentLoaded['\"]",
                "document:DOMContentLoaded",
            ),
            (r"\$\(document\)\.ready\s*\(", "jquery:ready"),
            (r"window\.addEventListener\(\s*['\"]load['\"]", "window:load"),
            (r"window\.addEventListener\(\s*['\"]resize['\"]", "window:resize"),
        ]
        for pattern, symbol in checks:
            if re.search(pattern, line):
                return symbol
        return None

    def _extract_razor_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """Extract symbols from Razor .cshtml files.

        Doctrine (2026-05-28, "space-faring empire"): this method is now
        the AIDOCS-DOMAIN half of the Razor outline. C#-language symbols
        (@page, @model, @inject, @using, and ALL @code/@functions block
        members) are extracted by the Roslyn daemon and merged in via
        outline_extractors.razor.merge_cshtml_outline. This method
        keeps the framework + i18n patterns Roslyn doesn't try:
        Lang.T("..."), <partial name=...>, Html.PartialAsync(...),
        @RenderSection, @section, Layout="...", asp-page-handler forms,
        Component.InvokeAsync, asp-for bindings, data-* attrs,
        permission checks, inline JS, API endpoint calls.
        Why: zero overlap with Roslyn → merge is union, not dedup.
        Measured on DentalClinic Edit.cshtml (2754L): regex 168
        entries + Roslyn 5 entries = merged 173 entries, no collisions.
        Apply: removed model_pattern, inject_pattern, page_directive,
        and the functions_block marker — Roslyn surfaces these
        with richer semantics (fully-qualified model, typed inject
        names, etc.). All other patterns stay.
        """
        outlines: list[tuple[str, str, int, str | None, bool]] = []

        # Regex patterns for razor constructs — TRIMMED to non-Roslyn
        # surface. Anything Roslyn parses (model / inject / page / @code
        # members) is no longer matched here.
        section_pattern = re.compile(r"@section\s+([A-Za-z_][A-Za-z0-9_]*)")
        partial_tag = re.compile(r'<partial\s+name="([^"]+)"', re.IGNORECASE)
        partial_async = re.compile(r'Html\.PartialAsync\(\s*"([^"]+)"')
        lang_t_pattern = re.compile(r'Lang\.T\(\s*"([^"]+)"\s*\)')
        form_tag = re.compile(
            r"<form[^>]*(?:asp-page-handler|asp-action|asp-page)\s*=\s*\"([^\"]+)\"",
            re.IGNORECASE,
        )
        component_pattern = re.compile(r"@(?:await\s+)?Component\.InvokeAsync\(\s*\"([^\"]+)\"")
        render_section = re.compile(r"@RenderSection\(\s*\"([^\"]+)\"")
        layout_pattern = re.compile(r'^\s*Layout\s*=\s*"([^"]+)"')

        # asp-for bindings (view↔model contract)
        asp_for_pattern = re.compile(r'asp-for="([^"]+)"')
        # data-* attributes (HTML↔JS contract)
        data_attr_pattern = re.compile(r"\bdata-([a-z][a-z0-9-]*)")
        # Permission/auth checks in views
        perm_check_patterns = [
            re.compile(
                r"@if\s*\(\s*Model\.([A-Za-z_]*(?:Can|Has|Is|Allow|Enable|Show|Permission)[A-Za-z_]*)",
            ),
            re.compile(r"@if\s*\(\s*(?:User|Context\.User)\.IsInRole\(\s*\"([^\"]+)\""),
            re.compile(r"@if\s*\(\s*ViewData\[\"([A-Za-z_]*(?:Can|Has|Is)[A-Za-z_]*)\""),
        ]

        seen_translations: set[str] = set()
        seen_asp_for: set[str] = set()
        seen_data_attrs: set[str] = set()

        for line_number, line in enumerate(text.splitlines(), start=1):
            # Layout assignment
            m = layout_pattern.search(line)
            if m:
                outlines.append((m.group(1), "layout_ref", line_number, None, False))
                continue

            # @section definitions
            for m in section_pattern.finditer(line):
                outlines.append((m.group(1), "section", line_number, None, False))

            # <partial> tag helper references
            for m in partial_tag.finditer(line):
                outlines.append((m.group(1), "partial_ref", line_number, None, False))

            # Html.PartialAsync references
            for m in partial_async.finditer(line):
                outlines.append((m.group(1), "partial_ref", line_number, None, False))

            # @await Component.InvokeAsync
            for m in component_pattern.finditer(line):
                outlines.append((m.group(1), "component_ref", line_number, None, False))

            # @RenderSection
            for m in render_section.finditer(line):
                outlines.append((m.group(1), "render_section", line_number, None, False))

            # Form handlers (asp-page-handler, asp-action, asp-page)
            for m in form_tag.finditer(line):
                outlines.append((m.group(1), "form_handler", line_number, None, False))

            # Translation keys — Lang.T("...")
            for m in lang_t_pattern.finditer(line):
                key = m.group(1)
                if key not in seen_translations:
                    seen_translations.add(key)
                    outlines.append((key, "translation_key", line_number, None, False))

            # Inline JS: function declarations inside <script> blocks
            func_match = re.match(r"\s*(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if func_match:
                outlines.append((func_match.group(1), "js_function", line_number, None, False))

            # Inline JS: fetch/AJAX endpoint calls
            for m in re.finditer(r"""fetch\(\s*[`'"](/api/[^`'"]+)[`'"]""", line):
                outlines.append((m.group(1), "api_call", line_number, None, False))

            # Inline JS: $.ajax, $.get, $.post URL patterns
            for m in re.finditer(
                r"""\$\.(?:ajax|get|post|getJSON)\(\s*[`'"](/api/[^`'"]+)[`'"]""",
                line,
            ):
                outlines.append((m.group(1), "api_call", line_number, None, False))

            # asp-for bindings (view↔model property contract)
            for m in asp_for_pattern.finditer(line):
                binding = m.group(1)
                if binding not in seen_asp_for:
                    seen_asp_for.add(binding)
                    outlines.append((binding, "asp_for_binding", line_number, None, False))

            # data-* attributes (HTML↔JS contract)
            for m in data_attr_pattern.finditer(line):
                attr = m.group(1)
                if attr not in seen_data_attrs and attr not in {
                    "toggle",
                    "bs-toggle",
                    "bs-target",
                    "bs-dismiss",
                }:
                    seen_data_attrs.add(attr)
                    outlines.append((f"data-{attr}", "data_attribute", line_number, None, False))

            # Permission/auth checks
            for perm_re in perm_check_patterns:
                for m in perm_re.finditer(line):
                    outlines.append((m.group(1), "permission_check", line_number, None, False))

        # Merge in the Roslyn-extracted C#-side symbols. The Roslyn
        # daemon supplies @page route, @model fully-qualified, @inject
        # locals + types, @using directives, AND every member declared
        # inside @code / @functions blocks (fields, properties, methods,
        # nested records/enums) at their correct source line numbers.
        # The two extractors are non-overlapping by design — see the
        # docstring on merge_cshtml_outline + the benchmark numbers.
        # NO regex-only degrade: when the trusted outliner is unavailable,
        # OutlinerUnavailable propagates and the indexer marks the file
        # NOT_INDEXED — a Razor outline missing every @code member is a
        # silent partial, not "better than nothing".
        from .outline_extractors.razor import merge_cshtml_outline

        return merge_cshtml_outline(text, outlines)

    def _extract_resx_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """Extract translation key-value pairs from .resx XML files."""
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        # Match <data name="Key" ...> <value>Value</value> </data>
        data_pattern = re.compile(r'<data\s+name="([^"]+)"')
        value_pattern = re.compile(r"<value>(.*?)</value>", re.DOTALL)

        in_data = False
        current_name: str | None = None
        current_line = 1

        for line_number, line in enumerate(text.splitlines(), start=1):
            m = data_pattern.search(line)
            if m:
                current_name = m.group(1)
                current_line = line_number
                in_data = True
            if in_data and current_name is not None:
                vm = value_pattern.search(line)
                if vm:
                    value = vm.group(1).strip()
                    container = value[:80] if value else None
                    outlines.append((current_name, "translation", current_line, container, False))
                    in_data = False
                    current_name = None
                elif "</data>" in line:
                    # Data block closed without a value — reset state
                    in_data = False
                    current_name = None

        return outlines

    def _extract_css_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """Extract symbols from CSS files: custom classes, CSS variables, @theme vars, @keyframes, @layer."""
        outlines: list[tuple[str, str, int, str | None, bool]] = []

        # Patterns for CSS constructs
        # Match ALL class names in a selector line, not just the first
        class_pattern = re.compile(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)")
        var_pattern = re.compile(r"--([a-zA-Z][a-zA-Z0-9_-]*)\s*:")
        keyframes_pattern = re.compile(r"@keyframes\s+([a-zA-Z_][a-zA-Z0-9_-]*)")
        layer_pattern = re.compile(r"@layer\s+([a-zA-Z_][a-zA-Z0-9_-]*)")
        theme_pattern = re.compile(r"@theme\s*\{")
        variant_pattern = re.compile(r"@variant\s+([a-zA-Z_][a-zA-Z0-9_-]*)")

        in_theme = False
        brace_depth = 0
        theme_depth: int | None = None

        for line_number, line in enumerate(text.splitlines(), start=1):
            opens = line.count("{")
            closes = line.count("}")

            # @theme block
            if theme_pattern.search(line):
                in_theme = True
                theme_depth = brace_depth + opens
                outlines.append(("@theme", "theme_block", line_number, None, False))

            # CSS variables (--color-primary, etc.)
            for m in var_pattern.finditer(line):
                var_name = m.group(1)
                context = "theme" if in_theme else None
                outlines.append((f"--{var_name}", "css_variable", line_number, context, False))

            # Custom classes — extract all class names from selector lines
            # Only match lines that look like selectors (contain { or , or start with .)
            stripped = line.strip()
            if (
                stripped
                and not stripped.startswith("/*")
                and not stripped.startswith("*")
                and not stripped.startswith("//")
            ):
                if (
                    "{" in stripped
                    or "," in stripped
                    or stripped.startswith(".")
                    or stripped.startswith("&")
                ):
                    seen_classes: set[str] = set()
                    for m in class_pattern.finditer(stripped.split("{")[0]):
                        cls_name = m.group(1)
                        if cls_name not in seen_classes:
                            seen_classes.add(cls_name)
                            outlines.append((cls_name, "css_class", line_number, None, False))

            # @keyframes
            m = keyframes_pattern.search(line)
            if m:
                outlines.append((m.group(1), "keyframes", line_number, None, False))

            # @layer
            m = layer_pattern.search(line)
            if m:
                outlines.append((m.group(1), "css_layer", line_number, None, False))

            # @variant
            m = variant_pattern.search(line)
            if m:
                outlines.append((m.group(1), "css_variant", line_number, None, False))

            brace_depth += opens
            brace_depth -= closes
            if in_theme and theme_depth is not None and brace_depth < theme_depth:
                in_theme = False
                theme_depth = None

        return outlines
