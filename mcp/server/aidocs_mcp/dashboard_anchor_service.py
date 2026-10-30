"""The dashboard's anchor door (#1187): ONE body for the desktop and the web gate.

The operator's dashboard lists, previews and changes memory->symbol anchors.
Two transports reach it and both call ``run`` here, so they cannot drift into
two write authorities (r0b1 383c371f-d7f item 5):

  * desktop: Tauri -> ``aidocs dashboard-anchor`` (cli.cmd_dashboard_anchor),
    authenticated by the operator token the desktop already passes the CLI;
  * web: the gate tool ``dashboard_anchor``
    (outer_gate_transport._ogt_pt_dashboard_anchor), authenticated by the gate's
    OAuth principal, OWNER/ADMIN in the org that owns the VIEWED project.

Each door authenticates on its own surface and hands ``run`` either a
``Caller`` or the reason there is none. It re-authenticates on every call, so
a principal that loses the permission between preview and apply is refused at
apply (r0b1 item 1).

An apply goes, in this order:

  1. no caller -> refused, through ``anchor_service.apply`` so the refusal is
     audited like every other anchor refusal;
  2. the plan is re-derived; an empty plan is refused before any consent is
     asked for (r0b1 item 3);
  3. the caller's fingerprint must match the state now; a stale preview is
     refused WITHOUT spending a consent;
  4. the consent: a single-use, short-lived token from
     ``canonical_invocation.confirm_exchange``, bound to the authenticated
     user, the credential (token id), the project, the op, the tier, the
     target, the fingerprint and the exact anchors the plan changes (r0b1
     d7745cbb-bb1 Q2). The first call mints it and returns
     ``confirm_required``; the second presents it. A token bound to anything
     else is refused and NOT consumed, so it still confirms its own act;
  5. ``anchor_service.apply`` -- the compare-and-swap inside the write
     transaction and the three-phase audit, with the authenticated user as
     the actor.

The consent is consumed before the compare-and-swap. The two live in different
databases, so they cannot commit together; if the anchors change in between,
the spent consent was for a state that no longer exists, and the new state
needs a new preview and a new consent anyway.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import anchor_service

TOOL = "dashboard_anchor"
MODES: tuple[str, ...] = ("list", "preview", "apply")
#: The intent the consent is bound to (folded into its bind hash).
INTENT = "confirm anchor change"


@dataclass(frozen=True)
class Caller:
    """The principal a door authenticated. Never built from a caller's claim."""

    user_id: str
    token_id: str  # the credential the consent is bound to
    surface: str  # "desktop" | "gate"
    auth_path: str


def _binding(project_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Everything the human agrees to. The anchors are named by id only."""
    return {
        "project_id": project_id,
        "op": plan["op"],
        "to": plan["to"],
        "target": str(plan["target"]).strip(),
        "fingerprint": plan["fingerprint"],
        "anchor_ids": sorted(int(c["anchor_id"]) for c in plan["changes"]),
    }


def _summary(plan: dict[str, Any], project_name: str, project_id: str) -> str:
    n = len(plan["changes"])
    tier = f" to {plan['to']}" if plan["to"] else ""
    where = f"project {project_name!r} ({project_id})" if project_name else f"project {project_id}"
    return (
        f"About to {plan['op']}{tier} {n} anchor{'s' if n != 1 else ''} on "
        f"{plan['target']!r} in {where}. The user must confirm."
    )


def run(
    project_root: Path | str,
    mode: str,
    *,
    caller: Caller | None,
    refusal: str = "",
    project_id: str,
    project_name: str = "",
    confirm_store: Any,
    target: str = "",
    op: str = "",
    to: str = "",
    fingerprint: str = "",
    confirm_token: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """One dashboard anchor call. ``caller`` None means unauthenticated or
    unauthorized, with ``refusal`` saying why."""
    from . import canonical_invocation as _ci

    root = Path(project_root)
    mode = str(mode or "").strip().lower()
    if mode not in MODES:
        return {"ok": False, "_error": "unknown_mode", "_detail": " | ".join(MODES)}
    if caller is not None and not str(caller.user_id or "").strip():
        caller, refusal = None, "the authorized principal could not be identified"
    if caller is None:
        reason = refusal or "no authenticated operator"
        if mode != "apply":
            return {"ok": False, "_error": "permission_denied", "_detail": reason}
        return anchor_service.apply(
            root,
            op,
            target,
            expected_fingerprint=fingerprint,
            authorize=lambda: (False, reason),
            actor={"source": TOOL, "surface": "", "user_id": ""},
            to=to,
        )
    if mode == "list":
        return anchor_service.list_anchors(root, target)
    plan = anchor_service.preview(root, op, target, to=to)
    if mode == "preview" or not plan.get("ok"):
        return plan
    if not plan["changes"]:
        return {
            "ok": False,
            "_error": "nothing_to_change",
            "_detail": f"{plan['op']} changes no anchor on {target!r}",
            "fingerprint": plan["fingerprint"],
        }
    if plan["fingerprint"] != str(fingerprint or ""):
        return {
            "ok": False,
            "_error": "state_changed",
            "_detail": "the anchors are not in the previewed state; preview again",
            "fingerprint": plan["fingerprint"],
        }
    consent_refusal, _ = _ci.confirm_exchange(
        store=confirm_store,
        tool=TOOL,
        confirm_args=_binding(project_id, plan),
        intent=INTENT,
        operator=caller.user_id,
        project_id=project_id,
        context_key=caller.token_id,
        presented=str(confirm_token or ""),
        now=time.time() if now is None else now,
        detail=(
            "an anchor change needs the operator's confirmation of exactly this "
            "change; confirm it, then call again with the server-issued confirm_token"
        ),
        summary=_summary(plan, project_name, project_id),
    )
    if consent_refusal is not None:
        consent_refusal.update(
            ok=False,
            project_id=project_id,
            project_name=project_name,
            op=plan["op"],
            to=plan["to"],
            target=target,
            count=len(plan["changes"]),
        )
        return consent_refusal
    return anchor_service.apply(
        root,
        op,
        target,
        expected_fingerprint=fingerprint,
        authorize=lambda: (True, ""),
        actor={
            "source": TOOL,
            "surface": caller.surface,
            "user_id": caller.user_id,
            "auth_path": caller.auth_path,
        },
        to=to,
    )
