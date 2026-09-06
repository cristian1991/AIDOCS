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

# ── THE ONE DECLARATION (#644) ─────────────────────────────────────────────
#
# THREE surfaces need this token set, and they DRIFTED — each in a DIFFERENT
# direction — because each held its own copy:
#
#   1. this module's ``_PRE_MATCHER`` / ``_POST_MATCHER`` — what `aidocs setup`
#      writes into a FRESH ~/.claude/settings.json,
#   2. ``claude_hook._REQUIRED_PRETOOLUSE_MATCHER_TOKENS`` — what passive
#      self-repair UNIONS into an EXISTING install on every hook run,
#   3. ``core/hooks/hooks.json`` — the shipped manifest a fresh checkout gets.
#
# Measured drift at the moment of this fix:
#   * (2) carried `Task` but NOT `Agent` — so the mechanism whose ENTIRE JOB is
#     healing stale installs could not heal the 2026-07-09 "agents cheat"
#     bypass, and reported success while failing. It also lacked `Skill`.
#   * (3) carried `Agent`/`Skill` but NOT `mcp__.*` — so a fresh install never
#     gated AIDOCS's OWN MCP tool surface at PreToolUse.
#   * (3) alone carried `ApplyPatch`; (2) alone carried `Cmd`/`Wsl`;
#     (1) alone carried both `Agent`/`Skill` and `mcp__.*`.
#
# NONE of those differences was deliberate. Every one is a token that landed in
# whichever copy its author happened to have open. So the tuple below is now
# THE declaration and the other two surfaces DERIVE from it: a synchronised
# copy drifts the moment someone edits one, a derived consumer cannot.
# hooks.json is a static file that cannot import, so its parity is a CONTRACT
# TEST (tests/security/test_matcher_rival_definitions_drift_644.py) that NAMES
# this symbol rather than restating its value.
#
# ADDING a token here WIDENS governance — more tool calls reach the gate.
# REMOVING one disables enforcement for that tool, for every user, silently.
PRETOOLUSE_MATCHER_TOKENS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash",
    "MultiEdit",
    "Patch",
    # `ApplyPatch` must be listed SEPARATELY from `Patch`: the host applies the
    # matcher with re.match semantics (anchored at the start), so `Patch` alone
    # never matches a tool named `ApplyPatch`.
    "ApplyPatch",
    "Search",
    "ListDir",
    # Task|Agent: Claude Code's subagent-spawn tool is surfaced as `Agent`;
    # older builds / other hosts call it `Task`. The gate logic already handled
    # BOTH (tool_gate_service.agent_dispatch_brief fires for tool_name in
    # {task, agent}), but the matcher listed only `Task` — so on current CC the
    # PreToolUse hook never fired for an `Agent` spawn and the brief gate was
    # silently bypassed ("agents cheat"): spawns escaped gating, auditing, and
    # parent-linkage. Both names are matched so every subagent dispatch is
    # governed. (2026-07-09)
    "Task",
    "Agent",
    # `Skill` added 2026-07-30 (operator: "'skills' tool can and should be
    # redirected to ai_skill logic"). SAME defect class as the Agent hole
    # above: the host `Skill` tool answered "Unknown skill: aidocs-doctrine"
    # for the project's OWN law while ai_skill(mode='read') returned it in
    # full, and the PreToolUse hook never fired because the matcher did not
    # name the tool. A redirect is impossible until the event ARRIVES.
    # Read-path only — this token opens no write door.
    "Skill",
    "NotebookEdit",
    # 2026-04-21: ScheduleWakeup MUST match the PreToolUse matcher or the
    # force-wakeup guard's own stamp path never fires when the agent dutifully
    # calls it, creating a catch-22 where every tool is refused and the
    # operator has to manually stamp the sqlite column. See session 2026-04-18
    # for the live repro.
    "ScheduleWakeup",
    # 2026-04-27 (#68): shell-equivalent host tools must match the PreToolUse
    # matcher or destructive ops slip through entirely. Pre-fix, PowerShell
    # tool calls bypassed the gate cascade — the MCP server had
    # `_RAW_SHELL_TOOLS = {"bash","powershell","pwsh","cmd","wsl","monitor"}`
    # registered, but Claude Code never even invoked the hook for non-matched
    # tool names. Verified live: `Remove-Item -LiteralPath ... -Force` deleted
    # a tempfile with zero gate fire (handoff issue #1). BOTH case spellings
    # are kept: the host matches the tool NAME with a case-sensitive regex and
    # different hosts spell these differently.
    "PowerShell",
    "pwsh",
    "Pwsh",
    "cmd",
    "Cmd",
    "wsl",
    "Wsl",
    "Monitor",
    # Every MCP tool on every server — AIDOCS's own surface included.
    "mcp__.*",
)

# The ONE declared exception, named rather than flattened: `TodoWrite` has a
# PostToolUse duty (the todo auto-bridge) and no PreToolUse duty. PostToolUse
# is otherwise DERIVED from the set above — it is not a second list.
POSTTOOLUSE_ONLY_TOKENS: tuple[str, ...] = ("TodoWrite",)
POSTTOOLUSE_MATCHER_TOKENS: tuple[str, ...] = (
    PRETOOLUSE_MATCHER_TOKENS + POSTTOOLUSE_ONLY_TOKENS
)

# Derived consumer #1 — the regex alternations `aidocs setup` writes.
_PRE_MATCHER = "|".join(PRETOOLUSE_MATCHER_TOKENS)
_POST_MATCHER = "|".join(POSTTOOLUSE_MATCHER_TOKENS)

# event -> (statusMessage, timeout, matcher | None). THE canonical AIDOCS hook
# set; both setup and passive repair register exactly these.
CANONICAL_HOOKS: tuple[tuple[str, str, int, str | None], ...] = (
    ("SessionStart", "AIDOCS startup routing", 30, None),
    ("UserPromptSubmit", "AIDOCS prompt routing", 30, None),
    ("PreToolUse", "AIDOCS tool guardrails", 30, _PRE_MATCHER),
    ("PostToolUse", "AIDOCS tool audit", 15, _POST_MATCHER),
    ("PostCompact", "AIDOCS context reset", 15, None),
    ("Stop", "AIDOCS turn audit", 10, None),
    # MEASURED 2026-08-22 (CC 2.1.239): SubagentStart is the ONLY event that
    # announces a subagent BEFORE it acts, and its payload carries `agent_id` --
    # the sole axis separating a subagent from its parent (`session_id` and
    # `transcript_path` are both the PARENT's). Without it N subagents derive one
    # identity and their strikes pool onto the conductor: three lane agents with
    # one strike each read as one actor with three, and the lockdown landed on a
    # conductor that had done nothing (2026-08-21).
    #
    # Matcher stays None ON PURPOSE: the host matches this event on `agent_type`,
    # so any matcher would govern only some agent types, and AIDOCS must see
    # every spawn.
    #
    # NOT once per agent: measured re-firing for the SAME agent_id when that
    # agent resumes after its own child completes (22:06:26 and 22:06:34 for one
    # agent). Handlers must upsert, never insert.
    ("SubagentStart", "AIDOCS subagent routing", 15, None),
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


def windowless_python(python_path: str) -> str:
    """#333A: prefer the GUI-subsystem ``pythonw.exe`` SIBLING of a Windows
    ``python.exe`` so hook spawns never flash a console window (same runtime,
    same directory — mirrors ``aidocs_service.windowless_python``, which is
    sys.executable-bound; this variant is path-parameterized for the installer).

    GUARDED, gate-over-cosmetics: returns the input UNCHANGED unless the
    sibling file actually exists on disk — never a PATH lookup, never a guess —
    so the swap cannot point enforcement at a missing binary (a hook that fails
    to launch is a security gate that silently fails open). Claude Code PIPES
    hook stdio, so pythonw still delivers the JSON verdict; pinned by
    tests/host/test_claude_hook_windowless.py.
    """
    p = str(python_path or "")
    fwd = p.replace("\\", "/")
    if fwd.rsplit("/", 1)[-1].lower() != "python.exe":
        return p
    sibling = Path(fwd).with_name("pythonw.exe")
    try:
        if sibling.is_file():
            return str(sibling)
    except OSError:
        pass
    return p


# ── fail-closed launcher shim (#616) ─────────────────────────────────────
# The hook entry point must NOT live inside the package the runtime refresh
# reinstalls: during the swap window `-m aidocs_mcp.claude_hook` dies inside
# runpy before any gate code loads, and Claude Code treats that crash as
# NON-BLOCKING — tool calls proceed ungoverned (measured 2026-07-29). The shim
# is a stdlib-only copy of claude_hook_shim.py placed OUTSIDE site-packages,
# where pip cannot take it away; it delegates verbatim when the package loads
# and REFUSES (deny verdict + breadcrumb) when it does not.
# The basename deliberately contains "aidocs_mcp" so every self-repair matcher
# (_is_aidocs_group, _AIDOCS_CMD_RE, the backslash repair) keeps recognizing
# shim-form commands as ours.
SHIM_BASENAME = "aidocs_mcp_claude_hook_shim.py"


def shim_path(home: Path | None = None) -> Path:
    """Where the deployed shim copy lives for ``home`` (under runtime_root,
    NEVER under the venv's site-packages)."""
    from .runtime_provisioner import runtime_root

    base = Path(home) if home else Path.home()
    return runtime_root(base) / SHIM_BASENAME


def shim_source() -> str:
    """The shim's source text — read from the packaged module so the deployed
    copy is byte-identical to the audited, tested file."""
    return Path(__file__).with_name("claude_hook_shim.py").read_text(encoding="utf-8")


def ensure_hook_shim(home: Path | None = None) -> Path | None:
    """Place (or heal) the fail-closed launcher copy outside site-packages.

    Idempotent: an up-to-date copy is left alone; a stale/tampered one is
    atomically rewritten from the packaged source. Returns the shim path, or
    ``None`` when it could not be placed — callers must then keep the ``-m``
    command rather than point the hook at a missing file (same
    gate-over-cosmetics guard as ``windowless_python``). Permissions are set
    EXPLICITLY: the ambient umask differs across build hosts.
    """
    import os

    try:
        dest = shim_path(home)
        src = shim_source()
        try:
            if dest.is_file() and dest.read_text(encoding="utf-8") == src:
                return dest
        except OSError:
            pass
        _atomic_write(dest, src)
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
        return dest
    except Exception:  # noqa: BLE001 — placement is best-effort; caller falls back
        return None


def hook_command(python_path: str | None = None, shim: Path | str | None = None) -> str:
    """The AIDOCS hook command with a FORWARD-SLASH python path (never mangles
    on Claude Code's Windows shell-string parsing). When no path is given, the
    AIDOCS-owned interpreter is preferred over ambient sys.executable. On
    Windows the console interpreter is swapped for its pythonw.exe sibling
    when (and only when) that sibling exists — see ``windowless_python``.

    ``shim`` (#616): a placed fail-closed launcher to target instead of
    ``-m aidocs_mcp.claude_hook``. A PURE FORMATTER on this axis by design —
    it never writes the shim itself, so calling it cannot mutate any home;
    placement belongs to ``ensure_claude_hooks`` / the passive self-repair,
    which own their filesystems. ``None`` keeps the module form (never point
    a hook at a file nobody placed).
    """
    py = str(python_path or resolve_aidocs_interpreter()["path"])
    py = windowless_python(py).replace("\\", "/")
    if shim is not None:
        fwd_shim = str(shim).replace("\\", "/")
        return f"{py} {fwd_shim}"
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


def claude_hooks_status(home: Path | None = None) -> dict:
    """READ-ONLY: is the AIDOCS hook layer actually installed for this host?

    #838. A broker that is UP and IDLE looks exactly like one that is UP and
    WORKING. Measured 2026-08-19 on the operator's own machine: the broker was
    up, listening and custody-verified, and its 256-sample ring held ZERO rows
    after ninety minutes of heavy tool use -- because ~/.claude/settings.json
    declared no hooks at all. Nothing anywhere said so. Health reported the
    broker; nothing reported that nobody was calling it.

    WHAT IS AND IS NOT COVERED, because the distinction is easy to invert: the
    MCP-side gates are independent of this and keep working (they refused raw
    reads, mojibake and un-acked doctrine edits throughout that same session).
    The hook layer governs the HOST-NATIVE tools -- Bash, Write, Edit, Glob --
    so its absence means those ran ungoverned by AIDOCS.

    REUSES THE INSTALLER'S OWN PREDICATE (`_is_aidocs_group`) and its own
    CANONICAL_HOOKS list rather than re-deriving what an AIDOCS hook looks like.
    A second definition of "is this ours" would drift from the installer, and
    the copy nobody exercises is the one that rots.

    NEVER RAISES and never writes: a corrupt or absent settings file is a
    RESULT, not an exception -- reported as installed=False with a reason, which
    is exactly the state an operator needs told.

    Returns {installed, path, present, missing, extra_events, reason}.
    `installed` is True only when EVERY canonical event carries an AIDOCS group;
    a partial install is reported as False WITH the gap named, because a
    half-installed enforcement layer is the more dangerous state -- it looks
    configured.
    """
    base = Path(home) if home else Path.home()
    path = base / ".claude" / "settings.json"
    expected = [ev for ev, _s, _t, _m in CANONICAL_HOOKS]
    out: dict = {
        "installed": False,
        "path": str(path),
        "present": [],
        "missing": list(expected),
        "extra_events": [],
        "reason": "",
    }
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        out["reason"] = f"no host settings file at {path}"
        return out
    try:
        data = json.loads(raw)
    except Exception:
        # The installer already knows this file can be left invalid by a raw
        # backslash write; say so plainly rather than reporting "not installed",
        # which would send the reader to the wrong remedy.
        out["reason"] = "host settings file is not valid JSON (see repair_raw_aidocs_backslashes)"
        return out
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        out["reason"] = "no hooks declared in the host settings file"
        return out
    present = [
        ev
        for ev in expected
        if any(_is_aidocs_group(g) for g in (hooks.get(ev) or []) if isinstance(g, dict))
    ]
    out["present"] = present
    out["missing"] = [ev for ev in expected if ev not in present]
    out["extra_events"] = [
        ev
        for ev, groups in hooks.items()
        if ev not in expected
        and any(_is_aidocs_group(g) for g in (groups or []) if isinstance(g, dict))
    ]
    out["installed"] = not out["missing"]
    if not present:
        out["reason"] = "hooks are declared, but none of them are AIDOCS hooks"
    elif out["missing"]:
        out["reason"] = f"PARTIAL install - missing: {', '.join(out['missing'])}"

    # ── shim freshness (#973) ───────────────────────────────────────────────
    # A host can carry every canonical event, every group ours, and still be
    # running an enforcement shim four days stale — which is precisely what
    # happened: the deployed copy predated #932 and emitted its pre-fix wording
    # while THIS function reported installed=True. Declaring the hooks and
    # EXECUTING the current code are two different facts, and reporting only the
    # first is the same "it looks configured" trap the partial-install branch
    # above already refuses to fall into.
    #
    # Freshness is only load-bearing when a command actually POINTS at the shim.
    # A host still on the `-m aidocs_mcp.claude_hook` form does not execute the
    # deployed file, so a stale copy there is inert and must not be reported as
    # a health failure it is not.
    shim = shim_path(base)
    referenced = SHIM_BASENAME in raw
    out["shim_path"] = str(shim)
    out["shim_referenced"] = referenced
    try:
        deployed = shim.read_text(encoding="utf-8")
        out["shim_present"] = True
        out["shim_fresh"] = deployed == shim_source()
    except OSError:
        out["shim_present"] = False
        # UNKNOWABLE IS NOT FRESH. An unreadable shim that a command points at
        # is a gate that cannot speak at all; False is the fail-closed reading.
        out["shim_fresh"] = False
    if referenced and not out["shim_fresh"]:
        # NOT an unqualified healthy state (operator ruling 2026-08-30).
        out["installed"] = False
        detail = (
            "the deployed hook shim is STALE (differs from the packaged source)"
            if out["shim_present"]
            else "the deployed hook shim is MISSING"
        )
        out["reason"] = (
            f"{out['reason']} - " if out["reason"] else ""
        ) + (
            f"{detail} at {shim}, and the host hook commands EXECUTE it. "
            "Host-native tools are governed by that file, not by the installed "
            "package. It self-heals on the next hook run (ensure_hook_shim); if "
            "it does not, the hook is not being delegated at all."
        )
    return out


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
    # #616: place the fail-closed launcher under the SAME home the settings
    # are written for, then point every command at it. A failed placement
    # falls back to the -m form — a launch that works beats a fail-closed
    # design pointing at a file that is not there.
    cmd = hook_command(python_path, shim=ensure_hook_shim(base))
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


# ── CODEX + PLUGIN: the two writers #808 caught destroying operator files ──
#
# Codex uses the SAME hook-document shape as Claude Code, at ~/.codex/hooks.json.
# cli.py built that document from literals and wrote it WHOLE — no read of the
# existing file anywhere in the block, no backup — so every `aidocs setup`
# deleted whatever the operator had there.
#
# The merge below is deliberately the SAME read → merge → atomic-write shape
# ensure_claude_hooks already uses, and it REUSES that function's helpers rather
# than hand-rolling a second merger. This codebase has produced EIGHT
# twin-implementation defects in two weeks (#522 #774 #779 #781 #782 #786 #787
# plus the one the #808 audit found); a rival copy of the merge logic would be
# the ninth, and the twin that rots is always the one nobody tests.
CODEX_HOOKS: tuple[tuple[str, str, int, str | None], ...] = (
    ("SessionStart", "AIDOCS startup routing", 30, None),
    ("UserPromptSubmit", "AIDOCS prompt routing", 30, None),
    ("PreToolUse", "AIDOCS bash guardrails", 30, "Bash"),
)


def merge_aidocs_hook_groups(
    path: Path,
    cmd: str,
    specs: tuple[tuple[str, str, int, str | None], ...],
) -> dict:
    """Install AIDOCS hook groups into ``path`` without destroying what it holds.

    Everything AIDOCS did not author survives: unrelated top-level keys, hooks on
    events we do not own, and other tools' groups on events we DO own. Only
    AIDOCS-owned groups (``_is_aidocs_group``) are stripped and re-added, which
    is what makes a re-run converge instead of accumulating copies.

    Duplicates are not cosmetic: hook entries MERGE across settings levels and
    every copy fires, so an accumulating group multiplies the governed hook path
    — measured at ~20 separate sqlite write transactions per call in #754.

    A document we cannot merge into (unparseable, or valid JSON that is not an
    object) is BACKED UP before being replaced. The operator's bytes are never
    dropped silently; the backup path comes back in the receipt so the caller can
    say so out loud.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc: dict = {}
    action = "created"
    backup = ""
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            doc = parsed
            action = "updated"
        else:
            backup = str(path) + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                Path(backup).write_text(raw, encoding="utf-8")
            except OSError:
                backup = ""
            doc = {}
            action = "reset"

    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = doc["hooks"] = {}
    for event, status, timeout, matcher in specs:
        hooks[event] = _without_aidocs(hooks.get(event)) + [_group(cmd, status, timeout, matcher)]

    _atomic_write(path, json.dumps(doc, indent=2) + "\n")
    return {
        "ok": True,
        "path": str(path),
        "action": action,
        "command": cmd,
        "events": [ev for ev, *_ in specs],
        "backup": backup,
    }


def install_file_preserving_user_copy(source: Path, target: Path) -> dict:
    """Copy ``source`` onto ``target`` without silently destroying a customized target.

    Restores the half of ``core/scripts/install_manifest.py`` the live path lost:
    that installer always wrote a ``.backup`` before overwriting (:107-121) and
    could return ``skip_user_modified`` (:85-86). The live path had degraded to a
    bare ``copy2`` with neither.

    ONLY THE DATA-LOSS HALF IS RESTORED HERE, and that is a decision rather than
    an oversight. A true ``skip_user_modified`` needs a manifest of
    previously-installed hashes to tell "the user edited this" apart from "AIDOCS
    shipped a new version" — and that manifest module lives under core/scripts/,
    which is not importable from a pip install. That is precisely why the live
    path degraded in the first place, so re-importing it would reintroduce the
    original breakage. Recorded as an open residual on #808 instead of faked:
    a customized plugin is still replaced on setup, but it is now always
    recoverable from the backup rather than destroyed.

    An identical target is left completely alone — no rewrite, no backup churn,
    no mtime change.
    """
    source, target = Path(source), Path(target)
    new = source.read_bytes()

    if target.is_file():
        current = target.read_bytes()
        if current == new:
            return {"ok": True, "path": str(target), "action": "unchanged", "backup": ""}
        backup_path: Path | None = Path(str(target) + ".backup")
        try:
            backup_path.write_bytes(current)
        except OSError:
            backup_path = None
        target.write_bytes(new)
        return {
            "ok": True,
            "path": str(target),
            "action": "updated",
            "backup": str(backup_path) if backup_path else "",
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(new)
    return {"ok": True, "path": str(target), "action": "installed", "backup": ""}
