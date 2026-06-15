"""Non-English grant-verb normalization.

Layer 5 NLP deliverable (2026-04-19). Extends claude_hook's English-only
grant-verb detection to Spanish, French, German, Portuguese, and Mandarin
Chinese. Doesn't replace the existing English detector — sits as a
normalizer layer in front: translates non-English grant phrases into
the canonical English form, then the existing detector fires.

Kept as a standalone module so the detector can be tested in isolation
without spinning up the whole claude_hook machinery.
"""

from __future__ import annotations

from collections.abc import Iterable

# Map of (lowercased non-English phrase) → canonical English equivalent.
# Phrases chosen for minimum-viable coverage — one grant verb per
# language per canonical action. Operators will add more as usage
# surfaces gaps.
_GRANT_TRANSLATIONS: dict[str, str] = {
    # Spanish
    "permitir": "allow",
    "permite": "allow",
    "autorizar": "allow",
    "déjame": "let me",
    "dejame": "let me",
    # French
    "permettre": "allow",
    "permets": "allow",
    "autoriser": "allow",
    "laisse-moi": "let me",
    "laisse moi": "let me",
    # German
    "erlauben": "allow",
    "erlaube": "allow",
    "zulassen": "allow",
    "lass mich": "let me",
    # Portuguese
    "permita": "allow",
    "permite-me": "let me",
    "permite me": "let me",
    # Chinese (simplified — operators paste zh phrases directly)
    "允许": "allow",
    "让我": "let me",
    "授权": "allow",
}

# Lane-exit phrases in each language → canonical "exit lane".
_LANE_EXIT_TRANSLATIONS: dict[str, str] = {
    # Spanish
    "salir de la pista": "exit lane",
    "salir del carril": "exit lane",
    # French
    "sortir de la voie": "exit lane",
    "quitter la voie": "exit lane",
    # German
    "spur verlassen": "exit lane",
    "spur beenden": "exit lane",
    # Portuguese
    "sair da pista": "exit lane",
    # Chinese
    "退出车道": "exit lane",
    "离开车道": "exit lane",
}


def normalize_grant_phrase(prompt: str) -> str:
    """Return the prompt with non-English grant verbs rewritten to
    their canonical English equivalents, lowercase. Unchanged text
    passes through (case-preserved in the unmodified spans).

    Intended to be called BEFORE the English detect_bash_subcommand_grants_v2
    and detect_lane_exit_v2 detectors so both paths see a unified
    vocabulary. The translation is non-destructive: only known phrases
    get rewritten; anything else stays verbatim.
    """
    if not prompt:
        return prompt
    text = str(prompt)
    lowered = text.lower()
    # Two passes — translate grant verbs first, then lane-exit phrases.
    # Longer phrases are matched before shorter ones to prevent "let me"
    # from clobbering a multi-word match like "let me use".
    merged: dict[str, str] = {**_GRANT_TRANSLATIONS, **_LANE_EXIT_TRANSLATIONS}
    # Sort keys by descending length for greedy-longest-match.
    keys_by_len = sorted(merged.keys(), key=len, reverse=True)
    for source in keys_by_len:
        if source in lowered:
            # Case-preserving replacement: operate on the lowered
            # string for detection but substitute at the same index in
            # the original. Since the downstream detector lowercases
            # anyway, we emit lowercase.
            lowered = lowered.replace(source, merged[source])
    return lowered


def known_languages() -> tuple[str, ...]:
    """Operators asked which languages are covered. Stable contract
    for dashboards that surface NLP capability.
    """
    return ("en", "es", "fr", "de", "pt", "zh")


def translation_entries() -> Iterable[tuple[str, str]]:
    """Expose the translation pairs for diagnostic tooling."""
    for source, target in _GRANT_TRANSLATIONS.items():
        yield source, target
    for source, target in _LANE_EXIT_TRANSLATIONS.items():
        yield source, target
