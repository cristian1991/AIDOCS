"""ai_preflight — battlefield briefing service (Phase B-lite, 2026-05-14).

Per aidocs-doctrine: preflight is NOT search — it's the briefing that prevents
reinvention. Given a task description, returns the existing wheels, the
files to inspect first, the known traps, the tests to run, and the
warnings about not creating new things when capabilities already exist.

Implementation: direct kingdom-DB reads (capability_definitions,
code_outlines, code_files, memory_files, memory_route_keywords,
memory_links) — no MCP tool indirection, no new schema, no embeddings
yet. spaCy seeds the candidate set; sqlite does the heavy lookup;
1-hop walk over memory_links pulls related doctrine. The card output
has two audiences: structured dict for agents, terse markdown for
operators.

Phase B-lite + E-lite. Future phases: graph extension with
do_not_duplicate / known_trap / supersedes edges, embedding-based
ranker, creation guard.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# ── Seed extraction ─────────────────────────────────────────────────


_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "with",
        "by",
        "as",
        "at",
        "from",
        "is",
        "are",
        "be",
        "been",
        "being",
        "was",
        "were",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "would",
        "should",
        "could",
        "can",
        "may",
        "might",
        "will",
        "shall",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "she",
        "it",
        "they",
        "them",
        "their",
        "his",
        "her",
        "its",
        "what",
        "when",
        "where",
        "who",
        "why",
        "how",
        "if",
        "then",
        "but",
        "not",
        "no",
        "yes",
        "ok",
        "okay",
        "please",
        "thanks",
        "thank",
        # Creation-intent verbs are signal of WHAT operator wants to do,
        # not signal of WHAT to look for. They match too broadly in
        # capability descriptions (every tool's description has "create"
        # / "add" / "build" somewhere) and drown the rare-keyword signal.
        "build",
        "create",
        "add",
        "make",
        "implement",
        "write",
        "new",
        "scaffold",
        "design",
        "draft",
        "introduce",
        "set",
        "setup",
        "need",
        "want",
        "use",
        "show",
        "get",
        "give",
        "take",
        # Tool-domain noise too common to be useful as a lookup keyword.
        "tool",
        "service",
        "page",
        "thing",
        "stuff",
        "code",
        "file",
        "function",
        "class",
        "method",
        "way",
        "things",
    },
)

_FILE_PATH_RE = re.compile(
    r"[A-Za-z0-9_\-./]+\.(py|ts|tsx|js|jsx|rs|toml|md|yaml|yml|sql|json|sh|ps1)",
)


_CREATION_INTENT_TOKENS: frozenset[str] = frozenset(
    {
        "build",
        "create",
        "add",
        "make",
        "implement",
        "write",
        "new",
        "scaffold",
        "design",
        "draft",
        "introduce",
        "setup",
    },
)


@dataclass
class Seeds:
    """Extracted from the task description; drives all lookups."""

    verbs: list[str] = field(default_factory=list)
    nouns: list[str] = field(default_factory=list)
    proper_nouns: list[str] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    raw_tokens: list[str] = field(default_factory=list)
    is_creation_intent: bool = False  # signals "do_not_create" warnings

    def all_keywords(self) -> list[str]:
        """De-duplicated keyword candidates for lookups."""
        seen: set[str] = set()
        out: list[str] = []
        for src in (self.verbs, self.nouns, self.proper_nouns, self.raw_tokens):
            for w in src:
                lw = w.lower().strip()
                if not lw or lw in _STOPWORDS or lw in seen or len(lw) < 3:
                    continue
                seen.add(lw)
                out.append(lw)
        return out


def _extract_seeds(task: str, project_root: Path) -> Seeds:
    """spaCy-backed seed extraction. Falls back to plain tokenization when
    no pipeline is loaded for the detected language.
    """
    seeds = Seeds()
    task = (task or "").strip()
    if not task:
        return seeds

    # Detect creation intent BEFORE stopwording removes the verbs.
    task_lower = task.lower()
    seeds.is_creation_intent = any(
        re.search(rf"\b{re.escape(v)}\b", task_lower) for v in _CREATION_INTENT_TOKENS
    )

    # File paths via regex (spaCy tokenizes them as garbage).
    for m in _FILE_PATH_RE.finditer(task):
        path = m.group(0).strip()
        if path and path not in seeds.file_paths:
            seeds.file_paths.append(path)

    # spaCy parse — pull verbs, nouns, proper nouns.
    try:
        from .aidocs_nlp.service import get_service

        svc = get_service(project_root, {})
        doc = svc.analyze(task)
    except Exception:
        doc = None

    if doc is not None and getattr(doc, "tokens", None):
        for tok in doc.tokens:
            text = (getattr(tok, "text", "") or "").lower().strip()
            lemma = (getattr(tok, "lemma", "") or "").lower().strip()
            pos = (getattr(tok, "pos", "") or "").upper()
            if not text:
                continue
            seeds.raw_tokens.append(text)
            chosen = lemma or text
            if chosen in _STOPWORDS or len(chosen) < 3:
                continue
            if pos == "VERB":
                if chosen not in seeds.verbs:
                    seeds.verbs.append(chosen)
            elif pos == "NOUN":
                if chosen not in seeds.nouns:
                    seeds.nouns.append(chosen)
            elif pos == "PROPN":
                if chosen not in seeds.proper_nouns:
                    seeds.proper_nouns.append(chosen)
    else:
        # No pipeline — split on whitespace + punctuation.
        for raw in re.split(r"[^\w\.]+", task):
            tok = raw.lower().strip()
            if tok:
                seeds.raw_tokens.append(tok)

    return seeds


# ── Direct kingdom-DB lookups ──────────────────────────────────────


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _find_capabilities(
    conn: sqlite3.Connection,
    seeds: Seeds,
    limit: int = 10,
) -> list[dict]:
    """Search capability_definitions name/title/description/aliases for
    seed keywords. Ranked by # of seed matches.
    """
    keywords = seeds.all_keywords()
    if not keywords:
        return []
    scored: dict[str, dict] = {}
    for kw in keywords:
        like = f"%{kw}%"
        rows = conn.execute(
            """SELECT name, capability_kind, capability_family, title, description, aliases_json
               FROM capability_definitions
               WHERE name LIKE ? OR title LIKE ? OR description LIKE ?
                  OR aliases_json LIKE ?
               LIMIT 25""",
            (like, like, like, like),
        ).fetchall()
        for r in rows:
            name = r[0]
            entry = scored.setdefault(
                name,
                {
                    "name": name,
                    "kind": r[1],
                    "family": r[2] or "",
                    "title": r[3] or "",
                    "description": r[4] or "",
                    "matches": [],
                },
            )
            if kw not in entry["matches"]:
                entry["matches"].append(kw)
    # Require >= 2 matched seeds for a wheel to be considered strong.
    # Single-match capabilities are too noisy to rank as top wheels.
    strong = [w for w in scored.values() if len(w["matches"]) >= 2]
    if not strong:
        # Fall back to top single-match if no strong signal — better
        # than nothing for niche tasks.
        strong = list(scored.values())
    ranked = sorted(strong, key=lambda x: -len(x["matches"]))
    return ranked[:limit]


def _find_code(
    conn: sqlite3.Connection,
    seeds: Seeds,
    limit: int = 8,
) -> list[dict]:
    """Symbol + file-path matches in code_outlines / code_files."""
    keywords = seeds.all_keywords()
    if not keywords and not seeds.file_paths:
        return []
    scored: dict[tuple[str, str], dict] = {}
    # Symbol matches in code_outlines.
    for kw in keywords:
        like = f"%{kw}%"
        rows = conn.execute(
            """SELECT path, symbol, kind, line_number
               FROM code_outlines
               WHERE symbol LIKE ?
               LIMIT 15""",
            (like,),
        ).fetchall()
        for r in rows:
            key = (r[0], r[1])
            entry = scored.setdefault(
                key,
                {
                    "path": r[0],
                    "symbol": r[1],
                    "kind": r[2],
                    "line": r[3],
                    "matches": [],
                },
            )
            if kw not in entry["matches"]:
                entry["matches"].append(kw)
    # Explicit file paths from the prompt.
    for fp in seeds.file_paths:
        rows = conn.execute(
            "SELECT path FROM code_files WHERE path LIKE ? LIMIT 5",
            (f"%{fp}%",),
        ).fetchall()
        for r in rows:
            key = (r[0], "")
            scored.setdefault(
                key,
                {
                    "path": r[0],
                    "symbol": "",
                    "kind": "file",
                    "line": 0,
                    "matches": [fp],
                },
            )
    ranked = sorted(scored.values(), key=lambda x: -len(x["matches"]))
    return ranked[:limit]


def _find_memory(
    conn: sqlite3.Connection,
    seeds: Seeds,
    limit: int = 8,
) -> list[dict]:
    """Memory pages matching via memory_route_keywords AND direct
    title/content_text fuzzy match.
    """
    keywords = seeds.all_keywords()
    if not keywords:
        return []
    scored: dict[str, dict] = {}
    # Keyword-routed memory.
    for kw in keywords:
        rows = conn.execute(
            """SELECT mf.path, mf.kind, COALESCE(mf.title,'') AS title
               FROM memory_route_keywords mrk
               JOIN memory_routes mr ON mr.route_id = mrk.route_id
               JOIN memory_index mf ON mf.path = mr.target_path
                 AND COALESCE(mf.status,'active')='active'
                 AND COALESCE(mf.superseded_by,'')=''
               WHERE mrk.keyword = ?""",
            (kw,),
        ).fetchall()
        for r in rows:
            entry = scored.setdefault(
                r[0],
                {
                    "path": r[0],
                    "kind": r[1],
                    "title": r[2] or "",
                    "matches": [],
                    "source": "route",
                },
            )
            if kw not in entry["matches"]:
                entry["matches"].append(kw)
    # Direct title/content match (covers memory without an explicit route).
    for kw in keywords:
        like = f"%{kw}%"
        rows = conn.execute(
            """SELECT path, kind, COALESCE(title,'') AS title FROM memory_index
               WHERE COALESCE(status,'active')='active' AND COALESCE(superseded_by,'')=''
                 AND (COALESCE(title,'') LIKE ? OR content LIKE ?)
               LIMIT 10""",
            (like, like),
        ).fetchall()
        for r in rows:
            entry = scored.setdefault(
                r[0],
                {
                    "path": r[0],
                    "kind": r[1],
                    "title": r[2] or "",
                    "matches": [],
                    "source": "text",
                },
            )
            if kw not in entry["matches"]:
                entry["matches"].append(kw)
    ranked = sorted(scored.values(), key=lambda x: -len(x["matches"]))
    return ranked[:limit]


def _find_tests(
    conn: sqlite3.Connection,
    seeds: Seeds,
    limit: int = 5,
) -> list[dict]:
    """Test files whose path matches a seed keyword."""
    keywords = seeds.all_keywords()
    if not keywords:
        return []
    scored: dict[str, dict] = {}
    for kw in keywords:
        like = f"%{kw}%"
        rows = conn.execute(
            """SELECT path FROM code_files
               WHERE (role = 'test' OR path LIKE '%/test%' OR path LIKE '%/tests/%')
                 AND path LIKE ?
               LIMIT 10""",
            (like,),
        ).fetchall()
        for r in rows:
            entry = scored.setdefault(
                r[0],
                {
                    "path": r[0],
                    "matches": [],
                },
            )
            if kw not in entry["matches"]:
                entry["matches"].append(kw)
    ranked = sorted(scored.values(), key=lambda x: -len(x["matches"]))
    return ranked[:limit]


def _walk_memory_links(
    conn: sqlite3.Connection,
    limit: int = 8,
) -> list[dict]:
    """RETIRED (SQLite-only doctrine, 2026-06): memory_links is no longer rebuilt
    from markdown and has no runtime reader. The 1-hop link walk is a no-op —
    related-memory expansion via the link graph is removed. Kept as a stable
    callable so the preflight caller needs no change."""
    return []


# ── Filters / synthesizers ─────────────────────────────────────────


_TRAP_HINT_TOKENS: frozenset[str] = frozenset(
    {
        "caveat",
        "trap",
        "gotcha",
        "pitfall",
        "bug",
        "bugged",
        "broken",
        "do not",
        "don't",
        "never",
        "avoid",
        "deprecated",
        "removed",
        "workaround",
        "known issue",
        "regression",
        "subtle",
        "fragile",
    },
)


def _filter_traps(memory_entries: list[dict]) -> list[dict]:
    """Pick entries whose kind / title / source signal a trap or
    cautionary note. Conservative — only flag when signal is clear.
    """
    traps: list[dict] = []
    for m in memory_entries:
        kind = (m.get("kind") or "").lower()
        title = (m.get("title") or "").lower()
        path = (m.get("path") or "").lower()
        is_trap = (
            "caveat" in kind
            or "trap" in kind
            or "rule" in kind
            or "caveat" in path
            or "rule" in path
            or "doctrine" in path
            or any(t in title for t in _TRAP_HINT_TOKENS)
        )
        if is_trap:
            traps.append(m)
    return traps


def _derive_warnings(
    wheels: list[dict],
    seeds: Seeds,
) -> list[dict]:
    """Emit do-not-create warnings when the prompt carries creation
    intent (build/create/add/etc.) AND a capability strongly matches
    the substantive seeds. Creation intent is captured upstream by
    Seeds.is_creation_intent so we keep it after stopwording strips
    the verbs themselves.
    """
    if not seeds.is_creation_intent:
        return []
    warnings: list[dict] = []
    for w in wheels:
        if len(w.get("matches", [])) < 1:
            continue
        warnings.append(
            {
                "capability": w["name"],
                "family": w.get("family") or w.get("kind"),
                "matches": w["matches"],
                "message": (
                    f"`{w['name']}` already exists "
                    f"({w.get('family') or w.get('kind')}). "
                    f"Inspect before creating a new one — matched on: "
                    f"{', '.join(w['matches'][:5])}"
                ),
            },
        )
    return warnings[:5]


def _score_confidence(
    seeds: Seeds,
    wheels: list[dict],
    files: list[dict],
    memory: list[dict],
) -> str:
    """Heuristic confidence: how many sources returned matches."""
    sources_hit = sum([bool(wheels), bool(files), bool(memory)])
    if not seeds.all_keywords():
        return "low_no_seeds"
    if sources_hit == 0:
        return "low_no_matches"
    if sources_hit == 1:
        return "medium"
    return "high"


def _flag_missing(
    seeds: Seeds,
    wheels: list[dict],
    files: list[dict],
) -> list[str]:
    """List seed keywords that didn't match anything."""
    matched: set[str] = set()
    for w in wheels:
        for m in w.get("matches", []):
            matched.add(m)
    for f in files:
        for m in f.get("matches", []):
            matched.add(m)
    missing = [kw for kw in seeds.all_keywords() if kw not in matched]
    return missing[:10]


# ── Card formatter ─────────────────────────────────────────────────


def _render_markdown(card: dict) -> str:
    """Compact markdown card for operator-facing display."""
    parts: list[str] = []
    parts.append(f"## Task preflight: `{card['task']}`")
    parts.append("")
    seeds = card.get("seeds", {})
    parts.append(
        f"**Seeds:** verbs={seeds.get('verbs', [])} "
        f"nouns={seeds.get('nouns', [])} "
        f"file_paths={seeds.get('file_paths', [])}",
    )
    parts.append(f"**Confidence:** {card.get('confidence', 'unknown')}")
    parts.append("")

    if card.get("do_not_create_warnings"):
        parts.append("### ⚠️ Do-not-create warnings")
        for w in card["do_not_create_warnings"]:
            parts.append(f"- {w['message']}")
        parts.append("")

    if card.get("existing_wheels"):
        parts.append("### Existing wheels")
        for w in card["existing_wheels"]:
            parts.append(
                f"- **`{w['name']}`** ({w.get('family') or w.get('kind')}) — "
                f"{(w.get('title') or w.get('description') or '')[:120]}",
            )
        parts.append("")

    if card.get("inspect_first"):
        parts.append("### Inspect first")
        for f in card["inspect_first"]:
            sym = f.get("symbol")
            line = f.get("line")
            if sym:
                parts.append(f"- `{f['path']}:{line}` — `{sym}` ({f['kind']})")
            else:
                parts.append(f"- `{f['path']}`")
        parts.append("")

    if card.get("known_traps"):
        parts.append("### Known traps / doctrine")
        for t in card["known_traps"]:
            parts.append(f"- `{t['path']}` — {t.get('title') or t.get('kind')}")
        parts.append("")

    if card.get("tests_to_run"):
        parts.append("### Tests to run")
        for t in card["tests_to_run"]:
            parts.append(f"- `{t['path']}`")
        parts.append("")

    if card.get("missing_info"):
        parts.append(f"**Missing info:** seeds with no matches: {card['missing_info']}")

    return "\n".join(parts)


# ── Public entry ───────────────────────────────────────────────────


def preflight(project_root: Path, task: str) -> dict:
    """Run the preflight pipeline. Returns
    `{"structured": dict, "markdown": str}`. Empty card on error / no DB.
    """
    seeds = _extract_seeds(task, project_root)
    db_path = _db_path(project_root)
    if not db_path.is_file():
        structured = {
            "task": task,
            "seeds": _seeds_to_dict(seeds),
            "confidence": "low_no_index",
            "missing_info": ["kingdom DB not present — run /aidocs to index"],
            "existing_wheels": [],
            "inspect_first": [],
            "known_traps": [],
            "tests_to_run": [],
            "do_not_create_warnings": [],
        }
        return {"structured": structured, "markdown": _render_markdown(structured)}

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = None
        wheels = _find_capabilities(conn, seeds)
        files = _find_code(conn, seeds)
        memory = _find_memory(conn, seeds)
        tests = _find_tests(conn, seeds)
        memory_hop1 = _walk_memory_links(conn)

    traps = _filter_traps(memory + memory_hop1)
    warnings = _derive_warnings(wheels, seeds)

    structured = {
        "task": task,
        "seeds": _seeds_to_dict(seeds),
        "existing_wheels": wheels,
        "inspect_first": files,
        "known_traps": traps[:6],
        "tests_to_run": tests,
        "do_not_create_warnings": warnings,
        "confidence": _score_confidence(seeds, wheels, files, memory),
        "missing_info": _flag_missing(seeds, wheels, files),
    }
    return {"structured": structured, "markdown": _render_markdown(structured)}


def _seeds_to_dict(seeds: Seeds) -> dict:
    return {
        "verbs": seeds.verbs,
        "nouns": seeds.nouns,
        "proper_nouns": seeds.proper_nouns,
        "file_paths": seeds.file_paths,
        "is_creation_intent": seeds.is_creation_intent,
    }
