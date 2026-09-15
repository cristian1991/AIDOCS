"""Directive-command vs descriptive-report classifier for the preflight tamper
gate.

Malicious operator prompts are IMPERATIVE: they tell the agent to DO something
("disable the gate", "bypass the freeze"). Security / triage REPORTS are
status-shaped: they DESCRIBE state ("the controller skips the check", "a caller
can bypass it", "the permission check is disabled in code"). The regex tamper
rules cannot tell them apart and false-positive on reports (operator-confirmed:
a subagent triage report froze a session).

This uses the spaCy Doc parse (verb morphology + subject dependency) to decide.
CONSERVATIVE + FAIL-CLOSED: a trigger verb counts as a report ONLY when it has a
3rd-person nominal subject and is neither imperative nor 2nd-person. Anything
imperative / 2nd-person / subjectless / pronoun-subject / unparsed /
NLP-unavailable stays a COMMAND, so the gate is never weakened on uncertainty.
"""

from __future__ import annotations

_SUBJECT_DEPS = frozenset({"nsubj", "nsubj:pass", "nsubjpass", "csubj"})
_SECOND_PERSON = frozenset({"you", "u", "ya"})
_NOUN_POS = frozenset({"NOUN", "PROPN"})

# Tamper rules whose regex match is verb-context-ambiguous (command vs report).
# Only these verdicts are eligible for the descriptive-report drop.
_TAMPER_RULE_IDS = frozenset(
    {
        "PREFLIGHT_GATE_TAMPER_FORBIDDEN",
        "PREFLIGHT_GATE_BYPASS_FORBIDDEN",
        "PREFLIGHT_INTERNALS_DIRECT_WRITE_FORBIDDEN",
    }
)
# Lemmas the tamper rules key on (_DISABLE_VERB + _MUTATE_VERB + _DIRECT_WRITE).
_TAMPER_TRIGGER_LEMMAS = frozenset(
    {
        "disable", "suppress", "unload", "remove", "skip", "bypass", "circumvent",
        "kill", "patch", "edit", "modify", "change", "rewrite", "update", "set",
        "pin", "rotate", "switch", "backfill", "overwrite", "flip", "write",
        "delete", "insert", "turn",
    }
)


def all_trigger_verbs_are_report(prompt, trigger_lemmas, service) -> bool:
    """True ONLY when EVERY occurrence of a trigger verb is a descriptive report
    clause (3rd-person nominal subject, non-imperative). False on any imperative
    / 2nd-person / subjectless / pronoun-subject verb, and False when the parse
    is unavailable — fail-closed, never weakens the gate on uncertainty."""
    if service is None:
        return False
    try:
        doc = service.analyze(prompt)
    except Exception:
        return False
    if doc is None:
        return False
    if "dep" not in (getattr(doc, "capabilities", None) or frozenset()):
        return False
    toks = getattr(doc, "tokens", None) or ()
    trigger_idxs = [
        i
        for i, t in enumerate(toks)
        if (t.lemma or "").lower() in trigger_lemmas
        or (t.text or "").lower() in trigger_lemmas
    ]
    if not trigger_idxs:
        return False
    for i in trigger_idxs:
        t = toks[i]
        if (t.morph or {}).get("Mood") == "Imp":
            return False  # imperative -> command
        subs = [s for s in toks if s.head_idx == i and s.dep in _SUBJECT_DEPS]
        if not subs:
            return False  # subjectless -> bare imperative -> command
        has_noun_subject = False
        for s in subs:
            if (s.text or "").lower() in _SECOND_PERSON or (s.morph or {}).get("Person") == "2":
                return False  # 2nd-person directive
            if s.pos in _NOUN_POS:
                has_noun_subject = True
        if not has_noun_subject:
            return False  # pronoun subject (I/we) -> conservative command
    return True


def drop_descriptive_tamper(prompt, verdicts, *, project_root=None, service=None):
    """Drop tamper-class verdicts when spaCy confirms their trigger verbs are all
    descriptive reports (not imperative commands). Conservative + fail-closed:
    returns the verdict list UNCHANGED when there are no tamper verdicts, when
    the NLP service is unavailable, or when any trigger verb is command-shaped."""
    tamper = [v for v in verdicts if v.rule_id in _TAMPER_RULE_IDS]
    if not tamper:
        return verdicts
    if service is None:
        if project_root is None:
            return verdicts  # cannot build the service -> fail-closed
        try:
            from ..service import get_service

            service = get_service(project_root)
        except Exception:
            return verdicts
    if not all_trigger_verbs_are_report(prompt, _TAMPER_TRIGGER_LEMMAS, service):
        return verdicts
    return [v for v in verdicts if v.rule_id not in _TAMPER_RULE_IDS]
