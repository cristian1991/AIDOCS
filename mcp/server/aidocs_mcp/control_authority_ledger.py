"""Control-authority ledger — the documented classification of where each
AIDOCS control decision's source of truth lives.

Audit conclusion (2026-05): governance/control authority is SQLite-
canonical across the board. The remaining on-disk writes are one of:
legacy shadow mirrors (SQLite is authority; the file is a deletable
migration-window fallback), generated host projections (rebuildable from
inputs), source descriptors (build-time), or exported verbatim records
(SQLite holds the control decision — e.g. session membership — while the
file remains the human-readable record, not the authority).

This module is the source of truth for that classification; a contract
test (test_control_authority_ledger) enforces it: AUTHORITY_SQL concepts
must be SQLite-backed, and shadow/projection files must be deletable
without changing the control decision.

Categories
----------
AUTHORITY_SQL          — control decision read from a canonical SQLite
                         store. The on-disk file (if any) is not authority.
LEGACY_SHADOW          — SQLite is authority; a file mirror is written for
                         a legacy reader during the migration window and is
                         deletable without changing the decision.
GENERATED_PROJECTION   — a file generated for an external/host consumer
                         (e.g. .mcp.json), rebuildable from its inputs.
SOURCE_DESCRIPTOR      — build-time descriptor (index-language TOMLs, seed).
DOMAIN_RECORD_ARTIFACT — the file IS the canonical record by doctrine
                         (verbatim session record). Not a config bypass;
                         the control decision reads the record itself.
"""

from __future__ import annotations

AUTHORITY_SQL = "AUTHORITY_SQL"
LEGACY_SHADOW = "LEGACY_SHADOW"
GENERATED_PROJECTION = "GENERATED_PROJECTION"
SOURCE_DESCRIPTOR = "SOURCE_DESCRIPTOR"
DOMAIN_RECORD_ARTIFACT = "DOMAIN_RECORD_ARTIFACT"

VALID_CATEGORIES = frozenset(
    {
        AUTHORITY_SQL,
        LEGACY_SHADOW,
        GENERATED_PROJECTION,
        SOURCE_DESCRIPTOR,
        DOMAIN_RECORD_ARTIFACT,
    },
)


# concept -> classification. `store` is the module that owns the SQLite
# table (for AUTHORITY_SQL; via `delegate` when the named module forwards
# to another store). `file_role` documents any on-disk write.
CONTROL_AUTHORITY: dict[str, dict[str, str]] = {
    # ── governance / control authority (SQLite-canonical) ──────────
    "session_freeze": {
        "category": AUTHORITY_SQL,
        "store": "session_freeze_store",
        "file_role": "none",
        "note": "freeze rows in aidocs_identity.sqlite3",
    },
    "escalation_grants": {
        "category": AUTHORITY_SQL,
        "store": "escalation_store",
        "file_role": "none",
        "note": "rbac_escalation_grants / rbac_escalations",
    },
    "host_operator_bindings": {
        "category": AUTHORITY_SQL,
        "store": "host_operator_binding_store",
        "file_role": "none",
        "note": "host↔operator bindings in sqlite",
    },
    "strike_counters": {
        "category": AUTHORITY_SQL,
        "store": "security_violation_service",
        "delegate": "session_freeze_store",
        "file_role": "none",
        "note": "repeated-violation strikes escalate via the freeze store",
    },
    "config_settings": {
        "category": AUTHORITY_SQL,
        "store": "config_store",
        "file_role": "none",
        "note": "config_settings sqlite (typed Settings)",
    },
    "gate_messages": {
        "category": AUTHORITY_SQL,
        "store": "intent_tokens_store",
        "file_role": "source_descriptor",
        "note": "gate_message_strings in empire sqlite; seed TOML build-only",
    },
    "intent_vocab": {
        "category": AUTHORITY_SQL,
        "store": "intent_tokens_store",
        "file_role": "source_descriptor",
        "note": "lemma rows in empire sqlite; seed TOML build-only",
    },
    "task_todos": {
        "category": AUTHORITY_SQL,
        "store": "task_todos_store",
        "file_role": "none",
        "note": "task/todo state in sqlite",
    },
    "lane_agents": {
        "category": AUTHORITY_SQL,
        "store": "session_lane_agents_store",
        "file_role": "none",
        "note": "lane agent state via sqlite index base",
    },
    "execution_audit": {
        "category": AUTHORITY_SQL,
        "store": "execution_index_store",
        "file_role": "none",
        "note": "audit/event ledger in sqlite",
    },
    "identity": {
        "category": AUTHORITY_SQL,
        "store": "identity_store",
        "file_role": "none",
        "note": "users/tokens in sqlite",
    },
    "rbac": {
        "category": AUTHORITY_SQL,
        "store": "rbac_store",
        "file_role": "none",
        "note": "roles/permissions/grants in sqlite",
    },
    # ── SQLite-only authority (file shadow REMOVED 2026-05) ────────
    "managed_mode": {
        "category": AUTHORITY_SQL,
        "store": "aidocs_managed_store",
        "via": "managed_mode_service",
        "file_role": "none",
        "note": "sqlite is the SOLE source of truth; the legacy JSON shadow "
        "write + fallback read were removed — a file can no longer "
        "rehydrate managed-mode authority when sqlite is empty",
    },
    "sticky_user_intent_grants": {
        "category": AUTHORITY_SQL,
        "store": "session_query_gate_store",
        "via": "query_gate",
        "file_role": "none",
        "note": "sqlite-only; the sidecar write + ingest-on-read + sqlite-"
        "failure fallback were removed — _load_sticky fails closed, "
        "never resurrecting grants from the sidecar file",
    },
    # ── SQLite authority + generated host projection ───────────────
    "mcp_servers": {
        "category": AUTHORITY_SQL,
        "store": "mcp_registry_store",
        "file_role": ".mcp.json (generated host projection)",
        "note": "the mcp_servers SQLite table is the source of truth; "
        ".mcp.json is projected from it (project_to_file) and is "
        "regenerable from SQL alone (dashboard-mcp-config "
        "--action regenerate), not by replaying install actions. The "
        "read path (dashboard-mcp-list) is SQL-only and never imports "
        ".mcp.json; legacy import is the explicit, admin-gated "
        "migrate-control-authority command (imports once, seals)",
    },
    # ── SQLite authority + exported verbatim record ─────────────────
    "session_membership": {
        "category": AUTHORITY_SQL,
        "store": "session_membership_store",
        "via": "project_authority",
        "file_role": ".MEMORY/sessions/<id>/SESSION.md (exported record)",
        "note": "session existence/membership is the session_membership "
        "SQLite table; session_belongs / list_sessions read it (SQL-"
        "only, never scanning SESSION.md). SESSION.md is the exported "
        "verbatim record, NOT authority — deleting it does not revoke "
        "membership, and a stray/unregistered SESSION.md does not mint "
        "it. Only create_session or the explicit, admin-gated "
        "migrate-control-authority command (or the bounded commission "
        "phase) mints membership. A bare folder is still not a session.",
    },
}


def concepts(category: str | None = None) -> list[str]:
    if category is None:
        return list(CONTROL_AUTHORITY)
    return [k for k, v in CONTROL_AUTHORITY.items() if v.get("category") == category]


def validate_ledger() -> list[str]:
    """Return problems (empty = healthy): bad category, or an AUTHORITY_SQL/
    LEGACY_SHADOW concept missing a backing store/delegate.
    """
    problems: list[str] = []
    for concept, meta in CONTROL_AUTHORITY.items():
        cat = meta.get("category")
        if cat not in VALID_CATEGORIES:
            problems.append(f"{concept}: invalid category {cat!r}")
        if cat in (AUTHORITY_SQL, LEGACY_SHADOW):
            if not (meta.get("store") or meta.get("delegate")):
                problems.append(f"{concept}: {cat} has no backing store")
    return problems
