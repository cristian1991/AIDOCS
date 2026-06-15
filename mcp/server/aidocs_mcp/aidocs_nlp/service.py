"""NLPService — the one door.

Every NLP consumer in AIDOCS goes through this object. Pipeline
lifecycle, caching, language detection, telemetry are all wrapped
here. Consumers never import spacy / lingua / rapidfuzz directly.

Bootstrap pattern:
    service = NLPService(project_root, config)
    doc = service.analyze(prompt)              # auto-detects language
    doc = service.analyze(prompt, language="en")
    doc = service.analyze(thought,
                          source=AnalysisSource.AGENT_THOUGHT)

Lifecycle on MCP server start:
    service.preload(config.get("nlp.preloaded_languages", ["en"]))
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout
from enum import Enum
from pathlib import Path
from typing import Any

from .cache import DocCache, ModelCache
from .doc import Doc
from .installer import Installer, InstallProgress
from .language_detect import detect_language
from .language_registry import DEFAULT_PACKS, LanguagePack, PackStatus, pack_for
from .pipeline import Pipeline, SpacyPipeline
from .substance import Substance, substance_of
from .telemetry import Telemetry

# Length threshold that switches from short-prompt budget to long-
# prompt budget. 500 chars ≈ a normal multi-sentence prompt; above
# this we expect a multi-paragraph user message (king's eulogy case).
_LONG_TEXT_THRESHOLD_CHARS = 500


class NoPipelineAvailable(Exception):
    """Raised when analyze() can't satisfy a request: no pack loaded
    for the resolved language, or the analyze call exceeded the hard
    timeout. Consumers catch this and skip their work cleanly.

    King doctrine 2026-05-12: no silent degradation via a regex
    tokenizer floor. Either the NLP can do the job correctly or the
    consumer doesn't run.
    """


class AnalysisSource(str, Enum):
    """Where the text came from. Per-source cache TTLs + audit + the
    AGENT_THOUGHT policy gate all key off this enum.

    AGENT_THOUGHT slot is reserved (king + co-co doctrine 2026-05-12):
    the consumer infrastructure accepts it from day one, but the
    EMITTER side must clear a per-host policy check before calling
    analyze() with that source. Hidden chain-of-thought streams MUST
    NOT be scraped — only host-policy-allowed visible thought channels.
    """

    USER_PROMPT = "user_prompt"
    AGENT_THOUGHT = "agent_thought"
    MEMORY_CAPTURE = "memory_capture"
    SKILL_DEFINITION = "skill_definition"
    SEARCH_QUERY = "search_query"
    TOOL_INPUT = "tool_input"
    BACKGROUND_INDEX = "background_index"


# Per-source doc cache TTLs in seconds. 0 = no expiry (FIFO eviction
# still applies). USER_PROMPT consumers (DNT, skill_trigger) fire
# within one turn — short TTL is fine. MEMORY_CAPTURE is referenced
# indefinitely by memory-surface queries, so it lives until the
# memory entry is rewritten (effectively long TTL).
_DEFAULT_TTL_BY_SOURCE = {
    AnalysisSource.USER_PROMPT.value: 90,
    AnalysisSource.AGENT_THOUGHT.value: 60,
    AnalysisSource.MEMORY_CAPTURE.value: 3600,
    AnalysisSource.SKILL_DEFINITION.value: 3600,
    AnalysisSource.SEARCH_QUERY.value: 30,
    AnalysisSource.TOOL_INPUT.value: 30,
    AnalysisSource.BACKGROUND_INDEX.value: 1800,
}


class NLPService:
    """The canonical NLP entry point."""

    def __init__(
        self,
        project_root: Path,
        config: dict[str, Any] | None = None,
    ):
        self._project_root = project_root
        self._config = config or {}
        self._model_cache = ModelCache(
            max_bytes=int(self._config.get("nlp.max_resident_bytes", 0)),
        )
        self._doc_cache = DocCache(
            max_entries=int(self._config.get("nlp.doc_cache_size", 256)),
            ttl_by_source=_DEFAULT_TTL_BY_SOURCE,
            default_ttl_s=int(self._config.get("nlp.doc_cache_ttl_s", 60)),
        )
        # Tiered hard timeouts. Short prompts (<500 chars) get the
        # conversational budget; long texts get a wider window; the
        # max cap protects against caller-override abuse.
        # King doctrine 2026-05-13: short bumped from 50ms to 200ms.
        # 50ms was too tight for spaCy cold-start (model first-use
        # warmup) AND for slightly longer than expected prompts. 200ms
        # is still well under hook-time perceptual lag (~300ms).
        self._analyze_timeout_ms_short = float(
            self._config.get("nlp.analyze_timeout_ms_short", 200.0),
        )
        self._analyze_timeout_ms_long = float(
            self._config.get("nlp.analyze_timeout_ms_long", 500.0),
        )
        self._analyze_timeout_ms_max = float(
            self._config.get("nlp.analyze_timeout_ms_max", 1000.0),
        )
        self._telemetry = Telemetry(
            analyze_budget_ms=self._analyze_timeout_ms_short,
        )
        self._installer = Installer()
        # Dedicated executor for analyze() so a slow call doesn't
        # block the calling thread past the timeout. max_workers
        # matches typical hook-time concurrency; analyses are CPU-bound
        # but spaCy releases the GIL on heavy ops.
        self._executor = ThreadPoolExecutor(
            max_workers=int(self._config.get("nlp.analyze_workers", 2)),
            thread_name_prefix="nlp-analyze",
        )

    # ── Hot path ──

    def analyze(
        self,
        text: str,
        *,
        language: str | None = None,
        source: AnalysisSource | str = AnalysisSource.USER_PROMPT,
        timeout_ms: float | None = None,
    ) -> Doc | None:
        """Analyze text. Returns Doc on success, None on:
          - empty input
          - AGENT_THOUGHT source without operator opt-in
          - no pipeline loaded for the resolved language
          - hard timeout exceeded (nlp.analyze_timeout_ms, default 50ms)
          - pipeline.analyze() raised

        Consumers MUST handle None — there is no silent fallback. Skip
        the consumer's work and record a miss in telemetry.

        timeout_ms overrides nlp.analyze_timeout_ms for this call,
        capped at nlp.analyze_timeout_ms_max (default 1000ms). Use for
        offline batch analysis (memory_capture indexing) where 50ms
        isn't enough for long text.
        """
        if not text or not text.strip():
            return None
        source_str = source.value if isinstance(source, AnalysisSource) else str(source)
        if source_str == AnalysisSource.AGENT_THOUGHT.value:
            if not bool(self._config.get("nlp.allow_agent_thought", False)):
                self._telemetry.record_consumer("nlp.agent_thought_refused", True)
                return None
        chosen_lang, _ = self._resolve_language(text, language)
        cached = self._doc_cache.get(text, chosen_lang, source_str)
        if cached is not None:
            return cached
        pipeline = self._pipeline_for(chosen_lang)
        if pipeline is None:
            self._telemetry.record_consumer(
                f"nlp.no_pipeline.{chosen_lang}",
                True,
            )
            return None
        # Default budget tier picks itself from text length; caller
        # can override (capped at max). Long-eulogy case gets 200ms,
        # short prompt gets 50ms — measured by char count, cheap.
        if timeout_ms is not None:
            effective_timeout_ms = min(float(timeout_ms), self._analyze_timeout_ms_max)
        else:
            budget = (
                self._analyze_timeout_ms_long
                if len(text) >= _LONG_TEXT_THRESHOLD_CHARS
                else self._analyze_timeout_ms_short
            )
            effective_timeout_ms = min(budget, self._analyze_timeout_ms_max)
        start = time.perf_counter()
        future = self._executor.submit(pipeline.analyze, text)
        try:
            doc = future.result(timeout=effective_timeout_ms / 1000.0)
        except _FuturesTimeout:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._telemetry.record_analyze(source_str, chosen_lang, elapsed_ms)
            self._telemetry.record_consumer("nlp.timeout", True)
            return None
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._telemetry.record_analyze(source_str, chosen_lang, elapsed_ms)
            self._telemetry.record_consumer("nlp.pipeline_error", True)
            return None
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._telemetry.record_analyze(source_str, chosen_lang, elapsed_ms)
        self._doc_cache.put(doc, source_str)
        return doc

    def enrich_token(
        self,
        token: str,
        *,
        language: str | None = None,
    ) -> dict | None:
        """Return enrichment data for a single token: canonical POS +
        optional vector bytes. Used by the empire intent-tokens store
        to populate intent_lemma_sets.pos / .centroid_blob.

        Single-word POS is unreliable in isolation (spaCy defaults
        bare words to NOUN). To get a stable answer we probe the
        token in TWO contexts:
          * verb context — "please {token} the file."
          * noun context — "the {token} is here."

        Rules:
          - verb-context returns VERB → record VERB
          - else noun-context returns NOUN → record NOUN
          - else bare-token POS (whatever spaCy thinks)
        Multi-word phrases ("commit and push") keep the prior
        VERB-prefer-else-first-alpha heuristic on the bare phrase —
        wrapping multi-word in a context confuses the parse.

        Shape: `{"pos": "VERB", "vector_bytes": b"...", "lang": "en"}`
        or None when analysis isn't available.
        """
        cleaned = (token or "").strip()
        if not cleaned:
            return None
        chosen_lang, _ = self._resolve_language(cleaned, language)
        pipeline = self._pipeline_for(chosen_lang)
        if pipeline is None:
            return None

        def _doc_pos_at(text: str, target_word: str) -> str:
            """Analyze text, return POS of the target_word token."""
            try:
                d = pipeline.analyze(text)
            except Exception:
                return ""
            if d is None or not d.tokens:
                return ""
            tw = target_word.lower()
            for t in d.tokens:
                if (getattr(t, "text", "") or "").lower() == tw or (
                    getattr(t, "lemma", "") or ""
                ).lower() == tw:
                    return getattr(t, "pos", "") or ""
            return ""

        pos = ""
        is_phrase = " " in cleaned
        if is_phrase:
            # Multi-word: VERB-prefer-else-first-alpha on the bare phrase.
            try:
                doc = pipeline.analyze(cleaned)
            except Exception:
                doc = None
            if doc and doc.tokens:
                chosen = None
                for tok in doc.tokens:
                    if not getattr(tok, "is_alpha", False):
                        continue
                    if (getattr(tok, "pos", "") or "") == "VERB":
                        chosen = tok
                        break
                    if chosen is None:
                        chosen = tok
                if chosen is None and doc.tokens:
                    chosen = doc.tokens[0]
                pos = getattr(chosen, "pos", "") if chosen else ""
        else:
            # Single-word: probe verb-context first, then noun-context.
            verb_ctx = f"please {cleaned} the file."
            noun_ctx = f"the {cleaned} is here."
            verb_pos = _doc_pos_at(verb_ctx, cleaned)
            if verb_pos == "VERB":
                pos = "VERB"
            else:
                noun_pos = _doc_pos_at(noun_ctx, cleaned)
                if noun_pos == "NOUN":
                    pos = "NOUN"
                else:
                    pos = verb_pos or noun_pos or ""

        out: dict = {"pos": pos, "lang": chosen_lang}
        # Word vectors — only if the loaded model ships with them.
        try:
            doc_vec = pipeline.analyze(cleaned)
            vec = getattr(doc_vec, "vector", None) if doc_vec else None
            if vec is not None and hasattr(vec, "tobytes"):
                out["vector_bytes"] = vec.tobytes()
        except Exception:
            pass
        return out

    def analyze_substance(
        self,
        text: str,
        *,
        language: str | None = None,
        source: AnalysisSource | str = AnalysisSource.USER_PROMPT,
        timeout_ms: float | None = None,
    ) -> Substance | None:
        """Substance projection — what 95% of consumers actually need.

        Returns Substance with verbs / their subjects+objects /
        adverbs / negations / nouns / entities. None when analyze()
        returns None (no pack, timeout, error).

        Consumers iterate substance.verbs (typically 1-10 tokens),
        match against semantic_dict categories, look up subjects/
        objects when relevant. Way faster than walking full Doc.tokens.
        """
        doc = self.analyze(
            text,
            language=language,
            source=source,
            timeout_ms=timeout_ms,
        )
        if doc is None:
            return None
        return substance_of(doc)

    def detect_language(self, text: str) -> str:
        lang, _ = self._resolve_language(text, None)
        return lang

    def record_consumer(self, name: str, fired: bool) -> None:
        """Consumer telemetry hook — called by each consumer after it
        runs (fired=True iff the consumer produced a positive signal).
        """
        self._telemetry.record_consumer(name, fired)

    # ── Pipeline lifecycle ──

    def preload(self, languages: Iterable[str]) -> None:
        """Eager-load language packs. Called at MCP server start with
        config.get('nlp.preloaded_languages', ['en']).
        """
        for lang in languages:
            try:
                self._pipeline_for(lang)
            except Exception:
                # Pack might not be installed yet; lazy path retries.
                pass

    def is_loaded(self, language: str) -> bool:
        return self._model_cache.get(language) is not None

    def loaded_languages(self) -> tuple[str, ...]:
        return self._model_cache.languages()

    def unload(self, language: str) -> None:
        self._model_cache.remove(language)

    # ── Pack management ──

    def available_packs(self) -> tuple[dict[str, Any], ...]:
        """For the dashboard. Adds install/load status to each pack."""
        out: list[dict[str, Any]] = []
        for pack in DEFAULT_PACKS:
            status = self._pack_status(pack)
            out.append(
                {
                    "language": pack.language,
                    "display_name": pack.display_name,
                    "model_name": pack.model_name,
                    "size_mb": pack.size_mb,
                    "tier": pack.tier,
                    "status": status.value,
                    "loaded": self.is_loaded(pack.language),
                },
            )
        return tuple(out)

    def install_pack(self, language: str) -> InstallProgress:
        pack = pack_for(language)
        if pack is None:
            raise ValueError(f"Unknown language: {language}")
        extra = self._config.get("nlp.pack_allowlist_extra", []) or []
        return self._installer.start(pack, extra_allowlist=extra)

    def uninstall_pack(self, language: str) -> tuple[bool, str]:
        pack = pack_for(language)
        if pack is None:
            return (False, f"unknown language: {language}")
        self.unload(language)
        return self._installer.uninstall(pack)

    def install_status(self, install_id: str) -> InstallProgress | None:
        return self._installer.status(install_id)

    # ── Telemetry / snapshot ──

    def snapshot(self) -> dict[str, Any]:
        return {
            "loaded_languages": list(self.loaded_languages()),
            "model_cache": {
                "entries": self._model_cache.stats.current_entries,
                "bytes": self._model_cache.stats.current_bytes,
                "hits": self._model_cache.stats.hits,
                "misses": self._model_cache.stats.misses,
                "evictions": self._model_cache.stats.evictions,
            },
            "doc_cache": {
                "entries": self._doc_cache.stats.current_entries,
                "hits": self._doc_cache.stats.hits,
                "misses": self._doc_cache.stats.misses,
                "evictions": self._doc_cache.stats.evictions,
            },
            "telemetry": self._telemetry.snapshot(),
        }

    # ── Internals ──

    def _resolve_language(
        self,
        text: str,
        caller_override: str | None,
    ) -> tuple[str, str]:
        operator_pref = str(self._config.get("nlp.preferred_language", "auto"))
        project_pinned = str(self._config.get("nlp.project_language", ""))
        # Supported = anything in DEFAULT_PACKS, regardless of install
        # state. If detection lands on an uninstalled language we still
        # return it; the pipeline path falls through to floor.
        supported = tuple(p.language for p in DEFAULT_PACKS)
        return detect_language(
            text,
            caller_override=caller_override,
            operator_preferred=operator_pref,
            project_pinned=project_pinned,
            supported_languages=supported,
        )

    def _pipeline_for(self, language: str) -> Pipeline | None:
        """Return the loaded pipeline for `language`, or None if no
        pack is installed/loadable. Caller (analyze()) translates None
        into a clean skip — no floor fallback.
        """
        cached = self._model_cache.get(language)
        if cached is not None:
            return cached
        pack = pack_for(language)
        if pack is None:
            return None
        pipeline = SpacyPipeline(
            model_name=pack.model_name,
            language=pack.language,
            tier=pack.tier,
        )
        try:
            pipeline.load()
        except Exception:
            return None
        self._model_cache.put(language, pipeline)
        return pipeline

    def _pack_status(self, pack: LanguagePack) -> PackStatus:
        if self.is_loaded(pack.language):
            return PackStatus.LOADED
        # Check if the spaCy model is importable.
        try:
            import importlib.util

            if importlib.util.find_spec(pack.model_name) is not None:
                return PackStatus.INSTALLED
        except Exception:
            pass
        # Check if an install is in flight.
        for in_flight in self._installer.list_active():
            if in_flight.pack.model_name == pack.model_name:
                return PackStatus.DOWNLOADING
        return PackStatus.NOT_INSTALLED


# ── Singleton accessor ──

_SERVICE: NLPService | None = None


def get_service(
    project_root: Path,
    config: dict[str, Any] | None = None,
) -> NLPService:
    """Return the process-wide NLPService. Creates on first call.
    Pass the live config dict so settings changes propagate.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = NLPService(project_root, config)
    return _SERVICE


def reset_service() -> None:
    """Test/admin helper — drops the singleton so the next get_service
    rebuilds with current config.
    """
    global _SERVICE
    if _SERVICE is not None:
        for lang in list(_SERVICE.loaded_languages()):
            _SERVICE.unload(lang)
    _SERVICE = None
