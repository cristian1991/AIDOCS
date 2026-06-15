"""Batch 2.0-B0: native-execution PILOT gate (gate-opening drill).

DEPRECATED 2026-06-05 — DEMOTED to compatibility/telemetry. Native
execution is now the normal governed shell surface gated by the canonical
ai_run law (see shell_adapter); the PILOT flag / `pilot_native_allowed` no
longer authorize execution. `is_pilot_safe` survives only as a classifier/
evidence helper — do not reintroduce it as an authorization gate.

This is NOT broad native execution. It opens a tiny, read-only, pilot-safe
command class for the host-native bash provider, ONLY when every gate is
already satisfied (shell_enforcement_live + native_shell_provider_enabled +
verdict execute_native [⇒ enabled + capability-safe + output replacement +
proven identity + native_route=native] + structural gates passed +
no active freeze). On top of those, 2.0-B0 additionally requires:

  * tools.native_shell_execution_pilot = true
  * host == claude_code
  * provider == bash
  * command ∈ the pilot-safe allowlist (no chains, redirection, package
    managers, writes, deletes, network, scripts, env/secret reads).

Anything else that resolves to execute_native still renders native-deny +
ai_run fallback. No fallback-to-host-allow on error — callers fail closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Shell metacharacters / chaining / redirection / expansion that are NOT
# pilot-safe. Their presence anywhere disqualifies the command.
_FORBIDDEN_CHARS = set(";&|`$()<>{}[]*?~!\\=\n\r\t\"'")

# Exact (whitespace-normalized) pilot commands. ls flag combos are an
# EXPLICIT allowlist — no generic flag acceptance — so noisy/recursive
# forms (ls -R / -laR / --recursive) are rejected. Tiny read-only drill.
_PILOT_EXACT = frozenset(
    {
        "pwd",
        "ls",
        "ls -l",
        "ls -a",
        "ls -la",
        "ls -al",
        "git status --short",
        "git diff --stat",
        "git rev-parse --show-toplevel",
    },
)


def is_pilot_safe(command: str) -> tuple[bool, str]:
    """True only for the tiny read-only pilot allowlist. Rejects any shell
    metacharacter (chains/redirection/subshell/expansion), parent
    traversal, and anything not in the allowlist (so package managers /
    builds / tests / writes are denied by default).
    """
    c = (command or "").strip()
    if not c:
        return False, "empty command"
    if any(ch in _FORBIDDEN_CHARS for ch in c):
        return False, ("shell metacharacter / chain / redirection / expansion is not pilot-safe")
    if ".." in c:
        return False, "parent traversal is not pilot-safe"
    norm = " ".join(c.split())
    if norm in _PILOT_EXACT:
        return True, f"pilot command: {norm}"
    return False, f"command not in pilot allowlist: {norm}"


def _pilot_flag(project_root: Path) -> bool:
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "tools.native_shell_execution_pilot",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def pilot_native_allowed(
    project_root: Path,
    envelope: Any,
) -> tuple[bool, str]:
    """The 2.0-B0 conditions BEYOND what an execute_native verdict already
    proves. Returns (allowed, reason). Fails closed on anything unexpected
    (no exception escapes as an allow).
    """
    try:
        if not _pilot_flag(project_root):
            return False, "native_shell_execution_pilot is off"
        if getattr(envelope, "host", "") != "claude_code":
            return False, "pilot host must be claude_code"
        if getattr(envelope, "provider", "") != "bash":
            return False, "pilot provider must be bash"
        return is_pilot_safe(getattr(envelope, "command", "") or "")
    except Exception as exc:
        return False, f"pilot check error: {type(exc).__name__}"
