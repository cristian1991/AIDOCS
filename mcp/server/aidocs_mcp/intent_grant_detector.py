"""Lemma-based multi-language accept/deny detector for user-intent grants.

Design (2026-04-23, replaces the 40-phrase regex list in claude_hook.py):

Two independent layers, both run on every UserPromptSubmit.

**Layer 1 — accept/deny classification** (tool-independent).
Detects whether the operator is approving or denying action in THIS
prompt, regardless of whether a tool name is present. Handles bare
forms ("agreed", "ok", "proceed", "go ahead", "allowed", "approved")
and contextual forms ("you can use grep", "let you proceed"). Fires
the session's per-turn accept_flag / deny_flag so downstream gates
can lift raw-tool blocks for the turn.

Subject disambiguation: "let you X" grants, "let me X" does NOT.
Contextual approve verbs require a second-person recipient
(you/agent/claude) within proximity. Scopeless acknowledgments
(yes/agreed/proceed/ok) are implicitly operator-to-agent in this
context and do not need disambiguation.

**Layer 2 — tool surfacing** (tool-dependent).
Independent scan for tool-keyword / domain-hint mentions. Any prompt
that mentions a tool (grep, read, bash) or a domain (css, kafka,
postgres) surfaces the matching deferred tools via the gate's
per-turn tool grant — no approve verb required. This is the design's
core "tools appear as the user talks" mechanism.

Implementation layers:
  1. stdlib tokenize + casefold + simple punctuation strip
  2. simplemma lemmatize (lazy per-language dict load)
  3. set-membership vs per-language lemma sets from intent_tokens/
  4. rapidfuzz fallback for typo/paraphrase near-misses
  5. lingua-py for language detection (cached)

All optional deps are import-guarded. When any is missing, the
detector falls back to English-only regex (current behavior) so the
base install still works without [nlp] extras.

Licenses:
  lingua-language-detector: Apache-2.0
  simplemma:                 MIT
  rapidfuzz:                 MIT
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Optional dep probes ────────────────────────────────────────────
try:
    from lingua import Language, LanguageDetectorBuilder  # type: ignore

    _HAS_LINGUA = True
except ImportError:
    _HAS_LINGUA = False

# simplemma dropped 2026-05-13 (king doctrine). Multilingual
# lemmatization now goes through aidocs_nlp.NLPService which uses
# spaCy per loaded language model. This file's _lemmatize() helper
# is now a no-op pass-through kept for call-site compatibility.

try:
    from rapidfuzz import fuzz  # type: ignore

    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False


# ── Data shapes ────────────────────────────────────────────────────
@dataclass(frozen=True)
class GrantDetection:
    """Result of one prompt scan.

    accept / deny are booleans — the prompt expresses approval or
    refusal overall. Only one should be True; both True means the
    prompt is mixed (unusual — caller decides tie-break).

    granted_tools is the Layer-2 surface of tools mentioned in the
    prompt. Independent of accept/deny: a prompt can mention tools
    without granting anything ("i use grep for searching") — the
    surface still fires so downstream systems can make those tools
    eagerly available.

    language is the detected ISO 639-1 code (en, de, es, ...) or
    None when detection failed / dep missing.
    """

    accept: bool = False
    deny: bool = False
    has_negation: bool = False
    granted_tools: frozenset[str] = field(default_factory=frozenset)
    surfaced_domains: frozenset[str] = field(default_factory=frozenset)
    language: str | None = None
    confidence: float = 1.0
    reasons: tuple[str, ...] = ()


# ── Language detection (cached) ────────────────────────────────────
_LANG_DETECTOR = None


def _get_lang_detector():
    global _LANG_DETECTOR
    if _LANG_DETECTOR is None and _HAS_LINGUA:
        # Restrict to languages we actually ship intent tokens for.
        # Adding languages here should mirror intent_tokens/<lang>.toml
        # availability. Unsupported langs fall back to English.
        langs = [
            Language.ENGLISH,
            Language.GERMAN,
            Language.SPANISH,
            Language.FRENCH,
            Language.ITALIAN,
            Language.PORTUGUESE,
            Language.JAPANESE,
            Language.CHINESE,
        ]
        _LANG_DETECTOR = (
            LanguageDetectorBuilder.from_languages(*langs).with_preloaded_language_models().build()
        )
    return _LANG_DETECTOR


_LINGUA_TO_ISO = {
    "ENGLISH": "en",
    "GERMAN": "de",
    "SPANISH": "es",
    "FRENCH": "fr",
    "ITALIAN": "it",
    "PORTUGUESE": "pt",
    "JAPANESE": "ja",
    "CHINESE": "zh",
}


def detect_language(text: str) -> str:
    """Return ISO 639-1 code. Defaults to 'en' on miss or missing dep.

    Config override (2026-04-24): `nlp.language` forces a specific
    language and bypasses lingua entirely. Default 'en' — set to
    'auto' to re-enable lingua detection. Motivated by short English
    prompts getting misdetected as ES/PT/IT and missing tool aliases
    only present in en.toml. English-default avoids the issue without
    dropping multilang support for operators who opt in.
    """
    try:
        from .config import get_setting
        from .mcp_server_runtime_helpers import resolve_project_root

        override = (
            (
                get_setting(
                    "nlp.language",
                    project_root=resolve_project_root(),
                    default="en",
                )
                or "en"
            )
            .strip()
            .lower()
        )
    except Exception:
        override = "en"
    if override and override != "auto":
        return override
    if not text.strip():
        return "en"
    detector = _get_lang_detector()
    if detector is None:
        return "en"
    try:
        lang = detector.detect_language_of(text)
        if lang is None:
            return "en"
        return _LINGUA_TO_ISO.get(lang.name, "en")
    except Exception:
        return "en"


# ── Lemma set loader ───────────────────────────────────────────────
@dataclass(frozen=True)
class LemmaSets:
    approve_verbs: frozenset[str]
    deny_verbs: frozenset[str]
    scopeless_accept: frozenset[str]  # no subject disambiguation needed
    scopeless_deny: frozenset[str]
    second_person: frozenset[str]  # "you", "agent", "claude", ...
    first_person: frozenset[str]  # "i", "me", "we" — NOT grant when object
    negation_markers: frozenset[str]  # "not", "don't", "never"
    tool_keywords: dict[str, frozenset[str]]  # tool_name -> set of aliases
    domain_keywords: dict[str, frozenset[str]]  # domain -> set of terms


# Hardcoded English fallback when intent_tokens/en.toml lacks grant
# sections (back-compat during rollout). Kept minimal — the toml is
# the authoritative source once populated.
_EN_FALLBACK = LemmaSets(
    approve_verbs=frozenset(
        {
            "allow",
            "permit",
            "grant",
            "approve",
            "authorize",
            "enable",
            "let",
            "use",
            "run",
            "proceed",
            "unlock",
            "greenlight",
            "clear",
            "accept",
            "ok",
            "okay",
        },
    ),
    deny_verbs=frozenset(
        {
            "deny",
            "block",
            "refuse",
            "forbid",
            "reject",
            "disallow",
            "stop",
            "avoid",
            "prevent",
        },
    ),
    scopeless_accept=frozenset(
        {
            "agreed",
            "accepted",
            "approved",
            "allowed",
            "permitted",
            "authorized",
            "confirmed",
            "greenlight",
            "greenlit",
            "yes",
            "yep",
            "yeah",
            "sure",
            "fine",
            "ok",
            "okay",
            "proceed",
            "continue",
            "go",
            "gucci",
        },
    ),
    scopeless_deny=frozenset(
        {
            "no",
            "nope",
            "nah",
            "denied",
            "rejected",
            "blocked",
            "refuse",
            "stop",
            "halt",
            "cancel",
        },
    ),
    second_person=frozenset(
        {
            "you",
            "agent",
            "claude",
            "assistant",
            "bot",
            "ai",
            "you're",
            "youre",
            "u",
        },
    ),
    first_person=frozenset(
        {
            "i",
            "me",
            "my",
            "we",
            "us",
            "our",
            "myself",
            "ourselves",
            "i'll",
            "ill",
            "i've",
            "ive",
            "i'd",
            "id",
        },
    ),
    negation_markers=frozenset(
        {
            "not",
            "never",
            "no",
            "don't",
            "dont",
            "doesn't",
            "doesnt",
            "won't",
            "wont",
            "can't",
            "cant",
            "shouldn't",
            "shouldnt",
            "avoid",
        },
    ),
    tool_keywords={
        # Host tools (raw, discouraged under managed mode but real)
        "grep": frozenset({"grep"}),
        "read": frozenset({"read", "view", "show", "cat"}),
        "bash": frozenset({"bash", "shell", "powershell"}),
        "glob": frozenset({"glob"}),
        # ── AIDOCS code lookup ──
        "ai_find": frozenset(
            {
                "find",
                "search",
                "lookup",
                "where",
                "locate",
                "symbol",
                "function",
                "class",
                "callers",
                "references",
            },
        ),
        "ai_investigate": frozenset(
            {
                "investigate",
                "understand",
                "explain",
                "architecture",
                "overview",
                "explore",
                "navigate",
            },
        ),
        "ai_bundle": frozenset({"bundle", "outline", "structure"}),
        "ai_trace": frozenset({"trace", "flow", "lineage", "follow"}),
        "ai_text_search": frozenset({"text", "string"}),
        "schema_query": frozenset(
            {
                "schema",
                "table",
                "column",
                "entity",
                "database",
                "db",
                "postgres",
                "sql",
            },
        ),
        "ai_get_symbol_snippet": frozenset(
            {
                "snippet",
                "definition",
                "body",
                "implementation",
            },
        ),
        "ai_get_lines": frozenset({"lines", "view", "show"}),
        # ── Edit ──
        "ai_replace": frozenset(
            {
                "replace",
                "swap",
                "substitute",
                "edit",
                "modify",
                "rewrite",
            },
        ),
        "ai_create_file": frozenset({"create"}),
        "ai_insert_lines": frozenset({"insert"}),
        # ── File-format readers ──
        "ai_read_pdf": frozenset({"pdf", "document"}),
        "ai_read_excel": frozenset({"excel", "xlsx", "spreadsheet", "csv"}),
        "ai_read_docx": frozenset({"docx", "word"}),
        "ai_read_sqlite": frozenset({"sqlite"}),
        "ai_read_jsonl": frozenset({"jsonl", "ndjson"}),
        "ai_read_raw": frozenset({"raw", "binary"}),
        # ── Memory ──
        "memory_read": frozenset({"memory", "remember", "recall", "notes"}),
        "memory_search": frozenset({"recall"}),
        "memory_capture": frozenset({"capture", "save"}),
        # ── Shell / run ──
        "ai_run": frozenset(
            {
                "run",
                "execute",
                "test",
                "build",
                "pytest",
                "npm",
                "cargo",
            },
        ),
        # ── Task lifecycle (was task_begin/complete/update/status) ──
        "ai_task": frozenset(
            {
                "task",
                "begin",
                "start",
                "complete",
                "finish",
                "done",
                "update",
                "progress",
            },
        ),
        # ── Session lifecycle (was session_connect/list/create/etc) ──
        "ai_session": frozenset(
            {
                "session",
                "connect",
                "bind",
                "resume",
                "claim",
                "release",
            },
        ),
        # ── Lane / plan (was conductor_lane_*/conductor_mode_*/plan_*) ──
        "ai_lane_control": frozenset({"lane", "pause", "cancel"}),
        "ai_lane_send": frozenset({"send"}),
        "ai_lane_inbox": frozenset({"inbox"}),
        "ai_lane_summary": frozenset({"summary"}),
        "ai_lane_exit": frozenset({"exit", "leave"}),
        "ai_plan_create": frozenset({"plan", "roadmap", "spec"}),
        "ai_plan_dispatch": frozenset({"dispatch", "parallel"}),
        "ai_plan_status": frozenset({"plan-status"}),
        "ai_plan_graph": frozenset({"graph", "topology", "lanes"}),
        "ai_plan_template": frozenset({"template"}),
        # ── Spawn (was agent_spawn_worker / agent_worker_*) ──
        "ai_spawn": frozenset({"spawn", "worker"}),
        "ai_status": frozenset({"status"}),
        "ai_jobs": frozenset({"jobs"}),
        "ai_kill": frozenset({"kill", "terminate"}),
        # ── Comms ──
        "ai_msg": frozenset({"message", "tell", "notify"}),
        "ai_qa": frozenset({"ask", "question", "answer"}),
        "ai_review": frozenset({"review", "approve", "deny", "verdict"}),
        # ── Slop / cleanup ──
        "ai_slop": frozenset(
            {
                "dead",
                "unused",
                "deadcode",
                "duplicate",
                "dedupe",
                "clone",
                "hotspot",
                "hot",
                "churn",
                "cleanup",
                "mismatch",
                "slop",
                "stale",
                "untested",
                "extract",
                "refactor",
                "split",
            },
        ),
        # ── Misc ──
        "git_ops": frozenset(
            {"git", "commit", "push", "pull", "rebase", "branch", "diff", "merge", "stash"},
        ),
        "ai_backlog": frozenset({"backlog"}),
        "ai_todo": frozenset({"todo", "checklist"}),
        "ai_protect": frozenset({"protect", "lock", "freeze"}),
    },
    domain_keywords={
        "css": frozenset({"css", "scss", "sass", "styles", "tailwind"}),
        "kafka": frozenset({"kafka", "broker", "topic", "consumer"}),
        "postgres": frozenset({"postgres", "postgresql", "psql"}),
        "git": frozenset({"git", "commit", "branch", "rebase"}),
    },
)


_LEMMASETS_BY_LANG: dict[str, LemmaSets] = {}

# Phase 6d (2026-05-14): cache stored alias POS per language, populated
# lazily from intent_lemma_sets.pos. Cleared alongside _LEMMASETS_BY_LANG
# when tests reset the in-process vocab cache.
_ALIAS_POS_CACHE: dict[str, dict[str, str]] = {}

# Phase 6d retry (2026-05-14): the "verb-required" alias set is the
# action_token kind's token universe. If a tool alias's surface
# matches a verb in action_token, the prompt MUST use it as a VERB
# (or AUX/ADJ-participle) for the surface to surface; noun usage is
# suppressed. Cleaner than per-alias single-word POS enrichment
# (which biased toward NOUN out of context, VERB inside synthetic
# probe sentences).
_VERB_REQUIRED_CACHE: dict[str, frozenset[str]] = {}

# Command words that are ALSO common English nouns. They must be VERB-used in
# the prompt to surface their tool, so the animal "cat", "the head of the
# queue", or "a nice touch" don't surface read/etc. (operator red-team
# 2026-06-29). Supplements the DB-sourced action_token set (unioned, so DB
# additions still apply); the per-prompt spaCy POS check in detect_grant does
# the actual noun-context suppression.
_CODE_VERB_REQUIRED: frozenset[str] = frozenset(
    {
        "cat", "find", "make", "touch", "head", "tail", "tree", "watch",
        "patch", "sort", "mount", "ping", "tag", "log", "diff",
    },
)


def _get_verb_required_aliases(lang: str) -> frozenset[str]:
    """Return the frozenset of alias surface forms that REQUIRE verb
    usage in the prompt to surface their tool. Sourced from the
    action_token kind in intent_lemma_sets.
    """
    lang_ = (lang or "en").lower()
    cached = _VERB_REQUIRED_CACHE.get(lang_)
    if cached is not None:
        return cached
    try:
        from . import intent_tokens_store as _store

        tokens = _store.get_action_verb_tokens(lang_)
    except Exception:
        tokens = set()
    fs = frozenset(tokens) | _CODE_VERB_REQUIRED
    _VERB_REQUIRED_CACHE[lang_] = fs
    return fs


def _get_alias_pos_map(lang: str) -> dict[str, str]:
    """Return {alias_token: pos_tag} for one language. Lazy + cached."""
    lang_ = (lang or "en").lower()
    cached = _ALIAS_POS_CACHE.get(lang_)
    if cached is not None:
        return cached
    try:
        import sqlite3

        from . import intent_tokens_store as _store

        _store.init_db()
        with sqlite3.connect(str(_store.empire_db_path())) as conn:
            rows = conn.execute(
                """SELECT token, pos FROM intent_lemma_sets
                   WHERE lang = ? AND pos <> ''""",
                (lang_,),
            ).fetchall()
    except Exception:
        rows = []
    out: dict[str, str] = {}
    for token, pos in rows:
        if not token or not pos:
            continue
        out[token] = pos
    _ALIAS_POS_CACHE[lang_] = out
    return out


def _clear_alias_pos_cache() -> None:
    """Drop the in-process alias-POS + verb-required caches. Tests
    call this after they write rows so subsequent reads see the new data.
    """
    _ALIAS_POS_CACHE.clear()
    _VERB_REQUIRED_CACHE.clear()


def _alias_first_person_agent(
    doc: object,
    alias: str,
    first_person: frozenset[str],
    second_person: frozenset[str],
) -> bool:
    """True when the alias appears in a first-person-agent context —
    operator describing their OWN action, not asking the agent.

    Cross-language structural detection via spaCy morph + dep:
      1. Find the alias token in the prompt.
      2. Walk to the head of the relevant clause (the alias itself if
         it's a verb, else its head verb).
      3. Look for an `nsubj` child of that verb.
      4. Check the subject's `Person` morph feature ('1' = first
         person) AND check it's not a second-person word in the
         language's pronoun set (defensive — some models tag
         "you" with Person=2 but flag inconsistently).

    Falls back to the word-list scan (per-language first_person /
    second_person) when morph/dep info is missing (pure tokenizer
    pipeline for an unloaded language).
    """
    if not doc or not alias:
        return False
    try:
        tokens = list(getattr(doc, "tokens", []) or [])
    except Exception:
        return False
    if not tokens:
        return False
    a = alias.lower()

    def _is_first_person_token(tok) -> bool:
        morph = getattr(tok, "morph", None) or {}
        if isinstance(morph, dict):
            person = str(morph.get("Person", "") or "").strip()
            if person == "1":
                surface = (getattr(tok, "text", "") or "").lower()
                # Defensive: avoid mis-tagged 2nd-person tokens.
                if surface in second_person:
                    return False
                return True
        return False

    for i, tok in enumerate(tokens):
        surface = (getattr(tok, "text", "") or "").lower()
        lemma = (getattr(tok, "lemma", "") or "").lower()
        if surface != a and lemma != a:
            continue
        # Identify the relevant verb head: if alias is a verb, use it;
        # else walk to its head.
        head_idx = i
        if (getattr(tok, "pos", "") or "").upper() != "VERB":
            try:
                head_idx = int(getattr(tok, "head_idx", i))
            except Exception:
                head_idx = i
        if head_idx < 0 or head_idx >= len(tokens):
            head_idx = i
        # Search for an nsubj child of head_idx.
        subj_first_person = False
        subj_second_person = False
        for j, child in enumerate(tokens):
            try:
                child_head = int(getattr(child, "head_idx", j))
            except Exception:
                continue
            if child_head != head_idx:
                continue
            child_dep = (getattr(child, "dep", "") or "").lower()
            if child_dep not in ("nsubj", "nsubjpass"):
                continue
            if _is_first_person_token(child):
                subj_first_person = True
            child_surface = (getattr(child, "text", "") or "").lower()
            if child_surface in second_person:
                subj_second_person = True
        if subj_first_person and not subj_second_person:
            return True
        # Fallback: word-list scan in the proximity window before the
        # alias (handles tokenizer-only pipelines without morph/dep).
        start = max(0, i - _PROXIMITY_TOKENS)
        first_person_pos = -1
        second_person_pos = -1
        for j in range(start, i):
            prev = tokens[j]
            prev_surface = (getattr(prev, "text", "") or "").lower()
            if prev_surface in first_person:
                first_person_pos = j
            elif prev_surface in second_person:
                second_person_pos = j
        if first_person_pos >= 0 and second_person_pos <= first_person_pos:
            return True
    return False


def _alias_in_noun_context(doc: object, alias: str) -> bool:
    """True when the matched alias token appears in a CLEAR noun context
    in the prompt — preceded by a determiner / possessive / demonstrative.

    Phase 6d final rule: instead of trying to identify VERB usage
    (spaCy single-word tags are noisy — refactor → PROPN, edit → NOUN),
    identify NOUN usage and suppress only that. Conservative: defaults
    to "surface" when context is ambiguous. Catches the canonical noise
    case ("the edit was approved" preceded by 'the') without
    over-suppressing imperative verb usage ("refactor the duplicate
    code" — refactor in position 0, not preceded by a determiner).
    """
    if not doc or not alias:
        return False
    try:
        tokens = list(getattr(doc, "tokens", []) or [])
    except Exception:
        return False
    if not tokens:
        return False
    a = alias.lower()
    for i, tok in enumerate(tokens):
        surface = (getattr(tok, "text", "") or "").lower()
        lemma = (getattr(tok, "lemma", "") or "").lower()
        if surface != a and lemma != a:
            continue
        if i == 0:
            return False
        prev = tokens[i - 1]
        prev_pos = (getattr(prev, "pos", "") or "").upper()
        # spaCy's Universal POS DET works cross-language when a
        # per-lang model is loaded. When only the tokenizer fallback
        # is available (no POS) we can't tell — defer to surface.
        if prev_pos == "DET":
            return True
    return False


def _doc_pos_for_alias(doc: object, alias: str) -> str:
    """Return the dominant POS of the prompt token(s) matching `alias`.

    Multi-word alias → POS of the alias's HEAD token in the prompt
    span (the verb if any, else the first alpha). Single-word →
    POS of the matching prompt token (by surface or lemma).
    Empty string if no match.
    """
    if not doc or not alias:
        return ""
    try:
        tokens = list(getattr(doc, "tokens", []) or [])
    except Exception:
        return ""
    if not tokens:
        return ""
    alias_words = alias.lower().split()
    n = len(tokens)
    # Multi-word: scan for contiguous surface/lemma run matching alias_words.
    if len(alias_words) > 1:
        for i in range(n - len(alias_words) + 1):
            run = tokens[i : i + len(alias_words)]
            ok = True
            for t, w in zip(run, alias_words):
                surface = (getattr(t, "text", "") or "").lower()
                lemma = (getattr(t, "lemma", "") or "").lower()
                if surface != w and lemma != w:
                    ok = False
                    break
            if ok:
                # Prefer the VERB head; else first alpha.
                for t in run:
                    if (getattr(t, "pos", "") or "") == "VERB":
                        return "VERB"
                first = run[0]
                return getattr(first, "pos", "") or ""
        return ""
    # Single-word match by surface or lemma.
    w = alias_words[0]
    for t in tokens:
        surface = (getattr(t, "text", "") or "").lower()
        lemma = (getattr(t, "lemma", "") or "").lower()
        if surface == w or lemma == w:
            return getattr(t, "pos", "") or ""
    return ""


# ── Registry-sourced tool keywords (hint surfacing, 2026-06-29) ──
# tool_interface is the single source of truth: EVERY registered tool must be
# NLP-surfaceable by name with zero curation. The tool's own name is always a
# key (detect_grant's self-name loop surfaces a literal mention); distinctive,
# non-generic name parts become aliases for verb-phrase mentions. Curated
# aliases in the loaded vocab are PRESERVED (unioned, never dropped). This is
# HINT surfacing only — tool AUTHORITY is gated separately (accept/deny intent +
# the T0/T1 tier system), so a broad hint surface never widens what an operator
# prompt can actually GRANT.
_GENERIC_NAME_TOKENS = frozenset(
    {
        "ai", "get", "set", "list", "run", "read", "write", "create", "new",
        "show", "add", "update", "delete", "file", "files", "lines", "line",
        "info", "data", "tool", "tools", "status", "check", "mode", "all",
        "the", "for", "and",
    },
)

# Grammar / boilerplate words that recur across many tool descriptions and are
# NEVER distinctive. The DF filter below catches most; this is the floor that
# guarantees the flood words (document/file/word/data/agent/session/...) can
# never become a surfacing keyword no matter how the DF math lands.
_KW_STOPLIST = frozenset(
    {
        "the", "and", "for", "with", "via", "per", "use", "used", "uses", "this",
        "that", "from", "into", "onto", "over", "when", "what", "which", "your",
        "you", "its", "any", "all", "one", "two", "not", "are", "was", "has",
        "have", "can", "may", "will", "must", "should", "each", "every", "only",
        "also", "but", "out", "off", "now", "new", "old", "see", "set", "get",
        "run", "read", "write", "list", "show", "add", "name", "names", "value",
        "values", "path", "paths", "file", "files", "line", "lines", "text",
        "data", "tool", "tools", "mode", "modes", "type", "types", "key", "keys",
        "call", "calls", "code", "row", "rows", "arg", "args", "param", "params",
        "return", "returns", "result", "results", "default", "optional", "input",
        "required", "string", "json", "dict", "object", "field", "fields",
        "document", "documents", "agent", "agents", "session", "project", "user",
        "operator", "prompt", "doc", "docs", "word", "words", "content", "context",
        "info", "given", "based", "first", "then", "before", "after",
    },
)

_REGISTRY_TOOL_KEYWORDS: dict[str, frozenset[str]] | None = None


def _registry_tool_keywords() -> dict[str, frozenset[str]]:
    """DISTINCTIVE NLP keywords for every registered tool, from tool_interface
    name + description (single source). A term is a keyword only if DISTINCTIVE:
    it occurs in few tools' name+desc (document-frequency <= _DF_MAX) and is not
    a stoplist word — common words (document/word/file) drop (no flood),
    distinctive concepts (conductor->ai_seat, duplicate->ai_slop) stay. Author
    ToolSpec.aliases are always kept (override for too-common terms like audit).
    Cached."""
    global _REGISTRY_TOOL_KEYWORDS
    if _REGISTRY_TOOL_KEYWORDS is not None:
        return _REGISTRY_TOOL_KEYWORDS
    try:
        from .tool_interface import tool_specs

        specs = tool_specs()
    except Exception:
        _REGISTRY_TOOL_KEYWORDS = {}
        return _REGISTRY_TOOL_KEYWORDS
    _word = re.compile(r"[a-z][a-z0-9]{2,}")
    per_tool: dict[str, set[str]] = {}
    df: dict[str, int] = {}
    for name, spec in specs.items():
        text = (name.replace("_", " ") + " " + (spec.description or "")).lower()
        terms = set(_word.findall(text))
        per_tool[name] = terms
        for t in terms:
            df[t] = df.get(t, 0) + 1
    out: dict[str, frozenset[str]] = {}
    for name, spec in specs.items():
        kws: set[str] = set()
        for part in name.split("_"):  # distinctive name parts (+ singular)
            if part and part not in _GENERIC_NAME_TOKENS and len(part) >= 3:
                kws.add(part)
                if part.endswith("s") and len(part) > 4:
                    kws.add(part[:-1])
        for term in per_tool[name]:
            # VERY distinctive only: long AND rare AND not boilerplate. Keeps
            # 'obfuscation'/'conductor'/'duplicate'; drops 'today'/'body'/'nice'.
            if len(term) >= 8 and term not in _KW_STOPLIST and df.get(term, 99) <= 1:
                kws.add(term)
        for alias in spec.aliases or ():  # explicit author override, always kept
            alias_l = str(alias).strip().lower()
            if alias_l:
                kws.add(alias_l)
                if alias_l.endswith("s") and len(alias_l) > 4:
                    kws.add(alias_l[:-1])
        out[name] = frozenset(kws)
    _REGISTRY_TOOL_KEYWORDS = out
    return _REGISTRY_TOOL_KEYWORDS


def _augment_tool_keywords(sets: LemmaSets) -> LemmaSets:
    """Union registry-sourced tool keys into the loaded vocab. Curated aliases
    win (are preserved); every registered tool becomes at least a self-name key
    so detect_grant surfaces it on a literal mention."""
    from dataclasses import replace

    auto = _registry_tool_keywords()
    if not auto:
        return sets
    merged: dict[str, frozenset[str]] = dict(auto)
    for tool, aliases in sets.tool_keywords.items():
        merged[tool] = frozenset(set(merged.get(tool, frozenset())) | set(aliases))
    return replace(sets, tool_keywords=merged)


def get_lemma_sets(lang: str) -> LemmaSets:
    """Return lemma sets for a language. Cached. Reads from the empire
    intent_tokens store (sqlite). Lazy-seeds on miss from _EN_FALLBACK
    (English) or from intent_tokens/<lang>.toml (one-shot migration for
    other languages, transition only). Per king-directive 2026-05-13:
    intent vocabulary lives in SQL; TOMLs are migration debt.
    """
    lang = (lang or "en").lower()
    if lang in _LEMMASETS_BY_LANG:
        return _LEMMASETS_BY_LANG[lang]
    sets = _load_lang_from_db(lang)
    if sets is None:
        _seed_lang_if_empty(lang)
        sets = _load_lang_from_db(lang)
    if sets is None:
        sets = _EN_FALLBACK
    sets = _augment_tool_keywords(sets)
    _LEMMASETS_BY_LANG[lang] = sets
    return sets


def _load_lang_from_db(lang: str) -> LemmaSets | None:
    """Read lemma sets for `lang` from intent_tokens_store. Returns None
    when the language has no rows in the empire DB.
    """
    try:
        from . import intent_tokens_store as _store

        d = _store.get_lemma_sets_dict(lang)
    except Exception:
        return None
    if not d:
        return None
    return LemmaSets(
        approve_verbs=d.get("approve_verbs") or _EN_FALLBACK.approve_verbs,
        deny_verbs=d.get("deny_verbs") or _EN_FALLBACK.deny_verbs,
        scopeless_accept=d.get("scopeless_accept") or _EN_FALLBACK.scopeless_accept,
        scopeless_deny=d.get("scopeless_deny") or _EN_FALLBACK.scopeless_deny,
        second_person=d.get("second_person") or _EN_FALLBACK.second_person,
        first_person=d.get("first_person") or _EN_FALLBACK.first_person,
        negation_markers=d.get("negation_markers") or _EN_FALLBACK.negation_markers,
        tool_keywords=d.get("tool_keywords") or _EN_FALLBACK.tool_keywords,
        domain_keywords=d.get("domain_keywords") or _EN_FALLBACK.domain_keywords,
    )


def _seed_lang_if_empty(lang: str) -> None:
    """Seed English from the Python literal if the empire DB is missing
    rows. Non-English langs that come up empty inherit `_EN_FALLBACK`
    in-memory (the get_lemma_sets caller already does that). No TOML
    fallback — intent vocabulary is SQL-only per king-directive
    2026-05-13. Idempotent under INSERT OR IGNORE.
    """
    if lang != "en":
        return
    try:
        from . import intent_tokens_store as _store
    except Exception:
        return
    if _store.has_any_rows("en"):
        return
    _store.seed_lang(
        "en",
        approve_verbs=_EN_FALLBACK.approve_verbs,
        deny_verbs=_EN_FALLBACK.deny_verbs,
        scopeless_accept=_EN_FALLBACK.scopeless_accept,
        scopeless_deny=_EN_FALLBACK.scopeless_deny,
        second_person=_EN_FALLBACK.second_person,
        first_person=_EN_FALLBACK.first_person,
        negation=_EN_FALLBACK.negation_markers,
        tool_aliases={t: list(a) for t, a in _EN_FALLBACK.tool_keywords.items()},
        domain_aliases={d: list(a) for d, a in _EN_FALLBACK.domain_keywords.items()},
        source="en_fallback_literal",
    )


# _load_lang_toml removed 2026-05-14 — intent vocabulary lives in
# the empire intent-tokens store (sqlite) via _load_lang_from_db.
# TOML files are no longer read by any active consumer.


# ── Tokenization + lemmatization ───────────────────────────────────
_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Cheap word tokenizer. Keeps apostrophes so contractions stay
    intact ("don't", "you're") for the second/first-person lookup.
    """
    return [t.lower() for t in _TOKEN_RE.findall(text.lower())]


def _lemmatize(tokens: list[str], lang: str) -> list[str]:
    """No-op since simplemma was dropped 2026-05-13. Callers wanting
    real lemmatization should route through aidocs_nlp.NLPService
    instead of this helper. English casefold is close enough for the
    lemma sets this module's legacy detector ships.
    """
    return tokens


# ── Detection pipeline ─────────────────────────────────────────────
_PROXIMITY_TOKENS = 6  # how many tokens between approve verb and tool/recipient


def _has_negation_near(
    tokens: list[str],
    idx: int,
    neg: frozenset[str],
    window: int = 3,
) -> bool:
    """True if a negation marker appears within `window` tokens BEFORE
    position `idx`. "don't read X" → negation before read.
    """
    lo = max(0, idx - window)
    return any(t in neg for t in tokens[lo:idx])


def _recipient_near(
    tokens: list[str],
    idx: int,
    second_person: frozenset[str],
    first_person: frozenset[str],
    window: int = 3,
) -> str | None:
    """Inspect tokens within `window` of the approve verb. Returns:
      "second" — grant shape: verb's direct object is second-person.
      "first"  — self-action shape: verb's direct object is first-person.
      None     — no pronoun in window.

    The DIRECT OBJECT after the verb is the grant target. Subject
    before the verb is who is doing the granting.
      "I let you grep"       → subject=I, object=you → grant     ("second")
      "the agent lets me X"  → subject=agent, object=me → NOT grant ("first")
      "let you grep"         → no subject, object=you → grant     ("second")
      "you can allow"        → object position empty, subject=you → ambiguous;
                               fall back to nearest-occurrence.

    Strategy: find the closest pronoun AFTER the verb (object slot).
    If no pronoun after verb, fall back to closest pronoun in window
    regardless of direction (handles imperative / fronted-modal cases).
    """
    lo = max(0, idx - window)
    hi = min(len(tokens), idx + window + 1)

    # First, scan AFTER the verb for an object-slot pronoun.
    for i in range(idx + 1, hi):
        t = tokens[i]
        if t in second_person:
            return "second"
        if t in first_person:
            return "first"

    # No pronoun after the verb — fall back to closest in window.
    nearest_second: int | None = None
    nearest_first: int | None = None
    for i in range(lo, idx):
        t = tokens[i]
        if t in second_person and nearest_second is None:
            nearest_second = idx - i
        if t in first_person and nearest_first is None:
            nearest_first = idx - i
    if nearest_second is not None and nearest_first is not None:
        return "second" if nearest_second <= nearest_first else "first"
    if nearest_second is not None:
        return "second"
    if nearest_first is not None:
        return "first"
    return None


def detect_grant(prompt: str) -> GrantDetection:
    """Top-level entry point. Runs both layers independently."""
    text = (prompt or "").strip()
    if not text:
        return GrantDetection()

    lang = detect_language(text)
    sets = get_lemma_sets(lang)
    tokens = _tokenize(text)
    if not tokens:
        return GrantDetection(language=lang)
    lemmas = _lemmatize(tokens, lang)

    accept = False
    deny = False
    reasons: list[str] = []

    # Layer 1a: scopeless acknowledgments. "agreed", "ok", "go ahead",
    # "allowed", "permitted". No subject disambiguation. Multi-word
    # phrases like "go ahead" are handled via a phrase scan below.
    #
    # Match against BOTH the original token AND its lemma: the
    # scopeless_* sets contain natural past-tense/adjective forms like
    # "agreed", "approved", "authorized" that simplemma reduces to
    # different stems ("agree", "approve", "authorize"). Without the
    # raw-token fallback, "agreed" as a standalone acknowledgment
    # wouldn't match anything.
    for i, (tok, lem) in enumerate(zip(tokens, lemmas)):
        if tok in sets.scopeless_accept or lem in sets.scopeless_accept:
            if not _has_negation_near(lemmas, i, sets.negation_markers):
                accept = True
                matched = tok if tok in sets.scopeless_accept else lem
                reasons.append(f"scopeless_accept:{matched}")
                break
    for i, (tok, lem) in enumerate(zip(tokens, lemmas)):
        if tok in sets.scopeless_deny or lem in sets.scopeless_deny:
            if not _has_negation_near(lemmas, i, sets.negation_markers):
                deny = True
                matched = tok if tok in sets.scopeless_deny else lem
                reasons.append(f"scopeless_deny:{matched}")
                break

    # Layer 1b: multi-word bare phrases ("go ahead", "green light").
    # Check text directly — these don't lemmatize cleanly.
    text_cf = text.lower()
    for phrase in (
        "go ahead",
        "green light",
        "greenlight",
        "you bet",
        "of course",
        "all good",
        "looks good",
    ):
        if phrase in text_cf and not accept:
            accept = True
            reasons.append(f"phrase_accept:{phrase}")
            break

    # Layer 1c: contextual approve verbs with subject disambiguation.
    #   "let you grep"       → second-person recipient → grant
    #   "use the read tool"  → imperative (verb leads, no subject)
    #                          → grant (operator-to-agent by convention)
    #   "let me grep"        → first-person object → NOT a grant
    #   "i use grep daily"   → first-person subject → NOT a grant
    if not accept:
        for i, lem in enumerate(lemmas):
            if lem not in sets.approve_verbs:
                continue
            if _has_negation_near(lemmas, i, sets.negation_markers):
                continue
            recipient = _recipient_near(
                tokens,
                i,
                sets.second_person,
                sets.first_person,
                window=_PROXIMITY_TOKENS,
            )
            if recipient == "second":
                accept = True
                reasons.append(f"contextual_accept:{lem}")
                break
            # Imperative mood: approve verb at position 0 or 1 (allowing
            # a leading discourse marker like "please"/"now") AND no
            # first-person pronoun anywhere in the prompt. Covers
            # "use read tool", "run bash command", "grep that file".
            if recipient is None and i <= 1 and not any(t in sets.first_person for t in tokens):
                accept = True
                reasons.append(f"imperative_accept:{lem}")
                break

    # Layer 1d: contextual deny verbs ("don't read", "block bash").
    if not deny:
        for i, lem in enumerate(lemmas):
            if lem not in sets.deny_verbs:
                continue
            deny = True
            reasons.append(f"contextual_deny:{lem}")
            break

    # Layer 2: tool surfacing. Independent of accept/deny. Any tool
    # keyword mentioned in the prompt surfaces that tool.
    #
    # Phase 6c (2026-05-14): handle multi-word aliases via word-boundary
    # regex on the lowercased prompt text. Single-word aliases keep
    # the token/lemma set-membership path (faster than regex per
    # token). Multi-word matching is the real win — "commit and push"
    # now atomically matches git_commit_and_push regardless of
    # tokenizer splits.
    #
    # POS-filter pass tried and rolled back 2026-05-14: single-word
    # POS enrichment is unreliable (spaCy biases toward NOUN out of
    # context; verb-context probe over-promotes to VERB). The pos
    # column stays in intent_lemma_sets for future use (vector
    # centroids, dependency-based scoping) but detect_grant does NOT
    # gate on it. Cleaner to surface a few extra tools than to
    # silently drop legitimate matches.
    granted_tools: set[str] = set()
    token_set = set(tokens)
    lemma_set = set(lemmas)
    text_lower = text.lower()
    # Self-name surfacing (operator finding 2026-06-11): mentioning a
    # tool by its LITERAL NAME ("issue with ai_protect") must surface
    # that tool — the alias sets are verb-flavored (protect/lock/freeze)
    # and never contained the tool's own name, so exact mentions
    # surfaced nothing. Explicit names are unambiguous: no POS filter.
    for tool in sets.tool_keywords:
        if tool in token_set or re.search(rf"\b{re.escape(tool)}\b", text_lower):
            granted_tools.add(tool)
            reasons.append(f"tool_self_name:{tool}")
    # Collect ALL matching aliases per tool so the POS-filter below
    # has multiple shots: if alias #1 gets suppressed (noun context /
    # first-person), alias #2 may still surface the tool. Without
    # this, "after the commit, push to main" suppresses git_ops on
    # "commit" (noun) and never gets to try "push" (verb).
    _matches: list[tuple[str, str, bool]] = []  # (tool, alias, is_multi)
    for tool, aliases in sets.tool_keywords.items():
        for alias in aliases:
            is_multi = " " in alias
            if is_multi:
                if re.search(rf"\b{re.escape(alias)}\b", text_lower):
                    _matches.append((tool, alias, True))
            elif alias in token_set or alias in lemma_set:
                _matches.append((tool, alias, False))

    # Phase 6d retry: verb-required POS filter. Aliases whose surface
    # appears in action_token's token universe (edit/fix/commit/push/
    # grep/build/…) MUST be VERB-used in the prompt to surface their
    # tool. Catches "the edit was approved" (alias 'edit' tags NOUN
    # → suppress) without over-suppressing legitimate verb usages
    # like "grep the logs" (alias 'grep' tags VERB → surface).
    # Only invokes spaCy when at least one match's alias is in the
    # verb-required set; pure-noun matches skip the analyze cost.
    verb_required = _get_verb_required_aliases(lang)
    needs_pos = verb_required and any(
        (not is_multi) and (a in verb_required) for _t, a, is_multi in _matches
    )
    _doc = None
    if needs_pos:
        try:
            from .aidocs_nlp.service import get_service

            _doc = get_service(Path.cwd(), {}).analyze(text, language=lang)
        except Exception:
            _doc = None

    for tool, alias, is_multi in _matches:
        if (not is_multi) and alias in verb_required and _doc is not None:
            if _alias_in_noun_context(_doc, alias):
                reasons.append(f"tool_noun_context_suppressed:{tool}:alias={alias}")
                continue
            if _alias_first_person_agent(
                _doc,
                alias,
                sets.first_person,
                sets.second_person,
            ):
                reasons.append(f"tool_first_person_suppressed:{tool}:alias={alias}")
                continue
        granted_tools.add(tool)
        reasons.append(f"tool_surfaced:{tool}")

        # Layer 2b: domain hints.
    surfaced_domains: set[str] = set()
    for domain, terms in sets.domain_keywords.items():
        if token_set & terms:
            surfaced_domains.add(domain)
            reasons.append(f"domain_surfaced:{domain}")

    # Layer 2c: rapidfuzz fallback for tool typos. Only fires when no
    # exact tool match found AND prompt is short (long prompts are
    # noisy and false-positive-prone).
    #
    # Phase 6d guard (2026-05-14): fuzzy fallback honors the same
    # verb-required POS check as the main path. Without this, a
    # noun-context prompt ("the edit was approved") that suppressed
    # ai_replace via POS would have re-surfaced it here through
    # fuzz.ratio("edit", "edit") == 100.
    if not granted_tools and _HAS_RAPIDFUZZ and len(tokens) <= 20:
        for tool, aliases in sets.tool_keywords.items():
            matched_via = None
            for token in tokens:
                for alias in aliases:
                    if fuzz.ratio(token, alias) >= 85:
                        matched_via = (token, alias)
                        break
                if matched_via:
                    break
            if not matched_via:
                continue
            token, alias = matched_via
            if alias in verb_required and _doc is not None:
                if _alias_in_noun_context(_doc, alias):
                    reasons.append(f"tool_fuzzy_noun_context_suppressed:{tool}:alias={alias}")
                    continue
                if _alias_first_person_agent(
                    _doc,
                    alias,
                    sets.first_person,
                    sets.second_person,
                ):
                    reasons.append(f"tool_fuzzy_first_person_suppressed:{tool}:alias={alias}")
                    continue
            granted_tools.add(tool)
            reasons.append(f"tool_fuzzy:{tool}:{token}")

    # Prompt-global negation: any negation marker (e.g. "never", "don't",
    # "can't") anywhere in the lemmatized stream. Used by Layer-2
    # callers to suppress passive-mention auto-sticky writes when the
    # operator's prompt carries a negation signal — even if no full
    # deny verb fired. See security-gates.md §5 "Layer-2 negation
    # suppression" (canonical 2026-04-28).
    has_negation = bool(set(lemmas) & sets.negation_markers) or bool(
        set(tokens) & sets.negation_markers,
    )

    return GrantDetection(
        accept=accept,
        deny=deny,
        has_negation=has_negation,
        granted_tools=frozenset(granted_tools),
        surfaced_domains=frozenset(surfaced_domains),
        language=lang,
        reasons=tuple(reasons),
    )


# ── Destructive-intent detection (Phase 4 of backlog #15) ─────────

# Tokens signaling operator intent for destructive action. If the
# prompt contains any of these (standalone word match) AND the judge
# later fires a destructive-pattern verdict, the block downgrades to
# ask-state confirm — operator expressed intent, gets final sign-off.
# Absence → judge hard-blocks as today (no intent, no ask).
_DESTRUCTIVE_INTENT_TOKENS: frozenset[str] = frozenset(
    {
        # File/state removal
        "nuke",
        "delete",
        "remove",
        "wipe",
        "erase",
        "destroy",
        "purge",
        "clear",
        "clean",
        "scrub",
        "trash",
        "drop",
        # Git-destructive
        "force",
        "reset",
        "rebase",
        "revert",
        "rollback",
        # Shell-destructive verbs
        "rm",
        "rmdir",
        "unlink",
        # Scope markers that often accompany destructive intent
        "recursive",
        "everything",
        "all",
        "entirely",
    },
)


def detect_destructive_intent_in_text(text: str) -> list[str]:
    """Scan text for standalone destructive-intent tokens. Returns
    sorted list of matched tokens (lowercased). Token boundaries use
    whitespace/punctuation — "recovery" won't match "recur" etc.

    Pure function, no I/O. Used by claude_hook at UserPromptSubmit
    time to stamp user_intent_destructive on the session's query gate.
    """
    if not text:
        return []
    import re

    matched: set[str] = set()
    # Word-boundary scan. Lowercase the prompt once.
    lower = text.lower()
    for token in _DESTRUCTIVE_INTENT_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", lower):
            matched.add(token)
    return sorted(matched)


def detect_destructive_intent(
    query_gate: Any,
    project_root: Any,
    session_id: str,
    verdict: Any = None,
) -> bool:
    """True iff the session's current prompt expressed destructive
    intent (non-empty user_intent_destructive list). Phase 4 of #15:
    orchestrator calls this when the judge fires a destructive-pattern
    verdict; on True → downgrade to ask; on False → hard-block.

    verdict is accepted for future per-rule matching refinements
    (e.g. only downgrade when verdict.rule_id semantically matches a
    destructive-intent token). Current impl: any destructive intent
    token unlocks the ask path for any destructive judge verdict.
    """
    try:
        tokens = query_gate.get_user_intent_destructive(
            project_root,
            session_id,
        )
    except Exception:
        return False
    return bool(tokens)


# ── Availability probe for callers ─────────────────────────────────
def nlp_backend_status() -> dict[str, bool]:
    """Report which optional deps are present. Caller decides whether
    to log a warning about reduced fidelity when deps are missing.
    """
    return {
        "lingua": _HAS_LINGUA,
        "rapidfuzz": _HAS_RAPIDFUZZ,
    }
