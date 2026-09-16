"""Memory scope classifier — Front 1 of the memory law-tier war (#213).

Decides, PREDICTABLY and DETERMINISTICALLY, whether a captured memory is
GLOBAL (empire law, shared across projects) or PROJECT (local memory).

Doctrine lock (120% §31 — "law enters only through the throne"):
  - GLOBAL is a PROMOTION, never an auto-assignment. A GLOBAL proposal ALWAYS
    carries ``requires_seal=True``; an operator/sovereign must seal it before it
    becomes law. The classifier only ever *proposes*.
  - The default is fail-closed to PROJECT. Ambiguity, low signal, or any code-unit
    anchor (which pins a memory to a concrete project symbol) yields PROJECT —
    global is never the fallback.

Deterministic-first (KISSSY): a memory is proposed GLOBAL only when it is BOTH
law-shaped (kind) AND speaks in universal language ("always", "every project",
"never", "everywhere", ...) AND is not anchored to a concrete code unit. An
optional ``analyzer`` (spaCy / aidocs_nlp substance) enriches the lemma signal;
without it a plain tokenizer is used, so the logic is provable without the NLP
runtime and the smarter analyzer is a future-proof seam, not a hard dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GLOBAL = "global"
PROJECT = "project"

# Universal / law-bearing language — signals a rule meant to hold everywhere.
_UNIVERSAL = {
    "always", "never", "every", "all", "any", "everywhere", "universal",
    "globally", "global", "mandatory", "everyone", "forbid", "forbidden",
    "anywhere", "anyone",
}

# Law-shaped memory kinds (vs project-scoped kinds below).
_GLOBAL_KINDS = {"rule", "doctrine", "security", "invariant", "workflow-rule", "spec"}
_PROJECT_KINDS = {
    "project", "domain", "daily", "reference", "related_project",
    "infrastructure", "preference",
}


@dataclass
class ScopeProposal:
    """A *proposed* scope. GLOBAL always requires a throne seal; PROJECT auto-applies."""

    scope: str
    confidence: float
    requires_seal: bool
    signals: list[str] = field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


def classify_scope(
    content: str,
    kind: str,
    *,
    anchors: list | None = None,
    analyzer=None,
) -> ScopeProposal:
    """Propose GLOBAL vs PROJECT for a memory. Fail-closed to PROJECT.

    ``analyzer`` (optional) must expose ``lemmas(text) -> Iterable[str]`` — e.g. a
    spaCy/aidocs_nlp adapter. When absent, a plain word tokenizer is used.
    """
    kind = (kind or "").lower()
    anchors = anchors or []

    if analyzer is not None and hasattr(analyzer, "lemmas"):
        lemmas = {str(token).lower() for token in (analyzer.lemmas(content) or [])}
    else:
        lemmas = _tokenize(content)

    universal_hits = lemmas & _UNIVERSAL
    has_universal = bool(universal_hits)
    is_law_kind = kind in _GLOBAL_KINDS
    has_anchor = len(anchors) > 0
    is_project_kind = kind in _PROJECT_KINDS

    signals: list[str] = []
    if has_universal:
        signals.append("universal_language")
    if is_law_kind:
        signals.append("law_kind")
    if has_anchor:
        signals.append("code_anchor")
    if is_project_kind:
        signals.append("project_kind")

    # GLOBAL only when law-shaped AND universal AND not pinned to a concrete code
    # unit. A code anchor always wins for PROJECT — an anchored memory is, by
    # construction, about a specific project symbol.
    propose_global = has_universal and is_law_kind and not has_anchor
    if propose_global:
        confidence = min(0.95, 0.6 + 0.1 * len(universal_hits))
        return ScopeProposal(GLOBAL, confidence, True, signals)

    confidence = 0.8 if (has_anchor or is_project_kind) else 0.55
    return ScopeProposal(PROJECT, confidence, False, signals)
