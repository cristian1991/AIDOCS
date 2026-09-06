"""Deploy phase-2 SAFE-TO-EDIT surfacing (#326).

When the crown deploy gate passes its LOCAL gates it enters PHASE 2: the
ship-stage is frozen, so the live tree is free to edit again. Since #612 the
deploy is PINNED to one commit sha, so HEAD moving is fine too — a commit landing
mid-run belongs to the NEXT deploy (runbook-deploy-ritual §3b). The gate
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
import os
import sqlite3
import time

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
    lines = [
        f"❌ DEPLOY FAILED — class={f.get('class', '?')} exit={f.get('exit', '?')}",
        f"   step:   {f.get('step', '?')}",
        f"   reason: {f.get('reason', '?')}",
    ]
    # RUN IDENTITY (2026-09-02). Without it a reader cannot tell whether this
    # notice describes the deploy they just watched or a superseded one, and the
    # rail is exactly where that confusion is most expensive: it fires on an
    # unrelated tool call, with no surrounding context to date it by.
    #
    # OMITTED WHEN ABSENT, never rendered as '?'. A flag written before this
    # field existed, or by an abort that fired before the ship-stage freeze set
    # HEAD_SHA, genuinely has no identity — and a fabricated or placeholder
    # provenance line is worse than a missing one (empire-doctrine XV, truth
    # before green). 'unknown' is what abort() writes for a real gap, so it is
    # treated as absent here too.
    ident = []
    for key, label, width in (
        ("run_token", "run", None),
        ("head_sha", "sha", 8),
        ("ts", "at", None),
    ):
        value = f.get(key, "").strip()
        if value and value != "unknown":
            ident.append(f"{label}={value[:width] if width else value}")
    if ident:
        lines.append(f"   {'  '.join(ident)}")
    # Point at the ANSWER, not just the directory holding it. The 2026-09-02
    # exit-13 run had its four failing node ids on disk the whole time, inside a
    # 17MB raw JSON nothing named.
    failing = f.get("failing_tests", "").strip()
    if failing and failing not in ("none", "unknown"):
        lines.append(f"   failing tests -> {f.get('artifacts', '')}{failing}")
    lines.append(
        "   full error log + per-gate artifacts -> "
        f"{f.get('artifacts', 'mcp/.deploy-reports/')}",
    )
    # The old text said "cleared automatically when the next deploy starts",
    # which reads as "your evidence is about to be destroyed — hurry". It is
    # not: _invalidate_stale_run_receipts MOVES raw/* into raw/previous-run/
    # before a new run writes anything, so one generation always survives.
    lines.append(
        "   (a new deploy moves this run's evidence to "
        "mcp/.deploy-reports/raw/previous-run/, it is not deleted)",
    )
    return "\n".join(lines)


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
        f"COMMITTING IS ALSO FINE (#612): the deploy is pinned to one commit sha, "
        f"so anything you commit now belongs to the next deploy instead of "
        f"breaking this one. (runbook-deploy-ritual §3b)"
    )


# ── deploy-wait = conducting prep (operator directive 2026-07-12) ───────────
# While a crown deploy runs its LOCAL gates (pre-SAFE-TO-EDIT) the tree is
# edit-frozen, but a CONDUCTOR sitting idle wastes the window. The Stop hook
# calls this gate: a conductor-mode Stop during "local-gates" is blocked ONCE
# per deploy with read-only scouting orders, so it can conduct the moment the
# phase-2 notice fires. Lane workers / subagents (SubagentStop) are NEVER
# touched, and any internal error falls through to the normal Stop path — a
# broken nudge must never trap an agent.


# ── HEAD-freeze ownership (#600) — NOW BELT-AND-BRACES ONLY (#612) ───
# ORIGINAL JUSTIFICATION (#600, and it was true then): the gate signed its
# release manifest at one commit and pushed that same commit by reading the LIVE
# branch, so a commit landing in between turned its push into a non-fast-forward
# and discarded a full ~15min gate cycle. Background services cannot read a
# runbook, so they had to ASK — that is what this reader is for.
#
# WHY IT IS NO LONGER REQUIRED (#612, operator ruling 2026-07-29 — "the deploy
# should run on a commit sha, so we can actually work while a deploy runs"): the
# gate now PINS its target sha and pushes exactly that refspec, and it treats an
# origin tip that has advanced PAST the pin as healthy (the pinned commit is
# still reachable). A sitter commit, push, or merge during a deploy therefore
# cannot break it — the pin stays an ancestor either way.
#
# WHY IT IS KEPT ANYWAY, deliberately: removing a safety net in the same change
# that rewrites the riskiest file in the repository leaves no independent signal
# if the rewrite is wrong. The cost of keeping it is bounded and tiny (a backlog
# flush waits one poll interval, default 30s, and DEFERS — never drops). It is
# retained as EXPLICIT belt-and-braces, not as live law.
# RETIRE-BY: a separate follow-up, once several pinned-sha deploys have run green
# with mid-deploy commits landing. Retiring it means deleting this block, the
# ``head_freeze_owner`` reader, and the sitter's ``deferred_by`` short-circuit
# together — see backlog #612 for the recommendation and #600 for the history.
#
# Two independent signals, either one sufficient (a deploy owns the freeze if
# EITHER says so):
#   1. the gate's own local single-flight lock dir, ``aidocs_deploy_gate.lock``
#      under the temp dir (mcp/scripts/deploy_aidocs_gate.sh, Gate 0a2). It is
#      created by ``mkdir`` before any gate work and removed in
#      ``_release_deploy_resources`` on EXIT/INT/TERM.
#   2. the repo-relative ``deploy-running.flag``, dropped the moment that lock
#      is won and cleared on the same exit paths.
# Signal 1 is the authoritative owner lock but lives at a path both sides must
# agree on; signal 2 is unambiguous in location but only exists for a non-dry
# run. Consulting both means neither a TMPDIR disagreement nor a dry-run
# early-out can silently drop the guard.
_DEPLOY_LOCK_DIRNAME = "aidocs_deploy_gate.lock"

# STALENESS: a lock outlives its owner if the gate dies un-trapped (SIGKILL,
# power loss). The lock records the gate's shell pid, but that pid is NOT a
# usable liveness probe from here: under git-bash on Windows ``$$`` is an
# MSYS-namespaced pid, so ``OpenProcess`` on it would answer about an unrelated
# Windows process or nothing at all — a false "alive" would wedge backlog sync
# permanently and a false "dead" would reinstate the very race being fixed.
# AGE is the honest test instead. A gate cycle is 15-20 minutes; past this
# bound the marker is treated as abandoned and the deferral lifts on its own.
# Erring long is cheap (deferral loses nothing, it only delays a flush) and
# erring short reopens the race, hence the generous multiple.
_HEAD_FREEZE_MAX_AGE_S = 60 * 60.0


def _temp_roots() -> list[Path]:
    """Candidate temp dirs, in the order the deploy gate would resolve them.

    The gate uses ``${TMPDIR:-/tmp}`` under git-bash; this process may have a
    different view of "the temp dir" (native Windows vs MSYS mapping), so every
    plausible root is checked rather than betting on one.
    """
    seen: list[Path] = []
    raw = [os.environ.get(k, "") for k in ("TMPDIR", "TEMP", "TMP")]
    try:
        # Lazy: this module sits on the claude-hook hot path and ``tempfile``
        # drags ``shutil`` in with it. Only a HEAD-freeze query needs it.
        import tempfile

        raw.append(tempfile.gettempdir())
    except Exception:
        pass
    raw.append("/tmp")
    for r in raw:
        if not r:
            continue
        try:
            p = Path(r)
        except Exception:
            continue
        if p not in seen:
            seen.append(p)
    return seen


def _marker_is_fresh(p: Path) -> bool:
    """True when ``p`` was last written inside the freeze window.

    An unreadable mtime is treated as NOT fresh: a marker we cannot date must
    never be able to hold the freeze open forever.
    """
    try:
        return (time.time() - p.stat().st_mtime) < _HEAD_FREEZE_MAX_AGE_S
    except Exception:
        return False


def _deploy_lock_owner() -> str | None:
    for root in _temp_roots():
        try:
            lock = root / _DEPLOY_LOCK_DIRNAME
            if not lock.is_dir():
                continue
            info = lock / "info"
            stamp = info if info.is_file() else lock
            if not _marker_is_fresh(stamp):
                continue  # abandoned lock — do not wedge on it
            owner = ""
            if info.is_file():
                # Malformed/empty info still counts: the DIRECTORY is the lock,
                # the info file is only its description.
                owner = " ".join(
                    info.read_text(encoding="utf-8", errors="replace").split()
                )[:120]
            return f"deploy gate lock ({owner or 'owner unknown'})"
        except Exception:
            continue
    return None


def head_freeze_owner(project_root: Path | None) -> str | None:
    """Who currently owns the deploy's HEAD freeze, or None if nobody does.

    Callers that would otherwise move HEAD (commit / push / pull / rebase) must
    DEFER — not skip — their work while this returns a string. The return value
    is a short human-readable owner description, suitable for a status field.

    Fail-quiet in the SAFE direction for each signal independently: an
    unreadable marker is treated as absent (never fabricate a freeze), and an
    undatable or over-age marker is treated as abandoned (never wedge forever).
    """
    owner = _deploy_lock_owner()
    if owner is not None:
        return owner
    if project_root is None:
        return None
    flag = _running_flag_path(Path(project_root))
    if flag is not None and _marker_is_fresh(flag):
        try:
            deploy_id = _parse_deploy_id(
                flag.read_text(encoding="utf-8", errors="replace")
            )
        except Exception:
            deploy_id = ""
        return f"deploy {deploy_id or 'in flight'}"
    return None


def _running_flag_path(project_root: Path) -> Path | None:
    for rel in _RUNNING_FLAG_CANDIDATES:
        try:
            p = Path(project_root) / rel
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def _activity_since_nudge(
    project_root: Path,
    host_session_id: str,
    marker: Path,
) -> bool:
    """Did this conductor record ANY tool activity since it was last ordered?

    True  -> the order was carried out; stay silent (law 311bf3e6).
    False -> nothing happened since; the order was ignored, so repeat it (#806).

    FAILS TRUE ON ANY DOUBT, and that direction is deliberate: an unreadable
    ledger must not manufacture a repeated order. A missed nag costs one idle
    wait; a nag that fires against a working conductor teaches it to discount
    the whole channel, which is the decay this gate is trying to avoid.

    ONE indexed EXISTS read on the Stop path. #754's warning is about the ~20
    sqlite WRITE transactions the governed hook path takes, not a single
    bounded read.
    """
    try:
        import sqlite3

        from .execution_index_store import ExecutionIndexStore

        stamped_at = marker.stat().st_mtime
        store = ExecutionIndexStore()
        conn = store.connect(project_root)
        try:
            row = conn.execute(
                "SELECT 1 FROM execution_events WHERE session_id = ? "
                "AND observed_at > ? LIMIT 1",
                (host_session_id, stamped_at),
            ).fetchone()
        finally:
            # Explicit close: `with conn` is a TRANSACTION context manager in
            # sqlite3 and does NOT close the handle. That confusion leaked
            # 2.05 GB in the hook broker once (see _sqlite_index_store_base).
            conn.close()
        return row is not None
    except (sqlite3.Error, OSError, ImportError, ValueError):
        return True
    except Exception:
        return True


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
        # #806: the key is WORK-SINCE-NUDGE, not the deploy.
        #
        # This used to return None whenever the marker existed -- one order per
        # deploy, then silence. The rationale was law 311bf3e6
        # (backlog_surfacer.py:68-70): "re-issuing an order the agent already
        # carried out ... teaches the agent to discount the whole channel."
        # That law forbids repeating an order ALREADY CARRIED OUT. It says
        # nothing about one that was IGNORED -- and deploy_id encodes the
        # DEPLOY, not the conductor's behaviour, so the gate could not tell the
        # two apart. A conductor nudged at minute 1 of a 15-minute deploy and
        # idle for the remaining 14 was never re-ordered: the exact failure this
        # gate exists to prevent.
        #
        # Its sibling three lines away in claude_hook.py already keys correctly
        # -- the backlog reminder dedups on (epoch, backlog-STATE) and re-fires
        # when there is genuinely new information. This now matches it.
        #
        # NOT A TIMER, deliberately: re-nudging every N minutes repeats the
        # order whether or not it was followed, which IS the decay 311bf3e6
        # names. Pinned by test_conduct_the_wait_renudges_on_idle_806.
        if marker.exists() and _activity_since_nudge(project_root, host_session_id, marker):
            return None  # the order was carried out — silence is correct here
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
