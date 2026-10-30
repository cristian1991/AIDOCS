"""RFC 003 conjoined-memory typed contract.

This module is the typed surface shared between MemPalace (running as
``hub.palace`` inside AIDOCS) and AIDOCS itself. The types defined here
are the load-bearing contract — every cross-boundary value moves as one
of these.

This file is import-safe on its own. It has no dependency on AIDOCS or
on the rest of MemPalace's runtime. It can be imported by AIDOCS's
``server_palace_tools`` module to type-check the surface, and by
MemPalace's standalone CLI / library callers who want to handle these
shapes too.

Reference: ``docs/rfcs/003-aidocs-conjoined-memory-spec.md`` (v0.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional


# ---------------------------------------------------------------------------
# Unit + UnitContent — RFC 003 §3
# ---------------------------------------------------------------------------


UnitKind = Literal[
    "function",
    "class",
    "method",
    "constant",
    "md_section",
    "memory_entry",
    "session_decision",
    "known_trap",
    "capability",
    "rule",
    "outline_section",
    "fallback_byte_window",
    # Size escape hatch (co-co review v0.1 #2)
    "class_summary",
    "section_summary",
    "section_child",
]


BoundaryConfidence = Literal[
    "exact",       # tree-sitter AST or equivalent hard boundary
    "structural",  # outline_extractors regex on known patterns
    "heuristic",   # best-effort regex on unknown shape
    "fallback",    # byte-window with no structural anchor
]


PrivacyClass = Literal[
    "public",
    "internal",
    "pii_potential",
    "sensitive",
    "secrets_possible",
]


UnitExportStatus = Literal[
    "allowed",
    "protected_refused",   # protected_file_registry hit
    "privacy_refused",     # privacy_class above palace floor
    "dnt_refused",          # do-not-touch tag hit  (see audit findings — may merge into protected_refused)
    "freeze_refused",      # session frozen
]


@dataclass(frozen=True)
class Unit:
    """An identifiable structural item in an AIDOCS-managed project.

    Issued by AIDOCS at index time. Consumed by MemPalace's
    PalaceService at mine time and at retrieval time.

    ``unit_id`` is stable across re-index cycles (formula: sha256 of
    project_uuid + normalized_source_file + kind + qualified_symbol_path).
    Lineage records (see ``UnitLineage``) handle refactors that change
    identity (rename / move / split / merge).
    """

    unit_id: str
    kind: UnitKind
    source_file: str
    line_start: int
    line_end: int
    language: Optional[str] = None
    role_tags: frozenset = field(default_factory=frozenset)
    parent_chain: tuple = ()
    content_hash: str = ""
    aidocs_index_version: str = ""
    anchored_memory_ids: tuple = ()
    privacy_class: PrivacyClass = "internal"
    boundary_confidence: BoundaryConfidence = "structural"


@dataclass(frozen=True)
class UnitContent:
    """Bytes vended by AIDOCS for a Unit. MemPalace never reads source
    files directly — every drawer's content comes through this record.

    Invariant (RFC 003 patch #11): when ``unit_export_status != "allowed"``,
    ``content`` MUST be ``b""`` and ``content_hash`` MUST be ``""``.
    Enforced at construction time via ``__post_init__``. Any caller
    that constructs a refused UnitContent with non-empty bytes is a
    bug, and PalaceService's ingest path raises a critical
    ContradictionEvent if it ever sees one.
    """

    unit_id: str
    content: bytes
    content_hash: str
    unit_export_status: UnitExportStatus
    boundary_confidence: BoundaryConfidence = "exact"
    refusal_reason: Optional[str] = None

    def __post_init__(self):
        if self.unit_export_status != "allowed":
            if self.content != b"":
                raise ValueError(
                    f"UnitContent invariant violation: "
                    f"unit_export_status={self.unit_export_status!r} "
                    f"requires content=b'', got {len(self.content)} bytes"
                )
            if self.content_hash != "":
                raise ValueError(
                    "UnitContent invariant violation: refused status "
                    f"({self.unit_export_status!r}) requires content_hash=''"
                )


@dataclass(frozen=True)
class UnitLineage:
    """Records when a unit's identity changes across reindex cycles.

    Without lineage, refactors look like delete+new and break every
    memory anchor pointing at the old unit_id.
    """

    old_unit_id: str
    new_unit_id: str
    relation: Literal["renamed", "moved", "split", "merged", "superseded"]
    detected_at: str  # ISO 8601 UTC
    detector: str     # "aidocs_index_sync" | "operator" | ...


# ---------------------------------------------------------------------------
# CompressionResult + CompressionPolicyViolation — RFC 003 §16
# ---------------------------------------------------------------------------


CompressionNeverCategory = Literal[
    "user_grant",
    "trigger_phrase",
    "freeze_phrase",
    "gate_verdict",
    "audit_reason",
    "hash",
    "enforcement_file_path",
    "contradiction_summary",
    "user_verbatim_in_envelope",
]


@dataclass(frozen=True)
class CompressionPolicyViolation:
    """Raised when ``hub.palace.compress`` is called on text matching
    the never-compress list (§16). Not a factual contradiction — it is
    policy enforcement.
    """

    event_id: str
    detected_at: str
    category: CompressionNeverCategory
    attempted_input_preview: str  # first 200 chars, redacted if sensitive
    caller: str                   # which hub service tried to compress
    action_taken: Literal["refused"] = "refused"


@dataclass(frozen=True)
class CompressionResult:
    """Returned by ``hub.palace.compress(...)``. Caller MUST check
    ``compressed`` and ``violation`` before using ``text``.

    Invariants:
      - ``text`` is ALWAYS the safe-to-use version (compressed when
        allowed, original when refused).
      - ``compressed == False AND violation is not None`` means the
        input matched the never-compress list; original returned for
        the caller's decision (inject as-is, abandon, or split).
      - ``compressed == False AND violation is None`` means the
        compressor had nothing to compress (already compact, empty
        input, etc.); original returned, no policy issue.
      - ``compressed == True`` means AAAK was applied.
    """

    text: str
    compressed: bool
    violation: Optional[CompressionPolicyViolation] = None
    original_length: int = 0
    final_length: int = 0
    method: str = ""  # "aaak_full" | "aaak_partial" | "noop" | "refused"


# ---------------------------------------------------------------------------
# Events — RFC 003 §12
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleUnitEvent:
    """Refresh signal — a drawer's source unit changed since last ingest.

    NOT a contradiction. NOT a policy violation. Default handling is
    quiet re-ingest. Operator may opt into verbose surfacing.
    """

    unit_id: str
    drawer_id: str
    reason: Literal[
        "content_hash_mismatch",
        "index_version_mismatch",
        "anchor_removed",
    ]
    detected_at: str
    action: Literal["re_ingest_quietly", "mark_stale_in_metadata"] = "re_ingest_quietly"


@dataclass(frozen=True)
class ContradictionEvent:
    """Reserved strictly for two-live-assertions disagreement.

    NOT for stale data (use StaleUnitEvent). NOT for policy violations
    (use CompressionPolicyViolation or refusal return values).
    """

    event_id: str
    detected_at: str
    severity: Literal["medium", "high", "critical"]
    kind: Literal[
        "kg_rule_conflict",
        "drawer_audit_conflict",
        "anchor_drift",
        "aidocs_export_invariant_violation",
    ]
    summary: str
    aidocs_assertion: dict
    mempalace_assertion: dict
    suggested_resolution: str = ""


@dataclass(frozen=True)
class PartialBridgeFailureEvent:
    """Raised by bridge_tx journal when a two-store write ends in
    'partial_failure' state — palace wrote, AIDOCS write failed, AND
    compensating palace rollback also failed. Operator must reconcile.
    """

    event_id: str
    tx_id: str
    palace_write_ref: str
    aidocs_failure_reason: str
    palace_rollback_failure_reason: str
    surfaced_at: str


# ---------------------------------------------------------------------------
# HubContext — RFC 003 §6
# ---------------------------------------------------------------------------


Seat = Literal["conductor", "lane"]


@dataclass(frozen=True)
class HubContext:
    """Per-call context the hub passes to every PalaceService method.

    Replaces v0.1's bilateral handshake / outbox / project-identity
    machinery with in-process invariants. Built fresh per inbound tool
    call; never cached across calls.

    The Callable[..., bool] gate primitives let palace tools query
    hub-canonical state without holding direct references to AIDOCS
    services. The hub builds the closures at context-creation time so
    each call's gates evaluate against the right session/project.
    """

    project_root: Path
    project_uuid: str
    session_id: str
    seat: Seat
    lane_id: Optional[str]
    audit_event_id: str
    privacy_floor: PrivacyClass

    # Gate primitives (rule 2: librarian asks; gatehouse answers)
    is_palace_disabled: Callable[[], bool]
    is_frozen: Callable[[], bool]
    is_kill_switch_active: Callable[[], bool]  # AIDOCS bypass — NOT refuse-all
    is_protected: Callable[[Path], bool]
    intent_grant_required: Callable[[str], bool]
    intent_grant_present: Callable[[str], bool]

    # Audit primitives
    mint_audit_event: Callable[..., str]
    record_audit_decision: Callable[..., None]

    # Bridge transaction journal (rule 4)
    bridge_tx_begin: Callable[[str], "BridgeTxHandle"]

    # Read-path staleness consult (RFC-4 Phase D). When the hub attaches a
    # PalaceStaleSignals-like object here, PalaceService.search consults it
    # to HIDE drawers marked 'deleted' (e.g. memory rows retired after
    # ingest) and annotate 'stale' ones. Optional + last so existing
    # constructions stay valid; None disables the consult.
    stale_signals: Optional[object] = None

    # AIDOCS PATCH: RFC-4 Phase A auto-anchoring fields (additive, upstream-safe —
    # service.add_drawer already reads these via getattr; this makes the duck-typed
    # contract explicit). When the hub attaches a
    # UnitResolver (resolve_unit) AND the AnchorStore.register_anchor callback
    # (register_anchor), add_drawer extracts code-unit anchors from drawer
    # content and persists them via register_anchor. Optional + last so existing
    # constructions stay valid; both None disables extraction (the pre-wire
    # 'not_required' behavior). Read via getattr in service.add_drawer.
    resolve_unit: Optional[object] = None
    register_anchor: Optional[object] = None


# ---------------------------------------------------------------------------
# BridgeTxHandle — RFC 003 §7
# ---------------------------------------------------------------------------


class BridgeTxHandle:
    """Handle for a two-store write recorded in palace_bridge_tx.

    Lifecycle:
        tx = hub_ctx.bridge_tx_begin("palace.add_drawer")
        # do palace write
        tx.record_palace_write(ref=drawer_id, status="committed")
        # do aidocs write
        tx.record_aidocs_write(ref=anchor_id, status="committed")
        tx.commit()

    On error:
        tx.rollback(reason="aidocs anchor register failed: ...")

    The concrete implementation lives in AIDOCS's bridge_tx module.
    This base class is the contract PalaceService programs against.
    """

    def __init__(self, tx_id: str, operation: str):
        self.tx_id = tx_id
        self.operation = operation

    def record_palace_write(
        self, *, ref: Optional[str] = None,
        status: Literal["pending", "committed", "failed", "rolled_back"],
    ) -> None:
        raise NotImplementedError

    def record_aidocs_write(
        self, *, ref: Optional[str] = None,
        status: Literal["pending", "committed", "failed", "rolled_back", "not_required"],
    ) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self, reason: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Refusal — uniform shape for explicit "no" returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Refused:
    """Returned by palace methods when a gate refuses the operation.

    Distinct from Python exceptions — gate refusals are normal flow,
    audited and operator-actionable, not error states.
    """

    reason: str
    detail: str = ""
    audit_event_id: Optional[str] = None


# ---------------------------------------------------------------------------
# SearchHit + SearchResult — RFC 003 §8.4 (patch #9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    """Default return shape for ai_palace_search.

    Does NOT include full drawer content (patch #9). Full verbatim is
    opt-in via the audited internal-only path; Phase 1 has no public
    ai_palace_get_drawer tool.
    """

    drawer_id: str
    score: float
    source_file: str
    unit_id: Optional[str]
    snippet: str
    why_matched: tuple        # tuple of str — which tokens / closet entities triggered
    privacy_class: PrivacyClass
    wing: str
    room: str
    staleness: Optional[Literal[
        "unit_deleted",
        "content_hash_mismatch",
        "index_version_mismatch",
    ]] = None
    is_evidence_only: bool = True


@dataclass(frozen=True)
class SearchResult:
    """Wrapper around list[SearchHit] with metadata about the search itself."""

    hits: tuple  # tuple of SearchHit (immutable)
    total_candidates: int
    total_returned: int
    filtered_by_policy: int  # how many hits were dropped by §6.4 current-policy filter
    query_sanitized: bool = False
    prefer_authority: Optional[str] = None  # e.g. "ai_find" for code-like queries


# ---------------------------------------------------------------------------
# PalaceStatus — RFC 003 §8.1
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapRecord:
    """One pinned, active non-code memory record for AIDOCS bootstrap-law
    injection (PalaceService.get_bootstrap_context).

    Unlike SearchHit, this carries FULL verbatim content — bootstrap law
    is injected exactly as stored (verbatim doctrine). Records are
    deterministically ordered and lifecycle-filtered by the caller
    (retired/superseded/deleted drawers never appear). Provenance fields
    let AIDOCS audit exactly what it injected and from where.
    """

    category: str        # rules | domains | roadmaps | operator_profile (== wing)
    title: str           # stable human label (room, e.g. rule filename stem)
    content: str         # full verbatim drawer content
    # provenance
    drawer_id: str
    memory_id: str       # stable unit_id, e.g. "memory:<path>"
    source_kind: str     # the wing the record came from (== category)
    scope: str           # project/scope hint (source_file path or wing)
    source_file: str     # markdown export path, for traceability
    checksum: str        # content_hash (sha256) of the verbatim content
    updated_at: str      # filed_at ISO timestamp
    version: str         # short checksum proxy for drift detection
    status: str = "active"


@dataclass(frozen=True)
class BootstrapContext:
    """Deterministic, bounded bundle of pinned active non-code memory
    returned by PalaceService.get_bootstrap_context for AIDOCS to render
    as constitutional bootstrap law (additionalContext).

    NOT a fuzzy/semantic result — records are exact-category lookups
    against the active palace store, lifecycle-filtered and stably
    ordered. ``truncated`` is True when a size/count limit dropped
    records, so the caller can audit completeness.
    """

    records: tuple       # tuple[BootstrapRecord], deterministic order
    categories: tuple    # tuple[str] — categories requested
    total_records: int
    total_chars: int
    truncated: bool
    generated_at: str
    session_id: Optional[str] = None
    host_session_id: Optional[str] = None
    lane_id: Optional[str] = None


@dataclass(frozen=True)
class PalaceStatus:
    """Returned by ai_palace_status. Health + counts, no content."""

    palace_path: str
    total_drawers: int
    wings: dict      # wing_name → drawer_count
    rooms: dict      # (wing, room) → drawer_count
    kg_entities: int
    kg_triples: int
    tunnels: int
    palace_disabled: bool
    kill_switch_active: bool  # AIDOCS-canonical (bypass semantic)
    vector_disabled: bool
    vector_disabled_reason: str = ""
    aidocs_integrated: bool = False  # True when hosted via hub.palace
