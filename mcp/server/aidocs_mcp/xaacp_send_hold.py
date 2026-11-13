"""XAACP send-time preflight, held sends, and the operator-approved send stamp.

Design: .MEMORY/sessions/ubermega/DESIGN-send-preflight-operator-stamp.md (v1,
Vesper's verdict recorded there), with the operator's 2026-11-12 amendment:
on Claude Code, the operator's OWN prompt naming the recipient ("send to
argus") is operator intent, captured by the UserPromptSubmit hook, and approves
the send without a further click.

FIRST SLICE (what this module does):
  * SEND-TIME JUDGE. The agent path of ``xaacp_send`` runs the same checker the
    ingress uses (``xaacp_ingress_verdict.judge_text_detail``: the operator
    preflight + the descriptive-report drop) on the SENDER-AUTHORED subject and
    body. Clean -> delivered as today. Hostile, ambiguous or unjudged -> NOT
    delivered: a held row is written (exact subject and body, their digest,
    sender, recipient, session, rule_ids, bounded spans, 30 min expiry) and the
    sender is told the rule_ids, the spans (<= 200 chars each), the held_id and
    the next steps.
  * DRY RUN. ``check_draft`` (ai_msg mode ``xaacp_send_check``) returns the
    same verdict and spans and writes nothing.
  * OPERATOR INTENT (Claude Code, local XAACP authority only). The UPS leg
    ``hook_pipeline._ups_xaacp_send_intent_leg`` -- the ONLY caller of
    :func:`record_operator_send_intents` -- parses the provenance-floored
    operator text of a new operator turn for an unambiguous "send ... to
    <recipient>", resolves the recipient to ONE exact actor, and records an
    intent bound to (session, the prompting host session, recipient, this
    turn), single-use, ``INTENT_TTL_SECONDS``. A new operator turn supersedes
    every earlier intent of that host session. The intent then approves
    exactly one held send from that window to that recipient, or the next
    send from that window to that recipient.
  * APPROVAL + DELIVERY. :func:`_approve_and_deliver` (reachable only from the
    two intent paths, pinned by AST test) claims the held row
    (held -> approved_pending_delivery, once), writes the approval row, and
    delivers the stored text with server-set metadata ``operator_approved``.
  * RECEIVE. :func:`verify_stamp` honours a stamp only when the approval row
    exists in THIS (authority) store, its held row was delivered as THIS
    message_id, and the digest of the delivered subject+body, the sender and
    the recipient all match. ``xaacp_ingress_verdict`` then shows the
    OPERATOR-APPROVED SEND notice instead of the hostile marker; anything else
    is judged as today plus a ``xaacp_stamp_invalid`` audit.

UNFORGEABILITY: an agent cannot set ``operator_*`` metadata on any send
(:func:`operator_metadata_refusal`, recursive, at the ai_msg boundary and the
dispatcher); no ai_msg mode records an intent or an approval; the intent is
read from the operator's prompt by the hook, never from tool arguments; a
gate-routed project records nothing (the gate cannot verify a locally attested
prompt -- the same reasoning as ``xaacp_ranks``).

OUT OF THIS SLICE: the ``xaacp_send_approval`` ask, the Claude Code PreToolUse
ask, the OpenAI-host input_required form, the gate dashboard held-sends list,
cross-project / cross-session approval, the durable cross-store outbox
(Vesper B1), and telling the sender when a held send expires.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Default life of a held send (design Q5: 30 min default).
HELD_TTL_SECONDS = 30 * 60
#: Life of an operator send intent captured from one prompt.
INTENT_TTL_SECONDS = 10 * 60
#: At most this many live held sends per sender (Vesper B4: bound the count).
MAX_LIVE_HELD_PER_SENDER = 20
#: Spans reported back to the sender.
MAX_SPANS = 5
SPAN_CHARS = 200

BLOCKED_BY = "send_preflight"
STAMP_KEY = "operator_approved"
#: The ONE approval channel of this slice: the operator's own prompt, captured
#: by the Claude Code UPS hook. Later channels (dashboard, OpenAI form) are each
#: one new caller plus one entry here.
CHANNEL_UPS_INTENT = "ups_intent"
APPROVAL_CHANNELS = frozenset({CHANNEL_UPS_INTENT})

HELD = "held"
APPROVED_PENDING_DELIVERY = "approved_pending_delivery"
DELIVERED = "delivered"
DELIVERY_FAILED = "delivery_failed"
EXPIRED = "expired"

INTENT_LIVE = "live"
INTENT_CONSUMED = "consumed"
INTENT_SUPERSEDED = "superseded"
INTENT_AMBIGUOUS = "ambiguous"


def _now() -> float:
    return time.time()


def send_digest(subject: str, body: str) -> str:
    """sha256 of the exact receiver-visible sender text (subject + body)."""
    canonical = json.dumps([str(subject or ""), str(body or "")], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()


def _judged_text(subject: str, body: str) -> str:
    # Same field order and joiner as the ingress judge (subject, then body).
    return "\n".join(t for t in (str(subject or ""), str(body or "")) if t)


def _spans(subject: str, body: str, spans: list[dict]) -> list[dict]:
    out: list[dict] = []
    for span in spans[:MAX_SPANS]:
        evidence = str(span.get("evidence") or "")[:SPAN_CHARS]
        field = "subject" if evidence and evidence in str(subject or "") else "body"
        out.append({"rule_id": str(span.get("rule_id") or ""), "field": field, "evidence": evidence})
    return out


def judge_draft(project_root: Path, *, subject: str, body: str) -> dict:
    """The send-time verdict on subject+body. Never raises; never writes."""
    # The preflight switch is read by the ingress module (one reader), which
    # answers preflight_off when the project switched the judge off.
    from .xaacp_ingress_verdict import judge_send_text

    status, rule_ids, spans = judge_send_text(_judged_text(subject, body), project_root=Path(project_root))
    return {"verdict": status, "rule_ids": list(rule_ids), "spans": _spans(subject, body, list(spans))}


def _would_hold(verdict: str) -> bool:
    from .xaacp_ingress_verdict import STATUS_AMBIGUOUS, STATUS_HOSTILE, STATUS_UNJUDGED

    return verdict in {STATUS_HOSTILE, STATUS_AMBIGUOUS, STATUS_UNJUDGED}


def check_draft(project_root: Path, *, subject: str, body: str) -> dict:
    """ai_msg mode='xaacp_send_check': the send-time verdict, nothing sent or stored."""
    if not str(body or "").strip() and not str(subject or "").strip():
        return {"ok": False, "status": "invalid", "error": "xaacp_send_check needs a subject and a body"}
    judged = judge_draft(project_root, subject=subject, body=body)
    hold = _would_hold(judged["verdict"])
    return {
        "ok": True,
        "status": "would_hold" if hold else "would_send",
        "sent": False,
        "would_hold": hold,
        "verdict": judged["verdict"],
        "rule_ids": judged["rule_ids"],
        "spans": judged["spans"],
        "digest": send_digest(subject, body),
        "note": (
            "dry run only: nothing was sent or stored. A send with this text would be held; "
            "reword the spans shown and check again"
            if hold
            else "dry run only: nothing was sent or stored. This text would be delivered"
        ),
    }


# ── metadata: the operator_* namespace is server-only ───────────────────────


def _operator_key_path(value: Any, path: str = "metadata", depth: int = 0) -> str:
    if depth > 16:
        return path  # absurdly deep metadata is refused, not walked forever
    if isinstance(value, dict):
        for key, inner in value.items():
            here = f"{path}.{key}"
            if str(key).strip().lower().startswith("operator_"):
                return here
            found = _operator_key_path(inner, here, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for i, inner in enumerate(value):
            found = _operator_key_path(inner, f"{path}[{i}]", depth + 1)
            if found:
                return found
    return ""


def operator_metadata_refusal(metadata: Any) -> dict | None:
    """Refuse any caller metadata key in the operator_* namespace, at any depth."""
    if metadata is None:
        return None
    found = _operator_key_path(metadata)
    if not found:
        return None
    return {
        "ok": False,
        "status": "forbidden",
        "field": found,
        "error": (
            "metadata keys in the operator_* namespace are set by the server only "
            "(an operator-approved send is stamped by the authority, never by the sender)"
        ),
    }


# ── store ───────────────────────────────────────────────────────────────────

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS xaacp_held_sends (
        held_id TEXT PRIMARY KEY,
        digest TEXT NOT NULL,
        session_id TEXT NOT NULL,
        source_session_id TEXT NOT NULL,
        sender_actor_id TEXT NOT NULL,
        sender_host_session_id TEXT NOT NULL DEFAULT '',
        target_actor_id TEXT NOT NULL,
        target_project_id TEXT NOT NULL DEFAULT '',
        message_kind TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        send_json TEXT NOT NULL,
        verdict TEXT NOT NULL,
        rule_ids TEXT NOT NULL DEFAULT '',
        spans_json TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        settled_at REAL,
        approvable INTEGER NOT NULL DEFAULT 0
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_xaacp_held_sends_route ON xaacp_held_sends "
        "(session_id, sender_host_session_id, target_actor_id, status)"
    ),
    """
    CREATE TABLE IF NOT EXISTS xaacp_send_approvals (
        held_id TEXT PRIMARY KEY,
        digest TEXT NOT NULL,
        session_id TEXT NOT NULL,
        sender_actor_id TEXT NOT NULL,
        target_actor_id TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        channel TEXT NOT NULL,
        intent_id TEXT NOT NULL DEFAULT '',
        approved_at REAL NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        delivered_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS xaacp_send_intents (
        intent_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        host_session_id TEXT NOT NULL,
        recipient_actor_id TEXT NOT NULL,
        recipient_label TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        turn_ref TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT NOT NULL,
        consumed_at REAL,
        consumed_by TEXT NOT NULL DEFAULT ''
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_xaacp_send_intents_route ON xaacp_send_intents "
        "(session_id, host_session_id, recipient_actor_id, status)"
    ),
)


def _conn(project_root: Path):
    from . import conductor_comms as cc

    conn = cc._connect(project_root)
    for stmt in _DDL:
        conn.execute(stmt)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(xaacp_held_sends)").fetchall()}
    if "approvable" not in cols:  # a store created before the column existed: nothing is approvable
        conn.execute("ALTER TABLE xaacp_held_sends ADD COLUMN approvable INTEGER NOT NULL DEFAULT 0")
    return conn


def _window_actor(project_root: Path, *, session_id: str, host_session_id: str) -> str:
    """The ONE actor the operator speaks to in this window, or "".

    The seat this host session holds, else the single non-subagent actor
    registered for it with no host agent id. A subagent shares its parent's
    host session but is never the window's actor (Argus B1 on a299a01be)."""
    sid = str(session_id or "").strip()
    host = str(host_session_id or "").strip()
    if not sid or not host:
        return ""
    try:
        from .operator_prompt_bridge import resolve_host_seat_actor

        seat = resolve_host_seat_actor(project_root, session_id=sid, host_session_id=host)
        if seat.get("status") == "held" and seat.get("actor_id"):
            return str(seat["actor_id"])
    except Exception:  # noqa: BLE001 -- no seat answer: fall through to the actor rows
        pass
    from . import conductor_comms as cc

    with cc._connect(project_root) as conn:
        rows = conn.execute(
            "SELECT actor_id FROM xaacp_actors WHERE session_id=? AND host_session_id=? "
            "AND COALESCE(host_agent_id, '')='' AND actor_kind NOT IN ('subagent', 'lane_worker')",
            (sid, host),
        ).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else ""


def _published_message_ids(conn, held_id: str) -> list[str]:
    """Ids of the XAACP messages in this store whose server-set stamp names
    ``held_id``. Agents cannot set operator_* metadata, so only the approval
    path writes such a row; there is at most one unless something is wrong."""
    out: list[str] = []
    for row in conn.execute(
        "SELECT id, metadata_json FROM messages WHERE direction='xaacp' AND instr(metadata_json, ?) > 0",
        (held_id,),
    ).fetchall():
        try:
            meta = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            continue
        stamp = meta.get(STAMP_KEY) if isinstance(meta, dict) else None
        if isinstance(stamp, dict) and str(stamp.get("held_id") or "") == held_id:
            out.append(str(row[0]))
    return out


def _store_exists(project_root: Path) -> bool:
    from . import conductor_comms as cc

    try:
        return Path(cc._db_path(project_root)).is_file()
    except Exception:  # noqa: BLE001
        return False


def _audit(project_root: Path, session_id: str, action_kind: str, target: str, payload: dict, kind: str) -> None:
    from . import conductor_comms as cc

    cc._audit_event(
        project_root, session_id, action_kind=action_kind, target_entity=target,
        payload=payload, event_kind=kind,
    )


# ── send time ───────────────────────────────────────────────────────────────


def preflight_send(
    project_root: Path,
    *,
    send: dict,
    sender_host_session_id: str,
    intent_eligible: bool,
) -> dict | None:
    """THE send-time gate for an agent's xaacp_send. ``None`` = deliver as today.

    ``send`` holds the exact ``conductor_comms.xaacp_send`` keyword arguments
    the dispatcher derived (sender, target, sessions, subject, body, ...).
    Any send from a window consumes a live intent of that window for that
    recipient (single use). A flagged send is held; if the consumed intent was
    live, it approves and delivers that held send once, stamped."""
    subject = str(send.get("subject") or "")
    body = str(send.get("body") or "")
    judged = judge_draft(project_root, subject=subject, body=body)
    same_session = str(send.get("source_session_id") or "") in {"", str(send.get("session_id") or "")}
    local_target = not str(send.get("target_project_id_requested") or "").strip()
    intent: dict | None = None
    if intent_eligible and same_session and local_target and sender_host_session_id:
        intent = _consume_live_intent(
            project_root,
            session_id=str(send.get("session_id") or ""),
            host_session_id=sender_host_session_id,
            recipient_actor_id=str(send.get("target_actor_id") or ""),
        )
    if not _would_hold(judged["verdict"]):
        return None
    held = _hold(
        project_root, send=send, judged=judged, sender_host_session_id=sender_host_session_id,
        approvable=bool(intent_eligible and same_session and local_target and sender_host_session_id),
    )
    if not held.get("held_id") or intent is None:
        return held
    out = _approve_and_deliver(
        project_root,
        held["held_id"],
        user_id=str(intent["user_id"]),
        channel=str(intent["channel"]),
        intent_id=str(intent["intent_id"]),
    )
    if out.get("ok"):
        out["send_preflight"] = {
            "verdict": judged["verdict"], "rule_ids": judged["rule_ids"], "held_id": held["held_id"],
        }
        return out
    return held


def _next_steps(held_id: str, recipient: str) -> list[str]:
    return [
        (
            "rewrite the flagged spans and send again; dry-run the draft first with "
            "ai_msg(mode='xaacp_send_check', subject=..., body=...), which sends nothing"
        ),
        (
            f"or ask the operator to approve held_id {held_id}: on Claude Code with a local XAACP "
            f"authority, the operator's own next prompt saying 'send to {recipient}' approves this "
            "one held send, once, within its expiry"
        ),
        "OpenAI-host and gate-dashboard approval of held sends are not built yet (second slice)",
    ]


def _hold(
    project_root: Path, *, send: dict, judged: dict, sender_host_session_id: str, approvable: bool = False
) -> dict:
    subject = str(send.get("subject") or "")
    body = str(send.get("body") or "")
    sender = str(send.get("sender_actor_id") or "")
    target = str(send.get("target_actor_id") or "")
    session_id = str(send.get("session_id") or "")
    source_session = str(send.get("source_session_id") or "") or session_id
    now = _now()
    held_id = secrets.token_hex(12)
    digest = send_digest(subject, body)
    stored = {k: v for k, v in send.items() if k not in {"subject", "body"}}
    refusal_base = {
        "ok": False,
        "blocked_by": BLOCKED_BY,
        "verdict": judged["verdict"],
        "rule_ids": judged["rule_ids"],
        "spans": judged["spans"],
        "sent": False,
    }
    with _conn(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute(
            "SELECT COUNT(*) FROM xaacp_held_sends WHERE sender_actor_id=? AND status=? AND expires_at>?",
            (sender, HELD, now),
        ).fetchone()[0]
        if int(live) >= MAX_LIVE_HELD_PER_SENDER:
            conn.rollback()
            return {
                **refusal_base,
                "status": "held_limit",
                "error": (
                    f"this send tripped the send-time check and you already have {live} held sends; "
                    "nothing was stored. Reword it (dry-run with mode='xaacp_send_check') and send again"
                ),
            }
        conn.execute(
            "INSERT INTO xaacp_held_sends (held_id, digest, session_id, source_session_id, "
            "sender_actor_id, sender_host_session_id, target_actor_id, target_project_id, message_kind, "
            "subject, body, send_json, verdict, rule_ids, spans_json, created_at, expires_at, status, "
            "approvable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                held_id, digest, session_id, source_session, sender, str(sender_host_session_id or ""),
                target, str(send.get("target_project_id_requested") or ""),
                str(send.get("message_kind") or ""), subject, body,
                json.dumps(stored, sort_keys=True, default=str), judged["verdict"],
                ",".join(judged["rule_ids"]), json.dumps(judged["spans"]), now,
                now + HELD_TTL_SECONDS, HELD, 1 if approvable else 0,
            ),
        )
        conn.commit()
    _audit(
        project_root, source_session, "xaacp_send_held", target,
        {
            "held_id": held_id, "digest": digest, "sender_actor_id": sender, "target_actor_id": target,
            "verdict": judged["verdict"], "rule_ids": judged["rule_ids"],
        },
        "stream_send",
    )
    recipient = str(send.get("target_label") or target)
    return {
        **refusal_base,
        "status": "held",
        "held_id": held_id,
        "digest": digest,
        "target_actor_id": target,
        "expires_at": now + HELD_TTL_SECONDS,
        "error": (
            "NOT DELIVERED: the send-time safety check flagged this message "
            f"({', '.join(judged['rule_ids'][:3]) or judged['verdict']}). It is held as {held_id}."
        ),
        "next_steps": _next_steps(held_id, recipient),
    }


def _consume_live_intent(project_root: Path, *, session_id: str, host_session_id: str, recipient_actor_id: str) -> dict | None:
    """Atomically consume the ONE live intent for (session, window, recipient)."""
    if not _store_exists(project_root):
        return None
    now = _now()
    with _conn(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT intent_id, user_id, channel FROM xaacp_send_intents WHERE session_id=? AND "
            "host_session_id=? AND recipient_actor_id=? AND status=? AND expires_at>? "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, host_session_id, recipient_actor_id, INTENT_LIVE, now),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        claimed = conn.execute(
            "UPDATE xaacp_send_intents SET status=?, consumed_at=?, consumed_by='send' "
            "WHERE intent_id=? AND status=?",
            (INTENT_CONSUMED, now, row["intent_id"], INTENT_LIVE),
        ).rowcount
        conn.commit()
    if claimed != 1:
        return None
    return {"intent_id": str(row["intent_id"]), "user_id": str(row["user_id"]), "channel": str(row["channel"])}


# ── approval + delivery ─────────────────────────────────────────────────────


def _approve_and_deliver(project_root: Path, held_id: str, *, user_id: str, channel: str, intent_id: str) -> dict:
    """Claim the held row ONCE, write the approval row, deliver once, stamped.

    Reachable only from the two operator-intent paths in this module (pinned by
    AST test); every value it trusts is read from the store, never an argument
    an agent controls."""
    from . import conductor_comms as cc

    if str(channel or "") not in APPROVAL_CHANNELS or not str(user_id or "").strip():
        return {"ok": False, "status": "not_attested", "held_id": held_id}
    now = _now()
    with _conn(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM xaacp_held_sends WHERE held_id=?", (held_id,)).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "status": "not_found", "held_id": held_id}
        if row["status"] == HELD and float(row["expires_at"]) <= now:
            conn.execute(
                "UPDATE xaacp_held_sends SET status=?, settled_at=? WHERE held_id=? AND status=?",
                (EXPIRED, now, held_id, HELD),
            )
            conn.commit()
            return {"ok": False, "status": EXPIRED, "held_id": held_id}
        if row["status"] != HELD:
            conn.rollback()
            return {"ok": False, "status": f"not_held:{row['status']}", "held_id": held_id}
        if send_digest(row["subject"], row["body"]) != row["digest"]:
            conn.rollback()
            return {"ok": False, "status": "digest_mismatch", "held_id": held_id}
        window_actor = _window_actor(
            project_root, session_id=str(row["session_id"]), host_session_id=str(row["sender_host_session_id"])
        )
        if not int(row["approvable"] or 0) or not window_actor or window_actor != str(row["sender_actor_id"]):
            # Held from a subagent (or any sender that is not the window's own
            # actor): the operator's prompt to the window never approves it.
            conn.rollback()
            return {"ok": False, "status": "not_approvable_by_prompt", "held_id": held_id}
        if str(row["target_project_id"] or "") or str(row["source_session_id"] or "") != str(row["session_id"] or ""):
            # Cross-project / cross-session approval is not in this slice.
            conn.rollback()
            return {"ok": False, "status": "not_approvable_here", "held_id": held_id}
        conn.execute(
            "UPDATE xaacp_held_sends SET status=?, settled_at=? WHERE held_id=? AND status=?",
            (APPROVED_PENDING_DELIVERY, now, held_id, HELD),
        )
        conn.execute(
            "INSERT INTO xaacp_send_approvals (held_id, digest, session_id, sender_actor_id, "
            "target_actor_id, approved_by, channel, intent_id, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                held_id, row["digest"], row["session_id"], row["sender_actor_id"], row["target_actor_id"],
                str(user_id), str(channel), str(intent_id or ""), now,
            ),
        )
        conn.commit()
    stored = json.loads(row["send_json"] or "{}")
    stamp = {
        "held_id": held_id,
        "approved_by": str(user_id),
        "channel": str(channel),
        "approved_at": now,
        "digest": str(row["digest"]),
    }
    metadata = dict(stored.get("metadata") or {})
    metadata[STAMP_KEY] = stamp
    kwargs = {
        k: v for k, v in stored.items()
        if k not in {"metadata", "target_label", "target_project_id_requested"}
    }
    try:
        sent = cc.xaacp_send(project_root, subject=row["subject"], body=row["body"], metadata=metadata, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- a failed delivery is recorded, never retried blind
        sent = {"ok": False, "status": f"error:{type(exc).__name__}"}
    message_id = str(sent.get("message_id") or "") if isinstance(sent, dict) else ""
    delivered = isinstance(sent, dict) and bool(sent.get("ok")) and bool(message_id)
    with _conn(project_root) as conn:
        if not delivered:
            published = _published_message_ids(conn, held_id)
            if len(published) == 1:
                # The send failed AFTER its row landed: that IS the delivery.
                message_id, delivered = published[0], True
                sent = {"ok": True, "message_id": message_id, "status": "delivered_after_error"}
        if delivered:
            conn.execute(
                "UPDATE xaacp_held_sends SET status=?, message_id=?, settled_at=? WHERE held_id=?",
                (DELIVERED, message_id, _now(), held_id),
            )
            conn.execute(
                "UPDATE xaacp_send_approvals SET message_id=?, delivered_at=? WHERE held_id=?",
                (message_id, _now(), held_id),
            )
        else:
            conn.execute(
                "UPDATE xaacp_held_sends SET status=?, settled_at=? WHERE held_id=?",
                (DELIVERY_FAILED, _now(), held_id),
            )
        conn.commit()
    _audit(
        project_root, str(row["source_session_id"] or row["session_id"]), "xaacp_send_operator_approved",
        str(row["target_actor_id"]),
        {
            "held_id": held_id, "digest": row["digest"], "approved_by": str(user_id), "channel": str(channel),
            "intent_id": str(intent_id or ""), "message_id": message_id,
            "delivered": delivered, "sender_actor_id": row["sender_actor_id"],
        },
        "stream_send",
    )
    if not delivered:
        return {
            "ok": False, "status": DELIVERY_FAILED, "held_id": held_id,
            "error": str((sent or {}).get("error") or (sent or {}).get("status") or "delivery failed"),
        }
    out = dict(sent)
    out[STAMP_KEY] = stamp
    out["held_id"] = held_id
    return out


# ── operator intent (Claude Code UPS) ───────────────────────────────────────


@dataclass(frozen=True)
class OperatorSendAttestation:
    """Who said "send to <recipient>", through which operator channel.

    Constructed only by the operator-channel caller (the UPS leg)."""

    channel: str
    user_id: str
    evidence: str = ""

    def problem(self) -> str:
        if str(self.channel or "").strip() not in APPROVAL_CHANNELS:
            return f"channel {self.channel!r} is not an operator send channel"
        if not str(self.user_id or "").strip():
            return "no authenticated operator user id"
        return ""


#: A COMMAND is the WHOLE operator prompt, one line of this exact shape
#: (Argus B1/B2 on 104fe32cf; whole-prompt rule after 948b0c845):
#: an optional "ok"/"please"/"go ahead and" lead-in, then "send [it|this|that|
#: the message] to <name>", an optional trailing "now"/"please", then only end
#: punctuation. Anything else in the sentence -- a condition after the command
#: (if/when/unless/once/after/before/until/only), a reporting or past-tense
#: verb (asked/said/sent), an example marker, a question mark, a second clause
#: -- leaves something the pattern does not consume, so nothing is recognised.
#: Unsure is never a command: the held-send fallback applies.
_COMMAND_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|alright|sure|yes|right|please|go\s+ahead\s+and)[\s,]+)*"
    r"send"
    r"(?:\s+(?:it|this|that|the\s+message))?"
    r"\s+to\s+@?([A-Za-z][A-Za-z0-9_-]{0,63})"
    r"(?:[\s,]+(?:now|please))?"
    r"\s*[.!]*$",
    re.IGNORECASE,
)
#: PROMPT-LEVEL VETO: a condition, a delay or a negation ANYWHERE in the
#: prompt makes every command in it uncertain -- "Send to Argus. Only after I
#: approve." splits into a clean command and a condition, and the condition
#: still governs it.
_VETO_RE = re.compile(
    r"\b(?:if|when|whenever|unless|once|after|before|until|till|only|wait|hold|later|tomorrow|"
    r"don'?t|do\s+not|not|never|no|cannot|can'?t|won'?t|shouldn'?t|stop|cancel)\b",
    re.IGNORECASE,
)
#: Any quote mark left in a sentence makes it uncertain (quoted or cited text).
_QUOTE_CHARS_RE = re.compile("['\"`\u2018\u2019\u201c\u201d]")
_NOT_A_NAME = frozenset({
    "the", "a", "an", "him", "her", "them", "it", "me", "you", "us", "everyone", "everybody", "all",
    "someone", "somebody", "anyone", "anybody", "whoever", "both", "each", "other", "others", "your",
    "my", "our", "their", "his", "its", "this", "that", "these", "those", "whom", "who", "which",
    "review", "reviewer", "reviewers", "peer", "peers", "now", "please",
})


def parse_operator_send_recipients(operator_text: str) -> list[str]:
    """The recipient when the WHOLE operator text is one send command.

    Argus on 948b0c845: any splitting into sentences or lines loses the
    context of earlier lines ("For example:" / "He wrote:" followed by a
    quoted command on its own line). So nothing is split. The operator text
    (already provenance-floored by the caller), trimmed of surrounding
    whitespace, must be exactly ONE line that ``_COMMAND_RE`` matches in full.
    Any remaining newline, any quote mark, and any condition, delay or
    negation (``_VETO_RE``) yields nothing. An operator who wants to approve a
    send says "send to argus" as its own prompt; anything else falls back to
    the held send."""
    text = str(operator_text or "").strip()
    if not text or "\n" in text or "\r" in text:
        return []
    if _QUOTE_CHARS_RE.search(text) or _VETO_RE.search(text):
        return []
    m = _COMMAND_RE.fullmatch(text)
    if m is None:
        return []
    name = m.group(1)
    if not name or name.lower() in _NOT_A_NAME:
        return []
    return [name]


def _supersede_turn(conn, *, session_id: str, host_session_id: str, now: float) -> None:
    conn.execute(
        "UPDATE xaacp_send_intents SET status=?, consumed_at=? WHERE session_id=? AND host_session_id=? AND status=?",
        (INTENT_SUPERSEDED, now, session_id, host_session_id, INTENT_LIVE),
    )


def _insert_intent(conn, *, session_id: str, host_session_id: str, recipient_actor_id: str, label: str,
                   attestation: OperatorSendAttestation, turn_ref: str, now: float) -> str:
    intent_id = secrets.token_hex(12)
    conn.execute(
        "INSERT INTO xaacp_send_intents (intent_id, session_id, host_session_id, recipient_actor_id, "
        "recipient_label, user_id, channel, turn_ref, created_at, expires_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent_id, session_id, host_session_id, recipient_actor_id, label, attestation.user_id,
            attestation.channel, turn_ref, now, now + INTENT_TTL_SECONDS, INTENT_LIVE,
        ),
    )
    return intent_id


def record_operator_send_intents(
    project_root: Path,
    *,
    session_id: str,
    host_session_id: str,
    recipients: list[str],
    attestation: OperatorSendAttestation,
) -> str:
    """THE ONE intent writer: a new operator turn in ``host_session_id``.

    Supersedes every earlier live intent of that window (the intent is bound
    to THIS turn), then records one single-use intent per recipient that
    resolves to exactly one actor, and applies it to the window's ONE held
    send to that recipient when there is exactly one. Returns an operator note
    ('' when there is nothing to say)."""
    from . import conductor_comms as cc

    sid = str(session_id or "").strip()
    host = str(host_session_id or "").strip()
    if not sid or not host:
        return ""
    names = [str(r).strip() for r in recipients or [] if str(r).strip()]
    now = _now()
    if not names:
        if _store_exists(project_root) and cc._xaacp_authority_refusal(project_root) is None:
            with _conn(project_root) as conn:
                _supersede_turn(conn, session_id=sid, host_session_id=host, now=now)
                conn.commit()
        return ""
    if not isinstance(attestation, OperatorSendAttestation) or attestation.problem():
        why = attestation.problem() if isinstance(attestation, OperatorSendAttestation) else "no attestation"
        return f"XAACP send intent not recorded: {why}."
    if cc._xaacp_authority_refusal(project_root) is not None:
        return (
            "XAACP send intent not recorded: this project's XAACP authority is the gate, which cannot "
            "verify a locally attested prompt. Held sends stay held; approve them through a gate-side "
            "operator channel (not built yet), or ask the sender to reword."
        )
    resolved: list[tuple[str, str]] = []
    notes: list[str] = []
    for name in names:
        res = cc.xaacp_resolve_send_target(project_root, session_id=sid, target_actor_id=name)
        actor = str(((res or {}).get("route") or {}).get("actor_id") or "").strip() if (res or {}).get("ok") else ""
        if not actor:
            notes.append(f"XAACP send intent for {name!r} not recorded: it does not name one known actor here.")
            continue
        if actor not in {a for a, _ in resolved}:
            resolved.append((actor, name))
    turn_ref = secrets.token_hex(8)
    intents: list[tuple[str, str, str]] = []
    with _conn(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _supersede_turn(conn, session_id=sid, host_session_id=host, now=now)
        for actor, label in resolved:
            iid = _insert_intent(
                conn, session_id=sid, host_session_id=host, recipient_actor_id=actor, label=label,
                attestation=attestation, turn_ref=turn_ref, now=now,
            )
            intents.append((iid, actor, label))
        conn.commit()
    window_actor = _window_actor(project_root, session_id=sid, host_session_id=host)
    for intent_id, actor, label in intents:
        with _conn(project_root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            held = conn.execute(
                "SELECT held_id FROM xaacp_held_sends WHERE session_id=? AND source_session_id=? AND "
                "sender_host_session_id=? AND sender_actor_id=? AND approvable=1 AND target_actor_id=? "
                "AND target_project_id='' AND status=? AND expires_at>? ORDER BY created_at",
                (sid, sid, host, window_actor or "\x00no-window-actor", actor, HELD, _now()),
            ).fetchall()
            if len(held) > 1:
                conn.execute(
                    "UPDATE xaacp_send_intents SET status=? WHERE intent_id=? AND status=?",
                    (INTENT_AMBIGUOUS, intent_id, INTENT_LIVE),
                )
                conn.commit()
                notes.append(
                    f"XAACP: more than one held send to {label} from this window "
                    f"({', '.join(h['held_id'] for h in held)}); none was approved. Ask the sender to "
                    "reword, or keep one."
                )
                continue
            if not held:
                conn.rollback()
                notes.append(
                    f"XAACP send intent recorded: the next send from this window to {label} in this turn "
                    f"is operator-approved once (expires in {INTENT_TTL_SECONDS // 60} min)."
                )
                continue
            held_id = str(held[0]["held_id"])
            claimed = conn.execute(
                "UPDATE xaacp_send_intents SET status=?, consumed_at=?, consumed_by=? WHERE intent_id=? AND status=?",
                (INTENT_CONSUMED, _now(), held_id, intent_id, INTENT_LIVE),
            ).rowcount
            conn.commit()
        if claimed != 1:
            continue
        out = _approve_and_deliver(
            project_root, held_id, user_id=attestation.user_id, channel=attestation.channel, intent_id=intent_id,
        )
        if out.get("ok"):
            notes.append(
                f"XAACP held send {held_id} to {label} was operator-approved and delivered once "
                f"(message {out.get('message_id')}), stamped OPERATOR-APPROVED SEND. Do not send it again."
            )
        else:
            notes.append(f"XAACP held send {held_id} to {label} was not delivered: {out.get('status')}.")
    return "\n".join(notes)


# ── receive ─────────────────────────────────────────────────────────────────


def verify_stamp(project_root: Path, entry: dict) -> tuple[bool, str, dict]:
    """(valid, reason, stamp) for an entry carrying ``metadata.operator_approved``.

    Valid ONLY when the authority's approval row exists for the stamp's
    held_id, its held send was delivered as THIS message id, and the digest of
    the entry's subject+body, its sender and its recipient all match."""
    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    stamp = meta.get(STAMP_KEY)
    if not isinstance(stamp, dict):
        return False, "malformed_stamp", {}
    held_id = str(stamp.get("held_id") or "").strip()
    if not held_id:
        return False, "no_held_id", {}
    if not _store_exists(project_root):
        return False, "no_approval_row", {}
    mid = str(entry.get("id") or "").strip()
    with _conn(project_root) as conn:
        row = conn.execute(
            "SELECT a.digest, a.sender_actor_id, a.target_actor_id, a.approved_by, a.channel, a.approved_at, "
            "a.message_id, h.status, h.message_id AS held_message_id FROM xaacp_send_approvals a "
            "JOIN xaacp_held_sends h ON h.held_id = a.held_id WHERE a.held_id=?",
            (held_id,),
        ).fetchone()
        published = _published_message_ids(conn, held_id) if row is not None else []
    if row is None:
        return False, "no_approval_row", {}
    digest = send_digest(str(entry.get("subject") or ""), str(entry.get("body") or ""))
    if row["status"] == DELIVERED:
        same_message = (
            bool(mid) and str(row["message_id"]) == mid and str(row["held_message_id"]) == mid
        )
    else:
        # Published but not yet finalized (a reader inside the send, or a
        # crash after the publish, Argus B2): valid only while no message id
        # is recorded and THIS is the one message carrying the stamp.
        same_message = (
            row["status"] in {APPROVED_PENDING_DELIVERY, DELIVERY_FAILED}
            and not str(row["message_id"] or "")
            and not str(row["held_message_id"] or "")
            and bool(mid)
            and published == [mid]
        )
    checks = (
        (same_message, "other_message"),
        (str(row["digest"]) == digest, "digest_mismatch"),
        (str(stamp.get("digest") or "") == digest, "stamp_digest_mismatch"),
        (str(row["sender_actor_id"]) == str(entry.get("sender_actor_id") or ""), "sender_mismatch"),
        (str(row["target_actor_id"]) == str(entry.get("target_actor_id") or ""), "recipient_mismatch"),
        (str(row["channel"]) in APPROVAL_CHANNELS, "unknown_channel"),
    )
    for ok, reason in checks:
        if not ok:
            return False, reason, {}
    return True, "", {
        "held_id": held_id,
        "approved_by": str(row["approved_by"]),
        "channel": str(row["channel"]),
        "approved_at": row["approved_at"],
    }
