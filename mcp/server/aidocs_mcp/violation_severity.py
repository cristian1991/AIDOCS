"""Violation severity taxonomy — 2026-05-27.

The strike+freeze mechanism in security_violation_service used to be
binary: a family either accrued toward a freeze (one path) or didn't
(`_NON_FREEZING_FAMILIES` = just `raw_shell_t0`). That was too coarse.
Many friction/routing failures (wrong tool used, cwd/context mismatch,
missing active task, managed-mode inactive, session reconnect needed,
lane tool unavailable) were striking the agent toward a freeze when
they're really workflow doctrine corrections — block + redirect, not
"prison."

This module gives every violation family ONE of four severities:

  SEVERITY_FRICTION       — block only, NO strike row recorded.
                            Agent gets "use the governed path" copy.
                            Worst-case spam is logged via the friction
                            audit event, not the strike event.

  SEVERITY_SOFT           — block + soft-strike. Tracked in a separate
                            soft counter; promotes to hard STRIKE only
                            on the (configurable) Nth repeat in the
                            same family. Default promote-on=2 (i.e.
                            the second soft hit is a real strike).

  SEVERITY_STRIKE         — block + hard strike, accrues to freeze on
                            threshold (default 3). The current behavior
                            for genuinely-hostile families.

  SEVERITY_IMMEDIATE_FREEZE — block + freeze on the FIRST hit. Bypasses
                            the "two chances" mechanism. For attempts
                            that can't be charitable: prompt-injected
                            exfil, doctrine disable, audit-log tamper,
                            release-trust tamper, lane escape.

Context-aware classification: `unknown_external` is the canonical
ambiguous family. Same family, very different meanings:

  - Path resolves under an operator-approved related-project root,
    user's home dev tree, or near the current cwd → FRICTION (operator
    forgot to switch project bind; not malicious).

  - Path is /etc/, /proc/, /sys/, /root/, Windows\\System32\\, or
    similar OS infrastructure → STRIKE (real path traversal attempt).

  - Repeated unknown_external in the same turn after AIDOCS already
    explained the governed path → SOFT (suspicious — the agent isn't
    learning).

Callers pass `target` (the path being touched) so the classifier can
make these decisions. When no context is supplied the FAMILY default
is used.
"""

from __future__ import annotations

from collections.abc import Mapping

SEVERITY_FRICTION: str = "friction"
SEVERITY_SOFT: str = "soft"
SEVERITY_STRIKE: str = "strike"
SEVERITY_IMMEDIATE_FREEZE: str = "immediate_freeze"


# ── Family default severities ──────────────────────────────────────
#
# Maintenance: when adding a new family to FAMILY_BY_BLOCKED_BY in
# security_violation_service, add an entry here in the SAME PR. An
# unmapped family defaults to SEVERITY_STRIKE — the conservative
# default (asking is strictly safer than passing). Adjust DOWN to
# friction/soft only when the family is genuinely workflow-grade.

_FAMILY_SEVERITY: Mapping[str, str] = {
    # ─── Friction: routing, doctrine correction, "use the governed path" ──
    # Wrong-tool / wrong-flow blocks. The agent should retry via the
    # correct AIDOCS surface; there's no security risk being averted.
    "raw_shell_t0": SEVERITY_FRICTION,  # raw Bash instead of ai_run
    "tier0_edit_redirect": SEVERITY_FRICTION,  # raw Edit/Write instead of ai_*
    "indexed_file_gate": SEVERITY_FRICTION,  # file not discovered yet → ai_find/ai_bundle first
    "read_gate": SEVERITY_FRICTION,  # same shape, read-path
    "tool_policy": SEVERITY_FRICTION,  # tool not allowed in this surface
    "no_active_task": SEVERITY_FRICTION,  # MUTATING_MCP_TOOLS without an open task
    "managed_mode_inactive": SEVERITY_FRICTION,  # agent calling before session_connect
    "reconnect_required": SEVERITY_FRICTION,  # boot-token rebind needed
    "lane_tool": SEVERITY_FRICTION,  # conductor-only tool in non-conductor session
    "test_retry": SEVERITY_FRICTION,  # too many test re-runs same key
    "foreground_long_running": SEVERITY_FRICTION,  # `npm run dev` without &
    "infrastructure": SEVERITY_FRICTION,  # wrote to .github/.git config without grant
    "agent_brief": SEVERITY_FRICTION,  # sub-agent dispatch missing brief
    # ─── Soft: suspicious but not obviously malicious ────────────────────
    # The default for "unknown_external" lives here because the cwd/
    # context-mismatch case is by FAR the most common cause; path-
    # traversal targeting OS infra is escalated by classify_severity()
    # below.
    "unknown_external": SEVERITY_SOFT,
    "command_read_intent": SEVERITY_SOFT,  # operator destructive-intent matched a read
    # ─── Strike: judge sees real threat ─────────────────────────────────
    "sensitive_read": SEVERITY_STRIKE,  # cat .ssh, .aws/credentials, etc.
    "blocked_sensitive_external": SEVERITY_STRIKE,
    "sensitive_external_read": SEVERITY_STRIKE,
    "judge_malicious_forbidden": SEVERITY_STRIKE,  # heuristic_judge MALICIOUS_FORBIDDEN
    "preflight_forbidden": SEVERITY_STRIKE,  # preflight prompt judge fired
    "self_cancel": SEVERITY_STRIKE,  # agent self-cancelled its OWN freeze
    #   (clear_freeze_service, operator directive 2026-07-15) — a mistake to
    #   learn from, so it ratchets like any real strike (freeze at threshold).
    # ─── Immediate freeze: bypass two-chances ───────────────────────────
    "hostile_operator_prompt": SEVERITY_IMMEDIATE_FREEZE,  # operator explicitly asked
    #   to bypass doctrine
    "release_trust_tamper": SEVERITY_IMMEDIATE_FREEZE,  # touched release_trust /
    #   manifest / pubkey
    "audit_log_tamper": SEVERITY_IMMEDIATE_FREEZE,  # DELETE from execution_events,
    #   memory tombstone bypass
    "lane_escape": SEVERITY_IMMEDIATE_FREEZE,  # worker tried to exit lane
    #   despite env-fence
    "doctrine_disable_attempt": SEVERITY_IMMEDIATE_FREEZE,  # edit to heuristic_judge,
    #   judge_taxonomy, gate cascade,
    #   security.* config
    "approval_forgery": SEVERITY_IMMEDIATE_FREEZE,  # agent emitted confirm tsk-X
    #   in its own response (UPS
    #   should already block, this
    #   is defense-in-depth)
}


# ── Path-traversal escalation patterns ─────────────────────────────
# OS infrastructure paths — any unknown_external touching these is
# never plausibly a cwd mistake. Escalate to STRIKE.
_OS_INFRA_PATH_PATTERNS: tuple[str, ...] = (
    "/etc/",
    "/proc/",
    "/sys/",
    "/root/",
    "/dev/",
    "/boot/",
    "/var/log/",
    "/var/run/",
    "\\windows\\system32\\",
    "\\windows\\syswow64\\",
    "c:\\windows\\",
    # AIDOCS' own trust/security material — touching this from outside
    # the AIDOCS project is doctrine_disable_attempt material
    "/aidocs_mcp/trust/",
    "/aidocs_mcp/security",
    "/aidocs_mcp/heuristic_judge",
    "/aidocs_mcp/judge_taxonomy",
)


# Friendly redirect messages for FRICTION families. Keep them short.
# Callers append the specific target to give the agent concrete guidance.
FRICTION_REDIRECTS: Mapping[str, str] = {
    "raw_shell_t0": ("Use ai_run for shell commands — raw Bash is not the governed path here."),
    "tier0_edit_redirect": (
        "Use ai_replace / ai_create_file for file "
        "edits inside the selected project — raw Edit/Write is not the "
        "governed path."
    ),
    "indexed_file_gate": (
        "The file isn't in the index yet. Run ai_find / ai_bundle to "
        "discover it first; ai_get_lines / ai_replace only operate "
        "on indexed files."
    ),
    "read_gate": (
        "Reach the file via ai_find / ai_get_symbol_snippet / ai_bundle first; raw Read is gated."
    ),
    "tool_policy": (
        "That tool is not advertised on this surface. Check tools/list "
        "for the governed alternatives."
    ),
    "no_active_task": (
        "Open a task with ai_task(mode='begin', ...) before mutating — "
        "every write is attributed via task_id (audit hardening doctrine)."
    ),
    "managed_mode_inactive": (
        "Call session_connect first — no session is currently bound, "
        "so per-call gates have no scope to evaluate against."
    ),
    "reconnect_required": (
        "Session needs to be re-bound (MCP boot-token rotated). Call session_connect to refresh."
    ),
    "lane_tool": (
        "This is a conductor-only tool; the current session isn't a "
        "conductor lane. session_select a conductor session if you "
        "intend to dispatch."
    ),
    "test_retry": (
        "Same test command has been re-run too many times this session — "
        "investigate the failure before re-running."
    ),
    "foreground_long_running": (
        "Long-running command in the foreground would block — append `&` "
        "and capture stdout to a log file."
    ),
    "infrastructure": (
        "That path is infrastructure (.github/, .git/config, deploy "
        "scripts) — grant first via the operator, then retry."
    ),
    "agent_brief": (
        "Sub-agent dispatch missing brief — include the brief argument "
        "describing what the sub-agent should do and why."
    ),
}


def classify_severity(
    family: str,
    *,
    target: str | None = None,
    related_roots: tuple[str, ...] = (),
    same_turn_repeat_count: int = 0,
) -> str:
    """Classify a violation into one of the four severity tiers.

    Args:
      family: the violation family (one of FAMILY_BY_BLOCKED_BY's
              values in security_violation_service).
      target: optional path (or other identifier) being touched —
              enables context-aware escalation for unknown_external.
      related_roots: operator-approved external roots (lowercased,
              path-normalized). When `target` resolves under one of
              these, an unknown_external block is demoted to FRICTION
              (the operator approved the cross-project relation; the
              cwd/session bind just needs switching).
      same_turn_repeat_count: how many times this family has fired in
              the current agent turn before this hit (0 = first). When
              >=1, friction is promoted to SOFT, and SOFT defaults are
              promoted to STRIKE.

    Returns one of SEVERITY_*.

    """
    base = _FAMILY_SEVERITY.get(family, SEVERITY_STRIKE)
    target_lc = (target or "").lower().replace("\\", "/")

    # Path-aware escalation for unknown_external — the main reason
    # this module exists.
    if family == "unknown_external":
        # OS infrastructure paths NEVER demote.
        for pat in _OS_INFRA_PATH_PATTERNS:
            pat_lc = pat.replace("\\", "/").lower()
            if pat_lc in target_lc:
                return SEVERITY_STRIKE

        # Operator-approved related-project root → friction (cwd
        # mismatch, not malicious).
        if target_lc and related_roots:
            for root in related_roots:
                root_lc = root.lower().replace("\\", "/").rstrip("/")
                if not root_lc:
                    continue
                if target_lc.startswith(root_lc + "/") or target_lc == root_lc:
                    base = SEVERITY_FRICTION
                    break

        # BENIGN-EXTERNAL demotion (2026-07-07, "fix the fake flags"): the
        # docstring has always promised temp/scratch + home-dev paths demote to
        # FRICTION, but only related_roots was wired — so a benign scratch write
        # (d:/tmp/x, /tmp/x) or a read under the operator's own home dev tree
        # stayed SOFT and FROZE the session on repeat. These are cwd/bind
        # mismatches, not exfil. Demote to FRICTION (redirect, no strike). OS
        # infra was already hard-STRUCK above and can't reach here; real
        # credential dirs are caught upstream as blocked_sensitive_external.
        if base != SEVERITY_FRICTION and target_lc:
            _benign = False
            # Conventional scratch/temp roots (cross-platform + the live tempdir).
            if ("/tmp/" in target_lc or target_lc.endswith("/tmp")
                    or "/temp/" in target_lc or "/var/tmp/" in target_lc):
                _benign = True
            else:
                try:
                    import tempfile

                    _tmp = tempfile.gettempdir().lower().replace("\\", "/").rstrip("/")
                    if _tmp and (target_lc.startswith(_tmp + "/") or target_lc == _tmp):
                        _benign = True
                except Exception:
                    pass
            # The operator's own home dev tree (sensitive subdirs already blocked
            # upstream; this is a bind/cwd mismatch on the operator's own files).
            if not _benign:
                try:
                    from pathlib import Path as _P

                    _home = str(_P.home()).lower().replace("\\", "/").rstrip("/")
                    if _home and (target_lc.startswith(_home + "/") or target_lc == _home):
                        _benign = True
                except Exception:
                    pass
            if _benign:
                base = SEVERITY_FRICTION
        # Truly-random external (another user's home, /var/lib/…) with no context
        # stays at the SEVERITY_SOFT family default — still charitable (two
        # chances), never an instant strike.

    # Same-turn-repeat promotion: friction-on-repeat becomes soft,
    # soft-on-repeat becomes strike. STRIKE/IMMEDIATE_FREEZE don't
    # need promotion — they're already at the top.
    if same_turn_repeat_count >= 1:
        if base == SEVERITY_FRICTION:
            return SEVERITY_SOFT
        if base == SEVERITY_SOFT:
            return SEVERITY_STRIKE

    return base


def is_freezing_severity(severity: str) -> bool:
    """True iff this severity contributes to / triggers a freeze."""
    return severity in (SEVERITY_STRIKE, SEVERITY_IMMEDIATE_FREEZE)


def friction_redirect(family: str, target: str | None = None) -> str:
    """Render the agent-facing redirect message for a FRICTION block.

    Returns "No. Use the governed path." as a fallback when no
    family-specific guidance exists.
    """
    msg = FRICTION_REDIRECTS.get(family, "No. Use the governed path.")
    if target:
        return f"{msg}\n  ↳ Blocked target: {target[:200]}"
    return msg
