"""The verdict CLASS — one canonical three-rung ladder (backlog #571).

King rulings implemented here verbatim in intent:

  * law 526fcfdd — "freezes have strikes for violations, blocks should not
    issue strikes or freeze the agent, confirmable blocks should be
    agent-cancellable for free, at no strike cost. strikes and freezes are
    security-adjacent actions, blocks only enforce workflow."
  * law c09cba5b — three distinct outcomes, keyed on SCOPE / CAPABILITY /
    INTENT, **not** on destructiveness:
      rung 1 FREEZE — genuinely malicious (ssh exfil, credential exfil).
      rung 2 STRIKE — a real offence that is not an attack (reading outside
              the project root; an ssh connection NOT gated by detected
              user-intent). Strike, no freeze.
      rung 3 BLOCK  — "idiocies" (a ``timeout 900`` prefix, a bare ``pwd``,
              ``dotnet restore``). Straight block, NO strike, NO freeze,
              agent-cancellable for free.
    Plus the intent rule: if user-intent to access the server was ALREADY
    detected, the agent should not even be blocked.

Why this module exists: before it, ``block``/``strike``/``freeze`` was not a
representable value anywhere in the gate river. The nearest things in code
were four unrelated enumerations —
``destructive_taxonomy`` tiers (destructiveness), ``session_freeze_store``
kinds (who may CLEAR it), ``bash_policy`` ``matched_rule`` strings (which rule
FIRED), and the free-form ``ApprovalCard.risk_class`` (render-only). None
answers "was this an offence, and of what class". So every non-allow,
non-flat-deny outcome took the single available exit: mint a session freeze.

Two features are built directly on the answer, which is why the answer must be
explicit and persisted rather than inferred:

  (a) a conductor may clear only WORKFLOW-class freezes for its subagents,
      never security-class;
  (b) strike accrual attaches only to genuine offences.

If the class were not trustworthy off the record, (a) silently degrades to
"clear anything" and (b) to "strike for nothing". Therefore the reader
(:func:`is_security_class`) **fails closed**: an unknown, empty, or legacy
value is reported as security-class. A caller can only ever get
"this is safe for a conductor to clear" from a row that explicitly says so.
"""

from __future__ import annotations

# ── The ladder ───────────────────────────────────────────────────────

#: Rung 3. Workflow enforcement only. No strike, no freeze, the agent may
#: cancel for free. "Your command is not on the allow list", "drop the
#: ``timeout`` prefix", "that lifecycle step is out of order".
CLASS_WORKFLOW_BLOCK = "workflow_block"

#: Rung 2. A real offence that is not an attack. Accrues a strike. Does NOT
#: freeze. Reading outside the project root; an ssh/network reach that was
#: never gated by detected user-intent.
CLASS_SECURITY_STRIKE = "security_strike"

#: Rung 1. Genuinely malicious — exfiltration, credential theft, control-plane
#: tampering, catastrophic/evasive execution shapes. Freezes the agent.
CLASS_SECURITY_FREEZE = "security_freeze"

#: Not an offence at all. Reserved for the INTENT rule: when user-intent to
#: perform the action was already detected, the agent must not even be
#: blocked. Never persisted on a freeze row (there is no freeze to persist).
CLASS_NO_OFFENCE = "no_offence"

VALID_VERDICT_CLASSES: frozenset[str] = frozenset(
    {
        CLASS_WORKFLOW_BLOCK,
        CLASS_SECURITY_STRIKE,
        CLASS_SECURITY_FREEZE,
        CLASS_NO_OFFENCE,
    },
)

#: The security-adjacent rungs. Membership here is what authorises a strike
#: and what forbids a conductor-side clear.
SECURITY_CLASSES: frozenset[str] = frozenset(
    {CLASS_SECURITY_STRIKE, CLASS_SECURITY_FREEZE},
)


# ── The truthful reader ──────────────────────────────────────────────


def is_security_class(verdict_class: str | None) -> bool:
    """Is this verdict security-adjacent?

    FAILS CLOSED. Anything not explicitly one of the two non-security classes
    — including ``""``, ``None``, a legacy row minted before the column
    existed, and any unrecognised string — answers ``True``. A caller that
    wants to relax an enforcement action (clear a freeze, skip a strike) must
    have a row that positively declares itself non-security.
    """
    value = (verdict_class or "").strip().lower()
    if value == CLASS_WORKFLOW_BLOCK or value == CLASS_NO_OFFENCE:
        return False
    return True


def freeze_is_security_class(freeze: object) -> bool:
    """:func:`is_security_class` applied to a freeze record.

    Accepts a ``SessionFreeze`` (or anything with a ``verdict_class``
    attribute / key). A record with no such attribute at all is security-class
    by the same fail-closed rule.
    """
    value: object = ""
    if isinstance(freeze, dict):
        value = freeze.get("verdict_class", "")
    else:
        value = getattr(freeze, "verdict_class", "")
    return is_security_class(str(value or ""))


def issues_strike(verdict_class: str | None) -> bool:
    """Does this class accrue a strike? Only the two security rungs do."""
    return (verdict_class or "").strip().lower() in SECURITY_CLASSES


def freezes_agent(verdict_class: str | None) -> bool:
    """Does this class freeze the agent? Only rung 1 does.

    Fails closed on unknown values for the same reason as
    :func:`is_security_class`: an unclassified verdict must not be quietly
    downgraded out of the freeze it historically produced.
    """
    value = (verdict_class or "").strip().lower()
    if value in (CLASS_WORKFLOW_BLOCK, CLASS_NO_OFFENCE, CLASS_SECURITY_STRIKE):
        return False
    return True


def agent_cancellable(verdict_class: str | None) -> bool:
    """May the agent cancel this for free, at no strike cost?

    Rung 3 only (law 526fcfdd: "confirmable blocks should be
    agent-cancellable for free, at no strike cost").
    """
    return (verdict_class or "").strip().lower() == CLASS_WORKFLOW_BLOCK


def normalize(verdict_class: str | None) -> str:
    """Coerce to a known class, defaulting UNKNOWN to the freeze rung.

    Used at persistence boundaries so a stored value is always one of
    ``VALID_VERDICT_CLASSES`` and never a caller's typo.
    """
    value = (verdict_class or "").strip().lower()
    if value in VALID_VERDICT_CLASSES:
        return value
    return CLASS_SECURITY_FREEZE


# ── Classification, keyed on scope / capability / intent ─────────────

# Rung 3. WORKFLOW ONLY. These denial tiers describe "you did not follow the
# process", never "you tried to do something you must not". Keyed on
# CAPABILITY: none of them widens what the agent can reach.
_WORKFLOW_BLOCKED_BY: frozenset[str] = frozenset(
    {
        # bash_policy default=ask / default=block: the command is simply not
        # on the operator's allow list. `pwd`, `dotnet restore`, `timeout 900`.
        "bash_policy_ask",
        "bash_policy_block",
        # Lifecycle / process ordering.
        "lifecycle_preflight",
        "lifecycle_block",
        "test_retry_block",
        "no_managed_session",
        "workflow_block",
        # Tool-shape / routing corrections (use ai_find, not grep).
        "edit_redirect_blocked",
        "raw_tool_redirect",
    },
)

# Rung 3 by matched_rule (bash_policy's own vocabulary). Keyed on SCOPE: an
# allow-table miss says nothing about what the command reaches.
_WORKFLOW_MATCHED_RULES: frozenset[str] = frozenset(
    {"default.ask", "default.block"},
)

# Rung 2. A real offence, not an attack. Keyed on SCOPE (out-of-root reach)
# and on INTENT (a capability used without detected user-intent).
_STRIKE_BLOCKED_BY: frozenset[str] = frozenset(
    {
        "path_outside_root",
        "scope_violation",
        "project_scope",
        "sensitive_read",
        "raw_shell_block",
        "unknown_external",
        "egress_no_intent",
        "ssh_no_intent",
    },
)

# Rung 1. Genuinely malicious. Keyed on CAPABILITY (exfil channel, credential
# material, control-plane mutation) — the only rung where the ACT itself is
# the attack.
_FREEZE_BLOCKED_BY: frozenset[str] = frozenset(
    {
        "judge_credential_confirm",
        "malicious_forbidden",
        "hostile_operator_prompt",
        "repeated_security_violation",
        "anti_coup",
        "control_plane",
    },
)

# Rung-1 risk_class prefixes already emitted by the existing gates.
_FREEZE_RISK_PREFIXES: tuple[str, ...] = (
    "data_exfiltration",
    "control_plane",
    "lockdown",
)

# Rung-3 risk_class prefixes already emitted by the existing gates.
_WORKFLOW_RISK_PREFIXES: tuple[str, ...] = ("lifecycle",)

# Rung 3 by JUDGE RULE. A confirmable judge verdict reaches the ladder as
# ``blocked_by="judge_confirm_required"`` with the rule_id in ``matched_rule``.
# Most confirmable rules ARE destructive acts (rm -rf <abs>, git reset --hard,
# db drop) and belong on rung 1's confirm-and-freeze path. These do not: they
# are TOOL-SHAPE corrections — the judge's own recommendation for them is
# "use ai_create_file / ai_replace / ai_insert_lines / ai_batch_edit", i.e.
# the same redirect ``edit_redirect_blocked`` gives a raw Write. Keyed on
# CAPABILITY: a shell write to a versioned source file inside the project
# widens nothing the agent cannot already reach through the governed edit
# tools; the refusal exists so the edit-gate stack fires, not because the act
# is an attack.
#
# MEASURED 2026-08-25: ``printf '...' >> document-items.css`` fired
# SHELL_WRITE_SOURCE; with a destructive-intent token in the operator's prompt
# it became judge_confirm_required, which this ladder did not recognise, so it
# fell to rung 1 and the session froze under ``run_destructive`` — every
# subsequent call, reads included, refused until an operator decided it. The
# refusal was right; the FREEZE was the misclassification.
#
# SHELL_WRITE_SENSITIVE (.MEMORY/, .env, sqlite, .mcp.json) is deliberately
# NOT here: writing the control plane from a shell is a capability concern.
_WORKFLOW_JUDGE_RULES: frozenset[str] = frozenset({"SHELL_WRITE_SOURCE"})

#: The blocked_by tier the orchestrator emits for a confirmable judge verdict.
JUDGE_CONFIRM_BLOCKED_BY = "judge_confirm_required"


def classify(
    *,
    blocked_by: str = "",
    matched_rule: str = "",
    risk_class: str = "",
    user_intent_detected: bool = False,
) -> str:
    """Place a refusal on the ladder.

    The three axes, in the King's order of authority:

      * **INTENT** — if user-intent to perform the action was already
        detected, there is no offence and the agent must not even be blocked
        (:data:`CLASS_NO_OFFENCE`). This overrides the rung-2 intent-gated
        cases; it deliberately does NOT override rung 1, because detected
        intent to *ask* for something is not detected intent to exfiltrate.
      * **CAPABILITY** — does the act itself constitute the attack
        (exfil channel, credential material, control-plane mutation)? → rung 1.
      * **SCOPE** — does the act reach outside what the agent is scoped to
        (outside the project root, an ungated network reach)? → rung 2.

    Anything left over that is a pure process/allow-list outcome is rung 3.
    Anything left over that is NOT recognisably a process outcome falls to
    rung 1 — unrecognised refusals must not be silently demoted to "idiocy".

    Note the deliberate ordering: rung 1 is tested BEFORE the intent
    override so that a detected "please ssh to the box" cannot launder a
    credential-exfil verdict.
    """
    bb = (blocked_by or "").strip().lower()
    mr = (matched_rule or "").strip().lower()
    rc = (risk_class or "").strip().lower()

    # CAPABILITY — rung 1 first, and unconditionally.
    if bb in _FREEZE_BLOCKED_BY:
        return CLASS_SECURITY_FREEZE
    if rc.startswith(_FREEZE_RISK_PREFIXES):
        return CLASS_SECURITY_FREEZE

    # INTENT — detected user-intent removes the offence entirely.
    if user_intent_detected:
        return CLASS_NO_OFFENCE

    # SCOPE — rung 2.
    if bb in _STRIKE_BLOCKED_BY:
        return CLASS_SECURITY_STRIKE

    # WORKFLOW — rung 3.
    if bb in _WORKFLOW_BLOCKED_BY:
        return CLASS_WORKFLOW_BLOCK
    # A confirmable JUDGE verdict whose rule is a tool-shape redirect. Keyed
    # on BOTH the tier and the rule so a bare rule name can never demote a
    # different tier, and so every other judge rule keeps falling through to
    # the fail-closed rung below exactly as before.
    if bb == JUDGE_CONFIRM_BLOCKED_BY and mr.upper() in _WORKFLOW_JUDGE_RULES:
        return CLASS_WORKFLOW_BLOCK
    if mr in _WORKFLOW_MATCHED_RULES:
        return CLASS_WORKFLOW_BLOCK
    if rc.startswith(_WORKFLOW_RISK_PREFIXES):
        return CLASS_WORKFLOW_BLOCK

    # Unrecognised — fail closed onto the freeze rung, matching the
    # pre-#571 behaviour so this module can never WEAKEN an existing refusal.
    return CLASS_SECURITY_FREEZE


# ── The three OUTCOMES the ladder authorises ─────────────────────────

#: Detected user-intent already covered the action — law c09cba5b: "if
#: user-intent to access the server was ALREADY detected, the agent should not
#: even be blocked". Not a softened block; no block at all.
OUTCOME_ALLOW = "allow"

#: Rung 3. Workflow enforcement. No freeze row, no escalation request, no
#: strike, nothing latched, agent-cancellable for free.
OUTCOME_BLOCK = "block"

#: Rungs 1-2. Security-adjacent: the existing freeze/escalation pipeline.
OUTCOME_FREEZE = "freeze"


def outcome_for(
    *,
    blocked_by: str = "",
    matched_rule: str = "",
    risk_class: str = "",
    user_intent_detected: bool = False,
    verdict_class: str = "",
) -> tuple[str, str]:
    """Resolve (outcome, resolved_class) for a refusal in ONE place.

    This is the only function that decides whether a refusal becomes an ALLOW,
    a BLOCK, or a FREEZE. Every mint site calls it and branches on the answer,
    so the three-way routing cannot drift between the five sites the way the
    old single-exit pipeline did.

    ``verdict_class`` short-circuits classification when a caller already knows
    the class — the same value is then returned, so a site never classifies
    twice and the outcome is a pure function of its inputs (no re-derivation,
    no ordering dependence).

    Fail-closed, both directions, unchanged from the class contract:
      * an unrecognised refusal classifies to ``CLASS_SECURITY_FREEZE`` and so
        returns ``OUTCOME_FREEZE`` — the pre-#571 behaviour;
      * only the two positively-non-security classes can produce a non-freeze
        outcome, and ``CLASS_SECURITY_STRIKE`` still returns ``OUTCOME_FREEZE``
        here. Rung 2 keeps its existing enforcement in this pass: it is
        strike-bearing (``issues_strike`` is True) and demoting its ENFORCEMENT
        to a free block would weaken a live security control. Only rung 3 gets
        the new exit.
    """
    resolved = (
        normalize(verdict_class)
        if str(verdict_class or "").strip()
        else classify(
            blocked_by=blocked_by,
            matched_rule=matched_rule,
            risk_class=risk_class,
            user_intent_detected=user_intent_detected,
        )
    )
    if resolved == CLASS_NO_OFFENCE:
        return OUTCOME_ALLOW, resolved
    if resolved == CLASS_WORKFLOW_BLOCK:
        return OUTCOME_BLOCK, resolved
    return OUTCOME_FREEZE, resolved


def gate_permission_for(verdict_class: str | None) -> str:
    """The escalation permission a freeze of this class files under.

    ``run_destructive`` is preserved for the security rungs because the
    operator-approval lift looks that exact name up
    (``agent_orchestrator.check_live_grant_or_bubble``); renaming it there
    would break the approval chain. Rung 3 gets its own name so a workflow
    block can never consume a destructive-action approval.
    """
    if is_security_class(verdict_class):
        return "run_destructive"
    return "workflow_block"
