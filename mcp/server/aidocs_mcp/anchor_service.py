"""The ONE service that changes memory->symbol anchors (#1187).

Design decision (2026-10-18): the operator and the dashboard are equivalent --
whatever the dashboard can do, the operator can also do through an agent on
their own word. Both
doors call THIS module; nothing else changes an anchor's tier on purpose.

  * list     -- anchors of a target (symbol name, file path, or anchor id)
  * preview  -- what an op WOULD change, with a fingerprint of the current
                state; grants nothing, spends nothing
  * apply    -- pin / unpin / downgrade / delete, only if the state still
                matches the previewed fingerprint (compare-and-swap)

An op on a symbol or file acts on ALL its anchors, across memories (operator
D2a), and lists them. Rules (r0b2 83612553-380):

  * RBAC is checked HERE, for every door: ``memory.manage_anchors``.
  * A direct downgrade to any lower tier is allowed (operator_pinned straight
    to semantic_guess included).
  * Deleting or unpinning leaves a TOMBSTONE, so automatic writers (capture,
    the leaf resolver) cannot silently re-create the row or raise it back
    above the operator's tier. An explicit operator ``pin`` clears it.
  * Every change and every refusal is a ``control_plane_mutation`` audit row
    carrying the before / after state. An apply is audited in three phases
    (outer_gate_audit): intent before the change (unrecordable = refused),
    result after (unrecordable = ``audit_degraded``, never a clean success).

Tier ladder, strongest first: operator_pinned > exact_symbol > file_anchor >
semantic_guess. The first two BLOCK edits until acknowledged.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

TIERS: tuple[str, ...] = ("operator_pinned", "exact_symbol", "file_anchor", "semantic_guess")
OPS: frozenset[str] = frozenset({"pin", "unpin", "downgrade", "delete"})
SERVICE_SOURCE = "anchor_service"
TOMBSTONE_TABLE = "memory_anchor_tombstones"

_TOMBSTONE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TOMBSTONE_TABLE} (
    route_id    INTEGER NOT NULL,
    symbol_name TEXT NOT NULL,
    file_path   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL CHECK (kind IN ('deleted', 'ceiling')),
    ceiling     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    created_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (route_id, symbol_name, file_path)
)
"""

#: (allowed, reason) for the caller about to change anchors.
Authorize = Callable[[], tuple[bool, str]]


def tier_rank(tier: str) -> int:
    """0 = strongest. Unknown tiers rank weakest."""
    try:
        return TIERS.index(str(tier or "").strip().lower())
    except ValueError:
        return len(TIERS)


def _store(project_root: Path):
    from .index_store import IndexStore
    from .session_store import SessionStore

    return IndexStore(SessionStore(project_root))


def ensure_tombstones(conn) -> None:
    conn.execute(_TOMBSTONE_SCHEMA)


def _project_relative(project_root: Path | None, text: str) -> str:
    """A file target as anchors store it: project-relative, forward slashes, no './'.

    An absolute path is matched only when it lies INSIDE this project (after
    resolving symlinks / junctions); anything outside names nothing here, so a
    path in another project or worktree never reaches this project's anchors.
    """
    path = text.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    candidate = Path(text)
    if candidate.is_absolute() and project_root is not None:
        try:
            return candidate.resolve().relative_to(Path(project_root).resolve()).as_posix()
        except (OSError, ValueError):
            return ""  # outside this project: matches no anchor
    return path


def _select(conn, target: str, project_root: Path | None = None) -> list[dict[str, Any]]:
    """Every anchor the target names, EXACTLY: an all-digit target is an anchor id;
    otherwise a symbol name equal to it, or a file path equal to it (project-
    relative). No prefix, descendant, alias or substring match: 'OrderService'
    does not name 'OrderService.place_order', 'src/orders' does not name
    'src/orders/service.py'."""
    text = str(target or "").strip()
    if not text:
        return []
    path = _project_relative(project_root, text)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_symbol_anchors)")}
    conf = "a.confidence" if "confidence" in cols else "'operator_pinned'"
    query = (
        "SELECT a.anchor_id, a.route_id, a.symbol_name, a.file_path, a.anchor_kind, "
        f"{conf} AS confidence, r.target_path AS memory_path "
        "FROM memory_symbol_anchors a LEFT JOIN memory_routes r ON r.route_id = a.route_id "
    )
    if text.isdigit():
        found = conn.execute(query + "WHERE a.anchor_id = ?", (int(text),)).fetchall()
    else:
        found = conn.execute(
            query + "WHERE a.symbol_name = ? OR a.file_path = ? ORDER BY a.anchor_id",
            (text, path),
        ).fetchall()
    return [dict(r) for r in found]


def _rows(project_root: Path, target: str) -> list[dict[str, Any]]:
    store = _store(project_root)
    store.init_db(project_root)
    with store.connect(project_root) as conn:
        return _select(conn, target, project_root)


#: Every field of a selected row that IDENTIFIES the binding a confirmation is
#: about (r0b2 adef865a-203): which memory (route + its path), which code
#: (symbol, file, kind) and its tier. Re-pointing an anchor at another memory
#: or symbol under the same anchor_id must invalidate an earlier preview.
FINGERPRINT_FIELDS = (
    "anchor_id",
    "route_id",
    "memory_path",
    "symbol_name",
    "file_path",
    "anchor_kind",
    "confidence",
)


def fingerprint(rows: list[dict[str, Any]]) -> str:
    state = sorted(
        [str(r.get(field) if r.get(field) is not None else "") for field in FINGERPRINT_FIELDS]
        for r in rows
    )
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()[:16]


def list_anchors(project_root: Path, target: str) -> dict[str, Any]:
    rows = _rows(project_root, target)
    return {"ok": True, "target": target, "anchors": rows, "fingerprint": fingerprint(rows)}


def _new_tier(op: str, current: str, to: str) -> str | None:
    """The tier ``op`` leaves an anchor at; None = the op does not apply."""
    if op == "pin":
        return "operator_pinned"
    if op == "unpin":
        if current != "operator_pinned":
            return None
        to = to or "exact_symbol"
    if op in ("unpin", "downgrade"):
        if to not in TIERS or tier_rank(to) <= tier_rank(current):
            return None  # not a downgrade
        return to
    return None


def _plan(rows: list[dict[str, Any]], op: str, to: str) -> list[dict[str, Any]]:
    changes = []
    for row in rows:
        after = "deleted" if op == "delete" else _new_tier(op, str(row["confidence"]), to)
        if after is not None and after != row["confidence"]:
            changes.append({**row, "after": after})
    return changes


def preview(project_root: Path, op: str, target: str, *, to: str = "") -> dict[str, Any]:
    """What ``op`` would change. Grants nothing and spends nothing."""
    op = str(op or "").strip().lower()
    to = str(to or "").strip().lower()
    if op not in OPS:
        return {"ok": False, "_error": "unknown_op", "_detail": f"op must be one of {sorted(OPS)}"}
    rows = _rows(project_root, target)
    changes = _plan(rows, op, to)
    return {
        "ok": True,
        "op": op,
        "to": to,
        "target": target,
        "anchors": rows,
        "changes": changes,
        "fingerprint": fingerprint(rows),
    }


def _event(project_root: Path, *, op: str, target: str, status: str, actor: dict, **extra) -> dict:
    """One control_plane_mutation row, as record_event takes it.

    The payload carries ids, tiers, counts and reasons only -- never a memory's
    path or content."""
    return {
        "event_kind": "control_plane_mutation",
        "source_kind": str(actor.get("source") or "anchor_service"),
        "capability_name": f"anchor_{op}",
        "action_kind": "anchor_admin",
        "target_entity": str(target),
        "status": status,
        "user_id": actor.get("user_id") or None,
        "principal_type": "human",
        "scope_id": str(project_root).replace("\\", "/"),
        "payload": {
            "op": op,
            "target": target,
            "status": status,
            "surface": actor.get("surface") or "",
            "auth_path": actor.get("auth_path") or "",
            **extra,
        },
    }


def _record(project_root: Path, event: dict) -> None:
    """Write one audit row; raises when it cannot be written."""
    from .execution_index_store import ExecutionIndexStore

    ExecutionIndexStore().record_event(project_root, **event)


def _audit(project_root: Path, **fields) -> bool:
    """Best-effort row for a refusal that changed nothing. True = recorded.

    A failed write never changes the verdict, but the caller reports it
    (r0b1: an audit failure must not masquerade as success)."""
    try:
        _record(project_root, _event(project_root, **fields))
    except Exception:  # noqa: BLE001 -- an audit failure never changes the verdict
        return False
    return True


def _change(
    project_root: Path, op: str, target: str, to: str, expected_fingerprint: str, who: str
) -> dict[str, Any]:
    """The compare-and-swap and the write: {"changes": [...]} when applied, else
    {"refused": code, "detail": ..., "extra": {...}} with nothing changed."""
    store = _store(project_root)
    store.init_db(project_root)
    now = time.time()
    with store.connect(project_root) as conn:
        # A pre-RFC-4 table has no confidence / source columns to write.
        from .anchor_schema_migration import migrate_memory_symbol_anchors

        migrate_memory_symbol_anchors(conn)
        ensure_tombstones(conn)
        conn.commit()
        # Compare-and-swap: the state is re-read and checked INSIDE the write
        # transaction, so two applies of one preview cannot both pass.
        conn.execute("BEGIN IMMEDIATE")
        rows = _select(conn, target, project_root)
        current = fingerprint(rows)
        if current != str(expected_fingerprint or ""):
            conn.rollback()
            return {
                "refused": "state_changed",
                "detail": "the anchors changed since the preview; preview again and confirm the new state",
                "extra": {"fingerprint": current},
            }
        changes = _plan(rows, op, to)
        if not changes:
            conn.rollback()
            return {
                "refused": "nothing_to_change",
                "detail": f"{op} changes no anchor of {target!r}",
                "extra": {},
            }
        for change in changes:
            key = (change["route_id"], change["symbol_name"], change["file_path"])
            if op == "delete":
                conn.execute(
                    "DELETE FROM memory_symbol_anchors WHERE anchor_id = ?", (change["anchor_id"],)
                )
                conn.execute(
                    f"INSERT OR REPLACE INTO {TOMBSTONE_TABLE} "
                    "(route_id, symbol_name, file_path, kind, ceiling, created_at, created_by) "
                    "VALUES (?, ?, ?, 'deleted', '', ?, ?)",
                    (*key, now, who),
                )
                continue
            conn.execute(
                "UPDATE memory_symbol_anchors SET confidence = ?, source = ? WHERE anchor_id = ?",
                (change["after"], SERVICE_SOURCE, change["anchor_id"]),
            )
            if op == "pin":
                conn.execute(
                    f"DELETE FROM {TOMBSTONE_TABLE} "
                    "WHERE route_id = ? AND symbol_name = ? AND file_path = ?",
                    key,
                )
            else:
                conn.execute(
                    f"INSERT OR REPLACE INTO {TOMBSTONE_TABLE} "
                    "(route_id, symbol_name, file_path, kind, ceiling, created_at, created_by) "
                    "VALUES (?, ?, ?, 'ceiling', ?, ?, ?)",
                    (*key, change["after"], now, who),
                )
        conn.commit()
    return {"changes": changes}


def apply(
    project_root: Path,
    op: str,
    target: str,
    *,
    expected_fingerprint: str,
    authorize: Authorize,
    actor: dict,
    to: str = "",
) -> dict[str, Any]:
    """Perform ``op`` on every anchor the target names -- only for an
    authorized caller, and only if the state is still the one previewed.

    Audited in three phases (three_phase_audit): the INTENT row is written
    before anything changes, and if it cannot be the apply is refused; the
    RESULT row follows, and if it cannot be written the change stands and is
    reported ``audit_degraded``. A refusal before the intent (permission,
    unknown op) changes nothing and is audited best-effort.

    Every call is its own ``attempt_id``, carried by each row it writes and
    by the reply: the audit store collapses byte-identical rows inside one
    time bucket, so without it two identical applies would share one intent
    row (r0b2 defb3b3f-865). It is random and names nothing."""
    from .three_phase_audit import three_phase_audited_execute

    op = str(op or "").strip().lower()
    to = str(to or "").strip().lower()
    attempt_id = secrets.token_hex(16)

    def _refuse(code: str, detail: str, **extra) -> dict[str, Any]:
        recorded = _audit(
            project_root,
            op=op,
            target=target,
            status="refused",
            actor=actor,
            attempt_id=attempt_id,
            reason=code,
            **extra,
        )
        return {
            "ok": False,
            "_error": code,
            "_detail": detail,
            "op": op,
            "target": target,
            "attempt_id": attempt_id,
            "audit": "recorded" if recorded else "failed",
            **extra,
        }

    try:
        allowed, why = authorize()
    except Exception as exc:  # noqa: BLE001 -- an unevaluable check refuses
        allowed, why = False, f"authorization check failed: {type(exc).__name__}"
    if not allowed:
        return _refuse("permission_denied", f"memory.manage_anchors required: {why}")
    if op not in OPS:
        return _refuse("unknown_op", f"op must be one of {sorted(OPS)}")
    who = str(actor.get("user_id") or actor.get("source") or "")

    def _result_event(result: dict[str, Any]) -> dict:
        if "refused" in result:
            return _event(
                project_root,
                op=op,
                target=target,
                status="refused",
                actor=actor,
                attempt_id=attempt_id,
                reason=result["refused"],
                **result["extra"],
            )
        changes = result["changes"]
        return _event(
            project_root,
            op=op,
            target=target,
            status="applied",
            actor=actor,
            attempt_id=attempt_id,
            to=to,
            count=len(changes),
            before=[{k: c[k] for k in ("anchor_id", "confidence")} for c in changes],
            after=[{"anchor_id": c["anchor_id"], "confidence": c["after"]} for c in changes],
        )

    run = three_phase_audited_execute(
        intent_event=_event(
            project_root,
            op=op,
            target=target,
            status="intent",
            actor=actor,
            attempt_id=attempt_id,
            to=to,
            expected_fingerprint=str(expected_fingerprint or ""),
        ),
        execute=lambda: _change(project_root, op, target, to, expected_fingerprint, who),
        result_event_builder=_result_event,
        sink=lambda event: _record(project_root, event),
        is_mutating=True,
    )
    if not run.executed:
        return {
            "ok": False,
            "_error": run.refused_reason,
            "_detail": "the intent audit row could not be written, so nothing was changed",
            "op": op,
            "target": target,
            "attempt_id": attempt_id,
            "audit": "failed",
        }
    audit = "degraded" if run.audit_degraded else "recorded"
    result = run.result
    if "refused" in result:
        refused: dict[str, Any] = {
            "ok": False,
            "_error": result["refused"],
            "_detail": result["detail"],
            "op": op,
            "target": target,
            "attempt_id": attempt_id,
            "audit": audit,
            "audit_degraded": run.audit_degraded,
            **result["extra"],
        }
        if run.audit_degraded:
            refused["_warning"] = (
                "nothing changed, but the refusal's audit row could not be written"
            )
        return refused
    out: dict[str, Any] = {
        "ok": True,
        "op": op,
        "to": to,
        "target": target,
        "changed": result["changes"],
        "attempt_id": attempt_id,
        "audit": audit,
        "audit_degraded": run.audit_degraded,
    }
    if run.audit_degraded:
        out["_warning"] = "the change is applied, but its result audit row could not be written"
    return out


def tombstone_for(conn, route_id: int, symbol_name: str, file_path: str) -> tuple[str, str] | None:
    """The tombstone an AUTOMATIC writer must honour: ('deleted', '') or
    ('ceiling', <tier>), else None. Never raises (no table = no tombstone)."""
    try:
        row = conn.execute(
            f"SELECT kind, ceiling FROM {TOMBSTONE_TABLE} "
            "WHERE route_id = ? AND symbol_name = ? AND file_path = ?",
            (route_id, symbol_name, file_path),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return (str(row[0]), str(row[1])) if row else None
