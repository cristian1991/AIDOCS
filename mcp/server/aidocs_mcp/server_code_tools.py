from __future__ import annotations

import contextvars
import re
from pathlib import Path
from typing import Any, Literal

from . import mcp_server_runtime_helpers as _rh
from .language_descriptors import (
    descriptor_match_summary,
    descriptor_registry_summary,
    descriptor_semantics_summary,
    validate_language_descriptors,
)
from .config import resolve_include_tests
from .deslop_import_manager import manage_python_extraction_imports
from .mcp_server_runtime_helpers import _env_project_root, resolve_project_root
from .tool_display import renders_as

# Max wall-clock the best-effort MemPalace ingest may take inside
# memory_capture before it is abandoned. The canonical sqlite memory row is
# already durable at that point; the palace projection is a discoverability
# nicety. mempalace is a 3rd-party engine that can block (embedding / KG IO)
# with no internal timeout — an un-timeboxed synchronous call here wedges the
# whole memory_capture tool (and, downstream, the MCP connection). 5s is far
# beyond a healthy single-entry ingest while still bounding a hang.
PALACE_INGEST_TIMEOUT_S = 5.0

# #481 (War KK): inline char budget for ai_get_symbol_snippet before the
# in-band overflow message points at to_file= (raw UTF-8 sidecar). ~40k
# chars ≈ 10k tokens — comfortably under host tool-output caps.
SNIPPET_INLINE_CHAR_BUDGET = 40_000

# ai_bundle(mode="file", include_content=N) inline-content caps.
INLINE_CONTENT_MAX_LINES = 400
INLINE_CONTENT_CHAR_BUDGET = 60_000


def _resolve_snippet_sidecar_dest(
    root: Path, to_file: str
) -> tuple[Path | None, str | None]:
    """Validate the raw-sidecar destination for ai_get_symbol_snippet.

    Allowed: the system temp dir (agent scratchpads live there) or the
    project's .MEMORY/ tree (session artifacts). Anything else — and
    especially project source files — is refused. Returns (dest, None)
    on success or (None, reason) on refusal.
    """
    import tempfile

    p = Path(to_file)
    if not p.is_absolute():
        p = (root / ".MEMORY" / "sessions" / p).resolve()
    else:
        p = p.resolve()
    root_res = root.resolve()
    memory_root = (root_res / ".MEMORY").resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        in_memory = p.is_relative_to(memory_root)
        in_project = p.is_relative_to(root_res)
        in_temp = p.is_relative_to(temp_root)
    except Exception:
        return None, f"to_file path could not be validated: {to_file}"
    if in_project and not in_memory:
        return None, (
            f"to_file refused: '{to_file}' is inside the project source "
            "tree. The raw sidecar may only write to your scratchpad "
            "(system temp) or the project's .MEMORY/sessions/ dir."
        )
    if not (in_memory or in_temp):
        return None, (
            f"to_file refused: '{to_file}' is outside the allowed "
            "destinations (system temp scratchpad or the project's "
            ".MEMORY/ tree)."
        )
    return p, None


def _run_timeboxed(fn, timeout_s: float):
    """Run a zero-arg callable in a daemon thread, returning
    (result, timed_out). On timeout the worker is abandoned (daemon — never
    blocks process exit) and (None, True) is returned; on exception inside
    fn, returns (None, False). Used to bound best-effort side calls (e.g. the
    MemPalace ingest) so a hanging dependency can't wedge the caller.
    """
    import threading

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
        except Exception:  # noqa: BLE001 — best-effort; caller degrades
            box["result"] = None

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return None, True
    return box.get("result"), False


# Async palace projection (2026-06-30): the mempalace ingest ends in a ChromaDB
# upsert that COLD-LOADS a sentence-transformer on the first call after each
# process start, blowing past the old 5s inline timebox — so every memory_capture
# after an MCP reconnect timed out and abandoned a daemon thread (which then piled
# up and could race ChromaDB). Decouple it: capture ENQUEUES the ingest and one
# background worker drains the queue, so the model warms exactly once and stays
# warm, ingests never race, and capture latency is never tied to the cold model.
# The canonical sqlite row is already durable, so a queued/dropped projection only
# lags palace search until the next ingest — it never loses memory.
import contextvars as _contextvars_mod  # noqa: E402
import logging as _logging_mod  # noqa: E402
import queue as _queue_mod  # noqa: E402
import threading as _threading_mod  # noqa: E402
import time as _time_mod  # noqa: E402

_palace_logger = _logging_mod.getLogger(__name__)

def _memory_entry_full_text(hub: Any, project_root: Path, row: Any) -> str:
    """Full body behind a memory-search row — search snippets are clipped
    windows that drop edge tokens, under-scoring true duplicates in the fit
    check (containment 0.667 on a verbatim re-add, found live). Reads via
    the CANONICAL store (no-file-layer seal 2026-06: markdown under
    /.MEMORY is a virtual indexed path, not a runtime file). Best-effort:
    any failure returns ''."""
    try:
        rel = (row or {}).get("path") if isinstance(row, dict) else ""
        if not rel:
            return ""
        payload = hub.memory.read_memory(project_root, [str(rel)])
        if isinstance(payload, dict):
            return str(payload.get(str(rel)) or "") or " ".join(
                str(v) for v in payload.values() if isinstance(v, str)
            )
        return ""
    except Exception:
        return ""


def _kingdom_rows_for_promotion(project_root: Path) -> list[dict[str, Any]]:
    """Active kingdom memory rows with their content hashes — the archive
    side of the promote-by-reference trusted path (#451). Reads the
    canonical sqlite memory_index directly (read-only, best-effort)."""
    import hashlib as _hl
    import sqlite3 as _sq

    from ._sqlite_connect import connect as _canonical_connect
    from .memory_sqlite_store import _db_path as _mem_db_path

    db = _mem_db_path(project_root)
    if not db.is_file():
        return []
    try:
        # read_only=True is the TRUTHFUL mode for this path — the docstring
        # already promised "read-only, best-effort" and now sqlite enforces it
        # rather than a comment. Durability is moot for a reader; what it gains
        # is the three PER-CONNECTION pragmas it had none of (synchronous,
        # busy_timeout, foreign_keys) plus the guarantee that a missing file is
        # not MATERIALISED as an empty one. row_factory left at the helper
        # default is wrong here: the unpack below is positional, so keep tuples.
        conn = _canonical_connect(str(db), read_only=True, row_factory=False)
        try:
            rows = conn.execute(
                "SELECT path, kind, content, COALESCE(checksum, ''), "
                "COALESCE(created_at, '') FROM memory_index "
                "WHERE COALESCE(status, 'active') = 'active' "
                "AND (superseded_by IS NULL OR superseded_by = '')",
            ).fetchall()
        finally:
            conn.close()
    except _sq.Error:
        return []
    out: list[dict[str, Any]] = []
    for path, kind, content, checksum, created_at in rows:
        text = str(content or "")
        out.append(
            {
                "path": str(path),
                "kind": str(kind or ""),
                "content": text,
                "checksum": str(checksum or "").lower(),
                "content_sha256": _hl.sha256(text.encode("utf-8")).hexdigest(),
                "created_at": str(created_at or ""),
            },
        )
    return out


def _resolve_kingdom_text_by_hash(
    project_root: Path, wanted: str,
) -> tuple[str, str, str] | None:
    """Resolve archived kingdom text by content hash → (text, kind, path).

    Matches, in order: the row's stored checksum, the sha256 of the full row
    content, then the sha256 of any single bullet line (memory_capture's
    captured_hash of a consolidated bullet). Returns None for an unknown
    hash — by-hash promotion can NEVER introduce text that is not already
    in the archive (verbatim-by-construction, #451)."""
    import hashlib as _hl

    wanted = (wanted or "").strip().lower()
    if not wanted:
        return None
    rows = _kingdom_rows_for_promotion(project_root)
    for r in rows:
        if wanted in (r["checksum"], r["content_sha256"]):
            return r["content"], r["kind"], r["path"]
    for r in rows:
        for line in r["content"].splitlines():
            t = line.strip()
            if t.startswith("- "):
                t = t[2:].strip()
            if not t:
                continue
            if _hl.sha256(t.encode("utf-8")).hexdigest() == wanted:
                return t, r["kind"], r["path"]
    return None


def _empire_candidate_rows(project_root: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Pending empire-candidate kingdom rows (#451): empire-worthy content
    not yet sealed as law. Terse rows — hash, kind, snippet<=120ch,
    captured_at — promotable via memory_promote(content_hash=...)."""
    import hashlib as _hl

    from .memory_fit import is_empire_candidate

    sealed: set[str] = set()
    try:
        from .global_law_store import list_active_global_law

        for law in list_active_global_law():
            law_text = str(law.get("content") or "")
            sealed.add(_hl.sha256(law_text.encode("utf-8")).hexdigest())
    except Exception:
        pass
    out: list[dict[str, Any]] = []
    for r in _kingdom_rows_for_promotion(project_root):
        worthy, _why = is_empire_candidate(r["kind"], r["content"])
        if not worthy:
            continue
        if r["content_sha256"] in sealed:
            continue  # already sealed as empire law — not pending
        out.append(
            {
                "hash": r["checksum"] or r["content_sha256"],
                "kind": r["kind"],
                "snippet": r["content"][:120],
                "captured_at": r["created_at"],
            },
        )
        if len(out) >= limit:
            break
    return out


_PALACE_INGEST_QUEUE: "_queue_mod.Queue" = _queue_mod.Queue(maxsize=2048)
_PALACE_WORKER_LOCK = _threading_mod.Lock()
_PALACE_WORKER_STARTED = False

# Queue-health bookkeeping (2026-07-17 silent-stall fix): pending enqueue
# timestamps + the last worker error, so a stalled/crashing projection is
# LOUD (capture reports palace_status='stale', search appends a health row)
# instead of 'queued' forever.
_PALACE_STALE_AFTER_S = 300.0
_PALACE_PENDING: dict[int, float] = {}
_PALACE_PENDING_LOCK = _threading_mod.Lock()
_PALACE_SEQ = 0
_PALACE_LAST_INGEST_ERROR: dict[str, Any] | None = None


def _palace_queue_health() -> dict[str, Any]:
    """Observable health of the async palace-projection queue: depth, age of
    the oldest still-pending item, staleness verdict, and the last worker
    error (if any). Cheap — two lock-guarded dict reads, no queue peeking."""
    now = _time_mod.monotonic()
    with _PALACE_PENDING_LOCK:
        ages = [now - t for t in _PALACE_PENDING.values()]
    oldest = max(ages) if ages else 0.0
    out: dict[str, Any] = {
        "depth": len(ages),
        "oldest_age_s": round(oldest, 1),
        "stale": oldest > _PALACE_STALE_AFTER_S,
    }
    err = _PALACE_LAST_INGEST_ERROR
    if err is not None:
        out["last_error"] = err["error"]
        out["last_error_age_s"] = round(now - err["at"], 1)
    return out


def _palace_worker_loop() -> None:
    global _PALACE_LAST_INGEST_ERROR
    while True:
        seq, ctx, fn = _PALACE_INGEST_QUEUE.get()
        try:
            # Run under a COPY of the enqueuing call's contextvars context.
            # The per-request project-root override travels with the item, so
            # resolve_project_root() inside the ingest resolves to the
            # CAPTURING request's project — a fresh thread context raised
            # NoAidocsProjectError under mcp.multitenant_strict and every
            # projection died silently (2026-07-17 stall).
            ctx.run(fn)
        except Exception as exc:  # noqa: BLE001 — projection must not kill the worker
            _PALACE_LAST_INGEST_ERROR = {
                "at": _time_mod.monotonic(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            _palace_logger.exception(
                "palace ingest failed — queued projection dropped "
                "(canonical sqlite row remains durable)"
            )
        finally:
            with _PALACE_PENDING_LOCK:
                _PALACE_PENDING.pop(seq, None)
            _PALACE_INGEST_QUEUE.task_done()


def _submit_palace_ingest(fn) -> bool:
    """Hand a zero-arg palace ingest to the single background worker. Returns
    True if queued, False if the queue is saturated. Never blocks the caller —
    the canonical sqlite row is already durable, so a dropped enqueue only lags
    palace search (a bootstrap heal re-ingests). The caller's contextvars
    context is captured with the item so the worker resolves the SAME project
    the capture did (multitenant-strict safe)."""
    global _PALACE_WORKER_STARTED, _PALACE_SEQ
    if not _PALACE_WORKER_STARTED:
        with _PALACE_WORKER_LOCK:
            if not _PALACE_WORKER_STARTED:
                _threading_mod.Thread(
                    target=_palace_worker_loop,
                    name="aidocs-palace-ingest",
                    daemon=True,
                ).start()
                _PALACE_WORKER_STARTED = True
    ctx = _contextvars_mod.copy_context()
    # Register pending BEFORE the put so the worker's finally-pop can never
    # race ahead of registration (a leaked entry would read as stale forever).
    with _PALACE_PENDING_LOCK:
        _PALACE_SEQ += 1
        seq = _PALACE_SEQ
        _PALACE_PENDING[seq] = _time_mod.monotonic()
    try:
        _PALACE_INGEST_QUEUE.put_nowait((seq, ctx, fn))
        return True
    except _queue_mod.Full:
        with _PALACE_PENDING_LOCK:
            _PALACE_PENDING.pop(seq, None)
        return False




def _project_palace_capture_and_recover(
    project_root: Path,
    palace_service,
    target_rel: str,
    *,
    hub,
    runtime,
    drawer_reader=None,
    recovery_limit: int = 25,
) -> dict[str, Any]:
    """Land the current capture, then opportunistically heal older staged rows.

    Runs only on the existing background palace worker. The current row must
    land with a read-back receipt before recovery begins; a dead/cold palace
    therefore does not spin through the whole staged queue. When the palace is
    healthy, one future capture heals prior verification failures and queue
    saturation without adding another daemon or touching capture latency.
    """
    from .memory_body_staging_store import drain_staged, project_staged_entry
    from .mcp_server_runtime_helpers import with_target_project_root
    from .palace_hub_extension import build_palace_context

    with with_target_project_root(project_root):
        palace_ctx = build_palace_context(
            hub,
            runtime,
            tool_name="memory_capture.palace_ingest",
        )
        current = project_staged_entry(
            project_root,
            palace_service,
            target_rel,
            hub_ctx=palace_ctx,
            drawer_reader=drawer_reader,
        )
        if not current.get("landed"):
            return current
        recovery = drain_staged(
            project_root,
            palace_service,
            hub_ctx=palace_ctx,
            drawer_reader=drawer_reader,
            limit=max(1, int(recovery_limit)),
        )
        if recovery.get("scanned"):
            current["staged_recovery"] = recovery
        return current
def _filter_symbol_info_by_path(
    result: Any,
    path: str,
) -> Any:
    """Post-filter an ai_get_symbol_info result by path.

    Most kinds (signature, signatures, constructor, constructors, enum,
    api) return {"matches": [...]} where each match has a "path" key.
    properties returns a single record with a "path" key. When `path`
    is empty → passthrough.
    """
    if not path or not isinstance(result, dict):
        return result
    norm = path.replace("\\", "/").lstrip("/")
    if isinstance(result.get("matches"), list):
        filtered = [
            m
            for m in result["matches"]
            if isinstance(m, dict)
            and str(m.get("path") or "").replace("\\", "/").lstrip("/") == norm
        ]
        return {**result, "matches": filtered}
    own_path = str(result.get("path") or "").replace("\\", "/").lstrip("/")
    if own_path and own_path != norm:
        return {
            "entity_name": result.get("entity_name"),
            "properties": [],
            "note": f"no match for path={path!r}",
        }
    return result


# --- #482 find-truthfulness: no ambiguous emptiness -------------------------
# Unified empty-reason vocabulary. Every empty ai_find / ai_text_search
# result names its reason with one of these + a next_action hint:
#   no_match           — searched fine, nothing matched
#   path_not_indexed   — the path= scope has NO files in the code index
#   symbol_not_indexed — references mode: no code_outlines row for the symbol
#   no_references      — references mode: symbol indexed, zero usage lines
#   timed_out          — the sweep hit its time budget (partials ride along)
#   pattern_invalid    — the regex does not compile (never reported as empty)
EMPTY_REASONS = frozenset(
    {
        "no_match",
        "path_not_indexed",
        "symbol_not_indexed",
        "no_references",
        "timed_out",
        "pattern_invalid",
    },
)

# References-mode default budget (#482 item 1): the general 10s discovery
# default was too tight for full-repo reference sweeps on hot symbols (War
# DD hit it on _collect_notification_blocks). The ai_find shim below injects
# this as the caller timeout when none is supplied, and hands ~90% of it to
# find_references as an internal budget so partial results always return
# before the hard tool kill.
_REFERENCES_DEFAULT_TIMEOUT_S = 30
_REFERENCES_ALIAS_MODES = frozenset(
    {
        "references",
        "reference",
        "callers",
        "usages",
        "usage",
        "service_usage",
        "service_consumers",
    },
)

_FIND_BUDGET_UNSET = -1.0
_find_references_budget: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "aidocs_find_references_budget", default=_FIND_BUDGET_UNSET,
)


def _references_default_timeout(fn):
    """ai_find decorator shim (#482): references-family modes get a 30s
    default timeout (instead of the general 10s discovery default) and the
    effective budget is published on a ContextVar so the dispatch body can
    hand find_references an internal deadline that beats the hard kill.
    Runs OUTSIDE timed_discovery so the injected kwargs["timeout"] is what
    _resolve_timeout pops. A caller-supplied timeout= always wins
    (timeout=0 keeps its meaning: unlimited)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "symbols")
        if str(mode or "").strip().lower() not in _REFERENCES_ALIAS_MODES:
            return fn(*args, **kwargs)
        timeout = kwargs.get("timeout")
        if timeout is None:
            kwargs["timeout"] = _REFERENCES_DEFAULT_TIMEOUT_S
            effective = _REFERENCES_DEFAULT_TIMEOUT_S
        else:
            try:
                effective = int(timeout)
            except (TypeError, ValueError):
                effective = _REFERENCES_DEFAULT_TIMEOUT_S
        budget = effective * 0.9 if effective > 0 else None  # 0 = unlimited
        token = _find_references_budget.set(budget)
        try:
            return fn(*args, **kwargs)
        finally:
            _find_references_budget.reset(token)

    return wrapper


def _validate_regex(query: str) -> str | None:
    """Return a compile-error message for an invalid regex, else None."""
    try:
        re.compile(query)
    except re.error as exc:
        return str(exc)
    return None


def _pattern_invalid_envelope(query: str, error: str) -> dict[str, Any]:
    return {
        "total_matches": 0,
        "results": [],
        "empty_reason": "pattern_invalid",
        "error": f"invalid regex {query!r}: {error}",
        "next_action": (
            "Fix the regular expression (see error), or use mode=text / "
            "regex=False for a literal search."
        ),
    }


# ---------------------------------------------------------------------------
# ai_find payload caps (#565, ai_find slice)
# ---------------------------------------------------------------------------
# Measured 2026-07-28 against this repo BEFORE these caps existed: the
# snippet-carrying concept modes returned payloads no agent can read and that
# blow the tool token cap outright —
#   validation(session)   385,294 chars   (50 rows, ~7.7KB of `snippet` each)
#   transitions(migrate)   95,996
#   async(connect)         86,978
#   references(connect)    73,977 AT limit=8 — a single unbounded `line`
#                                  field is enough to overflow, so lowering
#                                  `limit` was NOT a usable workaround
#   policy(permission)     58,290
#   touchpoints(login)     22,802
# An overflow dumps the result to a file under /.claude/ and parsing that file
# has previously triggered run_destructive freezes (incident 2026-07-12) — the
# same failure mode the text/regex budget at #314(4) already guarded. That
# budget covered ONLY mode=text/regex/string; every structural mode was
# unbounded. These caps close that gap for the rest of ai_find.
#
# Free-text row fields are the bulk of the payload, so they are trimmed FIRST
# (bounded per row, all rows kept) and only then is a total budget applied —
# trimming preserves row COUNT, which is what a caller needs to judge breadth.
_FIND_ROW_TEXT_MAX = 400
_FIND_OUTPUT_BUDGET_CHARS = 60_000
# Row fields that carry free source text rather than a decision-bearing value.
_FIND_ROW_TEXT_FIELDS = ("snippet", "line", "raw")
# Row-list containers ai_find modes use. Four different names for one concept
# is itself a measured wart (see the mode map); this list follows reality.
_FIND_ROW_CONTAINER_KEYS = ("matches", "results", "items", "result")


def _trim_find_row_text(rows: list[Any]) -> int:
    """Bound the free-text fields on each row. Returns rows trimmed."""
    trimmed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in _FIND_ROW_TEXT_FIELDS:
            value = row.get(field)
            if isinstance(value, str) and len(value) > _FIND_ROW_TEXT_MAX:
                row[field] = value[:_FIND_ROW_TEXT_MAX] + "…[trimmed]"
                trimmed += 1
    return trimmed


def _cap_find_payload(result: Any) -> Any:
    """Bound an ai_find payload's serialized size, honestly.

    Truncation is never silent: a dict payload that loses rows reports
    `results_shown`, `results_truncated`, `total_matches` and a `next_action`
    naming how to narrow — everything a caller needs to re-run a query that
    was cut. Refusal/empty vocabulary (`empty_reason`, `error`, rule ids) is
    never touched, so the #482 no-ambiguous-emptiness contract is unaffected.
    """
    import json as _json

    if isinstance(result, list):
        # List-shaped modes have no metadata slot, so they only ever get
        # row-text trimming — dropping rows there would be a silent loss.
        _trim_find_row_text(result)
        return result
    if not isinstance(result, dict):
        return result

    key = next(
        (
            k
            for k in _FIND_ROW_CONTAINER_KEYS
            if isinstance(result.get(k), list) and result.get(k)
        ),
        None,
    )
    if key is None:
        return result
    rows = result[key]
    _trim_find_row_text(rows)

    total = len(rows)
    kept: list[Any] = []
    used = 0
    for row in rows:
        size = len(_json.dumps(row, default=str))
        if kept and used + size > _FIND_OUTPUT_BUDGET_CHARS:
            break
        kept.append(row)
        used += size
    if len(kept) < total:
        result[key] = kept
        result.setdefault("total_matches", total)
        result["results_shown"] = len(kept)
        result["results_truncated"] = True
        result["next_action"] = (
            f"Showing {len(kept)} of {total} rows — output capped to stay "
            "under the tool limit. Narrow with a tighter query, a lower "
            "`limit`, or a more specific mode."
        )
    return result


#: ai_find's query-time test lens. Deliberately NOT named include_tests:
#: that one is the INDEX-BUILD policy (what ai_index_sync writes, what
#: index.include_tests governs). This one is a per-query lens over whatever
#: the index already holds. Two layers, two names — one shared name is
#: exactly how the two get conflated.
_TESTS_LENS_VALUES = ("exclude", "include", "only")

#: File roles the lens treats as "a test".
_TEST_ROLES = ("test", "fixture")


def _no_test_match_envelope() -> dict[str, Any]:
    """tests='only' found nothing. Say which lens produced the zero."""
    return {
        "total_matches": 0,
        "results": [],
        "empty_reason": "no_test_match",
        "next_action": (
            "No test/fixture file matched under tests='only'. Drop the lens "
            "(or pass tests='include') to search production code too, or "
            "widen the query."
        ),
    }


def _test_role_paths(hub: Any, root: Path) -> set[str]:
    """Indexed paths whose role marks them as a test/fixture."""
    try:
        with hub.code.connect(root) as conn:
            marks = ", ".join("?" for _ in _TEST_ROLES)
            return {
                str(r[0]).replace("\\", "/")
                for r in conn.execute(
                    f"SELECT path FROM code_files WHERE role IN ({marks})",
                    _TEST_ROLES,
                )
            }
    except Exception:
        return set()


def _apply_tests_lens_to_rows(
    result: dict[str, Any] | list[dict[str, Any]],
    hub: Any,
    root: Path,
    *,
    keep_tests: bool,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Apply the `tests` lens to a structural mode's rows.

    Those modes never learned a test filter — they rank test files alongside
    production, which is the right DEFAULT but leaves both "find the test that
    covers X" (keep_tests=True) and "get these tests out of my way"
    (keep_tests=False) unexpressible. Filtering by file role here gives the
    lens ONE meaning in every mode instead of a text-mode-only special case.
    Never called when the lens is unset, so the default is untouched.
    """
    test_paths = _test_role_paths(hub, root)

    def wanted(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        is_test = str(row.get("path", "")).replace("\\", "/") in test_paths
        return is_test is keep_tests

    if isinstance(result, list):
        return [row for row in result if wanted(row)]
    out = dict(result)
    for key in ("results", "matches", "cluster"):
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = [row for row in rows if wanted(row)]
    # The related-tier summary is an unfiltered digest of the other side of
    # the lens; carrying it through would reintroduce exactly what the caller
    # asked to be rid of.
    out.pop("related_summary", None)
    return out


def _lens_result_is_empty(result: dict[str, Any] | list[dict[str, Any]]) -> bool:
    if isinstance(result, list):
        return not result
    return not any(
        result.get(key) for key in ("matches", "cluster", "results", "result")
    )


def _tests_excluded_envelope(suppressed: int) -> dict[str, Any]:
    """Everything this query matched was a test, and the lens removed it."""
    return {
        "total_matches": 0,
        "results": [],
        "empty_reason": "tests_excluded",
        "suppressed_test_matches": suppressed,
        "remedy": {"param": "tests", "value": "include"},
        "next_action": (
            f"{suppressed} match(es) were dropped by tests='exclude' — they "
            "are test/fixture files, not absent code. Drop the lens, or pass "
            "tests='include' to see them."
        ),
    }


def _text_empty_envelope(
    hub: Any,
    root: Path,
    *,
    scope: str | None,
    include_tests: bool,
    for_find: bool = False,
    probe_query: str | None = None,
    probe_regex: bool = False,
    probe_limit: int = 50,
) -> dict[str, Any]:
    """Empty text/regex/string result with an honest reason (#478
    secondary): a path-scoped zero distinguishes path_not_indexed from
    no_match, judged by the same predicate that scoped the search.

    An empty result that means "hidden" is the worst answer a search tool
    can give — the caller concludes the code is ABSENT (law 311bf3e6: a
    refusal or empty answer must be actionable). So when tests were
    suppressed, re-run the SAME query with the test lens open: if it WOULD
    have matched, say `tests_excluded` and name the parameter that reaches
    those files, instead of claiming "no indexed file contains this text".
    """
    reason = "no_match"
    next_action = (
        "No indexed file contains this text — widen the query, or try "
        "ai_find mode=symbols for symbol lookup."
    )
    suppressed = 0
    if not include_tests and probe_query:
        try:
            suppressed = len(
                hub.code.search_text(
                    root,
                    probe_query,
                    glob=scope,
                    regex=probe_regex,
                    limit=probe_limit,
                    only_tests=True,
                ),
            )
        except Exception:
            suppressed = 0
    if suppressed:
        remedy = (
            {"param": "tests", "value": "include"}
            if for_find
            else {"param": "include_tests", "value": True}
        )
        hint = (
            "tests='include' (or 'only' to search tests exclusively)"
            if for_find
            else "include_tests=True"
        )
        return {
            "total_matches": 0,
            "results": [],
            "empty_reason": "tests_excluded",
            "suppressed_test_matches": suppressed,
            "remedy": remedy,
            "next_action": (
                f"{suppressed} test/fixture file(s) DO contain this — they are "
                f"hidden by the current test filter, not absent. Pass "
                f"{hint} to search them."
            ),
        }
    if scope:
        counts: dict[str, int] | None
        try:
            counts = hub.code.count_files_in_scope(
                root, scope, include_tests=include_tests,
            )
        except Exception:
            counts = None
        if counts is not None:
            if counts.get("in_scope_all_roles", 0) == 0:
                reason = "path_not_indexed"
                next_action = (
                    f"No indexed files match path {scope!r} — check the path "
                    "spelling, or run ai_index_sync if the files are new."
                )
            elif counts.get("in_scope", 0) == 0:
                next_action = (
                    f"Path {scope!r} only contains test/fixture files under "
                    "the current filter — pass include_tests=True to search "
                    "them."
                )
            else:
                next_action = (
                    f"{counts.get('in_scope')} indexed file(s) under "
                    f"{scope!r} contain no match — widen the query or drop "
                    "the path filter."
                )
    out: dict[str, Any] = {
        "total_matches": 0,
        "results": [],
        "empty_reason": reason,
        "next_action": next_action,
    }
    if not for_find:
        # ai_text_search only. Two duplications were paid on every empty
        # ai_find text/regex/string result: `empty_reason_legacy` restates
        # `empty_reason` for pre-#482 consumers that only ever keyed off
        # ai_text_search, and the static `hint` told ai_find's own caller to
        # "Try ai_find" — advice that is incoherent coming FROM ai_find.
        # ai_find keeps exactly one canonical reason + one next_action.
        out["empty_reason_legacy"] = "no_text_match"
        out["hint"] = "Try ai_find for symbol search."
    return out


def _inject_index_staleness(
    result: Any,
    project_root: Path,
) -> Any:
    """Code-index staleness stamp for ai_find/ai_slop. Now delegates to the
    shared neutral helper (index_staleness) that the central call_tool wrapper
    also uses, so the signal is identical whether stamped here or centrally.
    Best-effort, shape-preserving."""
    from .index_staleness import inject_index_staleness as _shared

    return _shared(result, project_root)


def _stamp_mutation(
    d: dict[str, Any],
    *,
    applied: bool,
    source_path: str = "",
    target_path: str = "",
) -> dict[str, Any]:
    """Stamp an EXPLICIT mutation contract onto an extract helper's result so
    callers (ai_deslop_apply) never have to infer mutation from loose fields:
      mutation_applied — did the filesystem actually change?
      changed_paths    — the files touched (empty when nothing changed).
    ``applied`` must reflect the real file move (file_extract_block success),
    NOT the overall success — a reindex failure still leaves the file mutated.
    """
    d["mutation_applied"] = bool(applied)
    d["changed_paths"] = [p for p in (source_path, target_path) if p] if applied else []
    return d




def _deslop_file_snapshot(
    project_root: Path,
    source_path: str,
    target_path: str,
) -> tuple[bytes, bytes]:
    """Capture the two module bytes before extraction for import repair."""
    snapshots: list[bytes] = []
    for rel in (source_path, target_path):
        try:
            snapshots.append((project_root / rel).read_bytes())
        except OSError:
            snapshots.append(b"")
    return snapshots[0], snapshots[1]


def _deslop_manage_imports(
    project_root: Path,
    source_path: str,
    target_path: str,
    *,
    before_source: bytes,
    before_target: bytes,
) -> dict[str, Any]:
    """Run the one canonical Python import-management pass after extraction."""
    return manage_python_extraction_imports(
        project_root,
        source_path,
        target_path,
        before_source=before_source,
        before_target=before_target,
    )
_DESLOP_OPERATIONS = frozenset({"extract_block", "extract_symbol", "refactor_extract"})


def deslop_apply_guard(
    project_root: Path,
    operation: str,
    *,
    reason: str,
    source_path: str,
    target_path: str,
    symbol: str = "",
    start_line: int = 0,
    end_line: int = 0,
) -> dict[str, Any] | None:
    """Fail-closed admission guard for ai_deslop_apply (Tier-M). Returns a
    refusal dict, or None if every guard passes. Pure + project_root-scoped so
    it is unit-testable without booting the server. Enforces: known operation,
    meaningful reason (>=6 chars), exact selectors (source+target, plus a valid
    line range for extract_block or an exact symbol otherwise), and refuses
    unsafe (traversal/absolute/symlink-escape) or PROTECTED (control-authority/
    secret/sentinel) source OR target paths.
    """
    op = (operation or "").strip().lower()
    if op not in _DESLOP_OPERATIONS:
        return {
            "ok": False,
            "blocked_by": "unknown_operation",
            "error": f"unknown operation {operation!r}",
            "operations": sorted(_DESLOP_OPERATIONS),
        }
    if len((reason or "").strip()) < 6:
        return {
            "ok": False,
            "blocked_by": "reason_required",
            "error": "a meaningful reason (>=6 chars) is required to apply",
        }
    if not source_path or not target_path:
        return {
            "ok": False,
            "blocked_by": "selectors_required",
            "error": "source_path and target_path are both required",
        }
    if op == "extract_block":
        if start_line <= 0 or end_line <= 0 or end_line < start_line:
            return {
                "ok": False,
                "blocked_by": "selectors_required",
                "error": "extract_block needs a valid start_line<=end_line",
            }
    elif not symbol:
        return {
            "ok": False,
            "blocked_by": "selectors_required",
            "error": f"{op} needs an exact symbol",
        }
    from .checkpoint_service import safe_relpath
    from .governed_deletion import CAT_PROTECTED, classify_deletion

    rels: list[str] = []
    for p in (source_path, target_path):
        rel = safe_relpath(project_root, p)
        if rel is None:
            return {
                "ok": False,
                "blocked_by": "unsafe_path",
                "error": f"{p!r}: unsafe path rejected",
            }
        cls, why = classify_deletion(project_root, p)
        if cls == CAT_PROTECTED:
            return {"ok": False, "blocked_by": "protected_path", "error": f"{p}: {why}"}
        rels.append(rel)
    # Source/target COLLISION: extracting a file into ITSELF would corrupt or
    # lose data (extract lines, append to the same file, then remove the
    # originals). casefold-compare so a case-only difference (src/a.py vs
    # src/A.py — the SAME file on a case-insensitive FS) is also refused.
    if rels[0].casefold() == rels[1].casefold():
        return {
            "ok": False,
            "blocked_by": "source_target_collision",
            "error": (
                "source_path and target_path resolve to the same file "
                f"({rels[0]!r}); extracting a file into itself is refused"
            ),
        }
    return None


def unresolved_python_names(source: str) -> set[str]:
    """Names USED but never DEFINED anywhere in a Python module — the cheap
    fail-closed detector for a semantically broken extraction (#189 Defect A:
    a moved body whose imports were left behind, or a source still calling
    the symbol that was just removed).

    Deliberately over-approximates definedness (any Store/def/import/arg
    anywhere counts) so it can under-report but essentially never
    false-positives — and the caller compares the DELTA vs the pre-mutation
    version, so pre-existing quirks never block. Returns {"<syntax-error>"}
    when the module does not parse (a broken parse is itself a refusal
    signal), never raises.
    """
    import ast as _ast
    import builtins as _builtins

    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return {"<syntax-error>"}
    defined: set[str] = set(dir(_builtins)) | {
        "__name__",
        "__file__",
        "__doc__",
        "__package__",
        "__spec__",
        "__loader__",
        "__builtins__",
        "__debug__",
    }
    used: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name):
            if isinstance(node.ctx, _ast.Load):
                used.add(node.id)
            else:  # Store / Del — assignment, loop/with/except targets, etc.
                defined.add(node.id)
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, _ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (_ast.Global, _ast.Nonlocal)):
            defined.update(node.names)
        elif isinstance(node, _ast.ExceptHandler) and node.name:
            defined.add(node.name)
    return used - defined


def deslop_apply_execute(
    project_root: Path,
    operation: str,
    *,
    reason: str,
    source_path: str,
    target_path: str,
    symbol: str = "",
    kind: str | None = None,
    start_line: int = 0,
    end_line: int = 0,
    target_position: str = "append",
    target_line: int | None = None,
    remove_from_source: bool = False,
    dry_run: bool = True,
    extract_block: Any,
    extract_symbol: Any,
    refactor_extract: Any,
    record_event: Any,
    preview_deps: Any = None,
    symbol_range: Any = None,
    rollback_reindex: Any = None,
) -> dict[str, Any]:
    """Doctrine-routed orchestration for ai_deslop_apply, with every dependency
    injected so the mutation law is unit-testable without the MCP server.

    ``record_event(event_kind, status, payload)`` records an audit event and MAY
    raise; the law is: AUDIT INTENT before any mutation (fail-closed → refuse on
    raise, nothing mutated), then checkpoint (with created_paths so rollback
    deletes creations), then mutate via the injected extract_* callable, then
    AUDIT RESULT (degraded-tolerant: the mutation stands, reported audit_degraded
    on raise). Dry-run is the default and mutates nothing.
    """
    import difflib

    from .checkpoint_service import CheckpointService

    op = operation.strip().lower()
    refusal = deslop_apply_guard(
        project_root,
        op,
        reason=reason,
        source_path=source_path,
        target_path=target_path,
        symbol=symbol,
        start_line=start_line,
        end_line=end_line,
    )
    if refusal is not None:
        return refusal

    if dry_run:
        plan: dict[str, Any] = {
            "operation": op,
            "source_path": source_path,
            "target_path": target_path,
            "remove_from_source": remove_from_source,
        }
        try:
            if op == "extract_block":
                if preview_deps is not None:
                    plan["deps_preview"] = preview_deps(source_path, start_line, end_line)
                plan["range"] = {"start_line": start_line, "end_line": end_line}
            elif symbol_range is not None:
                plan["symbol_range"] = symbol_range(source_path, symbol, kind=kind)
        except Exception as exc:  # noqa: BLE001
            plan["preview_error"] = repr(exc)
        return {
            "ok": True,
            "dry_run": True,
            "plan": plan,
            "would_checkpoint": [
                p for p in (source_path, target_path) if (project_root / p).is_file()
            ],
            "note": (
                "PLAN ONLY — nothing changed. Re-call with dry_run=false "
                "to apply (a checkpoint is taken first)."
            ),
        }

    # ── APPLY ──
    target_exists = (project_root / target_path).is_file()
    cp_paths = [source_path] + ([target_path] if target_exists else [])
    created_paths = [] if target_exists else [target_path]
    before: dict[str, bytes] = {}
    for rel in cp_paths:
        try:
            before[rel] = (project_root / rel).read_bytes()
        except OSError:
            before[rel] = b""

    # AUDIT INTENT — FAIL-CLOSED: no unaudited mutation ever happens.
    try:
        record_event(
            "deslop_apply_intent",
            "intent",
            {
                "operation": op,
                "source": source_path,
                "target": target_path,
                "reason": reason,
                "remove_from_source": remove_from_source,
                "created_paths": created_paths,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "blocked_by": "audit_intent_failed",
            "error": f"refusing to mutate: intent audit failed ({exc!r})",
        }

    cp = CheckpointService(project_root).create(
        cp_paths,
        reason=f"ai_deslop_apply:{op}: {reason}",
        provenance={"kind": "deslop", "operation": op},
        created_paths=created_paths,
    )
    if not cp.ok:
        return {
            "ok": False,
            "blocked_by": "checkpoint_failed",
            "error": cp.reason,
            "note": "refusing to mutate without a secured restore point",
        }

    if op == "extract_block":
        result = extract_block(
            source_path,
            start_line,
            end_line,
            target_path,
            target_position=target_position,
            target_line=target_line,
            remove_from_source=remove_from_source,
        )
    elif op == "extract_symbol":
        result = extract_symbol(
            source_path,
            symbol,
            target_path,
            kind=kind,
            target_position=target_position,
            remove_from_source=remove_from_source,
        )
    else:
        result = refactor_extract(source_path, symbol, target_path, kind=kind)

    # EXPLICIT mutation contract from the extract helper — never inferred from
    # loose fields. mutation_applied = the filesystem changed; reindex_ok =
    # clean success (False ⟹ mutated-but-index-stale).
    reindex_ok = bool(result.get("success"))
    applied = bool(result.get("mutation_applied"))
    changed_paths = list(result.get("changed_paths") or [])
    diffs: dict[str, str] = {}
    for rel in dict.fromkeys(cp_paths + created_paths + changed_paths):
        try:
            after = (project_root / rel).read_bytes()
        except OSError:
            after = b""
        if after != before.get(rel, b""):
            d = difflib.unified_diff(
                before.get(rel, b"").decode("utf-8", "replace").splitlines(),
                after.decode("utf-8", "replace").splitlines(),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            diffs[rel] = "\n".join(list(d)[:400])

    # ── FAIL-CLOSED semantic guard (#189 Defect A, 2026-07-03) ──────────────
    def _rollback_mutation() -> tuple[list[str], dict[str, Any]]:
        errors: list[str] = []
        for rel, data in before.items():
            try:
                (project_root / rel).write_bytes(data)
            except OSError as exc:
                errors.append(f"restore {rel}: {exc}")
        for rel in created_paths:
            try:
                (project_root / rel).unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"remove {rel}: {exc}")

        index_results: dict[str, Any] = {}
        if rollback_reindex is not None:
            rollback_paths = list(
                dict.fromkeys(cp_paths + created_paths + changed_paths)
            )
            try:
                raw_results = rollback_reindex(rollback_paths)
                if isinstance(raw_results, dict):
                    index_results = raw_results
                else:
                    errors.append("rollback reindex returned a non-dict result")
            except Exception as exc:  # noqa: BLE001 — report degraded rollback
                errors.append(f"rollback reindex failed: {exc}")
            for rel, status in index_results.items():
                if not isinstance(status, dict) or not status.get("ok"):
                    errors.append(f"rollback reindex {rel}: {status}")
        return errors, index_results

    if applied and result.get("rollback_required"):
        rollback_errors, rollback_index = _rollback_mutation()
        blocked_by = str(result.get("blocked_by") or "import_management_failed")
        try:
            record_event(
                "deslop_apply_result",
                "rollback_degraded" if rollback_errors else "rolled_back",
                {
                    "operation": op,
                    "source": source_path,
                    "target": target_path,
                    "reason": reason,
                    "checkpoint_id": cp.checkpoint_id,
                    "blocked_by": blocked_by,
                    "rollback_errors": rollback_errors,
                    "rollback_index": rollback_index,
                    "import_management": result.get("import_management"),
                },
            )
        except Exception:  # noqa: BLE001 — refusal stands either way
            pass
        return {
            "ok": False,
            "dry_run": False,
            "blocked_by": blocked_by,
            "rolled_back": not rollback_errors,
            "rollback_errors": rollback_errors,
            "rollback_index": rollback_index,
            "checkpoint_id": cp.checkpoint_id,
            "error": str(result.get("error") or "import management failed"),
            "result": result,
            "diff": diffs,
        }

    # The first live mutation proved the extract path can leave a target using
    # names it never imports or a source still calling a removed symbol. Scan
    # every changed Python file for NEW unresolved names vs its pre-mutation
    # version and restore the checkpoint bytes on any delta.
    if applied:
        new_unresolved: dict[str, list[str]] = {}
        for rel in dict.fromkeys(cp_paths + created_paths + changed_paths):
            if not rel.endswith(".py"):
                continue
            try:
                after_text = (project_root / rel).read_text(encoding="utf-8")
            except OSError:
                continue
            before_text = before.get(rel, b"").decode("utf-8", "replace")
            delta = unresolved_python_names(after_text) - unresolved_python_names(before_text)
            if delta:
                new_unresolved[rel] = sorted(delta)
        if new_unresolved:
            rollback_errors, rollback_index = _rollback_mutation()
            try:
                record_event(
                    "deslop_apply_result",
                    "rollback_degraded" if rollback_errors else "rolled_back",
                    {
                        "operation": op,
                        "source": source_path,
                        "target": target_path,
                        "reason": reason,
                        "checkpoint_id": cp.checkpoint_id,
                        "unresolved": new_unresolved,
                        "rollback_errors": rollback_errors,
                        "rollback_index": rollback_index,
                    },
                )
            except Exception:  # noqa: BLE001 — refusal stands either way
                pass
            return {
                "ok": False,
                "dry_run": False,
                "blocked_by": "unresolved_names_after_apply",
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
                "rollback_index": rollback_index,
                "unresolved": new_unresolved,
                "checkpoint_id": cp.checkpoint_id,
                "error": (
                    "extraction would leave unresolved names (broken imports / "
                    "removed symbol still referenced) — filesystem rollback "
                    "was attempted; inspect rollback_errors before retrying."
                ),
                "diff": diffs,
            }

    # AUDIT RESULT — degraded-tolerant (mutation stands; intent already recorded).
    audit_result_ok = True
    try:
        record_event(
            "deslop_apply_result",
            "applied" if reindex_ok else "applied_reindex_degraded",
            {
                "operation": op,
                "source": source_path,
                "target": target_path,
                "reason": reason,
                "remove_from_source": remove_from_source,
                "created_paths": created_paths,
                "checkpoint_id": cp.checkpoint_id,
                "reindex_ok": reindex_ok,
                "applied": applied,
            },
        )
    except Exception:
        audit_result_ok = False

    return {
        "ok": bool(result.get("success", False)),
        "dry_run": False,
        "operation": op,
        "result": result,
        "diff": diffs,
        "reindex_ok": reindex_ok,
        "reindex_degraded": (applied and not reindex_ok),
        "audit_result_ok": audit_result_ok,
        "audit_degraded": (not audit_result_ok),
        "created_paths": created_paths,
        "checkpoints": [
            {
                "checkpoint_id": cp.checkpoint_id,
                "mode": cp.mode_summary,
                "paths": cp_paths,
                "created_paths": created_paths,
                "rollback_removes_created": bool(created_paths),
                "restore": (
                    f"aidocs ai-restore --mode restore --checkpoint {cp.checkpoint_id} "
                    f'--reason "<why>"'
                ),
            },
        ],
    }


def _file_root_to_glob(
    raw_root: str,
    project_root: Path,
    *,
    caller_glob: str | None = None,
) -> tuple[Path, str | None]:
    """If `raw_root` points at a file, return (project_root, implicit_glob).

    Agents routinely pass a file path as `root` expecting file-scoped
    search. After the WinError 183 fix (981000e) the resolver silently
    walks up to the project root, so the tool doesn't crash — but it
    then searches the whole project, returning zero scoped matches.
    That's a confusing UX.

    This helper completes the translation: when `raw_root` is a file,
    emit its relative path as a glob filter that can be passed to
    `ai_text_search` / `ai_find` / the code index store.

    If the caller already supplied a `caller_glob`, respect it (they
    asked for something specific). Otherwise we generate one.

    Returns (project_root, glob_or_None). Caller passes both into the
    hub/store call.
    """
    if caller_glob:
        return project_root, caller_glob

    if not raw_root or not raw_root.strip():
        return project_root, None

    candidate = Path(raw_root)
    # Build the probe list the same way _normalize_root_path does so
    # this helper is consistent with the resolver. The caller's already-
    # resolved `project_root` takes precedence over module globals — it's
    # the authoritative answer for this call.
    probes: list[Path] = [candidate]
    if not candidate.is_absolute():
        probes.append(project_root / candidate)
        lkpr = _rh._last_known_project_root
        if lkpr is not None and lkpr != project_root:
            probes.append(lkpr / candidate)
        env_root = _env_project_root()
        if env_root is not None and env_root != project_root:
            probes.append(env_root / candidate)

    for probe in probes:
        if probe.exists() and probe.is_file():
            # Translate to a glob relative to the project root we resolved to.
            try:
                rel = probe.resolve().relative_to(project_root.resolve())
                return project_root, rel.as_posix()
            except ValueError:
                # File is outside project root — no scope translation.
                return project_root, None

    return project_root, None


# ── ai_slop truthfulness: per-mode evidence labels ─────────────────────────
# Each scanner mode carries an honest label of WHAT IT PROVES so an agent never
# mistakes a heuristic hint for an authoritative finding. kind ∈
# {precise, heuristic, suggestion}. This does NOT change detection — it only
# stops the tool output from over-promising what the mode's name implies.
_SLOP_EVIDENCE: dict[str, dict[str, str]] = {
    "dead_code": {
        "kind": "precise",
        "proves": "unused imports and unused local variables within THIS file (AST)",
        "limitations": "intra-file only — does NOT prove a function/class is "
        "unreachable project-wide; a symbol unused here may be "
        "called from another file",
    },
    "stale_refs": {
        "kind": "precise",
        "proves": "indexed references to the named symbol(s) across files",
        "limitations": "covers references the index captured; dynamic / string / "
        "reflective references can be missed",
    },
    "untested": {
        "kind": "heuristic",
        "proves": "source files with no test file sharing their basename in the index",
        "limitations": "BASENAME heuristic, not real coverage — a file exercised "
        "only by differently-named tests reads as untested "
        "(false positive)",
    },
    "duplicates": {
        "kind": "heuristic",
        "proves": "clusters of files sharing symbols of the same name+kind that "
        "appear in 3+ files (extraction candidates)",
        "limitations": "name+kind collision, NOT proven behaviorally identical; "
        "requires a symbol in >=3 files — a 2-file copy is NOT "
        "reported; overloads / interface impls appear here",
    },
    "hotspots": {
        "kind": "heuristic",
        "proves": "files ranked by complexity / size signals from the index",
        "limitations": "a ranking signal, not a defect — high rank is not a bug",
    },
    "query_hotspots": {
        "kind": "heuristic",
        "proves": "files ranked by DB-query density from the index",
        "limitations": "a ranking signal, not a defect",
    },
    "mismatches": {
        "kind": "heuristic",
        "proves": "candidate state/model representation conflicts for a concept",
        "limitations": "pattern hint — review each; not a proven inconsistency",
    },
    "partial_group": {
        "kind": "precise",
        "proves": "indexed partial-class files for a C# type",
        "limitations": "limited to what the index captured",
    },
    "partial_consumers": {
        "kind": "precise",
        "proves": "indexed pages referencing a Razor partial",
        "limitations": "limited to what the index captured",
    },
    "symbol_range": {
        "kind": "precise",
        "proves": "the indexed line range of a symbol",
        "limitations": "reflects the last index sync",
    },
    "suggest_extract": {
        "kind": "suggestion",
        "proves": "code blocks that COULD be extracted (size/shape heuristic)",
        "limitations": "a suggestion to consider, NOT a finding or a defect; "
        "apply only via ai_deslop_apply after review",
    },
    "preview_extract_deps": {
        "kind": "precise",
        "proves": "the dependencies a given line range reads/writes (AST)",
        "limitations": "intra-file dependency view for an extraction preview",
    },
    "spaghetti": {
        "kind": "heuristic",
        "proves": "AST call chains that reach a KNOWN-EXPENSIVE callee from "
        "inside a loop body (the N+1 shape), ranked by unit cost x "
        "iterations, with every suppressed candidate reported",
        "limitations": "unit costs are a CATALOGUE (only the spaCy 10ms/parse "
        "term is measured — the rest are order-of-magnitude "
        "estimates, labeled per finding); loop cardinality is "
        "UNKNOWN unless literal in the source, in which case "
        "iterations are ASSUMED; a memoised callee is reported, "
        "not suppressed, because a memo keyed on the varying loop "
        "value does not bound first-touch cost (#688); and "
        "`expensive_via` names the most expensive REACHABLE callee "
        "in the chain, which a branch may not execute on every pass",
    },
    "broken_refs": {
        "kind": "heuristic",
        "proves": "regex-extracted references whose token has no resolving definition",
        "limitations": "regex heuristic, not AST; only reference kinds with a "
        "declared definition_source are resolvable",
    },
}
# Modes whose results come from the code INDEX (vs pure per-file AST). Only these
# get the index-staleness signal — stamping it on a pure-AST mode would be a
# misleading freshness claim.
_SLOP_INDEX_BACKED = frozenset(
    {
        "stale_refs",
        "untested",
        "duplicates",
        "hotspots",
        "query_hotspots",
        "mismatches",
        "partial_group",
        "partial_consumers",
        "symbol_range",
        "clones",
        "dead_code_project",
        "law_patterns",
        "broken_refs",
        # spaghetti runs its own AST, but the FILE SET comes from the index —
        # a stale index means an unscanned file, so the signal is honest here.
        "spaghetti",
    },
)


def _attach_slop_evidence(mode: str, result: Any) -> Any:
    """Stamp a truthful `evidence` label onto a slop-mode result (dict). Never
    overwrites an existing `evidence` key; non-dict results pass through.
    """
    ev = _SLOP_EVIDENCE.get(mode)
    if ev is None or not isinstance(result, dict) or "evidence" in result:
        return result
    return {**result, "evidence": dict(ev)}


def _read_indexed_py_sources(
    hub: Any,
    root: Path,
    *,
    limit: int = 2000,
) -> list[tuple[str, str]]:
    """Read INDEXED Python SOURCE files (rel, text) for project-wide scanners.
    Pulled from the code index, so sensitive-file exclusion (applied at sync) is
    honored and tests/fixtures are skipped. Read-only.
    """
    out: list[tuple[str, str]] = []
    try:
        with hub.code.connect(root) as conn:
            rows = conn.execute(
                "SELECT path FROM code_files "
                "WHERE (role IS NULL OR role NOT IN ('test', 'fixture')) "
                "AND path LIKE '%.py' ORDER BY path LIMIT ?",
                (int(limit),),
            ).fetchall()
    except Exception:
        return out
    from .slop_ignore import is_ignored_path

    for r in rows:
        rel = str(r["path"])
        if is_ignored_path(rel):
            # vendored / generated / scratch subtree — out of slop-scan scope
            continue
        try:
            out.append((rel, (root / rel).read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def register_code_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    timed_tool: Any,
    timed_discovery: Any,
    timed_sync: Any,
    grant_known_exact_path_read: Any,
    post_edit_reindex_and_grant: Any,
    apply_trace_depth: Any,
    resolve_related_root: Any,
    file_extract_block: Any,
    file_get_lines: Any,
    file_create_file: Any,
    file_edit_lines: Any,
    file_batch_edit: Any,
    file_str_replace: Any,
    file_batch_str_replace: Any,
    available_config_edit_modes: Any,
    self_edit_available_in_profile: Any,
    registered_tools: Any,
    all_procedures: Any,
    all_capabilities: Any,
) -> None:

    def _grant_paths_from_result(result: Any, tool_name: str, root: Any) -> None:
        """Extract file paths from any tool result, sanitize them, and grant read access.

        Sanitization (2026-04-17, Beat 5 P1):
        - Strip hidden-Unicode chars from each returned path.
        - Flag components with instruction-shaped names as `suspicious_filename`.
        Caller sees cleaned paths with an optional flag; unsanitized paths
        never reach the agent.
        """
        from .unicode_safety import sanitize_path_for_agent as _sanitize_path

        def _process_item(item: dict) -> None:
            raw = item.get("path")
            if not raw:
                return
            cleaned, stripped, suspicious = _sanitize_path(str(raw))
            if stripped or cleaned != raw:
                item["path"] = cleaned
            if stripped:
                item["hidden_unicode_stripped"] = (
                    int(item.get("hidden_unicode_stripped", 0)) + stripped
                )
            if suspicious:
                item["suspicious_filename"] = True
            grant_known_exact_path_read(hub, root, tool_name, cleaned)

        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    _process_item(item)
        elif isinstance(result, dict):
            items = result.get("matches") or result.get("results") or result.get("result") or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        _process_item(item)

    def _surface_knowledge(
        result: Any,
        *,
        query: str,
        root: Path,
        explicit_paths: tuple[str, ...] = (),
    ) -> Any:
        """One fail-quiet DDD rail for terse memory/KG entry points."""
        try:
            from .read_memory_surfacer import ReadMemorySurfacer

            return ReadMemorySurfacer(runtime).decorate_discovery_result(
                result,
                query=query,
                project_root=root,
                explicit_paths=explicit_paths,
            )
        except Exception:
            return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Search",
        },
        meta={"anthropic/searchHint": True},
    )
    def ai_search(
        query: str = "",
        modified_since: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Find files by name or summary. Use modified_since to filter by recency: 'today', '1h', '24h', '7d'. Replaces Glob — returns ranked results with language and role info."""
        from .code_index_store import parse_modified_since

        root = resolve_project_root()
        mtime_ns = parse_modified_since(modified_since)
        result = hub.code.search_code(root, query=query, limit=limit, modified_since_ns=mtime_ns)
        for item in result:
            grant_known_exact_path_read(hub, root, "ai_search", str(item.get("path", "")))
        if not result:
            return {
                "results": [],
                "empty_reason": "no_file_match",
                "hint": "Try ai_text_search for content search.",
            }
        # #62 Phase 3: surface DNT banners for any protected paths.
        try:
            from .dnt_banner_injector import maybe_dnt_banners_for_paths

            _banners = maybe_dnt_banners_for_paths(
                root,
                [str(item.get("path", "")) for item in result],
            )
        except Exception:
            _banners = []
        if _banners:
            return {"dnt_banners": _banners, "results": result}
        return result

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Text Search",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("text_search")
    def ai_text_search(
        query: str,
        glob: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        limit: int = 50,
        include_tests: bool | None = None,
        expand: bool = False,
    ) -> dict[str, Any]:
        """Full-text search across all indexed files. Replaces Grep — returns matches with line numbers. Use | or OR for multi-term. Set regex=true for patterns. Use `glob` to scope the search to a subset of files. Set expand=True to broaden a single-word query via NLP lemma + semantic synonyms (e.g. 'protect' → matches 'lock', 'guard', 'shield' too). include_tests defaults to the project's index.include_tests setting; pass True/False to override."""
        root = resolve_project_root()
        include_tests = resolve_include_tests(include_tests, project_root=root)
        if regex:
            # #482: a broken regex is pattern_invalid, never "no match".
            _rx_err = _validate_regex(query)
            if _rx_err is not None:
                return _pattern_invalid_envelope(query, _rx_err)
        effective_query = query
        expansion_terms: list[str] = []
        if expand:
            try:
                from .aidocs_nlp.consumers.search_expander import expand_query
                from .aidocs_nlp.service import get_service

                service = get_service(root, {})
                expanded = expand_query(query, service)
                if expanded != query:
                    effective_query = expanded
                    expansion_terms = expanded.split("|")
            except Exception:
                pass
        matches = hub.code.search_text(
            root,
            effective_query,
            glob=glob,
            case_sensitive=case_sensitive,
            regex=regex,
            limit=limit,
            include_tests=include_tests,
        )
        for match in matches:
            grant_known_exact_path_read(hub, root, "ai_text_search", str(match.get("path", "")))
        if not matches:
            # #482 / #478 secondary: name the reason (path_not_indexed vs
            # no_match) + next_action; legacy 'no_text_match' key retained.
            return _text_empty_envelope(
                hub,
                root,
                scope=(glob or None),
                include_tests=include_tests,
                probe_query=effective_query,
                probe_regex=regex,
                probe_limit=limit,
            )
        out: dict[str, Any] = {"total_matches": len(matches), "results": matches}
        if expansion_terms:
            out["expansion_terms"] = expansion_terms
            out["expanded_query"] = effective_query
        # #62 Phase 3: surface DNT banners for any protected paths
        # touched by matches.
        try:
            from .dnt_banner_injector import maybe_dnt_banners_for_paths

            _banners = maybe_dnt_banners_for_paths(root, [str(m.get("path", "")) for m in matches])
        except Exception:
            _banners = []
        if _banners:
            out["dnt_banners"] = _banners
        return out

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Extract Block",
        },
    )
    def ai_extract_block(
        source_path: str,
        start_line: int,
        end_line: int,
        target_path: str,
        target_position: str = "append",
        target_line: int | None = None,
        remove_from_source: bool = True,
    ) -> dict[str, Any]:
        """Move a code block from source to target file. Atomic: extracts lines, places in target, removes from source. Use for refactoring large files into modules."""
        root = resolve_project_root()
        before_source, before_target = _deslop_file_snapshot(root, source_path, target_path)
        result = file_extract_block(
            root,
            source_path,
            start_line,
            end_line,
            target_path,
            target_position=target_position,
            target_line=target_line,
            remove_from_source=remove_from_source,
        )
        if result.get("success"):
            import_management = _deslop_manage_imports(
                root,
                source_path,
                target_path,
                before_source=before_source,
                before_target=before_target,
            )
            result["import_management"] = import_management
            if not import_management.get("ok"):
                return _stamp_mutation(
                    {
                        "success": False,
                        "error": import_management.get("error", "import management failed"),
                        "blocked_by": import_management.get(
                            "blocked_by", "import_management_failed"
                        ),
                        "rollback_required": True,
                        "source_path": source_path,
                        "target_path": target_path,
                        "import_management": import_management,
                    },
                    applied=True,
                    source_path=source_path,
                    target_path=target_path,
                )
            source_reindex = post_edit_reindex_and_grant(
                hub,
                root,
                "ai_extract_block",
                str(result.get("source_path") or source_path),
            )
            _src = str(result.get("source_path") or source_path)
            _tgt = str(result.get("target_path") or target_path)
            if not source_reindex.get("ok"):
                return _stamp_mutation(
                    {
                        "success": False,
                        "error": f"file modified. index refresh fail for source: {source_reindex.get('error')}. Indexed reads may be stale.",
                        "source_path": _src,
                        "target_path": _tgt,
                    },
                    applied=True,
                    source_path=_src,
                    target_path=_tgt,
                )
            target_reindex = post_edit_reindex_and_grant(
                hub,
                root,
                "ai_extract_block",
                str(result.get("target_path") or target_path),
            )
            if not target_reindex.get("ok"):
                return _stamp_mutation(
                    {
                        "success": False,
                        "error": f"file was modified, but index refresh failed for target: {target_reindex.get('error')}. Indexed reads may be stale.",
                        "source_path": _src,
                        "target_path": _tgt,
                    },
                    applied=True,
                    source_path=_src,
                    target_path=_tgt,
                )
        _mut = bool(result.get("success"))
        return _stamp_mutation(
            result,
            applied=_mut,
            source_path=str(result.get("source_path") or source_path),
            target_path=str(result.get("target_path") or target_path),
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Find Symbol Range",
        },
    )
    def ai_find_symbol_range(
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """Find start and end line of a symbol using the index. Use before extract_block to avoid manual line counting."""
        return hub.code.find_symbol_range(
            resolve_project_root(),
            path,
            symbol,
            kind=kind,
            line_number=line_number,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Preview Extraction Dependencies",
        },
    )
    def ai_preview_extraction_deps(path: str, start_line: int, end_line: int) -> dict[str, Any]:
        """Before extracting a block, show what imports and helpers it depends on that won't come with it."""
        return hub.code.preview_extraction_deps(resolve_project_root(), path, start_line, end_line)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Extract Symbol",
        },
    )
    def ai_extract_symbol(
        source_path: str,
        symbol: str,
        target_path: str,
        kind: str | None = None,
        target_position: str = "append",
        remove_from_source: bool = True,
    ) -> dict[str, Any]:
        """Move a symbol (function/class/method) from source to target file by name. No line numbers needed — uses the index to find boundaries."""
        root = resolve_project_root()
        rng = hub.code.find_symbol_range(root, source_path, symbol, kind=kind)
        if "error" in rng:
            return _stamp_mutation({"success": False, "error": rng["error"]}, applied=False)
        before_source, before_target = _deslop_file_snapshot(root, source_path, target_path)
        result = file_extract_block(
            root,
            source_path,
            int(rng["start"]),
            int(rng["end"]),
            target_path,
            target_position=target_position,
            remove_from_source=remove_from_source,
        )
        if result.get("success"):
            import_management = _deslop_manage_imports(
                root,
                source_path,
                target_path,
                before_source=before_source,
                before_target=before_target,
            )
            result["import_management"] = import_management
            if not import_management.get("ok"):
                return _stamp_mutation(
                    {
                        "success": False,
                        "error": import_management.get("error", "import management failed"),
                        "blocked_by": import_management.get(
                            "blocked_by", "import_management_failed"
                        ),
                        "rollback_required": True,
                        "source_path": source_path,
                        "target_path": target_path,
                        "import_management": import_management,
                    },
                    applied=True,
                    source_path=source_path,
                    target_path=target_path,
                )
            source_reindex = post_edit_reindex_and_grant(
                hub,
                root,
                "ai_extract_symbol",
                source_path,
            )
            if not source_reindex.get("ok"):
                return _stamp_mutation(
                    {
                        "success": False,
                        "error": f"file modified. index refresh fail for source: {source_reindex.get('error')}. Indexed reads may be stale.",
                        "source_path": source_path,
                        "target_path": target_path,
                    },
                    applied=True,
                    source_path=source_path,
                    target_path=target_path,
                )
            target_reindex = post_edit_reindex_and_grant(
                hub,
                root,
                "ai_extract_symbol",
                target_path,
            )
            if not target_reindex.get("ok"):
                return _stamp_mutation(
                    {
                        "success": False,
                        "error": f"file was modified, but index refresh failed for target: {target_reindex.get('error')}. Indexed reads may be stale.",
                        "source_path": source_path,
                        "target_path": target_path,
                    },
                    applied=True,
                    source_path=source_path,
                    target_path=target_path,
                )
        return _stamp_mutation(
            result,
            applied=bool(result.get("success")),
            source_path=source_path,
            target_path=target_path,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Suggest Extractions",
        },
    )
    def ai_suggest_extractions(path: str, min_lines: int = 20, limit: int = 10) -> dict[str, Any]:
        """Show the largest symbols in a file that are good extraction candidates. Use to plan deslopification."""
        candidates = hub.code.suggest_extractions(
            resolve_project_root(),
            path,
            min_lines=min_lines,
            limit=limit,
        )
        return {"path": path, "candidates": candidates, "total": len(candidates)}

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Refactor Extract",
        },
    )
    def ai_refactor_extract(
        source_path: str,
        symbol: str,
        target_path: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Full refactor pipeline: find symbol → extract to target → reindex both → detect stale references + dead code. Returns extraction result plus cleanup suggestions."""
        r = resolve_project_root()

        # 1. Find symbol range
        rng = hub.code.find_symbol_range(r, source_path, symbol, kind=kind)
        if "error" in rng:
            return _stamp_mutation(
                {"success": False, "step": "find_range", "error": rng["error"]},
                applied=False,
            )

        # 2. Extract
        before_source, before_target = _deslop_file_snapshot(r, source_path, target_path)
        extract_result = file_extract_block(
            r,
            source_path,
            int(rng["start"]),
            int(rng["end"]),
            target_path,
            target_position="append",
            remove_from_source=True,
        )
        if not extract_result.get("success"):
            return _stamp_mutation(
                {
                    "success": False,
                    "step": "extract",
                    "error": extract_result.get("error"),
                },
                applied=False,
            )
        import_management = _deslop_manage_imports(
            r,
            source_path,
            target_path,
            before_source=before_source,
            before_target=before_target,
        )
        extract_result["import_management"] = import_management
        if not import_management.get("ok"):
            return _stamp_mutation(
                {
                    "success": False,
                    "step": "import_management",
                    "error": import_management.get("error", "import management failed"),
                    "blocked_by": import_management.get(
                        "blocked_by", "import_management_failed"
                    ),
                    "rollback_required": True,
                    "import_management": import_management,
                },
                applied=True,
                source_path=source_path,
                target_path=target_path,
            )

        # 3. Reindex both files
        source_reindex = post_edit_reindex_and_grant(hub, r, "ai_refactor_extract", source_path)
        if not source_reindex.get("ok"):
            return _stamp_mutation(
                {
                    "success": False,
                    "step": "reindex_source",
                    "error": f"file modified. index refresh fail for source: {source_reindex.get('error')}. Indexed reads may be stale.",
                },
                applied=True,
                source_path=source_path,
                target_path=target_path,
            )
        target_reindex = post_edit_reindex_and_grant(hub, r, "ai_refactor_extract", target_path)
        if not target_reindex.get("ok"):
            return _stamp_mutation(
                {
                    "success": False,
                    "step": "reindex_target",
                    "error": f"file was modified, but index refresh failed for target: {target_reindex.get('error')}. Indexed reads may be stale.",
                },
                applied=True,
                source_path=source_path,
                target_path=target_path,
            )

        # 4. Find stale references to the moved symbol
        stale = hub.code.find_stale_references(r, [symbol], exclude_path=target_path, limit=20)

        # 5. Find dead code in source (imports that became unused after extraction)
        dead = hub.code.find_dead_code(r, source_path)

        return _stamp_mutation(
            {
                "success": True,
                "source_path": source_path,
                "target_path": target_path,
                "import_management": import_management,
                "extracted": {
                    "symbol": symbol,
                    "source": source_path,
                    "target": target_path,
                    "lines": rng["lines"],
                },
                "stale_references": stale,
                "dead_code": {
                    "dead_imports": dead.get("dead_imports", []),
                    "unused_locals": dead.get("unused_locals", []),
                },
            },
            applied=True,
            source_path=source_path,
            target_path=target_path,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,  # SCAN/READ only — apply lives in ai_deslop_apply
            "openWorldHint": False,
            "title": "AI Slop — Cleanup Scanner (read-only)",
        },
        meta={"anthropic/searchHint": True},
    )
    def ai_slop(
        mode: str,
        query: str = "",
        path: str = "",
        symbol: str = "",
        kind: str | None = None,
        start_line: int = 0,
        end_line: int = 0,
        symbols: list[str] | None = None,
        exclude_path: str | None = None,
        include_tests: bool = False,
        glob: str | None = None,
        min_lines: int = 20,
        line_number: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read-only slop FINDER (split 2026-05-24: all mutating refactor modes
        moved to ai_deslop_apply — a guarded Tier-M path). This tool NEVER
        mutates and issues no write grants.

        Use ai_find for REAL code (symbols, refs, routes, traces).
        Use ai_slop for STALE code (dead, duplicate, hot, mismatched).

        Read-only modes (find the slop):
          dead_code(path)
          stale_refs(symbols[], exclude_path?, include_tests?, limit?)
          untested(glob?, limit?)
          duplicates(query?, limit?)           — heuristic: files sharing name+kind
                                                  symbols that appear in 3+ files
          clones(min_lines?, limit?)           — PROJECT-WIDE structural clones
                                                  (2-file + renamed; built-in AST)
          dead_code_project()                  — PROJECT-WIDE Python dead code via
                                                  optional Vulture backend
          backends()                           — optional-backend availability
          spaghetti(limit?)                    — PROJECT-WIDE N+1: an expensive
                                                  call reached from inside a loop
                                                  (ranked by cost; AST)
          hotspots(query, limit?)              — complexity / churn hotspots
          query_hotspots(query, limit?)        — heavy DB-query files
          mismatches(query, limit?)            — state/model representation conflicts
          partial_group(query, limit?)         — all partial-class files for a C# type
          partial_consumers(query, limit?)     — pages referencing a Razor partial
          symbol_range(path, symbol, kind?, line_number?)
          suggest_extract(path, min_lines?, limit?)
          preview_extract_deps(path, start_line, end_line)

        To APPLY an extraction/refactor, call ai_deslop_apply (Tier-M; dry-run by
        default, checkpoints + audits, requires a reason).

        Large result sets (e.g. many `clones` clusters) page IN-BAND via
        `offset`/`limit` instead of spilling to a file — `.aidocs/runtime/`
        is refused outright by the read gate (forbidden_aidocs_path, #715),
        so a file-backed pointer would name a remedy the caller can never
        reach. Walk the full set by re-calling with `offset=<next_offset>`
        until `next_offset` is null.
        """
        m = mode.strip().lower()
        root = resolve_project_root()

        def _slop_spill(canon, payload, page_offset: int = 0, page_limit: int = 50):
            """Large results page IN-BAND over the payload's list field
            (e.g. 'clusters', 'results') via offset/limit, so the FULL
            result stays reachable through this same governed tool call —
            never spilled to `.aidocs/runtime/`, which the read gate
            refuses unconditionally (blocked_by=forbidden_aidocs_path,
            #715: a named remedy the caller could never reach). Small
            results (<=12000 chars serialized, offset==0) pass through
            unchanged."""
            import json as _json

            try:
                full = _json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                return payload
            needs_paging = len(full) > 12000 or int(page_offset or 0) > 0
            if not needs_paging:
                return payload
            if not isinstance(payload, dict):
                return payload
            list_key = None
            for k, v in payload.items():
                if isinstance(v, list) and list_key is None:
                    list_key = k
                    break
            if list_key is None:
                # Nothing list-shaped to page over — return as-is rather
                # than inventing an unreadable pointer.
                return payload
            items = payload[list_key]
            total = len(items)
            size = max(1, int(page_limit) or 50)
            start = max(0, int(page_offset or 0))
            page = items[start : start + size]
            has_more = (start + size) < total
            out: dict[str, Any] = {"mode": canon, "paginated": True}
            for k, v in payload.items():
                if k == list_key or isinstance(v, list):
                    # secondary lists (e.g. parse_errors) are dropped from
                    # the page, same as the prior single-sample summary.
                    continue
                out[k] = v
            out[list_key] = page
            out[f"{list_key}_offset"] = start
            out[f"{list_key}_limit"] = size
            out[f"{list_key}_returned"] = len(page)
            out[f"{list_key}_total"] = total
            out["has_more"] = has_more
            out["next_offset"] = (start + size) if has_more else None
            return out
        if m in ("extract_block", "extract_symbol", "refactor_extract"):
            return {
                "success": False,
                "error": (
                    f"'{m}' is a MUTATING refactor — moved to ai_deslop_apply "
                    "(guarded Tier-M: dry-run default, checkpoint, audit, reason "
                    "required). ai_slop is read-only."
                ),
                "use_instead": "ai_deslop_apply",
            }
        # Canonical mode key (collapse aliases) for the evidence/staleness layer.
        _canon = {
            "stale_references": "stale_refs",
            "untested_files": "untested",
            "suggest_extractions": "suggest_extract",
            "preview_extraction_deps": "preview_extract_deps",
        }.get(m, m)
        _result: Any = None
        if m == "dead_code":
            _result = hub.code.find_dead_code(root, path)
        elif m in ("stale_refs", "stale_references"):
            results = hub.code.find_stale_references(
                root,
                symbols or [],
                exclude_path=exclude_path,
                include_tests=include_tests,
                limit=limit,
            )
            _result = {"total_stale": len(results), "results": results}
        elif m in ("untested", "untested_files"):
            results = hub.code.find_untested_files(
                root,
                glob=glob,
                limit=int(limit),
            )
            _result = {"total": len(results), "results": results}
        elif m == "duplicates":
            _result = hub.code.find_duplicate_structures(
                root,
                role_filter=query or None,
                limit=limit,
            )
        elif m in ("clones", "duplicate_blocks"):
            # Project-wide structural clone detection (catches 2-file copies AND
            # renamed copies) via the built-in AST detector — no dependency.
            from . import slop_backends as _sb

            _result = _sb.ast_clones(
                _read_indexed_py_sources(hub, root),
                min_nodes=max(8, int(min_lines)),
                min_occurrences=2,
            )
        elif m in ("dead_code_project", "project_dead_code"):
            # Project-wide Python dead code via the OPTIONAL Vulture backend;
            # honest 'unavailable' + install hint when not installed.
            from . import slop_backends as _sb

            _abs = [str(root / rel) for rel, _ in _read_indexed_py_sources(hub, root)]
            _result = _sb.run_vulture(_abs)
        elif m in ("law_patterns", "castle_laws"):
            # AIDOCS castle-law scan via the OPTIONAL Semgrep backend (report-
            # first; honest 'unavailable' on engines that can't run, e.g. native
            # Windows → CI/Linux lane). The ruleset is PACKAGE-bundled
            # (aidocs_mcp/law_rules/aidocs-laws.yml), resolved independently of the
            # scanned project root, so it works in installed deployments too.
            from . import slop_backends as _sb

            _abs = [str(root / rel) for rel, _ in _read_indexed_py_sources(hub, root)]
            _result = _sb.run_semgrep(_abs, config=_sb.aidocs_law_rules_path())
        elif m in ("spaghetti", "n_plus_one"):
            # #686 signal 1: an EXPENSIVE call reached from inside a loop body.
            # Validated against the known answer #688 (the memory surfacer's
            # ~1022 spaCy parses per prompt). Read-only, like every mode here.
            from . import slop_spaghetti as _sg

            _result = _sg.find_n_plus_one(
                _read_indexed_py_sources(hub, root),
                limit=int(limit),
            )
        elif m in ("backends", "scanner_backends"):
            from . import slop_backends as _sb

            _result = {"backends": _sb.backend_status()}
        elif m in ("broken_refs", "ref_integrity", "dead_refs"):
            _result = hub.code.find_broken_references(root, limit=int(limit))
        elif m == "hotspots":
            _result = hub.code.find_hotspots(root, query=query, limit=limit)
        elif m == "query_hotspots":
            _result = hub.code.find_query_hotspots(root, query=query, limit=limit)
        elif m == "mismatches":
            _result = hub.code.find_state_model_mismatch(
                root,
                concept=query,
                limit=limit,
            )
        elif m == "partial_group":
            _result = hub.code.find_partial_group(root, symbol=query, limit=limit)
        elif m == "partial_consumers":
            _result = hub.code.find_partial_consumers(
                root,
                partial_name=query,
                limit=limit,
            )
        elif m == "symbol_range":
            _result = ai_find_symbol_range(
                path,
                symbol,
                kind=kind,
                line_number=line_number,
            )
        elif m in ("suggest_extract", "suggest_extractions"):
            _result = ai_suggest_extractions(
                path,
                min_lines=min_lines,
                limit=min(limit, 50),
            )
        elif m in ("preview_extract_deps", "preview_extraction_deps"):
            _result = ai_preview_extraction_deps(
                path,
                start_line,
                end_line,
            )
        if _result is not None:
            # Index-backed modes also carry the honest staleness signal so a
            # stale index never silently masquerades as fresh truth.
            if _canon in _SLOP_INDEX_BACKED:
                _result = _inject_index_staleness(_result, root)
            return _slop_spill(
                _canon,
                _attach_slop_evidence(_canon, _result),
                page_offset=offset,
                page_limit=limit,
            )
        return {
            "success": False,
            "error": f"unknown read mode '{mode}'",
            "modes": [
                "dead_code",
                "dead_code_project",
                "stale_refs",
                "untested",
                "duplicates",
                "clones",
                "law_patterns",
                "spaghetti",
                "hotspots",
                "query_hotspots",
                "mismatches",
                "partial_group",
                "partial_consumers",
                "symbol_range",
                "suggest_extract",
                "preview_extract_deps",
                "backends",
                "broken_refs",
            ],
            "apply_via": "ai_deslop_apply (extract_block/extract_symbol/refactor_extract)",
        }

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "AI Deslop Apply (guarded refactor)",
        },
    )
    def ai_deslop_apply(
        operation: str,
        reason: str,
        source_path: str = "",
        target_path: str = "",
        symbol: str = "",
        kind: str | None = None,
        start_line: int = 0,
        end_line: int = 0,
        target_position: str = "append",
        target_line: int | None = None,
        remove_from_source: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Guarded APPLY path for deslop refactors — the Tier-M counterpart of
        the read-only ai_slop scanner.

        operation: extract_block | extract_symbol | refactor_extract.

        Doctrine (all enforced here):
          * DRY-RUN BY DEFAULT (dry_run=True) → returns a plan, mutates nothing.
          * remove_from_source defaults to FALSE (copy, don't delete the source
            block) unless the caller explicitly opts in.
          * Requires a meaningful `reason` (>=6 chars) and EXACT selectors
            (source_path + target_path, plus a start/end line range for
            extract_block or an exact `symbol` for symbol/refactor).
          * Refuses PROTECTED / control-authority / unsafe paths (source+target).
          * Creates a pre-edit CHECKPOINT (git-ref or quarantine copy) BEFORE any
            mutation — returns checkpoint/restore metadata; refuses to mutate if
            the checkpoint can't be secured.
          * Reuses the canonical extract+reindex path; surfaces reindex-failure
            (stale-index) semantics.
          * AUDITS every apply.
        Returns: {ok, dry_run, plan|diff, checkpoints, reindex_ok, ...}.
        """
        root = resolve_project_root()

        def _record(event_kind: str, status: str, payload: dict) -> None:
            hub.execution.record_event(
                root,
                event_kind=event_kind,
                source_kind="ai_deslop_apply",
                capability_name="ai_deslop_apply",
                action_kind=operation.strip().lower(),
                target_entity=source_path,
                status=status,
                payload=payload,
            )

        def _rollback_reindex(paths: list[str]) -> dict[str, dict[str, Any]]:
            outcomes: dict[str, dict[str, Any]] = {}
            for rel in dict.fromkeys(paths):
                try:
                    hub.code.sync_code_files(
                        root,
                        paths=[rel],
                        incremental=True,
                    )
                    outcomes[rel] = {"ok": True}
                except Exception as exc:  # noqa: BLE001 — reported to caller
                    outcomes[rel] = {"ok": False, "error": str(exc)}
            return outcomes

        return deslop_apply_execute(
            root,
            operation,
            reason=reason,
            source_path=source_path,
            target_path=target_path,
            symbol=symbol,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            target_position=target_position,
            target_line=target_line,
            remove_from_source=remove_from_source,
            dry_run=dry_run,
            extract_block=ai_extract_block,
            extract_symbol=ai_extract_symbol,
            refactor_extract=ai_refactor_extract,
            record_event=_record,
            preview_deps=ai_preview_extraction_deps,
            symbol_range=ai_find_symbol_range,
            rollback_reindex=_rollback_reindex,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Dependencies",
        },
        meta={"anthropic/searchHint": True},
    )
    def ai_get_dependencies(path: str) -> list[dict[str, str]]:
        """Return lightweight dependency edges for one indexed code file."""
        root = resolve_project_root()
        result = hub.code.get_dependencies(root, path=path)
        return _surface_knowledge(result, query=path, root=root, explicit_paths=(path,))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Outline",
        },
        meta={"anthropic/searchHint": True},
    )
    def ai_get_outline(path: str) -> list[dict[str, Any]] | dict[str, Any]:
        # list|dict annotation: get_outline returns a bare list[dict] (the common
        # path) or {"dnt_banner", "outline"} dict. FastMCP wraps the list into the
        # structured_content={"result": [...]} sidecar so the outer wrapper stamps
        # code freshness beside it (items unchanged).
        """Return the outline (symbols + kinds + line numbers) of a
        single indexed file. Much cheaper than reading the whole file
        when you just need to know what's in it — useful before
        ai_get_symbol_snippet to pick the right symbol, or to locate
        a container class for a partial definition.

        Normally returns list[dict]. When the file is in a DNT family
        and the banner has not yet been shown this conversation,
        returns {"dnt_banner": str, "outline": list[dict]} instead so
        the agent must read past the banner before getting symbols
        (#62 Phase 3).
        """
        root = resolve_project_root()
        result = hub.code.get_outline(root, path=path)
        if result:
            grant_known_exact_path_read(hub, root, "ai_get_outline", path)
        try:
            from .dnt_banner_injector import maybe_dnt_banner_for_read

            _banner = maybe_dnt_banner_for_read(root, path)
        except Exception:
            _banner = ""
        if _banner:
            result = {"dnt_banner": _banner, "outline": result}
        return _surface_knowledge(result, query=path, root=root, explicit_paths=(path,))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Symbol Snippet",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("snippet")
    def ai_get_symbol_snippet(
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
        to_file: str | None = None,
    ) -> Any:
        """Return an exact code snippet for an indexed outline symbol.

        `to_file=<abs path>` writes the RAW DECODED UTF-8 source (never
        JSON-escaped) to a scratch/session file — use it for big symbols
        or verbatim-freeze work where mojibake is fatal. Allowed
        destinations: the system temp dir (agent scratchpad) or the
        project's .MEMORY/ tree. Read it back with:
        `Get-Content -Raw -Encoding UTF8 '<path>'`.
        """
        root = resolve_project_root()
        result = hub.code.get_symbol_snippet(
            root,
            path=path,
            symbol=symbol,
            kind=kind,
            line_number=line_number,
        )
        if result:
            grant_known_exact_path_read(hub, root, "ai_get_symbol_snippet", path)
        # #481 (War KK) raw sidecar / overflow honesty.
        if isinstance(result, dict) and isinstance(result.get("snippet"), str):
            snippet_text = str(result["snippet"])
            if to_file:
                dest, dest_err = _resolve_snippet_sidecar_dest(root, to_file)
                if dest is None:
                    result = dict(result)
                    result.pop("snippet", None)
                    result["error"] = dest_err
                    result["snippet"] = f"❌ {dest_err}"
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    raw_bytes = snippet_text.encode("utf-8")
                    dest.write_bytes(raw_bytes)
                    read_cmd = f"Get-Content -Raw -Encoding UTF8 '{dest}'"
                    result = dict(result)
                    result["written_to"] = str(dest)
                    result["bytes_written"] = len(raw_bytes)
                    result["encoding"] = "utf-8"
                    result["snippet"] = (
                        f"[snippet written RAW (decoded UTF-8, not JSON-escaped) "
                        f"to {dest} — {len(raw_bytes)} bytes]\n"
                        f"Read it byte-faithfully with: {read_cmd}"
                    )
            elif len(snippet_text) > SNIPPET_INLINE_CHAR_BUDGET:
                # In-band overflow: never leave truncation to a host-side
                # artifact dump whose JSON-escaped re-read mojibakes source.
                from .session_response_ledger import budget_label

                kept = snippet_text[:SNIPPET_INLINE_CHAR_BUDGET]
                result = dict(result)
                result["snippet_truncated"] = True
                result["snippet"] = (
                    kept
                    + "\n… "
                    + budget_label(
                        len(kept),
                        len(snippet_text),
                        "chars. Re-call with to_file='<abs path in your "
                        "scratchpad or .MEMORY/sessions/...>' for the FULL "
                        "RAW UTF-8 source, then read it with: "
                        "Get-Content -Raw -Encoding UTF8 '<that path>'",
                    )
                )
        # #62 Phase 3: surface DNT banner once-per-session per family.
        # Adds top-level dnt_banner field when path is in a DNT family
        # and not yet shown this conversation.
        try:
            from .dnt_banner_injector import maybe_dnt_banner_for_read

            _banner = maybe_dnt_banner_for_read(root, path)
        except Exception:
            _banner = ""
        if _banner and isinstance(result, dict):
            result["dnt_banner"] = _banner
        # #648 (P2 provenance stamp): ai_get_symbol_snippet serves
        # INDEXED content and never calls read_pipeline.gate(), so it
        # never computed a trust zone. Additive-only cheap stamp —
        # NO new gating/refusals: project-relative paths under root
        # are project_internal; third_party/ paths are additionally
        # flagged vendored=True (§XXXI vendored-source class).
        if isinstance(result, dict):
            try:
                from .path_trust_zone import classify_path

                result["zone"] = str(classify_path(path, project_root=root))
            except Exception:
                result["zone"] = "PathTrustZone.PROJECT_INTERNAL"
            if str(path).replace("\\", "/").lstrip("./").startswith("third_party/"):
                result["vendored"] = True
        return _surface_knowledge(
            result, query=symbol, root=root, explicit_paths=(path,)
        )
    # ── Unified symbol info tool (replaces 7 granular tools) ──

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "Get Symbol Info"},
        eager=True,
    )
    @timed_sync
    def ai_get_symbol_info(
        symbol: str,
        kind: str = "signature",
        symbols: list[str] | None = None,
        container: str | None = None,
        include_related: bool = False,
        limit: int = 20,
        path: str = "",
    ) -> dict[str, Any]:
        """Get symbol info without reading files. Kind: signature, signatures, constructor, constructors, enum, properties, api.

        Pass `path` to disambiguate same-named symbols across files
        (e.g. `edit_result` in 20+ places). Matches with a different
        path are filtered out after the hub returns.

        Examples:
          ai_get_symbol_info("my_method", kind="signature")
          ai_get_symbol_info("MyClass", kind="constructor", path="src/models.py")
          ai_get_symbol_info("MyClass", kind="api")
          ai_get_symbol_info("Status", kind="enum")
          ai_get_symbol_info("UserDTO", kind="properties")
          ai_get_symbol_info("", kind="signatures", symbols=["method_a", "method_b"])
          ai_get_symbol_info("", kind="constructors", symbols=["ClassA", "ClassB"])

        """
        root = resolve_project_root()
        k = kind.strip().lower()
        _unused_path_filter = path  # applied below after result collected

        if k == "signature":
            result = hub.code.get_method_signature(
                root,
                method_name=symbol,
                container=container,
                limit=limit,
            )
        elif k == "signatures":
            methods = symbols or [symbol]
            result = hub.code.get_method_signatures(
                root,
                methods=methods,
                container=container,
                limit_per_method=limit,
            )
        elif k == "constructor":
            result = hub.code.get_constructor_params(
                root,
                type_name=symbol,
                limit=limit,
                include_related=include_related,
            )
        elif k == "constructors":
            types = symbols or [symbol]
            result = hub.code.get_constructor_params_batch(
                root,
                types=types,
                include_related=include_related,
                limit_per_type=limit,
            )
        elif k == "enum":
            result = hub.code.get_enum_values(
                root,
                enum_name=symbol,
                limit=limit,
                include_related=include_related,
            )
        elif k == "properties":
            result = hub.code.get_entity_properties(root, entity_name=symbol)
        elif k == "api":
            result = hub.code.get_service_api(root, service_name=symbol, limit=limit)
        else:
            return {
                "error": f"Unknown kind: {kind}. Use: signature, signatures, constructor, constructors, enum, properties, api.",
            }
        return _filter_symbol_info_by_path(result, path)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Investigate",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("investigate")
    @timed_discovery
    def ai_investigate(
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
        timeout: int | None = None,
    ) -> Any:
        """Investigate a concept by ranking container symbols (classes, structs,
        records, interfaces) that match it. Returns findings plus suggested next
        tools, with paths pre-granted for follow-up reads.

        When to use vs alternatives:
        - Known symbol name (function or method): use ai_find(mode="symbols").
        - Concept/type/feature search: use this tool.
        - Architecture of a known file or module: use ai_bundle.

        Container kinds rank above plain functions; a query like "resolve root"
        will surface types/classes that match before functions of that name.

        Args:
            concept: Thing to investigate (e.g., "PDF generation", "Patient").
            depth: `shallow`, `standard`, or `deep`.
            focus: `general`, `workflow`, `service`, `schema`, `ui`, or `backend`.

        """
        root = resolve_project_root()
        # #462 golden-road: wire the palace so investigate's aggregated
        # evidence lanes (kingdom+empire semantic) can fire. Best-effort —
        # a missing palace/hub_ctx only silences those lanes (fail-quiet).
        _palace = getattr(hub, "palace", None)
        _hub_ctx = None
        if _palace is not None:
            try:
                from .palace_hub_extension import build_palace_context

                _hub_ctx = build_palace_context(hub, runtime, tool_name="ai_investigate")
            except Exception:
                _hub_ctx = None
        _investigate_session = ""
        try:
            from .mcp_server_runtime_helpers import current_calling_host_session_id

            from .managed_mode_service import resolve_managed_session

            _investigate_session = resolve_managed_session(
                hub.managed_mode,
                root,
                host_session_id=current_calling_host_session_id(),
            )
        except Exception:
            _investigate_session = ""
        result = hub.code.investigate(
            root,
            concept=concept,
            limit=limit,
            depth=depth,
            focus=focus,
            palace=_palace,
            hub_ctx=_hub_ctx,
            session_id=_investigate_session or None,
        )
        _paths_seen: list[str] = []
        for finding in result.get("findings") or []:
            for item in finding.get("top") or []:
                p = item.get("path")
                if p:
                    grant_known_exact_path_read(hub, root, "ai_investigate", str(p))
                    _paths_seen.append(str(p))
        # #62 Phase 3: DNT banners for any protected paths surfaced.
        try:
            from .dnt_banner_injector import maybe_dnt_banners_for_paths

            _banners = maybe_dnt_banners_for_paths(root, _paths_seen)
        except Exception:
            _banners = []
        if _banners:
            result["dnt_banners"] = _banners
        result = _surface_knowledge(result, query=concept, root=root)
        findings = result.get("findings") or []
        next_tools = result.get("next_tools") or []
        compact = runtime.build_artifact_backed_result(
            root,
            inline_summary=(
                f"Investigation for `{concept}` found {len(findings)} finding(s) and {len(next_tools)} suggested next tool(s)."
            ),
            payload=result,
            artifact_name=f"code-investigate-{concept}",
            structured_summary={
                "concept": concept,
                "finding_count": len(findings),
                "next_tool_count": len(next_tools),
                "depth": depth,
                "focus": focus,
            },
        )
        result.update(compact)
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Unified Tools (v1.1.0) — prefer these over granular tools below
    # ═══════════════════════════════════════════════════════════════════════

    _FIND_MODES = {
        "symbols": "Search symbols by name, kind, or role",
        "references": "Find all usages of a symbol across the codebase",
        "routes": "Find API endpoints, page routes, controllers",
        "entrypoints": "Find bootstrap, main, provider-like entry symbols",
        "api_consumers": "Find pages/scripts calling an API endpoint",
        "frontend_symbols": "Find components, hooks, providers by name",
        "data_structures": "Find classes, records, enums with their members",
        "initializers": "Find DOMContentLoaded, document.ready, window.onload",
        "mutations": "Find create/update/delete flows for a concept",
        "validation": "Find validation logic, required fields, validators",
        "async": "Find async boundaries, deferred execution, Task patterns",
        "policy": "Find authorization, RBAC, permission checks",
        "touchpoints": "Find UI↔backend connection points for a concept",
        "clusters": "Find cross-layer grouping for a domain concept",
        "transitions": "Find migration seams, adapters, compatibility layers",
        "factories": "Find Create* helpers, factory-style methods, and setup helpers",
    }

    _FIND_GENERIC = [
        "references",
        "text",
        "regex",
        "string",
        "routes",
        "entrypoints",
        "api_consumers",
        "frontend_symbols",
        "data_structures",
        "initializers",
        "mutations",
        "validation",
        "async",
        "policy",
        "touchpoints",
        "clusters",
        "transitions",
        "factories",
    ]

    # Per-mode one-liners rendered into the mode enum description
    # (2026-06-11): each says WHAT the mode returns and — critically —
    # what `query` MEANS in that mode (symbol vs file path vs concept),
    # so agents never guess the polymorphic param.
    _FIND_MODE_DESCS = {
        "symbols": "query=symbol name or concept → ranked definitions (members fold under their container; line_end spans)",
        "dependencies": "query=FILE PATH → its import/dependency edges (aliases: deps, imports)",
        "references": "query=SYMBOL NAME → every usage/caller site (aliases: callers, usages)",
        "text": "query=literal text → matching lines across indexed files",
        "regex": "query=regular expression → matching lines",
        "string": "query=exact string → matching lines",
        "routes": "query=concept or controller → API endpoints, page routes",
        "entrypoints": "query=concept → bootstrap/main/provider entry symbols",
        "api_consumers": "query=endpoint or URL fragment → pages/scripts that call it",
        "frontend_symbols": "query=name → components, hooks, providers",
        "data_structures": "query=concept → classes/records/enums with members",
        "initializers": "query=FILE PATH (empty = all files) → init/DOMContentLoaded handlers",
        "mutations": "query=domain concept → create/update/delete flows",
        "validation": "query=concept → validators, required fields, validation logic",
        "async": "query=concept (empty = all) → async boundaries, Task patterns",
        "policy": "query=concept → authorization/RBAC/permission checks",
        "touchpoints": "query=concept → UI↔backend connection points",
        "clusters": "query=domain concept → cross-layer file grouping",
        "transitions": "query=concept → migration seams, adapters, compat layers",
        "factories": "query=name or concept → Create*/factory/setup helpers (include_tests auto-on)",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Find",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("find")
    @_references_default_timeout
    @timed_discovery
    def ai_find(
        query: str,
        mode: str = "symbols",
        kind: str | None = None,
        role: str | None = None,
        modified_since: str | None = None,
        include_tests: bool | None = None,
        tests: Literal["exclude", "include", "only"] | None = None,
        limit: int = 50,
        path: str | None = None,
        strict: bool = False,
        fuzz: bool = False,
        timeout: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        # Concrete list|dict annotation (same as ai_search) so FastMCP emits the
        # structured_content={"result": [...]} sidecar for the bare-list modes
        # (references/routes/entrypoints/...); the outer call_tool wrapper then
        # stamps code freshness ALONGSIDE the list (items + mode semantics
        # unchanged). dict modes (symbols/clusters) keep stamping in place.
        """Unified find tool — replaces all code_find_* and code_search_* tools.

        Modes: symbols, references, dependencies, routes, entrypoints,
        api_consumers, frontend_symbols, data_structures, initializers,
        mutations, validation, async, policy, touchpoints, clusters,
        transitions, factories, text, regex, string.

        mode=dependencies: query is an indexed FILE PATH; returns its
        import/dependency edges (same data as ai_get_dependencies —
        "what does this depend on / who imports it").

        mode=regex: query is a REGULAR EXPRESSION matched against file
        contents — e.g. r"def\\s+handle_\\w+" or "settings_mutation|anti.?coup".
        mode=text / mode=string: literal substring / exact-string content
        search. These three are the plain-text/markdown fallback; for CODE
        prefer the symbol/structural modes above (they return the definition
        plus callers and fit, not just matching lines). Pass a TIGHT pattern —
        a broad query returns a huge match set or times out.

        Slop-shaped modes (duplicates, hotspots, query_hotspots, mismatches,
        partial_group, partial_consumers, dead_code, stale_refs, untested)
        moved to ai_slop(mode=...) — that's the maintenance/cleanup tool.

        Args:
            query: What to find (symbol name, concept, endpoint, class name, etc.).
            mode: Which find mode to use (see above).
            kind: Filter by symbol kind (only for mode=symbols).
            role: Filter by file role (only for mode=symbols).
            modified_since: Filter by file modification time: "today", "1h", "24h", "7d", or ISO datetime. Only for mode=symbols.
            include_tests: INDEX-BUILD policy escape hatch — whether test/fixture files are in scope at all. Default follows the project's index.include_tests setting. For "I am looking for tests" prefer `tests` below; these are two knobs at two layers and must not be confused.
            tests: QUERY-TIME test lens, the door to test files. "include" = search tests alongside production; "only" = search tests EXCLUSIVELY (use this for "find the test that covers X"); "exclude" = force tests out regardless of project config. Unset = today's default, which for text/regex/string means test/fixture-role files are NOT searched (they flood results), while the structural modes rank them alongside production. A text/regex/string result that is empty ONLY because tests were suppressed says empty_reason="tests_excluded" and names this parameter — an empty answer never silently means "hidden".
            path: Scope for mode=text/regex/string ONLY — a plain path prefix ("mcp/scripts"), an exact file, or a glob ("*.md" / "src/**/*.py"). Narrows the search before the limit applies; other modes ignore it.
            strict: mode=symbols only — return exact+strong tier hits only (no related/fuzzy padding).
            fuzz: mode=symbols only — force the permissive flood (all tiers, fuzz phase always fires, no related-tier collapse).

        """
        from .code_index_store import parse_modified_since

        root = resolve_project_root()
        # #12 (Empire 2026-06-20): a discovery tool keeps the sitter alive so the index
        # stays warm — this is the guarantee that makes removing ai_find's inline resync
        # (#74) safe. Idempotent + fast-path (a dict lookup once running); best-effort,
        # never block or fail a read on sitter bookkeeping.
        try:
            from .project_index_sitter import ensure_index_sitter

            ensure_index_sitter(root, hub)
        except Exception:
            pass
        include_tests = resolve_include_tests(include_tests, project_root=root)
        # The query-time test lens. `include_tests` decides what is INDEXED and
        # in scope; `tests` decides what this ONE query wants to see of it.
        # Unset leaves the resolved policy untouched — the default is not
        # rebalanced by the existence of the door.
        lens = (tests or "").strip().lower() or None
        if lens is not None and lens not in _TESTS_LENS_VALUES:
            return {
                "error": f"Unknown tests lens: {tests!r}",
                "valid_values": list(_TESTS_LENS_VALUES),
                "next_action": (
                    "tests='include' searches tests alongside production, "
                    "tests='only' searches tests exclusively, "
                    "tests='exclude' forces them out. Omit it for the default."
                ),
            }
        if lens == "exclude":
            include_tests = False
        elif lens == "include":
            include_tests = True
        # "only" deliberately leaves include_tests alone: only_tests SUPERSEDES
        # it everywhere it is consumed (the store's role filter, the structural
        # row filter, the empty envelope). Setting both would be dead state
        # that reads like a second, redundant switch.
        only_tests = lens == "only"
        m = mode.strip().lower()

        # Mode aliases
        if m == "function":
            m = "symbols"
        elif m == "reference":
            m = "references"

        _FIND_ALIASES = {
            "service_usage": "references",
            "service_consumers": "references",
            "callers": "references",
            "usages": "references",
            "usage": "references",
            "deps": "dependencies",
            "imports": "dependencies",
        }
        if m in _FIND_ALIASES:
            m = _FIND_ALIASES[m]
        if m in ("text", "regex", "string"):
            use_regex = m == "regex"
            if use_regex:
                # #482: a broken regex is pattern_invalid, never "no match".
                _rx_err = _validate_regex(query)
                if _rx_err is not None:
                    return _pattern_invalid_envelope(query, _rx_err)
            matches = hub.code.search_text(
                root,
                query,
                glob=(path or None),
                regex=use_regex,
                limit=limit,
                include_tests=include_tests,
                only_tests=only_tests,
            )
            for match in matches:
                grant_known_exact_path_read(hub, root, "ai_find", str(match.get("path", "")))
            if not matches:
                # #482 / #478 secondary: every empty result names its
                # reason (path_not_indexed vs no_match) + a next_action.
                if only_tests:
                    return _no_test_match_envelope()
                return _text_empty_envelope(
                    hub,
                    root,
                    scope=(path or None),
                    include_tests=include_tests,
                    for_find=True,
                    probe_query=query,
                    probe_regex=use_regex,
                    probe_limit=limit,
                )
            # #314(4): cap total SERIALIZED output so a broad text/regex match
            # never overflows the tool token cap. An overflow dumps the result
            # to a file under /.claude/, and parsing that file (e.g. python -c)
            # has triggered run_destructive freezes (incident 2026-07-12).
            # Truncate to a char budget + hint to narrow, rather than emit a
            # giant payload. total_matches stays the true count; results_shown
            # reports how many fit.
            import json as _json_cap

            _budget = 60_000
            _kept: list[dict[str, Any]] = []
            _used = 0
            for _mt in matches:
                _sz = len(_json_cap.dumps(_mt, default=str))
                if _kept and _used + _sz > _budget:
                    break
                _kept.append(_mt)
                _used += _sz
            _out: dict[str, Any] = {"total_matches": len(matches), "results": _kept}
            if len(_kept) < len(matches):
                _out["results_shown"] = len(_kept)
                _out["results_truncated"] = True
                _out["hint"] = (
                    f"Showing {len(_kept)} of {len(matches)} matches — output "
                    "capped to stay under the tool limit. Narrow with a tighter "
                    "query, a `path`/glob scope, or a lower `limit`."
                )
            return _surface_knowledge(_out, query=query, root=root)

        # mode=factories is test-heavy by nature, so it auto-opens the door.
        # NOTE (measured 2026-08-29): this line cannot be made to honour
        # tests='exclude', because find_factories hardcodes its own
        # sync_code_files(include_tests=True) index write inside the query —
        # guarding here changes nothing observable. The lens is enforced on
        # the ROWS instead (see _grant), which is the contract callers see.
        if m in ("factories",) and not include_tests:
            include_tests = True

        # NO inline sync here (#74, Empire 2026-06-20). ai_find is a READ-path discovery
        # tool; the index is kept current by edit-time auto-reindex + the
        # ProjectIndexSitter (its poll re-stat catches out-of-host edits and drop-in
        # files). A full sync_code_files(include_tests=True) on every call was a
        # ~2015-file reparse inside the 10s discovery budget — THE cold-index timeout
        # root. If test symbols are wanted, set index.include_tests=true so the sitter
        # indexes them ONCE, not per-find.

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            # tests='only' — the structural modes have no test filter of their
            # own (they rank tests alongside production, which stays the
            # default), so the lens is applied to their rows here. One rule,
            # every mode.
            if lens in ("only", "exclude"):
                _before = result
                result = _apply_tests_lens_to_rows(
                    result, hub, root, keep_tests=only_tests,
                )
                if _lens_result_is_empty(result):
                    if only_tests:
                        return _no_test_match_envelope()
                    # Everything this query matched WAS a test. Say that, and
                    # name the way back — an empty result must never be read
                    # as "the code does not exist" (law 311bf3e6).
                    _dropped = _apply_tests_lens_to_rows(
                        _before, hub, root, keep_tests=True,
                    )
                    if not _lens_result_is_empty(_dropped):
                        return _tests_excluded_envelope(
                            len(_dropped)
                            if isinstance(_dropped, list)
                            else len(_dropped.get("results") or []),
                        )
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "ai_find", root)
                return _surface_knowledge(
                    _cap_find_payload(result), query=query, root=root
                )
            if any(result.get(key) for key in ("matches", "cluster", "results", "result")):
                _grant_paths_from_result(result, "ai_find", root)
            elif "error" not in result and "empty_reason" not in result:
                # #482 catch-all: any dict-shaped mode that comes back with
                # no results and no reason of its own still names one —
                # no ambiguous emptiness anywhere in ai_find.
                result["empty_reason"] = "no_match"
                result.setdefault(
                    "next_action",
                    (
                        "No results for this query — widen/shorten the "
                        "query, try another mode (mode=auto layers "
                        "symbols→filename→text), or verify indexing with "
                        "ai_index_status."
                    ),
                )
            # Backlog #13 item 3: inject minimal staleness signal when
            # the index is missing or empty. Full freshness check lives
            # in ai_index_status (expensive); this covers the two
            # zero-ambiguity cases only.
            return _surface_knowledge(
                _cap_find_payload(_inject_index_staleness(result, root)),
                query=query,
                root=root,
            )

        if m == "symbols":
            mtime_ns = parse_modified_since(modified_since)
            # War AW: tiered search with the no-padding law. Row shape is
            # unchanged (plus score/tier); when the related tier collapses
            # the summary rides a top-level "related_summary" key.
            tiered = hub.code.search_symbols_tiered(
                root,
                query=query,
                kind=kind,
                role=role,
                limit=limit,
                modified_since_ns=mtime_ns,
                strict=strict,
                fuzz=fuzz,
            )
            decorated = hub.code.decorate_symbol_hits(
                root, tiered.get("results") or []
            )
            summary = tiered.get("related_summary")
            if summary:
                return _grant({"results": decorated, "related_summary": summary})
            if not decorated:
                # #482: an empty symbols result names its reason instead of
                # returning a bare [].
                return _grant(
                    {
                        "results": [],
                        "empty_reason": "no_match",
                        "next_action": (
                            "No symbol matched — try a shorter query, "
                            "mode=auto for layered fallback, or fuzz=true "
                            "for the permissive tier list."
                        ),
                    },
                )
            return _grant(decorated)
        if m == "references":
            # #482: internal budget = ~90% of the outer timeout (published
            # by the _references_default_timeout shim), so a slow sweep
            # returns PARTIAL results + timed_out instead of dying at the
            # hard tool kill with nothing.
            _budget = _find_references_budget.get()
            if _budget == _FIND_BUDGET_UNSET:
                _budget = _REFERENCES_DEFAULT_TIMEOUT_S * 0.9
            return _grant(
                hub.code.find_references(
                    root, symbol=query, limit=limit, budget_seconds=_budget,
                ),
            )
        if m == "dependencies":
            return _grant(hub.code.get_dependencies(root, path=query))
        if m == "routes":
            return _grant(hub.code.find_routes(root, query=query, limit=limit))
        if m == "entrypoints":
            return _grant(hub.code.find_entrypoints(root, concept=query, limit=limit))
        if m == "api_consumers":
            # This advertised mode ALWAYS raised "'CodeIndexStore' object has
            # no attribute 'find_api_consumers'" (measured 2026-07-28): the
            # implementation exists on the hotspot service but CodeIndexStore
            # never grew the delegating facade every sibling find_* has. Prefer
            # the facade so a later store change takes over transparently, and
            # fall back to the owning service so an advertised mode can never
            # 500 on a missing delegate again.
            _api_finder = getattr(hub.code, "find_api_consumers", None)
            if _api_finder is None:
                _api_finder = hub.code._hotspots.find_api_consumers
            return _grant(_api_finder(root, endpoint=query, limit=limit))
        if m == "frontend_symbols":
            return _grant(hub.code.find_frontend_symbols(root, query=query, limit=limit))
        if m == "data_structures":
            return _grant(hub.code.find_data_structures(root, query=query, limit=limit))
        if m == "initializers":
            return _grant(
                hub.code.find_initializers(
                    root, path=query if query.strip() else None, limit=limit
                ),
            )
        if m == "mutations":
            return _grant(hub.code.find_mutation_points(root, concept=query, limit=limit))
        if m == "validation":
            return _grant(hub.code.find_validation_surfaces(root, concept=query, limit=limit))
        if m == "async":
            return _grant(hub.code.find_async_boundaries(root, concept=query or None, limit=limit))
        if m == "policy":
            return _grant(hub.code.find_policy_surfaces(root, concept=query, limit=limit))
        if m == "touchpoints":
            return _grant(hub.code.find_ui_backend_touchpoints(root, concept=query, limit=limit))
        if m == "clusters":
            return _grant(hub.code.find_domain_clusters(root, concept=query, limit=limit))
        if m == "transitions":
            return _grant(hub.code.find_transition_points(root, concept=query, limit=limit))
        if m == "factories":
            return _grant(hub.code.find_factories(root, query=query, limit=limit))

        # Auto-discover mode: try symbols → file → text
        if m == "auto":
            mtime_ns = parse_modified_since(modified_since)
            layers_checked = []
            symbols_result = hub.code.search_symbols(
                root,
                query=query,
                kind=kind,
                role=role,
                limit=limit,
                modified_since_ns=mtime_ns,
            )
            layers_checked.append("symbols")
            if lens in ("only", "exclude"):
                symbols_result = _apply_tests_lens_to_rows(
                    symbols_result, hub, root, keep_tests=only_tests,
                )
            if symbols_result:
                _grant_paths_from_result(symbols_result, "ai_find", root)
                return {
                    "results": hub.code.decorate_symbol_hits(root, symbols_result),
                    "source": "symbols",
                    "layers_checked": layers_checked,
                }
            file_result = hub.code.search_code(
                root,
                query=query,
                limit=limit,
                modified_since_ns=mtime_ns,
            )
            layers_checked.append("filename")
            if lens in ("only", "exclude"):
                file_result = _apply_tests_lens_to_rows(
                    file_result, hub, root, keep_tests=only_tests,
                )
            if file_result:
                for item in file_result:
                    grant_known_exact_path_read(hub, root, "ai_find", str(item.get("path", "")))
                return {
                    "results": file_result,
                    "source": "filename",
                    "layers_checked": layers_checked,
                }
            # The text layer's parameter is `text`, not `query` — passing
            # `query=` raised TypeError on EVERY call, so auto's third layer
            # had never once run. It is the fallback every empty envelope in
            # this tool tells the caller to try; a named remedy that cannot
            # execute is not a remedy (law 311bf3e6).
            text_result = hub.code.search_text(
                root,
                query,
                limit=limit,
                include_tests=include_tests,
                only_tests=only_tests,
            )
            layers_checked.append("text")
            if text_result:
                for match in text_result:
                    grant_known_exact_path_read(hub, root, "ai_find", str(match.get("path", "")))
                return {
                    "total_matches": len(text_result),
                    "results": text_result,
                    "source": "text",
                    "layers_checked": layers_checked,
                }
            if only_tests:
                return {
                    **_no_test_match_envelope(),
                    "source": None,
                    "layers_checked": layers_checked,
                }
            return {
                "results": [],
                "empty_reason": "no_match",
                "source": None,
                "layers_checked": layers_checked,
                "hint": "No matches in symbols, filename, or text. Try shorter query.",
            }

        cross_tool_hints = {
            "service": "For service usage analysis try `ai_trace(query='...', mode='service')`.",
            "trace": "For flow/path tracing try `ai_trace(query='...', mode='field_flow'|'api_to_ui'|...)`.",
            "schema": "For entity/field lookups try `ai_schema(query='...', mode='entity'|'field')`.",
            "content": "For raw text search try `ai_text_search(query='...')`.",
            "text": "For raw text search try `ai_text_search(query='...')`.",
            "outline": "For a file overview try `ai_bundle(target='<path>', mode='file')`.",
        }
        hint = None
        low_mode = mode.strip().lower()
        for needle, message in cross_tool_hints.items():
            if needle in low_mode:
                hint = message
                break
        response: dict[str, Any] = {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(_FIND_MODES.keys()),
        }
        if hint:
            response["hint"] = hint
        return response

    _TRACE_MODES = {
        "references": "Callers of a function/method (delegates to ai_find mode=references)",
        "field_flow": "Trace a field across model→service→UI layers (DB/struct fields only; not for function callers)",
        "service": "Find where a service is injected and consumed",
        "model": "Trace a DTO/entity through the full stack",
        "component": "Trace component imports and usage",
        "api_to_ui": "Trace from API endpoint through to UI",
        "css_class": "Find CSS definitions AND HTML/template usages",
        "query_shape": "Trace query patterns + schema relationships",
        "setting": "Trace a configuration setting across layers",
    }

    _FIELD_LIKE_KINDS = frozenset(
        {"field", "property", "column", "schema_field", "record_field", "data_field"},
    )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Trace",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("trace")
    @timed_discovery
    def ai_trace(
        query: str,
        mode: str = "field_flow",
        limit: int = 50,
        max_depth: int | None = None,
        timeout: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        # Concrete list|dict annotation (same as ai_search) so FastMCP emits the
        # structured_content={"result": [...]} sidecar for the bare-list trace
        # modes; the outer wrapper stamps code freshness ALONGSIDE the list
        # (items + mode semantics unchanged).
        """Unified trace tool.

        Modes:
        - references: callers of a function/method (delegates to ai_find).
        - field_flow: DB/struct field lineage across model→service→UI.
          Warning: for callers of a *function*, use references — field_flow
          will match the function by name but won't return its call sites.
        - service, model, component, api_to_ui, css_class, query_shape, setting:
          targeted relationship traces.

        Args:
            query: Symbol/field/concept to trace.
            mode: Which trace mode to use (default: field_flow).

        """
        root = resolve_project_root()
        m = mode.strip().lower()

        def _grant(result: dict[str, Any]) -> dict[str, Any]:
            if any(result.get(key) for key in ("matches", "api", "logic", "ui")):
                _grant_paths_from_result(result, "ai_trace", root)
            # #62 Phase 3: collect any paths surfaced by trace and
            # attach DNT banners. api/logic/ui/matches are all
            # list[dict] with a "path" field on items that carry one.
            paths: list[str] = []
            for key in ("matches", "api", "logic", "ui", "results", "result"):
                items = result.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            p = item.get("path")
                            if p:
                                paths.append(str(p))
            if paths:
                try:
                    from .dnt_banner_injector import maybe_dnt_banners_for_paths

                    _banners = maybe_dnt_banners_for_paths(root, paths)
                except Exception:
                    _banners = []
                if _banners:
                    result["dnt_banners"] = _banners
            return _surface_knowledge(result, query=query, root=root)

        if m == "references":
            return _grant(hub.code.find_references(root, symbol=query, limit=limit))
        if m == "field_flow":
            result = apply_trace_depth(
                hub.code.trace_field_flow(root, field_name=query, limit=limit),
                m,
                max_depth,
            )
            matches = result.get("matches") if isinstance(result, dict) else None
            if isinstance(matches, list) and matches:
                function_like = {"function", "method"}
                kinds = {
                    str(match.get("kind") or "").lower()
                    for match in matches
                    if isinstance(match, dict)
                }
                looks_function_only = (
                    kinds and kinds.issubset(function_like) and not (kinds & _FIELD_LIKE_KINDS)
                )
                if looks_function_only:
                    result["hint"] = (
                        f"field_flow matched only function/method symbols for `{query}`. "
                        "field_flow traces DB/struct field lineage, not function callers. "
                        f'For callers use `ai_trace(query={query!r}, mode="references")` '
                        f'or `ai_find(query={query!r}, mode="references")`.'
                    )
            return _grant(result)
        if m == "service":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_service_usage(root, service_name=query, limit=limit),
                    m,
                    max_depth,
                ),
            )
        if m == "model":
            return _grant(hub.code.trace_model_usage(root, model_name=query, limit=limit))
        if m == "component":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_component_usage(root, component_name=query, limit=limit),
                    m,
                    max_depth,
                ),
            )
        if m == "api_to_ui":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_api_to_ui(root, concept=query, limit=limit),
                    m,
                    max_depth,
                ),
            )
        if m == "css_class":
            return _grant(hub.code.trace_css_class_usage(root, class_name=query, limit=limit))
        if m == "query_shape":
            return _grant(hub.code.trace_query_shape(root, path=query, limit=limit))
        if m == "setting":
            return _grant(
                apply_trace_depth(
                    hub.code.trace_setting_usage(root, setting_name=query, limit=limit),
                    m,
                    max_depth,
                ),
            )
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(_TRACE_MODES.keys()),
        }

    _BUNDLE_MODES = {
        "file": "Full file context: outline + deps + schema hints",
        "service": "Service file + related backend neighbors",
        "component": "Component + imported frontend neighbors",
        "query": "Query hotspot + schema hints + relationship paths",
        "subsystem": "Broad concept analysis across all layers",
        "dependency": "File + resolved dependency chain",
        "partial": "All partial class definitions for a C# type",
        "symbol": "Symbol definition + references + schema matches",
        "style": "CSS selector matches for class names",
        "session": "Session-guided code bundle from context targets",
        "context": "Session-guided ranked context bundle",
        "preset": "Preconfigured bundle (csharp-partial, data-structure, etc.)",
        "tree": "Recursive component import tree",
    }

    _BUNDLE_GENERIC = [
        "file",
        "service",
        "component",
        "query",
        "subsystem",
        "dependency",
        "partial",
        "symbol",
        "style",
        "preset",
        "tree",
    ]

    # What `target` MEANS per bundle mode — rendered into the schema so
    # agents don't guess (file path vs symbol vs concept vs preset name).
    _BUNDLE_MODE_DESCS = {
        "file": "target=FILE PATH → outline + deps + schema hints",
        "service": "target=service FILE PATH → file + related backend neighbors",
        "component": "target=component FILE PATH or name → + imported frontend neighbors",
        "query": "target=concept → query hotspot + schema hints + relationship paths",
        "subsystem": "target=concept → broad cross-layer analysis",
        "dependency": "target=FILE PATH → file + resolved dependency chain",
        "partial": "target=C# TYPE NAME → all partial-class definitions",
        "symbol": "target=SYMBOL NAME → definition + references + schema matches",
        "style": "target=CSS CLASS NAME → selector matches",
        "preset": "target=preset name (csharp-partial, data-structure, ...) → preconfigured bundle",
        "tree": "target=component FILE PATH → recursive import tree",
        "session": "target=focus hint + session_id → session-guided code bundle",
        "context": "target=focus hint + session_id → ranked context bundle",
    }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Bundle",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("bundle")
    @timed_discovery
    def ai_bundle(
        target: str,
        mode: str = "file",
        session_id: str | None = None,
        limit: int = 20,
        timeout: int | None = None,
        include_content: int = 0,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        # list|dict annotation: some bundle modes return a bare list (see _grant's
        # dict|list[dict] type). FastMCP wraps lists into the
        # structured_content={"result": [...]} sidecar so the outer wrapper stamps
        # code freshness beside the list (items + mode semantics unchanged).
        """Unified bundle tool — replaces all code_get_*_bundle tools.

        Modes: file, service, component, query, subsystem, dependency, partial,
        symbol, style, session, context, preset, tree.

        Args:
            target: File path, symbol name, concept, CSS class, or preset spec depending on mode.
            mode: Which bundle mode to use.
            session_id: Required for session/context modes.
            include_content: mode="file" only — inline the first N lines of
                the file in the response (size-capped, budget-labeled when
                trimmed). Default 0 = off (#481, HH's ask: the payload
                should not hide behind an unreadable sqlite artifact).

        """
        root = resolve_project_root()
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "ai_bundle", root)
                return _surface_knowledge(result, query=target, root=root)
            if any(
                result.get(key)
                for key in (
                    "primary_files",
                    "related_files",
                    "files",
                    "symbols",
                    "matches",
                )
            ):
                _grant_paths_from_result(result, "ai_bundle", root)
            # mode="file" returns a single-file shape {path, outline, ...} with
            # none of the keys above; grant the path directly so follow-up
            # ai_get_lines does not hit the discovery gate.
            if not result.get("missing") and isinstance(result.get("path"), str):
                grant_known_exact_path_read(hub, root, "ai_bundle", str(result["path"]))
            bundle_type = m
            file_count = len(result.get("files") or []) if isinstance(result, dict) else 0
            symbol_count = len(result.get("symbols") or []) if isinstance(result, dict) else 0
            if not file_count and isinstance(result, dict):
                file_count = len(result.get("primary_files") or []) + len(
                    result.get("related_files") or [],
                )
            summary_bits: list[str] = []
            if file_count:
                summary_bits.append(f"files={file_count}")
            if symbol_count:
                summary_bits.append(f"symbols={symbol_count}")
            if result.get("missing"):
                inline_summary = f"Bundle `{bundle_type}` for `{target}` is missing."
            else:
                suffix = f" ({', '.join(summary_bits)})" if summary_bits else ""
                inline_summary = f"Bundle `{bundle_type}` prepared for `{target}`{suffix}."
            compact = runtime.build_artifact_backed_result(
                root,
                inline_summary=inline_summary,
                payload=result,
                artifact_name=f"code-bundle-{bundle_type}-{target}",
                structured_summary={
                    "mode": bundle_type,
                    "target": target,
                    "missing": bool(result.get("missing")),
                    "file_count": file_count,
                    "symbol_count": symbol_count,
                },
            )
            result.update(compact)
            return _surface_knowledge(result, query=target, root=root)

        if m == "file":
            _bundle = _grant(hub.code.get_file_bundle(root, path=target))
            # #62 Phase 3: surface DNT banner once-per-session per
            # family. Only mode=file has a single concrete path; the
            # other modes target concepts/symbols and get no banner.
            try:
                from .dnt_banner_injector import maybe_dnt_banner_for_read

                _banner = maybe_dnt_banner_for_read(root, target)
            except Exception:
                _banner = ""
            if _banner and isinstance(_bundle, dict):
                _bundle["dnt_banner"] = _banner
            # #481 (War KK): opt-in inline content — first N lines with the
            # canonical budget label, so the payload is READABLE in-band
            # instead of hidden behind the sqlite artifact.
            if (
                include_content
                and isinstance(_bundle, dict)
                and not _bundle.get("missing")
            ):
                from .session_response_ledger import budget_label

                n = max(1, min(int(include_content), INLINE_CONTENT_MAX_LINES))
                rel = str(_bundle.get("path") or target)
                # #648 (P2 provenance stamp): the inline-content fold
                # serves INDEXED content and never calls
                # read_pipeline.gate(), so it never computed a trust
                # zone. Additive-only cheap stamp — NO new
                # gating/refusals.
                try:
                    from .path_trust_zone import classify_path

                    _bundle["zone"] = str(classify_path(rel, project_root=root))
                except Exception:
                    _bundle["zone"] = "PathTrustZone.PROJECT_INTERNAL"
                if rel.replace("\\", "/").lstrip("./").startswith("third_party/"):
                    _bundle["vendored"] = True
                try:
                    lines_res = file_get_lines(
                        root,
                        rel,
                        start_line=1,
                        count=n,
                        show_line_numbers=True,
                    )
                except Exception as exc:  # noqa: BLE001 — inline is best-effort
                    _bundle["inline_content_error"] = str(exc)
                else:
                    if isinstance(lines_res, dict):
                        inline_lines = [
                            str(x) for x in (lines_res.get("lines") or [])
                        ]
                        total_lines = int(
                            lines_res.get("total") or len(inline_lines)
                        )
                        # Char budget: trim at line boundaries.
                        kept: list[str] = []
                        used = 0
                        for ln in inline_lines:
                            if kept and used + len(ln) + 1 > INLINE_CONTENT_CHAR_BUDGET:
                                break
                            kept.append(ln)
                            used += len(ln) + 1
                        _bundle["inline_content"] = "\n".join(kept)
                        if len(kept) < total_lines:
                            _bundle["inline_content_budget"] = budget_label(
                                len(kept),
                                total_lines,
                                f"page with ai_get_lines(path='{rel}', "
                                f"start_line={len(kept) + 1})",
                            )
            return _bundle
        if m == "service":
            return _grant(hub.code.get_service_bundle(root, path=target, limit=limit))
        if m == "component":
            return _grant(hub.code.get_component_bundle(root, path=target, limit=limit))
        if m == "query":
            return _grant(hub.code.get_query_bundle(root, path=target, limit=limit))
        if m == "subsystem":
            return _grant(hub.code.get_subsystem_bundle(root, concept=target, limit=limit))
        if m == "dependency":
            return _grant(hub.code.get_dependency_bundle(root, path=target, limit=limit))
        if m == "partial":
            return _grant(hub.code.get_partial_bundle(root, symbol=target, limit=limit))
        if m == "symbol":
            return _grant(hub.code.get_symbol_bundle(root, symbol=target, limit=limit))
        if m == "style":
            # Accept comma/space separated class names
            if isinstance(target, str):
                class_names = [s.strip() for s in re.split(r"[,\s]+", target) if s.strip()]
            else:
                class_names = target
            return _grant(hub.code.get_style_bundle(root, class_names=class_names, limit=limit))
        if m == "session":
            if not session_id:
                return {"error": "session_id is required for session mode"}
            return _grant(hub.code.get_session_ai_bundle(root, session_id=session_id))
        if m == "context":
            if not session_id:
                return {"error": "session_id is required for context mode"}
            return _grant(hub.code.get_context_bundle(root, session_id=session_id, limit=limit))
        if m == "preset":
            # target format: "preset_name:value" e.g. "csharp-partial:FormPdfService"
            parts = target.split(":", 1)
            preset = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            return _grant(hub.code.get_preset_bundle(root, preset=preset, value=value, limit=limit))
        if m == "tree":
            return _grant(hub.code.get_component_tree(root, path=target, limit=limit))
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": list(_BUNDLE_MODES.keys()),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "AI Schema",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("schema")
    @timed_discovery
    def ai_schema(
        query: str,
        mode: str = "entities",
        limit: int = 50,
        include_related: bool = False,
        timeout: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Unified schema tool — replaces all schema_find_*, schema_get_*, schema_trace_* tools.

        Modes: entities, entity, batch_entity, field, constructor, properties, trace_flow, trace_path.

        Args:
            query: Entity name, field name, or "source→target" for trace_path mode.
            mode: Which schema operation to run.

        """
        root = resolve_project_root()
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "ai_schema", root)
                return result
            if any(result.get(key) for key in ("entities", "fields", "matches", "properties")):
                _grant_paths_from_result(result, "ai_schema", root)
            return result

        if m == "entities":
            return _grant(hub.schema.find_schema_entities(root, query=query or None, limit=limit))
        if m == "entity":
            return _grant(hub.schema.get_schema_entity(root, entity_name=query))
        if m == "batch_entity":
            names = [part.strip() for part in re.split(r"[\n,]+", query) if part.strip()]
            return _grant(hub.schema.get_schema_entities_batch(root, entity_names=names))
        if m == "field":
            return _grant(hub.schema.find_schema_field(root, field_name=query, limit=limit))
        if m == "constructor":
            return _grant(
                hub.schema.get_constructor_params(
                    root,
                    entity_name=query,
                    include_related=include_related,
                ),
            )
        if m == "properties":
            return _grant(hub.schema.get_entity_properties(root, entity_name=query))
        if m == "trace_flow":
            return _grant(hub.schema.trace_entity_flow(root, entity_name=query, limit=limit))
        if m == "trace_path":
            # Accept "Source→Target" or "Source -> Target" or "Source,Target"
            parts = re.split(r"[→\->,]+", query, maxsplit=1)
            if len(parts) < 2:
                return {
                    "error": "trace_path requires 'Source→Target' format",
                    "query": query,
                }
            return _grant(
                hub.schema.trace_relationship_path(
                    root,
                    source_entity=parts[0].strip(),
                    target_entity=parts[1].strip(),
                    limit=limit,
                ),
            )
        return {
            "error": f"Unknown mode: {mode}",
            "available_modes": [
                "entities",
                "entity",
                "batch_entity",
                "field",
                "constructor",
                "properties",
                "trace_flow",
                "trace_path",
            ],
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Legacy Tools (deprecated — use unified tools above)
    # ═══════════════════════════════════════════════════════════════════════

    def memory_promote(
        content: str = "",
        kind: str = "workflow-rule",
        law_id: str = "",
        keywords: list[str] | None = None,
        reason: str = "",
        content_hash: str = "",
        captured_hash: str = "",
        operation: str = "",
        request_id: str = "",
        approve: bool = False,
    ) -> dict[str, Any]:
        """Promote a kingdom memory entry into the EMPIRE tier (global law — sealed once, read in every kingdom).

        The trusted path runs THROUGH the archive (§XIII: evidence may enter
        the archive; law enters only through the throne):
          * the content must ALREADY exist in kingdom memory — capture it
            first; free text is refused (arbitrary law injection),
          * it must be empire-worthy: portable kind-shape, no machine-
            specific markers (absolute paths, IPs),
          * `reason` is required and audited,
          * the dedicated memory.set_global_law permission gates the actor.
        Re-promotion upserts the same law row (idempotent). Provenance
        (source project) is stamped in the law row's `source`.

        Promote-by-reference (#451): pass content_hash= (or captured_hash=)
        INSTEAD of content — the archived kingdom row is resolved by hash
        and its text promoted verbatim-by-construction (by-hash cannot
        alter text in flight). Unknown hash → not_in_kingdom_memory.
        Called with NO content and NO hash → lists pending empire-candidate
        rows ({hash, kind, snippet, captured_at}) instead of promoting.
        ``operation=retire_request|retire_decide`` routes through the laddered
        retirement service while keeping this internal tool as the one home.
        """
        project_root = resolve_project_root()
        if (operation or "").strip():
            from .global_law_retirement import handle_global_law_retirement

            return handle_global_law_retirement(
                project_root,
                hub,
                mode=operation,
                law_id=law_id,
                request_id=request_id,
                reason=reason,
                approve=approve,
            )
        _wanted_hash = (content_hash or captured_hash or "").strip().lower()
        if not (content or "").strip() and not _wanted_hash:
            # #451 candidates listing (also surfaced as ai_memory
            # mode='candidates'): a read, so no reason/RBAC required.
            cands = _empire_candidate_rows(project_root)
            return {
                "ok": True,
                "mode": "candidates",
                "count": len(cands),
                "candidates": cands,
                "message": (
                    "pending empire-candidate rows; promote one via "
                    "content_hash=<hash> + reason"
                ),
            }
        if not (reason or "").strip():
            return {
                "ok": False,
                "reason": "reason_required",
                "message": "promotion to empire law requires a non-empty audited reason",
            }
        from .permission_catalog import PERM_MEMORY_SET_GLOBAL_LAW

        _rbac = hub.require_permission(
            project_root,
            PERM_MEMORY_SET_GLOBAL_LAW,
            scope_type="project",
            scope_id=str(project_root).replace("\\", "/"),
            tool_name="memory_promote",
            extra_payload={"kind": kind, "law_id": law_id or ""},
        )
        if not _rbac["ok"]:
            return _rbac
        from .memory_fit import assess_fit, is_empire_candidate

        # #451 promote-by-reference: resolve the archived row by hash. The
        # resolved text IS archive text by construction, so the fit-check
        # (which exists to prove "content already lives in the archive")
        # is satisfied structurally — by-hash cannot alter text in flight.
        resolved_by_hash = False
        source_path = ""
        if _wanted_hash and not (content or "").strip():
            hit = _resolve_kingdom_text_by_hash(project_root, _wanted_hash)
            if hit is None:
                return {
                    "ok": False,
                    "reason": "not_in_kingdom_memory",
                    "message": (
                        "content_hash does not resolve to any archived kingdom "
                        "memory row — the trusted path runs through the archive: "
                        "capture first (memory_capture), then promote by its hash."
                    ),
                }
            content, _row_kind, source_path = hit
            resolved_by_hash = True

        worthy, why = is_empire_candidate(kind, content)
        if not worthy:
            return {
                "ok": False,
                "reason": "not_empire_worthy",
                "message": f"refusing empire promotion: {why}. Genericize the content first.",
            }
        if not resolved_by_hash:
            fit = assess_fit(
                content,
                lambda q, limit=10: hub.index.search_memory(project_root, query=q, limit=limit),
                fetch_text=lambda row: _memory_entry_full_text(hub, project_root, row),
            )
            if fit["verdict"] != "duplicate":
                return {
                    "ok": False,
                    "reason": "not_in_kingdom_memory",
                    "fit": fit,
                    "message": (
                        "the trusted path runs through the archive: capture this "
                        "into kingdom memory first (memory_capture), then promote "
                        "the archived entry."
                    ),
                }
        import hashlib as _hashlib

        from .empire_audit_store import project_id_for_root
        from .global_law_store import upsert_global_law

        _pid = project_id_for_root(project_root)
        lid = (law_id or "").strip() or (
            "promoted-" + _hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:12]
        )
        law = upsert_global_law(
            law_id=lid,
            kind=(kind or "workflow-rule").strip().lower(),
            content=content,
            keywords=",".join(
                (k or "").strip().lower() for k in (keywords or []) if (k or "").strip()
            ),
            source=f"promotion:{_pid}",
        )
        try:
            hub.execution.record_event(
                project_root,
                event_kind="memory_promoted_to_empire",
                source_kind="memory_promote",
                capability_name="memory_promote",
                action_kind="memory",
                target_entity=lid,
                status="ok",
                payload={
                    "law_id": lid,
                    "kind": kind,
                    "checksum": law.get("checksum", ""),
                    "reason": (reason or "").strip()[:500],
                    "project_id": _pid,
                },
            )
        except Exception:
            pass
        # #375 Phase 2 (c): the promoted row also lands in the machine-global
        # empire palace as a drawer. Best-effort + fail-quiet BY CONTRACT —
        # the law is already sealed in global_law (canonical); a palace
        # hiccup must never fail (or hang: the leg is timeboxed) promotion.
        empire_palace_receipt: dict[str, Any] = {"ok": False, "reason": "ingest_failed"}
        try:
            from .empire_palace import ingest_promoted_law

            empire_palace_receipt = ingest_promoted_law(
                law_id=lid,
                kind=(kind or "workflow-rule").strip().lower(),
                content=content,
            )
        except Exception:
            pass
        out: dict[str, Any] = {
            "ok": True,
            "law_id": lid,
            "checksum": law.get("checksum", ""),
            "source": f"promotion:{_pid}",
            "promoted_by": "hash" if resolved_by_hash else "content",
            "empire_palace": empire_palace_receipt,
            "message": "sealed as empire law; read in every kingdom via the global-law surface",
        }
        if source_path:
            out["kingdom_path"] = source_path
        return out

    def memory_capture(
        kind: str,
        content: str,
        target_hint: str | None = None,
        keywords: list[str] | None = None,
        severity: str = "normal",
        trigger: str = "topic",
        priority: str = "normal",
        injection_mode: str = "pointer",
        anchor_symbols: list[dict[str, str]] | None = None,
        skip_fit_check: bool = False,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Persist a DURABLE fact to project memory. Memory is the project's migration payload — loaded on a fresh machine, would an agent still need this to work correctly? If no, DON'T capture.

        Accepted kinds (strict — content is written to the kind's canonical file):
            invariant        → system/invariants.md    (always-true constraint: schema, security, architecture boundary)
            workflow-rule    → rules/workflow.md       (how to operate: git, deploy, session, task lifecycle)
            preference       → rules/communication.md  (user taste: tone, verbosity, naming, response shape)
            infrastructure   → config/infrastructure.md (non-code config: VPS IP, SSH user, PM2 names, DB credential location)
            caveat           → system/caveats.md       (non-obvious trap: platform quirks, timing issues, 3rd-party bugs)
            related-project  → related-projects/FIXES_BY_OTHER_AGENTS.md

        DO NOT capture — these are REJECTED with a redirect message:
            plan / phase / roadmap / status  → .MEMORY/plans/ or plan_create_from_spec
            bug                              → issue tracker (extract only the durable invariant)
            log / changelog                  → .MEMORY/archive/ or session journal
            feedback                         → .MEMORY/roadmap-feedback/ or archive
            snapshot / inventory / exploration → code indexer has inventories; write only the RULES as kind='invariant'
            domain / project                 → legacy permissive buckets; pick a strict kind above

        Also rejected (regardless of kind):
            - Content with phase-tracking markers (## Phase N, Phase 1 DONE)
            - Content starting with 'Source: agent exploration' / 'Live analysis of'
            - Content with Priority: CRITICAL / Root cause / Fix needed structure
            - Content with feedback-log / rating patterns (v2 feedback, X/10)
            - target_hint pointing at reserved filenames in wrong folders (plans.md in domains/)

        Args:
            kind: One of the 6 accepted kinds above, OR a common synonym
                auto-aliased onto them. Accepted synonyms include:
                security/policy/schema/contract/constraint/always/must/never → invariant
                rule/rules/workflow/process/procedure/ops/runbook/guideline/standard/convention → workflow-rule
                style/tone/taste/ui/communication → preference
                note/gotcha/trap/warning/quirk/pitfall → caveat
                deploy/deployment/env/environment/credentials → infrastructure
                related/cross-project/other-project → related-projects
                Plus back-compat: rule/system/config/user/reference/related_project.
            content: The durable fact to persist (any language). Test: "loaded on a fresh machine in 6 months, would an agent need this?"
            target_hint: Optional explicit target — filename or relative path. If omitted, kind's canonical file is used. Bare filename routes to the kind's folder.

        """
        project_root = resolve_project_root()
        # Sovereign-severity guard (Conductor Doctrine #1): the capture API
        # refuses to flag any entry as sovereign. Sovereign routes are written
        # only via direct edit by the seat-holder, never via this surface.
        if (severity or "").strip().lower() == "sovereign":
            return {
                "ok": False,
                "reason": "sovereign_severity_refused",
                "message": (
                    "severity='sovereign' is reserved for files owned by the "
                    "conductor/co-conductor (Doctrine #1). memory_capture cannot "
                    "register sovereign routes. The seat-holder edits sovereign "
                    "files directly; this API does not grant sovereign authority."
                ),
            }
        # RBAC enforcement (2026-04-21): high-authority memory kinds
        # (workflow-rule, invariant) require admin.manage_config at
        # project scope. They alter how AIDOCS orchestrates — treat
        # same as a config write. Other kinds (preference, caveat,
        # infrastructure, related-project) stay operator-writable.
        _kind_lower = (kind or "").strip().lower()
        _gated_kinds = {
            "workflow-rule",
            "workflow_rule",
            "workflow",
            "invariant",
            "security",
            "policy",
            "rule",
            "rules",  # common synonyms for workflow-rule
        }
        if _kind_lower in _gated_kinds:
            _rbac = hub.require_permission(
                project_root,
                "admin.manage_config",
                scope_type="project",
                scope_id=str(project_root).replace("\\", "/"),
                tool_name="memory_capture",
                extra_payload={"kind": kind, "target_hint": target_hint or ""},
            )
            if not _rbac["ok"]:
                return _rbac
        # ── Privacy floor (#363 / #375 Phase 3): the credential write-guard
        # runs BEFORE anything persists — the body is scrubbed before the
        # canonical sqlite write, before the projection stage, before any
        # drawer. Fail-quiet-but-masked: a guard failure degrades the body
        # to a mask (session_store's discipline), never persists unscanned.
        from .session_store import _scrub_credential_text

        content = _scrub_credential_text(content)
        # #684 SINK SCREEN: a memory capture is agent prose persisted to
        # sqlite, not to a file the write guard scans. Repair, never refuse —
        # a refusal here would wedge an agent mid-task. Signature-only, so the
        # Romanian lemmas #680 seeded (U+0103) pass through untouched.
        from .agent_prose_screen import repair_agent_prose

        content = repair_agent_prose(content, sink="memory_capture")
        # #601 capture-time scope routing: classification PROPOSES only.
        # Every capture remains kingdom/project evidence until the dedicated
        # global-law authority explicitly seals it through memory_promote.
        # Invalid/ambiguous anchors fail closed to PROJECT inside the classifier.
        from .memory_scope_classifier import GLOBAL as _GLOBAL_MEMORY_SCOPE
        from .memory_scope_classifier import classify_scope as _classify_memory_scope

        _scope_proposal = _classify_memory_scope(
            content,
            kind,
            anchors=list(anchor_symbols or []),
        )
        # ── Pre-add FIT check (Empire directive 2026-07-06, §X/§XI two-tier
        # memory): search existing memory BEFORE adding. Duplicate → refuse
        # with a pointer to the existing entry (skip_fit_check=True
        # overrides); neighbor → proceed, surfacing the near matches; a
        # broken index degrades to novel (triage aid, never a write-blocker).
        from .memory_fit import assess_fit, is_empire_candidate

        fit: dict[str, Any] = {"verdict": "skipped", "matches": []}
        if not skip_fit_check:
            fit = assess_fit(
                content,
                lambda q, limit=10: hub.index.search_memory(project_root, query=q, limit=limit),
                fetch_text=lambda row: _memory_entry_full_text(hub, project_root, row),
            )
            if fit["verdict"] == "duplicate":
                return {
                    "ok": False,
                    "reason": "duplicate_memory",
                    "fit": fit,
                    "message": (
                        "an existing memory entry already covers this "
                        f"(best fit_score={fit['best_score']}). Update that entry "
                        "instead, or re-invoke with skip_fit_check=True if it is "
                        "genuinely distinct."
                    ),
                }
        # ── NEIGHBOR-MERGE (#144): a `neighbor` verdict FOLDS the new
        # content into the best-matching existing row (same _append_bullet
        # consolidation capture_memory already does for same-path adds)
        # instead of fragmenting memory with a near-duplicate row.
        # Floors: never across DIFFERENT kinds; never into a sovereign
        # path (fail-closed if the sovereign set can't be read); only on
        # a real neighbor verdict (>= NEIGHBOR_THRESHOLD by construction);
        # skip_fit_check or an explicit target_hint keeps the caller's
        # placement — both remain force-separate-row escape hatches.
        # Idempotent: the folded row then covers the content, so a
        # re-capture scores `duplicate` and is refused above.
        neighbor_merged_into = ""
        if (
            fit.get("verdict") == "neighbor"
            and not skip_fit_check
            and not (target_hint or "").strip()
        ):
            _best = next(iter(fit.get("matches") or []), None) or {}
            _best_path = str(_best.get("path") or "").strip()
            _best_kind = str(_best.get("kind") or "").strip().lower()
            try:
                _resolved_kind = hub.memory.normalize_kind(kind)
            except Exception:
                _resolved_kind = ""
            try:
                from .memory_store import _SOVEREIGN_MEMORY_PATHS

                _sovereign_target = _best_path.lower() in {
                    p.lower() for p in _SOVEREIGN_MEMORY_PATHS
                }
            except Exception:
                _sovereign_target = True  # fail-closed on the sovereign floor
            if (
                _best_path
                and _resolved_kind
                and _best_kind == _resolved_kind
                and not _sovereign_target
            ):
                target_hint = _best_path
                neighbor_merged_into = _best_path
        # Empire-worthiness: CANDIDATE flag only — §XIII: evidence may enter
        # the archive; law enters only through the throne. A portable shape is
        # insufficient by itself: the deterministic scope classifier must also
        # propose GLOBAL with a required seal. Anchored/ambiguous rows stay local.
        _portable_candidate, _empire_reason = is_empire_candidate(kind, content)
        _empire_candidate = bool(
            _portable_candidate
            and _scope_proposal.scope == _GLOBAL_MEMORY_SCOPE
            and _scope_proposal.requires_seal
        )
        result = hub.memory.capture_memory(
            project_root,
            kind=kind,
            content=content,
            target_hint=target_hint,
        )
        # Refresh the memory index so memory_search can find what was just written.
        try:
            hub.index.sync_memory_files(project_root)
        except Exception:
            pass

        # Compute target_rel once for both route registration and audit.
        target_rel = ""
        try:
            memory_root = project_root / ".MEMORY"
            target_rel = result.target_file.relative_to(memory_root).as_posix()
        except Exception:
            pass

        # Register/update the memory route + keywords in one call. Closes the
        # discovery loop: the same call that wrote the bullet now makes it
        # findable. No hand-edit of frontmatter required (Brick 1, scribe).
        normalized_keywords = tuple(
            (k or "").strip().lower() for k in (keywords or []) if (k or "").strip()
        )
        normalized_anchors: list[dict[str, str]] = []
        for entry in anchor_symbols or []:
            if not isinstance(entry, dict):
                continue
            sym = str(entry.get("symbol") or "").strip()
            file_field = str(entry.get("file") or "").strip()
            kind_field = str(entry.get("kind") or "").strip().lower()
            # Three shapes accepted:
            #   {"symbol": "X"}                         → symbol-anchored, any file
            #   {"symbol": "X", "file": "path"}         → symbol-anchored, scoped
            #   {"file": "path", "kind": "file"}        → file-anchored
            #   {"file": "path"}                        → file-anchored (kind auto)
            # An entry with neither symbol nor file is dropped.
            if not sym and not file_field:
                continue
            if not sym:
                # File-only anchor: gate fires on any edit to this file
                # regardless of symbol target. Used for doctrines that
                # govern a whole file's shape, not a single function.
                normalized_anchors.append(
                    {
                        "symbol": "",
                        "file": file_field,
                        "kind": "file",
                    },
                )
                continue
            normalized_anchors.append(
                {
                    "symbol": sym,
                    "file": file_field,
                    "kind": kind_field or "symbol",
                },
            )

        has_metadata = bool(
            normalized_keywords
            or severity != "normal"
            or trigger != "topic"
            or priority != "normal"
            or injection_mode != "pointer"
            or normalized_anchors,
        )
        route_id_recorded: int | None = None
        anchors_recorded: int = 0
        # Phase-8 hardening (2026-05-19): track wiring failures so the
        # post-capture audit emits memory_route_lag / memory_anchor_lag
        # events the operator/dashboard can see. The canonical memory
        # row is already on disk in sqlite at this point; the lag
        # events are about DISCOVERABILITY, not the data itself.
        route_lag_error: str | None = None
        anchor_lag_errors: list[dict[str, str]] = []
        # ── Bounded capture analyzer (memory-loop seal, 2026-07-09) ──
        # Derives route terms (NLP substance + code-index symbols + target
        # path) and candidate semantic_guess anchors AFTER the canonical
        # write. Never blocks: on timeout/error it returns empty+degraded.
        derived_terms: tuple[str, ...] = ()
        derived_anchor_candidates: tuple = ()
        analyzer_degraded = ""
        if target_rel and injection_mode != "skip":
            try:
                from .memory_capture_analyzer import analyze_capture

                _analysis = analyze_capture(
                    project_root,
                    content=content,
                    kind=kind,
                    target_rel=target_rel,
                    explicit_keywords=normalized_keywords,
                )
                derived_terms = _analysis.derived_terms
                derived_anchor_candidates = _analysis.candidate_anchors
                if _analysis.degraded:
                    analyzer_degraded = _analysis.reason
            except Exception as _an_exc:
                analyzer_degraded = type(_an_exc).__name__
        # Route registration is ALWAYS-ON unless injection_mode='skip':
        # a capture with no explicit keywords still gets a route carrying
        # its derived terms, so it is discoverable from day one. Explicit
        # terms outrank derived (provenance law in upsert_memory_route).
        if target_rel and injection_mode != "skip":
            try:
                route_id_recorded = hub.index.upsert_memory_route(
                    project_root,
                    target_path=target_rel,
                    severity=severity,
                    trigger=trigger,
                    priority=priority,
                    injection_mode=injection_mode,
                    source="capture",
                    keywords=normalized_keywords,
                    derived_keywords=derived_terms,
                    locale="*",
                )
            except Exception as _route_exc:
                # Route registration must not break capture. The bullet is
                # already written; backfill can reconcile later. Capture
                # the error class for the lag audit event below.
                route_lag_error = type(_route_exc).__name__
            if route_id_recorded is not None and normalized_anchors:
                for anchor in normalized_anchors:
                    try:
                        hub.index.upsert_memory_anchor(
                            project_root,
                            route_id=route_id_recorded,
                            symbol_name=anchor["symbol"],
                            file_path=anchor["file"],
                            anchor_kind=anchor["kind"],
                        )
                        anchors_recorded += 1
                        # 2026-05-16 ai_vocab-removal side effect: when
                        # the anchor is a DOMAIN, also register the
                        # domain name in intent_lemma_sets kind=
                        # 'domain_hint'. This is the ONLY auto-register
                        # path open to agents — the other 8 vocab kinds
                        # (action_token / intent_guard / etc.) stay
                        # dashboard-only. Normalisation: lowercase +
                        # strip; deduped by intent_tokens_store's
                        # INSERT OR IGNORE.
                        if (anchor.get("kind") or "").lower() == "domain":
                            try:
                                from . import intent_tokens_store as _its

                                token_norm = str(anchor.get("symbol") or "").strip().lower()
                                if token_norm:
                                    _its.seed_kind_rows(
                                        "en",
                                        "domain_hint",
                                        [
                                            {
                                                "parent_key": token_norm,
                                                "tokens": [token_norm],
                                                "attrs": {
                                                    "source_capture": True,
                                                    "auto_registered": True,
                                                },
                                            },
                                        ],
                                        source="memory_capture",
                                    )
                            except Exception:
                                # Auto-register is best-effort. The
                                # anchor row already landed; vocab
                                # registration failure does not undo
                                # the capture.
                                pass
                    except Exception as _anchor_exc:
                        # Per-anchor failure must not break capture or
                        # other anchors. Record what worked, drop the
                        # rest. Capture the failure for the lag audit.
                        anchor_lag_errors.append(
                            {
                                "symbol": str(anchor.get("symbol") or ""),
                                "file": str(anchor.get("file") or ""),
                                "kind": str(anchor.get("kind") or ""),
                                "error_class": type(_anchor_exc).__name__,
                            },
                        )
            # Derived candidate anchors (memory-loop seal): registered at
            # confidence='semantic_guess' — advisory tier that SURFACES on
            # read/edit goggles but NEVER blocks an edit (edit gates skip
            # semantic_guess). Only explicit operator anchors block. §31:
            # auto-derived signals must not become law.
            derived_anchors_recorded = 0
            if route_id_recorded is not None and derived_anchor_candidates:
                explicit_anchor_keys = {
                    (a.get("symbol", ""), a.get("file", "")) for a in normalized_anchors
                }
                for cand in derived_anchor_candidates:
                    if (cand.symbol, cand.file) in explicit_anchor_keys:
                        continue
                    try:
                        hub.index.upsert_memory_anchor(
                            project_root,
                            route_id=route_id_recorded,
                            symbol_name=cand.symbol,
                            file_path=cand.file,
                            anchor_kind=cand.kind or "symbol",
                            confidence="semantic_guess",
                            source="capture_analyzer",
                        )
                        derived_anchors_recorded += 1
                    except Exception:
                        # Best-effort: a failed derived anchor is silence,
                        # not lag — nothing the operator relied on is lost.
                        pass
        else:
            derived_anchors_recorded = 0

        # ── #375 Phase 3 (B): smallest-leaf anchoring. Every capture also
        # anchors at the smallest resolvable code-index leaf (function/
        # method/class/symbol via code_outlines, file via code_files) from
        # the target_hint + content mentions — pure owned-index sqlite
        # reads (#448 Consumer-B precedent), semantic_guess tier only,
        # best-effort (a resolver fault never touches the capture).
        leaf_anchors_recorded = 0
        if route_id_recorded is not None and injection_mode != "skip":
            try:
                from .memory_leaf_anchoring import register_leaf_anchors

                leaf_anchors_recorded = register_leaf_anchors(
                    hub.index,
                    project_root,
                    route_id=route_id_recorded,
                    content=content,
                    target_hint=target_hint or "",
                    skip_keys={
                        (a.get("symbol", ""), a.get("file", ""))
                        for a in normalized_anchors
                    },
                )
            except Exception:
                leaf_anchors_recorded = 0

        # Audit emission (Brick 1 Phase 6, scribe). Stamps target chosen,
        # keywords registered, severity tag, route_id when route metadata
        # was set. 120% enforceable: every capture leaves a forensic trace
        # the operator can follow.
        try:
            hub.execution.record_event(
                project_root,
                event_kind="memory_capture",
                source_kind="mcp_tool",
                capability_name="memory_capture",
                action_kind="memory",
                target_entity=target_rel,
                status="success",
                payload={
                    "kind": kind,
                    "target_hint": target_hint or "",
                    "severity": severity,
                    "trigger": trigger,
                    "priority": priority,
                    "injection_mode": injection_mode,
                    "keywords_registered": list(normalized_keywords),
                    "route_metadata_set": has_metadata,
                    "route_id": route_id_recorded,
                    "anchors_recorded": anchors_recorded,
                    "derived_keywords": list(derived_terms),
                    "derived_anchors_recorded": derived_anchors_recorded,
                    # #375 Phase 3 (B): smallest-leaf anchors registered
                    # from target_hint + content mentions.
                    "leaf_anchors_recorded": leaf_anchors_recorded,
                    "analyzer_degraded": analyzer_degraded,
                    "fit_verdict": str(fit.get("verdict") or ""),
                    "neighbor_merged_into": neighbor_merged_into,
                    "scope_proposal": _scope_proposal.scope,
                    "scope_confidence": _scope_proposal.confidence,
                    "scope_requires_seal": _scope_proposal.requires_seal,
                    "scope_signals": list(_scope_proposal.signals),
                },
            )
        except Exception:
            # Audit must not break capture. Loud failures here would be
            # worse than a missing audit row; the bullet is already on disk.
            pass

        # Phase-8 export lag surfacing (2026-05-19): when the canonical
        # sqlite write succeeded but the markdown export failed, emit
        # ONE durable audit event AND surface the lag in the tool
        # response so the next agent turn sees the degraded state.
        # The sqlite row IS authoritative; this is purely about
        # visibility into "the markdown on disk is stale".
        export_lag = bool(getattr(result, "sqlite_ok", True)) and not bool(
            getattr(result, "markdown_ok", True),
        )
        if export_lag:
            try:
                hub.execution.record_event(
                    project_root,
                    event_kind="memory_export_lag",
                    source_kind="memory_capture",
                    capability_name="memory_capture",
                    action_kind="memory",
                    target_entity=target_rel,
                    status="degraded",
                    payload={
                        "path": target_rel,
                        "kind": kind,
                        "sqlite_checksum": getattr(
                            result,
                            "sqlite_checksum",
                            None,
                        ),
                        "error_class": getattr(
                            result,
                            "markdown_error",
                            None,
                        ),
                        "note": (
                            "sqlite memory_index row is canonical; "
                            "markdown export at .MEMORY/<path> is "
                            "stale until re-exported."
                        ),
                    },
                )
            except Exception:
                # Audit row is forensic — its failure must not change
                # the canonical write outcome. The next sync_memory_files
                # pass will still detect the disk-vs-sqlite drift via
                # checksum compare.
                pass

        # Phase-8 hardening (2026-05-19): emit memory_route_lag /
        # memory_anchor_lag events when capture succeeded but its
        # discoverability wiring (route upsert / anchor upsert) failed.
        # Canonical memory exists in memory_index; these events record
        # that operators looking up the memory by keyword/anchor may
        # not find it until backfill reconciles.
        route_lag = bool(route_lag_error) or (
            bool(target_rel) and injection_mode != "skip" and route_id_recorded is None
        )
        if route_lag:
            try:
                hub.execution.record_event(
                    project_root,
                    event_kind="memory_route_lag",
                    source_kind="memory_capture",
                    capability_name="memory_capture",
                    action_kind="memory",
                    target_entity=target_rel,
                    status="degraded",
                    payload={
                        "path": target_rel,
                        "kind": kind,
                        "severity": severity,
                        "trigger": trigger,
                        "priority": priority,
                        "keywords": list(normalized_keywords),
                        "error_class": route_lag_error or "no_route_id",
                        "note": (
                            "memory_index row is canonical; route "
                            "registration failed so keyword discovery "
                            "may miss this row until backfill."
                        ),
                    },
                )
            except Exception:
                pass

        # Anchor-lag: a route exists but one or more anchor upserts
        # failed. Each failure is captured with its symbol/file/kind
        # so the dashboard can show which specific anchors lagged.
        # Includes the "no route" case where every anchor implicitly
        # failed because route_id_recorded is None.
        anchor_lag = bool(anchor_lag_errors) or (
            route_id_recorded is None and bool(normalized_anchors)
        )
        if anchor_lag:
            failed = list(anchor_lag_errors)
            if route_id_recorded is None and bool(normalized_anchors) and not failed:
                # No route → all anchors implicitly failed; surface them
                # with a synthetic error so the dashboard sees the gap.
                failed = [
                    {
                        "symbol": str(a.get("symbol") or ""),
                        "file": str(a.get("file") or ""),
                        "kind": str(a.get("kind") or ""),
                        "error_class": "no_route_id",
                    }
                    for a in normalized_anchors
                ]
            try:
                hub.execution.record_event(
                    project_root,
                    event_kind="memory_anchor_lag",
                    source_kind="memory_capture",
                    capability_name="memory_capture",
                    action_kind="memory",
                    target_entity=target_rel,
                    status="degraded",
                    payload={
                        "path": target_rel,
                        "kind": kind,
                        "route_id": route_id_recorded,
                        "failed_anchors": failed,
                        "anchors_recorded": anchors_recorded,
                        "note": (
                            "memory_index row is canonical; one or "
                            "more symbol anchors failed to register "
                            "so symbol-led discovery may miss this row."
                        ),
                    },
                )
            except Exception:
                pass

        # Palace projection (2026-06-11): push the captured row into
        # MemPalace immediately. Pre-fix the ONLY producer was the
        # first-ever bootstrap sync, so projects bootstrapped before the
        # palace wiring (or memories captured after it) left the palace
        # permanently empty — and _load_project_rules serves bootstrap
        # rules FROM palace drawers, degrading closed. Best-effort:
        # canonical sqlite row is already durable; on failure emit a
        # memory_palace_lag event (same pattern as route/anchor lag).
        palace_queued = False
        _palace = getattr(hub, "palace", None)
        # ── #375 Phase 3 (A): stage the body DURABLY first. Pure sqlite
        # (no chroma import on this path — the 2026-06-30 wound stays
        # healed); the background projector below lands it in the palace
        # with a receipted verify-then-retire. A dead/cold chroma leaves
        # the staged row intact and retriable — a memory can never be
        # lost to the palace being down.
        if target_rel:
            try:
                from .memory_body_staging_store import stage_projection

                stage_projection(project_root, path=target_rel)
            except Exception:
                # Staging is belt-and-suspenders over the already-durable
                # canonical row; a ledger fault must not break capture.
                pass
        if _palace is not None and target_rel:
            # ASYNC (2026-06-30): the mempalace ingest cold-loads a ChromaDB
            # sentence-transformer on the first call after each process start,
            # which blew past the old 5s inline timebox — every capture after a
            # reconnect timed out + abandoned a daemon thread. Hand it to the
            # single background worker and return immediately (see
            # _submit_palace_ingest). Canonical sqlite row is already durable, so
            # a queued projection only lags palace search until the worker drains.
            def _do_palace_ingest():
                # Pin the capture's project scope, land the current row with a
                # read-back receipt, then opportunistically drain older staged
                # intents while the palace is known healthy (#763 Phase 3).
                return _project_palace_capture_and_recover(
                    project_root,
                    _palace,
                    target_rel,
                    hub=hub,
                    runtime=runtime,
                )

            palace_queued = _submit_palace_ingest(_do_palace_ingest)
            if not palace_queued:
                try:
                    hub.execution.record_event(
                        project_root,
                        event_kind="memory_palace_lag",
                        source_kind="memory_capture",
                        capability_name="memory_capture",
                        action_kind="memory",
                        target_entity=target_rel,
                        status="degraded",
                        payload={
                            "path": target_rel,
                            "kind": kind,
                            "queued": False,
                            "note": (
                                "memory_index row is canonical; the palace "
                                "ingest queue was saturated so this drawer "
                                "projection was dropped — palace search/"
                                "bootstrap-context may miss this row until the "
                                "next full ingest."
                            ),
                        },
                    )
                except Exception:
                    pass

        # Envelope: ok + export-lag visibility for the next agent turn.
        # markdown_ok defaults True for the happy path so existing
        # callers see no behavior change.
        #
        # Backlog #220 (observability receipt): a capture silently
        # CONSOLIDATES into an existing canonical row (only updated_at
        # moves), so the return now carries a deterministic receipt —
        # target path, canonical content-hash, and the exact bullet's
        # snippet+hash — plus an explicit palace projection status
        # (queued|dropped|skipped; 'projected' is never claimed
        # synchronously because the ingest is async by design).
        _captured_text = str(getattr(result, "content", content) or content)
        import hashlib as _hashlib

        _captured_hash = _hashlib.sha256(_captured_text.encode("utf-8")).hexdigest()
        _palace_health = _palace_queue_health()
        if _palace is None or not target_rel:
            palace_status = "skipped"
        elif palace_queued:
            # LOUD staleness (2026-07-17): if OLDER queued projections have
            # sat past the threshold the drain is stalled — never report a
            # silent 'queued' forever.
            palace_status = "stale" if _palace_health["stale"] else "queued"
        else:
            palace_status = "dropped"
        # #429 slim receipt: ONE palace verdict field (palace_status folds the
        # old palace_ok/palace_timed_out/palace_queued triplicate), verify
        # prose only when the projection is NOT healthy-async, and
        # default-valued fields (consolidated=False, markdown_ok=True, empty
        # export_lag, empire_candidate=False, depth 0) are omitted entirely.
        _consolidated = bool(getattr(result, "consolidated", False))
        out: dict[str, Any] = {
            "ok": True,
            # ── #220 receipt: verify the SPECIFIC capture, deterministically ──
            "target": target_rel,
            "content_hash": getattr(result, "sqlite_checksum", None),
            "captured_snippet": (
                _captured_text[:200] + ("…" if len(_captured_text) > 200 else "")
            ),
            "captured_hash": _captured_hash,
            "route_id": route_id_recorded,
            "scope_proposal": _scope_proposal.scope,
            "scope_confidence": _scope_proposal.confidence,
            "scope_requires_seal": _scope_proposal.requires_seal,
            "scope_signals": list(_scope_proposal.signals),
            # ── #220 palace projection status (async: never 'projected') ──
            "palace_status": palace_status,
        }
        if _consolidated:
            out["consolidated"] = True
        if int(_palace_health["depth"]):
            out["palace_queue_depth"] = int(_palace_health["depth"])
        if palace_status in ("stale", "dropped"):
            out["palace_verify"] = (
                "'stale' means older queued projections have NOT drained past "
                "the staleness threshold — the projection worker is stalled; "
                "'dropped' means the queue was saturated — either way the "
                "canonical sqlite row is still durable; the next full ingest "
                "heals the palace. Confirm via ai_palace_status/palace search."
            )
        if not bool(getattr(result, "markdown_ok", True)):
            out["markdown_ok"] = False
        if export_lag:
            out["export_lag"] = export_lag
        if _empire_candidate:
            out["empire_candidate"] = True
        if palace_status == "stale":
            out["palace_oldest_queued_age_s"] = _palace_health["oldest_age_s"]
        if "last_error" in _palace_health:
            out["palace_last_error"] = _palace_health["last_error"]
        if _consolidated:
            out["consolidated_note"] = (
                f"appended to the EXISTING canonical row '{target_rel}' — no new "
                "row was created (created_at-ordered queries will not show this "
                "add; verify via content_hash/captured_hash or memory_search)."
            )
        if _empire_candidate:
            out["empire_hint"] = (
                "this entry looks EMPIRE-WORTHY (portable rule/invariant/"
                "preference). It stays kingdom-side; the operator can promote "
                f"it to the empire tier. ({_empire_reason})"
            )
        if neighbor_merged_into:
            out["merged_into"] = neighbor_merged_into
            out["fit"] = fit
            out["fit_hint"] = (
                "neighbor verdict (#144): folded into the existing row "
                f"'{neighbor_merged_into}' (fit_score={fit.get('best_score')}) — "
                "no new row was created; both provenances are preserved as "
                "separate bullets in that row. Re-invoke with "
                "skip_fit_check=True if a separate row was genuinely intended."
            )
        elif fit.get("verdict") == "neighbor":
            # Merge floors refused the fold (cross-kind / sovereign target /
            # explicit target_hint) — surface the near matches, as before.
            out["fit"] = fit
            out["fit_hint"] = (
                "near-duplicate memory exists — consider merging into the "
                "closest match instead of accumulating variants."
            )
        if verbose:
            # #429: verbose=True keeps EVERY field of the full receipt —
            # no information loss, just not the default.
            return out
        # #429 lean receipt (default): one-line verdicts only. Everything
        # dropped here is one verbose=True re-call away.
        lean: dict[str, Any] = {
            "ok": True,
            "target": target_rel,
            "content_hash": out.get("content_hash"),
            "palace": palace_status,
            "consolidated": _consolidated,
            "empire_candidate": bool(_empire_candidate),
            "scope_proposal": _scope_proposal.scope,
            "scope_requires_seal": _scope_proposal.requires_seal,
        }
        notes: list[str] = []
        if neighbor_merged_into:
            notes.append(
                f"folded into existing row '{neighbor_merged_into}' (neighbor fit)"
            )
        elif _consolidated:
            notes.append(f"appended to existing canonical row '{target_rel}'")
        if palace_status in ("stale", "dropped"):
            notes.append(
                f"palace projection {palace_status} — canonical sqlite row is "
                "durable; next full ingest heals the palace"
            )
        if export_lag or not bool(getattr(result, "markdown_ok", True)):
            notes.append("markdown export lagged — sqlite row is canonical")
        if fit.get("verdict") == "neighbor" and not neighbor_merged_into:
            notes.append(
                "near-duplicate memory exists (verbose=True for matches)"
            )
        if _empire_candidate:
            notes.append(
                "global-law proposal only — remains project evidence until "
                "sealed via ai_memory mode='promote' content_hash=<content_hash>"
            )
        if notes:
            lean["note"] = "; ".join(notes)
        return lean

    from . import tool_interface as _ti_c20_memory

    _ti_c20_memory.register_impl("memory_promote", memory_promote)
    _ti_c20_memory.register_impl("memory_capture", memory_capture)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Initialize Project",
        },
    )
    @timed_sync
    def project_init(
        project_root: str,
        init_git: bool = True,
        create_remote: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Initialize AIDOCS structure on a new project — creates .MEMORY/, AGENTS.md/CLAUDE.md, and templates.

        Creates the full AIDOCS directory structure directly (no shell scripts).
        Safe to call on already-initialized projects (idempotent).
        Also ensures the project has a .mcp.json with the aidocs MCP server entry for Claude Code.

        Args:
            project_root: Absolute path to the project to initialize. Required
                because project_init runs BEFORE the ``.MEMORY/.aidocs/`` marker
                exists — auto-discovery would either miss the target or
                accidentally re-init AIDOCS itself.
            init_git: If True (default), initialize a git repo if none exists.
            create_remote: If True, create a private GitHub repo using `gh` CLI. Default: False (opt-in).

        """
        # RBAC enforcement (2026-04-21): project.bootstrap at global.
        # Target project has no .MEMORY yet, so the RBAC check runs
        # against the MCP server's own home project (resolve_project_root),
        # where the bootstrapped super_admin carries project.bootstrap.
        _home = resolve_project_root()
        _rbac = hub.require_permission(
            _home,
            "project.bootstrap",
            scope_type="global",
            scope_id=None,
            tool_name="project_init",
            extra_payload={"target_project_root": project_root},
        )
        if not _rbac["ok"]:
            return _rbac
        return runtime.project_init(
            Path(project_root),
            init_git=init_git,
            create_remote=create_remote,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Ensure MCP Config",
        },
    )
    def project_ensure_mcp_config() -> dict[str, Any]:
        """Ensure the target project has a .mcp.json with the aidocs MCP server entry for Claude Code.

        Idempotent — safe to call repeatedly. Creates or updates .mcp.json as needed.
        Preserves any existing non-aidocs MCP server entries.
        """
        return runtime.ensure_claude_mcp_config(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Check Project",
        },
    )
    def project_check() -> dict[str, Any]:
        """Run strict session-era structural check on a project."""
        return hub.updater.run_check(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Check Project (Legacy)",
        },
    )
    def project_check_legacy() -> dict[str, Any]:
        """Run legacy-compatible structural check on a project."""
        return hub.updater.run_check_legacy(resolve_project_root())

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Fix Project",
        },
    )
    def project_fix() -> dict[str, Any]:
        """Run safe deterministic structural fixes on a project."""
        return hub.updater.run_fix(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Inspect Legacy",
        },
    )
    def project_inspect_legacy() -> dict[str, Any]:
        """Inspect whether legacy runtime files/folders are still present."""
        return hub.updater.inspect_legacy_runtime(resolve_project_root())

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Sync Project Indexes",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @timed_sync
    def project_sync_indexes(
        include_tests: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Refresh all derived indexes for a project in one call."""
        root = resolve_project_root()
        capability_count = hub.capabilities.sync_capabilities(root, registered_tools())
        workflow_sync = hub.workflow.compile_project_rules(root)
        procedure_count = hub.procedures.sync_procedures(root, hub.workflow.read_compiled(root))
        link_count = hub.procedure_links.sync_links(
            root,
            all_procedures(root),
            all_capabilities(root),
        )
        code_processed = hub.code.sync_code_files(root, include_tests=include_tests)
        code_status = hub.code.code_status(root)
        return {
            "memory": hub.index.sync_all(root),
            "capabilities": {"capability_definitions": capability_count},
            "code_manifest": {
                "processed_code_files": code_processed,
                "code_files": code_status.get("code_files"),
                "parsed_code_files": code_status.get("parsed_code_files"),
            },
            "schema": hub.schema.sync_schema(root),
            "workflow": workflow_sync,
            "procedures": {"procedure_definitions": procedure_count},
            "procedure_capability_links": {"links": link_count},
            "execution": hub.execution.execution_status(root),
            "execution_pruning": hub.execution.prune_old_events(root),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Status",
        },
        meta={"anthropic/searchHint": True},
    )
    def project_status() -> dict[str, Any]:
        """Return a consolidated status view for memory, code, and schema indexes."""
        root = resolve_project_root()
        return {
            "origins": runtime.project_origins(root),
            "repo_summary": runtime.repo_summary(root),
            "memory": hub.index.status(root),
            "capabilities": hub.capabilities.capability_status(root),
            "code": hub.code.code_status(root),
            "schema": hub.schema.schema_status(root),
            "workflow": hub.workflow.status(root),
            "procedures": hub.procedures.procedure_status(root),
            "procedure_capability_links": hub.procedure_links.link_status(root),
            "execution": hub.execution.execution_status(root),
            "legacy": hub.updater.inspect_legacy_runtime(root),
        }

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Project Origins",
        },
    )
    def project_origins_get() -> dict[str, Any]:
        """Return git remote/origin context, including private/public split hints."""
        root = resolve_project_root()
        return runtime.project_origins(root)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptors",
        },
    )
    def index_language_descriptors_get() -> dict[str, Any]:
        """Return the active built-in + project-local language descriptor registry summary."""
        return descriptor_registry_summary(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Validate Language Descriptors",
        },
    )
    def index_language_descriptors_validate() -> dict[str, Any]:
        """Validate built-in and project-local TOML language descriptors."""
        return validate_language_descriptors(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptor Semantics",
        },
    )
    def index_language_descriptor_semantics_get() -> dict[str, Any]:
        """Return the available built-in descriptor semantic families/tags."""
        return descriptor_semantics_summary()

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Language Descriptor Match",
        },
    )
    def index_language_descriptor_match_get(relative_path: str) -> dict[str, Any]:
        """Show which descriptor would classify a given project-relative path."""
        return descriptor_match_summary(resolve_project_root(), relative_path)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Capability Index Status",
        },
    )
    def capability_index_status() -> dict[str, Any]:
        """Return current MCP capability index status for a project."""
        return hub.capabilities.capability_status(resolve_project_root())

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Get Capability Definitions",
        },
        meta={"anthropic/searchHint": True},
    )
    def capability_definitions_get(query: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Return indexed MCP capability definitions, optionally filtered by query."""
        root = resolve_project_root()
        result = hub.capabilities.find_capabilities(root, query=query, limit=limit)
        return runtime.build_artifact_backed_result(
            root,
            inline_summary=f"Found {len(result)} capability definition(s).",
            payload=result,
            artifact_name="capability-definitions",
            structured_summary={
                "count": len(result),
                "query": query,
                "limit": limit,
            },
        )
