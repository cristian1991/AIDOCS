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
}

static CONDUCTOR: std::sync::LazyLock<Mutex<ConductorState>> =
    std::sync::LazyLock::new(|| Mutex::new(ConductorState { process: None }));

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

    let prompt = format!(
        "You are the AIDOCS conductor for project at {}. \
        Use AIDOCS MCP tools to manage lanes, dispatch workers, verify results. \
        Wait for user commands.",
        root.display()
    );

    let child = std::process::Command::new(cli)
        .args(["-p", &prompt, "--output-format", "text"])
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
            open_url
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
