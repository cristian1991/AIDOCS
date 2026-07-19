"""Production ``hub.code_index`` adapter (memory-loop seal, 2026-07-09).

The RFC-4 recall pipeline (``server_recall_tools``) and the dashboard
heartbeat (``runtime_service.dashboard_heartbeat``) both reach for
``hub.code_index`` — an attribute that, until this module, existed ONLY in
tests. In production ``getattr(hub, "code_index", None)`` returned None, so
``ai_recall`` never clustered real code units and the heartbeat's
index-freshness probe silently degraded to None.

``CodeIndexAdapter`` closes that native seam:

* ``find_symbols(query, limit)`` — direct symbol lookup over
  ``code_outlines``, returning RFC-4-shaped dicts with stable ``unit_id``
  values that MATCH ``CodeUnitVendor.compute_unit_id`` (so palace anchors
  keyed by unit_id join correctly).
* ``vendor`` — a per-current-project ``CodeUnitVendor`` (lazy; None when
  mempalace or the index db is unavailable, which callers already treat
  as "not wired").
* every other attribute delegates to the wrapped ``CodeIndexStore``
  (``hub.code``) — so pre-existing ``hub.code_index.code_status(...)``
  call sites become correct instead of silently excepting.

Degrades quietly everywhere: a missing index db / missing mempalace /
sqlite error yields empty results, never an exception into the caller.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

# Mirror of code_units._KIND_MAP so unit ids stay identical to the ones
# CodeUnitVendor stamps WITHOUT importing mempalace at module import time
# (code_units imports mempalace.conjoined_types at top level). Divergence
# guard: test_memory_loop_seal asserts find_symbols unit_ids equal
# vendor.compute_unit_id output when mempalace is installed.
_KIND_MAP: dict[str, str] = {
    "function": "function",
    "fn": "function",
    "method": "method",
    "class": "class",
    "constant": "constant",
    "const": "constant",
    "module": "outline_section",
    "section": "md_section",
    "heading": "md_section",
}


def _unit_kind(raw: str) -> str:
    return _KIND_MAP.get((raw or "").lower(), "outline_section")


def _index_db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _project_uuid(project_root: Path) -> str:
    from .palace_hub_extension import _resolve_project_uuid

    return _resolve_project_uuid(project_root)


def compute_unit_id(
    *,
    project_uuid: str,
    source_file: str,
    kind: str,
    qualified_symbol: str,
) -> str:
    """Same formula as CodeUnitVendor.compute_unit_id (RFC-4 §3.3)."""
    norm = source_file.replace("\\", "/")
    payload = f"{project_uuid}|{norm}|{kind}|{qualified_symbol}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


class CodeIndexAdapter:
    """``hub.code_index`` — RFC-4 code-unit surface over ``hub.code``."""

    def __init__(self, code_store: Any) -> None:
        # Name-mangle-free private slot; __getattr__ delegates the rest.
        object.__setattr__(self, "_code_store", code_store)

    def __getattr__(self, name: str) -> Any:
        # Fires only for attributes NOT defined on the adapter — this is
        # the CodeIndexStore delegation lane (code_status, sync, ...).
        return getattr(object.__getattribute__(self, "_code_store"), name)

    # ------------------------------------------------------------------
    # RFC-4 unit vendor (per current project root, lazy)
    # ------------------------------------------------------------------

    @property
    def vendor(self) -> Any | None:
        """Per-call CodeUnitVendor for the CURRENT project root, or None
        when mempalace / the index db is unavailable. Callers already
        treat a missing vendor as "phase-2 wiring absent" and degrade.
        """
        try:
            from .mcp_server_runtime_helpers import resolve_project_root

            root = resolve_project_root()
            db = _index_db_path(root)
            if not db.is_file():
                return None
            from .code_units import CodeUnitVendor

            return CodeUnitVendor(
                project_root=root,
                index_db_path=db,
                project_uuid=_project_uuid(root),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # find_symbols — the native seam ai_recall prefers (path 1)
    # ------------------------------------------------------------------

    def find_symbols(
        self,
        *,
        query: str,
        limit: int = 5,
        project_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Symbol lookup over code_outlines → RFC-4 ClusterCodeRef dicts.

        Match order: exact symbol name, then prefix, then substring —
        first bucket that yields rows wins (precision over recall; the
        recall pipeline widens with intent-derived candidates itself).
        unit_id matches CodeUnitVendor.compute_unit_id so anchors join.
        """
        q = (query or "").strip()
        if not q or limit <= 0:
            return []
        try:
            if project_root is None:
                from .mcp_server_runtime_helpers import resolve_project_root

                project_root = resolve_project_root()
            db = _index_db_path(project_root)
            if not db.is_file():
                return []
            uuid = _project_uuid(project_root)
            out: list[dict[str, Any]] = []
            with sqlite3.connect(str(db)) as conn:
                base = (
                    "SELECT co.path, co.symbol, co.kind, co.container, "
                    "co.line_number, "
                    "LEAD(co.line_number) OVER "
                    "  (PARTITION BY co.path ORDER BY co.line_number) AS next_line, "
                    "COALESCE(cf.line_count, 0) "
                    "FROM code_outlines co "
                    "LEFT JOIN code_files cf ON cf.path = co.path "
                )
                like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                for where, params in (
                    ("WHERE co.symbol = ?", (q,)),
                    ("WHERE co.symbol LIKE ? ESCAPE '\\'", (like + "%",)),
                    ("WHERE co.symbol LIKE ? ESCAPE '\\'", ("%" + like + "%",)),
                ):
                    rows = conn.execute(
                        base + where + " ORDER BY co.path, co.line_number LIMIT ?",
                        (*params, int(limit)),
                    ).fetchall()
                    if rows:
                        break
                else:
                    rows = []
                for path, symbol, raw_kind, container, line_start, next_line, line_count in rows:
                    container = container or ""
                    qualified = f"{container}.{symbol}" if container else symbol
                    kind = _unit_kind(raw_kind or "")
                    line_start = int(line_start or 0)
                    if next_line is not None:
                        line_end = max(int(next_line) - 1, line_start)
                    else:
                        line_end = max(int(line_count or 0), line_start)
                    out.append(
                        {
                            "unit_id": compute_unit_id(
                                project_uuid=uuid,
                                source_file=str(path),
                                kind=kind,
                                qualified_symbol=qualified,
                            ),
                            "kind": kind,
                            "symbol": qualified,
                            "source_file": str(path),
                            "line_start": line_start,
                            "line_end": line_end,
                            "snippet": "",
                        },
                    )
            return out[:limit]
        except Exception:
            return []
