"""Agent dispatch brief gate.

Pure evaluator for sub-agent dispatch briefs. It refuses exactly one thing: an
empty brief (malformed dispatch).

RESEARCH LANGUAGE IS NOT REFUSED. King ruling (operator, 2026-07-27), verbatim:

    "we unblock research words on conductor. a worker will always need to also
     look at the files, conductor gets a birds-eye view but the nitty and
     gritty should still be worker"

This module used to enforce the opposite: a table of research verbs
(investigate / audit / determine / look at / figure out / report findings ...)
refused any brief containing one, on the rule that "the conductor always reads +
researches; sub-agents are for writing code per concrete briefs (file path +
signature + decision already made)".

That division of labour cannot survive contact with the work. A worker cannot
write a correct edit to a file it is forbidden to look at; the honest brief for
"fix this dedupe bug" MUST tell the worker to read the seam. The conductor's job
is the bird's-eye view — which work, in what order, under what constraints — not
to pre-read every line so the worker can type blind.

Two rounds of false-positive tuning were built on the bad premise and retire
with it:
  * #433 "implementation grounding" — a research keyword was forgiven when the
    brief also carried a repo file path plus an imperative verb.
  * #488 "feature-name masking" — determiner-led noun adjuncts ("the audit
    trail", "the investigate lanes") were blanked before matching, because
    naming a FEATURE was being read as ordering research.
Both were epicycles correcting a rule that was wrong at its root.

The operator override goes too. Its refusal advertised "the operator can include
'delegate research' in their prompt", but that flag is minted only by
prompt_mutator on UserPromptSubmit — and mid-turn operator messages never fire
UPS. The escape hatch was therefore unreachable exactly when an operator would
reach for it: while replying to the refusal they had just been shown. That is
the false-affordance failure protected_file_runtime.py warns about in its own
comment — "an operator who stops believing refusals starts routing around them."

The PreToolUse hook calls this when `tool_name == "Task"` (Claude Code's
underlying name for what is surfaced as `Agent`).
"""

from __future__ import annotations

from typing import Any


def evaluate_agent_brief(brief: str) -> dict[str, Any]:
    """Decide whether an agent dispatch brief may be dispatched.

    Returns:
        {
            "allowed": bool,
            "reason": str,           # human-readable, suitable for tool-decision
            "matched_pattern": str,  # which pattern triggered (empty when allowed)
        }

    """
    if not brief or not brief.strip():
        return {
            "allowed": False,
            "reason": "Empty agent dispatch brief — refuse malformed dispatch.",
            "matched_pattern": "empty_brief",
        }

    return {
        "allowed": True,
        "reason": "Dispatch permitted.",
        "matched_pattern": "",
    }
