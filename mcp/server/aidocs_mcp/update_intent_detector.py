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

LEXICON_VERSION = "v2"

# The stored evidence is a PREFIX of the sentence; a snippet this long may be
# truncated, so the cleanup treats it as unprovable (needs manual review).
SNIPPET_MAX = 200

# ── Lexicon (v2, English) ────────────────────────────────────────────────
# BASE forms only. A verb counts when the clause reader (``_instructs``)
# finds it as an imperative or a first/second-person present/future intent;
# past tense, participles, third person and noun uses never instruct.
_BASE_VERBS: frozenset[str] = frozenset(
    {
        "update", "change", "revise", "rework", "add", "remove", "drop",
        "reprioritize", "prioritize", "deprioritize", "deprecate", "finalize",
        "decide", "rename", "postpone", "defer", "promote", "demote", "cancel",
        "scrap", "split", "merge", "skip", "file", "record", "capture",
    }
)

# Gerunds: admitted ONLY inside hedged/questioned sentences ("worth adding a
# todo?"), where they classify AMBIGUOUS (confirm line, never a gate).
_GERUND_VERBS: frozenset[str] = frozenset(
    {
        "adding", "removing", "updating", "changing", "dropping", "prioritizing",
        "reprioritizing", "deprecating", "renaming", "postponing", "deferring",
        "cancelling", "canceling", "merging", "splitting", "recording",
        "capturing", "filing", "finalizing",
    }
)

# Clause-reader vocabulary.
_FILLERS: frozenset[str] = frozenset(
    {
        "please", "kindly", "just", "now", "also", "then", "ok", "okay", "so",
        "first", "finally", "next", "really", "actually", "definitely",
        "immediately", "and", "but",
    }
)
_SUBJECTS: frozenset[str] = frozenset(
    {
        "i", "we", "you", "i'm", "we're", "you're", "i've", "we've", "i'd",
        "we'd", "i'll", "we'll", "you'll",
    }
)
_SUBJECT_MODALS: frozenset[str] = frozenset({"i'll", "we'll", "you'll", "let's", "lets"})
_MODALS: frozenset[str] = frozenset({"will", "shall", "should", "must", "gonna"})
_INTENT_VP: frozenset[str] = frozenset(
    {"plan", "going", "gonna", "need", "want", "have", "intend", "like", "ought"}
)
_AUX: frozenset[str] = frozenset(
    {"am", "are", "be", "will", "shall", "really", "definitely", "just", "also", "now", "then"}
)
_STRUCTURAL_VERBS: frozenset[str] = frozenset(
    {
        "split", "merge", "drop", "defer", "postpone", "cancel", "scrap", "skip",
        "reprioritize", "deprioritize", "promote", "demote", "deprecate",
    }
)
_NEG_WORDS: frozenset[str] = frozenset({"no", "not"})
_CLAUSE_BREAK_CHARS: str = ",:;—–()[]|"
# Hyphen / underscore / dot compounds (task-notification, output-file,
# update_intent_hook) are ONE token and never match the lexicon.
_TOKEN = re.compile(r"[a-z][a-z']*(?:[-_.][a-z0-9][a-z0-9']*)*")

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


def _tokens(low: str) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for m in _TOKEN.finditer(low):
        word = m.group(0).rstrip("'")
        out.append((word, m.start(), m.start() + len(word)))
    return out


def _break_between(low: str, a_end: int, b_start: int) -> bool:
    seg = low[a_end:b_start]
    return any(c in seg for c in _CLAUSE_BREAK_CHARS) or " - " in seg


def _clause_text(low: str, start: int, end: int) -> str:
    left = max((low.rfind(c, 0, start) for c in _CLAUSE_BREAK_CHARS), default=-1)
    rights = [p for p in (low.find(c, end) for c in _CLAUSE_BREAK_CHARS) if p != -1]
    right = min(rights) if rights else len(low)
    return low[left + 1 : right]


def _subject_before(toks: list[tuple[str, int, int]], idx: int, low: str) -> bool:
    """True when the token before ``idx`` (skipping fillers) is a first/second
    person subject in the same clause."""
    j = idx
    while j > 0 and toks[j - 1][0] in _FILLERS and not _break_between(low, toks[j - 1][2], toks[j][1]):
        j -= 1
    if j == 0 or _break_between(low, toks[j - 1][2], toks[j][1]):
        return False
    return toks[j - 1][0] in _SUBJECTS


def _infinitive_intent(toks: list[tuple[str, int, int]], to_idx: int, low: str) -> bool:
    """``<subject> [aux...] <plan|need|want|going|...> to <verb>``."""
    j = to_idx - 1
    if j < 0 or toks[j][0] not in _INTENT_VP:
        return False
    j -= 1
    while j >= 0 and toks[j][0] in _AUX:
        j -= 1
    return j >= 0 and toks[j][0] in _SUBJECTS and not _break_between(low, toks[j][2], toks[j + 1][1])


def _instructs(toks: list[tuple[str, int, int]], i: int, low: str) -> int:
    """Clause reader: 1 = imperative, 2 = explicit first/second-person
    present/future intent frame ("we will X", "I plan to X", "let's X"),
    0 = does not instruct. Past tense, gerunds, participles, third person and
    noun uses fail here."""
    k = i
    while k > 0 and toks[k - 1][0] in _FILLERS and not _break_between(low, toks[k - 1][2], toks[k][1]):
        k -= 1
    if k == 0 or _break_between(low, toks[k - 1][2], toks[k][1]):
        return 1  # imperative: first word of its clause
    prev = toks[k - 1][0]
    if prev in _SUBJECT_MODALS:
        return 2
    if prev in _MODALS:
        return 2 if _subject_before(toks, k - 1, low) else 0
    if prev == "to":
        return 2 if _infinitive_intent(toks, k - 1, low) else 0
    return 0


def _clause_negated(clause: str) -> bool:
    c = clause.replace("no longer", " ").replace("not anymore", " ")
    if any(n in c for n in _NEGATORS):
        return True
    return bool(_NEG_WORDS & set(_WORD.findall(c)))


def _spacy_confirms(doc) -> bool:
    """Tier 2: a base-form (VB / VerbForm=Inf) update verb plus a planning
    object lemma. Morphology-aware so 'changed'/'changes' never upgrade."""
    objs = False
    verb = False
    for t in doc:
        lemma = t.lemma_.lower()
        if lemma in _PLANNING_OBJECTS:
            objs = True
        if lemma in _BASE_VERBS and (
            getattr(t, "tag_", "") in ("VB", "VBP") or "Inf" in str(getattr(t, "morph", ""))
        ):
            verb = True
    return verb and objs


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
        toks = _tokens(low)

        hedged = any(h in low for h in _HEDGES) or sentence.rstrip().endswith("?")

        # Strict readings (imperative / 1st-2nd person intent) always; hedged
        # sentences also admit base+gerund forms anywhere (AMBIGUOUS only).
        verb_idx: list[int] = []
        frame_verbs: set[str] = set()
        for i, (w, s, e) in enumerate(toks):
            ok = False
            mode = _instructs(toks, i, low) if w in _BASE_VERBS else 0
            if mode:
                ok = True
            elif w == "decided" and _subject_before(toks, i, low) and low[e:].lstrip().startswith(":"):
                ok = True
            elif hedged and (w in _BASE_VERBS or w in _GERUND_VERBS):
                ok = True
            if ok and not _clause_negated(_clause_text(low, s, e)):
                verb_idx.append(i)
                if mode == 2 and w in _STRUCTURAL_VERBS:
                    frame_verbs.add(w)
        verbs = tuple(sorted({toks[i][0] for i in verb_idx}))
        # 'plan' used as the VERB ("I plan to ...") is not a planning object.
        objects = tuple(
            sorted(
                {
                    w
                    for j, (w, _s, _e) in enumerate(toks)
                    if w in _PLANNING_OBJECTS
                    and not (w in ("plan", "plans") and j > 0 and toks[j - 1][0] in _SUBJECTS)
                }
            )
        )
        has_id_ref = bool(_ID_REF.search(sentence))
        phrases = tuple(p for p in _PHRASE_SIGNALS if p in low)
        signals: list[str] = []

        pair = bool(verbs) and bool(objects or has_id_ref)
        phrase_hit = bool(phrases) and bool(objects or has_id_ref) and not _clause_negated(low)
        # An explicit intent frame with a structural verb is a plan statement
        # on its own ("we will split the deploy" == "I plan to split the deploy").
        frame_hit = bool(frame_verbs) and not pair
        if not pair and not phrase_hit and not frame_hit:
            continue

        if frame_hit:
            signals.append("intent_frame")
        if pair:
            signals.append("verb_object_pair")
        if phrase_hit:
            signals.append("phrase_signal")
        if has_id_ref:
            signals.append("id_ref")

        confidence = 1.0 if (pair and not hedged) else 0.6
        ambiguous = hedged

        # Tier 2 (optional): spaCy refinement can only ADJUST confidence.
        if nlp is not None and confidence == 0.6 and not hedged:
            try:
                if _spacy_confirms(nlp(sentence)):
                    confidence = 1.0
                    ambiguous = False
                    signals.append("spacy_lemma_pair")
            except Exception:  # noqa: BLE001 - tier 2 must never break tier 1
                pass

        proposal = UpdateIntentProposal(
            detected=True,
            confidence=confidence,
            verbs=verbs,
            objects=objects,
            snippet=sentence[:SNIPPET_MAX],
            ambiguous=ambiguous,
            suggested_target=_suggest_target(low),
            signals=tuple(signals),
        )
        if not best.detected or proposal.confidence > best.confidence:
            best = proposal
        if best.confidence >= 1.0 and not best.ambiguous:
            break
    return best
