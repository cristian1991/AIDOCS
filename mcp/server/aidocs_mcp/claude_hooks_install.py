"""Canonical installer for AIDOCS hooks in Claude Code's ~/.claude/settings.json.

ONE source of truth for how AIDOCS wires its enforcement/audit hooks into the
host's global settings, so the install path (`aidocs setup`) and the passive
on-hook drift repair can't drift apart.

Why this exists (the bug it seals): a hook command whose python path contains
single backslashes (e.g. ``C:\\Python314\\python.exe`` written without JSON
escaping) makes settings.json INVALID JSON. Claude Code's lenient parser then
launches a mangled command (`C:Python314python.exe` → "command not found"), so
enforcement silently dies AND the broken hook can never run to fix itself. A
strict ``json.loads`` repair can't even parse the file. This installer:

  * always writes the python path with FORWARD SLASHES (never mangles),
  * SELF-HEALS an unparseable file by forward-slashing backslashes inside
    AIDOCS hook command strings via raw-text repair BEFORE re-parsing — so
    user keys are preserved instead of nuked,
  * is idempotent (marker-based remove+re-add; re-runs converge),
  * backs up before any corruption-driven rewrite, and writes valid JSON.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

_PRE_MATCHER = (
    "Read|Edit|Write|Glob|Grep|Bash|MultiEdit|Patch|Search|ListDir|Task|"
    "NotebookEdit|ScheduleWakeup|PowerShell|pwsh|Pwsh|cmd|wsl|Monitor|mcp__.*"
)
_POST_MATCHER = (
    "Read|Edit|Write|Glob|Grep|Bash|MultiEdit|Patch|Search|ListDir|Task|"
    "NotebookEdit|TodoWrite|ScheduleWakeup|PowerShell|pwsh|Pwsh|cmd|wsl|"
    "Monitor|mcp__.*"
)

# event -> (statusMessage, timeout, matcher | None). THE canonical AIDOCS hook
# set; both setup and passive repair register exactly these.
CANONICAL_HOOKS: tuple[tuple[str, str, int, str | None], ...] = (
    ("SessionStart", "AIDOCS startup routing", 30, None),
    ("UserPromptSubmit", "AIDOCS prompt routing", 30, None),
    ("PreToolUse", "AIDOCS tool guardrails", 30, _PRE_MATCHER),
    ("PostToolUse", "AIDOCS tool audit", 15, _POST_MATCHER),
    ("PostCompact", "AIDOCS context reset", 15, None),
    ("Stop", "AIDOCS turn audit", 10, None),
    ("SubagentStop", "AIDOCS subagent turn audit", 10, None),
)

# Raw-text fix for an invalid-JSON file: forward-slash backslashes that appear
# INSIDE an AIDOCS hook command string value (the only place they legitimately
# appear, as a Windows python path). Leaves every other byte untouched.
_AIDOCS_CMD_RE = re.compile(r'("command"\s*:\s*")([^"]*aidocs_mcp[^"]*)(")')


# AIDOCS-owned interpreter resolution + integrity verification + provisioning
# all live in ``runtime_provisioner``; this module consults it. A security-grade
# gate must NOT depend on whatever ambient python happened to run setup — that
# interpreter can vanish, get a different aidocs_mcp, or be shadowed.
def resolve_aidocs_interpreter(
    home: Path | None = None,
    env: dict | None = None,
) -> dict:
    """Resolve the interpreter the AIDOCS hook should run under, preferring an
    AIDOCS-OWNED, pinned interpreter over ambient sys.executable. Returns
    {path, owned, source, tier, degraded}. Existence-based (cheap, no subprocess)
    and OS-agnostic; full integrity verification + provisioning live in
    ``runtime_provisioner``. ``tier`` ∈ operator_pin|standalone|venv|ambient.
    """
    import os

    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    from .runtime_provisioner import resolve_runtime

    r = resolve_runtime(base, e, verify=False, allow_ambient=True)
    return {
        "path": r["path"],
        "owned": bool(r["owned"]),
        "source": r["source"],
        "tier": r["tier"],
        "degraded": bool(r.get("degraded")),
    }


def _classify_owned(python_path: str, home: Path, env: dict | None) -> bool:
    """Is ``python_path`` an AIDOCS-owned interpreter (operator pin or a runtime
    under ~/.aidocs/runtime), as opposed to ambient? Path-based, no subprocess.
    """
    import os

    from .runtime_provisioner import runtime_root

    e = env if env is not None else os.environ
    p = str(python_path or "").replace("\\", "/").rstrip("/")
    pin = str(e.get("AIDOCS_PYTHON") or "").strip().replace("\\", "/")
    if pin and p == pin.rstrip("/"):
        return True
    root = str(runtime_root(home)).replace("\\", "/").rstrip("/")
    return bool(p) and (p == root or p.startswith(root + "/"))


def hook_command(python_path: str | None = None) -> str:
    """The AIDOCS hook command with a FORWARD-SLASH python path (never mangles
    on Claude Code's Windows shell-string parsing). When no path is given, the
    AIDOCS-owned interpreter is preferred over ambient sys.executable.
    """
    py = str(python_path or resolve_aidocs_interpreter()["path"]).replace("\\", "/")
    return f"{py} -m aidocs_mcp.claude_hook"


def repair_raw_aidocs_backslashes(raw: str) -> str:
    """Return ``raw`` with backslashes inside AIDOCS hook command strings
    forward-slashed — turning an invalid-JSON single-backslash settings file
    back into valid JSON while preserving all other content.
    """
    return _AIDOCS_CMD_RE.sub(
        lambda m: m.group(1) + m.group(2).replace("\\", "/") + m.group(3),
        raw,
    )


def _is_aidocs_group(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks", []) or []:
        if not isinstance(h, dict):
            continue
        cmd = str(h.get("command", ""))
        status = str(h.get("statusMessage", ""))
        if "aidocs_mcp" in cmd or "claude-hook" in cmd or status.startswith("AIDOCS "):
            return True
    return False


def _without_aidocs(groups: object) -> list:
    if not groups:
        return []
    seq = groups if isinstance(groups, list) else [groups]
    return [g for g in seq if isinstance(g, dict) and not _is_aidocs_group(g)]


def _group(cmd: str, status: str, timeout: int, matcher: str | None) -> dict:
    g: dict = {
        "hooks": [{"type": "command", "command": cmd, "timeout": timeout, "statusMessage": status}],
    }
    if matcher:
        g["matcher"] = matcher
    return g


def _atomic_write(path: Path, text: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_UNSET = object()


def ensure_claude_hooks(
    python_path: str | None = None,
    home: Path | None = None,
    *,
    allow_ambient: bool = False,
    env: dict | None = None,
    runner=None,
    expected_version=_UNSET,
    verify: bool = True,
) -> dict:
    """Idempotently install the canonical AIDOCS hooks into the host's
    ~/.claude/settings.json, self-healing a corrupt file and preserving the
    user's other settings. Returns {ok, path, action, command, events, backup,
    owned, tier}. ``action`` ∈ created|updated|repaired|reset|refused_ambient|
    refused_unverified.

    ENFORCEMENT BOUNDARY (two gates, both fail-closed):
      1. The interpreter must be AIDOCS-OWNED (operator pin / provisioned
         runtime). Ambient sys.executable can vanish or be shadowed, silently
         killing the gate.
      2. An owned-LOOKING interpreter must be TRUSTWORTHY: freshly verified to
         import the EXPECTED aidocs_mcp version (not just any aidocs_mcp), OR
         backed by a still-valid verified manifest. A broken/decoy owned runtime
         is refused (``refused_unverified``).
    Either failure refuses the install UNLESS an explicit degraded/dev escape is
    given: ``allow_ambient=True`` or env ``AIDOCS_ALLOW_AMBIENT_HOOKS`` truthy.
    """
    import os

    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    from .runtime_provisioner import expected_aidocs_version, owned_runtime_trust

    exp = expected_aidocs_version() if expected_version is _UNSET else expected_version
    interp = resolve_aidocs_interpreter(home=base, env=e)
    if python_path is None:
        python_path = interp["path"]
        owned = bool(interp["owned"])
        tier = interp["tier"]
    else:
        owned = _classify_owned(python_path, base, e)
        tier = interp["tier"] if owned else "ambient"

    escape = bool(allow_ambient) or str(
        e.get("AIDOCS_ALLOW_AMBIENT_HOOKS") or "",
    ).strip().lower() in ("1", "true", "yes", "on")

    # Gate 2: an owned interpreter must prove it carries the expected law package
    # before we pin enforcement to it. Skipped only under the explicit escape.
    trust_reason = ""
    if owned and verify and not escape:
        trust = owned_runtime_trust(python_path, base, runner=runner, expected_version=exp)
        if not trust["ok"]:
            owned = False
            tier = "unverified"
            trust_reason = trust["reason"]

    if not owned and not escape:
        unverified = tier == "unverified"
        return {
            "ok": False,
            "action": "refused_unverified" if unverified else "refused_ambient",
            "owned": False,
            "tier": tier,
            "command": "",
            "events": [],
            "backup": "",
            "path": str(base / ".claude" / "settings.json"),
            "reason": (
                "Refusing to pin enforcement hooks to an owned-looking "
                f"interpreter ({python_path}) that does not verify the expected "
                f"aidocs_mcp version: {trust_reason}. Reprovision with "
                "`aidocs runtime --fix`."
                if unverified
                else "Refusing to install enforcement hooks against ambient "
                f"sys.executable ({python_path}). Provision an AIDOCS-owned "
                "runtime (`aidocs runtime --fix`) or pass an explicit "
                "degraded/dev escape (allow_ambient / AIDOCS_ALLOW_AMBIENT_HOOKS)."
            ),
        }
    path = base / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    action = "created"
    backup = ""
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
            settings = parsed if isinstance(parsed, dict) else {}
            action = "updated"
        except Exception:
            # SELF-HEAL: forward-slash AIDOCS command backslashes, re-parse.
            repaired = repair_raw_aidocs_backslashes(raw)
            try:
                parsed = json.loads(repaired)
                settings = parsed if isinstance(parsed, dict) else {}
                action = "repaired"
            except Exception:
                # Unrecoverable (e.g. fully commented-out) — back up, start
                # fresh so enforcement works rather than staying broken.
                backup = str(path) + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
                try:
                    Path(backup).write_text(raw, encoding="utf-8")
                except OSError:
                    backup = ""
                settings = {}
                action = "reset"

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    cmd = hook_command(python_path)
    for event, status, timeout, matcher in CANONICAL_HOOKS:
        hooks[event] = _without_aidocs(hooks.get(event)) + [_group(cmd, status, timeout, matcher)]

    _atomic_write(path, json.dumps(settings, indent=2) + "\n")
    return {
        "ok": True,
        "path": str(path),
        "action": action,
        "command": cmd,
        "events": [ev for ev, *_ in CANONICAL_HOOKS],
        "backup": backup,
        "owned": owned,
        "tier": tier,
        "degraded": (not owned) and escape,
    }
