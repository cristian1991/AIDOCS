"""RFC-4 Phase 2.0 — schema migration for memory_symbol_anchors.

Extends the existing AIDOCS table with four columns required by RFC-4:

  drawer_id     — palace drawer reference (nullable; NULL for legacy rows)
  content_hash  — snapshot at anchor time (for stale detection)
  confidence    — RFC-4 §3.5 tier (exact_symbol / operator_pinned /
                                    file_anchor / semantic_guess)
  source        — extractor identity (memory_store / palace_extractor /
                                       operator_pin / ...)

Plus a partial index on drawer_id for the palace-side lookup path.

Adopt by copying to ``aidocs_mcp/anchor_schema_migration.py``.

Idempotent — safe to call on every server boot. The migration detects
already-applied state via PRAGMA table_info() and skips columns that
already exist.

See ``docs/rfcs/004-phase-2-0-anchor-audit.md`` for the design rationale.
"""

from __future__ import annotations

import sqlite3

# RFC-4 §3.5 — the only legal confidence values
VALID_CONFIDENCE = frozenset(
    {
        "exact_symbol",
        "operator_pinned",
        "file_anchor",
        "semantic_guess",
    },
)


# Sources that may produce anchors. Open vocabulary; the canonical list:
KNOWN_SOURCES = frozenset(
    {
        "memory_store",  # legacy AIDOCS memory_store / capture flow
        "palace_extractor",  # RFC-4 Phase A automatic extractor
        "operator_pin",  # explicit operator pin command
        "kg_seed",  # KG triple seeded via seed_from_entity_facts
    },
)


# Canonical post-migration schema for memory_symbol_anchors. Used by the
# self-bootstrap path when the table does not yet exist. Mirrors the base
# DDL in index_store.ensure_schema() plus the four RFC-4 Phase 2.0
# columns this migration would otherwise have to ALTER in.
#
# Doctrine (2026-05-29 — king triage, clean-VPS Gate 2b cluster A):
# Prior to this commit, ``migrate_memory_symbol_anchors`` assumed the
# base table already existed and proceeded straight to ALTER TABLE ADD
# COLUMN. PRAGMA table_info() returns an empty rowset for a missing
# table (no error), so the ``existing_cols`` set was empty, every
# column was treated as "needs adding", and the first ALTER blew up
# with ``sqlite3.OperationalError: no such table: memory_symbol_anchors``.
#
# Locally the test order happened to seed the table via index_store
# before anchor_store.ensure_schema() fired; under VPS xdist isolation
# the seeding sequence didn't run and 8 ``@managed_mode_writes`` tests
# cratered. The fix: make the migration self-bootstrap — if the table
# doesn't exist, CREATE it with the FULL post-migration schema and
# skip the ALTER block; if it DOES exist, run the legacy ALTER path
# unchanged so already-deployed databases evolve in place.
#
# Why duplicate the DDL across this module and index_store.py instead
# of refactoring to a shared constant: index_store.ensure_schema()
# creates many related tables in one ``executescript`` and includes
# FK targets that aren't yet defined at that point; collapsing the
# two paths into one source-of-truth is a structural change beyond the
# triage scope. The duplication is intentional — both copies carry the
# same column list and a comment pointing here for the audit trail.
_BOOTSTRAP_FULL_SCHEMA_DDL = """
    CREATE TABLE memory_symbol_anchors (
        anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER NOT NULL,
        symbol_name TEXT NOT NULL,
        file_path TEXT NOT NULL DEFAULT '',
        anchor_kind TEXT NOT NULL DEFAULT 'symbol'
            CHECK (anchor_kind IN ('symbol', 'file', 'module', 'domain')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        drawer_id TEXT,
        content_hash TEXT NOT NULL DEFAULT '',
        confidence TEXT NOT NULL DEFAULT 'operator_pinned',
        source TEXT NOT NULL DEFAULT 'memory_store',
        FOREIGN KEY (route_id) REFERENCES memory_routes(route_id) ON DELETE CASCADE,
        UNIQUE (route_id, symbol_name, file_path)
    )
"""


# Columns the legacy ALTER path adds. Kept in module scope so the
# self-bootstrap branch and the ALTER branch share the same list of
# "post-migration columns" — a divergence here is the bug we're guarding
# against.
_RFC4_PHASE_2_0_COLUMNS = ("drawer_id", "content_hash", "confidence", "source")


def migrate_memory_symbol_anchors(conn: sqlite3.Connection) -> dict:
    """Apply RFC-4 Phase 2.0 schema extension to memory_symbol_anchors.

    Idempotent. Returns a dict describing which columns were added vs
    already present, suitable for boot-time logging.

    Self-bootstrapping (2026-05-29): if memory_symbol_anchors does not
    yet exist on this connection, CREATE it with the full post-
    migration schema and skip the ALTER block. Pre-existing tables
    take the legacy ALTER-per-missing-column path. See the
    ``_BOOTSTRAP_FULL_SCHEMA_DDL`` doctrine block above for why.

    Notes:
      - SQLite ALTER TABLE cannot add a CHECK constraint on existing
        columns. Confidence-value validation moves to application code
        in ``validate_confidence()`` below. Every write through
        ``register_anchor`` MUST call ``validate_confidence`` first.
      - The partial index on ``drawer_id`` skips rows where it is NULL
        (legacy memory_store anchors) so the index stays compact.

    """
    table_exists = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_symbol_anchors'",
        ).fetchone()
        is not None
    )

    added: list[str] = []
    skipped: list[str] = []

    if not table_exists:
        # Self-bootstrap path: empty db, no prior table — CREATE with
        # the full post-migration schema. Every RFC-4 Phase 2.0 column
        # is "added" in this branch from the caller's perspective.
        conn.execute(_BOOTSTRAP_FULL_SCHEMA_DDL)
        added.extend(_RFC4_PHASE_2_0_COLUMNS)
    else:
        existing_cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(memory_symbol_anchors)",
            ).fetchall()
        }

        def _add(name: str, ddl: str) -> None:
            if name in existing_cols:
                skipped.append(name)
            else:
                conn.execute(f"ALTER TABLE memory_symbol_anchors ADD COLUMN {ddl}")
                added.append(name)

        _add("drawer_id", "drawer_id TEXT")
        _add("content_hash", "content_hash TEXT NOT NULL DEFAULT ''")
        _add(
            "confidence",
            "confidence TEXT NOT NULL DEFAULT 'operator_pinned'",
        )
        _add("source", "source TEXT NOT NULL DEFAULT 'memory_store'")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_symbol_anchors_drawer "
        "ON memory_symbol_anchors(drawer_id) WHERE drawer_id IS NOT NULL",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_symbol_anchors_confidence "
        "ON memory_symbol_anchors(confidence) "
        "WHERE confidence IN ('exact_symbol', 'operator_pinned')",
    )

    conn.commit()
    return {
        "added_columns": added,
        "skipped_columns": skipped,
        "already_applied": len(added) == 0,
    }


def validate_confidence(value: str) -> str:
    """Application-level CHECK enforcement for the ``confidence`` field.

    SQLite ALTER TABLE cannot add CHECK constraints on existing
    columns; this function is the equivalent enforcement at write
    time. Every caller writing to memory_symbol_anchors MUST validate
    the confidence value through here.
    """
    if value not in VALID_CONFIDENCE:
        raise ValueError(
            f"invalid anchor confidence {value!r}; must be one of {sorted(VALID_CONFIDENCE)}",
        )
    return value


def validate_source(value: str) -> str:
    """Soft validation for ``source``. Unknown sources are allowed
    (open vocabulary) but flagged with a warning. Known sources pass
    silently.
    """
    if value not in KNOWN_SOURCES:
        import logging

        logging.getLogger(__name__).warning(
            "anchor source %r is not in the known set %s; "
            "writing anyway, but consider standardizing the source name",
            value,
            sorted(KNOWN_SOURCES),
        )
    return value
