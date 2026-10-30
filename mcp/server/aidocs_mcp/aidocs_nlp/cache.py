"""Two-tier cache: model cache (LRU) + doc cache (per-source ring).

Model cache holds loaded Pipeline instances. LRU eviction kicks in
when `max_resident_bytes` is reached; default 0 (unbounded). Loaded
pipelines stay until explicitly unloaded or evicted under pressure.

Doc cache holds analyzed results keyed by (sha256(text), language,
source). Different sources have different TTLs because their access
patterns differ:
  - USER_PROMPT analysis is referenced by many consumers within one
    turn, then never again — short TTL is fine.
  - MEMORY_CAPTURE analysis is referenced for the lifetime of the
    memory entry (every memory-surface query against the same text).
    Effectively infinite TTL until the memory is rewritten.

Cache stats are exposed for the dashboard's NLP telemetry panel.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from .doc import Doc
from .pipeline import Pipeline


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    current_entries: int = 0
    current_bytes: int = 0


class ModelCache:
    """LRU of Pipeline instances keyed by language."""

    def __init__(self, max_bytes: int = 0):
        self._max_bytes = max_bytes  # 0 = unbounded
        self._loaded: OrderedDict[str, Pipeline] = OrderedDict()
        self.stats = CacheStats()

    def get(self, language: str) -> Pipeline | None:
        pipeline = self._loaded.get(language)
        if pipeline is None:
            self.stats.misses += 1
            return None
        # LRU touch.
        self._loaded.move_to_end(language)
        self.stats.hits += 1
        return pipeline

    def put(self, language: str, pipeline: Pipeline) -> None:
        self._loaded[language] = pipeline
        self._loaded.move_to_end(language)
        self._evict_if_over_budget()
        self._refresh_size()

    def remove(self, language: str) -> None:
        pipeline = self._loaded.pop(language, None)
        if pipeline is not None:
            pipeline.unload()
        self._refresh_size()

    def languages(self) -> tuple[str, ...]:
        return tuple(self._loaded.keys())

    def total_bytes(self) -> int:
        return sum(p.memory_bytes() for p in self._loaded.values())

    def _evict_if_over_budget(self) -> None:
        if self._max_bytes <= 0:
            return
        while self.total_bytes() > self._max_bytes and len(self._loaded) > 1:
            # Keep at least one (the most recently used) to avoid
            # thrashing if the budget is unrealistically small.
            oldest_lang, oldest_pipe = next(iter(self._loaded.items()))
            self._loaded.pop(oldest_lang, None)
            oldest_pipe.unload()
            self.stats.evictions += 1

    def _refresh_size(self) -> None:
        self.stats.current_entries = len(self._loaded)
        self.stats.current_bytes = self.total_bytes()


@dataclass
class _DocEntry:
    doc: Doc
    inserted_at: float


class DocCache:
    """FIFO ring keyed by (text-hash, language, source). Per-source TTL."""

    def __init__(
        self,
        max_entries: int = 256,
        ttl_by_source: dict[str, int] | None = None,
        default_ttl_s: int = 60,
    ):
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str, str], _DocEntry] = OrderedDict()
        self._ttl_by_source = ttl_by_source or {}
        self._default_ttl_s = default_ttl_s
        self.stats = CacheStats()

    @staticmethod
    def _key(text: str, language: str, source: str) -> tuple[str, str, str]:
        return (
            hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
            language,
            source,
        )

    def get(self, text: str, language: str, source: str) -> Doc | None:
        key = self._key(text, language, source)
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        ttl = self._ttl_by_source.get(source, self._default_ttl_s)
        if ttl > 0 and (time.time() - entry.inserted_at) > ttl:
            self._entries.pop(key, None)
            self.stats.misses += 1
            self.stats.evictions += 1
            self._refresh_size()
            return None
        # FIFO does NOT touch on read (a hot prompt shouldn't survive
        # forever past its TTL by being read repeatedly).
        self.stats.hits += 1
        return entry.doc

    def put(self, doc: Doc, source: str) -> None:
        key = self._key(doc.text, doc.language, source)
        self._entries[key] = _DocEntry(doc=doc, inserted_at=time.time())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1
        self._refresh_size()

    def clear(self) -> None:
        self._entries.clear()
        self._refresh_size()

    def _refresh_size(self) -> None:
        self.stats.current_entries = len(self._entries)
        # bytes estimate skipped — Doc is a frozen dataclass of tuples,
        # hard to size accurately; entries count is the useful signal.
