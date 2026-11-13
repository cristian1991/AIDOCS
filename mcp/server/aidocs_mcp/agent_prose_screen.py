"""Screen agent-produced prose on its way to a NON-FILE sink (#684).

The existing mojibake defences all guard the FILE boundary:
``file_ops._detect_mojibake`` refuses writes, ``scripts/check_mojibake.py``
scans file contents pre-commit, ``skill_store`` repairs on skill serve.

Commit messages, backlog bodies, issue filings, memory captures, palace
bodies and task summaries are prose an agent authored that NEVER touches a
file those guards scan. Mojibake an agent copied out of a subprocess echo
landed in them unscreened — which is exactly how a double-encoded em-dash
reached a git commit message.

REPAIR, not refuse. Refusing would wedge an agent mid-task, and the
operator's ruling on the trade is empirical rather than a preference: nobody
types the mojibake byte sequences by hand, and the one context where they
appear legitimately — code that operates ON mojibake — is already handled by
``file_ops``'s self-exemption. ``fix_mojibake`` is non-destructive by
construction: it repairs only when the cp1252/UTF-8 round trip removes EVERY
signature, so it can subtract mojibake but never introduce it.

SIGNATURES ONLY, never a general non-ASCII screen. The operator writes
Romanian and Italian; Romanian lemmas carrying U+0103 must pass untouched.

This module holds NO signature literals of its own — it delegates to
``file_ops.fix_mojibake`` — so it needs no entry in the self-exemption
(test_mojibake_self_exempt.py stays as it is).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def repair_agent_prose(text, *, sink: str):
    """Return ``text`` with mojibake repaired, logging any repair.

    The repair is AUDITABLE, not silent: a repair emits a WARNING naming the
    sink. Auditing is best-effort and never blocks the write. Non-strings and
    empty strings pass straight through, so this is safe to drop in front of
    an optional field.
    """
    if not isinstance(text, str) or not text:
        return text
    from .file_ops import fix_mojibake

    repaired = fix_mojibake(text)
    if repaired != text:
        try:
            logger.warning(
                "mojibake repaired at sink %r (#684): agent prose was "
                "double-encoded UTF-8; %d chars in, %d out",
                sink,
                len(text),
                len(repaired),
            )
        except Exception:  # noqa: BLE001 — auditing must never block the sink
            pass
    return repaired


def repair_agent_prose_deep(value, *, sink: str, _depth: int = 0):
    """``repair_agent_prose`` over a nested dict/list/tuple payload.

    For structured sinks whose prose is nested rather than a bare string —
    task ``verification_evidence``, ``tool_report`` entries. Dict KEYS are
    left alone: they are field names, not prose, and rewriting one would
    change the payload's shape. Depth is bounded so a pathological payload
    cannot recurse without end.
    """
    if _depth > 8:
        return value
    if isinstance(value, str):
        return repair_agent_prose(value, sink=sink)
    if isinstance(value, dict):
        return {k: repair_agent_prose_deep(v, sink=sink, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [repair_agent_prose_deep(v, sink=sink, _depth=_depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(repair_agent_prose_deep(v, sink=sink, _depth=_depth + 1) for v in value)
    return value
