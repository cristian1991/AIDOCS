from __future__ import annotations

import importlib
import warnings
from typing import Any


def tree_sitter_support_reason(*module_names: str) -> str | None:
    """Return an import error string when tree-sitter support is unavailable."""
    try:
        import tree_sitter  # noqa: F401

        for module_name in module_names:
            importlib.import_module(module_name)
    except Exception as exc:
        return str(exc)
    return None


def create_tree_sitter_parser(module_name: str, func_name: str) -> Any:
    """Construct a parser from one tree-sitter grammar loader."""
    from tree_sitter import Language, Parser

    mod = importlib.import_module(module_name)
    lang_fn = getattr(mod, func_name)
    parser = Parser()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="int argument support is deprecated",
            category=DeprecationWarning,
        )
        parser.language = Language(lang_fn())
    return parser
