from __future__ import annotations

import re


def extract_csharp_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    """Extract a flat outline from C# source text.

    Doctrine (2026-05-28): prefers the Roslyn-backed tool at
    tools/aidocs-csharp-outliner when available. Falls back to the
    legacy regex extractor (this function's tail body) when Roslyn
    isn't installed — keeps the system working on dev boxes without
    dotnet-sdk-9.0 while operators migrate.
    Why: Roslyn IS the C# language definition; aligning with it kills
    a whole class of "regex didn't account for this new language
    feature" bugs (records, primary ctors, file-scoped namespaces,
    etc.). Razor (.cshtml/.razor) is ONLY parseable by the Roslyn
    path — the regex backend has no concept of HTML+@code interleave.
    Apply: callers don't need to know which backend ran; the output
    tuple shape is identical. extract_cshtml_outline / extract_razor_
    outline are the explicit-extension entry points for the indexer.
    """
    try:
        from ..csharp_roslyn_client import roslyn_outline

        rows = roslyn_outline(text)
        if rows is not None:
            return rows
    except Exception:
        # Bridge import or invocation failed — fall through to regex.
        pass
    return _extract_csharp_outline_regex(text)


def _extract_csharp_outline_regex(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    outlines: list[tuple[str, str, int, str | None, bool]] = []
    namespace_name: str | None = None
    type_pattern = re.compile(
        r"^\s*(?:public|private|internal|protected|sealed|abstract|static|unsafe|new|file|readonly|partial|\s)*\b(partial\s+)?(class|interface|struct|record|enum)\s+([A-Za-z_][A-Za-z0-9_]*)",
    )
    method_pattern = re.compile(
        r"^\s*(?:public|private|internal|protected|static|virtual|override|abstract|async|sealed|extern|unsafe|new|partial|\s)+[A-Za-z_<>,\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    )
    property_pattern = re.compile(
        r"^\s*(?:public|private|internal|protected|static|virtual|override|abstract|sealed|required|init|readonly|unsafe|new|\s)+[A-Za-z_<>,\[\]\.?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(?:get;|set;|init;)",
    )
    field_pattern = re.compile(
        r"^\s*(?:public|private|internal|protected|static|readonly|const|volatile|unsafe|new|\s)+[A-Za-z_<>,\[\]\.?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)",
    )
    namespace_pattern = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_\.]*)")
    http_attr_pattern = re.compile(
        r'\[(Http(?:Get|Post|Put|Delete|Patch))(?:\(\s*"([^"]*)"\s*\))?\]',
    )
    route_attr_pattern = re.compile(r'\[Route\(\s*"([^"]*)"\s*\)\]')
    authorize_attr_pattern = re.compile(
        r'\[Authorize(?:\(\s*(?:Roles\s*=\s*"([^"]*)")?(?:Policy\s*=\s*"([^"]*)")?\s*\))?\]',
    )
    allow_anon_pattern = re.compile(r"\[AllowAnonymous\]")
    validation_attr_pattern = re.compile(
        r"\[(Required|MaxLength|MinLength|StringLength|Range|RegularExpression|EmailAddress|Phone|Url|Compare|CreditCard)(?:\(\s*([^)]*)\s*\))?\]",
    )

    current_type: str | None = None
    current_kind: str | None = None
    brace_depth = 0
    type_depth: int | None = None
    inside_enum = False
    pending_attrs: list[tuple[str, str, int]] = []
    is_hub_class = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        opens = line.count("{")
        closes = line.count("}")
        ns_match = namespace_pattern.match(line)
        if ns_match:
            namespace_name = ns_match.group(1)
        for m in http_attr_pattern.finditer(line):
            verb = m.group(1)
            route = m.group(2) or ""
            endpoint = f"{verb}:{route}" if route else verb
            pending_attrs.append((endpoint, "http_endpoint", line_number))
        m = route_attr_pattern.search(line)
        if m:
            pending_attrs.append((m.group(1), "route", line_number))
        m = authorize_attr_pattern.search(line)
        if m:
            role = m.group(1)
            policy = m.group(2)
            auth_detail = role or policy or "default"
            pending_attrs.append((auth_detail, "authorize", line_number))
        if allow_anon_pattern.search(line):
            pending_attrs.append(("AllowAnonymous", "authorize", line_number))
        for m in validation_attr_pattern.finditer(line):
            attr_name = m.group(1)
            attr_args = m.group(2) or ""
            val_symbol = f"{attr_name}({attr_args})" if attr_args else attr_name
            pending_attrs.append((val_symbol, "validation", line_number))

        type_match = type_pattern.match(line)
        if type_match:
            is_partial = bool(type_match.group(1)) or " partial " in f" {line} "
            kind = type_match.group(2)
            symbol = type_match.group(3)
            container = namespace_name
            outlines.append((symbol, kind, line_number, container, is_partial))
            current_type = symbol
            current_kind = kind
            type_depth = brace_depth + 1
            inside_enum = kind == "enum"
            is_hub_class = ": Hub" in line or ":Hub" in line
            for attr_sym, attr_kind, attr_line in pending_attrs:
                outlines.append((attr_sym, attr_kind, attr_line, symbol, False))
            pending_attrs.clear()

        method_match = method_pattern.match(line)
        if method_match and current_type is not None:
            symbol = method_match.group(1)
            method_kind = "method"
            if is_hub_class and symbol not in {"OnConnectedAsync", "OnDisconnectedAsync"}:
                method_kind = "hub_method"
            outlines.append((symbol, method_kind, line_number, current_type, False))
            for attr_sym, attr_kind, attr_line in pending_attrs:
                outlines.append((attr_sym, attr_kind, attr_line, current_type, False))
            pending_attrs.clear()

        property_match = property_pattern.match(line)
        if property_match and current_type is not None and current_kind != "enum":
            symbol = property_match.group(1)
            outlines.append((symbol, "property", line_number, current_type, False))
            for attr_sym, attr_kind, attr_line in pending_attrs:
                if attr_kind == "validation":
                    outlines.append(
                        (f"{symbol}:{attr_sym}", "validation", attr_line, current_type, False),
                    )
            pending_attrs = [(s, k, l) for s, k, l in pending_attrs if k != "validation"]

        field_match = field_pattern.match(line)
        if field_match and current_type is not None and current_kind != "enum":
            symbol = field_match.group(1)
            outlines.append((symbol, "field", line_number, current_type, False))

        if inside_enum and current_type is not None:
            enum_member = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*[^,]+)?\s*,?\s*$", line)
            if enum_member:
                symbol = enum_member.group(1)
                if symbol not in {"public", "private", "internal", "protected"}:
                    outlines.append((symbol, "enum_member", line_number, current_type, False))

        stripped = line.strip()
        if not stripped or stripped in {"{", "}"}:
            pending_attrs.clear()

        brace_depth += opens
        brace_depth -= closes
        if type_depth is not None and brace_depth < type_depth - 1:
            current_type = None
            current_kind = None
            type_depth = None
            inside_enum = False
            is_hub_class = False
            pending_attrs.clear()

    return outlines
