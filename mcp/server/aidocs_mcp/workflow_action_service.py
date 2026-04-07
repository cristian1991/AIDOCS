from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path


class WorkflowActionService:
    """Compile human-readable workflow rules into structured actions."""

    RULE_SECTION_NAMES = (
        "Workflow Rules",
        "Automation Rules",
        "Workflow Automations",
    )

    ACTION_SECTION_NAMES = (
        "Workflow Actions",
        "Agent Workflow Actions",
    )

    WORKFLOW_ACTION_KINDS = {
        "git_commit_and_push",
        "git_status_check",
        "github_workflow_check",
        "deploy_health_check",
        "git_push",
        "git_commit",
    }

    _ACTION_KIND_TO_TRIGGERS: dict[str, list[str]] = {
        "task_complete": ["task_complete"],
        "task_begin": ["task_begin"],
        "edit": ["task_complete"],
        "write_memory": ["memory_write"],
        "archive": ["archive"],
        "project_update": ["project_update"],
        "git_push": ["after_git_push"],
        "git_commit_and_push": ["after_git_push"],
        "github_workflow_check": ["after_github_workflow_success"],
    }

    def __init__(self) -> None:
        self._action_token_mapping: list[tuple[str, tuple[str, ...]]] | None = None

    def rules_file_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "rules" / "workflow.md"

    def legacy_rules_file_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "rules" / "workflow-rules.md"

    def actions_file_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "rules" / "workflow-actions.md"

    def source_paths(self, project_root: Path) -> dict[str, Path]:
        return {
            "rules": self.rules_file_path(project_root),
            "legacy_rules": self.legacy_rules_file_path(project_root),
            "actions": self.actions_file_path(project_root),
        }

    def config_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "workflow-actions.json"

    def read_compiled(self, project_root: Path) -> dict[str, object] | None:
        path = self.config_path(project_root)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def status(self, project_root: Path) -> dict[str, object]:
        paths = self.source_paths(project_root)
        compiled = self.read_compiled(project_root)
        if compiled is None:
            return {
                "exists": False,
                "path": str(self.config_path(project_root)),
                "source_path": str(paths["rules"]),
                "source_exists": any(path.is_file() for path in paths.values()),
                "source_paths": {key: str(path) for key, path in paths.items()},
                "action_definition_count": 0,
                "rule_count": 0,
                "action_count": 0,
                "unsupported_count": 0,
                "section_found": False,
                "compiled_at": None,
            }

        return {
            "exists": True,
            "path": str(self.config_path(project_root)),
            "source_path": compiled.get("source_path") or str(paths["rules"]),
            "source_exists": bool(compiled.get("source_exists")),
            "source_paths": compiled.get("source_paths") or {key: str(path) for key, path in paths.items()},
            "action_definition_count": len(compiled.get("action_definitions", [])),
            "rule_count": len(compiled.get("rules", [])),
            "action_count": len(compiled.get("actions", [])),
            "unsupported_count": len(compiled.get("unsupported_rules", [])),
            "section_found": bool(compiled.get("section_found")),
            "compiled_at": compiled.get("compiled_at"),
        }

    def triggers_for_action_kind(self, action_kind: str) -> list[str]:
        return self._ACTION_KIND_TO_TRIGGERS.get(action_kind, [])

    def pending_actions_for_trigger(self, project_root: Path, trigger: str) -> list[dict[str, object]]:
        compiled = self.read_compiled(project_root)
        if not compiled:
            return []
        actions = compiled.get("actions", [])
        if not isinstance(actions, list):
            return []
        return [item for item in actions if isinstance(item, dict) and item.get("trigger") == trigger]

    def compile_project_rules(self, project_root: Path) -> dict[str, object]:
        paths = self.source_paths(project_root)
        target_path = self.config_path(project_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        source_exists = any(path.is_file() for path in paths.values())
        rules: list[str] = []
        defs: list[str] = []
        section_found = False
        if paths["rules"].is_file() or paths["legacy_rules"].is_file() or paths["actions"].is_file():
            if paths["rules"].is_file():
                sections = self._parse_sections(paths["rules"].read_text(encoding="utf-8"))
                rules, rules_found = self._extract_rules(sections, self.RULE_SECTION_NAMES)
                section_found = section_found or rules_found
            elif paths["legacy_rules"].is_file():
                sections = self._parse_sections(paths["legacy_rules"].read_text(encoding="utf-8"))
                rules, rules_found = self._extract_rules(sections, self.RULE_SECTION_NAMES)
                section_found = section_found or rules_found
            if paths["actions"].is_file():
                sections = self._parse_sections(paths["actions"].read_text(encoding="utf-8"))
                defs, defs_found = self._extract_rules(sections, self.ACTION_SECTION_NAMES)
                section_found = section_found or defs_found
        action_defs: list[dict[str, object]] = []
        action_map: dict[str, dict[str, object]] = {}
        unsupported: list[dict[str, str]] = []
        for def_index, item in enumerate(defs, start=1):
            compiled, rejected = self._compile_action_definition(item, def_index)
            if compiled is not None:
                action_defs.append(compiled)
                action_map[self._normalize_action_name(str(compiled.get("name") or ""))] = compiled
            if rejected is not None:
                unsupported.append(rejected)

        actions: list[dict[str, object]] = []
        compiled_rules: list[dict[str, object]] = []
        for rule_index, rule in enumerate(rules, start=1):
            compiled_actions, compiled_rule, rejected = self._compile_rule(rule, rule_index, action_map)
            actions.extend(compiled_actions)
            if compiled_rule is not None:
                compiled_rules.append(compiled_rule)
            unsupported.extend(rejected)

        payload = {
            "version": 2,
            "path": str(target_path),
            "source_path": str(paths["rules"] if paths["rules"].is_file() or not paths["legacy_rules"].is_file() else paths["legacy_rules"]),
            "source_exists": source_exists,
            "source_paths": {key: str(path) for key, path in paths.items()},
            "section_found": section_found,
            "compiled_at": self._timestamp(),
            "action_definitions": action_defs,
            "rules": compiled_rules,
            "actions": actions,
            "unsupported_rules": unsupported,
        }
        target_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {
            "path": str(target_path),
            "source_path": str(paths["rules"]),
            "source_exists": source_exists,
            "source_paths": {key: str(path) for key, path in paths.items()},
            "section_found": section_found,
            "action_definition_count": len(action_defs),
            "rule_count": len(compiled_rules),
            "action_count": len(actions),
            "unsupported_count": len(unsupported),
            "action_definitions": action_defs,
            "rules": compiled_rules,
            "actions": actions,
            "unsupported_rules": unsupported,
        }

    def _resolve_action_tokens_dir(self) -> Path:
        candidates = [
            Path(__file__).resolve().parents[3] / "action_tokens",
            Path(__file__).resolve().parent / "action_tokens",
        ]
        env_path = os.environ.get("AIDOCS_PATH")
        if env_path:
            candidates.insert(1, Path(env_path) / "action_tokens")
        for item in candidates:
            if item.is_dir():
                return item
        return candidates[0]

    def _load_action_tokens(self) -> list[tuple[str, tuple[str, ...]]]:
        root = self._resolve_action_tokens_dir()
        if not root.is_dir():
            return []

        merged: dict[str, list[str]] = {}

        # TOML files take precedence
        loaded_stems: set[str] = set()
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                tomllib = None  # type: ignore[assignment]

        if tomllib is not None:
            for toml_file in sorted(root.glob("*.toml")):
                loaded_stems.add(toml_file.stem)
                try:
                    from .intent_guard import _merge_toml_tokens
                    _merge_toml_tokens(toml_file, merged)
                except Exception:
                    continue

        # YAML fallback for stems not covered by TOML
        for yaml_file in sorted(root.glob("*.yaml")):
            if yaml_file.stem in loaded_stems:
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
            except Exception:
                continue

        result: list[tuple[str, tuple[str, ...]]] = []
        for action_kind, tokens in merged.items():
            if action_kind not in self.WORKFLOW_ACTION_KINDS:
                continue
            seen: set[str] = set()
            unique: list[str] = []
            for token in tokens:
                if token in seen:
                    continue
                seen.add(token)
                unique.append(token)
            result.append((action_kind, tuple(unique)))
        return result

    def _get_action_tokens(self) -> list[tuple[str, tuple[str, ...]]]:
        if self._action_token_mapping is None:
            self._action_token_mapping = self._load_action_tokens()
        return self._action_token_mapping

    def _classify_action_segment(self, text: str) -> str | None:
        for action_kind, tokens in self._get_action_tokens():
            if any(token in text for token in tokens):
                return action_kind
        return None

    def _extract_rules(self, sections: dict[str, list[str]], names: tuple[str, ...]) -> tuple[list[str], bool]:
        for section_name in names:
            lines = sections.get(section_name)
            if lines is None:
                continue
            rules: list[str] = []
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

    def _compile_rule(
        self,
        rule_text: str,
        rule_index: int,
        action_map: dict[str, dict[str, object]],
    ) -> tuple[list[dict[str, object]], dict[str, object] | None, list[dict[str, str]]]:
        normalized_rule = rule_text.strip().rstrip(".")
        match = re.match(r"^(?:when|after)\s+(.+?),\s*(.+)$", normalized_rule, flags=re.IGNORECASE)
        if match is None:
            return [], None, [{"rule": rule_text, "reason": "expected format: 'After <trigger>, <action>'."}]

        trigger_phrase = match.group(1).strip()
        action_phrase = match.group(2).strip()
        trigger = self._compile_trigger(trigger_phrase)
        if trigger is None:
            return [], None, [{"rule": rule_text, "reason": f"unsupported trigger: {trigger_phrase}"}]

        actions: list[dict[str, object]] = []
        steps: list[dict[str, object]] = []
        unsupported: list[dict[str, str]] = []
        for segment_index, action_segment in enumerate(self._split_action_segments(action_phrase), start=1):
            compiled = self._compile_action(rule_text, trigger, action_segment, rule_index, segment_index, action_map)
            if compiled is None:
                unsupported.append({"rule": rule_text, "reason": f"unsupported action: {action_segment}"})
                continue
            actions.append(compiled)
            steps.append(
                {
                    "index": segment_index,
                    "kind": compiled.get("kind"),
                    "source_segment": action_segment,
                    "action_ref": compiled.get("action_ref"),
                }
            )

        return actions, {
            "id": f"workflow-rule-{rule_index:02d}-{trigger}",
            "trigger": trigger,
            "source_rule": rule_text,
            "steps": steps,
        }, unsupported

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

    def _compile_action_definition(
        self,
        item: str,
        def_index: int,
    ) -> tuple[dict[str, object] | None, dict[str, str] | None]:
        match = re.match(r"^([^:]+):\s*(.+)$", item.strip())
        if match is None:
            return None, {"rule": item, "reason": "expected format: '<name>: <action>' in Workflow Actions."}
        name = match.group(1).strip()
        segment = match.group(2).strip()
        template = self._compile_action_template(segment)
        if template is None:
            return None, {"rule": item, "reason": f"unsupported action definition: {segment}"}
        return {
            "id": f"workflow-action-{def_index:02d}-{self._normalize_action_name(name)}",
            "name": name,
            **template,
            "source_rule": item,
            "source_segment": segment,
        }, None

    def _compile_action_template(self, action_segment: str) -> dict[str, object] | None:
        text = self._normalize(action_segment)
        ssh_match = re.search(r"\bssh\s+`([^`]+)`\s+`([^`]+)`", action_segment, flags=re.IGNORECASE)
        if ssh_match is not None:
            return {
                "kind": "ssh_command",
                "enabled": True,
                "approval": "explicit_command_rule",
                "host": ssh_match.group(1).strip(),
                "remote_command": ssh_match.group(2).strip(),
            }

        run_match = re.search(r"\brun\s+`([^`]+)`", action_segment, flags=re.IGNORECASE)
        if run_match is not None:
            return {
                "kind": "local_command",
                "enabled": True,
                "approval": "explicit_command_rule",
                "command": run_match.group(1).strip(),
            }

        kind = self._classify_action_segment(text)
        if kind is None:
            return None
        return {
            "kind": kind,
            "enabled": True,
            "approval": "project_rule",
        }

    def _compile_action(
        self,
        rule_text: str,
        trigger: str,
        action_segment: str,
        rule_index: int,
        segment_index: int,
        action_map: dict[str, dict[str, object]],
    ) -> dict[str, object] | None:
        ref = action_map.get(self._normalize_action_name(action_segment))
        template = ref or self._compile_action_template(action_segment)
        if template is None:
            return None
        kind = str(template.get("kind") or "").strip()
        result = {
            "id": self._action_id(rule_index, segment_index, trigger, kind),
            "trigger": trigger,
            "kind": kind,
            "enabled": bool(template.get("enabled", True)),
            "approval": template.get("approval", "project_rule"),
            "source_rule": rule_text,
            "source_segment": action_segment,
        }
        for key in ("command", "host", "remote_command"):
            if key in template:
                result[key] = template[key]
        if ref is not None:
            result["action_ref"] = ref.get("name")
        return result

    def _action_id(self, rule_index: int, segment_index: int, trigger: str, kind: str) -> str:
        return f"rule-{rule_index:02d}-{segment_index:02d}-{trigger}-{kind}"

    def _normalize_action_name(self, value: str) -> str:
        return re.sub(r"\s+", "_", value.replace("`", "").strip().lower())

    def _normalize(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
