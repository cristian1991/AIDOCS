"""Project adoption / commissioning state machine.

Governance must NOT hinge on the `.MEMORY/.aidocs/index.aidocs` marker
file being present — that file is created by bootstrap, so "no marker"
is ambiguous: it can mean an unrelated project, OR an AIDOCS project
whose bootstrap simply never finished. Keying off the marker alone turns
that ambiguity into a dead end (the foreign-`.MEMORY` refusal).

This module separates the lifecycle into explicit states and moves the
source of truth to SQLite (install-wide `project_commission` table),
with the marker demoted to a derived/fallback signal:

  UNSEEN                 — no adoption evidence, no `.MEMORY/`.
  FOREIGN_MEMORY         — `.MEMORY/` exists but the project was never
                           adopted (another tool likely owns it). Guarded:
                           AIDOCS keeps hands off, never auto-bootstraps.
  ADOPTED_UNCOMMISSIONED — prior-adoption evidence exists (the project
                           declares the aidocs MCP in `.mcp.json`, and/or
                           it is in the operator registry) but the AIDOCS
                           infrastructure was never created. Eligible for
                           UPS auto-repair (minimal, non-destructive).
  COMMISSIONED           — infrastructure present (registry says so, or the
                           marker exists). Ready; gates do not enforce
                           until a session arms managed mode.
  ARMED                  — COMMISSIONED + managed mode active for a session.

LAW:
  * First adoption is DELIBERATE (operator runs /aidocs or the dashboard
    → adopt()). A global hook never performs first adoption — it would
    creep governance onto every repo. adopt() records intent in SQLite.
  * Repair (commission of an already-adopted project) MAY happen
    automatically, but only creates AIDOCS-OWNED infrastructure and never
    rewrites CLAUDE.md / AGENTS.md (or any pre-existing file).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

STATE_UNSEEN = "unseen"
STATE_FOREIGN_MEMORY = "foreign_memory"
STATE_ADOPTED_UNCOMMISSIONED = "adopted_uncommissioned"
STATE_COMMISSIONED = "commissioned"
STATE_ARMED = "armed"

_MARKER_REL = Path(".MEMORY") / ".aidocs" / "index.aidocs"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── SQLite authority (install-wide, honors AIDOCS_GLOBAL_CONFIG_DB) ──


def _db_path() -> Path:
    from .config_store import _global_db_path

    return _global_db_path()


def _conn() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS project_commission ("
        "  project_root TEXT PRIMARY KEY,"
        "  adopted_at TEXT,"
        "  commissioned_at TEXT,"
        "  source TEXT,"
        "  title TEXT"
        ")",
    )
    return conn


def _norm(project_root: Path) -> str:
    return str(project_root).replace("\\", "/").rstrip("/").lower()


def _row(project_root: Path) -> sqlite3.Row | None:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT * FROM project_commission WHERE project_root = ?",
            (_norm(project_root),),
        ).fetchone()
    finally:
        conn.close()


# ── signals ─────────────────────────────────────────────────────────


def declares_aidocs_mcp(project_root: Path) -> bool:
    """True if the project's own `.mcp.json` declares the aidocs MCP
    server. This is the operator's deliberate, project-local declaration
    that AIDOCS governs here (Claude Code additionally prompts to approve
    a project MCP server before loading it).
    """
    mcp = project_root / ".mcp.json"
    if not mcp.is_file():
        return False
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
    except Exception:
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and "aidocs" in servers


def in_registry(project_root: Path) -> bool:
    """True if the project has an adoption record in SQLite."""
    row = _row(project_root)
    return bool(row and row["adopted_at"])


def has_marker(project_root: Path) -> bool:
    # "Marker" is now the commission stamp inside the sqlite index (single
    # is_aidocs_managed chokepoint) — the .aidocs file is deprecated.
    from .mcp_server_runtime_helpers import is_aidocs_managed

    return is_aidocs_managed(project_root)


def has_memory(project_root: Path) -> bool:
    return (project_root / ".MEMORY").is_dir()


def adoption_evidence(project_root: Path) -> bool:
    """Prior-adoption evidence: the project declares the aidocs MCP AND/OR
    it carries an operator-recorded adoption in the registry.
    """
    return declares_aidocs_mcp(project_root) or in_registry(project_root)


def is_commissioned(project_root: Path) -> bool:
    """Infrastructure present. SQLite is authority; the on-disk marker is
    a fallback so existing (pre-registry) projects still read as
    commissioned.
    """
    row = _row(project_root)
    if row and row["commissioned_at"]:
        return True
    return has_marker(project_root)


# ── classification ──────────────────────────────────────────────────


def classify(project_root: Path, *, managed_active: bool = False) -> str:
    """Resolve the project's lifecycle state. ``managed_active`` is the
    caller's managed-mode verdict (commissioned → armed when a session is
    bound); when unknown, leave it False and the caller treats
    COMMISSIONED as 'ready, not yet enforcing'.
    """
    if is_commissioned(project_root):
        return STATE_ARMED if managed_active else STATE_COMMISSIONED
    if adoption_evidence(project_root):
        return STATE_ADOPTED_UNCOMMISSIONED
    if has_memory(project_root):
        return STATE_FOREIGN_MEMORY
    return STATE_UNSEEN


# ── deliberate first adoption (operator: /aidocs or dashboard) ──────


def adopt(project_root: Path, *, source: str = "cli", title: str = "") -> dict:
    """Record a DELIBERATE first adoption in SQLite (does not create any
    files). After this, the project is ADOPTED_UNCOMMISSIONED and UPS may
    auto-repair it. Idempotent.
    """
    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT adopted_at FROM project_commission WHERE project_root = ?",
            (_norm(project_root),),
        ).fetchone()
        adopted_at = existing["adopted_at"] if existing and existing["adopted_at"] else _now()
        conn.execute(
            "INSERT INTO project_commission "
            "  (project_root, adopted_at, source, title) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_root) DO UPDATE SET "
            "  adopted_at = COALESCE(project_commission.adopted_at, "
            "    excluded.adopted_at), "
            "  source = excluded.source, title = excluded.title",
            (_norm(project_root), adopted_at, source, title),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "project_root": str(project_root),
        "adopted_at": adopted_at,
        "state": classify(project_root),
    }


# ── commissioning (minimal, non-destructive infrastructure) ─────────

# AIDOCS-OWNED paths the minimal commission creates. NONE of these is a
# user-authored file; CLAUDE.md / AGENTS.md / any pre-existing file is
# never touched here.
_OWNED_DIRS = (
    Path(".MEMORY") / ".aidocs",
    Path(".MEMORY") / ".index",
    Path(".MEMORY") / "sessions",
)

_MARKER_BODY = (
    "# AIDOCS project marker\n"
    "# Created by minimal commission (project_commission.commission).\n"
    "# Authority for managed state is the install-wide project_commission\n"
    "# table; this marker is a derived/fallback signal.\n"
)


def commission(project_root: Path, *, source: str = "auto_repair") -> dict:
    """Create the missing AIDOCS-owned infrastructure WITHOUT rewriting
    any user-authored file. Records commissioned_at in SQLite. Safe to
    call repeatedly; only creates what is absent.

    Refuses to commission a tree nested under an existing AIDOCS project
    (anti sub-project / confused-deputy) — this covers both explicit init
    and the UPS auto-repair path. FAIL-CLOSED: if the ancestor check
    cannot run, refuse rather than commission on an unverifiable check.
    """
    try:
        from .project_authority import assert_no_commissioned_ancestor

        guard = assert_no_commissioned_ancestor(project_root)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "ancestor_check_error",
            "error": repr(exc),
            "project_root": str(project_root),
            "created": [],
        }
    if not guard.get("ok"):
        # Distinguish a real nested ancestor from a check that could not
        # run (both refuse — fail closed — but keep stable reasons).
        reason = (
            "ancestor_check_error"
            if guard.get("blocked_by") == "ancestor_check_error"
            else "nested_under_commissioned"
        )
        return {
            "ok": False,
            "reason": reason,
            "ancestor": guard.get("ancestor"),
            "project_root": str(project_root),
            "created": [],
        }
    created: list[str] = []
    for rel in _OWNED_DIRS:
        d = project_root / rel
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(rel))
    marker = project_root / _MARKER_REL
    if not marker.exists():
        marker.write_text(_MARKER_BODY, encoding="utf-8")
        created.append(str(_MARKER_REL))
    # Commission STAMP (the governance-bearing signal): repair is the second
    # legitimate commissioning writer besides project_init — parity required,
    # or a repaired project would read unmanaged under the sqlite-stamp signal.
    try:
        from .code_index_store import CodeIndexStore

        _store = CodeIndexStore()
        _store.init_db(project_root)
        _store.mark_commissioned(project_root)
        created.append(".MEMORY/.index/aidocs.sqlite3 (commission stamp)")
    except Exception:
        # Best-effort: registry row below is still the SQLite source of truth.
        pass

    conn = _conn()
    try:
        now = _now()
        conn.execute(
            "INSERT INTO project_commission "
            "  (project_root, adopted_at, commissioned_at, source) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_root) DO UPDATE SET "
            "  adopted_at = COALESCE(project_commission.adopted_at, ?), "
            "  commissioned_at = ?, source = excluded.source",
            (_norm(project_root), now, now, source, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    # Bounded bootstrap migration phase: import any pre-registry legacy
    # control files (.mcp.json → mcp_servers, SESSION.md → session_membership)
    # into the canonical SQLite stores ONCE, here in commission. Normal read
    # paths NEVER ingest legacy files, so this phase (or the explicit
    # migrate-control-authority command) is the only way a pre-registry
    # project's legacy state enters authority. Best-effort: a hiccup must not
    # block commissioning — the per-store marker stays unsealed so the
    # explicit command (or the next commission) retries.
    #
    # OBSERVABLE, not silent: the outcome (applied/no_op/failed + counts,
    # including any skipped invalid entries) is recorded to the execution
    # ledger so a bootstrap import is never an invisible authority change.
    migration: dict = {}
    try:
        from .mcp_registry_store import McpRegistryStore
        from .session_membership_store import SessionMembershipStore

        mcp_res = McpRegistryStore().migrate_legacy_once(project_root)
        sess_res = SessionMembershipStore().migrate_legacy_once(project_root)
        applied = mcp_res["status"] == "migrated" or sess_res["status"] == "migrated"
        migration = {
            "status": "applied" if applied else "no_op",
            "mcp_servers": mcp_res,
            "sessions": sess_res,
        }
    except Exception as exc:
        migration = {"status": "failed", "error": repr(exc)}
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="control_plane_migration",
            source_kind="commission_bootstrap",
            capability_name="migrate-control-authority",
            action_kind="legacy_import",
            target_entity="control_authority/legacy_import",
            status=migration.get("status", "failed"),
            principal_type="system",
            scope_id=str(project_root).replace("\\", "/"),
            payload={"phase": "commission", "source_commission": source, **migration},
        )
    except Exception:
        pass

    return {
        "ok": True,
        "created": created,
        "project_root": str(project_root),
        "state": classify(project_root),
        "migration": migration,
    }


def repair_if_adopted(project_root: Path) -> dict:
    """UPS entry point: commission an ADOPTED_UNCOMMISSIONED project (and
    ONLY that state). Never performs first adoption; never touches a
    FOREIGN_MEMORY or UNSEEN project; no-op once commissioned.
    """
    state = classify(project_root)
    if state != STATE_ADOPTED_UNCOMMISSIONED:
        return {"ok": True, "repaired": False, "state": state}
    res = commission(project_root, source="ups_auto_repair")
    res["repaired"] = True
    return res
