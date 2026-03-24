from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


class WorkflowActionService:
    """Compile human-readable workflow automation rules into structured actions."""

    SECTION_NAMES = (
        "Automation Rules",
        "Workflow Automations",
        "Agent Workflow Actions",
    )

    def rules_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "rules" / "workflow.md"

    def config_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "workflow-actions.json"

    def read_compiled(self, project_root: Path) -> dict[str, object] | None:
        path = self.config_path(project_root)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def status(self, project_root: Path) -> dict[str, object]:
        source_path = self.rules_path(project_root)
        compiled = self.read_compiled(project_root)
        if compiled is None:
            return {
                "exists": False,
                "path": str(self.config_path(project_root)),
                "source_path": str(source_path),
                "source_exists": source_path.is_file(),
                "action_count": 0,
                "unsupported_count": 0,
                "section_found": False,
                "compiled_at": None,
            }

        return {
            "exists": True,
            "path": str(self.config_path(project_root)),
            "source_path": compiled.get("source_path") or str(source_path),
            "source_exists": bool(compiled.get("source_exists")),
            "action_count": len(compiled.get("actions", [])),
            "unsupported_count": len(compiled.get("unsupported_rules", [])),
            "section_found": bool(compiled.get("section_found")),
            "compiled_at": compiled.get("compiled_at"),
        }

    def compile_project_rules(self, project_root: Path) -> dict[str, object]:
        source_path = self.rules_path(project_root)
        target_path = self.config_path(project_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        source_exists = source_path.is_file()
        rules: list[str] = []
        section_found = False
        if source_exists:
            text = source_path.read_text(encoding="utf-8")
            rules, section_found = self._extract_rules(text)

        actions: list[dict[str, object]] = []
        unsupported: list[dict[str, str]] = []
        for rule_index, rule in enumerate(rules, start=1):
            compiled_actions, rejected = self._compile_rule(rule, rule_index=rule_index)
            actions.extend(compiled_actions)
            unsupported.extend(rejected)

        payload = {
            "version": 1,
            "path": str(target_path),
            "source_path": str(source_path),
            "source_exists": source_exists,
            "section_found": section_found,
            "compiled_at": self._timestamp(),
            "actions": actions,
            "unsupported_rules": unsupported,
        }
        target_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {
            "path": str(target_path),
            "source_path": str(source_path),
            "source_exists": source_exists,
            "section_found": section_found,
            "action_count": len(actions),
            "unsupported_count": len(unsupported),
            "actions": actions,
            "unsupported_rules": unsupported,
        }

    def _extract_rules(self, text: str) -> tuple[list[str], bool]:
        sections = self._parse_sections(text)
        for section_name in self.SECTION_NAMES:
            lines = sections.get(section_name)
            if lines is None:
                continue
            rules = []
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith("-"):
                    continue
                body = stripped[1:].strip()
                if not body or body == "-":
                    continue
                rules.append(body)
            return rules, True
        return [], False

    def _parse_sections(self, text: str) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
                continue
            if current is not None:
                sections[current].append(line)
        return sections

    def _compile_rule(self, rule_text: str, rule_index: int) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        normalized_rule = rule_text.strip().rstrip(".")
        match = re.match(r"^(?:when|after)\s+(.+?),\s*(.+)$", normalized_rule, flags=re.IGNORECASE)
        if match is None:
            return [], [{"rule": rule_text, "reason": "expected format: 'After <trigger>, <action>'."}]

        trigger_phrase = match.group(1).strip()
        action_phrase = match.group(2).strip()
        trigger = self._compile_trigger(trigger_phrase)
        if trigger is None:
            return [], [{"rule": rule_text, "reason": f"unsupported trigger: {trigger_phrase}"}]

        actions: list[dict[str, object]] = []
        unsupported: list[dict[str, str]] = []
        for segment_index, action_segment in enumerate(self._split_action_segments(action_phrase), start=1):
            compiled = self._compile_action(
                rule_text=rule_text,
                trigger=trigger,
                action_segment=action_segment,
                rule_index=rule_index,
                segment_index=segment_index,
            )
            if compiled is None:
                unsupported.append({"rule": rule_text, "reason": f"unsupported action: {action_segment}"})
                continue
            actions.append(compiled)
        return actions, unsupported

    def _compile_trigger(self, trigger_phrase: str) -> str | None:
        text = self._normalize(trigger_phrase)
        compact = text.strip()
        if any(token in text for token in (
            "each completed task",
            "task complete",
            "task completion",
            "task completes",
            "every task",
            "each completed change",
            "completed change",
            "each change",
        )) or compact in {"push after each completed task", "completed task", "completed change", "task", "change"}:
            return "task_complete"
        if any(token in text for token in ("each push", "every push", "git push", "after push", "a push")) or compact == "push":
            return "after_git_push"
        if any(token in text for token in (
            "github workflow success",
            "github actions success",
            "workflow success",
            "ci success",
            "ci succeeds",
            "successful ci",
        )) or compact in {"workflow success", "github workflow success", "github actions success", "ci success"}:
            return "after_github_workflow_success"
        if any(token in text for token in (
            "deploy success",
            "deployment success",
            "successful deploy",
            "successful deployment",
        )) or compact in {"deploy success", "deployment success"}:
            return "after_deploy_success"
        return None

    def _split_action_segments(self, action_phrase: str) -> list[str]:
        trimmed = action_phrase.strip().rstrip(".")
        if not trimmed:
            return []
        parts = re.split(r"\s+(?:and then|then)\s+|;\s*|,\s*then\s+", trimmed, flags=re.IGNORECASE)
        return [part.strip() for part in parts if part and part.strip()]

    def _compile_action(
        self,
        rule_text: str,
        trigger: str,
        action_segment: str,
        rule_index: int,
        segment_index: int,
    ) -> dict[str, object] | None:
        text = self._normalize(action_segment)

        ssh_match = re.search(r"\bssh\s+`([^`]+)`\s+`([^`]+)`", action_segment, flags=re.IGNORECASE)
        if ssh_match is not None:
            host = ssh_match.group(1).strip()
            remote_command = ssh_match.group(2).strip()
            return {
                "id": self._action_id(rule_index, segment_index, trigger, "ssh_command"),
                "trigger": trigger,
                "kind": "ssh_command",
                "enabled": True,
                "approval": "explicit_command_rule",
                "host": host,
                "remote_command": remote_command,
                "source_rule": rule_text,
                "source_segment": action_segment,
            }

        run_match = re.search(r"\brun\s+`([^`]+)`", action_segment, flags=re.IGNORECASE)
        if run_match is not None:
            command = run_match.group(1).strip()
            return {
                "id": self._action_id(rule_index, segment_index, trigger, "local_command"),
                "trigger": trigger,
                "kind": "local_command",
                "enabled": True,
                "approval": "explicit_command_rule",
                "command": command,
                "source_rule": rule_text,
                "source_segment": action_segment,
            }

        if "commit and push" in text:
            kind = "git_commit_and_push"
        elif "check git status" in text or "verify git status" in text:
            kind = "git_status_check"
        elif (
            "check github actions" in text
            or "check github workflow" in text
            or "check workflow status" in text
            or "check actions status" in text
            or "check ci status" in text
        ):
            kind = "github_workflow_check"
        elif (
            "check deploy status" in text
            or "check deployment status" in text
            or "check deployment health" in text
            or "check deploy health" in text
            or "check vps status" in text
            or "check vps deploy status" in text
        ):
            kind = "deploy_health_check"
        elif re.search(r"\bpush\b", text):
            kind = "git_push"
        elif re.search(r"\bcommit\b", text):
            kind = "git_commit"
        else:
            return None

        return {
            "id": self._action_id(rule_index, segment_index, trigger, kind),
            "trigger": trigger,
            "kind": kind,
            "enabled": True,
            "approval": "project_rule",
            "source_rule": rule_text,
            "source_segment": action_segment,
        }

    def _action_id(self, rule_index: int, segment_index: int, trigger: str, kind: str) -> str:
        return f"rule-{rule_index:02d}-{segment_index:02d}-{trigger}-{kind}"

    def _normalize(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
