"""Deterministic operator UPDATE-intent detector (#219/#221 PR-1).

Recognizes prompts that CHANGE plan/spec/task/roadmap/priority/decision
state — the operator utterances that must become durable (ai_backlog /
ai_task todo / ai_plan / memory_capture) instead of evaporating on compaction.

Doctrine (approved design 242f237d, Empire §9 answers 2026-07-04):
  * PURE: no I/O, no LLM judge. spaCy (optional ``nlp`` arg) REFINES the
    deterministic tier-1 verdict; its absence degrades to tier 1 — never off.
  * Recall-biased for ADVISE mode; only confidence 1.0 may ever gate (the
    block stage is NOT part of PR-1).
  * Quoted/code text never fires; questions and hedges classify AMBIGUOUS
    (confirm-line, never a gate); negation kills the match.
  * Callers must pass USER PROMPTS only — agent echoes are excluded at the
    call site (only AnalysisSource.USER_PROMPT creates a pending row).

Lexicon is versioned: changing _LEXICON_V1 is a CONTRACT MOVE — update the
table corpus in tests/nlp/test_update_intent_detector.py alongside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["UpdateIntentProposal", "detect_update_intent", "LEXICON_VERSION"]

LEXICON_VERSION = "v1"

# ── Lexicon (v1, English) ────────────────────────────────────────────────
_UPDATE_VERBS: frozenset[str] = frozenset(
    {
        "update",
        "updated",
        "change",
        "changed",
        "revise",
        "revised",
        "rework",
        "reworked",
        "add",
        "added",
        "remove",
        "removed",
        "drop",
        "dropped",
        "reprioritize",
        "reprioritized",
        "prioritize",
        "prioritized",
        "deprioritize",
        "deprecate",
        "deprecated",
        "finalize",
        "finalized",
        "decide",
        "decided",
        "rename",
        "renamed",
        "postpone",
        "postponed",
        "defer",
        "deferred",
        "promote",
        "promoted",
        "demote",
        "demoted",
        "cancel",
        "cancelled",
        "canceled",
        "scrap",
        "scrapped",
        "split",
        "merge",
        "merged",
        "skip",
        "skipped",
        "file",
        "record",
        "capture",
        # gerunds — hedged forms ("worth adding a todo?") still surface
        "adding",
        "removing",
        "updating",
        "changing",
        "dropping",
        "prioritizing",
        "reprioritizing",
        "deprecating",
        "renaming",
        "postponing",
        "deferring",
        "cancelling",
        "canceling",
        "merging",
        "splitting",
        "recording",
        "capturing",
        "filing",
        "finalizing",
    }
)

_PLANNING_OBJECTS: frozenset[str] = frozenset(
    {
        "plan",
        "plans",
        "spec",
        "specs",
        "task",
        "tasks",
        "roadmap",
        "backlog",
        "todo",
        "todos",
        "priority",
        "priorities",
        "prio",
        "decision",
        "decisions",
        "milestone",
        "milestones",
        "deadline",
        "deadlines",
        "scope",
        "requirement",
        "requirements",
        "item",
        "items",
        "feature",
        "features",
        "goal",
        "goals",
    }
)

# Multi-word / phrase signals that carry update-intent on their own when a
# planning object (or #id) is present.
_PHRASE_SIGNALS: tuple[str, ...] = (
    "needs fixing",
    "needs to be fixed",
    "should be",
    "from now on",
    "instead of",
    "no longer",
    "not anymore",
    # operator work-direction prefixes — a task switch IS a durable directive
    "next task",
    "new task",
    "next item",
    "t0 prio",
    "t0:",
)

# Standing-law phrasing → suggest memory (a rule), not a backlog row.
_LAW_MARKERS: tuple[str, ...] = ("from now on", "always ", "never ", "going forward")

_NEGATORS: tuple[str, ...] = (
    "don't",
    "dont",
    "do not",
    "no need to",
    "without",
    "not going to",
    "won't",
    "wont",
    "shouldn't",
    "shouldnt",
    "skip recording",
    "no changes to",
)

_HEDGES: tuple[str, ...] = (
    "should we",
    "could we",
    "maybe we",
    "what if",
    "would it",
    "do you think",
    "wondering",
    "worth ",
)

_ID_REF = re.compile(r"#\d{1,6}\b")
_WORD = re.compile(r"[a-z']+")

# fenced code, inline code, and quoted spans never fire
_EXCLUDED_SPANS = re.compile(
    r"```.*?```|`[^`\n]*`|\"[^\"\n]{4,}\"|'[^'\n]{8,}'",
    re.DOTALL,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;\n])\s+|\n+")


@dataclass(frozen=True)
class UpdateIntentProposal:
    detected: bool
    confidence: float = 0.0
    verbs: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    snippet: str = ""
    ambiguous: bool = False
    suggested_target: str = "backlog"
    signals: tuple[str, ...] = field(default=())


_NONE = UpdateIntentProposal(detected=False)


def _strip_excluded(text: str) -> str:
    return _EXCLUDED_SPANS.sub(" ", text)


def _suggest_target(sentence_lower: str) -> str:
    if any(m in sentence_lower for m in _LAW_MARKERS):
        return "memory"
    if "todo" in sentence_lower or "this task" in sentence_lower or " now" in sentence_lower:
        return "todo"
    if "plan" in sentence_lower or "lane" in sentence_lower or "step" in sentence_lower:
        return "plan"
    return "backlog"


def detect_update_intent(text: str, *, nlp=None) -> UpdateIntentProposal:
    """Classify one operator prompt. Pure; <5ms tier-1 on a 4KB prompt."""
    if not text or not text.strip():
        return _NONE
    cleaned = _strip_excluded(text)
    best: UpdateIntentProposal = _NONE
    for raw_sentence in _SENTENCE_SPLIT.split(cleaned):
        sentence = raw_sentence.strip()
        if not sentence or len(sentence) > 600:
            continue
        low = sentence.lower()
        words = set(_WORD.findall(low))

        verbs = tuple(sorted(words & _UPDATE_VERBS))
        objects = tuple(sorted(words & _PLANNING_OBJECTS))
        has_id_ref = bool(_ID_REF.search(sentence))
        phrases = tuple(p for p in _PHRASE_SIGNALS if p in low)
        signals: list[str] = []

        # Negation kills the sentence outright.
        if any(n in low for n in _NEGATORS):
            continue

        pair = bool(verbs) and bool(objects or has_id_ref)
        phrase_hit = bool(phrases) and bool(objects or has_id_ref)
        if not pair and not phrase_hit:
            continue

        if pair:
            signals.append("verb_object_pair")
        if phrase_hit:
            signals.append("phrase_signal")
        if has_id_ref:
            signals.append("id_ref")

        hedged = any(h in low for h in _HEDGES) or sentence.rstrip().endswith("?")
        confidence = 1.0 if (pair and not hedged) else 0.6
        ambiguous = hedged

        # Tier 2 (optional): spaCy refinement — lemma re-check + imperative
        # detection can only ADJUST confidence/ambiguity, never create or
        # destroy a tier-1 match class on its own.
        if nlp is not None and confidence == 0.6 and not hedged:
            try:
                doc = nlp(sentence)
                lemmas = {t.lemma_.lower() for t in doc}
                if lemmas & _UPDATE_VERBS and lemmas & _PLANNING_OBJECTS:
                    confidence = 1.0
                    ambiguous = False
                    signals.append("spacy_lemma_pair")
            except Exception:  # noqa: BLE001 — tier 2 must never break tier 1
                pass

        proposal = UpdateIntentProposal(
            detected=True,
            confidence=confidence,
            verbs=verbs,
            objects=objects,
            snippet=sentence[:200],
            ambiguous=ambiguous,
            suggested_target=_suggest_target(low),
            signals=tuple(signals),
        )
        if not best.detected or proposal.confidence > best.confidence:
            best = proposal
        if best.confidence >= 1.0 and not best.ambiguous:
            break
    return best
