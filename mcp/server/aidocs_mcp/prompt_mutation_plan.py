"""SEC-001 — plan-before-apply for prompt-side privilege mutations.

Doctrine (the courtroom, not the emergency net): prompt-side detectors
COLLECT their intended privilege writes into a plan instead of writing to
sqlite the moment they fire. Nothing touches the session-state "scroll
room" until the whole bundle is assembled and the prompt has survived the
block/route decision. Then the plan is applied atomically — every step
runs in order and the first failure propagates (no swallow, no partial
"success").

Two properties this buys over eager-write:

  1. Cleanliness — an empty plan performs zero writes. A clean prompt
     (no grant phrases) no longer stamps empty-list defaults into
     privilege columns just because the prompt path ran.
  2. Atomicity — apply() either runs the full bundle or raises on the
     first failing step. In production the caller wraps apply() so a
     mid-apply failure rolls back (SEC-002 snapshot/restore remains the
     emergency net for mutation families not yet routed through a plan).

The plan is deliberately tiny: it is an ordered list of zero-arg
callables. Each callable closes over the exact write it intends to make.
The plan does not interpret, reorder, or dedupe steps — that is the
caller's job when it decides what (if anything) to add.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

Step = Callable[[], None]


@dataclass
class PromptMutationPlan:
    """An ordered, all-or-nothing bundle of prompt-side privilege writes.

    Usage::

        plan = PromptMutationPlan()
        if granted_tools != current_tools:
            plan.add(lambda: qg.set_user_intent_tools(root, sid, granted))
        ...
        plan.apply()   # no-op when empty; raises on first failing step
    """

    _steps: list[Step] = field(default_factory=list)

    def add(self, step: Step) -> None:
        """Queue a zero-arg write callable. Does not execute it."""
        self._steps.append(step)

    def __len__(self) -> int:
        return len(self._steps)

    @property
    def is_empty(self) -> bool:
        """True when no steps were added — apply() will write nothing."""
        return not self._steps

    def apply(self) -> None:
        """Run every queued step in insertion order.

        The first step that raises aborts the run and the exception
        propagates unchanged — no swallowing, no partial-success return.
        The caller is responsible for rollback (production: the SEC-002
        snapshot/restore guard restores the pre-plan state). An empty
        plan is a no-op.
        """
        for step in self._steps:
            step()
