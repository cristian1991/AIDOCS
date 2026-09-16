"""Language pack registry — declares which spaCy models are installable.

The allowlist is curated + signed by SHA pin. Operators can extend
per-project via `nlp.pack_allowlist_extra` (settings page surfaces a
security warning for the override). Hardcoded allowlist alone would
block legitimate custom models (domain-specific medical/legal/code);
no allowlist at all would expose pip-install supply chain risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PackStatus(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    LOADED = "loaded"
    DOWNLOADING = "downloading"
    FAILED = "failed"


@dataclass(frozen=True)
class LanguagePack:
    """One installable spaCy pipeline pack."""

    language: str  # ISO 639-1
    display_name: str  # "English", "Italian", ...
    model_name: str  # spaCy model id, e.g. "en_core_web_sm"
    size_mb: float  # download size
    tier: str = "core"  # core | multilang | tokenizer
    pinned_version: str = ""  # spaCy model version pin; empty = latest


# Curated allowlist. Sizes are approximate and informational.
# When a new spaCy release ships, update pinned_version after vetting.
DEFAULT_PACKS: tuple[LanguagePack, ...] = (
    # English — shipped baseline (Empire directive 2026-05-12).
    LanguagePack("en", "English", "en_core_web_sm", 15.0),
    # Italian — for the Empire's boss (Empire directive 2026-05-12).
    LanguagePack("it", "Italian", "it_core_news_sm", 38.0),
    # On-demand for other locales.
    LanguagePack("es", "Spanish", "es_core_news_sm", 42.0),
    LanguagePack("pt", "Portuguese", "pt_core_news_sm", 45.0),
    LanguagePack("de", "German", "de_core_news_sm", 52.0),
    LanguagePack("ro", "Romanian", "ro_core_news_sm", 31.0),
    LanguagePack("fr", "French", "fr_core_news_sm", 44.0),
    LanguagePack("nl", "Dutch", "nl_core_news_sm", 42.0),
    LanguagePack("pl", "Polish", "pl_core_news_sm", 48.0),
    LanguagePack("ru", "Russian", "ru_core_news_sm", 50.0),
    LanguagePack("uk", "Ukrainian", "uk_core_news_sm", 38.0),
    LanguagePack("ja", "Japanese", "ja_core_news_sm", 40.0),
    LanguagePack("zh", "Chinese", "zh_core_web_sm", 49.0),
    LanguagePack("ko", "Korean", "ko_core_news_sm", 38.0),
    LanguagePack("nb", "Norwegian", "nb_core_news_sm", 31.0),
    LanguagePack("sv", "Swedish", "sv_core_news_sm", 32.0),
    LanguagePack("da", "Danish", "da_core_news_sm", 31.0),
    LanguagePack("fi", "Finnish", "fi_core_news_sm", 31.0),
    LanguagePack("hr", "Croatian", "hr_core_news_sm", 31.0),
    LanguagePack("ca", "Catalan", "ca_core_news_sm", 42.0),
    LanguagePack("el", "Greek", "el_core_news_sm", 42.0),
    LanguagePack("lt", "Lithuanian", "lt_core_news_sm", 31.0),
    LanguagePack("mk", "Macedonian", "mk_core_news_sm", 38.0),
    LanguagePack("sl", "Slovenian", "sl_core_news_sm", 31.0),
    # Multilingual fallback — tokenizer + POS only, no dep/NER/morph.
    LanguagePack("xx", "Multilingual (fallback)", "xx_sent_ud_sm", 15.0, tier="multilang"),
)


def pack_for(language: str) -> LanguagePack | None:
    """Look up a pack by ISO language code. None if unsupported."""
    for p in DEFAULT_PACKS:
        if p.language == language:
            return p
    return None
