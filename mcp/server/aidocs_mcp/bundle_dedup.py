"""Cross-project bundle deduplication — Layer 4 cross-project index slice 2.

When `related_project_subsystem_bundle` or similar fan-out tools
aggregate context from N related projects, the same file/snippet
often appears multiple times (shared utilities, vendored deps,
upstream/downstream of the same module). Returning all copies burns
context without adding signal.

This module provides a content-addressed dedup helper: each entry
gets a stable SHA256 over the normalized content, and the first
occurrence wins while subsequent duplicates are reported in a
`duplicate_sources` list. Callers can choose to include the
duplicate paths (useful for "this function exists in 5 projects")
or drop them entirely.

Standalone helper — callers wire it into the fan-out surface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DedupEntry:
    """One deduplicated bundle entry.

    content_hash: SHA256 of the normalized payload. Stable identity
        across projects even when the display path differs.
    first_source: where this content was first encountered (project +
        path). Authoritative copy for downstream tools.
    duplicate_sources: everyone else who had the same content. Useful
        when the operator asks "which projects use this?"
    payload: the deduped content itself (kept from the first source).
    """

    content_hash: str
    first_source: str
    duplicate_sources: list[str] = field(default_factory=list)
    payload: Any = None


def _normalize(content: str) -> str:
    """Normalize for content-hashing purposes. Whitespace-trim + line-
    ending normalize; does NOT lowercase or strip comments because
    semantic equality isn't our call to make at this layer.
    """
    return "\n".join(
        line.rstrip() for line in str(content or "").replace("\r\n", "\n").split("\n")
    ).strip()


def _content_hash(content: str) -> str:
    """SHA256 in hex of the normalized content. Chosen over CRC/murmur
    because the dedup set can grow into tens of thousands of entries
    across 50+ projects — collision resistance matters more than speed.
    """
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()


def dedupe_bundle(
    entries: Iterable[dict[str, Any]],
    content_field: str = "content",
    source_field: str = "source",
) -> list[DedupEntry]:
    """Deduplicate a bundle by content hash.

    `entries` is any iterable of dicts with at minimum the two named
    fields. Order is preserved: the first occurrence of a given hash
    wins as `first_source`; later occurrences go into
    `duplicate_sources`.

    Entries missing the content field are skipped (they carry no
    signal worth deduplicating on).
    """
    by_hash: dict[str, DedupEntry] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get(content_field)
        if content is None:
            continue
        source = str(entry.get(source_field, ""))
        digest = _content_hash(str(content))
        existing = by_hash.get(digest)
        if existing is None:
            by_hash[digest] = DedupEntry(
                content_hash=digest,
                first_source=source,
                duplicate_sources=[],
                payload=content,
            )
        # Skip self-recording when source already registered — idempotent.
        elif (
            source and source != existing.first_source and source not in existing.duplicate_sources
        ):
            existing.duplicate_sources.append(source)
    # Preserve insertion order so the caller's "first wins" semantics
    # survive dict ordering surprises.
    return list(by_hash.values())


def dedup_stats(entries: list[DedupEntry]) -> dict[str, int]:
    """Roll-up for dashboards — how much bundle context the dedup saved.

    unique: number of distinct content hashes after dedup.
    total_duplicates: sum of duplicate_sources lengths.
    savings_ratio: 0.0-1.0 — fraction of the raw bundle that was
        duplicate. Zero means nothing deduplicated; 1.0 means every
        entry was a duplicate of the first.
    """
    unique = len(entries)
    total_duplicates = sum(len(e.duplicate_sources) for e in entries)
    total_original = unique + total_duplicates
    savings = (total_duplicates / total_original) if total_original else 0.0
    return {
        "unique": unique,
        "total_duplicates": total_duplicates,
        "total_original": total_original,
        "savings_ratio": round(savings, 3),
    }
