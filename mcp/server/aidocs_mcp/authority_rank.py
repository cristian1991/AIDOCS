"""Canonical authority-rank ladder — the resolution of finding DRT-16.

Empire ruling 2026-09-10. ONE signed total order replaces the previous mix of
narrower-wins RBAC rank ints, intersection-style session gates and token-scope-
only checks. This module is the ONLY place the ladder is defined.

    r-3   super_admin    the AIDOCS project main admin (the Empire himself)
    r-2   org_admin
    r-1   admin
    r0    operator       the tenant / project user
    r0a   ALPHA          the conducting seat
    r0b   BETA           the co-conducting seat
    r0.x.y.z...          lanes / subagents, arbitrarily deep

MORE NEGATIVE = MORE AUTHORITY. Comparison is arithmetic on ONE axis
(`Rank.authority_value`), so there is no second axis for a special case to hide
in.

## RANK IS NOT IDENTITY

    RANK     = position in the ladder. Answers "how much authority".
    IDENTITY = an AIDOCS-ASSIGNED id (XAACP actor id, lane id, subagent id).
               Answers "which actor".

`Rank` carries NO actor id, NO lane id, NO session id — `ActorIdentity` carries
those, and the two are derived separately (`_rank_from_route` reads identity and
returns a rank; it never writes one back). `authority_value` is computed from
`(tier, depth)` ALONE: the `path` segment labels are ladder POSITION tokens and
are arithmetically inert, so no label a caller could influence can change how
much authority a rank has. A rank is never a key, a handle or an actor
reference. Conflating the two is exactly how a caller ends up asserting its own
authority by naming itself.

Identity ASSIGNMENT is NOT owned here — this module consumes
`conductor_comms.xaacp_resolve_caller_route`, the same resolver `ai_msg` uses
for sender attribution, and never mints an id.

## THE SIX GUARDS (each one mutation-proven; see scratch/mutate_drt16_rank.py)

1. TOTAL ORDER — `authority_value` is a single int; `<=` is the whole test.
2. MONOTONE NON-INCREASING DESCENT — `descend()` is the only way to build a
   descendant and it can only ever LOWER authority. A descendant holding
   authority its ancestor lacks is privilege escalation by definition, so it is
   made structurally impossible rather than merely unchecked.
3. LETTER SUFFIXES ARE PEERS, NOT RUNGS — `r0a` and `r0b` have the SAME
   `authority_value`. Neither outranks the other. This is load-bearing: equal
   standing is what makes BETA's approval of ALPHA meaningful rather than
   self-approval, and what lets BETA send ALPHA's work back.
4. DERIVED, NEVER CLAIMED — no runtime entry point takes a rank, role, tier,
   seat, depth or actor parameter. Pinned structurally by an
   `inspect.signature` regression guard (precedent:
   `mcp/tests/coordination/test_approval_authority_1055.py`).
5. r-3 IS REPRESENTED BUT NON-MINTABLE — `SUPER_ADMIN` exists as a module
   constant so the Empire appears in audit as r-3 and not as the falsehood r-2,
   and so an unmodelled top rung does not get emulated by special cases. But
   `parse_rank("r-3")` REFUSES and no derivation path returns it: there is no
   in-band mint — not a tool argument, not config, not a grant, not a parse of
   untrusted input.
6. FAIL CLOSED ON UNKNOWN — an unparseable, absent, malformed or merely
   AMBIGUOUS rank raises `RankRefused` with a named reason. It is not r0, not
   the lowest rung, and not permissive. Unknown is never laundered into a
   default.

Plus the 2026-09-10 widening, which under the ladder is only monotonicity
restated: NO DESCENDANT MAY BE THE GRANTOR OF ITS OWN AUTHORITY, for ANY
capability. Self-grant and peer-grant both refuse; a strict ancestor is the only
lawful grantor, bounded by its own authority. See `decide_grant`.

## DEPTH

Unbounded in principle, bounded in practice at `MAX_DEPTH` = 64. Beyond it we
REFUSE rather than truncate: truncation silently promotes a deep descendant
toward its ancestor, which is the escalation this module exists to prevent.
64 is far past any observed nesting (measured lane trees are depth 1-2) and
leaves `TIER_STRIDE` headroom so a depth can never arithmetically wrap into the
next tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ALPHA",
    "ADMIN",
    "BETA",
    "GrantVerdict",
    "MAX_DEPTH",
    "OPERATOR",
    "ORG_ADMIN",
    "Rank",
    "RankRefused",
    "SEAT_ALPHA",
    "SEAT_BETA",
    "SUPER_ADMIN",
    "TIER_ADMIN",
    "TIER_OPERATOR",
    "TIER_ORG_ADMIN",
    "TIER_SUPER_ADMIN",
    "at_least_as_authoritative",
    "decide_grant",
    "descend",
    "format_rank",
    "is_strict_ancestor",
    "parse_rank",
    "require_descent_grant",
    "resolve_rank",
    "same_authority",
]

# ── The ladder ────────────────────────────────────────────────────────────
TIER_SUPER_ADMIN = -3
TIER_ORG_ADMIN = -2
TIER_ADMIN = -1
TIER_OPERATOR = 0

#: Peer seat letters. NOT rungs — `r0a` and `r0b` stand at EQUAL authority.
SEAT_ALPHA = "a"
SEAT_BETA = "b"
_SEAT_LETTERS = frozenset({SEAT_ALPHA, SEAT_BETA})

#: Refuse beyond this; never truncate. See the module docstring.
MAX_DEPTH = 64

#: Arithmetic separation between tiers. Must exceed MAX_DEPTH so no depth can
#: wrap a rank into a stronger tier.
TIER_STRIDE = 1024

_KNOWN_TIERS: dict[int, str] = {
    TIER_SUPER_ADMIN: "super_admin",
    TIER_ORG_ADMIN: "org_admin",
    TIER_ADMIN: "admin",
    TIER_OPERATOR: "operator",
}

#: r-3 is representable but NON-MINTABLE from any in-band input. parse_rank
#: refuses it and no derivation returns it.
_NON_MINTABLE_TIERS = frozenset({TIER_SUPER_ADMIN})

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RANK_RE = re.compile(
    r"^r(?P<tier>-?\d+)(?P<seat>[a-z])?(?:\.(?P<path>[A-Za-z0-9_.-]+))?$",
)


class RankRefused(Exception):
    """Fail-closed refusal with a NAMED reason.

    Every path that cannot establish a rank raises this. There is deliberately
    no "unknown rank" value and no default rank: the absence of an answer is an
    explicit refusal, never a permission.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True, slots=True)
class Rank:
    """A position on the ladder. Carries NO identity — see the module docstring.

    `path` holds opaque ladder-POSITION tokens for the descent chain (needed for
    ancestry, which is a prefix relation). They are arithmetically inert: only
    `(tier, depth)` feed `authority_value`.
    """

    tier: int
    seat: str = ""
    path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tier not in _KNOWN_TIERS:
            raise RankRefused("unknown_tier", repr(self.tier))
        if self.seat:
            if self.tier != TIER_OPERATOR:
                raise RankRefused("seat_on_non_operator_tier", self.seat)
            if self.seat not in _SEAT_LETTERS:
                raise RankRefused("unknown_seat_letter", self.seat)
        if self.path:
            if self.tier != TIER_OPERATOR:
                raise RankRefused("descent_on_non_operator_tier", str(self.path))
            for seg in self.path:
                if not isinstance(seg, str) or not _SEGMENT_RE.match(seg):
                    raise RankRefused("malformed_path_segment", repr(seg))
        if self.depth > MAX_DEPTH:
            raise RankRefused(
                "depth_limit_exceeded",
                f"depth {self.depth} > MAX_DEPTH {MAX_DEPTH}",
            )

    @property
    def depth(self) -> int:
        """Steps below the tier rung. A seat letter counts as one step."""
        return (1 if self.seat else 0) + len(self.path)

    @property
    def authority_value(self) -> int:
        """THE single comparison axis. More negative = more authority.

        Computed from (tier, depth) ONLY. The seat letter is absent on purpose:
        that is what makes ALPHA and BETA peers (guard 3). The path labels are
        absent on purpose: that is what keeps identity out of authority.
        """
        return self.tier * TIER_STRIDE + self.depth

    @property
    def text(self) -> str:
        return format_rank(self)

    @property
    def tier_name(self) -> str:
        return _KNOWN_TIERS[self.tier]

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.text


# ── The named rungs ───────────────────────────────────────────────────────
#: Represented so the Empire appears in audit as r-3 rather than the falsehood
#: r-2. NON-MINTABLE: nothing in-band produces this value.
SUPER_ADMIN = Rank(TIER_SUPER_ADMIN)
ORG_ADMIN = Rank(TIER_ORG_ADMIN)
ADMIN = Rank(TIER_ADMIN)
OPERATOR = Rank(TIER_OPERATOR)
ALPHA = Rank(TIER_OPERATOR, SEAT_ALPHA)
BETA = Rank(TIER_OPERATOR, SEAT_BETA)


def format_rank(rank: Rank) -> str:
    """Canonical text. One-way: r-3 formats but never parses back."""
    out = f"r{rank.tier}{rank.seat}"
    if rank.path:
        out += "." + ".".join(rank.path)
    return out


def parse_rank(text: str) -> Rank:
    """Parse canonical rank text. FAIL CLOSED, and r-3 is REFUSED.

    This is a reader for ranks AIDOCS itself wrote (audit rows, fixtures). It is
    NOT reachable from any tool argument — guard 4's signature regression test
    pins that. r-3 is refused here specifically so that no parse of untrusted
    input can ever mint super_admin (guard 5).
    """
    if not isinstance(text, str):
        raise RankRefused("rank_not_a_string", type(text).__name__)
    raw = text.strip()
    if not raw:
        raise RankRefused("rank_absent")
    m = _RANK_RE.match(raw)
    if not m:
        raise RankRefused("rank_unparseable", repr(raw))
    tier_txt = m.group("tier")
    if tier_txt != "0" and (tier_txt.startswith("0") or tier_txt.startswith("-0")):
        raise RankRefused("rank_unparseable", repr(raw))
    tier = int(tier_txt)
    if tier in _NON_MINTABLE_TIERS:
        raise RankRefused(
            "rank_not_mintable",
            f"{raw} is represented but may never be minted from input",
        )
    if tier not in _KNOWN_TIERS:
        raise RankRefused("unknown_tier", repr(raw))
    path_txt = m.group("path") or ""
    path = tuple(p for p in path_txt.split(".")) if path_txt else ()
    if path and any(not p for p in path):
        raise RankRefused("malformed_path_segment", repr(raw))
    return Rank(tier, m.group("seat") or "", path)


# ── Comparison (guards 1 and 3) ───────────────────────────────────────────
def at_least_as_authoritative(a: Rank, b: Rank) -> bool:
    """Is `a` at least as authoritative as `b`? ONE arithmetic comparison."""
    return a.authority_value <= b.authority_value


def same_authority(a: Rank, b: Rank) -> bool:
    """Equal standing. True for ALPHA vs BETA — they are peers, not rungs."""
    return a.authority_value == b.authority_value


# ── Descent (guard 2) ─────────────────────────────────────────────────────
def descend(parent: Rank, segment: str) -> Rank:
    """The ONLY constructor for a descendant, and it can only LOWER authority.

    Monotone non-increasing descent is enforced structurally: the child is built
    by APPENDING to the parent, so its depth is always parent.depth + 1 and its
    authority_value always strictly greater (weaker). The post-condition below is
    belt-and-braces — if it can ever fire, the arithmetic is broken and we refuse
    rather than hand back an escalated rank.
    """
    seg = str(segment or "").strip()
    if not seg:
        raise RankRefused("descent_segment_absent")
    if not _SEGMENT_RE.match(seg):
        raise RankRefused("malformed_path_segment", repr(seg))
    if parent.tier != TIER_OPERATOR:
        raise RankRefused(
            "descent_on_non_operator_tier",
            f"{parent.text} has no lane descent",
        )
    child = Rank(parent.tier, parent.seat, (*parent.path, seg))
    if child.authority_value <= parent.authority_value:
        raise RankRefused(
            "descent_would_not_lower_authority",
            f"{child.text} !< {parent.text}",
        )
    return child


def is_strict_ancestor(a: Rank, b: Rank) -> bool:
    """Is `a` a STRICT ancestor of `b`? Never true for a == b, never for peers.

    - A stronger TIER is an ancestor of every weaker tier (the tiers are a chain).
    - Within r0, ancestry is the prefix relation on (seat, path): r0 is an
      ancestor of r0a, r0b, r0.1 and r0a.1; r0a is an ancestor of r0a.1 but NOT
      of r0b or r0b.1 (peers are not ancestors — guard 3).
    """
    if a.tier != b.tier:
        return a.tier < b.tier
    if a.seat and a.seat != b.seat:
        return False
    if not a.seat and b.seat:
        return len(a.path) == 0
    if len(a.path) >= len(b.path):
        return False
    return b.path[: len(a.path)] == a.path


# ── The grant law (monotonicity restated) ─────────────────────────────────
@dataclass(frozen=True, slots=True)
class GrantVerdict:
    allowed: bool
    reason: str
    capability: str = ""
    grantor: str = ""
    grantee: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "capability": self.capability,
            "grantor_rank": self.grantor,
            "grantee_rank": self.grantee,
            "detail": self.detail,
        }


def decide_grant(grantor: Rank, grantee: Rank, capability: str) -> GrantVerdict:
    """PURE core of the 2026-09-10 law: may `grantor` widen `grantee`?

    "A lane must not be able to grant ANY tools to itself" — generalised, because
    a blocklist of tool names cannot carry this law: `ai_lane(action='grant_scope')`
    is a MODE of a tool lanes legitimately need, and "block this mode" is not
    expressible as a set of tool names. So the question asked here is not "is this
    tool forbidden to lanes" but "does this call raise the CALLER's own authority,
    and is the grantor a STRICT ANCESTOR of the grantee".

    THREE outcomes, and only one of them is yes:
      - self-grant  (grantor IS grantee)           → refuse
      - peer-grant  (equal standing, no ancestry)  → refuse
      - ancestor-grant                             → allow, bounded by the
                                                     ancestor's own authority

    This is NOT a separate policy bolted beside the ladder; it is monotonicity.
    A descendant that can grant itself authority is a descendant holding
    authority its ancestor never delegated.

    PURE: no I/O, no identity lookup. Both ranks must already have been DERIVED
    (`resolve_rank`) — never parsed from a caller's argument.
    """
    cap = str(capability or "").strip()
    if not cap:
        return GrantVerdict(
            allowed=False,
            reason="capability_absent",
            grantor=grantor.text,
            grantee=grantee.text,
            detail="a grant must name the capability it widens",
        )
    if grantor == grantee:
        return GrantVerdict(
            allowed=False,
            reason="self_grant_refused",
            capability=cap,
            grantor=grantor.text,
            grantee=grantee.text,
            detail="no actor may be the grantor of its own authority",
        )
    if not is_strict_ancestor(grantor, grantee):
        reason = (
            "peer_grant_refused"
            if same_authority(grantor, grantee)
            else "grantor_not_strict_ancestor"
        )
        return GrantVerdict(
            allowed=False,
            reason=reason,
            capability=cap,
            grantor=grantor.text,
            grantee=grantee.text,
            detail="only a strict ancestor may widen a descendant's authority",
        )
    # Ancestry already implies this arithmetically; asserted so a future change
    # to is_strict_ancestor cannot quietly admit an escalating grant.
    if not at_least_as_authoritative(grantor, grantee):
        return GrantVerdict(
            allowed=False,
            reason="grant_would_exceed_grantor",
            capability=cap,
            grantor=grantor.text,
            grantee=grantee.text,
            detail="a grant may only SPEND the grantor's authority, never exceed it",
        )
    return GrantVerdict(
        allowed=True,
        reason="ancestor_grant",
        capability=cap,
        grantor=grantor.text,
        grantee=grantee.text,
    )


# ── Derivation (guard 4 + guard 6) ────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """WHICH actor. Kept in its own type, separate from Rank, on purpose."""

    actor_id: str
    actor_kind: str
    lane_id: str = ""
    session_id: str = ""
    role: str = ""
    extras: dict[str, str] = field(default_factory=dict)


#: actor_kind values that positively evidence a CONDUCTING seat (r0a / r0b).
_SEAT_KINDS = frozenset({"seat", "conductor", "co_conductor"})
#: actor_kind values that positively evidence a DESCENDANT of a seat/operator.
_DESCENDANT_KINDS = frozenset({"lane_worker", "worker", "subagent"})
#: actor_kind values that positively evidence the tenant operator.
_OPERATOR_KINDS = frozenset({"operator"})
#: The xaacp_actors.actor_kind column DEFAULTS to 'agent' (conductor_comms.py:165),
#: so a row that was never stamped is INDISTINGUISHABLE from a real bound agent.
#: A default is not evidence, so 'agent' alone cannot establish a rank — it is
#: refused as ambiguous unless a lane id or host agent id corroborates it.
#: See the report: this default is the identity-side defect that must be fixed by
#: whoever owns id assignment; until then this module refuses rather than guesses.
_AMBIGUOUS_DEFAULT_KINDS = frozenset({"agent", ""})

_SEAT_BY_ROLE = {
    "conductor": SEAT_ALPHA,
    "alpha": SEAT_ALPHA,
    "co_conductor": SEAT_BETA,
    "coco": SEAT_BETA,
    "beta": SEAT_BETA,
}


def _rank_from_identity(ident: ActorIdentity) -> Rank:
    """Identity IN, rank OUT. Never the reverse, and never a default.

    POSITIVE EVIDENCE ONLY. The defect this replaces (access_gate.py:2047-2049)
    read the ABSENCE of a `current_lane_id` stamp as proof of conductor-ness, so
    any actor whose stamp failed to propagate received the conductor surface.
    Absence is never evidence here: an actor_kind this function does not
    positively recognise is REFUSED.
    """
    kind = str(ident.actor_kind or "").strip().lower()
    role = str(ident.role or "").strip().lower()
    lane = str(ident.lane_id or "").strip()

    if kind in _OPERATOR_KINDS:
        return OPERATOR
    if kind in _SEAT_KINDS:
        seat = _SEAT_BY_ROLE.get(role) or (
            SEAT_ALPHA if kind == "conductor" else _SEAT_BY_ROLE.get(kind, "")
        )
        if not seat:
            raise RankRefused(
                "seat_role_unresolved",
                f"actor_kind={kind!r} role={role!r} names no seat letter",
            )
        base = Rank(TIER_OPERATOR, seat)
        return descend(base, lane) if lane else base
    if kind in _DESCENDANT_KINDS:
        if not lane:
            raise RankRefused(
                "descendant_without_lane",
                f"actor_kind={kind!r} carries no lane id to place it on the ladder",
            )
        return descend(OPERATOR, lane)
    if kind in _AMBIGUOUS_DEFAULT_KINDS:
        raise RankRefused(
            "ambiguous_actor_kind_default",
            "actor_kind is the unstamped DEFAULT ('agent'); a default is not "
            "evidence of rank",
        )
    raise RankRefused("unknown_actor_kind", repr(kind))


def _caller_identity(project_root: Path) -> ActorIdentity:
    """DERIVED identity. There is deliberately no parameter for this (guard 4).

    Same resolver `ai_msg` and `approval_authority` use for sender attribution, so
    both sides of any comparison come from one source. An unresolvable caller is
    NOBODY — never a default actor.
    """
    try:
        from . import conductor_comms
    except Exception as exc:  # pragma: no cover - import-time only
        raise RankRefused("identity_resolver_unavailable", str(exc)) from exc
    try:
        route = conductor_comms.xaacp_resolve_caller_route(project_root)
    except Exception as exc:
        raise RankRefused("identity_resolver_failed", str(exc)) from exc
    if not isinstance(route, dict):
        raise RankRefused("identity_route_malformed", type(route).__name__)
    actor_id = str(route.get("actor_id") or "").strip()
    unmapped = str(getattr(conductor_comms, "MSG_ROLE_UNMAPPED", "") or "")
    if not actor_id or (unmapped and actor_id == unmapped):
        raise RankRefused(
            "unresolved_actor_identity",
            "the caller could not be identified; rank cannot be derived",
        )
    return ActorIdentity(
        actor_id=actor_id,
        actor_kind=str(route.get("actor_kind") or "").strip(),
        lane_id=str(route.get("lane_id") or "").strip(),
        session_id=str(route.get("session_id") or "").strip(),
        role=str(route.get("role") or "").strip(),
    )


def resolve_rank(project_root: Path) -> Rank:
    """The caller's rank, DERIVED from runtime identity. Raises on unknown.

    GUARD 4: this signature accepts NO rank, role, tier, seat, depth or actor
    parameter, and must never grow one — the instant a caller can name its own
    rank, every check above is decided by the caller.
    """
    return _rank_from_identity(_caller_identity(project_root))


def require_descent_grant(
    project_root: Path,
    *,
    grantee_lane_id: str,
    capability: str,
) -> GrantVerdict:
    """THE WIRING POINT. Refuse any grant whose grantor is not a strict ancestor.

    `grantee_lane_id` names WHICH lane is being widened — an identity handle, not
    an authority claim: it can only ever make this stricter, never looser,
    because the grantor's rank is derived and the grantee is built by DESCENDING
    from it.

    Returns a GrantVerdict. A refusal is a value, not an exception, so call sites
    can audit the named reason; identity failures still raise `RankRefused`
    because there is no verdict to report when the caller is nobody.
    """
    lane = str(grantee_lane_id or "").strip()
    if not lane:
        raise RankRefused("grantee_lane_absent", "a grant must name its grantee")
    grantor = resolve_rank(project_root)
    # A descendant naming its OWN lane, or a sibling's, resolves to a rank that
    # is not below the grantor — decide_grant then refuses it as self or peer.
    try:
        grantee = descend(grantor, lane)
    except RankRefused as exc:
        if exc.reason == "descent_on_non_operator_tier":
            # An admin rung (r-1/r-2) has no lane descent of its own; it grants
            # against the operator's lane tree.
            grantee = descend(OPERATOR, lane)
        else:
            raise
    ident_lane = _caller_identity(project_root).lane_id
    if ident_lane and ident_lane == lane:
        return GrantVerdict(
            allowed=False,
            reason="self_grant_refused",
            capability=str(capability or "").strip(),
            grantor=grantor.text,
            grantee=grantor.text,
            detail=f"lane {lane!r} may not widen itself",
        )
    if ident_lane:
        return GrantVerdict(
            allowed=False,
            reason="peer_grant_refused",
            capability=str(capability or "").strip(),
            grantor=grantor.text,
            grantee=grantee.text,
            detail=(
                f"caller is itself in lane {ident_lane!r}; a descendant may not "
                "widen a sibling"
            ),
        )
    return decide_grant(grantor, grantee, capability)
