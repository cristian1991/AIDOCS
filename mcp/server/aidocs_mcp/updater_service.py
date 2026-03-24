from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path


class UpdaterService:
    """Bridge to the canonical AIDOCS updater/checker scripts."""

    def __init__(self, script_root: Path) -> None:
        self.script_root = script_root

    def run_check(self, project_root: Path) -> dict[str, object]:
        return self._run_script("check", project_root)

    def run_check_legacy(self, project_root: Path) -> dict[str, object]:
        return self._run_script("check-legacy", project_root)

    def run_fix(self, project_root: Path) -> dict[str, object]:
        return self._run_script("fix", project_root)

    def inspect_legacy_runtime(self, project_root: Path) -> dict[str, object]:
        memory_root = project_root / ".MEMORY"
        legacy = {
            "has_now": (memory_root / "NOW.md").is_file(),
            "has_todo": (memory_root / "TODO.md").is_file(),
            "has_done": (memory_root / "DONE.md").is_file(),
            "has_root_plans": (memory_root / "plans").is_dir(),
            "has_root_agents": (memory_root / "agents").is_dir(),
        }
        legacy["legacy_present"] = any(legacy.values())
        return legacy

    def _run_script(self, mode: str, project_root: Path) -> dict[str, object]:
        cmd = self._build_command(mode, project_root)
        if cmd is None:
            return {
                "mode": mode,
                "project_root": str(project_root),
                "exit_code": 1,
                "stdout": "",
                "stderr": "No compatible script runner found (need bash or powershell).",
                "ok": False,
            }

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "mode": mode,
            "project_root": str(project_root),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "ok": result.returncode == 0,
        }

    def _build_command(self, mode: str, project_root: Path) -> list[str] | None:
        is_windows = platform.system() == "Windows"

        # Prefer bash on all platforms (Git Bash on Windows, native on Linux/Mac)
        bash_script = self.script_root / "check-memory-drift.sh"
        bash_bin = shutil.which("bash")
        if bash_script.is_file() and bash_bin:
            return [bash_bin, str(bash_script), mode, str(project_root)]

        # Fall back to PowerShell on Windows
        if is_windows:
            ps_script = self.script_root / "check-memory-drift.ps1"
            if ps_script.is_file():
                # Try pwsh (PowerShell Core) first, then powershell (Windows PowerShell)
                ps_bin = shutil.which("pwsh") or shutil.which("powershell")
                if ps_bin:
                    return [
                        ps_bin,
                        "-ExecutionPolicy", "Bypass",
                        "-File", str(ps_script),
                        "-Mode", mode,
                        "-ScanRoot", str(project_root),
                    ]

        return None
