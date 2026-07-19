"""Hook bootstrap — break the self-repair deadlock from OUTSIDE the hook (#364).

THE BUG THIS SEALS: `claude_hook._self_repair_settings_json()` heals a damaged
hook config, but it only runs WHEN A HOOK FIRES. If the `hooks` key is removed
from ~/.claude/settings.json (settings reset, /config edit, upgrade, "turned
off temporarily"), no hook ever fires again, self-repair never runs, and the
host-level gate stays OFF forever — while every surface reads green. Found
live 2026-07-13: an entire multi-war session ran with host governance OFF.

THE SEAM: the MCP server is the one component that keeps running when the
hooks are dead — the agent still calls tools. So on every governed tool call
for a COMMISSIONED project (the notification_injector rail, the same proven
rail gate_health rides), this module verifies the host hook wiring and:

  * HEALS it when broken — via `claude_hooks_install.ensure_claude_hooks`,
    the SAME canonical installer `aidocs setup` uses (Article XXII: one
    installer, never a fork). That installer merges surgically: it removes
    only AIDOCS-owned groups and re-adds the canonical set; user hooks and
    every other settings key are preserved byte-for-byte.
  * If healing is IMPOSSIBLE (ambient/unverified interpreter refusal, write
    failure), it REFUSES LOUDLY on the rail with the exact repair command —
    a managed session never silently runs ungated.

CHEAP BY DESIGN: the healthy path memoizes on the settings.json stat
signature (mtime_ns, size) — one os.stat per call, no read, no parse, no
subprocess. Any edit or removal of the file changes the signature and forces
a full re-verify on the NEXT governed call. Heal attempts are rate-limited
(HEAL_RETRY_COOLDOWN_S) so a persistent failure cannot turn into an installer
loop — but the refusal notice is re-emitted on EVERY call while degraded.

NO BYPASS: nothing here reads agent-controllable input. There is no env
kill-switch; the only parameters exist for test injection. A crash in this
module degrades to the caller's fail-quiet rail handling — and gate_health's
independent hook-silence probe still fires — never to a fabricated all-clear.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

REPAIR_COMMAND = "aidocs setup"
HEAL_RETRY_COOLDOWN_S = 300.0

_LOCK = threading.Lock()
# Healthy fast-path memo: stat signature of settings.json last verified good.
_HEALTHY_SIG: tuple[int, int] | None = None
# Commissioned-project memo: project_root str -> bool.
_COMMISSIONED: dict[str, bool] = {}
# Degraded memo: {"ts": float, "notice": str} — refusal persists between heal
# attempts so the rail stays loud without re-running the installer per call.
_DEGRADED: dict | None = None


def _reset_for_tests() -> None:
    global _HEALTHY_SIG, _DEGRADED
    with _LOCK:
        _HEALTHY_SIG = None
        _DEGRADED = None
        _COMMISSIONED.clear()


def _default_home() -> Path:
    """Seam for the host home directory. Tests isolate the developer's REAL
    ~/.claude via conftest's autouse `isolate_hook_bootstrap` fixture, which
    patches THIS function — production always resolves the true home.
    """
    return Path.home()


def _settings_path(home: Path | None = None) -> Path:
    base = Path(home) if home else _default_home()
    return base / ".claude" / "settings.json"


def _stat_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _is_commissioned(project_root: Path) -> bool:
    """AIDOCS-commissioned project marker — the bootstrap acts ONLY for
    governed projects, so unit tests with tmp roots (and un-commissioned
    checkouts) never trigger a write to the developer's real settings.
    """
    key = str(project_root)
    hit = _COMMISSIONED.get(key)
    if hit is not None:
        return hit
    try:
        ok = (Path(project_root) / ".MEMORY" / ".aidocs" / "index.aidocs").is_file()
    except OSError:
        ok = False
    _COMMISSIONED[key] = ok
    return ok


def missing_hook_events(settings: object) -> list[str]:
    """Which canonical AIDOCS hook events lack an AIDOCS-owned group?

    Delegates AIDOCS-group recognition and the canonical event list to
    claude_hooks_install — the single source of truth — so this check can
    never drift from what the installer actually writes.
    """
    from .claude_hooks_install import CANONICAL_HOOKS, _is_aidocs_group

    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    missing: list[str] = []
    for event, *_rest in CANONICAL_HOOKS:
        groups = hooks.get(event) if isinstance(hooks, dict) else None
        seq = groups if isinstance(groups, list) else ([groups] if groups else [])
        if not any(_is_aidocs_group(g) for g in seq):
            missing.append(event)
    return missing


def _verify(path: Path) -> list[str]:
    """Full verify: read + parse + check every canonical event. Returns the
    list of missing events ([] == healthy). Unreadable/unparseable counts as
    everything missing — absence of a check must never read as a pass.
    """
    from .claude_hooks_install import CANONICAL_HOOKS

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return [ev for ev, *_ in CANONICAL_HOOKS]
    try:
        cfg = json.loads(raw)
    except Exception:
        # Same lenient lens the passive self-repair uses: a backslash-mangled
        # file may still carry the wiring; judge the repaired text.
        from .claude_hooks_install import repair_raw_aidocs_backslashes

        try:
            cfg = json.loads(repair_raw_aidocs_backslashes(raw))
        except Exception:
            return [ev for ev, *_ in CANONICAL_HOOKS]
    return missing_hook_events(cfg)


def _refusal_notice(reason: str) -> str:
    return (
        "🛑 HOST HOOK BOOTSTRAP FAILED — this Claude Code session is running "
        "WITHOUT host-level AIDOCS gating (the enforcement hooks are missing "
        "from ~/.claude/settings.json and could not be re-installed: "
        f"{reason}). MCP-side gates still apply, but native Read/Edit/Bash "
        "and subagent spawns are UNGOVERNED. Repair NOW: run "
        f"`{REPAIR_COMMAND}` (if the runtime is unverified, run "
        "`aidocs runtime --fix` first), then confirm gate health shows "
        "GATE ALIVE."
    )


def _healed_notice(missing: list[str], result: dict) -> str:
    return (
        "🔧 HOST HOOK BOOTSTRAP: the AIDOCS enforcement hooks were missing "
        f"from {result.get('path', '~/.claude/settings.json')} "
        f"(events: {', '.join(missing)}) — re-installed them now "
        f"(action={result.get('action', '?')}). Only AIDOCS-owned hook "
        "entries were touched; user hooks and other settings are preserved. "
        "Hooks load at process start — restart Claude Code (or start the "
        "next session) for host-level gating to actually fire; gate health "
        "must then show GATE ALIVE."
    )


def ensure_host_hooks(
    project_root: Path,
    *,
    home: Path | None = None,
    installer=None,
    now=time.monotonic,
) -> dict | None:
    """Verify (and heal) the host hook wiring on a governed call.

    Returns None when healthy (or when the surface does not apply: project
    not commissioned, no ~/.claude directory on this host). Otherwise a dict:
      {"status": "healed",  "notice": str}  — wiring was broken, re-installed
      {"status": "refused", "notice": str}  — broken and healing impossible;
                                              the notice names the repair.
    Never raises.
    """
    global _HEALTHY_SIG, _DEGRADED
    try:
        if not _is_commissioned(Path(project_root)):
            return None
        path = _settings_path(home)
        try:
            if not path.parent.is_dir():
                # No ~/.claude at all — not a Claude Code host; nothing to
                # verify and nothing we should create.
                return None
        except OSError:
            return None

        with _LOCK:
            sig = _stat_sig(path)
            if sig is not None and sig == _HEALTHY_SIG:
                return None  # memoized healthy fast path: one os.stat, no read

            missing = _verify(path)
            if not missing:
                _HEALTHY_SIG = sig
                _DEGRADED = None
                return None

            # Broken. Heal via THE canonical installer (same code as
            # `aidocs setup`) — unless a recent attempt already failed.
            ts = float(now())
            if _DEGRADED is not None and ts - _DEGRADED["ts"] < HEAL_RETRY_COOLDOWN_S:
                return {"status": "refused", "notice": _DEGRADED["notice"]}

            ins = installer
            if ins is None:
                from .claude_hooks_install import ensure_claude_hooks as ins
            try:
                result = ins(home=home) if home is not None else ins()
            except Exception as exc:  # installer crash == healing impossible
                result = {"ok": False, "reason": f"installer error: {exc}"}

            if result.get("ok") and not _verify(path):
                _HEALTHY_SIG = _stat_sig(path)
                _DEGRADED = None
                return {"status": "healed", "notice": _healed_notice(missing, result)}

            notice = _refusal_notice(
                str(result.get("reason") or "installer reported failure")
            )
            _DEGRADED = {"ts": ts, "notice": notice}
            return {"status": "refused", "notice": notice}
    except Exception:
        # Fail-quiet here; gate_health's independent hook-silence probe is
        # the backstop that still screams if governance is actually off.
        return None
