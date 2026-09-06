"""D5 — the PROJECTION LEDGER for the law family (doctrine + empire skills).

Memory points already carry projection DISCIPLINE — staged body + content
checksum, receipt, read-back verify, then the ``body_home`` marker flip
(``memory_body_staging_store``). The law family carried none of it:

  * ``backfill_empire_palace`` (empire_palace.py:392) is stamp-gated and has
    ZERO production callers — once stamped it never runs again;
  * ``ingest_promoted_law`` (empire_palace.py:502) covers ADDITIONS only;
  * so an ``ai_skill`` upsert / law EDIT left its palace drawer stale
    forever: no receipt, no read-back, no drift signal.

This module closes the EDIT hole by giving the law family the same
discipline.

BOUNDARY (ratified). The doctrine-residency lane owns the canonisation
WRITE DOOR. This module owns the LEDGER and canonises nothing: it is handed
a body, and it decides only whether that body may be PROJECTED into the
palace and whether an existing projection has drifted. It deliberately does
not read the empire store — the caller that owns the source hands the body
in.

THREE INVARIANTS

1. ADMITTED, not merely scanned. Projection requires an explicit ``admit``
   from the composite ``admit_law_body`` (malicious-intent + contradiction +
   shape). Anything else — refuse, unknown, empty, None, an exception, or an
   absent admitter — REFUSES. Unknown is never clean.

   This module must never pin to ``skill_scanner.scan_skill``. The window
   between "a check exists" and "the projector enforces it" is a live second
   door, and scan_skill is doubly wrong here: it deliberately SKIPS
   ``kind in {"doctrine", "stance"}``, which is exactly this family.

2. Drift DETECTION, never reconciliation. A divergence between projection
   and source raises a SIGNAL (the PalaceStaleSignals pattern). It never
   writes toward the source tier. The TIER axis is one-directional
   (Amendment 3 / #645). The REPLICATION axis (machine empire home <->
   tenant) is bidirectional at the SAME tier — the two axes are not the same
   thing and are not conflated here.

3. Tier-pair guard. An empire-tier row NEVER folds into a kingdom-tier row,
   whatever the HLC says. ``TIER_RANK`` declares the ordering at module
   level so callers and tests consult the symbol instead of restating it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# #755/#756: the ONE canonical connect. All three sites were
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- with no pragmas.
# DURABILITY: AUDIT. The receipt IS the evidence that a law body was
# projected and verified by read-back; it is what every later drift check
# compares against, and its absence after a crash reads as 'never
# projected'. This is a cold path (law edits are rare), so keeping the
# FULL these sites already had costs nothing.
from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# ---------------------------------------------------------------------------
# Module-level declarations (never restate these values inside a function)
# ---------------------------------------------------------------------------

#: Tier ordering for the fold guard. Higher rank = higher authority. A row
#: may never fold DOWN into a lower tier, regardless of HLC ordering.
TIER_RANK: dict[str, int] = {
    "kingdom": 10,
    "empire": 20,
}

#: The ONLY verdict string that admits a body. Compared exactly, after
#: casefolding and stripping — no prefix/substring matching, so a value like
#: "ADMITTED-ish" or "admit_with_caveats" does NOT admit.
ADMIT_VERDICT = "admit"

LAW_WING = "empire_law"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def body_checksum(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def law_drawer_id(law_id: str) -> str:
    """Deterministic drawer id, matching empire_palace.law_drawer_id so the
    ledger and the existing backfill address the SAME drawer."""
    return f"empire:law:{law_id}"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionOutcome:
    ok: bool
    reason: str
    drawer_id: str = ""
    receipt_id: str = ""
    checksum: str = ""


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    law_id: str
    drawer_id: str
    checksum: str
    tier: str
    recorded_at: str


@dataclass(frozen=True)
class FoldVerdict:
    refused: bool
    reason: str


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS law_projection_receipts (
    receipt_id   TEXT PRIMARY KEY,
    law_id       TEXT NOT NULL,
    drawer_id    TEXT NOT NULL,
    checksum     TEXT NOT NULL,
    tier         TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lpr_law ON law_projection_receipts(law_id);
"""


def init_db(ledger_db: Path) -> None:
    db = Path(ledger_db)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(
        str(db), durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def last_receipt(ledger_db: Path, *, law_id: str) -> Receipt | None:
    """Most recent receipt for ``law_id``, or None when never projected."""
    init_db(ledger_db)
    with _canonical_connect(str(Path(ledger_db)), durability=_Durability.AUDIT) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM law_projection_receipts WHERE law_id = ? "
            "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
            (law_id,),
        ).fetchone()
    if row is None:
        return None
    return Receipt(
        receipt_id=str(row["receipt_id"]),
        law_id=str(row["law_id"]),
        drawer_id=str(row["drawer_id"]),
        checksum=str(row["checksum"]),
        tier=str(row["tier"]),
        recorded_at=str(row["recorded_at"]),
    )


# ---------------------------------------------------------------------------
# Admission — the composite door, resolved late and failing CLOSED
# ---------------------------------------------------------------------------


def _resolve_admitter(admit: Callable[..., Any] | None) -> Callable[..., Any] | None:
    """Return the admission callable, or None when unavailable.

    Prefers an explicitly injected one; otherwise imports the
    doctrine-residency lane's composite ``admit_law_body``. That module may
    not exist yet — an absent admitter is UNKNOWN, and callers refuse on
    unknown rather than falling back to a weaker check.
    """
    if admit is not None:
        return admit
    try:
        from .doctrine_residency import admit_law_body  # type: ignore[import-not-found]

        return admit_law_body
    except Exception:
        return None


def _is_admitted(verdict: Any) -> bool:
    """True ONLY for an exact admit verdict. Everything else — refuse,
    unknown, empty, None, or an unexpected shape — is not clean."""
    if verdict is None:
        return False
    if isinstance(verdict, bool):
        # A bare bool is an ambiguous shape for a three-valued door.
        return False
    text = str(getattr(verdict, "verdict", verdict) or "").strip().casefold()
    return text == ADMIT_VERDICT


# ---------------------------------------------------------------------------
# 1 + 2. The projector
# ---------------------------------------------------------------------------


def project_law_body(
    *,
    law_id: str,
    kind: str,
    body: str,
    tier: str,
    ledger_db: Path,
    add_drawer: Callable[..., Any],
    read_back: Callable[[str], str | None],
    admit: Callable[..., Any] | None = None,
    permission_lookup: Callable[..., Any] | None = None,
    stale_signals: Any = None,
    hub_ctx: Any = None,
) -> ProjectionOutcome:
    """Project one law/skill body into the palace under full discipline.

    Order matters — every gate runs BEFORE the body reaches the palace:

      1. permission (a local RBAC miss is UNRESOLVED, #662),
      2. admission (``admit_law_body``; unknown is never clean),
      3. write,
      4. read-back verify,
      5. receipt — earned only by a verified read-back.

    Never raises: a refusal is a returned outcome, so a failed projection
    can never take down the caller's write path.
    """
    drawer_id = law_drawer_id(law_id)
    checksum = body_checksum(body)

    # (1) #662 — local RBAC is a PROJECTION of codenexus. A miss is
    # UNRESOLVED: never silently permitted, never hard-denied. We refuse the
    # GRANT while failing open on the report (a returned outcome, not a raise).
    if permission_lookup is not None:
        try:
            decision = permission_lookup(law_id=law_id, tier=tier, kind=kind)
        except Exception:
            decision = None
        if decision is None:
            return ProjectionOutcome(
                ok=False,
                reason="permission_unresolved",
                drawer_id=drawer_id,
                checksum=checksum,
            )
        if decision is False:
            return ProjectionOutcome(
                ok=False,
                reason="permission_denied",
                drawer_id=drawer_id,
                checksum=checksum,
            )

    # (2) Admission. Absent / raising / non-admit all refuse.
    admitter = _resolve_admitter(admit)
    if admitter is None:
        return ProjectionOutcome(
            ok=False,
            reason="admitter_unavailable",
            drawer_id=drawer_id,
            checksum=checksum,
        )
    try:
        verdict = admitter(law_id=law_id, kind=kind, body=body, tier=tier)
    except Exception:
        return ProjectionOutcome(
            ok=False,
            reason="admit_raised",
            drawer_id=drawer_id,
            checksum=checksum,
        )
    if not _is_admitted(verdict):
        return ProjectionOutcome(
            ok=False,
            reason="not_admitted",
            drawer_id=drawer_id,
            checksum=checksum,
        )

    # (3) Write.
    try:
        add_drawer(
            wing=LAW_WING,
            room=(kind or "law"),
            content=body,
            unit_id=f"{tier}:{law_id}",
            drawer_id=drawer_id,
            added_by="law_projection_ledger",
            hub_ctx=hub_ctx,
        )
    except Exception:
        return ProjectionOutcome(
            ok=False,
            reason="write_failed",
            drawer_id=drawer_id,
            checksum=checksum,
        )

    # (4) Read-back verify — the receipt must be EARNED. A drawer that does
    # not read back as what we wrote is a stale projection, so signal it.
    try:
        landed = read_back(drawer_id)
    except Exception:
        landed = None
    if landed is None or body_checksum(landed) != checksum:
        _signal_stale(
            stale_signals,
            drawer_id=drawer_id,
            unit_id=f"{tier}:{law_id}",
            old_checksum="",
            new_checksum=checksum,
        )
        return ProjectionOutcome(
            ok=False,
            reason="readback_mismatch",
            drawer_id=drawer_id,
            checksum=checksum,
        )

    # (5) Receipt.
    receipt_id = uuid.uuid4().hex
    init_db(ledger_db)
    with _canonical_connect(
        str(Path(ledger_db)), durability=_Durability.AUDIT, row_factory=False
    ) as conn:
        conn.execute(
            "INSERT INTO law_projection_receipts "
            "(receipt_id, law_id, drawer_id, checksum, tier, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (receipt_id, law_id, drawer_id, checksum, tier, _now()),
        )
        conn.commit()

    # A verified re-projection clears any prior staleness for this drawer.
    marker = getattr(stale_signals, "mark_active", None)
    if callable(marker):
        try:
            marker(drawer_id=drawer_id)
        except Exception:
            pass

    return ProjectionOutcome(
        ok=True,
        reason="projected",
        drawer_id=drawer_id,
        receipt_id=receipt_id,
        checksum=checksum,
    )


def _signal_stale(
    stale_signals: Any,
    *,
    drawer_id: str,
    unit_id: str,
    old_checksum: str,
    new_checksum: str,
) -> bool:
    """Raise a staleness SIGNAL. Never writes toward the source tier."""
    marker = getattr(stale_signals, "mark_stale", None)
    if not callable(marker):
        return False
    try:
        marker(
            drawer_id=drawer_id,
            unit_id=unit_id,
            old_content_hash=old_checksum,
            new_content_hash=new_checksum,
        )
        return True
    except Exception:
        return False


def detect_law_drift(
    *,
    law_id: str,
    current_body: str,
    ledger_db: Path,
    tier: str = "empire",
    stale_signals: Any = None,
) -> dict[str, Any]:
    """Compare the CURRENT source body against the last receipted projection.

    DETECTION ONLY. This raises a signal and returns a verdict; it never
    writes toward the source tier and never re-projects. Reconciliation is a
    separate, deliberate act by whoever owns the source — never a silent
    side effect of looking.

    Returns ``{"drift": bool|None, "state": str, "signalled": bool}``.
    ``state="unresolved"`` (with ``drift=None``) when the law has no receipt:
    with nothing to compare against we cannot assert cleanliness, so we never
    report clean.
    """
    receipt = last_receipt(ledger_db, law_id=law_id)
    current = body_checksum(current_body)

    if receipt is None:
        return {"drift": None, "state": "unresolved", "signalled": False}

    if receipt.checksum == current:
        return {"drift": False, "state": "active", "signalled": False}

    signalled = _signal_stale(
        stale_signals,
        drawer_id=receipt.drawer_id,
        unit_id=f"{tier}:{law_id}",
        old_checksum=receipt.checksum,
        new_checksum=current,
    )
    return {"drift": True, "state": "stale", "signalled": signalled}


# ---------------------------------------------------------------------------
# 3. Tier-pair guard
# ---------------------------------------------------------------------------


def refuse_cross_tier_fold(
    *,
    incoming_tier: str,
    existing_tier: str,
    incoming_hlc: str = "",
    existing_hlc: str = "",
) -> FoldVerdict:
    """Refuse ANY cross-tier fold. Same-tier folds are permitted; HLC decides.

    THE ASYMMETRY — both directions refuse, for OPPOSITE reasons. Both are
    stated here, because a reader who assumes symmetry gets one of them wrong:

    * DOWNWARD (empire -> kingdom) is refused because derivation is LOSSY.
      Doctrine XXVI strips the tutoring on the way down, so reconciling
      toward the source overwrites the RICH SOURCE with its IMPOVERISHED
      DERIVATIVE. That is the Amendment 3 / #645 destruction.

    * UPWARD (kingdom -> empire) is refused because derivation is GATED.
      The only lawful kingdom->empire route is PROMOTION via
      ``memory_promote`` -> ``ingest_promoted_law`` (empire_palace.py:502):
      operator-gated, fit-checked, tutoring stripped. A clock-ordered fold is
      COPYING — precisely the shape derivation forbids — and would land a
      TUTORED row at empire tier with no gate, no fit-check and no operator
      (AUDIENCE LAW: soul-leak severity).

    WHY CLOSING UPWARD COSTS REPLICATION NOTHING (ruled 2026-07-30): same-tier
    tenant replication NEVER presents as kingdom->empire at this seam. The
    replicated pair is {this machine's empire home} + {the operator's
    codenexus account} — BOTH ENDS EMPIRE TIER. The operator ruling
    ("bidirectional is correct, at tenant-level") means same tier, same row,
    two homes. Replication is empire<->empire or kingdom<->kingdom; it never
    crosses a tier, so a cross-tier pair arriving here is never replication.

    An UNKNOWN tier is refused, never permitted: an unrecognised tier cannot
    be proven safe, and this guard fails closed like every other door here.
    """
    inc = str(incoming_tier or "").strip().casefold()
    exi = str(existing_tier or "").strip().casefold()

    inc_rank = TIER_RANK.get(inc)
    exi_rank = TIER_RANK.get(exi)
    if inc_rank is None or exi_rank is None:
        return FoldVerdict(
            refused=True,
            reason=f"unknown tier in pair ({incoming_tier!r} -> {existing_tier!r})",
        )

    if inc_rank > exi_rank:
        return FoldVerdict(
            refused=True,
            reason=(
                f"cross-tier fold refused (DOWNWARD — derivation is LOSSY): "
                f"{inc} (rank {inc_rank}) never folds into {exi} "
                f"(rank {exi_rank}); reconciling toward the source would "
                f"overwrite the rich source with its impoverished derivative. "
                f"HLC {incoming_hlc!r} vs {existing_hlc!r} does not override "
                f"the tier axis"
            ),
        )

    if inc_rank < exi_rank:
        return FoldVerdict(
            refused=True,
            reason=(
                f"cross-tier fold refused (UPWARD — derivation is GATED): "
                f"{inc} (rank {inc_rank}) never folds into {exi} "
                f"(rank {exi_rank}); the lawful route is PROMOTION via "
                f"memory_promote (operator-gated, fit-checked, tutoring "
                f"stripped) — a fold-by-clock is copying and would land a "
                f"tutored row at empire tier. HLC {incoming_hlc!r} vs "
                f"{existing_hlc!r} does not override the tier axis"
            ),
        )

    return FoldVerdict(refused=False, reason="same-tier replication; hlc decides")
