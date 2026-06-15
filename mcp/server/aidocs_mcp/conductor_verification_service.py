"""Conductor verification service — validates agent work before lane completion.

Three verification levels:
1. Post-edit validation: syntax check on modified files, scope compliance
2. Agent self-test: agent must run tests and include evidence (configurable)
3. Full-suite verification: project-wide tests with failure attribution

No lane completes without passing all applicable verification levels.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .types import LaneState


class ConductorVerificationService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @property
    def hub(self) -> Any:
        return self.runtime.hub

    def verify_lane(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        commands: list[str] | None = None,
    ) -> dict[str, object]:
        """Run verification commands for a lane and transition state.

        If commands is None, uses the lane's verification_commands from the task packet.
        Returns {passed, commands_run, failures, lane_state}.
        """
        if commands is None:
            commands = self._lane_verification_commands(project_root, session_id, lane_id)

        if not commands:
            # No verification commands — pass by default (lane has no tests)
            self.runtime._conductor_state.transition_lane(
                project_root,
                session_id,
                lane_id,
                LaneState.COMPLETED,
            )
            self.runtime._conductor_dispatch.store_lane_result(
                project_root,
                session_id,
                lane_id,
                {"success": True},
            )
            return {
                "passed": True,
                "commands_run": [],
                "failures": [],
                "lane_state": LaneState.COMPLETED.value,
                "note": "No verification commands defined for this lane.",
            }

        results: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []

        for cmd in commands:
            result = self._run_command(project_root, cmd)
            results.append(result)
            if not result["passed"]:
                failures.append(result)

        passed = len(failures) == 0
        if passed:
            self.runtime._conductor_state.transition_lane(
                project_root,
                session_id,
                lane_id,
                LaneState.COMPLETED,
            )
            self.runtime._conductor_dispatch.store_lane_result(
                project_root,
                session_id,
                lane_id,
                {"success": True, "commands_run": [r.get("command", "") for r in results]},
            )
        else:
            self.runtime._conductor_state.transition_lane(
                project_root,
                session_id,
                lane_id,
                LaneState.REOPENED,
            )

        return {
            "passed": passed,
            "commands_run": results,
            "failures": failures,
            "lane_state": LaneState.COMPLETED.value if passed else LaneState.REOPENED.value,
        }

    def verify_full_suite(
        self,
        project_root: Path,
        session_id: str,
        command: str = "python -m pytest",
    ) -> dict[str, object]:
        """Run project-wide tests and attribute failures to lanes by file ownership.

        Returns {passed, result, attributed_lanes, unattributed_failures}.
        """
        result = self._run_command(project_root, command)
        if result["passed"]:
            return {
                "passed": True,
                "result": result,
                "attributed_lanes": [],
                "unattributed_failures": [],
            }

        # Parse failure file paths from output
        failure_files = self._extract_failure_files(str(result.get("output", "")))

        # Attribute to lanes by file ownership
        from .plan_conductor import PlanConductor

        state = self.runtime._conductor_state._read_plan_conductor_state(project_root, session_id)
        conductor = PlanConductor(
            self.hub,
            project_root,
            session_id,
            paused_lanes=dict(state.get("paused_lanes", {})),
            contract_ready_lane_ids=set(state.get("contract_ready_lane_ids", [])),
            reopened_lane_ids=set(state.get("reopened_lane_ids", [])),
            lane_signals=dict(state.get("lane_signals", {})),
            lane_states=dict(state.get("lane_states", {})),
        )

        attributed: list[dict[str, str]] = []
        unattributed: list[str] = []

        for file_path in failure_files:
            owner_lanes = conductor._file_owners().get(
                conductor._display_file_identity(file_path),
                [],
            )
            if owner_lanes:
                for lane_id in owner_lanes:
                    attributed.append({"lane_id": lane_id, "file": file_path})
                    # Reopen attributed lanes
                    try:
                        self.runtime._conductor_state.transition_lane(
                            project_root,
                            session_id,
                            lane_id,
                            LaneState.REOPENED,
                        )
                    except ValueError:
                        pass  # Already reopened or invalid transition
            else:
                unattributed.append(file_path)

        return {
            "passed": False,
            "result": result,
            "attributed_lanes": attributed,
            "unattributed_failures": unattributed,
        }

    def validate_agent_output(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
        packet_result: dict[str, object],
    ) -> dict[str, object]:
        """Post-edit validation — check agent's work before accepting.

        Validates:
        1. Syntax: all modified files parse correctly
        2. Scope: agent only touched files in the lane's allowed list
        3. Self-test evidence: if require_agent_tests is on, agent must provide test results
        4. No regressions: if agent ran tests, they must pass

        Returns {valid, issues, require_resubmit}.
        """
        issues: list[dict[str, str]] = []

        # 1. Check scope compliance — did agent only touch allowed files?
        modified_files = [
            str(f).replace("\\", "/")
            for f in (packet_result.get("modified_files") or [])
            if str(f).strip()
        ]
        lane_files = self._lane_allowed_files(project_root, session_id, lane_id)
        if lane_files and modified_files:
            out_of_scope = [f for f in modified_files if not self._file_in_scope(f, lane_files)]
            if out_of_scope:
                issues.append(
                    {
                        "category": "scope_violation",
                        "severity": "high",
                        "detail": f"Agent modified {len(out_of_scope)} file(s) outside lane scope: {', '.join(out_of_scope[:5])}",
                    },
                )

        # 2. Check syntax of modified files
        for file_path in modified_files[:20]:  # cap at 20
            abs_path = project_root / file_path
            if abs_path.is_file():
                syntax_issue = self._check_file_syntax(abs_path)
                if syntax_issue:
                    issues.append(
                        {
                            "category": "syntax_error",
                            "severity": "high",
                            "detail": f"Syntax error in {file_path}: {syntax_issue}",
                        },
                    )

        # 3. Check self-test requirement
        require_tests = self._require_agent_tests(project_root)
        if require_tests:
            test_evidence = packet_result.get("test_evidence") or packet_result.get(
                "verification_results",
            )
            if not test_evidence:
                issues.append(
                    {
                        "category": "missing_test_evidence",
                        "severity": "high",
                        "detail": "conductor.require_agent_tests is enabled but agent provided no test evidence. Agent must write and run tests before reporting done.",
                    },
                )
            elif isinstance(test_evidence, dict):
                commands_run = test_evidence.get("commands_run", [])
                if not commands_run:
                    issues.append(
                        {
                            "category": "no_tests_run",
                            "severity": "high",
                            "detail": "Agent provided test evidence but no commands were actually run.",
                        },
                    )
                # Check if tests passed
                command_results = test_evidence.get("command_results", [])
                for result_text in command_results:
                    if isinstance(result_text, str) and any(
                        kw in result_text.lower() for kw in ("failed", "error", "failure")
                    ):
                        issues.append(
                            {
                                "category": "test_failure",
                                "severity": "high",
                                "detail": f"Agent's self-tests reported failures: {result_text[:200]}",
                            },
                        )

        has_high = any(i["severity"] == "high" for i in issues)

        return {
            "valid": not has_high,
            "issues": issues,
            "issue_count": len(issues),
            "require_resubmit": has_high,
            "checked": {
                "scope_compliance": bool(lane_files),
                "syntax_validation": bool(modified_files),
                "self_test_required": require_tests,
            },
        }

    def _lane_allowed_files(self, project_root: Path, session_id: str, lane_id: str) -> list[str]:
        """Get the allowed file list for a lane from the plan."""
        try:
            plan = self.hub.sessions.read_plan(project_root, session_id)
            for lane in plan.lanes if hasattr(plan, "lanes") else []:
                if lane.lane_id == lane_id:
                    return [f.replace("\\", "/") for f in lane.files]
        except Exception:
            pass
        return []

    def _file_in_scope(self, file_path: str, allowed_files: list[str]) -> bool:
        """Check if a file is within the lane's scope (exact match or subdirectory)."""
        normalized = file_path.replace("\\", "/").lower()
        for allowed in allowed_files:
            allowed_norm = allowed.replace("\\", "/").lower()
            if normalized == allowed_norm or normalized.startswith(allowed_norm.rstrip("/") + "/"):
                return True
        return False

    def _check_file_syntax(self, abs_path: Path) -> str | None:
        """Quick syntax check on a file. Returns error message or None."""
        suffix = abs_path.suffix.lower()
        if suffix == ".py":
            try:
                import ast

                ast.parse(abs_path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                return f"line {exc.lineno}: {exc.msg}"
        elif suffix == ".json":
            try:
                import json as _json

                _json.loads(abs_path.read_text(encoding="utf-8", errors="replace"))
            except _json.JSONDecodeError as exc:
                return str(exc)
        elif suffix == ".toml":
            try:
                import tomllib

                tomllib.loads(abs_path.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:
                return str(exc)
        return None

    def _require_agent_tests(self, project_root: Path) -> bool:
        """Check if conductor.require_agent_tests is enabled."""
        try:
            from .config import get_setting

            return bool(
                get_setting(
                    "conductor.require_agent_tests",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            return False

    def _lane_verification_commands(
        self,
        project_root: Path,
        session_id: str,
        lane_id: str,
    ) -> list[str]:
        """Get verification commands for a lane from session context."""
        context = self.hub.sessions.read_context(project_root, session_id)
        sections = context.sections if isinstance(context.sections, dict) else {}
        commands = [
            str(item).strip()
            for item in sections.get("Relevant Commands", [])
            if str(item).strip() and str(item).strip() != "-"
        ]
        return commands

    def _run_command(
        self,
        project_root: Path,
        command: str,
        timeout: int = 120,
    ) -> dict[str, object]:
        """Run a shell command and return structured result.

        Batch B (canonical 2026-04-29): resolves a Bash-compatible
        provider and dispatches via shell=False + [bash, -c, command].
        Refuses cleanly when no provider available.
        """
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
                source_kind="conductor_verification_service._run_command",
                capability_name="conductor_verification",
                status=("allowed" if resolved.verdict == "usable" else "observed"),
                payload=dict(resolved.audit_payload),
            )
        except Exception:
            pass
        if resolved.verdict != "usable":
            return {
                "command": command,
                "passed": False,
                "exit_code": -1,
                "output": resolved.rejection_reason or ("no Bash-compatible provider available"),
            }
        try:
            proc = subprocess.run(
                [resolved.path, "-c", command],
                shell=False,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = proc.stdout + proc.stderr
            passed = proc.returncode == 0
            return {
                "command": command,
                "passed": passed,
                "exit_code": proc.returncode,
                "output": output[-2000:] if len(output) > 2000 else output,  # Cap output
            }
        except subprocess.TimeoutExpired:
            return {
                "command": command,
                "passed": False,
                "exit_code": -1,
                "output": f"Command timed out after {timeout}s",
            }
        except Exception as exc:
            return {
                "command": command,
                "passed": False,
                "exit_code": -1,
                "output": str(exc),
            }

    def _extract_failure_files(self, output: str) -> list[str]:
        """Extract file paths from test failure output (pytest format)."""
        files: list[str] = []
        # pytest FAILED lines: FAILED tests/foo/test_bar.py::test_name
        for match in re.finditer(r"FAILED\s+(\S+\.py)::", output):
            path = match.group(1).replace("\\", "/")
            if path not in files:
                files.append(path)
        # pytest ERROR lines: ERROR tests/foo/test_bar.py
        for match in re.finditer(r"ERROR\s+(\S+\.py)", output):
            path = match.group(1).replace("\\", "/")
            if path not in files:
                files.append(path)
        # Generic file:line patterns from tracebacks
        for match in re.finditer(r'File "([^"]+\.py)", line \d+', output):
            path = match.group(1).replace("\\", "/")
            # Only include project-relative paths
            if not path.startswith("/") and "site-packages" not in path:
                if path not in files:
                    files.append(path)
        return files
