"""AIDOCS MCP configuration loading and scoped resolution."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


_DEFAULT_CONFIG: dict[str, dict[str, object]] = {
    "journal": {
        "max_entries": 100,
        "evict_batch": 20,
        "trivial_actions": ["task_begin", "task_update", "project_update"],
        "min_intent_length": 10,
    },
    "index": {
        "extra_skip_dirs": [],
        "extra_module_hints": [],
        "max_json_size": 100_000,
        "enabled_languages": "all",
    },
    "languages": {
        "enabled": "all",
    },
    "tools": {
        "tool_call_timeout": 10,
        "sync_functions_timeout": 30,
        "git_functions_timeout": 30,
        "max_timeout": 120,
    },
    "agent": {
        "directive_style": "short",
        "inject_message_directives": True,
        "inject_rules_on_bootstrap": True,
    },
    "dev": {
        "dev_mode": False,
    },
    "code_quality": {
        "comment_enforcement": "advisory",
    },
}


def _find_config_file() -> Path | None:
    """Find the global aidocs.toml from the existing known locations."""
    candidates = [
        Path(__file__).resolve().parents[3] / "aidocs.toml",
        Path(__file__).resolve().parents[2] / "aidocs.toml",
        Path(os.environ.get("AIDOCS_PATH", "")) / "aidocs.toml" if os.environ.get("AIDOCS_PATH") else None,
        Path.cwd() / "aidocs.toml",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def _load_config_file(path: Path | None) -> dict[str, object]:
    if path is None or tomllib is None or not path.is_file():
        return {}
    try:
        with open(path, "rb") as handle:
            loaded = tomllib.load(handle)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _merge_dicts(base: dict[str, Any], override: dict[str, object]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dicts(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def _get_dotted(config: dict[str, object], key: str) -> object | None:
    current: object = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _parse_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _parse_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


class ConfigResolver:
    def __init__(self, global_config_path: Path | None = None) -> None:
        self._global_config_path = global_config_path

    def global_config_path(self) -> Path | None:
        return self._global_config_path if self._global_config_path is not None else _find_config_file()

    def project_config_path(self, project_root: Path | None) -> Path | None:
        if project_root is None:
            return None
        return project_root / "aidocs.toml"

    def session_config_path(self, project_root: Path | None, session_id: str | None) -> Path | None:
        if project_root is None or not isinstance(session_id, str) or not session_id.strip():
            return None
        return project_root / ".MEMORY" / "sessions" / session_id.strip() / "aidocs.toml"

    def effective_config(
        self,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        merged: dict[str, Any] = deepcopy(_DEFAULT_CONFIG)
        seen_paths: set[Path] = set()
        for path in (
            self.global_config_path(),
            self.project_config_path(project_root),
            self.session_config_path(project_root, session_id),
        ):
            if path is None or path in seen_paths:
                continue
            seen_paths.add(path)
            _merge_dicts(merged, _load_config_file(path))
        return merged

    def get(
        self,
        key: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> object | None:
        return _get_dotted(self.effective_config(project_root=project_root, session_id=session_id), key)


_DEFAULT_RESOLVER = ConfigResolver()
_DEFAULT_EFFECTIVE_CONFIG = _DEFAULT_RESOLVER.effective_config()

JOURNAL_MAX_ENTRIES: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.max_entries") or 100)
JOURNAL_EVICT_BATCH: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.evict_batch") or 20)
JOURNAL_TRIVIAL_ACTIONS: set[str] = set(
    _parse_string_list(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.trivial_actions"))
)
JOURNAL_MIN_INTENT_LENGTH: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.min_intent_length") or 10)

INDEX_EXTRA_SKIP_DIRS: set[str] = set(_parse_string_list(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.extra_skip_dirs")))
INDEX_EXTRA_MODULE_HINTS: set[str] = set(
    _parse_string_list(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.extra_module_hints"))
)
INDEX_MAX_JSON_SIZE: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.max_json_size") or 100_000)
INDEX_ENABLED_LANGUAGES: str = str(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.enabled_languages") or "all").strip()

LANGUAGES_ENABLED: str = str(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "languages.enabled") or "all").strip()

TOOLS_CALL_TIMEOUT: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.tool_call_timeout") or 10)
TOOLS_SYNC_TIMEOUT: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.sync_functions_timeout") or 30)
TOOLS_GIT_TIMEOUT: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.git_functions_timeout") or 30)
TOOLS_MAX_TIMEOUT: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.max_timeout") or 120)

AGENT_DIRECTIVE_STYLE: str = str(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agent.directive_style") or "short")
AGENT_INJECT_MESSAGE_DIRECTIVES: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agent.inject_message_directives"),
    default=True,
)
AGENT_INJECT_RULES_ON_BOOTSTRAP: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agent.inject_rules_on_bootstrap"),
    default=True,
)

DEV_MODE: bool = _parse_bool(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "dev.dev_mode"), default=False)

CODE_QUALITY_COMMENT_ENFORCEMENT: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "code_quality.comment_enforcement") or "advisory"
)

CONFIG_PATH: str = str(_DEFAULT_RESOLVER.global_config_path() or "(not found)")
