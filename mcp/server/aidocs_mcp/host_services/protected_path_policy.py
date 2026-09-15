"""Path-class checks for the host hook (claude_hook + future adapters).

Lifted from claude_hook.py 2026-05-27 (Phase 2 of the thinning).
The three constants `_PROTECTED_CONFIG`, `_INFRASTRUCTURE_PATHS`, and
`_CODE_EXTENSIONS` lived as class-field constants on
``ClaudeHookHandler``. Pre-lift they were not read by anything — they
were placeholders for the "edit attempt at AIDOCS infrastructure"
warning surface that never fully wired. Hoisted here so the canonical
data lives in one place and so future adapter code can reach the same
classifications without duplicating the lists.

This module is intentionally narrow — the BROADER protected-paths
classifier lives in ``protected_paths_classifier.py`` (covers OS
infra, secrets dirs, host config files, .ssh, .aws, etc.). The two
do not overlap by design: protected_paths_classifier is the SAFETY
boundary (refuses reads/writes/edits at the gate cascade); this
module is the ADVISORY boundary (surfaces "you're editing AIDOCS's
own config / source" hints).

Public API:
  is_protected_config(path)        → bool: is this an AIDOCS config
                                            file (aidocs.toml, etc.)?
  is_aidocs_infrastructure(path)   → bool: is this a path inside
                                            AIDOCS's own source tree?
  is_code_file(path)               → bool: is this a source-code file
                                            (indexer-recognized
                                            extension)?
  classify(path)                   → tuple[str, ...] of matching tags
                                            (zero, one, or more of
                                            ``protected_config``,
                                            ``infrastructure``,
                                            ``code_file``).
"""

from __future__ import annotations

PROTECTED_CONFIG: frozenset[str] = frozenset(
    {
        "aidocs.toml",
        "aidocs-plugin.json",
    },
)

INFRASTRUCTURE_PATH_FRAGMENTS: tuple[str, ...] = (
    "aidocs_mcp/",
    "aidocs_mcp\\",
    "core/plugins/aidocs.js",
)

# File extensions the AIDOCS code indexer recognizes — used to decide
# whether an "advise indexed-tool over raw read" hint is appropriate.
# Source: indexer language mapping (mcp/server/aidocs_mcp/index_languages/).
CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".cs",
        ".cshtml",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".ex",
        ".exs",
        ".swift",
        ".dart",
        ".lua",
        ".sh",
        ".bash",
        ".ps1",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".html",
        ".htm",
        ".vue",
        ".svelte",
        ".sql",
        ".prisma",
        ".resx",
    },
)


def _basename(path: str) -> str:
    """Return the basename component, handling both forward and back
    slashes (Windows + Unix). No Path() use — this is a pure string
    op so it stays cheap when called per-tool-event.
    """
    if not path:
        return ""
    # Last component after either separator
    last_fwd = path.rfind("/")
    last_back = path.rfind("\\")
    cut = max(last_fwd, last_back)
    return path[cut + 1 :] if cut >= 0 else path


def is_protected_config(path: str) -> bool:
    """True iff the path's basename is an AIDOCS config file that
    agents must never modify.
    """
    return _basename(path).lower() in {c.lower() for c in PROTECTED_CONFIG}


def is_aidocs_infrastructure(path: str) -> bool:
    """True iff the path is somewhere inside AIDOCS's own source tree
    (aidocs_mcp/, core/plugins/aidocs.js).
    """
    if not path:
        return False
    p = path.replace("\\", "/").lower()
    for frag in INFRASTRUCTURE_PATH_FRAGMENTS:
        f = frag.replace("\\", "/").lower()
        if f in p:
            return True
    return False


def is_code_file(path: str) -> bool:
    """True iff the file has an extension the AIDOCS indexer recognizes
    as source code. The boundary the advisory-hint surface uses to
    decide "should the agent be using an indexed AIDOCS tool here?\"
    """
    base = _basename(path).lower()
    if "." not in base:
        return False
    ext = "." + base.rsplit(".", 1)[-1]
    return ext in CODE_EXTENSIONS


def classify(path: str) -> tuple[str, ...]:
    """Return all tags that match this path. Useful when the caller
    wants to combine multiple advisories ("infrastructure code file"
    → both 'infrastructure' and 'code_file').
    """
    tags: list[str] = []
    if is_protected_config(path):
        tags.append("protected_config")
    if is_aidocs_infrastructure(path):
        tags.append("infrastructure")
    if is_code_file(path):
        tags.append("code_file")
    return tuple(tags)
