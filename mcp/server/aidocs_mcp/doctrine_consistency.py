"""Doctrine corpus integrity — no dangling cross-references.

A doctrine scroll that says "specifics live in `X`" / "Pair with `X`" where `X`
is not a real skill is a HALF-MIGRATION — the exact failure empire-doctrine §XII
("migrate without orphaning") forbids. This validator catches that class so a
future doctrine rename cannot leave a dangling pointer behind.

Origin: the `emperor-doctrine` ghost (2026-07-01) — `empire-doctrine` pointed
twice at an `emperor-doctrine` skill that no longer existed, and `castle-doctrine`
duplicated `empire-doctrine`. This guard locks the consolidated corpus.
"""

from __future__ import annotations

import re

# Phrases in doctrine prose that introduce a doctrine -> skill cross-reference.
# Kept deliberately narrow (backtick-quoted target after a "lives in / pair with"
# lead-in) so ordinary backticked tokens (tool names, types) are NOT treated as
# doctrine references.
_REF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:specifics?|specs?|implementation)[^`\n]{0,40}?live[^`\n]{0,12}?in\s+`([a-z0-9][a-z0-9-]+)`", re.I),
    re.compile(r"\bPair with\s+`([a-z0-9][a-z0-9-]+)`", re.I),
    re.compile(r"\blives? in\s+`([a-z0-9][a-z0-9-]+)`", re.I),
)


def find_dangling_doctrine_refs(skills: dict[str, str]) -> list[tuple[str, str]]:
    """Return ``(source_skill_id, missing_target)`` for every doctrine cross-
    reference whose target skill is absent from ``skills``. Empty list == the
    doctrine corpus is internally consistent (no half-migration)."""
    present = set(skills)
    dangling: list[tuple[str, str]] = []
    for sid, content in skills.items():
        seen: set[str] = set()
        for pat in _REF_PATTERNS:
            for m in pat.finditer(content or ""):
                target = m.group(1)
                if target == sid or target in present or target in seen:
                    continue
                seen.add(target)
                dangling.append((sid, target))
    return dangling


# ── §-level section refs (backlog #227) ──────────────────────────────────────
#
# The emperor-doctrine ghost's second face: the scroll was renamed/renumbered
# and ~15 code comments kept citing sections (§XIII–§XVII) the migration had
# dropped. A `<scroll>-doctrine §N` reference — in doctrine prose or in a code
# comment — must point at a numbered section that actually exists.

# `## XII. Title` — the numbered-section heading shape both scrolls use
# (same shape test_doctrine_scroll_sync pins for the v5 manual).
_SECTION_HEADING = re.compile(r"^##\s+([IVXLCDM]+)\.", re.M)

# `<name>-doctrine §N` plus continuation refs (`§X/§XI`, `§XI ..., §XV`).
# Line-scoped token stream: scroll names and `§N` tokens. Each `§N` on a line
# is attributed to the MOST RECENT scroll named earlier on that same line, so
# continuation shapes (`§X/§XI`, `§XI memory triggers, §XV evidence`) are each
# checked, while a bare `§N` with no scroll on its line is ignored.
_REF_TOKEN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9-]*-doctrine)\b|§\s*([IVXLCDM]+)\b",
)


def parse_scroll_sections(content: str) -> set[str]:
    """Roman numerals of every ``## <ROMAN>. Title`` heading in a scroll."""
    return {m.group(1) for m in _SECTION_HEADING.finditer(content or "")}


def find_dangling_section_refs(
    text: str,
    scroll_sections: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """Return ``(scroll_id, section)`` for every ``<scroll>-doctrine §N``
    reference in ``text`` whose scroll is known to ``scroll_sections`` but
    lacks that numbered section. References to scrolls not present in
    ``scroll_sections`` are ignored — they are not this guard's business.
    Empty list == every cited section exists."""
    dangling: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        current: str | None = None
        for m in _REF_TOKEN.finditer(line):
            scroll, sec = m.group(1), m.group(2)
            if scroll is not None:
                name = scroll.lower()
                current = name if name in scroll_sections else None
                continue
            if current is not None and sec not in scroll_sections[current]:
                dangling.append((current, sec))
    return dangling
