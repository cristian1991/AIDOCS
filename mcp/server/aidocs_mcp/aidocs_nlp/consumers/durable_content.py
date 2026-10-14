"""Declarative-durable content detector (#9 auto-surfacing slice).

Sibling of ``update_intent_detector``: where that one recognizes operator
COMMANDS that change plan state ("drop #211", "reprioritize the backlog"),
this one recognizes DECLARATIVE durable knowledge stated as fact — rules,
preferences, decisions, invariants ("X should always Y", "we decided Z",
"the convention is ...") — the statements that should land in
``memory_capture`` instead of evaporating on compaction.

Doctrine (mirrors update_intent_detector):
  * PURE: no I/O, no LLM judge. Optional ``nlp`` REFINES the deterministic
    tier-1 verdict; its absence degrades to tier 1 — never off.
  * Linguistic shape, not keyword soup. A durable statement is DECLARATIVE:
    it carries genericity/normativity structure (normative modal + universal
    quantifier, a decision/preference/convention frame, an invariant frame).
    Bare imperatives (do-it-now commands) and questions never fire. The one
    imperative shape that DOES fire is a universality-carried standing rule
    ("always run the linter before handoff" is a rule; "run the linter" is
    a task).
  * Quoted/fenced/inline-code text never fires.

Lexicon is versioned: changing the marker sets is a CONTRACT MOVE — update
the corpus in tests/nlp/test_durable_content_detector.py alongside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["DurableContentProposal", "detect_durable_content", "LEXICON_VERSION"]

LEXICON_VERSION = "v1"

# ── Lexicon (v1, English) ────────────────────────────────────────────────
# Normative modality: obligation/expectation stated about a subject.
_NORMATIVE_MODAL = re.compile(r"\b(should|must|shall|has to|have to|needs? to)\b")

# Universal quantification: what turns an obligation into a standing rule.
_UNIVERSAL_MARKERS: tuple[str, ...] = (
    "always",
    "never",
    "every time",
    "whenever",
    "by default",
    "in all cases",
)

# Standing-law openers: durable on their own (they announce a new law).
_LAW_OPENERS: tuple[str, ...] = ("from now on", "going forward")

# Convention/policy frames — rules stated as institutional fact.
_RULE_PHRASES: tuple[str, ...] = (
    "the convention is",
    "our convention",
    "by convention",
    "the rule is",
    "the policy is",
    "as a rule",
    "standard practice",
    "coding standard",
)

# Decision frames — settled outcomes, past/perfective, 1st-plural or passive.
_DECISION_PHRASES: tuple[str, ...] = (
    "we decided",
    "we've decided",
    "we have decided",
    "it was decided",
    "we agreed",
    "we've agreed",
    "the decision is",
    "we chose",
    "we settled on",
    "we're going with",
    "we are going with",
    "decision:",
)

# Preference frames — stated dispositions, not one-off requests.
_PREFERENCE_PHRASES: tuple[str, ...] = (
    "i prefer",
    "we prefer",
    "i'd rather",
    "we'd rather",
    "preferred approach",
    "preferred way",
    "the preference is",
)

# Invariant frames — properties asserted to hold permanently.
_INVARIANT_PHRASES: tuple[str, ...] = (
    "invariant",
    "is canonical",
    "source of truth",
    "must remain",
    "must stay",
    "never changes",
)

# Question shape: terminal '?' or subject–auxiliary inversion at the start.
_INTERROGATIVE_LEADS: frozenset[str] = frozenset(
    {
        "what", "why", "how", "when", "where", "who", "which", "whose",
        "is", "are", "was", "were", "do", "does", "did", "can", "could",
        "will", "would", "shall", "should", "may", "might", "am",
    }
)

_WORD = re.compile(r"[a-z']+")

# fenced code, inline code, and quoted spans never fire (same exclusion
# shape as update_intent_detector).
_EXCLUDED_SPANS = re.compile(
    r"```.*?```|`[^`\n]*`|\"[^\"\n]{4,}\"|'[^'\n]{8,}'",
    re.DOTALL,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;\n])\s+|\n+")

_SUBJECT_DEPS = frozenset({"nsubj", "nsubjpass", "nsubj:pass", "csubj"})

_MIN_WORDS = 4  # fragments ("Never mind.") carry no durable proposition


@dataclass(frozen=True)
class DurableContentProposal:
    detected: bool
    kind: str = ""  # rule | decision | preference | invariant
    confidence: float = 0.0
    snippet: str = ""
    signals: tuple[str, ...] = ()


_NONE = DurableContentProposal(detected=False)


def _classify_sentence(low: str) -> tuple[str, float, tuple[str, ...]] | None:
    """Return (kind, confidence, signals) for one lowered sentence, or None."""
    signals: list[str] = []

    # Decision frame wins first — "we decided X is canonical" is a decision.
    if any(p in low for p in _DECISION_PHRASES):
        return "decision", 1.0, ("decision_frame",)

    if any(p in low for p in _PREFERENCE_PHRASES):
        return "preference", 1.0, ("preference_frame",)

    if any(p in low for p in _RULE_PHRASES):
        return "rule", 1.0, ("convention_frame",)

    if any(low.startswith(o) or f", {o}" in low for o in _LAW_OPENERS):
        return "rule", 1.0, ("law_opener",)

    has_modal = bool(_NORMATIVE_MODAL.search(low))
    has_universal = any(m in low for m in _UNIVERSAL_MARKERS)
    if has_modal and has_universal:
        # "stores must never be deleted" — obligation + universality.
        signals = ["normative_modal", "universal_marker"]
        if any(p in low for p in _INVARIANT_PHRASES):
            return "invariant", 1.0, ("invariant_frame", *signals)
        return "rule", 1.0, tuple(signals)

    if any(p in low for p in _INVARIANT_PHRASES):
        return "invariant", 0.8, ("invariant_frame",)

    # Universality-carried standing imperative: "always run X before Y".
    if any(low.startswith(f"{m} ") for m in ("always", "never")):
        return "rule", 0.8, ("universal_lead",)

    return None


def detect_durable_content(text: str, *, nlp=None) -> DurableContentProposal:
    """Classify one span of conversation content. Pure; tier-1 deterministic."""
    if not text or not text.strip():
        return _NONE
    cleaned = _EXCLUDED_SPANS.sub(" ", text)
    best: DurableContentProposal = _NONE
    for raw_sentence in _SENTENCE_SPLIT.split(cleaned):
        sentence = raw_sentence.strip()
        if not sentence or len(sentence) > 600:
            continue
        low = sentence.lower()
        words = _WORD.findall(low)
        if len(words) < _MIN_WORDS:
            continue
        # Questions never fire: terminal '?' or aux/wh inversion at start.
        if sentence.rstrip().endswith("?") or (words and words[0] in _INTERROGATIVE_LEADS):
            continue

        hit = _classify_sentence(low)
        if hit is None:
            continue
        kind, confidence, signals = hit

        # Tier 2 (optional): a parsed nominal subject confirms declarative
        # shape — it may only RAISE confidence, never create/destroy a match.
        if nlp is not None and confidence < 1.0:
            try:
                doc = nlp(sentence)
                if any(getattr(t, "dep_", "") in _SUBJECT_DEPS for t in doc):
                    confidence = 1.0
                    signals = (*signals, "spacy_declarative_subject")
            except Exception:  # noqa: BLE001 — tier 2 must never break tier 1
                pass

        proposal = DurableContentProposal(
            detected=True,
            kind=kind,
            confidence=confidence,
            snippet=sentence[:200],
            signals=tuple(signals),
        )
        if not best.detected or proposal.confidence > best.confidence:
            best = proposal
        if best.confidence >= 1.0:
            break
    return best
