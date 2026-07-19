"""Edit-gate memory surfacing.

Before any edit tool mutates a file, this gate queries memory_symbol_anchors
for memories anchored to the target symbol OR file. If anchored memory
exists and the caller has not acked all the route_ids, the edit is
refused with the memory titles and the ack_memory_ids list to retry with.

Doctrine moves out of free-text code comments into anchored memory
entries (memory_capture with anchor_symbols=[{symbol, file, kind}]).
Anchored memories surface on edit — not on read, not on prompt — which
is the deterministic moment an agent is about to change behavior.

Doctrine cannot be silently ignored: the agent must include the route_id
of every anchored memory in the ack list before the edit proceeds.
That is the receipt that the doctrine was acknowledged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AnchoredMemory:
    route_id: int
    target_path: str
    title: str
    severity: str
    anchor_symbol: str
    anchor_file: str


@dataclass
class GateResult:
    allowed: bool
    refusal: dict | None = None
    surfaced_memories: tuple[AnchoredMemory, ...] = field(default_factory=tuple)


def query_anchored_memories(
    project_root: Path,
    *,
    file_path: str = "",
    symbol_name: str = "",
) -> list[AnchoredMemory]:
    """Return memories anchored to the symbol and/or file.

    Anchor row shapes:
      - anchor_kind='symbol', symbol_name=X, file_path=''     — fires on
        any edit to symbol X regardless of file.
      - anchor_kind='symbol', symbol_name=X, file_path=P      — fires
        only when symbol X is edited in file P.
      - anchor_kind='file',   symbol_name='', file_path=P     — fires
        on any edit to file P regardless of symbol target.

    Query semantics (avoid false positives across same-named symbols
    in different files):
      - sym AND fp given (mode='symbol' edit with known file):
          symbol-anchored row matches if name=sym AND (anchor_file='' OR anchor_file=fp).
          file-anchored row matches if file_path=fp.
      - only fp given (non-symbol edit on a file):
          all rows with file_path=fp (both kinds — we don't know which
          symbols the edit touches, so any in-file doctrine surfaces).
      - only sym given (unusual; symbol-mode without resolved file):
          all symbol-anchored rows with name=sym.

    Sovereign-severity routes filtered at SQL.
    """
    fp = (file_path or "").replace("\\", "/").strip()
    sym = (symbol_name or "").strip()
    if not fp and not sym:
        return []
    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return []

    params: list[str]
    if sym and fp:
        where = (
            "( (msa.anchor_kind = 'symbol' "
            "    AND msa.symbol_name = ? "
            "    AND (msa.file_path = '' OR msa.file_path = ?)) "
            "  OR (msa.anchor_kind = 'file' "
            "      AND msa.file_path = ?) "
            ")"
        )
        params = [sym, fp, fp]
    elif fp:
        where = "msa.file_path = ?"
        params = [fp]
    else:
        where = "msa.anchor_kind = 'symbol' AND msa.symbol_name = ?"
        params = [sym]

    out: list[AnchoredMemory] = []
    seen: set[int] = set()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Memory-loop seal (2026-07-09): semantic_guess anchors (the
            # capture analyzer's auto-derived tier) are ADVISORY — they
            # surface via the read/edit goggles but must never gate an
            # edit. Only when the RFC-4 confidence column exists; legacy
            # DBs keep legacy (all-blocking) semantics unchanged.
            confidence_filter = ""
            try:
                cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(memory_symbol_anchors)",
                    ).fetchall()
                }
                if "confidence" in cols:
                    confidence_filter = (
                        "AND COALESCE(msa.confidence, 'operator_pinned') "
                        "!= 'semantic_guess' "
                    )
            except sqlite3.Error:
                confidence_filter = ""
            sql = (
                "SELECT mr.route_id, mr.target_path, mr.severity, "
                "msa.symbol_name AS anchor_symbol, "
                "msa.file_path AS anchor_file, "
                "COALESCE(mf.title, '') AS title "
                "FROM memory_symbol_anchors msa "
                "JOIN memory_routes mr ON mr.route_id = msa.route_id "
                # SQLite-only doctrine (2026-06): INNER JOIN canonical active
                # memory_index — a route-only GHOST (no active canonical row) must
                # NOT block an edit. Title also comes from the canonical row.
                "JOIN memory_index mf ON mf.path = mr.target_path "
                "  AND COALESCE(mf.status,'active')='active' "
                "  AND COALESCE(mf.superseded_by,'')='' "
                f"WHERE ({where}) AND mr.severity != 'sovereign' "
                f"{confidence_filter}"
                "ORDER BY "
                "  CASE mr.severity WHEN 'critical' THEN 0 "
                "                   WHEN 'high' THEN 1 ELSE 2 END, "
                "  mr.route_id"
            )
            for row in conn.execute(sql, params).fetchall():
                rid = int(row["route_id"])
                if rid in seen:
                    continue
                seen.add(rid)
                title = str(row["title"]) or str(row["target_path"]).rsplit("/", 1)[
                    -1
                ].removesuffix(".md")
                out.append(
                    AnchoredMemory(
                        route_id=rid,
                        target_path=str(row["target_path"]),
                        title=title,
                        severity=str(row["severity"]),
                        anchor_symbol=str(row["anchor_symbol"] or ""),
                        anchor_file=str(row["anchor_file"] or ""),
                    ),
                )
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return out


def check_edit_memory_gate(
    project_root: Path,
    *,
    file_path: str,
    symbol_name: str = "",
    ack_memory_ids: list[int] | None = None,
    ack_drawer_ids: list[str] | None = None,
    tool_name: str = "edit",
    hub: Any = None,
) -> GateResult:
    """Decide whether an edit may proceed against anchored doctrine.

    Returns GateResult(allowed=True) if no anchored memory exists OR
    if every anchored route_id appears in ack_memory_ids.

    Returns GateResult(allowed=False, refusal=...) otherwise. The
    refusal payload is shaped to mirror the read-before-edit gate:
    `ok=False, error=<formatted message>, blocked_by='anchored_memory'`.

    RFC-4 Phase B: when ``hub`` is provided AND ``hub.palace`` is wired,
    the gate ALSO consults palace anchors via ``check_edit_with_palace``.
    Palace blockers (exact_symbol / operator_pinned tiers) compose with
    memory_store blockers — both must be acked for the edit to proceed.
    See ``edit_memory_gate_palace.py``.
    """
    memories = query_anchored_memories(
        project_root,
        file_path=file_path,
        symbol_name=symbol_name,
    )

    # RFC-4 Phase B — consult palace anchors when hub.palace is wired.
    palace_result = None
    if hub is not None and getattr(hub, "palace", None) is not None:
        try:
            from .edit_memory_gate_palace import check_edit_with_palace

            anchor_store = getattr(getattr(hub, "memory", None), "anchors", None)
            if anchor_store is not None:
                palace_result = check_edit_with_palace(
                    file_path=file_path,
                    symbol_name=symbol_name,
                    ack_memory_ids=ack_drawer_ids,
                    palace_anchor_store=anchor_store,
                    palace_service=hub.palace,
                    hub_ctx=None,  # gate consults palace anchors directly
                    file_anchor_strict=False,
                )
        except Exception:
            # Palace consult failure must not block AIDOCS gate — fail
            # open on the palace side; memory_store side still enforces.
            palace_result = None

    # Combine memory_store blockers + palace blockers
    if not memories and (palace_result is None or palace_result.allowed):
        return GateResult(allowed=True)

    acked: set[int] = {int(x) for x in (ack_memory_ids or [])}
    unacked = [m for m in memories if m.route_id not in acked]

    # If memory_store side allows but palace side refuses, build a
    # combined refusal that surfaces palace-side ack_memory_ids needed.
    if not unacked and palace_result is not None and not palace_result.allowed:
        target_label = symbol_name or file_path or "<edit target>"
        needed = list(palace_result.ack_memory_ids_needed)
        lines = [
            f"REFUSED: {len(needed)} palace-anchored memor{'y' if len(needed) == 1 else 'ies'} "
            f"for `{target_label}` (RFC-4 confidence tier exact_symbol/operator_pinned).",
        ]
        for d in palace_result.blocking_drawers:
            lines.append(f"  [{d.drawer_id}] {d.snippet[:80]}")
        lines.append(f"Re-call {tool_name} with ack_drawer_ids={needed} to proceed.")
        return GateResult(
            allowed=False,
            refusal={
                "ok": False,
                "error": "\n".join(lines),
                "blocked_by": "palace_anchored_memory",
                "ack_drawer_ids_needed": needed,
                "tool": tool_name,
            },
            surfaced_memories=tuple(memories),
        )

    if not unacked:
        return GateResult(
            allowed=True,
            surfaced_memories=tuple(memories),
        )

    target_label = symbol_name or file_path or "<edit target>"
    n = len(unacked)
    lines: list[str] = [
        f"REFUSED: {n} anchored memor{'y' if n == 1 else 'ies'} for `{target_label}`.",
    ]
    for m in unacked:
        lines.append(f"  [#{m.route_id}] {m.title} ({m.target_path})")
    all_ids = sorted({m.route_id for m in memories})
    lines.append(f"Re-call {tool_name} with ack_memory_ids={all_ids} to proceed.")
    paths = sorted({m.target_path for m in unacked})
    lines.append(f"To read the doctrine first: memory_read(targets={paths!r}).")
    msg = "\n".join(lines)

    return GateResult(
        allowed=False,
        refusal={
            "ok": False,
            "error": msg,
            "blocked_by": "anchored_memory",
            "unacked_memory_ids": [m.route_id for m in unacked],
            "memory_paths": paths,
            "tool": tool_name,
        },
        surfaced_memories=tuple(memories),
    )
