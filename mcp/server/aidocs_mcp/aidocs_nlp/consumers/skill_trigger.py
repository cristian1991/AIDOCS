"""Skill trigger consumer — infer which skills a prompt activates.

Current model (intent_tokens/en.toml): each skill declares an
__skill_trigger_<name> block with intent = [...] and workflow = [...]
token lists. The runtime resolves these into set lookups, matching on
NORMALIZED LITERAL STRINGS — so "I want to brainstorm new ideas"
does NOT fire the brainstorming skill unless the agent explicitly
passes intent="brainstorming" or intent="creative".

This consumer adds NLP inference: given a free-form user prompt,
extract noun+verb lemmas and match against each skill's intent tokens
(also lemmatized). Returns ranked candidates the caller (skill scanner /
UserPromptSubmit hook) can union with the current literal-match path.

Doesn't replace the current path — augments it. The literal path stays
useful for agent-supplied intent strings; the NLP path covers
free-form user prompts where the operator didn't explicitly name an
intent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..service import NLPService


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """One skill the prompt may want to activate."""

    skill_name: str
    score: float
    matched_lemmas: tuple[str, ...]
    matched_axis: str  # "intent" | "workflow" | "both"
    # #620: the RESOLVABLE address of the suggestion. ``skill_name`` is the
    # raw intent_tokens ``parent_key`` (a flat vocab key), which is NOT an
    # address: the catalog namespaces external-provider skills
    # ("superpowers_external/writing-plans"), so a flat name may not exist as
    # a catalog ``skill_id`` at all. Nothing may surface without a handle the
    # surfacing tool itself can resolve.
    #
    # DEFAULTED and left EMPTY by the detectors on purpose: this consumer is
    # NLP-only and never reaches for the skill catalog. Identity is attached
    # downstream by the ONE identity module
    # (``skill_resolution.resolve_suggested_skill_handle``) — never by a rival
    # resolver here. An empty handle means "unresolvable"; the emit site then
    # renders no retrieval call rather than fabricating one (drop-on-doubt).
    skill_id: str = ""


# Stop-lemmas — too generic to be useful as skill activation signals.
_STOP_LEMMAS = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "it",
        "thing",
        "stuff",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "yours",
        "do",
        "be",
        "have",
        "make",
        "let",
        "want",
        "need",
        "try",
        "use",
        "get",
        "go",
        "see",
        "look",  # too verb-generic
    },
)


def _normalize(text: str) -> str:
    """Same normalization as runtime_service._normalize_skill_trigger_token:
    lowercase, replace hyphens with spaces, strip.
    """
    return text.lower().replace("-", " ").strip()


_WORD_RE = re.compile(r"[a-z0-9]+")


def _literal_words(text: str) -> set[str]:
    """Normalized alphanumeric words of `text`, minus stop-lemmas and tokens
    shorter than 3 chars. NLP-free; same length/stop filters as the lemma path
    so candidate scoring stays comparable."""
    out: set[str] = set()
    for w in _WORD_RE.findall(_normalize(text)):
        if len(w) >= 3 and w not in _STOP_LEMMAS:
            out.add(w)
    return out


def detect_skill_triggers_literal(
    prompt: str,
    skill_trigger_tokens: dict[str, dict[str, list[str]]],
    *,
    top_n: int = 5,
    min_score: float = 1.0,
) -> list[SkillCandidate]:
    """NLP-free, best-effort skill suggestion via literal word overlap.

    Mirror of detect_skill_triggers but with no NLPService dependency: the
    prompt and each skill's intent/workflow tokens are reduced to normalized
    words (lowercased, hyphens→spaces, ≥3 chars, stop-lemmas dropped) and
    intersected. Same 2×intent + 1×workflow scoring and ordering.

    Best-effort: it preserves LITERAL trigger matches (exact normalized words)
    but does not collapse inflections the way the lemma path does — a prompt
    that only matches an inflected surface form ("brainstorm" vs token
    "brainstorming") will not fire. Returns [] on empty prompt/triggers.
    """
    if not prompt or not prompt.strip() or not skill_trigger_tokens:
        return []
    prompt_words = _literal_words(prompt)
    if not prompt_words:
        return []

    out: list[SkillCandidate] = []
    for skill_name, axes in skill_trigger_tokens.items():
        intent_words: set[str] = set()
        for t in (axes.get("intent") or []):
            intent_words |= _literal_words(t)
        workflow_words: set[str] = set()
        for t in (axes.get("workflow") or []):
            workflow_words |= _literal_words(t)
        intent_hits = prompt_words & intent_words
        workflow_hits = prompt_words & workflow_words
        total_hits = intent_hits | workflow_hits
        if len(total_hits) < min_score:
            continue
        score = 2.0 * len(intent_hits) + 1.0 * len(workflow_hits)
        axis = "both" if intent_hits and workflow_hits else "intent" if intent_hits else "workflow"
        out.append(
            SkillCandidate(
                skill_name=skill_name,
                score=score,
                matched_lemmas=tuple(sorted(total_hits)),
                matched_axis=axis,
            ),
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:top_n]


# One spaCy call per ~400 chars of trigger vocabulary, NOT per skill (#693).
# 400 keeps each doc inside the service's short-text analyze budget while
# letting one parse carry many skills' token lists.
_BATCH_CHUNK_CHARS = 400
_SEGMENT_SEP = " ; "


def _floor_words(tokens: list[str], *, min_len: int) -> set[str]:
    out: set[str] = set()
    for t in tokens:
        for word in _normalize(t).split():
            if word and word not in _STOP_LEMMAS and len(word) >= min_len:
                out.add(word)
    return out


def _lemmatize_token_lists(
    token_lists: list[list[str]],
    service: NLPService,
) -> list[set[str]]:
    """Lemmatise MANY static token lists with O(total text) spaCy calls
    instead of one per list (#693 — the #688 disease, no cache at all).

    The enclosing loop used to multiply a "we only invoke spaCy once" comment
    by the number of registered skills: two full ``analyze_substance`` parses
    PER SKILL, on token lists that never change between prompts. The lists are
    batched into shared documents here and each substance token is attributed
    back to its owning list by character offset — the same per-list lemma sets,
    without paying per-list pipeline overhead.
    """
    results: list[set[str]] = [set() for _ in token_lists]
    # Normalized originals — the NLP-free floor, kept because spaCy mistags
    # domain terms ("brainstorming") that the substance projection then drops.
    for idx, tokens in enumerate(token_lists):
        results[idx] |= _floor_words(tokens, min_len=3)

    segments = [
        (idx, " ".join(_normalize(t) for t in tokens))
        for idx, tokens in enumerate(token_lists)
        if tokens
    ]
    chunks: list[list[tuple[int, str]]] = []
    chunk: list[tuple[int, str]] = []
    chunk_chars = 0
    for idx, seg_text in segments:
        if chunk and chunk_chars + len(seg_text) > _BATCH_CHUNK_CHARS:
            chunks.append(chunk)
            chunk, chunk_chars = [], 0
        chunk.append((idx, seg_text))
        chunk_chars += len(seg_text) + len(_SEGMENT_SEP)
    if chunk:
        chunks.append(chunk)

    for group in chunks:
        spans: list[tuple[int, int, int]] = []  # (list_idx, start, end)
        parts: list[str] = []
        cursor = 0
        for idx, seg_text in group:
            spans.append((idx, cursor, cursor + len(seg_text)))
            parts.append(seg_text)
            cursor += len(seg_text) + len(_SEGMENT_SEP)
        substance = service.analyze_substance(_SEGMENT_SEP.join(parts))
        if substance is None:
            # NLP unavailable for this chunk — same fallback as before:
            # normalized tokens as-is.
            for idx, _seg in group:
                results[idx] |= _floor_words(token_lists[idx], min_len=1)
            continue
        for collection in (substance.nouns, substance.verbs, substance.adverbs):
            for tok in collection:
                lemma = tok.lemma.lower()
                if not lemma or lemma in _STOP_LEMMAS or len(lemma) < 3:
                    continue
                pos = int(getattr(tok, "char_start", 0) or 0)
                for idx, start, end in spans:
                    if start <= pos < end:
                        results[idx].add(lemma)
                        break
    return results


def load_skill_trigger_tokens(
    langs: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Load skill-trigger token lists from the intent_tokens_store.

    Queries `get_rows_by_kind(lang, 'skill_trigger')` for each lang and
    groups rows by `parent_key` (= skill_name). Inner dict splits tokens
    by `parent_mode` ('intent' vs 'workflow').

    Return shape matches the legacy TOML loader:
        {skill_name: {"intent": [...], "workflow": [...]}}
    """
    from ... import intent_tokens_store as store  # type: ignore

    if not langs:
        langs = ("en",)
    out: dict[str, dict[str, list[str]]] = {}
    for lang in langs:
        try:
            rows = store.get_rows_by_kind(lang, "skill_trigger")
        except Exception:
            continue
        for row in rows:
            skill_name = row.get("parent_key") or ""
            mode = row.get("parent_mode") or ""
            token = row.get("token") or ""
            if not skill_name or not token or mode not in ("intent", "workflow"):
                continue
            axes = out.setdefault(skill_name, {"intent": [], "workflow": []})
            bucket = axes.setdefault(mode, [])
            if token not in bucket:
                bucket.append(token)
    return out


def detect_skill_triggers(
    prompt: str,
    skill_trigger_tokens: dict[str, dict[str, list[str]]],
    service: NLPService,
    *,
    top_n: int = 5,
    min_score: float = 1.0,
) -> list[SkillCandidate]:
    """Return skills the prompt may want to activate.

    Args:
      prompt: free-form user text (or agent thought, etc.).
      skill_trigger_tokens: {skill_name: {"intent": [...], "workflow": [...]}}
        as loaded from intent_tokens/<lang>.toml __skill_trigger_* blocks.
      service: NLPService.
      top_n: cap on returned candidates.
      min_score: matched lemmas needed to count as a fire (default 1).

    Returns empty list when NLP unavailable or no triggers configured.

    """
    if not prompt or not prompt.strip() or not skill_trigger_tokens:
        return []
    substance = service.analyze_substance(prompt)
    if substance is None:
        return []
    prompt_lemmas: set[str] = set()
    for collection in (substance.nouns, substance.verbs, substance.adverbs):
        for tok in collection:
            l = tok.lemma.lower()
            if l and l not in _STOP_LEMMAS and len(l) >= 3:
                prompt_lemmas.add(l)
    if not prompt_lemmas:
        return []

    # #693: lemmatise EVERY skill's (static) token lists in one batch — the
    # spaCy cost now scales with the trigger vocabulary, not with the number of
    # registered skills times two.
    names = list(skill_trigger_tokens)
    axis_lists: list[list[str]] = []
    for skill_name in names:
        axes = skill_trigger_tokens[skill_name]
        axis_lists.append(list(axes.get("intent") or []))
        axis_lists.append(list(axes.get("workflow") or []))
    lemma_sets = _lemmatize_token_lists(axis_lists, service)

    out: list[SkillCandidate] = []
    for pos, skill_name in enumerate(names):
        intent_lemmas = lemma_sets[2 * pos]
        workflow_lemmas = lemma_sets[2 * pos + 1]
        intent_hits = prompt_lemmas & intent_lemmas
        workflow_hits = prompt_lemmas & workflow_lemmas
        total_hits = intent_hits | workflow_hits
        if len(total_hits) < min_score:
            continue
        # Score: intent matches weighted 2x, workflow matches 1x.
        # Rationale: intent is what the user EXPLICITLY wants to do;
        # workflow is the action class (less direct signal).
        score = 2.0 * len(intent_hits) + 1.0 * len(workflow_hits)
        axis = "both" if intent_hits and workflow_hits else "intent" if intent_hits else "workflow"
        out.append(
            SkillCandidate(
                skill_name=skill_name,
                score=score,
                matched_lemmas=tuple(sorted(total_hits)),
                matched_axis=axis,
            ),
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:top_n]
