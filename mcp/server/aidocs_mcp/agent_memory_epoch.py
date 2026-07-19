"""AIDOCS deterministic identity stack.

All identities are pure derivations — sha256 over version-tagged inputs,
truncated to 16 hex chars. Same inputs → same id, forever. No random
uuids, no migration, no backfill. Compaction count is the only mutable
piece (sqlite-backed).

Layered top-to-bottom:

    project_uuid =
        sha256("aidocs-project:v1:" + normalized_project_root)[:16]

    session_uuid =
        sha256("aidocs-session:v1:" + project_uuid + ":" + session_dir_name)[:16]

    agent_context_id =
        sha256(
            "agent-context:v1:" +
            project_uuid + ":" +
            host_kind + ":" +
            host_session_id
        )[:16]

    aidocs_session_id =
        sha256(
            "aidocs-session-bind:v1:" +
            project_uuid + ":" +
            host_kind + ":" +
            host_session_id + ":" +
            session_uuid
        )[:16]

    agent_memory_epoch =
        sha256(
            "agent-memory-epoch:v1:" +
            agent_context_id + ":" +
            compaction_count
        )[:16]

Identity contract (locked 2026-04-28):

- host_session_id      = raw host value (Claude/OpenCode/Codex), input
                         only, never primary AIDOCS identity.
- session_id (work)    = operator's human-readable label like
                         "2026-04-27-castle-maintenance". Filesystem
                         dir name. UX/routing only.
- project_uuid         = derived from project_root path.
- session_uuid         = derived from project_uuid + work session label.
- agent_context_id     = derived per-conductor; gates "what the agent
                         has been told" (banner dedup, read grants).
                         Excludes session_uuid → switching work session
                         within the same conversation does NOT reset
                         agent memory.
- aidocs_session_id    = derived per-conductor-per-work-session; used
                         for work-bound state (audit, lane bind, task
                         lifecycle). Includes session_uuid → switching
                         work session DOES change this id.
- agent_memory_epoch   = agent_context_id + compaction_count. Rotates
                         on compaction so per-conversation gates reset
                         cleanly.

Compaction count is host-pushed via bump_compaction_count. Stored per
(host_kind, host_session_id). Hosts wire their own compaction events.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path


def _db_path(project_root: Path) -> Path:
    """Same identity-sqlite db used by ProtectedFileRegistryStore."""
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"


_PROJECT_VERSION_TAG = "aidocs-project:v1:"
_SESSION_VERSION_TAG = "aidocs-session:v1:"
_AGENT_CONTEXT_VERSION_TAG = "agent-context:v1:"
_SESSION_BIND_VERSION_TAG = "aidocs-session-bind:v1:"
_EPOCH_VERSION_TAG = "agent-memory-epoch:v1:"


def _sha16(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_root(project_root: Path | str) -> str:
    p = Path(str(project_root))
    try:
        return str(p.resolve()).replace("\\", "/").rstrip("/")
    except Exception:
        return str(p).replace("\\", "/").rstrip("/")


# ── Pure derivations ──


def derive_project_uuid(project_root: Path | str) -> str:
    """sha16 of normalized project_root. Reproducible from path alone."""
    return _sha16(_PROJECT_VERSION_TAG + _normalize_root(project_root))


def derive_session_uuid(project_root: Path | str, session_dir_name: str) -> str:
    """sha16 of project_uuid + session dir name. Reproducible from
    project + the human-readable session label.
    """
    if not session_dir_name:
        return ""
    project_uuid = derive_project_uuid(project_root)
    return _sha16(_SESSION_VERSION_TAG + project_uuid + ":" + session_dir_name)


def derive_agent_context_id(
    *,
    host_kind: str,
    project_root: Path | str,
    host_session_id: str,
) -> str:
    """Per-conductor identity for agent-memory state (banner dedup,
    read grants, NLP intent). Excludes session_uuid so switching the
    work session inside the same conversation does NOT reset the
    agent's memory of what it has already seen.

    Empty host_session_id → empty id. Caller must refuse rather than
    fall back to anything stale.

    id-tree honesty (operator 2026-07-16): host_kind is a REQUIRED link, not a
    defaultable one. An empty host_kind returns "" (a missing link the caller must
    refuse) — NEVER a fabricated "unknown" bucket, which would collide every
    kind-less host into one epoch and silently mis-attribute their memory state.
    """
    if not host_session_id or not host_kind:
        return ""
    project_uuid = derive_project_uuid(project_root)
    payload = (
        _AGENT_CONTEXT_VERSION_TAG
        + project_uuid
        + ":"
        + host_kind
        + ":"
        + host_session_id
    )
    return _sha16(payload)


def derive_aidocs_session_id(
    *,
    host_kind: str,
    project_root: Path | str,
    host_session_id: str,
    session_uuid: str,
) -> str:
    """Per-conductor-per-work-session identity for work-bound state
    (audit, lane bind, task lifecycle). Includes session_uuid so
    switching work session DOES yield a fresh id.
    """
    if not host_session_id or not session_uuid or not host_kind:
        return ""
    project_uuid = derive_project_uuid(project_root)
    payload = (
        _SESSION_BIND_VERSION_TAG
        + project_uuid
        + ":"
        + host_kind
        + ":"
        + host_session_id
        + ":"
        + session_uuid
    )
    return _sha16(payload)


def derive_epoch(
    *,
    agent_context_id: str,
    compaction_count: int,
) -> str:
    """agent_memory_epoch from agent_context_id + count.
    Rotates on compaction; survives work-session switch.
    """
    if not agent_context_id:
        return ""
    payload = _EPOCH_VERSION_TAG + agent_context_id + ":" + str(int(compaction_count or 0))
    return _sha16(payload)


# ── Compaction-count store (only mutable piece) ──
# One row per (host_kind, host_session_id). Bumped by host plugins on
# compaction events.

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS agent_memory_compaction_state (
        host_kind         TEXT NOT NULL,
        host_session_id   TEXT NOT NULL,
        compaction_count  INTEGER NOT NULL DEFAULT 0,
        updated_at        TEXT NOT NULL,
        PRIMARY KEY (host_kind, host_session_id)
    )
"""


def _init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(_TABLE_DDL)
        conn.commit()


def get_compaction_count(
    project_root: Path,
    *,
    host_kind: str,
    host_session_id: str,
) -> int:
    if not host_kind or not host_session_id:
        return 0
    _init_db(project_root)
    db = _db_path(project_root)
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT compaction_count FROM agent_memory_compaction_state "
            "WHERE host_kind = ? AND host_session_id = ?",
            (host_kind, host_session_id),
        ).fetchone()
    return int(row[0]) if row else 0


def bump_compaction_count(
    project_root: Path,
    *,
    host_kind: str,
    host_session_id: str,
) -> int:
    """Atomically increment the count for (host_kind, host_session_id).
    Returns the new count. Hosts call this on their compaction events.
    """
    if not host_kind or not host_session_id:
        return 0
    _init_db(project_root)
    db = _db_path(project_root)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO agent_memory_compaction_state "
            "(host_kind, host_session_id, compaction_count, updated_at) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(host_kind, host_session_id) DO UPDATE SET "
            "compaction_count = compaction_count + 1, updated_at = ?",
            (host_kind, host_session_id, ts, ts),
        )
        row = conn.execute(
            "SELECT compaction_count FROM agent_memory_compaction_state "
            "WHERE host_kind = ? AND host_session_id = ?",
            (host_kind, host_session_id),
        ).fetchone()
        conn.commit()
    return int(row[0]) if row else 0


def current_epoch(
    project_root: Path,
    *,
    host_kind: str,
    host_session_id: str,
) -> str:
    """Derive agent_context_id, read current compaction_count, derive
    epoch. Returns "" when host_session_id is empty.
    """
    if not host_session_id:
        return ""
    ctx_id = derive_agent_context_id(
        host_kind=host_kind,
        project_root=project_root,
        host_session_id=host_session_id,
    )
    count = get_compaction_count(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
    )
    return derive_epoch(
        agent_context_id=ctx_id,
        compaction_count=count,
    )


class IdentityTreeError(Exception):
    """A prerequisite in the identity ladder is missing (operator 2026-07-16).

    The id-tree is a chain of PREREQUISITES:
        user_id (principal) => host_session_id => host_kind
                            => agent_context_id => epoch
    A break in the chain is a BUG, not a benign empty. Raised by ``require_epoch``
    so a caller that NEEDS an epoch fails CLOSED (loud) instead of failing OPEN on a
    "" epoch (which collapses distinct identities into one bucket) or a fabricated id.
    """

    def __init__(self, missing_link: str, detail: str = "") -> None:
        super().__init__(detail or f"id-tree prerequisite missing: {missing_link}")
        self.missing_link = missing_link


def require_epoch(
    project_root: Path,
    *,
    host_kind: str,
    host_session_id: str,
    user_id: str = "",
) -> str:
    """Resolve the agent_memory_epoch, failing CLOSED on any broken prerequisite.

    Unlike ``current_epoch`` (which returns "" for optional/best-effort surfaces),
    this is the seam for operations where an epoch is a HARD prerequisite — audit
    attribution, per-epoch notification/memory gates, anything keyed on the epoch.
    It never returns "": a missing link raises ``IdentityTreeError`` naming it.

    Enforced ladder (top-down):
      * user_id present but no host_session_id → the operator's exact bug: a real
        principal with no host session. FAIL CLOSED.
      * host_session_id required regardless (the epoch cannot exist without it) —
        epoch is a prerequisite, never fail-open.
      * host_session_id present but no host_kind → the id would otherwise be
        fabricated/empty. FAIL CLOSED.
      * the derived epoch must be non-empty (defense in depth).
    """
    uid = str(user_id or "").strip()
    hsid = str(host_session_id or "").strip()
    hkind = str(host_kind or "").strip()
    if uid and not hsid:
        raise IdentityTreeError(
            "host_session_id",
            f"principal {uid!r} present but no host_session_id — the epoch system "
            "requires a host session for every identified caller",
        )
    if not hsid:
        raise IdentityTreeError(
            "host_session_id",
            "epoch is a prerequisite: no host_session_id, so no epoch can be derived",
        )
    if not hkind:
        raise IdentityTreeError(
            "host_kind",
            "host_session_id present but no host_kind — refusing to fabricate an "
            "'unknown' bucket that would collide distinct hosts",
        )
    epoch = current_epoch(project_root, host_kind=hkind, host_session_id=hsid)
    if not epoch:
        raise IdentityTreeError(
            "epoch", "id-tree resolved to an empty epoch despite complete inputs"
        )
    return epoch
