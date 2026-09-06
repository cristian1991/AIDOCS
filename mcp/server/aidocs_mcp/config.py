"""AIDOCS MCP configuration loading and scoped resolution."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import UTC
from pathlib import Path
from typing import Any, NamedTuple

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
        "sync_write_timeout": 60,
        "index_sync_timeout": 120,
        "memory_surfacing_timeout_ms": 500,
        "git_functions_timeout": 30,
        "max_timeout": 120,
        # Cap for foreground bash long-runners (pytest, npm install,
        # pip install, etc.). Commands matching those families are
        # hard-blocked unless invoked with run_in_background=true.
        # 300s = 5 min — short enough that "background it or narrow
        # scope" becomes the natural response, long enough that
        # normal single-test invocations still complete inline.
        "bash_long_runner_cap_seconds": 300,
    },
    "agent": {
        "host_mode": "enforced",  # "enforced" (default) = PreToolUse gates enforce; works on Claude Code, Codex, OpenCode CLI. "advisory" = in-message directive injection only — needed for OpenCode Desktop (no hook surface).
        "inject_message_directives": True,
        "inject_rules_on_bootstrap": True,
        "directive_style": "short",
    },
    "dev": {
        "dev_mode": False,  # unlocks AIDOCS source editing
        "allow_config_edit": False,  # unlocks aidocs.toml editing
    },
    "agents": {
        "allow_subagents": False,
    },
    "security": {
        "enforce": True,  # tool gates active (bash allowlist, raw tool blocking, destructive blocking)
        # Tier enforcement policy. Tiers classify gates by TRUST
        # SURFACE, not implementation:
        #   T0 = dashboard-only unblock (edit_redirect, raw_shell,
        #        shell_deny, test_retry, heuristic, infrastructure,
        #        foreground_cap, lane_scope, tool_policy). NLP user-
        #        intent grants have no authority here.
        #   T1 = user-intent unblock (raw_tool, shell_allow,
        #        agent_brief). Per-turn prompt phrases lift the block.
        # "strict"     → T0 dashboard-only, T1 user-intent. Default.
        # "user_trust" → T0 can also be lifted by user-intent grants.
        #                Operator-trust mode; rarely appropriate.
        # "off"        → all tier gates become advisory (logged, not
        #                blocked). Dev-only escape hatch.
        "tier_enforcement": "strict",
        "exempt_extensions": [
            ".output",
            ".log",
            ".txt",
            # Images: Claude's harness ingests these visually via Read.
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            # PDFs: Read extracts text; AIDOCS-native PDF parser ships in Phase 2
            # but built-in Read already handles short narrative PDFs well.
            ".pdf",
            # Jupyter notebooks: Read parses cells + outputs natively.
            ".ipynb",
        ],
        "exempt_paths": [],
        "protected_patterns": [
            "tokens.json",
            "keys.json",
            "auth.json",
            "secrets.json",
            "*.local.json",
            "appsettings.*.json",
        ],
        # security.bash_allowed legacy substring allowlist removed
        # 2026-04-25 — superseded by the canonical [bash] table.
        # Operators with stale bash_allowed entries in sqlite get them
        # silently ignored by all current code paths. (Audit: no
        # remaining grep references in mcp/server/.)
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
        "backend": "claude",  # "claude" | "codex" — which agent backend for workers
        # Global cap on simultaneously-running spawned workers. Each
        # worker is a full Claude CLI subprocess with its own MCP stdio
        # server; on typical dev machines >3-4 concurrent chew through
        # memory + Anthropic rate limits fast. Enforced in
        # AgentExpertService.spawn_worker_async.
        "max_concurrent_workers": 3,
        # autowake_max_interval_seconds REMOVED 2026-04-30 along with
        # the autowake/forced-work feature.
        # Sticky lane-exit for the conductor: when True, every
        # UserPromptSubmit in a non-worker process clears the
        # session_query_gate row's current_lane_id + lane_exact_paths.
        # Solves the shared-row demotion trap for long-running
        # overnight chains without the operator having to type
        # "exit lane" every turn. Worker processes are detected via
        # AIDOCS_EXPERT_LANE_ID env and are NEVER auto-exited (would
        # defeat lane isolation). Default False — explicit opt-in
        # because a stuck conductor is a real signal most of the time.
        "auto_exit_lane": False,
    },
    "edit": {
        # Empire doctrine 2026-05-01: cap tightened from 2000 → 200 to
        # discourage shipping large old/new pairs. Larger edits use
        # ai_replace(mode='anchor') or ai_replace(mode='symbol').
        "str_replace_max_old_chars": 1000,
        # Re-lock free span (2026-06-17): a 2nd line-edit to a file in one turn is
        # allowed WITHOUT a re-read when it touches <= this many lines (small line-number
        # drift). Larger edits must re-read first (which releases the lock). 0 disables.
        "line_edit_relock_free_span": 10,
    },
    "notifications": {
        # Phoenix 2026-05-10: max times a run-done notification
        # re-displays in tool-call envelopes before auto-dismissing.
        # 3 = surface three times, then drop even if the agent
        # never read the output. 0 = classic 'until satisfied'
        # behavior (persists forever until ai_run_output dismisses).
        "max_displays": 3,
    },
    # Declarative bash policy — replaces the legacy security.bash_allowed
    # substring list. Grammar: `allow/deny` map base_cmd → [fnmatch patterns]
    # applied to the argument tail. `"*"` means any args allowed;
    # `"push --force*"` means starts with that. See bash_policy.py for the
    # full pattern language.
    "bash": {
        # #472: shipped default flipped block→ask for the interactive
        # native-hook surface — an unlisted command surfaces a one-shot
        # permissionDecision=ask instead of hard-blocking (usability so
        # host governance can stay ON). Operators set `bash.default` back
        # to "block" to restore hard default-deny. Fail-closed contexts
        # are unaffected: evaluate_destructive_floor never reads this,
        # locked families (_JUDGE_DENYLIST) can never escalate to ask,
        # and a project with NO [bash] table still fails closed entirely.
        "default": "ask",
        "allow": {
            "cd": ["*"],
            "ls": ["*"],
            "pwd": ["*"],
            "which": ["*"],
            "where": ["*"],
            "echo": ["*"],
            "wc": ["*"],
            "mkdir": ["*"],
            "env": ["*"],
            "export": ["*"],
            "set": ["*"],
            "cat": ["*"],
            "head": ["*"],
            "tail": ["*"],
            "type": ["*"],
            "file": ["*"],
            "stat": ["*"],
            "du": ["*"],
            "df": ["*"],
            # Read-only network diagnostics (2026-06-11). These MUTATE
            # NOTHING — they resolve names / probe reachability. Pre-fix
            # they were default-blocked like `rm`, so a DNS lookup minted
            # a "destructive_action" escalation. Allowlisted here so they
            # stop default-blocking; their DESTINATION is still gated by
            # the heuristic_judge egress allowlist (NET_DNS_LOOKUP /
            # _DNS_TOOL_PATTERN), so a lookup of an unallowlisted host
            # still refuses — but as network_egress, not destruction.
            "nslookup": ["*"],
            "dig": ["*"],
            "host": ["*"],
            "drill": ["*"],
            "kdig": ["*"],
            "ping": ["*"],
            "ping6": ["*"],
            "traceroute": ["*"],
            "tracert": ["*"],
            "tracepath": ["*"],
            "find": ["*"],  # deny table below blocks -delete / -exec rm
            # Read-only searchers — same class as cat/head/tail: the read
            # TARGET is still gated by command_read_intent (so `grep /etc/shadow`
            # is refused), giving parity for "grep of own task output".
            "grep": ["*"],
            "egrep": ["*"],
            "fgrep": ["*"],
            "rg": ["*"],
            "basename": ["*"],
            "dirname": ["*"],
            "realpath": ["*"],
            "readlink": ["*"],
            "diff": ["*"],
            "cmp": ["*"],
            "sort": ["*"],
            "uniq": ["*"],
            "cut": ["*"],
            "tr": ["*"],
            "printf": ["*"],
            "date": ["*"],
            "test": ["*"],
            "true": ["*"],
            "false": ["*"],
            "jq": ["*"],
            "yq": ["*"],
            "python": ["*"],
            "python3": ["*"],
            "pytest": ["*"],
            "pip": ["*"],
            "pip3": ["*"],
            "uv": ["*"],
            "git": ["*"],
            "gh": ["*"],
            "npm": ["*"],
            "npx": ["*"],
            "node": ["*"],
            "pnpm": ["*"],
            "yarn": ["*"],
            "bun": ["*"],
            "tsc": ["*"],
            "eslint": ["*"],
            "prettier": ["*"],
            "vite": ["*"],
            "vitest": ["*"],
            "jest": ["*"],
            "webpack": ["*"],
            "rollup": ["*"],
            "esbuild": ["*"],
            "dotnet": ["*"],
            "cargo": ["*"],
            "rustc": ["*"],
            "go": ["*"],
            "javac": ["*"],
            "java": ["*"],
            "gradle": ["*"],
            "mvn": ["*"],
            "ant": ["*"],
            "docker": ["*"],
            "docker-compose": ["*"],
            "podman": ["*"],
            "make": ["*"],
            "cmake": ["*"],
            "ninja": ["*"],
            "black": ["*"],
            "ruff": ["*"],
            "mypy": ["*"],
            "flake8": ["*"],
            "isort": ["*"],
            "pylint": ["*"],
            "bandit": ["*"],
            "wsl": ["*"],
            "pwsh": ["*"],
            "powershell": ["*"],
            "curl": ["*"],
            "wget": ["*"],
            "tar": ["*"],
            "unzip": ["*"],
            "zip": ["*"],
            "cp": ["*"],
            "mv": ["*"],
            "touch": ["*"],
            "psql": ["*"],
        },
        "deny": {
            # Defense-in-depth: destructive variants stay denied even when
            # the heuristic judge is disabled. The judge is the main block;
            # these are belt-and-suspenders for common footguns.
            "git": [
                "reset --hard*",
                "push --force*",
                "push -f*",
                "push --force-with-lease*",
                "clean -fd*",
                "clean -fdx*",
                "checkout -- *",
            ],
            "find": ["* -delete*", "* -exec rm*"],
            "rm": ["*"],  # raw rm never allowed through bash gate
            "rmdir": ["*"],
            "del": ["*"],
            "shutdown": ["*"],
            "reboot": ["*"],
            "kill": ["-9 *"],  # allow kill <pid>; block kill -9
        },
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
    # LEGACY best-effort locator (aidocs.toml is deprecated; config is sqlite-only).
    # A dropped-priv runtime (the WebMCP gate runs as `app`) may be unable to
    # traverse the HOME candidate -> is_file() raises. Inaccessible == absent.
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


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
        if candidate is None:
            continue
        # Deprecated best-effort locator: an inaccessible candidate (e.g. cwd=/root
        # under the dropped-priv gate runtime) must never crash the caller (the
        # indexer). Treat a PermissionError/OSError as "not found".
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _find_config_file() -> Path | None:
    """Find the most specific global config: user-global first, then distribution."""
    return _find_user_config_file() or _find_distribution_config_file()


# NOTE (SQL-canonical doctrine): there is intentionally NO runtime config
# file reader here. AIDOCS config authority is sqlite-only — resolved through
# LayeredConfigResolver (factory > global > project > session, all DB-backed).
# The former `_load_config_file` (tomllib read of aidocs.toml) was removed
# 2026-05-21: it had no callers and a loose aidocs.toml must never be a
# runtime authority. The ConfigResolver path methods below COMPUTE paths only
# (diagnostics / one-shot import targets); they never read file content for
# runtime decisions. TOML→sqlite import lives in ConfigStore.import_from_toml
# (IMPORT_ONLY), never on the read path.


def _insert_dotted(tree: dict[str, Any], dotted: str, value: object) -> None:
    parts = dotted.split(".")
    node: dict[str, Any] = tree
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _merge_dicts(base: dict[str, Any], override: dict[str, object]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dicts(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


#: Resolution status of the gate-message / tool-description catalog (#605).
#:
#: ``_load_action_hook_defaults()`` returned ``{}`` through FOUR different
#: failure paths, so no caller could tell "this catalog is genuinely empty"
#: from "the import failed", "the db is missing" or "sqlite raised". A falsy
#: default standing in for an unresolved value is the shape behind #605: the
#: surface pin had to SNIFF the payload for canary docstring text to guess
#: which of those had happened, and when it guessed wrong the pin skipped
#: ITSELF for an unclassified reason and failed a green deploy closed.
#:
#: An unresolved catalog is now REPRESENTABLE and DISTINGUISHABLE from a
#: resolved one.
CATALOG_LOADED = "loaded"
CATALOG_EMPTY = "empty"
CATALOG_UNAVAILABLE = "unavailable"


class CatalogLoad(NamedTuple):
    """The catalog, plus WHETHER IT WAS RESOLVED and — if not — why."""

    payload: dict[str, Any]
    status: str
    detail: str

    @property
    def resolved(self) -> bool:
        """True iff the store was actually read. ``empty`` counts as resolved:
        a store that answered with zero rows is a MEASUREMENT; a store that
        could not be read is not."""
        return self.status in (CATALOG_LOADED, CATALOG_EMPTY)


def load_action_hook_defaults_resolved() -> CatalogLoad:
    """Bulk-load gate / interaction strings from the empire SQLite store,
    reporting the resolution status alongside the payload.

    One ``SELECT key, body FROM gate_message_strings WHERE lang='en'`` per cache
    build; rows are re-nested into a dict so ``_get_dotted`` lookups (and
    therefore ``render_interaction_text``) keep working unchanged.

    Every failure path names itself. Callers that only want the payload use
    :func:`_load_action_hook_defaults`; callers that must not confuse an absent
    catalog with an empty one (the #533 surface pin, the degraded-surface
    warning) read ``status``.
    """
    try:
        from .intent_tokens_store import empire_db_path
    except Exception as exc:  # noqa: BLE001 — named, not swallowed
        return CatalogLoad({}, CATALOG_UNAVAILABLE, f"import failed: {type(exc).__name__}")
    import sqlite3

    # #755/#756: the ONE canonical connect. The gate-message catalog read
    # was `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION
    # context manager, which commits and NEVER closes the handle -- with no
    # pragmas. It is a pure SELECT on the empire db and runs on the
    # governed path, so read_only=True is both truthful and the cheap mode.
    from ._sqlite_connect import connect as _canonical_connect

    try:
        db = empire_db_path()
    except Exception as exc:  # noqa: BLE001 — named, not swallowed
        return CatalogLoad(
            {}, CATALOG_UNAVAILABLE, f"empire db path unresolvable: {type(exc).__name__}"
        )
    if not db.exists():
        # SELF-SEED ON FIRST READ (2026-07-26). Returning {} here meant a FRESH
        # INSTALL silently served a DIFFERENT TOOL SURFACE: these rows carry the
        # curated tool descriptions that override every tool's docstring, so
        # without them fastmcp/griffe parses each function's `Args:` block instead
        # and the wire inputSchema of every mode-dispatched tool changes (#533).
        #
        # The rows ship in the bundled factory DB, so "absent" is a state that
        # repairs itself — init_db() creates the schema and copies the factory
        # vocabulary + messages, and is idempotent. Not doing it here is why the
        # deploy proof root (a fresh HOME) advertised raw docstrings while the
        # provisioned server advertised curated ones, which is what made the
        # surface golden get pinned to the wrong side.
        #
        # Still fail-OPEN — a seed failure must never break a config read — but
        # no longer fail-SILENT (#605): the outcome is reported, so a caller that
        # needs a provisioned catalog can refuse instead of guessing.
        try:
            from .intent_tokens_store import init_db as _empire_init_db

            _empire_init_db()
        except Exception as exc:  # noqa: BLE001 — named, not swallowed
            return CatalogLoad(
                {}, CATALOG_UNAVAILABLE, f"self-seed failed: {type(exc).__name__}"
            )
        if not db.exists():
            return CatalogLoad(
                {}, CATALOG_UNAVAILABLE, "self-seed ran but the empire db still absent"
            )
    merged: dict[str, Any] = {}
    try:
        with _canonical_connect(str(db), read_only=True, row_factory=False) as conn:
            rows = conn.execute(
                "SELECT key, body FROM gate_message_strings WHERE lang='en'",
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        return CatalogLoad(
            {}, CATALOG_UNAVAILABLE, f"gate_message_strings unreadable: {type(exc).__name__}"
        )
    for key, body in rows:
        if not isinstance(key, str) or not isinstance(body, str):
            continue
        _insert_dotted(merged, key, body)
    if not merged:
        # The store ANSWERED, with nothing. Distinct from "could not ask".
        return CatalogLoad({}, CATALOG_EMPTY, "gate_message_strings has no en rows")
    return CatalogLoad(merged, CATALOG_LOADED, "")


def _load_action_hook_defaults() -> dict[str, object]:
    """Payload-only view of :func:`load_action_hook_defaults_resolved`.

    Kept because most callers legitimately want "the strings, or nothing" and
    have a per-key default anyway. A caller that must NOT read an unresolved
    catalog as an empty one reads the resolved form instead (#605)."""
    return load_action_hook_defaults_resolved().payload


# Populated once at import — invalidated by reload_config_caches()
_ACTION_HOOK_DEFAULTS: dict[str, object] = _load_action_hook_defaults()


def reload_config_caches() -> None:
    """Invalidate all module-level config caches. Call after editing config files."""
    global _ACTION_HOOK_DEFAULTS
    _ACTION_HOOK_DEFAULTS = _load_action_hook_defaults()
    # Also invalidate action token cache
    try:
        from .intent_guard import _invalidate_intent_token_cache

        _invalidate_intent_token_cache()
    except ImportError:
        pass
    # Drop any active request-scoped config layer-rows cache too.
    try:
        from .config_resolver import invalidate_request_config_scope

        invalidate_request_config_scope()
    except Exception:
        pass


def _get_dotted(config: dict[str, object], key: str) -> object | None:
    current: object = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _parse_string_list(value: object) -> list[str]:
    """Split the TOML/env encoding of a list, where "a,b,c" IS three items.

    This serves values authored as a single line of text
    (`index.extra_skip_dirs`, `index.extra_module_hints`) whose documented
    input form is comma-separated. It is NOT the right reader for a
    catalog `string_list` setting resolved out of the config store —
    use `coerce_string_list` / `get_string_list` for those. The two
    differ ONLY in how they treat a scalar `str`, and that divergence is
    deliberate; see `coerce_string_list`.
    """
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def coerce_string_list(raw: object) -> list[str]:
    """Normalise a catalog `string_list` value into a real `list[str]`.

    THE BUG THIS EXISTS FOR (measured live 2026-08-28). The operator set
    `security.egress_allowlist` through the Dashboard and the value landed
    in the config store as a SCALAR STRING rather than a one-element list.
    The consumer did the obvious thing —
    `[str(x) for x in (raw or []) if str(x).strip()]` — and a `str`
    iterates into CHARACTERS, so a 14-character address became fourteen
    one-character "hosts": ['1','9','8','.','5','1', ...]. The
    operator's deliberate, dashboard-authenticated T0 entry did NOTHING,
    with no error anywhere, while the junk one-character entries were
    themselves live and matchable. Every `string_list` setting in the
    catalog has that same hole, so the coercion lives here once
    (Doctrine XXII) instead of at each reader.

    Shape law:
      * `None`            -> `[]`
      * a `str`           -> exactly ONE stripped element (`[]` if blank)
      * a `list`/`tuple`  -> stripped, non-empty elements
      * anything else     -> one stringified element

    A scalar is NOT comma-split. A host never contains a comma, but other
    `string_list` settings might, and splitting would INVENT allowlist
    entries the operator never wrote. One element is predictable; a split
    is a guess, and a guess that widens a security allowlist is the wrong
    kind of guess.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        return [stripped] if stripped else []
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in raw if str(item).strip()]
    stripped = str(raw).strip()
    return [stripped] if stripped else []


def get_string_list(
    key: str,
    *,
    project_root: Path | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Resolve a catalog `string_list` setting and coerce it (see above).

    The single seam every `string_list` reader should use. Never raises:
    a config-layer failure yields `[]`, so a security gate reading its
    allowlist degrades to default-deny rather than exploding into the
    caller.
    """
    try:
        raw = get_setting(
            key,
            project_root=project_root,
            session_id=session_id,
            default=[],
        )
    except Exception:
        return []
    return coerce_string_list(raw)


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
        """Build the full effective config dict via the canonical resolver.

        Phase 4b (2026-05-02): each top-level namespace resolves
        through LayeredConfigResolver so this shares semantic with
        get_setting / get_effective. The previous code-defaults +
        DB-overlay deep-merge lost operator full-namespace REPLACE
        intent for direct-row writes; the resolver preserves it.

        Action hooks (TOML) overlay on top — not part of the
        operator-config cascade.
        """
        from .config_resolver import LayeredConfigResolver
        from .config_schema import SETTINGS_CATALOG

        merged: dict[str, Any] = {}
        # Action hooks / interaction templates from TOML (static, shipped with MCP)
        hooks = _ACTION_HOOK_DEFAULTS if self is _DEFAULT_RESOLVER else _load_action_hook_defaults()
        _merge_dicts(merged, hooks)

        # Resolve every top-level namespace through the canonical
        # resolver. Universe = _DEFAULT_CONFIG roots ∪ catalog roots.
        top_keys: set[str] = set(_DEFAULT_CONFIG.keys())
        for catalog_key in SETTINGS_CATALOG:
            top_keys.add(catalog_key.split(".", 1)[0])
        resolver = LayeredConfigResolver()
        # Batch the per-layer DB reads: each layer DB is read ONCE into this
        # cache and filtered in memory per key, instead of one open/query/close
        # per top namespace. Verdict-identical (every key sees the same rows it
        # would have queried) and a consistent single-snapshot read per call.
        rows_cache: dict = {}
        for top_key in top_keys:
            try:
                resolved = resolver.resolve(
                    top_key,
                    project_root,
                    session_id=session_id,
                    rows_cache=rows_cache,
                )
            except Exception:
                continue
            if resolved.value is None:
                continue
            existing = merged.get(top_key)
            if isinstance(resolved.value, dict) and isinstance(existing, dict):
                _merge_dicts(existing, resolved.value)
            else:
                merged[top_key] = resolved.value
        return merged

    def config_layer_paths(
        self,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> dict[str, Path | None]:
        """Return the SQLite config store path for diagnostics."""
        if project_root is None:
            return {"sqlite": None}
        return {"sqlite": project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"}

    def write_resolved_config(
        self,
        *,
        project_root: Path,
        session_id: str | None = None,
    ) -> None:
        """Persist the merged config snapshot into ``aidocs.sqlite3``
        (``resolved_config`` table) so host plugins can read it without
        opening a JSON sidecar file.

        Replaces the old ``.MEMORY/config/resolved-config.json`` writer
        (2026-04-20). The ResolvedConfigStore ingests any leftover JSON
        sidecar on first init and hard-deletes it so the project never
        carries two sources of truth.
        """
        from datetime import datetime

        from .resolved_config_store import ResolvedConfigStore

        effective = self.effective_config(project_root=project_root, session_id=session_id)
        layers = self.config_layer_paths(project_root=project_root, session_id=session_id)
        layers_payload = {k: str(v) if v else None for k, v in layers.items()}
        active_layers = [k for k, v in layers.items() if v is not None and v.is_file()]
        ResolvedConfigStore().set(
            project_root,
            resolved=effective,
            layers=layers_payload,
            active_layers=active_layers,
            last_updated=datetime.now(UTC).isoformat(),
        )

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
            self.effective_config(project_root=project_root, session_id=session_id),
            key,
        )

    def get_layer_value(
        self,
        key: str,
        scope: str,
        *,
        project_root: Path | None = None,
        session_id: str | None = None,
    ) -> object | None:
        """Read a single config layer's raw value for a key.

        Returns only the SQLite override for this scope — TOML values are
        treated as defaults and not reported as scope overrides.
        """
        if project_root is not None:
            try:
                from .config_store import ConfigStore

                scope_key = session_id or "" if scope == "session" else ""
                return ConfigStore().get(project_root, key, scope=scope, scope_key=scope_key)
            except Exception:
                pass
        return None

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
            _SafeFormatDict({str(name): value for name, value in kwargs.items()}),
        )


_DEFAULT_RESOLVER = ConfigResolver()
_DEFAULT_EFFECTIVE_CONFIG = _DEFAULT_RESOLVER.effective_config()


INDEX_ENABLED_LANGUAGES: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.enabled_languages") or "all",
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
    """Read a setting via the canonical 5-layer resolver.

    Phase 4 (2026-05-02): delegates to ConfigResolver. Cascades
    factory > global > project > session. Factory reads from both
    _DEFAULT_CONFIG (nested namespaces) and catalog leaf defaults.

    project_root=None resolves factory + global only.
    Raises RemovedSettingError when the key is hard-removed.
    """
    from .config_resolver import LayeredConfigResolver
    from .config_store import RemovedSettingError

    try:
        resolved = LayeredConfigResolver().resolve(
            key,
            project_root,
            session_id=session_id,
        )
        return default if resolved.value is None else resolved.value
    except RemovedSettingError:
        raise
    except Exception:
        return default


def resolve_include_tests(
    explicit: bool | None,
    *,
    project_root: Path | None = None,
    session_id: str | None = None,
) -> bool:
    """Resolve the effective `include_tests` for a search/index call.

    `explicit` is the value the caller passed: None means "not specified"
    and the project's `index.include_tests` setting decides; an explicit
    True/False is honored verbatim (config ignored). This is the single
    point that lets a project opting into indexed tests
    (index.include_tests=true) have its tests searchable by DEFAULT,
    instead of every search tool hardcoding False.
    """
    if explicit is not None:
        return bool(explicit)
    return bool(
        get_setting(
            "index.include_tests",
            project_root=project_root,
            session_id=session_id,
            default=False,
        ),
    )


# ── Legacy module-level constants ──
# Kept for backward compatibility with tests that monkeypatch them.
# Production code should use get_setting() or effective_config() instead.

TOOLS_CALL_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.tool_call_timeout") or 10,
)
TOOLS_SYNC_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.sync_write_timeout") or 60,
)
TOOLS_GIT_TIMEOUT: int = int(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.git_functions_timeout") or 30,
)
TOOLS_MAX_TIMEOUT: int = int(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "tools.max_timeout") or 120)


# DEV_MODE constant removed (2026-06-12): dev authority is flavour-derived
# (see enforcement.dev_mode_authorized), not a config flag.

_TOOL_OUTPUT_SECRET_POLICY_VALUES = ("redact", "report_only", "allow_raw")
TOOL_OUTPUT_SECRET_POLICY: str = (
    str(_get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.tool_output_secret_policy") or "redact")
    .strip()
    .lower()
)
if TOOL_OUTPUT_SECRET_POLICY not in _TOOL_OUTPUT_SECRET_POLICY_VALUES:
    TOOL_OUTPUT_SECRET_POLICY = "redact"

# Derived booleans for callsites that only need yes/no decisions.
# OUTPUT_GUARD_ENABLED: scan or skip entirely.
# OUTPUT_GUARD_REDACT: when scanning, modify text or report_only.
OUTPUT_GUARD_ENABLED: bool = TOOL_OUTPUT_SECRET_POLICY != "allow_raw"
OUTPUT_GUARD_REDACT: bool = TOOL_OUTPUT_SECRET_POLICY == "redact"

_raw_exempt_ext = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.exempt_extensions")
GATE_EXEMPT_EXTENSIONS: list[str] = (
    list(_raw_exempt_ext) if isinstance(_raw_exempt_ext, list) else []
)
_raw_exempt_paths = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.exempt_paths")
GATE_EXEMPT_PATHS: list[str] = (
    list(_raw_exempt_paths) if isinstance(_raw_exempt_paths, list) else []
)
_raw_protected = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.protected_patterns")
GATE_PROTECTED_PATTERNS: list[str] = (
    list(_raw_protected) if isinstance(_raw_protected, list) else []
)
# Operator EXTRA globs that EXTEND the hard-protected DATA floor (sqlite DBs,
# AIDOCS index, gate-state JSON — see hard_protected_paths.CORE_HARD_PROTECTED_GLOBS).
# Config can only ADD to the immutable core, never shrink it.
_raw_hard_protected = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.hard_protected")
GATE_HARD_PROTECTED_PATTERNS: list[str] = (
    list(_raw_hard_protected) if isinstance(_raw_hard_protected, list) else []
)
# Read deny-list + per-role/user read ACL (Empire 2026-06-13). Reads are
# permitted by default; these only TIGHTEN. security.read_deny = globs refused
# for reads; security.read_acl = [{pattern, roles?, users?}] re-opening a denied
# file to named principals. See read_access_policy.py.
_raw_read_deny = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.read_deny")
GATE_READ_DENY: list[str] = list(_raw_read_deny) if isinstance(_raw_read_deny, list) else []
_raw_read_acl = _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "security.read_acl")
GATE_READ_ACL: list[dict] = (
    [e for e in _raw_read_acl if isinstance(e, dict)] if isinstance(_raw_read_acl, list) else []
)
# GATE_BASH_ALLOWED removed 2026-04-25 — security.bash_allowed
# legacy substring allowlist superseded by the canonical [bash] table.
INDEX_INCLUDE_TESTS: bool = _parse_bool(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "index.include_tests"),
    default=False,
)

CODE_QUALITY_COMMENT_ENFORCEMENT: str = str(
    _get_dotted(_DEFAULT_EFFECTIVE_CONFIG, "code_quality.comment_enforcement") or "advisory",
)


def render_interaction_text(
    key: str,
    *,
    project_root: Path | None = None,
    session_id: str | None = None,
    **kwargs: object,
) -> str:
    # Use a fresh resolver so tests and runtime env/path changes (AIDOCS_PATH,
    # custom gate_messages roots) are reflected immediately.
    return ConfigResolver().render_text(
        key,
        project_root=project_root,
        session_id=session_id,
        **kwargs,
    )
