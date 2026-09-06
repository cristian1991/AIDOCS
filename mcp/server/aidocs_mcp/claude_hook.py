# ══════════════════════════════════════════════════════════════════════════
#  ⚠️  DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST  ⚠️
# ──────────────────────────────────────────────────────────────────────────
#  Live verification (2026-06-11) of the ungated additive-protection path. claude_hook.py is the UPS/PreToolUse host-hook entrypoint and grant-minting pipeline — the agent chose this file by recognition, no grant phrase needed.
#
# ══════════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Article XXV inversion (2026-07-13, king's order): this module is the Claude
# Code envelope ADAPTER. It imports the AIDOCS core — the core never hangs off
# it — and it does so LAZILY: module scope is stdlib-only, so the broker-first
# ``main()`` pays interpreter + stdlib + loopback client per hook spawn, and
# the fat gate stack loads only on the local-fallback evaluation path.
# Structural pin: tests/host/test_claude_hook_thin_entry.py fails if any core
# module creeps back into this module's import graph.


class _LazyCoreAlias:
    """Back-compat class-attr alias (S2 rip) resolved on first ACCESS.

    The canonical tables live in the core (hook_pipeline /
    prompt_context_service); tests read them via ``ClaudeHookHandler._X``.
    A plain class-body assignment would force the core import at module
    import time — the exact adapter-imports-the-kingdom inversion Article
    XXV forbids — so the alias resolves on access and never caches, keeping
    the core table the single source of truth (Article XXII).
    """

    def __init__(self, module: str, attr: str) -> None:
        self._module = module
        self._attr = attr

    def __get__(self, obj: object, _objtype: type | None = None):
        import importlib

        return getattr(importlib.import_module(self._module, __package__), self._attr)


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
        # Lazy core imports (Article XXV): constructing the handler IS the
        # local-evaluation path, the only place the fat stack is owed.
        from .runtime_service import RuntimeService
        from .service_hub import AidocsServiceHub

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
        # And the same treatment for the INDEX db (#489): a warm
        # UserPromptSubmit opened aidocs.sqlite3 ~116 times through
        # SQLiteIndexStoreBase.session(), ~1.2ms each just to open a file that was
        # open moments earlier. Beyond the latency, hundreds of opens per prompt
        # contend on one file — with many agents running concurrently, readers and
        # writers queue on WAL locks and the whole surface crawls. Both scopes
        # close on exit, so no handle (and no WAL lock) outlives the request.
        from ._sqlite_index_store_base import SQLiteIndexStoreBase
        from .intent_tokens_store import request_cache

        # ── SUBAGENT IDENTITY SCOPE (2026-08-22) ──────────────────────────
        # SECURITY-GRADE JUSTIFICATION for editing this banner-protected file
        # (the operator explicitly requested this work):
        #
        # PreToolUse/PostToolUse fire INSIDE a subagent and carry `agent_id`,
        # but a subagent's `session_id` is its PARENT's verbatim. Until this
        # stamp existed, every in-subagent tool call resolved to the parent's
        # agent_context_id: N subagents scored as ONE actor, and on 2026-08-21
        # three subagents earning one strike each locked out a conductor that
        # had done nothing.
        #
        # WHY HERE, AND NOWHERE ELSE. set_request_host_identity's own docstring
        # names this caller — "the SUBAGENT axis, and the ONE axis no transport
        # delivers. A caller that holds a hook payload (the only place it
        # exists) stamps it here." A hook payload exists in exactly two
        # evaluators, and BOTH funnel through this method: the resident broker
        # (hook_broker.py, "nothing is duplicated") and main()'s local
        # fallback. One stamp here therefore covers every hook evaluation in
        # both processes; anywhere else would cover only one of them.
        #
        # try/FINALLY IS THE SECURITY PROPERTY, not tidiness. The broker serves
        # many events for DIFFERENT subagents from one long-lived process, so a
        # binding that outlived its event would hand the next subagent a
        # sibling's identity — and a sibling's strike history.
        #
        # NO SESSION ID ⇒ NO SCOPE, deliberately. Opening the scope sets
        # _request_identity_scoped, and current_calling_host_session_id then
        # returns "" INSTEAD of the process stamp (#672). Doing that for a
        # payload carrying no session id would silently withdraw the legacy
        # single-window stdio fallback from the entire hook path — a real
        # regression bought for no gain, since there is no identity to add.
        _host_session_id = str(payload.get("session_id") or "").strip()
        if not _host_session_id:
            with request_cache(), SQLiteIndexStoreBase.connection_scope():
                return self._handle_impl(payload)

        from .mcp_server_runtime_helpers import (
            reset_request_host_identity,
            set_request_host_identity,
        )

        _identity_token = set_request_host_identity(
            _host_session_id,
            host_kind="claude_code",
            # Blank/absent is stored as ABSENT by the setter, so the main
            # thread (which sends no agent_id) keeps the identity it has
            # always had, byte for byte.
            agent_id=str(payload.get("agent_id") or "").strip(),
        )
        try:
            with request_cache(), SQLiteIndexStoreBase.connection_scope():
                return self._handle_impl(payload)
        finally:
            reset_request_host_identity(_identity_token)

    def _handle_impl(self, payload: dict[str, object]) -> dict[str, object] | None:
        event_name = str(payload.get("hook_event_name") or "").strip()
        if not event_name:
            return None

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
            # #224 follow-up: the gate cascade resolves ~37 config settings per
            # tool call, each re-reading the same layer DBs (~120ms/call, >half
            # the per-call cost the operator feels — the PreToolUse hook is a
            # fresh process per tool call for Claude Code). request_config_scope
            # reads each layer ONCE and filters in memory; verdict-identical
            # (config writes call invalidate_request_config_scope). Measured the
            # gate cascade 164ms → 79ms median.
            from .config_resolver import request_config_scope

            # #1007 subagent axis: see _record_xaacp_call_claim for why HERE.
            self._record_xaacp_call_claim(project_root, payload)
            with request_config_scope():
                return self._handle_pre_tool_use(project_root, payload)
        if event_name == "PostToolUse":
            return self._handle_post_tool_use(project_root, payload)
        if event_name == "PostCompact":
            return self._handle_post_compact(project_root, payload)
        if event_name in ("Stop", "SubagentStop"):
            return self._handle_stop(project_root, payload, event_name)
        # SubagentStart is placed AFTER the enforcement arms on purpose: it is
        # a pure observer (see _handle_subagent_start) and must never sit on a
        # path that could shadow a verdict-bearing event. The generic
        # _record_hook_event above has already fired for it; this arm adds the
        # per-subagent identity that the generic row's payload allowlist drops.
        if event_name == "SubagentStart":
            return self._handle_subagent_start(project_root, payload)
        return None

    def _handle_aidocs_command(self, payload: dict[str, object]) -> dict[str, object]:
        """Thin CC adapter — core in hook_pipeline.run_aidocs_command (S6, #251)."""
        from . import hook_pipeline as _hp

        return _hp.run_aidocs_command(self.runtime, payload, host_kind="claude_code")

    def _handle_session_start(
        self,
        project_root: Path,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Thin CC adapter — core in hook_pipeline.on_session_start (S6, #251)."""
        from . import hook_pipeline as _hp

        return _hp.on_session_start(self.runtime, project_root, payload, host_kind="claude_code")

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
    def capture_durable_only(self, payload: dict[str, object]) -> bool:
        """Run ONLY the durable half of the UPS pipeline (#489 Step 4).

        Called when a deadline forced the broker to drop a UserPromptSubmit. The
        operator's prompt and its intent are DURABLE state; memory, doctrine and
        palace surfacing are PRESENTATIONAL. The old drop path skipped both,
        which spent the signal AIDOCS is built on ("user-intent is what makes
        AIDOCS work") to save enrichment nobody would have missed.

        COMPOSES, NEVER COPIES (Article XXII). Both legs are the exact callables
        the full pipeline uses:
          * hook_pipeline._ups_safety_screen — the always-safe head, whose first
            act is PromptMutator.record_user_prompt_received (stage 1 of the
            pinned GOLDEN_UPS_TRACE: the user_prompt_received audit row carrying
            prompt_hash, prompt_len and cli_session_id).
          * update_intent_hook.process_user_prompt + durable_hint_store —
            the #219/#221/#9 update-intent durability rail, which persists the
            pending_durable_write row. In the full pipeline this rides inside
            _ups_advise_rails; here it is reached directly because the rails
            around it are all presentational.

        DOES NOT reorder _run_user_prompt_core, so GOLDEN_UPS_TRACE stays green.
        This is a separate degraded-path composer, not a new ordering of the
        canonical one.

        Deliberately does NOT run the authority-bearing mutation stages (SEC-001
        snapshot / SEC-002 grant cascade / intent-phrase dispatch). Those MINT
        PRIVILEGE, and a path that runs after the client has stopped listening
        must never grant anything — the operator would never see the
        acknowledgement, so a grant minted here would be invisible authority.
        Recording and capturing is durable; granting is not merely presentational
        but actively unsafe to do unobserved.

        Returns True when the durable capture ran. Fail-quiet by contract.
        """
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return False
        from .intent_tokens_store import request_cache

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return False
        with request_cache():
            from . import hook_pipeline as _hp

            # Bind this host first, or the audit row this whole rescue exists
            # to save is silently dropped. #672 made managed-mode resolution
            # refuse a host with no per-conductor row, and
            # record_user_prompt_received returns empty on an inactive resolve
            # — so on the very drop path whose contract is "the prompt MUST be
            # recorded", an unbound host recorded NOTHING. The bind is identity,
            # not privilege: it mints no grant, so it does not violate this
            # method's rule that a path running unobserved must never grant.
            # See PromptMutator.ensure_host_session_bound. Idempotent.
            try:
                from .prompt_mutator import PromptMutator as _PromptMutatorBind

                _PromptMutatorBind(self.runtime).ensure_host_session_bound(
                    project_root=project_root,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                )
            except Exception:  # noqa: BLE001 — a rescue must never break the drop
                pass
            # Always-safe head: the UPS audit row + secret block + preflight.
            # A block envelope here is irrelevant (nobody is listening) but the
            # audit row it wrote is exactly what must survive.
            try:
                _hp._ups_safety_screen(self.runtime, prompt, payload, project_root)
            except Exception:  # noqa: BLE001 — the intent rail still owes work
                pass
            # Update-intent durability rail (#219/#221) + durable-content hint
            # (#9). This is the "capture the intent" half.
            try:
                from .managed_mode_service import resolve_managed_session

                session_id = resolve_managed_session(
                    self.runtime.hub.managed_mode,
                    project_root,
                    host_session_id=str(payload.get("session_id") or "").strip(),
                )
                if session_id:
                    from .update_intent_hook import process_user_prompt

                    process_user_prompt(project_root, session_id, prompt)
                    try:
                        from .durable_hint_store import observe_content

                        observe_content(project_root, session_id, prompt)
                    except Exception:  # noqa: BLE001 — sibling advise rail
                        pass
            except Exception:  # noqa: BLE001 — never break the degraded path
                pass
        return True

    def _handle_user_prompt_submit(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Thin CC adapter — the pipeline lives in hook_pipeline.run_user_prompt
        (S4 rip, #251). Return contract unchanged (verbatim relocation)."""
        from . import hook_pipeline as _hp

        return _hp.run_user_prompt(
            self.runtime, project_root, payload, host_kind="claude_code",
        )

    # ── User-intent raw tool grants ──
    # Grant raw tool access for the next tool call when the user's prompt
    # contains BOTH a grant verb ("allow", "enable", "use", "run"...) AND
    # a tool token ("grep", "bash", "glob"...) within proximity of each
    # other. Requiring both stops casual mentions ("I usually grep for X",
    # "in bash you'd...") from unlocking raw tools. Cleared on next
    # UserPromptSubmit.

    # Legacy indirect phrasings that imply a raw tool without naming it.
    # SINGLE SOURCE (identity-spine rip, 2026-07-06): the user-intent phrase
    # tables live in canonical_intent_registry — the same tables PromptMutator's
    # detect_user_intent_tools_v2 matches against (verified byte-identical at
    # rip time). These class attrs are ALIASES for back-compat readers (the
    # injection-resistance tests import them via ClaudeHookHandler), so the
    # hook can no longer drift from the detector it fronts.
    # Lazy aliases (Article XXV): resolved on access, never a module-scope
    # core import.
    _DIRECT_INTENT_PHRASES = _LazyCoreAlias(
        ".canonical_intent_registry", "USER_INTENT_DIRECT_PHRASES"
    )
    _GRANT_PROXIMITY = _LazyCoreAlias(".canonical_intent_registry", "USER_INTENT_GRANT_PROXIMITY")
    _GRANT_VERB_PHRASES = _LazyCoreAlias(
        ".canonical_intent_registry", "USER_INTENT_GRANT_VERB_PHRASES"
    )
    _OBSERVATIONAL_PREFIXES = _LazyCoreAlias(
        ".canonical_intent_registry", "USER_INTENT_OBSERVATIONAL_PREFIXES"
    )
    _TOOL_TOKEN_PATTERNS = _LazyCoreAlias(
        ".canonical_intent_registry", "USER_INTENT_TOOL_TOKEN_PATTERNS"
    )

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
            # Freeze vs stewardship deadlock (2026-07-17, live incident): a frozen
            # session cannot run ANY tool (ai_failures included), so the
            # stewardship Stop-block's demand to triage/dispose is unperformable —
            # the turn can never seal, and the agent loops on dozens of blocked
            # turns. When an active freeze is in force, the stewardship duty YIELDS
            # (deferred to thaw) so the turn can seal; duty resumes when the freeze
            # lifts (the blockers stay in the ledger). Fail-closed is preserved: an
            # UNfrozen session still cannot seal with untriaged duty.
            frozen = self._active_freeze_for_stop(project_root, payload)
            if frozen is None:
                return {"decision": "block", "reason": gate.block_reason}
            self._note_stewardship_deferred(project_root, payload, frozen, gate)

        # #219 PR-2: update-intent durability gate — hold the turn-seal when an
        # operator update was detected this session but is not yet recorded
        # durably (nlp.update_gate=block). Mirrors the stewardship block
        # envelope; advise/off and ambiguous detections never block.
        from .update_intent_hook import gate_stop as _ui_gate_stop

        _sid = str(payload.get("session_id") or "").strip()
        if _sid:
            _ug = _ui_gate_stop(project_root, _sid)
            if _ug.get("block"):
                return {"decision": "block", "reason": str(_ug["reason"])}

        # Deploy-wait = conducting prep (2026-07-12): a CONDUCTOR stopping while
        # a crown deploy is still pre-SAFE-TO-EDIT gets ordered ONCE per deploy
        # to scout the next war read-only instead of idling. Conductor-only
        # (SubagentStop excluded inside the gate too); fail-open by contract.
        try:
            from .deploy_edit_window import conduct_the_wait_stop_gate

            _nudge = conduct_the_wait_stop_gate(
                project_root,
                event_name=event_name,
                host_session_id=_sid,
            )
            if _nudge is not None:
                return _nudge
        except Exception:
            pass

        # #419 War DD: never seal a turn silently while open backlog work is
        # unmentioned. Blocks the clean stop ONCE per (epoch, backlog-state)
        # with an explicit tell-the-user instruction, then yields — dedupe is
        # strict (no session id / broken ledger → no block, never a loop).
        # SubagentStop is excluded inside the surfacer (worker fencing §III).
        try:
            from .backlog_surfacer import stop_backlog_reminder

            _bl = stop_backlog_reminder(
                project_root,
                event_name=event_name,
                host_session_id=_sid,
            )
            if _bl is not None:
                return _bl
        except Exception:
            pass
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

            from .mcp_server_runtime_helpers import (
                current_calling_agent_id,
                current_calling_host_kind,
            )

            # THE DUTY IDENTITY IS COMPOSED IN ONE PLACE, not two (operator
            # ruling 2026-08-29). `payload["session_id"]` alone is the PARENT
            # conversation for a subagent too -- measured on Claude Code 2.1.239,
            # a subagent's hook payload carries its parent's session_id and its
            # parent's transcript_path, and only `agent_id` differs -- so N
            # concurrent lane agents collapsed into ONE duty holder, each able to
            # autoclear the others' blockers on a green run they were never part
            # of. The agent id is read from the REQUEST SCOPE rather than the
            # payload so the Stop hook and `ai_failures` draw from the same
            # channel; `handle()` stamps it from this same payload.
            session_id = fs.compose_failure_duty_id(
                project_root=project_root,
                host_session_id=str(payload.get("session_id") or "").strip(),
                agent_id=current_calling_agent_id(),
                host_kind=current_calling_host_kind() or "claude_code",
            )
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

    def _active_freeze_for_stop(self, project_root: Path, payload: dict[str, object]):
        """Return the active freeze governing this Stop-hook turn, or None.

        Best-effort + fail-open toward BLOCKING: any error returns None, so a
        lookup crash never masks a real untriaged-duty seal-block (the
        stewardship gate stays authoritative when we can't confirm a freeze).
        """
        try:
            from .freeze_service import get_existing_freeze

            sid = str(payload.get("session_id") or "").strip()
            if not sid:
                return None
            return get_existing_freeze(
                project_root,
                sid,
                host_session_id=sid,
                host_kind="claude_code",
            )
        except Exception:
            return None

    def _note_stewardship_deferred(
        self,
        project_root: Path,
        payload: dict[str, object],
        frozen,
        gate,
    ) -> None:
        """Record the one-line 'stewardship deferred: session frozen (<freeze_id>)'
        note when a frozen turn seals with duty outstanding. Best-effort audit only
        — never raises, never blocks the seal."""
        try:
            freeze_id = str(getattr(frozen, "request_id", "") or "")
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="stewardship_deferred_frozen",
                source_kind="stop",
                session_id=str(payload.get("session_id") or "").strip(),
                payload={
                    "note": f"stewardship deferred: session frozen ({freeze_id})",
                    "freeze_id": freeze_id,
                    "freeze_kind": str(getattr(frozen, "kind", "") or ""),
                    "seal_blockers": list(getattr(gate, "seal_blockers", []) or []),
                },
            )
        except Exception:
            pass

    @staticmethod
    def _read_turn_scan_text(payload: dict[str, object], report_text: str) -> str:
        """Thin delegate — transcript scanning lives in failure_stewardship
        (host-agnostic; identity-spine rip 2026-07-06). ``report_text`` is
        deliberately unused: reports are lint-only, never registration."""
        from . import failure_stewardship as fs

        return fs.scan_text_from_transcript(str(payload.get("transcript_path") or ""))

    @staticmethod
    def _extract_tool_result_text(obj: object) -> str:
        from . import failure_stewardship as fs

        return fs.extract_tool_result_text(obj)

    @staticmethod
    def _extract_text(obj: object) -> str:
        from . import failure_stewardship as fs

        return fs.extract_text(obj)

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
        # S2 rip: body moved to hook_pipeline.on_post_compact (host-agnostic);
        # this adapter passes its OWN host_kind.
        from . import hook_pipeline as _hp

        return _hp.on_post_compact(
            self.runtime,
            project_root,
            payload,
            host_kind="claude_code",
        )

    # ── SubagentStart (Claude Code 2.1.239, payload MEASURED 2026-08-22) ──
    #
    # SECURITY-GRADE JUSTIFICATION for editing this banner-protected file
    # (the operator explicitly requested this work):
    #
    # A CC subagent inherits its PARENT's `session_id` AND `transcript_path`
    # verbatim — `agent_id` is the ONLY per-subagent axis the host gives us.
    # Any actor derived from host_session_id alone therefore COLLAPSES N
    # concurrent subagents into one identity: on 2026-08-21 three subagents
    # earning one strike each scored as ONE actor earning three, and the
    # lockdown that followed landed on a conductor that had done nothing.
    # This arm is the first place the host names the spawn, so it is the
    # first place attribution is even possible.
    #
    # WHY A DEDICATED WRITE, when `_record_hook_event` already fired for this
    # event in `_dispatch_event`: hook_pipeline.record_hook_event narrows the
    # payload to {"hook_event_name","tool_name","tool_input","prompt","cwd"}
    # before persisting it, so that generic row DROPS `agent_id`/`agent_type`
    # and names nobody. Widening that allowlist is a core (hook_pipeline)
    # change; this arm records the identity fact the adapter uniquely holds.
    #
    # OBSERVER ONLY — no verdict, no envelope, no influence on the spawn.
    # Nothing in the 2026-08-22 capture established that CC accepts a
    # `hookSpecificOutput` on SubagentStart, and `on_post_compact` documents
    # CC REJECTING that shape on at least one event; guessing a new envelope
    # buys no enforcement, because PreToolUse still gates every tool the
    # subagent tries. The body is wrapped fail-open ON TOP OF `_handle_impl`'s
    # outer net (defence in depth): observation must never wedge a spawn.
    def _record_xaacp_call_claim(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> None:
        """#1007/#1015: bind EVERY aidocs MCP call to the identity issuing it.

        #1015 (operator ruling 2026-09-04) widened this from subagents only to
        every ``mcp__aidocs__*`` call, because the daemon must tell "the MAIN
        THREAD called" from "a subagent called and its claim is gone" -- and
        both used to arrive as a bare absence. A payload with no ``agent_id``
        is the main thread, and is claimed under
        ``XAACP_MAIN_THREAD_AGENT``.

        NO LONGER FAIL-OPEN IN EFFECT. The write itself is still wrapped (an
        observation must never wedge a tool call), but a claim that never
        lands no longer donates the conductor's actor: on a conversation that
        HAS claimed before, the daemon refuses the call instead. That is the
        ruling -- a call the gate never saw must not silently gain the
        conductor's identity.
        """
        try:
            host_agent_id = str(payload.get("agent_id") or "").strip()
            tool_name = str(payload.get("tool_name") or "").strip()
            # Only the aidocs MCP surface reaches the daemon; claiming Bash or
            # Edit would write rows nothing can ever take.
            if not tool_name.startswith("mcp__aidocs__"):
                return
            from .conductor_comms import xaacp_record_call_claim

            xaacp_record_call_claim(
                project_root,
                host_session_id=str(payload.get("session_id") or "").strip(),
                host_agent_id=host_agent_id,
                tool_name=tool_name,
                tool_input=payload.get("tool_input"),
            )
        except Exception:
            pass

    def _handle_subagent_start(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Record a CC subagent spawn under the SUBAGENT's own identity."""
        # #1007: establish the subagent's OWN xaacp_actors row (kind
        # subagent, parent's host_session_id, its host_agent_id) so the
        # directory lists it and XAACP can address it. Registration requires
        # the parent's managed binding; without one there is no session to
        # scope the row to, and nothing is minted. Independent of the
        # observation write below: neither may wedge the other.
        try:
            from .conductor_comms import xaacp_register_host_actor

            xaacp_register_host_actor(
                project_root,
                host_session_id=str(payload.get("session_id") or "").strip(),
                host_kind="claude_code",
                actor_kind="subagent",
                host_agent_id=str(payload.get("agent_id") or "").strip(),
                source="subagent_start",
            )
        except Exception:
            pass
        try:
            host_session_id = str(payload.get("session_id") or "").strip()
            if not host_session_id:
                # Fail CLOSED on attribution: derive_agent_context_id returns
                # "" for an empty host session, and a row keyed on "" is
                # addressable by every actor at once. No identity ⇒ no row.
                return None
            agent_id = str(payload.get("agent_id") or "").strip()

            from .agent_memory_epoch import derive_agent_context_id

            agent_context_id = derive_agent_context_id(
                host_kind="claude_code",
                project_root=project_root,
                host_session_id=host_session_id,
                # Blank/absent is byte-identical to the historical v1
                # derivation, so a host that omits the field can never fork
                # the MAIN conductor into a second actor.
                agent_id=agent_id or None,
            )
            if not agent_context_id:
                return None

            # Mirror record_hook_event's session axis: the AIDOCS managed
            # session, with the raw host session kept in the payload so the
            # two are never conflated by a reader.
            try:
                _managed = self.runtime.hub.managed_mode.get_mode(project_root)
                _session_id = str(_managed.get("session_id") or "").strip() or None
            except Exception:
                _session_id = None

            from .tool_call_log import record as _log_record

            # IDEMPOTENT BY DERIVATION, not by row count. SubagentStart
            # RE-FIRES for the SAME agent_id when that agent resumes after its
            # own child finishes (measured: a26c3b2da0c816517 at 22:06:26 and
            # again at 22:06:34). execution_events is append-only, so a
            # re-fire is legitimately a second OBSERVATION — but the identity
            # it carries is a pure function of (project, host_session_id,
            # agent_id), so repeated fires converge on ONE actor instead of
            # minting a fresh one and resetting its strike history.
            _log_record(
                self.runtime.hub,
                project_root,
                phase="subagentstart",
                name=None,
                payload={
                    "hook_event_name": "SubagentStart",
                    "agent_id": agent_id,
                    "agent_type": str(payload.get("agent_type") or "").strip(),
                    "agent_context_id": agent_context_id,
                    # Per user TURN, shared by every agent spawned in it — the
                    # only sibling-grouping axis the host provides. There is NO
                    # `parent_agent_id` on the wire: lineage and depth are NOT
                    # knowable here and are deliberately not invented.
                    "prompt_id": str(payload.get("prompt_id") or "").strip(),
                    "host_session_id": host_session_id,
                },
                session_id=_session_id,
                source="claude_hook",
                action_kind="subagent_spawn",
                status="observed",
            )
        except Exception:
            # Observation must never break a spawn.
            pass
        return None

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
        # CONNECTED login gate (#404 wiring): login is required to use
        # AIDOCS. An unauthenticated caller (no valid operator token, no
        # approved host binding) is denied EVERY tool with actionable
        # login guidance. Authentication uncertainty fails closed; a resolver
        # exception can never become authority. An authenticated caller passes.
        try:
            from . import login_gate as _lg

            _login_block = _lg.login_required_block(
                project_root, str(payload.get("session_id") or "").strip()
            )
        except Exception:
            _login_block = {
                "blocked_by": "login_required",
                "reason": (
                    "AIDOCS could not verify an authenticated operator. "
                    "Sign in with `aidocs operator-login` or use the "
                    "Dashboard/Codenexus login flow, then retry."
                ),
            }
        if _login_block is not None:
            return self._deny_envelope(
                _login_block["reason"], _login_block["blocked_by"]
            )

        # Mid-flight UPS (claude adapter): a prompt the operator submitted MID-TURN never
        # reached UserPromptSubmit — CC enqueues it and drains it into a queued_command
        # attachment, bypassing the whole UPS pipeline. PreToolUse is the only hook CC
        # fires mid-turn, so re-play UPS on any un-judged mid-flight enqueue HERE: a
        # hostile one denies the pending tool (block-before-action); its memory/intent
        # context rides the allow path below. Fail-open — never wedge the host on it.
        try:
            from . import mid_flight_ups as _mf

            _mf_env = _mf.evaluate(self.runtime, project_root, payload)
        except Exception:
            _mf_env = None
        _mf_context = ""
        if _mf_env is not None:
            _mf_hso = _mf_env.get("hookSpecificOutput", {})
            if _mf_hso.get("permissionDecision") == "deny":
                return _mf_env
            _mf_context = str(_mf_hso.get("additionalContext") or "")

        # S3 rip: the whole decision pipeline (native-shell 2.0-A branch,
        # lane-worker stamp/auto-bind, evaluate_tool with data-producing
        # gate hooks, ShellPolicy shadow) moved to
        # hook_pipeline.decide_tool_use. This adapter only renders the
        # returned ToolUseDecision into CC hookSpecificOutput envelopes.
        from . import hook_pipeline as _hp

        decision = _hp.decide_tool_use(
            self.runtime,
            project_root,
            payload,
            host_kind="claude_code",
        )

        # Pre-rendered passthrough (shell_adapter 2.0-A envelope, or a
        # defensive foreign host_envelope) — returned as-is apart from the
        # mid-flight carry below.
        env = decision.meta.get("host_envelope")
        if env is not None:
            return self._carry_mid_flight_context(env, _mf_context)

        if decision.verdict == "freeze":
            out: dict[str, object] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
                "blocked_by": decision.blocked_by or "session_frozen",
            }
            if decision.meta.get("freeze_state"):
                out["freeze_state"] = decision.meta["freeze_state"]
            return self._carry_mid_flight_context({"hookSpecificOutput": out}, _mf_context)

        if decision.verdict == "ask":
            return self._carry_mid_flight_context(
                self._ask_envelope(
                    decision.reason,
                    ask_kind=str(decision.meta.get("ask_kind") or ""),
                ),
                _mf_context,
            )

        if decision.verdict == "deny":
            return self._carry_mid_flight_context(
                self._deny_envelope(decision.reason, decision.blocked_by),
                _mf_context,
            )

        # Allow: render additional_context_blocks (conductor messages +
        # x-ray goggles) into additionalContext, else let CC proceed. The
        # mid-flight UPS context is appended by the single carrier below, on
        # this and on every other branch.
        blocks = list(decision.meta.get("additional_context_blocks") or [])
        env_out: dict[str, object] | None = None
        if blocks:
            env_out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(blocks),
                },
            }
        return self._carry_mid_flight_context(env_out, _mf_context)

    @staticmethod
    def _carry_mid_flight_context(
        env: dict[str, object] | None,
        mid_flight_context: str,
    ) -> dict[str, object] | None:
        """Append a mid-turn operator message's UPS context to whatever
        PreToolUse envelope this call is about to return (#476).

        WHY EVERY BRANCH. ``mid_flight_ups.evaluate`` commits its per-session
        cursor before returning, so the mid-turn prompt is judged exactly once,
        ever. Any branch that returned without this context therefore destroyed
        it — the operator's mid-turn memory surfacing, doctrine and intent
        grants vanished with nothing said and no second chance. The freeze /
        ask / deny / pre-rendered-envelope branches all did exactly that.

        STRICTLY ADDITIVE. Only ``additionalContext`` is touched, appended to
        anything already there; no verdict, reason or taxonomy key is read or
        rewritten, so a refusal refuses exactly as hard as before.
        """
        if not mid_flight_context:
            return env
        if env is None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": mid_flight_context,
                },
            }
        hso = env.get("hookSpecificOutput")
        if not isinstance(hso, dict):
            return env
        existing = str(hso.get("additionalContext") or "")
        hso["additionalContext"] = (
            f"{existing}\n{mid_flight_context}" if existing else mid_flight_context
        )
        return env

    @staticmethod
    def _join_response_text(resp: object) -> str:
        """Flatten a tool_response (str / dict / list) into scan text.

        Thin delegate — body moved to hook_pipeline.join_response_text (S2 rip).
        """
        from . import hook_pipeline as _hp

        return _hp.join_response_text(resp)

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
            # Route through the host-agnostic policy wrapper (was dead code —
            # claude_hook used to inline on_host_read_output and, crucially,
            # returned WITHOUT recording the redaction audit event; only bash was
            # audited). Now read + bash share one audit + envelope path.
            from .host_services.output_redaction_policy import evaluate_read_output

            rr = evaluate_read_output(
                runtime=self.runtime,
                project_root=project_root,
                path=path,
                text_view=text_view,
                host_session_id=str(payload.get("session_id") or ""),
                host_kind="claude_code",
                result_obj=tool_response,
            )
            if rr is None or rr.redacted is None:
                return None
            _redacted_out = rr.redacted
            _count, _cats, _mech = rr.redaction_count, list(rr.categories), rr.mechanism
            _withheld = False
        else:
            # bash / monitor: host-agnostic generic decision (gate + regex scan).
            decision = _hp.decide_generic_output_redaction(
                "claude_code",
                tool_name,
                tool_response,
                project_root=project_root,
            )
            if decision is None:
                return None
            _redacted_out = decision.redacted
            _count, _cats, _mech = decision.count, list(decision.categories), decision.mechanism
            # #401 gap 3: an uncertifiable scan is a WITHHOLD, not a redaction
            # — say so in the audit rather than logging a clean zero-count.
            _withheld = decision.withheld

        # Shared audit + Claude updatedToolOutput envelope. READ is now audited
        # too (previously the read branch returned before record_event).
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind=(
                    "host_output_withheld" if _withheld else "host_output_redacted"
                ),
                source_kind="post_tool_use",
                session_id=str(payload.get("session_id") or ""),
                capability_name=tool_name,
                action_kind="withhold" if _withheld else "redacted",
                status="blocked" if _withheld else "applied",
                payload={
                    "tool_name": tool_name,
                    "redaction_count": _count,
                    "categories": _cats,
                    "withheld": _withheld,
                    "host_kind": "claude_code",
                    "mechanism": _mech or "posttooluse.updatedToolOutput",
                },
            )
        except Exception:
            pass
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": _redacted_out,
            },
        }


    def _handle_post_tool_use(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Thin CC adapter — core in hook_pipeline.after_tool_use (S5, #251)."""
        from . import hook_pipeline as _hp

        return _hp.after_tool_use(
            self.runtime,
            project_root,
            payload,
            host_kind="claude_code",
            redact_output=lambda pr, pl: self._maybe_redact_read_output(pr, pl),
        )

    # _dispatch_task_lifecycle removed — logic moved to
    # LifecycleService.dispatch_todo_lifecycle.

    # Override phrases that lift the agent-brief research-block for the
    # current operator turn. Closed list — not heuristic. Operator has
    # to type the phrase exactly to opt into delegated research.
    # S2 rip: canonical table lives in hook_pipeline; class attr kept as
    # a back-compat alias for tests — lazy so the adapter's module scope
    # stays core-free (Article XXV).
    _AGENT_RESEARCH_OVERRIDE_PHRASES = _LazyCoreAlias(
        ".hook_pipeline", "AGENT_RESEARCH_OVERRIDE_PHRASES"
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

    # S2 rip: canonical allowlist lives in hook_pipeline.RECONNECT_ALLOWED_TOOLS;
    # class attr kept as a back-compat alias for tests — lazy (Article XXV).
    _RECONNECT_ALLOWED_TOOLS = _LazyCoreAlias(".hook_pipeline", "RECONNECT_ALLOWED_TOOLS")

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

        S2 rip: decision core (incl. the flag-clearing side effect) lives
        in hook_pipeline.decide_reconnect; this adapter only renders the
        CC deny envelope.
        """
        from . import hook_pipeline as _hp

        decision = _hp.decide_reconnect(
            self.runtime,
            project_root,
            tool_name,
            cli_session_id=cli_session_id,
        )
        if decision is None:
            return None
        return self._deny_envelope(
            str(decision["reason"]),
            blocked_by=str(decision["blocked_by"]),
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

        S2 rip: decision core lives in hook_pipeline.decide_session_freeze;
        this adapter only renders the CC PreToolUse deny envelope.
        (#588 D4: tool_name / tool_input are no longer decoration — the
        core needs them to tell the unfreeze and operator-notification
        surfaces, and out-of-jurisdiction paths, apart from work.)
        """
        from . import hook_pipeline as _hp

        decision = _hp.decide_session_freeze(
            self.runtime,
            project_root,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        if decision is None:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision["permissionDecisionReason"],
                "blocked_by": decision.get("blocked_by", "session_frozen"),
                "freeze_state": decision["freeze_state"],
            },
        }

    def _resolve_session_freeze(
        self,
        project_root: Path,
        prompt: str,
        *,
        host_session_id: str = "",
    ) -> None:
        """Thin delegate to PromptMutator.resolve_session_freeze
        (host-agnostic). Kept as a method on the hook for back-compat
        with any existing test that targets it directly; new code
        should use the service entry point.
        """
        from .prompt_mutator import PromptMutator

        PromptMutator(self.runtime).resolve_session_freeze(
            prompt=prompt,
            host_session_id=host_session_id,
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
        blocked_by: str = "",
        matched_rule: str = "",
        user_intent_detected: bool = False,
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
        from .verdict_class import OUTCOME_ALLOW, OUTCOME_BLOCK, outcome_for

        # #571 three-way routing. The callers of this method do not all thread
        # blocked_by/matched_rule yet, and the defaults ('' / '' / False)
        # classify to CLASS_SECURITY_FREEZE — i.e. an un-threaded caller keeps
        # EXACTLY its pre-#571 behaviour. This is the fail-closed direction on
        # purpose: a caller that has not been taught the ladder must not have
        # its refusals demoted to a free block.
        _ch_outcome, _ch_class = outcome_for(
            blocked_by=blocked_by,
            matched_rule=matched_rule,
            user_intent_detected=user_intent_detected,
        )
        if _ch_outcome == OUTCOME_ALLOW:
            # Intent already established → CC's "do not block" signal.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        "user-intent for this action was already detected"
                    ),
                },
            }
        if _ch_outcome == OUTCOME_BLOCK:
            from .freeze_service import build_workflow_block_response

            _blk = build_workflow_block_response(
                tool_name=tool_name,
                tool_input=tool_input,
                reason=judge_summary,
                verdict_class=_ch_class,
                # #588 D5: compose the envelope's claims from the freeze
                # store as it actually stands, not from this branch's
                # intent to write nothing.
                project_root=project_root,
                session_id=session_id,
            )
            # deny with NO freeze_state key — the absence is what stops CC and
            # every downstream latch from treating this as a frozen session.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _blk["permissionDecisionReason"],
                    "blocked_by": _blk["blocked_by"],
                    "block_state": _blk["block_state"],
                },
            }

        env = build_freeze_response(
            project_root,
            session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            judge_summary=judge_summary,
            admin_tier=admin_tier,
            verdict_class=_ch_class,
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

    def _get_current_lane_id(self, project_root: Path) -> str | None:
        """Get the current lane ID from gate state. Thin delegate —
        body moved to hook_pipeline.current_lane_id (S2 rip)."""
        from . import hook_pipeline as _hp

        return _hp.current_lane_id(self.runtime, project_root)

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
        """Thin delegate — body moved to hook_pipeline.record_hook_event (S2 rip)."""
        from . import hook_pipeline as _hp

        _hp.record_hook_event(
            self.runtime,
            project_root,
            event_name,
            payload,
            source="claude_hook",
        )

    def _resolve_cwd_root(self, payload: dict[str, object]) -> Path | None:
        """Thin delegate — body moved to hook_pipeline.resolve_cwd_root (S2 rip)."""
        from . import hook_pipeline as _hp

        return _hp.resolve_cwd_root(payload)

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

        S2 rip: body moved to hook_pipeline.resolve_project_root
        (identical resolution semantics incl. commission checks + log
        message); this delegate passes the hook's failure logger.
        """
        from . import hook_pipeline as _hp

        return _hp.resolve_project_root(
            self.runtime,
            payload,
            self._log_resolution_failure,
        )

    def _record_classification_event(
        self,
        project_root: Path,
        action_kind: str,
        prompt: str,
    ) -> None:
        """Record the classified action_kind as an execution event for traceability.

        Thin delegate — body moved to hook_pipeline.record_classification_event (S2 rip).
        """
        from . import hook_pipeline as _hp

        _hp.record_classification_event(
            self.runtime,
            project_root,
            action_kind,
            prompt,
            source="claude_hook",
        )

    def _log_resolution_failure(self, project_root: Path, reason: str) -> None:
        """Record a project-root resolution failure for debugging — at DEBUG, NOT
        WARNING (2026-07-10). This fires on EVERY hook whose cwd is a project
        SUBDIR (e.g. .../AIDOCS/mcp): resolution is deliberately no-walk-up, so a
        subdir "isn't commissioned" — an EXPECTED, benign condition, not a fault.
        Emitting it at WARNING wrote to the hook subprocess's stderr, which Claude
        Code surfaces as a "PostToolUse:Bash hook warning" on every such call (the
        recurring noise). DEBUG keeps it for troubleshooting without polluting the
        host-visible stderr stream."""
        logger.debug("Project resolution failed for %s: %s", project_root, reason)

    @staticmethod
    def _operator_intent_note(outcome) -> str:
        """Render a one-line operator-facing acknowledgment for an
        operator-intent outcome. Never echoes the prompt or any secret —
        only the structured action/target/scope and the decision.

        Thin delegate — body moved to hook_pipeline.operator_intent_note (S2 rip).
        """
        from . import hook_pipeline as _hp

        return _hp.operator_intent_note(outcome)

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
        # Bug #234-1: route.session_id is now resolved by THIS host session's
        # per-conductor binding — prefer it over the payload's session_state,
        # which can carry the global/last-active session.
        session_id = str(route.get("session_id") or session_state.get("session_id") or "").strip()
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
        """Thin host-adapter delegate to the shared, host-agnostic
        PromptContextBuilder (identity-spine extraction, 2026-07). claude_hook
        supplies only its host identity (host_kind + host_session_id)."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime).build_enforced_context(
            action_kind,
            session_id,
            route,
            prompt_payload,
            prompt,
            project_root,
            host_kind="claude_code",
            host_session_id=cli_session_id,
        )

    def _build_prompt_context(self, result: dict[str, object]) -> str:
        """Thin delegate — logic lives in the shared PromptContextBuilder (S2 rip)."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime).build_prompt_context(result)

    def _build_tool_discovery_hint(
        self,
        prompt: str,
        project_root: Path | None = None,
        action_kind: str | None = None,
        cli_session_id: str = "",
    ) -> list[str]:
        """Thin delegate — logic lives in the shared PromptContextBuilder."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime)._tool_discovery_hint(
            prompt,
            project_root=project_root,
            action_kind=action_kind,
            host_kind="claude_code",
            host_session_id=cli_session_id,
        )

    def _tools_used_in_session(self, project_root: Path) -> set[str]:
        """Thin delegate — logic lives in the shared PromptContextBuilder."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime)._tools_used_in_session(project_root)

    def _infer_skill_suggestions(
        self,
        prompt: str,
        project_root: Path,
        *,
        already_active: set[str],
    ) -> list[str]:
        """Thin delegate — logic lives in the shared PromptContextBuilder."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime)._infer_skill_suggestions(
            prompt, project_root, already_active=already_active
        )

    def _build_lifecycle_followthrough_nudge(self, lifecycle_state: dict[str, object]) -> str:
        """Thin delegate to LifecycleService.build_followthrough_nudge
        (host-agnostic pure function).
        """
        from .lifecycle_service import LifecycleService

        return LifecycleService.build_followthrough_nudge(lifecycle_state)

    # S2 rip: canonical tables live in prompt_context_service; class attrs
    # kept as back-compat aliases for tests — lazy (Article XXV).
    _TOOL_FIRST_PREAMBLE = _LazyCoreAlias(".prompt_context_service", "TOOL_FIRST_PREAMBLE")

    _ACTION_DIRECTIVES = _LazyCoreAlias(".prompt_context_service", "ACTION_DIRECTIVES")

    def _action_directive(self, action_kind: str) -> str:
        """Thin delegate — logic lives in the shared PromptContextBuilder (S2 rip)."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime)._action_directive(action_kind)

    def _build_compiled_workflow_summary(self, workflow: dict[str, object] | None) -> str:
        """Thin delegate — logic lives in the shared PromptContextBuilder (S2 rip)."""
        from .prompt_context_service import PromptContextBuilder

        return PromptContextBuilder(self.runtime)._build_compiled_workflow_summary(workflow)


# DERIVED CONSUMER #2 (#644) — NOT a second copy.
#
# This is the token set `_repair_pretooluse_matcher` UNIONS into an EXISTING
# install's matcher on every hook run, so installs that pre-date each update
# catch up without re-running `aidocs setup`. It used to be a hand-maintained
# tuple, and it drifted: it carried `Task` but not `Agent`, so the mechanism
# whose whole job is healing stale installs could not heal the 2026-07-09
# "agents cheat" bypass — and reported success while failing. It also lacked
# `Skill`. Both holes existed because the 2026-07-09 and 2026-07-30 fixes were
# applied to ONE of the three copies. The per-token history now lives with the
# declaration in claude_hooks_install.
#
# The declaring module is stdlib-only at import (json/re/time/pathlib), so this
# module-scope import does not violate the Article XXV adapter inversion pinned
# by tests/host/test_claude_hook_thin_entry.py — that pin forbids the AIDOCS
# CORE at module scope, and the installer is not core.
#
# NAMED EXCEPTION (deliberate): `_repair_posttooluse` below reuses THIS
# PreToolUse set for the PostToolUse group rather than
# `POSTTOOLUSE_MATCHER_TOKENS`. Self-repair only ever UNIONS tokens in, so
# unioning `TodoWrite` here would rewrite every already-converged install on
# its next hook run. The `aidocs setup` path (CANONICAL_HOOKS) is what carries
# the full PostToolUse set.
from .claude_hooks_install import PRETOOLUSE_MATCHER_TOKENS

_REQUIRED_PRETOOLUSE_MATCHER_TOKENS: tuple[str, ...] = PRETOOLUSE_MATCHER_TOKENS


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

        changed = _repair_backslash_commands(cfg, raw) or changed
        changed = _repair_windowless_python(cfg) or changed
        changed = _repair_hook_shim_command(cfg) or changed
        changed = _repair_pretooluse_matcher(cfg) or changed

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
        template_cmd = _find_aidocs_template_cmd(
            hooks_root,
            ("PreToolUse", "UserPromptSubmit", "SessionStart"),
        )

        changed = _repair_stop_events(hooks_root, template_cmd) or changed
        changed = _repair_posttooluse(hooks_root) or changed

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


def _repair_backslash_commands(cfg: dict, raw: str) -> bool:
    """Backslash repair on AIDOCS hook command paths (#413 extraction)."""
    changed = False
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
    return changed


def _repair_windowless_python(cfg: dict) -> bool:
    """Windowless migration (#333A): older installs pin `python.exe`;
    swap to the pythonw.exe SIBLING so hook spawns stop flashing a
    console window. windowless_python is existence-guarded — no
    sibling on disk → command untouched (a hook that fails to launch
    is a gate that silently fails open; cosmetics never outrank it).
    """
    from .claude_hooks_install import SHIM_BASENAME, windowless_python as _windowless

    changed = False
    for groups in (cfg.get("hooks") or {}).values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if "aidocs_mcp.claude_hook" not in cmd and SHIM_BASENAME not in cmd:
                    continue
                i = cmd.find(" -m aidocs_mcp")
                if i <= 0:
                    # #616 shim-form command: the interpreter is everything
                    # before the space that precedes the shim path.
                    k = cmd.find(SHIM_BASENAME)
                    i = cmd.rfind(" ", 0, k) if k > 0 else -1
                if i <= 0:
                    continue
                interp = cmd[:i].strip()
                swapped = _windowless(interp).replace("\\", "/")
                if swapped != interp:
                    h["command"] = swapped + cmd[i:]
                    changed = True
    return changed


def _repair_hook_shim_command(cfg: dict) -> bool:
    """#616 passive migration: `-m aidocs_mcp.claude_hook` commands move onto
    the fail-closed launcher shim OUTSIDE site-packages, so a mid-reinstall
    runtime REFUSES instead of crashing with a status the host treats as
    non-blocking (the measured ungoverned-window fail-open).

    Existence-guarded like the windowless repair: the command is rewritten
    only after ``ensure_hook_shim`` actually placed the current shim on disk —
    a hook command pointing at a missing file is a gate that silently fails
    open, which is the very defect this migration exists to close.
    """
    from .claude_hooks_install import ensure_hook_shim

    # RECONCILE THE DEPLOYED FILE FIRST, AND UNCONDITIONALLY (#973).
    #
    # This call used to live inside the loop below, reached only when a hook
    # command still carried the legacy ` -m aidocs_mcp.claude_hook` form. So it
    # healed the COMMAND and, as a side effect, the FILE — and the moment the
    # migration succeeded the scan matched nothing, the healer became
    # unreachable, and the deployed copy froze at whatever version placed it
    # while the package moved on underneath it. Measured: a shim from Aug 23
    # still executing after the build-221 swap, emitting the pre-#932 wording
    # ("clears itself in seconds", "run aidocs runtime --fix") that sends a
    # reader at the wrong remedy during an ordinary update window.
    #
    # The two axes are now independent, which is the actual fix: the legacy scan
    # decides whether SETTINGS need command migration; freshness of the deployed
    # file is reconciled every run regardless of what syntax settings.json uses.
    # `ensure_hook_shim` is content-comparing and idempotent, so on a current box
    # this is one stat + one read and no write.
    #
    # THIS IS THE MANDATORY HALF. The first upgrade carrying this fix is
    # performed by the OLD updater, so the package-swap reconciliation cannot
    # repair that same transition retroactively — the first successfully
    # delegated hook after the new package lands is what heals the box.
    placed = ensure_hook_shim()

    shim_fwd: str | None = None
    changed = False
    for groups in (cfg.get("hooks") or {}).values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                i = cmd.find(" -m aidocs_mcp.claude_hook")
                if i <= 0:
                    continue
                if shim_fwd is None:
                    # Reuse the reconciliation above rather than placing twice.
                    # The existence guard is UNCHANGED: a command is rewritten
                    # only once the shim is actually on disk, because a hook
                    # pointing at a missing file is a gate that silently fails
                    # open — the defect this migration exists to close.
                    if placed is None:
                        return changed  # could not place: keep the -m form
                    shim_fwd = str(placed).replace("\\", "/")
                h["command"] = f"{cmd[:i]} {shim_fwd}"
                changed = True
    return changed


def _repair_pretooluse_matcher(cfg: dict) -> bool:
    """Matcher drift repair on PreToolUse (#413 extraction)."""
    changed = False
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
    return changed


def _find_aidocs_template_cmd(hooks_root: dict, events: tuple[str, ...]) -> dict | None:
    """Pick a template AIDOCS hook command from the given events' groups."""
    for _ev_key in events:
        for group in hooks_root.get(_ev_key) or []:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                if isinstance(h, dict) and "aidocs_mcp" in str(h.get("command", "")):
                    return dict(h)
    return None


def _repair_stop_events(hooks_root: dict, template_cmd: dict | None) -> bool:
    """Stop / SubagentStop audit (2026-04-21). Captures turn
    boundaries so execution_events spans the full turn lifecycle.
    Unconditional register-if-missing — these events have no
    matcher (they fire on every stop).
    """
    changed = False
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
    return changed


def _repair_posttooluse(hooks_root: dict) -> bool:
    """Ensure the aidocs-owned PostToolUse group exists with the
    canonical matcher (#413 extraction — behavior preserved verbatim,
    including the PreToolUse-only template lookup for a missing group).
    """
    changed = False
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
        template_cmd = _find_aidocs_template_cmd(hooks_root, ("PreToolUse",))
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
    return changed


# Detail of the LAST integrity verdict this process computed, stashed so the
# refusal can say WHAT drifted without paying for a second check — and so a
# test that stubs `_hook_integrity_ok` (several do) gets the generic wording
# instead of a crash. Never authority: `_hook_integrity_ok` is.
_LAST_INTEGRITY_VERDICT: dict | None = None


def _hook_integrity_ok() -> bool:
    """Trusted-code boundary for the hook. Returns False on proven drift of a
    non-editable install so the hook REFUSES to run its (possibly tampered)
    enforcement logic — fail-closed without bricking the user's session.
    Editable/unverified installs and any internal check error return True (the
    hook still runs locally; remote trust is handled separately).

    The editable exemption is NOT implemented here and does not need to be:
    ``verify_package_integrity`` short-circuits ``PROV_EDITABLE`` to
    ``ok=True, drifted=False`` at the TOP, before it hashes or compares
    anything, so a dev checkout can never reach this False branch. (#589's
    original root cause claimed otherwise; it was retracted.)
    """
    global _LAST_INTEGRITY_VERDICT
    try:
        from . import package_integrity as _pi

        v = _pi.startup_integrity_gate(Path.home())
        _LAST_INTEGRITY_VERDICT = v if isinstance(v, dict) else None
        return bool(v.get("ok"))
    except Exception:
        _LAST_INTEGRITY_VERDICT = None
        return True


def _package_drift_allowed(env: dict | None = None) -> bool:
    """The documented escape hatch. Same vocabulary as
    ``mcp_server.enforce_package_integrity_or_refuse`` so one env var means one
    thing everywhere.
    """
    import os as _os

    src = _os.environ if env is None else env
    return str(src.get("AIDOCS_ALLOW_PACKAGE_DRIFT") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _record_package_command() -> str:
    """The remedy command that ACTUALLY CLEARS THIS (law 311bf3e6).

    ``--record-package`` records the provenance of the interpreter it RUNS
    UNDER. The hooks execute from the runtime venv (``official_wheel``); running
    the bare ``aidocs`` console script from a dev checkout records
    ``dev_editable``, reports success, and does NOT clear the refusal — an
    operator lost an hour to exactly that trap on 2026-07-28.

    The interpreter that must do the recording is therefore THIS one — the one
    running this very hook — so we name ``sys.executable`` verbatim instead of
    guessing a path. ``pythonw`` is swapped for its console sibling when present
    so the operator can actually see the output.
    """
    exe = Path(sys.executable)
    if exe.name.lower().startswith("pythonw"):
        sibling = exe.with_name(exe.name.replace("pythonw", "python", 1))
        try:
            if sibling.exists():
                exe = sibling
        except Exception:
            pass
    return f'"{exe}" -m aidocs_mcp.cli runtime --record-package'


def _package_drift_reason_text() -> str:
    """The operator/agent-facing refusal prose: WHAT drifted, WHY that means
    refuse, a remedy that works from where the reader stands, and the escape.
    """
    v = _LAST_INTEGRITY_VERDICT or {}
    prov = str(v.get("provenance") or "").strip() or "non-editable"
    what = "INTERPRETER drift" if v.get("interpreter_drift") else "PACKAGE drift"
    src = str(v.get("trust_source") or "").strip() or "the recorded package-trust row"
    detail = (
        str(v.get("reason") or "").strip()
        or "the installed aidocs_mcp bytes differ from the recorded trust row"
    )
    return (
        "AIDOCS GATE REFUSING — it cannot verify its own integrity.\n"
        f"WHAT DRIFTED: {what} on a `{prov}` install — the enforcement code this "
        f"hook would run no longer matches the trust recorded in {src}.\n"
        f"DETAIL: {detail}\n"
        "WHY REFUSE RATHER THAN PROCEED: a gate that cannot verify itself cannot "
        "be trusted to PERMIT, but can always be trusted to REFUSE. Until this "
        "clears, tool calls are DENIED — previously they were silently allowed "
        "with no verdict at all, which ran ~110 ungoverned calls in an hour "
        "(#589).\n"
        "REMEDY — run exactly this, under the RUNTIME interpreter (the one the "
        "hooks execute, which is the one printing this):\n"
        f"    {_record_package_command()}\n"
        "  Do NOT run `aidocs runtime --record-package` from a dev/editable "
        "checkout: --record-package records the provenance of the interpreter it "
        "RUNS UNDER, so the dev venv records `dev_editable`, reports success, and "
        "does NOT clear this.\n"
        "ESCAPE HATCH (documented, deliberate): set AIDOCS_ALLOW_PACKAGE_DRIFT=1 "
        "in the environment to skip the integrity check entirely and restore "
        "governance of everything else."
    )


# WHICH EVENTS REFUSE, AND WHY ONLY THESE.
#
# PreToolUse is the enforcement floor — it is the event whose verdict-less
# return WAS the outage, and the only event where a DENY actually prevents the
# ungoverned action. It hard-refuses.
#
# UserPromptSubmit / SessionStart / PostToolUse DEGRADE LOUDLY instead. #578's
# title is "hardening it would lock out contributors", and a refusal the
# operator cannot read is worse than the fail-open: denying SessionStart or
# UserPromptSubmit would make the session unusable BEFORE the remedy text could
# be acted on — the operator could not even ask how to fix it. PostToolUse is
# post-hoc; the tool already ran, so a deny there prevents nothing. All three
# therefore emit the FULL refusal text as additionalContext — model-visible and
# operator-visible, never a silent verdict-less return. No enforcement is lost
# by that choice, because PreToolUse still denies every tool call.
#
# Stop / SubagentStop / PostCompact have no verdict shape that would help here
# and a block on Stop can loop the host; they fall through to stderr + the
# decline breadcrumb, which is the loudest thing available on those events.
#
# SubagentStart is DELIBERATELY absent (2026-08-22, added with the routing arm).
# Two reasons, both from this file's own doctrine rather than a guess about the
# host: (1) the banner exists so a reader can ACT on the remedy, and the reader
# here is a just-spawned subagent whose very first tool call PreToolUse denies
# anyway — the enforcement floor is untouched, so nothing is bought; (2) the
# envelope's acceptance on this event has never been MEASURED, and
# hook_pipeline.on_post_compact records CC's schema REJECTING hookSpecificOutput
# on at least one event — an integrity refusal is the worst moment to discover
# we emitted a payload the host throws away. The omission is not silent: main()
# names the "NO verdict shape exists for SubagentStart" posture on stderr and in
# the gate_health decline breadcrumb. Revisit only WITH a measurement.
_DRIFT_BANNER_EVENTS = ("UserPromptSubmit", "SessionStart", "PostToolUse")


def _package_drift_response(payload: dict[str, object]) -> dict[str, object] | None:
    """Render the drift refusal in the shape Claude Code understands for the
    incoming event. PreToolUse gets the same envelope shape every other refusal
    in this adapter uses (see ``_deny_envelope``) — built inline rather than via
    the handler, because the handler is the code we just failed to trust.
    """
    event = str(payload.get("hook_event_name") or "").strip()
    text = _package_drift_reason_text()
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": text,
                "blocked_by": "package_drift_integrity",
            },
        }
    if event in _DRIFT_BANNER_EVENTS:
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": text,
            },
        }
    return None


def _report_hook_failure(exc: BaseException) -> None:
    """LOUD failure trail (#342 floor): if the hook dies, Claude Code proceeds
    with the tool call — the gate fails OPEN. A dying hook must therefore leave
    evidence: stderr for the host transcript AND an append-only breadcrumb next
    to the watchdog's health.json (visible without a console — the hook may run
    under pythonw). Best-effort by design; reporting must never mask the
    original failure.
    """
    import os as _os
    import time as _time

    # #932 — SAY WHICH IT ACTUALLY IS. This line claimed "gate fails OPEN"
    # unconditionally, which was true when it was written and is FALSE whenever
    # claude_hook_shim wrapped us: the shim catches BaseException and calls
    # refuse(crashed=True), delivering a DENY for this same call in this same
    # second. gate_health keys 'ungoverned' off that phrase, so a GOVERNED
    # denial was counted as an escape and the health banner told the operator
    # the gate might not be governing — four times (#803, #890).
    #
    # Conditional, NOT a reword: a legacy install whose settings.json still
    # launches `-m aidocs_mcp.claude_hook` directly has no shim to convert the
    # crash, and there the gate genuinely DOES fail open. That case must keep
    # saying so and keep counting as ungoverned (#770's floor).
    _wrapped = bool(_os.environ.get("AIDOCS_HOOK_SHIM"))
    _posture = (
        "the shim will DENY this call"
        if _wrapped
        else "gate fails OPEN for this event"
    )
    line = (
        f"{_time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())} "
        f"claude_hook FAILED ({_posture}): "
        f"{type(exc).__name__}: {exc}"
    )
    try:
        sys.stderr.write(f"[aidocs hook] {line}\n")
    except Exception:
        pass
    try:
        from .aidocs_service import daemon_dir  # stdlib-only module

        with (daemon_dir() / "hook_failures.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def read_hook_payload_text(stream=None) -> str:
    """The hook payload as text, decoded UTF-8 whatever the console codec is.

    #1017. Both hook entry points read `sys.stdin.read()`, which decodes with
    the PLATFORM default -- cp1252 on Windows. A payload containing an em dash,
    an accented name or an emoji therefore arrived MOJIBAKED, and every
    consumer downstream saw the corruption: measured 2026-09-04, the #1015 call
    claim was keyed on the mangled text while the daemon keyed on the intact
    text, so the two could never match and fail-closed refused every managed
    call carrying a non-ASCII argument. `stdio_shim` and `cli` already force
    utf-8 on their streams; this is the path that did not.

    Reading the underlying BUFFER sidesteps the text wrapper entirely, so no
    console codec can touch the bytes. `errors="replace"` keeps a genuinely
    undecodable payload loud-but-parseable rather than raising here, where the
    integrity gate has not yet had its say.
    """
    import sys as _sys

    stream = stream if stream is not None else _sys.stdin
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- fall back to the text stream below
            pass
    try:
        return stream.read()
    except Exception:  # noqa: BLE001 -- an unreadable stdin is an empty payload
        return ""


def main() -> None:
    # #589 fix 2 — REFUSE, DO NOT WITHHOLD. The payload is read FIRST because
    # the refusal must be shaped for the incoming hook event, and the integrity
    # check runs before a single byte of it is trusted or acted on. A malformed
    # payload stays as loud as it was (reported + re-raised) — but only after
    # the integrity verdict has had its say.
    raw = ""
    payload: dict[str, object] = {}
    _parse_error: BaseException | None = None
    try:
        raw = read_hook_payload_text().strip()
        _parsed = json.loads(raw) if raw else {}
        payload = _parsed if isinstance(_parsed, dict) else {}
    except Exception as exc:  # noqa: BLE001 - re-raised below, after the gate
        _parse_error = exc

    if not _hook_integrity_ok() and not _package_drift_allowed():
        response = _package_drift_response(payload)
        event = str(payload.get("hook_event_name") or "").strip() or "unknown"
        if response is None:
            posture = (
                f"NO verdict shape exists for {event} — the gate is OFF for "
                "this event"
            )
        elif event == "PreToolUse":
            posture = "this tool call is DENIED (not silently permitted)"
        else:
            posture = f"{event} degraded LOUDLY — refusal surfaced as context"
        sys.stderr.write(
            "AIDOCS hook declining to run its enforcement logic: installed "
            "package drifted from the verified runtime manifest — "
            f"{posture}.\n"
            f"{_package_drift_reason_text()}\n",
        )
        # GATE-LIVENESS: still a first-class breadcrumb. The wording now records
        # WHICH posture was taken, so hook_failures.log distinguishes a refusal
        # that actually blocked from an event that could only shout.
        try:
            from .gate_health import record_hook_decline

            record_hook_decline(
                f"package drift integrity refusal ({event}) — {posture}",
            )
        except Exception:
            pass
        if response is not None:
            json.dump(response, sys.stdout)
            sys.stdout.write("\n")
        return

    if _parse_error is not None:
        _report_hook_failure(_parse_error)
        raise _parse_error

    _self_repair_settings_json()

    # #876 phase 1 — THE ONE PLACE THE WINDOW IS MEASURED.
    #
    # THIS process is the one Claude Code spawned, so its ancestry reaches the
    # `claude.exe` that IS the window. Nothing downstream can repeat the
    # measurement: the very next step hands the payload to the resident broker,
    # which is hosted by the WATCHDOG (hook_broker.py:11) -- a process whose
    # ancestry is not this window's, and which (because `aidocs service start`
    # is routinely run from a Claude Code window's Bash) may descend from some
    # OTHER window entirely. A walk there would give a plausible WRONG answer.
    #
    # Stamped BEFORE the broker call so the local-evaluation fallback and the
    # brokered path carry identical payloads -- one measurement, both routes.
    #
    # It REPLACES any incoming value rather than deferring to it: a value this
    # process did not measure is not a measurement.
    #
    # ADDITIVE: SessionStart records `window -> conversation` from this field
    # and nothing reads that row to make a decision (#880 is where it becomes a
    # lease). Best-effort throughout -- an unstamped payload costs a diagnostic
    # row, a raising stamp would cost the operator their hook.
    try:
        from .window_key import stamp_payload_window

        stamp_payload_window(payload)
    except Exception:  # noqa: BLE001 -- never the reason a hook dies
        pass

    try:
        # `payload` was parsed above, BEFORE the integrity gate, so the drift
        # refusal could be shaped for the incoming event (#589 fix 2).
        # #332/#342/#877: ask the resident broker FIRST. The broker hosts the
        # canonical evaluator with the gate stack warm. A trusted answer is the
        # verdict. Broker failure has event-specific handling:
        #   * UserPromptSubmit -> fast labeled sqlite-only context (advisory
        #     surfacing; tool gates remain the enforcement floor).
        #   * every enforcement-bearing hook -> the full local evaluator.
        from .hook_broker_client import (
            build_degraded_user_prompt_response,
            evaluate_via_broker_with_reason,
        )

        # #504: take the REASON variant so the degraded banner can NAME why the
        # broker did not answer. The bare-None wrapper stays the security floor;
        # this call site simply carries the reason alongside it. Without this the
        # operator-visible banner reads "reason: unreported" on every prompt --
        # which is the whole defect #504 exists to end, one call away from real.
        # #489: stamp the moment this prompt reached AIDOCS and measure the
        # round trip, so the degraded banner can say WHEN and HOW LONG instead of
        # only WHY. Without it a stale banner and a fresh failure are
        # indistinguishable, and a 2s timeout looks identical to an instant
        # refusal. Two cheap clock reads on a path that already spawns a socket.
        import time as _hook_time  # local: main() has no module-level `time`

        from .hook_budget import now_ms as _now_ms

        from .hook_budget import budget_for_event as _budget_for_event

        # #489: UserPromptSubmit needs a wider budget than the enforcement
        # hooks. A real authenticated UPS evaluation measures 3.5-4.1s, so the
        # shared 2.0s deadline made EVERY prompt pay 2s and still degrade. The
        # enforcement events keep the tight budget — they fire per tool call and
        # fall back to the full local evaluator.
        _received_at_ms = _now_ms()
        _t0 = _hook_time.perf_counter()
        verdict, broker_reason = evaluate_via_broker_with_reason(
            payload,
            total_timeout=_budget_for_event(payload.get("hook_event_name")),
        )
        _elapsed_ms = (_hook_time.perf_counter() - _t0) * 1000.0
        if verdict is not None:
            # Trusted broker answer. {"response": None} is a REAL verdict
            # ("hook has no output; proceed") — not a failure.
            response = verdict["response"]
        else:
            event_name = str(payload.get("hook_event_name") or "").strip()
            prompt = str(payload.get("prompt") or "").strip()
            if (
                event_name == "UserPromptSubmit"
                and not prompt.startswith("/aidocs")
            ):
                response = build_degraded_user_prompt_response(
                    payload,
                    reason=broker_reason,
                    received_at_ms=_received_at_ms,
                    elapsed_ms=_elapsed_ms,
                )
            else:
                # Security floor: PreToolUse and every non-UPS event still
                # evaluate through the same local core when the broker is down.
                # /aidocs also stays local so bootstrap remains possible before
                # a daemon exists.
                from .managed_mode_service import (
                    begin_request_mode_memo,
                    reset_request_mode_memo,
                )

                memo_token = begin_request_mode_memo()
                try:
                    response = ClaudeHookHandler().handle(payload)
                finally:
                    reset_request_mode_memo(memo_token)
    except Exception as exc:
        _report_hook_failure(exc)
        raise
    # GATE-LIVENESS (additive, verdict-path-neutral): the hook REACHED a
    # verdict — record the pulse that proves hooks are firing. Written AFTER
    # evaluation and BEFORE output; a broken observer can never alter verdict.
    try:
        from .gate_health import record_hook_pulse

        record_hook_pulse(
            event=str(payload.get("hook_event_name") or ""),
            session_id=str(payload.get("session_id") or ""),
        )
    except Exception:
        pass
    if response is not None:
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
