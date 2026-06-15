"""Canonical intent registry — §XVI compliance substrate.

Per emperor-doctrine §XVI:
  *"NLP intent detectors MUST match semantic shape via lemma sets +
   token patterns + proximity rules — NEVER literal strings. The
   operator phrases the same intent in any language they speak; the
   canonical-intent layer normalizes."*

Today (pre-Phoenix migration) the protect/unprotect/override detectors
in `claude_hook.py` use LITERAL tuples of strings (e.g. _PROTECT_VERBS).
That's the doctrine-vs-code gap this module closes.

Shape: each intent is an `IntentSpec` carrying lemma sets + multi-
language gate tokens + proximity rules + observational-prefix
suppressors. `match_intent(prompt, intent_name)` returns a
`MatchResult` carrying:
  - matched: bool
  - confidence: float (0.0..1.0; reserved for future tone-strengthener)
  - granted_paths: set[str]
  - evidence: dict (positions of matches for audit)

Existing literal-tuple detectors continue to run side-by-side until
parity is verified; the migration is additive, not destructive.

Phoenix, 2026-05-08.
"""

from __future__ import annotations

import re

# dataclasses + typing.Iterable imports dropped 2026-05-13 with the
# DNT detector rip (IntentSpec/MatchResult/_extract_paths_near gone).


# ── DNT-specific detector machinery removed 2026-05-13 ──
# IntentSpec, MatchResult, INTENT_REGISTRY (4 specs), match_intent,
# _find_verb_anchors, _find_gate_anchors, _pair_within,
# _extract_paths_near, _PATH_TOKEN_RE, _GATE_TOKENS_DNT,
# _OBSERVATIONAL_EN, lemma_of, _stem, _LEMMA_SUFFIXES,
# detect_protect_grants_v2, detect_unprotect_grants_v2 — all
# deleted. Production DNT signal flows through
# aidocs_nlp.consumers.dnt_detector. The legacy primitives below
# (LEGACY_*, legacy_match_verb_gate_pairs, legacy_extract_paths_near)
# stay alive because detect_protected_edit_overrides_v2 (the
# protected-edit-override grant axis) still uses them — overrides
# include multi-word phrases like "go ahead and" that don't fit
# the spaCy lemma model and the volume there is low enough that
# moving them to the NLP layer is deferred until the dashboard
# Languages page ships.

# ── Shared legacy primitives (moved from claude_hook 2026-05-08) ──
#
# These tuples + helpers were Claude-Code-specific by accident — they
# lived in claude_hook.py because that's where the first grant
# detector landed. Codex / OpenCode CLI couldn't reach them. Per
# emperor-doctrine §XVI (intent detection is multi-language by
# construction), they belong in the canonical-intent registry as
# shared host-agnostic primitives. The literal-tuple legacy path
# stays available alongside the lemma-aware canonical path; once
# parity holds for one release, the legacy retires.

LEGACY_PROTECT_VERBS: tuple[str, ...] = (
    "add",
    "protect",
    "lock",
    "mark",
    "tag",
    "flag",
)
LEGACY_UNPROTECT_VERBS: tuple[str, ...] = (
    "remove",
    "unprotect",
    "unlock",
    "strip",
    "clear",
)
LEGACY_OVERRIDE_VERBS: tuple[str, ...] = (
    "override",
    "bypass",
    "pass",
    "skip",
    "allow",
    "permit",
    "let",
    "go ahead and",
    "you can",
    "you may",
    "ok to",
    "it's ok to",
    "it is ok to",
    "feel free to",
)
LEGACY_GATE_TOKENS: tuple[str, ...] = (
    "do not touch",
    "do-not-touch",
    "do_not_touch",
    "don't touch",
    "dont touch",
    "the touch gate",
    "the protection",
    "the no-touch",
    "the no touch",
)
LEGACY_OBSERVATIONAL_PREFIXES: tuple[str, ...] = (
    "usually ",
    "typically ",
    "normally ",
    "sometimes ",
    "often ",
    "would ",
    "used to ",
    "tend to ",
)
LEGACY_PROTECT_GRANT_PROXIMITY = 80
LEGACY_PROTECT_PATH_PROXIMITY = 200
LEGACY_PATH_TOKEN_RE_STR = r"\b([A-Za-z_\.][\w\-]*(?:[/\\][\w\-]+)*\.[A-Za-z0-9]+)\b"


def legacy_match_verb_gate_pairs(
    text: str,
    verbs: tuple[str, ...],
    gate_tokens: tuple[str, ...],
    observational_prefixes: tuple[str, ...] = LEGACY_OBSERVATIONAL_PREFIXES,
    proximity: int = LEGACY_PROTECT_GRANT_PROXIMITY,
) -> list[tuple[int, int]]:
    """Legacy literal-tuple verb x gate matcher. Same algorithm
    claude_hook used pre-2026-05-08, lifted intact to this shared
    home so all hosts can reach it. Prefer match_intent() for new
    callers — it's lemma-aware and multi-language by construction.
    """
    pairs: list[tuple[int, int]] = []
    text_lower = text.lower()

    verb_positions: list[tuple[int, int]] = []
    for verb in verbs:
        start = 0
        while True:
            idx = text_lower.find(verb, start)
            if idx == -1:
                break
            preceding = text_lower[max(0, idx - 20) : idx]
            if any(preceding.endswith(p) for p in observational_prefixes):
                start = idx + 1
                continue
            verb_positions.append((idx, idx + len(verb)))
            start = idx + 1

    gate_positions: list[tuple[int, int]] = []
    for token in gate_tokens:
        start = 0
        while True:
            idx = text_lower.find(token, start)
            if idx == -1:
                break
            gate_positions.append((idx, idx + len(token)))
            start = idx + 1

    for v_start, v_end in verb_positions:
        for g_start, g_end in gate_positions:
            if abs(g_start - v_end) <= proximity or abs(v_start - g_end) <= proximity:
                pairs.append((v_end, g_end))
    return pairs


def legacy_extract_paths_near(
    text: str,
    anchors: list[tuple[int, int]],
    proximity: int = LEGACY_PROTECT_PATH_PROXIMITY,
) -> set[str]:
    """Legacy path extractor. Find path-like tokens within proximity
    chars of any anchor; comma-continuation chains extend the capture
    so user-written lists like "protect a.js, b.css, c.cshtml" grant
    every entry even when the tail of the list exceeds proximity.
    """
    pattern = re.compile(LEGACY_PATH_TOKEN_RE_STR)
    all_matches = list(pattern.finditer(text))
    accepted: set[int] = set()
    paths: set[str] = set()
    for idx, match in enumerate(all_matches):
        path_start = match.start()
        path_end = match.end()
        if any(
            min(abs(path_start - a_end), abs(path_end - a_start)) <= proximity
            for a_start, a_end in anchors
        ):
            accepted.add(idx)
            paths.add(match.group(1).replace("\\", "/"))
    if accepted:
        comma_gap = re.compile(r"^[\s,]*,[\s,]*$")
        changed = True
        while changed:
            changed = False
            for i in list(accepted):
                if i + 1 < len(all_matches) and (i + 1) not in accepted:
                    gap = text[all_matches[i].end() : all_matches[i + 1].start()]
                    if comma_gap.match(gap):
                        accepted.add(i + 1)
                        paths.add(
                            all_matches[i + 1].group(1).replace("\\", "/"),
                        )
                        changed = True
                if i - 1 >= 0 and (i - 1) not in accepted:
                    gap = text[all_matches[i - 1].end() : all_matches[i].start()]
                    if comma_gap.match(gap):
                        accepted.add(i - 1)
                        paths.add(
                            all_matches[i - 1].group(1).replace("\\", "/"),
                        )
                        changed = True
    return paths


def detect_protected_edit_overrides_v2(
    prompt: str,
    *,
    max_paths: int = 20,
) -> set[str]:
    """Override grants — multi-word + single-word verbs + gate token.
    Uses the legacy primitives (verb list has multi-word phrases like
    'go ahead and' that don't fit the lemma model). Returns granted
    paths or {'*'} for blanket overrides (no path named).

    ``max_paths`` caps how many specific paths a single prompt can
    grant — 20 by default, the historic ``_PROTECT_CAP`` value. Over
    cap: if the blanket marker ``"*"`` is among the extracted paths
    it wins (treated as a sentinel for "grant everything anyway");
    otherwise the result is truncated to the first ``max_paths``
    entries. Cap pushed inline 2026-05-27 — previously lived in the
    claude_hook wrapper that this function now replaces directly.
    """
    if not prompt or not prompt.strip():
        return set()
    pairs = legacy_match_verb_gate_pairs(
        prompt,
        LEGACY_OVERRIDE_VERBS,
        LEGACY_GATE_TOKENS,
    )
    if not pairs:
        return set()
    anchors: list[tuple[int, int]] = []
    for v_end, g_end in pairs:
        anchors.append((v_end - 1, v_end))
        anchors.append((g_end - 1, g_end))
    paths = legacy_extract_paths_near(prompt, anchors)
    if not paths:
        return {"*"}  # blanket override (no path named)
    if len(paths) > max_paths:
        if "*" in paths:
            return {"*"}
        paths = set(list(paths)[:max_paths])
    return paths


# ── Lane-exit grant detection ──────────────────────────────────────
# Phrase-only detection (no verb+token pairing). Multi-language
# surface forms; primary-intent guard blocks buried-text leakage.

LANE_EXIT_PHRASES: tuple[str, ...] = (
    "exit lane",
    "exit the lane",
    "leave lane",
    "leave the lane",
    "clear lane",
    "clear the lane",
    "clear lane scope",
    "drop lane",
    "drop the lane",
    "unstick lane",
    "unstick the lane",
    "salir del lane",
    "salir de la lane",
    "limpiar lane",
    "quitter la voie",
    "quitter le lane",
    "uscire dal lane",
    "ieși din lane",
    "iesi din lane",
)


def detect_lane_exit_v2(
    prompt: str,
    *,
    is_worker_caller: bool = False,
    primary_intent_head_chars: int = 120,
    primary_intent_max_total: int = 200,
) -> bool:
    """Phoenix 2026-05-08: moved from claude_hook (host-agnostic).
    Three guards: worker fence, autowake fence, primary-intent rule
    (phrase must appear in first head_chars or whole prompt
    <=max_total — buried text in long prompts can't leak).
    """
    if not prompt or not prompt.strip():
        return False
    if is_worker_caller:
        return False
    text = prompt.strip()
    if "<<autonomous-loop" in text.lower():
        return False
    if len(text) <= primary_intent_max_total:
        haystack = text.lower()
    else:
        haystack = text[:primary_intent_head_chars].lower()
    for phrase in LANE_EXIT_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", haystack):
            return True
    return False


# ── Sticky-grant markers ───────────────────────────────────────────
# Substring-match against lowercased prompt. Multi-language surface
# forms; presence upgrades per-turn user-intent grants to sticky.

STICKY_GRANT_MARKERS: tuple[str, ...] = (
    # English
    "sticky",
    "always allow",
    "always enable",
    "keep allowing",
    "keep allowed",
    "across turns",
    "persist the grant",
    "persistent grant",
    "persistent allow",
    "for the whole session",
    "for this whole session",
    # Spanish
    "permitir siempre",
    "siempre permitir",
    "para toda la sesión",
    "para toda la sesion",
    # French
    "toujours autoriser",
    "autoriser toujours",
    "pour toute la session",
    # Italian
    "sempre permetti",
    "sempre consenti",
    # Romanian
    "permite mereu",
    "mereu permite",
)


def detect_sticky_grant_flag_v2(prompt: str) -> bool:
    """True when prompt contains any sticky-grant marker.
    Phoenix 2026-05-08: moved from claude_hook (host-agnostic).
    """
    text = (prompt or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in STICKY_GRANT_MARKERS)


STICKY_REVOKE_MARKERS: tuple[str, ...] = (
    "revoke sticky",
    "clear sticky",
    "drop sticky",
    "remove sticky",
    "stop sticky",
    "revoke all grants",
    "clear all grants",
    "revocar sticky",
    "limpiar sticky",
    "revoquer sticky",
    "revoca sticky",
    "revocă sticky",
    "revoca sticky",
)


def detect_sticky_revoke_grant_v2(prompt: str) -> bool:
    """True when prompt contains any sticky-revoke marker."""
    text = (prompt or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in STICKY_REVOKE_MARKERS)


# ── Scoped revoke detection (backlog #15 phase 4) ──────────────────
# Scope-aware revoke: "revoke bash grant" → {"bash"}. Empty set means
# either no revoke intent OR a wholesale revoke (the WHOLESALE path
# is handled separately by detect_sticky_revoke_grant_v2 — wholesale
# clears all sticky grants; scoped clears just the named tool(s)).
SCOPED_REVOKE_VERBS: tuple[str, ...] = (
    "revoke ",
    "stop allowing ",
    "stop granting ",
    "drop sticky ",
    "unstick ",
    "remove sticky ",
    "cancel grant for ",
    "cancel sticky for ",
)


def detect_scoped_revoke_tools_v2(prompt: str) -> set[str]:
    """Parse a prompt for scoped sticky revocations.

    Looks for verb + tool-token pairs inside a 40-char proximity
    window. Returns lowercased tool names the operator asked to
    revoke. Only matches tools from USER_INTENT_TOOL_TOKEN_PATTERNS
    so arbitrary words don't count as tool names.

    Phoenix 2026-05-27: moved from claude_hook (host-agnostic). The
    legacy function used a class field on ClaudeHookHandler; the
    canonical version uses USER_INTENT_TOOL_TOKEN_PATTERNS which
    carries the same content.
    """
    text = (prompt or "").strip().lower()
    if not text:
        return set()
    tools: set[str] = set()
    for verb in SCOPED_REVOKE_VERBS:
        start = 0
        while True:
            idx = text.find(verb, start)
            if idx == -1:
                break
            verb_end = idx + len(verb)
            window = text[verb_end : verb_end + 40]
            for pattern, tool in USER_INTENT_TOOL_TOKEN_PATTERNS.items():
                if re.search(pattern, window):
                    tools.add(tool)
            start = idx + 1
    return tools


# ── Config grant detection ─────────────────────────────────────────
# Per-turn config grant map extracted from the prompt. config_set
# refuses writes whose key isn't granted. Phoenix 2026-05-08: moved
# from claude_hook (host-agnostic). Multi-language verbs added.

CONFIG_KEY_PATTERN = r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+"

CONFIG_GRANT_PATTERNS: tuple[tuple[str, str], ...] = (
    # English
    (rf"\bturn\s+on\s+(?:the\s+)?({CONFIG_KEY_PATTERN})\b", "on"),
    (rf"\benable\s+(?:the\s+)?({CONFIG_KEY_PATTERN})\b", "on"),
    (rf"\bturn\s+off\s+(?:the\s+)?({CONFIG_KEY_PATTERN})\b", "off"),
    (rf"\bdisable\s+(?:the\s+)?({CONFIG_KEY_PATTERN})\b", "off"),
    (rf"\b(?:flip|toggle)\s+(?:the\s+)?({CONFIG_KEY_PATTERN})\b", "toggle"),
    (rf"\bset\s+({CONFIG_KEY_PATTERN})\s+to\s+(\S+)", "capture"),
    (rf"\bset\s+({CONFIG_KEY_PATTERN})\s*=\s*(\S+)", "capture"),
    # Spanish
    (rf"\bactivar\s+({CONFIG_KEY_PATTERN})\b", "on"),
    (rf"\bdesactivar\s+({CONFIG_KEY_PATTERN})\b", "off"),
    (rf"\bestablecer\s+({CONFIG_KEY_PATTERN})\s+a\s+(\S+)", "capture"),
    # French
    (rf"\bactiver\s+({CONFIG_KEY_PATTERN})\b", "on"),
    (rf"\bdésactiver\s+({CONFIG_KEY_PATTERN})\b", "off"),
    (rf"\bdesactiver\s+({CONFIG_KEY_PATTERN})\b", "off"),
    # Romanian
    (rf"\bactivează\s+({CONFIG_KEY_PATTERN})\b", "on"),
    (rf"\bactiveaza\s+({CONFIG_KEY_PATTERN})\b", "on"),
    (rf"\bdezactivează\s+({CONFIG_KEY_PATTERN})\b", "off"),
    (rf"\bdezactiveaza\s+({CONFIG_KEY_PATTERN})\b", "off"),
)


def parse_config_value(raw: str) -> object:
    """Coerce a captured token into a JSON-compatible scalar.
    Phoenix 2026-05-08: moved from claude_hook (pure transform).
    """
    import json as _json

    s = raw.strip().strip(",.;:").strip("\"'`")
    low = s.lower()
    if low in ("true", "yes", "on", "sí", "si", "oui", "ja", "da"):
        return True
    if low in ("false", "no", "off", "non", "nein", "nu"):
        return False
    if low in ("null", "none", "nada", "rien", "nichts"):
        return None
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except ValueError:
            pass
    if s.startswith(("[", "{")):
        try:
            return _json.loads(s)
        except Exception:
            pass
    return s


# ── User-intent tool grants ────────────────────────────────────────
# Detection pieces for "the operator authorized raw tool X this turn".
# Phoenix 2026-05-08: moved from claude_hook (host-agnostic part).
# Used by the hook's _grant_user_intent_tools which couples this
# detection with the sticky-grant judge + SQL writes (host-related).

USER_INTENT_DIRECT_PHRASES: dict[str, tuple[str, ...]] = {
    "grep": ("grep for", "search the files for", "find in files", "rg "),
    "read": (
        "read the file",
        "read file",
        "cat the",
        "show me the file",
        "open the file",
    ),
    "glob": ("find files named", "list the files", "ls "),
    "edit": ("sed ", "edit the file"),
    "write": (
        "write to the file",
        "save to file",
        "save to the file",
    ),
    "bash": (
        "run `",
        "execute `",
        "run the script",
        "run the command",
    ),
}

USER_INTENT_OBSERVATIONAL_PREFIXES: tuple[str, ...] = (
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

USER_INTENT_GRANT_VERB_PHRASES: tuple[str, ...] = (
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

USER_INTENT_TOOL_TOKEN_PATTERNS: dict[str, str] = {
    r"\bgrep\b": "grep",
    r"\bglob\b": "glob",
    r"\bbash\b": "bash",
    r"\bshell\b": "bash",
    r"\bread\b": "read",
    r"\bedit\b": "edit",
    r"\bwrite\b": "write",
}

USER_INTENT_GRANT_PROXIMITY = 60

USER_INTENT_ENUM_TOKENS: frozenset[str] = frozenset(
    {
        "grep",
        "rg",
        "find",
        "glob",
        "ls",
        "cat",
        "sed",
        "bash",
        "read",
        "write",
        "edit",
        "update",
        "powershell",
        "pwsh",
        "cmd",
    },
)

USER_INTENT_IMPERATIVE_TOOLS: tuple[str, ...] = (
    "grep",
    "rg",
    "find",
    "glob",
    "ls",
    "cat",
    "sed",
)

USER_INTENT_IMPERATIVE_MAP: dict[str, str] = {
    "grep": "grep",
    "rg": "grep",
    "find": "glob",
    "glob": "glob",
    "ls": "glob",
    "cat": "read",
    "sed": "edit",
}


# ── Bash subcommand grants ─────────────────────────────────────────
# Per-turn grants for individual bash binaries (psql, pg_dump, etc.)
# beyond the coarse raw-tool grants. Phoenix 2026-05-08: moved from
# claude_hook (host-agnostic). Multi-language verbs added.

BASH_SUBCMD_GRANT_VERBS: tuple[str, ...] = (
    "allow ",
    "you can use ",
    "go ahead and run ",
    "go ahead and use ",
    "let me use ",
    "let me run ",
    "permit ",
    "enable ",
    "ok use ",
    "ok run ",
    # Spanish
    "permitir ",
    "puedes usar ",
    # French
    "autoriser ",
    "tu peux utiliser ",
    # Italian
    "permetti ",
    "puoi usare ",
    # Romanian
    "permite ",
    "poți folosi ",
    "poti folosi ",
)

BASH_SUBCMD_GRANT_DENYLIST: frozenset[str] = frozenset(
    {
        "rm",
        "sudo",
        "doas",
        "runas",
        "dd",
        "mkfs",
        "format",
        "kill",
        "killall",
        "pkill",
    },
)

BASH_SUBCMD_GRANT_CAP: int = 10

BASH_SUBCMD_TRAILING_PATTERN = r"\b([a-z][a-z0-9_\-]*)\s+(?:is\s+)?allowed\b"
BASH_SUBCMD_TOKEN_PATTERN = r"\b([a-z][a-z0-9_\-]{1,})\b"

_BASH_SUBCMD_FILLER_TOKENS: frozenset[str] = frozenset(
    {
        "for",
        "to",
        "this",
        "the",
        "turn",
        "time",
        "session",
        "please",
        "also",
        "and",
        "or",
        "now",
        "as",
        "needed",
        "tool",
        "tools",
        "command",
        "commands",
    },
)


def detect_bash_subcommand_grants_v2(prompt: str) -> set[str]:
    """Per-turn bash subcommand grants from prompt.
    Phoenix 2026-05-08: moved from claude_hook (host-agnostic).
    Two forms: '<verb> <token>...' and '<token> allowed'.
    """
    if not prompt:
        return set()
    text = prompt.strip().lower()
    if not text:
        return set()
    granted: set[str] = set()
    # Form 1: verb-leading.
    for verb in BASH_SUBCMD_GRANT_VERBS:
        start = 0
        while True:
            idx = text.find(verb, start)
            if idx == -1:
                break
            tail = text[idx + len(verb) : idx + len(verb) + 80]
            for m in re.finditer(BASH_SUBCMD_TOKEN_PATTERN, tail):
                token = m.group(1)
                if token in _BASH_SUBCMD_FILLER_TOKENS:
                    continue
                if token in BASH_SUBCMD_GRANT_DENYLIST:
                    continue
                granted.add(token)
                if len(granted) >= BASH_SUBCMD_GRANT_CAP:
                    return granted
            start = idx + 1
    # Form 2: trailing "allowed".
    for m in re.finditer(BASH_SUBCMD_TRAILING_PATTERN, text):
        token = m.group(1)
        if token in BASH_SUBCMD_GRANT_DENYLIST:
            continue
        granted.add(token)
        if len(granted) >= BASH_SUBCMD_GRANT_CAP:
            break
    return granted


# ── Confirmation response detection (yes/no/cancel for ask-state) ──

CONFIRM_YES_PHRASES: tuple[str, ...] = (
    # English
    "yes",
    "y",
    "yep",
    "yeah",
    "yup",
    "go ahead",
    "proceed",
    "do it",
    "confirm",
    "approve",
    "approved",
    "authorize",
    "authorized",
    "allow it",
    "allow that",
    "ok do it",
    "sure",
    "sure thing",
    # Spanish
    "sí",
    "si",
    "claro",
    "adelante",
    "hazlo",
    # French
    "oui",
    "vas-y",
    "fais-le",
    # Italian
    "sì",
    "si",
    "vai",
    "fallo",
    # German
    "ja",
    "mach",
    "weiter",
    # Romanian
    "da",
    "fă-o",
    "fa-o",
    "continuă",
)

CONFIRM_NO_PHRASES: tuple[str, ...] = (
    # English
    "no",
    "n",
    "nope",
    "cancel",
    "abort",
    "stop",
    "nevermind",
    "never mind",
    "don't",
    "do not",
    "dont do it",
    "do not do it",
    "deny",
    "reject",
    "denied",
    "rejected",
    "hold on",
    "wait",
    # Spanish
    "no lo hagas",
    "cancelar",
    "espera",
    # French
    "non",
    "annule",
    "attends",
    # Italian
    "no",
    "annulla",
    "aspetta",
    # German
    "nein",
    "abbrechen",
    "warte",
    # Romanian
    "nu",
    "anulează",
    "anuleaza",
    "așteaptă",
    "asteapta",
)


def _word_boundary_contains(text: str, phrase: str) -> bool:
    """Word-boundary substring check. Multi-word phrases fall back
    to substring (operator typing 'go ahead' probably means it).
    """
    if " " in phrase:
        return phrase in text
    return bool(re.search(rf"\b{re.escape(phrase)}\b", text))


def detect_confirmation_response_v2(prompt: str) -> str | None:
    """Parse prompt for confirm yes/no. Returns 'yes', 'no', None.
    No-phrases win over yes-phrases when both present.
    """
    if not prompt:
        return None
    text = prompt.strip().lower()
    if not text:
        return None
    for no in CONFIRM_NO_PHRASES:
        if _word_boundary_contains(text, no):
            return "no"
    for yes in CONFIRM_YES_PHRASES:
        if _word_boundary_contains(text, yes):
            return "yes"
    return None


def detect_user_intent_tools_v2(prompt: str) -> set[str]:
    """Pure NLP detection — returns set of tool keys the operator
    authorized this turn. Phoenix 2026-05-08: extracted from
    claude_hook._grant_user_intent_tools so the SQL-write side stays
    in the hook (host-related) and pure detection lives here.

    Three independent matchers union their results:
      1. Direct intent phrases ("grep for", "cat the", etc.) — single
         phrase suffices, observational prefix suppresses.
      2. Bare-imperative first-word ("grep CreateEdit", "rg foo") —
         tool token as first word + something after.
      3. Verb-near-token co-occurrence ("allow grep", "you can use
         bash") — verb within 60 chars of tool token.

    Token-dump enumerations ("grep glob bash read") are filtered out:
    those are tool lists, not invocations.
    """
    if not prompt:
        return set()
    text = prompt.strip().lower()
    if not text:
        return set()

    # Token-dump guard.
    leading_words = re.findall(r"[a-zA-Z][\w\-]*", text)
    leading_tool_run = 0
    for word in leading_words:
        if word.lower() in USER_INTENT_ENUM_TOKENS:
            leading_tool_run += 1
        else:
            break
    is_token_dump = leading_tool_run >= 3 and leading_tool_run == len(leading_words)

    granted: set[str] = set()

    if not is_token_dump:
        # 1. Direct intent phrases.
        for tool, phrases in USER_INTENT_DIRECT_PHRASES.items():
            for phrase in phrases:
                idx = text.find(phrase)
                if idx == -1:
                    continue
                preceding = text[max(0, idx - 20) : idx]
                if any(preceding.endswith(prefix) for prefix in USER_INTENT_OBSERVATIONAL_PREFIXES):
                    continue
                granted.add(tool)
                break

        # 2. Bare-imperative first-word.
        first_word_match = re.match(r"^\s*([a-z]+)\s+\S+", text)
        if first_word_match:
            first_word = first_word_match.group(1)
            if first_word in USER_INTENT_IMPERATIVE_TOOLS:
                tool_key = USER_INTENT_IMPERATIVE_MAP.get(first_word)
                if tool_key:
                    granted.add(tool_key)

    # 3. Verb-near-token co-occurrence (always runs, even on
    # token-dump-leading prompts — explicit grants override the dump
    # heuristic).
    verb_positions: list[int] = []
    for verb in USER_INTENT_GRANT_VERB_PHRASES:
        start = 0
        while True:
            idx = text.find(verb, start)
            if idx == -1:
                break
            verb_positions.append(idx + len(verb))
            start = idx + 1

    if verb_positions:
        for pattern, tool in USER_INTENT_TOOL_TOKEN_PATTERNS.items():
            for match in re.finditer(pattern, text):
                token_start = match.start()
                if any(
                    0 <= token_start - verb_end <= USER_INTENT_GRANT_PROXIMITY
                    for verb_end in verb_positions
                ):
                    granted.add(tool)
                    break

    return granted


def detect_config_grants_v2(prompt: str) -> dict[str, object]:
    """Per-turn config grant map. Empty dict on no match (common case)."""
    if not prompt:
        return {}
    text = prompt.strip()
    if not text:
        return {}
    lower = text.lower()
    grants: dict[str, object] = {}
    for pattern, spec in CONFIG_GRANT_PATTERNS:
        for m in re.finditer(pattern, lower):
            key = m.group(1).strip()
            if not key:
                continue
            if spec == "on":
                grants[key] = True
            elif spec == "off":
                grants[key] = False
            elif spec == "toggle":
                grants[key] = "__toggle__"
            elif spec == "capture":
                raw_value = m.group(2)
                grants[key] = parse_config_value(raw_value)
    return grants
