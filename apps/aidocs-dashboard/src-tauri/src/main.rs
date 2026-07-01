#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
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
    // Non-empty when the document is read-only because its runtime
    // authority moved off the TOML file (legacy config / build-source).
    deprecated: String,
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

// Install-wide project registry sqlite — mirrors KnownProjectsStore's
// path resolution (mcp/server/aidocs_mcp/known_projects_store.py).
// MCP migrated from project-registry.json to this DB in Beat 4 and
// HARD-DELETES the JSON after first ingest, so the JSON fallback
// alone surfaces only the current project after any MCP call runs.
fn global_registry_db_path() -> Option<PathBuf> {
    if let Ok(override_path) = env::var("AIDOCS_GLOBAL_CONFIG_DB") {
        let trimmed = override_path.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    let home = if let Some(userprofile) = env::var_os("USERPROFILE") {
        PathBuf::from(userprofile)
    } else if let Some(home) = env::var_os("HOME") {
        PathBuf::from(home)
    } else {
        return None;
    };
    Some(home.join(".aidocs").join("config.sqlite3"))
}

// Rows: (project_root, title). Returns empty vec on any error — the
// registry is advisory, never fatal to dashboard startup.
fn read_known_projects_from_sqlite() -> Vec<GlobalProjectRegistryEntry> {
    let Some(db_path) = global_registry_db_path() else {
        return Vec::new();
    };
    if !db_path.is_file() {
        return Vec::new();
    }
    let conn = match rusqlite::Connection::open_with_flags(
        &db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    let mut stmt = match conn.prepare(
        "SELECT project_root, title FROM known_projects",
    ) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let rows = stmt.query_map([], |row| {
        Ok(GlobalProjectRegistryEntry {
            project_root: row.get::<_, String>(0)?,
            title: row.get::<_, Option<String>>(1).ok().flatten(),
        })
    });
    let iter = match rows {
        Ok(i) => i,
        Err(_) => return Vec::new(),
    };
    iter.filter_map(Result::ok).collect()
}

fn is_aidocs_project(candidate: &Path) -> bool {
    // Marker->sqlite migration: the AIDOCS-managed signal is the DELIBERATE
    // commission stamp (index_meta['aidocs_commissioned']) inside the sqlite
    // index — NOT bare .MEMORY/ (foreign memory systems create that) and NOT
    // the db file's mere existence (any store touch creates the file).
    let db_path = candidate
        .join(".MEMORY")
        .join(".index")
        .join("aidocs.sqlite3");
    if !db_path.is_file() {
        return false;
    }
    let Ok(conn) = rusqlite::Connection::open_with_flags(
        &db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) else {
        return false;
    };
    conn.query_row(
        "SELECT 1 FROM index_meta WHERE key = 'aidocs_commissioned'",
        [],
        |_| Ok(()),
    )
    .is_ok()
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
    // Managed-mode state lives in the big-boss aidocs.sqlite3 post-Beat-3.
    // The Python store ingests the legacy JSON and hard-deletes it on
    // first touch, so this is the only authoritative read path. Open
    // the DB read-only — the dashboard must never mutate project DBs.
    let db_path = project_root
        .join(".MEMORY")
        .join(".index")
        .join("aidocs.sqlite3");
    if !db_path.is_file() {
        return None;
    }
    let conn = rusqlite::Connection::open_with_flags(
        &db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .ok()?;
    let mut stmt = conn
        .prepare("SELECT active, session_id FROM aidocs_managed WHERE id = 1")
        .ok()?;
    let row = stmt
        .query_row([], |r| {
            // active is stored as INTEGER (0/1); session_id is TEXT.
            let active: i64 = r.get(0)?;
            let session_id: Option<String> = r.get(1)?;
            Ok((active, session_id))
        })
        .ok()?;
    let (active, session_id) = row;
    if active != 1 {
        return None;
    }
    session_id.filter(|s| !s.trim().is_empty())
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

fn now_seconds() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn push_conductor_output(key: &ConductorKey, stream: &str, text: String) {
    let trimmed = text.trim_end().to_string();
    if trimmed.is_empty() {
        return;
    }
    if let Ok(mut map) = CONDUCTORS.lock() {
        if let Some(state) = map.get_mut(key) {
            let is_duplicate = state
                .output
                .last()
                .map(|(_, last_stream, last_text)| last_stream == stream && last_text == &trimmed)
                .unwrap_or(false);
            if is_duplicate {
                return;
            }
            state
                .output
                .push((now_seconds(), stream.to_string(), trimmed));
            if state.output.len() > 2000 {
                let drain_count = state.output.len().saturating_sub(2000);
                state.output.drain(0..drain_count);
            }
        }
    }
}

fn handle_claude_stream_event(key: &ConductorKey, line: &str) {
    let envelope: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => {
            push_conductor_output(key, "stderr", format!("[non-json stdout] {line}"));
            return;
        }
    };
    let kind = envelope.get("type").and_then(|v| v.as_str()).unwrap_or("");
    match kind {
        "system" => {
            let subtype = envelope
                .get("subtype")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if subtype == "init" {
                if let Some(sid) = envelope.get("session_id").and_then(|v| v.as_str()) {
                    if let Ok(mut map) = CONDUCTORS.lock() {
                        if let Some(state) = map.get_mut(key) {
                            state.claude_session_id = Some(sid.to_string());
                        }
                    }
                }
            }
        }
        "assistant" => {
            if let Some(content) = envelope
                .get("message")
                .and_then(|m| m.get("content"))
                .and_then(|c| c.as_array())
            {
                for block in content {
                    let block_type = block.get("type").and_then(|v| v.as_str()).unwrap_or("");
                    if block_type == "text" {
                        if let Some(text) = block.get("text").and_then(|v| v.as_str()) {
                            if !text.is_empty() {
                                push_conductor_output(key, "stdout", text.to_string());
                            }
                        }
                    }
                }
            }
        }
        "result" => {
            let is_error = envelope
                .get("is_error")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if is_error {
                if let Some(text) = envelope.get("result").and_then(|v| v.as_str()) {
                    push_conductor_output(key, "stderr", text.to_string());
                }
            }
        }
        _ => {}
    }
}

fn handle_codex_stream_event(key: &ConductorKey, line: &str) {
    let trimmed = line.trim_start();
    if !trimmed.starts_with('{') {
        return;
    }
    let envelope: Value = match serde_json::from_str(trimmed) {
        Ok(v) => v,
        Err(_) => return,
    };
    let kind = envelope.get("type").and_then(|v| v.as_str()).unwrap_or("");
    match kind {
        "thread.started" => {
            if let Some(tid) = envelope.get("thread_id").and_then(|v| v.as_str()) {
                if let Ok(mut map) = CONDUCTORS.lock() {
                    if let Some(state) = map.get_mut(key) {
                        state.codex_session_id = Some(tid.to_string());
                    }
                }
            }
        }
        "item.completed" | "message" | "assistant" => {
            if let Some(text) = envelope.get("text").and_then(|v| v.as_str()) {
                if !text.is_empty() {
                    push_conductor_output(key, "stdout", text.to_string());
                }
                return;
            }
            if let Some(content) = envelope.get("content").and_then(|v| v.as_array()) {
                for block in content {
                    if let Some(text) = block.get("text").and_then(|v| v.as_str()) {
                        if !text.is_empty() {
                            push_conductor_output(key, "stdout", text.to_string());
                        }
                    }
                }
                return;
            }
            if let Some(delta) = envelope.get("delta").and_then(|v| v.as_str()) {
                if !delta.is_empty() {
                    push_conductor_output(key, "stdout", delta.to_string());
                }
            }
        }
        "error" | "turn.failed" => {
            let msg = envelope
                .get("message")
                .and_then(|v| v.as_str())
                .or_else(|| envelope.get("error").and_then(|e| e.get("message")).and_then(|v| v.as_str()))
                .unwrap_or("(unknown codex error)");
            push_conductor_output(key, "stderr", msg.to_string());
        }
        "turn.started" | "turn.completed" | "thread.completed" => {}
        _ => {}
    }
}

fn spawn_conductor_output_readers(
    child: &mut std::process::Child,
    backend: &str,
    key: ConductorKey,
) {
    let backend_owned = backend.to_string();
    if let Some(stdout) = child.stdout.take() {
        let backend_reader = backend_owned.clone();
        let key_reader = key.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(text) => {
                        if backend_reader == "claude" {
                            handle_claude_stream_event(&key_reader, &text);
                        } else if backend_reader == "codex" {
                            handle_codex_stream_event(&key_reader, &text);
                        } else {
                            push_conductor_output(&key_reader, "stdout", text);
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        let key_reader = key;
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                match line {
                    Ok(text) => push_conductor_output(&key_reader, "stderr", text),
                    Err(_) => break,
                }
            }
        });
    }
}

fn discover_managed_projects(current_root: &Path) -> Vec<ManagedProjectOption> {
    let mut seen = BTreeSet::new();
    let mut projects = Vec::new();

    register_project_candidate(current_root, None, current_root, &mut seen, &mut projects);

    // Authoritative source post-Beat-4: install-wide sqlite registry.
    // The JSON fallback below stays as a migration safety net for
    // pre-Beat-4 installs that never ran an MCP call since the upgrade
    // (the ingest+delete hasn't fired yet for those boxes).
    for entry in read_known_projects_from_sqlite() {
        let candidate = Path::new(&entry.project_root);
        if candidate.join(".MEMORY").is_dir() {
            register_project_candidate(
                candidate,
                entry.title.as_deref(),
                current_root,
                &mut seen,
                &mut projects,
            );
        }
    }

    if let Some(path) = global_registry_path() {
        if let Ok(text) = fs::read_to_string(path) {
            if let Ok(payload) = serde_json::from_str::<GlobalProjectRegistryPayload>(&text) {
                // Prune entries whose project root no longer has .MEMORY/
                let alive: Vec<_> = payload
                    .projects
                    .into_iter()
                    .filter(|entry| Path::new(&entry.project_root).join(".MEMORY").is_dir())
                    .collect();
                for entry in &alive {
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
    // Security (120% §16): run the sensitive AIDOCS CLI (token mint, escalation
    // approve/deny, config writes) through the PINNED python that ships with
    // AIDOCS FIRST (resolve_mcp_python -> find_bundled_python), so a PATH-hijack
    // or a shim cannot substitute a malicious interpreter for the security
    // backend. The bare host-PATH names remain ONLY as a bootstrap fallback
    // (before the bundled python is installed) and are tried LAST.
    let mcp_args = {
        let mut items = vec!["-m".to_string(), "aidocs_mcp.cli".to_string()];
        items.extend(args.to_vec());
        items
    };
    let mut command_sets: Vec<(String, Vec<String>)> = Vec::new();
    if let Some(py) = resolve_mcp_python() {
        command_sets.push((py, mcp_args.clone()));
    }
    command_sets.push(("python".to_string(), mcp_args.clone()));
    command_sets.push(("py".to_string(), mcp_args));
    command_sets.push(("aidocs".to_string(), args.to_vec()));

    // Single-flight: only one dashboard Python runs at a time (see SPAWN_GATE).
    let _spawn_gate = SPAWN_GATE.lock().unwrap_or_else(|p| p.into_inner());
    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in command_sets {
        let mut cmd = Command::new(&program);
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
            let code = output
                .status
                .code()
                .map(|c| c.to_string())
                .unwrap_or_else(|| "?".into());
            errors.push(format!(
                "{} (exit {}): {} {}",
                program,
                code,
                if stderr.is_empty() {
                    "(no stderr)"
                } else {
                    &stderr
                },
                stdout_short
            ));
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

/// Like `run_json_cli`, but a LOGICAL failure (`{"ok": false}`) is
/// surfaced as an `Err` even when the process exited 0. Without this a
/// refused/failed mutation (e.g. operator_auth, validation) parses as a
/// resolved value and the frontend shows a success notice + checkmark.
/// Use for mutating commands; reads that legitimately return ok:false
/// (e.g. auth status) should keep calling `run_json_cli` directly.
fn run_json_cli_checked(project_root: &Path, args: &[String]) -> Result<Value, String> {
    let parsed = run_json_cli(project_root, args)?;
    if parsed.get("ok") == Some(&Value::Bool(false)) {
        let msg = parsed
            .get("message")
            .and_then(|m| m.as_str())
            .or_else(|| parsed.get("error").and_then(|e| e.as_str()))
            .or_else(|| parsed.get("reason").and_then(|r| r.as_str()))
            .unwrap_or("operation failed");
        return Err(msg.to_string());
    }
    Ok(parsed)
}

/// Process-lifetime cache of the dashboard operator token.
///
/// Token-lifecycle fix (2026-05-20 +3): previously
/// resolve_operator_token minted a FRESH token on every admin
/// command, growing identity_tokens unbounded. Now the token is
/// minted once per dashboard process and cached here; subsequent
/// admin commands reuse it after a cheap validity check. Logout /
/// app-exit revokes it.
static OPERATOR_TOKEN_CACHE: std::sync::Mutex<Option<String>> =
    std::sync::Mutex::new(None);

/// Serialize dashboard Python spawns (2026-06-30 storm fix). The frontend can
/// fire several `invoke()`s near-simultaneously (the 2s poll + the project/TOML
/// effect + a manual refresh), and each `run_json_cli` / `run_python_json_command`
/// blocks on `cmd.output()`. Without this gate they spawn CONCURRENT Python CLIs
/// — and a churning frontend (an oscillating session selection) piled up dozens,
/// dragging the whole machine even after the window was "closed". Holding this
/// mutex across the spawn caps the dashboard to ONE live Python at a time;
/// callers wait briefly rather than fan out. Poisoning is tolerated so a panicked
/// holder can never wedge the gate forever.
static SPAWN_GATE: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn _mint_operator_token(project_root: &Path) -> Option<String> {
    let args = vec![
        "dashboard-auth-token".to_string(),
        "--json".to_string(),
    ];
    match run_json_cli(project_root, &args) {
        Ok(v) => v
            .get("token")
            .and_then(|t| t.as_str())
            .map(|s| s.to_string()),
        Err(_) => None,
    }
}

fn _token_is_valid(project_root: &Path, token: &str) -> bool {
    let args = vec![
        "dashboard-auth-status".to_string(),
        "--operator-token".to_string(),
        token.to_string(),
        "--json".to_string(),
    ];
    match run_json_cli(project_root, &args) {
        Ok(v) => v
            .get("authenticated")
            .and_then(|a| a.as_bool())
            .unwrap_or(false),
        Err(_) => false,
    }
}

/// Resolve a local operator token, reusing the cached one when still
/// valid. Mints + caches a new token only when the cache is empty or
/// the cached token has expired/been revoked. Returns None on
/// corpo-without-login / bootstrap failure (callers surface the
/// refusal).
fn resolve_operator_token(project_root: &Path) -> Option<String> {
    // Fast path: reuse the cached token if it still validates.
    {
        let guard = OPERATOR_TOKEN_CACHE.lock().ok()?;
        if let Some(tok) = guard.as_ref() {
            if _token_is_valid(project_root, tok) {
                return Some(tok.clone());
            }
        }
    }
    // Slow path: mint a fresh token + cache it.
    let minted = _mint_operator_token(project_root)?;
    if let Ok(mut guard) = OPERATOR_TOKEN_CACHE.lock() {
        *guard = Some(minted.clone());
    }
    Some(minted)
}

/// Revoke + clear the cached operator token (dashboard logout / app
/// exit). Best-effort: clears the in-process cache and asks the CLI
/// to revoke the row + GC expired tokens.
fn revoke_cached_operator_token(project_root: &Path) {
    let token = {
        match OPERATOR_TOKEN_CACHE.lock() {
            Ok(mut guard) => guard.take(),
            Err(_) => None,
        }
    };
    if let Some(tok) = token {
        let args = vec![
            "dashboard-auth-logout".to_string(),
            "--operator-token".to_string(),
            tok,
            "--json".to_string(),
        ];
        let _ = run_json_cli(project_root, &args);
    }
}

#[tauri::command]
fn dashboard_auth_status(project_root: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    // Reflect the cached token's status without minting.
    let cached = OPERATOR_TOKEN_CACHE
        .lock()
        .ok()
        .and_then(|g| g.clone());
    match cached {
        Some(tok) => {
            let args = vec![
                "dashboard-auth-status".to_string(),
                "--operator-token".to_string(),
                tok,
                "--json".to_string(),
            ];
            run_json_cli(&root, &args)
        }
        None => Ok(serde_json::json!({
            "ok": true, "authenticated": false, "user_id": "", "role": "",
        })),
    }
}

#[tauri::command]
fn dashboard_logout(project_root: Option<String>) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    revoke_cached_operator_token(&root);
    Ok(serde_json::json!({"ok": true, "logged_out": true}))
}

fn run_python_json_command(
    command_sets: Vec<(&str, Vec<String>)>,
    label: &str,
) -> Result<Value, String> {
    // Security (120% §16): prefer the PINNED bundled python that ships with
    // AIDOCS for any caller targeting a bare host-PATH python/py, so a PATH-hijack
    // or shim cannot substitute the interpreter. Host PATH stays as a fallback.
    let mut sets: Vec<(String, Vec<String>)> = Vec::new();
    if let Some(py) = resolve_mcp_python() {
        if let Some((_, a)) = command_sets.iter().find(|(p, _)| *p == "python" || *p == "py") {
            sets.push((py, a.clone()));
        }
    }
    for (p, a) in command_sets {
        sets.push((p.to_string(), a));
    }
    // Single-flight: only one dashboard Python runs at a time (see SPAWN_GATE).
    let _spawn_gate = SPAWN_GATE.lock().unwrap_or_else(|p| p.into_inner());
    let mut errors: Vec<String> = Vec::new();
    for (program, program_args) in sets {
        let mut cmd = Command::new(&program);
        cmd.args(&program_args);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        // Timebox the spawn (#201): a wedged python must not hold SPAWN_GATE and
        // block every dashboard python feature. wait_with_output reads the pipes
        // concurrently (no deadlock on large output); a watchdog kills the child
        // if it overruns 20s.
        use std::process::Stdio;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::Arc;
        use std::time::Duration;
        cmd.stdout(Stdio::piped());
        cmd.stderr(Stdio::piped());
        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(err) => {
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        let pid = child.id();
        let done = Arc::new(AtomicBool::new(false));
        let done_w = done.clone();
        let watchdog = std::thread::spawn(move || {
            for _ in 0..200 {
                if done_w.load(Ordering::SeqCst) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            if !done_w.load(Ordering::SeqCst) {
                let mut k = Command::new("taskkill");
                k.args(["/PID", &pid.to_string(), "/T", "/F"]);
                #[cfg(target_os = "windows")]
                {
                    use std::os::windows::process::CommandExt;
                    k.creation_flags(0x08000000);
                }
                let _ = k.output();
            }
        });
        let output = match child.wait_with_output() {
            Ok(o) => o,
            Err(err) => {
                done.store(true, Ordering::SeqCst);
                let _ = watchdog.join();
                errors.push(format!("{}: {}", program, err));
                continue;
            }
        };
        done.store(true, Ordering::SeqCst);
        let _ = watchdog.join();
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            errors.push(format!("{}: {} (or 20s timeout, killed)", program, stderr));
            continue;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        return serde_json::from_str::<Value>(&stdout)
            .map_err(|err| format!("Failed to parse {label} JSON from {}: {}", program, err));
    }
    Err(format!(
        "{label} bridge could not run Python. {}",
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
    run_python_json_command(command_sets, "Registry search")
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
    run_python_json_command(command_sets, "Metrics snapshot")
}

fn run_python_skill_scan(project_root: &Path, session_id: Option<&str>) -> Result<Value, String> {
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.cli import _dashboard_runtime
from aidocs_mcp.skill_store import SkillStore
from aidocs_mcp.skill_scanner import scan_skill
root = Path(sys.argv[1])
session_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
store = SkillStore()
selected = store.get_selected_skills(root, session_id) if session_id else {"selected_skills": []}
selected_ids = set(selected.get("selected_skills", []))
active_ids = set()
provider_statuses = {}
selected_skill_modes = {}
runtime_owned_capabilities = []
_, runtime = _dashboard_runtime()
for skill in store.list_skills(root):
    if str(skill.get("source") or "") != "external_provider":
        continue
    provider_id = str(skill.get("provider") or "")
    if not provider_id or provider_id in provider_statuses:
        continue
    try:
        provider_statuses[provider_id] = runtime.skill_provider_status(root, provider_id)
    except Exception:
        provider_statuses[provider_id] = None
if session_id:
    active_state = runtime._read_host_skill_state(root, session_id)
    active_ids = set(active_state.get("active_skills", []))
    mode_metadata = active_state.get("mode_metadata") if isinstance(active_state.get("mode_metadata"), dict) else {}
    selected_skill_modes = mode_metadata.get("selected_skill_modes") if isinstance(mode_metadata.get("selected_skill_modes"), dict) else {}
    runtime_owned_capabilities = [
        item for item in (active_state.get("runtime_owned_capabilities") or []) if isinstance(item, dict)
    ]
results = []
for skill in store.list_skills(root):
    content = str(skill.get("content") or "")
    skill_id = str(skill.get("skill_id") or skill.get("name") or "unknown")
    result = scan_skill(skill_id, content)
    provider_id = str(skill.get("provider") or "")
    skill_source = str(skill.get("source") or "")
    skill_kind = str(skill.get("skill_kind") or "helper")
    activation_tags = []
    if skill_source in {"bundled_provider", "project"} and skill_kind in {"helper", "reasoning", "verification", "authoring"}:
        activation_tags.append("session helper")
    elif skill_source == "external_provider":
        activation_tags.append("prompt-triggered")
    override_mode = str(selected_skill_modes.get(skill_id) or "")
    if override_mode == "provider_content_aidocs_runtime":
        activation_tags.append("provider content")
    runtime_owned = any(str(item.get("selected_skill_id") or "") == skill_id for item in runtime_owned_capabilities)
    if runtime_owned:
        activation_tags.append("runtime-owned")
    if skill_id in active_ids:
        activation_tags.append("session active")
    results.append({
        "skill": skill,
        "selected": skill_id in selected_ids,
        "active": skill_id in active_ids,
        "activation_tags": activation_tags,
        "provider_status": provider_statuses.get(provider_id),
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
    run_python_json_command(command_sets, "Skill scan")
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
    run_python_json_command(command_sets, "Context budget")
}

fn allowed_toml_paths(
    project_root: &Path,
    session_id: Option<&str>,
) -> Result<Vec<PathBuf>, String> {
    let mut paths: Vec<PathBuf> = Vec::new();

    let project_config = project_root.join("aidocs.toml");
    if project_config.is_file() {
        paths.push(project_config);
    }

    if let Some(session_id) = session_id {
        let trimmed = session_id.trim();
        if !trimmed.is_empty() {
            let session_config = project_root
                .join(".MEMORY")
                .join("sessions")
                .join(trimmed)
                .join("aidocs.toml");
            if session_config.is_file() {
                paths.push(session_config);
            }
        }
    }

    for relative_dir in [
        PathBuf::from("gate_messages"),
        PathBuf::from("intent_tokens"),
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
        value if value.starts_with("intent_tokens/") => format!(
            "Action tokens: {}",
            Path::new(value)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(value)
        ),
        value if value.starts_with("gate_messages/") => format!(
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
    if relative_path.starts_with("intent_tokens/") {
        return "Intent tokens".into();
    }
    if relative_path.starts_with("gate_messages/") {
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
    if relative_path.starts_with("intent_tokens/") {
        return "Tokens".into();
    }
    if relative_path.starts_with("gate_messages/") {
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
    if relative_path.starts_with("intent_tokens/") {
        return descriptor_enabled(target, enabled_languages);
    }
    if relative_path.starts_with("gate_messages/") {
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

    if relative_path.starts_with("intent_tokens/") {
        let connected = enabled_languages.unwrap_or("default");
        return format!("Intent classification tokens · project set {connected}");
    }
    if relative_path.starts_with("gate_messages/") {
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

    // Editability is decided by ONE authority (the Python CLI), the same
    // source the write path uses. Legacy TOMLs (config → SQLite, gate
    // messages / intent tokens → seed+SQLite) come back editable=false with
    // a deprecation reason; only dev-flavor canonical-source descriptors
    // are editable. Read-only call; on failure, fail closed (nothing
    // editable) so a stale path can never appear writable.
    let editability = {
        let mut a = vec![
            "dashboard-toml-editability".to_string(),
            project_root.to_string_lossy().to_string(),
            "--json".to_string(),
        ];
        if let Some(sid) = session_id.filter(|v| !v.trim().is_empty()) {
            a.push("--session".to_string());
            a.push(sid.to_string());
        }
        run_json_cli(project_root, &a)
            .ok()
            .and_then(|v| v.get("editability").cloned())
            .unwrap_or(Value::Null)
    };
    let editable_for = |rel: &str| -> (bool, String) {
        editability
            .get(rel)
            .map(|e| {
                (
                    e.get("editable").and_then(|b| b.as_bool()).unwrap_or(false),
                    e.get("deprecated")
                        .and_then(|s| s.as_str())
                        .unwrap_or("")
                        .to_string(),
                )
            })
            .unwrap_or((false, "Read-only: authority is not this TOML file.".into()))
    };

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
        let (editable, deprecated) = editable_for(&relative);
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
            editable,
            deprecated,
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
    let session = session_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());
    // Dashboard-war (c): the snapshot is the highest-frequency python call
    // (every live-cursor change + the 30s safety refresh). Serve it from the
    // ONE persistent worker instead of a fresh ~2.6s CLI cold-boot per call —
    // that spawn-per-snapshot loop WAS the python process storm. Any worker
    // failure falls back to the classic one-shot spawn below (and the worker
    // respawns on the next call), so this is strictly an optimization layer.
    if let Ok(v) = dash_worker_snapshot(&root, session) {
        return Ok(v);
    }
    let mut args = vec![
        "dashboard".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
    ];
    if let Some(session) = session {
        args.push("--session".to_string());
        args.push(session.to_string());
    }
    run_json_cli(&root, &args)
}

// ── Persistent dashboard snapshot worker (dashboard-war (c)) ─────────────
// One long-lived `aidocs_mcp.cli dashboard-worker` child serves snapshot
// requests over stdin/stdout (one JSON line each way). Protocol + failure
// semantics are pinned python-side in tests/runtime/test_dashboard_worker.py.

struct DashWorker {
    child: std::process::Child,
    stdin: std::process::ChildStdin,
    rx: std::sync::mpsc::Receiver<String>,
    next_id: u64,
}

static DASH_WORKER: std::sync::Mutex<Option<DashWorker>> = std::sync::Mutex::new(None);

fn spawn_dash_worker() -> Result<DashWorker, String> {
    // Pinned interpreter ONLY (120% §16): the worker runs the security
    // backend's runtime; a PATH-hijacked python must not become a resident
    // process. No host-PATH fallback here — fallback is the one-shot spawn.
    let py = resolve_mcp_python().ok_or("no pinned python for dashboard worker")?;
    let mut cmd = Command::new(&py);
    cmd.args(["-m", "aidocs_mcp.cli", "dashboard-worker"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let mut child = cmd.spawn().map_err(|e| format!("dashboard worker spawn: {e}"))?;
    let stdin = child.stdin.take().ok_or("dashboard worker: no stdin")?;
    let stdout = child.stdout.take().ok_or("dashboard worker: no stdout")?;
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    std::thread::spawn(move || {
        use std::io::BufRead;
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    Ok(DashWorker { child, stdin, rx, next_id: 1 })
}

fn kill_dash_worker(slot: &mut Option<DashWorker>) {
    if let Some(mut w) = slot.take() {
        let _ = w.child.kill();
        let _ = w.child.wait();
    }
}

fn dash_worker_snapshot(root: &Path, session_id: Option<&str>) -> Result<Value, String> {
    let mut guard = DASH_WORKER.lock().unwrap_or_else(|p| p.into_inner());
    if guard.is_none() {
        *guard = Some(spawn_dash_worker()?);
    }
    let (id, write_ok) = {
        let worker = guard.as_mut().expect("just ensured");
        let id = worker.next_id;
        worker.next_id += 1;
        let req = serde_json::json!({
            "id": id,
            "root": root.to_string_lossy(),
            "session_id": session_id,
        });
        use std::io::Write;
        let ok = writeln!(worker.stdin, "{req}")
            .and_then(|()| worker.stdin.flush())
            .is_ok();
        (id, ok)
    };
    if !write_ok {
        kill_dash_worker(&mut guard);
        return Err("dashboard worker write failed (will respawn)".into());
    }
    // 20s watchdog (matches the one-shot timebox): a wedged worker is killed
    // and the caller falls back to the classic spawn; next call respawns.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    loop {
        let now = std::time::Instant::now();
        if now >= deadline {
            kill_dash_worker(&mut guard);
            return Err("dashboard worker timed out (killed; will respawn)".into());
        }
        let recv = {
            let worker = guard.as_mut().expect("held above");
            worker.rx.recv_timeout(deadline - now)
        };
        match recv {
            Ok(line) => {
                let v: Value = match serde_json::from_str(&line) {
                    Ok(v) => v,
                    Err(_) => continue, // non-JSON noise on stdout — skip
                };
                if v.get("id").and_then(serde_json::Value::as_u64) != Some(id) {
                    continue; // stale answer from an earlier timed-out request
                }
                if v.get("ok") == Some(&Value::Bool(true)) {
                    return Ok(serde_json::json!({
                        "ok": true,
                        "snapshot": v.get("snapshot").cloned().unwrap_or(Value::Null),
                    }));
                }
                return Err(v
                    .get("error")
                    .and_then(|e| e.as_str())
                    .unwrap_or("dashboard worker error")
                    .to_string());
            }
            Err(_) => {
                kill_dash_worker(&mut guard);
                return Err("dashboard worker channel closed/timeout (will respawn)".into());
            }
        }
    }
}

// AUTHENTICATED LIVE APPROVE/REJECT (2026-05-26): the LivePage Pending
// Approvals panel previously rendered escalation rows but the Approve
// and Reject buttons had no onClick — clicking did nothing. These two
// Tauri commands wire the buttons to the canonical CLI surface
// (`aidocs admin approve-escalation` / `deny-escalation`) which in
// turn invokes the gate-enforced `rbac_approve_escalation` /
// `rbac_deny_escalation` MCP tools — same trust path as the headless
// CLI, no shortcut around RBAC. The `approver_email` is required by
// the CLI; the dashboard must prompt the operator (or look it up from
// snapshot.config.rbac.users) — never hardcode an identity here.

#[tauri::command]
fn approve_escalation(
    project_root: Option<String>,
    request_id: String,
    approver_email: String,
    reason: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let trimmed_request = request_id.trim();
    if trimmed_request.is_empty() {
        return Err("request_id is required".to_string());
    }
    let trimmed_email = approver_email.trim();
    if trimmed_email.is_empty() {
        return Err("approver_email is required".to_string());
    }
    let mut args = vec![
        "admin".to_string(),
        "approve-escalation".to_string(),
        trimmed_request.to_string(),
        "--approver-email".to_string(),
        trimmed_email.to_string(),
        "--json".to_string(),
    ];
    if let Some(reason) = reason {
        let trimmed_reason = reason.trim();
        if !trimmed_reason.is_empty() {
            args.push("--reason".to_string());
            args.push(trimmed_reason.to_string());
        }
    }
    // 2026-05-26 — admin-only CLI command: attach the operator token so
    // the CLI authorizes the bridge call AND use run_json_cli_checked
    // so a refused mutation surfaces as a UI error (test_auth_boundary_seal
    // contract). Defense-in-depth on top of the CLI's own
    // approver-email + rbac.has_permission check.
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn deny_escalation(
    project_root: Option<String>,
    request_id: String,
    approver_email: String,
    reason: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let trimmed_request = request_id.trim();
    if trimmed_request.is_empty() {
        return Err("request_id is required".to_string());
    }
    let trimmed_email = approver_email.trim();
    if trimmed_email.is_empty() {
        return Err("approver_email is required".to_string());
    }
    let mut args = vec![
        "admin".to_string(),
        "deny-escalation".to_string(),
        trimmed_request.to_string(),
        "--approver-email".to_string(),
        trimmed_email.to_string(),
        "--json".to_string(),
    ];
    if let Some(reason) = reason {
        let trimmed_reason = reason.trim();
        if !trimmed_reason.is_empty() {
            args.push("--reason".to_string());
            args.push(trimmed_reason.to_string());
        }
    }
    // 2026-05-26 — see approve_escalation comment; same contract.
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
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
    reason: Option<String>,
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
    // Operator reason from the T0 confirm dialog (audited via --reason).
    if let Some(r) = reason {
        if !r.trim().is_empty() {
            args.push("--reason".to_string());
            args.push(r);
        }
    }
    // Generic Settings/T0 saves use the SAME operator-token bridge as
    // Governed Bash / vocab / palace admin: attach the cached-or-minted
    // local token (solo/dev). In corpo, resolve returns None → no token
    // → the CLI returns blocked_by=operator_auth (login-required), so the
    // UI prompts login instead of failing opaquely. dashboard-set-config
    // requires an explicit token (no dev auto-mint), which is why the
    // bridge must supply it.
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn batch_config_settings(project_root: Option<String>, operations: Value) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let batch_json = serde_json::to_string(&operations).unwrap_or_else(|_| "[]".to_string());
    let mut args = vec![
        "dashboard-batch-config".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--batch".to_string(),
        batch_json,
    ];
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn delete_config_setting(
    project_root: Option<String>,
    setting_path: String,
    scope: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-delete-config".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--setting".to_string(),
        setting_path,
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
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
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
    // Local sanity (fast feedback) — the AUTHORITATIVE allowlist + write
    // lives in the authenticated CLI (`dashboard-save-toml`), so the
    // dashboard has ONE config write authority (OperatorAuthService), not a
    // separate Tauri fs::write side door.
    resolve_allowed_toml_path(&root, &relative_path, session_id.as_deref())?;

    // Stage content in a temp file (avoids argv limits) and let the CLI
    // re-validate the path against its allowlist, TOML-validate, write, and
    // audit under the operator-auth wall.
    let tmp = std::env::temp_dir().join(format!(
        "aidocs-toml-{}-{}.tmp",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
    ));
    fs::write(&tmp, &content)
        .map_err(|err| format!("Could not stage TOML content: {}", err))?;

    let mut args = vec![
        "dashboard-save-toml".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--relative".to_string(),
        relative_path.clone(),
        "--content-file".to_string(),
        tmp.to_string_lossy().to_string(),
    ];
    if let Some(sid) = session_id.as_deref().filter(|v| !v.trim().is_empty()) {
        args.push("--session".to_string());
        args.push(sid.to_string());
    }
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    let result = run_json_cli_checked(&root, &args);
    let _ = fs::remove_file(&tmp);
    result?;

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
fn toggle_skill(
    project_root: Option<String>,
    session_id: Option<String>,
    skill_id: String,
    enabled: bool,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let flag = if enabled { "--enable" } else { "--disable" };
    let mut args = vec![
        "dashboard-toggle-skill".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--skill".to_string(),
        skill_id,
        flag.to_string(),
    ];
    if let Some(session_id) = session_id.filter(|value| !value.is_empty()) {
        args.push("--session".to_string());
        args.push(session_id);
    }
    // Admin-only CLI command: attach the operator token and surface a
    // logical {"ok": false} (e.g. operator_auth) as an Err so the UI shows
    // a failure, not a false success.
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn delete_skill(
    project_root: Option<String>,
    session_id: Option<String>,
    skill_id: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-delete-skill".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--skill".to_string(),
        skill_id,
    ];
    if let Some(session_id) = session_id.filter(|value| !value.is_empty()) {
        args.push("--session".to_string());
        args.push(session_id);
    }
    // Admin-only CLI command: attach the operator token + checked envelope.
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn set_skill_provider_override(
    project_root: Option<String>,
    provider_id: String,
    choice: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let choice_text = choice.unwrap_or_default();
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.cli import _dashboard_runtime
root = Path(sys.argv[1])
provider_id = sys.argv[2]
choice = sys.argv[3].strip() if len(sys.argv) > 3 else ""
choice = choice or None
_, runtime = _dashboard_runtime()
result = runtime.set_skill_provider_override(root, provider_id, choice)
print(json.dumps({"ok": True, **result}))
"#;
    run_python_with_args(
        &root,
        script,
        &[&root.to_string_lossy(), &provider_id, &choice_text],
    )
}

#[tauri::command]
fn upload_skill(
    project_root: Option<String>,
    file_name: String,
    content: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let skill_dir = root.join(".MEMORY").join("skills");
    fs::create_dir_all(&skill_dir)
        .map_err(|err| format!("Failed to create skills directory: {}", err))?;
    let target = skill_dir.join(&file_name);
    fs::write(&target, &content).map_err(|err| format!("Failed to write skill file: {}", err))?;
    Ok(serde_json::json!({
        "ok": true,
        "path": target.to_string_lossy(),
        "message": format!("Uploaded skill: {}", file_name),
    }))
}

#[tauri::command]
fn select_skill_file() -> Result<Option<String>, String> {
    #[cfg(target_os = "windows")]
    {
        let output = Command::new("powershell")
            .args(["-NoProfile", "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.OpenFileDialog; $f.Filter = 'Markdown files (*.md)|*.md'; $f.Title = 'Select skill file'; if ($f.ShowDialog() -eq 'OK') { $f.FileName }"])
            .output()
            .map_err(|e| e.to_string())?;
        let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if path.is_empty() {
            return Ok(None);
        }
        return Ok(Some(path));
    }
    #[allow(unreachable_code)]
    Ok(None)
}

#[tauri::command]
fn import_skill_file(project_root: Option<String>, file_path: String) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let source = std::path::Path::new(&file_path);
    if !source.is_file() {
        return Err(format!("File not found: {}", file_path));
    }
    let file_name = source
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("skill.md");
    let skill_dir = root.join(".MEMORY").join("skills");
    fs::create_dir_all(&skill_dir)
        .map_err(|err| format!("Failed to create skills directory: {}", err))?;
    let target = skill_dir.join(file_name);
    fs::copy(source, &target).map_err(|err| format!("Failed to copy skill file: {}", err))?;
    Ok(serde_json::json!({
        "ok": true,
        "path": target.to_string_lossy(),
        "message": format!("Imported skill: {}", file_name),
    }))
}

#[tauri::command]
fn list_mcp_servers(project_root: Option<String>) -> Result<Value, String> {
    // SQL is the source of truth; .mcp.json is a regenerable projection.
    // List through the CLI registry read (`dashboard-mcp-config --action
    // list`) so the dashboard always shows canonical SQL state — a stale,
    // deleted, or corrupt .mcp.json can never hide or distort the registry.
    // This is a user-safe READ, so it uses run_json_cli (not _checked) and
    // attaches no operator token.
    let root = resolve_project_root(project_root)?;
    let cli_args = vec![
        "dashboard-mcp-list".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
    ];
    let listed = run_json_cli(&root, &cli_args)?;
    let mut result = Vec::new();
    if let Some(servers) = listed.get("servers").and_then(|v| v.as_array()) {
        for entry in servers {
            let name = entry.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let transport = entry
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("stdio");
            let command = entry.get("command").and_then(|v| v.as_str()).unwrap_or("");
            // Reconstruct a .mcp.json-shaped config object for UI parity with
            // the previous file-derived shape (the SQL row carries the same
            // fields the projection would).
            let config = serde_json::json!({
                "type": transport,
                "command": command,
                "args": entry.get("args").cloned().unwrap_or(Value::Array(vec![])),
            });
            result.push(serde_json::json!({
                "name": name,
                "transport": transport,
                "command": command,
                "config": config,
            }));
        }
    }
    Ok(serde_json::json!({ "ok": true, "servers": result }))
}

#[tauri::command]
fn install_mcp_server(
    project_root: Option<String>,
    name: String,
    command: String,
    args: Vec<String>,
    transport: Option<String>,
) -> Result<Value, String> {
    // An MCP server is a capability provider for the agent, so editing
    // .mcp.json is a control-plane mutation — route through the
    // authenticated CLI (`dashboard-mcp-config`), not a raw fs::write. The
    // CLI owns validation, the Windows cmd-wrap, the write, and the audit
    // under the operator-auth wall (solo/dev local-mintable; corpo login).
    let root = resolve_project_root(project_root)?;
    let args_json = serde_json::to_string(&args).unwrap_or_else(|_| "[]".into());
    let mut cli_args = vec![
        "dashboard-mcp-config".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--action".to_string(),
        "install".to_string(),
        "--name".to_string(),
        name,
        "--command".to_string(),
        command,
        "--args".to_string(),
        args_json,
        "--transport".to_string(),
        transport.unwrap_or_else(|| "stdio".to_string()),
    ];
    if let Some(token) = resolve_operator_token(&root) {
        cli_args.push("--operator-token".to_string());
        cli_args.push(token);
    }
    run_json_cli_checked(&root, &cli_args)
}

#[tauri::command]
fn delete_mcp_server(
    project_root: Option<String>,
    name: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut cli_args = vec![
        "dashboard-mcp-config".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--action".to_string(),
        "delete".to_string(),
        "--name".to_string(),
        name,
    ];
    if let Some(token) = resolve_operator_token(&root) {
        cli_args.push("--operator-token".to_string());
        cli_args.push(token);
    }
    run_json_cli_checked(&root, &cli_args)
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
        run_json_cli_checked(&root, &args)
    } else {
        let args = vec![
            "managed-mode-clear".to_string(),
            root.to_string_lossy().to_string(),
            "--json".to_string(),
        ];
        run_json_cli_checked(&root, &args)
    }
}

#[tauri::command]
fn metrics_snapshot() -> Result<Value, String> {
    run_python_metrics_snapshot()
}

fn run_python_memory_anchor_health(project_root: &Path) -> Result<Value, String> {
    // Cheap COUNT-only memory-anchor health (active/anchored/total/coverage/wire).
    // Off the ai_palace_status hot path by design — surfaced only in the dashboard.
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.memory_sqlite_store import memory_anchor_health
print(json.dumps({"ok": True, "health": memory_anchor_health(Path(sys.argv[1]))}))
"#;
    let root = project_root.to_string_lossy().to_string();
    let command_sets = vec![
        (
            "python",
            vec!["-c".to_string(), script.to_string(), root.clone()],
        ),
        ("py", vec!["-c".to_string(), script.to_string(), root]),
    ];
    run_python_json_command(command_sets, "Memory anchor health")
}

#[tauri::command]
fn memory_anchor_health(project_root: String) -> Result<Value, String> {
    run_python_memory_anchor_health(Path::new(&project_root))
}

#[tauri::command]
fn dashboard_live_cursor(project_root: String) -> Result<Value, String> {
    // Cheap change-detector for the dashboard's live state (execution events +
    // lane agents), read DIRECTLY from sqlite in Rust — NO python spawn. The
    // frontend polls this; only when the cursor CHANGES does it fetch the full
    // snapshot, so an idle dashboard no longer spawns cli-dashboard every 2s
    // (that was the process storm + the refresh stutter).
    let db = Path::new(&project_root)
        .join(".MEMORY")
        .join(".index")
        .join("aidocs.sqlite3");
    if !db.is_file() {
        return Ok(serde_json::json!({ "ok": true, "cursor": "nodb" }));
    }
    let conn = match rusqlite::Connection::open_with_flags(
        &db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) {
        Ok(c) => c,
        Err(e) => return Err(e.to_string()),
    };
    let one = |sql: &str| -> i64 {
        conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap_or(0)
    };
    let ev_max = one("SELECT COALESCE(MAX(rowid),0) FROM execution_events");
    let ev_cnt = one("SELECT COUNT(*) FROM execution_events");
    let lane_max = one("SELECT COALESCE(MAX(rowid),0) FROM session_lane_agents");
    let lane_active = one(
        "SELECT COUNT(*) FROM session_lane_agents WHERE state NOT IN \
         ('done','completed','failed','killed','exited')",
    );
    Ok(serde_json::json!({
        "ok": true,
        "cursor": format!("{ev_max}:{ev_cnt}:{lane_max}:{lane_active}")
    }))
}

// ── Memory knowledge-graph reads (dashboard-war (d), #200) ────────────────
// Pure rusqlite reads of the structured memory (memory_index + routes +
// keywords + anchors + links) — NO python spawn, same pattern as
// dashboard_live_cursor. Progressive disclosure: list is lean (memories +
// connection counts); expand(path) returns ONE node's branches.

fn memory_db(project_root: &str) -> Result<rusqlite::Connection, String> {
    let db = Path::new(project_root)
        .join(".MEMORY")
        .join(".index")
        .join("aidocs.sqlite3");
    if !db.is_file() {
        return Err("no memory index db".into());
    }
    rusqlite::Connection::open_with_flags(&db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn memory_kg_list(
    project_root: String,
    search: Option<String>,
    kind: Option<String>,
) -> Result<Value, String> {
    let conn = memory_db(&project_root)?;
    let needle = search.unwrap_or_default().trim().to_lowercase();
    let kind_filter = kind.unwrap_or_default().trim().to_string();
    let mut stmt = conn
        .prepare(
            "SELECT m.path, m.kind, COALESCE(m.title,'') AS title,
                    (SELECT COUNT(*) FROM memory_symbol_anchors a
                      WHERE a.drawer_id = 'memdrawer:' || m.path) AS anchor_count,
                    (SELECT COUNT(*) FROM memory_route_keywords k
                      JOIN memory_routes r ON r.route_id = k.route_id
                      WHERE r.target_path = m.path) AS keyword_count,
                    (SELECT COUNT(*) FROM memory_links l
                      WHERE l.source_path = m.path OR l.target_path = m.path) AS link_count
             FROM memory_index m
             WHERE (m.superseded_by IS NULL OR m.superseded_by = '')
               AND COALESCE(m.status,'active') = 'active'
             ORDER BY m.kind, m.path",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |r| {
            Ok(serde_json::json!({
                "path": r.get::<_, String>(0)?,
                "kind": r.get::<_, String>(1)?,
                "title": r.get::<_, String>(2)?,
                "anchor_count": r.get::<_, i64>(3)?,
                "keyword_count": r.get::<_, i64>(4)?,
                "link_count": r.get::<_, i64>(5)?,
            }))
        })
        .map_err(|e| e.to_string())?
        .filter_map(Result::ok)
        .filter(|v| {
            let kind_ok = kind_filter.is_empty()
                || v.get("kind").and_then(|k| k.as_str()) == Some(kind_filter.as_str());
            if !kind_ok {
                return false;
            }
            if needle.is_empty() {
                return true;
            }
            let hay = format!(
                "{} {}",
                v.get("path").and_then(|p| p.as_str()).unwrap_or(""),
                v.get("title").and_then(|t| t.as_str()).unwrap_or(""),
            )
            .to_lowercase();
            hay.contains(&needle)
        })
        .collect::<Vec<_>>();
    Ok(serde_json::json!({ "ok": true, "items": rows }))
}

#[tauri::command]
fn memory_kg_get(project_root: String, path: String) -> Result<Value, String> {
    let conn = memory_db(&project_root)?;
    conn.query_row(
        "SELECT path, kind, COALESCE(title,''), content,
                COALESCE(source,''), COALESCE(status,''), updated_at
         FROM memory_index WHERE path = ?1",
        [&path],
        |r| {
            Ok(serde_json::json!({
                "ok": true,
                "path": r.get::<_, String>(0)?,
                "kind": r.get::<_, String>(1)?,
                "title": r.get::<_, String>(2)?,
                "content": r.get::<_, String>(3)?,
                "source": r.get::<_, String>(4)?,
                "status": r.get::<_, String>(5)?,
                "updated_at": r.get::<_, String>(6)?,
            }))
        },
    )
    .map_err(|e| format!("memory not found: {e}"))
}

#[tauri::command]
fn memory_kg_expand(project_root: String, path: String) -> Result<Value, String> {
    let conn = memory_db(&project_root)?;
    let mut anchors: Vec<Value> = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT COALESCE(symbol_name,''), COALESCE(file_path,''),
                        COALESCE(confidence,'')
                 FROM memory_symbol_anchors WHERE drawer_id = 'memdrawer:' || ?1",
            )
            .map_err(|e| e.to_string())?;
        let it = stmt
            .query_map([&path], |r| {
                Ok(serde_json::json!({
                    "symbol": r.get::<_, String>(0)?,
                    "file": r.get::<_, String>(1)?,
                    "confidence": r.get::<_, String>(2)?,
                }))
            })
            .map_err(|e| e.to_string())?;
        anchors.extend(it.filter_map(Result::ok));
    }
    let mut keywords: Vec<String> = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT DISTINCT k.keyword FROM memory_route_keywords k
                 JOIN memory_routes r ON r.route_id = k.route_id
                 WHERE r.target_path = ?1 ORDER BY k.keyword",
            )
            .map_err(|e| e.to_string())?;
        let it = stmt
            .query_map([&path], |r| r.get::<_, String>(0))
            .map_err(|e| e.to_string())?;
        keywords.extend(it.filter_map(Result::ok));
    }
    // Linked memories (both directions), joined for title/kind so the UI can
    // grow the branch without a second round-trip per neighbor.
    let mut linked: Vec<Value> = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT DISTINCT other.path, other.kind, COALESCE(other.title,'')
                 FROM memory_links l
                 JOIN memory_index other ON other.path =
                   CASE WHEN l.source_path = ?1 THEN l.target_path ELSE l.source_path END
                 WHERE (l.source_path = ?1 OR l.target_path = ?1)
                   AND (other.superseded_by IS NULL OR other.superseded_by = '')",
            )
            .map_err(|e| e.to_string())?;
        let it = stmt
            .query_map([&path], |r| {
                Ok(serde_json::json!({
                    "path": r.get::<_, String>(0)?,
                    "kind": r.get::<_, String>(1)?,
                    "title": r.get::<_, String>(2)?,
                }))
            })
            .map_err(|e| e.to_string())?;
        linked.extend(it.filter_map(Result::ok));
    }
    Ok(serde_json::json!({
        "ok": true,
        "path": path,
        "anchors": anchors,
        "keywords": keywords,
        "linked": linked,
    }))
}

/// Full graph for the memory KG page — a faithful Rust port of the proven
/// extractor (scratch/memory_kg_export.py): memory nodes (config-file rows
/// excluded per the palace .aidocs allow-list policy), anchor edges via the
/// deterministic drawer id, keyword edges via memory_routes, memory<->memory
/// links. Shape matches the template: {nodes, edges, counts}.
#[tauri::command]
fn memory_kg_graph(project_root: String) -> Result<Value, String> {
    let conn = memory_db(&project_root)?;
    const CONFIG_ALLOW: [&str; 3] = [
        ".aidocs/coding-standards",
        ".aidocs/global-instructions",
        ".aidocs/memory-system",
    ];
    let is_config_memory = |path: &str, kind: &str| -> bool {
        if kind == "config" || kind == "aidocs" {
            return !CONFIG_ALLOW.iter().any(|a| path.starts_with(a));
        }
        path.ends_with(".toml")
    };
    let mut nodes: Vec<Value> = Vec::new();
    let mut node_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut edges: Vec<Value> = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT path, kind, COALESCE(title,'') FROM memory_index \
                 WHERE (superseded_by IS NULL OR superseded_by='') \
                   AND COALESCE(status,'active')='active'",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .map_err(|e| e.to_string())?;
        for row in rows.filter_map(Result::ok) {
            let (path, kind, title) = row;
            if is_config_memory(&path, &kind) {
                continue;
            }
            let id = format!("mem:{path}");
            let label = if title.is_empty() {
                path.rsplit('/').next().unwrap_or(&path).to_string()
            } else {
                title.clone()
            };
            node_ids.insert(id.clone());
            nodes.push(serde_json::json!({
                "id": id, "label": label, "group": kind, "type": "memory",
                "path": path, "kind": kind,
            }));
        }
    }
    {
        let mut stmt = conn
            .prepare(
                "SELECT COALESCE(drawer_id,''), COALESCE(symbol_name,''), \
                        COALESCE(file_path,''), COALESCE(confidence,'') \
                 FROM memory_symbol_anchors",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                ))
            })
            .map_err(|e| e.to_string())?;
        for (did, sym, fp, conf) in rows.filter_map(Result::ok) {
            let Some(mem) = did.strip_prefix("memdrawer:") else { continue };
            let mem_id = format!("mem:{mem}");
            if !node_ids.contains(&mem_id) {
                continue;
            }
            let key = if !sym.is_empty() && sym != "<file>" { sym.clone() } else { fp.clone() };
            if key.is_empty() {
                continue;
            }
            let uid = format!("unit:{key}|{fp}");
            if node_ids.insert(uid.clone()) {
                let label = if sym.is_empty() {
                    fp.rsplit('/').next().unwrap_or(&fp).to_string()
                } else {
                    sym.clone()
                };
                nodes.push(serde_json::json!({
                    "id": uid, "label": label, "group": "unit", "type": "unit",
                    "file": fp, "symbol": sym,
                }));
            }
            edges.push(serde_json::json!({
                "from": mem_id, "to": uid, "type": "anchor", "confidence": conf,
            }));
        }
    }
    {
        let mut stmt = conn
            .prepare(
                "SELECT r.target_path, k.keyword FROM memory_route_keywords k \
                 JOIN memory_routes r ON r.route_id = k.route_id",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| e.to_string())?;
        for (mem, kw) in rows.filter_map(Result::ok) {
            let kw = kw.trim().to_string();
            let mem_id = format!("mem:{mem}");
            if kw.is_empty() || !node_ids.contains(&mem_id) {
                continue;
            }
            let kid = format!("kw:{}", kw.to_lowercase());
            if node_ids.insert(kid.clone()) {
                nodes.push(serde_json::json!({
                    "id": kid, "label": kw, "group": "keyword", "type": "keyword",
                }));
            }
            edges.push(serde_json::json!({ "from": mem_id, "to": kid, "type": "keyword" }));
        }
    }
    {
        let mut stmt = conn
            .prepare("SELECT COALESCE(source_path,''), COALESCE(target_path,'') FROM memory_links")
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| e.to_string())?;
        for (s, t) in rows.filter_map(Result::ok) {
            let (sid, tid) = (format!("mem:{s}"), format!("mem:{t}"));
            if node_ids.contains(&sid) && node_ids.contains(&tid) {
                edges.push(serde_json::json!({ "from": sid, "to": tid, "type": "link" }));
            }
        }
    }
    let counts = serde_json::json!({
        "memories": nodes.iter().filter(|n| n["type"] == "memory").count(),
        "units": nodes.iter().filter(|n| n["type"] == "unit").count(),
        "keywords": nodes.iter().filter(|n| n["type"] == "keyword").count(),
        "edges": edges.len(),
    });
    Ok(serde_json::json!({ "ok": true, "nodes": nodes, "edges": edges, "counts": counts }))
}

use std::collections::HashMap;
use std::sync::Mutex;

type ConductorKey = (PathBuf, String);

struct ConductorState {
    process: Option<std::process::Child>,
    output: Vec<(f64, String, String)>,
    backend: Option<String>,
    model: Option<String>,
    opencode_port: Option<u16>,
    claude_session_id: Option<String>,
    codex_session_id: Option<String>,
    codex_send_in_flight: bool,
}

static CONDUCTORS: std::sync::LazyLock<Mutex<HashMap<ConductorKey, ConductorState>>> =
    std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));

fn make_conductor_key(root: &Path, session_id: &str) -> Result<ConductorKey, String> {
    let sid = session_id.trim();
    if sid.is_empty() {
        return Err("session_id is required".into());
    }
    Ok((root.to_path_buf(), sid.to_string()))
}

fn kill_conductor_state(state: &mut ConductorState) {
    if let Some(ref mut proc) = state.process {
        let _ = proc.stdin.take();
        let _ = proc.kill();
        let _ = proc.wait();
    }
    state.process = None;
    state.backend = None;
    state.model = None;
    state.opencode_port = None;
    state.claude_session_id = None;
    state.codex_session_id = None;
    state.codex_send_in_flight = false;
}

fn stop_all_conductors() {
    if let Ok(mut map) = CONDUCTORS.lock() {
        for state in map.values_mut() {
            kill_conductor_state(state);
        }
    }
}

#[tauri::command]
fn conductor_start(
    project_root: Option<String>,
    session_id: String,
    backend: Option<String>,
    model: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let key = make_conductor_key(&root, &session_id)?;
    let backend_str = backend.unwrap_or_else(|| "claude".into());
    let model_str = model.unwrap_or_default();
    let cli_name = match backend_str.as_str() {
        "claude" => "claude",
        "codex" => "codex",
        "opencode" => "opencode",
        other => return Err(format!("Unknown backend: {other}")),
    };
    let cli = which(cli_name).map_err(|_| format!("{cli_name} CLI not found"))?;

    {
        let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
        if let Some(state) = map.get_mut(&key) {
            kill_conductor_state(state);
            state.output.clear();
        }
    }

    if backend_str == "codex" {
        let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
        let entry = map.entry(key.clone()).or_insert_with(|| ConductorState {
            process: None,
            output: Vec::new(),
            backend: None,
            model: None,
            opencode_port: None,
            claude_session_id: None,
            codex_session_id: None,
            codex_send_in_flight: false,
        });
        entry.process = None;
        entry.backend = Some(backend_str.clone());
        entry.model = if model_str.trim().is_empty() {
            None
        } else {
            Some(model_str.clone())
        };
        entry.opencode_port = None;
        entry.claude_session_id = None;
        entry.codex_session_id = None;
        entry.codex_send_in_flight = false;
        drop(map);
        return Ok(serde_json::json!({
            "started": true,
            "backend": backend_str,
            "model": if model_str.trim().is_empty() { Value::Null } else { Value::String(model_str) },
            "project_root": root.to_string_lossy(),
            "session_id": session_id,
        }));
    }

    #[cfg(target_os = "windows")]
    let mut cmd = {
        let ext = cli
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_ascii_lowercase);
        match ext.as_deref() {
            Some("cmd") | Some("bat") => {
                let mut c = std::process::Command::new("cmd");
                c.arg("/C").arg(&cli);
                c
            }
            _ => std::process::Command::new(&cli),
        }
    };
    #[cfg(not(target_os = "windows"))]
    let mut cmd = std::process::Command::new(&cli);

    let mut opencode_port: Option<u16> = None;
    if backend_str == "claude" {
        // Long-lived programmatic chat: --print + stream-json both ways keeps
        // the process alive across many user messages. --verbose is required
        // by --output-format stream-json.
        cmd.arg("--print")
            .arg("--input-format")
            .arg("stream-json")
            .arg("--output-format")
            .arg("stream-json")
            .arg("--verbose");
        let project_name = root
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("project");
        let identity_prompt = format!(
            "You are the AIDOCS conductor for project '{project_name}' at '{root_display}'. The user is working in AIDOCS session '{session_id}'. Use mcp__aidocs__ai_session(mode='list') to enumerate sessions and mcp__aidocs__ai_session(mode='resume') to inspect them; never glob /.MEMORY/sessions/. When the user asks about the current project or session, answer with the identity above.",
            project_name = project_name,
            root_display = root.to_string_lossy(),
            session_id = session_id,
        );
        cmd.arg("--append-system-prompt").arg(&identity_prompt);
        if !model_str.trim().is_empty() {
            cmd.arg("--model").arg(model_str.trim());
        }
    } else {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")
            .map_err(|e| format!("Failed to allocate OpenCode port: {e}"))?;
        let port = listener
            .local_addr()
            .map_err(|e| format!("Failed to read OpenCode port: {e}"))?
            .port();
        drop(listener);
        cmd.arg("serve").arg("--port").arg(port.to_string());
        opencode_port = Some(port);
    }
    cmd.current_dir(&root)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Failed to spawn conductor: {e}"))?;

    // Insert the fresh state BEFORE spawning reader threads so the first
    // output line finds the entry.
    {
        let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
        let entry = map.entry(key.clone()).or_insert_with(|| ConductorState {
            process: None,
            output: Vec::new(),
            backend: None,
            model: None,
            opencode_port: None,
            claude_session_id: None,
            codex_session_id: None,
            codex_send_in_flight: false,
        });
        entry.process = None;
        entry.backend = Some(backend_str.clone());
        entry.model = if model_str.trim().is_empty() {
            None
        } else {
            Some(model_str.clone())
        };
        entry.opencode_port = opencode_port;
        entry.claude_session_id = None;
        entry.codex_session_id = None;
        entry.codex_send_in_flight = false;
    }
    spawn_conductor_output_readers(&mut child, &backend_str, key.clone());
    {
        let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
        if let Some(entry) = map.get_mut(&key) {
            entry.process = Some(child);
        }
    }

    Ok(serde_json::json!({
        "started": true,
        "backend": backend_str,
        "model": if model_str.trim().is_empty() { Value::Null } else { Value::String(model_str) },
        "project_root": root.to_string_lossy(),
        "session_id": session_id,
    }))
}

#[tauri::command]
fn conductor_send(
    project_root: Option<String>,
    session_id: String,
    message: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let key = make_conductor_key(&root, &session_id)?;

    let backend: String;
    let model: String;
    let codex_session: Option<String>;
    let opencode_port_opt: Option<u16>;
    {
        let map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
        let state = map
            .get(&key)
            .ok_or("No conductor running for this session")?;
        backend = state.backend.clone().unwrap_or_else(|| "claude".into());
        model = state.model.clone().unwrap_or_default();
        codex_session = state.codex_session_id.clone();
        opencode_port_opt = state.opencode_port;
    }

    if backend == "codex" {
        return conductor_send_codex(&root, &key, &model, codex_session, message);
    }

    let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
    let state = map
        .get_mut(&key)
        .ok_or("No conductor running for this session")?;
    let proc = state
        .process
        .as_mut()
        .ok_or("No conductor running for this session")?;
    if proc.try_wait().map_err(|e| e.to_string())?.is_some() {
        kill_conductor_state(state);
        return Err("Conductor process has exited".into());
    }
    if backend == "opencode" {
        let oc_port = opencode_port_opt.ok_or("OpenCode port not available")?;
        drop(map);
        let oc_cli = which("opencode").map_err(|_| "opencode CLI not found".to_string())?;
        let mut cmd = Command::new(oc_cli);
        cmd.arg("run");
        if !model.trim().is_empty() {
            cmd.arg("--model").arg(model.trim());
        }
        cmd.arg("--attach")
            .arg(format!("http://localhost:{oc_port}"))
            .arg(&message);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000);
        }
        let output = cmd
            .output()
            .map_err(|e| format!("Failed to send to OpenCode: {e}"))?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(if stderr.is_empty() {
                "OpenCode send failed".into()
            } else {
                stderr
            });
        }
        let stdout = String::from_utf8_lossy(&output.stdout).to_string();
        push_conductor_output(&key, "stdout", stdout.clone());
        return Ok(serde_json::json!({ "sent": true, "output": stdout }));
    }
    if let Some(ref mut stdin) = proc.stdin {
        use std::io::Write;
        let envelope = serde_json::json!({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": message}
                ]
            }
        });
        let line = format!("{envelope}\n");
        stdin
            .write_all(line.as_bytes())
            .map_err(|e| format!("Failed to send: {e}"))?;
        stdin.flush().map_err(|e| format!("Failed to flush: {e}"))?;
    }
    Ok(serde_json::json!({ "sent": true }))
}

fn conductor_send_codex(
    root: &Path,
    key: &ConductorKey,
    model: &str,
    codex_session: Option<String>,
    message: String,
) -> Result<Value, String> {
    {
        let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
        let state = map
            .get_mut(key)
            .ok_or("No conductor running for this session")?;
        if state.codex_send_in_flight {
            return Err("A codex turn is already in flight for this session. Wait for it to finish before sending again.".into());
        }
        state.codex_send_in_flight = true;
    }

    let result = run_codex_exec(root, key, model, codex_session.as_deref(), &message);

    {
        if let Ok(mut map) = CONDUCTORS.lock() {
            if let Some(state) = map.get_mut(key) {
                state.codex_send_in_flight = false;
            }
        }
    }

    result
}

fn run_codex_exec(
    root: &Path,
    key: &ConductorKey,
    model: &str,
    codex_session: Option<&str>,
    message: &str,
) -> Result<Value, String> {
    let codex_cli = which("codex").map_err(|_| "codex CLI not found".to_string())?;
    let mut cmd = Command::new(&codex_cli);
    cmd.arg("exec");
    if let Some(sid) = codex_session {
        cmd.arg("resume").arg(sid);
    }
    cmd.arg("--json");
    if !model.trim().is_empty() {
        cmd.arg("-m").arg(model.trim());
    }
    cmd.arg("--skip-git-repo-check").arg(message);
    cmd.current_dir(root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Failed to spawn codex exec: {e}"))?;

    let stdout_key = key.clone();
    let stdout_thread = child.stdout.take().map(|stdout| {
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().flatten() {
                handle_codex_stream_event(&stdout_key, &line);
            }
        })
    });

    let stderr_key = key.clone();
    let stderr_thread = child.stderr.take().map(|stderr| {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().flatten() {
                if line.contains(" ERROR ") || line.contains(" WARN ") {
                    push_conductor_output(&stderr_key, "stderr", line);
                }
            }
        })
    });

    let status = child
        .wait()
        .map_err(|e| format!("Failed to wait for codex: {e}"))?;
    if let Some(t) = stdout_thread {
        let _ = t.join();
    }
    if let Some(t) = stderr_thread {
        let _ = t.join();
    }

    let captured_session = CONDUCTORS
        .lock()
        .ok()
        .and_then(|map| map.get(key).and_then(|s| s.codex_session_id.clone()));

    if !status.success() {
        return Err(format!(
            "codex exec exited with code {}",
            status.code().map(|c| c.to_string()).unwrap_or_else(|| "?".into())
        ));
    }

    if codex_session.is_none() && captured_session.is_none() {
        return Err("codex exec did not emit thread.started; cannot persist session for resume".into());
    }

    Ok(serde_json::json!({
        "sent": true,
        "codex_session_id": captured_session,
    }))
}

#[tauri::command]
fn conductor_status(
    project_root: Option<String>,
    session_id: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let key = make_conductor_key(&root, &session_id)?;
    let map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
    match map.get(&key) {
        Some(state) => {
            let backend_str = state.backend.as_deref().unwrap_or("");
            let running = if backend_str == "codex" {
                state.backend.is_some()
            } else {
                state.process.is_some()
            };
            Ok(serde_json::json!({
                "running": running,
                "backend": state.backend,
                "model": state.model,
                "session_id": session_id,
                "claude_session_id": state.claude_session_id,
                "codex_session_id": state.codex_session_id,
                "codex_send_in_flight": state.codex_send_in_flight,
            }))
        }
        None => Ok(serde_json::json!({
            "running": false,
            "backend": Value::Null,
            "model": Value::Null,
            "session_id": session_id,
            "claude_session_id": Value::Null,
            "codex_session_id": Value::Null,
            "codex_send_in_flight": false,
        })),
    }
}

#[tauri::command]
fn conductor_stop(
    project_root: Option<String>,
    session_id: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let key = make_conductor_key(&root, &session_id)?;
    let mut map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
    if let Some(state) = map.get_mut(&key) {
        kill_conductor_state(state);
    }
    Ok(serde_json::json!({ "stopped": true }))
}

#[tauri::command]
fn conductor_output(
    project_root: Option<String>,
    session_id: String,
    since: Option<f64>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let key = make_conductor_key(&root, &session_id)?;
    let map = CONDUCTORS.lock().map_err(|e| e.to_string())?;
    match map.get(&key) {
        Some(state) => {
            let since_ts = since.unwrap_or(0.0);
            let lines: Vec<Value> = state
                .output
                .iter()
                .filter(|(ts, _, _)| *ts > since_ts)
                .map(|(ts, stream, text)| {
                    serde_json::json!({
                        "timestamp": ts,
                        "stream": stream,
                        "text": text,
                    })
                })
                .collect();
            Ok(serde_json::json!({
                "running": state.process.is_some(),
                "lines": lines,
                "total_buffered": state.output.len(),
            }))
        }
        None => Ok(serde_json::json!({
            "running": false,
            "lines": [],
            "total_buffered": 0,
        })),
    }
}

#[tauri::command]
fn opencode_models() -> Result<Value, String> {
    let cli = which("opencode").map_err(|_| "opencode CLI not found".to_string())?;
    let mut cmd = Command::new(cli);
    cmd.arg("models");
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    let output = cmd
        .output()
        .map_err(|e| format!("Failed to query OpenCode models: {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if stderr.is_empty() {
            "OpenCode models command failed".into()
        } else {
            stderr
        });
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let models: Vec<String> = stdout
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| line.to_string())
        .collect();
    Ok(serde_json::json!({ "ok": true, "models": models }))
}

fn which(name: &str) -> Result<PathBuf, String> {
    let cmd = if cfg!(windows) { "where" } else { "which" };

    let mut which_cmd = std::process::Command::new(cmd);
    which_cmd.arg(name);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        which_cmd.creation_flags(0x08000000);
    }
    let output = which_cmd.output().map_err(|e| e.to_string())?;
    if output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let candidates: Vec<&str> = stdout
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .collect();
        #[cfg(target_os = "windows")]
        {
            // `where` returns both extensionless shims and `.cmd`/`.exe` siblings.
            // CreateProcess only executes true PE binaries or script shims it can
            // resolve via PATHEXT, so prefer an extension Windows can launch.
            let priority = ["exe", "cmd", "bat", "com"];
            for ext in priority {
                if let Some(hit) = candidates.iter().find(|p| {
                    std::path::Path::new(p)
                        .extension()
                        .and_then(|e| e.to_str())
                        .map(|e| e.eq_ignore_ascii_case(ext))
                        .unwrap_or(false)
                }) {
                    return Ok(PathBuf::from(hit));
                }
            }
        }
        if let Some(first) = candidates.first() {
            return Ok(PathBuf::from(first));
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
fn execution_clear_tokens(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
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
fn execution_clear_tool_calls(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<Value, String> {
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

// SEC-005 (2026-04-23): Clear the session's degraded_state flag.
// Called from the dashboard's "Clear State" button in the degraded
// strip. Wipes reason + failure_event_id too so stale text doesn't
// linger after recovery. Reset-only — does not retry or reconnect;
// those are separate buttons.
#[tauri::command]
fn clear_degraded_state(
    project_root: Option<String>,
    session_id: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.query_gate import QueryGateStore
root = Path(sys.argv[1])
sid = sys.argv[2]
QueryGateStore().clear_degraded_state(root, sid)
state = QueryGateStore().get_degraded_state(root, sid)
print(json.dumps({"ok": True, "cleared": "degraded_state", "state": state}))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &session_id])
}

#[tauri::command]
fn execution_prune_events(
    project_root: Option<String>,
    keep_days: Option<i32>,
    max_events: Option<i32>,
) -> Result<Value, String> {
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
    run_python_with_args(
        &root,
        script,
        &[
            &root.to_string_lossy(),
            &days.to_string(),
            &max_ev.to_string(),
        ],
    )
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

// Message substrate (slice B1). The dashboard speaks as `king` and
// may target `conductor`, `co-conductor`, or both. Project-scoped
// store today, so session_id is accepted for symmetry / future use
// only. Phoenix 2026-05-12: renamed from cerberus_* (king directive).
#[tauri::command]
fn tauri_msg_send(
    project_root: Option<String>,
    session_id: Option<String>,
    to_roles: Vec<String>,
    body: String,
    in_reply_to: Option<String>,
) -> Result<Value, String> {
    let _ = session_id;
    let root = resolve_project_root(project_root)?;
    let to_roles_json = serde_json::to_string(&to_roles).map_err(|e| e.to_string())?;
    let in_reply_to = in_reply_to.unwrap_or_default();
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.conductor_comms import msg_send
root = Path(sys.argv[1])
to_roles = json.loads(sys.argv[2])
body = sys.argv[3]
in_reply_to = sys.argv[4] if len(sys.argv) > 4 else ""
result = msg_send(root, from_role="king", to_roles=to_roles, body=body, in_reply_to=in_reply_to)
print(json.dumps({"ok": True, **result}))
"#;
    run_python_with_args(
        &root,
        script,
        &[&root.to_string_lossy(), &to_roles_json, &body, &in_reply_to],
    )
}

#[tauri::command]
fn tauri_msg_inbox(
    project_root: Option<String>,
    session_id: Option<String>,
    role: String,
) -> Result<Value, String> {
    let _ = session_id;
    let root = resolve_project_root(project_root)?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp.conductor_comms import msg_inbox
root = Path(sys.argv[1])
role = sys.argv[2]
messages = msg_inbox(root, role=role, unread_only=True, mark_read=True)
print(json.dumps({"ok": True, "messages": messages}))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &role])
}

#[tauri::command]
fn delete_session(project_root: Option<String>, session_id: String) -> Result<Value, String> {
    // Route through the GATED dashboard CLI (operator-token + RBAC +
    // session_deletion_law: checkpoint/quarantine, not raw rmtree) — same
    // bridge as create/connect. run_json_cli (not _checked) so a REFUSAL
    // ({ok:false, blocked_by:operator_auth}) flows to the UI as structured
    // data instead of a thrown error, letting the shared authority notice
    // render it. Replaces the prior ungated inline filesystem delete.
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-delete-session".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--session".to_string(),
        session_id,
    ];
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
fn create_session(
    project_root: Option<String>,
    title: String,
    session_id: Option<String>,
    goal: Option<String>,
) -> Result<Value, String> {
    // Route through the GATED CLI (operator-token + RBAC) so the result
    // carries the control-plane authority truth (owner_grant /
    // ownership_degraded / blocked_by) — same bridge as save_config_setting.
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-create-session".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--title".to_string(),
        title,
    ];
    if let Some(s) = session_id {
        if !s.trim().is_empty() {
            args.push("--session".to_string());
            args.push(s);
        }
    }
    if let Some(g) = goal {
        if !g.trim().is_empty() {
            args.push("--goal".to_string());
            args.push(g);
        }
    }
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
fn connect_session(
    project_root: Option<String>,
    session_id: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-connect-session".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--session".to_string(),
        session_id,
    ];
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli(&root, &args)
}

// ─── ai_backlog / ai_todo bridges (Phoenix backlog-todo dashboard) ───
// Tauri shim around project_backlog_store + task_todos_store. Mirrors
// the msg_send pattern: kwargs marshalled as JSON, Python script
// loads the store module and calls the matching function directly.

#[tauri::command]
fn tauri_backlog_list(
    project_root: Option<String>,
    status: Option<String>,
    priority: Option<String>,
    limit: Option<i64>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let kwargs = serde_json::json!({
        "status": status,
        "priority": priority,
        "limit": limit,
    });
    let kwargs_s = serde_json::to_string(&kwargs).map_err(|e| e.to_string())?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import project_backlog_store
root = Path(sys.argv[1])
kw = {k: v for k, v in json.loads(sys.argv[2]).items() if v is not None}
items = project_backlog_store.list_backlog(root, **kw)
print(json.dumps({"ok": True, "items": items}, default=str))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &kwargs_s])
}

#[tauri::command]
fn tauri_backlog_get(
    project_root: Option<String>,
    backlog_id: i64,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import project_backlog_store
root = Path(sys.argv[1])
bid = int(sys.argv[2])
row = project_backlog_store.get_by_id(root, backlog_id=bid)
print(json.dumps({"ok": True, "item": row}, default=str))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &backlog_id.to_string()])
}

#[tauri::command]
fn tauri_backlog_add(
    project_root: Option<String>,
    content: String,
    priority: Option<String>,
    status: Option<String>,
    tags: Option<Vec<String>>,
    session_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let kwargs = serde_json::json!({
        "content": content,
        "priority": priority,
        "status": status,
        "tags": tags,
        "session_id": session_id,
    });
    let kwargs_s = serde_json::to_string(&kwargs).map_err(|e| e.to_string())?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import project_backlog_store
root = Path(sys.argv[1])
kw = {k: v for k, v in json.loads(sys.argv[2]).items() if v is not None}
r = project_backlog_store.add(root, **kw)
print(json.dumps({"ok": True, "result": r}, default=str))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &kwargs_s])
}

#[tauri::command]
fn tauri_backlog_update(
    project_root: Option<String>,
    backlog_id: i64,
    status: Option<String>,
    priority: Option<String>,
    content: Option<String>,
    tags: Option<Vec<String>>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let kwargs = serde_json::json!({
        "backlog_id": backlog_id,
        "status": status,
        "priority": priority,
        "content": content,
        "tags": tags,
    });
    let kwargs_s = serde_json::to_string(&kwargs).map_err(|e| e.to_string())?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import project_backlog_store
root = Path(sys.argv[1])
kw = {k: v for k, v in json.loads(sys.argv[2]).items() if v is not None}
r = project_backlog_store.update(root, **kw)
print(json.dumps({"ok": True, "result": r}, default=str))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &kwargs_s])
}

#[tauri::command]
fn tauri_backlog_remove(
    project_root: Option<String>,
    backlog_id: i64,
    reason: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import project_backlog_store
root = Path(sys.argv[1])
bid = int(sys.argv[2])
reason = sys.argv[3]
r = project_backlog_store.remove(root, backlog_id=bid, reason=reason)
print(json.dumps({"ok": True, "result": r}, default=str))
"#;
    run_python_with_args(
        &root,
        script,
        &[&root.to_string_lossy(), &backlog_id.to_string(), &reason],
    )
}

#[tauri::command]
fn tauri_todo_list(
    project_root: Option<String>,
    session_id: Option<String>,
    task_id: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let kwargs = serde_json::json!({
        "session_id": session_id,
        "task_id": task_id,
    });
    let kwargs_s = serde_json::to_string(&kwargs).map_err(|e| e.to_string())?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import task_todos_store
root = Path(sys.argv[1])
kw = json.loads(sys.argv[2])
task_id = kw.get("task_id")
session_id = kw.get("session_id")
if task_id:
    items = task_todos_store.list_for_task(root, task_id=task_id)
elif session_id:
    items = task_todos_store.list_for_session_unresolved(root, session_id=session_id)
else:
    items = []
print(json.dumps({"ok": True, "items": items}, default=str))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &kwargs_s])
}

#[tauri::command]
fn tauri_todo_update(
    project_root: Option<String>,
    todo_id: i64,
    status: Option<String>,
    content: Option<String>,
    tags: Option<Vec<String>>,
    task_id: Option<String>,
    session_id: Option<String>,
    urgency: Option<String>,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let kwargs = serde_json::json!({
        "todo_id": todo_id,
        "status": status,
        "content": content,
        "tags": tags,
        "task_id": task_id,
        "session_id": session_id,
        "urgency": urgency,
    });
    let kwargs_s = serde_json::to_string(&kwargs).map_err(|e| e.to_string())?;
    // task_todos_store.update REQUIRES task_id (ownership); the operator
    // dashboard passes the row's own task_id (always the legitimate owner)
    // and session_id (same-session escape, todo 91) when known.
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import task_todos_store
root = Path(sys.argv[1])
kw = {k: v for k, v in json.loads(sys.argv[2]).items() if v is not None}
kw.setdefault("task_id", "")
r = task_todos_store.update(root, **kw)
print(json.dumps({"ok": True, "result": r}, default=str))
"#;
    run_python_with_args(&root, script, &[&root.to_string_lossy(), &kwargs_s])
}

#[tauri::command]
fn tauri_todo_remove(
    project_root: Option<String>,
    todo_id: i64,
    reason: String,
) -> Result<Value, String> {
    let root = resolve_project_root(project_root)?;
    let script = r#"
import json, sys
from pathlib import Path
from aidocs_mcp import task_todos_store
root = Path(sys.argv[1])
tid = int(sys.argv[2])
reason = sys.argv[3]
r = task_todos_store.remove(root, todo_id=tid, reason=reason)
print(json.dumps({"ok": True, "result": r}, default=str))
"#;
    run_python_with_args(
        &root,
        script,
        &[&root.to_string_lossy(), &todo_id.to_string(), &reason],
    )
}

fn run_python_with_args(
    _project_root: &Path,
    script: &str,
    args: &[&str],
) -> Result<Value, String> {
    // Smarter/safer (2026-07-02): pinned interpreter FIRST (120% §16 — same
    // contract as run_json_cli; host PATH only as bootstrap fallback), and
    // hold SPAWN_GATE so these spawns can't fan out concurrently with the
    // CLI spawns (the 2026-06-30 storm class).
    let _spawn_gate = SPAWN_GATE.lock().unwrap_or_else(|p| p.into_inner());
    let mut programs: Vec<String> = Vec::new();
    if let Some(py) = resolve_mcp_python() {
        programs.push(py);
    }
    programs.push("python".to_string());
    programs.push("py".to_string());
    for program in programs {
        let mut cmd_args: Vec<String> = vec!["-c".to_string(), script.to_string()];
        cmd_args.extend(args.iter().map(|a| a.to_string()));
        let mut cmd = Command::new(program);
        cmd.args(&cmd_args);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let output = cmd.output();
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

fn resolve_mcp_python() -> Option<String> {
    // Security (120% §16 supply chain): the PINNED python that ships with AIDOCS
    // (find_bundled_python) comes FIRST; host PATH is only a bootstrap fallback
    // (before the bundled python is installed), never the preferred interpreter
    // for sensitive CLI calls a PATH-hijack/shim could otherwise substitute.
    find_bundled_python().or_else(find_python_path)
}

// ── Empire intent-tokens store · Phase 4b dashboard backend ──
// Direct rusqlite reads/writes to the empire intent vocabulary DB.
// Mirrors mcp/server/aidocs_mcp/intent_tokens_store.py — keep kinds
// in sync. Backs the new schema-aware React panels that replace the
// file-shaped TOML editor for intent_tokens/ + gate_messages/.

fn empire_db_path_rs() -> PathBuf {
    if let Ok(override_path) = std::env::var("AIDOCS_EMPIRE_DB") {
        if !override_path.trim().is_empty() {
            return PathBuf::from(override_path);
        }
    }
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    PathBuf::from(home).join(".aidocs").join("empire.sqlite3")
}

#[derive(Serialize)]
struct VocabGroup {
    parent_key: String,
    parent_mode: String,
    tokens: Vec<String>,
    attrs: serde_json::Value,
}

#[tauri::command]
fn vocab_list_kinds() -> Result<serde_json::Value, String> {
    // Keep in sync with intent_tokens_store._LEMMA_KINDS.
    Ok(serde_json::json!({
        "kinds": [
            "approve_verb", "deny_verb",
            "scopeless_accept", "scopeless_deny",
            "second_person", "first_person", "negation",
            "tool_alias", "domain_alias",
            "action_token", "intent_guard", "plan_vague_pattern",
            "intent_phrase", "skill_trigger", "domain_hint",
            "memory_route", "tool_discovery"
        ]
    }))
}

#[tauri::command]
fn vocab_list_langs() -> Result<serde_json::Value, String> {
    let db = empire_db_path_rs();
    if !db.is_file() {
        return Ok(serde_json::json!({"langs": []}));
    }
    let conn = rusqlite::Connection::open_with_flags(
        &db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare("SELECT DISTINCT lang FROM intent_lemma_sets ORDER BY lang")
        .map_err(|e| e.to_string())?;
    let langs: Vec<String> = stmt
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(|e| e.to_string())?
        .filter_map(Result::ok)
        .collect();
    Ok(serde_json::json!({"langs": langs}))
}

#[tauri::command]
fn vocab_get_grouped(
    kind: String,
    lang: String,
) -> Result<serde_json::Value, String> {
    let db = empire_db_path_rs();
    if !db.is_file() {
        return Ok(serde_json::json!({
            "groups": {}, "kind": kind, "lang": lang
        }));
    }
    let conn = rusqlite::Connection::open_with_flags(
        &db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare(
            "SELECT parent_key, parent_mode, token, attrs \
             FROM intent_lemma_sets \
             WHERE lang = ? AND kind = ? \
             ORDER BY parent_key, token",
        )
        .map_err(|e| e.to_string())?;
    let mut groups: std::collections::BTreeMap<String, VocabGroup> =
        Default::default();
    let rows = stmt
        .query_map(rusqlite::params![lang, kind], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })
        .map_err(|e| e.to_string())?;
    for row in rows.filter_map(Result::ok) {
        let (pk, pm, tok, attrs_json) = row;
        let group_key = if pm.is_empty() {
            pk.clone()
        } else {
            format!("{}::{}", pk, pm)
        };
        let entry = groups.entry(group_key).or_insert_with(|| {
            let attrs = serde_json::from_str::<serde_json::Value>(&attrs_json)
                .unwrap_or_else(|_| serde_json::json!({}));
            VocabGroup {
                parent_key: pk.clone(),
                parent_mode: pm.clone(),
                tokens: Vec::new(),
                attrs,
            }
        });
        if !tok.is_empty() && !entry.tokens.contains(&tok) {
            entry.tokens.push(tok);
        }
    }
    Ok(serde_json::json!({
        "groups": groups, "kind": kind, "lang": lang
    }))
}

#[tauri::command]
fn vocab_upsert_group(
    project_root: Option<String>,
    kind: String,
    lang: String,
    parent_key: String,
    tokens: Vec<String>,
    attrs: Option<serde_json::Value>,
    parent_mode: Option<String>,
) -> Result<serde_json::Value, String> {
    if kind.is_empty() || parent_key.is_empty() {
        return Err("kind + parent_key required".into());
    }
    // Control-plane auth wall: route through the authenticated
    // dashboard-vocab-set CLI subcommand instead of writing sqlite
    // directly. Requires an operator token (admin.manage_config).
    let root = resolve_project_root(project_root)?;
    let token = resolve_operator_token(&root)
        .ok_or_else(|| "operator_auth: no operator token (vocab upsert is admin-only)".to_string())?;
    let pm = parent_mode.unwrap_or_default();
    let row = serde_json::json!([{
        "parent_key": parent_key,
        "parent_mode": pm,
        "tokens": tokens,
        "attrs": attrs.unwrap_or_else(|| serde_json::json!({})),
    }]);
    let args = vec![
        "dashboard-vocab-set".to_string(),
        root.to_string_lossy().to_string(),
        "--kind".to_string(), kind,
        "--lang".to_string(), lang,
        "--rows".to_string(), row.to_string(),
        "--replace".to_string(),
        "--operator-token".to_string(), token,
        "--json".to_string(),
    ];
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn palace_maintenance(
    project_root: Option<String>,
    mode: Option<String>,
    dry_run: Option<bool>,
    force: Option<bool>,
    session_id: Option<String>,
) -> Result<serde_json::Value, String> {
    // Guarded MemPalace maintenance — authenticated dashboard admin only.
    // Attach the cached operator token (minted in solo/dev, present after
    // dashboard login in corpo). Without a token the CLI returns
    // blocked_by=operator_auth + login_required, so the UI can prompt login
    // rather than erroring here.
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "dashboard-palace-maintenance".to_string(),
        root.to_string_lossy().to_string(),
        "--mode".to_string(),
        mode.unwrap_or_else(|| "backfill_legacy_memory_drawers".to_string()),
        "--json".to_string(),
    ];
    if dry_run.unwrap_or(false) {
        args.push("--dry-run".to_string());
    }
    if force.unwrap_or(false) {
        args.push("--force".to_string());
    }
    if let Some(sid) = session_id {
        if !sid.trim().is_empty() {
            args.push("--session".to_string());
            args.push(sid);
        }
    }
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn capability_profiles(project_root: Option<String>) -> Result<serde_json::Value, String> {
    // Read-only: capability-profile groupings + live Governed Bash posture.
    let root = resolve_project_root(project_root)?;
    run_json_cli(&root, &[
        "dashboard-capability-profiles".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
    ])
}

#[tauri::command]
fn governed_bash_status(project_root: Option<String>) -> Result<serde_json::Value, String> {
    // Read-only: the re-derived security posture. `verified` is the only
    // bit the UI may use to show ENABLED.
    let root = resolve_project_root(project_root)?;
    run_json_cli(&root, &[
        "governed-bash-status".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
    ])
}

#[tauri::command]
fn governed_bash_enable(
    project_root: Option<String>,
    approval_card_json: Option<String>,
    scope: Option<String>,
) -> Result<serde_json::Value, String> {
    // Admin-only: THE one action "Allow shell tools validated and supported
    // by AIDOCS". With no approval card, the CLI auto-discovers + attests
    // the canonical provider (no path/hash/signature ceremony). To approve
    // an unproven candidate, the UI echoes back ONE server-issued signed
    // card verbatim via --approval-card-json. Attach the operator token
    // (minted in solo/dev, present after login in corpo); without it the
    // CLI returns blocked_by=operator_auth so the UI can prompt login.
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "governed-bash-enable".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--scope".to_string(),
        scope.unwrap_or_else(|| "global".to_string()),
    ];
    if let Some(c) = approval_card_json {
        if !c.trim().is_empty() {
            args.push("--approval-card-json".to_string());
            args.push(c);
        }
    }
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    // Intentionally NOT run_json_cli_checked: governed-bash-enable returns
    // ok:false WITH the re-derived posture (failed checks: provider
    // identity, trusted root, probe, …) which the Governed Bash panel
    // RENDERS to tell the operator what to fix. ok:false here is structured
    // result data, not a refusal to surface as an error.
    run_json_cli(&root, &args)
}

#[tauri::command]
fn governed_bash_disable(
    project_root: Option<String>,
    scope: Option<String>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "governed-bash-disable".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--scope".to_string(),
        scope.unwrap_or_else(|| "global".to_string()),
    ];
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    // See governed_bash_enable: ok:false carries the re-derived posture the
    // panel renders, so this stays on run_json_cli (documented exception).
    run_json_cli(&root, &args)
}

// ── Operator Surface Catalog ────────────────────────────────────────
// Doctrine-level control profiles over the raw config ledger. list/status/
// inspect/rows are read-only; apply + expert_set are operator-auth gated
// (the CLI attaches the operator token and enforces editability / atomic
// posture). The UI must drive dangerous changes through these — never a
// raw per-key save of a service-managed or deprecated key.

#[tauri::command]
fn operator_surface_list(
    project_root: Option<String>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    run_json_cli(&root, &[
        "operator-surface".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
    ])
}

#[tauri::command]
fn operator_surface_status(
    project_root: Option<String>,
    profile_id: String,
    session_id: Option<String>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "operator-surface".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--status".to_string(), profile_id,
    ];
    if let Some(sid) = session_id {
        if !sid.trim().is_empty() {
            args.push("--session-id".to_string());
            args.push(sid);
        }
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
fn operator_surface_inspect(
    project_root: Option<String>,
    key: String,
    session_id: Option<String>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "operator-surface".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--inspect".to_string(), key,
    ];
    if let Some(sid) = session_id {
        if !sid.trim().is_empty() {
            args.push("--session-id".to_string());
            args.push(sid);
        }
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
fn operator_surface_rows(
    project_root: Option<String>,
    session_id: Option<String>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "operator-surface".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--rows".to_string(),
    ];
    if let Some(sid) = session_id {
        if !sid.trim().is_empty() {
            args.push("--session-id".to_string());
            args.push(sid);
        }
    }
    run_json_cli(&root, &args)
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
fn operator_surface_apply(
    project_root: Option<String>,
    profile_id: String,
    values_json: Option<String>,
    confirm: Option<String>,
    reason: Option<String>,
    scope: Option<String>,
    action: Option<String>,
    provider_path: Option<String>,
    hash_pin: Option<String>,
    require_os_signature: Option<bool>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "operator-surface".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--apply".to_string(), profile_id,
        "--scope".to_string(), scope.unwrap_or_else(|| "global".to_string()),
    ];
    if let Some(v) = values_json {
        if !v.trim().is_empty() {
            args.push("--values".to_string());
            args.push(v);
        }
    }
    if let Some(c) = confirm {
        if !c.trim().is_empty() {
            args.push("--confirm".to_string());
            args.push(c);
        }
    }
    if let Some(r) = reason {
        if !r.trim().is_empty() {
            args.push("--reason".to_string());
            args.push(r);
        }
    }
    if let Some(a) = action {
        if !a.trim().is_empty() {
            args.push("--action".to_string());
            args.push(a);
        }
    }
    if let Some(p) = provider_path {
        if !p.trim().is_empty() {
            args.push("--provider-path".to_string());
            args.push(p);
        }
    }
    if let Some(h) = hash_pin {
        if !h.trim().is_empty() {
            args.push("--hash-pin".to_string());
            args.push(h);
        }
    }
    if require_os_signature.unwrap_or(false) {
        args.push("--require-os-signature".to_string());
    }
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn operator_surface_expert_set(
    project_root: Option<String>,
    key: String,
    value_json: String,
    confirm: Option<String>,
    scope: Option<String>,
) -> Result<serde_json::Value, String> {
    let root = resolve_project_root(project_root)?;
    let mut args = vec![
        "operator-surface".to_string(),
        root.to_string_lossy().to_string(),
        "--json".to_string(),
        "--expert-set".to_string(), key,
        "--value".to_string(), value_json,
        "--scope".to_string(), scope.unwrap_or_else(|| "global".to_string()),
    ];
    if let Some(c) = confirm {
        if !c.trim().is_empty() {
            args.push("--confirm".to_string());
            args.push(c);
        }
    }
    if let Some(token) = resolve_operator_token(&root) {
        args.push("--operator-token".to_string());
        args.push(token);
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn vocab_delete_group(
    project_root: Option<String>,
    kind: String,
    lang: String,
    parent_key: String,
    parent_mode: Option<String>,
) -> Result<serde_json::Value, String> {
    if kind.is_empty() || parent_key.is_empty() {
        return Err("kind + parent_key required".into());
    }
    let root = resolve_project_root(project_root)?;
    let token = resolve_operator_token(&root)
        .ok_or_else(|| "operator_auth: no operator token (vocab delete is admin-only)".to_string())?;
    let mut args = vec![
        "dashboard-vocab-delete".to_string(),
        root.to_string_lossy().to_string(),
        "--kind".to_string(), kind,
        "--lang".to_string(), lang,
        "--parent-key".to_string(), parent_key,
        "--operator-token".to_string(), token,
        "--json".to_string(),
    ];
    if let Some(pm) = parent_mode {
        if !pm.is_empty() {
            args.push("--parent-mode".to_string());
            args.push(pm);
        }
    }
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn gate_msg_list(lang: String) -> Result<serde_json::Value, String> {
    let db = empire_db_path_rs();
    if !db.is_file() {
        return Ok(serde_json::json!({"rows": []}));
    }
    let conn = rusqlite::Connection::open_with_flags(
        &db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare(
            "SELECT key, body, source FROM gate_message_strings \
             WHERE lang = ? ORDER BY key",
        )
        .map_err(|e| e.to_string())?;
    let rows: Vec<serde_json::Value> = stmt
        .query_map([lang], |r| {
            Ok(serde_json::json!({
                "key": r.get::<_, String>(0)?,
                "body": r.get::<_, String>(1)?,
                "source": r.get::<_, String>(2)?,
            }))
        })
        .map_err(|e| e.to_string())?
        .filter_map(Result::ok)
        .collect();
    Ok(serde_json::json!({"rows": rows}))
}

#[tauri::command]
fn gate_msg_upsert(
    project_root: Option<String>,
    key: String,
    body: String,
    lang: Option<String>,
) -> Result<serde_json::Value, String> {
    if key.is_empty() {
        return Err("key required".into());
    }
    // Control-plane auth wall: route through the authenticated
    // dashboard-gate-msg-set CLI subcommand (admin.manage_config).
    let lang = lang.unwrap_or_else(|| "en".to_string());
    let root = resolve_project_root(project_root)?;
    let token = resolve_operator_token(&root)
        .ok_or_else(|| "operator_auth: no operator token (gate-msg upsert is admin-only)".to_string())?;
    let args = vec![
        "dashboard-gate-msg-set".to_string(),
        root.to_string_lossy().to_string(),
        "--key".to_string(), key,
        "--body".to_string(), body,
        "--lang".to_string(), lang,
        "--operator-token".to_string(), token,
        "--json".to_string(),
    ];
    run_json_cli_checked(&root, &args)
}

#[tauri::command]
fn gate_msg_delete(
    project_root: Option<String>,
    key: String,
    lang: Option<String>,
) -> Result<serde_json::Value, String> {
    if key.is_empty() {
        return Err("key required".into());
    }
    let lang = lang.unwrap_or_else(|| "en".to_string());
    let root = resolve_project_root(project_root)?;
    let token = resolve_operator_token(&root)
        .ok_or_else(|| "operator_auth: no operator token (gate-msg delete is admin-only)".to_string())?;
    let args = vec![
        "dashboard-gate-msg-delete".to_string(),
        root.to_string_lossy().to_string(),
        "--key".to_string(), key,
        "--lang".to_string(), lang,
        "--operator-token".to_string(), token,
        "--json".to_string(),
    ];
    run_json_cli_checked(&root, &args)
}

/// WebMCP OAuth loopback (DoD #6). Binds 127.0.0.1:<port>, waits for the single
/// OAuth redirect from the gate, parses `?code=`/`?state=`, replies with a small
/// page, and returns "code|state".
///
/// MUST be async + spawn_blocking: a SYNCHRONOUS command runs on Tauri's main/UI
/// thread, so a blocking `accept()` froze the whole window (and the OS killed it)
/// while waiting for the redirect. Here the blocking socket work runs on the
/// blocking pool and the command awaits it, keeping the UI responsive; a
/// non-blocking accept loop bounds the wait to `timeout_secs` (no leaked thread).
#[tauri::command]
async fn webmcp_oauth_capture(port: u16, timeout_secs: u64) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::time::{Duration, Instant};
        let listener = TcpListener::bind(("127.0.0.1", port))
            .map_err(|e| format!("could not bind 127.0.0.1:{port}: {e}"))?;
        listener.set_nonblocking(true).map_err(|e| e.to_string())?;
        let deadline = Instant::now() + Duration::from_secs(timeout_secs.max(1));
        let mut stream = loop {
            match listener.accept() {
                Ok((s, _addr)) => break s,
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        return Err("timed out waiting for the OAuth redirect".to_string());
                    }
                    std::thread::sleep(Duration::from_millis(150));
                }
                Err(e) => return Err(e.to_string()),
            }
        };
        stream.set_nonblocking(false).ok();
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let mut buf = [0u8; 8192];
        let n = stream.read(&mut buf).map_err(|e| e.to_string())?;
        let req = String::from_utf8_lossy(&buf[..n]);
        let target = req
            .lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(1))
            .unwrap_or("");
        let query = target.split_once('?').map(|(_, q)| q).unwrap_or("");
        let mut code = String::new();
        let mut state = String::new();
        for kv in query.split('&') {
            if let Some(v) = kv.strip_prefix("code=") {
                code = v.to_string();
            } else if let Some(v) = kv.strip_prefix("state=") {
                state = v.to_string();
            }
        }
        let body = "<!doctype html><html><body style=\"font-family:sans-serif;background:#0b0f14;color:#e2e8f0;text-align:center;padding-top:4rem\"><h2>WebMCP connected</h2><p>You can close this window and return to AIDOCS.</p></body></html>";
        let _ = stream.write_all(
            format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .as_bytes(),
        );
        let _ = stream.flush();
        if code.is_empty() {
            return Err(format!("no authorization code in callback (state={state})"));
        }
        Ok(format!("{code}|{state}"))
    })
    .await
    .map_err(|e| format!("oauth capture task failed: {e}"))?
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .invoke_handler(tauri::generate_handler![
            webmcp_oauth_capture,
            list_managed_projects,
            load_dashboard,
            approve_escalation,
            deny_escalation,
            save_config_setting,
            delete_config_setting,
            batch_config_settings,
            dashboard_auth_status,
            dashboard_logout,
            load_toml_documents,
            save_toml_document,
            toggle_managed_mode,
            mcp_registry_search,
            metrics_snapshot,
            memory_anchor_health,
            dashboard_live_cursor,
            skill_scan_results,
            toggle_skill,
            delete_skill,
            set_skill_provider_override,
            upload_skill,
            list_mcp_servers,
            install_mcp_server,
            delete_mcp_server,
            select_skill_file,
            import_skill_file,
            context_budget_check,
            context_compact,
            execution_clear_tokens,
            execution_clear_tool_calls,
            execution_prune_events,
            execution_usage_by_identity,
            delete_session,
            create_session,
            connect_session,
            conductor_start,
            conductor_send,
            conductor_status,
            conductor_stop,
            conductor_output,
            tauri_msg_send,
            tauri_msg_inbox,
            tauri_backlog_list,
            tauri_backlog_get,
            tauri_backlog_add,
            tauri_backlog_update,
            tauri_backlog_remove,
            tauri_todo_list,
            tauri_todo_update,
            tauri_todo_remove,
            opencode_models,
            open_url,
            select_folder,
            setup_detect,
            setup_install,
            setup_configure,
            setup_install_python,
            clear_degraded_state,
            vocab_list_kinds,
            vocab_list_langs,
            vocab_get_grouped,
            vocab_upsert_group,
            vocab_delete_group,
            palace_maintenance,
            capability_profiles,
            governed_bash_status,
            governed_bash_enable,
            governed_bash_disable,
            operator_surface_list,
            operator_surface_status,
            operator_surface_inspect,
            operator_surface_rows,
            operator_surface_apply,
            operator_surface_expert_set,
            gate_msg_list,
            gate_msg_upsert,
            gate_msg_delete,
            memory_kg_list,
            memory_kg_get,
            memory_kg_expand,
            memory_kg_graph
        ])
        .build(tauri::generate_context!())
        .expect("error while building AIDOCS Dashboard")
        .run(|_app, event| {
            if matches!(event, tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit) {
                stop_all_conductors();
                // Dashboard-war (c): the persistent snapshot worker must not
                // outlive the app window (an orphaned resident python IS the
                // storm class this kills).
                if let Ok(mut slot) = DASH_WORKER.lock() {
                    kill_dash_worker(&mut slot);
                }
                // Revoke the cached operator token on exit so the
                // row doesn't outlive the process. Best-effort:
                // resolve the project root the same way commands do.
                if let Ok(root) = resolve_project_root(None) {
                    revoke_cached_operator_token(&root);
                }
            }
        });
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        // NOT `cmd /c start <url>`: cmd treats '&' in the URL as a command
        // separator, so an OAuth URL (?client_id=...&redirect_uri=...&...) is
        // truncated at the first '&' — the browser only ever received
        // "?client_id=...", causing "redirect_uri not registered". rundll32's
        // FileProtocolHandler opens the EXACT url string via the default browser
        // with no shell re-parsing of '&'.
        std::process::Command::new("rundll32")
            .args(["url.dll,FileProtocolHandler", &url])
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&url)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&url)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
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
                        if let (Ok(major), Ok(minor)) =
                            (parts[0].parse::<u32>(), parts[1].parse::<u32>())
                        {
                            if major >= 3 && minor >= 11 {
                                #[cfg(target_os = "windows")]
                                if let Ok(w) = Command::new("where").arg(candidate).output() {
                                    let p = String::from_utf8_lossy(&w.stdout)
                                        .lines()
                                        .next()
                                        .unwrap_or("")
                                        .trim()
                                        .to_string();
                                    if !p.is_empty() {
                                        return Some(p);
                                    }
                                }
                                #[cfg(not(target_os = "windows"))]
                                if let Ok(w) = Command::new("which").arg(candidate).output() {
                                    let p = String::from_utf8_lossy(&w.stdout).trim().to_string();
                                    if !p.is_empty() {
                                        return Some(p);
                                    }
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
        if path.is_empty() {
            return Ok(None);
        }
        return Ok(Some(path));
    }
    #[allow(unreachable_code)]
    Ok(None)
}

#[tauri::command]
fn setup_detect(project_root: Option<String>) -> Result<Value, String> {
    let root = project_root.unwrap_or_else(|| {
        env::current_dir()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string()
    });
    let python_path = find_python_path();
    let python_version = python_path
        .as_ref()
        .and_then(|py| {
            Command::new(py).args(["--version"]).output().ok().map(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .trim()
                    .replace("Python ", "")
            })
        })
        .unwrap_or_default();
    // CLI presence probe. On Windows, npm-installed CLIs are `.cmd` shims and
    // `Command::new("claude")` does NOT resolve them (it tries only the bare name +
    // `.exe`), so claude/codex/npm read as "not detected" despite being installed.
    // Probe the PATHEXT-style candidates explicitly. (node is a real `.exe`, but route
    // it through the same helper for consistency.)
    let cli_found = |cmd: &str| -> bool {
        let candidates: Vec<String> = if cfg!(windows) {
            vec![
                format!("{cmd}.cmd"),
                format!("{cmd}.exe"),
                format!("{cmd}.bat"),
                cmd.to_string(),
            ]
        } else {
            vec![cmd.to_string()]
        };
        candidates.iter().any(|c| {
            Command::new(c.as_str())
                .args(["--version"])
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
        })
    };
    let node_found = cli_found("node");
    let npm_found = cli_found("npm");
    let home = if cfg!(windows) {
        env::var("USERPROFILE").unwrap_or_default()
    } else {
        env::var("HOME").unwrap_or_default()
    };
    let claude_found = cli_found("claude");
    let claude_authed = claude_found
        && (PathBuf::from(&home)
            .join(".claude")
            .join("credentials.json")
            .is_file()
            || PathBuf::from(&home)
                .join(".claude")
                .join("statsig_metadata")
                .is_file());
    let codex_found = cli_found("codex");
    let codex_authed = codex_found
        && (env::var("OPENAI_API_KEY").is_ok() || PathBuf::from(&home).join(".openai").exists());
    let vscode_ext = PathBuf::from(&home).join(".vscode").join("extensions");
    let has_vscode_claude = vscode_ext.is_dir()
        && fs::read_dir(&vscode_ext)
            .map(|entries| {
                entries.filter_map(|e| e.ok()).any(|e| {
                    e.file_name()
                        .to_string_lossy()
                        .to_lowercase()
                        .contains("claude")
                })
            })
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
            "Claude Code CLI" => (
                "npm",
                vec!["install", "-g", "@anthropic-ai/claude-code"],
                "Claude Code CLI",
            ),
            "Codex CLI" => ("npm", vec!["install", "-g", "@openai/codex"], "Codex CLI"),
            _ => {
                errors.push(format!("Unknown: {}", target));
                continue;
            }
        };
        match Command::new(cmd).args(&args).output() {
            Ok(output) if output.status.success() => installed.push(label.to_string()),
            Ok(output) => errors.push(format!(
                "{}: {}",
                label,
                String::from_utf8_lossy(&output.stderr)
                    .chars()
                    .take(200)
                    .collect::<String>()
            )),
            Err(e) => errors.push(format!("{}: {}", label, e)),
        }
    }
    Ok(
        serde_json::json!({ "success": errors.is_empty(), "installed": installed, "errors": errors }),
    )
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
    let home = if cfg!(windows) {
        env::var("USERPROFILE").unwrap_or_default()
    } else {
        env::var("HOME").unwrap_or_default()
    };
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
        let bundled = PathBuf::from(&local_app_data)
            .join("AIDOCS")
            .join("python")
            .join("python.exe");
        if bundled.is_file() {
            return Some(bundled.to_string_lossy().to_string());
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let home = env::var("HOME").unwrap_or_default();
        let bundled = PathBuf::from(&home)
            .join(".aidocs")
            .join("python")
            .join("bin")
            .join("python3");
        if bundled.is_file() {
            return Some(bundled.to_string_lossy().to_string());
        }
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
        let install_script = if script_dir
            .join("..\\..\\..\\..\\core\\scripts\\install.ps1")
            .is_file()
        {
            script_dir.join("..\\..\\..\\..\\core\\scripts\\install.ps1")
        } else {
            // Fallback: inline the download + extract
            return install_python_inline();
        };
        let output = Command::new("powershell")
            .args([
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                &install_script.to_string_lossy(),
            ])
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

    fs::create_dir_all(&aidocs_home)
        .map_err(|e| format!("Cannot create {}: {}", aidocs_home.display(), e))?;

    // Download python-build-standalone
    let url = "https://github.com/indygreg/python-build-standalone/releases/download/20250409/cpython-3.12.10+20250409-x86_64-pc-windows-msvc-install_only.tar.gz";
    let tmp_file = aidocs_home.join("python-download.tar.gz");

    let dl_output = Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "Invoke-WebRequest -Uri '{}' -OutFile '{}' -UseBasicParsing",
                url,
                tmp_file.to_string_lossy()
            ),
        ])
        .output()
        .map_err(|e| format!("Download failed: {}", e))?;

    if !dl_output.status.success() {
        return Err(format!(
            "Download failed: {}",
            String::from_utf8_lossy(&dl_output.stderr)
        ));
    }

    // Extract
    let extract_output = Command::new("tar")
        .args([
            "-xzf",
            &tmp_file.to_string_lossy(),
            "-C",
            &aidocs_home.to_string_lossy(),
        ])
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
        return Err(format!(
            "Python not found after extraction at {}",
            python_exe.display()
        ));
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
    fn toml_label_intent_tokens() {
        assert_eq!(
            toml_label("intent_tokens/commit.toml"),
            "Action tokens: commit.toml"
        );
    }

    #[test]
    fn toml_label_gate_messages() {
        assert_eq!(
            toml_label("gate_messages/post_push.toml"),
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
        assert_eq!(toml_category("intent_tokens/x.toml"), "Intent tokens");
    }

    #[test]
    fn toml_category_hooks() {
        assert_eq!(toml_category("gate_messages/y.toml"), "Workflow hooks");
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
        assert_eq!(toml_scope("intent_tokens/x.toml"), "Tokens");
    }

    #[test]
    fn toml_scope_hooks() {
        assert_eq!(toml_scope("gate_messages/y.toml"), "Hooks");
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
        assert_eq!(toml_target("intent_tokens/commit.toml"), "commit");
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
    fn toml_active_status_intent_tokens_enabled() {
        assert_eq!(
            toml_active_status("intent_tokens/commit.toml", "commit", Some("all")),
            "Enabled"
        );
    }

    #[test]
    fn toml_active_status_intent_tokens_not_selected() {
        assert_eq!(
            toml_active_status("intent_tokens/commit.toml", "commit", Some("review")),
            "Not selected"
        );
    }

    #[test]
    fn toml_active_status_hooks() {
        assert_eq!(
            toml_active_status("gate_messages/post.toml", "post", None),
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
    fn toml_language_context_intent_tokens() {
        assert_eq!(
            toml_language_context("intent_tokens/commit.toml", "", Some("python")),
            "Intent classification tokens \u{00b7} project set python"
        );
    }

    #[test]
    fn toml_language_context_gate_messages() {
        assert_eq!(
            toml_language_context("gate_messages/post.toml", "", None),
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
    fn is_aidocs_project_true_when_commission_stamp_exists() {
        let dir = tempfile::tempdir().unwrap();
        let idx = dir.path().join(".MEMORY").join(".index");
        fs::create_dir_all(&idx).unwrap();
        let conn = rusqlite::Connection::open(idx.join("aidocs.sqlite3")).unwrap();
        conn.execute_batch(
            "CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             INSERT INTO index_meta VALUES ('aidocs_commissioned', '2026-07-02');",
        )
        .unwrap();
        drop(conn);
        assert!(is_aidocs_project(dir.path()));
    }

    #[test]
    fn is_aidocs_project_false_when_db_exists_without_stamp() {
        // Forcing-bug guard: an incidental db (any store touch creates the
        // file) with NO commission stamp must NOT read as AIDOCS-managed.
        let dir = tempfile::tempdir().unwrap();
        let idx = dir.path().join(".MEMORY").join(".index");
        fs::create_dir_all(&idx).unwrap();
        let conn = rusqlite::Connection::open(idx.join("aidocs.sqlite3")).unwrap();
        conn.execute_batch(
            "CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )
        .unwrap();
        drop(conn);
        assert!(!is_aidocs_project(dir.path()));
    }

    #[test]
    fn is_aidocs_project_false_when_only_bare_memory() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        assert!(!is_aidocs_project(dir.path()));
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

    // Helper: seed a minimal aidocs_managed row in a project's big-boss
    // sqlite. Mirrors what the Python AidocsManagedStore writes on set()
    // — test fixtures here must match production schema exactly.
    fn seed_managed(dir: &Path, active: bool, session_id: &str) {
        let index_dir = dir.join(".MEMORY").join(".index");
        fs::create_dir_all(&index_dir).unwrap();
        let db_path = index_dir.join("aidocs.sqlite3");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS aidocs_managed (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                active INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                activated_at TEXT,
                last_updated TEXT,
                source TEXT
            );",
        )
        .unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO aidocs_managed (id, active, session_id, activated_at, last_updated, source)
             VALUES (1, ?, ?, ?, ?, ?)",
            rusqlite::params![
                if active { 1 } else { 0 },
                session_id,
                "2026-04-01 00:00",
                "2026-04-01 00:00",
                "/test",
            ],
        )
        .unwrap();
    }

    #[test]
    fn managed_session_id_active() {
        let dir = tempfile::tempdir().unwrap();
        seed_managed(dir.path(), true, "2026-04-01-test");
        assert_eq!(
            managed_session_id(dir.path()),
            Some("2026-04-01-test".to_string())
        );
    }

    #[test]
    fn managed_session_id_inactive() {
        let dir = tempfile::tempdir().unwrap();
        seed_managed(dir.path(), false, "2026-04-01-test");
        assert_eq!(managed_session_id(dir.path()), None);
    }

    #[test]
    fn managed_session_id_missing_file() {
        // No sqlite file at all — same "not managed" outcome.
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(managed_session_id(dir.path()), None);
    }

    #[test]
    fn managed_session_id_missing_table() {
        // Sqlite DB exists (other tables may have been initialized first)
        // but aidocs_managed table hasn't been created yet. Must treat
        // identically to "missing file" — managed mode is off.
        let dir = tempfile::tempdir().unwrap();
        let index_dir = dir.path().join(".MEMORY").join(".index");
        fs::create_dir_all(&index_dir).unwrap();
        let db_path = index_dir.join("aidocs.sqlite3");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch("CREATE TABLE unrelated (x INT);").unwrap();
        drop(conn);
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
            dir.path(),
            Some("My Project"),
            dir.path(),
            &mut seen,
            &mut projects,
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

        register_project_candidate(dir.path(), None, dir.path(), &mut seen, &mut projects);

        assert!(projects.is_empty());
    }

    #[test]
    fn register_candidate_deduplicates() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join(".MEMORY")).unwrap();
        let mut seen = BTreeSet::new();
        let mut projects = Vec::new();

        register_project_candidate(
            dir.path(),
            Some("First"),
            dir.path(),
            &mut seen,
            &mut projects,
        );
        register_project_candidate(
            dir.path(),
            Some("Second"),
            dir.path(),
            &mut seen,
            &mut projects,
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
            dir.path(),
            Some("my-cool_project"),
            dir.path(),
            &mut seen,
            &mut projects,
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
            dir.path(),
            Some("Other"),
            other.path(),
            &mut seen,
            &mut projects,
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
        let session_dir = dir.path().join(".MEMORY").join("sessions").join("s1");
        fs::create_dir_all(&session_dir).unwrap();
        fs::write(session_dir.join("aidocs.toml"), "# session").unwrap();

        let paths = allowed_toml_paths(dir.path(), Some("s1")).unwrap();
        assert!(paths.iter().any(|p| p.to_string_lossy().contains("s1")));
    }

    #[test]
    fn allowed_paths_includes_intent_tokens() {
        let dir = tempfile::tempdir().unwrap();
        let tokens = dir.path().join("intent_tokens");
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
    fn resolve_allowed_path_rejects_project_config() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("aidocs.toml"), "# config").unwrap();

        let result = resolve_allowed_toml_path(dir.path(), "aidocs.toml", None);
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), dir.path().join("aidocs.toml"));
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
        let tokens = dir.path().join("intent_tokens");
        fs::create_dir_all(&tokens).unwrap();
        fs::write(tokens.join("commit.toml"), "name = \"commit\"\n").unwrap();

        let docs = load_toml_documents_for_session(dir.path(), None).unwrap();

        assert_eq!(docs.len(), 1);

        let token_doc = docs
            .iter()
            .find(|d| d.path == "intent_tokens/commit.toml")
            .unwrap();
        assert_eq!(token_doc.label, "Action tokens: commit.toml");
        assert_eq!(token_doc.category, "Intent tokens");
        assert_eq!(token_doc.scope, "Tokens");
        assert_eq!(token_doc.active, "Unknown");
        assert!(token_doc.editable);
        assert!(!token_doc.content.is_empty());
    }

    // ── resolve_project_root ──

    #[test]
    fn resolve_root_explicit_path() {
        let dir = tempfile::tempdir().unwrap();
        let result = resolve_project_root(Some(dir.path().to_string_lossy().to_string()));
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), dir.path());
    }

    #[test]
    fn resolve_root_nonexistent_falls_through() {
        let result = resolve_project_root(Some("/nonexistent/path/xyz".to_string()));
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
