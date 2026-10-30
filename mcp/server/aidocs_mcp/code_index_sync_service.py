from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from stat import S_ISREG
from typing import Any

from .relpath_util import posix_root_prefix, relpath_posix

from .csharp_outliner_artifact import OutlinerUnavailable
from .language_descriptors import descriptor_for_language
from .symbol_hygiene import clean_outline_symbol

# #755/#756: the ONE canonical connect. The single site here is the
# readability PROBE -- one `SELECT 1 FROM config_settings` -- opened with
# no pragmas at all. read_only=True is the truthful mode for a probe, and
# it also stops the probe MATERIALISING an empty db when the file is
# missing, which is the boundary _sqlite_connect already refuses to cross.
from ._sqlite_connect import connect as _canonical_connect

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
        conn = _canonical_connect(str(db_path), read_only=True, row_factory=False)
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
        # #1039: a FULL walk is the authority on what was refused — the
        # refused-source ledger becomes exactly that set. Best-effort: a
        # ledger hiccup must never fail the sync.
        try:
            self.store.record_excluded(
                project_root,
                list(getattr(self.store, "_walk_refused", []) or []),
                replace=True,
            )
        except Exception:
            pass
        return inventory

    def _drop_tree_excluded(self, project_root: Path, scoped_paths: set[str]) -> set[str]:
        from .index_tree_policy import EXCLUSION_RULE, diagnostics, excluded_rel_reason

        kept: set[str] = set()
        excluded: list[tuple[str, str]] = []
        for rel in scoped_paths:
            reason = excluded_rel_reason(project_root, rel)
            if reason is None:
                kept.add(rel)
            else:
                excluded.append((rel, reason))
        reasons: dict[str, int] = {}
        by_reason: dict[str, list[str]] = {}
        for rel, reason in sorted(excluded):
            reasons[reason] = reasons.get(reason, 0) + 1
            bucket = by_reason.setdefault(reason, [])
            if len(bucket) < 5:
                bucket.append(rel)
        self.store._last_tree_excluded = {
            "count": len(excluded),
            "reasons": reasons,
            "examples": [{"path": r, "reason": why} for r, why in sorted(excluded)[:5]],
            "diagnostics": diagnostics(reasons, by_reason),
            "rule": EXCLUSION_RULE,
        }
        if excluded:
            self._purge_rows(project_root, [rel for rel, _ in excluded])
            # A SYSTEM DNT row such a path left behind (the pre-policy walk did
            # not prune `.tmp/`) is stale too; operator rows are kept.
            try:
                from .dnt_registry_sync import reconcile_system_dnt_rows

                self.store._last_tree_excluded["dnt_reconciled"] = reconcile_system_dnt_rows(
                    project_root, paths=[rel for rel, _ in excluded]
                )
            except Exception as exc:  # noqa: BLE001 - reported, never fails the sync
                self.store._last_tree_excluded["dnt_reconciled"] = {"error": f"{type(exc).__name__}: {exc}"}
                from .project_index_sitter import mark_index_known_stale

                mark_index_known_stale(project_root)  # visible, never clean
        return kept

    def _reconcile_dnt(
        self,
        project_root: Path,
        *,
        scoped_paths: set[str] | None,
        inventory: list[dict[str, Any]],
        reparsed: set[str],
        found: list[str],
        absent: set[str],
        writes_ok: bool,
        registry: Any = None,
    ) -> dict[str, Any]:
        """DNT reconcile of this sync, PER FILE (r0b2 d6b3dac7-69a).

        Scoped (delta / post-edit): exactly its paths; never claims whole-
        registry verification. Full: a system row is TRUSTED when its file's
        current version and the row's content digest match the file's
        attestation (dnt_registry_sync.attested_trust); every other row - and
        every attested-with-header file whose row is gone - is checked on
        disk. There is no global "registry verified" token any more: an
        external rewrite of one row breaks exactly that row's digest, so a
        concurrent writer can never get a stale state blessed; it is caught
        on the next check of that file (read / edit / next sync).
        ``verified`` = this full pass checked or trusted every row, wrote
        everything it had to, and kept nothing unread."""
        from .dnt_registry_sync import attested_trust, reconcile_system_dnt_rows
        from .protected_file_registry_store import ProtectedFileRegistryStore

        if registry is None:
            registry = ProtectedFileRegistryStore()
        versions = {rec["rel"]: (int(rec["mtime_ns"]), int(rec["size_bytes"])) for rec in inventory}
        if scoped_paths is not None:
            out = reconcile_system_dnt_rows(
                project_root,
                paths=scoped_paths,
                known_present=found,
                known_absent=absent,
                versions=versions,
                registry=registry,
            )
            out["mode"] = "scoped"
            out["verified"] = False  # a scoped pass never verifies the whole registry
            return out
        trusted, also = attested_trust(
            project_root, registry=registry, inventory_versions=versions, reparsed=reparsed
        )
        out = reconcile_system_dnt_rows(
            project_root,
            known_present=found,
            known_absent=absent,
            trusted=trusted,
            also_check=also,
            versions=versions,
            registry=registry,
        )
        out["mode"] = "attested"
        out["verified"] = bool(writes_ok and not out.get("error_count") and not out["kept_unreadable"])
        return out

    @staticmethod
    def _content_differs(path: Path, existing: dict[str, Any], found: list[str], rel: str) -> bool:
        """Integrity check of a stat-identical file: its content checksum (the
        exact hash the parse pass stores) against the indexed one."""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return True  # let the parse pass decide (it drops / raises as usual)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != str(existing.get("checksum") or ""):
            found.append(rel)
            return True
        return False

    def _purge_rows(self, project_root: Path, rels: list[str]) -> None:
        """Delete every index row of ``rels`` - only when one exists, so a
        clean index pays a read, never a write transaction."""
        self.store.init_db(project_root)
        qm = ",".join("?" * len(rels))
        with self.store.connect(project_root) as conn:
            present = [
                r[0]
                for r in conn.execute(
                    f"SELECT path FROM code_files WHERE path IN ({qm})", tuple(rels)
                )
            ]
            for rel in present:
                conn.execute("DELETE FROM code_files WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (rel,))
                conn.execute("DELETE FROM code_references WHERE path = ?", (rel,))

    def _collect_scoped_inventory(
        self,
        project_root: Path,
        scoped_paths: set[str],
        include_tests: bool = False,
        *,
        dropped: list[str] | None = None,
    ) -> list[dict[str, Any]] | None:
        """#224: build inventory records for ONLY the scoped paths — no
        source-tree walk.

        Returns None (→ caller falls back to the full walk) when ANY scoped
        path is missing on disk: a deletion needs the tree-wide manifest
        stale-sweep to remove its rows and any siblings, which the fast path
        deliberately skips. Scoped paths that exist but resolve to
        language=None (e.g. a .md file) are simply omitted — same as the full
        walk, which never indexes them.
        """
        from .code_index_store import (
            _INDEX_SOURCE_SUFFIXES,
            _is_sensitive_for_index,
            may_enter_index,
        )

        inventory: list[dict[str, Any]] = []
        refused: list[tuple[str, str]] = []
        admitted: list[str] = []
        for rel in scoped_paths:
            abs_path = project_root / rel
            if not abs_path.is_file():
                if dropped is not None:
                    dropped.append(rel)  # strict read: never the full walk
                    continue
                return None  # deletion (or a bad path) — fall back to full walk
            # THE SAME ADMISSION AUTHORITY THE FULL WALK USES (#1039).
            #
            # This fast path existed to skip the source-tree WALK, and skipped
            # every check inside it as a side effect — the name blocklist and
            # the content gate both. So a `.env` or a token-bearing source file
            # reached by a scoped sync was INDEXED, while the identical file
            # found by the full walk was refused. Two answers to one question,
            # decided by which code path happened to run.
            #
            # Not an edge case: `sync_session_code` funnels here, and this
            # module's own note says post-edit reindex calls `sync_code_files`
            # directly — so the ordinary edit-then-reindex cycle IS this path.
            # A secret pasted into a file and saved was indexed on the next
            # keystroke-triggered sync and searchable from then on.
            if not may_enter_index(rel, abs_path):
                # #1039: same ledger the full walk feeds — a refusal on the
                # post-edit path is just as invisible to a search.
                if rel.lower().endswith(_INDEX_SOURCE_SUFFIXES):
                    refused.append(
                        (
                            rel,
                            "name_blocked" if _is_sensitive_for_index(rel) else "content_refused",
                        ),
                    )
                if dropped is not None:
                    dropped.append(rel)  # strict read: its old rows must go
                continue
            admitted.append(rel)
            try:
                code_language = self.store._language_for(abs_path, project_root=project_root)
                if code_language is None:
                    # No longer indexable (language/descriptor eligibility
                    # changed): the full sweep would remove its row, so a
                    # strict read drops it too instead of serving its old
                    # summary (r0b2 d40ad161-ef6).
                    if dropped is not None:
                        dropped.append(rel)
                    continue
                descriptor = descriptor_for_language(project_root, rel, abs_path.suffix.lower())
                stat = abs_path.stat()
            except OSError:
                # Deleted / unreadable between the is_file() check and here.
                if dropped is None:
                    raise
                dropped.append(rel)  # strict read: drop it, never the full walk
                continue
            inventory.append(
                {
                    "rel": rel,
                    "path": abs_path,
                    "language": code_language,
                    "language_tier": descriptor.tier if descriptor else None,
                    "language_source": descriptor.source if descriptor else None,
                    "size_bytes": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                },
            )
        if refused or admitted:
            try:
                self.store.record_excluded(
                    project_root, refused, replace=False, admitted=admitted,
                )
            except Exception:
                pass
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
                # #1121 (r0b1): the sweep cleared a removed file's files /
                # outlines / edges but not its references, so ghost rows
                # outlived the file and kept surfacing in reference search.
                conn.execute("DELETE FROM code_references WHERE path = ?", (stale,))

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
                    "SELECT size_bytes, mtime_ns, parsed, language, language_tier, "
                    "language_source, role FROM code_files WHERE path = ? LIMIT 1",
                    (rel,),
                ).fetchone()
                parsed = 0
                if (
                    current
                    and int(current["size_bytes"] or 0) == size_bytes
                    and int(current["mtime_ns"] or 0) == mtime_ns
                ):
                    parsed = int(current["parsed"] or 0)
                    # #1121: an unchanged file's manifest row is already
                    # exactly what the upsert below would write. Skipping it
                    # keeps a no-change poll from rewriting every code_files
                    # row inside a write transaction on the single-writer DB.
                    if (
                        current["language"] == language
                        and current["language_tier"] == language_tier
                        and current["language_source"] == language_source
                        and current["role"] == role
                    ):
                        continue
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
        incremental: bool = False,
        read_hydrate: bool = False,
        dnt_verdict_out: dict[str, Any] | None = None,
        delta: bool = False,
        integrity: bool = False,
    ) -> int:
        # DELTA (scoped sitter sync): exactly the given paths - no tree walk, no
        # manifest sweep; a missing path is a DELETION (its rows go, here),
        # never a fallback to the full walk; per-file DNT rows follow the same
        # paths. A delta never marks the root known-stale for a deletion and
        # never claims enumeration coverage (the sitter owns both).
        strict_scope = read_hydrate or delta
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

        # Doctrine 2026-05-29 (Empire triage — code_index_store flake
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
        scoped_paths = None
        if paths is not None:
            scoped_paths = {item.replace("\\", "/") for item in paths if str(item).strip()}
            # MAIN-INDEX ADMISSION (r0b2 worktree isolation): a scoped path
            # inside `.tmp/`, a nested git worktree, a dot-dir or a link into
            # one never becomes a main-index row. The walk already prunes
            # these; this is the row-write boundary, because the post-edit
            # reindex of an AIDOCS edit made INSIDE a worktree arrives here
            # with `.tmp/wt-x/...` and no walk at all. Any row such a path
            # left behind before this check is purged (without marking the
            # index stale: the main tree did not change). Reported on
            # store._last_tree_excluded, never silent.
            requested = len(scoped_paths)
            scoped_paths = self._drop_tree_excluded(project_root, scoped_paths)
            if requested and not scoped_paths:
                if dnt_verdict_out is not None:
                    # every path was outside the main tree: purged above,
                    # nothing left to verify - an explicit (scoped) verdict
                    dnt_verdict_out.update(
                        project_root=str(project_root),
                        full_sync=False,
                        report={"ok": True, "status": "ok", "excluded_only": True, "errors": []},
                        tree_exclusions=None,
                    )
                return 0  # every scoped path was outside the main tree

        # #224 scoped fast-path: a post-edit reindex of specific EXISTING files
        # must not pay full-tree cost. The old path ALWAYS walked the whole
        # source tree (stat + language + descriptor per file) and ran a full
        # manifest deletion-sweep even to sync one file — ~12s per edit on a
        # 2200-file project. When every scoped path exists on disk we build the
        # inventory from ONLY those paths and skip the manifest: the parse loop
        # below writes each scoped file's own rows (reused or reparsed), so the
        # manifest's extra jobs (tree-wide stale sweep, unchanged-file upsert)
        # are unnecessary for a scoped edit. A MISSING scoped path (a deletion)
        # returns None → full walk, so the stale sweep still removes it.
        #
        # OPT-IN (incremental=True): only callers that KNOW this is an edit of
        # existing files — the post-edit reindex — take the fast path. Callers
        # that scope the PARSE but still want the full-tree manifest side-effect
        # (sync_session_code records every file as a placeholder; full sync)
        # leave incremental=False and keep the walk.
        inventory: list[dict[str, Any]] | None = None
        walk_pruned: dict[str, Any] | None = None  # set only when this sync walks the tree
        # READ HYDRATION (r0b2 b5eaa857-2b0 S1): a search lazily parsing its
        # parsed=0 candidates. STRICT: never the full walk, never the manifest
        # sweep - a missing or refused candidate is dropped (its rows deleted
        # below) instead of sending the read down the whole tree.
        dropped: list[str] = []
        integrity_mismatches: list[str] = []  # same-stat content rewrites found by an integrity pass
        if strict_scope:
            if not scoped_paths:
                return 0
            inventory = self._collect_scoped_inventory(
                project_root, scoped_paths, include_tests, dropped=dropped
            )
        elif incremental and scoped_paths:
            inventory = self._collect_scoped_inventory(
                project_root, scoped_paths, include_tests
            )
        if inventory is None:
            # ONE shared walk: build the inventory once, feed it to the manifest
            # pass AND reuse it for the parse pass below (was two full walks).
            inventory = self._collect_source_inventory(project_root, include_tests)
            from .code_index_store import walk_report_for_this_thread

            walk_pruned = walk_report_for_this_thread(self.store)  # THIS walk's prune report
            self.sync_code_manifest(
                project_root, include_tests=include_tests, inventory=inventory
            )
        rows: list[
            tuple[str, str, str | None, str | None, str, int, str, str | None, int, int, int]
        ] = []
        outline_rows: list[tuple[str, str, str, int, str | None, int]] = []
        edge_rows: list[tuple[str, str, str]] = []
        reference_rows: list[tuple[str, int, str, str, str, str]] = []

        # Scope the existing-metadata read to the scoped paths on the fast path —
        # a full ``code_files`` table scan would re-introduce O(project) cost.
        existing_meta = {}
        _meta_sql = (
            "SELECT path, checksum, size_bytes, mtime_ns, language, language_tier, "
            "language_source, line_count, summary, role, parsed FROM code_files"
        )
        with self.store.connect(project_root) as conn:
            if scoped_paths:
                _qm = ",".join("?" * len(scoped_paths))
                _meta_rows = conn.execute(
                    f"{_meta_sql} WHERE path IN ({_qm})", tuple(scoped_paths)
                )
            else:
                _meta_rows = conn.execute(_meta_sql)
            for row in _meta_rows:
                existing_meta[row["path"]] = dict(row)

        seen_paths: set[str] = set()
        # Parsed-row reuse: unchanged files keep their stored outlines/edges. We
        # collect them here and refetch ALL of them in ONE batched connection
        # after the loop (was: one connection per reused file → 828 opens on a
        # no-op reconcile of the live tree).
        reused_paths: list[str] = []
        # Files actually REPARSED this pass (changed/new — not metadata-reused).
        # This is the bounded scope handed to the LSP joint at the end.
        changed_paths: list[str] = []
        from .dnt_registry_sync import DNT_SCAN_EXTS as _DNT_EXTS
        from .dnt_registry_sync import header_from_index_text as _dnt_header_from_text

        dnt_scanned = 0
        dnt_found: list[tuple[str, Any]] = []
        dnt_absent: set[str] = set()  # reparsed DNT-ext files with NO header
        dnt_parse_errors: list[dict[str, str]] = []
        # C# family files whose REQUIRED outliner was unavailable this pass:
        # rel -> (reason, detail). Written durably below.
        not_indexed: dict[str, tuple[str, str]] = {}
        cs_unavailable: tuple[str, str] | None = None
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
                # INTEGRITY sweep: stat equality is not content equality (a
                # same-size rewrite inside one mtime tick keeps both) - the
                # forced sweep re-hashes and reparses on a checksum mismatch.
                and not (integrity and self._content_differs(path, existing, integrity_mismatches, rel))
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
            changed_paths.append(rel)
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                if not strict_scope:
                    raise
                # Deleted (or made unreadable) after the inventory was built:
                # drop it like a missing candidate, never fall back to a walk.
                changed_paths.pop()
                seen_paths.discard(rel)
                dropped.append(rel)
                continue
            # INDEX-DERIVED DNT (f55b9ad9-1c2): the header is parsed from the
            # text this pass already read - no separate tree walk, no second
            # read. Unchanged files are not re-read and their registry rows
            # are already current.
            if path.suffix.lower() in _DNT_EXTS:
                dnt_scanned += 1
                try:
                    _hdr = _dnt_header_from_text(text)
                    if _hdr.is_present:
                        dnt_found.append((rel, _hdr))
                    else:
                        dnt_absent.add(rel)
                except Exception as exc:  # noqa: BLE001 - reported below
                    dnt_parse_errors.append({"path": rel, "error": f"parse failed: {type(exc).__name__}"})
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            line_count = len(text.splitlines())
            summary = self.store._summarize(text, path.name)
            # C# FAMILY: the trusted Roslyn outliner is REQUIRED. Unavailable
            # => the file is tracked but NOT_INDEXED (parsed=0, a durable
            # reason row, NO symbols/edges/references -- its old rows are
            # deleted below with every other replaced path). One failure
            # short-circuits the rest of this pass: the verdict is about the
            # toolchain, not the file, and re-verifying per file is waste.
            is_cs_family = path.suffix.lower() in self.store.CSHARP_FAMILY_SUFFIXES
            outlines: list[tuple[str, str, int, str | None, bool]] = []
            if is_cs_family and cs_unavailable is not None:
                not_indexed[rel] = cs_unavailable
            else:
                try:
                    outlines = self.store._extract_outline(project_root, text, code_language)
                except OutlinerUnavailable as exc:
                    if not is_cs_family:
                        raise
                    cs_unavailable = (exc.reason, exc.detail[:400])
                    not_indexed[rel] = cs_unavailable
            if rel in not_indexed:
                rows.append(
                    (
                        rel,
                        code_language,
                        language_tier,
                        language_source,
                        checksum,
                        line_count,
                        summary,
                        None,
                        size_bytes,
                        mtime_ns,
                        0,
                    ),
                )
                continue
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
            # Identifier hygiene (War AW): validate/repair every symbol at
            # outline-write time — garbage fragments (truncated TSX
            # identifiers, punctuation-bearing strings) never reach
            # code_outlines. Unrepairable symbols drop the row.
            outline_rows.extend(
                (rel, cleaned, kind, line_number, container, 1 if is_partial else 0)
                for symbol, kind, line_number, container, is_partial in outlines
                if (cleaned := clean_outline_symbol(symbol, kind)) is not None
            )
            edge_rows.extend(
                (rel, target, kind)
                for target, kind in self.store._extract_edges(text, code_language)
            )
            reference_rows.extend(
                (rel, line_number, token, kind, raw, enclosing)
                for token, line_number, kind, raw, enclosing in self.store._extract_references(
                    project_root, text, code_language
                )
            )

        # #1121: REUSED FILES ARE NOT REWRITTEN. This used to refetch every
        # reused file's stored outlines/edges/references and then, in the one
        # write transaction below, DELETE and re-INSERT them byte for byte.
        # The index sitter runs a FULL sync on every 30s poll and every
        # file-save trigger, so a no-change pass rewrote the whole code index:
        # measured 2026-09-28, a ~16s write transaction putting 183 MB into the
        # WAL (907k code_references rows), while every hook that needed the
        # single-writer lock to append an audit row waited 5-8s behind it
        # (broker ring: PreToolUse p95 13.4s, 58/97 late). A reused file's
        # stored rows are already exactly what a rewrite would produce, so the
        # replace set is the changed files only, plus any scoped path that is
        # not a reused file (a scoped deletion still clears its rows). Pinned by
        # tests/indexing/test_full_sync_rewrites_only_changed_files_1121.py,
        # including a parity oracle against a fresh index.
        reused = set(reused_paths)
        write_rows = [row for row in rows if row[0] not in reused]

        with self.store.connect(project_root) as conn:
            targets_to_replace = (
                scoped_paths if scoped_paths is not None else seen_paths
            ) - reused
            for rel in targets_to_replace:
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (rel,))
                conn.execute("DELETE FROM code_references WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_files_not_indexed WHERE path = ?", (rel,))
            if not_indexed:
                from datetime import UTC as _UTC
                from datetime import datetime as _dt

                _now = _dt.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.executemany(
                    "INSERT OR REPLACE INTO code_files_not_indexed "
                    "(path, language, reason, detail, recorded_at) VALUES (?, 'csharp', ?, ?, ?)",
                    [(rel, why, detail, _now) for rel, (why, detail) in not_indexed.items()],
                )
            # A read-hydration candidate that is gone or now refused loses its
            # file row too, in this same transaction - exactly what the full
            # sweep would do for it, bounded to it. Never left searchable.
            for rel in dropped:
                conn.execute("DELETE FROM code_files WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_files_not_indexed WHERE path = ?", (rel,))
            outline_rows = list(dict.fromkeys(outline_rows))
            edge_rows = list(dict.fromkeys(edge_rows))
            reference_rows = list(dict.fromkeys(reference_rows))
            # Upsert (NOT INSERT OR REPLACE): REPLACE deletes+reinserts the
            # row, silently nulling columns absent from the column list —
            # notably `module`, which wiped every sync_modules tag on each
            # reindex and made ai_get_module_files return [] while
            # ai_get_modules still listed the module (phoenix bug 1).
            conn.executemany(
                """
                INSERT INTO code_files (path, language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  language=excluded.language,
                  language_tier=excluded.language_tier,
                  language_source=excluded.language_source,
                  checksum=excluded.checksum,
                  line_count=excluded.line_count,
                  summary=excluded.summary,
                  role=excluded.role,
                  size_bytes=excluded.size_bytes,
                  mtime_ns=excluded.mtime_ns,
                  parsed=excluded.parsed
                """,
                write_rows,
            )
            conn.executemany(
                "INSERT INTO code_outlines (path, symbol, kind, line_number, container, is_partial) VALUES (?, ?, ?, ?, ?, ?)",
                outline_rows,
            )
            conn.executemany(
                "INSERT INTO code_edges (source_path, target, kind) VALUES (?, ?, ?)",
                edge_rows,
            )
            # INSERT OR IGNORE, not plain INSERT: the PK is
            # (path, line_number, token, kind) but `enclosing` is NOT part of
            # it, so two rows can differ only in `enclosing` and collide.
            # Real case: `f(x) if p else (lambda: f(y))()` on one line — same
            # name, same line, different enclosing scope. Whole-row dedup
            # above cannot merge those, and a plain INSERT would abort the
            # entire sync on the IntegrityError. First row wins; see the
            # writer notes for the proposed PK widening.
            conn.executemany(
                "INSERT OR IGNORE INTO code_references "
                "(path, line_number, token, kind, raw, enclosing) VALUES (?, ?, ?, ?, ?, ?)",
                reference_rows,
            )
        # A READ hydration never rewrites the index policy: include_tests and
        # its provenance belong to the sitter and to explicit syncs. A search
        # that lazily parsed a tests/ candidate (path shim -> include_tests
        # True) would otherwise flip the stored policy, and the sitter's next
        # reconcile flip it back (r0b2 b5eaa857-2b0).
        if not strict_scope:
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
        # ONE registry instance carries every DNT write of this pass.
        from .protected_file_registry_store import ProtectedFileRegistryStore as _Pfrs

        dnt_registry = _Pfrs()
        dnt_versions = {rec["rel"]: (int(rec["mtime_ns"]), int(rec["size_bytes"])) for rec in inventory}
        # DNT rows for the changed files, AFTER the index commit, writing only
        # rows that change. Best-effort and reported on the store, never a
        # sync failure (the on-disk sentinel stays the edit-gate authority).
        dnt_report: dict[str, Any] = {
            "source": "index_changed_files",
            "scanned": dnt_scanned,
            "found": len(dnt_found),
            "written": 0,
            "manual_preserved": [],
            "errors": list(dnt_parse_errors),
        }
        # Set when ANY registry write of this sync failed (index-derived
        # upsert or reconcile): such a sync is never green and never verified.
        dnt_write_failed = False
        if dnt_found:
            try:
                from .dnt_registry_sync import upsert_dnt_headers

                # each row is attested at the file version this pass parsed
                up = upsert_dnt_headers(project_root, dnt_found, registry=dnt_registry, versions=dnt_versions)
                dnt_report["written"] = up["written"]
                dnt_report["manual_preserved"] = list(up.get("manual_preserved", []))
                dnt_report["errors"].extend(up["errors"])
                dnt_write_failed = bool(up.get("error_count") or up["errors"])
            except Exception as exc:  # noqa: BLE001
                dnt_report["errors"].append({"path": "", "error": f"{type(exc).__name__}: {exc}"})
                dnt_write_failed = True
        # STALE SYSTEM ROWS (r0b2 finding 1): header removed, file deleted or
        # now under a pruned dir. Operator rows are never touched. Per file:
        # a scoped sync reconciles exactly its paths; a full sync trusts a row
        # only on a matching per-file attestation (see _reconcile_dnt).
        try:
            dnt_report["reconciled"] = self._reconcile_dnt(
                project_root,
                scoped_paths=scoped_paths,
                inventory=inventory,
                reparsed=set(changed_paths) | set(dropped),
                found=[rel for rel, _h in dnt_found],
                absent=dnt_absent,
                writes_ok=not dnt_report["errors"],
                registry=dnt_registry,
            )
            # Registry write failures inside the reconcile are the sync's
            # errors too (r0b2 P0): never green over a row it failed to fix.
            dnt_report["errors"].extend(dnt_report["reconciled"].get("errors") or [])
            if dnt_report["reconciled"].get("error_count"):
                dnt_write_failed = True
            unreadable = dnt_report["reconciled"].get("kept_unreadable") or []
            if unreadable:
                # Explicitly NOT verified (never ok): these rows were kept
                # unchecked because their files could not be read.
                dnt_report["status"] = "unverified"
                dnt_report["errors"].append(
                    {
                        "path": ", ".join(unreadable),
                        "error": "unreadable, system DNT row kept UNVERIFIED (file could not be read): "
                        + ", ".join(unreadable),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            dnt_report["errors"].append({"path": "", "error": f"reconcile: {type(exc).__name__}: {exc}"})
            # Never let a failed reconcile look clean: stale rows may remain,
            # so reads say the index is known-stale until a clean pass.
            from .project_index_sitter import mark_index_known_stale

            mark_index_known_stale(project_root)
        # ANY failed registry write in this sync (index-derived upsert or
        # reconcile) leaves rows that may be wrong: known-stale, not green.
        if dnt_write_failed:
            from .project_index_sitter import mark_index_known_stale

            mark_index_known_stale(project_root)
            dnt_report.setdefault("status", "write_failed")
        dnt_report["ok"] = not dnt_report["errors"]
        dnt_report.setdefault("status", "ok" if dnt_report["ok"] else "error")
        dnt_report["error_count"] = len(dnt_report["errors"])
        dnt_report["errors"] = dnt_report["errors"][:20]
        if dnt_verdict_out is not None:
            # INVOCATION-BOUND verdict (r0b2 P1): a private copy for THIS call
            # and THIS project root. The verdict is handed back ONLY here - the
            # former shared store field _last_dnt_derived (overwritten by any
            # concurrent sync on the same store) is gone.
            import copy

            dnt_verdict_out["project_root"] = str(project_root)
            dnt_verdict_out["full_sync"] = scoped_paths is None
            dnt_verdict_out["report"] = copy.deepcopy(dnt_report)
            # THIS sync's walk exclusions (None when it did not walk).
            dnt_verdict_out["tree_exclusions"] = walk_pruned
            dnt_verdict_out["dropped"] = sorted(dropped)  # this delta's deletions / refusals
            dnt_verdict_out["integrity"] = bool(integrity)
            dnt_verdict_out["integrity_mismatches"] = sorted(integrity_mismatches)
        if dropped and read_hydrate:
            # A candidate vanished, became unreadable or is now refused without
            # the sitter having reconciled it: other changes may be pending
            # too. Reads now say so instead of serving the index as fresh.
            # (A DELTA is the sitter reconciling exactly those changes: its
            # deletions are the expected outcome, not a staleness signal.)
            from .project_index_sitter import mark_index_known_stale

            mark_index_known_stale(project_root)
        # ── #448 first production LSP joint (§XXVII guest oracle) ──
        # AFTER the sync has fully committed; dormant-by-default and
        # fail-OPEN — the return value below is byte-identical whether
        # the door exists, runs, raises, or is absent.
        self._lsp_joint_after_sync(project_root, changed_paths)
        return len(rows)

    def _lsp_joint_after_sync(self, project_root: Path, changed_paths: list[str]) -> None:
        """#448 slice 1 — the FIRST production caller of the aidocs_lsp door.

        Runs after a sync pass commits, scoped to ONLY the files reparsed in
        that pass (bounded — never whole-tree). Guarded twice (§XXVII "a
        guest is lube, not an organ"):

          * dormant-by-default: ``index.lsp_materialize`` (config cascade,
            default False) must be explicitly truthy or the door is never
            even imported;
          * fail-OPEN: ANY exception — config read, import, drain, timeout —
            is swallowed; the sync completes exactly as it does today.

        No judge/authority path reads the result (future #448 work); the
        drain itself materializes ``semantic_ref`` edges via
        :meth:`replace_semantic_ref_edges` and evicts the servers.
        """
        try:
            if not changed_paths:
                return
            from .config import get_setting

            raw = get_setting(
                "index.lsp_materialize", project_root=project_root, default=False
            )
            enabled = raw is True or str(raw).strip().lower() in ("1", "true", "yes", "on")
            if not enabled:
                return
            from .lsp import lsp_drain_and_evict

            lsp_drain_and_evict(
                project_root, list(changed_paths), materialize_refs=True
            )
        except Exception:
            # §XXVII: an absent/broken guest costs zero function.
            return

    def replace_semantic_ref_edges(
        self,
        project_root: Path,
        targets: list[str],
        edge_rows: list[tuple[str, str, str]],
    ) -> int:
        """Materialize LSP-derived ``semantic_ref`` edges (§XXVII guest oracle).

        Scoped delete-then-reinsert MIRRORING how import edges are stored
        (``sync_code_files`` deletes+reinserts by ``source_path``): here the
        replace is scoped by TARGET — every prior ``semantic_ref`` row whose
        ``target`` is one of ``targets`` is dropped, then ``edge_rows`` are
        inserted fresh. This keeps a re-drain idempotent (no duplicate rows)
        and only ever touches ``kind='semantic_ref'`` rows, never the
        ``import`` graph the tree-sitter derive owns.

        Returns the number of rows actually inserted. Fail-safe: an empty
        ``targets`` is a no-op (0); the caller (the LSP door) already wraps
        this so any store error degrades to zero writes.
        """
        if not targets:
            return 0
        self.store.init_db(project_root)
        inserted = 0
        with self.store.connect(project_root) as conn:
            for target in targets:
                conn.execute(
                    "DELETE FROM code_edges WHERE kind='semantic_ref' AND target = ?",
                    (target,),
                )
            for row in dict.fromkeys(edge_rows):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO code_edges (source_path, target, kind) VALUES (?, ?, ?)",
                    row,
                )
                inserted += cur.rowcount
        return inserted

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
            # NOT_INDEXED files are a COVERAGE fact, reported as such (and
            # stamped on every discovery result) — not leftover sync work.
            # Counting them as "unparsed" would hold the index known-stale
            # forever on a box without the outliner.
            not_indexed_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM code_files f JOIN code_files_not_indexed n "
                    "ON n.path = f.path WHERE f.parsed = 0"
                ).fetchone()[0]
            )
        unparsed = code_count - parsed_count - not_indexed_count
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
            "not_indexed_code_files": not_indexed_count,
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
            **self._coverage_fields(project_root),
        }

    def _coverage_fields(self, project_root: Path) -> dict[str, object]:
        """Explicit C# coverage for ai_index_status: counts, reason, examples.

        Same source and shape as the ``_index_coverage`` stamp on discovery
        results (index_staleness.code_coverage), so the two cannot disagree."""
        from .index_staleness import code_coverage

        block, hint = code_coverage(project_root)
        out: dict[str, object] = {
            "coverage": block,
            "not_indexed_code_files": int(block.get("files_not_indexed") or 0),
        }
        if hint:
            out["coverage_hint"] = hint
        return out

    def _code_freshness(self, project_root: Path) -> dict[str, object]:
        indexed_rows: dict[str, sqlite3.Row] = {}
        with self.store.connect(project_root) as conn:
            for row in conn.execute(
                "SELECT path, checksum, mtime_ns, parsed FROM code_files ORDER BY path",
            ):
                indexed_rows[str(row["path"])] = row
            # Files whose REQUIRED extractor (the trusted Roslyn outliner) was
            # unavailable: a COVERAGE gap with a named cause, not unfinished
            # sync work — ai_index_sync alone cannot fix it.
            try:
                not_indexed = {
                    str(r[0])
                    for r in conn.execute("SELECT path FROM code_files_not_indexed").fetchall()
                }
            except sqlite3.Error:
                not_indexed = set()

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
            path
            for path, row in indexed_rows.items()
            if int(row["parsed"] or 0) != 1 and path not in not_indexed
        )
        not_indexed_count = sum(
            1 for path, row in indexed_rows.items() if int(row["parsed"] or 0) != 1 and path in not_indexed
        )
        reasons: list[str] = []
        if missing_paths or extra_paths:
            reasons.append("path_drift")
        if drifted_paths:
            reasons.append("content_drift")
        if unparsed_paths or (tracked_paths and not indexed_rows):
            reasons.append("missing_index_state")
        if not_indexed_count:
            reasons.append("csharp_not_indexed")
        if tracked_paths and not indexed_rows:
            state = "missing"
        elif any(r != "csharp_not_indexed" for r in reasons):
            state = "stale"
        elif reasons:
            # Fresh for everything it COULD index, but coverage is incomplete:
            # never "ready" while C# files carry no symbols.
            state = "partial"
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
