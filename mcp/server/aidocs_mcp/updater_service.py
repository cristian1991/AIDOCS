from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


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

        import os
        import tempfile

        # Use temp files instead of pipes to avoid conflicts with MCP stdio transport
        out_path = err_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".out", delete=False) as out_f:
                out_path = out_f.name
            with tempfile.NamedTemporaryFile(mode="w", suffix=".err", delete=False) as err_f:
                err_path = err_f.name
            with (
                open(out_path, "w", encoding="utf-8") as out_fh,
                open(err_path, "w", encoding="utf-8") as err_fh,
            ):
                # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
                from .shell_egress_service import audited_run

                result = audited_run(
                    cmd,
                    fingerprint=("updater_service.py", "_run_script", "subprocess.run"),
                    reason="updater-script-exec",
                    run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
                    stdout=out_fh,
                    stderr=err_fh,
                    text=True,
                    check=False,
                    timeout=60,
                    creationflags=_WIN_NO_WINDOW,
                )
            stdout = Path(out_path).read_text(encoding="utf-8", errors="ignore")
            stderr = Path(err_path).read_text(encoding="utf-8", errors="ignore")
        except subprocess.TimeoutExpired:
            return {
                "mode": mode,
                "project_root": str(project_root),
                "exit_code": 1,
                "stdout": "",
                "stderr": "Script timed out",
                "ok": False,
            }
        except Exception as exc:
            return {
                "mode": mode,
                "project_root": str(project_root),
                "exit_code": 1,
                "stdout": "",
                "stderr": str(exc),
                "ok": False,
            }
        finally:
            for p in (out_path, err_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        return {
            "mode": mode,
            "project_root": str(project_root),
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "ok": result.returncode == 0,
        }

    def _build_command(self, mode: str, project_root: Path) -> list[str] | None:
        # AIDOCS shell provider lock — Batch B (canonical 2026-04-29).
        # Resolve a Bash-compatible provider via the AIDOCS resolver
        # and dispatch the .sh drift-check script through it. The
        # PowerShell silent fallback that lived here is removed —
        # PowerShell-as-provider is gated by the superadmin flag AND
        # a Batch-C provider implementation. Batch B has neither, so
        # PowerShell dispatch is no longer reachable from this code
        # path. The check-memory-drift.ps1 file is left on disk as
        # dead code (separate cleanup); this builder no longer
        # references it.
        from .shell_resolver import (
            _emit_resolution_event as _emit_audit,
        )
        from .shell_resolver import (
            resolve_shell as _resolve_shell,
        )

        resolved = _resolve_shell(project_root, session_id=None)
        try:
            _emit_audit(
                project_root=project_root,
                session_id=None,
                source_kind="updater_service._build_command",
                capability_name="updater_drift_check",
                status=("allowed" if resolved.verdict == "usable" else "observed"),
                payload=dict(resolved.audit_payload),
            )
        except Exception:
            pass
        if resolved.verdict != "usable":
            return None

        bash_script = self.script_root / "check-memory-drift.sh"
        if not bash_script.is_file():
            return None

        return [resolved.path, str(bash_script), mode, str(project_root)]
