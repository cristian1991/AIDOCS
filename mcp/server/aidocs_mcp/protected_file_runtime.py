"""Per-process runtime state for the DO NOT TOUCH gate.

Spec: protected-file edits require sub-agent detection + per-turn grant
state. Both live in a thin process-local module so:

- Tests can set/reset the state directly (no MCP round-trip needed).
- Middleware plumbing stays out of file_ops.
- State is naturally ephemeral (per MCP process), which matches the
  "one-turn grant" invariant.

The values here are set by:
- Sub-agent flag: the MCP middleware when dispatching via Agent tool /
  lane spawn. Set to True for the duration of the lane's call chain,
  False otherwise.
- Grant lists: claude_hook on UserPromptSubmit after parsing the
  user's current message for verb × gate combinatorics.

The values are read by access_gate / file_ops / the protect/unprotect
tools during edit-tool preflight.
"""

from __future__ import annotations

# Default = conductor posture. The middleware flips this True for the
# scope of a sub-agent / lane call and back to False when the call ends.
_SUB_AGENT_CALL: bool = False
# Once a subagent latches the flag ON via lane_worker_bind, it cannot
# be flipped back to False from inside the subprocess — prevents a
# malicious subagent from clearing the flag and calling conductor-only
# tools. Reset only happens on process exit.
_SUB_AGENT_LATCHED: bool = False

# Per-turn lists of normalized relative paths. Cleared on each
# UserPromptSubmit. Grants are written by claude_hook after parsing
# the user's current message for verb × gate combinatorics; the gate
# reads them to decide whether a write passes.
#
# Three separate grant axes — they don't overlap:
#   _PROTECTED_EDIT_GRANTS — sub-agent edit on an already-protected file
#   _PROTECT_GRANTS        — calling ai_protect on these paths
#   _UNPROTECT_GRANTS      — calling ai_protect on these paths
#
# ...and a FOURTH axis that is NOT a path list — it is a per-turn BOOLEAN:
#   _AGENT_RESEARCH_OVERRIDE — may a sub-agent brief carry research language?
# It gets its OWN store precisely because the old code did NOT give it one: it
# scavenged the flag out of _PROTECTED_EDIT_GRANTS (a list of FILE PATHS) and
# asked whether "delegate research" appeared among them. Always false. The
# override never fired, for anyone, ever — while the gate's refusal message
# actively told the operator to use it. Axes that mean different things do not
# share a store (that is what "three separate grant axes — they don't overlap"
# was already trying to say).
_AGENT_RESEARCH_OVERRIDE: bool = False
#
# Reads accumulate separately in _TURN_READ_FILES as the lane calls
# ai_get_lines / ai_get_symbol_snippet.
_PROTECTED_EDIT_GRANTS: list[str] = []
_PROTECT_GRANTS: list[str] = []
_UNPROTECT_GRANTS: list[str] = []
_TURN_READ_FILES: list[str] = []

# Config-set grants DELETED 2026-04-21 — migrated to sqlite
# (session_query_gate.config_grants column) because the MCP tool
# server runs in a different process than the hook. Module-level
# dicts don't cross process boundaries. The sqlite column is the
# single source of truth. See session_query_gate_store
# .set_config_grants / .get_config_grants.


def set_sub_agent_call(value: bool) -> None:
    """Set the per-process subagent flag.

    Refuses to clear the flag once latched ON via
    `latch_sub_agent_call_on()` — prevents a subagent from escaping
    its restrictions by calling set(False) mid-session. Conductor-side
    middleware can still flip freely as long as it never latched.
    """
    global _SUB_AGENT_CALL
    if _SUB_AGENT_LATCHED and not value:
        return  # latched ON; refuse to clear
    _SUB_AGENT_CALL = bool(value)


def is_sub_agent_call() -> bool:
    return _SUB_AGENT_CALL


def latch_sub_agent_call_on() -> None:
    """One-way latch: sets sub_agent_call=True and blocks future
    set_sub_agent_call(False). Called by lane_worker_bind on a
    spawned subagent process so it cannot escape conductor-only
    gates by re-flipping the flag.
    """
    global _SUB_AGENT_CALL, _SUB_AGENT_LATCHED
    _SUB_AGENT_CALL = True
    _SUB_AGENT_LATCHED = True


def is_sub_agent_latched() -> bool:
    return _SUB_AGENT_LATCHED


def set_protected_edit_grants(paths: list[str]) -> None:
    """Replace the per-turn grant list. Called by claude_hook on
    UserPromptSubmit after parsing the user's current message.

    An empty list from a user message without a grant phrase acts as a
    reset — any prior grant evaporates.
    """
    global _PROTECTED_EDIT_GRANTS
    _PROTECTED_EDIT_GRANTS = [p.replace("\\", "/").strip() for p in paths if p and p.strip()]


def get_protected_edit_grants() -> list[str]:
    return list(_PROTECTED_EDIT_GRANTS)


def set_turn_read_files(paths: list[str]) -> None:
    """Replace the per-turn read-file list. Tests use this directly.
    Production callers should prefer `add_turn_read_file`.
    """
    global _TURN_READ_FILES
    _TURN_READ_FILES = [p.replace("\\", "/").strip() for p in paths if p and p.strip()]


def add_turn_read_file(path: str) -> None:
    canonical = path.replace("\\", "/").strip()
    if canonical and canonical not in _TURN_READ_FILES:
        _TURN_READ_FILES.append(canonical)


def get_turn_read_files() -> list[str]:
    return list(_TURN_READ_FILES)


def set_protect_grants(paths: list[str]) -> None:
    """Replace the per-turn grant list for ai_protect calls."""
    global _PROTECT_GRANTS
    _PROTECT_GRANTS = [p.replace("\\", "/").strip() for p in paths if p and p.strip()]


def get_protect_grants() -> list[str]:
    return list(_PROTECT_GRANTS)


def set_unprotect_grants(paths: list[str]) -> None:
    """Replace the per-turn grant list for ai_protect calls."""
    global _UNPROTECT_GRANTS
    _UNPROTECT_GRANTS = [p.replace("\\", "/").strip() for p in paths if p and p.strip()]


def get_unprotect_grants() -> list[str]:
    return list(_UNPROTECT_GRANTS)


def set_agent_research_override(granted: bool) -> None:
    """Per-turn flag: may a sub-agent brief carry research/inspection language?

    ITS OWN STORE, DELIBERATELY. The old code scavenged this flag out of
    ``get_protected_edit_grants()`` — a list of FILE PATHS produced by the
    protected-file-EDIT detector — and asked "does 'delegate research' appear
    among these paths?". The answer is always no, so the override NEVER FIRED,
    while the gate's own refusal message told the operator to use it. A false
    affordance is worse than none: it teaches the operator that the gate's
    instructions are lies, and an operator who stops believing refusals starts
    routing around them.
    """
    global _AGENT_RESEARCH_OVERRIDE
    _AGENT_RESEARCH_OVERRIDE = bool(granted)


def get_agent_research_override() -> bool:
    return _AGENT_RESEARCH_OVERRIDE


def set_agent_research_override_from_prompt(prompt: str) -> bool:
    """Derive the override from the OPERATOR'S CURRENT PROMPT — which is exactly
    what the refusal message promises ("include 'delegate research' in their
    prompt"). Returns the flag it set.

    Reads the ONE canonical phrase table (hook_pipeline) — imported lazily so a
    low-level runtime module never drags the core in at import time. An empty or
    phrase-less prompt RESETS the flag: a grant that outlives its turn is a
    standing privilege nobody granted (doctrine XV — operator intent is
    present-tense and per-turn).
    """
    granted = False
    try:
        from .canonical_intent_registry import (
            detect_research_delegation_v2,
            strip_non_operator_text,
        )
        from .hook_pipeline import AGENT_RESEARCH_OVERRIDE_PHRASES

        # Provenance floor (WAR P Task 2): only operator-authored text can
        # mint the override — harness-injected notification/reminder blocks
        # are stripped before ANY phrase or shape is evaluated.
        low = strip_non_operator_text(prompt or "").lower()
        # #396 (WAR U): a UPS with NO operator-authored text (a pure harness
        # injection — task-notification, system reminder) is not a new
        # operator turn. It MINTS nothing (return False — the provenance
        # floor) but must not REVOKE the grant the operator's own message
        # minted this turn (the 'delegate research' override died because an
        # injected mid-turn message reset it before the Task dispatch). Leave
        # the stored flag untouched; the next REAL operator prompt still
        # resets per-turn scope (doctrine XV).
        if (prompt or "").strip() and not low.strip():
            return False
        granted = any(phrase in low for phrase in AGENT_RESEARCH_OVERRIDE_PHRASES)
        if not granted:
            # #459 delegation-intent SHAPE (lemma+proximity, §X negatives
            # DROP) — same flag, same per-turn scope as the literal table.
            granted = detect_research_delegation_v2(low)
    except Exception:
        granted = False  # cannot detect => no grant. Fail CLOSED on the allow-path.
    set_agent_research_override(granted)
    return granted


def clear_all_protected_state() -> None:
    """Reset everything — called on UserPromptSubmit before re-parsing."""
    set_protected_edit_grants([])
    set_protect_grants([])
    set_unprotect_grants([])
    set_turn_read_files([])
    set_agent_research_override(False)
