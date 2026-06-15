"""aidocs_nlp — the canonical NLP layer for AIDOCS.

King doctrine 2026-05-12: NLP is core, not optional. One door (NLPService),
analyzer-agnostic Doc structure, language packs downloaded on demand via
the dashboard, semantic dictionaries map lemmas to intent categories.

NLP authorizes; access_gate enforces. The semantic_dict entries here
(protect_verb, destroy_verb, etc.) classify intent — they NEVER touch
file-write enforcement. Co-conductor correction 2026-05-12.

Triskeleton: every consumer has both a full-capability path AND a
baseline path that works with zero language packs installed. Tests
assert both.
"""

from __future__ import annotations

from .doc import Doc, Entity, Span, Token
from .language_registry import LanguagePack, PackStatus
from .pipeline import Pipeline
from .service import AnalysisSource, NLPService, NoPipelineAvailable
from .substance import Substance, substance_of

__all__ = [
    "AnalysisSource",
    "Doc",
    "Entity",
    "LanguagePack",
    "NLPService",
    "NoPipelineAvailable",
    "PackStatus",
    "Pipeline",
    "Span",
    "Substance",
    "Token",
    "substance_of",
]
