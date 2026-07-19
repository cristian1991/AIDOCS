"""Settings catalog — defines all AIDOCS configuration settings.

This stays intentionally flat: each entry is keyed by the dotted TOML path and
describes the setting without introducing a second nested config model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

SettingType = Literal["integer", "boolean", "string", "string_list"]
SettingScope = Literal["global", "project", "session"]
ConfigEditMode = Literal["explicit_user_permitted"]


class SettingMetadata(TypedDict):
    type: SettingType
    default: int | bool | str | list[str]
    allowed_values: list[str] | None
    description: str
    value_descriptions: dict[str, str]
    allowed_scopes: list[SettingScope]
    agent_editable_scopes: list[SettingScope]
    security_sensitive: bool
    requires_restart: bool
    # dashboard_only: when True, the MCP config_set tool (agent
    # write surface) categorically refuses this key. Only the
    # dashboard UI can flip it via its direct sqlite write path.
    # Use for any setting whose truthy value would disable AIDOCS
    # guardrails — e.g. enforcement kill switches. Read-side code
    # is unaffected.
    dashboard_only: bool
    # service_managed: non-empty names the high-level service that
    # OWNS this key (e.g. "governed_bash"). Such keys are NOT
    # editable through ANY normal settings surface — not the agent
    # config_set tool and not the dashboard settings editor. They are
    # written ONLY by their owning service (which validates + applies
    # the whole interdependent group atomically), and elsewhere are
    # exposed read-only as Advanced Diagnostics. This prevents a
    # half-enabled posture from flipping one flag at a time.
    service_managed: str
    # deprecated: non-empty is the operator-facing migration message for
    # a deprecated/reserved key. Deprecated keys are HIDDEN from normal
    # settings editing (config_set refuses with this message) and appear
    # only in the Advanced Raw Catalog as read-only with the migration
    # note. The read side still honors the stored value for one
    # deprecation cycle; only the write surface is closed.
    deprecated: str


def _setting(
    *,
    type: SettingType,
    default: int | bool | str | list[str],
    description: str,
    allowed_values: list[str] | None = None,
    value_descriptions: dict[str, str] | None = None,
    security_sensitive: bool = False,
    dashboard_only: bool = False,
    service_managed: str = "",
    deprecated: str = "",
    scope: SettingScope | list[SettingScope] = "project",
) -> SettingMetadata:
    allowed_scopes: list[SettingScope] = list(scope) if isinstance(scope, list) else [scope]
    agent_editable_scopes: list[SettingScope] = (
        []
        if (
            ("global" in allowed_scopes and len(allowed_scopes) == 1)
            or security_sensitive
            or dashboard_only
        )
        else [s for s in allowed_scopes if s != "global"]
    )
    return {
        "type": type,
        "default": default,
        "allowed_values": allowed_values,
        "description": description,
        "value_descriptions": value_descriptions or {},
        "allowed_scopes": allowed_scopes,
        "agent_editable_scopes": agent_editable_scopes,
        "security_sensitive": security_sensitive,
        "requires_restart": True,
        "dashboard_only": dashboard_only,
        "service_managed": service_managed,
        "deprecated": deprecated,
    }


SETTINGS_CATALOG: dict[str, SettingMetadata] = {
    # ── delete.* (ai_delete trash + quota knobs, sealed 2026-05-27) ──
    "delete.max_trash_bytes": _setting(
        type="integer",
        default=25 * 1024 * 1024,
        description=(
            "Hard size ceiling for a single file going to "
            ".TRASH/. Files larger than this refuse with typed "
            "`too_large` error instead of being moved."
        ),
        scope=["global", "project"],
    ),
    "delete.max_trash_items": _setting(
        type="integer",
        default=500,
        description=(
            "Maximum file count under .TRASH/ before ai_delete "
            "refuses with `trash_quota_exhausted`. 0 = unlimited."
        ),
        scope=["global", "project"],
    ),
    "delete.max_trash_total_bytes": _setting(
        type="integer",
        default=500 * 1024 * 1024,
        description=(
            "Maximum total bytes under .TRASH/ before ai_delete "
            "refuses with `trash_quota_exhausted`. 0 = unlimited."
        ),
        scope=["global", "project"],
    ),
    "delete.idempotency_window_minutes": _setting(
        type="integer",
        default=10,
        description=(
            "Minutes within which a repeat ai_delete on a path "
            "returns the prior trash_id (`already_deleted=true`) "
            "rather than refusing or re-trashing."
        ),
        scope=["global", "project"],
    ),
    "journal.max_entries": _setting(
        type="integer",
        default=100,
        description="Maximum journal entries kept per session before eviction starts. 0 = unlimited (never evict).",
        scope=["global", "project", "session"],
    ),
    "journal.evict_batch": _setting(
        type="integer",
        default=20,
        description="How many oldest journal entries to archive when the journal is full.",
        scope=["global", "project", "session"],
    ),
    "journal.trivial_actions": _setting(
        type="string_list",
        default=["task_begin", "task_update", "project_update"],
        description="Action kinds that are too trivial to journal.",
        scope=["global", "project", "session"],
    ),
    # ── mcp.* (host MCP wiring, #249 daemon mode) ──
    "mcp.local_daemon_url": _setting(
        type="string",
        default="",
        description=(
            "When set (e.g. http://127.0.0.1:8748/mcp), ensure_claude_mcp_config "
            "writes the aidocs .mcp.json entry as this shared local HTTP daemon "
            "instead of a per-project stdio spawn (Claude Code auto-reconnects "
            "http; the aidocs service watchdog restarts crashes). Empty = stdio."
        ),
        scope=["global"],
    ),
    "mcp.multitenant_strict": _setting(
        type="boolean",
        default=False,
        description=(
            "#280: when True, the shared HTTP daemon REFUSES a tool call that "
            "declares no project root (no ?root= / X-AIDOCS-Project-Root) with an "
            "actionable error instead of resolving via a process-global — the "
            "cross-tenant leak. Enable ONLY after every project's .mcp.json is "
            "regenerated to the scoped ?root= URL (else rootless calls refuse). "
            "Off = pre-#280 behavior (stdio + single-tenant unaffected)."
        ),
        scope=["global"],
    ),
    # ── memory.capture_analyzer.* (memory-loop seal, 2026-07-09) ──
    "memory.capture_analyzer_timeout_ms": _setting(
        type="integer",
        default=1500,
        description=(
            "Hard budget for the bounded post-capture analyzer (NLP substance "
            "+ code-index term/anchor derivation) run by memory_capture. "
            "On timeout the capture proceeds with explicit metadata only. "
            "0 disables the analyzer entirely."
        ),
        scope=["global", "project", "session"],
    ),
    # ── memory.semantic_recall.* (palace hybrid-recall lane, catalogued 2026-07-01) ──
    # ── memory.stop_capture (#316 Stop-time memory capture, 2026-07-13) ──
    "memory.stop_capture": _setting(
        type="boolean",
        default=False,
        description=(
            "When True, the Stop hook triages the assistant's turn text "
            "for durable declarative content (#316): sentences carrying a "
            "rule/invariant/decision/preference frame that are NOVEL "
            "against existing memory are auto-captured with "
            "source=stop_capture provenance. Default False — this inverts "
            "the audit.capture_response_content sensitivity default, so "
            "Stop-time capture is strictly opt-in. SubagentStop (lane "
            "worker) turns never capture regardless of this setting."
        ),
        scope=["global", "project"],
    ),
    "memory.semantic_recall_on_ups": _setting(
        type="boolean",
        default=True,
        description=(
            "Whether the palace semantic-recall lane runs on a prompt (strict, "
            "timeboxed, ranked last). False disables recall; the keyword/lemma "
            "memory lanes still run."
        ),
        scope=["global", "project"],
    ),
    "memory.semantic_recall_limit": _setting(
        type="integer",
        default=2,
        description=(
            "Top-K cap for palace semantic-recall hints surfaced on a prompt "
            "(small by design so recall can't drown the keyword/lemma lanes)."
        ),
        scope=["global", "project"],
    ),
    "memory.semantic_recall_max_distance": _setting(
        type="string",
        default="0.85",
        description=(
            "Max embedding distance for a palace semantic-recall hit (parsed as "
            "float; conservative floor keeps precision high — lower is stricter)."
        ),
        scope=["global", "project"],
    ),
    "journal.min_intent_length": _setting(
        type="integer",
        default=10,
        description="Minimum intent length required before a journal entry is recorded. 0 = no minimum (record all).",
        scope=["global", "project", "session"],
    ),
    "index.extra_skip_dirs": _setting(
        type="string_list",
        default=[],
        description="Extra directories to skip during indexing.",
        scope=["global", "project", "session"],
    ),
    "index.extra_module_hints": _setting(
        type="string_list",
        default=[],
        description="Extra directory names that hint at project modules.",
        scope=["global", "project", "session"],
    ),
    "index.max_json_size": _setting(
        type="integer",
        default=100_000,
        description="Maximum JSON file size in bytes before the indexer skips the file. 0 = unlimited (index any size).",
        scope=["global", "project", "session"],
    ),
    "index.enabled_languages": _setting(
        type="string",
        default="all",
        description="Language set used by index-side language filtering.",
        scope=["global", "project", "session"],
    ),
    "index.include_tests": _setting(
        type="boolean",
        default=False,
        description="Include test directories (tests/, test/, __tests__/) in the code index by default.",
        scope=["global", "project"],
    ),
    "index.lsp_materialize": _setting(
        type="boolean",
        default=False,
        description="Call the aidocs_lsp door (lsp_drain_and_evict, scoped to the files changed in the pass) at the end of a code-index sync to materialize semantic_ref edges. Dormant by default; fail-open — any door failure leaves the sync result unchanged (doctrine XXXII guest oracle).",
        scope=["global", "project", "session"],
    ),
    "languages.enabled": _setting(
        type="string",
        default="all",
        description="Comma-separated language descriptors to load for prompt classification.",
        scope=["global", "project", "session"],
    ),
    "tools.tool_call_timeout": _setting(
        type="integer",
        default=10,
        description="Default timeout in seconds for general MCP tool calls. 0 = unlimited (no watchdog). A caller-supplied timeout= is honored, capped at tools.max_timeout.",
        scope=["global", "project", "session"],
    ),
    "tools.sync_write_timeout": _setting(
        type="integer",
        default=60,
        description="Default timeout in seconds for non-indexer synchronous write operations (task_begin session writes, etc.). 0 = unlimited. Capped at tools.max_timeout per call. Index-sync tools use tools.index_sync_timeout instead.",
        scope=["global", "project", "session"],
    ),
    "tools.index_sync_timeout": _setting(
        type="integer",
        default=120,
        description="Default timeout in seconds for index-sync operations (ai_index_sync, schema_index_sync, semantic_index_sync). 0 = unlimited. A full reindex of a large repo routinely exceeds the general sync default. Capped at tools.max_timeout per call; accepts a caller-supplied timeout= up to that ceiling.",
        scope=["global", "project", "session"],
    ),
    "tools.memory_surfacing_timeout_ms": _setting(
        type="integer",
        default=500,
        description="Fail-open budget in milliseconds for the read-memory surfacing pass (x-ray goggles on file-reading tools). 0 = unlimited (no budget). If memory discovery exceeds this, surfacing is skipped silently — it never blocks or fails the tool call. Raise it if a large knowledge graph is being skipped; lower it for snappier reads.",
        scope=["global", "project", "session"],
    ),
    "tools.git_functions_timeout": _setting(
        type="integer",
        default=30,
        description="Default timeout in seconds for git-related operations. 0 = unlimited.",
        scope=["global", "project", "session"],
    ),
    "tools.max_timeout": _setting(
        type="integer",
        default=120,
        description="Maximum timeout in seconds allowed for any tool call (the ceiling a caller-supplied timeout= is clamped to). 0 = no ceiling (unlimited).",
        scope=["global", "project", "session"],
    ),
    "tools.bash_long_runner_cap_seconds": _setting(
        type="integer",
        default=300,
        description="[T0] Cap for foreground bash long-runners (pytest, npm install, pip install, yarn, cargo, docker). Commands matching those families are hard-blocked unless invoked with run_in_background=true, forcing the agent to either background the call or narrow its scope so the conversation doesn't wedge. 0 = unlimited (no hard block — degrades to a non-blocking advisory).",
        scope=["global", "project", "session"],
    ),
    "tools.shell_enforcement_live": _setting(
        type="boolean",
        default=False,
        description="[T0 DASHBOARD-ONLY] [Batch 2.0-A, default OFF] Make ShellPolicy/ShellEnforcement the AUTHORITATIVE verdict for host-native shell tools (Bash/PowerShell/cmd) on supporting adapters, replacing the orchestrator verdict slice for those tools. Structural gates (managed-mode, reconnect, active freeze) still run above it. NO native process executes in 2.0-A: execute_native and any allow collapse to native-deny + ai_run fallback; even a kill-switch/debug allow cannot run a native process. Native execution requires Batch 2.0-B (tools.native_shell_provider_enabled).",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.shell_lifecycle_preflight_enforce": _setting(
        type="boolean",
        default=False,
        description="[T0 DASHBOARD-ONLY] [Future-sight, default OFF] Enforce the hidden-execution-chain preflight on the ai_run path: when True, commands that trigger package/build/script-runner/CI/git-hook/interpreter/local-script lifecycle execution are denied (arbitrary/remote code) or require an operator freeze (builds/test-runners/git hooks) BEFORE spawning. When False, the preflight still classifies and AUDITS (future_sight_preflight event) but does not change ai_run verdicts — existing gates/receipts are preserved. Native Bash and ai_run consume ONE shared decision (resolve_shell_enforcement delegates to the canonical ai_run core law), so this preflight applies IDENTICALLY to both transports — there is no native-only classifier; the read-only catalog is demoted to telemetry/capability evidence.",
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_readonly_enabled": _setting(
        type="boolean",
        default=False,
        description="[T0 DASHBOARD-ONLY] [DEMOTED 2026-06-06] Two-transport shell architecture. LOCAL agent hosts with a proven native seam (today: Claude Code) use their built-in Bash tool as the PRIMARY governed dev shell; REMOTE/web agents (GPT via mcp.codenexus.cloud) have no host-native Bash and use MCP ai_run. Native bash is the NORMAL governed shell surface: a command runs natively whenever the canonical ai_run law returns execute_native AND capability is proven (output-replaceable receipt + fresh attestation + host↔provider identity + no command-substitution / no unbounded follow). This flag NO LONGER gates authorization — the master switch is tools.shell_enforcement_live. shell_readonly/shell_family are DEMOTED to telemetry/capability EVIDENCE only. HOST-ADAPTER CONTRACT: when the operator enabled Governed Bash and pinned a system-authority provider, AIDOCS freshly re-attests that exact provider before each local ALLOW and the PROVEN Claude Code adapter self-binds to it for ALL law-permitted work (reads, project-local writes, git, builds, tests); unknown hosts, OpenCode (until separately proven), and remote MCP/web NEVER self-bind → ai_run. All native output is receipted + guarded + capped; ai_run remains the canonical remote shell + reference impl + capability fallback.",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_readonly_extra_commands": _setting(
        type="string",
        default="",
        description="[T0 DASHBOARD-ONLY] [Batch 2.0-B1] Operator-extensible read-only native bash binaries (separated by ';'/','/OS pathsep), ADDED to the built-in governed read-only allowlist. Extension is bounded: entries in the immutable hard-deny family (writes/network/scripts/eval/package-managers/secret-env: rm, curl, ssh, bash, python, npm, sed, awk, env, …) are silently dropped and can never be re-enabled, and every extra command still passes the no-metacharacter / no-redirection / no-path / per-binary guards. Use for genuinely read-only tools (e.g. jq, yq, rg, fd, bat). Empty by default.",
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_trusted_roots": _setting(
        type="string",
        default="",
        description="[T0 DASHBOARD-ONLY] [Batch 2.0-B] OS path-list (separated by ';' on Windows, ':' elsewhere, or ',') of TRUSTED install roots for host-native shell provider executables. A provider with an IDENTITY_HOST_VERIFIED_PATH contract is only native-eligible when the host-reported executable exists, is a file, sits UNDER one of these roots, and its basename matches the provider family. Empty (default) = no path verifies → native execution stays disabled (fail closed). This is the allowlist that turns a host-reported path into real identity proof; tool name is never sufficient.",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_provider_enabled": _setting(
        type="boolean",
        default=False,
        description="[T0 DASHBOARD-ONLY] [Batch 1, default OFF] Allow host-native shell tools (Bash/PowerShell/cmd) to act as AIDOCS-managed shell PROVIDERS instead of routing to ai_run. Even when True, a native provider is only permitted for a host/provider pair whose capability matrix proves command-visibility + PreToolUse hard-deny; unproven pairs fail closed to ai_run. ai_run remains canonical/reference/fallback regardless. Native shell is transport only — ShellPolicy still owns the law.",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_provider_path": _setting(
        type="string",
        default="",
        description="[T0 DASHBOARD-ONLY] [Governed Bash] Operator-pinned absolute path to the native shell provider executable (e.g. C:\\Program Files\\Git\\usr\\bin\\bash.exe). Set by the Governed Bash wizard. Empty = autodetect (PATH lookup) for posture checks. Used together with native_shell_trusted_roots to prove provider identity; the wizard validates it exists, is a file, sits under a trusted root, and the basename matches the provider family.",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_provider_sha256": _setting(
        type="string",
        default="",
        description="[T0 DASHBOARD-ONLY] [Governed Bash] Optional SHA-256 hash pin (hex) of the provider executable. When set, the Governed Bash posture is verified ONLY if the provider file's SHA-256 matches this pin — defeats a swapped binary under a trusted root. Empty = hash pinning skipped (path + trusted-root identity still required).",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.native_shell_require_os_signature": _setting(
        type="boolean",
        default=False,
        description="[T0 DASHBOARD-ONLY] [Governed Bash] When True, the Governed Bash posture requires the provider executable to carry a valid OS code signature (Windows Authenticode 'Valid'). If the platform cannot verify a signature, posture fails closed. Default False = signature check skipped.",
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
    ),
    "tools.shell_disconnect_after_seconds": _setting(
        type="integer",
        default=0,
        description="[T0 DASHBOARD-ONLY] [Batch 2 reserved] Seconds a native foreground shell command may run before AIDOCS detaches it (returning a run_id handle) instead of wedging the agent. 0 = disabled (Batch 1 does not implement native detach; ai_run is already always-detached). Carried on ShellCommandEnvelope so the policy contract is stable before Batch 2 wires detach.",
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "tools.shell_policy_shadow_enabled": _setting(
        type="boolean",
        default=False,
        description="[Batch 1.5, default OFF] Observe-only ShellPolicy shadow. When True, native-shell PreToolUse calls additionally run ShellPolicy in a SIDE-EFFECT-FREE shadow that consumes the already-computed live verdict (it never re-runs the gate cascade, never calls ai_run, never mints freezes) and records a shell_policy_shadow audit comparing live vs ShellPolicy verdicts (law-divergence vs transport-divergence). Enforcement stays with the live cascade; native execution stays disabled. Pure observation for parity evidence before Batch 2.",
        scope=["global", "project", "session"],
    ),
    "agent.inject_message_directives": _setting(
        type="boolean",
        default=True,
        description="Whether tool directives are injected into user messages for supported hosts.",
        scope=["global", "project", "session"],
    ),
    "agent.inject_rules_on_bootstrap": _setting(
        type="boolean",
        default=True,
        description="Whether project workflow and standards rules are loaded during bootstrap.",
        scope=["global", "project", "session"],
    ),
    "agent.directive_style": _setting(
        type="string",
        default="short",
        description="How action directives are delivered to the agent.",
        allowed_values=["short", "detailed"],
        value_descriptions={
            "short": "Concise 3-step directive chains.",
            "detailed": "Full directive lists with examples.",
        },
        scope=["global", "project", "session"],
    ),
    "global.aidocs_core_version": _setting(
        type="string",
        default="2.3.0b1",
        description="AIDOCS core version. Global setting that agents must never modify.",
        scope="global",
    ),
    # `dev.dev_mode` removed (2026-06-12); #404 (2026-07-16): self-edit /
    # dev authority is now ordinary authenticated admin authority on the
    # canonical AIDOCS source repo (enforcement.dev_mode_authorized).
    "tool_output.pretty": _setting(
        type="boolean",
        default=False,
        description=(
            "When ON: tool output is dual-channel — pretty rendering for "
            "the user (headers, diffs, footer) + terse ack for the agent. "
            "When OFF (default): raw tool payload only. Agent and user see "
            "the same thing."
        ),
        scope=["global", "project"],
    ),
    "tool_output.show_tool_name": _setting(
        type="boolean",
        default=False,
        description="Show tool name in inline debug marker (e.g. [ai_find ...]).",
        scope=["global", "project"],
    ),
    "tool_output.show_duration": _setting(
        type="boolean",
        default=False,
        description="Show per-call duration in inline debug marker (e.g. [... 42ms]).",
        scope=["global", "project"],
    ),
    "tool_output.show_tokens": _setting(
        type="boolean",
        default=False,
        description="Show estimated token counts in inline debug marker (e.g. [... in=120 out=840]). Estimated from payload byte size ÷ 4.",
        scope=["global", "project"],
    ),
    "security.allow_config_edit": _setting(
        type="boolean",
        default=False,
        description=(
            "[T0 DASHBOARD-ONLY] Unlocks AIDOCS config editing via the "
            "audited mcp__aidocs__config_set tool path. Per Invariant #40 "
            "this is a scope grant for an AUDITED route — raw shell writes, "
            "Edit/Write tools targeting config files, and any other path "
            "escaping config_set's validation layer remain blocked even "
            "when this is True. Per §6 override taxonomy, agents cannot "
            "grant this via NLP — dashboard direct-sqlite write only."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["project"],
    ),
    # The dev kill-switch key was removed (#404, 2026-07-16): the enforcement
    # kill-switch break-glass is excised entirely — no config key, no
    # facade, no bypass heads. Every gate enforces for every caller.
    "nlp.language": _setting(
        type="string",
        default="en",
        description=(
            "Force a specific language for NLP tool-intent detection, "
            "or 'auto' to let lingua detect per-prompt. Short English "
            "prompts are frequently misdetected as ES/PT/IT, which "
            "then pulls lemma sets from the wrong TOML. English-default "
            "dodges this; 'auto' restores multilang detection for "
            "operators whose prompts are mostly non-English. "
            "Accepted: en, it, de, es, pt, auto."
        ),
        allowed_values=["en", "it", "de", "es", "pt", "auto"],
        value_descriptions={
            "en": "Force English lemma sets for NLP intent detection.",
            "it": "Force Italian lemma sets.",
            "de": "Force German lemma sets.",
            "es": "Force Spanish lemma sets.",
            "pt": "Force Portuguese lemma sets.",
            "auto": "Let lingua detect language per-prompt (multilang operators).",
        },
        scope=["global", "project"],
    ),
    "nlp.update_gate": _setting(
        type="string",
        default="advise",
        description=(
            "Update-intent durability gate (#219/#221): when an operator "
            "prompt changes plan/spec/task/roadmap/priority/decision state, "
            "the deterministic detector asks for a durable record "
            "(ai_backlog/ai_task todo/ai_plan/memory_capture) before the change "
            "evaporates on compaction. 'advise' injects a repeating reminder "
            "and audits detected/satisfied/expired; 'off' disables; 'block' "
            "(future PR-2) additionally soft-blocks the first mutating tool "
            "call — currently treated as 'advise'."
        ),
        allowed_values=["off", "advise", "block"],
        value_descriptions={
            "off": "No detection; operator updates persist only by agent diligence.",
            "advise": "Detect + remind + audit (recommended; telemetry-first default).",
            "block": "Reserved for PR-2 (soft-block mutating tools); behaves as 'advise' today.",
        },
        scope=["global", "project", "session"],
    ),
    "run.max_timeout_seconds": _setting(
        type="integer",
        default=600,
        description=(
            "Hard ceiling on ai_run subprocess timeout (seconds). "
            "Agents pass timeout_seconds to ai_run; the value is "
            "clamped to this ceiling. Raise for projects with slow "
            "test suites or builds. Absolute max is 3600s — a safety "
            "rail that can't be exceeded regardless of this setting."
        ),
        scope=["global", "project"],
    ),
    "run.max_live_processes_per_machine": _setting(
        type="integer",
        default=4,
        description=(
            "Per-MACHINE ceiling on total live AIDOCS-managed processes "
            "(conductors + lane workers combined) on this host. Not "
            "global-config, not per-project — the actual constraint "
            "is OS/machine resources (RAM, file handles, model context "
            "per CLI subprocess). With the dashboard moving to network "
            "mode, one operator may control several machines; each "
            "machine keeps its own ceiling. Spawn attempts past the "
            "ceiling refuse cleanly and register nothing."
        ),
        scope=["global"],
    ),
    "workflow.tdd_mode": _setting(
        type="boolean",
        default=False,
        description=(
            "When true, task_complete's verification_gate enforces that "
            "sibling test files for edited sources are run. When false "
            "(default), verification_gate returns verified=True without "
            "requiring specific test commands — projects without a test "
            "suite aren't forced to have one. Flip this per project in "
            "the dashboard when the project actually practices TDD."
        ),
        scope=["global", "project"],
    ),
    "security.enforce": _setting(
        type="boolean",
        default=True,
        description="Tool gates active: bash allowlist, raw tool blocking, destructive command blocking.",
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.allow_raw_edits": _setting(
        type="boolean",
        default=False,
        description=(
            "[T0 DASHBOARD-ONLY] Operator override for the tier-0 "
            "raw-edit redirect. When True, raw Edit/Write/MultiEdit/"
            "NotebookEdit pass through instead of being routed to "
            "ai_replace / ai_create_file. Breaks the AIDOCS index "
            "freshness guarantee (other lanes see stale content, "
            "edit_history misses the change). Per Invariant #40 the "
            "AUDITED route is the AIDOCS edit family (ai_replace, "
            "ai_create_file, etc.); the RAW paths (host Edit/Write) "
            "remain blocked unless this flag is True. Per §6, dashboard-"
            "only — no NLP grant lifts this."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.allow_raw_read_of_secrets": _setting(
        type="boolean",
        default=False,
        description=(
            "[DASHBOARD-ONLY] Operator override that lets raw read tools "
            "open secret-shaped paths (.env, *.pem, SSH/cloud cred files, "
            "etc.) inside the project root. When False (default), "
            "read_pipeline._secrets_block refuses any read that classifies "
            "as secrets_gated and routes the operator to the audited path. "
            "Setting True opens raw reads of those paths and is logged in "
            "audit. Per §6, dashboard-only — no NLP grant lifts this."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.allow_inactive_memory_read": _setting(
        type="boolean",
        default=False,
        description=(
            "[DASHBOARD-ONLY] Audit/debug override that lets the "
            "memory_read MCP tool honor include_inactive=true and return "
            "SUPERSEDED / REMOVED memory. When False (default), an "
            "include_inactive=true request from an ordinary agent is "
            "IGNORED (retired memory stays suppressed) and an "
            "attempted_inactive_memory_read audit event is emitted. "
            "dev.dev_mode also lifts this (debug authority). The internal "
            "primitive MemoryStore.read_memory(include_inactive=True) is "
            "unaffected — this gate is only the agent-facing MCP surface. "
            "Per §6, dashboard-only — no NLP grant lifts this."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.allow_raw_shell": _setting(
        deprecated=(
            "Deprecated and ignored in managed AIDOCS sessions. Host-native "
            "shell stays T0-blocked. For governed native shell use the "
            "Governed Bash profile (`aidocs governed-bash-enable`); otherwise "
            "use ai_run with the Bash provider."
        ),
        type="boolean",
        default=False,
        description=(
            "[DEPRECATED 2026-04-29 — DASHBOARD-ONLY] Ignored in managed "
            "AIDOCS sessions. Host-native shell tools remain T0-blocked. "
            "Use ai_run with Bash provider (see "
            "security.superadmin_allow_powershell_ai_run_backend for the "
            "PowerShell escape hatch). Behavior in unmanaged mode "
            "unchanged for one release; setting True in managed mode "
            "emits a deprecation warning at runtime read. Per §6 — "
            "dashboard-only even for the deprecated path."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.approved_external_roots": _setting(
        type="string_list",
        default=[],
        description=(
            "[SEC-004 2026-04-23 — DASHBOARD-ONLY] List of absolute "
            "paths that tool calls may target without being treated as "
            "unknown or sensitive. A target path INSIDE any entry here "
            "is zoned as approved_external_workspace and skips the "
            "sensitive/unknown block. Prefix match. Entries OUTSIDE the "
            "project_root let the operator grant tool access to "
            "approved scratch dirs, sibling repos, etc. Sensitive home "
            "subdirs (.ssh, .aws, .gcloud, .azure, .config, AppData) "
            "are hard-blocked regardless of this list. Per §6 — "
            "dashboard-only; expanding the trust surface is an explicit "
            "operator-physical-presence decision."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "session.project_bind_ttl_minutes": _setting(
        type="integer",
        default=30,
        description=(
            "[2026-05-31] Idle TTL (minutes) for an ai_project(mode='bind') "
            "host-session→project binding. Any tool call that resolves the "
            "bind refreshes its activity; once idle past this many minutes "
            "the bind expires and resolution reverts to cwd-discovery. The "
            "configured value is captured INTO the bind row at bind time, so "
            "changing it affects future binds (not ones already live)."
        ),
        scope=["global", "project", "session"],
    ),
    "session.task_begin_autoscaffold": _setting(
        type="boolean",
        default=False,
        description=(
            "[#475 2026-07-19] Permit ai_task(mode='begin') against a "
            "NONEXISTENT session to SCAFFOLD it (same writer as "
            "ai_session create: SESSION.md + plans/agents/artifacts + "
            "membership registration) instead of refusing. Off by "
            "default: without this flag a missing session requires an "
            "operator ai_session create or a conductor-minted "
            "session-scaffold work-grant. Each scaffold is audit-"
            "stamped (execution event 'session_scaffolded')."
        ),
        scope=["global", "project", "session"],
    ),
    "security.emit_decision_trace": _setting(
        type="boolean",
        default=False,
        description=(
            "[SEC-012 2026-04-22] Emit a 'tool_decision_trace' "
            "execution event for every check_tool call. Each trace "
            "carries the per-layer gate breakdown (which checks ran, "
            "what they decided, why). Off by default — ~1-2kb per "
            "call adds up fast on busy sessions. Turn on when "
            "debugging why a specific call was blocked or allowed; "
            "the dashboard's 'Why?' button reads the trace payload."
        ),
        scope=["global", "project", "session"],
    ),
    "security.tier_enforcement": _setting(
        type="string",
        default="strict",
        description=(
            "[T0 DASHBOARD-ONLY] Tier enforcement policy. Tiers classify "
            "gates by trust surface: T0 (dashboard-only unblock — "
            "edit_redirect, raw_shell, shell_deny, test_retry, "
            "heuristic, infrastructure, foreground_cap, lane_scope, "
            "tool_policy) vs T1 (user-intent unblock — raw_tool, "
            "shell_allow, agent_brief). 'strict' keeps T0 dashboard-"
            "only and T1 user-intent; 'user_trust' additionally lets "
            "user-intent grants lift T0 (use sparingly — defeats the "
            "whole point of T0); 'off' downgrades all tier gates to "
            "advisory (logged, never blocked) — dev escape hatch. Per "
            "§6, the policy that controls T0 enforcement must itself "
            "be T0 — dashboard-only, no NLP grant."
        ),
        allowed_values=["strict", "user_trust", "off"],
        value_descriptions={
            "strict": "T0 dashboard-only, T1 user-intent. Default.",
            "user_trust": "T0 also lifts on user-intent grants. Rarely safe.",
            "off": "All tier gates advisory-only (logged, not blocked).",
        },
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project"],
    ),
    "security.tool_output_secret_policy": _setting(
        type="string",
        default="redact",
        description=(
            "[T0] How tool-output secret detection behaves before "
            "results enter the conversation context. Single explicit "
            "policy replaces the legacy security.output_guard + "
            "security.output_guard_redact booleans (hard-removed "
            "2026-04-28; legacy keys raise loudly)."
        ),
        allowed_values=["redact", "report_only", "allow_raw"],
        value_descriptions={
            "redact": "Scan tool results, replace any detected credentials with [REDACTED:category] markers before the agent sees them. Default — Castle-grade.",
            "report_only": "Scan tool results, audit findings to execution_events, but DO NOT modify the text. Agent receives raw output. For dev environments where you need to verify detection before flipping to redact.",
            "allow_raw": "Skip scanning entirely. Tool output reaches the agent unmodified. Use only for trusted local dev or controlled environments where output guard adds no value.",
        },
        security_sensitive=True,
        scope=["global", "project"],
    ),
    # ── Security / Freeze Policy ──────────────────────────────────────
    "security.agent_security_violation_freeze_threshold": _setting(
        type="integer",
        default=3,
        description=(
            "[T0 — Security / Freeze Policy] AGENT/TOOL-SIDE flat security "
            "violations (command_read_intent, sensitive_read, "
            "unknown_external, raw_shell_t0, blocked_sensitive_external) by "
            "the same actor/lane/family within a session before a "
            "repeated_security_violation freeze is created. Value help: "
            "0 = freeze escalation DISABLED (violations still BLOCK + AUDIT, "
            "just never freeze); 1 = freeze on the first violation; "
            "2 = freeze on the second (warning on the first); "
            "3 = freeze on the third (warning on the second). Strikes do not "
            "reset on a safe action — only operator/admin clearing the "
            "freeze (or a fresh session) resets the count. 'Disabled' "
            "disables freeze ESCALATION only, never the block or the audit."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    "security.operator_forbidden_prompt_freeze_threshold": _setting(
        type="integer",
        default=1,
        description=(
            "[T0 — Security / Freeze Policy] FORBIDDEN operator "
            "UserPromptSubmit verdicts before a hostile_operator_prompt "
            "freeze is created. A UPS is judged before the agent sees it, so "
            "the default is immediate. Value help: 0 = freeze escalation "
            "DISABLED (forbidden prompts still BLOCK + AUDIT, just never "
            "freeze); 1 = immediate freeze on the first forbidden prompt "
            "(default); 2 = freeze on the second forbidden prompt (optional "
            "ladder); N = freeze on the Nth. 'Disabled' disables freeze "
            "ESCALATION only, never the block or the audit. Independent of "
            "the agent threshold."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    "security.repeated_violation_freeze_threshold": _setting(
        deprecated=(
            "Superseded by security.agent_security_violation_freeze_threshold "
            "(per-agent tool-side threshold) and "
            "security.operator_forbidden_prompt_freeze_threshold (operator "
            "prompt threshold). Set those via the Freeze & Approval Policy "
            "profile in the operator surface instead of this combined alias."
        ),
        type="integer",
        default=3,
        description=(
            "[T0 — DEPRECATED alias] Superseded by "
            "security.agent_security_violation_freeze_threshold. When set "
            "(and the new agent key is NOT set) this value still drives the "
            "AGENT/TOOL-SIDE freeze threshold for one deprecation cycle. It "
            "never governed operator/UserPromptSubmit forbidden verdicts — "
            "those use security.operator_forbidden_prompt_freeze_threshold "
            "and create a hostile_operator_prompt freeze immediately on the "
            "first forbidden verdict. 0 = no freeze (block + audit only, "
            "escalation disabled). Migrate to the new key."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    "security.egress_allowlist": _setting(
        type="string_list",
        default=[],
        description=(
            "[DASHBOARD-ONLY] Allowlist of network egress destinations "
            "(hosts/URLs) that the heuristic judge's egress check permits "
            "for outbound commands (scp/curl/etc.). A destination NOT on "
            "this list is flagged EGRESS_BLOCKED_DESTINATION. Empty by "
            "default — no outbound destinations pre-approved. Per §6 — "
            "dashboard-only; widening the egress surface is an explicit "
            "operator decision, never an agent NLP grant."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.allow_palace_maintenance": _setting(
        type="boolean",
        default=False,
        description=(
            "[DASHBOARD-ONLY] POLICY switch (not identity) for the "
            "ai_palace_maintenance tool (e.g. mode=backfill_legacy_memory_"
            "drawers). Option A: maintenance requires BOTH an authenticated "
            "dashboard admin (operator token holding admin.palace_maintenance "
            "or admin.manage_config) AND this flag = true. When False "
            "(default) even an authenticated admin is refused "
            "(blocked_by=maintenance_policy_disabled); dev.dev_mode is the "
            "separate debug path that bypasses both. Per §6 — dashboard-only; "
            "a config flag enables policy but never replaces authentication."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.require_output_redaction_for_run": _setting(
        type="boolean",
        default=True,
        description=(
            "[T0] Fail-closed knob for the ai_run command output guard. "
            "When tool_output_secret_policy=redact and a run's stdout/"
            "stderr contains a credential but redaction cannot be "
            "applied (text field missing, guard error), True WITHHOLDS "
            "the offending output (replaces it with a marker) and emits "
            "run_output_guard_failed_closed. False lets the output "
            "through but marks the payload degraded and audits the "
            "bypass. Default True — a leaked secret is worse than a "
            "withheld log. report_only / allow_raw policies are "
            "unaffected (they never claim redaction)."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    # security.bash_allowed and security.bash_denied REMOVED 2026-04-25.
    # Superseded by the canonical [bash] declarative table. Operators
    # with legacy entries in sqlite get them ignored — schema no longer
    # validates them.
    "bash": _setting(
        type="string",
        default="",
        description=(
            "[T0 DASHBOARD-ONLY] Shell-policy namespace root for "
            "ai_run's shell-out gating. Holds the [bash.allow] / "
            "[bash.deny] declarative tables — operator-curated "
            "command allowlists/denylists matched per command family. "
            "Native host shells are transports over the exact same "
            "ai_run law and this one policy. Read as a tree by "
            "runtime_presentation_service to render the dashboard's "
            "Shell Policy page; written only via dashboard direct "
            "sqlite path. Per §6 override taxonomy, this is "
            "dashboard-only — agent-editable shell policy would "
            "defeat the purpose. Backlog #18: also holds the flat "
            "[bash.commands] family→verdict map ({<family>: 'allow' | "
            "'deny' | 'ask'}) which ranks above the pattern tables and "
            "below the unbypassable denylist/dangerous-chain layers; "
            "'ask' returns permissionDecision=ask (one-shot, never "
            "sticky)."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),

    "security.allow_allowlist_edit": _setting(
        type="boolean",
        default=False,
        description=(
            "[T0 DASHBOARD-ONLY] #100 FIX1: dedicated authority to edit shell "
            "allow/deny policy tables (bash.allow.* / bash.deny.* / *.allow.* / "
            "*.deny.*) via config_set. Default FALSE. Editing the shell "
            "allow/deny lists is the gateway to self-granting new shell "
            "commands, so it requires BOTH security.allow_config_edit AND this "
            "toggle — a single coarse 'can edit config' switch must not also "
            "mean 'can widen the shell execution surface'. DANGER when enabled: "
            "the agent can modify shell allow/deny rules. Never agent-editable."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.sticky_epoch_bound": _setting(
        type="boolean",
        default=True,
        description=(
            "[T0 DASHBOARD-ONLY] #99 FIX2: when True (default), sticky "
            "user-intent grants are bound to the agent_memory_epoch they were "
            "registered under — a compaction rotates the epoch and clears them "
            "from the enforcement surface, so the operator re-demonstrates "
            "intent in the fresh conversation context (closes the "
            "sticky-as-backdoor regression, Empire report 2026-05-01). False = "
            "persist-until-revoked for power users. Audit/display of grants is "
            "never epoch-filtered; only enforcement "
            "(active_tools/active_bash_subcommands) is."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    "security.judge_override": _setting(
        type="string_list",
        default=[],
        description=(
            "[T0 DASHBOARD-ONLY] LEGACY flat list of judge rule IDs to "
            "suppress for this project (e.g. GIT_FORCE_PUSH for sync "
            "repos). Backlog #19: superseded by the per-family rows "
            "security.judge_override.<family>; this flat list keeps "
            "working — judge_overrides.get_judge_overrides() auto-"
            "buckets it into families on read. Per §6 override "
            "taxonomy this is dashboard-only — bypassing specific "
            "judge rule_ids is a posture decision and never "
            "agent-grantable."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
    ),
    # Backlog #19 — per-family judge-rule opt-out rows. Each list holds
    # the rule_ids the operator has opted out of within that family;
    # the literal "@all" disables the whole family. Locked rules
    # (credential exfil, download-then-execute, catastrophic
    # destructive) are refused by judge_overrides.set_judge_override
    # AND ignored by flatten_judge_overrides — defense in depth.
    **{
        f"security.judge_override.{_family}": _setting(
            type="string_list",
            default=[],
            description=(
                f"[T0 DASHBOARD-ONLY] Judge rule IDs opted out of the "
                f"'{_family}' family (backlog #19 family-split). Empty "
                f"= family fully active. The literal '@all' disables "
                f"the whole family (locked rules stay active "
                f"regardless). Written via "
                f"judge_overrides.set_judge_override, which validates "
                f"locked rules and emits judge_rule_disabled/enabled "
                f"audit events."
            ),
            security_sensitive=True,
            dashboard_only=True,
            scope=["global", "project", "session"],
        )
        for _family in (
            "bash",
            "git",
            "file_write",
            "network",
            "dangerous",
            "credential",
            "general",
        )
    },
    # The universal-login toggle was removed (#404, 2026-07-16): login is
    # unconditionally required for every install flavor — there is no
    # local-admin passthrough to suppress.
    "security.freeze_all_sessions_on_malicious_intent": _setting(
        type="boolean",
        default=True,
        description=(
            "[T0 DASHBOARD-ONLY] When a forbidden (malicious-intent) operator "
            "prompt freezes a session, freeze the OPERATOR across ALL their "
            "sessions (matched by the authenticated user_id), and let one "
            "operator-scoped clear lift them all. Default true (secure). When "
            "false, the freeze is per-session (the redteam isolation opt). "
            "Login is always required (#404); with no resolved user_id the "
            "freeze stays per-session regardless."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global"],
    ),
    "agents.allow_subagents": _setting(
        type="boolean",
        default=False,
        description="[T1] Allow agent subprocess delegation. When false, the Agent tool is blocked and agents must use AIDOCS indexed tools directly. T1 means a user-intent grant ('delegate research') also lifts this when security.tier_enforcement=strict.",
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    "conductor.backend": _setting(
        type="string",
        default="claude",
        description="Default agent backend for the conductor.",
        allowed_values=["claude", "codex", "opencode"],
        value_descriptions={
            "claude": "Anthropic Claude agent via claude CLI.",
            "codex": "OpenAI Codex agent via codex CLI.",
            "opencode": "OpenCode agent (multi-provider, serve mode).",
        },
        scope=["global", "project", "session"],
    ),
    "conductor.max_concurrent_workers": _setting(
        type="integer",
        default=3,
        description=(
            "Global cap on simultaneously-running spawned workers. Each "
            "worker is a full Claude CLI subprocess with its own MCP "
            "server; more than 3-4 concurrent on a typical dev machine "
            "exhausts memory and provider rate limits. spawn_worker_async "
            "refuses new spawns once the cap is reached."
        ),
        scope=["global", "project", "session"],
    ),
    "conductor.auto_exit_lane": _setting(
        type="boolean",
        default=False,
        description=(
            "[T0] Sticky lane-exit for the conductor. When True, every "
            "UserPromptSubmit in a non-worker process auto-clears the "
            "session_query_gate row's current_lane_id + lane_exact_paths. "
            "Fixes the shared-row demotion trap for long overnight "
            "chains without the operator typing 'exit lane' each turn. "
            "Worker processes (AIDOCS_EXPERT_LANE_ID env set) are "
            "NEVER auto-exited — they'd escape their own lane sandbox. "
            "Default False: a stuck conductor is usually a real signal."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    # conductor.forced_work_mode REMOVED 2026-04-30 (autowake removal).
    # The setting and its enforcement were retired together — the
    # underlying mechanism could not actually achieve its goal (agents
    # could decline ScheduleWakeup and stall the session).
    "security.prompt_secret_policy": _setting(
        type="string",
        default="block",
        description=(
            "[T0] How AIDOCS handles a UserPromptSubmit whose text "
            "contains a provider-prefix credential token (AWS/GitHub/"
            "Stripe/OpenAI/Anthropic/Google/Slack/JWT/PEM/URI-with-creds). "
            "Single explicit policy replaces the legacy "
            "security.block_user_credentials boolean (hard-removed "
            "2026-04-28; legacy key raises loudly). "
            "Castle-grade note: canonical message rewrite ('redact in-place') "
            "is NOT supported here — most hosts (Claude Code, Codex, "
            "OpenCode, Cline) only expose block/add-context, not "
            "modifiedPrompt rewrite. So 'block' is the strongest "
            "guarantee available cross-host."
        ),
        allowed_values=["block", "allow"],
        value_descriptions={
            "block": "Refuse the turn. Operator sees 🛑 SECRET DETECTED, agent never receives the prompt. Default — strongest cross-host enforcement available.",
            "allow": "Permit the prompt through unchanged. Use only when the operator deliberately pastes credentials they want the agent to consume (and accepts the audit/leak risk).",
        },
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "observability.watch_user_drops": _setting(
        type="boolean",
        default=False,
        description=(
            "When True, a background filesystem watcher (folder-sitter) "
            "reindexes files the operator drops into the project so "
            "ai_find surfaces them without a manual ai_index_sync. "
            "AIDOCS-initiated writes are tagged with a per-edit marker "
            "and skipped — only user-originated changes trigger reindex. "
            "Default False: the pull-on-demand model already covers "
            "95% of cases; enable for local dev boxes where you "
            "frequently drag files in from outside the project. "
            "Incompatible with serverless / ephemeral environments "
            "(watchdog requires a long-lived process). 500ms debounce "
            "+ gitignore-aware; ~8MB RAM baseline."
        ),
        scope=["global", "project"],
    ),
    "observability.watch_user_drops_debounce_ms": _setting(
        type="integer",
        default=500,
        description=(
            "Folder-sitter debounce window in milliseconds. Events that "
            "arrive within this window get coalesced into one reindex "
            "call. 500ms handles a drag-drop burst of N files cleanly; "
            "lower values risk thrashing, higher values delay surfacing."
        ),
        scope=["global", "project"],
    ),
    "observability.project_index_sitter": _setting(
        type="boolean",
        default=True,
        description=(
            "When True (default), the ProjectIndexSitter keeps the code index "
            "truthful for externally added/edited/DELETED files that bypass "
            "AIDOCS tools. It runs an OS-agnostic polling reconciler (the "
            "always-on truthful fallback) with watchdog acceleration when "
            "available, suppresses AIDOCS self-writes, and sets a known-stale "
            "flag so read/discovery tools never silently serve a stale index. "
            "Disable on serverless/ephemeral hosts where a long-lived process "
            "isn't available — the pull-on-demand + read-tool staleness gate "
            "still apply."
        ),
        scope=["global", "project"],
    ),
    "observability.index_sitter_poll_seconds": _setting(
        type="integer",
        default=30,
        description=(
            "ProjectIndexSitter polling-reconcile interval in seconds (min 2). "
            "The poll loop catches missed watcher events and deletes even when "
            "watchdog is unavailable; lower = fresher but more rescans, higher "
            "= cheaper but slower to notice external changes."
        ),
        scope=["global", "project"],
    ),
    "outer_gate.enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "DISABLED by default. When True, the Outer Gate loopback skeleton "
            "(admission control only — NO network listener, NO token issuer, NO "
            "Tier-M mutation path yet) will admit tool discovery + invocation of "
            "ONLY manifest remote_eligible entries, enforcing authenticated "
            "principal + project-binding + mandatory audit. Leave False until the "
            "gateway is built out."
        ),
        scope=["global", "project"],
    ),
    "outer_gate.bind": _setting(
        type="string",
        default="127.0.0.1",
        allowed_values=["127.0.0.1", "::1", "localhost"],
        value_descriptions={
            "127.0.0.1": "IPv4 loopback (default).",
            "::1": "IPv6 loopback.",
            "localhost": "Loopback hostname.",
        },
        description=(
            "Loopback bind for the Outer Gate. Only loopback addresses are "
            "permitted; a non-loopback value is refused (no public bind in this "
            "phase)."
        ),
        scope=["global", "project"],
    ),
    "distribution.flavor": _setting(
        type="string",
        default="solo",
        allowed_values=["dev", "solo", "corpo"],
        value_descriptions={
            "dev": "AIDOCS contributor build: self-edit allowed, dev_mode implicit, no login. Install-path-locked — site-packages can't self-elevate to dev.",
            "solo": "Default for pip-installed single-operator boxes. Bootstraps the local user as super_admin; no login.",
            "corpo": "Team install. Login required; first-register becomes super_admin.",
        },
        description=(
            "Install flavor. `dev` = AIDOCS contributor build (self-"
            "edit allowed, dev_mode implicit, no login). `solo` "
            "(default) = pip-installed single-operator box, bootstrap "
            "local user as super_admin, no login. `corpo` = team "
            "install, login required, first-register becomes "
            "super_admin. `dev` is install-path-locked — code running "
            "from site-packages refuses to self-elevate to `dev` "
            "regardless of this setting."
        ),
        security_sensitive=True,
        scope=["global"],
    ),
    # AIDOCS shell provider lock — Batch A (canonical 2026-04-29).
    # Radioactive flag: observable but inert until Batch C ships the
    # PowerShell provider. See .MEMORY/system/security-gates.md
    # invariant "AIDOCS shell provider lock" + §6 + §7A.
    "security.superadmin_allow_powershell_ai_run_backend": _setting(
        type="boolean",
        default=False,
        description=(
            "[RADIOACTIVE — DASHBOARD-ONLY T0] When True AND a "
            "PowerShell provider implementation exists (Batch C), "
            "ai_run may dispatch via PowerShell. Until Batch C "
            "ships, this flag is observable in audit "
            "(shell_provider_resolved records "
            "backend='powershell_superadmin', verdict='rejected', "
            "rejection_reason='powershell provider not implemented') "
            "but does NOT enable PowerShell dispatch. Audited. NOT "
            "grantable by NLP / sticky / lane delegation. Default "
            "False; flip only on machines where Bash cannot be "
            "installed. The setting name is intentionally ugly so "
            "misuse is obvious."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global"],
    ),
    "audit.capture_prompt_content": _setting(
        type="boolean",
        default=False,
        description=(
            "When True, the full text of each UserPromptSubmit is "
            "stored in the execution_events payload. Default False: "
            "only prompt length, hash, and extracted metadata (grants, "
            "credential tokens detected, etc.) are recorded. Prompt "
            "content is sensitive — turn this on only when you need "
            "a replay-grade audit trail and the project policy allows "
            "it."
        ),
        scope=["global", "project"],
    ),
    "audit.capture_response_content": _setting(
        type="boolean",
        default=False,
        description=(
            "When True, the assistant's response text for each turn "
            "is stored in the execution_events payload on Stop. "
            "Default False: only metadata (tool_use_count, stop_reason, "
            "duration_ms, token totals) are recorded. Same sensitivity "
            "caveat as audit.capture_prompt_content."
        ),
        scope=["global", "project"],
    ),
    "observability.expose_pids": _setting(
        type="boolean",
        default=False,
        description=(
            "When True, every spawn tool (ai_run, "
            "agent_spawn_worker_async) and every read/poll tool "
            "(ai_run_status, ai_run_output, ai_status, "
            "agent_worker_jobs) includes the OS PID of the underlying "
            "process in its response. Default False to keep agent "
            "context clean — flip this on from the dashboard when "
            "debugging a runaway worker or when you need to correlate "
            "a process in Task Manager / htop with an AIDOCS run_id "
            "or worker_id."
        ),
        scope=["global", "project"],
    ),
    # conductor.autowake_mode and conductor.autowake_max_interval_seconds
    # REMOVED 2026-04-30 (autowake removal). Both settings were retired
    # with the rest of the autowake/forced-work feature.
    "conductor.opencode_model": _setting(
        type="string",
        default="",
        description="Default model for OpenCode backend (e.g. anthropic/claude-sonnet-4-20250514, openai/gpt-4o). Empty = OpenCode default.",
        scope=["global", "project", "session"],
    ),
    "conductor.claude_model": _setting(
        type="string",
        default="",
        description="Default model for Claude backend (e.g. claude-sonnet-4-6, claude-opus-4-6). Empty = Claude default.",
        scope=["global", "project", "session"],
    ),
    "conductor.codex_model": _setting(
        type="string",
        default="",
        description="Default model for Codex backend (e.g. gpt-5.4, gpt-5.3-codex, o3). Empty = Codex default.",
        scope=["global", "project", "session"],
    ),
    "conductor.task_routing": _setting(
        type="string",
        default="{}",
        description='JSON mapping task types to routing objects. Shape: {"implement":{"host":"claude","model":"claude-sonnet-4-6","think_mode":"low"},"design":{"host":"opencode","model":"google/gemini-2.5-pro","think_mode":"high"}}',
        scope=["global", "project", "session"],
    ),
    "conductor.think_mode": _setting(
        type="string",
        default="medium",
        description="Default logical reasoning depth for conductor routing. Used when a task route does not set think_mode explicitly.",
        allowed_values=["off", "low", "medium", "high"],
        value_descriptions={
            "off": "No extra reasoning budget; fastest, cheapest.",
            "low": "Brief reasoning; suitable for simple routing.",
            "medium": "Balanced reasoning depth (default).",
            "high": "Deep reasoning; for complex multi-step routing.",
        },
        scope=["global", "project", "session"],
    ),
    "conductor.require_agent_tests": _setting(
        type="boolean",
        default=False,
        description="Agents must write and run tests for their changes before reporting done. The conductor checks for test evidence in the dispatch report.",
        scope=["global", "project", "session"],
    ),
    "conductor.lane_allowed_tools": _setting(
        type="string_list",
        default=[
            "code_*",
            "session_*",
            "memory_*",
            "schema_*",
            "index_*",
            "plan_*",
            "execution_*",
            "task_*",
            "verification_*",
            # Phoenix 2026-05-09: ai_* glob added. The AIDOCS toolset
            # is `ai_*`-prefixed (ai_get_lines, ai_str_replace,
            # ai_create_file, ai_find, ai_text_search, ai_run, …).
            # The prior list only had `code_*` (legacy naming) +
            # specific `ai_run*` tools, so lane workers couldn't call
            # any read/edit tool without an explicit per-plan
            # `Allowed tools:` line (which silently dropped pre-fix).
            # Single glob covers the family; per-call gates still
            # enforce path/secret/DNT refusals.
            "ai_*",
            "skill_*",
            "context_*",
            "edit_history_*",
            # Phoenix 2026-05-12 (Empire directive): msg_* universal
            # comms surface. Every agent — workers, experts,
            # conductors, co-co — can send/receive role-addressed
            # messages. Targets stay predefined via the role enum
            # (msg_send to_roles param). Witnessed gap in smoke
            # 2026-05-11: lane workers couldn't call cerberus_*
            # (old name) and blocked on plan steps that required
            # mid-flight comms with the conductor.
            "msg_*",
        ],
        description="Glob patterns for tools allowed in conductor lanes. Agents in lanes can only use matching tools.",
        scope=["global", "project", "session"],
    ),
    "conductor.lane_extra_tools": _setting(
        type="string_list",
        default=[],
        description="Additional tool patterns allowed in lanes (for custom MCP tools). Added on top of lane_allowed_tools.",
        scope=["global", "project", "session"],
    ),
    "execution.max_events": _setting(
        type="integer",
        default=10000,
        description="Maximum execution events in the database. Oldest events are pruned when this limit is exceeded. 0 = unlimited (no count-based pruning).",
        scope=["global", "project"],
    ),
    "execution.auto_prune_days": _setting(
        type="integer",
        default=7,
        description="Auto-delete execution events older than this many days. Set 0 to disable.",
        scope=["global", "project"],
    ),
    "code_quality.comment_enforcement": _setting(
        type="string",
        default="advisory",
        description="Controls how strictly agent edits must follow comment-quality rules.",
        allowed_values=["strict", "advisory", "off"],
        value_descriptions={
            "strict": "Require comment-quality rules during agent edits.",
            "advisory": "Remind agents about comment-quality rules without blocking edits.",
            "off": "Disable comment-quality rule reminders and enforcement.",
        },
        scope=["global", "project", "session"],
    ),
    "memory.capture_gate.auto_merge": _setting(
        type="boolean",
        default=False,
        description=(
            "Controls how the memory-capture similarity gate handles "
            "an incoming capture that overlaps an existing memory. "
            "Default False (Empire doctrine 2026-05-17): gate refuses "
            "the write and returns 'needs_user_clarification' with "
            "the conflicting memory paths — the agent must read "
            "them and ask the operator. True: gate evaluates from "
            "content overlap and auto-merges via UPGRADE without "
            "user prompt. Operators in trusted contexts can flip "
            "True to reduce friction; the dashboard toggle wraps "
            "this setting."
        ),
        scope=["global", "project"],
    ),
    "edit.str_replace_max_old_chars": _setting(
        type="integer",
        default=1000,
        description=(
            "Maximum characters allowed in `old_str` for "
            "ai_replace(mode='string') calls. History: tightened "
            "2026-05-01 from 2000 to 500 (Empire doctrine #113), "
            "re-tuned 2026-05-10 500 to 1000 as a context-hygiene "
            "compromise. Large enough for most real refactors, "
            "still small enough to steer operators toward "
            "ai_replace(mode='anchor') for spans or "
            "ai_replace(mode='symbol') for index-resolved body "
            "rewrites on bigger work. Only old_string is capped; "
            "new_string is unlimited. 0 = unlimited (no cap on old_string)."
        ),
        scope=["global", "project", "session"],
    ),
    "edit.line_edit_relock_free_span": _setting(
        type="integer",
        default=10,
        description=(
            "Only one line-based edit (ai_replace(mode='lines') / ai_insert_lines) per file per "
            "turn is allowed by default — the first edit shifts line numbers so the next "
            "call's start/end would be stale. A SECOND line-edit is allowed without "
            "re-reading when it touches at most this many lines (small drift). Larger "
            "edits must first re-read the file (ai_get_lines / ai_bundle / "
            "ai_get_symbol_snippet — a targeted range is enough), which releases the lock "
            "with fresh line numbers; ai_replace(mode='string') / ai_batch_edit always bypass. "
            "0 disables the free span (every 2nd line-edit then needs a re-read). "
            "Resets each user prompt."
        ),
        scope=["global", "project", "session"],
    ),
    "notifications.max_displays": _setting(
        type="integer",
        default=3,
        description=(
            "Displays-until-dismissed: maximum times a run-done "
            "notification re-surfaces in tool-call envelopes before "
            "auto-dismissing itself. Default 3 — three surfaces, "
            "then the record drops from the queue even if the agent "
            "never read the run's output. 0 = classic 'until "
            "satisfied' behavior (persists forever until "
            "ai_run_output dismisses on output read). Added "
            "2026-05-10 to bound 📣 noise across long sessions."
        ),
        scope=["global", "project", "session"],
    ),
    "presentation.helper_skill_excerpt_lines": _setting(
        type="integer",
        default=12,
        description="Maximum non-empty lines injected from a helper skill into host context. 0 = unlimited (no line truncation).",
        scope=["global", "project"],
    ),
    "presentation.helper_skill_excerpt_chars": _setting(
        type="integer",
        default=1200,
        description="Maximum characters injected from a helper skill into host context. 0 = unlimited (no char truncation).",
        scope=["global", "project"],
    ),
    "presentation.workflow_summary_limit": _setting(
        type="integer",
        default=3,
        description="Maximum workflow actions shown in compact workflow summaries. 0 = unlimited (show all).",
        scope=["global", "project"],
    ),
    "presentation.resume_journal_last_n": _setting(
        type="integer",
        default=10,
        description="Default journal entry count returned by session resume bundles. 0 = unlimited (all entries).",
        scope=["global", "project", "session"],
    ),
    "presentation.handoff_stale_after_hours": _setting(
        type="integer",
        default=24,
        description="Hours after which handoff freshness is considered stale.",
        scope=["global", "project"],
    ),
    "presentation.handoff_recent_hours": _setting(
        type="integer",
        default=24,
        description="Hours during which a handoff step counts as recently changed.",
        scope=["global", "project"],
    ),
    # ── Phase 1 catalog completion (2026-05-02) — keys read by code but
    # previously absent from the catalog. Each carries §6/§40 doctrine
    # flagging per security-gates.md.
    # `audit.dev_mode_bypass` / `rbac.dev_mode_bypass` removed (#404,
    # 2026-07-16): the dev-flavor bypass surface is excised — audit-trail
    # and RBAC enforcement have no config-driven escape hatch.
    "policies.dangerous": _setting(
        type="string",
        default="[]",
        description=(
            "[T0 DASHBOARD-ONLY] JSON array of operator-tunable "
            "dangerous-pattern overlays for the heuristic_judge. "
            "Merged with the ~30 builtin _BUILTIN_DANGEROUS entries "
            "at runtime per project. Each entry produces one CFG_* "
            "rule_id; severity inherits from the entry's risk field. "
            "Per security-gates.md §4.17 — dashboard-only because "
            "loosening dangerous-pattern detection is a posture "
            "decision."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project"],
    ),
    "policies.tools": _setting(
        type="string",
        default="[]",
        description=(
            "[T0 DASHBOARD-ONLY] JSON array of operator-tunable "
            "tool-policy entries. Each maps a tool match to "
            "allow/deny verdict, optionally tied to scope or path "
            "prefix. Used by tool_policy module to gate non-bash "
            "tools. Dashboard-only — agent-mediated grants for "
            "policy edits would defeat the whole point."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project"],
    ),
    "security.delegate_research_allowed": _setting(
        type="boolean",
        default=False,
        description=(
            "[T1] Allow conductors to delegate research subagents. "
            "When false, claude_hook refuses Task/agent-spawn calls "
            "for research patterns. T1: a user-intent grant of the "
            "form 'allow delegate research' lifts this for the turn "
            "when security.tier_enforcement is 'strict' or "
            "'user_trust'. Read in claude_hook."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    "security.exempt_extensions": _setting(
        type="string_list",
        default=[
            ".output",
            ".log",
            ".txt",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            ".pdf",
            ".ipynb",
        ],
        description=(
            "File extensions exempt from secret-scanning and "
            "tool-output redaction. Default set covers obvious "
            "non-text payloads (logs, images, PDFs) and the .output "
            "/ .log convention. Per Invariant #40 this is a scope "
            "grant — adding extensions widens what AIDOCS treats as "
            "safe-content; removing extensions is always safe."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.exempt_paths": _setting(
        type="string_list",
        default=[],
        description=(
            "Absolute or project-relative paths exempt from secret "
            "scanning. Use sparingly — adds blind spots to the "
            "credential-detection layer. Per Invariant #40, "
            "operator-set exemptions are an explicit policy "
            "statement; the gate still fires on non-listed paths."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.protected_patterns": _setting(
        type="string_list",
        default=[
            "tokens.json",
            "keys.json",
            "auth.json",
            "secrets.json",
            "*.local.json",
            "appsettings.*.json",
        ],
        description=(
            "Glob patterns for files that read tools must never "
            "surface to the agent. Defaults cover common credential-"
            "store filenames. Operators add project-specific patterns "
            "(e.g. 'config/*.production.yaml'). Path-trust-zone "
            "checks consult this list before serving file content."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.explicit_confirm_on_grant": _setting(
        type="boolean",
        default=False,
        description=(
            "[T1] When True, every NLP-grant for a tier-1 non-raw "
            "tool requires an additional 'explicitly' word in the "
            "phrase before the registration judge auto-allows. "
            "When False (default), tier-1 non-raw grants auto-allow "
            "if the proximity check passes. Tier-1 raw and tier-2 "
            "always require_confirm regardless of this flag. Read "
            "in gate_confirm and grant_registration_judge."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "dev.runtime.ai_run_trace": _setting(
        type="boolean",
        default=False,
        description=(
            "[DEV-FLAVOR ONLY] When True (and distribution.flavor="
            "'dev'), every ai_run boundary emits a structured trace "
            "event with command, exit code, duration, and shell "
            "provider details. Used by AIDOCS contributors to debug "
            "shell dispatch. No-op on solo/corpo flavors regardless "
            "of value. Read in code_runner_detached, server_run_tools, "
            "shell_resolver."
        ),
        scope=["global", "project", "session"],
    ),
    "pg.connection_string": _setting(
        type="string",
        default="",
        description=(
            "Postgres connection string for legacy git/db tools. "
            "Format accepted: postgresql://user:pass@host:port/db or "
            "key=value pairs. Read by server_legacy_git_tools when "
            "scanning a project for db connections (.env, "
            "appsettings.json, alembic.ini are checked first). Empty "
            "= no aidocs-config override; fall through to filesystem "
            "scan. Operator-data, not a security toggle."
        ),
        scope=["global", "project", "session"],
    ),
}


def available_config_edit_modes(profile: str = "release") -> list[ConfigEditMode]:
    if profile != "release":
        raise ValueError(f"Unknown config edit profile: {profile}")
    return ["explicit_user_permitted"]


def self_edit_available_in_profile(
    profile: str = "release",
    project_root: Path | None = None,
) -> bool:
    """Self-editing of AIDOCS source is authorized ONLY on a DEV-flavour
    install whose project_root IS the canonical AIDOCS source repo.

    Authority change (2026-06-12): the `dev.dev_mode` config toggle is
    removed. There is no config flag and no caller-privilege gate — on a
    contributor (DEV-flavour) source build every agent, conductor OR
    spawned subagent/lane worker, may edit the source, because that IS the
    dev workflow. On any other install nobody can. Fails closed.
    """
    if profile != "release":
        raise ValueError(f"Unknown config edit profile: {profile}")
    from .enforcement import dev_mode_authorized

    return bool(dev_mode_authorized(project_root))


def is_setting_agent_editable(
    setting_path: str,
    *,
    scope: SettingScope = "project",
    edit_mode: ConfigEditMode | None = None,
) -> bool:
    if edit_mode != "explicit_user_permitted":
        return False

    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None or metadata["security_sensitive"]:
        return False
    if scope not in metadata["allowed_scopes"]:
        return False
    # Agents must never write to global config — that is human-owned and
    # install-wide. Project and session scopes are agent-editable.
    if scope == "global":
        return False

    editable_scopes = metadata["agent_editable_scopes"] or [
        allowed_scope for allowed_scope in metadata["allowed_scopes"] if allowed_scope == scope
    ]
    return scope in editable_scopes


def validate_setting_value(setting_path: str, value: object) -> None:
    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None:
        raise ValueError(f"Unknown config setting: {setting_path}.")

    setting_type = metadata["type"]
    if setting_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Config setting {setting_path} requires an integer value.")
    elif setting_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Config setting {setting_path} requires a boolean value.")
    elif setting_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"Config setting {setting_path} requires a string value.")
    elif setting_type == "string_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"Config setting {setting_path} requires a list of strings.")
    else:
        raise ValueError(f"Unsupported config setting type for {setting_path}: {setting_type}.")

    allowed_values = metadata["allowed_values"]
    if allowed_values is not None and isinstance(value, str) and value not in allowed_values:
        allowed = ", ".join(allowed_values)
        raise ValueError(f"Config setting {setting_path} must be one of: {allowed}.")
