"""Deploy phase-2 SAFE-TO-EDIT surfacing (#326).

When the crown deploy gate passes its LOCAL gates it enters PHASE 2: the
ship-stage is frozen, so the live tree is free to edit again — but HEAD must
never move until the deploy finishes (runbook-deploy-ritual §3b). The gate
drops a marker at ``mcp/.deploy-reports/raw/safe-to-edit.flag`` at that
boundary and clears it on exit.

This reader turns that marker into a notice string. It is wired into the SAME
tool-output injection surface that ``run_notifications`` uses (drain into every
tool-call envelope), so the working agent learns of the phase-2 transition on
its NEXT tool call — no polling the deploy stream, and (unlike a UPS-only
nudge) it fires during autonomous tool-by-tool work with no operator prompt.

Pure + fail-quiet: any read error or missing marker returns None. A broken or
absent marker must NEVER fabricate a SAFE-TO-EDIT claim (empire-doctrine XV —
truth before green; the atomic-deploy HEAD guard stays the real enforcement).
"""

from __future__ import annotations

from pathlib import Path

import hashlib
import json
import sqlite3

from ._sqlite_index_store_base import SQLiteIndexStoreBase

# Rail-surfacing cap (operator 2026-07-16): these notices previously re-fired on
# EVERY tool call for the marker's whole lifetime. Same doctrine as the
# freeze-strike notices — tell the agent a bounded number of times, then stop
# nagging. Counted per (kind, epoch, session): the EPOCH is the notice's
# identity (deploy_id for safe-to-edit; failure-summary hash for deploy-failed).
# When the epoch changes (a new deploy / a new failure) the ledger drops the old
# epoch's rows and counts fresh against the new one. Legacy callers that pass no
# session stay uncapped. Cap = 3 (operator 2026-07-16).
#
# Storage (WAR M Phase A, #445 — no-file-layer): the ledger lives in the
# canonical kingdom sqlite (``deploy_notice_surfaces`` table), NOT the legacy
# ``.MEMORY/.index/deploy_notice_surfaces.json`` loose file. Heal-forward
# bridge mirrors the commission-stamp precedent: on first DB touch a
# still-present legacy JSON is adopted exactly once (stamped in ``index_meta``),
# then the DB is canonical — the file is never written again and is NOT deleted
# here (Phase B owns removal).
_MAX_NOTICE_SURFACES = 3
_LEGACY_SURFACE_LEDGER_REL = (".MEMORY", ".index", "deploy_notice_surfaces.json")
_SURFACE_ADOPTION_STAMP_KEY = "deploy_notice_surfaces_file_adopted"


class _DeployNoticeSurfaceStore(SQLiteIndexStoreBase):
    """Sqlite-backed (kind, epoch, session_id) → surface-count ledger."""

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deploy_notice_surfaces (
                kind TEXT NOT NULL,
                epoch TEXT NOT NULL,
                session_id TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (kind, epoch, session_id)
            )
            """,
        )

    def _adopt_legacy_file(self, conn: sqlite3.Connection, project_root: Path) -> None:
        """One-shot legacy-JSON adoption (stamped; file left in place)."""
        row = conn.execute(
            "SELECT 1 FROM index_meta WHERE key = ?",
            (_SURFACE_ADOPTION_STAMP_KEY,),
        ).fetchone()
        if row is not None:
            return
        legacy = Path(project_root).joinpath(*_LEGACY_SURFACE_LEDGER_REL)
        if legacy.is_file():
            try:
                counts = json.loads(legacy.read_text(encoding="utf-8"))
            except Exception:
                counts = None
            if isinstance(counts, dict):
                for key, value in counts.items():
                    parts = str(key).split(":", 2)
                    if len(parts) != 3:
                        continue
                    kind, epoch, session_id = parts
                    try:
                        count = int(value)
                    except (TypeError, ValueError):
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO deploy_notice_surfaces "
                        "(kind, epoch, session_id, count, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (kind, epoch, session_id, count, self._timestamp()),
                    )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (_SURFACE_ADOPTION_STAMP_KEY, self._timestamp()),
        )

    def bump(
        self,
        project_root: Path,
        *,
        kind: str,
        epoch: str,
        session_id: str,
    ) -> int:
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            self._adopt_legacy_file(conn, project_root)
            # Epoch rollover: drop this kind's rows from other epochs.
            conn.execute(
                "DELETE FROM deploy_notice_surfaces WHERE kind = ? AND epoch != ?",
                (kind, epoch),
            )
            row = conn.execute(
                "SELECT count FROM deploy_notice_surfaces "
                "WHERE kind = ? AND epoch = ? AND session_id = ?",
                (kind, epoch, session_id),
            ).fetchone()
            count = int(row["count"] if row is not None else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO deploy_notice_surfaces "
                "(kind, epoch, session_id, count, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, epoch, session_id, count, self._timestamp()),
            )
        return count


_SURFACE_STORE = _DeployNoticeSurfaceStore()


def _bump_notice_surfaces(
    project_root: Path,
    *,
    kind: str,
    epoch: str,
    session_id: str,
) -> int:
    """Increment + persist the surface count for (kind, epoch, session);
    return the new count. Rows of the same kind from OTHER epochs are
    pruned — an epoch change resets the count and re-keys the ledger to
    the new epoch. Fail-quiet: any ledger error returns 1 (the notice
    still shows — a broken ledger must silence nothing)."""
    try:
        return _SURFACE_STORE.bump(
            project_root, kind=kind, epoch=epoch, session_id=session_id
        )
    except Exception:
        return 1



# Under raw/ so it is gitignored + custody-excluded, and NOT a raw/*.json so the
# deploy-reports summarize guard (mcp/scripts/summarize_deploy_reports.py) leaves
# it alone. Resolved relative to the project root the deploy runs in.
_FLAG_CANDIDATES = (
    "mcp/.deploy-reports/raw/safe-to-edit.flag",
    ".deploy-reports/raw/safe-to-edit.flag",
)

# Deploy-wait conducting prep (2026-07-12): the gate drops this marker the
# moment a run is definitely underway (local single-flight lock won) and clears
# it in _release_deploy_resources. running-only = phase 1 (local gates, tree
# edit-frozen); safe-to-edit present = phase 2.
_RUNNING_FLAG_CANDIDATES = (
    "mcp/.deploy-reports/raw/deploy-running.flag",
    ".deploy-reports/raw/deploy-running.flag",
)


def _read_flag(
    project_root: Path,
    candidates: tuple[str, ...] = _FLAG_CANDIDATES,
) -> str | None:
    for rel in candidates:
        try:
            p = Path(project_root) / rel
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return None


def _parse_deploy_id(raw: str | None) -> str:
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("deploy_id="):
            return line.split("=", 1)[1].strip()
    return ""


def deploy_phase(project_root: Path | None) -> str | None:
    """Which window of a crown deploy are we in?

    Returns ``"safe-to-edit"`` when the phase-2 marker is present (local gates
    passed; ship-stage frozen; tree editable), ``"local-gates"`` when only the
    deploy-running marker is present (deploy underway, pre-phase-2: the tree
    is edit-frozen but read-only investigation is free), and ``None`` when no
    deploy is running. Fail-quiet: a broken read NEVER fabricates a phase.
    """
    if project_root is None:
        return None
    try:
        if _read_flag(project_root) is not None:
            return "safe-to-edit"
        if _read_flag(project_root, _RUNNING_FLAG_CANDIDATES) is not None:
            return "local-gates"
    except Exception:
        return None
    return None


_FAILED_FLAG_CANDIDATES = (
    "mcp/.deploy-reports/raw/deploy-failed.flag",
    ".deploy-reports/raw/deploy-failed.flag",
)


def deploy_failure_notice(
    project_root: Path | None,
    *,
    session_id: str = "",
) -> str | None:
    """The LAST deploy's FAIL SUMMARY, surfaced on the notification rail.

    #271 gave abort() a class + step + reason + artifact link — but it printed only
    to the deploy's stdout, which lands in a log nobody opens, while the harness
    reports just a bare exit code. So the operator saw "exit code 11" and had to go
    look up what 11 even means. The gate now writes that same summary to a flag and
    this reader turns it into a human notice on the NEXT tool call. Cleared by the
    next deploy's start (never shows a stale failure). Pure + fail-quiet: a missing
    or malformed flag returns None — it must NEVER fabricate a failure claim
    (empire-doctrine XV, truth before green)."""
    if project_root is None:
        return None
    raw = _read_flag(project_root, _FAILED_FLAG_CANDIDATES)
    if raw is None:
        return None
    f: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            f[k.strip()] = v.strip()
    if not f.get("class") and not f.get("exit"):
        return None
    if session_id:
        epoch = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        if _bump_notice_surfaces(
            project_root, kind="deploy_failed", epoch=epoch, session_id=session_id
        ) > _MAX_NOTICE_SURFACES:
            return None
    return (
        f"❌ DEPLOY FAILED — class={f.get('class', '?')} exit={f.get('exit', '?')}\n"
        f"   step:   {f.get('step', '?')}\n"
        f"   reason: {f.get('reason', '?')}\n"
        f"   full error log + per-gate artifacts -> {f.get('artifacts', 'mcp/.deploy-reports/')}\n"
        f"   (cleared automatically when the next deploy starts)"
    )


def safe_to_edit_notice(
    project_root: Path | None,
    *,
    session_id: str = "",
) -> str | None:
    """Return the phase-2 SAFE-TO-EDIT notice when a deploy has passed its local
    gates (marker present), else None. Never raises."""
    if project_root is None:
        return None
    raw = _read_flag(project_root)
    if raw is None:
        return None
    deploy_id = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("deploy_id="):
            deploy_id = line.split("=", 1)[1].strip()
            break
    if session_id:
        if _bump_notice_surfaces(
            project_root,
            kind="safe_to_edit",
            epoch=deploy_id or "unknown",
            session_id=session_id,
        ) > _MAX_NOTICE_SURFACES:
            return None
    tag = f" (deploy {deploy_id})" if deploy_id else ""
    return (
        f"✅ SAFE TO EDIT{tag} — the crown deploy gate passed its LOCAL gates; "
        f"the ship-stage is frozen, so the live tree is free to edit again. "
        f"Do NOT commit / move HEAD until the deploy finishes — the atomic-deploy "
        f"guard hard-fails. (runbook-deploy-ritual §3b)"
    )


# ── deploy-wait = conducting prep (operator directive 2026-07-12) ───────────
# While a crown deploy runs its LOCAL gates (pre-SAFE-TO-EDIT) the tree is
# edit-frozen, but a CONDUCTOR sitting idle wastes the window. The Stop hook
# calls this gate: a conductor-mode Stop during "local-gates" is blocked ONCE
# per deploy with read-only scouting orders, so it can conduct the moment the
# phase-2 notice fires. Lane workers / subagents (SubagentStop) are NEVER
# touched, and any internal error falls through to the normal Stop path — a
# broken nudge must never trap an agent.


def _running_flag_path(project_root: Path) -> Path | None:
    for rel in _RUNNING_FLAG_CANDIDATES:
        try:
            p = Path(project_root) / rel
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def conduct_the_wait_stop_gate(
    project_root: Path | None,
    *,
    event_name: str,
    host_session_id: str,
) -> dict[str, str] | None:
    """Return a Stop block envelope ordering a CONDUCTOR to scout read-only
    while the deploy's local gates run — or None (pass through).

    Fires only when ALL hold: event is ``Stop`` (SubagentStop excluded — a
    lane worker stopping must never be blocked), the caller's host session is
    mapped 'conductor' (msg_role_map, fail-closed), and
    ``deploy_phase() == "local-gates"``. Dedup: a ``stop-nudged-<deploy_id>``
    sidecar next to the running flag — same gitignored/custody-excluded raw/
    dir as its siblings, swept by the gate's _release_deploy_resources so it
    cannot accumulate, and deploy_id-suffixed so a stale leftover can never
    suppress the nudge for a FUTURE deploy. Fail-open: any error → None.
    """
    try:
        if event_name != "Stop":
            return None
        if project_root is None:
            return None
        if deploy_phase(project_root) != "local-gates":
            return None
        from .conductor_comms import msg_role_for_host

        if msg_role_for_host(project_root, host_session_id) != "conductor":
            return None
        flag = _running_flag_path(project_root)
        if flag is None:
            return None
        deploy_id = _parse_deploy_id(
            flag.read_text(encoding="utf-8", errors="replace")
        )
        safe_id = "".join(
            ch for ch in (deploy_id or "unknown") if ch.isalnum() or ch in "-_."
        ) or "unknown"
        marker = flag.parent / f"stop-nudged-{safe_id}.flag"
        if marker.exists():
            return None  # already nudged this deploy — the agent has its orders
        marker.write_text(f"host_session_id={host_session_id}\n", encoding="utf-8")
        shown_id = deploy_id or "in flight"
        return {
            "decision": "block",
            "reason": (
                f"⚔️ CONDUCT THE WAIT — deploy {shown_id} is running its local "
                f"gates (pre-SAFE-TO-EDIT), so the tree is edit-frozen but "
                f"READ-ONLY investigation is free. Do not idle: pick the next "
                f"open war (ai_backlog list status=open) and SCOUT it now — "
                f"ai_investigate / ai_find / ai_get_lines the territory, write "
                f"the dispatch brief — so you can conduct the moment the "
                f"SAFE-TO-EDIT notice fires. No edits, no commits, no HEAD "
                f"moves until then."
            ),
        }
    except Exception:
        return None
