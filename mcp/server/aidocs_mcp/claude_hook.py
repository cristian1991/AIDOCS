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

# Article XXI inversion (2026-07-13, king's order): this module is the Claude
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
    XXI forbids — so the alias resolves on access and never caches, keeping
    the core table the single source of truth (Article VI).
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


class _StewardshipUnavailable:
    """The stewardship verdict when ``failure_stewardship`` itself cannot be
    imported (r0b2 round 4). Defined HERE, with no dependency on that module,
    so an import/init fault can still produce a typed outcome. It is never
    clean: ``degraded`` is True, so DUTY 2 records the duty as debt instead
    of proving it, classified like every degraded outcome (durable /
    visible-but-not-durable / availability-risk, see DUTY 2). It never blocks
    on its own: a broken install must not wedge every turn."""

    ok = True
    block_reason = ""
    degraded = True
    new_failures: tuple[str, ...] = ()
    seal_blockers: tuple[str, ...] = ()
    lint_offenses: tuple[str, ...] = ()

    def __init__(self, error_type: str, *, recorded: bool) -> None:
        self.degraded_recorded = bool(recorded)
        self.degraded_error_type = str(error_type)
        self.degraded_reason = (
            f"stewardship UNVERIFIED: failure stewardship is UNAVAILABLE ("
            f"{error_type} importing failure_stewardship), so this turn was not "
            "checked for test failures at all. Recovery: repair the AIDOCS "
            "install and restart the MCP server / hook broker, and tell the "
            "operator; until then every turn seals DEGRADED, never clean."
        )


class ClaudeHookHandler:
    """Claude Code adapter — translates CC hook events to AgentOrchestrator calls."""

    def __init__(self) -> None:
        # Lazy core imports (Article XXI): constructing the handler IS the
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
        # EVERY EVENT IS SCOPED, INCLUDING ONE WITH NO SESSION ID (identity
        # recut, r0b1 f42e7b30-044). A hook event demonstrably ARRIVED, so a
        # missing session_id is evidence of an unidentified caller -- the
        # honest SCOPED EMPTY -- never permission to consult the process
        # stamp, which in the resident broker is whichever window it served
        # before. The earlier carve-out here (no session id => no scope, to
        # keep the legacy single-window fallback) is withdrawn on purpose:
        # passive non-AIDOCS pass-through is decided by the project-aware
        # gates (#234-2), not by leaving identity unscoped. Same law as
        # host_adapter_cli.main.
        _host_session_id = str(payload.get("session_id") or "").strip()

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
            # #1135 item 8: fail-open is NOT acceptable for a PreToolUse shell
            # or file-mutation call. "If AIDOCS is broken the operator keeps
            # working" is right for prompts, reads and audits; for a mutation
            # it means the one evaluation that could refuse a destructive call
            # silently permitted it. Those tools get a DENY verdict; every
            # other tool and event keeps the historical fail-open posture.
            # #1166 (r0b1 7728434a-73d blocker 2): EXCEPT an unservable
            # project -- a selection or HOME pin that exists but cannot be
            # served. That is not "AIDOCS is broken", it is "no project's
            # rules can be applied"; failing open there ran a native Read /
            # Glob / Grep / Task under no read rules at all.
            from .hook_pipeline import WindowHomeUnavailable

            if event_name == "PreToolUse" and isinstance(exc, WindowHomeUnavailable):
                return _unservable_project_verdict(payload, exc)
            return _mutation_exception_verdict(payload, exc)

    def _dispatch_event(
        self,
        event_name: str,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """Routed dispatch, extracted from handle() so the outer
        fail-open wrapper has a single function to try/except.
        """
        # #1166 blocker A (r0b1 7f12fa71-126 / 9d47e7aa-834 / 77914355-7d9): EVERY
        # prompt FIRST revokes the conversation's previous move grants and bumps
        # its prompt generation -- before the adoption repair, before the
        # /aidocs branch returns and before an unservable selection raises, so
        # no path leaves last turn's word spendable. A failed revocation leaves
        # the store's latch pending, and the SPEND side then refuses every move
        # (whoever asks: this turn, an old one, a direct MCP call) until a later
        # prompt's revocation commits; this prompt mints nothing.
        _prompt_generation: int | None = None
        if event_name == "UserPromptSubmit":
            try:
                from .binding_intent_store import begin_prompt_checked

                _prompt_generation, _revocation_safe = begin_prompt_checked(
                    str(payload.get("session_id") or "")
                )
            except Exception:  # noqa: BLE001 -- unevaluable: treat as unsafe
                _prompt_generation, _revocation_safe = None, False
            if not _revocation_safe:
                # r0b1 c33d21bb-4b8: NEITHER the revocation latch NOR the grant
                # DB could be written, so the previous prompt's move grant would
                # still be spendable once the store recovers. No new turn runs on
                # that old word: this prompt is blocked, and says why.
                return {"decision": "block", "reason": _REVOCATION_UNSAFE_REASON}
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
                from . import hook_pipeline as _hp_notice

                # #1166: audited and warned on the governing project, like
                # every prompt. An unservable selection still answers (the
                # bootstrap must never lock the agent in) but no project's
                # audit gets the prompt and no project's flags are shown.
                project_root = self._resolve_project_root(payload)
                rules_root = project_root
                if project_root is not None:
                    try:
                        rules_root = _hp_notice.selected_root_for_payload(payload) or project_root
                    except _hp_notice.WindowHomeUnavailable as exc:
                        self._record_degraded_hook_event(
                            project_root, event_name, payload, str(exc)
                        )
                        rules_root = None
                if rules_root is not None:
                    self._record_hook_event(rules_root, event_name=event_name, payload=payload)
                return _hp_notice.attach_stewardship_degraded_notice(
                    self._handle_aidocs_command(payload), payload, rules_root
                )

        if event_name == "SessionStart":
            from . import hook_pipeline as _hp

            # #1166 B7': a SessionStart in a window that already has a HOME
            # (/clear, /resume, compact -- possibly after a cd) is handled in
            # HOME; the window's FIRST start is attested in its cwd and pins it.
            project_root = _hp.home_root_for_payload(payload) or self._resolve_cwd_root(payload)
            if project_root is None:
                return None
            result = self._handle_session_start(project_root, payload)
            if (project_root / ".MEMORY").is_dir():
                self._record_hook_event(project_root, event_name=event_name, payload=payload)
            # After the handler: its adoption repair may have just commissioned
            # the project. Write-once -- a pinned window is never re-homed here.
            _hp.pin_window_home_at_session_start(project_root, payload)
            # #1166: admission rows whose Post never came are pruned on this
            # lifecycle rail, never on the per-call path. Never raises.
            from .tool_admission_store import prune_stale_admissions

            prune_stale_admissions()
            return result

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return None

        # #1166 (operator 2026-10-17): "when agent moves from project A to
        # project B, project B's rules should apply to it." A prompt or a tool
        # is governed -- and AUDITED (r0b1 60f49c3a-505 blocker D: never B's raw
        # prompt / tool_input in A's audit) -- by the project the conversation
        # moved to (its ai_project selection), else HOME. Bookkeeping the daemon
        # reads back where the hooks wrote it (the #1015 claim, XAACP liveness)
        # stays on HOME.
        #   Prompt / Pre: an unservable selection leaves a METADATA-ONLY
        #     degraded record on HOME and raises -- a prompt fails open with no
        #     pipeline run on any project; a tool is denied by _handle_impl.
        #   Post: recorded under the project that ADMITTED this invocation at
        #     Pre (tool_admission_store; r0b1 blockers 3 / B), never wherever
        #     the selection points by now. See _post_rules_root for the one
        #     fallback and the unprovable case.
        from . import hook_pipeline as _hp_sel

        _session_id = str(payload.get("session_id") or "").strip()
        _tool_use_id = str(payload.get("tool_use_id") or "").strip()
        rules_root = project_root
        if event_name in ("UserPromptSubmit", "PreToolUse"):
            try:
                rules_root = _hp_sel.selected_root_for_payload(payload) or project_root
            except _hp_sel.WindowHomeUnavailable as exc:
                self._record_degraded_hook_event(project_root, event_name, payload, str(exc))
                raise
        elif event_name in ("Stop", "SubagentStop", "PostCompact"):
            # #1166 (r0b1 60f49c3a-505 blocker C): Stop's project gates
            # (stewardship, update-intent, deploy, backlog, blast) and the
            # compaction epoch belong to the governing project; the receive
            # lease and consul mirror stay HOME (see _handle_stop). With the
            # selection unreadable no project's gates can run -- never A's on a
            # B turn. The turn still seals (operator 2026-10-18: the Stop hook
            # must let the agent stop) and says WHY, loudly: the debt is a
            # metadata trace on HOME plus a stderr line. Because the turn really
            # ends, the HOME identity duties still run.
            try:
                rules_root = _hp_sel.selected_root_for_payload(payload) or project_root
            except _hp_sel.WindowHomeUnavailable as exc:
                self._record_degraded_hook_event(project_root, event_name, payload, str(exc))
                if event_name == "PostCompact":
                    return None  # no project known: no epoch rotates
                _note_unservable_stop(event_name, exc)
                return self._stop_identity_duties(project_root, payload, event_name)
        elif event_name in ("PostToolUse", "PostToolUseFailure"):
            rules_root = self._post_rules_root(project_root, payload, _session_id, _tool_use_id)
            if rules_root is None:
                # Unprovable: release the native lease unconditionally (a leak
                # blocks every branch switch on that tree), record the outcome
                # under NO project, and say so.
                self._release_native_mutation_lease(payload)
                self._record_degraded_hook_event(
                    project_root, event_name, payload, "admission_unprovable"
                )
                if event_name == "PostToolUse":
                    return _unprovable_post_envelope(payload)
                return None

        self._record_hook_event(rules_root, event_name=event_name, payload=payload)

        if event_name == "UserPromptSubmit":
            # #1166 (operator 2026-10-18): moving the conversation to another
            # project / session changes the agent's scope, so it is the
            # operator's word -- "connect project B" -- not the model's. This
            # prompt's stated moves REPLACE the last turn's (none stated ->
            # none held), keyed to the conversation, before anything else runs.
            _mint_binding_intents(payload, rules_root, _prompt_generation)
            # #1166 (r0b1 7728434a-73d blocker 1): per-turn grants, freeze /
            # confirmation, intent and update-intent land in the governing
            # project -- or a grant would be minted into HOME A while B's
            # PreToolUse checks B.
            return self._handle_user_prompt_submit(rules_root, payload)
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
            # #1095: the project-bind KEEPALIVE rides this host lifecycle event
            # (fires for every tool call, reads included), not tool-root
            # resolution -- so a read-only tool never owns a lease refresh.
            self._keep_project_bind_alive(payload)
            # #1095 step 4a: the XAACP actor's liveness stamp rides the SAME
            # host lifecycle event -- never a messaging call's side effect.
            self._keep_xaacp_actor_alive(project_root, payload)
            with request_config_scope():
                _pre = self._handle_pre_tool_use(rules_root, payload)
            # #1166 (r0b1 60f49c3a-505 blocker B): only an ADMITTED call -- any
            # verdict but deny -- is recorded, AFTER its verdict. And it runs
            # only once its admission is DURABLE (operator 2026-10-18, r0b1
            # bbb4cb96-2ce blocker 1): a row that could not be written would
            # leave its Post unattributable, so the call is denied -- no body,
            # no native lease, no witness. The recovery calls stay open.
            _pre_hso = (_pre or {}).get("hookSpecificOutput") if isinstance(_pre, dict) else None
            _denied = isinstance(_pre_hso, dict) and _pre_hso.get("permissionDecision") == "deny"
            # A call with no tool_use_id or conversation id has nothing to
            # correlate its Post by, so it is denied the same way (r0b1
            # b4b5110c-526 Z1) -- Claude Code stamps both on every call.
            if not _denied:
                from .tool_admission_store import record_admission

                _correlated = bool(_tool_use_id and _session_id)
                _recorded = _correlated and record_admission(
                    _tool_use_id, rules_root, host_session_id=_session_id
                )
                if not _recorded and not _is_unservable_recovery_call(payload):
                    if not _correlated:
                        return _admission_uncorrelated_verdict()
                    return _admission_unrecorded_verdict(rules_root)
            # #1166 B7' (r0b1 b650227e-ee4): the claim, liveness and verdict
            # above are HOME's, but a native tool writes where the host's cwd
            # is -- so THAT tree's branch-transition lease is taken. Leasing
            # HOME while the host writes another tree would hide the write from
            # a switch there.
            _native_target = (
                _hp_sel.resolve_cwd_project_root(payload, lambda *_a: None) or rules_root
            )
            return self._register_native_mutation_lease(
                rules_root, payload, _pre, target_root=_native_target
            )
        if event_name == "PostToolUse":
            self._release_native_mutation_lease(payload)
            return self._handle_post_tool_use(rules_root, payload)
        if event_name == "PostToolUseFailure":
            return self._handle_post_tool_use_failure(rules_root, payload)
        if event_name == "PostCompact":
            return self._handle_post_compact(rules_root, payload)
        if event_name in ("Stop", "SubagentStop"):
            return self._handle_stop(rules_root, payload, event_name, home_root=project_root)
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

        COMPOSES, NEVER COPIES (Article VI). Both legs are the exact callables
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
        # #1121: this is a THIRD evaluator of a hook payload -- the broker's
        # deadline-drop rescue calls it directly, never through handle() -- so
        # it must stamp the payload's caller exactly as handle() does. Unstamped,
        # every session resolve below reads no_caller_identity and the audit row
        # this rescue exists to save is silently not written (VPS gate dev-1177,
        # test_a_deadline_drop_still_persists_the_prompt). try/finally for the
        # same reason as handle(): the broker is long-lived and serves many
        # windows, so the stamp must not outlive this call.
        from .mcp_server_runtime_helpers import (
            reset_request_host_identity,
            set_request_host_identity,
        )

        _identity_token = set_request_host_identity(
            str(payload.get("session_id") or "").strip(),
            host_kind="claude_code",
            agent_id=str(payload.get("agent_id") or "").strip(),
        )
        try:
            return self._capture_durable_only_impl(payload, prompt)
        finally:
            reset_request_host_identity(_identity_token)

    def _capture_durable_only_impl(self, payload: dict[str, object], prompt: str) -> bool:
        from .intent_tokens_store import request_cache

        project_root = self._resolve_project_root(payload)
        if project_root is None:
            return False
        # #1166: recorded where the prompt is governed -- the conversation's
        # selection, else HOME. A selection that cannot be served records
        # nothing rather than writing B's prompt into HOME A.
        from . import hook_pipeline as _hp_sel

        try:
            project_root = _hp_sel.selected_root_for_payload(payload) or project_root
        except _hp_sel.WindowHomeUnavailable as exc:
            # Not silent (r0b1 60f49c3a-505 blocker D): a metadata-only trace
            # that a prompt was dropped with no project to record it in.
            self._record_degraded_hook_event(
                project_root, "UserPromptSubmit", payload, f"durable_capture_skipped: {exc}"
            )
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
                # Origin gate: only an operator-origin prompt may arm the
                # update-intent rail (same verdict the full UPS uses).
                _eligible, _principal = _hp._ups_origin_gate(
                    payload, project_root, None, lambda _tag: None
                )
                if session_id and _eligible:
                    from .update_intent_hook import process_user_prompt

                    process_user_prompt(project_root, session_id, prompt)
                    try:
                        from .aidocs_nlp.durable_hint_store import observe_content

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

        # The one UPS exit: a pending stewardship_degraded_unrecorded flag's
        # fixed warning rides EVERY return shape (round 7), early returns too.
        return _hp.attach_stewardship_degraded_notice(
            _hp.run_user_prompt(
                self.runtime, project_root, payload, host_kind="claude_code",
            ),
            payload,
            project_root,
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
    # Lazy aliases (Article XXI): resolved on access, never a module-scope
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
        *,
        home_root: Path | None = None,
    ) -> dict[str, object] | None:
        """Stop / SubagentStop — audit (LifecycleService, host-agnostic)
        plus the Failure Stewardship turn gate.

        #1166 (r0b1 60f49c3a-505 blocker C): ``project_root`` is the GOVERNING
        project (the conversation's selection, else HOME) -- every project
        duty below runs there. ``home_root`` is the window's HOME, where the
        identity duties live: the XAACP receive-lease arm and the seated-
        Consul mirror. Defaults to ``project_root`` (no selection: one project).

        The audit half never blocks. The stewardship half DOES: it
        registers every pytest failure observed this turn into the
        project's PERSISTENT ledger and blocks the turn from sealing
        when the agent's final report carries an unproven excuse phrase
        ("pre-existing", "not my bug", "flaky", …) or leaves a failure
        it owns untriaged. This is what makes "find out why before you
        say 'not my fault'" a deterministic code rule, not a prompt.
        """
        from .lifecycle_service import LifecycleService

        # ── STOP IS ONE FAULT DOMAIN FOR NINE DUTIES (r0b ruling) ────────
        # Multi-duty lifecycle hooks must be FAULT-CONTAINED AND DEBT-AWARE.
        # `_dispatch` is the execution-marker ledger: its row is written
        # BEFORE the first duty, each duty marks itself proven as it
        # completes, and the terminal `close` computes planned-minus-proven.
        # If this process dies mid-dispatch the row stays `in_flight` and
        # NAMES the duties that became unproven — the fact that did not
        # exist when the delegated hook died seven times in one day (exit 1,
        # 0 bytes of stdout, cause=hook_crashed_before_verdict, once on
        # event=Stop) and the reason the only honest post-mortem was "Stop
        # result absent; eight duties have no recovery evidence".
        #
        # IT CANNOT CHANGE THIS HANDLER'S BEHAVIOUR, and that is a hard
        # constraint rather than an aspiration. `open_dispatch` returns None
        # instead of raising, every marker swallows its own failure, and the
        # three duties whose exception propagation is load-bearing
        # RE-RAISE after recording the debt. Containing duty 1 instead would
        # let duty 2 (the stewardship gate) block a turn that today seals
        # because the crash reached the shim first — a change to whether
        # Stop can block, which is REPORTED, never shipped.
        from . import stop_duty_ledger as _sdl

        # THE GUARD THE MUTANT FOUND. `open_dispatch` is written to return
        # None rather than raise, but "written to" is not "cannot": an import
        # failure, a monkeypatched module or a future edit can still throw
        # here, and at THIS line nothing has been proven yet, so an escape
        # takes all nine duties down with it. The ledger is never allowed to
        # be the cause of the crash it exists to explain.
        try:
            _dispatch = _sdl.open_dispatch(project_root, event_name=event_name, payload=payload)
        except Exception:
            _dispatch = None

        def _proven(duty: str) -> None:
            if _dispatch is not None:
                _dispatch.mark_proven(duty)

        def _owed(duty: str, exc: BaseException) -> None:
            if _dispatch is not None:
                _dispatch.mark_failed(duty, exc)

        def _close(outcome: str, note: str = "") -> None:
            if _dispatch is not None:
                _dispatch.close(outcome, note=note)

        # #1075 B1: before ANY duty can block, a fresh (non-re-entry) Stop clears
        # the blast block marker, so duty 5b suppresses only the re-entry caused
        # by its own block. Fail-quiet: a stale marker costs one deferred
        # presentation, never a blocked or looping Stop.
        try:
            from .blast_envelope import note_stop_entry as _blast_note_stop

            _blast_agent = str(payload.get("agent_id") or "").strip() or None
            if event_name == "Stop" or (event_name == "SubagentStop" and _blast_agent):
                _blast_note_stop(
                    project_root,
                    stop_hook_active=bool(payload.get("stop_hook_active")),
                    agent_id=_blast_agent,
                )
        except Exception:
            pass

        # ── DUTY 1 — turn-end audit. Duties 6, 7, 8 and 9b (managed-session
        # authority, the assistant_turn_end event + gated message_text,
        # _maybe_stop_capture #316, actor-owned stop_diagnostic
        # #1046) are NESTED INSIDE this one call, which is why this boundary
        # can prove six duties and names the other four rather than
        # pretending six is the whole set.
        try:
            LifecycleService(self.runtime).on_assistant_turn_end(
                event_name=event_name,
                project_root=project_root,
                payload=payload,
            )
        except BaseException as _exc:
            _owed(_sdl.DUTY_TURN_END_AUDIT, _exc)
            _close(_sdl.OUTCOME_RAISED, note="duty 1 raised; duties 2-9 never reached")
            raise
        _proven(_sdl.DUTY_TURN_END_AUDIT)

        # ── DUTY 2 — failure stewardship. BLOCKS.
        _blocked: dict[str, object] | None = None
        try:
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
                    _blocked = {"decision": "block", "reason": gate.block_reason}
                else:
                    self._note_stewardship_deferred(project_root, payload, frozen, gate)
        except BaseException as _exc:
            _owed(_sdl.DUTY_FAILURE_STEWARDSHIP, _exc)
            _close(_sdl.OUTCOME_RAISED, note="duty 2 raised; duties 3-9 never reached")
            raise
        if gate is not None and getattr(gate, "degraded", False):
            # DEGRADED, not clean (r0b2 rounds 3-4): the duty is DEBT, never
            # proven, and the outcome says which of three cases this is:
            #   durable  -- the debt reached the Stop dispatch ledger or the
            #               audit store (confirmed writes only); the turn seals.
            #   visible-but-not-durable -- neither store confirmed and this is
            #               not a host re-entry: the turn is blocked with the
            #               reason, which goes in front of the agent and into
            #               the host transcript; the agent must tell the
            #               operator.
            #   availability exception -- neither store confirmed and the host
            #               is re-entering Stop (stop_hook_active). OPERATOR-
            #               ACCEPTED (r0b2 round 6, option a): the turn seals
            #               (no wedge) but is never verified or safe. The flag
            #               only says SOME Stop hook blocked earlier, so nothing
            #               claims this warning was shown; instead the warm
            #               broker holds a stewardship_degraded_unrecorded flag
            #               (stewardship_health) that the next UserPromptSubmit
            #               for the session shows, and that flushes to the audit
            #               store when a writer works again.
            _reason = str(getattr(gate, "degraded_reason", "") or "stewardship UNVERIFIED")
            _in_dispatch = bool(
                _dispatch is not None
                and _dispatch.mark_failed(_sdl.DUTY_FAILURE_STEWARDSHIP, _reason) is True
            )
            _in_audit = bool(getattr(gate, "degraded_recorded", False))
            if _in_dispatch or _in_audit:
                _degraded_note = (
                    "stewardship DEGRADED (unverified); debt record: durable "
                    f"(dispatch={_in_dispatch}, audit={_in_audit})"
                )
                self._flush_stewardship_flags(project_root)
            elif not bool(payload.get("stop_hook_active")):
                # BLOCKED, not sealed: no availability-exception flag is raised
                # here (round 8) -- this turn's own block reason is the
                # host-visible record, and counting it as a seal would be false.
                _degraded_note = (
                    "stewardship DEGRADED (unverified); debt record: visible-but-not-durable"
                )
                _blocked = {
                    "decision": "block",
                    "reason": (
                        f"{_reason}\nDebt record: visible-but-not-durable -- neither "
                        "the Stop dispatch ledger nor the audit store accepted it, so "
                        "it is not durably recorded anywhere. Tell the operator now "
                        "that failure stewardship is degraded and unrecorded. Ending "
                        "the turn again seals it as an operator-accepted availability "
                        "exception (never verified)."
                    ),
                }
            else:
                # THE availability-exception seal -- the only branch that
                # raises the flag, for this Stop's own project + session.
                _flag = self._raise_stewardship_flag(project_root, payload, gate)
                _degraded_note = (
                    "stewardship DEGRADED (unverified); availability exception, "
                    f"visibility NOT guaranteed; health flag: {_flag}"
                )
                _flag_says = (
                    "flag recorded in the warm hook broker; the next prompt of "
                    "this session shows it"
                    if _flag in ("recorded", "deduped", "rate_limited")
                    else "flag NOT recorded (no long-lived process held it: "
                    f"{_flag}); this notice may be the only trace"
                )
                try:
                    print(
                        f"[aidocs] {_reason}. Sealed as an operator-accepted "
                        "availability exception -- NOT verified, NOT safe, authorizes "
                        "nothing; no debt store accepted the record; visibility NOT "
                        f"guaranteed; {_flag_says}. Tell the operator.",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
        else:
            _degraded_note = ""
            _proven(_sdl.DUTY_FAILURE_STEWARDSHIP)
            self._flush_stewardship_flags(project_root)
        if _blocked is not None:
            _close(
                _sdl.OUTCOME_EARLY_RETURN,
                note=_degraded_note or "stewardship gate blocked the seal",
            )
            return _blocked

        # ── DUTY 3 — #219 PR-2: update-intent durability gate — hold the
        # turn-seal when an operator update was detected this session but is
        # not yet recorded durably (nlp.update_gate=block). Mirrors the
        # stewardship block envelope; advise/off and ambiguous detections
        # never block.
        from .update_intent_hook import gate_stop as _ui_gate_stop

        _sid = str(payload.get("session_id") or "").strip()
        try:
            _ui_block: dict[str, object] | None = None
            if _sid:
                _ug = _ui_gate_stop(project_root, _sid)
                if _ug.get("block"):
                    _ui_block = {"decision": "block", "reason": str(_ug["reason"])}
        except BaseException as _exc:
            _owed(_sdl.DUTY_UPDATE_INTENT, _exc)
            _close(_sdl.OUTCOME_RAISED, note="duty 3 raised; duties 4-9 never reached")
            raise
        _proven(_sdl.DUTY_UPDATE_INTENT)
        if _ui_block is not None:
            _close(_sdl.OUTCOME_EARLY_RETURN, note="update-intent gate blocked the seal")
            return _ui_block

        # ── DUTY 4 — Deploy-wait = conducting prep (2026-07-12): a CONDUCTOR
        # stopping while a crown deploy is still pre-SAFE-TO-EDIT gets ordered
        # ONCE per deploy to scout the next war read-only instead of idling.
        # Conductor-only (SubagentStop excluded inside the gate too);
        # fail-open by contract.
        #
        # ALREADY fail-quiet, so the swallow is pre-existing behaviour and is
        # kept byte-for-byte — what changes is that the swallow now leaves
        # DEBT instead of silence. This is the fault containment r0b asked
        # for, shipped exactly where it costs no semantics: duty 5 still runs
        # when duty 4 throws, and duty 4's failure is durably owed.
        try:
            from .deploy_edit_window import conduct_the_wait_stop_gate

            _nudge = conduct_the_wait_stop_gate(
                project_root,
                event_name=event_name,
                host_session_id=_sid,
            )
            if _nudge is not None:
                _proven(_sdl.DUTY_DEPLOY_WAIT_NUDGE)
                _close(
                    _sdl.OUTCOME_EARLY_RETURN,
                    note="deploy-wait conducting-prep nudge early-returned",
                )
                return _nudge
        except Exception as _exc:
            _owed(_sdl.DUTY_DEPLOY_WAIT_NUDGE, _exc)
        else:
            _proven(_sdl.DUTY_DEPLOY_WAIT_NUDGE)

        # ── DUTY 5 — #419 War DD: never seal a turn silently while open
        # backlog work is unmentioned. Blocks the clean stop ONCE per
        # (epoch, backlog-state) with an explicit tell-the-user instruction,
        # then yields — dedupe is strict (no session id / broken ledger → no
        # block, never a loop). SubagentStop is excluded inside the surfacer
        # (worker fencing §II).
        try:
            from .backlog_surfacer import stop_backlog_reminder

            _bl = stop_backlog_reminder(
                project_root,
                event_name=event_name,
                host_session_id=_sid,
            )
            if _bl is not None:
                _proven(_sdl.DUTY_BACKLOG_SURFACER)
                _close(_sdl.OUTCOME_EARLY_RETURN, note="backlog surfacer early-returned")
                return _bl
        except Exception as _exc:
            _owed(_sdl.DUTY_BACKLOG_SURFACER, _exc)
        else:
            _proven(_sdl.DUTY_BACKLOG_SURFACER)

        # ── DUTY 5b — #1075 blast completion. LOWEST-PRIORITY blocker: every
        # duty above that blocks has already returned, so this one is deferred
        # behind them (the surfaces stay owed and unpresented). Presents only
        # NEWLY owed surfaces, once; never when the host re-entered Stop
        # because of a Stop block (stop_hook_active) — no loop by construction.
        try:
            from .blast_envelope import stop_presentation as _blast_stop

            _blast_block = _blast_stop(
                project_root,
                event_name=event_name,
                agent_id=str(payload.get("agent_id") or "").strip() or None,
                stop_hook_active=bool(payload.get("stop_hook_active")),
                feedback_host="claude_code",  # this handler IS the Claude Code Stop hook
            )
            if _blast_block is not None:
                _proven(_sdl.DUTY_BLAST_COMPLETION)
                _close(_sdl.OUTCOME_EARLY_RETURN, note="blast completion presented")
                return _blast_block
        except Exception as _exc:
            _owed(_sdl.DUTY_BLAST_COMPLETION, _exc)
        else:
            _proven(_sdl.DUTY_BLAST_COMPLETION)

        # ── DRT-13: the receive lease ARMS LAST ──────────────────────────
        # Every function above has run to completion and none of their
        # early returns reached here, so this turn is genuinely ending.
        # That is the only moment at which arming is correct: a BLOCKED
        # turn is not an ending turn and must arm nothing.
        #
        # This records that the actor now owes a park; it does NOT park.
        # A 300s wait inside a ~30s hook budget would starve every gate
        # above it, so the park itself belongs in-model, one tool call
        # later. See receive_lease_stop for the full reasoning and for
        # the enforcement block that is deliberately NOT shipped.
        #
        # Fail-quiet and fail-AFTER: the return value cannot influence
        # whether the turn seals, and a broken lease ledger cannot fail a
        # Stop that otherwise succeeded.
        #
        # THE PARK REQUEST IS NOT EMITTED HERE, and that is deliberate. Stop
        # is a stderr-only event for this host (claude_hook_shim's posture
        # table): it carries no additionalContext shape, so the only way to
        # put text in front of the actor from here would be to change what
        # Stop RETURNS — a change to this hook's output contract, which is
        # reported and not shipped. The obligation is made durable by the
        # arm above and is rendered by `receive_lease_stop.park_request_block`
        # on the notification rail, which is the operator-controlled host's
        # actual receive rail. That builder currently has NO caller; see its
        # docstring for the exact site it must be wired into.
        try:
            from . import receive_lease_stop as _rls

            # #1166: an identity duty -- HOME, not the governing project.
            _lease_out = _rls.arm_on_stop(
                home_root or project_root, event_name=event_name, payload=payload
            )
        except Exception as _exc:
            _lease_out = None
            _owed(_sdl.DUTY_RECEIVE_LEASE_ARM, _exc)
        else:
            _proven(_sdl.DUTY_RECEIVE_LEASE_ARM)
        # A finished subagent's duty is discharged by RELEASE (design decision
        # 2026-09-13); the ledger records that, never a silent success.
        _released = isinstance(_lease_out, dict) and _lease_out.get("action") == "released"

        # ── #1061 — SEATED CONSUL: mirror the turn to the peer Consul and
        # REFUSE THE SEAL until the actor has parked in wait_next (design
        # decision 2026-09-14; r0b's pinned state machine lives in
        # receive_lease_stop.consul_stop_duty). Runs AFTER every existing
        # duty, so any pre-existing blocker has already returned: a blocked
        # turn is not a turn end and is never mirrored. Non-seated actors,
        # subagents and workers get None and seal exactly as before.
        #
        # Fault containment: a raise is recorded as durable debt and the turn
        # seals (fail-open on the ledger, like every other post-gate duty) —
        # the failure is observable in stop_duty_dispatch, never silent.
        _consul_block: dict[str, object] | None = None
        try:
            from . import receive_lease_stop as _rls_consul

            # #1166: an identity duty -- the seat and its peer live on HOME.
            _identity_root = home_root or project_root
            _consul_block = _rls_consul.consul_stop_duty(
                _identity_root,
                event_name=event_name,
                payload=payload,
                # Lazy: the freeze lookup runs only for a seated Consul.
                frozen=lambda: self._active_freeze_for_stop(_identity_root, payload) is not None,
            )
        except Exception as _exc:
            _consul_block = None
            _owed(_sdl.DUTY_CONSUL_PARK, _exc)
        else:
            _proven(_sdl.DUTY_CONSUL_PARK)
        if _consul_block is not None:
            _close(_sdl.OUTCOME_EARLY_RETURN, note="consul park required before seal")
            return _consul_block
        _close(
            _sdl.OUTCOME_SEALED,
            note="; ".join(
                n for n in (_degraded_note, "receive_lease_released" if _released else "") if n
            ),
        )
        return None

    def _stop_identity_duties(
        self,
        home_root: Path,
        payload: dict[str, object],
        event_name: str,
    ) -> dict[str, object] | None:
        """#1166: ONLY the window-identity Stop duties, on HOME -- for a turn
        that ends while no project's rules could be served (the repeat Stop
        after _unservable_stop_verdict blocked once). The XAACP receive-lease
        arm keeps the actor reachable; the seated-Consul mirror keeps its peer
        in step. No project duty runs and none is claimed proven. Each is
        fault-contained, exactly as in _handle_stop. Returns the Consul's
        block, if any."""
        try:
            from . import receive_lease_stop as _rls

            _rls.arm_on_stop(home_root, event_name=event_name, payload=payload)
        except Exception:  # noqa: BLE001 -- as in _handle_stop: never fails a Stop
            pass
        try:
            from . import receive_lease_stop as _rls_consul

            return _rls_consul.consul_stop_duty(
                home_root,
                event_name=event_name,
                payload=payload,
                frozen=lambda: self._active_freeze_for_stop(home_root, payload) is not None,
            )
        except Exception:  # noqa: BLE001
            return None

    def _failure_stewardship_gate(
        self,
        project_root: Path,
        payload: dict[str, object],
    ):
        """Run the failure-stewardship turn gate. Returns a StopGateResult or
        None (seal).

        COVERAGE DECIDES (r0b2). A scan that cannot prove it read the window
        -- the byte ceiling cut it short ("partial") or the given transcript
        could not be read ("unknown") -- cannot vouch for a clean turn: the
        gate FAILS CLOSED and names the recovery door
        (``fs.scan_coverage_remedy``: repair, or escalate to the operator).
        No retry count, elapsed time or stop_hook_active re-entry lifts it,
        and there is no self-service receipt shortcut (round 3: a receipt
        could only re-prove the bytes it re-hashed). The lines that were read
        still go to the ledger, so the newest failure is registered either
        way. No path at all ("absent") claims no scan and leaves the verdict
        as it was.

        AN EVALUATOR FAULT IS NEVER SILENT. With incomplete coverage it still
        blocks (the fault cannot cancel the coverage block). With complete or
        absent coverage the turn may seal, but only as an explicit DEGRADED
        result (``degraded=True``), which DUTY 2 records as durable debt in
        the Stop dispatch ledger instead of a proven duty -- a broken ledger
        must not wedge every normal turn. Each fault is also recorded as
        ``stewardship_gate_error`` (exception type only, never its text).
        """
        try:
            from . import failure_stewardship as fs
        except Exception as exc:
            # Never a silent seal: a typed, import-independent DEGRADED outcome.
            recorded = self._note_stewardship_error(project_root, payload, "", "unscanned", exc)
            return _StewardshipUnavailable(type(exc).__name__, recorded=recorded)
        transcript_path = str(payload.get("transcript_path") or "")
        try:
            scan = fs.scan_transcript(transcript_path)
        except Exception:  # never raises by contract; a regression fails closed
            scan = fs.TranscriptScan("", fs.SCAN_COVERAGE_UNKNOWN)
        incomplete = scan.coverage in fs.SCAN_COVERAGE_INCOMPLETE
        session_id = ""
        try:
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
            gate = fs.evaluate_turn(
                project_root=project_root,
                session_id=session_id,
                scan_text=scan.text,
                report_text=str(payload.get("message") or ""),
            )
        except Exception as exc:
            recorded = self._note_stewardship_error(
                project_root, payload, session_id, scan.coverage, exc
            )
            if incomplete:
                return self._fail_closed_on_coverage(
                    None, scan.coverage, transcript_path, error_type=type(exc).__name__
                )
            # complete/absent: an explicit DEGRADED seal, never a clean one.
            return fs.StopGateResult(
                ok=True,
                block_reason="",
                degraded=True,
                degraded_reason=(
                    f"stewardship UNVERIFIED: the ledger evaluation raised "
                    f"{type(exc).__name__} (scan coverage {scan.coverage}); the turn "
                    "sealed in a DEGRADED posture and the stewardship duty is "
                    "recorded as outstanding debt"
                ),
                degraded_recorded=recorded,
                degraded_error_type=type(exc).__name__,
            )
        if incomplete:
            self._note_scan_incomplete(project_root, payload, session_id, scan.coverage)
            return self._fail_closed_on_coverage(gate, scan.coverage, transcript_path)
        return gate

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
    def _fail_closed_on_coverage(gate, coverage: str, transcript_path: str, *, error_type: str = ""):
        """The stewardship verdict for a turn whose transcript scan could not
        prove its coverage (``fs.SCAN_COVERAGE_INCOMPLETE``): never a clean
        seal, always with its recovery door named. A ledger block stays that
        block (its reason first, its signatures kept) with the coverage
        appended; a clean -- or absent, when the evaluator raised -- verdict
        becomes a block naming the coverage. A frozen session still defers it
        (DUTY 2's freeze rule), like every stewardship block."""
        from . import failure_stewardship as fs

        if coverage == fs.SCAN_COVERAGE_PARTIAL:
            why = (
                f"stewardship scan coverage {coverage}: the last "
                f"{fs._SCAN_TAIL_LINES} transcript lines exceed the "
                f"{fs._SCAN_TAIL_MAX_BYTES}-byte scan ceiling, so older lines of "
                "that window were not scanned and a failure there cannot be ruled out"
            )
        else:
            why = (
                f"stewardship scan coverage {coverage}: the turn's transcript "
                "could not be read, so no failure in it can be ruled out"
            )
        why += "; the turn cannot seal as clean on an incomplete scan."
        if error_type:
            why += (
                f" The ledger evaluation also failed ({error_type}); it is "
                "recorded as stewardship_gate_error and does not lift this block."
            )
        why += "\n" + fs.scan_coverage_remedy(coverage, transcript_path)
        if gate is None:
            return fs.StopGateResult(ok=False, block_reason=why)
        if not gate.ok:
            gate.block_reason = f"{gate.block_reason}\n{why}" if gate.block_reason else why
            return gate
        return fs.StopGateResult(
            ok=False,
            block_reason=why,
            new_failures=list(gate.new_failures),
            seal_blockers=list(gate.seal_blockers),
            lint_offenses=list(gate.lint_offenses),
        )

    def _note_stewardship_error(
        self,
        project_root: Path,
        payload: dict[str, object],
        duty_id: str,
        coverage: str,
        exc: BaseException,
    ) -> bool:
        """Record that the stewardship evaluation raised. The exception TYPE
        only -- its text can carry paths, test output or ledger content, so
        it is never copied. Never raises. True only when the store returned
        an event id (confirmed); False on any failure or a None answer -- an
        unconfirmed write is never counted as a durable record."""
        try:
            event_id = self.runtime.hub.execution.record_event(
                project_root,
                event_kind="stewardship_gate_error",
                source_kind="stop",
                session_id=str(payload.get("session_id") or "").strip(),
                payload={
                    "note": "stewardship gate evaluation raised",
                    "error_type": type(exc).__name__,
                    "coverage": coverage,
                    "duty_id": duty_id,
                },
            )
        except Exception:
            return False
        return bool(event_id)

    @staticmethod
    def _raise_stewardship_flag(project_root: Path, payload: dict[str, object], gate) -> str:
        """Hold the stewardship_degraded_unrecorded flag for THIS Stop's
        project + host session in the warm broker's memory
        (stewardship_health). Outside a broker evaluation there is no
        long-lived holder and the answer says so. Never raises."""
        try:
            from . import stewardship_health as _sh
            from .hook_write_behind import in_broker_evaluation

            return _sh.note_degraded_unrecorded(
                project_root,
                str(payload.get("session_id") or "").strip(),
                str(getattr(gate, "degraded_error_type", "") or ""),
                in_long_lived=in_broker_evaluation(),
            )
        except Exception:
            return "not_recorded_flag_unavailable"

    def _flush_stewardship_flags(self, project_root: Path) -> int:
        """A writer of THIS project works again: flush this project's pending
        flags (only) as durable audit events into this project's own store,
        and clear the confirmed ones. Another project's flags are never
        written here (round 8: no cross-project custody). Never raises."""
        try:
            from . import stewardship_health as _sh

            def _write(entry: dict[str, object]) -> object:
                return self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind=_sh.FLAG_NAME,
                    source_kind="stop",
                    session_id=str(entry.get("session") or ""),
                    payload={
                        "note": (
                            "turn(s) sealed as an operator-accepted availability "
                            "exception: failure stewardship NOT verified, no debt "
                            "store accepted the record at the time"
                        ),
                        **entry,
                    },
                )

            return _sh.flush_pending(project_root, _write)
        except Exception:
            return 0

    def _note_scan_incomplete(
        self,
        project_root: Path,
        payload: dict[str, object],
        duty_id: str,
        coverage: str,
    ) -> None:
        """Record that the stewardship scan could not prove its coverage:
        ``partial`` (the 400-line window was cut short by the byte ceiling --
        the newest lines were scanned, older ones were not) or ``unknown``
        (the given transcript could not be read). Best-effort audit only --
        never raises; the fail-closed verdict does not depend on it."""
        try:
            from . import failure_stewardship as fs

            partial = coverage == fs.SCAN_COVERAGE_PARTIAL
            note = (
                "stewardship scan coverage partial: the last "
                f"{fs._SCAN_TAIL_LINES} transcript lines exceed "
                f"{fs._SCAN_TAIL_MAX_BYTES} bytes; only the complete "
                "lines inside that ceiling were scanned"
                if partial
                else "stewardship scan coverage unknown: the transcript could not be read"
            )
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="stewardship_scan_truncated" if partial else "stewardship_scan_unknown",
                source_kind="stop",
                session_id=str(payload.get("session_id") or "").strip(),
                payload={
                    "note": note,
                    "coverage": coverage,
                    "duty_id": duty_id,
                    "window_lines": fs._SCAN_TAIL_LINES,
                    "max_bytes": fs._SCAN_TAIL_MAX_BYTES,
                },
            )
        except Exception:
            pass

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

        2026-05-03 fix: also rotates the agent_memory_epoch
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

        #1015 (design decision 2026-09-04) widened this from subagents only to
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
            from .conductor_comms import (
                xaacp_drain_attach_outbox,
                xaacp_register_host_actor,
            )

            # #1095 step 4 (r0b2 804329fc-66d): SubagentStart IS the subagent's
            # identity-creation event. The attach runs through the ONE authority
            # with THIS hook-measured identity: local project -> local row;
            # cloud-bound -> forwarded to the gate, bounded, and owed to the
            # transport outbox on failure. The spawn never fails on it.
            xaacp_register_host_actor(
                project_root,
                host_session_id=str(payload.get("session_id") or "").strip(),
                host_kind="claude_code",
                actor_kind="subagent",
                host_agent_id=str(payload.get("agent_id") or "").strip(),
                source="subagent_start",
            )
            xaacp_drain_attach_outbox(project_root, limit=5)
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

    @staticmethod
    def _keep_project_bind_alive(payload: dict[str, object]) -> None:
        """Refresh this host session's LIVE project bind (#1095). Never
        revives an expired one; never raises -- a heartbeat cannot wedge the
        host."""
        sid = str(payload.get("session_id") or "").strip()
        if not sid:
            return
        try:
            from .session_project_bind_store import SessionProjectBindStore

            SessionProjectBindStore().touch_live(sid)
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _keep_xaacp_actor_alive(project_root: Path, payload: dict[str, object]) -> None:
        """Refresh this caller's EXISTING XAACP actor liveness (#1095 step 4a).

        The actor is derived exactly as SessionStart / SubagentStart register
        it (derive_agent_context_id over host kind, project, host session and
        -- for a subagent -- the host agent id). Never registers, never creates
        the store, never raises."""
        sid = str(payload.get("session_id") or "").strip()
        if not sid:
            return
        try:
            from .agent_memory_epoch import derive_agent_context_id
            from .conductor_comms import xaacp_touch_actor_liveness

            agent = str(payload.get("agent_id") or "").strip()
            actor = derive_agent_context_id(
                host_kind="claude_code",
                project_root=project_root,
                host_session_id=sid,
                agent_id=agent or None,
            )
            if actor:
                xaacp_touch_actor_liveness(project_root, actor)
        except Exception:  # noqa: BLE001 -- a heartbeat cannot wedge the host
            return

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

    def _register_native_mutation_lease(
        self,
        project_root: Path,
        payload: dict[str, object],
        pre_result: dict[str, object] | None,
        *,
        target_root: Path | None = None,
    ) -> dict[str, object] | None:
        """PreToolUse half of the native-tool bridge (r0b d7137542, native gap).

        ``target_root`` is the tree the native tool runs in (#1166 B7'); the
        lease is taken there. ``project_root`` is the project whose rules judged
        the tool (the conversation's selection, else HOME), where the gate
        minted any edit witness. Same project unless the shell moved.

        A host-native tool (Edit/Write/MultiEdit/NotebookEdit/Bash/...) runs in
        the HOST process, so AIDOCS cannot lease its write. Instead, BEFORE this
        hook returns a non-deny verdict, it registers a SHARED mutation lease
        keyed by the host ``tool_use_id``; the insert and the barrier check are
        one transaction, so either a switch's drain sees the native tool or the
        tool is denied here. PostToolUse / PostToolUseFailure release it. A lease
        whose release never arrives (host crash, user-denied ask) is TTL-only
        (``NATIVE_TOOL_TTL_S``) and FAILS CLOSED: switches refuse while it exists.
        Registration failure of any kind DENIES (this hook otherwise fails open).
        """
        from . import hook_pipeline as _hp

        hso = (pre_result or {}).get("hookSpecificOutput") if isinstance(pre_result, dict) else None
        denied = isinstance(hso, dict) and hso.get("permissionDecision") == "deny"
        why = _hp.native_tool_pre(
            target_root or project_root,
            # PROVEN, never defaulted: this handler is SHARED with Codex CLI,
            # whose events carry no PostToolUse lifecycle. An unproven host
            # records as `unknown`, which blocks ai_git switch.
            host_kind=_hp.resolve_hook_host_kind(payload),
            host_session_id=str(payload.get("session_id") or ""),
            tool_use_id=str(payload.get("tool_use_id") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            tool_input=payload.get("tool_input"),
            denied=denied,
        )
        if why:
            # ALLOW → DENY: decide_tool_use already minted the test-retry edit
            # witness for this invocation, and the tool will now never run, so no
            # PostToolUse can close it. Dispose it FIRST (best-effort, never
            # raises) or an abandoned preimage blocks later edits of that file.
            _hp.abort_edit_witness_after_allow(self.runtime, project_root, payload)
            return self._deny_envelope(why, "branch_transition_barrier")
        return pre_result

    @staticmethod
    def _release_native_mutation_lease(payload: dict[str, object]) -> None:
        """PostToolUse / PostToolUseFailure half of the native-tool bridge."""
        from . import hook_pipeline as _hp

        _hp.native_tool_post(
            host_kind=_hp.resolve_hook_host_kind(payload),
            host_session_id=str(payload.get("session_id") or ""),
            tool_use_id=str(payload.get("tool_use_id") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            tool_input=payload.get("tool_input"),
        )

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

        #1136 F2/F3: the core returns an explicit verdict
        (NOT_APPLICABLE | CLEAN | REDACTED | UNKNOWN). This adapter RENDERS
        REDACTED and UNKNOWN and never reads an exception as "raw is fine":
        once the tool is known redactable with a response, a raise anywhere
        (core included) is UNKNOWN -> shape-preserving withhold.
        """
        from . import hook_pipeline as _hp

        tool_name = _hp.normalize_tool_name(payload.get("tool_name"))
        if not _hp.is_redactable_tool(tool_name):
            return None
        tool_response = payload.get("tool_response")
        if tool_response is None:
            return None
        session_id = str(payload.get("session_id") or "")
        try:
            outcome = _hp.classify_output_redaction(
                self.runtime,
                "claude_code",
                payload,
                project_root=project_root,
            )
        except Exception:
            outcome = _hp.unknown_output_outcome(
                "claude_code", tool_name, tool_response, project_root
            )
        decision = outcome.decision
        unknown = outcome.state is _hp.OutputScanState.UNKNOWN
        # Audit is best-effort and never affects delivery. Event kinds are
        # literal at the emit site (the retention registry scans for them).
        if decision is None:
            if unknown:
                # Operator posture opted out of the fail-closed floor: the
                # output passes, but the audit says it was NOT certified.
                try:
                    self.runtime.hub.execution.record_event(
                        project_root,
                        event_kind="output_scan_unknown_passthrough",
                        source_kind="post_tool_use",
                        session_id=session_id,
                        capability_name=tool_name,
                        action_kind="passthrough",
                        status="degraded",
                        payload={"tool_name": tool_name, "host_kind": "claude_code"},
                    )
                except Exception:
                    pass
            return None
        withheld = bool(decision.withheld) or unknown
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind=(
                    "output_scan_unknown_withheld" if withheld else "host_output_redacted"
                ),
                source_kind="post_tool_use",
                session_id=session_id,
                capability_name=tool_name,
                action_kind="withhold" if withheld else "redacted",
                status="blocked" if withheld else "applied",
                payload={
                    "tool_name": tool_name,
                    "redaction_count": decision.count,
                    "categories": list(decision.categories),
                    "withheld": withheld,
                    "scan_state": outcome.state.value,
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


    def _handle_post_tool_use_failure(
        self,
        project_root: Path,
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        """The OTHER terminal door: a native tool the host reports as failed.

        A failed native edit never reaches PostToolUse, so nothing else retires
        the host-native witness (native leases carry no pid and no heartbeat, so
        expiry proves nothing). The host's failure IS the terminal evidence —
        recorded the same way the success arm does it, AFTER the lease release.
        ``record_edit_terminal_event`` is best-effort and never raises, so it
        cannot swallow or mask the underlying tool failure.

        NAMED ARM ON PURPOSE. This used to be an inline block in
        ``_dispatch_event``, which meant ``test_hook_event_dispatch``'s
        behavioural probe (it records ``_handle_*`` calls) could not observe it:
        the event could only be declared as UNHANDLED, which it is not. An arm
        that does real work must be reachable by the coverage probe that exists
        to prove arms are reached.

        NOT IN ``CANONICAL_HOOKS``: AIDOCS does not register this event, so on a
        stock install it does not arrive and the ordinary PostToolUse arm
        (which carries ``is_error`` responses — see
        ``test_failed_ordinary_post_tool_use_is_terminal_but_never_unlocks``)
        is the door that fires. This arm is the fail-safe for a host that
        delivers the event anyway.
        """
        self._release_native_mutation_lease(payload)
        from . import hook_pipeline as _hp

        _hp.record_edit_terminal_event(self.runtime, project_root, payload)
        return None

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
    # stays core-free (Article XXI).
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
    # class attr kept as a back-compat alias for tests — lazy (Article XXI).
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

    def _record_degraded_hook_event(
        self,
        home_root: Path,
        event_name: str,
        payload: dict[str, object],
        reason: str,
    ) -> None:
        """#1166 (r0b1 60f49c3a-505 blocker D): a METADATA-ONLY trace on HOME
        when no project's rules could be applied, or a Post's admission cannot
        be proven. Never the prompt, tool_input or cwd -- those belong to the
        project that governs them, which is exactly what is unknown here. Grants
        nothing. Never raises.

        record_hook_event keeps only {hook_event_name, tool_name, tool_input,
        prompt, cwd}, so the reason travels in a ``tool_input`` built HERE from
        metadata -- the call's real tool_input is never copied."""
        degraded = {
            "aidocs_degraded": str(reason)[:300],
            "tool_use_id": str(payload.get("tool_use_id") or ""),
            "host_session_id": str(payload.get("session_id") or ""),
        }
        trace = {
            "hook_event_name": event_name,
            "tool_name": payload.get("tool_name"),
            "tool_input": {k: v for k, v in degraded.items() if v},
        }
        try:
            self._record_hook_event(
                home_root,
                event_name=event_name,
                payload={k: v for k, v in trace.items() if v},
            )
        except Exception:  # noqa: BLE001 -- a trace must never change a verdict
            pass

    def _post_rules_root(
        self,
        home_root: Path,
        payload: dict[str, object],
        session_id: str,
        tool_use_id: str,
    ) -> Path | None:
        """#1166: the project a PostToolUse / PostToolUseFailure records under.

        * The project that ADMITTED this invocation at Pre, keyed by
          (conversation, tool_use_id) -- whatever is selected now.
        * Anything else is UNPROVABLE -> None, and the caller must not
          substitute another project (r0b1 60f49c3a-505 / bbb4cb96-2ce):
          - no ``tool_use_id`` or no conversation id on the payload: nothing
            to correlate by. Pre DENIES such a call (r0b1 b4b5110c-526 Z1), so
            no governed native body ran under any project's rules; it is never
            re-resolved onto whatever is selected (or HOME) now;
          - no admission row: Pre denies any call whose admission could not be
            written (operator 2026-10-18), so a missing row is a denied,
            expired, consumed, mismatched or forged invocation."""
        from .tool_admission_store import take_admission

        del home_root, payload  # never a fallback: see above
        if not tool_use_id or not session_id:
            return None
        return take_admission(tool_use_id, host_session_id=session_id)

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
    # kept as back-compat aliases for tests — lazy (Article XXI).
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
# module-scope import does not violate the Article XXI adapter inversion pinned
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
        # AFTER the shim migration, so a legacy `-m` entry converges onto the
        # stable launcher in ONE pass instead of staying generation-pinned
        # until some later hook happens to fire (#925j).
        changed = _repair_generation_pinned_command(cfg) or changed
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
        changed = _repair_post_tool_use_failure(hooks_root, template_cmd) or changed
        # The env wait_next parks need (CANONICAL_ENV), through the installer's
        # own helper so the two writers cannot drift. Only absent keys are
        # added, so an existing install catches up without re-running setup.
        from .claude_hooks_install import merge_aidocs_env

        changed = "added" in merge_aidocs_env(cfg).values() or changed

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


def _repair_generation_pinned_command(cfg: dict) -> bool:
    """#925j/#925k convergence: a host command naming `runtime/generations/<id>`
    is one GC pass away from naming a DELETED binary — and then NO shim fix can
    rescue it, because the fail-closed launcher never starts. The passive
    reconciler therefore rewrites such an entry onto the stable, non-prunable
    launcher. Nothing is weakened: the shim still reads the activation pointer
    and delegates (#925i), so the ACTIVE generation is still what governs.

    THE REWRITE ITSELF lives in `claude_hooks_install.stabilize_hook_commands`,
    shared with `aidocs runtime --reconcile-hook-shim` and the bootstrap health
    check. Two copies of a rule this load-bearing is how the last eight
    twin-implementation defects happened, and the passive path and the recovery
    path must not be able to disagree about what "stable" means.
    """
    from .claude_hooks_install import stabilize_hook_commands

    return stabilize_hook_commands(cfg)


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


def _repair_post_tool_use_failure(hooks_root: dict, template_cmd: dict | None) -> bool:
    """Register the aidocs PostToolUseFailure group on an install that lacks it.

    The host reports a failed tool call ONLY through this event, and its arm
    is the failed edit's terminal receipt. Installs that predate its
    registration (2026-10-27) never received it. Status, timeout and matcher
    come from the installer's CANONICAL_HOOKS entry, so the two writers agree.
    A present aidocs group is left alone; a non-list value is not ours to
    rewrite; no template means nothing to clone, so nothing changes.
    """
    from .claude_hooks_install import CANONICAL_HOOKS, _is_aidocs_group

    groups = hooks_root.get("PostToolUseFailure")
    if groups is not None and not isinstance(groups, list):
        return False
    if any(_is_aidocs_group(g) for g in groups or []):
        return False
    if template_cmd is None:
        return False
    status, timeout, matcher = next(
        (s, t, m) for event, s, t, m in CANONICAL_HOOKS if event == "PostToolUseFailure"
    )
    group: dict = {"hooks": [dict(template_cmd, timeout=timeout, statusMessage=status)]}
    if matcher:
        group["matcher"] = matcher
    hooks_root["PostToolUseFailure"] = [*(groups or []), group]
    return True


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


def _report_hook_failure(exc: BaseException, payload: dict[str, object] | None = None) -> None:
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
    # #1135 item 8: a PreToolUse shell / file mutation is DENIED by this hook
    # itself when its evaluation raises (see _mutation_exception_verdict), so
    # the line must not claim fail-open for it either.
    _closed_here = _mutation_exception_verdict(payload or {}, exc) is not None
    if _wrapped:
        _posture = "the shim will DENY this call"
    elif _closed_here:
        _posture = "the hook will DENY this mutation (#1135)"
    else:
        _posture = "gate fails OPEN for this event"
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


def _host_destructive_floor_verdict(payload: dict[str, object]) -> dict[str, object] | None:
    """The #1135 floor's verdict for this payload (stdlib-only module).

    A floor that cannot run is not a floor that said "allow": for a shell or
    file-mutation tool the failure itself is answered with a deny.
    """
    try:
        from .host_destructive_floor import evaluate_pretooluse

        return evaluate_pretooluse(payload)
    except Exception as exc:  # noqa: BLE001 - converted to a verdict below
        return _mutation_exception_verdict(payload, exc)


#: #1166 (r0b1 60f49c3a-505 blocker A): what an unservable-project refusal
#: leaves open -- by tool AND mode. NOT the managed-mode gate's bootstrap set:
#: "not blocked by an inactive session" is not "safe when no project can be
#: served" (that set includes ai_backlog writes, admin_clear_reconnect and both
#: connects). Only the way back and pure, project-free diagnostics:
#:   * ai_project status / unbind -- see and drop the broken selection;
#:   * ai_whoami, ai_gate_explain -- request metadata and code-level tables;
#:   * ToolSearch -- the schema loader for the above.
#: ``None`` = no mode argument to check. An empty or unknown mode is denied.
_UNSERVABLE_RECOVERY: dict[str, frozenset[str] | None] = {
    "ai_project": frozenset({"status", "unbind"}),
    "ai_whoami": None,
    "ai_gate_explain": None,
    "toolsearch": None,
}


def _is_unservable_recovery_call(payload: dict[str, object]) -> bool:
    """True for a call in ``_UNSERVABLE_RECOVERY`` (tool AND mode): the way back
    and project-free diagnostics, left open by every #1166 fail-closed refusal."""
    bare = str(payload.get("tool_name") or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    if bare not in _UNSERVABLE_RECOVERY:
        return False
    modes = _UNSERVABLE_RECOVERY[bare]
    tool_input = payload.get("tool_input")
    mode = str(tool_input.get("mode") or "").strip().lower() if isinstance(tool_input, dict) else ""
    return modes is None or mode in modes


def _note_unservable_stop(event_name: str, exc: BaseException) -> None:
    """#1166 (operator 2026-10-18): a turn whose project cannot be served still
    STOPS -- the Stop hook must let the agent stop -- but never silently: none
    of its project's end-of-turn checks (failure stewardship, update-intent,
    ...) could run, and this says why. Stop is a stderr-only event for this
    host (claude_hook_shim posture table), so the reason goes to stderr, next
    to the metadata trace the caller records on HOME. Never raises."""
    try:
        import sys as _sys

        print(
            f"[aidocs {event_name}] the turn sealed WITHOUT its project's end-of-turn "
            f"checks (failure stewardship, update-intent, deploy/backlog notices): {exc} "
            "Fix with ai_project(mode='status') / ai_project(mode='unbind').",
            file=_sys.stderr,
        )
    except Exception:  # noqa: BLE001 -- a diagnostic must never change a verdict
        pass


def _mint_binding_intents(
    payload: dict[str, object], governing_root: Path | None, generation: object = ...
) -> None:
    """#1166: record the moves this prompt states (binding_intent_store),
    stamped with THIS prompt's generation (begin_prompt already revoked the
    previous ones). A session named without a project ("connect session b") is
    THIS project's b -- the one the conversation is governed by as the
    operator speaks. Never raises -- a failure leaves no grant, so every move
    is refused, never allowed. No generation (the revocation failed) mints
    nothing; a direct caller that passes none gets a fresh one."""
    if generation is ...:
        from .binding_intent_store import begin_prompt

        generation = begin_prompt(str(payload.get("session_id") or ""))
    if generation is None:
        return
    try:
        from .binding_intent_store import replace_binding_intents
        from .canonical_intent_registry import detect_binding_intents

        intents = detect_binding_intents(
            str(payload.get("prompt") or ""), nlp_root=governing_root
        )
        for intent in intents:
            if str(intent.get("tool") or "").startswith("ai_session") and not intent.get("project"):
                intent["project"] = str(governing_root) if governing_root else ""
        replace_binding_intents(str(payload.get("session_id") or ""), intents, generation)
    except Exception:  # noqa: BLE001 -- see the docstring
        pass


_REVOCATION_UNSAFE_REASON = (
    "AIDOCS blocked this prompt: it could not record that a new prompt began "
    "(neither its revocation latch nor its grant store could be written), so the "
    "previous prompt's 'connect project/session' consent could not be revoked. "
    "Check that ~/.aidocs is writable (disk space, permissions, locks), then send "
    "the prompt again."
)
_UNPROVABLE_POST_REASON = (
    "AIDOCS could not prove which project admitted this tool call (no admission "
    "record for it), so its outcome was recorded under no project and its output "
    "was WITHHELD. Treat this result as unverified; re-run the call to get it."
)
_WITHHELD_NOTICE = (
    "[AIDOCS: output withheld -- this call's project admission could not be proven]"
)


_POST_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})


def _broker_fallback(event_name: str, reason: str | None, dispatched: bool) -> str:
    """#1217 (r0b2 design ruling edee80f4-a0d): after a broker miss, "local"
    or "unknown".

    A Post is evaluated exactly once, because its evaluation takes the one-shot
    admission row. So it is re-evaluated locally only when the broker PROVABLY
    did not evaluate it: never dispatched (no or untrusted registration, connect
    refused, a failure before the send), or the broker's TYPED
    ``dropped_without_evaluation`` refusal. Never a substring of free-form error
    text: the broker wraps ANY handler exception the same way, after the
    evaluation may already have taken the admission (r0b2 828d3346-267). A
    timeout or a lost reply after dispatch is UNKNOWN. Every other event keeps
    its local fallback -- PreToolUse's strict floor included."""
    from .hook_broker_client import REASON_DROPPED_UNEVALUATED

    if str(event_name or "").strip() not in _POST_EVENTS:
        return "local"
    if not dispatched:
        return "local"
    head = str(reason or "").split(":", 1)[0].strip()
    if head == REASON_DROPPED_UNEVALUATED:
        return "local"
    return "unknown"


_POST_OUTCOME_UNKNOWN_REASON = (
    "AIDOCS post_outcome_unknown: this call's PostToolUse reached the hook broker "
    "but its result did not come back in time ({reason}, {elapsed_ms:.0f} ms). "
    "The broker may already have recorded it, so it was NOT evaluated a second "
    "time, and its output was WITHHELD rather than shown unguarded. The command "
    "itself ran; re-run it if you need the output."
)


def _post_outcome_unknown_response(
    payload: dict[str, object], reason: str | None, elapsed_ms: float
) -> dict[str, object] | None:
    """#1217: the answer for a Post whose single broker evaluation outcome is
    unknown. PostToolUse: the output is withheld with a NAMED reason (never the
    generic unprovable one). PostToolUseFailure has no output to guard: nothing
    is emitted, and the reason goes to stderr."""
    text = _POST_OUTCOME_UNKNOWN_REASON.format(
        reason=reason or "no reason", elapsed_ms=float(elapsed_ms or 0.0)
    )
    if str(payload.get("hook_event_name") or "").strip() != "PostToolUse":
        sys.stderr.write(text + "\n")
        return None
    envelope = _unprovable_post_envelope(payload)
    envelope["reason"] = text
    hso = envelope.get("hookSpecificOutput")
    if isinstance(hso, dict):
        hso["additionalContext"] = text
    return envelope


def _unprovable_post_envelope(payload: dict[str, object]) -> dict[str, object]:
    """#1166 (r0b1 b4b5110c-526 gate 1): the PostToolUse answer for a call
    whose admission cannot be proven.

    ``decision: block`` alone only puts the reason NEXT TO the tool result --
    Claude still sees the original output (hooks contract). What replaces it
    is ``hookSpecificOutput.updatedToolOutput``, which the host validates
    against the tool's output shape, so the replacement is a shape-preserving
    copy of the real ``tool_response`` with every text leaf withheld
    (output_guard.withhold_tool_response -- the walk the credential redactor
    already ships through this hook). Where there is no response or no text to
    withhold, nothing is replaced and the block + reason are the notice: that
    residual is covered by Pre, which admits no call whose admission it could
    not record or correlate."""
    from .output_guard import withhold_tool_response

    envelope: dict[str, object] = {"decision": "block", "reason": _UNPROVABLE_POST_REASON}
    response = payload.get("tool_response")
    withheld, replaced = (
        withhold_tool_response(response, _WITHHELD_NOTICE) if response is not None else (None, 0)
    )
    hso: dict[str, object] = {
        "hookEventName": "PostToolUse",
        "additionalContext": _UNPROVABLE_POST_REASON,
    }
    if replaced:
        hso["updatedToolOutput"] = withheld
    envelope["hookSpecificOutput"] = hso
    return envelope


def _admission_uncorrelated_verdict() -> dict[str, object]:
    """#1166 (r0b1 b4b5110c-526 Z1): deny a call the host sent without a
    tool_use_id or conversation id -- its Post could never be correlated to
    the project that judged it, so its outcome would land wherever the
    selection points by then."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "AIDOCS GATE REFUSING - this call arrived without a tool_use_id or "
                "conversation id, so its outcome could not be attributed to the "
                "project that judged it. This host is not supported for governed "
                "native tools -- ai_whoami and ai_gate_explain stay available."
            ),
            "blocked_by": "admission_uncorrelated",
        }
    }


def _admission_unrecorded_verdict(project_root: Path) -> dict[str, object]:
    """#1166 (operator 2026-10-18): deny a call whose admission could not be
    recorded -- its outcome could never be attributed to the project that
    judged it."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "AIDOCS GATE REFUSING - this call's admission by project "
                f"{project_root} could not be recorded (the install-wide config store "
                "did not accept the write), so its outcome could not be attributed to "
                "the project that judged it. Retry; if it persists the store is locked "
                "or damaged -- ai_whoami and ai_gate_explain stay available."
            ),
            "blocked_by": "admission_unrecorded",
        }
    }


def _unservable_project_verdict(
    payload: dict[str, object], exc: BaseException
) -> dict[str, object] | None:
    """#1166: deny EVERY governed tool while this conversation's project --
    its selection, or the window's HOME -- cannot be served; reads included.

    Only ``_UNSERVABLE_RECOVERY`` (tool + mode) stays open -- the way back and
    project-free diagnostics -- or the refusal would name a remedy it has just
    locked. A recovery call that reaches the MCP daemon is still governed
    there (identity choke, RBAC); this only declines to refuse it here."""
    if _is_unservable_recovery_call(payload):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"AIDOCS GATE REFUSING - {exc} No project's rules can be applied, so "
                "no governed tool runs (reads included). Open: ai_project(mode='status' "
                "| 'unbind') to see or drop a broken selection, ai_whoami, ai_gate_explain."
            ),
            "blocked_by": "project_unservable",
        }
    }


def _mutation_exception_verdict(
    payload: dict[str, object], exc: BaseException
) -> dict[str, object] | None:
    """Deny for a PreToolUse shell / file mutation whose evaluation raised."""
    try:
        from .host_destructive_floor import exception_deny_response

        return exception_deny_response(payload, exc)
    except Exception:  # noqa: BLE001 - the floor module itself is broken
        event = str(payload.get("hook_event_name") or "").strip()
        tool = str(payload.get("tool_name") or "").strip().lower()
        if event == "PreToolUse" and tool in _HOOK_EXCEPTION_DENY_TOOLS:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "AIDOCS GATE REFUSING - the PreToolUse evaluation raised "
                        f"({type(exc).__name__}) and the host floor is unavailable; a "
                        "shell or file mutation is denied rather than permitted (#1135)."
                    ),
                    "blocked_by": "hook_exception_fail_closed",
                }
            }
        return None


#: Last-resort copy of host_destructive_floor.HOST_MUTATION_TOOLS, used only
#: when that module cannot be imported. Held in lockstep by
#: tests/security/test_destructive_scope_floor_hardening_1135.py.
_HOOK_EXCEPTION_DENY_TOOLS = frozenset(
    {
        "bash", "powershell", "pwsh", "cmd", "wsl", "monitor",
        "edit", "write", "multiedit", "patch", "applypatch", "apply_patch",
        "notebookedit", "str_replace_based_edit_tool", "update",
    }
)


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

    # #1135 HOST DESTRUCTIVE SCOPE FLOOR, module form. The launcher shim runs
    # the same floor before it ever imports this package; this copy covers a
    # host still on `-m aidocs_mcp.claude_hook`. It runs BEFORE the integrity
    # gate, the broker, project detection, config and identity, in managed
    # and non-managed cwds alike, and reads none of them.
    _floor_verdict = _host_destructive_floor_verdict(payload)
    if _floor_verdict is not None:
        json.dump(_floor_verdict, sys.stdout)
        sys.stdout.write("\n")
        return

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

    # #1095: install-wide startup steps at process entry, AFTER the integrity
    # gate (a drifted package runs none of its own code). A single stat when
    # there is nothing to migrate; never raises.
    from .process_bootstrap import run_process_bootstrap

    run_process_bootstrap()

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
            evaluate_via_broker_outcome,
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
        verdict, broker_reason, _dispatched = evaluate_via_broker_outcome(
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
            elif _broker_fallback(event_name, broker_reason, _dispatched) == "unknown":
                # #1217: the broker may already have evaluated this Post and
                # TAKEN its one-shot admission. Evaluating it again would find
                # no row and withhold a good result -- so it is NOT re-run.
                response = _post_outcome_unknown_response(payload, broker_reason, _elapsed_ms)
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
        _report_hook_failure(exc, payload)
        # #1135 item 8: a PreToolUse shell / file mutation is DENIED, not
        # left to the host's "crashed hook is non-blocking" reading.
        _closed = _mutation_exception_verdict(payload, exc)
        if _closed is not None:
            json.dump(_closed, sys.stdout)
            sys.stdout.write("\n")
            return
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
