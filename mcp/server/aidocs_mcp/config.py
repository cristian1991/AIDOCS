"""AIDOCS MCP configuration loader.

Reads aidocs.toml once at import time and caches all values in module-level variables.
No file I/O after initial load.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]  # fallback for older Python
    except ImportError:
        tomllib = None  # type: ignore[assignment]


def _find_config_file() -> Path | None:
    """Find aidocs.toml by searching known locations."""
    candidates = [
        # Project root (canonical location)
        Path(__file__).resolve().parents[3] / "aidocs.toml",
        # Legacy: next to the MCP server package
        Path(__file__).resolve().parents[2] / "aidocs.toml",
        # AIDOCS_PATH env var
        Path(os.environ.get("AIDOCS_PATH", "")) / "aidocs.toml" if os.environ.get("AIDOCS_PATH") else None,
        # Current working directory
        Path.cwd() / "aidocs.toml",
    ]
    for c in candidates:
        if c is not None and c.is_file():
            return c
    return None


def _load_config() -> dict:
    """Load and parse aidocs.toml. Returns empty dict if not found or unparseable."""
    path = _find_config_file()
    if path is None or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _parse_comma_set(value: str) -> set[str]:
    """Parse a comma-separated string into a set of stripped, non-empty values."""
    return {item.strip() for item in value.split(",") if item.strip()}


# ── Load once at import time ────────────────────────────────────────

_raw = _load_config()
_journal = _raw.get("journal", {})
_index = _raw.get("index", {})
_languages = _raw.get("languages", {})
_agent = _raw.get("agent", {})

# Journal settings
JOURNAL_MAX_ENTRIES: int = int(_journal.get("max_entries", 100))
JOURNAL_EVICT_BATCH: int = int(_journal.get("evict_batch", 20))
JOURNAL_TRIVIAL_ACTIONS: set[str] = _parse_comma_set(
    str(_journal.get("trivial_actions", "task_begin, task_update, project_update"))
)
JOURNAL_MIN_INTENT_LENGTH: int = int(_journal.get("min_intent_length", 10))

# Index settings
INDEX_EXTRA_SKIP_DIRS: set[str] = _parse_comma_set(str(_index.get("extra_skip_dirs", "")))
INDEX_EXTRA_MODULE_HINTS: set[str] = _parse_comma_set(str(_index.get("extra_module_hints", "")))
INDEX_MAX_JSON_SIZE: int = int(_index.get("max_json_size", 100_000))
INDEX_ENABLED_LANGUAGES: str = str(_index.get("enabled_languages", "all")).strip()

# Language settings
LANGUAGES_ENABLED: str = str(_languages.get("enabled", "all")).strip()

# Tool timeout settings
_tools = _raw.get("tools", {})
TOOLS_CALL_TIMEOUT: int = int(_tools.get("tool_call_timeout", 10))
TOOLS_SYNC_TIMEOUT: int = int(_tools.get("sync_functions_timeout", 30))
TOOLS_GIT_TIMEOUT: int = int(_tools.get("git_functions_timeout", 30))
TOOLS_MAX_TIMEOUT: int = int(_tools.get("max_timeout", 120))

# Agent settings
AGENT_DIRECTIVE_STYLE: str = str(_agent.get("directive_style", "short"))
AGENT_INJECT_MESSAGE_DIRECTIVES: bool = bool(_agent.get("inject_message_directives", True))
AGENT_INJECT_RULES_ON_BOOTSTRAP: bool = _agent.get("inject_rules_on_bootstrap", True) is not False

# Code quality settings
_code_quality = _raw.get("code_quality", {})
CODE_QUALITY_COMMENT_ENFORCEMENT: str = str(_code_quality.get("comment_enforcement", "advisory"))

# Expose the config file path for debugging
CONFIG_PATH: str = str(_find_config_file() or "(not found)")
