"""Analyzer-agnostic structured output.

Consumers code against these types, never against `spacy.tokens.*`.
When the backend changes (stanza, transformer, local LLM), consumers
keep working unchanged. The bridge from analyzer-native types to
these lives in each Pipeline implementation.

Every field is optional in the sense that a baseline pipeline (no
POS/dep model loaded) may emit Doc with tokens but empty noun_chunks,
empty entities, and Token.pos == "" / Token.dep == "". Consumers check
Doc.capabilities before assuming presence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Token:
    """One token in a Doc.

    text: surface form as it appeared.
    lemma: language-specific lemma; empty string if pipeline can't.
    pos: Universal POS tag (NOUN/VERB/ADJ/...); empty if not available.
    morph: morphological features ({"Mood":"Imp","Person":"2",...});
           empty mapping if not available.
    head_idx: token index in Doc.tokens of the syntactic head.
              Equals own index when this token IS the root.
              -1 when dep parse not available.
    dep: dependency relation label ("nsubj","obj",...); empty if N/A.
    is_alpha / is_upper: cheap character-class flags, always set.
    char_start / char_end: byte offsets in Doc.text.
    """

    text: str
    lemma: str
    pos: str
    morph: Mapping[str, str]
    head_idx: int
    dep: str
    is_alpha: bool
    is_upper: bool
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class Span:
    """Contiguous token range. Used for noun_chunks and sentences.

    start_idx / end_idx are token indices into Doc.tokens (end exclusive).
    text is the materialized substring from Doc.text.
    """

    start_idx: int
    end_idx: int
    text: str


@dataclass(frozen=True, slots=True)
class Entity:
    """Named entity span. label is the entity type (PERSON, ORG, LOC,
    FILE, FUNCTION, ...). Pipelines emit only what their model knows;
    custom labels can be injected by post-processors.
    """

    start_idx: int
    end_idx: int
    text: str
    label: str


@dataclass(frozen=True, slots=True)
class Doc:
    """Analyzed text.

    text: original input verbatim.
    language: ISO 639-1 code of the pipeline that produced this Doc.
    pipeline_name: identifying string like "spacy:en_core_web_sm" so
                   audit can trace which analyzer fired.
    capabilities: frozenset of what THIS Doc actually carries (a
                  baseline pipeline might emit {"tokens","lemmas"}
                  only; a full one emits {"tokens","lemmas","pos",
                  "dep","ner","morph"}). Consumers consult this
                  before reaching for advanced fields.
    tokens / noun_chunks / entities / sentences: structural data.
    """

    text: str
    language: str
    pipeline_name: str
    capabilities: frozenset[str]
    tokens: tuple[Token, ...] = field(default_factory=tuple)
    noun_chunks: tuple[Span, ...] = field(default_factory=tuple)
    entities: tuple[Entity, ...] = field(default_factory=tuple)
    sentences: tuple[Span, ...] = field(default_factory=tuple)

    def children_of(self, idx: int) -> tuple[Token, ...]:
        """All tokens whose head is at `idx`. Empty when no dep parse
        (capabilities lacks 'dep'). Useful for queries like
        'does this verb have a 2nd-person nsubj?'.
        """
        if "dep" not in self.capabilities:
            return ()
        return tuple(t for i, t in enumerate(self.tokens) if t.head_idx == idx and i != idx)

    def root_of(self, idx: int) -> Token:
        """Walk up head chain to the syntactic root. Returns the
        token itself when no dep parse is available.
        """
        if "dep" not in self.capabilities:
            return self.tokens[idx]
        seen: set[int] = set()
        current = idx
        while current not in seen:
            seen.add(current)
            head = self.tokens[current].head_idx
            if head == current or head < 0:
                return self.tokens[current]
            current = head
        return self.tokens[current]
