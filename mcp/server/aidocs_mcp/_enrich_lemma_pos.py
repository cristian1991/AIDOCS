"""Phase 6b — populate intent_lemma_sets.pos for every unenriched row.

One-shot enrichment script. Walks DISTINCT (lang, token) pairs where
pos='' (or centroid_blob is NULL if the loaded spaCy model ships
vectors), calls NLPService.enrich_token, writes back. Idempotent —
re-runs touch only the rows still missing data.

Run from repo root:
    python -m aidocs_mcp._enrich_lemma_pos

Delete after Phase 6e ships and POS+vector enrichment moves to a
background job triggered on row insert.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import intent_tokens_store as _store


def _service():
    from .aidocs_nlp.service import get_service

    return get_service(Path.cwd(), {})


def enrich_one_lang(lang: str, svc) -> dict[str, int]:
    """Enrich every (lang, token) pair in this lang with empty pos.
    Returns counts of {seen, updated, no_pipeline, errored}.
    """
    counts = {"seen": 0, "updated": 0, "no_pipeline": 0, "errored": 0}
    db = _store.empire_db_path()
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            """SELECT DISTINCT token FROM intent_lemma_sets
               WHERE lang = ? AND pos = ''""",
            (lang,),
        ).fetchall()
    for (token,) in rows:
        counts["seen"] += 1
        try:
            data = svc.enrich_token(token, language=lang)
        except Exception:
            counts["errored"] += 1
            continue
        if not data:
            counts["no_pipeline"] += 1
            continue
        pos = data.get("pos") or ""
        vector_bytes = data.get("vector_bytes")
        with sqlite3.connect(str(db)) as conn:
            cur = conn.execute(
                """UPDATE intent_lemma_sets
                   SET pos = ?, centroid_blob = ?
                   WHERE lang = ? AND token = ? AND pos = ''""",
                (pos, vector_bytes, lang, token),
            )
            counts["updated"] += cur.rowcount or 0
            conn.commit()
    return counts


def main() -> dict[str, dict[str, int]]:
    _store.init_db()
    svc = _service()
    out: dict[str, dict[str, int]] = {}
    for lang in _store.list_langs():
        out[lang] = enrich_one_lang(lang, svc)
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(main(), indent=2))
