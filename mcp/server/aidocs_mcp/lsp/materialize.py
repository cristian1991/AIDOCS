"""Materialize LSP answers into durable ``code_edges`` (§XXXII).

The guest oracle's law is "keep the answers, not the machine": we ask a
warm language server for the reference sites of a changed file's
top-level symbols, write those cross-file connections into the owned
code index as ``kind='semantic_ref'`` rows (mirroring how ``import``
edges are stored: ``source_path`` = the REFERENCING file, ``target`` =
the changed module's dotted name), and then evict the server. The index
survives; the guest does not (evict-after-materialize).

Everything FAILS OPEN. Disabled config, no installed server, a project
below the materiality threshold, an unmappable changed file, or ANY
exception yields ``DrainReport(noop=True)`` with zero durable writes and
never raises to the caller — the guest oils the selector's joint; it is
never load-bearing (§XXXII, §XXI, §XXVIII).

This module deliberately does NOT import the door (``lsp.__init__``):
the door imports IT (to wire ``lsp_drain_and_evict``), so the query
functions are INJECTED by the door. That keeps the dependency one-way
and the import graph acyclic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .domain import DrainReport, Language, Location, SymbolInfo

# Type aliases for the injected door queries.
SymbolsFn = Callable[[Path, str], "Optional[list[SymbolInfo]]"]
ReferencesFn = Callable[[Path, str, int, int], "Optional[list[Location]]"]
Writer = Callable[[Path, "list[str]", "list[tuple[str, str, str]]"], int]
EvictFn = Callable[[Path], DrainReport]

# The code-index source marker: mcp/server/<dotted...>.py -> dotted name.
# MUST mirror ``select_affected_vps_tests._module_idents`` so a materialized
# ``semantic_ref`` target matches the reverse-dep walk's changed-module
# frontier exactly.
_SOURCE_MARKER = "mcp/server/"


def _dotted_module(rel_path: str) -> str | None:
    """Repo-relative source path -> dotted module name, or None.

    ``mcp/server/aidocs_mcp/sub/foo.py`` -> ``aidocs_mcp.sub.foo``.
    ``__init__.py`` and non-source paths yield None (nothing to target).
    """
    p = str(rel_path).replace("\\", "/")
    if p.startswith(_SOURCE_MARKER) and p.endswith(".py") and not p.endswith("__init__.py"):
        return p[len(_SOURCE_MARKER):-3].replace("/", ".")
    return None


def _to_rel(project_root: Path, abs_path: str) -> str | None:
    """Absolute reference-site path -> repo-relative posix, or None if
    it resolves outside the project (a stdlib / site-packages hit)."""
    try:
        rel = Path(abs_path).resolve().relative_to(Path(project_root).resolve())
    except (ValueError, OSError):
        return None
    return rel.as_posix()


def _default_writer(
    project_root: Path,
    targets: list[str],
    edge_rows: list[tuple[str, str, str]],
) -> int:
    """Lazy-bind the owned code-index write path. Any failure -> 0 writes."""
    try:
        from ..code_index_store import CodeIndexStore

        store = CodeIndexStore()
        return int(
            store._sync.replace_semantic_ref_edges(Path(project_root), targets, edge_rows)
        )
    except Exception:  # noqa: BLE001 — materialization is fail-open
        return 0


def drain_semantic_refs(
    project_root: Path,
    changed_files: list[str],
    *,
    symbols_fn: SymbolsFn,
    references_fn: ReferencesFn,
    writer: Writer | None = None,
    evict_fn: EvictFn | None = None,
) -> DrainReport:
    """Materialize cross-file ``semantic_ref`` edges for ``changed_files``.

    For each changed source file that maps to a dotted module AND for
    which the door surfaces document symbols (i.e. a server is enabled,
    installed, and above materiality), take each TOP-LEVEL symbol, ask
    for its references, and record ``(referencing_file, module, 'semantic_ref')``
    edges. Then replace prior semantic_ref rows for the touched targets
    and (optionally) evict the servers.

    Fail-open: nothing engaged -> ``DrainReport(noop=True)``, zero writes.
    """
    root = Path(project_root)
    if not changed_files:
        return DrainReport(evicted=0, languages=(), noop=True)

    write = writer if writer is not None else _default_writer

    touched_targets: set[str] = set()
    edge_rows: list[tuple[str, str, str]] = []
    languages: set[str] = set()
    engaged = False

    for rel in changed_files:
        rel_posix = str(rel).replace("\\", "/")
        target = _dotted_module(rel_posix)
        if target is None:
            continue
        abs_path = str(root / rel_posix)
        try:
            symbols = symbols_fn(root, abs_path)
        except Exception:  # noqa: BLE001 — fail open per file
            symbols = None
        if symbols is None:
            # Disabled / no server / below materiality / query failure:
            # the owned floor is the right tool here, not a degradation.
            continue

        engaged = True
        lang = Language.from_path(rel_posix)
        if lang is not None:
            languages.add(lang.value)
        # Always in the replace scope once queried — a symbol whose refs
        # vanished must have its stale rows cleared on re-drain.
        touched_targets.add(target)

        for sym in symbols:
            if sym.container is not None:
                continue  # top-level symbols only
            try:
                locs = references_fn(root, abs_path, int(sym.line), 0)
            except Exception:  # noqa: BLE001
                locs = None
            if not locs:
                continue
            for loc in locs:
                src_rel = _to_rel(root, loc.path)
                if src_rel is None or src_rel == rel_posix:
                    continue  # outside project, or a self-reference
                edge_rows.append((src_rel, target, "semantic_ref"))

    if not engaged:
        return DrainReport(evicted=0, languages=(), noop=True)

    edge_rows = list(dict.fromkeys(edge_rows))
    try:
        written = int(write(root, sorted(touched_targets), edge_rows))
    except Exception:  # noqa: BLE001 — a write failure is still fail-open
        written = 0

    evicted = 0
    evicted_langs: tuple[str, ...] = tuple(sorted(languages))
    if evict_fn is not None:
        try:
            report = evict_fn(root)
            evicted = report.evicted
            evicted_langs = report.languages or evicted_langs
        except Exception:  # noqa: BLE001 — eviction is best-effort
            pass

    return DrainReport(
        evicted=evicted,
        languages=evicted_langs,
        noop=False,
        edges_written=written,
        targets=tuple(sorted(touched_targets)),
    )
