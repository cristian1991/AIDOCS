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

# #755/#756: the four remaining sites here were
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- with no pragmas.
# DURABILITY: RUNTIME, matching the site already migrated below. The
# compaction epoch is live host telemetry (a counter of compaction events
# plus the model/window in play); losing the last commits to a power cut
# hands back no authorisation, and these run on hook events, which is
# exactly where synchronous=NORMAL's 8-10x lands. The two pure SELECTs
# say read_only=True.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect
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

# Tag namespace for the SUBAGENT id, which is derived FROM the parent id --
# sha16(tag + parent_agent_context_id + ":" + agent_id) -- never concatenated
# into the parent's own payload.
#
# The first cut did concatenate, with a ":subagent:v1:" segment called
# "self-delimiting". Mutation M3 (2026-08-22) reduced that segment to a bare ":"
# and SURVIVED the suite, which exposed the real defect: the property was never
# true. A host reporting host_session_id="parent:subagent:v1:evil" produced the
# identical payload to the genuine pair ("parent", "evil") -- same bytes, same
# id, no delimiter length can fix it. Because this id keys strikes, freezes and
# read grants, that collision was an authority bypass.
#
# Hashing the parent first makes forgery structurally impossible: host_session_id
# reaches the child only through a sha16 it cannot invert, and the child's tag
# namespace is disjoint from the parent's.
_AGENT_SUBAGENT_VERSION_TAG = "agent-context-subagent:v1:"


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
    agent_id: str | None = None,
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

    ``agent_id`` — the SUBAGENT link (measured 2026-08-22, Claude Code 2.1.239).
    A subagent's hook payload carries its parent's ``session_id`` AND its parent's
    ``transcript_path``; the only field that differs is ``agent_id``. Derived from
    host_session_id alone, N concurrent subagents therefore collapse into ONE
    identity — and since this id is the strike/freeze/todo scope key, three lane
    agents earning one strike each were scored as one actor earning three, and the
    lockdown fell on a conductor that had done nothing. Including agent_id makes
    the per-agent isolation that ``security_violation_service._scope_key`` already
    promises actually achievable.

    THE PARENT'S FORMULA IS UNTOUCHED, and that is deliberate. Every live freeze
    row, todo primary key, banner-dedup record and read grant is keyed on ids
    already derived by the v1 formula. Rotating them would orphan all of it
    silently — freezes would stop matching the actor they bind, todo state would
    fork. So with no agent_id this returns the v1 id BYTE-IDENTICALLY (the main
    thread, and every host that never sends one). No migration, no backfill —
    the module's standing contract.

    With an agent_id it returns a SECOND-LAYER id: sha16 over its own tag
    namespace, the parent id, and agent_id. Deriving from the already-hashed
    parent (rather than concatenating into the parent's payload) is what makes
    the child unforgeable — see _AGENT_SUBAGENT_VERSION_TAG for the collision
    this replaced and the mutant that exposed it.

    Blank/whitespace agent_id is treated as absent: some hosts send "" for the
    main thread, and that must land on the same identity as sending nothing, or
    the conductor forks in two depending on which channel spoke last.

    agent_id does NOT resurrect a missing host_session_id — the fail-closed rule
    above still wins. A subagent whose parent session is unknown is unidentified,
    not identified-by-agent_id.
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
    parent = _sha16(payload)
    agent = str(agent_id or "").strip()
    if not agent:
        return parent
    # DERIVED FROM THE PARENT ID, never concatenated into the parent's payload.
    #
    # The first cut appended ":subagent:v1:"+agent_id to `payload` and justified
    # it as "self-delimiting". That was WRONG, and the mutation gate proved it:
    # concatenation is ambiguous no matter how ornate the delimiter, so a host
    # reporting its session id as "parent:subagent:v1:evil" built byte-for-byte
    # the same payload as the genuine pair ("parent", "evil") and derived the
    # SAME id. host_session_id is host-supplied -- attacker-influenced in exactly
    # the threat model this id defends, since it keys strikes, freezes and read
    # grants -- so that collision was an authority bypass, not a curiosity.
    #
    # Hashing the parent FIRST closes it structurally: host_session_id reaches
    # the child only through a sha16 it cannot invert, and the child lives in its
    # own tag namespace, so no parent-payload string can collide with a child id.
    return _sha16(_AGENT_SUBAGENT_VERSION_TAG + parent + ":" + agent)



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
        model_id          TEXT NOT NULL DEFAULT '',
        context_window    INTEGER NOT NULL DEFAULT 0,
        updated_at        TEXT NOT NULL,
        PRIMARY KEY (host_kind, host_session_id)
    )
"""


def _init_db(project_root: Path) -> None:
    # The SECOND adoption-by-side-effect tunnel (operator report 2026-07-28). This
    # module does not ride SQLiteIndexStoreBase, so it carried its own
    # mkdir(parents=True) and created `.MEMORY/.index/` — plus
    # aidocs_identity.sqlite3 — in any directory a SessionStart merely touched.
    # Fixing only the store base would have left this one open, which is why the
    # rule lives in ONE shared function that both call.
    from ._sqlite_index_store_base import _require_adopted

    _require_adopted(project_root)
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    # #756: canonical connect -> the `with` block closes, not just commits.
    with _canonical_connect(db, durability=_Durability.RUNTIME) as conn:
        conn.execute(_TABLE_DDL)
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(agent_memory_compaction_state)"
            ).fetchall()
        }
        if "model_id" not in columns:
            conn.execute(
                "ALTER TABLE agent_memory_compaction_state "
                "ADD COLUMN model_id TEXT NOT NULL DEFAULT ''"
            )
        if "context_window" not in columns:
            conn.execute(
                "ALTER TABLE agent_memory_compaction_state "
                "ADD COLUMN context_window INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()


def record_host_context_profile(
    project_root: Path,
    *,
    host_kind: str,
    host_session_id: str,
    model_id: str = "",
    context_window: int = 0,
) -> dict[str, object] | None:
    """Persist the host's identity + metadata beside compaction state.

    #587: this row is now READ BACK as a rung of ``resolve_host_identity``, so it
    is no longer "non-authority" metadata — it is the DURABLE half of the
    authority, the part that survives a request boundary. That promotion is only
    sound if the row never carries a fabricated kind: it used to coerce an empty
    ``host_kind`` to ``"unknown"``, which is exactly the colliding bucket
    ``derive_agent_context_id`` refuses to build. A kind-less profile is now
    REFUSED (returns None) rather than written as a lie that a later reader would
    trust. Callers that legitimately have no kind simply record nothing; the
    resolver then answers an honest "".
    """
    kind = normalize_host_kind(host_kind)
    sid = str(host_session_id or "").strip()
    if not sid or not kind:
        return None
    _init_db(project_root)
    existing = get_host_context_profile(
        project_root,
        host_session_id=sid,
        host_kind=kind,
    )
    resolved_model = str(model_id or "").strip() or str(
        (existing or {}).get("model_id") or ""
    )
    try:
        resolved_window = int(context_window or 0)
    except (TypeError, ValueError):
        resolved_window = 0
    if resolved_window <= 0:
        resolved_window = int((existing or {}).get("context_window") or 0)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _canonical_connect(
        str(_db_path(project_root)),
        durability=_Durability.RUNTIME,
        row_factory=False,
    ) as conn:
        conn.execute(
            "INSERT INTO agent_memory_compaction_state "
            "(host_kind, host_session_id, compaction_count, model_id, "
            "context_window, updated_at) VALUES (?, ?, 0, ?, ?, ?) "
            "ON CONFLICT(host_kind, host_session_id) DO UPDATE SET "
            "model_id = excluded.model_id, "
            "context_window = excluded.context_window, "
            "updated_at = excluded.updated_at",
            (kind, sid, resolved_model, resolved_window, ts),
        )
        conn.commit()
    return get_host_context_profile(
        project_root,
        host_session_id=sid,
        host_kind=kind,
    )


def get_host_context_profile(
    project_root: Path,
    *,
    host_session_id: str,
    host_kind: str = "",
) -> dict[str, object] | None:
    """Read the latest profile for one host session without affecting epoch."""
    sid = str(host_session_id or "").strip()
    if not sid:
        return None
    _init_db(project_root)
    with _canonical_connect(str(_db_path(project_root)), read_only=True) as conn:
        conn.row_factory = sqlite3.Row
        if host_kind:
            row = conn.execute(
                "SELECT host_kind, host_session_id, model_id, context_window, "
                "updated_at FROM agent_memory_compaction_state "
                "WHERE host_kind = ? AND host_session_id = ? LIMIT 1",
                (str(host_kind).strip(), sid),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT host_kind, host_session_id, model_id, context_window, "
                "updated_at FROM agent_memory_compaction_state "
                "WHERE host_session_id = ? "
                "ORDER BY updated_at DESC, host_kind ASC LIMIT 1",
                (sid,),
            ).fetchone()
    if row is None:
        return None
    return {
        "host_kind": str(row["host_kind"] or ""),
        "host_session_id": str(row["host_session_id"] or ""),
        "model_id": str(row["model_id"] or ""),
        "context_window": int(row["context_window"] or 0),
        "updated_at": str(row["updated_at"] or ""),
    }


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
    with _canonical_connect(str(db), read_only=True, row_factory=False) as conn:
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
    with _canonical_connect(
        str(db), durability=_Durability.RUNTIME, row_factory=False
    ) as conn:
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


# ── The ONE resolution authority (#525 / #539) ──

# The placeholder that fail-OPEN surfaces have always keyed on when the host's
# real kind was unavailable. It is NOT a host kind: it is the colliding bucket
# `derive_agent_context_id` refuses to build, smuggled past that refusal as a
# non-empty string. It is named here so the two contracts can be told apart —
# `resolve_host_identity` strips it (honest ""), `resolve_epoch` re-applies it
# (byte-identical fail-open dedup) — and so a search finds every site that has
# to change when #525 lands.
LEGACY_UNKNOWN_HOST_KIND = "unknown"


def normalize_host_kind(host_kind: str | None) -> str:
    """Canonical form of a host_kind, or an honest "" when there isn't one.

    #587 item D: casing was UNMEASURED, and it is a real divergence — the id
    derivations hash ``host_kind`` RAW, so a host that reports ``Claude_Code``
    on one edge and ``claude_code`` on another gets two ``agent_context_id``s
    for one conversation, i.e. one more answer on top of the six. Every rung of
    the resolver, and every durable write, now passes through here, so the case
    a host happens to use can no longer fork the identity.

    The legacy ``"unknown"`` placeholder is stripped to "" HERE, once, so no
    caller has to remember to do it: it is not a kind, it is the colliding
    bucket ``derive_agent_context_id`` exists to refuse.
    """
    kind = str(host_kind or "").strip().lower()
    if kind == LEGACY_UNKNOWN_HOST_KIND:
        return ""
    return kind


def record_host_identity(
    project_root: Path,
    *,
    host_kind: str,
    host_session_id: str,
) -> bool:
    """THE durable write of "this host session belongs to this host kind".

    #587-A. Before this, ``host_kind`` existed only for the lifetime of one
    request (a ContextVar the transport stamped) or was GUESSED from the MCP
    server's own environment. Nothing persisted it, so the moment a request
    ended the authoritative answer was gone and the next caller had to guess
    again — that is how one session accumulated six answers.

    Truth is recorded by the code that actually SEES the inbound request (the
    UPS / SessionStart hook path and the stamp sites), keyed by the host's own
    session id, and read back by ``resolve_host_identity``. No new table and no
    new authority: it is the same ``agent_memory_compaction_state`` row the
    SessionStart profile already wrote — the row was simply never consulted for
    identity.

    Returns True when a real identity was recorded. An unresolvable kind is NOT
    recorded (never "unknown"): an unknown host must stay unknown.
    """
    kind = normalize_host_kind(host_kind)
    sid = str(host_session_id or "").strip()
    if not kind or not sid:
        return False
    try:
        return bool(
            record_host_context_profile(
                project_root,
                host_kind=kind,
                host_session_id=sid,
            )
        )
    except Exception:
        # A durable identity record is best-effort at the WRITE edge: failing a
        # tool call because a metadata row could not be written would be a worse
        # outage than resolving one request from the stamp alone.
        return False


def persisted_host_kind(project_root: Path, host_session_id: str) -> str:
    """Read back the recorded host_kind for a host session, or "".

    This is the rung that makes identity SURVIVE A REQUEST BOUNDARY. It is
    keyed STRICTLY by the caller's own ``host_session_id``: it can only ever
    answer for the session that was asked about, so it can never hand one
    conductor's identity to another agent — the failure #587 exists to prevent.
    It is emphatically NOT a "last host we saw" default; with no session id
    there is no key and no answer.

    Legacy rows written before ``record_host_context_profile`` refused a
    kind-less write may still carry the fabricated ``"unknown"``. Those are
    filtered out here rather than migrated, so the placeholder cannot be
    laundered back into the id-tree by a stale row.
    """
    sid = str(host_session_id or "").strip()
    if not sid:
        return ""
    try:
        profile = get_host_context_profile(project_root, host_session_id=sid)
    except Exception:
        return ""
    return normalize_host_kind((profile or {}).get("host_kind"))


def _env_sniffed_host_kind() -> str:
    """LAST-RESORT host_kind guess from the MCP SERVER's OWN environment.

    This is a KNOWN DEFECT, kept deliberately and named so it stops being
    invisible. It reads CLAUDE_CODE_VERSION / OPENCODE_VERSION out of *this*
    process, which is only the host's environment by accident — true when the
    server is a stdio child of the host, false for the shared HTTP daemon, and
    wrong for every host that is neither claude_code nor opencode (codex_cli,
    openai_agents, conductor_worker, generic_mcp are all first-class in
    host_support_matrix but land in "unknown" here).

    STATUS AFTER #587-A: it is no longer the ONLY source on the stdio path, and
    it is no longer the FIRST one tried. The stamp sites now go through
    ``stamp_host_identity`` (which stamps the kind as well as the session id and
    RECORDS it), and the UPS path records the host's own stated kind, so
    ``resolve_host_identity`` reaches the durable record BEFORE falling through
    to this sniff. What is left for it to cover is the genuinely cold case: a
    stdio session whose host has never been through a hook or a stamp, where
    there is no record to read yet.

    It still cannot be DELETED, because deleting it would turn that cold case
    from "guessed, and usually right for the two hosts it knows" into "no kind
    at all". The honest description of the remaining gap: for any host other
    than claude_code / opencode (codex_cli, openai_agents, conductor_worker,
    generic_mcp are all first-class in host_support_matrix) this still answers
    "" on a cold start, and the epoch is then unavailable rather than wrong —
    which is the correct failure direction. See backlog #525/#587.
    """
    import os

    if os.environ.get("CLAUDE_CODE_VERSION", "").strip():
        return "claude_code"
    if os.environ.get("OPENCODE_VERSION", "").strip():
        return "opencode"
    return ""


def resolve_host_identity(
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    project_root: Path | str | None = None,
) -> tuple[str, str]:
    """THE single authority for ``(host_kind, host_session_id)``.

    Every epoch-derivation edge resolves identity HERE and nowhere else. It used
    to be resolved ad hoc in six places — three copy-pasted ``_detect_host_kind``
    + ``_resolve_epoch`` pairs in the surfacers, a hardcoded "generic_mcp" in the
    project_scope middleware, and a sqlite read-back in agent_audit — and those
    copies is HOW #539 happened: two writers of ONE dedup ledger row resolved the
    same session's identity differently on 12 of 12 consecutive calls, so every
    call looked like news (#565 was a symptom of it).

    Resolution ladder, highest authority first:

      1. EXPLICIT arguments. The caller that has the hook/UPS payload knows who
         the host is; a value the HOST stated always wins.
      2. The REQUEST-scoped identity stamp (``_request_host_*`` ContextVars, set
         by the transport for the duration of one request). This is the correct
         second source and it is why the surfacers must stop reaching past it to
         the environment: for the HTTP gate the stamped kind is the caller-
         resolved per-client kind, and the env sniff below would silently
         override it with the daemon's own environment.
      3. The process-global conductor stamp. RETAINED UNDER PROTEST: a mutable
         process global is not an identity source, and on a shared daemon it is
         the leak vector #280 was opened for. It is still the only host_session_id
         a stdio host ever gets, so removing it here would blank the epoch for
         every stdio session rather than harden anything.
      4. THE PERSISTED RECORD for this session id (#587-A), when a
         ``project_root`` is available. This is what makes the answer SURVIVE A
         REQUEST BOUNDARY: the request that saw the host stated its kind and
         RECORDED it (``record_host_identity``), so a later request that knows
         only WHO (the host_session_id) can recover WHAT HOST without guessing.
         It ranks below the live stamps — a host restating itself always wins
         over a record — and above the environment, because a value the host
         actually stated at some point beats a value sniffed off this process.
         Keyed strictly by the session id, so it can never answer for a session
         it was not asked about.
      5. ``_env_sniffed_host_kind()`` for the KIND ONLY — see its docstring for
         why it survives and what has to land before it can go.

    Returns HONEST EMPTIES. A link this cannot resolve comes back "" so
    ``require_epoch`` can raise naming it; this never fabricates "unknown".

    Every rung is normalised through ``normalize_host_kind``, so the same host
    reported in different casing is ONE answer rather than a seventh.
    """
    kind = normalize_host_kind(host_kind)
    sid = str(host_session_id or "").strip()
    if kind and sid:
        return (kind, sid)

    # Go through the PUBLIC accessors, never the module attributes behind them.
    # Those accessors are the documented seam (and what every existing test
    # patches); reaching past them would fork resolution a fourth way, which is
    # the bug this function exists to end.
    try:
        from . import mcp_server_runtime_helpers as _h

        if not sid:
            sid = str(_h.current_calling_host_session_id() or "").strip()
        if not kind:
            kind = str(_h.current_calling_host_kind() or "").strip()
    except Exception:
        pass

    # LEGACY_UNKNOWN is a FABRICATION, not a kind. `derive_agent_context_id`
    # refuses an EMPTY host_kind precisely so kind-less hosts cannot collide into
    # one bucket — and this placeholder walks straight past that refusal, because
    # it is a non-empty string. `current_calling_host_kind()` substitutes it
    # whenever nothing was stamped, so the authority must strip it back out to
    # answer honestly. (normalize_host_kind is where that stripping now lives.)
    kind = normalize_host_kind(kind)

    # Rung 4 — the DURABLE record. Only reachable with a session id to key on;
    # never a "last host we saw" default.
    if not kind and sid and project_root is not None:
        kind = persisted_host_kind(Path(str(project_root)), sid)

    if not kind:
        kind = normalize_host_kind(_env_sniffed_host_kind())
    return (kind, sid)


def resolve_epoch(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> str:
    """Fail-OPEN epoch for best-effort surfaces (banners, skill blocks, memory
    hints, backlog nags), resolving identity via the ONE authority above.

    Returns "" when host_session_id is unresolvable — the caller then surfaces
    UNCONDITIONALLY, because a dedup key it cannot compute must degrade to a
    duplicate, never to silence.

    DELIBERATE FAIL-OPEN, byte-identical to the three hand-rolled copies this
    replaced: when the host's real kind is unavailable the epoch is still derived,
    against ``LEGACY_UNKNOWN_HOST_KIND``. That placeholder is wrong — it buckets
    every kind-less host together — but it IS the current dedup key, and refusing
    it here instead would silently stop deduplicating banners on every kind-less
    path (measured: 6 failures across ``test_dnt_banner_injection`` and
    ``test_agent_memory_epoch`` when this returned "" instead). Fixing the INPUT
    comes before tightening the guard.

    Callers for whom the epoch is a HARD prerequisite must use ``require_epoch``,
    which refuses instead of bucketing. That flip is blocked on the identity
    inputs being stable — see backlog #525 and
    ``tests/runtime/test_identity_resolver_stability.py``.
    """
    kind, sid = resolve_host_identity(
        host_kind=host_kind,
        host_session_id=host_session_id,
        project_root=project_root,
    )
    if not sid:
        return ""
    if not kind:
        kind = LEGACY_UNKNOWN_HOST_KIND
    try:
        return current_epoch(project_root, host_kind=kind, host_session_id=sid)
    except Exception:
        return ""


def stamp_host_identity(
    project_root: Path,
    *,
    host_session_id: str,
    host_kind: str = "",
) -> tuple[str, str]:
    """THE stamp sites' single entry point (#587-A). Resolve, stamp, RECORD.

    The three stdio stamp sites (``mcp_server`` conductor-branch and
    ``conductor_mode_enter``, ``server_session_tools.session_start`` twice) are
    the only production code that both sees an inbound request AND recovers a
    ``host_session_id`` from the query-gate bridge. Every one of them used to
    call ``set_calling_conductor_host_session_id(sid)`` with NO host_kind, which
    is precisely why the authoritative kind did not exist anywhere to read on
    the stdio path: the sites that could have said WHO said only WHICH SESSION.

    This does the three things that were missing, in one place so they cannot
    drift apart again:

      1. RESOLVE the kind through the ONE authority (explicit → request stamp →
         process stamp → durable record → env sniff);
      2. STAMP it onto the process identity so the rest of THIS process's calls
         see a real kind instead of the "unknown" placeholder;
      3. RECORD it durably so it survives the request boundary.

    Returns the honest ``(host_kind, host_session_id)`` it resolved — "" for the
    kind when the host genuinely cannot be identified. It NEVER invents one:
    stamping a guessed kind is how a subagent would be reattached to another
    conductor's session id.
    """
    sid = str(host_session_id or "").strip()
    if not sid:
        return ("", "")
    kind, sid = resolve_host_identity(
        host_kind=host_kind,
        host_session_id=sid,
        project_root=project_root,
    )
    try:
        from .mcp_server_runtime_helpers import (
            set_calling_conductor_host_session_id,
        )

        # host_kind="" is ignored by the setter, so an unidentified host stamps
        # its session id only — no fabricated kind enters the process stamp.
        set_calling_conductor_host_session_id(sid, host_kind=kind)
    except Exception:
        pass
    if kind:
        record_host_identity(
            project_root,
            host_kind=kind,
            host_session_id=sid,
        )
    return (kind, sid)


def correlate_host_session(project_root: Path) -> tuple[str, str]:
    """Server-side correlation: declared root -> the ONE live host session (#599/#54).

    THE stateless-HTTP identity source. Under ``DAEMON_STATELESS_HTTP`` the
    FastMCP session_id is minted PER REQUEST, so the transport cannot carry a
    stable host identity and the host's static ``.mcp.json`` cannot declare
    one (#54 transport investigation). The operator's ruling forbids the
    obvious alternative — an agent-passed id is spoofable, so identity must be
    "inferred automatically by code" from what the server itself observes.

    What the server observes durably: the conductor registry
    (``aidocs_managed_per_conductor``), written by ``session_connect`` from the
    hook-recovered ``cli_session_id`` — the host's OWN id, stated on the
    HOOK/UPS path, never a tool argument. Every request already declares a
    validated commissioned root (``?root=``). The join is therefore:

        declared root  ->  the live conductor bindings on that root

    and it resolves ONLY when the answer is unambiguous (#54 option B):

      * exactly ONE live binding, and NO running worker lane
                                  -> ``(recorded host_kind or "", cli_session_id)``
      * zero live bindings        -> ``("", "")``
      * two or more live bindings -> ``("", "")`` — two host windows on one
        root; most-recently-active-wins is REFUSED, it is the mutable
        last-writer shape this war exists to end, one level up (#54 option A);
      * any RUNNING worker lane   -> ``("", "")`` — a worker reaches the
        daemon through the SAME declared root, and a per-request caller
        cannot be told apart from the conductor, so correlating would merge
        two actors into one bucket (the #651 shared-bucket failure).

    LIVENESS IS NOT THE BOOT-TOKEN PID (#982). That stamp names the DAEMON that
    wrote the row — provenance, never actor liveness — and filtering on it here
    both erased every row after a restart and let one of two rows win the
    single-candidate test by process provenance. Every row is a candidate; the
    ambiguity is resolved by REFUSING (``("", "")``), never by a pid.

    Returns the CANONICAL UNRESOLVED MARKER — the honest empty string per
    axis, ``("", "")`` — whenever it cannot answer truthfully. Never a
    fabricated kind, never an exception: hard consumers get their typed
    refusal from ``require_epoch`` (``IdentityTreeError`` naming the link).

    Reads DURABLE stores only (no process globals, no env), so the answer
    survives a daemon hot-swap (#435) and is identical from a fresh process.
    """
    try:
        root = Path(str(project_root))
        from .aidocs_managed_store import AidocsManagedStore

        # NO BOOT-TOKEN PID FILTER (#982, operator ruling 2026-08-30: "Remove
        # daemon-PID death as an actor-death predicate EVERYWHERE").
        #
        # This used to `continue` past any row whose boot-stamp pid was not
        # alive. That pid is the MCP SERVER's own, so the filter asked "did the
        # process that WROTE this row die?" and used the answer to decide which
        # ACTOR is live. Two concrete harms, and the second is worse:
        #   * after any restart EVERY row is excluded, so this returns the
        #     unresolved marker for a project whose conductor is sitting right
        #     there;
        #   * with two rows, excluding one by daemon pid lets the OTHER win the
        #     `len(live) == 1` test — a winner picked by process provenance,
        #     which is exactly the ambiguity this function refuses to resolve.
        #
        # Every row is now a candidate. Ambiguity still returns ("", ""), the
        # honest unresolved marker this function already documents — so the
        # failure direction is unchanged and only the false CONFIDENCE is gone.
        live: list[dict] = []
        for row in AidocsManagedStore().list_conductors(root):
            live.append(row)
            if len(live) > 1:
                return ("", "")  # Ambiguous: never pick a winner.
        if len(live) != 1:
            return ("", "")
        sid = str(live[0].get("cli_session_id") or "").strip()
        if not sid:
            return ("", "")

        # A live worker lane is a second actor behind the same declared root.
        from .session_lane_agents_store import SessionLaneAgentsStore, _pid_alive

        for lane in SessionLaneAgentsStore().get_all_lane_agents(
            root, state_filter="running"
        ):
            lane_pid = lane.get("pid")
            try:
                lane_pid = int(lane_pid or 0)
            except (TypeError, ValueError):
                lane_pid = 0
            if lane_pid <= 0 or _pid_alive(lane_pid):
                return ("", "")  # Running (or unprovably dead) worker: refuse.

        return (persisted_host_kind(root, sid), sid)
    except Exception:
        return ("", "")


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
