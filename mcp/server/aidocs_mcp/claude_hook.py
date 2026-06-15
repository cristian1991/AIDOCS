# ══════════════════════════════════════════════════════════════════════════
#  ⚠️  DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST  ⚠️
# ──────────────────────────────────────────────────────────────────────────
#  Live verification (2026-06-11) of the ungated additive-protection path. claude_hook.py is the UPS/PreToolUse host-hook entrypoint and grant-minting pipeline — the agent chose this file by recognition, no grant phrase needed.
#
# ══════════════════════════════════════════════════════════════════════════
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_orchestrator import ToolDecision  # type-only annotation (F821 seal)

import json
import logging
import sys
from pathlib import Path

from .config import render_interaction_text
from .runtime_service import RuntimeService
from .service_hub import AidocsServiceHub

logger = logging.getLogger("aidocs.claude_hook")


def _resolve_templates_root() -> Path:
    """Phase 2 (2026-05-27): delegate to host_services.path_resolver_service
    so opencode plugin + future host adapters share the same resolver.
    """
    from .host_services.path_resolver_service import resolve_templates_root

    return resolve_templates_root()


def _resolve_script_root() -> Path:
    """Phase 2 (2026-05-27): delegate to host_services.path_resolver_service."""
    from .host_services.path_resolver_service import resolve_script_root

    return resolve_script_root()


# Phase 1B (2026-05-27): the last three intent-detection delegates
# (_detect_scoped_revoke_tools, _detect_lane_exit_grant,
# _detect_protected_edit_overrides) and their constants
# (_PROTECT_CAP, _SCOPED_REVOKE_VERBS) moved to
# canonical_intent_registry. The _PROTECT_CAP=20 clamp is now an arg
# on detect_protected_edit_overrides_v2; the env-derived
# is_worker_caller fence on detect_lane_exit_v2 is supplied by
# callers that are NOT already inside a worker-fenced branch
# (prompt_mutator does that bail at the top of its lane-exit handler,
# so the call passes is_worker_caller=False unconditionally).


class ClaudeHookHandler:
    """Claude Code adapter — translates CC hook events to AgentOrchestrator calls."""

    def __init__(self) -> None:
        hub = AidocsServiceHub(
            templates_root=_resolve_templates_root(),
            script_root=_resolve_script_root(),
        )
        self.runtime = RuntimeService(hub)
        from .agent_orchestrator import AgentOrchestrator

        self.orchestrator = AgentOrchestrator(self.runtime)
        self._last_user_prompt: str = ""

    def handle(self, payload: dict[str, object]) -> dict[str, object] | None:
        # Open a request-scoped empire-DB read cache for the whole hook event:
        # the intent-token store reads the same (lang, kind) sets dozens of times
        # per UserPromptSubmit. The cache reuses one connection and memoizes reads
        # (cross-process truth preserved via PRAGMA data_version), collapsing the
        # repeated init_db + SELECT storm. Closed automatically on exit.
        from .intent_tokens_store import request_cache

        with request_cache():
            return self._handle_impl(payload)

    def _handle_impl(self, payload: dict[str, object]) -> dict[str, object] | None:
        event_name = str(payload.get("hook_event_name") or "").strip()
        if not event_name:
            return None

        # [DEV-ONLY FAILSAFE] Outermost kill switch (2026-04-22).
        # Checked BEFORE project resolution, event routing, or any
        # other hook logic so if AIDOCS internals misbehave the
        # operator can still flip this flag and keep working. Wrapped
        # in its own try/except: if even the flag-read fails, we do
        # NOT refuse — return None and let Claude Code proceed
        # unimpeded. The whole point is "AIDOCS never blocks
        # operator work." Flavor-locked to dev installs only.
        try:
            root_candidate = self._resolve_cwd_root(payload)
            # Per-session scope (Phoenix 2026-05-07): resolve aidocs
            # session_id from the hook payload's cli_session_id via
            # per-conductor lookup, so a per-session kill switch flip
            # is honored at the OUTERMOST gate too.
            _outer_aidocs_sid: str | None = None
            _outer_cli_sid = str(payload.get("session_id") or "").strip()
            if root_candidate is not None and _outer_cli_sid:
                try:
                    _managed = self.runtime.hub.managed_mode.get_mode(
                        root_candidate,
                        cli_session_id=_outer_cli_sid,
                    )
                    _outer_aidocs_sid = str(_managed.get("session_id") or "").strip() or None
                except Exception:
                    _outer_aidocs_sid = None
            if root_candidate is not None and self._enforcement_disabled(
                root_candidate,
                session_id=_outer_aidocs_sid,
            ):
                # Best-effort audit (never raises; event stamps that
                # a bypass happened even though nothing was enforced).
                try:
                    self._log_enforcement_bypass(
                        root_candidate,
                        event_name,
                        payload,
                    )
                except Exception:
                    pass
                # Phoenix 2026-05-07: kill_switch bypasses ENFORCEMENT,
                # not informational notifications. On UPS still surface
                # pending run_notifications + lane_completion_reviews
                # via additionalContext — the Emperor's "notifications
                # everywhere" directive holds even under bypass. Returns
                # plain None when there's nothing to surface (preserving
                # original behavior).
                if event_name == "UserPromptSubmit":
                    try:
                        _bypass_blocks: list[str] = []
                        try:
                            from . import run_notifications as _rn_b

                            _bypass_runs = _rn_b.peek(root_candidate)
                            if _bypass_runs:
                                _bypass_blocks.append(_rn_b.format_block(_bypass_runs))
                        except Exception:
                            pass
                        try:
                            from . import lane_completion_review_store as _lcr_b

                            if _outer_aidocs_sid or _outer_cli_sid:
                                _bypass_reviews = _lcr_b.pending_for_session(
                                    root_candidate,
                                    session_id=_outer_aidocs_sid or "",
                                    host_session_id=_outer_cli_sid or "",
                                )
                                if _bypass_reviews:
                                    _bypass_blocks.append(
                                        _lcr_b.format_pending_block(_bypass_reviews),
                                    )
                        except Exception:
                            pass
                        if _bypass_blocks:
                            return {
                                "hookSpecificOutput": {
                                    "hookEventName": "UserPromptSubmit",
                                    "additionalContext": "\n\n".join(_bypass_blocks),
                                },
                            }
                    except Exception:
                        pass
                return None
        except Exception:
            # Flag read blew up — fail OPEN, do not let AIDOCS's
            # own errors block the operator. Continue into the
            # normal hook path, which has its own try/except nets.
            pass

        # Fail-open dispatch: any unhandled exception inside the
        # event routers returns None instead of bubbling out of the
        # hook. A hook that raises leaves Claude Code with no clear
        # signal and typically gets interpreted as "refuse" by the
        # host. Returning None === "no refusal, carry on" which is
        # the correct fail-open posture: if AIDOCS is broken, the
        # operator should keep working, not be blocked.
        try:
            return self._dispatch_event(event_name, payload)
        except Exception as exc:  # noqa: BLE001
            # Best-effort log so debugging isn't blind. stderr
            # reaches Claude Code's hook log on Windows/POSIX alike.
            try:
                import sys as _sys_fail_open

                print(
                    f"[aidocs hook fail-open] {event_name}: {type(exc).__name__}: {exc}",
                    file=_sys_fail_open.stderr,
                )
            except Exception:
                pass
            return None

    def _dispatch_event(
        self,
        event_name: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Routed dispatch, extracted from handle() so the outer
        fail-open wrapper has a single function to try/except.
        """
        # ── Adoption auto-repair (UPS / SessionStart) ──────────────
        # An already-ADOPTED project (declares the aidocs MCP in
        # .mcp.json and/or carries an operator adoption record) whose
        # infrastructure was never created gets a minimal, non-
        # destructive commission here — so governance can engage WITHOUT
        # a manual /aidocs, and so the strict marker resolver below finds
        # the freshly-written marker. repair_if_adopted ONLY acts on the
        # ADOPTED_UNCOMMISSIONED state: UNSEEN / FOREIGN_MEMORY projects
        # are left untouched (first adoption is never performed here, so
        # governance never creeps onto a non-adopted repo), and it is a
        # no-op once commissioned. CLAUDE.md / AGENTS.md are never
        # rewritten. Best-effort; failure must not break the hook.
        if event_name in ("UserPromptSubmit", "SessionStart"):
            _cwd_root = self._resolve_cwd_root(payload)
            if _cwd_root is not None:
                try:
                    from .project_commission import repair_if_adopted

                    repair_if_adopted(_cwd_root)
                except Exception:
                    pass
        # Allow /aidocs command even on non-AIDOCS projects (for bootstrapping)
        if event_name == "UserPromptSubmit":
            prompt = str(payload.get("prompt") or "").strip()
            if prompt.startswith("/aidocs"):
                project_root = self._resolve_project_root(payload)
                if project_root is not None:
                    self._record_hook_event(project_root, event_name=event_name, payload=payload)
                return self._handle_aidocs_command(payload)

        if event_name == "SessionStart":
            project_root = self._resolve_cwd_root(payload)
            if project_root is None:
                return None
            result = self._handle_session_start(project_root, payload)
            if (project_root / ".MEMORY").is_dir():
                self._record_hook_event(project_root, event_name=event_name, payload=payload)
            return result

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return None

        self._record_hook_event(project_root, event_name=event_name, payload=payload)

        if event_name == "UserPromptSubmit":
            return self._handle_user_prompt_submit(project_root, payload)
        if event_name == "PreToolUse":
            return self._handle_pre_tool_use(project_root, payload)
        if event_name == "PostToolUse":
            return self._handle_post_tool_use(project_root, payload)
        if event_name == "PostCompact":
            return self._handle_post_compact(project_root, payload)
        if event_name in ("Stop", "SubagentStop"):
            return self._handle_stop(project_root, payload, event_name)
        return None

    def _handle_aidocs_command(self, payload: dict[str, object]) -> dict[str, object]:
        """Handle /aidocs command — works on both initialized and uninitialized projects.

        Two side effects beyond the additionalContext return:

        1. MCP auto-injection (2026-05-17, operator request): when invoked
           with a known cwd, idempotently ensures the project's `.mcp.json`
           carries the aidocs MCP server entry. Without this, the agent
           cannot call `project_init` / `project_bootstrap_or_resume`
           because those tools live behind the aidocs MCP — chicken-and-
           egg for fresh projects. Claude Code re-reads `.mcp.json` on
           `/mcp` reload or restart; the additionalContext message
           instructs the operator when an injection actually changed disk.
           OpenCode reads MCP globally so this hook is CC-specific; the
           Codex pathway is unowned today.

        2. Foreign-`.MEMORY/` detection: a project may have `.MEMORY/`
           created by another tool (e.g. a memory-palace product). The
           legacy branch fired the "use MCP bootstrap" message in that
           case, which would land AIDOCS files inside the other tool's
           memory tree. We now distinguish AIDOCS-marked `.MEMORY/` (has
           `.MEMORY/.aidocs/index.aidocs`) from foreign `.MEMORY/` and
           refuse to auto-bootstrap the foreign case — operator must
           confirm.
        """
        cwd = str(payload.get("cwd") or "").strip()
        project_root = Path(cwd).resolve() if cwd else None

        # ── /aidocs is ADMIN-ONLY ──────────────────────────────────
        # Adoption/commissioning binds governance to a project — a
        # privilege act. solo/dev = local-admin passthrough (the
        # operator's own machine); corpo requires the manage-config
        # grant. A non-admin user must not be able to adopt a tree or
        # activate managed mode (self-escalation / confused-deputy).
        if project_root is not None:
            # FAIL-CLOSED: any error evaluating the admin gate refuses
            # /aidocs (no silent proceed on an unevaluable check).
            _adm_reason = ""
            try:
                from .project_authority import require_admin

                _adm = require_admin(
                    project_root,
                    operation="aidocs_command",
                    host_session_id=str(payload.get("session_id") or "").strip(),
                )
                _adm_ok = bool(_adm.get("ok"))
                _adm_reason = str(_adm.get("reason") or "")
            except Exception as _exc:
                _adm_ok = False
                _adm_reason = f"authorization check failed: {_exc!r}"
            if not _adm_ok:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": (
                            "`/aidocs` is admin-only. "
                            + _adm_reason
                            + " Adoption/commissioning and managed-mode "
                            "activation require an authenticated operator "
                            "with the admin grant; no change was made."
                        ),
                    },
                }

        memory_exists = bool(project_root and (project_root / ".MEMORY").is_dir())
        from .mcp_server_runtime_helpers import is_aidocs_managed

        aidocs_marker = bool(project_root and is_aidocs_managed(project_root))

        # MCP injection. Idempotent — no-ops if entry already correct.
        # Surface failures inline so the operator can fix `.mcp.json`
        # by hand if the runtime call breaks.
        mcp_inject_note = ""
        if project_root is not None:
            try:
                mcp_result = self.runtime.ensure_claude_mcp_config(project_root)
                action = str((mcp_result or {}).get("action") or "")
                if action in ("created", "updated"):
                    mcp_inject_note = (
                        f"AIDOCS MCP entry was {action} in `.mcp.json`. "
                        "Run `/mcp` in Claude Code (or restart) to load "
                        "the aidocs server before invoking `project_init` "
                        "/ `project_bootstrap_or_resume`. "
                    )
            except Exception as exc:  # noqa: BLE001 — surface, not crash.
                mcp_inject_note = (
                    f"Could not ensure `.mcp.json`: "
                    f"{type(exc).__name__}: {exc}. "
                    "Manual update may be required before bootstrap. "
                )

        # Record DELIBERATE first adoption — running /aidocs IS the
        # operator's intent. Skipped for the foreign-`.MEMORY` case
        # (memory present, no marker), which still requires explicit
        # confirmation below before AIDOCS touches the tree. Adoption is
        # SQLite-only (creates no files); the actual infrastructure is
        # created by bootstrap / UPS auto-repair afterwards.
        if project_root is not None and not (memory_exists and not aidocs_marker):
            try:
                from .project_commission import adopt

                adopt(project_root, source="aidocs_cmd")
            except Exception:
                pass

        if aidocs_marker:
            context = (
                mcp_inject_note
                + "AIDOCS entry command detected. Use the MCP bootstrap/orchestrator flow for this project, "
                "report selected session and managed-mode state, and avoid broad repo reads before session routing completes. "
                "If multiple candidate sessions exist, STOP and ask the user which to bind."
            )
        elif memory_exists:
            # Foreign `.MEMORY/` — another tool owns it. Do NOT auto-init.
            context = (
                mcp_inject_note + f"AIDOCS entry command detected on `{project_root}` — "
                "`.MEMORY/` exists but is NOT marked as AIDOCS-managed "
                "(no `.MEMORY/.aidocs/index.aidocs`). This usually means "
                "another tool owns the `.MEMORY/` directory (e.g. a "
                "memory-system project). STOP and ASK the operator "
                "before running `project_init` — auto-bootstrap would "
                "mix two memory systems in one tree. If the operator "
                "confirms AIDOCS should manage this project, proceed with "
                "`project_init` then `project_bootstrap_or_resume`. "
                "Otherwise leave the existing `.MEMORY/` alone."
            )
        elif project_root is None:
            context = (
                "AIDOCS entry command detected, but no project root was provided by the host. "
                "Ask the user for the project path before initializing."
            )
        else:
            context = (
                mcp_inject_note
                + f"AIDOCS entry command detected on `{project_root}` — this project has no `.MEMORY/` yet. "
                "Call the `project_init` MCP tool with this root to create .MEMORY/, AGENTS.md/CLAUDE.md, and AIDOCS templates. "
                "Then call `project_bootstrap_or_resume` to activate managed mode. "
                "Do not begin other work until initialization and bootstrap succeed."
            )

        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            },
        }

    def _handle_session_start(
        self,
        project_root: Path,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """SessionStart context — delegated to LifecycleService
        (host-agnostic). CC renders the result into its
        hookSpecificOutput.additionalContext shape.
        """
        import os as _os_env_ws

        from .lifecycle_service import LifecycleService

        context = LifecycleService(self.runtime).build_session_start_context(
            host_kind="claude_code",
            host_session_id=str(
                (payload or {}).get("session_id") or "",
            ).strip(),
            project_root=project_root,
            is_worker_proc=bool(
                _os_env_ws.environ.get(
                    "AIDOCS_EXPERT_LANE_ID",
                    "",
                ).strip(),
            ),
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        }

    # ─── AIDOCS-SEC INVARIANT ───────────────────────────────────────
    # Blocked or unmanaged prompt MUST NOT change any privilege-relevant
    # session state.
    #
    # Privilege-relevant = any mutation that affects:
    #   • tool permissions (user_intent_tools, bash_subcommands,
    #     lane_raw_tools_granted, lane_eager_tools_granted)
    #   • escalation state (pending approvals, consumed approvals)
    #   • lane / actor identity (current_lane_id, lane_exact_paths)
    #   • confirmation / approval state (pending_confirmation,
    #     last_confirmed_operation)
    #   • credentials used in tool execution (user_intent_credentials)
    #   • sticky-grant flags
    #   • protect / unprotect / edit override grants
    #   • config-set grants
    #
    # Carve-outs (MUST still run even on blocked prompts):
    #   • user_prompt_received audit event — audit chain integrity
    #   • check_and_update_cli_session_id — defensive detection, the
    #     requires_reconnect flag it raises is a SAFETY signal
    #
    # Enforcement today (SEC-001 hotfix 2026-04-23): snapshot_privilege_state
    # taken before mutations, restore_privilege_state called on block.
    # Enforcement tomorrow (SEC-001 full + SEC-002): plan-before-apply
    # with atomic transaction commit.
    #
    # When adding a new grant type, new detector, or new write:
    #   1. If it's privilege-relevant → it MUST be covered by the
    #      snapshot column list OR flow through the eventual
    #      PromptMutationPlan. Add column to _PRIVILEGE_COLUMNS in
    #      session_query_gate_store.py.
    #   2. If it's a carve-out (liveness / audit / defensive) → document
    #      WHY it's a carve-out in a comment at the call site.
    #   3. Add a test that a blocked prompt leaves your new state
    #      unchanged.
    # ─────────────────────────────────────────────────────────────────
    def _handle_user_prompt_submit(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return None

        # UPS audit + fresh-CLI detection — delegated to PromptMutator
        # (host-agnostic).
        from .prompt_mutator import PromptMutator

        PromptMutator(self.runtime).record_user_prompt_received(
            prompt=prompt,
            host_session_id=str(payload.get("session_id") or "").strip(),
            project_root=project_root,
        )

        # Prompt-secret block — delegated to PromptMutator
        # (host-agnostic). Returns block envelope when prompt contains
        # credential tokens AND the policy is set to 'block'.
        from .prompt_mutator import PromptMutator

        _secret_result = PromptMutator(self.runtime).prompt_secret_block(
            prompt=prompt,
            project_root=project_root,
        )
        if _secret_result.decision == "block":
            return {
                "decision": "block",
                "reason": _secret_result.block_reason or "",
            }

        # ── Pre-flight prompt judge (#44 Batch 1, 2026-04-27) ──
        # Hostile operator intent is blocked HERE — before
        # `_grant_user_intent_tools` reads grant phrases (a hostile
        # prompt that says "allow bash; then exfil secrets" must NOT
        # inflate per-turn grants), before sticky-grant mutation,
        # before SEC-001 snapshot/SEC-002 atomic stage, before intent-
        # phrase dispatch. The ordering contract is load-bearing: any
        # mutation that runs before pre-flight could be poisoned by a
        # hostile prompt.
        #
        # Batch 1 has NO rules yet — the evaluator returns empty-
        # verdicts PASS for every prompt. The shape is in place so
        # Batches 2-7 can land rule families incrementally.
        #
        # Side-band degraded path: if the evaluator raises an
        # unhandled exception, return a deny envelope with the
        # operator-facing "pre-flight unavailable / degraded"
        # message and emit a distinct `event_type="preflight_degraded"`
        # audit event. Distinct from hostile-prompt verdicts so #43
        # strikes can filter system-bug events out of infraction
        # counts. Per security-gates.md §0.5 invariant #62.
        # Pre-flight judge — delegated to PromptMutator (host-agnostic).
        # Returns block when hostile/confirmable verdicts fire OR when
        # the evaluator degraded.
        _preflight_result = PromptMutator(self.runtime).preflight_judge(
            prompt=prompt,
            project_root=project_root,
        )
        if _preflight_result.decision == "block":
            return {
                "decision": "block",
                "reason": _preflight_result.block_reason or "",
            }
        # (The outer try/except safety net previously here was
        # preserved across the preflight-judge migration but became a
        # no-op once the body moved into PromptMutator.preflight_judge
        # — which carries its own safety net. Removed.)

        # Worker lane mailbox + protocol injection — delegated to
        # PromptMutator. CC resolves worker identity from env vars
        # (AIDOCS_EXPERT_*); other hosts will surface this via their
        # runtime mechanism. The service is identity-source-neutral.
        import os as _os_env_worker

        _worker_result = PromptMutator(self.runtime).worker_lane_intercept(
            project_root=project_root,
            worker_lane_id=_os_env_worker.environ.get(
                "AIDOCS_EXPERT_LANE_ID",
                "",
            ).strip(),
            worker_session_id=_os_env_worker.environ.get(
                "AIDOCS_EXPERT_SESSION_ID",
                "",
            ).strip(),
            worker_id=_os_env_worker.environ.get(
                "AIDOCS_EXPERT_ID",
                "",
            ).strip(),
        )
        if _worker_result.rewritten_prompt is not None:
            # Worker turn → also dump the WORKER role (what the seat does)
            # for the AIDOCS-spawned subagent. Best-effort, never blocks.
            _worker_ctx = _worker_result.rewritten_prompt
            try:
                _wrole = self.runtime.hub.skills.read_role("worker")
                if _wrole and _wrole.get("content_text"):
                    _worker_ctx = f"== WORKER ROLE ==\n{_wrole['content_text']}\n\n" + _worker_ctx
            except Exception:
                pass
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _worker_ctx,
                },
            }

        # ── ORIGIN GATE (origin-bound law) ────────────────────────
        # Built ONCE here, before ANY authority-bearing pipeline. Prompt
        # ORIGIN — not prompt shape — decides whether grant / mutation /
        # confirmation logic may run. A worker / -p / -q / delegated /
        # compaction / handoff / replay / tool prompt is INERT for all
        # such consumption even if it contains an exact grant/operator
        # phrase. ALWAYS-SAFE steps already ran above (UPS audit, secret
        # block, preflight, worker-lane intercept). Everything below that
        # grants, mutates, confirms, or dispatches is gated on
        # `_grant_eligible`.
        import os as _os_origin_gate

        try:
            from .identity_resolver import current_principal_type as _cpt

            _origin_principal = _cpt(project_root)
            if not str(_origin_principal or "").strip():
                _origin_principal = "unknown"
        except Exception:
            _origin_principal = "unknown"
        try:
            from .operator_intent_resolver import (
                is_authority_bearing_prompt_eligible as _is_grant_eligible,
            )

            _origin_ctx = {
                "event_kind": "UserPromptSubmit",
                "principal_type": _origin_principal,
                "host_session_id": str(payload.get("session_id") or "").strip(),
                "project_root": str(project_root),
                "worker_lane_id": _os_origin_gate.environ.get(
                    "AIDOCS_EXPERT_LANE_ID",
                    "",
                ).strip(),
                "source_surface": str(payload.get("source_surface") or ""),
                "delivery": str(payload.get("delivery") or ""),
            }
            _grant_eligible = bool(_is_grant_eligible(_origin_ctx))
        except Exception:
            # Fail closed: if the gate can't be evaluated, treat the
            # prompt as ineligible for authority-bearing consumption.
            _grant_eligible = False

        # Confirmation-freeze resolver (#39, 2026-04-25). Single-window
        # contract: exact phrase mints grant, cancel pattern records
        # denial, ANYTHING ELSE keeps the session frozen (a stray/garbage
        # prompt no longer lifts it). AUTHORITY-BEARING: gated on origin.
        _chat_unfreeze_blocks: tuple = ()
        _soul_dump_blocks: list = []
        if _grant_eligible:
            try:
                self._resolve_session_freeze(
                    project_root,
                    prompt,
                    cli_session_id=str(payload.get("session_id") or "").strip(),
                )
            except Exception:
                # Best-effort. Failure here means the freeze sticks until
                # next UPS — annoying but safe (operator retries).
                pass
            # Operator chat-unfreeze: an EXPLICIT, MOTIVATED unfreeze
            # request (NLP "unfreeze the agent because …" OR the literal
            # clear-freeze command) by a permissioned operator lifts ANY
            # freeze (incl. a security lock). Origin-gated here so the
            # frozen AGENT cannot lift its own lock; perms checked inside.
            try:
                from .prompt_mutator import PromptMutator as _PM

                _uf = _PM(self.runtime).resolve_chat_unfreeze(
                    prompt=prompt,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                    project_root=project_root,
                )
                _chat_unfreeze_blocks = tuple(getattr(_uf, "additional_context_blocks", ()) or ())
            except Exception:
                _chat_unfreeze_blocks = ()

            # ── Sovereign soul gate (origin-gated, per-turn) ──────────
            # The Emperor's word opens ONE soul this turn: its content
            # DUMPS into additionalContext (the read), and a per-turn grant
            # is minted for ai_soul WRITES. This whole block is
            # _grant_eligible — only the king's own prompt can open a soul;
            # a worker / delegated / replayed prompt never can. Fail-closed.
            try:
                from .empire_soul_gate import (
                    detect_read_unlocks,
                    detect_write_unlocks,
                    set_turn_grants,
                )

                _soul_sid = ""
                _ms = self.runtime.hub.managed_mode.get_mode(
                    project_root,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                )
                if isinstance(_ms, dict) and _ms.get("active"):
                    _soul_sid = str(_ms.get("session_id") or "").strip()
                _read_souls = detect_read_unlocks(prompt)
                _write_souls = detect_write_unlocks(prompt)
                # CONTRACT (operator, 2026-06-11): souls are NLP-gated
                # TOOLS, not auto-dumped content. The Emperor's read-word
                # mints a per-turn, single-use OP_READ grant so the agent's
                # ai_soul(mode='read') call SUCCEEDS this turn; a write
                # evocation (inscription verb) mints OP_WRITE. The soul is
                # NOT injected into context here — that auto-surface is for
                # ROLE SKILLS on seat ENTER (conductor / co-conductor, via
                # helper_skill_injector), a SEPARATE mechanism. set_turn_grants
                # is unconditional so a prompt naming no soul re-seals the door.
                set_turn_grants(
                    project_root,
                    _soul_sid,
                    read_souls=_read_souls,
                    write_souls=_write_souls,
                )
            except Exception:
                pass

        # Trivial-prompt early-return REMOVED 2026-04-30 (operator
        # doctrine). The block previously here dropped short prompts
        # like "ok"/"yes"/"thanks"/"sure"/"👍" before any AIDOCS
        # pipeline ran — which left a security gap: an attacker could
        # fragment a malicious instruction across turns and use a
        # trivial reply as an unaudited cover turn (no grant detect,
        # no revoke detect, no destructive-intent stamp, no audit
        # emission). The original optimization (avoid context-build
        # cost on conversational filler) was added when AIDOCS managed
        # mode had bugs that made the agent unresponsive to short
        # prompts — those bugs were fixed long ago. Today's contract:
        # AIDOCS managed = pipeline runs on every prompt, period.
        #
        # Autowake fast-path also REMOVED 2026-04-30 along with the
        # rest of the force-wakeup feature — the heuristic detector
        # could not actually achieve its goal (agents could decline to
        # set ScheduleWakeup and stall the session waiting for an
        # autowake reset). See the autowake-removal commit for the
        # full rationale; reuse may revisit this via a stop-hook
        # architecture instead.

        # ── RBAC escalation scrub ─────────────────────────────────
        # Detect `approve: <email> <password>` / `deny: <request_id>`
        # lines and strip credentials from the prompt BEFORE the
        # agent ever sees it. Approvals that authenticate also flip
        # the pending escalation row to approved; the consume pass
        # below picks it up and issues the gate grant. This runs
        # before grant-detection / route-classification so the
        # scrubbed prompt is what downstream logic operates on.
        # Escalation scrub — delegated to PromptMutator (host-agnostic).
        # Strips `approve:`/`deny:` lines from the prompt body before
        # the agent ever sees them; flips matching escalation rows.
        _escalation_side_effects: list[dict[str, object]] = []
        from .prompt_mutator import PromptMutator

        # AUTHORITY-BEARING: flips escalation approve/deny rows. Gated on
        # origin — a worker/delegated prompt cannot consume an approval
        # even if it carries an `approve:`/`deny:` line.
        if _grant_eligible:
            _scrub_result = PromptMutator(self.runtime).escalation_scrub(
                prompt=prompt,
                project_root=project_root,
            )
            if _scrub_result.rewritten_prompt is not None:
                prompt = _scrub_result.rewritten_prompt
                payload["prompt"] = prompt
                _escalation_side_effects = list(_scrub_result.side_effects)

        # Clear previous turn's user-intent tool grants on every new prompt,
        # then apply any grants implied by this prompt BEFORE running route
        # classification. Grants must not depend on downstream context
        # building, which can short-circuit and skip grant application.
        # Cache TTL: zero. The hook is a one-shot subprocess (`python -m
        # aidocs_mcp.claude_hook`) so every event spawns a fresh process
        # with no cross-invocation cache, and ManagedModeService.get_mode
        # reads sqlite on every call (no in-process memoization). A
        # set_mode write from the conductor is therefore visible on the
        # next hook invocation without any invalidation step. Pinned by
        # tests/host/test_claude_hook_managed_mode_cache.py.
        managed = self.runtime.hub.managed_mode.get_mode(project_root)
        session_id = str(managed.get("session_id") or "").strip() if managed.get("active") else ""
        # SEC-001 HOTFIX (2026-04-23): snapshot privilege state BEFORE
        # any mutation so we can restore it if route-validate blocks
        # the prompt. This is a temporary containment — the full fix
        # is the plan-before-apply refactor. Carve-outs
        # (user_prompt_received audit, check_and_update_cli_session_id)
        # already ran above and are NOT restored; they're defensive /
        # audit-chain signals that must fire on every prompt.
        _sec001_snapshot: dict[str, object] = {}
        if session_id:
            try:
                _sec001_snapshot = self.runtime.hub.query_gate.snapshot_privilege_state(
                    project_root,
                    session_id,
                )
            except Exception:
                _sec001_snapshot = {}

        # SEC-002 atomic mutation stage (2026-04-23). The whole
        # privilege-mutation block is wrapped in one try/except. On
        # any exception: restore the pre-mutation snapshot, emit
        # prompt_mutation_failed with the failing site + exception
        # info, set degraded_state. Goal is "no partial state +
        # visible failure," not block the operator.
        _sec002_tripped = False

        # AUTHORITY-BEARING privilege-mutation stage. ORIGIN-BOUND: runs
        # only for a bound session AND an authority-bearing origin. A
        # worker / -p / -q / delegated / compaction / handoff / replay
        # prompt skips the ENTIRE block — no sticky grants, no user-intent
        # grants, no per-turn intent state, no DNT/config-set grants, no
        # lane-exit, no approval peek — even if it contains an exact grant
        # phrase.
        if session_id and _grant_eligible:
            try:
                # Per-turn TTL: drop non-sticky user-intent grants from the
                # previous turn while preserving the sticky baseline. An
                # explicit "revoke sticky" phrase in this prompt clears the
                # sticky slice as well before the new-turn detection runs.
                # Sticky-grant lifecycle (revoke / clear_expired /
                # clear_turn_edited) — delegated to PromptMutator.
                PromptMutator(self.runtime).apply_sticky_grant_lifecycle(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                # Resolve prior-turn pending sticky grants (Phase 3 of
                # backlog #15). Operator answered "yes" on an AskUserQuestion
                # → register the grant. "no" → drop. Any other reply or no
                # pending → clear_expired_pending sweeps stale rows so the
                # single-turn TTL holds.
                # Sticky-grant answer consumption + user-intent tool
                # grants — both delegated to PromptMutator.
                PromptMutator(self.runtime).consume_sticky_grant_answers(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                PromptMutator(self.runtime).apply_user_intent_tool_grants(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                # SEC-001 hotfix (2026-04-23): PEEK not CONSUME. Pre-hotfix
                # this called consume_approvals_for_session which burned
                # the admin approval unconditionally on every prompt. Now
                # we read pending approvals WITHOUT consuming — the
                # matching-action-consume path (SEC-003 proper fix) will
                # land later. Until then this path only SURFACES the
                # pending approvals for audit/telemetry; actual grant
                # application happens at tool-call time when the action
                # matches the approval's capability.
                try:
                    from .escalation_hook import (
                        peek_approved_for_session as _peek_escalations,
                    )

                    _approved = _peek_escalations(project_root, session_id)
                    for _req in _approved:
                        _escalation_side_effects.append(
                            {
                                "kind": "escalation.pending",
                                "request_id": _req.request_id,
                                "gate_permission": _req.gate_permission,
                                "sticky": _req.sticky,
                            },
                        )
                except Exception:
                    pass
                # Per-turn intent-state mutations — delegated to
                # PromptMutator.apply_per_turn_intent_state. Bundles 4
                # coupled state writes (bash subcommand grants, ask-state
                # plumbing, credential stash, destructive stash) that all
                # key on the managed session_id. Each sub-mutation is
                # independent inside the service so a single failure
                # doesn't suppress the others.
                from .prompt_mutator import PromptMutator

                PromptMutator(self.runtime).apply_per_turn_intent_state(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                # DNT (DO-NOT-TOUCH) grants — delegated to PromptMutator
                # (host-agnostic). Writes both per-process module state
                # AND sqlite (#236 2026-05-12 cross-process truth).
                PromptMutator(self.runtime).apply_dnt_grants(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                )
                # Config-set grants — delegated to PromptMutator
                # (host-agnostic).
                PromptMutator(self.runtime).apply_config_set_grants(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                )

                # Conductor lane-exit escape hatch — delegated to PromptMutator.
                # Workers self-fenced via env; sticky auto-exit conditional
                # on no-live-worker check.
                import os as _os_lane_exit

                PromptMutator(self.runtime).apply_lane_exit_grant(
                    prompt=prompt,
                    managed_session_id=session_id,
                    project_root=project_root,
                    is_worker_proc=bool(
                        _os_lane_exit.environ.get(
                            "AIDOCS_EXPERT_LANE_ID",
                            "",
                        ).strip(),
                    ),
                )
            except Exception as _sec002_exc:
                # SEC-002 (2026-04-23) atomic mutation stage.
                # On any exception escaping the inner per-site nets:
                # restore the pre-mutation snapshot, emit
                # prompt_mutation_failed, set _sec002_tripped as a
                # visible degraded-state flag (session-scoped via the
                # audit event payload). Carve-outs above (audit,
                # cli_session_id) already ran and are NOT rolled back.
                _sec002_tripped = True
                if _sec001_snapshot:
                    try:
                        self.runtime.hub.query_gate.restore_privilege_state(
                            project_root,
                            session_id,
                            dict(_sec001_snapshot),
                        )
                    except Exception:
                        pass
                _sec002_event_id = ""
                try:
                    _sec002_event_id = (
                        self.runtime.hub.execution.record_event(
                            project_root,
                            event_kind="prompt_mutation_failed",
                            source_kind="sec002_atomic_stage",
                            session_id=session_id or None,
                            capability_name="UserPromptSubmit",
                            action_kind="mutation_error",
                            status="rolled_back",
                            payload={
                                "exception_type": type(_sec002_exc).__name__,
                                "exception_msg": str(_sec002_exc)[:200],
                            },
                        )
                        or ""
                    )
                except Exception:
                    pass
                # SEC-005 (2026-04-23): surface degraded_state on the
                # session row so the dashboard top bar + right-panel
                # strip render the red badge without a second query.
                # Reason string is exception_type:msg to keep the UI
                # short; full payload stays in the audit event.
                if session_id:
                    try:
                        _reason = f"{type(_sec002_exc).__name__}: {str(_sec002_exc)[:140]}"
                        self.runtime.hub.query_gate.set_degraded_state(
                            project_root,
                            session_id,
                            reason=_reason,
                            failure_event_id=str(_sec002_event_id),
                        )
                    except Exception:
                        pass

        # DNT grants for UNMANAGED projects (operator repro 2026-06-11,
        # DentalApp): the privilege-mutation block above is gated on
        # `session_id and _grant_eligible`, and session_id is only set
        # for MANAGED sessions — so in an unmanaged project the literal
        # "protect <path>" phrase never even reached the parser and
        # ai_protect was refused by construction. DNT authority is the
        # human's direct word about FILES, not session state: run the
        # same deterministic literal parser with no session bound. The
        # origin gate (_grant_eligible) still applies — worker/-p/
        # compaction/replay prompts mint nothing. Grants land under the
        # '__unbound__' sqlite key (see prompt_mutator) which the
        # ai_protect read side always checks.
        if _grant_eligible and not session_id:
            try:
                PromptMutator(self.runtime).apply_dnt_grants(
                    prompt=prompt,
                    managed_session_id="",
                    project_root=project_root,
                )
            except Exception:
                pass

        # Closed-vocabulary intent-phrase detection runs before route
        # classification so state changes (plan_session_enter, etc.) are
        # visible to downstream context-building. Intent dispatch results
        # are appended to the additional_context block below so the agent
        # sees the activation acknowledgment ("Plan mode active. Scope:
        # ...") in the same turn that triggered it.
        # Intent-phrase dispatch — delegated to PromptMutator
        # (host-agnostic). AUTHORITY-BEARING: dispatches state changes
        # (plan_session_enter etc.) from intent phrases — gated on origin
        # so a worker/delegated prompt cannot trigger them.
        intent_dispatch_results: list[dict[str, object]] = []
        if _grant_eligible:
            _intent_dispatch_result = PromptMutator(self.runtime).intent_phrase_dispatch(
                prompt=prompt,
                managed_session_id=session_id or "",
                project_root=project_root,
            )
            intent_dispatch_results = [
                {"context": block} for block in _intent_dispatch_result.additional_context_blocks
            ]

        # UPS consumes no freshness field from host_state (only session_id), so
        # skip the exact per-prompt SHA freshness walks: verify_index=False yields
        # an honest "unverified" index status. SessionStart and the
        # status/sync/diagnostic tools keep verify_index=True (exact). The former
        # request_config_scope() wrapper here existed solely to batch the per-UPS
        # config-read storm that the code-freshness walk produced; with the walk
        # skipped that storm is gone, so the wrapper is removed.
        host_state = self.runtime.host_state(
            project_root, prompt_text=prompt, verify_index=False,
        )
        prompt_state = (
            host_state.get("prompt_state")
            if isinstance(host_state.get("prompt_state"), dict)
            else {}
        )
        action_kind = str(prompt_state.get("action_kind") or "understand")
        route = self.runtime.aidocs_route_prompt(
            project_root,
            user_request=prompt,
            action_kind=action_kind,
        )

        # SEC-001 HOTFIX (2026-04-23): helper to restore privilege
        # state before returning a block decision. Snapshot was taken
        # before any mutation; restore writes it back verbatim.
        # Called inline (not a closure) so the audit event emits once
        # per actual rollback, not per block-branch.
        def _sec001_restore_and_audit(reason_tag: str) -> None:
            if not session_id or not _sec001_snapshot:
                return
            try:
                self.runtime.hub.query_gate.restore_privilege_state(
                    project_root,
                    session_id,
                    _sec001_snapshot,
                )
                self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind="prompt_mutation_rolled_back",
                    source_kind="sec001_hotfix",
                    session_id=session_id,
                    capability_name="UserPromptSubmit",
                    action_kind="rollback",
                    status="rolled_back",
                    payload={"reason_tag": reason_tag},
                )
            except Exception:
                # Never let restore itself break the block path.
                pass

        if not route.get("managed_mode"):
            # Self-heal auto-bind — delegated to PromptMutator.
            # When a session was auto-bound, we re-fetch the route
            # to pick up the new managed_mode state. If nothing was
            # bound (truly uninitialized project), fall through to
            # the block envelope.
            auto_bound = False
            _ab_result = PromptMutator(self.runtime).auto_bind_session(
                project_root=project_root,
            )
            if _ab_result.why and _ab_result.why[0] == "auto_bind_session":
                route = self.runtime.aidocs_route_prompt(
                    project_root,
                    user_request=prompt,
                    action_kind=action_kind,
                )
                auto_bound = bool(route.get("managed_mode"))
            if not auto_bound:
                _sec001_restore_and_audit("managed_mode_inactive")
                return {
                    "decision": "block",
                    "reason": "Run /aidocs first to activate AIDOCS-managed mode for this project.",
                }

        if route.get("blocked_reason"):
            _sec001_restore_and_audit("route_blocked_reason")
            blocked_reason = str(
                route.get("blocked_reason") or "This prompt is blocked by AIDOCS runtime policy.",
            )
            return {
                "decision": "block",
                "reason": blocked_reason,
            }

        # Trivial-prompt gate deleted 2026-04-24: it was suppressing the
        # NLP tool-surfacing hint on short prompts like "git pdf" even
        # when the prompt carried real tool intent. The hint is cheap
        # (frozenset lookup + optional fuzzy) and genuinely useful even
        # on 2-word prompts. If over-chatter re-emerges on pure
        # conversational noise, gate inside _build_lightweight_prompt_context
        # on tool-hint emptiness instead of on word count.
        additional_context = self._build_lightweight_prompt_context(
            action_kind=action_kind,
            route=route,
            project_root=project_root,
            host_state=host_state,
            prompt=prompt,
            cli_session_id=str(payload.get("session_id") or "").strip(),
        )

        # Strike-count visibility (operator directive 2026-06-11). The
        # repeated-security-violation counter is invisible until the freeze
        # detonates — so an agent has no idea it's at 2/3 until the 3rd
        # strike locks the session (admin-clear only). Surface it when
        # nonzero so the agent can self-correct BEFORE the freeze. Benign
        # indexed-source reads are friction (no strike); this only climbs on
        # real flat security denials (secret reads, sensitive paths, etc.).
        if session_id:
            try:
                from .security_violation_service import SecurityViolationService

                _peak, _thr = SecurityViolationService(
                    self.runtime.hub,
                ).peak_strike_count(project_root, session_id)
                if _peak >= 1 and _thr >= 1:
                    _strike_note = (
                        f"⚠ Security strikes this session: {_peak}/{_thr}. "
                        f"At {_thr} the session FREEZES (admin-clear only). "
                        f"This counts real flat security denials (reading a "
                        f"SECRET/sensitive path via a command, etc.) — NOT "
                        f"benign source reads. Use ai_find / ai_get_lines for "
                        f"indexed project source; never grep/cat it via ai_run."
                    )
                    additional_context = (
                        (additional_context or "")
                        + ("\n\n" if additional_context else "")
                        + _strike_note
                    )
            except Exception:
                pass

        # Surface the operator chat-unfreeze result (✅ cleared / 🛑 needs
        # perms-or-reason) computed in the origin-gated block above.
        for _block in _chat_unfreeze_blocks:
            if _block:
                additional_context = (
                    (additional_context or "")
                    + ("\n\n" if additional_context else "")
                    + str(_block)
                )

        # Dump any soul(s) the Emperor's word opened this turn — sovereign
        # content injected into context (the read surface). Origin-gated
        # (built only when _grant_eligible); private to the seat.
        for _block in _soul_dump_blocks:
            if _block:
                additional_context = (
                    (additional_context or "")
                    + ("\n\n" if additional_context else "")
                    + str(_block)
                )

        # ── Auto-task (friction removal) ───────────────────────────
        # An imperative ("commit and push") or investigation question
        # ("did I set the address?") opens a task in sqlite if none is
        # active for this session, so the agent doesn't have to call
        # task_begin by hand. Answerable prompts ("did you commit?",
        # "thanks") open nothing. SQL-only (no SESSION.md/PLAN.md) per
        # the no-file-layer doctrine; best-effort — a store hiccup never
        # blocks the prompt.
        #
        # ORIGIN-BOUND: task lifecycle is law-adjacent state, so only an
        # authority-bearing OPERATOR prompt may auto-open a task. Worker /
        # delegated / -p / -q / replayed prompts are inert here (same
        # _grant_eligible gate the grant/mutation pipeline obeys) — a
        # sub-agent must not mutate the session's task lifecycle.
        if session_id and _grant_eligible:
            try:
                from .prompt_intent_classifier import classify_prompt_intent

                _intent = classify_prompt_intent(prompt)
                if _intent in ("imperative", "investigation"):
                    _kind = "investigation" if _intent == "investigation" else "work"
                    _opened = self.runtime.auto_task_begin(
                        project_root,
                        session_id,
                        goal=prompt[:200],
                        kind=_kind,
                        origin_prompt=prompt,
                    )
                    if _opened is not None:
                        _task_note = (
                            f"Auto-started {_kind} task "
                            f"`{_opened['task_id']}` (goal: {_opened['goal']}). "
                            f"Use task_update / task_complete as you work; "
                            f"no need to call task_begin."
                        )
                        additional_context = (
                            (additional_context or "")
                            + ("\n\n" if additional_context else "")
                            + _task_note
                        )
            except Exception:
                # Auto-task is a convenience; never block a prompt on it.
                pass

        # ── Operator-intent resolution (first vertical slice) ──────
        # Runs AFTER the NLP grant/intent extraction above. Maps an
        # authenticated operator's natural-language control-plane
        # request ("enable decision trace for this session") into a
        # structured, host_binding-authenticated, RBAC-gated, audited
        # mutation via the canonical config service. NLP authorizes
        # nothing; host_binding proves WHO; the permission service
        # decides IF; the canonical service performs the write; the
        # resolver audits the seal. A non-human principal (an MCP tool
        # or sub-agent) is refused inside resolve_and_apply before any
        # identity is resolved — guardrails cannot be self-unlocked.
        # Reuses the single origin gate (_grant_eligible) computed once
        # above — operator intent is one authority-bearing consumer among
        # many and obeys the same origin-bound law. A worker/-p/-q/
        # delegated/replayed prompt is inert: we do not even parse it.
        if _grant_eligible:
            try:
                from .operator_intent_resolver import OperatorIntentResolver

                _intent_outcome = OperatorIntentResolver().resolve_and_apply(
                    prompt,
                    project_root=project_root,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                    principal_type=_origin_principal,
                    confirm_phrase=prompt,
                )
                if _intent_outcome is not None:
                    _note = self._operator_intent_note(_intent_outcome)
                    if _note:
                        additional_context = (
                            (additional_context or "")
                            + ("\n\n" if additional_context else "")
                            + _note
                        )
            except Exception:
                # Best-effort surface; an intent-resolution hiccup never
                # blocks the operator's prompt.
                pass

        # Intent-dispatch results piggy-back on the same context block
        # so the agent sees state-change acknowledgments ("Plan mode
        # active") in the same turn the operator's phrase fired. The
        # append happens AFTER lightweight_prompt_context so dispatch
        # outcomes appear after the standard managed-mode header.
        intent_context_parts = [
            str(r.get("context", "")).strip() for r in intent_dispatch_results if r.get("context")
        ]
        if intent_context_parts:
            additional_context = (additional_context or "") + " " + " ".join(intent_context_parts)

        # CC-only sub-pipelines that mutate_prompt now composes (above)
        # are already invoked individually earlier in this handler,
        # each wrapped by the SEC-001/002 snapshot/restore transactional
        # contract. mutate_prompt's full composition is the canonical
        # entry point for HOSTS THAT DON'T HAVE THAT WRAPPER (OpenCode,
        # OpenAI Agents, host_adapter_cli).
        #
        # Calling mutate_prompt here would double-fire every sub-pipeline
        # because the wrapping plus the canonical call would each
        # invoke them. Two paths to deduplication later:
        #   (a) collapse CC's individual calls to one mutate_prompt
        #       inside the SEC-001 wrapper (deferred — large surgery)
        #   (b) parameterize mutate_prompt with a "skip_*" set so
        #       CC can call only the not-yet-fired tail
        # The dashboard_config_advisory and notifications_drain tail
        # are currently the only sub-pipelines CC does NOT fire
        # individually — invoke them directly here to preserve the
        # pre-canonical behavior without duplicating the rest.
        try:
            from .prompt_mutator import PromptMutator

            pm = PromptMutator(self.runtime)
            # dashboard_config_advisory is AUTHORITY-ADJACENT: it runs a
            # config-grant SHAPE detector (detect_config_grants_v2) on the
            # prompt. ORIGIN-BOUND: no grant detector — even advisory-only
            # — runs on an ineligible origin. notifications_drain is
            # ALWAYS-SAFE informational.
            if _grant_eligible:
                _adv = pm.dashboard_config_advisory({"prompt": prompt}, project_root)
            else:
                from .prompt_mutator import PromptMutationResult

                _adv = PromptMutationResult.empty()
            _drain = pm.notifications_drain(
                {"prompt": prompt, "session_id": str(payload.get("session_id") or "")},
                project_root,
            )
            _tail_blocks = list(_adv.additional_context_blocks) + list(
                _drain.additional_context_blocks,
            )
            if _tail_blocks:
                # The dashboard advisory historically concatenated with
                # NO separator (space-prefixed inline); the drain block
                # used \n\n separators. Preserve both shapes.
                advisory_inline = (
                    _adv.additional_context_blocks[0] if _adv.additional_context_blocks else ""
                )
                drain_block = "\n\n".join(
                    _drain.additional_context_blocks,
                )
                if advisory_inline:
                    additional_context = (additional_context or "") + advisory_inline
                if drain_block:
                    additional_context = (
                        (additional_context or "")
                        + ("\n\n" if additional_context else "")
                        + drain_block
                    )
        except Exception:
            pass

        if not additional_context:
            return None

        self._record_classification_event(project_root, action_kind, prompt)

        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            },
        }

    # ── User-intent raw tool grants ──
    # Grant raw tool access for the next tool call when the user's prompt
    # contains BOTH a grant verb ("allow", "enable", "use", "run"...) AND
    # a tool token ("grep", "bash", "glob"...) within proximity of each
    # other. Requiring both stops casual mentions ("I usually grep for X",
    # "in bash you'd...") from unlocking raw tools. Cleared on next
    # UserPromptSubmit.

    # Legacy indirect phrasings that imply a raw tool without naming it.
    # Each phrase is matched as a substring and, when present, directly
    # grants the mapped tool (these phrases are specific enough that a
    # separate grant verb is not required).
    _DIRECT_INTENT_PHRASES: dict[str, list[str]] = {
        "grep": ["grep for", "search the files for", "find in files", "rg "],
        "read": [
            "read the file",
            "read file",
            "cat the",
            "show me the file",
            "open the file",
        ],
        "glob": ["find files named", "list the files", "ls "],
        "edit": ["sed ", "edit the file"],
        "write": ["write to the file", "save to file", "save to the file"],
        "bash": ["run `", "execute `", "run the script", "run the command"],
    }

    # Observational adverbs that, when they immediately precede a direct
    # intent phrase, indicate the user is describing a habit or reporting
    # past behavior rather than issuing a command ("I usually grep for...",
    # "typically we cat the logs"). Suppress the grant in that case.
    _OBSERVATIONAL_PREFIXES: tuple[str, ...] = (
        "usually ",
        "typically ",
        "normally ",
        "sometimes ",
        "often ",
        "occasionally ",
        "would ",
        "used to ",
        "tend to ",
    )

    # Grant verbs — must be followed (within proximity) by a tool token to
    # trigger a grant. Ordering in this list does not matter; the matcher
    # checks for any verb followed by any tool token within _GRANT_PROXIMITY
    # characters.
    _GRANT_VERB_PHRASES: tuple[str, ...] = (
        # Direct permission grants.
        "i allow",
        "allow the use",
        "allow the raw",
        "allow raw",
        "allow you to use",
        "allow you to run",
        "you can use",
        "you may use",
        "you're allowed to",
        "you are allowed to",
        "feel free to use",
        "go ahead and use",
        "go ahead and run",
        "it's ok to use",
        "it is ok to use",
        "i authorize",
        "permission to use",
        "permission to run",
        "enable ",
        "whitelist ",
        "unblock ",
        # Natural affirmation forms — still need a nearby tool token to fire
        # (the co-occurrence guard in _grant_user_intent_tools handles that).
        # Chosen to match what operators actually say: "access granted to
        # bash", "approved to use psql", "yes use grep", "ok run pytest".
        "access granted",
        "granted access",
        "approved to",
        "approved for",
        "approval to",
        "greenlight",
        "green light",
        "go grant",
        "grant access",
        "i grant",
        "you're cleared to",
        "you are cleared to",
        "cleared to use",
        "cleared to run",
        "ok use",
        "ok run",
        "yes use",
        "yes run",
        "proceed with",
        "proceed to",
    )

    # Raw tool tokens — each must appear as a whole word so "readable" does
    # not match "read" and "grepping" does not match "grep". Mapping from
    # tool token regex to the gate's tool key.
    _TOOL_TOKEN_PATTERNS: dict[str, str] = {
        r"\bgrep\b": "grep",
        r"\bglob\b": "glob",
        r"\bbash\b": "bash",
        r"\bshell\b": "bash",
        r"\bread\b": "read",
        r"\bedit\b": "edit",
        r"\bwrite\b": "write",
    }

    # Maximum distance (characters) between the end of a grant verb and the
    # start of a tool token for them to count as a single grant statement.
    # Chosen to cover phrases like "allow the use of grep/bash/glob tools"
    # without matching unrelated sentences in the same prompt.
    _GRANT_PROXIMITY = 60

    def _consume_sticky_grant_answers(
        self,
        project_root: Path,
        session_id: str,
        prompt: str,
        resolved: list[tuple[str, str]],
    ) -> None:
        """Thin delegate to PromptMutator.consume_sticky_grant_answers.
        Back-compat method kept so any direct caller continues to
        work. The ``resolved`` out-parameter is populated from the
        service result for caller compatibility.
        """
        from .prompt_mutator import PromptMutator

        r = PromptMutator(self.runtime).consume_sticky_grant_answers(
            prompt=prompt,
            managed_session_id=session_id,
            project_root=project_root,
        )
        # Reconstruct (tool, answer) tuples from why+side_effects
        if r.why and r.why[0] == "sticky_answers_resolved":
            answer = r.why[1] if len(r.why) >= 2 else "yes"
            for se in r.side_effects:
                # "sticky grant <answer>: <tool>"
                if ":" in se:
                    tool = se.split(":", 1)[1].strip()
                    resolved.append((tool, answer))

    def _grant_user_intent_tools(self, project_root: Path, session_id: str, prompt: str) -> None:
        """Thin delegate to PromptMutator.apply_user_intent_tool_grants.
        Back-compat method kept so any direct caller continues to
        work. All logic now in the host-agnostic service.
        """
        from .prompt_mutator import PromptMutator

        PromptMutator(self.runtime).apply_user_intent_tool_grants(
            prompt=prompt,
            managed_session_id=session_id,
            project_root=project_root,
        )

    def _handle_stop(
        self,
        project_root: Path,
        payload: dict[str, object],
        event_name: str,
    ) -> dict[str, object] | None:
        """Stop / SubagentStop — audit (LifecycleService, host-agnostic)
        plus the Failure Stewardship turn gate.

        The audit half never blocks. The stewardship half DOES: it
        registers every pytest failure observed this turn into the
        project's PERSISTENT ledger and blocks the turn from sealing
        when the agent's final report carries an unproven excuse phrase
        ("pre-existing", "not my bug", "flaky", …) or leaves a failure
        it owns untriaged. This is what makes "find out why before you
        say 'not my fault'" a deterministic code rule, not a prompt.
        """
        from .lifecycle_service import LifecycleService

        LifecycleService(self.runtime).on_assistant_turn_end(
            event_name=event_name,
            project_root=project_root,
            payload=payload,
        )

        gate = self._failure_stewardship_gate(project_root, payload)
        if gate is not None and not gate.ok:
            return {"decision": "block", "reason": gate.block_reason}
        return None

    def _failure_stewardship_gate(
        self,
        project_root: Path,
        payload: dict[str, object],
    ):
        """Run the failure-stewardship turn gate. Fail-OPEN on any
        internal error — a hook crash must never wedge the agent — but
        enforce whenever we successfully observe a failure or an
        unproven excuse phrase. Returns a StopGateResult or None.
        """
        try:
            from . import failure_stewardship as fs

            session_id = str(payload.get("session_id") or "").strip()
            report_text = str(payload.get("message") or "")
            scan_text = self._read_turn_scan_text(payload, report_text)
            return fs.evaluate_turn(
                project_root=project_root,
                session_id=session_id,
                scan_text=scan_text,
                report_text=report_text,
            )
        except Exception:
            return None

    @staticmethod
    def _read_turn_scan_text(payload: dict[str, object], report_text: str) -> str:
        """Assemble the text scanned for pytest failure REGISTRATION.

        Scoped to TOOL-RESULT OUTPUTS only — i.e. the stdout/stderr of
        commands the agent ran (where a real `=== short test summary info`
        block lives). Deliberately EXCLUDES (a) the agent's report/message
        (that is lint-only, never registration — `report_text` is unused
        here) and (b) tool-call INPUTS such as file-write contents (so a
        `FAILED tests/x.py::t …` literal inside a test fixture the agent is
        WRITING never registers a phantom). This is the source-side half of
        the 2026-05-31 phantom-registration fix; the parser nodeid filter +
        green-run auto-clear are the other halves.
        """
        transcript_path = str(payload.get("transcript_path") or "").strip()
        if not transcript_path:
            return ""
        chunks: list[str] = []
        try:
            import json as _json

            p = Path(transcript_path)
            if p.is_file():
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                for raw in lines[-400:]:
                    raw = raw.strip()
                    if not raw or ("FAILED " not in raw and "passed" not in raw):
                        continue
                    try:
                        node = _json.loads(raw)
                    except Exception:
                        continue
                    # ONLY tool_result content — never assistant text or
                    # tool_use inputs.
                    text = ClaudeHookHandler._extract_tool_result_text(node)
                    if text:
                        chunks.append(text)
        except Exception:
            pass
        return "\n".join(chunks)

    @staticmethod
    def _extract_tool_result_text(obj: object) -> str:
        """Collect text ONLY from `tool_result` content blocks (command
        outputs). Ignores assistant text and `tool_use` inputs, so file
        contents being written / report prose never reach the failure
        parser.
        """
        out: list[str] = []
        if isinstance(obj, dict):
            if obj.get("type") == "tool_result":
                out.append(ClaudeHookHandler._extract_text(obj.get("content")))
            else:
                for v in obj.values():
                    out.append(ClaudeHookHandler._extract_tool_result_text(v))
        elif isinstance(obj, list):
            for v in obj:
                out.append(ClaudeHookHandler._extract_tool_result_text(v))
        return "\n".join(c for c in out if c)

    @staticmethod
    def _extract_text(obj: object) -> str:
        """Recursively collect string values from a transcript JSON
        node (handles {content:[{type:text,text:..}]} and tool_result
        content shapes without assuming an exact schema).
        """
        out: list[str] = []
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                out.append(ClaudeHookHandler._extract_text(v))
        elif isinstance(obj, list):
            for v in obj:
                out.append(ClaudeHookHandler._extract_text(v))
        return "\n".join(c for c in out if c)

    def _handle_post_compact(
        self,
        project_root: Path,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Reset token counters + rotate agent_memory_epoch after compaction.

        2026-05-03 fix (king directive): also rotates the agent_memory_epoch
        via bump_compaction_count so once-per-epoch memory (helper skills,
        DNT banners, etc.) re-injects on the next prompt. Prior version
        had a comment claiming it did this but never called the function.
        Truth in comments now matches truth in code.

        The session bind itself (managed_mode active + bound_by_boot_token)
        is already preserved across compaction — same MCP process, same
        host session UUID, no requires_reconnect trigger. The agent doesn't
        need a "you're bound" reminder; it just needs the epoch rotation
        to receive fresh memory cues.
        """
        # PostCompact side effects — delegated to LifecycleService
        # (host-agnostic). The same handler powers OpenCode's
        # experimental.session.compacting hook (via python subprocess)
        # and any future Codex adapter.
        from .lifecycle_service import LifecycleService

        host_session_id = ""
        if payload:
            host_session_id = str(payload.get("session_id") or "").strip()
        _lifecycle = LifecycleService(self.runtime).on_post_compact(
            host_kind="claude_code",
            host_session_id=host_session_id,
            project_root=project_root,
        )
        # PostCompact side-effects (token counter reset, epoch bump,
        # compaction-grace stamp) run above. CC's hook schema rejects
        # hookSpecificOutput with hookEventName='PostCompact', and the
        # agent doesn't need to know about internal bookkeeping.
        return {}

    def _handle_pre_tool_use(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """CC adapter: translate PreToolUse → ToolGate.evaluate_tool.

        Calls the canonical pretool pipeline once with CC-specific
        GateHooks that render hookSpecificOutput shapes for deny /
        ask / freeze / continue + additionalContext. No CC-side
        sub-gate recomposition — every gate decision lives in
        ToolGate.evaluate_tool.
        """
        from .mcp_server_runtime_helpers import is_aidocs_managed as _is_aidocs_project
        from .tool_gate_service import GateHooks, ToolGate

        tool_name = str(payload.get("tool_name") or "").strip()
        tool_input = (
            payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        )
        # #58 conductor identity: Claude Code stamps its per-process
        # UUID as payload.session_id on every hook fire.
        cli_session_id = str(payload.get("session_id") or "").strip()

        # Lane-worker plumbing — env-keyed side effects, not gate
        # decisions. Run around the evaluate_tool call.
        import os as _os_lw

        _worker_id = _os_lw.environ.get("AIDOCS_EXPERT_ID", "").strip()
        _worker_session_id = _os_lw.environ.get(
            "AIDOCS_EXPERT_SESSION_ID",
            "",
        ).strip()
        _worker_lane_id = _os_lw.environ.get(
            "AIDOCS_EXPERT_LANE_ID",
            "",
        ).strip()

        gate = ToolGate(self.runtime)

        # Batch 2.0-A: when shell-enforcement is live, host-native shell
        # tools (Bash/PowerShell/cmd) are owned by ShellPolicy/
        # ShellEnforcement — structural gates first, then ShellEnforcement
        # as the single authority + single freeze minter. NO native process
        # runs (execute_native/allow → native-deny + ai_run). Returns the
        # envelope directly, bypassing the normal orchestrator slice (so no
        # double freeze mint and no kill-switch native-allow). Off by
        # default; non-native tools and flag-off are unaffected.
        #
        # FAIL CLOSED: for a flag-on host-native shell tool, ANY error
        # (including the adapter import itself) must DENY — never fall
        # through to the normal pipeline, which could let the host run the
        # process. Native detection uses shell_envelope directly so it
        # holds even if shell_adapter cannot be imported. (Monitor is a
        # read/status surface and is intentionally NOT enforced in 2.0-A.)
        _native_shell_enforced = False
        _native_detection_failed = False
        try:
            from .shell_envelope import (
                TRANSPORT_HOST_NATIVE,
                detect_provider_and_transport,
            )

            _, _transport = detect_provider_and_transport(tool_name)
            if _transport == TRANSPORT_HOST_NATIVE:
                from .config import get_setting

                _native_shell_enforced = bool(
                    get_setting(
                        "tools.shell_enforcement_live",
                        project_root=project_root,
                        default=False,
                    ),
                )
        except Exception:
            # Detection / config lookup itself threw — we cannot prove the
            # tool is non-native or that enforcement is off.
            _native_detection_failed = True

        # Last-resort literal fallback: if detection/config was
        # indeterminate AND the tool NAME literally looks like a host-native
        # shell, fail closed (a Bash/PowerShell/cmd call must never slip
        # through on an indeterminate enforcement state). Monitor is NOT in
        # this set — it is a read/status surface, excluded from 2.0-A.
        if _native_detection_failed:
            _bn = (tool_name or "").strip().lower()
            for _pre in ("mcp__aidocs__", "mcp__"):
                _bn = _bn.removeprefix(_pre)
            _bn = _bn.removesuffix(".exe")
            if _bn in (
                "bash",
                "sh",
                "zsh",
                "wsl",
                "powershell",
                "pwsh",
                "cmd",
            ):
                _native_shell_enforced = True

        if _native_shell_enforced:
            try:
                from .shell_adapter import native_shell_2a_pretool

                _na_env = native_shell_2a_pretool(
                    runtime=self.runtime,
                    hub=self.runtime.hub,
                    project_root=project_root,
                    host="claude_code",
                    tool_name=tool_name,
                    tool_input=(tool_input if isinstance(tool_input, dict) else {}),
                    host_session_id=cli_session_id or "",
                    tool_use_id=str(payload.get("tool_use_id") or ""),
                )
            except Exception:
                _na_env = None
            # A flag-on native-shell call MUST be handled by the adapter.
            # If it returned an envelope, use it; if it raised or returned
            # None unexpectedly, fail closed — do not fall through.
            if _na_env is not None:
                return _na_env
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "native shell enforcement failed; action remains "
                        "blocked. Use ai_run(command=...) instead."
                    ),
                    "blocked_by": "shell_enforcement_error",
                },
            }

        # Phoenix §VIII deny-path stamp BEFORE the pipeline runs.
        gate.stamp_lane_worker_host_session_id(
            host_session_id=cli_session_id or "",
            worker_id=_worker_id,
            worker_lane_id=_worker_lane_id,
            project_root=project_root,
        )
        # Lane-worker auto-bind BEFORE evaluate_tool so a worker's
        # first non-bootstrap call passes managed_mode_required.
        # Idempotent on already-active managed_mode.
        gate.auto_bind_lane_worker_managed_mode(
            worker_id=_worker_id,
            worker_session_id=_worker_session_id,
            worker_lane_id=_worker_lane_id,
            project_root=project_root,
        )
        try:
            _lane_id_for_audit = self._get_current_lane_id(project_root)
        except Exception:
            _lane_id_for_audit = None

        # CC-specific envelope rendering hooks. Each returns the
        # exact hookSpecificOutput dict CC used to build inline.
        def _cc_on_allow(result):
            # Kill-switch bypass: write CC-specific audit; CC's
            # "let proceed" signal is the outer return None.
            if "kill_switch_bypass" in (result.why or ()):
                self._log_enforcement_bypass(
                    project_root,
                    tool_name,
                    payload,
                )

        # blocked_by markers — the gates that emit a (marker, blocked_by)
        # pair in their why tuple. evaluate_tool accumulates why
        # across all gates, so we can't read why[1] verbatim; scan
        # for the marker instead.
        _BLOCKED_BY_AFTER_MARKER = (
            "orchestrator_deny",
            "agent_brief_blocked",
            "reconnect_required",
        )
        # Gates whose denials historically omitted blocked_by from
        # hookSpecificOutput — preserve that exact shape.
        _NO_BLOCKED_BY_PREFIX = (
            "managed_mode_required",
            "conductor_comms",
        )

        def _cc_on_deny(result):
            why_list = list(result.why or ())
            # Find the deciding gate's marker in the accumulated tuple.
            # The deciding marker is the LAST one (gates short-circuit
            # on terminal; later why entries come from the gate that
            # produced the terminal verdict).
            decider = why_list[-1] if why_list else ""
            # Find blocked_by either by trailing position after a
            # known (marker, blocked_by) gate, or by looking at the
            # LAST why entry when the gate's convention is to put
            # blocked_by as the second element.
            blocked_by = ""
            for i, marker in enumerate(why_list):
                if marker in _BLOCKED_BY_AFTER_MARKER and i + 1 < len(why_list):
                    blocked_by = str(why_list[i + 1])
                    break
            # Some gates (managed_mode_required, conductor_comms)
            # historically omitted blocked_by entirely. Detect them
            # by their why-marker prefix.
            for prefix in _NO_BLOCKED_BY_PREFIX:
                if decider.startswith(prefix) or any(w.startswith(prefix) for w in why_list):
                    blocked_by = ""
                    break
            return self._deny_envelope(result.reason or "", blocked_by)

        def _cc_on_ask(result):
            # Sticky-grant-pending uses ask_kind="sticky_grant_registration".
            # Orchestrator freeze ask is intercepted by on_freeze
            # (which fires first when a FREEZE marker is present),
            # so any ask reaching here is the sticky-grant case.
            return self._ask_envelope(
                result.reason or "",
                ask_kind="sticky_grant_registration",
            )

        def _cc_on_freeze(fields: dict):
            # Both session_freeze_pretool and orchestrator_check's
            # needs_confirmation path produce FREEZE marker fields:
            # {reason, blocked_by, freeze_state}. CC's envelope shape
            # mirrors that exactly.
            out: dict[str, object] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": fields.get("reason", ""),
                "blocked_by": fields.get("blocked_by", "session_frozen"),
            }
            if fields.get("freeze_state"):
                out["freeze_state"] = fields["freeze_state"]
            return {"hookSpecificOutput": out}

        # Note: no on_context_block hook — evaluate_tool's
        # conductor_comms gate already produces ">>> CONDUCTOR MESSAGE:"
        # blocks in the right order, and x-ray goggles append the
        # 🧠 lines after. The result.additional_context_blocks tuple
        # is already in the order CC wants (comms first, x-ray after).

        cc_hooks = GateHooks(
            on_allow=_cc_on_allow,
            on_deny=_cc_on_deny,
            on_ask=_cc_on_ask,
            on_freeze=_cc_on_freeze,
        )

        # ── Canonical pipeline (one call, all sub-gates composed) ──
        result = gate.evaluate_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            host_session_id=cli_session_id or "",
            project_root=project_root,
            payload=payload,
            lane_id=_lane_id_for_audit,
            is_aidocs_project=_is_aidocs_project(project_root),
            hooks=cc_hooks,
        )

        # ── ShellPolicy shadow (Batch 1.5, observe-only) ──
        # Side-effect-free: consumes the ALREADY-computed live verdict via
        # a replay delegate; never re-runs the cascade, never blocks,
        # never enables native execution. Default OFF. Best-effort.
        try:
            from .shell_policy_shadow import run_pretool_shadow

            run_pretool_shadow(
                project_root=project_root,
                host="claude_code",
                tool_name=tool_name,
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                host_session_id=cli_session_id or "",
                live_verdict=str(result.verdict or ""),
                live_reason=str(result.reason or ""),
                live_why=tuple(result.why or ()),
            )
        except Exception:
            pass

        # ── Render verdict into CC envelope ──
        # 1. Terminal with host_envelope set → return the host envelope
        if result.host_envelope is not None:
            return result.host_envelope

        # 2. Allow (kill-switch bypass that returned None from on_allow)
        if result.verdict == "allow":
            return None

        # 3. Deny / ask without host_envelope (defensive — should not
        # happen with cc_hooks wired, but cover the case).
        if result.verdict in ("deny", "ask"):
            return self._deny_envelope(
                result.reason or "",
                str(result.why[1]) if len(result.why or ()) >= 2 else "",
            )

        # 4. Continue: render additional_context_blocks (conductor
        # messages + x-ray goggles) into additionalContext.
        if result.additional_context_blocks:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(result.additional_context_blocks),
                },
            }

        return None

    @staticmethod
    def _join_response_text(resp: object) -> str:
        """Flatten a tool_response (str / dict / list) into scan text."""
        if isinstance(resp, str):
            return resp
        parts: list[str] = []

        def _walk(node: object) -> None:
            if isinstance(node, str):
                parts.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(resp)
        return "\n".join(parts)

    def _maybe_redact_read_output(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Return a Claude Code PostToolUse envelope with a shape-preserving
        redacted ``updatedToolOutput`` when a model-visible tool result carries
        a secret. None when not redaction-eligible, no finding, or the host
        can't pre-context redact.

        THIN Claude adapter over ``hook_pipeline``: the host-agnostic core owns
        the redactable-tool check, the capability gate, and the generic secret
        scan; this method passes its OWN host_kind ("claude_code") and only
        renders the Claude ``updatedToolOutput`` envelope + audit. A codex_hook
        adapter would call the same core with "codex" and render feedback.
        """
        from . import hook_pipeline as _hp

        tool_name = _hp.normalize_tool_name(payload.get("tool_name"))
        if not _hp.is_redactable_tool(tool_name):
            return None
        tool_response = payload.get("tool_response")
        if tool_response is None:
            return None
        text_view = self._join_response_text(tool_response)
        if not text_view:
            return None

        # Capability gate via the core. claude_hook is the CLAUDE adapter, so it
        # passes its OWN host_kind; the core owns the host-capability decision
        # (a codex_hook would pass "codex" and the core would refuse, since
        # Codex can't shape-preserving-redact).
        if not _hp.host_can_redact_output("claude_code"):
            return None

        if tool_name == "read":
            tool_input = payload.get("tool_input") or {}
            path = ""
            if isinstance(tool_input, dict):
                path = str(
                    tool_input.get("file_path")
                    or tool_input.get("filePath")
                    or tool_input.get("path")
                    or "",
                ).strip()
            try:
                from .lifecycle_service import LifecycleService

                lc = LifecycleService(self.runtime).on_host_read_output(
                    tool_name="Read",
                    path=path,
                    result_text=text_view,
                    host_session_id=str(payload.get("session_id") or ""),
                    host_kind="claude_code",
                    project_root=project_root,
                    result_obj=tool_response,
                )
            except Exception:
                return None
            if lc.redacted_response is None:
                return None
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": lc.redacted_response,
                },
            }

        # bash / monitor: delegate the host-agnostic decision (gate + scan) to
        # the core; the adapter renders Claude's updatedToolOutput envelope.
        decision = _hp.decide_generic_output_redaction(
            "claude_code",
            tool_name,
            tool_response,
        )
        if decision is None:
            return None
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="host_output_redacted",
                source_kind="post_tool_use",
                session_id=str(payload.get("session_id") or ""),
                capability_name=tool_name,
                action_kind="redacted",
                status="applied",
                payload={
                    "tool_name": tool_name,
                    "redaction_count": decision.count,
                    "categories": decision.categories,
                    "host_kind": "claude_code",
                    "mechanism": decision.mechanism or "posttooluse.updatedToolOutput",
                },
            )
        except Exception:
            pass
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": decision.redacted,
            },
        }


    def _handle_post_tool_use(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """PostToolUse: universal audit + TodoWrite→task_* auto-bridge.

        Every tool that made it past PreToolUse records an audit event
        here (native_tool_use). This closes the gap where host Read /
        Edit / Bash / etc. were invisible to execution_events — only
        AIDOCS-MCP tool calls were being logged. Without this hook,
        reads of PDFs, non-indexed files, external data sources, etc.
        had no trail at all. Stamp one row per PostToolUse.

        Universal-audit guarantees:
          * Runs for EVERY tool (AIDOCS MCP + host + playwright + any
            others) except the bootstrap window — we only record when
            managed mode is active and a session is bound, so unmanaged
            projects don't accumulate noise.
          * Best-effort: a failed audit must NEVER block the tool call
            from completing (swallows all exceptions).
          * Fields captured: tool_name, tool_use_id, action_kind
            (edit/read/run/other), target_entity (path/command when
            present), status (hardcoded "completed" — presence here
            means the tool ran; failure events are the domain of
            PreToolUse deny envelopes).

        After the universal audit, the TodoWrite→task lifecycle bridge
        runs (legacy behavior preserved).
        """
        tool_name_raw = str(payload.get("tool_name") or "").strip()

        # Universal post-tool audit — delegated to LifecycleService
        # (host-agnostic). Same audit shape every host writes; status
        # detection (completed/failed) lives in the service.
        try:
            _lane_id_post = self._get_current_lane_id(project_root)
        except Exception:
            _lane_id_post = None
        from .lifecycle_service import LifecycleService

        LifecycleService(self.runtime).on_post_tool_use_audit(
            tool_name=tool_name_raw,
            tool_input=payload.get("tool_input") or {},
            tool_response=payload.get("tool_response"),
            host_session_id=str(payload.get("session_id") or ""),
            project_root=project_root,
            payload=payload,
            lane_id=_lane_id_post,
            # Dedup (UPS/PostToolUse sqlite seal, 2026-06-02): this Claude path
            # runs its OWN surface_on_edit below (line ~2167) and returns that
            # envelope; the audit call's downstream goggles were computed and
            # DISCARDED here (return value unused). Skip the duplicate goggles —
            # the audit event itself still fires. Other hosts (CLI / openai
            # adapter) keep surface_downstream=True since they consume it.
            surface_downstream=False,
        )

        # Batch 2.0-B0.1: native pilot completion receipt + output proof.
        # For host-native shell outputs the receipt/output-guard is NOT
        # best-effort: if it raises (or its import fails), raw native output
        # must NOT fall through — fail CLOSED by withholding the output.
        # Non-native tools are unaffected (skip the block entirely).
        _is_native_shell_post = False
        try:
            from .shell_envelope import (
                TRANSPORT_HOST_NATIVE,
                detect_provider_and_transport,
            )

            _, _post_transport = detect_provider_and_transport(tool_name_raw)
            _is_native_shell_post = _post_transport == TRANSPORT_HOST_NATIVE
        except Exception:
            # Detection threw for a possibly-native tool — literal fallback.
            _bn = (tool_name_raw or "").strip().lower()
            for _pre in ("mcp__aidocs__", "mcp__"):
                _bn = _bn.removeprefix(_pre)
            _bn = _bn.removesuffix(".exe")
            _is_native_shell_post = _bn in (
                "bash",
                "sh",
                "zsh",
                "wsl",
                "powershell",
                "pwsh",
                "cmd",
            )

        if _is_native_shell_post:
            try:
                from .shell_receipt import native_post_receipt

                _receipt = native_post_receipt(
                    project_root,
                    self.runtime,
                    host="claude_code",
                    tool_name=tool_name_raw,
                    tool_input=payload.get("tool_input") or {},
                    tool_response=payload.get("tool_response"),
                    host_session_id=str(payload.get("session_id") or ""),
                    tool_use_id=str(payload.get("tool_use_id") or ""),
                )
            except Exception:
                # Receipt/guard crashed for a native shell output → fail
                # closed: withhold the raw output, record the failure.
                try:
                    self.runtime.hub.execution.record_event(
                        project_root,
                        event_kind="shell_native_receipt_failed",
                        source_kind="post_tool_use",
                        session_id=str(payload.get("session_id") or ""),
                        capability_name=tool_name_raw,
                        action_kind="receipt",
                        status="degraded",
                        payload={"tool_name": tool_name_raw, "host": "claude_code"},
                    )
                except Exception:
                    pass
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedToolOutput": (
                            "[AIDOCS: native receipt/output guard failed; output withheld]"
                        ),
                    },
                }
            if _receipt is not None:
                return _receipt

        # Host Read output secret-redaction (one-law goal 2026-05-20).
        # Claude Code's PostToolUse supports hookSpecificOutput.
        # updatedToolOutput, which REPLACES the tool result before it
        # enters model context. A SAFE read path whose bytes happen to
        # contain a credential is the case PreToolUse path-blocking can't
        # catch — so scan the Read response and, if a secret is found,
        # return a SHAPE-PRESERVING redacted updatedToolOutput. The secret
        # never appears in additionalContext (only the redacted output is
        # returned). Capability-gated: only fires because claude_code is
        # registered can_redact_tool_output_before_context=True.
        _read_redacted = self._maybe_redact_read_output(project_root, payload)
        if _read_redacted is not None:
            return _read_redacted

        # Post-edit downstream goggles — delegated to ReadMemorySurfacer.
        # Returns hookSpecificOutput envelope ONLY when there are hints
        # to surface; otherwise falls through to the TodoWrite branch
        # (and ultimately None) preserving existing behavior.
        from .read_memory_surfacer import ReadMemorySurfacer

        tool_input_post = payload.get("tool_input") or {}
        _downstream = ReadMemorySurfacer(self.runtime).surface_on_edit(
            tool_name=tool_name_raw,
            tool_input=tool_input_post,
            project_root=project_root,
        )
        if _downstream.hint_count > 0:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n".join(_downstream.advisory_lines),
                },
            }

        tool_name = tool_name_raw.lower()
        if tool_name not in ("todowrite", "todoread"):
            return None
        if tool_name == "todoread":
            return None

        # TodoWrite bridge → task lifecycle dispatch, delegated to
        # LifecycleService.dispatch_todo_lifecycle.
        try:
            tool_input = payload.get("tool_input")
            if not isinstance(tool_input, dict):
                return None
            todos = tool_input.get("todos")
            if not isinstance(todos, list):
                return None
        except Exception:
            return None
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            sid = str(managed.get("session_id") or "").strip() if managed.get("active") else ""
        except Exception:
            return None
        LifecycleService(self.runtime).dispatch_todo_lifecycle(
            todos=todos,
            managed_session_id=sid,
            project_root=project_root,
        )
        return None

    # _dispatch_task_lifecycle removed — logic moved to
    # LifecycleService.dispatch_todo_lifecycle.

    # Override phrases that lift the agent-brief research-block for the
    # current operator turn. Closed list — not heuristic. Operator has
    # to type the phrase exactly to opt into delegated research.
    _AGENT_RESEARCH_OVERRIDE_PHRASES: tuple[str, ...] = (
        "delegate research",
        "let agents research",
        "let the agent research",
        "agent can research",
        "agents may research",
        "ok to research",
    )

    # Tools that may run while requires_reconnect=1. Everything else
    # must wait for session_connect to clear the flag. Keeps the
    # allowlist tight — any read-only tool added here must make
    # sense BEFORE a session has re-bound.
    def _classify_tool_action(self, tool_name: str) -> str:
        """Thin delegate to ToolGate.classify_tool_action — kept as
        a method for back-compat with any test that targets it.
        """
        from .tool_gate_service import classify_tool_action

        return classify_tool_action(tool_name)

    def _build_audit_payload(
        self,
        project_root: Path,
        tool_name: str,
        tool_input: object,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Thin delegate to ToolGate.build_audit_payload — kept as
        a method for back-compat. Resolves lane_id from claude_hook's
        per-conductor state before passing through.
        """
        from .tool_gate_service import build_audit_payload

        try:
            lane_id = self._get_current_lane_id(project_root)
        except Exception:
            lane_id = None
        return build_audit_payload(
            tool_name=tool_name,
            tool_input=tool_input,
            payload=payload,
            lane_id=lane_id,
        )

    _RECONNECT_ALLOWED_TOOLS: frozenset[str] = frozenset(
        {
            # Bind / bootstrap — the tools that clear the flag.
            "ai_session",
            "session_connect",
            # session_start: compat alias for CC's hardcoded probe
            # (restored 2026-05-03 as auto-activator).
            "session_start",
            "session_list",
            "aidocs_orchestrate",
            "project_bootstrap_or_resume",
            "project_status",
            "project_check",
            # Admin escape hatch (2026-04-22) — named tool that clears
            # both reconnect flags in one idempotent call. Exists so
            # operators never have to do manual sqlite surgery to
            # escape a future AIDOCS-internal deadlock. FastMCP
            # registers the function as `admin_clear_reconnect` (strips
            # leading `aidocs_`); allowlist uses that bare name.
            "admin_clear_reconnect",
            # Schema discovery — agent must be able to LOAD the schema for
            # session_connect before calling it. Without ToolSearch
            # here, we recreate a catch-22 (user reported 2026-04-21).
            "ToolSearch",
            # Minimum discovery surface so the agent can figure out WHAT
            # to reconnect to without being fully blind. These are
            # read-only and don't depend on sqlite continuity.
            "ai_find",
            "ai_investigate",
            "ai_get_lines",
            "ai_bundle",
            # Heartbeat + task lifecycle.
            "ScheduleWakeup",
            "task_complete",
        },
    )

    def _check_reconnect_required(
        self,
        project_root: Path,
        tool_name: str,
        *,
        cli_session_id: str = "",
    ) -> dict[str, object] | None:
        """Fresh-CLI reconnect gate (2026-04-21).

        When Claude Code's per-process session_id changed mid-session
        (window reopen), requires_reconnect=1 is sticky until the agent
        calls session_connect or an equivalent session-bind tool.
        While the flag is raised, every tool outside
        _RECONNECT_ALLOWED_TOOLS is hard-refused. Keeps agents from
        acting on inherited sqlite state (known paths, lane binding)
        with an empty in-memory context.

        cli_session_id (#58, canonical 2026-04-26): when provided,
        managed_mode resolution is per-conductor — the deny envelope
        returns the calling conductor's bound session, not whichever
        session another conductor most recently set on the singleton.
        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=cli_session_id,
            )
        except Exception:
            return None
        if not managed.get("active"):
            return None
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return None
        # Single reconnect trigger: query_gate.requires_reconnect.
        # Set by UserPromptSubmit's check_and_update_cli_session_id
        # when Claude Code's per-process session_id changes (fresh CLI
        # launch that inherited sqlite state with empty in-memory
        # context). The old boot-token path was removed 2026-04-23:
        # claude_hook runs as a separate subprocess per tool call so
        # its module-level token never matched the long-running MCP
        # server's stamp, causing an infinite PreToolUse lockout.
        try:
            needs = self.runtime.hub.query_gate.get_requires_reconnect(
                project_root,
                session_id,
            )
        except Exception:
            return None
        if not needs:
            return None

        # Normalize tool name (Claude Code prefixes mcp__aidocs__ tools).
        bare = tool_name.strip()
        for prefix in ("mcp__aidocs__", "mcp__"):
            if bare.startswith(prefix):
                bare = bare[len(prefix) :]
                break
        # Tool names Claude Code registers for host tools keep case
        # ("Read", "Task", "ScheduleWakeup") — match case-insensitively
        # against the allowlist to cover both.
        if bare in self._RECONNECT_ALLOWED_TOOLS or bare.lower() in {
            t.lower() for t in self._RECONNECT_ALLOWED_TOOLS
        }:
            # Clear the flag when session_connect runs — that's
            # the contract the agent is re-binding via.
            # (session_start MCP tool removed 2026-04-30; only
            # session_connect remains as the bind path.)
            if bare.lower() in {"ai_session", "session_connect"}:
                try:
                    self.runtime.hub.query_gate.clear_requires_reconnect(
                        project_root,
                        session_id,
                    )
                except Exception:
                    pass
            return None

        # Server resolves host_session_id from the gate row stamped
        # by the UPS hook; the agent only passes the human-readable
        # session name. (Fixed 2026-05-13 — was demanding an arg the
        # tool schema doesn't accept, deadlocking fresh CLIs.)
        return self._deny_envelope(
            (
                "Fresh CLI — call `mcp__aidocs__ai_session(mode='connect', "
                f'session_id="{session_id}")` or `/aidocs`. '
                "Known-path reads wiped; re-discover via ai_find / "
                "ai_investigate."
            ),
            blocked_by="requires_reconnect",
        )

    def _check_session_freeze(
        self,
        project_root: Path,
        tool_name: str,
        tool_input: dict[str, object],
    ) -> dict[str, object] | None:
        """Session-freeze pre-tool guard (#39, 2026-04-25).

        When a confirmable destructive verdict landed on a previous
        tool call, the session is frozen until the next UPS resolves
        the freeze (self_approve) or the admin decides
        (admin_escalation, Phase B). While frozen, every tool returns
        the same deny envelope with the fingerprint phrase the
        operator must type.

        Single-row-per-session contract. Failure on store read =
        return None (let other gates run); never inject a stale
        freeze if the row can't be read.
        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
        except Exception:
            return None
        if not managed.get("active"):
            return None
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return None
        from .freeze_service import (
            build_existing_freeze_response,
            get_existing_freeze,
        )

        freeze = get_existing_freeze(project_root, session_id)
        if freeze is None:
            return None
        env = build_existing_freeze_response(freeze, project_root)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": env["permissionDecisionReason"],
                "blocked_by": env.get("blocked_by", "session_frozen"),
                "freeze_state": env["freeze_state"],
            },
        }

    def _resolve_session_freeze(
        self,
        project_root: Path,
        prompt: str,
        *,
        cli_session_id: str = "",
    ) -> None:
        """Thin delegate to PromptMutator.resolve_session_freeze
        (host-agnostic). Kept as a method on the hook for back-compat
        with any existing test that targets it directly; new code
        should use the service entry point.
        """
        from .prompt_mutator import PromptMutator

        PromptMutator(self.runtime).resolve_session_freeze(
            prompt=prompt,
            host_session_id=cli_session_id,
            project_root=project_root,
        )

    def _deny_envelope(self, reason: str, blocked_by: str = "") -> dict[str, object]:
        """Build a PreToolUse deny envelope carrying the canonical tier.

        `blocked_by` surfaces the denial taxonomy tier in the hook output
        so audit tooling, dashboard filters, and regression tests can
        assert on a stable vocabulary instead of parsing reason prose.
        Unknown keys in hookSpecificOutput are ignored by Claude Code.
        """
        out: dict[str, object] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
        if blocked_by:
            out["blocked_by"] = blocked_by
        return {"hookSpecificOutput": out}

    def _ask_envelope(
        self,
        reason: str,
        *,
        ask_kind: str = "",
    ) -> dict[str, object]:
        """Return a PreToolUse envelope that surfaces a native CC
        permission-ask popup. Operator picks allow/deny; CC rerouts
        the decision back through the normal permission pipeline.

        DEPRECATED 2026-04-25: superseded by _freeze_envelope (#39).
        Kept during Phase A migration so existing call sites don't
        break. Phase C of #39 removes this entirely.
        """
        out: dict[str, object] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
        if ask_kind:
            out["ask_kind"] = ask_kind
        return {"hookSpecificOutput": out}

    def _freeze_envelope(
        self,
        project_root: Path,
        session_id: str,
        *,
        tool_name: str,
        tool_input: object,
        judge_summary: str,
        admin_tier: bool = False,
    ) -> dict[str, object]:
        """Create a freeze + escalation request, return a CC PreToolUse
        deny envelope wrapping the shared freeze response.

        Single-turn for self_approve (admin_tier=False) — operator's
        next UPS resolves it exactly once. admin_tier=True reserved
        for Phase B; currently always False.

        Delegates the actual freeze creation to freeze_service so
        ai_run uses the same primitive and one freeze contract serves
        every host.
        """
        from .freeze_service import build_freeze_response

        env = build_freeze_response(
            project_root,
            session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            judge_summary=judge_summary,
            admin_tier=admin_tier,
        )
        out: dict[str, object] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": env["permissionDecisionReason"],
            "blocked_by": env.get("blocked_by", "judge_confirm_required"),
        }
        if "freeze_state" in env:
            out["freeze_state"] = env["freeze_state"]
        return {"hookSpecificOutput": out}

    def _check_agent_dispatch_brief(
        self,
        project_root: Path,
        tool_input: dict[str, object],
    ) -> ToolDecision | None:
        """Refuse Task/Agent dispatches whose brief is research-shaped.

        Returns a ToolDecision with allowed=False and
        blocked_by="agent_brief" when the brief contains research /
        inspection language without an operator override. Returns None
        to let the standard cascade proceed.

        Operator-toggleable via `security.delegate_research_allowed` config
        key. Default false (block). Some operators want their conductor
        to dispatch research-style sub-agents; the toggle skips this
        gate entirely when on.
        """
        brief = str((tool_input or {}).get("prompt") or "").strip()
        if not brief:
            return None

        # Toggle: when operator explicitly allows delegated research at
        # the project level, skip the gate. Hot-reload via get_setting
        # so dashboard edits take effect without process restart.
        try:
            from .config import get_setting

            delegate_allowed = bool(
                get_setting(
                    "security.delegate_research_allowed",
                    project_root=project_root,
                    default=False,
                ),
            )
            if delegate_allowed:
                return None
        except Exception:
            # Config-read failure → fall through to default safe path
            # (block research briefs).
            pass

        # Check operator's current-turn prompt for explicit override.
        # The current-prompt isn't in tool_input — pull it from the
        # query gate's user-intent state populated by the same
        # UserPromptSubmit hook that grants raw-tool access.
        override_present = False
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = (
                str(managed.get("session_id") or "").strip() if managed.get("active") else ""
            )
            if session_id:
                from .protected_file_runtime import (
                    get_protected_edit_grants as _get_edit_grants,
                )

                # Reuse the per-turn grant store for the override flag —
                # operators with the protected-edit pattern already use
                # this mechanism, no new persistence needed.
                grants_blob = " ".join(_get_edit_grants() or [])
                lower_blob = grants_blob.lower()
                override_present = any(
                    phrase in lower_blob for phrase in self._AGENT_RESEARCH_OVERRIDE_PHRASES
                )
        except Exception:
            override_present = False

        from .agent_brief_gate import evaluate_agent_brief

        decision = evaluate_agent_brief(brief, override_present)
        if decision["allowed"]:
            return None

        from .agent_orchestrator import ToolDecision

        return ToolDecision(
            allowed=False,
            reason=str(decision["reason"]),
            blocked_by="agent_brief",
        )

    def _get_current_lane_id(self, project_root: Path) -> str | None:
        """Get the current lane ID from gate state."""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            sid = managed.get("session_id") if isinstance(managed, dict) else None
            if not sid:
                return None
            state = self.runtime.hub.query_gate.get(project_root, str(sid))
            return state.get("current_lane_id")
        except Exception:
            return None

    # Phase 2 (2026-05-27): _MCP_ALTERNATIVES / _CODE_EXTENSIONS
    # / _PROTECTED_CONFIG / _INFRASTRUCTURE_PATHS moved to
    # host_services.tool_discovery_hint + protected_path_policy.
    # All four were unused class-field constants (zero readers).

    def _record_hook_event(
        self,
        project_root: Path,
        event_name: str,
        payload: dict[str, object],
    ) -> None:
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = str(managed.get("session_id") or "").strip() or None
            tool_name = str(payload.get("tool_name") or "").strip() or None
            prompt = str(payload.get("prompt") or "").strip() or None
            event_kind = event_name.lower()
            payload_summary = {
                key: value
                for key, value in payload.items()
                if key in {"hook_event_name", "tool_name", "tool_input", "prompt", "cwd"}
            }
            # Token estimation from hook payloads
            tokens_in = 0
            tokens_out = 0
            if prompt:
                tokens_in += max(1, len(prompt.encode("utf-8")) // 4)
            tool_input = payload.get("tool_input")
            if isinstance(tool_input, dict):
                try:
                    tokens_out += max(
                        1,
                        len(json.dumps(tool_input, default=str).encode("utf-8")) // 4,
                    )
                except Exception:
                    pass
            elif isinstance(tool_input, str):
                tokens_out += max(1, len(tool_input.encode("utf-8")) // 4)
            from .tool_call_log import record as _log_record

            _log_record(
                self.runtime.hub,
                project_root,
                phase=event_kind,
                name=tool_name,
                payload={
                    **payload_summary,
                    "prompt_preview": prompt[:200] if prompt else None,
                    "tokens_in_estimate": tokens_in,
                    "tokens_out_estimate": tokens_out,
                },
                session_id=session_id,
                source="claude_hook",
                action_kind="hook_intercept",
                status="observed",
            )
        except Exception as exc:
            logger.debug("Failed to record hook event: %s", exc)
            return

    def _enforcement_disabled(
        self,
        project_root: Path,
        *,
        session_id: str | None = None,
    ) -> bool:
        """[DEV-ONLY FAILSAFE] Read the kill switch.

        Delegates to enforcement.is_kill_switch_active so this hook,
        gate_tool.enforce_tool_call, ai_run_kill, and the
        AgentOrchestrator inline check all read the same flag with
        the same flavor lock (dev only) and the same fail-closed
        posture on a config-read exception.

        Per-session scope (Phoenix, 2026-05-07): pass session_id so
        the cascade picks up a session-scoped flip. None falls back
        to project + global only (back-compat).

        Castle law: emergency key, one source of truth.
        """
        from .enforcement import is_kill_switch_active

        return is_kill_switch_active(project_root, session_id=session_id)

    def _log_enforcement_bypass(
        self,
        project_root: Path,
        event_name: str,
        payload: dict[str, object],
    ) -> None:
        """Emit one audit event per bypass.

        Delegates to enforcement.record_kill_switch_bypass so the
        audit shape stays identical across hook + gate_tool +
        ai_run_kill + orchestrator. Best-effort.
        """
        from .enforcement import record_kill_switch_bypass

        tool_name = str(payload.get("tool_name") or "").strip()
        record_kill_switch_bypass(
            project_root,
            source="claude_hook",
            target=tool_name or event_name,
            payload={"hook_event": event_name, "tool_name": tool_name},
        )

    def _resolve_cwd_root(self, payload: dict[str, object]) -> Path | None:
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        return Path(cwd).resolve()

    def _resolve_project_root(self, payload: dict[str, object]) -> Path | None:
        """Resolve project root from the hook payload's cwd.

        A project resolves when it is COMMISSIONED per
        ``project_commission`` — i.e. the install-wide registry/SQLite
        records it (authority), OR the on-disk
        ``.MEMORY/.aidocs/index.aidocs`` marker exists (back-compat
        fallback). Governance no longer hinges on the marker file alone:
        a project commissioned via the registry resolves even if the
        marker was never written / was deleted, and a legacy marker-only
        project still resolves.

        Adopted-but-uncommissioned projects (declare the aidocs MCP /
        carry an adoption record but have no infrastructure yet) do NOT
        resolve here — they are first commissioned by the UPS /
        SessionStart auto-repair in ``_dispatch_event``, after which this
        check succeeds.

        No walk-up — the hook's cwd is the user's terminal cwd, which IS
        the kingdom root. Walking up would incorrectly adopt a parent
        project from a non-project subdir; and the old loose check
        (`.MEMORY/` + AGENTS.md/CLAUDE.md) wrongly accepted subdirs like
        `mcp/`/`core/` that ship their own guidance files. (2026-05-03 /
        commission-state fix.)
        """
        cwd_root = self._resolve_cwd_root(payload)
        if cwd_root is None:
            return None
        try:
            from .project_commission import is_commissioned

            commissioned = is_commissioned(cwd_root)
        except Exception:
            # Fail back to the marker so a commission-store hiccup never
            # silently un-manages a project that has the on-disk marker.
            from .mcp_server_runtime_helpers import is_aidocs_managed

            commissioned = is_aidocs_managed(cwd_root)
        if not commissioned:
            self._log_resolution_failure(
                cwd_root,
                "project not commissioned (no registry record or "
                ".MEMORY/.aidocs/index.aidocs marker at cwd)",
            )
            return None
        return cwd_root

    def _record_classification_event(
        self,
        project_root: Path,
        action_kind: str,
        prompt: str,
    ) -> None:
        """Record the classified action_kind as an execution event for traceability."""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = str(managed.get("session_id") or "").strip() or None
            from .tool_call_log import record as _log_record

            _log_record(
                self.runtime.hub,
                project_root,
                phase="prompt_classified",
                name=None,
                payload={"prompt_preview": prompt[:200] if prompt else None},
                session_id=session_id,
                source="claude_hook",
                action_kind=action_kind,
                status="classified",
            )
        except Exception as exc:
            logger.debug("Failed to record classification event: %s", exc)

    def _log_resolution_failure(self, project_root: Path, reason: str) -> None:
        """Log project root resolution failure for debugging."""
        logger.warning("Project resolution failed for %s: %s", project_root, reason)

    @staticmethod
    def _operator_intent_note(outcome) -> str:
        """Render a one-line operator-facing acknowledgment for an
        operator-intent outcome. Never echoes the prompt or any secret —
        only the structured action/target/scope and the decision.
        """
        status = getattr(outcome, "status", "")
        target = getattr(outcome, "target", "")
        scope = getattr(outcome, "scope", "")
        action = getattr(outcome, "action", "")
        if status == "reported":
            value = getattr(outcome, "read_value", None)
            # Bash-allowlist report: distinguish session entries from
            # inherited ones, concisely.
            if target == "bash.allowlist" and isinstance(value, dict):
                session_cmds = value.get("session") or []
                inherited = value.get("inherited") or []
                head = ", ".join(session_cmds) if session_cmds else "(none)"
                note = f"📊 Bash allowlist for this session: {head}"
                if inherited:
                    shown = ", ".join(inherited[:6])
                    extra = len(inherited) - 6
                    if extra > 0:
                        shown += f", +{extra} more"
                    note += f" (plus inherited: {shown})"
                return note + "."
            provenance = getattr(outcome, "provenance", "") or "default"
            state = "ON" if value else "OFF"
            return f"📊 {target} is {state} (scope: {scope}, value from: {provenance})."
        if status == "noop":
            # Non-mutating: a remove of an entry not present at session
            # scope. Name the command so the operator knows nothing
            # changed (and why).
            if target == "bash.allowlist":
                value = getattr(outcome, "value", None) or {}
                cmd = value.get("command", "?") if isinstance(value, dict) else "?"
                return (
                    f"ℹ️ '{cmd}' was not present in the session bash allowlist. No change was made."
                )
            return (
                f"ℹ️ Operator intent '{action} {target}' ({scope}): nothing "
                f"to change. No change was made."
            )
        if status == "applied":
            # Bash-allowlist route reports the exact base command.
            if target == "bash.allowlist":
                value = getattr(outcome, "value", None) or {}
                cmd = value.get("command", "?") if isinstance(value, dict) else "?"
                if action == "remove":
                    return (
                        f"✅ Removed '{cmd}' from the session bash allowlist "
                        f"(scope: {scope}). Authenticated via host binding."
                    )
                return (
                    f"✅ Added base command '{cmd}' to the bash allowlist "
                    f"(scope: {scope}). Authenticated via host binding."
                )
            return (
                f"✅ Operator intent applied: {action} {target} "
                f"(scope: {scope}). Authenticated via host binding."
            )
        if status == "needs_exact_confirm":
            if target == "bash.allowlist":
                value = getattr(outcome, "value", None) or {}
                cmd = value.get("command", "?") if isinstance(value, dict) else "?"
                if action == "remove":
                    phrase = f"remove {cmd} from bash allowlist for this session"
                    verb = f"remove '{cmd}' from"
                else:
                    phrase = f"add {cmd} to bash allowlist for this session"
                    verb = f"add '{cmd}' to"
                return (
                    f"⚠️ To {verb} the bash allowlist ({scope}), type the "
                    f'exact phrase: "{phrase}". No change was made.'
                )
            return (
                f"⚠️ Operator intent '{action} {target}' ({scope}) is a "
                f"dangerous route — type the exact confirmation phrase to "
                f"proceed. No change was made."
            )
        if status == "needs_confirmation":
            return (
                f"❓ Operator intent '{action} {target}' read with low "
                f"confidence — rephrase to confirm. No change was made."
            )
        if status == "refused":
            reason = getattr(outcome, "reason", "")
            return f"⛔ Operator intent '{action} {target}' refused ({reason}). No change was made."
        return ""

    def _build_lightweight_prompt_context(
        self,
        action_kind: str,
        route: dict[str, object],
        project_root: Path,
        host_state: dict[str, object] | None = None,
        prompt: str = "",
        cli_session_id: str = "",
    ) -> str:
        """Build context from classification + route only (no orchestration data).

        Single path: enforced mode. Advisory mode was retired
        2026-04-28 — every supported host (Claude Code, Codex,
        OpenCode CLI/Desktop) now has PreToolUse + UserPromptSubmit
        hooks, so the gates do the enforcement and the prompt-side
        injection stays minimal.
        """
        prompt_payload = host_state if isinstance(host_state, dict) else {}
        session_state = (
            prompt_payload.get("session_state")
            if isinstance(prompt_payload.get("session_state"), dict)
            else {}
        )
        session_id = str(session_state.get("session_id") or route.get("session_id") or "").strip()
        return self._build_enforced_context(
            action_kind,
            session_id,
            route,
            prompt_payload,
            prompt,
            project_root,
            cli_session_id=cli_session_id,
        )

    def _build_enforced_context(
        self,
        action_kind: str,
        session_id: str,
        route: dict[str, object],
        prompt_payload: dict[str, object],
        prompt: str = "",
        project_root: Path | None = None,
        *,
        cli_session_id: str = "",
    ) -> str:
        """Minimal context for hosts with PreToolUse gate enforcement."""
        # The static "AIDOCS-managed mode active" preamble that used to
        # fire here was vestigial: UserPromptSubmit IS the auto-bootstrap
        # that activates managed mode, so by the time this builder runs
        # the agent is already inside AIDOCS. Telling it again every
        # prompt was ~50 tok of pure repetition. Removed 2026-04-28.
        # Dynamic action + session label still fire — they vary per
        # prompt and carry actual signal.
        parts = [
            f"AIDOCS managed. Action: `{action_kind}`.",
        ]
        if session_id:
            parts.append(f"Session: `{session_id}`.")

        for line in self._build_tool_discovery_hint(
            prompt,
            project_root=project_root,
            action_kind=action_kind,
            cli_session_id=cli_session_id,
        ):
            parts.append(line)

        # Domain hints only — these are useful even with gates
        classification = (
            prompt_payload.get("prompt_state")
            if isinstance(prompt_payload.get("prompt_state"), dict)
            else {}
        )
        domain_hints = classification.get("domain_hints")
        if isinstance(domain_hints, list) and domain_hints:
            hint_parts = []
            for hint in domain_hints:
                if isinstance(hint, dict) and hint.get("recommended_tools"):
                    hint_parts.append(f"{hint['domain']}: {hint['recommended_tools']}")
            if hint_parts:
                parts.append(f"Domain tools: {'; '.join(hint_parts)}.")
        # Triggered skill guidance — inject full content when a skill matches the prompt
        skill_state = (
            prompt_payload.get("skill_state")
            if isinstance(prompt_payload.get("skill_state"), dict)
            else {}
        )
        prompt_activation = (
            skill_state.get("prompt_activation")
            if isinstance(skill_state.get("prompt_activation"), dict)
            else {}
        )
        session_snapshot = (
            skill_state.get("session_snapshot")
            if isinstance(skill_state.get("session_snapshot"), dict)
            else {}
        )
        helper_skill_guidance = (
            prompt_activation.get("helper_skill_guidance")
            if isinstance(prompt_activation.get("helper_skill_guidance"), list)
            else []
        )
        if not helper_skill_guidance:
            helper_skill_guidance = (
                session_snapshot.get("helper_skill_guidance")
                if isinstance(session_snapshot.get("helper_skill_guidance"), list)
                else []
            )
        # Once-per-epoch dedup (mirrors DNT banner contract). Skills
        # already shown this epoch are silently dropped — host context
        # retains them until compaction rotates the epoch via
        # bump_compaction_count, at which point the next prompt
        # re-emits.
        if project_root is not None:
            from .helper_skill_injector import maybe_helper_skill_blocks

            for block in maybe_helper_skill_blocks(
                project_root,
                helper_skill_guidance,
                host_kind="claude_code",
                host_session_id=cli_session_id,
            ):
                parts.append(block)

        # Active skill names
        active_skills = (
            prompt_activation.get("active_skills")
            if isinstance(prompt_activation.get("active_skills"), list)
            else (
                session_snapshot.get("active_skills")
                if isinstance(session_snapshot.get("active_skills"), list)
                else []
            )
        )
        if active_skills:
            parts.append(f"Active skills: {', '.join(f'`{s}`' for s in active_skills if s)}.")

        # NLP skill suggestion (king doctrine 2026-05-12). Infer
        # additional skills from prompt content via aidocs_nlp.
        # Augments the literal-intent path above with free-form
        # prompt matching.
        try:
            if project_root is not None and prompt:
                suggested = self._infer_skill_suggestions(
                    prompt,
                    project_root,
                    already_active=set(active_skills or []),
                )
                if suggested:
                    parts.append(f"Suggested skills: {', '.join(f'`{s}`' for s in suggested)}.")
        except Exception:
            pass

        # Lifecycle nudge — still useful as a reminder
        lifecycle_state = (
            prompt_payload.get("lifecycle_state")
            if isinstance(prompt_payload.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)

        return " ".join(parts)

    def _build_prompt_context(self, result: dict[str, object]) -> str:
        classification = (
            result.get("classification") if isinstance(result.get("classification"), dict) else {}
        )
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        orchestration = (
            result.get("orchestration") if isinstance(result.get("orchestration"), dict) else {}
        )

        action_kind = str(classification.get("action_kind") or "understand")
        mode = str(result.get("mode") or "")
        session_id = str(
            route.get("session_id") or orchestration.get("selected_session_id") or "",
        ).strip()
        recommended = (
            route.get("recommended_mcp_flow")
            if isinstance(route.get("recommended_mcp_flow"), list)
            else []
        )
        recommended_text = ", ".join(str(item) for item in recommended if str(item).strip())
        retrieval = (
            orchestration.get("retrieval")
            if isinstance(orchestration.get("retrieval"), dict)
            else {}
        )
        retrieval_mode = str(retrieval.get("mode") or "")
        # Prefer workflow from orchestration result (avoids re-reading)
        workflow = (
            orchestration.get("workflow") if isinstance(orchestration.get("workflow"), dict) else {}
        )
        if not workflow:
            # Fallback: try bootstrap sync path
            bootstrap = (
                orchestration.get("bootstrap")
                if isinstance(orchestration.get("bootstrap"), dict)
                else {}
            )
            sync = bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
            workflow = sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}

        parts = [
            "AIDOCS-managed mode is active for this project.",
            f"AIDOCS suggests action kind: `{action_kind}` (advisory — use your judgment if the classification seems wrong).",
        ]
        if session_id:
            parts.append(f"Bound session: `{session_id}`.")
            parts.append(
                "Stay in the bound AIDOCS session and continue its current conductor/plan flow; do not switch to generic worktree or standalone execution setup.",
            )
        if mode == "mcp_orchestrated":
            parts.append(
                "Route this turn through the AIDOCS MCP flow before broad repo inspection.",
            )
        elif mode == "direct_inspection_allowed":
            parts.append(
                "Inspect the explicit target first, then return to MCP-first flow for broader work.",
            )
        if retrieval_mode:
            parts.append(f"Current retrieval mode: `{retrieval_mode}`.")
        if recommended_text:
            parts.append(f"Recommended MCP flow: {recommended_text}.")

        action_directive = self._action_directive(action_kind)
        if action_directive:
            parts.append(action_directive)

        # Domain-specific tool recommendations from __domain_hint_* tokens
        domain_hints = classification.get("domain_hints")
        if isinstance(domain_hints, list) and domain_hints:
            hint_parts = []
            for hint in domain_hints:
                if isinstance(hint, dict) and hint.get("recommended_tools"):
                    hint_parts.append(f"{hint['domain']}: {hint['recommended_tools']}")
            if hint_parts:
                parts.append(f"Domain-specific tools: {'; '.join(hint_parts)}.")

        workflow_summary = self._build_compiled_workflow_summary(workflow)
        if workflow_summary:
            parts.append(f"Compiled workflow actions: {workflow_summary}.")
        host_state = result.get("host_state") if isinstance(result.get("host_state"), dict) else {}
        lifecycle_state = (
            host_state.get("lifecycle_state")
            if isinstance(host_state.get("lifecycle_state"), dict)
            else {}
        )
        lifecycle_nudge = self._build_lifecycle_followthrough_nudge(lifecycle_state)
        if lifecycle_nudge:
            parts.append(lifecycle_nudge)
        parts.append(
            "Avoid ad-hoc broad repo scanning when the MCP routing result already provides the path forward.",
        )
        return " ".join(parts)

    def _build_tool_discovery_hint(
        self,
        prompt: str,
        project_root: Path | None = None,
        action_kind: str | None = None,
        cli_session_id: str = "",
    ) -> list[str]:
        """Surface AIDOCS tools AND project memory relevant to the prompt.

        Thin adapter — delegates the policy decision to the
        host-agnostic ReadMemorySurfacer. claude_hook adds only the
        runtime-state lookups (which tools were used + sticky-surfaced)
        because they're cheap and identical across hosts; the surfacer
        consumes them as inputs and returns the SurfacingResult.
        """
        if not prompt or not prompt.strip():
            return []
        from .read_memory_surfacer import ReadMemorySurfacer

        surfacer = ReadMemorySurfacer(self.runtime)
        used = self._tools_used_in_session(project_root) if project_root else set()
        already_surfaced = surfacer.sticky_surfaced_tools(project_root) if project_root else set()
        result = surfacer.surface_on_prompt(
            prompt=prompt,
            project_root=project_root,
            action_kind=action_kind,
            already_used_tools=used,
            already_surfaced_tools=already_surfaced,
            host_kind="claude_code",
            host_session_id=cli_session_id or "",
        )
        return list(result.advisory_lines)

    def _tools_used_in_session(self, project_root: Path) -> set[str]:
        """Tool names already used in the current managed session.

        Reads native_tool_use rows from execution_events. Unmanaged
        sessions return empty — no filtering happens. Exceptions
        suppressed so hint generation never fails on a store hiccup.
        """
        try:
            managed = self.runtime.hub.managed_mode.get_mode(project_root)
            if not managed.get("active"):
                return set()
            session_id = str(managed.get("session_id") or "")
            if not session_id:
                return set()
            events = (
                self.runtime.hub.execution.list_events(
                    project_root,
                    session_id=session_id,
                    limit=200,
                )
                or []
            )
            used: set[str] = set()
            for ev in events:
                if ev.get("event_kind") != "native_tool_use":
                    continue
                name = str(ev.get("capability_name") or "").strip()
                if name:
                    used.add(name)
            return used
        except Exception:
            return set()

    def _infer_skill_suggestions(
        self,
        prompt: str,
        project_root: Path,
        *,
        already_active: set[str],
    ) -> list[str]:
        """Return skill names the prompt suggests activating, beyond
        what's already in the active_skills list.

        NLP-FREE, best-effort: uses literal word-overlap against the configured
        skill triggers (no NLPService / spaCy), so UserPromptSubmit no longer
        pays a cold model load just to surface advisory skill suggestions. The
        literal trigger contract is preserved; inflected-only matches are not
        inferred (advisory feature, acceptable). Returns [] on any error or when
        no skills are configured.
        """
        # Skill triggers live in the empire intent-tokens store
        # (kind='skill_trigger'). load_skill_trigger_tokens returns
        # {skill: {intent: [...], workflow: [...]}}.
        from .aidocs_nlp.consumers.skill_trigger import (
            detect_skill_triggers_literal,
            load_skill_trigger_tokens,
        )

        try:
            triggers = load_skill_trigger_tokens()
            if not triggers:
                return []
            hits = detect_skill_triggers_literal(prompt, triggers, top_n=5)
        except Exception:
            return []
        # Filter out already-active and return ordered names.
        out: list[str] = []
        for hit in hits:
            if hit.skill_name in already_active:
                continue
            out.append(hit.skill_name)
        return out

    def _build_lifecycle_followthrough_nudge(self, lifecycle_state: dict[str, object]) -> str:
        """Thin delegate to LifecycleService.build_followthrough_nudge
        (host-agnostic pure function).
        """
        from .lifecycle_service import LifecycleService

        return LifecycleService.build_followthrough_nudge(lifecycle_state)

    _TOOL_FIRST_PREAMBLE = (
        "AIDOCS indexed tools for code. Raw Read/Grep/Glob blocked. Widen query if empty."
    )

    _ACTION_DIRECTIVES: dict[str, str] = {
        "write_memory": (
            "Use `memory_capture` with `target_hint` (workflow/coding-standards/security/project-state/user-profile). "
            "Do NOT write memory files manually."
        ),
        "task_begin": "Use `ai_task(mode='begin')` to register the task before starting work.",
        "task_complete": "Use `ai_task(mode='complete')` to finalize the task.",
        "task_update": "Use `ai_task(mode='update')` to record progress on the current task.",
        "trace": (
            'Function/method callers: `ai_find(query, mode="references")` or `ai_trace(query, mode="references")` (delegates to ai_find). '
            'Data/field lineage: `ai_trace(query, mode="field_flow")` — for DB and struct fields only; it will not return callers of a function. '
            'CSS rules: `ai_trace(query, mode="css_class")`. API↔UI: `ai_trace(query, mode="api_to_ui")`. '
            'DB trace: `schema_query(query, mode="trace_path")`.'
        ),
        "understand": (
            '`ai_bundle(path, mode="file")` (structure) → `ai_find(query, mode="symbols")` (find symbol) → '
            "`ai_get_symbol_snippet` (read it). "
            "Precision: `ai_get_symbol_info(kind='signature')`, `ai_get_symbol_info(kind='constructor')`, `ai_get_symbol_info(kind='enum')`, `ai_get_symbol_info(kind='api')`. "
            'Broad: `ai_bundle(concept, mode="subsystem")`. DB: `schema_query(name, mode="entity")`.'
        ),
        "ai_bundle": (
            '`ai_bundle(path, mode="context", session_id=...)` (session-guided) or '
            '`ai_bundle(path, mode="file")` (single file).'
        ),
        "edit": (
            "Flow: `ai_task(mode='begin')` → read with `ai_get_lines` or `ai_get_symbol_snippet` → write with ONE of: "
            "`ai_replace(mode='string')` (small exact-match edit), `ai_replace(mode='lines')` (line-range rewrite), or `ai_batch_edit` (multiple edits atomic, up to 20) → `ai_task(mode='complete')`. "
            "Do not use raw Edit or apply_patch for managed files. Do not chain two writers against overlapping regions in the same turn. "
            "Signature shortcuts before editing: `ai_get_symbol_info(kind='signature')`, `ai_get_symbol_info(kind='constructor')`, `ai_get_symbol_info(kind='enum')`. "
            'CSS: `ai_trace(class, mode="css_class")`. DB: `schema_query(entity, mode="entity")`.'
        ),
        "test_heavy": (
            "If test/support code matters, re-run retrieval with test-inclusive indexing where the tool supports it. "
            "Then prefer: `ai_get_symbol_info(kind='api')` → `ai_get_symbol_info(kind='signature')s` → `ai_get_symbol_info(kind='constructors')` → `ai_get_symbol_info(kind='enum')` → `ai_get_symbol_info(kind='properties')`. "
            "Do not guess property names, constructor params, enum members, or service surfaces when the precision chain can confirm them first."
        ),
        "inspect": (
            '`ai_bundle(path, mode="file")` → `ai_get_dependencies` / '
            '`ai_find(query, mode="references")` → `ai_get_modules` (project boundaries). Read only after narrowing.'
        ),
        "read_error": (
            '`ai_find(symbol, mode="symbols")` (find it) → `ai_find(symbol, mode="references")` (trace) → '
            '`ai_get_symbol_snippet` (read method). DB: add `schema_query(entity, mode="entity")`.'
        ),
        "investigate": (
            'Pick by target: known symbol name → `ai_find(name, mode="symbols")`; '
            "concept/type/class search → `ai_investigate(concept, depth=..., focus=...)` (symbol-ranked, favors types/classes/structs); "
            'architecture of a known file/module → `ai_bundle(path, mode="file"|"subsystem")`. '
            "These are alternatives, not a chain. Narrow hits with "
            '`ai_find(concept, mode="mutations"|"validation"|"policy"|"references")`.'
        ),
    }

    def _action_directive(self, action_kind: str) -> str:
        directive = render_interaction_text(f"interaction.action_directives.{action_kind}")
        if not directive:
            directive = self._ACTION_DIRECTIVES.get(action_kind, "")
        if directive and action_kind not in (
            "write_memory",
            "task_begin",
            "task_complete",
            "task_update",
        ):
            return f"{self._TOOL_FIRST_PREAMBLE} {directive}"
        return directive

    def _build_compiled_workflow_summary(self, workflow: dict[str, object] | None) -> str:
        if not isinstance(workflow, dict):
            return ""
        actions = workflow.get("actions") if isinstance(workflow.get("actions"), list) else []
        if not actions:
            return ""
        rendered = []
        for action in actions[:3]:
            if not isinstance(action, dict):
                continue
            trigger = str(action.get("trigger") or "?")
            kind = str(action.get("kind") or "?")
            rendered.append(f"`{trigger} -> {kind}`")
        if not rendered:
            return ""
        if len(actions) > len(rendered):
            rendered.append(f"and {len(actions) - len(rendered)} more")
        return ", ".join(rendered)


_REQUIRED_PRETOOLUSE_MATCHER_TOKENS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash",
    "MultiEdit",
    "Patch",
    "Search",
    "ListDir",
    "Task",
    "NotebookEdit",
    # 2026-04-21: ScheduleWakeup MUST match the PreToolUse matcher or
    # the force-wakeup guard's own stamp path never fires when the
    # agent dutifully calls it, creating a catch-22 where every tool
    # is refused and the operator has to manually stamp the sqlite
    # column. See session 2026-04-18 for the live repro.
    "ScheduleWakeup",
    # 2026-04-27 (#68): shell-equivalent host tools must match the
    # PreToolUse matcher or destructive ops slip through entirely.
    # Pre-fix, PowerShell tool calls bypassed the gate cascade — the
    # MCP server had `_RAW_SHELL_TOOLS = {"bash","powershell","pwsh",
    # "cmd","wsl","monitor"}` registered, but Claude Code never even
    # invoked the hook for non-matched tool names. Verified live:
    # `Remove-Item -LiteralPath ... -Force` deleted a tempfile with
    # zero gate fire (handoff issue #1, this castle session repro).
    # The self-repair below unions these into existing installs so
    # operators don't have to re-run setup to get coverage.
    "PowerShell",
    "Pwsh",
    "Cmd",
    "Wsl",
    "Monitor",
    "mcp__.*",
)


def _self_repair_settings_json() -> None:
    """Passively repair drift in ~/.claude/settings.json on every hook run.

    Two repair axes currently in scope:

    1. Backslashed AIDOCS hook commands (Windows-specific; CC won't
       launch a command whose path contains \\).

    2. Stale PreToolUse matcher missing required tokens. The canonical
       matcher in cli.py evolves (e.g. adding ScheduleWakeup in
       2026-04-21) but installs that pre-date each update keep the
       old regex. Every hook invocation unions the required tokens
       into the matcher in place so existing installs catch up
       without re-running `aidocs setup`.

    Cheap stat + one read per hook invocation; rewrites only on drift
    and are idempotent.
    """
    try:
        p = Path.home() / ".claude" / "settings.json"
        if not p.is_file():
            return
        raw = p.read_text(encoding="utf-8")
        try:
            cfg = json.loads(raw)
            changed = False
        except Exception:
            # SELF-HEAL an invalid-JSON file caused by single-backslash AIDOCS
            # hook paths: forward-slash them in raw text, then re-parse and
            # persist. If it still won't parse, the outer except gives up.
            from .claude_hooks_install import repair_raw_aidocs_backslashes

            cfg = json.loads(repair_raw_aidocs_backslashes(raw))
            changed = True

        # Backslash repair on command paths.
        if "\\" in raw:
            for groups in (cfg.get("hooks") or {}).values():
                if not isinstance(groups, list):
                    continue
                for group in groups:
                    if not isinstance(group, dict):
                        continue
                    for h in group.get("hooks") or []:
                        cmd = h.get("command", "") if isinstance(h, dict) else ""
                        if "aidocs_mcp" in cmd and "\\" in cmd:
                            h["command"] = cmd.replace("\\", "/")
                            changed = True

        # Matcher drift repair on PreToolUse.
        pt_groups = (cfg.get("hooks") or {}).get("PreToolUse")
        if isinstance(pt_groups, list):
            for group in pt_groups:
                if not isinstance(group, dict):
                    continue
                # Only repair groups where the inner hook actually
                # invokes aidocs_mcp — leave foreign hooks' matchers
                # alone.
                inner = group.get("hooks") or []
                touches_aidocs = any(
                    "aidocs_mcp" in str(h.get("command", "")) for h in inner if isinstance(h, dict)
                )
                if not touches_aidocs:
                    continue
                current = str(group.get("matcher") or "")
                parts = [p.strip() for p in current.split("|") if p.strip()]
                missing = [t for t in _REQUIRED_PRETOOLUSE_MATCHER_TOKENS if t not in parts]
                if missing:
                    parts.extend(missing)
                    group["matcher"] = "|".join(parts)
                    changed = True

        # PostToolUse universal-audit repair (2026-04-21). Older installs
        # registered PostToolUse with matcher="TodoWrite" (narrow) or
        # didn't register it at all. The new universal-audit path needs
        # every tool to fire PostToolUse so execution_events accumulates
        # a native_tool_use row for each. Ensure the group exists with
        # the canonical matcher.
        hooks_root = (
            cfg.setdefault("hooks", {})
            if isinstance(cfg.get("hooks"), dict) or "hooks" not in cfg
            else cfg["hooks"]
        )

        # Pick a template AIDOCS hook command from any existing group;
        # used to clone into new hook events (PostToolUse / Stop /
        # SubagentStop) when we need to register them ourselves.
        template_cmd = None
        for _ev_key in ("PreToolUse", "UserPromptSubmit", "SessionStart"):
            for group in hooks_root.get(_ev_key) or []:
                if not isinstance(group, dict):
                    continue
                for h in group.get("hooks") or []:
                    if isinstance(h, dict) and "aidocs_mcp" in str(h.get("command", "")):
                        template_cmd = dict(h)
                        break
                if template_cmd is not None:
                    break
            if template_cmd is not None:
                break

        # Stop / SubagentStop audit (2026-04-21). Captures turn
        # boundaries so execution_events spans the full turn lifecycle.
        # Unconditional register-if-missing — these events have no
        # matcher (they fire on every stop).
        for _stop_event in ("Stop", "SubagentStop"):
            stop_groups = hooks_root.get(_stop_event)
            if not isinstance(stop_groups, list):
                stop_groups = []
                hooks_root[_stop_event] = stop_groups
            already_registered = any(
                isinstance(g, dict)
                and any(
                    "aidocs_mcp" in str(h.get("command", ""))
                    for h in (g.get("hooks") or [])
                    if isinstance(h, dict)
                )
                for g in stop_groups
            )
            if not already_registered and template_cmd is not None:
                stop_groups.append({"hooks": [template_cmd]})
                changed = True

        post_groups = hooks_root.get("PostToolUse")
        if not isinstance(post_groups, list):
            post_groups = []
            hooks_root["PostToolUse"] = post_groups
        # Find any existing aidocs-owned PostToolUse group.
        aidocs_post_group = None
        for group in post_groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks") or []
            if any("aidocs_mcp" in str(h.get("command", "")) for h in inner if isinstance(h, dict)):
                aidocs_post_group = group
                break
        if aidocs_post_group is None:
            # No PostToolUse group at all — create the canonical one by
            # cloning the first PreToolUse aidocs group's command spec.
            # We only mutate if we have a template to clone (otherwise
            # leave settings.json alone — self_repair is best-effort).
            template_cmd = None
            for group in hooks_root.get("PreToolUse") or []:
                if not isinstance(group, dict):
                    continue
                for h in group.get("hooks") or []:
                    if isinstance(h, dict) and "aidocs_mcp" in str(h.get("command", "")):
                        template_cmd = dict(h)
                        break
                if template_cmd is not None:
                    break
            if template_cmd is not None:
                post_groups.append(
                    {
                        "matcher": "|".join(_REQUIRED_PRETOOLUSE_MATCHER_TOKENS),
                        "hooks": [template_cmd],
                    },
                )
                changed = True
        else:
            current = str(aidocs_post_group.get("matcher") or "")
            parts = [p.strip() for p in current.split("|") if p.strip()]
            missing = [t for t in _REQUIRED_PRETOOLUSE_MATCHER_TOKENS if t not in parts]
            if missing:
                parts.extend(missing)
                aidocs_post_group["matcher"] = "|".join(parts)
                changed = True

        if changed:
            p.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    except Exception:
        # Self-repair is best-effort. A bad settings.json is worse if we
        # make it empty; leave the user's file intact and let them re-run
        # `aidocs setup` if repair keeps being needed.
        return


def _hook_integrity_ok() -> bool:
    """Trusted-code boundary for the hook. Returns False on proven drift of a
    non-editable install so the hook DECLINES to run its (possibly tampered)
    enforcement logic — fail-closed without bricking the user's session.
    Editable/unverified installs and any internal check error return True (the
    hook still runs locally; remote trust is handled separately).
    """
    try:
        from . import package_integrity as _pi

        v = _pi.startup_integrity_gate(Path.home())
        return bool(v.get("ok"))
    except Exception:
        return True


def main() -> None:
    if not _hook_integrity_ok():
        import os as _os

        if str(_os.environ.get("AIDOCS_ALLOW_PACKAGE_DRIFT") or "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            sys.stderr.write(
                "AIDOCS hook declining: installed package drifted from the "
                "verified runtime manifest (run `aidocs runtime "
                "--record-package` after a legit upgrade).\n",
            )
            return
    _self_repair_settings_json()
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    response = ClaudeHookHandler().handle(payload)
    if response is not None:
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
