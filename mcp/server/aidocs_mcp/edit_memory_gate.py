"""Edit-gate memory surfacing.

Before any edit tool mutates a file, this gate queries memory_symbol_anchors
for memories anchored to the target symbol OR file. If anchored memory
exists and the caller has not acked all the route_ids, the edit is
refused with the memory titles and the ack_memory_ids list to retry with.

Doctrine moves out of free-text code comments into anchored memory
entries (memory_capture with anchor_symbols=[{symbol, file, kind}]).
Anchored memories surface on edit — not on read, not on prompt — which
is the deterministic moment an agent is about to change behavior.

Doctrine cannot be silently ignored: the agent must include the route_id
of every anchored memory in the ack list before the edit proceeds.
That is the receipt that the doctrine was acknowledged.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

#: #910 -- the palace axis failing to run is an ERROR-level event on this
#: logger. It used to be an INFO line in another module, worded identically to
#: the benign no-palace case, which is why nobody ever saw it.
logger = logging.getLogger(__name__)
from typing import Any

# #755/#756: the ONE canonical connect. The single site opened a handle
# with no pragmas at all (foreign_keys OFF over a JOIN of
# memory_symbol_anchors -> memory_routes, no busy_timeout). The gate only
# SELECTs -- it reads the anchors that block an edit, it never writes one
# -- so read_only=True makes that a fact sqlite enforces rather than a
# docstring. The handle is still closed by hand in the existing finally.
from ._sqlite_connect import connect as _canonical_connect


@dataclass
class AnchoredMemory:
    route_id: int
    target_path: str
    title: str
    severity: str
    anchor_symbol: str
    anchor_file: str


#: #910 -- WHAT THE PALACE AXIS ACTUALLY DID. Five values, because collapsing
#: them into "palace_result is None" is the defect: three different facts
#: (consulted-clean, never-consulted, consulted-and-threw) all became one silent
#: allowed=True, so "the palace had nothing to say" and "the palace was never
#: asked" were indistinguishable to every caller and to the audit.
#:
#: The split between NOT_WIRED and the two UNAVAILABLE_* values is the one that
#: matters most and the one a first draft gets wrong: running with no palace is a
#: SUPPORTED configuration and must stay quiet, while a palace that failed to
#: load is a defect wearing the supported configuration's clothes.
PALACE_CLEAN = "clean"  # consulted; nothing blocked
PALACE_BLOCKED = "blocked"  # consulted; blockers found
PALACE_NOT_WIRED = "not_wired"  # no hub / no palace. Benign, expected.
PALACE_UNAVAILABLE_SKEW = "unavailable_skew"  # failed to import (#913). DEFECT.
PALACE_NO_ANCHOR_STORE = "skipped_no_anchor_store"  # wired, store absent. DEFECT.
PALACE_ERROR = "error"  # consult raised. DEFECT.

#: The values that mean "the palace axis did not run and that is NOT normal".
PALACE_DEFECT_STATES = frozenset(
    {PALACE_UNAVAILABLE_SKEW, PALACE_NO_ANCHOR_STORE, PALACE_ERROR}
)

#: DRT-08 policy key -- what a CONFIGURED-but-unreachable palace anchor
#: authority means for authorization. `PALACE_NOT_WIRED` never consults this:
#: running with no palace is a supported configuration and stays permissive,
#: which is the one thing DRT-08 explicitly says not to change.
PALACE_UNAVAILABLE_POLICY_KEY = "security.palace_axis_unavailable"

#: DRT-06 policy key -- whether an ack must be backed by a server-minted
#: memory-read receipt, or whether the bare route_id list is still accepted.
ACK_RECEIPT_POLICY_KEY = "security.memory_ack_requires_read_receipt"


@dataclass
class GateResult:
    allowed: bool
    refusal: dict | None = None
    surfaced_memories: tuple[AnchoredMemory, ...] = field(default_factory=tuple)
    #: #910 -- which of the states above the palace axis reached, and why.
    palace_axis: str = PALACE_NOT_WIRED
    palace_note: str = ""
    #: DRT-07/08 -- TRUE when this verdict was reached WITHOUT one of the
    #: authorities it nominally consults. An allowed=True carrying
    #: authority_degraded=True is NOT a grounded pass and must never be
    #: rendered, logged or audited as one. The whole defect class is that a
    #: caller could not tell these two apart.
    authority_degraded: bool = False
    degraded_note: str = ""
    #: DRT-06 -- which receipt state the acknowledgement rested on
    #: (memory_read_receipts.RECEIPT_*). "" when no memory blocked this edit.
    ack_receipt_state: str = ""


def query_anchored_memories(
    project_root: Path,
    *,
    file_path: str = "",
    symbol_name: str = "",
) -> list[AnchoredMemory]:
    """Return memories anchored to the symbol and/or file.

    Anchor row shapes:
      - anchor_kind='symbol', symbol_name=X, file_path=''     — fires on
        any edit to symbol X regardless of file.
      - anchor_kind='symbol', symbol_name=X, file_path=P      — fires
        only when symbol X is edited in file P.
      - anchor_kind='file',   symbol_name='', file_path=P     — fires
        on any edit to file P regardless of symbol target.

    Query semantics (avoid false positives across same-named symbols
    in different files):
      - sym AND fp given (mode='symbol' edit with known file):
          symbol-anchored row matches if name=sym AND (anchor_file='' OR anchor_file=fp).
          file-anchored row matches if file_path=fp.
      - only fp given (non-symbol edit on a file):
          all rows with file_path=fp (both kinds — we don't know which
          symbols the edit touches, so any in-file doctrine surfaces).
      - only sym given (unusual; symbol-mode without resolved file):
          all symbol-anchored rows with name=sym.

    Sovereign-severity routes filtered at SQL.
    """
    fp = (file_path or "").replace("\\", "/").strip()
    sym = (symbol_name or "").strip()
    if not fp and not sym:
        return []
    db_path = project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"
    if not db_path.is_file():
        return []

    params: list[str]
    if sym and fp:
        where = (
            "( (msa.anchor_kind = 'symbol' "
            "    AND msa.symbol_name = ? "
            "    AND (msa.file_path = '' OR msa.file_path = ?)) "
            "  OR (msa.anchor_kind = 'file' "
            "      AND msa.file_path = ?) "
            ")"
        )
        params = [sym, fp, fp]
    elif fp:
        where = "msa.file_path = ?"
        params = [fp]
    else:
        where = "msa.anchor_kind = 'symbol' AND msa.symbol_name = ?"
        params = [sym]

    out: list[AnchoredMemory] = []
    seen: set[int] = set()
    try:
        conn = _canonical_connect(str(db_path), read_only=True)
        conn.row_factory = sqlite3.Row
        try:
            # Memory-loop seal (2026-07-09): semantic_guess anchors (the
            # capture analyzer's auto-derived tier) are ADVISORY — they
            # surface via the read/edit goggles but must never gate an
            # edit. Only when the RFC-4 confidence column exists; legacy
            # DBs keep legacy (all-blocking) semantics unchanged.
            confidence_filter = ""
            try:
                cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(memory_symbol_anchors)",
                    ).fetchall()
                }
                if "confidence" in cols:
                    confidence_filter = (
                        "AND COALESCE(msa.confidence, 'operator_pinned') "
                        "!= 'semantic_guess' "
                    )
            except sqlite3.Error:
                confidence_filter = ""
            sql = (
                "SELECT mr.route_id, mr.target_path, mr.severity, "
                "msa.symbol_name AS anchor_symbol, "
                "msa.file_path AS anchor_file, "
                "COALESCE(mf.title, '') AS title "
                "FROM memory_symbol_anchors msa "
                "JOIN memory_routes mr ON mr.route_id = msa.route_id "
                # SQLite-only doctrine (2026-06): INNER JOIN canonical active
                # memory_index — a route-only GHOST (no active canonical row) must
                # NOT block an edit. Title also comes from the canonical row.
                "JOIN memory_index mf ON mf.path = mr.target_path "
                "  AND COALESCE(mf.status,'active')='active' "
                "  AND COALESCE(mf.superseded_by,'')='' "
                f"WHERE ({where}) AND mr.severity != 'sovereign' "
                f"{confidence_filter}"
                "ORDER BY "
                "  CASE mr.severity WHEN 'critical' THEN 0 "
                "                   WHEN 'high' THEN 1 ELSE 2 END, "
                "  mr.route_id"
            )
            for row in conn.execute(sql, params).fetchall():
                rid = int(row["route_id"])
                if rid in seen:
                    continue
                seen.add(rid)
                title = str(row["title"]) or str(row["target_path"]).rsplit("/", 1)[
                    -1
                ].removesuffix(".md")
                out.append(
                    AnchoredMemory(
                        route_id=rid,
                        target_path=str(row["target_path"]),
                        title=title,
                        severity=str(row["severity"]),
                        anchor_symbol=str(row["anchor_symbol"] or ""),
                        anchor_file=str(row["anchor_file"] or ""),
                    ),
                )
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return out


def check_edit_memory_gate(
    project_root: Path,
    *,
    file_path: str,
    symbol_name: str = "",
    ack_memory_ids: list[int] | None = None,
    ack_drawer_ids: list[str] | None = None,
    tool_name: str = "edit",
    hub: Any = None,
) -> GateResult:
    """Decide whether an edit may proceed against anchored doctrine.

    Returns GateResult(allowed=True) if no anchored memory exists OR
    if every anchored route_id appears in ack_memory_ids.

    Returns GateResult(allowed=False, refusal=...) otherwise. The
    refusal payload is shaped to mirror the read-before-edit gate:
    `ok=False, error=<formatted message>, blocked_by='anchored_memory'`.

    RFC-4 Phase B: when ``hub`` is provided AND ``hub.palace`` is wired,
    the gate ALSO consults palace anchors via ``check_edit_with_palace``.
    Palace blockers (exact_symbol / operator_pinned tiers) compose with
    memory_store blockers — both must be acked for the edit to proceed.
    See ``edit_memory_gate_palace.py``.
    """
    memories = query_anchored_memories(
        project_root,
        file_path=file_path,
        symbol_name=symbol_name,
    )

    # RFC-4 Phase B — consult palace anchors when hub.palace is wired.
    #
    # #910: THREE WAYS TO REACH `palace_result is None` USED TO LOOK IDENTICAL,
    # and that is the whole defect. The palace axis contributes real blockers
    # (exact_symbol, operator_pinned). When it silently does not run, those
    # blockers do not degrade and do not log — they simply cease to exist for
    # that call, and the gate returns as though the palace had been consulted and
    # had nothing to say. "Consulted and clean" and "never reached" became the
    # same answer, which is the unknown-is-not-a-pass rule inverted inside a
    # security gate.
    #
    # POLICY IS DELIBERATELY UNCHANGED: this still FAILS OPEN. An unreachable
    # palace must not stop every edit in the project, and turning this
    # fail-closed would trade a silent hole for a loud outage. What changes is
    # only that the choice is now RECORDED instead of being made in silence.
    palace_result = None
    palace_axis = PALACE_NOT_WIRED
    palace_note = ""

    if hub is None or getattr(hub, "palace", None) is None:
        # DOOR ZERO, and the one a first draft mislabels. No palace is a
        # SUPPORTED configuration — but so is "the palace failed to import", and
        # from here the two are indistinguishable: both leave hub.palace as None.
        # #913 has palace_hub_extension record WHICH it was, so ask rather than
        # assume. Grading a failed import as "not configured" would file the
        # dangerous case under the benign one, which is exactly how this hid.
        unavailable = getattr(hub, "palace_unavailable", None) if hub else None
        if isinstance(unavailable, dict) and unavailable.get("state") == "skewed":
            palace_axis = PALACE_UNAVAILABLE_SKEW
            palace_note = str(unavailable.get("reason") or "")
    else:
        anchor_store = getattr(getattr(hub, "memory", None), "anchors", None)
        if anchor_store is None:
            # DOOR TWO — NO EXCEPTION IS RAISED HERE AT ALL. The palace is wired
            # and the consult is skipped on the happy path. Worse, the getattr
            # chain that finds the store swallows a RENAMED or MISSPELLED
            # attribute identically to a legitimately-absent one: rename
            # hub.memory.anchors and this gate stops consulting the palace
            # forever, with every test that does not specifically assert
            # consultation still passing.
            palace_axis = PALACE_NO_ANCHOR_STORE
            palace_note = (
                "hub.palace is wired but hub.memory.anchors is absent, so palace "
                "anchors were NOT consulted and exact_symbol / operator_pinned "
                "blockers were not evaluated for this edit. This is not the "
                "no-palace configuration: something is half-wired."
            )
        else:
            try:
                from .edit_memory_gate_palace import check_edit_with_palace

                palace_result = check_edit_with_palace(
                    file_path=file_path,
                    symbol_name=symbol_name,
                    ack_memory_ids=ack_drawer_ids,
                    palace_anchor_store=anchor_store,
                    palace_service=hub.palace,
                    hub_ctx=None,  # gate consults palace anchors directly
                    file_anchor_strict=False,
                )
            except (ImportError, AttributeError, TypeError, OSError, ValueError) as exc:
                # DOOR ONE, NARROWED. A bare `except Exception` here also
                # swallowed bugs in our OWN gate logic and reported them as a
                # palace outage. These are what a drifting vendored dependency
                # actually raises; anything else is ours and must not be
                # disguised as somebody else's failure.
                palace_axis = PALACE_ERROR
                palace_note = (
                    f"palace consult raised {type(exc).__name__}: {exc} -- palace "
                    "anchors were NOT evaluated for this edit"
                )
                palace_result = None
            else:
                palace_axis = (
                    PALACE_CLEAN if palace_result.allowed else PALACE_BLOCKED
                )

    authority_degraded = False
    degraded_note = ""
    if palace_axis in PALACE_DEFECT_STATES:
        # LOUD, because the whole point is that this used to be inaudible.
        logger.error("edit gate: palace axis did not run (%s): %s", palace_axis, palace_note)
        # DRT-08 — A CONFIGURED AUTHORITY THAT CANNOT ANSWER IS NOT AN ABSENT ONE.
        #
        # #910 made the five states VISIBLE and deliberately left all of them
        # meaning allowed=True. That was the right first move and the wrong
        # resting place: the three DEFECT states all imply the operator
        # CONFIGURED a palace (wired hub.palace, or a vendored mempalace that
        # failed to import), so exact_symbol / operator_pinned blockers are
        # expected to exist and simply could not be evaluated. Sharing one
        # authorization meaning with "this project has no palace" files the
        # dangerous case under the benign one -- the same mislabelling #910
        # fixed for diagnostics, still live in the VERDICT.
        #
        # PALACE_NOT_WIRED is untouched and does not reach this branch.
        #
        # WHAT LEGITIMATELY REACHES HERE TODAY: in a healthy wiring, nothing.
        # palace_hub_extension wires hub.memory.anchors immediately after
        # hub.palace (palace_hub_extension.py:181-203), so a wired palace has an
        # anchor store unless AnchorStore construction itself failed. The real
        # exposure is a SKEWED vendored mempalace (#913), which is a deploy-time
        # condition affecting the whole box -- hence the named escape hatch in
        # the refusal text rather than a silent pass.
        from .evidence_authority import (
            POLICY_CLOSED,
            POLICY_OPEN,
            format_unavailable_reason,
            resolve_unavailable_policy,
        )

        policy = resolve_unavailable_policy(
            PALACE_UNAVAILABLE_POLICY_KEY, project_root, default=POLICY_CLOSED
        )
        degraded_note = format_unavailable_reason(
            axis=f"palace_anchor_authority:{palace_axis}",
            cause=palace_note or palace_axis,
            recovery=(
                "The palace is CONFIGURED, so operator-pinned / exact-symbol "
                "blockers for this target are UNKNOWN, not absent. Repair the "
                "palace wiring (ai_preflight names the skew; see backlog #913) "
                "or, to proceed with an explicitly unverified palace axis, an "
                f"operator can set {PALACE_UNAVAILABLE_POLICY_KEY}"
                "=degraded_allow."
            ),
            policy=policy,
        )
        if policy == POLICY_CLOSED:
            return GateResult(
                allowed=False,
                refusal={
                    "ok": False,
                    "error": degraded_note,
                    "blocked_by": "palace_authority_unavailable",
                    "palace_axis": palace_axis,
                    "tool": tool_name,
                },
                surfaced_memories=tuple(memories),
                palace_axis=palace_axis,
                palace_note=palace_note,
                authority_degraded=True,
                degraded_note=degraded_note,
            )
        # degraded_allow / open: the edit proceeds, but the verdict is STAMPED.
        # POLICY_OPEN is the pre-DRT-08 behaviour, kept only as a named choice.
        authority_degraded = policy != POLICY_OPEN

    # Combine memory_store blockers + palace blockers.
    #
    # `palace_result is None` still lets the edit through -- the fail-open POLICY
    # is unchanged and intentionally so. The difference is that the verdict now
    # CARRIES which of the five states got us here, so a caller and the audit can
    # tell "consulted and clean" from "never consulted", instead of both arriving
    # as an indistinguishable allowed=True.
    if not memories and (palace_result is None or palace_result.allowed):
        return GateResult(
            allowed=True,
            palace_axis=palace_axis,
            palace_note=palace_note,
            authority_degraded=authority_degraded,
            degraded_note=degraded_note,
        )

    acked: set[int] = {int(x) for x in (ack_memory_ids or [])}
    unacked = [m for m in memories if m.route_id not in acked]

    # If memory_store side allows but palace side refuses, build a
    # combined refusal that surfaces palace-side ack_memory_ids needed.
    if not unacked and palace_result is not None and not palace_result.allowed:
        target_label = symbol_name or file_path or "<edit target>"
        needed = list(palace_result.ack_memory_ids_needed)
        lines = [
            (f"REFUSED: {len(needed)} palace-anchored memor{'y' if len(needed) == 1 else 'ies'} "
            f"for `{target_label}` (RFC-4 confidence tier exact_symbol/operator_pinned)."),
        ]
        for d in palace_result.blocking_drawers:
            lines.append(f"  [{d.drawer_id}] {d.snippet[:80]}")
        lines.append(f"Re-call {tool_name} with ack_drawer_ids={needed} to proceed.")
        return GateResult(
            allowed=False,
            refusal={
                "ok": False,
                "error": "\n".join(lines),
                "blocked_by": "palace_anchored_memory",
                "ack_drawer_ids_needed": needed,
                "tool": tool_name,
            },
            surfaced_memories=tuple(memories),
            palace_axis=palace_axis,
            palace_note=palace_note,
            authority_degraded=authority_degraded,
            degraded_note=degraded_note,
        )

    if not unacked:
        # DRT-06 — THE ACK COVERS EVERY ID. THAT PROVES TYPING, NOT READING.
        #
        # Everything above this line has only ever checked that a set of
        # integers covers a set of integers, and the refusal that produced
        # those integers PRINTED THEM. So the cheapest way past this gate was
        # always to copy them back, and the runtime could not tell that apart
        # from having consumed the doctrine. MEASURED on the live estate (see
        # the memory_read_receipts module docstring): a whole session of
        # surfaced HIGH-priority pointers, none read, edits continuing.
        #
        # The receipt is minted by the READ DOOR and scoped to actor + memory
        # epoch + memory id. It is not a parameter of this function and cannot
        # be: anything a caller can hand us is the ID-copying hole with extra
        # steps.
        receipt_refusal, receipt_state, receipt_degraded, receipt_note = (
            _check_ack_receipts(
                project_root,
                hub=hub,
                memories=memories,
                tool_name=tool_name,
            )
        )
        if receipt_refusal is not None:
            refused = GateResult(
                allowed=False,
                refusal=receipt_refusal,
                surfaced_memories=tuple(memories),
                palace_axis=palace_axis,
                palace_note=palace_note,
                authority_degraded=authority_degraded or receipt_degraded,
                degraded_note=degraded_note or receipt_note,
                ack_receipt_state=receipt_state,
            )
            # THE STATE TRAVELS WITH THE REFUSAL, not just on the verdict
            # object. `ack_receipt_state` was STAMPED and never READ: the axis
            # the acknowledgement rested on was recorded where only the
            # in-process caller could see it, so the refusal the actor actually
            # receives -- and the audit row built from it -- could not say
            # whether this was "never read" (absent), "read a body that has
            # since been amended" (stale), or "read, but actor/epoch unproven"
            # (proven_path_only). Those are three different remedies. Reading
            # the field here is what makes the distinction reach the actor.
            if isinstance(refused.refusal, dict) and refused.ack_receipt_state:
                refused.refusal.setdefault(
                    "ack_receipt_state",
                    refused.ack_receipt_state,
                )
            return refused
        return GateResult(
            allowed=True,
            surfaced_memories=tuple(memories),
            palace_axis=palace_axis,
            palace_note=palace_note,
            authority_degraded=authority_degraded or receipt_degraded,
            degraded_note=degraded_note or receipt_note,
            ack_receipt_state=receipt_state,
        )

    target_label = symbol_name or file_path or "<edit target>"
    n = len(unacked)
    lines: list[str] = [
        f"REFUSED: {n} anchored memor{'y' if n == 1 else 'ies'} for `{target_label}`.",
    ]
    for m in unacked:
        lines.append(f"  [#{m.route_id}] {m.title} ({m.target_path})")
    all_ids = sorted({m.route_id for m in memories})
    lines.append(f"Re-call {tool_name} with ack_memory_ids={all_ids} to proceed.")
    paths = sorted({m.target_path for m in unacked})
    lines.append(f"To read the doctrine first: memory_read(targets={paths!r}).")
    msg = "\n".join(lines)

    return GateResult(
        allowed=False,
        refusal={
            "ok": False,
            "error": msg,
            "blocked_by": "anchored_memory",
            "unacked_memory_ids": [m.route_id for m in unacked],
            "memory_paths": paths,
            "tool": tool_name,
        },
        surfaced_memories=tuple(memories),
        palace_axis=palace_axis,
        palace_note=palace_note,
        authority_degraded=authority_degraded,
        degraded_note=degraded_note,
    )


def _check_ack_receipts(
    project_root: Path,
    *,
    hub: Any,
    memories: list[AnchoredMemory],
    tool_name: str,
) -> tuple[dict | None, str, bool, str]:
    """DRT-06 receipt check. Returns (refusal|None, state, degraded, note).

    Refuses when the ack list covers the blocking route ids but the audit
    store holds no server-minted read receipt for their memory bodies under
    this actor + epoch. The remedy named is the one the blocking refusal
    already advertises (``memory_read``), so the loop is reachable: read,
    then ack, then edit.

    The evidence-store-outage branch is the DRT-07 shape applied here: a
    receipt store we could not read is UNKNOWN, not satisfied.
    """
    from .evidence_authority import (
        POLICY_CLOSED,
        POLICY_OPEN,
        format_unavailable_reason,
        resolve_unavailable_policy,
    )
    from .memory_read_receipts import (
        RECEIPT_ABSENT,
        RECEIPT_PROVEN_PATH_ONLY,
        RECEIPT_STALE,
        RECEIPT_STORE_UNAVAILABLE,
        verify_memory_read_receipts,
    )

    if hub is None or getattr(hub, "execution", None) is None:
        # No audit store to consult at all. Not the same as "no receipt": say
        # which it is rather than inventing either answer.
        return (
            None,
            RECEIPT_STORE_UNAVAILABLE,
            True,
            "ack receipts not verified: no execution audit store on this hub",
        )
    try:
        from .config import get_setting

        required = bool(
            get_setting(
                ACK_RECEIPT_POLICY_KEY,
                project_root=project_root,
                default=True,
            )
        )
    except Exception:  # noqa: BLE001 - an unreadable policy must not weaken the gate
        required = True
    paths = sorted({m.target_path for m in memories})
    verdict = verify_memory_read_receipts(hub, project_root, paths)
    note = ""
    if verdict.unproven_axes:
        note = (
            "ack receipt accepted with UNPROVEN identity axes "
            f"({', '.join(verdict.unproven_axes)}): the read is proven, "
            "actor/epoch scoping is not. What would settle it: a stamped host "
            "session on the minting and checking calls."
        )
    if not required:
        # Operator-chosen legacy behaviour. Still reports the state, so an
        # audit can see that the ack rested on ids alone.
        return None, verdict.state, bool(verdict.unproven_axes), note
    # ONE PREDICATE, NOT TWO. `ReceiptVerdict.is_unavailable` is the verdict's
    # own answer to "could we look at all?". This gate used to re-derive it with
    # an inline `state == RECEIPT_STORE_UNAVAILABLE` comparison, which made the
    # raw string the rival definition and left the property with no production
    # consumer. The property is now the single authority for the question.
    if verdict.is_unavailable:
        policy = resolve_unavailable_policy(
            PALACE_UNAVAILABLE_POLICY_KEY, project_root, default=POLICY_CLOSED
        )
        reason = format_unavailable_reason(
            axis="memory_read_receipt_store",
            cause=verdict.cause,
            recovery=(
                "Whether this actor read the blocking doctrine is UNKNOWN. "
                "Retry; if it persists an operator can set "
                f"{PALACE_UNAVAILABLE_POLICY_KEY}=degraded_allow."
            ),
            policy=policy,
        )
        if policy == POLICY_CLOSED:
            return (
                {
                    "ok": False,
                    "error": reason,
                    "blocked_by": "memory_read_receipt_unavailable",
                    "memory_paths": paths,
                    "tool": tool_name,
                },
                verdict.state,
                True,
                reason,
            )
        return None, verdict.state, policy != POLICY_OPEN, reason
    if verdict.state == RECEIPT_STALE:
        # A DISTINCT OBLIGATION WITH A DISTINCT DISCHARGE. This agent DID read
        # these memories; the law then moved. Telling it "you never read this"
        # would send it looking for a read it already performed, so the refusal
        # names the amendment, the exact memories, and the re-read door --
        # BOUNDED to the memories that actually changed, never the whole
        # anchored set.
        changed = sorted(verdict.stale)
        lines = [
            (
                f"REFUSED: {len(changed)} anchored memor"
                f"{'y' if len(changed) == 1 else 'ies'} CHANGED since you read "
                f"{'it' if len(changed) == 1 else 'them'}."
            ),
            (
                "Your read receipt proves a body that is no longer the version "
                "governing this edit (DRT-06 content axis). A receipt must die "
                "when the law it proves is amended:"
            ),
        ]
        lines.extend(f"  {p}" for p in changed)
        lines.append(
            f"Re-read the current text: memory_read(targets={changed!r}), "
            f"then re-call {tool_name}."
        )
        return (
            {
                "ok": False,
                "error": "\n".join(lines),
                "blocked_by": "memory_read_receipt_stale",
                "stale_memory_paths": changed,
                "memory_paths": paths,
                "tool": tool_name,
            },
            verdict.state,
            False,
            note,
        )
    if verdict.state == RECEIPT_ABSENT:
        missing = sorted(verdict.missing)
        lines = [
            (
                f"REFUSED: {len(missing)} anchored memor"
                f"{'y' if len(missing) == 1 else 'ies'} were ACKNOWLEDGED but "
                f"never READ by this actor at this memory epoch."
            ),
            (
                "An acknowledgement token is not a substitute for having read "
                "the content it acknowledges (DRT-06). The route ids were "
                "accepted; no server-minted read receipt exists for:"
            ),
        ]
        lines.extend(f"  {p}" for p in missing)
        lines.append(f"Read them first: memory_read(targets={missing!r}), then re-call {tool_name}.")
        if verdict.unproven_axes:
            lines.append(
                "Note: identity axes currently unproven "
                f"({', '.join(verdict.unproven_axes)}); the receipt is matched "
                "on memory id alone and actor/epoch scoping is NOT claimed."
            )
        return (
            {
                "ok": False,
                "error": "\n".join(lines),
                "blocked_by": "memory_ack_without_read",
                "unreceipted_memory_paths": missing,
                "memory_paths": paths,
                "tool": tool_name,
            },
            verdict.state,
            bool(verdict.unproven_axes),
            note,
        )
    return (
        None,
        verdict.state,
        verdict.state == RECEIPT_PROVEN_PATH_ONLY,
        note,
    )
