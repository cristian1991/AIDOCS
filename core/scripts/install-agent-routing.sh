#!/usr/bin/env bash
set -euo pipefail

ROOT_PATH="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$ROOT_PATH" ]]; then
  ROOT_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

ROOT="$(cd "$ROOT_PATH" && pwd)"
PROJECT_ROOT="$ROOT"
SOURCE_ROOT="$ROOT"
BUILD_CANDIDATE="$ROOT/build"

if [[ -f "$BUILD_CANDIDATE/.MEMORY/.aidocs/index.aidocs" ]]; then
  SOURCE_ROOT="$(cd "$BUILD_CANDIDATE" && pwd)"
elif [[ "$ROOT" == */build && -d "$(dirname "$ROOT")/mcp/server" ]]; then
  PROJECT_ROOT="$(cd "$(dirname "$ROOT")" && pwd)"
fi

if [[ ! -d "$PROJECT_ROOT/mcp/server" ]]; then
  CANDIDATE_PARENT="$(dirname "$ROOT")"
  if [[ -n "$CANDIDATE_PARENT" && -d "$CANDIDATE_PARENT/mcp/server" ]]; then
    PROJECT_ROOT="$(cd "$CANDIDATE_PARENT" && pwd)"
  fi
fi

INDEX_FILE="$SOURCE_ROOT/.MEMORY/.aidocs/index.aidocs"
if [[ ! -f "$INDEX_FILE" ]]; then
  echo ".MEMORY/.aidocs/index.aidocs not found at runtime root: $SOURCE_ROOT" >&2
  exit 1
fi

VERSION_FILE="$SOURCE_ROOT/.MEMORY/.aidocs/command-pack.version"
COMMAND_PACK_VERSION="unknown"
if [[ -f "$VERSION_FILE" ]]; then
  COMMAND_PACK_VERSION="$(head -n 1 "$VERSION_FILE" | tr -d '\r')"
fi

OPENCODE_DIR="$HOME/.config/opencode"
OPENCODE_COMMANDS_DIR="$OPENCODE_DIR/commands"
OPENCODE_PLUGINS_DIR="$OPENCODE_DIR/plugins"
OPENCODE_SETTINGS_PATH="$OPENCODE_DIR/opencode.json"
CLAUDE_DIR="$HOME/.claude"
CLAUDE_COMMANDS_DIR="$CLAUDE_DIR/commands"
CLAUDE_SETTINGS_PATH="$CLAUDE_DIR/settings.json"

mkdir -p "$OPENCODE_COMMANDS_DIR" "$OPENCODE_PLUGINS_DIR" "$CLAUDE_COMMANDS_DIR"

HEADER="$(printf '\U1F6D1') STOP"

cat > "$OPENCODE_DIR/AGENTS.md" <<EOF
# Global AGENTS.md - Cross-Agent Bootstrap

AIDOCS source: $SOURCE_ROOT

Non-negotiables:
- Do not operate outside the current project unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When clarification is needed, print a blank line, then: $HEADER
- Read only files relevant to the task (do not scan full repo by default).
- After entering a project, read project \`AGENTS.md\`/\`CLAUDE.md\`, then \`/.MEMORY/.aidocs/index.aidocs\`, then \`/.MEMORY/INDEX.md\`, then inspect \`/.MEMORY/sessions/*/SESSION.md\` and read the selected session.
- Durable memory, plans, and task output belong only in project-local \`/.MEMORY/**\`.
- Spawned-agent plans/investigations belong in the active session under \`/.MEMORY/sessions/<session-id>/agents/\`.
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.
- If a STOP condition appears during a multi-step script/command sequence, halt immediately and issue STOP (do not run remaining steps).

Routing order:
1) Project \`AGENTS.md\` or \`CLAUDE.md\` if present
2) Follow the project router (\`/.MEMORY/.aidocs/index.aidocs\` -> \`/.MEMORY/INDEX.md\` -> selected \`/.MEMORY/sessions/*/SESSION.md\`)
3) If project setup is missing, fall back to $SOURCE_ROOT/.MEMORY/.aidocs/index.aidocs
EOF

cat > "$CLAUDE_DIR/CLAUDE.md" <<EOF
# Global CLAUDE.md - Cross-Agent Bootstrap

AIDOCS source: $SOURCE_ROOT

Non-negotiables:
- Do not operate outside the current project unless explicitly instructed.
- Before acting, briefly state what you think the task is and what you will do.
- If user provides an error, explain WHY first; if clear, fix; if unclear, STOP and ask.
- When clarification is needed, print a blank line, then: $HEADER
- Read only files relevant to the task (do not scan full repo by default).
- After entering a project, read project \`AGENTS.md\`/\`CLAUDE.md\`, then \`/.MEMORY/.aidocs/index.aidocs\`, then \`/.MEMORY/INDEX.md\`, then inspect \`/.MEMORY/sessions/*/SESSION.md\` and read the selected session.
- Durable memory, plans, and task output belong only in project-local \`/.MEMORY/**\`.
- Claude auto-memory \`~/.claude/projects/<resolved>/memory/MEMORY.md\` is bootstrap-only; never store memory, plans, or task output there.
- Spawned-agent plans/investigations belong in the active session under \`/.MEMORY/sessions/<session-id>/agents/\`.
- If user states a durable fact/rule/lesson/preference to remember, persist it immediately to categorized project memory and log it in today's daily file.
- Router files list/link docs only; do not force-load full documentation by default.
- If context is insufficient, read necessary related docs + memory files; if still unclear, STOP and ask.
- If a STOP condition appears during a multi-step script/command sequence, halt immediately and issue STOP (do not run remaining steps).

Routing order:
1) Project \`AGENTS.md\` or \`CLAUDE.md\` if present
2) Follow the project router (\`/.MEMORY/.aidocs/index.aidocs\` -> \`/.MEMORY/INDEX.md\` -> selected \`/.MEMORY/sessions/*/SESSION.md\`)
3) If project setup is missing, fall back to $SOURCE_ROOT/.MEMORY/.aidocs/index.aidocs
EOF

OPENCODE_PLUGIN_SOURCE="$SOURCE_ROOT/plugins/aidocs.js"
if [[ ! -f "$OPENCODE_PLUGIN_SOURCE" ]]; then
  echo "Missing OpenCode plugin script: $OPENCODE_PLUGIN_SOURCE" >&2
  exit 1
fi
OPENCODE_PLUGIN_TARGET="$OPENCODE_PLUGINS_DIR/aidocs.js"
cp "$OPENCODE_PLUGIN_SOURCE" "$OPENCODE_PLUGIN_TARGET"

ACTION_TOKENS_ROOT="$PROJECT_ROOT/mcp/server/aidocs_mcp/action_tokens"
if [[ ! -d "$ACTION_TOKENS_ROOT" ]]; then
  ACTION_TOKENS_ROOT="$SOURCE_ROOT/server/aidocs_mcp/action_tokens"
fi
if [[ ! -d "$ACTION_TOKENS_ROOT" ]]; then
  echo "Missing action_tokens directory: $ACTION_TOKENS_ROOT" >&2
  exit 1
fi

OPENCODE_ACTION_TOKENS_DIR="$ACTION_TOKENS_ROOT/opencode"
mkdir -p "$OPENCODE_ACTION_TOKENS_DIR"
find "$OPENCODE_ACTION_TOKENS_DIR" -maxdepth 1 -type f -name '*.yaml' -delete

declare -a OPENCODE_ACTION_TOKEN_EXPORTS=()
link_or_copy() {
  local source="$1"
  local target="$2"
  rm -f "$target"
  if ln -s "$source" "$target" 2>/dev/null; then
    OPENCODE_ACTION_TOKEN_EXPORTS+=("$target (link)")
  else
    cp "$source" "$target"
    OPENCODE_ACTION_TOKEN_EXPORTS+=("$target (copy)")
  fi
}

for token_file in "$ACTION_TOKENS_ROOT"/*.yaml; do
  [[ -f "$token_file" ]] || continue
  link_or_copy "$token_file" "$OPENCODE_ACTION_TOKENS_DIR/$(basename "$token_file")"
done

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

export OPENCODE_SETTINGS_PATH PROJECT_ROOT PYTHON_BIN CLAUDE_SETTINGS_PATH SOURCE_ROOT

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

CLAUDE_HOOK_SCRIPT="$SOURCE_ROOT/scripts/claude-hook.sh"
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

hook_command = f"bash '{Path(os.environ['SOURCE_ROOT']) / 'scripts' / 'claude-hook.sh'}'"
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

hooks["UserPromptSubmit"] = remove_aidocs_groups(hooks.get("UserPromptSubmit")) + [user_prompt_group]
hooks["PreToolUse"] = remove_aidocs_groups(hooks.get("PreToolUse")) + [pre_tool_group]

path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

SHARED_COMMANDS_DIR="$SOURCE_ROOT/.commands"
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
echo "AIDOCS source wired to: $SOURCE_ROOT"
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
