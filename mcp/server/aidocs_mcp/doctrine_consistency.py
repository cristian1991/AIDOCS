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
