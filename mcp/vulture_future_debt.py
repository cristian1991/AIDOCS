"""Vulture FUTURE-DEBT ledger — code intentionally not yet wired (#426).

Doctrine (king, 2026-07-17): TWO surfaces govern the deploy gate's vulture
lane (Gate 1d):
  - mcp/vulture_allowlist.py   = FALSE POSITIVES ONLY. Vulture is WRONG:
      the symbol IS consumed (tests, dynamic dispatch, getattr). Fed to
      vulture as source; hidden from deploy output.
  - mcp/vulture_future_debt.py = THIS FILE. Vulture is RIGHT that no
      production consumer exists — but the absence is INTENTIONAL and
      tracked. Every record carries a direction + owner + note:
        direction=add    -> a consumer is coming (staged wiring, next slice)
        direction=remove -> the symbol is scheduled for deletion
      These records ALWAYS SURFACE in the deploy report as the non-blocking
      Gate-1d future-debt ledger (appended to
      mcp/.deploy-reports/vulture.summary.txt) — never hidden.
  Any finding matching NEITHER surface = a bug (hard-fail, as always).

EXACT-MATCH LEDGER (r0b1 BLOCK, 2026-09-28). This file used to be a list of
BARE NAMES fed to vulture as source. Vulture matches by NAME, so one debt line
for a generic name (install, tighten, add_next, of_scope, cached_at, ...)
marked EVERY same-named symbol in the package as used — an unrelated dead
symbol could hide behind someone else's debt. Now:
  * this file is a DATA module and is NEVER passed to vulture. Gate 1d scans
    the production roots + the allowlist only, RAW;
  * each record is keyed by (path, symbol, kind) — `path` relative to mcp/
    with forward slashes, `kind` = vulture's own word (method / function /
    class / variable / attribute / property / import). Line numbers are NOT
    part of the key;
  * mcp/scripts/vulture_gate.py post-classifies the raw findings and FAILS
    CLOSED when a finding matches no record exactly, when a record matches
    zero findings (moved / renamed / now consumed — graduate or update it),
    or when a record matches more than one finding.
So a record can no longer outlive its symbol silently: the commit that wires
the consumer (direction=add) or ships the deletion (direction=remove) MUST
delete the record, or Gate 1d fails on the zero-match.

THE DEBT ITSELF LIVES IN THE BACKLOG (operator ruling 2026-07-25: "move
everything from vulture debt into backlog"). Each record is a POINTER: the
evidence, the reason the absence is intentional, the named coming consumer
and the done-when all live in the `owner` backlog item. The record dies in
the SAME commit that closes its backlog item.

Read by mcp/scripts/vulture_gate.py and mcp/scripts/vulture_allowlist_classify.py
via ast.literal_eval — never imported, never executed by runtime code.
"""

# ── history notes (kept: they are the reasoning behind the records below) ──
#
# #1030 blue/green runtime generations: the four #1030 entries that stood here
# died with the commit that gave them production consumers — runtime_provisioner
# now builds, seals and activates generations, exactly as direction=add promised.
#
# RESOLVED 2026-09-26 by #1074 (entry removed): `GateBindingStore.set_session`
# is no longer vestigial. #526 recorded the session dimension of the old
# user-wide binding as written empty and never read. #1074 made the binding
# per-(user_id, surface_ref) and the SOLE execution-context authority, so the
# session now LIVES there: GateProjectStore.select_session and
# .restore_session_selection write it through set_session, and
# GateProjectStore.resolve_selection reads it.
#
# evict_all_projects — PREMISE CORRECTED 2026-08-28, direction FLIPPED add ->
# remove. Slice 2 landed and drains via `client.evict(project_root)`,
# per-project, never through this door-level helper, so no consumer is coming.
# BUT IT IS NOT DELETABLE: test_lsp_door_fail_open.py:20-26 uses it in an
# AUTOUSE TEARDOWN FIXTURE to clear the module-level _POOL before and after
# every test; `client.evict(project_root)` drops ONE project's server and
# cannot substitute. The classification is genuinely awkward: vulture is RIGHT
# (not an allowlist false positive), "consumed only by tests" is NOT grounds for
# the allowlist (see active_keys), and no consumer is coming. It is TEST-ONLY
# INFRASTRUCTURE; direction=remove stands, blocked on a test-isolation substitute.
#
# invalidate_operator: ENTRY RETIRED 2026-08-27 (#529 step 3). The tracked
# consumer LANDED — IdentityStore.validate_token now executes the revocation on
# an affirmative REVOKED verdict.
#
# REVOCATION_LIVE: ENTRY RETIRED 2026-09-28 (exact-debt migration). MEASURED:
# raw vulture (without the debt file) no longer reports it — identity_store.py
# now reads it in production (the live-probe verdict at :417, :514, :534). A
# record for it would match zero findings and fail Gate 1d, as it should.
#
# ground: OrgAdminVerdict.ground (#630 instance 1, landed ba372a6c1). Vulture is
# RIGHT — tests read it, no PRODUCTION code does. NOT the allowlist. The old
# bare-name caveat ("`ground` masks any OTHER unused variable of that name") is
# CURED by the exact (path, symbol, kind) key.
#
# request_runtime_refresh: ENTRY RETIRED 2026-08-28 (#575 producer half) —
# aidocs_service.py calls it from production (#868 wired it).
#
# KIND_ORDER: snapshot statistic removed 2026-08-28 (a debt entry citing a
# live-changing count is guaranteed to become false; the count belongs in #573).
#
# issues_strike / freezes_agent / agent_cancellable: ENTRIES RETIRED 2026-08-28
# (#574) — consumed by refusal_explainer.py (the ai_gate_explain builder).
# WATCH THE NAME COLLISION: `is_security_class` is consumed there;
# `freeze_is_security_class` is a DIFFERENT function and nothing consumes it.
#
# #1039 S2 late-result cache: vulture is RIGHT about the instruments — test-only,
# no production consumer — so they are debt, not false positives.
# `attached` (tool_latency.py LateCallStats field, backlog #1044): NO RECORD.
# MEASURED 2026-09-28: raw vulture does not report it, because vulture matches by
# NAME and heuristic_judge.py reads an unrelated local called `attached`. The
# counter is still written on every late_attach and never read (the #1042b blind
# spot is still open — tracked in #1044), but vulture cannot see it, so an exact
# record would match zero findings. Vulture's own name matching, not this ledger,
# is what hides it now.
#
# native-lease lifecycle (#1082): the per-call release landed, the session sweep
# did not. Native leases carry NO pid and NO heartbeat, so a host that dies
# mid-tool leaves a `native:` lease blocking `ai_git switch` for the whole TTL;
# Stop/SubagentStop is the evidence the sweep needs and it already arrives at a
# handler that ignores it.
#
# RFC 0003 application packs (#1109): slices S1-S15 landed UNWIRED on main, and
# the gate wiring (branch crm-demo-m1, r0b2 GREEN) is now merged. Its 15
# consumed records died in that merge; the records below name later slices.

# Pure literals only: the readers use ast.literal_eval (no names, no calls).
FUTURE_DEBT = (
    # ── #1134 Phase B slice 1 ──
    {
        "path": "server/aidocs_mcp/app_packs/link.py",
        "symbol": "authorize_page",
        "kind": "method",
        "direction": "remove",
        "owner": "#1148",
        "note": "retired §8 Profilo link: the routes answer 410 app_link_retired since Phase B; "
        "LinkService and its tests are deleted together in the slice-1 debt follow-up",
    },
    {
        "path": "server/aidocs_mcp/app_packs/link.py",
        "symbol": "authorize_confirm",
        "kind": "method",
        "direction": "remove",
        "owner": "#1148",
        "note": "retired §8 Profilo link (see authorize_page); deleted with LinkService",
    },
    {
        "path": "server/aidocs_mcp/app_packs/generation_attestation.py",
        "symbol": "current_kid",
        "kind": "property",
        "direction": "add",
        "owner": "#1148",
        "note": "the attestation-key rotation caller (stage/promote) reads the current kid; "
        "tests pin it today",
    },
    # ── core ──
    {
        "path": "server/aidocs_mcp/agent_memory_epoch.py",
        "symbol": "require_epoch",
        "kind": "function",
        "direction": "add",
        "owner": "#525",
        "note": "fail-CLOSED epoch guard with no production consumer; flip dnt_banner/"
        "helper_skill/read_memory_surfacer/agent_audit off fail-open current_epoch",
    },
    {
        "path": "server/aidocs_mcp/lsp/client.py",
        "symbol": "evict_all_projects",
        "kind": "function",
        "direction": "remove",
        "owner": "#527",
        "note": "test-only pool reset; Slice 2 landed via client.evict(project_root), but "
        "deleting this breaks test isolation until a substitute exists",
    },
    {
        "path": "server/aidocs_mcp/outer_gate_project_acl.py",
        "symbol": "ground",
        "kind": "variable",
        "direction": "add",
        "owner": "#630",
        "note": "named grounds exist so an admission can say WHY and a refusal can name "
        "the two facts that disagreed; the refusal-render consumer is instance 2's work",
    },
    {
        "path": "server/aidocs_mcp/sticky_grants_store.py",
        "symbol": "active_bash_subcommands_for_session",
        "kind": "method",
        "direction": "remove",
        "owner": "#530",
        "note": "went dead when the sticky bash union was removed from prompt_mutator; "
        "dies with sticky_grants_store",
    },
    {
        "path": "server/aidocs_mcp/outer_gate_sandbox.py",
        "symbol": "validate_worker_token",
        "kind": "method",
        "direction": "add",
        "owner": "#532",
        "note": "sandbox worker-token capability check for the remote-agent relay (#180), "
        "which does not exist yet. Moved here from vulture_allowlist.py: it had zero "
        "references INCLUDING tests, so 'false positive' was the wrong classification "
        "for a safety-floor validator.",
    },
    {
        "path": "server/aidocs_mcp/hook_broker.py",
        "symbol": "active_keys",
        "kind": "method",
        "direction": "add",
        "owner": "#489",
        "note": "EvalGate key-registry count. Production ENFORCES the bound (prune "
        "self._keys against _KEY_REGISTRY_MAX) but nothing READS the count; the coming "
        "consumer is the broker health surface (health['hook_broker'] should report the "
        "live session-key count). Test-only consumption today, and per "
        "test_operator_invalidation.py 'consumed only by tests' is NOT grounds for the "
        "allowlist, hence future debt, not fp.",
    },
    {
        "path": "server/aidocs_mcp/project_backlog_store.py",
        "symbol": "KIND_ORDER",
        "kind": "variable",
        "direction": "add",
        "owner": "#573",
        "note": "canonical ordering for the five `kind` values, for the severity x kind "
        "triage grid. The consumer is the ranked/grouped backlog surface that has not "
        "landed; nothing sorts by kind yet.",
    },
    {
        "path": "server/aidocs_mcp/verdict_class.py",
        "symbol": "freeze_is_security_class",
        "kind": "function",
        "direction": "add",
        "owner": "#574",
        "note": "the conductor-clears-its-subagent ladder is the named consumer: it must "
        "ask 'is this freeze security-class?' and refuse to clear if so. Blocked on #571 "
        "GAP A: rung 2 still freezes, so strikes and freezes remain fused.",
    },
    # ── #1039 S2 late-result cache (#1044) ──
    {
        "path": "server/aidocs_mcp/tool_latency.py",
        "symbol": "late_status",
        "kind": "function",
        "direction": "add",
        "owner": "#1044",
        "note": "'none'|'running'|'finished' for a parked call; test-only today. Coming "
        "consumer: a late-cache health surface reporting attach/park counts.",
    },
    {
        "path": "server/aidocs_mcp/tool_latency.py",
        "symbol": "remaining_seconds",
        "kind": "function",
        "direction": "add",
        "owner": "#1044",
        "note": "the documented extension point for implementations that return PARTIAL "
        "results before the hard kill. No tool returns partials yet, so nothing calls it.",
    },
    # ── native-lease lifecycle (#1082) ──
    {
        "path": "server/aidocs_mcp/branch_transition_barrier.py",
        "symbol": "release_native_session_leases",
        "kind": "function",
        "direction": "add",
        "owner": "#1082",
        "note": "sweep every `native:%` lease of a host session when its Stop/SubagentStop "
        "arrives; claude_hook's Stop arm is the named, already-present consumer.",
    },
    # ── RFC 0003 application packs (#1109) — merged from crm-demo-m1, not yet consumed ──
    {
        "path": "server/aidocs_mcp/app_packs/authority_db.py",
        "symbol": "verify_decisions",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "durable decision-log integrity check (hash chain over authority decisions); "
        "consumer = the S18 Connected Apps admin health surface",
    },
    {
        "path": "server/aidocs_mcp/app_packs/authority_stores.py",
        "symbol": "verification_keyset",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "durable org-signer public keyset for the CRM's verification fetch; "
        "consumer = the S13 published-keyset route",
    },
    {
        "path": "server/aidocs_mcp/app_packs/host_file_ingress.py",
        "symbol": "spool_path",
        "kind": "property",
        "direction": "add",
        "owner": "#1109",
        "note": "spooled upload location for the S16 relay; consumer = the ingress "
        "janitor that sweeps abandoned spools",
    },
    {
        "path": "server/aidocs_mcp/app_packs/link_rate.py",
        "symbol": "tracked_keys",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "live key count of the S8 link rate limiter (bounded by max_keys); "
        "consumer = the gate health surface. Test-only today.",
    },
    {
        "path": "server/aidocs_mcp/app_packs/pairing_rate.py",
        "symbol": "tracked_keys",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "live key count of the S18 §5.8 pairing rate limiter (bounded by max_keys), "
        "twin of link_rate.tracked_keys; consumer = the gate health surface. Test-only today "
        "(tests/app_packs/test_app_pairing_rate.py).",
    },
    # ── #1109 still unconsumed on crm-demo-m1 — each names its later slice ──
    {
        "path": "server/aidocs_mcp/app_packs/app_freeze.py",
        "symbol": "admin_clear",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S11 admin freeze clear; consumer = S18 Connected Apps control-plane route",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_freeze.py",
        "symbol": "of_scope",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S11 FreezeTarget constructor; consumer = the S18 admin freeze act / R-c "
        "strike producer",
    },
    {
        "path": "server/aidocs_mcp/app_packs/seat_pin.py",
        "symbol": "admin_reset",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S6 seat-pin admin reset; consumer = S18 Connected Apps pin reset",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_policy.py",
        "symbol": "tighten",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S11 R-h tenant policy tightening; consumer = S18 policy UI",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_policy.py",
        "symbol": "rate_limits",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S11 R-h policy -> S10 limiter; consumer = the policy read in admission "
        "once R-h has a UI",
    },
    {
        "path": "server/aidocs_mcp/app_packs/binding_store.py",
        "symbol": "flip_org_signer",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S3 key rotation flip; consumer = the rotation control-plane act",
    },
    {
        "path": "server/aidocs_mcp/app_packs/signer.py",
        "symbol": "verification_keyset",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S3 public keyset for the CRM; consumer = the rotation / JWKS publication act",
    },
    {
        "path": "server/aidocs_mcp/app_packs/entitlements.py",
        "symbol": "cached_keys",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S9 keyset cache read; consumer = the S18 binding status view",
    },
    {
        "path": "server/aidocs_mcp/app_packs/entitlements.py",
        "symbol": "refresh_after_stale",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "section 7.6 one refresh on crm_grants_stale; consumer = runtime admission "
        "(known M1 gap)",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_audit_ledger.py",
        "symbol": "high_water",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S12 ledger backup manifest; consumer = the gate-host backup job "
        "(production gate)",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_audit_ledger.py",
        "symbol": "prune_operational",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S12 OPERATIONAL retention; consumer = the retention sweep (production gate)",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_audit_ledger.py",
        "symbol": "backup_to",
        "kind": "method",
        "direction": "add",
        "owner": "#1109",
        "note": "S12 online backup; consumer = the gate-host backup job (production gate)",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_audit_ledger.py",
        "symbol": "egress_permitted",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "S12 ledger row field; consumer = the audit read surface",
    },
    {
        "path": "server/aidocs_mcp/app_packs/app_audit_ledger.py",
        "symbol": "freshness_claimed",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "S12 ledger row field; consumer = the audit read surface",
    },
    {
        "path": "server/aidocs_mcp/app_packs/bundle.py",
        "symbol": "external_effect",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "section 6.3.2 branch effect metadata; consumer = production-gate "
        "admission policy",
    },
    {
        "path": "server/aidocs_mcp/app_packs/bundle.py",
        "symbol": "downstream_irreversible",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "section 6.3.2 branch effect metadata; consumer = production-gate "
        "admission policy",
    },
    {
        "path": "server/aidocs_mcp/app_packs/bundle.py",
        "symbol": "prior_state_retention",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "section 6.3.2 retention metadata; consumer = S19 recovery / undo surface",
    },
    {
        "path": "server/aidocs_mcp/app_packs/entitlements.py",
        "symbol": "cached_at",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "S9 cache entry field; consumer = the S18 entitlement status view",
    },
    {
        "path": "server/aidocs_mcp/app_packs/pairing.py",
        "symbol": "aidocs_nonce",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "wire section 3 pairing offer field; consumer = the S18 pairing screen that "
        "displays the offer",
    },
    {
        "path": "server/aidocs_mcp/app_packs/write_path.py",
        "symbol": "audit_complete",
        "kind": "variable",
        "direction": "add",
        "owner": "#1109",
        "note": "CallOutcome flag; consumer = the outcome surface that reports a degraded "
        "post-fact audit",
    },
    # ── #1074 round 6 native credential: staged or test-only ──
    {
        "path": "server/aidocs_mcp/native_keystore.py",
        "symbol": "KeyringBackend",
        "kind": "class",
        "direction": "add",
        "owner": "#1074",
        "note": "non-Windows native key custody; native_keystore.py refuses off Windows "
        "until this is flipped on",
    },
    {
        "path": "server/aidocs_mcp/outer_gate_oauth.py",
        "symbol": "set_native_enrollment",
        "kind": "method",
        "direction": "add",
        "owner": "#1074",
        "note": "the operator act that grants a client the native_enrollment capability; "
        "test-only today, the operator CLI is the coming consumer",
    },
    {
        "path": "server/aidocs_mcp/webmcp_identity.py",
        "symbol": "compose_host_session_id",
        "kind": "function",
        "direction": "remove",
        "owner": "#1074",
        "note": "superseded by compose_host_identity; only tests call it now, and it dies "
        "when they move",
    },
    {
        "path": "server/aidocs_mcp/lifecycle_service.py",
        "symbol": "record_agent_handoff",
        "kind": "method",
        "direction": "remove",
        "owner": "#1121",
        "note": "its only production caller was openai_agents_adapter.on_handoff, retired in "
        "5121193be; only test_lifecycle_service_parity calls it now, and it dies with them",
    },
    {
        "path": "server/aidocs_mcp/claude_hook.py",
        "symbol": "orchestrator",
        "kind": "attribute",
        "direction": "remove",
        "owner": "#1121",
        "note": "ClaudeHookHandler sets it but production never reads it; the retired "
        "openai_agents_adapter's self.orchestrator reads masked that by name. Tests still "
        "assign/read it, so it goes when they stop",
    },
)
