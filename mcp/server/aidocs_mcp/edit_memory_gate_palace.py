"""RFC-4 Phase B — edit_memory_gate palace integration.

Extends AIDOCS's existing edit_memory_gate to consult palace anchors
with confidence-tier filtering per RFC-4 §3.5.

Adopt by copying to ``aidocs_mcp/edit_memory_gate_palace.py``. Wire
it into AIDOCS's edit_memory_gate.check_edit() so palace anchors
are consulted alongside the existing memory_store anchors.

Confidence-tier behavior (RFC-4 §3.5):
  exact_symbol    → BLOCKS edit until ack_memory_ids present
  operator_pinned → BLOCKS edit until ack_memory_ids present
  file_anchor     → WARNS by default; blocks if config.file_anchor_strict=True
  semantic_guess  → SUGGESTS only; never blocks

The gate returns a structured GateResult that AIDOCS's edit tools
consume. ack_memory_ids is checked against the union of blocking-tier
drawer_ids — once every blocker is acked, edit proceeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Anchor confidence tiers that BLOCK edits by default per RFC-4 §3.5.
BLOCKING_TIERS = frozenset({"exact_symbol", "operator_pinned"})

# Tier that warns but doesn't block (unless config overrides).
WARNING_TIERS = frozenset({"file_anchor"})

# Tier that never blocks — surfaces as informational context only.
SUGGEST_TIERS = frozenset({"semantic_guess"})


@dataclass(frozen=True)
class AnchoredDrawerSurface:
    """Summary of a palace drawer anchored to the unit being edited.

    Returned by the gate to the agent (and surfaced via the freeze
    envelope) so the operator can decide whether to ack.
    """

    drawer_id: str
    anchor_id: int
    confidence: str
    snippet: str  # ~200 chars; from PalaceService.get_drawer_summary
    source_file: str
    symbol_name: str


@dataclass(frozen=True)
class GateResult:
    """Result of edit_memory_gate.check_edit_with_palace.

    Mirrors the shape of AIDOCS's existing GateResult (allowed,
    refusal) but extends with palace-specific fields.
    """

    allowed: bool
    refusal: dict | None = None
    blocking_drawers: tuple = field(default_factory=tuple)  # tuple of AnchoredDrawerSurface
    warning_drawers: tuple = field(default_factory=tuple)  # tuple of AnchoredDrawerSurface
    suggest_drawers: tuple = field(default_factory=tuple)  # tuple of AnchoredDrawerSurface
    ack_memory_ids_needed: tuple = field(default_factory=tuple)  # drawer_ids


def check_edit_with_palace(
    *,
    file_path: str,
    symbol_name: str | None = None,
    ack_memory_ids: list | None = None,
    palace_anchor_store,  # AnchorStore instance
    palace_service,  # PalaceService instance
    hub_ctx,  # for get_drawer_summary
    file_anchor_strict: bool = False,
) -> GateResult:
    """Consult palace anchors for the target unit; return GateResult.

    Workflow:
      1. Look up palace anchors for (symbol_name, file_path) OR (file_path)
      2. Group by confidence tier
      3. If any blocking anchors exist:
         - Filter against ack_memory_ids
         - Unacked blockers → refuse with ack_memory_ids_needed
      4. If only warning anchors (file_anchor) AND not strict mode:
         - Allow but surface warnings
      5. If only suggest anchors (semantic_guess):
         - Allow; surface as informational

    Args:
        file_path: path being edited
        symbol_name: optional symbol within the file
        ack_memory_ids: drawer_ids the operator has explicitly ack'd
        palace_anchor_store: AnchorStore instance
        palace_service: PalaceService instance (for drawer summaries)
        hub_ctx: HubContext for the summary call
        file_anchor_strict: if True, file_anchor tier also blocks

    """
    acked: set = set(ack_memory_ids or [])

    # Look up anchors. If symbol_name is given, query by symbol; else by file.
    if symbol_name:
        anchors = palace_anchor_store.list_anchors_for_unit(
            symbol_name=symbol_name,
            file_path=file_path,
        )
    else:
        # No symbol resolution available — look up any anchor with this file
        anchors = palace_anchor_store.list_anchors_for_unit(
            symbol_name="",
            file_path=file_path,
        )

    # Skip anchors that aren't palace-backed (legacy memory_store rows
    # with drawer_id=NULL).
    palace_anchors = [a for a in anchors if a.drawer_id]
    if not palace_anchors:
        return GateResult(allowed=True)

    # Group by confidence tier
    blockers = []
    warnings = []
    suggests = []
    for a in palace_anchors:
        if a.confidence in BLOCKING_TIERS:
            blockers.append(a)
        elif a.confidence in WARNING_TIERS:
            if file_anchor_strict:
                blockers.append(a)
            else:
                warnings.append(a)
        elif a.confidence in SUGGEST_TIERS:
            suggests.append(a)

    # Filter blockers by ack
    unacked_blockers = [a for a in blockers if a.drawer_id not in acked]

    # Hydrate drawer summaries (only for blockers + warnings — saves
    # palace reads on semantic_guess noise)
    def _hydrate(anchors_list) -> list:
        out = []
        for a in anchors_list:
            try:
                summary = palace_service.get_drawer_summary(
                    drawer_id=a.drawer_id,
                    hub_ctx=hub_ctx,
                )
            except Exception:
                summary = {"snippet": "<unavailable>"}
            snippet = summary.get("snippet") if isinstance(summary, dict) else "<unavailable>"
            out.append(
                AnchoredDrawerSurface(
                    drawer_id=a.drawer_id or "",
                    anchor_id=a.anchor_id,
                    confidence=a.confidence,
                    snippet=(snippet or "")[:200],
                    source_file=a.file_path,
                    symbol_name=a.symbol_name,
                ),
            )
        return out

    blocking_surfaces = _hydrate(blockers)
    warning_surfaces = _hydrate(warnings)
    suggest_surfaces = _hydrate(suggests)

    if unacked_blockers:
        return GateResult(
            allowed=False,
            refusal={
                "kind": "anchored_memory_unacked",
                "message": (
                    f"{len(unacked_blockers)} blocking-tier anchor(s) "
                    f"require ack_memory_ids before editing "
                    f"{symbol_name or file_path!r}."
                ),
                "ack_memory_ids_needed": [a.drawer_id for a in unacked_blockers],
                "blocking_drawers": [
                    {
                        "drawer_id": a.drawer_id,
                        "anchor_id": a.anchor_id,
                        "confidence": a.confidence,
                        "snippet": next(
                            (s.snippet for s in blocking_surfaces if s.drawer_id == a.drawer_id),
                            "",
                        ),
                        "symbol_name": a.symbol_name,
                    }
                    for a in unacked_blockers
                ],
            },
            blocking_drawers=tuple(blocking_surfaces),
            warning_drawers=tuple(warning_surfaces),
            suggest_drawers=tuple(suggest_surfaces),
            ack_memory_ids_needed=tuple(a.drawer_id for a in unacked_blockers),
        )

    # All blockers acked (or none). Edit proceeds.
    return GateResult(
        allowed=True,
        blocking_drawers=tuple(blocking_surfaces),
        warning_drawers=tuple(warning_surfaces),
        suggest_drawers=tuple(suggest_surfaces),
    )
