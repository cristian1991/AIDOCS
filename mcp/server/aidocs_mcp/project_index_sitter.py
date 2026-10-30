"""ProjectIndexSitter — always-truthful hybrid code-index freshness service.

Treats externally added/edited/deleted project files as index-affecting
mutations even when they bypass AIDOCS tools, and guarantees read/discovery
tools never silently serve a known-stale index.

Generalizes folder_sitter into a service with:
  * WATCHDOG ACCELERATION when the package is available — event-driven, fast.
  * OS-AGNOSTIC POLLING RECONCILIATION as the always-on fallback — a plain
    sleep+reconcile loop that catches missed watcher events AND deletes even
    where watchdog can't run (serverless/locked-down hosts).
  * DELETE DETECTION + skip/sensitive filtering for free, by reconciling
    through the existing sync_code_manifest (deletes removed files from
    code_files/outlines/edges, reuses the walker's skip + sensitive rules) +
    sync_code_files (reparses new/edited).
  * SELF-WRITE SUPPRESSION via folder_sitter.mark_self_write so AIDOCS-owned
    edits (already reindexed on the pull path) don't double-index.
  * DEBOUNCE/BATCH for accelerated events; idempotent reconcile (mtime+size
    match → no reparse) for the poll path.
  * A KNOWN-STALE flag so read/discovery tools surface staleness instead of
    silently serving a stale index, plus lifecycle status + audit events.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from .folder_sitter import _is_self_write_recent  # reuse self-write suppression

_SKIP_PREFIXES = (
    ".git/",
    ".MEMORY/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    ".pytest-tmp/",
    ".runs/",
)
_SKIP_SUBPATHS = (
    "/__pycache__/",
    "/.venv/",
    "/venv/",
    "/node_modules/",
    "/dist/",
    "/build/",
    "/.pytest-tmp/",
    "/.runs/",
)


def _key(project_root: Path | str) -> str:
    try:
        return str(Path(project_root).resolve()).replace("\\", "/")
    except Exception:
        return str(project_root).replace("\\", "/")


# ── known-stale truth (read by the discovery-tool staleness gate) ────────
_KNOWN_STALE: set[str] = set()
_KS_LOCK = threading.Lock()
# Per-root STALE GENERATION (r0b2 P1, ordering): every mark bumps it, so a
# reconcile can clear only the marks that existed when its sync started -
# never one a concurrent producer (another sync's write_failed, a watcher
# event, an edit tool) set while it ran. Per root: other roots never block.
_STALE_GEN: dict[str, int] = {}


def mark_index_known_stale(project_root: Path | str) -> None:
    key = _key(project_root)
    with _KS_LOCK:
        _KNOWN_STALE.add(key)
        _STALE_GEN[key] = _STALE_GEN.get(key, 0) + 1


def stale_generation(project_root: Path | str) -> int:
    """How many known-stale marks this root has received (monotonic)."""
    with _KS_LOCK:
        return _STALE_GEN.get(_key(project_root), 0)


def clear_index_known_stale(project_root: Path | str, *, if_generation: int | None = None) -> bool:
    """Clear the ROOT mark. With ``if_generation`` this is a compare-and-clear:
    it clears only if no mark landed since that generation was read, and
    returns whether it cleared. Without it (tests, explicit resets) it is an
    unconditional RESET of all three states for the root (root mark, pending
    paths, coverage record)."""
    key = _key(project_root)
    with _KS_LOCK:
        if if_generation is not None and _STALE_GEN.get(key, 0) != if_generation:
            return False
        _KNOWN_STALE.discard(key)
        if if_generation is None:
            _PENDING.pop(key, None)
            _COVERAGE.pop(key, None)
        return True


# ── THREE SEPARATE STATES (scoped sitter, r0b2 6aa4b9c0-623 + d6b3dac7-69a) ──
#
#   (i)   PER-PATH FRESHNESS - _PENDING[root] = {rel: generation}. A watcher
#         event or a stat difference marks a path pending with a fresh
#         generation; a reconcile snapshots the generations it covers and
#         clears a path only if its generation is unchanged afterwards
#         (an event during the delta keeps the path pending).
#   (ii)  PER-FILE DNT ATTESTATION - in the registry DB (dnt_file_attestation,
#         see dnt_registry_sync); not held here.
#   (iii) TREE ENUMERATION COVERAGE - _COVERAGE[root] = {gen, valid, reason}.
#         Invalidated by startup, watch overflow / lost watcher, a stat error,
#         an unreliable clock / network drive, directory events and admission
#         changes (any missed-event signal); ONLY a full walk that saw the
#         generation unchanged from its start validates it.
#   The ROOT MARK (_KNOWN_STALE + _STALE_GEN, above) stays the failure /
#   external-change flag; only a full walk clears it (compare-and-clear).
# is_index_known_stale = root mark OR any pending path OR coverage invalid.
_PENDING: dict[str, dict[str, int]] = {}
_PENDING_SEQ: dict[str, int] = {}
_COVERAGE: dict[str, dict[str, Any]] = {}
#: A watcher batch larger than this is treated as an overflow (missed events
#: are likely): coverage is invalidated and the next run walks the tree.
MAX_PENDING_PATHS = 5000


def mark_paths_pending(project_root: Path | str, rels: Any) -> int:
    """Mark project-relative paths pending (each with a new generation).
    Returns how many paths are pending for the root afterwards."""
    key = _key(project_root)
    with _KS_LOCK:
        bucket = _PENDING.setdefault(key, {})
        for rel in rels:
            seq = _PENDING_SEQ.get(key, 0) + 1
            _PENDING_SEQ[key] = seq
            bucket[str(rel)] = seq
        return len(bucket)


def pending_paths(project_root: Path | str) -> dict[str, int]:
    with _KS_LOCK:
        return dict(_PENDING.get(_key(project_root)) or {})


def clear_pending_if_unchanged(project_root: Path | str, snapshot: dict[str, int]) -> list[str]:
    """Compare-and-clear per path: drop each path whose generation is still
    the one in ``snapshot`` (i.e. no event for it since the snapshot)."""
    key = _key(project_root)
    cleared: list[str] = []
    with _KS_LOCK:
        bucket = _PENDING.get(key)
        if not bucket:
            return cleared
        for rel, gen in snapshot.items():
            if bucket.get(rel) == gen:
                del bucket[rel]
                cleared.append(rel)
        if not bucket:
            _PENDING.pop(key, None)
    return cleared


def invalidate_coverage(project_root: Path | str, reason: str) -> None:
    """Enumeration coverage is no longer known complete: unhealthy (known-
    stale) until a full walk validates it."""
    key = _key(project_root)
    with _KS_LOCK:
        cov = _COVERAGE.setdefault(key, {"gen": 0, "valid": False, "reason": "", "validated_at": None})
        cov["gen"] = int(cov["gen"]) + 1
        cov["valid"] = False
        cov["reason"] = str(reason)


def coverage_state(project_root: Path | str) -> dict[str, Any] | None:
    with _KS_LOCK:
        cov = _COVERAGE.get(_key(project_root))
        return dict(cov) if cov is not None else None


def _coverage_gen(project_root: Path | str) -> int | None:
    with _KS_LOCK:
        cov = _COVERAGE.get(_key(project_root))
        return int(cov["gen"]) if cov is not None else None


def _validate_coverage_if(project_root: Path | str, gen: int | None) -> bool:
    """Compare-and-validate: only if no invalidation landed since ``gen``."""
    if gen is None:
        return False
    key = _key(project_root)
    with _KS_LOCK:
        cov = _COVERAGE.get(key)
        if cov is None or int(cov["gen"]) != gen:
            return False
        cov["valid"] = True
        cov["reason"] = ""
        cov["validated_at"] = time.time()
        return True


def is_index_known_stale(project_root: Path | str) -> bool:
    """Cheap in-process check: has an external change been observed but not yet
    reconciled? Read/discovery tools consult this so they never silently serve
    a KNOWN-stale index (no per-read filesystem walk needed). True for the
    root mark, any pending path, or enumeration coverage not (yet) valid.
    """
    key = _key(project_root)
    with _KS_LOCK:
        if key in _KNOWN_STALE or _PENDING.get(key):
            return True
        cov = _COVERAGE.get(key)
        return cov is not None and not cov["valid"]


def stale_reasons(project_root: Path | str) -> list[str]:
    key = _key(project_root)
    out: list[str] = []
    with _KS_LOCK:
        if key in _KNOWN_STALE:
            out.append("root_mark")
        if _PENDING.get(key):
            out.append(f"pending_paths:{len(_PENDING[key])}")
        cov = _COVERAGE.get(key)
        if cov is not None and not cov["valid"]:
            out.append(f"coverage:{cov['reason'] or 'unknown'}")
    return out


# ── freshness window (poll-cadence truth, no filesystem walk) ─────────────
# Per-project freshness context: when the index was last reconciled, the poll
# cadence guaranteeing the next reconcile, and whether watchdog acceleration is
# live. Read by the bounded freshness guard so high-value read/discovery tools
# can distinguish three states cheaply: KNOWN-stale (observed-but-unreconciled),
# FRESH (reconciled within the poll window or watchdog-accelerated), and
# POLL-WINDOW-RISK / UNKNOWN (the last reconcile is older than the cadence that
# was supposed to refresh it, so an external change may be unreflected).
_FRESHNESS: dict[str, dict] = {}
_FRESH_LOCK = threading.Lock()


def _register_freshness_context(
    project_root: Path | str,
    *,
    poll_seconds: int | None,
    watchdog: bool,
) -> None:
    with _FRESH_LOCK:
        info = _FRESHNESS.setdefault(_key(project_root), {})
        info["poll_seconds"] = poll_seconds
        info["watchdog"] = bool(watchdog)


def _clear_freshness_context(project_root: Path | str) -> None:
    """A stopped sitter no longer guarantees a poll cadence — drop poll/watchdog
    context but keep last_reconcile_at so window math stays honest.
    """
    with _FRESH_LOCK:
        info = _FRESHNESS.get(_key(project_root))
        if info is not None:
            info["poll_seconds"] = None
            info["watchdog"] = False


def _record_reconcile_time(project_root: Path | str) -> None:
    with _FRESH_LOCK:
        _FRESHNESS.setdefault(_key(project_root), {})["last_reconcile_at"] = time.time()


def freshness_window(project_root: Path | str) -> dict:
    """Cheap, bounded freshness truth — NO filesystem walk. Returns a state +
    ``trustworthy`` flag so callers never have to guess:

      * ``known_stale``      — observed change not yet reconciled (hard).
      * ``fresh``            — watchdog-accelerated, or reconciled within the
                               configured poll window.
      * ``poll_window_risk`` — polling-only and the last reconcile is OLDER than
                               the poll window that should have refreshed it.
      * ``unknown``          — never reconciled, or no active sitter guaranteeing
                               a cadence.
    """
    if is_index_known_stale(project_root):
        return {"state": "known_stale", "trustworthy": False, "reasons": stale_reasons(project_root)}
    with _FRESH_LOCK:
        info = dict(_FRESHNESS.get(_key(project_root)) or {})
    last = info.get("last_reconcile_at")
    if last is None:
        return {"state": "unknown", "trustworthy": False, "reason": "no_reconcile_yet"}
    age = round(max(0.0, time.time() - float(last)), 3)
    poll = info.get("poll_seconds")
    if info.get("watchdog"):
        # Watchdog delivers events in near-real-time; an unreconciled change
        # would already have set the known-stale flag checked above.
        return {"state": "fresh", "trustworthy": True, "age_seconds": age, "backend": "watchdog"}
    if poll is None:
        # No running poll loop → no cadence guarantee. Honest: we cannot vouch.
        return {
            "state": "unknown",
            "trustworthy": False,
            "age_seconds": age,
            "reason": "no_active_sitter",
        }
    if age <= float(poll):
        return {
            "state": "fresh",
            "trustworthy": True,
            "age_seconds": age,
            "window_seconds": poll,
            "backend": "polling",
        }
    return {
        "state": "poll_window_risk",
        "trustworthy": False,
        "age_seconds": age,
        "window_seconds": poll,
        "backend": "polling",
    }


_EXCLUDED_EVENTS: dict[str, int] = {}
_EXCLUDED_EVENTS_LOCK = threading.Lock()


def excluded_event_counts() -> dict[str, int]:
    """Watcher events dropped by the main-tree policy, by reason (process-wide)."""
    with _EXCLUDED_EVENTS_LOCK:
        return dict(_EXCLUDED_EVENTS)


def event_relevant(project_root: Path, abs_path: str) -> bool:
    """Whether a filesystem event should trigger a reconcile: inside root, not
    a skipped dir, and NOT an AIDOCS self-write (already reindexed on the pull
    path). Sensitive paths are additionally refused by the sync walker.
    """
    try:
        rel = Path(abs_path).resolve().relative_to(Path(project_root).resolve()).as_posix()
    except Exception:
        return False
    if not rel or rel.startswith(_SKIP_PREFIXES):
        return False
    if any(seg in rel for seg in _SKIP_SUBPATHS):
        return False
    # MAIN-tree policy (r0b2 worktree isolation): a save inside `.tmp/`, a
    # nested git worktree, a dot-dir or site-packages can never change a
    # main-index row (the walk prunes them, admission refuses them), so it
    # must not mark the MAIN index stale nor trigger a full main reconcile.
    # Counted by reason - excluded_event_counts() - never silent.
    from .index_tree_policy import excluded_rel_reason, invalidate_marker

    # A `.git` appearing (or a checkout moved in) must not hide behind a
    # cached negative probe: drop the probe of this path and its parent.
    invalidate_marker(abs_path)
    invalidate_marker(str(Path(abs_path).parent))
    reason = excluded_rel_reason(project_root, rel)
    if reason is not None:
        with _EXCLUDED_EVENTS_LOCK:
            _EXCLUDED_EVENTS[reason] = _EXCLUDED_EVENTS.get(reason, 0) + 1
        return False
    try:
        mtime = Path(abs_path).resolve().stat().st_mtime_ns
    except Exception:
        mtime = -1
    if _is_self_write_recent(rel, mtime):
        return False
    return True


def _audit_reconcile(
    project_root: Path,
    hub: Any,
    *,
    tracked: int,
    synced: int,
    state: str,
    trigger: str,
    principal_type: str = "system",  # noqa: ARG001 — kept for call-site parity
) -> None:
    """Record the reconcile as a HEARTBEAT, not as an audit event.

    2026-08-23: this wrote one ``index_sitter_reconcile`` row into
    ``execution_events`` per reconcile -- 30,202 rows in 27 days, the
    second-largest kind in the table whose write lock saturated and took the
    daemon down. A 2026-07-20 fix had already silenced the no-op POLL rows;
    what remained was the watchdog firing on every file save, and that is not
    an audit-worthy act. An audit trail records decisions, refusals and
    privileged acts. Nothing in the tree ever read these rows.

    The observability they nominally offered ("when did the index reconcile,
    and when before that") is now kept at O(1) in ``index_reconcile_state`` --
    one row, upserted, with last + previous moments and a separate
    last-unhealthy stamp so a bad state is not erased by the next good poll.
    Every reconcile updates it, including the no-op polls the old code had to
    drop to survive its own volume, so the heartbeat is strictly MORE
    truthful than what it replaces.
    """
    try:
        hub.execution.record_index_reconcile(
            project_root,
            tracked=tracked,
            synced=synced,
            state=state,
            trigger=trigger,
        )
    except Exception:
        pass


def reconcile(
    project_root: Path,
    hub: Any,
    *,
    trigger: str = "poll",
    principal_type: str = "system",
    integrity: bool = False,
) -> dict:
    """Always-truthful full reconcile - the FULL WALK (startup, safety sweep,
    integrity sweep, every coverage loss). ``integrity`` re-hashes files whose
    stat is unchanged (a same-size rewrite inside one mtime tick). The only
    pass that may validate enumeration coverage, clear pending paths
    wholesale and clear the root mark - each by compare-and-clear against the
    generation read before the walk. Full reconcile. ``sync_code_files`` already runs
    ``sync_code_manifest`` ONCE internally (adds new, removes DELETED from all
    index tables, marks edited unparsed, reuses skip + sensitive filters) before
    reparsing new/edited — so we call ONLY ``sync_code_files`` here. Calling
    ``sync_code_manifest`` separately would walk the source tree a second time
    for no benefit (the duplicate-walk removed 2026-05-24). Idempotent: an
    unchanged tree reparses nothing. Clears the known-stale flag iff the index
    ends up ready; records the reconcile time for the freshness window; audited.

    PERFORMANCE TRUTH: the poll path uses ``code_index_db_status`` (DB-only
    COUNT queries — NO filesystem walk, NO sha256) to decide success. The
    expensive ``_code_freshness`` on-disk verification is reserved for explicit
    check/sync diagnostics (``index-sitter --check`` / ``ai_index_status``).
    Drift-since-disk is covered by the next poll's own ``sync_code_files``
    re-stat plus the known-stale flag and the poll-window-risk window.
    """
    pr = Path(project_root)
    try:
        # sync_code_files centrally resolves effective include_tests from
        # live config — a stale process with INDEX_INCLUDE_TESTS=False will
        # auto-promote to True if the live config says include_tests=true.
        #
        # ...and it resolves that setting ONCE PER FILE. MEASURED 2026-08-05
        # with the connect sampler on the live daemon: a single sync opened
        # 2,162 sqlite connections through config_resolver._db_layer (two
        # layers x ~1,081 files), 70% of the 3,123 connects that one tool call
        # cost in total (+415 MB, +49s CPU). That is what made the MANAGED HTTP
        # backend climb ~1,200 handles/minute at 82% of a core while the direct
        # backend, which does not run the sitter, stayed perfectly flat.
        #
        # The request-scoped layer cache that solves this already exists and is
        # already entered by the two OTHER entrypoints (mcp_server's tool-call
        # wrapper, claude_hook's event handler). The sitter runs on its own
        # thread and so never inherited it — the same anchor-failure shape as
        # #746/#755: one correct decision, unreachable from a third caller.
        # Entering it here reads each layer ONCE for the whole sync, and is
        # verdict-identical (config writes call invalidate_request_config_scope).
        from .config_resolver import request_config_scope

        # The DNT verdict must be THIS invocation's, for THIS root: it comes
        # back through a private out-param (the former shared store field
        # _last_dnt_derived, which a concurrent sync on the same store could
        # overwrite between our return and our read, is gone - r0b2 P1).
        dnt_supported = bool(getattr(hub.code, "DNT_VERDICT_SUPPORTED", False))
        dnt_verdict: dict[str, Any] = {}
        # Marks that exist NOW are the ones this sync can reconcile; one that
        # lands while it runs (another sync's failure, a watcher event, an edit)
        # is newer than our verdict and must survive it.
        stale_gen_at_start = stale_generation(pr)
        coverage_gen_at_start = _coverage_gen(pr)
        pending_at_start = pending_paths(pr)
        with request_config_scope():
            if dnt_supported:
                kw: dict[str, Any] = {"dnt_verdict_out": dnt_verdict}
                if integrity:
                    kw["integrity"] = True
                synced = int(hub.code.sync_code_files(pr, **kw) or 0)
            else:
                synced = int(hub.code.sync_code_files(pr) or 0)
    except Exception as exc:
        # FAIL-TRUTHFUL: leave known-stale set so reads keep warning.
        mark_index_known_stale(pr)
        return {"ok": False, "mode": "full", "reason": repr(exc), "known_stale": True, "trigger": trigger}
    state, tracked = "", 0
    try:
        status = hub.code.code_index_db_status(pr) or {}
        state = str(status.get("db_state") or "")
        tracked = int(status.get("code_files") or 0)
    except Exception:
        state, tracked = "", 0
    # "ready" = rows all parsed; "empty" = no source files (a legitimately empty
    # tree, e.g. after deleting the last file). Both mean the reconcile succeeded
    # and the DB is internally consistent → clear known-stale. Only "unparsed"
    # (or an unknown/failed state) leaves the warning standing.
    # DNT TRUTH OF THE SAME SYNC (r0b2 P1): a ready code DB does not make the
    # index trustworthy when this sync reported its DNT registry unverified or
    # failed - the stale mark the sync set must survive, and the heartbeat
    # must say why. This reconcile is a FULL sync, so only a whole-registry
    # verification (reconciled.verified) counts as verified.
    if not dnt_supported:
        dnt_trust = "not_reported"  # genuinely non-DNT code store
    elif (
        dnt_verdict.get("project_root") == str(pr)
        and dnt_verdict.get("full_sync") is True
        and isinstance(dnt_verdict.get("report"), dict)
    ):
        dnt_trust = _dnt_trust(dnt_verdict["report"])
    else:
        dnt_trust = "unverified"  # a DNT store gave no verdict for this call: fail closed
    good = state in ("ready", "empty") and dnt_trust in ("verified", "not_reported")
    # COMPARE-AND-CLEAR (r0b2 P1, ordering): a good verdict clears only the
    # marks that predate this sync. A newer mark - set by ANY producer while
    # the sync ran - wins; the next reconcile judges it.
    cleared = good and clear_index_known_stale(pr, if_generation=stale_gen_at_start)
    coverage_validated = False
    if good:
        # (iii) the walk enumerated the tree: coverage is valid - unless an
        # invalidation landed while it ran (compare-and-validate); (i) every
        # path pending at the start was covered by the walk (compare-and-clear
        # per path: a newer event for a path keeps it pending).
        coverage_validated = _validate_coverage_if(pr, coverage_gen_at_start)
        clear_pending_if_unchanged(pr, pending_at_start)
    if good and not cleared:
        state = f"{state}+newer_stale_mark"  # not healthy in the heartbeat row
    if cleared:
        # Durable batch-write dirt clears only per path, only when that path's
        # row is VERIFIED against disk - never because a pass ran (r0b2
        # 09788453-f2e: a refused or absent row stays dirty).
        try:
            from .batch_write_guard import reverify_dirty

            reverify_dirty(pr)
        except Exception:
            pass
    elif not good:
        mark_index_known_stale(pr)
        if dnt_trust not in ("verified", "not_reported"):
            state = f"{state}+dnt_{dnt_trust}"  # unhealthy in the heartbeat row
    _record_reconcile_time(pr)
    # HEARTBEAT, every reconcile. The 2026-07-20 "meaningful only" filter
    # existed solely because each pass wrote an append-only audit row and a
    # steady-state project reconciles every poll interval (the ogp census
    # found 1900+ back-to-back no-op rows). That defence was not enough --
    # the watchdog fires on every file save, so 30,202 rows still landed in
    # 27 days and became the second-largest kind in the table that saturated
    # the audit write lock on 2026-08-23.
    #
    # The row is now ONE upserted heartbeat (see
    # ExecutionIndexStore.record_index_reconcile), so recording EVERY
    # reconcile costs nothing and the suppression can go: "when did the index
    # last reconcile" is finally answered for the no-op polls too.
    _audit_reconcile(
        pr,
        hub,
        tracked=tracked,
        synced=synced,
        state=state,
        trigger=trigger,
        principal_type=principal_type,
    )
    return {
        "ok": True,
        "tracked_rows": tracked,
        "synced_rows": synced,
        "freshness_state": state,
        "known_stale": is_index_known_stale(pr),
        "dnt_trust": dnt_trust,
        "trigger": trigger,
        "mode": "full",
        "coverage_validated": coverage_validated,
        "integrity_mismatches": list(dnt_verdict.get("integrity_mismatches") or []),
    }


def reconcile_paths(
    project_root: Path,
    hub: Any,
    rels: Any,
    *,
    trigger: str = "watchdog",
) -> dict:
    """SCOPED reconcile: a delta sync of exactly ``rels`` (deletes and both
    rename sides included; no walk, no manifest sweep). Clears each path's
    pending entry by compare-and-clear against the generation read before
    the delta; a path kept unread (DNT fail-closed) or a failed delta stays
    pending. NEVER validates coverage and NEVER clears the root mark - its
    report is about these paths only (r0b2 6aa4b9c0-623)."""
    pr = Path(project_root)
    wanted = sorted({str(r).replace("\\", "/") for r in rels})
    snapshot = {r: g for r, g in pending_paths(pr).items() if r in set(wanted)}
    verdict: dict[str, Any] = {}
    try:
        from .config_resolver import request_config_scope

        with request_config_scope():
            synced = int(
                hub.code.sync_code_files(pr, paths=wanted, incremental=True, delta=True, dnt_verdict_out=verdict)
                or 0
            )
    except Exception as exc:  # noqa: BLE001 - paths stay pending; reported
        return {"ok": False, "mode": "scoped", "reason": repr(exc), "known_stale": True, "trigger": trigger}
    report = verdict.get("report") if verdict.get("project_root") == str(pr) else None
    failed = not isinstance(report, dict) or (
        not report.get("ok") and str(report.get("status") or "") != "unverified"
    )
    unread: set[str] = set()
    if isinstance(report, dict) and isinstance(report.get("reconciled"), dict):
        unread = set(report["reconciled"].get("kept_unreadable") or [])
    cleared: list[str] = []
    if not failed:
        cleared = clear_pending_if_unchanged(pr, {r: g for r, g in snapshot.items() if r not in unread})
    _record_reconcile_time(pr)
    if failed:
        state = "scoped+dnt_failed"
    elif unread:
        state = "scoped+dnt_unverified"
    else:
        state = "ready"
    _audit_reconcile(pr, hub, tracked=0, synced=synced, state=state, trigger=trigger)
    return {
        "ok": not failed,
        "mode": "scoped",
        "paths": len(wanted),
        "cleared": len(cleared),
        "dropped": list(verdict.get("dropped") or []),
        "kept_unreadable": sorted(unread),
        "known_stale": is_index_known_stale(pr),
        "trigger": trigger,
    }


def _dnt_trust(report: Any) -> str:
    """Tri-state DNT trust of ONE sync invocation's report (handed back by
    sync_code_files through ``dnt_verdict_out``): ``verified`` (ok and the WHOLE registry verified),
    ``unverified`` (kept_unreadable / concurrent writes / a pass that did not
    verify), ``failed`` (a registry write or the reconcile failed).
    The caller decides ``not_reported`` (a store without DNT support) and
    ``unverified`` for a DNT store that returned no verdict; a non-dict here
    is treated as unverified, never as not_reported."""
    if not isinstance(report, dict):
        return "unverified"
    status = str(report.get("status") or "")
    if status in ("write_failed", "error") or (not report.get("ok") and status not in ("unverified", "unverifiable")):
        return "failed"
    if status in ("unverified", "unverifiable"):
        return "unverified"
    reconciled = report.get("reconciled")
    if not isinstance(reconciled, dict) or reconciled.get("verified") is not True:
        return "unverified"
    return "verified"


class ProjectIndexSitter:
    """Per-project hybrid sitter: watchdog acceleration (if available) over an
    always-on polling reconciler. Lifetime: process-long; idempotent start.

    SCOPED (operator-approved, r0b2 6aa4b9c0-623 + d6b3dac7-69a). The sitter
    used to run a FULL sync of the whole project on every trigger (every 30 s
    poll, every 500 ms watcher batch, startup): one external save walked the
    tree and opened every file. Now, per trigger:
      * watchdog: the debounce COLLECTS the changed paths (both sides of a
        move; a directory event or a `.git` admission change marks every
        indexed path under it and invalidates coverage) and runs a DELTA of
        exactly those paths;
      * poll: a STAT-ONLY pass (no opens) over the indexed + attested paths
        and the directories they live in (a changed directory is listed to
        find new files; a new subdirectory invalidates coverage), then a
        delta of what changed;
      * FULL WALK only at startup, as a safety sweep every <= 5 min (missed
        events / inventory completeness), as an integrity sweep (re-hash:
        stat-only cannot see a same-size rewrite in one mtime tick), and
        whenever enumeration coverage is lost: watch overflow, a lost
        watcher, a stat error, an unreliable clock, a network drive, a
        directory or admission event. Reported unhealthy (known-stale)
        until that walk validates coverage.
    A store without delta support (no DNT_VERDICT_SUPPORTED) keeps the old
    full reconcile on every trigger. Runs of one sitter are serialized
    (_run_lock); different roots stay concurrent.
    """

    def __init__(
        self,
        project_root: Path,
        hub: Any,
        *,
        poll_seconds: int = 30,
        debounce_ms: int = 500,
        enable_watchdog: bool = True,
        sweep_seconds: int = 300,
        integrity_seconds: int = 3600,
    ) -> None:
        self.project_root = Path(project_root)
        self.hub = hub
        self.poll_seconds = max(2, int(poll_seconds))
        self.debounce_ms = max(50, int(debounce_ms))
        self.enable_watchdog = bool(enable_watchdog)
        # the safety sweep never exceeds 5 minutes (r0b2: not 15 yet)
        self.sweep_seconds = max(self.poll_seconds, min(300, int(sweep_seconds)))
        self.integrity_seconds = max(self.sweep_seconds, int(integrity_seconds))
        self._observer: Any = None
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._startup_thread: threading.Thread | None = None
        self._flush_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._run_lock = threading.RLock()
        self._started = False
        self._backends: list[str] = []
        self._last_reconcile_at: float | None = None
        self._last_result: dict = {}
        self._last_full_at: float | None = None
        self._last_integrity_at: float | None = None
        # directory baseline of the last full walk: rel dir -> (mtime_ns, child dir names)
        self._dir_baseline: dict[str, tuple[int, frozenset[str]]] = {}
        self._remote: bool | None = None

    # ── lifecycle ──
    def begin_coverage(self) -> None:
        """Coverage is unknown until the startup walk validates it."""
        invalidate_coverage(self.project_root, "startup")

    def start(self) -> bool:
        if self._started:
            return True
        self._stop.clear()
        self.begin_coverage()
        # Polling is the ALWAYS-ON truthful backbone (OS-agnostic).
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name=f"index-sitter-poll:{self.project_root.name}",
        )
        self._poll_thread.start()
        self._backends = ["polling"]
        # Watchdog is OPTIONAL acceleration on top (skippable for honest
        # polling-only operation, e.g. locked-down hosts or tests).
        watchdog = bool(self.enable_watchdog and self._try_start_watchdog())
        if watchdog:
            self._backends.insert(0, "watchdog")
        self._started = True
        _register_freshness_context(
            self.project_root,
            poll_seconds=self.poll_seconds,
            watchdog=watchdog,
        )
        # STARTUP TRUTH: don't wait a full poll interval to establish freshness.
        # Kick an immediate reconcile in the background so the window where reads
        # see freshness=unknown/no_reconcile_yet is as short as possible (and the
        # read-tool gate surfaces that window honestly until this lands).
        self._startup_thread = threading.Thread(
            target=lambda: self._run("startup"),
            daemon=True,
            name=f"index-sitter-startup:{self.project_root.name}",
        )
        self._startup_thread.start()
        return True

    def owned_threads(self) -> list[threading.Thread]:
        """The threads this sitter started (poll, startup, debounce timer)."""
        return [t for t in (self._poll_thread, self._startup_thread, self._flush_timer) if t is not None]

    def stop(self, join_timeout: float | None = None) -> bool:
        """Stop the sitter. Signals every loop (a queued startup / poll /
        debounce run then never reaches reconcile - see _run) and, with
        ``join_timeout``, waits up to that many seconds in total for the
        threads it owns to finish an in-flight reconcile. Returns True when
        nothing it owns is still running (always True without a join).
        QUIESCE (r0b2 P1): without the join, an in-flight walk outlived the
        stop."""
        self._stop.set()
        self._started = False
        _clear_freshness_context(self.project_root)
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
        if self._flush_timer is not None:
            try:
                self._flush_timer.cancel()
            except Exception:
                pass
        if join_timeout is None:
            return True
        deadline = time.monotonic() + max(0.0, float(join_timeout))
        me = threading.current_thread()
        for t in self.owned_threads():
            if t is me:
                continue
            t.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in self.owned_threads() if t is not me)

    def _try_start_watchdog(self) -> bool:
        if not self.enable_watchdog:
            return False
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            return False
        sitter = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: ANN001
                try:
                    sitter._on_event(event)
                except Exception:  # noqa: BLE001 - an unreadable event is a missed event
                    invalidate_coverage(sitter.project_root, "watch_event_error")
                    sitter._schedule()

        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), str(self.project_root), recursive=True)
            self._observer.daemon = True
            self._observer.start()
            return True
        except Exception:
            self._observer = None
            return False

    # ── change collection ──
    def _rel(self, abs_path: str) -> str | None:
        try:
            return Path(abs_path).resolve().relative_to(self.project_root.resolve()).as_posix()
        except Exception:
            return None

    def _on_event(self, event: Any) -> None:
        etype = str(getattr(event, "event_type", "") or "")
        paths = [p for p in (getattr(event, "src_path", None), getattr(event, "dest_path", None)) if p]
        if getattr(event, "is_directory", False):
            if etype == "modified":
                return  # a directory's own "modified" is its children's events
            self._on_directory_change(paths, reason=f"directory_{etype or 'event'}")
            return
        if any(Path(p).name == ".git" for p in paths):
            # a checkout appearing / disappearing changes admission for its
            # whole directory: mark it and walk (coverage)
            self._on_directory_change([str(Path(p).parent) for p in paths], reason="admission_change")
            return
        self.on_paths(paths)

    def on_paths(self, abs_paths: Any) -> None:
        """Collect changed paths (both sides of a move) for the next delta."""
        rels = []
        for p in abs_paths:
            if event_relevant(self.project_root, p):
                rel = self._rel(p)
                if rel:
                    rels.append(rel)
        if not rels:
            return
        if mark_paths_pending(self.project_root, rels) > MAX_PENDING_PATHS:
            invalidate_coverage(self.project_root, "watch_overflow")
        self._schedule()

    def _on_directory_change(self, abs_dirs: Any, *, reason: str) -> None:
        prefixes = [r for r in (self._rel(d) for d in abs_dirs) if r is not None]
        under = self._indexed_paths_under(prefixes)
        if under:
            mark_paths_pending(self.project_root, under)
        invalidate_coverage(self.project_root, reason)
        self._schedule()

    def _indexed_paths_under(self, prefixes: list[str]) -> list[str]:
        out: list[str] = []
        try:
            known = self._known_versions()
        except Exception:  # noqa: BLE001
            return out
        for rel in known:
            for pre in prefixes:
                if pre in ("", ".") or rel == pre or rel.startswith(pre.rstrip("/") + "/"):
                    out.append(rel)
                    break
        return out

    def _schedule(self) -> None:
        if not self._started:
            return  # not running (tests drive _run directly)
        with self._lock:
            if self._flush_timer is None or not self._flush_timer.is_alive():
                self._flush_timer = threading.Timer(
                    self.debounce_ms / 1000.0,
                    lambda: self._run("watchdog"),
                )
                self._flush_timer.daemon = True
                self._flush_timer.start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self._run("poll")

    # ── stat-only pass ──
    def _known_versions(self) -> dict[str, tuple[int, int]]:
        """rel -> (mtime_ns, size_bytes) of every indexed and attested path
        (two DB reads, no file access)."""
        known: dict[str, tuple[int, int]] = {}
        with self.hub.code.connect(self.project_root) as conn:
            for r in conn.execute("SELECT path, mtime_ns, size_bytes FROM code_files"):
                known[str(r[0])] = (int(r[1] or 0), int(r[2] or 0))
        from .protected_file_registry_store import ProtectedFileRegistryStore

        for rel, (m, s, _d) in ProtectedFileRegistryStore().get_attestations(self.project_root).items():
            known.setdefault(rel, (m, s))
        return known

    def _on_unreliable_filesystem(self) -> bool:
        """A network drive (or UNC share): mtimes / events are unreliable -
        every pass walks."""
        if self._remote is None:
            self._remote = False
            root = str(self.project_root)
            if root.startswith(("\\\\", "//")):
                self._remote = True
            elif os.name == "nt":
                try:
                    import ctypes

                    drive = os.path.splitdrive(os.path.abspath(root))[0]
                    if drive:
                        self._remote = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
                except Exception:  # noqa: BLE001
                    self._remote = False
        return bool(self._remote)

    def _stat_scan(self) -> tuple[str, Any]:
        """('ok', changed rels) or ('forced', reason). Stats every known path
        and the baseline directories; lists ONLY a directory whose mtime
        moved. Never opens a file."""
        try:
            known = self._known_versions()
        except Exception:  # noqa: BLE001
            return "forced", "index_unreadable"
        root = str(self.project_root)
        horizon = time.time_ns() + 2_000_000_000
        changed: list[str] = []
        for rel, version in known.items():
            try:
                st = os.stat(os.path.join(root, rel))
            except FileNotFoundError:
                changed.append(rel)
                continue
            except OSError:
                return "forced", "stat_error"
            if st.st_mtime_ns > horizon:
                return "forced", "clock_skew"
            if (int(st.st_mtime_ns), int(st.st_size)) != version:
                changed.append(rel)
        from .index_tree_policy import name_prune_reason

        for rel_dir, (old_mtime, children) in list(self._dir_baseline.items()):
            d = os.path.join(root, rel_dir) if rel_dir else root
            try:
                st = os.stat(d)
            except FileNotFoundError:
                self._dir_baseline.pop(rel_dir, None)  # its files show up as missing above
                continue
            except OSError:
                return "forced", "stat_error"
            if int(st.st_mtime_ns) == old_mtime:
                continue
            try:
                entries = list(os.scandir(d))
            except OSError:
                return "forced", "stat_error"
            for e in entries:
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    return "forced", "stat_error"
                if is_dir:
                    if name_prune_reason(e.name) is None and e.name not in children:
                        return "forced", "new_directory"
                elif rel not in known:
                    changed.append(rel)
            self._dir_baseline[rel_dir] = (int(st.st_mtime_ns), children)
        return "ok", changed

    def _record_baseline(self) -> None:
        """Directory baseline after a full walk: every directory holding a
        known path (and its ancestors) -> (mtime, child directory names)."""
        try:
            known = self._known_versions()
        except Exception:  # noqa: BLE001
            self._dir_baseline = {}
            return
        dirs = {""}
        for rel in known:
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        root = str(self.project_root)
        baseline: dict[str, tuple[int, frozenset[str]]] = {}
        for rel_dir in dirs:
            d = os.path.join(root, rel_dir) if rel_dir else root
            try:
                st = os.stat(d)
                children = frozenset(e.name for e in os.scandir(d) if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
            baseline[rel_dir] = (int(st.st_mtime_ns), children)
        self._dir_baseline = baseline

    # ── reconcile dispatch ──
    def _run(self, trigger: str) -> dict:
        # A stopped sitter never starts a background reconcile: a startup,
        # poll or debounce run that was queued before stop() lands here after
        # it (an explicit reconcile_now still runs).
        if trigger != "manual" and self._stop.is_set():
            return {"ok": False, "skipped": "stopped", "trigger": trigger}
        with self._run_lock:
            res = self._run_locked(trigger)
        self._last_reconcile_at = time.time()
        self._last_result = res
        return res

    def _run_locked(self, trigger: str) -> dict:
        root, hub = self.project_root, self.hub
        if not getattr(hub.code, "DNT_VERDICT_SUPPORTED", False):
            return reconcile(root, hub, trigger=trigger)  # no delta support: the old full pass
        now = time.time()
        full = trigger in ("startup", "sweep", "integrity", "manual")
        integrity = trigger == "integrity"
        forced: str | None = None
        if not full:
            if self._on_unreliable_filesystem():
                forced = "network_drive"
            elif (
                trigger == "poll"
                and self.enable_watchdog
                and self._observer is not None
                and not self._observer.is_alive()
            ):
                forced = "watcher_lost"
                self._observer = None
                self._try_start_watchdog()
            elif trigger == "poll" and (
                self._last_integrity_at is not None and now - self._last_integrity_at >= self.integrity_seconds
            ):
                forced, integrity = "integrity_sweep", True
            elif trigger == "poll" and (self._last_full_at is None or now - self._last_full_at >= self.sweep_seconds):
                forced = "safety_sweep"
            if forced is None:
                cov = coverage_state(root)
                if cov is None or not cov["valid"]:
                    forced = (cov or {}).get("reason") or "no_coverage"
            if forced is None and trigger == "poll":
                kind, value = self._stat_scan()
                if kind == "forced":
                    forced = str(value)
                elif value:
                    mark_paths_pending(root, value)
            if forced is not None:
                invalidate_coverage(root, forced)
        if full or forced is not None:
            res = reconcile(root, hub, trigger=trigger, integrity=integrity)
            if forced is not None:
                res["forced_by"] = forced
            if res.get("ok") and res.get("coverage_validated"):
                self._record_baseline()
                self._last_full_at = time.time()
                if integrity or self._last_integrity_at is None:
                    self._last_integrity_at = self._last_full_at
            return res
        rels = list(pending_paths(root))
        if not rels:
            _record_reconcile_time(root)
            _audit_reconcile(root, hub, tracked=0, synced=0, state="ready", trigger=trigger)
            return {
                "ok": True,
                "mode": "stat" if trigger == "poll" else "scoped",
                "paths": 0,
                "known_stale": is_index_known_stale(root),
                "trigger": trigger,
            }
        return reconcile_paths(root, hub, rels, trigger=trigger)

    def reconcile_now(self, trigger: str = "manual") -> dict:
        return self._run(trigger)

    def status(self) -> dict:
        return {
            "running": self._started,
            "backends": list(self._backends),
            "poll_seconds": self.poll_seconds,
            "debounce_ms": self.debounce_ms,
            "last_reconcile_at": self._last_reconcile_at,
            "last_result": self._last_result,
            "known_stale": is_index_known_stale(self.project_root),
            "events_excluded": excluded_event_counts(),
            # Honest freshness window: distinguishes fresh from poll-window-risk.
            "freshness": freshness_window(self.project_root),
        }


# ── registry + lifecycle helpers ─────────────────────────────────────────
_INSTANCES: dict[str, ProjectIndexSitter] = {}
_INSTANCES_LOCK = threading.Lock()


def _sitter_config(project_root: Path) -> tuple[bool, int, int]:
    try:
        from .config import get_setting

        enabled = bool(
            get_setting(
                "observability.project_index_sitter",
                project_root=project_root,
                default=True,
            ),
        )
        poll = int(
            get_setting(
                "observability.index_sitter_poll_seconds",
                project_root=project_root,
                default=30,
            )
            or 30,
        )
        debounce = int(
            get_setting(
                "observability.watch_user_drops_debounce_ms",
                project_root=project_root,
                default=500,
            )
            or 500,
        )
    except Exception:
        enabled, poll, debounce = True, 30, 500
    return enabled, poll, debounce


def ensure_index_sitter(project_root: Path, hub: Any) -> bool:
    """Start the sitter for ``project_root`` when enabled. Idempotent. Started
    on MCP attach / managed session / dashboard. Returns whether it's running.
    """
    enabled, poll, debounce = _sitter_config(project_root)
    if not enabled:
        stop_index_sitter(project_root)
        return False
    key = _key(project_root)
    with _INSTANCES_LOCK:
        existing = _INSTANCES.get(key)
        if existing is not None and existing._started:
            return True
        sitter = ProjectIndexSitter(project_root, hub, poll_seconds=poll, debounce_ms=debounce)
        if sitter.start():
            _INSTANCES[key] = sitter
            return True
    return False


def stop_index_sitter(project_root: Path) -> None:
    key = _key(project_root)
    with _INSTANCES_LOCK:
        sitter = _INSTANCES.pop(key, None)
    if sitter is not None:
        sitter.stop()


def stop_all_index_sitters() -> None:
    with _INSTANCES_LOCK:
        instances = list(_INSTANCES.values())
        _INSTANCES.clear()
    for sitter in instances:
        sitter.stop()


def index_sitter_status(project_root: Path) -> dict:
    """Lifecycle/status for settings/catalog/dashboard visibility."""
    enabled, poll, debounce = _sitter_config(project_root)
    key = _key(project_root)
    with _INSTANCES_LOCK:
        sitter = _INSTANCES.get(key)
    if sitter is not None:
        st = sitter.status()
        st["enabled"] = enabled
        return st
    return {
        "running": False,
        "enabled": enabled,
        "backends": [],
        "poll_seconds": poll,
        "known_stale": is_index_known_stale(project_root),
        "last_reconcile_at": None,
        "last_result": {},
        "freshness": freshness_window(project_root),
    }
