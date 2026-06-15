"""Host-agnostic prompt mutation service (UserPromptSubmit pipeline).

Owns the UPS pipeline contract: every host (Claude Code, OpenCode,
future Codex adapter) translates its prompt-submit event into a
``PromptMutator.mutate_prompt(payload, project_root)`` call and
renders the resulting ``PromptMutationResult`` into its envelope
shape.

**This is an incremental extraction**, not a clean-room rewrite.

Per audit (AIDOCS RFC 0001 §1.3 hub plumbing): the UPS law in
``claude_hook.py`` spans ~22 distinct decision points across lines
828–1969. Migrating them all in one commit is impractical and
risks behavior drift. Strategy:

  1. This module defines the **contract** — the ``PromptMutationResult``
     shape and the ``mutate_prompt`` entry point — which is the
     stable seam all hosts target.
  2. Sub-pipelines extract one at a time. Each extraction:
       - moves the logic into a method here,
       - claude_hook delegates that one section,
       - parity tests pin the contract,
       - the rest of claude_hook stays unchanged until its turn.

The completion bar (matches /goal): "no host-specific file contains
authoritative law, only envelope translation, and tests prove
identical behavior across host adapters." We are working toward
that bar, one sub-pipeline at a time.

## Extraction status

Hosts call ``mutate_prompt`` or individual sub-pipelines and
render the result. The contract distinguishes:
  - ``decision="block"``           — refuse the prompt
  - ``rewritten_prompt`` set       — replace the user's text
  - ``additional_context_blocks``  — append context, prompt unchanged

Migrated to this service (host-agnostic):
  - notifications_drain          — run_notifications + lane_completion_reviews
  - dashboard_config_advisory    — config-set grant advisory under
                                   security.allow_config_edit=false
  - resolve_session_freeze       — confirmation-freeze single-turn resolution
  - escalation_scrub             — strips approve:/deny: lines from prompt
                                   and flips pending escalation rows
  - prompt_secret_block          — block when prompt contains credential
                                   tokens AND security.prompt_secret_policy
                                   ='block' (the default)
  - preflight_judge              — hostile-prompt judge with degraded-path
                                   fail-closed and audit emission
  - apply_per_turn_intent_state  — 4 coupled per-turn state mutations:
                                   bash subcommand grants (per-turn ∪ sticky),
                                   ask-state plumbing (turn counter +
                                   yes/no resolution), credential token
                                   stash, destructive intent token stash
  - worker_lane_intercept        — lane-worker mailbox swap + protocol
                                   reminder injection (replaces wake-prompt
                                   with conductor instructions)
  - apply_config_set_grants      — per-turn config_set grant detection +
                                   stash to query_gate.config_grants
  - intent_phrase_dispatch       — closed-vocabulary intent detection
                                   (plan_session_enter etc.) + dispatch
  - apply_lane_exit_grant        — conductor lane-exit escape hatch
                                   (phrase OR sticky auto-exit when no
                                   live worker); env-gated against
                                   worker self-escape
  - record_user_prompt_received  — universal UPS audit event + fresh-CLI
                                   detection (raises requires_reconnect
                                   when host session UUID changes)
  - apply_dnt_grants             — DO-NOT-TOUCH grants (protect /
                                   unprotect / edit-override) via the
                                   NLP dnt_detector + tone_consumer.
                                   Writes to both protected_file_runtime
                                   (per-process) AND query_gate
                                   (cross-process sqlite).
  - apply_sticky_grant_lifecycle — clear_sticky_grants (revoke phrase),
                                   scoped revoke_tool, clear_expired,
                                   clear_turn_edited
  - auto_bind_session            — self-heal rebind to most-recent
                                   active session when managed_mode
                                   is inactive on UPS
  - consume_sticky_grant_answers — yes/no resolution on prior-turn
                                   pending sticky grants
  - apply_user_intent_tool_grants — per-turn vs sticky user-intent
                                    raw-tool grants with registration
                                    judge

Pending migration (still inside claude_hook.py): all major
sub-pipelines extracted. The remaining UPS logic in claude_hook
is envelope translation, SEC-001/002 snapshot/restore (transactional
wrapper around the mutations the service performs), and the route
classification block (which lives in runtime, not the hook).
  - SEC-001/002 snapshot+restore
  - sticky-grant lifecycle (revoke / clear_expired / consume / grant)
  - bash subcommand grants
  - ask-state plumbing (turn counter, pending_confirmation)
  - credentials + destructive intent stash
  - DNT protect/unprotect/edit-override grants
  - lane-exit escape hatch
  - intent-phrase detector dispatch
  - self-heal auto-bind on inactive managed_mode
  - route.blocked_reason → block

These will move in subsequent commits. Tests in
`mcp/tests/host/test_prompt_mutator_parity.py` pin migrated
sections; further tests land alongside each migration.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Deterministic DNT authority grammar (doctrine split 2026-06-03)
# ---------------------------------------------------------------------------
# DNT protect/unprotect authority is minted ONLY from this closed, auditable,
# NLP-FREE literal grammar — no NLPService, no spaCy, on the UPS hot path. Soft
# NLP signals (inflected verbs, tone/rage, dep-parse) no longer mint authority.
#
#   protect   ← any multilingual DNT_GATE_PHRASE (literal substring, whole prompt)
#             OR a CLAUSE that BEGINS (after optional approved polite prefixes)
#               with a protect keyword, then a clause-local path OR explicit "all"
#   unprotect ← a CLAUSE beginning with an unprotect keyword + clause-local
#               path/all
#
# CLAUSE-LOCAL + START-ANCHORED is the auditability seal: a command must START
# its clause, so incidental mentions ("I should protect X"), quoted/doc text,
# hypotheticals ("if you protect X"), and negated forms ("do not protect X",
# "don't unlock X" — they begin with a negator, not the keyword) mint NOTHING.
# Targets are taken only from the keyword's own clause, so an unrelated path in a
# neighbouring clause ("protect a.py but read b.log") cannot leak into the grant.
# Ambiguous "release" is deliberately NOT an unprotect keyword. Word boundaries +
# keyword-specific clause regexes keep "lock" out of "unlock", "blocca" out of
# "sblocca". The single shared path extractor lives in dnt_paths (no mirror).
_DNT_POLITE = r"(?:please|kindly|pls|bitte|svp|per\s+favore|por\s+favor|te\s+rog)"
_DNT_PROTECT_CMD = re.compile(
    rf"^\s*(?:{_DNT_POLITE}\s+)*(?:protect|lock|seal|guard|shield|proteggi|blocca)\b",
    re.IGNORECASE,
)
_DNT_UNPROTECT_CMD = re.compile(
    rf"^\s*(?:{_DNT_POLITE}\s+)*(?:unprotect|unlock|sproteggi|sblocca)\b",
    re.IGNORECASE,
)
# Path-token shape comes from ONE shared source (dnt_paths.PATH_TOKEN). Both the
# clause-split lookahead and the immediate target-list grammar derive from it.
from .aidocs_nlp.consumers.dnt_paths import PATH_TOKEN as _DNT_PATH

# Immediate explicit target grammar (anchored at the position right after a
# matched keyword/gate phrase — NEVER a whole-clause scan):
#   * explicit "all" family -> "*";
#   * otherwise a path LIST: one path token, then zero or more (comma | and | or)
#     separated path tokens. The first non-target token ends the list.
_DNT_ALL_IMM = re.compile(r"^\s*(?:all|everything|every\s+file|tutto)\b", re.IGNORECASE)
_DNT_TARGET_LIST = re.compile(
    rf"^\s*(?:{_DNT_PATH})(?:(?:\s*,\s*|\s+(?:and|or)\s+)(?:{_DNT_PATH}))*",
    re.IGNORECASE,
)
# Clause boundaries:
#   * `;!?` and newlines always terminate; `.` only when followed by whitespace
#     /end (so it never splits a file path like config.py / src/auth.py);
#   * but/then/while/so/because/however/nor always start an independent clause;
#   * `and`/`or` split ONLY when NOT immediately followed by a path token — so
#     "protect a.py and b.py" keeps both (continuation list) while
#     "protect a.py and read b.py" / "... and protect c.py" split into clauses.
_DNT_CLAUSE_SPLIT = re.compile(
    r"[;!?\n]+"
    r"|\.(?=\s|$)"
    r"|\s+(?:but|then|while|so|because|however|nor)\s+"
    rf"|\s+(?:and|or)\s+(?!{_DNT_PATH})",
    re.IGNORECASE,
)

# Clause-start gate-phrase matcher (built lazily; cached). Anchored after
# optional polite prefixes so a quoted/incidental gate phrase ('the README says
# "do not touch"') does NOT begin its clause. A trailing (?!\w) exact boundary
# keeps "do not touching" from matching the "do not touch" idiom.
_DNT_GATE_RE: "re.Pattern[str] | None" = None


def _dnt_gate_re() -> "re.Pattern[str]":
    global _DNT_GATE_RE
    if _DNT_GATE_RE is None:
        from .aidocs_nlp.semantic_dict import DNT_GATE_PHRASES

        alt = "|".join(re.escape(p) for p in sorted(DNT_GATE_PHRASES, key=len, reverse=True))
        _DNT_GATE_RE = re.compile(rf"^\s*(?:{_DNT_POLITE}\s+)*(?:{alt})(?!\w)", re.IGNORECASE)
    return _DNT_GATE_RE


def _bind_immediate_targets(rest: str) -> set[str]:
    """Parse ONLY the explicit target grammar immediately after a matched
    keyword/gate phrase. Returns the granted set ("*" for the all-family, the
    listed paths for a path list), or an empty set when no target immediately
    follows. Never scans past the first non-target token."""
    from .aidocs_nlp.consumers.dnt_paths import extract_dnt_paths

    if _DNT_ALL_IMM.match(rest):
        return {"*"}
    m = _DNT_TARGET_LIST.match(rest)
    if m:
        return extract_dnt_paths(m.group(0))
    return set()


def _literal_dnt_grants(prompt: str) -> tuple[set[str], set[str]]:
    """Deterministic, fully clause-local literal DNT grant parser. Returns
    (protect, unprotect) path sets. NLP-free; never constructs NLPService.

    A gate phrase or a protect/unprotect keyword MUST begin its clause (after
    optional approved polite prefixes) to be a command, and its target must
    IMMEDIATELY follow (explicit "all" family, or a comma/and/or path list) — the
    parser never scans the rest of the clause for a later path or a later "all".

    A protect/unprotect keyword with no immediate target mints nothing. A gate
    phrase with no immediate target yields "*" (bare standalone compat preserved).
    Quoted/incidental/hypothetical/negated forms begin no command clause and mint
    nothing; targets never leak across clauses.
    """
    text = prompt or ""
    protect: set[str] = set()
    unprotect: set[str] = set()
    gate_re = _dnt_gate_re()
    for clause in _DNT_CLAUSE_SPLIT.split(text):
        if not clause or not clause.strip():
            continue
        # Gate phrase as a clause-local direct command (protect axis).
        mg = gate_re.match(clause)
        if mg is not None:
            tgt = _bind_immediate_targets(clause[mg.end():])
            protect |= (tgt if tgt else {"*"})
            continue
        mu = _DNT_UNPROTECT_CMD.match(clause)
        mp = None if mu else _DNT_PROTECT_CMD.match(clause)
        m = mu or mp
        if m is None:
            continue
        # Keyword command: an immediate target is REQUIRED; none -> mint nothing.
        target = unprotect if mu else protect
        target |= _bind_immediate_targets(clause[m.end():])
    return protect, unprotect


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptMutationResult:
    """Host-agnostic UPS mutation output.

    Hosts translate this into their envelope shape:
      - Claude Code → hookSpecificOutput with decision/additionalContext
      - OpenCode    → chat.messages.transform / sessionPromptContext
      - OpenAI Agents → system message injection (when UPS hook exists)

    ``decision`` is the gate verdict:
      - "allow"  — proceed with prompt (possibly rewritten).
      - "block"  — refuse with ``block_reason``. Host renders host-
                   specific block envelope (decision=block / throw /
                   raise depending on shape).

    ``rewritten_prompt`` is non-None when the pipeline mutated the
    prompt (e.g. worker mailbox injection, plan continuation).
    Hosts replace the user's text with this when set.

    ``additional_context_blocks`` is a list of strings hosts append
    to context. Joined with ``\\n\\n``. Each block is a self-
    contained advisory or notification.

    ``audit_events`` is a list of (event_kind, payload) tuples the
    host (or the pipeline itself) writes to execution_events.

    ``side_effects`` is a list of human-readable notes about
    non-audit state changes (e.g. "cleared sticky grants",
    "minted scoped grant for tool=X"). Used in tests and the
    operator-facing dashboard.

    ``why`` tags which sub-pipelines fired during this mutation.
    Empty when the prompt passed through without any sub-pipeline
    actually emitting output.
    """

    decision: str = "allow"  # "allow" | "block"
    block_reason: str | None = None
    rewritten_prompt: str | None = None
    additional_context_blocks: tuple[str, ...] = ()
    audit_events: tuple[tuple[str, dict], ...] = ()
    side_effects: tuple[str, ...] = ()
    why: tuple[str, ...] = ()

    def merge(self, other: PromptMutationResult) -> PromptMutationResult:
        """Compose two mutation results in pipeline order. A ``block``
        from either side wins; otherwise outputs accumulate. The
        rewritten_prompt of the later result wins if set.
        """
        if self.decision == "block":
            return self
        if other.decision == "block":
            return other
        return PromptMutationResult(
            decision="allow",
            block_reason=None,
            rewritten_prompt=(
                other.rewritten_prompt
                if other.rewritten_prompt is not None
                else self.rewritten_prompt
            ),
            additional_context_blocks=(
                self.additional_context_blocks + other.additional_context_blocks
            ),
            audit_events=self.audit_events + other.audit_events,
            side_effects=self.side_effects + other.side_effects,
            why=self.why + other.why,
        )

    @classmethod
    def empty(cls) -> PromptMutationResult:
        return cls()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PromptMutator:
    """Host-agnostic UPS pipeline.

    Bound to a runtime (so it can resolve hub.managed_mode,
    hub.query_gate, etc.) but stateless across calls — safe to
    instantiate fresh per inbound prompt event.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Top-level entry point (the canonical seam hosts call into)
    # ------------------------------------------------------------------

    def mutate_prompt(
        self,
        payload: dict,
        project_root: Path,
        *,
        worker_lane_id: str = "",
        worker_session_id: str = "",
        worker_id: str = "",
        is_worker_proc: bool = False,
        grant_eligible: bool | None = None,
    ) -> PromptMutationResult:
        """Canonical entry point for hosts WITHOUT a per-sub-pipeline
        transactional wrapper (host_adapter_cli, OpenAI Agents,
        future Codex adapters).

        Claude Code's hook does NOT call this — it invokes the
        individual sub-pipeline methods one-by-one with a SEC-001/002
        snapshot/restore wrapper around each, then calls
        ``dashboard_config_advisory`` and ``notifications_drain``
        directly for the tail. Calling ``mutate_prompt`` from CC
        would double-fire every sub-pipeline. The dedup invariant
        is pinned by ``test_canonical_entry_point_parity
        .TestNoDoubleFireFromCC``.

        Runs every migrated UPS sub-pipeline
        in the order claude_hook's _handle_user_prompt_submit used to
        invoke them, accumulating results via ``.merge()``. Any sub-
        pipeline that returns ``decision="block"`` short-circuits the
        rest.

        Pipeline order (matches claude_hook's original sequence):

          1. record_user_prompt_received  — audit + fresh-CLI
          2. prompt_secret_block          — credential token block
          3. preflight_judge              — hostile-prompt block / freeze
          4. worker_lane_intercept        — mailbox + protocol injection
          5. resolve_session_freeze       — confirmation freeze resolution
          6. escalation_scrub             — strip approve:/deny: lines
          7. apply_sticky_grant_lifecycle — revoke / clear
          8. consume_sticky_grant_answers — yes/no on prior pending
          9. apply_user_intent_tool_grants — raw-tool grants + Layer-2
         10. apply_per_turn_intent_state — bash subs / ask-state / creds /destr
         11. apply_dnt_grants             — DO-NOT-TOUCH grants
         12. apply_config_set_grants      — config_set per-turn grants
         13. apply_lane_exit_grant        — conductor lane-exit escape
         14. intent_phrase_dispatch       — plan_session_enter etc.
         15. auto_bind_session            — self-heal rebind (when route says unmanaged)
         16. dashboard_config_advisory    — informational
         17. notifications_drain          — run_notifications + lane reviews

        Caller passes worker identity kwargs so ``worker_lane_intercept``
        and the worker fence in notifications_drain see the same env-
        derived state. Hosts that aren't worker subprocesses pass the
        defaults (all empty / False).

        Pipelines that need the managed_session_id resolve it inside;
        callers don't have to pre-thread it.

        ORIGIN-BOUND LAW: ``grant_eligible`` (computed by the caller from
        the prompt-origin gate, is_authority_bearing_prompt_eligible)
        controls whether the AUTHORITY-BEARING pipelines (steps 5-14:
        freeze resolution, escalation scrub, sticky/user-intent grants,
        per-turn intent state, DNT/config-set grants, lane-exit,
        intent-phrase dispatch) run. When False — a worker / -p / -q /
        delegated / compaction / handoff / replay / tool prompt — those
        are SKIPPED entirely. Prompt shape is never authority.

        ``grant_eligible`` DEFAULTS FAIL-CLOSED: omitting it (None) is
        treated as False, and a ``grant_eligible_unset_failed_closed``
        why-tag is emitted. Every host MUST explicitly pass True only
        after verifying a direct-human origin. No silent fail-open.

        INVARIANT: No grant detector — even the advisory-only
        ``dashboard_config_advisory`` (step 16, which runs
        detect_config_grants_v2) — runs on an ineligible prompt origin.
        Step 16 is AUTHORITY-ADJACENT and gated on grant_eligible; only
        ``notifications_drain`` (step 17) is ALWAYS-SAFE informational.
        """
        prompt = str(payload.get("prompt") or "")
        host_session_id = str(payload.get("session_id") or "")
        result = PromptMutationResult.empty()

        # FAIL-CLOSED: a caller that omits grant_eligible gets NO
        # authority-bearing pipelines. Only an explicit True (after the
        # caller verified a direct-human origin) unlocks grants. The
        # why-tag makes the fail-closed default visible in audit/debug.
        if grant_eligible is None:
            grant_eligible = False
            result = result.merge(
                PromptMutationResult(
                    why=("grant_eligible_unset_failed_closed",),
                ),
            )

        # Resolve managed_session_id once for the sub-pipelines that
        # need it. Each pipeline still tolerates missing session.
        managed_session_id = ""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=host_session_id,
            )
            if managed.get("active"):
                managed_session_id = str(managed.get("session_id") or "").strip() or ""
        except Exception:
            managed_session_id = ""

        # 1. UPS audit + fresh-CLI detection (always; side-effect)
        result = result.merge(
            self.record_user_prompt_received(
                prompt=prompt,
                host_session_id=host_session_id,
                project_root=project_root,
            ),
        )
        if result.decision == "block":
            return result

        # 2. Prompt-secret block (may refuse)
        result = result.merge(
            self.prompt_secret_block(
                prompt=prompt,
                project_root=project_root,
            ),
        )
        if result.decision == "block":
            return result

        # 3. Preflight judge (may refuse / degrade-closed)
        result = result.merge(
            self.preflight_judge(
                prompt=prompt,
                project_root=project_root,
            ),
        )
        if result.decision == "block":
            return result

        # 4. Worker-lane mailbox / protocol injection (rewrites prompt)
        if worker_lane_id:
            wlr = self.worker_lane_intercept(
                project_root=project_root,
                worker_lane_id=worker_lane_id,
                worker_session_id=worker_session_id,
                worker_id=worker_id,
            )
            result = result.merge(wlr)
            if wlr.rewritten_prompt is not None:
                # Worker intercept replaces the prompt outright; the
                # remaining pipelines should not also mutate it.
                return result

        # The state-mutation pipelines that key on managed_session_id
        # only run when one is bound AND the prompt origin is authority-
        # bearing (a verified direct-human submit). ORIGIN-BOUND LAW:
        # worker / -p / -q / delegated / compaction / handoff / replay /
        # tool prompts are INERT for grant/confirmation/mutation
        # consumption even if they contain an exact grant phrase. Steps
        # 1-4 (audit, secret-block, preflight, worker-intercept) and the
        # informational tail (16-17) ALWAYS run.
        if managed_session_id and grant_eligible:
            # 5. Session-freeze resolver (#39)
            result = result.merge(
                self.resolve_session_freeze(
                    prompt=prompt,
                    host_session_id=host_session_id,
                    project_root=project_root,
                ),
            )
            # 6. Escalation scrub (may rewrite prompt)
            scrub = self.escalation_scrub(
                prompt=prompt,
                project_root=project_root,
            )
            result = result.merge(scrub)
            if scrub.rewritten_prompt is not None:
                # Downstream sub-pipelines see the scrubbed prompt
                prompt = scrub.rewritten_prompt
            # 7. Sticky-grant lifecycle (revoke / clear)
            result = result.merge(
                self.apply_sticky_grant_lifecycle(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )
            # 8. Consume prior-turn sticky-grant answers
            result = result.merge(
                self.consume_sticky_grant_answers(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )
            # 9. User-intent raw-tool grants + Layer-2
            result = result.merge(
                self.apply_user_intent_tool_grants(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )
            # 10. Per-turn intent-state (bash subs / ask-state / creds /destr)
            result = result.merge(
                self.apply_per_turn_intent_state(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )
            # 11. DNT grants
            result = result.merge(
                self.apply_dnt_grants(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )
            # 12. Config-set grants
            result = result.merge(
                self.apply_config_set_grants(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )
            # 13. Lane-exit grant (env-fenced against worker self-escape)
            result = result.merge(
                self.apply_lane_exit_grant(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                    is_worker_proc=is_worker_proc,
                ),
            )
            # 14. Intent-phrase dispatch (plan_session_enter etc.)
            result = result.merge(
                self.intent_phrase_dispatch(
                    prompt=prompt,
                    managed_session_id=managed_session_id,
                    project_root=project_root,
                ),
            )

        # 16. Dashboard config-set advisory — AUTHORITY-ADJACENT
        # informational. It runs the config-grant SHAPE detector
        # (detect_config_grants_v2) on the prompt, so it is gated on the
        # SAME origin law: no grant detector — even advisory-only — runs
        # on an ineligible origin.
        if grant_eligible:
            result = result.merge(
                self.dashboard_config_advisory(
                    {"prompt": prompt},
                    project_root,
                ),
            )

        # 17. Notifications drain — ALWAYS-SAFE informational
        # (worker-fenced internally; no prompt-intent detection).
        result = result.merge(
            self.notifications_drain(
                {"prompt": prompt, "session_id": host_session_id},
                project_root,
            ),
        )

        return result

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: dashboard config advisory
    # ------------------------------------------------------------------

    def dashboard_config_advisory(
        self,
        payload: dict,
        project_root: Path,
    ) -> PromptMutationResult:
        """When the prompt contains a config-set grant phrase ("turn
        on X", "set Y to Z") and the dashboard gate
        ``security.allow_config_edit`` is OFF, inject an advisory
        explaining why config_set will refuse.

        Informational only — never blocks. Surfaces the dashboard
        truth before the agent wastes a turn attempting config_set.
        """
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip():
            return PromptMutationResult.empty()

        try:
            from .canonical_intent_registry import detect_config_grants_v2

            cfg_grants = detect_config_grants_v2(prompt)
        except Exception:
            return PromptMutationResult.empty()

        if not cfg_grants:
            return PromptMutationResult.empty()

        try:
            from .config import get_setting

            allow_cfg_edit = bool(
                get_setting(
                    "security.allow_config_edit",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            return PromptMutationResult.empty()

        if allow_cfg_edit:
            return PromptMutationResult.empty()

        keys = ", ".join(sorted(cfg_grants.keys())[:4])
        advisory = (
            f" [AIDOCS dashboard note] the prompt asks to change "
            f"config settings ({keys}) but the dashboard gate "
            f"`security.allow_config_edit` is OFF. The config_set "
            f"tool will refuse until the operator flips that toggle "
            f"in the dashboard. Acknowledge the request and explain "
            f"— do not attempt config_set."
        )
        return PromptMutationResult(
            decision="allow",
            additional_context_blocks=(advisory,),
            why=("dashboard_config_advisory",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: consume sticky grant answers
    # ------------------------------------------------------------------

    def consume_sticky_grant_answers(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Phase 3 of backlog #15: when the prior turn wrote pending
        sticky-grant rows, scan this prompt for affirmative/denial
        tokens and consume each pending row with the matched answer.

        Cheap whole-prompt heuristic — AskUserQuestion typically
        returns "yes" / "no" / "confirm" / "deny" literally. No NLP
        needed.

        Precedence: explicit "no" beats "yes" when both appear
        ("no thanks"). Absence of both → drop via TTL sweep.

        Best-effort: store failure returns empty.
        """
        if not managed_session_id:
            return PromptMutationResult.empty()
        try:
            from .sticky_grants_store import StickyGrantsStore

            sgs = StickyGrantsStore()
            pendings = sgs.list_pending(project_root, managed_session_id)
        except Exception:
            return PromptMutationResult.empty()
        if not pendings:
            return PromptMutationResult.empty()

        text = (prompt or "").strip().lower()
        is_yes = any(
            token in text
            for token in (
                "yes",
                "confirm",
                "approve",
                "register",
                "ok to grant",
            )
        )
        is_no = any(
            token in text
            for token in (
                " no",
                "deny",
                "reject",
                "cancel",
                "skip grant",
                "don't",
                "do not",
            )
        ) or text.strip() in ("no", "n")
        answer = "no" if is_no else ("yes" if is_yes else "")
        if not answer:
            try:
                sgs.clear_expired_pending(project_root, managed_session_id)
            except Exception:
                pass
            return PromptMutationResult(
                decision="allow",
                why=("sticky_answers_no_response",),
            )

        resolved: list[tuple[str, str]] = []
        for row in pendings:
            try:
                sgs.consume_pending(
                    project_root,
                    pending_id=str(row.get("pending_id") or ""),
                    answer=answer,
                )
                resolved.append(
                    (str(row.get("tool") or ""), answer),
                )
            except Exception:
                continue

        if not resolved:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            side_effects=tuple(f"sticky grant {answer}: {tool}" for tool, _ in resolved),
            why=("sticky_answers_resolved", answer),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: user-intent raw-tool grants
    # ------------------------------------------------------------------

    def apply_user_intent_tool_grants(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Detect explicit user intent for raw tools and grant for
        this turn (per-turn) or session (sticky).

        Three trigger axes (handled by detect_user_intent_tools_v2):
          (a) direct intent phrase ("grep for")
          (b) grant verb + tool token within proximity
          (c) bare imperative tool token at prompt start ("grep ...")

        Sticky-flag detection (``_detect_sticky_grant_flag``) decides
        between per-turn (cleared next prompt) and sticky (persists
        until session end). Sticky grants run through the
        registration judge:
          - refuse → silently dropped (operator sees refusal context)
          - require_confirm → write pending row, AskUserQuestion next turn
          - allow → apply sticky

        Layer-2 NLP tool surfacing: lemma-based deferred-MCP-tool
        grants accumulate session-wide (separate from raw-tool exec
        grants). Excludes raw exec tools (per-turn-only) and eager
        tools (already visible). Prompt-global negation suppresses
        all Layer-2 writes.

        Best-effort failures: judge unavailable → fail-open to
        today's behavior; NLP error → no Layer-2 writes.
        """
        if not managed_session_id:
            return PromptMutationResult.empty()

        try:
            from .canonical_intent_registry import (
                detect_sticky_grant_flag_v2,
                detect_user_intent_tools_v2,
            )
        except Exception:
            return PromptMutationResult.empty()

        qg = self.runtime.hub.query_gate
        text = (prompt or "").strip().lower()
        if not text:
            # Empty/whitespace prompt: explicit per-turn clear. This branch
            # is unreachable from the hook (it returns early on an empty
            # prompt) — kept for direct callers/unit tests, so the clear
            # write stays unconditional here.
            qg.set_user_intent_tools(
                project_root,
                managed_session_id,
                [],
            )
            return PromptMutationResult(
                decision="allow",
                why=("user_intent_grants_cleared",),
            )

        try:
            sticky_flag = bool(detect_sticky_grant_flag_v2(prompt))
            granted = set(detect_user_intent_tools_v2(prompt))
        except Exception:
            return PromptMutationResult.empty()

        # Registration judge for sticky grants
        denied_raw: list[str] = []
        pending_raw: list[tuple[str, str]] = []
        auto_allowed: set[str] = set(granted)
        if sticky_flag and granted:
            try:
                from .grant_registration_judge import (
                    evaluate_grant_registration,
                )
                from .sticky_grants_store import StickyGrantsStore

                sgs = StickyGrantsStore()
                auto_allowed = set()
                for tool in sorted(granted):
                    v = evaluate_grant_registration(
                        tier=1,
                        tool=tool,
                        phrase=prompt[:200],
                        project_root=project_root,
                    )
                    if v.decision == "refuse":
                        denied_raw.append(f"{tool}: {v.reason}")
                    elif v.decision == "require_confirm":
                        try:
                            sgs.record_pending(
                                project_root,
                                session_id=managed_session_id,
                                tier=1,
                                tool=tool,
                                phrase=prompt[:500],
                                judge_reason=v.reason,
                            )
                            pending_raw.append((tool, v.reason))
                        except Exception:
                            # FAIL CLOSED (2026-06-11, co-co review): a
                            # require_confirm tool whose pending row could NOT
                            # be written is NOT auto-allowed — persistence
                            # defaults deny. It stays ungranted; the operator
                            # re-issues. (Was: auto_allowed.add(tool).)
                            denied_raw.append(
                                f"{tool}: confirmation required but the pending "
                                f"store write failed — not granted (re-issue).",
                            )
                    else:
                        auto_allowed.add(tool)
            except Exception:
                # FAIL CLOSED (2026-06-11, co-co review): the registration
                # judge IS the validator; if it cannot run, grant NOTHING this
                # turn rather than auto-allowing every detected tool.
                # Validator-unavailable fails closed. (Was: set(granted).)
                auto_allowed = set()
                denied_raw.append(
                    "grant judge unavailable — no sticky grants registered "
                    "this turn (fail-closed).",
                )

        # SEC-002: user_intent_tools is the headline authority field — its
        # write must NOT be swallowed. A failure propagates to the caller's
        # atomic stage for snapshot-restore + degraded + audit.
        # Cleanliness: only write on a delta so a clean prompt (nothing
        # granted, nothing already stored) does not stamp an empty default.
        # Per-turn TTL is preserved — when a prior grant exists the resolved
        # set differs, so the clearing write still fires.
        _new_tools = sorted(auto_allowed)
        if _new_tools != list(qg.get_user_intent_tools(project_root, managed_session_id)):
            qg.set_user_intent_tools(
                project_root,
                managed_session_id,
                _new_tools,
                sticky=sticky_flag,
            )

        # Layer-2 NLP tool surfacing
        layer2_added = 0
        try:
            from .intent_grant_detector import detect_grant

            detection = detect_grant(prompt)
            _RAW_TOOLS = {"grep", "bash", "read", "edit", "write", "glob"}
            try:
                from .agent_orchestrator import is_eager_tool

                nlp_granted = {
                    t
                    for t in detection.granted_tools
                    if t not in _RAW_TOOLS and not is_eager_tool(t)
                }
            except Exception:
                nlp_granted = set(detection.granted_tools) - _RAW_TOOLS
            if nlp_granted and not detection.deny and not detection.has_negation:
                current_sticky = set(
                    qg.get_sticky_user_intent_tools(
                        project_root,
                        managed_session_id,
                    )
                    or [],
                )
                new_sticky = current_sticky | nlp_granted
                if new_sticky != current_sticky:
                    qg.add_sticky_user_intent_tools(
                        project_root,
                        managed_session_id,
                        sorted(nlp_granted),
                    )
                    layer2_added = len(nlp_granted - current_sticky)
                    try:
                        qg.bump_grants_generation(
                            project_root,
                            managed_session_id,
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        side_effects: list[str] = []
        why: list[str] = []
        if auto_allowed:
            side_effects.append(
                f"user_intent_tools set (count={len(auto_allowed)}, sticky={sticky_flag})",
            )
            why.append("user_intent_grants")
        if denied_raw:
            side_effects.append(f"sticky grants refused by judge: {len(denied_raw)}")
            why.append("user_intent_judge_denied")
        if pending_raw:
            side_effects.append(f"sticky grants pending confirm: {len(pending_raw)}")
            why.append("user_intent_judge_pending")
        if layer2_added:
            side_effects.append(f"Layer-2 NLP sticky additions: {layer2_added}")
            why.append("user_intent_layer2_added")

        if not why:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            side_effects=tuple(side_effects),
            why=tuple(why),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: self-heal auto-bind on inactive managed_mode
    # ------------------------------------------------------------------

    def auto_bind_session(
        self,
        *,
        project_root: Path,
    ) -> PromptMutationResult:
        """King directive 2026-05-03: any user message in an unbound
        state should auto-activate, not block. Rebind to the most
        recent active session before refusing.

        Returns:
          - empty when no active session exists (truly uninitialized
            project — operator must run /aidocs)
          - allow with side_effects + audit event when a session was
            auto-bound. The bound session_id is in
            ``audit_events[0][1]["bound_session_id"]`` so the caller
            can re-resolve managed_mode.

        Best-effort: any exception returns empty, caller's normal
        block-on-unmanaged path runs as before.

        """
        try:
            sessions = self.runtime.hub.sessions.list_sessions(project_root)
            active_sessions = [
                s
                for s in (sessions or [])
                if (
                    (s.get("status") if isinstance(s, dict) else getattr(s, "status", None))
                    == "active"
                )
            ]
            if not active_sessions:
                return PromptMutationResult.empty()
            bind_obj = active_sessions[0]
            bind_sid = str(
                bind_obj.get("session_id")
                if isinstance(bind_obj, dict)
                else getattr(bind_obj, "session_id", ""),
            ).strip()
            if not bind_sid:
                return PromptMutationResult.empty()
            self.runtime.hub.managed_mode.set_mode(
                project_root,
                session_id=bind_sid,
                source="user_prompt_submit_auto_activate",
            )
        except Exception:
            return PromptMutationResult.empty()

        # Audit the auto-bind so it's visible in the dashboard event
        # stream — not silent magic.
        audit_payload = {
            "trigger": "user_prompt_with_inactive_managed_mode",
            "bound_session_id": bind_sid,
        }
        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="managed_mode_auto_activated",
                source_kind="user_prompt_submit",
                session_id=bind_sid,
                capability_name="UserPromptSubmit",
                action_kind="auto_bind",
                status="activated",
                payload=audit_payload,
            )
        except Exception:
            pass

        return PromptMutationResult(
            decision="allow",
            side_effects=(f"managed_mode auto-bound to session={bind_sid}",),
            audit_events=(
                (
                    "managed_mode_auto_activated",
                    audit_payload,
                ),
            ),
            why=("auto_bind_session", bind_sid),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: sticky-grant lifecycle (revoke / clear)
    # ------------------------------------------------------------------

    def apply_sticky_grant_lifecycle(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Per-turn sticky-grant lifecycle.

        Four coupled operations on every UPS:
          1. Wholesale sticky revoke: when ``_detect_sticky_revoke_grant``
             fires (operator says "revoke sticky" or equivalent),
             ``query_gate.clear_sticky_grants`` drops the whole sticky
             slice.
          2. Scoped revokes: ``_detect_scoped_revoke_tools`` returns
             a set ({"bash", "opencode"}); ``StickyGrantsStore.revoke_tool``
             drops just those.
          3. ``clear_expired_grants`` — TTL sweep on sticky rows
             whose lifetime has expired.
          4. ``clear_turn_edited_files`` — reset the per-turn edited-file
             tracker (legacy gate read path).

        Each sub-op has independent try/except so one failure doesn't
        suppress the others. Returns side_effects naming what fired.
        """
        if not managed_session_id:
            return PromptMutationResult.empty()

        side_effects: list[str] = []
        why: list[str] = []
        qg = self.runtime.hub.query_gate

        # 1. Wholesale revoke
        try:
            from .canonical_intent_registry import detect_sticky_revoke_grant_v2

            if detect_sticky_revoke_grant_v2(prompt):
                qg.clear_sticky_grants(project_root, managed_session_id)
                side_effects.append("sticky_grants wholesale cleared")
                why.append("sticky_revoke_wholesale")
        except Exception:
            pass

        # 2. Scoped revokes
        try:
            from .canonical_intent_registry import detect_scoped_revoke_tools_v2

            scoped = detect_scoped_revoke_tools_v2(prompt)
            if scoped:
                from .sticky_grants_store import StickyGrantsStore

                sgs = StickyGrantsStore()
                for tool in scoped:
                    try:
                        sgs.revoke_tool(
                            project_root,
                            session_id=managed_session_id,
                            tool=tool,
                            revoked_reason="operator-scoped-revoke",
                        )
                    except Exception:
                        continue
                side_effects.append(f"sticky scoped revokes: {sorted(scoped)}")
                why.append("sticky_revoke_scoped")
        except Exception:
            pass

        # 3. Clear expired
        try:
            qg.clear_expired_grants(project_root, managed_session_id)
            why.append("sticky_expired_cleared")
        except Exception:
            pass

        # 4. Clear turn edited files
        try:
            qg.clear_turn_edited_files(project_root, managed_session_id)
            why.append("turn_edited_cleared")
        except Exception:
            pass

        if not why:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            side_effects=tuple(side_effects),
            why=tuple(why),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: DNT (DO-NOT-TOUCH) grants
    # ------------------------------------------------------------------

    def apply_dnt_grants(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Three DNT axes parsed from the current prompt:
          - protect_paths   — files the operator wants protected
          - unprotect_paths — files the operator wants un-protected
          - edit_paths      — specific edit-override grants

        All cleared on each UPS and re-populated here.

        NLP-first detection (king doctrine 2026-05-12) via
        ``aidocs_nlp.consumers.dnt_detector`` + ``tone_consumer``
        (rage = blanket protect grant). When no language pack is
        loaded, NLP returns None — we degrade silently rather than
        block. en_core_web_sm bundles with AIDOCS so the NLP path
        runs on every install.

        Edit-override grants come from the legacy
        ``_detect_protected_edit_overrides`` (regex + closed
        vocabulary).

        Writes happen on TWO surfaces:
          1. ``protected_file_runtime`` module-level (per-process,
             used by tests + secondary read path when no managed
             session is active)
          2. ``query_gate.set_*_grants`` sqlite (authoritative
             cross-process source; #236 2026-05-12)

        Also resets ``set_turn_read_files([])`` so the forced-read
        tracker starts each turn empty.
        """
        # Per-process module-level writes always happen (they're the
        # secondary path used by tests + when no managed session).
        try:
            from .protected_file_runtime import (
                set_protect_grants,
                set_protected_edit_grants,
                set_turn_read_files,
                set_unprotect_grants,
            )
        except Exception:
            return PromptMutationResult.empty()

        # DNT authority is minted ONLY by the deterministic, auditable literal
        # parser — NO NLP, NO NLPService/spaCy construction on UPS (doctrine split
        # 2026-06-03). Free-form/inflected verbs, tone/rage, and dep-parse signals
        # NO LONGER mint authority; only the closed literal grammar (gate phrases +
        # explicit protect/unprotect keyword + path/all, negation-refused, no
        # ambiguous 'release') writes grants.
        protect_paths_set, unprotect_paths_set = _literal_dnt_grants(prompt)

        # Edit-override grants — canonical detector with the historic
        # 20-path cap baked in (see _PROTECT_CAP migration 2026-05-27).
        try:
            from .canonical_intent_registry import (
                detect_protected_edit_overrides_v2,
            )

            edit_paths_set = set(detect_protected_edit_overrides_v2(prompt))
        except Exception:
            edit_paths_set = set()

        protect_paths = sorted(protect_paths_set)
        unprotect_paths = sorted(unprotect_paths_set)
        edit_paths = sorted(edit_paths_set)

        # Per-process writes
        try:
            set_protect_grants(protect_paths)
            set_unprotect_grants(unprotect_paths)
            set_protected_edit_grants(edit_paths)
            set_turn_read_files([])
        except Exception:
            pass

        # Sqlite writes — only when a managed session is bound.
        # Persist-until-consumed (operator directive 2026-06-11): a
        # path-specific protect/unprotect grant survives ACROSS turns
        # until protect_file/unprotect_file actually consumes it — the
        # user said "protect X" and the obligation stands until X is
        # protected, not until they happen to send another message.
        # The blanket '*' grant stays per-turn (it names no file, so
        # there is no completion event to consume it; letting it
        # persist would be a standing protect-anything authority).
        #
        # Unbound fallback (operator repro 2026-06-11): after a server
        # crash/reconnect the managed session may not be re-bound yet —
        # the old `if managed_session_id` guard silently dropped the
        # sqlite write, so a verbatim canonical grant phrase minted
        # NOTHING the MCP-server-side ai_protect could see. Grants now
        # land under the '__unbound__' key when no session is bound;
        # _active_grants always reads that key in union.
        _grant_sid = managed_session_id or "__unbound__"
        try:
            gate = self.runtime.hub.query_gate
            # Prior-grant reads are guarded separately: a gate without
            # get_* (test stubs, older stores) must not abort the writes.
            try:
                prior_protect = {
                    p
                    for p in gate.get_protect_grants(project_root, _grant_sid)
                    if p and p != "*"
                }
                prior_unprotect = {
                    p
                    for p in gate.get_unprotect_grants(project_root, _grant_sid)
                    if p and p != "*"
                }
            except Exception:
                prior_protect = set()
                prior_unprotect = set()
            gate.set_protect_grants(
                project_root,
                _grant_sid,
                sorted(set(protect_paths) | prior_protect),
            )
            gate.set_unprotect_grants(
                project_root,
                _grant_sid,
                sorted(set(unprotect_paths) | prior_unprotect),
            )
            gate.set_protected_edit_grants(
                project_root,
                _grant_sid,
                edit_paths,
            )
        except Exception:
            pass

        if not (protect_paths or unprotect_paths or edit_paths):
            return PromptMutationResult.empty()

        side_effects = []
        if protect_paths:
            side_effects.append(f"protect grants: {len(protect_paths)} paths")
        if unprotect_paths:
            side_effects.append(f"unprotect grants: {len(unprotect_paths)} paths")
        if edit_paths:
            side_effects.append(f"protected_edit grants: {len(edit_paths)} paths")
        return PromptMutationResult(
            decision="allow",
            side_effects=tuple(side_effects),
            why=("dnt_grants_applied",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: user_prompt_received audit + fresh-CLI
    # ------------------------------------------------------------------

    def record_user_prompt_received(
        self,
        *,
        prompt: str,
        host_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Record the universal UPS audit event AND detect fresh-CLI
        identity changes.

        Audit row: one ``user_prompt_received`` event per operator
        message. Always captures prompt_hash + prompt_len + cli_session_id
        for replay integrity; prompt text captured only when
        ``audit.capture_prompt_content`` is on (dashboard toggle,
        default off — prompts are sensitive).

        Fresh-CLI detection: hosts send a per-process session UUID in
        every UPS payload. When it changes, the agent is a fresh launch
        that inherited sqlite state with empty in-memory context. Raise
        ``requires_reconnect`` so the agent MUST re-bind before any
        other tool. Continuation calls (same UUID) are a no-op.

        Caller passes ``host_session_id`` already extracted from its
        envelope (CC's payload.session_id; OC's hookCtx.sessionID).
        """
        if not host_session_id:
            return PromptMutationResult.empty()

        # Resolve managed session via per-conductor mapping
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=host_session_id,
            )
        except Exception:
            return PromptMutationResult.empty()
        if not managed.get("active"):
            return PromptMutationResult.empty()
        session_id = str(managed.get("session_id") or "").strip()
        if not session_id:
            return PromptMutationResult.empty()

        side_effects: list[str] = []
        audit_events: list[tuple[str, dict]] = []
        why: list[str] = []

        # Universal audit event
        try:
            import hashlib

            from .config import get_setting

            prompt_hash = hashlib.sha256(
                prompt.encode("utf-8", "replace"),
            ).hexdigest()[:32]
            capture_content = bool(
                get_setting(
                    "audit.capture_prompt_content",
                    project_root=project_root,
                    default=False,
                ),
            )
            audit_payload: dict = {
                "prompt_hash": prompt_hash,
                "prompt_len": len(prompt),
                "cli_session_id": host_session_id,
            }
            if capture_content:
                audit_payload["prompt_text"] = prompt[:8000]
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="user_prompt_received",
                source_kind="user_prompt_submit",
                session_id=session_id,
                capability_name="UserPromptSubmit",
                action_kind="prompt",
                status="received",
                payload=audit_payload,
            )
            audit_events.append(("user_prompt_received", audit_payload))
            why.append("ups_audit_written")
        except Exception:
            pass

        # Fresh-CLI detection
        try:
            changed = self.runtime.hub.query_gate.check_and_update_cli_session_id(
                project_root,
                session_id,
                host_session_id,
            )
            if changed:
                side_effects.append(
                    f"fresh-CLI detected (session={session_id}, new_cli={host_session_id})",
                )
                why.append("fresh_cli_detected")
                # Audit the rebind event too
                try:
                    self.runtime.hub.execution.record_event(
                        project_root,
                        event_kind="new_cli_session",
                        source_kind="user_prompt_submit",
                        session_id=session_id,
                        capability_name="UserPromptSubmit",
                        status="observed",
                        payload={
                            "session_id": session_id,
                            "cli_session_id": host_session_id,
                        },
                    )
                    audit_events.append(
                        (
                            "new_cli_session",
                            {
                                "session_id": session_id,
                                "cli_session_id": host_session_id,
                            },
                        ),
                    )
                except Exception:
                    pass
        except Exception:
            pass

        if not why:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            side_effects=tuple(side_effects),
            audit_events=tuple(audit_events),
            why=tuple(why),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: lane-exit grant
    # ------------------------------------------------------------------

    def apply_lane_exit_grant(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
        is_worker_proc: bool,
    ) -> PromptMutationResult:
        """Conductor lane-exit escape hatch. When a worker's
        session_connect left ``current_lane_id`` set on the session
        query_gate row, the conductor gets trapped in the worker's
        lane scope. Two paths clear it:

          1. Per-turn phrase grant ("exit lane" + variants)
          2. Sticky safety net: ``conductor.auto_exit_lane=true`` AND
             the lane has NO live worker processes

        Both paths are env-gated off worker processes — workers must
        never self-exit (would defeat lane isolation).

        Returns ``allow`` with side_effects + audit_events when the
        exit fired. Empty when no exit conditions were satisfied.
        """
        if not managed_session_id:
            return PromptMutationResult.empty()
        if is_worker_proc:
            # Workers must never self-escape their own lane.
            return PromptMutationResult.empty()

        # Phrase grant detection. is_worker_caller=False is structurally
        # safe here: the caller bails at the top of this function when
        # is_worker_proc is True (workers must never self-exit). The
        # worker-fence env read that lived in the prior _detect_lane_
        # exit_grant wrapper was redundant — this call never reaches
        # for a worker process.
        try:
            from .canonical_intent_registry import detect_lane_exit_v2

            phrase_grant = bool(detect_lane_exit_v2(prompt, is_worker_caller=False))
        except Exception:
            phrase_grant = False

        # Sticky auto-exit config check
        sticky_auto_exit_cfg = False
        try:
            from .config import get_setting

            sticky_auto_exit_cfg = bool(
                get_setting(
                    "conductor.auto_exit_lane",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            sticky_auto_exit_cfg = False

        # Sticky only fires when a lane is bound AND no live worker.
        sticky_auto_exit = False
        if sticky_auto_exit_cfg:
            try:
                current_row = (
                    self.runtime.hub.query_gate.get(
                        project_root,
                        managed_session_id,
                    )
                    or {}
                )
                current_lane = str(current_row.get("current_lane_id") or "").strip()
            except Exception:
                current_lane = ""
            if current_lane:
                try:
                    from .session_lane_agents_store import (
                        SessionLaneAgentsStore,
                    )

                    live_workers = (
                        SessionLaneAgentsStore().get_lane_agents(
                            project_root,
                            session_id=managed_session_id,
                            state_filter="running",
                        )
                        or []
                    )
                    live_in_lane = [
                        w
                        for w in live_workers
                        if str(w.get("lane_id") or "").strip() == current_lane
                    ]
                    sticky_auto_exit = not live_in_lane
                except Exception:
                    # Err on the side of NOT auto-exiting — safer to
                    # leave the lane bound than accidentally break
                    # isolation.
                    sticky_auto_exit = False

        if not (phrase_grant or sticky_auto_exit):
            return PromptMutationResult.empty()

        trigger = "user_intent_grant" if phrase_grant else "sticky_auto_exit"
        try:
            self.runtime.hub.query_gate.set(
                project_root,
                managed_session_id,
                last_tool="lane_exit",
                current_lane_id=None,
                lane_exact_paths=[],
            )
        except Exception:
            return PromptMutationResult.empty()

        try:
            self.runtime.hub.execution.record_event(
                project_root,
                event_kind="lane_exit_grant",
                session_id=managed_session_id,
                capability_name="conductor_lane_exit",
                status="observed",
                payload={"trigger": trigger},
            )
        except Exception:
            pass

        return PromptMutationResult(
            decision="allow",
            side_effects=(f"lane_exit cleared (session={managed_session_id}, trigger={trigger})",),
            audit_events=(
                (
                    "lane_exit_grant",
                    {
                        "session_id": managed_session_id,
                        "trigger": trigger,
                    },
                ),
            ),
            why=("lane_exit_grant", trigger),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: config-set grants
    # ------------------------------------------------------------------

    def apply_config_set_grants(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Detect config_set grant phrases ("turn on X", "set Y to Z",
        "disable W") and stash to ``query_gate.config_grants``. The
        config_set MCP tool consults this per-session map before
        applying mutations.

        SQLite-only because the MCP tool server runs in a separate
        process from this hook — process-local dicts wouldn't cross
        the boundary.

        Empty when no managed session, no grant phrase matched, or
        the store call failed.
        """
        if not managed_session_id:
            return PromptMutationResult.empty()
        try:
            from .canonical_intent_registry import detect_config_grants_v2

            grants = detect_config_grants_v2(prompt)
            self.runtime.hub.query_gate.set_config_grants(
                project_root,
                managed_session_id,
                grants,
            )
        except Exception:
            return PromptMutationResult.empty()
        if not grants:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            side_effects=(
                f"config_set grants stashed ({len(grants)} keys: "
                f"{', '.join(sorted(grants.keys())[:4])})",
            ),
            why=("config_set_grants",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: intent-phrase dispatch
    # ------------------------------------------------------------------

    def intent_phrase_dispatch(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Closed-vocabulary intent-phrase detection (plan_session_enter
        / plan_session_exit / etc.) followed by dispatch.

        Runs before route classification so state changes are visible
        to downstream context-building. Dispatch results are surfaced
        as side_effects + additional_context_blocks (one block per
        intent that emitted an acknowledgment).

        Opportunistic: failures must not break the rest of the
        prompt-submit flow.
        """
        if not managed_session_id:
            return PromptMutationResult.empty()
        try:
            from .intent_phrase_detector import detect_intent_phrases
            from .intent_phrase_handlers import dispatch_intents

            intents = detect_intent_phrases(prompt)
            if not intents:
                return PromptMutationResult.empty()
            results = dispatch_intents(
                self.runtime.hub.query_gate._store,
                project_root,
                managed_session_id,
                intents,
            )
        except Exception:
            return PromptMutationResult.empty()

        blocks: list[str] = []
        for r in results or []:
            ctx = r.get("context") or r.get("additional_context")
            if isinstance(ctx, str) and ctx.strip():
                blocks.append(ctx)
        return PromptMutationResult(
            decision="allow",
            additional_context_blocks=tuple(blocks),
            side_effects=(f"intent_phrase_dispatch: {len(intents)} intents detected",),
            why=("intent_phrase_dispatch",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: worker-lane mailbox intercept + protocol
    # ------------------------------------------------------------------

    def worker_lane_intercept(
        self,
        *,
        project_root: Path,
        worker_lane_id: str = "",
        worker_session_id: str = "",
        worker_id: str = "",
    ) -> PromptMutationResult:
        """When the current process is a lane worker (host's
        equivalent of Claude Code's AIDOCS_EXPERT_LANE_ID env vars),
        intercept the wake-prompt:

          1. Check the lane mailbox via ``LaneMailboxStore.take``.
             If a conductor message is waiting, rewrite the prompt
             with that message + a "report-when-done" tail.
          2. Otherwise, rewrite the prompt with a terse worker
             protocol reminder (the lane scope, the 4-step protocol).

        Worker identity is passed as kwargs rather than read from env
        so each host adapter can resolve "am I a worker?" however its
        runtime presents that fact (CC reads AIDOCS_EXPERT_LANE_ID env
        vars; OpenCode workers may surface this via a different
        mechanism in the future).

        Empty result when not a worker — caller's normal UPS flow
        proceeds.

        Opportunistic TTL sweep on every lane wake — cheap (indexed
        scan) and keeps stale messages from the queue without a
        separate cron.
        """
        if not worker_lane_id:
            return PromptMutationResult.empty()

        # Mailbox intercept (only if worker_id is also set)
        if worker_id:
            try:
                from .lane_mailbox_store import LaneMailboxStore

                store = LaneMailboxStore()
                store.expire_stale(project_root)
                msg = store.take(project_root, worker_id=worker_id)
            except Exception:
                msg = None
            if msg is not None:
                injected = (
                    f"[AIDOCS mailbox] conductor sent task "
                    f"(mailbox_id={msg['mailbox_id']}): "
                    f"{msg['prompt']}\n\n"
                    f"When this task is complete, call "
                    f"`mcp__aidocs__ai_task(mode='update', ...)` to report "
                    f"then `ScheduleWakeup(delaySeconds=60, "
                    f'prompt="check mailbox")` to park for '
                    f"the next instruction."
                )
                return PromptMutationResult(
                    decision="allow",
                    rewritten_prompt=injected,
                    side_effects=(
                        f"worker mailbox consumed "
                        f"(worker_id={worker_id}, "
                        f"mailbox_id={msg['mailbox_id']})",
                    ),
                    why=("worker_mailbox_intercept",),
                )

        # No mailbox message — emit the protocol reminder
        protocol = (
            f"Lane sub-agent for `{worker_lane_id}` "
            f"(session `{worker_session_id}`). Your task brief is "
            f"in the first user message — do not re-bootstrap. "
            f"Protocol: "
            f"1) `mcp__aidocs__ai_session(mode='connect', "
            f'session_id="{worker_session_id}", '
            f'lane_id="{worker_lane_id}")`. '
            f"2) `mcp__aidocs__ai_task(mode='begin', ...)`. "
            f"3) Work the brief with the allowed `mcp__aidocs__*` "
            f"tools (indexed reads + edits keep other lanes "
            f"consistent via the shared index). "
            f"4) `mcp__aidocs__ai_task(mode='complete', ...)`, emit "
            f"AIDOCS_EXPERT_RESULT, exit — OR ScheduleWakeup and "
            f"wait for conductor instruction via mailbox."
        )
        return PromptMutationResult(
            decision="allow",
            rewritten_prompt=protocol,
            why=("worker_protocol_inject",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: per-turn intent-state mutations
    # ------------------------------------------------------------------

    def apply_per_turn_intent_state(
        self,
        *,
        prompt: str,
        managed_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Run the four coupled per-turn state mutations:

          1. Bash subcommand grants: union of per-turn detected
             ("allow psql") with active tier-2 sticky bash grants.
             Written to ``query_gate.user_intent_bash_subcommands``.
          2. Ask-state plumbing: increment turn counter, then resolve
             yes/no on any pending_confirmation (yes → promote to
             last_confirmed_operation; no → clear; ambiguous → let
             TTL handle).
          3. Credential token stash: extract provider-prefix tokens
             and write to ``query_gate.user_intent_credentials``
             (downgrades FILE_*_KEY hard-blocks to confirm).
          4. Destructive intent token stash: detect destructive-intent
             tokens and write to ``query_gate.user_intent_destructive``
             (downgrades judge destructive-pattern hard-blocks).

        SEC-002 transactional contract: best-effort DETECTION (parsing
        the prompt) may degrade silently, but the AUTHORITY-BEARING
        query-gate WRITES (user_intent_bash_subcommands,
        pending_confirmation / last_confirmed_operation,
        user_intent_credentials, user_intent_destructive — all
        ``_PRIVILEGE_COLUMNS``) must NOT swallow their exceptions. A write
        failure propagates to the caller's SEC-002 atomic stage, which
        restores the pre-mutation snapshot, sets degraded_state, and
        audits ``prompt_mutation_failed``. Swallowing a write failure here
        would leave partial authority state (e.g. user_intent_tools
        already granted by an earlier sub-pipeline) silently committed.

        Caller passes ``managed_session_id`` (already resolved). When
        empty, no mutations run (these all key on the managed session).
        """
        if not managed_session_id:
            return PromptMutationResult.empty()

        from .prompt_mutation_plan import PromptMutationPlan

        side_effects: list[str] = []
        why: list[str] = []
        qg = self.runtime.hub.query_gate
        plan = PromptMutationPlan()

        # 1. Bash subcommand grants. Detection best-effort; the write is
        # QUEUED only when the resolved set differs from current state — a
        # clean prompt (nothing detected, nothing already stored) adds no
        # step. Per-turn TTL is preserved: when a prior grant exists the
        # resolved set ([]/shrunk) differs, so the clearing write still
        # fires.
        try:
            from .canonical_intent_registry import (
                detect_bash_subcommand_grants_v2,
            )

            per_turn = set(detect_bash_subcommand_grants_v2(prompt))
        except Exception:
            per_turn = set()
        try:
            from .sticky_grants_store import StickyGrantsStore

            sticky = set(
                StickyGrantsStore().active_bash_subcommands_for_session(
                    project_root,
                    managed_session_id,
                ),
            )
        except Exception:
            sticky = set()
        new_bash = sorted(per_turn | sticky)
        if new_bash != list(
            qg.get_user_intent_bash_subcommands(
                project_root,
                managed_session_id,
            ),
        ):
            plan.add(
                lambda v=new_bash: qg.set_user_intent_bash_subcommands(
                    project_root,
                    managed_session_id,
                    v,
                ),
            )
            side_effects.append(f"bash_subs set (per_turn={len(per_turn)}, sticky={len(sticky)})")
            why.append("bash_subcommand_grants")

        # 2. Ask-state plumbing. Only touches state when a pending
        # confirmation exists — a clean prompt with nothing pending queues
        # nothing (not even the turn-counter tick, which only matters as the
        # TTL clock for an outstanding confirmation).
        try:
            pending = qg.get_pending_confirmation(
                project_root,
                managed_session_id,
            )
        except Exception:
            pending = None
        if pending is not None:
            try:
                from .canonical_intent_registry import (
                    detect_confirmation_response_v2,
                )

                decision = detect_confirmation_response_v2(prompt)
            except Exception:
                decision = None
            plan.add(
                lambda: qg.increment_turn_counter(
                    project_root,
                    managed_session_id,
                ),
            )
            if decision == "yes":
                import time as _t

                _confirmed = {
                    "id": pending.get("id"),
                    "command_sha": pending.get("command_sha"),
                    "consumed": False,
                    "approved_at": _t.strftime(
                        "%Y-%m-%dT%H:%M:%S",
                        _t.gmtime(),
                    ),
                }
                plan.add(
                    lambda p=_confirmed: qg.set_last_confirmed_operation(
                        project_root,
                        managed_session_id,
                        p,
                    ),
                )
                plan.add(
                    lambda: qg.set_pending_confirmation(
                        project_root,
                        managed_session_id,
                        None,
                    ),
                )
                side_effects.append("ask_state: yes → promoted")
                why.append("ask_state_yes")
            elif decision == "no":
                plan.add(
                    lambda: qg.set_pending_confirmation(
                        project_root,
                        managed_session_id,
                        None,
                    ),
                )
                side_effects.append("ask_state: no → cleared")
                why.append("ask_state_no")
            # Ambiguous: the queued counter tick ages it via TTL.

        # 3. Credential token stash. Queued only on a delta.
        try:
            from .heuristic_judge import extract_credential_tokens

            tokens = list(extract_credential_tokens(prompt))
        except Exception:
            tokens = []
        if tokens != list(qg.get_user_intent_credentials(project_root, managed_session_id)):
            plan.add(
                lambda v=tokens: qg.set_user_intent_credentials(
                    project_root,
                    managed_session_id,
                    v,
                ),
            )
            if tokens:
                side_effects.append(f"credential tokens stashed (count={len(tokens)})")
                why.append("credential_stash")

        # 4. Destructive intent token stash. Queued only on a delta.
        try:
            from .intent_grant_detector import (
                detect_destructive_intent_in_text,
            )

            destructive = list(detect_destructive_intent_in_text(prompt))
        except Exception:
            destructive = []
        if destructive != list(qg.get_user_intent_destructive(project_root, managed_session_id)):
            plan.add(
                lambda v=destructive: qg.set_user_intent_destructive(
                    project_root,
                    managed_session_id,
                    v,
                ),
            )
            if destructive:
                side_effects.append(f"destructive intent stashed (count={len(destructive)})")
                why.append("destructive_stash")

        # Plan-before-apply: nothing above touched the gate. Empty plan ⇒
        # zero writes (clean-prompt cleanliness). Writes propagate — a
        # mid-apply failure escapes to the caller's SEC-002 atomic stage,
        # which restores the snapshot, sets degraded, and audits.
        plan.apply()

        if not side_effects and not why:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            side_effects=tuple(side_effects),
            why=tuple(why),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: prompt-secret block
    # ------------------------------------------------------------------

    def prompt_secret_block(
        self,
        *,
        prompt: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Block the prompt when it contains credential token(s) AND
        ``security.prompt_secret_policy`` is ``'block'`` (the default).

        Per castle doctrine: canonical message rewrite isn't supported
        across hosts (most expose block/add-context only, not
        modifiedPrompt), so 'block' is the strongest enforcement
        available. Operators who deliberately hand the agent a secret
        can set the policy to 'allow'.

        Fail-open: a broken config resolver must NOT trap the operator
        out of their chat. Downstream user-intent flow is the backstop.
        """
        try:
            from .config import get_setting

            policy = (
                str(
                    get_setting(
                        "security.prompt_secret_policy",
                        project_root=project_root,
                        default="block",
                    )
                    or "block",
                )
                .strip()
                .lower()
            )
        except Exception:
            return PromptMutationResult.empty()

        if policy != "block":
            return PromptMutationResult.empty()

        try:
            from .heuristic_judge import extract_credential_tokens

            hits = extract_credential_tokens(prompt)
        except Exception:
            return PromptMutationResult.empty()

        if not hits:
            return PromptMutationResult.empty()

        # Redact the preview so the refusal message itself doesn't
        # leak the token back into any transcript we don't control.
        preview = ", ".join((t[:6] + "…" + t[-2:] if len(t) > 10 else "***") for t in hits[:3])
        return PromptMutationResult(
            decision="block",
            block_reason=(
                f"🛑 SECRET DETECTED — your message contains "
                f"credential token(s) [{preview}] and the "
                f"security.prompt_secret_policy='block' is "
                f"active. The turn was refused; the agent "
                f"never saw the prompt. Store the secret in "
                f"an env var or a secret manager and refer to "
                f"it by name instead. Dashboard toggle: set "
                f"security.prompt_secret_policy='allow' if "
                f"you intended to share this credential."
            ),
            why=("prompt_secret_block",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: pre-flight prompt judge
    # ------------------------------------------------------------------

    def preflight_judge(
        self,
        *,
        prompt: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Run the hostile-prompt judge. Three outcomes:

          1. ``PreflightDegraded`` (judge raised internally) → block
             with explicit 'pre-flight unavailable' message + emit
             distinct ``preflight_degraded`` audit event (so the
             strike system can filter system-bug events from
             hostile-prompt verdicts).
          2. Hostile verdict (critical-risk forbidden class) → block.
          3. Confirmable verdict → block with "rephrase" message
             (full freeze-pipeline integration is a future batch).
          4. PASS → empty result; outer pipeline continues.

        Ordering matters: this MUST run before grant detection,
        sticky-grant mutation, intent-phrase dispatch — any
        mutation before this could be poisoned by a hostile prompt.

        Outer try/except is a safety net only. The evaluator has
        its own try/except that returns PreflightDegraded on
        internal errors; failures past that return an empty
        result and let the normal UPS flow proceed.
        """
        try:
            from .preflight_prompt_judge import (
                PreflightDegraded as _PreflightDegraded,
            )
            from .preflight_prompt_judge import (
                evaluate_prompt as _preflight_evaluate,
            )

            outcome = _preflight_evaluate(
                prompt,
                project_root=project_root,
            )
        except Exception:
            return PromptMutationResult.empty()

        # Degraded path
        if isinstance(outcome, _PreflightDegraded):
            try:
                self.runtime.hub.execution.record_event(
                    project_root,
                    event_kind="preflight_degraded",
                    source_kind="user_prompt_submit",
                    capability_name="UserPromptSubmit",
                    action_kind="degraded",
                    status="rolled_back",
                    payload={
                        "exception_class": outcome.exception_class,
                        "exception_message": outcome.exception_message[:200],
                        "operator_message": "pre-flight unavailable / degraded",
                        "strike_increment": False,
                    },
                )
            except Exception:
                pass
            return PromptMutationResult(
                decision="block",
                block_reason=(
                    "🛑 PRE-FLIGHT UNAVAILABLE — the hostile-prompt "
                    "evaluator raised an internal error and the "
                    "request was refused defensively. This is a "
                    "system condition, not a hostile-prompt verdict. "
                    "Retry the prompt; if it persists, check the "
                    "preflight_degraded audit events."
                ),
                audit_events=(
                    (
                        "preflight_degraded",
                        {"exception_class": outcome.exception_class},
                    ),
                ),
                why=("preflight_degraded",),
            )

        # Verdict path
        if outcome.should_block:
            forbidden = [v for v in outcome.verdicts if v.risk == "critical"]
            if forbidden:
                rule_ids = ", ".join(v.rule_id for v in forbidden[:3])
                # Operator forbidden prompt → IMMEDIATE freeze (doctrine
                # split a3fec0de→this commit): a UserPromptSubmit is judged
                # before the agent sees it, so a hostile operator prompt
                # gets no "two chances" strike ladder — the first forbidden
                # verdict freezes the session (admin-clear only). Degraded
                # is handled above and does NOT freeze; confirmable (below)
                # does NOT create a repeated-security freeze either.
                # Best-effort: never breaks the block path.
                strike_msg = ""
                try:
                    from .security_violation_service import SecurityViolationService

                    sid = ""
                    managed = self.runtime.hub.managed_mode.get_mode(project_root)
                    if managed.get("active"):
                        sid = str(managed.get("session_id") or "").strip()
                    if sid:
                        _sv = SecurityViolationService(
                            self.runtime.hub,
                        ).freeze_operator_forbidden_prompt(
                            project_root,
                            session_id=sid,
                            rule_ids=rule_ids,
                        )
                        strike_msg = getattr(_sv, "message", "") or ""
                except Exception:
                    strike_msg = ""
                block_reason = (
                    f"🛑 PRE-FLIGHT BLOCKED — hostile operator "
                    f"intent detected ({rule_ids}). The agent "
                    f"never saw the prompt. If this is a "
                    f"false positive, file the rule_id and "
                    f"prompt with the AIDOCS team."
                )
                if strike_msg:
                    block_reason += f"\n\n{strike_msg}"
                return PromptMutationResult(
                    decision="block",
                    block_reason=block_reason,
                    why=("preflight_forbidden", rule_ids),
                )
            confirmable_ids = ", ".join(v.rule_id for v in outcome.verdicts[:3])
            return PromptMutationResult(
                decision="block",
                block_reason=(
                    f"🛑 PRE-FLIGHT FLAG — operator intent flagged "
                    f"({confirmable_ids}). Re-phrase the request "
                    f"or confirm intent explicitly. (Freeze "
                    f"pipeline integration lands in #44 Batch 7.)"
                ),
                why=("preflight_confirmable", confirmable_ids),
            )

        return PromptMutationResult.empty()

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: escalation scrub
    # ------------------------------------------------------------------

    def escalation_scrub(
        self,
        *,
        prompt: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Strip ``deny: <request_id> [reason]`` decision lines from a
        prompt and flip the matching pending escalation rows.

        Scope correction (2026-06-11): this handles ``deny:`` ONLY, which
        carries NO credentials (denial is the permissive action). ``approve:``
        via chat is intentionally UNSUPPORTED (the host transcript captures
        any typed password before hooks fire), and approve/credential tokens
        are blocked EARLIER by ``prompt_secret_block`` (pipeline step 2). So
        this surface never sees a password — the prior docstring's
        "approve: <email> <password>" claim was wrong.

        Returns ``rewritten_prompt`` when a decision line was stripped; caller
        uses it downstream. Empty when no decision line was present. On a
        processing exception the decision line is still redacted by a pure
        substitution (fail-closed — see the except block); only the row
        mutation is lost, recorded as degraded.
        """
        try:
            from .escalation_hook import scrub_and_process

            active = self.runtime.hub.managed_mode.get_mode(project_root)
            session_id = (
                str(active.get("session_id") or "").strip() if active.get("active") else None
            )
            scrub = scrub_and_process(
                project_root,
                prompt,
                session_id=session_id,
            )
        except Exception:
            # FAIL CLOSED (2026-06-11, co-co): split redaction from row
            # mutation. scrub_and_process couples decision-line REDACTION
            # (pure string op) with the EscalationStore row flip (can fail).
            # If it raised AND the prompt still carries a decision line, strip
            # that line by a pure substitution that cannot fail — so an
            # unprocessed escalation line never flows to the agent — and mark
            # the lost row mutation as degraded. Only a prompt with NO
            # decision shape is safe to pass through unchanged.
            try:
                from .escalation_hook import _DENY_PATTERN

                if _DENY_PATTERN.search(prompt or ""):
                    return PromptMutationResult(
                        decision="allow",
                        rewritten_prompt=_DENY_PATTERN.sub("", prompt),
                        side_effects=(
                            "escalation_scrub: processing failed; decision line "
                            "redacted defensively, row mutation NOT applied "
                            "(fail-closed).",
                        ),
                        why=("escalation_scrub_failclosed",),
                    )
            except Exception:
                pass
            return PromptMutationResult.empty()

        if not scrub.credentials_scrubbed:
            return PromptMutationResult.empty()

        return PromptMutationResult(
            decision="allow",
            rewritten_prompt=scrub.rewritten_prompt,
            side_effects=tuple(f"escalation_scrub: {se}" for se in (scrub.side_effects or [])),
            why=("escalation_scrub",),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: session-freeze resolver (#39)
    # ------------------------------------------------------------------

    def resolve_session_freeze(
        self,
        *,
        prompt: str,
        host_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Single-turn resolver for an active session freeze.

        Per #39:
          - prompt EQUALS the freeze fingerprint phrase → mint scoped
            grant via escalation_store.decide(approve=True), clear
            freeze.
          - prompt matches cancel pattern → decide(approve=False),
            clear freeze, no grant.
          - anything else → clear freeze silently, no grant, no
            audit blame.

        Self-approve only at Phase A. admin_escalation freezes are
        skipped here — they resolve via escalation_store polling on
        the next PreToolUse instead.

        Resolution cascade (king-rendered 2026-05-03, patch order #5a):
          1) per-conductor: host_session_id → bound session.
          2) singleton fallback (legacy path).

        Side-effect only. Returns an empty mutation result regardless
        of outcome — the freeze clear is observable in the next
        PreToolUse's gate evaluation, not in the UPS additional context.
        Failures swallowed: freeze sticks until next UPS (annoying
        but safe).
        """
        try:
            return self._resolve_session_freeze_impl(
                prompt=prompt,
                host_session_id=host_session_id,
                project_root=project_root,
            )
        except Exception:
            # Best-effort. Freeze stays until next UPS — operator
            # retries — no decision made; safe failure mode.
            return PromptMutationResult.empty()

    def _resolve_session_freeze_impl(
        self,
        *,
        prompt: str,
        host_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        from .session_freeze_store import (
            KIND_SELF_APPROVE,
            SessionFreezeStore,
        )

        sfs = SessionFreezeStore()

        # Resolution cascade
        session_id = ""
        if host_session_id:
            try:
                m_pc = self.runtime.hub.managed_mode.get_mode(
                    project_root,
                    cli_session_id=host_session_id,
                )
                if m_pc.get("active") and m_pc.get("resolved_via") == "per_conductor":
                    session_id = str(m_pc.get("session_id") or "").strip()
            except Exception:
                session_id = ""
        if not session_id:
            try:
                m_sg = self.runtime.hub.managed_mode.get_mode(project_root)
            except Exception:
                return PromptMutationResult.empty()
            if not m_sg.get("active"):
                return PromptMutationResult.empty()
            session_id = str(m_sg.get("session_id") or "").strip()
            if not session_id:
                return PromptMutationResult.empty()

        freeze = sfs.get_active_freeze(project_root, session_id)
        if freeze is None:
            return PromptMutationResult.empty()
        if freeze.kind != KIND_SELF_APPROVE:
            return PromptMutationResult.empty()

        text = (prompt or "").strip()
        text_lower = text.lower()
        target_phrase = freeze.fingerprint_phrase.strip()

        from .escalation_store import EscalationStore

        es = EscalationStore()

        # CONTAINMENT MATCH (2026-05-27, step 1/3): operators routinely
        # pair the confirm phrase with extra instructions in the same
        # message ("set bla.bla to true, confirm tsk-XXX, then clean
        # the .pyc files"). The prior `text == target_phrase` check
        # rejected EVERY natural composition. Now we accept the
        # confirmation when the phrase appears as a distinct token in
        # the prompt — word-boundary aware so it never matches as a
        # substring of an unrelated word.
        #
        # Cancel-token detection stays substring-based for cancel
        # phrases (already tolerant of natural language). When BOTH
        # signals appear in the same prompt we fail closed: the freeze
        # stays, and a side-effect tells the operator to re-issue
        # with just one signal. That guards against an operator
        # changing their mind mid-sentence ("yes, confirm tsk-1, no
        # wait — cancel that") silently going either way.
        # Cancel tokens — kept tight on PURPOSE (2026-05-27 step 1/3).
        # TIERED cancel signal (2026-05-27 step 1/3).
        #
        # STRONG cancel = unambiguous reversal intent ("cancel" /
        # "abort" / "deny" / "no thanks" / "nope" / bare "no"/"n").
        # If a STRONG cancel appears ALONGSIDE the confirm phrase,
        # the prompt is ambiguous and we fail closed
        # (changed-mind-mid-sentence guard).
        #
        # WEAK cancel = colloquial verb that's also natural commentary
        # ("stop"). The emperor's example: "...and stop being a
        # smart-ass" is a directive to the agent, NOT a cancel of
        # the freeze. Weak cancel counts ONLY when it appears WITHOUT
        # a confirm phrase (alone, it's still a valid cancel intent:
        # a bare prompt of "stop" cancels).
        #
        # Each token is word-boundary-matched so "cancel" doesn't
        # match inside "cancellation" / "deny" doesn't match inside
        # "endeniable" — defensive against operator commentary like
        # "approve the cancellation of the prior request".
        strong_cancel_tokens = (
            "cancel",
            "deny",
            "no thanks",
            "nope",
            "abort",
        )
        weak_cancel_tokens = ("stop",)

        def _has_token(toks: tuple[str, ...]) -> bool:
            return any(
                re.search(
                    r"(?<![A-Za-z0-9_-])" + re.escape(tok) + r"(?![A-Za-z0-9_-])",
                    text_lower,
                )
                for tok in toks
            )

        _is_cancel_strong = text_lower in {"no", "n"} or _has_token(strong_cancel_tokens)
        _is_cancel_weak = _has_token(weak_cancel_tokens)
        # Cancel branch (no-confirm path) accepts EITHER strong or
        # weak. Ambiguity guard (confirm + cancel) only triggers on
        # STRONG cancel — weak alongside confirm is ignored.
        _is_cancel = _is_cancel_strong or _is_cancel_weak

        # Word-boundary containment: the phrase must appear flanked by
        # chars that are NOT alphanumeric / underscore / hyphen. That
        # makes "tsk-4213213" match cleanly when embedded in a sentence
        # while refusing a longer string like "tsk-4213213-extension"
        # or "preceding-tsk-4213213" (those would consume different
        # fingerprints and shouldn't accidentally satisfy this one).
        _bdy_class = r"(?<![A-Za-z0-9_-])"
        _bdy_class_end = r"(?![A-Za-z0-9_-])"
        _has_confirm = (
            bool(
                re.search(
                    _bdy_class + re.escape(target_phrase) + _bdy_class_end,
                    text,
                ),
            )
            if target_phrase
            else False
        )

        if _has_confirm and _is_cancel_strong:
            # Ambiguous — confirm phrase AND a STRONG cancel signal
            # ("cancel" / "abort" / "deny" / "nope" / "no thanks" /
            # bare "no") in the same prompt. Keep the freeze and ask
            # the operator to re-issue cleanly. Weak cancel ("stop")
            # alongside confirm is IGNORED — it's natural commentary,
            # not a reversal (see TIERED note above).
            return PromptMutationResult(
                decision="allow",
                side_effects=(
                    f"ambiguous freeze response (request_id="
                    f"{freeze.request_id}): both the confirm phrase AND "
                    f"a cancel signal are present in the same prompt. "
                    f"Freeze KEPT — re-issue with exactly one signal.",
                ),
                why=("session_freeze_ambiguous_response",),
            )

        # CONFIRM branch: phrase present (anywhere in the prompt) and
        # no cancel signal alongside it.
        # FAIL CLOSED: the freeze is cleared and "grant minted" is
        # reported ONLY when a real, consumable grant_id was created.
        # If approval or grant minting fails (or the escalation request
        # has gone missing), the freeze STAYS — a cleared freeze with no
        # grant would let the retry through ungoverned, or strand the
        # operator with a freeze that looks approved but has nothing to
        # consume. Mint a grant bound to the EXACT action (fingerprint +
        # tool + session + machine) read from the binding stashed on
        # the request.
        if _has_confirm:
            from .freeze_service import mint_confirm_grant

            grant_id: str | None = None
            mint_error = ""
            # decide() flips PENDING → APPROVED and returns the updated
            # row ONLY when the request was actually pending; it returns
            # None for an unknown / already-denied / cancelled / consumed
            # request. We mint EXCLUSIVELY from that returned approved row
            # — never from a fresh es.get — so an exact confirm cannot
            # resurrect a non-pending request into a consumable grant.
            decided = None
            try:
                decided = es.decide(
                    project_root,
                    freeze.request_id,
                    approve=True,
                    approver_user_id=None,
                    approver_label="operator-self-approve",
                    reason="approved via freeze phrase",
                )
            except Exception as exc:
                mint_error = f"approve failed: {exc}"
            if decided is None:
                mint_error = mint_error or (
                    "escalation request not pending (unknown/denied/"
                    "cancelled/consumed) — refusing to mint a grant"
                )
            else:
                try:
                    grant_id = mint_confirm_grant(
                        project_root,
                        decided,
                        session_id,
                    )
                    if not grant_id:
                        mint_error = "grant creation returned no grant_id"
                except Exception as exc:
                    grant_id = None
                    mint_error = f"grant mint failed: {exc}"

            if not grant_id:
                # Fail closed: keep the freeze (do NOT clear), report the
                # failure. No "grant minted" claim. decision stays the
                # non-blocking default — "keep freeze" is observable via
                # the persisted freeze row (re-rendered on the next
                # PreToolUse), exactly like the garbage-prompt path; we
                # must not block the operator's prompt itself. The failure
                # is also recorded durably below.
                try:
                    from .execution_index_store import ExecutionIndexStore

                    ExecutionIndexStore().record_event(
                        project_root,
                        event_kind="self_approve_mint_failed",
                        source_kind="prompt_mutator",
                        session_id=session_id,
                        capability_name="resolve_session_freeze",
                        action_kind="escalation",
                        target_entity=freeze.request_id,
                        status="blocked",
                        payload={
                            "request_id": freeze.request_id,
                            "error": mint_error or "no consumable grant",
                        },
                    )
                except Exception:
                    pass
                return PromptMutationResult(
                    decision="allow",
                    side_effects=(
                        f"self-approve mint FAILED — freeze KEPT "
                        f"(request_id={freeze.request_id}): "
                        f"{mint_error or 'no consumable grant created'}",
                    ),
                    why=("session_freeze_mint_failed",),
                )

            sfs.clear_freeze(project_root, session_id)
            return PromptMutationResult(
                decision="allow",
                side_effects=(
                    f"freeze approved (request_id={freeze.request_id})",
                    f"grant minted (grant_id={grant_id}, scope=once)",
                    f"freeze cleared (session={session_id})",
                ),
                why=("session_freeze_approved",),
            )

        # Cancel patterns → explicit deny. _is_cancel was computed
        # up-front (see CONTAINMENT MATCH note above); we re-test it
        # here because the confirm branch already returned if both
        # signals were present.
        if _is_cancel:
            try:
                es.decide(
                    project_root,
                    freeze.request_id,
                    approve=False,
                    approver_user_id=None,
                    approver_label="operator-self-approve",
                    reason="declined via freeze cancel phrase",
                )
            except Exception:
                pass
            sfs.clear_freeze(project_root, session_id)
            return PromptMutationResult(
                decision="allow",
                side_effects=(
                    f"freeze declined (request_id={freeze.request_id})",
                    f"freeze cleared (session={session_id})",
                ),
                why=("session_freeze_declined",),
            )

        # Anything else → DO NOT clear. A freeze is resolved ONLY by an
        # EXPLICIT confirm (exact phrase → grant) or an explicit cancel
        # token (→ deny). An arbitrary/garbage prompt no longer silently
        # lifts the freeze — that was a workaround from when the CLI/
        # dashboard clear paths were unreliable. The freeze persists; the
        # next PreToolUse re-renders the confirm/cancel instructions.
        return PromptMutationResult.empty()

    # Explicit unfreeze INTENT — NLP or the literal command. Garbage (no
    # unfreeze verb) is intentionally NOT matched, so a stray prompt keeps
    # the session frozen.
    _UNFREEZE_INTENT_RE = re.compile(
        r"\b(unfreeze|unlock|unblock|clear[\s-]?freeze|"
        r"lift(?:ing)?\s+(?:the\s+)?freeze|clear\s+the\s+freeze)\b",
        re.IGNORECASE,
    )

    def resolve_chat_unfreeze(
        self,
        *,
        prompt: str,
        host_session_id: str,
        project_root: Path,
    ) -> PromptMutationResult:
        """Clear ANY active freeze (incl. a security lock) when the operator
        makes an EXPLICIT, MOTIVATED unfreeze request in chat AND holds the
        clear-freeze permission.

        Trigger: an explicit unfreeze intent — NLP ("unfreeze the agent
        because he tried reading a secret") OR the literal command
        (``aidocs admin clear-freeze --freeze-id … --reason …``). Garbage /
        unrelated prompts do NOT match and keep the session frozen.

        Motivated: a reason is required — from ``--reason``, a "because …"
        clause, or substantive request text. Authority: the host-operator
        binding (RBAC ``rbac.admin_clear_freeze``) or a dev-flavor local
        super-admin. A frozen AGENT cannot use it — the caller gates this
        on operator origin (``_grant_eligible``). Returns a confirmation
        block on success, a guidance block when unauthorized / no reason,
        else empty.
        """
        import re

        text = prompt or ""
        if not self._UNFREEZE_INTENT_RE.search(text):
            return PromptMutationResult.empty()

        def _opt(name: str) -> str:
            m = re.search(rf"{name}\s+(\"[^\"]*\"|'[^']*'|\S+)", text)
            if not m:
                return ""
            return m.group(1).strip().strip("\"'")

        freeze_id = _opt("--freeze-id")
        # Reason: --reason, else a "because …" clause, else substantive
        # request text beyond the bare unfreeze verb.
        reason = _opt("--reason")
        if not reason:
            mb = re.search(
                r"\bbecause\b\s+(.+)$",
                text,
                re.IGNORECASE | re.DOTALL,
            )
            if mb:
                reason = mb.group(1).strip()
        if not reason:
            residue = self._UNFREEZE_INTENT_RE.sub("", text).strip(" .!?,\n")
            if len(residue) >= 8:
                reason = text.strip()

        from .session_freeze_store import SessionFreezeStore

        sfs = SessionFreezeStore()

        # Resolve the managed session (per-conductor → singleton).
        session_id = ""
        try:
            m_pc = (
                self.runtime.hub.managed_mode.get_mode(
                    project_root,
                    cli_session_id=host_session_id,
                )
                if host_session_id
                else {}
            )
            if m_pc.get("active") and m_pc.get("resolved_via") == "per_conductor":
                session_id = str(m_pc.get("session_id") or "").strip()
        except Exception:
            session_id = ""
        if not session_id:
            try:
                m_sg = self.runtime.hub.managed_mode.get_mode(project_root)
            except Exception:
                return PromptMutationResult.empty()
            if not m_sg.get("active"):
                return PromptMutationResult.empty()
            session_id = str(m_sg.get("session_id") or "").strip()
        if not session_id:
            return PromptMutationResult.empty()

        freeze = sfs.get_active_freeze(project_root, session_id)
        if freeze is None:
            return PromptMutationResult.empty()

        # A supplied --freeze-id MUST match this session's active freeze —
        # never clear a different/stale lock by id. (Guidance only; the
        # freeze persists. decision='allow' because the hook surfaces this
        # as context — the freeze gate, not this result, keeps it locked.)
        if freeze_id and freeze_id != str(freeze.request_id):
            return PromptMutationResult(
                decision="allow",
                additional_context_blocks=(
                    f"🛑 --freeze-id {freeze_id} does not match the active "
                    f"freeze ({freeze.request_id}); not cleared.",
                ),
                why=("clear_freeze_id_mismatch",),
            )

        # AUTHORITY: host-operator binding (RBAC) OR dev-flavor super-admin.
        from .permission_catalog import PERM_ADMIN_CLEAR_FREEZE

        authorized = False
        approver_label = ""
        try:
            from .operator_auth_service import OperatorAuthService

            auth = OperatorAuthService()
            ctx = auth.resolve_operator_context_from_host_session(
                host_session_id,
                project_root,
            )
            if ctx is not None and auth.require_permission(
                ctx,
                PERM_ADMIN_CLEAR_FREEZE,
                project_root,
            ):
                authorized = True
                approver_label = ctx.email or ctx.user_id or "bound-operator"
        except Exception:
            authorized = False
        if not authorized:
            try:
                from .enforcement import is_dev_flavor

                if is_dev_flavor(project_root):
                    authorized = True
                    approver_label = "local-operator-dev"
            except Exception:
                pass

        # Unauthorized / no-reason → GUIDANCE surfaced as context. The
        # freeze is NOT cleared; it stays locked via the freeze gate (not
        # via decision='block', which this hook path does not honor).
        if not authorized:
            return PromptMutationResult(
                decision="allow",
                additional_context_blocks=(
                    "🛑 Unfreeze refused: this host session has no "
                    "clear-freeze permission. Bind an operator (dashboard) "
                    "or run `aidocs admin clear-freeze` from the CLI.",
                ),
                why=("clear_freeze_unauthorized",),
            )
        if not reason:
            return PromptMutationResult(
                decision="allow",
                additional_context_blocks=(
                    "🛑 Unfreeze needs a motivation: say why "
                    '("unfreeze the agent because …") or add --reason.',
                ),
                why=("clear_freeze_no_reason",),
            )

        # Clear through the ONE audited primitive — ledger-first ordering,
        # no decide/clear/audit split-brain (shared with the CLI + MCP
        # admin_clear_freeze paths).
        from .clear_freeze_service import ClearFreezeService

        result = ClearFreezeService().clear_with_audit(
            project_root,
            target_freeze=freeze,
            reason=reason,
            approver_label=approver_label,
            source_kind="chat_clear_freeze",
            cleared_event_kind="freeze_chat_cleared",
        )
        why = {
            "cleared": "freeze_chat_cleared",
            "audit_failed": "clear_freeze_audit_failed",
            "decide_failed": "clear_freeze_decide_failed",
            "repair_needed": "clear_freeze_repair_needed",
            "not_cleared": "clear_freeze_not_cleared",
        }.get(result.status, "clear_freeze_failed")
        if result.cleared:
            return PromptMutationResult(
                decision="allow",
                additional_context_blocks=(
                    f"✅ Freeze cleared via chat by {approver_label} (reason: {reason}).",
                ),
                side_effects=(f"freeze cleared (session={session_id})",),
                why=(why,),
            )
        return PromptMutationResult(
            decision="allow",
            additional_context_blocks=(f"🛑 {result.message}",),
            why=(why,),
        )

    # ------------------------------------------------------------------
    # Migrated sub-pipeline: notifications drain
    # ------------------------------------------------------------------

    def notifications_drain(
        self,
        payload: dict,
        project_root: Path,
    ) -> PromptMutationResult:
        """Drain pending run_notifications + lane_completion_reviews
        into additional context on every UPS.

        Worker-fenced via AIDOCS_EXPERT_LANE_ID: workers don't drain
        notifications meant for their conductor.

        Best-effort: never fails the prompt, never blocks.
        Resolves session_id via managed_mode; falls back to peek-all
        when managed_mode unresolved so notifications still surface
        in that edge state.
        """
        # Worker fence — workers see their own mailbox via a
        # different pipeline, not this drain.
        if os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
            return PromptMutationResult.empty()

        cli_sid = str(payload.get("session_id") or "").strip()
        managed_session_id = ""
        try:
            managed = self.runtime.hub.managed_mode.get_mode(
                project_root,
                cli_session_id=cli_sid,
            )
            if managed.get("active"):
                managed_session_id = str(managed.get("session_id") or "").strip()
        except Exception:
            managed_session_id = ""

        blocks: list[str] = []

        # Run-done notifications
        try:
            from . import run_notifications

            if managed_session_id:
                pending = run_notifications.peek_for_session(
                    project_root,
                    session_id=managed_session_id,
                )
            else:
                # Managed unresolved → peek-all fallback (same shape
                # tool_display uses for back-compat).
                pending = run_notifications.peek(project_root)
            if pending:
                blocks.append(run_notifications.format_block(pending))
        except Exception:
            pass

        # Pending lane completion reviews — OR-match on
        # (aidocs session_id, host_session_id) so a conductor that
        # swapped sessions still gets their reviews.
        try:
            if managed_session_id or cli_sid:
                from . import lane_completion_review_store

                pending_reviews = lane_completion_review_store.pending_for_session(
                    project_root,
                    session_id=managed_session_id or "",
                    host_session_id=cli_sid or "",
                )
                if pending_reviews:
                    blocks.append(
                        lane_completion_review_store.format_pending_block(
                            pending_reviews,
                        ),
                    )
        except Exception:
            pass

        if not blocks:
            return PromptMutationResult.empty()
        return PromptMutationResult(
            decision="allow",
            additional_context_blocks=tuple(blocks),
            why=("notifications_drain",),
        )
