"""Bounded post-capture analyzer — derives route terms + candidate code
anchors for a freshly-captured memory (memory-loop seal, 2026-07-09).

Runs AFTER the canonical memory_index write, so nothing here can ever lose
or block a capture: the analyzer is advisory metadata generation only.

Inputs it reasons over:
  * the captured content, via ``NLPService.analyze_substance``
    (source=MEMORY_CAPTURE) — nouns / verbs / entities become derived terms;
  * code-shaped tokens in the content (CamelCase / snake_case / dotted),
    cross-checked against the project's ``code_outlines`` — a token that IS
    a real indexed symbol becomes both a derived term and a candidate anchor;
  * the target path + kind — path segments are cheap, always-available terms.

Output law:
  * Derived terms carry provenance='derived' and NEVER outrank explicit
    operator keywords (enforced by IndexStore.upsert_memory_route).
  * Candidate anchors are resolved through AidocsUnitResolver /
    CodeUnitVendor identity and are SEMANTIC_GUESS tier only — they surface
    on read but can never block an edit. Only explicit operator anchors
    (operator_pinned / exact_symbol) block. Law enters only through the
    throne (§31 memory poisoning).

Bounded: the whole analysis runs inside a worker thread under
``memory.capture_analyzer_timeout_ms`` (config, default 1500ms; 0 disables
the analyzer entirely). On timeout/error the result is empty+degraded —
capture proceeds exactly as before.
"""

from __future__ import annotations

import concurrent.futures as _cf
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_TIMEOUT_MS = 1500.0
_MAX_DERIVED_TERMS = 12
_MAX_CANDIDATE_ANCHORS = 5
_MIN_TERM_LEN = 3

# One shared single-worker executor: capture is not hot-path concurrent,
# and a stuck analysis must not stack threads (same doctrine as the
# read-surfacing executor).
_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem-capture-analyzer")

# Code-shaped tokens: CamelCase, snake_case with letters, dotted idents.
_CODE_TOKEN_RE = re.compile(
    r"\b(?:[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+"  # CamelCase (2+ humps)
    r"|[a-z_][a-z0-9]*(?:_[a-z0-9]+)+"  # snake_case (has underscore)
    r"|[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]+)+"  # dotted.path
    r")\b",
)

# Generic filler that would create noise routes.
_TERM_STOPLIST = frozenset(
    {
        "the", "and", "for", "with", "this", "that", "have", "must", "never",
        "always", "use", "not", "are", "was", "when", "then", "into", "from",
        "file", "files", "code", "project", "memory", "should", "will",
    },
)


@dataclass(frozen=True)
class CandidateAnchor:
    symbol: str
    file: str
    kind: str = "symbol"  # symbol | file
    confidence: str = "semantic_guess"  # NEVER a blocking tier from here
    unit_id: str = ""


@dataclass(frozen=True)
class CaptureAnalysis:
    derived_terms: tuple[str, ...] = ()
    candidate_anchors: tuple[CandidateAnchor, ...] = ()
    degraded: bool = False
    reason: str = ""
    elapsed_ms: float = 0.0
    provenance: dict = field(default_factory=dict)  # term -> lane that produced it


def _timeout_ms(project_root: Path) -> float:
    try:
        from .config import get_setting

        val = get_setting(
            "memory.capture_analyzer_timeout_ms",
            project_root=project_root,
            default=_DEFAULT_TIMEOUT_MS,
        )
        ms = float(val) if val is not None else _DEFAULT_TIMEOUT_MS
    except Exception:
        ms = _DEFAULT_TIMEOUT_MS
    if ms < 0:
        ms = _DEFAULT_TIMEOUT_MS
    return ms


def _nlp_terms(content: str, project_root: Path) -> set[str]:
    """Substance lemmas (nouns/verbs/entities) of the captured content.
    Empty set when the NLP pack is unavailable — the code/path lanes
    still produce terms, so captures stay discoverable."""
    try:
        from .aidocs_nlp.service import AnalysisSource, get_service

        svc = get_service(project_root)
        sub = svc.analyze_substance(content, source=AnalysisSource.MEMORY_CAPTURE)
        if sub is None:
            return set()
        out: set[str] = set()
        for tok in list(getattr(sub, "nouns", ()) or ()) + list(getattr(sub, "verbs", ()) or ()):
            lem = str(getattr(tok, "lemma", "") or getattr(tok, "text", "") or "").strip().lower()
            if lem:
                out.add(lem)
        for ent in getattr(sub, "entities", ()) or ():
            etxt = str(getattr(ent, "text", "") or "").strip().lower()
            if etxt:
                out.add(etxt)
        return out
    except Exception:
        return set()


def _code_tokens(content: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _CODE_TOKEN_RE.finditer(content or ""):
        tok = m.group(0)
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        out.append(tok)
        if len(out) >= 24:  # bounded scan
            break
    return out


def _match_code_symbols(
    tokens: list[str],
    project_root: Path,
) -> list[tuple[str, str]]:
    """Cross-check code-shaped tokens against code_outlines.
    Returns [(symbol, file_path)] for tokens that are REAL indexed symbols."""
    db = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not tokens or not db.is_file():
        return []
    out: list[tuple[str, str]] = []
    try:
        with sqlite3.connect(str(db)) as conn:
            for tok in tokens:
                # Dotted paths: try the tail symbol.
                sym = tok.rsplit(".", 1)[-1]
                rows = conn.execute(
                    "SELECT symbol, path FROM code_outlines WHERE symbol = ? LIMIT 2",
                    (sym,),
                ).fetchall()
                if rows:
                    out.append((str(rows[0][0]), str(rows[0][1])))
                if len(out) >= _MAX_CANDIDATE_ANCHORS:
                    break
    except sqlite3.Error:
        return out
    return out


def _path_terms(target_rel: str, kind: str) -> set[str]:
    out: set[str] = set()
    k = (kind or "").strip().lower()
    if k:
        out.add(k)
    stem = (target_rel or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0]
    for w in re.split(r"[-_\s]+", stem):
        w = w.strip().lower()
        if w:
            out.add(w)
    return out


def _analyze(
    *,
    project_root: Path,
    content: str,
    kind: str,
    target_rel: str,
    explicit_keywords: tuple[str, ...],
) -> CaptureAnalysis:
    start = time.monotonic()
    explicit = {str(k).strip().lower() for k in explicit_keywords if str(k).strip()}
    provenance: dict[str, str] = {}

    nlp = _nlp_terms(content, project_root)
    for t in nlp:
        provenance.setdefault(t, "nlp_substance")
    path_t = _path_terms(target_rel, kind)
    for t in path_t:
        provenance.setdefault(t, "target_path")
    tokens = _code_tokens(content)
    code_matches = _match_code_symbols(tokens, project_root)
    for sym, _fp in code_matches:
        provenance.setdefault(sym.lower(), "code_index")

    terms: list[str] = []
    for t in list(nlp) + [s.lower() for s, _ in code_matches] + list(path_t):
        t = t.strip().lower()
        if (
            not t
            or len(t) < _MIN_TERM_LEN
            or t in _TERM_STOPLIST
            or t in explicit
            or t in terms
        ):
            continue
        terms.append(t)
        if len(terms) >= _MAX_DERIVED_TERMS:
            break

    # Candidate anchors — resolved through the RFC-4 unit identity so the
    # anchor joins palace/code clusters. semantic_guess tier ONLY.
    anchors: list[CandidateAnchor] = []
    if code_matches:
        resolver = None
        try:
            from .palace_hub_extension import _resolve_project_uuid
            from .unit_resolver import AidocsUnitResolver

            db = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
            if db.is_file():
                resolver = AidocsUnitResolver(
                    project_root=project_root,
                    index_db_path=db,
                    project_uuid=_resolve_project_uuid(project_root),
                )
        except Exception:
            resolver = None
        seen_anchor: set[tuple[str, str]] = set()
        for sym, fp in code_matches:
            key = (sym, fp)
            if key in seen_anchor:
                continue
            seen_anchor.add(key)
            unit_id = ""
            if resolver is not None:
                try:
                    resolved = resolver.resolve_symbol(sym)
                    if resolved is not None:
                        unit_id = resolved.unit_id
                        fp = resolved.file_path or fp
                except Exception:
                    unit_id = ""
            anchors.append(
                CandidateAnchor(symbol=sym, file=fp, kind="symbol", unit_id=unit_id),
            )
            if len(anchors) >= _MAX_CANDIDATE_ANCHORS:
                break

    return CaptureAnalysis(
        derived_terms=tuple(terms),
        candidate_anchors=tuple(anchors),
        degraded=False,
        reason="",
        elapsed_ms=(time.monotonic() - start) * 1000.0,
        provenance={t: provenance.get(t, "") for t in terms},
    )


def analyze_capture(
    project_root: Path,
    *,
    content: str,
    kind: str,
    target_rel: str,
    explicit_keywords: tuple[str, ...] = (),
    timeout_ms: float | None = None,
) -> CaptureAnalysis:
    """Bounded entry point. Never raises; on timeout/disable/error returns
    an empty degraded result and capture proceeds exactly as before."""
    budget = _timeout_ms(project_root) if timeout_ms is None else float(timeout_ms)
    if budget == 0:
        return CaptureAnalysis(degraded=True, reason="disabled")
    try:
        future = _executor.submit(
            _analyze,
            project_root=project_root,
            content=content or "",
            kind=kind or "",
            target_rel=target_rel or "",
            explicit_keywords=tuple(explicit_keywords or ()),
        )
    except Exception:
        return CaptureAnalysis(degraded=True, reason="submit_failed")
    try:
        return future.result(timeout=budget / 1000.0)
    except _cf.TimeoutError:
        future.cancel()
        return CaptureAnalysis(degraded=True, reason="timeout")
    except Exception as exc:  # analyzer bug must never break capture
        return CaptureAnalysis(degraded=True, reason=type(exc).__name__)
