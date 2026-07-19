from __future__ import annotations

from pathlib import Path
from typing import Any

from .session_query_gate_store import SessionQueryGateStore


class QueryGateStore:
    """Session-scoped gate tracking per-file discovery grants and
    per-turn tool grants.

    Storage moved to sqlite (``session_query_gate`` table) in Beat 3.
    This class keeps its original public shape so the dozens of callers
    in access_gate, claude_hook, runtime_service, conductor_comms etc.
    don't have to change; the legacy per-session JSON files are ingested
    and hard-deleted by the store's init path.
    """

    _KEEP = SessionQueryGateStore._KEEP

    def __init__(self) -> None:
        self._store = SessionQueryGateStore()

    def gate_path(self, project_root: Path, session_id: str) -> Path:
        # Path kept advisory for diagnostics that show "where gate state
        # lives for this session". Post-Beat-3 the file is absent — the
        # store's init deletes it on first touch.
        return project_root / ".MEMORY" / "sessions" / session_id / "query-gate.json"

    def _sticky_path(self, project_root: Path, session_id: str) -> Path:
        # ADVISORY ONLY — sticky-grant authority lives in SQLite
        # (StickyGrantsStore); this path is NOT read or written as authority.
        # It is retained so diagnostics/UI that display a "sticky-grants file
        # location" keep showing a sensible path. The legacy sidecar JSON is
        # never read back (it cannot rehydrate grant authority) and is never
        # written (no projection that could diverge from SQL).
        return project_root / ".MEMORY" / "sessions" / session_id / "sticky-grants.json"

    # Sink-level defense: the ONLY raw tools permitted in the sticky
    # sidecar. Everything else (bash, grep, read, edit, write, multiedit,
    # patch, apply_patch, ...) must flow through per-turn grants so the
    # operator re-demonstrates intent every prompt. Enforced on BOTH
    # read and write so a legacy sidecar with forbidden tokens heals
    # itself without needing an out-of-band migration. See T0 security
    # backlog item #15 — sticky-grants injection vector fix.
    _STICKY_RAW_TOOL_FORBIDDEN: frozenset[str] = frozenset(
        {
            "bash",
            "grep",
            "read",
            "edit",
            "write",
            "multiedit",
            "patch",
            "apply_patch",
        },
    )

    @classmethod
    def _sanitize_sticky_tokens(cls, tokens: list[str]) -> list[str]:
        """Drop forbidden raw-tool tokens. glob stays (listing filenames
        is safe). Dedup + sort for stable output.
        """
        kept, _dropped = cls._sanitize_with_drops(tokens)
        return kept

    @classmethod
    def _sanitize_with_drops(
        cls,
        tokens: list[str],
    ) -> tuple[list[str], list[str]]:
        """Like _sanitize_sticky_tokens but returns (kept, dropped) so
        the write path can emit a diagnostic when an operator's
        sticky phrase produces a token that the sink refuses.

        #99 (Phoenix 2026-05-10): the sink-level forbid list is
        intentional T0 doctrine, but pre-fix the drop was silent and
        operators saw "I said it right and the grant didn't stick"
        with no signal as to why. The dropped list lets callers emit
        ``sticky_grant_silently_dropped`` so the audit chain explains
        the dropped grants.
        """
        kept_set: set[str] = set()
        dropped_set: set[str] = set()
        for raw in tokens:
            t = str(raw).strip()
            if not t:
                continue
            if t.lower() in cls._STICKY_RAW_TOOL_FORBIDDEN:
                dropped_set.add(t)
            else:
                kept_set.add(t)
        return sorted(kept_set), sorted(dropped_set)

    def _load_sticky(self, project_root: Path, session_id: str) -> list[str]:
        """Read active sticky tools — SQLite ONLY (hard authority law,
        2026-05).

        The legacy JSON sidecar is NEVER read back: not as a sqlite-failure
        fallback and not via ingest-on-read. A file must not be able to
        rehydrate grant authority when sqlite is empty/unavailable. On any
        sqlite error this fails CLOSED (no grants), which is the safe
        direction for an authority gate.

        Return value goes through the sink sanitizer so any forbidden
        raw-tool tokens are dropped on this read path too — defense-in-depth
        against a compromised store.
        """
        try:
            from .sticky_grants_store import StickyGrantsStore

            tools = StickyGrantsStore().active_tools_for_session(project_root, session_id)
        except Exception:
            tools = []  # fail closed — never resurrect from the sidecar file
        return self._sanitize_sticky_tokens(tools)

    def _write_sticky(
        self,
        project_root: Path,
        session_id: str,
        sticky: list[str],
        *,
        source: str = "layer1_sticky_write",
    ) -> None:
        """Persist the sticky set.

        Writes go to sqlite via StickyGrantsStore as tier-1 grants and
        NOWHERE else — the legacy JSON sidecar is no longer written, so no
        on-disk projection can diverge from (or rehydrate) SQL authority.
        The sanitizer still runs so forbidden raw-tool tokens are rejected
        before they reach the store.

        Idempotent by design: the store's ingest-on-read dedup logic
        means re-registering a tool already-active is safe. The old
        set-and-diff pattern at the caller still works unchanged.

        ``source`` (Patch C-sticky, 2026-04-29): registration_source
        threaded down to register_grant so the sticky_grant_registered
        audit row distinguishes which write path triggered each new
        registration. Default ``"layer1_sticky_write"`` — caller
        overrides via kwarg (e.g. ``"layer2_direct"`` from
        add_sticky_user_intent_tools).

        #99 (Phoenix 2026-05-10): when the sanitizer drops tokens
        (operator phrase parsed correctly but the verb names a sink-
        forbidden raw tool like 'grep' / 'edit'), emit a
        ``sticky_grant_silently_dropped`` execution_event with the
        dropped list so the audit chain explains why the operator's
        sticky phrase didn't take. Pre-fix the drop was silent; this
        closes the "I said it right and it didn't stick" gap.
        """
        normalized, dropped = self._sanitize_with_drops(sticky)
        if dropped:
            try:
                from .execution_index_store import ExecutionIndexStore

                ExecutionIndexStore().record_event(
                    project_root,
                    event_kind="sticky_grant_silently_dropped",
                    source_kind="query_gate.write_sticky",
                    session_id=session_id or None,
                    capability_name="",
                    action_kind="grant_sanitize",
                    target_entity=session_id,
                    status="dropped",
                    payload={
                        "session_id": session_id,
                        "registration_source": source,
                        "dropped_tokens": dropped,
                        "kept_tokens": normalized,
                        "reason": (
                            "Tokens are in the sink-forbidden raw-tool "
                            "set (bash/grep/read/edit/write/multiedit/"
                            "patch/apply_patch). T0 doctrine refuses "
                            "to sticky-grant these — operator must "
                            "re-demonstrate intent each turn, or use "
                            "tier-2 narrowed scope (e.g. 'allow bash "
                            "to run X, persist the grant')."
                        ),
                    },
                )
            except Exception:
                pass
        try:
            from .sticky_grants_store import StickyGrantsStore

            store = StickyGrantsStore()
            existing = set(
                store.active_tools_for_session(
                    project_root,
                    session_id,
                ),
            )
            new_set = set(normalized)
            # Add tools that appeared this turn.
            for tool in new_set - existing:
                try:
                    store.register_grant(
                        project_root,
                        session_id=session_id,
                        tier=1,
                        tool=tool,
                        phrase="",
                        registered_by_user_id="",
                        judge_verdict="legacy-write-path",
                        confirmation_answer="legacy-write-path",
                        registration_source=source,
                    )
                except Exception:
                    # Skip a single row on failure but keep writing others.
                    pass
            # Revoke tools that disappeared this turn — keeps sqlite
            # in sync with the caller's "full replacement" semantics.
            for tool in existing - new_set:
                try:
                    store.revoke_tool(
                        project_root,
                        session_id=session_id,
                        tool=tool,
                        revoked_reason="legacy-write-path-removed",
                    )
                except Exception:
                    pass
        except Exception:
            # sqlite is the sole authority — no sidecar fallback write.
            pass
        # No sidecar mirror: sticky-grant authority lives ONLY in sqlite
        # and must never be projected to a file that could rehydrate it
        # (hard authority law, 2026-05).

    def _init(self, project_root: Path) -> None:
        self._store.init_db(project_root)

    def set_user_intent_tools(
        self,
        project_root: Path,
        session_id: str,
        tools: list[str],
        *,
        sticky: bool = False,
        provenance: dict | None = None,
    ) -> None:
        # TTL contract: sticky=False grants live for the current
        # UserPromptSubmit turn only — clear_expired_grants() drops them
        # on the next turn. sticky=True unions the tools into the SQLite
        # sticky set (StickyGrantsStore) that survives per-turn clears until
        # the operator explicitly revokes them.
        #
        # SEC-006 (2026-04-23): optional provenance dict (tool →
        # {source_kind, actor, scope, created_at, expires_at}) is
        # threaded through to the store. Back-compat: legacy callers
        # that don't pass provenance still work; their tools are
        # stored without attribution and will read back with
        # source_kind='unknown'.
        self._init(project_root)
        sticky_set = set(self._load_sticky(project_root, session_id))
        new_tools = {str(t).strip() for t in tools if str(t).strip()}
        if sticky:
            sticky_set |= new_tools
            self._write_sticky(project_root, session_id, sorted(sticky_set))
        union = sorted(sticky_set | new_tools)
        # AIDOCS shell provider lock — Invariant #38 producer hygiene
        # (2026-04-29 Path A). Bare 'bash' is structurally never a
        # valid authority token in user_intent_tools because:
        #   - Native Bash is permanently T0-blocked in managed AIDOCS
        #     sessions (Invariant #38). user_granted=True from this
        #     list would lift T0 against doctrine.
        #   - Lane delegation reads this list as the conductor's
        #     "I have authority over X" gate (runtime_service.
        #     grant_raw_tools_for_lane). Bare bash here would
        #     authorize lane-delegated raw bash.
        # The sticky read path already filters bash via
        # _STICKY_RAW_TOOL_FORBIDDEN; this filter closes the
        # parallel per-turn flat-list write gap. Matching consumer-
        # side hard stops in check_raw_shell and
        # grant_raw_tools_for_lane provide belt-and-suspenders for
        # legacy rows / direct sqlite writes. Filter is bash-only on
        # purpose: other raw file tools (grep, read, edit, write)
        # have their own gate semantics and remain valid per-turn
        # tokens for the operator to authorize.
        union = [t for t in union if t.lower() != "bash"]
        self._store.set_user_intent_tools(
            project_root,
            session_id,
            union,
            provenance=provenance,
        )

    def add_sticky_user_intent_tools(
        self,
        project_root: Path,
        session_id: str,
        tools: list[str],
    ) -> None:
        """Union tools into the sticky sidecar AND into the store,
        preserving any per-turn grants already written this turn.

        Contrast with set_user_intent_tools(..., sticky=True), which
        replaces the store's per-turn tools with only the passed set.
        Use this when you need to ADD sticky grants on top of an
        already-written per-turn set (Layer-2 NLP surfacing firing
        after Layer-1 raw-tool verb-proximity grants).
        """
        self._init(project_root)
        new_tools = {str(t).strip() for t in tools if str(t).strip()}
        if not new_tools:
            return
        sticky_set = set(self._load_sticky(project_root, session_id))
        sticky_set |= new_tools
        self._write_sticky(
            project_root,
            session_id,
            sorted(sticky_set),
            source="layer2_direct",
        )
        current = set(self._store.get_user_intent_tools(project_root, session_id))
        self._store.set_user_intent_tools(
            project_root,
            session_id,
            sorted(current | new_tools),
        )

    def get_user_intent_tools_with_provenance(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict:
        """SEC-006 (2026-04-23) read helper. Returns a dict keyed by
        tool_name whose values carry {source_kind, actor, scope,
        created_at, expires_at}. Tools present but without recorded
        attribution surface as source_kind='unknown'.
        """
        self._init(project_root)
        return self._store.get_user_intent_tools_meta(project_root, session_id)

    def set_agent_research_override(
        self,
        project_root: Path,
        session_id: str,
        granted: bool,
    ) -> None:
        """Facade passthrough — per-turn agent-research override (#365). Written
        by prompt_mutator on the hook process, read by the MCP-server process."""
        self._init(project_root)
        self._store.set_agent_research_override(project_root, session_id, granted)

    def get_agent_research_override(
        self,
        project_root: Path,
        session_id: str,
    ) -> bool:
        """Facade passthrough — read the per-turn agent-research override (#365)."""
        self._init(project_root)
        return self._store.get_agent_research_override(project_root, session_id)


    def get_user_intent_tools_meta(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict:
        """Facade-parity passthrough to the store's method. Callers
        should prefer get_user_intent_tools_with_provenance — this
        direct alias exists so the facade parity check
        (tests/security/test_query_gate_facade_parity.py) stays green
        without hand-maintaining an exemption list.
        """
        self._init(project_root)
        return self._store.get_user_intent_tools_meta(project_root, session_id)

    def activate_pending_grant(
        self,
        project_root: Path,
        session_id: str,
        tool_name: str,
    ) -> None:
        """SEC-007 (2026-04-23): flip a pending grant to active.
        Called from the confirmation handler when the operator
        confirms a corpo-flavor grant.
        """
        self._init(project_root)
        self._store.activate_pending_grant(project_root, session_id, tool_name)

    def deny_pending_grant(
        self,
        project_root: Path,
        session_id: str,
        tool_name: str,
    ) -> None:
        """SEC-007 (2026-04-23): drop a pending grant entirely.
        Called from the confirmation handler when the operator
        denies a corpo-flavor grant.
        """
        self._init(project_root)
        self._store.deny_pending_grant(project_root, session_id, tool_name)

    # ── SEC-005 degraded-state (2026-04-23) ──

    def set_degraded_state(
        self,
        project_root: Path,
        session_id: str,
        *,
        reason: str,
        failure_event_id: str = "",
    ) -> None:
        self._init(project_root)
        self._store.set_degraded_state(
            project_root,
            session_id,
            reason=reason,
            failure_event_id=failure_event_id,
        )

    def clear_degraded_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> None:
        self._init(project_root)
        self._store.clear_degraded_state(project_root, session_id)

    def get_degraded_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict:
        self._init(project_root)
        return self._store.get_degraded_state(project_root, session_id)

    def clear_expired_grants(self, project_root: Path, session_id: str) -> None:
        # Called from claude_hook.UserPromptSubmit at the top of every
        # new turn: drops per-turn (sticky=False) grants and restores
        # the underlying store to the sticky baseline. Sticky grants
        # survive until revoked via clear_sticky_grants.
        self._init(project_root)
        sticky = self._load_sticky(project_root, session_id)
        self._store.set_user_intent_tools(project_root, session_id, sticky)

    def clear_sticky_grants(
        self,
        project_root: Path,
        session_id: str,
        tools: list[str] | None = None,
    ) -> None:
        # Explicit revocation path. tools=None drops every sticky grant;
        # otherwise only the named tools are removed. The underlying
        # store is refreshed to the new sticky baseline so the next
        # PreToolUse check sees the revocation immediately.
        self._init(project_root)
        if tools is None:
            sticky_new: list[str] = []
        else:
            remove = {str(t).strip() for t in tools if str(t).strip()}
            sticky_new = [t for t in self._load_sticky(project_root, session_id) if t not in remove]
        self._write_sticky(project_root, session_id, sticky_new)
        self._store.set_user_intent_tools(project_root, session_id, sorted(set(sticky_new)))

    def get_sticky_user_intent_tools(self, project_root: Path, session_id: str) -> list[str]:
        return self._load_sticky(project_root, session_id)

    def get_user_intent_tools(self, project_root: Path, session_id: str) -> list[str]:
        self._init(project_root)
        return self._store.get_user_intent_tools(project_root, session_id)

    def set_user_intent_bash_subcommands(
        self,
        project_root: Path,
        session_id: str,
        subcommands: list[str],
    ) -> None:
        self._init(project_root)
        self._store.set_user_intent_bash_subcommands(project_root, session_id, subcommands)

    def get_user_intent_bash_subcommands(self, project_root: Path, session_id: str) -> list[str]:
        self._init(project_root)
        return self._store.get_user_intent_bash_subcommands(project_root, session_id)

    # forced_work accessors REMOVED 2026-04-30 (autowake removal).
    # Underlying column kept in storage for migration safety.

    # ── Ask-state confirmations (judge override, 1-turn TTL) ──

    def increment_turn_counter(self, project_root: Path, session_id: str) -> int:
        self._init(project_root)
        return self._store.increment_turn_counter(project_root, session_id)

    # ── Causal turn id (#441) ─────────────────────────────────────

    def rotate_current_turn_id(
        self,
        project_root: Path,
        session_id: str,
        *,
        instruction_content_hash: str = "",
        actor_id: str = "",
        actor_role: str = "",
        origin_channel: str = "user_prompt_submit",
    ) -> str:
        """Mint + store a fresh server-generated causal turn id.

        UserPromptSubmit calls this for operator-authored, authority-
        bearing prompts only; injections/interrupts never rotate. #467:
        also opens the causal turn entity + its revision-1 UserPrompt
        instruction event (content HASH only — the body never lands here).
        """
        self._init(project_root)
        return self._store.rotate_current_turn_id(
            project_root,
            session_id,
            instruction_content_hash=instruction_content_hash,
            actor_id=actor_id,
            actor_role=actor_role,
            origin_channel=origin_channel,
        )

    def get_current_turn_id(self, project_root: Path, session_id: str) -> str:
        self._init(project_root)
        return self._store.get_current_turn_id(project_root, session_id)

    def bump_grants_generation(self, project_root: Path, session_id: str) -> int:
        """Advance the NLP-sticky-grants generation counter.

        Call after any sticky user_intent_tools change so the MCP
        call_tool wrapper detects the delta and re-enables deferred
        tools in-process (FastMCP auto-emits tools/list_changed).
        """
        self._init(project_root)
        return self._store.bump_grants_generation(project_root, session_id)

    def get_grants_generation(self, project_root: Path, session_id: str) -> int:
        self._init(project_root)
        return self._store.get_grants_generation(project_root, session_id)

    def get_turn_counter(self, project_root: Path, session_id: str) -> int:
        self._init(project_root)
        return self._store.get_turn_counter(project_root, session_id)

    def set_pending_confirmation(
        self,
        project_root: Path,
        session_id: str,
        confirmation: dict[str, Any] | None,
    ) -> None:
        self._init(project_root)
        self._store.set_pending_confirmation(
            project_root,
            session_id,
            confirmation,
        )

    def get_pending_confirmation(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, Any] | None:
        self._init(project_root)
        return self._store.get_pending_confirmation(
            project_root,
            session_id,
        )

    def set_last_confirmed_operation(
        self,
        project_root: Path,
        session_id: str,
        confirmation: dict[str, Any] | None,
    ) -> None:
        self._init(project_root)
        self._store.set_last_confirmed_operation(
            project_root,
            session_id,
            confirmation,
        )

    def get_last_confirmed_operation(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, Any] | None:
        self._init(project_root)
        return self._store.get_last_confirmed_operation(
            project_root,
            session_id,
        )

    def add_turn_edited_file(
        self,
        project_root: Path,
        session_id: str,
        canonical_path: str,
    ) -> bool:
        self._init(project_root)
        return self._store.add_turn_edited_file(project_root, session_id, canonical_path)

    def remove_turn_edited_file(
        self,
        project_root: Path,
        session_id: str,
        canonical_path: str,
    ) -> bool:
        """A fresh read of the file re-enables line-edit mode for it (current line
        numbers). Returns True if the file was in the turn-edited set + removed."""
        self._init(project_root)
        return self._store.remove_turn_edited_file(project_root, session_id, canonical_path)

    def clear_turn_edited_files(self, project_root: Path, session_id: str) -> None:
        self._init(project_root)
        self._store.clear_turn_edited_files(project_root, session_id)

    def set_current_task_id(
        self,
        project_root: Path,
        session_id: str,
        task_id: str,
    ) -> None:
        """Audit-stamp hook: task_begin writes, task_complete clears (empty)."""
        self._init(project_root)
        self._store.set_current_task_id(project_root, session_id, task_id)

    def get_current_task_id(
        self,
        project_root: Path,
        session_id: str,
    ) -> str:
        """Audit-stamp hook: record_event reads this to stamp events."""
        self._init(project_root)
        return self._store.get_current_task_id(project_root, session_id)

    def get_turn_edited_files(self, project_root: Path, session_id: str) -> list[str]:
        self._init(project_root)
        return self._store.get_turn_edited_files(project_root, session_id)

    # Force-wakeup passthroughs REMOVED 2026-04-30 (autowake removal):
    # stamp_autowake / get_last_autowake. The compaction-grace passthroughs
    # (stamp_compaction / get_last_compaction) followed 2026-07-12 — the earlier
    # "used by session-freeze recovery grace" note was wrong; a reference check
    # found NO reader of last_compaction_at, so the stamp was a dead write left
    # over from the same force-wakeup workaround. Underlying columns
    # (last_autowake_at, last_compaction_at) are kept for migration safety.

    # ── Config-set grant passthroughs (2026-04-21) ──

    def set_config_grants(
        self,
        project_root: Path,
        session_id: str,
        grants: dict[str, object],
    ) -> None:
        self._init(project_root)
        self._store.set_config_grants(project_root, session_id, grants)

    def get_config_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        self._init(project_root)
        return self._store.get_config_grants(project_root, session_id)

    # ── DNT grant axes passthroughs (2026-05-12) — cross-process ──

    def set_protect_grants(
        self,
        project_root: Path,
        session_id: str,
        paths: list[str],
    ) -> None:
        self._init(project_root)
        self._store.set_protect_grants(project_root, session_id, paths)

    def get_protect_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        self._init(project_root)
        return self._store.get_protect_grants(project_root, session_id)

    def set_unprotect_grants(
        self,
        project_root: Path,
        session_id: str,
        paths: list[str],
    ) -> None:
        self._init(project_root)
        self._store.set_unprotect_grants(project_root, session_id, paths)

    def get_unprotect_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        self._init(project_root)
        return self._store.get_unprotect_grants(project_root, session_id)

    def set_protected_edit_grants(
        self,
        project_root: Path,
        session_id: str,
        paths: list[str],
    ) -> None:
        self._init(project_root)
        self._store.set_protected_edit_grants(project_root, session_id, paths)

    def get_protected_edit_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        self._init(project_root)
        return self._store.get_protected_edit_grants(project_root, session_id)

    # ── Fresh-host detection passthroughs (2026-04-21; renamed
    # 2026-05-01 to canonical host_session_id per identity contract) ──

    def check_and_update_host_session_id(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str,
    ) -> bool:
        self._init(project_root)
        return self._store.check_and_update_host_session_id(
            project_root,
            session_id,
            host_session_id,
        )

    def get_requires_reconnect(
        self,
        project_root: Path,
        session_id: str,
    ) -> bool:
        self._init(project_root)
        return self._store.get_requires_reconnect(project_root, session_id)

    def clear_requires_reconnect(
        self,
        project_root: Path,
        session_id: str,
    ) -> None:
        self._init(project_root)
        self._store.clear_requires_reconnect(project_root, session_id)

    # ── SEC-001 hotfix passthroughs (2026-04-23) ──
    #
    # Covers both sqlite column state AND the sticky-grants sidecar
    # JSON so a blocked prompt that contained a sticky-revoke phrase
    # leaves sticky state untouched too. Sidecar path:
    # .MEMORY/sessions/<session-id>/sticky-grants.json — separate
    # from config, doesn't interfere with it, but must be rolled
    # back alongside the sqlite row.


    def delete_prompt_submit_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> None:
        """Delete query authority for a session minted inside this submit."""
        self._init(project_root)
        self._store.delete_prompt_submit_state(project_root, session_id)
        try:
            self._sticky_path(project_root, session_id).unlink()
        except FileNotFoundError:
            pass


    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Capture query-gate authority with explicit absent/error semantics."""
        self._init(project_root)
        snapshot = dict(
            self._store.snapshot_prompt_submit_state(project_root, session_id)
        )
        sticky_path = self._sticky_path(project_root, session_id)
        state = snapshot.get("state")
        if not isinstance(state, dict):
            raise RuntimeError("query-gate prompt-submit snapshot has invalid state")
        snapshot["state"] = {
            **state,
            "__sticky_grants": list(self._load_sticky(project_root, session_id)),
            "__sticky_grants_existed": sticky_path.exists(),
        }
        return snapshot

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        session_id: str,
        snapshot: dict[str, object],
    ) -> None:
        """Restore the scoped query row and sticky sidecar exactly."""
        self._init(project_root)
        raw_state = snapshot.get("state")
        if not isinstance(raw_state, dict):
            raise ValueError("query-gate prompt-submit snapshot has invalid state")
        state = dict(raw_state)
        sticky = state.pop("__sticky_grants", [])
        sticky_existed = bool(state.pop("__sticky_grants_existed", False))
        row_snapshot = {**snapshot, "state": state}
        self._store.restore_prompt_submit_state(
            project_root,
            session_id,
            row_snapshot,
        )
        sticky_path = self._sticky_path(project_root, session_id)
        if sticky_existed:
            self._write_sticky(
                project_root,
                session_id,
                [str(token) for token in sticky] if isinstance(sticky, list) else [],
                source="prompt_submit_rollback",
            )
        else:
            try:
                sticky_path.unlink()
            except FileNotFoundError:
                pass


    def snapshot_privilege_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        self._init(project_root)
        snap = dict(self._store.snapshot_privilege_state(project_root, session_id))
        # Fold sticky grants into the same snapshot dict under a
        # reserved key. Restore reads this key back out.
        snap["__sticky_grants"] = list(self._load_sticky(project_root, session_id))
        return snap

    def restore_privilege_state(
        self,
        project_root: Path,
        session_id: str,
        snapshot: dict[str, object],
    ) -> None:
        self._init(project_root)
        # Split the sidecar payload off before handing the dict to
        # the store (which only knows about sqlite columns).
        sticky_snapshot = snapshot.pop("__sticky_grants", None)
        try:
            self._store.restore_privilege_state(
                project_root,
                session_id,
                snapshot,
            )
        finally:
            # Restore sidecar even if column restore raised. Sticky
            # state is operator-authored and must not diverge from
            # the pre-prompt snapshot on block.
            if isinstance(sticky_snapshot, list):
                try:
                    self._write_sticky(
                        project_root,
                        session_id,
                        [str(t) for t in sticky_snapshot],
                        source="snapshot_restore",
                    )
                except Exception:
                    pass

    # ── User-intent credential passthroughs (2026-04-21) ──

    def set_user_intent_credentials(
        self,
        project_root: Path,
        session_id: str,
        tokens: list[str],
    ) -> None:
        self._init(project_root)
        self._store.set_user_intent_credentials(
            project_root,
            session_id,
            tokens,
        )

    def set_user_intent_destructive(
        self,
        project_root: Path,
        session_id: str,
        tokens: list[str],
    ) -> None:
        """Turn-scoped destructive-intent tokens (Phase 4 of #15).
        claude_hook writes on UserPromptSubmit; orchestrator reads at
        judge-block time to decide ask vs hard-deny.
        """
        self._init(project_root)
        self._store.set_user_intent_destructive(
            project_root,
            session_id,
            tokens,
        )

    def get_user_intent_destructive(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        self._init(project_root)
        return self._store.get_user_intent_destructive(
            project_root,
            session_id,
        )

    def get_user_intent_credentials(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        self._init(project_root)
        return self._store.get_user_intent_credentials(
            project_root,
            session_id,
        )

    def set_dev_ai_run_bash_path(
        self,
        project_root: Path,
        session_id: str,
        path: str,
        *,
        actor: str = "",
    ) -> dict[str, object]:
        """Dev-flavor session-scoped ai_run shell override (Batch A
        shell-provider-lock). Setter validates flavor='dev' + path
        absolute before storing; non-dev installs get a refusal dict.
        """
        self._init(project_root)
        return self._store.set_dev_ai_run_bash_path(
            project_root,
            session_id,
            path,
            actor=actor,
        )

    def get_dev_ai_run_bash_path(
        self,
        project_root: Path,
        session_id: str,
    ) -> str:
        """Read the dev-flavor session-scoped ai_run override; returns
        '' on non-dev installs, missing rows, or empty column.
        """
        self._init(project_root)
        return self._store.get_dev_ai_run_bash_path(
            project_root,
            session_id,
        )

    def get(self, project_root: Path, session_id: str) -> dict[str, Any]:
        self._init(project_root)
        state: dict[str, Any] = dict(self._store.get(project_root, session_id))
        state["path"] = str(self.gate_path(project_root, session_id))
        # Surface the sticky-grant set so dashboard_snapshot (which reads
        # through query_gate.get) can render a per-session "Sticky grants"
        # panel without touching the underlying sqlite schema.
        state["sticky_user_intent_tools"] = self._load_sticky(project_root, session_id)
        # Lane identity: worker subprocesses carry their lane_id in an env
        # var set by agent_expert_service at spawn time. The env overrides
        # the sqlite column so concurrent workers never see each other's
        # lane state through the shared session row. The conductor never
        # sets the env var and still reads whatever the sqlite column
        # has (which is always None under the new contract — set_lane_scope
        # stopped writing it on 2026-04-19).
        import os as _os_env_read

        worker_lane = _os_env_read.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()
        worker_session = _os_env_read.environ.get("AIDOCS_EXPERT_SESSION_ID", "").strip()
        if worker_lane and (not worker_session or worker_session == session_id):
            state["current_lane_id"] = worker_lane
            # TRANSITIONAL — deprecated by session-lane-agents-table
            # spec. That migration replaces the single-row session_query_
            # gate with `(session_id, lane_id, worker_id)` rows, at
            # which point every worker reads its own scope from sqlite
            # directly and the env-var dance goes away. Until then,
            # lane_exact_paths must be carried in env for the same
            # reason current_lane_id is: parallel dispatches clobber
            # the shared column and the worker that actually runs sees
            # a sibling's files instead of its own.
            import json as _json_env_read

            worker_files_raw = _os_env_read.environ.get("AIDOCS_EXPERT_LANE_FILES", "").strip()
            if worker_files_raw:
                try:
                    parsed = _json_env_read.loads(worker_files_raw)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, list):
                    state["lane_exact_paths"] = [
                        str(p).replace("\\", "/").strip() for p in parsed if str(p).strip()
                    ]
            # WAR D (#452): lane_allowed_tools follows the SAME env-override
            # pattern. The spawner stamps the plan-declared toolset into
            # AIDOCS_EXPERT_LANE_ALLOWED (agent_expert_service); without
            # this override a parallel sibling dispatch clobbers the shared
            # sqlite column and check_lane_tool enforces the WRONG lane's
            # declared set for this worker.
            worker_tools_raw = _os_env_read.environ.get(
                "AIDOCS_EXPERT_LANE_ALLOWED", "",
            ).strip()
            if worker_tools_raw:
                try:
                    parsed_tools = _json_env_read.loads(worker_tools_raw)
                except (ValueError, TypeError):
                    parsed_tools = None
                if isinstance(parsed_tools, list):
                    state["lane_allowed_tools"] = [
                        str(t).strip() for t in parsed_tools if str(t).strip()
                    ]
        return state

    def get_last_host_session_id(
        self,
        project_root: Path,
        session_id: str,
    ) -> str:
        """Return last_host_session_id stamped for this session, or "".

        Public wrapper around SessionQueryGateStore.get_last_host_session_id.
        Used by session_connect / conductor_mode_enter / _resolve_task_session
        to recover host identity when MCP-server global is empty.
        """
        self._init(project_root)
        return self._store.get_last_host_session_id(project_root, session_id)

    def record_host_session_ids(
        self,
        project_root: Path,
        session_id: str,
        ids: list[str] | tuple[str, ...],
    ) -> bool:
        """#464: merge authenticated host/transcript ids into the session's
        owned host-id chain. Public wrapper around
        SessionQueryGateStore.record_host_session_ids."""
        self._init(project_root)
        return self._store.record_host_session_ids(project_root, session_id, ids)

    def get_host_session_id_chain(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        """#464: every host/harness id this session has legitimately owned."""
        self._init(project_root)
        return self._store.get_host_session_id_chain(project_root, session_id)

    def set(
        self,
        project_root: Path,
        session_id: str,
        *,
        last_tool: str | None = None,
        known_exact_paths: Any = _KEEP,
        current_lane_id: Any = _KEEP,
        lane_exact_paths: Any = _KEEP,
        lane_allowed_tools: Any = _KEEP,
        lane_extra_tools: Any = _KEEP,
        lane_raw_tools_granted: Any = _KEEP,
    ) -> dict[str, Any]:
        self._init(project_root)
        state: dict[str, Any] = dict(
            self._store.set(
                project_root,
                session_id,
                last_tool=last_tool,
                known_exact_paths=known_exact_paths,
                current_lane_id=current_lane_id,
                lane_exact_paths=lane_exact_paths,
                lane_allowed_tools=lane_allowed_tools,
                lane_extra_tools=lane_extra_tools,
                lane_raw_tools_granted=lane_raw_tools_granted,
            ),
        )
        state["path"] = str(self.gate_path(project_root, session_id))
        return state
