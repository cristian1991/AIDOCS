"""Settings catalog — defines all AIDOCS configuration settings.

This stays intentionally flat: each entry is keyed by the dotted TOML path and
describes the setting without introducing a second nested config model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, TypedDict

# #749 (design decision 2026-08-27): the [T0] tag derives from is_t0, since two
# sources for one fact is wrong. Matches the tag and the
# whitespace before it, so stripping leaves the sentence intact. Deliberately
# tolerant of variants ([T0], [T0 guardrail], ...) — the FIRST pass's guard
# matched the open bracket `[T0`, which means variants exist in the wild, and a
# variant that survived the strip would be exactly the second copy this closes.
_T0_TAG_RE = re.compile(r"\s*\[T0[^\]]*\]")
_T0_MARKER = "[T0]"

SettingType = Literal["integer", "boolean", "string", "string_list"]
# Editor hint for a `string` setting whose stored text is structured or numeric.
# Presentation only: the wire type stays `string`, so storage and
# coerce_setting_value are unchanged. The dashboard reads it to render a
# validated JSON / number control instead of a free-text box. "" = plain text.
ValueFormat = Literal["", "json_object", "json_array", "number"]
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
    # Editor hints (dashboard typed settings). value_format: see ValueFormat.
    # minimum/maximum: an `integer` setting's bound, declared ONLY where the
    # reader enforces it (e.g. a `max(2, poll_seconds)` clamp) so the number
    # input states the real floor instead of accepting a value the runtime
    # silently rewrites. None = no known bound. Not a write guard.
    value_format: ValueFormat
    minimum: int | None
    maximum: int | None


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
    value_format: ValueFormat = "",
    minimum: int | None = None,
    maximum: int | None = None,
) -> SettingMetadata:
    if value_format and type != "string":
        raise ValueError("value_format is a hint for string settings only")
    if (minimum is not None or maximum is not None) and type != "integer":
        raise ValueError("minimum/maximum are hints for integer settings only")
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
    # DESIGN DECISION (2026-08-27): the [T0] tag derives from is_t0; two
    # sources for one fact is wrong.
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
        "value_format": value_format,
        "minimum": minimum,
        "maximum": maximum,
    }


SETTINGS_CATALOG: dict[str, SettingMetadata] = {
    # ── dashboard.* — NAMESPACE REMOVED 2026-08-27 (design decision) ──
    # It held exactly one key, `dashboard.auto_bind_local_sessions`, and the
    # decision on #559 was to remove it: no auto-bind.
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
    # operator decision — no agent may supply one.

    # ── delete.* (ai_delete trash + quota knobs, sealed 2026-05-27) ──
    "delete.max_trash_bytes": _setting(
        type="integer",
        default=25 * 1024 * 1024,
        description=(
            "Largest file, in bytes, that a delete moves to the .TRASH folder. "
            "A bigger file is not deleted; the delete is refused as too large."
        ),
        scope=["global", "project"],
    ),
    "delete.max_trash_items": _setting(
        type="integer",
        default=500,
        description=(
            "How many files the .TRASH folder may hold. When it is full, "
            "deletes are refused until it is emptied. 0 = no limit."
        ),
        scope=["global", "project"],
    ),
    "delete.max_trash_total_bytes": _setting(
        type="integer",
        default=500 * 1024 * 1024,
        description=(
            "Total size, in bytes, the .TRASH folder may reach. When it is "
            "full, deletes are refused until it is emptied. 0 = no limit."
        ),
        scope=["global", "project"],
    ),
    "delete.idempotency_window_minutes": _setting(
        type="integer",
        default=10,
        description=(
            "Deleting the same file again within this many minutes reports "
            "the earlier delete (already deleted) instead of failing."
        ),
        scope=["global", "project"],
    ),
    # ── mcp.* (host MCP wiring, #249 daemon mode) ──
    "mcp.local_daemon_url": _setting(
        type="string",
        default="",
        description=(
            "Address of one shared local AIDOCS server, e.g. "
            "http://127.0.0.1:8748/mcp. When set, projects connect to it "
            "instead of each starting their own AIDOCS process; it reconnects "
            "and restarts on its own. Empty = each project starts its own."
        ),
        scope=["global"],
    ),
    "mcp.multitenant_strict": _setting(
        type="boolean",
        default=False,
        description=(
            "For one shared AIDOCS server used by several projects. On: a "
            "request that doesn't say which project it is for is refused, "
            "so one project's work can't land in another. Turn on only after "
            "every project's connection file names its project, or older "
            "setups stop working. Off: the server guesses the project as "
            "before. Has no effect on single-project setups."
        ),
        scope=["global"],
    ),
    # ── memory.capture_analyzer.* (memory-loop seal, 2026-07-09) ──
    "memory.capture_analyzer_timeout_ms": _setting(
        type="integer",
        default=1500,
        description=(
            "Time limit, in milliseconds, for analysing a newly saved memory "
            "(finding its key terms and the code it refers to). If time runs "
            "out, the memory is still saved, just without that extra detail. "
            "0 = skip the analysis."
        ),
        scope=["global", "project", "session"],
    ),
    # ── memory.semantic_recall.* (palace hybrid-recall lane, catalogued 2026-07-01) ──
    # ── memory.stop_capture (#316 Stop-time memory capture, 2026-07-13) ──
    "memory.stop_capture": _setting(
        type="boolean",
        default=False,
        description=(
            "After each assistant reply, save any new rules, decisions or "
            "preferences it states to memory automatically, marked as "
            "auto-saved. Off by default, because it stores assistant text "
            "without being asked; expect some noise if you turn it on. "
            "Replies from helper agents are never saved."
        ),
        scope=["global", "project"],
    ),
    "memory.semantic_recall_on_ups": _setting(
        type="boolean",
        default=True,
        description=(
            "On each prompt, also look up memories that are similar in "
            "meaning, not just ones sharing keywords. These come last and are "
            "time-limited. Off: only keyword matches are shown."
        ),
        scope=["global", "project"],
    ),
    "memory.semantic_recall_limit": _setting(
        type="integer",
        default=2,
        description=(
            "Most similar-meaning memories shown per prompt. Kept small so "
            "they don't crowd out direct keyword matches."
        ),
        scope=["global", "project"],
    ),
    "memory.semantic_recall_max_distance": _setting(
        type="string",
        default="0.85",
        value_format="number",
        description=(
            "How close in meaning a memory must be to be shown, as a number "
            "(e.g. 0.85). Lower = stricter, fewer but better matches."
        ),
        scope=["global", "project"],
    ),
    "index.extra_skip_dirs": _setting(
        type="string_list",
        default=[],
        description="Extra folders the code index ignores, on top of the built-in list.",
        scope=["global", "project", "session"],
    ),
    "index.extra_module_hints": _setting(
        type="string_list",
        default=[],
        description="Extra folder names that mark the start of a module, so the index groups code under them.",
        scope=["global", "project", "session"],
    ),
    "index.max_json_size": _setting(
        type="integer",
        default=100_000,
        description="JSON files larger than this many bytes are not indexed. 0 = unlimited.",
        scope=["global", "project", "session"],
    ),
    "index.enabled_languages": _setting(
        type="string",
        default="all",
        description="Which programming languages the code index reads. 'all' = every supported language.",
        scope=["global", "project", "session"],
    ),
    "index.include_tests": _setting(
        type="boolean",
        default=False,
        description="Also index test folders (tests/, test/, __tests__/). Off: tests are left out of code search.",
        scope=["global", "project"],
    ),
    "index.lsp_materialize": _setting(
        type="boolean",
        default=False,
        description="After each index update, ask the language server which symbols the changed files refer to, for more precise cross-references. Off by default. If the language server fails, the index update still succeeds.",
        scope=["global", "project", "session"],
    ),
    "languages.enabled": _setting(
        type="string",
        default="all",
        description="Human languages AIDOCS understands when reading your prompts, comma-separated. 'all' = every bundled language.",
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
            "The Python used to run this project's tests. Set it when the "
            "project has no .venv folder (a fresh git clone never does). "
            "Empty = look for .venv in the test folder; if none is found, "
            "the test run is refused and lists where it looked."
        ),
        scope=["global", "project"],
    ),
    "tools.tool_call_timeout": _setting(
        type="integer",
        default=10,
        description="How long, in seconds, an AIDOCS tool may run before it is stopped. A tool call may ask for longer, up to tools.max_timeout. 0 = no limit.",
        scope=["global", "project", "session"],
    ),
    "tools.sync_write_timeout": _setting(
        type="integer",
        default=60,
        description="Time limit, in seconds, for tools that write session files (starting a task and similar). Never more than tools.max_timeout. 0 = no limit. Index rebuilds use tools.index_sync_timeout.",
        scope=["global", "project", "session"],
    ),
    "tools.index_sync_timeout": _setting(
        type="integer",
        default=120,
        description="Time limit, in seconds, for rebuilding the code, schema or meaning indexes. Large projects need longer than ordinary tools. Never more than tools.max_timeout. 0 = no limit.",
        scope=["global", "project", "session"],
    ),
    "tools.memory_surfacing_timeout_ms": _setting(
        type="integer",
        default=500,
        description="When a file is read, AIDOCS also shows memories about that file. This is the time limit for finding them, in milliseconds; if it runs out they are skipped and the read still works. Raise it if memories are missing, lower it for faster reads. 0 = no limit.",
        scope=["global", "project", "session"],
    ),
    "tools.git_functions_timeout": _setting(
        type="integer",
        default=30,
        description="Time limit, in seconds, for git commands run by AIDOCS. 0 = no limit.",
        scope=["global", "project", "session"],
    ),
    "tools.max_timeout": _setting(
        type="integer",
        default=120,
        description="The longest any single tool call may be allowed to run, in seconds, even when it asks for more. 0 = no ceiling.",
        scope=["global", "project", "session"],
    ),
    "tools.bash_long_runner_cap_seconds": _setting(
        type="integer",
        default=300,
        description=(
            "Slow shell commands (test suites, package installs, cargo, "
            "docker) must run in the background so they can't freeze the "
            "conversation. This is how many seconds one may take. 0 = only "
            "warn; a slow command can then stall the session."
        ),
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "tools.shell_enforcement_live": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets the new shell policy decide which shell commands the "
            "agent's own Bash/PowerShell/cmd tools may run. Off: the existing "
            "checks decide. Allowed commands still run through AIDOCS's "
            "managed runner, and freezes still apply. Turn on only when the "
            "Governed Bash setup asks you to. Only you can change it, from "
            "the dashboard."
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
            "Checks each shell command before it runs for hidden code it "
            "would start: package install scripts, build and git hooks, "
            "downloaded code. Off: risky commands are only logged. On: they "
            "are blocked or wait for your approval. Check the log first; "
            "turning this on too early can block normal builds. Only you can "
            "change it, from the dashboard."
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
            "Old switch that no longer changes anything; it is only reported "
            "in diagnostics. Whether the agent's shell may run a command is "
            "decided by tools.shell_enforcement_live and the shell policy. "
            "Leave it as it is."
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
            "Extra read-only commands the agent's shell may run, on top of "
            "the built-in list, separated by ';' or ',' (e.g. jq, rg, fd). "
            "Commands that can write files, use the network or run code "
            "(rm, curl, ssh, python, npm, sed...) are ignored if listed. "
            "Only you can change it, from the dashboard."
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
            "Folders a trusted shell program must live in (separated by ';' "
            "on Windows, ':' elsewhere, or ','). A shell found anywhere else "
            "is not trusted, so a look-alike program can't stand in for it. "
            "Empty = no shell can be trusted and the agent's own shell stays "
            "off. Normally filled in by the Governed Bash setup."
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
            "Lets the agent's own shell tools (Bash/PowerShell/cmd) run "
            "commands directly instead of through AIDOCS's managed runner. "
            "Even when on, this only applies to shells AIDOCS can watch and "
            "stop; the rest still use the managed runner. Leave off unless "
            "the Governed Bash setup asks you to turn it on."
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
            "Full path to the shell program AIDOCS treats as your shell (e.g. "
            "C:\\Program Files\\Git\\usr\\bin\\bash.exe), so a different "
            "program on PATH can't be picked up instead. Normally set by the "
            "Governed Bash setup. Empty = find it on PATH."
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
            "Optional SHA-256 fingerprint of the shell program above. When "
            "set, the shell is only accepted if the file matches it exactly, "
            "so a replaced file is caught. Update it whenever you update the "
            "shell. Empty = no fingerprint check (path and folder are still "
            "checked)."
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
            "On: the shell program must be digitally signed (e.g. Windows "
            "Authenticode), or it is rejected; so is any shell on a system "
            "that can't check signatures. Off by default because many normal "
            "shells, such as Git for Windows bash, are unsigned."
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
            "Not used yet; changing it has no effect. Planned: after this "
            "many seconds, a shell command moves to the background instead "
            "of holding up the conversation. 0 = off."
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
            "Test-drive the new shell policy without letting it decide: it "
            "checks every shell command too, and each time it disagrees with "
            "the current checks, that is written to the audit log. Nothing "
            "else changes. Use it to build confidence before switching to the "
            "new policy."
        ),
        scope=["global", "project", "session"],
    ),
    "agent.inject_message_directives": _setting(
        type="boolean",
        default=True,
        description="Add short instructions to each of your prompts telling the agent which AIDOCS tools to use (on hosts that support it).",
        scope=["global", "project", "session"],
    ),
    "agent.inject_rules_on_bootstrap": _setting(
        type="boolean",
        default=True,
        description="Give the agent the project's workflow and coding rules when it starts.",
        scope=["global", "project", "session"],
    ),
    "agent.directive_style": _setting(
        type="string",
        default="short",
        description="How much detail the agent gets in AIDOCS's step-by-step instructions.",
        allowed_values=["short", "detailed"],
        value_descriptions={
            "short": "A few short steps.",
            "detailed": "Every step, with examples.",
        },
        scope=["global", "project", "session"],
    ),
    "global.aidocs_core_version": _setting(
        type="string",
        default="2.3.0b1",
        description="The installed AIDOCS version. Set by the installer; don't change it.",
        scope="global",
    ),
    "oauth.chatgpt_redirect_uris": _setting(
        type="string_list",
        default=[],
        description=(
            "The exact sign-in return address of the one ChatGPT connector "
            "allowed to sign in to this server (e.g. https://chatgpt.com/"
            "connector/oauth/<token>). Any other connector is refused. Empty "
            "= no ChatGPT sign-in. Always use the full address, never just "
            "the domain: a domain-wide entry would let sign-in codes go to "
            "any page on chatgpt.com."
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
            "On: you see tool results formatted (headings, diffs, a footer) "
            "while the agent gets a short version. Off: you and the agent "
            "both see the raw result."
        ),
        scope=["global", "project"],
    ),
    "tool_output.show_tool_name": _setting(
        type="boolean",
        default=False,
        description="Show the tool's name in the debug tag after each tool result, e.g. [ai_find ...].",
        scope=["global", "project"],
    ),
    "tool_output.show_duration": _setting(
        type="boolean",
        default=False,
        description="Show how long each tool call took in the debug tag, e.g. [... 42ms].",
        scope=["global", "project"],
    ),
    "tool_output.show_tokens": _setting(
        type="boolean",
        default=False,
        description="Show a rough token count (size ÷ 4) in the debug tag, e.g. [... in=120 out=840].",
        scope=["global", "project"],
    ),
    "security.allow_config_edit": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets agents change ordinary AIDOCS settings for you. Off: agents "
            "can read settings but not change them. On: every change an "
            "agent makes is checked and logged. Security settings stay "
            "locked either way, and editing config files directly stays "
            "blocked."
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
            "The language you write prompts in, so AIDOCS reads them "
            "correctly. 'auto' detects it per prompt, but often mistakes "
            "short English prompts for Spanish, Portuguese or Italian, so "
            "pick your language if you mostly use one."
        ),
        allowed_values=["en", "it", "de", "es", "pt", "auto"],
        value_descriptions={
            "en": "English.",
            "it": "Italian.",
            "de": "German.",
            "es": "Spanish.",
            "pt": "Portuguese.",
            "auto": "Detect the language of each prompt.",
        },
        scope=["global", "project"],
    ),
    "nlp.update_gate": _setting(
        type="string",
        default="advise",
        description=(
            "When a prompt changes plans, priorities, tasks or decisions, "
            "remind the agent to write the change down (backlog, plan, todo "
            "or memory) before it is forgotten in a long conversation."
        ),
        allowed_values=["off", "advise", "block"],
        value_descriptions={
            "off": "No reminders; changes are kept only if the agent records them.",
            "advise": "Remind the agent and log it (recommended).",
            "block": "Not built yet; works the same as 'advise' for now.",
        },
        scope=["global", "project", "session"],
    ),
    "run.max_timeout_seconds": _setting(
        type="integer",
        default=600,
        description=(
            "Longest a command run through AIDOCS may take, in seconds, even "
            "if the agent asks for more. Raise it for slow tests or builds. "
            "Never more than 3600 (one hour), whatever is set here."
        ),
        scope=["global", "project"],
    ),
    "run.max_live_processes_per_machine": _setting(
        type="integer",
        default=4,
        description=(
            "Most AIDOCS agents (conductors and their workers) that may run "
            "at once on this computer, to protect its memory and CPU. Each "
            "computer has its own limit. Starting one more past the limit is "
            "refused."
        ),
        scope=["global"],
    ),
    "workflow.tdd_mode": _setting(
        type="boolean",
        default=False,
        description=(
            "On: an agent can only finish a task after running the tests "
            "for the files it changed. Off: no tests are required, so "
            "projects without tests still work. Turn on for projects that "
            "have a test suite."
        ),
        scope=["global", "project"],
    ),
    "security.enforce": _setting(
        type="boolean",
        default=True,
        description=(
            "Main safety switch: the allowed-commands list, blocking of the "
            "agent's raw tools, and blocking of destructive commands. Off "
            "removes all of that for every agent here. Don't turn it off on "
            "a computer that matters."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.allow_raw_edits": _setting(
        type="boolean",
        default=False,
        description=(
            "Lets the agent edit files with its own tools instead of AIDOCS's. "
            "Off: edits go through AIDOCS, so the code index and edit history "
            "stay up to date. On: they can fall out of date and other agents "
            "may work from old file contents. Only for debugging AIDOCS's "
            "editing."
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
            "Lets the agent open files that hold secrets (.env, .pem, SSH and "
            "cloud keys). Off: those reads are refused. On: the agent can "
            "read them, so the secrets can end up in the conversation and in "
            "any logs of it; each read is still recorded. Turn on only "
            "briefly."
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
            "Lets agents read memories that were replaced or retired. Off: "
            "they stay hidden even when asked for, and the attempt is logged. "
            "On: useful for finding out why memory changed, but those entries "
            "hold the outdated advice they were retired for."
        ),
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "security.read_grounding_evidence_unavailable": _setting(
        type="string",
        default="closed",
        allowed_values=["closed", "degraded_allow", "open"],
        description=(
            "An agent must read a file before editing it. This decides what "
            "happens when AIDOCS can't check whether it did (usually the "
            "database is briefly busy, so a retry works)."
        ),
        value_descriptions={
            "closed": "Refuse the edit and say why (default).",
            "degraded_allow": "Allow the edit, marked as unchecked.",
            "open": "Treat 'couldn't check' as 'checked'. Not advised.",
        },
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "security.palace_axis_unavailable": _setting(
        type="string",
        default="closed",
        allowed_values=["closed", "degraded_allow", "open"],
        description=(
            "Memories can be pinned to code to block edits that would break "
            "them. This decides what happens when that memory store is set "
            "up but not answering. Projects without one are not affected."
        ),
        value_descriptions={
            "closed": "Refuse the edit and say why (default).",
            "degraded_allow": "Allow the edit, marked as unchecked.",
            "open": "Treat a broken store as no store. Not advised.",
        },
        security_sensitive=True,
        dashboard_only=True,
        is_t0=True,
        scope=["global", "project", "session"],
    ),
    "security.memory_ack_requires_read_receipt": _setting(
        type="boolean",
        default=True,
        description=(
            "When an edit is held back by a memory pinned to that code, the "
            "agent must confirm it saw the memory. On: it has to have "
            "actually opened it. Off: naming the memory is enough."
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
            "No longer used in AIDOCS sessions: the agent's own shell stays "
            "blocked whatever this says. To run shell commands, use AIDOCS's "
            "runner or turn on Governed Bash."
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
            "Folders outside the project the agent may use (full paths; "
            "subfolders included), such as a scratch folder or a related "
            "repo. Empty = everything outside the project is blocked. Private "
            "folders (.ssh, .aws, .gcloud, .azure, .config, AppData) stay "
            "blocked even if listed."
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
            "Minutes an agent stays linked to its project with no activity. "
            "After that, AIDOCS works out the project from the agent's "
            "folder again. Any tool call restarts the timer. A change only "
            "applies to links made after it."
        ),
        scope=["global", "project", "session"],
    ),
    "session.task_begin_autoscaffold": _setting(
        type="boolean",
        default=False,
        description=(
            "On: an agent starting a task in a session that doesn't exist "
            "yet creates it (logged). Off: the session must first be created "
            "by you or a conductor, so agents can't make their own."
        ),
        scope=["global", "project", "session"],
    ),
    "security.emit_decision_trace": _setting(
        type="boolean",
        default=False,
        description=(
            "Record, for every allowed or blocked tool call, which checks ran "
            "and why. The dashboard's 'Why?' button shows it. Each record is "
            "1-2 KB and busy sessions make thousands, so turn on only while "
            "investigating."
        ),
        scope=["global", "project", "session"],
    ),
    "security.tier_enforcement": _setting(
        type="string",
        default="strict",
        description=(
            "There are two kinds of safety checks. T0 checks protect AIDOCS's "
            "own guardrails and can only be changed here in the dashboard. "
            "T1 checks lift when you clearly ask for the action in a prompt. "
            "[T0]"
        ),
        allowed_values=["strict", "user_trust", "off"],
        value_descriptions={
            "strict": "T0 only from the dashboard, T1 by asking in a prompt (default).",
            "user_trust": "Asking in a prompt also lifts T0 checks. Rarely safe.",
            "off": "Nothing is blocked; checks are only logged.",
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
            "What to do when a password, API key or other secret shows up in "
            "a tool result, before the agent sees it. Anything other than "
            "'redact' lets real secrets into the conversation."
        ),
        allowed_values=["redact", "report_only", "allow_raw"],
        value_descriptions={
            "redact": "Replace the secret with [REDACTED:kind] (default).",
            "report_only": "Log it, but pass the text through unchanged. For testing detection.",
            "allow_raw": "Don't look for secrets at all. Trusted local setups only.",
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
            "How many blocked security violations one agent may make in a "
            "session (reading secret files, forbidden commands, unknown "
            "outside folders) before it is frozen until you clear it. With 3, "
            "it is warned on the second and frozen on the third. 1 = freeze "
            "on the first. 0 = no freeze; violations are still blocked "
            "and logged. The count resets only when you clear the freeze or "
            "a new session starts."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.operator_forbidden_prompt_freeze_threshold": _setting(
        type="integer",
        default=1,
        description=(
            "How many prompts judged clearly malicious are allowed before the "
            "session is frozen. Prompts are checked before the agent sees "
            "them. 1 = freeze on the first. Raise it to get a warning first. "
            "0 = no freeze; such prompts are still blocked and logged."
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
            "Old name for the agent/tool-side freeze threshold; use "
            "security.agent_security_violation_freeze_threshold instead. "
            "Still read when that is unset. It never covered malicious "
            "prompts: those freeze immediately (hostile_operator_prompt). "
            "0 = no freeze."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
        is_t0=True,
    ),
    "security.egress_allowlist": _setting(
        type="string_list",
        default=[],
        description=(
            "Servers and URLs that commands like scp or curl may send data "
            "to. Empty = sending anywhere is blocked. Each entry is a place "
            "your project's data can leave this computer, so add only "
            "services you mean the agent to upload to."
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
            "Allows memory-store maintenance that rewrites or removes data. "
            "It also needs a signed-in admin; off means even an admin is "
            "refused. Turn on for the maintenance, then off again."
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
            "If a secret is found in a command's output but can't be blanked "
            "out: On = hide the whole output (better a lost log than a leaked "
            "secret). Off = show it as-is and log that. Only matters when "
            "security.tool_output_secret_policy is 'redact'."
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
            "Which shell commands the agent may run: allow and deny lists, "
            "and per command allow / deny / ask (ask = you approve each "
            "call). Dangerous commands stay blocked whatever is set here. "
            "Edit it on the Shell Policy page."
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
            "Lets agents edit the shell allow and deny lists. Dangerous: an "
            "agent could allow itself any command. Also needs "
            "security.allow_config_edit. Turn on only while you watch, then "
            "off again."
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
            "How long a lasting permission you give in a conversation holds. "
            "On: it ends when the conversation is summarised to save space, "
            "so you confirm again. Off: it lasts until you revoke it, which "
            "is handier but less safe."
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
            "Old single list of safety rules switched off here (e.g. "
            "GIT_FORCE_PUSH where force-pushing is normal). Still works, but "
            "use the per-kind lists (security.judge_override.<kind>) instead. "
            "Each entry is a safety check that no longer runs."
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
                f"Safety rules switched off for '{_family}' commands. Empty = "
                f"all on; '@all' = all off. Add a rule only when it keeps "
                f"blocking normal work. Rules against stealing secrets, "
                f"running downloaded code and wiping data can't be switched "
                f"off. Changes are logged."
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
            "When a malicious prompt freezes a session, also freeze that "
            "user's other sessions, so they can't just carry on in another "
            "window; one clear lifts them all. Off: only that session is "
            "frozen (useful for security testing)."
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
            "Lets the agent start helper agents. Faster for big research "
            "tasks, but more programs act on your computer at once. Off: "
            "the agent does the work itself, unless you ask for helpers in "
            "a prompt (that turn only)."
        ),
        security_sensitive=True,
        scope=["global", "project", "session"],
    ),
    "xaacp.wait_next_max_seconds": _setting(
        type="integer",
        default=86400,
        description=(
            "Longest, in seconds, an agent may wait for its next message in "
            "one call, when AIDOCS can tell the call was cancelled. "
            "Otherwise the wait is capped at 300."
        ),
        scope=["global", "project"],
    ),
    "xaacp.wait_next_min_seconds": _setting(
        type="integer",
        default=4,
        description=(
            "Shortest, in seconds, an agent's wait for its next message "
            "lasts, even if it asks for 0. A message that is already waiting "
            "is still delivered at once."
        ),
        scope=["global", "project"],
    ),
    "xaacp.wait_next_default_seconds": _setting(
        type="integer",
        default=1200,
        description=(
            "How long, in seconds, an agent waits for its next message when "
            "it doesn't say, on apps that can wait in the background (see "
            "xaacp.wait_next_backgrounding_hosts)."
        ),
        scope=["global", "project"],
    ),
    "xaacp.wait_next_foreground_max_seconds": _setting(
        type="integer",
        default=20,
        description=(
            "How long, in seconds, an agent waits for its next message on "
            "apps that can't wait in the background (ChatGPT gives up after "
            "about 25 s and a message could be lost). Also the most it may "
            "ask for there."
        ),
        scope=["global", "project"],
    ),
    "xaacp.wait_next_host_max_seconds": _setting(
        type="string",
        default="",
        description=(
            "Longest message wait per app, as app=seconds, comma-separated "
            "(e.g. 'webmcp_chatgpt=20'). Claude Code uses its own settings "
            "first."
        ),
        scope=["global", "project"],
    ),
    "xaacp.wait_next_backgrounding_hosts": _setting(
        type="string",
        default="claude_code",
        description=(
            "Apps that can wait for messages in the background, "
            "comma-separated. They may wait up to xaacp.wait_next_max_seconds; "
            "others up to xaacp.wait_next_foreground_max_seconds."
        ),
        scope=["global", "project"],
    ),
    "conductor.backend": _setting(
        type="string",
        default="claude",
        description="Which AI app the conductor starts its worker agents in, unless a task says otherwise.",
        allowed_values=["claude", "codex", "opencode"],
        value_descriptions={
            "claude": "Claude Code (Anthropic).",
            "codex": "Codex (OpenAI).",
            "opencode": "OpenCode (works with several AI providers).",
        },
        scope=["global", "project", "session"],
    ),
    "conductor.max_concurrent_workers": _setting(
        type="integer",
        default=3,
        description=(
            "Most worker agents the conductor may run at once. Each is a full "
            "AI app with its own AIDOCS; more than 3-4 at once usually runs "
            "out of memory or hits the AI provider's rate limits. Starting "
            "one more past the limit is refused."
        ),
        scope=["global", "project", "session"],
    ),
    "conductor.auto_exit_lane": _setting(
        type="boolean",
        default=False,
        description=(
            "On: each new prompt to a conductor lifts any leftover work-area "
            "(lane) limit, so it can't stay stuck in a finished lane. "
            "Workers are never released this way. Off by default: a "
            "conductor stuck in a lane usually means something worth "
            "looking at."
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
            "What happens when a prompt you send contains a key, token, "
            "password or private key (AWS, GitHub, OpenAI, Anthropic and "
            "others)."
        ),
        allowed_values=["block", "allow"],
        value_descriptions={
            "block": "Stop the prompt and show SECRET DETECTED; the agent never sees it (default).",
            "allow": "Send it anyway. Only when you mean to give the agent the secret; it will be in the conversation and logs.",
        },
        security_sensitive=True,
        scope=["global", "project"],
        is_t0=True,
    ),
    "observability.watch_user_drops": _setting(
        type="boolean",
        default=False,
        description=(
            "Watch the project folder and index files you add yourself right "
            "away, so code search finds them without a manual reindex. "
            "Off by default; files are indexed when needed anyway. Useful "
            "if you often copy files in. Needs AIDOCS to keep running (not "
            "for short-lived cloud setups); uses about 8 MB of memory."
        ),
        scope=["global", "project"],
    ),
    "observability.watch_user_drops_debounce_ms": _setting(
        type="integer",
        default=500,
        description=(
            "Milliseconds to wait for more files before reindexing what you "
            "added, so copying many files at once triggers one reindex. "
            "Lower = sooner but more work; higher = later."
        ),
        scope=["global", "project"],
    ),
    "observability.project_index_sitter": _setting(
        type="boolean",
        default=True,
        description=(
            "Keep the code index up to date when files are added, changed or "
            "deleted outside AIDOCS (by you or other programs). Tools are "
            "told when the index may be out of date. Turn off only on "
            "short-lived cloud setups where AIDOCS doesn't keep running."
        ),
        scope=["global", "project"],
    ),
    "observability.index_sitter_poll_seconds": _setting(
        type="integer",
        default=30,
        minimum=2,  # ProjectIndexSitter: max(2, poll_seconds)
        description=(
            "How often, in seconds, the project is rescanned for changes made "
            "outside AIDOCS (at least 2). Lower = notices sooner but uses "
            "more CPU."
        ),
        scope=["global", "project"],
    ),
    # ── #442 sync hub (VPS = authoritative) ──────────────────────────────
    "sync.vps_hub_enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "Sync this computer with your online AIDOCS account, so local and "
            "web sessions see the same data. The server checks and orders "
            "every change. Off, or with no token or connection, everything "
            "keeps working locally. Needs a token with the 'sync' permission."
        ),
        scope=["global", "project"],
    ),
    "runtime.update_policy": _setting(
        type="string",
        default="notify",
        description=(
            "What to do when a newer AIDOCS is released. 'auto' downloads, "
            "checks, installs and restarts on its own; 'notify' tells you and "
            "waits; 'pinned' ignores it. 'notify' is the default because "
            "AIDOCS is what enforces your rules, so upgrading should be your "
            "choice. The AIDOCS_UPDATE_POLICY environment variable overrides "
            "this."
        ),
        scope=["global", "project"],
    ),
    "sync.vps_hub_url": _setting(
        type="string",
        default="https://mcp.codenexus.cloud",
        description="Address of the online AIDOCS server this computer syncs with.",
        scope=["global", "project"],
    ),
    "sync.vps_hub_token": _setting(
        type="string",
        default="",
        description=(
            "Access token for syncing; it must have the 'sync' permission (a "
            "read-only token can't sync). The AIDOCS_OPERATOR_TOKEN "
            "environment variable is used instead when set."
        ),
        scope=["global", "project"],
    ),
    "sync.hub_org_id": _setting(
        type="string",
        default="",
        description=(
            "Leave empty. The organisation is taken from the project's "
            "registration, or else from your login. Set it only to sync with "
            "a different organisation than that."
        ),
        scope=["global", "project"],
    ),
    "sync.vps_hub_project_id": _setting(
        type="string",
        default="",
        description=(
            "Leave empty. The project's online id (ogp_...) comes from "
            "connecting the project. Setting a different one can't give "
            "access to another project; the server refuses it."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_autosync": _setting(
        type="boolean",
        default=True,
        description=(
            "Copy (replicate) the backlog between this computer and the "
            "server through git, automatically: changes are pushed shortly after "
            "they happen and pulled regularly, so item numbers match "
            "everywhere. If two places change the same field, the later one "
            "wins and the overwrite is logged (recoverable). A failed sync "
            "never blocks a backlog change. Turning this off does not stop "
            "sending changes to the server; that is "
            "observability.backlog_outbox_drain."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_outbox_drain": _setting(
        type="boolean",
        default=True,
        description=(
            "Send backlog changes made here to the server (the backlog "
            "authority), even when git sync "
            "(observability.backlog_autosync) is off. Off: changes wait on "
            "this computer and never reach the server; the sync warning "
            "names this setting."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_autosync_poll_seconds": _setting(
        type="integer",
        default=30,
        minimum=5,  # BacklogSyncSitter: max(5, poll_seconds)
        description=(
            "How often, in seconds, backlog changes from other computers are "
            "fetched (at least 5). Lower = sooner, but more git traffic."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_autosync_push_debounce_ms": _setting(
        type="integer",
        default=1500,
        minimum=200,  # BacklogSyncSitter: max(200, push_debounce_ms)
        description=(
            "Milliseconds to wait after a backlog change before pushing it "
            "(at least 200), so several quick changes go out together."
        ),
        scope=["global", "project"],
    ),
    "observability.backlog_sync_stale_seconds": _setting(
        type="integer",
        default=900,
        description=(
            "Show a warning when the backlog hasn't synced for this many "
            "seconds. Lower = earlier warning; higher = fewer false alarms "
            "on slow connections."
        ),
        scope=["global", "project"],
    ),
    "outer_gate.enabled": _setting(
        type="boolean",
        default=False,
        description=(
            "Unfinished feature; leave off. Will let remote apps use a limited "
            "set of AIDOCS tools on this computer, with sign-in, a project "
            "check and logging. It doesn't accept network connections yet."
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
            "Local address the remote-tools feature (outer_gate.enabled) "
            "listens on. Only this computer's own addresses are allowed."
        ),
        scope=["global", "project"],
    ),
    "distribution.flavor": _setting(
        type="string",
        default="solo",
        allowed_values=["dev", "solo", "corpo"],
        value_descriptions={
            "dev": "For people working on AIDOCS itself: it may edit its own code. Only works from a source checkout.",
            "solo": "One person on their own computer; you are the admin (default).",
            "corpo": "A team: everyone signs in, and the first to register is the admin.",
        },
        description="What kind of install this is.",
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
            "Not built yet; turning it on only logs a refused request. "
            "Planned: run AIDOCS commands with PowerShell instead of Bash, "
            "for computers where Bash can't be installed."
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
            "Store the full text of each prompt in the activity log. Off: "
            "only its length, a fingerprint and what was detected in it are "
            "kept. Prompts can be sensitive, so turn on only when you need a "
            "complete record and are allowed to keep one."
        ),
        scope=["global", "project"],
    ),
    "audit.capture_response_content": _setting(
        type="boolean",
        default=False,
        description=(
            "Store the full text of each assistant reply in the activity log. "
            "Off: only counts, duration and token totals are kept. Same "
            "caution as audit.capture_prompt_content."
        ),
        scope=["global", "project"],
    ),
    "observability.expose_pids": _setting(
        type="boolean",
        default=False,
        description=(
            "Show process IDs in the results of tools that start or check "
            "commands and workers, so you can find them in Task Manager or "
            "htop. Off by default to keep results short; turn on when "
            "chasing a runaway process."
        ),
        scope=["global", "project"],
    ),
    # conductor.autowake_mode and conductor.autowake_max_interval_seconds
    # REMOVED 2026-04-30 (autowake removal). Both settings were retired
    # with the rest of the autowake/forced-work feature.
    "conductor.opencode_model": _setting(
        type="string",
        default="",
        description="AI model OpenCode workers use, e.g. anthropic/claude-sonnet-4-20250514 or openai/gpt-4o. Empty = OpenCode's own default.",
        scope=["global", "project", "session"],
    ),
    "conductor.claude_model": _setting(
        type="string",
        default="",
        description="AI model Claude Code workers use, e.g. claude-sonnet-4-6. Empty = Claude Code's own default.",
        scope=["global", "project", "session"],
    ),
    "conductor.codex_model": _setting(
        type="string",
        default="",
        description="AI model Codex workers use, e.g. gpt-5.4 or o3. Empty = Codex's own default.",
        scope=["global", "project", "session"],
    ),
    "conductor.task_routing": _setting(
        type="string",
        default="{}",
        value_format="json_object",
        description='Which app, model and thinking depth the conductor uses for each kind of task, as JSON. Example: {"implement":{"host":"claude","model":"claude-sonnet-4-6","think_mode":"low"},"design":{"host":"opencode","model":"google/gemini-2.5-pro","think_mode":"high"}}. Kinds not listed use the defaults above.',
        scope=["global", "project", "session"],
    ),
    "conductor.think_mode": _setting(
        type="string",
        default="medium",
        description="How hard worker agents think before acting, unless conductor.task_routing says otherwise for that kind of task.",
        allowed_values=["off", "low", "medium", "high"],
        value_descriptions={
            "off": "No extra thinking; fastest and cheapest.",
            "low": "A little; fine for simple tasks.",
            "medium": "Balanced (default).",
            "high": "A lot; for complex, many-step work.",
        },
        scope=["global", "project", "session"],
    ),
    "conductor.require_agent_tests": _setting(
        type="boolean",
        default=False,
        description="Worker agents must write and run tests for their changes before reporting done; the conductor checks their report for it.",
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
            # Phoenix 2026-05-12 (design rule): msg_* universal
            # comms surface. Every agent — workers, experts,
            # conductors, co-co — can send/receive role-addressed
            # messages. Targets stay predefined via the role enum
            # (msg_send to_roles param). Witnessed gap in smoke
            # 2026-05-11: lane workers couldn't call cerberus_*
            # (old name) and blocked on plan steps that required
            # mid-flight comms with the conductor.
            "msg_*",
        ],
        description="Tools a worker agent may use, as name patterns (e.g. ai_* = every tool starting with ai_). Anything not matching is blocked for workers.",
        scope=["global", "project", "session"],
    ),
    "conductor.lane_extra_tools": _setting(
        type="string_list",
        default=[],
        description="More tool name patterns workers may use, on top of conductor.lane_allowed_tools (e.g. for your own MCP tools).",
        scope=["global", "project", "session"],
    ),
    "execution.max_events": _setting(
        type="integer",
        default=10000,
        description="How many activity-log entries to keep; the oldest are deleted past this. 0 = unlimited.",
        scope=["global", "project"],
    ),
    "execution.auto_prune_days": _setting(
        type="integer",
        default=7,
        description="Delete activity-log entries older than this many days. 0 = keep them.",
        scope=["global", "project"],
    ),
    "code_quality.comment_enforcement": _setting(
        type="string",
        default="advisory",
        description="How strictly agents must follow the code-comment rules when editing.",
        allowed_values=["strict", "advisory", "off"],
        value_descriptions={
            "strict": "Edits that break the rules are refused.",
            "advisory": "Agents are reminded, but edits go through (default).",
            "off": "No reminders, no checks.",
        },
        scope=["global", "project", "session"],
    ),
    "memory.capture_gate.auto_merge": _setting(
        type="boolean",
        default=False,
        description=(
            "When a new memory overlaps one already saved. Off: it isn't "
            "saved; the agent must show you both and ask, so you decide what "
            "memory says. On: they are merged without asking, which is "
            "quicker but can change saved memory without you seeing it."
        ),
        scope=["global", "project"],
    ),
    "edit.str_replace_max_old_chars": _setting(
        type="integer",
        default=1000,
        description=(
            "Longest text, in characters, an agent may quote when replacing "
            "text in a file. Bigger edits must name a symbol or anchor "
            "instead, which keeps the conversation shorter. The new text has "
            "no limit. 0 = unlimited."
        ),
        scope=["global", "project", "session"],
    ),
    "edit.line_edit_relock_free_span": _setting(
        type="integer",
        default=10,
        description=(
            "After an edit by line number, line numbers in that file shift, "
            "so the agent must re-read it before the next such edit. A "
            "second edit of up to this many lines is allowed without "
            "re-reading. 0 = always re-read. Edits by text are not affected. "
            "Resets with each prompt."
        ),
        scope=["global", "project", "session"],
    ),
    "notifications.max_displays": _setting(
        type="integer",
        default=3,
        description=(
            "How many times a 'command finished' notice is shown to the agent "
            "before it goes away on its own. 0 = keep showing it until the "
            "agent reads the command's output."
        ),
        scope=["global", "project", "session"],
    ),
    "presentation.helper_skill_excerpt_lines": _setting(
        type="integer",
        default=12,
        description="Most lines of a suggested skill shown to the agent. 0 = unlimited.",
        scope=["global", "project"],
    ),
    "presentation.helper_skill_excerpt_chars": _setting(
        type="integer",
        default=1200,
        description="Most characters of a suggested skill shown to the agent. 0 = unlimited.",
        scope=["global", "project"],
    ),
    "presentation.workflow_summary_limit": _setting(
        type="integer",
        default=3,
        description="Most next steps listed in a short workflow summary. 0 = unlimited.",
        scope=["global", "project"],
    ),
    "presentation.resume_journal_last_n": _setting(
        type="integer",
        default=10,
        description="How many recent session activity entries (from the audit DB) an agent gets when it resumes a session. 0 = unlimited.",
        scope=["global", "project", "session"],
    ),
    "presentation.handoff_stale_after_hours": _setting(
        type="integer",
        default=24,
        description="A handoff note not updated for this many hours is marked out of date.",
        scope=["global", "project"],
    ),
    "presentation.handoff_recent_hours": _setting(
        type="integer",
        default=24,
        description="A handoff step changed within this many hours is marked as recent.",
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
        value_format="json_array",
        description=(
            "Extra dangerous command patterns for this project, as a JSON "
            "list, each with its own severity. Use it for risks specific to "
            "your tools; the ~30 built-in patterns always apply too."
        ),
        security_sensitive=True,
        dashboard_only=True,
        scope=["global", "project"],
        is_t0=True,
    ),
    "policies.tools": _setting(
        type="string",
        default="[]",
        value_format="json_array",
        description=(
            "Your own allow and deny rules for AIDOCS tools (not shell "
            "commands), as a JSON list. Each rule names a tool and can be "
            "limited to a scope or folder. Use it to switch tools off or "
            "keep them to parts of the project."
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
            "Lets conductors start helper agents for research. Off: refused, "
            "unless you say 'allow delegate research' in a prompt (that turn "
            "only). Turn on if you often want research spread over several "
            "agents."
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
            "File types not checked for secrets (by default images, PDFs, "
            "notebooks, logs). Secrets in these files are not caught, so add "
            "types with care; removing one is always safe."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.exempt_paths": _setting(
        type="string_list",
        default=[],
        description=(
            "Files or folders not checked for secrets (full or "
            "project-relative paths). Secrets there are not caught, so keep "
            "this list short."
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
            "File name patterns the agent may never read (by default "
            "tokens.json, secrets.json, appsettings.*.json and similar). Add "
            "your project's own secret files, e.g. 'config/*.production.yaml'."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "security.explicit_confirm_on_grant": _setting(
        type="boolean",
        default=False,
        description=(
            "On: to give a permission in a prompt you must include the word "
            "'explicitly', so casual wording isn't taken as a yes. Off: a "
            "clear request is enough. Some permissions always need a "
            "confirmation anyway."
        ),
        security_sensitive=True,
        scope=["global", "project"],
    ),
    "dev.runtime.ai_run_trace": _setting(
        type="boolean",
        default=False,
        description=(
            "For AIDOCS developers only (distribution.flavor 'dev'): log "
            "every command AIDOCS runs, with exit code, duration and shell. "
            "Does nothing on other installs."
        ),
        scope=["global", "project", "session"],
    ),
    "pg.connection_string": _setting(
        type="string",
        default="",
        description=(
            "PostgreSQL connection for the older database tools. "
            "Format accepted: postgresql://user:pass@host:port/db or "
            "key=value pairs. Empty = look for one in the project's .env, "
            "appsettings.json or alembic.ini."
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
