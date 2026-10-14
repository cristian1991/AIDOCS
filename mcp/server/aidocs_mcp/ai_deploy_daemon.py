"""The root deploy-runner daemon's PURE core (the queue consumer).

ai_deploy (app) writes a request to the queue; THIS is the root-side counterpart that picks it up
and runs the crown deploy. The daemon is the ONLY privileged component — so it NEVER trusts the
queue file blindly: it RE-VALIDATES every request (ref allowlist + AIDOCS_PRIVATE binding of the
tree it is about to deploy) before doing anything, single-flights so two deploys never overlap, and
records status + log back to the queue.

This module is the pure, dependency-injected core: claim + re-validate + lifecycle. The thin I/O
shell — the watch loop, the real git fetch/checkout, the actual `deploy_aidocs_gate.sh --deploy
--skip-sign` subprocess, and the systemd unit — is wired at the VPS install and kept OUT of here so
the security-relevant logic is unit-testable. The injected `deploy_runner(ref) -> (rc, log)` is what
finally shells the deploy; the injected `origin_resolver() -> str` reports the bound tree's origin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .ai_deploy_authority import DEFAULT_ALLOWED_REFS
from .ai_deploy_queue import LOG_SUFFIX, STATUS_SUFFIX

STATUS_QUEUED = "queued"


def _status_of(queue_dir: Path, deploy_id: str) -> str:
    sp = queue_dir / f"{deploy_id}{STATUS_SUFFIX}"
    if sp.exists():
        return sp.read_text(encoding="utf-8").strip()
    return ""


def write_log(queue_dir: str | Path, deploy_id: str, log: str) -> None:
    """Daemon-owned log write."""
    (Path(queue_dir) / f"{deploy_id}{LOG_SUFFIX}").write_text(log, encoding="utf-8")


def claim_next(queue_dir: str | Path) -> dict[str, Any] | None:
    """DISABLED (Blocker C, 2026-07-10) — permanently fail-closed.

    The coarse ``status == queued`` consumer selected a request the moment it was
    enqueued, with NO 2FA / approval / signed-artifact gate. Installed as a watch loop
    it could deploy a tree BEFORE the operator completed the dashboard second factor.
    The canonical consumer is the state-machine runner
    ``ai_deploy_runner.claim_ready()``, which only claims a deploy that has advanced past
    the operator sign. This function now REFUSES rather than returning a runnable
    request, so it can never be wired as a production consumer by accident.

    ``queue_dir`` is accepted for signature compatibility only; it is never read."""
    raise RuntimeError(
        "ai_deploy_daemon.claim_next is disabled (Blocker C): the coarse status==queued "
        "consumer has no 2FA/approval/signed gate and must never run a deploy. Use the "
        "state-machine runner ai_deploy_runner.claim_ready() instead."
    )


def validate_request(
    request: dict[str, Any] | None,
    *,
    bound_origin: str | None,
    allowed_refs: "frozenset[str] | set[str]" = DEFAULT_ALLOWED_REFS,
) -> dict[str, Any]:
    """DEPRECATED thin delegate → the canonical runner-side re-validation now lives in
    ai_deploy_authority.validate_deploy_tree (ref allowlist + AIDOCS_PRIVATE origin), which the
    state-machine runner ai_deploy_runner.run_once() calls. Kept only for back-compat; the daemon's
    own run path is disabled. One validation source, so the two can never drift."""
    from .ai_deploy_authority import validate_deploy_tree

    return validate_deploy_tree(request, bound_origin=bound_origin, allowed_refs=allowed_refs)


def run_request(
    request: dict[str, Any] | None,
    *,
    queue_dir: str | Path,
    bound_origin: str | None,
    deploy_runner: Callable[[str], "tuple[int, str]"],
    allowed_refs: "frozenset[str] | set[str]" = DEFAULT_ALLOWED_REFS,
) -> dict[str, Any]:
    """DISABLED (Blockers C/D, 2026-07-10) — permanently fail-closed. The coarse run path ran a
    ref/origin-validated request but with NO deploy-state / 2FA / signed-artifact gate, and trusted
    the unprivileged app's mutable fields — so it could deploy a request before approval. The
    canonical runner is ai_deploy_runner.run_once(): it claims only a signed-ready deploy under an
    atomic lease AND re-validates the tree (authority.validate_deploy_tree) before running. This shim
    REFUSES so the coarse runner can never be wired as a production consumer."""
    raise RuntimeError(
        "ai_deploy_daemon.run_request is disabled (Blockers C/D): the coarse run path had no "
        "state/2FA/signed gate and trusted app-mutable fields. Use ai_deploy_runner.run_once()."
    )
