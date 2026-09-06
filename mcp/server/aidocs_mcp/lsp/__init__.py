"""aidocs_lsp — the DOOR (§XXXII "LSP joints law").

ONE door, vendor the servers, build only the door. This module is the
ONLY public surface. Every function FAILS OPEN: no server, below the
materiality threshold, disabled config, or ANY error yields None/False/
empty — never a raise to the caller, never a block. The judge and gates
must never depend on this (advisory enrichment, not law).

Lifecycle is evict-after-materialize: servers are transient. This slice
exposes warm→query→drain→evict; it materializes NOTHING durable yet
(the code_edges writer is a later slice) — drain_and_evict just stops
servers.

Config (env):
  AIDOCS_LSP_ENABLED           default "0" (OFF this slice) — door returns
                               None instantly when not "1"/"true".
  AIDOCS_LSP_THRESHOLD_LOC     default 5000 — materiality LOC threshold.
  AIDOCS_LSP_MEMORY_BUDGET_MB  default 2048 — refuse to spawn below the
                               per-server RAM floor.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import client, materialize, materiality, registry
from .domain import Diagnostic, DrainReport, Language, Location, MaterialityVerdict, SymbolInfo

__all__ = [
    "Diagnostic",
    "DrainReport",
    "Language",
    "Location",
    "MaterialityVerdict",
    "SymbolInfo",
    "lsp_available",
    "lsp_document_symbols",
    "lsp_references",
    "lsp_diagnostics",
    "lsp_drain_and_evict",
]

# A language server's fixed RAM floor (§XXXII). Below the configured
# budget we refuse to spawn — the tree-sitter floor is the right tool.
_SERVER_RAM_FLOOR_MB = 400


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    return _truthy(os.environ.get("AIDOCS_LSP_ENABLED", "0"))


def _threshold() -> int:
    try:
        return int(os.environ.get("AIDOCS_LSP_THRESHOLD_LOC", "5000"))
    except ValueError:
        return 5000


def _budget_mb() -> int:
    try:
        return int(os.environ.get("AIDOCS_LSP_MEMORY_BUDGET_MB", "2048"))
    except ValueError:
        return 2048


def _coerce_language(language: "Language | str | None") -> Language | None:
    if isinstance(language, Language):
        return language
    if isinstance(language, str):
        try:
            return Language(language)
        except ValueError:
            return None
    return None


def _gate(project_root: Path, language: Language) -> bool:
    """The spawn precondition: enabled AND budget AND binary AND material.

    Pure decision — never spawns. Any failure returns False.
    """
    if not _enabled():
        return False
    if _budget_mb() < _SERVER_RAM_FLOOR_MB:
        return False
    try:
        if registry.resolve_server(language) is None:
            return False
        v = materiality.verdict(Path(project_root), language, _threshold())
    except Exception:  # noqa: BLE001 — fail open
        return False
    return bool(v.material)


def lsp_available(project_root: Path, language: "Language | str") -> bool:
    """True iff a server is installed AND materiality passes (no spawn)."""
    lang = _coerce_language(language)
    if lang is None:
        return False
    try:
        return _gate(Path(project_root), lang)
    except Exception:  # noqa: BLE001
        return False


def _warm_for_file(project_root: Path, file_path: str):
    lang = Language.from_path(file_path)
    if lang is None:
        return None
    if not _gate(Path(project_root), lang):
        return None
    try:
        return client.warm(Path(project_root), lang)
    except Exception:  # noqa: BLE001
        return None


def lsp_document_symbols(project_root: Path, file_path: str) -> list[SymbolInfo] | None:
    server = _warm_for_file(project_root, file_path)
    if server is None:
        return None
    try:
        return server.document_symbols(file_path)
    except Exception:  # noqa: BLE001 — fail open
        return None


def lsp_references(
    project_root: Path, file_path: str, line: int, char: int
) -> list[Location] | None:
    server = _warm_for_file(project_root, file_path)
    if server is None:
        return None
    try:
        return server.references(file_path, line, char)
    except Exception:  # noqa: BLE001
        return None


def lsp_diagnostics(
    project_root: Path, file_path: str, content: str
) -> list[Diagnostic] | None:
    server = _warm_for_file(project_root, file_path)
    if server is None:
        return None
    try:
        return server.diagnostics(file_path, content)
    except Exception:  # noqa: BLE001
        return None


def lsp_drain_and_evict(
    project_root: Path,
    changed_files: "list[str] | None" = None,
    *,
    materialize_refs: bool = False,
) -> DrainReport:
    """Stop every warm server for the project. Always safe to call.

    Slice 1 behavior (default, and the 29 fail-open tests' contract):
    just drains+evicts, materializing nothing durable.

    Slice 2 (§XXXII "materialize the answers, evict the machine"): when
    ``materialize_refs`` is requested AND ``changed_files`` is supplied,
    materialize cross-file ``semantic_ref`` edges for those files FIRST
    (via the guest servers), then evict. Fail-open throughout — any
    failure falls back to the plain drain and never raises.
    """
    if materialize_refs and changed_files:
        try:
            return materialize.drain_semantic_refs(
                Path(project_root),
                list(changed_files),
                symbols_fn=lsp_document_symbols,
                references_fn=lsp_references,
                evict_fn=lambda root: client.evict(root),
            )
        except Exception:  # noqa: BLE001 — fall through to a plain drain
            pass
    try:
        return client.evict(Path(project_root))
    except Exception:  # noqa: BLE001 — drain must never raise
        return DrainReport(evicted=0, languages=())
