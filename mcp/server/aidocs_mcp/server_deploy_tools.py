"""ai_deploy + ai_deploy_output — the gate-side (app) half of the remote deploy trigger.

ai_deploy is the highest-authority tool: it triggers a remote AIDOCS crown deploy. The gate
(OuterGate.execute) enforces the strict super_admin + ref-allowlist authority BEFORE this impl
runs; this impl then verifies the AIDOCS_PRIVATE binding (which needs project context to resolve
the git origin) and ENQUEUES a request for the root deploy-runner daemon. The impl never signs,
never runs root, never holds a key — it only writes a queue file (see ai_deploy_queue).

The enqueue core (`perform_deploy_enqueue`) is a module-level, dependency-injected function so it
is unit-testable without building a server.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import uuid
from pathlib import Path

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
from typing import Any

from . import ai_deploy_queue
from . import ai_deploy_states as _states
from .ai_deploy_authority import DEFAULT_ALLOWED_REFS, origin_is_aidocs_private
from .mcp_server_runtime_helpers import resolve_project_root
from .tool_interface import register_impl

# Operator dashboard origin where the 2-factor sign-flow (TOTP + static password) lives. The agent
# is handed a one-time link here; the secret inputs never touch the agent's harness.
#
# This is the AIDOCS GATE host (mcp.codenexus.cloud = outer_gate_transport.DEFAULT_PUBLIC_BASE),
# NOT the codenexus.cloud product site: the TOTP factor + the sign state machine are AIDOCS-native
# (ai_deploy_totp / ai_deploy_states), so /deploy/sign is served by the gate transport, not ADB.
# (2026-07-10: was https://codenexus.cloud — the sign-link 404'd against the wrong host.)
# Production overrides with AIDOCS_DASHBOARD_URL; keep this default in sync with DEFAULT_PUBLIC_BASE.
_DEFAULT_DASHBOARD_URL = "https://mcp.codenexus.cloud"

# One-time dashboard sign-link lifetime. A triggered deploy's link is BOTH one-time (consumed on the
# first successful sign) AND time-limited: past this TTL the sign gate refuses it fail-closed, so a
# leaked or forgotten link cannot be signed indefinitely later (§5 canonical-request hardening).
SIGN_LINK_TTL_SECONDS = 900.0  # 15 minutes


def resolve_deploy_queue_dir() -> Path:
    """Where ai_deploy writes requests for the root daemon. AIDOCS_DEPLOY_QUEUE_DIR wins
    (tests + ops override); else `<AIDOCS_GATE_ROOT>/deploy-queue`; else a local fallback."""
    env = os.environ.get("AIDOCS_DEPLOY_QUEUE_DIR")
    if env:
        return Path(env)
    gate_root = os.environ.get("AIDOCS_GATE_ROOT")
    if gate_root:
        return Path(gate_root) / "deploy-queue"
    return Path.home() / ".aidocs" / "deploy-queue"


def resolve_git_origin(root: str | Path) -> str:
    """`git -C <root> config --get remote.origin.url`; returns '' on any failure so the
    binding check FAILS CLOSED (an unresolvable origin is never treated as AIDOCS_PRIVATE)."""
    try:
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        p = audited_run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            fingerprint=("server_deploy_tools.py", "resolve_git_origin", "subprocess.run"),
            reason="deploy-origin-probe",
            run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_WIN_NO_WINDOW,
        )
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def resolve_git_commit(root: str | Path, ref: str) -> str:
    """`git -C <root> rev-parse --verify <ref>^{commit}` → the immutable 40-hex commit the ref points
    at; returns '' on any failure so a deploy that cannot pin a commit FAILS CLOSED (the runner
    refuses a request with no valid commit_sha)."""
    try:
        from .shell_egress_service import audited_run

        p = audited_run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            fingerprint=("server_deploy_tools.py", "resolve_git_commit", "subprocess.run"),
            reason="deploy-commit-pin",
            run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_WIN_NO_WINDOW,
        )
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def perform_deploy_enqueue(
    ref: str,
    *,
    project_root: str | Path,
    queue_dir: str | Path,
    principal_user: str,
    now: float,
    origin: str | None = None,
    reason: str = "",
    dashboard_base_url: str = "",
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Binding-check + enqueue, fail-closed. `origin` may be injected for tests; otherwise it is
    resolved from the project's git remote. The super_admin + session + ref + reason authority is
    enforced upstream at the gate — this layer re-checks the AIDOCS_PRIVATE binding (CANONICAL
    origin parse), the ref allowlist, and a non-empty reason (defense-in-depth) before the queue
    write. `principal_user` is the GATE-RESOLVED owner; it is recorded so ai_deploy_output can
    restrict reads to the owner or a super_admin."""
    resolved_origin = origin if origin is not None else resolve_git_origin(project_root)
    if not origin_is_aidocs_private(resolved_origin):
        return {
            "ok": False,
            "error": "wrong_project_binding",
            "reason": (
                f"ai_deploy refuses: bound project origin {resolved_origin!r} is not AIDOCS_PRIVATE "
                "— rebind to AIDOCS_PRIVATE before deploying (never deploy a drifted/default binding)"
            ),
        }
    _ref = str(ref or "main").strip()
    if _ref not in DEFAULT_ALLOWED_REFS:
        return {
            "ok": False,
            "error": "ref_not_allowed",
            "reason": f"ai_deploy ref {_ref!r} is not in the allowlist {sorted(DEFAULT_ALLOWED_REFS)}",
        }
    _reason = str(reason or "").strip()
    if not _reason:
        return {
            "ok": False,
            "error": "reason_required",
            "reason": "ai_deploy requires a non-empty `reason` (audited; bound to the deploy confirmation)",
        }
    deploy_id = "dpl_" + uuid.uuid4().hex[:12]
    # Stepwise 2-factor: a triggered deploy starts in AWAITING_2FA — it does NOT run until the
    # operator completes TOTP + the static password on the dashboard. Hand back a ONE-TIME sign-link
    # carrying a fresh token; persist ONLY the token's sha256 (the raw token lives solely in the
    # returned link, so only its holder can drive the sign), never the token itself.
    sign_token = _states.new_sign_token()
    token_sha256 = hashlib.sha256(sign_token.encode("utf-8")).hexdigest()
    # Pin the immutable commit the ref points at NOW (injected in tests; resolved from git in prod).
    # Best-effort at this unprivileged layer — the privileged runner INDEPENDENTLY requires a valid
    # 40-hex commit_sha (authority.validate_deploy_tree), so an unresolvable pin fails closed there.
    resolved_commit = commit_sha if commit_sha is not None else resolve_git_commit(project_root, _ref)
    res = ai_deploy_queue.enqueue_deploy(
        queue_dir,
        deploy_id=deploy_id,
        ref=_ref,
        principal_user=principal_user,
        requested_at=now,
        extra={
            "reason": _reason,
            "state": _states.AWAITING_2FA,
            "sign_token_sha256": token_sha256,
            # §5 canonical binding: pin the immutable commit + time-limit the one-time link + a
            # per-request nonce. All immutable inputs; the nonce is what the future approval-receipt
            # digest binds to; commit_sha is the commit the operator approves + the runner deploys.
            "commit_sha": resolved_commit,
            "expires_at": now + SIGN_LINK_TTL_SECONDS,
            "nonce": secrets.token_hex(16),
        },
    )
    if not res.get("ok"):
        return res
    res["state"] = _states.AWAITING_2FA
    if dashboard_base_url:
        res["dashboard_link"] = _states.sign_link(
            dashboard_base_url, deploy_id=deploy_id, token=sign_token
        )
    res["message"] = (
        "deploy queued in AWAITING_2FA — open the dashboard link to complete TOTP + the static "
        "password; the release then signs, tests in DEV, and promotes DEV->LIVE. Poll ai_deploy_output."
    )
    return res


def register_deploy_tools(*, server: Any, hub: Any = None, runtime: Any = None) -> None:
    """Register the ai_deploy + ai_deploy_output @server.tool impls and their direct-dispatch
    entries. hub/runtime are accepted for call-site symmetry with the other register_* tools."""

    @server.tool(
        annotations={"destructiveHint": False, "openWorldHint": False, "title": "AI Deploy (super_admin)"},
    )
    def ai_deploy(ref: str = "main", reason: str = "", confirm_token: str = "") -> Any:
        """Trigger a remote AIDOCS crown deploy of `ref`. Authority (super_admin + session +
        ref + non-empty reason + consumable confirm) is enforced at the gate; this verifies the
        AIDOCS_PRIVATE binding + enqueues for the daemon. The owner recorded is the gate-resolved
        principal. `confirm_token` is consumed by the gate's two-phase confirm (ignored here)."""
        import time

        project_root = resolve_project_root()
        # Owner = the GATE-RESOLVED principal (authoritative on the remote gate). Fall back to
        # the local identity only when there is no gate principal (local in-process trigger).
        from .mcp_server_runtime_helpers import current_gate_principal

        _gp = current_gate_principal() or {}
        principal_user = str(_gp.get("user_id") or "")
        if not principal_user:
            try:
                from .identity_resolver import current_user_id

                principal_user = current_user_id(project_root)
            except Exception:
                principal_user = ""
        return perform_deploy_enqueue(
            ref,
            project_root=project_root,
            queue_dir=resolve_deploy_queue_dir(),
            principal_user=principal_user,
            now=time.time(),
            reason=reason,
            dashboard_base_url=os.environ.get("AIDOCS_DASHBOARD_URL", _DEFAULT_DASHBOARD_URL),
        )

    register_impl("ai_deploy", ai_deploy)

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "AI Deploy Output"},
    )
    def ai_deploy_output(deploy_id: str = "") -> Any:
        """Read the status + log of a deploy enqueued by ai_deploy (queued|running|ok|failed)."""
        return ai_deploy_queue.read_deploy_output(
            resolve_deploy_queue_dir(), str(deploy_id or "").strip()
        )

    register_impl("ai_deploy_output", ai_deploy_output)
