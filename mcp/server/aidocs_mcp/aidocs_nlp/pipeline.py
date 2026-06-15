"""Pipeline protocol + concrete spaCy implementation.

Pipeline is the swappable analyzer. Today's implementation wraps
spaCy; future backends (stanza, transformers, local LLM) implement
the same protocol. Consumers never reach into the backend directly —
NLPService.analyze() returns a Doc and that's all they see.

A pipeline declares its `capabilities`: which Doc fields it can
populate. The baseline tokenizer-only pipeline (used as a floor for
languages without a loaded pack) declares {"tokens"} and that's it.
The full small-model spaCy pipelines declare {"tokens","lemmas","pos",
"dep","ner","morph"}.
"""

from __future__ import annotations

from typing import Protocol

from .doc import Doc, Entity, Span, Token


class Pipeline(Protocol):
    """The analyzer protocol. Implementations are stateful (a loaded
    spaCy model is heavy); NLPService manages lifecycle + caching.
    """

    @property
    def name(self) -> str: ...

    @property
    def language(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[str]: ...

    def analyze(self, text: str) -> Doc: ...

    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def memory_bytes(self) -> int:
        """Best-effort RSS attributable to this pipeline. 0 if unknown."""
        ...


# ── Concrete: spaCy ──


class SpacyPipeline:
    """spaCy-backed Pipeline. Lazy load — calling `.load()` actually
    imports spaCy and instantiates the model. Until then, holds only
    the model name.
    """

    _CAPABILITIES_BY_TIER = {
        # All spaCy core models ship POS, lemmas, dep, NER, morph.
        # Older "_news_" models occasionally drop NER; we declare it
        # and let analyze() emit empty entities if absent.
        "core": frozenset({"tokens", "lemmas", "pos", "dep", "ner", "morph"}),
        # xx_sent_ud_sm — multilingual sentence segmentation + UD POS.
        # No dep, no NER, no morph.
        "multilang": frozenset({"tokens", "lemmas", "pos"}),
        # Pure tokenizer fallback.
        "tokenizer": frozenset({"tokens"}),
    }

    def __init__(self, model_name: str, language: str, tier: str = "core"):
        self._model_name = model_name
        self._language = language
        self._tier = tier
        self._nlp = None  # lazy-loaded spacy.Language

    @property
    def name(self) -> str:
        return f"spacy:{self._model_name}"

    @property
    def language(self) -> str:
        return self._language

    @property
    def capabilities(self) -> frozenset[str]:
        return self._CAPABILITIES_BY_TIER.get(
            self._tier,
            self._CAPABILITIES_BY_TIER["core"],
        )

    def is_loaded(self) -> bool:
        return self._nlp is not None

    def load(self) -> None:
        if self._nlp is not None:
            return
        import spacy

        self._nlp = spacy.load(self._model_name)

    def unload(self) -> None:
        self._nlp = None

    def memory_bytes(self) -> int:
        if self._nlp is None:
            return 0
        # spaCy models don't expose a stable memory_bytes; report
        # vocab size as a proxy. Real telemetry uses psutil-based
        # per-process RSS deltas around load() in NLPService.
        try:
            return int(getattr(self._nlp.vocab, "length", 0)) * 64
        except Exception:
            return 0

    def analyze(self, text: str) -> Doc:
        if self._nlp is None:
            self.load()
        sd = self._nlp(text)

        tokens: list[Token] = []
        for tok in sd:
            morph: dict[str, str] = {}
            try:
                morph = {k: v[0] if v else "" for k, v in tok.morph.to_dict().items()}
            except Exception:
                morph = {}
            tokens.append(
                Token(
                    text=tok.text,
                    lemma=tok.lemma_ or tok.text.lower(),
                    pos=tok.pos_ or "",
                    morph=morph,
                    head_idx=tok.head.i if tok.head is not None else tok.i,
                    dep=tok.dep_ or "",
                    is_alpha=tok.is_alpha,
                    is_upper=tok.is_upper,
                    char_start=tok.idx,
                    char_end=tok.idx + len(tok.text),
                ),
            )

        noun_chunks: list[Span] = []
        try:
            for nc in sd.noun_chunks:
                noun_chunks.append(
                    Span(
                        start_idx=nc.start,
                        end_idx=nc.end,
                        text=nc.text,
                    ),
                )
        except (ValueError, NotImplementedError):
            # noun_chunks not supported by this model (some non-EN models).
            noun_chunks = []

        entities: list[Entity] = []
        for ent in sd.ents:
            entities.append(
                Entity(
                    start_idx=ent.start,
                    end_idx=ent.end,
                    text=ent.text,
                    label=ent.label_,
                ),
            )

        sentences: list[Span] = []
        try:
            for s in sd.sents:
                sentences.append(
                    Span(
                        start_idx=s.start,
                        end_idx=s.end,
                        text=s.text,
                    ),
                )
        except ValueError:
            sentences = []

        return Doc(
            text=text,
            language=self._language,
            pipeline_name=self.name,
            capabilities=self.capabilities,
            tokens=tuple(tokens),
            noun_chunks=tuple(noun_chunks),
            entities=tuple(entities),
            sentences=tuple(sentences),
        )


# Floor tokenizer removed 2026-05-12 (king directive). NLPService
# returns None when no pack is loaded; consumers handle that
# explicitly. No silent degradation.
