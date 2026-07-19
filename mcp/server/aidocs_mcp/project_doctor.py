"""Registry-wide project health doctor (diagnose + safe heal).

Born from the 2026-07-06 audit that found half-initialized projects (index db
without schema/marker/registry — an aborted project_init), a registry row with
adoption but no commissioning, and dead known_projects rows pointing at deleted
temp dirs. `aidocs doctor` runs install-level checks; THIS module covers the
projects themselves, cross-checking DISK truth (marker / index db / commission
stamp) against the SQLite registries (known_projects + project_commission).

Statuses per root:
  healthy        — commission stamp present (post-migration signal).
  legacy         — governance-bearing legacy marker, no stamp yet; managed via
                   the heal-forward bridge; converges at next bootstrap.
  half_init      — AIDOCS debris (.MEMORY and/or index db) but NO commission
                   evidence (no stamp, no marker, no registry row). An aborted
                   init or an incidental store touch. NEVER auto-healed — the
                   operator decides adopt (project_init) vs clean.
  registry_drift — registries disagree with disk (adopted-uncommissioned row,
                   or commissioned row whose disk evidence is gone).
  stale_row      — registry row whose project_root no longer exists on disk.

Heal policy (deliberately conservative):
  * SAFE, automatic under heal(): stamp-forward legacy projects
    (heal_legacy_commission — only ever stamps roots that already proved
    commission) and prune stale known_projects rows.
  * NEVER automatic: initializing a half_init (that is adoption — a deliberate
    operator act, per commission LAW) or deleting anything on disk.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectHealth:
    root: str
    status: str
    exists: bool = True
    has_memory: bool = False
    has_legacy_marker: bool = False
    has_index_db: bool = False
    has_code_schema: bool = False
    has_stamp: bool = False
    in_known_projects: bool = False
    commission_state: str = ""
    notes: list[str] = field(default_factory=list)


def _index_db(root: Path) -> Path:
    return root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _inspect_disk(root: Path) -> dict[str, bool]:
    from .mcp_server_runtime_helpers import _has_commission_stamp, _has_legacy_marker

    db = _index_db(root)
    has_code_schema = False
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                has_code_schema = bool(
                    con.execute(
                        "SELECT name FROM sqlite_master WHERE name='code_files'",
                    ).fetchone(),
                )
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return {
        "exists": root.is_dir(),
        "has_memory": (root / ".MEMORY").is_dir(),
        "has_legacy_marker": _has_legacy_marker(root),
        "has_index_db": db.is_file(),
        "has_code_schema": has_code_schema,
        "has_stamp": _has_commission_stamp(root),
    }


def _registry_roots() -> dict[str, dict[str, object]]:
    """Union of known_projects + project_commission rows, keyed by a
    normalized root path. Values: {known: bool, commission_row: dict|None}.
    """
    out: dict[str, dict[str, object]] = {}

    def norm(p: str) -> str:
        return str(p).replace("\\", "/").rstrip("/").lower()

    try:
        from .known_projects_store import KnownProjectsStore

        for row in KnownProjectsStore().list_projects():
            raw = str(row.get("project_root") or "")
            if raw:
                out.setdefault(norm(raw), {"raw": raw, "known": False, "commission": None})
                out[norm(raw)]["known"] = True
    except Exception:
        pass
    try:
        from .config_store import _global_db_path

        con = sqlite3.connect(f"file:{_global_db_path()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            if con.execute(
                "SELECT name FROM sqlite_master WHERE name='project_commission'",
            ).fetchone():
                for row in con.execute("SELECT * FROM project_commission"):
                    raw = str(row["project_root"] or "")
                    if raw:
                        out.setdefault(
                            norm(raw), {"raw": raw, "known": False, "commission": None},
                        )
                        out[norm(raw)]["commission"] = dict(row)
        finally:
            con.close()
    except Exception:
        pass
    return out


def diagnose(extra_roots: list[str] | None = None) -> list[ProjectHealth]:
    """Cross-check every registry-known root (+ any extra roots) disk<->registry."""
    registry = _registry_roots()
    roots: dict[str, str] = {key: str(val["raw"]) for key, val in registry.items()}
    for extra in extra_roots or []:
        key = str(extra).replace("\\", "/").rstrip("/").lower()
        roots.setdefault(key, str(extra))

    results: list[ProjectHealth] = []
    for key, raw in sorted(roots.items()):
        root = Path(raw)
        reg = registry.get(key, {"known": False, "commission": None})
        commission_row = reg.get("commission")
        disk = _inspect_disk(root)

        health = ProjectHealth(
            root=str(root),
            status="healthy",
            exists=disk["exists"],
            has_memory=disk["has_memory"],
            has_legacy_marker=disk["has_legacy_marker"],
            has_index_db=disk["has_index_db"],
            has_code_schema=disk["has_code_schema"],
            has_stamp=disk["has_stamp"],
            in_known_projects=bool(reg.get("known")),
        )
        if isinstance(commission_row, dict):
            if commission_row.get("commissioned_at"):
                health.commission_state = "commissioned"
            elif commission_row.get("adopted_at"):
                health.commission_state = "adopted_uncommissioned"

        if not disk["exists"]:
            health.status = "stale_row"
            health.notes.append("registry row points at a missing directory")
        elif disk["has_stamp"]:
            health.status = "healthy"
            if health.commission_state == "adopted_uncommissioned":
                health.status = "registry_drift"
                health.notes.append("disk stamped but registry says adopted-uncommissioned")
        elif disk["has_legacy_marker"]:
            health.status = "legacy"
            health.notes.append("pre-migration project; heal-forward stamps it at next bootstrap")
        elif disk["has_memory"] or disk["has_index_db"]:
            health.status = "half_init"
            health.notes.append(
                "AIDOCS debris without commission evidence — adopt (project_init) or clean",
            )
        elif health.commission_state or health.in_known_projects:
            health.status = "registry_drift"
            health.notes.append("registry says AIDOCS but disk carries no evidence")
        results.append(health)
    return results


def heal(results: list[ProjectHealth] | None = None) -> dict[str, object]:
    """Apply ONLY the safe heals: stamp-forward legacy roots + prune stale
    known_projects rows. Returns visible accounting. Half-inits are REPORTED,
    never auto-adopted (commission LAW: first adoption is deliberate).
    """
    from .mcp_server_runtime_helpers import heal_legacy_commission

    results = results if results is not None else diagnose()
    stamped: list[str] = []
    backfilled: list[str] = []
    failures: list[str] = []
    for h in results:
        if h.status == "legacy":
            try:
                if heal_legacy_commission(Path(h.root)):
                    stamped.append(h.root)
            except Exception as exc:
                failures.append(f"{h.root}: {exc}")
        elif h.status == "registry_drift" and h.has_stamp:
            # SAFE: disk already PROVES commission (the stamp); the registry row
            # just never closed (adopted_at set, commissioned_at NULL). Backfill
            # via the canonical writer so classify()/resolve stay consistent.
            try:
                from . import project_commission as _pc

                _pc.commission(Path(h.root))
                backfilled.append(h.root)
            except Exception as exc:
                failures.append(f"{h.root}: {exc}")
    pruned = 0
    try:
        from .known_projects_store import KnownProjectsStore

        pruned = KnownProjectsStore().prune_stale()
    except Exception as exc:
        failures.append(f"prune_stale: {exc}")
    return {
        "stamped_forward": stamped,
        "registry_backfilled": backfilled,
        "pruned_stale_rows": pruned,
        "needs_operator": [h.root for h in results if h.status == "half_init"],
        "failures": failures,
    }
