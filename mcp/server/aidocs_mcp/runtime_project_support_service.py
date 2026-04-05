from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .git_helpers import run_git_sync as _run_git_sync


class RuntimeProjectSupportService:
    def __init__(self, hub: Any, logger: Any, origin_role: Any) -> None:
        self.hub = hub
        self._logger = logger
        self._origin_role = origin_role

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
        python_bin = (
            sys.executable
            or shutil.which("python")
            or shutil.which("python3")
            or "python"
        )

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
                self._logger.warning("Failed to parse existing .mcp.json: %s", exc)
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
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(parsed), 0) FROM code_files"
                ).fetchone()
                if row:
                    code_files = int(row[0] or 0)
                    parsed = int(row[1] or 0)
                row = conn.execute("SELECT COUNT(*) FROM code_modules").fetchone()
                if row:
                    modules = int(row[0] or 0)
                for row in conn.execute(
                    "SELECT COALESCE(language_tier, 'unknown') AS tier, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_tier, 'unknown')"
                ):
                    language_tiers[str(row["tier"])] = int(row["count"] or 0)
                for row in conn.execute(
                    "SELECT COALESCE(language_source, 'unknown') AS source, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_source, 'unknown')"
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
            bullets.append(
                "language tiers: "
                + ", ".join(f"{k}={v}" for k, v in sorted(language_tiers.items()))
            )
        if language_sources:
            bullets.append(
                "language sources: "
                + ", ".join(f"{k}={v}" for k, v in sorted(language_sources.items()))
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
        }

    def project_structure_gaps(self, project_root: Path) -> list[str]:
        memory_root = project_root / ".MEMORY"
        required = [
            memory_root / "INDEX.md",
            memory_root / ".aidocs" / "index.aidocs",
            memory_root / "rules" / "workflow-rules.md",
            memory_root / "rules" / "workflow-actions.md",
        ]
        missing = [
            str(path.relative_to(project_root)).replace("\\", "/")
            for path in required
            if not path.exists()
        ]
        if not (
            (project_root / "AGENTS.md").is_file()
            or (project_root / "CLAUDE.md").is_file()
        ):
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

    def _index_freshness_status(
        self, project_root: Path
    ) -> tuple[str, dict[str, object]]:
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
            code_status.get("freshness")
            if isinstance(code_status.get("freshness"), dict)
            else {}
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
