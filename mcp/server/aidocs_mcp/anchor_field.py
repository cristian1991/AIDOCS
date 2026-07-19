"""Canonical anchor extractor — produces a KingField struct for a prompt.

Phase 3 (2026-05-15). One module, one entry point. Hoists the Seeds-shape
extractor out of preflight_service so it can be the canonical "what does
this prompt name" producer used by both UPS (king-field) and task_begin
(agent-crop).

Caching: extract_field is called once per epoch per prompt-hash. The cache
lives in session_query_gate (king_field_json column). Subsequent calls in
the same epoch with the same prompt hit the cache; epoch bump or new
prompt re-extracts.

KingField shape:
  action_verbs:        list of VERB lemmas
  anchors:             list of NOUN + PROPN lemmas (ordered)
  root_anchor:         the syntactic root noun (dep='ROOT', pos NOUN/PROPN)
  file_paths:          regex-extracted paths from prompt
  resolved_symbols:    code_outlines.symbol matches for the anchors
  resolved_files:      code_files.path matches for the anchors
  domain_candidates:   intent_lemma_sets kind='domain_hint' parent_keys
                       whose tokens overlap with the anchors
  is_creation_intent:  bool — prompt contains create/build/new/etc.
  language:            ISO 639-1 language code
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Tokens that imply creation intent. Used by the diff verdict in Phase 4
# to weight scope-expansion warnings.
_CREATION_INTENT_TOKENS = frozenset(
    {
        "create",
        "build",
        "make",
        "add",
        "implement",
        "new",
        "scaffold",
        "generate",
        "introduce",
    },
)

# Stopwords — too generic to anchor on. Kept short; over-stopping hurts
# matches more than letting common nouns through.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "it",
        "thing",
        "stuff",
        "way",
        "time",
        "day",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "yours",
        "please",
        "now",
        "then",
        "here",
        "there",
    },
)

# File-path regex — same shape as preflight_service uses.
_FILE_PATH_RE = re.compile(
    r"[\w][\w./-]*\.(?:py|ts|tsx|js|jsx|cs|cshtml|md|json|yml|yaml|toml|sql|rs|go|java|rb|php)",
)


@dataclass
class KingField:
    """Structured projection of a prompt — the canonical anchor field.

    Produced once per epoch per prompt. Cached in session_query_gate.
    Read by UPS hook for king-field, by task_begin for agent-crop.
    """

    action_verbs: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    root_anchor: str = ""
    file_paths: list[str] = field(default_factory=list)
    resolved_symbols: list[str] = field(default_factory=list)
    resolved_files: list[str] = field(default_factory=list)
    domain_candidates: list[str] = field(default_factory=list)
    is_creation_intent: bool = False
    language: str = "en"
    prompt_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "action_verbs": list(self.action_verbs),
            "anchors": list(self.anchors),
            "root_anchor": self.root_anchor,
            "file_paths": list(self.file_paths),
            "resolved_symbols": list(self.resolved_symbols),
            "resolved_files": list(self.resolved_files),
            "domain_candidates": list(self.domain_candidates),
            "is_creation_intent": bool(self.is_creation_intent),
            "language": self.language,
            "prompt_hash": self.prompt_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> KingField:
        return cls(
            action_verbs=list(data.get("action_verbs") or []),
            anchors=list(data.get("anchors") or []),
            root_anchor=str(data.get("root_anchor") or ""),
            file_paths=list(data.get("file_paths") or []),
            resolved_symbols=list(data.get("resolved_symbols") or []),
            resolved_files=list(data.get("resolved_files") or []),
            domain_candidates=list(data.get("domain_candidates") or []),
            is_creation_intent=bool(data.get("is_creation_intent") or False),
            language=str(data.get("language") or "en"),
            prompt_hash=str(data.get("prompt_hash") or ""),
        )


def hash_prompt(prompt: str) -> str:
    """Stable hash of the prompt text — used as cache key."""
    return hashlib.sha256((prompt or "").strip().encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_with_spacy(prompt: str, project_root: Path) -> tuple[list[str], list[str], str, str]:
    """SpaCy parse → (verbs, anchors, root_anchor, language). Falls back
    to empty lists when no pipeline is loaded.
    """
    try:
        from .aidocs_nlp.service import get_service

        svc = get_service(project_root, {})
        doc = svc.analyze(prompt)
    except Exception:
        doc = None
    if doc is None or not getattr(doc, "tokens", None):
        return [], [], "", "en"

    verbs: list[str] = []
    anchors: list[str] = []
    root_anchor = ""
    for tok in doc.tokens:
        text = (getattr(tok, "text", "") or "").lower().strip()
        lemma = (getattr(tok, "lemma", "") or "").lower().strip()
        pos = (getattr(tok, "pos", "") or "").upper()
        dep = (getattr(tok, "dep", "") or "").lower()
        chosen = lemma or text
        if not chosen or chosen in _STOPWORDS or len(chosen) < 2:
            continue
        if pos == "VERB" and chosen not in verbs:
            verbs.append(chosen)
        elif pos in ("NOUN", "PROPN"):
            if chosen not in anchors:
                anchors.append(chosen)
            if dep == "root" and not root_anchor:
                root_anchor = chosen
    # Fallback: if no ROOT noun found but anchors exist, use the last one
    # (often the syntactic head of the noun phrase in imperative prompts).
    if not root_anchor and anchors:
        root_anchor = anchors[-1]
    return verbs, anchors, root_anchor, getattr(doc, "language", "en") or "en"


def _resolve_symbols(
    conn: sqlite3.Connection,
    anchors: Iterable[str],
    cap_per_anchor: int = 5,
) -> list[str]:
    """Resolve anchor nouns against code_outlines.symbol. Deterministic
    LIKE match; cap per anchor to bound query cost.
    """
    out: list[str] = []
    seen: set[str] = set()
    for anchor in anchors:
        like = f"%{anchor}%"
        try:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM code_outlines WHERE LOWER(symbol) LIKE ? LIMIT ?",
                (like, cap_per_anchor),
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            sym = str(r[0])
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def _resolve_files(
    conn: sqlite3.Connection,
    paths: Iterable[str],
    cap_per_path: int = 5,
) -> list[str]:
    """Resolve regex file paths against code_files.path."""
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        like = f"%{p}%"
        try:
            rows = conn.execute(
                "SELECT path FROM code_files WHERE path LIKE ? LIMIT ?",
                (like, cap_per_path),
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            fp = str(r[0])
            if fp and fp not in seen:
                seen.add(fp)
                out.append(fp)
    return out


def _resolve_domains(
    empire_db_path: Path,
    anchors: Iterable[str],
) -> list[str]:
    """Look up domain candidates from empire's intent_lemma_sets where
    kind='domain_hint' and token overlaps anchors. Returns parent_keys
    (domain names) sorted by hit count.
    """
    if not empire_db_path.is_file():
        return []
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(empire_db_path))
        try:
            for anchor in anchors:
                rows = conn.execute(
                    "SELECT DISTINCT parent_key FROM intent_lemma_sets "
                    "WHERE kind='domain_hint' AND token = ?",
                    (anchor,),
                ).fetchall()
                for r in rows:
                    pk = str(r[0])
                    counts[pk] = counts.get(pk, 0) + 1
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return sorted(counts.keys(), key=lambda k: -counts[k])


def _empire_db_path() -> Path:
    """Locate empire DB path — env override or default."""
    import os

    override = os.environ.get("AIDOCS_EMPIRE_DB", "").strip()
    if override:
        return Path(override)
    home = Path(os.path.expanduser("~"))
    return home / ".aidocs" / "empire.sqlite3"


def _ensure_king_field_table(conn: sqlite3.Connection) -> None:
    """Idempotent migration for the king-field cache table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_king_field (
            session_id TEXT NOT NULL,
            epoch_id TEXT NOT NULL DEFAULT '',
            prompt_hash TEXT NOT NULL DEFAULT '',
            field_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id)
        )
        """,
    )


def stash_king_field(
    project_root: Path,
    session_id: str,
    field: KingField,
    epoch_id: str = "",
) -> None:
    """Persist a king-field for a session. Replaces any previous entry —
    one king-field active per session (latest UPS wins). The epoch_id is
    stored for audit but not part of the PK.
    """
    import json

    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.parent.is_dir():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_king_field_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO session_king_field "
                "(session_id, epoch_id, prompt_hash, field_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    epoch_id,
                    field.prompt_hash,
                    json.dumps(field.to_dict()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


def get_king_field(
    project_root: Path,
    session_id: str,
) -> KingField | None:
    """Return the cached king-field for a session, or None if not set."""
    import json

    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_king_field_table(conn)
            row = conn.execute(
                "SELECT field_json FROM session_king_field WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except Exception:
        return None
    return KingField.from_dict(data)


def extract_field(prompt: str, project_root: Path) -> KingField:
    """Extract a KingField from a prompt. Pure function — no caching here;
    the cache is the caller's responsibility (see UPS hook integration).
    """
    f = KingField(prompt_hash=hash_prompt(prompt))
    text = (prompt or "").strip()
    if not text:
        return f
    f.is_creation_intent = any(
        re.search(rf"\b{re.escape(v)}\b", text.lower()) for v in _CREATION_INTENT_TOKENS
    )
    # File paths from raw text (spaCy mangles them).
    for m in _FILE_PATH_RE.finditer(text):
        path = m.group(0).strip()
        if path and path not in f.file_paths:
            f.file_paths.append(path)
    # spaCy verbs/nouns/root.
    verbs, anchors, root_anchor, language = _extract_with_spacy(text, project_root)
    f.action_verbs = verbs
    f.anchors = anchors
    f.root_anchor = root_anchor
    f.language = language
    # Resolve against project's kingdom DB.
    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if db_path.is_file() and f.anchors:
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                f.resolved_symbols = _resolve_symbols(conn, f.anchors)
                if f.file_paths:
                    f.resolved_files = _resolve_files(conn, f.file_paths)
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    # Resolve domains against empire DB.
    if f.anchors:
        f.domain_candidates = _resolve_domains(_empire_db_path(), f.anchors)
    return f


def compute_diff_verdict(
    king: KingField,
    crop: KingField,
) -> tuple[str, dict]:
    """Compare king-field vs agent-crop. Returns (verdict, details).

    Verdicts (Empire-locked 2026-05-15):
      - 'block': zero anchor overlap AND zero domain overlap AND zero
                 code-symbol overlap (resolved against code_outlines).
                 Totally divergent.
      - 'warn':  zero root-anchor overlap OR zero resolved-symbol
                 overlap, but at least one bridge exists (shared
                 anchor, domain, or symbol). Similar concept space.
      - 'allow': any strong shared anchor/domain/symbol bridge.
    """
    a_king = set(s.lower() for s in king.anchors)
    a_crop = set(s.lower() for s in crop.anchors)
    d_king = set(s.lower() for s in king.domain_candidates)
    d_crop = set(s.lower() for s in crop.domain_candidates)
    s_king = set(s.lower() for s in king.resolved_symbols)
    s_crop = set(s.lower() for s in crop.resolved_symbols)
    anchor_overlap = a_king & a_crop
    domain_overlap = d_king & d_crop
    symbol_overlap = s_king & s_crop
    details = {
        "anchor_overlap": sorted(anchor_overlap),
        "domain_overlap": sorted(domain_overlap),
        "symbol_overlap": sorted(symbol_overlap),
        "king_root": king.root_anchor,
        "crop_root": crop.root_anchor,
    }
    # BLOCK: total divergence — nothing shared anywhere.
    if not anchor_overlap and not domain_overlap and not symbol_overlap:
        return "block", details
    # WARN: root-anchor differs AND symbols don't overlap (some bridge
    # exists but the target diverges).
    root_match = bool(
        king.root_anchor
        and (
            king.root_anchor.lower() == crop.root_anchor.lower()
            or king.root_anchor.lower() in a_crop
        ),
    )
    if not root_match and not symbol_overlap:
        return "warn", details
    return "allow", details
