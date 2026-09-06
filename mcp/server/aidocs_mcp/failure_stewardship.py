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
  cleared_by          — "" while open; "observation" when a LATER RUN was
                        seen green (the ledger's own evidence), "claim"
                        when an agent/operator asserted the outcome. #673:
                        an auditor must be able to tell those apart.

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
import os
import re
import sqlite3
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# ── Dispositions ─────────────────────────────────────────────────────

DISPOSITION_UNTRIAGED = "untriaged"
DISPOSITION_AGENT_DUTY = "agent_duty"
DISPOSITION_PRESERVE_BASELINE = "preserve_baseline"
DISPOSITION_QUARANTINE = "quarantine"
DISPOSITION_ESCALATE = "escalate"
DISPOSITION_FIXED = "fixed"
DISPOSITION_WAIVER = "waiver"

# ── How a row stopped blocking (#673) ────────────────────────────────
#
# `disposition == FIXED` answers "is it still open?" but NOT "on whose
# word?". Those are different facts and an auditor must be able to tell
# them apart: a row cleared because a later run was OBSERVED green is
# evidence; a row cleared because an agent ASSERTED it is a claim.
CLEARED_BY_OBSERVATION = "observation"
CLEARED_BY_CLAIM = "claim"


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


# ── Transcript scan (identity-spine rip from claude_hook, 2026-07-06) ──
# Host-agnostic assembly of the text scanned for pytest failure REGISTRATION.
# Scoped to TOOL-RESULT OUTPUTS only — the stdout/stderr of commands the agent
# ran (where a real `=== short test summary info` block lives). Deliberately
# EXCLUDES the agent's report/message (lint-only, never registration) and
# tool-call INPUTS such as file-write contents, so a `FAILED tests/x.py::t`
# literal inside a fixture being WRITTEN never registers a phantom. Source-side
# half of the 2026-05-31 phantom-registration fix.


def extract_text(obj: object) -> str:
    """Recursively collect string values from a transcript JSON node (handles
    {content:[{type:text,text:..}]} and tool_result shapes without assuming an
    exact schema)."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.append(extract_text(v))
    elif isinstance(obj, list):
        for v in obj:
            out.append(extract_text(v))
    return "\n".join(c for c in out if c)


def extract_tool_result_text(obj: object) -> str:
    """Collect text ONLY from `tool_result` content blocks (command outputs).
    Ignores assistant text and `tool_use` inputs."""
    out: list[str] = []
    if isinstance(obj, dict):
        if obj.get("type") == "tool_result":
            out.append(extract_text(obj.get("content")))
        else:
            for v in obj.values():
                out.append(extract_tool_result_text(v))
    elif isinstance(obj, list):
        for v in obj:
            out.append(extract_tool_result_text(v))
    return "\n".join(c for c in out if c)


def scan_text_from_transcript(transcript_path: str) -> str:
    """Tool-result-only scan text from a host transcript (JSONL), last 400
    lines, prefiltered to failure/pass markers. Empty string on any problem —
    the stewardship gate fails open."""
    path_str = str(transcript_path or "").strip()
    if not path_str:
        return ""
    chunks: list[str] = []
    try:
        import json as _json
        from pathlib import Path as _Path

        p = _Path(path_str)
        if p.is_file():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for raw in lines[-400:]:
                raw = raw.strip()
                if not raw or ("FAILED " not in raw and "passed" not in raw):
                    continue
                try:
                    node = _json.loads(raw)
                except Exception:
                    continue
                text = extract_tool_result_text(node)
                if text:
                    chunks.append(text)
    except Exception:
        pass
    return "\n".join(chunks)


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
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        out = audited_run(
            ["git", "-C", str(project_root), "write-tree"],
            fingerprint=("failure_stewardship.py", "capture_first_seen_tree_hash", "subprocess.run"),
            reason="failure-write-tree",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_WIN_NO_WINDOW,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        from .shell_egress_service import audited_run

        out = audited_run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD^{tree}"],
            fingerprint=("failure_stewardship.py", "capture_first_seen_tree_hash", "subprocess.run"),
            reason="failure-head-tree-fallback",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_WIN_NO_WINDOW,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def capture_head_sha(project_root: Path) -> str:
    try:
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        out = audited_run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            fingerprint=("failure_stewardship.py", "capture_head_sha", "subprocess.run"),
            reason="failure-head-sha",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_WIN_NO_WINDOW,
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
    # THE PARTITION KEY (operator ruling 2026-08-29: "every agent has its own
    # ledger, no cross-corruption"). The stable id of the agent that REGISTERED
    # this row, and it never moves again.
    #
    # NOT the same axis as `current_duty`, and the two must never be collapsed:
    # every disposition sets `current_duty = ""` (mark_fixed, quarantine,
    # escalate, issue_waiver, claim_pre_existing, both autoclears), so duty is a
    # moving target. A partition keyed on it would relocate a row every time the
    # row was triaged, which is the one moment its storage must hold still.
    #
    # Empty means UNOWNED — a legacy row that owed nobody, or an intake with no
    # resolvable identity. Unowned rows block EVERY seal (see `seal_blockers`),
    # so an empty partition is the strictest state, never the loosest.
    owner_key: str = ""
    proof_command: str = ""
    proof_log_hash: str = ""
    disposition: str = DISPOSITION_UNTRIAGED
    followup_ref: str = ""  # required when disposition ∈ REQUIRES_FOLLOWUP
    waiver_operator: str = ""  # required when disposition == WAIVER
    # #673: CLEARED_BY_OBSERVATION (a later run was seen green) vs
    # CLEARED_BY_CLAIM (an agent/operator asserted it). Empty while open.
    cleared_by: str = ""
    # WHY THE LAST RE-VERIFY DID NOT CLEAR THIS ROW (2026-08-29). Empty means
    # "it ran and the test was still red" — the only case the ledger could
    # previously express. Operator, measured: "when a test fails when ran
    # locally and does not re-green until next stop hook it should scream.
    # what's going on?"
    #
    # It was not screaming because THREE different outcomes rendered as one
    # silent unchanged row: the re-verify could not RUN (runner raised, caller
    # swallowed), the nodeid could not COLLECT (renamed/removed test — zero
    # items, read as red forever), or the test genuinely still fails. Only the
    # third is a duty the agent can discharge; the first two are the ledger
    # failing to observe, wearing the same face as a real failure.
    reverify_note: str = ""
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
            "owner_key": self.owner_key,
            "proof_command": self.proof_command,
            "proof_log_hash": self.proof_log_hash,
            "disposition": self.disposition,
            "followup_ref": self.followup_ref,
            "waiver_operator": self.waiver_operator,
            "cleared_by": self.cleared_by,
            "reverify_note": self.reverify_note,
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
            # ADOPTION, NOT ORPHANING. A ledger already in the field has rows
            # written before the partition existed. Falling back to the duty
            # holder files each one under the agent that owed it at the moment
            # the column arrived — the only attribution the row itself carries.
            # A legacy row that owed nobody lands unowned, where it keeps
            # blocking every seal. No migration pass, no backfill script, and no
            # row is dropped for lacking a key it could not have had.
            owner_key=str(d.get("owner_key") or d.get("current_duty") or ""),
            proof_command=str(d.get("proof_command", "")),
            proof_log_hash=str(d.get("proof_log_hash", "")),
            disposition=str(d.get("disposition", DISPOSITION_UNTRIAGED)),
            followup_ref=str(d.get("followup_ref", "")),
            waiver_operator=str(d.get("waiver_operator", "")),
            cleared_by=str(d.get("cleared_by", "")),
            reverify_note=str(d.get("reverify_note", "")),
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
            # The partition is stamped ONCE, here, and never again. Duty moves
            # (every disposition releases it); the row's home does not.
            owner_key=observing_session_id,
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
        row.cleared_by = CLEARED_BY_CLAIM
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

    def blockers_under_other_duty(self, session_id: str) -> list[FailureRow]:
        """SEAL_BLOCKING rows owned by a DIFFERENT session (#673).

        `seal_blockers` deliberately skips these — another agent owes them, so
        they are not this seal's problem. But skipping them silently is what
        turned `ai_failures(mode='list')` into a bare `blockers: []` that is
        indistinguishable from a clean ledger while the Stop hook was refusing
        to seal. The rows must be VISIBLE on the read path even where they are
        not ACTIONABLE, together with the identity that can act on them.
        """
        return [
            row
            for row in self._rows.values()
            if row.disposition in SEAL_BLOCKING and row.current_duty and row.current_duty != session_id
        ]

    def autoclear_on_green_run(
        self,
        session_id: str = "",
        *,
        proof: str = "observed-green-pytest-run",
        cleared_by: str = CLEARED_BY_OBSERVATION,
    ) -> list[str]:
        """A fully-green pytest run was observed this turn. Mark every
        SEAL_BLOCKING row this session owns as FIXED — the green run is the
        proof. This is the evidence-based triage that closes the common
        fix-then-rerun loop WITHOUT a manual disposition, and also reaps
        phantom rows (transcript/fixture false-positives) the next time the
        suite is actually green. Returns the cleared signatures.

        `cleared_by` (#673) records WHOSE WORD closed the row. The Stop hook
        parsed a real green footer out of real pytest output — that is an
        OBSERVATION. An agent reaching for `ai_failures(mode='autoclear')` is
        merely asserting one, so `apply_disposition` passes CLEARED_BY_CLAIM.
        Same transition, materially different evidence; an auditor gets to see
        which.
        """
        cleared: list[str] = []
        for row in self._rows.values():
            if row.disposition not in SEAL_BLOCKING:
                continue
            if session_id and row.current_duty and row.current_duty != session_id:
                continue
            row.disposition = DISPOSITION_FIXED
            row.current_duty = ""
            row.cleared_by = cleared_by
            row.history.append(
                {
                    "event": "autocleared_green_run",
                    "by_session": session_id,
                    "proof": proof,
                    "cleared_by": cleared_by,
                },
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
            row.cleared_by = CLEARED_BY_OBSERVATION
            row.history.append(
                {
                    "event": "autocleared_rerun_green",
                    "by_session": session_id,
                    "proof": "observed-nodeid-rerun-green",
                    "cleared_by": CLEARED_BY_OBSERVATION,
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
        except Exception as exc:
            # FAIL-OPEN TOWARD BLOCKING, BUT NOT IN SILENCE (2026-08-29).
            # The swallow itself is correct and stays — a re-verify crash must
            # never wedge the turn and must never false-absolve a real failure.
            # What was wrong is that it threw away the RUNNER'S OWN MESSAGE and
            # left the row byte-identical to "the test is still red".
            #
            # `_default_reverify_runner` was deliberately taught to RAISE with an
            # actionable reason, and this repo has a test named
            # test_a_missing_project_interpreter_is_raised_not_swallowed whose
            # docstring says the point out loud: "CANNOT VERIFY IS NOT
            # VERIFIED-RED ... 'the tests still fail' and 'I could not run the
            # tests' are different facts with different remedies, and today they
            # are the same empty list." That raise was defeated ONE FRAME UP, by
            # this handler. The pin held; nothing checked its caller.
            self._note_reverify(
                verifiable,
                session_id,
                "could_not_run",
                f"re-verify could not run: {type(exc).__name__}: {str(exc)[:300]}",
            )
            return []

        # ONE DEAD NODEID MUST NOT HOLD THE REST HOSTAGE (2026-08-29).
        #
        # MEASURED, on this repo's own ledger. Six blocking rows; classified
        # individually, FIVE WERE ALREADY GREEN and one named a test that had
        # been renamed. All six re-ran in ONE pytest invocation, pytest aborted
        # on the first `ERROR: not found`, the run collected 0 items, and with
        # no PASS line for anybody NOTHING cleared. Five green tests stayed
        # sealed behind one stale name, for hours, and every re-run reproduced
        # it identically — the batch is deterministic, so the wedge is permanent.
        #
        # The single batched call is the right FAST path and stays. This is the
        # fallback for the one shape that cannot be read: a run that observed
        # nothing at all. Re-running the ids separately turns "I learned nothing
        # about six tests" into six independent answers, and it costs a spawn
        # per id ONLY in the pathological case.
        #
        # Still no false absolution: each retry is the same deterministic
        # nodeid re-run, judged by the same green rule. An id that stays
        # uncollectable simply yields no PASS and is annotated below.
        if _run_collected_nothing(text) and len(nodeids) > 1:
            for row in verifiable:
                try:
                    row_text, row_green = runner(project_root, [row.nodeid])
                except Exception:  # noqa: BLE001 — one bad id must not stop the rest
                    continue
                if row_green or nodeid_last_outcome_is_pass(row_text, row.nodeid):
                    # Fold the individual verdict back into the batch text the
                    # clear loop reads, so there is ONE place that decides what
                    # "cleared" means rather than two that can drift.
                    text += f"\n{row.nodeid} PASSED\n"

        cleared: list[str] = []
        for row in verifiable:
            if all_green or nodeid_last_outcome_is_pass(text, row.nodeid):
                row.disposition = DISPOSITION_FIXED
                row.current_duty = ""
                row.cleared_by = CLEARED_BY_OBSERVATION
                row.history.append(
                    {
                        "event": "autocleared_reverify_green",
                        "by_session": session_id,
                        "proof": "deterministic-nodeid-rerun-green",
                        "cleared_by": CLEARED_BY_OBSERVATION,
                    },
                )
                cleared.append(row.failure_signature)

        # ZERO COLLECTED IS NOT RED (2026-08-29). Measured on this ledger: a row
        # naming `test_connect_refuses_rather_than_binding_to_nobody` re-ran as
        #
        #     8 workers [0 items]        no tests ran in 6.48s
        #
        # and pytest, asked for that id directly, answers `ERROR: not found`.
        # The test had been RENAMED earlier in the session. all_green was False
        # and no PASS line existed, so the row read as "still failing" — and it
        # will read that way forever, because a nodeid that cannot be collected
        # can never re-run green. A PERMANENT, UNFALSIFIABLE DUTY.
        #
        # #775 already named this exact shape ("a truncated id can never be
        # collected, so it can never re-run green, so the duty is permanent and
        # unfalsifiable") and guards it AT INTAKE. A rename AFTER intake reopens
        # the identical hole from the other side of time, and intake cannot see
        # into the future.
        #
        # This does NOT clear the row — an uncollectable test is not a passing
        # test, and auto-clearing on "I couldn't find it" is exactly the
        # false-absolution the whole ledger exists to prevent. It makes the row
        # SAY SO, so the agent disposes it deliberately (quarantine/fixed with
        # proof) instead of staring at a blocker that no run can ever move.
        #
        # Bounded on purpose: the collectability probe spawns pytest, so it runs
        # ONLY when the whole re-run collected nothing — the pathological case,
        # which is precisely when the answer is worth paying for.
        still_blocking = [r for r in verifiable if r.failure_signature not in cleared]
        if still_blocking and _run_collected_nothing(text):
            for row in still_blocking:
                if not _nodeid_is_collectable(row.nodeid, project_root):
                    self._note_reverify(
                        [row],
                        session_id,
                        "uncollectable",
                        f"`{row.nodeid}` no longer collects — the test was renamed, "
                        "moved or deleted. It can never re-run green, so it can "
                        "never auto-clear: dispose it explicitly (fixed / "
                        "quarantine / preserve_baseline).",
                    )
        return cleared

    def _note_reverify(
        self,
        rows: list[FailureRow],
        session_id: str,
        kind: str,
        note: str,
    ) -> None:
        """Record WHY a re-verify left these rows blocking, on the rows themselves.

        The ledger could previously express exactly one reason for an unchanged
        row — "the test is still red" — so a re-verify that COULD NOT RUN and a
        re-verify that ran and saw red were indistinguishable to every consumer.
        The note never changes a disposition and never clears anything; it is
        the observation, not the verdict.
        """
        for row in rows:
            row.reverify_note = note
            row.history.append(
                {
                    "event": f"reverify_{kind}",
                    "by_session": session_id,
                    "detail": note,
                },
            )

    def assert_seal_allowed(self, session_id: str = "") -> None:
        """Raise StewardshipError if any failure blocks seal."""
        blockers = self.seal_blockers(session_id)
        if blockers:
            ids = ", ".join(b.failure_signature[:12] for b in blockers)
            # THE REASON TRAVELS WITH THE REFUSAL. Without it the operator reads
            # "N failures still untriaged" and reasonably assumes N red tests,
            # when some of them may be rows the steward could not verify at all.
            notes = [f"{b.failure_signature[:12]}: {b.reverify_note}" for b in blockers if b.reverify_note]
            tail = ("\n  " + "\n  ".join(notes)) if notes else ""
            raise StewardshipError(
                f"seal refused — {len(blockers)} failure(s) still untriaged "
                f"or under agent duty: {ids}. Claim a disposition "
                f"(preserve_baseline / quarantine / escalate / fixed / "
                f"waiver) with structured proof for each." + tail,
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


# ── SQLite persistence ───────────────────────────────────────────────
#
# Project-scoped backing store. One row per failure_signature plus a
# small key/value table for the full-suite-run counters. The duty a
# failure carries must survive across turns and processes — an
# in-process dict cannot enforce "find out why before 'not my fault'"
# when each Stop hook runs in a fresh interpreter.

_LEDGER_DB_RELPATH = (".MEMORY", ".aidocs", "failure_stewardship.sqlite3")

#: How long a second writer waits for the ledger's write lock. See `_connect`.
_LEDGER_BUSY_TIMEOUT_MS = 30_000


def ledger_db_path(project_root: Path) -> Path:
    return Path(project_root).joinpath(*_LEDGER_DB_RELPATH)


def _connect(db_path: Path) -> sqlite3.Connection:
    # #746: this ran on sqlite's DEFAULT rollback journal, where a writer takes
    # an EXCLUSIVE lock over the whole file and any concurrent READER gets
    # SQLITE_BUSY -- "database is locked". The Stop hook WRITES this ledger while
    # ai_failures READS it, so the two block each other by construction. Routed
    # through the ONE canonical connect (#755, empire-doctrine XXII) rather than
    # re-deciding the pragmas here: it establishes journal_mode=WAL (persisted on
    # the FILE, so once per file per process), synchronous, busy_timeout and
    # foreign_keys=ON. mkdir stays HERE because the helper deliberately never
    # creates a directory -- opening a store is not an adoption.
    #
    # BUSY TIMEOUT, RAISED ABOVE THE 2s DEFAULT ON PURPOSE. `evaluate_turn` now
    # holds the write transaction across `reverify_blockers_green`, which SPAWNS
    # PYTEST — seconds, not milliseconds, and by design (the re-verify is what
    # turns a stale blocker back into an observation). At 2s a legitimate
    # `ai_failures` disposition landing during a Stop hook would be refused with
    # "database is locked" often enough to look like a broken tool. The wait
    # stays BOUNDED — an unbounded one hangs the agent — and a timeout still
    # fails toward BLOCKING: the disposition does not land, so nothing is
    # cleared.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _canonical_connect(
        db_path,
        durability=_Durability.RUNTIME,
        timeout=_LEDGER_BUSY_TIMEOUT_MS / 1000.0,
        busy_timeout_ms=_LEDGER_BUSY_TIMEOUT_MS,
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS failure_rows ("
        "failure_signature TEXT PRIMARY KEY, data TEXT NOT NULL, "
        "owner_key TEXT NOT NULL DEFAULT '')",
    )
    # THE PARTITION COLUMN ON A STORE THAT ALREADY EXISTS. Every live ledger was
    # created with the two-column shape, and CREATE TABLE IF NOT EXISTS is a
    # no-op against it — so the column has to be added, not declared. Cheap
    # (SQLite ADD COLUMN is O(1) metadata), idempotent via the pragma check, and
    # NOT wrapped in a blanket try: a partition column that silently failed to
    # appear would leave every write filed under nobody.
    _columns = {row[1] for row in conn.execute("PRAGMA table_info(failure_rows)")}
    if "owner_key" not in _columns:
        conn.execute(
            "ALTER TABLE failure_rows ADD COLUMN owner_key TEXT NOT NULL DEFAULT ''",
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS suite_runs (session_id TEXT PRIMARY KEY, n INTEGER NOT NULL)",
    )
    return conn


def _hydrate(
    conn: sqlite3.Connection,
) -> tuple[FailureStewardshipLedger, dict[str, str]]:
    """Build a ledger from rows on an ALREADY-OPEN connection.

    Returns `(ledger, snapshot)`. The SNAPSHOT is the stored JSON text of every
    row exactly as it was read, and it is what makes the write partitioned:
    `_persist` diffs against it and touches ONLY what actually changed. Without
    it a writer cannot tell "I changed this row" from "I merely hold a copy of
    it", and rewriting the second kind is precisely how one agent's persist
    erased another's.

    Split out so `load_ledger` (own connection) and `mutate_ledger` (inside the
    write transaction) read through ONE implementation — the two must not drift,
    because a mutator that hydrates differently from a reader is exactly how a
    row becomes visible to one and not the other.
    """
    ledger = FailureStewardshipLedger()
    snapshot: dict[str, str] = {}
    rows: list[FailureRow] = []
    for signature, data in conn.execute(
        "SELECT failure_signature, data FROM failure_rows",
    ):
        rows.append(FailureRow.from_dict(json.loads(data)))
        snapshot[str(signature)] = str(data)
    ledger._rows = {r.failure_signature: r for r in rows if r.failure_signature}
    ledger._full_suite_runs = {
        sid: int(n) for sid, n in conn.execute("SELECT session_id, n FROM suite_runs")
    }
    return ledger, snapshot


def _persist(
    conn: sqlite3.Connection,
    ledger: FailureStewardshipLedger,
    *,
    snapshot: dict[str, str] | None,
) -> None:
    """Write the ledger's rows on an ALREADY-OPEN connection. Caller commits.

    PARTITIONED BY CONSTRUCTION (operator ruling 2026-08-29). With a `snapshot`
    this writes a DELTA: a row is UPSERTed only when its serialized form differs
    from what was read, and a row is DELETEd only when it was read and is now
    gone. A row this writer never changed is never issued a statement at all, so
    a concurrent writer's row cannot be erased or reverted by it — LOCK OR NO
    LOCK. That is the difference the operator asked for: isolation by
    construction rather than by serialization.

    The lock (`mutate_ledger`'s BEGIN IMMEDIATE) is still held and still earns
    its keep — it serializes two writers contending for the SAME row, which the
    delta cannot decide. The two are layers, not alternatives.

    `snapshot=None` restores the historical FULL REPLACE, and exists for exactly
    one caller: `save_ledger`, which seeds a store from a ledger built in memory
    and therefore genuinely owns the whole file. Never pass None from a path that
    READ the store first — that is the lost update.
    """
    payloads = {r.failure_signature: json.dumps(r.to_dict()) for r in ledger.all()}
    if snapshot is None:
        conn.execute("DELETE FROM failure_rows")
        conn.executemany(
            "INSERT INTO failure_rows (failure_signature, data, owner_key) "
            "VALUES (?, ?, ?)",
            [
                (r.failure_signature, payloads[r.failure_signature], r.owner_key)
                for r in ledger.all()
            ],
        )
    else:
        for row in ledger.all():
            sig = row.failure_signature
            if snapshot.get(sig) == payloads[sig]:
                continue  # unchanged — not this writer's row to rewrite
            conn.execute(
                "INSERT INTO failure_rows (failure_signature, data, owner_key) "
                "VALUES (?, ?, ?) ON CONFLICT(failure_signature) DO UPDATE SET "
                "data = excluded.data, owner_key = excluded.owner_key",
                (sig, payloads[sig], row.owner_key),
            )
        for sig in snapshot:
            if sig not in payloads:
                conn.execute(
                    "DELETE FROM failure_rows WHERE failure_signature = ?",
                    (sig,),
                )

    # suite_runs is already keyed per session, so a full replace here CAN drop a
    # concurrent session's counter. Upsert-only: the counter is monotonic
    # per-session bookkeeping, and no caller removes another session's row.
    for session_id, n in ledger._full_suite_runs.items():
        conn.execute(
            "INSERT INTO suite_runs (session_id, n) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET n = excluded.n",
            (session_id, int(n)),
        )


def load_ledger(project_root: Path) -> FailureStewardshipLedger:
    """Hydrate a ledger from the project's SQLite store (empty if none).

    READ-ONLY USE ONLY. Pairing this with `save_ledger` to change something is
    the lost-update race documented on `mutate_ledger`; use that instead.
    """
    ledger = FailureStewardshipLedger()
    db_path = ledger_db_path(project_root)
    if not db_path.exists():
        return ledger
    try:
        conn = _connect(db_path)
        try:
            ledger, _snapshot = _hydrate(conn)
        finally:
            conn.close()
    except Exception:
        # Corrupt/unreadable store must not wedge the agent — start clean.
        return FailureStewardshipLedger()
    return ledger


@contextmanager
def mutate_ledger(project_root: Path) -> Iterator[FailureStewardshipLedger]:
    """Load, mutate and persist the ledger under ONE write lock.

    LOST UPDATES, MEASURED 2026-08-29. `save_ledger` is a FULL REPLACE
    (`DELETE FROM failure_rows` then re-insert every row it happens to hold), so
    load-mutate-save from two processes is a classic read-modify-write race: the
    second writer's snapshot — taken BEFORE the first writer's change — erases it
    wholesale, silently, with no error and no trace.

    It is not theoretical. A `quarantine` disposition applied through
    `ai_failures` vanished so completely that even its `quarantined` HISTORY
    EVENT was gone; the row read `untriaged` again, and the next `autoclear`
    then cleared it legitimately, on a green run it had never been part of.
    A disposition became a false absolution by DATA LOSS.

    The ledger has genuine concurrent writers by design: the Stop hook, the
    PostToolUse intake and this tool surface all write it, and a box running
    background agents has several of those live at once. #746 put the store on
    WAL so a reader and a writer stop blocking each other — which fixed the
    "database is locked" symptom and made this race MORE reachable, because both
    writers now proceed instead of one failing loudly.

    BEGIN IMMEDIATE takes the write lock BEFORE the read, so a second mutator
    waits on the connection's busy_timeout instead of racing. Load and save then
    happen on that one connection inside that one transaction, so what is written
    is always derived from what was read. An exception rolls the whole thing back
    rather than committing a half-applied ledger.
    """
    db_path = ledger_db_path(project_root)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ledger, snapshot = _hydrate(conn)
        yield ledger
        _persist(conn, ledger, snapshot=snapshot)
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def save_ledger(project_root: Path, ledger: FailureStewardshipLedger) -> None:
    """Persist the ledger to the project's SQLite store (full replace).

    RACY BY CONSTRUCTION when paired with a separate `load_ledger` — see
    `mutate_ledger`, which is the safe primitive for read-modify-write and what
    every production write path uses. This stays for tests that build a ledger
    in memory and seed a store with it (no read to lose), and for callers that
    genuinely own the whole file.
    """
    db_path = ledger_db_path(project_root)
    conn = _connect(db_path)
    try:
        _persist(conn, ledger, snapshot=None)
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


def compose_failure_duty_id(
    *,
    project_root: Path | str,
    host_session_id: str,
    agent_id: str = "",
    host_kind: str = "claude_code",
) -> str:
    """THE stable id this ledger keys duty and partitions on — ONE derivation.

    OPERATOR RULING 2026-08-29: "aidocs tools and functionality should key on the
    stable id derivated from conversation+the rest or agent_id+the rest. so every
    agent has its own ledger, no cross-corruption."

    WHICH ID, AND WHY THIS ONE. Nothing new is invented here. The ledger has
    ALWAYS keyed `current_duty` on the HOST SESSION id — the Stop hook stamps
    `payload["session_id"]`, and `_resolve_failure_duty_id` was written precisely
    because the tool surface had been defaulting to the managed-mode session NAME
    instead, which made an agent unable to see or dispose its own failures. That
    axis stays. The subagent link is the EXISTING `derive_agent_context_id`, the
    same derivation already used for strike/freeze/todo scope, chosen over a
    hand-rolled composition because it hashes the parent id FIRST: a host
    reporting its session id as "parent:subagent:v1:evil" cannot collide with the
    genuine pair, and host_session_id is host-supplied in exactly the threat model
    this id defends.

    NO agent_id ⇒ THE HOST SESSION ID, BYTE FOR BYTE. This is the whole reason
    the composition is shaped this way rather than hashing unconditionally. Every
    row already in a live ledger carries a raw host-session duty string; returning
    anything else for the main thread would orphan all of them at once —
    `seal_blockers` would stop matching, `blocked_elsewhere` would fill with rows
    nobody could reach, and the cross-session refusal would name identities that
    no longer exist. A blank/whitespace agent_id is treated as absent for the same
    reason: some hosts send "" for the main thread, and that must land on the same
    identity as sending nothing or the conductor forks in two.

    WHY THE SUBAGENT LINK IS NEEDED AT ALL (measured, Claude Code 2.1.239): a
    subagent's hook payload carries its PARENT's `session_id` and its parent's
    `transcript_path`; only `agent_id` differs. Keyed on the session alone, N
    concurrent subagents collapse into ONE duty holder — each one able to see the
    others' failures as its own, and `autoclear_on_green_run` (which names no
    signature) sweeping all of them on a green run they were never part of. That
    is the cross-corruption, and it is an ABSOLUTION, which is the direction that
    must never get easier.

    FAIL CLOSED: no host session ⇒ "". An agent_id does not resurrect a missing
    conversation, and a row filed under "" is addressable by every actor at once.
    """
    session = str(host_session_id or "").strip()
    agent = str(agent_id or "").strip()
    if not session:
        return ""
    if not agent:
        return session
    from .agent_memory_epoch import derive_agent_context_id

    composed = derive_agent_context_id(
        host_kind=str(host_kind or "").strip(),
        project_root=project_root,
        host_session_id=session,
        agent_id=agent,
    )
    # derive_agent_context_id returns "" for a missing host_kind (a missing link
    # its caller must refuse, never a fabricated "unknown" bucket). Here the
    # conversation IS known, so falling back to it is the honest narrower-than-
    # ideal answer rather than no identity at all — and it is exactly the
    # identity this ledger used before the subagent link existed.
    return composed or session


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
        "cleared_by": row.cleared_by,
        # Present only when the last re-verify could not turn this row into an
        # observation. Absent means the ordinary case: it ran, the test was red.
        # Emitted conditionally so an ordinary listing does not grow a column of
        # empty strings that readers learn to ignore.
        **({"reverify_note": row.reverify_note} if row.reverify_note else {}),
    }


def _foreign_duty_next_step(row: FailureRow, session_id: str) -> str:
    """The REACHABLE action for a row this session cannot dispose (law 311bf3e6).

    Never "claim a disposition" — `apply_disposition` would refuse it, which is
    exactly the dead end #673 measured. Name the identity that CAN act and the
    operator path that is not session-scoped.
    """
    return (
        f"not disposable from session {session_id!r} — duty is {row.current_duty!r}. "
        f"Inspect it with ai_failures(mode='list', session_id={row.current_duty!r}); "
        f"if you are acting for that session, pass the same session_id to the "
        f"disposition call. Otherwise this needs the OPERATOR: "
        f"ai_failures(mode='waiver', signature={row.failure_signature[:12]!r}, "
        f"operator=..., reason=...) — waiver is operator authority and is NOT "
        f"session-scoped."
    )


def list_session_failures(
    project_root: Path, session_id: str, *, include_all: bool = False
) -> dict[str, object]:
    """Return the failures this `session_id` owes an answer for, summarized
    for an agent to read and triage.

    `blocked_elsewhere` (#673) is the honesty fix: still-blocking rows owned by
    ANOTHER duty, each with the reachable next step for whoever can act. Without
    it, an agent whose session owns nothing read `blockers: []` — identical to a
    clean ledger — while the turn stayed unsealable, and had no way to learn the
    identity the disposition surface actually scopes on.

    `include_all` (#852): the full ledger is OPT-IN. The Stop hook's demand —
    "for EACH blocker, claim a disposition" — is satisfied entirely by
    `blockers` + `blocked_elsewhere`; shipping every row by default cost
    ~246KB per call (measured three times in one session: the useful payload
    was 6, 5 and 2 rows) and blew the tool-result token cap on the critical
    path of every turn that touches a test. When False the `all` key is
    ABSENT, not empty: an absent key says only "you did not ask", while an
    empty list falsely says "the ledger holds nothing".
    """
    ledger = load_ledger(project_root)
    payload: dict[str, object] = {
        "ok": True,
        "session_id": session_id,
        "blockers": [_row_summary(r) for r in ledger.seal_blockers(session_id)],
        "blocked_elsewhere": [
            {**_row_summary(r), "next_step": _foreign_duty_next_step(r, session_id)}
            for r in ledger.blockers_under_other_duty(session_id)
        ],
    }
    if include_all:
        payload["all"] = [_row_summary(r) for r in ledger.all()]
    return payload


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

    # ONE WRITE LOCK AROUND THE WHOLE READ-MUTATE-WRITE. This was
    # `load_ledger(...)` ... `save_ledger(...)`, which loses a concurrent
    # writer's row wholesale — measured, and it silently converted a quarantine
    # into a false absolution. See `mutate_ledger`.
    with mutate_ledger(project_root) as ledger:
        if act == "autoclear":
            cleared = ledger.autoclear_on_green_run(
                session_id,
                proof=proof_command or "agent-asserted-green-run",
                # An agent asserting a green run is a CLAIM, not an observation (#673).
                cleared_by=CLEARED_BY_CLAIM,
            )
            return {"ok": True, "action": "autoclear", "cleared": cleared}

        return _apply_one_disposition(
            ledger,
            session_id,
            act=act,
            signature=signature,
            proof_command=proof_command,
            proof_log=proof_log,
            baseline_sha=baseline_sha,
            followup_ref=followup_ref,
            operator_alert=operator_alert,
            operator=operator,
            reason=reason,
        )


def _apply_one_disposition(
    ledger: FailureStewardshipLedger,
    session_id: str,
    *,
    act: str,
    signature: str = "",
    proof_command: str = "",
    proof_log: bytes = b"",
    baseline_sha: str = "",
    followup_ref: str = "",
    operator_alert: str = "",
    operator: str = "",
    reason: str = "",
) -> dict[str, object]:
    """Apply ONE disposition to an already-locked ledger. Caller commits.

    Split from `apply_disposition` only so the transaction boundary is a single
    `with` at the top rather than a save call per branch — a shape where one new
    early-return silently skips the persist.
    """
    sig = _resolve_signature(ledger, signature)
    row = ledger.get(sig)

    # Session scope: an agent may only dispose a failure it owns or one
    # that is unowned. Waiver is operator authority (handled separately).
    if act != "waiver" and session_id and row.current_duty and row.current_duty != session_id:
        raise StewardshipError(
            f"failure {sig[:12]} is under another session's duty "
            f"({row.current_duty!r}); cannot dispose cross-session. "
            f"If you are acting for that session, pass "
            f"session_id={row.current_duty!r}.",
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

    # No save here: `apply_disposition` holds the write transaction and commits
    # on exit. A save on this path would be a SECOND, unlocked write.
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

    ONE WRITE LOCK AROUND THE WHOLE READ-MUTATE-WRITE. This was
    `load_ledger(...)` ... `save_ledger(...)`, and it is the HIGHEST-concurrency
    writer of the lot — every Stop hook of every agent on the box. The full
    replace at the end erased whatever a concurrent writer had committed since
    the load, silently, which is how a `quarantine` became a false absolution.
    See `mutate_ledger`.

    The body is a single `with` delegating to ONE helper rather than a save per
    branch: a shape where one new early-return silently skips the persist is how
    this class of bug is re-introduced, and this function has five exits.
    """
    with mutate_ledger(project_root) as ledger:
        return _evaluate_turn_locked(
            ledger,
            project_root=project_root,
            session_id=session_id,
            scan_text=scan_text,
            report_text=report_text,
        )


def _evaluate_turn_locked(
    ledger: FailureStewardshipLedger,
    *,
    project_root: Path,
    session_id: str,
    scan_text: str,
    report_text: str,
) -> StopGateResult:
    """One turn's intake + triage against an ALREADY-LOCKED ledger.

    Caller (`evaluate_turn`) holds the write transaction and commits on exit —
    including on every `return` below. Nothing here persists.

    NOTE ON HOLD TIME: `reverify_blockers_green` spawns pytest, so the write lock
    is held for as long as that takes. That is deliberate — the re-verify's
    verdict and the intake it is judging must land in the same transaction, or a
    row can be cleared against a ledger it was never read from — and the ledger's
    busy_timeout is raised to match (see `_connect`). A writer that still times
    out fails toward BLOCKING: its disposition does not land, so nothing is
    cleared.
    """
    new_sigs: list[str] = []
    for nodeid, error_type, top_line in parse_pytest_failures(scan_text):
        # Per-nodeid red→green reconciliation: a test that FAILED then was fixed
        # and re-ran green in this SAME window is not an open duty — don't register
        # a phantom for it (the common fix-then-rerun-in-one-turn case).
        if nodeid_last_outcome_is_pass(scan_text, nodeid):
            continue
        # #775: refuse a nodeid intake can never re-verify. Only probe BRAND
        # NEW signatures — a reobservation of an already-registered row is
        # cheap bookkeeping, not a fresh intake decision, so it skips the
        # subprocess spawn. A confirmed-uncollectable id (e.g. a truncated
        # `...::test_e` from console scraping) is skipped rather than
        # persisted; the probe fails OPEN on anything ambiguous so a real
        # failure is never silently dropped.
        #
        # #743: a pure COLLECTION error (whole file failed to import) has
        # NO `::` — it is a file path, not a pytest nodeid, by design (see
        # `parse_pytest_failures`). The #775 probe answers "is this a real,
        # collectable `path::test` item?", which is simply the wrong
        # question for a file-level signature — `_is_safe_pytest_nodeid`
        # would call it unsafe (no `::`) and the probe would reject it,
        # silently dropping exactly the destructive case #743 exists to
        # catch. So the probe is skipped entirely for this shape; the
        # ERROR line itself, straight from pytest's own short summary, is
        # already the proof the file failed to collect.
        _sig = compute_failure_signature(nodeid, error_type, top_line)
        _is_file_level_signature = "::" not in nodeid
        if (
            not _is_file_level_signature
            and ledger.get(_sig) is None
            and not _nodeid_is_collectable(nodeid, project_root)
        ):
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

    # Deterministic re-verify (Empire 2026-06-20, bug #68): the scan-based auto-clears
    # above only fire when the agent's pytest output carried per-nodeid PASS lines or a
    # full green footer — `pytest -q` (dots) and `| tail` truncation defeat both, so a
    # real fix-by-revert stays a phantom and wedges the seal. Re-run the still-blocking
    # nodeids ourselves and clear the ones that now pass. Bounded (only blockers) +
    # fail-open toward blocking.
    ledger.reverify_blockers_green(session_id, project_root=project_root)

    lint = lint_report(report_text, ledger=ledger)
    blockers = ledger.seal_blockers(session_id)

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
        # #673: say WHICH duty each row is under, and never bill a row to an
        # identity that cannot act on it. "under your duty" for an UNOWNED row
        # was simply false, and the message named no identity at all — so an
        # agent whose ai_failures call resolved a different session_id got
        # `blockers: []` and no way to discover why.
        mine = [b for b in blockers if b.current_duty == session_id and session_id]
        unowned = [b for b in blockers if not b.current_duty]
        if mine:
            parts.append(
                f"{len(mine)} failure(s) under YOUR duty (session_id={session_id!r}): "
                f"{', '.join(b.failure_signature[:12] for b in mine)}. "
                f"For EACH: find out why it fails, then claim a disposition "
                f"(fixed / preserve_baseline / quarantine / escalate / waiver) with proof "
                f"via ai_failures(..., session_id={session_id!r}). "
                f'Do not declare "not my bug" without reproducing it against a clean baseline.',
            )
        if unowned:
            parts.append(
                f"{len(unowned)} failure(s) UNOWNED (no duty holder) and untriaged: "
                f"{', '.join(b.failure_signature[:12] for b in unowned)}. "
                f"An unowned failure blocks every seal until someone takes it. "
                f"Claim one with ai_failures(mode='fixed'|'preserve_baseline'|'quarantine'|"
                f"'escalate', signature=..., session_id={session_id!r}) and proof.",
            )
        # Anonymous caller (session_id == "") — the global seal check. Rows with a
        # duty holder land here; name the holder rather than implying they are the
        # caller's, since the caller has no identity to dispose them with.
        claimed = [b for b in blockers if b.current_duty and b.current_duty != session_id]
        if claimed:
            parts.append(
                f"{len(claimed)} failure(s) open under ANOTHER duty — NOT yours to dispose: "
                + "; ".join(
                    f"{r.failure_signature[:12]} -> {_foreign_duty_next_step(r, session_id)}"
                    for r in claimed
                ),
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


_COLLECTED_NOTHING = re.compile(
    r"\bno tests ran\b|\[0 items\]|\bcollected 0 items\b|\bERROR: not found:",
    re.IGNORECASE,
)


def _run_collected_nothing(text: str) -> bool:
    """True when a re-verify run collected NO tests at all.

    Distinct from "the tests ran and failed", and the distinction is the whole
    point: a run that collected nothing produces no PASS line, so every row in
    it stays blocking exactly as if it had been observed red — while nothing was
    observed at all. Measured on this ledger (2026-08-29): `8 workers [0 items]
    / no tests ran in 6.48s` for four rows whose tests had been renamed.

    Matched on pytest's own phrasing rather than the exit code, because the
    runner returns text plus an all-green flag and both are already derived from
    that same output; adding a third derivation would be a fourth thing to keep
    in agreement.
    """
    return bool(text) and bool(_COLLECTED_NOTHING.search(text))


def _is_safe_pytest_nodeid(nodeid: str) -> bool:
    """A ledger nodeid is safe to hand to `pytest <nodeid>` ONLY if it is a real test
    id (`path::test`), never a flag-shaped string. Nodeids are parsed from transcript
    text an agent can influence, so a `--pdb` / `-p evil` shaped 'nodeid' must never
    reach the pytest argv as a flag (Empire 2026-06-20, §15A hardening of bug #68).
    """
    if not nodeid or not isinstance(nodeid, str):
        return False
    n = nodeid.strip()
    return "::" in n and not n.startswith("-")


def _pytest_run_dir(project_root: Path) -> Path:
    """Where `pytest <nodeid>` must be spawned for the ledger's nodeids to resolve.

    #673 root cause. Ledger nodeids are parsed out of the agent's pytest output and
    are therefore relative to the directory pytest RAN IN — on this repo that is
    `mcp/`, which owns `[tool.pytest.ini_options]`. The re-verify runner spawned at
    the repo ROOT instead, so every re-run exited "file or directory not found":
    non-zero rc, no per-nodeid PASSED line, `all_green` False. The one mechanism
    built to notice a red row had gone green could therefore NEVER clear anything,
    and a fixed-green-shipped test stayed "untriaged" forever.

    Mirrors server_run_tools' test-runner cwd rule (nested `mcp/pyproject.toml`
    wins, else the root) so both runners agree on one answer to one question.
    """
    root = Path(project_root)
    return root / "mcp" if (root / "mcp" / "pyproject.toml").is_file() else root


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
    # ── #673'S TWIN, ONE AXIS OVER ───────────────────────────────────
    # #673 fixed WHERE this spawns (see `_pytest_run_dir`) and left WHICH PYTHON
    # on `sys.executable`. But this runs inside the Stop hook, whose interpreter
    # is the OWNED RUNTIME venv — measured 2026-08-23:
    #
    #     ~/.aidocs/runtime/venv/Scripts/python.exe -c "import pytest"
    #     -> ModuleNotFoundError: No module named 'pytest'
    #
    # So the re-run could never start: non-zero rc, no PASSED lines, and the
    # caller reads that as "cleared nothing". Combined with `-q` output (dots,
    # no per-nodeid PASS) and piped output (no green footer), ALL THREE clearing
    # paths were dead at once — red registered from the transcript scan and green
    # never cleared, which is precisely how an operator sees a steward that only
    # notices failures.
    #
    # `test_runner._project_interpreter` already answers this exact question and
    # warns about this exact trap ("never the ambient/gate sys.executable ... the
    # minimal gate venv with no pytest"). Reused rather than re-derived, because
    # `_pytest_run_dir` set the rule that both runners must "agree on one answer
    # to one question" — and that has to cover the interpreter too, not just cwd.
    from .test_runner import _project_interpreter

    run_dir = _pytest_run_dir(project_root) if project_root else None
    # DECLARED INTERPRETER, SAME AS ai_test (2026-08-29). `_project_interpreter`
    # now returns (path, tried) and takes an optional declaration, because
    # DISCOVERY CANNOT SUCCEED ON A CLONE — `.venv` is untracked, so a synced
    # checkout structurally cannot carry one. This runner has the identical
    # problem for the identical reason, and `_pytest_run_dir`'s rule says both
    # runners must "agree on one answer to one question": if ai_test honours the
    # declaration and re-verify does not, the steward reports green blockers as
    # still failing on exactly the boxes ai_test now works on.
    tried: list[str] = []
    interp = None
    if run_dir:
        declared = ""
        if project_root:
            try:
                from .config import get_setting as _get_setting

                declared = str(
                    _get_setting("test.interpreter", project_root=project_root, default="") or ""
                ).strip()
            except Exception:
                declared = ""
        interp, tried = _project_interpreter(run_dir, declared)
    if interp is None:
        # REFUSE ACTIONABLY, never substitute a python that cannot run the tests.
        # The caller is fail-open toward BLOCKING, so raising clears nothing —
        # exactly as before — but now for a stated reason instead of silently.
        raise RuntimeError(
            "failure re-verify cannot run: no project test interpreter found. "
            "Tried, in order: "
            + ("; ".join(tried) if tried else "(no run dir resolved)")
            + ". The hook's own interpreter carries no pytest, so substituting "
            "it would report every already-green blocker as still failing. On a "
            "git clone there is no .venv to find (it is untracked) — set "
            "`test.interpreter` to a python that already has this project's "
            "test deps."
        )

    cmd = [
        interp,
        "-m",
        "pytest",
        *nodeids,
        "-v",
        "--tb=no",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
    from .shell_egress_service import audited_run

    proc = audited_run(
        cmd,
        fingerprint=("failure_stewardship.py", "_default_reverify_runner", "subprocess.run"),
        reason="failure-reverify-rerun",
        run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603 — argv form, no shell, internal nodeids only
        cwd=str(run_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",  # #684: else the ANSI codepage mojibakes UTF-8 output
        errors="replace",
        timeout=600,
        creationflags=_WIN_NO_WINDOW,
    )
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode == 0


def _this_process_has_pytest() -> bool:
    """Can THIS interpreter import pytest? Checked, never assumed.

    Cheap (`find_spec`, no subprocess) and honest: the whole family of bugs
    this guards against comes from assuming an interpreter can run pytest
    because it is the one we happen to be inside.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("pytest") is not None
    except Exception:  # noqa: BLE001 — a broken import system is not a yes
        return False


def _resolve_probe_interpreter(run_dir: Path) -> str | None:
    """An interpreter that can actually RUN pytest for *run_dir*, or None.

    Order, and each step is a MEASUREMENT rather than a guess:
      1. the project's own venv (`test_runner._project_interpreter`) — it
         carries the project's deps, and is the same answer `ai_test` resolves,
         so the two runners agree on one answer to one question (`_pytest_run_dir`'s rule);
      2. this process, ONLY IF it can import pytest — which is what makes a
         consumer repo with no venv of its own still probeable, and what keeps
         this repo's own tmp-project fixtures working;
      3. None — there is no interpreter that can answer, and the caller must
         say so rather than spawn one that cannot.
    """
    import sys

    from .test_runner import _project_interpreter

    interp, _tried = _project_interpreter(run_dir)
    if interp:
        return interp
    return sys.executable if _this_process_has_pytest() else None


def _nodeid_is_collectable(nodeid: str, project_root: Path | None) -> bool:
    """Probe whether `nodeid` names a real, collectable pytest item (#775).

    Console-scraped intake (`parse_pytest_failures`) only checks that "::"
    appears in the text — a display-layer truncation (a harness's printed
    `FAILED <nodeid>` line cut mid-name) satisfies that shape check while
    naming no collectable test. Once such an id is persisted, the ledger's
    OWN auto-clear (re-run the nodeid, see it go green) can never fire: a
    truncated id can never be collected, so it can never re-run green, so
    the duty is permanent and unfalsifiable. This is the intake-side twin
    of `_default_reverify_runner` — same probe, run once, BEFORE the id
    ever becomes a row instead of after it is stuck as one.

    Only a CONFIRMED pytest collection failure (`--collect-only` explicitly
    reporting "not found") rejects the nodeid. Everything ambiguous — a
    flag-shaped string, no project_root to probe in, a probe crash/timeout,
    or any other non-zero exit — fails OPEN (treated as collectable) so a
    genuine failure is never silently dropped at intake: a dropped real
    failure is worse than a noisy ledger row (#775 hard constraint).
    """
    if not _is_safe_pytest_nodeid(nodeid):
        return False
    if project_root is None:
        return True  # no cwd to probe in — fail open, never drop on ambiguity

    # ── THE PROBE MUST RUN ON AN INTERPRETER THAT HAS PYTEST ───────────────
    # This used to be `sys.executable`, and the docstring above already calls
    # this "the intake-side twin of `_default_reverify_runner`" — which had the
    # identical defect (fixed 12627e0c0). The caller is the STOP HOOK, running
    # under the owned runtime venv, and measured on this box that interpreter
    # has NO pytest. Seen live in the process-audit ledger: this exact probe
    # spawning `pythonw.exe -m pytest --collect-only` and exiting 1, repeatedly.
    #
    # The damage is subtle because this function fails OPEN by design: a probe
    # that cannot start returns non-zero, lands on "ambiguous", and yields True.
    # So the guard did not misfire — it went INERT, returning collectable for
    # everything, and #775's whole protection quietly stopped existing. The
    # tests in this repo never caught it because under pytest `sys.executable`
    # IS a venv with pytest: the test environment satisfies the precondition
    # that production violates.
    run_dir = _pytest_run_dir(project_root)
    interp = _resolve_probe_interpreter(run_dir)
    if interp is None:
        # Fail OPEN — #775's hard constraint ("a dropped real failure is worse
        # than a noisy ledger row"), the OPPOSITE direction from the reverify
        # twin, which refuses. But do not SPAWN an interpreter already known to
        # be unable to run pytest and then read its failure as evidence: that
        # is how a dead probe wears the costume of a working one.
        return True

    cmd = [
        interp,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        nodeid,
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
    from .shell_egress_service import audited_run

    try:
        proc = audited_run(
            cmd,
            fingerprint=("failure_stewardship.py", "_nodeid_is_collectable", "subprocess.run"),
            reason="failure-intake-collectability-probe",
            run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603 — argv form, no shell, internal nodeid only
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",  # #684: else the ANSI codepage mojibakes UTF-8 output
            errors="replace",
            timeout=60,
            creationflags=_WIN_NO_WINDOW,
        )
    except Exception:
        return True  # probe itself failed — fail open, never drop on ambiguity
    if proc.returncode == 0:
        return True
    output_lower = ((proc.stdout or "") + (proc.stderr or "")).lower()
    if proc.returncode == 4 and "file or directory not found" in output_lower:
        # The FILE itself couldn't be located — that's a cwd/environment
        # mismatch (#673's own root cause), not proof the nodeid is
        # malformed. Fail open: don't punish a real failure for a probe
        # run in the wrong directory.
        return True
    if proc.returncode == 4 and "not found" in output_lower and "no match in any of" in output_lower:
        # Confirmed: pytest OPENED the file (it exists, it collects) but
        # this exact test name is not in it — exactly the truncated-nodeid
        # shape (`...::test_e`) from console scraping (#775).
        return False
    return True  # any other nonzero exit is ambiguous — fail open


_PYTEST_BARE_FILE_PATH_RE = re.compile(r"^[\w./\\-]+\.py$")


def _looks_like_pytest_file_path(candidate: str) -> bool:
    """True when `candidate` is shaped like a pytest-reported source file
    path (no spaces, ends in `.py`) rather than English prose. Used only
    for the `::`-less ERROR shape — a pure collection error names just
    the file, so there is no `::` to anchor on the way FAILED/per-test
    ERROR lines can."""
    return bool(_PYTEST_BARE_FILE_PATH_RE.match(candidate))


def parse_pytest_failures(
    pytest_short_test_summary_text: str,
) -> Iterable[tuple[str, str, str]]:
    """Parse `=== short test summary info ===` blocks. Yields
    (nodeid, error_type, top_assertion_line) tuples.

    Robust against pytest's standard `FAILED <nodeid> - <ErrorClass>:
    <message>` shape, AND against both `ERROR` shapes (#743): a
    per-test setup/fixture error (`ERROR <file>::<test> - <Err>: <msg>`,
    has `::`, same handling as FAILED) and a pure COLLECTION error
    (`ERROR <file> - <Err>: <msg>`, NO `::` — pytest names only the file
    because no test was ever collected). The collection-error shape is
    the destructive one: a whole file's tests vanish at once. It is
    yielded with the bare file path as the "nodeid" — deliberately NOT a
    collectable pytest nodeid, so callers must not hand it to anything
    that expects `path::test` (see `evaluate_turn`'s intake, which skips
    the #775 collectability probe for this shape rather than letting the
    probe reject it as unsafe).

    Returns an empty iterator on malformed input.
    """
    if not pytest_short_test_summary_text:
        return []
    out: list[tuple[str, str, str]] = []
    for raw in pytest_short_test_summary_text.splitlines():
        line = raw.strip()
        if line.startswith("FAILED "):
            rest = line[len("FAILED ") :]
        elif line.startswith("ERROR "):
            rest = line[len("ERROR ") :]
        else:
            continue
        # Require a real pytest nodeid/file shape. This filters English
        # prose like "the deploy FAILED - timeout" or "the deploy ERROR -
        # timeout" that would otherwise register a phantom failure. A
        # genuine pytest summary line is always `FAILED <file>::<test>
        # [...] - <Err>: <msg>` or `ERROR <file>[::<test>] - <Err>: <msg>`
        # — the `::`-less ERROR shape is a pure collection error (file
        # failed to import), so it's accepted when it looks like a bare
        # source file path instead of requiring `::`.
        _candidate_nodeid = rest.split(" - ", 1)[0].strip()
        if "::" not in _candidate_nodeid and not _looks_like_pytest_file_path(_candidate_nodeid):
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
