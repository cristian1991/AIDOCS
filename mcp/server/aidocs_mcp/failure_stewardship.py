"""Failure Stewardship Law — test/report honesty enforcement.

DOCTRINE (2026-05-26): every test failure has an OWNER and a DUTY.
An agent that runs the suite, observes failures, and seals work
without addressing each failure by NAME is shipping false success.
This module is the canonical ledger + lint layer that closes that loop.

Each failure carries:
  failure_signature   — sha256 of (nodeid, error_type, top_assertion_line).
                        Stable across re-runs of the SAME failure shape;
                        any change (test renamed, error class changed,
                        new assertion line) yields a new signature → new
                        duty.
  first_seen_sha      — git HEAD when the failure was first registered.
  first_seen_tree_hash — git tree hash AT first observation (so off-VCS
                        state — uncommitted edits, env shifts — is
                        captured even when HEAD didn't move).
  causal_origin       — who/what introduced it: "agent:<session>" |
                        "pre_existing_baseline" | "env:<dep>" | "unknown".
  current_duty        — session_id of the agent currently on the hook.
                        Empty string = orphaned; the seal blocker
                        REFUSES seal while any orphaned failure exists.
  proof_command       — verbatim command used to prove the claim
                        (typically the targeted pytest invocation that
                        reproduced the failure against a clean tree).
  proof_log_hash      — sha256 of the proof log; the LINT layer checks
                        excuse phrases in agent reports reference a
                        proof_log_hash that exists in the ledger.
  disposition         — see DISPOSITION_* below.

DISPOSITIONS — the only legal endpoints for a failure:
  UNTRIAGED          — newly observed, no agent has claimed it yet.
                        Blocks seal.
  AGENT_DUTY         — an agent is actively working on it. Blocks seal.
  PRESERVE_BASELINE  — proved pre-existing (signature was present
                        BEFORE this session's first commit; proof log
                        attached). Allowed to carry forward as a known
                        baseline crack — does NOT block seal.
  QUARANTINE         — temporarily skipped (env-broken / flaky), with
                        a follow-up commitment. Allowed to carry; does
                        NOT block seal. MUST have a follow-up reference.
  ESCALATE           — pushed up the chain (operator decision). Does
                        NOT block seal but raises an alert.
  FIXED              — patched in this session, verified green.
  WAIVER             — operator-issued waiver. Operator-only state.

LINT — refuses the "not my bug" report pattern unless the agent
attaches a structured proof.

  EXCUSE PHRASES (regex-detected):
    "pre-existing", "pre existing", "preexisting",
    "unrelated to this change", "not related to my change",
    "not caused by this change", "flaky", "not my bug",
    "downstream issue", "environmental", "env-only".

  ACCEPTED FORMS:
    Each phrase MUST be paired with a structured reference within 200
    chars, of the form `proof_log_hash=<64-hex>` OR `[ledger:<sig>]`
    where <sig> is a failure_signature present in the ledger with a
    non-UNTRIAGED disposition. Otherwise lint_report() rejects the
    text and surfaces the offending phrase.

The ledger is dict-backed in memory for fast, zero-coupling unit
coverage, with a project-scoped SQLite backing (`load_ledger` /
`save_ledger`) so a failure's duty survives across turns and
processes — each Stop hook runs in a fresh interpreter, so the duty
to triage cannot live only in process memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# ── Dispositions ─────────────────────────────────────────────────────

DISPOSITION_UNTRIAGED = "untriaged"
DISPOSITION_AGENT_DUTY = "agent_duty"
DISPOSITION_PRESERVE_BASELINE = "preserve_baseline"
DISPOSITION_QUARANTINE = "quarantine"
DISPOSITION_ESCALATE = "escalate"
DISPOSITION_FIXED = "fixed"
DISPOSITION_WAIVER = "waiver"

ALL_DISPOSITIONS: frozenset[str] = frozenset(
    {
        DISPOSITION_UNTRIAGED,
        DISPOSITION_AGENT_DUTY,
        DISPOSITION_PRESERVE_BASELINE,
        DISPOSITION_QUARANTINE,
        DISPOSITION_ESCALATE,
        DISPOSITION_FIXED,
        DISPOSITION_WAIVER,
    },
)

# Dispositions that BLOCK seal — the agent owes more work.
SEAL_BLOCKING: frozenset[str] = frozenset(
    {
        DISPOSITION_UNTRIAGED,
        DISPOSITION_AGENT_DUTY,
    },
)

# Dispositions that allow seal but require a follow-up commitment.
REQUIRES_FOLLOWUP: frozenset[str] = frozenset(
    {
        DISPOSITION_QUARANTINE,
        DISPOSITION_ESCALATE,
    },
)


# ── Excuse phrase lint ───────────────────────────────────────────────
#
# Phrases that, without structured evidence, function as agent excuses
# ("trust me, this isn't my problem"). Matched case-insensitively as
# whole tokens — fragments inside larger words (e.g. "unrelated" inside
# "interrelated") don't fire.
_EXCUSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpre[-\s]?existing\b", re.IGNORECASE),
    re.compile(r"\bunrelated to (?:this|my) (?:change|work|commit|pr)\b", re.IGNORECASE),
    re.compile(r"\bnot related to (?:this|my) (?:change|work|commit|pr)\b", re.IGNORECASE),
    re.compile(r"\bnot caused by (?:this|my) (?:change|work|commit|pr)\b", re.IGNORECASE),
    re.compile(r"\bflaky\b", re.IGNORECASE),
    re.compile(r"\bnot my bug\b", re.IGNORECASE),
    re.compile(r"\bdownstream issue\b", re.IGNORECASE),
    re.compile(r"\benv(?:ironmental|[-_]only)\b", re.IGNORECASE),
)

# Structured-evidence markers the lint accepts. MUST appear within
# `_PROOF_PROXIMITY_CHARS` of an excuse phrase to count.
_PROOF_HASH_PATTERN = re.compile(
    r"proof_log_hash\s*=\s*([0-9a-f]{64})",
    re.IGNORECASE,
)
_LEDGER_REF_PATTERN = re.compile(
    r"\[\s*ledger\s*:\s*([0-9a-f]{12,64})\s*\]",
    re.IGNORECASE,
)
_PROOF_PROXIMITY_CHARS = 200


# ── Signature ────────────────────────────────────────────────────────


def compute_failure_signature(
    nodeid: str,
    error_type: str,
    top_assertion_line: str,
) -> str:
    """sha256 of the failure's stable triple. Changes ↔ new duty."""
    payload = "\n".join(
        (
            (nodeid or "").strip(),
            (error_type or "").strip(),
            (top_assertion_line or "").strip(),
        ),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_log_hash(log_bytes: bytes) -> str:
    """sha256 of a proof log. Stored as `proof_log_hash`."""
    return hashlib.sha256(log_bytes or b"").hexdigest()


# ── Tree hash capture ────────────────────────────────────────────────


def capture_first_seen_tree_hash(project_root: Path) -> str:
    """`git write-tree` against the current working tree — captures
    on-disk state INCLUDING uncommitted edits. Falls back to HEAD's
    tree hash when the working tree isn't a git repo, and to empty
    string on total failure.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "write-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def capture_head_sha(project_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


# ── Ledger ───────────────────────────────────────────────────────────


@dataclass
class FailureRow:
    failure_signature: str
    nodeid: str
    error_type: str
    top_assertion_line: str
    first_seen_sha: str
    first_seen_tree_hash: str
    causal_origin: str
    current_duty: str
    proof_command: str = ""
    proof_log_hash: str = ""
    disposition: str = DISPOSITION_UNTRIAGED
    followup_ref: str = ""  # required when disposition ∈ REQUIRES_FOLLOWUP
    waiver_operator: str = ""  # required when disposition == WAIVER
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_signature": self.failure_signature,
            "nodeid": self.nodeid,
            "error_type": self.error_type,
            "top_assertion_line": self.top_assertion_line,
            "first_seen_sha": self.first_seen_sha,
            "first_seen_tree_hash": self.first_seen_tree_hash,
            "causal_origin": self.causal_origin,
            "current_duty": self.current_duty,
            "proof_command": self.proof_command,
            "proof_log_hash": self.proof_log_hash,
            "disposition": self.disposition,
            "followup_ref": self.followup_ref,
            "waiver_operator": self.waiver_operator,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> FailureRow:
        return cls(
            failure_signature=str(d.get("failure_signature", "")),
            nodeid=str(d.get("nodeid", "")),
            error_type=str(d.get("error_type", "")),
            top_assertion_line=str(d.get("top_assertion_line", "")),
            first_seen_sha=str(d.get("first_seen_sha", "")),
            first_seen_tree_hash=str(d.get("first_seen_tree_hash", "")),
            causal_origin=str(d.get("causal_origin", "")),
            current_duty=str(d.get("current_duty", "")),
            proof_command=str(d.get("proof_command", "")),
            proof_log_hash=str(d.get("proof_log_hash", "")),
            disposition=str(d.get("disposition", DISPOSITION_UNTRIAGED)),
            followup_ref=str(d.get("followup_ref", "")),
            waiver_operator=str(d.get("waiver_operator", "")),
            history=list(d.get("history", []) or []),
        )


class StewardshipError(Exception):
    """Raised when a stewardship contract is violated (bad transition,
    missing proof, orphaned duty at seal time). Distinct from a
    generic AssertionError so callers can catch + render.
    """


class FailureStewardshipLedger:
    """In-process ledger keyed by failure_signature.

    Persistent backing (SQLite) is a small follow-up; the API surface
    below is the contract every consumer should code against. The
    ledger is per-process / per-test so unit tests don't pollute one
    another (no module-level singleton).
    """

    def __init__(self) -> None:
        self._rows: dict[str, FailureRow] = {}
        # Full-suite invocation counter — the seal blocker uses it to
        # enforce "one full suite per session" (single publish event).
        self._full_suite_runs: dict[str, int] = {}

    # ── Registration ─────────────────────────────────────────────

    def register_failure(
        self,
        *,
        nodeid: str,
        error_type: str,
        top_assertion_line: str,
        observing_session_id: str,
        project_root: Path | None = None,
        causal_origin: str = "",
    ) -> FailureRow:
        """Register a freshly-observed failure. If the signature is
        already in the ledger (re-observed in the same or a later run),
        the existing row is returned UNCHANGED — re-observation does
        not reset duty or disposition.

        causal_origin defaults to `agent:<observing_session_id>` so the
        agent that first sees a failure owns it until proved otherwise.
        That's the load-bearing claim of the law: silence ≠ innocence.
        """
        sig = compute_failure_signature(nodeid, error_type, top_assertion_line)
        if sig in self._rows:
            row = self._rows[sig]
            row.history.append(
                {
                    "event": "reobserved",
                    "by_session": observing_session_id,
                },
            )
            return row
        sha = capture_head_sha(project_root) if project_root else ""
        tree = capture_first_seen_tree_hash(project_root) if project_root else ""
        row = FailureRow(
            failure_signature=sig,
            nodeid=nodeid,
            error_type=error_type,
            top_assertion_line=top_assertion_line,
            first_seen_sha=sha,
            first_seen_tree_hash=tree,
            causal_origin=causal_origin or f"agent:{observing_session_id}",
            current_duty=observing_session_id,
            disposition=DISPOSITION_UNTRIAGED,
        )
        row.history.append(
            {
                "event": "registered",
                "by_session": observing_session_id,
            },
        )
        self._rows[sig] = row
        return row

    def get(self, failure_signature: str) -> FailureRow | None:
        return self._rows.get(failure_signature)

    def all(self) -> list[FailureRow]:
        return list(self._rows.values())

    # ── Disposition transitions ─────────────────────────────────

    def claim_pre_existing(
        self,
        failure_signature: str,
        *,
        by_session: str,
        proof_command: str,
        proof_log: bytes,
        baseline_sha: str,
    ) -> FailureRow:
        """Move a failure to PRESERVE_BASELINE.

        The CLAIM is: this failure existed BEFORE the agent's first
        commit in this session. The PROOF: a recorded log (we hash it
        and store the hash) of the same test failing against
        `baseline_sha` (a SHA from BEFORE the session started). Without
        proof_log this method raises.
        """
        if not proof_log:
            raise StewardshipError(
                "claim_pre_existing requires proof_log bytes — the recorded "
                "output of the test failing against the baseline SHA",
            )
        if not baseline_sha:
            raise StewardshipError(
                "claim_pre_existing requires baseline_sha — the commit SHA "
                "from BEFORE this session that the failure was reproduced on",
            )
        row = self._rows.get(failure_signature)
        if row is None:
            raise StewardshipError(
                f"unknown failure_signature {failure_signature!r} — "
                "register the failure before claiming a disposition",
            )
        row.disposition = DISPOSITION_PRESERVE_BASELINE
        row.proof_command = proof_command
        row.proof_log_hash = compute_log_hash(proof_log)
        row.causal_origin = f"pre_existing_baseline:{baseline_sha}"
        row.current_duty = ""  # orphaned of duty (intentionally — proven baseline)
        row.history.append(
            {
                "event": "claimed_preserve_baseline",
                "by_session": by_session,
                "baseline_sha": baseline_sha,
                "proof_log_hash": row.proof_log_hash,
            },
        )
        return row

    def quarantine(
        self,
        failure_signature: str,
        *,
        by_session: str,
        followup_ref: str,
        proof_log: bytes | None = None,
        proof_command: str = "",
    ) -> FailureRow:
        """Skip the test for now, with a written follow-up commitment.

        followup_ref MUST be a non-empty pointer to where the work
        will resume (issue id, follow-up goal description, ledger
        slot). Without it this is just abandonment under a different
        name — refused.
        """
        if not followup_ref.strip():
            raise StewardshipError(
                "quarantine requires followup_ref — a non-empty pointer to "
                "where the work will resume (issue id, follow-up goal, etc.)",
            )
        row = self._rows.get(failure_signature)
        if row is None:
            raise StewardshipError(f"unknown failure_signature {failure_signature!r}")
        row.disposition = DISPOSITION_QUARANTINE
        row.followup_ref = followup_ref.strip()
        if proof_log is not None:
            row.proof_log_hash = compute_log_hash(proof_log)
            row.proof_command = proof_command
        row.current_duty = ""
        row.history.append(
            {
                "event": "quarantined",
                "by_session": by_session,
                "followup_ref": row.followup_ref,
            },
        )
        return row

    def escalate(
        self,
        failure_signature: str,
        *,
        by_session: str,
        operator_alert: str,
    ) -> FailureRow:
        """Push the failure up the chain — operator decision needed."""
        if not operator_alert.strip():
            raise StewardshipError("escalate requires operator_alert text")
        row = self._rows.get(failure_signature)
        if row is None:
            raise StewardshipError(f"unknown failure_signature {failure_signature!r}")
        row.disposition = DISPOSITION_ESCALATE
        row.followup_ref = operator_alert.strip()
        row.current_duty = ""
        row.history.append(
            {
                "event": "escalated",
                "by_session": by_session,
                "alert": row.followup_ref,
            },
        )
        return row

    def mark_fixed(
        self,
        failure_signature: str,
        *,
        by_session: str,
        proof_command: str,
        proof_log: bytes,
    ) -> FailureRow:
        """Mark a failure as fixed in THIS session. Proof required:
        the same test passing under proof_command.
        """
        if not proof_log:
            raise StewardshipError(
                "mark_fixed requires proof_log — the recorded output of the "
                "test passing under proof_command",
            )
        row = self._rows.get(failure_signature)
        if row is None:
            raise StewardshipError(f"unknown failure_signature {failure_signature!r}")
        row.disposition = DISPOSITION_FIXED
        row.proof_command = proof_command
        row.proof_log_hash = compute_log_hash(proof_log)
        row.current_duty = ""
        row.history.append(
            {
                "event": "fixed",
                "by_session": by_session,
                "proof_log_hash": row.proof_log_hash,
            },
        )
        return row

    def issue_waiver(
        self,
        failure_signature: str,
        *,
        operator: str,
        reason: str,
    ) -> FailureRow:
        """Operator-only state. The lint accepts a waiver without
        further proof because the operator IS the authority.
        """
        if not operator.strip() or not reason.strip():
            raise StewardshipError("issue_waiver requires both operator and reason")
        row = self._rows.get(failure_signature)
        if row is None:
            raise StewardshipError(f"unknown failure_signature {failure_signature!r}")
        row.disposition = DISPOSITION_WAIVER
        row.waiver_operator = operator.strip()
        row.followup_ref = reason.strip()
        row.current_duty = ""
        row.history.append(
            {
                "event": "waived",
                "operator": row.waiver_operator,
                "reason": row.followup_ref,
            },
        )
        return row

    # ── Seal blocker ─────────────────────────────────────────────

    def seal_blockers(self, session_id: str = "") -> list[FailureRow]:
        """Return all failure rows that block seal for `session_id`.

        Blocking dispositions: UNTRIAGED + AGENT_DUTY where the duty
        belongs to the sealing session (or any session if session_id
        is empty — global seal check). Other dispositions
        (PRESERVE_BASELINE, QUARANTINE, ESCALATE, FIXED, WAIVER) do NOT
        block.
        """
        out: list[FailureRow] = []
        for row in self._rows.values():
            if row.disposition not in SEAL_BLOCKING:
                continue
            if session_id and row.current_duty and row.current_duty != session_id:
                # Another agent owns it — not this seal's blocker.
                continue
            out.append(row)
        return out

    def autoclear_on_green_run(
        self,
        session_id: str = "",
        *,
        proof: str = "observed-green-pytest-run",
    ) -> list[str]:
        """A fully-green pytest run was observed this turn. Mark every
        SEAL_BLOCKING row this session owns as FIXED — the green run is the
        proof. This is the evidence-based triage that closes the common
        fix-then-rerun loop WITHOUT a manual disposition, and also reaps
        phantom rows (transcript/fixture false-positives) the next time the
        suite is actually green. Returns the cleared signatures.
        """
        cleared: list[str] = []
        for row in self._rows.values():
            if row.disposition not in SEAL_BLOCKING:
                continue
            if session_id and row.current_duty and row.current_duty != session_id:
                continue
            row.disposition = DISPOSITION_FIXED
            row.current_duty = ""
            row.history.append(
                {"event": "autocleared_green_run", "by_session": session_id, "proof": proof},
            )
            cleared.append(row.failure_signature)
        return cleared

    def reap_rerun_green(self, session_id: str, scan_text: str) -> list[str]:
        """Per-nodeid green reconciliation: clear any SEAL_BLOCKING row this
        session owns whose test was observed to FAIL then later PASS (or simply
        pass) in ``scan_text``. The re-run green IS the proof. Unlike
        ``autoclear_on_green_run``, this fires even when the scanned window ALSO
        contains the earlier red (a mixed fix-then-rerun window where the global
        green-run footer check can't fire) — that exact case otherwise leaves a
        phantom duty that blocks seal forever. Returns the cleared signatures.
        """
        cleared: list[str] = []
        for row in self.seal_blockers(session_id):
            if not nodeid_last_outcome_is_pass(scan_text, row.nodeid):
                continue
            row.disposition = DISPOSITION_FIXED
            row.current_duty = ""
            row.history.append(
                {
                    "event": "autocleared_rerun_green",
                    "by_session": session_id,
                    "proof": "observed-nodeid-rerun-green",
                },
            )
            cleared.append(row.failure_signature)
        return cleared

    def reverify_blockers_green(
        self,
        session_id: str = "",
        *,
        project_root: Path | None = None,
        run_pytest=None,
    ) -> list[str]:
        """Deterministically RE-RUN each seal-blocking failure's nodeid and mark the
        ones that now pass as FIXED.

        Closes the fix-by-revert gap that the transcript-scan auto-clears
        (autoclear_on_green_run / reap_rerun_green) MISS when the agent's pytest output
        was `-q` (dots, no per-nodeid PASS lines) or `| tail`-truncated (no green
        footer) — the exact case that wedges the seal on an already-green test. The
        re-run green IS the proof (v4 §8: no orphan, proof required), recorded in the
        row history (§40.16: every proof needs a ledger).

        Bounded: re-runs ONLY the still-blocking nodeids THIS session owns, in a single
        pytest invocation. FAIL-OPEN toward BLOCKING: any runner error clears nothing
        and is swallowed — a re-verify crash must never wedge the turn, and must never
        silently absolve a real failure (the blockers simply remain). `run_pytest(
        project_root, nodeids) -> (output_text, all_green)` is injectable for tests.
        Returns the cleared signatures.
        """
        blockers = self.seal_blockers(session_id)
        # Only re-verify SAFE pytest nodeids (`path::test`). A ledger nodeid is parsed
        # from agent-influenced transcript text, so a flag-shaped id (`--pdb`, `-p
        # evil`) must never reach the pytest argv. Unsafe ids are left BLOCKING
        # (fail-safe) — never re-run, never cleared by an all-green run they weren't in.
        verifiable = [r for r in blockers if _is_safe_pytest_nodeid(r.nodeid)]
        nodeids = [r.nodeid for r in verifiable]
        if not nodeids:
            return []
        runner = run_pytest or _default_reverify_runner
        try:
            text, all_green = runner(project_root, nodeids)
        except Exception:
            return []  # fail-open toward blocking — never wedge, never false-absolve
        cleared: list[str] = []
        for row in verifiable:
            if all_green or nodeid_last_outcome_is_pass(text, row.nodeid):
                row.disposition = DISPOSITION_FIXED
                row.current_duty = ""
                row.history.append(
                    {
                        "event": "autocleared_reverify_green",
                        "by_session": session_id,
                        "proof": "deterministic-nodeid-rerun-green",
                    },
                )
                cleared.append(row.failure_signature)
        return cleared

    def assert_seal_allowed(self, session_id: str = "") -> None:
        """Raise StewardshipError if any failure blocks seal."""
        blockers = self.seal_blockers(session_id)
        if blockers:
            ids = ", ".join(b.failure_signature[:12] for b in blockers)
            raise StewardshipError(
                f"seal refused — {len(blockers)} failure(s) still untriaged "
                f"or under agent duty: {ids}. Claim a disposition "
                f"(preserve_baseline / quarantine / escalate / fixed / "
                f"waiver) with structured proof for each.",
            )

    # ── Full-suite waste blocker ─────────────────────────────────

    def record_full_suite_run(self, session_id: str) -> int:
        """Increment + return the full-suite-run counter for
        `session_id`. Used by `assert_full_suite_allowed` to block
        repeated waste runs in the same session.
        """
        sid = session_id or "_anonymous"
        self._full_suite_runs[sid] = self._full_suite_runs.get(sid, 0) + 1
        return self._full_suite_runs[sid]

    def get_full_suite_runs(self, session_id: str) -> int:
        return self._full_suite_runs.get(session_id or "_anonymous", 0)

    def assert_full_suite_allowed(self, session_id: str) -> None:
        """Permit AT MOST ONE full-suite run per session. The doctrine:
        a full-suite invocation is a publish event; chaining a second
        one to "see what else broke" wastes the cache TTL and the
        operator's time. After the first run, targeted re-runs are
        the only allowed mode until session end.
        """
        n = self.get_full_suite_runs(session_id)
        if n >= 1:
            raise StewardshipError(
                f"full-suite run refused — session {session_id!r} has "
                f"already invoked the full suite {n} time(s). Use a "
                f"targeted re-run (pytest path::test) instead; one full "
                f"suite per session, terminal.",
            )

    # ── State (de)serialization for persistence ──────────────────

    def to_state(self) -> dict[str, object]:
        """Serialize the full ledger to a plain dict (JSON-safe)."""
        return {
            "rows": [r.to_dict() for r in self._rows.values()],
            "full_suite_runs": dict(self._full_suite_runs),
        }

    def load_state(self, state: dict[str, object]) -> None:
        """Replace in-memory state from a `to_state()` dict."""
        self._rows = {}
        for rd in state.get("rows", []) or []:
            row = FailureRow.from_dict(rd)
            if row.failure_signature:
                self._rows[row.failure_signature] = row
        self._full_suite_runs = dict(state.get("full_suite_runs", {}) or {})


# ── SQLite persistence ───────────────────────────────────────────────
#
# Project-scoped backing store. One row per failure_signature plus a
# small key/value table for the full-suite-run counters. The duty a
# failure carries must survive across turns and processes — an
# in-process dict cannot enforce "find out why before 'not my fault'"
# when each Stop hook runs in a fresh interpreter.

_LEDGER_DB_RELPATH = (".MEMORY", ".aidocs", "failure_stewardship.sqlite3")


def ledger_db_path(project_root: Path) -> Path:
    return Path(project_root).joinpath(*_LEDGER_DB_RELPATH)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS failure_rows ("
        "failure_signature TEXT PRIMARY KEY, data TEXT NOT NULL)",
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS suite_runs (session_id TEXT PRIMARY KEY, n INTEGER NOT NULL)",
    )
    return conn


def load_ledger(project_root: Path) -> FailureStewardshipLedger:
    """Hydrate a ledger from the project's SQLite store (empty if none)."""
    ledger = FailureStewardshipLedger()
    db_path = ledger_db_path(project_root)
    if not db_path.exists():
        return ledger
    try:
        conn = _connect(db_path)
        try:
            rows = [
                FailureRow.from_dict(json.loads(data))
                for (data,) in conn.execute("SELECT data FROM failure_rows")
            ]
            ledger._rows = {r.failure_signature: r for r in rows if r.failure_signature}
            ledger._full_suite_runs = {
                sid: int(n) for sid, n in conn.execute("SELECT session_id, n FROM suite_runs")
            }
        finally:
            conn.close()
    except Exception:
        # Corrupt/unreadable store must not wedge the agent — start clean.
        return FailureStewardshipLedger()
    return ledger


def save_ledger(project_root: Path, ledger: FailureStewardshipLedger) -> None:
    """Persist the ledger to the project's SQLite store (full replace)."""
    db_path = ledger_db_path(project_root)
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM failure_rows")
        conn.executemany(
            "INSERT INTO failure_rows (failure_signature, data) VALUES (?, ?)",
            [(r.failure_signature, json.dumps(r.to_dict())) for r in ledger.all()],
        )
        conn.execute("DELETE FROM suite_runs")
        conn.executemany(
            "INSERT INTO suite_runs (session_id, n) VALUES (?, ?)",
            list(ledger._full_suite_runs.items()),
        )
        conn.commit()
    finally:
        conn.close()


# ── Agent disposition surface ────────────────────────────────────────
#
# The PRODUCER half (evaluate_turn) registers failures into the
# persistent ledger; these two functions are the CONSUMER half an agent
# reaches through the `ai_failures` MCP tool. Without them the ledger
# SQLite is unreachable (it lives under a forbidden_aidocs_path) and a
# session whose only blockers are tests it already fixed could wedge
# unable to seal. Logic lives here (pure, testable) so the tool wrapper
# stays a thin shim.

# Disposition actions an AGENT may drive (waiver is operator authority,
# still reachable here when the caller supplies operator+reason).
AGENT_DISPOSITION_ACTIONS = (
    "fixed",
    "preserve_baseline",
    "quarantine",
    "escalate",
    "waiver",
    "autoclear",
)


def _row_summary(row: "FailureRow") -> dict[str, object]:
    return {
        "signature": row.failure_signature,
        "short": row.failure_signature[:12],
        "nodeid": row.nodeid,
        "error_type": row.error_type,
        "top_assertion_line": row.top_assertion_line,
        "disposition": row.disposition,
        "current_duty": row.current_duty,
        "causal_origin": row.causal_origin,
        "followup_ref": row.followup_ref,
    }


def list_session_failures(project_root: Path, session_id: str) -> dict[str, object]:
    """Return the failures this `session_id` owns (seal blockers) plus the
    full ledger, summarized for an agent to read and triage.
    """
    ledger = load_ledger(project_root)
    return {
        "ok": True,
        "session_id": session_id,
        "blockers": [_row_summary(r) for r in ledger.seal_blockers(session_id)],
        "all": [_row_summary(r) for r in ledger.all()],
    }


def _resolve_signature(ledger: "FailureStewardshipLedger", signature: str) -> str:
    """Accept a full signature or an unambiguous short prefix."""
    signature = (signature or "").strip()
    if not signature:
        raise StewardshipError("a failure signature is required for this disposition")
    if ledger.get(signature) is not None:
        return signature
    matches = [r.failure_signature for r in ledger.all() if r.failure_signature.startswith(signature)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise StewardshipError(f"unknown failure_signature {signature!r}")
    raise StewardshipError(f"ambiguous signature prefix {signature!r} matches {len(matches)} failures")


def apply_disposition(
    project_root: Path,
    session_id: str,
    *,
    action: str,
    signature: str = "",
    proof_command: str = "",
    proof_log: bytes = b"",
    baseline_sha: str = "",
    followup_ref: str = "",
    operator_alert: str = "",
    operator: str = "",
    reason: str = "",
) -> dict[str, object]:
    """Claim a disposition for a failure the agent owns and persist it.

    Session-scoped: refuses to dispose a failure currently under ANOTHER
    session's duty (waiver excepted — that is operator authority, not a
    duty transfer). Proof requirements are enforced by the underlying
    ledger transitions (e.g. `fixed` without proof_log raises).
    """
    act = (action or "").strip().lower()
    if act not in AGENT_DISPOSITION_ACTIONS:
        raise StewardshipError(
            f"unknown disposition action {action!r}; one of {list(AGENT_DISPOSITION_ACTIONS)}",
        )

    ledger = load_ledger(project_root)

    if act == "autoclear":
        cleared = ledger.autoclear_on_green_run(
            session_id,
            proof=proof_command or "agent-asserted-green-run",
        )
        save_ledger(project_root, ledger)
        return {"ok": True, "action": "autoclear", "cleared": cleared}

    sig = _resolve_signature(ledger, signature)
    row = ledger.get(sig)

    # Session scope: an agent may only dispose a failure it owns or one
    # that is unowned. Waiver is operator authority (handled separately).
    if act != "waiver" and session_id and row.current_duty and row.current_duty != session_id:
        raise StewardshipError(
            f"failure {sig[:12]} is under another session's duty "
            f"({row.current_duty!r}); cannot dispose cross-session",
        )

    if act == "fixed":
        ledger.mark_fixed(sig, by_session=session_id, proof_command=proof_command, proof_log=proof_log)
    elif act == "preserve_baseline":
        ledger.claim_pre_existing(
            sig,
            by_session=session_id,
            proof_command=proof_command,
            proof_log=proof_log,
            baseline_sha=baseline_sha,
        )
    elif act == "quarantine":
        ledger.quarantine(
            sig,
            by_session=session_id,
            followup_ref=followup_ref,
            proof_log=proof_log or None,
            proof_command=proof_command,
        )
    elif act == "escalate":
        ledger.escalate(sig, by_session=session_id, operator_alert=operator_alert or followup_ref)
    elif act == "waiver":
        ledger.issue_waiver(sig, operator=operator, reason=reason)

    save_ledger(project_root, ledger)
    return {"ok": True, "action": act, "row": ledger.get(sig).to_dict()}


# ── Stop-hook turn gate ──────────────────────────────────────────────
#
# The deterministic loop-closer. On assistant turn-end the Stop hook
# calls evaluate_turn() with (a) the text to scan for pytest failures
# (the turn's tool output / transcript) and (b) the agent's final
# report. It registers every observed failure into the PERSISTENT
# ledger (so the duty survives the next interpreter), lints the report
# for unproven excuse phrases, then computes whether the turn may seal.
#
# This is what makes "find out why before you say 'not my fault'" a
# CODE rule rather than a prompt suggestion: an agent that observes a
# failure and writes "pre-existing, not my bug" without a proof hash or
# ledger reference gets the turn BLOCKED with the offending phrase and
# the untriaged signatures quoted back.


@dataclass
class StopGateResult:
    ok: bool
    block_reason: str
    new_failures: list[str] = field(default_factory=list)  # signatures registered this turn
    seal_blockers: list[str] = field(default_factory=list)  # blocking signatures
    lint_offenses: list[str] = field(default_factory=list)  # offending matched phrases

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "block_reason": self.block_reason,
            "new_failures": list(self.new_failures),
            "seal_blockers": list(self.seal_blockers),
            "lint_offenses": list(self.lint_offenses),
        }


def evaluate_turn(
    *,
    project_root: Path,
    session_id: str,
    scan_text: str,
    report_text: str,
) -> StopGateResult:
    """Close the failure-stewardship loop for one assistant turn.

    1. Load the project's persistent ledger.
    2. Register every pytest failure found in `scan_text` (the duty is
       persisted so it outlives this process).
    3. Lint `report_text` for unproven excuse phrases.
    4. Persist the ledger.
    5. Return a StopGateResult; `ok` is False (turn should be blocked)
       when the report carries an unproven excuse phrase OR a failure
       this session owns is still untriaged / under duty.
    """
    ledger = load_ledger(project_root)

    new_sigs: list[str] = []
    for nodeid, error_type, top_line in parse_pytest_failures(scan_text):
        # Per-nodeid red→green reconciliation: a test that FAILED then was fixed
        # and re-ran green in this SAME window is not an open duty — don't register
        # a phantom for it (the common fix-then-rerun-in-one-turn case).
        if nodeid_last_outcome_is_pass(scan_text, nodeid):
            continue
        row = ledger.register_failure(
            nodeid=nodeid,
            error_type=error_type,
            top_assertion_line=top_line,
            observing_session_id=session_id,
            project_root=project_root,
        )
        if row.failure_signature not in new_sigs:
            new_sigs.append(row.failure_signature)

    # Reap any EXISTING duty whose test now re-runs green in this window — covers
    # rows registered in an earlier turn (e.g. before hooks were turned on, or
    # before the fix landed) where the window mixes the old red with the new green.
    ledger.reap_rerun_green(session_id, scan_text)

    # Auto-clear on a fully-green run: if this turn observed a completed
    # pytest run with zero failures, every outstanding duty this session
    # owns is proven resolved (the green run IS the proof). This closes the
    # fix-then-rerun loop without manual triage AND reaps phantom rows that
    # transcript/fixture text may have registered earlier.
    if pytest_run_is_green(scan_text):
        ledger.autoclear_on_green_run(session_id)

    # Deterministic re-verify (king 2026-06-20, bug #68): the scan-based auto-clears
    # above only fire when the agent's pytest output carried per-nodeid PASS lines or a
    # full green footer — `pytest -q` (dots) and `| tail` truncation defeat both, so a
    # real fix-by-revert stays a phantom and wedges the seal. Re-run the still-blocking
    # nodeids ourselves and clear the ones that now pass. Bounded (only blockers) +
    # fail-open toward blocking.
    ledger.reverify_blockers_green(session_id, project_root=project_root)

    lint = lint_report(report_text, ledger=ledger)
    blockers = ledger.seal_blockers(session_id)

    save_ledger(project_root, ledger)

    offenses = [o.matched_text for o in lint.offenses]
    blocker_sigs = [b.failure_signature for b in blockers]

    if lint.ok and not blockers:
        return StopGateResult(
            ok=True,
            block_reason="",
            new_failures=new_sigs,
            seal_blockers=[],
            lint_offenses=[],
        )

    parts: list[str] = ["FAILURE STEWARDSHIP — turn cannot seal yet."]
    if not lint.ok:
        phrases = ", ".join(sorted({repr(o) for o in offenses}))
        parts.append(
            f"Unproven excuse phrase(s) in your report: {phrases}. "
            f"Each must be backed within 200 chars by `proof_log_hash=<64hex>` "
            f"or `[ledger:<signature>]` referencing a triaged failure.",
        )
    if blockers:
        ids = ", ".join(s[:12] for s in blocker_sigs)
        parts.append(
            f"{len(blockers)} failure(s) still untriaged or under your duty: {ids}. "
            f"For EACH: find out why it fails, then claim a disposition "
            f"(fixed / preserve_baseline / quarantine / escalate / waiver) with proof. "
            f'Do not declare "not my bug" without reproducing it against a clean baseline.',
        )

    return StopGateResult(
        ok=False,
        block_reason=" ".join(parts),
        new_failures=new_sigs,
        seal_blockers=blocker_sigs,
        lint_offenses=offenses,
    )


# ── Module-level default ledger ──────────────────────────────────────
#
# Most callers reach for the singleton via get_ledger(); tests
# construct fresh FailureStewardshipLedger() instances.

_DEFAULT_LEDGER: FailureStewardshipLedger | None = None


def get_ledger() -> FailureStewardshipLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = FailureStewardshipLedger()
    return _DEFAULT_LEDGER


def reset_ledger() -> None:
    """Test-only — wipe the module-level singleton."""
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


# ── Report lint ──────────────────────────────────────────────────────


@dataclass
class LintOffense:
    phrase: str
    position: int  # char offset in the input text
    matched_text: str
    snippet: str  # +/- 80 chars context

    def to_dict(self) -> dict[str, object]:
        return {
            "phrase": self.phrase,
            "position": self.position,
            "matched_text": self.matched_text,
            "snippet": self.snippet,
        }


@dataclass
class LintResult:
    ok: bool
    offenses: list[LintOffense] = field(default_factory=list)
    accepted_phrases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "offenses": [o.to_dict() for o in self.offenses],
            "accepted_phrases": list(self.accepted_phrases),
        }


def lint_report(
    text: str,
    *,
    ledger: FailureStewardshipLedger | None = None,
) -> LintResult:
    """Scan `text` for excuse phrases and validate each.

    A phrase is ACCEPTED when, within `_PROOF_PROXIMITY_CHARS` of its
    position, the text carries either:
      - `proof_log_hash=<64-hex>` whose hash is recorded against any
        ledger row with a non-UNTRIAGED disposition; OR
      - `[ledger:<failure_signature_prefix>]` matching a ledger row
        with a non-UNTRIAGED disposition.

    All other excuse-phrase occurrences are LintOffense rows; the
    LintResult.ok flag is False iff any offense fired.
    """
    ledger = ledger or get_ledger()
    known_hashes = {
        r.proof_log_hash.lower()
        for r in ledger.all()
        if r.proof_log_hash and r.disposition != DISPOSITION_UNTRIAGED
    }
    valid_sigs = {
        r.failure_signature.lower() for r in ledger.all() if r.disposition != DISPOSITION_UNTRIAGED
    }
    offenses: list[LintOffense] = []
    accepted: list[str] = []
    if not text:
        return LintResult(ok=True)
    for pattern in _EXCUSE_PATTERNS:
        for m in pattern.finditer(text):
            window_start = max(0, m.start() - _PROOF_PROXIMITY_CHARS)
            window_end = min(len(text), m.end() + _PROOF_PROXIMITY_CHARS)
            window = text[window_start:window_end]
            accepted_here = False
            # proof_log_hash check
            for h in _PROOF_HASH_PATTERN.finditer(window):
                if h.group(1).lower() in known_hashes:
                    accepted_here = True
                    break
            if not accepted_here:
                # [ledger:<sig>] check (prefix-match against signatures)
                for lr in _LEDGER_REF_PATTERN.finditer(window):
                    needle = lr.group(1).lower()
                    if any(s.startswith(needle) for s in valid_sigs):
                        accepted_here = True
                        break
            if accepted_here:
                accepted.append(m.group(0))
                continue
            snippet_start = max(0, m.start() - 80)
            snippet_end = min(len(text), m.end() + 80)
            offenses.append(
                LintOffense(
                    phrase=pattern.pattern,
                    position=m.start(),
                    matched_text=m.group(0),
                    snippet=text[snippet_start:snippet_end],
                ),
            )
    return LintResult(ok=not offenses, offenses=offenses, accepted_phrases=accepted)


# ── Helpers for callers ──────────────────────────────────────────────


_PYTEST_PASSED_RE = re.compile(r"\b\d+\s+passed\b", re.IGNORECASE)
_PYTEST_FAILED_RE = re.compile(r"\b[1-9]\d*\s+(?:failed|errors?)\b", re.IGNORECASE)


def pytest_run_is_green(text: str) -> bool:
    """True when `text` carries the footer of a COMPLETED pytest run that
    had ZERO failures/errors (e.g. `=== 412 passed in 8.1s ===`). Used to
    auto-clear a session's outstanding duty: a fully-green run is proof
    that nothing this session owns is still failing — the green run IS the
    disposition proof, so no manual triage is needed for the common
    fix-then-rerun case. A run WITH failures (nonzero failed/error count)
    is NOT green and never auto-clears.
    """
    if not text:
        return False
    return bool(_PYTEST_PASSED_RE.search(text)) and not bool(_PYTEST_FAILED_RE.search(text))


def _last_match_index(pattern: str, text: str) -> int:
    last = -1
    for m in re.finditer(pattern, text):
        last = m.start()
    return last


def nodeid_last_outcome_is_pass(text: str, nodeid: str) -> bool:
    """True when test ``nodeid``'s LAST observed pytest outcome in ``text`` is a
    PASS — the fix-then-rerun case: it FAILED, was fixed, and re-ran green in the
    same scanned window (or it only ever passed). Exact-nodeid matching (the
    nodeid is regex-escaped) so parametrized ids with spaces/brackets are handled.

    Returns False when no PASS marker appears for the nodeid, so a still-red or
    never-rerun test is never silently cleared. A later PASS than the last FAIL
    is required, so a pass→fail regression in the same window still counts as red.
    """
    if not text or not nodeid:
        return False
    esc = re.escape(nodeid)
    last_pass = _last_match_index(esc + r"\s+PASSED\b", text)  # verbose: "<nodeid> PASSED"
    if last_pass < 0:
        return False
    last_fail = max(
        _last_match_index(esc + r"\s+FAILED\b", text),  # verbose: "<nodeid> FAILED"
        _last_match_index(r"FAILED\s+" + esc + r"(?:\s|$)", text),  # summary: "FAILED <nodeid>"
    )
    return last_pass > last_fail


def _is_safe_pytest_nodeid(nodeid: str) -> bool:
    """A ledger nodeid is safe to hand to `pytest <nodeid>` ONLY if it is a real test
    id (`path::test`), never a flag-shaped string. Nodeids are parsed from transcript
    text an agent can influence, so a `--pdb` / `-p evil` shaped 'nodeid' must never
    reach the pytest argv as a flag (king 2026-06-20, §15A hardening of bug #68).
    """
    if not nodeid or not isinstance(nodeid, str):
        return False
    n = nodeid.strip()
    return "::" in n and not n.startswith("-")


def _default_reverify_runner(
    project_root: Path | None,
    nodeids: list[str],
) -> tuple[str, bool]:
    """Re-run the given nodeids and return (combined_output, all_green). Verbose (`-v`)
    so per-nodeid PASSED/FAILED lines exist for nodeid_last_outcome_is_pass; `--tb=no`
    keeps it cheap; no cache provider so a stale cache can't taint the verdict. Bounded
    by a timeout — on timeout/crash the caller treats it as 'cleared nothing'
    (fail-open toward blocking). Only the still-blocking nodeids are passed in, so this
    is a targeted re-run, not a full-suite waste run (v4 §7).
    """
    import sys

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *nodeids,
        "-v",
        "--tb=no",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    proc = subprocess.run(  # noqa: S603 — argv form, no shell, internal nodeids only
        cmd,
        cwd=str(project_root) if project_root else None,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode == 0


def parse_pytest_failures(
    pytest_short_test_summary_text: str,
) -> Iterable[tuple[str, str, str]]:
    """Parse `=== short test summary info ===` blocks. Yields
    (nodeid, error_type, top_assertion_line) tuples.

    Robust against pytest's standard `FAILED <nodeid> - <ErrorClass>:
    <message>` shape. Returns an empty iterator on malformed input.
    """
    if not pytest_short_test_summary_text:
        return []
    out: list[tuple[str, str, str]] = []
    for raw in pytest_short_test_summary_text.splitlines():
        line = raw.strip()
        if not line.startswith("FAILED "):
            continue
        rest = line[len("FAILED ") :]
        # Require a real pytest nodeid shape (path::test). This filters
        # English prose like "the deploy FAILED - timeout" that would
        # otherwise register a phantom failure. A genuine pytest summary
        # line is always `FAILED <file>::<test>[...] - <Err>: <msg>`.
        _candidate_nodeid = rest.split(" - ", 1)[0].strip()
        if "::" not in _candidate_nodeid:
            continue
        if " - " in rest:
            nodeid, err = rest.split(" - ", 1)
            err = err.strip()
            if ":" in err:
                err_type, err_msg = err.split(":", 1)
                out.append(
                    (
                        nodeid.strip(),
                        err_type.strip(),
                        err_msg.strip(),
                    ),
                )
            else:
                out.append((nodeid.strip(), err.strip(), ""))
        else:
            out.append((rest.strip(), "Unknown", ""))
    return out
