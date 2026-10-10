"""RFC-4 — deterministic code-first recall intent resolver.

Given a raw query and a project's code-index-derived vocabulary, build
a structured ``RecallIntent`` used to constrain palace.search BEFORE
embeddings are consulted.

Design constraints (per architecture discussion):
  - No LLM. Heuristic-only. Latency budget <5ms per resolution.
  - Vocabulary comes from code-index paths + symbols, not a manual
    glossary. Code structure is authoritative.
  - ``forbidden_or_conflicting_artifacts`` is a DOWNRANK signal, NOT
    a hard filter. Under-recall is the worse failure mode.

Usage:
    vocab = build_vocabulary(paths=[...], symbols=[...])
    intent = resolve_recall_intent(query, vocabulary=vocab)
    # ... use intent.domain_terms / candidate_paths to constrain
    # palace.search, then post-process semantic hits via
    # downrank_by_intent(hits, intent).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Operations the agent typically asks about. Kept tiny + closed-set;
# anything else falls through to artifact/domain classification.
OPERATION_VERBS = frozenset(
    {
        "fix",
        "add",
        "remove",
        "delete",
        "refactor",
        "rename",
        "move",
        "render",
        "save",
        "load",
        "parse",
        "validate",
        "format",
        "build",
        "generate",
        "export",
        "import",
        "open",
        "close",
        "create",
        "update",
        "patch",
        "debug",
        "log",
        "trace",
        "test",
    },
)

# Query-side stopwords. Conservative — we only strip the obvious ones
# so that domain words like "for" or "the" don't pollute term sets.
QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "to",
        "in",
        "on",
        "with",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "please",
        "can",
        "you",
        "we",
        "i",
    },
)

# Project-tree dirs that are not source-of-truth and must not seed the
# domain vocabulary.
_DIR_BLOCKLIST = frozenset(
    {
        ".git",
        ".github",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "dist",
        "build",
        "out",
        "target",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "coverage",
        "site-packages",
        "tests",
        "test",
        "docs",
        "doc",
    },
)

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    """Code-index-derived vocabulary. Built once per recall call."""

    domain_terms: frozenset  # top-level dir names (e.g. "documents", "forms")
    artifact_terms: frozenset  # split symbol fragments (e.g. "renderer", "pdf")
    # Reverse index: artifact_term -> set of domains that contain it.
    # Drives forbidden_or_conflicting_artifacts.
    artifact_to_domains: dict  # str -> frozenset[str]
    # Map domain -> sample paths that live under it (for candidate_paths).
    domain_to_paths: dict  # str -> tuple[str, ...]
    # Map (domain, artifact) -> sample symbols.
    domain_artifact_to_symbols: dict  # tuple[str, str] -> tuple[str, ...]


@dataclass(frozen=True)
class RecallIntent:
    """Structured query intent. All fields are normalized to lowercase."""

    raw_query: str
    action: str | None = None
    domain_terms: tuple = ()
    operation_terms: tuple = ()
    target_artifact_terms: tuple = ()
    forbidden_or_conflicting_artifacts: tuple = ()  # domain names
    candidate_paths: tuple = ()
    candidate_symbols: tuple = ()
    # Confidence is "high" when at least one domain AND one artifact
    # term resolve against vocabulary. Used by callers to decide
    # whether to constrain palace.search vs. let it run broad.
    confidence: str = "low"  # "low" | "medium" | "high"


# ---------------------------------------------------------------------------
# Tokenization helpers
# ---------------------------------------------------------------------------


def _tokenize(query: str) -> list[str]:
    """Lowercase, split on non-identifier chars, drop stopwords."""
    if not query:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(query):
        tok = m.group(0).lower()
        if tok in QUERY_STOPWORDS:
            continue
        out.append(tok)
    return out


def _split_camel(symbol: str) -> list[str]:
    """Split CamelCase / snake_case / Pascal_Snake into lowercase parts."""
    if not symbol:
        return []
    # Replace separators with spaces, then run camel regex.
    flat = symbol.replace(".", " ").replace("_", " ").replace("-", " ")
    parts: list[str] = []
    for chunk in flat.split():
        for m in _CAMEL_RE.finditer(chunk):
            p = m.group(0).lower()
            if p and len(p) >= 2:
                parts.append(p)
    return parts


def _top_dir(path: str) -> str:
    """Return the top-level directory name from a project-relative path."""
    if not path:
        return ""
    p = path.replace("\\", "/").lstrip("/")
    # Strip a leading "src/" or "lib/" wrapper — those are scaffolding,
    # not domains. The real domain lives one level deeper.
    head, _, rest = p.partition("/")
    if head in {"src", "lib", "app", "pkg"} and rest:
        head2, _, _ = rest.partition("/")
        return head2.lower()
    return head.lower()


# ---------------------------------------------------------------------------
# Vocabulary builder
# ---------------------------------------------------------------------------


def build_vocabulary(
    *,
    paths: Iterable[str] = (),
    symbols: Iterable[tuple[str, str]] = (),  # (symbol_name, source_file)
) -> Vocabulary:
    """Build vocabulary from indexed paths + symbols.

    Args:
        paths: iterable of project-relative file paths
        symbols: iterable of (symbol_name, source_file) pairs from the
            code index. ``source_file`` is used to attribute the symbol
            to a domain.

    """
    domain_to_paths: dict[str, list[str]] = {}
    for p in paths:
        d = _top_dir(p)
        if not d or d in _DIR_BLOCKLIST or d.startswith("."):
            continue
        domain_to_paths.setdefault(d, []).append(p)

    artifact_to_domains: dict[str, set[str]] = {}
    domain_artifact_to_symbols: dict[tuple[str, str], list[str]] = {}
    all_artifacts: set[str] = set()

    for symbol_name, source_file in symbols:
        d = _top_dir(source_file)
        if not d or d in _DIR_BLOCKLIST or d.startswith("."):
            continue
        # Ensure domain seen even if path-list didn't include it.
        domain_to_paths.setdefault(d, []).append(source_file)
        for piece in _split_camel(symbol_name):
            if piece in QUERY_STOPWORDS or piece in OPERATION_VERBS:
                # Verbs aren't artifacts. Skip.
                continue
            all_artifacts.add(piece)
            artifact_to_domains.setdefault(piece, set()).add(d)
            domain_artifact_to_symbols.setdefault((d, piece), []).append(symbol_name)

    return Vocabulary(
        domain_terms=frozenset(domain_to_paths.keys()),
        artifact_terms=frozenset(all_artifacts),
        artifact_to_domains={k: frozenset(v) for k, v in artifact_to_domains.items()},
        domain_to_paths={k: tuple(dict.fromkeys(v))[:20] for k, v in domain_to_paths.items()},
        domain_artifact_to_symbols={
            k: tuple(dict.fromkeys(v))[:10] for k, v in domain_artifact_to_symbols.items()
        },
    )


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_recall_intent(
    query: str,
    *,
    vocabulary: Vocabulary | None = None,
) -> RecallIntent:
    """Resolve a raw query against the code vocabulary.

    Falls back to a low-confidence intent (operation_terms only) when
    vocabulary is absent or unhelpful.
    """
    tokens = _tokenize(query)
    if not tokens:
        return RecallIntent(raw_query=query)

    action: str | None = None
    op_terms: list[str] = []
    domain_terms: list[str] = []
    artifact_terms: list[str] = []

    vocab = vocabulary
    domain_set = vocab.domain_terms if vocab else frozenset()
    artifact_set = vocab.artifact_terms if vocab else frozenset()

    for tok in tokens:
        if tok in OPERATION_VERBS:
            if action is None:
                action = tok
            op_terms.append(tok)
        elif tok in domain_set:
            domain_terms.append(tok)
        elif tok in artifact_set or (len(tok) >= 3 and not tok.isdigit()):
            artifact_terms.append(tok)

    # De-dup while preserving order.
    op_terms = list(dict.fromkeys(op_terms))
    domain_terms = list(dict.fromkeys(domain_terms))
    artifact_terms = list(dict.fromkeys(artifact_terms))

    # Forbidden-or-conflicting: sibling domains that ALSO contain the
    # artifact terms the user named, minus the domains the user named.
    # This is the documents-vs-forms case: "pdf" appears in both, but
    # the user asked about "documents", so "forms" is conflicting.
    forbidden: list[str] = []
    if vocab and domain_terms and artifact_terms:
        user_domains = set(domain_terms)
        for art in artifact_terms:
            for d in vocab.artifact_to_domains.get(art, frozenset()):
                if d not in user_domains:
                    forbidden.append(d)
    forbidden = list(dict.fromkeys(forbidden))

    # Candidate paths/symbols: the chosen domain × artifact intersection.
    candidate_paths: list[str] = []
    candidate_symbols: list[str] = []
    if vocab and domain_terms:
        for d in domain_terms:
            candidate_paths.extend(vocab.domain_to_paths.get(d, ()))
            for art in artifact_terms:
                candidate_symbols.extend(vocab.domain_artifact_to_symbols.get((d, art), ()))
    candidate_paths = list(dict.fromkeys(candidate_paths))[:50]
    candidate_symbols = list(dict.fromkeys(candidate_symbols))[:50]

    # Confidence: high when domain + artifact both resolved against vocab.
    if domain_terms and artifact_terms:
        confidence = "high"
    elif domain_terms or artifact_terms:
        confidence = "medium"
    else:
        confidence = "low"

    return RecallIntent(
        raw_query=query,
        action=action,
        domain_terms=tuple(domain_terms),
        operation_terms=tuple(op_terms),
        target_artifact_terms=tuple(artifact_terms),
        forbidden_or_conflicting_artifacts=tuple(forbidden),
        candidate_paths=tuple(candidate_paths),
        candidate_symbols=tuple(candidate_symbols),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Downrank post-processor
# ---------------------------------------------------------------------------


# Multiplier applied to hit scores whose source_file lives under a
# forbidden domain. <1 = downrank. Soft, not exclusion.
FORBIDDEN_DOWNRANK = 0.35

# Multiplier applied to hits whose source_file lives under a chosen
# domain. Slight boost so structurally-relevant hits surface above
# cross-domain noise.
DOMAIN_BOOST = 1.25


def downrank_by_intent(
    hits: list,
    intent: RecallIntent,
    *,
    source_file_attr: str = "source_file",
    score_attr: str = "score",
) -> list:
    """Re-rank hits by applying domain boost + forbidden downrank.

    Operates on duck-typed hit objects (dataclass or dict). Returns a
    new list sorted descending by adjusted score. Original hits are
    not mutated — adjustment lives on a private ``_adj_score``.

    DOES NOT exclude any hit. Forbidden hits stay in the result, just
    pushed below same-domain hits. Per design discussion: under-recall
    is the worse failure mode.
    """
    if not hits:
        return []
    user_domains = set(intent.domain_terms)
    forbidden = set(intent.forbidden_or_conflicting_artifacts)
    if not user_domains and not forbidden:
        return list(hits)

    def _get(h, attr):
        if isinstance(h, dict):
            return h.get(attr)
        return getattr(h, attr, None)

    scored: list[tuple[float, object]] = []
    for h in hits:
        sf = _get(h, source_file_attr) or ""
        base = float(_get(h, score_attr) or 0.0)
        d = _top_dir(sf)
        mult = 1.0
        if d in forbidden:
            mult *= FORBIDDEN_DOWNRANK
        if d in user_domains:
            mult *= DOMAIN_BOOST
        scored.append((base * mult, h))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [h for _, h in scored]
