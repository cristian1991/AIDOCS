"""Memory-capture similarity gate.

Phase 5 (2026-05-15, doctrine refined 2026-05-17). Stands BEFORE every
capture and returns one of:

  INSERT                     — no overlap, write proceeds.
  REJECT                     — content+anchor exact match; refuse silently.
  CODE_TEACHES               — captured fact is structurally present in
                                code_outlines (3+ symbol matches); refuse.
  NEEDS_USER_CLARIFICATION   — similar anchor + content overlap OR
                                contradictory content with an existing
                                memory. Refuse write; return existing
                                memory paths. The AGENT must read them
                                and ASK THE USER what to do. No auto-
                                merge, no auto-supersede — humans curate
                                durable knowledge (Empire doctrine
                                2026-05-17).

The gate is pure: inspects existing memory + code indexes, returns a
decision. Callers act:
  - INSERT → proceed to file write
  - REJECT / CODE_TEACHES → return the refusal to the agent
  - NEEDS_USER_CLARIFICATION → return refusal + existing paths so the
    agent reads them and asks the user before re-attempting

Explicit `supersedes: <path>` frontmatter still works as the deliberate-
replace channel: if the agent (after user consultation) has decided the
old memory must be replaced, they include the frontmatter declaration
on the new capture. That bypasses the NEEDS_USER_CLARIFICATION refusal
because the user has already weighed in.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SUPERSEDES_RE = re.compile(r"^supersedes\s*:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)

_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "but",
        "this",
        "that",
        "it",
        "with",
        "by",
        "be",
        "as",
        "at",
    },
)


@dataclass
class GateDecision:
    decision: str  # 'insert' | 'reject' | 'code_teaches' | 'supersede' | 'needs_user_clarification'
    reason: str = ""
    target_path: str = ""  # the most-overlapping existing memory
    similarity: float = 0.0
    code_teaches_hits: int = 0
    # For NEEDS_USER_CLARIFICATION: all overlapping memory paths the
    # agent should read before asking the user what to do.
    conflicting_paths: tuple[str, ...] = ()


def _tokenize(text: str) -> list[str]:
    return [
        t
        for t in re.findall(r"[a-zA-Z][a-zA-Z\-]+", (text or "").lower())
        if t not in _STOP and len(t) >= 3
    ]


def _multiset_jaccard(a: list[str], b: list[str]) -> float:
    """Lemma-multiset Jaccard — sum(min) / sum(max) over the union."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    overlap = 0
    union = 0
    for token in set(ca) | set(cb):
        overlap += min(ca[token], cb[token])
        union += max(ca[token], cb[token])
    return overlap / union if union else 0.0


def _bigram_signature(content: str) -> list[tuple[str, str]]:
    tokens = _tokenize(content)
    return list(zip(tokens, tokens[1:]))


def _detect_supersedes(content: str) -> str | None:
    """Return the supersedes target path if frontmatter declares one."""
    m = _SUPERSEDES_RE.search(content or "")
    if m:
        return m.group(1).strip()
    return None


def _code_teaches_hits(
    conn: sqlite3.Connection,
    content: str,
    threshold: int = 3,
) -> int:
    """Count code_outlines.symbol rows whose name matches noun-bigram
    signatures from the content. Cheap heuristic: combine each bigram
    into 'word1word2' (no separator, mimicking CamelCase) and probe.
    """
    bigrams = _bigram_signature(content)
    if not bigrams:
        return 0
    hits = 0
    seen: set[str] = set()
    for left, right in bigrams[:8]:  # cap probe count
        # Form a CamelCase-ish probe: capitalize each part.
        probe = left.capitalize() + right.capitalize()
        if probe in seen:
            continue
        seen.add(probe)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM code_outlines WHERE symbol LIKE ?",
                (f"%{probe}%",),
            ).fetchone()
        except sqlite3.Error:
            continue
        if row and int(row[0]) > 0:
            hits += 1
            if hits >= threshold:
                break
    return hits


def _auto_merge_enabled(project_root: Path) -> bool:
    """Read the dashboard setting `memory.capture_gate.auto_merge`.

    Default False: agent must read overlapping memories and ask the
    user (Empire doctrine 2026-05-17 — humans curate). When the
    operator flips this True in the dashboard, the gate evaluates
    from code/context and decides on its own (UPGRADE on high
    similarity, REJECT on exact duplicate).
    """
    try:
        from .config import get_setting

        return bool(
            get_setting(
                "memory.capture_gate.auto_merge",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def evaluate(
    project_root: Path,
    *,
    content: str,
    anchor_symbols: list[str] | None = None,
    similarity_threshold: float = 0.7,
    code_teaches_threshold: int = 3,
) -> GateDecision:
    """Decide what to do with an incoming capture. Pure: reads the
    kingdom DB, does not write.

    Doctrine 2026-05-17 (Empire): when overlap is detected, the gate
    returns 'needs_user_clarification' by default — the agent must
    read existing memories and ask the user. Dashboard setting
    `memory.capture_gate.auto_merge=True` flips behavior to auto-
    evaluate (UPGRADE on high similarity) for operators who want
    less friction in trusted contexts.
    """
    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return GateDecision(decision="insert", reason="no_index_yet")

    # Explicit supersede declaration ALWAYS wins (Empire doctrine — no
    # inference).
    supersedes_target = _detect_supersedes(content)
    if supersedes_target:
        return GateDecision(
            decision="supersede",
            target_path=supersedes_target,
            reason=f"explicit_supersedes:{supersedes_target}",
        )

    anchors = list(anchor_symbols or [])
    incoming_tokens = _tokenize(content)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # Code-teaches check — runs regardless of anchors. If the
            # captured fact is structurally present in code (3+ symbol
            # matches), reject as redundant.
            code_hits = _code_teaches_hits(
                conn,
                content,
                threshold=code_teaches_threshold,
            )
            if code_hits >= code_teaches_threshold:
                return GateDecision(
                    decision="reject",
                    reason=(f"code_teaches_this:{code_hits}_symbol_matches"),
                    code_teaches_hits=code_hits,
                )

            # Anchor-aware similarity check. Find existing memories that
            # share any of the incoming anchors (or fall back to all
            # memory_files when no anchors provided).
            # SQLite-only: similarity reads canonical active memory_index content,
            # not the retired memory_files cache.
            _active = "COALESCE(mf.status,'active')='active' AND COALESCE(mf.superseded_by,'')=''"
            if anchors:
                placeholders = ",".join("?" * len(anchors))
                rows = conn.execute(
                    f"""SELECT DISTINCT mf.path, mf.content AS content_text
                        FROM memory_index mf
                        JOIN memory_routes mr ON mr.target_path = mf.path
                        JOIN memory_symbol_anchors msa ON msa.route_id = mr.route_id
                        WHERE msa.symbol_name IN ({placeholders}) AND {_active}""",
                    tuple(anchors),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT path, content AS content_text FROM memory_index mf WHERE {_active}",
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return GateDecision(decision="insert", reason="db_error_fallback")

    best_path = ""
    best_sim = 0.0
    overlapping_paths: list[str] = []
    for path, existing_content in rows:
        existing_tokens = _tokenize(str(existing_content or ""))
        sim = _multiset_jaccard(incoming_tokens, existing_tokens)
        if sim > best_sim:
            best_sim = sim
            best_path = str(path)
        # Track every path with non-trivial overlap so the agent gets
        # the full set of memories to read before asking the user.
        if sim >= similarity_threshold:
            overlapping_paths.append(str(path))

    if best_sim >= 1.0 - 1e-9:
        return GateDecision(
            decision="reject",
            reason="duplicate_exact_match",
            target_path=best_path,
            similarity=best_sim,
        )
    if best_sim >= similarity_threshold and best_path:
        # Dashboard-configurable: auto_merge=False (default) asks the
        # user via the agent; auto_merge=True lets the gate decide.
        if _auto_merge_enabled(project_root):
            return GateDecision(
                decision="upgrade",
                reason=f"auto_merge_enabled:merge:{best_sim:.2f}",
                target_path=best_path,
                similarity=best_sim,
                conflicting_paths=tuple(sorted(set(overlapping_paths))),
            )
        return GateDecision(
            decision="needs_user_clarification",
            reason=(
                f"existing_memory_overlap:{best_sim:.2f}. Read the "
                f"conflicting memories and ask the operator. To "
                f"replace, re-capture with `supersedes: <path>` "
                f"after user consent. Flip "
                f"memory.capture_gate.auto_merge=True in the "
                f"dashboard to skip this prompt."
            ),
            target_path=best_path,
            similarity=best_sim,
            conflicting_paths=tuple(sorted(set(overlapping_paths))),
        )
    return GateDecision(
        decision="insert",
        reason="novel",
        similarity=best_sim,
    )
