"""Substance extraction — the "what matters" projection of a Doc.

Empire doctrine 2026-05-12: don't analyze EVERYTHING — that's the LLM's
job. The NLP layer extracts substance:
  1. Verbs (primary signal — what action is requested)
  2. Their subjects (nsubj) + objects (obj/dobj) — the "who/what"
  3. Adverbs (modifiers, intensity, polarity)
  4. Negations (flip polarity — "don't protect" != "protect")
  5. Nouns / proper nouns (substantives) when no verbs are present
  6. Named entities (file paths, function names, etc.)

Consumers iterate Substance.verbs (small list, typically 1-10 tokens
for normal prompts), not Doc.tokens (often 50-500). Cuts both
processing time and consumer complexity.

A Substance is computed once per Doc and cached on the Doc itself
(Doc is frozen — caching uses a weak-key dict in this module).
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field

from .doc import Doc, Entity, Token


@dataclass(frozen=True, slots=True)
class Substance:
    """Light projection of a Doc — only what consumers need.

    Token.head_idx points into the ORIGINAL Doc.tokens. To traverse,
    keep the Doc reference around (Substance does NOT carry it back
    by value to stay frozen+hashable).
    """

    verbs: tuple[Token, ...]
    nouns: tuple[Token, ...]
    adverbs: tuple[Token, ...]
    negations: tuple[Token, ...]
    entities: tuple[Entity, ...]
    # verb_idx → ALL subject Tokens for that verb. spaCy can attach
    # multiple nsubj arcs to one verb (especially on slang/rage
    # phrasings where "the fuck" and "you" both point at the same
    # verb). Consumers walk all candidates.
    verb_subjects: dict[int, tuple[Token, ...]] = field(default_factory=dict)
    # verb_idx → tuple of object Tokens (dobj/obj/iobj/pobj)
    verb_objects: dict[int, tuple[Token, ...]] = field(default_factory=dict)


# UD POS tags (https://universaldependencies.org/u/pos/).
_VERB_POS = frozenset({"VERB", "AUX"})
_NOUN_POS = frozenset({"NOUN", "PROPN"})
_ADVERB_POS = frozenset({"ADV"})
# UD dep labels used to find subjects/objects.
_SUBJECT_DEPS = frozenset({"nsubj", "nsubj:pass", "csubj"})
_OBJECT_DEPS = frozenset({"obj", "dobj", "iobj", "pobj"})
_NEGATION_DEPS = frozenset({"neg", "advmod:neg"})

# Negation lemmas as a fallback when dep parse can't label them.
_NEGATION_LEMMAS = frozenset(
    {
        "not",
        "never",
        "no",
        "none",
        "non",
        "mai",
        "ne",
        "ningún",
        "ninguna",  # multilingual seed
        "nu",  # Romanian
    },
)


_SUBSTANCE_CACHE: weakref.WeakValueDictionary[int, Substance] = weakref.WeakValueDictionary()


def substance_of(doc: Doc) -> Substance:
    """Extract the substance projection of a Doc. Cached per-Doc via
    object id; cache evicts when the Doc is GC'd.

    Works in graceful degradation mode: if the Doc carries no POS
    capability (regex floor would, but we removed that), returns an
    empty Substance. If POS is present but dep is not, Substance has
    verbs/nouns/etc. but empty verb_subjects/verb_objects.
    """
    cached = _SUBSTANCE_CACHE.get(id(doc))
    if cached is not None:
        return cached

    if "pos" not in doc.capabilities:
        empty = Substance(verbs=(), nouns=(), adverbs=(), negations=(), entities=())
        return empty

    verbs: list[Token] = []
    nouns: list[Token] = []
    adverbs: list[Token] = []
    negations: list[Token] = []

    for tok in doc.tokens:
        if tok.pos in _VERB_POS:
            verbs.append(tok)
        elif tok.pos in _NOUN_POS:
            nouns.append(tok)
        elif tok.pos in _ADVERB_POS:
            adverbs.append(tok)
        if tok.dep in _NEGATION_DEPS or tok.lemma in _NEGATION_LEMMAS:
            negations.append(tok)

    verb_subjects_list: dict[int, list[Token]] = {}
    verb_objects: dict[int, list[Token]] = {}
    if "dep" in doc.capabilities:
        # Walk once: for each token, if its head is a verb, classify.
        # Build verb index map (token index → True for verbs).
        verb_idx_set = {i for i, t in enumerate(doc.tokens) if t.pos in _VERB_POS}
        for i, tok in enumerate(doc.tokens):
            head = tok.head_idx
            if head < 0 or head not in verb_idx_set:
                continue
            if tok.dep in _SUBJECT_DEPS:
                verb_subjects_list.setdefault(head, []).append(tok)
            elif tok.dep in _OBJECT_DEPS:
                verb_objects.setdefault(head, []).append(tok)

    substance = Substance(
        verbs=tuple(verbs),
        nouns=tuple(nouns),
        adverbs=tuple(adverbs),
        negations=tuple(negations),
        entities=doc.entities,
        verb_subjects={k: tuple(v) for k, v in verb_subjects_list.items()},
        verb_objects={k: tuple(v) for k, v in verb_objects.items()},
    )
    try:
        _SUBSTANCE_CACHE[id(doc)] = substance
    except TypeError:
        # Substance has dict fields → not weakly referenceable. The
        # cache miss is harmless; just skip caching for this Doc.
        pass
    return substance
