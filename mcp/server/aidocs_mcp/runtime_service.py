from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .service_hub import AidocsServiceHub

logger = logging.getLogger("aidocs.runtime")


def _run_git_sync(cwd: str, *args: str, timeout: int = 10) -> str:
    import tempfile
    out_path = err_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".out", delete=False) as f:
            out_path = f.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".err", delete=False) as f:
            err_path = f.name
        with open(out_path, "w") as out_fh, open(err_path, "w") as err_fh:
            result = subprocess.run(
                ["git", "-c", "safe.directory=*", *args],
                cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=out_fh, stderr=err_fh,
                text=True, timeout=timeout, check=False,
            )
        stdout = Path(out_path).read_text(encoding="utf-8", errors="ignore").strip()
        stderr = Path(err_path).read_text(encoding="utf-8", errors="ignore").strip()
    finally:
        for p in (out_path, err_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    if result.returncode != 0:
        message = (stderr or stdout or f"git exited with code {result.returncode}").strip()
        raise RuntimeError(message)
    return stdout


def _origin_role(name: str, url: str) -> str:
    lower_name = name.lower()
    lower_url = url.lower()
    if lower_name == "public":
        return "public"
    if lower_name == "origin" and ("private" in lower_url or "_private" in lower_url):
        return "private"
    if lower_name == "origin":
        return "primary"
    return "other"

def _resolve_action_tokens_dir() -> Path:
    """Find action_tokens directory: project root first, then legacy MCP location."""
    candidates = [
        Path(__file__).resolve().parents[3] / "action_tokens",  # project root
        Path(__file__).resolve().parent / "action_tokens",       # legacy: inside MCP package
    ]
    env_path = os.environ.get("AIDOCS_PATH")
    if env_path:
        candidates.insert(1, Path(env_path) / "action_tokens")
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]  # fallback to project root even if missing

_ACTION_TOKENS_DIR = _resolve_action_tokens_dir()


def _load_action_tokens(directory: Path | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """Load action token mappings from all YAML files in the action_tokens directory.

    Returns an ordered list of (action_kind, tokens) tuples suitable for
    first-match classification.  Files are simple ``key: [- value]`` YAML
    parsed without PyYAML to avoid an extra dependency.
    """
    root = directory or _ACTION_TOKENS_DIR
    if not root.is_dir():
        logger.warning("action_tokens directory not found: %s", root)
        return []

    # Filter by configured languages
    from .config import LANGUAGES_ENABLED
    enabled = LANGUAGES_ENABLED.lower().strip()
    if enabled != "all":
        enabled_set = {lang.strip() for lang in enabled.split(",") if lang.strip()}
    else:
        enabled_set = None  # load all

    merged: dict[str, list[str]] = {}
    for yaml_file in sorted(root.glob("*.yaml")):
        if enabled_set is not None and yaml_file.stem not in enabled_set:
            continue
        current_key: str | None = None
        try:
            for raw_line in yaml_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                key_match = re.match(r"^(\w[\w_]*):\s*$", line)
                if key_match:
                    current_key = key_match.group(1)
                    continue
                item_match = re.match(r"^\s+-\s+(.+)$", line)
                if item_match and current_key:
                    token = item_match.group(1).strip()
                    if token:
                        merged.setdefault(current_key, []).append(token)
        except Exception as exc:
            logger.warning("Failed to load action tokens from %s: %s", yaml_file, exc)

    # Deduplicate tokens per action_kind while preserving order
    result: list[tuple[str, tuple[str, ...]]] = []
    for action_kind, tokens in merged.items():
        seen: set[str] = set()
        unique: list[str] = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                unique.append(token)
        result.append((action_kind, tuple(unique)))
    return result


class RuntimeService:
    """High-level runtime orchestration over sessions, memory, and indexes."""

    def __init__(self, hub: AidocsServiceHub) -> None:
        self.hub = hub
        self._action_token_mapping: list[tuple[str, tuple[str, ...]]] | None = None

    def _get_action_tokens(self) -> list[tuple[str, tuple[str, ...]]]:
        if self._action_token_mapping is None:
            self._action_token_mapping = _load_action_tokens()
        return self._action_token_mapping

    def ensure_claude_mcp_config(self, project_root: Path) -> dict[str, object]:
        """Ensure the target project has a .mcp.json with the aidocs MCP server entry.

        Idempotent: if the entry already exists and points to a valid path, no change is made.
        Returns a dict describing what happened.
        """
        mcp_json_path = project_root / ".mcp.json"
        aidocs_source_root = Path(__file__).resolve().parents[3]
        # Prefer AIDOCS_PATH env var if set (installed by installer)
        env_aidocs_path = os.environ.get("AIDOCS_PATH")
        if env_aidocs_path and Path(env_aidocs_path).is_dir():
            aidocs_source_root = Path(env_aidocs_path)
        mcp_server_dir = aidocs_source_root / "mcp" / "server"

        # Resolve the python executable — prefer the one running this process
        python_bin = sys.executable or shutil.which("python") or shutil.which("python3") or "python"

        new_entry = {
            "type": "stdio",
            "command": python_bin,
            "args": ["-m", "aidocs_mcp.mcp_server"],
            "env": {
                "PYTHONPATH": str(mcp_server_dir),
            },
        }

        # Read existing .mcp.json if present
        existing: dict[str, object] = {}
        if mcp_json_path.is_file():
            try:
                existing = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to parse existing .mcp.json: %s", exc)
                existing = {}

        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            existing["mcpServers"] = servers

        # Check if aidocs entry already exists and is correct
        current = servers.get("aidocs")
        if isinstance(current, dict):
            current_pythonpath = (current.get("env") or {}).get("PYTHONPATH", "")
            if current_pythonpath == str(mcp_server_dir):
                return {
                    "action": "no_change",
                    "path": str(mcp_json_path),
                    "reason": "aidocs MCP entry already present and correct",
                }

        servers["aidocs"] = new_entry
        existing["mcpServers"] = servers
        mcp_json_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        action = "updated" if current else "created"
        return {
            "action": action,
            "path": str(mcp_json_path),
            "entry": new_entry,
        }

    def project_origins(self, project_root: Path) -> dict[str, object]:
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
                {"name": name, "url": url, "fetch": False, "push": False, "role": _origin_role(name, url)},
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
        return result

    def _load_project_rules(self, project_root: Path) -> dict[str, str]:
        """Load rule files from /.MEMORY/rules/ and return as {filename: content} dict."""
        rules_dir = project_root / ".MEMORY" / "rules"
        if not rules_dir.is_dir():
            return {}
        result: dict[str, str] = {}
        for rule_file in sorted(rules_dir.glob("*.md")):
            try:
                content = rule_file.read_text(encoding="utf-8", errors="ignore").strip()
                if content and len(content) > 10:
                    result[rule_file.stem] = content
            except Exception:
                continue
        return result

    def repo_summary(self, project_root: Path) -> dict[str, object]:
        code_files = 0
        modules = 0
        parsed = 0
        schema_entities = 0
        schema_fields = 0
        session_count = 0
        language_tiers: dict[str, int] = {}
        language_sources: dict[str, int] = {}

        try:
            with self.hub.code.connect(project_root) as conn:
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(parsed), 0) FROM code_files").fetchone()
                if row:
                    code_files = int(row[0] or 0)
                    parsed = int(row[1] or 0)
                row = conn.execute("SELECT COUNT(*) FROM code_modules").fetchone()
                if row:
                    modules = int(row[0] or 0)
                for row in conn.execute("SELECT COALESCE(language_tier, 'unknown') AS tier, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_tier, 'unknown')"):
                    language_tiers[str(row["tier"])] = int(row["count"] or 0)
                for row in conn.execute("SELECT COALESCE(language_source, 'unknown') AS source, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_source, 'unknown')"):
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

        try:
            session_count = len(self.hub.sessions.list_sessions(project_root))
        except Exception:
            session_count = 0

        origins = self.project_origins(project_root)
        bullets = [
            f"{code_files} indexed code files ({parsed} parsed)",
            f"{modules} detected modules",
            f"{schema_entities} schema entities / {schema_fields} fields",
            f"{session_count} sessions",
        ]
        if language_tiers:
            bullets.append("language tiers: " + ", ".join(f"{k}={v}" for k, v in sorted(language_tiers.items())))
        if language_sources:
            bullets.append("language sources: " + ", ".join(f"{k}={v}" for k, v in sorted(language_sources.items())))
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
        }

    def project_structure_gaps(self, project_root: Path) -> list[str]:
        memory_root = project_root / ".MEMORY"
        required = [
            memory_root / "INDEX.md",
            memory_root / ".aidocs" / "index.aidocs",
            memory_root / "rules" / "workflow-rules.md",
            memory_root / "rules" / "workflow-actions.md",
        ]
        missing = [str(path.relative_to(project_root)).replace("\\", "/") for path in required if not path.exists()]
        if not ((project_root / "AGENTS.md").is_file() or (project_root / "CLAUDE.md").is_file()):
            missing.append("AGENTS.md or CLAUDE.md")
        return missing

    def project_init(self, project_root: Path, init_git: bool = True, create_remote: bool = False) -> dict[str, object]:
        root = project_root
        if not root.is_dir():
            root.mkdir(parents=True, exist_ok=True)

        created: list[str] = []
        skipped: list[str] = []

        templates_root = self.hub.sessions.templates_root
        memory_template = templates_root.parent / "templates" / "memory"
        memory_dest = root / ".MEMORY"

        if memory_template.is_dir():
            for src_file in memory_template.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(memory_template)
                    dest = memory_dest / rel
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src_file), str(dest))
                        created.append(f".MEMORY/{rel}")
                    else:
                        skipped.append(f".MEMORY/{rel}")
        else:
            for d in [
                ".MEMORY/.aidocs",
                ".MEMORY/sessions",
                ".MEMORY/rules",
                ".MEMORY/domains",
                ".MEMORY/system",
                ".MEMORY/config",
                ".MEMORY/archive/sessions",
            ]:
                (root / d).mkdir(parents=True, exist_ok=True)
            idx = memory_dest / "INDEX.md"
            if not idx.exists():
                idx.write_text(
                    "# Memory Index\n\n"
                    "## Sessions\n- `sessions/`\n\n"
                    "## Rules\n"
                    "- `rules/workflow-rules.md`\n"
                    "- `rules/workflow-actions.md`\n",
                    encoding="utf-8",
                )
                created.append(".MEMORY/INDEX.md")

        workflow_rules = memory_dest / "rules" / "workflow-rules.md"
        if not workflow_rules.exists():
            workflow_rules.parent.mkdir(parents=True, exist_ok=True)
            workflow_rules.write_text("# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8")
            created.append(".MEMORY/rules/workflow-rules.md")
        else:
            skipped.append(".MEMORY/rules/workflow-rules.md")

        workflow_actions = memory_dest / "rules" / "workflow-actions.md"
        if not workflow_actions.exists():
            workflow_actions.parent.mkdir(parents=True, exist_ok=True)
            workflow_actions.write_text("# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8")
            created.append(".MEMORY/rules/workflow-actions.md")
        else:
            skipped.append(".MEMORY/rules/workflow-actions.md")

        router = memory_dest / ".aidocs" / "index.aidocs"
        if not router.exists():
            router.parent.mkdir(parents=True, exist_ok=True)
            src_router = templates_root.parent / "index.aidocs"
            if src_router.is_file():
                shutil.copy2(str(src_router), str(router))
            else:
                router.write_text("# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8")
            created.append(".MEMORY/.aidocs/index.aidocs")

        for tmpl_name in ["AGENTS.md", "CLAUDE.md"]:
            dest = root / tmpl_name
            if not dest.exists():
                src = templates_root.parents[1] / tmpl_name
                if src.is_file():
                    shutil.copy2(str(src), str(dest))
                else:
                    dest.write_text(f"# {tmpl_name.replace('.md','')}\n\nAIDOCS-managed project.\n", encoding="utf-8")
                created.append(tmpl_name)
            else:
                skipped.append(tmpl_name)

        git_result: dict[str, object] = {"action": "none"}
        if init_git and not (root / ".git").exists():
            try:
                toplevel = _run_git_sync(str(root), "rev-parse", "--show-toplevel")
                git_result = {"action": "already_in_repo", "root": toplevel}
            except FileNotFoundError:
                git_result = {"action": "skipped", "reason": "git not installed"}
            except RuntimeError:
                try:
                    _run_git_sync(str(root), "init")
                    gitignore = root / ".gitignore"
                    if not gitignore.exists():
                        gitignore.write_text(
                            "# AIDOCS defaults\n/.MEMORY/.index/\nnode_modules/\ndist/\n__pycache__/\n.venv/\n*.pyc\n.env\n",
                            encoding="utf-8",
                        )
                        created.append(".gitignore")
                    _run_git_sync(str(root), "add", "-A")
                    _run_git_sync(str(root), "commit", "-m", "chore: initialize project with AIDOCS")
                    git_result = {"action": "initialized", "initial_commit": True}
                except Exception as exc:
                    git_result = {"action": "failed", "reason": str(exc)}
            except Exception as exc:
                git_result = {"action": "failed", "reason": str(exc)}

        if create_remote and git_result.get("action") == "initialized":
            try:
                output = _run_git_sync(str(root), "remote", "get-url", "origin")
                git_result["remote"] = {"created": False, "reason": f"Remote already exists: {output}"}
            except RuntimeError:
                try:
                    import tempfile as _tf
                    _gh_out = None
                    try:
                        with _tf.NamedTemporaryFile(mode="w", suffix=".gh.out", delete=False) as _f:
                            _gh_out = _f.name
                        with open(_gh_out, "w") as _fh:
                            result = subprocess.run(
                                ["gh", "repo", "create", root.name, "--private", "--source", str(root), "--push"],
                                cwd=str(root), stdin=subprocess.DEVNULL,
                                stdout=_fh, stderr=subprocess.DEVNULL,
                                text=True, timeout=30, check=False,
                            )
                        result.stdout = Path(_gh_out).read_text(encoding="utf-8", errors="ignore").strip()
                    finally:
                        if _gh_out:
                            try:
                                os.unlink(_gh_out)
                            except OSError:
                                pass
                    git_result["remote"] = {
                        "created": result.returncode == 0,
                        "name": root.name,
                        "url": (result.stdout or "").strip(),
                        "reason": (result.stderr or "").strip() if result.returncode != 0 else "",
                    }
                except FileNotFoundError:
                    git_result["remote"] = {"created": False, "reason": "gh CLI not installed"}
                except Exception as exc:
                    git_result["remote"] = {"created": False, "reason": str(exc)}

        mcp_config_result = self.ensure_claude_mcp_config(root)
        return {
            "initialized": True,
            "created": created,
            "skipped": skipped,
            "git": git_result,
            "origins": self.project_origins(root),
            "repo_summary": self.repo_summary(root),
            "mcp_config": mcp_config_result,
            "next_step": "Call project_bootstrap_or_resume to activate managed mode and select a session.",
        }

    def session_start(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        sync_indexes: bool = True,
        include_tests: bool = False,
    ) -> dict[str, object]:
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)

        startup_files = [
            "/.MEMORY/.aidocs/index.aidocs",
            "/.MEMORY/.aidocs/global-instructions.aidocs",
            "/.MEMORY/.aidocs/coding-standards.aidocs",
            "/.MEMORY/.aidocs/memory-system.aidocs",
            "/.MEMORY/INDEX.md",
        ]

        sessions = self.hub.sessions.list_sessions(project_root)
        session_summaries = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "status": item.status,
                "owner": item.owner,
                "goal": item.goal,
                "last_updated": item.last_updated,
            }
            for item in sessions
        ]

        if session_id is None:
            active = [item for item in sessions if item.status == "active"]
            if len(active) == 1:
                session_id = active[0].session_id
            else:
                response = {
                    "startup_files": startup_files,
                    "origins": self.project_origins(project_root),
                    "repo_summary": self.repo_summary(project_root),
                    "requires_session_selection": True,
                    "reason": "no_unique_active_session",
                    "sessions": session_summaries,
                }
                response["report"] = self._build_session_start_report(response)
                return response

        session = self.hub.sessions.read_session(project_root, session_id)
        context = self.hub.sessions.read_context(project_root, session_id)
        handoff = self.hub.sessions.read_handoff(project_root, session_id)
        handoff_steps = self.hub.sessions.read_handoff_steps(project_root, session_id)
        compliance = self.session_compliance_summary(project_root, session_id)

        if sync_indexes:
            self.hub.code.sync_session_code(project_root, session_id=session_id, include_tests=include_tests)

        response: dict[str, object] = {
            "startup_files": startup_files,
            "origins": self.project_origins(project_root),
            "repo_summary": self.repo_summary(project_root),
            "requires_session_selection": False,
            "selected_session": {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            },
            "context": {
                "path": str(context.path),
                "sections": context.sections,
            },
            "handoff": {
                "path": str(handoff.path),
                "sections": handoff.sections,
            },
            "handoff_steps": handoff_steps,
            "compliance": compliance,
            "sessions": session_summaries,
        }

        if include_code_bundle:
            response["code_bundle"] = self.hub.code.get_context_bundle(project_root, session_id=session_id)

        response["report"] = self._build_session_start_report(response)

        return response

    def session_resume_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
    ) -> dict[str, object]:
        session = self.hub.sessions.read_session(project_root, session_id)
        context = self.hub.sessions.read_context(project_root, session_id)
        plan = self.hub.sessions.read_plan(project_root, session_id)
        handoff = self.hub.sessions.read_handoff(project_root, session_id)
        journal = self.hub.sessions.read_journal(project_root, session_id, last_n=journal_last_n)
        freshness = self._handoff_freshness(handoff.sections)
        handoff_steps = self.hub.sessions.read_handoff_steps(project_root, session_id)
        actionable_steps = [step for step in handoff_steps if step.get("status") in {"open", "reset", "failed", "stale"}]
        recently_changed_steps = [step for step in handoff_steps if self._step_changed_recently(step)]
        compliance = self.session_compliance_summary(project_root, session_id)

        result: dict[str, object] = {
            "session": {"session_id": session.session_id, "path": str(session.path), "sections": session.sections},
            "context": {"path": str(context.path), "sections": context.sections},
            "plan": {"path": str(plan.path), "sections": plan.sections},
            "handoff": {"path": str(handoff.path), "sections": handoff.sections},
            "handoff_steps": handoff_steps,
            "actionable_handoff_steps": actionable_steps,
            "recently_changed_handoff_steps": recently_changed_steps,
            "handoff_freshness": freshness,
            "compliance": compliance,
            "journal": journal,
            "repo_summary": self.repo_summary(project_root),
        }
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        return result

    def session_compliance_summary(self, project_root: Path, session_id: str) -> dict[str, object]:
        session = self.hub.sessions.read_session(project_root, session_id)
        plan = self.hub.sessions.read_plan(project_root, session_id)
        handoff_steps = self.hub.sessions.read_handoff_steps(project_root, session_id)
        journal = self.hub.sessions.read_journal(project_root, session_id, last_n=20)
        execution_summary = self.hub.execution.query_execution_summary(project_root, session_id=session_id)
        recent_events = self.hub.execution.query_last_execution(project_root, session_id=session_id, limit=20)

        status_values = self._clean_bullets(session.sections.get("Status", []))
        task_open = any(value == "active" for value in status_values)
        partial_goals = self._clean_bullets(plan.sections.get("Partial Goals", []))
        upcoming = self._clean_bullets(session.sections.get("Upcoming", []))
        actionable_steps = [step for step in handoff_steps if str(step.get("status")) in {"open", "reset", "failed", "stale"}]

        latest_journal_ts = None
        if journal:
            try:
                latest_journal_ts = max(datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M") for entry in journal if entry.get("timestamp"))
            except Exception:
                latest_journal_ts = None

        work_events = [
            event for event in recent_events
            if str(event.get("action_kind") or "") not in {"", "task_begin", "task_update", "task_complete"}
        ]
        latest_work_ts = None
        if work_events:
            try:
                latest_work_ts = max(datetime.strptime(str(event["observed_at"]), "%Y-%m-%d %H:%M:%S") for event in work_events if event.get("observed_at"))
            except Exception:
                latest_work_ts = None

        logging_debt = bool(latest_work_ts and (latest_journal_ts is None or latest_work_ts > latest_journal_ts))
        summary = {
            "task_open": task_open,
            "logging_debt": logging_debt,
            "actionable_step_count": len(actionable_steps),
            "partial_goal_count": len(partial_goals),
            "upcoming_count": len(upcoming),
            "execution_events": int(execution_summary.get("total_events", 0)),
            "latest_work_event_at": latest_work_ts.strftime("%Y-%m-%d %H:%M:%S") if latest_work_ts else None,
            "latest_journal_at": latest_journal_ts.strftime("%Y-%m-%d %H:%M") if latest_journal_ts else None,
            "warnings": [],
        }
        warnings: list[str] = []
        if task_open:
            warnings.append("task remains open")
        if logging_debt:
            warnings.append("work occurred after the latest journal entry")
        if actionable_steps:
            warnings.append(f"{len(actionable_steps)} actionable handoff steps remain")
        summary["warnings"] = warnings
        return summary

    def _registered_tools_snapshot(self) -> list[object]:
        from .mcp_server import create_server

        server = create_server()
        components = getattr(getattr(server, "_local_provider", None), "_components", {})
        return [component for key, component in components.items() if str(key).startswith("tool:")]

    def _sync_bootstrap_indexes(self, project_root: Path, include_tests: bool) -> dict[str, object]:
        workflow = self.hub.workflow.compile_project_rules(project_root)
        capabilities = self.hub.capabilities.sync_capabilities(project_root, self._registered_tools_snapshot())
        procedures = self.hub.procedures.sync_procedures(project_root, self.hub.workflow.read_compiled(project_root))
        links = self.hub.procedure_links.sync_links(
            project_root,
            self.hub.procedures.find_procedures(project_root, query=None, limit=1000),
            self.hub.capabilities.find_capabilities(project_root, query=None, limit=1000),
        )
        return {
            "memory": self.hub.index.sync_all(project_root),
            "code_manifest": {"code_files": self.hub.code.sync_code_files(project_root, include_tests=include_tests), "modules": self.hub.code.sync_modules(project_root)},
            "schema": self.hub.schema.sync_schema(project_root),
            "workflow": workflow,
            "capabilities": {"capability_definitions": capabilities},
            "procedures": {"procedure_definitions": procedures},
            "procedure_capability_links": {"links": links},
            "execution": self.hub.execution.execution_status(project_root),
        }

    def _build_session_start_report(self, response: dict[str, object]) -> dict[str, object]:
        if response.get("requires_session_selection"):
            sessions = response.get("sessions") if isinstance(response.get("sessions"), list) else []
            repo_summary = response.get("repo_summary") if isinstance(response.get("repo_summary"), dict) else {}
            extra = repo_summary.get("bullets") if isinstance(repo_summary.get("bullets"), list) else []
            return {
                "headline": "Session selection is required before continuing.",
                "bullets": [f"Active/available sessions: {len(sessions)}."] + [str(item) for item in extra[:3]],
                "next_step": "select_session",
            }

        selected = response.get("selected_session") if isinstance(response.get("selected_session"), dict) else {}
        session_id = selected.get("session_id")
        bullets = [f"Selected session: {session_id}."] if session_id else []
        if response.get("code_bundle"):
            bullets.append("Context code bundle is included.")
        else:
            bullets.append("Context code bundle is deferred by default.")
        repo_summary = response.get("repo_summary") if isinstance(response.get("repo_summary"), dict) else {}
        extra = repo_summary.get("bullets") if isinstance(repo_summary.get("bullets"), list) else []
        bullets.extend(str(item) for item in extra[:3])
        handoff = response.get("handoff") if isinstance(response.get("handoff"), dict) else {}
        handoff_sections = handoff.get("sections") if isinstance(handoff.get("sections"), dict) else {}
        handoff_now = handoff_sections.get("What Matters Now") if isinstance(handoff_sections.get("What Matters Now"), list) else []
        bullets.extend(str(item) for item in handoff_now[:2] if str(item).strip() != "-")
        handoff_steps = response.get("handoff_steps") if isinstance(response.get("handoff_steps"), list) else []
        actionable_count = sum(1 for step in handoff_steps if str(step.get("status")) in {"open", "reset", "failed", "stale"})
        if actionable_count:
            bullets.append(f"Actionable handoff steps: {actionable_count}.")
        freshness = self._handoff_freshness(handoff_sections)
        if freshness.get("status") == "stale":
            bullets.append(f"Handoff freshness is stale ({freshness.get('age_hours')}h old).")
        elif freshness.get("status") == "unknown":
            bullets.append("Handoff freshness is unknown.")
        compliance = response.get("compliance") if isinstance(response.get("compliance"), dict) else {}
        for warning in compliance.get("warnings", [])[:3] if isinstance(compliance.get("warnings"), list) else []:
            bullets.append(f"Compliance: {warning}.")
        return {
            "headline": "Session context is ready.",
            "bullets": bullets,
            "next_step": None,
        }

    def _build_bootstrap_report(self, result: dict[str, object]) -> dict[str, object]:
        stage = str(result.get("stage") or "unknown")
        repo_summary = result.get("repo_summary") if isinstance(result.get("repo_summary"), dict) else {}
        repo_bullets = repo_summary.get("bullets") if isinstance(repo_summary.get("bullets"), list) else []
        if stage == "setup_required":
            return {
                "headline": "AIDOCS project setup is required.",
                "bullets": [str(result.get("reason") or "Missing AIDOCS project structure.")],
                "next_step": result.get("next_step"),
            }
        if stage == "migration_required":
            return {
                "headline": "Legacy migration choice is required before continuing.",
                "bullets": ["Legacy runtime files are present and no session has been migrated yet."],
                "next_step": result.get("next_step"),
            }

        session = result.get("session") if isinstance(result.get("session"), dict) else {}
        selected = session.get("selected_session") if isinstance(session.get("selected_session"), dict) else {}
        sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
        capabilities = sync.get("capabilities") if isinstance(sync.get("capabilities"), dict) else {}
        procedures = sync.get("procedures") if isinstance(sync.get("procedures"), dict) else {}
        links = sync.get("procedure_capability_links") if isinstance(sync.get("procedure_capability_links"), dict) else {}
        bullets = []
        repaired = result.get("repaired") if isinstance(result.get("repaired"), dict) else None
        if repaired:
            created = repaired.get("created") if isinstance(repaired.get("created"), list) else []
            bullets.append(f"Repaired canonical AIDOCS structure ({len(created)} files created).")
        if selected.get("session_id"):
            bullets.append(f"Selected session: {selected.get('session_id')}.")
        bullets.append(
            f"Action surfaces synced: capabilities={capabilities.get('capability_definitions')}, procedures={procedures.get('procedure_definitions')}, links={links.get('links')}."
        )
        bullets.extend(str(item) for item in repo_bullets[:4])
        return {
            "headline": "Project bootstrap is ready.",
            "bullets": bullets,
            "next_step": None,
        }

    def _build_readiness_summary(
        self,
        *,
        bootstrap: dict[str, object],
        selected_session_id: str | None,
        managed_mode: dict[str, object] | None,
        operator_summary: dict[str, object] | None,
    ) -> dict[str, object]:
        sync = bootstrap.get("sync") if isinstance(bootstrap.get("sync"), dict) else {}
        workflow = sync.get("workflow") if isinstance(sync.get("workflow"), dict) else {}
        capabilities = sync.get("capabilities") if isinstance(sync.get("capabilities"), dict) else {}
        procedures = sync.get("procedures") if isinstance(sync.get("procedures"), dict) else {}
        links = sync.get("procedure_capability_links") if isinstance(sync.get("procedure_capability_links"), dict) else {}
        execution = sync.get("execution") if isinstance(sync.get("execution"), dict) else {}
        memory = sync.get("memory") if isinstance(sync.get("memory"), dict) else {}
        code_manifest = sync.get("code_manifest") if isinstance(sync.get("code_manifest"), dict) else {}
        schema = sync.get("schema") if isinstance(sync.get("schema"), dict) else {}

        return {
            "ready": bool(bootstrap.get("ready")),
            "stage": bootstrap.get("stage"),
            "selected_session_id": selected_session_id,
            "managed_mode_active": bool((managed_mode or {}).get("active")),
            "managed_mode_session_id": (managed_mode or {}).get("session_id"),
            "operator_state": (operator_summary or {}).get("overall_state") or (operator_summary or {}).get("state"),
            "indexes": {
                "memory_files": memory.get("memory_files"),
                "code_files": code_manifest.get("code_files"),
                "schema_entities": schema.get("entities"),
                "workflow_actions": workflow.get("action_count"),
                "capability_definitions": capabilities.get("capability_definitions"),
                "procedure_definitions": procedures.get("procedure_definitions"),
                "procedure_capability_links": links.get("links"),
                "execution_runs": execution.get("execution_runs"),
                "execution_events": execution.get("execution_events"),
            },
        }

    def _build_operator_report(
        self,
        *,
        readiness_summary: dict[str, object],
        operator_summary: dict[str, object] | None,
        bootstrap: dict[str, object],
        action_kind: str | None = None,
        project_root: Path | None = None,
    ) -> dict[str, object]:
        ready = bool(readiness_summary.get("ready"))
        stage = str(readiness_summary.get("stage") or "unknown")
        operator_state = str(readiness_summary.get("operator_state") or "unknown")
        selected_session_id = str(readiness_summary.get("selected_session_id") or "").strip() or None
        indexes = readiness_summary.get("indexes") if isinstance(readiness_summary.get("indexes"), dict) else {}

        if not ready:
            next_step = bootstrap.get("next_step") or bootstrap.get("stage")
            return {
                "headline": f"AIDOCS is not ready: {stage}.",
                "bullets": [
                    f"Next step: {next_step}.",
                ],
                "next_step": next_step,
            }

        bullets = []
        if selected_session_id:
            bullets.append(f"Active session: {selected_session_id}.")
        bullets.append(f"Operator state: {operator_state}.")
        bullets.append(
            "Index coverage: "
            f"memory={indexes.get('memory_files')}, "
            f"code={indexes.get('code_files')}, "
            f"schema={indexes.get('schema_entities')}, "
            f"capabilities={indexes.get('capability_definitions')}, "
            f"procedures={indexes.get('procedure_definitions')}, "
            f"links={indexes.get('procedure_capability_links')}."
        )
        next_step = None
        if isinstance(operator_summary, dict):
            attention_items = operator_summary.get("attention_items")
            if isinstance(attention_items, list) and attention_items:
                first_attention = attention_items[0]
                if isinstance(first_attention, dict):
                    steps = list(first_attention.get("recommended_next_steps") or [])
                    next_step = steps[0] if steps else None
            if next_step is None:
                steps = list(operator_summary.get("recommended_next_steps") or [])
                next_step = steps[0] if steps else None
            if next_step is None and str(operator_summary.get("overall_state") or "") == "healthy":
                next_step = "No immediate gap detected; continue monitoring execution history and drift."

        # Surface pending workflow actions for the current action_kind
        pending_workflow = self._collect_pending_workflow(action_kind, project_root)
        if pending_workflow:
            bullets.append(f"Pending workflow actions after `{action_kind}`: {pending_workflow}.")

        return {
            "headline": f"AIDOCS is ready in stage `{stage}`.",
            "bullets": bullets,
            "next_step": next_step,
        }

    def _build_handle_prompt_report(
        self,
        *,
        mode: str,
        classification: dict[str, object],
        route: dict[str, object],
        next_step: object = None,
        operator_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        action_kind = str(classification.get("action_kind") or "unknown")
        if mode == "requires_aidocs_entry":
            return {
                "headline": "Enter `/aidocs` first to work in managed mode.",
                "bullets": [f"Requested action kind: {action_kind}."],
                "next_step": next_step,
            }
        if mode == "blocked":
            return {
                "headline": "The requested action is blocked by current policy or routing state.",
                "bullets": [
                    f"Requested action kind: {action_kind}.",
                    f"Blocked reason: {route.get('blocked_reason')}.",
                ],
                "next_step": next_step,
            }
        if mode == "direct_inspection_allowed":
            return {
                "headline": "Direct inspection is allowed for the requested target.",
                "bullets": [
                    f"Requested action kind: {action_kind}.",
                    "Inspect the target first, then return to MCP orchestration for broader work.",
                ],
                "next_step": next_step,
            }
        if mode == "mcp_orchestrated" and isinstance(operator_report, dict):
            return operator_report
        return {
            "headline": "Prompt was classified and routed successfully.",
            "bullets": [f"Requested action kind: {action_kind}."],
            "next_step": next_step,
        }

    def project_bootstrap_or_resume(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        agents = project_root / "AGENTS.md"
        claude = project_root / "CLAUDE.md"
        memory_root = project_root / ".MEMORY"

        initialized = memory_root.is_dir() and (agents.is_file() or claude.is_file())
        if not initialized:
            result = {
                "stage": "setup_required",
                "ready": False,
                "next_step": "project_init",
                "reason": "missing AIDOCS project structure",
            }
            result["report"] = self._build_bootstrap_report(result)
            return result

        repaired = None
        structure_gaps = self.project_structure_gaps(project_root)
        if structure_gaps:
            repaired = self.project_init(project_root, init_git=False, create_remote=False)

        # Ensure .mcp.json is present for Claude Code (idempotent)
        try:
            self.ensure_claude_mcp_config(project_root)
        except Exception as exc:
            logger.debug("Failed to ensure .mcp.json: %s", exc)

        sync_result = self._sync_bootstrap_indexes(project_root, include_tests=include_tests)

        legacy_state = self.hub.legacy.inspect_legacy(project_root)
        sessions = self.hub.sessions.list_sessions(project_root)
        if legacy_state.get("legacy_present") and len(sessions) == 0:
            proposal = self.hub.legacy.build_session_proposal(project_root, session_id=session_id)
            result = {
                "stage": "migration_required",
                "ready": False,
                "initialized": True,
                "indexes_synced": True,
                "repaired": repaired,
                "sync": sync_result,
                "legacy": legacy_state,
                "proposal": proposal,
                "next_step": "issue_stop_for_migration_choice",
            }
            result["report"] = self._build_bootstrap_report(result)
            return result

        session_result = self.session_start(
            project_root,
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            sync_indexes=False,
            include_tests=include_tests,
        )

        if include_code_bundle and not session_result.get("requires_session_selection"):
            selected = session_result.get("selected_session") or {}
            selected_session_id = selected.get("session_id")
            if isinstance(selected_session_id, str) and selected_session_id.strip():
                session_result["code_bundle"] = self._refresh_session_code_bundle(
                    project_root,
                    session_id=selected_session_id,
                    include_tests=include_tests,
                    sync_indexes=False,
                )

        result = {
            "stage": "session_active" if not session_result.get("requires_session_selection") else "session_selection_required",
            "ready": not session_result.get("requires_session_selection"),
            "initialized": True,
            "indexes_synced": True,
            "repaired": repaired,
            "repo_summary": self.repo_summary(project_root),
            "sync": sync_result,
            "session": session_result,
        }

        # Without rules injection, AIDOCS operates in MCP-tool-only mode —
        # the agent can use indexed retrieval but does not follow any /.MEMORY/rules/ directives.
        from .config import AGENT_INJECT_RULES_ON_BOOTSTRAP
        if AGENT_INJECT_RULES_ON_BOOTSTRAP:
            rules = self._load_project_rules(project_root)
            if rules:
                result["rules"] = rules

        result["report"] = self._build_bootstrap_report(result)
        return result

    def aidocs_orchestrate(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str = "understand",
        session_id: str | None = None,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        preflight = self.hub.policy.preflight_action(
            project_root,
            action_kind=action_kind,
            session_id=session_id,
            user_explicit_targets=explicit_targets,
        )

        bootstrap = self.project_bootstrap_or_resume(
            project_root,
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

        result: dict[str, object] = {
            "request": user_request,
            "action_kind": action_kind,
            "preflight": preflight,
            "bootstrap": bootstrap,
        }

        if not bootstrap.get("ready"):
            result["readiness_summary"] = self._build_readiness_summary(
                bootstrap=bootstrap,
                selected_session_id=None,
                managed_mode=None,
                operator_summary=None,
            )
            result["operator_report"] = self._build_operator_report(
                readiness_summary=result["readiness_summary"],
                operator_summary=None,
                bootstrap=bootstrap,
                action_kind=action_kind,
                project_root=project_root,
            )
            result["report"] = result["operator_report"]
            result["next_step"] = bootstrap.get("next_step") or bootstrap.get("stage")
            return result

        selected = bootstrap["session"]["selected_session"]["session_id"]
        result["selected_session_id"] = selected
        result["managed_mode"] = self.hub.managed_mode.set_mode(project_root, session_id=selected, source="/aidocs")
        result["operator_summary"] = self.hub.action_surface.current_session_bundle(project_root, limit=10, max_queries=12)
        result["readiness_summary"] = self._build_readiness_summary(
            bootstrap=bootstrap,
            selected_session_id=selected,
            managed_mode=result.get("managed_mode") if isinstance(result.get("managed_mode"), dict) else None,
            operator_summary=result.get("operator_summary") if isinstance(result.get("operator_summary"), dict) else None,
        )
        result["operator_report"] = self._build_operator_report(
            readiness_summary=result["readiness_summary"],
            operator_summary=result.get("operator_summary") if isinstance(result.get("operator_summary"), dict) else None,
            bootstrap=bootstrap,
            action_kind=action_kind,
            project_root=project_root,
        )
        result["report"] = result["operator_report"]

        if explicit_targets:
            if include_code_bundle:
                file_bundles = []
                for target in explicit_targets:
                    normalized = target.replace("\\", "/")
                    if not self.hub.code._is_indexed_file(project_root, normalized):
                        file_bundles.append({"path": normalized, "missing": True})
                        continue
                    file_bundles.append(self.hub.code.get_file_bundle(project_root, normalized))
                result["retrieval"] = {
                    "mode": "explicit_targets",
                    "targets": explicit_targets,
                    "bundles": file_bundles,
                }
            else:
                result["retrieval"] = {
                    "mode": "explicit_targets_deferred",
                    "targets": explicit_targets,
                    "reason": "bundle_omitted_by_default",
                }
        else:
            if include_code_bundle:
                result["retrieval"] = {
                    "mode": "session_bundle",
                    "bundle": self.hub.code.get_context_bundle(project_root, session_id=selected),
                }
            else:
                preview = self.hub.sessions.session_code_targets(project_root, selected)
                result["retrieval"] = {
                    "mode": "session_bundle_deferred",
                    "session_id": selected,
                    "session_target_count": len([item for item in preview if item and item.strip()]),
                    "memory_structure": self._memory_structure_summary(project_root),
                    "reason": "bundle_omitted_by_default",
                }

        # Include compiled workflow actions so the host doesn't need to re-read
        try:
            result["workflow"] = self.hub.workflow.read_compiled(project_root)
        except Exception as exc:
            logger.warning("Failed to read workflow for orchestration result: %s", exc)
            result["workflow"] = None

        return result

    def aidocs_route_prompt(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
    ) -> dict[str, object]:
        managed = self.hub.managed_mode.get_mode(project_root)
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        if not managed.get("active"):
            return {
                "managed_mode": False,
                "action_kind": action_kind,
                "allowed_direct_inspection": bool(explicit_targets),
                "requires_session": False,
                "requires_task_lifecycle": False,
                "recommended_mcp_flow": ["/aidocs"],
                "blocked_reason": None,
            }

        session_id = managed.get("session_id")
        preflight = self.hub.policy.preflight_action(
            project_root,
            action_kind=action_kind,
            session_id=str(session_id) if session_id else None,
            user_explicit_targets=explicit_targets,
        )

        requires_task_lifecycle = action_kind in {"edit", "write_memory", "task_begin", "task_update", "task_complete"}
        recommended = ["runtime_preflight"]
        if preflight.get("requires_session"):
            recommended.append("session_start")
        if requires_task_lifecycle:
            recommended.append("task_begin")
        if action_kind in {"understand", "trace", "edit", "code_bundle"}:
            recommended.append("aidocs_orchestrate")

        blocked_reason = None
        if managed.get("active") and preflight.get("allowed") is False:
            blocked_reason = str(preflight.get("reason"))

        return {
            "managed_mode": True,
            "action_kind": action_kind,
            "session_id": session_id,
            "allowed_direct_inspection": bool(explicit_targets) and action_kind in {"inspect", "read_file", "read_error"},
            "requires_session": bool(preflight.get("requires_session")),
            "requires_task_lifecycle": requires_task_lifecycle,
            "recommended_mcp_flow": recommended,
            "blocked_reason": blocked_reason,
            "preflight": preflight,
        }

    def classify_prompt_action(self, user_request: str, explicit_targets: list[str] | None = None) -> dict[str, object]:
        text = user_request.strip().lower()
        explicit_targets = [item for item in (explicit_targets or []) if str(item).strip()]

        if explicit_targets:
            if any(token in text for token in ("error", "stack trace", "traceback", "log", "logs", "why")):
                action_kind = "read_error"
            else:
                action_kind = "inspect"
            return {"action_kind": action_kind, "why": ["explicit_targets"]}

        mapping = self._get_action_tokens()
        for action_kind, tokens in mapping:
            if any(token in text for token in tokens):
                return {"action_kind": action_kind, "why": [f"matched:{action_kind}"]}

        return {"action_kind": "understand", "why": ["default:understand"]}

    def aidocs_handle_prompt(
        self,
        project_root: Path,
        user_request: str,
        action_kind: str,
        explicit_targets: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        if not action_kind or action_kind == "auto":
            classified = self.classify_prompt_action(user_request, explicit_targets=explicit_targets)
            action_kind = str(classified["action_kind"])
        else:
            classified = {"action_kind": action_kind, "why": ["provided"]}

        route = self.aidocs_route_prompt(
            project_root,
            user_request=user_request,
            action_kind=action_kind,
            explicit_targets=explicit_targets,
        )

        if not route.get("managed_mode"):
            return {
                "handled": False,
                "mode": "requires_aidocs_entry",
                "classification": classified,
                "route": route,
                "report": self._build_handle_prompt_report(
                    mode="requires_aidocs_entry",
                    classification=classified,
                    route=route,
                    next_step="/aidocs",
                ),
                "next_step": "/aidocs",
            }

        if route.get("blocked_reason"):
            return {
                "handled": False,
                "mode": "blocked",
                "classification": classified,
                "route": route,
                "report": self._build_handle_prompt_report(
                    mode="blocked",
                    classification=classified,
                    route=route,
                    next_step=route.get("recommended_mcp_flow"),
                ),
                "next_step": route.get("recommended_mcp_flow"),
            }

        session_id = route.get("session_id")
        if action_kind in {"inspect", "read_file", "read_error"} and route.get("allowed_direct_inspection"):
            return {
                "handled": True,
                "mode": "direct_inspection_allowed",
                "classification": classified,
                "route": route,
                "selected_session_id": session_id,
                "report": self._build_handle_prompt_report(
                    mode="direct_inspection_allowed",
                    classification=classified,
                    route=route,
                    next_step="inspect_target_then_return_to_mcp_for_broader_work",
                ),
                "next_step": "inspect_target_then_return_to_mcp_for_broader_work",
            }

        if action_kind in {"understand", "trace", "code_bundle", "edit", "write_memory"}:
            orchestration = self.aidocs_orchestrate(
                project_root,
                user_request=user_request,
                action_kind=action_kind,
                session_id=str(session_id) if session_id else None,
                explicit_targets=explicit_targets,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
            )
            return {
                "handled": True,
                "mode": "mcp_orchestrated",
                "classification": classified,
                "route": route,
                "operator_report": orchestration.get("operator_report"),
                "readiness_summary": orchestration.get("readiness_summary"),
                "report": self._build_handle_prompt_report(
                    mode="mcp_orchestrated",
                    classification=classified,
                    route=route,
                    operator_report=orchestration.get("operator_report") if isinstance(orchestration.get("operator_report"), dict) else None,
                ),
                "orchestration": orchestration,
            }

        return {
            "handled": True,
            "mode": "preflight_only",
            "classification": classified,
            "route": route,
            "report": self._build_handle_prompt_report(
                mode="preflight_only",
                classification=classified,
                route=route,
                next_step=route.get("recommended_mcp_flow"),
            ),
        }

    def plan_preflight(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Analyze a session plan and surface all decision points BEFORE implementation.

        Reads PLAN.md, extracts incomplete steps, runs code_investigate on each,
        and returns: what exists, what's missing, what decisions the agent must make
        before starting. The agent resolves decisions once upfront, then implements
        without mid-plan stops.
        """
        plan = self.hub.sessions.read_plan(project_root, session_id)
        if not plan or not plan.sections:
            return {"session_id": session_id, "error": "No plan found for this session."}

        # Extract incomplete steps from all plan sections
        steps: list[str] = []
        for section_name, lines in plan.sections.items():
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("- [ ]"):
                    step_text = stripped[5:].strip()
                    if step_text:
                        steps.append(step_text)

        if not steps:
            return {"session_id": session_id, "steps": [], "message": "All plan steps are complete."}

        # Investigate each step — find what exists, what's missing
        step_analysis: list[dict[str, object]] = []
        for step_text in steps:
            # Extract key concepts from the step text (first 3 significant words)
            words = [w for w in step_text.split() if len(w) > 3 and w[0].isalpha()]
            concept = " ".join(words[:3]) if words else step_text[:40]

            investigation = self.hub.code.investigate(project_root, concept, limit=3)
            findings = investigation.get("findings", [])
            next_tools = investigation.get("next_tools", [])

            # Classify: does infrastructure exist or is this greenfield?
            has_symbols = any(f.get("area") == "symbols" for f in findings)
            has_schema = any(f.get("area") in ("schema_entities", "schema_fields") for f in findings)
            has_files = any(f.get("area") == "files" for f in findings)

            if has_symbols or has_schema:
                status = "extend"  # modify existing code
            elif has_files:
                status = "integrate"  # wire into existing structure
            else:
                status = "create"  # greenfield, needs decisions

            decisions: list[str] = []
            if status == "create":
                decisions.append(f"No existing code found for '{concept}' — decide: where to create, which patterns to follow")
            if has_schema and not has_symbols:
                decisions.append(f"Schema exists for '{concept}' but no service/controller code — decide: service layer architecture")
            if not has_schema and has_symbols:
                decisions.append(f"Code exists for '{concept}' but no schema — decide: is DB/model layer needed?")

            step_analysis.append({
                "step": step_text,
                "status": status,
                "concept": concept,
                "existing": investigation.get("summary", ""),
                **({"decisions": decisions} if decisions else {}),
                **({"next_tools": next_tools[:2]} if next_tools else {}),
            })

        # Summarize decision points across all steps
        all_decisions = []
        for sa in step_analysis:
            for d in sa.get("decisions", []):
                all_decisions.append(d)

        create_steps = [sa for sa in step_analysis if sa["status"] == "create"]
        extend_steps = [sa for sa in step_analysis if sa["status"] == "extend"]
        integrate_steps = [sa for sa in step_analysis if sa["status"] == "integrate"]

        return {
            "session_id": session_id,
            "total_steps": len(steps),
            "steps": step_analysis,
            "summary": {
                "create": len(create_steps),
                "extend": len(extend_steps),
                "integrate": len(integrate_steps),
                "decisions_needed": len(all_decisions),
            },
            **({"decisions": all_decisions} if all_decisions else {}),
            "recommended_order": (
                "Resolve all decisions first, then implement 'extend' steps (safest), "
                "then 'integrate' steps, then 'create' steps (most risk)."
            ),
        }

    def task_begin(
        self,
        project_root: Path,
        session_id: str,
        goal: str | None = None,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = True,
        include_tests: bool = False,
    ) -> dict[str, object]:
        session_patch: dict[str, list[str]] = {"Status": ["- active"]}
        if goal is not None:
            session_patch["Goal"] = [f"- {goal}"]
        if state is not None:
            session_patch["State"] = self._as_bullets(state)
        if upcoming is not None:
            session_patch["Upcoming"] = self._as_bullets(upcoming)
        if blockers is not None:
            session_patch["Blockers"] = self._as_bullets(blockers)
        session = self.hub.sessions.update_session(project_root, session_id, session_patch)

        plan_patch: dict[str, list[str]] = {}
        if goal is not None:
            plan_patch["Purpose"] = [f"- {goal}"]
        session_scope = self.hub.sessions.read_session(project_root, session_id).sections.get("Scope", ["-"])
        if session_scope:
            plan_patch.setdefault("Scope", session_scope)
        if state is not None:
            plan_patch["Current State"] = self._as_bullets(state)
        if partial_goals is not None:
            plan_patch["Partial Goals"] = self._as_bullets(partial_goals)
        elif upcoming is not None:
            plan_patch["Partial Goals"] = self._as_bullets(upcoming)
        if end_goal is not None:
            plan_patch["End Goal"] = [f"- {end_goal}"]
        elif goal is not None:
            plan_patch["End Goal"] = [f"- {goal}"]
        if constraints is not None:
            plan_patch["Constraints"] = self._as_bullets(constraints)
        if blockers is not None:
            existing_constraints = []
            try:
                existing_plan = self.hub.sessions.read_plan(project_root, session_id)
                existing_constraints = self._clean_bullets(existing_plan.sections.get("Constraints", []))
            except Exception:
                existing_constraints = []
            merged_constraints = [item for item in existing_constraints if item and not item.startswith("Blockers: ")]
            merged_constraints.extend(f"Blockers: {item}" for item in blockers)
            plan_patch["Constraints"] = self._as_bullets(merged_constraints)
        if upcoming is not None:
            plan_patch["Next Steps"] = self._as_bullets(upcoming)
        if not plan_patch:
            plan = self.hub.sessions.read_plan(project_root, session_id)
        else:
            plan = self.hub.sessions.update_plan(project_root, session_id, plan_patch)

        context_patch: dict[str, list[str]] = {}
        if relevant_files is not None:
            context_patch["Relevant Files"] = self._as_file_bullets(relevant_files)
        if relevant_commands is not None:
            context_patch["Relevant Commands"] = self._as_bullets(relevant_commands)
        if relevant_snippets is not None:
            context_patch["Relevant Snippets"] = self._as_code_block(relevant_snippets)
        if session_facts is not None:
            context_patch["Session Facts"] = self._as_bullets(session_facts)
        if constraints is not None:
            context_patch["Constraints"] = self._as_bullets(constraints)
        context = self.hub.sessions.update_context(project_root, session_id, context_patch) if context_patch else self.hub.sessions.read_context(project_root, session_id)

        result: dict[str, object] = {
            "session": {"session_id": session.session_id, "path": str(session.path), "sections": session.sections},
            "plan": {"path": str(plan.path), "sections": plan.sections},
            "context": {"path": str(context.path), "sections": context.sections},
        }
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        return result

    def task_update(
        self,
        project_root: Path,
        session_id: str,
        state: list[str] | None = None,
        upcoming: list[str] | None = None,
        partial_goals: list[str] | None = None,
        end_goal: str | None = None,
        blockers: list[str] | None = None,
        relevant_files: list[str] | None = None,
        relevant_commands: list[str] | None = None,
        relevant_snippets: list[str] | None = None,
        session_facts: list[str] | None = None,
        constraints: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        return self.task_begin(
            project_root=project_root,
            session_id=session_id,
            goal=None,
            state=state,
            upcoming=upcoming,
            partial_goals=partial_goals,
            end_goal=end_goal,
            blockers=blockers,
            relevant_files=relevant_files,
            relevant_commands=relevant_commands,
            relevant_snippets=relevant_snippets,
            session_facts=session_facts,
            constraints=constraints,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
        )

    def task_complete(
        self,
        project_root: Path,
        session_id: str,
        result_summary: str,
        next_status: str = "done",
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        session = self.hub.sessions.read_session(project_root, session_id)
        existing_state = self._clean_bullets(session.sections.get("State", []))
        existing_state.append(result_summary)
        session_patch = {
            "Status": [f"- {next_status}"],
            "State": self._as_bullets(existing_state),
        }
        updated = self.hub.sessions.update_session(project_root, session_id, session_patch)
        try:
            existing_plan = self.hub.sessions.read_plan(project_root, session_id)
            existing_validation = self._clean_bullets(existing_plan.sections.get("Validation", []))
            existing_validation.append(f"Completion result: {result_summary}")
            plan = self.hub.sessions.update_plan(
                project_root,
                session_id,
                {
                    "Current State": self._as_bullets(existing_state),
                    "Validation": self._as_bullets(existing_validation),
                    "Next Steps": ["- Work completed; choose the next roadmap/plan slice or close the session."],
                },
            )
        except Exception:
            plan = None
        try:
            handoff = self.hub.sessions.update_handoff(
                project_root,
                session_id,
                {
                    "Current State": self._as_bullets(existing_state),
                    "What Was Done": self._as_bullets([result_summary]),
                    "What Matters Now": ["- This session has completed its current work; review whether follow-up should stay here or move to a successor session."],
                    "Suggested Next Steps": ["- Review remaining roadmap or plan work and decide whether to pause, close, or hand off this session."],
                    "Freshness": [f"- Updated {self._timestamp()} after task completion."],
                },
            )
        except Exception:
            handoff = None

        # Auto-journal the task completion
        try:
            self.hub.sessions.write_journal_entry(
                project_root, session_id,
                action_kind="task_complete",
                intent=result_summary[:120],
                outcome=f"completed → {next_status}",
            )
        except Exception:
            pass  # journal is best-effort, never block task_complete

        result: dict[str, object] = {
            "session": {"session_id": updated.session_id, "path": str(updated.path), "sections": updated.sections}
        }
        if plan is not None:
            result["plan"] = {"path": str(plan.path), "sections": plan.sections}
        if handoff is not None:
            result["handoff"] = {"path": str(handoff.path), "sections": handoff.sections}
        if include_code_bundle:
            result["code_bundle"] = self._refresh_session_code_bundle(
                project_root,
                session_id=session_id,
                include_tests=include_tests,
                sync_indexes=True,
            )
        return result

    def _refresh_session_code_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_tests: bool = False,
        sync_indexes: bool = False,
    ) -> dict[str, object]:
        if sync_indexes:
            self.hub.index.sync_all(project_root)
            self.hub.code.sync_code_files(project_root, include_tests=include_tests)
        self.hub.code.sync_session_code(project_root, session_id=session_id, include_tests=include_tests)
        return self.hub.code.get_context_bundle(project_root, session_id=session_id)

    def _as_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- {item}" for item in cleaned] or ["-"]

    def _as_file_bullets(self, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        return [f"- `{item}`" for item in cleaned]

    def _as_code_block(self, values: list[str]) -> list[str]:
        cleaned = [item.rstrip() for item in values if item and item.strip()]
        if not cleaned:
            return []
        return ["```text", *cleaned, "```"]

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _handoff_freshness(self, sections: dict[str, list[str]], stale_after_hours: int = 24) -> dict[str, object]:
        freshness_lines = sections.get("Freshness", []) if isinstance(sections, dict) else []
        for line in freshness_lines:
            match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", line)
            if not match:
                match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
            if not match:
                continue
            raw = match.group(1)
            try:
                if len(raw) == 10:
                    dt = datetime.strptime(raw, "%Y-%m-%d")
                else:
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
                age_hours = max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
                return {
                    "status": "stale" if age_hours > stale_after_hours else "fresh",
                    "timestamp": raw,
                    "age_hours": round(age_hours, 2),
                    "stale_after_hours": stale_after_hours,
                }
            except ValueError:
                continue
        return {
            "status": "unknown",
            "timestamp": None,
            "age_hours": None,
            "stale_after_hours": stale_after_hours,
        }

    def _step_changed_recently(self, step: dict[str, object], recent_hours: int = 24) -> bool:
        raw = str(step.get("updated_at") or "").strip()
        if not raw:
            return False
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        age_hours = (datetime.now() - dt).total_seconds() / 3600.0
        return age_hours <= recent_hours

    def _clean_bullets(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            stripped = item.strip()
            if not stripped or stripped == "-":
                continue
            if stripped.startswith("-"):
                stripped = stripped[1:].strip()
            if stripped:
                result.append(stripped)
        return result

    def _collect_pending_workflow(self, action_kind: str | None, project_root: Path | None) -> str:
        """Collect pending workflow actions for a given action_kind and format as a summary string."""
        if not action_kind or not project_root:
            return ""
        try:
            triggers = self.hub.workflow.triggers_for_action_kind(action_kind)
            if not triggers:
                return ""
            pending: list[dict[str, object]] = []
            flows: list[dict[str, object]] = []
            compiled = self.hub.workflow.read_compiled(project_root) or {}
            rule_defs = compiled.get("rules", []) if isinstance(compiled.get("rules"), list) else []
            for trigger in triggers:
                pending.extend(self.hub.workflow.pending_actions_for_trigger(project_root, trigger))
                flows.extend(
                    item
                    for item in rule_defs
                    if isinstance(item, dict) and item.get("trigger") == trigger
                )
            if not pending:
                return ""
            # Record trigger evaluation event
            try:
                managed = self.hub.managed_mode.get_mode(project_root)
                session_id = str(managed.get("session_id") or "").strip() or None
                self.hub.execution.record_event(
                    project_root,
                    event_kind="workflow_trigger_evaluated",
                    source_kind="operator_report",
                    session_id=session_id,
                    action_kind=action_kind,
                    status="pending",
                    payload={
                        "triggers": triggers,
                        "pending_count": len(pending),
                        "pending_actions": [
                            {"trigger": a.get("trigger"), "kind": a.get("kind")}
                            for a in pending[:5]
                        ],
                        "pending_flows": [
                            {
                                "trigger": item.get("trigger"),
                                "rule": item.get("source_rule"),
                                "steps": [
                                    step.get("action_ref") or step.get("kind")
                                    for step in (item.get("steps") or [])[:5]
                                    if isinstance(step, dict)
                                ],
                            }
                            for item in flows[:3]
                        ],
                    },
                )
            except Exception as exc:
                logger.debug("Failed to record workflow trigger evaluation event: %s", exc)
            parts = []
            for item in flows[:3]:
                trigger = item.get("trigger", "?")
                steps = [
                    str(step.get("action_ref") or step.get("kind") or "?")
                    for step in (item.get("steps") or [])
                    if isinstance(step, dict)
                ]
                if steps:
                    parts.append(f"`{trigger} → {' then '.join(steps)}`")
            if not parts:
                for action in pending[:3]:
                    trigger = action.get("trigger", "?")
                    kind = action.get("action_ref") or action.get("kind", "?")
                    parts.append(f"`{trigger} → {kind}`")
            if len(pending) > 3:
                parts.append(f"and {len(pending) - 3} more")
            return ", ".join(parts)
        except Exception as exc:
            logger.warning("Failed to collect pending workflow for action_kind=%s: %s", action_kind, exc)
            return ""

    def _memory_structure_summary(self, project_root: Path) -> dict[str, object]:
        root = project_root / ".MEMORY"
        sections: list[dict[str, object]] = []

        def add_file_section(name: str, relative_dir: str, legacy: bool = False) -> None:
            directory = root / relative_dir
            if not directory.exists():
                return
            files = sorted(path.name for path in directory.glob("*.md") if path.is_file())
            if not files and relative_dir != "config":
                return
            sections.append(
                {
                    "name": name,
                    "file_count": len(files),
                    "samples": files[:3],
                    "legacy": legacy,
                }
            )

        sessions = self.hub.sessions.list_sessions(project_root)
        archived_sessions_root = root / "archive" / "sessions"
        archived_sessions = 0
        if archived_sessions_root.exists():
            archived_sessions = sum(1 for path in archived_sessions_root.iterdir() if path.is_dir())
        sections.append(
            {
                "name": "sessions",
                "active_count": len(sessions),
                "archived_count": archived_sessions,
                "legacy": False,
            }
        )

        add_file_section("rules", "rules")
        add_file_section("domains", "domains")
        add_file_section("system", "system")
        add_file_section("config", "config")
        add_file_section("daily", "daily")
        add_file_section("archive", "archive")
        add_file_section("policy", "policy", legacy=True)
        add_file_section("architecture", "architecture", legacy=True)
        add_file_section("operations", "operations", legacy=True)
        add_file_section("decisions", "decisions", legacy=True)

        return {
            "router_files": ["/.MEMORY/.aidocs/index.aidocs", "/.MEMORY/INDEX.md"],
            "sections": sections,
        }
