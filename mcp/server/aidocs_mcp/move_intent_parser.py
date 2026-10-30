"""Read the operator's instruction to MOVE this conversation (#1166).

Operator 2026-10-18/19: "ai project and ai session are user-intent tools
because they change agent permissions/scope" ... "connect to project X,
session ww should have the info necessary" ... "if you see connect project,
then a session and no negation in between then it's a px-sy allow, lemma
smart, not dumb please. ex: connect to project x and any other session
but/except for/don't connect to y".

So this reads a sentence as CLAUSES, not as a pattern:

  * a present-tense move verb (by LEMMA -- connect, switch, move, go, join,
    use, attach, bind, open, enter, hop, jump, change; Romanian conecta /
    trece / muta / comuta / intra) followed by ``project X`` opens X's scope;
  * every session spec after it, until another project is named, belongs to
    X: ``session ww``, ``sessions a and b`` (allow), ``any/all/every (other)
    session(s)`` (allow ANY session of X);
  * polarity is ALLOW until a negation (not, don't, never, no, nu) or an
    exclusion (but, except (for), excluding, besides, without, other than,
    apart from) -- then the ids that follow are EXCLUDED; a fresh,
    non-negated move verb restores ALLOW ("... and connect to session z");
  * a session named with no project belongs to the project the conversation
    is governed by when the operator speaks (the minting seam fills it).

Verb form is judged by morphology when the NLP pack is present (spaCy via
aidocs_nlp: Tense=Past, VerbForm=Part/Ger do not instruct) and by a small
stem heuristic otherwise -- both fail CLOSED: a form they cannot place as an
instruction mints nothing. The caller applies the sentence-level floors
(provenance, question, interrogative lead, past / reported, conditional,
quoted speech); a cancellation ("no wait", "never mind", "scratch that")
drops the sentence's grants.

Output grants (binding_intent_store):
  {"tool": "ai_project", "target": X}
  {"tool": "ai_session", "target": sid | "*", "project": X | ""}
  {"tool": "ai_session_deny", "target": sid, "project": X | ""}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MOVE_LEMMAS: frozenset[str] = frozenset(
    {
        # Not "use" / "open": "use the project x tests", "open the project
        # file" are everyday prose, and a phantom grant is the costly error.
        "connect",
        "switch",
        "move",
        "go",
        "join",
        "attach",
        "bind",
        "enter",
        "hop",
        "jump",
        "change",
        # Romanian (lemma and the imperative forms a fallback sees)
        "conecta",
        "conectează",
        "conecteaza",
        "trece",
        "treci",
        "muta",
        "mută",
        "comuta",
        "comută",
        "intra",
        "intră",
    }
)
PROJECT_NOUNS: frozenset[str] = frozenset(
    {"project", "projects", "repo", "repository", "proiect", "proiectul", "proiectului"}
)
SESSION_NOUNS: frozenset[str] = frozenset(
    {"session", "sessions", "sesiune", "sesiunea", "sesiuni", "sesiunile"}
)
WILDCARD_WORDS: frozenset[str] = frozenset({"any", "all", "every", "orice", "oricare", "toate"})
_WILDCARD_SKIP: frozenset[str] = frozenset({"other", "others", "alta", "alte", "altă", "the"})
NEGATION_WORDS: frozenset[str] = frozenset(
    {"not", "never", "no", "dont", "doesnt", "cant", "cannot", "wont", "shouldnt", "nu", "nici"}
)
EXCLUDE_WORDS: frozenset[str] = frozenset(
    {"but", "except", "excluding", "besides", "without", "exceptand", "exceptând", "fara", "fără"}
)
# Two-word exclusions: "other than", "apart from", "except for", "în afară de".
EXCLUDE_BIGRAMS: frozenset[tuple[str, str]] = frozenset(
    {("other", "than"), ("apart", "from"), ("aside", "from"), ("afara", "de"), ("afară", "de")}
)
CANCEL_WORDS: frozenset[str] = frozenset(
    {"cancel", "nevermind", "wait", "abort", "undo", "scratch", "forget", "anuleaza", "anulează"}
)
_CANCEL_PHRASES = ("never mind", "hold on", "on second thought", "scratch that", "forget it")
FILLER_WORDS: frozenset[str] = frozenset(
    {
        "to",
        "into",
        "onto",
        "on",
        "in",
        "the",
        "a",
        "an",
        "this",
        "that",
        "me",
        "us",
        "window",
        "chat",
        "conversation",
        "please",
        "named",
        "called",
        "id",
        "with",
        "for",
        "la",
        "pe",
        "în",
        "mă",
        "ma",
        "te",
        "of",
        "de",
        "back",
        "over",
        "it",
    }
)
CONNECTORS: frozenset[str] = frozenset({",", ";", "and", "or", "then", "also", "si", "și", "sau"})
# A session noun followed by a PURPOSE is not naming one: "a session for the
# auth work", "session about X".
_SESSION_STOP: frozenset[str] = frozenset(
    {"for", "about", "regarding", "so", "because", "pentru", "despre", "ca"}
)
_PLURAL_SESSION_NOUNS: frozenset[str] = frozenset({"sessions", "sesiuni", "sesiunile"})
_VOCAB = (
    MOVE_LEMMAS
    | PROJECT_NOUNS
    | SESSION_NOUNS
    | WILDCARD_WORDS
    | NEGATION_WORDS
    | EXCLUDE_WORDS
    | CANCEL_WORDS
    | FILLER_WORDS
    | CONNECTORS
    | _WILDCARD_SKIP
)

_TOKEN_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s,;\"`“”]*"  # Windows path
    r"|[\\/~][^\s,;\"`“”]+"  # POSIX / home path
    r"|[\w][\w.\-]*"  # word / id (dots and dashes kept: v1.2, s-2026-10-18)
    r"|[,;]"
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*$")


@dataclass
class _Tok:
    text: str
    low: str
    start: int
    lemma: str = ""
    morph: dict = field(default_factory=dict)


def _tokens(sentence: str) -> list[_Tok]:
    # Apostrophes go, so contractions arrive whole: "don't" -> "dont".
    flat = sentence.replace("'", "").replace("’", "")
    return [_Tok(m.group(0), m.group(0).lower(), m.start()) for m in _TOKEN_RE.finditer(flat)]


def _annotate(sentence: str, toks: list[_Tok], nlp_root: Path | None) -> None:
    """Lemma + morphology from aidocs_nlp, aligned by character offset. A
    missing pack leaves both empty and the stem heuristic decides."""
    if nlp_root is None:
        return
    try:
        from .aidocs_nlp.service import get_service

        doc = get_service(nlp_root).analyze(sentence.replace("'", "").replace("’", ""))
    except Exception:  # noqa: BLE001 -- NLP is an aid, never a dependency
        return
    by_start = {}
    for t in getattr(doc, "tokens", None) or []:
        by_start[int(getattr(t, "char_start", -1))] = t
    for tok in toks:
        nt = by_start.get(tok.start)
        if nt is not None:
            tok.lemma = str(getattr(nt, "lemma", "") or "").lower()
            tok.morph = dict(getattr(nt, "morph", None) or {})


def _move_verb(tok: _Tok) -> str:
    """'instruct' -- a present-tense / imperative move verb; 'reported' -- a
    move verb in a past / participle / gerund form (instructs nothing); ''."""
    if tok.lemma and tok.lemma in MOVE_LEMMAS:
        if (
            tok.morph.get("Tense") == "Past"
            or tok.morph.get("VerbForm") in ("Part", "Ger")
            or tok.morph.get("Person") == "3"  # "she connects ..." reports
        ):
            return "reported"
        return "instruct"
    w = tok.low
    if w in MOVE_LEMMAS:
        return "instruct"
    for suffix, base in (("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"), ("d", "")):
        if w.endswith(suffix) and (w[: -len(suffix)] + base) in MOVE_LEMMAS:
            return "reported"
    if (w.endswith("es") and w[:-2] in MOVE_LEMMAS) or (w.endswith("s") and w[:-1] in MOVE_LEMMAS):
        return "reported"  # third person ("she connects ...") describes, never instructs
    return ""


def _is_path(text: str) -> bool:
    return text[:1] in "\\/~" or (len(text) > 2 and text[1] == ":" and text[2] in "\\/")


def _is_id(tok: _Tok) -> bool:
    """A name the operator could mean as a project / session: a path, or an
    id-shaped word that is not part of the instruction's own vocabulary."""
    if _is_path(tok.text):
        return True
    return tok.low not in _VOCAB and tok.low not in _SESSION_STOP and bool(_ID_RE.match(tok.text))


def parse_move_intents(sentence: str, *, nlp_root: Path | None = None) -> list[dict[str, str]]:
    """The grants ONE sentence states (see the module docstring)."""
    low = sentence.lower()
    if any(p in low for p in _CANCEL_PHRASES):
        return []
    toks = _tokens(sentence)
    if any(t.low in CANCEL_WORDS for t in toks):
        return []
    _annotate(sentence, toks, nlp_root)

    out: list[dict[str, str]] = []
    project = ""
    active = False  # a non-negated move verb is in force
    allow = True  # polarity
    session_context = False  # a session spec opened in this scope
    last_negation = -10
    i = 0
    n = len(toks)

    def _emit(tool: str, target: str, scope: str | None = None) -> None:
        grant = {"tool": tool, "target": target}
        if scope is not None:
            grant["project"] = scope
        if grant not in out:
            out.append(grant)

    while i < n:
        tok = toks[i]
        w = tok.low
        if w in NEGATION_WORDS:
            allow, last_negation = False, i
            i += 1
            continue
        if w in EXCLUDE_WORDS or (i + 1 < n and (w, toks[i + 1].low) in EXCLUDE_BIGRAMS):
            allow = False
            i += 2 if (i + 1 < n and (w, toks[i + 1].low) in EXCLUDE_BIGRAMS) else 1
            if i < n and toks[i].low == "for":  # "except for"
                i += 1
            continue
        verb = _move_verb(tok)
        if verb == "reported":
            i += 1
            continue
        if verb == "instruct":
            if i - last_negation > 3:
                allow = True  # a fresh, non-negated instruction
            active = True
            i += 1
            continue
        if not active:
            i += 1
            continue
        if w in PROJECT_NOUNS:
            j = i + 1
            while j < n and toks[j].low in FILLER_WORDS:
                j += 1
            if j < n and _is_id(toks[j]):
                if allow:
                    project = toks[j].text.rstrip(".,;:!)")
                    _emit("ai_project", project)
                    session_context = False
                else:
                    project = ""  # a negated project opens no scope
                i = j + 1
                continue
        if w in WILDCARD_WORDS:
            j = i + 1
            while j < n and toks[j].low in _WILDCARD_SKIP:
                j += 1
            if j < n and toks[j].low in SESSION_NOUNS:
                if allow:
                    _emit("ai_session", "*", project)
                session_context = True
                i = j + 1
                continue
        if w in SESSION_NOUNS:
            session_context = True
            # One id after "session"; a LIST only after "sessions" ("sessions a
            # and b") -- so "switch to session s1 and keep going" names s1 only.
            plural = w in _PLURAL_SESSION_NOUNS
            named = 0
            j = i + 1
            while j < n:
                t = toks[j]
                if t.low in _SESSION_STOP:
                    break
                if t.low in FILLER_WORDS or (named and plural and t.low in CONNECTORS):
                    j += 1
                    continue
                if not _is_id(t) or (named and not plural):
                    break
                _emit(
                    "ai_session" if allow else "ai_session_deny", t.text.rstrip(".,;:!)"), project
                )
                named += 1
                j += 1
            i = j
            continue
        if not allow and session_context and _is_id(tok):
            # "any other session but y", "... don't connect to y"
            _emit("ai_session_deny", tok.text.rstrip(".,;:!)"), project)
        i += 1
    return out
