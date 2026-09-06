"""The ONE home of operator identity + authority (#488, operator ruling).

WHY THIS MODULE EXISTS
----------------------
Identity was a PER-PROJECT sqlite store (``<project>/.MEMORY/.index/
aidocs_identity.sqlite3``) while the operator TOKEN cache has always been
MACHINE-GLOBAL (``~/.aidocs/operator_token.json``). Those two facts
contradict each other, and the contradiction is what the operator felt:

    log in while project A is selected
      -> user row + token row land in PROJECT A's identity DB
      -> the token is cached machine-wide
    swap to project B
      -> the cached token is validated against PROJECT B's identity DB
      -> no such token row  => "not logged in"
      -> log in again      => no such USER row => "invalid email or password"

An account that provably exists reads as invalid; a login that provably
happened reads as absent. The operator is ONE person on ONE machine, so
their identity is a MACHINE fact -- exactly like the empire law store
(``~/.aidocs/empire.sqlite3``), the souls, the skills and the token cache.

THE TIER SPLIT
--------------
GLOBAL (this module, ``~/.aidocs/identity.sqlite3``) -- who you are and what
you may do anywhere: ``identity_users``, ``identity_tokens`` and the RBAC
authority tables (roles, permissions, role-permissions, user-roles, scoped
role-permissions, per-user overrides, cache version). Scope-bearing rows
keep their ``scope_type``/``scope_id`` columns, so a project-scoped grant is
still project-scoped -- the STORE is global, the GRANT need not be.

PROJECT (unchanged, in each project's identity DB) -- what happened HERE:
``session_freeze``, ``rbac_escalations``, ``rbac_escalation_grants``,
``protected_file_registry``, ``dnt_banners_shown``,
``agent_memory_compaction_state``.

MIGRATION (no operator ceremony)
--------------------------------
The first time a store initializes the global home, any table that is EMPTY
globally and POPULATED in that project's legacy DB is imported verbatim.
Empty-target-only is the safe direction: it can never resurrect a row an
operator deliberately deleted, and it is idempotent. Legacy rows are left in
place (read-only history), so a rollback loses nothing.

Test isolation follows the proven pattern of every other machine-global
store: ``AIDOCS_IDENTITY_DB`` overrides the path, and conftest points it at
a per-test file so no suite ever writes the developer's real identity.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

#: Env override -- the test-isolation seam every global store uses.
IDENTITY_DB_ENV = "AIDOCS_IDENTITY_DB"

#: Tables that belong to the operator, not to a project.
GLOBAL_TIER_TABLES: tuple[str, ...] = (
    "identity_users",
    "identity_tokens",
    "rbac_roles",
    "rbac_permissions",
    "rbac_role_permissions",
    "rbac_user_roles",
    "rbac_scoped_role_permissions",
    "rbac_user_permission_overrides",
    "rbac_cache_version",
)

#: Tables that stay with the project they describe (documented so the split
#: is reviewable in one place; this module never touches them).
PROJECT_TIER_TABLES: tuple[str, ...] = (
    "session_freeze",
    "rbac_escalations",
    "rbac_escalation_grants",
    "protected_file_registry",
    "dnt_banners_shown",
    "agent_memory_compaction_state",
    # #516: the tenant RBAC one-shot bootstrap STAMP. "Has THIS project been
    # bootstrapped?" is a project fact; in the global home its single bare key
    # was shared by every project and closed the heal-forward bridge after the
    # first stamp.
    "rbac_bootstrap_meta",
)


def identity_db_path(project_root: Path | str | None = None) -> Path:
    """The machine-global identity home.

    ``project_root`` is accepted (and ignored) so the store call sites read
    naturally and so a future per-tenant split has an obvious seam.
    """
    override = str(os.environ.get(IDENTITY_DB_ENV) or "").strip()
    if override:
        return Path(override)
    # Named aidocs_identity.sqlite3, NOT identity.sqlite3: the protected-path
    # classifier shields `aidocs[a-z_]*\.sqlite3?$`, so the operator's
    # credential store must keep the prefix or the move would quietly strip
    # its raw-read/raw-write protection (caught in review, pinned by
    # test_identity_home_is_protected_by_path_classifier).
    return Path.home() / ".aidocs" / "aidocs_identity.sqlite3"


def legacy_project_identity_path(project_root: Path | str) -> Path:
    """Where identity USED to live, per project (pre-#488)."""
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"


#: Tables that belong to ONE TENANT HOME (an org's home directory, or the
#: shared auth home) and MUST NOT be pooled into the machine-global file.
#: #516: these rode on IdentityStore().db_path and silently went global with
#: #488, which listed a foreign org's projects to a non-member and attributed
#: a project to the wrong org. Their isolation is FILE isolation — they carry
#: no scope columns — so the file must stay per-home.
#:
#: #528: `project_acl` was written here as `gate_project_acl` — a name no
#: CREATE TABLE in the tree ever used. Nothing read the manifest, so the
#: phantom sat unnoticed: the precise silent drift these lists exist to
#: prevent. ENFORCED by tests/security/test_tier_residency_manifests.py, which
#: creates every named table through its real initializer and asserts which
#: physical file it lands in. Adding a name here without a creator now FAILS.
TENANT_HOME_TIER_TABLES: tuple[str, ...] = (
    "gate_projects",
    "gate_selection",
    "gate_binding",
    "gate_org_selection",
    "project_acl",
)


def tenant_home_db_path(home: Path | str) -> Path:
    """The sqlite file for ONE tenant home's registries (#516).

    Same spelling as the pre-#488 per-project identity DB — the store file a
    tenant home has always used — but a DIFFERENT tier from
    ``identity_db_path``: WHO the operator is stays machine-global (#488),
    while WHICH PROJECTS AN ORG HAS is a fact about that org's home and must
    never be visible from another org's home.
    """
    return legacy_project_identity_path(home)


# Legacy files this PROCESS has already settled -- see
# adopt_legacy_project_identity for why the durable stamp alone was not enough.
_ADOPTED_THIS_PROCESS: set[str] = set()


def _table_exists(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",  # noqa: S608
        (table,),
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, table: str, schema: str = "main") -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {schema}.{table}").fetchone()[0])  # noqa: S608
    except sqlite3.Error:
        return 0


def adopt_legacy_project_identity(project_root: Path | str) -> dict[str, int]:
    """Import a project's legacy identity/authority rows into the global home.

    ONE-SHOT per legacy file (stamped in ``identity_adoptions``) and, within
    that shot, only into tables that are EMPTY globally. Both belts matter:
    the empty-target rule never overwrites live rows, and the stamp makes
    adoption a single historical event — without it, deleting the last user
    empties the table and the next init would resurrect the very account the
    operator removed.

    Returns ``{table: rows_copied}`` for whatever moved (empty dict when
    nothing did). Fail-quiet by contract: identity must keep working even if
    a legacy file is corrupt, locked, or half-written — a failed import
    leaves the global home exactly as it was.
    """
    legacy = legacy_project_identity_path(project_root)
    if not legacy.is_file():
        return {}
    target = identity_db_path(project_root)
    if not target.is_file():
        return {}
    # PER-PROCESS SHORT-CIRCUIT. The sqlite `identity_adoptions` stamp makes
    # adoption one-shot in EFFECT, but every caller still had to OPEN the
    # global DB and read the stamp to discover that -- and init_db runs on
    # every store construction. MEASURED 2026-08-05: 44 opens of
    # aidocs_identity.sqlite3 inside ONE tool call, all of them re-reading a
    # stamp that said "already done". Adoption is a historical event: once
    # this process has settled a legacy file, no later call in the same
    # process can have anything to do. The durable stamp remains the real
    # guard for FRESH processes; this only skips re-asking a question already
    # answered.
    legacy_seen_key = str(Path(legacy).resolve()).replace("\\", "/")
    if legacy_seen_key in _ADOPTED_THIS_PROCESS:
        return {}
    if Path(target).resolve() == Path(legacy).resolve():
        return {}  # override points AT the legacy file -- nothing to move
    moved: dict[str, int] = {}
    legacy_key = str(Path(legacy).resolve()).replace("\\", "/")
    # #756: `with sqlite3.connect(p) as conn:` is sqlite3's TRANSACTION context
    # manager -- it commits or rolls back and NEVER closes the handle, and these
    # connections sit in reference cycles, so only a gen-2 collection frees them.
    # Ownership is therefore explicit: the `with` keeps the transaction exactly
    # as it was, `finally` releases the handle. NOT routed through
    # _sqlite_connect.connect deliberately: that turns foreign_keys ON, and this
    # block bulk-copies whole tables between an ATTACHed legacy file and main in
    # GLOBAL_TIER_TABLES order. Enforcing FKs mid-adoption could refuse rows the
    # old file legitimately holds, which is a behaviour change, not a leak fix.
    #
    # RE-CHECKED 2026-08-26 (#755 sweep) and CONFIRMED AS A PERMANENT
    # EXCEPTION, not a to-do. foreign_keys=ON is the one pragma the canonical
    # connect refuses to fail open on -- deliberately, because it is
    # correctness rather than performance -- so there is no way to reach the
    # helper's other pragmas here without also taking that one. The lifecycle
    # debt (#756) is already paid by hand below: `conn = ...` with an explicit
    # `finally: conn.close()`, so this raw call leaks nothing. Recorded as an
    # exception in tests/runtime/test_sqlite_connect_chokepoint.py so the next
    # sweep does not spend a pass re-deriving it -- and a forced migration
    # that breaks adoption would be worse than the missing pragmas.
    conn = sqlite3.connect(str(target))
    try:
        with conn:
            # PER-TABLE stamp, not per-file: identity_store and rbac_store
            # initialize independently, so a file-level stamp written by the
            # identity pass would lock the authority tables out of adoption
            # forever. Per-table is also exactly the resurrection guard —
            # once identity_users has been adopted, deleting the last user
            # can never re-import it.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS identity_adoptions (
                    legacy_path TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    adopted_at TEXT NOT NULL,
                    rows_copied INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (legacy_path, table_name)
                )
            """)
            done = {
                str(r[0])
                for r in conn.execute(
                    "SELECT table_name FROM identity_adoptions WHERE legacy_path = ?",
                    (legacy_key,),
                )
            }
            conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
            try:
                for table in GLOBAL_TIER_TABLES:
                    if table in done:
                        continue  # adopted once already — never again
                    if not _table_exists(conn, table, "main"):
                        continue  # the owning store has not created it yet
                    if not _table_exists(conn, table, "legacy"):
                        continue
                    if _row_count(conn, table, "main"):
                        continue  # already populated -- never overwrite
                    src_rows = _row_count(conn, table, "legacy")
                    if not src_rows:
                        continue
                    conn.execute(
                        f"INSERT OR IGNORE INTO main.{table} SELECT * FROM legacy.{table}"  # noqa: S608
                    )
                    copied = _row_count(conn, table, "main")
                    conn.execute(
                        "INSERT OR IGNORE INTO identity_adoptions "
                        "(legacy_path, table_name, adopted_at, rows_copied) "
                        "VALUES (?, ?, ?, ?)",
                        (legacy_key, table, _iso_now(), copied),
                    )
                    if copied:
                        moved[table] = copied
                conn.commit()
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("DETACH DATABASE legacy")
    except sqlite3.Error:
        # Do NOT mark settled: a locked or half-written legacy file may still
        # have rows to give, and the next call should be free to retry.
        return {}
    finally:
        # OWNERSHIP (#756). Without this the handle -- plus its -wal and -shm
        # siblings -- outlives the call and PINS the legacy identity file.
        conn.close()
    _ADOPTED_THIS_PROCESS.add(legacy_seen_key)
    return moved
