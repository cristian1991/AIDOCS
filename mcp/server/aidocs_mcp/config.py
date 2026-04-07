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
        "include_tests": False,
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
        "host_mode": "enforced",  # "enforced" = gates do the work, minimal injection | "advisory" = verbose directives for hosts without gates
        "inject_message_directives": True,
        "inject_rules_on_bootstrap": True,
    },
    "dev": {
        "dev_mode": False,         # unlocks AIDOCS source editing
        "allow_config_edit": False, # unlocks aidocs.toml editing
    },
    "agents": {
        "allow_subagents": False,
    },
    "gate": {
        "enforce": True,  # tool gates active (bash allowlist, raw tool blocking, destructive blocking)
        "exempt_extensions": [".output", ".log", ".txt", ".md", ".json", ".toml", ".yaml", ".yml"],
        "exempt_paths": [],
        "protected_patterns": ["tokens.json", "keys.json", "auth.json", "secrets.json", "*.local.json", "appsettings.*.json"],
        "bash_allowed": [
            "cd", "ls", "pwd", "which", "where", "echo", "wc", "mkdir", "env", "export", "set",
            "python", "python3", "python -m pytest", "python -m", "pytest", "pip", "pip3", "pip install", "uv",
            "git", "gh",  # git allowed — heuristic judge blocks destructive ops (force push, reset --hard, etc.)
            "npm", "npm run", "npm install", "npm test", "npm start", "npm build",
            "npx", "node", "pnpm", "pnpm run", "yarn", "yarn run", "bun", "bun run",
            "tsc", "eslint", "prettier", "vite", "vitest", "jest", "webpack", "rollup", "esbuild",
            "dotnet", "dotnet build", "dotnet test", "dotnet run", "dotnet publish", "dotnet restore",
            "cargo", "cargo build", "cargo test", "cargo run", "cargo clippy", "rustc",
            "go", "go build", "go test", "go run", "go mod",
            "javac", "java", "gradle", "mvn", "ant",
            "docker", "docker-compose", "docker compose", "podman",
            "make", "cmake", "ninja",
            "black", "ruff", "mypy", "flake8", "isort", "pylint", "bandit",
            "wsl", "pwsh", "powershell",
            "curl", "wget",
            "tar", "unzip", "zip",
        ],
    },
    "code_quality": {
        "comment_enforcement": "advisory",
    },
    "presentation": {
        "helper_skill_excerpt_lines": 12,
        "helper_skill_excerpt_chars": 1200,
        "workflow_summary_limit": 3,
        "resume_journal_last_n": 10,
        "handoff_stale_after_hours": 24,
        "handoff_recent_hours": 24,
    },
    "interaction": {},
    "conductor": {
        "mode": "normal",    # "normal" = serial, one agent | "parallel" = multi-agent concurrent lanes
        "backend": "claude", # "claude" | "codex" — which agent backend for workers
    },
}


class _SafeFormatDict(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _find_user_config_file() -> Path | None:
    """Find the user-global aidocs.toml at ~/.config/aidocs/aidocs.toml."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", "")) / "aidocs"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) / "aidocs" if xdg else Path.home() / ".config" / "aidocs"
    candidate = base / "aidocs.toml"
    return candidate if candidate.is_file() else None


def _find_distribution_config_file() -> Path | None:
    """Find the AIDOCS distribution aidocs.toml (ships with the MCP server)."""
    candidates = [
        Path(__file__).resolve().parents[3] / "aidocs.toml",
        Path(__file__).resolve().parents[2] / "aidocs.toml",
        Path(os.environ.get("AIDOCS_PATH", "")) / "aidocs.toml"
        if os.environ.get("AIDOCS_PATH")
        else None,
        Path.cwd() / "aidocs.toml",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def _find_config_file() -> Path | None:
    """Find the most specific global config: user-global first, then distribution."""
    return _find_user_config_file() or _find_distribution_config_file()


def _load_config_file(path: Path | None) -> dict[str, object]:
    if path is None or tomllib is None or not path.is_file():
        return {}
    try:
        with open(path, "rb") as handle:
            loaded = tomllib.load(handle)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _resolve_action_hooks_dir() -> Path | None:
    candidates = []
    env_root = os.environ.get("AIDOCS_PATH")
    if env_root:
        candidates.append(Path(env_root) / "action_hooks")
    candidates.extend(
        [
            Path(__file__).resolve().parents[3] / "action_hooks",
            Path(__file__).resolve().parents[2] / "action_hooks",
            Path.cwd() / "action_hooks",
        ]
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def _load_action_hook_defaults() -> dict[str, object]:
    root = _resolve_action_hooks_dir()
    if root is None or tomllib is None:
        return {}
    merged: dict[str, Any] = {}
    for path in sorted(root.glob("*.toml")):
        loaded = _load_config_file(path)
        if loaded:
            _merge_dicts(merged, loaded)
    return merged


def _merge_dicts(base: dict[str, Any], override: dict[str, object]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dicts(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


# Populated once at import — invalidated by reload_config_caches()
_ACTION_HOOK_DEFAULTS: dict[str, object] = _load_action_hook_defaults()


def reload_config_caches() -> None:
    """Invalidate all module-level config caches. Call after editing config files."""
    global _ACTION_HOOK_DEFAULTS
    _ACTION_HOOK_DEFAULTS = _load_action_hook_defaults()
    # Also invalidate action token cache
    try:
        from .intent_guard import _invalidate_action_token_cache
        _invalidate_action_token_cache()
    except ImportError:
        pass


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
    def __init__(
        self,
        global_config_path: Path | None = None,
        *,
        user_config_path: Path | None = None,
    ) -> None:
        self._global_config_path = global_config_path
        self._user_config_path = user_config_path
        self._has_custom_paths = global_config_path is not None or user_config_path is not None

    def distribution_config_path(self) -> Path | None:
        return (
            self._global_config_path
            if self._global_config_path is not None
            else _find_distribution_config_file()
        )

    def user_config_path(self) -> Path | None:
        return (
            self._user_config_path
            if self._user_config_path is not None
            else _find_user_config_file()
        )

    def global_config_path(self) -> Path | None:
        """Backward compat: returns the most-specific global path found."""
        return self.user_config_path() or self.distribution_config_path()

    def project_config_path(self, project_root: Path | None) -> Path | None:
        if project_root is None:
            return None
        return project_root / "aidocs.toml"

    def session_config_path(
        self, project_root: Path | None, session_id: str | None
    ) -> Path | None:
        if (
            project_root is None
            or not isinstance(session_id, str)
            or not session_id.strip()
        ):
            return None
        return (
            project_root / ".MEMORY" / "sessions" / session_id.strip() / "aidocs.toml"
        )

    def effective_config(
        self,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Build effective config: defaults + action hooks (TOML) + settings (SQLite).

        TOML files are only read for action hooks, interaction templates, and
        language descriptors — NOT for settings. Settings come from SQLite only.
        """
        merged: dict[str, Any] = deepcopy(_DEFAULT_CONFIG)
        # Action hooks / interaction templates from TOML (static definitions)
        hooks = _ACTION_HOOK_DEFAULTS if self is _DEFAULT_RESOLVER else _load_action_hook_defaults()
        _merge_dicts(merged, hooks)

        # Read TOML config files
        dist_path = self.distribution_config_path()
        user_path = self.user_config_path()
        project_path = self.project_config_path(project_root) if project_root else None
        session_path = self.session_config_path(project_root, session_id) if project_root else None

        seen_paths: set[Path] = set()
        for path in (
            dist_path if dist_path != project_path else None,
            user_path,
            project_path,
            session_path,
        ):
            if path is None or path in seen_paths:
                continue
            seen_paths.add(path)
            _merge_dicts(merged, _load_config_file(path))
        if project_root is not None:
            try:
                from .config_store import ConfigStore
                store = ConfigStore()
                db_config = store.effective_config(
                    project_root,
                    session_id=session_id,
                    defaults={},
                )
                if db_config:
                    _merge_dicts(merged, db_config)
            except Exception:
                pass
        return merged

    def config_layer_paths(
        self,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, Path | None]:
        """Return all resolved layer paths for diagnostics and resolved-config output."""
        return {
            "distribution": self.distribution_config_path(),
            "user": self.user_config_path(),
            "project": self.project_config_path(project_root),
            "session": self.session_config_path(project_root, session_id),
        }

    def write_resolved_config(
        self,
        *,
        project_root: Path,
        session_id: str | None = None,
    ) -> Path:
        """Write resolved-config.json into project .MEMORY so host plugins can read it."""
        import json

        effective = self.effective_config(
            project_root=project_root, session_id=session_id
        )
        layers = self.config_layer_paths(
            project_root=project_root, session_id=session_id
        )
        payload = {
            "resolved": effective,
            "layers": {k: str(v) if v else None for k, v in layers.items()},
            "active_layers": [
                k for k, v in layers.items() if v is not None and v.is_file()
            ],
        }
        out_dir = project_root / ".MEMORY" / "config"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "resolved-config.json"
        out_path.write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return out_path

    def get(
        self,
        key: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> object | None:
        # Fast path: only the module-level default resolver uses the cached config
        if project_root is None and session_id is None and self is _DEFAULT_RESOLVER:
            return _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, key)
        return _get_dotted(
            self.effective_config(project_root=project_root, session_id=session_id), key
        )

    def get_layer_value(
        self,
        key: str,
        scope: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> object | None:
        """Read a single config layer's raw value for a key (checks SQLite first, then TOML)."""
        # Check SQLite first (primary store)
        if project_root is not None:
            try:
                from .config_store import ConfigStore
                db_scope = "user" if scope == "global" else scope
                scope_key = session_id or "" if scope == "session" else ""
                val = ConfigStore().get(project_root, key, scope=db_scope, scope_key=scope_key)
                if val is not None:
                    return val
            except Exception:
                pass
        # Fall back to TOML
        path_map = {
            "global": self.user_config_path(),
            "user": self.user_config_path(),
            "project": self.project_config_path(project_root),
            "session": self.session_config_path(project_root, session_id),
        }
        layer_path = path_map.get(scope)
        layer_data = _load_config_file(layer_path)
        return _get_dotted(layer_data, key)


    def render_text(
        self,
        key: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
        **kwargs: object,
    ) -> str:
        template = self.get(key, project_root=project_root, session_id=session_id)
        if not isinstance(template, str):
            return ""
        return template.format_map(
            _SafeFormatDict({str(name): value for name, value in kwargs.items()})
        )


_DEFAULT_RESOLVER = ConfigResolver()
_DEFAULT_EFFECTIVE_CONFIG = _DEFAULT_RESOLVER.effective_config()

JOURNAL_MAX_ENTRIES: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.max_entries") or 100
)
JOURNAL_EVICT_BATCH: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.evict_batch") or 20
)
JOURNAL_TRIVIAL_ACTIONS: set[str] = set(
    _parse_string_list(
        _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.trivial_actions")
    )
)
JOURNAL_MIN_INTENT_LENGTH: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "journal.min_intent_length") or 10
)

INDEX_EXTRA_SKIP_DIRS: set[str] = set(
    _parse_string_list(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.extra_skip_dirs"))
)
INDEX_EXTRA_MODULE_HINTS: set[str] = set(
    _parse_string_list(
        _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.extra_module_hints")
    )
)
INDEX_MAX_JSON_SIZE: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.max_json_size") or 100_000
)
INDEX_ENABLED_LANGUAGES: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.enabled_languages") or "all"
).strip()

LANGUAGES_ENABLED: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "languages.enabled") or "all"
).strip()

# ── Project-aware setting accessor ──
# These replace stale module-level constants. They read from ConfigStore
# (SQLite) with fallback to _DEFAULT_CONFIG when no project context is available.

def get_setting(
    key: str,
    *,
    project_root: Path | None = None,
    session_id: str | None = None,
    default: Any = None,
) -> Any:
    """Read a setting from SQLite config store, falling back to defaults.

    This is the canonical way to read settings at runtime. Do NOT use
    module-level constants (DEV_MODE, GATE_ENFORCE, etc.) — they are stale.
    """
    # Try SQLite first when project context is available
    if project_root is not None:
        try:
            from .config_store import ConfigStore
            value = ConfigStore().get_effective(
                project_root, key, session_id=session_id, default=None,
            )
            if value is not None:
                return value
        except Exception:
            pass

    # Fall back to defaults
    fallback = _get_dotted(_DEFAULT_CONFIG, key)
    return fallback if fallback is not None else default


# ── Legacy module-level constants ──
# Kept for backward compatibility with tests that monkeypatch them.
# Production code should use get_setting() or effective_config() instead.

TOOLS_CALL_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.tool_call_timeout") or 10
)
TOOLS_SYNC_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.sync_functions_timeout") or 30
)
TOOLS_GIT_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.git_functions_timeout") or 30
)
TOOLS_MAX_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.max_timeout") or 120
)

AGENT_HOST_MODE: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agent.host_mode") or "enforced"
).strip().lower()
AGENT_INJECT_MESSAGE_DIRECTIVES: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agent.inject_message_directives"),
    default=True,
)
AGENT_INJECT_RULES_ON_BOOTSTRAP: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agent.inject_rules_on_bootstrap"),
    default=True,
)

DEV_MODE: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "dev.dev_mode"), default=False
)
ALLOW_CONFIG_EDIT: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "dev.allow_config_edit"), default=False
)
GATE_ENFORCE: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.enforce"), default=True
)
ALLOW_SUBAGENTS: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "agents.allow_subagents"), default=False
)

OUTPUT_GUARD_ENABLED: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.output_guard"), default=True
)
OUTPUT_GUARD_REDACT: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.output_guard_redact"), default=True
)

_raw_exempt_ext = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.exempt_extensions")
GATE_EXEMPT_EXTENSIONS: list[str] = list(_raw_exempt_ext) if isinstance(_raw_exempt_ext, list) else []
_raw_exempt_paths = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.exempt_paths")
GATE_EXEMPT_PATHS: list[str] = list(_raw_exempt_paths) if isinstance(_raw_exempt_paths, list) else []
_raw_protected = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.protected_patterns")
GATE_PROTECTED_PATTERNS: list[str] = list(_raw_protected) if isinstance(_raw_protected, list) else []
_raw_bash_allowed = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "gate.bash_allowed")
GATE_BASH_ALLOWED: list[str] = [s.lower() for s in _raw_bash_allowed] if isinstance(_raw_bash_allowed, list) else []
INDEX_INCLUDE_TESTS: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.include_tests"), default=False
)

CODE_QUALITY_COMMENT_ENFORCEMENT: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "code_quality.comment_enforcement")
    or "advisory"
)

CONFIG_PATH: str = str(_DEFAULT_RESOLVER.global_config_path() or "(not found)")


def render_interaction_text(
    key: str,
    *,
    project_root: Path | None = None,
    session_id: str | None = None,
    **kwargs: object,
) -> str:
    return _DEFAULT_RESOLVER.render_text(
        key,
        project_root=project_root,
        session_id=session_id,
        **kwargs,
    )
