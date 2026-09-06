"""SHARED-TRUTH REGISTRY — the meta-parity surface for #461 (WAR O, sita spartă).

THE DISEASE THIS FILE CURES — and it is NOT any of the 13 instances.

#461's 75-item audit resolved the broken sieve into ONE disease with two
expressions:

  CAUSE A (10/13) — one truth, N unconsumed copies. There is no
    machine-readable source that every consumer imports, so a fix applied to
    one surface leaves the other surface still ruling. A dedup key migrated on
    one of two surfaces; a self-repair token set missing ``Agent``/``Skill``; a
    promote marker meaning "tested" while doctrine reads it as "deployed"; a
    taxonomy declaring coverage its regex could not match.

  CAUSE B (3/13) — a control that is INERT in the process that matters. Hook
    verdicts computed in a broker no deploy restarts; a stamp existing only in
    the server process while the hook process has none; a page reachable only
    by a hash nothing sets; ``evaluate_authorization`` built and green with
    zero production callers.

Both are EDGES ASSERTED BUT NOT ENFORCED. The sieve is not missing parts — it
is missing CONTRACTS BETWEEN PARTS.

WHY THIS FILE, AND NOT FIFTEEN MORE PARITY TESTS. Roughly twenty ad-hoc
``*_parity*`` / ``*_contract*`` suites already live under
``mcp/tests/security/``. Each was written AFTER its own instance bled. NOTHING
required the NEXT shared truth to get one. That absence — not any single
missing test — is the actual hole in the sieve, and it is what this registry
closes: a shared truth is REGISTERED here as importable data, and
``mcp/tests/security/test_shared_truth_edge_contracts.py`` is the REVERSE test
that fails when a registered declaration has no contract test consuming it.

Three-tier ladder (#461's mechanism):

  TIER 1 GENERATE           one declaration generates all consumers;
                            disagreement becomes impossible. #380's end state,
                            a large migration — NOT this file.
  TIER 2 PARITY-CONTRACT    declaration as importable DATA + enforcement
                            through the PUBLIC boundary + a monotone floor + a
                            disclaimed-gap ledger with a ceiling, so the gate
                            cannot be satisfied by WITHDRAWING coverage. Proven
                            twice (the #624 taxonomy gate; the run-trio
                            contract). THIS registry is its host.
  TIER 3 EXECUTION RECEIPT  a control that must run in ANOTHER process writes a
                            durable, versioned receipt IN THAT PROCESS at run
                            time; a gate-time probe asserts receipt presence
                            and freshness. Nothing else catches cause B. The
                            deploy gate already embodies it (doctrine §XVIII:
                            "the crown-head marker is the verdict"); a truth
                            declares ``receipt=True`` to demand it.

THE ABSENT CASE WAITS — IT NEVER FAILS (doctrine §XXIX, KISS). The registry is
SEEDED with declarations that ALREADY have contracts. A shared truth that
exists in the codebase but is NOT registered here produces NO failure: this is
a driver-lookup over what is declared today, not a wall of red for truths
nobody has registered yet. The reverse test bites on NEWLY REGISTERED truths —
which is exactly the moment a shared truth acquires an owner.

FAIL CLOSED ON THE GRANT, FAIL OPEN ON THE REPORT. ``audit()`` never raises:
an unimportable module, a missing file, a permission error all degrade into a
recorded finding rather than an exception. But a truth whose declaration cannot
be RESOLVED is recorded UNCLASSIFIED — never as covered. A registry that
cannot see a declaration must never report it as guarded.

This module is pure DATA + pure functions over the source tree. It imports no
gate, grants nothing, and is never consulted by any runtime dispatch path
(doctrine §XXIX: a seam with a clean input->output boundary).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

# <repo>/mcp/server/aidocs_mcp/shared_truth_registry.py -> parents[2] == <repo>/mcp
MCP_ROOT = Path(__file__).resolve().parents[2]


# ── The declaration ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SharedTruth:
    """One truth that MORE THAN ONE surface must agree about.

    ``declaration`` is the whole point: ``"module:SYMBOL"``, an IMPORTABLE
    object. A shared truth that exists only in prose — a docstring, a doctrine
    line, a comment — cannot be registered, because ``resolve_declaration``
    will not find it. That refusal is deliberate: "everything AIDOCS says is
    LAW" makes a prose claim of coverage a FALSE RULE, not stale documentation.

    ``contract_tests`` are paths relative to ``mcp/`` naming the suites that
    enforce the declaration. At least one must EXIST and must actually NAME the
    declared symbol — a contract test that does not consume the declaration is
    a second copy of the truth, which is cause A again.

    ``consumers`` names the surfaces that must agree. It is EVIDENCE for a
    human reader and for triage; it is not resolved, because a consumer may be
    a shell script, a dashboard page, or another process entirely.

    ``receipt`` is the TIER-3 flag. Set it when the truth governs a control
    that runs in a DIFFERENT process from the one the tests run in (hook
    process, broker, daemon, deploy shell). A receipt-bearing truth needs an
    execution receipt written in THAT process, because an in-process test
    passing proves nothing about a control that is inert in production.
    """

    truth_id: str
    declaration: str
    consumers: tuple[str, ...]
    contract_tests: tuple[str, ...]
    disease: str
    receipt: bool = False


# ── THE REGISTRY ───────────────────────────────────────────────────────────
#
# SEEDED, not complete. Every entry below is a declaration that ALREADY has a
# contract test — the registry starts GREEN on purpose (§XXIX: the absent
# tongue waits). Registering a truth is how it acquires an enforced edge;
# adding an entry with no contract test is the failure this whole file exists
# to produce, and the reverse test will say so by name.
#
# The registry may only GROW. SHARED_TRUTH_FLOOR pins that in the suite:
# withdrawing a registration is withdrawing an enforced edge, and must be an
# explicit reviewed act — never a side effect of a refactor.

SHARED_TRUTHS: tuple[SharedTruth, ...] = (
    # ── #624: the taxonomy's DECLARED coverage vs the patterns implementing it
    #
    # The archetype. judge_taxonomy's module docstring declared "service stop"
    # as confirmable_destructive while the implementing pattern required
    # *service <word> stop* — so `aidocs service stop`, the one stop that
    # DISABLES THE GOVERNANCE DAEMON, was the single shape the rule could not
    # see. Two layers each claiming coverage, neither having it.
    #
    # Its anatomy is the tier-2 template: declaration as importable data,
    # enforcement through the PUBLIC boundary (`evaluate_tool_call`, chosen
    # because the rules live behind FOUR separate doors and a single-cascade
    # gate would reproduce the blind spot it detects), a monotone floor, and a
    # disclaimed-gap ledger with a ceiling.
    SharedTruth(
        truth_id="judge_taxonomy.declared_command_shapes",
        declaration="aidocs_mcp.judge_taxonomy:DECLARED_COMMAND_SHAPES",
        consumers=(
            "aidocs_mcp.heuristic_judge:evaluate_tool_call (the public boundary)",
            "aidocs_mcp.judge_taxonomy:classify (the rule->class buckets)",
            "judge_taxonomy module docstring (the prose that was the lie)",
        ),
        contract_tests=("tests/security/test_taxonomy_pattern_parity_624.py",),
        disease=(
            "CAUSE A — coverage declared in prose that no pattern matched; the "
            "governance daemon's own stop was invisible to the rule guarding it"
        ),
    ),
    # ── #624's DISCLAIMED-GAP LEDGER — the half that makes the gate honest
    #
    # Registered SEPARATELY from the corpus above, deliberately. A parity gate
    # with only a floor can be satisfied by DELETING the inconvenient
    # declaration; the ceilinged gap ledger is what forbids that. Pinning the
    # ledger as its own shared truth means the escape hatch itself carries an
    # enforced edge.
    SharedTruth(
        truth_id="judge_taxonomy.known_uncovered_shapes",
        declaration="aidocs_mcp.judge_taxonomy:KNOWN_UNCOVERED_SHAPES",
        consumers=(
            "aidocs_mcp.judge_taxonomy:known_uncovered_shapes (the one door)",
            "aidocs_mcp.heuristic_judge (the patterns that must NOT match yet)",
        ),
        contract_tests=("tests/security/test_taxonomy_pattern_parity_624.py",),
        disease=(
            "CAUSE A — an unfixed gap quietly deleted from the corpus is the "
            "#624 disease again; pinned asserting the CURRENT WRONG behaviour"
        ),
    ),
    # ── The run trio: ONE RUNNER PER SURFACE, asserted on the WIRE
    #
    # The second proof that tier 2 is host-able today. Declaring
    # surface=GATE_ONLY in tool_interface changed NOTHING on its own —
    # server_run_tools still bound the trio with @server.tool, so the tools
    # stayed on stdio and the ruling was INERT. That is cause A and cause B in
    # one artefact: a declaration with an unconsumed second surface, and a
    # control that was inert where it mattered.
    #
    # Deadlock it caused: policy refused a deploy via ai_run ("use the Bash
    # tool") while the Bash allowlist refused `bash` ("requires operator
    # confirmation") — each refusal pointing at the other.
    SharedTruth(
        truth_id="outer_gate_executor.run_allowlist",
        declaration="aidocs_mcp.outer_gate_executor:RUN_ALLOWLIST",
        consumers=(
            "aidocs_mcp.tool_interface (the SSOT spec + surface declaration)",
            "aidocs_mcp.outer_gate_catalog (the WebMCP gate catalog row)",
            "aidocs_mcp.mcp_server:create_server (expose_run registration gate)",
        ),
        contract_tests=("tests/security/test_ssot_run_contracts.py",),
        disease=(
            "CAUSE A+B — a GATE_ONLY declaration that stdio registration "
            "ignored, so the ruling was inert on the surface it named"
        ),
    ),
    # ── The tool-surface waiver ledgers — the REVERSE walk
    #
    # Four parity suites all iterated FROM the registry OUTWARD, so a tool
    # registered on a live surface with ZERO declaration passed every one of
    # them invisibly — which is how the off-catalog surface grew to roughly the
    # size of the catalog itself before anyone counted it. The reverse walk is
    # the shape THIS registry copies: walk the live thing backwards to the
    # declaration, and require an explicit reviewed waiver for the difference.
    #
    # Both ledgers are registered because the two live surfaces are two
    # processes' worth of truth: stdio (local agent) and the WebMCP gate.
    SharedTruth(
        truth_id="tool_surface_waivers.stdio",
        declaration="aidocs_mcp.tool_surface_waivers:EXPLICIT_WAIVER_SET",
        consumers=(
            "aidocs_mcp.tool_interface (the canonical catalog)",
            "aidocs_mcp.mcp_server (the live stdio surface walked backwards)",
        ),
        contract_tests=("tests/security/test_tool_surface_reverse_parity.py",),
        disease=(
            "CAUSE A — forward-only parity: a live tool with no declaration "
            "was invisible to all four existing parity suites"
        ),
    ),
    SharedTruth(
        truth_id="tool_surface_waivers.gate",
        declaration="aidocs_mcp.tool_surface_waivers:EXPLICIT_GATE_WAIVER_SET",
        consumers=(
            "aidocs_mcp.tool_interface (the canonical catalog)",
            "aidocs_mcp.outer_gate_catalog:advertised (the live WebMCP surface)",
        ),
        contract_tests=("tests/security/test_tool_surface_reverse_parity.py",),
        disease=(
            "CAUSE A — the same forward-only blind spot on the gate surface, "
            "where privileged-only tools hide from an unprivileged walk"
        ),
    ),
    # ── The vulture suppression classification — cause B's own detector
    #
    # Vulture is the ONE detector that catches a caller-less control, and its
    # verdict is suppressible by a line in a file nobody audits. #461's audit
    # found the dead three-phase security helper sitting in
    # vulture_allowlist.py with no owner and no end state: cause B's detector,
    # disabled. So the suppression surface is itself a shared truth — the
    # allowlist file, the classifier, the deploy lane and the pinned ceilings
    # must agree about what is suppressed and why.
    SharedTruth(
        truth_id="vulture.security_surface_predicate",
        declaration="aidocs_mcp.shared_truth_registry:SECURITY_SURFACE_TOKENS",
        consumers=(
            "mcp/vulture_allowlist.py (@vulture-class sections)",
            "mcp/scripts/vulture_allowlist_classify.py (the classifier)",
            "mcp/scripts/deploy_aidocs_gate.sh Gate 1d (the vulture lane)",
        ),
        contract_tests=(
            "tests/security/test_vulture_security_stewardship.py",
        ),
        disease=(
            "CAUSE B — the caller-less-control detector silenced by an "
            "allowlist entry carrying no owner and no end state"
        ),
    ),
    # ── The registry's OWN floor — the meta-truth
    #
    # A mechanism that does not judge itself is the first thing to rot. The
    # floor is a shared truth like any other: the module declares it, the
    # reverse suite enforces it, and withdrawing a registration must fail
    # loudly rather than quietly shrink the enforced boundary.
    SharedTruth(
        truth_id="shared_truth_registry.floor",
        declaration="aidocs_mcp.shared_truth_registry:SHARED_TRUTH_FLOOR",
        consumers=(
            "aidocs_mcp.shared_truth_registry:SHARED_TRUTHS (the registry)",
            "pytest -m edge_contract (the countable acceptance suite)",
        ),
        contract_tests=(
            "tests/security/test_shared_truth_edge_contracts.py",
        ),
        disease=(
            "CAUSE A applied to the cure itself — a meta-gate whose own "
            "coverage can be withdrawn silently is one more unenforced edge"
        ),
    ),
    # ── The scanner's exemption set — the door it silently opened
    #
    # skill_scanner SKIPS kind in {doctrine, stance} (scan_skill) because
    # documentation prose legitimately contains `subprocess`, `curl|sh` and the
    # other shapes the scanner hunts; scanning it drowns the real signal. That
    # exemption is CORRECT for the empire's own scrolls.
    #
    # It became a hole the moment a SECOND consumer appeared: the public
    # ai_skill upsert door, where `kind` is CALLER-SUPPLIED. The one class
    # amendment 2 most requires scanning was the class exempt from it, and the
    # exemption was ATTACKER-SELECTED. Classic cause A — one truth (which kinds
    # are law) that the scanner and the write door each had to know, with no
    # shared source, so hardening one left the other admitting law unscanned.
    #
    # The write door now DERIVES from this symbol rather than listing kinds, so
    # a future exempt kind is guarded the moment it is added to the scanner —
    # the class, not the instance. An earlier revision of the door carried a
    # hardcoded fallback set; that WAS the second copy, and it is removed. When
    # the set cannot be resolved the door refuses EVERY write: an undecidable
    # verdict is UNKNOWN, never clean (promoted-06ad3c5f61ab).
    SharedTruth(
        truth_id="skill_scanner.documentation_kinds",
        declaration="aidocs_mcp.skill_scanner:_DOCUMENTATION_KINDS",
        consumers=(
            "aidocs_mcp.skill_scanner:scan_skill (the exemption itself)",
            "aidocs_mcp.skill_store:_scanner_exempt_kinds (the write-door guard)",
            "aidocs_mcp.server_skill_tools (ai_skill upsert — the admission path)",
        ),
        contract_tests=("tests/security/test_doctrine_write_door_closed.py",),
        disease=(
            "CAUSE A — a scanner exemption that a second, agent-reachable "
            "consumer had to know about and did not, so law entered unscanned "
            "through a caller-supplied kind"
        ),
    ),
    # ── The sync-fold stream whitelist — the interim tier-pair proxy
    #
    # The sync transport's fold is LWW and direction-blind (fold_events), and
    # the hub assigns canonical order (server_hlc). Per the operator rulings
    # (2026-07-30): law-family streams ARE permitted on this transport for
    # SAME-TIER tenant replication ("bidirectional is correct, at
    # tenant-level"), but a law row must NEVER cross a tier boundary through
    # the clock-ordered fold (Amendment 3, #645 — kingdom->empire derivation
    # is lossy, so a cross-tier reconcile destroys the rich source). No
    # tier-pair guard exists on the transport yet, so STREAMS is the interim
    # proxy keeping the announced plan ("memory joins later via the same
    # fold/transport", sync_store.py) from loading the gun. RE-POINTED
    # (2026-07-30): the tier-pair guard LANDED as
    # law_projection_ledger.refuse_cross_tier_fold — a fold that would
    # collapse a higher-tier row into a lower tier is refused whatever the
    # HLC says; unknown tiers refuse; same-tier folds let HLC decide (the
    # REPLICATION axis is bidirectional at tenant level per the operator
    # ruling). The guard is now the enforced truth; the STREAMS whitelist
    # remains what it always was — an interim proxy, still asserted by the
    # same contract test as the law-family intersection invariant (consumed
    # as data, never restated).
    SharedTruth(
        truth_id="sync_store.streams_law_family_exclusion",
        declaration="aidocs_mcp.law_projection_ledger:refuse_cross_tier_fold",
        consumers=(
            "aidocs_mcp.law_projection_ledger:project_law_body (the ledger)",
            "aidocs_mcp.sync_store:SyncEvent.validate (interim envelope refusal)",
            "aidocs_mcp.backlog_sync_sitter:_vps_hub_reconcile (the transport)",
            "aidocs_mcp.sync_store:STREAMS (the interim whitelist proxy)",
        ),
        contract_tests=("tests/memory/test_sync_direction_amendment3_645.py",),
        disease=(
            "CAUSE A latent — a clock-ordered, direction-blind fold whose own "
            "header announces law will join it; without a registered edge the "
            "admission would decide a tier boundary by HLC"
        ),
    ),
    # ── #54: THE LANE ROSTER — the false zero's own table
    #
    # The canonical CAUSE-A instance of 2026-07-30: `ai_seat(mode='overview')`
    # reported ZERO live lanes while SIX were running, because
    # get_all_lanes_status read ONLY `lane_control` — a table whose sole
    # writer is the manual pause/resume/cancel override — while the spawn
    # path writes `session_lane_agents` and never touches lane_control.
    # Writer and reader never shared a table. Six completed lanes' reports
    # sat undelivered while a conductor decided on a false statement of fact.
    #
    # ANTI-PATTERN, RECORDED DELIBERATELY (what this truth IS NOT):
    # `lane_control` IS AN OVERRIDE CHANNEL, NEVER A ROSTER. Any reader that
    # treats lane_control as the set of live lanes reproduces the false zero.
    # The roster is SessionLaneAgentsStore over session_lane_agents; overview
    # merges lane_control back in only as the manual override it is.
    SharedTruth(
        truth_id="lane_roster.session_lane_agents",
        declaration=(
            "aidocs_mcp.session_lane_agents_store:SessionLaneAgentsStore"
        ),
        consumers=(
            (
                "aidocs_mcp.conductor_comms:get_all_lanes_status "
                "(ai_seat overview — the cured false zero)"
            ),
            "aidocs_mcp.conductor_comms:xaacp_directory (#54 discovery surface)",
            (
                "aidocs_mcp.conductor_comms:xaacp_resolve_caller_route / "
                "_xaacp_resolve_target_route (XAACP routing)"
            ),
            "aidocs_mcp.cross_agent_coordination:roster_view (ai_lane_agents)",
            "aidocs_mcp.task_actor_identity (worker slot resolution)",
            "aidocs_mcp.agent_audit (roster liveness audit)",
            (
                "apps/aidocs-dashboard/src-tauri/src/main.rs "
                "(DECLARED EXCEPTION: Rust process reads session_lane_agents via "
                "raw SQL — another process, cannot import the store)"
            ),
        ),
        contract_tests=("tests/coordination/test_xaacp_directory_54.py",),
        disease=(
            "CAUSE A — overview read lane_control (an override channel) as if "
            "it were the roster while the spawn path wrote "
            "session_lane_agents; zero lanes reported while six ran, and the "
            "conductor hand-carried payloads the protocol claimed to deliver"
        ),
    ),
    # ── #54/#640: THE SEAT MAP — the durable seat registry's one write door
    #
    # msg_role_map is the durable record of which host holds which seat on
    # which session. Its ONE writer is msg_register_role; every XAACP seat
    # resolution (caller route, role-name target addressing, the directory's
    # seat listing) and the seat-authority check for role messages read it.
    # #640 specimen 3 was this edge unenforced from the other side: the seat
    # branch of the target resolver was unreachable dead code, so the map's
    # readers could never reach what its writer recorded.
    SharedTruth(
        truth_id="seat_map.msg_role_map",
        declaration="aidocs_mcp.conductor_comms:msg_register_role",
        consumers=(
            (
                "aidocs_mcp.conductor_comms:_xaacp_role_route_for_host "
                "(caller seat route)"
            ),
            (
                "aidocs_mcp.conductor_comms:_xaacp_resolve_target_route "
                "(role-name addressing — #640 specimen 3)"
            ),
            "aidocs_mcp.conductor_comms:xaacp_directory (seat listing)",
            (
                "aidocs_mcp.conductor_comms:msg_resolve_caller_role "
                "(seat authority for send/reply)"
            ),
        ),
        contract_tests=(
            "tests/coordination/test_xaacp_directory_54.py",
            "tests/security/test_ai_msg_block_reporting_640.py",
        ),
        disease=(
            "CAUSE A+B — one durable seat map whose reader branch was "
            "unreachable dead code (a control inert where it mattered), so a "
            "lane could not address the conductor its own session had "
            "registered; refusals stay pinned: cross-session {}, "
            "unregistered {}, ambiguous {}"
        ),
    ),
    # ── #474/#639/#669: THE DASHBOARD RESTATES SERVER VOCABULARIES IN TYPESCRIPT
    #
    # A whole CLASS of cause A that no Python-side parity suite could ever have
    # caught, because the second copy is in another LANGUAGE. Nothing imports
    # across that boundary, so these copies rot in total silence — and the
    # dashboard is the operator's only unmediated view of his own project, which
    # makes a stale copy here a FALSE RULE he then rules from.
    #
    # THE ANTI-PATTERN, recorded because recording what a truth IS NOT is what
    # stops the next reader repeating it:
    #
    #   A DASHBOARD CONSTANT IS A RENDERING CHOICE, NEVER A VOCABULARY.
    #
    # The dashboard legitimately renders a SUBSET (statuses reached only through
    # a dedicated operation are not dropdown options) and legitimately adds
    # presentation the store has no opinion about (icons, tones, ordering for
    # display). What it may NEVER do is INVENT or OMIT a member and thereby
    # answer a question the store already answered. The distinction is testable:
    # a subset must be derived from the authority MINUS a NAMED, justified
    # exclusion — never re-typed as its own list.
    #
    # THE FALSE-ZERO ANATOMY, measured 2026-07-30: `STATUSES` omitted
    # `rejected`. A rejected item then rendered in a <select> whose value was not
    # among its options, so the browser fell back to the FIRST option and
    # REJECTED items displayed as OPEN — and the status was unsettable from the
    # dashboard at all. Nothing threw. Nothing logged. The page looked correct.
    # A wrong value that renders is worse than a missing one that errors.
    SharedTruth(
        truth_id="backlog.statuses",
        declaration="aidocs_mcp.project_backlog_store:_STATUSES",
        consumers=(
            "aidocs_mcp.project_backlog_store:update (the validating writer)",
            (
                "apps/aidocs-dashboard/src/BacklogTodoPage.tsx:STATUSES "
                "(the status dropdown — MINUS {removed, merged}, which are written "
                "only by remove()/merge() and are named exclusions, not omissions)"
            ),
            "aidocs_mcp.backlog_surfacer:_ACTIVE_STATUSES (the UPS surfacing filter)",
        ),
        contract_tests=("tests/security/test_dashboard_client_truth_parity.py",),
        disease=(
            "CAUSE A — a cross-LANGUAGE copy no import can bind; the dashboard "
            "omitted `rejected` and silently rendered rejected items as OPEN"
        ),
    ),
    SharedTruth(
        truth_id="backlog.priorities",
        declaration="aidocs_mcp.project_backlog_store:_PRIORITIES",
        consumers=(
            "aidocs_mcp.project_backlog_store:add/update (_canon_priority + validation)",
            (
                "apps/aidocs-dashboard/src/BacklogTodoPage.tsx:PRIORITIES "
                "(the priority dropdown and the tier filter)"
            ),
        ),
        contract_tests=("tests/security/test_dashboard_client_truth_parity.py",),
        disease=(
            "CAUSE A — the operator's triage ladder restated client-side; a "
            "missing tier is unreachable from the UI, an invented one is a "
            "write the store rejects"
        ),
    ),
    # ── KIND: REGISTERED AS AN ABSENCE, NOT A DRIFT
    #
    # DIFFERENT SHAPE FROM ITS TWO SIBLINGS ABOVE, and the entry says so
    # deliberately. #573's kind vocabulary had NO dashboard consumer at all
    # until 2026-07-30 — kind is half the triage grid (severity x kind;
    # high-severity x known-fix is the actionable quadrant) and the operator
    # could neither see nor set it. There was nothing to drift, which is exactly
    # why every parity discipline in the codebase was blind to it: a truth with
    # ZERO consumers passes every comparison ever written.
    #
    # THAT IS THE INERTNESS CLASS NOTHING ELSE DETECTS — cause B wearing cause
    # A's clothes. A registry that only guards truths which already have
    # consumers cannot see the surface that was never built. Registering the
    # absence is what gave it an owner; the consumer below was then wired to
    # satisfy it, not the other way round.
    #
    # SECOND ANTI-PATTERN, and the reason this entry needs its own contract:
    #
    #   KIND_UNSET ("") IS THE STORED DEFAULT AND IS NOT A KIND.
    #
    # `validate_kind` REFUSES it as a write value — an item nobody rated must
    # stay distinguishable from one deliberately marked `investigate`, because
    # defaulting would manufacture false confidence. So the dashboard DISPLAYS
    # unrated as a disabled option and never sends it, and the whole wire path
    # (TS `kind || null` -> Rust empty-drop -> service `_opt`) keeps "don't
    # touch it" and "set it to unrated" as different values. Collapsing those
    # two would be a silent false rating on every unrelated edit.
    #
    # ORDER IS PART OF THIS TRUTH: KIND_ORDER is "cheapest-to-act-on first — the
    # ONE list consumers iterate", so the contract asserts SEQUENCE, not just
    # membership. A set comparison would pass on a dashboard that inverted the
    # ladder the operator reads.
    SharedTruth(
        truth_id="backlog.kind_order",
        declaration="aidocs_mcp.project_backlog_store:KIND_ORDER",
        consumers=(
            "aidocs_mcp.project_backlog_store:validate_kind (the one refusal door)",
            (
                "aidocs_mcp.project_backlog_store:list_backlog (kind_filter, plus the "
                "KIND_FILTER_UNSET pseudo-value for the UNRATED worklist)"
            ),
            (
                "apps/aidocs-dashboard/src/BacklogTodoPage.tsx:KINDS "
                "(the per-row kind selector — THE CONSUMER THAT DID NOT EXIST until "
                "2026-07-30; this truth was registered as an absence and the surface "
                "built to satisfy it)"
            ),
        ),
        contract_tests=("tests/security/test_dashboard_client_truth_parity.py",),
        disease=(
            "CAUSE B — a vocabulary with ZERO consumers on the operator's own "
            "surface; half the triage grid existed in the store and was "
            "invisible and unsettable from the dashboard, and no parity "
            "discipline can see a truth nothing consumes"
        ),
    ),
    # ── The judge's verdict classes on the dashboard (#639)
    #
    # The panel groups override rules under one heading per class. A class
    # present in the taxonomy but absent from the client list renders under NO
    # heading — the rules simply vanish from a page the operator is using to
    # decide what the judge may skip. Silent omission again, on a security
    # surface this time.
    #
    # SCOPE OF THE CONTRACT, stated so nobody reads it as broader than it is: it
    # pins the class VOCABULARY and that every class has a label entry. It does
    # NOT pin the label TEXT, and one label is known conditionally false —
    # "Confirmable destructive — freeze + operator confirm" holds only when
    # operator destructive intent is present; for a REMOTE principal the confirm
    # never happens and the same rule hard-refuses (#632). A guarded vocabulary
    # under an unguarded claim is still an improvement, but it is not coverage
    # of the claim.
    SharedTruth(
        truth_id="judge_taxonomy.dashboard_class_vocabulary",
        declaration="aidocs_mcp.judge_taxonomy:ALL_CLASSES",
        consumers=(
            "aidocs_mcp.judge_taxonomy:classify (the rule->class buckets)",
            (
                "apps/aidocs-dashboard/src/JudgeOverridePanel.tsx:CLASS_ORDER "
                "(section order — an unlisted class renders under no heading)"
            ),
            (
                "apps/aidocs-dashboard/src/JudgeOverridePanel.tsx:CLASS_LABEL/CLASS_TONE "
                "(an unlabelled class renders as a bare identifier)"
            ),
        ),
        contract_tests=("tests/security/test_dashboard_client_truth_parity.py",),
        disease=(
            "CAUSE A — the judge taxonomy restated in TypeScript with no parity "
            "test (#639); rename or split a class and override rules silently "
            "disappear from the operator's own security surface"
        ),
    ),
)


# ── The floor: the registry may only GROW ──────────────────────────────────
#
# Raising it is how enforced edges accumulate. Lowering it is withdrawing an
# edge and must be an explicit, reviewed act — never a refactor side effect.
# Same one-way ratchet as judge_taxonomy._DECLARED_SHAPE_FLOOR.
# 11 -> 15 (2026-07-30): the four dashboard-facing vocabularies — backlog
# statuses, priorities and KIND_ORDER, plus the judge class list. First entries
# whose second copy is in another LANGUAGE, and the first registered as an
# ABSENCE (KIND_ORDER had no consumer at all).
SHARED_TRUTH_FLOOR: int = 15


# ── The security-surface predicate (shared with the vulture classifier) ────
#
# A CLASS PREDICATE, not an enumeration of artefacts. The law forbids curing by
# enumeration: naming the 13 instances would fix 13 things and leave the
# disease. These tokens describe the SHAPE of a security surface — a gate, a
# judge, an audit, an enforcement path, a guard, a trust/identity boundary —
# and they match against a suppression entry's defining FILE and SYMBOL name.
#
# Consequence, and it is the point: a NEW security surface named by the same
# shape is in scope the day it is written. Nobody has to remember to add it.
#
# Lives HERE rather than in the classifier script because the classifier is a
# build-time tool (doctrine §XXXI: dev/CI toolchain, never in the runtime), and
# a build-time tool must not be the home of a truth the runtime suite reads.
SECURITY_SURFACE_TOKENS: tuple[str, ...] = (
    "anticoup",
    "audit",
    "authent",
    "authoriz",
    "confirm",
    "credential",
    "enforce",
    "enforcement",
    "escalation",
    "freeze",
    "gate",
    "governed",
    "guard",
    "hook",
    "identity",
    "judge",
    "permission",
    "policy",
    "principal",
    "privile",
    "rbac",
    "sandbox",
    "secret",
    "security",
    "strike",
    "taxonomy",
    "tenanc",
    "token",
    "trust",
    "violation",
)


def is_security_surface(text: str) -> bool:
    """True when ``text`` names a gate / audit / enforcement surface.

    ``text`` is a whole suppression entry line — symbol AND its defining file —
    so either half can put the entry in scope. Deliberately generous: a false
    positive costs one ``owner=``/``end_state=`` annotation, while a false
    negative silences the only detector that finds a caller-less control. Fail
    toward stewardship.
    """
    low = str(text or "").lower()
    return any(tok in low for tok in SECURITY_SURFACE_TOKENS)


# ── The audit (fail-open on the report, fail-closed on the grant) ──────────

STATUS_COVERED = "covered"
STATUS_UNCOVERED = "uncovered"
STATUS_UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class TruthFinding:
    """One truth's verdict. ``status`` is a machine state, never prose."""

    truth_id: str
    status: str
    reason: str
    present_tests: tuple[str, ...] = ()
    missing_tests: tuple[str, ...] = ()
    consuming_tests: tuple[str, ...] = ()


@dataclass
class RegistryAudit:
    findings: tuple[TruthFinding, ...] = ()
    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Green ONLY when every registered truth is covered.

        Unclassified is NOT ok. A registry that cannot see a declaration must
        never report it as guarded — that inversion is the #461 disease
        reproduced inside the detector.
        """
        return not self.uncovered and not self.unclassified

    def by_id(self, truth_id: str) -> "TruthFinding | None":
        for finding in self.findings:
            if finding.truth_id == truth_id:
                return finding
        return None


def resolve_declaration(declaration: str) -> "tuple[object | None, str]":
    """Resolve ``"module:SYMBOL"`` to the live object.

    Returns ``(obj, "")`` on success or ``(None, reason)`` on any failure.
    NEVER raises: an import that explodes on a box missing an optional vendor
    is a REPORT-level fact, and the report fails open (doctrine §XXXII — an
    absent guest costs zero function). The GRANT still fails closed: the caller
    records UNCLASSIFIED, which ``RegistryAudit.ok`` counts as not-ok.
    """
    spec = str(declaration or "").strip()
    if ":" not in spec:
        return None, f"declaration {spec!r} is not 'module:SYMBOL'"
    module_name, _, symbol = spec.partition(":")
    module_name, symbol = module_name.strip(), symbol.strip()
    if not module_name or not symbol:
        return None, f"declaration {spec!r} has an empty module or symbol"
    try:
        module = importlib.import_module(module_name)
    except BaseException as exc:  # noqa: BLE001 — report, never raise
        return None, f"module {module_name!r} did not import: {type(exc).__name__}"
    if not hasattr(module, symbol):
        return None, (
            f"{module_name} has no attribute {symbol!r} — a declaration that is "
            f"not importable DATA is prose, and prose is the disease"
        )
    return getattr(module, symbol), ""


def _symbol_of(declaration: str) -> str:
    return str(declaration or "").rpartition(":")[2].strip()


def _test_names_declaration(path: Path, symbol: str) -> bool:
    """Does the contract test actually NAME the declared symbol?

    The load-bearing check, and the reason a marker alone would not do. A suite
    that never mentions the declaration is not enforcing it — it is a SECOND
    COPY of the truth, restated in assertions, which is precisely cause A.
    Reading the file is enough: an import, a parametrize over it, or a call
    through its one door all mention the name.

    Fail-open on an unreadable file (returns False -> reported as uncovered,
    never as covered).
    """
    if not symbol:
        return False
    try:
        return symbol in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def audit(
    truths: "tuple[SharedTruth, ...] | list[SharedTruth] | None" = None,
    *,
    mcp_root: "Path | None" = None,
) -> RegistryAudit:
    """Classify every registered shared truth. The reverse test's one door.

    A truth is COVERED only when ALL of:
      1. its declaration resolves to a live importable object;
      2. at least one declared contract-test path EXISTS on disk;
      3. at least one existing contract test NAMES the declared symbol.

    Anything else is UNCOVERED (the edge is asserted, not enforced) or
    UNCLASSIFIED (the registry cannot see the declaration at all). Neither is
    covered, and ``ok`` is false for both.

    ``truths``/``mcp_root`` are injectable so the suite can prove the checker
    BITES on a synthetic newly-registered truth without touching the real
    registry — a meta-test must be falsifiable by construction, or it is itself
    an unenforced edge.
    """
    entries = tuple(SHARED_TRUTHS if truths is None else truths)
    root = MCP_ROOT if mcp_root is None else Path(mcp_root)
    findings: list[TruthFinding] = []

    for truth in entries:
        obj, reason = resolve_declaration(truth.declaration)
        if obj is None:
            findings.append(
                TruthFinding(
                    truth_id=truth.truth_id,
                    status=STATUS_UNCLASSIFIED,
                    reason=reason,
                )
            )
            continue

        present: list[str] = []
        missing: list[str] = []
        for rel in truth.contract_tests:
            (present if (root / rel).is_file() else missing).append(rel)

        symbol = _symbol_of(truth.declaration)
        consuming = tuple(
            rel for rel in present if _test_names_declaration(root / rel, symbol)
        )

        if not truth.contract_tests:
            status = STATUS_UNCOVERED
            why = (
                "registered with NO contract test — this is the #461 hole "
                "itself: an edge declared, never enforced"
            )
        elif not present:
            status = STATUS_UNCOVERED
            why = (
                f"every declared contract test is MISSING from disk: {missing} "
                f"— a deleted contract silently withdraws the edge"
            )
        elif not consuming:
            status = STATUS_UNCOVERED
            why = (
                f"contract test(s) {present} exist but none NAMES {symbol!r} — "
                f"a suite that restates the truth instead of importing it is a "
                f"second copy, which is the disease it should cure"
            )
        else:
            status = STATUS_COVERED
            why = f"{symbol} is consumed as data by {list(consuming)}"

        findings.append(
            TruthFinding(
                truth_id=truth.truth_id,
                status=status,
                reason=why,
                present_tests=tuple(present),
                missing_tests=tuple(missing),
                consuming_tests=consuming,
            )
        )

    result = RegistryAudit(findings=tuple(findings))
    for finding in findings:
        {
            STATUS_COVERED: result.covered,
            STATUS_UNCOVERED: result.uncovered,
            STATUS_UNCLASSIFIED: result.unclassified,
        }[finding.status].append(finding.truth_id)
    return result


def contract_test_paths() -> "tuple[str, ...]":
    """Every distinct contract-test path the registry claims, sorted.

    The countable acceptance surface's companion: ``pytest -m edge_contract``
    selects the mechanism, and this names the suites the registry stands
    behind — the same way ``pytest -m rust_contract`` IS the Rust port's
    acceptance suite (doctrine §XXIX, "the boundary is enumerable").
    """
    return tuple(sorted({rel for t in SHARED_TRUTHS for rel in t.contract_tests}))


def render_summary(result: "RegistryAudit | None" = None) -> str:
    """Operator-readable audit block. Report-only; grants nothing."""
    res = audit() if result is None else result
    lines = [
        "-- shared-truth edge contracts (#461 WAR O) --",
        (
            f"registered={len(res.findings)} covered={len(res.covered)} "
            f"uncovered={len(res.uncovered)} unclassified={len(res.unclassified)} "
            f"floor={SHARED_TRUTH_FLOOR}"
        ),
    ]
    for finding in res.findings:
        if finding.status != STATUS_COVERED:
            lines.append(f"  [{finding.status.upper()}] {finding.truth_id}: {finding.reason}")
    return "\n".join(lines)
