"""RFC-4 UnitResolver over the AIDOCS code index.

Feeds mempalace's Phase A anchor extractor (``add_drawer`` ->
``extract_anchors``): given a code reference found in drawer/memory content,
map it to a code-index unit so the memory auto-anchors to that unit. The
extractor calls ``resolve_symbol`` / ``resolve_path``; this implementation
backs them with ``code_outlines`` / ``code_files`` and stamps unit ids via
``CodeUnitVendor.compute_unit_id`` (the canonical RFC-4 hash).

Two deliberate properties:

* **Fail-quiet.** Any miss or sqlite error returns ``None`` — anchor
  extraction degrades to "no anchor", never an exception that would roll back
  the drawer write.
* **Suggest-tier by design (§31 memory poisoning).** Symbol confidence is
  capped BELOW the extractor's 0.85 exact threshold, so every auto-extracted
  symbol anchor lands at ``semantic_guess``. Auto-anchors SURFACE on read but
  never BLOCK an edit; only an explicit operator pin (``operator_pinned``)
  blocks. Law enters only through the throne.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .code_units import CodeUnitVendor

# Below mempalace's HIGH_CONFIDENCE_THRESHOLD (0.85): a unique exact-name match
# is strong but still suggest-tier; an ambiguous (multi-hit) match is weaker.
_UNIQUE_SCORE = 0.80
_AMBIGUOUS_SCORE = 0.60


@dataclass(frozen=True)
class ResolvedSymbol:
    unit_id: str
    symbol_name: str
    file_path: str
    confidence_score: float


@dataclass(frozen=True)
class ResolvedPath:
    unit_id: str
    file_path: str


class AidocsUnitResolver:
    """Read-only resolver over a project's code index. Constructed per palace
    context; cheap (opens its own sqlite connection per call)."""

    def __init__(self, *, project_root, index_db_path, project_uuid: str):
        self._index_db = Path(index_db_path)
        self._vendor = CodeUnitVendor(
            project_root=Path(project_root),
            index_db_path=Path(index_db_path),
            project_uuid=project_uuid,
        )

    def resolve_symbol(
        self, symbol: str, *, container: Optional[str] = None
    ) -> Optional[ResolvedSymbol]:
        sym = (symbol or "").strip()
        if not sym:
            return None
        try:
            with sqlite3.connect(str(self._index_db)) as conn:
                rows = []
                if container:
                    rows = conn.execute(
                        "SELECT path, symbol, kind, container FROM code_outlines "
                        "WHERE symbol = ? AND container = ? LIMIT 4",
                        (sym, container),
                    ).fetchall()
                if not rows:
                    rows = conn.execute(
                        "SELECT path, symbol, kind, container FROM code_outlines "
                        "WHERE symbol = ? LIMIT 4",
                        (sym,),
                    ).fetchall()
        except sqlite3.Error:
            return None
        if not rows:
            return None
        path, sym_name, kind, cont = (
            rows[0][0],
            rows[0][1],
            rows[0][2],
            rows[0][3] or "",
        )
        score = _UNIQUE_SCORE if len(rows) == 1 else _AMBIGUOUS_SCORE
        qualified = f"{cont}.{sym_name}" if cont else sym_name
        try:
            unit_id = self._vendor.compute_unit_id(
                source_file=path, kind=kind or "symbol", qualified_symbol=qualified
            )
        except Exception:
            return None
        return ResolvedSymbol(
            unit_id=unit_id,
            symbol_name=sym_name,
            file_path=path,
            confidence_score=score,
        )

    def resolve_path(self, path: str) -> Optional[ResolvedPath]:
        p = (path or "").strip().replace("\\", "/")
        if not p:
            return None
        try:
            with sqlite3.connect(str(self._index_db)) as conn:
                row = conn.execute(
                    "SELECT path FROM code_files WHERE path = ? LIMIT 1", (p,)
                ).fetchone()
                if row is None:
                    # The reference may be a tail of the real path (src/x.py vs
                    # a/b/src/x.py). Accept a unique suffix match only.
                    suffix = conn.execute(
                        "SELECT path FROM code_files WHERE path LIKE ? LIMIT 2",
                        ("%/" + p,),
                    ).fetchall()
                    row = suffix[0] if len(suffix) == 1 else None
        except sqlite3.Error:
            return None
        if row is None:
            return None
        fp = row[0]
        try:
            unit_id = self._vendor.compute_unit_id(
                source_file=fp, kind="file", qualified_symbol=""
            )
        except Exception:
            return None
        return ResolvedPath(unit_id=unit_id, file_path=fp)
