"""Layered language detection.

Resolution order (deterministic, every step audit-logged):
  1. Caller override (caller passed language=...)
  2. Operator setting (nlp.preferred_language) if not "auto"
  3. Project setting (nlp.project_language) if non-empty
  4. lingua-py classifier on prompt text — ONLY when lingua is
     installed (it's optional; pulled in with non-English packs
     via the dashboard, not bundled at baseline).
  5. English fallback

King doctrine 2026-05-13: lingua-language-detector (291 MB) dropped
from the baseline pyproject install. AIDOCS ships English-only by
default; auto-detection of other languages activates once the
operator installs additional packs.
"""

from __future__ import annotations

from collections.abc import Iterable

# Confidence threshold below which we ignore lingua's guess and fall
# back. lingua scores 0..1; 0.55 is a balance between "good enough"
# and avoiding mis-classification on short prompts.
_LINGUA_MIN_CONFIDENCE = 0.55


def detect_language(
    text: str,
    *,
    caller_override: str | None = None,
    operator_preferred: str = "auto",
    project_pinned: str = "",
    supported_languages: Iterable[str] = ("en",),
) -> tuple[str, str]:
    """Return (language_iso, source) where source is one of:
    'caller' | 'operator' | 'project' | 'lingua' | 'fallback'.

    Telemetry uses `source` to audit decisions and detect drift
    (e.g. lingua picking wrong language repeatedly).
    """
    supported = frozenset(supported_languages)

    if caller_override and caller_override in supported:
        return (caller_override, "caller")
    if operator_preferred and operator_preferred != "auto":
        if operator_preferred in supported:
            return (operator_preferred, "operator")
    if project_pinned and project_pinned in supported:
        return (project_pinned, "project")

    guess = _lingua_detect(text)
    if guess and guess in supported:
        return (guess, "lingua")

    return ("en", "fallback")


def _lingua_detect(text: str) -> str | None:
    """Run lingua. Returns None when lingua isn't installed (the
    baseline case) or confidence is too low.
    """
    if not text or not text.strip():
        return None
    detector = _detector()
    if detector is None:
        return None
    try:
        result = detector.compute_language_confidence_values(text)
        if not result:
            return None
        top = result[0]
        if top.value < _LINGUA_MIN_CONFIDENCE:
            return None
        return top.language.iso_code_639_1.name.lower()
    except Exception:
        return None


_DETECTOR_CACHE: dict[str, object] = {"obj": None, "tried": False}


def _detector():
    """Lazy-build the lingua detector. Returns None when lingua isn't
    installed (the baseline case). The 'tried' flag prevents repeated
    import attempts when the package is absent.
    """
    if _DETECTOR_CACHE.get("obj") is not None:
        return _DETECTOR_CACHE["obj"]
    if _DETECTOR_CACHE.get("tried"):
        return None
    _DETECTOR_CACHE["tried"] = True
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError:
        return None
    # All languages spaCy supports — keep lingua's classifier scoped
    # so it doesn't bias toward exotic options we can't analyze anyway.
    #
    # Doctrine 2026-05-29: lingua-language-detector 2.0.0 split the
    # single Language.NORWEGIAN enum into BOKMAL + NYNORSK (the two
    # written forms of Norwegian, ~85%/15% usage split). We adapt
    # rather than pin: include both new forms when present, fall back
    # to the legacy NORWEGIAN when the venv has an older lingua. This
    # keeps the gate's NLP layer working across the version transition
    # window — the failure mode the deploy script's Gate 2b VPS run
    # caught on 2026-05-29.
    base_langs = [
        Language.ENGLISH,
        Language.ITALIAN,
        Language.SPANISH,
        Language.PORTUGUESE,
        Language.GERMAN,
        Language.FRENCH,
        Language.ROMANIAN,
        Language.DUTCH,
        Language.POLISH,
        Language.RUSSIAN,
        Language.UKRAINIAN,
        Language.JAPANESE,
        Language.CHINESE,
        Language.KOREAN,
        Language.SWEDISH,
        Language.DANISH,
        Language.FINNISH,
        Language.CROATIAN,
        Language.CATALAN,
        Language.GREEK,
        Language.LITHUANIAN,
        Language.MACEDONIAN,
        Language.SLOVENE,
    ]
    # Norwegian: prefer the post-2.0 split; fall back to the legacy
    # combined enum when running against pre-2.0 lingua.
    for _norw in ("BOKMAL", "NYNORSK", "NORWEGIAN"):
        _val = getattr(Language, _norw, None)
        if _val is not None:
            base_langs.append(_val)
    langs = base_langs
    detector = LanguageDetectorBuilder.from_languages(*langs).build()
    _DETECTOR_CACHE["obj"] = detector
    return detector
