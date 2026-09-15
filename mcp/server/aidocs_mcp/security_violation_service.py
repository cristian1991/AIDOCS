"""Repeated-security-violation strike counter + freeze escalation.

AIDOCS already freezes a session for CONFIRMABLE destructive verdicts
(freeze_service) and rate-limits repeated identical test runs (test-retry
gate). But FLAT security denials — command_read_intent, sensitive reads,
unknown-external reads, raw-shell T0 attempts, hostile preflight prompts —
just bounce off their gate with no memory. An agent (or operator) could
hammer them indefinitely.

This service records a strike per flat security denial, scoped by
(session, actor, lane, family), and escalates:

  1st strike  → block + ``security_violation_recorded`` audit
  2nd strike  → block + stern warning + ``security_violation_threshold_warning``
  Nth strike  → create a ``repeated_security_violation`` session freeze +
                ``security_violation_freeze_created`` audit
                (N = security.agent_security_violation_freeze_threshold,
                 default 3; 0 disables freeze escalation — block + audit
                 only. The deprecated security.repeated_violation_freeze_
                 threshold still drives the agent path for one cycle when
                 set and the new key is not.)

Forbidden OPERATOR UserPromptSubmit verdicts use a SEPARATE setting
(security.operator_forbidden_prompt_freeze_threshold, default 1 = immediate
hostile_operator_prompt freeze; 0 disables; N>=2 = optional ladder) — see
``freeze_operator_forbidden_prompt``.

The freeze is admin-clear-only (see session_freeze_store /
freeze_service): a UPS self-approve phrase cannot lift it, so the
offending actor cannot talk its own way out.

Strikes do NOT reset on a subsequent safe action (chosen policy: a
security lockdown is an emergency posture; only operator/admin clearing
the freeze, or a fresh session, resets the count). Counting reuses the
execution_events store with a forced-unique event_id per strike so rapid
repeats inside the dedup bucket still count distinctly, scoped by the
indexed ``target_entity`` (the scope key) rather than payload exact-match.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_memory_epoch import derive_agent_context_id


def _calling_agent_id() -> str:
    """The SUBAGENT axis for THIS call, or "" — see
    ``mcp_server_runtime_helpers.current_calling_agent_id``.

    MEASURED 2026-08-22 (Claude Code 2.1.239): a subagent's hook payload
    carries its PARENT's ``session_id`` and its parent's ``transcript_path``;
    ``agent_id`` is the only field that differs. Without it every subagent of
    one conversation derives ONE ``agent_context_id``, so the scope key below
    pools their ledgers: on 2026-08-21 three lane agents earning one strike
    each were scored as one actor with three, and the freeze landed on a
    conductor that had done nothing.

    Read HERE rather than passed down from the gate because nothing between a
    hook entrypoint and this ledger has an ``agent_id`` parameter, and the two
    other identity axes already travel the same request-scoped way.

    Returns "" for every caller that is not inside a hook request — the MCP
    transport cannot carry the axis at all — which keeps the derivation
    byte-identical for the main thread and for every host that never sends one.
    """
    try:
        from .mcp_server_runtime_helpers import current_calling_agent_id

        return current_calling_agent_id()
    except Exception:
        return ""


# blocked_by / verdict tag → canonical violation FAMILY. Only these flat
# denials are security-relevant strikes. Everything NOT here (system
# degraded, test_retry, no_active_task, config-missing, innocent
# no-path/typo, indexed_file_gate discovery nudges) must NOT increment.
FAMILY_BY_BLOCKED_BY: dict[str, str] = {
    "command_read_intent": "command_read_intent",
    "heuristic_judge_sensitive": "sensitive_read",
    "sensitive_path_blocked": "blocked_sensitive_external",
    "unknown_external_path": "unknown_external",
    "tier0_raw_shell": "raw_shell_t0",
    "host_read": "sensitive_external_read",
    # raw_tool is family-resolved by GateDecision.level at the call site
    # (only sensitive_file_protection counts), so it is NOT mapped here.
    "preflight_forbidden": "preflight_forbidden",
    # CONFIRMATION-WAR SPLIT (2026-05-26): malicious_forbidden hits from
    # the explicit judge_taxonomy decision (e.g. exfil, env/secret read,
    # blocked egress, inline net/subprocess evasion, gate/self-mod
    # tamper, fork bomb, docker host escape, auth-token exfil,
    # persistence/host-harness tamper). Routed via gate_tool's
    # _judge_taxonomy_precheck before the orchestrator cascade. Counts
    # as a freezing family — repeated hits escalate to a session
    # repeated_security_violation freeze at the configured threshold.
    "judge_malicious_forbidden": "judge_malicious_forbidden",
}

VALID_ACTORS: frozenset[str] = frozenset(
    {"operator", "agent", "subagent", "lane_worker"},
)

_DEFAULT_THRESHOLD = 3

# ── Gentle first strike for sensitive-READ families (backlog #302 bug 3,
# 2026-07-12) ────────────────────────────────────────────────────────────
# A FIRST blocked sensitive read is most often a probe the gate already
# refused — calling it "repeated" and threatening an imminent freeze was
# too aggressive. These families get an EFFECTIVE freeze threshold of at
# least 3 so the ladder is: attempt 1 = gentle warning (blocked &
# recorded; repeating escalates), attempt 2 = "one more will FREEZE",
# attempt 3 = freeze. EVERY attempt stays blocked and recorded — only
# the freeze is delayed one step. Threshold 0 (escalation disabled)
# stays disabled. Genuinely-malicious families (judge_malicious_forbidden,
# preflight_forbidden) are NOT in this set and keep the configured
# fast-freeze threshold.
_SENSITIVE_READ_FAMILIES: frozenset[str] = frozenset(
    {
        "sensitive_read",
        "blocked_sensitive_external",
        "sensitive_external_read",
    },
)
_SENSITIVE_READ_MIN_FREEZE_THRESHOLD = 3

_STRIKE_EVENT = "security_violation_strike"
# Session-scoped reset marker emitted by clear_freeze_service when an
# operator/admin clears a freeze. _count_strikes counts only strikes
# observed AFTER the latest marker, so a cleared session starts fresh.
_STRIKE_RESET_EVENT = "security_violation_reset"

# FRICTION families never accrue toward a freeze — they are workflow
# guidance, not security risk. Today that is only ``raw_shell_t0`` (the
# agent used a raw shell instead of ai_run). EVERYTHING ELSE still freezes
# on repetition: judge-caught malice (heuristic_judge / preflight_forbidden)
# AND the security-relevant policy families (sensitive_file_protection,
# blocked_sensitive_external, unknown_external, host-read refusals,
# command_read_intent[...] read-gate blocks). Repeatedly probing a secret /
# external / host path is a real signal and MUST be able to freeze.
# (Discovery nudges like indexed_file_gate / read_gate / managed-mode
# reroute never strike in the first place — see agent_orchestrator.)
_NON_FREEZING_FAMILIES: frozenset[str] = frozenset({"raw_shell_t0"})


@dataclass(frozen=True)
class ViolationOutcome:
    recorded: bool = False
    count: int = 0
    family: str = ""
    actor: str = ""
    lane_id: str = ""
    warning: bool = False  # 2nd-strike stern warning issued
    frozen: bool = False  # freeze created on this strike
    request_id: str = ""  # freeze/escalation id (when frozen)
    message: str = ""  # text to append to the deny reason

    @classmethod
    def noop(cls) -> ViolationOutcome:
        return cls()


_AGENT_KEY = "security.agent_security_violation_freeze_threshold"
_OPERATOR_KEY = "security.operator_forbidden_prompt_freeze_threshold"
_DEPRECATED_AGENT_KEY = "security.repeated_violation_freeze_threshold"


def _explicit_setting(key: str, project_root: Path, session_id: str | None = None):
    """Return the configured value ONLY when it was explicitly set at a
    non-factory layer (global/project/session); None when it falls back to
    the catalog default. The session layer is consulted when ``session_id``
    is supplied, so session-scoped dashboard config is honored. Lets the
    deprecated alias be detected as 'set' (factory default never counts).
    """
    try:
        from .config_resolver import LayeredConfigResolver

        rv = LayeredConfigResolver().resolve(
            key,
            project_root,
            session_id=session_id or None,
        )
        if rv.value is None:
            return None
        if rv.origin.get(key, "factory") == "factory":
            return None
        return rv.value
    except Exception:
        return None


def _coerce_threshold(v: Any, default: int) -> int:
    """Non-negative int. 0 = freeze escalation disabled (block+audit only)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(n, 0)


def _agent_threshold(project_root: Path, session_id: str | None = None) -> int:
    """New key wins if explicitly set (session > project > global); else the
    DEPRECATED alias if explicitly set at any layer; else the catalog
    default (3).
    """
    new = _explicit_setting(_AGENT_KEY, project_root, session_id)
    if new is not None:
        return _coerce_threshold(new, _DEFAULT_THRESHOLD)
    old = _explicit_setting(_DEPRECATED_AGENT_KEY, project_root, session_id)
    if old is not None:
        return _coerce_threshold(old, _DEFAULT_THRESHOLD)
    return _DEFAULT_THRESHOLD


def _operator_threshold(project_root: Path, session_id: str | None = None) -> int:
    try:
        from .config import get_setting

        v = get_setting(
            _OPERATOR_KEY,
            project_root=project_root,
            session_id=session_id or None,
            default=1,
        )
        return _coerce_threshold(v, 1)
    except Exception:
        return 1


def _format_attempt(tool_name: str, target: str) -> str:
    """Render a one-line evidence string for the WARN/freeze message.

    Empty when both fields are blank (call-sites pass it through an
    f-string so an empty return is a no-op). Keeps the lesson visible
    to the agent: 'you tried <tool> <target>'. Trims target so a long
    command/path doesn't blow up the deny envelope.
    """
    name = (tool_name or "").strip()
    tgt = (target or "").strip()
    if not name and not tgt:
        return ""
    if len(tgt) > 200:
        tgt = tgt[:197] + "..."
    if name and tgt:
        return f"\n  ↳ Last attempt: {name} → {tgt}"
    return f"\n  ↳ Last attempt: {name or tgt}"


class SecurityViolationService:
    """Stateful (via execution_events) strike counter + freeze escalator."""

    def __init__(self, hub: Any) -> None:
        self.hub = hub

    @staticmethod
    def _scope_key(actor: str, lane_id: str, family: str, agent_context_id: str = "") -> str:
        # agent_context_id (the DERIVED per-agent identity) makes the strike
        # COUNT per-AGENT: two agents on the same session accumulate (and
        # freeze) independently and cannot collide across host_kinds that share
        # a raw host_session_id string. '' = legacy / session-wide.
        return f"{actor}|{lane_id or ''}|{family}|{agent_context_id or ''}"

    def _approved_external_roots(self, project_root: Path) -> tuple[str, ...]:
        """Pull ``security.approved_external_roots`` from project config so
        unknown_external classification can demote operator-approved
        cross-project paths to FRICTION (cwd/bind mismatch) rather than
        SOFT/STRIKE (path traversal).

        Returns lowercased, forward-slash-normalized roots. Empty tuple
        on read failure — callers fall back to family defaults.
        """
        try:
            from .config import get_setting

            raw = get_setting(
                "security.approved_external_roots",
                project_root=project_root,
            )
        except Exception:
            return ()
        if not raw:
            return ()
        if isinstance(raw, str):
            raw = [raw]
        try:
            return tuple(str(r).replace("\\", "/").lower().rstrip("/") for r in raw if r)
        except Exception:
            return ()

    def get_recent_strikes(
        self,
        project_root: Path,
        session_id: str,
        *,
        agent_context_id: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent strikes for one canonical actor, newest first."""
        if not session_id:
            return []
        try:
            import json as _json

            self.hub.execution.init_db(project_root)
            where = (
                "WHERE event_kind = ? AND "
                "COALESCE(session_id, '') = COALESCE(?, '') "
            )
            params: list[object] = [_STRIKE_EVENT, session_id]
            if agent_context_id:
                where += "AND target_entity LIKE ? "
                params.append(f"%|{agent_context_id}")
            params.append(int(limit))
            with self.hub.execution.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT capability_name, target_entity, observed_at, payload_json "
                    "FROM execution_events "
                    + where
                    + "ORDER BY rowid DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload: dict[str, Any] = _json.loads(row["payload_json"] or "{}")
                except Exception:
                    payload = {}
                out.append(
                    {
                        "family": str(row["capability_name"] or ""),
                        "tool_name": str(payload.get("tool_name") or ""),
                        "target": str(payload.get("target") or ""),
                        "scope_key": str(row["target_entity"] or ""),
                        "observed_at": str(row["observed_at"] or ""),
                    },
                )
            return out
        except Exception:
            return []

    def _count_strikes(
        self,
        project_root: Path,
        session_id: str,
        scope_key: str,
        agent_context_id: str = "",
    ) -> int:
        """Count strikes for (session, scope_key), AFTER the most recent
        freeze-clear reset marker. The operator/admin clearing a freeze
        emits a session-scoped ``security_violation_reset`` event
        (clear_freeze_service); strikes observed at or before it no longer
        count, so a cleared session truly starts fresh instead of
        re-counting days-old strikes against a stable session id
        (2026-06-11 fix — previously the docstring claimed this but no
        marker existed). A fresh session id also resets (different scope).
        """
        try:
            self.hub.execution.init_db(project_root)
            with self.hub.execution.connect(project_root) as conn:
                # Lexical compare is valid: observed_at is ISO-8601 UTC.            with self.hub.execution.connect(project_root) as conn:
                # Use rowid (monotonic insertion order), NOT observed_at:
                # observed_at is second-granularity, so a strike in the same
                # second as the reset marker would be wrongly excluded by a
                # timestamp compare. rowid is collision-free. No reset marker
                # → MAX is NULL → COALESCE(0) → every strike rowid > 0 →
                # counts all (legacy behaviour preserved).
                # AGENT-scoped (operator 2026-07-15, agent-identity invariant):
                # count by target_entity (scope_key includes agent_context_id)
                # with NO session_id filter, so the strike ledger FOLLOWS the
                # agent across work sessions; the reset marker matches by
                # agent_context_id so a clear resets the agent's count from any
                # session. session_id stays in the signature but no longer
                # partitions the count.
                # RESET-MARKER REACH (#662 clause 3). Two marker shapes, two
                # reaches — an ACTOR-targeted marker follows that agent across
                # work sessions (so does its ledger, above); a SESSION-WIDE
                # marker (empty target) is bound to the session it was written
                # for. Previously the empty target matched with NO session
                # filter, so one session's freeze-clear silently absolved every
                # actor in every OTHER session in the project — a grant far
                # wider than any freeze scope (measured 2026-07-30). An empty
                # agent_context_id can no longer match an actor-targeted marker
                # either (the `<> ''` guard), so an unattributed count cannot
                # harvest a stranger's absolution.
                row = conn.execute(
                    "SELECT COUNT(*) FROM execution_events "
                    "WHERE event_kind = ? "
                    "AND target_entity = ? "
                    "AND rowid > COALESCE("
                    "  (SELECT MAX(rowid) FROM execution_events "
                    "   WHERE event_kind = ? "
                    "   AND ((target_entity <> '' AND target_entity = ?) "
                    "     OR (target_entity = '' "
                    "         AND COALESCE(session_id, '') = COALESCE(?, '')))), 0)",
                    (
                        _STRIKE_EVENT,
                        scope_key,
                        _STRIKE_RESET_EVENT,
                        agent_context_id or "",
                        session_id or None,
                    ),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def peak_strike_count(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "claude_code",
    ) -> tuple[int, int]:
        """(current strike count for the ACTING agent, threshold).

        ONE AUTHORITY (#664/#736). This is the DISPLAYED number; the
        ENFORCED number is ``_count_strikes``. They used to be two
        different queries over two different scopes — the enforcing one
        agent-scoped and cross-session (#588 D1: a freeze binds the
        ACTOR, so the ledger FOLLOWS the agent across work sessions),
        the displayed one session-filtered — so an agent whose strikes
        spanned two sessions was frozen at 3 while being shown 1/3, and
        an agent with no resolvable host session was shown the WORST
        SIBLING's count in its session.

        Now the display is DERIVED FROM THE ENFORCING FUNCTION: we only
        enumerate which scope keys belong to the acting agent, then ask
        ``_count_strikes`` — the freeze's own authority — for each one.
        The peak across the agent's families is the ratchet (the agent's
        WORST family, reset-marker-aware).

        Actor axis: scope_key is ``actor|lane|family|agent_context_id``,
        so an agent owns exactly the keys ending in ``"|" + acid``. An
        UNATTRIBUTED caller (no resolvable host_session_id, acid='')
        owns exactly the unattributed keys — never a sibling's. Empty
        no longer means "session-wide max".

        Returns (0, threshold) when clean.
        """
        if not session_id:
            return (0, 0)
        threshold = _agent_threshold(project_root, session_id)
        acid = ""
        if host_session_id:
            try:
                acid = derive_agent_context_id(
                    host_kind=host_kind, project_root=project_root,
                    host_session_id=host_session_id,
                    agent_id=_calling_agent_id(),
                )
            except Exception:
                acid = ""
        suffix = "|" + acid
        try:
            self.hub.execution.init_db(project_root)
            with self.hub.execution.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT target_entity FROM execution_events "
                    "WHERE event_kind = ?",
                    (_STRIKE_EVENT,),
                ).fetchall()
            keys = [
                str(r[0] or "")
                for r in rows
                if str(r[0] or "").endswith(suffix)
            ]
            peak = 0
            for key in keys:
                peak = max(
                    peak,
                    self._count_strikes(project_root, session_id, key, acid),
                )
        except Exception:
            peak = 0
        return (peak, threshold)

    def _emit(self, project_root: Path, **kw: Any) -> None:
        try:
            self.hub.execution.record_event(project_root, **kw)
        except Exception:
            pass

    def reset_strikes(
        self,
        project_root: Path,
        session_id: str,
        *,
        host_session_id: str = "",
        host_kind: str = "",
        reason: str = "epoch_change",
        actor: str = "agent",
    ) -> bool:
        """Emit a session-scoped strike RESET marker so ``_count_strikes``
        starts fresh (it counts only strikes recorded AFTER the latest marker).

        Boundaries kept deliberately tight: this does NOT lift an active freeze
        (a real freeze stays admin-clear-only via clear_freeze_service) and does
        NOT erase the recorded violation events -- the full audit trail stays
        intact; only the count that drives the NEXT freeze threshold resets.

        Used on compaction (agent_memory_epoch change): a compacted agent is
        effectively a fresh mind, so the freeze threshold starts over. Returns
        True when a marker was written.
        """
        if not session_id:
            return False
        # Identity symmetry with record_violation (War 1 attribution): the
        # strike ROWS are keyed by the agent_context_id derived from the
        # RESOLVED calling host_kind (explicit param > request/process
        # context > 'unknown'). The reset marker must derive with the SAME
        # resolution or a compacting agent's own strikes are never reset
        # (marker lands under a foreign agent_context_id). Only the kind
        # axis is resolved here — host_session_id keeps its explicit
        # semantics ('' = session-wide reset marker).
        if not host_kind:
            from .freeze_service import _resolve_freeze_actor_identity

            _, host_kind = _resolve_freeze_actor_identity("", "")
        agent_context_id = derive_agent_context_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            agent_id=_calling_agent_id(),
        )
        # #879 B5 / THE LAW (IDENTITY HAS NO FALLBACK). A caller that NAMED a
        # host but whose actor key would not derive must NOT fall through to
        # target_entity='' -- that is the SESSION-WIDE marker, and it would
        # absolve every actor in the session on the strength of an identity we
        # just failed to resolve. Only an explicitly EMPTY host_session_id
        # asks for the session-wide reset; anything else refuses.
        if host_session_id and not agent_context_id:
            return False
        try:
            self._emit(
                project_root,
                event_kind=_STRIKE_RESET_EVENT,
                source_kind="security_violation",
                session_id=session_id,
                capability_name="strike_reset",
                action_kind="reset",
                # target_entity = the DERIVED agent_context_id makes the reset
                # PER-AGENT ('' = session-wide / legacy). _count_strikes matches
                # target_entity IN ('', agent_context_id), so an agent's reset
                # clears only its own count (or '' clears everyone).
                target_entity=str(agent_context_id or "")[:300],
                status="cleared",
                event_id=f"secreset-{uuid.uuid4().hex}",
                payload={"reason": reason, "actor": actor, "host_session_id": host_session_id},
            )
            return True
        except Exception:
            return False

    # Markers only a USER-typed local command can put into a UserPromptSubmit
    # payload: Claude Code wraps `!`/slash-command output in these blocks
    # inside a USER message. Agent-run shell produces tool results, never a
    # user message, so an agent cannot forge this signal.
    _LOCAL_CMD_MARKERS = ("<local-command-stdout>", "<bash-stdout>", "<local-command-caveat>")

    @classmethod
    def prompt_shows_local_clear_freeze(cls, prompt: str) -> bool:
        """True iff `prompt` (a raw UserPromptSubmit text) carries a user-typed
        local-command block whose content is a clear-freeze invocation/receipt."""
        p = str(prompt or "")
        if not any(m in p for m in cls._LOCAL_CMD_MARKERS):
            return False
        return ("clear-freeze" in p) or ("clear_freeze" in p)

    def void_self_cancel_after_local_clear(
        self,
        project_root: Path,
        *,
        session_id: str,
        host_session_id: str = "",
        host_kind: str = "",
    ) -> bool:
        """Void a self_cancel strike that was mis-attributed to the agent
        because the OPERATOR ran `! aidocs admin clear-freeze ...` (fix 2 of
        the 2026-07-16 mis-strike bug; operator chose BOTH fixes).

        From inside the CLI process a user-typed `!` command is byte-identical
        to agent-run shell (same OS user/env, no TTY), so the CLI classifies
        it agent_self (never under-strike). The distinguishing signal exists
        only HOST-side: the command's output arrives in the NEXT user prompt
        as a local-command block — something agent-run shell can never
        produce. When that receipt is seen, this reclassifies the clear as
        the operator action it was: audit a strike-void row, then emit the
        SAME per-agent reset marker an operator-origin clear_with_audit would
        have emitted. Fires only when a recent self_cancel strike (within the
        agent's last recorded strikes) exists for this agent — an unrelated paste mentioning clear-freeze
        resets nothing.
        """
        if not session_id:
            return False
        # Same identity resolution as reset_strikes: the strike rows are keyed
        # by the derived agent_context_id, so the existence probe must derive
        # identically or it looks at a foreign agent's ledger.
        if not host_kind:
            from .freeze_service import _resolve_freeze_actor_identity

            _, host_kind = _resolve_freeze_actor_identity("", "")
        agent_context_id = derive_agent_context_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            agent_id=_calling_agent_id(),
        )
        try:
            recent = self.get_recent_strikes(
                project_root,
                session_id,
                agent_context_id=str(agent_context_id or ""),
            )
        except Exception:
            return False
        if not any(str(r.get("family") or "") == "self_cancel" for r in recent or []):
            return False
        try:
            self._emit(
                project_root,
                event_kind="security_violation_strike_voided",
                source_kind="security_violation",
                session_id=session_id,
                capability_name="strike_void",
                action_kind="reset",
                target_entity="self_cancel",
                status="voided",
                event_id=f"secvoid-{uuid.uuid4().hex}",
                payload={
                    "family": "self_cancel",
                    "reason": "operator_local_command_clear_freeze",
                    "host_session_id": host_session_id,
                },
            )
        except Exception:
            pass
        return self.reset_strikes(
            project_root,
            session_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
            reason="operator_local_command_clear_freeze",
            actor="agent",
        )

    def record_and_escalate(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str = "",
        target: str = "",
        tool_name: str = "",
        threshold: int | None = None,
        freeze_kind: str | None = None,
        host_session_id: str = "",
        user_id: str = "",
        host_kind: str = "",
    ) -> ViolationOutcome:
        """Record one security strike and escalate per threshold.

        ``threshold`` semantics (default: the AGENT threshold from config):
          0 = freeze escalation DISABLED — still records + audits, never
              freezes; N >= 1 = freeze on the Nth strike (warning on the
              (N-1)th when N >= 2).
        ``freeze_kind`` defaults to repeated_security_violation (agent path);
        the operator path passes hostile_operator_prompt.

        Returns a ViolationOutcome whose ``message`` the caller appends to
        the deny reason. A missing session_id (unmanaged) is a no-op — the
        freeze is session-scoped, so there is nothing to lock.

        Decomposed 2026-07-19 (#413 tranche D): this orchestrator pins the
        escalation-ladder ORDER — friction → retry-amnesty → strike row →
        immediate-freeze → non-freezing → threshold-freeze → warning →
        first-strike — and each ``_rae_*`` stage helper owns one rung.
        """
        if not session_id or not family:
            return ViolationOutcome.noop()
        actor = actor if actor in VALID_ACTORS else "agent"
        from .freeze_service import _resolve_freeze_actor_identity

        host_session_id, host_kind = _resolve_freeze_actor_identity(
            host_session_id,
            host_kind,
        )
        if threshold is None:
            threshold = _agent_threshold(project_root, session_id)
        # Gentle first strike (backlog #302 bug 3): sensitive-READ
        # families get a freeze ladder one step longer — the block
        # itself is unchanged; only the freeze arrives on attempt 3
        # instead of 2. 0 = escalation disabled is honored.
        if threshold >= 1 and family in _SENSITIVE_READ_FAMILIES:
            threshold = max(threshold, _SENSITIVE_READ_MIN_FREEZE_THRESHOLD)
        from .session_freeze_store import KIND_REPEATED_SECURITY_VIOLATION

        freeze_kind = freeze_kind or KIND_REPEATED_SECURITY_VIOLATION
        agent_context_id = derive_agent_context_id(
            host_kind=host_kind,
            project_root=project_root,
            host_session_id=host_session_id,
            agent_id=_calling_agent_id(),
        )
        scope_key = self._scope_key(actor, lane_id, family, agent_context_id)

        # ── Severity classification (2026-05-27) ──────────────────────
        # Determine the tier BEFORE emitting the strike event so
        # friction-class blocks don't pollute the strike trail.
        from .violation_severity import (
            SEVERITY_FRICTION,
            SEVERITY_IMMEDIATE_FREEZE,
            classify_severity,
        )

        related_roots = self._approved_external_roots(project_root)
        severity = classify_severity(
            family,
            target=target,
            related_roots=related_roots,
        )

        if severity == SEVERITY_FRICTION:
            return self._rae_friction_outcome(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                target=target,
                tool_name=tool_name,
                severity=severity,
            )

        if actor == "agent" and severity != SEVERITY_IMMEDIATE_FREEZE:
            amnesty = self._rae_retry_amnesty_outcome(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                target=target,
                tool_name=tool_name,
                severity=severity,
            )
            if amnesty is not None:
                return amnesty

        count = self._rae_record_strike_row(
            project_root,
            session_id=session_id,
            family=family,
            actor=actor,
            lane_id=lane_id,
            target=target,
            tool_name=tool_name,
            severity=severity,
            scope_key=scope_key,
            agent_context_id=agent_context_id,
            host_session_id=host_session_id,
            host_kind=host_kind,
            threshold=threshold,
        )

        if severity == SEVERITY_IMMEDIATE_FREEZE:
            return self._rae_immediate_freeze_outcome(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                target=target,
                tool_name=tool_name,
                severity=severity,
                scope_key=scope_key,
                count=count,
                freeze_kind=freeze_kind,
                host_session_id=host_session_id,
                host_kind=host_kind,
                user_id=user_id,
            )

        # FRICTION families never freeze (back-compat: kept for any
        # caller that classifies into _NON_FREEZING_FAMILIES directly
        # rather than going through classify_severity. The friction
        # branch above is the primary path.)
        if family in _NON_FREEZING_FAMILIES:
            return ViolationOutcome(
                recorded=True,
                count=count,
                family=family,
                actor=actor,
                lane_id=lane_id or "",
                message="",
            )

        # threshold 0 → freeze escalation disabled: block + audit only.
        if threshold >= 1 and count >= threshold:
            return self._rae_threshold_freeze_outcome(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                target=target,
                tool_name=tool_name,
                scope_key=scope_key,
                count=count,
                freeze_kind=freeze_kind,
                host_session_id=host_session_id,
                host_kind=host_kind,
                user_id=user_id,
            )

        if threshold >= 2 and count == threshold - 1:
            return self._rae_warning_outcome(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                target=target,
                tool_name=tool_name,
                scope_key=scope_key,
                count=count,
                threshold=threshold,
            )

        return self._rae_first_strike_outcome(
            family=family,
            actor=actor,
            lane_id=lane_id,
            target=target,
            tool_name=tool_name,
            severity=severity,
            count=count,
        )

    def _rae_friction_outcome(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        severity: str,
    ) -> ViolationOutcome:
        """FRICTION: routing block, NO strike row recorded.

        Emits a ``friction_routing_block`` audit event so dashboards can
        surface noisy redirects, but the strike counter stays put and the
        deny envelope carries a friendly "use the governed path" message
        instead of "SECURITY STRIKE." (Extracted from record_and_escalate,
        #413 tranche D — behavior unchanged.)
        """
        from .violation_severity import friction_redirect

        self._emit(
            project_root,
            event_kind="friction_routing_block",
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="redirect",
            target_entity=(target or "")[:300],
            status="blocked",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "target": (target or "")[:300],
                "tool_name": tool_name,
                "severity": severity,
            },
        )
        return ViolationOutcome(
            recorded=False,
            count=0,
            family=family,
            actor=actor,
            lane_id=lane_id or "",
            message=friction_redirect(family, target=target),
        )

    def _rae_retry_amnesty_outcome(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        severity: str,
    ) -> ViolationOutcome | None:
        """System-instructed retry amnesty (#172, 2026-07-03).

        Incident class: the operator APPROVES a one-shot retry, the grant
        is consumed, and the retried action is then hard-refused by a
        DIFFERENT gate. Striking the agent for doing exactly what the
        system instructed — and leaving the original escalation open to
        re-arm a freeze on a spent one-shot — is the P0 deadlock. When the
        most recent grant-consume for this session is FRESH and UN-SPENT,
        this refusal is the downstream VERDICT on the approved retry:
        record it (audited, action_kind marks it), count NO strike, and
        CLOSE the originating escalation with the verdict. Single-use per
        consume; immediate-freeze severities are never amnestied (the
        caller enforces that precondition). Returns None when no fresh
        un-spent retry grant exists — the ladder proceeds to a strike.
        (Extracted from record_and_escalate, #413 tranche D.)
        """
        _retry = self._system_instructed_retry(project_root, session_id)
        if _retry is None:
            return None
        _req_id = str(_retry.get("request_id") or "")
        _closed = False
        if _req_id:
            try:
                from .escalation_store import EscalationStore

                _closed = (
                    EscalationStore().consume(project_root, _req_id) is not None
                )
            except Exception:
                _closed = False
        self._emit(
            project_root,
            event_kind="security_violation_recorded",
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="system_instructed_retry_refused",
            target_entity=(target or "")[:300],
            status="blocked",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "target": (target or "")[:300],
                "tool_name": tool_name,
                "severity": severity,
                "strike_counted": False,
                "grant_id": str(_retry.get("grant_id") or ""),
                "request_id": _req_id,
                "escalation_closed": _closed,
            },
        )
        return ViolationOutcome(
            recorded=True,
            count=0,
            family=family,
            actor=actor,
            lane_id=lane_id or "",
            message=(
                f"⛔ blocked ({family}) — but this was the operator-approved "
                f"retry, refused DOWNSTREAM by a different gate. NO security "
                f"strike counted (system-instructed retry)"
                + (
                    f"; escalation {_req_id} is CLOSED with this verdict"
                    if _closed
                    else ""
                )
                + ". Do not re-attempt the action — report the downstream "
                "refusal to the operator instead."
            ),
        )

    def _rae_record_strike_row(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        severity: str,
        scope_key: str,
        agent_context_id: str,
        host_session_id: str,
        host_kind: str,
        threshold: int,
    ) -> int:
        """SOFT / STRIKE / IMMEDIATE_FREEZE: record the strike row.

        Severity is in the payload so soft strikes can be filtered from
        the trail UI separately from hard strikes. Also enqueues the
        freeze-strike NOTIFICATION notice and emits the full-detail
        (not counted) ``security_violation_recorded`` audit event.
        Returns the post-record strike count for this scope key.
        (Extracted from record_and_escalate, #413 tranche D.)
        """
        self._emit(
            project_root,
            event_kind=_STRIKE_EVENT,
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="strike",
            target_entity=scope_key,
            status="blocked",
            event_id=f"secstrike-{uuid.uuid4().hex}",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "target": (target or "")[:300],
                "tool_name": tool_name,
                "severity": severity,
            },
        )
        count = self._count_strikes(project_root, session_id, scope_key, agent_context_id)

        # Freeze-strike visibility (operator directive 2026-07-15): surface EVERY
        # recorded strike on the NOTIFICATION rail (5 displays, then auto-drops)
        # instead of the old per-prompt UPS strike-note (removed from
        # hook_pipeline) — the agent is told, then it stops nagging. Single point
        # for ALL strike families (self-cancel included, via clear_freeze_service
        # → record_and_escalate). The displayed number is peak_strike_count (the
        # agent's WORST family, reset-marker-aware) not this one family's count —
        # so after a self-cancel that did NOT reset, the notice shows the agent
        # still at the ceiling (the ratchet). Fail-quiet: never breaks the strike.
        try:
            from . import freeze_strike_notice_store as _fsn

            _peak, _thr = self.peak_strike_count(
                project_root,
                session_id,
                host_session_id=host_session_id,
                host_kind=host_kind,
            )
            _fsn.enqueue_strike_notice(
                project_root,
                session_id,
                count=_peak or count,
                threshold=_thr or threshold,
                family=family,
                origin=family,
                agent_context_id=agent_context_id,
                # #879 B3: ALSO record the actor's own CONVERSATION key.
                # `agent_context_id` above is the SUBAGENT key on the hook
                # path, and every drain path runs on the MCP transport, which
                # cannot carry `agent_id` at all -- so such a row matched no
                # reader, was never surfaced, never counted, and therefore
                # never hit the `surfaced_count == 3` prune: permanent.
                # Both keys are resolved and stored; nothing is substituted.
                conversation_agent_context_id=derive_agent_context_id(
                    host_kind=host_kind,
                    project_root=project_root,
                    host_session_id=host_session_id,
                ),
            )
        except Exception:
            pass

        # Always audit the recorded violation (full detail; not counted).
        self._emit(
            project_root,
            event_kind="security_violation_recorded",
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="recorded",
            target_entity=(target or "")[:300],
            status="blocked",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "target": (target or "")[:300],
                "tool_name": tool_name,
                "count": count,
                "severity": severity,
            },
        )
        return count

    def _rae_immediate_freeze_outcome(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        severity: str,
        scope_key: str,
        count: int,
        freeze_kind: str,
        host_session_id: str,
        host_kind: str,
        user_id: str,
    ) -> ViolationOutcome:
        """IMMEDIATE_FREEZE: skip the threshold check and freeze on first hit.

        For attempts that can't be charitable (release-trust tamper,
        doctrine disable, lane escape, etc.) the "two chances" mechanism
        is the wrong shape. (Extracted from record_and_escalate, #413
        tranche D — behavior unchanged.)
        """
        request_id = self._create_freeze(
            project_root,
            session_id=session_id,
            family=family,
            actor=actor,
            lane_id=lane_id,
            count=count,
            kind=freeze_kind,
            host_session_id=host_session_id,
            host_kind=host_kind,
            user_id=user_id,
        )
        self._emit(
            project_root,
            event_kind="security_violation_freeze_created",
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="freeze",
            target_entity=request_id or scope_key,
            status="frozen",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "count": count,
                "request_id": request_id,
                "tool_name": tool_name,
                "freeze_kind": freeze_kind,
                "severity": severity,
                "immediate": True,
            },
        )
        attempted = _format_attempt(tool_name, target)
        return ViolationOutcome(
            recorded=True,
            count=count,
            family=family,
            actor=actor,
            lane_id=lane_id or "",
            frozen=True,
            request_id=request_id,
            message=(
                f"🛑 SESSION LOCKED — immediate freeze "
                f"({family}). This attempt class cannot be retried; "
                f"the session is frozen on the first hit. Only "
                f"operator/admin recovery can clear it. "
                f"Freeze id: {request_id}. Clear with "
                f'aidocs admin clear-freeze --freeze-id {request_id} --reason <why>  (operator/admin CLI; the frozen agent cannot run this).'
                f"{attempted}"
            ),
        )

    def _rae_threshold_freeze_outcome(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        scope_key: str,
        count: int,
        freeze_kind: str,
        host_session_id: str,
        host_kind: str,
        user_id: str,
    ) -> ViolationOutcome:
        """Nth-strike freeze: the threshold is met — lock the session.

        (Extracted from record_and_escalate, #413 tranche D — behavior
        unchanged.)
        """
        request_id = self._create_freeze(
            project_root,
            session_id=session_id,
            family=family,
            actor=actor,
            lane_id=lane_id,
            count=count,
            kind=freeze_kind,
            host_session_id=host_session_id,
            host_kind=host_kind,
            user_id=user_id,
        )
        self._emit(
            project_root,
            event_kind="security_violation_freeze_created",
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="freeze",
            target_entity=request_id or scope_key,
            status="frozen",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "count": count,
                "request_id": request_id,
                "tool_name": tool_name,
                "freeze_kind": freeze_kind,
            },
        )
        # LEARNING SURFACE (2026-05-27 step 2): include WHAT the
        # agent just tried so the deny envelope teaches the lesson
        # alongside the lock notice. Without this the agent only
        # sees "you got frozen" with no causal trace, and the next
        # session repeats the same probe.
        attempted = _format_attempt(tool_name, target)
        if freeze_kind == "hostile_operator_prompt":
            msg = (
                f"🛑 SESSION LOCKED — hostile operator prompt "
                f"({family}). The session is frozen; only operator/admin "
                f"recovery can clear it. Freeze id: {request_id}. Clear with "
                f'aidocs admin clear-freeze --freeze-id {request_id} --reason <why>  (operator/admin CLI; the frozen agent cannot run this).'
            )
        else:
            msg = (
                f"🛑 SESSION LOCKED — {count} security violations "
                f"({family}). This session is now frozen; only "
                f"operator/admin recovery can clear it. The agent "
                f"cannot lift this lock by prompt. Freeze id: {request_id}. "
                f'Clear with aidocs admin clear-freeze --freeze-id {request_id} --reason <why>  (operator/admin CLI; the frozen agent cannot run this).'
                f"{attempted}"
            )
        return ViolationOutcome(
            recorded=True,
            count=count,
            family=family,
            actor=actor,
            lane_id=lane_id or "",
            frozen=True,
            request_id=request_id,
            message=msg,
        )

    def _rae_warning_outcome(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        scope_key: str,
        count: int,
        threshold: int,
    ) -> ViolationOutcome:
        """(threshold-1)th strike: emit the threshold warning.

        LEARNING SURFACE (2026-05-27 step 2): the 2nd-strike WARN that
        goes back to the AGENT must include the action it just attempted
        — that's the lesson. Without the attempt the agent learns "I got
        warned" but not "this exact verb-and-target is the boundary."
        (Extracted from record_and_escalate, #413 tranche D.)
        """
        self._emit(
            project_root,
            event_kind="security_violation_threshold_warning",
            source_kind="security_violation",
            session_id=session_id,
            capability_name=family,
            action_kind="warning",
            target_entity=scope_key,
            status="blocked",
            payload={
                "family": family,
                "actor": actor,
                "lane_id": lane_id or "",
                "count": count,
                "threshold": threshold,
                "tool_name": tool_name,
            },
        )
        attempted = _format_attempt(tool_name, target)
        return ViolationOutcome(
            recorded=True,
            count=count,
            family=family,
            actor=actor,
            lane_id=lane_id or "",
            warning=True,
            message=(
                f"⚠️ SECURITY WARNING — repeated blocked attempt "
                f"({family}, strike {count}/{threshold}). One more "
                f"will FREEZE this session and require operator/admin "
                f"recovery. Stop attempting to bypass the read/security "
                f"gates."
                f"{attempted}"
            ),
        )

    def _rae_first_strike_outcome(
        self,
        *,
        family: str,
        actor: str,
        lane_id: str,
        target: str,
        tool_name: str,
        severity: str,
        count: int,
    ) -> ViolationOutcome:
        """First-strike catch (count=1 with threshold>=2 — and any count
        when threshold==0 / freeze disabled).

        Surface the attempt now so the agent SEES what got blocked, not
        just that *something* got blocked. (2026-05-27 step 2.)
        SOFT-severity gets softer copy ("doctrine warning") and a
        different glyph so the agent can tell apart "suspicious" from
        "real strike." Both still record an event row; the threshold-
        based freeze still applies on cross-turn repeat.
        (Extracted from record_and_escalate, #413 tranche D.)
        """
        from .violation_severity import SEVERITY_SOFT

        attempted = _format_attempt(tool_name, target)
        if severity == SEVERITY_SOFT:
            lead = (
                f"⚠️ DOCTRINE WARNING — {family} (soft strike {count}). "
                f"This attempt is blocked; the gate is unsure whether "
                f"it's intentional. Repeating it without changing the "
                f"approach will promote to a hard strike."
            )
        elif family in _SENSITIVE_READ_FAMILIES:
            # Gentle first-strike copy (backlog #302 bug 3): warning-
            # toned, no imminent-freeze threat — that copy is reserved
            # for the (threshold-1)th attempt above.
            lead = (
                f"⚠️ SECURITY WARNING — {family} (strike {count}). "
                f"This attempt is blocked and recorded; repeating it "
                f"will escalate toward a session freeze."
            )
        else:
            lead = (
                f"⛔ SECURITY STRIKE — {family} (strike {count}). "
                f"This attempt is blocked and recorded."
            )
        return ViolationOutcome(
            recorded=True,
            count=count,
            family=family,
            actor=actor,
            lane_id=lane_id or "",
            message=(f"{lead}{attempted}" if attempted else lead),
        )


    def freeze_operator_forbidden_prompt(
        self,
        project_root: Path,
        *,
        session_id: str,
        rule_ids: str = "",
    ) -> ViolationOutcome:
        """Escalate a FORBIDDEN operator UserPromptSubmit verdict.

        Doctrine split from the agent ladder: a UPS is judged BEFORE the
        agent sees it, so the operator default threshold is 1 (immediate
        freeze on the first forbidden prompt). The threshold is its own
        dashboard setting (security.operator_forbidden_prompt_freeze_
        threshold): 0 = block + audit but never freeze; 1 = immediate; N>=2
        = optional ladder. Delegates to record_and_escalate with the
        operator threshold + hostile_operator_prompt freeze kind.
        """
        from .session_freeze_store import KIND_HOSTILE_OPERATOR_PROMPT

        # Operator-wide freeze (security.freeze_all_sessions_on_malicious_intent):
        # resolve the AUTHENTICATED operator identity at mint so the freeze can be
        # matched across ALL the operator's sessions and cleared operator-wide.
        # Best-effort: "" when there is no login (per-session freeze, today's
        # behavior). Same resolver the gate authorizes by.
        user_id = ""
        try:
            from .project_authority import _authenticated_uid

            user_id = _authenticated_uid(project_root)
        except Exception:
            user_id = ""

        return self.record_and_escalate(
            project_root,
            session_id=session_id,
            family="preflight_forbidden",
            actor="operator",
            target=rule_ids,
            tool_name="UserPromptSubmit",
            threshold=_operator_threshold(project_root, session_id),
            freeze_kind=KIND_HOSTILE_OPERATOR_PROMPT,
            user_id=user_id,
        )

    # Retry window: a consume older than this cannot amnesty a violation —
    # it shields the IMMEDIATE downstream verdict, never later misbehavior.
    _RETRY_AMNESTY_WINDOW_S = 180

    def _system_instructed_retry(self, project_root: Path, session_id: str) -> dict | None:
        """The most recent escalation-grant consume for this session, iff it
        is fresh (within the retry window) and UN-SPENT (no strike and no
        prior amnesty recorded after it). Returns the consume payload
        ({grant_id, request_id}) or None. One amnesty per consume — enforced
        by ordering, not bookkeeping: the amnesty event itself spends it."""
        import datetime as _dt
        import json as _json

        try:
            self.hub.execution.init_db(project_root)
            with self.hub.execution.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT rowid, observed_at, payload_json FROM execution_events "
                    "WHERE event_kind IN ('escalation_grant_consumed', 'escalation_consumed') "
                    "AND COALESCE(session_id, '') = COALESCE(?, '') "
                    "ORDER BY rowid DESC LIMIT 1",
                    (session_id or None,),
                ).fetchone()
                if not row:
                    return None
                consume_rowid, observed_at, payload_raw = row[0], row[1], row[2]
                # Freshness: the amnesty covers the immediate retry only.
                try:
                    seen = _dt.datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
                    age = (_dt.datetime.now(_dt.timezone.utc) - seen).total_seconds()
                    if age > self._RETRY_AMNESTY_WINDOW_S:
                        return None
                except (ValueError, TypeError):
                    return None  # unparseable timestamp → fail closed (no amnesty)
                # Spent? Any strike OR prior amnesty AFTER the consume.
                spent = conn.execute(
                    "SELECT COUNT(*) FROM execution_events WHERE rowid > ? "
                    "AND COALESCE(session_id, '') = COALESCE(?, '') "
                    "AND (event_kind = ? OR action_kind = 'system_instructed_retry_refused')",
                    (consume_rowid, session_id or None, _STRIKE_EVENT),
                ).fetchone()
                if spent and int(spent[0]) > 0:
                    return None
                try:
                    payload = _json.loads(payload_raw) if payload_raw else {}
                except (ValueError, TypeError):
                    payload = {}
                return {
                    "grant_id": str(payload.get("grant_id") or ""),
                    "request_id": str(payload.get("request_id") or ""),
                }
        except Exception:
            return None  # fail closed: no amnesty on any lookup error

    def _create_freeze(
        self,
        project_root: Path,
        *,
        session_id: str,
        family: str,
        actor: str,
        lane_id: str,
        count: int,
        kind: str | None = None,
        host_session_id: str = "",
        host_kind: str = "",
        user_id: str = "",
    ) -> str:
        """Create an admin-clear-only freeze + an escalation row (so the
        documented admin recovery commands work). Returns request_id.

        ``kind`` defaults to the repeated-violation lockdown; the operator
        immediate-freeze path passes ``hostile_operator_prompt``.
        """
        from .session_freeze_store import (
            FREEZE_SCOPE_ACTOR,
            FREEZE_SCOPE_SESSION,
            KIND_REPEATED_SECURITY_VIOLATION,
            SessionFreezeStore,
        )
        from .verdict_class import CLASS_SECURITY_FREEZE

        freeze_kind = kind or KIND_REPEATED_SECURITY_VIOLATION
        phrase = f"{freeze_kind}:{family}"
        request_id = ""
        try:
            from .escalation_store import EscalationStore

            req = EscalationStore().create_request(
                project_root,
                requester_label=f"security-violation-{actor}",
                gate_permission="clear_security_freeze",
                gate_phrase=phrase,
                session_id=session_id or None,
                command_snippet=(
                    f"{freeze_kind}: {family} (actor={actor}, lane={lane_id or '-'}, count={count})"
                ),
            )
            request_id = req.request_id
        except Exception:
            request_id = f"secviol-{uuid.uuid4().hex[:12]}"
        # #588 D1: name the ACTOR before latching, through the ONE identity
        # authority (#587), so a lockdown earned by one subagent does not
        # stop its siblings and the conductor. This producer used to hand
        # `set_freeze` whatever host identity its caller happened to have —
        # frequently nothing — and an empty key was silently a SESSION-WIDE
        # row.
        #
        # WHEN THE ACTOR CANNOT BE NAMED this path deliberately does NOT
        # refuse the way the confirm path does: a repeated-violation /
        # hostile-prompt lockdown that silently failed to latch would be a
        # weakening, and `except Exception: pass` below would have hidden it.
        # It falls back to a DECLARED session scope instead — the same reach
        # it has always had, now recorded as a decision rather than implied
        # by a missing id.
        #
        # #879 B1: the actor is now named on ALL THREE axes. The strike
        # LEDGER has been per-subagent since 2026-08-22 (`agent_id` at the
        # scope key above); this producer resolved only the conversation, so
        # a subagent crossed the threshold under its OWN key and latched the
        # lockdown under its PARENT's — which matched every sibling and the
        # conductor. The comment above promised exactly the opposite of what
        # the code did.
        from .freeze_service import resolve_freeze_actor

        _actor_host, _actor_kind, _actor_agent_id = resolve_freeze_actor(
            host_session_id,
            host_kind,
            project_root=project_root,
        )
        # #879 B5: the scope is decided on what actually KEYS, not on the
        # host alone. A host_session_id whose KIND cannot be named derives the
        # EMPTY actor id, and asking for FREEZE_SCOPE_ACTOR with an empty key
        # makes set_freeze raise UnattributableFreeze — which the bare
        # `except Exception: pass` below would swallow, silently losing a
        # repeated-violation lockdown entirely. Before #879 that never
        # surfaced because the kind was FABRICATED into the literal "unknown",
        # which keyed fine and bound the wrong actor.
        _freeze_scope = (
            FREEZE_SCOPE_ACTOR
            if (_actor_host and _actor_kind)
            else FREEZE_SCOPE_SESSION
        )
        try:
            SessionFreezeStore().set_freeze(
                project_root,
                session_id=session_id,
                request_id=request_id,
                fingerprint_phrase=phrase,
                kind=freeze_kind,
                host_session_id=_actor_host,
                host_kind=_actor_kind,
                agent_id=_actor_agent_id,
                user_id=user_id,
                scope=_freeze_scope,
                # #571: this producer only ever mints rung-1 lockdowns
                # (repeated_security_violation / hostile_operator_prompt).
                # Stamp the class explicitly so a conductor-side clear can
                # positively identify it as security-class rather than relying
                # on the fail-closed default.
                verdict_class=CLASS_SECURITY_FREEZE,
            )
        except Exception:
            pass
        return request_id
