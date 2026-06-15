"""Prompt-intent classifier — should a user prompt start a task?

Classifies a raw user prompt into one of three buckets so the
UserPromptSubmit pipeline can auto-start a task (task_begin) and remove
the friction of the agent having to open one by hand on every turn:

  * "imperative"     — a command to DO something ("commit and push",
                       "fix the auth bug"). -> start a work task.
  * "investigation"  — a question about the PROJECT / USER / world state
                       that the agent must check ("did I set the
                       address?", "is X configured?"). -> start an
                       investigation task.
  * "answerable"     — a question about the AGENT's own recent actions
                       ("did you commit?"), or chit-chat / acknowledgement
                       ("thanks", "ok"). -> no task; the agent just answers.

Design notes:
  * Pure, dependency-free, deterministic — safe to run inside the hook's
    <500ms budget on every prompt (no spaCy load).
  * The load-bearing distinction is "did I…" (investigation) vs
    "did you…" (answerable): a question about the agent's own turn is
    answered directly; a question about the project/world is investigated.
  * Conservative: when a prompt is ambiguous and not clearly a command or
    a project-state question, default to "answerable" (do NOT spawn a task
    on filler). False negatives (missing a task) are cheaper than spamming
    the task DB on conversational turns.
"""

from __future__ import annotations

import re
from typing import Literal

Intent = Literal["imperative", "investigation", "answerable"]

# Base-form action verbs that, at the start of a clause, signal a command.
# Kept explicit (not POS tagging) so the classifier stays hook-cheap and
# deterministic. Extend deliberately.
_ACTION_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "build",
        "bump",
        "change",
        "check",
        "clean",
        "commit",
        "configure",
        "create",
        "debug",
        "delete",
        "deploy",
        "disable",
        "document",
        "draft",
        "enable",
        "extract",
        "find",
        "fix",
        "format",
        "generate",
        "implement",
        "init",
        "inline",
        "insert",
        "install",
        "investigate",
        "lint",
        "make",
        "merge",
        "migrate",
        "move",
        "optimize",
        "patch",
        "publish",
        "push",
        "rebase",
        "refactor",
        "remove",
        "rename",
        "replace",
        "research",
        "resolve",
        "revert",
        "rewrite",
        "run",
        "scaffold",
        "set",
        "setup",
        "ship",
        "split",
        "test",
        "update",
        "upgrade",
        "verify",
        "wire",
        "write",
    },
)

# Phrases that mark a question about the AGENT's own recent actions — these
# are answered directly, never spawn a task. Second-person framing with a
# STATE/PAST auxiliary ("did you", "have you", "are you", even wh-prefixed
# like "what did you change"). Request modals (can/could/would/will you …)
# are deliberately EXCLUDED — those are polite imperatives, handled below.
_AGENT_ACTION_RE = re.compile(
    r"\b(did|do|does|have|has|are|were|was)\s+(you|u)\b"
    r"|\b(you|u)\s+(committed|pushed|did|ran|changed|added|removed|fixed)\b",
    re.IGNORECASE,
)

# Polite-imperative wrappers: "please <verb>", "can you <verb>",
# "could you <verb>", "let's <verb>", "go ahead and <verb>". These are
# commands despite the question-ish surface.
_POLITE_IMPERATIVE_RE = re.compile(
    r"^\s*(please\s+|let'?s\s+|go\s+ahead\s+and\s+|"
    r"(can|could|would|will)\s+(you|u)\s+(please\s+)?)"
    r"([a-z]+)",
    re.IGNORECASE,
)

# First-person / project-state question openers -> investigation.
_INVESTIGATION_OPENERS_RE = re.compile(
    r"^\s*(did|do|have|has|is|are|was|were|does|should|can|could|will)\s+"
    r"(i|we|the|this|that|my|our|it|there|he|she|they)\b"
    r"|^\s*(what|where|why|how|which|who|whose|when)\b",
    re.IGNORECASE,
)

# Pure acknowledgements / chit-chat (whole prompt) -> answerable.
_CHITCHAT_RE = re.compile(
    r"^\s*(thanks?|thank\s+you|ty|ok(ay)?|k|cool|nice|great|awesome|"
    r"perfect|good|gotcha|got\s+it|yes|yep|yeah|no|nope|sure|hi|hello|"
    r"hey|lol|haha|nvm|never\s*mind)[\s!.?]*$",
    re.IGNORECASE,
)


def classify_prompt_intent(prompt: str) -> Intent:
    """Classify a raw user prompt. See module docstring for the buckets."""
    text = (prompt or "").strip()
    if not text:
        return "answerable"

    is_question = text.endswith("?")

    # 1. Chit-chat / acknowledgement -> answerable.
    if _CHITCHAT_RE.match(text):
        return "answerable"

    # 2. Question about the AGENT's own recent actions -> answerable.
    #    (Checked before investigation so "did you …?" / "what did you …?"
    #    never spawn a task. search(), not match(), to catch wh-prefixes.)
    if _AGENT_ACTION_RE.search(text):
        return "answerable"

    # 3. Polite-imperative wrappers ("please fix…", "can you commit…")
    #    -> imperative IF they wrap an action verb.
    m = _POLITE_IMPERATIVE_RE.match(text)
    if m:
        verb = m.group(m.lastindex or 0)
        if verb and verb.lower() in _ACTION_VERBS:
            return "imperative"

    # 4. Bare imperative: first token is a base-form action verb AND the
    #    prompt is not a question ("commit and push", "fix the bug").
    first = re.match(r"\s*([a-zA-Z]+)", text)
    if first and not is_question:
        if first.group(1).lower() in _ACTION_VERBS:
            return "imperative"

    # 5. Investigation: a question about project/user/world state.
    if is_question and _INVESTIGATION_OPENERS_RE.match(text):
        return "investigation"

    # 6. A question that slipped past the openers but isn't agent-directed
    #    is still more likely an investigation than chit-chat.
    if is_question:
        return "investigation"

    # 7. Default: not a command, not a question -> answerable (don't spawn
    #    a task on statements/filler).
    return "answerable"


def should_start_task(prompt: str) -> bool:
    """True when the prompt's intent warrants auto-starting a task."""
    return classify_prompt_intent(prompt) in ("imperative", "investigation")
