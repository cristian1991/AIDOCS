"""Context-memory discovery — surfaces canonical durable-memory entries as
pointers when the user's prompt matches their keyword triggers.

SQLite-only no-scroll seal (2026-06): discovery reads the SQL route ledger
ONLY — ``memory_routes`` + ``memory_route_keywords`` joined to the ACTIVE
``memory_index`` rows. It does NOT scan the ``.MEMORY/`` tree or read on-disk
Markdown frontmatter at runtime; loose .md files are invisible until an
operator imports them via the explicit ``migrate_markdown_to_sqlite`` path
(which is where the frontmatter schema below is parsed INTO routes — an
AUTHORING/import shape, not a runtime read).

Philosophy:
- No always-on memory. Every inclusion is triggered.
- Pointers, not content. The hook emits a path + reason. The agent decides
  whether to call memory_read. This keeps the per-turn context budget lean
  and avoids re-injecting the same entry on every matching prompt.
- Opt-in keywords. An entry with no keyword routes is invisible to this
  matcher; keywords are declared at capture/import time and live in
  ``memory_route_keywords`` (SQL), never read from disk at match time.

Frontmatter schema:
    ---
    keywords: [live server, pm2, deploy, ssh, prod logs]
    trigger: [topic]              # optional; default = ["topic"]
    priority: high                # optional: low | normal | high
    ---

    # File content follows.

``trigger`` values:
- ``topic``   → subject matter keywords ("live server", "pdf", "patients").
- ``action``  → user-intent keywords ("implement", "research", "harden").
  Both are matched against the same prompt; the flag exists so one project
  can mix topic files and action-type files in the same tree.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemoryHint:
    path: str  # project-relative, forward-slash separated
    why: str
    matched_keywords: tuple[str, ...]
    severity: str = "normal"  # normal | high | critical | sovereign
    hop_depth: int = 0  # 0 = direct keyword/anchor match; 1+ = via code_edges traversal
    matched_symbol: str = ""  # symbol that anchored this hint (when hop>=0 from anchor)


# Keep the per-prompt hint count small — false positives teach agents to
# ignore the hint line. Surface only the most-specific matches.
_MAX_HINTS = 3

# Frontmatter delimiters.
_FRONTMATTER_START = "---\n"
_FRONTMATTER_END_RE = re.compile(r"^---\s*$", re.MULTILINE)

_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}

# Observational adverbs — mirrors tool_discovery and the grant matcher so
# behavior is consistent across all three keyword surfaces.
_OBSERVATIONAL_PREFIXES: tuple[str, ...] = (
    "usually ",
    "typically ",
    "normally ",
    "sometimes ",
    "often ",
    "occasionally ",
    "would ",
    "used to ",
    "tend to ",
)

_OBS_LOOKBACK = 20


@dataclass(frozen=True)
class _FileEntry:
    path: str  # project-relative, forward-slash separated
    why: str
    keywords: tuple[str, ...]
    triggers: frozenset[str]
    priority: str  # "high" | "normal" | "low"
    severity: str = "normal"  # normal | high | critical | sovereign


# Cache keyed by (project_root, max_mtime_ns). Rebuild when any memory file
# changes — cheap because the walk is small.
_scan_cache: dict[tuple[str, int], dict[str, list[_FileEntry]]] = {}


def _parse_frontmatter(text: str) -> tuple[dict[str, object] | None, str]:
    """Return (frontmatter_dict, body). When no frontmatter is present,
    returns (None, full_text). Does not import a YAML library — we only
    need a handful of scalar/list keys.
    """
    if not text.startswith(_FRONTMATTER_START):
        return None, text
    rest = text[len(_FRONTMATTER_START) :]
    end_match = _FRONTMATTER_END_RE.search(rest)
    if not end_match:
        return None, text
    fm_text = rest[: end_match.start()]
    body = rest[end_match.end() :].lstrip("\n")
    meta: dict[str, object] = {}
    for raw_line in fm_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not value:
            meta[key] = ""
            continue
        if value.startswith("[") and value.endswith("]"):
            inside = value[1:-1].strip()
            if not inside:
                meta[key] = []
            else:
                meta[key] = [
                    item.strip().strip("'\"") for item in inside.split(",") if item.strip()
                ]
        else:
            meta[key] = value.strip("'\"")
    return meta, body


def _first_section_heading(body: str) -> str:
    """Extract a short one-line summary from the first markdown heading."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.lstrip("# ").strip()
    return ""


def _build_entry(project_root: Path, abs_path: Path, text: str) -> _FileEntry | None:
    meta, body = _parse_frontmatter(text)
    if not isinstance(meta, dict):
        return None
    raw_keywords = meta.get("keywords")
    if not isinstance(raw_keywords, list) or not raw_keywords:
        return None
    keywords = tuple(str(item).strip().lower() for item in raw_keywords if str(item).strip())
    if not keywords:
        return None

    raw_triggers = meta.get("trigger") or meta.get("triggers")
    if isinstance(raw_triggers, list):
        triggers = frozenset(str(t).strip().lower() for t in raw_triggers if str(t).strip())
    elif isinstance(raw_triggers, str) and raw_triggers.strip():
        triggers = frozenset({raw_triggers.strip().lower()})
    else:
        triggers = frozenset({"topic"})

    raw_priority = meta.get("priority")
    priority = "normal"
    if isinstance(raw_priority, str) and raw_priority.strip().lower() in _PRIORITY_ORDER:
        priority = raw_priority.strip().lower()

    why = _first_section_heading(body) or abs_path.stem.replace("_", " ")
    # Cap the reason so hint lines stay short.
    if len(why) > 60:
        why = why[:57].rstrip() + "…"

    # Paths are stored relative to the .MEMORY/ root so they line up with
    # memory_read's expected targets list (e.g. "config/infrastructure.md",
    # not ".MEMORY/config/infrastructure.md").
    memory_root = project_root / ".MEMORY"
    rel = abs_path.relative_to(memory_root).as_posix()
    return _FileEntry(
        path=rel,
        why=why,
        keywords=keywords,
        triggers=triggers,
        priority=priority,
    )


def _memory_tree_mtime_ns(memory_root: Path) -> int:
    """Highest mtime across the memory tree. Used as a cache-invalidation
    signal — if any file changes, the scan is redone.
    """
    if not memory_root.is_dir():
        return 0
    max_ns = 0
    for dirpath, _, files in os.walk(memory_root):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            max_ns = max(max_ns, st.st_mtime_ns)
    return max_ns


def _load_keyword_map_from_sql(
    project_root: Path,
) -> dict[str, list[_FileEntry]]:
    """Build the keyword→entries map from the memory_routes sql ledger.

    Returns {} when the route table is empty (signals filesystem fallback
    during transition before backfill). Sovereign-severity routes are
    EXCLUDED at the SQL layer — discovery never surfaces them. Deathbed-grant
    access uses a separate code path (future brick).
    """
    import sqlite3

    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_routes WHERE severity != 'sovereign'",
            ).fetchone()
            if not count_row or int(count_row["n"]) == 0:
                return {}
            # Phase-8 flip (2026-05-19): prefer memory_index (sqlite-
            # canonical) for the title enrichment, fall back to
            # memory_files only when memory_index has no row (cold-
            # SQLite-only doctrine (2026-06): INNER JOIN canonical active
            # memory_index. A route whose target has NO active canonical row is a
            # ROUTE-ONLY GHOST (the memory was removed/superseded, or the route
            # was never backed by a real entry) and must NOT surface. Removed the
            # cold-start `mi.path IS NULL` escape that let ghosts through.
            rows = conn.execute(
                """
                SELECT mrk.keyword, mr.target_path, mr.trigger, mr.priority,
                       mr.severity,
                       COALESCE(mi.title, '') AS title
                FROM memory_route_keywords mrk
                JOIN memory_routes mr ON mr.route_id = mrk.route_id
                JOIN memory_index mi ON mi.path = mr.target_path
                WHERE mr.severity != 'sovereign'
                  AND COALESCE(mi.status, 'active') = 'active'
                  AND COALESCE(mi.superseded_by, '') = ''
                """,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}

    by_path: dict[str, dict[str, object]] = {}
    for row in rows:
        path = str(row["target_path"])
        bucket = by_path.setdefault(
            path,
            {
                "keywords": set(),
                "trigger": str(row["trigger"]),
                "priority": str(row["priority"]),
                "severity": str(row["severity"]),
                "title": str(row["title"]),
            },
        )
        kw = str(row["keyword"]).strip().lower()
        if kw:
            bucket["keywords"].add(kw)  # type: ignore[union-attr]

    keyword_map: dict[str, list[_FileEntry]] = {}
    for path, bucket in by_path.items():
        keywords = tuple(sorted(bucket["keywords"]))  # type: ignore[arg-type]
        if not keywords:
            continue
        why = str(bucket["title"]) or path.rsplit("/", 1)[-1].removesuffix(".md")
        if len(why) > 60:
            why = why[:57].rstrip() + "…"
        triggers = frozenset({str(bucket["trigger"])})
        entry = _FileEntry(
            path=path,
            why=why,
            keywords=keywords,
            triggers=triggers,
            priority=str(bucket["priority"]),
            severity=str(bucket["severity"]),
        )
        for kw in keywords:
            keyword_map.setdefault(kw, []).append(entry)
    return keyword_map


def _scan_memory_files(project_root: Path) -> dict[str, list[_FileEntry]]:
    """Return a map from lower-cased keyword → list of matching file entries.

    SQLite-only doctrine (2026-06): the SQL route ledger (memory_routes /
    memory_route_keywords joined to canonical memory_index) is the SOLE source.
    The legacy filesystem-frontmatter fallback (walking .MEMORY/*.md) is REMOVED
    — loose markdown is invisible at runtime; import it via the explicit operator
    migrate_markdown_to_sqlite path.
    """
    return _load_keyword_map_from_sql(project_root)


def _is_observational(text: str, match_start: int) -> bool:
    """True if the keyword is immediately preceded by a habit adverb."""
    lookback = text[max(0, match_start - _OBS_LOOKBACK) : match_start].lower()
    return any(lookback.endswith(prefix) for prefix in _OBSERVATIONAL_PREFIXES)


def _keyword_matches_prompt(keyword: str, prompt_lower: str) -> int | None:
    """Return the index of the first non-observational occurrence of
    ``keyword`` in ``prompt_lower``, or None if none matches.

    Word-boundary logic is keyword-aware: multi-word keywords ("live server")
    match as substrings because a word-boundary regex is less useful for
    phrases; single-word keywords use \\b boundaries to avoid partial hits.
    """
    kw = keyword.strip().lower()
    if not kw:
        return None
    if " " in kw:
        # Phrase match — substring.
        idx = 0
        while True:
            found = prompt_lower.find(kw, idx)
            if found == -1:
                return None
            if _is_observational(prompt_lower, found):
                idx = found + 1
                continue
            return found
    # Single word — word-boundary regex.
    pattern = re.compile(rf"\b{re.escape(kw)}\b")
    for match in pattern.finditer(prompt_lower):
        if not _is_observational(prompt_lower, match.start()):
            return match.start()
    return None


def discover_relevant_memory(
    prompt: str,
    project_root: Path,
    *,
    action_kind: str | None = None,
    max_hints: int = _MAX_HINTS,
) -> list[MemoryHint]:
    """Surface memory files whose keywords match the prompt.

    Args:
        prompt: User prompt text.
        project_root: Project root containing a .MEMORY/ tree.
        action_kind: When provided, files whose ``trigger`` includes
            ``action`` are eligible regardless of keyword match — the
            file's action relevance is the trigger. Topic-trigger files
            still require a keyword match.
        max_hints: Cap on hints returned.

    Returns:
        Deduped list of MemoryHint, ranked by priority (high first) then
        by number of matched keywords (more specific first).

    """
    if not prompt or not prompt.strip() or not project_root:
        return []
    try:
        keyword_map = _scan_memory_files(project_root)
    except Exception:
        return []
    if not keyword_map:
        return []

    prompt_lower = prompt.lower()
    hits: dict[str, tuple[_FileEntry, set[str], int]] = {}
    for keyword, entries in keyword_map.items():
        match_idx = _keyword_matches_prompt(keyword, prompt_lower)
        if match_idx is None:
            continue
        for entry in entries:
            if "topic" not in entry.triggers and "action" not in entry.triggers:
                continue
            # Topic-trigger files surface on any keyword match.
            # Action-trigger files ALSO surface on keyword match; the flag
            # only matters for the prompt-agnostic action path below.
            existing = hits.get(entry.path)
            if existing is None:
                hits[entry.path] = (entry, {keyword}, match_idx)
            else:
                _, kws, first_idx = existing
                kws.add(keyword)
                hits[entry.path] = (entry, kws, min(first_idx, match_idx))

    # Action-type surfacing: files whose triggers include "action" and whose
    # keywords include the action_kind string also qualify. e.g. a file with
    # keywords=[implement, refactor] + trigger=[action] fires on action_kind
    # == "implement" even if the word "implement" isn't in the prompt.
    if action_kind:
        action_key = action_kind.strip().lower()
        for keyword, entries in keyword_map.items():
            if keyword != action_key:
                continue
            for entry in entries:
                if "action" not in entry.triggers:
                    continue
                existing = hits.get(entry.path)
                if existing is None:
                    hits[entry.path] = (entry, {keyword}, 10**9)
                else:
                    _, kws, first_idx = existing
                    kws.add(keyword)
                    hits[entry.path] = (entry, kws, first_idx)

    if not hits:
        return []

    ranked = sorted(
        hits.values(),
        key=lambda item: (
            _PRIORITY_ORDER.get(item[0].priority, 1),
            -len(item[1]),
            item[2],
            item[0].path,
        ),
    )
    return [
        MemoryHint(
            path=entry.path,
            why=entry.why,
            matched_keywords=tuple(sorted(matched)),
            severity=entry.severity,
        )
        for entry, matched, _ in ranked[:max_hints]
    ]


_TS_EXTS = (".tsx", ".ts", ".jsx", ".js")
_PY_EXTS = (".py",)


def _resolve_import_target(source_path: str, target: str, project_root: Path) -> str | None:
    """Resolve a code_edges target (module specifier) to a project-relative
    file path, or None when the target is external / unresolvable.

    Handles:
    - Relative imports: "./X" / "../X" — resolve via source's directory,
      try common extensions (.tsx, .ts, .jsx, .js, .py) and dir-index.
    - Bare imports for Python source: "X" → source_dir / X.py.
    - Scoped/external (e.g. "@tauri-apps/...", "react", or 3rd-party
      Python pkg) — return None; not in project tree.
    """
    raw = (target or "").strip()
    if not raw:
        return None
    # External-looking patterns we don't resolve
    if raw.startswith("@") or raw.startswith("/"):
        return None
    src = Path(source_path.replace("\\", "/"))
    src_dir = src.parent if src.parts else Path()
    # Pick extensions based on source language
    if str(src).endswith((".py", ".pyi")):
        exts = _PY_EXTS
    else:
        exts = _TS_EXTS
    if raw.startswith("./") or raw.startswith("../") or exts is _PY_EXTS:
        base = (src_dir / raw).as_posix()
    else:
        return None
    candidates: list[str] = []
    for ext in exts:
        candidates.append(base + ext)
        candidates.append(base + "/index" + ext)
    if exts is _PY_EXTS:
        candidates.append(base + "/__init__.py")
    for cand in candidates:
        cand_norm = Path(cand).as_posix()
        if (project_root / cand_norm).is_file():
            return cand_norm
    return None


def discover_memory_for_symbol(
    project_root: Path,
    symbol_name: str,
    file_path: str = "",
    *,
    max_hints: int = _MAX_HINTS,
    max_hops: int = 0,
) -> list[MemoryHint]:
    """Find memory entries anchored on a symbol or file (goggles hop-0 lookup).

    Two query modes:
      * Symbol-led: ``symbol_name`` non-empty → match by
        ``memory_symbol_anchors.symbol_name``. When ``file_path`` also
        supplied, anchors with the same file_path rank first.
      * File-led: ``symbol_name`` empty AND ``file_path`` non-empty →
        match by ``memory_symbol_anchors.file_path`` only. Wired for the
        x-ray goggles on pure file reads (Read/Edit on a path with no
        named symbol).

    Sovereign routes are filtered at the SQL layer. Results carry
    ``hop_depth=0`` and ``matched_symbol`` set to the anchor's symbol.

    Hop 1+ (anchors in imported files) walks code_edges from ``file_path``.
    """
    import sqlite3

    sym = (symbol_name or "").strip()
    fp = (file_path or "").replace("\\", "/").strip()
    if not sym and not fp:
        return []
    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            if sym:
                sql = (
                    "SELECT mr.route_id, mr.target_path, mr.severity, mr.priority, "
                    "msa.symbol_name AS anchor_symbol, "
                    "msa.file_path AS anchor_file, "
                    "COALESCE(mi.title, '') AS title "
                    "FROM memory_symbol_anchors msa "
                    "JOIN memory_routes mr ON mr.route_id = msa.route_id "
                    "JOIN memory_index mi ON mi.path = mr.target_path "
                    "WHERE msa.symbol_name = ? AND mr.severity != 'sovereign' "
                    # Suppress removed/superseded canonical rows
                    # (2026-05-20). Same shape as the keyword query.
                    "AND COALESCE(mi.status, 'active') = 'active' "
                    "AND COALESCE(mi.superseded_by, '') = '' "
                    "ORDER BY "
                    "  CASE WHEN msa.file_path = ? THEN 0 ELSE 1 END, "
                    "  CASE mr.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
                    "  mr.route_id"
                )
                rows = conn.execute(sql, (sym, fp)).fetchall()
            else:
                # File-led: no symbol, find anchors by file_path.
                sql = (
                    "SELECT mr.route_id, mr.target_path, mr.severity, mr.priority, "
                    "msa.symbol_name AS anchor_symbol, "
                    "msa.file_path AS anchor_file, "
                    "COALESCE(mi.title, '') AS title "
                    "FROM memory_symbol_anchors msa "
                    "JOIN memory_routes mr ON mr.route_id = msa.route_id "
                    "JOIN memory_index mi ON mi.path = mr.target_path "
                    "WHERE msa.file_path = ? AND mr.severity != 'sovereign' "
                    # Suppress removed/superseded canonical rows.
                    "AND COALESCE(mi.status, 'active') = 'active' "
                    "AND COALESCE(mi.superseded_by, '') = '' "
                    "ORDER BY "
                    "  CASE mr.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
                    "  mr.route_id"
                )
                rows = conn.execute(sql, (fp,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    hints: list[MemoryHint] = []
    seen: set[str] = set()
    for row in rows:
        path = str(row["target_path"])
        if path in seen:
            continue
        seen.add(path)
        why = str(row["title"]) or path.rsplit("/", 1)[-1].removesuffix(".md")
        if len(why) > 60:
            why = why[:57].rstrip() + "…"
        hints.append(
            MemoryHint(
                path=path,
                why=why,
                matched_keywords=(),
                severity=str(row["severity"]),
                hop_depth=0,
                matched_symbol=sym or str(row["anchor_symbol"]),
            ),
        )
        if len(hints) >= max_hints:
            break

    # ── hop 1: anchors in files that file_path imports (via code_edges) ──
    if max_hops >= 1 and fp and len(hints) < max_hints:
        imported_files: set[str] = set()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                for r in conn.execute(
                    "SELECT target FROM code_edges WHERE source_path = ?",
                    (fp,),
                ):
                    resolved = _resolve_import_target(fp, str(r["target"]), project_root)
                    if resolved:
                        imported_files.add(resolved)
                if imported_files:
                    placeholders = ",".join("?" * len(imported_files))
                    hop1_sql = (
                        "SELECT mr.route_id, mr.target_path, mr.severity, "
                        "msa.symbol_name AS anchor_symbol, "
                        "COALESCE(mi.title, '') AS title "
                        "FROM memory_symbol_anchors msa "
                        "JOIN memory_routes mr ON mr.route_id = msa.route_id "
                        "JOIN memory_index mi ON mi.path = mr.target_path "
                            f"WHERE msa.file_path IN ({placeholders}) "
                        "AND mr.severity != 'sovereign' "
                        # Suppress removed/superseded canonical rows.
                        "AND COALESCE(mi.status, 'active') = 'active' "
                    "AND COALESCE(mi.superseded_by, '') = '' "
                        "ORDER BY "
                        "  CASE mr.severity WHEN 'critical' THEN 0 "
                        "                   WHEN 'high'     THEN 1 "
                        "                   ELSE 2 END, "
                        "  mr.route_id"
                    )
                    for row in conn.execute(hop1_sql, list(imported_files)):
                        if len(hints) >= max_hints:
                            break
                        path = str(row["target_path"])
                        if path in seen:
                            continue
                        seen.add(path)
                        why = str(row["title"]) or path.rsplit("/", 1)[-1].removesuffix(".md")
                        if len(why) > 60:
                            why = why[:57].rstrip() + "…"
                        hints.append(
                            MemoryHint(
                                path=path,
                                why=why,
                                matched_keywords=(),
                                severity=str(row["severity"]),
                                hop_depth=1,
                                matched_symbol=str(row["anchor_symbol"]),
                            ),
                        )
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    return hints


def format_memory_hints(hints: Iterable[MemoryHint]) -> str:
    """Render memory hints as a single prompt-context line.

    Severity is a tag, not inline content (king's decree). Each entry is
    prefixed with [CRITICAL]/[HIGH] when applicable; [hop:N] when the
    entry was reached via symbol-anchor traversal at depth N>0;
    [symbol:NAME] when a hop-0 anchor match named the symbol. Sovereign
    entries are filtered upstream and never reach this renderer.
    """
    hint_list = list(hints)
    if not hint_list:
        return ""
    parts: list[str] = []
    for h in hint_list:
        markers: list[str] = []
        sev = (h.severity or "normal").lower()
        if sev == "critical":
            markers.append("[CRITICAL]")
        elif sev == "high":
            markers.append("[HIGH]")
        if h.hop_depth and h.hop_depth > 0:
            markers.append(f"[hop:{h.hop_depth}]")
        elif h.matched_symbol:
            markers.append(f"[symbol:{h.matched_symbol}]")
        prefix = (" ".join(markers) + " ") if markers else ""
        if h.why:
            parts.append(f"{prefix}`{h.path}` ({h.why})")
        else:
            parts.append(f"{prefix}`{h.path}`")
    paths = [h.path for h in hint_list]
    call_hint = f"memory_read(targets={paths!r})"
    return "Relevant project memory: " + ", ".join(parts) + f". Read with {call_hint}."


def clear_cache() -> None:
    """Test-only helper — drop the scan cache."""
    _scan_cache.clear()
