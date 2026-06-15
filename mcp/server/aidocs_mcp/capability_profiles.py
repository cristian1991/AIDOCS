"""Capability profiles — a curated grouping over the flat settings catalog.

The raw config catalog is a long list of individual knobs. For operators
that is a dump, not a control plane. A *capability profile* groups the
keys that together turn one capability on/off, with a human title, a risk
band, and (optionally) a high-level manager (a wizard) that owns the
group so the operator flips ONE switch instead of N interdependent flags.

This is a PRESENTATION layer only. It does not replace the catalog — the
full flat catalog stays available for advanced/debug use — and it never
changes how a value is stored or validated. ``managed`` profiles (e.g.
Governed Bash) are rendered by their dedicated wizard; their member keys
remain visible in the advanced catalog for inspection/override.
"""

from __future__ import annotations

# Risk bands drive the UI accent + an "are you sure" affordance.
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# Each profile: id → metadata. `keys` are catalog setting_paths the group
# governs. `managed` names the high-level controller that owns the group
# ("" = render the member keys directly). `advanced_only` hides the group
# from the simple view but keeps the keys in the advanced catalog.
_PROFILES: list[dict] = [
    {
        "id": "governed_bash",
        "title": "Governed Bash (native shell)",
        "description": (
            "Let agents run native shell commands under the SAME AIDOCS law "
            "as ai_run — the governed [bash] allow-table + deny table + "
            "dangerous-chain + destructive floor + heuristic judge — instead "
            "of routing every command to ai_run. (NOT a read-only subset: the "
            "full governed [bash] table applies.) Identity-verified provider, "
            "trusted root, optional hash/signature pin, and a live execution "
            "probe must ALL verify before this reports enabled."
        ),
        "risk": RISK_HIGH,
        "managed": "governed_bash",  # rendered by the Governed Bash wizard
        "keys": [
            "tools.shell_enforcement_live",
            "tools.native_shell_provider_enabled",
            "tools.native_shell_readonly_enabled",
            "tools.native_shell_provider_path",
            "tools.native_shell_trusted_roots",
            "tools.native_shell_provider_sha256",
            "tools.native_shell_require_os_signature",
            "tools.native_shell_readonly_extra_commands",
        ],
    },
    {
        "id": "shell_advanced",
        "title": "Shell — advanced flags",
        "description": (
            "Lower-level shell-policy switches (deprecated pilot no-op, "
            "shadow observation, lifecycle preflight, native detach). Most "
            "operators should use Governed Bash above instead."
        ),
        "risk": RISK_HIGH,
        "managed": "",
        "advanced_only": True,
        "keys": [
            "tools.native_shell_execution_pilot",
            "tools.shell_lifecycle_preflight_enforce",
            "tools.shell_policy_shadow_enabled",
            "tools.shell_disconnect_after_seconds",
        ],
    },
]


def list_profiles() -> list[dict]:
    """Return the capability-profile definitions (static; values are joined
    against the live catalog by the caller/UI).
    """
    return [dict(p) for p in _PROFILES]


def profile_keys() -> set[str]:
    """Every setting_path that belongs to some profile — lets the UI mark
    which catalog entries are 'grouped' vs. ungrouped (advanced).
    """
    keys: set[str] = set()
    for p in _PROFILES:
        keys.update(p.get("keys", []))
    return keys
