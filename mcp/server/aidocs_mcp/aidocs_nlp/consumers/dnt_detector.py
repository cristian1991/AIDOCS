"""DNT (Do Not Touch) intent consumer — spaCy-backed.

Replaces the lemma+regex+proximity matcher in canonical_intent_registry
for the protect/unprotect grant axes. Uses real dep parse + morphology:

  - Verb identification via POS tag, not surface matching. spaCy's
    lemmatizer handles inflection per loaded language model.
  - Negation detection via dep label (UD `neg` / `advmod:neg`) +
    multilingual lemma fallback. The legacy detector missed this
    entirely — "don't protect" was treated identical to "protect".
  - Imperative mood detection via morph feature (Mood=Imp). Filters
    habit/hypothetical phrasings ("I usually protect ...") more
    reliably than the legacy observational-prefix list.
  - Subject classification (2nd-person agent reference) for rage
    detection consistency.

Path extraction remains regex-based because spaCy's NER doesn't
recognize file paths as a category (en_core_web_sm tags `foo.py` as
NUM or NOUN depending on context). The legacy _PATH_TOKEN_RE +
comma-list continuation logic moves over unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..semantic_dict import DNT_GATE_PHRASES, SEMANTIC_CATEGORIES

if TYPE_CHECKING:
    from ..service import NLPService


# Path token + extractor now live in ONE neutral module shared with
# prompt_mutator's literal parser (no mirrored copy). Same pattern as before.
from .dnt_paths import PATH_TOKEN_RE as _PATH_TOKEN_RE  # noqa: F401 (back-compat ref)
from .dnt_paths import extract_dnt_paths as _extract_dnt_paths

# Lemma sets pulled from semantic_dict at import time (frozen). Keeping
# local references avoids dict lookups on every token check.
_PROTECT_VERBS = SEMANTIC_CATEGORIES["protect_verb"]
_UNPROTECT_VERBS = SEMANTIC_CATEGORIES["unprotect_verb"]
_NEGATION_LEMMAS = SEMANTIC_CATEGORIES["negation"]


def detect_protect_grants(prompt: str, service: NLPService) -> set[str] | None:
    """Return granted paths for ai_protect. Set contains explicit
    paths when present, or "*" wildcard when verb intent fires without
    a path (Empire doctrine 2026-05-12: agent picks the file).
    Returns None when NLP isn't available.
    """
    substance = service.analyze_substance(prompt)
    if substance is None:
        return None
    prompt_lower = prompt.lower()
    # Gate-phrase check first: "do not touch" is a fixed idiom whose
    # internal "not" is NOT a negation modifier.
    gate_phrase_present = any(phrase in prompt_lower for phrase in DNT_GATE_PHRASES)
    # Verb-lemma scan covers substance.verbs AND all token lemmas —
    # spaCy occasionally tags rare verbs (unprotect, etc.) as NOUN
    # but the lemma is still correct.
    all_lemmas = [t.lemma.lower() for t in _all_tokens(substance)]
    protect_verb_present = any(l in _PROTECT_VERBS for l in all_lemmas)
    if not protect_verb_present and not gate_phrase_present:
        return set()
    # Negation suppression: only when no gate phrase is present
    # (so "not" can't be part of the idiom), AND the negation is
    # within 30 chars of a protect-verb (so unrelated negations
    # elsewhere in the prompt don't kill the grant).
    if not gate_phrase_present and substance.negations:
        for neg in substance.negations:
            for v in substance.verbs:
                if v.lemma.lower() not in _PROTECT_VERBS:
                    continue
                if abs(neg.char_start - v.char_start) <= 30:
                    return set()
    paths: set[str] = _extract_dnt_paths(prompt)
    if paths:
        return paths
    return {"*"}


def detect_unprotect_grants(prompt: str, service: NLPService) -> set[str] | None:
    """Symmetric to detect_protect_grants for unprotect intent."""
    substance = service.analyze_substance(prompt)
    if substance is None:
        return None

    all_lemmas = [t.lemma.lower() for t in _all_tokens(substance)]
    unprotect_verb_present = any(l in _UNPROTECT_VERBS for l in all_lemmas)
    if not unprotect_verb_present:
        return set()

    # Same negation rule as protect side.
    if substance.negations:
        for neg in substance.negations:
            for v in substance.verbs:
                if v.lemma.lower() not in _UNPROTECT_VERBS:
                    continue
                if abs(neg.char_start - v.char_start) <= 30:
                    return set()

    paths: set[str] = _extract_dnt_paths(prompt)

    if paths:
        return paths
    return {"*"}


def detect_edit_complaint(
    prompt: str,
    service: NLPService,
) -> bool | None:
    """Detect 2nd-person edit-complaint pattern via dep parse.

    Examples that fire:
      - "Why did you edit that?"           (edit verb, you=nsubj)
      - "Who told you to modify foo.py?"   (modify verb, you=iobj)
      - "Did I tell you to change it?"     (change verb, you=nsubj of clause)

    Returns True/False on success, None when NLP unavailable.
    """
    substance = service.analyze_substance(prompt)
    if substance is None:
        return None
    if "dep" not in substance.entities and not substance.verbs:
        # Pipeline without dep parse — can't compute.
        return False

    edit_verbs = SEMANTIC_CATEGORIES["edit_verb"]
    second_person = SEMANTIC_CATEGORIES["second_person"]
    destroy_verbs = SEMANTIC_CATEGORIES["destroy_verb"]

    for verb in substance.verbs:
        lemma_l = verb.lemma.lower()
        if lemma_l not in edit_verbs and lemma_l not in destroy_verbs:
            continue
        # Look up subject of this verb in substance.verb_subjects.
        # verb's index in Doc.tokens is needed; we don't have it on
        # Token (frozen dataclass). Substance was built with verb_subjects
        # keyed by index — re-derive by linear scan of substance.verbs
        # against the underlying Doc.tokens position.
        # Cheaper: scan the entire substance.verb_subjects dict and
        # match the verb Token to its idx-bearing key.
        for verb_idx, subjects in substance.verb_subjects.items():
            for subj in subjects:
                if subj.lemma.lower() in second_person:
                    if (
                        subj.head_idx >= 0
                        and verb_idx == subj.head_idx
                        and abs(subj.char_start - verb.char_start) < 200
                    ):
                        return True
    return False


def _all_tokens(substance):
    """Iterate every distinct Token referenced by Substance. Used
    when scanning lemmas across POS classes (spaCy mis-tag workaround).
    """
    yielded: set[tuple[int, int]] = set()
    for collection in (
        substance.verbs,
        substance.nouns,
        substance.adverbs,
        substance.negations,
    ):
        for t in collection:
            key = (t.char_start, t.char_end)
            if key in yielded:
                continue
            yielded.add(key)
            yield t
