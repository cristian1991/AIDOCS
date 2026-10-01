"""Grant-registration judge — evaluates whether a sticky user-intent
grant is safe to register, and whether it needs operator confirmation.

Fires at grant birth (NOT per activation). Activation still goes
through the normal per-tool-call heuristic_judge.evaluate_tool_call
path for destructive-pattern detection.

Verdicts:
  REFUSE         — Register path hard-refused. Operator sees reason.
                   Examples: tier-2 bash with subcommand in
                   bash_policy._JUDGE_DENYLIST ('rm', 'sudo', etc.);
                   unknown tier; empty tool.
  REQUIRE_CONFIRM — Grant would register but needs explicit operator
                    sign-off via AskUserQuestion popup first.
                    Always for raw-tool tier-1, always for tier-2
                    (narrower scope still gets a review), and always
                    for Tier-1 grants when
                    `security.explicit_confirm_on_grant` is true.
  ALLOW          — Register without confirmation. Only non-raw tier-1
                    when the setting is off (frictionless path).

Design rationale: see backlog #15 Phase 3 plan. The judge runs at the
operator's grant request, not at the tool call — so it classifies
intent (is this a dangerous grant class?) not behavior (is this
specific invocation destructive?).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Raw-tool classes that ALWAYS require confirmation, regardless of
# `security.explicit_confirm_on_grant`. Bypassing this set silently
# would reintroduce the class of bugs backlog #15 was opened to fix.
RAW_TOOL_CLASSES: frozenset[str] = frozenset(
    {
        "bash",
        "grep",
        "read",
        "edit",
        "write",
        "multiedit",
        "patch",
        "apply_patch",
    },
)


@dataclass(frozen=True)
class GrantJudgeVerdict:
    decision: str  # "refuse" | "require_confirm" | "allow"
    reason: str  # Human-readable rationale. Shown in popup or error.
    tier: int
    tool: str
    subcommand: str | None


def evaluate_grant_registration(
    *,
    tier: int,
    tool: str,
    subcommand: str | None = None,
    phrase: str = "",
    project_root: Path | None = None,
) -> GrantJudgeVerdict:
    """Judge a sticky grant registration request.

    Pure function (no side effects). Caller decides what to do with
    the verdict — refuse means don't register, require_confirm means
    hand off to confirmation flow, allow means register directly.
    """
    tool_norm = (tool or "").strip().lower()
    sub_norm = (subcommand or "").strip().lower() or None

    # ── Structural refusals (tier/shape errors) ─────────────────
    if tier not in (1, 2):
        return GrantJudgeVerdict(
            decision="refuse",
            reason=f"tier must be 1 or 2, got {tier!r}",
            tier=tier,
            tool=tool_norm,
            subcommand=sub_norm,
        )
    if not tool_norm:
        return GrantJudgeVerdict(
            decision="refuse",
            reason="tool required",
            tier=tier,
            tool=tool_norm,
            subcommand=sub_norm,
        )
    if tier == 2 and not sub_norm:
        return GrantJudgeVerdict(
            decision="refuse",
            reason="tier 2 requires a subcommand (e.g. 'opencode' in bash)",
            tier=tier,
            tool=tool_norm,
            subcommand=sub_norm,
        )
    if tier == 1 and sub_norm:
        return GrantJudgeVerdict(
            decision="refuse",
            reason="tier 1 must not carry a subcommand",
            tier=tier,
            tool=tool_norm,
            subcommand=sub_norm,
        )

    # ── Tier 2 bash: bash_policy._JUDGE_DENYLIST trumps any grant ──
    # Hardcoded destructive primitives (rm, sudo, dd, chmod, ...) cannot
    # be sticky-granted for a session no matter how the phrase is worded.
    if tier == 2 and tool_norm == "bash" and sub_norm:
        try:
            from .bash_policy import _JUDGE_DENYLIST
        except Exception:
            _JUDGE_DENYLIST = frozenset()
        if sub_norm in _JUDGE_DENYLIST:
            return GrantJudgeVerdict(
                decision="refuse",
                reason=(
                    f"subcommand `{sub_norm}` is in the hardcoded "
                    "destructive denylist — cannot sticky-grant. "
                    "See .MEMORY/rules/security.md for the denylist "
                    "rationale."
                ),
                tier=tier,
                tool=tool_norm,
                subcommand=sub_norm,
            )

    # ── Tier 2: ALWAYS confirm (narrower scope still needs review) ──
    if tier == 2:
        return GrantJudgeVerdict(
            decision="require_confirm",
            reason=(
                f"Grant `{tool_norm}` for subcommand `{sub_norm}` "
                f"for the whole session? (phrase: {phrase[:120]!r})"
            ),
            tier=tier,
            tool=tool_norm,
            subcommand=sub_norm,
        )

    # ── Tier 1 raw-tool class: ALWAYS confirm, setting can't disable ──
    if tool_norm in RAW_TOOL_CLASSES:
        return GrantJudgeVerdict(
            decision="require_confirm",
            reason=(
                f"Grant RAW `{tool_norm}` for the whole session? "
                f"This bypasses ai_run audit and scope checks. "
                f"(phrase: {phrase[:120]!r})"
            ),
            tier=tier,
            tool=tool_norm,
            subcommand=sub_norm,
        )

    # ── Tier 1 non-raw: respect operator setting ───────────────────
    if project_root is not None:
        try:
            from .config import get_setting

            explicit = bool(
                get_setting(
                    "security.explicit_confirm_on_grant",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            # FAIL CLOSED (2026-06-11, co-co): if the confirm-on-grant setting
            # can't be read, require confirmation rather than auto-registering.
            explicit = True
        if explicit:
            return GrantJudgeVerdict(
                decision="require_confirm",
                reason=(f"Grant `{tool_norm}` sticky for the session? (phrase: {phrase[:120]!r})"),
                tier=tier,
                tool=tool_norm,
                subcommand=sub_norm,
            )

    return GrantJudgeVerdict(
        decision="allow",
        reason=f"non-raw tier-1 grant `{tool_norm}` — auto-registered",
        tier=tier,
        tool=tool_norm,
        subcommand=sub_norm,
    )
