"""Project-agnostic ignore rules for the ai_slop finder suite.

DOCTRINE (2026-07-16, no-blanket-allow): these rules suppress only classes of
findings that are structurally non-actionable in ANY project — code we do not
own (vendored trees), code no human wrote (generated/minified bundles), and
throwaway areas (scratch). They must stay narrow: a real finding in first-party
code is NEVER suppressed here — it gets fixed or logged with a reason.

Matching is by exact path COMPONENT (never substring), so `src/distutils.py`
or `vendors_api/` are not swallowed by `dist`/`vendor`.
"""

from __future__ import annotations

import re

__all__ = ["IGNORED_DIR_PARTS", "is_ignored_path", "is_generated_asset", "looks_minified_symbol"]

# Directory components whose subtrees are out of slop-scan scope everywhere:
# vendored (not ours to fix), package/build output (regenerated, not authored),
# and scratch (explicitly throwaway).
IGNORED_DIR_PARTS = frozenset(
    {
        "third_party",
        "vendor",
        "vendored",
        "node_modules",
        "bower_components",
        "site-packages",
        ".venv",
        "venv",
        ".tox",
        ".eggs",
        "__pycache__",
        ".git",
        "dist",
        "dist-web",
        "build",
        "scratch",
    }
)

# Hashed bundle names (vite/webpack content hashes, e.g. `Charts-Ma3qqnjN.js`):
# a mixed-case or digit-bearing 8+ char suffix right before a script/style ext.
_HASHED_BUNDLE_RE = re.compile(
    r"-(?=[A-Za-z0-9_-]*[A-Z0-9])[A-Za-z0-9_-]{8,}\.(?:js|mjs|cjs|css)$"
)


def is_ignored_path(rel_path: str) -> bool:
    """True when `rel_path` lies in an out-of-scope subtree or is a generated asset."""
    parts = str(rel_path).replace("\\", "/").split("/")
    if any(p.lower() in IGNORED_DIR_PARTS for p in parts[:-1]):
        return True
    return bool(parts) and is_generated_asset(parts[-1])


def is_generated_asset(filename: str) -> bool:
    """Minified or content-hash-named bundle output — machine-written, never slop-fixable."""
    low = filename.lower()
    if low.endswith((".min.js", ".min.css", ".min.mjs")):
        return True
    return bool(_HASHED_BUNDLE_RE.search(filename))


def looks_minified_symbol(name: str) -> bool:
    """Single/double-char identifiers are minifier output, not shared design structure."""
    return len(name.strip()) <= 2
