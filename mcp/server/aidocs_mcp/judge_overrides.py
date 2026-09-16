"""Judge-rule family-split opt-out — data layer (backlog #19).

Operator reasoning about judge overrides is category-first ("is this
the destructive-git catch?") rather than rule-first ("should
GIT_FORCE_PUSH fire?"). This module stores the override config split
by rule family so any surface (dashboard, TOML tooling) reasons at
the level that matches intent.

Storage (additive, migration-guarded — the #104 PRAGMA/ALTER spirit
applied to config keys):

- Canonical per-family rows: ``security.judge_override.<family>``
  (string_list). Empty/absent = family fully active.
- Legacy flat list ``security.judge_override`` keeps working: the
  reader auto-buckets its rule_ids into families on every read. The
  writer only ever writes the per-family rows.

Sentinel: the literal ``"@all"`` inside a family list disables the
whole family (bulk opt-out) while keeping per-rule granularity
visible in the same data shape.

Locked rules (heuristic_judge.LOCKED_JUDGE_RULE_IDS — credential
exfil patterns, download-then-execute chains, catastrophic
destructive taxonomy) are refused by the writer and silently ignored
by ``flatten_judge_overrides`` — they can never leave the enforcement
surface, whatever the config rows say.

Audit: every toggle emits ``judge_rule_disabled`` /
``judge_rule_enabled`` execution events carrying family, rule_id,
scope and operator identity (identity resolves via the execution
store's IdentityResolver at insert time).
"""

from __future__ import annotations

from pathlib import Path

from .heuristic_judge import (
    JUDGE_FAMILIES,
    LOCKED_JUDGE_RULE_IDS,
    list_judge_rules,
)

ALL_SENTINEL = "@all"

_OVERRIDE_KEY = "security.judge_override"


def _rule_family_map(project_root: Path | None = None) -> dict[str, str]:
    return {str(r["rule_id"]): str(r["family"]) for r in list_judge_rules(project_root)}


def get_judge_overrides(
    project_root: Path,
    session_id: str | None = None,
) -> dict[str, list[str]]:
    """Effective override map ``{family: [rule_ids]}``.

    Merges the canonical per-family rows with the legacy flat list
    (auto-bucketed by each rule_id's family; unknown rule_ids land in
    the ``general`` bucket so legacy suppressions never silently
    vanish). Order is preserved, duplicates dropped.
    """
    from .config import get_setting

    overrides: dict[str, list[str]] = {family: [] for family in JUDGE_FAMILIES}

    for family in JUDGE_FAMILIES:
        value = get_setting(
            f"{_OVERRIDE_KEY}.{family}",
            project_root=project_root,
            session_id=session_id,
            default=[],
        )
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item and item not in overrides[family]:
                    overrides[family].append(item)

    legacy = get_setting(
        _OVERRIDE_KEY,
        project_root=project_root,
        session_id=session_id,
        default=[],
    )
    if isinstance(legacy, list) and legacy:
        family_of = _rule_family_map(project_root)
        for item in legacy:
            if not isinstance(item, str) or not item:
                continue
            family = family_of.get(item, "general")
            if item not in overrides[family]:
                overrides[family].append(item)

    return overrides


def set_judge_override(
    project_root: Path,
    family: str,
    rule_ids: list[str],
    *,
    scope: str = "project",
    session_id: str = "",
    actor: str = "",
) -> dict[str, list[str]]:
    """Persist the opt-out list for one family (dashboard-only surface).

    Validates family and refuses locked rule_ids outright. Writes the
    canonical per-family row and emits ``judge_rule_disabled`` /
    ``judge_rule_enabled`` audit events for the delta against the
    previously effective list. Returns the new effective map.
    """
    if family not in JUDGE_FAMILIES:
        raise ValueError(
            f"Unknown judge-rule family `{family}`. Valid families: "
            f"{', '.join(JUDGE_FAMILIES)}.",
        )
    if not isinstance(rule_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in rule_ids
    ):
        raise ValueError("rule_ids must be a list of non-empty strings.")
    normalized = [item.strip() for item in rule_ids]
    locked_requested = sorted(set(normalized) & LOCKED_JUDGE_RULE_IDS)
    if locked_requested:
        raise ValueError(
            "Refusing to override LOCKED judge rules (never disablable): "
            + ", ".join(locked_requested),
        )

    previous = get_judge_overrides(
        project_root,
        session_id=session_id or None,
    ).get(family, [])

    from .config_store import ConfigStore

    ConfigStore().set(
        project_root,
        f"{_OVERRIDE_KEY}.{family}",
        normalized,
        scope=scope,
        scope_key=session_id if scope == "session" else "",
    )

    added = [item for item in normalized if item not in previous]
    removed = [item for item in previous if item not in normalized]
    for rule_id, event_kind in [(r, "judge_rule_disabled") for r in added] + [
        (r, "judge_rule_enabled") for r in removed
    ]:
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind=event_kind,
                source_kind="judge_overrides.set_judge_override",
                session_id=session_id or None,
                capability_name="security.judge_override",
                action_kind="config_write",
                target_entity=f"{family}:{rule_id}",
                status="allowed",
                payload={
                    "family": family,
                    "rule_id": rule_id,
                    "scope": scope,
                    "actor": actor,
                    "new_list": normalized,
                },
            )
        except Exception:
            # Best-effort audit — the config write above already
            # emitted config_write_internal; never roll back on a
            # journal failure.
            pass

    return get_judge_overrides(project_root, session_id=session_id or None)


def flatten_judge_overrides(
    overrides: dict[str, list[str]] | list[str],
    project_root: Path | None = None,
) -> set[str]:
    """Expand an override map (or legacy flat list) into the flat
    rule_id suppression set the orchestrator's active_verdicts filter
    consumes.

    ``"@all"`` expands to every non-locked rule in that family. Locked
    rule_ids never flatten into the suppression set — enforcement
    keeps them active regardless of config contents (defense in depth
    behind the writer-side refusal).
    """
    flat: set[str] = set()
    if isinstance(overrides, list):
        flat.update(item for item in overrides if isinstance(item, str) and item)
    elif isinstance(overrides, dict):
        rules_by_family: dict[str, list[str]] | None = None
        for family, items in overrides.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, str) or not item:
                    continue
                if item == ALL_SENTINEL:
                    if rules_by_family is None:
                        rules_by_family = {}
                        for rule in list_judge_rules(project_root):
                            rules_by_family.setdefault(str(rule["family"]), []).append(
                                str(rule["rule_id"]),
                            )
                    flat.update(rules_by_family.get(str(family), []))
                else:
                    flat.add(item)
    return flat - LOCKED_JUDGE_RULE_IDS
