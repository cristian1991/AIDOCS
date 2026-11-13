#!/usr/bin/env python3
"""Build the shipped SQLite-native PROJECT-MEMORY seed artifact.

SQLite-only doctrine (2026-06). DEV/BUILD-TIME tool. Reads the bundled factory
memory templates (``core/.MEMORY/.aidocs/templates/memory/**/*.{md,aidocs}``)
ONE TIME at build time and emits a shipped, seeded SQLite database
``mcp/server/aidocs_mcp/seed/project_memory.sqlite3``.

This is DISTINCT from ``seed/factory.sqlite3`` (the global NLP/gate vocabulary
seed for the empire DB) — it seeds a DIFFERENT live database: the per-project
canonical ``memory_index`` (+ deterministic ``memory_routes`` / keywords).
RuntimeBootstrapService._seed_factory_memory_into_index opens this shipped DB
and upserts each row idempotently. The runtime/bootstrap read NO factory
Markdown — the .md templates are build-time source only.

``rules/workflow.md`` is intentionally EXCLUDED: workflow needs code-level
enforcement (compiled workflow_definitions), not advisory memory seeding.
``INDEX.md`` is excluded (retired router fossil).

Regenerate after editing a template:

    python core/scripts/build_project_memory_seed.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "mcp" / "server"))

from aidocs_mcp.memory_discovery import _parse_frontmatter  # noqa: E402

_KIND_BY_HEAD = {
    "rules": "rule",
    "system": "system",
    "domains": "domain",
    "roadmaps": "roadmap",
    "specs": "spec",
    "config": "config",
    "related-projects": "related_project",
    "daily": "daily",
    ".aidocs": "aidocs",
    "sessions": "session",
}

# Excluded from project-memory seeding:
#   INDEX.md       — retired router fossil.
#   rules/workflow.md — workflow needs code-level enforcement, NOT advisory
#                       memory seeding (tracked TODO; STOPPED for now).
_EXCLUDE_RELPATHS = {"INDEX.md", "rules/workflow.md"}


def _kind_for(rel: str) -> str:
    head = rel.split("/", 1)[0] if "/" in rel else ""
    return _KIND_BY_HEAD.get(head, "memory")


def collect(memory_root: Path) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for path in sorted(memory_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".md", ".aidocs"):
            continue
        rel = path.relative_to(memory_root).as_posix()
        if rel in _EXCLUDE_RELPATHS or path.name in _EXCLUDE_RELPATHS:
            continue
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        meta = meta if isinstance(meta, dict) else {}
        content = body or text
        kws = meta.get("keywords") or []
        keywords = ",".join(
            sorted({str(k).strip().lower() for k in kws if str(k).strip()})
        )
        raw_trig = meta.get("trigger") or meta.get("triggers") or "topic"
        trigger = (
            raw_trig[0]
            if isinstance(raw_trig, list) and raw_trig
            else (raw_trig if isinstance(raw_trig, str) else "topic")
        )
        trigger = (trigger or "topic").strip().lower()
        if trigger not in ("topic", "action"):
            trigger = "topic"
        priority = str(meta.get("priority") or "normal").strip().lower()
        if priority not in ("high", "normal", "low"):
            priority = "normal"
        rows.append((rel, _kind_for(rel), content, keywords, trigger, priority))
    return rows


def write_seed_db(out_path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(str(out_path))
    try:
        conn.execute(
            "CREATE TABLE project_memory_seed ("
            "  path TEXT PRIMARY KEY,"
            "  kind TEXT NOT NULL,"
            "  content TEXT NOT NULL,"
            "  keywords TEXT NOT NULL DEFAULT '',"
            "  trigger TEXT NOT NULL DEFAULT 'topic',"
            "  priority TEXT NOT NULL DEFAULT 'normal'"
            ")",
        )
        conn.executemany(
            "INSERT INTO project_memory_seed"
            "(path, kind, content, keywords, trigger, priority) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    bundle = _REPO / "core" / ".MEMORY" / ".aidocs" / "templates"
    memory_root = bundle / "memory"
    if not memory_root.is_dir():
        sys.stderr.write(f"factory memory templates not found: {memory_root}\n")
        return 1
    rows = collect(memory_root)
    out = _REPO / "mcp" / "server" / "aidocs_mcp" / "seed" / "project_memory.sqlite3"
    write_seed_db(out, rows)
    sys.stderr.write(f"wrote {out} ({len(rows)} rows)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
