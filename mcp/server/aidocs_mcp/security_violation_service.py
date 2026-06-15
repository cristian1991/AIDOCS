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
    def _scope_key(actor: str, lane_id: str, family: str) -> str:
        return f"{actor}|{lane_id or ''}|{family}"

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
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the recent strike rows for a session, newest first.

        Used by build_existing_freeze_response to enrich the freeze
        deny envelope with the audit trail of what the agent tried
        before getting frozen. Empty list on any error / no session.
        Each row is a dict with: family, tool_name, target, count.
        """
        if not session_id:
            return []
        try:
            import json as _json

            self.hub.execution.init_db(project_root)
            with self.hub.execution.connect(project_root) as conn:
                # ORDER BY rowid DESC for deterministic insertion-
                # order recency: strikes in quick succession share
                # an observed_at second, and event_id is a random
                # uuid4 (not monotonic), so observed_at + event_id
                # tied break arbitrarily. rowid is sqlite's
                # insertion-order surrogate — newest row always last.
                rows = conn.execute(
                    "SELECT capability_name, target_entity, observed_at, "
                    "payload_json FROM execution_events "
                    "WHERE event_kind = ? AND "
                    "COALESCE(session_id, '') = COALESCE(?, '') "
                    "ORDER BY rowid DESC "
                    "LIMIT ?",
                    (_STRIKE_EVENT, session_id, int(limit)),
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                pl: dict[str, Any] = {}
                try:
                    pl = _json.loads(r["payload_json"] or "{}")
                except Exception:
                    pl = {}
                out.append(
                    {
                        "family": str(r["capability_name"] or ""),
                        "tool_name": str(pl.get("tool_name") or ""),
                        "target": str(pl.get("target") or ""),
                        "scope_key": str(r["target_entity"] or ""),
                        "observed_at": str(r["observed_at"] or ""),
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
                row = conn.execute(
                    "SELECT COUNT(*) FROM execution_events "
                    "WHERE event_kind = ? "
                    "AND COALESCE(session_id, '') = COALESCE(?, '') "
                    "AND target_entity = ? "
                    "AND rowid > COALESCE("
                    "  (SELECT MAX(rowid) FROM execution_events "
                    "   WHERE event_kind = ? "
                    "   AND COALESCE(session_id, '') = COALESCE(?, '')), 0)",
                    (
                        _STRIKE_EVENT,
                        session_id or None,
                        scope_key,
                        _STRIKE_RESET_EVENT,
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
    ) -> tuple[int, int]:
        """Return (highest current strike count across scopes, threshold)
        for this session, AFTER the last freeze-clear reset.

        Surfaced in the UPS context so the agent can SEE it's walking on
        glass (operator directive 2026-06-11): pre-fix the only signal was
        the freeze itself detonating. Returns (0, threshold) when clean.
        """
        if not session_id:
            return (0, 0)
        threshold = _agent_threshold(project_root, session_id)
        try:
            self.hub.execution.init_db(project_root)
            with self.hub.execution.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) c FROM execution_events "
                    "WHERE event_kind = ? "
                    "AND COALESCE(session_id, '') = COALESCE(?, '') "
                    "AND rowid > COALESCE("
                    "  (SELECT MAX(rowid) FROM execution_events "
                    "   WHERE event_kind = ? "
                    "   AND COALESCE(session_id, '') = COALESCE(?, '')), 0) "
                    "GROUP BY target_entity ORDER BY c DESC LIMIT 1",
                    (
                        _STRIKE_EVENT,
                        session_id or None,
                        _STRIKE_RESET_EVENT,
                        session_id or None,
                    ),
                ).fetchone()
                peak = int(row[0]) if row else 0
        except Exception:
            peak = 0
        return (peak, threshold)

    def _emit(self, project_root: Path, **kw: Any) -> None:
        try:
            self.hub.execution.record_event(project_root, **kw)
        except Exception:
            pass

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
        """
        if not session_id or not family:
            return ViolationOutcome.noop()
        actor = actor if actor in VALID_ACTORS else "agent"
        if threshold is None:
            threshold = _agent_threshold(project_root, session_id)
        from .session_freeze_store import KIND_REPEATED_SECURITY_VIOLATION

        freeze_kind = freeze_kind or KIND_REPEATED_SECURITY_VIOLATION
        scope_key = self._scope_key(actor, lane_id, family)

        # ── Severity classification (2026-05-27) ──────────────────────
        # Determine the tier BEFORE emitting the strike event so
        # friction-class blocks don't pollute the strike trail.
        from .violation_severity import (
            SEVERITY_FRICTION,
            SEVERITY_IMMEDIATE_FREEZE,
            SEVERITY_SOFT,
            classify_severity,
            friction_redirect,
        )

        related_roots = self._approved_external_roots(project_root)
        severity = classify_severity(
            family,
            target=target,
            related_roots=related_roots,
        )

        # FRICTION: routing block, NO strike row recorded. Emit a
        # `friction_routing_block` audit event so dashboards can
        # surface noisy redirects, but the strike counter stays put
        # and the deny envelope carries a friendly "use the governed
        # path" message instead of "SECURITY STRIKE."
        if severity == SEVERITY_FRICTION:
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

        # SOFT / STRIKE / IMMEDIATE_FREEZE: record the strike row.
        # Severity is in the payload so soft strikes can be filtered
        # from the trail UI separately from hard strikes.
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
        count = self._count_strikes(project_root, session_id, scope_key)

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

        # IMMEDIATE_FREEZE: skip the threshold check and freeze on
        # the first hit. For attempts that can't be charitable
        # (release-trust tamper, doctrine disable, lane escape, etc.)
        # the "two chances" mechanism is the wrong shape.
        if severity == SEVERITY_IMMEDIATE_FREEZE:
            request_id = self._create_freeze(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                count=count,
                kind=freeze_kind,
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
                    f"operator/admin recovery can clear it."
                    f"{attempted}"
                ),
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
            request_id = self._create_freeze(
                project_root,
                session_id=session_id,
                family=family,
                actor=actor,
                lane_id=lane_id,
                count=count,
                kind=freeze_kind,
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
                    f"recovery can clear it."
                )
            else:
                msg = (
                    f"🛑 SESSION LOCKED — {count} security violations "
                    f"({family}). This session is now frozen; only "
                    f"operator/admin recovery can clear it. The agent "
                    f"cannot lift this lock by prompt."
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

        if threshold >= 2 and count == threshold - 1:
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
            # LEARNING SURFACE (2026-05-27 step 2): the 2nd-strike
            # WARN that goes back to the AGENT must include the
            # action it just attempted — that's the lesson. Without
            # the attempt the agent learns "I got warned" but not
            # "this exact verb-and-target is the boundary."
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

        # First-strike catch (count=1 with threshold>=2 — and any
        # count when threshold==0 / freeze disabled). Surface the
        # attempt now so the agent SEES what got blocked, not just
        # that *something* got blocked. (2026-05-27 step 2.)
        attempted = _format_attempt(tool_name, target)
        # SOFT-severity gets softer copy ("doctrine warning") and a
        # different glyph so the agent can tell apart "suspicious"
        # from "real strike." Both still record an event row; the
        # threshold-based freeze still applies on cross-turn repeat.
        if severity == SEVERITY_SOFT:
            lead = (
                f"⚠️ DOCTRINE WARNING — {family} (soft strike {count}). "
                f"This attempt is blocked; the gate is unsure whether "
                f"it's intentional. Repeating it without changing the "
                f"approach will promote to a hard strike."
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

        return self.record_and_escalate(
            project_root,
            session_id=session_id,
            family="preflight_forbidden",
            actor="operator",
            target=rule_ids,
            tool_name="UserPromptSubmit",
            threshold=_operator_threshold(project_root, session_id),
            freeze_kind=KIND_HOSTILE_OPERATOR_PROMPT,
        )

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
    ) -> str:
        """Create an admin-clear-only freeze + an escalation row (so the
        documented admin recovery commands work). Returns request_id.

        ``kind`` defaults to the repeated-violation lockdown; the operator
        immediate-freeze path passes ``hostile_operator_prompt``.
        """
        from .session_freeze_store import (
            KIND_REPEATED_SECURITY_VIOLATION,
            SessionFreezeStore,
        )

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
        try:
            SessionFreezeStore().set_freeze(
                project_root,
                session_id=session_id,
                request_id=request_id,
                fingerprint_phrase=phrase,
                kind=freeze_kind,
            )
        except Exception:
            pass
        return request_id
