"""Cursor-based pagination for cross-project code search.

Layer 4 cross-project index slice 1. Large result sets (symbol search
spanning 50+ related projects, 10K+ file bundles) can't be returned
whole — the MCP payload limit bites first. Current code-search path
uses offset+limit which becomes O(N) when the backing index changes
between pages.

This module provides opaque-cursor semantics: the cursor encodes
(sort_key, last_value) as base64 JSON; callers pass the cursor back
to fetch the next page. Stable across index mutations because we
seek-by-value, not seek-by-offset.

Standalone helpers — the callers wire them into existing search
surfaces. No search-path edit in this slice to keep blast radius small.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_CURSOR_VERSION = 1


@dataclass(frozen=True)
class Cursor:
    """Opaque pagination cursor.

    version: schema version; bumps when the payload shape changes so
        stale cursors fail loudly instead of silently misinterpreting.
    sort_key: name of the field rows are sorted on (typically 'path'
        or 'project_id:path' for cross-project).
    last_value: last-seen value in the prior page. Next page is
        everything with sort_key > last_value.
    limit: page size caller requested. Carried inside the cursor so
        pagination stays stable even if the caller forgets to re-send.
    """

    version: int
    sort_key: str
    last_value: str
    limit: int


def encode_cursor(
    sort_key: str,
    last_value: str,
    limit: int,
    version: int = _CURSOR_VERSION,
) -> str:
    """Serialize a cursor into an opaque token.

    Token format: base64url-encoded JSON. Opaque on purpose — callers
    must round-trip the value they received, not manipulate it.
    """
    if not sort_key or not str(sort_key).strip():
        raise ValueError("sort_key must be non-empty")
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")
    payload = {
        "v": int(version),
        "k": str(sort_key),
        "l": str(last_value),
        "n": int(limit),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return token


def decode_cursor(token: str) -> Cursor:
    """Parse an opaque cursor token back to its fields.

    Raises ValueError on any malformed input — padding error, JSON
    error, missing field, wrong type, or version mismatch. Callers
    should catch ValueError and treat it as "start from the beginning".
    """
    if not token or not str(token).strip():
        raise ValueError("empty cursor token")
    # base64 URL-safe decode with auto-padding.
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"cursor b64 decode failed: {exc}") from exc
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"cursor json decode failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("cursor payload must be a dict")
    try:
        version = int(payload["v"])
        sort_key = str(payload["k"])
        last_value = str(payload["l"])
        limit = int(payload["n"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"cursor missing/invalid field: {exc}") from exc
    if version != _CURSOR_VERSION:
        raise ValueError(f"cursor version {version} unsupported (expected {_CURSOR_VERSION})")
    if limit <= 0:
        raise ValueError(f"cursor limit must be > 0, got {limit}")
    return Cursor(
        version=version,
        sort_key=sort_key,
        last_value=last_value,
        limit=limit,
    )


def paginate(
    rows: Iterable[dict[str, Any]],
    sort_key: str,
    limit: int,
    after_value: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Apply cursor-based pagination to a sorted iterable.

    Returns (page_rows, next_cursor_token). next_cursor_token is None
    when the result set is exhausted. Rows must already be sorted
    ascending by sort_key — the seek-by-value contract depends on it.

    Callers compose this with their own SQL/index query: run the query
    with WHERE sort_key > after_value ORDER BY sort_key LIMIT N, pass
    the rows here for wrapping.
    """
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")
    page: list[dict[str, Any]] = []
    # Seek-past-after_value is the caller's job at the query layer; we
    # still guard here so misuse produces a meaningful page rather than
    # silently returning rows the caller has already seen.
    for row in rows:
        if after_value is not None:
            value = row.get(sort_key)
            if value is None or str(value) <= str(after_value):
                continue
        page.append(row)
        if len(page) >= limit:
            break
    next_token: str | None = None
    if len(page) >= limit:
        last = page[-1].get(sort_key)
        if last is not None:
            next_token = encode_cursor(sort_key, str(last), limit)
    return page, next_token
