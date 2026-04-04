"""Tests for the unified AccessGate — 6-level cascade.

Levels:
    1. Managed Mode Gate — block raw file tools when managed
    2. Infrastructure Protection — block writes to AIDOCS config/source
    3. Sensitive File Protection — block .env, credentials, keys
    4. Memory Path Gate — .MEMORY/ reads free, writes intent-gated
    5. Read Gate — per-file discovery, known_exact_path bypass
    6. Edit Gate — requires prior read/discovery
"""

from __future__ import annotations

import pytest

from aidocs_mcp.access_gate import AccessGate, GateContext, GateDecision


# ── Helpers ──


def _ctx(
    *,
    managed: bool = True,
    session_id: str | None = "test-session",
    dev_mode: bool = False,
    gate_state: dict | None = None,
) -> GateContext:
    return GateContext(
        managed=managed,
        session_id=session_id,
        dev_mode=dev_mode,
        gate_state=gate_state or {"known_exact_paths": [], "lane_exact_paths": []},
    )


def _ctx_with_discovered(*paths: str, **kwargs) -> GateContext:
    return _ctx(gate_state={"known_exact_paths": list(paths), "lane_exact_paths": []}, **kwargs)


# ── Level 1: Managed Mode Gate — raw tool blocking ──


class TestLevel1ManagedModeGate:
    """Raw file tools blocked when managed mode is active."""

    @pytest.mark.parametrize("tool", ["read", "grep", "glob", "edit", "write"])
    def test_raw_file_tools_blocked_when_managed(self, tool: str) -> None:
        decision = AccessGate.check_raw_tool(_ctx(managed=True), tool)
        assert not decision.allowed
        assert decision.level == "managed_mode_gate"

    @pytest.mark.parametrize("tool", ["read", "grep", "glob", "edit", "write"])
    def test_raw_file_tools_allowed_when_unmanaged(self, tool: str) -> None:
        decision = AccessGate.check_raw_tool(_ctx(managed=False), tool)
        assert decision.allowed

    def test_bash_not_blocked(self) -> None:
        """Bash gating is deferred — always allowed regardless of managed mode."""
        decision = AccessGate.check_raw_tool(_ctx(managed=True), "bash")
        assert decision.allowed

    @pytest.mark.parametrize("tool", ["agent", "skill", "todowrite", "compact", "askuserquestion"])
    def test_non_file_tools_always_allowed(self, tool: str) -> None:
        decision = AccessGate.check_raw_tool(_ctx(managed=True), tool)
        assert decision.allowed

    def test_agent_blocked_when_subagents_disabled(self) -> None:
        decision = AccessGate.check_raw_tool(
            _ctx(managed=True), "agent", allow_subagents=False
        )
        assert not decision.allowed
        assert decision.level == "managed_mode_gate"

    def test_agent_allowed_when_subagents_enabled(self) -> None:
        decision = AccessGate.check_raw_tool(
            _ctx(managed=True), "agent", allow_subagents=True
        )
        assert decision.allowed

    def test_block_message_includes_replacement(self) -> None:
        decision = AccessGate.check_raw_tool(_ctx(managed=True), "read")
        assert decision.reason is not None
        assert "code_get_lines" in decision.reason

    def test_edit_block_message_includes_replacement(self) -> None:
        decision = AccessGate.check_raw_tool(_ctx(managed=True), "edit")
        assert decision.reason is not None
        assert "code_edit_lines" in decision.reason


# ── Level 2: Infrastructure Protection ──


class TestLevel2InfrastructureProtection:
    """Writes to AIDOCS config/source blocked unless dev_mode."""

    def test_block_write_aidocs_toml(self) -> None:
        decision = AccessGate.check_write(_ctx(), "aidocs.toml")
        assert not decision.allowed
        assert decision.level == "infrastructure_protection"

    def test_block_write_aidocs_plugin_json(self) -> None:
        decision = AccessGate.check_write(_ctx(), "aidocs-plugin.json")
        assert not decision.allowed
        assert decision.level == "infrastructure_protection"

    def test_block_write_mcp_source(self) -> None:
        decision = AccessGate.check_write(_ctx(), "mcp/server/aidocs_mcp/runtime_service.py")
        assert not decision.allowed
        assert decision.level == "infrastructure_protection"

    def test_allow_write_mcp_source_in_dev_mode(self) -> None:
        decision = AccessGate.check_write(_ctx(dev_mode=True), "mcp/server/aidocs_mcp/runtime_service.py")
        assert decision.allowed

    def test_config_files_blocked_even_in_dev_mode(self) -> None:
        """aidocs.toml and aidocs-plugin.json are always blocked."""
        decision = AccessGate.check_write(_ctx(dev_mode=True), "aidocs.toml")
        assert not decision.allowed

    def test_allow_write_normal_file(self) -> None:
        ctx = _ctx_with_discovered("src/app.py")
        decision = AccessGate.check_write(ctx, "src/app.py")
        assert decision.allowed


# ── Level 3: Sensitive File Protection ──


class TestLevel3SensitiveFileProtection:
    """Block access to credential/key files."""

    @pytest.mark.parametrize("path", [
        ".env",
        ".env.local",
        "config/credentials.json",
        "secrets.yaml",
        "server.key",
        "cert.pem",
        "auth.pfx",
    ])
    def test_block_sensitive_files(self, path: str) -> None:
        decision = AccessGate.check_read(_ctx(), path)
        assert not decision.allowed
        assert decision.level == "sensitive_file_protection"

    @pytest.mark.parametrize("path", [
        ".env",
        "secrets.yaml",
    ])
    def test_block_sensitive_writes(self, path: str) -> None:
        decision = AccessGate.check_write(_ctx(), path)
        assert not decision.allowed
        assert decision.level == "sensitive_file_protection"


# ── Level 4: Memory Path Gate ──


class TestLevel4MemoryPathGate:
    """.MEMORY/ reads always allowed; workflow/security rule writes need intent."""

    def test_memory_reads_always_allowed(self) -> None:
        decision = AccessGate.check_read(_ctx(), ".MEMORY/sessions/s1/context.md")
        assert decision.allowed
        assert decision.level == "memory_path_exemption"

    def test_memory_reads_allowed_without_discovery(self) -> None:
        """No indexed prerequisite for .MEMORY/ files."""
        decision = AccessGate.check_read(
            _ctx(gate_state={"known_exact_paths": [], "lane_exact_paths": []}),
            ".MEMORY/INDEX.md",
        )
        assert decision.allowed

    def test_memory_session_file_allowed(self) -> None:
        decision = AccessGate.check_read(_ctx(), ".MEMORY/sessions/s1/SESSION.md")
        assert decision.allowed

    def test_workflow_rules_write_requires_intent(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/rules/workflow-rules.md")
        assert not decision.allowed
        assert decision.level == "memory_write_intent_gate"

    def test_security_rules_write_requires_intent(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/rules/security.md")
        assert not decision.allowed
        assert decision.level == "memory_write_intent_gate"

    def test_workflow_actions_write_requires_intent(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/rules/workflow-actions.md")
        assert not decision.allowed
        assert decision.level == "memory_write_intent_gate"

    def test_compiled_workflow_write_requires_intent(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/config/workflow-actions.json")
        assert not decision.allowed
        assert decision.level == "memory_write_intent_gate"

    def test_workflow_write_allowed_with_intent(self) -> None:
        decision = AccessGate.check_write(
            _ctx(), ".MEMORY/rules/workflow-rules.md", has_intent=True
        )
        assert decision.allowed

    def test_session_file_write_freely_allowed(self) -> None:
        """Session operational files are not protected — agents write them normally."""
        decision = AccessGate.check_write(_ctx(), ".MEMORY/sessions/s1/context.md")
        assert decision.allowed
        assert decision.level == "memory_path_exemption"

    def test_journal_write_freely_allowed(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/sessions/s1/journal.jsonl")
        assert decision.allowed

    def test_domain_memory_write_freely_allowed(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/domains/accounting.md")
        assert decision.allowed

    def test_index_file_write_freely_allowed(self) -> None:
        decision = AccessGate.check_write(_ctx(), ".MEMORY/INDEX.md")
        assert decision.allowed

    def test_dev_mode_bypasses_workflow_protection(self) -> None:
        """dev_mode=true bypasses all .MEMORY/ write protection."""
        decision = AccessGate.check_write(
            _ctx(dev_mode=True), ".MEMORY/rules/workflow-rules.md"
        )
        assert decision.allowed

    def test_non_workflow_rules_freely_writable(self) -> None:
        """Rules that aren't workflow/security are operational — freely writable."""
        decision = AccessGate.check_write(_ctx(), ".MEMORY/rules/standards.md")
        assert decision.allowed

        decision = AccessGate.check_write(_ctx(), ".MEMORY/rules/communication.md")
        assert decision.allowed


# ── Level 5: Read Gate (per-file discovery) ──


class TestLevel5ReadGate:
    """Code files require indexed discovery before reading."""

    def test_block_undiscovered_file(self) -> None:
        decision = AccessGate.check_read(_ctx(), "src/app.py")
        assert not decision.allowed
        assert decision.level == "read_gate"

    def test_allow_discovered_file(self) -> None:
        ctx = _ctx_with_discovered("src/app.py")
        decision = AccessGate.check_read(ctx, "src/app.py")
        assert decision.allowed

    def test_known_exact_path_bypasses_gate(self) -> None:
        """known_exact_path=true allows reading without prior discovery."""
        decision = AccessGate.check_read(_ctx(), "src/app.py", known_exact_path=True)
        assert decision.allowed

    def test_lane_exact_paths_grant_access(self) -> None:
        ctx = _ctx(gate_state={"known_exact_paths": [], "lane_exact_paths": ["src/app.py"]})
        decision = AccessGate.check_read(ctx, "src/app.py")
        assert decision.allowed

    def test_path_normalization_forward_slashes(self) -> None:
        ctx = _ctx_with_discovered("src/app.py")
        decision = AccessGate.check_read(ctx, "src\\app.py")
        assert decision.allowed

    def test_no_blanket_allow_read(self) -> None:
        """allow_read=True is removed — only per-file grants work."""
        ctx = _ctx(gate_state={"allow_read": True, "known_exact_paths": [], "lane_exact_paths": []})
        decision = AccessGate.check_read(ctx, "src/undiscovered.py")
        assert not decision.allowed

    def test_unmanaged_allows_all_reads(self) -> None:
        """When not in managed mode, reads are unrestricted."""
        decision = AccessGate.check_read(_ctx(managed=False), "src/anything.py")
        assert decision.allowed

    def test_protected_paths_not_grantable(self) -> None:
        """aidocs.toml, aidocs-plugin.json, aidocs_mcp/* cannot be granted."""
        ctx = _ctx_with_discovered("aidocs.toml")
        decision = AccessGate.check_read(ctx, "aidocs.toml")
        assert not decision.allowed


# ── Level 6: Edit Gate ──


class TestLevel6EditGate:
    """Files must be discovered/read before editing."""

    def test_block_edit_undiscovered_file(self) -> None:
        decision = AccessGate.check_edit(_ctx(), "src/app.py")
        assert not decision.allowed
        assert decision.level == "edit_gate"

    def test_allow_edit_discovered_file(self) -> None:
        ctx = _ctx_with_discovered("src/app.py")
        decision = AccessGate.check_edit(ctx, "src/app.py")
        assert decision.allowed

    def test_unmanaged_allows_all_edits(self) -> None:
        decision = AccessGate.check_edit(_ctx(managed=False), "src/anything.py")
        assert decision.allowed


# ── grant_discovery ──


class TestGrantDiscovery:
    """Indexed tools grant per-file read access."""

    def test_grant_adds_to_known_exact_paths(self, tmp_path) -> None:
        from aidocs_mcp.query_gate import QueryGateStore

        project = tmp_path / "project"
        (project / ".MEMORY" / "sessions" / "s1").mkdir(parents=True)

        store = QueryGateStore()
        AccessGate.grant_discovery(store, project, "s1", "code_investigate", ["src/a.py", "src/b.py"])

        state = store.get(project, "s1")
        assert "src/a.py" in state["known_exact_paths"]
        assert "src/b.py" in state["known_exact_paths"]

    def test_grant_does_not_set_allow_read(self, tmp_path) -> None:
        """Blanket allow_read is removed — grants are per-file only."""
        from aidocs_mcp.query_gate import QueryGateStore

        project = tmp_path / "project"
        (project / ".MEMORY" / "sessions" / "s1").mkdir(parents=True)

        store = QueryGateStore()
        AccessGate.grant_discovery(store, project, "s1", "code_investigate", ["src/a.py"])

        state = store.get(project, "s1")
        assert not state.get("allow_read", False)

    def test_grant_rejects_protected_paths(self, tmp_path) -> None:
        from aidocs_mcp.query_gate import QueryGateStore

        project = tmp_path / "project"
        (project / ".MEMORY" / "sessions" / "s1").mkdir(parents=True)

        store = QueryGateStore()
        AccessGate.grant_discovery(
            store, project, "s1", "code_find",
            ["src/ok.py", "aidocs.toml", "mcp/server/aidocs_mcp/secret.py"],
        )

        state = store.get(project, "s1")
        assert "src/ok.py" in state["known_exact_paths"]
        assert "aidocs.toml" not in state["known_exact_paths"]
        assert "mcp/server/aidocs_mcp/secret.py" not in state["known_exact_paths"]
