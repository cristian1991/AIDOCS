"""Empire-level intent token store — replaces intent_tokens/*.toml.

Per empire-doctrine §XI + Empire-directive 2026-05-05: NLP intent vocabulary
(tool aliases, domain aliases, approve/deny lemma sets) is empire content
and lives in SQL, not in fragmented TOML files. One table, one matcher,
one migration unit.

Schema:
  intent_lemma_sets(lang, kind, parent_key, parent_mode, token, source)

Where `kind` ∈ {
  approve_verb, deny_verb, scopeless_accept, scopeless_deny,
  second_person, first_person, negation, tool_alias, domain_alias,
}
and `parent_key` carries the tool_name for tool_alias rows, the domain
key for domain_alias rows, and "" for everything else.

`parent_mode` is reserved for mode-aware tool surfacing (e.g. surfacing
ai_task(mode='begin') vs just ai_task). Empty today; populated when the
matcher learns mode-aware emission.

Empire DB path: ~/.aidocs/empire.sqlite3 (overridable via AIDOCS_EMPIRE_DB).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

_LEMMA_KINDS = (
    # Grant detector (intent_grant_detector.py — already migrated).
    "approve_verb",
    "deny_verb",
    "scopeless_accept",
    "scopeless_deny",
    "second_person",
    "first_person",
    "negation",
    "tool_alias",
    "domain_alias",
    # intent_guard + adjacent consumers (foundation 2026-05-13 —
    # consumer rewires land in follow-up turns).
    "action_token",  # bare action verbs: edit, git_commit, git_push, understand
    "intent_guard",  # __intent_guard_{write,bash,destructive} categories
    "plan_vague_pattern",  # __plan_validation_vague_patterns
    "intent_phrase",  # __intent_phrases.<name> with attrs.requires_scope etc.
    "skill_trigger",  # __skill_trigger_<name>, parent_mode = 'intent'|'workflow'
    "domain_hint",  # __domain_hint_<name>, attrs.tools = "..."
    "memory_route",  # memory_routing.<name>, attrs.target = "rules/x.md"
    "tool_discovery",  # __tool_discovery_<name>, attrs.tools = [tool1, tool2, ...]
    "soul_lineage",  # #167 Phase 3: sovereign-soul evocation shapes (parent_key=soul_id,
    #                  parent_mode=phrase|anchor|context|write_verb)
)


# ── schema-ensure-once + request-scoped read cache ─────────────────
#
# Two independent optimizations for the disposable hook process (and the
# long-lived MCP server, per-thread):
#
#   1. _SCHEMA_READY: maps db-path -> file IDENTITY ((st_dev, st_ino)) whose
#      schema has been ensured AND seeded in THIS process. init_db() no-ops only
#      when the path is present AND the file identity still matches — so if the
#      db file at the same path is deleted/replaced, the stale entry no longer
#      matches and the schema is re-ensured (identity-safe). The cheap stat is
#      stable across writes (inode doesn't change on content writes) so the
#      once-only guard still holds for normal operation. A path is recorded ONLY
#      after a stable init: a transient executescript failure propagates and a
#      transient factory-seed failure is fail-open (see init_db) — either way the
#      entry is absent, so the next call retries.
#
#   2. request_cache(): a thread-local window that reuses ONE empire-DB
#      connection and memoizes get_rows_by_kind by (db, lang, kind,
#      include_attrs). Cross-process truth is preserved with `PRAGMA
#      data_version`, read on the SAME reused connection (the only way its value
#      is comparable across calls): if any OTHER connection — another process,
#      or a local writer on its own connection — commits, data_version advances
#      and the memo entry is rejected. Local mutations additionally clear the
#      memo explicitly. Outside a request window, reads keep the original
#      per-call-connection, no-memo behavior unchanged.
_SCHEMA_READY: dict[str, tuple[int, int]] = {}

# Sentinel returned by _seed_from_factory_if_empty on a transient (retryable)
# failure: init_db stays fail-open and does NOT record the path.
_SEED_RETRY = -1


def _db_identity(db: Path) -> tuple[int, int] | None:
    """Stable file identity (st_dev, st_ino) for the empire db, or None if the
    file does not exist yet. Inode is stable across content writes but changes
    when the file is deleted/replaced — exactly what the schema-ready guard needs
    to stay correct under same-path replacement."""
    try:
        st = db.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


_request_state = threading.local()


class _RequestCache:
    __slots__ = ("conn", "conn_bind", "memo", "seed_failed")

    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None
        # (db_path, file_identity) the open connection is bound to. Identity is
        # always non-None once bound (the file exists after connect).
        self.conn_bind: tuple[str, tuple[int, int]] | None = None
        self.memo: dict[tuple, tuple[int, list[dict]]] = {}
        # (db_path, file_identity) pairs whose factory-seed already failed THIS
        # request — suppresses repeat seed attempts within the request (a new
        # request retries). Keyed by identity too so a same-path db REPLACEMENT
        # mid-request does NOT inherit the old file's suppression.
        self.seed_failed: set[tuple[str, tuple[int, int] | None]] = set()


def _active_request() -> "_RequestCache | None":
    return getattr(_request_state, "cache", None)


@contextmanager
def request_cache():
    """Open a request-scoped empire-DB read cache for the duration of the block.
    Reentrant: a nested call reuses the outer window. The reused connection is
    closed and the memo dropped on exit, so nothing leaks past the request."""
    existing = _active_request()
    if existing is not None:
        yield existing
        return
    cache = _RequestCache()
    _request_state.cache = cache
    try:
        yield cache
    finally:
        _request_state.cache = None
        if cache.conn is not None:
            try:
                cache.conn.close()
            except Exception:
                pass


def _request_conn() -> "sqlite3.Connection | None":
    cache = _active_request()
    if cache is None:
        return None
    db_path = str(empire_db_path())
    ident = _db_identity(Path(db_path))
    # Reopen (and drop the now-untrustworthy memo) if the bound db path changed,
    # or the file identity changed/vanished — so a mid-request path switch or
    # same-path replacement can never be read through a connection pinned to the
    # old file. data_version alone can't catch a file swap; the identity bind can.
    if cache.conn is not None and (
        cache.conn_bind is None
        or ident is None
        or cache.conn_bind[0] != db_path
        or cache.conn_bind[1] != ident
    ):
        try:
            cache.conn.close()
        except Exception:
            pass
        cache.conn = None
        cache.conn_bind = None
        cache.memo.clear()
    if cache.conn is None:
        conn = sqlite3.connect(db_path)
        post_ident = _db_identity(Path(db_path))  # non-None: the file exists post-connect
        cache.conn = conn
        cache.conn_bind = (db_path, post_ident) if post_ident is not None else None
    return cache.conn


def _invalidate_request_memo() -> None:
    """Drop the request-scoped read memo after a local token mutation, so a
    write made in THIS request is immediately visible to subsequent reads even
    on the reused connection (whose own PRAGMA data_version would not advance
    for a write committed on a different connection in the same process is
    covered too, but this is the explicit contract)."""
    cache = _active_request()
    if cache is not None:
        cache.memo.clear()


def empire_db_path() -> Path:
    """Return the empire sqlite path. Honors AIDOCS_EMPIRE_DB env override
    so tests can isolate without mutating ~/.aidocs/empire.sqlite3.
    """
    override = os.environ.get("AIDOCS_EMPIRE_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "empire.sqlite3"


def init_db() -> None:
    """Create empire intent-vocab tables if missing. Idempotent.

    Two tables:
      intent_lemma_sets   — token lists for the matcher (grant tokens,
                            action tokens, intent_guard, skill triggers,
                            domain hints, memory routes, plan vague
                            patterns, intent phrases). `attrs` carries
                            structured extras (requires_scope, target
                            path, tools-recommendation string).
      gate_message_strings — refusal text + tool description hints
                            keyed by (key, lang).
    """
    db = empire_db_path()
    db_key = str(db)
    # Schema-ensure-once (identity-guarded): skip the executescript + factory-probe
    # storm once this process has fully initialized THIS db file. The guard matches
    # only when the file identity is unchanged, so a same-path replacement forces a
    # re-ensure. A transient executescript failure propagates (retryable); a
    # transient factory-seed failure is fail-open (retryable) — neither records the
    # path, so the next call retries.
    ident = _db_identity(db)
    # Skip only when the file EXISTS (non-None identity) AND matches the recorded
    # identity. _SCHEMA_READY never holds a None identity, so a missing/replaced
    # file (ident None or mismatch) always re-ensures.
    if ident is not None and _SCHEMA_READY.get(db_key) == ident:
        return
    # Per-request seed-failure suppression: once the factory seed has failed for
    # this db this request, do not re-run init (schema DDL already ran on the first
    # attempt this request; only the seed was missing). A new request retries.
    cache = _active_request()
    if cache is not None and (db_key, ident) in cache.seed_failed:
        return
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS intent_lemma_sets (
                lang        TEXT NOT NULL,
                kind        TEXT NOT NULL,
                parent_key  TEXT NOT NULL DEFAULT '',
                parent_mode TEXT NOT NULL DEFAULT '',
                token       TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'seed',
                PRIMARY KEY (lang, kind, parent_key, parent_mode, token)
            );
            CREATE INDEX IF NOT EXISTS idx_intent_lemma_lookup
                ON intent_lemma_sets(lang, kind);

            CREATE TABLE IF NOT EXISTS gate_message_strings (
                key      TEXT NOT NULL,
                lang     TEXT NOT NULL DEFAULT 'en',
                body     TEXT NOT NULL,
                source   TEXT NOT NULL DEFAULT 'seed',
                PRIMARY KEY (key, lang)
            );
            """,
        )
        # Idempotent column adds — sqlite has no IF NOT EXISTS for
        # ALTER. Catch the duplicate-column error to stay idempotent.
        for ddl in (
            "ALTER TABLE intent_lemma_sets ADD COLUMN attrs TEXT NOT NULL DEFAULT '{}'",
            # Phase 6a (2026-05-14): spaCy-enrichable columns. `pos`
            # is the canonical part-of-speech tag (VERB / NOUN / ADJ
            # / PROPN / ...) computed by NLPService.enrich_token.
            # Empty string = unenriched. POS-aware matchers (Phase 6c)
            # use this to filter "edit" as VERB out of observational
            # "the edit was approved." prompts. `centroid_blob` is the
            # token's vector embedding (float32 bytes) for similarity
            # fallback when literal/lemma match fails. NULL = no vector
            # model loaded for this lang.
            "ALTER TABLE intent_lemma_sets ADD COLUMN pos TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE intent_lemma_sets ADD COLUMN centroid_blob BLOB DEFAULT NULL",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    # Fresh install: seed the full vocabulary from the bundled FACTORY DB
    # (sqlite shipped in the package), NOT from loose TOML files. SQL is
    # canonical — TOMLs are build-source only. Idempotent: copies only when
    # intent_lemma_sets is empty.
    seed_result = _seed_from_factory_if_empty(db)
    # #298 (SSOT-05 #386): repair known-stale seeded strings in ALREADY-seeded
    # live DBs (the factory copy above only runs on empty DBs). Guarded +
    # idempotent — matches the exact stale body only, so an operator-customized
    # row is never clobbered.
    _repair_stale_seed_rows(db)
    # Fail-open retry: a transient seed failure returns the _SEED_RETRY sentinel
    # (-1). We do NOT abort the UPS (schema DDL already succeeded; the hardcoded
    # en_fallback covers reads). On a stable result (>=0) record the path — but
    # only with a NON-None identity (never cache a None schema identity). On
    # failure, mark this db seed-failed for the request so we don't re-attempt the
    # seed again this request; a new request retries.
    if seed_result >= 0:
        post_ident = _db_identity(db)
        if post_ident is not None:
            _SCHEMA_READY[db_key] = post_ident
    elif cache is not None:
        # Re-evaluate identity AFTER executescript (the db file now exists, even
        # if it didn't at the top of this call) so the suppression key matches the
        # identity later calls will observe — and so a same-path replacement (new
        # identity) is NOT suppressed.
        cache.seed_failed.add((db_key, _db_identity(db)))


# #298 (SSOT-05 #386, 2026-07-18): the seed's edit-block reroute string named
# the 2026-05-12-deregistered edit tools (ai_str_replace / ai_anchor_replace /
# ai_batch_str_replace / ai_edit_lines). The agent-facing door is
# ai_replace(mode=...) + ai_batch_edit. Each entry is (key, lang, exact stale
# body, replacement) — the exact-body guard keeps the migration idempotent and
# never overwrites an operator-customized row.
_STALE_SEED_ROW_REPAIRS: tuple[tuple[str, str, str, str], ...] = (
    (
        "interaction.raw_tool_replacements.edit",
        "en",
        "ai_str_replace / ai_anchor_replace / ai_batch_str_replace "
        "(ai_edit_lines only as last resort for line-range-only edits)",
        "ai_replace(mode='string'|'anchor'|'symbol'|'lines') — one edit door; "
        "many edits at once → ai_batch_edit",
    ),
    # #92 tail: retired edit-tool names in operator-facing description rows.
    (
        "tool_descriptions.ai_insert_lines",
        "en",
        "Line-indexed insert. LAST RESORT. Prefer ai_anchor_replace. "
        "Same per-file-per-turn lock.",
        "Line-indexed insert. LAST RESORT. Prefer ai_replace(mode='anchor'). "
        "Same per-file-per-turn lock.",
    ),
    (
        "tool_descriptions.ai_edit_lines",
        "en",
        "Line-range edit. LAST RESORT. Prefer ai_str_replace/ai_anchor_replace/"
        "ai_batch_edit. AST-validates Python/JS/TS/JSON/TOML/XML. "
        "One call per file per turn.",
        "Line-range edit. LAST RESORT. Prefer ai_replace(mode='string'|'anchor') "
        "or ai_batch_edit. AST-validates Python/JS/TS/JSON/TOML/XML. "
        "One call per file per turn.",
    ),
    (
        "tool_descriptions.ai_str_replace",
        "en",
        "DEPRECATED: deregistered from MCP surface 2026-05-12. Use "
        "ai_replace(mode='string'); over cap → ai_replace(mode='anchor'); "
        "many edits at once → ai_batch_str_replace.",
        "DEPRECATED: deregistered from MCP surface 2026-05-12. Use "
        "ai_replace(mode='string'); over cap → ai_replace(mode='anchor'); "
        "many edits at once → ai_batch_edit(mode='string').",
    ),
)


# #83 hard merge (2026-07-18, no-alias law): tool_alias lemma rows pointing
# the 'todo'/'checklist' vocabulary at the REMOVED ai_todo name re-point at
# ai_task (whose todo modes now own that surface). UPDATE OR IGNORE + guarded
# DELETE keeps the migration idempotent even if an equal (lang, kind, token)
# row already exists under the new parent.
_STALE_LEMMA_PARENT_REPAIRS: tuple[tuple[str, str], ...] = (
    ("ai_todo", "ai_task"),
)


def _repair_stale_seed_rows(db: Path) -> int:
    """Guarded idempotent migration (#298, the #151 pattern): UPDATE known-stale
    seeded gate_message_strings rows in the LIVE empire DB when the body still
    byte-matches the retired text; re-point stale tool_alias lemma parents
    (#83). Returns rows repaired. Fail-open on sqlite errors (same posture as
    the factory seed — a transient failure must never abort the UPS)."""
    try:
        with sqlite3.connect(str(db)) as conn:
            repaired = 0
            for key, lang, stale, replacement in _STALE_SEED_ROW_REPAIRS:
                repaired += conn.execute(
                    "UPDATE gate_message_strings SET body = ? "
                    "WHERE key = ? AND lang = ? AND body = ?",
                    (replacement, key, lang, stale),
                ).rowcount
            for old_parent, new_parent in _STALE_LEMMA_PARENT_REPAIRS:
                repaired += conn.execute(
                    "UPDATE OR IGNORE intent_lemma_sets SET parent_key = ? "
                    "WHERE parent_key = ? AND kind = 'tool_alias'",
                    (new_parent, old_parent),
                ).rowcount
                # OR IGNORE skips rows whose (lang,kind,token) already exists
                # under the new parent — those duplicates are dropped, so the
                # stale name can never resurface from the vocab tables.
                repaired += conn.execute(
                    "DELETE FROM intent_lemma_sets "
                    "WHERE parent_key = ? AND kind = 'tool_alias'",
                    (old_parent,),
                ).rowcount
            conn.commit()
        if repaired:
            _invalidate_request_memo()
        return repaired
    except sqlite3.Error:
        return 0


def factory_db_path() -> Path:
    """Path to the bundled factory empire DB (full intent-token + gate-message
    vocabulary, shipped as package data). The SQL-canonical replacement for
    seeding from seed/intent_tokens/*.toml + seed/gate_messages/*.toml.
    """
    return Path(__file__).resolve().parent / "seed" / "factory.sqlite3"


def _seed_from_factory_if_empty(db: Path) -> int:
    """Copy the factory vocabulary into an EMPTY live empire DB. No-op once
    seeded. Returns rows copied. Never reads TOML.
    """
    factory = factory_db_path()
    if not factory.is_file():
        # Degraded path: a fresh/empty empire DB with NO factory DB shipped
        # means vocabulary falls back to the hardcoded en_fallback (English
        # only, partial). That's a packaging bug — be LOUD so it's noticed
        # rather than silently shipping degraded multilingual vocab.
        try:
            with sqlite3.connect(str(db)) as _c:
                _empty = _c.execute("SELECT 1 FROM intent_lemma_sets LIMIT 1").fetchone() is None
        except sqlite3.Error:
            _empty = True
        if _empty:
            import sys as _sys

            print(
                "[AIDOCS][WARN] factory vocabulary DB missing at "
                f"{factory} — empire intent vocab will degrade to the "
                "hardcoded en_fallback. The wheel/sdist must ship "
                "aidocs_mcp/seed/factory.sqlite3 (see "
                "test_factory_db_packaging.py).",
                file=_sys.stderr,
            )
        return 0
    try:
        with sqlite3.connect(str(db)) as conn:
            if conn.execute("SELECT 1 FROM intent_lemma_sets LIMIT 1").fetchone() is not None:
                return 0  # already seeded — sqlite is canonical
            conn.execute("ATTACH DATABASE ? AS factory", (str(factory),))
            try:
                n1 = conn.execute(
                    "INSERT OR IGNORE INTO intent_lemma_sets "
                    "(lang,kind,parent_key,parent_mode,token,source,attrs,pos,centroid_blob) "
                    "SELECT lang,kind,parent_key,parent_mode,token,source,attrs,pos,centroid_blob "
                    "FROM factory.intent_lemma_sets",
                ).rowcount
                n2 = conn.execute(
                    "INSERT OR IGNORE INTO gate_message_strings (key,lang,body,source) "
                    "SELECT key,lang,body,source FROM factory.gate_message_strings",
                ).rowcount
                conn.commit()
            finally:
                conn.execute("DETACH DATABASE factory")
            _invalidate_request_memo()
            return int(n1) + int(n2)
    except sqlite3.Error:
        # Transient/unknown copy failure (locked db, I/O hiccup): FAIL-OPEN +
        # RETRYABLE. Return the _SEED_RETRY sentinel so init_db neither aborts the
        # UPS nor records the path — the next call re-attempts the seed. The
        # hardcoded en_fallback gives a minimal working vocab meanwhile. (The
        # genuinely degraded "no factory file shipped" case returns 0 above and is
        # a stable, non-retryable state.)
        return _SEED_RETRY


def has_any_rows(lang: str = "") -> bool:
    """Return True if intent_lemma_sets has at least one row (optionally
    scoped to a lang). Used to decide whether to seed from TOML.
    """
    init_db()
    sql = "SELECT 1 FROM intent_lemma_sets"
    params: tuple = ()
    if lang:
        sql += " WHERE lang = ?"
        params = (lang.strip().lower(),)
    sql += " LIMIT 1"
    with sqlite3.connect(str(empire_db_path())) as conn:
        return conn.execute(sql, params).fetchone() is not None


def _insert_tokens(
    conn: sqlite3.Connection,
    lang: str,
    kind: str,
    parent_key: str,
    parent_mode: str,
    tokens: Iterable[str],
    source: str = "seed",
    attrs: dict | None = None,
) -> int:
    """Insert tokens with INSERT OR IGNORE. Returns rows actually added.

    attrs (optional): structured extras serialized as JSON. Applies to
    every row in this batch (use one call per attrs-bearing group —
    intent_phrases with requires_scope, domain_hints with tools-rec,
    memory_routes with target_path).
    """
    import json as _json

    if kind not in _LEMMA_KINDS:
        raise ValueError(f"unknown lemma kind: {kind}")
    attrs_blob = _json.dumps(attrs or {}, sort_keys=True, separators=(",", ":"))
    rows = [
        (lang, kind, parent_key, parent_mode, str(t).strip().lower(), source, attrs_blob)
        for t in tokens
        if t and str(t).strip()
    ]
    if not rows:
        return 0
    cur = conn.executemany(
        """INSERT OR IGNORE INTO intent_lemma_sets
           (lang, kind, parent_key, parent_mode, token, source, attrs)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    # Local token mutation — drop any request-scoped read memo so the write is
    # visible to subsequent reads in the same request.
    _invalidate_request_memo()
    return cur.rowcount or 0


# ── Generic typed read API ──────────────────────────────────────────
#
# Consumer code (intent_guard, skill_trigger, domain hints, memory
# routing) reads via these helpers. Each returns the row shape
# consumers actually want — list of dicts or flat dict — so the
# consumer doesn't have to rebuild structure from raw rows.
#
# Foundation 2026-05-13: these are the read APIs. Consumer rewires
# (replacing TOML loaders) land in the next turn.


def get_rows_by_kind(
    lang: str,
    kind: str,
    *,
    include_attrs: bool = False,
) -> list[dict]:
    """Return all rows for a (lang, kind) pair. When include_attrs=True
    the attrs JSON blob is parsed and merged into each dict.
    """
    import json as _json

    init_db()
    lang = (lang or "en").strip().lower()
    cols = "parent_key, parent_mode, token, source"
    if include_attrs:
        cols += ", attrs"
    sql = f"SELECT {cols} FROM intent_lemma_sets WHERE lang = ? AND kind = ?"

    def _build(rows: list) -> list[dict]:
        built: list[dict] = []
        for r in rows:
            d: dict = {
                "parent_key": r[0],
                "parent_mode": r[1],
                "token": r[2],
                "source": r[3],
            }
            if include_attrs:
                try:
                    d["attrs"] = _json.loads(r[4] or "{}")
                except Exception:
                    d["attrs"] = {}
            built.append(d)
        return built

    cache = _active_request()
    if cache is None:
        # No request window: original per-call connection, no memo.
        with sqlite3.connect(str(empire_db_path())) as conn:
            rows = conn.execute(sql, (lang, kind)).fetchall()
        return _build(rows)

    # Request window: reuse the shared connection + data_version-guarded memo.
    conn = _request_conn()
    db = str(empire_db_path())
    # data_version is read on the SAME reused connection — the only way its value
    # is comparable across calls. It advances iff another connection (other
    # process, or a local writer on its own connection) committed since last read.
    data_version = conn.execute("PRAGMA data_version").fetchone()[0]
    key = (db, lang, kind, include_attrs)
    hit = cache.memo.get(key)
    if hit is not None and hit[0] == data_version:
        # The memo stores the RAW immutable row tuples; rebuild fresh dicts (with a
        # fresh attrs object parsed per call) on every hand-out, so a caller that
        # mutates a returned row — or its nested attrs dict — can never poison the
        # cache or another caller's copy.
        return _build(hit[1])
    rows = conn.execute(sql, (lang, kind)).fetchall()
    cache.memo[key] = (data_version, list(rows))
    return _build(rows)


# ── Generic seed API ───────────────────────────────────────────────


def seed_kind_rows(
    lang: str,
    kind: str,
    rows: Iterable[dict],
    *,
    source: str = "seed",
) -> int:
    """Bulk-seed rows of one kind. Each row dict: {
        parent_key?, parent_mode?, tokens: list[str], attrs?: dict
    }. Returns total inserted.
    """
    if kind not in _LEMMA_KINDS:
        raise ValueError(f"unknown lemma kind: {kind}")
    init_db()
    total = 0
    lang_ = (lang or "en").strip().lower()
    with sqlite3.connect(str(empire_db_path())) as conn:
        for r in rows:
            total += _insert_tokens(
                conn,
                lang_,
                kind,
                r.get("parent_key", "") or "",
                r.get("parent_mode", "") or "",
                r.get("tokens", []) or [],
                source,
                r.get("attrs") or None,
            )
        conn.commit()
    return total


# ── gate_message_strings API ───────────────────────────────────────


def seed_gate_message(
    key: str,
    body: str,
    *,
    lang: str = "en",
    source: str = "seed",
) -> bool:
    """Insert one gate-message row. Returns True if a new row was added."""
    init_db()
    with sqlite3.connect(str(empire_db_path())) as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO gate_message_strings
               (key, lang, body, source) VALUES (?, ?, ?, ?)""",
            (key.strip(), (lang or "en").lower(), body, source),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def get_action_verb_tokens(lang: str = "en") -> set[str]:
    """Return the distinct token strings under kind='action_token' for
    one lang. These are by definition verb-domain (action_token =
    classification of operator-uttered action verbs). detect_grant's
    POS-filter uses this set to know which tool aliases require VERB
    usage in the prompt — `edit`/`fix`/`commit`/`push` etc. are
    naturally verb-y because they're action_token tokens.

    Phase 6d retry (2026-05-14): replaces the unreliable per-alias
    pos column with a stable, kind-driven verb-required signal.
    """
    init_db()
    lang_ = (lang or "en").strip().lower()
    with sqlite3.connect(str(empire_db_path())) as conn:
        rows = conn.execute(
            """SELECT DISTINCT token FROM intent_lemma_sets
               WHERE lang = ? AND kind = 'action_token'""",
            (lang_,),
        ).fetchall()
    return {r[0] for r in rows if r[0]}


def list_kinds() -> tuple[str, ...]:
    """Return the tuple of valid lemma kinds (schema constants)."""
    return _LEMMA_KINDS


def list_langs() -> list[str]:
    """Return the distinct languages that have at least one row."""
    init_db()
    with sqlite3.connect(str(empire_db_path())) as conn:
        return [
            r[0] for r in conn.execute("SELECT DISTINCT lang FROM intent_lemma_sets ORDER BY lang")
        ]


def delete_parent_rows(
    lang: str,
    kind: str,
    parent_key: str,
    parent_mode: str = "",
) -> int:
    """Delete all rows for (lang, kind, parent_key, parent_mode).
    Used by the dashboard's 'replace this group' upsert flow.
    Returns rows deleted.
    """
    init_db()
    if kind not in _LEMMA_KINDS:
        raise ValueError(f"unknown lemma kind: {kind}")
    with sqlite3.connect(str(empire_db_path())) as conn:
        cur = conn.execute(
            """DELETE FROM intent_lemma_sets
               WHERE lang=? AND kind=? AND parent_key=? AND parent_mode=?""",
            (
                (lang or "en").lower(),
                kind,
                parent_key,
                parent_mode,
            ),
        )
        conn.commit()
        _invalidate_request_memo()
        return cur.rowcount or 0


def replace_parent_rows(
    lang: str,
    kind: str,
    parent_key: str,
    tokens: Iterable[str],
    *,
    parent_mode: str = "",
    attrs: dict | None = None,
    source: str = "operator",
) -> dict[str, int]:
    """Atomic 'replace this group': drop all existing rows for
    (lang, kind, parent_key, parent_mode), then insert the new set
    with the given tokens + attrs. Returns {deleted, inserted}.
    """
    deleted = delete_parent_rows(lang, kind, parent_key, parent_mode)
    inserted = seed_kind_rows(
        lang,
        kind,
        [
            {
                "parent_key": parent_key,
                "parent_mode": parent_mode,
                "tokens": list(tokens),
                "attrs": attrs or {},
            },
        ],
        source=source,
    )
    return {"deleted": deleted, "inserted": inserted}


def list_gate_messages(lang: str = "en") -> list[dict]:
    """Return all gate_message_strings rows for one lang as list of
    {key, body, source} dicts. Ordered by key.
    """
    init_db()
    lang_ = (lang or "en").lower()
    with sqlite3.connect(str(empire_db_path())) as conn:
        rows = conn.execute(
            "SELECT key, body, source FROM gate_message_strings WHERE lang = ? ORDER BY key",
            (lang_,),
        ).fetchall()
    return [{"key": r[0], "body": r[1], "source": r[2]} for r in rows]


def delete_gate_message(key: str, lang: str = "en") -> bool:
    """Drop one gate-message row. Returns True if a row was deleted."""
    init_db()
    with sqlite3.connect(str(empire_db_path())) as conn:
        cur = conn.execute(
            "DELETE FROM gate_message_strings WHERE key=? AND lang=?",
            (key.strip(), (lang or "en").lower()),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def upsert_gate_message(
    key: str,
    body: str,
    *,
    lang: str = "en",
    source: str = "operator",
) -> dict[str, int]:
    """Upsert: delete then insert. Returns {deleted, inserted}."""
    deleted = 1 if delete_gate_message(key, lang) else 0
    inserted = 1 if seed_gate_message(key, body, lang=lang, source=source) else 0
    return {"deleted": deleted, "inserted": inserted}


def get_gate_message(key: str, lang: str = "en") -> str | None:
    """Return the gate message body for (key, lang), or None if absent.
    Falls back to English if the requested lang has no row.
    """
    init_db()
    key_ = key.strip()
    lang_ = (lang or "en").lower()
    with sqlite3.connect(str(empire_db_path())) as conn:
        row = conn.execute(
            "SELECT body FROM gate_message_strings WHERE key=? AND lang=?",
            (key_, lang_),
        ).fetchone()
        if row:
            return row[0]
        if lang_ != "en":
            row = conn.execute(
                "SELECT body FROM gate_message_strings WHERE key=? AND lang='en'",
                (key_,),
            ).fetchone()
            if row:
                return row[0]
    return None


def seed_lang(
    lang: str,
    *,
    approve_verbs: Iterable[str] = (),
    deny_verbs: Iterable[str] = (),
    scopeless_accept: Iterable[str] = (),
    scopeless_deny: Iterable[str] = (),
    second_person: Iterable[str] = (),
    first_person: Iterable[str] = (),
    negation: Iterable[str] = (),
    tool_aliases: dict[str, Iterable[str]] | None = None,
    domain_aliases: dict[str, Iterable[str]] | None = None,
    source: str = "seed",
) -> int:
    """Bulk-seed one language. Returns total rows inserted."""
    init_db()
    lang = (lang or "en").strip().lower()
    total = 0
    with sqlite3.connect(str(empire_db_path())) as conn:
        total += _insert_tokens(conn, lang, "approve_verb", "", "", approve_verbs, source)
        total += _insert_tokens(conn, lang, "deny_verb", "", "", deny_verbs, source)
        total += _insert_tokens(conn, lang, "scopeless_accept", "", "", scopeless_accept, source)
        total += _insert_tokens(conn, lang, "scopeless_deny", "", "", scopeless_deny, source)
        total += _insert_tokens(conn, lang, "second_person", "", "", second_person, source)
        total += _insert_tokens(conn, lang, "first_person", "", "", first_person, source)
        total += _insert_tokens(conn, lang, "negation", "", "", negation, source)
        for tool_name, aliases in (tool_aliases or {}).items():
            if not tool_name or tool_name.startswith("_"):
                continue
            total += _insert_tokens(
                conn,
                lang,
                "tool_alias",
                tool_name,
                "",
                aliases,
                source,
            )
        for domain_key, terms in (domain_aliases or {}).items():
            if not domain_key or domain_key.startswith("_"):
                continue
            total += _insert_tokens(
                conn,
                lang,
                "domain_alias",
                domain_key,
                "",
                terms,
                source,
            )
        conn.commit()
    return total


def get_lemma_sets_dict(lang: str) -> dict | None:
    """Return a dict matching LemmaSets fields, or None if the language
    has no rows. Caller hydrates into the LemmaSets dataclass.

    Shape:
      {
        "approve_verbs": frozenset(...),
        "deny_verbs": frozenset(...),
        "scopeless_accept": frozenset(...),
        "scopeless_deny": frozenset(...),
        "second_person": frozenset(...),
        "first_person": frozenset(...),
        "negation_markers": frozenset(...),
        "tool_keywords": {tool_name: frozenset(...), ...},
        "domain_keywords": {domain: frozenset(...), ...},
      }
    """
    init_db()
    lang = (lang or "en").strip().lower()
    with sqlite3.connect(str(empire_db_path())) as conn:
        rows = conn.execute(
            """SELECT kind, parent_key, token
               FROM intent_lemma_sets WHERE lang = ?""",
            (lang,),
        ).fetchall()
    if not rows:
        return None

    flat: dict[str, set[str]] = {
        k: set()
        for k in (
            "approve_verb",
            "deny_verb",
            "scopeless_accept",
            "scopeless_deny",
            "second_person",
            "first_person",
            "negation",
        )
    }
    tool_map: dict[str, set[str]] = {}
    domain_map: dict[str, set[str]] = {}
    for kind, parent_key, token in rows:
        if kind in flat:
            flat[kind].add(token)
        elif kind == "tool_alias":
            tool_map.setdefault(parent_key, set()).add(token)
        elif kind == "domain_alias":
            domain_map.setdefault(parent_key, set()).add(token)

    return {
        "approve_verbs": frozenset(flat["approve_verb"]),
        "deny_verbs": frozenset(flat["deny_verb"]),
        "scopeless_accept": frozenset(flat["scopeless_accept"]),
        "scopeless_deny": frozenset(flat["scopeless_deny"]),
        "second_person": frozenset(flat["second_person"]),
        "first_person": frozenset(flat["first_person"]),
        "negation_markers": frozenset(flat["negation"]),
        "tool_keywords": {t: frozenset(a) for t, a in tool_map.items()},
        "domain_keywords": {d: frozenset(a) for d, a in domain_map.items()},
    }


# seed_lang_from_toml / migrate_all_tomls REMOVED 2026-05-21: the seed
# TOMLs were deleted (SQL-canonical). Fresh installs seed from the bundled
# factory.sqlite3 via init_db()/_seed_from_factory_if_empty; vocab edits go
# through seed_lang / seed_kind_rows (operator) against the empire DB. No
# code path reads intent_tokens/*.toml any more.


