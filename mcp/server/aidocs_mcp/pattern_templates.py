"""Named pattern templates for TOML line_patterns.

Instead of writing raw regex in TOML files, users can use named matchers:

    # Raw regex (power users only):
    { pattern = "\\\\bt\\\\(\\\\s*[`'\"]([^`'\"]+)[`'\"]", kind = "translation_key" }

    # Named matcher (anyone can understand):
    { match = "function_call", function = "t", kind = "translation_key" }

The TOML loader expands `match` entries into regex `pattern` at load time.
If both `match` and `pattern` are present, `pattern` wins (escape hatch).
"""

from __future__ import annotations

import re


def expand_match_to_pattern(entry: dict[str, str]) -> str | None:
    """Expand a named match entry into a regex pattern string.

    Returns None if the match type is unknown (caller should warn).
    """
    match_type = entry.get("match", "").strip()
    if not match_type:
        return None

    expander = _TEMPLATES.get(match_type)
    if expander is None:
        return None

    return expander(entry)


# ── Template definitions ──
# Each template is a function: dict → regex pattern string


def _function_call(entry: dict[str, str]) -> str:
    """Match a function/method call and capture its first string argument.

    Examples:
        { match = "function_call", function = "t" }
        → matches: t("hello"), t('hello'), t(`hello`)

        { match = "function_call", function = "Lang.T" }
        → matches: Lang.T("hello")

        { match = "function_call", function = "fetch", args_starts_with = "/api/" }
        → matches: fetch("/api/patients") but not fetch("/other")

    """
    fn = entry.get("function", "")
    if not fn:
        return None
    escaped_fn = re.escape(fn)
    # Add word boundary for short function names (avoid matching substring)
    prefix = r"\b" if len(fn) <= 3 and fn.isalpha() else ""
    args_filter = entry.get("args_starts_with", "")
    if args_filter:
        escaped_filter = re.escape(args_filter)
        return rf"""{prefix}{escaped_fn}\(\s*[`'"](({escaped_filter})[^`'"]*)[`'"]"""
    return rf"""{prefix}{escaped_fn}\(\s*[`'"]([^`'"]+)[`'"]"""


def _attribute(entry: dict[str, str]) -> str:
    """Match an HTML/Razor attribute and capture its value.

    Examples:
        { match = "attribute", name = "asp-for" }
        → matches: asp-for="PatientName"

        { match = "attribute", names = "asp-page,asp-action,asp-page-handler" }
        → matches any of the three attributes

    """
    names = entry.get("names", "")
    name = entry.get("name", "")
    if names:
        alternatives = "|".join(re.escape(n.strip()) for n in names.split(",") if n.strip())
        return rf"""(?:{alternatives})\s*=\s*"([^"]+)\""""
    if name:
        return rf"""{re.escape(name)}\s*=\s*"([^"]+)\""""
    return None


def _tag_attribute(entry: dict[str, str]) -> str:
    """Match an HTML tag with a specific attribute and capture the attribute value.

    Examples:
        { match = "tag_attribute", tag = "partial", attribute = "name" }
        → matches: <partial name="_Toolbar" />

    """
    tag = entry.get("tag", "")
    attr = entry.get("attribute", "")
    if not tag or not attr:
        return None
    return rf"""{re.escape(tag)}\s+{re.escape(attr)}="([^"]+)\""""


def _data_attribute(entry: dict[str, str]) -> str:
    """Match HTML data-* attributes and capture the attribute name.

    Examples:
        { match = "data_attribute" }
        → matches: data-tab="overview", captures "tab"

    """
    return r"data-([a-z][a-z0-9-]*)"


def _css_at_rule(entry: dict[str, str]) -> str:
    """Match a CSS @-rule and capture its identifier.

    Examples:
        { match = "css_at_rule", rule = "keyframes" }
        → matches: @keyframes fadeIn

        { match = "css_at_rule", rule = "media", capture = "block" }
        → matches: @media (min-width: 768px) { ... }, captures the condition

    """
    rule = entry.get("rule", "")
    if not rule:
        return None
    capture = entry.get("capture", "identifier")
    if capture == "block":
        return rf"@{re.escape(rule)}\s+([^{{]+)"
    return rf"@{re.escape(rule)}\s+([A-Za-z_][A-Za-z0-9_-]*)"


def _css_at_rule_block(entry: dict[str, str]) -> str:
    """Match a CSS @-rule block start (no capture, just detect).

    Examples:
        { match = "css_at_rule_block", rule = "theme" }
        → matches: @theme {

    """
    rule = entry.get("rule", "")
    if not rule:
        return None
    return rf"@{re.escape(rule)}\s*\{{"


def _css_variable(entry: dict[str, str]) -> str:
    """Match CSS custom property declarations.

    Examples:
        { match = "css_variable" }
        → matches: --color-primary: #3B82F6, captures "color-primary"

    """
    return r"--([A-Za-z][A-Za-z0-9_-]*)\s*:"


def _razor_directive(entry: dict[str, str]) -> str:
    """Match a Razor directive and capture its argument.

    Examples:
        { match = "razor_directive", directive = "@page" }
        → matches: @page "/patients/{id}"

        { match = "razor_directive", directive = "@model" }
        → matches: @model DentalApp.Web.Pages.Patients.DetailsModel

        { match = "razor_directive", directive = "@section" }
        → matches: @section Scripts

    """
    directive = entry.get("directive", "")
    if not directive:
        return None
    escaped = re.escape(directive)
    return rf"""^\s*{escaped}(?:\s+"?([^"\s]*)"?)?"""


def _razor_code_block(entry: dict[str, str]) -> str:
    """Match Razor code block directives.

    Examples:
        { match = "razor_code_block", directives = "@functions,@code" }
        → matches: @functions { or @code {

    """
    directives = entry.get("directives", "@functions,@code")
    parts = [re.escape(d.strip()) for d in directives.split(",") if d.strip()]
    if not parts:
        return None
    alternatives = "|".join(parts)
    return rf"""^\s*({alternatives})\s*\{{"""


def _route_navigation(entry: dict[str, str]) -> str:
    """Match client-side navigation calls and capture the route.

    Examples:
        { match = "route_navigation" }
        → matches: router.push("/patients"), navigate("/home"), redirect("/login")

        { match = "route_navigation", functions = "router.push,navigate,redirect,goto" }
        → custom list of navigation functions

    """
    functions = entry.get("functions", "router.push,navigate,redirect")
    parts = [re.escape(f.strip()) for f in functions.split(",") if f.strip()]
    alternatives = "|".join(parts)
    return rf"""(?:{alternatives})\(\s*[`'"]([^`'"]+)[`'"]"""


def _decorator_route(entry: dict[str, str]) -> str:
    """Match Python decorator-style route definitions.

    Examples:
        { match = "decorator_route", decorator = "@app" }
        → matches: @app.route("/api/patients"), @app.get("/api/users")

        { match = "decorator_route", function = "path" }
        → matches: path("/api/patients/")

    """
    decorator = entry.get("decorator", "")
    function = entry.get("function", "")
    if decorator:
        escaped = re.escape(decorator)
        return rf"""{escaped}\.(?:route|get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]"""
    if function:
        escaped = re.escape(function)
        return rf"""{escaped}\(\s*['"]([^'"]+)['"]"""
    return None


def _jquery_ajax(entry: dict[str, str]) -> str:
    """Match jQuery AJAX calls and capture the URL.

    Examples:
        { match = "jquery_ajax" }
        → matches: $.ajax("/api/..."), $.get("/api/..."), $.post("/api/...")

        { match = "jquery_ajax", args_starts_with = "/api/" }
        → only matches URLs starting with /api/

    """
    args_filter = entry.get("args_starts_with", "")
    if args_filter:
        escaped_filter = re.escape(args_filter)
        return rf"""\$\.(?:ajax|get|post|getJSON)\(\s*[`'"](({escaped_filter})[^`'"]*)[`'"]"""
    return r"""\$\.(?:ajax|get|post|getJSON)\(\s*[`'"](/[^`'"]+)[`'"]"""


def _assignment(entry: dict[str, str]) -> str:
    """Match a property assignment and capture the value.

    Examples:
        { match = "assignment", property = "Layout" }
        → matches: Layout = "_Layout", captures "_Layout"

    """
    prop = entry.get("property", "")
    if not prop:
        return None
    return rf"""^\s*{re.escape(prop)}\s*=\s*"([^"]+)\""""


def _js_function_declaration(entry: dict[str, str]) -> str:
    """Match JavaScript/TypeScript function declarations.

    Examples:
        { match = "js_function_declaration" }
        → matches: function doSomething(, async function loadData(

    """
    return r"^\s*(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("


def _css_media_feature(entry: dict[str, str]) -> str:
    """Match CSS media query features.

    Examples:
        { match = "css_media_feature" }
        → matches: (min-width: 768px), captures "min-width:768px"

    """
    features = "min-width|max-width|min-height|max-height|min-resolution|max-resolution|resolution|orientation|prefers-color-scheme|prefers-reduced-motion|scan|hover|pointer"
    return rf"\(({features})\s*:\s*([^\)]+)\)"


# ── Template registry ──

_TEMPLATES: dict[str, callable] = {
    "function_call": _function_call,
    "attribute": _attribute,
    "tag_attribute": _tag_attribute,
    "data_attribute": _data_attribute,
    "css_at_rule": _css_at_rule,
    "css_at_rule_block": _css_at_rule_block,
    "css_variable": _css_variable,
    "css_media_feature": _css_media_feature,
    "razor_directive": _razor_directive,
    "razor_code_block": _razor_code_block,
    "route_navigation": _route_navigation,
    "decorator_route": _decorator_route,
    "jquery_ajax": _jquery_ajax,
    "assignment": _assignment,
    "js_function_declaration": _js_function_declaration,
}


def available_templates() -> dict[str, str]:
    """Return {name: docstring} for all available pattern templates."""
    return {name: (fn.__doc__ or "").strip().split("\n")[0] for name, fn in _TEMPLATES.items()}
