"""Approval authority — a durable, general, agent-to-agent approval verdict.

THE NAME IS THE DOMAIN, NOT THE SEAT (r0b ruling iii, 2026-09-12). This module
was `coco_approval` until it acquired its first production consumer. `coco`
encoded "co-conductor", a word the sealed Consulship law retired: `r0a` and `r0b`
are both CONSULS of equal rank with a mutual veto, so there is no "co-" anything
left to name. BETA/r0b is the approving PRINCIPAL here -- it appears in data and
in policy (`backlog_admission.BETA_APPROVER_ROLE`) and deliberately nowhere in an
identifier in this file. The `coco_approvals` TABLE keeps its name on purpose: a
table rename is a data migration under the #242 interrupted-rename crash window,
the seat-succession path already reads rows there, and a storage spelling is not
a public surface. That residue is NAMED, not hidden.

WHY THIS EXISTS (Empire ruling 2026-09-10, backlog #1055). Durable work was
entering the trusted ledger on an agent's own assertion: "a lot of backlog items
are fake, bullshit, no-op, and untrustable." The remedy is NOT to gate the write
(an earlier ruling, 2026-07-29, deliberately left recording ungated so that every
gate refusal's named remedy stays reachable) but to gate ENTRY INTO TRUST. This
module is that gate's durable half; `backlog_admission` is its first consumer.

TWO LANES, ONE OF THEM HERE. The operator lane is the higher-level human
interaction flow and is NOT implemented here. This is the BETA lane:
agent-to-agent over XAACP, and deliberately GENERAL -- the subject is
`(subject_kind, subject_id)`, where SUBJECT_KIND IS THE PURPOSE. That is what
keeps one shared store safe across domains: `xaacp_seat` and
`backlog_admission` approvals can never satisfy one another, because every read
here is keyed on the kind and `is_approved` refuses a kind it cannot even shape-
check.

STRUCTURED ENVELOPE, FREETEXT BODY, AND NO RIVAL ENUM. The envelope must be
machine-readable because a verdict flips durable state deterministically; the
body must be prose because the REASON cannot be an enum (a co-conductor's
critique is a concrete flaw, alternative or risk, and "unease" is a logged
non-veto). So the envelope rides XAACP's typed fields
(`message_kind='approval_request'`, `correlation_id`, `metadata`) and the verdict
rides XAACP's EXISTING decision vocabulary -- `accepted` / `rejected` /
`blocked` -- via `xaacp_reply`. Minting an `approval_decision` enum beside it
would be the rival-definition breach this project keeps paying for: one logic,
one home.

All four XAACP decisions are terminal. For an ACK that is a known defect
(DRT-12); for an APPROVAL it is exactly right -- an approval is a one-shot
verdict, not a receipt -- so this primitive needed no protocol change.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from . import conductor_comms

logger = logging.getLogger("aidocs.approval_authority")

# The envelope's message_kind. A reader that sees this kind knows the metadata
# carries {subject_kind, subject_id, requested_action} and that a terminal
# xaacp_reply on it is a VERDICT, not a receipt.
APPROVAL_REQUEST_KIND = "approval_request"

# The audit action kinds, named once. They are a READABLE SURFACE (an operator
# greps them), so they carry the domain name rather than the retired seat word --
# and they are spelled here so a consumer asserting on the audit trail cannot
# drift from the writer.
AUDIT_REQUEST_KIND = "approval_request"
AUDIT_DECIDE_KIND = "approval_decide"
AUDIT_EXPIRE_KIND = "approval_expire"
AUDIT_CONSUME_KIND = "approval_consume"
AUDIT_REBIND_KIND = "approval_rebind"

# THE EVENT KIND THIS DOMAIN OWNS (r0b ruling, 2026-09-12). Every write above
# used to route through `conductor_comms._audit_event`, which HARDCODES
# event_kind="conductor_comms" -- and `conductor_comms` is OPERATIONAL: 7 days
# AND count-capped. `action_kind` proves WHAT happened; `event_kind` decides how
# long that proof survives, so the five governance writes were carrying a
# correct action_kind on a chatter horizon, and a burst of conductor traffic
# could count-cap away the audit record of an approval decision.
#
# ONE KIND, NOT FIVE. The five verbs are already distinguished by `action_kind`
# (AUDIT_*_KIND above), and a second axis is only worth minting when RETENTION
# semantics differ. All five are FORENSIC, so five entries would be five
# identical policies -- a dead distinction the registry would have to carry
# forever. The verb axis stays where it already was.
#
# FORENSIC, not DECISION, and the registry's own criteria decide it: the
# directly analogous `escalation_requested/approved/denied/consumed` are all
# FORENSIC, and these rows are "a gate decision, refusal, grant" about WHO WAS
# AUTHORISED TO ACT -- the security-and-governance record the class is for.
# DECISION would bound them at 90 days; an authorisation one may have to justify
# does not stop needing justification on day 91. The volume argument agrees:
# these fire once per approval lifecycle step, never on a hot path.
#
# NOT a generic `event_kind=` parameter on `_audit_event`. An optional override
# with a `conductor_comms` default recreates this exact bug by OMISSION -- the
# next governance write forgets the argument and silently inherits the chatter
# horizon. This helper owns its kind and has no parameter for it, so a caller
# CANNOT get it wrong.
AUDIT_EVENT_KIND = "approval_authority"

# WHERE A LOST GOVERNANCE AUDIT BECOMES VISIBLE. Classifying a row FORENSIC
# guarantees retention of a row that was WRITTEN; it says nothing about one that
# never was. `_audit_event` swallows every exception, so "FORENSIC but maybe
# never written" would be theatre. Mirrors `config_store.GLOBAL_AUDIT_DEGRADED`
# -- the estate's existing degraded surface -- rather than inventing a second
# mechanism.
GOVERNANCE_AUDIT_DEGRADED: list[dict] = []

# GENERALITY IS A DATA SHAPE, NOT A WHITELIST (#1055 guarantee 1). A new subject
# kind must need no schema change and no edit here, so the kind is validated for
# SHAPE only. `backlog_item` is merely the first one through the door.
_SUBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# The XAACP decisions that ARE an approval verdict, and the durable state each
# one lands. `completed` is the fourth XAACP decision and is NOT in this map on
# purpose: "done" is not "approved", and silently treating it as one would be the
# fail-open hole guarantee 6 exists to close.
_DECISION_TO_STATE = {
    "accepted": "approved",
    "rejected": "rejected",
    "blocked": "blocked",
}

# Only this state means approved. Every other state -- including every failure
# mode, every unknown subject and every expiry -- reads as NOT approved.
STATE_APPROVED = "approved"
STATE_PENDING = "pending"
STATE_EXPIRED = "expired"

# A request nobody rules on must AGE OUT EXPLICITLY (guarantee 7), because the
# alternative is a shadow queue that silently becomes the new swamp. Three days
# is long enough for a co-conductor seat to be re-seated after a restart and
# short enough that a stale request does not masquerade as live.
DEFAULT_TTL_SECONDS = 72 * 3600

# THE DURABLE HALF. The request lives in a TABLE, not in a message, because a
# message can be lost, unread or garbage-collected while "awaiting approval"
# quietly becomes "approved by nobody, forever" (guarantee 2). It shares the
# conductor_comms database so a verdict and the XAACP message that carried it are
# one restore unit -- a separate file could be restored half-way and leave a
# pending request pointing at a message that no longer exists.
_DDL = """
CREATE TABLE IF NOT EXISTS coco_approvals (
    id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    requested_action TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    lane_id TEXT NOT NULL DEFAULT '',
    requester_actor_id TEXT NOT NULL,
    approver_actor_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    decision TEXT NOT NULL DEFAULT '',
    decided_by_actor_id TEXT NOT NULL DEFAULT '',
    request_rationale TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    requested_at REAL NOT NULL,
    decided_at REAL,
    expires_at REAL,
    request_announced_at REAL,
    verdict_announced_at REAL
)
"""

# IDEMPOTENCY IS ENFORCED BY THE DATABASE, NOT BY A READ-THEN-WRITE (guarantee
# 3). A SELECT followed by an INSERT is two statements, and two agents racing
# through them both see "no open request" -- which is exactly how a subject ends
# up with two live requests and the notification fires twice (#1054). A partial
# unique index makes the second INSERT fail instead.
_ONE_OPEN_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS coco_approvals_one_open "
    "ON coco_approvals(subject_kind, subject_id) WHERE state='pending'"
)


# ADDITIVE COLUMNS, added after the table shipped. Plain ALTERs, never a table
# rebuild: the `coco_approvals` CHECK-free schema makes this safe, which is
# exactly why #1055's `proposed`-status design was refused on the backlog table
# (a CHECK constraint there needs the full rename dance under the #242 crash
# window) and is fine here.
#
#   consumed_ref      WHAT this approval was spent on (the backlog row id).
#                     NULL = never consumed. This is the exactly-once ledger:
#                     the AUTHORITATIVE store settles whether the work landed,
#                     not the caller's memory of whether it got a response.
#   claim_actor_id    who is mid-consumption, and
#   claim_session_id  where, and
#   claimed_at        when -- so a concurrent second consumer is REFUSED rather
#                     than racing into a duplicate row, and so an abandoned
#                     claim is attributable rather than permanent.
#   decided_by_role   THE ROLE THE DECIDER HELD AT DECISION TIME. Recorded,
#                     because it cannot be re-derived later: seats succeed, and
#                     re-resolving the actor at consumption time would make a
#                     properly rendered verdict evaporate the moment the seat
#                     changed hands. "Live at decision time" has to be WRITTEN
#                     DOWN to survive being true.
_ADDITIVE_COLUMNS = {
    "consumed_ref": "TEXT",
    "claim_actor_id": "TEXT NOT NULL DEFAULT ''",
    "claim_session_id": "TEXT NOT NULL DEFAULT ''",
    "claimed_at": "REAL",
    "decided_by_role": "TEXT NOT NULL DEFAULT ''",
}

# HOW LONG AN UNFINISHED CLAIM BLOCKS A RETRY. Short, because the window it
# covers is "between the claim and the row landing" -- a single local INSERT.
# Long enough that a concurrent caller is genuinely refused rather than waved
# through into a duplicate.
CLAIM_STALE_SECONDS = 120.0


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    conn.execute(_ONE_OPEN_INDEX)
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(coco_approvals)").fetchall()
    }
    for column, ddl in _ADDITIVE_COLUMNS.items():
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE coco_approvals ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError:
            # Another migrator won the race. The PRAGMA check alone is not
            # enough here: several agents open this same database concurrently,
            # so two processes can both see the column missing and both ALTER.
            pass


def _col(row: sqlite3.Row, name: str):
    """Read a possibly-not-yet-migrated column without pretending it is absent.

    `_ensure_schema` adds the additive columns on every connect, so in practice
    they are there. This exists for the one case that is not in practice: a row
    object produced by a reader that opened the table before the ALTER landed. A
    KeyError there would surface as "the approval could not be read", which
    `is_approved` would correctly translate to UNAPPROVED -- a fail-closed
    outcome, but one whose cause nobody could find.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _row_dict(row: sqlite3.Row) -> dict:
    return {
        "approval_id": str(row["id"]),
        "subject_kind": str(row["subject_kind"]),
        "subject_id": str(row["subject_id"]),
        "requested_action": str(row["requested_action"] or ""),
        "session_id": str(row["session_id"] or ""),
        "lane_id": str(row["lane_id"] or ""),
        "requester_actor_id": str(row["requester_actor_id"] or ""),
        "approver_actor_id": str(row["approver_actor_id"] or ""),
        "message_id": str(row["message_id"] or ""),
        "correlation_id": str(row["correlation_id"] or ""),
        "state": str(row["state"] or STATE_PENDING),
        "decision": str(row["decision"] or ""),
        "decided_by_actor_id": str(row["decided_by_actor_id"] or ""),
        "decided_by_role": str(_col(row, "decided_by_role") or ""),
        "consumed_ref": _col(row, "consumed_ref"),
        "claim_actor_id": str(_col(row, "claim_actor_id") or ""),
        "claim_session_id": str(_col(row, "claim_session_id") or ""),
        "claimed_at": _col(row, "claimed_at"),
        # WHAT WAS ASKED, not just what was answered. Absent from this shape
        # until a successor needed it: a redelivered obligation must carry the
        # original ask, or the inheriting seat is handed an id and no question.
        "request_rationale": str(_col(row, "request_rationale") or ""),
        "rationale": str(row["rationale"] or ""),
        "requested_at": row["requested_at"],
        "decided_at": row["decided_at"],
        "expires_at": row["expires_at"],
    }


def _expire_due(conn: sqlite3.Connection, *, now: float | None = None) -> list[str]:
    """Age out overdue requests EXPLICITLY and return the ids that aged out.

    Applied lazily at every entry point rather than by a sweeper, for the same
    reason `_xaacp_expire_due` is: a store whose TTL depends on a background
    process having run is a store whose TTL is a coin flip after a restart.
    """
    current = time.time() if now is None else float(now)
    rows = conn.execute(
        "SELECT id FROM coco_approvals WHERE state=? AND expires_at IS NOT NULL "
        "AND expires_at <= ?",
        (STATE_PENDING, current),
    ).fetchall()
    if not rows:
        return []
    conn.execute(
        "UPDATE coco_approvals SET state=?, decided_at=COALESCE(decided_at, ?) "
        "WHERE state=? AND expires_at IS NOT NULL AND expires_at <= ?",
        (STATE_EXPIRED, current, STATE_PENDING, current),
    )
    return [str(row["id"]) for row in rows]


def _governance_audit(
    project_root: Path,
    session_id: str,
    *,
    action_kind: str,
    target_entity: str,
    payload: dict,
) -> None:
    """Write this domain's governance audit row under :data:`AUDIT_EVENT_KIND`.

    Signature-compatible with ``conductor_comms._audit_event`` EXCEPT that there
    is no ``event_kind`` parameter: the kind is the helper's property, so no
    call site can omit it or pass the wrong one.

    Best-effort like the function it replaces -- these are called AFTER the
    authoritative `coco_approvals` write has committed, so raising here would
    turn a lost forensic copy into a lost authorisation. But the failure is NOT
    swallowed silently: it lands on :data:`GOVERNANCE_AUDIT_DEGRADED` and a
    secondary ``audit_emit_failed`` row is attempted, which is the one kind the
    registry says must never itself be pruned.
    """
    from .execution_index_store import ExecutionIndexStore

    try:
        ExecutionIndexStore().record_event(
            project_root,
            event_kind=AUDIT_EVENT_KIND,
            source_kind="mcp",
            session_id=session_id,
            action_kind=action_kind,
            target_entity=target_entity,
            status="ok",
            payload=payload,
        )
    except Exception as exc:
        marker = {
            "target_entity": target_entity,
            "action_kind": action_kind,
            "error": str(exc),
        }
        GOVERNANCE_AUDIT_DEGRADED.append(marker)
        logger.warning(
            "governance audit write LOST for %s/%s: %s",
            action_kind,
            target_entity,
            exc,
        )
        try:
            ExecutionIndexStore().record_event(
                project_root,
                event_kind="audit_emit_failed",
                source_kind="mcp",
                session_id=session_id,
                action_kind=action_kind,
                target_entity=target_entity,
                status="degraded",
                payload={"original_event_kind": AUDIT_EVENT_KIND, "error": str(exc)},
            )
        except Exception:
            pass


def _caller_actor(project_root: Path) -> dict[str, str]:
    """The acting actor, DERIVED. There is deliberately no parameter for this.

    Guarantee 5 is the one that makes the whole ruling more than theatre, and it
    is only as strong as the identity it compares. The instant either side of the
    comparison can be supplied by the caller, an agent files a row and approves
    it in the same breath. So both sides come from the SAME resolver `ai_msg`
    uses for its sender attribution, and an unresolvable caller is nobody --
    never a default actor (empire §V: unknown is not pass).
    """
    try:
        route = conductor_comms.xaacp_resolve_caller_route(project_root)
    except Exception:  # pragma: no cover - resolver is defensive already
        return {}
    actor_id = str(route.get("actor_id") or "").strip()
    if not actor_id or actor_id == conductor_comms.MSG_ROLE_UNMAPPED:
        return {}
    return {
        "actor_id": actor_id,
        "session_id": str(route.get("session_id") or "").strip(),
        "lane_id": str(route.get("lane_id") or "").strip(),
        "actor_kind": str(route.get("actor_kind") or "").strip(),
        # THE SEAT THE CALLER HOLDS, as the route resolver reports it -- '' for
        # a worker or an unseated agent. Carried here so a verdict can record
        # the role its decider held AT DECISION TIME (see `decide_approval`):
        # seats succeed, so that fact is only knowable while it is true.
        "role": str(route.get("role") or "").strip().lower(),
    }


def _invalid(error: str) -> dict:
    return {"ok": False, "status": "invalid", "error": error}


def request_approval(
    project_root: Path,
    *,
    subject_kind: str,
    subject_id: str,
    approver_actor_id: str,
    requested_action: str = "",
    rationale: str = "",
    ttl_seconds: float | None = None,
) -> dict:
    """Open (or return) the ONE open approval request for a subject.

    Returns `existing=True` when a request was already open: re-requesting is not
    a second request and must not re-announce. A subject that already carries a
    standing verdict is refused WITH that verdict rather than re-opened -- the
    verdict is one-shot.
    """
    subject_kind = str(subject_kind or "").strip()
    subject_id = str(subject_id or "").strip()
    approver_actor_id = str(approver_actor_id or "").strip()
    if not _SUBJECT_KIND_RE.match(subject_kind):
        return _invalid(
            "subject_kind must be a lowercase slug like 'backlog_item' "
            "(2-64 chars, [a-z][a-z0-9_]*)"
        )
    if not subject_id:
        return _invalid("subject_id is required")
    if not approver_actor_id:
        return _invalid("approver_actor_id is required")

    caller = _caller_actor(project_root)
    if not caller:
        return {
            "ok": False,
            "status": "unidentified",
            "error": (
                "the calling actor could not be derived; approval identity is "
                "never supplied by the caller"
            ),
        }
    requester_actor_id = caller["actor_id"]
    if requester_actor_id == approver_actor_id:
        return {
            "ok": False,
            "status": "self_approval_refused",
            "error": "the requesting actor cannot be the deciding actor",
            "requester_actor_id": requester_actor_id,
        }

    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
    if ttl != ttl or ttl in (float("inf"), float("-inf")) or ttl < 0:
        return _invalid("ttl_seconds must be a finite non-negative number")

    now = time.time()
    approval_id = str(uuid4())[:12]
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        _expire_due(conn, now=now)
        open_row = conn.execute(
            "SELECT * FROM coco_approvals WHERE subject_kind=? AND subject_id=? "
            "AND state=?",
            (subject_kind, subject_id, STATE_PENDING),
        ).fetchone()
        if open_row is not None:
            conn.commit()
            return {"ok": True, "existing": True, **_row_dict(open_row)}
        decided_row = conn.execute(
            "SELECT * FROM coco_approvals WHERE subject_kind=? AND subject_id=? "
            "AND state NOT IN (?, ?) ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            (subject_kind, subject_id, STATE_PENDING, STATE_EXPIRED),
        ).fetchone()
        if decided_row is not None:
            conn.commit()
            return {
                "ok": False,
                "status": "already_decided",
                "existing": True,
                **_row_dict(decided_row),
            }
        try:
            conn.execute(
                "INSERT INTO coco_approvals (id, subject_kind, subject_id, "
                "requested_action, session_id, lane_id, requester_actor_id, "
                "approver_actor_id, state, request_rationale, requested_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    subject_kind,
                    subject_id,
                    str(requested_action or ""),
                    caller["session_id"],
                    caller["lane_id"],
                    requester_actor_id,
                    approver_actor_id,
                    STATE_PENDING,
                    str(rationale or ""),
                    now,
                    now + ttl,
                ),
            )
        except sqlite3.IntegrityError:
            # The partial unique index caught a racing requester. The loser
            # reports the WINNER's request, which is what idempotency means.
            raced = conn.execute(
                "SELECT * FROM coco_approvals WHERE subject_kind=? AND "
                "subject_id=? AND state=?",
                (subject_kind, subject_id, STATE_PENDING),
            ).fetchone()
            conn.commit()
            if raced is None:  # pragma: no cover - violation with no surviving row
                return {"ok": False, "status": "conflict"}
            return {"ok": True, "existing": True, **_row_dict(raced)}
        conn.commit()

    # ONE ANNOUNCEMENT, AFTER THE ROW EXISTS. Announcing first would notify an
    # approver about a request the idempotency index then refuses to store.
    sent = conductor_comms.xaacp_send(
        project_root,
        session_id=caller["session_id"],
        sender_actor_id=requester_actor_id,
        target_actor_id=approver_actor_id,
        lane_id=caller["lane_id"],
        sender_actor_kind=caller["actor_kind"],
        message_kind=APPROVAL_REQUEST_KIND,
        body=(
            str(rationale or "")
            or f"approval requested for {subject_kind}:{subject_id}"
        ),
        metadata={
            "approval_id": approval_id,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "requested_action": str(requested_action or ""),
        },
        ttl_seconds=ttl,
    )
    if not sent.get("ok"):
        # FAIL CLOSED AND LEAVE NO GHOST. An unreachable approver must not leave
        # a pending row nobody will ever be notified about -- that row would read
        # as "awaiting approval" forever and block every re-request.
        with conductor_comms._connect(project_root) as conn:
            _ensure_schema(conn)
            conn.execute(
                "DELETE FROM coco_approvals WHERE id=? AND state=?",
                (approval_id, STATE_PENDING),
            )
            conn.commit()
        return {
            "ok": False,
            "status": "unreachable_approver",
            "error": str(sent.get("error") or sent.get("status") or "send failed"),
            "approver_actor_id": approver_actor_id,
        }

    message_id = str(sent.get("message_id") or "")
    correlation_id = str(sent.get("correlation_id") or "")
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE coco_approvals SET message_id=?, correlation_id=?, "
            "request_announced_at=COALESCE(request_announced_at, ?) WHERE id=?",
            (message_id, correlation_id, time.time(), approval_id),
        )
        row = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        conn.commit()

    _governance_audit(
        project_root,
        caller["session_id"],
        action_kind=AUDIT_REQUEST_KIND,
        target_entity=f"{subject_kind}:{subject_id}",
        payload={
            "approval_id": approval_id,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "requested_action": str(requested_action or ""),
            "requester_actor_id": requester_actor_id,
            "approver_actor_id": approver_actor_id,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "rationale": str(rationale or ""),
            "requested_at": now,
            "expires_at": now + ttl,
        },
    )
    return {"ok": True, "existing": False, **_row_dict(row)}


def decide_approval(
    project_root: Path,
    *,
    approval_id: str,
    decision: str,
    body: str = "",
) -> dict:
    """Record the ONE verdict on an open request. Identity is derived, not passed.

    `decision` is XAACP's own vocabulary -- `accepted` approves, `rejected`
    refuses, `blocked` says "cannot rule without X" and leaves the subject
    UNAPPROVED. `completed` is a valid XAACP decision and NOT a verdict here:
    "done" is not "approved".
    """
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return _invalid("approval_id is required")
    normalised = str(decision or "").strip().lower()

    caller = _caller_actor(project_root)
    if not caller:
        return {
            "ok": False,
            "status": "unidentified",
            "error": (
                "the deciding actor could not be derived; a verdict from nobody "
                "is not a verdict"
            ),
        }
    decider = caller["actor_id"]

    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        _expire_due(conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
    if row is None:
        return {"ok": False, "status": "not_found", "approval_id": approval_id}
    record = _row_dict(row)
    if record["state"] != STATE_PENDING:
        # The standing verdict is RETURNED, never replaced (guarantee 4): a
        # second ruling must not re-announce and must not overturn the first.
        return {
            "ok": False,
            "status": (
                "expired" if record["state"] == STATE_EXPIRED else "already_decided"
            ),
            **record,
        }

    # SELF-APPROVAL IS CHECKED BEFORE ANYTHING ELSE CONSUMES THE REQUEST, so a
    # refused self-verdict leaves the request exactly as open as it was.
    if decider == record["requester_actor_id"]:
        return {
            "ok": False,
            "status": "self_approval_refused",
            "error": "the requesting actor cannot be the deciding actor",
            **record,
            "decided_by_actor_id": decider,
        }
    if normalised not in _DECISION_TO_STATE:
        return {
            "ok": False,
            "status": "invalid",
            "error": (
                f"decision must be one of {sorted(_DECISION_TO_STATE)} -- "
                "'completed' is an XAACP decision but not an approval verdict"
            ),
            **record,
        }
    if not str(body or "").strip():
        # EMPIRE §XVI: the reason IS the point. A bare yes/no deletes the critique
        # this seat exists to produce, so a bodiless verdict is malformed and
        # (guarantee 6) leaves the subject unapproved.
        return {
            "ok": False,
            "status": "invalid",
            "error": "a verdict must carry a freetext rationale in `body`",
            **record,
        }
    if decider != record["approver_actor_id"]:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "only the addressed approver may rule on this request",
            **record,
            "decided_by_actor_id": decider,
        }

    # THE VERDICT IS THE XAACP REPLY. Recording it there first means the protocol
    # -- not a second copy of the rules here -- owns the terminal-once guarantee
    # and the addressed-responder check (one logic, one home).
    replied = conductor_comms.xaacp_reply(
        project_root,
        message_id=record["message_id"],
        session_id=record["session_id"],
        responder_actor_id=decider,
        decision=normalised,
        body=str(body),
    )
    if not replied.get("ok") or str(replied.get("status") or "") != normalised:
        return {
            "ok": False,
            "status": "verdict_not_recorded",
            "error": str(
                replied.get("error") or replied.get("status") or "reply failed"
            ),
            **record,
        }

    state = _DECISION_TO_STATE[normalised]
    decided_at = time.time()
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        updated = conn.execute(
            "UPDATE coco_approvals SET state=?, decision=?, decided_by_actor_id=?, "
            "decided_by_role=?, rationale=?, decided_at=?, "
            "verdict_announced_at=COALESCE(verdict_announced_at, ?) "
            "WHERE id=? AND state=?",
            (
                state,
                normalised,
                decider,
                # THE ROLE AS IT WAS, recorded once and never recomputed. A
                # consumer asking "was this BETA's verdict?" months and two
                # successions later cannot re-derive the answer from the actor
                # id, and must not have to: the fact is stamped here, from the
                # SAME derived route the decider's identity came from. An
                # unseated decider stamps '' -- which is not a role, and which
                # every role-checking consumer therefore refuses.
                caller.get("role", ""),
                str(body),
                decided_at,
                decided_at,
                approval_id,
                STATE_PENDING,
            ),
        ).rowcount
        after = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        conn.commit()
    if not updated:  # pragma: no cover - lost a decide race
        return {"ok": False, "status": "already_decided", **_row_dict(after)}

    _governance_audit(
        project_root,
        record["session_id"],
        action_kind=AUDIT_DECIDE_KIND,
        target_entity=f"{record['subject_kind']}:{record['subject_id']}",
        payload={
            "approval_id": approval_id,
            "subject_kind": record["subject_kind"],
            "subject_id": record["subject_id"],
            "requester_actor_id": record["requester_actor_id"],
            "approver_actor_id": record["approver_actor_id"],
            "decided_by_actor_id": decider,
            "decision": normalised,
            "decided_by_role": caller.get("role", ""),
            "state": state,
            "rationale": str(body),
            "message_id": record["message_id"],
            "correlation_id": record["correlation_id"],
            "decided_at": decided_at,
        },
    )
    return {"ok": True, "status": state, **_row_dict(after)}


def approval_status(
    project_root: Path,
    *,
    subject_kind: str,
    subject_id: str,
) -> dict:
    """The subject's current approval standing.

    A subject nobody asked about is `unknown`, which is NOT approved -- absence
    of a no is not a yes.
    """
    subject_kind = str(subject_kind or "").strip()
    subject_id = str(subject_id or "").strip()
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        _expire_due(conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM coco_approvals WHERE subject_kind=? AND subject_id=? "
            "ORDER BY requested_at DESC, rowid DESC LIMIT 1",
            (subject_kind, subject_id),
        ).fetchone()
    if row is None:
        return {
            "ok": True,
            "state": "unknown",
            "approved": False,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
        }
    record = _row_dict(row)
    return {"ok": True, "approved": record["state"] == STATE_APPROVED, **record}


def rebind_approver_to_seat_holder(
    project_root: Path,
    *,
    approval_id: str,
    approver_actor_id: str,
    approver_role: str,
) -> dict:
    """Follow a ROLE-held obligation to whoever holds the role NOW.

    WHY THIS IS NOT A BACK DOOR, AND WHY IT IS NECESSARY. `request_approval`
    stores a concrete `approver_actor_id`, which is the right thing for routing
    (XAACP addresses actors, not abstractions) and the wrong thing for authority
    (seats succeed). Without this, a pending obligation DIES the moment the BETA
    seat changes hands -- `decide_approval` would refuse the new seat-holder as
    "not the addressed approver", and the obligation would sit pending until it
    expired, for a reason nobody could act on.

    The two values MUST come from a LIVE authority resolver in the calling
    domain, never from a tool parameter. `backlog_admission` derives them from
    `conductor_comms._xaacp_resolve_target_route` and has already proven that
    the CALLER is that seat-holder before reaching here, so this cannot install
    an approver of the caller's choosing.

    AND IT STILL CANNOT CREATE A SELF-APPROVAL. A requester who later takes the
    seat would otherwise rebind its own obligation onto itself; that is refused
    here as well as in `decide_approval`, because the one guarantee that makes
    the whole ruling more than theatre should not depend on a single check.
    """
    approval_id = str(approval_id or "").strip()
    actor_id = str(approver_actor_id or "").strip()
    role = str(approver_role or "").strip()
    if not approval_id or not actor_id or not role:
        return _invalid("approval_id, approver_actor_id and approver_role required")
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        _expire_due(conn)
        row = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return {"ok": False, "status": "not_found", "approval_id": approval_id}
        record = _row_dict(row)
        if record["state"] != STATE_PENDING:
            conn.commit()
            return {"ok": False, "status": "already_decided", **record}
        if actor_id == record["requester_actor_id"]:
            conn.commit()
            return {
                "ok": False,
                "status": "self_approval_refused",
                "error": (
                    "the requesting actor cannot become the deciding actor by "
                    "taking the approving seat"
                ),
                **record,
            }
        moved = actor_id != record["approver_actor_id"]
        if moved:
            conn.execute(
                "UPDATE coco_approvals SET approver_actor_id=? WHERE id=? AND state=?",
                (actor_id, approval_id, STATE_PENDING),
            )
        conn.commit()

    if not moved:
        with conductor_comms._connect(project_root) as conn:
            after = conn.execute(
                "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
            ).fetchone()
        return {"ok": True, "rebound": False, **_row_dict(after)}

    # THE OBLIGATION IS RE-DELIVERED TO THE NEW HOLDER, and that is required
    # rather than tidy. `xaacp_reply` enforces its own addressed-responder rule
    # on the MESSAGE, so re-pointing only the approval row left the successor
    # holding an obligation it was authorized to decide and unable to record the
    # verdict ("forbidden") -- authority without a channel, which is the same
    # shape as a remedy with no door.
    #
    # This is ONE announcement for ONE new event ("you inherited this"), aimed at
    # an actor that has never been told, so it does not breach fire-once: the
    # previous holder was announced to once, and is not announced to again.
    sent = conductor_comms.xaacp_send(
        project_root,
        session_id=record["session_id"],
        # The ORIGINAL requester stays the sender. A successor must see who is
        # actually asking, and the obligation's identity must not drift to
        # whoever happened to trigger the redelivery.
        sender_actor_id=record["requester_actor_id"],
        target_actor_id=actor_id,
        lane_id=record["lane_id"],
        # DERIVED FROM THE STORED ROUTE, not claimed. `xaacp_send` forgives a
        # blank lane only for kinds that genuinely have none, so the redelivery
        # has to say which the original requester was -- and the row already
        # records that fact implicitly: a lane worker's route IS its lane, so a
        # stored lane means a worker and no stored lane means a laneless actor.
        sender_actor_kind="worker" if record["lane_id"] else "agent",
        message_kind=APPROVAL_REQUEST_KIND,
        body=(
            record["request_rationale"]
            or f"approval requested for {record['subject_kind']}:{record['subject_id']}"
        )
        + (
            f"\n\n[redelivered: the {role} seat changed hands while this "
            "obligation was pending; it is now yours to rule on]"
        ),
        metadata={
            "approval_id": approval_id,
            "subject_kind": record["subject_kind"],
            "subject_id": record["subject_id"],
            "requested_action": record["requested_action"],
            "redelivered_from_actor_id": record["approver_actor_id"],
        },
    )
    if not sent.get("ok"):
        # FAIL CLOSED, AND PUT THE ROW BACK. A rebind whose delivery failed would
        # leave an obligation addressed to an actor that cannot be reached and
        # whose predecessor can no longer answer -- unrulable by anyone.
        with conductor_comms._connect(project_root) as conn:
            conn.execute(
                "UPDATE coco_approvals SET approver_actor_id=? WHERE id=? AND state=?",
                (record["approver_actor_id"], approval_id, STATE_PENDING),
            )
            conn.commit()
        return {
            "ok": False,
            "status": "unreachable_approver",
            "error": str(sent.get("error") or sent.get("status") or "send failed"),
            "approver_actor_id": actor_id,
        }

    with conductor_comms._connect(project_root) as conn:
        conn.execute(
            "UPDATE coco_approvals SET message_id=?, correlation_id=? WHERE id=?",
            (
                str(sent.get("message_id") or ""),
                str(sent.get("correlation_id") or ""),
                approval_id,
            ),
        )
        after = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        conn.commit()
    _governance_audit(
        project_root,
        record["session_id"],
        action_kind=AUDIT_REBIND_KIND,
        target_entity=f"{record['subject_kind']}:{record['subject_id']}",
        payload={
            "approval_id": approval_id,
            "from_actor_id": record["approver_actor_id"],
            "to_actor_id": actor_id,
            "role": role,
            "message_id": str(sent.get("message_id") or ""),
        },
    )
    return {"ok": True, "rebound": True, **_row_dict(after)}


def claim_for_consumption(project_root: Path, *, approval_id: str) -> dict:
    """Take the EXCLUSIVE right to spend this approval once. Identity derived.

    THE EXACTLY-ONCE PROBLEM, AND WHY NEITHER OBVIOUS ANSWER WORKS. An approval
    that stays spendable forever lets a retry (or a second concurrent caller)
    turn one verdict into two trusted rows. An approval consumed as a one-shot
    token the moment it is read leaves the caller holding a REFUSAL for work
    that actually landed -- because the row can land and the response can still
    die on the way back. Both are wrong, and a client-side "did I already do
    this" memory cannot settle it: the authoritative store has to.

    So consumption is TWO writes around the work, and the store answers three
    distinct questions:

      * `consumed_ref` set     -> ALREADY DONE. Return that reference. The
                                  caller gets the row it created, not a denial,
                                  however many times it retries.
      * claim held by someone  -> IN FLIGHT. Refused, so two concurrent retries
        else, recently            cannot both proceed into a duplicate row.
      * no claim, or our own
        stale claim            -> ours. Proceed.

    `UPDATE ... WHERE claim_actor_id=''` is the atomic part: SQLite serializes
    it, so exactly one of two racing callers sees `rowcount == 1`.
    """
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return _invalid("approval_id is required")
    caller = _caller_actor(project_root)
    if not caller:
        return {
            "ok": False,
            "status": "unidentified",
            "error": "the consuming actor could not be derived",
        }
    now = time.time()
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        _expire_due(conn, now=now)
        row = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return {"ok": False, "status": "not_found", "approval_id": approval_id}
        record = _row_dict(row)
        if record["consumed_ref"] is not None:
            conn.commit()
            return {"ok": False, "status": "already_consumed", **record}
        if record["state"] != STATE_APPROVED:
            conn.commit()
            return {"ok": False, "status": "not_approved", **record}
        held_by = record["claim_actor_id"]
        claimed_at = record["claimed_at"] or 0.0
        if held_by and held_by != caller["actor_id"]:
            if now - float(claimed_at) < CLAIM_STALE_SECONDS:
                conn.commit()
                return {"ok": False, "status": "consumption_in_flight", **record}
            # A STALE FOREIGN CLAIM IS RECLAIMABLE, and saying so is the point:
            # a claim that could never be released would turn one crashed
            # consumer into a permanently unusable approval, which reads to
            # everyone afterwards as "approved but refused" with no remedy.
            logger.warning(
                "reclaiming a stale consumption claim on %s from %s",
                approval_id,
                held_by,
            )
        taken = conn.execute(
            "UPDATE coco_approvals SET claim_actor_id=?, claim_session_id=?, "
            "claimed_at=? WHERE id=? AND consumed_ref IS NULL AND state=? "
            "AND (claim_actor_id='' OR claim_actor_id=? OR claimed_at IS NULL "
            "OR claimed_at <= ?)",
            (
                caller["actor_id"],
                caller["session_id"],
                now,
                approval_id,
                STATE_APPROVED,
                caller["actor_id"],
                now - CLAIM_STALE_SECONDS,
            ),
        ).rowcount
        after = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        conn.commit()
    if not taken:
        return {"ok": False, "status": "consumption_in_flight", **_row_dict(after)}
    return {"ok": True, "status": "claimed", **_row_dict(after)}


def record_consumption(
    project_root: Path, *, approval_id: str, consumed_ref: str
) -> dict:
    """Close the exactly-once loop: this approval produced THIS reference.

    Written IMMEDIATELY after the work lands and before any response is
    serialized, which is what makes the "row landed, response died, caller
    retried" case answerable: by the time the caller can possibly retry, the
    store already knows what the approval produced.

    The write is conditional on `consumed_ref IS NULL`, so a second writer can
    never overwrite the first reference -- the FIRST row the approval produced
    is the one it produced, permanently.
    """
    approval_id = str(approval_id or "").strip()
    ref = str(consumed_ref or "").strip()
    if not approval_id or not ref:
        return _invalid("approval_id and consumed_ref are required")
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE coco_approvals SET consumed_ref=?, claim_actor_id='', "
            "claimed_at=NULL WHERE id=? AND consumed_ref IS NULL",
            (ref, approval_id),
        )
        after = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        conn.commit()
    if after is None:
        return {"ok": False, "status": "not_found", "approval_id": approval_id}
    record = _row_dict(after)
    _governance_audit(
        project_root,
        record["session_id"],
        action_kind=AUDIT_CONSUME_KIND,
        target_entity=f"{record['subject_kind']}:{record['subject_id']}",
        payload={
            "approval_id": approval_id,
            "consumed_ref": record["consumed_ref"],
            "requested_ref": ref,
            "subject_kind": record["subject_kind"],
            "subject_id": record["subject_id"],
        },
    )
    return {"ok": True, "status": "consumed", **record}


def release_consumption_claim(project_root: Path, *, approval_id: str) -> dict:
    """Hand an unfinished claim back, because the work did NOT land.

    The honest counterpart to `claim_for_consumption`: a consumer that claims and
    then fails must not leave the approval looking busy for the claim's whole
    stale window. Only an UNCONSUMED claim is released, so this can never undo a
    recorded consumption.
    """
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return _invalid("approval_id is required")
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        released = conn.execute(
            "UPDATE coco_approvals SET claim_actor_id='', claim_session_id='', "
            "claimed_at=NULL WHERE id=? AND consumed_ref IS NULL",
            (approval_id,),
        ).rowcount
        conn.commit()
    return {"ok": True, "released": bool(released), "approval_id": approval_id}


def approval_by_id(project_root: Path, approval_id: str) -> dict:
    """One obligation BY ID -- the shape a DIAGNOSIS door needs.

    `approval_status` answers "what is this subject's standing", which a consumer
    asks. A human (or a deciding seat) holding the id from a refusal asks the
    other question, and without this they would have to be handed a LIST to find
    their row in. A refusal must be bounded, so the read behind it must be too.

    Purpose is NOT filtered here on purpose: the id is already the narrowest key
    there is, and returning `subject_kind` is what lets a domain door REFUSE an
    obligation belonging to another domain rather than silently ruling on it.
    """
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return _invalid("approval_id is required")
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        _expire_due(conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM coco_approvals WHERE id=?", (approval_id,)
        ).fetchone()
    if row is None:
        return {"ok": False, "status": "not_found", "approval_id": approval_id}
    record = _row_dict(row)
    return {"ok": True, "approved": record["state"] == STATE_APPROVED, **record}


def is_approved(project_root: Path, subject_kind: str, subject_id: str) -> bool:
    """The one question a consumer asks, answered fail-closed.

    Anything that is not an explicit recorded `approved` -- unknown subject,
    pending request, rejection, block, expiry, or a read that raises -- is False.

    THE PURPOSE MUST MATCH, AND IT IS CHECKED BEFORE THE READ. `subject_kind` IS
    the purpose, and it is what makes one shared store safe for several domains:
    a `backlog_admission` verdict can never answer an `xaacp_seat` question
    because every row is keyed on the kind. The shape check below is the explicit
    half of that: a blank or malformed kind is not "any kind", it is NO kind, so
    it cannot be laundered into a wildcard by a caller that forgot to pass one.
    """
    if not _SUBJECT_KIND_RE.match(str(subject_kind or "").strip()):
        logger.warning(
            "approval read refused: %r is not a purpose (subject_kind)", subject_kind
        )
        return False
    try:
        return (
            approval_status(
                project_root, subject_kind=subject_kind, subject_id=subject_id
            )["state"]
            == STATE_APPROVED
        )
    except Exception:
        logger.exception("approval read failed; treating subject as UNAPPROVED")
        return False


def expire_due_approvals(project_root: Path, *, now: float | None = None) -> int:
    """Age out every overdue request and RECORD each one as expired.

    Guarantee 7: an aged-out request is never silently dropped and never
    auto-approved. The audit row is what makes the difference visible later --
    "nobody ruled on this in time" and "nobody ever asked" must not look alike.
    """
    with conductor_comms._connect(project_root) as conn:
        _ensure_schema(conn)
        expired = _expire_due(conn, now=now)
        conn.commit()
    if expired:
        _governance_audit(
            project_root,
            "",
            action_kind=AUDIT_EXPIRE_KIND,
            target_entity="coco_approvals",
            payload={"approval_ids": expired, "count": len(expired)},
        )
    return len(expired)
