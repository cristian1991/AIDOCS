"""ShellCommandEnvelope — the canonical normalized shape for every
shell-like AGENT/HOST execution surface (Batch 1).

Doctrine (native-shell parity program, Batch 1):
  * ai_run remains canonical/reference/fallback.
  * Native shell is TRANSPORT only — AIDOCS owns the law.
  * Only shell-like AGENT/HOST surfaces normalize here. Internal
    AIDOCS subprocesses (git_helpers, file_ops, updater, …) are NOT
    normalized in Batch 1 — they are a later classification batch.

Surfaces normalized in Batch 1:
  * ai_run                          (transport=ai_run,    provider=bash)
  * host-native Bash / sh / zsh     (transport=host_native, provider=bash)
  * host-native PowerShell / pwsh   (transport=host_native, provider=powershell)
  * host-native cmd                 (transport=host_native, provider=cmd)
  * monitor / long-runner shell     (transport=monitor)

This module is pure data + detection. It performs NO policy decisions
and NO execution — ``shell_policy.ShellPolicy`` consumes the envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── provider / transport vocab ──────────────────────────────────────
PROVIDER_BASH = "bash"
PROVIDER_POWERSHELL = "powershell"
PROVIDER_CMD = "cmd"
PROVIDER_UNKNOWN = "unknown"

TRANSPORT_AI_RUN = "ai_run"
TRANSPORT_HOST_NATIVE = "host_native"
TRANSPORT_MONITOR = "monitor"
TRANSPORT_UNKNOWN = "unknown"

HOST_UNKNOWN = "unknown"

# Host-native tool-name → provider. Lower-cased basenames; the ".exe"
# suffix is stripped before lookup.
# Detection is intentionally BROADER than execution identity: sh/zsh/wsl are
# intercepted here as shell-like surfaces (gated/denied/fallbacked under
# provider=bash), but only real bash/bash.exe may satisfy bash NATIVE
# execution identity (see shell_provider_identity._PROVIDER_BASENAMES). If
# zsh/sh native execution is ever wanted, add separate provider semantics
# and law rather than treating them as bash.
_NATIVE_BASH_TOOLS = frozenset({"bash", "sh", "zsh", "wsl"})
_NATIVE_PWSH_TOOLS = frozenset({"powershell", "pwsh"})
_NATIVE_CMD_TOOLS = frozenset({"cmd"})

# tool_input keys that may carry the command text, in priority order.
_COMMAND_KEYS = ("command", "cmd", "script", "code")


@dataclass(frozen=True)
class HostCapabilityInfo:
    """A host/provider's proven execution-surface capabilities.

    Carried on the envelope so ShellPolicy can gate native execution
    without re-querying the matrix. Defaults are the FAIL-CLOSED shape
    (nothing proven) so an un-populated envelope can never authorize a
    native provider.
    """

    host: str = HOST_UNKNOWN
    command_visible_before_exec: bool = False
    tool_name_visible: bool = False
    cwd_session_visible: bool = False
    pretooluse_hard_deny: bool = False
    posttooluse_output_replacement: bool = False
    detach_supported: bool = False
    notify_or_poll_supported: bool = False


@dataclass(frozen=True)
class ShellCommandEnvelope:
    tool_name: str
    provider: str
    transport: str
    command: str
    project_root: Path
    host: str = HOST_UNKNOWN
    cwd: str | None = None
    host_session_id: str = ""
    managed_session_id: str = ""
    actor: str = ""
    principal: str = ""
    lane: str = ""
    run_in_background: bool = False
    timeout: int | None = None
    disconnect_after_seconds: int | None = None
    host_capability: HostCapabilityInfo | None = None
    # Host-exposed provider executable path/metadata (Batch 2.0-B identity).
    # None when the host does not expose it — then native eligibility falls
    # back to a static-trusted contract or to ai_run. Tool name is NOT a
    # substitute for this.
    provider_executable_path: str | None = None
    raw_tool_input: dict[str, Any] = field(default_factory=dict)

    @property
    def is_native(self) -> bool:
        return self.transport in (TRANSPORT_HOST_NATIVE, TRANSPORT_MONITOR)


def _normalize_tool_basename(tool_name: str) -> str:
    name = (tool_name or "").strip().lower()
    # Strip a Claude-Code style namespace prefix if present.
    for prefix in ("mcp__aidocs__", "mcp__"):
        name = name.removeprefix(prefix)
    name = name.removesuffix(".exe")
    return name


def detect_provider_and_transport(tool_name: str) -> tuple[str, str]:
    """Map a tool name to (provider, transport).

    ai_run is the canonical bash transport. Native shell tool names map
    to host_native with their provider. ``monitor`` is a transport in
    its own right (long-runner read surface); its provider defaults to
    bash and is refined from the command elsewhere if needed. Anything
    unrecognized is (unknown, unknown) so the policy fails closed.
    """
    base = _normalize_tool_basename(tool_name)
    if base in ("ai_run", "ai_run_output", "ai_run_status", "ai_run_kill"):
        return PROVIDER_BASH, TRANSPORT_AI_RUN
    if base == "monitor":
        return PROVIDER_BASH, TRANSPORT_MONITOR
    if base in _NATIVE_BASH_TOOLS:
        return PROVIDER_BASH, TRANSPORT_HOST_NATIVE
    if base in _NATIVE_PWSH_TOOLS:
        return PROVIDER_POWERSHELL, TRANSPORT_HOST_NATIVE
    if base in _NATIVE_CMD_TOOLS:
        return PROVIDER_CMD, TRANSPORT_HOST_NATIVE
    return PROVIDER_UNKNOWN, TRANSPORT_UNKNOWN


def _extract_command(tool_input: dict[str, Any]) -> str:
    for key in _COMMAND_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize(
    *,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    project_root: Path,
    host: str = HOST_UNKNOWN,
    host_session_id: str = "",
    managed_session_id: str = "",
    actor: str = "",
    principal: str = "",
    lane: str = "",
    host_capability: HostCapabilityInfo | None = None,
    provider_executable_path: str | None = None,
) -> ShellCommandEnvelope:
    """Normalize one shell-like surface call into a ShellCommandEnvelope.

    Detection is name-driven (provider/transport) and input-driven
    (command, run_in_background, timeout, disconnect window). No policy
    is applied here.
    """
    tin = tool_input if isinstance(tool_input, dict) else {}
    provider, transport = detect_provider_and_transport(tool_name)
    command = _extract_command(tin)
    # Only an explicit run_in_background flag marks a backgrounded call;
    # ai_run's `foreground=True` is a separate visible-terminal concern.
    run_bg = bool(tin.get("run_in_background", False))
    return ShellCommandEnvelope(
        tool_name=tool_name,
        provider=provider,
        transport=transport,
        command=command,
        project_root=project_root,
        host=host or HOST_UNKNOWN,
        cwd=(str(tin["cwd"]) if isinstance(tin.get("cwd"), str) else None),
        host_session_id=host_session_id,
        managed_session_id=managed_session_id,
        actor=actor,
        principal=principal,
        lane=lane,
        run_in_background=run_bg,
        timeout=_coerce_int(tin.get("timeout") or tin.get("timeout_seconds")),
        disconnect_after_seconds=_coerce_int(tin.get("disconnect_after_seconds")),
        host_capability=host_capability,
        # Host-EXPOSED executable identity ONLY. The path is taken from the
        # caller (a host that cryptographically binds/exposes the executable
        # it will run) or from a host-supplied tool_input["executable_path"].
        # Empire re-seal 2026-05-30: AIDOCS NEVER fabricates this from the
        # enrolled provider config — an assumed-equals-enrolled path is not
        # the actual executable the host will run. When the host exposes
        # nothing, this stays None and the native-ALLOW identity binding
        # (governed_shell_attest.identity_binding_ok) fails closed → ai_run.
        provider_executable_path=(
            provider_executable_path
            or (
                str(tin["executable_path"]) if isinstance(tin.get("executable_path"), str) else None
            )
        ),
        raw_tool_input=dict(tin),
    )
