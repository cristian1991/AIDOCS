"""ai_git network verbs (fetch / pull / push) for a GATE principal.

Pure orchestration over two seams so it is testable without a server:
  * ``git_net`` — the credentialed primitive (gate_git_transport.run_gate_git)
  * ``git_local(argv) -> (rc, stdout)`` — plain local git (rev-parse etc.)

Authority is enforced HERE as well as at the outer gate (fail-closed,
defence in depth; never inferred from the org credential being resolvable):
  fetch  → read tier (admitted at the gate on tier_r_invoke)
  pull   → tier_m_edit + selected session
  push   → tier_m_edit + git.push + selected session, explicit refspec (#762),
           and a ledger row (principal, session, project, remote, refspec, result).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import gate_git_transport as ggt
from .git_push_authority import (
    PushAuthority,
    live_push_authority,
    pull_refusal,
    push_session_refusal,
)

GitLocal = Callable[[list[str]], "tuple[int, str]"]

PUSH_EVENT_KIND = "git_push"


def derive_push_plan(remote: str, refspec: str, git_local: GitLocal) -> dict[str, Any]:
    """#762: an explicit remote + source:destination refspec, or a refusal.

    Both empty ⇒ derived from the CURRENT branch and its tracked upstream.
    One given without the other ⇒ ambiguous, refused. Detached HEAD / no
    upstream ⇒ refused. Force (+), delete (:dst) and flag shapes ⇒ refused.
    Returns {"ok": True, remote, refspec, source, destination, derived} or
    {"ok": False, "error": ...}.
    """
    r = str(remote or "").strip()
    s = str(refspec or "").strip()
    derived = False
    if bool(r) != bool(s):
        return {
            "ok": False,
            "error": (
                "git push refused: pass BOTH `remote` and `refspec` "
                "(source:destination), or neither to derive them from the current "
                "branch's tracked upstream (#762)."
            ),
        }
    if not r:
        derived = True
        rc, br = git_local(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = (br or "").strip()
        if rc != 0 or not branch or branch == "HEAD":
            return {
                "ok": False,
                "error": "git push refused: detached HEAD — no current branch to push (#762).",
            }
        rc, up = git_local(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{u}}"]
        )
        upstream = (up or "").strip()
        if rc != 0 or "/" not in upstream:
            return {
                "ok": False,
                "error": (
                    f"git push refused: branch {branch!r} has no tracked upstream; "
                    "pass remote + refspec explicitly (#762)."
                ),
            }
        r, dst = upstream.split("/", 1)
        s = f"{branch}:{dst}"
    refusal = ggt.refspec_refusal(r, s)
    if refusal:
        return {"ok": False, "error": f"git push refused: {refusal} (#762)"}
    src, dst = s.split(":", 1)
    return {
        "ok": True,
        "remote": r,
        "refspec": s,
        "source": src,
        "destination": dst,
        "derived": derived,
    }


def _net_response(res: ggt.GateGitResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "success": res.ok,
        "output": (res.stdout or res.stderr),
        "credentialed": res.credentialed,
    }
    if not res.ok:
        out["exit_code"] = res.returncode
        out["failure_category"] = res.category
        out["reason"] = res.reason
    return out


def _ledger(hub: Any, project_root: Path, *, critical: bool, **fields: Any) -> bool:
    from .enforcement_pkg.audit_critical import (
        record_audit_best_effort,
        record_audit_critical,
    )

    fn = record_audit_critical if critical else record_audit_best_effort
    if hub is None:
        return not critical
    return fn(hub, project_root, **fields)


def run_gate_git_op(
    op: str,
    *,
    project_root: Path,
    principal: dict,
    remote: str = "",
    refspec: str = "",
    hub: Any = None,
    git_net: Callable[..., ggt.GateGitResult] | None = None,
    git_local: GitLocal,
    push_authority: Callable[[dict], PushAuthority] | None = None,
) -> dict[str, Any]:
    """Run fetch / pull / push for a gate principal. Returns the ai_git body.

    ``push_authority`` is a test seam; production always runs the LIVE per-push
    check (git_push_authority.live_push_authority, #1060 r0b)."""
    net = git_net or ggt.run_gate_git
    tenant = str((principal or {}).get("tenant_id") or "")
    o = str(op or "").strip().lower()
    if o == "fetch":
        return _net_response(net(["fetch", "--all"], cwd=project_root, tenant_id=tenant))
    if o == "pull":
        why = pull_refusal(principal)
        if why:
            return {"ok": False, "blocked": True, "blocked_by": "insufficient_scope", "error": why}
        return _net_response(net(["pull"], cwd=project_root, tenant_id=tenant))
    if o == "push":
        # #1060 v3: the shared guard here is the SESSION binding; the token-scope
        # path is no longer a precondition (an interactive client never has it).
        # `live_push_authority` below is THE authority — current role or an
        # active explicit grant — and it is re-decided on every use.
        why = push_session_refusal(principal)
        if why:
            return {"ok": False, "blocked": True, "blocked_by": "insufficient_scope", "error": why}
        decision = (push_authority or live_push_authority)(principal)
        if not decision.ok:
            # A refusal is a retained decision too (git_push is FORENSIC).
            _ledger(
                hub, project_root, critical=False, status="refused",
                event_kind=PUSH_EVENT_KIND, source_kind="ai_git", capability_name="git.push",
                action_kind="push", target_entity=f"{remote} {refspec}".strip(),
                principal_type="user", user_id=str(principal.get("user_id") or ""),
                session_id=str(principal.get("session_id") or ""),
                payload={
                    "principal": str(principal.get("user_id") or ""),
                    "tenant_id": tenant,
                    "session_id": str(principal.get("session_id") or ""),
                    "project_root": str(project_root),
                    "result": "refused",
                    "authority_detail": decision.detail,
                    **decision.as_payload(),
                },
            )
            msg = f"git push refused: {decision.reason}"
            if decision.detail:
                msg += f" ({decision.detail})"
            return {"ok": False, "blocked": True, "blocked_by": "push_authority",
                    "reason": decision.reason, "error": msg}
        plan = derive_push_plan(remote, refspec, git_local)
        if not plan.get("ok"):
            return {"ok": False, "error": plan["error"]}
        push_plan = {k: plan[k] for k in ("remote", "refspec", "source", "destination", "derived")}
        common = dict(
            event_kind=PUSH_EVENT_KIND,
            source_kind="ai_git",
            capability_name="git.push",
            action_kind="push",
            target_entity=f"{plan['remote']} {plan['refspec']}",
            principal_type="user",
            user_id=str(principal.get("user_id") or ""),
            session_id=str(principal.get("session_id") or ""),
        )
        base_payload = {
            "principal": str(principal.get("user_id") or ""),
            "tenant_id": tenant,
            "session_id": str(principal.get("session_id") or ""),
            "project_root": str(project_root),
            "remote": plan["remote"],
            "refspec": plan["refspec"],
            **decision.as_payload(),
        }
        # Mutation doctrine: no unaudited push. The intent row must land first.
        if not _ledger(
            hub, project_root, critical=True, status="requested",
            payload={**base_payload, "result": "requested"}, **common,
        ):
            return {
                "ok": False,
                "blocked": True,
                "blocked_by": "audit_unavailable",
                "error": "git push refused: the push ledger row could not be written",
                "push_plan": push_plan,
            }
        res = net(
            ["push", plan["remote"], plan["refspec"]], cwd=project_root, tenant_id=tenant
        )
        body = _net_response(res)
        body["push_plan"] = push_plan
        _ledger(
            hub, project_root, critical=False,
            status="ok" if res.ok else "failed",
            payload={
                **base_payload,
                "result": "ok" if res.ok else "failed",
                "failure_category": res.category,
                "exit_code": res.returncode,
            },
            **common,
        )
        return body
    return {"ok": False, "error": f"gate git op {o!r} is not a network verb"}


def gate_drift_runner(project_root: Path, tenant_id: str, git_local: GitLocal, *, git_net=None):
    """compute_origin_drift runner for the gate: fetch credentialed, rest local.

    Returns 3-tuples (rc, stdout, failure dict) for fetch so an unreachable
    origin carries category + scrubbed reason.
    """
    net = git_net or ggt.run_gate_git

    def _run(argv: list[str]):
        if argv and argv[0] == "fetch":
            res = net(list(argv), cwd=project_root, tenant_id=tenant_id, timeout=15)
            return res.returncode, res.stdout, {"category": res.category, "reason": res.reason}
        return git_local(list(argv))

    return _run
