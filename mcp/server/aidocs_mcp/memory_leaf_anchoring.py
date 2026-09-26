"""#375 Phase 3 (B) — smallest-leaf anchor resolution over the OWNED index.

EMPEROR RULING (2026-07-19): every memory is connected to the code index
at the smallest resolvable leaf — file, then function/symbol — resolved
at capture time from the target_hint + content mentions through the
OWNED stores (``code_files`` / ``code_outlines``), the #448 Consumer-B
machinery's precedent. No LSP guest, no chroma, pure sqlite reads —
bounded and fail-quiet, so capture latency is untouched.

Resolution ladder (smallest leaf wins):
  identifier mention → code_outlines row → function/method/class/symbol
  path mention       → code_files row    → file
  unresolvable identifier with a resolvable file context → file
  nothing resolves → no anchor (the memory stays route/keyword-discoverable)

All anchors produced here are ``semantic_guess`` tier (advisory — they
surface on read/edit goggles but NEVER block an edit; §31: auto-derived
signals must not become law) with ``source='leaf_resolver'`` and a
``leaf_granularity`` marker carried by the extended
``memory_symbol_anchors`` schema (anchor_schema_migration).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ._sqlite_connect import connect as _canonical_connect

_MAX_LEAF_ANCHORS = 5
_MAX_IDENT_LOOKUPS = 16
_MAX_PATH_LOOKUPS = 8

# code_outlines.kind → leaf_granularity ladder. Anything named but not in
# the map is still a leaf ('symbol'); containers map to their own rung.
_KIND_TO_GRANULARITY = {
    "function": "function",
    "method": "method",
    "class": "class",
    "interface": "class",
    "struct": "class",
    "record": "class",
    "enum": "class",
    "module": "module",
    "namespace": "module",
    "component": "symbol",
    "field": "symbol",
    "property": "symbol",
    "const": "symbol",
    "constant": "symbol",
    "variable": "symbol",
}


@dataclass(frozen=True)
class LeafAnchor:
    symbol: str  # '' for file-granularity anchors
    file: str
    anchor_kind: str  # 'symbol' | 'file' (stays inside the legacy CHECK set)
    granularity: str  # file | module | family | class | function | method | symbol
    line_number: int = 0


def _connect_ro(project_root: Path) -> sqlite3.Connection | None:
    db = Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db.is_file():
        return None
    try:
        # read_only=True is the canonical connect's `file:...?mode=ro` (#755);
        # row_factory=sqlite3.Row is its default.
        return _canonical_connect(db, read_only=True)
    except sqlite3.Error:
        return None


def _granularity_for(kind: str) -> str:
    return _KIND_TO_GRANULARITY.get((kind or "").strip().lower(), "symbol")


def resolve_leaf_anchors(
    project_root: Path,
    *,
    content: str,
    target_hint: str = "",
    cap: int = _MAX_LEAF_ANCHORS,
) -> list[LeafAnchor]:
    """Resolve the smallest code-index leaves the memory speaks about.

    Fail-quiet: any index absence / sqlite error returns what resolved so
    far (possibly []). Never raises, never imports chroma — safe on the
    capture path.
    """
    text = f"{target_hint or ''}\n{content or ''}"
    conn = _connect_ro(Path(project_root))
    if conn is None:
        return []

    anchors: list[LeafAnchor] = []
    symbol_files: set[str] = set()  # files already covered by a finer leaf
    seen_keys: set[tuple[str, str]] = set()

    def _add(anchor: LeafAnchor) -> bool:
        key = (anchor.symbol, anchor.file)
        if key in seen_keys:
            return len(anchors) >= cap
        seen_keys.add(key)
        anchors.append(anchor)
        return len(anchors) >= cap

    try:
        # 1) identifier mentions → the smallest leaves (function/method/
        #    class/symbol). Reuses the capture analyzer's code-token shape.
        from .memory_capture_analyzer import _code_tokens

        for tok in _code_tokens(text)[:_MAX_IDENT_LOOKUPS]:
            if len(anchors) >= cap:
                break
            sym = tok.rsplit(".", 1)[-1]
            try:
                row = conn.execute(
                    "SELECT symbol, path, kind, line_number FROM code_outlines "
                    "WHERE symbol = ? LIMIT 1",
                    (sym,),
                ).fetchone()
            except sqlite3.Error:
                break
            if row is None:
                continue
            fp = str(row["path"]).replace("\\", "/")
            symbol_files.add(fp)
            if _add(
                LeafAnchor(
                    symbol=str(row["symbol"]),
                    file=fp,
                    anchor_kind="symbol",
                    granularity=_granularity_for(str(row["kind"])),
                    line_number=int(row["line_number"] or 0),
                )
            ):
                break

        # 2) path mentions → file leaves, ONLY where no finer leaf already
        #    covers that file (smallest leaf wins). Unresolvable-to-symbol
        #    content falls back here — the pinned fallback granularity.
        if len(anchors) < cap:
            from .semantic_enrichment import extract_path_tokens

            for tok in extract_path_tokens(text, cap=_MAX_PATH_LOOKUPS):
                if len(anchors) >= cap:
                    break
                norm = tok.replace("\\", "/").strip("/")
                try:
                    row = conn.execute(
                        "SELECT path FROM code_files "
                        "WHERE path = ? OR path LIKE ? LIMIT 1",
                        (norm, f"%/{norm}"),
                    ).fetchone()
                except sqlite3.Error:
                    break
                if row is None:
                    continue
                fp = str(row["path"]).replace("\\", "/")
                if fp in symbol_files:
                    continue  # a finer leaf already anchors this file
                if _add(
                    LeafAnchor(
                        symbol="",
                        file=fp,
                        anchor_kind="file",
                        granularity="file",
                    )
                ):
                    break
    except Exception:
        # Fail-quiet by contract — return whatever resolved before the
        # fault; anchoring is enrichment, never a capture blocker.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return anchors


def register_leaf_anchors(
    hub_index,
    project_root: Path,
    *,
    route_id: int,
    content: str,
    target_hint: str = "",
    skip_keys: set[tuple[str, str]] | None = None,
) -> int:
    """Resolve + upsert leaf anchors for a fresh capture. Returns the
    number registered. Best-effort per anchor; a failed upsert is silence
    (the route/keyword rail still finds the memory)."""
    skip = skip_keys or set()
    recorded = 0
    for leaf in resolve_leaf_anchors(
        project_root, content=content, target_hint=target_hint
    ):
        if (leaf.symbol, leaf.file) in skip:
            continue
        try:
            hub_index.upsert_memory_anchor(
                project_root,
                route_id=route_id,
                symbol_name=leaf.symbol,
                file_path=leaf.file,
                anchor_kind=leaf.anchor_kind,
                confidence="semantic_guess",
                source="leaf_resolver",
                leaf_granularity=leaf.granularity,
            )
            recorded += 1
        except Exception:
            continue
    return recorded
