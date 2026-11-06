"""Sync the DNT registry from on-disk sentinel-marked files.

Walks the project tree, parses any structured DNT header at file head,
upserts via ProtectedFileRegistryStore.record_dnt_header. Same semantics
as ai_protect(mode='sync') — extracted so ai_index_sync can call it
automatically. The on-disk sentinel and SQL registry must not diverge;
manual-only sync was the gap that left dental's ~30 sentinel-marked
files unregistered (witnessed 2026-05-07 by the Phoenix-scribe seat).

Per backlog #79 (DNT auto-surface): the read tools' DNT banner only
fires when find_family_by_path returns a row with non-empty dnt_id.
Auto-population on every ai_index_sync closes that gap structurally
so the on-disk truth and the SQL truth converge automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .dnt_header_parser import DntHeader, parse_dnt_header
from .protected_file_registry_store import SYSTEM_DNT_OWNER, ProtectedFileRegistryStore

# Bounded extensions — only languages where a DNT header is plausible.
# Razor (.cshtml) and C# (.cs) are the canonical operator targets;
# .py/.ts/.js round it out.
_DNT_SCAN_EXTS = frozenset(
    {
        ".cs",
        ".cshtml",
        ".razor",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".html",
        ".scss",
        ".css",
        ".sql",
        ".md",
        ".yml",
        ".yaml",
        ".toml",
    },
)

# Skip well-known noise dirs to keep the walk cheap.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".MEMORY",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "bin",
        "obj",
        "dist",
        "build",
        ".next",
        ".cache",
        "target",
    },
)

# Probe just the top of each file (parser scans first 200 lines
# internally, but cap the read at a few KB to avoid pulling huge
# files entirely into memory).
_PROBE_BYTES = 16384


def sync_dnt_registry(
    project_root: Path,
    *,
    registry: ProtectedFileRegistryStore | None = None,
) -> dict[str, Any]:
    """Walk the project tree, find DNT-headered files, upsert all rows.

    Returns counts dict: {ok, synced, skipped, errors, error_count}.
    Idempotent — re-running over an already-populated tree just
    re-upserts the same rows (INSERT OR REPLACE in record_dnt_header).
    Conservative on per-file errors: collects + caps at 20 surfaced,
    keeps walking the rest of the tree.
    """
    if registry is None:
        registry = ProtectedFileRegistryStore()

    from .index_tree_policy import PruneReport
    from .index_tree_policy import walk as _policy_walk

    synced = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    manual_preserved: list[str] = []
    _walk_present: list[str] = []
    report = PruneReport()

    # The common tree policy (.git / .tmp / nested worktrees / dot-dirs /
    # site-packages) prunes at descent; _SKIP_DIRS composes on top.
    for root_dir, dirs, files in _policy_walk(project_root, report=report):
        # Prune skip dirs in-place so os.walk doesn't recurse.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            fpath = Path(root_dir) / fname
            if fpath.suffix.lower() not in _DNT_SCAN_EXTS:
                continue
            try:
                with fpath.open("rb") as fh:
                    head = fh.read(_PROBE_BYTES)
                try:
                    text = head.decode("utf-8")
                except UnicodeDecodeError:
                    text = head.decode("utf-8", errors="replace")
                header = parse_dnt_header(text)
            except OSError as exc:
                errors.append(
                    {
                        "path": str(fpath.relative_to(project_root)),
                        "error": f"read failed: {exc}",
                    },
                )
                continue
            except Exception as exc:
                errors.append(
                    {
                        "path": str(fpath.relative_to(project_root)),
                        "error": f"parse failed: {exc}",
                    },
                )
                continue
            if not header.is_present:
                skipped += 1
                continue
            rel_path = str(fpath.relative_to(project_root)).replace("\\", "/")
            _walk_present.append(rel_path)
            dnt_role, master_path = dnt_role_for(rel_path, header)
            try:
                # only_if_system: an operator row on this path is never
                # replaced by the header (r0b2 ownership rule).
                written = registry.record_dnt_header(
                    project_root,
                    path=rel_path,
                    dnt_id=header.dnt_id,
                    dnt_role=dnt_role,
                    master=master_path,
                    pair_files=list(header.pair),
                    forbid_list=list(header.forbid),
                    allow_list=list(header.allow),
                    incidents=list(header.incident),
                    baseline=header.baseline,
                    cost=header.cost,
                    full_header_text=header.raw_header_text,
                    why="",
                    only_if_system=True,
                )
                if written is False:
                    manual_preserved.append(rel_path)
                else:
                    synced += 1
            except Exception as exc:
                errors.append(
                    {
                        "path": rel_path,
                        "error": f"upsert failed: {exc}",
                    },
                )

    # Stale SYSTEM rows (header removed, file deleted, now under a pruned
    # dir) are reconciled away; operator rows are never touched.
    reconciled: dict[str, Any] = {}
    try:
        reconciled = reconcile_system_dnt_rows(project_root, known_present=_walk_present, registry=registry)
        errors.extend(reconciled.get("errors") or [])
    except Exception as exc:  # noqa: BLE001 - reported, never fails the walk
        errors.append({"path": "", "error": f"reconcile failed: {type(exc).__name__}: {exc}"})
    return {
        "ok": len(errors) == 0,
        "synced": synced,
        "reconciled": reconciled,
        "skipped": skipped,
        "errors": errors[:20],
        "error_count": len(errors),
        "manual_preserved": manual_preserved,
        "pruned": report.as_dict(),
    }


# ── index-derived DNT ────────────────────────────────────────────────────
#
# ai_index_sync no longer walks the tree for DNT. The code index already
# reads the full text of every CHANGED file it reparses; the header is parsed
# from that text (header_from_index_text) and upserted only when the row would
# change (upsert_dnt_headers). A file the index does not hold (a .md, a
# tests/ or migrations/ file, ...) is read on demand - ONE specific file -
# when a read banner or an edit gate asks about it (ensure_dnt_registered).
# ai_protect(mode='sync') keeps the manual full walk above. The on-disk
# sentinel stays authoritative for the edit gates (file_ops reads it).

DNT_SCAN_EXTS = _DNT_SCAN_EXTS

_ON_DEMAND_CACHE: dict[tuple[str, int, int], DntHeader | None] = {}
_ON_DEMAND_CACHE_MAX = 20_000


def dnt_role_for(rel_path: str, header: DntHeader) -> tuple[str, str]:
    """(role, master) exactly as the walk decides it: master when there is no
    master line or it points at this file, satellite otherwise."""
    master_path = (header.master or "").strip().replace("\\", "/")
    if not master_path or master_path == rel_path:
        return "master", master_path
    return "satellite", master_path


def header_from_index_text(text: str) -> DntHeader:
    """Parse the header from text the indexer already holds, through the SAME
    16 KB probe window the walk reads (byte-identical for UTF-8 files)."""
    head = text.encode("utf-8")[:_PROBE_BYTES]
    return parse_dnt_header(head.decode("utf-8", errors="replace"))


def _row_matches(existing: Any, *, dnt_id: str, role: str, header: DntHeader) -> bool:
    if existing is None:
        return False
    return (
        existing.dnt_id == dnt_id
        and existing.dnt_role == role
        and list(existing.pair_files) == list(header.pair)
        and list(existing.forbid_list) == list(header.forbid)
        and list(existing.allow_list) == list(header.allow)
        and list(existing.incidents) == list(header.incident)
        and existing.baseline == header.baseline
        and existing.cost == header.cost
        and existing.full_header_text == header.raw_header_text
        and existing.protected_by_user_id == SYSTEM_DNT_OWNER
        and (existing.why or "") == ""
    )


def header_digest(rel: str, header: DntHeader) -> str:
    """dnt_row_digest of the system row ``header`` implies for ``rel``."""
    from .protected_file_registry_store import dnt_row_digest

    role, _master = dnt_role_for(rel, header)
    return dnt_row_digest(
        dnt_id=header.dnt_id,
        dnt_role=role,
        pair_files=list(header.pair),
        forbid_list=list(header.forbid),
        allow_list=list(header.allow),
        incidents=list(header.incident),
        baseline=header.baseline,
        cost=header.cost,
        full_header_text=header.raw_header_text,
    )


def _file_version(project_root: Path, rel: str) -> tuple[int, int] | None:
    """(mtime_ns, size_bytes) of an in-root regular file - a stat, no open -
    or None when it is missing, not a file, or outside the root."""
    import os

    from .index_tree_policy import _normalize_rel

    norm = _normalize_rel(rel)
    if norm is None:
        return None
    try:
        real_root = os.path.realpath(project_root)
        real = os.path.realpath(os.path.join(real_root, norm))
        if os.path.commonpath([os.path.normcase(real_root), os.path.normcase(real)]) != os.path.normcase(real_root):
            return None
        st = os.stat(real)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(real):
        return None
    return int(st.st_mtime_ns), int(st.st_size)


def upsert_dnt_headers(
    project_root: Path,
    items: list[tuple[str, DntHeader]],
    *,
    registry: ProtectedFileRegistryStore | None = None,
    existing_rows: dict[str, Any] | None = None,
    versions: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Upsert (path, header) pairs with the walk's exact row shape, writing
    ONLY rows that would change (the walk rewrote every row every sync).

    OWNERSHIP: a path whose row belongs to an operator (ai_protect add) is
    left untouched and listed in ``manual_preserved``.
    ATTESTATION: for every item whose file version is in ``versions`` the
    (version, header digest) attestation is written in the SAME transaction
    as the changed rows (or alone, when no row changes)."""
    if registry is None:
        registry = ProtectedFileRegistryStore()
    written = 0
    errors: list[dict[str, str]] = []
    manual_preserved: list[str] = []
    if existing_rows is None and items:
        # ONE query for every existing row (any owner, so the operator-row
        # check below still sees them) - was one connection per path.
        existing_rows = registry.get_full_many(project_root, [rel for rel, _h in items])
    pending: list[dict[str, Any]] = []
    attest: list[tuple[str, int, int, str]] = []
    for rel, header in items:
        role, _master = dnt_role_for(rel, header)
        version = (versions or {}).get(rel)
        if version is not None:
            attest.append((rel, version[0], version[1], header_digest(rel, header)))
        existing = existing_rows.get(rel) if existing_rows is not None else None
        if existing is not None and existing.protected_by_user_id != SYSTEM_DNT_OWNER:
            manual_preserved.append(rel)
            continue
        if _row_matches(existing, dnt_id=header.dnt_id, role=role, header=header):
            continue
        pending.append(
            {
                "path": rel,
                "dnt_id": header.dnt_id,
                "dnt_role": role,
                "pair_files": list(header.pair),
                "forbid_list": list(header.forbid),
                "allow_list": list(header.allow),
                "incidents": list(header.incident),
                "baseline": header.baseline,
                "cost": header.cost,
                "full_header_text": header.raw_header_text,
                "why": "",
            }
        )
    if pending:
        # ONE transaction for the whole pass (was 2 connections per row); the
        # owner guard stays in each upsert's WHERE clause; the attestations
        # commit with the rows they vouch for.
        try:
            results = registry.record_dnt_headers(project_root, pending, attestations=attest)
        except Exception as exc:  # noqa: BLE001 - reported, never fails the sync
            reason = f"upsert failed: {type(exc).__name__}: {exc}"
            errors.extend({"path": row["path"], "error": reason} for row in pending)
        else:
            for row, ok in zip(pending, results, strict=True):
                if ok is False:  # an operator row appeared since the read
                    manual_preserved.append(row["path"])
                else:
                    written += 1
    elif attest:
        try:
            registry.attest_files(project_root, attest)
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": "", "error": f"upsert failed: attestation {type(exc).__name__}: {exc}"})
    return {
        "found": len(items),
        "written": written,
        "manual_preserved": manual_preserved,
        "errors": errors[:20],
        "error_count": len(errors),
    }


def _probe_header(project_root: Path, rel: str) -> tuple[str, DntHeader | None]:
    """Bounded head read of ONE in-root file, with the read-security floor:
    no path outside the root (also not through a link), only DNT extensions,
    never a name the index refuses as sensitive.

    Returns (status, header): status is ``present`` (header returned),
    ``absent`` (readable, no header), ``missing``, ``outside_root``,
    ``refused`` (extension / sensitive name) or ``read_error``."""
    import os

    from .code_index_store import _is_sensitive_for_index
    from .index_tree_policy import _normalize_rel

    norm = _normalize_rel(rel)
    if norm is None:
        return "outside_root", None
    if Path(norm).suffix.lower() not in _DNT_SCAN_EXTS or _is_sensitive_for_index(norm):
        return "refused", None
    try:
        real_root = os.path.realpath(project_root)
        real = os.path.realpath(os.path.join(real_root, norm))
        if os.path.commonpath([os.path.normcase(real_root), os.path.normcase(real)]) != os.path.normcase(
            real_root
        ):
            return "outside_root", None
    except (OSError, ValueError):
        return "outside_root", None
    try:
        st = os.stat(real)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError):
        return "read_error", None
    if not os.path.isfile(real):
        return "missing", None
    key = (os.path.normcase(real), int(st.st_mtime_ns), int(st.st_size))
    if key in _ON_DEMAND_CACHE:
        cached = _ON_DEMAND_CACHE[key]
        return ("present" if cached is not None else "absent"), cached
    try:
        with open(real, "rb") as fh:
            head = fh.read(_PROBE_BYTES)
        header = parse_dnt_header(head.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return "read_error", None
    found = header if header.is_present else None
    if len(_ON_DEMAND_CACHE) >= _ON_DEMAND_CACHE_MAX:
        _ON_DEMAND_CACHE.clear()
    _ON_DEMAND_CACHE[key] = found
    return ("present" if found is not None else "absent"), found


def _read_header_on_demand(project_root: Path, rel: str) -> DntHeader | None:
    return _probe_header(project_root, rel)[1]


def reconcile_system_dnt_rows(
    project_root: Path,
    *,
    paths: Any = None,
    known_present: Any = (),
    known_absent: Any = (),
    trusted: Any = (),
    also_check: Any = (),
    versions: dict[str, tuple[int, int]] | None = None,
    registry: ProtectedFileRegistryStore | None = None,
) -> dict[str, Any]:
    """Reconcile SYSTEM-owned DNT rows with their files, PER FILE
    (r0b2 finding 1 + d6b3dac7-69a). ``paths=None`` checks every system row,
    else only those.

    A row is stale when its path is now excluded from the main tree
    (index_tree_policy: `.tmp/`, nested worktree / submodule / clone,
    dot-dir, link out of the root ...), the file is missing, or the file no
    longer carries a header. A present header refreshes the row (write only
    on change). ``known_present`` / ``known_absent`` are paths whose header
    state the caller just parsed (the index pass), so they are not re-read.
    ``trusted`` are paths whose attestation matches the file's current
    version AND the row's digest (see attested_trust); they cost nothing.
    ``also_check`` are paths WITHOUT a row whose attestation says they carry
    a header (an externally deleted row): probed and restored.
    ``versions`` maps rel -> (mtime_ns, size_bytes) the caller observed.

    Every outcome is ATTESTED in the same transaction as its row change: a
    removed row with "no header at version V" (or the attestation dropped
    when the file is gone / excluded), a refreshed row with its digest.

    OWNERSHIP: only rows owned by SYSTEM_DNT_OWNER are listed and the delete
    re-tests the owner (remove_system_rows); operator rows are never touched.
    FAIL CLOSED: a file that exists but cannot be read keeps its row
    (``kept_unreadable``) - a transient error never drops a protection."""
    from .index_tree_policy import _normalize_rel, excluded_rel_reason

    if registry is None:
        registry = ProtectedFileRegistryStore()
    vers = dict(versions or {})
    if paths is None:
        owned = registry.list_system_dnt_paths(project_root)
    else:
        owned = registry.list_system_dnt_paths(project_root, paths={_normalize_rel(p) or p for p in paths})
    present = set(known_present)
    absent = set(known_absent)
    trusted_set = set(trusted)
    owned_set = set(owned)
    stale: list[tuple[str, str]] = []
    unreadable: list[str] = []
    refresh: list[tuple[str, DntHeader]] = []
    attest_absent: list[tuple[str, int, int, str]] = []
    drop_attest: list[str] = []
    checked = 0
    skipped_trusted = 0

    def _version(rel: str) -> tuple[int, int] | None:
        if rel not in vers:
            v = _file_version(project_root, rel)
            if v is not None:
                vers[rel] = v
        return vers.get(rel)

    def _judge(rel: str) -> str | None:
        """None = keep/refresh decided; else the stale reason."""
        reason = excluded_rel_reason(project_root, rel)
        if reason is None and rel in absent:
            reason = "header_removed"
        if reason is None:
            status, header = _probe_header(project_root, rel)
            if status == "present" and header is not None:
                refresh.append((rel, header))
                return None
            if status == "read_error":
                unreadable.append(rel)
                return None
            reason = "header_removed" if status == "absent" else status
        return reason

    for rel in owned:
        if rel in present:
            continue
        if rel in trusted_set and rel not in absent:
            skipped_trusted += 1
            continue
        checked += 1
        reason = _judge(rel)
        if reason is not None:
            stale.append((rel, reason))
    restored_candidates = [p for p in also_check if p not in owned_set and p not in present]
    for rel in restored_candidates:
        checked += 1
        reason = _judge(rel)
        if reason == "header_removed":
            v = _version(rel)
            if v is not None:
                attest_absent.append((rel, v[0], v[1], ""))
        elif reason is not None:
            drop_attest.append(rel)
    for rel, reason in stale:
        v = _version(rel) if reason == "header_removed" else None
        if v is not None:
            attest_absent.append((rel, v[0], v[1], ""))
        else:
            drop_attest.append(rel)
    # scoped paths with no system row that are gone or no longer admitted:
    # their attestation goes too (a deleted plain file must not stay "known"
    # to the stat pass forever)
    if paths is not None:
        for p in paths:
            rel = _normalize_rel(p) or p
            if rel in owned_set or rel in present or rel in absent:
                continue
            if excluded_rel_reason(project_root, rel) is not None or _version(rel) is None:
                drop_attest.append(rel)
    # reparsed files the index found WITHOUT a header and with no row: attest
    for rel in absent - owned_set:
        v = vers.get(rel)
        if v is not None:
            attest_absent.append((rel, v[0], v[1], ""))
    write_errors: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    if stale or attest_absent or drop_attest:
        # ONE transaction: every DELETE re-tests the owner, and the
        # attestations explaining the removals commit (or roll back) with them.
        try:
            gone = set(
                registry.remove_system_rows(
                    project_root,
                    [rel for rel, _r in stale],
                    attestations=attest_absent,
                    drop_attestations=drop_attest,
                )
            )
            removed = [{"path": rel, "reason": why} for rel, why in stale if rel in gone]
        except Exception as exc:  # noqa: BLE001 - a failed removal is a failed write: reported
            write_errors.append({"path": "", "error": f"upsert failed: removal {type(exc).__name__}: {exc}"})
    refreshed = 0
    if refresh:
        rows = registry.get_full_many(project_root, [rel for rel, _h in refresh])
        refresh_versions = {rel: v for rel, _h in refresh if (v := _version(rel)) is not None}
        up = upsert_dnt_headers(
            project_root, refresh, registry=registry, existing_rows=rows, versions=refresh_versions
        )
        refreshed = up["written"]
        # A refresh that failed leaves a WRONG system row: the caller must
        # see it (never verify, never report green) - r0b2 P0.
        write_errors.extend(up.get("errors") or [])
    return {
        "errors": write_errors[:20],
        "error_count": len(write_errors),
        "owned": len(owned),
        "checked": checked,
        "trusted_unchanged": skipped_trusted,
        "removed": removed[:20],
        "removed_count": len(removed),
        "refreshed": refreshed,
        "kept_unreadable": unreadable[:20],
    }


def attested_trust(
    project_root: Path,
    *,
    registry: ProtectedFileRegistryStore,
    inventory_versions: dict[str, tuple[int, int]],
    reparsed: set[str],
) -> tuple[set[str], set[str]]:
    """Per-file attestation trust for a FULL sync (replaces the global
    registry fingerprint): (trusted, also_check).

    A system row is trusted when its file was not reparsed, its current
    version (from the walk's inventory, else one stat - never an open)
    equals its attestation's, AND the row's content digest equals the
    attested digest: an external rewrite of ONE row breaks only that row's
    trust. ``also_check`` are files attested WITH a header that have no row
    of any owner (an externally deleted row) - probed and restored."""
    from .protected_file_registry_store import record_digest

    owned = registry.list_system_dnt_paths(project_root)
    rows = registry.get_full_many(project_root, owned) if owned else {}
    att = registry.get_attestations(project_root)
    trusted: set[str] = set()
    for rel in owned:
        if rel in reparsed:
            continue
        a = att.get(rel)
        if a is None:
            continue
        version = inventory_versions.get(rel)
        if version is None:
            # not walked this pass: trusted only while still admitted to the
            # main tree (a file now under a nested checkout / .tmp is not)
            from .index_tree_policy import excluded_rel_reason

            if excluded_rel_reason(project_root, rel) is not None:
                continue
            version = _file_version(project_root, rel)
        if version is not None and (a[0], a[1]) == version and a[2] == record_digest(rows.get(rel)):
            trusted.add(rel)
    rowless = [rel for rel, a in att.items() if a[2] and rel not in rows and rel not in reparsed]
    if rowless:
        any_owner = registry.get_full_many(project_root, rowless)
        also = {rel for rel in rowless if rel not in any_owner}
    else:
        also = set()
    # header-less attestations of files this walk did not see: re-check the
    # ones that moved or vanished (a deleted file's attestation is dropped),
    # leave the unchanged ones alone (one stat, no read)
    for rel, a in att.items():
        if a[2] or rel in rows or rel in inventory_versions or rel in reparsed:
            continue
        if _file_version(project_root, rel) != (a[0], a[1]):
            also.add(rel)
    return trusted, also


def dnt_family_for_file(
    project_root: Path,
    rel: str,
    *,
    registry: ProtectedFileRegistryStore | None = None,
    allow_removal: bool = True,
) -> tuple[str, str, DntHeader | None] | None:
    """The DNT family of ONE file as read / edited NOW: (dnt_id, role,
    on-disk header or None), or None.

    Checks the file-local header against the registry on the file's
    OBSERVED version (r0b2 d6b3dac7-69a): when the file's (mtime, size) and
    the row's digest match its attestation the registry answers (one stat,
    no open); otherwise the head is probed (cached by version) and the row
    plus attestation are updated in one transaction - a header added or
    changed since the last sync is honoured immediately, and (when
    ``allow_removal``) a removed header drops the system row. The EDIT GATE
    passes allow_removal=False: on the gate path a protection is only ever
    added or updated, never dropped (the sync's reconcile does that). FAIL
    CLOSED: a read error (or a missing file) answers from the registry. A
    path outside the main tree is answered but never persisted. Operator
    rows are never changed."""
    from .index_tree_policy import _normalize_rel, excluded_rel_reason
    from .protected_file_registry_store import record_digest

    if registry is None:
        registry = ProtectedFileRegistryStore()
    norm = _normalize_rel(rel) or rel
    fam = registry.find_family_by_path(project_root, norm)  # the registry's answer (hot path)
    row_fam = (fam[0], fam[1], None) if fam else None
    version = _file_version(project_root, norm)
    if version is None:
        return row_fam  # missing / outside the root: the registry's answer stands
    row = registry.get_full(project_root, norm)
    operator = row is not None and row.protected_by_user_id != SYSTEM_DNT_OWNER
    if not operator:
        a = registry.get_attestations(project_root, [norm]).get(norm)
        if a is not None and (a[0], a[1]) == version and a[2] == record_digest(row):
            return row_fam  # attested at this exact version: no read
    status, header = _probe_header(project_root, norm)
    if status == "read_error" or status not in ("present", "absent"):
        return row_fam  # fail closed
    persist = excluded_rel_reason(project_root, norm) is None
    if status == "present" and header is not None:
        role, _master = dnt_role_for(norm, header)
        if persist:
            try:
                upsert_dnt_headers(project_root, [(norm, header)], registry=registry, versions={norm: version})
            except Exception:  # noqa: BLE001 - the answer stands even if persisting fails
                pass
        if operator and row_fam is not None:
            return row_fam
        return header.dnt_id, role, header
    # absent: the file carries no header at this version
    if not allow_removal or operator:
        return row_fam  # never drop a protection on this path
    if persist:
        try:
            registry.remove_system_rows(project_root, [norm], attestations=[(norm, version[0], version[1], "")])
        except Exception:  # noqa: BLE001 - a failed removal keeps the protection
            return row_fam
        return None
    return None


def read_header_for_edit(project_root: Path, rel: str) -> DntHeader | None:
    """The EDIT AUTHORITY's header read: a bounded (first _PROBE_BYTES),
    UNCACHED read of the file's current bytes - never a stat-keyed cache hit,
    because a same-size rewrite inside one mtime tick is invisible to stat
    (r0b2 fb72b895-69e). Returns the header, or None for no header / a missing
    file / a path outside the root or of a non-DNT kind. RAISES OSError when
    the file exists but cannot be read (the gate then fails closed)."""
    import os

    from .code_index_store import _is_sensitive_for_index
    from .index_tree_policy import _normalize_rel

    norm = _normalize_rel(rel)
    if norm is None or Path(norm).suffix.lower() not in _DNT_SCAN_EXTS or _is_sensitive_for_index(norm):
        return None
    real_root = os.path.realpath(project_root)
    real = os.path.realpath(os.path.join(real_root, norm))
    try:
        inside = os.path.commonpath([os.path.normcase(real_root), os.path.normcase(real)]) == os.path.normcase(
            real_root
        )
    except ValueError:  # another drive: outside the root, not an I/O failure
        inside = False
    if not inside:
        return None
    # Explicit stat, NOT os.path.isfile(): isfile() turns a stat error (e.g.
    # PermissionError) into False, which would read as "absent, no DNT" and
    # let the gate allow the edit. Only a genuinely missing path is absent;
    # every other OSError propagates and the cite gate fails closed.
    import stat as _stat

    try:
        st = os.stat(real)
    except FileNotFoundError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return None  # a directory / device / pipe carries no header to enforce
    with open(real, "rb") as fh:
        head = fh.read(_PROBE_BYTES)
    header = parse_dnt_header(head.decode("utf-8", errors="replace"))
    return header if header.is_present else None


def dnt_family_for_edit(
    project_root: Path,
    rel: str,
    *,
    registry: ProtectedFileRegistryStore | None = None,
) -> tuple[str, str, DntHeader | None] | None:
    """The DNT family that GOVERNS AN EDIT of ``rel``: decided on the file's
    current bytes (read_header_for_edit - one bounded uncached read per
    check), never on stat + row hash. A header found on disk is enforced at
    once (and persisted with its attestation); with no header on disk the
    registry's protection still stands (the edit path never drops one).
    Registry and read errors PROPAGATE - the caller refuses the edit."""
    from .index_tree_policy import _normalize_rel, excluded_rel_reason

    if registry is None:
        registry = ProtectedFileRegistryStore()
    norm = _normalize_rel(rel) or rel
    fam = registry.find_family_by_path(project_root, norm)
    header = read_header_for_edit(project_root, norm)
    if header is None:
        return (fam[0], fam[1], None) if fam else None
    role, _master = dnt_role_for(norm, header)
    row = registry.get_full(project_root, norm)
    if row is not None and row.protected_by_user_id != SYSTEM_DNT_OWNER and fam:
        return fam[0], fam[1], None  # an operator row governs
    from .protected_file_registry_store import record_digest

    version = _file_version(project_root, norm)
    digest = header_digest(norm, header)
    current = (
        version is not None
        and record_digest(row) == digest
        and registry.get_attestations(project_root, [norm]).get(norm) == (version[0], version[1], digest)
    )
    if not current and excluded_rel_reason(project_root, norm) is None:
        # persist only when the row / attestation actually lag the file
        try:
            upsert_dnt_headers(
                project_root, [(norm, header)], registry=registry, versions={norm: version} if version else None
            )
        except Exception:  # noqa: BLE001 - persisting is best-effort; the decision below is not
            pass
    return header.dnt_id, role, header


def ensure_dnt_registered(
    project_root: Path,
    rel: str,
    *,
    registry: ProtectedFileRegistryStore | None = None,
) -> tuple[str, str, DntHeader] | None:
    """On-demand DNT for one file the registry has no row for.

    Returns (dnt_id, role, header) when the file carries a DNT header on disk,
    else None. The row is persisted for a MAIN-tree path; a path outside the
    main tree (`.tmp/`, a nested worktree) is answered but never written into
    the main registry."""
    header = _read_header_on_demand(project_root, rel)
    if header is None:
        return None
    from .index_tree_policy import _normalize_rel, excluded_rel_reason

    norm = _normalize_rel(rel) or rel
    role, _master = dnt_role_for(norm, header)
    if excluded_rel_reason(project_root, norm) is None:
        try:
            upsert_dnt_headers(project_root, [(norm, header)], registry=registry)
        except Exception:  # noqa: BLE001 - the answer stands even if persisting fails
            pass
    return header.dnt_id, role, header
