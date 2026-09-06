"""Closed-vocabulary phrase detection for UserPromptSubmit hook.

Loads `[__intent_phrases.*]` sections from intent_tokens/*.toml and matches
the operator's prompt against the curated phrase lists. Returns a list of
intent records that downstream handlers (intent_phrase_handlers.py) consume
to make state changes (enter plan mode, etc.) without requiring an agent
tool call.

Design constraints from the spec:
- Substring match on lowercased prompt (case-insensitive operator typing).
- Word-boundary respect — "planning" must not match "plan done".
- requires_scope enforces a non-empty noun phrase after the matched phrase
  before the intent is honored. Bare phrases without scope return an
  *_invalid intent so handlers can inject a corrective message instead of
  silently flipping state.
- Multiple intents per prompt allowed, ordered by detected position.
- Closed vocabulary: every entry is an explicit operator decision in toml.
  No verb+noun heuristic. No fuzzy match.
"""

from __future__ import annotations

import re
from typing import Any

from .intent_guard import _load_intent_token_lists

# Word-boundary regex cache. Built lazily on first detect call so unit
# tests can change the toml between runs without stale state. The toml
# loader (_load_intent_token_lists) caches its own results based on file
# mtime, so phrase reload follows token-list reload semantics for free.
_PHRASE_CACHE: dict[str, list[tuple[re.Pattern[str], str, dict[str, Any]]]] = {}


def _phrase_to_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a phrase into a word-boundary-anchored regex.

    "plan done" → r"\\bplan\\s+done\\b" so we don't false-fire on
    "planned one" or "planning". The phrase is lowercased; the input
    is lowercased at match time.
    """
    parts = phrase.lower().split()
    spaced = r"\s+".join(re.escape(p) for p in parts)
    return re.compile(rf"\b{spaced}\b")


def _build_phrase_cache() -> dict[str, list[tuple[re.Pattern[str], str, dict[str, Any]]]]:
    """Load all `__intent_phrases.*` blocks and compile their phrases.

    Returns a mapping of `intent_name → list of (compiled_pattern, raw_phrase, config_dict)`.
    Config dict carries `requires_scope`, `scope_min_chars`, `scope_proximity_chars`
    so the matcher can apply per-intent rules without re-reading the toml.
    """
    raw = _load_intent_token_lists()
    out: dict[str, list[tuple[re.Pattern[str], str, dict[str, Any]]]] = {}

    for key, value in raw.items():
        # Phrase blocks live under __intent_phrases.<intent_name> and are
        # surfaced by the toml loader as nested dicts; defensive isinstance
        # because the legacy lists at the same level are flat tuples.
        if not key.startswith("__intent_phrases."):
            continue
        if not isinstance(value, dict):
            continue
        intent = key[len("__intent_phrases.") :]
        phrases = value.get("phrases") or []
        if not isinstance(phrases, (list, tuple)):
            continue
        config = {
            "requires_scope": bool(value.get("requires_scope", False)),
            "scope_min_chars": int(value.get("scope_min_chars", 3)),
            "scope_proximity_chars": int(value.get("scope_proximity_chars", 200)),
        }
        compiled = [(_phrase_to_pattern(p), p.lower(), config) for p in phrases if p]
        if compiled:
            out[intent] = compiled
    return out


def _ensure_cache() -> dict[str, list[tuple[re.Pattern[str], str, dict[str, Any]]]]:
    if not _PHRASE_CACHE:
        _PHRASE_CACHE.update(_build_phrase_cache())
    return _PHRASE_CACHE


def _extract_scope(text: str, match_end: int, proximity: int) -> str:
    """Pull the scope text following a matched phrase.

    Stops at sentence terminators (`.` `?` `!` `\\n`) so chained intents
    in one prompt don't bleed scope across to each other ("plan done.
    create a plan for X" → exit_plan_mode has no scope, enter_plan_mode
    scope is "X").
    """
    tail = text[match_end : match_end + proximity]
    # Stop at sentence-end punctuation followed by space, OR newline.
    stop_match = re.search(r"[\.\?!]\s|\n", tail)
    if stop_match:
        tail = tail[: stop_match.start()]
    return tail.strip()


def detect_intent_phrases(prompt: str) -> list[dict[str, Any]]:
    """Detect closed-vocabulary intent phrases in a prompt.

    Returns a list of intent records ordered by detected position. Each
    record:
        {
            "intent": "<name>" | "<name>_invalid",
            "matched_phrase": "<exact phrase from toml>",
            "scope": "<extracted text>",   # empty when not requires_scope
            "reason": "missing_scope",     # only on _invalid
            "position": <int char offset>, # for stable ordering
        }
    """
    if not prompt:
        return []
    text = prompt.lower()

    cache = _ensure_cache()
    detected: list[dict[str, Any]] = []

    for intent_name, patterns in cache.items():
        for pattern, raw_phrase, config in patterns:
            for m in pattern.finditer(text):
                record: dict[str, Any] = {
                    "intent": intent_name,
                    "matched_phrase": raw_phrase,
                    "scope": "",
                    "position": m.start(),
                }
                if config["requires_scope"]:
                    scope = _extract_scope(text, m.end(), config["scope_proximity_chars"])
                    if len(scope) < config["scope_min_chars"]:
                        record["intent"] = f"{intent_name}_invalid"
                        record["reason"] = "missing_scope"
                    else:
                        record["scope"] = scope
                detected.append(record)
                # Only one match per phrase per intent — chaining the same
                # phrase twice in one prompt is operator nuance we'll
                # surface if it becomes a real use case.
                break

    detected.sort(key=lambda r: r["position"])
    return detected
