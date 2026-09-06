"""Single neutral DNT path extractor — shared by prompt_mutator's deterministic
literal grant parser AND the legacy NLP dnt_detector. One regex, one function;
no mirrored copies.

A DNT path token contains a `/`/`\\` separator OR an extension dot
(e.g. ``src/auth.py``, ``config.py``, ``DentalApp.Web/foo.js``). Backslashes are
normalized to forward slashes. NLP-free, pure regex.
"""
from __future__ import annotations

import re

# Canonical DNT path-token shape, as ONE raw pattern fragment. Everything that
# needs to recognise a path (the extractor regex here, prompt_mutator's clause
# split lookahead, and its immediate target-list grammar) derives from this single
# source — no mirrored copies.
PATH_TOKEN = r"[A-Za-z_.][\w\-.]*(?:[/\\][\w\-.]+)*\.[A-Za-z0-9]+"

PATH_TOKEN_RE = re.compile(rf"\b({PATH_TOKEN})\b")


def extract_dnt_paths(text: str) -> set[str]:
    """All DNT path tokens in `text`, backslash-normalized. Empty set when none."""
    return {m.group(1).replace("\\", "/") for m in PATH_TOKEN_RE.finditer(text or "")}
