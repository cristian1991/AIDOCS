"""Anti-coup law — canonical, surface-agnostic tool authority.

A coup is any agent-invoked mutation that can install, remove, disable,
bypass, shadow, or weaken AIDOCS itself: MCP/tool registry edits, Claude/
editor settings, AIDOCS config + security flags, hooks, or forging a
PARALLEL tool path that runs OUTSIDE the gate (shell rc / alias / PATH,
git hooks, symlinks into bin).

This module is the ONE place the patterns and their verdicts live. It is
consumed by:
  * agent_orchestrator.check_tool — the canonical tool-call verdict, so
    EVERY surface (Bash/ai_run, Write/Edit, MCP create/replace/batch, …)
    routes through the same anti-coup decision, fail closed on
    uncertainty.
  * shell_xray._xray_control_plane — maps the same findings into the
    ai_run future-sight execution graph for audit/freeze.

Decisions: deny (edits that disarm the gate or forge an unchecked exec
path), confirm (registry/settings edits an operator may legitimately
want → freeze), allow (not a control-plane mutation).

Jurisdiction honesty: a finding may be marked OUT of jurisdiction when the
RESULT runs outside AIDOCS (e.g. a newly registered MCP server points at
an external host binary). We gate the agent COMMAND that creates it, but
we never claim to govern the unmanaged host-parallel tool itself. AIDOCS
also never claims control over background host self-updaters or already-
running processes — they issue no agent command and are simply not seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DECISION_ALLOW = "allow"
DECISION_CONFIRM = "confirm"
DECISION_DENY = "deny"
_RANK = {DECISION_ALLOW: 0, DECISION_CONFIRM: 1, DECISION_DENY: 2}

JURISDICTION_IN = "in"
JURISDICTION_OUT = "out"

# node/finding kind → decision (single source of truth, shared with the
# shell x-ray severity sets).
_KIND_DECISION = {
    "aidocs_config_mutation": DECISION_DENY,
    "security_flag_mutation": DECISION_DENY,
    "hook_mutation": DECISION_DENY,
    "parallel_tool_path": DECISION_DENY,
    "mcp_registry_mutation": DECISION_CONFIRM,
    "tool_registry_mutation": DECISION_CONFIRM,
    "settings_mutation": DECISION_CONFIRM,
    "make_executable": DECISION_CONFIRM,
    # uncertainty → fail closed.
    "uncertain": DECISION_DENY,
}


@dataclass(frozen=True)
class CoupFinding:
    kind: str
    decision: str
    label: str  # SAFE: pattern/category name — never raw command/secret
    jurisdiction: str = JURISDICTION_IN
    reason: str = ""


# ── shared constants ────────────────────────────────────────────────
EDITOR_BINARIES = frozenset(
    {
        "code",
        "code-insiders",
        "codium",
        "vscodium",
        "code-server",
        "cursor",
        "windsurf",
        "positron",
    },
)

# control-plane file targets (substrings, matched against a /-normalized
# lowercased path or command).
_CP_DENY_FILES = (
    "/.aidocs/",
    ".memory/.aidocs",
    ".aidocs/config",
    "aidocs.config",
    # AIDOCS security / config store. Writing into the index directory or
    # the config DB via a file tool or shell redirect is NEVER legitimate
    # (the real writers are AIDOCS services, not agent tools) — a script
    # authored next to the store, or a direct DB write, is a self-
    # escalation coup. Hard-deny the whole store directory + the DB by name.
    ".memory/.index/",
    ".memory/.index\\",
    "aidocs.sqlite3",
    "empire.sqlite3",
    ".claude/hooks",
    ".git/hooks",
    ".bashrc",
    ".zshrc",
    ".bash_profile",
    ".bash_aliases",
    ".profile",
    ".kshrc",
    "powershell_profile",
    "profile.ps1",
    "/etc/profile",
)
# substrings within a matched deny path that route it to the AIDOCS
# config/security-store kind (vs hooks / shell-init).
_AIDOCS_STORE_HINTS = ("aidocs", "index", "sqlite")
_CP_CONFIRM_FILES = (
    ".mcp.json",
    "mcp.json",
    "claude_desktop_config.json",
    ".claude/settings",
    ".cursor/mcp.json",
    ".vscode/mcp.json",
    ".continue/config",
)

_WRITE_LEAD_BINARIES = frozenset(
    {
        "tee",
        "cp",
        "mv",
        "dd",
        "install",
        "rsync",
        "truncate",
        "set-content",
        "add-content",
        "out-file",
        "copy-item",
        "move-item",
        "new-item",
    },
)
_REDIR = re.compile(r">>?")
# A redirect that WRITES TO A REAL FILE: optional fd number, > or >>, then a target
# that is NOT the null device (`>/dev/null`, `2>/dev/null`) and NOT an fd-dup
# (`2>&1`, `>&2`). Reading a control-plane file while discarding stderr to /dev/null
# must NOT look like a write — that false positive froze the whole session on a
# `grep ... .claude/settings.json 2>/dev/null` (Empire 2026-06-20, bug #67).
_FILE_WRITE_REDIR = re.compile(
    r"\d*>>?\s*(?!&)(?!/dev/null\b)(?!nul\b)\S",
    re.IGNORECASE,
)
_ALIAS_DEF = re.compile(r"(?:^|\s)alias\s+[\w.\-]+\s*=")
_PATH_MUT = re.compile(
    r"\bexport\s+path\s*=|\bpath\s*=\s*\$?\{?path|\$env:path\s*=|"
    r"\bsetx\s+path\b",
)
_CHAIN_SPLIT = re.compile(r"[;&|\n]+|\|\||&&")
_BINARY_EXTS = (".exe", ".cmd", ".bat", ".ps1")


def _kind(
    kind: str,
    label: str,
    *,
    jurisdiction: str = JURISDICTION_IN,
    reason: str = "",
) -> CoupFinding:
    return CoupFinding(kind, _KIND_DECISION.get(kind, DECISION_DENY), label, jurisdiction, reason)


def _normalize_binary(token: str) -> str:
    b = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    for ext in _BINARY_EXTS:
        if b.endswith(ext):
            return b[: -len(ext)]
    return b


def _norm_path(path: str) -> str:
    return str(path).replace("\\", "/").lower()


# ── path classification (file/MCP/config mutation surfaces) ─────────
def finding_for_path(path: str, operation: str = "write") -> CoupFinding | None:
    """Classify a file-mutation target path. Returns the worst finding or
    None when the path is not a control-plane target.

    A READ is never a coup (#176): reading a control-plane registry/config
    file (e.g. ``cat .mcp.json``) does not mutate it, so it must not be
    classified as an ``*_mutation``. Read-sensitivity (secrets in the file)
    is the read gate's concern, not this coup-mutation classifier. Callers
    only ever pass writer paths today (known readers are excluded upstream),
    so this honors the param without changing write gating.
    """
    if str(operation or "").strip().lower() == "read":
        return None
    low = _norm_path(path)
    for sub in _CP_DENY_FILES:
        if sub in low:
            if any(h in sub for h in _AIDOCS_STORE_HINTS):
                return _kind(
                    "aidocs_config_mutation",
                    "control_plane",
                    reason="writes the AIDOCS config/security store (index dir / config DB)",
                )
            if "hook" in sub:
                return _kind("hook_mutation", "control_plane", reason="edits an auto-exec hook")
            return _kind(
                "parallel_tool_path",
                "shell_init",
                reason="edits a shell init / PATH surface",
            )
    for sub in _CP_CONFIRM_FILES:
        if sub in low:
            if "mcp" in sub or "desktop" in sub:
                return _kind(
                    "mcp_registry_mutation",
                    "control_plane",
                    jurisdiction=JURISDICTION_OUT,
                    reason="edits an MCP server registry",
                )
            return _kind("settings_mutation", "control_plane", reason="edits agent/editor settings")
    return None


# ── command classification (shell mutation surfaces) ────────────────
def findings_for_segment(seg: str, binary: str, args: list[str]) -> list[CoupFinding]:
    """All anti-coup findings for one already-split command segment.
    ``binary`` should be normalized (no path / .exe / .cmd).
    """
    out: list[CoupFinding] = []
    low = seg.lower()
    # 1. registry / config / plugin CLIs
    if binary == "claude":
        pos = [a.lower() for a in args if not a.startswith("-")]
        s1 = pos[0] if pos else ""
        s2 = pos[1] if len(pos) > 1 else ""
        if s1 == "mcp" and s2 in ("add", "add-json", "remove", "rm", "reset-project-choices"):
            jx = JURISDICTION_OUT if s2 in ("add", "add-json") else JURISDICTION_IN
            out.append(
                _kind(
                    "mcp_registry_mutation",
                    f"claude_mcp_{s2}",
                    jurisdiction=jx,
                    reason="registers/removes an MCP server that runs outside AIDOCS",
                ),
            )
        elif s1 == "config" and s2 in ("add", "set", "remove", "rm", "unset"):
            out.append(_kind("tool_registry_mutation", f"claude_config_{s2}"))
        elif s1 == "plugin" and s2 in ("install", "add", "uninstall", "remove", "rm"):
            out.append(_kind("tool_registry_mutation", f"claude_plugin_{s2}"))
    if binary in EDITOR_BINARIES and "--add-mcp" in low:
        out.append(
            _kind(
                "mcp_registry_mutation",
                "editor_add_mcp",
                jurisdiction=JURISDICTION_OUT,
                reason="registers an MCP server in the editor",
            ),
        )
    # 2. parallel unchecked tool paths
    if _ALIAS_DEF.search(low):
        out.append(
            _kind("parallel_tool_path", "alias", reason="defines a shell alias (unchecked path)"),
        )
    if _PATH_MUT.search(low):
        out.append(
            _kind("parallel_tool_path", "path_mutation", reason="mutates PATH (unchecked path)"),
        )
    if binary == "chmod" and any(
        ("+x" in a) or a in ("755", "0755", "777", "0777", "+rwx") for a in args
    ):
        out.append(_kind("make_executable", "chmod"))
    if binary == "ln" and any(a in ("-s", "--symbolic", "-sf") for a in args):
        out.append(_kind("make_executable", "symlink"))
    # 3. control-plane FILE writes (qualified by a write signal or claude)
    if _seg_writes(low, binary, args) or binary == "claude":
        for sub in _CP_DENY_FILES:
            if sub in low:
                if any(h in sub for h in _AIDOCS_STORE_HINTS):
                    out.append(_kind("aidocs_config_mutation", "control_plane"))
                elif "hook" in sub:
                    out.append(_kind("hook_mutation", "control_plane"))
                else:
                    out.append(_kind("parallel_tool_path", "shell_init"))
                break
        for sub in _CP_CONFIRM_FILES:
            if sub in low:
                if "mcp" in sub or "desktop" in sub:
                    out.append(
                        _kind(
                            "mcp_registry_mutation",
                            "control_plane",
                            jurisdiction=JURISDICTION_OUT,
                        ),
                    )
                else:
                    out.append(_kind("settings_mutation", "control_plane"))
                break
    return out


def _seg_writes(low: str, binary: str, args: list[str]) -> bool:
    # A redirect signals a write ONLY when it targets a real file — a discard to the
    # null device or an fd-dup (`2>/dev/null`, `2>&1`) writes nothing and must not turn
    # a READ of a control-plane file into a coup (bug #67).
    if _FILE_WRITE_REDIR.search(low):
        return True
    if binary in _WRITE_LEAD_BINARIES or binary == "ln":
        return True
    if binary == "sed" and any(a.startswith("-i") or a == "--in-place" for a in args):
        return True
    return False


def findings_for_command(command: str) -> list[CoupFinding]:
    out: list[CoupFinding] = []
    for seg in _CHAIN_SPLIT.split(command or ""):
        toks = seg.strip().split()
        if not toks:
            continue
        binary = _normalize_binary(toks[0])
        out.extend(findings_for_segment(seg, binary, toks[1:]))
    return out


def _worst(findings: list[CoupFinding]) -> CoupFinding | None:
    worst: CoupFinding | None = None
    for f in findings:
        if worst is None or _RANK[f.decision] > _RANK[worst.decision]:
            worst = f
    return worst


def classify_command(command: str) -> CoupFinding | None:
    return _worst(findings_for_command(command))


# ── canonical surface-agnostic entry point ──────────────────────────
_PATH_KEYS = (
    "file_path",
    "filePath",
    "path",
    "notebook_path",
    "notebookPath",
    "target",
    "dest",
    "destination",
)

# Host tools that WRITE/EDIT a file by path. Anchored to access_gate's
# _RAW_EDIT_TOOLS (cross-checked by test_anticoup) PLUS the AIDOCS ai_*
# file mutators. A READ of a control-plane file is never a coup; exec
# tools are classified by command.
FILE_MUTATION_TOOLS = frozenset(
    {
        # host edit/write surfaces (mirror of access_gate._RAW_EDIT_TOOLS)
        "edit",
        "update",
        "write",
        "patch",
        "apply_patch",
        "multiedit",
        "notebookedit",
        "str_replace_based_edit_tool",
        # AIDOCS ai_* file mutators
        "ai_create_file",
        "ai_replace",
        "ai_batch_edit",
        "ai_insert_lines",
    },
)

# ai_* tools that READ a file by a path-shaped key — excluded from the
# ai_* deny-by-default so a read of a control-plane file is not flagged.
# Missing one only costs a (fail-safe) confirm on that read; never a
# bypass. ai_* tools that take no path key never reach this check.
AI_READ_PATH_TOOLS = frozenset(
    {
        "ai_get_lines",
        "ai_bundle",
        "ai_get_symbol_snippet",
        "ai_get_symbol_info",
        "ai_get_outline",
        "ai_get_dependencies",
        "ai_read_raw",
        "ai_read_pdf",
        "ai_read_excel",
        "ai_read_docx",
        "ai_read_jsonl",
        "ai_read_sqlite",
        "ai_get_module_files",
    },
)

# Universally-safe host READ tools — the ONLY tools allowed to carry a
# path without an anti-coup verdict when the authority is UNAVAILABLE.
# Kept tiny + stable so the fail-closed fallback needs no mutation list.
SAFE_READ_TOOLS = frozenset(
    {
        "read",
        "grep",
        "glob",
        "ls",
        "notebookread",
    },
)


def _norm_tool(tool_name: str) -> str:
    n = (tool_name or "").strip().lower()
    for p in ("mcp__aidocs__", "mcp__"):
        if n.startswith(p):
            return n[len(p) :]
    return n


def _extract_simple_path(tool_input: dict) -> str | None:
    for k in _PATH_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def classify_tool(tool_name: str, tool_input: dict | None) -> CoupFinding | None:
    """The ONE anti-coup verdict for any agent-invoked tool call.

    Returns the worst CoupFinding (deny/confirm) or None (not a
    control-plane mutation → allow). Exec tools are classified by command;
    file-mutation tools by target path. Coverage that defeats bypass:
      * known host edit/write tools (FILE_MUTATION_TOOLS), and
      * ANY ai_* tool with a path that is not a known ai_* reader —
        so an UNKNOWN ai_* writer cannot slip past the verdict.
    Reads are never coups. Fails CLOSED: classification errors return a
    DENY 'uncertain' finding.
    """
    ti = tool_input or {}
    try:
        cmd = ti.get("command")
        if isinstance(cmd, str) and cmd.strip():
            f = classify_command(cmd)
            if f is not None:
                return f
        nt = _norm_tool(tool_name)
        path = _extract_simple_path(ti)
        if path is None:
            return None
        if nt in FILE_MUTATION_TOOLS:
            return finding_for_path(path)
        # ai_* namespace deny-by-default: a non-read ai_* tool carrying a
        # path is treated as a potential writer (covers unknown/new ai_*
        # mutators). Known ai_* readers are excluded.
        if nt.startswith("ai_") and nt not in AI_READ_PATH_TOOLS:
            return finding_for_path(path)
        return None
    except Exception:
        return _kind("uncertain", "classification_error", reason="anti-coup classification failed")
