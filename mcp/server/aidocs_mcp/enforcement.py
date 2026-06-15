"""Shared enforcement-override surface — the emergency key.

Castle law (king-rendered 2026-05-03):
    The emergency key must hang outside the prison cell, not inside it.

`dev.kill_switch` is the operator's break-glass. It MUST be checked at
the top of every gate-bearing entrypoint, BEFORE any gate (freeze,
managed-mode, policy, judge) gets a chance to refuse. If a gate can
short-circuit before this helper runs, the override is theatre.

Single source of truth so claude_hook PreToolUse, gate_tool.enforce_tool_call,
ai_run_kill, and AgentOrchestrator.check_tool all read the same flag with
the same flavor lock and emit the same audit shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def is_kill_switch_active(
    project_root: Path,
    *,
    session_id: str | None = None,
) -> bool:
    """Always-shipped FACADE over the dev-only break-glass.

    Release-only excision (king re-seal 2026-05-30): the kill-switch
    IMPLEMENTATION lives in the optional ``_dev_killswitch`` module,
    which is STRIPPED from every release artifact before the package
    is fingerprinted + signed. This facade is the always-present layer
    that every gate imports; it fails CLOSED (kill inactive, gates
    enforce) whenever the implementation is unavailable or forbidden.

    Three independent layers keep a released install from ever honoring
    kill — defense by identity, absence, and integrity:

      1. IDENTITY — honor kill ONLY on a dev-flavor (run-from-source)
         install. ``is_dev_flavor()`` reads distribution.flavor
         GLOBAL-only, so a project/session row can't forge it. A
         released (solo/corpo) install returns False here regardless of
         whether the optional module is present (reinsertion-proof at
         the flavor level).
      2. ABSENCE — on a release artifact ``_dev_killswitch`` is not in
         the shipped bytes, so even if distribution.flavor were flipped
         to 'dev' on a released install, the import below raises and
         this returns False. Flipping the flavor bit alone is inert —
         there is no implementation to load. This is the layer that
         answers "if somehow the bit gets flipped".
      3. INTEGRITY — re-inserting the module (to defeat layer 2 after a
         flavor flip) changes the package fingerprint, so
         release_trust.verify_release reports tamper (the strip happens
         BEFORE signing, so the signed fingerprint is of the stripped
         tree). Resurrection cannot be silent.

    On a clean run-from-source dev tree: flavor=='dev' → layer 1 passes
    → the module is present → layer 2 passes → the real flavor-locked,
    per-session implementation runs exactly as before.

    Never raises. Any error returns False — enforce, don't bypass.
    """
    # Layer 1 (identity): only a dev-flavor source build may honor kill.
    # Reinsertion-proof at the flavor level; GLOBAL-only flavor read.
    if not is_dev_flavor(project_root):
        return False
    # Layer 2 (absence): import the optional implementation; fail closed.
    # On a release artifact the module is gone → ImportError → False,
    # so a forged flavor=='dev' on a released install is still inert.
    try:
        from ._dev_killswitch import is_kill_switch_active_impl
    except Exception:
        return False
    try:
        return bool(
            is_kill_switch_active_impl(project_root, session_id=session_id),
        )
    except Exception:
        return False


def is_dev_flavor(project_root: Path | None = None) -> bool:
    """True iff ``distribution.flavor == 'dev'`` — a contributor build.

    Dev installs are the operator's own machine, so dev-only break-glass
    paths (e.g. the admin-CLI operator-token wall) may relax WITHOUT the
    operator also having to flip ``dev.kill_switch`` first: dev flavor OR
    kill switch, not AND. This is intentionally BROADER than
    :func:`is_kill_switch_active` (which requires the explicit switch too).
    corpo/solo installs are never dev flavor, so they never relax here.

    distribution.flavor is GLOBAL/install state (scope=['global']) and is
    read GLOBAL-ONLY here: a project- or session-scope config row can NOT
    elevate an install to dev-flavor (so a hostile repo can't self-grant
    the dev super-admin CLI). ``project_root`` is accepted for call-site
    symmetry and intentionally NOT used for the flavor read.

    Never raises — a config-read error returns False (enforce, don't
    accidentally relax).
    """
    try:
        from .config import get_setting

        flavor = (
            str(
                get_setting(
                    "distribution.flavor",
                    default="solo",
                )
                or "",
            )
            .strip()
            .lower()
        )
        return flavor == "dev"
    except Exception:
        return False


def dev_mode_authorized(project_root: Path | None) -> bool:
    """DERIVED authority for dev_mode source-editing.

    dev_mode unlocks editing the AIDOCS source itself (the aidocs_mcp
    package, index-language TOMLs, plugins). That power must NOT be
    grantable by a freely-set config flag alone — a solo/corpo install, or
    any project that merely USES AIDOCS, must never unlock source editing.

    True ONLY when BOTH hold:
      * the install is dev-flavor (a contributor build), and
      * project_root IS the canonical AIDOCS source repo (the package
        subdir exists at the well-known path).

    This is the SOLE authority for self-editing AIDOCS source (2026-06-12):
    the former ``dev.dev_mode`` config toggle is removed, and there is no
    caller-privilege gate — on such a contributor build every agent
    (conductor or spawned worker) may edit the source. Fails closed on any
    error.
    """
    try:
        if project_root is None:
            return False
        if not is_dev_flavor(project_root):
            return False
        from .file_ops import _is_aidocs_source_repo

        return bool(_is_aidocs_source_repo(Path(project_root)))
    except Exception:
        return False


def hard_protected_edit_authorized(project_root: Path | None) -> bool:
    """Runtime authority for editing a hard-protected DATA file (non-sqlite).

    Hard-protected DATA files are the project sqlite DBs, the AIDOCS index, and
    gate-state JSON (see ``hard_protected_paths``). sqlite is NEVER file-
    editable regardless of this resolver — ``config_set`` is its only door.
    For the remaining data files the edit authority is:

      * the principal must be HUMAN — agents/subagents are always denied
        (the whole point is fencing autonomous editors off these files), and
      * the principal must hold the admin ``security.hard_protected`` RBAC
        permission. In no-RBAC-users mode ``check_permission`` allows (the
        established single-user backward-compat), so a solo human operator
        keeps least-friction access while every agent stays fenced.

    Escalation (a non-admin requesting + an admin approving) lands as a
    follow-up; until then non-admins are refused here. Fails closed.
    """
    try:
        if project_root is None:
            return False
        from .identity_resolver import current_user
        from .rbac import RBACStore

        user_id, _email, principal_type = current_user(Path(project_root))
        if principal_type != "human":
            return False
        check = RBACStore().check_permission(
            Path(project_root), user_id or None, "security.hard_protected"
        )
        return bool(check.allowed)
    except Exception:
        return False


def record_kill_switch_bypass(
    project_root: Path,
    *,
    source: str,
    target: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit one audit event per bypass so compliance can reconstruct
    the exemption window.

    `event_kind` stays as ``enforcement_disabled_bypass`` for dashboard
    compatibility (ExecutionPage filters on that name). `source` is the
    entrypoint that bypassed (e.g. ``claude_hook``, ``gate_tool``,
    ``ai_run_kill``, ``agent_orchestrator``). `target` is the
    tool/event being bypassed.

    Best-effort: never raises, never blocks the bypass return.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        merged: dict[str, Any] = {
            "source": source,
            "target": target,
            "reason": (
                "dev.kill_switch=true on a dev-flavor install; "
                "gate cascade short-circuited to allow."
            ),
        }
        if payload:
            for k, v in payload.items():
                if k not in merged:
                    merged[k] = v
        ExecutionIndexStore().record_event(
            project_root,
            event_kind="enforcement_disabled_bypass",
            source_kind=source,
            capability_name=target or source,
            action_kind="enforcement_bypass",
            target_entity=str(target or source)[:200],
            status="bypassed",
            payload=merged,
        )
    except Exception:
        pass
