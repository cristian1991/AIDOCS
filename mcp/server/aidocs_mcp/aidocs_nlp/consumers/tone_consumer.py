"""Tone / anger / profanity consumer — NLP-backed.

Replaces the regex-based tone_detector module shipped today. Same
public surface (detect_tone_score, detect_protect_rage) so callers
swap import paths only.

Why NLP-backed: spaCy lemmatizes "fucking" → "fuck", "shittiest" →
"shitty", "stupido" → "stupido" per loaded language model. Regex
word-boundary matching missed these inflected forms unless we
enumerated every variant per language. The lemma lookup makes the
profanity category language-aware without per-language regex tables.

Structural signals (CAPS burst, repeated punctuation) stay
regex/char-level — they are language-agnostic by construction and
don't need a parser.

Edit-complaint detection delegates to dnt_detector.detect_edit_complaint
(dep-parse aware). When NLP is unavailable, falls back to lexical
phrase scanning so calm operators get the rage signal even before
a language pack is installed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..semantic_dict import (
    EDIT_COMPLAINT_PHRASES,
    SEMANTIC_CATEGORIES,
)

if TYPE_CHECKING:
    from ..service import NLPService


_REPEATED_PUNCT_RE = re.compile(r"[!?]{2,}")

_PROFANITY_LEMMAS = SEMANTIC_CATEGORIES["profanity"]
_ANGER_MARKERS = SEMANTIC_CATEGORIES["anger_marker"]


def _caps_burst_ratio(text: str) -> float:
    """Fraction of alphabetic chars that are uppercase. Short text
    (<8 alpha chars) returns 0.0 so "NO" alone does not max the score.
    """
    alpha = [c for c in text if c.isalpha()]
    if len(alpha) < 8:
        return 0.0
    upper = sum(1 for c in alpha if c.isupper())
    return upper / len(alpha)


def _repeated_punct_count(text: str) -> int:
    return len(_REPEATED_PUNCT_RE.findall(text))


def _profanity_count(prompt: str, service: NLPService) -> int:
    """Count distinct profanity lemmas in the prompt. Uses NLP for
    lemmatization when available; falls back to word-boundary regex
    on the curated lemma set otherwise (no pack loaded case).

    Returns 0 if neither path can produce a count — never raises.
    """
    substance = service.analyze_substance(prompt)
    if substance is not None:
        # spaCy gives us lemmas across all tokens regardless of POS.
        # Walking all categories of Substance covers what we need.
        seen: set[str] = set()
        for collection in (
            substance.verbs,
            substance.nouns,
            substance.adverbs,
            substance.negations,
        ):
            for tok in collection:
                lemma = tok.lemma.lower()
                if lemma in _PROFANITY_LEMMAS:
                    seen.add(lemma)
        return len(seen)
    # NLP unavailable — fall back to word-boundary regex on the same
    # curated lemma set. Less accurate (won't catch inflections) but
    # ships a usable rage detector before any pack is downloaded.
    text_lower = prompt.lower()
    count = 0
    for stem in _PROFANITY_LEMMAS:
        if re.search(r"\b" + re.escape(stem) + r"\b", text_lower):
            count += 1
    return count


def _has_edit_complaint(prompt: str, service: NLPService) -> bool:
    """True if either the dep-parse-aware detector OR the phrase
    substring scan fires. Union not fallback — the NLP detector may
    legitimately return False on multilingual text (Romanian prompt
    parsed by an en model won't recognize "editat" as edit_verb)
    while the multilingual phrase scan catches it. Both signals
    matter; either is enough.
    """
    from .dnt_detector import detect_edit_complaint as _nlp_complaint

    nlp_result = _nlp_complaint(prompt, service)
    text_lower = prompt.lower()
    phrase_match = any(p in text_lower for p in EDIT_COMPLAINT_PHRASES)
    return bool(nlp_result) or phrase_match


def detect_tone_score(prompt: str, service: NLPService) -> float:
    """0.0..1.0 anger/intensity score.

    Combines language-agnostic structural signals (CAPS burst, repeated
    punctuation) with lemma-aware lexical signal (profanity count via
    spaCy). A single mild expletive scores low; CAPS + multi-hit
    profanity + repeated punctuation scores high.

    Weights:
      - CAPS burst ratio: 0.4
      - Profanity hits (clipped 3): 0.4
      - Repeated punctuation (clipped 3): 0.2
    """
    if not prompt or not prompt.strip():
        return 0.0
    caps = _caps_burst_ratio(prompt)
    prof = min(_profanity_count(prompt, service), 3) / 3.0
    rep = min(_repeated_punct_count(prompt), 3) / 3.0
    return min(1.0, 0.4 * caps + 0.4 * prof + 0.2 * rep)


def detect_protect_rage(
    prompt: str,
    service: NLPService,
    *,
    tone_threshold: float = 0.4,
) -> bool:
    """True iff the prompt is an angry edit-complaint that should
    grant blanket protect authority to the agent.

    Two paths:
    1. tone_score >= 0.7 — pure rage, no specific phrase required.
    2. tone_score >= threshold AND edit-complaint signal — dep-parse-
       aware 2nd-person edit verb detection via dnt_detector, with
       phrase-scan fallback when no pack is loaded.
    """
    if not prompt or not prompt.strip():
        return False
    score = detect_tone_score(prompt, service)
    if score >= 0.7:
        return True
    if score >= tone_threshold and _has_edit_complaint(prompt, service):
        return True
    return False


def evidence(prompt: str, service: NLPService) -> dict:
    """Diagnostic breakdown for audit / dashboard."""
    return {
        "caps_ratio": round(_caps_burst_ratio(prompt), 3),
        "profanity_count": _profanity_count(prompt, service),
        "repeated_punct": _repeated_punct_count(prompt),
        "edit_complaint": _has_edit_complaint(prompt, service),
        "tone_score": round(detect_tone_score(prompt, service), 3),
        "protect_rage": detect_protect_rage(prompt, service),
    }
