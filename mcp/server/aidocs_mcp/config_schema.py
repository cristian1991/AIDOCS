"""Settings catalog — defines all AIDOCS configuration settings.

This stays intentionally flat: each entry is keyed by the dotted TOML path and
describes the setting without introducing a second nested config model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, TypedDict

# #749 (operator ruling 2026-08-27): "the [T0] tag should depend on is_t0,
# having 2 stuff for 1 functionality is wrong." Matches the tag and the
# whitespace before it, so stripping leaves the sentence intact. Deliberately
# tolerant of variants ([T0], [T0 guardrail], ...) — the FIRST pass's guard
# matched the open bracket `[T0`, which means variants exist in the wild, and a
# variant that survived the strip would be exactly the second copy this closes.
_T0_TAG_RE = re.compile(r"\s*\[T0[^\]]*\]")
_T0_MARKER = "[T0]"

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
    # is_t0: the guardrail TIER, as a FIELD (#749, 2026-08-03).
    # It used to live in the prose: runtime_presentation_service derived the
    # dashboard's T0 badge with `"[T0" in description`, so the security posture
    # of a setting depended on a bracket surviving inside an English sentence. A
    # routine copy edit — by a writer with no reason to suspect the description
    # was load-bearing — could silently un-flag a guardrail with every test
    # still green. That near miss actually happened while rewriting this
    # catalog for readability; it was caught by hand, which is not a control.
    # The tier is now data. `description` is free to be prose.
    is_t0: bool


def coerce_setting_value(setting_path: str, value: object) -> object:
    """Coerce a string value to the catalog type (bool/int) so a write and its
    readback compare in the same type space.

    Lives here, beside SETTINGS_CATALOG, because TWO surfaces need the identical
    rule: the CLI (which has had it since it needed readback parity) and the
    operator surface's expert_set (#747, which had NO typing at all and stored
    the string 'false' for a boolean DANGER-flagged authority -- truthy, so the
    operator's OFF read as ON everywhere downstream).

    SAFE IN THE DIRECTION THAT MATTERS: an unrecognised string resolves to
    False, so garbage can never silently ENABLE something. Only 'true', '1' and
    'yes' grant. No-op for unknown keys (e.g. the canonical bash.* runtime
    policy namespace) and for values already of the right type.
    """
    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None:
        return value
    expected_type = metadata.get("type", "string")
    if expected_type == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if expected_type == "boolean" and isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return value


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
    # Named to MATCH the metadata field, like every sibling flag
    # (dashboard_only / service_managed / deprecated all use one name for the
    # field, the parameter and the call sites). The first cut called this `t0`,
    # which left the identifier `is_t0` appearing only as a string subscript —
    # so the deploy gate's vulture lane correctly reported it as unused. The
    # finding was about naming drift, not dead code, and the answer is the
    # consistent name rather than an allowlist waiver.
    is_t0: bool = False,
) -> SettingMetadata:
    # #749 (2026-08-27): THE PROSE SEED IS GONE. Until now this helper fell back
    # to `"[T0" in description` whenever the flag was not passed, which is how 38
    # of the catalog's 43 guardrails got their tier — from a bracket surviving
    # inside an English sentence. A readability pass over operator-facing copy
    # (the exact work that surfaced this) would have silently un-flagged all 38
    # with every test still green. Each of those 38 now carries is_t0=True.
    #
    # The tag is still ALLOWED in the prose as a human-readable echo, but it is
    # now checked against the field rather than consulted as one. A tag with no
    # flag is the residual hole — a new setting written in the house style whose
    # author forgets the keyword — and treating it as not-T0 would fail OPEN on
    # a security marker. So it is a HARD ERROR at import: the module will not
    # load until someone states the tier as data.
    if "[T0" in description and not is_t0:
        raise ValueError(
            "setting description carries the '[T0' tag but does not pass "
            "is_t0=True. The guardrail tier is a FIELD, not a substring of the "
            "description (#749). Pass is_t0=True if this setting is a T0 "
            "guardrail; otherwise remove the tag from the prose."
        )
    # OPERATOR RULING 2026-08-27: "the [T0] tag should depend on is_t0, having
    # 2 stuff for 1 functionality is wrong."
    #
    # The guard above closes ONE direction — tag present, flag absent. The
    # other stayed silent: a T0 guardrail whose prose simply LACKS the marker
    # renders as an ordinary setting, so the sentence the operator reads says
    # "not a guardrail" while the field says it is. No error fires, because
    # there is nothing to compare against.
    #
    # So the marker is DERIVED, not stored beside the field. Any tag in the
    # incoming prose is stripped and re-rendered from is_t0, which makes the
    # two impossible to disagree: the prose is no longer a copy of the fact,
    # it is a projection of it. An author who writes the tag gets it
    # normalised; an author who forgets it gets it added.
    #
    # The guard stays anyway, and is checked BEFORE the strip: writing the tag
    # without the flag is an author stating a tier they did not set, and
    # silently normalising that away would swallow the mistake instead of
    # surfacing it.
    _clean = _T0_TAG_RE.sub("", description).rstrip()
    description = f"{_clean} {_T0_MARKER}" if is_t0 else _clean
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
        "is_t0": is_t0,
    }


SETTINGS_CATALOG: dict[str, SettingMetadata] = {
    # ── dashboard.* — NAMESPACE REMOVED 2026-08-27 by operator ruling ──
    # It held exactly one key, `dashboard.auto_bind_local_sessions`, and the
    # ruling on #559 was verbatim: "559. no, remove it, no auto-bind."
    #
    # The key was WRITABLE and had ZERO backend readers — the toggle stopped
    # lying in the ledger (it had been refused as unknown_setting) only to go on
    # lying in the UI. Law 183074ae: a capability with no consumer is not a
    # capability. It was NOT wired, and the reason it stayed unwired is the
    # reason it is now gone: implementing it means auto-APPROVING host-session
    # bindings, i.e. minting operator authority from a stored flag, on top of the
    # `project_authority` path (c) that already authenticates local sessions
    # while a machine login is live. Two authority paths that can disagree is a
    # worse outcome than no auto-bind, and the operator chose no auto-bind.
    #
    # Tombstoned in config_store._REMOVED_SETTINGS so an operator who ticked the
    # box gets their stored row swept and a loud message on read, rather than a
    # silent fall-through to a default. Re-adding this key requires a NEW
    # operator ruling — no agent may supply one.

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
            "For a shared local HTTP daemon serving several projects: when "
            "on, a tool call that does not say which project it belongs to "
            "(no ?root= / X-AIDOCS-Project-Root) is refused with instructions "
            "instead of being guessed — guessing is how one project's calls "
            "can leak into another. Enable only after every project's "
            ".mcp.json has been regenerated with the scoped ?root= URL, or "
            "older configs will start failing. Off (default) keeps the old "
            "resolve-by-process behavior; stdio and single-project setups "
            "are unaffected either way. (#280)"
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
            "At the end of each assistant turn, scan the reply for durable "
            "statements — rules, invariants, decisions, preferences — that "
            "are not already in memory, and save them automatically, marked "
            "as auto-captured (source=stop_capture). Off by default and "
            "strictly opt-in, because it stores assistant text without you "
            "asking. Turn on if you want a self-maintaining memory and "
            "accept occasional noise. Subagent (lane worker) turns are never "
            "captured regardless. (#316)"
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
    # ── test.* (ai_test runtime, 2026-08-29) ──
    #
    # WHY A DECLARATION EXISTS AT ALL. ai_test resolved the project's test
    # interpreter by DISCOVERY ONLY — `<cwd>/.venv/bin/python` and its two
    # siblings. That can never succeed on a CLONE: `.venv` is untracked
    # (`git ls-files mcp/.venv` is empty), and a clone is exactly what the gate
    # syncs for a tenant. Measured live on WebMCP: cwd resolution worked, the
    # auto-`mcp/` subtree rule worked, and the run still refused — because
    # `mcp/.venv/bin/python` does not exist there and structurally cannot.
    #
    # The box is NOT missing a test runtime. The VPS builds a full one every
    # deploy (gate 2b runs the whole suite under the custody dir); the resolver
    # simply could not see any interpreter outside the clone. This key is how a
    # box points at the one it already has.
    #
    # NOT AUTO-PROVISIONING: building the env on demand means running `pip
    # install` off a tenant's lockfile — executing that repo's build hooks as
    # the gate user on a shared host. A declared path adds NO execution surface;
    # it names an interpreter that already exists and was already trusted by
    # whoever installed it.
    "test.interpreter": _setting(
        type="string",
        default="",
        description=(
            "Absolute (or test-root-relative) path to the python that runs "
            "ai_test for this project. Set it when the checkout has no .venv — "
            "a git clone never does, since .venv is untracked. Empty = "
            "discover <test root>/.venv/{bin/python,bin/python3,"
            "Scripts/python.exe}, and refuse naming every path tried."
        ),
        scope=["global", "project"],
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
        description=(
            "Long-running shell commands (test suites, npm/pip/yarn installs, "
            "cargo, docker) are blocked from running in the foreground and must "
            "be started in the background, so one slow command cannot wedge the "
            "whole conversation; this is the cap in seconds. Set 0 to turn the "
            "hard block into a warning only — foreground long-runners can then "
            "stall the session. [T0]"
        ),
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.shell_enforcement_live": _setting(
        type="boolean",
        default=False,
        description=(
            "Master switch that makes the new shell-policy engine the authority "
            "for the host's built-in shell tools (Bash/PowerShell/cmd). Off "
            "(default): the existing gate chain decides. On: ShellPolicy "
            "decides — but in this phase no native command actually executes; "
            "anything it would allow still runs through the managed ai_run path, "
            "and the structural gates (managed mode, freezes) still apply above "
            "it. Only enable when the Governed Bash setup flow tells you to. "
            "Dashboard-only: agents can never flip it. [T0] (Batch 2.0-A)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.shell_lifecycle_preflight_enforce": _setting(
        type="boolean",
        default=False,
        description=(
            "Inspects a command before it runs and stops ones that would "
            "trigger hidden code execution: package install scripts, build and "
            "git hooks, interpreters running downloaded code. Off (default): "
            "the check still classifies and logs what it WOULD have blocked, "
            "but changes nothing. On: those commands are denied, or require an "
            "operator hold before they start — turning it on too early can "
            "block legitimate builds, so review the logged verdicts first. "
            "Applies identically to native shell and ai_run. Dashboard-only. "
            "[T0] (future_sight_preflight)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_readonly_enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "Leftover switch from the two-shell design: it no longer turns "
            "anything on or off. Whether the host's built-in shell may run a "
            "command is decided by tools.shell_enforcement_live and the shell "
            "policy; this flag survives only as capability evidence in "
            "telemetry. Safe to leave at its default — changing it does not "
            "change enforcement. Dashboard-only. [T0] (demoted 2026-06-06; "
            "history: two-transport shell architecture, governed_bash)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_readonly_extra_commands": _setting(
        type="string",
        default="",
        description=(
            "Extra command names the host's built-in shell may run in governed "
            "read-only mode, on top of the built-in allowlist (separate entries "
            "with ';' or ','). Use it for genuinely read-only tools such as jq, "
            "yq, rg, fd, bat. Anything that can write, reach the network, or "
            "run code (rm, curl, ssh, python, npm, sed, env, ...) is silently "
            "dropped and can never be re-enabled here, and every added command "
            "still passes the no-metacharacter/no-redirection guards. Empty by "
            "default. Dashboard-only. [T0] (Batch 2.0-B1)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_trusted_roots": _setting(
        type="string",
        default="",
        description=(
            "Folders where shell executables are allowed to live (paths "
            "separated by ';' on Windows, ':' elsewhere, or ','). A shell "
            "program is only trusted when the file actually exists under one "
            "of these roots and its filename matches the expected shell — this "
            "is what stops a look-alike binary elsewhere on disk from being "
            "accepted. Empty (default) means nothing can be verified, so "
            "native shell execution stays off (fail closed). Usually set by "
            "the Governed Bash wizard. Dashboard-only. [T0] (Batch 2.0-B)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_provider_enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets the host's built-in shell tools (Bash/PowerShell/cmd) "
            "execute commands directly instead of always routing them through "
            "the managed ai_run runner. Even when on, direct execution is only "
            "permitted for host/shell pairs that have proven they can be "
            "supervised (command visibility + hard-deny hooks); anything "
            "unproven falls back to ai_run, which stays the canonical runner "
            "regardless. This widens what runs natively on your machine — "
            "leave off unless the Governed Bash setup told you to enable it. "
            "Dashboard-only. [T0] (Batch 1)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_provider_path": _setting(
        type="string",
        default="",
        description=(
            "Full path to the exact shell program AIDOCS should treat as your "
            "shell (e.g. C:\\Program Files\\Git\\usr\\bin\\bash.exe). Normally "
            "set by the Governed Bash wizard, which checks the file exists, is "
            "a file, sits under a trusted root, and has the right name. "
            "Pinning it stops a different shell binary from being picked up "
            "off PATH. Empty = autodetect from PATH for posture checks. "
            "Dashboard-only. [T0] (Governed Bash)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_provider_sha256": _setting(
        type="string",
        default="",
        description=(
            "Optional fingerprint (SHA-256, hex) of the pinned shell program. "
            "When set, the shell is accepted only if the file on disk matches "
            "this exact hash — so even a swapped binary inside a trusted "
            "folder is rejected. Empty (default) skips the fingerprint check; "
            "path and trusted-root identity are still required. If you set it, "
            "remember to update it whenever you update the shell. "
            "Dashboard-only. [T0] (Governed Bash)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.native_shell_require_os_signature": _setting(
        type="boolean",
        default=False,
        description=(
            "When on, the pinned shell program must carry a valid OS code "
            "signature (e.g. Windows Authenticode) or it is rejected; if the "
            "platform cannot verify signatures at all, it is also rejected "
            "(fail closed). Off by default because many legitimate shells "
            "(e.g. Git-for-Windows bash) ship unsigned — only turn this on if "
            "your pinned shell is actually signed. Dashboard-only. [T0] "
            "(Governed Bash)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        service_managed="governed_bash",
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.shell_disconnect_after_seconds": _setting(
        type="integer",
        default=0,
        description=(
            "Reserved for a future release: how many seconds a foreground "
            "native shell command may run before AIDOCS detaches it into the "
            "background (handing back a run_id) instead of freezing the "
            "conversation. 0 (default) = off. Nothing implements the detach "
            "yet, so changing this currently has no effect; ai_run is already "
            "always-detached. Dashboard-only. [T0] (Batch 2 reserved, "
            "ShellCommandEnvelope)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.shell_policy_shadow_enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "Observation mode for the new shell-policy engine: each native "
            "shell call is ALSO evaluated by the new engine, and any "
            "disagreement with the live gate's decision is written to the "
            "audit log (shell_policy_shadow). Enforcement does not change — "
            "the live gates still decide, no extra commands run, no freezes "
            "are created. Turn on to collect evidence that the new engine "
            "agrees with the old one before making it live; the only cost is "
            "extra audit rows. (Batch 1.5)"
        ),
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
    "oauth.chatgpt_redirect_uris": _setting(
        type="string_list",
        default=[],
        description=(
            "The exact https callback URL(s) of the single ChatGPT connector "
            "allowed to sign in to this gate (e.g. https://chatgpt.com/"
            "connector/oauth/<per-connector-token>). This list IS the "
            "binding: sign-in only succeeds from a connector whose callback "
            "matches exactly, so a different connector is refused even though "
            "the public client id is guessable. Empty (default) = no "
            "connector is bound and sign-in is refused. Never widen an entry "
            "to a whole-domain prefix — that would let authorization codes "
            "be redirected anywhere on chatgpt.com. Global because the "
            "connector binds the whole gate, not one project."
        ),
        security_sensitive=True,
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
            "Lets agents change AIDOCS settings through the audited config_set "
            "tool. Off (default): agents can read settings but not change "
            "them. On: agents may edit ordinary settings — every change is "
            "validated and logged, and raw shell or file edits to config files "
            "remain blocked either way. Turn it on when you want the agent to "
            "tune non-security settings for you; security-sensitive and "
            "dashboard-only keys stay locked regardless, and an agent cannot "
            "grant itself this switch. [T0] (Invariant #40, §6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["project"],
        is_t0=True,
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
            "When your prompt changes plans, priorities, tasks or decisions, "
            "AIDOCS can notice and remind the agent to write the change into "
            "a durable record (backlog, plan, todo, memory) before it is "
            "lost to context compaction. 'advise' (default): detect, remind "
            "repeatedly, and audit. 'off': no detection — updates survive "
            "only if the agent thinks to record them. 'block' is reserved "
            "for a future release and currently behaves like 'advise'. "
            "(#219/#221)"
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
        description=(
            "Master switch for the tool-level security gates: the shell "
            "command allowlist, raw-tool blocking, and destructive-command "
            "blocking. On by default. Turning it off removes those "
            "protections for every agent in scope — do not do this on a "
            "machine that matters."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.allow_raw_edits": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets the agent use its host's raw file-editing tools (Edit/Write/"
            "MultiEdit/NotebookEdit) instead of being redirected to the AIDOCS "
            "editing tools. Off (default): raw edits are rerouted so every "
            "change lands in the code index and the edit history. On: raw "
            "edits pass through — the index can then go stale and edit "
            "history misses those changes, so other agents may act on "
            "outdated file contents. Enable only for debugging the edit "
            "pipeline itself. Dashboard-only: no prompt-level grant lifts "
            "this. [T0] (Invariant #40, §6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.allow_raw_read_of_secrets": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets raw file-reading tools open credential-shaped files inside "
            "the project (.env, *.pem, SSH and cloud key files, and similar). "
            "Off (default): such reads are refused and routed to the audited "
            "path. On: the agent can read those files directly, which means "
            "their contents can end up in the conversation and anything that "
            "logs it; each read is still audited. Enable only briefly, and "
            "only when you accept that exposure. Dashboard-only: no "
            "prompt-level grant lifts this. (§6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "security.allow_inactive_memory_read": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets the memory_read tool return retired memory — entries that "
            "were superseded or removed. Off (default): retired memory stays "
            "hidden even when explicitly requested (include_inactive=true is "
            "ignored) and the attempt is logged. On: agents can see retired "
            "entries — useful when auditing or debugging why memory changed, "
            "but those entries may contain exactly the outdated guidance they "
            "were retired for. Only affects the agent-facing tool, not "
            "internal reads. Dashboard-only. (§6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
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
            "Does nothing anymore in managed AIDOCS sessions — the host's own "
            "shell tools stay blocked regardless of this value, and setting "
            "True only produces a deprecation warning. To run shell commands, "
            "use ai_run, or enable the Governed Bash profile for a supervised "
            "native shell. Unmanaged-mode behavior is unchanged for one more "
            "release. Dashboard-only. [T0] (deprecated 2026-04-29, §6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.approved_external_roots": _setting(
        type="string_list",
        default=[],
        description=(
            "Folders outside the project that tools are allowed to touch "
            "(absolute paths; anything under a listed folder counts as "
            "approved workspace). Empty by default, so paths outside the "
            "project are blocked as unknown. Add entries for scratch folders "
            "or sibling repos the agent legitimately needs — each entry "
            "widens what the agent can reach on this machine. Sensitive home "
            "folders (.ssh, .aws, .gcloud, .azure, .config, AppData) stay "
            "blocked no matter what is listed. Dashboard-only: expanding the "
            "trust surface is an operator decision. (SEC-004, §6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "session.project_bind_ttl_minutes": _setting(
        type="integer",
        default=30,
        description=(
            "How many minutes a host-session-to-project binding survives "
            "without activity before it expires and project resolution falls "
            "back to the working directory. Any tool call that uses the "
            "binding refreshes the timer. The value is captured into each "
            "binding when it is created, so changing it affects future "
            "bindings, not ones already live. Default 30."
        ),
        scope=["global", "project", "session"],
    ),
    "session.task_begin_autoscaffold": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets an agent that starts a task in a session that does not "
            "exist yet create that session automatically (SESSION.md, plans/"
            "agents/artifacts folders, membership registration) instead of "
            "being refused. Off (default): a missing session must first be "
            "created by you or granted by a conductor — this keeps agents "
            "from minting their own workspaces. Each auto-created session is "
            "stamped in the audit log ('session_scaffolded'). (#475)"
        ),
        scope=["global", "project", "session"],
    ),
    "security.emit_decision_trace": _setting(
        type="boolean",
        default=False,
        description=(
            "Writes a detailed trace for every tool-permission decision: "
            "which checks ran, what each decided, and why. Off by default — "
            "each trace adds roughly 1-2 KB and busy sessions produce "
            "thousands. Turn it on while investigating why a specific call "
            "was blocked or allowed (the dashboard's 'Why?' button reads "
            "these traces), then turn it back off. (SEC-012)"
        ),
        scope=["global", "project", "session"],
    ),
    "security.tier_enforcement": _setting(
        type="string",
        default="strict",
        description=(
            "How strictly the two classes of safety gates are enforced. T0 "
            "gates protect the AIDOCS guardrails themselves and normally "
            "unlock only from the dashboard; T1 gates unlock when you clearly "
            "ask for something in your prompt. 'strict' (default) keeps that "
            "split. 'user_trust' lets prompt-level permission open T0 gates "
            "too — which largely defeats the reason T0 exists. 'off' makes "
            "all tier gates log-only, blocking nothing. Dashboard-only, since "
            "the policy that controls T0 must itself be T0. [T0] (§6)"
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
        is_t0=True,
    ),
    "security.tool_output_secret_policy": _setting(
        type="string",
        default="redact",
        description=(
            "What happens when a password, API key or other credential is "
            "detected in tool output before the agent sees it. 'redact' "
            "(default) replaces the credential with a [REDACTED:category] "
            "marker. 'report_only' logs the finding but lets the raw text "
            "through. 'allow_raw' skips scanning entirely. Anything below "
            "'redact' means real credentials can land in the conversation and "
            "whatever stores it. [T0] (replaces the legacy "
            "security.output_guard booleans, removed 2026-04-28)"
        ),
        allowed_values=["redact", "report_only", "allow_raw"],
        value_descriptions={
            "redact": "Scan tool results, replace any detected credentials with [REDACTED:category] markers before the agent sees them. Default — Castle-grade.",
            "report_only": "Scan tool results, audit findings to execution_events, but DO NOT modify the text. Agent receives raw output. For dev environments where you need to verify detection before flipping to redact.",
            "allow_raw": "Skip scanning entirely. Tool output reaches the agent unmodified. Use only for trusted local dev or controlled environments where output guard adds no value.",
        },
        security_sensitive=True,
        scope=["global", "project"],
        is_t0=True,
    ),
    # ── Security / Freeze Policy ──────────────────────────────────────
    "security.agent_security_violation_freeze_threshold": _setting(
        type="integer",
        default=3,
        description=(
            "How many blocked security violations by the same agent — "
            "attempts to read sensitive files, run forbidden shell commands, "
            "reach unknown external paths — are tolerated in a session before "
            "that agent is frozen and needs an operator to clear it. Default "
            "3: warning on the second strike, freeze on the third. 1 = freeze "
            "on the first violation; 0 = no freeze (each violation is "
            "still blocked and audited either way). Strikes reset only when "
            "the freeze is cleared or a fresh session starts, never on a safe "
            "action. [T0] (Security / Freeze Policy)"
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.operator_forbidden_prompt_freeze_threshold": _setting(
        type="integer",
        default=1,
        description=(
            "How many operator prompts judged outright forbidden (malicious "
            "intent) are tolerated before the session is frozen. Prompts are "
            "judged before the agent ever sees them, so the default is 1 — "
            "the first forbidden prompt freezes immediately. Raise it to "
            "allow a warning step first; 0 = no freeze (forbidden prompts "
            "are still blocked and audited). Independent of the agent-side "
            "violation threshold. [T0] (Security / Freeze Policy)"
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
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
            "Old combined name for the agent/tool-side freeze threshold — "
            "superseded; use "
            "security.agent_security_violation_freeze_threshold instead. If "
            "the new key is not set, this value still drives the "
            "agent/tool-side threshold for one deprecation cycle. It never "
            "applied to forbidden operator prompts: those freeze immediately "
            "as hostile_operator_prompt under "
            "security.operator_forbidden_prompt_freeze_threshold. 0 = block "
            "and audit only; 0 = no freeze. [T0]"
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.egress_allowlist": _setting(
        type="string_list",
        default=[],
        description=(
            "Hosts and URLs that outbound commands (scp, curl, and similar) "
            "are allowed to send data to. Empty (default): no destination is "
            "pre-approved, and anything else is flagged as a blocked egress "
            "destination. Add entries only for services you deliberately let "
            "the agent upload to — every entry is a place project data can "
            "leave this machine. Dashboard-only: agents cannot widen it. (§6)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "security.allow_palace_maintenance": _setting(
        type="boolean",
        default=False,
        description=(
            "Permission switch for destructive memory-maintenance operations "
            "(the ai_palace_maintenance tool, e.g. backfilling legacy memory "
            "drawers). Maintenance requires BOTH a signed-in dashboard admin "
            "AND this flag — while it is off (the default) even an "
            "authenticated admin is refused, and the flag alone never "
            "authenticates anyone. Turn it on for the maintenance window, "
            "then off again. Dashboard-only. (§6; blocked_by="
            "maintenance_policy_disabled)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "security.require_output_redaction_for_run": _setting(
        type="boolean",
        default=True,
        description=(
            "Safety net for shell-command output: decides what happens if a "
            "credential is detected in a run's output but redaction cannot "
            "actually be applied (guard error, missing text field). On "
            "(default): the output is withheld and replaced with a marker — a "
            "lost log beats a leaked secret. Off: the raw output goes through "
            "and the bypass is audited. Only matters when "
            "security.tool_output_secret_policy is 'redact'. [T0]"
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    # security.bash_allowed and security.bash_denied REMOVED 2026-04-25.
    # Superseded by the canonical [bash] declarative table. Operators
    # with legacy entries in sqlite get them ignored — schema no longer
    # validates them.
    "bash": _setting(
        type="string",
        default="",
        description=(
            "Root of the shell command policy: which command families the "
            "agent's shell may run. Holds the operator-curated allow and deny "
            "tables ([bash.allow] / [bash.deny]) plus a per-family verdict "
            "map ([bash.commands]: allow / deny / ask — 'ask' prompts you "
            "once per call, never stickily). The built-in denylist for "
            "dangerous commands always applies above anything set here, and "
            "native host shells obey this same policy. Edited only on the "
            "dashboard's Shell Policy page — if agents could edit their own "
            "shell policy it would protect nothing. Dashboard-only. [T0] "
            "(§6, #18)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),

    "security.allow_allowlist_edit": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets agents edit the shell allow/deny lists themselves "
            "(bash.allow.* / bash.deny.* and similar) via config_set. Off by "
            "default, and DANGEROUS when on: an agent that can edit the "
            "allowlist can grant itself new shell commands, which is why this "
            "needs BOTH security.allow_config_edit AND this dedicated toggle "
            "— 'can edit config' must not silently mean 'can widen shell "
            "execution'. Enable only for a supervised policy-tuning session, "
            "then switch it back off. Dashboard-only, never agent-editable. "
            "[T0] (#100)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.sticky_epoch_bound": _setting(
        type="boolean",
        default=True,
        description=(
            "Controls how long a 'sticky' permission you grant in "
            "conversation stays in force. On (default): grants expire when "
            "the conversation context is compacted, so you re-confirm in the "
            "fresh context — this closes the loophole where an old grant "
            "silently outlives the conversation that justified it. Off: "
            "grants persist until explicitly revoked — convenient, but "
            "weaker. The audit and display of grants is unaffected either "
            "way; only enforcement is. Dashboard-only. [T0] (#99)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.judge_override": _setting(
        type="string_list",
        default=[],
        description=(
            "Older flat list of safety-judge rule IDs disabled for this "
            "project (e.g. GIT_FORCE_PUSH on a repo where force-push is "
            "routine). It still works — entries are sorted into the "
            "per-family lists (security.judge_override.<family>) "
            "automatically on read — but prefer setting those directly. "
            "Every entry here is a safety check that no longer fires. "
            "Dashboard-only: bypassing judge rules is a posture decision. "
            "[T0] (§6, #19)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project", "session"],
        is_t0=True,
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
                f"Safety-judge rule IDs switched off for the '{_family}' "
                f"command family. Empty (default) = every rule in the family "
                f"is active; the literal '@all' disables the whole family. "
                f"Each entry is a safety check that no longer fires for this "
                f"project, so add rules only when they demonstrably block "
                f"legitimate work. Rules covering credential theft, "
                f"download-then-execute and catastrophic destruction are "
                f"locked and stay active no matter what is listed. Changes "
                f"are audited (judge_rule_disabled/enabled). Dashboard-only. "
                f"[T0] (#19)"
            ),
            security_sensitive=True,
            dashboard_only=True,
            scope=["global", "project", "session"],
            is_t0=True,
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
            "When a malicious operator prompt freezes a session, also freeze "
            "every other session belonging to that same signed-in user, and "
            "let one operator-level clear lift them all. On by default — a "
            "hostile operator should not be able to just continue in a "
            "second window. Off: the freeze stays limited to the one session "
            "(useful for red-team isolation). If no user can be resolved, "
            "the freeze is per-session regardless. Dashboard-only. [T0] "
            "(#404: login is always required)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global"],
        is_t0=True,
    ),
    "agents.allow_subagents": _setting(
        type="boolean",
        default=False,
        description=(
            "Whether the agent may spawn subagents (the host's Agent/Task "
            "tool). Off (default): delegation is blocked and the agent works "
            "through AIDOCS tools directly. On: the agent can fan work out "
            "to subprocesses — faster for large research tasks, but it "
            "multiplies the processes acting on your machine. Asking for it "
            "plainly in your prompt (e.g. 'delegate research') also lifts "
            "this for the turn under strict tier enforcement. [T1]"
        ),
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
            "When on, every new prompt to a conductor (non-worker) process "
            "automatically clears any leftover lane restriction, so a "
            "long-running conductor cannot stay wrongly locked into a "
            "finished lane overnight — no need to type 'exit lane' each "
            "turn. Worker processes are never auto-cleared: that would let "
            "them escape their own sandbox. Off by default, because a "
            "conductor stuck in a lane is usually a real signal worth "
            "seeing. [T0]"
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    # conductor.forced_work_mode REMOVED 2026-04-30 (autowake removal).
    # The setting and its enforcement were retired together — the
    # underlying mechanism could not actually achieve its goal (agents
    # could decline ScheduleWakeup and stall the session).
    "security.prompt_secret_policy": _setting(
        type="string",
        default="block",
        description=(
            "What happens when your own prompt contains a recognizable "
            "credential (AWS/GitHub/Stripe/OpenAI/Anthropic/Google/Slack "
            "keys, JWTs, PEM blocks, URLs with passwords). 'block' "
            "(default): the turn is refused, you see a SECRET DETECTED "
            "notice, and the agent never receives the text — the strongest "
            "protection available, since most hosts cannot rewrite a prompt "
            "in place, only block it. 'allow': the prompt goes through "
            "unchanged; use only when you are deliberately handing the agent "
            "a credential and accept that it lands in context and logs. "
            "[T0] (replaces security.block_user_credentials, removed "
            "2026-04-28)"
        ),
        allowed_values=["block", "allow"],
        value_descriptions={
            "block": "Refuse the turn. Operator sees 🛑 SECRET DETECTED, agent never receives the prompt. Default — strongest cross-host enforcement available.",
            "allow": "Permit the prompt through unchanged. Use only when the operator deliberately pastes credentials they want the agent to consume (and accepts the audit/leak risk).",
        },
        security_sensitive=True,
        scope=["global", "project"],
        is_t0=True,
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
    # ── #442 sync hub (VPS = authoritative) ──────────────────────────────
    "sync.vps_hub_enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "When True, each sync cycle submits the git OUTBOX to the "
            "authoritative VPS hub (POST /sync/events) for re-authorization and "
            "pulls server-ordered events. The SERVER authorizes the actor and "
            "issues the receipts, so the untouched receipted-only fold applies "
            "what it accepts — this is what lets local and WebMCP converge under "
            "ONE codenexus account. DEFAULT OFF and FAIL-OPEN: with the flag "
            "off, no token, or no reachable hub this is a no-op and local "
            "behaviour is byte-identical (hard floor: local-first/offline — the "
            "VPS is hub + backup, never a hard dependency). Requires a token "
            "carrying the 'sync' scope."
        ),
        scope=["global", "project"],
    ),
    "runtime.update_policy": _setting(
        type="string",
        default="notify",
        description=(
            "What this install does when the authority publishes a newer build "
            "(#868/#903). 'auto' pulls, verifies, installs and restarts itself; "
            "'notify' reports the new build and waits; 'pinned' ignores it. "
            "Defaults to 'notify' because what gets swapped is the package that "
            "ENFORCES - a surprise upgrade is the operator's to opt into, never "
            "AIDOCS's to assume. An unknown value fails CLOSED and names itself "
            "rather than falling back to the most permissive reading. "
            "AIDOCS_UPDATE_POLICY overrides this for a single process."
        ),
        scope=["global", "project"],
    ),
    "sync.vps_hub_url": _setting(
        type="string",
        default="https://mcp.codenexus.cloud",
        description="Base URL of the authoritative sync hub (#442).",
        scope=["global", "project"],
    ),
    "sync.vps_hub_token": _setting(
        type="string",
        default="",
        description=(
            "Bearer token for the sync hub; must carry the 'sync' scope (its own "
            "least-privilege scope — a catalog/read token cannot write the "
            "authoritative event log). AIDOCS_OPERATOR_TOKEN takes precedence."
        ),
        scope=["global", "project"],
    ),
    "sync.hub_org_id": _setting(
        type="string",
        default="",
        description=(
            "OVERRIDE ONLY — leave unset. The org is DERIVED (2026-07-21 operator "
            "ruling): from the project's registration, which already carries its "
            "org, and failing that from the signed-in operator's login (the email "
            "is the join key into codenexus, where org membership is already "
            "authoritative). Set this only to point a host at a different org "
            "than the one it resolves to."
        ),
        scope=["global", "project"],
    ),
    "sync.vps_hub_project_id": _setting(
        type="string",
        default="",
        description=(
            "OVERRIDE ONLY — leave unset. The project id (ogp_…) comes from the "
            "project's own REGISTRATION; connecting a project is what establishes "
            "it. The server still refuses any event whose project_id does not "
            "match the principal's entitled project (tenant_mismatch), so an "
            "override cannot buy access — it can only fail closed."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_autosync": _setting(
        type="boolean",
        default=True,
        description=(
            "When True (default), the BacklogSyncSitter continuously replicates "
            "the git-backed backlog event log between this host and the gate — "
            "AUTOMATICALLY, not on command/sync/deploy: debounced push on each "
            "backlog mutation + poll-pull that re-derives the convergent display "
            "id (so #N matches across stores) and audits any same-field "
            "last-writer-wins lost update (backlog_lww_superseded, recoverable). "
            "Fail-open: a sync cycle never blocks or fails a backlog write. "
            "Disable on serverless/ephemeral hosts without a long-lived process."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_autosync_poll_seconds": _setting(
        type="integer",
        default=30,
        description=(
            "BacklogSyncSitter poll-pull interval in seconds (min 5). Lower = "
            "fresher cross-machine convergence but more git fetches; higher = "
            "cheaper but a wider window before remote edits land."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_autosync_push_debounce_ms": _setting(
        type="integer",
        default=1500,
        description=(
            "BacklogSyncSitter push debounce in milliseconds (min 200). A burst "
            "of backlog mutations batches into a single commit+push after this "
            "quiet period."
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
            "Would let ai_run execute commands via PowerShell instead of "
            "Bash — but the PowerShell backend does not exist yet, so today "
            "turning this on changes nothing except an audit entry showing "
            "it was requested and rejected. Intended only for machines where "
            "Bash cannot be installed, once the backend ships. The "
            "deliberately ugly name is the warning sign: treat it as "
            "radioactive. Dashboard-only; never grantable by prompt, sticky "
            "grant, or lane delegation. [T0] (Batch A/C, shell provider "
            "lock)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global"],
        is_t0=True,
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
            "What happens when a new memory capture overlaps an existing "
            "memory. Off (default): the write is refused and the agent must "
            "show you the conflicting entries and ask — you stay the arbiter "
            "of what memory says. On: overlapping captures are merged "
            "automatically (as an upgrade) without asking — less friction, "
            "but stored memory can be reshaped without you seeing it. The "
            "dashboard toggle wraps this setting. (Empire doctrine "
            "2026-05-17)"
        ),
        scope=["global", "project"],
    ),
    "edit.str_replace_max_old_chars": _setting(
        type="integer",
        default=1000,
        description=(
            "Maximum size, in characters, of the text an "
            "ai_replace(mode='string') call may match against (old_string). "
            "Bigger edits should use anchor or symbol mode, which do not "
            "resend the old text — this cap is what steers agents there and "
            "keeps conversation context lean. Only the old text is capped; "
            "the replacement is unlimited. 0 = unlimited (no cap). Default 1000 (tuned "
            "2026-05-01/2026-05-10, doctrine #113)."
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
            "Project-specific additions to the built-in list of dangerous "
            "command patterns the safety judge watches for (JSON array; each "
            "entry becomes its own CFG_* rule with the severity you give "
            "it). Use it to teach the judge about hazards specific to your "
            "stack; the ~30 built-in patterns always stay active. Editing "
            "this shifts the project's safety posture, so it is "
            "dashboard-only. [T0] (§4.17)"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project"],
        is_t0=True,
    ),
    "policies.tools": _setting(
        type="string",
        default="[]",
        description=(
            "Operator-written allow/deny rules for non-shell tools (JSON "
            "array; each entry matches a tool name, optionally limited to a "
            "scope or path prefix). Use it to switch specific tools off "
            "entirely or confine them to parts of the tree. Dashboard-only "
            "— if agents could edit tool policy it would protect nothing. "
            "[T0]"
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project"],
        is_t0=True,
    ),
    "security.delegate_research_allowed": _setting(
        type="boolean",
        default=False,
        description=(
            "Whether conductors may spawn research subagents. Off (default): "
            "research-pattern agent spawns are refused. Saying 'allow "
            "delegate research' in your prompt lifts it for the turn. Turn "
            "it on permanently if you routinely run research fan-outs and "
            "accept the extra subprocesses they create. [T1]"
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
            "File extensions skipped by secret scanning and tool-output "
            "redaction. The default set covers non-text payloads (images, "
            "PDFs, notebooks) and the .output/.log convention. Adding an "
            "extension means credentials in such files pass through "
            "unscanned — add with care; removing entries is always safe. "
            "(Invariant #40)"
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.exempt_paths": _setting(
        type="string_list",
        default=[],
        description=(
            "Specific paths skipped by secret scanning (absolute or "
            "project-relative). Every entry is a blind spot in credential "
            "detection, so keep the list as short as possible; all other "
            "paths remain scanned. (Invariant #40)"
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
            "Filename patterns that reading tools must never show the agent. "
            "Defaults cover common credential stores (tokens.json, "
            "secrets.json, appsettings.*.json, ...). Add patterns for your "
            "project's own credential files (e.g. "
            "'config/*.production.yaml'); matching files are refused before "
            "any content is served."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.explicit_confirm_on_grant": _setting(
        type="boolean",
        default=False,
        description=(
            "When on, granting a tier-1 tool permission in your prompt "
            "requires the word 'explicitly' in the phrase before it is "
            "auto-allowed — a guard against casual phrasing being read as "
            "consent. Off (default): a clear grant phrase near the request "
            "is enough. Raw tools and tier-2 grants always require "
            "confirmation regardless of this flag. [T1]"
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
