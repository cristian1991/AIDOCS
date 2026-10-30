"""Pending 💾 capture-hint store + rail surfacing (#9 auto-surfacing slice).

When conversation/prompt content classifies as DECLARATIVE durable knowledge
(rule / decision / preference / invariant — see
``consumers/durable_content.py``), a pending hint lands here and the
universal notification injector (``notification_injector.
_collect_notification_blocks``) surfaces a terse "record as durable?" block
on the tool-output rail until the hint burns out.

Lifecycle (mirrors run_notifications max_displays semantics):
  * ``observe_content`` — classify; on detection enqueue ONE pending hint per
    unique content (sha256 ledger: the same content never hints twice, EVER —
    including after its hint auto-dropped). Automated harness content
    ([SYSTEM NOTIFICATION...], <task-notification>, <system-reminder>,
    Stop hook feedback) NEVER enqueues — the #325 lesson: automated turns
    must not mine false nudges.
  * ``surface_pending`` — peek + bump surfaced_count + auto-drop at
    ``_MAX_SURFACES`` (2). Session-scoped: only the owning session sees it.
  * ``format_hint_block`` — the 💾 rail block: names the detected snippet
    (<=120 chars) and suggests ``memory_capture`` with the detected kind.

Storage: one JSON file per project (.MEMORY/.index/durable_capture_hints.json)
holding {"pending": [...], "seen": [hashes]} — same neighborhood as
run_notifications.jsonl.

FAIL-QUIET everywhere: any classifier/store error returns the no-op value.
A broken hint layer must never break a tool call or a prompt turn.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "observe_content",
    "pending_for_session",
    "surface_pending",
    "format_hint_block",
]

_STORE_FILENAME = "durable_capture_hints.json"
_STORE_LOCK = threading.Lock()
_MAX_SURFACES = 2  # conductor decision: max 2 surfaces, then auto-drop
_SNIPPET_DISPLAY_CAP = 120

# Automated-content guard — reuse update_intent_hook's detector; keep a
# byte-identical mirror as fallback so an import hiccup fails CLOSED for
# automated markers (guard still works) instead of open.
_AUTOMATED_MARKERS: tuple[str, ...] = (
    "[SYSTEM NOTIFICATION - NOT USER INPUT]",
    "<task-notification>",
    "<system-reminder>",
    "Stop hook feedback",
    "This is an automated",
)


def _is_automated_content(text: str | None) -> bool:
    try:
        from ..update_intent_hook import _is_automated_prompt

        return _is_automated_prompt(text)
    except Exception:
        if not text:
            return False
        return any(m in text for m in _AUTOMATED_MARKERS)


def _store_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / _STORE_FILENAME


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _content_hash(snippet: str) -> str:
    normalized = re.sub(r"\s+", " ", (snippet or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load(project_root: Path) -> dict[str, Any]:
    try:
        raw = json.loads(_store_path(project_root).read_text(encoding="utf-8"))
        pending = raw.get("pending")
        seen = raw.get("seen")
        return {
            "pending": pending if isinstance(pending, list) else [],
            "seen": seen if isinstance(seen, list) else [],
        }
    except Exception:
        return {"pending": [], "seen": []}


def _save(project_root: Path, state: dict[str, Any]) -> None:
    path = _store_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def observe_content(
    project_root: Path,
    session_id: str,
    text: str,
    *,
    nlp=None,
) -> bool:
    """Classify ``text``; enqueue a pending 💾 hint on durable detection.

    Returns True only when a NEW hint was enqueued. Fail-quiet: any error
    returns False and breaks nothing.
    """
    try:
        if not session_id or not text or not text.strip():
            return False
        if _is_automated_content(text):
            return False  # #325: automated turns never mine nudges
        from .consumers.durable_content import detect_durable_content

        proposal = detect_durable_content(text, nlp=nlp)
        if not proposal.detected:
            return False
        digest = _content_hash(proposal.snippet)
        with _STORE_LOCK:
            state = _load(project_root)
            if digest in state["seen"] or any(
                h.get("hash") == digest for h in state["pending"]
            ):
                return False  # once-per-content — ever
            state["pending"].append(
                {
                    "hash": digest,
                    "session_id": session_id,
                    "snippet": proposal.snippet,
                    "kind": proposal.kind or "fact",
                    "confidence": proposal.confidence,
                    "signals": list(proposal.signals),
                    "surfaced_count": 0,
                    "created_at": _now(),
                }
            )
            state["seen"].append(digest)
            _save(project_root, state)
        return True
    except Exception:
        return False


def pending_for_session(project_root: Path, session_id: str) -> list[dict[str, Any]]:
    """Peek (no bump, no drop). Empty session_id returns []. Fail-quiet."""
    try:
        if not session_id:
            return []
        return [
            dict(h)
            for h in _load(project_root)["pending"]
            if str(h.get("session_id") or "") == session_id
        ]
    except Exception:
        return []


def surface_pending(
    project_root: Path,
    *,
    session_id: str,
    max_surfaces: int = _MAX_SURFACES,
) -> list[dict[str, Any]]:
    """Peek + bump surfaced_count + auto-drop at ``max_surfaces``.

    Mirrors run_notifications.surface_for_session: each call that RETURNS a
    hint counts as one surface; a hint that reaches the cap is dropped from
    pending (its hash stays in the seen-ledger, so the content never hints
    again). Empty session_id returns []. Fail-quiet.
    """
    try:
        if not session_id:
            return []
        with _STORE_LOCK:
            state = _load(project_root)
            surfaced: list[dict[str, Any]] = []
            kept: list[dict[str, Any]] = []
            for hint in state["pending"]:
                if str(hint.get("session_id") or "") != session_id:
                    kept.append(hint)
                    continue
                hint["surfaced_count"] = int(hint.get("surfaced_count") or 0) + 1
                surfaced.append(dict(hint))
                if hint["surfaced_count"] < max_surfaces:
                    kept.append(hint)  # below cap — stays queued
                # at/over cap: dropped from pending; hash stays in seen
            if surfaced:
                state["pending"] = kept
                _save(project_root, state)
            return surfaced
    except Exception:
        return []


def format_hint_block(hints: list[dict[str, Any]]) -> str:
    """Terse 💾 rail block. One line per hint; snippet capped at 120 chars."""
    lines = []
    for hint in hints:
        snippet = str(hint.get("snippet") or "")
        if len(snippet) > _SNIPPET_DISPLAY_CAP:
            snippet = snippet[: _SNIPPET_DISPLAY_CAP - 1] + "…"
        kind = str(hint.get("kind") or "fact")
        lines.append(
            f"💾 Durable-content hint — looks like a {kind} worth keeping: "
            f"{snippet!r}. If so: memory_capture(kind={kind!r}, content=...); "
            f"if not, ignore (auto-drops after {_MAX_SURFACES} surfaces)."
        )
    return "\n".join(lines)
