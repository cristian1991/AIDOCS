import json
import subprocess
from pathlib import Path

from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub
from aidocs_mcp.skill_store import SkillStore


REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_PATH = REPO_ROOT / "core" / "plugins" / "aidocs.js"


def _run_node_json(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")
    (root.parent / "index.aidocs").write_text("# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8")
    (root.parent / "global-instructions.aidocs").write_text("# Global Instructions\n", encoding="utf-8")
    (root.parent / "coding-standards.aidocs").write_text("# Coding Standards\n", encoding="utf-8")
    (root.parent / "memory-system.aidocs").write_text("# Memory System\n", encoding="utf-8")
    (root.parent / "research-safety.aidocs").write_text("# Research Safety\n", encoding="utf-8")
    (root.parent / "personalities").mkdir(parents=True, exist_ok=True)
    (root.parent / "personalities" / "default.aidocs").write_text("# Default Personality\n", encoding="utf-8")
    memory_template = root / "memory"
    memory_template.mkdir(parents=True, exist_ok=True)
    (memory_template / "INDEX.md").write_text("# Memory Index\n", encoding="utf-8")
    (memory_template / "rules").mkdir(parents=True, exist_ok=True)
    (memory_template / "rules" / "workflow-rules.md").write_text("# Workflow Rules\n\n## Workflow Rules\n", encoding="utf-8")
    (memory_template / "rules" / "workflow-actions.md").write_text("# Workflow Actions\n\n## Workflow Actions\n", encoding="utf-8")


def _make_runtime(tmp_path: Path) -> RuntimeService:
    templates = tmp_path / "templates"
    _write_templates(templates)
    return RuntimeService(AidocsServiceHub(templates_root=templates))


def _write_provider_root(provider_root: Path) -> None:
    provider_root.mkdir(parents=True, exist_ok=True)
    (provider_root / "provider.json").write_text(
        '{"provider_id": "superpowers_external", "version": "5.1.0"}\n',
        encoding="utf-8",
    )
    skill_root = provider_root / "skills" / "brainstorming"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: brainstorming\n"
        "description: Imported brainstorming skill.\n"
        "tags: external, provider\n"
        "---\n",
        encoding="utf-8",
    )


def test_skill_provider_registry_moves_under_memory_config(tmp_path: Path) -> None:
    store = SkillStore()
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    provider_root = tmp_path / "provider-root"
    _write_provider_root(provider_root)
    legacy_path = project_root / ".MEMORY" / "skill-providers.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "superpowers_external",
                        "root_path": str(provider_root),
                        "version": "5.1.0",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    providers = store.list_external_providers(project_root)

    normalized_path = project_root / ".MEMORY" / "config" / "skill-providers.json"
    assert store.external_provider_registry_path(project_root) == normalized_path
    assert [item.provider_id for item in providers] == ["superpowers_external"]
    assert normalized_path.is_file()
    assert json.loads(normalized_path.read_text(encoding="utf-8"))["providers"][0]["provider_id"] == "superpowers_external"
    assert not legacy_path.exists()


def test_opencode_plugin_uses_normalized_provider_registry_not_legacy_root_copy(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project-plugin"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")
    session = runtime.hub.sessions.create_session(project_root, "2026-03-31-plugin", "A", "Agent", "Goal A")
    runtime.hub.managed_mode.set_mode(project_root, session.session_id)
    provider_root = tmp_path / "plugin-provider"
    _write_provider_root(provider_root)
    runtime.hub.skills.register_external_provider(
        project_root,
        provider_name="superpowers_external",
        path=str(provider_root),
    )
    runtime.hub.skills.set_selected_skills(project_root, session.session_id, ["superpowers_external/brainstorming"])
    runtime.project_bootstrap_or_resume(project_root, session_id=session.session_id, include_code_bundle=False, include_tests=False)

    legacy_path = project_root / ".MEMORY" / "skill-providers.json"
    legacy_path.write_text(json.dumps({"providers": []}, indent=2) + "\n", encoding="utf-8")

    script = f"""
const plugin = require({json.dumps(str(PLUGIN_PATH))});
(async () => {{
  const state = await plugin._internal.resolveAidocsState({json.dumps(str(project_root))});
  console.log(JSON.stringify(state));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""

    result = _run_node_json(script)

    assert result["importedSkillState"]["active_skills"] == ["superpowers_external/brainstorming"]
    assert result["importedSkillState"]["provider_states"]["superpowers_external"] == "compatible"


def test_aidocs_managed_is_classified_as_runtime_binding_state(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project-managed"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")

    result = runtime.project_bootstrap_or_resume(project_root, include_code_bundle=False)

    artifact = result["project_overview"]["artifact_catalog"]["aidocs_managed"]
    assert artifact["path"] == str(project_root / ".MEMORY" / "config" / "aidocs-managed.json")
    assert artifact["classification"] == "runtime_binding_state"


def test_workflow_actions_json_is_treated_as_compiled_runtime_artifact(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    project_root = tmp_path / "project-workflow"
    runtime.project_init(project_root, init_git=False, create_remote=False)
    (project_root / "AGENTS.md").write_text("routing\n", encoding="utf-8")

    result = runtime.project_bootstrap_or_resume(project_root, include_code_bundle=False)

    artifact = result["project_overview"]["artifact_catalog"]["workflow_actions"]
    assert artifact["path"] == str(project_root / ".MEMORY" / "config" / "workflow-actions.json")
    assert artifact["classification"] == "compiled_runtime_artifact"
