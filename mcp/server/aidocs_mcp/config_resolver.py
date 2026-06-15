"""Canonical 5-layer config resolver — factory > global > project > session.

Phase 3 of the settings-rationalization arc.

Core responsibilities:

1. Resolve the effective value for a `setting_path` by deep-merging the
   four operator-facing layers (factory at the floor, session at the top).
2. Track per-leaf origin so audit_drift / dashboard rendering can label
   "this came from factory" vs "you customized this at project".
3. Be the single read path that all higher-level config consumers
   (`config.get_setting`, `ConfigStore.get_effective`,
   `runtime.effective_config`, `audit_drift`) eventually delegate to.

Out of scope: the lane layer. Lane settings live in
`session_query_gate_store` (gate-state, not config_settings) and are
applied as a separate runtime overlay AFTER `resolve()` returns.

Design rules (king's decrees):

- Lists REPLACE per leaf within the cascade. Project may LOOSEN.
- Dicts deep-merge.
- Factory layer comes from `_DEFAULT_CONFIG` (for nested namespaces like
  `bash` that are tree-shaped) PLUS `SETTINGS_CATALOG[k].default` for
  leaf-shaped catalog entries that aren't in `_DEFAULT_CONFIG`.
- Origin tracking is per-leaf. For dict-typed values the origin map
  keys are dotted-paths (e.g. `bash.allow.python`).
- `_REMOVED_SETTINGS` keys raise `RemovedSettingError` on read.

This module is read-only. Writes still go through `ConfigStore.set`.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_store import (
    _REMOVED_SETTINGS,
    RemovedSettingError,
    _global_db_path,
    _project_db_path,
)

_ROWS_UNSET = object()

# ── Request-scoped layer-rows cache (UPS sqlite-open seal, 2026-06-02) ──────
# A single logical hook event resolves the SAME config keys hundreds of times
# (the code-freshness walk calls get_setting per directory). Each standalone
# get_setting opens a fresh sqlite connection (config_resolver._db_layer). When
# a request scope is active, get_setting auto-borrows the SAME caller-owned
# rows_cache that effective_config already uses — reading each (db, scope,
# scope_key) layer ONCE and filtering in memory thereafter. This is
# verdict-IDENTICAL (same rows, same merge predicate as the per-key query;
# proven in _db_layer's cached path) and request-SCOPED (the contextvar holds
# only for one event, so cross-process truth is preserved — every fresh hook
# process and every long-lived-server request reads the DB fresh on entry).
# Explicit invalidation: any config write (ConfigStore.set / reload_config_caches)
# clears the active scope so a read-after-write in the same event re-reads.
_REQUEST_ROWS: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "aidocs_config_request_rows",
    default=None,
)


@contextlib.contextmanager
def request_config_scope():
    """Activate a request-scoped config layer-rows cache for the duration of one
    logical hook event. Re-entrant-safe: a nested scope reuses the outer dict."""
    if _REQUEST_ROWS.get() is not None:
        yield  # already inside a scope; reuse it (no nested teardown)
        return
    token = _REQUEST_ROWS.set({})
    try:
        yield
    finally:
        try:
            _REQUEST_ROWS.reset(token)
        except Exception:
            pass


def invalidate_request_config_scope() -> None:
    """Explicit revision invalidation: drop the active scope's cached layer rows
    so a read after a config write in the same event observes the new value."""
    d = _REQUEST_ROWS.get()
    if d is not None:
        d.clear()


def _read_layer_rows(
    db_path: Path,
    scope_filter: str,
    scope_key: str,
) -> list[tuple]:
    """Read ALL (setting_path, value) rows for one scope from a layer DB in a
    single query. Returns [] when the DB/table is absent — the same contribution
    as 'no rows'. Used to batch effective_config's per-key reads into one read
    per layer (verdict-identical: every key sees the same rows it would have
    queried individually).
    """
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute(
                "SELECT setting_path, value FROM config_settings WHERE scope = ? AND scope_key = ?",
                (scope_filter, scope_key),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


# Layer names — single source of truth for origin labelling.
LAYER_FACTORY = "factory"
LAYER_GLOBAL = "global"
LAYER_PROJECT = "project"
LAYER_SESSION = "session"

_LAYER_ORDER = (LAYER_FACTORY, LAYER_GLOBAL, LAYER_PROJECT, LAYER_SESSION)


@dataclass(slots=True)
class ResolvedValue:
    """The merged value for a setting plus per-leaf origin metadata."""

    value: Any
    origin: dict[str, str] = field(default_factory=dict)
    """Map of dotted-leaf-path -> layer name. For a scalar at
    `bash.default`, origin is `{"bash.default": "global"}`. For a dict
    at `bash`, origin carries one entry per descendant leaf:
    `{"bash.default": "factory", "bash.allow.python": "factory",
    "bash.allow.opencode": "project", ...}`. Empty when the setting
    has no value at any layer."""


_RESOLVER_LOCK = threading.Lock()


class LayeredConfigResolver:
    """5-layer resolver. One method, one return type, no surprises.

    Distinct from the legacy ``config.ConfigResolver`` (TOML-path
    facade); this is the canonical per-key cascade introduced in
    Phase 3 of settings-rationalization.
    """

    def resolve(
        self,
        setting_path: str,
        project_root: Path | None = None,
        *,
        session_id: str | None = None,
        rows_cache: dict | None = None,
    ) -> ResolvedValue:
        """Return the effective value + origin metadata for a setting.

        `project_root=None` resolves only factory + global (no project
        DB context). `session_id=None` skips the session layer.

        `rows_cache`: an optional caller-owned dict that batches per-layer DB
        reads across many resolve() calls (effective_config). Each (db, scope,
        scope_key) is read ONCE into the cache and filtered in memory thereafter
        — verdict-identical to per-key queries (same rows). None ⇒ each layer is
        read fresh per call (the standalone get_setting path, unchanged).

        Raises `RemovedSettingError` when the setting is hard-removed.
        """
        if setting_path in _REMOVED_SETTINGS:
            raise RemovedSettingError(
                f"Setting `{setting_path}` was removed. {_REMOVED_SETTINGS[setting_path]}",
            )

        # Auto-borrow the request-scoped layer-rows cache when the caller did not
        # pass an explicit one. Identical batching to effective_config; only
        # active inside request_config_scope() so standalone callers and
        # cross-process reads are unchanged.
        if rows_cache is None:
            rows_cache = _REQUEST_ROWS.get()

        def _cached(db_path: Path, scope_filter: str, scope_key: str) -> Any:
            if rows_cache is None:
                return _ROWS_UNSET
            ck = (str(db_path), scope_filter, scope_key)
            if ck not in rows_cache:
                rows_cache[ck] = _read_layer_rows(db_path, scope_filter, scope_key)
            return rows_cache[ck]

        # Factory layer — code-defined defaults.
        value, origin = self._factory_layer(setting_path)

        # Global layer — install-wide DB.
        global_value, global_origin, global_replace = self._db_layer(
            _global_db_path(),
            setting_path,
            LAYER_GLOBAL,
            scope_filter="global",
            scope_key="",
            cached_rows=_cached(_global_db_path(), "global", ""),
        )
        value, origin = self._merge_layer(
            value,
            origin,
            global_value,
            global_origin,
            layer_replace=global_replace,
        )

        if project_root is not None:
            project_db = _project_db_path(project_root)

            # Project layer.
            project_value, project_origin, project_replace = self._db_layer(
                project_db,
                setting_path,
                LAYER_PROJECT,
                scope_filter="project",
                scope_key="",
                cached_rows=_cached(project_db, "project", ""),
            )
            value, origin = self._merge_layer(
                value,
                origin,
                project_value,
                project_origin,
                layer_replace=project_replace,
            )

            # Session layer (only when the caller passes session_id).
            if session_id:
                session_value, session_origin, session_replace = self._db_layer(
                    project_db,
                    setting_path,
                    LAYER_SESSION,
                    scope_filter="session",
                    scope_key=session_id,
                    cached_rows=_cached(project_db, "session", session_id),
                )
                value, origin = self._merge_layer(
                    value,
                    origin,
                    session_value,
                    session_origin,
                    layer_replace=session_replace,
                )

        return ResolvedValue(value=value, origin=origin)

    # ── Layer readers ────────────────────────────────────────────────

    def _factory_layer(
        self,
        setting_path: str,
    ) -> tuple[Any, dict[str, str]]:
        """Build the factory tree for `setting_path`.

        Sources, in priority order:
        1. `_DEFAULT_CONFIG` walked via dotted path. Captures nested
           namespaces (e.g. `bash`, `raw_bash`).
        2. `SETTINGS_CATALOG[setting_path].default` — leaf catalog
           entries not present in `_DEFAULT_CONFIG`. Phase 1 audit
           found ~35 such entries.

        Catalog children are also folded in for namespace queries:
        `resolve("bash")` includes any `SETTINGS_CATALOG["bash.X"]`
        entries the catalog declares.
        """
        from .config import _DEFAULT_CONFIG, _get_dotted
        from .config_schema import SETTINGS_CATALOG

        value: Any = None

        # 1. _DEFAULT_CONFIG dotted lookup.
        defaults_value = _get_dotted(_DEFAULT_CONFIG, setting_path)
        if defaults_value is not None:
            value = self._deepcopy(defaults_value)

        # 2a. Catalog exact-match override at the leaf level.
        if setting_path in SETTINGS_CATALOG:
            catalog_default = SETTINGS_CATALOG[setting_path]["default"]
            if value is None:
                value = self._deepcopy(catalog_default)
            elif isinstance(value, dict) and isinstance(catalog_default, dict):
                value = self._deep_merge(value, catalog_default)

        # 2b. Catalog children (e.g. resolve("bash") picks up
        # catalog "bash.X" entries).
        prefix = setting_path + "."
        for catalog_key, meta in SETTINGS_CATALOG.items():
            if not catalog_key.startswith(prefix):
                continue
            relative = catalog_key[len(prefix) :]
            child_default = self._deepcopy(meta["default"])
            child_tree = self._inflate(relative, child_default)
            if value is None:
                value = child_tree
            elif isinstance(value, dict):
                value = self._deep_merge(value, child_tree)
            # if value is a non-dict scalar, catalog children are
            # ignored — type drift, surfaced by validate_setting_value.

        if value is None:
            return None, {}

        origin = self._stamp_origin(value, setting_path, LAYER_FACTORY)
        return value, origin

    def _db_layer(
        self,
        db_path: Path,
        setting_path: str,
        layer_name: str,
        *,
        scope_filter: str,
        scope_key: str,
        cached_rows: Any = _ROWS_UNSET,
    ) -> tuple[Any, dict[str, str], bool]:
        """Read all rows for setting_path or its descendants from a DB.

        Returns (value, origin, has_direct_row). has_direct_row is
        True when at least one row exists at the EXACT setting_path
        (operator intent: REPLACE this namespace at this layer).
        Otherwise the layer's contribution is sub-key-only and will
        deep-merge with lower layers in `_merge_layer`.

        Returns (None, {}, False) when the DB has no contribution.

        ``cached_rows``: when provided (effective_config batch path), the FULL
        row list for this (scope, scope_key) has already been read once; we
        filter it in memory by exactly the SQL predicate (path == setting_path
        OR path LIKE 'setting_path.%'). This is verdict-identical to the per-key
        query — same rows, same merge — but reads each layer DB once per
        effective_config instead of once per catalog key.
        """
        prefix = setting_path + "."
        if cached_rows is not _ROWS_UNSET:
            rows = [r for r in cached_rows if r[0] == setting_path or r[0].startswith(prefix)]
        else:
            if not db_path.is_file():
                return None, {}, False
            try:
                # READ path: do NOT create the table or set WAL here — those are
                # write-time concerns owned by config_store (every write CREATEs
                # the table + sets WAL). If the table is absent (never written),
                # the SELECT raises and the except below returns None — identical
                # to CREATE→empty→None, so the resolved value is verdict-identical.
                conn = sqlite3.connect(str(db_path))
                try:
                    rows = conn.execute(
                        "SELECT setting_path, value FROM config_settings "
                        "WHERE scope = ? AND scope_key = ? "
                        "AND (setting_path = ? OR setting_path LIKE ?)",
                        (scope_filter, scope_key, setting_path, prefix + "%"),
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                return None, {}, False

        if not rows:
            return None, {}, False

        # Sort by path-depth ascending so shallower writes set the
        # base, deeper writes overlay. Prevents a deeper write from
        # being clobbered by a shallower write applied later.
        sorted_rows = sorted(rows, key=lambda r: r[0].count("."))

        value: Any = None
        has_direct_row = False
        for row_path, raw_value in sorted_rows:
            try:
                row_value = json.loads(raw_value)
            except (TypeError, ValueError):
                continue
            if row_path == setting_path:
                has_direct_row = True
                # Direct write at the requested path.
                if value is None:
                    value = row_value
                elif isinstance(value, dict) and isinstance(row_value, dict):
                    value = self._deep_merge(value, row_value)
                else:
                    value = row_value
            else:
                # Sub-key write (row_path = setting_path.X.Y...).
                relative = row_path[len(prefix) :]
                inflated = self._inflate(relative, row_value)
                if value is None:
                    value = inflated
                elif isinstance(value, dict):
                    # Strict within-layer merge: when an existing leaf
                    # collides with a deeper overlay (overlay would
                    # descend through a non-dict leaf), the leaf wins.
                    # Pinned by test_only_immediate_children_merge —
                    # contradictory shapes (`bash.allow.opencode` as
                    # list AND `bash.allow.opencode.subflag` as a sub-
                    # key) are schema-invalid; the leaf row is the
                    # authoritative value.
                    value = self._deep_merge_strict(value, inflated)
                else:
                    # Type drift — sub-key under a non-dict at the
                    # very root. Drop the sub-key.
                    continue

        if value is None:
            return None, {}, False

        origin = self._stamp_origin(value, setting_path, layer_name)
        return value, origin, has_direct_row

    # ── Merge primitives ─────────────────────────────────────────────

    def _merge_layer(
        self,
        base_value: Any,
        base_origin: dict[str, str],
        layer_value: Any,
        layer_origin: dict[str, str],
        layer_replace: bool = False,
    ) -> tuple[Any, dict[str, str]]:
        """Apply one upper layer onto the running merged tree.

        Per king's decree: dicts deep-merge, lists REPLACE per leaf.
        """
        if layer_value is None:
            return base_value, base_origin
        if base_value is None or layer_replace:
            # Direct-row write at setting_path: layer fully shadows
            # any lower-layer value here. Operator intent is "replace
            # this namespace", not "extend it". Sub-key-only writes
            # from a layer don't trigger this and deep-merge instead.
            return layer_value, dict(layer_origin)
        if isinstance(base_value, dict) and isinstance(layer_value, dict):
            merged_value = self._deep_merge(base_value, layer_value)
            merged_origin = dict(base_origin)
            merged_origin.update(layer_origin)
            return merged_value, merged_origin
        # Scalar / list / type-mismatch: layer replaces base.
        return layer_value, dict(layer_origin)

    @staticmethod
    def _deep_merge(base: dict, overlay: dict) -> dict:
        """Recursive dict merge. Lists at leaves REPLACE per king's decree.

        Used for cross-layer merges where the operator deliberately
        overrides — overlay wins on shape conflicts (dict-vs-leaf).
        """
        out = dict(base)
        for k, v in overlay.items():
            if k in out and isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = LayeredConfigResolver._deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    @staticmethod
    def _deep_merge_strict(base: dict, overlay: dict) -> dict:
        """Strict variant: leaf wins on shape conflict.

        Used for within-layer tree-building where contradictory rows
        (a leaf at `X` AND a sub-key at `X.Y`) are schema-invalid.
        The shallower leaf laid down first is the authoritative value;
        the deeper overlay is silently dropped.
        """
        out = dict(base)
        for k, v in overlay.items():
            if k in out:
                if isinstance(out[k], dict) and isinstance(v, dict):
                    out[k] = LayeredConfigResolver._deep_merge_strict(out[k], v)
                elif not isinstance(out[k], dict) and isinstance(v, dict):
                    # Leaf already laid down at this key; the deeper
                    # overlay would descend through it. Drop overlay.
                    continue
                else:
                    out[k] = v
            else:
                out[k] = v
        return out

    @staticmethod
    def _deepcopy(value: Any) -> Any:
        """Shallow-immutable + dict/list deep-copy via json round-trip
        when the value is JSON-friendly; falls back to identity for
        types that aren't (non-issue for our schema).
        """
        if isinstance(value, (dict, list)):
            try:
                return json.loads(json.dumps(value))
            except (TypeError, ValueError):
                pass
        return value

    @staticmethod
    def _inflate(relative: str, value: Any) -> dict:
        """Build a nested dict from a dotted relative key.

        `_inflate("a.b.c", 1)` -> `{"a": {"b": {"c": 1}}}`.
        """
        parts = relative.split(".")
        out: Any = value
        for part in reversed(parts):
            out = {part: out}
        return out

    @staticmethod
    def _stamp_origin(
        value: Any,
        setting_path: str,
        layer_name: str,
    ) -> dict[str, str]:
        """Walk a value and return `{leaf_path: layer_name}` for every
        leaf descendant. For a scalar, returns one entry. For a dict,
        recurses to include every nested leaf.
        """
        origin: dict[str, str] = {}

        def _walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                if not node:
                    origin[path] = layer_name
                    return
                for k, v in node.items():
                    child_path = f"{path}.{k}" if path else k
                    _walk(v, child_path)
            else:
                origin[path] = layer_name

        _walk(value, setting_path)
        return origin


# Module-level convenience instance — most callers just want the
# default behaviour and don't care about isolation.
_DEFAULT_RESOLVER = LayeredConfigResolver()


def resolve(
    setting_path: str,
    project_root: Path | None = None,
    *,
    session_id: str | None = None,
) -> ResolvedValue:
    """Convenience wrapper around `LayeredConfigResolver().resolve(...)`."""
    return _DEFAULT_RESOLVER.resolve(
        setting_path,
        project_root,
        session_id=session_id,
    )
