"""Operator Surface Catalog — product-level control plane over the raw
config ledger.

``config_schema.SETTINGS_CATALOG`` stays the canonical low-level schema:
one flat entry per dotted key, with type/default/scopes/security flags.
That catalog is a *ledger*, not a control plane — handing an operator 100
individual T0/debug/provider flags is a dump, and (worse) it lets a
dangerous capability be HALF-enabled by flipping one interdependent flag
at a time.

This module is the operator-grade layer ON TOP of that ledger. It groups
keys into coherent **profiles** per doctrine area, each with:

  * a human title + description + danger band,
  * the member keys it presents and the hidden keys it OWNS,
  * an optional ``managed_by`` service (e.g. Governed Bash) that applies
    the whole interdependent set atomically with posture verification,
  * a status resolver (what is the current verified state?), and
  * an apply action (write a coherent set atomically, readback + rollback).

It NEVER replaces the catalog or changes how a value is stored/validated.
Every raw key remains inspectable (``inspect_key``) with its provenance,
scope cascade, effective value, and owning profile — the Advanced Raw
Catalog is the expert/diagnostics view, gated by exact confirmation for
T0/dashboard-only keys.

Doctrine areas (one coherent surface each):
  governed_bash · secrets_transcript · freeze_approval · breakglass_flavor
  · authority_border · conductor_profile · runtime_budgets ·
  indexing_memory · observability_audit · advanced_raw (catch-all).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── danger bands ─────────────────────────────────────────────────────
DANGER_LOW = "low"
DANGER_MEDIUM = "medium"
DANGER_HIGH = "high"
DANGER_CRITICAL = "critical"

ADVANCED_RAW_ID = "advanced_raw"


@dataclass(frozen=True)
class OperatorProfile:
    id: str
    title: str
    doctrine_area: str
    description: str
    danger: str
    # Visible member keys the profile presents for normal editing.
    keys: tuple[str, ...] = ()
    # Keys the profile OWNS but does not surface for direct editing — set
    # only via the profile's apply action / managed service, or shown
    # read-only as diagnostics (deprecated aliases, low-level provider
    # flags, etc.).
    hidden_owned_keys: tuple[str, ...] = ()
    # Non-empty → a high-level service owns enable/disable for this group
    # (e.g. "governed_bash"); apply routes there for atomic posture work.
    managed_by: str = ""
    # advanced_only profiles are hidden from the simple operator view.
    advanced_only: bool = False
    # requires_confirmation: a dangerous profile whose apply action demands
    # an exact confirmation token AND a non-empty reason — never a quiet
    # per-key save (Breakglass & Flavor, Authority Exceptions/Border Law).
    requires_confirmation: bool = False

    def all_owned(self) -> tuple[str, ...]:
        return tuple(self.keys) + tuple(self.hidden_owned_keys)


# ── the catalog ──────────────────────────────────────────────────────
_PROFILES: list[OperatorProfile] = [
    OperatorProfile(
        id="governed_bash",
        title="Governed Bash / Shell Authority",
        doctrine_area="shell",
        description=(
            "One switch for governed native shell. Identity-verified "
            "provider, trusted root, optional hash/signature pin, and a "
            "live probe must ALL verify before this reports enabled. "
            "Enable/disable/repair via the Governed Bash wizard — never by "
            "flipping the individual provider flags."
        ),
        danger=DANGER_HIGH,
        managed_by="governed_bash",
        keys=("bash",),
        hidden_owned_keys=(
            "tools.shell_enforcement_live",
            "tools.native_shell_provider_enabled",
            "tools.native_shell_readonly_enabled",
            "tools.native_shell_provider_path",
            "tools.native_shell_trusted_roots",
            "tools.native_shell_provider_sha256",
            "tools.native_shell_require_os_signature",
            "tools.native_shell_readonly_extra_commands",
            "tools.shell_lifecycle_preflight_enforce",
            "tools.shell_disconnect_after_seconds",
            "tools.shell_policy_shadow_enabled",
        ),
    ),
    OperatorProfile(
        id="secrets_transcript",
        title="Secrets & Transcript Safety",
        doctrine_area="secrets",
        description=(
            "Secret redaction in tool output and prompts, raw-secret read "
            "policy, and the exemption / protected-pattern lists that shape "
            "what is treated as sensitive."
        ),
        danger=DANGER_HIGH,
        keys=(
            "security.allow_raw_read_of_secrets",
            "security.tool_output_secret_policy",
            "security.prompt_secret_policy",
            "security.require_output_redaction_for_run",
            "security.exempt_extensions",
            "security.exempt_paths",
            "security.protected_patterns",
        ),
    ),
    OperatorProfile(
        id="freeze_approval",
        title="Freeze & Approval Policy",
        doctrine_area="freeze",
        description=(
            "When the session freezes and how operator approvals work: "
            "explicit-confirm-on-grant, the agent tool-side violation "
            "threshold, and the operator forbidden-prompt threshold."
        ),
        danger=DANGER_HIGH,
        keys=(
            "security.explicit_confirm_on_grant",
            "security.agent_security_violation_freeze_threshold",
            "security.operator_forbidden_prompt_freeze_threshold",
        ),
        hidden_owned_keys=(
            "security.repeated_violation_freeze_threshold",  # deprecated alias
        ),
    ),
    OperatorProfile(
        id="breakglass_flavor",
        title="Breakglass & Flavor",
        doctrine_area="flavor",
        description=(
            "Distribution flavor (dev/solo/corpo) and superadmin escape "
            "hatches. #404: the kill switch and dev-mode audit/RBAC bypass "
            "keys are excised — flavor remains global-locked."
        ),
        danger=DANGER_CRITICAL,
        requires_confirmation=True,
        keys=(
            "distribution.flavor",
            "security.superadmin_allow_powershell_ai_run_backend",
        ),
    ),
    OperatorProfile(
        id="authority_border",
        title="Authority Exceptions / Border Law",
        doctrine_area="authority",
        description=(
            "The master enforcement switch and the deliberate exceptions "
            "to it: approved external roots, config-edit gate, raw-edit / "
            "inactive-memory exceptions, egress allowlist, judge override, "
            "and palace maintenance."
        ),
        danger=DANGER_HIGH,
        requires_confirmation=True,
        keys=(
            "security.enforce",
            "security.tier_enforcement",
            "security.allow_config_edit",
            "security.approved_external_roots",
            "security.allow_raw_edits",
            "security.allow_inactive_memory_read",
            "security.egress_allowlist",
            "security.delegate_research_allowed",
            "security.judge_override",
            "security.allow_palace_maintenance",
        ),
        hidden_owned_keys=(
            "security.allow_raw_shell",  # deprecated → Governed Bash
        ),
    ),
    OperatorProfile(
        id="conductor_profile",
        title="Conductor Profile",
        doctrine_area="conductor",
        description=(
            "How the conductor runs lanes: backend, per-backend models, "
            "concurrency, task routing, think mode, lane tool grants, and "
            "whether sub-agents and agent tests are required."
        ),
        danger=DANGER_MEDIUM,
        keys=(
            "conductor.backend",
            "conductor.max_concurrent_workers",
            "conductor.auto_exit_lane",
            "conductor.opencode_model",
            "conductor.claude_model",
            "conductor.codex_model",
            "conductor.task_routing",
            "conductor.think_mode",
            "conductor.require_agent_tests",
            "conductor.lane_allowed_tools",
            "conductor.lane_extra_tools",
            "agents.allow_subagents",
        ),
    ),
    OperatorProfile(
        id="runtime_budgets",
        title="Runtime Budgets",
        doctrine_area="budgets",
        description=(
            "Time and concurrency budgets: tool-call / sync / index / git "
            "timeouts, the bash long-runner cap, run timeouts, per-machine "
            "live-process cap, and edit size limits."
        ),
        danger=DANGER_LOW,
        keys=(
            "tools.tool_call_timeout",
            "tools.sync_write_timeout",
            "tools.index_sync_timeout",
            "tools.memory_surfacing_timeout_ms",
            "tools.git_functions_timeout",
            "tools.max_timeout",
            "tools.bash_long_runner_cap_seconds",
            "run.max_timeout_seconds",
            "run.max_live_processes_per_machine",
            "edit.str_replace_max_old_chars",
        ),
    ),
    OperatorProfile(
        id="indexing_memory",
        title="Indexing & Memory",
        doctrine_area="indexing",
        description=(
            "What gets indexed and how memory/journal behaves: skip dirs, "
            "module hints, languages, JSON size cap, test inclusion, "
            "journal limits, and memory capture auto-merge."
        ),
        danger=DANGER_LOW,
        keys=(
            "index.extra_skip_dirs",
            "index.extra_module_hints",
            "index.max_json_size",
            "index.enabled_languages",
            "index.include_tests",
            "languages.enabled",
            "journal.max_entries",
            "journal.evict_batch",
            "journal.trivial_actions",
            "journal.min_intent_length",
            "memory.capture_gate.auto_merge",
        ),
    ),
    OperatorProfile(
        id="observability_audit",
        title="Observability / Audit",
        doctrine_area="observability",
        description=(
            "What AIDOCS records and exposes: user-drop watch, PID "
            "exposure, prompt/response capture, execution-event retention, "
            "and the decision-trace emitter."
        ),
        danger=DANGER_LOW,
        keys=(
            "observability.watch_user_drops",
            "observability.watch_user_drops_debounce_ms",
            "observability.expose_pids",
            "audit.capture_prompt_content",
            "audit.capture_response_content",
            "execution.max_events",
            "execution.auto_prune_days",
            "security.emit_decision_trace",
        ),
    ),
    OperatorProfile(
        id=ADVANCED_RAW_ID,
        title="Advanced Raw Catalog",
        doctrine_area="advanced",
        description=(
            "Expert / diagnostics view of the full flat catalog. Every key "
            "not owned by a doctrine profile above lives here. Editing a "
            "T0 / dashboard-only key requires exact confirmation; "
            "deprecated keys are read-only with a migration note. Use the "
            "doctrine profiles for normal operation."
        ),
        danger=DANGER_CRITICAL,
        advanced_only=True,
    ),
]


# ── reverse index: every key → its owning profile ───────────────────
def _build_claimed() -> dict[str, str]:
    claimed: dict[str, str] = {}
    for prof in _PROFILES:
        if prof.id == ADVANCED_RAW_ID:
            continue
        for key in prof.all_owned():
            # First profile to claim a key wins; a key must not be owned
            # by two doctrine profiles (validate_catalog enforces this).
            claimed.setdefault(key, prof.id)
    return claimed


_CLAIMED: dict[str, str] = _build_claimed()


def list_profiles(*, include_advanced: bool = True) -> list[OperatorProfile]:
    return [p for p in _PROFILES if include_advanced or not p.advanced_only]


def get_profile(profile_id: str) -> OperatorProfile | None:
    for p in _PROFILES:
        if p.id == profile_id:
            return p
    return None


def owning_profile(key: str) -> str:
    """The profile id that owns ``key``. Unclaimed keys belong to the
    Advanced Raw Catalog so the index is total over the whole catalog.
    """
    return _CLAIMED.get(key, ADVANCED_RAW_ID)


# ── deprecated / reserved keys ───────────────────────────────────────
def migration_message(key: str) -> str:
    """The operator-facing migration note for a deprecated key, or ""."""
    from .config_schema import SETTINGS_CATALOG

    meta = SETTINGS_CATALOG.get(key)
    return str(meta.get("deprecated") or "") if meta else ""


def is_deprecated(key: str) -> bool:
    return bool(migration_message(key))


# ── editability verdict (single source for every write surface) ──────
def editability(key: str) -> dict[str, Any]:
    """Why a key can or cannot be edited via a NORMAL settings surface.

    Returns {editable, reason, redirect, owning_profile}. Service-managed
    and deprecated keys are never normally editable; dashboard_only /
    security_sensitive keys are editable only via the expert path with
    exact confirmation.
    """
    from .config_schema import SETTINGS_CATALOG

    meta = SETTINGS_CATALOG.get(key)
    prof = owning_profile(key)
    if meta is None:
        return {"editable": False, "reason": "unknown_key", "redirect": "", "owning_profile": prof}
    if meta.get("deprecated"):
        return {
            "editable": False,
            "reason": "deprecated",
            "redirect": str(meta.get("deprecated")),
            "owning_profile": prof,
        }
    svc = str(meta.get("service_managed") or "")
    if svc:
        return {
            "editable": False,
            "reason": "service_managed",
            "redirect": (
                f"Managed by the '{svc}' profile — apply via that "
                f"profile's wizard, not as an individual setting."
            ),
            "owning_profile": prof,
        }
    if meta.get("dashboard_only"):
        return {
            "editable": False,
            "reason": "dashboard_only",
            "redirect": (
                "T0 / dashboard-only — editable only via the Advanced Raw "
                "Catalog expert path with exact confirmation."
            ),
            "owning_profile": prof,
        }
    if is_hidden_owned(key):
        return {
            "editable": False,
            "reason": "hidden_owned",
            "redirect": (
                f"Hidden-owned by the {prof!r} profile — editable only via "
                f"the Advanced Raw expert path (exact confirmation) or the "
                f"owning profile action."
            ),
            "owning_profile": prof,
        }
    return {"editable": True, "reason": "", "redirect": "", "owning_profile": prof}


def is_normal_editable(key: str) -> bool:
    """POSITIVE proof that ``key`` may be written by a NORMAL save path.

    True ONLY when the key exists in the catalog and is not deprecated,
    service-managed, dashboard-only, or hidden-owned. Fails CLOSED: any
    exception (import error, malformed catalog, unknown key) returns False.
    Every normal config write path must require this — a normal save may
    persist a key only when it is positively proven normal-editable, never
    merely 'not known to be bad'.
    """
    try:
        return bool(editability(key).get("editable"))
    except Exception:
        return False


def blast_radius(scope: str, setting_path: str = "") -> dict[str, Any]:
    """Classify the BLAST RADIUS of a config write so coarser/broadening
    writes are explicit in the audit trail and the operator UI.

    Config resolution is factory < global < project < session (more specific
    wins). A write's blast radius is the inverse: the COARSER the scope, the
    MORE it affects.

      - ``global``  → install-wide: EVERY project on this machine inherits it
        (unless a project/session overrides). This is a BROADENING write — it
        reaches beyond the project the operator is looking at, so it carries an
        explicit warning the dashboard must surface before confirming.
      - ``project`` → one project.
      - ``session`` → one session (narrowest).

    Returned shape (stable for audit payloads + UI):
    ``{radius, scope, affects, broadening, warning}``. This is LABELING only —
    it never authorizes; the auth/RBAC/scope gates decide whether the write
    happens, this only makes its reach honest.
    """
    s = (scope or "project").strip().lower()
    key = f" `{setting_path}`" if setting_path else ""
    if s == "global":
        return {
            "radius": "global",
            "scope": "global",
            "affects": "all projects on this install",
            "broadening": True,
            "warning": (
                f"GLOBAL (install-wide) write{key}: this applies to EVERY "
                f"project on this machine, not only the current one. A project "
                f"or session may still override it, but the default changes "
                f"everywhere. Confirm this broad reach is intended."
            ),
        }
    if s == "session":
        return {
            "radius": "session",
            "scope": "session",
            "affects": "one session",
            "broadening": False,
            "warning": "",
        }
    return {
        "radius": "project",
        "scope": "project",
        "affects": "one project",
        "broadening": False,
        "warning": "",
    }


# ── single guard for EVERY raw write surface ────────────────────────
def guard_raw_write(key: str, *, action: str = "set") -> dict[str, Any]:
    """The one verdict every raw settings write/delete surface must
    consult BEFORE persisting — agent config_set, CLI config set, the
    authenticated dashboard single/batch/delete saves, and any direct
    dashboard settings save.

    Refuses:
      * service_managed keys, always — they belong to a high-level
        profile/service (e.g. Governed Bash) that applies the whole
        interdependent set atomically; flipping one through a raw save
        would create a half-enabled posture. Even an authenticated
        admin save is refused; the operator must use the profile action.
      * deprecated keys on set/batch — surface the migration path instead
        of writing a dead alias. (delete is allowed: clearing a stale
        deprecated override is cleanup, never a half-enable.)

    Returns {allowed, reason, message, redirect}. Unknown keys pass here
    (the surface's own unknown-key handling applies).
    """
    from .config_schema import SETTINGS_CATALOG

    meta = SETTINGS_CATALOG.get(key)
    if meta is None:
        # Fail CLOSED: an unknown key cannot be proven safe. delete of an
        # unknown override is harmless cleanup, so allow only that.
        if action == "delete":
            return {"allowed": True, "reason": "", "message": "", "redirect": ""}
        return {
            "allowed": False,
            "reason": "unknown_key",
            "message": (
                f"'{key}' is not in the settings catalog; refusing to write an unrecognized key."
            ),
            "redirect": "",
        }
    svc = str(meta.get("service_managed") or "")
    if svc:
        prof = owning_profile(key)
        msg = (
            f"'{key}' is managed by the '{svc}' profile and cannot be "
            f"written as an individual setting from any settings surface. "
            f"Apply it through the {prof!r} profile action "
            f"(e.g. `aidocs governed-bash-enable/-disable`) so the whole "
            f"set stays coherent."
        )
        return {"allowed": False, "reason": "service_managed", "message": msg, "redirect": prof}
    dep = str(meta.get("deprecated") or "")
    if dep and action != "delete":
        return {
            "allowed": False,
            "reason": "deprecated",
            "message": f"'{key}' is deprecated. {dep}",
            "redirect": dep,
        }
    # Hidden-owned keys (a profile owns them but does not surface them for
    # direct editing — e.g. low-level shell pilot/shadow flags) are refused
    # from every NORMAL write surface, not merely hidden from the row list.
    # The ONLY explicitly-allowed path is the Advanced Raw expert edit
    # (expert_set, with exact confirmation). delete (revert to default) is
    # allowed as cleanup.
    if action != "delete" and is_hidden_owned(key):
        prof = owning_profile(key)
        return {
            "allowed": False,
            "reason": "hidden_owned",
            "message": (
                f"'{key}' is a hidden-owned key of the {prof!r} profile and "
                f"is not editable from a normal settings surface. Use the "
                f"Advanced Raw expert edit (exact confirmation) if you must "
                f"change it directly."
            ),
            "redirect": prof,
        }
    return {"allowed": True, "reason": "", "message": "", "redirect": ""}


# ── raw-key inspector: provenance + scope cascade + owning profile ───
def inspect_key(
    project_root: Path,
    key: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Full diagnostics for one raw key: schema metadata, owning profile,
    deprecation, the per-scope value cascade (factory < global < project <
    session), the effective value and which scope won, and the editability
    verdict. This is what the Advanced Raw Catalog renders per row.
    """
    from .config_schema import SETTINGS_CATALOG
    from .config_store import ConfigStore

    meta = SETTINGS_CATALOG.get(key)
    store = ConfigStore()

    factory = meta.get("default") if meta else None
    g = store.get(project_root, key, scope="global", scope_key="")
    p = store.get(project_root, key, scope="project", scope_key="")
    s = store.get(project_root, key, scope="session", scope_key=session_id) if session_id else None
    # Winning source: most specific scope that holds a value.
    if s is not None:
        eff_source, eff_value = "session", s
    elif p is not None:
        eff_source, eff_value = "project", p
    elif g is not None:
        eff_source, eff_value = "global", g
    else:
        eff_source, eff_value = "factory", factory

    cascade = [
        {"scope": "factory", "value": factory, "set": meta is not None},
        {"scope": "global", "value": g, "set": g is not None},
        {"scope": "project", "value": p, "set": p is not None},
    ]
    if session_id:
        cascade.append({"scope": "session", "value": s, "set": s is not None})

    return {
        "key": key,
        "exists_in_catalog": meta is not None,
        "owning_profile": owning_profile(key),
        "type": (meta or {}).get("type"),
        "description": (meta or {}).get("description"),
        "security_sensitive": bool((meta or {}).get("security_sensitive")),
        "dashboard_only": bool((meta or {}).get("dashboard_only")),
        "service_managed": str((meta or {}).get("service_managed") or ""),
        "deprecated": str((meta or {}).get("deprecated") or ""),
        "scope_cascade": cascade,
        "effective_value": eff_value,
        "effective_source": eff_source,
        "editability": editability(key),
    }


# ── row classification: normal Settings vs Advanced Raw diagnostics ──
def _hidden_owned_index() -> set[str]:
    keys: set[str] = set()
    for prof in _PROFILES:
        keys.update(prof.hidden_owned_keys)
    return keys


_HIDDEN_OWNED: set[str] = _hidden_owned_index()


def is_hidden_owned(key: str) -> bool:
    """True iff a profile OWNS this key but does not surface it for direct
    editing (low-level/diagnostic flags). Refused on normal write surfaces;
    editable only via the Advanced Raw expert path.
    """
    return key in _HIDDEN_OWNED


def is_advanced_only_key(key: str) -> bool:
    """A key that must NOT appear in normal Settings rows — surfaced ONLY
    in Advanced Raw diagnostics (with migration/owner messages). True for
    service-managed, deprecated, dashboard-only, and any profile's
    hidden-owned key.
    """
    from .config_schema import SETTINGS_CATALOG

    meta = SETTINGS_CATALOG.get(key)
    if meta is None:
        return True
    if meta.get("service_managed") or meta.get("deprecated"):
        return True
    if meta.get("dashboard_only"):
        return True
    return key in _HIDDEN_OWNED


def settings_rows(
    project_root: Path,
    *,
    session_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Split the whole catalog into the two surfaces the dashboard renders:
    ``normal`` (editable doctrine/ungrouped keys) and ``advanced_raw``
    (service-managed / deprecated / dashboard-only / hidden-owned keys,
    each carrying its inspect_key provenance + migration/owner message).
    Normal rows never include a key that could half-enable a dangerous
    system.
    """
    from .config_schema import SETTINGS_CATALOG

    normal: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []
    for key in SETTINGS_CATALOG:
        row = inspect_key(project_root, key, session_id=session_id)
        if is_advanced_only_key(key):
            advanced.append(row)
        else:
            normal.append(row)
    return {"normal": normal, "advanced_raw": advanced}


# ── status resolution ───────────────────────────────────────────────
def resolve_status(
    project_root: Path,
    profile_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Current state of a profile. Governed Bash delegates to its posture
    card (status derived solely from posture.verified). Generic profiles
    report each member key's effective value + source.
    """
    prof = get_profile(profile_id)
    if prof is None:
        return {"ok": False, "error": f"unknown profile {profile_id!r}"}

    if prof.managed_by == "governed_bash":
        from .governed_bash_service import posture_card
        from .governed_shell_attest import live_execution_posture

        # SINGLE AUTHORITY (Empire re-seal 2026-05-30): live_execution_posture
        # is the ONLY ENABLED bit. The legacy posture_card is retained ONLY
        # as read-only diagnostics — it can never independently show ENABLED.
        live = live_execution_posture(project_root)
        card = posture_card(project_root)
        verified = bool(live.get("ok"))
        return {
            "ok": True,
            "profile": prof.id,
            "managed_by": "governed_bash",
            "status": "enabled" if verified else "disabled",
            # Authoritative bit = live posture.
            "verified": verified,
            "live_execution_posture": {
                "route": live.get("route"),
                "ok": live.get("ok"),
                "reason": live.get("reason"),
                "repair": live.get("repair"),
                "checks": live.get("checks", {}),
            },
            # Legacy posture: READ-ONLY diagnostics only (never the badge).
            "legacy_posture": card,
            "card": card,
        }

    members = {}
    for key in prof.keys:
        info = inspect_key(project_root, key, session_id=session_id)
        members[key] = {
            "effective_value": info["effective_value"],
            "effective_source": info["effective_source"],
        }
    # A generic profile is "customized" when any member differs from
    # factory (i.e. some scope set a value).
    customized = any(m["effective_source"] != "factory" for m in members.values())
    return {
        "ok": True,
        "profile": prof.id,
        "managed_by": "",
        "status": "customized" if customized else "default",
        "members": members,
    }


# ── atomic apply (coherent set, readback, rollback) ─────────────────
def _atomic_apply(
    project_root: Path,
    writes: dict[str, Any],
    *,
    scope: str,
    scope_key: str = "",
) -> dict[str, Any]:
    """Write a coherent set of keys, reading each back. On ANY readback
    mismatch, restore every key written so far to its prior value and
    report failure — never leave a partially applied set.
    """
    from .config_store import ConfigStore

    store = ConfigStore()
    prior: dict[str, Any] = {}
    applied: list[str] = []
    for key, value in writes.items():
        prior[key] = store.get(project_root, key, scope=scope, scope_key=scope_key)
    failed: list[str] = []
    for key, value in writes.items():
        try:
            store.set(project_root, key, value, scope=scope, scope_key=scope_key)
            applied.append(key)
            if store.get(project_root, key, scope=scope, scope_key=scope_key) != value:
                failed.append(key)
                break
        except Exception:
            failed.append(key)
            break
    if failed:
        # Roll back everything we touched to its prior value.
        for key in applied:
            try:
                if prior[key] is None:
                    store.delete(project_root, key, scope=scope, scope_key=scope_key)
                else:
                    store.set(project_root, key, prior[key], scope=scope, scope_key=scope_key)
            except Exception:
                pass
        return {
            "ok": False,
            "failed": failed,
            "message": f"readback failed for {', '.join(failed)}; the whole set was rolled back.",
        }
    return {"ok": True, "applied": applied}


def profile_confirm_token(profile_id: str) -> str:
    """The exact phrase an operator must echo to apply a dangerous
    (requires_confirmation) profile.
    """
    return f"confirm-apply {profile_id}"


def apply_profile(
    project_root: Path,
    profile_id: str,
    *,
    values: dict[str, Any] | None = None,
    operator_authenticated: bool = False,
    confirm_token: str = "",
    reason: str = "",
    scope: str = "global",
    scope_key: str = "",
    **service_params: Any,
) -> dict[str, Any]:
    """Apply a coherent change to a profile.

    Governed Bash routes to its service (atomic enable/disable with
    posture verification + rollback). Generic profiles take a ``values``
    dict of member keys and write them atomically with readback/rollback.
    Keys outside the profile, or keys not normally editable (deprecated /
    service-managed / dashboard-only), are refused — so an operator cannot
    half-enable a dangerous system by smuggling a stray key through a
    profile apply.
    """
    prof = get_profile(profile_id)
    if prof is None:
        return {"ok": False, "error": f"unknown profile {profile_id!r}"}

    if prof.managed_by == "governed_bash":
        from . import governed_bash_service as gb

        action = str(service_params.get("action") or "enable").lower()
        if action == "disable":
            return gb.disable(
                project_root,
                operator_authenticated=operator_authenticated,
                scope=scope,
            )
        # THE one control (Empire re-seal 2026-05-30): "Allow shell tools
        # validated and supported by AIDOCS" — auto-discover the canonical
        # system-owned provider, GENERATE the SHA-256 pin, auto-enroll. No
        # operator-typed path/hash/signature wizard. An unknown / unproven
        # provider returns an EXACT-PATH approval card (enable_supported);
        # the operator then approves that exact path by re-applying WITH an
        # explicit provider_path — the ONLY case a path is accepted, and
        # then ONLY as approval of one exact discovered candidate.
        from . import governed_shell_attest as gsa

        # No card → THE one action (auto-discover + attest). A signed,
        # single-use control-plane approval card → approve that exact
        # candidate. The legacy provider_path side door is REMOVED: a bare
        # operator-typed path can no longer enroll a provider.
        card = service_params.get("approval_card")
        if not card:
            return gsa.enable_supported(
                project_root,
                operator_authenticated=operator_authenticated,
                scope=scope,
            )
        return gsa.approve_exact_path(
            project_root,
            "",
            operator_authenticated=operator_authenticated,
            scope=scope,
            card=card,
        )

    # Dangerous profiles (Breakglass & Flavor, Authority/Border) must be
    # applied through an explicit confirmed action — exact token + a
    # non-empty reason — never a quiet per-key save.
    if prof.requires_confirmation:
        expected = profile_confirm_token(prof.id)
        if confirm_token.strip() != expected:
            return {
                "ok": False,
                "error": "confirmation_required",
                "message": (
                    f"'{prof.id}' is a dangerous profile. To apply, echo "
                    f"the exact confirmation {expected!r} and pass a reason."
                ),
                "expected_confirm": expected,
            }
        if not reason.strip():
            return {
                "ok": False,
                "error": "reason_required",
                "message": f"a non-empty reason is required to apply the '{prof.id}' profile.",
            }

    values = values or {}
    if not values:
        return {"ok": False, "error": "no values to apply"}
    from .config_schema import SETTINGS_CATALOG

    # Only this profile's visible member keys may be set. Per-key rules:
    #   * service-managed / deprecated keys are refused ALWAYS — even a
    #     confirmed dangerous-profile action cannot resurrect a dead alias
    #     or half-enable another service's posture.
    #   * dashboard-only / security-sensitive keys are refused for a
    #     NORMAL profile (use the Advanced Raw expert path) but ALLOWED for
    #     a requires_confirmation profile — that is the whole point of the
    #     confirmed action: Breakglass/Authority owns its own guardrail
    #     keys and may write them after exact confirmation + reason.
    allowed = set(prof.keys)
    for key in values:
        if key not in allowed:
            return {
                "ok": False,
                "error": "key_not_in_profile",
                "message": (
                    f"'{key}' is not a member of the '{profile_id}' profile; refusing to apply."
                ),
            }
        meta = SETTINGS_CATALOG.get(key) or {}
        if meta.get("service_managed"):
            return {
                "ok": False,
                "error": "service_managed",
                "message": (
                    f"'{key}' is managed by the "
                    f"'{meta.get('service_managed')}' service; not writable "
                    f"even through a confirmed profile action."
                ),
            }
        if meta.get("deprecated"):
            return {
                "ok": False,
                "error": "deprecated",
                "message": f"'{key}' is deprecated: {meta.get('deprecated')}",
            }
        if (
            meta.get("dashboard_only") or meta.get("security_sensitive")
        ) and not prof.requires_confirmation:
            return {
                "ok": False,
                "error": "requires_expert_path",
                "message": (
                    f"'{key}' is a T0/security-sensitive key; edit it via "
                    f"the Advanced Raw expert path (exact confirmation), "
                    f"not a plain profile apply."
                ),
            }
    return _atomic_apply(project_root, values, scope=scope, scope_key=scope_key)


# ── expert raw edit (Advanced Raw Catalog) ──────────────────────────
def expert_confirm_token(key: str) -> str:
    """The exact phrase an operator must echo to edit a T0/dashboard-only
    key from the Advanced Raw Catalog.
    """
    return f"confirm-set {key}"


def expert_set(
    project_root: Path,
    key: str,
    value: Any,
    *,
    operator_authenticated: bool,
    confirm_token: str = "",
    scope: str = "global",
    scope_key: str = "",
) -> dict[str, Any]:
    """Expert/diagnostics write for the Advanced Raw Catalog. Deprecated
    keys are refused (migration note). Service-managed keys are refused
    (use the owning profile). Dashboard-only / security-sensitive keys
    require ``confirm_token == expert_confirm_token(key)``. Plain keys
    apply with readback. Always operator-auth gated.
    """
    from .config_schema import SETTINGS_CATALOG

    if not operator_authenticated:
        return {"ok": False, "blocked_by": "operator_auth"}
    meta = SETTINGS_CATALOG.get(key)
    if meta is None:
        return {"ok": False, "error": "unknown_key"}
    if meta.get("deprecated"):
        return {"ok": False, "error": "deprecated", "message": str(meta.get("deprecated"))}
    if meta.get("service_managed"):
        return {
            "ok": False,
            "error": "service_managed",
            "message": (
                f"'{key}' is managed by the '{meta.get('service_managed')}' profile; edit it there."
            ),
        }
    if meta.get("dashboard_only") or meta.get("security_sensitive") or is_hidden_owned(key):
        expected = expert_confirm_token(key)
        if confirm_token.strip() != expected:
            return {
                "ok": False,
                "error": "confirmation_required",
                "message": (
                    f"'{key}' is a T0 / dashboard-only / hidden-owned key. "
                    f"To apply, echo the exact confirmation: {expected!r}."
                ),
                "expected_confirm": expected,
            }
    return _atomic_apply(project_root, {key: value}, scope=scope, scope_key=scope_key)


# ── catalog consistency (used by tests + a CLI doctor) ──────────────
def validate_catalog() -> list[str]:
    """Return a list of problems (empty = healthy):
    * a key owned by two doctrine profiles,
    * a profile key absent from SETTINGS_CATALOG.
    """
    from .config_schema import SETTINGS_CATALOG

    problems: list[str] = []
    seen: dict[str, str] = {}
    for prof in _PROFILES:
        if prof.id == ADVANCED_RAW_ID:
            continue
        for key in prof.all_owned():
            if key in seen:
                problems.append(f"key {key!r} owned by both {seen[key]!r} and {prof.id!r}")
            else:
                seen[key] = prof.id
            if key not in SETTINGS_CATALOG:
                problems.append(f"profile {prof.id!r} references unknown key {key!r}")
    return problems
