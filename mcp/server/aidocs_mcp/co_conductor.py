"""Co-conductor — second-tier sovereign aid (king doctrine 2026-05-01).

Castle hierarchy (from #105):
- King          : the operator. Rules by judgment.
- Conductor     : crowned cerberus head. Strikes at king's will.
- Co-Conductor  : black wolf head. Focus + alert. Backs the conductor when
                  "the tough gets heavy." Reviews conductor's own tool
                  requests (parallel to conductor reviewing Expert/Worker
                  requests). Helps rule the kingdom in king's absence.
- Lane Experts  : white wolf head. No will of their own; serve the strike.

Status: DEFERRED. The king has chosen to ship Worker/Expert split first;
co-conductor lands when:
  (a) Chain-of-thought visibility surfaces (so co-co can review
      conductor's intent the way conductor reviews Expert intent — see
      backlog #111).
  (b) Workers/Experts are stable and battle-tested.
  (c) The dispatch + review machinery (#107, #108) is shipped, so co-co
      reuses the same protocol.

This module is the placeholder. All methods raise NotImplementedError
with TODO markers tagged co-conductor-deferred so an audit grep finds
them. When the dependencies above land, the methods fill in.
"""

from __future__ import annotations

from typing import Any


# TODO(co-conductor-deferred, king 2026-05-01): wire the review path for
# conductor's own tool-grant requests (#111 agent-intent grant, conductor
# tier). Deferred until chain-of-thought visibility lands and #108
# completion-review protocol ships — co-co reuses both.
class CoConductor:
    """Second sovereign tier — backs the conductor for review work.

    Today: stub. Every method raises NotImplementedError. The class
    exists so callers can reference it (typed imports, route stubs) and
    so the audit grep `co-conductor-deferred` finds every site that
    needs filling once dependencies land.
    """

    def review_conductor_tool_request(
        self,
        *,
        tool: str,
        reason: str,
        cot_excerpt: str | None = None,
    ) -> dict[str, Any]:
        """Approve / deny a conductor's own tool-grant request.

        Mirror of conductor reviewing an Expert/Worker request, one tier
        up. Conductor → co-co → (rarely) king.

        TODO(co-conductor-deferred): implement once #111 protocol +
        chain-of-thought surfacing land.
        """
        raise NotImplementedError("co-conductor-deferred: tool-grant review not yet wired")

    def relieve_conductor(self, *, reason: str = "") -> dict[str, Any]:
        """Take over conducting in the king's absence.

        Operator-triggered (or alarm-triggered) when the conductor is
        stuck, hung, or off-watch. Co-co assumes conducting role.

        TODO(co-conductor-deferred): wire to existing
        conductor_mode_enter / conductor_mode_exit machinery once
        co-conductor identity layer is defined.
        """
        raise NotImplementedError("co-conductor-deferred: relief / handoff not yet wired")

    def heartbeat(self) -> dict[str, Any]:
        """Watchdog — periodic check the conductor is still healthy.

        TODO(co-conductor-deferred): wire to conductor liveness signal
        + escalation alerts.
        """
        raise NotImplementedError("co-conductor-deferred: heartbeat not yet wired")
