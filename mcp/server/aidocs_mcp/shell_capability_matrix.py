"""Host × provider shell capability matrix (Batch 1 — data model only).

A native shell tool may only act as an AIDOCS-managed provider when the
host proves it gives AIDOCS the seams the law needs. This module records,
per (host, provider), whether each capability is proven. ``ShellPolicy``
reads it to decide whether a native transport is even eligible — and
fails closed for any pair not present or not proven.

Batch 1 records the matrix and the eligibility rule. It does NOT enable
native execution (that is config-gated, default off) and does NOT
implement detach (Batch 2).

Hard-deny prerequisites (ALL required for native eligibility):
  * command_visible_before_exec — PreToolUse sees the command text
  * tool_name_visible           — PreToolUse sees which tool fired
  * cwd_session_visible         — cwd / project / session resolvable
  * pretooluse_hard_deny        — host honors a PreToolUse deny verdict

Strictness inputs (do NOT block eligibility; shape post-exec handling):
  * posttooluse_output_replacement — host can replace output pre-context
  * detach_supported               — Batch 2 native detach
  * notify_or_poll_supported       — completion surfacing path exists
"""

from __future__ import annotations

from dataclasses import dataclass

from .shell_envelope import (
    PROVIDER_BASH,
    PROVIDER_CMD,
    PROVIDER_POWERSHELL,
    HostCapabilityInfo,
)

HOST_CLAUDE_CODE = "claude_code"
HOST_OPENCODE = "opencode"
HOST_OPENAI_AGENTS = "openai_agents"

# ── Provider IDENTITY contract (Batch 2.0-B) ────────────────────────
# The capability matrix proves host SEAMS (visibility / hard-deny / output
# replacement) — it does NOT prove that the tool named "Bash" actually runs
# bash. Tool name is costume, not identity. Native EXECUTION (2.0-B) is
# native-eligible ONLY for:
#   IDENTITY_HOST_VERIFIED_PATH — the host exposes the provider's executable
#       path AND it passes REAL identity proof: exists, is a file, sits
#       under a configured TRUSTED install root (tools.native_shell_trusted_
#       roots), basename matches the provider family. (Probe/hash/signature
#       is a future optional layer.)
#   IDENTITY_STATIC_TRUSTED — a static/signed capability contract declares
#       the provider identity host-owned and non-spoofable (the host is the
#       enforcement substrate; if it lies, the gate is moot anyway).
# NOT native-eligible:
#   IDENTITY_HOST_REPORTED_PATH_SANITY — the host reports a path that passes
#       only SHAPE sanity (absolute, not under project_root, basename match).
#       Shape is not identity — a renamed bash.exe outside the project still
#       passes shape but proves nothing. Diagnostic tier only.
#   IDENTITY_NONE — unproven → ai_run fallback.
IDENTITY_NONE = ""
IDENTITY_HOST_REPORTED_PATH_SANITY = "host_reported_path_sanity"
IDENTITY_HOST_VERIFIED_PATH = "host_verified_path"
IDENTITY_STATIC_TRUSTED = "static_trusted"


@dataclass(frozen=True)
class HostProviderCapability:
    host: str
    provider: str
    command_visible_before_exec: bool
    tool_name_visible: bool
    cwd_session_visible: bool
    pretooluse_hard_deny: bool
    posttooluse_output_replacement: bool
    detach_supported: bool
    notify_or_poll_supported: bool
    # Provider IDENTITY contract (2.0-B). Default IDENTITY_NONE: tool name
    # is not identity, so a pair is NOT native-eligible until a contract is
    # explicitly established.
    provider_identity: str = IDENTITY_NONE

    def is_native_safe(self) -> bool:
        """The four hard-deny prerequisites that make native execution
        *eligible*. Strictness inputs are deliberately excluded.
        """
        return (
            self.command_visible_before_exec
            and self.tool_name_visible
            and self.cwd_session_visible
            and self.pretooluse_hard_deny
        )

    def to_capability_info(self) -> HostCapabilityInfo:
        return HostCapabilityInfo(
            host=self.host,
            command_visible_before_exec=self.command_visible_before_exec,
            tool_name_visible=self.tool_name_visible,
            cwd_session_visible=self.cwd_session_visible,
            pretooluse_hard_deny=self.pretooluse_hard_deny,
            posttooluse_output_replacement=self.posttooluse_output_replacement,
            detach_supported=self.detach_supported,
            notify_or_poll_supported=self.notify_or_poll_supported,
        )


def _cap(
    host,
    provider,
    *,
    cmd_vis,
    tool_vis,
    cwd_vis,
    hard_deny,
    out_replace,
    detach,
    notify,
    prov_id: str = IDENTITY_NONE,
) -> HostProviderCapability:
    return HostProviderCapability(
        host=host,
        provider=provider,
        command_visible_before_exec=cmd_vis,
        tool_name_visible=tool_vis,
        cwd_session_visible=cwd_vis,
        pretooluse_hard_deny=hard_deny,
        posttooluse_output_replacement=out_replace,
        detach_supported=detach,
        notify_or_poll_supported=notify,
        provider_identity=prov_id,
    )


# Seeded from observed host behavior. Claude Code: PreToolUse hard-deny +
# PostToolUse output replacement are proven (claude_hook). Native detach
# is Batch 2, so detach_supported stays False. Hosts/pairs absent from
# this matrix are treated as UNPROVEN (fail closed).
_MATRIX: dict[tuple[str, str], HostProviderCapability] = {
    (HOST_CLAUDE_CODE, PROVIDER_BASH): _cap(
        HOST_CLAUDE_CODE,
        PROVIDER_BASH,
        cmd_vis=True,
        tool_vis=True,
        cwd_vis=True,
        hard_deny=True,
        out_replace=True,
        detach=False,
        notify=True,
        # Identity contract: HOST_VERIFIED_PATH (Empire re-seal 2026-05-30).
        # claude_code/bash identity is proven by VERIFYING the enrolled
        # provider executable path (exists + under a non-user-writable
        # trusted root + basename) — NOT a static-trusted contract. The
        # live execution posture then independently re-proves FS authority
        # + current SHA-256 + provenance for that same path before any
        # native ALLOW. This is the honest real-operator-enable bridge:
        # capability honesty is preserved (we claim only path-verified
        # identity), and the costume route (IDENTITY_STATIC_TRUSTED) is
        # not used for native execution.
        prov_id=IDENTITY_HOST_VERIFIED_PATH,
    ),
    (HOST_CLAUDE_CODE, PROVIDER_POWERSHELL): _cap(
        HOST_CLAUDE_CODE,
        PROVIDER_POWERSHELL,
        cmd_vis=True,
        tool_vis=True,
        cwd_vis=True,
        hard_deny=True,
        out_replace=True,
        detach=False,
        notify=True,
    ),
    (HOST_CLAUDE_CODE, PROVIDER_CMD): _cap(
        HOST_CLAUDE_CODE,
        PROVIDER_CMD,
        cmd_vis=True,
        tool_vis=True,
        cwd_vis=True,
        hard_deny=True,
        out_replace=True,
        detach=False,
        notify=True,
    ),
    # OpenCode: PreToolUse bridge proven, but output replacement before
    # model context is not yet confirmed → eligible for verdict gating,
    # but post-exec handling stays strict (Batch 1 records this honestly).
    (HOST_OPENCODE, PROVIDER_BASH): _cap(
        HOST_OPENCODE,
        PROVIDER_BASH,
        cmd_vis=True,
        tool_vis=True,
        cwd_vis=True,
        hard_deny=True,
        out_replace=False,
        detach=False,
        notify=False,
    ),
    # OpenAI Agents adapter: on_tool_start gating proven; output
    # replacement unknown. Same honest shape as OpenCode.
    (HOST_OPENAI_AGENTS, PROVIDER_BASH): _cap(
        HOST_OPENAI_AGENTS,
        PROVIDER_BASH,
        cmd_vis=True,
        tool_vis=True,
        cwd_vis=True,
        hard_deny=True,
        out_replace=False,
        detach=False,
        notify=False,
    ),
}


def lookup(host: str, provider: str) -> HostProviderCapability | None:
    return _MATRIX.get((host or "", provider or ""))


def provider_identity(host: str, provider: str) -> str:
    """The provider-identity contract for a pair, or IDENTITY_NONE when the
    pair is unknown or has no contract. Tool name alone is never identity.
    """
    cap = lookup(host, provider)
    return cap.provider_identity if cap else IDENTITY_NONE


def is_native_safe(host: str, provider: str) -> bool:
    """True only when the pair is present AND its hard-deny prerequisites
    are all proven. Unknown host/provider → False (fail closed).
    """
    cap = lookup(host, provider)
    return bool(cap and cap.is_native_safe())


def capability_info(host: str, provider: str) -> HostCapabilityInfo:
    """Return the HostCapabilityInfo for a pair, or the fail-closed
    default (all-False) when the pair is unknown.
    """
    cap = lookup(host, provider)
    return cap.to_capability_info() if cap else HostCapabilityInfo()
