"""Keyword lemma lexicon — lemmas paid for on the WRITE edge, not per prompt.

#688. The memory surfacer used to run the FULL spaCy pipeline ONCE PER
CANDIDATE KEYWORD (profiled: 1022 ``Language.__call__`` / 8.3s per prompt,
reproduced 2026-08-01 at 1052 invocations / 11.2s for 1000 keywords). The
asymmetry the old design ignored:

  * the PROMPT is new every time  -> lemmatise it ONCE per hook,
  * the KEYWORDS are STATIC       -> lemmatise them ONCE when the route is
    registered, persist the lemmas, and match by SET INTERSECTION.

No cache sits on the hot path, because nothing is left to cache: the memo it
replaces (``_KW_LEMMA_CACHE``) could never work — every hook is a fresh
process, and within one prompt each keyword is a distinct key, so it had no
hits to give.

TWO CORRECTNESS PROPERTIES, built in from the start:

1. MODEL STAMP. Every stored row records the model that produced it
   (``en_core_web_sm@3.8.0``). A row whose stamp differs from the live model's
   is STALE and is NEVER served — it is excluded from the fresh map, reported,
   and recomputed (bounded per call, then persisted). A model upgrade can
   therefore cost one slow-ish prompt; it can never cause a silent, fail-quiet
   MISS where a memory route simply stops matching.
2. PER-LANGUAGE KEYS. A keyword's lemma differs per language pack (#680 seeded
   Romanian), so the primary key is ``(keyword, language)``. A language with no
   rows reads as EMPTY — never as another language's lemmas.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# #755/#756: the ONE canonical connect. Both sites opened a handle with
# no pragmas at all (foreign_keys OFF, no busy_timeout) and both are
# closed by hand in their existing finally blocks.
# DURABILITY: RUNTIME. This table is a LEMMA CACHE and nothing else --
# the module's own docstring says a busy DB 'costs recall of the
# OPTIMISATION, never correctness', and every row is re-derivable by
# re-lemmatising. Neither site can be read_only: load_lexicon calls
# ensure_table, which is a CREATE TABLE IF NOT EXISTS.
from ._sqlite_connect import connect as _canonical_connect

# How many keyword lemmatisations one read may pay for when the lexicon is
# cold or was invalidated by a model change. Bounds the worst case (a first
# prompt after a model upgrade) while keeping the surfacer AVAILABLE: a faster
# surfacer that surfaces LESS is a regression, not a fix. Steady state is 0.
LEMMA_REPAIR_BUDGET = 64

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS keyword_lemmas (
    keyword     TEXT NOT NULL,
    language    TEXT NOT NULL,
    lemmas      TEXT NOT NULL DEFAULT '[]',
    model_stamp TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (keyword, language)
)
"""

_UNKNOWN_STAMP = "unknown"


def language_for_locale(locale: str) -> str:
    """Route locales are '*' (any) or a language tag. '*' means the pack the
    runtime actually reads with, which is 'en' today (#680: detect_language
    returns 'en' unless the operator configures "auto"). A locale that names a
    language is honoured as-is, so a Romanian route stores Romanian lemmas.
    """
    tag = str(locale or "").strip().lower()
    if not tag or tag == "*":
        return "en"
    return tag.split("-", 1)[0].split("_", 1)[0]


def db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)


# ── the model stamp ───────────────────────────────────────────────────────


def current_model_stamp(language: str) -> str:
    """``<model_name>@<version>`` for ``language``'s pack, from package
    metadata — resolvable WITHOUT loading the model.

    Returns ``"<model>@unknown"`` when the version cannot be read and
    ``"unknown"`` when the language has no pack at all. Those degrade
    honestly: a constant stamp means staleness cannot be *detected*, never
    that a known-stale row is served.
    """
    try:
        from .aidocs_nlp.language_registry import pack_for

        pack = pack_for(language)
    except Exception:
        pack = None
    if pack is None:
        return _UNKNOWN_STAMP
    model_name = str(getattr(pack, "model_name", "") or "") or _UNKNOWN_STAMP
    try:
        from importlib.metadata import version

        return f"{model_name}@{version(model_name)}"
    except Exception:
        return f"{model_name}@{_UNKNOWN_STAMP}"


# ── computing lemmas (the write edge pays this) ───────────────────────────


def compute_lemmas(text: str, project_root: Path, language: str) -> frozenset[str] | None:
    """spaCy lemma set for ``text`` under ``language``'s pack.

    ``None`` (not an empty set) when NLP is unavailable/timed out, so callers
    can refuse to PERSIST a bogus emptiness that would look authoritative
    forever after. ``language`` is passed explicitly, which also keeps lingua
    language detection off this path entirely (#680: ``detect_language``
    returns 'en' unconditionally unless configured to "auto", so those calls
    could not change the answer today anyway).
    """
    kw = str(text or "").strip().lower()
    if not kw:
        return None
    try:
        from .aidocs_nlp.service import get_service

        svc = get_service(project_root)
        doc = svc.analyze(kw, language=language)
        if doc is None:
            return None
        out: set[str] = set()
        for tok in getattr(doc, "tokens", None) or []:
            lem = str(getattr(tok, "lemma", "") or "").strip().lower()
            if lem:
                out.add(lem)
        return frozenset(out) if out else None
    except Exception:
        return None


# ── read ──────────────────────────────────────────────────────────────────


def load_lexicon(
    project_root: Path, language: str,
) -> tuple[dict[str, frozenset[str]], set[str]]:
    """One SELECT. Returns ``(fresh, stale)``:

    * ``fresh`` — {keyword: lemmas} for rows whose model stamp matches the
      live model. Safe to match against.
    * ``stale`` — keywords whose stored lemmas were produced by a DIFFERENT
      model. Reported, never served.
    """
    path = db_path(project_root)
    if not path.is_file():
        return {}, set()
    stamp = current_model_stamp(language)
    fresh: dict[str, frozenset[str]] = {}
    stale: set[str] = set()
    try:
        conn = _canonical_connect(path, timeout=2.0, row_factory=False)
        try:
            ensure_table(conn)
            rows = conn.execute(
                "SELECT keyword, lemmas, model_stamp FROM keyword_lemmas WHERE language = ?",
                (language,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}, set()
    for keyword, lemmas_json, row_stamp in rows:
        kw = str(keyword)
        if str(row_stamp) != stamp:
            stale.add(kw)  # NEVER served — the model that made it is gone.
            continue
        try:
            values = json.loads(str(lemmas_json))
        except Exception:
            stale.add(kw)
            continue
        if isinstance(values, list) and values:
            fresh[kw] = frozenset(str(v) for v in values)
    return fresh, stale


# ── write ─────────────────────────────────────────────────────────────────


def store_lemmas(
    project_root: Path,
    entries: dict[str, frozenset[str]],
    language: str,
    stamp: str | None = None,
) -> int:
    """Persist ``{keyword: lemmas}`` for ``language``. Best effort: a
    read-only or busy DB costs recall of the OPTIMISATION, never correctness —
    the caller has the lemmas it just computed either way.
    """
    if not entries:
        return 0
    path = db_path(project_root)
    if not path.parent.is_dir():
        return 0
    model_stamp = stamp if stamp is not None else current_model_stamp(language)
    try:
        conn = _canonical_connect(path, timeout=2.0, row_factory=False)
        try:
            ensure_table(conn)
            conn.executemany(
                """
                INSERT INTO keyword_lemmas (keyword, language, lemmas, model_stamp, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(keyword, language) DO UPDATE SET
                    lemmas = excluded.lemmas,
                    model_stamp = excluded.model_stamp,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [
                    (kw, language, json.dumps(sorted(lemmas)), model_stamp)
                    for kw, lemmas in entries.items()
                ],
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return len(entries)


def ensure_keywords(
    project_root: Path,
    keywords: tuple[str, ...] | list[str],
    language: str = "en",
) -> int:
    """WRITE EDGE. Lemmatise ``keywords`` under ``language`` and persist them.

    Called when a memory route is registered — that is the edge to pay on.
    Rows already stored under the CURRENT model stamp are skipped; stale ones
    are recomputed. Returns the number of rows written.
    """
    wanted = {str(k).strip().lower() for k in keywords if str(k).strip()}
    if not wanted:
        return 0
    fresh, _stale = load_lexicon(project_root, language)
    todo = [kw for kw in sorted(wanted) if kw not in fresh]
    if not todo:
        return 0
    computed: dict[str, frozenset[str]] = {}
    for kw in todo:
        lemmas = compute_lemmas(kw, project_root, language)
        if lemmas:
            computed[kw] = lemmas
    return store_lemmas(project_root, computed, language)


# ── the hot-path object ───────────────────────────────────────────────────


class KeywordLexicon:
    """Matcher for the hot path: SET INTERSECTION, zero parses.

    ``matches(keyword, prompt_lemmas)`` answers the same question the old
    ``_keyword_lemma_in_prompt`` did — "is every lemma of this keyword present
    in the prompt's lemmas?" — from the persisted table instead of a fresh
    spaCy parse.

    A keyword the table does not have FRESH lemmas for (never written, or
    written by a superseded model) is repaired at most ``repair_budget`` times
    per instance and the result is persisted, so the cost is paid once and
    availability never depends on a backfill having happened.
    """

    __slots__ = (
        "_project_root", "_language", "_fresh", "_stale",
        "_budget", "_pending", "_repaired", "_exhausted", "_enabled",
    )

    def __init__(
        self,
        project_root: Path,
        language: str,
        fresh: dict[str, frozenset[str]],
        stale: set[str],
        *,
        repair_budget: int = LEMMA_REPAIR_BUDGET,
        repair_enabled: bool = True,
    ) -> None:
        self._project_root = project_root
        self._language = language
        self._fresh = fresh
        self._stale = stale
        self._budget = int(repair_budget)
        self._pending: dict[str, frozenset[str]] = {}
        self._repaired = 0
        self._exhausted = False
        self._enabled = bool(repair_enabled)

    def matches(self, keyword: str, prompt_lemmas: set[str]) -> bool:
        if not prompt_lemmas:
            return False
        kw = str(keyword).strip().lower()
        if not kw:
            return False
        lemmas = self._fresh.get(kw)
        if lemmas is None:
            lemmas = self._repair(kw)
        return bool(lemmas) and lemmas.issubset(prompt_lemmas)

    def _repair(self, kw: str) -> frozenset[str] | None:
        if not self._enabled:
            return None
        if self._repaired >= self._budget:
            self._exhausted = True
            return None
        self._repaired += 1
        lemmas = compute_lemmas(kw, self._project_root, self._language)
        if not lemmas:
            return None
        self._fresh[kw] = lemmas
        self._pending[kw] = lemmas
        return lemmas

    def flush(self) -> int:
        """Persist anything repaired during this read, so the NEXT prompt
        costs nothing. Best effort."""
        if not self._pending:
            return 0
        written = store_lemmas(self._project_root, self._pending, self._language)
        self._pending = {}
        return written

    @property
    def stats(self) -> dict[str, object]:
        return {
            "language": self._language,
            "fresh": len(self._fresh),
            "stale": len(self._stale),
            "repaired": self._repaired,
            "budget_exhausted": self._exhausted,
        }


def open_lexicon(
    project_root: Path,
    language: str,
    *,
    repair_budget: int = LEMMA_REPAIR_BUDGET,
    repair_enabled: bool = True,
) -> KeywordLexicon:
    fresh, stale = load_lexicon(project_root, language)
    return KeywordLexicon(
        project_root,
        language,
        fresh,
        stale,
        repair_budget=repair_budget,
        repair_enabled=repair_enabled,
    )
