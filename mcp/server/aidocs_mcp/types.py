from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    path: Path
    title: str | None
    status: str | None
    owner: str | None
    goal: str | None
    last_updated: str | None


@dataclass(slots=True)
class SessionData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]


@dataclass(slots=True)
class ContextData:
    session_id: str
    path: Path
    sections: dict[str, list[str]]


@dataclass(slots=True)
class MemoryWriteResult:
    target_file: Path
    content: str


def lines_to_text(lines: Iterable[str]) -> str:
    text = "\n".join(lines).rstrip()
    return text + "\n"
