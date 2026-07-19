from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .git_helpers import run_git_sync as _run_git_sync

# PERF (2026-05-26): mtime-keyed cache for project_origins. The dashboard
# refresh path calls repo_summary every tick, which previously fired a fresh
# `git remote -v` subprocess every time (~46 ms on Windows). The remote
# configuration is keyed off .git/config; if that file's mtime is unchanged,
# the parsed result is byte-identical to last call. Cache keyed by
# (project_root_resolved, .git/config mtime_ns) → invalidates the instant
# the user runs `git remote add/set-url/remove` (which always rewrites
# .git/config). Truth-perfect: a stale entry can only persist while the
# underlying config is still the same bytes that produced it.
_ORIGINS_CACHE: dict[tuple[str, int], dict[str, object]] = {}


def _origins_cache_key(project_root: Path) -> tuple[str, int] | None:
    """Return (resolved_root, .git/config_mtime_ns) or None when the
    project is not a git repo / config is unreadable. None disables the
    cache for that call — the caller then re-runs the subprocess.
    """
    cfg = project_root / ".git" / "config"
    try:
        st = cfg.stat()
    except OSError:
        return None
    try:
        root_key = str(project_root.resolve())
    except OSError:
        root_key = str(project_root)
    return (root_key, st.st_mtime_ns)


def scope_daemon_url(daemon_url: str, project_root) -> str:
    """#280 clause 1: bind a shared-daemon URL to ONE project by adding a
    validated ``?root=<abs>`` query param. Idempotent (overwrites any existing
    ``root``), preserves the rest of the URL. The daemon's ProjectScopeMiddleware
    reads ``root`` per request and honors it ONLY for a commissioned project, so
    each window resolves to ITS OWN root instead of a process-global."""
    from pathlib import Path as _P
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(str(daemon_url))
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["root"] = str(_P(project_root))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


class RuntimeProjectSupportService:
    def __init__(self, hub: Any, logger: Any, origin_role: Any) -> None:
        self.hub = hub
        self._logger = logger
        self._origin_role = origin_role

    def ensure_claude_mcp_config(
        self, project_root: Path, interpreter: str | None = None
    ) -> dict[str, object]:
        """Ensure the project's ``.mcp.json`` carries the aidocs MCP server entry.

        ONE WRITER (2026-06 one-runtime/one-writer seal): the SQL ``mcp_servers``
        registry is authority and ``.mcp.json`` is its projection. This method
        migrates any pre-existing legacy ``.mcp.json`` into SQL (once), upserts
        the aidocs row, and regenerates the file via ``project_to_file`` — the
        SAME canonical writer ``cmd_setup`` uses. No direct hand-written JSON.

        ONE INTERPRETER: uses the explicitly-threaded ``interpreter`` (the
        AIDOCS-owned choice ``cmd_setup``/``decide_setup_interpreter`` made) when
        given; otherwise the canonical resolver (``resolve_aidocs_interpreter``).
        NEVER ``sys.executable`` — so ``project_init`` can no longer clobber
        setup's selection with the ambient interpreter that runs the CLI.

        Idempotent: an unchanged aidocs entry returns ``no_change`` without a
        rewrite. Returns a dict describing what happened.
        """
        from .mcp_registry_store import McpRegistryStore

        mcp_json_path = project_root / ".mcp.json"

        # ── Daemon mode (#249): when mcp.local_daemon_url is configured, the
        # aidocs entry is the shared local HTTP daemon (Claude Code
        # auto-reconnects http servers; the watchdog restarts crashes) instead
        # of a per-project stdio spawn. Same one-writer path: SQL row (url in
        # the command column, transport=http) -> project_to_file. ──
        daemon_url = ""
        try:
            from .config import get_setting

            daemon_url = str(get_setting("mcp.local_daemon_url", default="") or "").strip()
        except Exception:
            daemon_url = ""
        if daemon_url:
            daemon_url = scope_daemon_url(daemon_url, project_root)
            new_entry = {"type": "http", "url": daemon_url}
            prior: dict[str, object] = {}
            if mcp_json_path.is_file():
                try:
                    prior = json.loads(mcp_json_path.read_text(encoding="utf-8"))
                except Exception:
                    prior = {}
            prior_servers = prior.get("mcpServers") if isinstance(prior, dict) else {}
            prior_aidocs = (
                prior_servers.get("aidocs") if isinstance(prior_servers, dict) else None
            )
            if isinstance(prior_aidocs, dict) and prior_aidocs == new_entry:
                return {
                    "action": "no_change",
                    "path": str(mcp_json_path),
                    "reason": "aidocs daemon entry already present and correct",
                }
            store = McpRegistryStore()
            store.migrate_legacy_once(project_root)
            store.upsert(
                project_root,
                "aidocs",
                command=daemon_url,
                args=[],
                transport="http",
                source="aidocs",
            )
            store.project_to_file(project_root)
            action = "updated" if isinstance(prior_aidocs, dict) else "created"
            return {"action": action, "path": str(mcp_json_path), "entry": new_entry}

        # ── ONE interpreter (never ambient sys.executable) ──
        python_bin = (interpreter or "").strip()
        if not python_bin:
            try:
                from .claude_hooks_install import resolve_aidocs_interpreter

                python_bin = str(resolve_aidocs_interpreter().get("path") or "").strip()
            except Exception:
                python_bin = ""
        if not python_bin:
            python_bin = shutil.which("python") or shutil.which("python3") or "python"

        # ── env: ALWAYS carry AIDOCS_PROJECT_ROOT; add PYTHONPATH only in
        # source-checkout mode (aidocs_mcp not importable without it). Fixes the
        # prior bug where PYTHONPATH mode dropped AIDOCS_PROJECT_ROOT entirely. ──
        env: dict[str, str] = {"AIDOCS_PROJECT_ROOT": str(project_root)}
        aidocs_source_root = Path(__file__).resolve().parents[3]
        env_aidocs_path = os.environ.get("AIDOCS_PATH")
        if env_aidocs_path and Path(env_aidocs_path).is_dir():
            aidocs_source_root = Path(env_aidocs_path)
        try:
            import importlib.util

            if importlib.util.find_spec("aidocs_mcp") is None:
                env["PYTHONPATH"] = str(aidocs_source_root / "mcp" / "server")
        except Exception:
            env["PYTHONPATH"] = str(aidocs_source_root / "mcp" / "server")

        args = ["-m", "aidocs_mcp.mcp_server"]
        new_entry = {"type": "stdio", "command": python_bin, "args": args, "env": env}

        # Idempotence: diff the prior ON-DISK aidocs entry against the target.
        prior: dict[str, object] = {}
        if mcp_json_path.is_file():
            try:
                prior = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self._logger.warning("Failed to parse existing .mcp.json: %s", exc)
                prior = {}
        prior_servers = prior.get("mcpServers") if isinstance(prior, dict) else {}
        prior_aidocs = prior_servers.get("aidocs") if isinstance(prior_servers, dict) else None
        if isinstance(prior_aidocs, dict) and prior_aidocs == new_entry:
            return {
                "action": "no_change",
                "path": str(mcp_json_path),
                "reason": "aidocs MCP entry already present and correct",
            }

        # ── ONE canonical writer: migrate legacy → upsert → project ──
        store = McpRegistryStore()
        # Bring any operator-placed legacy servers (e.g. playwright) into SQL so
        # the projection below does not drop them. Sealed-once + validated.
        store.migrate_legacy_once(project_root)
        store.upsert(
            project_root,
            "aidocs",
            command=python_bin,
            args=args,
            transport="stdio",
            source="aidocs",
            env=env,
        )
        store.project_to_file(project_root)
        action = "updated" if isinstance(prior_aidocs, dict) else "created"
        return {"action": action, "path": str(mcp_json_path), "entry": new_entry}

    def ensure_claude_project_settings(self, project_root: Path) -> dict[str, object]:
        """Ensure the project's `.claude/settings.json` opts Claude Code out of auto-loading CLAUDE.md.

        Writes `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` into the project-scoped settings file
        when the `disable_global_claude_md` toggle is on (currently hardcoded to True;
        aidocs.toml-driven config is a follow-up). Operation is idempotent:
        - Other keys inside `env` are preserved.
        - Other top-level keys (hooks, permissions, etc.) are preserved.
        - No-op when the value is already `"1"`.

        Returns a dict describing the action taken.
        """
        if not project_root.exists() or not project_root.is_dir():
            raise FileNotFoundError(
                f"project_root does not exist or is not a directory: {project_root}",
            )

        # TODO(config): replace with aidocs.toml [host.claude].disable_global_claude_md read.
        toggle_on = True

        claude_dir = project_root / ".claude"
        settings_path = claude_dir / "settings.json"

        existing: dict[str, object] = {}
        existed_before = settings_path.is_file()
        if existed_before:
            try:
                raw = settings_path.read_text(encoding="utf-8")
                parsed = json.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    existing = parsed
                else:
                    self._logger.warning(
                        "Existing .claude/settings.json is not a JSON object; leaving alone: %s",
                        settings_path,
                    )
                    return {
                        "action": "no_change",
                        "path": str(settings_path),
                        "env": {},
                        "reason": "existing settings.json is not a JSON object",
                    }
            except Exception as exc:
                self._logger.warning(
                    "Failed to parse existing .claude/settings.json (%s); leaving alone: %s",
                    settings_path,
                    exc,
                )
                return {
                    "action": "no_change",
                    "path": str(settings_path),
                    "env": {},
                    "reason": f"failed to parse existing settings.json: {exc}",
                }

        env_block = existing.get("env")
        if not isinstance(env_block, dict):
            env_block = {}
        # Work on a shallow copy so we can detect whether we actually changed anything.
        new_env: dict[str, object] = dict(env_block)

        target_key = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
        target_value = "1"

        if toggle_on:
            current_value = new_env.get(target_key)
            if current_value == target_value:
                return {
                    "action": "no_change",
                    "path": str(settings_path),
                    "env": dict(new_env),
                }
            new_env[target_key] = target_value
        # Toggle is off: remove only the AIDOCS-shaped value ("1"); leave operator overrides alone.
        elif new_env.get(target_key) == target_value:
            new_env.pop(target_key, None)
        else:
            # Nothing to remove.
            return {
                "action": "no_change",
                "path": str(settings_path),
                "env": dict(new_env),
            }

        new_settings: dict[str, object] = dict(existing)
        if new_env:
            new_settings["env"] = new_env
        # Keep the key off the file if it would be empty AND wasn't present before.
        elif "env" in new_settings and not new_env:
            new_settings.pop("env", None)

        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(new_settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        action = "updated" if existed_before else "created"
        return {
            "action": action,
            "path": str(settings_path),
            "env": dict(new_env),
        }

    def project_origins(self, project_root: Path) -> dict[str, object]:
        # PERF (2026-05-26): hit the .git/config mtime-keyed cache before
        # spawning git. On the dashboard hot path this turns a 46 ms
        # subprocess into a ~0.05 ms dict lookup; invalidates the instant
        # the operator changes a remote (git rewrites .git/config). When
        # the project is not a git repo OR config is unreadable, key is
        # None and we fall through to the original subprocess path.
        cache_key = _origins_cache_key(project_root)
        if cache_key is not None:
            cached = _ORIGINS_CACHE.get(cache_key)
            if cached is not None:
                # DEEP COPY (2026-05-26): a shallow dict() copy left nested
                # structures (remotes list of dicts, roles dict of lists,
                # notes list) shared with the cached entry — a caller
                # mutating `result["remotes"][0]["url"]` would corrupt the
                # cache for every subsequent call. The contract claims
                # caller-mutation isolation; honor it at every nesting
                # depth via copy.deepcopy. Cost is trivial (a few-element
                # dict of small lists), correctness is absolute.
                return copy.deepcopy(cached)
        result: dict[str, object] = {
            "git_repo": (project_root / ".git").exists(),
            "remotes": [],
            "roles": {},
            "notes": [],
        }
        try:
            remote_output = _run_git_sync(str(project_root), "remote", "-v")
        except FileNotFoundError:
            result["notes"] = ["git not installed"]
            return result
        except Exception as exc:
            result["notes"] = [str(exc)]
            return result

        remotes: dict[tuple[str, str], dict[str, object]] = {}
        for line in remote_output.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, url, kind = parts[0], parts[1], parts[2].strip("()")
            key = (name, url)
            entry = remotes.setdefault(
                key,
                {
                    "name": name,
                    "url": url,
                    "fetch": False,
                    "push": False,
                    "role": self._origin_role(name, url),
                },
            )
            if kind == "fetch":
                entry["fetch"] = True
            if kind == "push":
                entry["push"] = True

        entries = list(remotes.values())
        result["remotes"] = entries
        roles: dict[str, list[str]] = {}
        for entry in entries:
            role = str(entry.get("role") or "other")
            roles.setdefault(role, []).append(str(entry.get("name")))
        result["roles"] = roles

        notes: list[str] = []
        if roles.get("private") and roles.get("public"):
            notes.append("private/public split detected")
        elif roles.get("private"):
            notes.append("private remote detected")
        elif roles.get("public"):
            notes.append("public remote detected")
        result["notes"] = notes
        # PERF (2026-05-26): cache write keyed by .git/config mtime — see
        # _origins_cache_key. Only stores when the project has a readable
        # config (cache_key not None); error-path returns above never hit
        # the cache so a transient subprocess failure isn't memoized.
        if cache_key is not None:
            # Symmetric to the read path: store a deep copy so the
            # returned `result` the caller now owns is fully decoupled
            # from the cached entry. Without this, the FIRST caller could
            # mutate the result and corrupt subsequent cache hits.
            _ORIGINS_CACHE[cache_key] = copy.deepcopy(result)
        return result

    # _load_project_rules was retired 2026-05-21: bootstrap-law rules are
    # now served from the MemPalace bridge (PalaceService.get_bootstrap_context)
    # via RuntimeService._load_project_rules, NOT by globbing .MEMORY/rules/*.md.
    # See the AIDOCS non-code-memory exit. Markdown is import/export only.

    def repo_summary(
        self,
        project_root: Path,
        *,
        freshness_mode: str = "deep",
        session_count: int | None = None,
    ) -> dict[str, object]:
        """``freshness_mode``:
          * ``deep``  (default) — full ``_code_freshness`` sha256 walk; precise
            on-disk drift count. Used by bootstrap reports.
          * ``cheap`` — sitter's DB-only signal (known-stale flag + db_state +
            poll-window) — NO filesystem walk / NO sha256. The sanctioned hot-path
            freshness truth (lane 1d); used by the dashboard first paint.
          * ``skip``  — no freshness bullet at all.
        The returned dict carries ``freshness_check`` so consumers know which
        check produced the bullet (cheap is poll-based, not a deep drift count).
        """
        code_files = 0
        modules = 0
        parsed = 0
        schema_entities = 0
        schema_fields = 0
        # NB: session_count is intentionally NOT initialized here — the
        # kwarg above is None by default and the `if session_count is None`
        # branch below decides whether to fetch it via list_sessions. A
        # local `session_count = 0` here would shadow the kwarg and kill
        # the same-call reuse path used by dashboard_snapshot.
        language_tiers: dict[str, int] = {}
        language_sources: dict[str, int] = {}

        try:
            with self.hub.code.connect(project_root) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(parsed), 0) FROM code_files",
                ).fetchone()
                if row:
                    code_files = int(row[0] or 0)
                    parsed = int(row[1] or 0)
                row = conn.execute("SELECT COUNT(*) FROM code_modules").fetchone()
                if row:
                    modules = int(row[0] or 0)
                for row in conn.execute(
                    "SELECT COALESCE(language_tier, 'unknown') AS tier, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_tier, 'unknown')",
                ):
                    language_tiers[str(row["tier"])] = int(row["count"] or 0)
                for row in conn.execute(
                    "SELECT COALESCE(language_source, 'unknown') AS source, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_source, 'unknown')",
                ):
                    language_sources[str(row["source"])] = int(row["count"] or 0)
        except Exception:
            pass

        try:
            with self.hub.schema.connect(project_root) as conn:
                row = conn.execute("SELECT COUNT(*) FROM schema_entities").fetchone()
                if row:
                    schema_entities = int(row[0] or 0)
                row = conn.execute("SELECT COUNT(*) FROM schema_fields").fetchone()
                if row:
                    schema_fields = int(row[0] or 0)
        except Exception:
            pass

        # PERF (2026-05-26): if the caller already has the session list (e.g.
        # dashboard_snapshot computes it for its own use), avoid the duplicate
        # directory walk. Default None preserves byte-identical behavior for
        # every other caller (bootstrap, report builders) that doesn't pass it.
        if session_count is None:
            try:
                session_count = len(self.hub.sessions.list_sessions(project_root))
            except Exception:
                session_count = 0

        # Surface code-index staleness loudly (operator doctrine
        # 2026-04-30 / co-co review: light bootstrap path skips the
        # rebuild but MUST NOT hide stale state — make freshness
        # visible at the report-bullet level so an operator skimming
        # the bootstrap result sees drift without digging into the
        # nested code_manifest payload).
        freshness_bullet: str | None = None
        if freshness_mode == "deep":
            try:
                freshness = self.hub.code._code_freshness(project_root)
                if isinstance(freshness, dict):
                    state = str(freshness.get("state") or "").lower()
                    if state == "stale":
                        drifted = freshness.get("drifted_paths") or []
                        drift_count = len(drifted) if isinstance(drifted, list) else 0
                        reasons = freshness.get("reasons") or []
                        reason_str = ", ".join(str(r) for r in reasons[:3]) if reasons else ""
                        freshness_bullet = (
                            f"⚠ code index STALE: {drift_count} file(s) drifted"
                            + (f" ({reason_str})" if reason_str else "")
                            + " — run ai_index_sync to refresh"
                        )
                    elif state == "missing":
                        freshness_bullet = (
                            "⚠ code index MISSING: tracked files have no index "
                            "rows — run ai_index_sync"
                        )
            except Exception:
                pass
        elif freshness_mode == "cheap":
            # Hot-path: DB-only signal, no walk/sha256 (lane 1d). Truthful but
            # coarser than deep — never claims a precise on-disk drift count;
            # points at ai_index_status for the deep check.
            try:
                from . import project_index_sitter as _sitter

                if _sitter.is_index_known_stale(project_root):
                    freshness_bullet = (
                        "⚠ code index KNOWN-STALE (external change since last "
                        "sync) — run ai_index_sync (deep drift detail: ai_index_status)"
                    )
                else:
                    status = self.hub.code.code_index_db_status(project_root)
                    if str((status or {}).get("db_state") or "") == "unparsed":
                        freshness_bullet = "⚠ code index has UNPARSED rows — run ai_index_sync"
                    else:
                        fw = _sitter.freshness_window(project_root)
                        if str((fw or {}).get("state") or "") == "poll_window_risk":
                            freshness_bullet = (
                                "code index freshness UNVERIFIED (poll-window "
                                "risk) — ai_index_status for a deep drift check"
                            )
            except Exception:
                pass
        # freshness_mode == "skip" → no bullet

        origins = self.project_origins(project_root)
        bullets = [
            f"{code_files} indexed code files ({parsed} parsed)",
        ]
        if freshness_bullet:
            bullets.append(freshness_bullet)
        bullets.extend(
            [
                f"{modules} detected modules",
                f"{schema_entities} schema entities / {schema_fields} fields",
                f"{session_count} sessions",
            ],
        )
        if language_tiers:
            bullets.append(
                "language tiers: "
                + ", ".join(f"{k}={v}" for k, v in sorted(language_tiers.items())),
            )
        if language_sources:
            bullets.append(
                "language sources: "
                + ", ".join(f"{k}={v}" for k, v in sorted(language_sources.items())),
            )
        notes = origins.get("notes") if isinstance(origins.get("notes"), list) else []
        bullets.extend(str(note) for note in notes[:2])
        return {
            "project_root": str(project_root),
            "project_name": project_root.name,
            "code_files": code_files,
            "parsed_code_files": parsed,
            "modules": modules,
            "schema_entities": schema_entities,
            "schema_fields": schema_fields,
            "sessions": session_count,
            "language_tiers": language_tiers,
            "language_sources": language_sources,
            "origins": origins,
            "headline": f"{project_root.name}: indexed project summary",
            "bullets": bullets,
            # Which freshness check produced the bullet: "deep" = full sha256
            # drift count; "cheap" = poll-based hot-path signal (no walk),
            # deep detail via ai_index_status; "skip" = not checked.
            "freshness_check": freshness_mode,
        }

    def project_structure_gaps(self, project_root: Path) -> list[str]:
        # Stage 5c (no-file-layer): workflow-rules.md / workflow-actions.md are
        # no longer required structure — workflow rule/action definitions are
        # SQL-canonical (workflow_definitions). Requiring the legacy markdown
        # here would wedge every fresh project at "not_bootstrapped".
        # SQLite-only doctrine (2026-06): .MEMORY/INDEX.md is likewise NO LONGER
        # required structure. Marker->sqlite migration (2026-07-02): the
        # structural requirement is the COMMISSION STAMP in the sqlite index —
        # the deprecated .aidocs routing file is no longer required.
        missing: list[str] = []
        from .mcp_server_runtime_helpers import is_aidocs_managed

        if not is_aidocs_managed(project_root):
            missing.append(".MEMORY/.index/aidocs.sqlite3 (commission stamp)")
        if not ((project_root / "AGENTS.md").is_file() or (project_root / "CLAUDE.md").is_file()):
            missing.append("AGENTS.md or CLAUDE.md")
        return missing

    def _copy_missing_tree(
        self,
        source_root: Path,
        dest_root: Path,
        label_prefix: str,
        created: list[str],
        skipped: list[str],
    ) -> None:
        if not source_root.is_dir():
            return
        source_files = [path for path in source_root.rglob("*") if path.is_file()]
        for src_file in source_files:
            rel = src_file.relative_to(source_root)
            dest = dest_root / rel
            label = f"{label_prefix}/{rel.as_posix()}"
            if dest.exists():
                skipped.append(label)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dest))
            created.append(label)

    def _copy_missing_file(
        self,
        source_file: Path,
        dest_file: Path,
        label: str,
        created: list[str],
        skipped: list[str],
    ) -> None:
        if not source_file.is_file():
            return
        if dest_file.exists():
            skipped.append(label)
            return
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_file), str(dest_file))
        created.append(label)

    def _latest_mtime_ns(self, paths: list[Path]) -> int | None:
        mtimes: list[int] = []
        for path in paths:
            try:
                if path.is_file():
                    mtimes.append(path.stat().st_mtime_ns)
                elif path.is_dir():
                    for child in path.rglob("*"):
                        if child.is_file():
                            mtimes.append(child.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
        return max(mtimes) if mtimes else None

    def _index_freshness_status(self, project_root: Path) -> tuple[str, dict[str, object]]:
        memory_db = self.hub.index.db_path(project_root)
        code_db = self.hub.code.db_path(project_root)
        memory_status = self.hub.index.status(project_root)
        code_status = self.hub.code.code_status(project_root)
        memory_freshness = (
            memory_status.get("freshness")
            if isinstance(memory_status.get("freshness"), dict)
            else {}
        )
        code_freshness = (
            code_status.get("freshness") if isinstance(code_status.get("freshness"), dict) else {}
        )

        missing = [
            label
            for label, path, freshness in (
                ("memory", memory_db, memory_freshness),
                ("code", code_db, code_freshness),
            )
            if not path.is_file() or freshness.get("state") == "missing"
        ]
        if missing:
            return "missing", {
                "missing_indexes": missing,
                "memory_freshness": memory_freshness,
                "code_freshness": code_freshness,
            }

        stale_reasons: list[str] = []
        if memory_freshness.get("state") == "stale":
            stale_reasons.extend(
                f"memory:{reason}"
                for reason in memory_freshness.get("reasons", [])
                if isinstance(reason, str) and reason.strip()
            )
        if code_freshness.get("state") == "stale":
            stale_reasons.extend(
                f"code:{reason}"
                for reason in code_freshness.get("reasons", [])
                if isinstance(reason, str) and reason.strip()
            )
        if stale_reasons:
            return "stale", {
                "reasons": stale_reasons,
                "memory_freshness": memory_freshness,
                "code_freshness": code_freshness,
            }
        return "ready", {
            "reasons": [],
            "memory_freshness": memory_freshness,
            "code_freshness": code_freshness,
        }

    def repo_summary_short(self, project_root: Path) -> str:
        """Return a one-line summary of the project: name + session count + branch (if any).

        Format: "<project_name> | <N> sessions | branch:<name>"
        Falls back to "(no git)" if not a git repo.
        """
        name = project_root.name
        try:
            session_count = len(self.hub.sessions.list_sessions(project_root))
        except Exception:
            session_count = 0

        # Only look for a branch when the project ROOT itself is a git repo.
        # Without this guard git's cwd discovery walks up until it finds
        # some ancestor repo (AIDOCS's own .git, for instance), and tests
        # that deliberately skip git init see a branch from the outer
        # project — a false positive. `symbolic-ref` also succeeds before
        # the first commit (rev-parse --abbrev-ref HEAD returns literal
        # "HEAD" on an unborn branch, which looks like garbage data).
        branch_segment = "(no git)"
        git_dir = project_root / ".git"
        if git_dir.exists():
            try:
                ref = _run_git_sync(str(project_root), "symbolic-ref", "--short", "HEAD").strip()
                if ref:
                    branch_segment = f"branch:{ref}"
            except Exception:
                pass

        return f"{name} | {session_count} sessions | {branch_segment}"
