"""DO NOT TOUCH protection — sentinel detection + pair-files parser.

Spec: .MEMORY/sessions/2026-04-13-repo-and-dashboard-audit/plans/do-not-touch-gate-spec.md

This module is the primitive layer:
- Detecting whether a file is marked protected.
- Parsing its "Pair files:" list from the header.

Callers (index sync, gate, dashboard) use these to populate and consult
the code_files.protected / code_files.pair_files columns.
"""

from __future__ import annotations

import re
from pathlib import Path

# Exact literal phrase — case-sensitive. Anywhere in the first 20 lines of
# a file's on-disk content marks it protected. Intentionally specific
# enough that natural prose doesn't trip it, recognizable enough that a
# human grepping for "DO NOT TOUCH" finds it.
PROTECTION_SENTINEL = "DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST"

# How many leading lines the sentinel has to appear within. Covers
# copyright headers + import/using blocks on top of the header itself.
_SENTINEL_HEADER_LINES = 20

# Regex for a pair-files entry line: one or more leading comment/space
# characters, then a path-like token (letters/digits/`_-./`), then optional
# trailing whitespace or comment close. Matches `//   a/b.cs`, `     a.cs`,
# `-- a.cs`, `#   src/a.py`, etc. Rejects lines that look like sentences
# (multiple whitespace-separated tokens or trailing punctuation).
_PAIR_LINE_RE = re.compile(
    r"""^
    [\s/*#@\-]*         # comment prefix + leading whitespace
    (?P<path>           # capture the path token
        [A-Za-z_\.]     # must start with a letter, underscore, or dot
        [\w\-./]*       # followed by path-safe chars
        (?:\.[A-Za-z0-9]+)?  # optional extension
    )
    [\s*@/]*            # trailing whitespace + comment close
    $
    """,
    re.VERBOSE,
)

# Case-insensitive marker for the "Pair files:" section header — permits
# "Pair files:", "Pair Files :", "pair-files:", etc.
_PAIR_SECTION_RE = re.compile(r"pair[\s_\-]*files\s*:", re.IGNORECASE)


def has_protection_sentinel(content: str) -> bool:
    """Return True iff the sentinel phrase appears in the first 20 lines.

    Case-sensitive — so lowercase or TitleCase variants don't trigger,
    but the literal phrase embedded in any comment syntax does.
    """
    if not content:
        return False
    # Split into at most _SENTINEL_HEADER_LINES + 1 parts so we check only
    # the header window regardless of overall file size.
    head = "\n".join(content.splitlines()[:_SENTINEL_HEADER_LINES])
    return PROTECTION_SENTINEL in head


def parse_pair_files(content: str) -> list[str]:
    """Extract the file paths listed under a header's `Pair files:` block.

    Header shape (comment syntax agnostic):

        Pair files:
            path/one.ext
            path/two.ext

    Stops at the first line that doesn't look like a path entry (blank
    comment-only line, prose, or end of header block).
    """
    if not content or not has_protection_sentinel(content):
        return []

    lines = content.splitlines()[:_SENTINEL_HEADER_LINES]
    pairs: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip("/*#@- \t")
        if not in_block:
            if _PAIR_SECTION_RE.search(line):
                in_block = True
            continue
        # In the pair-files block. Try to match a path entry.
        if not stripped:
            # Blank comment line ends the block.
            break
        m = _PAIR_LINE_RE.match(line)
        if m is None:
            # Non-path line ends the block.
            break
        path = m.group("path")
        # Reject tokens that are obviously prose (single word, no
        # separator, no extension).
        if "/" not in path and "." not in path:
            break
        pairs.append(path)
    return pairs


def scan_protected_state(path: Path) -> tuple[bool, list[str]]:
    """One-call helper for the index: reads the file, returns
    (protected, pair_files). Returns (False, []) on any read error —
    missing file, permission, binary content, etc.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return False, []
    protected = has_protection_sentinel(content)
    pairs = parse_pair_files(content) if protected else []
    return protected, pairs
