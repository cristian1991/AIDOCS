"""RFC-4 Phase A — anchor extraction from drawer content.

When a conversation drawer is being written, scan its content for code
symbol references (CamelCase, qualified names, file paths). For each
detected reference, ask a UnitResolver to map the reference to a
unit_id. High-confidence matches register as anchors via
``hub_ctx.register_anchor``; low-confidence matches go to a pending
candidates list for operator review.

The UnitResolver interface decouples extraction from AIDOCS. In
production the resolver wraps ``CodeUnitVendor`` (aidocs_integration);
in tests a MockUnitResolver returns canned unit_ids for fixture
symbols. This makes Phase A fully testable without AIDOCS installed.

Per RFC-4 §3.5 confidence tiers, the extractor assigns:
  - ``exact_symbol`` — qualified symbol that resolved unambiguously
  - ``file_anchor`` — file path that resolved but no symbol disambiguation
  - ``semantic_guess`` — bare CamelCase token; the resolver returned
                         best-match below the high-confidence threshold

The extractor never assigns ``operator_pinned``. That tier is reserved
for explicit operator commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------


# Qualified symbol: Container.Symbol or Module.Class.method
# Requires at least one dot; both sides CamelCase or method-shaped.
_QUALIFIED_SYMBOL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]+)(?:\.([A-Za-z_][A-Za-z0-9_]+))+\b"
)

# Bare CamelCase: PdfRenderer, DocumentService, GenerateDocument
# Heuristic — must start uppercase, contain at least one internal capital,
# length >= 5. This filters "The", "But", "And", etc.
_BARE_CAMELCASE_RE = re.compile(
    r"\b([A-Z][a-z]+[A-Z][A-Za-z0-9_]{2,})\b"
)

# File path: src/foo.cs, src\foo.cs, ./foo.py, mempalace/dialect.py
# Must contain a path separator AND a recognizable extension.
_FILE_PATH_RE = re.compile(
    r"\b((?:[\w.-]+[/\\])+[\w.-]+\.(?:py|ts|tsx|js|jsx|cs|cshtml|md|json|yml|yaml|toml|sql|rs|go|java|rb|php|cpp|h|hpp|c))\b"
)


# Common English words that look CamelCase but aren't symbols.
# Conservative list — false negatives on real symbols are cheaper than
# false positives that pollute the anchor table.
_CAMELCASE_FALSE_POSITIVES = frozenset({
    "TheBook", "TheUser", "TheCode", "AnyBody", "NoBody", "NoOne",
    "AnyOne", "NotEnough", "MoreThan", "EveryDay", "HighWay",
    "OutBound", "InBound", "ReadMe", "TodoList",
})


# ---------------------------------------------------------------------------
# UnitResolver protocol
# ---------------------------------------------------------------------------


class UnitResolver(Protocol):
    """Maps a code reference (symbol or path) to a unit_id.

    Production implementation wraps CodeUnitVendor from
    aidocs_integration. Tests use MockUnitResolver.
    """

    def resolve_symbol(
        self,
        symbol: str,
        *,
        container: Optional[str] = None,
    ) -> "ResolvedSymbol | None":
        """Resolve a symbol name to (unit_id, file_path, confidence)."""
        ...

    def resolve_path(self, path: str) -> "ResolvedPath | None":
        """Resolve a file path to (unit_id, ...) for the file's
        fallback unit (the file-level Unit, not any specific symbol)."""
        ...


@dataclass(frozen=True)
class ResolvedSymbol:
    """Returned by UnitResolver.resolve_symbol on a hit."""
    unit_id: str
    symbol_name: str
    file_path: str
    confidence_score: float  # 0.0..1.0


@dataclass(frozen=True)
class ResolvedPath:
    """Returned by UnitResolver.resolve_path on a hit."""
    unit_id: str
    file_path: str


# ---------------------------------------------------------------------------
# Mock resolver — for tests
# ---------------------------------------------------------------------------


class MockUnitResolver:
    """Deterministic resolver for tests. Maps a fixed dict of
    (symbol, container) → ResolvedSymbol and path → ResolvedPath.

    Production code uses the production resolver (wrapping
    CodeUnitVendor); this mock is for test isolation.
    """

    def __init__(
        self,
        *,
        symbols: Optional[dict[tuple[str, Optional[str]], ResolvedSymbol]] = None,
        paths: Optional[dict[str, ResolvedPath]] = None,
    ):
        self._symbols = dict(symbols or {})
        self._paths = dict(paths or {})

    def resolve_symbol(
        self,
        symbol: str,
        *,
        container: Optional[str] = None,
    ) -> Optional[ResolvedSymbol]:
        # First try exact (symbol, container) match
        if container is not None:
            hit = self._symbols.get((symbol, container))
            if hit is not None:
                return hit
        # Fall back to (symbol, None) — bare match
        return self._symbols.get((symbol, None))

    def resolve_path(self, path: str) -> Optional[ResolvedPath]:
        # Normalize to forward slashes
        norm = path.replace("\\", "/")
        return self._paths.get(norm)


# ---------------------------------------------------------------------------
# Extracted anchor candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorCandidate:
    """One candidate extracted from drawer content.

    Confidence (RFC-4 §3.5) is assigned by ``classify_candidate``
    based on:
      - exact_symbol: qualified name resolved with score >= 0.9
      - file_anchor: file path resolved
      - semantic_guess: bare CamelCase OR low-confidence symbol match
    """
    raw_match: str            # text that triggered the candidate
    unit_id: str
    symbol_name: str
    file_path: str
    confidence: str           # exact_symbol | file_anchor | semantic_guess
    confidence_score: float   # 0.0..1.0; informational
    char_offset: int          # where in the drawer content it matched


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------


# Threshold above which a symbol match is considered exact rather than guess.
HIGH_CONFIDENCE_THRESHOLD = 0.85


def extract_anchors(
    content: str,
    resolver: UnitResolver,
    *,
    high_confidence_only: bool = False,
) -> list[AnchorCandidate]:
    """Scan ``content`` for code references; resolve each via ``resolver``;
    return AnchorCandidates for every hit.

    Args:
        content: drawer content text.
        resolver: UnitResolver implementation. MockUnitResolver for
            tests; production resolver for real ingest.
        high_confidence_only: if True, return only candidates with
            confidence in {exact_symbol, file_anchor}. Filters out
            semantic_guess candidates.

    Returns:
        List of AnchorCandidate, deduplicated by (unit_id) — if
        multiple text matches resolve to the same unit, only the
        first match is kept (highest precedence).
    """
    if not content:
        return []

    candidates: list[AnchorCandidate] = []
    seen_unit_ids: set[str] = set()

    # Pass 1: qualified symbols (highest precedence — most unambiguous)
    for match in _QUALIFIED_SYMBOL_RE.finditer(content):
        full_text = match.group(0)
        parts = full_text.split(".")
        symbol = parts[-1]
        container = ".".join(parts[:-1])

        resolved = resolver.resolve_symbol(symbol, container=container)
        if resolved is None:
            # Try resolving without container
            resolved = resolver.resolve_symbol(symbol, container=None)
        if resolved is None:
            continue
        if resolved.unit_id in seen_unit_ids:
            continue

        score = resolved.confidence_score
        confidence = "exact_symbol" if score >= HIGH_CONFIDENCE_THRESHOLD else "semantic_guess"
        if high_confidence_only and confidence == "semantic_guess":
            continue

        candidates.append(AnchorCandidate(
            raw_match=full_text,
            unit_id=resolved.unit_id,
            symbol_name=resolved.symbol_name,
            file_path=resolved.file_path,
            confidence=confidence,
            confidence_score=score,
            char_offset=match.start(),
        ))
        seen_unit_ids.add(resolved.unit_id)

    # Pass 2: file paths
    for match in _FILE_PATH_RE.finditer(content):
        path = match.group(1)
        resolved = resolver.resolve_path(path)
        if resolved is None:
            continue
        if resolved.unit_id in seen_unit_ids:
            continue

        candidates.append(AnchorCandidate(
            raw_match=path,
            unit_id=resolved.unit_id,
            symbol_name="<file>",
            file_path=resolved.file_path,
            confidence="file_anchor",
            confidence_score=1.0,
            char_offset=match.start(),
        ))
        seen_unit_ids.add(resolved.unit_id)

    # Pass 3: bare CamelCase (semantic_guess only — lowest precedence)
    for match in _BARE_CAMELCASE_RE.finditer(content):
        token = match.group(1)
        if token in _CAMELCASE_FALSE_POSITIVES:
            continue

        resolved = resolver.resolve_symbol(token, container=None)
        if resolved is None:
            continue
        if resolved.unit_id in seen_unit_ids:
            continue

        score = resolved.confidence_score
        # Bare CamelCase is inherently lower confidence — clamp to semantic_guess
        # UNLESS the resolver scored it above the high threshold.
        confidence = (
            "exact_symbol"
            if score >= HIGH_CONFIDENCE_THRESHOLD
            else "semantic_guess"
        )
        if high_confidence_only and confidence == "semantic_guess":
            continue

        candidates.append(AnchorCandidate(
            raw_match=token,
            unit_id=resolved.unit_id,
            symbol_name=resolved.symbol_name,
            file_path=resolved.file_path,
            confidence=confidence,
            confidence_score=score,
            char_offset=match.start(),
        ))
        seen_unit_ids.add(resolved.unit_id)

    return candidates


def register_extracted_anchors(
    candidates: Iterable[AnchorCandidate],
    drawer_id: str,
    content_hash: str,
    anchor_writer,
    *,
    source: str = "palace_extractor",
) -> list[int]:
    """Persist anchor candidates via the AIDOCS anchor_writer.

    Returns list of anchor_ids that were successfully registered.
    Candidates whose registration raises are skipped (logged) — a
    bad anchor write should not block the drawer write.
    """
    import logging
    logger = logging.getLogger(__name__)

    anchor_ids: list[int] = []
    for c in candidates:
        try:
            anchor_id = anchor_writer(
                symbol_name=c.symbol_name,
                file_path=c.file_path,
                drawer_id=drawer_id,
                content_hash=content_hash,
                confidence=c.confidence,
                source=source,
            )
            anchor_ids.append(int(anchor_id))
        except Exception:
            logger.exception(
                "anchor register failed for unit_id=%s drawer_id=%s",
                c.unit_id, drawer_id,
            )
            continue
    return anchor_ids
