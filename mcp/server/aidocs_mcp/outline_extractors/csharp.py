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


def _strip_csharp_literals(line: str, state: dict[str, object]) -> str:
    """Return ``line`` with string/char-literal and comment CONTENT blanked
    to spaces, so a downstream brace count sees only real code braces.

    This is the regex-fallback lane's guard against the string-blind brace
    counter bug: a method returning ``$"}}}}"`` (or a verbatim/raw string,
    or a comment) must not corrupt ``brace_depth``. Precision target is
    "never corrupt depth on a literal", NOT full C# lexing.

    ``state`` carries a tiny amount of cross-line context for constructs
    that span lines:
      - block comments  ``/* ... */``
      - verbatim strings ``@"... "`` (doubled quotes escape, backslash literal)
      - raw strings with a 3+ quote fence (C# 11+, arbitrary-length fence)

    Handled inline (single line): ``//`` comments, char literals ``'{'``,
    regular ``"..."`` (``\\`` escapes), verbatim/interpolated/raw openers.
    Braces inside interpolation holes are blanked with the rest of the
    string — the outline only needs TYPE/MEMBER boundaries, which never
    live inside a hole.
    """
    chars = list(line)
    n = len(chars)

    def wipe(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            chars[k] = " "

    i = 0
    mode = state.get("mode")
    # ── continuations from a previous line ──
    if mode == "block_comment":
        end = line.find("*/")
        if end == -1:
            wipe(0, n)
            return "".join(chars)
        wipe(0, end + 2)
        state["mode"] = None
        i = end + 2
    elif mode == "verbatim":
        j = 0
        closed = False
        while j < n:
            if chars[j] == '"':
                if j + 1 < n and chars[j + 1] == '"':
                    j += 2
                    continue
                j += 1
                closed = True
                break
            j += 1
        wipe(0, j)
        if closed:
            state["mode"] = None
        i = j
    elif mode == "raw":
        fence_len = int(state.get("fence", 3))
        fence = '"' * fence_len
        end = line.find(fence)
        if end == -1:
            wipe(0, n)
            return "".join(chars)
        wipe(0, end + fence_len)
        state["mode"] = None
        i = end + fence_len

    while i < n:
        ch = chars[i]
        # line comment — rest of line is inert
        if ch == "/" and i + 1 < n and chars[i + 1] == "/":
            wipe(i, n)
            break
        # block comment
        if ch == "/" and i + 1 < n and chars[i + 1] == "*":
            end = line.find("*/", i + 2)
            if end == -1:
                wipe(i, n)
                state["mode"] = "block_comment"
                break
            wipe(i, end + 2)
            i = end + 2
            continue
        # char literal
        if ch == "'":
            j = i + 1
            while j < n:
                if chars[j] == "\\":
                    j += 2
                    continue
                if chars[j] == "'":
                    j += 1
                    break
                j += 1
            wipe(i, j)
            i = j
            continue
        if ch == '"':
            run = 0
            k = i
            while k < n and chars[k] == '"':
                run += 1
                k += 1
            if run >= 3:
                # raw string literal — fence is `run` quotes
                fence = '"' * run
                end = line.find(fence, k)
                if end == -1:
                    wipe(i, n)
                    state["mode"] = "raw"
                    state["fence"] = run
                    break
                wipe(i, end + run)
                i = end + run
                continue
            if run == 2:
                # empty string "" (possibly @"" / $"") — nothing inside
                wipe(i, k)
                i = k
                continue
            # run == 1: single-quote-delimited string.
            verbatim = chars[i - 1] == "@" if i - 1 >= 0 else False
            if not verbatim and i - 1 >= 0 and chars[i - 1] == "$":
                verbatim = i - 2 >= 0 and chars[i - 2] == "@"
            if verbatim:
                j = i + 1
                closed = False
                while j < n:
                    if chars[j] == '"':
                        if j + 1 < n and chars[j + 1] == '"':
                            j += 2
                            continue
                        j += 1
                        closed = True
                        break
                    j += 1
                wipe(i, j)
                if not closed:
                    state["mode"] = "verbatim"
                i = j
                continue
            # regular (optionally interpolated) string with \ escapes
            j = i + 1
            while j < n:
                if chars[j] == "\\":
                    j += 2
                    continue
                if chars[j] == '"':
                    j += 1
                    break
                j += 1
            wipe(i, j)
            i = j
            continue
        i += 1

    return "".join(chars)


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
    literal_state: dict[str, object] = {}

    for line_number, line in enumerate(text.splitlines(), start=1):
        # Count braces on a copy with string/char literals + comments blanked,
        # so literals like `$"}}}}"` can't corrupt brace_depth and drop members.
        code_line = _strip_csharp_literals(line, literal_state)
        opens = code_line.count("{")
        closes = code_line.count("}")
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
