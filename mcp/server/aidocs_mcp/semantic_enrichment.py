"""#448 — Semantic layer for the two-sided judge + intent enrichment.

Three decided consumers (Emperor 2026-07-17/18), all served from the
OWNED stores (the per-project code index sqlite: ``code_files`` /
``code_outlines`` / ``code_edges``). The LSP guest only *refreshes*
those stores via the doctrine-XXXII joint
(``CodeIndexSyncService._lsp_joint_after_sync``); NOTHING here talks to
the guest inline and no verdict may DEPEND on it (#446/#436 — never
re-hot the hot path).

Consumer A — judge semantic enrichment (``judge_semantic_verdicts``):
    for commands/edits touching code files, classify what is being
    touched (gate file? test? vendored? config? mempalace?) so the
    heuristic judge can cite the semantic class. SEAM LAW
    (enrichment-never-weakens): this function only ever returns EXTRA
    verdict dicts to APPEND — it never sees or mutates the cascade's
    verdicts, so every existing refusal fires with enrichment on AND
    off. Failures degrade to "no enrichment" (fail-quiet); the caller
    wraps the whole call in try/except.

Consumer B — user-intent enrichment (``prompt_code_mention_block``):
    the UPS pipeline consults the code index for file/symbol mentions
    in the OPERATOR prompt (do-means-know substrate, #462). A prompt
    naming a real file/symbol gets it resolved into the intent context;
    the hint rides the existing additionalContext rail. Includes the
    intent-target reachability summary (item C's "decided direction"):
    the first resolved file's bounded reverse-dependency closure.

Consumer C — blast-radius tracing (``blast_radius_for_file``):
    an edit-time seam that computes the touched file's
    reverse-dependency radius over ``code_edges`` (import ∪
    semantic_ref — the exact union the smart-test selector walks) and
    attaches it to the edit audit event + surfaces a radius summary to
    the agent. MANDATORY per the 2026-07-18 ruling ("the agent NEEDS to
    be aware of everything") but performance-bounded: depth-capped,
    per-level capped, memoized per (file, mtime, index mtime).

Judge rule ids introduced here (registered in judge_taxonomy):
    SEMANTIC_CONTEXT    — safe_advisory, message improvement only.
    SEMANTIC_GATE_WRITE — confirmable_destructive, an ADDED refusal
                          ground: shell-redirect/in-place writes aimed
                          at gate/security-surface source files.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

# ── semantic path classification (pure, no I/O) ──────────────────────

# Security/gate surface basenames — files whose modification changes
# what the gates enforce. Conservative, basename-keyed; used to CITE
# the class and (for raw shell writes only) to add a refusal ground.
_GATE_BASENAMES = {
    "access_gate.py",
    "heuristic_judge.py",
    "judge_taxonomy.py",
    "hook_pipeline.py",
    "claude_hook.py",
    "bash_policy.py",
    "tool_policy.py",
    "tool_gate_service.py",
    "login_gate.py",
    "anticoup.py",
    "protected_file_runtime.py",
    "enforcement.py",
    "gate_confirm.py",
    "gate_health.py",
    "gate_tool.py",
    "semantic_enrichment.py",
    "grant_registration_judge.py",
    "canonical_intent_registry.py",
    "prompt_mutator.py",
    "shell_envelope.py",
    "shell_egress_service.py",
}

_CODE_EXTS = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".cs",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
)

_CONFIG_SUFFIXES = (".toml", ".ini", ".cfg", ".yaml", ".yml")
_CONFIG_BASENAMES = ("settings.json", "settings.local.json", "workflow-actions.json")
_VENDORED_MARKERS = (
    "node_modules/",
    "/vendor/",
    ".venv/",
    "site-packages/",
    "templates/webapp/assets/",
)


def classify_code_path(path: str) -> str:
    """Semantic class of a project path — pure string logic, no I/O.

    Returns one of: "gate", "test", "vendored", "config", "mempalace",
    "source", "other".
    """
    p = str(path or "").replace("\\", "/").strip().strip("'\"")
    if not p:
        return "other"
    low = p.lower()
    name = low.rsplit("/", 1)[-1]
    if low.startswith(".memory/") or "/.memory/" in low:
        return "mempalace"
    if any(marker in low for marker in _VENDORED_MARKERS):
        return "vendored"
    if name in _GATE_BASENAMES or name.startswith(("gate_", "outer_gate_")):
        return "gate"
    if name.startswith("test_") or "/tests/" in low or low.startswith("tests/"):
        return "test"
    if name in _CONFIG_BASENAMES or low.endswith(_CONFIG_SUFFIXES):
        return "config"
    if low.endswith(_CODE_EXTS):
        return "source"
    return "other"


# path-shaped token inside a shell command (must carry a slash or a
# known code/config extension to count — bare words are too noisy).
_PATH_TOKEN_RE = re.compile(r"""[A-Za-z0-9_.~/\\-]+\.[A-Za-z0-9_]{1,6}|[A-Za-z0-9_.~-]+/[A-Za-z0-9_.~/\\-]+""")

# write-shaped shell constructs aimed at a following path token
_SHELL_WRITE_RE = re.compile(
    r"""(?:>>?\s*|(?:\btee\b|\bsed\s+-i[^\s]*|\btruncate\s+(?:-s\s*\S+\s+)?)\s+)(?P<target>[A-Za-z0-9_.~/\\'"-]+)""",
)


def extract_path_tokens(text: str, *, cap: int = 12) -> list[str]:
    """Path-looking tokens from a command/prompt, bounded."""
    out: list[str] = []
    for m in _PATH_TOKEN_RE.finditer(str(text or "")):
        tok = m.group(0).strip("'\".,;:()")
        if not tok or tok in out:
            continue
        # require a slash or a code/config extension — drop bare version
        # numbers ("2.0"), flags and prose artifacts.
        low = tok.lower()
        if "/" not in tok and "\\" not in tok:
            if not low.endswith(_CODE_EXTS + _CONFIG_SUFFIXES) and not low.endswith(".json"):
                continue
        out.append(tok)
        if len(out) >= cap:
            break
    return out


# ── Consumer A — judge semantic enrichment (ADD-ONLY) ────────────────

SEMANTIC_CONTEXT_RULE_ID = "SEMANTIC_CONTEXT"
SEMANTIC_GATE_WRITE_RULE_ID = "SEMANTIC_GATE_WRITE"

_EDIT_TOOL_NAMES = (
    "edit",
    "write",
    "ai_edit_lines",
    "ai_batch_edit",
    "ai_replace",
    "ai_str_replace",
    "ai_anchor_replace",
    "ai_insert_lines",
    "ai_create_file",
)

_SHELL_TOOL_NAMES = (
    "bash",
    "ai_run",
    "powershell",
    "pwsh",
    "shell",
    "cmd",
    "wsl",
    "monitor",
)


def judge_semantic_verdicts(
    tool_name: str,
    tool_input: dict[str, object] | None,
    project_root: Path | None,
    *,
    existing_count: int = 0,
) -> list[dict[str, str]]:
    """Extra verdict dicts for the heuristic judge to APPEND (#448 A).

    STRUCTURAL never-weakens guarantee: the return value is a list of
    NEW verdicts only — this function cannot see, reorder, or remove
    the cascade's verdicts. Two shapes are produced:

    - SEMANTIC_GATE_WRITE (confirmable_destructive, risk=high): a raw
      shell write construct (redirect / tee / sed -i / truncate) whose
      target classifies as a GATE-surface source file. An ADDED refusal
      ground: the AIDOCS edit tools have their own gate stack, but a
      shell redirect into gate code bypasses it.
    - SEMANTIC_CONTEXT (safe_advisory, risk=low): only emitted when the
      cascade ALREADY produced verdicts (existing_count > 0) — it
      improves the message by citing the semantic classes touched. A
      clean command stays clean (never flips clean→non-clean).

    Pure string logic — no index/LSP I/O on this path (the judge is
    sub-millisecond; §XXXII keeps the guest off the hot path).
    """
    args = tool_input or {}
    name = str(tool_name or "").strip().lower()
    verdicts: list[dict[str, str]] = []

    touched: list[tuple[str, str]] = []  # (path, class)

    if name in _SHELL_TOOL_NAMES:
        command = str(args.get("command", "") or "")
        if command:
            for tok in extract_path_tokens(command):
                cls = classify_code_path(tok)
                if cls != "other":
                    touched.append((tok, cls))
            # gate-write refusal ground: write-shaped construct → gate file
            for m in _SHELL_WRITE_RE.finditer(command):
                target = m.group("target").strip("'\"")
                if classify_code_path(target) == "gate":
                    verdicts.append(
                        {
                            "rule_id": SEMANTIC_GATE_WRITE_RULE_ID,
                            "risk": "high",
                            "description": (
                                "Shell write construct targets a gate/security-surface "
                                "source file (semantic class: gate)."
                            ),
                            "evidence": target,
                            "recommendation": (
                                "Gate code is modified through the governed edit tools "
                                "(ai_replace / ai_batch_edit), never via shell redirects."
                            ),
                        },
                    )
                    break
    elif name in _EDIT_TOOL_NAMES:
        for key in ("path", "file_path"):
            val = str(args.get(key, "") or "")
            if val:
                cls = classify_code_path(val)
                if cls != "other":
                    touched.append((val, cls))
                break

    if touched and existing_count > 0:
        cited = ", ".join(f"`{p}` → {c}" for p, c in touched[:5])
        verdicts.append(
            {
                "rule_id": SEMANTIC_CONTEXT_RULE_ID,
                "risk": "low",
                "description": f"Semantic class of touched file(s): {cited}",
                "evidence": cited,
                "recommendation": (
                    "Informational — semantic file classes cited so refusal/allow "
                    "decisions and audit can name what is being touched."
                ),
            },
        )
    return verdicts


# ── owned-store access (read-only, fail-quiet) ───────────────────────


def _index_db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _connect_ro(project_root: Path) -> sqlite3.Connection | None:
    db = _index_db_path(project_root)
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


# ── Consumer C — blast radius (bounded, memoized) ────────────────────

# memo: key → (stamp, result). Bounded LRU-ish (dict insertion order).
_RADIUS_CACHE: dict[tuple[str, str], tuple[tuple[int, int], dict[str, object]]] = {}
_RADIUS_CACHE_MAX = 128
_radius_cache_hits = 0  # test-observable


def _module_candidates(rel_path: str) -> tuple[list[str], str]:
    """(exact target candidates, leaf) for reverse-dep matching.

    code_edges.target is a raw import specifier — absolute dotted
    ('aidocs_mcp.foo'), relative ('.foo', './foo'), or a path (LSP
    semantic_ref rows). Candidates over-match rather than under-match:
    the radius is an awareness layer, truncation-labeled, never a gate.
    """
    p = rel_path.replace("\\", "/").strip("/")
    no_ext = p.rsplit(".", 1)[0] if "." in p.rsplit("/", 1)[-1] else p
    leaf = no_ext.rsplit("/", 1)[-1]
    dotted_full = no_ext.replace("/", ".")
    candidates = [p, no_ext, dotted_full, leaf, f".{leaf}", f"./{leaf}"]
    # progressively shorter dotted suffixes: a/b/c → b.c (package roots
    # are unknowable generically; suffix chains cover the common ones)
    parts = no_ext.split("/")
    for i in range(1, len(parts)):
        candidates.append(".".join(parts[i:]))
    seen: set[str] = set()
    uniq = [c for c in candidates if c and not (c in seen or seen.add(c))]
    return uniq, leaf


def blast_radius_for_file(
    project_root: Path,
    rel_path: str,
    *,
    max_depth: int = 2,
    per_level_cap: int = 25,
    total_cap: int = 60,
) -> dict[str, object] | None:
    """Reverse-dependency radius of ``rel_path`` over code_edges (#448 C).

    Walks kind IN ('import','semantic_ref') — the same union the
    smart-test selector walks — from the touched file outward to the
    files that DEPEND on it, depth- and count-bounded, memoized per
    (file mtime, index mtime). Returns None when the index is absent
    (fail-quiet: the radius is awareness, never a gate).

    Shape: {"target", "dependents": [...], "dependent_count", "depth",
    "truncated", "summary"}.
    """
    global _radius_cache_hits
    root = Path(project_root)
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    db = _index_db_path(root)
    if not db.is_file():
        return None
    try:
        db_mtime = db.stat().st_mtime_ns
    except OSError:
        return None
    try:
        file_mtime = (root / rel).stat().st_mtime_ns
    except OSError:
        file_mtime = 0
    key = (str(root), rel)
    stamp = (file_mtime, db_mtime)
    cached = _RADIUS_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        _radius_cache_hits += 1
        return dict(cached[1])

    conn = _connect_ro(root)
    if conn is None:
        return None
    dependents: list[str] = []
    seen: set[str] = {rel}
    truncated = False
    try:
        frontier = [rel]
        depth = 0
        while frontier and depth < max_depth and len(dependents) < total_cap:
            depth += 1
            next_frontier: list[str] = []
            for path in frontier:
                candidates, leaf = _module_candidates(path)
                qmarks = ",".join("?" for _ in candidates)
                try:
                    rows = conn.execute(
                        "SELECT DISTINCT source_path FROM code_edges "
                        "WHERE kind IN ('import','semantic_ref') "
                        f"AND (target IN ({qmarks}) OR target LIKE ?)",
                        (*candidates, f"%.{leaf}"),
                    ).fetchall()
                except sqlite3.Error:
                    return None
                if len(rows) > per_level_cap:
                    truncated = True
                    rows = rows[:per_level_cap]
                for row in rows:
                    src = str(row["source_path"]).replace("\\", "/")
                    if src in seen:
                        continue
                    seen.add(src)
                    if len(dependents) >= total_cap:
                        truncated = True
                        break
                    dependents.append(src)
                    next_frontier.append(src)
            frontier = next_frontier
    finally:
        conn.close()

    result: dict[str, object] = {
        "target": rel,
        "dependents": dependents,
        "dependent_count": len(dependents),
        "depth": max_depth,
        "truncated": truncated,
        "summary": (
            f"blast radius `{rel}`: {len(dependents)} dependent file(s) "
            f"within {max_depth} hop(s)"
            + (" [truncated]" if truncated else "")
            + (
                " — " + ", ".join(f"`{d}`" for d in dependents[:5])
                + (" …" if len(dependents) > 5 else "")
                if dependents
                else ""
            )
        ),
    }
    if len(_RADIUS_CACHE) >= _RADIUS_CACHE_MAX:
        _RADIUS_CACHE.pop(next(iter(_RADIUS_CACHE)), None)
    _RADIUS_CACHE[key] = (stamp, dict(result))
    return result


# ── Consumer C rail: one-shot pending radius note ────────────────────
# _post_edit_reindex_and_grant stashes the computed radius here; the
# shared edit_result renderer drains it into the SAME edit response the
# agent reads. One-shot + TTL so a note can never leak onto an
# unrelated later response.

_PENDING_RADIUS: dict[str, object] | None = None
_PENDING_RADIUS_AT: float = 0.0
_PENDING_RADIUS_TTL_S = 30.0


def stash_radius_note(radius: dict[str, object] | None) -> None:
    # #375 Phase 3: a note is worth stashing when it carries dependents OR
    # anchored memories for the touched leaf — either is edit-time awareness.
    global _PENDING_RADIUS, _PENDING_RADIUS_AT
    if radius and (radius.get("dependent_count") or radius.get("anchored_memories")):
        _PENDING_RADIUS = dict(radius)
        _PENDING_RADIUS_AT = time.monotonic()


def take_pending_radius_note() -> dict[str, object] | None:
    global _PENDING_RADIUS
    note = _PENDING_RADIUS
    _PENDING_RADIUS = None
    if note is None:
        return None
    if (time.monotonic() - _PENDING_RADIUS_AT) > _PENDING_RADIUS_TTL_S:
        return None
    return note


# ── #375 Phase 3 (B) — memories anchored to a touched leaf ───────────
# The leaf anchors are BIDIRECTIONAL consumers: capture pins a memory to
# the smallest code-index leaf; editing that leaf surfaces the memory on
# the same rail the blast radius rides. Bounded + fail-quiet — awareness,
# never a gate (the blocking path stays edit_memory_gate's).


def memories_for_touched_leaf(
    project_root: Path,
    rel_path: str,
    symbol: str | None = None,
    *,
    cap: int = 5,
) -> list[dict[str, object]]:
    """Memories anchored at (or above) the touched leaf: rows in
    memory_symbol_anchors matching the edited file — or the specific
    symbol when known — joined to their route target and canonical title.
    Retired/superseded memories are filtered out. [] on any error."""
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if not rel:
        return []
    conn = _connect_ro(Path(project_root))
    if conn is None:
        return []
    out: list[dict[str, object]] = []
    try:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(memory_symbol_anchors)"
            ).fetchall()
        }
        if not cols:
            return []
        leaf_sel = (
            "COALESCE(msa.leaf_granularity, '')"
            if "leaf_granularity" in cols
            else "''"
        )
        params: list[object] = [rel]
        where = "msa.file_path = ?"
        if symbol:
            where += " OR (msa.symbol_name != '' AND msa.symbol_name = ?)"
            params.append(str(symbol))
        rows = conn.execute(
            f"SELECT msa.symbol_name, msa.anchor_kind, {leaf_sel} AS leaf, "
            "mr.target_path, COALESCE(mi.title, '') AS title "
            "FROM memory_symbol_anchors msa "
            "JOIN memory_routes mr ON mr.route_id = msa.route_id "
            "LEFT JOIN memory_index mi ON mi.path = mr.target_path "
            f"WHERE ({where}) "
            "AND (mi.path IS NULL OR ("
            "  COALESCE(mi.status,'active') = 'active' "
            "  AND COALESCE(mi.superseded_by,'') = '')) "
            "ORDER BY msa.anchor_id DESC LIMIT ?",
            (*params, max(1, int(cap))),
        ).fetchall()
        seen: set[str] = set()
        for r in rows:
            target = str(r["target_path"])
            if target in seen:
                continue
            seen.add(target)
            out.append(
                {
                    "memory_path": target,
                    "title": str(r["title"] or ""),
                    "symbol": str(r["symbol_name"] or ""),
                    "anchor_kind": str(r["anchor_kind"] or ""),
                    "granularity": str(r["leaf"] or ""),
                },
            )
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def attach_anchored_memories(
    project_root: Path,
    rel_path: str,
    radius: dict[str, object] | None,
    *,
    cap: int = 5,
) -> dict[str, object] | None:
    """Fold the touched leaf's anchored memories into the edit-time note
    (the blast-radius dict when present, a minimal note otherwise).
    Fail-quiet: any error returns ``radius`` unchanged."""
    try:
        memories = memories_for_touched_leaf(project_root, rel_path, cap=cap)
    except Exception:
        return radius
    if not memories:
        return radius
    note: dict[str, object] = dict(radius) if radius else {
        "target": str(rel_path or "").replace("\\", "/").strip("/"),
        "dependents": [],
        "dependent_count": 0,
        "depth": 0,
        "truncated": False,
        "summary": "",
    }
    note["anchored_memories"] = memories
    mem_bits = ", ".join(
        f"`{m['memory_path']}`" + (f" ({m['title']})" if m.get("title") else "")
        for m in memories[:3]
    )
    mem_line = (
        f"anchored memories for this leaf: {len(memories)} — {mem_bits}"
        + (" …" if len(memories) > 3 else "")
    )
    base = str(note.get("summary") or "")
    note["summary"] = f"{base} | {mem_line}" if base else mem_line
    return note


# ── Consumer B — operator-prompt code-mention resolution ─────────────

_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})`|\b([a-z_][a-z0-9]*(?:_[a-z0-9]+)+)\b")


def prompt_code_mention_block(
    project_root: Path,
    prompt: str,
    *,
    max_hints: int = 5,
) -> str:
    """Resolve file/symbol mentions in the OPERATOR prompt (#448 B).

    Consults the owned code index (never the LSP guest) for path-like
    and identifier-like tokens in the prompt. Returns an
    additionalContext block ("" when nothing resolves / index absent /
    any error — fail-quiet, the rail is advisory).

    Includes the intent-target reachability summary for the FIRST
    resolved file (item C's decided delivery direction: a bounded
    closure summary enriches the investigation context automatically).
    """
    text = str(prompt or "")
    if not text.strip():
        return ""
    conn = _connect_ro(Path(project_root))
    if conn is None:
        return ""
    hints: list[str] = []
    first_file: str | None = None
    try:
        # 1) path-like mentions → code_files
        for tok in extract_path_tokens(text, cap=8):
            if len(hints) >= max_hints:
                break
            norm = tok.replace("\\", "/").strip("/")
            try:
                row = conn.execute(
                    "SELECT path, language, line_count, role FROM code_files "
                    "WHERE path = ? OR path LIKE ? LIMIT 1",
                    (norm, f"%/{norm}"),
                ).fetchone()
            except sqlite3.Error:
                return ""
            if row is not None:
                cls = classify_code_path(str(row["path"]))
                hints.append(
                    f"- `{row['path']}` — indexed {row['language'] or 'file'}, "
                    f"{row['line_count']} lines, class: {cls}"
                    + (f", role: {row['role']}" if row["role"] else ""),
                )
                if first_file is None:
                    first_file = str(row["path"])
        # 2) identifier-like mentions → code_outlines symbols
        seen_syms: set[str] = set()
        for m in _IDENT_RE.finditer(text):
            if len(hints) >= max_hints:
                break
            sym = (m.group(1) or m.group(2) or "").strip()
            if not sym or sym in seen_syms:
                continue
            seen_syms.add(sym)
            if len(seen_syms) > 8:
                break
            try:
                row = conn.execute(
                    "SELECT path, symbol, kind, line_number FROM code_outlines "
                    "WHERE symbol = ? LIMIT 1",
                    (sym,),
                ).fetchone()
            except sqlite3.Error:
                return ""
            if row is not None:
                hints.append(
                    f"- `{row['symbol']}` — {row['kind']} in "
                    f"`{row['path']}`:{row['line_number']}",
                )
                if first_file is None:
                    first_file = str(row["path"])
    finally:
        conn.close()

    if not hints:
        return ""

    lines = [
        "🧭 Code targets resolved from the prompt (#448 do-means-know):",
        *hints,
    ]
    if first_file:
        try:
            radius = blast_radius_for_file(Path(project_root), first_file)
        except Exception:
            radius = None
        if radius and radius.get("dependent_count"):
            lines.append(f"- {radius['summary']}")
    lines.append(
        "Trace before acting: dependents above may rely on the named "
        "target (ai_trace / ai_bundle).",
    )
    return "\n".join(lines)
