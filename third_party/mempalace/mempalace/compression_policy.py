"""Never-compress policy for AAAK dialect compression.

RFC 003 §16: certain categories of text MUST NOT be compressed via AAAK
because the compression is lossy and these categories require verbatim
preservation in the audit chain or downstream gate decisions.

This module is the pattern-matching layer. ``Dialect.compress_safe``
in ``dialect.py`` consults it before applying AAAK and routes any
match to a ``CompressionPolicyViolation`` audit event.

The detector is intentionally conservative: false positives (refusing
to compress text that *could* be compressed) are cheap — caller gets
the original. False negatives (compressing text that should have been
preserved) are catastrophic — verbatim user grants become AAAK soup
and the audit chain loses fidelity.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from .conjoined_types import (
    CompressionNeverCategory,
    CompressionPolicyViolation,
)


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------


# 16+ char hex string (sha256[:16] or longer, content_hash, audit_event_id).
_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{16,}\b")

# Approval / grant trigger phrases. Conservative list — expand as
# AIDOCS's intent_grant_detector enumerates more patterns.
_USER_GRANT_PATTERNS = [
    re.compile(r"\b(?:go ahead|do it|proceed|approved?|yes,?\s+(?:do|push|deploy|run))\b", re.IGNORECASE),
    re.compile(r"\b(?:i grant|i authorize|i approve|you (?:can|may)\s+(?:run|push|deploy|delete))\b", re.IGNORECASE),
    re.compile(r"\b(?:stop asking|always allow|never ask)\b", re.IGNORECASE),
]

# Freeze phrases (from AIDOCS freeze_service).
_FREEZE_PATTERNS = [
    re.compile(r"\bconfirm \w+-[0-9a-fA-F]{8}\b"),  # AIDOCS freeze fingerprint format
    re.compile(r"\b(?:i confirm|approve freeze|release freeze)\b", re.IGNORECASE),
]

# Gate verdict markers. When text is tagged as a gate output, never compress.
_GATE_VERDICT_PATTERNS = [
    re.compile(r"\b(?:deny|refuse|block|allow|insert|reject|code_teaches|needs_user_clarification)\b", re.IGNORECASE),
    re.compile(r"\b(?:gate_verdict|verdict|gate_decision)\s*[:=]", re.IGNORECASE),
]

# Audit reason markers.
_AUDIT_REASON_PATTERNS = [
    re.compile(r"\b(?:audit_reason|reason)\s*[:=]", re.IGNORECASE),
    re.compile(r"\b(?:enforcement_disabled_bypass|kill_switch_bypass_used|palace_disabled)\b"),
]

# File paths in enforcement decisions — when a path appears alongside a
# verdict, treat the whole text as preserve-verbatim.
_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]|[\\/](?:src|tests?|docs?|mcp|mempalace|aidocs)[\\/]")

# Contradiction summaries.
_CONTRADICTION_PATTERNS = [
    re.compile(r"\bContradictionEvent\b"),
    re.compile(r"\b(?:contradiction|conflict)\s+(?:detected|summary)", re.IGNORECASE),
]


def detect_never_compress(text: str) -> Optional[CompressionNeverCategory]:
    """Return the category the text matches, or None if compression is allowed.

    Order is informative but not strict — the first match returns.
    Categories are checked roughly in order of "obvious-policy-signal"
    so a phrase that's both a user grant AND contains a hash returns
    user_grant (the more semantic category).
    """
    if not text:
        return None

    # User grants first — most semantic.
    for pattern in _USER_GRANT_PATTERNS:
        if pattern.search(text):
            return "user_grant"

    # Freeze phrases — also high-semantic-signal.
    for pattern in _FREEZE_PATTERNS:
        if pattern.search(text):
            return "freeze_phrase"

    # Gate verdicts.
    for pattern in _GATE_VERDICT_PATTERNS:
        if pattern.search(text):
            return "gate_verdict"

    # Audit reasons.
    for pattern in _AUDIT_REASON_PATTERNS:
        if pattern.search(text):
            return "audit_reason"

    # Contradiction summaries.
    for pattern in _CONTRADICTION_PATTERNS:
        if pattern.search(text):
            return "contradiction_summary"

    # Hashes (least specific; check last so semantic patterns win).
    if _HEX_PATTERN.search(text):
        return "hash"

    # Enforcement file paths — only refuse if the text ALSO has a verdict
    # marker; a path alone is fine to compress.
    if _PATH_PATTERN.search(text):
        # Already-checked above: gate_verdict / audit_reason patterns.
        # If we're here, the path stands alone — allow compression.
        pass

    return None


def build_violation(
    text: str,
    category: CompressionNeverCategory,
    caller: str,
) -> CompressionPolicyViolation:
    """Construct a CompressionPolicyViolation event record."""

    preview = text[:200] if text else ""
    # Redact obvious secrets in the preview itself — preview should
    # never become a new leak vector.
    preview = _HEX_PATTERN.sub("[hash]", preview)
    return CompressionPolicyViolation(
        event_id=f"cpv-{uuid.uuid4().hex[:16]}",
        detected_at=datetime.now(tz=timezone.utc).isoformat(),
        category=category,
        attempted_input_preview=preview,
        caller=caller,
    )
