"""Blast-radius completion envelope — PHASE 1, OBSERVE ONLY (#1046).

WHAT THIS ANSWERS, and nothing else:

    Before this agent stops, has it accounted for the materially
    connected surfaces of the code it changed?

That is HOST/RUNTIME GOVERNANCE. It works for ANY AIDOCS-managed
project — one with no deploy script, no release pipeline, no CI, and
possibly not even Git-backed. There is deliberately ZERO knowledge of
any deployment script, ship stage, commit sha or write-tree anywhere in
this module; #1046 cut all five explicitly after an earlier revision
dragged them in. A project's deploy gate MAY someday ask "are there
unresolved blast obligations?" — that is one more optional CONSUMER of
``stop_diagnostic``, never a participant in this design.

THE DEFECT THIS EXISTS TO CATCH (#925j). The no-argument installer
branch shipped "complete and proven" with 8 end-to-end tests. True, and
nearly useless: ``ensure_claude_hooks`` had a production consumer at
``cli.py`` ``cmd_setup`` that passed an explicit python_path and
defeated the whole law. A reference lookup would have shown it. #925k
had to redo all of it afterwards. So the acceptance test for this module
is not "does the ledger persist" — it is "does editing
``ensure_claude_hooks`` surface ``cmd_setup`` as an obligation".

PHASE 1 IS OBSERVE ONLY. Nothing here blocks a single Stop. It computes,
it records, and ``stop_diagnostic`` reports what a FUTURE gate WOULD
have refused. The blocking flip is Phase 2 and lives at exactly one
seam — ``StopDiagnostic.would_refuse`` — so turning it on is a decision
about one boolean, not a rewrite.

FOUR LOAD-BEARING RULES, each of which has a test:

1. UNKNOWN IS NOT ZERO. An unavailable index, an unindexed symbol or a
   reference sweep that ran out of budget records ``unknown``. It must
   NEVER record "0 dependents", because the whole failure class is a
   local fix declaring itself complete over surfaces nobody looked at,
   and a fabricated zero is indistinguishable from a real clean.

2. DEFERRAL IS NOT A DISPOSITION (#1046 amendment 1).
       resolved   = fixed | unaffected | covered_by_invariant
       unresolved = unchecked | unknown | blocked_unavailable
   ``blocked_unavailable`` may let the TURN end — an agent cannot repair
   the evidence service — but the envelope stays open and loudly marked.
   Availability failure may DEFER proof; it may never SATISFY proof.
   Making blocked_unavailable a disposition turns rule 1 into
   "unknown == waived" one layer down, which is the same bug wearing a
   hat.

3. IDENTITY IS ACTOR + SESSION, NOT TASK (#1046 amendment 5). Much of
   the #925 work ran with no open task at all. Keying the epoch on
   task_id makes the gate silently no-op exactly where it is most
   needed, which is the worst failure mode a safety gate can have. Task
   is available as a grouping axis and is never a key.

4. A SUBAGENT'S UNRESOLVED OBLIGATIONS MERGE UPWARD (#1046 amendment 3).
   Delegation is normal here, not exceptional: on the same night #925j
   shipped, a subagent edited ``server_code_edit_tools.py`` and its test
   and the PARENT committed that work. A child exit that discards
   unresolved obligations launders them.

THE LEDGER IS AIDOCS-OWNED. It is a sqlite store written by the governed
edit path, never an agent-editable file — agents must not hand-author
the evidence proving their own closure. The wording throughout is
"PRESENTED", not "examined": the system can prove an agent was shown a
reference; it cannot prove cognition, and a law statement must not
re-license that overclaim. The artifact-bound DISPOSITION carries the
examination claim, because that is the only checkable part.

ONE BLAST COMPUTATION. File radius comes from
``semantic_enrichment.blast_radius_for_file`` and symbol consumers from
``CodeIndexStore.find_references`` — the existing authorities. This
module builds no rival graph; the plan's own §11 says so and this
codebase has a documented history of twin-implementation defects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# ── Status vocabulary (#1046 amendment 1) ────────────────────────────

RESOLVED_STATUSES = frozenset({"fixed", "unaffected", "covered_by_invariant"})
UNRESOLVED_STATUSES = frozenset({"unchecked", "unknown", "blocked_unavailable"})
ALL_STATUSES = RESOLVED_STATUSES | UNRESOLVED_STATUSES

# Surface kinds. `unknown_surface` is its own kind ON PURPOSE: it is the
# record of a LOOKUP THAT DID NOT ANSWER, which is a different fact from
# a consumer that was found and not checked. Collapsing the two is how
# "the index was down" becomes "there was nothing there".
KIND_TOUCHED_FILE = "touched_file"
KIND_CHANGED_SYMBOL = "changed_symbol"
KIND_FILE_DEPENDENT = "file_dependent"
KIND_SYMBOL_CONSUMER = "symbol_consumer"
KIND_TEST_SURFACE = "test_surface"
KIND_UNKNOWN_SURFACE = "unknown_surface"
# An IOU for a reference sweep that has not run yet. Distinct from
# `unknown` on purpose: unknown means the question was ASKED and not
# answered, pending means it has not been asked. Collapsing them would
# let a deliberate deferral read as an evidence outage.
KIND_PENDING_CONSUMERS = "pending_consumers"

# The reference sweep's own honesty contract (#482) maps straight onto
# rule 1. `no_references` is the ONLY empty that means zero; the other
# two mean the question was not answered.
_UNKNOWN_EMPTY_REASONS = frozenset({"symbol_not_indexed", "timed_out"})

_ENVELOPE_VERSION_TAG = "blast-envelope:v1:"

# Bounds. Stop stays cheap and incremental (#1046 amendment 8) — this
# must never attempt whole-tree truth.
_MAX_CHANGED_SYMBOLS = 12
_MAX_CONSUMERS_PER_SYMBOL = 25
_REFERENCE_BUDGET_S = 3.0


def _db_path(project_root: Path) -> Path:
    """The blast ledger's own store.

    NOT the code index (which is a rebuildable projection — deleting it
    must never delete an obligation) and NOT the identity db (whose
    contract is pure derivation plus one mutable counter). An obligation
    is durable governance state and owns its file.
    """
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs_blast.sqlite3"


_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS blast_epoch (
    envelope_key     TEXT PRIMARY KEY,
    project_uuid     TEXT NOT NULL,
    lineage_id       TEXT NOT NULL,
    lineage_resolved INTEGER NOT NULL DEFAULT 0,
    epoch            INTEGER NOT NULL DEFAULT 0,
    envelope_seq     INTEGER NOT NULL DEFAULT 0,
    last_mutation_at REAL
);
CREATE TABLE IF NOT EXISTS blast_surface (
    envelope_key        TEXT NOT NULL,
    envelope_seq        INTEGER NOT NULL,
    surface_id          TEXT NOT NULL,
    kind                TEXT NOT NULL,
    origin              TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    evidence            TEXT NOT NULL DEFAULT '[]',
    novel               INTEGER NOT NULL DEFAULT 1,
    first_seen_epoch    INTEGER NOT NULL,
    last_seen_epoch     INTEGER NOT NULL,
    presented_at        REAL,
    deferred_from_seq   INTEGER,
    deferred_from_epoch INTEGER,
    merged_from         TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL,
    PRIMARY KEY (envelope_key, envelope_seq, surface_id)
);
CREATE INDEX IF NOT EXISTS idx_blast_surface_open
    ON blast_surface (envelope_key, envelope_seq, status);
CREATE TABLE IF NOT EXISTS blast_symbol_baseline (
    path       TEXT NOT NULL,
    symbol_key TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    sha        TEXT NOT NULL,
    header_sha TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (path, symbol_key)
);
CREATE TABLE IF NOT EXISTS blast_file_lineage (
    envelope_key TEXT NOT NULL,
    path         TEXT NOT NULL,
    depth        INTEGER NOT NULL,
    pre_sha      TEXT NOT NULL DEFAULT '',
    post_sha     TEXT NOT NULL,
    epoch        INTEGER NOT NULL,
    novel        TEXT NOT NULL DEFAULT '[]',
    settled      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (envelope_key, path, depth)
);
CREATE TABLE IF NOT EXISTS blast_stop_marker (
    envelope_key TEXT PRIMARY KEY,
    blocked_at   REAL NOT NULL
);
"""

# Sentinel row: "a baseline WAS taken for this path" even when the file
# defined no symbols, so an empty baseline is not mistaken for no baseline.
_BASELINE_SENTINEL = "\x00baseline"


def _connect(project_root: Path) -> sqlite3.Connection:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    # AUDIT, not RUNTIME: an obligation whose ABSENCE after a crash would
    # itself be a finding is the enum's own definition of evidence. A
    # ledger that can lose the last unresolved surface to a power cut
    # fails open, which is the one direction this must never fail.
    conn = _canonical_connect(db, durability=_Durability.AUDIT)
    conn.executescript(_TABLE_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(blast_symbol_baseline)").fetchall()}
    if "header_sha" not in cols:
        conn.execute(
            "ALTER TABLE blast_symbol_baseline ADD COLUMN header_sha TEXT NOT NULL DEFAULT ''"
        )
    return conn


def _sha16(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Identity (#1046 amendment 5) ─────────────────────────────────────


@dataclass(frozen=True)
class EnvelopeIdentity:
    """Project + actor lineage + host/session/context. No task_id."""

    envelope_key: str
    project_uuid: str
    lineage_id: str
    lineage_resolved: bool


def envelope_identity(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> EnvelopeIdentity:
    """Resolve the envelope key through the ONE identity authority.

    ``agent_memory_epoch`` is the project's single resolver for
    ``(host_kind, host_session_id)`` and the only place a subagent's
    lineage is derived from its parent's already-hashed id. Re-deriving
    either here would fork resolution a seventh way, and forking that
    resolution is literally how #539 happened.

    THE UNATTRIBUTED BUCKET, and why it is not a fabrication.
    ``derive_agent_context_id`` returns "" for an unresolvable host so
    that kind-less hosts cannot COLLIDE into one identity — correct
    there, because that id keys strikes, freezes and read grants, where
    a collision is an authority bypass. Here the failure runs the other
    way: an empty lineage would make the envelope silently no-op, which
    is amendment 5's defect in a different coat. So an unresolvable
    lineage lands in an explicit per-project ``unattributed`` bucket
    with ``lineage_resolved=False`` recorded on the row. That
    over-reports (two unidentified actors share one envelope and each
    sees the other's obligations) and never under-reports. For a ledger
    whose whole job is to refuse to look clean, noisy beats silent —
    and the flag makes the imprecision visible instead of implied.
    """
    from .agent_memory_epoch import derive_agent_context_id, derive_project_uuid

    project_uuid = derive_project_uuid(project_root)
    try:
        from .agent_memory_epoch import resolve_host_identity

        kind, sid = resolve_host_identity(
            host_kind=host_kind,
            host_session_id=host_session_id,
            project_root=project_root,
        )
    except Exception:
        kind, sid = ("", "")
    lineage = ""
    if kind and sid:
        lineage = derive_agent_context_id(
            host_kind=kind,
            project_root=project_root,
            host_session_id=sid,
            agent_id=agent_id,
        )
    resolved = bool(lineage)
    if not resolved:
        lineage = f"unattributed:{project_uuid}"
    return EnvelopeIdentity(
        envelope_key=_sha16(_ENVELOPE_VERSION_TAG + project_uuid + ":" + lineage),
        project_uuid=project_uuid,
        lineage_id=lineage,
        lineage_resolved=resolved,
    )


# ── Surfaces ─────────────────────────────────────────────────────────


@dataclass
class Surface:
    """One materially connected surface, and what is known about it."""

    surface_id: str
    kind: str
    status: str
    origin: str = ""
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


def _is_test_path(rel: str) -> bool:
    """One predicate, used by every caller that asks the question."""
    low = rel.replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    return (
        "/tests/" in low
        or low.startswith("tests/")
        or base.startswith("test_")
        or base.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts"))
    )


def _outline_symbols(project_root: Path, rel: str) -> tuple[list[tuple[str, int]], bool]:
    """``(symbols, answered)`` for ``rel``.

    The second element is the whole point, and it is NOT derivable from
    the first. ``get_outline`` returns ``[]`` both for "this file is
    indexed and defines nothing" and for "this file was never indexed" —
    fine for a viewer, fatal here, because the second case silently
    becomes "no changed symbols, therefore no consumers, therefore
    clean". That is rule 1's violation at the very first hop, so the
    question is answered from ``code_files`` membership instead: a file
    with no index row means the outline was NOT answered.
    """
    try:
        from .code_index_store import CodeIndexStore

        store = CodeIndexStore()
        store.init_db(project_root)
        with store.connect(project_root) as conn:
            known = conn.execute(
                "SELECT 1 FROM code_files WHERE path = ? LIMIT 1", (rel,)
            ).fetchone()
        if known is None:
            return ([], False)
        outline = store.get_outline(project_root, rel)
    except Exception:
        return ([], False)
    out: list[tuple[str, int]] = []
    for row in outline or []:
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            line = int(row.get("line_number") or 0)
        except (TypeError, ValueError):
            line = 0
        out.append((sym, line))
    return (out, True)


SEVERITY_CONTRACT = "contract"  # signature changed or symbol removed: callers can break
SEVERITY_BODY = "body"  # implementation changed: callers still compile, behaviour may not hold
SEVERITY_ADDED = "added"  # new symbol: nothing can depend on it yet by name

_HEADER_MAX_LINES = 12


def _symbol_header(block: list[str]) -> str:
    """The declaration part of a symbol: from its first line to the line that
    opens its body (``:`` for Python, ``{`` / ``=>`` for brace languages), capped.
    A symbol whose body never opens (a constant) is all header — its value IS
    its contract."""
    head: list[str] = []
    for line in block[:_HEADER_MAX_LINES]:
        head.append(line.rstrip())
        s = line.split("#", 1)[0].rstrip()
        if s.endswith(":") or "{" in s or "=>" in s:
            break
    return "\n".join(head).strip()


def _symbol_hashes(
    text: str, own: list[tuple[str, int]]
) -> dict[str, tuple[str, str, str]]:
    """``{symbol_key: (symbol, sha, header_sha)}`` — each symbol's text from its
    line to the next symbol's line. The outline carries no end line, so a span
    may run over trailing comments: that errs toward MORE changed symbols,
    never fewer. Duplicate names (methods in two classes, overloads) are keyed
    by occurrence so one cannot mask the other."""
    lines = text.splitlines()
    ordered = sorted(own, key=lambda s: (s[1], s[0]))
    starts = sorted({max(1, ln) for _, ln in ordered})
    seen: dict[str, int] = {}
    out: dict[str, tuple[str, str, str]] = {}
    for sym, ln in ordered:
        start = max(1, ln)
        nxt = next((s for s in starts if s > start), len(lines) + 1)
        block = lines[start - 1 : nxt - 1]
        # Trailing whitespace is layout, not the symbol: deleting the NEXT
        # symbol must not mark this one changed.
        body = "\n".join(block).rstrip()
        n = seen.get(sym, 0)
        seen[sym] = n + 1
        out[f"{sym}#{n}"] = (sym, _sha16(body), _sha16(_symbol_header(block)))
    return out


def symbol_changes_for_edit(project_root: Path, rel_path: str) -> dict[str, str] | None:
    """``{symbol: severity}`` for the symbols THIS edit changed, or ``None`` when
    that is not known.

    Compares the post-edit file (read after the index refresh) with the
    per-symbol baseline stored by the previous governed edit of the same file,
    then stores the new baseline. ``None`` — the first governed edit of a file,
    an unanswered outline or an unreadable file — keeps the existing
    over-inclusive behaviour (every symbol is a candidate): UNKNOWN IS NOT ZERO.
    A baseline older than the file on disk (an edit outside the gate) only
    widens the set, never narrows it.

    Severity (r0b, #1075): an unchanged signature does NOT mean no blast. A body
    change keeps its consumer obligation at ``body`` severity; only a proven
    absence of external consumers may drop it, and that is decided by the sweep,
    never here. Removed symbols and changed headers are ``contract``.
    """
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    own, available = _outline_symbols(project_root, rel)
    if not available:
        return None
    try:
        text = (Path(project_root) / rel).read_text(encoding="utf-8")
    except Exception:
        return None
    current = _symbol_hashes(text, own)
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT symbol_key, symbol, sha, header_sha FROM blast_symbol_baseline WHERE path = ?",
            (rel,),
        ).fetchall()
        conn.execute("DELETE FROM blast_symbol_baseline WHERE path = ?", (rel,))
        conn.executemany(
            "INSERT INTO blast_symbol_baseline (path, symbol_key, symbol, sha, header_sha)"
            " VALUES (?, ?, ?, ?, ?)",
            [(rel, _BASELINE_SENTINEL, "", "", "")]
            + [(rel, key, sym, sha, hsha) for key, (sym, sha, hsha) in current.items()],
        )
        conn.commit()
    if not rows:
        return None
    baseline = {r[0]: (r[1], r[2], r[3]) for r in rows if r[0] != _BASELINE_SENTINEL}
    rank = {SEVERITY_ADDED: 0, SEVERITY_BODY: 1, SEVERITY_CONTRACT: 2}
    changes: dict[str, str] = {}

    def _note(sym: str, sev: str) -> None:
        if rank[sev] > rank.get(changes.get(sym, ""), -1):
            changes[sym] = sev

    for key, (sym, sha, hsha) in current.items():
        old = baseline.get(key)
        if old is None:
            _note(sym, SEVERITY_ADDED)
        elif old[2] != hsha:
            _note(sym, SEVERITY_CONTRACT)
        elif old[1] != sha:
            _note(sym, SEVERITY_BODY)
    for key, (sym, _sha, _hsha) in baseline.items():
        if key not in current:
            _note(sym, SEVERITY_CONTRACT)
    return changes


def changed_symbols_for_edit(project_root: Path, rel_path: str) -> list[str] | None:
    """Names only — see ``symbol_changes_for_edit``."""
    changes = symbol_changes_for_edit(project_root, rel_path)
    return None if changes is None else list(changes)


def _containing_symbol(symbols: list[tuple[str, int]], line: int) -> str:
    """Attribute a reference line to the symbol that encloses it.

    THIS IS THE #925j STEP. "cli.py references ensure_claude_hooks" is a
    fact an agent skims past; "cli.py cmd_setup is a production consumer
    of ensure_claude_hooks" is the sentence that would have stopped the
    incident. Last-definition-at-or-before, over the index's own ordered
    outline — cheap, and correct for the nested case too, since a method
    is outlined after its class.
    """
    best = ""
    best_line = -1
    for sym, sym_line in symbols:
        if 0 < sym_line <= line and sym_line > best_line:
            best = sym
            best_line = sym_line
    return best


def _file_dependent_surfaces(project_root: Path, rel: str) -> list[Surface]:
    """Reverse-dependency radius, via the EXISTING computation."""
    try:
        from .semantic_enrichment import blast_radius_for_file

        radius = blast_radius_for_file(Path(project_root), rel)
    except Exception:
        radius = None
    if radius is None:
        # Rule 1. `blast_radius_for_file` returns None for an absent or
        # unreadable index — fail-quiet is right for an ADVISORY, and
        # dead wrong for an obligation ledger, so the None is promoted
        # into a loud unknown rather than swallowed.
        return [
            Surface(
                surface_id=f"unknown:file_dependents:{rel}",
                kind=KIND_UNKNOWN_SURFACE,
                status="unknown",
                origin=rel,
                reason="reverse-dependency index unavailable — dependents NOT known to be zero",
            )
        ]
    out: list[Surface] = []
    for dep in radius.get("dependents") or []:
        dep = str(dep).replace("\\", "/")
        kind = KIND_TEST_SURFACE if _is_test_path(dep) else KIND_FILE_DEPENDENT
        out.append(
            Surface(
                surface_id=f"{kind}:{dep}",
                kind=kind,
                status="unchecked",
                origin=rel,
                reason=f"depends on `{rel}`",
            )
        )
    if radius.get("truncated"):
        # A truncated walk is a partially answered question. Saying
        # nothing here would let a capped radius read as an exhaustive
        # one — rule 1 at the cap boundary.
        out.append(
            Surface(
                surface_id=f"unknown:file_dependents_truncated:{rel}",
                kind=KIND_UNKNOWN_SURFACE,
                status="unknown",
                origin=rel,
                reason="dependent walk hit its depth/count cap — the radius is a floor, not a total",
            )
        )
    return out


def _symbol_consumer_surfaces(
    project_root: Path,
    rel: str,
    symbol: str,
    symbols_by_path: dict[str, list[tuple[str, int]]],
) -> list[Surface]:
    """Consumers of one changed symbol, via the EXISTING reference sweep."""
    try:
        from .code_index_store import CodeIndexStore

        refs = CodeIndexStore().find_references(
            project_root,
            symbol,
            limit=_MAX_CONSUMERS_PER_SYMBOL,
            budget_seconds=_REFERENCE_BUDGET_S,
        )
    except Exception as exc:
        return [
            Surface(
                surface_id=f"unknown:symbol_consumers:{symbol}",
                kind=KIND_UNKNOWN_SURFACE,
                status="unknown",
                origin=f"{rel}:{symbol}",
                reason=f"reference lookup failed ({type(exc).__name__}) — consumers NOT known to be zero",
            )
        ]
    empty_reason = str(refs.get("empty_reason") or "")
    if empty_reason in _UNKNOWN_EMPTY_REASONS or refs.get("timed_out"):
        # The sweep's own #482 contract already separates "indexed with
        # zero usages" from "could not answer". This is the one place
        # that distinction has to survive into a gate, so it is read
        # rather than re-derived.
        return [
            Surface(
                surface_id=f"unknown:symbol_consumers:{symbol}",
                kind=KIND_UNKNOWN_SURFACE,
                status="unknown",
                origin=f"{rel}:{symbol}",
                reason=(
                    f"reference lookup did not answer ({empty_reason or 'timed_out'}) "
                    "— consumers NOT known to be zero"
                ),
            )
        ]
    out: list[Surface] = []
    for match in refs.get("matches") or []:
        path = str(match.get("path") or "").replace("\\", "/")
        if not path or path == rel:
            continue  # the definition site is the edit, not a consumer
        try:
            line = int(match.get("line_number") or 0)
        except (TypeError, ValueError):
            line = 0
        if path not in symbols_by_path:
            symbols_by_path[path] = _outline_symbols(project_root, path)[0]
        container = _containing_symbol(symbols_by_path[path], line)
        kind = KIND_TEST_SURFACE if _is_test_path(path) else KIND_SYMBOL_CONSUMER
        label = f"{path}:{container}" if container else path
        out.append(
            Surface(
                surface_id=f"{kind}:{label}:{symbol}",
                kind=kind,
                status="unchecked",
                origin=f"{rel}:{symbol}",
                reason=(
                    f"`{container or path}` consumes `{symbol}` at {path}:{line}"
                    if container
                    else f"{path}:{line} references `{symbol}`"
                ),
            )
        )
    return out


def surfaces_for_mutation(
    project_root: Path,
    rel_path: str,
    *,
    changed_symbols: list[str] | None = None,
    resolve_consumers: bool = False,
    symbol_severity: dict[str, str] | None = None,
) -> list[Surface]:
    """The blast envelope contribution of ONE governed mutation.

    Pure computation — no ledger writes — so a caller can preview a
    radius without opening an envelope, and so the accumulation logic
    below has exactly one thing to test.

    ``resolve_consumers`` DEFAULTS OFF, and that default is the design.
    ``find_references`` is a whole-tree text sweep; running one per
    changed symbol on the edit path would put seconds onto every single
    edit, and #446/#436 already settled that nothing re-hots that path.
    So an edit records the cheap, memoized parts (touched file, reverse
    file radius) plus a PENDING IOU naming the symbols still owed a
    sweep, and ``sweep_pending_consumers`` — called from Stop, once per
    envelope, over the accumulated set — pays it. Same functions, two
    call times: "Stop is where the bill comes due" (plan §2) and "Stop
    stays cheap and incremental" (#1046 amendment 8) are the same
    sentence read from both ends.
    """
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if not rel:
        return []
    out: list[Surface] = [
        Surface(
            surface_id=f"{KIND_TOUCHED_FILE}:{rel}",
            kind=KIND_TOUCHED_FILE,
            status="fixed",
            origin=rel,
            reason="authored in this envelope",
            evidence=[f"edit:{rel}"],
        )
    ]
    out.extend(_file_dependent_surfaces(project_root, rel))

    symbols_by_path: dict[str, list[tuple[str, int]]] = {}
    if changed_symbols is None:
        own, available = _outline_symbols(project_root, rel)
        symbols_by_path[rel] = own
        if not available:
            out.append(
                Surface(
                    surface_id=f"unknown:changed_symbols:{rel}",
                    kind=KIND_UNKNOWN_SURFACE,
                    status="unknown",
                    origin=rel,
                    reason="outline unavailable — changed symbols NOT known to be none",
                )
            )
            return out
        # No line range reached us, so every symbol the file defines is a
        # CANDIDATE changed symbol. Deliberately over-inclusive and hard
        # capped: over-inclusion costs an obligation the agent can
        # dispose of in one line, while under-inclusion is the #925j
        # defect itself. Phase 2's seam is here — pass the edit's line
        # span and this narrows to the symbols it actually crossed.
        candidates = [s for s, _ in own]
    else:
        candidates = [str(s).strip() for s in changed_symbols if str(s).strip()]
    names = candidates[:_MAX_CHANGED_SYMBOLS]
    if len(candidates) > _MAX_CHANGED_SYMBOLS:
        # RULE 1 AT THE CAP BOUNDARY, the same argument the radius walk
        # already makes at its own cap. Dropping the tail silently would
        # let a capped symbol list read as an exhaustive one — and the
        # file this feature exists to catch (claude_hooks_install.py)
        # defines far more than the cap, so on the wrong ordering the
        # incident's own symbol is the one that vanishes.
        dropped = candidates[_MAX_CHANGED_SYMBOLS:]
        out.append(
            Surface(
                surface_id=f"{KIND_UNKNOWN_SURFACE}:{rel}:changed-symbols-truncated",
                kind=KIND_UNKNOWN_SURFACE,
                status="unknown",
                origin=rel,
                reason=(
                    f"{len(candidates)} candidate changed symbols exceeded the "
                    f"cap of {_MAX_CHANGED_SYMBOLS}; {len(dropped)} were NOT "
                    f"examined (first unexamined: {dropped[0]}). Narrow this by "
                    "passing the edit's line span."
                ),
            )
        )

    for name in names:
        # #1075 step 3: severity rides the evidence (no schema change). Absent
        # when changes were not diffed — the presenter reads that as unknown.
        sev = (symbol_severity or {}).get(name)
        out.append(
            Surface(
                surface_id=f"{KIND_CHANGED_SYMBOL}:{rel}:{name}",
                kind=KIND_CHANGED_SYMBOL,
                status="fixed",
                origin=rel,
                reason=f"changed in this envelope ({sev})" if sev else "changed in this envelope",
                evidence=[f"edit:{rel}:{name}"] + ([f"severity:{sev}"] if sev else []),
            )
        )
    if not names:
        return out
    if resolve_consumers:
        for name in names:
            out.extend(
                _symbol_consumer_surfaces(project_root, rel, name, symbols_by_path)
            )
        return out
    out.append(
        Surface(
            surface_id=f"{KIND_PENDING_CONSUMERS}:{rel}",
            kind=KIND_PENDING_CONSUMERS,
            status="unchecked",
            origin=rel,
            reason=(
                "consumers of "
                + ", ".join(f"`{n}`" for n in names)
                + " not swept yet — sweep runs at Stop"
            ),
            evidence=list(names),
        )
    )
    return out


def sweep_pending_consumers(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> list[str]:
    """Pay the IOUs the edit path deferred. Returns the new surface ids.

    Runs ONCE per Stop over the whole accumulated envelope, so N edits
    to one file cost one sweep rather than N. The pending row is then
    marked ``fixed`` — not deleted: the record that the question was
    asked, and when, is part of the evidence trail.
    """
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    with _connect(project_root) as conn:
        row = _epoch_row(conn, ident)
        seq = int(row["envelope_seq"])
        epoch = int(row["epoch"])
        pending = conn.execute(
            "SELECT * FROM blast_surface WHERE envelope_key = ? AND envelope_seq = ? "
            "AND kind = ? AND status = 'unchecked'",
            (ident.envelope_key, seq, KIND_PENDING_CONSUMERS),
        ).fetchall()
        if not pending:
            return []
        symbols_by_path: dict[str, list[tuple[str, int]]] = {}
        found: list[str] = []
        for p in pending:
            rel = str(p["origin"])
            names = list(json.loads(str(p["evidence"]) or "[]"))
            surfaces: list[Surface] = []
            for name in names:
                surfaces.extend(
                    _symbol_consumer_surfaces(project_root, rel, name, symbols_by_path)
                )
            found.extend(_merge_surfaces(conn, ident, seq, epoch, surfaces))
            conn.execute(
                "UPDATE blast_surface SET status = 'fixed', novel = 0, "
                "reason = ?, updated_at = ? WHERE envelope_key = ? "
                "AND envelope_seq = ? AND surface_id = ?",
                (
                    f"consumer sweep ran for {len(names)} symbol(s)",
                    time.time(),
                    ident.envelope_key,
                    seq,
                    str(p["surface_id"]),
                ),
            )
        conn.commit()
    return found


# ── Ledger ───────────────────────────────────────────────────────────


def _epoch_row(conn: sqlite3.Connection, ident: EnvelopeIdentity) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO blast_epoch "
        "(envelope_key, project_uuid, lineage_id, lineage_resolved, epoch, envelope_seq) "
        "VALUES (?, ?, ?, ?, 0, 0)",
        (
            ident.envelope_key,
            ident.project_uuid,
            ident.lineage_id,
            int(ident.lineage_resolved),
        ),
    )
    return conn.execute(
        "SELECT * FROM blast_epoch WHERE envelope_key = ?",
        (ident.envelope_key,),
    ).fetchone()


def file_checksum(project_root: Path, rel: str) -> str:
    """Same bytes the code index hashes (read_text, universal newlines), so a
    post-edit checksum compares equal to the index's pre-edit ``checksum``."""
    try:
        text = (Path(project_root) / rel).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def indexed_checksum(project_root: Path, rel: str) -> str | None:
    """The code index's checksum for ``rel`` — read BEFORE the post-edit refresh
    it is the file's pre-edit state. ``None`` when not indexed or unreadable."""
    try:
        from .code_index_store import CodeIndexStore

        store = CodeIndexStore()
        with store.connect(project_root) as conn:
            row = conn.execute(
                "SELECT checksum FROM code_files WHERE path = ? LIMIT 1",
                (str(rel or "").replace("\\", "/").strip("/"),),
            ).fetchone()
    except Exception:
        return None
    return str(row[0]) if row and row[0] else None


def _withdraw_inverse(
    conn: sqlite3.Connection,
    ident: EnvelopeIdentity,
    seq: int,
    surface_ids: list[str],
    undone_epoch: int,
) -> list[str]:
    """Withdraw the obligations an exactly-undone edit introduced. Only rows
    still UNRESOLVED are touched: a disposition the agent already made stands."""
    now = time.time()
    out: list[str] = []
    for sid in surface_ids:
        row = conn.execute(
            "SELECT status, presented_at FROM blast_surface "
            "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
            (ident.envelope_key, seq, sid),
        ).fetchone()
        if row is None or str(row["status"]) not in UNRESOLVED_STATUSES:
            continue
        if row["presented_at"] is None:
            conn.execute(
                "DELETE FROM blast_surface WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
                (ident.envelope_key, seq, sid),
            )
        else:
            conn.execute(
                "UPDATE blast_surface SET status = 'unaffected', reason = ?, evidence = ?, "
                "novel = 0, updated_at = ? "
                "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
                (
                    "edit reverted by its exact inverse",
                    json.dumps([f"inverse:{undone_epoch}"]),
                    now,
                    ident.envelope_key,
                    seq,
                    sid,
                ),
            )
        out.append(sid)
    return out


# Undo lineage kept per envelope+path; deeper history falls off (an undo past
# it is recorded as a normal mutation — over-reports, never under-reports).
_LINEAGE_MAX_DEPTH = 32


def _reopen_settled(
    conn: sqlite3.Connection,
    ident: EnvelopeIdentity,
    seq: int,
    settled: dict[str, str],
    undone_epoch: int,
) -> list[str]:
    """Reopen obligations the undone edit settled by editing the owed file. Only
    a row still carrying THAT edit's evidence is reopened: a later, independent
    disposition stands.

    ACROSS ENVELOPES (r0b): close_envelope carries only UNRESOLVED rows, so an
    obligation settled in envelope N has no row in N+1. The newest row for the
    surface in ANY earlier seq of this envelope key is the witness; when it is
    fixed by the undone epoch and the current seq has no row, the reopened
    obligation is inserted into the current seq, deferred_from the seq it came
    from."""
    now = time.time()
    out: list[str] = []
    ev_suffix = f"@epoch{undone_epoch}"
    for sid, prior in settled.items():
        status = prior if prior in UNRESOLVED_STATUSES else "unchecked"
        reason = f"reopened: the edit that settled it (epoch {undone_epoch}) was exactly undone"
        evidence = json.dumps([f"reopened_by_inverse:{undone_epoch}"])
        row = conn.execute(
            "SELECT * FROM blast_surface WHERE envelope_key = ? AND surface_id = ? "
            "AND envelope_seq <= ? ORDER BY envelope_seq DESC LIMIT 1",
            (ident.envelope_key, sid, seq),
        ).fetchone()
        if row is None or str(row["status"]) != "fixed":
            continue
        if not any(str(e).endswith(ev_suffix) for e in json.loads(row["evidence"] or "[]")):
            continue
        if int(row["envelope_seq"]) == seq:
            conn.execute(
                "UPDATE blast_surface SET status = ?, reason = ?, evidence = ?, novel = 1, "
                "presented_at = NULL, updated_at = ? "
                "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
                (status, reason, evidence, now, ident.envelope_key, seq, sid),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO blast_surface (envelope_key, envelope_seq, "
                "surface_id, kind, origin, status, reason, evidence, novel, "
                "first_seen_epoch, last_seen_epoch, deferred_from_seq, "
                "deferred_from_epoch, merged_from, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    ident.envelope_key,
                    seq,
                    sid,
                    str(row["kind"]),
                    str(row["origin"]),
                    status,
                    reason,
                    evidence,
                    int(row["first_seen_epoch"]),
                    int(row["last_seen_epoch"]),
                    int(row["envelope_seq"]),
                    int(row["last_seen_epoch"]),
                    str(row["merged_from"]),
                    now,
                ),
            )
        out.append(sid)
    return out


def note_stop_entry(
    project_root: Path,
    *,
    stop_hook_active: bool,
    agent_id: str | None = None,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> None:
    """Called at the TOP of every Stop, before any duty can block. A fresh Stop
    (not a re-entry caused by a Stop block) clears the blast block marker, so
    only a re-entry following BLAST'S OWN block is suppressed — a re-entry after
    a higher-priority blocker still gets its blast presentation (#1075 B1)."""
    if stop_hook_active:
        return
    ident = envelope_identity(
        project_root, host_kind=host_kind, host_session_id=host_session_id, agent_id=agent_id
    )
    with _connect(project_root) as conn:
        conn.execute("DELETE FROM blast_stop_marker WHERE envelope_key = ?", (ident.envelope_key,))
        conn.commit()


def _merge_surfaces(
    conn: sqlite3.Connection,
    ident: EnvelopeIdentity,
    seq: int,
    epoch: int,
    surfaces: list[Surface],
    *,
    merged_from: str = "",
) -> list[str]:
    """Accumulate ``surfaces`` into the open envelope. Returns novel ids.

    ONE dedup/novelty rule, here and nowhere else. A surface already in
    the envelope keeps its status, its evidence and its ``novel=0``: a
    later edit re-discovering the same consumer must not un-resolve a
    disposition an agent already made, nor re-announce it as news. That
    re-announcement is exactly the repetitive prose the plan's §3 wants
    gone.
    """
    now = time.time()
    novel: list[str] = []
    for s in surfaces:
        if s.status not in ALL_STATUSES:
            # An unrecognised status is REFUSED, never coerced: a typo'd
            # status that silently became "unaffected" would satisfy the
            # gate while meaning nothing.
            raise ValueError(f"unknown blast surface status: {s.status!r}")
        existing = conn.execute(
            "SELECT status FROM blast_surface "
            "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
            (ident.envelope_key, seq, s.surface_id),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE blast_surface SET last_seen_epoch = ?, updated_at = ? "
                "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
                (epoch, now, ident.envelope_key, seq, s.surface_id),
            )
            continue
        conn.execute(
            "INSERT INTO blast_surface (envelope_key, envelope_seq, surface_id, kind, "
            "origin, status, reason, evidence, novel, first_seen_epoch, last_seen_epoch, "
            "merged_from, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                ident.envelope_key,
                seq,
                s.surface_id,
                s.kind,
                s.origin,
                s.status,
                s.reason,
                json.dumps(list(s.evidence)),
                epoch,
                epoch,
                merged_from,
                now,
            ),
        )
        novel.append(s.surface_id)
    return novel


def record_mutation(
    project_root: Path,
    rel_path: str,
    *,
    succeeded: bool = True,
    changed_symbols: list[str] | None = None,
    resolve_consumers: bool = False,
    symbol_severity: dict[str, str] | None = None,
    pre_sha: str | None = None,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, object]:
    """Record one governed code mutation against the open envelope.

    THE EPOCH ADVANCES ON SUCCESS ONLY. A refused or failed edit changed
    no code, so it creates no obligation and must not invalidate a
    receipt either — an agent that trips the gate five times has not
    thereby aged out its own evidence.

    EDIT + EXACT UNDO IS ONE MUTATION (#1075 pin c). ``pre_sha`` is the
    file's checksum before this edit (the code index row, read before the
    post-edit refresh). Governed edits of a path form a LINEAGE STACK
    (``blast_file_lineage``). An edit whose result equals the TOP entry's
    pre-state is that edit's inverse, and it POPS it:
      * obligations the undone edit introduced are withdrawn (never presented →
        deleted; presented and unresolved → ``unaffected``, evidence
        ``inverse:<epoch>``);
      * obligations the undone edit SETTLED by editing an owed file are
        REOPENED to their prior status (r0b: the only remediation evidence is
        gone), novel again so Stop presents them;
      * nothing new accrues.
    The entry below becomes the top, so A→B→C, C→B, B→A collapses both.
    A pre_sha that does not continue the stack (an edit outside the gate in
    between) resets the path's lineage; unknown pre-state never claims inverse.
    """
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    with _connect(project_root) as conn:
        row = _epoch_row(conn, ident)
        epoch = int(row["epoch"])
        seq = int(row["envelope_seq"])
        if not succeeded:
            return {
                "ok": True,
                "advanced": False,
                "epoch": epoch,
                "envelope_seq": seq,
                "envelope_key": ident.envelope_key,
                "novel": [],
            }
        epoch += 1
        conn.execute(
            "UPDATE blast_epoch SET epoch = ?, last_mutation_at = ? WHERE envelope_key = ?",
            (epoch, time.time(), ident.envelope_key),
        )
        post_sha = file_checksum(project_root, rel)
        top = conn.execute(
            "SELECT depth, pre_sha, post_sha, epoch, novel, settled FROM blast_file_lineage "
            "WHERE envelope_key = ? AND path = ? ORDER BY depth DESC LIMIT 1",
            (ident.envelope_key, rel),
        ).fetchone()
        # An inverse must CONTINUE the lineage: this edit started from exactly
        # what the top entry produced. Unknown pre-state never claims inverse —
        # an out-of-gate change in between would otherwise let a lookalike
        # "undo" withdraw real obligations.
        if (
            top is not None
            and post_sha
            and top["pre_sha"]
            and post_sha == top["pre_sha"]
            and pre_sha
            and pre_sha == top["post_sha"]
        ):
            withdrawn = _withdraw_inverse(conn, ident, seq, json.loads(top["novel"] or "[]"), int(top["epoch"]))
            reopened = _reopen_settled(conn, ident, seq, json.loads(top["settled"] or "{}"), int(top["epoch"]))
            conn.execute(
                "DELETE FROM blast_file_lineage WHERE envelope_key = ? AND path = ? AND depth = ?",
                (ident.envelope_key, rel, int(top["depth"])),
            )
            conn.commit()
            return {
                "ok": True,
                "advanced": True,
                "epoch": epoch,
                "envelope_seq": seq,
                "envelope_key": ident.envelope_key,
                "lineage_resolved": ident.lineage_resolved,
                "novel": [],
                "inverse_of_epoch": int(top["epoch"]),
                "withdrawn": withdrawn,
                "reopened": reopened,
            }
        surfaces = surfaces_for_mutation(
            project_root,
            rel_path,
            changed_symbols=changed_symbols,
            resolve_consumers=resolve_consumers,
            symbol_severity=symbol_severity,
        )
        novel = _merge_surfaces(conn, ident, seq, epoch, surfaces)
        # REOPEN INTENT, persisted in THIS transaction BEFORE the resolver below
        # can mark anything fixed (r0b 5f4049e4-35a): the still-UNRESOLVED
        # file-level obligations this edit may settle, with their prior status.
        # _reopen_settled only acts on rows that are fixed with THIS epoch's
        # evidence, so intent for a row that never got settled is inert.
        settle_intent = {
            str(r["surface_id"]): str(r["status"])
            for r in conn.execute(
                "SELECT surface_id, status FROM blast_surface WHERE envelope_key = ? "
                "AND envelope_seq = ? AND surface_id IN (?, ?) "
                f"AND status IN ({','.join('?' * len(UNRESOLVED_STATUSES))})",
                (
                    ident.envelope_key,
                    seq,
                    f"{KIND_FILE_DEPENDENT}:{rel}",
                    f"{KIND_TEST_SURFACE}:{rel}",
                    *sorted(UNRESOLVED_STATUSES),
                ),
            ).fetchall()
        }
        if post_sha:
            continues = top is not None and (not pre_sha or pre_sha == top["post_sha"])
            if top is not None and not continues:
                conn.execute(
                    "DELETE FROM blast_file_lineage WHERE envelope_key = ? AND path = ?",
                    (ident.envelope_key, rel),
                )
            prior = pre_sha or (top["post_sha"] if continues else "") or ""
            depth = int(top["depth"]) + 1 if continues else 0
            conn.execute(
                "INSERT OR REPLACE INTO blast_file_lineage "
                "(envelope_key, path, depth, pre_sha, post_sha, epoch, novel, settled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ident.envelope_key, rel, depth, prior, post_sha, epoch, json.dumps(novel), json.dumps(settle_intent)),
            )
            conn.execute(
                "DELETE FROM blast_file_lineage WHERE envelope_key = ? AND path = ? AND depth <= ?",
                (ident.envelope_key, rel, depth - _LINEAGE_MAX_DEPTH),
            )
        conn.commit()
    # ── EDITING WHAT YOU OWED SETTLES IT (#1046 amendment 5) ────────────────
    #
    # Without this, `resolve_surface` had NO production caller and no obligation
    # could ever be cleared: the ledger accumulated debt, closure carried it
    # forward forever, and the unresolved count meant nothing because it could
    # only grow. A disposition path that exists but is unreachable is the same
    # coded-but-not-wired defect this feature exists to catch.
    #
    # WHY THIS DISPOSITION IS EVIDENCE-BACKED AND NOT A RUBBER STAMP: the
    # surface being settled is a CONSUMER the radius named, and the thing
    # settling it is a GOVERNED MUTATION OF THAT EXACT FILE. "The agent changed
    # the connected file" is a fact the edit path witnessed, not a claim the
    # agent made about itself — which is the distinction between `fixed` and the
    # self-attestation #1046 refuses. It resolves the FILE-level obligation
    # only; a symbol consumer inside that file stays owed, because editing a
    # file is not evidence about every symbol in it.
    resolved = _resolve_obligations_for_edit(
        project_root,
        rel_path,
        ident=ident,
        epoch=epoch,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    # Reopen metadata was persisted as INTENT in the lineage insert above, in the
    # same transaction, BEFORE anything was fixed (r0b 5f4049e4-35a): a crash
    # between here and there leaves intent for a row that is still unresolved,
    # which _reopen_settled ignores — over-report, never under-report.
    return {
        "ok": True,
        "advanced": True,
        "epoch": epoch,
        "envelope_seq": seq,
        "envelope_key": ident.envelope_key,
        "lineage_resolved": ident.lineage_resolved,
        "novel": novel,
        "resolved_by_this_edit": resolved,
    }


def _resolve_obligations_for_edit(
    project_root: Path,
    rel_path: str,
    *,
    ident: EnvelopeIdentity,
    epoch: int,
    host_kind: str | None,
    host_session_id: str | None,
    agent_id: str | None,
) -> list[str]:
    """Settle any FILE-level obligation this edit just discharged.

    Routed through the one disposition function rather than writing status
    inline, so every resolution — automatic or explicit — passes the same
    validation and lands the same shape. A second status-writer here would be
    the twin that rots.

    Both file-shaped kinds are candidates: the radius files a dependent as a
    TEST SURFACE when its path looks like a test and a FILE DEPENDENT
    otherwise, so settling only one of them would leave half the obligations
    permanently unclearable depending on where the consumer lives.
    """
    norm = str(rel_path).replace("\\", "/")
    out: list[str] = []
    for kind in (KIND_FILE_DEPENDENT, KIND_TEST_SURFACE):
        owed = f"{kind}:{norm}"
        try:
            with _connect(project_root) as conn:
                seq = int(_epoch_row(conn, ident)["envelope_seq"])
                hit = conn.execute(
                    "SELECT surface_id FROM blast_surface WHERE envelope_key = ? "
                    "AND envelope_seq = ? AND surface_id = ? "
                    f"AND status IN ({','.join('?' * len(UNRESOLVED_STATUSES))})",
                    (ident.envelope_key, seq, owed, *sorted(UNRESOLVED_STATUSES)),
                ).fetchone()
            if hit is None:
                continue
        except Exception:  # noqa: BLE001 — a ledger read must never fail an edit
            return out
        res = resolve_surface(
            project_root,
            owed,
            status="fixed",
            reason=f"edited at epoch {epoch} — the governed mutation is the evidence",
            evidence=[f"governed_edit:{norm}@epoch{epoch}"],
            host_kind=host_kind,
            host_session_id=host_session_id,
            agent_id=agent_id,
        )
        if res.get("ok"):
            out.append(owed)
    return out


def current_epoch(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> int:
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    with _connect(project_root) as conn:
        return int(_epoch_row(conn, ident)["epoch"])


def current_envelope(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, object]:
    """The accumulated envelope, in the shape the plan's §2 asks for."""
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    with _connect(project_root) as conn:
        row = _epoch_row(conn, ident)
        seq = int(row["envelope_seq"])
        rows = conn.execute(
            "SELECT * FROM blast_surface WHERE envelope_key = ? AND envelope_seq = ? "
            "ORDER BY kind, surface_id",
            (ident.envelope_key, seq),
        ).fetchall()
        epoch = int(row["epoch"])
    buckets: dict[str, list[str]] = {
        "touched_files": [],
        "changed_symbols": [],
        "file_dependents": [],
        "symbol_consumers": [],
        "test_surfaces": [],
        "unknown_surfaces": [],
        "pending_consumer_sweeps": [],
    }
    bucket_of = {
        KIND_PENDING_CONSUMERS: "pending_consumer_sweeps",
        KIND_TOUCHED_FILE: "touched_files",
        KIND_CHANGED_SYMBOL: "changed_symbols",
        KIND_FILE_DEPENDENT: "file_dependents",
        KIND_SYMBOL_CONSUMER: "symbol_consumers",
        KIND_TEST_SURFACE: "test_surfaces",
        KIND_UNKNOWN_SURFACE: "unknown_surfaces",
    }
    novel: list[str] = []
    unresolved: list[dict[str, object]] = []
    deferred: list[str] = []
    for r in rows:
        sid = str(r["surface_id"])
        buckets[bucket_of[str(r["kind"])]].append(sid)
        if int(r["novel"]):
            novel.append(sid)
        if str(r["status"]) in UNRESOLVED_STATUSES:
            unresolved.append(
                {
                    "surface_id": sid,
                    "kind": str(r["kind"]),
                    "status": str(r["status"]),
                    "origin": str(r["origin"]),
                    "reason": str(r["reason"]),
                    "merged_from": str(r["merged_from"]),
                    # WHICH ENVELOPE THIS DEBT CAME FROM (#1046 amendment 4).
                    # The columns were written at closure and never surfaced, so
                    # a reader could not tell a CARRIED obligation from a fresh
                    # one — and "answer about the envelope it came from" is
                    # unanswerable if the provenance is invisible. `None` here
                    # means the surface originated in THIS envelope.
                    "deferred_from_seq": r["deferred_from_seq"],
                    "deferred_from_epoch": r["deferred_from_epoch"],
                }
            )
        if r["deferred_from_seq"] is not None:
            deferred.append(sid)
    return {
        "envelope_key": ident.envelope_key,
        "lineage_id": ident.lineage_id,
        "lineage_resolved": ident.lineage_resolved,
        "envelope_seq": seq,
        "epoch": epoch,
        **buckets,
        "novel_since_last_notice": novel,
        "unresolved": unresolved,
        "deferred_from_earlier_envelope": deferred,
    }


def mark_presented(
    project_root: Path,
    surface_ids: list[str],
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> int:
    """Record that these surfaces were PRESENTED to the agent.

    PRESENTED, not examined. This is the entire honest claim the system
    can make, and the reason the word is load-bearing everywhere in this
    module: clearing the surface still requires a disposition, because
    the disposition is the only part that can be checked. Presentation
    also clears ``novel``, which is what stops the same radius being
    re-announced on every subsequent edit.
    """
    if not surface_ids:
        return 0
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    now = time.time()
    with _connect(project_root) as conn:
        seq = int(_epoch_row(conn, ident)["envelope_seq"])
        n = 0
        for sid in surface_ids:
            cur = conn.execute(
                "UPDATE blast_surface SET novel = 0, presented_at = ?, updated_at = ? "
                "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
                (now, now, ident.envelope_key, seq, sid),
            )
            n += cur.rowcount or 0
        conn.commit()
    return n


def resolve_surface(
    project_root: Path,
    surface_id: str,
    *,
    status: str,
    reason: str = "",
    evidence: list[str] | None = None,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, object]:
    """Disposition one surface.

    ``blocked_unavailable`` is accepted and is NOT a closure (amendment
    1): it lands in UNRESOLVED, so the envelope stays open and the Stop
    diagnostic keeps naming it. An agent cannot repair the evidence
    service, so the turn may end — but the obligation does not.

    PHASE 2 SEAM: ``_requires_artifact_evidence`` below is where a
    high-risk surface starts demanding a file:line, a ref result, a test
    nodeid or an invariant id instead of prose (#1046 amendment 9).
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown blast surface status: {status!r}")
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    with _connect(project_root) as conn:
        seq = int(_epoch_row(conn, ident)["envelope_seq"])
        cur = conn.execute(
            "UPDATE blast_surface SET status = ?, reason = ?, evidence = ?, "
            "novel = 0, updated_at = ? "
            "WHERE envelope_key = ? AND envelope_seq = ? AND surface_id = ?",
            (
                status,
                reason,
                json.dumps(list(evidence or [])),
                time.time(),
                ident.envelope_key,
                seq,
                surface_id,
            ),
        )
        conn.commit()
    return {
        "ok": bool(cur.rowcount),
        "surface_id": surface_id,
        "status": status,
        "resolved": status in RESOLVED_STATUSES,
    }


# PHASE 2 SEAM — risk classification depth, deliberately NOT stubbed.
#
# The obvious move here was an inert `_requires_artifact_evidence()`
# returning False as a placeholder. That is a function with no
# production consumer, which is the precise shape this project's own
# structural-law gate catches (it caught `names_a_generation` the same
# way) — and a blast-radius module shipping its own coded-not-wired
# drift would be self-refuting. So the seam is a comment until it has a
# caller.
#
# When Phase 2 lands, clearing a runtime / security / authority
# obligation must require a file:line, a symbol/ref result, a test
# nodeid or an invariant id rather than prose: a query issued and
# ignored satisfies a naive evidence ledger, which is self-attestation
# with extra steps (#1046 amendment 9). The classification itself must
# DERIVE from the governed registry / gate class (amendment 7) — a
# hand-maintained list of "gates, resolvers, settings writers, dispatch
# tables" is a twin that rots within a month.


# ── Delegation (#1046 amendment 3) ───────────────────────────────────


def merge_child_envelope(
    project_root: Path,
    *,
    child_agent_id: str,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    parent_agent_id: str | None = None,
) -> dict[str, object]:
    """SubagentStop — the child's UNRESOLVED obligations land on the parent.

    A child exit that discarded these would launder them: the parent
    goes on to commit and report work whose consumers nobody opened.
    Only unresolved rows move; the child's resolved dispositions stay
    where the evidence was produced, so the audit trail is not rewritten
    by the merge. ``merged_from`` keeps the child's lineage on the row,
    because "who owes this" survives the merge too.
    """
    child = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=child_agent_id,
    )
    parent = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=parent_agent_id,
    )
    if child.envelope_key == parent.envelope_key:
        # No lineage split resolved (unattributed bucket, or a host that
        # sends no agent_id). Merging an envelope into itself is a
        # no-op, not an error — and saying so is more honest than
        # pretending a merge happened.
        return {"ok": True, "merged": [], "reason": "same_envelope"}
    moved: list[str] = []
    with _connect(project_root) as conn:
        child_row = _epoch_row(conn, child)
        parent_row = _epoch_row(conn, parent)
        child_seq = int(child_row["envelope_seq"])
        parent_seq = int(parent_row["envelope_seq"])
        parent_epoch = int(parent_row["epoch"])
        rows = conn.execute(
            "SELECT * FROM blast_surface WHERE envelope_key = ? AND envelope_seq = ? "
            f"AND status IN ({','.join('?' * len(UNRESOLVED_STATUSES))})",
            (child.envelope_key, child_seq, *sorted(UNRESOLVED_STATUSES)),
        ).fetchall()
        carried = [
            Surface(
                surface_id=str(r["surface_id"]),
                kind=str(r["kind"]),
                status=str(r["status"]),
                origin=str(r["origin"]),
                reason=str(r["reason"]),
                evidence=list(json.loads(str(r["evidence"]) or "[]")),
            )
            for r in rows
        ]
        moved = _merge_surfaces(
            conn,
            parent,
            parent_seq,
            parent_epoch,
            carried,
            merged_from=child.lineage_id,
        )
        conn.commit()
    return {
        "ok": True,
        "merged": moved,
        "carried": [s.surface_id for s in carried],
        "child_envelope_key": child.envelope_key,
        "parent_envelope_key": parent.envelope_key,
    }


# ── Stop (#1046 amendment 4 + 8) ─────────────────────────────────────


@dataclass(frozen=True)
class StopDiagnostic:
    """What a FUTURE gate would have done. Phase 1 acts on none of it."""

    epoch: int
    envelope_seq: int
    would_refuse: bool
    unresolved: tuple[dict[str, object], ...]
    text: str
    # Phase 1 constant. This field exists so the flip to enforcement is
    # ONE assignment at ONE seam rather than a new code path — and so a
    # reader can see from the dataclass that nothing here blocks yet.
    blocking: bool = False


def stop_diagnostic(
    project_root: Path,
    *,
    event_name: str = "Stop",
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
    mark: bool = True,
) -> StopDiagnostic:
    """Emit what a future gate WOULD have refused, and nothing more.

    ``mark=False`` (#1075): the observe-only audit call in the turn-end duty
    runs BEFORE ``stop_presentation``; it must not mark surfaces presented that
    the agent never saw, or the real presentation would find nothing novel.
    """
    # Pay the edit path's deferred reference sweeps FIRST — this is the
    # one place the expensive question is asked, and asking it after the
    # envelope is read would report a radius one sweep out of date.
    try:
        sweep_pending_consumers(
            project_root,
            host_kind=host_kind,
            host_session_id=host_session_id,
            agent_id=agent_id,
        )
    except Exception:
        # A sweep that crashes must not silence the diagnostic: the
        # pending IOU stays `unchecked`, so the envelope keeps owing it.
        pass
    env = current_envelope(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    unresolved = list(env["unresolved"])  # type: ignore[arg-type]
    epoch = int(env["epoch"])  # type: ignore[arg-type]
    seq = int(env["envelope_seq"])  # type: ignore[arg-type]
    if not unresolved:
        return StopDiagnostic(
            epoch=epoch,
            envelope_seq=seq,
            would_refuse=False,
            unresolved=(),
            text="",
        )
    lines = [
        f"BLAST COMPLETION (observe-only, Phase 1) — {event_name} was NOT blocked.",
        (
            f"Mutation epoch {epoch}: {len(unresolved)} materially connected "
            "surface(s) unaccounted for."
        ),
        "",
    ]
    for item in unresolved[:20]:
        # Every line names a SURFACE and a REASON, because a refusal that
        # cannot be acted on is a trap (plan §9), and the diagnostic is
        # the dry run of that refusal.
        lines.append(
            f"- [{item['status']}] {item['surface_id']} — {item['reason'] or item['kind']}"
        )
    if len(unresolved) > 20:
        lines.append(f"- … and {len(unresolved) - 20} more")
    lines += [
        "",
        "Each needs a disposition: fixed | unaffected | covered_by_invariant.",
        (
            "A surface marked `unknown` was NOT measured as zero — the lookup "
            "did not answer."
        ),
    ]
    # A surface named in the diagnostic HAS been presented. Recording it
    # here (rather than at some later "did you read it" checkpoint) is
    # the honest boundary: presentation is what the system observed.
    if mark:
        mark_presented(
            project_root,
            [str(i["surface_id"]) for i in unresolved],
            host_kind=host_kind,
            host_session_id=host_session_id,
            agent_id=agent_id,
        )
    return StopDiagnostic(
        epoch=epoch,
        envelope_seq=seq,
        would_refuse=True,
        unresolved=tuple(unresolved),
        text="\n".join(lines),
    )


_STOP_MAX_ITEMS = 8
_STOP_KIND_ORDER = {
    KIND_SYMBOL_CONSUMER: 0,
    KIND_FILE_DEPENDENT: 1,
    KIND_UNKNOWN_SURFACE: 2,
    KIND_PENDING_CONSUMERS: 3,
}


def _surface_path(item: dict[str, object]) -> str:
    sid = str(item.get("surface_id") or "")
    rest = sid.split(":", 1)[1] if ":" in sid else sid
    return rest.split(":", 1)[0]


def stop_presentation(
    project_root: Path,
    *,
    event_name: str = "Stop",
    agent_id: str | None = None,
    stop_hook_active: bool = False,
    feedback_host: str | None = None,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> dict[str, str] | None:
    """#1075 step 5 — the Stop surface of the blast envelope, or ``None``.

    Presents ONLY surfaces not yet presented (``novel``), bounded, once:
      * only on ``Stop`` (a subagent's obligations merge upward and are
        presented at the parent's Stop);
      * only when ``feedback_host`` declares Stop feedback reaches the agent —
        any other host got the inline compact note on the edit ack instead;
      * never on a re-entry caused by BLAST'S OWN block (durable marker set
        when it blocks, cleared by ``note_stop_entry`` on every fresh Stop).
        A re-entry caused by a HIGHER-priority blocker still presents: that
        blocker deferred blast, it did not dispose of it (r0b B1);
      * vendored / scratch paths are never named, only counted; every
        considered surface — named or not — is marked presented, so it never
        repeats.
    """
    if event_name != "Stop":
        return None
    from .blast_presenter import CHANNEL_STOP, channel_for_host, classify_path

    if channel_for_host(feedback_host) != CHANNEL_STOP:
        return None
    ident: dict[str, str | None] = {
        "host_kind": host_kind,
        "host_session_id": host_session_id,
        "agent_id": agent_id,
    }
    key = envelope_identity(project_root, **ident).envelope_key
    with _connect(project_root) as conn:
        marker = conn.execute(
            "SELECT 1 FROM blast_stop_marker WHERE envelope_key = ?", (key,)
        ).fetchone()
    if stop_hook_active and marker is not None:
        return None
    try:
        sweep_pending_consumers(project_root, **ident)
    except Exception:
        pass
    env = current_envelope(project_root, **ident)
    novel = set(env["novel_since_last_notice"])  # type: ignore[arg-type]
    items = [
        u
        for u in env["unresolved"]  # type: ignore[union-attr]
        if u["surface_id"] in novel and u["kind"] != KIND_TOUCHED_FILE
    ]
    if not items:
        return None
    named = [i for i in items if classify_path(_surface_path(i)) != "other"]
    other = len(items) - len(named)
    named.sort(
        key=lambda i: (
            classify_path(_surface_path(i)) != "production",
            _STOP_KIND_ORDER.get(str(i["kind"]), 9),
            str(i["surface_id"]),
        )
    )
    # Mark EVERYTHING considered, shown or not: presentation is a one-time event.
    mark_presented(project_root, [str(i["surface_id"]) for i in items], **ident)
    if not named:
        return None
    shown = named[:_STOP_MAX_ITEMS]
    lines = [f"BLAST CHECK — your edits this turn reach {len(named)} place(s) not yet checked:"]
    for it in shown:
        lines.append(f"- {it['surface_id']} — {it['reason'] or it['kind']}")
    tail = []
    if len(named) > len(shown):
        tail.append(f"{len(named) - len(shown)} more")
    if other:
        tail.append(f"{other} vendored/scratch not listed")
    if tail:
        lines.append(f"(+ {', '.join(tail)}; ai_bundle(mode='dependency', target=<file>) expands)")
    lines.append(
        "Inspect the ones your change could materially break and verify them; editing one "
        "settles it. Nothing else records a disposition yet, so an unverified one stays owed "
        "in the ledger. Shown once; it will not repeat."
    )
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blast_stop_marker (envelope_key, blocked_at) VALUES (?, ?)",
            (key, time.time()),
        )
        conn.commit()
    return {"decision": "block", "reason": "\n".join(lines)}


def close_envelope(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, object]:
    """Close the envelope; CARRY unresolved obligations into the next one.

    #1046 amendment 4 — a deferred obligation persists INDEPENDENTLY of
    envelope closure and must still answer about the envelope it came
    from. So the carried row keeps ``deferred_from_seq`` and
    ``deferred_from_epoch``: when evidence returns, re-evaluation
    projects the historical obligation into current state while
    RETAINING the original unresolved fact. Without that, envelope B
    defers with the index down, later work moves the problematic
    consumer, evidence recovers, and B silently looks clean having
    shipped the unaccounted architecture — the epoch error pointing
    backwards, certifying a state that no longer exists.
    """
    ident = envelope_identity(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        agent_id=agent_id,
    )
    now = time.time()
    carried: list[str] = []
    with _connect(project_root) as conn:
        row = _epoch_row(conn, ident)
        seq = int(row["envelope_seq"])
        epoch = int(row["epoch"])
        rows = conn.execute(
            "SELECT * FROM blast_surface WHERE envelope_key = ? AND envelope_seq = ? "
            f"AND status IN ({','.join('?' * len(UNRESOLVED_STATUSES))})",
            (ident.envelope_key, seq, *sorted(UNRESOLVED_STATUSES)),
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO blast_surface (envelope_key, envelope_seq, "
                "surface_id, kind, origin, status, reason, evidence, novel, "
                "first_seen_epoch, last_seen_epoch, deferred_from_seq, "
                "deferred_from_epoch, merged_from, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                (
                    ident.envelope_key,
                    seq + 1,
                    str(r["surface_id"]),
                    str(r["kind"]),
                    str(r["origin"]),
                    str(r["status"]),
                    str(r["reason"]),
                    str(r["evidence"]),
                    int(r["first_seen_epoch"]),
                    int(r["last_seen_epoch"]),
                    seq,
                    int(r["last_seen_epoch"]),
                    str(r["merged_from"]),
                    now,
                ),
            )
            carried.append(str(r["surface_id"]))
        conn.execute(
            "UPDATE blast_epoch SET envelope_seq = ? WHERE envelope_key = ?",
            (seq + 1, ident.envelope_key),
        )
        conn.commit()
    return {
        "ok": True,
        "closed_seq": seq,
        "closed_epoch": epoch,
        "carried_forward": carried,
    }
