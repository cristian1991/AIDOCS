from __future__ import annotations

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

    AGENT_RULE_SECTION_NAMES = (
        "Agent Workflow Rules",
        "Agent Rules",
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
        # Storage moved to sqlite (workflow_actions table) in Beat 3.
        # Service keeps its read shape identical so callers (status,
        # pending_actions_for_trigger, runtime_service.compile_actions
        # consumer chain) don't have to change.
        from .workflow_actions_store import WorkflowActionsStore

        store = WorkflowActionsStore()
        store.init_db(project_root)
        return store.get(project_root)

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
            "source_paths": compiled.get("source_paths")
            or {key: str(path) for key, path in paths.items()},
            "action_definition_count": len(compiled.get("action_definitions", [])),
            "rule_count": len(compiled.get("rules", [])),
            "action_count": len(compiled.get("actions", [])),
            "unsupported_count": len(compiled.get("unsupported_rules", [])),
            "section_found": bool(compiled.get("section_found")),
            "compiled_at": compiled.get("compiled_at"),
        }

    def triggers_for_action_kind(self, action_kind: str) -> list[str]:
        return self._ACTION_KIND_TO_TRIGGERS.get(action_kind, [])

    def pending_actions_for_trigger(
        self,
        project_root: Path,
        trigger: str,
    ) -> list[dict[str, object]]:
        compiled = self.read_compiled(project_root)
        if not compiled:
            return []
        actions = compiled.get("actions", [])
        if not isinstance(actions, list):
            return []
        return [
            item for item in actions if isinstance(item, dict) and item.get("trigger") == trigger
        ]

    def compile_project_rules(self, project_root: Path) -> dict[str, object]:
        from . import workflow_definitions_store as _wd

        paths = self.source_paths(project_root)
        target_path = self.config_path(project_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # SQL is canonical for workflow definitions (Stage 5c). compile reads
        # the active workflow_definitions rows. When the SQL store is still
        # empty but legacy markdown exists, do a ONE-SHOT, audited migration
        # of the markdown into sqlite, then read SQL — the markdown is read
        # exactly once and is never authority again.
        rules: list[str] = _wd.active_bodies(project_root, "rule")
        defs: list[str] = _wd.active_bodies(project_root, "action")
        definitions_source = "sqlite"
        migration_degraded = False
        migration_audit_error: str | None = None

        # Per-kind migration (Stage 5c): migrate a kind only when its SQL store
        # is empty AND its legacy markdown exists, so a partially-populated SQL
        # store (e.g. rules present, actions still in markdown) still migrates
        # the missing kind. A kind that already has SQL rows is never re-read
        # from markdown — markdown is never authority twice.
        rules_md = paths["rules"] if paths["rules"].is_file() else paths["legacy_rules"]
        need_rule_migration = (not rules) and rules_md.is_file()
        need_action_migration = (not defs) and paths["actions"].is_file()
        if need_rule_migration or need_action_migration:
            migrated = self._migrate_markdown_to_definitions(
                project_root,
                paths,
                migrate_rules=need_rule_migration,
                migrate_actions=need_action_migration,
            )
            if migrated.get("degraded"):
                # Audit could not be written — do NOT pretend the migration was
                # clean. No rows were imported (ledger-first), so SQL stays
                # empty and the markdown remains for a future retry; compile
                # surfaces the degraded state instead of silent success.
                migration_degraded = True
                migration_audit_error = migrated.get("audit_error")
                definitions_source = "sqlite_migration_degraded"
            elif migrated.get("migrated"):
                definitions_source = "sqlite_migrated"
            rules = _wd.active_bodies(project_root, "rule")
            defs = _wd.active_bodies(project_root, "action")

        section_found = bool(rules or defs)
        source_exists = section_found or any(path.is_file() for path in paths.values())
        action_defs: list[dict[str, object]] = []
        action_map: dict[str, dict[str, object]] = {}
        unsupported: list[dict[str, str]] = []
        for def_index, item in enumerate(defs, start=1):
            compiled, rejected = self._compile_action_definition(item, def_index)
            if compiled is not None:
                # Heuristic-judge pre-scan on action_definitions
                # (2026-04-24): a rule that references a named action
                # pulls its command template from action_map at compile
                # time — so if the template carries a destructive
                # command, the judge must reject it BEFORE any rule
                # can bind to it. Rejecting here keeps the malicious
                # name out of action_map, so downstream rule bindings
                # also get rejected automatically (unknown name).
                judged_reason = self._judge_command_in_action(
                    compiled,
                    project_root,
                )
                if judged_reason is not None:
                    unsupported.append(
                        {
                            "rule": item,
                            "reason": judged_reason,
                        },
                    )
                    continue
                action_defs.append(compiled)
                action_map[self._normalize_action_name(str(compiled.get("name") or ""))] = compiled
            if rejected is not None:
                unsupported.append(rejected)

        actions: list[dict[str, object]] = []
        compiled_rules: list[dict[str, object]] = []
        for rule_index, rule in enumerate(rules, start=1):
            compiled_actions, compiled_rule, rejected = self._compile_rule(
                rule,
                rule_index,
                action_map,
            )
            actions.extend(compiled_actions)
            if compiled_rule is not None:
                compiled_rules.append(compiled_rule)
            unsupported.extend(rejected)

        payload = {
            "version": 2,
            "path": str(target_path),
            "source_path": str(
                paths["rules"]
                if paths["rules"].is_file() or not paths["legacy_rules"].is_file()
                else paths["legacy_rules"],
            ),
            "source_exists": source_exists,
            "source_paths": {key: str(path) for key, path in paths.items()},
            "definitions_source": definitions_source,
            "migration_degraded": migration_degraded,
            "migration_audit_error": migration_audit_error,
            "section_found": section_found,
            "compiled_at": self._timestamp(),
            "action_definitions": action_defs,
            "rules": compiled_rules,
            "actions": actions,
            "unsupported_rules": unsupported,
        }
        # Defense-in-depth: also scan any action that snuck through
        # via a direct-segment rule (e.g. a rule body literally saying
        # `run \`rm -rf /\`` rather than referencing a named action).
        # The action_defs loop above catches templates; this catches
        # inline commands in rule bindings.
        judged_actions: list[dict[str, object]] = []
        for action in actions:
            reason = self._judge_command_in_action(action, project_root)
            if reason is not None:
                unsupported.append(
                    {
                        "rule": str(
                            action.get("source_rule") or action.get("source_segment") or "",
                        ),
                        "reason": reason,
                    },
                )
                continue
            judged_actions.append(action)
        actions = judged_actions
        payload["actions"] = actions
        payload["unsupported_rules"] = unsupported

        # Persist via the sqlite store; target_path stays computed for
        # the return-value contract that callers use to display "where
        # the compiled rules live" — file is absent post-Beat-3.
        # init_db() must run before set() because compile_project_rules
        # may be the first store touch on a fresh project.
        from .workflow_actions_store import WorkflowActionsStore

        store = WorkflowActionsStore()
        store.init_db(project_root)
        store.set(project_root, payload)
        return {
            "path": str(target_path),
            "source_path": str(paths["rules"]),
            "source_exists": source_exists,
            "source_paths": {key: str(path) for key, path in paths.items()},
            "definitions_source": definitions_source,
            "migration_degraded": migration_degraded,
            "migration_audit_error": migration_audit_error,
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

    def _migrate_markdown_to_definitions(
        self,
        project_root: Path,
        paths: dict[str, Path],
        *,
        migrate_rules: bool = True,
        migrate_actions: bool = True,
    ) -> dict[str, object]:
        """One-shot, ledger-first import of legacy workflow-rules.md /
        workflow-actions.md into the workflow_definitions sqlite table.

        This is a one-time authority migration, so it is audited BEFORE any
        row is imported: if the audit event cannot be written, NOTHING is
        imported and a degraded result is returned, so the markdown never
        becomes SQL authority unaudited. The caller invokes this per kind only
        when that kind's SQL store is empty, so each kind migrates at most once.

        Returns a result dict:
          {"migrated": bool, "rules": int, "actions": int}              on success
          {"migrated": False}                                           when nothing to migrate
          {"migrated": False, "degraded": True, "audit_error": str,
           "rules": int, "actions": int}                               when audit failed
        """
        from . import workflow_definitions_store as _wd

        rules_path = paths["rules"] if paths["rules"].is_file() else paths["legacy_rules"]
        migrated_rules: list[str] = []
        migrated_actions: list[str] = []
        if migrate_rules and rules_path.is_file():
            sections = self._parse_sections(
                rules_path.read_text(encoding="utf-8"),
            )
            migrated_rules, _ = self._extract_rules(
                sections,
                self.RULE_SECTION_NAMES,
            )
        if migrate_actions and paths["actions"].is_file():
            sections = self._parse_sections(
                paths["actions"].read_text(encoding="utf-8"),
            )
            migrated_actions, _ = self._extract_rules(
                sections,
                self.ACTION_SECTION_NAMES,
            )
        if not migrated_rules and not migrated_actions:
            return {"migrated": False}

        # Ledger-first: record the migration audit BEFORE importing the rows.
        # A swallowed audit failure on a one-time authority migration is
        # exactly the silent-unaudited-proceed this guards against, so on
        # failure we import nothing and report degraded.
        import hashlib

        def _hashes(bodies: list[str]) -> list[str]:
            return [hashlib.sha256(b.encode("utf-8")).hexdigest() for b in bodies]

        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="workflow_definitions_migrated",
                source_kind="workflow_action_service",
                capability_name="compile_project_rules",
                action_kind="migrate",
                target_entity="workflow_definitions",
                status="migrated",
                payload={
                    "rules": len(migrated_rules),
                    "actions": len(migrated_actions),
                    "rule_body_hashes": _hashes(migrated_rules),
                    "action_body_hashes": _hashes(migrated_actions),
                    "via": "markdown_one_shot",
                },
            )
        except Exception as exc:
            return {
                "migrated": False,
                "degraded": True,
                "audit_error": str(exc),
                "rules": len(migrated_rules),
                "actions": len(migrated_actions),
            }

        for body in migrated_rules:
            _wd.add(
                project_root,
                kind="rule",
                body=body,
                source="markdown_migration",
            )
        for body in migrated_actions:
            _wd.add(
                project_root,
                kind="action",
                body=body,
                source="markdown_migration",
            )
        return {
            "migrated": True,
            "rules": len(migrated_rules),
            "actions": len(migrated_actions),
        }

    def _judge_command_in_action(
        self,
        action: dict[str, object],
        project_root: Path,
    ) -> str | None:
        """Return rejection reason if the action's command is destructive,
        else None. Scans local_command.command and ssh_command.
        remote_command via the same judge ai_run uses — workflow
        actions can fire at any trigger so destructive content must
        be stopped at compile time, not execution time.
        """
        kind = str(action.get("kind") or "")
        if kind == "local_command":
            cmd = str(action.get("command") or "")
        elif kind == "ssh_command":
            cmd = str(action.get("remote_command") or "")
        else:
            return None
        if not cmd:
            return None
        try:
            from .heuristic_judge import evaluate_tool_call

            judged = evaluate_tool_call(
                "ai_run",
                {"command": cmd},
                project_root=project_root,
            )
        except Exception:
            return None
        if not judged.should_block:
            return None
        top = next(
            (v for v in judged.verdicts if v.risk == "critical"),
            judged.verdicts[0] if judged.verdicts else None,
        )
        rule_id = top.rule_id if top else "heuristic"
        desc = top.description if top else "destructive pattern detected"
        return (
            f"judge refused: {desc} (rule: {rule_id}). "
            f"Workflow actions can trigger at any time — destructive "
            f"commands must be rejected at compile time, not at "
            f"execution time."
        )

    def _resolve_intent_tokens_dir(self) -> Path:
        """Deprecated shim — sqlite is the source. Returns the
        package-bundled seed dir post-Phase 5b. No active consumer in
        this module calls it anymore.
        """
        from pathlib import Path as _Path

        return _Path(__file__).resolve().parent / "seed" / "intent_tokens"

    def _load_intent_tokens(self) -> list[tuple[str, tuple[str, ...]]]:
        """Read workflow-action tokens from the empire intent-tokens store.

        Per Empire-directive 2026-05-13: intent vocabulary is sqlite-only.
        Delegates to intent_guard._load_intent_token_lists (sqlite-backed)
        and filters to WORKFLOW_ACTION_KINDS, preserving the old tuple
        shape callers depend on.
        """
        from .intent_guard import _load_intent_token_lists

        merged = _load_intent_token_lists()
        result: list[tuple[str, tuple[str, ...]]] = []
        for action_kind, tokens in merged.items():
            if action_kind not in self.WORKFLOW_ACTION_KINDS:
                continue
            if not isinstance(tokens, list):
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

    def _get_intent_tokens(self) -> list[tuple[str, tuple[str, ...]]]:
        if self._action_token_mapping is None:
            self._action_token_mapping = self._load_intent_tokens()
        return self._action_token_mapping

    def _classify_action_segment(self, text: str) -> str | None:
        for action_kind, tokens in self._get_intent_tokens():
            if any(token in text for token in tokens):
                return action_kind
        return None

    def _extract_rules(
        self,
        sections: dict[str, list[str]],
        names: tuple[str, ...],
    ) -> tuple[list[str], bool]:
        """Extract rules from sections. Also captures 'verify:' continuation lines."""
        for section_name in names:
            lines = sections.get(section_name)
            if lines is None:
                continue
            rules: list[str] = []
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped.startswith("-"):
                    continue
                body = stripped[1:].strip()
                if not body or body == "-":
                    continue
                # Check for verify: on the next indented line
                verify = ""
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.startswith("verify:"):
                        verify = next_line[7:].strip()
                if verify:
                    rules.append(f"{body} [verify:{verify}]")
                else:
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
        # Extract verify spec if present: "rule text [verify:spec]"
        verify_spec = ""
        verify_match = re.search(r"\[verify:(.+?)\]$", rule_text)
        if verify_match:
            verify_spec = verify_match.group(1).strip()
            rule_text = rule_text[: verify_match.start()].strip()

        normalized_rule = rule_text.strip().rstrip(".")
        match = re.match(
            r"^(?:before|after|when)\s+(.+?),\s*(.+)$",
            normalized_rule,
            flags=re.IGNORECASE,
        )
        if match is None:
            return (
                [],
                None,
                [
                    {
                        "rule": rule_text,
                        "reason": "expected format: 'Before/After <trigger>, <action>'.",
                    },
                ],
            )
        is_before = normalized_rule.lower().startswith("before")
        is_after = normalized_rule.lower().startswith("after")

        trigger_phrase = match.group(1).strip()
        action_phrase = match.group(2).strip()
        trigger = self._compile_trigger(trigger_phrase)
        if trigger is None:
            return (
                [],
                None,
                [{"rule": rule_text, "reason": f"unsupported trigger: {trigger_phrase}"}],
            )
        # Prefix with before_ for "Before" rules
        if is_before and not trigger.startswith("before_"):
            trigger = f"before_{trigger}"
        # Prefix with after_ for "After" rules on git events (to match _ACTION_KIND_TO_TRIGGERS)
        elif (
            is_after and trigger in ("git_push", "git_commit") and not trigger.startswith("after_")
        ):
            trigger = f"after_{trigger}"

        actions: list[dict[str, object]] = []
        steps: list[dict[str, object]] = []
        unsupported: list[dict[str, str]] = []
        for segment_index, action_segment in enumerate(
            self._split_action_segments(action_phrase),
            start=1,
        ):
            compiled = self._compile_action(
                rule_text,
                trigger,
                action_segment,
                rule_index,
                segment_index,
                action_map,
            )
            if compiled is None:
                continue
            if verify_spec:
                compiled["verify"] = verify_spec
            actions.append(compiled)
            steps.append(
                {
                    "index": segment_index,
                    "kind": compiled.get("kind"),
                    "source_segment": action_segment,
                    "action_ref": compiled.get("action_ref"),
                },
            )

        return (
            actions,
            {
                "id": f"workflow-rule-{rule_index:02d}-{trigger}",
                "trigger": trigger,
                "source_rule": rule_text,
                "steps": steps,
            },
            unsupported,
        )

    def _compile_trigger(self, trigger_phrase: str) -> str | None:
        text = self._normalize(trigger_phrase)
        compact = text.strip()
        if any(
            token in text
            for token in (
                "each completed task",
                "task complete",
                "task completion",
                "task completes",
                "every task",
                "each completed change",
                "completed change",
                "each change",
            )
        ) or compact in {
            "push after each completed task",
            "completed task",
            "completed change",
            "task",
            "change",
        }:
            return "task_complete"
        if any(
            token in text
            for token in ("each push", "every push", "git push", "after push", "a push", "pushing")
        ) or compact in {"push", "pushing to git"}:
            return "git_push"
        if any(
            token in text
            for token in ("each commit", "every commit", "git commit", "committing", "commiting")
        ) or compact in {"commit", "committing to git", "commiting to git"}:
            return "git_commit"
        if any(
            token in text
            for token in (
                "committing/pushing",
                "commiting/pushing",
                "commit/push",
                "commit and push",
                "committing or pushing",
            )
        ):
            return "git_commit"  # before commit covers both since commit comes first
        if any(
            token in text
            for token in (
                "github workflow success",
                "github actions success",
                "workflow success",
                "ci success",
                "ci succeeds",
                "successful ci",
            )
        ) or compact in {
            "workflow success",
            "github workflow success",
            "github actions success",
            "ci success",
        }:
            return "after_github_workflow_success"
        if any(
            token in text
            for token in (
                "deploy success",
                "deployment success",
                "successful deploy",
                "successful deployment",
            )
        ) or compact in {"deploy success", "deployment success"}:
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
            return None, {
                "rule": item,
                "reason": "expected format: '<name>: <action>' in Workflow Actions.",
            }
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
            # Fallback: treat unrecognized actions as advisory (agent guidance)
            return {
                "kind": "advisory",
                "enabled": True,
                "approval": "project_rule",
                "source_segment": action_segment.strip(),
            }
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

    def get_agent_workflow_rules(self, project_root: Path) -> list[str]:
        """Read natural language agent workflow rules for the conductor to enforce."""
        paths = self.source_paths(project_root)
        for rule_path in [paths["rules"], paths["legacy_rules"]]:
            if rule_path.is_file():
                sections = self._parse_sections(rule_path.read_text(encoding="utf-8"))
                rules, found = self._extract_rules(sections, self.AGENT_RULE_SECTION_NAMES)
                if found:
                    return rules
        return []

    def verify_action(self, project_root: Path, action: dict[str, object]) -> dict[str, object]:
        """Verify a workflow action using its verify spec. Returns {"verified": bool, "method": str, "detail": str}."""
        verify = str(action.get("verify", "")).strip()
        if not verify:
            # No verify spec — check satisfaction table (evidence-based fallback)
            action_id = str(action.get("id", ""))
            if action_id and self.is_action_satisfied(project_root, action_id):
                return {
                    "verified": True,
                    "method": "satisfaction",
                    "detail": "Marked as satisfied with evidence",
                }
            return {
                "verified": False,
                "method": "none",
                "detail": "No verification method defined. Call workflow_action_satisfy to proceed.",
            }

        # webhook("url")
        webhook_match = re.match(r'webhook\("(.+?)"\)', verify)
        if webhook_match:
            url = webhook_match.group(1)
            # #195: in-process egress is GATED. A workflow webhook may only reach an allowlisted
            # host (AIDOCS_WORKFLOW_EGRESS_ALLOWLIST, comma-separated). Fail-closed + audited —
            # this closes the ungated second-egress lane §6 swore did not exist.
            import os as _os

            from .governed_egress import EgressRefused, assert_egress_allowed

            _allow = [h.strip() for h in _os.environ.get("AIDOCS_WORKFLOW_EGRESS_ALLOWLIST", "").split(",") if h.strip()]

            def _egress_audit(_rec: dict) -> None:
                try:
                    from .execution_index_store import ExecutionIndexStore

                    ExecutionIndexStore().record_event(
                        project_root,
                        event_kind="in_process_egress",
                        source_kind="workflow_action_service",
                        capability_name="workflow_webhook",
                        action_kind="egress_" + str(_rec.get("decision", "?")),
                        target_entity=str(_rec.get("host", "?")),
                        status="allow" if _rec.get("decision") == "allow" else "refused",
                        payload={"purpose": _rec.get("purpose")},
                    )
                except Exception:
                    pass

            try:
                assert_egress_allowed(url, purpose="workflow_webhook", allow_hosts=_allow, audit=_egress_audit)
            except EgressRefused as _er:
                return {
                    "verified": False,
                    "method": "webhook",
                    "detail": f"egress refused — host not in AIDOCS_WORKFLOW_EGRESS_ALLOWLIST: {_er}",
                }
            try:
                import urllib.request

                req = urllib.request.Request(
                    url,
                    method="GET",
                    headers={"User-Agent": "AIDOCS-Workflow/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    import json as _json

                    data = _json.loads(resp.read())
                    satisfied = bool(
                        data.get("satisfied") or data.get("ok") or data.get("verified"),
                    )
                    return {
                        "verified": satisfied,
                        "method": "webhook",
                        "detail": str(data.get("message", url)),
                    }
            except Exception as exc:
                return {"verified": False, "method": "webhook", "detail": f"Webhook failed: {exc}"}

        # tool_called("tool_name", success=true)
        tool_match = re.match(r'tool_called\("(.+?)"(?:,\s*success=(\w+))?\)', verify)
        if tool_match:
            tool_name = tool_match.group(1)
            require_success = tool_match.group(2) != "false" if tool_match.group(2) else True
            try:
                from .execution_index_store import ExecutionIndexStore

                store = ExecutionIndexStore()
                events = store.query_events(project_root, limit=50)
                found = any(
                    e.get("capability_name") == tool_name
                    and (not require_success or e.get("status") == "success")
                    for e in events
                )
                return {
                    "verified": found,
                    "method": "tool_called",
                    "detail": f"{tool_name} {'found' if found else 'not found'} in recent events",
                }
            except Exception as exc:
                return {
                    "verified": False,
                    "method": "tool_called",
                    "detail": f"Check failed: {exc}",
                }

        # files_modified("pattern")
        files_match = re.match(r'files_modified\("(.+?)"\)', verify)
        if files_match:
            import fnmatch

            pattern = files_match.group(1)
            try:
                from .edit_history import EditHistoryStore

                touched = EditHistoryStore().files_touched_summary(project_root)
                found = any(fnmatch.fnmatch(f["file"], pattern) for f in touched)
                return {
                    "verified": found,
                    "method": "files_modified",
                    "detail": f"Pattern '{pattern}' {'matched' if found else 'no match'} in edit history",
                }
            except Exception as exc:
                return {
                    "verified": False,
                    "method": "files_modified",
                    "detail": f"Check failed: {exc}",
                }

        # command("cmd") — run and check exit code
        cmd_match = re.match(r'command\("(.+?)"\)', verify)
        if cmd_match:
            cmd = cmd_match.group(1)
            # AIDOCS shell provider lock — Batch B spawn flip
            # (canonical 2026-04-29). Resolve Bash-compatible
            # provider; refuse if unavailable.
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
                    source_kind="workflow_action_service.command_verify",
                    capability_name="workflow_command_verify",
                    status=("allowed" if resolved.verdict == "usable" else "observed"),
                    payload=dict(resolved.audit_payload),
                )
            except Exception:
                pass
            if resolved.verdict != "usable":
                return {
                    "verified": False,
                    "method": "command",
                    "detail": resolved.rejection_reason
                    or ("no Bash-compatible provider available"),
                }
            try:
                import subprocess

                # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
                from .shell_egress_service import audited_run

                result = audited_run(
                    [resolved.path, "-c", cmd],
                    fingerprint=("workflow_action_service.py", "verify_action", "subprocess.run"),
                    reason="workflow-command-verify",
                    run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
                    shell=False,
                    cwd=str(project_root),
                    capture_output=True,
                    timeout=30,
                    # #333 Phase 2: daemon runs console-less (pythonw); without
                    # this flag every spawn allocates a NEW visible console window.
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return {
                    "verified": result.returncode == 0,
                    "method": "command",
                    "detail": f"Exit code {result.returncode}",
                }
            except Exception as exc:
                return {"verified": False, "method": "command", "detail": f"Command failed: {exc}"}

        return {
            "verified": False,
            "method": "unknown",
            "detail": f"Unknown verify method: {verify}",
        }

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Workflow action satisfaction tracking ──

    @staticmethod
    def _satisfaction_db(project_root: Path):
        import sqlite3

        path = project_root / ".MEMORY" / ".index" / "workflow_satisfaction.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS satisfied_actions (
                action_id TEXT PRIMARY KEY,
                evidence TEXT NOT NULL,
                satisfied_at REAL NOT NULL
            )
        """)
        conn.commit()
        return conn

    def satisfy_action(self, project_root: Path, action_id: str, evidence: str) -> dict:
        """Mark a workflow action as satisfied with evidence. Resets on next workflow compile."""
        import time

        conn = self._satisfaction_db(project_root)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO satisfied_actions (action_id, evidence, satisfied_at) VALUES (?, ?, ?)",
                (action_id, evidence, time.time()),
            )
            conn.commit()
            return {"satisfied": True, "action_id": action_id, "evidence": evidence}
        finally:
            conn.close()

    def is_action_satisfied(self, project_root: Path, action_id: str) -> bool:
        """Check if a workflow action has been satisfied."""
        conn = self._satisfaction_db(project_root)
        try:
            row = conn.execute(
                "SELECT 1 FROM satisfied_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def clear_satisfaction(self, project_root: Path) -> int:
        """Clear all satisfaction records (called on recompile)."""
        conn = self._satisfaction_db(project_root)
        try:
            cursor = conn.execute("DELETE FROM satisfied_actions")
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()
