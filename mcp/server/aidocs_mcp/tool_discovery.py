"""Deprecated 2026-04-23.

Regex-dict prompt→tool hinter superseded by the lemma-based stack
(lingua + simplemma + rapidfuzz) now shipped by default. Shim kept
so stale imports raise clearly instead of 404.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolHint:
    tool: str
    why: str


def discover_relevant_tools(*_a, **_k) -> list[ToolHint]:
    return []


def format_hints(*_a, **_k) -> str:
    return ""
