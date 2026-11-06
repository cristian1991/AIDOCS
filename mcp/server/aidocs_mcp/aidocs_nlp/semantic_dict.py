"""Semantic categories: lemma → intent class mappings.

Co-conductor 2026-05-12 correction (CRITICAL): these categories are
for INTENT DETECTION, not for file-write enforcement. The chain is
strictly upstream-to-downstream:

    NLP detector consults this dict → emits grant signal
                                   → grant lands in sqlite
                                   → access_gate reads grant
                                   → access_gate decides allow/deny

No code in this module touches file I/O, sentinels, or write policy.
Cross-wiring would defeat the layering.

Categories are language-neutral keys; their values are lemmas in the
form spaCy's loaded language model produces. spaCy's lemmatizer handles
morphology per-language. For languages where lemmatization can't
generalize across cultures (notably profanity), per-language sets are
isolated to that one category.
"""

from __future__ import annotations

# Lemma → category. Single-token lemmas only; multi-word phrases go
# in the consumer's own dispatch (the consumer composes them from token
# sequences).
SEMANTIC_CATEGORIES: dict[str, frozenset[str]] = {
    # ── DNT / file protection ──
    "protect_verb": frozenset(
        {
            "protect",
            "lock",
            "guard",
            "seal",
            "shield",
            # Italian (Empire's boss language).
            "proteggere",
            "bloccare",
        },
    ),
    "unprotect_verb": frozenset(
        {
            "unprotect",
            "unlock",
            "release",
            "sproteggere",
            "sbloccare",
        },
    ),
    "override_verb": frozenset(
        {
            "override",
            "bypass",
            "skip",
            "ignore",
            "ignorare",
            "saltare",
        },
    ),
    # ── Mutation verbs (used by intent guard + edit detector) ──
    "edit_verb": frozenset(
        {
            "edit",
            "change",
            "modify",
            "update",
            "fix",
            "patch",
            "refactor",
            "modificare",
            "cambiare",
            "aggiornare",
            "correggere",
        },
    ),
    "create_verb": frozenset(
        {
            "create",
            "add",
            "make",
            "build",
            "scaffold",
            "generate",
            "creare",
            "aggiungere",
            "generare",
        },
    ),
    "destroy_verb": frozenset(
        {
            "delete",
            "remove",
            "drop",
            "wipe",
            "destroy",
            "kill",
            "purge",
            "eliminare",
            "rimuovere",
            "cancellare",
        },
    ),
    "execute_verb": frozenset(
        {
            "run",
            "execute",
            "test",
            "build",
            "deploy",
            "eseguire",
            "lanciare",
        },
    ),
    # ── Grant grammar ──
    "approve_verb": frozenset(
        {
            "allow",
            "permit",
            "grant",
            "approve",
            "authorize",
            "enable",
            "permettere",
            "autorizzare",
            "consentire",
        },
    ),
    "deny_verb": frozenset(
        {
            "deny",
            "block",
            "refuse",
            "forbid",
            "reject",
            "disallow",
            "negare",
            "bloccare",
            "rifiutare",
            "vietare",
        },
    ),
    # ── Grammatical markers ──
    "second_person": frozenset(
        {
            "you",
            "yourself",
            "tu",
            "voi",
            "te",
        },
    ),
    "first_person": frozenset(
        {
            "i",
            "me",
            "myself",
            "we",
            "us",
            "our",
            "io",
            "noi",
        },
    ),
    "negation": frozenset(
        {
            "not",
            "never",
            "no",
            "none",
            "nothing",
            "non",
            "mai",
            "nessuno",
            "niente",
        },
    ),
    # ── Tone signals (lexical layer for tone_detector) ──
    # Profanity stems kept per-language because cultural/linguistic
    # boundary is what matters here, not morphological inflection.
    # Lemmatization wouldn't help — "merda" and "shit" are different
    # lemmas in different languages; the category is the bridge.
    "profanity": frozenset(
        {
            # English
            "fuck",
            "shit",
            "damn",
            "bullshit",
            "asshole",
            "bitch",
            "bastard",
            "wtf",
            "stfu",
            "retard",
            "moron",
            "dumbass",
            # Italian
            "cazzo",
            "merda",
            "stronzo",
            "vaffanculo",
            "minchia",
            "puttana",
            # Spanish
            "mierda",
            "joder",
            "puta",
            "carajo",
            "pendejo",
            # Portuguese
            "porra",
            "caralho",
            "fuder",
            # German
            "scheisse",
            "verdammt",
            "arschloch",
            # Romanian
            "pula",
            "pizda",
            "muie",
            "rahat",
            # French
            "putain",
            "merde",
            "connard",
        },
    ),
    "anger_marker": frozenset(
        {
            "wtf",
            "stfu",
            "ugh",
            "stop",
            "fermare",
            "para",
        },
    ),
    # ── DNT gate phrases (literal multi-word substrings) ──
    # NOT lemmas — these are cultural surface forms detected as raw
    # substrings by consumers. Listed here so the doctrine "all
    # semantic categories live in semantic_dict" holds.
    # Consumers do `if phrase in text.lower()`, not token lookups.
}


DNT_GATE_PHRASES: tuple[str, ...] = (
    # English
    "do not touch",
    "do-not-touch",
    "don't touch",
    "dont touch",
    "do not edit",
    "do not modify",
    # Italian
    "non toccare",
    "non modificare",
    # Spanish
    "no tocar",
    "no modificar",
    # Portuguese
    "nao tocar",
    "não tocar",
    # German
    "nicht anfassen",
    "nicht berühren",
    "nicht beruhren",
    # Romanian
    "nu atinge",
    "nu modifica",
    # French
    "ne pas toucher",
    "ne pas modifier",
)


EDIT_COMPLAINT_PHRASES: tuple[str, ...] = (
    # English
    "why did you edit",
    "why did you touch",
    "why did you change",
    "why did you modify",
    "why the fuck did you",
    "who told you to",
    "did i tell you to",
    "did i ask you to",
    "leave it alone",
    "leave that alone",
    "stop touching",
    "stop editing",
    "stop changing",
    "you shouldn't have",
    "you should not have",
    # Italian
    "perche hai modificato",
    "chi ti ha detto",
    "lascia stare",
    # Spanish
    "por que editaste",
    "quien te dijo",
    "deja eso",
    # Portuguese
    "por que editou",
    "quem mandou",
    "deixa quieto",
    # German
    "warum hast du",
    "wer hat dir gesagt",
    "lass das",
    # Romanian
    "de ce ai editat",
    "cine ti-a spus",
    "lasa in pace",
    # French
    "pourquoi as-tu",
)


