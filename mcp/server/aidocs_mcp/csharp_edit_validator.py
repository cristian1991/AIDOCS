"""C# edit-time syntax validator (Phase 1).

Parses C# source with tree-sitter-c-sharp and reports ERROR nodes.
Incremental parsing and per-file cache are Phase 2; hook integration
is Phase 3.

#52 (canonical 2026-04-27): tree-sitter and tree-sitter-c-sharp are
HARD DEPS in pyproject.toml — no fail-open path. If either import
fails, install is broken; raising at import time is louder and faster
than silently passing every C# edit. Per security-gates.md §0.5
invariant #57: validators that can't decide fail closed, never pass.
"""

from __future__ import annotations

from typing import Any

import tree_sitter_c_sharp as _ts_csharp  # type: ignore
from tree_sitter import Language, Parser  # type: ignore

_LANGUAGE: Any = Language(_ts_csharp.language())
_PARSER: Any = Parser(_LANGUAGE)
_PARSER_NAME = "tree-sitter-c-sharp"

# #811: extensions whose content is NOT C# even though C# lives inside
# it. Razor templates interleave markup and C#; only the Razor compiler
# may split them. Handing a whole .cshtml (or a code block lifted out of
# one) to a C# parser asks the wrong question, and the parser answers it
# correctly by refusing — which is how one `@section X { <link /> }`
# made an entire file uneditable through every governed write path.
RAZOR_EXTS = frozenset({".cshtml", ".razor"})

# Receipt string used when no Razor-aware backend is reachable at all.
# Fail OPEN, deliberately: a validator that cannot parse a language must
# not hold the only write path to that language hostage (#811 step 3).
# This is NOT a #57 fail-open violation — #57 governs a validator that
# COULD decide but failed to; here the validator was never entitled to
# decide in the first place.
_RAZOR_FAIL_OPEN_PARSER = "none (razor validate unavailable — fail-open per #811)"


def _validate_razor_content(content: str, ext: str) -> dict[str, Any]:
    """Validate Razor content — never with a C# parser (#811).

    Routes through the existing extension-hint bridge, the same one
    ``outline_extractors/razor.py`` uses for outlines. The Roslyn side
    already dispatches on ``ext`` (``DaemonLoop.HandleValidate`` /
    ``Program.cs``) and today answers "razor validate not supported;
    outline-only" with ok=True — a real Razor verdict will start
    flowing through here the moment that side learns to give one, with
    no change on this end. That is the reuse: one bridge, one dispatch.

    When no Razor-aware backend answers, FAIL OPEN. Refusing an edit is
    a strong action and must be reserved for a parser that was asked a
    question it can actually answer; tree-sitter-c-sharp cannot parse
    Razor any more than Roslyn can, so falling through to it would only
    reproduce the defect with a different accent.
    """
    try:
        from .csharp_roslyn_client import roslyn_validate

        result = roslyn_validate(content, ext=ext)
        if result is not None:
            return result
    except Exception:
        pass
    return {"ok": True, "error_nodes": [], "parser": _RAZOR_FAIL_OPEN_PARSER}


def _collect_error_nodes(node: Any, errors: list[dict[str, Any]]) -> None:
    """Recursively walk tree and record ERROR / MISSING nodes."""
    if node.type == "ERROR" or getattr(node, "is_missing", False):
        start_row, start_col = node.start_point
        try:
            text = node.text.decode("utf-8", errors="replace") if node.text else ""
        except Exception:
            text = ""
        errors.append(
            {
                "line": start_row + 1,
                "column": start_col,
                "text": text[:200],
            },
        )
    for child in node.children:
        _collect_error_nodes(child, errors)


def validate_csharp_content(content: str, *, ext: str = ".cs") -> dict[str, Any]:
    """Parse ``content`` in the language named by ``ext`` and report errors.

    Doctrine (2026-05-28): prefers the Roslyn-backed validator at
    tools/aidocs-csharp-outliner when dotnet is available. Falls back
    to tree-sitter-c-sharp (this function's tail body) when the Roslyn
    tool isn't installed. Both paths return the same {ok, error_nodes,
    parser} contract; Roslyn additionally enriches each error with a
    ``code`` field (CS-codes) which is purely additive.
    Why: Roslyn IS the language's own parser — its diagnostics match
    `dotnet build` exactly. Tree-sitter's grammar drifts behind the
    language by 1-2 versions and misses subtle errors (uninitialized
    `required` properties, target-typed `new()` misuse, init-only
    assignment violations). Both reject obviously broken syntax; only
    Roslyn agrees with the operator's IDE on the boundary cases.
    Apply: hard-fail invariant from #52 is preserved — if BOTH
    backends are unavailable, raise at import time. The Roslyn path
    is the preferred surface; tree-sitter remains the floor.

    ``ext`` (#811) is the LANGUAGE HINT, and it is the whole point of
    this signature. Before it existed this function took content only,
    so no caller could tell it that a .cshtml is Razor — Razor markup
    went to a C# parser, which refused it correctly for a question it
    was never asked to answer. ``.cshtml``/``.razor`` now route through
    the SAME extension-hint bridge the outline path already uses
    (``roslyn_validate(..., ext=...)`` -> ``DaemonLoop.HandleValidate``)
    instead of growing a second, rival Razor-detection path beside it.
    The default stays ``.cs``, so every pre-existing caller keeps its
    exact prior behaviour and C# validation is unchanged.

    Returns a dict with keys:
      - ``ok``: bool (True if no ERROR nodes)
      - ``error_nodes``: list of {line, column, text[, code]}
      - ``parser``: names WHICH parser judged the content —
        "roslyn-net9" | "tree-sitter-c-sharp" | a Razor receipt
    """
    ext = (ext or ".cs").lower()
    if ext in RAZOR_EXTS:
        return _validate_razor_content(content, ext)

    if content == "":
        return {"ok": True, "error_nodes": [], "parser": _PARSER_NAME}

    # Roslyn preferred path.
    try:
        from .csharp_roslyn_client import roslyn_validate

        result = roslyn_validate(content, ext=ext)
        if result is not None:
            return result
    except Exception:
        # Bridge import or invocation failed — fall through to
        # tree-sitter. Don't suppress the tree-sitter failure mode
        # though; if BOTH are gone, the import at top of this module
        # already raised.
        pass

    source = content.encode("utf-8")
    tree = _PARSER.parse(source)
    errors: list[dict[str, Any]] = []
    _collect_error_nodes(tree.root_node, errors)
    # has_error on the root catches some cases the recursive walk misses
    # (e.g. grammar-level recovery with no dedicated ERROR node).
    has_error = bool(errors) or bool(getattr(tree.root_node, "has_error", False))
    return {
        "ok": not has_error,
        "error_nodes": errors,
        "parser": _PARSER_NAME,
    }
