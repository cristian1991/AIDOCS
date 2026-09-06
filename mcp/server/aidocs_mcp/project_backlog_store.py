"""project_backlog — project-owned durable future-work inventory.

ai_backlog is project-owned, not session-owned. session_id is metadata
(`created_in_session_id`), never ownership key. source_task_id +
promoted_from_todo_id + linked_task_id are nullable relation fields for
future promote/link operations (schema-ready; tool surface deferred).

Status semantics (canonical, do not drift):
  open         — active, not yet worked
  in_progress  — currently being worked
  blocked      — can't proceed, waiting on something
  done         — work completed successfully
  rejected     — CONSIDERED and intentionally DECLINED as work.
                 Product/engineering judgment: "we decided not to do this."
                 Has decision value; can be reviewed later.
  removed      — administratively HIDDEN/tombstoned from active lists
                 WITHOUT implying product judgment. Uses: duplicates,
                 accidental adds, stale items, cleanup. Audit-preserving.
  merged       — absorbed into an umbrella item (#450). merged_into
                 points at the surviving id; the row is KEPT (never
                 removed) and reversible: update(status='open') clears
                 merged_into. Hidden from list by default
                 (include_merged=True shows).

Tombstone model: remove transitions status → 'removed', never DELETE.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. Every site below was
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- and none of them
# set a single pragma, so this store ran with foreign_keys OFF (its FKs
# inert), no busy_timeout, and the default synchronous=FULL fsync tax.
# DURABILITY: RUNTIME (the helper's default). This table is a DERIVED
# projection: "the event log is the source of truth -- EVERY backlog write
# path feeds it here" (_emit_backlog), and rebuild_from_events rebuilds
# sqlite from it wholesale. A commit lost to a power cut is re-folded, and
# a backlog item is a work item rather than a grant -- losing one hands
# nobody an authority back. The durable half of this write path is
# backlog_write_queue, which is AUDIT for exactly that reason.
from ._sqlite_connect import connect as _canonical_connect

_STATUSES = {"open", "in_progress", "done", "blocked", "rejected", "removed", "merged"}
# #101 urgency tiers (Empire directive 2026-05-01): 'urgent' sits between
# critical and high ("operationally bleeding now"); 'medium' renamed to
# 'normal'. 'medium' remains an INPUT alias (coerced) so old callers and
# the dashboard embed keep working; stored rows use one name.
_PRIORITIES = {"critical", "urgent", "high", "normal", "low", "idea"}
_PRIORITY_ALIASES = {"medium": "normal"}

# Canonical severity order, highest first — the ONE list consumers iterate
# (roadmap bands, dashboards). Derive from this; never hardcode a copy:
# the pre-#101 hardcoded copy in roadmap_layer_progress silently DROPPED
# items in unknown bands from progress.
PRIORITY_ORDER: tuple[str, ...] = ("critical", "urgent", "high", "normal", "low", "idea")


def _canon_priority(priority: str) -> str:
    return _PRIORITY_ALIASES.get(priority, priority)

# #573 `kind` — WHAT KIND OF MONSTER an item is, orthogonal to priority (HOW
# MUCH it matters). The triage grid is severity x kind; the actionable quadrant
# is high-severity x known-fix, which priority alone cannot separate (measured:
# #569's root cause was four commands away while #568 ate hours — both
# critical). Constrained on purpose: free text drifts into synonyms within a
# week and becomes unfilterable.
#   known-fix    cause identified, remedy named, mechanical
#   wire-up      machinery exists, needs connecting
#   design       needs a decision before any code
#   investigate  cause unknown — the work IS finding it
#   research     may not be solvable as scoped
_KINDS = {"known-fix", "wire-up", "design", "investigate", "research"}

# Canonical order, cheapest-to-act-on first — the ONE list consumers iterate.
KIND_ORDER: tuple[str, ...] = ("known-fix", "wire-up", "design", "investigate", "research")

# UNSET is the stored default and is NOT a kind. An item nobody rated must stay
# distinguishable from one deliberately marked `investigate`; defaulting to a
# real kind would manufacture false confidence. Empty string, matching the
# title/global_id NOT NULL DEFAULT '' convention already in this table.
KIND_UNSET = ""
# Filter-only pseudo-value: selects the UNRATED rows (the backfill worklist).
# Never storable — validate_kind() refuses it as a write value.
KIND_FILTER_UNSET = "unset"


def validate_kind(kind: str) -> str | None:
    """Return an error string when `kind` is not a storable kind, else None.

    Rejecting beats coercing: a stored synonym is invisible to kind_filter
    forever, and a silently-defaulted kind is a false rating.
    """
    if kind in _KINDS:
        return None
    return (
        f"kind {kind!r} not in {sorted(_KINDS)}. "
        f"Kind is READ OFF the item, never estimated — leave it unset rather "
        f"than guessing. Defaulting would hide the bug."
    )

# `difficulty` — HOW DEEPLY an item must be DECOMPOSED, as a BOUNDED INTEGER.
#
# THE THIRD TRIAGE AXIS, and the one the other two cannot express: priority says
# how much an item matters, kind (#573) says what kind of monster it is, and
# neither says HOW MANY AGENTS IT TAKES TO KILL.
#
# 1. READ IT AS DECOMPOSITION DEPTH, NEVER AS WALL-CLOCK TIME. The stated
#    downstream use is gating how deep a workflow tree may reach — one agent, a
#    captain with workers, or a conductor spawning sub-conductors. An item is
#    hard when it must be SPLIT, not when it is merely long. A thousand
#    mechanical line edits is a 1 done many times; a two-hour problem that
#    cannot be started until it is carved into four independent fronts is a 4.
#    Fill this column with hour estimates and the tree-depth use silently
#    breaks, because duration and splittability are uncorrelated.
#
# 2. IT IS A MEASUREMENT, NOT THE PERMITTED DEPTH. Tempting and self-
#    documenting to define 4 as "four levels of tree" — and wrong. If the
#    number IS the policy, every change to the policy forces a RE-RATING OF
#    EVERY ITEM (182 and rising). Keep `max_depth = f(difficulty)` so `f` can
#    change alone. Nothing in this module computes `f`; the operator has not
#    decided it yet, and NO enforcement is wired to this column.
#
# 3. AN INTEGER, so it can be COMPARED. "What needs decomposing?" is
#    `difficulty >= 4` — one range filter, no lookup table. A t-shirt label
#    would have needed a label-to-number map to answer the same question.
#
# 4. BOUNDED AND ANCHORED. A bare unbounded integer has the opposite failure of
#    an enum: it accepts anything, and if nothing distinguishes a 6 from a 7 the
#    ratings drift until the column means nothing. So: a closed range, and every
#    rung anchored to a DISTINGUISHABLE ORCHESTRATION SHAPE. Two adjacent rungs
#    that fail that test would mean the scale is too fine.
DIFFICULTY_MIN = 1
DIFFICULTY_MAX = 5

# The anchors ARE the scale — a rung without one is a number people guess at.
# Ascending by decomposition depth; each step is a different orchestration
# SHAPE, not a longer version of the step below it.
DIFFICULTY_ANCHORS: dict[int, str] = {
    1: "one edit, no decomposition - inline, no agent needed",
    2: "one agent, one pass; no plan worth writing",
    3: (
        "one agent over several passes, OR a captain with a couple of workers "
        "- the first rung where splitting is a real option"
    ),
    4: (
        "MUST be decomposed: a captain with a real worker fleet, because no "
        "single agent holds the whole thing in context"
    ),
    5: "needs sub-conductors; spans items and probably sessions",
}

# Canonical order, SHALLOWEST FIRST — the ONE list consumers iterate. Order is
# part of the truth (as with KIND_ORDER): a depth policy reads the POSITION on
# the ladder, which is precisely why the stored value is the position.
DIFFICULTY_ORDER: tuple[int, ...] = tuple(range(DIFFICULTY_MIN, DIFFICULTY_MAX + 1))

# UNSET is the stored default and is NOT a rating. NULL, not 0: an item nobody
# rated must stay distinguishable from one deliberately rated, and a guessed
# default would silently authorise (or deny) a tree depth nobody chose. (kind
# uses '' for the same reason — TEXT there, INTEGER here, same semantics.)
DIFFICULTY_UNSET: None = None
# Filter-only pseudo-value: selects the UNRATED rows (the backfill worklist).
# Never storable — validate_difficulty() refuses it as a write value.
DIFFICULTY_FILTER_UNSET = "unset"


def _difficulty_scale_text() -> str:
    return "; ".join(f"{rung} = {DIFFICULTY_ANCHORS[rung]}" for rung in DIFFICULTY_ORDER)


def validate_difficulty(difficulty: Any) -> str | None:
    """Return an error string when `difficulty` is not storable, else None.

    Same posture as validate_kind, on purpose — one way of doing this, not two.
    REJECT, NEVER COERCE: no clamping an out-of-range number to the nearest
    rung, no int() on a string that happens to parse, no rounding a float. A
    coerced difficulty is a rating nobody made, and the stated downstream use
    (how deep a workflow tree may reach) would then be decided by a guess.

    bool is excluded explicitly — it is an int subclass in Python, so True would
    otherwise sail through as a 1.

    The message NAMES THE WHOLE SCALE so a caller learns it from the error
    instead of going to read this file.
    """
    if isinstance(difficulty, bool) or not isinstance(difficulty, int):
        return (
            f"difficulty {difficulty!r} must be an INTEGER "
            f"{DIFFICULTY_MIN}..{DIFFICULTY_MAX} ({_difficulty_scale_text()}). "
            f"Not parsed, not rounded, not coerced — pass the number or leave "
            f"it unset."
        )
    if difficulty in DIFFICULTY_ORDER:
        return None
    return (
        f"difficulty {difficulty!r} out of range "
        f"{DIFFICULTY_MIN}..{DIFFICULTY_MAX} ({_difficulty_scale_text()}). "
        f"Difficulty is DECOMPOSITION DEPTH (how many agents it takes to kill), "
        f"NOT a time estimate and NOT the permitted tree depth — leave it unset "
        f"rather than guessing. Defaulting would hide the bug."
    )


def validate_status_for_add(status: str) -> str | None:
    """Return an error string when `status` is not acceptable on add(), else None.

    #818 clause 2: `add()` used to accept a `status` kwarg and silently DROP
    it — the INSERT hardcoded 'open' regardless of what was passed. #835 was
    filed with status='done', landed 'open', and was reported to the operator
    as closed; it sat open for ~4 hours behind a false receipt.

    THE TRAP: making add() honour an arbitrary status would let an item be
    filed 'done' with NO EVIDENCE TRAIL and NO VERIFICATION — worse than the
    silent discard, because today's failure at least leaves the item open
    where an audit finds it. #601 already drew this line (recording is not
    gated, but STATE CHANGES need evidence): filing is recording, declaring
    something done is a state change.

    So add() REFUSES any status other than the implicit 'open'. status='open'
    passed explicitly is accepted — it matches reality (every new item IS
    open) and refusing it would be pedantic. The refusal NAMES mode='update'
    (law 311bf3e6: a named remedy must be reachable), because that is where a
    status change belongs — it is the surface that carries `reason`.
    """
    if status == "open":
        return None
    return (
        f"status={status!r} is not settable on add() — a new item always "
        f"lands 'open', because that is the only status a brand-new record "
        f"can honestly carry. Declaring a different status is a STATE "
        f"CHANGE, and state changes need evidence, not a filing default. "
        f"File it first, then use "
        f"ai_backlog(mode='update', id=<the new item's id>, "
        f"status={status!r}, reason='<why>') to make that change with a "
        f"reason attached."
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Additive column migration, safe under CONCURRENT migrators.

    The PRAGMA check alone is not enough: other agents open and migrate this
    SAME db concurrently, so two processes can both see the column missing and
    both issue the ADD. The loser surfaced "duplicate column name" out of
    init_db and failed an unrelated caller's read (2026-07-28). Losing the race
    IS success — the column exists either way. Any OTHER OperationalError still
    raises. Mirrors CodeIndexStore._ensure_column.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


# KISS title extraction (#59): real stored field, derived once at
# add() time, never re-parsed at list() time. First markdown heading
# (# / ## / ###) wins; falls back to first non-blank line; then to a
# trimmed prefix of the body. Hard cap at 160 chars so list-default
# rows stay lean.
_TITLE_MAX = 160


def _extract_title(content: str) -> str:
    if not content:
        return ""
    body = content.strip()
    if not body:
        return ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Markdown heading: strip leading #'s and any trailing #'s.
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().rstrip("#").strip()
            if heading:
                return heading[:_TITLE_MAX]
        # First non-blank, non-heading line is the fallback.
        return stripped[:_TITLE_MAX]
    return body[:_TITLE_MAX]


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(db, row_factory=False) as conn:
        # #242 recovery guard: a rebuild killed between DROP and RENAME
        # strands every row in project_backlog__new — and the CREATE IF NOT
        # EXISTS below would quietly build an EMPTY table over the evidence
        # (the 2026-07-04 incident). Complete the rename FIRST.
        from .store_migrations import recover_interrupted_rename

        recover_interrupted_rename(
            conn,
            "project_backlog",
            finish_statements=[
                ("CREATE INDEX IF NOT EXISTS idx_project_backlog_status "
                "ON project_backlog(status, priority)"),
                ("CREATE INDEX IF NOT EXISTS idx_project_backlog_linked "
                "ON project_backlog(linked_task_id)"),
            ],
            guard_views=["canonical_rows"],
        )
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS project_backlog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN (
                        'open','in_progress','done','blocked','rejected','removed','merged'
                    )),
                priority TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN (
                        'critical','urgent','high','normal','low','idea'
                    )),
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_in_session_id TEXT,
                source_task_id TEXT,
                promoted_from_todo_id INTEGER,
                linked_task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                removed_at TEXT,
                removed_reason TEXT,
                merged_into INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_project_backlog_status
                ON project_backlog(status, priority);
            CREATE INDEX IF NOT EXISTS idx_project_backlog_linked
                ON project_backlog(linked_task_id);
        """)
        # Migration 2026-07-01 (#101): pre-urgency tables carry a CHECK
        # without 'urgent' — SQLite cannot ALTER a CHECK, so rebuild the
        # table once (copy-first, §XII): new schema, rows copied with
        # medium→normal, indexes recreated. Idempotent: the rebuilt
        # table's sql contains 'urgent', so this never runs twice.
        master = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='project_backlog'",
        ).fetchone()
        if master and "'urgent'" not in (master[0] or ""):
            old_cols = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(project_backlog)",
                ).fetchall()
            ]
            # #242: the whole rebuild runs atomically — the old shape
            # (executescript for CREATE, then DROP;RENAME in a second
            # executescript, each statement autocommitting) had a kill
            # window between DROP and RENAME that ate the table on
            # 2026-07-04. atomic_rebuild = ONE BEGIN IMMEDIATE…COMMIT.
            from .store_migrations import atomic_rebuild

            new_table_sql = """
                CREATE TABLE project_backlog__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN (
                            'open','in_progress','done','blocked','rejected','removed'
                        )),
                    priority TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN (
                            'critical','urgent','high','normal','low','idea'
                        )),
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_in_session_id TEXT,
                    source_task_id TEXT,
                    promoted_from_todo_id INTEGER,
                    linked_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    removed_at TEXT,
                    removed_reason TEXT,
                    title TEXT NOT NULL DEFAULT ''
                )
            """
            new_cols = {
                "id", "content", "status", "priority", "tags_json",
                "created_in_session_id", "source_task_id", "promoted_from_todo_id",
                "linked_task_id", "created_at", "updated_at", "completed_at",
                "removed_at", "removed_reason", "title",
            }
            common = [c for c in old_cols if c in new_cols and c != "priority"]
            col_list = ", ".join(common)
            atomic_rebuild(
                conn,
                [
                    new_table_sql,
                    (f"INSERT INTO project_backlog__new ({col_list}, priority) "
                    f"SELECT {col_list}, "
                    f"CASE priority WHEN 'medium' THEN 'normal' ELSE priority END "
                    f"FROM project_backlog"),
                    "DROP TABLE project_backlog",
                    "ALTER TABLE project_backlog__new RENAME TO project_backlog",
                    ("CREATE INDEX IF NOT EXISTS idx_project_backlog_status "
                    "ON project_backlog(status, priority)"),
                    ("CREATE INDEX IF NOT EXISTS idx_project_backlog_linked "
                    "ON project_backlog(linked_task_id)"),
                ],
                guard_views=["canonical_rows"],
            )
        # Migration 2026-04-26: add title column (KISS list defaults
        # use it as primary identifier; derived from content at add()
        # time, backfilled here for pre-migration rows). Idempotent.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(project_backlog)").fetchall()}
        if "title" not in cols:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT id, content FROM project_backlog WHERE title = ''",
            ).fetchall():
                derived = _extract_title(row["content"] or "")
                conn.execute(
                    "UPDATE project_backlog SET title = ? WHERE id = ?",
                    (derived, row["id"]),
                )
            conn.row_factory = None
        # Migration 2026-07-07: global_id (uuid) — the STABLE cross-agent entity
        # id for event-sourced sync. The local autoincrement 'id' collides across
        # agent clones (a fresh gate clone restarts the counter), so events key on
        # global_id. Backfilled for existing rows. Idempotent.
        if "global_id" not in cols:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN global_id TEXT NOT NULL DEFAULT ''")
            conn.row_factory = sqlite3.Row
            for _r in conn.execute("SELECT id FROM project_backlog WHERE global_id = ''").fetchall():
                conn.execute(
                    "UPDATE project_backlog SET global_id = ? WHERE id = ?",
                    (uuid.uuid4().hex, _r["id"]),
                )
            conn.row_factory = None
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_backlog_global_id "
                "ON project_backlog(global_id)"
            )
        # Migration 2026-07-18 (#450): 'merged' status + merged_into column.
        # SQLite cannot ALTER a CHECK, so tables whose CHECK predates 'merged'
        # are rebuilt once (atomic, copy-first — §XII/#242), PRESERVING every
        # live column (title + global_id included — this runs AFTER their
        # backfills so the copy carries them). Idempotent: the rebuilt table's
        # sql contains 'merged', so this never runs twice.
        master_m = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='project_backlog'",
        ).fetchone()
        if master_m and "'merged'" not in (master_m[0] or ""):
            from .store_migrations import atomic_rebuild

            old_cols_m = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(project_backlog)",
                ).fetchall()
            ]
            merged_table_sql = """
                CREATE TABLE project_backlog__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN (
                            'open','in_progress','done','blocked','rejected','removed','merged'
                        )),
                    priority TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN (
                            'critical','urgent','high','normal','low','idea'
                        )),
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_in_session_id TEXT,
                    source_task_id TEXT,
                    promoted_from_todo_id INTEGER,
                    linked_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    removed_at TEXT,
                    removed_reason TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    global_id TEXT NOT NULL DEFAULT '',
                    merged_into INTEGER
                )
            """
            merged_new_cols = {
                "id", "content", "status", "priority", "tags_json",
                "created_in_session_id", "source_task_id", "promoted_from_todo_id",
                "linked_task_id", "created_at", "updated_at", "completed_at",
                "removed_at", "removed_reason", "title", "global_id", "merged_into",
            }
            common_m = [c for c in old_cols_m if c in merged_new_cols]
            col_list_m = ", ".join(common_m)
            atomic_rebuild(
                conn,
                [
                    merged_table_sql,
                    (f"INSERT INTO project_backlog__new ({col_list_m}) "
                    f"SELECT {col_list_m} FROM project_backlog"),
                    "DROP TABLE project_backlog",
                    "ALTER TABLE project_backlog__new RENAME TO project_backlog",
                    ("CREATE INDEX IF NOT EXISTS idx_project_backlog_status "
                    "ON project_backlog(status, priority)"),
                    ("CREATE INDEX IF NOT EXISTS idx_project_backlog_linked "
                    "ON project_backlog(linked_task_id)"),
                    ("CREATE UNIQUE INDEX IF NOT EXISTS idx_project_backlog_global_id "
                    "ON project_backlog(global_id)"),
                ],
                guard_views=["canonical_rows"],
            )
        # Additive guard: merged_into on tables whose CHECK already carries
        # 'merged' (fresh CREATE path) but predate the column. Cheap ALTER.
        cols_m = {
            row[1] for row in conn.execute("PRAGMA table_info(project_backlog)").fetchall()
        }
        if "merged_into" not in cols_m:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN merged_into INTEGER")
        # Migration 2026-07-20 (id-divergence fix): display_id — the CONVERGENT
        # human-facing "#N". The autoincrement `id` is a per-store rowid that
        # differs across clones (VPS #769 != local #769); display_id is a pure
        # dense-rank projection of the authoritative event log (creation-HLC
        # order), so every store with the same events computes the same number.
        # Nullable + derived: (re)assigned by _reassign_display_ids on every
        # hydrate/add/update, never authoritative (global_id remains the stable
        # key). NOT unique-indexed (a rank is derived, transient during sync).
        if "display_id" not in cols_m:
            conn.execute("ALTER TABLE project_backlog ADD COLUMN display_id INTEGER")
        # Migration 2026-07-28 (#573): `kind` — what kind of monster the item is
        # (known-fix / wire-up / design / investigate / research), orthogonal to
        # priority. Additive, race-safe (_ensure_column swallows ONLY the
        # concurrent-migrator "duplicate column name"). NO CHECK constraint: the
        # value set is enforced in validate_kind() so a rejection is an
        # actionable message naming the legal values, not an opaque
        # IntegrityError — and so the set can widen without a table rebuild.
        # DEFAULT '' = UNSET, deliberately not a guessed kind.
        _ensure_column(conn, "project_backlog", "kind", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_backlog_kind "
            "ON project_backlog(kind, priority)"
        )
        # Migration 2026-08-01: `difficulty` — decomposition depth as a bounded
        # INTEGER (see the block above validate_difficulty). SAME MECHANISM AS
        # #573's kind, deliberately: one _ensure_column ALTER, additive,
        # race-safe (it swallows ONLY the concurrent-migrator "duplicate column
        # name"), and re-runnable. It NEVER rewrites an existing row and never
        # rebuilds the table — a sibling agent writes this store continuously,
        # and a copy-first rebuild would race that writer and lose its work.
        # NULLABLE with no DEFAULT: NULL = UNSET, deliberately not a guessed
        # rating. NO CHECK constraint — the range is enforced in
        # validate_difficulty() so a rejection is an actionable message naming
        # the scale, not an opaque IntegrityError, and so the ladder can be
        # re-anchored without a table rebuild.
        _ensure_column(conn, "project_backlog", "difficulty", "INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_backlog_difficulty "
            "ON project_backlog(difficulty, priority)"
        )
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    tags = []
    try:
        parsed = json.loads(row["tags_json"] or "[]")
        if isinstance(parsed, list):
            tags = [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    # title column is post-migration. Older rows may not surface it
    # via row.keys() if a stale connection reads pre-ALTER schema; fall
    # back to a freshly-derived title in that defensive case so list
    # never returns an empty title field for an indexed item.
    try:
        title = row["title"] or ""
    except (KeyError, IndexError):
        title = ""
    if not title:
        title = _extract_title(row["content"] or "")
    try:
        global_id = row["global_id"] or ""
    except (KeyError, IndexError):
        global_id = ""
    try:
        merged_into = row["merged_into"]
    except (KeyError, IndexError):
        merged_into = None
    try:
        display_id = row["display_id"]
    except (KeyError, IndexError):
        display_id = None
    try:
        kind = row["kind"] or KIND_UNSET
    except (KeyError, IndexError):
        # Pre-migration row read through a stale connection (#573) — unset, and
        # never a guessed kind.
        kind = KIND_UNSET
    try:
        _raw_difficulty = row["difficulty"]
    except (KeyError, IndexError):
        # Pre-migration row read through a stale connection — unset, and never
        # a guessed rating.
        _raw_difficulty = None
    # A stored value is either a legal rung or nothing. Anything else (an old
    # hand-written row, a bad sync payload) reads back as UNSET rather than as
    # a rating nobody made — the read side coerces nothing either.
    difficulty = (
        int(_raw_difficulty)
        if isinstance(_raw_difficulty, int)
        and not isinstance(_raw_difficulty, bool)
        and _raw_difficulty in DIFFICULTY_ORDER
        else DIFFICULTY_UNSET
    )
    _display = display_id if display_id is not None else row["id"]
    return {
        "merged_into": merged_into,
        # "id" is the CONVERGENT display_id (#N) — identical across stores for the
        # same global_id. update()/remove() resolve it via display_id→rowid, and
        # the write keys on global_id, so referencing #N is always correct even
        # through a renumber. "rowid" is the internal per-store key (not shown to
        # humans); "global_id" is the durable cross-machine identity.
        "id": _display,
        "display_id": _display,
        "rowid": row["id"],
        "global_id": global_id,
        "title": title,
        "content": row["content"],
        "status": row["status"],
        "priority": row["priority"],
        "kind": kind,  # #573: '' = UNSET (unrated), never a guessed kind
        # Decomposition depth 1..5; None = UNSET, never a guessed rating.
        "difficulty": difficulty,
        "tags": tags,
        "created_in_session_id": row["created_in_session_id"],
        "source_task_id": row["source_task_id"],
        "promoted_from_todo_id": row["promoted_from_todo_id"],
        "linked_task_id": row["linked_task_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "removed_at": row["removed_at"],
        "removed_reason": row["removed_reason"],
    }


def _resolve_rowid(conn: sqlite3.Connection, backlog_id: int) -> int | None:
    """THE single source of id truth for this store (#580). Caller id → rowid.

    What a caller calls `id` is the CONVERGENT display_id (#N) — a dense rank
    recomputed from the authoritative event log by _reassign_display_ids.
    sqlite's `id` column is the per-store rowid. The two coincide ONLY in a
    fresh store; after any renumber they diverge, and #580's entire defect was
    that `_merge_locked` resolved the caller's number as a RAW ROWID while
    get_by_id/_update_locked/_remove_locked resolved it as a display_id. Two
    readers, two DIFFERENT ROWS, each internally consistent — so merge read and
    wrote rows nobody had asked about. Every id resolution now goes through
    here, so there is exactly one answer.

    Legacy fallback: a row predating the display_id column (still NULL) stays
    addressable by its rowid. That fallback is NARROW BY CONSTRUCTION: it
    matches ONLY rows whose display_id IS NULL. #597 measured what an
    UNCONDITIONAL `WHERE id = ?` fallback does — `get(id=769)` on a store whose
    highest display id is far below 769 returned item #277 with ok:true,
    because rowid 769 happened to be occupied. Display ids are a dense rank
    recomputed from the event log; rowids are insertion order. The two spaces
    overlap at the low end and diverge upward, so an unconditional fallback
    turns every stale citation above the display maximum into a WRONG-ROW hit —
    and since #580 single-sourced update/remove/merge through here, a wrong-row
    WRITE that still reports ok. Restricting the fallback to `display_id IS
    NULL` keeps the one case it exists for and closes the silent-equivalence
    window between the two id spaces.

    Returns None when NEITHER resolves — the caller must then REFUSE. An
    unresolvable id is never defaulted onto another row (#580 fence: "unknown
    is not a pass"; #597 made that fence reach the ids a rowid occupies, which
    is the range where it had never engaged).
    """
    for sql in (
        "SELECT id FROM project_backlog WHERE display_id = ?",
        # #597: the legacy escape hatch, and ONLY that. Never a generic rowid
        # lookup — an id in neither space must come back unknown.
        "SELECT id FROM project_backlog WHERE id = ? AND display_id IS NULL",
    ):
        res = conn.execute(sql, (int(backlog_id),)).fetchone()
        if res is not None:
            try:
                return int(res["id"])
            except (TypeError, IndexError, KeyError):
                return int(res[0])
    return None


def _rewrite_absorbed_line(
    body: str,
    display_id: int,
    replacement: str | None,
) -> tuple[str, bool]:
    """Rewrite the '- #<display_id> …' line in an umbrella's '## Absorbed'
    ledger. Returns (new_body, matched).

    HISTORY AND TRUTH (#580). When an item is re-pointed to another umbrella or
    unmerged, the old umbrella's ledger keeps asserting a relation that no
    longer exists. Deleting the line would erase the fact that it once did.
    So the line is REWRITTEN IN PLACE — it still names the item, and now says
    what actually happened to it. replacement=None deletes (kept so the one
    line-owning helper covers both shapes).
    """
    prefix = f"- #{display_id} "
    bare = f"- #{display_id}"
    out: list[str] = []
    matched = False
    for line in body.split("\n"):
        stripped = line.rstrip()
        if stripped == bare or stripped.startswith(prefix):
            matched = True
            if replacement is not None:
                out.append(replacement)
            continue
        out.append(line)
    return "\n".join(out), matched


# ── #503: a mutation's (sqlite commit → event emit) pair is NOT atomic ────────
# Between the commit and the emit the row and the event log are TORN: sqlite
# already says 'done', the log still says 'in_progress'. A fold-on-read landing
# in that window folds the PRE-WRITE log and _materialize_backlog patches that
# older state back over the row it just missed — the observed #503 revert (status
# rolled back to in_progress while completed_at, absent from the older fold,
# survived, and the outcome section never appeared).
#
# The fix makes the pair atomic WITH RESPECT TO FOLDS, without touching the #376
# receipt doctrine (a receipt must still attest to an already-committed local
# mutation, so the emit stays AFTER the commit):
#   * mutations run inside _mutating() and hold _MUTATION_LOCK for the whole
#     commit+emit span, so a hydrate on ANOTHER thread waits out the window
#     instead of folding a torn state;
#   * a hydrate re-entered on the SAME thread (a mutation-path callback that
#     reads) cannot wait for a lock it already holds, so it DEFERS — see
#     hydrate_from_events / sync_store.HYDRATE_DEFERRED. The fold-on-read
#     watermark does not advance on a deferral, so the next read still folds.
# Cost to reads: one uncontended RLock acquire. Fold-on-read stays as it was.
_MUTATION_LOCK = threading.RLock()
_MUTATION_DEPTH = threading.local()


@contextlib.contextmanager
def _mutating():
    """Hold the backlog mutation lock across a write's commit+emit span (#503)."""
    with _MUTATION_LOCK:
        _MUTATION_DEPTH.n = getattr(_MUTATION_DEPTH, "n", 0) + 1
        try:
            yield
        finally:
            _MUTATION_DEPTH.n = getattr(_MUTATION_DEPTH, "n", 1) - 1


def _mutation_in_flight() -> bool:
    """True when THIS thread is inside a backlog mutation (#503)."""
    return getattr(_MUTATION_DEPTH, "n", 0) > 0


def _emit_backlog(project_root: Path, global_id: str, op: str, fields: dict, *, session_id: str = "") -> None:
    """Store-layer emit for the backlog stream (Phase 1). Best-effort; never
    raises. The event log is the source of truth — EVERY backlog write path feeds
    it here, so tools/dashboard/migrations/imports are all covered by law."""
    try:
        from . import sync_store

        sync_store.emit(project_root, "backlog", global_id, op, fields, session_id=session_id)
    except Exception:
        pass

    # P1: a BOUND project also QUEUES the mutation as an intent for the
    # authoritative hub (operator ruling: offline writes queue, not refused).
    # The drain submits it on the next sync cycle; the server adjudicates, and a
    # genuine collision returns as a SURFACED conflict rather than a silent
    # overwrite.
    #
    # The BASE is the server updatedAt we last observed for this item — the
    # intent means "I changed this based on what I last saw from the server",
    # which is exactly what optimistic concurrency must check. Never seen (a
    # fresh item) ⇒ "" ⇒ no base check.
    #
    # UNBOUND/local-only projects skip this entirely and stay byte-identical to
    # before. Best-effort throughout: queuing must never fail a local write that
    # has already committed.
    try:
        from . import backlog_hub_client as _hub

        if _hub.is_bound(project_root):
            from . import backlog_write_queue as _queue

            _org, _pid = _hub.binding(project_root)
            _queue.enqueue(
                project_root,
                global_id=global_id,
                project_id=_pid,
                op=op,
                fields=fields,
                base_updated_at=_hub.cached_updated_at(project_root, global_id),
            )
    except Exception:
        pass


def add(
    project_root: Path,
    *,
    content: str,
    priority: str = "normal",
    kind: str | None = None,
    difficulty: int | None = None,
    tags: list[str] | None = None,
    created_in_session_id: str | None = None,
    source_task_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    # #818 clause 2: status is OPTIONAL and, when passed, is validated FIRST
    # — before priority/kind/difficulty and well before the INSERT. None or
    # '' both mean "not passed" (the tool layer's sentinel-empty-string
    # convention, matching kind's `kind=(kind or None)`). Anything else must
    # be exactly 'open' or the call is refused. See validate_status_for_add.
    if status:
        status_err = validate_status_for_add(status)
        if status_err is not None:
            return {"ok": False, "error": status_err}
    priority = _canon_priority(priority)
    if priority not in _PRIORITIES:
        return {
            "ok": False,
            "error": (
                f"priority {priority!r} not in "
                f"{sorted(_PRIORITIES)}. Defaulting would hide the bug."
            ),
        }
    # #573: kind is OPTIONAL and defaults to UNSET. Passing an unknown value is
    # an error, not a coercion — see validate_kind.
    if kind is None:
        kind = KIND_UNSET
    else:
        err = validate_kind(kind)
        if err is not None:
            return {"ok": False, "error": err}
    # difficulty is OPTIONAL and defaults to UNSET (NULL). Same posture as kind:
    # an out-of-range or wrong-typed value is an error, never a clamp or a
    # parse — see validate_difficulty.
    if difficulty is not None:
        derr = validate_difficulty(difficulty)
        if derr is not None:
            return {"ok": False, "error": derr}
    # #684 SINK SCREEN: a backlog body is agent prose that never becomes a
    # file the write guard or the pre-commit scanner sees. Repair (never
    # refuse) — signature-only, so Romanian/Italian diacritics pass untouched.
    from .agent_prose_screen import repair_agent_prose

    content = repair_agent_prose(content, sink="backlog_body")
    init_db(project_root)
    now = _now()
    tags_json = json.dumps(tags or [])
    title = _extract_title(content)
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        gid = uuid.uuid4().hex
        cur = conn.execute(
            "INSERT INTO project_backlog "
            "(content, title, status, priority, kind, difficulty, tags_json, "
            " created_in_session_id, source_task_id, "
            " created_at, updated_at, global_id) "
            "VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content, title, priority, kind, difficulty, tags_json,
             created_in_session_id,
             source_task_id, now, now, gid),
        )
        conn.commit()
        new_id = cur.lastrowid
    _emit_backlog(
        project_root, gid, "add",
        {
            "content": content, "title": title, "status": "open", "priority": priority,
            "kind": kind,
            "difficulty": difficulty,
            "tags": tags or [], "created_at": now, "updated_at": now,
            "created_in_session_id": created_in_session_id, "source_task_id": source_task_id,
        },
        session_id=created_in_session_id or "",
    )
    _reassign_display_ids(project_root)  # convergent #N now includes the new item
    _notify_autosync(project_root)  # debounced push to the gate (fail-open)
    display_id = new_id
    try:
        with _canonical_connect(_db_path(project_root)) as _c:
            _c.row_factory = sqlite3.Row
            _r = _c.execute(
                "SELECT display_id FROM project_backlog WHERE global_id = ?", (gid,)
            ).fetchone()
            if _r is not None and _r["display_id"] is not None:
                display_id = _r["display_id"]
    except Exception:
        pass
    return {
        "ok": True,
        "id": display_id,  # convergent #N (see _row_to_dict)
        "display_id": display_id,
        "rowid": new_id,
        "global_id": gid,
        "content": content,
        "title": title,
        "status": "open",
        "priority": priority,
        "kind": kind,
        "difficulty": difficulty,
        "tags": tags or [],
        "created_at": now,
    }


def list_backlog(
    project_root: Path,
    *,
    status: str | None = None,
    priority: str | None = None,
    kind_filter: str | None = None,
    difficulty_filter: Any = None,
    difficulty_min: int | None = None,
    difficulty_max: int | None = None,
    tag_filter: str | None = None,
    tags: list[str] | None = None,
    include_removed: bool = False,
    include_merged: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    init_db(project_root)
    try:
        from . import sync_store

        sync_store.maybe_hydrate(project_root, "backlog", hydrate_from_events)  # fold-on-read
    except Exception:
        pass
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    else:
        # Default view hides tombstoned (removed) and absorbed (merged, #450)
        # rows; each returns via its explicit include_* flag or a direct
        # status= filter.
        #
        # #580: ABSORBED-NESS IS `merged_into IS NOT NULL`, NOT status='merged'.
        # Status and merge-membership are INDEPENDENT axes (King ruling), so a
        # 'done' item folded under an umbrella KEEPS 'done' — a status-based
        # filter cannot see it and would leak it into the default view. The
        # status='merged' arm stays for rows written before #580 (and for open
        # items, which still wear 'merged' as the legacy flag).
        if not include_removed:
            clauses.append("status != 'removed'")
        if not include_merged:
            clauses.append("(merged_into IS NULL AND status != 'merged')")
    if priority is not None:
        clauses.append("priority = ?")
        params.append(_canon_priority(priority))
    # #573 kind_filter: one of the five kinds, or the filter-only pseudo-value
    # 'unset' for the UNRATED worklist. An unknown value RAISES rather than
    # silently matching nothing — an empty result set is indistinguishable from
    # "no such items", which is how a typo'd filter reads as a clean backlog.
    if kind_filter:
        if kind_filter == KIND_FILTER_UNSET:
            clauses.append("(kind IS NULL OR kind = '')")
        else:
            err = validate_kind(kind_filter)
            if err is not None:
                raise ValueError(
                    f"{err} (kind_filter also accepts {KIND_FILTER_UNSET!r} "
                    f"for unrated items)"
                )
            clauses.append("kind = ?")
            params.append(kind_filter)
    # difficulty_filter mirrors kind_filter exactly (one pattern, not two): a
    # legal rung for exact match, or the filter-only 'unset' pseudo-value for
    # the UNRATED worklist. An illegal value RAISES rather than silently
    # matching nothing — an empty result set is indistinguishable from "no such
    # items", which is how a typo'd filter reads as a clean backlog.
    if difficulty_filter is not None and difficulty_filter != "":
        if difficulty_filter == DIFFICULTY_FILTER_UNSET:
            clauses.append("difficulty IS NULL")
        else:
            derr = validate_difficulty(difficulty_filter)
            if derr is not None:
                raise ValueError(
                    f"{derr} (difficulty_filter also accepts "
                    f"{DIFFICULTY_FILTER_UNSET!r} for unrated items; use "
                    f"difficulty_min/difficulty_max for a range)"
                )
            clauses.append("difficulty = ?")
            params.append(int(difficulty_filter))
    # RANGE filters — the reason difficulty is a number at all. "What needs
    # decomposing?" is difficulty_min=4, one comparison, no rung enumeration.
    # UNRATED rows are NEVER swept into a range (NULL fails every comparison in
    # SQL, and that is the correct answer: an unrated item is not a 1).
    for bound, op, label in (
        (difficulty_min, ">=", "difficulty_min"),
        (difficulty_max, "<=", "difficulty_max"),
    ):
        if bound is None:
            continue
        berr = validate_difficulty(bound)
        if berr is not None:
            raise ValueError(f"{label}: {berr}")
        clauses.append(f"difficulty {op} ?")
        params.append(int(bound))
    if tag_filter:
        clauses.append("tags_json LIKE ?")
        params.append(f'%"{tag_filter}"%')
    # tags=[...] — any-of intersection: keep rows whose tag set carries at
    # least one requested tag (tags=[x] ≡ tag_filter=x). Blank entries are
    # noise, not a match-nothing filter. The JSON-quoted LIKE anchor
    # ('%"tag"%') matches whole stored tags only ('bug' never hits 'debug').
    wanted_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if wanted_tags:
        clauses.append("(" + " OR ".join(["tags_json LIKE ?"] * len(wanted_tags)) + ")")
        params.extend(f'%"{t}"%' for t in wanted_tags)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Priority ordering: critical > urgent > high > normal > low > idea.
    # status ordering: active first, then blocked, then closed.
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM project_backlog {where} "
            f"ORDER BY "
            f"  CASE status "
            f"    WHEN 'in_progress' THEN 0 "
            f"    WHEN 'open' THEN 1 "
            f"    WHEN 'blocked' THEN 2 "
            f"    WHEN 'done' THEN 3 "
            f"    WHEN 'rejected' THEN 4 "
            f"    ELSE 5 END, "
            f"  CASE priority "
            f"    WHEN 'critical' THEN 0 "
            f"    WHEN 'urgent' THEN 1 "
            f"    WHEN 'high' THEN 2 "
            f"    WHEN 'normal' THEN 3 "
            f"    WHEN 'low' THEN 4 "
            f"    ELSE 5 END, "
            f"  created_at ASC "
            f"LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_by_id(
    project_root: Path,
    *,
    backlog_id: int,
) -> dict[str, Any] | None:
    """Single-item read for ai_backlog mode='get'. Returns full row
    dict (including content) or None when id not found. Used by the
    paged-body get surface; list mode never calls this.
    """
    init_db(project_root)
    # #580 asymmetry fold-in: list_backlog hydrates fold-on-read, get_by_id did
    # not — so the two readers could answer from different generations of the
    # same store. Same call, same fail-open contract.
    try:
        from . import sync_store

        sync_store.maybe_hydrate(project_root, "backlog", hydrate_from_events)  # fold-on-read
    except Exception:
        pass
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rid = _resolve_rowid(conn, backlog_id)  # #580 single-sourced resolution
        if rid is None:
            return None
        row = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (rid,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def update(
    project_root: Path,
    *,
    backlog_id: int,
    status: str | None = None,
    content: str | None = None,
    title: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
    difficulty: int | None = None,
    tags: list[str] | None = None,
    linked_task_id: str | None = None,
    reason: str | None = None,
    allow_clear: bool = False,
    append: bool = False,
) -> dict[str, Any]:
    """Patch a backlog item — see _update_locked for the full content contract
    (content REPLACES the body and re-derives the title; append=True ADDS to it
    and leaves the title alone; title= patches the HEADLINE ALONE and beats
    derivation, #839). Serialized against fold-on-read hydrates (#503) so a
    concurrent hydrating read cannot replay pre-write event state over this
    write."""
    # #684 SINK SCREEN — same law as add(). None stays None so the
    # non-destructive contract (#399) is untouched.
    from .agent_prose_screen import repair_agent_prose

    content = repair_agent_prose(content, sink="backlog_body")
    with _mutating():
        return _update_locked(
            project_root,
            backlog_id=backlog_id,
            status=status,
            content=content,
            title=title,
            priority=priority,
            kind=kind,
            difficulty=difficulty,
            tags=tags,
            linked_task_id=linked_task_id,
            reason=reason,
            allow_clear=allow_clear,
            append=append,
        )


def _update_locked(
    project_root: Path,
    *,
    backlog_id: int,
    status: str | None = None,
    content: str | None = None,
    title: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
    difficulty: int | None = None,
    tags: list[str] | None = None,
    linked_task_id: str | None = None,
    reason: str | None = None,
    allow_clear: bool = False,
    append: bool = False,
) -> dict[str, Any]:
    """Patch a backlog item. Only fields explicitly passed change (#399):
    content=None leaves body/title untouched, tags=None leaves tags untouched.

      * title=<text>                patches the HEADLINE ALONE and leaves the
                                    body untouched (#839). This is the safe
                                    way to correct a stale title — before it
                                    existed, the only route was the
                                    destructive content= replace below, so
                                    nobody took it and headlines rotted while
                                    the bodies stayed accurate. An explicit
                                    title BEATS content='s derivation.

    READ THIS BEFORE PASSING content (#800). "Non-destructive" means UNPASSED
    FIELDS ARE NOT TOUCHED. It does NOT mean the body is protected:

      * content=<text>              REPLACES the entire body, and RE-DERIVES
                                    the title from its first line. There is no
                                    history, no revision list and no undo — a
                                    replaced body is gone.
      * content=<text>, append=True ADDS <text> to the end of the body and
                                    LEAVES THE TITLE ALONE. This is the safe
                                    primitive for the "keep the reasoning, add
                                    a dated section" workflow this backlog is
                                    maintained by.
      * content=""                  refused unless allow_clear=True.

    allow_clear guards only the EMPTY-string case — i.e. the one input a caller
    would never send by accident. It has never guarded the input they would.
    That gap cost #789 its body (4593 characters, ok:true, no warning), which
    is why the replace behaviour is now stated here instead of implied.

    Merge membership is NOT a status (#580/#595). A status change LEAVES
    merged_into intact — marking a folded child 'done' keeps it in its
    umbrella. The single exception is the documented #450 reactivation
    gesture: status='open' on a row whose status is literally 'merged'
    clears merged_into, and the receipt carries unmerged=True so it is never
    silent. To un-fold at any other status, call unmerge().
    """
    init_db(project_root)
    if status is not None and status not in _STATUSES:
        return {"ok": False, "error": f"status {status!r} not in {sorted(_STATUSES)}"}
    if priority is not None:
        priority = _canon_priority(priority)
        if priority not in _PRIORITIES:
            return {"ok": False, "error": f"priority {priority!r} not in {sorted(_PRIORITIES)}"}
    # #573: a kind is MEANT to change as understanding improves (#569 moved
    # investigate → wire-up, and that transition IS the work being done). Only
    # a known kind is storable; unknown is refused, never coerced.
    if kind is not None:
        err = validate_kind(kind)
        if err is not None:
            return {"ok": False, "error": err}
    # A difficulty is likewise MEANT to change: an item read as a 2 that turns
    # out to need a worker fleet becomes a 4, and that re-rating is the signal a
    # future depth policy reads. Only a legal rung is storable; anything else is
    # refused, never clamped.
    if difficulty is not None:
        derr = validate_difficulty(difficulty)
        if derr is not None:
            return {"ok": False, "error": derr}
    now = _now()
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        # #580: ONE resolver for every id on this store (see _resolve_rowid).
        # Writes key on the resolved row's global_id, so a display renumber can
        # never redirect to the wrong item; an unresolvable id is REFUSED.
        _rid = _resolve_rowid(conn, backlog_id)
        if _rid is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        backlog_id = _rid
        existing = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        if existing["status"] == "removed":
            return {
                "ok": False,
                "error": f"backlog id={backlog_id} is removed; cannot update",
            }
        sets: list[str] = []
        params: list[Any] = []
        # #595 (a status update silently un-folding an item): a status change
        # must NOT touch merged_into. #580 made status and merge-membership
        # INDEPENDENT axes, but this rule — written for #450 (merge/unmerge
        # reversibility), when `unmerge` did not yet exist — still coupled them
        # for rows wearing the LEGACY status='merged'. Measured consequence:
        # marking a folded child 'done' silently EJECTED it from its umbrella,
        # ok:true and no receipt. Clearing the pointer is `unmerge`'s job.
        #
        # ONE path survives as back-compat: status explicitly set to 'open' on
        # a row whose status is literally 'merged'. That is the documented
        # "reactivate a merged item" gesture (#450), it is pinned by a
        # committed seam test, and it is unambiguous — 'merged' is not a real
        # status, so returning such a row to 'open' IS the un-fold. Every
        # OTHER status (done/in_progress/blocked/rejected) now keeps the fold.
        _unmerge = (
            status == "open"
            and str(existing["status"] or "") == "merged"
        )
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "done":
                sets.append("completed_at = ?")
                params.append(now)
            if _unmerge:
                # #450 reversibility, narrowed by #595: reactivating a legacy
                # 'merged' row to 'open' clears the umbrella pointer, and the
                # receipt SAYS so (unmerged=True) — it is never silent.
                sets.append("merged_into = NULL")
        # #800: the effective body and title are computed ONCE, here, and reused
        # by BOTH writers — the SQL SET list below and the event payload ~130
        # lines further down. They used to be derived independently from the
        # `content` ARGUMENT in two places, which is why the first append fix
        # appeared to work in sqlite and was then undone by the event: this
        # store is event-sourced, so the event is the copy that wins.
        # Two derivations of one fact, in one function. See the event build.
        _eff_content: str | None = None
        _eff_title: str | None = None
        if content is not None and append:
            # #800: APPEND — the safe primitive for how this backlog is
            # actually maintained ("preserve the body, ADD a dated section").
            # Without it, every such edit was a read-whole-body-then-rewrite-it
            # dance that costs a full round-trip and, worse, LOSES THE TAIL
            # whenever the body is longer than the caller's read window.
            # Measured cost of not having it: #789 lost 4593 characters of an
            # operator-directed audit umbrella to a one-line write, ok:true.
            if not content.strip():
                return {
                    "ok": False,
                    "error": (
                        "append=True with empty content does nothing. Pass the "
                        "text to add, or omit content entirely to leave the "
                        "body untouched."
                    ),
                }
            if allow_clear:
                return {
                    "ok": False,
                    "error": (
                        "append=True and allow_clear=True contradict each other "
                        "— one adds to the body, the other empties it. Pass "
                        "exactly one."
                    ),
                }
            prior = str(existing["content"] or "")
            merged = (
                prior.rstrip("\n") + "\n\n" + content.lstrip("\n")
                if prior.strip()
                else content
            )
            _eff_content = merged
            sets.append("content = ?")
            params.append(merged)
            # TITLE IS DELIBERATELY NOT RE-DERIVED (#800 defect 3). A title that
            # changes because someone appended a dated section is a footgun —
            # that is exactly how #789 got renamed to "=== SEMANTICS PROBE ===".
            # An append adds to the END; the first line, which IS the title, did
            # not move, so neither does the title.
        elif content is not None:
            if not content.strip() and not allow_clear:
                return {
                    "ok": False,
                    "error": (
                        "content='' would CLEAR this item's body and title "
                        "(#399 non-destructive contract). Pass allow_clear=True "
                        "to clear deliberately, or omit content to leave the "
                        "body untouched."
                    ),
                }
            _eff_content = content
            _eff_title = _extract_title(content)
            sets.append("content = ?")
            params.append(_eff_content)
            # Re-derive title when body changes; title stays in sync.
            # (The SET is emitted once, below, so an explicit title= can win.)
        # ── #839: the NON-DESTRUCTIVE headline fix ────────────────────────
        # Before this, the only route to a stale title was content=<whole new
        # body> — a replace with no history and no undo. Nobody trades an
        # item's accumulated evidence for a headline, so everyone appended,
        # and titles rotted permanently while the bodies underneath stayed
        # accurate. The rot was never a missing capability; it was the SHAPE
        # OF THE CHOICE: the safe primitive could not fix a title, and the one
        # that could was destructive.
        #
        # PRECEDENCE IS DELIBERATE, not an artefact of statement order: an
        # explicit title= WINS over content='s derivation. A caller who names
        # a title means it, and silently overwriting it from line 1 would
        # reintroduce the exact divergence this parameter exists to fix.
        if title is not None:
            _t = " ".join(str(title).split())
            if not _t:
                return {
                    "ok": False,
                    "error": (
                        "title='' (or whitespace) would leave this item with no "
                        "headline and unfindable in every list — a worse outcome "
                        "than the stale title it replaced. Pass real text, or "
                        "omit title to leave the headline untouched."
                    ),
                }
            # Same ceiling as the derived route, or the two disagree about
            # what a title is.
            _eff_title = _t[:_TITLE_MAX]
        if _eff_title is not None:
            sets.append("title = ?")
            params.append(_eff_title)
        if priority is not None:
            sets.append("priority = ?")
            params.append(priority)
        if kind is not None:
            sets.append("kind = ?")
            params.append(kind)
        if difficulty is not None:
            sets.append("difficulty = ?")
            params.append(int(difficulty))
        if tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(tags))
        if linked_task_id is not None:
            sets.append("linked_task_id = ?")
            params.append(linked_task_id)
        if not sets:
            note = (reason or "").strip()
            if not note:
                return {"ok": False, "error": "no updates provided"}
            # Reason-only ANNOTATION (#314): no field changed, but a
            # non-empty reason was given. Record it — bump updated_at and
            # emit an update event carrying the reason (mirrors how remove
            # persists removed_reason) — then return ok.
            conn.execute(
                "UPDATE project_backlog SET updated_at = ? WHERE id = ?",
                (now, backlog_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM project_backlog WHERE id = ?",
                (backlog_id,),
            ).fetchone()
            gid = str(row["global_id"] if "global_id" in row.keys() else "")
            if gid:
                _emit_backlog(
                    project_root, gid, "update",
                    {"updated_at": now, "reason": note},
                )
            return {"ok": True, "annotation": True, "reason": note, **_row_to_dict(row)}
        sets.append("updated_at = ?")
        params.append(now)
        params.append(backlog_id)
        conn.execute(
            f"UPDATE project_backlog SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
        gid = str(row["global_id"] if "global_id" in row.keys() else "")
    _changed: dict[str, Any] = {"updated_at": now}
    if status is not None:
        _changed["status"] = status
        if status == "done":
            _changed["completed_at"] = now
        if _unmerge:
            _changed["merged_into"] = None
    # #800 ONE DERIVATION. This block used to recompute the body and title from
    # the `content` ARGUMENT, independently of the SQL write above — two copies
    # of one fact, ~130 lines apart, in the same function. Because the store is
    # EVENT-SOURCED, this copy is the one that survives a rebuild, so the two
    # disagreeing was not cosmetic: an append written correctly to sqlite was
    # silently replaced by the raw appended text, title and all. Both writers
    # now read the values computed once at the content branch.
    if _eff_content is not None:
        _changed["content"] = _eff_content
    if _eff_title is not None:
        _changed["title"] = _eff_title
    if priority is not None:
        _changed["priority"] = priority
    if kind is not None:
        _changed["kind"] = kind
    if difficulty is not None:
        _changed["difficulty"] = int(difficulty)
    if tags is not None:
        _changed["tags"] = tags
    if linked_task_id is not None:
        _changed["linked_task_id"] = linked_task_id
    # #710: a reason accompanying a FIELD CHANGE was dropped on the floor.
    # Verified by read-back at the time: update(id=681, status='done',
    # reason='<2.5KB of bisect evidence>') returned applied:["status"] and the
    # rationale was gone — so every item closed that day is marked done with no
    # record of WHY, which is worse than leaving it open: it looks settled, so
    # nobody re-derives it.
    #
    # The reason-ONLY branch above already persists it exactly this way. The
    # two paths simply disagreed about whether a reason was worth keeping, and
    # the one that also changed a field was the one that discarded it. This is
    # the same disagreement-between-siblings shape as the rest of this file.
    _reason_note = (reason or "").strip()
    if _reason_note:
        _changed["reason"] = _reason_note
    if gid:
        _emit_backlog(project_root, gid, "update", _changed)
        # A status change (e.g. → removed) alters the live set → ranks shift.
        _reassign_display_ids(project_root)
        _notify_autosync(project_root)  # debounced push to the gate (fail-open)
        with contextlib.suppress(Exception):
            with _canonical_connect(_db_path(project_root)) as _c:
                _c.row_factory = sqlite3.Row
                _r = _c.execute(
                    "SELECT * FROM project_backlog WHERE global_id = ?", (gid,)
                ).fetchone()
                if _r is not None:
                    row = _r
    # #710: the RECEIPT names the reason too. The item's second choice was
    # "fail loud" — a caller could not tell a persisted rationale from a
    # discarded one, because `applied` listed only the fields it recognised.
    # Now that the reason is durable, saying so is what closes the loop: an
    # ok:true that is silent about a field the caller passed is the fail-quiet
    # write this item is filed against.
    _receipt: dict[str, Any] = {"ok": True}
    if _unmerge:
        # #595: an un-fold is never silent — the receipt names it.
        _receipt["unmerged"] = True
    if _reason_note:
        _receipt["reason"] = _reason_note
    return {**_receipt, **_row_to_dict(row)}


def _row_title(row: sqlite3.Row) -> str:
    try:
        title = str(row["title"] or "")
    except (KeyError, IndexError):
        title = ""
    return title or _extract_title(str(row["content"] or ""))


def _row_tags(row: sqlite3.Row) -> list[str]:
    try:
        parsed = json.loads(row["tags_json"] or "[]")
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def merge(
    project_root: Path,
    *,
    ids: list[int],
    umbrella_id: int | None = None,
) -> dict[str, Any]:
    """#450 merge — see _merge_locked. Serialized against fold-on-read hydrates
    (#503): a merge writes several rows then emits several events, so a fold
    landing mid-batch would see a half-merged log.

    #580 asymmetry fold-in: the fold-on-read hydrate runs BEFORE the lock is
    taken. list_backlog already folded on read while merge did not, so merge
    could decide against a staler generation of the store than the reader that
    picked the ids. Inside _mutating() a hydrate can only DEFER (see
    hydrate_from_events), so it has to happen out here.
    """
    try:
        from . import sync_store

        sync_store.maybe_hydrate(project_root, "backlog", hydrate_from_events)
    except Exception:
        pass
    with _mutating():
        return _merge_locked(project_root, ids=ids, umbrella_id=umbrella_id)


def _merge_locked(
    project_root: Path,
    *,
    ids: list[int],
    umbrella_id: int | None = None,
) -> dict[str, Any]:
    """#450/#580: fold N backlog items — ANY STATUS, ANY NUMBER OF TIMES —
    into one umbrella item.

    MERGE IS A RELATION, NOT A STATE TRANSITION THAT CONSUMES THE ROW (King
    ruling 2026-07-28: "no matter the status of the backlog item they should be
    mergeable forever"):
      * ``merged_into`` is a POINTER and nothing more. It never gates a future
        operation. Merging again RE-POINTS; unmerge() clears.
      * Status and merge-membership are INDEPENDENT axes. A 'done' item folded
        for history KEEPS 'done'. Only an 'open' item takes the legacy
        status='merged' marker, and only because the pre-#580 event log and the
        include_merged surface still speak it.
      * An umbrella may be ANY item that exists, including one that is itself
        absorbed (umbrella-into-umbrella). The ONE refusal left is a REMOVED
        umbrella: a tombstone must not become a live grouping (mirrors
        _update_locked refusing to update a removed row).
    Absorbed items are KEPT, never removed — body, tags, status and identity
    all survive. The umbrella takes the tag UNION and grows an '## Absorbed'
    ledger line per absorbed item. Every write emits the NORMAL per-row update
    event via _emit_backlog — the same sync path as any update, never a forked
    emit.

    IDS ARE DISPLAY IDS. Callers pass the convergent #N; every one is resolved
    through _resolve_rowid (the #580 fix — merge used to read and write RAW
    ROWIDS while get/update resolved display ids, so it moved different rows
    than the ones asked for). ``merged_into`` and the ledger keep DISPLAY ids,
    which is what a human reads as #N and what _row_to_dict returns.

    umbrella_id defaults to the LOWEST id in `ids`. When given, it may be
    outside `ids` (absorb all listed items into it).
    """
    init_db(project_root)
    uniq: list[int] = []
    for raw in ids or []:
        val = int(raw)
        if val not in uniq:
            uniq.append(val)
    if umbrella_id is not None:
        umbrella_id = int(umbrella_id)
        if umbrella_id not in uniq:
            uniq.append(umbrella_id)
    if len(uniq) < 2:
        return {"ok": False, "error": "merge requires >= 2 distinct backlog ids"}
    target = umbrella_id if umbrella_id is not None else min(uniq)
    now = _now()
    repointed: list[dict[str, Any]] = []
    repoint_body: dict[int, str] = {}
    repoint_gid: dict[int, str] = {}
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rows: dict[int, sqlite3.Row] = {}
        rowid_of: dict[int, int] = {}
        for bid in uniq:
            rid = _resolve_rowid(conn, bid)
            row = (
                conn.execute(
                    "SELECT * FROM project_backlog WHERE id = ?", (rid,)
                ).fetchone()
                if rid is not None
                else None
            )
            if row is None:
                return {"ok": False, "error": f"backlog id={bid} not found"}
            rows[bid] = row
            rowid_of[bid] = int(rid)
        umbrella = rows[target]
        if str(umbrella["status"]) == "removed":
            return {
                "ok": False,
                "error": (
                    f"umbrella id={target} is removed (tombstone); a removed "
                    "item cannot become an umbrella"
                ),
            }
        absorbed_ids = [b for b in uniq if b != target]
        # Tag UNION — order-preserving: umbrella's tags first, then each
        # absorbed item's, first occurrence wins.
        union: list[str] = []
        for bid in (target, *absorbed_ids):
            for t in _row_tags(rows[bid]):
                if t not in union:
                    union.append(t)
        # Umbrella body: append the '## Absorbed' ledger (id + title per
        # absorbed item). Re-merges into the same umbrella extend the existing
        # section instead of duplicating the heading, and an item ALREADY
        # listed is re-stated once, not twice (#580 re-merge is legal now).
        body = str(umbrella["content"] or "").rstrip()
        for b in absorbed_ids:
            body, _restated = _rewrite_absorbed_line(body, b, None)
        body = body.rstrip()
        absorbed_lines = "\n".join(
            f"- #{b} {_row_title(rows[b])}" for b in absorbed_ids
        )
        if "## Absorbed" in body:
            body = f"{body}\n{absorbed_lines}\n"
        else:
            body = f"{body}\n\n## Absorbed\n{absorbed_lines}\n"
        new_title = _extract_title(body)
        conn.execute(
            "UPDATE project_backlog SET content = ?, title = ?, tags_json = ?, "
            "updated_at = ? WHERE id = ?",
            (body, new_title, json.dumps(union), now, rowid_of[target]),
        )
        status_of: dict[int, str] = {}
        for b in absorbed_ids:
            cur_status = str(rows[b]["status"] or "open")
            # #580: STATUS IS NOT THE MERGE FLAG. Only an 'open' item takes the
            # legacy 'merged' marker; every other status is left EXACTLY as it
            # is — folding a 'done' item must not destroy the fact that it is
            # done. The default list view hides on merged_into, not on status.
            status_of[b] = "merged" if cur_status == "open" else cur_status
            conn.execute(
                "UPDATE project_backlog SET status = ?, merged_into = ?, "
                "updated_at = ? WHERE id = ?",
                (status_of[b], target, now, rowid_of[b]),
            )
        # RE-POINT (#580 defect 5): an item moving umbrellas leaves a now-FALSE
        # '- #id title' claim in its PREVIOUS umbrella's ledger. Rewrite that
        # line to record the move — history AND truth, never a silent delete.
        for b in absorbed_ids:
            prev = rows[b]["merged_into"]
            if prev is None or int(prev) == int(target):
                continue
            prev_rowid = _resolve_rowid(conn, int(prev))
            if prev_rowid is None or prev_rowid == rowid_of[target]:
                continue
            if prev_rowid not in repoint_body:
                prow = conn.execute(
                    "SELECT content, global_id FROM project_backlog WHERE id = ?",
                    (prev_rowid,),
                ).fetchone()
                if prow is None:
                    continue
                repoint_body[prev_rowid] = str(prow["content"] or "")
                repoint_gid[prev_rowid] = str(prow["global_id"] or "")
            repoint_body[prev_rowid], matched = _rewrite_absorbed_line(
                repoint_body[prev_rowid],
                b,
                f"- #{b} {_row_title(rows[b])} → re-pointed to #{target}",
            )
            repointed.append(
                {"id": b, "from": int(prev), "to": target, "ledger_line": matched}
            )
        for prev_rowid, prev_body in repoint_body.items():
            conn.execute(
                "UPDATE project_backlog SET content = ?, title = ?, updated_at = ? "
                "WHERE id = ?",
                (prev_body, _extract_title(prev_body), now, prev_rowid),
            )
        conn.commit()
        gid_of = {
            b: str(rows[b]["global_id"] if "global_id" in rows[b].keys() else "")
            for b in uniq
        }
    # Normal update events through the ONE store-layer emit (#450: merge is
    # a batch of ordinary updates on the wire — no forked event op).
    if gid_of.get(target):
        _emit_backlog(
            project_root, gid_of[target], "update",
            {"content": body, "title": new_title, "tags": union, "updated_at": now},
        )
    for b in absorbed_ids:
        if gid_of.get(b):
            _emit_backlog(
                project_root, gid_of[b], "update",
                {"status": status_of[b], "merged_into": target, "updated_at": now},
            )
    for prev_rowid, prev_body in repoint_body.items():
        if repoint_gid.get(prev_rowid):
            _emit_backlog(
                project_root, repoint_gid[prev_rowid], "update",
                {
                    "content": prev_body,
                    "title": _extract_title(prev_body),
                    "updated_at": now,
                },
            )
    return {
        "ok": True,
        "umbrella_id": target,
        "merged_ids": absorbed_ids,
        "statuses": status_of,
        "repointed": repointed,
        "tags": union,
        "title": new_title,
    }


def unmerge(
    project_root: Path,
    *,
    backlog_id: int,
) -> dict[str, Any]:
    """#580 unmerge — see _unmerge_locked. Serialized against fold-on-read
    hydrates (#503) like every other mutation."""
    with _mutating():
        return _unmerge_locked(project_root, backlog_id=backlog_id)


def _unmerge_locked(
    project_root: Path,
    *,
    backlog_id: int,
) -> dict[str, Any]:
    """Release an item from its umbrella WITHOUT touching its status (#580).

    Before this verb the only escape from a merge was ``update(status='open')``
    — which is DATA LOSS for anything that was not open when it was folded: a
    'done' item absorbed for history came back 'open', and the item's real
    status was gone. Status and merge-membership are INDEPENDENT axes, so
    unmerge clears the POINTER and nothing else. The one exception is a row
    still wearing the legacy status='merged' marker: 'merged' is not a
    standalone state, so such a row returns to 'open' — the status it had when
    it was folded. update(status=...) keeps clearing the pointer too, for
    compatibility with callers written against #450.

    The old umbrella's ledger line is REWRITTEN (never deleted): the umbrella
    stops claiming an item it no longer holds, and the record that it once did
    survives.

    A never-merged item is an honest no-op — ``not_merged: True``, said out
    loud rather than reported as work done. An unresolvable id is REFUSED.
    A tombstoned row CAN be released (merge is allowed to absorb one); nothing
    else about it changes.
    """
    init_db(project_root)
    now = _now()
    umbrella_gid = ""
    umbrella_body: str | None = None
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rid = _resolve_rowid(conn, backlog_id)
        row = (
            conn.execute(
                "SELECT * FROM project_backlog WHERE id = ?", (rid,)
            ).fetchone()
            if rid is not None
            else None
        )
        if row is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        display_id = _row_to_dict(row)["display_id"]
        prev = row["merged_into"]
        cur_status = str(row["status"] or "")
        if prev is None and cur_status != "merged":
            return {
                "ok": True,
                "not_merged": True,
                "id": display_id,
                "status": cur_status,
                "merged_into": None,
            }
        new_status = "open" if cur_status == "merged" else cur_status
        conn.execute(
            "UPDATE project_backlog SET status = ?, merged_into = NULL, "
            "updated_at = ? WHERE id = ?",
            (new_status, now, rid),
        )
        gid = str(row["global_id"] if "global_id" in row.keys() else "")
        if prev is not None:
            prev_rowid = _resolve_rowid(conn, int(prev))
            if prev_rowid is not None and prev_rowid != rid:
                prow = conn.execute(
                    "SELECT content, global_id FROM project_backlog WHERE id = ?",
                    (prev_rowid,),
                ).fetchone()
                if prow is not None:
                    new_body, matched = _rewrite_absorbed_line(
                        str(prow["content"] or ""),
                        int(display_id),
                        f"- #{display_id} {_row_title(row)} → unmerged",
                    )
                    if matched:
                        umbrella_body = new_body
                        umbrella_gid = str(prow["global_id"] or "")
                        conn.execute(
                            "UPDATE project_backlog SET content = ?, title = ?, "
                            "updated_at = ? WHERE id = ?",
                            (new_body, _extract_title(new_body), now, prev_rowid),
                        )
        conn.commit()
    if gid:
        _emit_backlog(
            project_root, gid, "update",
            {"status": new_status, "merged_into": None, "updated_at": now},
        )
    if umbrella_gid and umbrella_body is not None:
        _emit_backlog(
            project_root, umbrella_gid, "update",
            {
                "content": umbrella_body,
                "title": _extract_title(umbrella_body),
                "updated_at": now,
            },
        )
    return {
        "ok": True,
        "id": display_id,
        "status": new_status,
        "merged_into": None,
        "was_merged_into": int(prev) if prev is not None else None,
    }


def similar_open_items(
    project_root: Path,
    *,
    tags: list[str],
    exclude_id: int | None = None,
    min_shared: int = 2,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """#450 suggestion half: OPEN items sharing >= min_shared tags with the
    given tag set. Terse [{id, title}] — advisory only, NEVER auto-merges.
    Fewer than min_shared input tags can never overlap enough → [].
    """
    wanted = {str(t).strip() for t in (tags or []) if str(t).strip()}
    if len(wanted) < min_shared:
        return []
    out: list[dict[str, Any]] = []
    for item in list_backlog(project_root, status="open", limit=500):
        if exclude_id is not None and item["id"] == exclude_id:
            continue
        # #580: an ABSORBED item is never a merge candidate, whatever status it
        # carries — status stopped being the merge flag, so status='open' alone
        # no longer proves the row is unfolded.
        if item.get("merged_into") is not None:
            continue
        if len(wanted & set(item["tags"])) >= min_shared:
            out.append({"id": item["id"], "title": item["title"]})
            if len(out) >= limit:
                break
    return out


def remove(
    project_root: Path,
    *,
    backlog_id: int,
    reason: str,
) -> dict[str, Any]:
    """Tombstone a backlog item. Never DELETEs. Serialized against fold-on-read
    hydrates (#503) — a tombstone may not be un-tombstoned by a racing fold."""
    with _mutating():
        return _remove_locked(project_root, backlog_id=backlog_id, reason=reason)


def _remove_locked(
    project_root: Path,
    *,
    backlog_id: int,
    reason: str,
) -> dict[str, Any]:
    init_db(project_root)
    now = _now()
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        # #580: ONE resolver for every id on this store (see _resolve_rowid).
        # Writes key on the resolved row's global_id, so a display renumber can
        # never redirect to the wrong item; an unresolvable id is REFUSED.
        _rid = _resolve_rowid(conn, backlog_id)
        if _rid is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        backlog_id = _rid
        existing = conn.execute(
            "SELECT * FROM project_backlog WHERE id = ?",
            (backlog_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"backlog id={backlog_id} not found"}
        if existing["status"] == "removed":
            return {
                "ok": True,
                "already_removed": True,
                "id": backlog_id,
                "removed_reason": existing["removed_reason"],
            }
        conn.execute(
            "UPDATE project_backlog SET status = 'removed', "
            "updated_at = ?, removed_at = ?, removed_reason = ? "
            "WHERE id = ?",
            (now, now, reason, backlog_id),
        )
        conn.commit()
        gid = str(existing["global_id"] if "global_id" in existing.keys() else "")
    if gid:
        # remove is a TOMBSTONE-via-status ('removed'), audit-preserving -> an
        # 'update' event (status=removed), NOT an 'delete' op (which would drop
        # the entity from the folded view; the backlog keeps removed items).
        _emit_backlog(
            project_root, gid, "update",
            {"status": "removed", "updated_at": now, "removed_at": now, "removed_reason": reason},
        )
    return {
        "ok": True,
        "id": backlog_id,
        "status": "removed",
        "removed_at": now,
        "removed_reason": reason,
    }


def _row_col(row: sqlite3.Row, column: str) -> Any:
    """Read a post-migration column off a row that may predate it (stale
    connection reading pre-ALTER schema). None when absent — callers supply the
    unset default so a missing column never becomes a guessed value."""
    try:
        return row[column]
    except (KeyError, IndexError):
        return None


def _materialize_backlog(project_root: Path, folded: dict[str, dict]) -> None:
    """Upsert folded event-state into sqlite by global_id. This is the DERIVE
    step — it writes sqlite directly and does NOT re-emit (no mutation-path loop)."""
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        for gid, r in folded.items():
            existing = conn.execute(
                "SELECT * FROM project_backlog WHERE global_id = ?", (gid,)
            ).fetchone()
            if existing is not None:
                # #399 / generalized #376 — NON-DESTRUCTIVE MERGE. The folded
                # state is a PATCH over the live row, never a full overwrite. A
                # field ABSENT from the fold (e.g. a status-only update whose
                # add-event was quarantined by the authority split, so its
                # content/priority/tags never reached the fold) keeps its existing
                # value. A DB write may not unintentionally destroy fields it was
                # never told to change. Content is extra-guarded: an empty/
                # whitespace content never blanks a non-empty body.
                new_content = str(r.get("content") or "")
                content = new_content if new_content.strip() else str(existing["content"] or "")
                title = str(r.get("title") or _extract_title(content) or existing["title"] or "")
                status = str(r["status"]) if "status" in r.keys() else str(existing["status"] or "open")
                priority = (
                    _canon_priority(str(r["priority"]))
                    if "priority" in r.keys()
                    else str(existing["priority"] or "normal")
                )
                tags_json = (
                    json.dumps(r["tags"] or []) if "tags" in r.keys() else str(existing["tags_json"] or "[]")
                )
                updated_at = str(r.get("updated_at") or existing["updated_at"] or _now())
                removed_at = r["removed_at"] if "removed_at" in r.keys() else existing["removed_at"]
                removed_reason = (
                    r["removed_reason"] if "removed_reason" in r.keys() else existing["removed_reason"]
                )
                completed_at = (
                    r["completed_at"] if "completed_at" in r.keys() else existing["completed_at"]
                )
                merged_into = (
                    r["merged_into"] if "merged_into" in r.keys() else existing["merged_into"]
                )
                # #573 kind folds with the same PATCH semantics: absent from the
                # fold keeps the live value (so a pre-#573 event log cannot wipe
                # a rating), present replaces it (so a rating made on another
                # machine actually arrives).
                kind = (
                    str(r["kind"] or KIND_UNSET)
                    if "kind" in r.keys()
                    else str(_row_col(existing, "kind") or KIND_UNSET)
                )
                # difficulty folds with the same PATCH semantics as kind: absent
                # from the fold keeps the live rating (so a pre-difficulty event
                # log cannot wipe one), present replaces it (so a rating made on
                # another machine actually arrives). A present-but-illegal value
                # lands as UNSET rather than as a rating nobody made.
                if "difficulty" in r.keys():
                    _fold_diff = r["difficulty"]
                    difficulty = (
                        int(_fold_diff)
                        if isinstance(_fold_diff, int)
                        and not isinstance(_fold_diff, bool)
                        and _fold_diff in DIFFICULTY_ORDER
                        else DIFFICULTY_UNSET
                    )
                else:
                    difficulty = _row_col(existing, "difficulty")
                conn.execute(
                    "UPDATE project_backlog SET content=?, title=?, status=?, priority=?, "
                    "kind=?, difficulty=?, tags_json=?, updated_at=?, removed_at=?, "
                    "removed_reason=?, completed_at=?, merged_into=? "
                    "WHERE global_id=?",
                    (content, title, status, priority, kind, difficulty, tags_json,
                     updated_at, removed_at, removed_reason, completed_at,
                     merged_into, gid),
                )
            else:
                content = str(r.get("content", ""))
                title = str(r.get("title") or _extract_title(content))
                status = str(r.get("status", "open"))
                priority = _canon_priority(str(r.get("priority", "normal")))
                tags_json = json.dumps(r.get("tags") or [])
                created_at = str(r.get("created_at") or _now())
                updated_at = str(r.get("updated_at") or created_at)
                _new_diff = r.get("difficulty")
                if not (
                    isinstance(_new_diff, int)
                    and not isinstance(_new_diff, bool)
                    and _new_diff in DIFFICULTY_ORDER
                ):
                    _new_diff = DIFFICULTY_UNSET
                conn.execute(
                    "INSERT INTO project_backlog (content, title, status, priority, kind, "
                    "difficulty, tags_json, "
                    "created_in_session_id, source_task_id, created_at, updated_at, "
                    "removed_at, removed_reason, completed_at, merged_into, global_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (content, title, status, priority,
                     str(r.get("kind") or KIND_UNSET), _new_diff, tags_json,
                     r.get("created_in_session_id"), r.get("source_task_id"),
                     created_at, updated_at, r.get("removed_at"), r.get("removed_reason"),
                     r.get("completed_at"), r.get("merged_into"), gid),
                )
        conn.commit()


def _notify_autosync(project_root: Path) -> None:
    """Debounced-push hook: tell the BacklogSyncSitter a mutation happened so it
    batches a git push to the gate. No-op when autosync is off / no sitter runs.
    Fail-open — a sync trigger must never affect the write that just succeeded."""
    try:
        from .backlog_sync_sitter import notify_backlog_mutation

        notify_backlog_mutation(project_root)
    except Exception:
        pass


def _reassign_display_ids(project_root: Path) -> None:
    """Recompute the CONVERGENT display_id for every live row from the
    AUTHORITATIVE event log — a dense rank over creation-HLC order (see
    sync_store.convergent_display_ids). Pure projection: the same event set
    yields the same numbers on every store, so VPS and local stop diverging.
    Best-effort: a display renumber must NEVER block or fail a write, so any
    error is swallowed and the stale display_id simply persists until the next
    hydrate. global_id stays the authoritative key throughout.
    """
    try:
        from . import sync_store

        all_events = sync_store.GitEventTransport(project_root).read("backlog")
        authoritative, _incoming = sync_store.split_by_authority(
            project_root, "backlog", all_events
        )
        id_map = sync_store.convergent_display_ids(authoritative)
        if not id_map:
            return
        with _canonical_connect(
            _db_path(project_root), row_factory=False
        ) as conn:
            for gid, did in id_map.items():
                conn.execute(
                    "UPDATE project_backlog SET display_id = ? WHERE global_id = ?",
                    (did, gid),
                )
            conn.commit()
    except Exception:
        pass


def hydrate_from_events(project_root: Path) -> int:
    """Phase 2/3: fold the backlog event log into sqlite (materialized cache).

    AUTHORITY (#376, 2026-07-13): folds ONLY events that carry a local
    authoritative receipt (produced by an authenticated write on THIS gate).
    Incoming events that merely appeared in the events dir — a fresh clone's
    foreign files or a FORGED file with a self-asserted actor + max HLC — have
    no receipt and are QUARANTINED (recorded to a clear-status log, never
    applied), so a received file can never inject a phantom row or overwrite an
    existing one. Idempotent; does NOT re-emit. Returns entities materialized.

    #503 — NEVER FOLDS OVER AN IN-FLIGHT WRITE. A mutation's commit and its emit
    are not atomic; folding the log inside that window replays pre-write state
    over the row just written (the observed status revert). So:
      * a mutation on ANOTHER thread: we block on _MUTATION_LOCK until its
        commit+emit span closes, then fold a consistent log;
      * a mutation on THIS thread (a hydrate re-entered from a mutation-path
        callback — waiting would self-deadlock on the reentrant lock): we DEFER
        and return sync_store.HYDRATE_DEFERRED, which stops the fold-on-read
        watermark from advancing so the very next read folds instead.
    """
    init_db(project_root)
    from . import sync_store

    if _mutation_in_flight():
        return sync_store.HYDRATE_DEFERRED
    with _MUTATION_LOCK:
        all_events = sync_store.GitEventTransport(project_root).read("backlog")
        authoritative, incoming = sync_store.split_by_authority(
            project_root, "backlog", all_events
        )
        if incoming:
            sync_store.record_quarantine(project_root, "backlog", incoming)
        folded = sync_store.fold_events(authoritative)
        _materialize_backlog(project_root, folded)
        _reassign_display_ids(project_root)  # convergent #N from the event log
        return len(folded)


def rebuild_from_events(project_root: Path, *, adopt_incoming: bool = False) -> int:
    """Phase 3: rebuild sqlite from the AUTHORITATIVE event log (sqlite =
    derived). Clears the table then materializes the receipted fold — the repair
    path for stale/suspicious sqlite. By default rebuilds ONLY this gate's own
    canonical history (receipted events); an unreceipted (incoming/forged) file
    can neither survive nor be introduced by a rebuild.

    OPERATOR-APPROVED SNAPSHOT RECOVERY (#376): ``adopt_incoming=True`` first
    adopts EVERY current event file as authoritative (the operator declares the
    present event log to be truth — the disaster-recovery / fresh-clone
    bootstrap), then rebuilds. This is the explicit, operator-gated path for
    importing prior history; it is NEVER reached on the fold-on-read path, which
    stays receipted-only so a received file can never auto-mutate canonical
    state."""
    init_db(project_root)
    from . import sync_store

    if adopt_incoming:
        sync_store.adopt_events_as_authoritative(project_root, "backlog", None)
    with _canonical_connect(_db_path(project_root), row_factory=False) as conn:
        conn.execute("DELETE FROM project_backlog")
        conn.commit()
    return hydrate_from_events(project_root)


def seed_events_from_sqlite(project_root: Path) -> int:
    """One-time backfill: emit an 'add' event carrying the CURRENT state of every
    backlog row that has NO event yet, so rows created before store-layer emit
    existed enter the canonical log. IDEMPOTENT — rows whose global_id is already
    an event entity are skipped, so re-running is a no-op. Includes tombstoned
    (removed) rows so the log reflects full reality. Returns rows seeded.
    """
    init_db(project_root)
    from . import sync_store

    seen = {e.entity_id for e in sync_store.GitEventTransport(project_root).read("backlog")}
    with _canonical_connect(_db_path(project_root)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM project_backlog").fetchall()
    seeded = 0
    for row in rows:
        gid = str(row["global_id"] or "")
        if not gid or gid in seen:
            continue
        d = _row_to_dict(row)
        _emit_backlog(
            project_root, gid, "add",
            {
                "content": d["content"], "title": d["title"], "status": d["status"],
                "priority": d["priority"], "tags": d["tags"],
                "created_at": d["created_at"], "updated_at": d["updated_at"],
                "created_in_session_id": d["created_in_session_id"],
                "source_task_id": d["source_task_id"],
                "removed_at": d["removed_at"], "removed_reason": d["removed_reason"],
                "completed_at": d["completed_at"],
            },
            session_id=d["created_in_session_id"] or "",
        )
        seeded += 1
    return seeded
