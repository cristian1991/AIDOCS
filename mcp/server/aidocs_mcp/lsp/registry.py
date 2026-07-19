"""ServerSpec registry + discovery (§XXXII: vendor the servers).

pyright is the live spec (Python — the door's Slice-1 subject). csharp-ls
and rust-analyzer are STUBS: spec-only, no probe requirement this slice
(integration lands later). Discovery is ``shutil.which`` over the
candidate binary names — absent => None, never an error.
"""

from __future__ import annotations

import shutil

from .domain import Language, ServerSpec

# One spec per language. binary_candidates are tried left-to-right;
# the first on PATH wins. Vendored, permissive-licensed servers only.
_SPECS: dict[Language, ServerSpec] = {
    Language.PY: ServerSpec(
        language=Language.PY,
        # basedpyright (pip) first — it's the gate's own type lane server,
        # so the door reuses one install (§XXXII loot). Node pyright next.
        binary_candidates=("basedpyright-langserver", "pyright-langserver"),
        args=("--stdio",),
        initialization_options={},
    ),
    # ── STUBS (spec-only this slice) ──────────────────────────────
    Language.CS: ServerSpec(
        language=Language.CS,
        binary_candidates=("csharp-ls",),
        args=(),
        initialization_options={},
    ),
    Language.RS: ServerSpec(
        language=Language.RS,
        binary_candidates=("rust-analyzer",),
        args=(),
        initialization_options={},
    ),
    Language.TS: ServerSpec(
        language=Language.TS,
        binary_candidates=("typescript-language-server",),
        args=("--stdio",),
        initialization_options={},
    ),
}


def resolve_server(language: Language) -> tuple[str, ServerSpec] | None:
    """Locate an installed server for ``language``.

    Returns ``(binary_path, spec)`` for the first candidate found on
    PATH, or None when no server binary is installed (fail-open: the
    door treats None as 'not available', never an error).
    """
    spec = _SPECS.get(language)
    if spec is None:
        return None
    for candidate in spec.binary_candidates:
        found = shutil.which(candidate)
        if found:
            return (found, spec)
    return None
