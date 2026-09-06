"""Canonical mutation invocation + consumable confirmation.

Sealed design §3c (exact-confirm token binding) + §4a (one invocation service).

ONE service that every standalone direct wrapper routes mutations through,
so handler execution and confirmation authority cannot drift, and a confirm token
is consumable exactly once for the identical standalone invocation.

The confirm token is:
  * single-use (consumed on first success; replay denied),
  * TTL-bound (expiry denied),
  * cryptographically bound to the EXACT invocation identity —
    (operator, project, session, tool, normalized arguments, mutation intent) —
    so it cannot be replayed against a different operator/project/session/tool
    (cross-context) or a mutated argument set (mismatch).

All refusals are NAMED and fail-closed (default deny on any uncertainty).

This module is the AUTHORITY. Projections (the gate edit path, the local
wrappers) migrate onto it incrementally; it does not change the public registry
contract (`public_schema(spec)` derivation and aliases are untouched).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# #755/#756: the ONE canonical connect. All three sites were
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none set a
# single pragma (foreign_keys OFF, no busy_timeout).
# DURABILITY: AUDIT. These rows ARE the single-use confirmation, and the
# whole defence is that `consumed=1` is written BEFORE the mutation is
# allowed to proceed. A consumption mark a power cut un-does gives the
# token back and the replay then passes -- the same call as
# outer_gate_edit's confirmation mark.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

CONFIRM_TTL_SECONDS = 300

# ── Named, closed-set refusal reasons (fail-closed) ─────────────────
CONFIRM_REQUIRED = "confirm_required"  # missing token on a gated tool → phase one
CONFIRM_UNKNOWN = "confirm_unknown"  # token not found
CONFIRM_REPLAYED = "confirm_replayed"  # already consumed (single-use violation)
CONFIRM_EXPIRED = "confirm_expired"  # past TTL
CONFIRM_CROSS_CONTEXT = "confirm_cross_context"  # operator/project/session/tool differ
CONFIRM_MISMATCH = "confirm_mismatch"  # normalized args / intent differ (mutated op)


@dataclass(frozen=True)
class Refusal:
    blocked_by: str
    reason: str


def normalize_args(args: dict | None) -> str:
    """Canonical, stable serialization of host-supplied arguments. Stable key
    order + compact separators so the SAME logical operation hashes identically
    and any mutation changes the hash (binding defeats replay-against-mutation)."""
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


def _bind_hash(
    *,
    operator: str,
    project_id: str,
    session_id: str,
    tool: str,
    normalized_args: str,
    intent: str,
) -> str:
    h = hashlib.sha256()
    for part in (operator, project_id, session_id, tool, normalized_args, intent):
        h.update(b"\x1f")
        h.update(str(part).encode("utf-8"))
    return h.hexdigest()


class ConfirmStore:
    """SQLite-backed single-use confirm-token store bound to the full invocation
    identity. A token issued for one standalone tool call is consumable only for
    that identical binding; any difference fails closed with a named reason."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def init_db(self) -> None:
        with _canonical_connect(
            self.db_path, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS canonical_confirmations (
                    token TEXT PRIMARY KEY,
                    bind_hash TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0
                )
                """,
            )
            conn.commit()

    def issue(
        self,
        *,
        operator: str,
        project_id: str,
        session_id: str,
        tool: str,
        normalized_args: str,
        intent: str,
        now: float,
        ttl: int = CONFIRM_TTL_SECONDS,
    ) -> str:
        """Mint a single-use handle bound to the exact invocation identity."""
        self.init_db()
        token = "editconf-" + secrets.token_hex(4)
        bind = _bind_hash(
            operator=operator,
            project_id=project_id,
            session_id=session_id,
            tool=tool,
            normalized_args=normalized_args,
            intent=intent,
        )
        with _canonical_connect(
            self.db_path, durability=_Durability.AUDIT, row_factory=False
        ) as conn:
            conn.execute(
                "INSERT INTO canonical_confirmations (token, bind_hash, operator, "
                "project_id, session_id, tool, args_hash, intent, issued_at, "
                "expires_at, consumed) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (
                    token,
                    bind,
                    operator,
                    project_id,
                    session_id,
                    tool,
                    hashlib.sha256(normalized_args.encode()).hexdigest(),
                    intent,
                    now,
                    now + ttl,
                ),
            )
            conn.commit()
        return token

    def consume(
        self,
        token: str,
        *,
        operator: str,
        project_id: str,
        session_id: str,
        tool: str,
        normalized_args: str,
        intent: str,
        now: float,
    ) -> Refusal | None:
        """Single-use. Returns None (consumed OK) ONLY when the token exists, is
        unexpired, unconsumed, and matches the EXACT binding. Otherwise a NAMED
        fail-closed Refusal. The check order makes each failure attributable:
        unknown → replayed → expired → cross-context → mismatch."""
        if not token:
            return Refusal(CONFIRM_REQUIRED, "no confirm_token presented")
        self.init_db()
        with _canonical_connect(self.db_path, durability=_Durability.AUDIT) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM canonical_confirmations WHERE token=?",
                (token,),
            ).fetchone()
            if row is None:
                return Refusal(CONFIRM_UNKNOWN, "confirm_token not recognized")
            if int(row["consumed"]):
                return Refusal(CONFIRM_REPLAYED, "confirm_token already consumed (single-use)")
            if float(row["expires_at"]) <= now:
                return Refusal(CONFIRM_EXPIRED, "confirm_token expired")
            # Cross-context: identity fields differ (incl. a token reused across
            # a different tool / project / session / operator).
            if (
                str(row["operator"]) != operator
                or str(row["project_id"]) != project_id
                or str(row["session_id"]) != session_id
                or str(row["tool"]) != tool
            ):
                return Refusal(CONFIRM_CROSS_CONTEXT, "confirm_token bound to a different context")
            # Mismatch: same context but mutated arguments / intent.
            expected = _bind_hash(
                operator=operator,
                project_id=project_id,
                session_id=session_id,
                tool=tool,
                normalized_args=normalized_args,
                intent=intent,
            )
            if str(row["bind_hash"]) != expected:
                return Refusal(CONFIRM_MISMATCH, "confirm_token bound to different arguments/intent")
            # All good — consume single-use.
            conn.execute(
                "UPDATE canonical_confirmations SET consumed=1 WHERE token=?",
                (token,),
            )
            conn.commit()
            return None


def confirm_exchange(
    *,
    store: "ConfirmStore",
    tool: str,
    confirm_args: dict,
    intent: str,
    operator: str,
    project_id: str,
    context_key: str,
    presented: str,
    now: float,
    detail: str,
    summary: str,
) -> tuple[dict | None, str]:
    """THE mint/consume/binding primitive. Both handshakes are adapters over it.

    Returns ``(refusal_or_None, args_hash)``; an empty args_hash means nothing
    was confirmed.

    WHY A SHARED PRIMITIVE (local backlog 984). There are now TWO confirmation
    flavours over one token authority:

      STATIC  — `gate_confirm_handshake`: the tool carries a ToolSpec
                confirmation contract, intent comes from `build_confirm_phrase`,
                and the context slot is the TOKEN id (a session-binding tool
                moves its own session between phase one and phase two, so the
                session cannot be the key).
      DYNAMIC — `gate_bash_ask_handshake`: the tool is NOT globally
                confirmation-gated; only THIS invocation became `ask` after
                policy evaluation, so intent is the policy verdict and the
                context slot is a server-derived context hash.

    They differ ONLY in where intent and the context key come from. Everything
    that must not drift — normalization, the bind shape, mint, single-use
    consume, the refusal envelope — lives here once. Two careful copies would
    have drifted exactly as the gate's two entry points already did for the
    static case, which is the failure `gate_confirm_handshake`'s own docstring
    was written to record.

    `context_key` occupies ConfirmStore's `session_id` column. That column means
    "the stable context this handle is bound to", and what counts as stable
    differs by flavour — which is why it is a parameter rather than a lookup.
    """
    import hashlib as _hashlib

    norm = normalize_args(confirm_args)
    args_hash = _hashlib.sha256(norm.encode()).hexdigest()
    # NOTE: `bash_ask_args_hash` is this same computation over the same
    # already-stripped mapping, and `test_the_mint_and_the_check_agree` pins
    # them equal. It exists as its own function because the CHECK runs deep in
    # the cascade where `confirm_args` is not in scope, and a second inline
    # sha256 there is exactly how the two halves of a confirmation drift apart.
    bind = {
        "operator": operator,
        "project_id": project_id,
        "session_id": context_key,
        "tool": tool,
        "normalized_args": norm,
        "intent": intent,
    }

    if not presented:
        issued = store.issue(now=now, **bind)
        return (
            {
                "_error": "confirm_required",
                "_detail": detail,
                "action": tool,
                "confirm_token": issued,
                "summary": summary,
            },
            "",
        )

    refusal = store.consume(presented, now=now, **bind)
    if refusal is not None:
        return (
            {
                "_error": refusal.blocked_by,
                "_detail": f"{tool}: {refusal.reason}",
                "action": tool,
            },
            "",
        )
    return None, args_hash


def gate_confirm_handshake(
    *,
    store: "ConfirmStore",
    spec,
    tool: str,
    args: dict,
    operator: str,
    project_id: str,
    token_id: str,
    now: float,
) -> tuple[dict | None, str]:
    """#939 — THE confirm handshake for a GATE-surface tool. One home.

    Returns ``(refusal_or_None, args_hash)``; an empty args_hash means nothing
    was confirmed.

    WHY THIS IS A SHARED FUNCTION RATHER THAN TWO CAREFUL COPIES. The gate has
    TWO entry points that confirm — ``OuterGate`` (what the connector drives)
    and ``_ogt_pt_registry_dispatch`` (what the test suite drives) — and they
    had drifted into different contracts for the SAME spec. That is why every
    confirm test was green while ai_session(connect) was unusable in
    production: no test exercised the path production takes. Making two copies
    agree only postpones the next drift; doctrine XXII says give the logic one
    home, so a future change cannot land on one path and miss the other.

    THE INTENT IS BUILT FOR THE GATE SURFACE, ALWAYS. One caller previously
    built it with the default LOCAL surface, so a gate call was bound to the
    LOCAL phrase — a #380 per-surface violation, and enough on its own to make
    a handle minted by one path unconsumable by the other, since intent is
    folded into bind_hash.

    BOUND TO THE TOKEN, NEVER TO THE SESSION. A session-binding tool keyed on
    the current session moves its own key between phase one and phase two. The
    other caller resolved the session through a three-rung or-ladder
    (request.session_id or principal.session_id or token_id) — an identity
    fallback inside a confirm key, against "identity has no fallback".
    """
    from . import tool_interface as _ti

    _confirm_args = {
        k: v for k, v in args.items() if k not in ("confirm_token", "edit_confirmation_id")
    }
    # DELEGATES to `confirm_exchange` (local backlog 984) so mint/consume/binding
    # have ONE implementation. Every externally observable behaviour of #939 is
    # preserved BYTE-FOR-BYTE: the same excluded keys, the same normalization,
    # the same bind (token_id in the context slot), the same payload strings,
    # the same refusal envelope, the same (refusal, args_hash) contract.
    return confirm_exchange(
        store=store,
        tool=tool,
        confirm_args=_confirm_args,
        intent=_ti.build_confirm_phrase(spec, args, surface=_ti.SURFACE_GATE),
        operator=operator,
        project_id=project_id,
        context_key=token_id,
        presented=str(
            args.get("confirm_token") or args.get("edit_confirmation_id") or ""
        ),
        now=now,
        detail=(
            f"{tool} is a confirmation-gated action; ask the user, then "
            f"re-invoke with the exact server-issued confirm_token"
        ),
        summary=(
            f"About to invoke {tool} with args={_confirm_args!r}. "
            f"The user must confirm before this change."
        ),
    )


def gate_bash_ask_handshake(
    *,
    store: "ConfirmStore",
    tool: str,
    args: dict,
    operator: str,
    project_id: str,
    context_key: str,
    matched_rule: str,
    now: float,
) -> tuple[dict | None, str]:
    """DYNAMIC confirmation for a genuine `bash_policy_ask` (local backlog 984).

    `ai_run` is NOT globally confirmation-gated. Only THIS invocation became
    `ask` after policy evaluation, so there is no ToolSpec contract to read and
    no `build_confirm_phrase` to call — the intent IS the policy verdict. That
    is why this cannot go through `gate_confirm_handshake`, which requires a
    spec and binds its context slot to `token_id` for a reason that belongs to
    session-binding tools rather than to shell policy.

    WHAT THIS IS FOR. On a hook-capable host an `ask` becomes
    `permissionDecision=ask` and the operator answers through the host's native
    channel. WebMCP has no hook and no NLP round-trip, so its `ask` had no way
    to become approved: the refusal named "operator confirmation" on a surface
    that has no confirmation to give.

    WHAT IT IS NOT:
      * NOT a grant. One approved `sleep 10` never becomes
        `user_intent_bash_subcommands=["sleep"]`; the next `sleep` asks again.
      * NOT a freeze. Phase one blocks THE INVOCATION, not the agent — a
        mistaken command can simply be abandoned, and nothing else is blocked.
      * NOT a bypass. A consumed handle authorises re-entering the FULL
        enforcement cascade carrying proof that one exact ask was answered.
        Every harder refusal still runs and still wins.

    `context_key` MUST be SERVER-DERIVED (tenant, client, credential context,
    selected session) — never a value read out of the caller's own `ai_run`
    arguments, which are the thing being authorised.
    """
    # `confirm_token` is the caller's channel for phase two and must not be part
    # of what is bound, or phase two could never hash equal to phase one.
    _confirm_args = {k: v for k, v in args.items() if k != "confirm_token"}
    rule = str(matched_rule or "default.ask")
    return confirm_exchange(
        store=store,
        tool=tool,
        confirm_args=_confirm_args,
        # The verdict IS the intent. Folding the matched rule in means a handle
        # minted for one policy decision cannot satisfy a different one, even
        # for identical arguments.
        intent=f"bash_policy_ask:{rule}",
        operator=operator,
        project_id=project_id,
        context_key=context_key,
        presented=str(args.get("confirm_token") or ""),
        now=now,
        detail=(
            "this command is not on the allowlist, so it needs the operator's "
            "approval for THIS invocation. ASK THE USER; if they approve, "
            "re-invoke the EXACT same call with the confirm_token below. If the "
            "command was a mistake, simply do not use the token and continue — "
            "nothing is frozen and no other tool is blocked."
        ),
        summary=(
            f"About to run {tool} with args={_confirm_args!r} "
            f"(bash policy: {rule}). Approval covers THIS invocation only — it "
            f"does not allow the command in future."
        ),
    )


BASH_POLICY_ASK = "bash_policy_ask"


@dataclass(frozen=True)
class CascadeOutcome:
    """What the canonical cascade said, in the only terms this helper needs.

    An ADAPTER SHAPE, not a second verdict type. Each entry point maps its own
    concrete result (`ToolGateResult`, `EnforceResult`) into this, so the helper
    depends on neither and no import cycle is created. It deliberately carries
    nothing the sequence does not use — there is no room in it to smuggle an
    identity, a principal, or an authority hint upward.
    """

    allowed: bool = False
    needs_confirmation: bool = False
    blocked_by: str = ""
    matched_rule: str = ""
    reason: str = ""

    def is_genuine_ask(self) -> bool:
        """Only a real `bash_policy_ask` may become a confirmation challenge.

        Both halves are load-bearing. `blocked_by` alone would let any future
        refusal that happens to set `needs_confirmation` become click-through,
        and `needs_confirmation` alone would do the same for anything that
        borrows the string. A hard deny, a dangerous chain, a sensitive read, a
        control-plane refusal or a judge-locked primitive answers False here and
        can never be handed a token.
        """
        return bool(self.needs_confirmation) and self.blocked_by == BASH_POLICY_ASK

    def as_refusal(self, tool: str) -> dict:
        return {
            "_error": self.blocked_by or "blocked",
            "_detail": self.reason or f"{tool}: refused",
            "action": tool,
        }


def bash_ask_args_hash(args: dict) -> str:
    """The hash a bash-ask handle binds to, computed in ONE place.

    Mint and check MUST agree byte-for-byte or an approved command silently
    fails to be recognised as approved — which reads as "the confirmation did
    not work" and invites someone to loosen the comparison until it does. The
    `confirm_token` is excluded here for the same reason it is excluded at mint:
    it is the CHANNEL for phase two and cannot be part of what phase two must
    hash equal to.
    """
    import hashlib as _hashlib

    return _hashlib.sha256(
        normalize_args({k: v for k, v in (args or {}).items() if k != "confirm_token"}).encode()
    ).hexdigest()


def bash_ask_confirmation_satisfies(tool: str, args: dict) -> bool:
    """Is THIS exact invocation the one whose ask was approved at the boundary?

    Answers ONE question and grants nothing. A True here means a single rung —
    the `bash_policy_ask` — is satisfied for this invocation; every other rung
    still runs and still decides. It is deliberately not phrased as "is
    confirmed", because a name like that invites `if confirmed: return allow`,
    which is the bypass this whole design refuses.

    Returns False when nothing was confirmed at the boundary, which is the
    normal case on a hook-capable host: there, an `ask` becomes the host's
    native prompt and no gate confirmation is ever set. Local semantics are
    therefore untouched by construction rather than by a flag.
    """
    from .mcp_server_runtime_helpers import current_gate_confirmation

    proof = current_gate_confirmation()
    if not proof:
        return False
    proof_tool, proof_hash = proof
    if not proof_hash or str(proof_tool) != str(tool):
        return False
    return proof_hash == bash_ask_args_hash(args)


def bash_ask_two_pass(
    *,
    run_cascade,
    store: "ConfirmStore",
    tool: str,
    args: dict,
    operator: str,
    project_id: str,
    context_key: str,
    now: float,
) -> tuple[dict | None, str]:
    """THE two-pass `ask` sequence. One implementation, both entry points.

    Returns ``(refusal_or_None, args_hash)``. `None` means DISPATCH; a non-empty
    args_hash is the proof the second pass ran under.

    ORCHESTRATION ONLY. This function decides nothing about whether a command
    may run — `run_cascade` does, twice, and this reads its answers. That is why
    the cascade is INJECTED rather than imported: a helper that reached for
    enforcement itself would have to acquire it, and there is nowhere here to
    put the result of doing so. Outer Gate sequences the passes; the canonical
    cascade remains the authority in both.

    WHY THE SECOND PASS IS A FULL RE-ENTRY, not a dispatch on a valid token.
    A token answers ONE QUESTION — "do you approve this exact ask?" — and the
    answer stops being sufficient the moment anything stronger becomes true.
    Between the two phases the working tree, the policy, the freeze state and
    the judge locks can all change, and a design that trusts the handle would
    execute against a world that has since said no. So phase two re-enters
    enforcement FROM THE TOP carrying proof, and every harder refusal still runs
    and still wins. `test_a_stronger_refusal_in_phase_two_beats_a_valid_token`
    is that property, and it is the first test in the file for that reason.

    THE HANDLE IS SPENT EITHER WAY. `consume` happens before the second pass, so
    a refusal there does not refund it. Refunding would turn one approval into
    unlimited attempts against a moving target.

    Ordering is preserved exactly as it was: hard deny / dangerous chain /
    sensitive read / control plane / judge-locked primitive all outrank `ask`,
    because they are simply what the cascade returns and a non-ask refusal
    leaves here without ever reaching the store.
    """
    from .mcp_server_runtime_helpers import with_gate_confirmation

    first = run_cascade()
    if first.allowed:
        return None, ""
    if not first.is_genuine_ask():
        # Hard refusals are never click-through. Nothing is minted, nothing is
        # consumed, and the caller sees the refusal it would have seen before
        # this feature existed.
        return first.as_refusal(tool), ""

    refusal, args_hash = gate_bash_ask_handshake(
        store=store,
        tool=tool,
        args=args,
        operator=operator,
        project_id=project_id,
        context_key=context_key,
        matched_rule=first.matched_rule,
        now=now,
    )
    if refusal is not None:
        # Either the mint (phase one: `confirm_required`, carrying the handle)
        # or a consume refusal — unknown / replayed / expired / cross-context /
        # mismatch, in the store's own precedence.
        return refusal, ""

    with with_gate_confirmation(tool, args_hash):
        second = run_cascade()

    if second.allowed:
        return None, args_hash
    # Phase two refused. Return it VERBATIM and never re-mint: if the cascade
    # still answers `ask` here it did not honour the proof, and minting again
    # would loop the caller through handles forever while looking like progress.
    return second.as_refusal(tool), ""


def invoke(
    *,
    tool: str,
    public_args: dict,
    handler,
    operator: str,
    project_id: str,
    session_id: str,
    intent: str = "",
    requires_confirm: bool,
    confirm_token: str | None,
    store: ConfirmStore,
    now: float,
):
    """THE canonical invocation path used by standalone mutation wrappers —
    the only place "name + args → result" happens for a mutation.

    Two-phase for `requires_confirm` tools:
      phase one (no token)  → mint a token bound to THIS exact operation; return
                              {_error: confirm_required, confirm_token}.
      phase two (token)     → consume single-use, binding-checked; on success run
                              the handler exactly once; else NAMED refusal.
    Non-confirm tools run the handler directly. Every standalone wrapper reaches
    the same handler object, so confirmation semantics cannot drift by surface.
    """
    norm = normalize_args(public_args)
    if requires_confirm:
        if not confirm_token:
            token = store.issue(
                operator=operator,
                project_id=project_id,
                session_id=session_id,
                tool=tool,
                normalized_args=norm,
                intent=intent,
                now=now,
            )
            return {
                "_error": CONFIRM_REQUIRED,
                "_detail": f"{tool} is confirmation-gated; re-invoke with confirm_token",
                "tool": tool,
                "confirm_token": token,
            }
        refusal = store.consume(
            confirm_token,
            operator=operator,
            project_id=project_id,
            session_id=session_id,
            tool=tool,
            normalized_args=norm,
            intent=intent,
            now=now,
        )
        if refusal is not None:
            return {"_error": refusal.blocked_by, "_detail": refusal.reason, "tool": tool}
    return handler(**public_args)
