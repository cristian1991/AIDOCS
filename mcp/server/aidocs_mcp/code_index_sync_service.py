from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from stat import S_ISREG
from typing import Any

from .relpath_util import posix_root_prefix, relpath_posix

from .language_descriptors import descriptor_for_language

# Sentinel returned by ``get_setting`` when an exception escapes the
# resolver. In practice this path is rarely reached because
# ``_read_layer_rows`` swallows ``sqlite3.Error`` (including corrupted
# DB) and returns []. Our independent readability check below catches
# that silent-degradation case.
_UNREAD = object()


def _project_config_readable(project_root: Path) -> bool | None:
    """Independently verify the project config DB is readable.

    Returns ``True`` when ``config_settings`` table queries work,
    ``False`` when the DB is corrupted/missing the table (silent-degradation
    that ``_read_layer_rows`` swallows), and ``None`` when the DB file
    does not exist yet (fresh project — not degradation).
    """
    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("SELECT 1 FROM config_settings LIMIT 1")
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()
    except sqlite3.Error:
        return False


# Trusted-lineage of an authoritative live_config_true policy. Once the live
# config decided `include_tests=true` (provenance `live_config_true`), and the
# config layer then went degraded, the resolver writes `degraded_fail_closed` to
# preserve protection. On the NEXT degraded sync, the last-recorded provenance
# is `degraded_fail_closed` — which is itself a faithful descendant of the
# original live_config_true policy (it ONLY exists because we previously
# observed live_config_true), so it must remain sticky. Without this,
# protection silently regresses after the first degraded write.
#
# `caller_true` / `path_shim` are deliberately EXCLUDED: they reflect a
# caller-side override, not a recorded user/admin intent in the config, and so
# must NOT make include_tests sticky across syncs.
_LIVE_CONFIG_TRUE_LINEAGE = frozenset({"live_config_true", "degraded_fail_closed"})


def _resolve_include_tests(
    caller_value: bool,
    project_root: Path,
    store: Any,
    *,
    provenance_hint: str | None = None,
) -> tuple[bool, str]:
    """Central effective-policy resolver for include_tests.

    Returns ``(effective_value, provenance)`` where provenance is the
    authority source that determined the result. The caller (typically
    ``sync_code_files``) passes ``provenance_hint`` when the truthy
    call came from a path-shim or ``INDEX_INCLUDE_TESTS`` module
    constant rather than from an explicit user intent.

    Resolution cascade (evaluated in order; first match wins):
      1. If caller_value is already True (from explicit arg,
         INDEX_INCLUDE_TESTS module constant, or path-shim), return True
         with ``provenance_hint`` (``"caller_true"`` / ``"path_shim"``).
         These are NEVER sticky across syncs.
      2. Read live config via ``get_setting``. If the value is truthy
         (bool True or string ``"true"``/``"1"``/``"yes"``/``"on"``),
         return ``(True, "live_config_true")`` — this is the auto-promotion
         that fixes stale sitters with a frozen False module constant.
      3. If live config read failed entirely (exception — sentinel
         returned), check the last recorded index policy. If it says
         ``include_tests=true`` **and** the last provenance is in the
         live_config_true LINEAGE (``"live_config_true"`` itself, or
         ``"degraded_fail_closed"`` — a faithful descendant), return
         ``(True, "degraded_fail_closed")`` to preserve the last trustworthy
         true policy ACROSS MULTIPLE consecutive degraded syncs. Otherwise
         return ``(False, "factory_false")``.
      4. Live config resolved to false (bool False or falsy string).
         Independently verify the project config DB is readable
         (``_project_config_readable``). If the DB is degraded and the
         last recorded policy is in the live_config_true LINEAGE (same set
         as above), return ``(True, "degraded_fail_closed")``. Otherwise
         return ``(False, "factory_false")`` (legitimate user disablement,
         fresh DB, or degraded with no prior live_config_true lineage).
    """
    if caller_value:
        provenance = provenance_hint or "caller_true"
        return True, provenance

    from .config import get_setting

    live = get_setting("index.include_tests", project_root=project_root, default=_UNREAD)

    def _has_live_lineage(last: dict) -> bool:
        # Sticky preservation requires (a) a recorded true value AND (b) a
        # provenance descended from live_config_true. caller_true / path_shim
        # are explicitly NOT in the lineage.
        return (
            last.get("index_policy_include_tests") == "true"
            and last.get("index_policy_provenance") in _LIVE_CONFIG_TRUE_LINEAGE
        )

    if live is _UNREAD:
        last = store._read_index_policy(project_root)
        if _has_live_lineage(last):
            return True, "degraded_fail_closed"
        return False, "factory_false"

    if isinstance(live, bool):
        effective = live
    elif isinstance(live, str):
        effective = live.strip().lower() in {"true", "1", "yes", "on"}
    else:
        effective = bool(live)

    if effective:
        return True, "live_config_true"

    readable = _project_config_readable(project_root)
    if readable is False:
        last = {}
        try:
            last = store._read_index_policy(project_root)
        except sqlite3.Error:
            pass
        if _has_live_lineage(last):
            return True, "degraded_fail_closed"
        return False, "factory_false"

    return False, "factory_false"


_FRESHNESS_PATH_KEYS = ("drifted_paths", "missing_paths", "extra_paths", "unparsed_paths")


def bound_freshness_paths(freshness: dict, cap: int = 25) -> dict:
    """Cap each per-path list in a freshness dict so a status report can never balloon
    (a fully-unparsed 2015-file index once emitted a 101k blob — bug #76). A list <=
    cap passes through UNCHANGED (preserves the exact-list contracts in
    test_index_freshness); a longer list is capped to the first `cap` entries and gains
    an explicit `<key>_total` count so the truth — how many — survives the truncation.
    Returns a shallow copy; never mutates the input.
    """
    out = dict(freshness)
    for key in _FRESHNESS_PATH_KEYS:
        lst = out.get(key)
        if isinstance(lst, list) and len(lst) > cap:
            out[f"{key}_total"] = len(lst)
            out[key] = lst[:cap]
    return out


class CodeIndexSyncService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _collect_source_inventory(
        self,
        project_root: Path,
        include_tests: bool = False,
    ) -> list[dict[str, Any]]:
        """ONE source-tree walk shared by sync_code_manifest + sync_code_files.

        Exactly one ``_walk_source_files`` + ``stat()`` + ``_language_for`` +
        ``descriptor_for_language`` per file (these were performed TWICE — once
        per method — when sync_code_files called sync_code_manifest then walked
        again). Returns only language!=None files: the exact set both passes
        actually write to the index (language=None files were skipped by both
        before any DB write, and only ever entered the old files-walk
        ``seen_paths`` to delete outlines/edges they never had — a no-op).
        """
        inventory: list[dict[str, Any]] = []
        for path in self.store._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            code_language = self.store._language_for(path, project_root=project_root)
            if code_language is None:
                continue
            descriptor = descriptor_for_language(project_root, rel, path.suffix.lower())
            stat = path.stat()
            inventory.append(
                {
                    "rel": rel,
                    "path": path,
                    "language": code_language,
                    "language_tier": descriptor.tier if descriptor else None,
                    "language_source": descriptor.source if descriptor else None,
                    "size_bytes": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                },
            )
        return inventory

    def sync_code_manifest(
        self,
        project_root: Path,
        include_tests: bool = False,
        *,
        inventory: list[dict[str, Any]] | None = None,
    ) -> int:
        self.store.init_db(project_root)
        # Reuse a pre-built inventory when the caller already walked (sync_code_files
        # passes its own); otherwise walk once here. Standalone callers unchanged.
        if inventory is None:
            inventory = self._collect_source_inventory(project_root, include_tests)
        manifest_rows: list[tuple[str, str, str | None, str | None, str, int, int]] = []
        seen_paths: set[str] = set()

        for rec in inventory:
            rel = rec["rel"]
            seen_paths.add(rel)
            role = self.store._infer_code_role(project_root, rel, rec["language"], [])
            manifest_rows.append(
                (
                    rel,
                    rec["language"],
                    rec["language_tier"],
                    rec["language_source"],
                    role,
                    rec["size_bytes"],
                    rec["mtime_ns"],
                ),
            )

        with self.store.connect(project_root) as conn:
            existing_paths = {row["path"] for row in conn.execute("SELECT path FROM code_files")}
            stale_paths = existing_paths - seen_paths
            for stale in stale_paths:
                conn.execute("DELETE FROM code_files WHERE path = ?", (stale,))
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (stale,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (stale,))

            for (
                rel,
                language,
                language_tier,
                language_source,
                role,
                size_bytes,
                mtime_ns,
            ) in manifest_rows:
                current = conn.execute(
                    "SELECT size_bytes, mtime_ns FROM code_files WHERE path = ? LIMIT 1",
                    (rel,),
                ).fetchone()
                parsed = 0
                if (
                    current
                    and int(current["size_bytes"] or 0) == size_bytes
                    and int(current["mtime_ns"] or 0) == mtime_ns
                ):
                    parsed_row = conn.execute(
                        "SELECT parsed FROM code_files WHERE path = ? LIMIT 1",
                        (rel,),
                    ).fetchone()
                    parsed = int(parsed_row["parsed"]) if parsed_row else 0
                conn.execute(
                    """
                    INSERT INTO code_files (path, language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                      language=excluded.language,
                      language_tier=excluded.language_tier,
                      language_source=excluded.language_source,
                      role=excluded.role,
                      size_bytes=excluded.size_bytes,
                      mtime_ns=excluded.mtime_ns,
                      parsed=CASE
                        WHEN code_files.size_bytes = excluded.size_bytes AND code_files.mtime_ns = excluded.mtime_ns THEN code_files.parsed
                        ELSE 0
                      END
                    """,
                    (
                        rel,
                        language,
                        language_tier,
                        language_source,
                        "",
                        0,
                        "",
                        role,
                        size_bytes,
                        mtime_ns,
                        parsed,
                    ),
                )
        return len(manifest_rows)

    def sync_code_files(
        self,
        project_root: Path,
        paths: list[str] | None = None,
        include_tests: bool = False,
    ) -> int:
        # ── Phase 1: collect initial include_tests from caller + overrides ──
        provenance_hint: str | None = None
        # Config-level include_tests overrides the default
        from .config import INDEX_INCLUDE_TESTS

        if INDEX_INCLUDE_TESTS:
            include_tests = True
            provenance_hint = "caller_true"
        # When the caller scopes to explicit paths AND any of them look
        # like test files, force-include tests in the walker so the
        # explicit-path filter doesn't silently drop them.
        if paths:
            lowered = {str(p).replace("\\", "/").lower() for p in paths if str(p).strip()}
            if any(
                "/tests/" in p
                or p.startswith("tests/")
                or "/test_" in p
                or p.split("/")[-1].startswith("test_")
                for p in lowered
            ):
                include_tests = True
                provenance_hint = "path_shim"

        # Doctrine 2026-05-29 (king triage — code_index_store flake
        # surfaced by test_degraded_corrupted_file_existing_policy):
        # init_db may raise sqlite3.DatabaseError when the index file
        # has been physically corrupted (test scenario: garbage bytes
        # written over; real scenario: disk/filesystem hiccup mid-
        # checkpoint). The downstream _resolve_include_tests path
        # already conservatively returns False on db read failure, but
        # init_db is called BEFORE the resolver, so its exception
        # propagated all the way up to the caller. Pin: catch
        # DatabaseError here and return 0 — sync can't proceed
        # without a working db, but the caller (reconcile loop /
        # MCP tool) should NOT see an unhandled DatabaseError.
        try:
            self.store.init_db(project_root)
        except sqlite3.DatabaseError:
            return 0

        # ── Phase 2: central effective-policy resolution ──
        include_tests, provenance = _resolve_include_tests(
            include_tests,
            project_root,
            self.store,
            provenance_hint=provenance_hint,
        )

        # ONE shared walk: build the inventory once, feed it to the manifest pass
        # AND reuse it for the parse pass below (was two full source-tree walks +
        # two stat()/language/descriptor passes per file).
        inventory = self._collect_source_inventory(project_root, include_tests)
        self.sync_code_manifest(project_root, include_tests=include_tests, inventory=inventory)
        rows: list[
            tuple[str, str, str | None, str | None, str, int, str, str | None, int, int, int]
        ] = []
        outline_rows: list[tuple[str, str, str, int, str | None, int]] = []
        edge_rows: list[tuple[str, str, str]] = []
        reference_rows: list[tuple[str, int, str, str, str]] = []

        scoped_paths = None
        if paths is not None:
            scoped_paths = {item.replace("\\", "/") for item in paths if str(item).strip()}

        existing_meta = {}
        with self.store.connect(project_root) as conn:
            for row in conn.execute(
                "SELECT path, checksum, size_bytes, mtime_ns, language, language_tier, language_source, line_count, summary, role, parsed FROM code_files",
            ):
                existing_meta[row["path"]] = dict(row)

        seen_paths: set[str] = set()
        # Parsed-row reuse: unchanged files keep their stored outlines/edges. We
        # collect them here and refetch ALL of them in ONE batched connection
        # after the loop (was: one connection per reused file → 828 opens on a
        # no-op reconcile of the live tree).
        reused_paths: list[str] = []
        for rec in inventory:
            rel = rec["rel"]
            if scoped_paths is not None and rel not in scoped_paths:
                continue
            seen_paths.add(rel)
            path = rec["path"]
            code_language = rec["language"]
            language_tier = rec["language_tier"]
            language_source = rec["language_source"]
            size_bytes = rec["size_bytes"]
            mtime_ns = rec["mtime_ns"]

            existing = existing_meta.get(rel)
            if (
                existing
                and existing.get("size_bytes") == size_bytes
                and existing.get("mtime_ns") == mtime_ns
            ):
                parsed = int(existing.get("parsed") or 0)
                if parsed == 1:
                    rows.append(
                        (
                            rel,
                            str(existing["language"]),
                            existing.get("language_tier"),
                            existing.get("language_source"),
                            str(existing["checksum"]),
                            int(existing["line_count"]),
                            str(existing["summary"]),
                            existing.get("role"),
                            size_bytes,
                            mtime_ns,
                            1,
                        ),
                    )
                    # Outlines/edges for this reused file are refetched in the
                    # post-loop batch (see reused_paths) — no per-file connection.
                    reused_paths.append(rel)
                    continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            line_count = len(text.splitlines())
            summary = self.store._summarize(text, path.name)
            outlines = self.store._extract_outline(project_root, text, code_language)
            role = self.store._infer_code_role(project_root, rel, code_language, outlines)
            rows.append(
                (
                    rel,
                    code_language,
                    language_tier,
                    language_source,
                    checksum,
                    line_count,
                    summary,
                    role,
                    size_bytes,
                    mtime_ns,
                ),
            )
            rows[-1] = (*rows[-1], 1)
            outline_rows.extend(
                (rel, symbol, kind, line_number, container, 1 if is_partial else 0)
                for symbol, kind, line_number, container, is_partial in outlines
            )
            edge_rows.extend(
                (rel, target, kind)
                for target, kind in self.store._extract_edges(text, code_language)
            )
            reference_rows.extend(
                (rel, line_number, token, kind, raw)
                for token, line_number, kind, raw in self.store._extract_references(
                    project_root, text, code_language
                )
            )

        # Batched refetch of reused files' stored outlines/edges in a SINGLE
        # connection (was one connection per reused file). Chunked IN keeps each
        # query under SQLite's bound-parameter limit. Identical rows — the final
        # dict.fromkeys dedup + INSERT below are unchanged; row CONTENT (hence
        # search/index results) is byte-identical to the per-file fetch.
        if reused_paths:
            with self.store.connect(project_root) as conn:
                for i in range(0, len(reused_paths), 500):
                    chunk = reused_paths[i : i + 500]
                    qmarks = ",".join("?" * len(chunk))
                    for row in conn.execute(
                        "SELECT path, symbol, kind, line_number, container, is_partial "
                        f"FROM code_outlines WHERE path IN ({qmarks})",
                        chunk,
                    ):
                        outline_rows.append(
                            (
                                row["path"],
                                row["symbol"],
                                row["kind"],
                                int(row["line_number"]),
                                row["container"],
                                int(row["is_partial"]),
                            ),
                        )
                    for row in conn.execute(
                        f"SELECT source_path, target, kind FROM code_edges WHERE source_path IN ({qmarks})",
                        chunk,
                    ):
                        edge_rows.append((row["source_path"], row["target"], row["kind"]))
                    for row in conn.execute(
                        f"SELECT path, line_number, token, kind, raw FROM code_references WHERE path IN ({qmarks})",
                        chunk,
                    ):
                        reference_rows.append(
                            (
                                row["path"],
                                int(row["line_number"]),
                                row["token"],
                                row["kind"],
                                row["raw"],
                            )
                        )

        with self.store.connect(project_root) as conn:
            targets_to_replace = scoped_paths if scoped_paths is not None else seen_paths
            for rel in targets_to_replace:
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (rel,))
                conn.execute("DELETE FROM code_references WHERE path = ?", (rel,))
            outline_rows = list(dict.fromkeys(outline_rows))
            edge_rows = list(dict.fromkeys(edge_rows))
            reference_rows = list(dict.fromkeys(reference_rows))
            conn.executemany(
                "INSERT OR REPLACE INTO code_files (path, language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO code_outlines (path, symbol, kind, line_number, container, is_partial) VALUES (?, ?, ?, ?, ?, ?)",
                outline_rows,
            )
            conn.executemany(
                "INSERT INTO code_edges (source_path, target, kind) VALUES (?, ?, ?)",
                edge_rows,
            )
            conn.executemany(
                "INSERT INTO code_references (path, line_number, token, kind, raw) VALUES (?, ?, ?, ?, ?)",
                reference_rows,
            )
        # Record index policy after successful write
        effective_policy_label = (
            "include_tests:"
            + str(include_tests).lower()
            + ",provenance:"
            + provenance
            + ",pid:"
            + str(os.getpid())
        )
        self.store._write_index_policy(
            project_root,
            include_tests=include_tests,
            effective_policy_label=effective_policy_label,
            provenance=provenance,
        )
        return len(rows)

    def sync_session_code(
        self,
        project_root: Path,
        session_id: str,
        include_tests: bool = False,
    ) -> int:
        if self.store.session_store is None:
            raise RuntimeError("SessionStore is required for session-guided code sync")
        paths = self.store.session_store.session_code_targets(project_root, session_id)
        if not include_tests and paths:
            # Session plans may mention test files. Respect
            # include_tests=False by stripping them BEFORE sync_code_files,
            # so the auto-include shim inside that method doesn't flip
            # the flag back on. Post-edit reindex paths (mcp_server.py:264
            # etc.) call sync_code_files directly, keeping the shim.
            def _is_test_path(p: str) -> bool:
                low = p.replace("\\", "/").lower()
                leaf = low.split("/")[-1]
                return (
                    "/tests/" in low
                    or low.startswith("tests/")
                    or "/test_" in low
                    or leaf.startswith("test_")
                    or ".test." in leaf
                    or ".spec." in leaf
                )

            paths = [p for p in paths if not _is_test_path(str(p))]
        return self.store.sync_code_files(project_root, paths=paths, include_tests=include_tests)

    def code_index_db_status(self, project_root: Path) -> dict[str, object]:
        """Cheap DB-ONLY index status — counts + a derived ``db_state``, with NO
        filesystem walk and NO per-file hashing. Used by the reconcile poll path
        so the normal sitter cadence never pays for a full freshness walk.

        ``db_state``:
          * ``empty``    — no tracked code_files rows.
          * ``unparsed`` — rows exist but some have parsed=0 (a sync left work
                           undone / a parse failed).
          * ``ready``    — rows exist and every one is parsed.

        This reflects the index's INTERNAL consistency with the last sync, not
        on-disk drift. Drift detection (sha256 per file) lives in
        ``code_status``/``_code_freshness`` and is reserved for explicit
        check/sync diagnostics. The known-stale flag + poll-window-risk window
        cover the "index may be behind disk" case for the cheap path.
        """
        self.store.init_db(project_root)
        with self.store.connect(project_root) as conn:
            code_count = int(conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0])
            parsed_count = int(
                conn.execute("SELECT COUNT(*) FROM code_files WHERE parsed = 1").fetchone()[0],
            )
            outline_count = int(conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0])
            edge_count = int(conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0])
        unparsed = code_count - parsed_count
        if code_count == 0:
            db_state = "empty"
        elif unparsed > 0:
            db_state = "unparsed"
        else:
            db_state = "ready"
        return {
            "db_path": str(self.store.db_path(project_root)),
            "code_files": code_count,
            "parsed_code_files": parsed_count,
            "unparsed_code_files": unparsed,
            "code_outlines": outline_count,
            "code_edges": edge_count,
            "db_state": db_state,
        }

    def code_status(self, project_root: Path) -> dict[str, object]:
        self.store.init_db(project_root)
        with self.store.connect(project_root) as conn:
            code_count = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
            parsed_count = conn.execute(
                "SELECT COUNT(*) FROM code_files WHERE parsed = 1",
            ).fetchone()[0]
            outline_count = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]
            partial_count = conn.execute(
                "SELECT COUNT(*) FROM code_outlines WHERE is_partial = 1",
            ).fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
            role_rows = conn.execute(
                "SELECT COALESCE(role, 'unknown') AS role, COUNT(*) AS count FROM code_files GROUP BY COALESCE(role, 'unknown') ORDER BY count DESC, role ASC",
            ).fetchall()
            tier_rows = conn.execute(
                "SELECT COALESCE(language_tier, 'unknown') AS tier, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_tier, 'unknown') ORDER BY count DESC, tier ASC",
            ).fetchall()
            source_rows = conn.execute(
                "SELECT COALESCE(language_source, 'unknown') AS source, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_source, 'unknown') ORDER BY count DESC, source ASC",
            ).fetchall()
        roles = {row["role"]: int(row["count"]) for row in role_rows}
        tiers = {row["tier"]: int(row["count"]) for row in tier_rows}
        sources = {row["source"]: int(row["count"]) for row in source_rows}
        role_groups: dict[str, int] = {}
        for role, count in roles.items():
            group = self.store._role_group(role)
            role_groups[group] = role_groups.get(group, 0) + count
        return {
            "db_path": str(self.store.db_path(project_root)),
            "code_files": int(code_count),
            "parsed_code_files": int(parsed_count),
            "code_outlines": int(outline_count),
            "partial_symbols": int(partial_count),
            "code_edges": int(edge_count),
            "roles": roles,
            "role_groups": role_groups,
            "language_tiers": tiers,
            "language_sources": sources,
            "freshness": bound_freshness_paths(self._code_freshness(project_root)),
        }

    def _code_freshness(self, project_root: Path) -> dict[str, object]:
        indexed_rows: dict[str, sqlite3.Row] = {}
        with self.store.connect(project_root) as conn:
            for row in conn.execute(
                "SELECT path, checksum, mtime_ns, parsed FROM code_files ORDER BY path",
            ):
                indexed_rows[str(row["path"])] = row

            include_tests = any(self.store._path_looks_like_test(path) for path in indexed_rows)
        # Hoist the descriptor snapshot + root resolve OUT of the per-file loop:
        # load once, match in-memory per file. Previously each file re-entered
        # load_language_descriptors (and its project_root.resolve()), producing a
        # realpath storm. language_from_snapshot is the same matcher
        # language_for_custom_descriptor uses, so language attribution is
        # byte-identical — only the per-file I/O is removed.
        from .language_descriptors import language_from_snapshot, load_language_descriptors

        descriptor_snapshot = load_language_descriptors(project_root)
        root_prefix = posix_root_prefix(project_root)
        tracked_paths: dict[str, dict[str, int | str]] = {}
        for path in self.store._walk_source_files(project_root, include_tests=include_tests):
            # ONE stat replaces the prior is_file() + stat() pair (is_file() is
            # itself a stat). Identical skip semantics: a vanished/non-regular
            # entry is skipped (is_file() returns False / raised OSError before).
            try:
                st = path.stat()
            except OSError:
                continue
            if not S_ISREG(st.st_mode):
                continue
            # Root-relative posix path via the shared cheap helper (handles
            # relative/absolute/drive/fs roots + '.'), equal to
            # relative_to(project_root).as_posix() for files under root without
            # the per-file pathlib walk cost.
            rel = relpath_posix(path, root_prefix)
            code_language = language_from_snapshot(descriptor_snapshot, rel, path.suffix.lower())
            if code_language is None:
                continue
            # Checksum read path UNCHANGED: read_text applies universal-newline
            # translation (CRLF->LF) that read_bytes would NOT, and sync_code_files
            # stores the checksum the same read_text way (line ~404). Switching to
            # read_bytes would change the checksum bytes on CRLF files and break
            # exact drift parity — so this stays read_text verbatim.
            checksum = hashlib.sha256(
                path.read_text(encoding="utf-8", errors="ignore").encode("utf-8"),
            ).hexdigest()
            tracked_paths[rel] = {
                "mtime_ns": int(st.st_mtime_ns),
                "checksum": checksum,
            }

        indexed_path_set = set(indexed_rows)
        tracked_path_set = set(tracked_paths)
        missing_paths = sorted(tracked_path_set - indexed_path_set)
        extra_paths = sorted(indexed_path_set - tracked_path_set)
        drifted_paths = sorted(
            path
            for path in tracked_path_set & indexed_path_set
            if str(indexed_rows[path]["checksum"] or "") != str(tracked_paths[path]["checksum"])
        )
        unparsed_paths = sorted(
            path for path, row in indexed_rows.items() if int(row["parsed"] or 0) != 1
        )
        reasons: list[str] = []
        if missing_paths or extra_paths:
            reasons.append("path_drift")
        if drifted_paths:
            reasons.append("content_drift")
        if unparsed_paths or (tracked_paths and not indexed_rows):
            reasons.append("missing_index_state")
        if tracked_paths and not indexed_rows:
            state = "missing"
        elif reasons:
            state = "stale"
        else:
            state = "ready"
        latest_source_mtime_ns = max(
            (int(meta["mtime_ns"]) for meta in tracked_paths.values()),
            default=None,
        )
        latest_indexed_mtime_ns = max(
            (int(row["mtime_ns"] or 0) for row in indexed_rows.values()),
            default=None,
        )
        return {
            "state": state,
            "reasons": reasons,
            "tracked_paths": len(tracked_paths),
            "indexed_paths": len(indexed_rows),
            "drifted_paths": sorted(dict.fromkeys(missing_paths + drifted_paths + extra_paths)),
            "missing_paths": missing_paths,
            "extra_paths": extra_paths,
            "unparsed_paths": unparsed_paths,
            "latest_source_mtime_ns": latest_source_mtime_ns,
            "latest_indexed_mtime_ns": latest_indexed_mtime_ns,
        }
