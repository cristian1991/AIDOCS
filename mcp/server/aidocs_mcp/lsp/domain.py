"""aidocs_lsp domain — frozen value objects, no I/O (§XXXII).

The door speaks in these; nothing here touches a filesystem, a
subprocess, or the network. Language maps deterministically from a
file extension so callers never hand-wire language detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Language(Enum):
    """The vendored-server languages (§XXXII: never the license-murky ones)."""

    PY = "py"
    TS = "ts"
    CS = "cs"
    RS = "rs"

    @property
    def extensions(self) -> tuple[str, ...]:
        return _LANG_EXTENSIONS[self]

    @classmethod
    def from_path(cls, path: str) -> "Language | None":
        """Map a file path to its Language by extension, or None.

        Deterministic and total: an unknown extension yields None, never
        a guess. Multi-dot names (``a.tar.gz``) match on the LAST suffix
        only.
        """
        lower = str(path).lower()
        dot = lower.rfind(".")
        if dot < 0:
            return None
        ext = lower[dot:]
        return _EXTENSION_INDEX.get(ext)


_LANG_EXTENSIONS: dict[Language, tuple[str, ...]] = {
    Language.PY: (".py", ".pyi"),
    Language.TS: (".ts", ".tsx"),
    Language.CS: (".cs",),
    Language.RS: (".rs",),
}

_EXTENSION_INDEX: dict[str, Language] = {
    ext: lang for lang, exts in _LANG_EXTENSIONS.items() for ext in exts
}


@dataclass(frozen=True)
class SymbolInfo:
    """A declaration surfaced by ``textDocument/documentSymbol``."""

    name: str
    kind: str
    path: str
    line: int
    line_end: int
    container: str | None


@dataclass(frozen=True)
class Location:
    """A source position (``textDocument/references`` result element)."""

    path: str
    line: int
    char: int


@dataclass(frozen=True)
class Diagnostic:
    """A compiler verdict (``textDocument/publishDiagnostics`` element)."""

    path: str
    line: int
    severity: str
    message: str
    code: str | None


@dataclass(frozen=True)
class MaterialityVerdict:
    """The spend-gate outcome for a (project × language) pair."""

    material: bool
    loc: int
    threshold: int
    reason: str


@dataclass(frozen=True)
class ServerSpec:
    """How to find and launch a language server (no process here)."""

    language: Language
    binary_candidates: tuple[str, ...]
    args: tuple[str, ...]
    initialization_options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DrainReport:
    """The result of draining+evicting a project's warm servers.

    Slice 2 adds the materialize outcome fields (all defaulted so the
    Slice-1 ``DrainReport(evicted=, languages=)`` construction stays
    valid): ``noop`` is True when the guest never engaged (disabled,
    no server, or below materiality) and nothing durable was written;
    ``edges_written`` counts the ``semantic_ref`` rows materialized;
    ``targets`` is the dotted module names whose semantic refs were
    refreshed (delete-then-reinsert scope).
    """

    evicted: int
    languages: tuple[str, ...]
    noop: bool = False
    edges_written: int = 0
    targets: tuple[str, ...] = ()
