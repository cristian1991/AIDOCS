"""Deprecated 2026-04-23.

NLP stack (lingua + simplemma + rapidfuzz) now ships as base
dependencies in pyproject.toml. No runtime install path needed.
Module kept as an import-shim so stale callers raise a clear error.
"""


def _removed(*_a, **_k):  # pragma: no cover
    raise RuntimeError("aidocs_mcp.nlp_install was removed — NLP deps are shipped by default")


install_nlp_extras = _removed
