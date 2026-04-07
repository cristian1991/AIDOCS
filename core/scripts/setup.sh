#!/usr/bin/env bash
set -euo pipefail

ROOT_PATH="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$ROOT_PATH" ]]; then
  ROOT_PATH="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi

ROOT="$(cd "$ROOT_PATH" && pwd)"

resolve_repo_and_core_roots() {
  local candidate="$1"
  local parent="$(dirname "$candidate")"
  for root in "$candidate" "$parent"; do
    if [[ -d "$root/mcp/server" && -f "$root/core/plugins/aidocs.js" ]]; then
      printf '%s\n%s\n' "$(cd "$root" && pwd)" "$(cd "$root/core" && pwd)"
      return 0
    fi
  done
  if [[ -f "$candidate/plugins/aidocs.js" && -d "$parent/mcp/server" ]]; then
    printf '%s\n%s\n' "$(cd "$parent" && pwd)" "$(cd "$candidate" && pwd)"
    return 0
  fi
  return 1
}

if ! mapfile -t ROOTS < <(resolve_repo_and_core_roots "$ROOT"); then
  echo "Could not resolve repo root and core root from: $ROOT" >&2
  exit 1
fi

PROJECT_ROOT="${ROOTS[0]}"
CORE_ROOT="${ROOTS[1]}"

INDEX_FILE="$PROJECT_ROOT/.MEMORY/.aidocs/index.aidocs"
if [[ ! -f "$INDEX_FILE" ]]; then
  echo ".MEMORY/.aidocs/index.aidocs not found at repo root: $PROJECT_ROOT" >&2
  exit 1
fi

VERSION_FILE="$PROJECT_ROOT/.MEMORY/.aidocs/command-pack.version"
COMMAND_PACK_VERSION="unknown"
if [[ -f "$VERSION_FILE" ]]; then
  COMMAND_PACK_VERSION="$(head -n 1 "$VERSION_FILE" | tr -d '\r')"
fi

OPENCODE_DIR="$HOME/.config/opencode"
OPENCODE_COMMANDS_DIR="$OPENCODE_DIR/commands"
OPENCODE_PLUGINS_DIR="$OPENCODE_DIR/plugins"
OPENCODE_ACTION_HOOKS_DIR="$OPENCODE_DIR/action_hooks"
if [[ -f "$OPENCODE_DIR/opencode.jsonc" ]]; then
  OPENCODE_SETTINGS_PATH="$OPENCODE_DIR/opencode.jsonc"
else
  OPENCODE_SETTINGS_PATH="$OPENCODE_DIR/opencode.json"
fi
CLAUDE_DIR="$HOME/.claude"
CLAUDE_COMMANDS_DIR="$CLAUDE_DIR/commands"
CLAUDE_SETTINGS_PATH="$CLAUDE_DIR/settings.json"

mkdir -p "$OPENCODE_COMMANDS_DIR" "$OPENCODE_PLUGINS_DIR" "$OPENCODE_ACTION_HOOKS_DIR" "$CLAUDE_COMMANDS_DIR"

HEADER="$(printf '\U1F6D1') STOP"

write_agent_file_with_backup() {
  local target_path="$1"
  local template_path="$2"
  local aidocs_path="$3"
  local stop_header="$4"

  # Read and substitute template
  local managed
  managed=$(sed -e "s|{{AIDOCS_PATH}}|$aidocs_path|g" -e "s|{{STOP_HEADER}}|$stop_header|g" "$template_path")

  if [[ -f "$target_path" ]]; then
    local existing
    existing=$(cat "$target_path")

    # Extract user section (everything after AIDOCS:END)
    local end_tag="<!-- AIDOCS:END -->"
    if echo "$existing" | grep -qF "$end_tag"; then
      local user_section
      user_section=$(echo "$existing" | sed -n "/<!-- AIDOCS:END -->/,\$p" | tail -n +2)
    else
      # No tags — entire file is user content
      local user_section="$existing"
    fi

    # Backup before overwrite
    cp "$target_path" "${target_path}.backup"

    # Merge: managed + user section
    if [[ -n "${user_section// /}" ]]; then
      printf '%s\n%s\n' "$managed" "$user_section" > "$target_path"
    else
      printf '%s\n' "$managed" > "$target_path"
    fi
  else
    printf '%s\n' "$managed" > "$target_path"
  fi
}

AGENTS_TEMPLATE="$CORE_ROOT/templates/global-agents.md.tmpl"
CLAUDE_TEMPLATE="$CORE_ROOT/templates/global-claude.md.tmpl"

write_agent_file_with_backup "$OPENCODE_DIR/AGENTS.md" "$AGENTS_TEMPLATE" "$PROJECT_ROOT" "$HEADER"
write_agent_file_with_backup "$CLAUDE_DIR/CLAUDE.md" "$CLAUDE_TEMPLATE" "$PROJECT_ROOT" "$HEADER"

OPENCODE_PLUGIN_SOURCE="$CORE_ROOT/plugins/aidocs.js"
if [[ ! -f "$OPENCODE_PLUGIN_SOURCE" ]]; then
  echo "Missing OpenCode plugin script: $OPENCODE_PLUGIN_SOURCE" >&2
  exit 1
fi

# ── Smart file installation via manifest-aware Python installer ──
INSTALL_SCRIPT="$SCRIPT_DIR/install_files.py"

echo ""
echo "Installing plugin files..."
python3 "$INSTALL_SCRIPT" "$PROJECT_ROOT" "$CORE_ROOT" "$OPENCODE_PLUGINS_DIR" "plugin" 2>/dev/null || python "$INSTALL_SCRIPT" "$PROJECT_ROOT" "$CORE_ROOT" "$OPENCODE_PLUGINS_DIR" "plugin"

echo ""
echo "Installing action tokens..."
PLUGIN_ACTION_TOKENS_DIR="$OPENCODE_PLUGINS_DIR/action_tokens"
python3 "$INSTALL_SCRIPT" "$PROJECT_ROOT" "$CORE_ROOT" "$PLUGIN_ACTION_TOKENS_DIR" "action_tokens" 2>/dev/null || python "$INSTALL_SCRIPT" "$PROJECT_ROOT" "$CORE_ROOT" "$PLUGIN_ACTION_TOKENS_DIR" "action_tokens"

echo ""
echo "Installing action hooks..."
python3 "$INSTALL_SCRIPT" "$PROJECT_ROOT" "$CORE_ROOT" "$OPENCODE_ACTION_HOOKS_DIR" "action_hooks" 2>/dev/null || python "$INSTALL_SCRIPT" "$PROJECT_ROOT" "$CORE_ROOT" "$OPENCODE_ACTION_HOOKS_DIR" "action_hooks"


PYTHON_BIN=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3/python/py not found in PATH" >&2
  exit 1
fi

# Prefer venv Python if available
VENV_PYTHON="$HOME/.aidocs/venv/bin/python"
if [[ -f "$VENV_PYTHON" ]]; then
  PYTHON_BIN="$VENV_PYTHON"
fi

export OPENCODE_SETTINGS_PATH PROJECT_ROOT PYTHON_BIN CLAUDE_SETTINGS_PATH CORE_ROOT

"$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["OPENCODE_SETTINGS_PATH"])
if path.exists():
    raw = path.read_text(encoding="utf-8").strip()
    data = json.loads(raw) if raw else {}
else:
    data = {}

data.setdefault("$schema", "https://opencode.ai/config.json")
data.setdefault("mcp", {})
data["mcp"]["aidocs"] = {
    "type": "local",
    "enabled": True,
    "timeout": 120000,
    "command": [os.environ["PYTHON_BIN"], "-m", "aidocs_mcp.mcp_server"],
    "environment": {
        "PYTHONPATH": str(Path(os.environ["PROJECT_ROOT"]) / "mcp" / "server")
    },
}
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

CLAUDE_HOOK_SCRIPT="$CORE_ROOT/scripts/claude-hook.sh"
if [[ ! -f "$CLAUDE_HOOK_SCRIPT" ]]; then
  echo "Missing Claude hook script: $CLAUDE_HOOK_SCRIPT" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["CLAUDE_SETTINGS_PATH"])
if path.exists():
    raw = path.read_text(encoding="utf-8").strip()
    data = json.loads(raw) if raw else {}
else:
    data = {}

hooks = data.setdefault("hooks", {})

def normalize_groups(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def remove_aidocs_groups(groups):
    result = []
    for group in normalize_groups(groups):
        group_hooks = normalize_groups(group.get("hooks")) if isinstance(group, dict) else []
        is_aidocs = False
        for hook in group_hooks:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command") or ""
            status = hook.get("statusMessage") or ""
            if ("claude-hook.ps1" in command or "claude-hook.sh" in command or status.startswith("AIDOCS ")):
                is_aidocs = True
                break
        if not is_aidocs:
            result.append(group)
    return result

hook_command = f"bash '{Path(os.environ['CORE_ROOT']) / 'scripts' / 'claude-hook.sh'}'"
session_start_group = {
    "hooks": [
        {
            "type": "command",
            "shell": "bash",
            "command": hook_command,
            "timeout": 30,
            "statusMessage": "AIDOCS startup routing",
        }
    ]
}
user_prompt_group = {
    "hooks": [
        {
            "type": "command",
            "shell": "bash",
            "command": hook_command,
            "timeout": 30,
            "statusMessage": "AIDOCS prompt routing",
        }
    ]
}
pre_tool_group = {
    "matcher": "Read|Edit|Write|Glob|Grep|Bash",
    "hooks": [
        {
            "type": "command",
            "shell": "bash",
            "command": hook_command,
            "timeout": 30,
            "statusMessage": "AIDOCS tool guardrails",
        }
    ]
}

hooks["SessionStart"] = remove_aidocs_groups(hooks.get("SessionStart")) + [session_start_group]
hooks["UserPromptSubmit"] = remove_aidocs_groups(hooks.get("UserPromptSubmit")) + [user_prompt_group]
hooks["PreToolUse"] = remove_aidocs_groups(hooks.get("PreToolUse")) + [pre_tool_group]


# Disable CC auto-memory for AIDOCS project — memory_capture is the only path
local_settings = Path(os.environ["PROJECT_ROOT"]) / ".claude" / "settings.local.json"
local_settings.parent.mkdir(parents=True, exist_ok=True)
if local_settings.exists():
    try:
        ls_data = json.loads(local_settings.read_text(encoding="utf-8").strip() or "{}")
    except Exception:
        ls_data = {}
else:
    ls_data = {}
if ls_data.get("autoMemoryEnabled") is not False:
    ls_data["autoMemoryEnabled"] = False
    local_settings.write_text(json.dumps(ls_data, indent=2) + "\n", encoding="utf-8")
    print(f"Disabled CC auto-memory in {local_settings}")
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

SHARED_COMMANDS_DIR="$CORE_ROOT/.commands"
if [[ ! -d "$SHARED_COMMANDS_DIR" ]]; then
  echo "Missing shared command source folder: $SHARED_COMMANDS_DIR" >&2
  exit 1
fi

find "$OPENCODE_COMMANDS_DIR" -maxdepth 1 -type f -name '*.md' -delete
find "$CLAUDE_COMMANDS_DIR" -maxdepth 1 -type f -name '*.md' -delete

export SHARED_COMMANDS_DIR OPENCODE_COMMANDS_DIR CLAUDE_COMMANDS_DIR
"$PYTHON_BIN" - <<'PY'
import os
import re
from pathlib import Path

skip = {"doctor.md"}
shared = Path(os.environ["SHARED_COMMANDS_DIR"])
opencode_dir = Path(os.environ["OPENCODE_COMMANDS_DIR"])
claude_dir = Path(os.environ["CLAUDE_COMMANDS_DIR"])

for source in sorted(shared.glob("*.md")):
    if source.name in skip:
        continue
    raw = source.read_text(encoding="utf-8")
    (claude_dir / source.name).write_text(raw, encoding="utf-8")

    opencode_raw = raw
    match = re.match(r"(?s)^---\r?\n(.*?)\r?\n---", opencode_raw)
    if match and not re.search(r"(?m)^agent:\s*", match.group(1)):
        replacement = "---\n" + match.group(1) + "\nagent: build\n---"
        opencode_raw = re.sub(r"(?s)^---\r?\n(.*?)\r?\n---", replacement, opencode_raw, count=1)
    (opencode_dir / source.name).write_text(opencode_raw, encoding="utf-8")
PY

declare -a COPIED_FILES=()
declare -a CLAUDE_COPIED_FILES=()

while IFS= read -r -d '' file; do
  COPIED_FILES+=("$file")
done < <(find "$OPENCODE_COMMANDS_DIR" -maxdepth 1 -type f -name '*.md' -print0 | sort -z)

while IFS= read -r -d '' file; do
  CLAUDE_COPIED_FILES+=("$file")
done < <(find "$CLAUDE_COMMANDS_DIR" -maxdepth 1 -type f -name '*.md' -print0 | sort -z)

echo "Installed global routing files:"
echo "- $OPENCODE_DIR/AGENTS.md"
echo "- $OPENCODE_SETTINGS_PATH"
echo "- $OPENCODE_PLUGIN_TARGET"
echo "- $CLAUDE_DIR/CLAUDE.md"
echo "- $CLAUDE_SETTINGS_PATH"
for file in "${COPIED_FILES[@]}"; do
  echo "- $file"
done
for file in "${CLAUDE_COPIED_FILES[@]}"; do
  echo "- $file"
done
for export_file in "${OPENCODE_ACTION_TOKEN_EXPORTS[@]}"; do
  echo "- $export_file"
done
if [[ -d "$ACTION_HOOKS_ROOT" ]]; then
  while IFS= read -r -d '' file; do
    echo "- $file"
  done < <(find "$OPENCODE_ACTION_HOOKS_DIR" -maxdepth 1 -type f -name '*.toml' -print0 | sort -z)
fi

# ── Auto-install MCP runtime into ~/.aidocs/venv ──
AIDOCS_HOME="$HOME/.aidocs"
VENV_DIR="$AIDOCS_HOME/venv"
MCP_PACKAGE_DIR="$PROJECT_ROOT/mcp"

if [[ -f "$MCP_PACKAGE_DIR/pyproject.toml" ]]; then
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating AIDOCS MCP venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR" || python -m venv "$VENV_DIR"
  fi

  VENV_PIP="$VENV_DIR/bin/pip"
  if [[ -f "$VENV_PIP" ]]; then
    echo "Installing AIDOCS MCP runtime..."
    "$VENV_PIP" install -e "$MCP_PACKAGE_DIR" --quiet 2>/dev/null
    if [[ $? -eq 0 ]]; then
      echo "MCP runtime installed successfully."
    else
      echo "WARNING: MCP runtime install failed. You may need to install manually: cd mcp && pip install -e ."
    fi
  fi
fi


# Set AIDOCS_PATH in shell profile
AIDOCS_EXPORT_LINE="export AIDOCS_PATH=\"$PROJECT_ROOT\""
SHELL_PROFILE=""
if [[ -f "$HOME/.zshrc" ]]; then
  SHELL_PROFILE="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then
  SHELL_PROFILE="$HOME/.bashrc"
elif [[ -f "$HOME/.profile" ]]; then
  SHELL_PROFILE="$HOME/.profile"
fi

if [[ -n "$SHELL_PROFILE" ]]; then
  # Remove any existing AIDOCS_PATH line and add the new one
  if grep -q "^export AIDOCS_PATH=" "$SHELL_PROFILE" 2>/dev/null; then
    sed -i.bak '/^export AIDOCS_PATH=/d' "$SHELL_PROFILE"
    rm -f "${SHELL_PROFILE}.bak"
  fi
  echo "$AIDOCS_EXPORT_LINE" >> "$SHELL_PROFILE"
  export AIDOCS_PATH="$PROJECT_ROOT"
  echo "Set AIDOCS_PATH=$PROJECT_ROOT in $SHELL_PROFILE"
else
  echo "WARNING: Could not find shell profile (.zshrc/.bashrc/.profile)."
  echo "Add manually: $AIDOCS_EXPORT_LINE"
fi

echo "AIDOCS source wired to: $PROJECT_ROOT"
echo "AIDOCS core assets wired to: $CORE_ROOT"
echo "Command pack version: $COMMAND_PACK_VERSION"

REQUIRED_COMMAND_FILES=(aidocs.md reingest.md archive.md personality.md clean.md)
for command_name in "${REQUIRED_COMMAND_FILES[@]}"; do
  if [[ ! -f "$OPENCODE_COMMANDS_DIR/$command_name" ]]; then
    echo "Missing installed OpenCode command: $OPENCODE_COMMANDS_DIR/$command_name" >&2
    exit 1
  fi
  if [[ ! -f "$CLAUDE_COMMANDS_DIR/$command_name" ]]; then
    echo "Missing installed Claude command: $CLAUDE_COMMANDS_DIR/$command_name" >&2
    exit 1
  fi
done
