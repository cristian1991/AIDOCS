"""Three-leg XAACP round-trip proof over the LOCAL transport (companion proof).

Legs: conductor <-> subagent, conductor <-> lane worker, plus the #1015
fail-closed attribution verdicts. Transport is the local XAACP store, NOT the
outer gate -- the gate-crossing proof is its sibling in this directory,
gate_roundtrip_proof.py, and that one is the evidence for anything about
authorization, scope or gate-composed identity. This file proves the store and
attribution semantics only.

Run: PYTHONPATH=mcp/server python mcp/scratch/gate-proofs/local_roundtrip_proof.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "mcp" / "server"))

from aidocs_mcp import conductor_comms as cc  # noqa: E402
from aidocs_mcp.managed_mode_service import ManagedModeService  # noqa: E402

SESSION = "roundtrip-proof"
HOST_KIND = "claude_code"
PARENT = "11111111-1111-4111-8111-111111111111"
AGENT_A = "a1a1a1a1a1a1a1a1"
AGENT_B = "b2b2b2b2b2b2b2b2"
LANE_HOST = "22222222-2222-4222-8222-222222222222"
LANE_ID = "proof-lane"

# The sender kind each actor reports. Set AIDOCS_PROOF_LEGACY_KINDS=1 to send the
# pre-#1007 vocabulary ("agent"), which isolates whether a failure is the message
# machinery or only the lane-less allow-set not knowing the new kind names.
_LEGACY = os.environ.get("AIDOCS_PROOF_LEGACY_KINDS", "") not in ("", "0")
KIND_CONDUCTOR = "agent" if _LEGACY else "conductor"
KIND_SUBAGENT = "agent" if _LEGACY else "subagent"

results: list[tuple[str, bool, str]] = []


def _ids(box) -> list[str]:
    """Message ids in an inbox payload.

    The stored row names it `id`; the SEND receipt names the same value
    `message_id`. Comparing the two names cost three false failures here before
    the mismatch was seen — the system was right and the harness was wrong.
    """
    return [
        str(m.get("id") or m.get("message_id") or "")
        for m in (box.get("messages") or [])
    ]


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="xaacp_proof_"))
    (root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    print(f"project_root={root}\n")

    mm = ManagedModeService()
    mm.set_mode(root, SESSION, source="proof", host_session_id=PARENT)
    mm.set_mode(root, SESSION, source="proof", host_session_id=LANE_HOST)

    # ── actors ────────────────────────────────────────────────────────────
    print("ACTORS")
    conductor = cc.xaacp_register_host_actor(
        root, host_session_id=PARENT, host_kind=HOST_KIND,
        actor_kind="conductor", source="proof",
    )
    check("conductor actor established", bool(conductor), conductor)

    sub_a = cc.xaacp_register_host_actor(
        root, host_session_id=PARENT, host_kind=HOST_KIND, actor_kind="subagent",
        host_agent_id=AGENT_A, source="proof",
    )
    check("subagent actor established", bool(sub_a), sub_a)
    check(
        "subagent actor DIFFERS from its parent conductor",
        bool(sub_a) and sub_a != conductor,
        f"conductor={conductor} subagent={sub_a}",
    )

    sub_b = cc.xaacp_register_host_actor(
        root, host_session_id=PARENT, host_kind=HOST_KIND, actor_kind="subagent",
        host_agent_id=AGENT_B, source="proof",
    )
    check("two sibling subagents are two actors", sub_b not in {"", sub_a, conductor}, sub_b)

    blank = cc.xaacp_register_host_actor(
        root, host_session_id=PARENT, host_kind=HOST_KIND, actor_kind="subagent",
        host_agent_id="", source="proof",
    )
    check("a blank agent_id mints NO subagent row", blank == "", repr(blank))

    lane_actor = cc.xaacp_register_host_actor(
        root, host_session_id=LANE_HOST, host_kind=HOST_KIND,
        actor_kind="conductor", source="proof-lane",
    )
    check("lane-worker host actor established", bool(lane_actor), lane_actor)

    # ── leg 1: conductor <-> subagent ─────────────────────────────────────
    print("\nLEG 1  conductor <-> subagent")
    sent = cc.xaacp_send(
        root, session_id=SESSION, sender_actor_id=conductor, target_actor_id=sub_a,
        lane_id="", message_kind="status", body="PING conductor->subagent",
        sender_actor_kind=KIND_CONDUCTOR,
    )
    check("conductor -> subagent send accepted", bool(sent.get("ok")), str(sent)[:160])
    mid = str(sent.get("message_id") or "")

    inbox = cc.xaacp_inbox(
        root, session_id=SESSION, target_actor_id=sub_a, lane_id="", unread_only=False,
        reader_actor_kind=KIND_SUBAGENT,
    )
    msgs = inbox.get("messages") or []
    check(
        "subagent inbox carries it",
        mid in _ids(inbox),
        f"{len(msgs)} message(s)",
    )

    rep = cc.xaacp_reply(
        root, message_id=mid, session_id=SESSION, responder_actor_id=sub_a,
        decision="accepted", body="PONG subagent->conductor",
    )
    check("subagent reply accepted", bool(rep.get("ok")), str(rep)[:160])

    back = cc.xaacp_send(
        root, session_id=SESSION, sender_actor_id=sub_a, target_actor_id=conductor,
        lane_id="", message_kind="status", body="PING subagent->conductor",
        sender_actor_kind=KIND_SUBAGENT,
    )
    check("subagent -> conductor send accepted", bool(back.get("ok")), str(back)[:160])
    cinbox = cc.xaacp_inbox(
        root, session_id=SESSION, target_actor_id=conductor, lane_id="", unread_only=False,
        reader_actor_kind=KIND_CONDUCTOR,
    )
    cmsgs = cinbox.get("messages") or []
    check(
        "conductor inbox carries the return leg",
        str(back.get("message_id")) in _ids(cinbox),
        f"{len(cmsgs)} message(s)",
    )

    # ── leg 1b: #1022 recipient identity is authoritative ─────────────────
    print("\nLEG 1b  #1022 lane-less visibility, and its two leakage negatives")
    upward = cc.xaacp_send(
        root, session_id=SESSION, sender_actor_id=sub_a, target_actor_id=conductor,
        lane_id=LANE_ID, message_kind="status", body="lane worker -> conductor",
        sender_actor_kind="lane_worker",
    )
    check("a lane worker's upward send is accepted", bool(upward.get("ok")), str(upward)[:160])
    laneless = cc.xaacp_inbox(
        root, session_id=SESSION, target_actor_id=conductor, lane_id="",
        unread_only=False, reader_actor_kind=KIND_CONDUCTOR,
    )
    check(
        "the conductor's LANE-LESS inbox contains the lane-stamped report",
        str(upward.get("message_id")) in _ids(laneless),
        f"{len(laneless.get('messages') or [])} message(s)",
    )
    narrowed = cc.xaacp_inbox(
        root, session_id=SESSION, target_actor_id=conductor, lane_id="unused-lane",
        unread_only=False, reader_actor_kind=KIND_CONDUCTOR,
    )
    check(
        "naming a lane still narrows to exactly that lane",
        str(upward.get("message_id")) not in _ids(narrowed),
        f"{len(narrowed.get('messages') or [])} message(s)",
    )
    to_other = cc.xaacp_send(
        root, session_id=SESSION, sender_actor_id=conductor, target_actor_id=sub_b,
        lane_id=LANE_ID, message_kind="status", body="addressed to sibling B",
        sender_actor_kind=KIND_CONDUCTOR,
    )
    other_session = cc.xaacp_send(
        root, session_id="roundtrip-proof-elsewhere", sender_actor_id=sub_a,
        target_actor_id=conductor, lane_id=LANE_ID, message_kind="status",
        body="same actor, other session", sender_actor_kind="lane_worker",
    )
    leaks = _ids(cc.xaacp_inbox(
        root, session_id=SESSION, target_actor_id=conductor, lane_id="",
        unread_only=False, reader_actor_kind=KIND_CONDUCTOR,
    ))
    check(
        "NEGATIVE cross-actor: a message addressed to a sibling is invisible",
        str(to_other.get("message_id")) not in leaks,
        str(to_other.get("message_id")),
    )
    check(
        "NEGATIVE cross-session: the same actor id in another session is invisible",
        str(other_session.get("message_id")) not in leaks,
        str(other_session.get("message_id")),
    )

    # ── leg 2: conductor <-> lane worker ──────────────────────────────────
    print("\nLEG 2  conductor <-> lane worker")
    lsent = cc.xaacp_send(
        root, session_id=SESSION, sender_actor_id=conductor, target_actor_id=lane_actor,
        lane_id="", message_kind="status", body="PING conductor->lane",
        sender_actor_kind=KIND_CONDUCTOR,
    )
    check("conductor -> lane send accepted", bool(lsent.get("ok")), str(lsent)[:160])
    linbox = cc.xaacp_inbox(
        root, session_id=SESSION, target_actor_id=lane_actor, lane_id="", unread_only=False,
        reader_actor_kind=KIND_CONDUCTOR,
    )
    lmsgs = linbox.get("messages") or []
    check(
        "lane inbox carries it",
        str(lsent.get("message_id")) in _ids(linbox),
        f"{len(lmsgs)} message(s)",
    )
    lrep = cc.xaacp_reply(
        root, message_id=str(lsent.get("message_id")), session_id=SESSION,
        responder_actor_id=lane_actor, decision="accepted", body="PONG lane->conductor",
    )
    check("lane reply accepted", bool(lrep.get("ok")), str(lrep)[:160])

    # ── leg 3: #1015 fail-closed attribution ──────────────────────────────
    print("\nLEG 3  fail-closed attribution (#1015)")
    args = {"mode": "xaacp_inbox", "session_id": SESSION}

    cc.xaacp_record_call_claim(
        root, host_session_id=PARENT, host_agent_id=AGENT_A,
        tool_name="mcp__aidocs__ai_msg", tool_input=args,
    )
    v = cc.xaacp_resolve_call_attribution(
        root, host_session_id=PARENT, tool_name="mcp__aidocs__ai_msg", arguments=dict(args),
    )
    check(
        "a claimed subagent call is ATTRIBUTED to it",
        v.get("status") == "attributed" and v.get("host_agent_id") == AGENT_A,
        str(v),
    )

    cc.xaacp_record_call_claim(
        root, host_session_id=PARENT, host_agent_id="",
        tool_name="mcp__aidocs__ai_msg", tool_input=args,
    )
    v = cc.xaacp_resolve_call_attribution(
        root, host_session_id=PARENT, tool_name="mcp__aidocs__ai_msg", arguments=dict(args),
    )
    check("a MAIN-THREAD call stays the conductor's", v.get("status") == "main_thread", str(v))

    v = cc.xaacp_resolve_call_attribution(
        root, host_session_id=PARENT, tool_name="mcp__aidocs__ai_msg",
        arguments={"mode": "xaacp_directory", "never": "claimed"},
    )
    check(
        "a claimed conversation's UNSEEN call is REFUSED, not the conductor",
        v.get("ok") is False and v.get("status") == "forbidden",
        str(v),
    )

    cc.xaacp_record_call_claim(
        root, host_session_id=PARENT, host_agent_id=AGENT_A,
        tool_name="mcp__aidocs__ai_msg", tool_input={"mode": "amb"},
    )
    cc.xaacp_record_call_claim(
        root, host_session_id=PARENT, host_agent_id=AGENT_B,
        tool_name="mcp__aidocs__ai_msg", tool_input={"mode": "amb"},
    )
    v = cc.xaacp_resolve_call_attribution(
        root, host_session_id=PARENT, tool_name="mcp__aidocs__ai_msg",
        arguments={"mode": "amb"},
    )
    check(
        "two siblings on one key are AMBIGUOUS, never guessed",
        v.get("ok") is False and v.get("error") == "subagent_attribution_ambiguous",
        str(v),
    )

    v = cc.xaacp_resolve_call_attribution(
        root, host_session_id="99999999-9999-4999-8999-999999999999",
        tool_name="mcp__aidocs__ai_msg", arguments=dict(args),
    )
    check(
        "a host that NEVER claimed (lane/gate/non-CC) is unchanged, not refused",
        v.get("ok") is True and v.get("status") == "unclaimed_host",
        str(v),
    )

    print("\n" + "=" * 62)
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
