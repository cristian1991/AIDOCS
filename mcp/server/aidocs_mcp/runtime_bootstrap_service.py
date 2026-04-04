from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .git_helpers import run_git_sync as _run_git_sync


class RuntimeBootstrapService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    def project_init(
        self, project_root: Path, init_git: bool = True, create_remote: bool = False
    ) -> dict[str, object]:
        root = project_root
        if not root.is_dir():
            root.mkdir(parents=True, exist_ok=True)

        created: list[str] = []
        skipped: list[str] = []

        templates_root = self.hub.sessions.templates_root
        aidocs_bundle_root = templates_root.parent
        memory_template = aidocs_bundle_root / "templates" / "memory"
        memory_dest = root / ".MEMORY"
        aidocs_dest = memory_dest / ".aidocs"

        if memory_template.is_dir():
            self.runtime._copy_missing_tree(
                memory_template, memory_dest, ".MEMORY", created, skipped
            )
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

        for src_file in aidocs_bundle_root.glob("*.aidocs"):
            self.runtime._copy_missing_file(
                src_file,
                aidocs_dest / src_file.name,
                f".MEMORY/.aidocs/{src_file.name}",
                created,
                skipped,
            )
        self.runtime._copy_missing_tree(
            aidocs_bundle_root / "personalities",
            aidocs_dest / "personalities",
            ".MEMORY/.aidocs/personalities",
            created,
            skipped,
        )

        workflow_rules = memory_dest / "rules" / "workflow-rules.md"
        if not workflow_rules.exists():
            workflow_rules.parent.mkdir(parents=True, exist_ok=True)
            workflow_rules.write_text(
                "# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8"
            )
            created.append(".MEMORY/rules/workflow-rules.md")
        else:
            skipped.append(".MEMORY/rules/workflow-rules.md")

        workflow_actions = memory_dest / "rules" / "workflow-actions.md"
        if not workflow_actions.exists():
            workflow_actions.parent.mkdir(parents=True, exist_ok=True)
            workflow_actions.write_text(
                "# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8"
            )
            created.append(".MEMORY/rules/workflow-actions.md")
        else:
            skipped.append(".MEMORY/rules/workflow-actions.md")

        router = aidocs_dest / "index.aidocs"
        if not router.exists():
            router.parent.mkdir(parents=True, exist_ok=True)
            src_router = aidocs_bundle_root / "index.aidocs"
            if src_router.is_file():
                shutil.copy2(str(src_router), str(router))
            else:
                router.write_text(
                    "# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n",
                    encoding="utf-8",
                )
            created.append(".MEMORY/.aidocs/index.aidocs")

        for tmpl_name in ["AGENTS.md", "CLAUDE.md"]:
            dest = root / tmpl_name
            src = templates_root.parents[1] / tmpl_name
            managed_content = ""
            if src.is_file():
                managed_content = src.read_text(encoding="utf-8")
            else:
                managed_content = f"<!-- AIDOCS:BEGIN -->\n# {tmpl_name.replace('.md', '')}\n\nAIDOCS-managed project.\n<!-- AIDOCS:END -->\n"

            if dest.exists():
                existing = dest.read_text(encoding="utf-8")
                end_tag = "<!-- AIDOCS:END -->"
                end_idx = existing.find(end_tag)
                if end_idx >= 0:
                    # Preserve user section after AIDOCS:END
                    user_section = existing[end_idx + len(end_tag):].lstrip("\r\n")
                    if user_section.strip():
                        managed_content = managed_content.rstrip() + "\n\n" + user_section
                else:
                    # No tags — backup existing content as user section
                    dest.with_suffix(".md.backup").write_text(existing, encoding="utf-8")
                    managed_content = managed_content.rstrip() + "\n\n" + existing
                dest.write_text(managed_content, encoding="utf-8")
                created.append(f"{tmpl_name} (updated)")
            else:
                dest.write_text(managed_content, encoding="utf-8")
                created.append(tmpl_name)

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
                    _run_git_sync(
                        str(root),
                        "commit",
                        "-m",
                        "chore: initialize project with AIDOCS",
                    )
                    git_result = {"action": "initialized", "initial_commit": True}
                except Exception as exc:
                    git_result = {"action": "failed", "reason": str(exc)}
            except Exception as exc:
                git_result = {"action": "failed", "reason": str(exc)}

        if create_remote and git_result.get("action") == "initialized":
            try:
                output = _run_git_sync(str(root), "remote", "get-url", "origin")
                git_result["remote"] = {
                    "created": False,
                    "reason": f"Remote already exists: {output}",
                }
            except RuntimeError:
                try:
                    import tempfile as _tf

                    _gh_out = None
                    try:
                        with _tf.NamedTemporaryFile(
                            mode="w", suffix=".gh.out", delete=False
                        ) as _f:
                            _gh_out = _f.name
                        with open(_gh_out, "w") as _fh:
                            result = subprocess.run(
                                [
                                    "gh",
                                    "repo",
                                    "create",
                                    root.name,
                                    "--private",
                                    "--source",
                                    str(root),
                                    "--push",
                                ],
                                cwd=str(root),
                                stdin=subprocess.DEVNULL,
                                stdout=_fh,
                                stderr=subprocess.DEVNULL,
                                text=True,
                                timeout=30,
                                check=False,
                            )
                        result.stdout = (
                            Path(_gh_out)
                            .read_text(encoding="utf-8", errors="ignore")
                            .strip()
                        )
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
                        "reason": (result.stderr or "").strip()
                        if result.returncode != 0
                        else "",
                    }
                except FileNotFoundError:
                    git_result["remote"] = {
                        "created": False,
                        "reason": "gh CLI not installed",
                    }
                except Exception as exc:
                    git_result["remote"] = {"created": False, "reason": str(exc)}

        mcp_config_result = self.runtime.ensure_claude_mcp_config(root)
        return {
            "initialized": True,
            "created": created,
            "skipped": skipped,
            "git": git_result,
            "origins": self.runtime.project_origins(root),
            "repo_summary": self.runtime.repo_summary(root),
            "mcp_config": mcp_config_result,
            "next_step": "Call project_bootstrap_or_resume to activate managed mode and select a session.",
        }
