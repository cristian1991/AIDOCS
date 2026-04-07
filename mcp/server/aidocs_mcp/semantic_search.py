"""Semantic code search — local embedding model, no external API.

Uses sentence-transformers with a small model for embedding code snippets.
Embeddings stored in SQLite alongside the code index. Search returns
ranked results by cosine similarity.

Falls back gracefully: if sentence-transformers isn't installed, returns
empty results with a helpful message.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

_MODEL_NAME = "all-MiniLM-L6-v2"  # ~23MB, fast on CPU
_EMBEDDING_DIM = 384
_model = None
_model_load_attempted = False


def _get_model():
    """Lazy-load the embedding model."""
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    except ImportError:
        _model = None
    return _model


def _embed(texts: list[str]) -> list[list[float]] | None:
    """Embed a list of texts. Returns None if model unavailable."""
    model = _get_model()
    if model is None:
        return None
    embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return [e.tolist() for e in embeddings]


def _pack_embedding(embedding: list[float]) -> bytes:
    """Pack float list into bytes for SQLite storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(data: bytes) -> list[float]:
    """Unpack bytes back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two normalized vectors (just dot product)."""
    return sum(x * y for x, y in zip(a, b))


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "semantic.sqlite3"


def _connect(project_root: Path) -> sqlite3.Connection:
    path = _db_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            file_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (file_path, chunk_index)
        )
    """)
    conn.commit()
    return conn


def _chunk_file(content: str, chunk_size: int = 500, overlap: int = 100) -> list[str]:
    """Split file content into overlapping chunks for embedding."""
    lines = content.splitlines()
    chunks = []
    i = 0
    while i < len(lines):
        chunk_lines = lines[i:i + chunk_size // 20]  # ~20 chars per line average
        chunk = "\n".join(chunk_lines)
        if chunk.strip():
            chunks.append(chunk)
        i += max(1, len(chunk_lines) - overlap // 20)
    return chunks if chunks else [content[:chunk_size]]


def index_file(project_root: Path, file_path: str, content: str) -> int:
    """Index a file's content as embeddings. Returns number of chunks indexed."""
    model = _get_model()
    if model is None:
        return 0

    chunks = _chunk_file(content)
    embeddings = _embed(chunks)
    if not embeddings:
        return 0

    import time
    now = time.time()
    conn = _connect(project_root)
    try:
        conn.execute("DELETE FROM embeddings WHERE file_path = ?", (file_path,))
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            conn.execute(
                "INSERT INTO embeddings (file_path, chunk_index, chunk_text, embedding, updated_at) VALUES (?, ?, ?, ?, ?)",
                (file_path, i, chunk[:2000], _pack_embedding(emb), now),
            )
        conn.commit()
        return len(chunks)
    finally:
        conn.close()


def search(project_root: Path, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Semantic search across indexed files. Returns ranked results."""
    query_emb_list = _embed([query])
    if not query_emb_list:
        return []
    query_emb = query_emb_list[0]

    conn = _connect(project_root)
    try:
        rows = conn.execute("SELECT file_path, chunk_index, chunk_text, embedding FROM embeddings").fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    # Score all chunks
    scored = []
    for row in rows:
        emb = _unpack_embedding(row["embedding"])
        score = _cosine_similarity(query_emb, emb)
        scored.append({
            "file": row["file_path"],
            "chunk_index": row["chunk_index"],
            "preview": row["chunk_text"][:200],
            "score": round(score, 4),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate by file (keep best chunk per file)
    seen_files: set[str] = set()
    results = []
    for item in scored:
        if item["file"] not in seen_files:
            seen_files.add(item["file"])
            results.append(item)
            if len(results) >= limit:
                break

    return results


def index_status(project_root: Path) -> dict[str, Any]:
    """Check semantic index status."""
    model = _get_model()
    db = _db_path(project_root)
    if not db.is_file():
        return {
            "available": model is not None,
            "model": _MODEL_NAME if model else "not installed (pip install sentence-transformers)",
            "indexed_files": 0,
            "indexed_chunks": 0,
        }
    conn = _connect(project_root)
    try:
        files = conn.execute("SELECT COUNT(DISTINCT file_path) as c FROM embeddings").fetchone()["c"]
        chunks = conn.execute("SELECT COUNT(*) as c FROM embeddings").fetchone()["c"]
    finally:
        conn.close()
    return {
        "available": model is not None,
        "model": _MODEL_NAME,
        "indexed_files": files,
        "indexed_chunks": chunks,
    }


def sync_from_code_index(project_root: Path, max_files: int = 500) -> dict[str, Any]:
    """Build semantic index from the existing code index. Reads file contents and embeds them."""
    model = _get_model()
    if model is None:
        return {"indexed": 0, "error": "sentence-transformers not installed. Run: pip install sentence-transformers"}

    # Read files from code index
    try:
        from .code_index_store import CodeIndexStore
        store = CodeIndexStore()
        files = store.list_files(project_root, limit=max_files)
    except Exception as e:
        return {"indexed": 0, "error": str(e)}

    indexed = 0
    errors = 0
    for f in files:
        try:
            abs_path = project_root / f["path"]
            if abs_path.is_file() and abs_path.stat().st_size < 100_000:  # skip large files
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                n = index_file(project_root, f["path"], content)
                if n > 0:
                    indexed += 1
        except Exception:
            errors += 1

    return {"indexed": indexed, "errors": errors, "total_files": len(files)}
