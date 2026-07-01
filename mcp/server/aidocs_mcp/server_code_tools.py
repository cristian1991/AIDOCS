from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import mcp_server_runtime_helpers as _rh
from .language_descriptors import (
    descriptor_match_summary,
    descriptor_registry_summary,
    descriptor_semantics_summary,
    validate_language_descriptors,
)
from .config import resolve_include_tests
from .mcp_server_runtime_helpers import _env_project_root, resolve_project_root
from .mode_schema import modes
from .tool_display import renders_as

# Max wall-clock the best-effort MemPalace ingest may take inside
# memory_capture before it is abandoned. The canonical sqlite memory row is
# already durable at that point; the palace projection is a discoverability
# nicety. mempalace is a 3rd-party engine that can block (embedding / KG IO)
# with no internal timeout — an un-timeboxed synchronous call here wedges the
# whole memory_capture tool (and, downstream, the MCP connection). 5s is far
# beyond a healthy single-entry ingest while still bounding a hang.
PALACE_INGEST_TIMEOUT_S = 5.0


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
import queue as _queue_mod  # noqa: E402
import threading as _threading_mod  # noqa: E402

_PALACE_INGEST_QUEUE: "_queue_mod.Queue" = _queue_mod.Queue(maxsize=2048)
_PALACE_WORKER_LOCK = _threading_mod.Lock()
_PALACE_WORKER_STARTED = False


def _palace_worker_loop() -> None:
    while True:
        fn = _PALACE_INGEST_QUEUE.get()
        try:
            fn()
        except Exception:  # noqa: BLE001 — best-effort projection
            pass
        finally:
            _PALACE_INGEST_QUEUE.task_done()


def _submit_palace_ingest(fn) -> bool:
    """Hand a zero-arg palace ingest to the single background worker. Returns
    True if queued, False if the queue is saturated. Never blocks the caller —
    the canonical sqlite row is already durable, so a dropped enqueue only lags
    palace search (a bootstrap heal re-ingests)."""
    global _PALACE_WORKER_STARTED
    if not _PALACE_WORKER_STARTED:
        with _PALACE_WORKER_LOCK:
            if not _PALACE_WORKER_STARTED:
                _threading_mod.Thread(
                    target=_palace_worker_loop,
                    name="aidocs-palace-ingest",
                    daemon=True,
                ).start()
                _PALACE_WORKER_STARTED = True
    try:
        _PALACE_INGEST_QUEUE.put_nowait(fn)
        return True
    except _queue_mod.Full:
        return False


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
                "restore": f"aidocs ai-restore restore --checkpoint {cp.checkpoint_id}",
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
    for r in rows:
        rel = str(r["path"])
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
    require_indexed_read_gate: Any,
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
            return {
                "total_matches": 0,
                "results": [],
                "empty_reason": "no_text_match",
                "hint": "Try ai_find for symbol search.",
            }
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
        rng = hub.code.find_symbol_range(resolve_project_root(), source_path, symbol, kind=kind)
        if "error" in rng:
            return _stamp_mutation({"success": False, "error": rng["error"]}, applied=False)
        result = file_extract_block(
            resolve_project_root(),
            source_path,
            int(rng["start"]),
            int(rng["end"]),
            target_path,
            target_position=target_position,
            remove_from_source=remove_from_source,
        )
        if result.get("success"):
            root = resolve_project_root()
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
        """
        m = mode.strip().lower()
        root = resolve_project_root()

        def _slop_spill(canon, payload):
            """Large results spill to .aidocs/runtime/slop-<mode>.json and the
            tool returns a compact summary (counts + small sample + file path)
            instead of flooding the caller. Small results pass through."""
            import json as _json

            try:
                full = _json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                return payload
            if len(full) <= 12000:
                return payload
            try:
                out_dir = root / ".aidocs" / "runtime"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"slop-{canon}.json"
                out_path.write_text(full, encoding="utf-8")
                rel = out_path.relative_to(root).as_posix()
            except Exception:
                return payload
            summary = {
                "mode": canon,
                "truncated": True,
                "full_output_file": rel,
                "full_output_bytes": len(full),
            }
            if isinstance(payload, dict):
                sample_key = None
                for k, v in payload.items():
                    if v is None or isinstance(v, (int, float, str, bool)):
                        summary[k] = v
                    elif isinstance(v, list) and sample_key is None:
                        sample_key = k
                if sample_key is not None:
                    summary["sample_of"] = sample_key
                    summary["sample_count"] = len(payload[sample_key])
                    summary["sample"] = payload[sample_key][:5]
            return summary
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
            return _slop_spill(_canon, _attach_slop_evidence(_canon, _result))
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
        return hub.code.get_dependencies(resolve_project_root(), path=path)

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
            return {"dnt_banner": _banner, "outline": result}
        return result

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
    ) -> Any:
        """Return an exact code snippet for an indexed outline symbol."""
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
        return result

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
        result = hub.code.investigate(root, concept=concept, limit=limit, depth=depth, focus=focus)
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

    @modes(
        symbols={
            "required": ["query"],
            "optional": ["kind", "role", "modified_since", "include_tests", "limit"],
            "desc": _FIND_MODE_DESCS["symbols"],
        },
        dependencies={
            "required": ["query"],
            "optional": ["limit"],
            "desc": _FIND_MODE_DESCS["dependencies"],
        },
        **{
            m: {
                "required": ["query"],
                "optional": ["limit", "include_tests"],
                "desc": _FIND_MODE_DESCS[m],
            }
            for m in _FIND_GENERIC
        },
    )
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Code Find",
        },
        meta={"anthropic/alwaysLoad": True},
    )
    @renders_as("find")
    @timed_discovery
    def ai_find(
        query: str,
        mode: str = "symbols",
        kind: str | None = None,
        role: str | None = None,
        modified_since: str | None = None,
        include_tests: bool | None = None,
        limit: int = 50,
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
            include_tests: Include test/fixture files in search. Default follows the project's index.include_tests setting; pass True/False to override. Auto-enabled for mode=factories.

        """
        from .code_index_store import parse_modified_since

        root = resolve_project_root()
        # #12 (king 2026-06-20): a discovery tool keeps the sitter alive so the index
        # stays warm — this is the guarantee that makes removing ai_find's inline resync
        # (#74) safe. Idempotent + fast-path (a dict lookup once running); best-effort,
        # never block or fail a read on sitter bookkeeping.
        try:
            from .project_index_sitter import ensure_index_sitter

            ensure_index_sitter(root, hub)
        except Exception:
            pass
        include_tests = resolve_include_tests(include_tests, project_root=root)
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
            matches = hub.code.search_text(
                root,
                query,
                regex=use_regex,
                limit=limit,
                include_tests=include_tests,
            )
            for match in matches:
                grant_known_exact_path_read(hub, root, "ai_find", str(match.get("path", "")))
            if not matches:
                return {
                    "total_matches": 0,
                    "results": [],
                    "empty_reason": "no_text_match",
                    "hint": "Try ai_find for symbol search.",
                }
            return {"total_matches": len(matches), "results": matches}

        if m in ("factories",) and not include_tests:
            include_tests = True

        # NO inline sync here (#74, king 2026-06-20). ai_find is a READ-path discovery
        # tool; the index is kept current by edit-time auto-reindex + the
        # ProjectIndexSitter (its poll re-stat catches out-of-host edits and drop-in
        # files). A full sync_code_files(include_tests=True) on every call was a
        # ~2015-file reparse inside the 10s discovery budget — THE cold-index timeout
        # root. If test symbols are wanted, set index.include_tests=true so the sitter
        # indexes them ONCE, not per-find.

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "ai_find", root)
                return result
            if any(result.get(key) for key in ("matches", "cluster", "results", "result")):
                _grant_paths_from_result(result, "ai_find", root)
            # Backlog #13 item 3: inject minimal staleness signal when
            # the index is missing or empty. Full freshness check lives
            # in ai_index_status (expensive); this covers the two
            # zero-ambiguity cases only.
            return _inject_index_staleness(result, root)

        if m == "symbols":
            mtime_ns = parse_modified_since(modified_since)
            return _grant(
                hub.code.decorate_symbol_hits(
                    root,
                    hub.code.search_symbols(
                        root,
                        query=query,
                        kind=kind,
                        role=role,
                        limit=limit,
                        modified_since_ns=mtime_ns,
                    ),
                ),
            )
        if m == "references":
            return _grant(hub.code.find_references(root, symbol=query, limit=limit))
        if m == "dependencies":
            return _grant(hub.code.get_dependencies(root, path=query))
        if m == "routes":
            return _grant(hub.code.find_routes(root, query=query, limit=limit))
        if m == "entrypoints":
            return _grant(hub.code.find_entrypoints(root, concept=query, limit=limit))
        if m == "api_consumers":
            return _grant(hub.code.find_api_consumers(root, endpoint=query, limit=limit))
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
            if file_result:
                for item in file_result:
                    grant_known_exact_path_read(hub, root, "ai_find", str(item.get("path", "")))
                return {
                    "results": file_result,
                    "source": "filename",
                    "layers_checked": layers_checked,
                }
            text_result = hub.code.search_text(
                root,
                query=query,
                limit=limit,
                include_tests=include_tests,
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
            "schema": "For entity/field lookups try `schema_query(query='...', mode='entity'|'field')`.",
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
            return result

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

    @modes(
        **{
            m: {
                "required": ["target"],
                "optional": ["limit"],
                "desc": _BUNDLE_MODE_DESCS[m],
            }
            for m in _BUNDLE_GENERIC
        },
        session={
            "required": ["target", "session_id"],
            "optional": ["limit"],
            "desc": _BUNDLE_MODE_DESCS["session"],
        },
        context={
            "required": ["target", "session_id"],
            "optional": ["limit"],
            "desc": _BUNDLE_MODE_DESCS["context"],
        },
    )
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

        """
        root = resolve_project_root()
        m = mode.strip().lower()

        def _grant(
            result: dict[str, Any] | list[dict[str, Any]],
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if isinstance(result, list):
                if result:
                    _grant_paths_from_result(result, "ai_bundle", root)
                return result
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
            return result

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
            "title": "Schema Query",
        },
        meta={"anthropic/searchHint": True},
    )
    @renders_as("schema")
    @timed_discovery
    def schema_query(
        query: str,
        mode: str = "entities",
        limit: int = 50,
        include_related: bool = False,
        timeout: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Unified schema tool — replaces all schema_find_*, schema_get_*, schema_trace_* tools.

        Modes: entities, entity, field, trace_flow, trace_path.

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
                    _grant_paths_from_result(result, "schema_query", root)
                return result
            if any(result.get(key) for key in ("entities", "fields", "matches", "properties")):
                _grant_paths_from_result(result, "schema_query", root)
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
                "field",
                "trace_flow",
                "trace_path",
            ],
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Legacy Tools (deprecated — use unified tools above)
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Capture Memory",
        },
        meta={"anthropic/alwaysLoad": True},
    )
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
        if has_metadata and target_rel:
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
            has_metadata and target_rel and route_id_recorded is None
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
        palace_ok = False
        palace_timed_out = False
        palace_queued = False
        _palace = getattr(hub, "palace", None)
        if _palace is not None and target_rel:
            # ASYNC (2026-06-30): the mempalace ingest cold-loads a ChromaDB
            # sentence-transformer on the first call after each process start,
            # which blew past the old 5s inline timebox — every capture after a
            # reconnect timed out + abandoned a daemon thread. Hand it to the
            # single background worker and return immediately (see
            # _submit_palace_ingest). Canonical sqlite row is already durable, so
            # a queued projection only lags palace search until the worker drains.
            def _do_palace_ingest():
                from .memory_sqlite_store import palace_ingest_entry
                from .palace_hub_extension import build_palace_context

                _palace_ctx = build_palace_context(
                    hub,
                    runtime,
                    tool_name="memory_capture.palace_ingest",
                )
                return palace_ingest_entry(
                    project_root,
                    _palace,
                    target_rel,
                    hub_ctx=_palace_ctx,
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
        return {
            "ok": True,
            "markdown_ok": bool(getattr(result, "markdown_ok", True)),
            "export_lag": export_lag,
            "palace_ok": palace_ok,
            "palace_timed_out": palace_timed_out,
            "palace_queued": palace_queued,
        }

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
