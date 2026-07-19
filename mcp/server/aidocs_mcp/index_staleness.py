"""Central, cheap index-staleness stamping for read/search tool results.

One place computes the honest freshness signal an agent needs to know whether a
result is against a current index — WITHOUT the expensive SHA freshness walk
(_code_freshness / _memory_freshness). Called centrally from the instrumented
call_tool wrapper for every index-backed read tool (see INDEX_BACKED_TOOLS).

Three independent axes, each cheap (no filesystem walk). All three indexes share
``.MEMORY/.index/aidocs.sqlite3`` (code_files / memory_files / schema_entities):

  * code  (``_index_*``)  — preserves the prior _inject_index_staleness semantics:
      1. sitter is_index_known_stale -> _index_stale (observed-unreconciled)
      2. code_files missing / 0 rows  -> _index_stale (missing/empty)
      3. freshness_window poll_window_risk / unknown -> _index_freshness (soft)
      else: no code keys (present + window ok).
  * memory (``_memory_*``) — ONLY missing / empty / present_unverified, NEVER
    'fresh': the sitter does not watch .MEMORY and there is no memory version gate,
    so content currency cannot be cheaply proven.
  * schema (``_schema_*``) — same honest missing/empty/present_unverified contract
    for the derived schema catalog (schema_entities).

DB-probe failure NEVER silently drops the signal — it emits an explicit
``unknown`` for that axis.

Authoritative & shape-safe: for each applicable axis the stamp FIRST strips any
pre-existing reserved keys for that axis (``_index*`` / ``_memory*`` / ``_schema*``)
from the payload — so a tool cannot spoof or leave a stale signal — THEN writes the
computed keys. A plain dict and a structured MCP envelope (object with a dict
``.structured_content``) are mutated IN PLACE; the result shape never changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# tool_name -> frozenset of axes to stamp ({"code"}, {"memory"}, {"schema"}, or a
# union). Corrected from live tool bodies.
#   - ai_get_module_files: code-index (parse_modified_since over code_files).
#   - ai_recall: "code + palace + KG" — palace/KG are separate stores (NOT the
#     memory_files index), so only the CODE axis is honest here.
#   - ai_schema: its own SchemaIndexStore (schema_entities) -> SCHEMA axis only.
#   - memory_read: REMOVED — it is a gated memory CONTENT read, not the derived
#     memory index.
#   - ai_slop: REMOVED — its staleness is mode-selective (only _SLOP_INDEX_BACKED
#     modes are index-backed); its existing local stamp handles that. Stamping it
#     unconditionally here would mislabel pure-AST modes.
# Excluded entirely (not index-backed / wrong store / exact diagnostics):
#   raw-file readers (ai_get_lines, ai_read_*), memory_read, semantic_search,
#   palace tools, sync tools, ai_index_status / index_status / project_freshness.
INDEX_BACKED_TOOLS: dict[str, frozenset[str]] = {
    # code-index
    "ai_search": frozenset({"code"}),
    "ai_text_search": frozenset({"code"}),
    "ai_find": frozenset({"code"}),
    "ai_find_symbol_range": frozenset({"code"}),
    "ai_get_dependencies": frozenset({"code"}),
    "ai_get_outline": frozenset({"code"}),
    "ai_get_symbol_snippet": frozenset({"code"}),
    "ai_get_symbol_info": frozenset({"code"}),
    "ai_investigate": frozenset({"code"}),
    "ai_trace": frozenset({"code"}),
    "ai_bundle": frozenset({"code"}),
    "ai_get_modules": frozenset({"code"}),
    "ai_get_module_files": frozenset({"code"}),
    "ai_preview_extraction_deps": frozenset({"code"}),  # hub.code.preview_extraction_deps
    "ai_suggest_extractions": frozenset({"code"}),       # hub.code.suggest_extractions
    "ai_recall": frozenset({"code"}),
    # memory-index
    "memory_search": frozenset({"memory"}),
    # schema-index
    "ai_schema": frozenset({"schema"}),
}

# Reserved key prefix per axis. Stamp strips keys starting with the prefix (for the
# applicable axis) before writing authoritative keys.
_AXIS_PREFIX: dict[str, str] = {
    "code": "_index",
    "memory": "_memory",
    "schema": "_schema",
}

# Shared index db (code_files / memory_files / schema_entities live together).
_DB_REL = (".MEMORY", ".index", "aidocs.sqlite3")

# poll_window_risk surfacing floor (operator 2026-07-16): the risk state begins
# the moment the last reconcile ages past the poll window (30s default), which
# nagged "call ai_index_sync" within seconds of normal operation. The sync hint
# only surfaces once the reconcile age reaches this floor; younger risk windows
# stay silent (the sitter's next poll resolves them on its own).
_POLL_RISK_MIN_AGE_SECONDS = 60


def _shared_db(project_root: Path) -> Path:
    return project_root.joinpath(*_DB_REL)


def _probe_count(db_path: Path, table: str) -> int | None:
    """Row count of `table`, 0 if empty, or None when the db file is absent.
    Raises on any probe failure (missing table / corrupt db / IO) so the caller
    can emit an explicit `unknown` rather than silence."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 (fixed table)
        return int(row[0]) if row else 0
    finally:
        conn.close()  # explicit close (sqlite3 `with` commits but does not close)


def code_staleness_keys(project_root: Path) -> dict[str, Any]:
    """Cheap code-index staleness delta (no SHA walk). {} when present + window ok.
    ANY probe failure (sitter known-stale, db, or freshness window) emits an
    explicit '_index_freshness': 'unknown' — never silence."""
    # 1. KNOWN-stale (sitter). Probe failure -> unknown (not silence).
    try:
        from .project_index_sitter import is_index_known_stale

        known_stale = bool(is_index_known_stale(project_root))
    except Exception:
        return {
            "_index_freshness": "unknown",
            "_index_hint": (
                "Code index known-stale probe failed (sitter unavailable). "
                "Freshness could not be determined; call ai_index_status."
            ),
        }
    if known_stale:
        return {
            "_index_stale": True,
            "_index_hint": (
                "Index is known-stale: an external file change was observed but "
                "not yet reconciled. Call ai_index_sync (or wait for the index "
                "sitter) before relying on this."
            ),
        }
    # 2. DB present/empty probe (explicit unknown on failure).
    try:
        total = _probe_count(_shared_db(project_root), "code_files")
    except Exception:
        return {
            "_index_freshness": "unknown",
            "_index_hint": (
                "Code index probe failed (db unreadable or missing table). "
                "Freshness could not be determined; call ai_index_status."
            ),
        }
    if total is None:
        return {
            "_index_stale": True,
            "_index_hint": (
                "No code index yet. Call ai_index_sync to build one before relying "
                "on search results."
            ),
        }
    if total == 0:
        return {
            "_index_stale": True,
            "_index_hint": "Code index is empty. Call ai_index_sync to populate it.",
        }
    # 3. SOFT freshness window (sitter cadence). Probe failure -> unknown.
    try:
        from .project_index_sitter import freshness_window

        fw = freshness_window(project_root)
    except Exception:
        return {
            "_index_freshness": "unknown",
            "_index_hint": (
                "Code index freshness-window probe failed (sitter unavailable). "
                "Freshness could not be determined; call ai_index_status."
            ),
        }
    if fw.get("state") == "poll_window_risk":
        try:
            _age = float(fw.get("age_seconds") or 0.0)
        except (TypeError, ValueError):
            _age = 0.0
        if _age < _POLL_RISK_MIN_AGE_SECONDS:
            # Younger than the surfacing floor — not worth a per-result nag.
            return {}
        return {
            "_index_freshness": "poll_window_risk",
            "_index_freshness_age_seconds": fw.get("age_seconds"),
            "_index_poll_window_seconds": fw.get("window_seconds"),
            "_index_hint": (
                "Index freshness unverified: the last poll reconcile is older "
                "than the configured poll window. Call ai_index_sync to force a "
                "reconcile if currency matters."
            ),
        }
    if fw.get("state") == "unknown":
        # no_reconcile_yet is the benign pre-first-reconcile window, NOT a stale
        # index. Keep the small freshness marker but DROP the per-result "call
        # ai_index_sync" hint — it was an alarm emitted on every result that
        # never indicated real staleness (S-tier scorecard #4).
        return {
            "_index_freshness": "unknown",
            "_index_freshness_reason": fw.get("reason"),
        }
    return {}


def _present_axis_keys(
    project_root: Path, *, table: str, key: str, hint_key: str, label: str, sync_cmd: str,
) -> dict[str, Any]:
    """Shared missing/empty/present_unverified/unknown logic for memory & schema.
    `sync_cmd` is the axis-correct rebuild tool (index_sync / schema_index_sync)."""
    try:
        total = _probe_count(_shared_db(project_root), table)
    except Exception:
        return {
            key: "unknown",
            hint_key: (
                f"{label} index probe failed (db unreadable or missing table). "
                "Freshness could not be determined; call ai_index_status."
            ),
        }
    if total is None:
        return {key: "missing", hint_key: f"No {label} index yet. Call {sync_cmd}."}
    if total == 0:
        return {key: "empty", hint_key: f"{label} index is empty. Call {sync_cmd}."}
    return {
        key: "present_unverified",
        hint_key: (
            f"{label} index present but its content freshness is not cheaply "
            f"verifiable. Call {sync_cmd} or ai_index_status if currency matters."
        ),
    }


def memory_staleness_keys(project_root: Path) -> dict[str, Any]:
    """Memory-index delta against the CANONICAL sqlite memory_index (SQLite-only
    doctrine): missing (no db/table) / empty (no active rows) / ready (active
    rows present) / unknown (probe failure). memory_index IS the source of truth
    that memory reads serve from, so present == ready — there is no derived-cache
    drift to qualify as 'present_unverified'. (MemPalace lag is a separate axis.)"""
    db = _shared_db(project_root)
    if not db.is_file():
        return {"_memory_index": "missing",
                "_memory_index_hint": "No memory index yet. Capture memory or run migrate_markdown_to_sqlite."}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_index "
                "WHERE COALESCE(status,'active')='active' AND COALESCE(superseded_by,'')=''",
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return {"_memory_index": "unknown",
                "_memory_index_hint": "Memory index probe failed; call ai_index_status."}
    active = int(row[0]) if row else 0
    if active == 0:
        return {"_memory_index": "empty",
                "_memory_index_hint": "Memory index has no active rows. Capture memory."}
    return {"_memory_index": "ready"}


def schema_staleness_keys(project_root: Path) -> dict[str, Any]:
    """Schema-index delta: missing/empty/present_unverified/unknown — never 'fresh'.
    Sync via schema_index_sync."""
    return _present_axis_keys(
        project_root, table="schema_entities",
        key="_schema_index", hint_key="_schema_index_hint", label="Schema",
        sync_cmd="schema_index_sync",
    )


_AXIS_FN = {
    "code": code_staleness_keys,
    "memory": memory_staleness_keys,
    "schema": schema_staleness_keys,
}


def apply_axes(payload: dict, project_root: Path, axes: frozenset[str] | set[str]) -> dict:
    """Authoritatively stamp `payload` (mutated in place) for the given axes: for
    each axis, STRIP any pre-existing reserved keys for that axis, then write the
    freshly computed keys. Returns the same dict."""
    for axis in axes:
        prefix = _AXIS_PREFIX[axis]
        for k in [k for k in payload if isinstance(k, str) and k.startswith(prefix)]:
            del payload[k]
        payload.update(_AXIS_FN[axis](project_root))
    return payload


RESERVED_PREFIXES = ("_index", "_memory", "_schema")


def reserved_freshness_keys(payload: Any) -> dict[str, Any]:
    """Extract the reserved freshness keys (_index*/_memory*/_schema*) already
    present on a dict payload — used by the renderer's terse ack so a stamped raw
    payload's freshness survives into the agent-facing ack."""
    if not isinstance(payload, dict):
        return {}
    return {
        k: v for k, v in payload.items()
        if isinstance(k, str) and k.startswith(RESERVED_PREFIXES)
    }


def freshness_keys_for_tool(tool_name: str, project_root: Path) -> dict[str, Any]:
    """Compute the freshness delta for a tool by name (its mapped axes), or {}
    when the tool is not index-backed. For the renderer to stamp a terse ack when
    the raw payload is a list (no dict slot to stamp in place)."""
    axes = INDEX_BACKED_TOOLS.get(tool_name)
    if not axes:
        return {}
    out: dict[str, Any] = {}
    try:
        for axis in axes:
            out.update(_AXIS_FN[axis](project_root))
    except Exception:
        return {}
    return out


def inject_index_staleness(result: Any, project_root: Path) -> Any:
    """Back-compat code-only stamp for ai_find/ai_slop (the latter calls it only for
    its index-backed modes). Authoritative + shape-preserving on a dict result."""
    if not isinstance(result, dict):
        return result
    return apply_axes(result, project_root, frozenset({"code"}))


def _strip_nested_freshness(obj: Any, _top: bool = True) -> None:
    """Remove reserved freshness keys (_index*/_memory*/_schema*) from NESTED
    dicts/lists in place, leaving the TOP level untouched.

    Composite tools (ai_bundle / ai_investigate and friends) embed sub-results
    that were ALREADY freshness-stamped when produced; the central
    stamp_tool_result then stamps the wrapping envelope too, so the annotation
    appears at two nesting depths (cosmetic redundancy, identical values). The
    envelope's stamp is the single authoritative one, so nested copies are
    stripped — freshness renders exactly once. Top level is preserved because
    apply_axes (re)stamps it authoritatively right after this runs."""
    if isinstance(obj, dict):
        if not _top:
            for k in [k for k in obj if isinstance(k, str) and k.startswith(RESERVED_PREFIXES)]:
                del obj[k]
        for v in obj.values():
            _strip_nested_freshness(v, _top=False)
    elif isinstance(obj, list):
        for item in obj:
            _strip_nested_freshness(item, _top=False)


def stamp_tool_result(result: Any, tool_name: str, project_root: Path) -> Any:
    """Central entry: authoritatively stamp honest freshness onto an index-backed
    read tool's result, by tool_name. No-op for tools not in INDEX_BACKED_TOOLS.
    Handles a plain dict and a structured MCP envelope (dict `.structured_content`)
    in place without changing shape. Nested sub-result freshness stamps are
    stripped first so the envelope carries the ONE authoritative stamp (no
    double-stamp at two nesting depths). Best-effort: never raises into the call path."""
    try:
        axes = INDEX_BACKED_TOOLS.get(tool_name)
        if not axes:
            return result
        if isinstance(result, dict):
            _strip_nested_freshness(result)
            apply_axes(result, project_root, axes)
            return result
        sc = getattr(result, "structured_content", None)
        if isinstance(sc, dict):
            _strip_nested_freshness(sc)
            apply_axes(sc, project_root, axes)
        return result
    except Exception:
        return result
