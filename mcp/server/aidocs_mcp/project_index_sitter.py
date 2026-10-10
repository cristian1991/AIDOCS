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


def mark_index_known_stale(project_root: Path | str) -> None:
    with _KS_LOCK:
        _KNOWN_STALE.add(_key(project_root))


def clear_index_known_stale(project_root: Path | str) -> None:
    with _KS_LOCK:
        _KNOWN_STALE.discard(_key(project_root))


def is_index_known_stale(project_root: Path | str) -> bool:
    """Cheap in-process check: has an external change been observed but not yet
    reconciled? Read/discovery tools consult this so they never silently serve
    a KNOWN-stale index (no per-read filesystem walk needed).
    """
    with _KS_LOCK:
        return _key(project_root) in _KNOWN_STALE


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
        return {"state": "known_stale", "trustworthy": False}
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
) -> dict:
    """Always-truthful full reconcile. ``sync_code_files`` already runs
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

        with request_config_scope():
            synced = int(hub.code.sync_code_files(pr) or 0)
    except Exception as exc:
        # FAIL-TRUTHFUL: leave known-stale set so reads keep warning.
        mark_index_known_stale(pr)
        return {"ok": False, "reason": repr(exc), "known_stale": True, "trigger": trigger}
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
    if state in ("ready", "empty"):
        clear_index_known_stale(pr)
    else:
        mark_index_known_stale(pr)
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
        "trigger": trigger,
    }


class ProjectIndexSitter:
    """Per-project hybrid sitter: watchdog acceleration (if available) over an
    always-on polling reconciler. Lifetime: process-long; idempotent start.
    """

    def __init__(
        self,
        project_root: Path,
        hub: Any,
        *,
        poll_seconds: int = 30,
        debounce_ms: int = 500,
        enable_watchdog: bool = True,
    ) -> None:
        self.project_root = Path(project_root)
        self.hub = hub
        self.poll_seconds = max(2, int(poll_seconds))
        self.debounce_ms = max(50, int(debounce_ms))
        self.enable_watchdog = bool(enable_watchdog)
        self._observer: Any = None
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._flush_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._started = False
        self._backends: list[str] = []
        self._last_reconcile_at: float | None = None
        self._last_result: dict = {}

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> bool:
        if self._started:
            return True
        self._stop.clear()
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
        threading.Thread(
            target=lambda: self._run("startup"),
            daemon=True,
            name=f"index-sitter-startup:{self.project_root.name}",
        ).start()
        return True

    def stop(self) -> None:
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
            self._flush_timer = None

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
                if getattr(event, "is_directory", False):
                    return
                path = getattr(event, "dest_path", None) or event.src_path
                if event_relevant(sitter.project_root, path):
                    sitter._on_change()

        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), str(self.project_root), recursive=True)
            self._observer.daemon = True
            self._observer.start()
            return True
        except Exception:
            self._observer = None
            return False

    # ── reconcile paths ──────────────────────────────────────────────
    def _on_change(self) -> None:
        """Accelerated path: a relevant external change was observed. Mark
        known-stale immediately (reads must warn now) and debounce a reconcile
        so a burst batches into one sync.
        """
        mark_index_known_stale(self.project_root)
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

    def _run(self, trigger: str) -> dict:
        res = reconcile(self.project_root, self.hub, trigger=trigger)
        self._last_reconcile_at = time.time()
        self._last_result = res
        return res

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
