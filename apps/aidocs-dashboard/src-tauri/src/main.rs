#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Serialize)]
struct TomlDocument {
    path: String,
    label: String,
    category: String,
    scope: String,
    target: String,
    active: String,
    language_context: String,
    editable: bool,
    content: String,
}

#[derive(Serialize)]
struct ManagedProjectOption {
    title: String,
    project_root: String,
    session_count: usize,
    managed_session_id: Option<String>,
    current: bool,
}

#[derive(Deserialize)]
struct GlobalProjectRegistryPayload {
    projects: Vec<GlobalProjectRegistryEntry>,
}

#[derive(Deserialize)]
struct GlobalProjectRegistryEntry {
    project_root: String,
    title: Option<String>,
}

fn global_registry_path() -> Option<PathBuf> {
    if let Some(appdata) = env::var_os("APPDATA") {
        return Some(
            PathBuf::from(appdata)
                .join("AIDOCS")
                .join("project-registry.json"),
        );
    }
    if let Some(config_home) = env::var_os("XDG_CONFIG_HOME") {
        return Some(
            PathBuf::from(config_home)
                .join("aidocs")
                .join("project-registry.json"),
        );
    }
    env::var_os("HOME").map(|home| {
        PathBuf::from(home)
            .join(".config")
            .join("aidocs")
            .join("project-registry.json")
    })
}

fn is_aidocs_project(candidate: &Path) -> bool {
    // Only require .MEMORY/ — aidocs.toml is optional (only the AIDOCS source project has it)
    candidate.join(".MEMORY").is_dir()
}

fn count_sessions(project_root: &Path) -> usize {
    let sessions_dir = project_root.join(".MEMORY").join("sessions");
    fs::read_dir(sessions_dir)
        .ok()
        .into_iter()
        .flat_map(|entries| entries.filter_map(Result::ok))
        .filter(|entry| entry.path().is_dir())
        .count()
}

fn managed_session_id(project_root: &Path) -> Option<String> {
    let path = project_root
        .join(".MEMORY")
        .join("config")
        .join("aidocs-managed.json");
    let text = fs::read_to_string(path).ok()?;
    let payload = serde_json::from_str::<Value>(&text).ok()?;
    if !payload
        .get("active")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return None;
    }
    payload
        .get("session_id")
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn register_project_candidate(
    candidate: &Path,
    title_hint: Option<&str>,
    current_root: &Path,
    seen: &mut BTreeSet<String>,
    projects: &mut Vec<ManagedProjectOption>,
) {
    if !is_aidocs_project(candidate) {
        return;
    }

    let canonical = fs::canonicalize(candidate).unwrap_or_else(|_| candidate.to_path_buf());
    let key = canonical
        .to_string_lossy()
        .strip_prefix(r"\\?\")
        .unwrap_or(&canonical.to_string_lossy())
        .to_string();
    if !seen.insert(key.clone()) {
        return;
    }

    let raw_title = title_hint
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .or_else(|| {
            canonical
                .file_name()
                .and_then(|name| name.to_str())
                .map(str::to_string)
        })
        .unwrap_or_else(|| "AIDOCS Project".to_string());
    let title = raw_title.to_uppercase().replace('-', " ").replace('_', " ");

    let current_canonical =
        fs::canonicalize(current_root).unwrap_or_else(|_| current_root.to_path_buf());

    projects.push(ManagedProjectOption {
        title,
        project_root: key,
        session_count: count_sessions(&canonical),
        managed_session_id: managed_session_id(&canonical),
        current: canonical == current_canonical,
    });
}

fn discover_managed_projects(current_root: &Path) -> Vec<ManagedProjectOption> {
    let mut seen = BTreeSet::new();
    let mut projects = Vec::new();

    register_project_candidate(current_root, None, current_root, &mut seen, &mut projects);

    if let Some(path) = global_registry_path() {
        if let Ok(text) = fs::read_to_string(path) {
            if let Ok(payload) = serde_json::from_str::<GlobalProjectRegistryPayload>(&text) {
                for entry in payload.projects {
                    register_project_candidate(
                        Path::new(&entry.project_root),
                        entry.title.as_deref(),
                        current_root,
                        &mut seen,
                        &mut projects,
                    );
                }
            }
        }
    }

    projects.sort_by(|left, right| {
        right
            .current
            .cmp(&left.current)
            .then_with(|| {
                right
                    .managed_session_id
                    .is_some()
                    .cmp(&left.managed_session_id.is_some())
            })
            .then_with(|| left.title.to_lowercase().cmp(&right.title.to_lowercase()))
    });
    projects
}
fn resolve_project_root(project_root: Option<String>) -> Result<PathBuf, String> {
    if let Some(path) = project_root {
        let candidate = PathBuf::from(&path);
        if candidate.exists() {
            return Ok(candidate);
        }
    }

    // Check AIDOCS_PATH env var
    if let Some(aidocs_path) = env::var_os("AIDOCS_PATH") {
        let candidate = PathBuf::from(aidocs_path);
        if candidate.join(".MEMORY").is_dir() {
            return Ok(candidate);
        }
    }

    // Walk up from cwd
    let mut current = std::env::current_dir().map_err(|err| err.to_string())?;
    loop {
        if current.join(".MEMORY").is_dir() {
            return Ok(current);
        }
        if !current.pop() {
            break;
        }
    }
    Err("Could not resolve the AIDOCS project root for the dashboard.".into())
}

fn run_json_cli(project_root: &Path, args: &[String]) -> Result<Value, String> {
    let command_sets = vec![
        ("aidocs", args.to_vec()),
        ("python", {
            let mut items = vec!["-m".to_string(), "aidocs_mcp.cli".to_string()];
            items.extend(args.to_vec());
            items
        }),
        ("py", {
            let mut items = vec!["-m".to_string(), "aidocs_mcp.cli".to_string()];
            items.extend(args.to_vec());
            items
        }),
    ];

    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in command_sets {
        let mut cmd = Command::new(program);
        cmd.args(&program_args).current_dir(project_root);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let output = match cmd.output() {
            Ok(output) => output,
            Err(err) => {
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let stdout_preview = String::from_utf8_lossy(&output.stdout);
            let stdout_short = stdout_preview.trim().chars().take(200).collect::<String>();
            let code = output.status.code().map(|c| c.to_string()).unwrap_or_else(|| "?".into());
            errors.push(format!("{} (exit {}): {} {}", program, code, if stderr.is_empty() { "(no stderr)" } else { &stderr }, stdout_short));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        return serde_json::from_str::<Value>(&stdout)
            .map_err(|err| format!("Failed to parse dashboard JSON from {}: {}", program, err));
    }
    Err(format!(
        "Dashboard bridge could not run the local AIDOCS CLI. {}",
        errors.join(" | ")
    ))
}

fn run_python_registry_search(
    query: &str,
    limit: usize,
    cursor: Option<&str>,
) -> Result<Value, String> {
    let script = r#"
import json, sys
from aidocs_mcp.mcp_registry import search_servers
query = sys.argv[1]
limit = int(sys.argv[2])
cursor = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
result = search_servers(query=query, limit=limit, cursor=cursor)
print(json.dumps({
    'ok': True,
    'servers': [{**server.to_dict(), 'install_commands': server.install_commands()} for server in result.servers],
    'next_cursor': result.next_cursor,
}))
"#;
    let command_sets = vec![
        (
            "python",
            vec![
                "-c".to_string(),
                script.to_string(),
                query.to_string(),
                limit.to_string(),
                cursor.unwrap_or("").to_string(),
            ],
        ),
        (
            "py",
            vec![
                "-c".to_string(),
                script.to_string(),
                query.to_string(),
                limit.to_string(),
                cursor.unwrap_or("").to_string(),
            ],
        ),
    ];
    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in command_sets {
        let output = Command::new(program).args(&program_args).output();
        let output = match output {
            Ok(output) => output,
            Err(err) => {
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            errors.push(format!("{}: {}", program, stderr));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        return serde_json::from_str::<Value>(&stdout)
            .map_err(|err| format!("Failed to parse registry JSON from {}: {}", program, err));
    }
    Err(format!(
        "Registry search bridge could not run Python. {}",
        errors.join(" | ")
    ))
}

fn run_python_metrics_snapshot() -> Result<Value, String> {
    let script = r#"
import json
from aidocs_mcp.metrics import get_collector
print(json.dumps({"ok": True, "snapshot": get_collector().snapshot()}))
"#;
    let command_sets = vec![
        ("python", vec!["-c".to_string(), script.to_string()]),
        ("py", vec!["-c".to_string(), script.to_string()]),
    ];
    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in command_sets {
        let output = Command::new(program).args(&program_args).output();
        let output = match output {
            Ok(output) => output,
            Err(err) => {
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            errors.push(format!("{}: {}", program, stderr));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        return serde_json::from_str::<Value>(&stdout)
            .map_err(|err| format!("Failed to parse metrics JSON from {}: {}", program, err));
    }
    Err(format!(
        "Metrics snapshot bridge could not run Python. {}",
        errors.join(" | ")
    ))
}

fn run_python_skill_scan(project_root: &Path, session_id: Option<&str>) -> Result<Value, String> {
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.skill_store import SkillStore
from aidocs_mcp.skill_scanner import scan_skill
root = Path(sys.argv[1])
session_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
store = SkillStore()
selected = store.get_selected_skills(root, session_id) if session_id else {"selected_skills": []}
selected_ids = set(selected.get("selected_skills", []))
results = []
for skill in store.list_skills(root):
    content = str(skill.get("content") or "")
    result = scan_skill(str(skill.get("skill_id") or skill.get("name") or "unknown"), content)
    results.append({
        "skill": skill,
        "selected": str(skill.get("skill_id") or "") in selected_ids,
        "scan": result.summary(),
    })
print(json.dumps({"ok": True, "results": results}))
"#;
    let root = project_root.to_string_lossy().to_string();
    let sid = session_id.unwrap_or("").to_string();
    let command_sets = vec![
        (
            "python",
            vec![
                "-c".to_string(),
                script.to_string(),
                root.clone(),
                sid.clone(),
            ],
        ),
        ("py", vec!["-c".to_string(), script.to_string(), root, sid]),
    ];
    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in command_sets {
        let output = Command::new(program).args(&program_args).output();
        let output = match output {
            Ok(output) => output,
            Err(err) => {
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            errors.push(format!("{}: {}", program, stderr));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        return serde_json::from_str::<Value>(&stdout)
            .map_err(|err| format!("Failed to parse skill scan JSON from {}: {}", program, err));
    }
    Err(format!(
        "Skill scan bridge could not run Python. {}",
        errors.join(" | ")
    ))
}

fn run_python_context_budget(
    project_root: &Path,
    session_id: Option<&str>,
    compact: bool,
) -> Result<Value, String> {
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.context_budget import context_budget_check, context_compact
root = Path(sys.argv[1])
session_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
compact = sys.argv[3].lower() == 'true'
result = context_compact(root, session_id) if compact else context_budget_check(root, session_id)
print(json.dumps({"ok": True, "result": result}))
"#;
    let root = project_root.to_string_lossy().to_string();
    let sid = session_id.unwrap_or("").to_string();
    let compact_flag = if compact { "true" } else { "false" }.to_string();
    let command_sets = vec![
        (
            "python",
            vec![
                "-c".to_string(),
                script.to_string(),
                root.clone(),
                sid.clone(),
                compact_flag.clone(),
            ],
        ),
        (
            "py",
            vec![
                "-c".to_string(),
                script.to_string(),
                root,
                sid,
                compact_flag,
            ],
        ),
    ];
    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in command_sets {
        let output = Command::new(program).args(&program_args).output();
        let output = match output {
            Ok(output) => output,
            Err(err) => {
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            errors.push(format!("{}: {}", program, stderr));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        return serde_json::from_str::<Value>(&stdout).map_err(|err| {
            format!(
                "Failed to parse context budget JSON from {}: {}",
                program, err
            )
        });
    }
    Err(format!(
        "Context budget bridge could not run Python. {}",
        errors.join(" | ")
    ))
}


fn allowed_toml_paths(
    project_root: &Path,
    session_id: Option<&str>,
) -> Result<Vec<PathBuf>, String> {
    let mut paths: Vec<PathBuf> = Vec::new();

    // Global/user config
    let global_config = if cfg!(windows) {
        std::env::var("APPDATA")
            .map(|appdata| PathBuf::from(appdata).join("aidocs").join("aidocs.toml"))
            .ok()
    } else {
        std::env::var("XDG_CONFIG_HOME")
            .map(|xdg| PathBuf::from(xdg).join("aidocs").join("aidocs.toml"))
            .ok()
            .or_else(|| {
                std::env::var("HOME").ok().map(|home| {
                    PathBuf::from(home)
                        .join(".config")
                        .join("aidocs")
                        .join("aidocs.toml")
                })
            })
    };
    if let Some(ref global_path) = global_config {
        if global_path.is_file() {
            paths.push(global_path.clone());
        }
    }

    // Project config
    let project_config = project_root.join("aidocs.toml");
    if project_config.is_file() {
        paths.push(project_config);
    }

    for relative_dir in [
        PathBuf::from("action_hooks"),
        PathBuf::from("action_tokens"),
        PathBuf::from("mcp/server/aidocs_mcp/index_languages"),
    ] {
        let dir = project_root.join(relative_dir);
        if !dir.is_dir() {
            continue;
        }
        let entries = fs::read_dir(&dir)
            .map_err(|err| format!("Could not read TOML directory {}: {}", dir.display(), err))?;
        for entry in entries {
            let entry = entry.map_err(|err| err.to_string())?;
            let path = entry.path();
            if path
                .extension()
                .and_then(|value| value.to_str())
                .is_some_and(|value| value.eq_ignore_ascii_case("toml"))
            {
                paths.push(path);
            }
        }
    }

    if let Some(session_id) = session_id.map(str::trim).filter(|value| !value.is_empty()) {
        let session_config = project_root
            .join(".MEMORY")
            .join("sessions")
            .join(session_id)
            .join("aidocs.toml");
        if session_config.is_file() {
            paths.push(session_config);
        }
    }

    paths.sort();
    Ok(paths)
}

fn toml_label(relative_path: &str) -> String {
    match relative_path {
        "~global/aidocs.toml" => "Global config".into(),
        "aidocs.toml" => "Project config".into(),
        value if value.starts_with(".MEMORY/sessions/") && value.ends_with("/aidocs.toml") => {
            "Session config".into()
        }
        value if value.starts_with("action_tokens/") => format!(
            "Action tokens: {}",
            Path::new(value)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(value)
        ),
        value if value.starts_with("action_hooks/") => format!(
            "Action hook: {}",
            Path::new(value)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(value)
        ),
        value if value.starts_with("mcp/server/aidocs_mcp/index_languages/") => format!(
            "Language descriptor: {}",
            Path::new(value)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(value)
        ),
        value if value.contains("/.MEMORY/") || value.starts_with(".MEMORY/") => format!(
            "Session config: {}",
            Path::new(value)
                .parent()
                .and_then(|parent| parent.file_name())
                .and_then(|name| name.to_str())
                .unwrap_or(value)
        ),
        value => value.into(),
    }
}

fn toml_category(relative_path: &str) -> String {
    if relative_path == "aidocs.toml" {
        return "Project config".into();
    }
    if relative_path.starts_with("action_tokens/") {
        return "Intent tokens".into();
    }
    if relative_path.starts_with("action_hooks/") {
        return "Workflow hooks".into();
    }
    if relative_path.starts_with("mcp/server/aidocs_mcp/index_languages/") {
        return "Index descriptors".into();
    }
    if relative_path.starts_with(".MEMORY/sessions/") {
        return "Session overrides".into();
    }
    "TOML".into()
}

fn project_enabled_languages(project_root: &Path) -> Option<String> {
    let content = fs::read_to_string(project_root.join("aidocs.toml")).ok()?;
    let value = toml::from_str::<toml::Value>(&content).ok()?;
    value
        .get("languages")
        .and_then(|item| item.get("enabled"))
        .and_then(toml::Value::as_str)
        .map(str::to_string)
}

fn toml_scope(relative_path: &str) -> String {
    if relative_path == "~global/aidocs.toml" {
        return "Global".into();
    }
    if relative_path == "aidocs.toml" {
        return "Project".into();
    }
    if relative_path.starts_with(".MEMORY/sessions/") {
        return "Session".into();
    }
    if relative_path.starts_with("action_tokens/") {
        return "Tokens".into();
    }
    if relative_path.starts_with("action_hooks/") {
        return "Hooks".into();
    }
    if relative_path.ends_with("_index_config.toml") {
        return "Shared index".into();
    }
    if relative_path.starts_with("mcp/server/aidocs_mcp/index_languages/") {
        return "Language".into();
    }
    "TOML".into()
}

fn toml_target(relative_path: &str) -> String {
    if relative_path == "aidocs.toml" {
        return "Current project".into();
    }
    if relative_path.starts_with(".MEMORY/sessions/") {
        return Path::new(relative_path)
            .parent()
            .and_then(|parent| parent.file_name())
            .and_then(|name| name.to_str())
            .unwrap_or("Session")
            .to_string();
    }
    Path::new(relative_path)
        .file_stem()
        .and_then(|name| name.to_str())
        .unwrap_or(relative_path)
        .to_string()
}

fn descriptor_enabled(descriptor_name: &str, enabled_languages: Option<&str>) -> String {
    let Some(enabled_languages) = enabled_languages else {
        return "Unknown".into();
    };
    let trimmed = enabled_languages.trim();
    if trimmed.eq_ignore_ascii_case("all") {
        return "Enabled".into();
    }
    let matched = trimmed
        .split(',')
        .map(str::trim)
        .any(|item| item.eq_ignore_ascii_case(descriptor_name));
    if matched {
        "Enabled".into()
    } else {
        "Not selected".into()
    }
}

fn toml_active_status(
    relative_path: &str,
    target: &str,
    enabled_languages: Option<&str>,
) -> String {
    if relative_path == "aidocs.toml" {
        return "Active".into();
    }
    if relative_path.starts_with(".MEMORY/sessions/") {
        return format!("Session {target}");
    }
    if relative_path.starts_with("action_tokens/") {
        return descriptor_enabled(target, enabled_languages);
    }
    if relative_path.starts_with("action_hooks/") {
        return "Loaded".into();
    }
    if relative_path.ends_with("_index_config.toml") {
        return "Shared".into();
    }
    if relative_path.starts_with("mcp/server/aidocs_mcp/index_languages/") {
        return descriptor_enabled(target, enabled_languages);
    }
    "Available".into()
}

fn toml_language_context(
    relative_path: &str,
    content: &str,
    enabled_languages: Option<&str>,
) -> String {
    if relative_path == "aidocs.toml" {
        return enabled_languages
            .map(|value| format!("Connected languages: {value}"))
            .unwrap_or_else(|| "Connected languages: default".into());
    }

    if relative_path.starts_with(".MEMORY/sessions/") {
        let parsed = toml::from_str::<toml::Value>(content).ok();
        if let Some(value) = parsed
            .as_ref()
            .and_then(|item| item.get("languages"))
            .and_then(|item| item.get("enabled"))
            .and_then(toml::Value::as_str)
        {
            return format!("Override languages: {value}");
        }
        return "Inherits project language set".into();
    }

    if relative_path.ends_with("_index_config.toml") {
        return "Shared indexing defaults".into();
    }

    if relative_path.starts_with("mcp/server/aidocs_mcp/index_languages/") {
        let parsed = toml::from_str::<toml::Value>(content).ok();
        let name = parsed
            .as_ref()
            .and_then(|item| item.get("name"))
            .and_then(toml::Value::as_str)
            .unwrap_or("descriptor");
        let extensions = parsed
            .as_ref()
            .and_then(|item| item.get("extensions"))
            .and_then(toml::Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(toml::Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "no extensions listed".into());
        let connected = enabled_languages.unwrap_or("default");
        return format!("{name} · {extensions} · project set {connected}");
    }

    if relative_path.starts_with("action_tokens/") {
        let connected = enabled_languages.unwrap_or("default");
        return format!("Intent classification tokens · project set {connected}");
    }
    if relative_path.starts_with("action_hooks/") {
        return "Workflow and interaction hooks".into();
    }

    "TOML document".into()
}

fn load_toml_documents_for_session(
    project_root: &Path,
    session_id: Option<&str>,
) -> Result<Vec<TomlDocument>, String> {
    let paths = allowed_toml_paths(project_root, session_id)?;
    let mut documents: Vec<TomlDocument> = Vec::new();
    let enabled_languages = project_enabled_languages(project_root);

    for path in paths {
        let relative = path
            .strip_prefix(project_root)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| {
                // Global config — use a synthetic relative path
                if path.ends_with("aidocs.toml") && !path.starts_with(project_root) {
                    "~global/aidocs.toml".into()
                } else {
                    path.to_string_lossy().replace('\\', "/")
                }
            });
        let content = fs::read_to_string(&path)
            .map_err(|err| format!("Could not read {}: {}", path.display(), err))?;
        let target = toml_target(&relative);
        documents.push(TomlDocument {
            label: toml_label(&relative),
            category: toml_category(&relative),
            scope: toml_scope(&relative),
            target: target.clone(),
            active: toml_active_status(&relative, &target, enabled_languages.as_deref()),
            language_context: toml_language_context(
                &relative,
                &content,
                enabled_languages.as_deref(),
            ),
            path: relative,
            editable: true,
            content,
        });
    }

    Ok(documents)
}

fn resolve_allowed_toml_path(
    project_root: &Path,
    relative_path: &str,
    session_id: Option<&str>,
) -> Result<PathBuf, String> {
    let requested = relative_path.trim().replace('\\', "/");
    if requested.is_empty() {
        return Err("TOML path is required.".into());
    }

    let allowed = allowed_toml_paths(project_root, session_id)?;
    for path in allowed {
        let relative = path
            .strip_prefix(project_root)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| "~global/aidocs.toml".into());
        if relative == requested {
            return Ok(path);
        }
    }

    Err(format!(
        "TOML path is not part of the dashboard control surface: {}",
        requested
    ))
}

#[tauri::command]
fn load_dashboard(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
    ];
    if let Some(session_id) = session_id {
        if !session_id.trim().is_empty() {
            args.push("--session".to_string());
            args.push(session_id);
        }
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
fn list_managed_projects(project_root: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    Ok(serde_json::json!({
        "ok": true,
        "projects": discover_managed_projects(&root),
    }))
}

#[tauri::command]
fn save_config_setting(
    project_root: Option<String>,
    setting_path: String,
    value: Value,
    scope: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-set-config".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--setting".to_string(),
        setting_path,
        "--value".to_string(),
        serde_json::to_string(&value).unwrap_or_else(|_| value.to_string()),
    ];
    if let Some(s) = scope {
        if !s.trim().is_empty() {
            args.push("--scope".to_string());
            args.push(s);
        }
    }
    if let Some(sid) = session_id {
        if !sid.trim().is_empty() {
            args.push("--session".to_string());
            args.push(sid);
        }
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
fn load_toml_documents(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let documents = load_toml_documents_for_session(&root, session_id.as_deref())?;
    Ok(serde_json::json!({
        "ok": true,
        "documents": documents,
    }))
}

#[tauri::command]
fn save_toml_document(
    project_root: Option<String>,
    session_id: Option<String>,
    relative_path: String,
    content: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let target = resolve_allowed_toml_path(&root, &relative_path, session_id.as_deref())?;

    toml::from_str::<toml::Value>(&content)
        .map_err(|err| format!("TOML validation failed for {}: {}", relative_path, err))?;
    fs::write(&target, content)
        .map_err(|err| format!("Could not write {}: {}", target.display(), err))?;

    let documents = load_toml_documents_for_session(&root, session_id.as_deref())?;
    Ok(serde_json::json!({
        "ok": true,
        "message": format!("Saved {}", relative_path),
        "documents": documents,
    }))
}

#[tauri::command]
fn skill_scan_results(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    run_python_skill_scan(&root, session_id.as_deref())
}

#[tauri::command]
fn context_budget_check(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    run_python_context_budget(&root, session_id.as_deref(), false)
}

#[tauri::command]
fn context_compact(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    run_python_context_budget(&root, session_id.as_deref(), true)
}

#[tauri::command]
fn toggle_managed_mode(
    project_root: Option<String>,
    session_id: Option<String>,
    enable: bool,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    if enable {
        let sid = session_id.unwrap_or_default();
        if sid.trim().is_empty() {
            return Err("Session ID is required to enable managed mode.".into());
        }
        let args = vec![
            "managed-mode-set".to_string(),
            root.to_string_lossy().to_string(),
            "--json".to_string(),
            "--session".to_string(),
            sid,
        ];
        run_json_cli(&root, &args)
    } else {
        let args = vec![
            "managed-mode-clear".to_string(),
            root.to_string_lossy().to_string(),
            "--json".to_string(),
        ];
        run_json_cli(&root, &args)
    }
}

#[tauri::command]
fn metrics_snapshot() -> Result<Value, String> {
    run_python_metrics_snapshot()
}

use std::sync::Mutex;

struct ConductorState {
    process: Option<std::process::Child>,
    output: Vec<(f64, String, String)>,
}

static CONDUCTOR: std::sync::LazyLock<Mutex<ConductorState>> =
    std::sync::LazyLock::new(|| Mutex::new(ConductorState { process: None, output: Vec::new() }));

#[tauri::command]
fn conductor_start(project_root: Option<String>, backend: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let backend_str = backend.unwrap_or_else(|| "claude".into());
    let cli_name = match backend_str.as_str() {
        "claude" => "claude",
        "codex" => "codex",
        other => return Err(format!("Unknown backend: {other}")),
    };
    let cli = which(cli_name).map_err(|_| format!("{cli_name} CLI not found"))?;

    let mut state = CONDUCTOR.lock().map_err(|e| e.to_string())?;
    if let Some(ref mut proc) = state.process {
        let _ = proc.kill();
        let _ = proc.wait();
    }
    state.output.clear();

    // Interactive mode — stays alive between tasks
    let child = std::process::Command::new(cli)
        .args(["--output-format", "text"])
        .current_dir(&root)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn conductor: {e}"))?;

    state.process = Some(child);
    Ok(serde_json::json!({
        "started": true,
        "backend": backend_str,
        "project_root": root.to_string_lossy(),
    }))
}

#[tauri::command]
fn conductor_send(message: String) -> Result<Value, String> {
    let mut state = CONDUCTOR.lock().map_err(|e| e.to_string())?;
    let proc = state.process.as_mut().ok_or("No conductor running")?;
    if proc.try_wait().map_err(|e| e.to_string())?.is_some() {
        state.process = None;
        return Err("Conductor process has exited".into());
    }
    if let Some(ref mut stdin) = proc.stdin {
        use std::io::Write;
        writeln!(stdin, "{message}").map_err(|e| format!("Failed to send: {e}"))?;
        stdin.flush().map_err(|e| format!("Failed to flush: {e}"))?;
    }
    Ok(serde_json::json!({ "sent": true }))
}

#[tauri::command]
fn conductor_status() -> Result<Value, String> {
    let state = CONDUCTOR.lock().map_err(|e| e.to_string())?;
    let running = state.process.as_ref().is_some();
    Ok(serde_json::json!({ "running": running }))
}

#[tauri::command]
fn conductor_stop() -> Result<Value, String> {
    let mut state = CONDUCTOR.lock().map_err(|e| e.to_string())?;
    if let Some(ref mut proc) = state.process {
        let _ = proc.kill();
        let _ = proc.wait();
    }
    state.process = None;
    Ok(serde_json::json!({ "stopped": true }))
}

#[tauri::command]
fn conductor_output(since: Option<f64>) -> Result<Value, String> {
    let state = CONDUCTOR.lock().map_err(|e| e.to_string())?;
    let since_ts = since.unwrap_or(0.0);
    let lines: Vec<Value> = state.output.iter()
        .filter(|(ts, _, _)| *ts > since_ts)
        .map(|(ts, stream, text)| serde_json::json!({
            "timestamp": ts,
            "stream": stream,
            "text": text,
        }))
        .collect();
    let running = state.process.is_some();
    Ok(serde_json::json!({
        "running": running,
        "lines": lines,
        "total_buffered": state.output.len(),
    }))
}

fn which(name: &str) -> Result<PathBuf, String> {
    let cmd = if cfg!(windows) { "where" } else { "which" };

    let output = std::process::Command::new(cmd)
        .arg(name)
        .output()
        .map_err(|e| e.to_string())?;
    if output.status.success() {
        let path = String::from_utf8_lossy(&output.stdout);
        let first_line = path.lines().next().unwrap_or("").trim();
        if !first_line.is_empty() {
            return Ok(PathBuf::from(first_line));
        }
    }
    Err(format!("{name} not found"))
}

#[tauri::command]
fn mcp_registry_search(
    query: String,
    limit: Option<usize>,
    cursor: Option<String>,
) -> Result<Value, String> {
    run_python_registry_search(query.trim(), limit.unwrap_or(20), cursor.as_deref())
}

#[tauri::command]
fn execution_clear_tokens(project_root: Option<String>, session_id: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let sid = session_id.unwrap_or_default();
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.execution_index_store import ExecutionIndexStore
root = Path(sys.argv[1])
sid = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
store = ExecutionIndexStore()
count = store.clear_token_usage(root, session_id=sid)
print(json.dumps({"ok": True, "cleared": "tokens", "runs_deleted": count}))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &sid])
}

#[tauri::command]
fn execution_clear_tool_calls(project_root: Option<String>, session_id: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let sid = session_id.unwrap_or_default();
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.execution_index_store import ExecutionIndexStore
root = Path(sys.argv[1])
sid = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
store = ExecutionIndexStore()
result = store.clear_tool_calls(root, session_id=sid)
print(json.dumps({"ok": True, "cleared": "tool_calls", **result}))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &sid])
}

#[tauri::command]
fn execution_prune_events(project_root: Option<String>, keep_days: Option<i32>, max_events: Option<i32>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let days = keep_days.unwrap_or(7);
    let max_ev = max_events.unwrap_or(0);
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.execution_index_store import ExecutionIndexStore
root = Path(sys.argv[1])
keep_days = int(sys.argv[2])
max_events = int(sys.argv[3])
store = ExecutionIndexStore()
result = {}
if keep_days > 0:
    result['by_age'] = store.prune_old_events(root, keep_days=keep_days)
if max_events > 0:
    result['by_size'] = store.prune_to_max_size(root, max_events=max_events)
if not result:
    result = store.auto_prune(root)
result['counts'] = store.event_count(root)
print(json.dumps({"ok": True, **result}))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &days.to_string(), &max_ev.to_string()])
}

#[tauri::command]
fn execution_usage_by_identity(project_root: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.execution_index_store import ExecutionIndexStore
root = Path(sys.argv[1])
store = ExecutionIndexStore()
print(json.dumps({"ok": True, "by_host": store.usage_by_host(root), "by_agent": store.usage_by_agent(root), "counts": store.event_count(root)}))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy()])
}

fn run_python_with_args(_project_root: &Path, script: &str, args: &[&str]) -> Result<Value, String> {
    for program in ["python", "py"] {
        let mut cmd_args: Vec<String> = vec!["-c".to_string(), script.to_string()];
        cmd_args.extend(args.iter().map(|a| a.to_string()));
        let output = Command::new(program)
            .args(&cmd_args)
            .output();
        match output {
            Ok(output) if output.status.success() => {
                let stdout = String::from_utf8_lossy(&output.stdout);
                return serde_json::from_str::<Value>(&stdout)
                    .map_err(|e| format!("JSON parse error: {e}"));
            }
            Ok(_) => continue,
            Err(_) => continue,
        }
    }
    Err("Python not available".into())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            list_managed_projects,
            load_dashboard,
            save_config_setting,
            load_toml_documents,
            save_toml_document,
            toggle_managed_mode,
            mcp_registry_search,
            metrics_snapshot,
            skill_scan_results,
            context_budget_check,
            context_compact,
            execution_clear_tokens,
            execution_clear_tool_calls,
            execution_prune_events,
            execution_usage_by_identity,
            conductor_start,
            conductor_send,
            conductor_status,
            conductor_stop,
            conductor_output,
            open_url,
            select_folder,
            setup_detect,
            setup_install,
            setup_configure,
            setup_install_python
        ])
        .run(tauri::generate_context!())
        .expect("error while running AIDOCS Dashboard");
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    { std::process::Command::new("cmd").args(["/c", "start", &url]).spawn().map_err(|e| e.to_string())?; }
    #[cfg(target_os = "macos")]
    { std::process::Command::new("open").arg(&url).spawn().map_err(|e| e.to_string())?; }
    #[cfg(target_os = "linux")]
    { std::process::Command::new("xdg-open").arg(&url).spawn().map_err(|e| e.to_string())?; }
    Ok(())
}

// ── Setup Wizard ──

fn find_python_path() -> Option<String> {
    for candidate in &["python", "python3", "py"] {
        if let Ok(output) = Command::new(candidate).args(["--version"]).output() {
            if output.status.success() {
                let ver = String::from_utf8_lossy(&output.stdout);
                if let Some(v) = ver.trim().strip_prefix("Python ") {
                    let parts: Vec<&str> = v.split('.').collect();
                    if parts.len() >= 2 {
                        if let (Ok(major), Ok(minor)) = (parts[0].parse::<u32>(), parts[1].parse::<u32>()) {
                            if major >= 3 && minor >= 11 {
                                #[cfg(target_os = "windows")]
                                if let Ok(w) = Command::new("where").arg(candidate).output() {
                                    let p = String::from_utf8_lossy(&w.stdout).lines().next().unwrap_or("").trim().to_string();
                                    if !p.is_empty() { return Some(p); }
                                }
                                #[cfg(not(target_os = "windows"))]
                                if let Ok(w) = Command::new("which").arg(candidate).output() {
                                    let p = String::from_utf8_lossy(&w.stdout).trim().to_string();
                                    if !p.is_empty() { return Some(p); }
                                }
                                return Some(candidate.to_string());
                            }
                        }
                    }
                }
            }
        }
    }
    None
}

#[tauri::command]
fn select_folder() -> Result<Option<String>, String> {
    #[cfg(target_os = "windows")]
    {
        let output = Command::new("powershell")
            .args(["-NoProfile", "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description = 'Select project folder'; if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath }"])
            .output()
            .map_err(|e| e.to_string())?;
        let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if path.is_empty() { return Ok(None); }
        return Ok(Some(path));
    }
    #[allow(unreachable_code)]
    Ok(None)
}

#[tauri::command]
fn setup_detect(project_root: Option<String>) -> Result<Value, String> {
    let root = project_root.unwrap_or_else(|| env::current_dir().unwrap_or_default().to_string_lossy().to_string());
    let python_path = find_python_path();
    let python_version = python_path.as_ref().and_then(|py| {
        Command::new(py).args(["--version"]).output().ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().replace("Python ", ""))
    }).unwrap_or_default();
    let node_found = Command::new("node").args(["--version"]).output().map(|o| o.status.success()).unwrap_or(false);
    let npm_found = Command::new("npm").args(["--version"]).output().map(|o| o.status.success()).unwrap_or(false);
    let home = if cfg!(windows) { env::var("USERPROFILE").unwrap_or_default() } else { env::var("HOME").unwrap_or_default() };
    let claude_found = Command::new("claude").args(["--version"]).output().map(|o| o.status.success()).unwrap_or(false);
    let claude_authed = claude_found && (
        PathBuf::from(&home).join(".claude").join("credentials.json").is_file()
        || PathBuf::from(&home).join(".claude").join("statsig_metadata").is_file()
    );
    let codex_found = Command::new("codex").args(["--version"]).output().map(|o| o.status.success()).unwrap_or(false);
    let codex_authed = codex_found && (
        env::var("OPENAI_API_KEY").is_ok()
        || PathBuf::from(&home).join(".openai").exists()
    );
    let vscode_ext = PathBuf::from(&home).join(".vscode").join("extensions");
    let has_vscode_claude = vscode_ext.is_dir() && fs::read_dir(&vscode_ext)
        .map(|entries| entries.filter_map(|e| e.ok()).any(|e| e.file_name().to_string_lossy().to_lowercase().contains("claude")))
        .unwrap_or(false);

    Ok(serde_json::json!({
        "python": { "found": python_path.is_some(), "path": python_path.unwrap_or_default(), "version": python_version },
        "node": { "found": node_found, "path": "" },
        "hosts": [
            { "name": "Claude Code CLI", "found": claude_found, "authenticated": claude_authed, "installable": npm_found },
            { "name": "VS Code Claude Extension", "found": has_vscode_claude, "authenticated": has_vscode_claude, "installable": false },
            { "name": "Codex CLI", "found": codex_found, "authenticated": codex_authed, "installable": npm_found }
        ],
        "project_root": root,
        "project_initialized": PathBuf::from(&root).join(".MEMORY").is_dir(),
        "has_mcp": PathBuf::from(&root).join(".mcp.json").is_file(),
    }))
}

#[tauri::command]
fn setup_install(targets: Vec<String>) -> Result<Value, String> {
    let mut installed: Vec<String> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    for target in &targets {
        let (cmd, args, label): (&str, Vec<&str>, &str) = match target.as_str() {
            "Claude Code CLI" => ("npm", vec!["install", "-g", "@anthropic-ai/claude-code"], "Claude Code CLI"),
            "Codex CLI" => ("npm", vec!["install", "-g", "@openai/codex"], "Codex CLI"),
            _ => { errors.push(format!("Unknown: {}", target)); continue; }
        };
        match Command::new(cmd).args(&args).output() {
            Ok(output) if output.status.success() => installed.push(label.to_string()),
            Ok(output) => errors.push(format!("{}: {}", label, String::from_utf8_lossy(&output.stderr).chars().take(200).collect::<String>())),
            Err(e) => errors.push(format!("{}: {}", label, e)),
        }
    }
    Ok(serde_json::json!({ "success": errors.is_empty(), "installed": installed, "errors": errors }))
}

#[tauri::command]
fn setup_configure(project_root: String) -> Result<Value, String> {
    // Try system Python first, fall back to bundled Python
    let python = find_python_path()
        .or_else(|| find_bundled_python())
        .ok_or("Python 3.11+ not found. Click 'Install Python' first.")?;
    let output = Command::new(&python)
        .args(["-m", "aidocs_mcp.cli", "setup", &project_root, "--auto"])
        .output()
        .map_err(|e| format!("Setup failed: {}", e))?;
    let root = PathBuf::from(&project_root);
    let home = if cfg!(windows) { env::var("USERPROFILE").unwrap_or_default() } else { env::var("HOME").unwrap_or_default() };
    Ok(serde_json::json!({
        "success": output.status.success(),
        "python_used": python,
        "mcp_path": root.join(".mcp.json").to_string_lossy().to_string(),
        "hooks_path": PathBuf::from(&home).join(".claude").join("settings.json").to_string_lossy().to_string(),
        "project_initialized": root.join(".MEMORY").is_dir(),
        "errors": if output.status.success() { Vec::<String>::new() } else {
            vec![String::from_utf8_lossy(&output.stderr).chars().take(300).collect::<String>()]
        },
    }))
}

fn find_bundled_python() -> Option<String> {
    // Check AIDOCS bundled Python locations
    #[cfg(target_os = "windows")]
    {
        let local_app_data = env::var("LOCALAPPDATA").unwrap_or_default();
        let bundled = PathBuf::from(&local_app_data).join("AIDOCS").join("python").join("python.exe");
        if bundled.is_file() { return Some(bundled.to_string_lossy().to_string()); }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let home = env::var("HOME").unwrap_or_default();
        let bundled = PathBuf::from(&home).join(".aidocs").join("python").join("bin").join("python3");
        if bundled.is_file() { return Some(bundled.to_string_lossy().to_string()); }
    }
    None
}

#[tauri::command]
fn setup_install_python() -> Result<Value, String> {
    // Run the platform-specific installer that downloads + extracts python-build-standalone
    #[cfg(target_os = "windows")]
    {
        let script_dir = env::current_exe()
            .map(|p| p.parent().unwrap_or(Path::new(".")).to_path_buf())
            .unwrap_or_default();
        // Try to find install.ps1 relative to the exe, or use the known repo path
        let install_script = if script_dir.join("..\\..\\..\\..\\core\\scripts\\install.ps1").is_file() {
            script_dir.join("..\\..\\..\\..\\core\\scripts\\install.ps1")
        } else {
            // Fallback: inline the download + extract
            return install_python_inline();
        };
        let output = Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", &install_script.to_string_lossy()])
            .output()
            .map_err(|e| format!("Install script failed: {}", e))?;
        let stdout = String::from_utf8_lossy(&output.stdout).to_string();
        return Ok(serde_json::json!({
            "success": output.status.success(),
            "python_path": find_bundled_python(),
            "output": stdout.chars().take(500).collect::<String>(),
        }));
    }
    #[allow(unreachable_code)]
    Err("Platform not supported for auto-install".to_string())
}

#[cfg(target_os = "windows")]
fn install_python_inline() -> Result<Value, String> {
    let local_app_data = env::var("LOCALAPPDATA").map_err(|e| e.to_string())?;
    let aidocs_home = PathBuf::from(&local_app_data).join("AIDOCS");
    let python_dir = aidocs_home.join("python");

    if python_dir.join("python.exe").is_file() {
        return Ok(serde_json::json!({
            "success": true,
            "python_path": python_dir.join("python.exe").to_string_lossy().to_string(),
            "output": "Python already installed",
        }));
    }

    fs::create_dir_all(&aidocs_home).map_err(|e| format!("Cannot create {}: {}", aidocs_home.display(), e))?;

    // Download python-build-standalone
    let url = "https://github.com/indygreg/python-build-standalone/releases/download/20250409/cpython-3.12.10+20250409-x86_64-pc-windows-msvc-install_only.tar.gz";
    let tmp_file = aidocs_home.join("python-download.tar.gz");

    let dl_output = Command::new("powershell")
        .args(["-NoProfile", "-Command", &format!(
            "Invoke-WebRequest -Uri '{}' -OutFile '{}' -UseBasicParsing",
            url, tmp_file.to_string_lossy()
        )])
        .output()
        .map_err(|e| format!("Download failed: {}", e))?;

    if !dl_output.status.success() {
        return Err(format!("Download failed: {}", String::from_utf8_lossy(&dl_output.stderr)));
    }

    // Extract
    let extract_output = Command::new("tar")
        .args(["-xzf", &tmp_file.to_string_lossy(), "-C", &aidocs_home.to_string_lossy()])
        .output()
        .map_err(|e| format!("Extract failed: {}", e))?;

    // Cleanup download
    let _ = fs::remove_file(&tmp_file);

    // python-build-standalone may extract to python/install/
    let install_subdir = python_dir.join("install");
    if install_subdir.is_dir() {
        let tmp_name = aidocs_home.join("python-tmp");
        let _ = fs::rename(&install_subdir, &tmp_name);
        let _ = fs::remove_dir_all(&python_dir);
        let _ = fs::rename(&tmp_name, &python_dir);
    }

    let python_exe = python_dir.join("python.exe");
    if !python_exe.is_file() {
        return Err(format!("Python not found after extraction at {}", python_exe.display()));
    }

    // Install aidocs-mcp into bundled Python
    let pip_output = Command::new(&python_exe)
        .args(["-m", "pip", "install", "aidocs-mcp", "-q"])
        .output()
        .map_err(|e| format!("pip install failed: {}", e))?;

    Ok(serde_json::json!({
        "success": extract_output.status.success() && pip_output.status.success(),
        "python_path": python_exe.to_string_lossy().to_string(),
        "output": format!("Python {} installed at {}", 
            String::from_utf8_lossy(&Command::new(&python_exe).args(["--version"]).output().map(|o| o.stdout).unwrap_or_default()),
            python_exe.display()),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;
    use std::fs;

    // ── toml_label ──

    #[test]
    fn toml_label_global_config() {
        assert_eq!(toml_label("~global/aidocs.toml"), "Global config");
    }

    #[test]
    fn toml_label_project_config() {
        assert_eq!(toml_label("aidocs.toml"), "Project config");
    }

    #[test]
    fn toml_label_session_config() {
        assert_eq!(
            toml_label(".MEMORY/sessions/2026-01-01-test/aidocs.toml"),
            "Session config"
        );
    }

    #[test]
    fn toml_label_action_tokens() {
        assert_eq!(
            toml_label("action_tokens/commit.toml"),
            "Action tokens: commit.toml"
        );
    }

    #[test]
    fn toml_label_action_hooks() {
        assert_eq!(
            toml_label("action_hooks/post_push.toml"),
            "Action hook: post_push.toml"
        );
    }

    #[test]
    fn toml_label_language_descriptor() {
        assert_eq!(
            toml_label("mcp/server/aidocs_mcp/index_languages/rust.toml"),
            "Language descriptor: rust.toml"
        );
    }

    #[test]
    fn toml_label_fallback() {
        assert_eq!(toml_label("some/other/path.toml"), "some/other/path.toml");
    }

    // ── toml_category ──

    #[test]
    fn toml_category_project() {
        assert_eq!(toml_category("aidocs.toml"), "Project config");
    }

    #[test]
    fn toml_category_tokens() {
        assert_eq!(toml_category("action_tokens/x.toml"), "Intent tokens");
    }

    #[test]
    fn toml_category_hooks() {
        assert_eq!(toml_category("action_hooks/y.toml"), "Workflow hooks");
    }

    #[test]
    fn toml_category_languages() {
        assert_eq!(
            toml_category("mcp/server/aidocs_mcp/index_languages/go.toml"),
            "Index descriptors"
        );
    }

    #[test]
    fn toml_category_session() {
        assert_eq!(
            toml_category(".MEMORY/sessions/s1/aidocs.toml"),
            "Session overrides"
        );
    }

    #[test]
    fn toml_category_fallback() {
        assert_eq!(toml_category("random.toml"), "TOML");
    }

    // ── toml_scope ──

    #[test]
    fn toml_scope_global() {
        assert_eq!(toml_scope("~global/aidocs.toml"), "Global");
    }

    #[test]
    fn toml_scope_project() {
        assert_eq!(toml_scope("aidocs.toml"), "Project");
    }

    #[test]
    fn toml_scope_session() {
        assert_eq!(toml_scope(".MEMORY/sessions/s1/aidocs.toml"), "Session");
    }

    #[test]
    fn toml_scope_tokens() {
        assert_eq!(toml_scope("action_tokens/x.toml"), "Tokens");
    }

    #[test]
    fn toml_scope_hooks() {
        assert_eq!(toml_scope("action_hooks/y.toml"), "Hooks");
    }

    #[test]
    fn toml_scope_shared_index() {
        assert_eq!(toml_scope("shared_index_config.toml"), "Shared index");
    }

    #[test]
    fn toml_scope_language() {
        assert_eq!(
            toml_scope("mcp/server/aidocs_mcp/index_languages/py.toml"),
            "Language"
        );
    }

    #[test]
    fn toml_scope_fallback() {
        assert_eq!(toml_scope("other.toml"), "TOML");
    }

    // ── toml_target ──

    #[test]
    fn toml_target_project_root() {
        assert_eq!(toml_target("aidocs.toml"), "Current project");
    }

    #[test]
    fn toml_target_session_path() {
        assert_eq!(
            toml_target(".MEMORY/sessions/2026-03-15-review/aidocs.toml"),
            "2026-03-15-review"
        );
    }

    #[test]
    fn toml_target_file_stem() {
        assert_eq!(toml_target("action_tokens/commit.toml"), "commit");
    }

    #[test]
    fn toml_target_bare_name() {
        assert_eq!(toml_target("something.toml"), "something");
    }

    // ── descriptor_enabled ──

    #[test]
    fn descriptor_enabled_none_languages() {
        assert_eq!(descriptor_enabled("rust", None), "Unknown");
    }

    #[test]
    fn descriptor_enabled_all() {
        assert_eq!(descriptor_enabled("rust", Some("all")), "Enabled");
    }

    #[test]
    fn descriptor_enabled_all_case_insensitive() {
        assert_eq!(descriptor_enabled("rust", Some("ALL")), "Enabled");
    }

    #[test]
    fn descriptor_enabled_matching_csv() {
        assert_eq!(
            descriptor_enabled("rust", Some("python, rust, go")),
            "Enabled"
        );
    }

    #[test]
    fn descriptor_enabled_not_matching_csv() {
        assert_eq!(
            descriptor_enabled("rust", Some("python, go")),
            "Not selected"
        );
    }

    // ── toml_active_status ──

    #[test]
    fn toml_active_status_project_config() {
        assert_eq!(toml_active_status("aidocs.toml", "proj", None), "Active");
    }

    #[test]
    fn toml_active_status_session() {
        assert_eq!(
            toml_active_status(".MEMORY/sessions/s1/aidocs.toml", "s1", None),
            "Session s1"
        );
    }

    #[test]
    fn toml_active_status_action_tokens_enabled() {
        assert_eq!(
            toml_active_status("action_tokens/commit.toml", "commit", Some("all")),
            "Enabled"
        );
    }

    #[test]
    fn toml_active_status_action_tokens_not_selected() {
        assert_eq!(
            toml_active_status("action_tokens/commit.toml", "commit", Some("review")),
            "Not selected"
        );
    }

    #[test]
    fn toml_active_status_hooks() {
        assert_eq!(
            toml_active_status("action_hooks/post.toml", "post", None),
            "Loaded"
        );
    }

    #[test]
    fn toml_active_status_shared_index() {
        assert_eq!(
            toml_active_status("shared_index_config.toml", "shared", None),
            "Shared"
        );
    }

    #[test]
    fn toml_active_status_language_descriptor() {
        assert_eq!(
            toml_active_status(
                "mcp/server/aidocs_mcp/index_languages/rust.toml",
                "rust",
                Some("rust, python")
            ),
            "Enabled"
        );
    }

    #[test]
    fn toml_active_status_fallback() {
        assert_eq!(
            toml_active_status("random.toml", "random", None),
            "Available"
        );
    }

    // ── toml_language_context ──

    #[test]
    fn toml_language_context_project_with_languages() {
        assert_eq!(
            toml_language_context("aidocs.toml", "", Some("rust, python")),
            "Connected languages: rust, python"
        );
    }

    #[test]
    fn toml_language_context_project_default() {
        assert_eq!(
            toml_language_context("aidocs.toml", "", None),
            "Connected languages: default"
        );
    }

    #[test]
    fn toml_language_context_session_override() {
        let content = "[languages]\nenabled = \"csharp\"";
        assert_eq!(
            toml_language_context(".MEMORY/sessions/s1/aidocs.toml", content, None),
            "Override languages: csharp"
        );
    }

    #[test]
    fn toml_language_context_session_inherits() {
        assert_eq!(
            toml_language_context(".MEMORY/sessions/s1/aidocs.toml", "", None),
            "Inherits project language set"
        );
    }

    #[test]
    fn toml_language_context_shared_index() {
        assert_eq!(
            toml_language_context("shared_index_config.toml", "", None),
            "Shared indexing defaults"
        );
    }

    #[test]
    fn toml_language_context_language_descriptor() {
        let content = "name = \"Rust\"\nextensions = [\"rs\"]";
        let result = toml_language_context(
            "mcp/server/aidocs_mcp/index_languages/rust.toml",
            content,
            Some("all"),
        );
        assert!(result.contains("Rust"));
        assert!(result.contains("rs"));
        assert!(result.contains("all"));
    }

    #[test]
    fn toml_language_context_action_tokens() {
        assert_eq!(
            toml_language_context("action_tokens/commit.toml", "", Some("python")),
            "Intent classification tokens \u{00b7} project set python"
        );
    }

    #[test]
    fn toml_language_context_action_hooks() {
        assert_eq!(
            toml_language_context("action_hooks/post.toml", "", None),
            "Workflow and interaction hooks"
        );
    }

    #[test]
    fn toml_language_context_fallback() {
        assert_eq!(
            toml_language_context("other.toml", "", None),
            "TOML document"
        );
    }

    // ── is_aidocs_project (filesystem) ──

    #[test]
    fn is_aidocs_project_true_when_memory_exists() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        assert!(is_aidocs_project(dir.path()));
    }

    #[test]
    fn is_aidocs_project_false_when_no_memory() {
        let dir = tempfile::tempdir().unwrap();
        assert!(!is_aidocs_project(dir.path()));
    }

    // ── count_sessions ──

    #[test]
    fn count_sessions_zero() {
        let dir = tempfile::tempdir().unwrap();
        let sessions = dir.path().join(".MEMORY").join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        assert_eq!(count_sessions(dir.path()), 0);
    }

    #[test]
    fn count_sessions_three() {
        let dir = tempfile::tempdir().unwrap();
        let sessions = dir.path().join(".MEMORY").join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        for name in ["s1", "s2", "s3"] {
            fs::create_dir(sessions.join(name)).unwrap();
        }
        assert_eq!(count_sessions(dir.path()), 3);
    }

    #[test]
    fn count_sessions_missing_dir() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(count_sessions(dir.path()), 0);
    }

    #[test]
    fn count_sessions_ignores_files() {
        let dir = tempfile::tempdir().unwrap();
        let sessions = dir.path().join(".MEMORY").join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        fs::create_dir(sessions.join("real-session")).unwrap();
        fs::write(sessions.join("not-a-session.txt"), "hi").unwrap();
        assert_eq!(count_sessions(dir.path()), 1);
    }

    // ── managed_session_id ──

    #[test]
    fn managed_session_id_active() {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join(".MEMORY").join("config");
        fs::create_dir_all(&config).unwrap();
        fs::write(
            config.join("aidocs-managed.json"),
            r#"{"active":true,"session_id":"2026-04-01-test"}"#,
        )
        .unwrap();
        assert_eq!(
            managed_session_id(dir.path()),
            Some("2026-04-01-test".to_string())
        );
    }

    #[test]
    fn managed_session_id_inactive() {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join(".MEMORY").join("config");
        fs::create_dir_all(&config).unwrap();
        fs::write(
            config.join("aidocs-managed.json"),
            r#"{"active":false,"session_id":"2026-04-01-test"}"#,
        )
        .unwrap();
        assert_eq!(managed_session_id(dir.path()), None);
    }

    #[test]
    fn managed_session_id_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(managed_session_id(dir.path()), None);
    }

    #[test]
    fn managed_session_id_malformed_json() {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join(".MEMORY").join("config");
        fs::create_dir_all(&config).unwrap();
        fs::write(config.join("aidocs-managed.json"), "not json").unwrap();
        assert_eq!(managed_session_id(dir.path()), None);
    }

    // ── register_project_candidate ──

    #[test]
    fn register_candidate_adds_valid_project() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        let mut seen = BTreeSet::new();
        let mut projects = Vec::new();

        register_project_candidate(
            dir.path(), Some("My Project"), dir.path(), &mut seen, &mut projects,
        );

        assert_eq!(projects.len(), 1);
        assert_eq!(projects[0].title, "MY PROJECT");
        assert!(projects[0].current);
    }

    #[test]
    fn register_candidate_skips_non_aidocs() {
        let dir = tempfile::tempdir().unwrap();
        let mut seen = BTreeSet::new();
        let mut projects = Vec::new();

        register_project_candidate(
            dir.path(), None, dir.path(), &mut seen, &mut projects,
        );

        assert!(projects.is_empty());
    }

    #[test]
    fn register_candidate_deduplicates() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        let mut seen = BTreeSet::new();
        let mut projects = Vec::new();

        register_project_candidate(
            dir.path(), Some("First"), dir.path(), &mut seen, &mut projects,
        );
        register_project_candidate(
            dir.path(), Some("Second"), dir.path(), &mut seen, &mut projects,
        );

        assert_eq!(projects.len(), 1);
        assert_eq!(projects[0].title, "FIRST");
    }

    #[test]
    fn register_candidate_title_normalization() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        let mut seen = BTreeSet::new();
        let mut projects = Vec::new();

        register_project_candidate(
            dir.path(), Some("my-cool_project"), dir.path(), &mut seen, &mut projects,
        );

        assert_eq!(projects[0].title, "MY COOL PROJECT");
    }

    #[test]
    fn register_candidate_not_current() {
        let dir = tempfile::tempdir().unwrap();
        let other = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        let mut seen = BTreeSet::new();
        let mut projects = Vec::new();

        register_project_candidate(
            dir.path(), Some("Other"), other.path(), &mut seen, &mut projects,
        );

        assert_eq!(projects.len(), 1);
        assert!(!projects[0].current);
    }

    // ── allowed_toml_paths (filesystem) ──

    #[test]
    fn allowed_paths_includes_project_config() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("aidocs.toml"), "# config").unwrap();

        let paths = allowed_toml_paths(dir.path(), None).unwrap();
        assert!(paths.iter().any(|p| p.ends_with("aidocs.toml")));
    }

    #[test]
    fn allowed_paths_includes_session_config() {
        let dir = tempfile::tempdir().unwrap();
        let session_dir = dir
            .path()
            .join(".MEMORY")
            .join("sessions")
            .join("s1");
        fs::create_dir_all(&session_dir).unwrap();
        fs::write(session_dir.join("aidocs.toml"), "# session").unwrap();

        let paths = allowed_toml_paths(dir.path(), Some("s1")).unwrap();
        assert!(paths
            .iter()
            .any(|p| p.to_string_lossy().contains("s1")));
    }

    #[test]
    fn allowed_paths_includes_action_tokens() {
        let dir = tempfile::tempdir().unwrap();
        let tokens = dir.path().join("action_tokens");
        fs::create_dir_all(&tokens).unwrap();
        fs::write(tokens.join("commit.toml"), "# token").unwrap();

        let paths = allowed_toml_paths(dir.path(), None).unwrap();
        assert!(paths
            .iter()
            .any(|p| p.to_string_lossy().contains("commit.toml")));
    }

    #[test]
    fn allowed_paths_includes_language_descriptors() {
        let dir = tempfile::tempdir().unwrap();
        let langs = dir
            .path()
            .join("mcp")
            .join("server")
            .join("aidocs_mcp")
            .join("index_languages");
        fs::create_dir_all(&langs).unwrap();
        fs::write(langs.join("rust.toml"), "# lang").unwrap();

        let paths = allowed_toml_paths(dir.path(), None).unwrap();
        assert!(paths
            .iter()
            .any(|p| p.to_string_lossy().contains("rust.toml")));
    }

    #[test]
    fn allowed_paths_empty_session_id_ignored() {
        let dir = tempfile::tempdir().unwrap();
        let paths = allowed_toml_paths(dir.path(), Some("")).unwrap();
        assert!(paths
            .iter()
            .all(|p| !p.to_string_lossy().contains("sessions")));
    }

    // ── resolve_allowed_toml_path ──

    #[test]
    fn resolve_allowed_path_passes_for_allowed() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("aidocs.toml"), "# config").unwrap();

        let result = resolve_allowed_toml_path(dir.path(), "aidocs.toml", None);
        assert!(result.is_ok());
        assert!(result.unwrap().ends_with("aidocs.toml"));
    }

    #[test]
    fn resolve_allowed_path_rejects_disallowed() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("secret.toml"), "# secret").unwrap();

        let result = resolve_allowed_toml_path(dir.path(), "secret.toml", None);
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .contains("not part of the dashboard control surface"));
    }

    #[test]
    fn resolve_allowed_path_rejects_empty() {
        let dir = tempfile::tempdir().unwrap();
        let result = resolve_allowed_toml_path(dir.path(), "", None);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("TOML path is required"));
    }

    // ── load_toml_documents_for_session ──

    #[test]
    fn load_toml_docs_assembles_full_vec() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("aidocs.toml"),
            "[languages]\nenabled = \"rust, python\"\n",
        )
        .unwrap();
        let tokens = dir.path().join("action_tokens");
        fs::create_dir_all(&tokens).unwrap();
        fs::write(tokens.join("commit.toml"), "name = \"commit\"\n").unwrap();

        let docs = load_toml_documents_for_session(dir.path(), None).unwrap();

        assert!(docs.len() >= 2);

        let project_doc = docs.iter().find(|d| d.path == "aidocs.toml").unwrap();
        assert_eq!(project_doc.label, "Project config");
        assert_eq!(project_doc.category, "Project config");
        assert_eq!(project_doc.scope, "Project");
        assert_eq!(project_doc.active, "Active");
        assert!(project_doc.language_context.contains("rust, python"));
        assert!(project_doc.editable);
        assert!(!project_doc.content.is_empty());
    }

    // ── resolve_project_root ──

    #[test]
    fn resolve_root_explicit_path() {
        let dir = tempfile::tempdir().unwrap();
        let result =
            resolve_project_root(Some(dir.path().to_string_lossy().to_string()));
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), dir.path());
    }

    #[test]
    fn resolve_root_nonexistent_falls_through() {
        let result =
            resolve_project_root(Some("/nonexistent/path/xyz".to_string()));
        // Should fall through to env var / cwd walk-up
        if let Ok(path) = &result {
            assert!(path.join(".MEMORY").is_dir());
        }
    }

    #[test]
    fn resolve_root_none_without_project() {
        // With no explicit path and (likely) no AIDOCS_PATH, walks up from cwd
        let result = resolve_project_root(None);
        match result {
            Ok(path) => assert!(path.join(".MEMORY").is_dir()),
            Err(msg) => assert!(msg.contains("Could not resolve")),
        }
    }
}

