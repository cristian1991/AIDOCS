"""Vulture allowlist — INTENTIONAL dead-code references.

Doctrine: vulture flags every unreachable branch. Some branches are
          deliberately disabled (staged migrations, controller skeletons
          retained for trace shape) and should NOT be touched until the
          migration that owns them finishes. List those here so the
          gate stays high-signal — vulture's REAL findings (typos,
          indentation bugs like mcp_server.py:738 that we just fixed)
          shouldn't drown in known-intentional noise.
Why:      every entry needs a comment naming the OWNER ticket / lane.
          When a migration lands, its allowlist entries get deleted in
          the SAME commit. The list rots otherwise.
Usage:    vulture mcp/server/aidocs_mcp mcp/vulture_allowlist.py

Classification invariant (#427/#426): every entry sits under a section-scoped
`# @vulture-class: <fp|foundation|dead-pending|legacy>` marker:
  fp           false positive — the symbol IS consumed (tests outside vulture
               scan roots, dynamic dispatch/getattr, framework hooks, registry
               scan); the entry names its consumer.
  foundation   deliberately retained with a declared owner + wire/retire plan
               (staged migration skeletons, doctrine seams, regression guards).
  dead-pending verified zero-reference dead code whose deletion is queued
               behind owner sign-off (safety-floor surfaces) or a surgical
               same-file rewrite. Shrinks toward 0.
  legacy       grandfathered #330 bulk not yet re-proven; COUNT IS PINNED and
               may only go DOWN (test_vulture_allowlist_classification.py).
An entry under NO marker is a BUG — mcp/scripts/vulture_allowlist_classify.py
exits non-zero and the deploy vulture lane fails loud. Genuine future debt
(unwired-on-purpose, with an add-or-remove marker) lives in
mcp/vulture_future_debt.py, never here.

This file is consumed by vulture only; never imported by runtime code.
"""

# @vulture-class: foundation
# ── enforcement_pkg controller migration (Lane 2 phase 1) ──
# Dead-skeleton fallback in controller.py — `enforce_via_legacy(request)`
# returns first; the Decision(...) block below is retained as a typed
# template for the next phase. Delete when controller is fully wired.
Decision  # noqa: F821


# ── agent_orchestrator dev-mode kill-switch trace (castle law 2026-05-04) ──
# `if False: _sec012_trace.add(...)` branch preserves trace shape during
# the migration that replaces this whole try/except. Delete when migration
# lands and the kill_switch path moves into the controller.
_sec012_trace  # noqa: F821


# @vulture-class: fp
# ── host_operator_binding_store.restamp_window — OPERATOR-INVOKED, by design ──
# Owner: #1024 / #1025 (the window identity spine).
# It has no in-tree caller and MUST NOT acquire one. It is the migration
# command for the bindings `adopt_window` cannot reach: rows approved before
# the window column existed, whose conversation rotated before the lease table
# existed, so NO ATTESTED FACT connects the old conversation to the live
# window. Automatic adoption correctly refuses them; widening it to "any
# approved row for this user" would hand any window the newest binding in the
# store — the exact substitution that programme exists to remove. A human
# naming both ids is evidence; a resolver guessing between them is the bug, so
# the only caller is an operator at a shell and a production call site would be
# a defect rather than a fix.
# CONSUMERS (outside vulture's ship-stage scan roots):
#   mcp/tests/security/test_operator_who_survives_conversation_rotation.py (x3)
# Retire when every pre-window binding is migrated and the method is deleted.
restamp_window  # noqa: F821


# @vulture-class: fp
# ── ai_deploy_daemon back-compat param ──
# `deploy_runner` is asserted directly by test_ai_deploy_daemon.py for
# back-compat callers. Keep until that test is retired.
deploy_runner  # noqa: F821


# @vulture-class: fp
# ── #850-adjacent: failure-ledger store seeding, test-only since 741ebc10e ──
# CONSUMERS (all outside vulture's ship-stage scan roots):
#   mcp/tests/security/test_failure_stewardship_persistence.py (x3),
#   _673.py (x2), _rerun_green.py, _disposition_surface.py,
#   test_ai_failures_list_does_not_dump_the_ledger_852.py,
#   test_failure_duty_identity_axis.py,
#   mcp/tests/runtime/test_stores_are_wal_746.py
#
# THE FINDING IS CORRECT, NOT NOISE. Until 741ebc10e every ledger write was
# `load_ledger(...)` ... mutate ... `save_ledger(...)`. That pair is a
# read-modify-write race against a FULL REPLACE, and it was measured eating a
# real disposition — the row reverted to untriaged and the next autoclear
# absolved it on a green run it was never part of. Every production path now
# goes through `mutate_ledger`, so mcp/server/** genuinely has zero callers.
#
# WHY IT STAYS: seeding a store from a ledger built in memory is a DIFFERENT
# operation, not a degenerate case of the safe one. There is no prior read, so
# there is no update to lose, and `_persist(snapshot=None)` is the honest
# spelling of "I own this whole file". Making tests open a write transaction to
# lay down a fixture would be ceremony that buys nothing.
#
# WHY IT IS FLAGGED HERE RATHER THAN JUST DELETED: it is a loaded primitive kept
# for convenience — its own docstring says "Never pass None from a path that
# READ the store first — that is the lost update." An allowlist entry is the
# right place for that warning to be visible to whoever reaches for it next.
#
# RETIRE WHEN: the test fixtures seed through `mutate_ledger` and this function
# is deleted with them, in the same commit as this entry.
save_ledger  # noqa: F821


# @vulture-class: fp
# ── #738 process-stamp test seam ──
# CONSUMERS: mcp/tests/host/test_process_stamp_738.py and
# test_ai_version_running_identity_738.py both call
# reset_process_stamp_for_test() to simulate a process boot. Vulture scans the
# SHIP STAGE (mcp/server/** without mcp/tests/**), so from the artefact's point
# of view a test-only helper genuinely IS unreferenced — the finding was
# CORRECT, not noise.
# WHY IT STAYS: the alternative is tests poking the module's private _STAMP
# global directly, which is a worse contract than a named seam.
# WHY IT IS SAFE: it is GUARDED (process_stamp.py) — it raises RuntimeError
# unless pytest is loaded. That guard exists because #738's whole value is that
# the stamp is taken ONCE and cannot be re-taken; an unguarded reset would let
# any caller re-stamp at a later HEAD and claim a commit the process never
# imported — #738's own defect, reproducible from shipped code. The guard is
# pinned by test_the_test_seam_refuses_outside_a_test_run.
# Delete this entry if the seam is ever removed from the shipped package.
reset_process_stamp_for_test  # noqa: F821


# @vulture-class: foundation
# ── co_conductor deferred stub API ──
# `cot_excerpt` is part of the co-conductor-deferred stub API surface.
# Keep until the stub is implemented or removed.
cot_excerpt  # noqa: F821


# @vulture-class: foundation
# ── #561 phase 2: the pre-phase-2 parity baseline ──
# `evaluate_provider` is the entry point phase 2 REPLACED with the dialect
# dispatch. It is retained deliberately as the REGRESSION-GUARD TARGET that
# proves the replacement changed no verdicts: the negative control at
# mcp/tests/security/test_shell_dialect_follows_interpreter.py:164 asserts
# evaluate_provider(provider, cmd) == evaluate_dialect(...) across every
# provider-by-command pair. It has NO production caller by design — the whole
# point is that it is the old answer, kept to compare against the new one.
# RETIRE-BY: delete it when #561 phase 4 lands and the dialect dispatch is the
# only path. Dies with its plan.
evaluate_provider  # noqa: F821


# @vulture-class: fp
# ── #561 phase 3: the discovery pass's attestation standing ──
# Phase 3's scope is a single discovery pass "returning eligibility + reason +
# attestation (+ dialect)". `ShellCandidate.attestation` and the
# `KNOWN_ATTESTATIONS` vocabulary it is drawn from are that third return value.
# Both ARE consumed — by the seam tests, which live outside vulture's scan root:
#   mcp/tests/security/test_shell_candidate_registry.py:234
#       test_every_candidate_reports_an_attestation_standing
#   mcp/tests/security/test_shell_candidate_registry.py:246
#       test_ineligible_candidates_are_never_attested (invariant 2's seal)
#   mcp/tests/security/test_shell_candidate_registry.py:258
#       test_attestation_is_a_standing_never_material
# NOT a migration scaffold, so not `foundation` and no retire-by: it is a
# permanent part of the pass's contract. Its remaining reader is the UI half of
# #171 bullet 4 (render the detected-not-eligible rows in GovernedBashPanel),
# which phase 3 deliberately does not build. Delete these two entries when that
# panel reads the field.
attestation  # noqa: F821
KNOWN_ATTESTATIONS  # noqa: F821


# @vulture-class: fp
# ── claude_hook.ClaudeHookHandler user-intent phrase aliases ──
# These class attrs are back-compat aliases (identity-spine rip,
# 2026-07-06) read directly by test_gate_flow_injection_resistance.py.
# Keep until that test is migrated to canonical_intent_registry.
_DIRECT_INTENT_PHRASES  # noqa: F821
_GRANT_PROXIMITY  # noqa: F821
_GRANT_VERB_PHRASES  # noqa: F821
_TOOL_TOKEN_PATTERNS  # noqa: F821


# ====================================================================
# Generated sections below: 2026-07-12 vulture-clean sweep (#330 hard-fail
# prerequisite). Evidence: repo-wide token scan over mcp/, webapp/,
# dashboard/, scripts/, third_party/ (code files, excluding reports/docs).
# Owner: conductor #330. Doctrine unchanged: delete an entry in the same
# commit that removes (or starts truly using) its symbol.
# ====================================================================

# @vulture-class: fp
# ── Individually-reviewed entries (2026-07-12 vulture-clean sweep, #330) ──
_.row_factory  # noqa: F821 (attribute @ _sqlite_index_store_base.py:21, agent_audit.py:68, agent_expert_service.py:554 (+123 more)) sqlite3: conn.row_factory = sqlite3.Row is consumed by sqlite itself — always LIVE
drift_warning  # noqa: F821 (function @ public_mirror_drift.py:116, webapp_bundle_drift.py:111) deploy tooling — called by mcp/scripts/check_webapp_bundle.py + check_public_mirror.py + security tests
matched_policy  # noqa: F821 (variable @ tool_policy.py:46) result-dataclass field populated at construction (tool_policy.py:119) for callers — API surface
is_freezing_severity  # noqa: F821 (function @ violation_severity.py:303) security API — asserted by mcp/tests/security/test_violation_severity_tiers.py + test_sensitive_read_gentle_first_strike.py
CodenexusIdentityResolver  # noqa: F821 (class @ webmcp_authz.py:68) deliberate API seam: codenexus identity resolver Protocol, wired when operator picks service-mode auth mechanism (webmcp service-mode plan)
UnconfiguredResolver  # noqa: F821 (class @ webmcp_authz.py:79) deliberate fail-closed default resolver for the codenexus seam above (webmcp service-mode plan)
count_active  # noqa: F821 (function @ workflow_definitions_store.py:238) used by mcp/tests (test_workflow_definitions_store.py, test_project_init_workflow_files.py)

# @vulture-class: fp
# ── Framework/protocol callbacks — invoked by external libraries, never by our code ──
_.__annotations__  # noqa: F821 (attribute @ mcp_server.py:6642) framework hook: dunder consumed by typing/introspection
_.on_llm_start  # noqa: F821 (method @ openai_agents_adapter.py:219) framework hook: openai-agents RunHooks callback
_.on_llm_end  # noqa: F821 (method @ openai_agents_adapter.py:222) framework hook: openai-agents RunHooks callback
_.on_start  # noqa: F821 (method @ openai_agents_adapter.py:250) framework hook: openai-agents AgentHooks callback
_.on_end  # noqa: F821 (method @ openai_agents_adapter.py:253) framework hook: openai-agents AgentHooks callback
_.do_PUT  # noqa: F821 (method @ outer_gate_transport.py:4206) framework hook: http.server BaseHTTPRequestHandler verb hook
_.do_DELETE  # noqa: F821 (method @ outer_gate_transport.py:4209) framework hook: http.server BaseHTTPRequestHandler verb hook
_.do_POST  # noqa: F821 (method @ outer_gate_transport.py:4203) framework hook: http.server BaseHTTPRequestHandler verb hook (#427 audit: reclassified from cross-boundary bulk — sibling of do_PUT/do_DELETE)
daemon_threads  # noqa: F821 (variable @ outer_gate_transport.py:4213) framework hook: socketserver.ThreadingMixIn tuning attribute (#427 audit: reclassified — sibling of request_queue_size)
request_queue_size  # noqa: F821 (variable @ outer_gate_transport.py:4214) framework hook: socketserver.TCPServer tuning attribute
_.on_any_event  # noqa: F821 (method @ project_index_sitter.py:392) framework hook: watchdog FileSystemEventHandler hook
_.on_list_tools  # noqa: F821 (method @ project_scope.py:171) framework hook: MCP middleware hook (host calls it)

# @vulture-class: fp
# ── Test-covered API — referenced only from mcp/tests (outside vulture scan roots) ──
KNOWN_DIALECTS  # noqa: F821 (variable @ shell_provider_dialect.py:49) the closed set of shell grammars a resolved binary may be tagged with (#561 phase 2). Consumed by mcp/tests/security/test_shell_dialect_follows_interpreter.py:93, which asserts every ResolvedShell.dialect is a member — the assertion that keeps the tag a closed vocabulary rather than free text. ResolvedShell's own field docstring (shell_resolver.py:142) names it as the authority, so the constant is the written contract that docstring points at.
PROJECT_TIER_TABLES  # noqa: F821 (variable @ identity_db.py:80) tier-residency manifest — consumed by mcp/tests/security/test_tier_residency_manifests.py (#528), which creates every named table through the real initializer that owns it and asserts residency from each physical file's sqlite_master. NO RUNTIME CONSUMER BY DESIGN: the only production loop over a manifest (identity_db.py:217) is legacy ADOPTION into the global file, and adopting project-tier tables there is precisely what the tiering forbids.
TENANT_HOME_TIER_TABLES  # noqa: F821 (variable @ identity_db.py:130) sibling of the above — same test, same reason. Worth the entry: this manifest named `gate_project_acl`, a table no CREATE TABLE in the tree ever used (the real one is project_acl @ outer_gate_project_acl.py:77), and the drift went unnoticed for as long as nothing read it. The #528 test is what reads it now.
_invalidate_cache  # noqa: F821 (function @ _dev_trace.py:99)
_.check_write  # noqa: F821 (method @ access_gate.py:1813)
authorize_deploy  # noqa: F821 (function @ ai_deploy_authority.py:113)
_status_of  # noqa: F821 (function @ ai_deploy_daemon.py:30)
validate_request  # noqa: F821 (function @ ai_deploy_daemon.py:69)
run_request  # noqa: F821 (function @ ai_deploy_daemon.py:84)
dashboard_sign_and_deploy  # noqa: F821 (function @ ai_deploy_orchestrate.py:88)
can_advance  # noqa: F821 (function @ ai_deploy_states.py:59)
code_at  # noqa: F821 (function @ ai_deploy_totp.py:62)
detect_protect_grants  # noqa: F821 (function @ dnt_detector.py:45)
detect_unprotect_grants  # noqa: F821 (function @ dnt_detector.py:82)
expanded_terms  # noqa: F821 (function @ search_expander.py:107)
matched_axis  # noqa: F821 (variable @ skill_trigger.py:38)
detect_skill_triggers  # noqa: F821 (function @ skill_trigger.py:223)
reset_service  # noqa: F821 (function @ service.py:506)
stash_king_field  # noqa: F821 (function @ anchor_field.py:293)
turn_index  # noqa: F821 (variable @ anchor_stack.py:46)
has_demonstrative  # noqa: F821 (function @ anchor_stack.py:174)
resolve_demonstrative  # noqa: F821 (function @ anchor_stack.py:184)
dedupe_bundle  # noqa: F821 (function @ bundle_dedup.py:64)
dedup_stats  # noqa: F821 (function @ bundle_dedup.py:106)
CANONICAL_COLUMNS  # noqa: F821 (variable @ canonical_taxonomy.py:300)
EXCLUDED_FROM_CANONICAL_VIEW  # noqa: F821 (variable @ canonical_taxonomy.py:319)
drop_canonical_view  # noqa: F821 (function @ canonical_taxonomy.py:515)
profile_keys  # noqa: F821 (function @ capability_profiles.py:81)
code_teaches_hits  # noqa: F821 (variable @ capture_gate.py:74)
conflicting_paths  # noqa: F821 (variable @ capture_gate.py:77)
_._consume_sticky_grant_answers  # noqa: F821 (method @ claude_hook.py:359)
_._check_reconnect_required  # noqa: F821 (method @ claude_hook.py:767)
_._resolve_session_freeze  # noqa: F821 (method @ claude_hook.py:847)
_._freeze_envelope  # noqa: F821 (method @ claude_hook.py:907)
_._build_tool_discovery_hint  # noqa: F821 (method @ claude_hook.py:1218)
MAX_RUN_TIMEOUT  # noqa: F821 (variable @ code_runner.py:51)
TestResult  # noqa: F821 (class @ code_runner.py:84)
BuildResult  # noqa: F821 (class @ code_runner.py:119)
_detect_build_command  # noqa: F821 (function @ code_runner.py:409)
_detect_test_command  # noqa: F821 (function @ code_runner.py:428)
_parse_test_counts  # noqa: F821 (function @ code_runner.py:464)
_extract_test_failures  # noqa: F821 (function @ code_runner.py:498)
_extract_summary_line  # noqa: F821 (function @ code_runner.py:525)
sweep_orphan_run_artifacts  # noqa: F821 (function @ code_runner_detached.py:180)
known_predicate_names  # noqa: F821 (function @ conditional_predicates.py:176)
evaluate_predicate  # noqa: F821 (function @ conditional_predicates.py:181)
predicate_help  # noqa: F821 (function @ conditional_predicates.py:199)
get_lane_scope  # noqa: F821 (function @ conductor_comms.py:150)
msg_thread  # noqa: F821 (function @ conductor_comms.py:952)
referenced_tool_names  # noqa: F821 (function @ conductor_doctrine.py:83)
_find_config_file  # noqa: F821 (function @ config.py:380)
TOOLS_SYNC_TIMEOUT  # noqa: F821 (variable @ config.py:814)
validate_ledger  # noqa: F821 (function @ control_authority_ledger.py:185)
decode_cursor  # noqa: F821 (function @ cursor_pagination.py:75)
CustomActionRegistry  # noqa: F821 (class @ custom_action_registry.py:47)
_.registered_names  # noqa: F821 (method @ custom_action_registry.py:135)
_.expanded_action_kinds  # noqa: F821 (method @ custom_action_registry.py:139)
render_banner_digest  # noqa: F821 (function @ dnt_header_parser.py:267)
find_dangling_doctrine_refs  # noqa: F821 (function @ doctrine_consistency.py:28)
parse_scroll_sections  # noqa: F821 (function @ doctrine_consistency.py:67)
find_dangling_section_refs  # noqa: F821 (function @ doctrine_consistency.py:72)
surfaced_memories  # noqa: F821 (variable @ edit_memory_gate.py:40)
_.ledger_copy_to_kingdom  # noqa: F821 (method @ empire_audit_store.py:206)
record_audit_critical  # noqa: F821 (function @ audit_critical.py:35)
record_audit_best_effort  # noqa: F821 (function @ audit_critical.py:60)
_.is_degraded  # noqa: F821 (method @ degraded_latch.py:74)
_.reset_for_tests  # noqa: F821 (method @ degraded_latch.py:90)
MCP_TOOL  # noqa: F821 (variable @ surface.py:23)
VALID_STATUSES  # noqa: F821 (variable @ escalation_store.py:43)
_.find_grant_by_id  # noqa: F821 (method @ escalation_store.py:562)
_.list_grants_for_request  # noqa: F821 (method @ escalation_store.py:661)
_.clear_all  # noqa: F821 (method @ execution_index_store.py:1731)
_.assert_seal_allowed  # noqa: F821 (method @ failure_stewardship.py:743)
_.record_full_suite_run  # noqa: F821 (method @ failure_stewardship.py:757)
_.assert_full_suite_allowed  # noqa: F821 (method @ failure_stewardship.py:769)
reset_ledger  # noqa: F821 (function @ failure_stewardship.py:1168)
ensure_watcher  # noqa: F821 (function @ folder_sitter.py:79)
stop_watcher  # noqa: F821 (function @ folder_sitter.py:92)
stop_all_watchers  # noqa: F821 (function @ folder_sitter.py:102)
PendingGrantConfirmation  # noqa: F821 (class @ gate_confirm.py:35)
gate_label  # noqa: F821 (variable @ gate_confirm.py:49)
gate_permissions  # noqa: F821 (variable @ gate_confirm.py:50)
explicit_confirm_required  # noqa: F821 (function @ gate_confirm.py:55)
decision_blocked_by  # noqa: F821 (variable @ gate_tool.py:243)
merge_git_sync  # noqa: F821 (function @ git_origin_drift.py:195)
retire_global_law  # noqa: F821 (function @ global_law_store.py:164)
IN_PROCESS_EGRESS_FINGERPRINTS  # noqa: F821 (variable @ governed_egress.py:29)
KNOWN_NON_EGRESS  # noqa: F821 (variable @ governed_egress.py:58)
authority_ok  # noqa: F821 (function @ governed_shell_approval_store.py:261)
approvals_available  # noqa: F821 (function @ governed_shell_approval_store.py:351)
serve_unix  # noqa: F821 (function @ governed_shell_broker.py:399)
register_host_capability  # noqa: F821 (function @ host_capabilities.py:112)
_.list_live  # noqa: F821 (method @ host_concurrency_store.py:363)
evaluate_command_output  # noqa: F821 (function @ output_redaction_policy.py:130)
format_suggestion  # noqa: F821 (function @ tool_discovery_hint.py:63)
supports_user_prompt_submit  # noqa: F821 (function @ host_support_matrix.py:317)
_.delete_memory_route  # noqa: F821 (method @ index_store.py:507)
_.get_memory_route  # noqa: F821 (method @ index_store.py:534)
_.get_memory_anchors  # noqa: F821 (method @ index_store.py:681)
nlp_backend_status  # noqa: F821 (function @ intent_grant_detector.py:1390)
check_intent  # noqa: F821 (function @ intent_guard.py:430)
scan_for_injection  # noqa: F821 (function @ intent_guard.py:500)
classify_fallback  # noqa: F821 (function @ intent_router.py:70)
known_action_kinds  # noqa: F821 (function @ intent_router.py:87)
replace_parent_rows  # noqa: F821 (function @ intent_tokens_store.py:635)
ALL_CLASSES  # noqa: F821 (variable @ judge_taxonomy.py:63)
DECISION_ASK  # noqa: F821 (variable @ judge_taxonomy.py:88)
ALL_DECISIONS  # noqa: F821 (variable @ judge_taxonomy.py:91)
get_strike_count  # noqa: F821 (function @ judge_taxonomy.py:451)
definition_source_for_language  # noqa: F821 (function @ language_descriptors.py:785)
_.read_user_section  # noqa: F821 (method @ managed_file_service.py:22)
_.rewrite_managed_section  # noqa: F821 (method @ managed_file_service.py:31)
_._aidocs_test_hub  # noqa: F821 (attribute @ mcp_server.py:1230)
_._boot_self_test  # noqa: F821 (attribute @ mcp_server.py:1294, mcp_server.py:1321, mcp_server.py:1376)
_auto_register_related_project_wrappers  # noqa: F821 (function @ mcp_server.py:6309)
find_git_root  # noqa: F821 (function @ mcp_server_runtime_helpers.py:184)
resolve_authoritative_session_id  # noqa: F821 (function @ mcp_server_runtime_helpers.py:519)
_memory_tree_mtime_ns  # noqa: F821 (function @ memory_discovery.py:193)
requires_seal  # noqa: F821 (variable @ memory_scope_classifier.py:51)
classify_scope  # noqa: F821 (function @ memory_scope_classifier.py:59)
recent_palace_retirement_lag_events  # noqa: F821 (function @ memory_sqlite_store.py:217)
mark_superseded  # noqa: F821 (function @ memory_sqlite_store.py:455)
mark_removed  # noqa: F821 (function @ memory_sqlite_store.py:490)
install_nlp_extras  # noqa: F821 (variable @ nlp_install.py:13)
normalize_grant_phrase  # noqa: F821 (function @ non_english_grant.py:68)
known_languages  # noqa: F821 (function @ non_english_grant.py:98)
_.on_handoff  # noqa: F821 (method @ openai_agents_adapter.py:225)
create_aidocs_hooks  # noqa: F821 (function @ openai_agents_adapter.py:263)
TAURI_USER_SAFE_MUTATIONS  # noqa: F821 (variable @ operator_auth_service.py:261)
unintrospectable_commands  # noqa: F821 (function @ operator_auth_service.py:345)
read_only_mutation_offenders  # noqa: F821 (function @ operator_auth_service.py:373)
mutating_cli_commands  # noqa: F821 (function @ operator_auth_service.py:404)
TAURI_DIRECT_SQLITE_MUTATIONS  # noqa: F821 (variable @ operator_auth_service.py:426)
MUTATION_PERMISSIONS  # noqa: F821 (variable @ operator_intent_resolver.py:243)
READ_PERMISSIONS  # noqa: F821 (variable @ operator_intent_resolver.py:248)
SAFE_MUTATION_EXCEPTIONS  # noqa: F821 (variable @ operator_intent_resolver.py:255)
CONFIG_BACKED_SERVICE_ROUTES  # noqa: F821 (variable @ operator_intent_resolver.py:268)
is_deprecated  # noqa: F821 (function @ operator_surface.py:347)
validate_catalog  # noqa: F821 (function @ operator_surface.py:990)
cross_kind_collisions  # noqa: F821 (function @ outer_gate.py:51)
invokable_now_names  # noqa: F821 (function @ outer_gate.py:1216)
discoverable_eligible_names  # noqa: F821 (function @ outer_gate.py:1222)
EditConfirmStore  # noqa: F821 (class @ outer_gate_edit.py:187)
tier_m_execution_authorized  # noqa: F821 (function @ outer_gate_execution_authority.py:61) owner=#461 end_state=production-caller-or-deleted — #461 WAR O cause B: ZERO production callers; every reference is its own definition plus tests/security/test_outer_gate_execution_authority.py. It sits under `fp`, the class that asserts VULTURE IS WRONG, while the repo's own ruling (vulture_future_debt.py: "consumed only by tests is NOT grounds for the allowlist" for a safety-floor validator) says otherwise. Done-when: an execution path calls it, or the symbol and this entry die in one commit.
VALID_TIERS  # noqa: F821 (variable @ outer_gate_manifest.py:48)
VALID_DATA_SCOPES  # noqa: F821 (variable @ outer_gate_manifest.py:49)
KNOWN_DUPLICATES  # noqa: F821 (variable @ outer_gate_manifest.py:58)
discover_registered_tools  # noqa: F821 (function @ outer_gate_manifest.py:715)
manifest_mcp_names  # noqa: F821 (function @ outer_gate_manifest.py:893)
remote_manifest  # noqa: F821 (function @ outer_gate_manifest.py:900)
FORBIDDEN_WORKER_ENV  # noqa: F821 (variable @ outer_gate_sandbox.py:46)
reap_stale_workers  # noqa: F821 (function @ outer_gate_sandbox.py:340)
minted_by  # noqa: F821 (variable @ outer_gate_token_store.py:115)
decline_pending  # noqa: F821 (function @ pending_durable_writes_store.py:171)
ALL_PERMISSION_NAMES  # noqa: F821 (variable @ permission_catalog.py:144)
is_plan_mode_on_phrase  # noqa: F821 (function @ plan_mode_detector.py:81)
is_plan_mode_off_phrase  # noqa: F821 (function @ plan_mode_detector.py:86)
lane_aware_plan_fixture_markdown  # noqa: F821 (function @ plan_template.py:170)
RuleClass  # noqa: F821 (variable @ preflight_prompt_judge.py:84)
_.reconcile_now  # noqa: F821 (method @ project_index_sitter.py:435)
_._registry_path  # noqa: F821 (method @ project_registry_service.py:23)
forget_session  # noqa: F821 (function @ project_scope.py:110)
_.on_call_tool  # noqa: F821 (method @ project_scope.py:168)
should_start_task  # noqa: F821 (function @ prompt_intent_classifier.py:186)
_.clear_banners_shown_for_conductor  # noqa: F821 (method @ protected_file_registry_store.py:440)
clear_all_protected_state  # noqa: F821 (function @ protected_file_runtime.py:145)
_._sticky_path  # noqa: F821 (method @ query_gate.py:31)
_.get_user_intent_tools_with_provenance  # noqa: F821 (method @ query_gate.py:324)
_.delete_role  # noqa: F821 (method @ rbac_store.py:518)
_.set_pid  # noqa: F821 (method @ session_lane_agents_store.py:196) FALSE POSITIVE, not debt: the ONLY caller is the in-worker JS plugin (core/plugins/lib/gate.js + data/opencode_plugin.js), which reaches it via a `python -c` one-liner alongside the existing set_host_session_id stamp — invisible to a Python-only scanner. It must be stamped from THERE because the spawn paths register the row before launching the CLI and then use blocking subprocess.run, which never exposes a child pid. Pinned by tests/runtime/test_lane_agent_pid_stamp.py.
_.revoke_role_from_user  # noqa: F821 (method @ rbac_store.py:706)
_.set_scoped_role_permission  # noqa: F821 (method @ rbac_store.py:743)
_.set_user_permission_override  # noqa: F821 (method @ rbac_store.py:798)
_.format_for_read_tool_output  # noqa: F821 (method @ read_memory_surfacer.py:821)
evaluate_poll  # noqa: F821 (function @ run_output_gate.py:41)
summarize_run_output  # noqa: F821 (function @ run_output_summary.py:75)
plan_session_status  # noqa: F821 (function @ runtime_plan_authoring_service.py:76)
_.ensure_claude_project_settings  # noqa: F821 (method @ runtime_project_support_service.py:234)
_.repo_summary_short  # noqa: F821 (method @ runtime_project_support_service.py:726)
NOT_BLESSED_YET  # noqa: F821 (variable @ runtime_provisioner.py:162)
_.plan_conductor_verify_full_suite  # noqa: F821 (method @ runtime_service.py:3881)
_.plan_conductor_lane_ownership_history  # noqa: F821 (method @ runtime_service.py:3959)
_.normalize_plan_prose  # noqa: F821 (method @ runtime_service.py:8272)
PALACE_INGEST_TIMEOUT_S  # noqa: F821 (variable @ server_code_tools.py:26)
_run_timeboxed  # noqa: F821 (function @ server_code_tools.py:29)
_file_root_to_glob  # noqa: F821 (function @ server_code_tools.py:607)
_.list_binds  # noqa: F821 (method @ session_project_bind_store.py:232)
_.get_lane_eager_tools_granted  # noqa: F821 (method @ session_query_gate_store.py:1910)
_.set_lane_eager_tools_granted  # noqa: F821 (method @ session_query_gate_store.py:1934)
_.touch_plan_mode_activity  # noqa: F821 (method @ session_query_gate_store.py:1954)
_.resolve_active  # noqa: F821 (method @ session_skill_overlay.py:91)
_.all_session_ids  # noqa: F821 (method @ session_skill_overlay.py:116)
_.scan_lane_plans  # noqa: F821 (method @ session_store.py:392)
is_native_shell_enforced  # noqa: F821 (function @ shell_adapter.py:116)
COMMAND_REACHABILITIES  # noqa: F821 (variable @ shell_egress_service.py:506)
_._aidocs_audit_thread  # noqa: F821 (attribute @ shell_egress_service.py:1090)
_._aidocs_audit_row_id  # noqa: F821 (attribute @ shell_egress_service.py:1091)
evaluate_lifecycle  # noqa: F821 (function @ shell_lifecycle.py:322)
has_validator  # noqa: F821 (function @ shell_readonly.py:595)
readonly_native_allowed  # noqa: F821 (function @ shell_readonly.py:768)
native_pilot_post_receipt  # noqa: F821 (variable @ shell_receipt.py:321)
argv_template  # noqa: F821 (variable @ shell_resolver.py:137)
report_shell_resolution  # noqa: F821 (function @ shell_resolver.py:718)
is_raw_bypass  # noqa: F821 (function @ shell_route.py:81)
_.ensure_empire_seed  # noqa: F821 (method @ skill_store.py:354)
_.register_external_provider  # noqa: F821 (method @ skill_store.py:1535)
_.revoke_grant  # noqa: F821 (method @ sticky_grants_store.py:374)
_.ingest_legacy_sidecar  # noqa: F821 (method @ sticky_grants_store.py:418)
SymbolNeighborhoodCache  # noqa: F821 (class @ symbol_neighborhood_cache.py:49)
_.invalidate_project  # noqa: F821 (method @ symbol_neighborhood_cache.py:109)
_.fresh_size  # noqa: F821 (method @ symbol_neighborhood_cache.py:129)
has_emit_failures  # noqa: F821 (function @ sync_store.py:279)
clear_emit_failures  # noqa: F821 (function @ sync_store.py:289)
flush_events  # noqa: F821 (function @ sync_store.py:326)
has_active_task  # noqa: F821 (function @ task_lifecycle_store.py:106)
before_gate  # noqa: F821 (variable @ tool_gate_service.py:365)
after_gate  # noqa: F821 (variable @ tool_gate_service.py:367)
has_direct_impl  # noqa: F821 (function @ tool_interface.py:185)
signature_param_names  # noqa: F821 (function @ tool_interface.py:524)
all_specs  # noqa: F821 (function @ tool_interface.py:540)
stdio_advertised_names  # noqa: F821 (function @ tool_interface.py:578)
local_only_names  # noqa: F821 (function @ tool_interface.py:586)
describe_tools  # noqa: F821 (function @ tool_suggestion_descs.py:51)

# @vulture-class: fp
# ── Cross-boundary API — referenced from tests/scripts/webapp/benchmarks outside scan roots ──
_extract_bash_commands  # noqa: F821 (function @ access_gate.py:959)
_.check_edit  # noqa: F821 (method @ access_gate.py:1760)
DENIAL_TIERS  # noqa: F821 (variable @ agent_orchestrator.py:338)
claim_next  # noqa: F821 (function @ ai_deploy_daemon.py:50)
run_once  # noqa: F821 (function @ ai_deploy_runner.py:76)
encrypt_signing_key  # noqa: F821 (function @ ai_deploy_secret.py:55)
generate_secret  # noqa: F821 (function @ ai_deploy_totp.py:34)
provisioning_uri  # noqa: F821 (function @ ai_deploy_totp.py:88)
matched_lemmas  # noqa: F821 (variable @ skill_trigger.py:37, operator_intent_resolver.py:322)
finished_at  # noqa: F821 (variable @ installer.py:33)
_.subscribe  # noqa: F821 (method @ installer.py:103)
_.finished_at  # noqa: F821 (attribute @ installer.py:150)
_.preload  # noqa: F821 (method @ service.py:348)
push  # noqa: F821 (function @ anchor_stack.py:76)
_.list_anchors_for_drawer  # noqa: F821 (method @ anchor_store.py:360)
_.mark_stale  # noqa: F821 (method @ anchor_store.py:401, palace_stale_signals.py:95)
similarity  # noqa: F821 (variable @ capture_gate.py:73, memory_fit.py:54)
_._grant_user_intent_tools  # noqa: F821 (method @ claude_hook.py:387)
_.heartbeat  # noqa: F821 (method @ co_conductor.py:73)
_run_process  # noqa: F821 (function @ code_runner.py:216)
sln  # noqa: F821 (variable @ code_runner.py:421)
evict_old_logs  # noqa: F821 (function @ code_runner_detached.py:1018)
_.get_unit_content  # noqa: F821 (method @ code_units.py:295)
_.verify_lane  # noqa: F821 (method @ conductor_verification_service.py:34)
_.verify_full_suite  # noqa: F821 (method @ conductor_verification_service.py:109)
requires_restart  # noqa: F821 (variable @ config_schema.py:26)
concepts  # noqa: F821 (function @ control_authority_ledger.py:179)
invalidate_cache  # noqa: F821 (function @ csharp_roslyn_client.py:121, identity_resolver.py:121)
paginate  # noqa: F821 (function @ cursor_pagination.py:115)
_.prune  # noqa: F821 (method @ edit_history.py:474)
warning_drawers  # noqa: F821 (variable @ edit_memory_gate_palace.py:62)
suggest_drawers  # noqa: F821 (variable @ edit_memory_gate_palace.py:63)
reason_code  # noqa: F821 (variable @ decision.py:83, write_authorizer.py:28)
user_message  # noqa: F821 (variable @ decision.py:84, write_authorizer.py:29)
enforcement  # noqa: F821 (variable @ decision.py:85)
_.authorize_write  # noqa: F821 (method @ write_authorizer.py:36)
consume_approvals_for_session  # noqa: F821 (function @ escalation_hook.py:316)
approved_at  # noqa: F821 (variable @ escalation_store.py:91, host_operator_binding_store.py:77)
_.support  # noqa: F821 (property @ frontend_ast.py:33)
clear_cache  # noqa: F821 (function @ heuristic_judge.py:3453, mcp_registry.py:239, memory_discovery.py:1062 (+1 more))
known_hosts  # noqa: F821 (function @ host_support_matrix.py:308)
issued_at  # noqa: F821 (variable @ identity_store.py:79)
_.count_tokens  # noqa: F821 (method @ identity_store.py:435)
matched_keywords  # noqa: F821 (variable @ intent_guard.py:32, memory_discovery.py:50)
strike  # noqa: F821 (variable @ judge_taxonomy.py:340)
record_security_strike  # noqa: F821 (function @ judge_taxonomy.py:438)
ingested  # noqa: F821 (variable @ known_projects_store.py:104, known_projects_store.py:138)
current_boot_token  # noqa: F821 (function @ managed_mode_service.py:41)
conductor_start  # noqa: F821 (function @ mcp_server.py:4872)
conductor_send  # noqa: F821 (function @ mcp_server.py:5153)
conductor_stop  # noqa: F821 (function @ mcp_server.py:5241)
conductor_output  # noqa: F821 (function @ mcp_server.py:5910)
migrate_markdown_to_sqlite  # noqa: F821 (function @ memory_sqlite_store.py:707)
memory_anchor_health  # noqa: F821 (function @ memory_sqlite_store.py:1072)
audit_recorded  # noqa: F821 (variable @ outer_gate.py:87)
executed  # noqa: F821 (variable @ outer_gate_audit.py:43)
audit_degraded  # noqa: F821 (variable @ outer_gate_audit.py:45)
refused_reason  # noqa: F821 (variable @ outer_gate_audit.py:46, shell_egress_service.py:573)
three_phase_audited_execute  # noqa: F821 (function @ outer_gate_audit.py:58) owner=#461 end_state=production-caller-or-deleted — #461 WAR O cause B, THE archetype: the three-phase intent-audit-before-mutation helper has ZERO production callers (definition + tests/security/test_outer_gate_three_phase_audit.py only), and three other modules' docstrings point at it as though it were the live discipline. The one detector that catches a caller-less control was silenced HERE, under `fp`. Done-when: the WebMCP mutation path routes through it, or the symbol, its prose references and this entry die in one commit.
unclassified  # noqa: F821 (function @ outer_gate_manifest.py:908)
_.log_message  # noqa: F821 (method @ outer_gate_transport.py:4060)
_.do_GET  # noqa: F821 (method @ outer_gate_transport.py:4200)
allow_reuse_address  # noqa: F821 (variable @ outer_gate_transport.py:4215)
_.record_palace_write  # noqa: F821 (method @ palace_hub_extension.py:403)
_.record_aidocs_write  # noqa: F821 (method @ palace_hub_extension.py:406)
_.get_states  # noqa: F821 (method @ palace_stale_signals.py:162)
_.list_stale_for_unit  # noqa: F821 (method @ palace_stale_signals.py:178)
_.unresolved_count  # noqa: F821 (method @ palace_stale_signals.py:192)
bootstrap_local_superadmin  # noqa: F821 (function @ permission_catalog.py:356)
has_global_palace_drawers  # noqa: F821 (variable @ phase_g_migration.py:35)
dry_run_dental_migration  # noqa: F821 (function @ phase_g_migration.py:47)
_.has_global_palace_drawers  # noqa: F821 (attribute @ phase_g_migration.py:86)
generate_mcp_json_patch  # noqa: F821 (function @ phase_g_migration.py:123)
generate_hook_patch  # noqa: F821 (function @ phase_g_migration.py:153)
survey_summary  # noqa: F821 (function @ phase_g_migration.py:191)
rebuild_from_events  # noqa: F821 (function @ project_backlog_store.py:651, task_todos_store.py:526)
seed_events_from_sqlite  # noqa: F821 (function @ project_backlog_store.py:662, task_todos_store.py:536)
_.invalidate  # noqa: F821 (method @ proof_cache.py:55, symbol_neighborhood_cache.py:105)
compute_public_mirror_drift  # noqa: F821 (function @ public_mirror_drift.py:37)
raw_query  # noqa: F821 (variable @ recall_intent.py:154, server_recall_tools.py:555)
operation_terms  # noqa: F821 (variable @ recall_intent.py:157, server_recall_tools.py:558)
_.as_audit  # noqa: F821 (method @ release_trust.py:73)
_.resolve_stats  # noqa: F821 (method @ run_duration_bucket_store.py:179)
_.plan_connect  # noqa: F821 (method @ runtime_service.py:3968)
frozen  # noqa: F821 (variable @ security_violation_service.py:128)
_.get_recent_strikes  # noqa: F821 (method @ security_violation_service.py:264)
session_update  # noqa: F821 (function @ server_runtime_context_tools.py:46)
session_list  # noqa: F821 (function @ server_session_tools.py:20)
session_create  # noqa: F821 (function @ server_session_tools.py:440)
session_skills_set  # noqa: F821 (function @ server_skill_tools.py:92)
_.access_gate  # noqa: F821 (attribute @ service_hub.py:51)
_.touch  # noqa: F821 (method @ session_project_bind_store.py:148)
LEGACY_SUBPROCESS_FINGERPRINTS  # noqa: F821 (variable @ shell_egress_service.py:1152)
LEGACY_SUBPROCESS_CALLSITES  # noqa: F821 (variable @ shell_egress_service.py:1648)
revoked_at  # noqa: F821 (variable @ sticky_grants_store.py:69)
_.observe  # noqa: F821 (method @ sync_store.py:54)
_.isError  # noqa: F821 (attribute @ tool_display.py:381)
ai_search  # noqa: F821 (function @ discovery.py:18)
ai_text_search  # noqa: F821 (function @ discovery.py:30)
ai_trace  # noqa: F821 (function @ discovery.py:57)
ai_investigate  # noqa: F821 (function @ discovery.py:86)
ai_get_outline  # noqa: F821 (function @ discovery.py:103)
ai_get_symbol_info  # noqa: F821 (function @ discovery.py:111)
ai_get_symbol_snippet  # noqa: F821 (function @ discovery.py:136)
ai_get_dependencies  # noqa: F821 (function @ discovery.py:149)
ai_test  # noqa: F821 (function @ execution.py:17)
on_deny  # noqa: F821 (variable @ tool_gate_service.py:374)
on_ask  # noqa: F821 (variable @ tool_gate_service.py:377)
on_freeze  # noqa: F821 (variable @ tool_gate_service.py:380)
HIDDEN  # noqa: F821 (variable @ tool_interface.py:58)
RUN  # noqa: F821 (variable @ tool_interface.py:63)
direct_impls  # noqa: F821 (function @ tool_interface.py:190)
public_schema  # noqa: F821 (function @ tool_interface.py:506)
admin_clear_freeze  # noqa: F821 (function @ tool_interface.py:1354)
admin_clear_reconnect  # noqa: F821 (function @ tool_interface.py:1405)
ai_msg  # noqa: F821 (function @ tool_interface.py:1445)
ai_task  # noqa: F821 (function @ tool_interface.py:1487)
ai_session  # noqa: F821 (function @ tool_interface.py:1556)
ai_project  # noqa: F821 (function @ tool_interface.py:1614)
ai_version  # noqa: F821 (function @ tool_interface.py:1666)
ai_whoami  # noqa: F821 (function @ tool_interface.py:2225) owner=phoenix/#859 end_state=retire-when-#859-lands-the-chain-migration-and-the-/clear-divergence-is-readable-from-ai_agents-alone — #859 identity-channel diagnostic; bare @tool registry decl consumed by the ToolSpec REGISTRY like every sibling above (the gate's --ignore-decorators covers @server.tool, not @tool). Security-surface by shape (gate-exempt tool); stewarded on day one per #461 WAR O so the ratchet does not absorb it unowned.
ai_gate_explain  # noqa: F821 (function @ tool_interface.py) owner=phoenix end_state=retire-when-the-ladder-and-strike-tables-are-self-describing-on-the-refusal-envelope — 2026-08-25 refusal-consequence diagnostic; bare @tool registry decl consumed by the ToolSpec REGISTRY like every sibling above. Security-surface by shape (gate-exempt tool).
ai_seat  # noqa: F821 (function @ tool_interface.py:1689)
ai_skill  # noqa: F821 (function @ tool_interface.py:1717)
ai_soul  # noqa: F821 (function @ tool_interface.py:1763)
ai_qa  # noqa: F821 (function @ tool_interface.py:1926)
ai_failures  # noqa: F821 (function @ tool_interface.py:1974)
related_project_register  # noqa: F821 (function @ tool_interface.py:2028)
ai_gate_msg  # noqa: F821 (function @ tool_interface.py:2066)
ai_concurrency_reset  # noqa: F821 (function @ tool_interface.py:2075)
bump_agent_memory_epoch  # noqa: F821 (function @ tool_interface.py:2083)
ai_preflight  # noqa: F821 (function @ tool_interface.py:2101)
ai_resolve_backend  # noqa: F821 (function @ tool_interface.py:2109)
ai_resolve_scope  # noqa: F821 (function @ tool_interface.py:2118)
skill_registry_get  # noqa: F821 (function @ tool_interface.py:2152)
skill_scan  # noqa: F821 (function @ tool_interface.py:2160)
skill_trigger_state_get  # noqa: F821 (function @ tool_interface.py:2168)
ai_notifications_clear  # noqa: F821 (function @ tool_interface.py:2185)
ai_backlog  # noqa: F821 (function @ tool_interface.py:2215)
ai_get_lines  # noqa: F821 (function @ tool_interface.py:2282)
ai_agents  # noqa: F821 (function @ tool_interface.py:2303)
# SSOT-04/07 (2026-07-15): registry declaration stubs — invoked via the
# gate/registry projections (_delegate / schema_for / auto-extension), never
# called by name in Python. Same class as every registry row above.
tool_catalog  # noqa: F821 (function @ tool_interface.py:4194)
tool_capabilities  # noqa: F821 (function @ tool_interface.py:4234)
dashboard_memory_capture  # noqa: F821 (function @ tool_interface.py:4519)
dashboard_view  # noqa: F821 (function @ tool_interface.py:4571)
config_view  # noqa: F821 (function @ tool_interface.py:4630)
project_index_status  # noqa: F821 (function @ tool_interface.py:3988)
project_sync  # noqa: F821 (function @ tool_interface.py:4016)
project_register_from_github_url  # noqa: F821 (function @ tool_interface.py:4047)
org_list  # noqa: F821 (function @ tool_interface.py:4077)
org_select  # noqa: F821 (function @ tool_interface.py:4112)
session_current  # noqa: F821 (function @ tool_interface.py:3780)
session_select  # noqa: F821 (function @ tool_interface.py:3810)
session_delete  # noqa: F821 (function @ tool_interface.py:3895)
project_current  # noqa: F821 (function @ tool_interface.py:3934)
project_status  # noqa: F821 (function @ tool_interface.py:3960)
lane_scope  # noqa: F821 (function @ tool_interface.py:4163)
skill_scan_results  # noqa: F821 (function @ tool_interface.py:4266)
broken_references  # noqa: F821 (function @ tool_interface.py:4293)
list_mcp_servers  # noqa: F821 (function @ tool_interface.py:4322)
mcp_registry_search  # noqa: F821 (function @ tool_interface.py:4349)
memory_kg_get  # noqa: F821 (function @ tool_interface.py:4377)
memory_kg_graph  # noqa: F821 (function @ tool_interface.py:4410)
vocab_get_grouped  # noqa: F821 (function @ tool_interface.py:4439)
vocab_list_kinds  # noqa: F821 (function @ tool_interface.py:4467)
vocab_list_langs  # noqa: F821 (function @ tool_interface.py:4493)
dashboard_snapshot  # noqa: F821 (function @ tool_interface.py:4601)
ai_backends  # noqa: F821 (function @ tool_interface.py:2266)
ai_models  # noqa: F821 (function @ tool_interface.py:2279)
ai_deslop_apply  # noqa: F821 (function @ tool_interface.py:2292)
planning_step_mark  # noqa: F821 (function @ tool_interface.py:2334)
workflow_action_satisfy  # noqa: F821 (function @ tool_interface.py:2349)
ai_find  # noqa: F821 (function @ tool_interface.py:2340)
config_get  # noqa: F821 (function @ tool_interface.py:2397)
ai_get_modules  # noqa: F821 (function @ tool_interface.py:2418)
ai_get_module_files  # noqa: F821 (function @ tool_interface.py:2424)
ai_index_status  # noqa: F821 (function @ tool_interface.py:2441)
ai_recall  # noqa: F821 (function @ tool_interface.py:2447)
ai_palace_search  # noqa: F821 (function @ tool_interface.py:2466)
ai_palace_status  # noqa: F821 (function @ tool_interface.py:2487)
ai_palace_diary_read  # noqa: F821 (function @ tool_interface.py:2495)
ai_read_pdf  # noqa: F821 (function @ tool_interface.py:2515)
ai_read_docx  # noqa: F821 (function @ tool_interface.py:2535)
ai_read_excel  # noqa: F821 (function @ tool_interface.py:2553)
ai_read_jsonl  # noqa: F821 (function @ tool_interface.py:2576)
ai_read_sqlite  # noqa: F821 (function @ tool_interface.py:2595)
ai_read_raw  # noqa: F821 (function @ tool_interface.py:2618)
audit_events_for_task  # noqa: F821 (function @ tool_interface.py:2637)
ai_process_audit  # noqa: F821 (function @ tool_interface.py:2647)
ai_schema  # noqa: F821 (function @ tool_interface.py:2663)
semantic_search  # noqa: F821 (function @ tool_interface.py:2689)
memory_read  # noqa: F821 (function @ tool_interface.py:2698)
memory_search  # noqa: F821 (function @ tool_interface.py:2706)
ai_bundle  # noqa: F821 (function @ tool_interface.py:2732)
ai_slop  # noqa: F821 (function @ tool_interface.py:2754)
workflow_actions_get  # noqa: F821 (function @ tool_interface.py:2797)
workflow_agent_rules  # noqa: F821 (function @ tool_interface.py:2803)
related_project_list  # noqa: F821 (function @ tool_interface.py:2811)
related_project_code_search  # noqa: F821 (function @ tool_interface.py:2825)
related_project_symbol_bundle  # noqa: F821 (function @ tool_interface.py:2837)
related_project_subsystem_bundle  # noqa: F821 (function @ tool_interface.py:2845)
related_project_compare_concept  # noqa: F821 (function @ tool_interface.py:2853)
ai_create_file  # noqa: F821 (function @ tool_interface.py:2908)
ai_insert_lines  # noqa: F821 (function @ tool_interface.py:2927)
ai_replace  # noqa: F821 (function @ tool_interface.py:2948)
ai_batch_edit  # noqa: F821 (function @ tool_interface.py:2985)
ai_delete  # noqa: F821 (function @ tool_interface.py:3022)
ai_file  # noqa: F821 (function @ tool_interface.py) owner=phoenix/#958 end_state=retire-when-vulture-can-see-the-@tool-registry-decorator — 2026-08-28 governed file-identity tool (create|rename|delete|restore), the home of the RENAME that previously had no governed path at all. Bare @tool registry declaration consumed by the ToolSpec REGISTRY and dispatched BY NAME through _delegate, exactly like ai_delete and ai_create_file above it; no Python caller invokes the symbol, which is precisely what vulture reports. Stewarded rather than bare because tool_interface is a gate surface: the guard's own message is right that silencing the one detector that finds caller-less controls needs a name attached.
ai_protect  # noqa: F821 (function @ tool_interface.py:3045)
ai_palace_diary_write  # noqa: F821 (function @ tool_interface.py:3076)
ai_palace_maintenance  # noqa: F821 (function @ tool_interface.py:3096)
memory_promote  # noqa: F821 (function @ tool_interface.py:3125)
memory_capture  # noqa: F821 (function @ tool_interface.py:3150)
ai_index_sync  # noqa: F821 (function @ tool_interface.py:3238)
ai_git  # noqa: F821 (function @ tool_interface.py:3783)
ai_memory  # noqa: F821 (registry-invoked public tool — memory_* consolidator @ tool_interface.py)
PERM_SECURITY_PREFLIGHT_FAILSAFE  # noqa: F821 (USED: permission_catalog.py:129 catalog row + prompt_mutator.py:1955 lazy import — vulture misses same-file container use at 60)
phase_order  # noqa: F821 (variable @ types.py:133)
file_owners  # noqa: F821 (variable @ types.py:136)
confidence_score  # noqa: F821 (variable @ unit_resolver.py:42)
_.resolve_path  # noqa: F821 (method @ unit_resolver.py:109)

# @vulture-class: fp
# ── In-tree dynamic/string references — verify individually before any delete ──
_.spawn_interactive  # noqa: F821 (method @ agent_expert_service.py:2130) verify-before-delete
_._build_lightweight_prompt_context  # noqa: F821 (method @ claude_hook.py:1148) verify-before-delete
_._build_prompt_context  # noqa: F821 (method @ claude_hook.py:1212) verify-before-delete
_.project_config_path  # noqa: F821 (method @ config.py:553) verify-before-delete
_.session_config_path  # noqa: F821 (method @ config.py:558) verify-before-delete
_.get_layer_value  # noqa: F821 (method @ config.py:675) verify-before-delete
context_budget_check  # noqa: F821 (function @ context_budget.py:14) verify-before-delete
context_compact  # noqa: F821 (function @ context_budget.py:82) verify-before-delete
anchor_symbol  # noqa: F821 (variable @ edit_memory_gate.py:32) verify-before-delete
anchor_file  # noqa: F821 (variable @ edit_memory_gate.py:33) verify-before-delete
CANONICAL_PIPELINE  # noqa: F821 (variable @ controller.py:34) verify-before-delete
CONFIRMABLE  # noqa: F821 (variable @ decision.py:22) verify-before-delete
_._kind_for  # noqa: F821 (method @ index_store.py:790) verify-before-delete
audit_id  # noqa: F821 (variable @ outer_gate.py:122, shell_egress_service.py:578) verify-before-delete
FAMILY_BY_BLOCKED_BY  # noqa: F821 (variable @ security_violation_service.py:53) verify-before-delete
on_allow  # noqa: F821 (variable @ tool_gate_service.py:370) verify-before-delete
lane_agent_limits  # noqa: F821 (variable @ types.py:159) verify-before-delete

# @vulture-class: fp
# ── Test-covered API additions (2026-07-12 CANDIDATE-DELETE burndown) ──
# Reclassified out of the deletion queue: real references exist in mcp/tests
# (outside vulture scan roots).
terse_tool_output  # noqa: F821 (function @ mcp_server_runtime_helpers.py:734) referenced by mcp/tests/audit/test_terse_tool_output.py
evaluate_poll_budget  # noqa: F821 (function @ poll_budget.py:60) referenced by mcp/tests/audit/test_poll_budget.py
host_label  # noqa: F821 (function @ host_support_matrix.py:285) referenced by mcp/tests/host/test_host_support_matrix.py
host_matrix  # noqa: F821 (function @ host_support_matrix.py:290) referenced by mcp/tests/host/test_host_support_matrix.py
ManagedFileService  # noqa: F821 (class @ managed_file_service.py:8) constructed by mcp/tests/runtime/test_managed_file_service.py; unused hub wiring (service_hub.managed_files) removed 2026-07-12
_.is_loaded  # noqa: F821 (method @ service.py:359, pipeline.py:38, pipeline.py:91) NLPService.is_loaded asserted by mcp/tests/aidocs_nlp/test_service.py; pipeline pair is the protocol/impl twin
authenticated_at  # noqa: F821 (variable @ operator_auth_service.py:452) auth-context field written at operator_auth_service.py:542/662/721; constructed directly by tests/host/test_palace_maintenance_tool.py:55+377, tests/host/test_palace_mine_guard.py:453, tests/security/test_dashboard_action_gates.py:117 (#427 audit: reclassified from cross-boundary bulk with proof)

# @vulture-class: foundation
# ── aidocs_nlp dashboard pack-install seam (doctrine 2026-05-12) ──
# Reclassified from CANDIDATE-DELETE 2026-07-12: zero in-repo callers today,
# but this is the documented one-door API for "language packs downloaded on
# demand via the dashboard" (aidocs_nlp/__init__ docstring). Deleting it
# cascades into exported package API (PackStatus, Installer, LanguagePack
# catalog fields). Delete only if the doctrine seam itself is retired.
_.available_packs  # noqa: F821 (method @ service.py:370)
_.install_pack  # noqa: F821 (method @ service.py:388)
_.uninstall_pack  # noqa: F821 (method @ service.py:395)
_.install_status  # noqa: F821 (method @ service.py:402)
bytes_downloaded  # noqa: F821 (variable @ installer.py:31) InstallProgress payload field
last_message  # noqa: F821 (variable @ installer.py:34) InstallProgress payload field, written at installer.py:137, streamed to subscribers
_.last_message  # noqa: F821 (attribute @ installer.py:137)
pinned_version  # noqa: F821 (variable @ language_registry.py:33) LanguagePack catalog field

# @vulture-class: dead-pending
# ── Same-file-only extra refs — deletion candidates (owner review) ──
# Burndown 2026-07-12: re-verified repo-wide (token + string scan incl.
# tests/scripts/webapp/dashboard/benchmarks) — still zero refs outside the
# defining file. Left queued because deletion also needs the same-file
# writer/usage sites removed; several RHS are side-effectful (boot calls),
# so this is a surgical rewrite per entry, not a line delete.
anchor_node_id  # noqa: F821 (variable @ anchor_stack.py:49) CANDIDATE-DELETE(samefile)
last_failure_at  # noqa: F821 (variable @ circuit_breaker.py:26) CANDIDATE-DELETE(samefile)
_.last_failure_at  # noqa: F821 (attribute @ circuit_breaker.py:117) CANDIDATE-DELETE(samefile)
TIER_PERMIT  # noqa: F821 (variable @ destructive_taxonomy.py:60) CANDIDATE-DELETE(samefile)
latched_at  # noqa: F821 (variable @ degraded_latch.py:41) CANDIDATE-DELETE(samefile)
REQUIRES_FOLLOWUP  # noqa: F821 (variable @ failure_stewardship.py:117) CANDIDATE-DELETE(samefile)
apply_recommended  # noqa: F821 (function @ governed_bash_profile.py:161) CANDIDATE-DELETE(samefile) governed_* = safety floor
set_broker  # noqa: F821 (function @ governed_shell_approval_store.py:328) CANDIDATE-DELETE(samefile) governed_* = safety floor
_._conductor_binding_prune  # noqa: F821 (attribute @ mcp_server.py:1342, mcp_server.py:1352, mcp_server.py:1354) CANDIDATE-DELETE(samefile) RHS is the boot prune call — keep the call, drop only the binding
_._freeze_boot_sweep  # noqa: F821 (attribute @ mcp_server.py:1363, mcp_server.py:1369) CANDIDATE-DELETE(samefile) RHS is the boot sweep call — keep the call, drop only the binding
_._aidocs_deferred_to_disable  # noqa: F821 (attribute @ mcp_server.py:1768) CANDIDATE-DELETE(samefile)
has_legacy_marker  # noqa: F821 (variable @ project_doctor.py:43) CANDIDATE-DELETE(samefile)
has_index_db  # noqa: F821 (variable @ project_doctor.py:44) CANDIDATE-DELETE(samefile)
add_turn_read_file  # noqa: F821 (function @ protected_file_runtime.py:114) CANDIDATE-DELETE(samefile) protected-file machinery — owner sign-off
LifecyclePreflightFn  # noqa: F821 (variable @ shell_egress_service.py:189) CANDIDATE-DELETE(samefile) shell_egress = safety floor
disconnect_after_seconds  # noqa: F821 (variable @ shell_envelope.py:93) CANDIDATE-DELETE(samefile)
raw_tool_input  # noqa: F821 (variable @ shell_envelope.py:100) CANDIDATE-DELETE(samefile)
_._tool_spec  # noqa: F821 (attribute @ tool_interface.py:255) CANDIDATE-DELETE(samefile)

# @vulture-class: dead-pending
# ── DELETION-CANDIDATE REVIEW QUEUE — burndown 2026-07-12 ──
# Every remaining entry was re-verified this pass: repo-wide token +
# string-literal scan (src, tests, scripts, webapp, dashboard, benchmarks,
# third_party) found ZERO references and no dynamic-use pattern. They stay
# queued ONLY because they sit on the safety floor (security/gate/
# enforcement surfaces: deletion there needs owner sign-off) or belong to
# a deliberately retained stub. 113 queue entries whose symbols were truly
# dead were deleted from source in this same pass; 14 were reclassified
# into the evidence-tagged sections above; comp_kind was fixed by
# underscore-rename at its unpack site.
SAFE_READ_TOOLS  # noqa: F821 (variable @ anticoup.py:397) zero refs 2026-07-12; anticoup = security floor
_._extract_tool_result_text  # noqa: F821 (method @ claude_hook.py:476) zero refs 2026-07-12; claude_hook = safety floor
_._extract_text  # noqa: F821 (method @ claude_hook.py:482) zero refs 2026-07-12; claude_hook = safety floor
_._classify_tool_action  # noqa: F821 (method @ claude_hook.py:731) zero refs 2026-07-12; claude_hook = safety floor
_._check_session_freeze  # noqa: F821 (method @ claude_hook.py:808) zero refs 2026-07-12; claude_hook = safety floor
_._record_classification_event  # noqa: F821 (method @ claude_hook.py:1105) zero refs 2026-07-12; claude_hook = safety floor
_._operator_intent_note  # noqa: F821 (method @ claude_hook.py:1136) zero refs 2026-07-12; claude_hook = safety floor
CoConductor  # noqa: F821 (class @ co_conductor.py:35) zero refs 2026-07-12; deferred co-conductor stub (see cot_excerpt above) — retire with the stub decision
_.review_conductor_tool_request  # noqa: F821 (method @ co_conductor.py:44) see CoConductor
_.relieve_conductor  # noqa: F821 (method @ co_conductor.py:61) see CoConductor
_._gates  # noqa: F821 (attribute @ controller.py:66) zero refs 2026-07-12; enforcement_pkg = safety floor
error_kind  # noqa: F821 (variable @ decision.py:46) zero refs 2026-07-12; enforcement_pkg Decision field = safety floor
error_detail  # noqa: F821 (variable @ decision.py:47) zero refs 2026-07-12; enforcement_pkg Decision field = safety floor
bypassed_by_override  # noqa: F821 (variable @ decision.py:91) zero refs 2026-07-12; enforcement_pkg Decision field = safety floor
skipped_after_decision  # noqa: F821 (variable @ decision.py:92) zero refs 2026-07-12; enforcement_pkg Decision field = safety floor
grants_consumed  # noqa: F821 (variable @ decision.py:100) zero refs 2026-07-12; enforcement_pkg Decision field = safety floor
normalized_targets  # noqa: F821 (variable @ decision.py:106) zero refs 2026-07-12; enforcement_pkg Decision field = safety floor
CLAUDE_PRETOOL  # noqa: F821 (variable @ surface.py:19) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
CLAUDE_USER_PROMPT_SUBMIT  # noqa: F821 (variable @ surface.py:20) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
CLAUDE_SESSION_START  # noqa: F821 (variable @ surface.py:21) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
CLAUDE_POST_TOOL_USE  # noqa: F821 (variable @ surface.py:22) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
MCP_RUN_KILL  # noqa: F821 (variable @ surface.py:24) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
OPENCODE_PRETOOL  # noqa: F821 (variable @ surface.py:25) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
CODEX_PRETOOL  # noqa: F821 (variable @ surface.py:26) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
OPENAI_AGENTS_PRETOOL  # noqa: F821 (variable @ surface.py:27) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
DASHBOARD_ACTION  # noqa: F821 (variable @ surface.py:28) zero refs 2026-07-12; enforcement_pkg surface id = safety floor
actor_id  # noqa: F821 (variable @ surface.py:37) zero refs 2026-07-12; enforcement_pkg surface field = safety floor
has_signed_admin_token  # noqa: F821 (variable @ surface.py:40) zero refs 2026-07-12; enforcement_pkg surface field = safety floor
operation_kind  # noqa: F821 (variable @ surface.py:63) zero refs 2026-07-12; enforcement_pkg surface field = safety floor
raw_payload  # noqa: F821 (variable @ surface.py:65) zero refs 2026-07-12; enforcement_pkg surface field = safety floor
target_paths  # noqa: F821 (variable @ surface.py:66) zero refs 2026-07-12; enforcement_pkg surface field = safety floor
serve_windows  # noqa: F821 (function @ governed_shell_broker.py:637) zero refs 2026-07-12; governed_* = safety floor (platform entrypoint)
_client_sid_of_pipe  # noqa: F821 (function @ governed_shell_broker_win.py:198) zero refs 2026-07-12; governed_* = safety floor (Windows pipe ACL)
path_acl_operator_only  # noqa: F821 (function @ governed_shell_broker_win.py:444) zero refs 2026-07-12; governed_* = safety floor (Windows ACL)
_._aidocs_eager_tag  # noqa: F821 (attribute @ mcp_server.py:1769) zero refs 2026-07-12; diagnostic attr, assignment site needs surgical rewrite not line-delete
_._aidocs_deferred_tag  # noqa: F821 (attribute @ mcp_server.py:1770) zero refs 2026-07-12; see _aidocs_eager_tag
_._aidocs_filter_applied  # noqa: F821 (attribute @ mcp_server.py:1774) zero refs 2026-07-12; see _aidocs_eager_tag
AIDOCSAgentHooks  # noqa: F821 (class @ openai_agents_adapter.py:244) zero refs 2026-07-12; openai-agents integration seam — its on_start/on_end are the framework hooks allowlisted above; retire together
_content_binding  # noqa: F821 (function @ outer_gate_edit.py:134) zero refs 2026-07-12; outer_gate_* = safety floor
tenant_projects_dir  # noqa: F821 (function @ outer_gate_tenancy.py:84) zero refs 2026-07-12; outer_gate_* = safety floor
_.purge_expired  # noqa: F821 (method @ outer_gate_token_store.py:554) zero refs 2026-07-12; outer_gate_* = safety floor (token hygiene)
_.sweep_expired  # noqa: F821 (method @ session_freeze_store.py:483) zero refs 2026-07-12; freeze machinery — owner sign-off
capability_info  # noqa: F821 (function @ shell_capability_matrix.py:231) zero refs 2026-07-12; shell governance surface
DECISION_DETACH_REQUIRED  # noqa: F821 (variable @ shell_policy.py:57) zero refs 2026-07-12; shell policy constant = safety floor
CAT_DESTRUCTIVE  # noqa: F821 (variable @ shell_provider_dialect.py:31) zero refs 2026-07-12; shell dialect taxonomy = safety floor
_read_flavor  # noqa: F821 (function @ shell_resolver.py:471) zero refs 2026-07-12; shell resolver = safety floor
_check_confirm  # noqa: F821 (function @ tool_interface.py:390) zero refs 2026-07-12; two-phase-confirm surface — verify no dynamic dispatch before delete

# @vulture-class: foundation
# ── #342 awaiting-wiring cores (territory wars 2026-07-12) ──
# Built + test-covered primitives whose CALLERS are the banked conductor
# wirings in backlog #342 (admit-path read-gate, session_connect latch,
# ask-verdict, override flatten, delta audit). Each entry DIES in the same
# commit that lands its wiring. Owner: head-conductor.
normalize_policy_commands  # noqa: F821 (bash_policy.py — #18 family fold; caller = config surfaces)
read_gate_check  # noqa: F821 (conductor_comms.py — #217 admit gate wiring)
lane_scope_ask  # noqa: F821 (conductor_comms.py — #218 worker ask surface)
get_pending_scope_asks  # noqa: F821 (conductor_comms.py — #218 conductor view)
lane_scope_grant  # noqa: F821 (conductor_comms.py — #218 grant routing)
set_judge_override  # noqa: F821 (judge_overrides.py — #19 delta-audit write path)
clear_conversation_latch  # noqa: F821 (managed_mode_service.py — #63 admin_clear_reconnect)
# evaluate_via_broker entry removed 2026-07-13: the #342 flip landed — claude_hook.main() now calls it.


# @vulture-class: fp
# ── Seven-wars seal, 2026-07-13 (220e5e27) ──
# NOT dead code. Vulture scans ONLY mcp/server/aidocs_mcp (see the gate's
# _vulture_targets), so a symbol whose ONLY consumers are TESTS reads as dead.
# Every entry below names its live consumer — verify it, do not trust this list.
# These die the day their consumer does; if you delete a consumer, delete its
# entry in the SAME commit (see the header: "the list rots otherwise").
__getattr__  # noqa: F821 (aidocs_mcp/__init__.py — PEP 562 module-level lazy __version__; Python calls it on attribute access, which vulture cannot see. Landed with the adapter inversion so a hook spawn no longer pays a pyproject/.git read.)
_reset_for_tests  # noqa: F821 (gate_health.py — test-only state reset; consumer: tests/host/test_gate_health.py)
last_mcp_activity  # noqa: F821 (gate_health.py — traffic-clock accessor; consumers: tests/host/test_gate_health.py:130/323/325, incl. the monotonic pin `test_stale_timestamp_cannot_drag_the_traffic_clock_backwards`. That pin guards a REAL fail-open: a backwards clock made live traffic look idle, and idle never alarms.)
reset_conductor_bind_state  # noqa: F821 (mcp_server_runtime_helpers.py — the ONE owned home for clearing the three bind globals incl. _last_known_project_root_by_host; consumer: mcp/tests/conftest.py autouse fixture. Half-resetting these caused #287's weeks-long misdiagnosis.)
DELIBERATE_CONSOLE_SPAWNS  # noqa: F821 (shell_egress_service.py — registry of spawns that INTENTIONALLY keep a console (cli._run_install streams live install output). Consumer: tests/security/test_spawn_surface_seal.py, which fails if a win32 spawn is windowless-less AND unregistered — and also fails on STALE rows here.)


# @vulture-class: fp
# ── Nine-wars seal, 2026-07-13 (#376) ──
# Same rule as the seven-wars block above: vulture scans ONLY
# mcp/server/aidocs_mcp, so a NEW public/diagnostic API whose consumers are
# tests (pending a dashboard wire-up) reads as dead. Each names its consumer.
gate_ro_health  # noqa: F821 (codenexus_identity.py — War 3 gate read-only role health diagnostic; consumer: tests/security/test_gate_ro_health.py — classifies missing-role/missing-grant/unavailable-db/schema-mismatch so a fresh provisioning reports exactly what is missing.)
missing_tables  # noqa: F821 (codenexus_identity.py — GateRoHealth field naming the un-SELECTable/absent identity tables; consumer: tests/security/test_gate_ro_health.py asserts it per failure mode.)
quarantined_count  # noqa: F821 (sync_store.py — War 1 clear-status read of incoming (unreceipted/forged) events hydration refused; consumer: tests/security/test_project_event_authority.py + tests/memory/test_sync_phase2.py.)
resolve_effective_org  # noqa: F821 (outer_gate_tenancy.py — War 4 back-compat tuple wrapper delegating to resolve_effective_org_result so the tuple and richer result never drift; consumers: tests/security/test_outer_gate_tenancy.py + test_effective_org_resolution.py.)


# @vulture-class: fp
# ── Prompt-pipeline war seal, 2026-07-14 (#377) ──
# Same rule: vulture scans ONLY mcp/server/aidocs_mcp, so symbols whose
# consumers are TESTS (or the MCP host runtime) read as dead. Each names its
# live consumer — verify before deleting; entries die with their consumer.
state_ledger  # noqa: F821 (variable @ prompt_submit_service.py PromptSubmitResult field — transaction-outcome audit surface; consumer: tests/hosts/test_war3_prompt_submit_transaction.py asserts state_ledger.committed/rolled_back/stages per outcome)
committed  # noqa: F821 (variable @ prompt_submit_service.py PromptSubmitStateLedger field — set at the commit boundary; consumer: war3 suite, e.g. test_failed_submit_rollback_precedes_later_commit / test_post_commit_fault_...)
_.committed  # noqa: F821 (attribute @ prompt_submit_service.py phase_boundary commit write; consumer: same war3 assertions as `committed`)
semantic_fingerprint  # noqa: F821 (method @ prompt_submit_service.py PromptSubmitResult — host-envelope parity probe; consumers: war3 test_same_semantic_result_renders_host_specific_envelopes + tests/host/test_prompt_mutator_parity.py parity-three-host-shapes)
_.held  # noqa: F821 (property @ prompt_submit_service.py PromptSubmitTransactionLock — lock-liveness probe; consumer: war3 test_process_crash_releases_prompt_lock_without_stale_lease)
_._surface_for_prompt  # noqa: F821 (method @ runtime_prompt_handling_service.py — hookless surfacing seam, consumer: tests/host/test_handle_prompt_surfacing.py; NOT wired into aidocs_handle_prompt because that entry already surfaces + arms the update gate via PromptSubmitService (double-fire); pending the #316/#69 hookless-entry decision — wire or delete there)
project_select  # noqa: F821 (function @ tool_interface.py — MCP tool spec invoked dynamically by the host (WebMCP project binding); consumers: tests/tooling/test_war6_tool_interface_ssot.py + gate_checks/webmcp_smoke.py + gate_checks/ai_deploy_smoke.py)

# @vulture-class: foundation
# ── Operator-intent War-3 host-agnostic seal, 2026-07-14 ──
# The 69d1fc8b regression added an `if host_kind == "claude_code"` gate whose
# else-branch ran these two obsolete symbols; the fix (9ccf880d) restored
# Phoenix's host-agnostic single-call-site contract and deleted that branch, so
# both symbols lost their production caller. They are RETAINED deliberately as
# regression-guard targets — deleting them would remove the tests that pin the
# obsolete path can never come back. Entries die with their consumers.
looks_like_operator_intent  # noqa: F821 (function @ operator_intent_resolver.py — OBSOLETE prompt classifier, no production caller after the host-agnostic fix; consumers: tests/host/test_operator_intent_adapter_audit.py monkeypatches it (test_obsolete_classifier_failure_cannot_block_*) to prove it NEVER runs on an ineligible origin and a broken classifier cannot block the bash-allowlist/decision-trace, + the ALLOWED_DEFINITION registry assertion)
OPERATOR_INTENT_UNSUPPORTED_NOTE  # noqa: F821 (constant @ operator_intent_resolver.py — the off-Claude "operator intent unsupported" downgrade note, no production emitter after the fix; consumer: tests/security/test_soul_grant_resolution_and_act_audit.py asserts it is NOT surfaced — the shared pipeline applies operator intent, never downgrades)
# @vulture-class: fp
_reset_publisher_cache  # noqa: F821 (governed_shell_attest.py — test-only Authenticode publisher-cache reset; consumer: tests/security/test_authenticode_publisher_cache.py accesses via getattr so vulture can't trace the call)

# @vulture-class: fp
# ── Session campaign 2026-07-16 (identity/auth war + Rust-foundation) ──
# require_epoch + set_session moved to mcp/vulture_future_debt.py (#426):
# their consumers are COMING (staged wiring), so they are future debt,
# not vulture false positives. This file is FALSE POSITIVES ONLY.
assign_role_to_user  # noqa: F821 (method @ rbac_store.py:618 — public global-scope RBAC grant (assign_role_to_user_scoped is the scoped sibling); public store API + consumer tests/security/test_rbac_and_escalation.py exercise the RBAC surface; vulture does not scan tests.)
contract_drivers  # noqa: F821 (function @ rust_contract.py:92 — the aidocs-doctrine §XXIX "everything-migrates-to-Rust" contract-driver harness; the ENTIRE point is to be imported by *_contract tests (tests/runtime/test_rust_contract_harness.py + tests/deploy_contract/**), which vulture does not scan. Deleting it deletes the migration acceptance mechanism.)

# @vulture-class: fp
# ── LSP guest-oracle door (doctrine XXXII, Slice 1, 2026-07-17) ──
# evict_all_projects moved to mcp/vulture_future_debt.py (#426): its
# production consumer (Slice 2 drain path) is coming — future debt.
edges_written  # noqa: F821 (field @ lsp/domain.py:122 — DrainReport.edges_written, the materialization count consumers read off the report; consumers today: tests/lsp/test_lsp_materialize.py asserts it (vulture does not scan tests); Slice-3 callers will log it. Sibling of the DrainReport contract fields.)

# @vulture-class: fp
# ── wave-2/3 wars (2026-07-18): staged contract + privileged surfaces ──
# Owner: conductor ubermega. Doctrine unchanged — each entry deletes in the
# commit that wires (or removes) its symbol.
INVALID  # noqa: F821 (enum member @ governance_contract.py:76 — MembershipState.INVALID, part of the sealed governance spec's exhaustive vocabulary; consumed by tests/governance/* 144-combo property matrix (vulture does not scan tests); Phase-3 consumers adopt the contract (#437).)
authorization_ready  # noqa: F821 (field @ governance_contract.py:133 — GovernanceDecision.authorization_ready, THE five-question answer field of the sealed spec; asserted throughout tests/governance/*; Phase-3/4 gates consume it (#437).)
stale_binding  # noqa: F821 (property @ governance_contract.py:138 — BindingState convenience projection per the spec's Stale Binding term; tests/governance vocabulary suite pins it; Phase-3 consumers (#437).)
verify_issue_hash  # noqa: F821 (function @ issue_filing_service.py:170 — the public integrity-verify API for immutable issues (#449); tests/security/test_ai_issues_filing.py exercises tamper detection (vulture does not scan tests); the superadmin triage flow (#449 v2) is the coming production caller.)
unbind_all_conductors  # noqa: F821 (method @ managed_mode_service.py:619 — the PRIVILEGED all-conductor unbind of the #438 split (admin-gated, fail-closed); tests/host/test_managed_mode_unbind.py proves refusal+success; the dashboard privileged control (#437 governance ops) is the coming caller.)
reconcile_outbox  # noqa: F821 (function @ sync_vps.py:289 — War H client half of the King-ruled VpsApiTransport (#442); tests/memory/test_vps_api_transport.py drives it against the double; the production caller lands with the server half (spec in the module docstring).)
ai_issues  # noqa: F821 (function @ tool_interface.py:2721 — the #449 registry stub, exact ai_backlog pattern (surface=BOTH); the registry/dispatcher consumes rows by scan, and tests/security/test_ai_issues_filing.py pins registry parity (vulture does not scan tests).)


# ── LEGACY GRANDFATHER (#427 audit 2026-07-18) ──
# Entries from the #330 bulk sweep whose section-level consumer claim was
# NOT confirmed by the mechanical wide scan (audit_vulture_allowlist.py +
# wide-root re-scan over server/tests/scripts/webapp/dashboard/benchmarks/
# third_party incl. dynamic/string hits). Unproven, not proven-dead: many
# are short names the scan cannot disambiguate, write-only dataclass
# fields, or consumers outside the scanned surfaces. Each needs a human
# verdict: prove the consumer (-> fp), declare the plan (-> foundation),
# queue the deletion (-> dead-pending), or delete symbol + entry.
# The COUNT of this section is PINNED (may only go DOWN) by
# mcp/tests/security/test_vulture_allowlist_classification.py.
# @vulture-class: legacy
_._last_user_prompt  # noqa: F821 (attribute @ claude_hook.py:67)
_.list_anchors_blocking_edit  # noqa: F821 (method @ anchor_store.py:373)
_.set_palace_disabled  # noqa: F821 (method @ palace_control_store.py:68)
target_artifact_terms  # noqa: F821 (variable @ recall_intent.py:158, server_recall_tools.py:559)
is_local_only  # noqa: F821 (function @ outer_gate_catalog.py:128, tool_interface.py:565) verify-before-delete



# ── wave-4 wars (2026-07-18, conductor triage of the live ai_slop scan) ──
# @vulture-class: fp
is_small  # noqa: F821 (property @ box_profile.py:66 — BoxProfile API consumed by tests/runtime/test_box_profile.py (worker-matrix pins); vulture does not scan tests.)
dwLength  # noqa: F821 (ctypes field @ box_profile.py:107 — MEMORYSTATUSEX protocol requires assigning dwLength before GlobalMemoryStatusEx reads it in-kernel; write-only by Win32 contract.)
reset_box_profile_cache  # noqa: F821 (function @ box_profile.py:179 — test-only cache reset, consumed by tests/runtime/test_box_profile.py; the process-wide cache must be droppable per test.)
backfill_empire_palace  # noqa: F821 (function @ empire_palace.py:392 — the one-shot empire-palace backfill (#375 phase 2, 55f8162c); consumed by tests/memory/test_empire_palace_phase2.py (vulture does not scan tests) and executed once for real on this machine (26 drawers); owner=memory-war; wire-by: the phase-3 operator surface (ai_palace_maintenance mode or CLI) recorded on #375.)
memory_class  # noqa: F821 (dataclass field @ box_profile.py:59 — the memory-classification axis of the BoxProfile record, pinned by tests/runtime/test_box_profile.py (memory-tightening matrix); vulture does not scan tests. Surfaced after describe()'s deletion removed its only in-module reader.)
reset_registry_cache_for_tests  # noqa: F821 (function @ task_actor_identity.py:58 — test-only cache reset, consumed by tests/security/test_task_actor_slots_and_lane_autobind.py.)
# @vulture-class: fp


# ── WAR AU (#467, 2026-07-18): causal-turn contract-freeze remainder ──
# @vulture-class: fp
# The frozen Causal Turn Context vocabulary (causal_turn_contract.py) is
# pinned member-by-member via set-iteration in
# tests/audit/test_causal_turn_state.py::TestVocabulary (vulture does not
# scan tests, and enum iteration is dynamic). One name per flagged member;
# duplicates (OPERATOR_OVERRIDE in InstructionKind AND CausalEdge) share
# one row.
OPERATOR_OVERRIDE  # noqa: F821
SYSTEM_CONSTRAINT  # noqa: F821
CONFIRMATION  # noqa: F821
RECOVERY_DIRECTIVE  # noqa: F821
DIRECT_USER_REQUEST  # noqa: F821
DERIVED_PLAN_STEP  # noqa: F821
REQUIRED_VERIFICATION  # noqa: F821
GOVERNANCE_REQUIRED  # noqa: F821
REPAIR_OR_RECOVERY  # noqa: F821
CO_CONDUCTOR_REDIRECT  # noqa: F821
PROVEN_NOT_EXECUTED  # noqa: F821
PROVEN_SUCCEEDED  # noqa: F821
PROVEN_FAILED  # noqa: F821
ALLOWED_AND_SUCCEEDED  # noqa: F821
ALLOWED_AND_FAILED  # noqa: F821
DENIED  # noqa: F821
CANCELED_BEFORE_EXECUTION  # noqa: F821
INTERRUPTED_DURING_EXECUTION  # noqa: F821
TIMED_OUT  # noqa: F821
CONNECTION_LOST  # noqa: F821
# @vulture-class: fp
# CausalTurnStore (causal_turn_store.py) frozen-entity API — consumed by
# tests/audit/test_causal_turn_state.py, test_causal_turn_seal.py and
# test_causal_interrupt_recovery.py (both-ways entity tests; vulture does
# not scan tests). Production consumers: open_turn is wired
# (session_query_gate_store.rotate_current_turn_id) — and #444 wired the
# TERMINAL half too: close_superseded_turn rides the same mint seam and
# aidocs_service.run_watchdog schedules recover_open_turns via
# recover_causal_turns, so get_turn / record_interrupt / recover_open_turns
# have REAL production callers now and are no longer listed here. The
# surfaces below still await the dispatch-chokepoint adoption phase recorded
# on #467 and are exercised end-to-end by the adversarial battery until then.
_.transition_turn  # noqa: F821 (method @ causal_turn_store.py)
_.record_instruction  # noqa: F821 (method @ causal_turn_store.py)
_.verify_instruction_chain  # noqa: F821 (method @ causal_turn_store.py)
_.mark_cancellation_observed  # noqa: F821 (method @ causal_turn_store.py)
_.list_interrupts  # noqa: F821 (method @ causal_turn_store.py)
_.get_seal  # noqa: F821 (method @ causal_turn_store.py)
_.verify_turn_seal  # noqa: F821 (method @ causal_turn_store.py)
# @vulture-class: foundation
# War AV (#375 phase 3, memory-home flip tranche 1, 8fc2ed93) — surfaces
# whose production callers are the RECORDED tranche-2 acts: drain_staged is
# the periodic heal for staged rows awaiting a live palace (wiring is a
# named tranche-2 item on #375); last_completed_run/migrate_bodies_to_palace
# are the one-shot stamped migrator the CONDUCTOR runs deliberately
# post-deploy (fixture-proven in tests/memory/test_memory_home_migrator.py,
# 6 tests); get_body_home is the staging-state accessor consumed by
# tests/memory/test_memory_body_staging_store.py (11 tests). Vulture does
# not scan tests.
_.drain_staged  # noqa: F821 (function @ memory_body_staging_store.py)
_.last_completed_run  # noqa: F821 (function @ memory_home_migrator.py)
_.migrate_bodies_to_palace  # noqa: F821 (function @ memory_home_migrator.py)
_.get_body_home  # noqa: F821 (method @ memory_sqlite_store.py)
# @vulture-class: foundation
# War AY (#472, f23b296f) — shell_command_insight is the tree-sitter-bash
# guest-oracle seam (doctrine XXXII): the bash grammar is not yet enrolled
# in this runtime, so parser_available and the heredoc_consumers insight
# field are exercised only by tests/security/test_shell_parser_oracle_472.py
# (divergence tests activate on grammar enrollment — named tranche-2 item
# on #472). Fail-open contract pinned there.
_.parser_available  # noqa: F821 (function @ shell_command_insight.py)
_.heredoc_consumers  # noqa: F821 (variable @ shell_command_insight.py)
# @vulture-class: foundation
# War AW (#183, b434fd77) — census_non_identifier_symbols is the index-
# hygiene census surface consumed by the stamped repair sweep
# (mcp/scripts/repair_outline_symbols_2026_07_19.py — scripts are outside
# vulture's package scope) and pinned both ways by
# tests/indexing/test_outline_symbol_hygiene.py.
_.census_non_identifier_symbols  # noqa: F821 (function @ symbol_hygiene.py)
# @vulture-class: foundation
# War R (#475, 7b2c51a7) — mint_session_scaffold_grant is the conductor's
# work-grant mint (reached via RuntimeService callers; the ai_seat/ai_session
# tool-surface exposure is a NAMED #475 tranche item) and
# compact_session_markdown is the stamped one-shot sweep the CONDUCTOR runs
# deliberately (real run 2026-07-19: 22,074 lines removed; fixture-proven by
# tests/runtime/test_session_scaffold.py + test_session_store.py 475 pins).
# Vulture does not scan tests or ad-hoc conductor invocations.
_.mint_session_scaffold_grant  # noqa: F821 (method @ runtime_service.py)
_.compact_session_markdown  # noqa: F821 (function @ session_md_compaction.py)
# @vulture-class: foundation
# War Z (#480, 38e1a2d8) — seed_doctrine_global_law is the deliberate
# bootstrap FORCE path (docstring marks it bootstrap-only); the routine
# path is the row-wise ensure. Consumed by
# tests/memory/test_doctrine_global_law_seed.py; Z's named follow-up is
# to RBAC-gate or deprecate direct use — until that ruling it stays a
# guarded entrypoint, not dead code.
_.seed_doctrine_global_law  # noqa: F821 (function @ doctrine_global_law_seed.py)
# @vulture-class: foundation
# War LL (#482, cdc5c135) — EMPTY_REASONS is the canonical empty-reason
# vocabulary {no_match, path_not_indexed, symbol_not_indexed,
# no_references, timed_out, pattern_invalid}: the single named source the
# per-mode envelopes cite and tests/indexing/test_ai_find_empty_reason.py
# pins membership against. Vulture does not scan tests; the constant IS
# the contract surface.
EMPTY_REASONS  # noqa: F821 (variable @ server_code_tools.py)
# @vulture-class: foundation
# War HH (Emperor tool_report charter) — list_reports is the read surface
# for the tool_usage_reports telemetry table: consumed by
# tests/runtime/test_tool_report_param.py today; the operator-facing
# review surface (dashboard/#183 aggregation reads) is the recorded
# consumer-to-come. Vulture does not scan tests.
_.list_reports  # noqa: F821 (method @ tool_usage_report_store.py)
# WAIVER RETIRED 2026-07-26 (#523 item A) — palace_ingest_from_canonical is
# WIRED again and needs no waiver: ai_palace_maintenance(mode='reingest_canonical')
# calls it from server_palace_tools.run_palace_maintenance. The ruling the old
# waiver was waiting on ("re-wire or fully retire") came out RE-WIRE, because it
# is not redundant with backfill_legacy_memory_drawers — it is also the #382
# palace-KG backfill adapter (writes extract_kg_facts into <palace>/kg.sqlite3,
# reports kg_triples), and the palace is a rebuildable projection whose graph
# would otherwise have no rebuild path.
# @vulture-class: foundation
# 442 (XAACP actor routing, 2026-07-20) — xaacp_resolve_caller_actor is the
# public caller-actor resolver for the XAACP surface; it has no in-source caller
# yet (the dispatch wiring is a named follow-up) but is part of the declared
# XAACP helper API. Kept, not deleted, pending that wiring.
_.xaacp_resolve_caller_actor  # noqa: F821 (function @ conductor_comms.py)
# @vulture-class: foundation
# 490/credential-fix (2026-07-21) — sync_tenant_clones_cli is the deploy
# script's entrypoint for credential-aware tenant convergence. It IS called,
# just not from Python: deploy_aidocs_gate.sh step 5 runs it on the VPS as
#   PYTHONPATH=<release>/server <gate>/current/.venv/bin/python3 -c
#   'from aidocs_mcp.outer_gate_projects import sync_tenant_clones_cli; ...'
# Vulture cannot see a shell callsite. It exists so the sweep authenticates via
# the JIT org credential instead of a PAT persisted in each clone's remote URL
# (the credential leak fixed the same day). Deleting it silently breaks private
# tenant sync on every deploy.
_.sync_tenant_clones_cli  # noqa: F821 (function @ outer_gate_projects.py)
# @vulture-class: foundation
# 504 (broker diagnostic reason, 2026-07-25) — evaluate_via_broker lost its LAST
# production caller when claude_hook moved to evaluate_via_broker_with_reason so
# the degraded banner could NAME why the broker did not answer. It is retained
# deliberately, not stranded: the module docstring declares this function's
# contract a TEST-PINNED SECURITY FLOOR (None selects the local fallback and
# never fails open), and tests/host/test_hook_broker.py pins that signature and
# semantics. The _with_reason variant holds the implementation and this stays a
# thin wrapper precisely so the pinned public contract is untouched. Vulture does
# not scan tests. Deleting it would break the floor's pins and remove the public
# name other surfaces may call.
_.evaluate_via_broker  # noqa: F821 (function @ hook_broker_client.py)
# @vulture-class: foundation
# 516 (grant-class split, 2026-07-25) — assign_role_to_user_by_name is the
# GLOBAL-scope role assignment. Its only production caller was the tenant RBAC
# bootstrap, and #516 correctly moved that to PROJECT scope, so it now has no
# in-source caller. HONEST NOTE recorded rather than glossed: #516's write-site
# audit labelled this "class 1 — operator break-glass / login reconciliation",
# but no production path currently issues a break-glass grant through it, so that
# label is aspirational and the class-1 route is presently TEST-ONLY.
# It is kept because three security suites depend on it and assert real
# properties: tests/security/test_auth_boundary_seal.py (:157-165, that assigning
# an unknown role and a duplicate both return False),
# test_identity_is_machine_global_488.py (:87, that a role granted under project
# A still holds under B — the class-1 law the operator ruled must be preserved),
# and test_war_u_411_super_admin_escape.py (that a resolved uid actually HOLDS
# the role). Vulture does not scan tests. Deleting it would delete the only
# mechanism the class-1 half of the operator's ruling is proven by.
_.assign_role_to_user_by_name  # noqa: F821 (method @ rbac_store.py:718)
# @vulture-class: fp
# 876 phase 1 (window-anchored identity, 2026-08-23) — the window axis is
# DERIVED, FORWARDED and RECORDED in this phase, and deliberately READ BY
# NOTHING that makes a decision; #880 (conversation leases) is where the readers
# land, and it carries the lockout risk. So the read side has test consumers
# only, and vulture does not scan tests. CONSUMERS, named:
#   * current_request_window_key — tests/runtime/test_daemon_records_the_window_876.py
#     (set/read/reset, per-request isolation, and the tripwire that FAILS when a
#     second reader appears, so phase 2 cannot begin by accident)
#   * resolve_window_key       — tests/host/test_window_key_876.py, and the
#     production caller stdio_shim.resolve_window uses derive_window_key
#     directly; this is the public one-value spelling the tests pin
#   * window_conversation      — tests/host/test_window_conversation_binding_876.py
#     (the honest "{}" for a window with no row)
#   * dwSize                   — NOT dead: PROCESSENTRY32.dwSize must be assigned
#     before Process32First or the Win32 call fails. Vulture cannot see a ctypes
#     struct field being read by the OS.
# WIRE-OR-RETIRE: delete these entries in the SAME change that wires phase 2.
_.current_request_window_key  # noqa: F821 (function @ mcp_server_runtime_helpers.py)
_.resolve_window_key  # noqa: F821 (function @ window_key.py)
_.window_conversation  # noqa: F821 (method @ window_binding_store.py)
_.dwSize  # noqa: F821 (ctypes struct field @ window_key.py)
# @vulture-class: fp
# 880 phase 2 (window_lease.py, 2026-08-23) — THE RESOLVER IS BUILT, TESTED AND
# DELIBERATELY UNWIRED. Its production caller is PATCH 3 (the lease becomes the
# authority in mcp_server._instrumented_call_tool), which is HELD BACK ON
# PURPOSE and preserved in `git stash`: the only MEASURED window derivation is
# win32, Linux carries an unverified host-process name, and macOS has none at
# all — so wiring it today would refuse every non-Windows Claude Code window.
# Every function below is exercised by
# mcp/tests/security/test_window_lease_is_the_authority_880.py (57 tests) and
# by mcp/tests/security/test_window_axis_readers_880.py, and vulture does not
# scan tests.
# WIRE-OR-RETIRE: delete these entries in the SAME change that lands PATCH 3.
# If PATCH 3 is ever abandoned rather than landed, DELETE THE MODULE — do not
# leave the allowlist holding up code with no future caller.
_.set_request_lease_reason  # noqa: F821 (function @ window_lease.py:229) owner=phoenix/#880 end_state=wired-by-PATCH-3-or-module-deleted — request-scoped stash so the refusal site and the identity stamp cannot disagree about WHY
_.reset_request_lease_reason  # noqa: F821 (function @ window_lease.py:234) owner=phoenix/#880 end_state=wired-by-PATCH-3-or-module-deleted — the release half; a leaked reason explains THIS refusal with the PREVIOUS request's cause
_.current_request_lease_reason  # noqa: F821 (function @ window_lease.py:242) owner=phoenix/#880 end_state=wired-by-PATCH-3-or-module-deleted — the read half
_.resolve_request_lease  # noqa: F821 (function @ window_lease.py:247) owner=phoenix/#880 end_state=wired-by-PATCH-3-or-module-deleted — window -> the conversation SessionStart bound to it; THE resolution, no ladder
_.lease_refusal  # noqa: F821 (function @ window_lease.py:262) owner=phoenix/#880 end_state=wired-by-PATCH-3-or-module-deleted — the refusal whose blocked_by/rule_id IS the missing axis, not a generic managed_mode_not_active
_.describe_lease  # noqa: F821 (function @ window_lease.py:285) owner=phoenix/#880 end_state=wired-by-PATCH-4-or-module-deleted — the ai_whoami surface; every value names its source
_.channels_agree  # noqa: F821 (function @ window_lease.py:308) owner=phoenix/#880 end_state=wired-by-PATCH-4-or-module-deleted — header vs LEASE, the real comparison; replaces a tautology that compared the header to itself and reported true while three rotations stale
# @vulture-class: fp
# AUDIT RETENTION (2026-08-23, commit 80e8c3b01) — the registry, its build-time
# scanner, and the reset/flush seams the tests drive. Consumers named, all under
# mcp/tests/ which vulture does not scan:
#   * RETIRED_EVENT_KINDS        — test_execution_event_retention_registry.py:55
#     (the escape hatch for kinds no longer emitted but still on disk; removing
#     an emitter does not remove its rows)
#   * undeclared_dynamic_sites   — ..._registry.py:43,137,151
#   * unclassified_event_kinds   — ..._registry.py:32,84 (the build-time test
#     that FAILS when a new event_kind is emitted without a classification)
#   * flush_retention            — test_execution_event_retention.py:330,355
#     (settles the background retention worker so a test can assert on it)
#   * index_reconcile_state      — test_index_sitter_heartbeat.py (the reader
#     half of the heartbeat that replaced 30,202 per-occurrence audit rows)
#   * reset_deferred_audits      — test_result_audit_nonblocking.py:114
#
# count_capped IS DIFFERENT AND IS NOT A CLEAN FALSE POSITIVE. It is read by
# tests only (5 assertions in ..._registry.py). PRODUCTION NEVER READS IT: the
# pruner derives count-capping independently, from
# `kinds_in_class(DECISION) + kinds_in_class(FORENSIC) + forensic_prefixes()`
# in `execution_index_store._retention_predicates`. So the same fact —— "which
# classes are exempt from the count cap" — has TWO HOMES, and they can drift
# apart silently: flipping this field fails the tests while changing nothing the
# pruner does, and editing the predicate changes behaviour while this field
# still claims otherwise. Allowlisted so the deploy is not blocked on a
# documentation-vs-behaviour split that predates it; the fix is to make the
# pruner read the policy, which is tracked separately. DO NOT treat this entry
# as evidence the field is load-bearing — it is evidence that it is not.
_.count_capped  # noqa: F821 (dataclass field @ execution_event_retention.py:67) owner=phoenix/#880 end_state=deleted-or-read-by-the-pruner — TWO HOMES: tests assert this field (5 assertions) while `_retention_predicates` derives the same rule from kinds_in_class(DECISION)+kinds_in_class(FORENSIC)+forensic_prefixes(). Not evidence the field is load-bearing; evidence it is not.
_.RETIRED_EVENT_KINDS  # noqa: F821 (constant @ execution_event_retention.py:422) owner=phoenix/#880 end_state=permanent-registry-surface — the escape hatch for kinds no longer emitted but still on disk; consumed by tests/audit/test_execution_event_retention_registry.py:55
_.undeclared_dynamic_sites  # noqa: F821 (function @ execution_event_retention.py:711) owner=phoenix/#880 end_state=permanent-build-time-guard — the AST scanner's dynamic-emitter half; tests/audit/..._registry.py:43,137,151
_.unclassified_event_kinds  # noqa: F821 (function @ execution_event_retention.py:721) owner=phoenix/#880 end_state=permanent-build-time-guard — FAILS THE BUILD when a kind is emitted with no retention class; tests/audit/..._registry.py:32,84
_.flush_retention  # noqa: F821 (function @ execution_index_store.py:184) owner=phoenix/#880 end_state=permanent-test-seam — settles the background retention worker so a test can assert on it; tests/audit/test_execution_event_retention.py:330,355
_.index_reconcile_state  # noqa: F821 (method @ execution_index_store.py) owner=phoenix/#880 end_state=production-caller-or-deleted — reader half of the index-sitter heartbeat that replaced 30,202 per-occurrence audit rows; tests/audit/test_index_sitter_heartbeat.py. Its WRITER is wired; if no production reader appears, delete both.
_.reset_deferred_audits  # noqa: F821 (function @ local_intent_audit.py:257) owner=phoenix/#880 end_state=permanent-test-seam — clears the deferred result-audit queue between tests; tests/audit/test_result_audit_nonblocking.py:114
# @vulture-class: fp
# 2026-07-27 — MEMO-CACHE RESET/INTROSPECTION HELPERS. Each one exists so a test
# can clear or inspect process-level memoized state that would otherwise leak
# BETWEEN tests and make the perf work unprovable. Vulture's scan root is
# mcp/server/aidocs_mcp, so it cannot see any of these consumers. Each entry
# names the file that calls it; deleting the helper deletes the only way its
# invariant is proven.
#   connect_cache_clear  — tests/security/test_sqlite_connect_hot_path.py
#                          (:93,:142,:165,:195 — resets the WAL/dirs memo so each
#                          case measures a cold connect, not the previous test's)
#   _scope_pool_size     — tests/security/test_sqlite_connection_scope.py
#                          (:219,:222,:236,:250,:255,:285 — asserts the checkout
#                          pool actually returns to 0, i.e. no leaked connection)
#   schema_ready_clear   — tests/security/test_schema_ensure_once.py
#                          (:44,:69,:95,:113,:124,:133,:155 — proves the schema is
#                          ensured ONCE per process, which needs the memo cleared)
#   scan_skill_cache_*   — tests/security/test_skill_scan_memoization.py
#                          (stats at :47-:51,:66,:156,:160 reads hits/misses and
#                          the LRU bound; clear resets between cases)
_.connect_cache_clear  # noqa: F821 (method @ _sqlite_index_store_base.py:41)
_._scope_pool_size  # noqa: F821 (method @ _sqlite_index_store_base.py:142)
_.schema_ready_clear  # noqa: F821 (method @ session_query_gate_store.py:81)
_.scan_skill_cache_stats  # noqa: F821 (function @ skill_scanner.py:249)
_.scan_skill_cache_clear  # noqa: F821 (function @ skill_scanner.py:259)
# @vulture-class: fp
# 2026-07-27 — PER-SURFACE ToolSpec ACCESSORS. tier_for/scope_for resolve a
# tool's tier/scope for a given surface (gate_tier or gate_scope when set, else
# the local value). Consumed by tests/security/test_tool_spec_per_surface.py
# (tier_for :60,:70,:103,:107,:130,:139; scope_for :61,:71,:104,:108,:131),
# which pins the override semantics AND cross-checks spec.gate_tier against
# outer_gate_manifest.MCP_TIER_OVERRIDES (:285-:298) so the two cannot drift.
#
# HONEST NOTE, recorded rather than glossed: gate_tier/gate_scope currently have
# NO production consumer — the gate resolves tiers from MCP_TIER_OVERRIDES, so a
# per-surface tier declared on a @tool decorator does not yet change gate
# behaviour. These accessors are the SSOT half of a parity-checked pair, not a
# wired feature. Wiring the gate to read tier_for(SURFACE_GATE) changes the
# security projection (and its golden), so it is filed as its own item rather
# than smuggled in here. Keeping them is what keeps the parity test honest.
#
# 2026-07-29 (#558) — SECOND NAMED CONSUMER: tests/security/
# test_tier_ssot_migration_558.py calls tier_for(SURFACE_GATE) to MEASURE the
# gate-vs-SSOT distance (14 divergent tools, 37 registry-less overrides, 147
# discovered tools with no ToolSpec). That file also records why the retirement
# is still blocked: deleting MCP_TIER_OVERRIDES today loosens 11 tools and
# leaves 66 with no tier at all.
_.tier_for  # noqa: F821 (method @ tool_interface.py:229)
_.scope_for  # noqa: F821 (method @ tool_interface.py:235)

# @vulture-class: fp
# ── #461 WAR O — the shared-truth registry (the meta-parity mechanism) ──
#
# The registry is DECLARATION-AS-DATA plus pure functions over the source
# tree. Its consumers are, by design, a test suite and a build-time script —
# so vulture cannot see either of them:
#
#   tests/security/test_shared_truth_edge_contracts.py     — the reverse test
#       ("every registered shared truth has >=1 contract test"), the whole
#       point of the module; imports SHARED_TRUTHS, audit, render_summary,
#       contract_test_paths, resolve_declaration, is_security_surface and
#       reads every TruthFinding field.
#   tests/security/test_vulture_security_stewardship.py    — the allowlist
#       owner/end-state gate.
#   mcp/scripts/vulture_allowlist_classify.py:_load_security_predicate —
#       imports is_security_surface through a sys.path insert, which is
#       invisible to a static scan. That import IS the edge: a local copy of
#       the predicate in the build-time script would be the very "one truth,
#       N unconsumed copies" defect this registry exists to detect.
#
# The dataclass fields (consumers/disease/present_tests/missing_tests/
# consuming_tests) are the AUDIT PAYLOAD: they are what makes a failure
# actionable rather than a bare boolean, and they are asserted in the suite.
#
# NOTE, and it is the mechanism proving itself: four of these entries land on a
# security surface only because this very comment says "audit". The predicate
# is deliberately generous — a false positive costs one annotation, a false
# negative silences the only detector that finds a caller-less control. The
# correct response is to ANNOTATE, never to reword the comment until the
# predicate stops seeing it. Dodging the scope to go green is the #624 escape
# hatch, and it is exactly what #461 was filed about.
consumers  # noqa: F821 (variable @ shared_truth_registry.py:111) owner=#461 end_state=dies-with-the-registry — SharedTruth evidence field naming the surfaces that must agree; asserted by test_every_truth_names_more_than_one_consumer
disease  # noqa: F821 (variable @ shared_truth_registry.py:113) owner=#461 end_state=dies-with-the-registry — SharedTruth evidence field recording which #461 cause the truth belongs to
present_tests  # noqa: F821 (variable @ shared_truth_registry.py:372) owner=#461 end_state=dies-with-the-registry — TruthFinding audit payload; makes a failure actionable instead of a bare boolean
missing_tests  # noqa: F821 (variable @ shared_truth_registry.py:373) owner=#461 end_state=dies-with-the-registry — TruthFinding audit payload; names the contract files that vanished
consuming_tests  # noqa: F821 (variable @ shared_truth_registry.py:374) owner=#461 end_state=dies-with-the-registry — TruthFinding audit payload; names the suites that actually import the declaration
contract_test_paths  # noqa: F821 (function @ shared_truth_registry.py:544) owner=#461 end_state=dies-with-the-registry — names the suites behind `pytest -m edge_contract`
render_summary  # noqa: F821 (function @ shared_truth_registry.py:555) owner=#461 end_state=dies-with-the-registry — operator-readable audit block; asserted by the reverse test

# This one is on a SECURITY surface by its own predicate, so it carries the
# owner + end state its own rule demands — the mechanism biting on the module
# that hosts it, which is the cheapest possible proof that it bites at all.
is_security_surface  # noqa: F821 (function @ shared_truth_registry.py:345) owner=#461 end_state=static-caller-or-deleted — consumed by vulture_allowlist_classify._load_security_predicate via a sys.path import vulture cannot see, and by both #461 suites. Done-when: the classifier becomes a package-internal module so the import is statically visible, or the predicate moves to the caller and this entry dies with it.


# @vulture-class: foundation
# ── D5 — the receipted law-projection ledger, deliberately unwired ──
#
# NOT a false positive, and it must never be filed as one: these three have
# ZERO production callers and vulture is RIGHT. `fp` asserts "vulture is wrong,
# the symbol IS consumed" — the exact mislabel that parked the dead three-phase
# security helper at :429 and silenced cause B's only detector. So they sit
# under `foundation`: retained on purpose, with an owner and a real wiring
# condition, and they die with their plan.
#
# WHY UNWIRED IS THE CORRECT STATE TODAY. The projector refuses unless the
# doctrine-residency lane's composite `admit_law_body(...)` admits — and
# `admit_law_body` DOES NOT EXIST YET. It is a deferred import behind a
# try/except at law_projection_ledger.py:184 (`from .doctrine_residency import
# admit_law_body`), and `doctrine_residency` has zero hits repo-wide. Wiring the
# ledger into the canonisation write door today would therefore make EVERY live
# skill edit refuse — an outage, not a hardening. Consumed meanwhile only by
# tests/memory/test_law_projection_ledger.py (which vulture does not scan) and,
# for the tier guard, tests/memory/test_sync_direction_amendment3_645.py.
project_law_body  # noqa: F821 (function @ law_projection_ledger.py:208) owner=D5-doctrine-residency end_state=wired-at-canonisation-write-door — the receipted projection door. Done-when: the doctrine-residency lane lands `admit_law_body` and empire_skill_upsert calls this instead of writing the body directly — one line at the canonisation write door. Until then it must stay unwired or every live skill edit refuses.
detect_law_drift  # noqa: F821 (function @ law_projection_ledger.py:380) owner=D5-doctrine-residency end_state=wired-at-canonisation-write-door — the ledger's drift read (projected body vs current body). Done-when: it lands with `project_law_body` at the same write door in the same commit; a projector with no drift read is a receipt nobody checks.
refuse_cross_tier_fold  # noqa: F821 (function @ law_projection_ledger.py:424) owner=D5-doctrine-residency end_state=wired-at-fold-events-call-site — the tier-pair guard (#645 amendment 3): an empire law must never be folded away by a kingdom row carrying a newer clock. Done-when: the `fold_events` call site consults it; that site is still direction-blind last-write-wins, so this guard is written and inert BY RECORD, not by oversight.


# @vulture-class: foundation
# ── #440 — the ShadowObservation payload, observing before it enforces ──
#
# Three fields of governance_contract.ShadowObservation (:326). Vulture is
# right: nothing in production reads them yet. They exist to OBSERVE before
# enforcing because this gate decides whether the whole managed surface answers
# — a wrong verdict is a total outage, not a narrow denial — so #440's own
# phased plan is: publish the mismatch rate, flip only on zero unexplained
# mismatches. The fields ARE constructed today (governance_contract.py:388-392)
# and asserted by tests/governance/test_commission_carrier.py; no non-test
# reader consumes them. A shadow record with no reader is the honest state of
# phase 1, recorded here rather than papered over as a false positive.
in_force_verdict  # noqa: F821 (variable @ governance_contract.py:341) owner=#440 end_state=consumed-in-shadow-then-enforced — the verdict production actually returned; shadow mode never alters it. Done-when: is_aidocs_managed / agent_orchestrator emit the observation in shadow, then the flip lands on a clean shadow run.
would_be_verdict  # noqa: F821 (variable @ governance_contract.py:347) owner=#440 end_state=consumed-in-shadow-then-enforced — the verdict enforcement WOULD return post-flip; observing this before enforcing is the whole design. Done-when: the same shadow emission lands, then enforcement.
enforcement_would_change  # noqa: F821 (variable @ governance_contract.py:349) owner=#440 end_state=consumed-in-shadow-then-enforced — the mismatch bit itself; this is the number the flip decision is made on. Done-when: the shadow run shows no mismatch on healthy reads and the gate enforces.


# @vulture-class: foundation
# ── #529 — the revocation seam, waiting on a channel that does not exist ──
#
# Vulture is right: no production caller. The revocation channel itself is not
# built — today a 30-day token cannot be invalidated by a ban or a project
# removal, which is precisely why the seam was cut now and left visible rather
# than left implicit. REPORTABLE, never enforcing by contract (its own
# docstring): a caller may surface staleness, refuse a GRANT, or refetch; it may
# NOT sign the operator out. Asserted by tests/governance/
# test_revocation_projection.py:115,:125, which vulture does not scan.
projection_freshness_is_unresolved  # noqa: F821 (function @ identity_store.py:209) owner=#529 end_state=consumed-by-pull-based-revocation-refresh — true iff a RevocationProbe cannot vouch for its own freshness. Done-when: the pull-based revocation refresh under #662's projection model calls it on the read path; if #662 lands without a revocation channel, this and the probe die in one commit.


# @vulture-class: foundation
# ── #624 — the taxonomy parity accessors (consumer is the parity gate) ──
#
# Vulture flags these because their ONLY consumer is a test file, outside its
# scan roots — VERIFIED, not assumed: tests/security/
# test_taxonomy_pattern_parity_624.py imports both at :39-:40 and drives them as
# parametrize sources (declared_shapes :64; known_uncovered_shapes :147, plus
# len() at :177 for the uncovered count). `known_uncovered_shapes` is also the
# registered "one door" for that truth in shared_truth_registry.py:165-168.
#
# Recorded plainly rather than filed as `fp`: consumed only by tests is NOT by
# itself grounds for the allowlist here (vulture_future_debt.py's own ruling on
# a safety-floor validator), and these sit on a judge/taxonomy surface. They are
# kept because the accessor IS the parity mechanism — but that earns an owner
# and an end state, not a claim that vulture was mistaken.
known_uncovered_shapes  # noqa: F821 (function @ judge_taxonomy.py:445) owner=#624 end_state=consumed-by-test_taxonomy_pattern_parity_624 — the declared-but-unmatched shapes; the uncovered ratchet reads it. Done-when: the uncovered set reaches zero and the accessor plus this entry are deleted together.
declared_shapes  # noqa: F821 (function @ judge_taxonomy.py:454) owner=#624 end_state=consumed-by-test_taxonomy_pattern_parity_624 — the read door onto DECLARED_COMMAND_SHAPES that the parity gate parametrizes over. Done-when: it gains a production reader (a judge enumerating its own declared shapes), or it dies with the parity gate.


# @vulture-class: foundation
# ── #665 — the output-status vocabulary, closed but not yet universal ──
#
# The tuple is the CLOSED vocabulary of shell_egress output_guard verdicts,
# EXTENDED (never forked, §XXII) with `withheld_truncated` so a tree-killed
# timeout stops lying by returning the silent `not_executed` default. Vulture is
# right that nothing in production reads the tuple itself: production reads the
# individual constants, and the tuple is the closure proof — asserted by
# tests/security/test_shell_egress_chokepoint_doctrine.py:511-522 (every status
# seen is a member) and :1204-1210 (the sixth member is IN the vocabulary), and
# named by the code_runner.py:74 and shell_egress_service.py:854 contracts.
OUTPUT_STATUSES  # noqa: F821 (variable @ shell_egress_service.py:503) owner=#665 end_state=every-call-site-reports-output_status — the closed status vocabulary. Done-when: every egress call site reports an explicit `output_status` (some paths still fall through to the default) and the tuple becomes a runtime validation input rather than a test-only closure proof.


# @vulture-class: foundation
# ── #651 — cross-turn scrutiny ships its REFUSAL path only ──
#
# Vulture is right; `fp` would be a lie. Tier 2 ACCUMULATION is gated on a
# stable session identity, and `correlate_host_session` binds only when exactly
# one live binding exists with no worker lane running — on this box it usually
# refuses. So the module in production takes the refusal branch: it declines to
# accumulate rather than accumulate into a wrong or shared bucket. These two are
# the accumulation half's vocabulary and its ledger accessor, and they are
# unreferenced BECAUSE the half that would read them is not switched on yet.
# Consumed by tests/security/test_cross_turn_scrutiny_651.py, which vulture does
# not scan (ACCUMULATION_STATES :245,:246,:428; total_rows :317).
ACCUMULATION_STATES  # noqa: F821 (variable @ cross_turn_scrutiny.py:111) owner=#651 end_state=consumed-when-tier2-accumulation-enabled — the closed set of honest observe_prompt outcomes; it has no "skipped" and no "exempt" member by #615 rule, and the test pins that absence. Done-when: Tier 2 accumulation runs on a stable session key under #599's C-layer and a caller validates against this vocabulary.
_.total_rows  # noqa: F821 (method @ cross_turn_scrutiny.py:734) owner=#651 end_state=consumed-when-tier2-accumulation-enabled — the window-ledger accessor that proves a REFUSED accumulation wrote nothing, anywhere. Done-when: Tier 2 accumulation is enabled and this becomes the live diagnostic for the store it counts; it dies with the accumulation half if that half is abandoned.


# @vulture-class: foundation
# ── #440 — STORE_UNREADABLE, the fail-open this contract exists to close ──
#
# THIS ONE IS THE POINT OF THE FIX, so it is recorded as such rather than as a
# spare enum member. `_has_commission_stamp` swallowed `sqlite3.Error` into
# `False` — a store that did not ANSWER was recorded as a store that answered
# "no". A carrier with fewer states than the outcome it reports is the defect;
# `bool` could not express "unknown", so unknown became a decision.
# STORE_UNREADABLE is the third state that makes "the store did not answer"
# representable, and it maps to INVALID_AUTHORITY, never UNCOMMISSIONED —
# asserted by tests/governance/test_commission_carrier.py:53,:77,:89.
#
# DELETING IT WOULD RESTORE THE FAIL-OPEN. It is unreferenced in production for
# exactly the same reason as the three ShadowObservation fields above: the
# carrier is built and observed before it is enforced. Same owner, same end
# state, deliberately worded to read as one group with them.
STORE_UNREADABLE  # noqa: F821 (variable @ governance_contract.py:303) owner=#440 end_state=consumed-in-shadow-then-enforced — the CommissionReadOutcome member for "the store did not answer", mapping to INVALID_AUTHORITY rather than UNCOMMISSIONED. Done-when: read_commission_state is wired into is_aidocs_managed in shadow, then enforced on a clean shadow run.


# @vulture-class: fp
# ── #627 phase 3: the build stamp's WRITER ──
# `write_build_stamp` is consumed at mcp/scripts/build_signed_release.py:227
# (imported :169) — mcp/scripts/ is OUTSIDE vulture's scan roots
# (mcp/server/aidocs_mcp + this file), so the caller is invisible to it. Also
# pinned by five assertions in mcp/tests/security/test_build_stamp_627.py,
# including an ORDERING pin (:310) that the stamp is written BEFORE the
# fingerprint is computed. If this were truly unreferenced the artefact would
# ship with no stamp at all and #627's whole point — a component that can say
# what it was built from — would be silently absent.
write_build_stamp  # noqa: F821


# @vulture-class: fp
# ── #279 cross-session WRITE guard, superseded by #672's three-way ──
# `is_foreign_session_workspace` is consumed by
# mcp/tests/security/test_session_artifact.py:112 (a test outside vulture's
# scan roots). PRODUCTION no longer calls it: #672 replaced the boolean pair
# with `session_workspace_ownership()` because ownership is a THREE-WAY fact —
# own / foreign / UNESTABLISHED — and the old boolean asserted "another
# session's" on False even when the caller had no usable id at all (the
# mixed-axis bag's dead slug element defeated the emptiness guard). Retire this
# entry together with its test when #672's carrier is fully retired.
is_foreign_session_workspace  # noqa: F821


# @vulture-class: dead-pending
# ── #279 own-workspace predicate, fully superseded by #672 ──
# `is_own_session_workspace` has ZERO references — no production caller, no
# test. The only surviving mention is a comment at access_gate.py:1055-1061
# describing it in the PAST TENSE: "the old code asked is_own_session_workspace()
# and, on False, asserted 'another session's'". #672 replaced it with the
# three-way `session_workspace_ownership()`.
# NOT deleted in this commit because session ownership is a SAFETY-FLOOR surface
# and this was found mid-deploy: deleting a floor predicate without the operator
# present is the wrong trade. Queued behind sign-off; delete with its sibling.
is_own_session_workspace  # noqa: F821


# @vulture-class: fp
# ── #757 process-lifetime API + the ctypes field that IS the guarantee ──
# CONSUMERS, both outside vulture's scan root (mcp/server/aidocs_mcp):
#
# `persistent` — mcp/tests/security/test_process_lifetime_757.py:90, 101 and
# 167 call `Lifetime.persistent(...)`. It is the named opt-out the operator
# ruling requires ("tools should have their lifetime, i give life, not take
# it"): unbounded runs must be asked for BY NAME and never be the default.
#
# `LimitFlags` — a ctypes struct FIELD, consumed by Win32 itself: it is read
# out of the buffer that `SetInformationJobObject(..., byref(info), ...)`
# hands to the kernel at process_lifetime.py:154. Assigning it is what sets
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, i.e. the entire kill-the-tree
# guarantee. Vulture cannot see a write consumed through a byref buffer.
# DELETING THIS LINE WOULD SILENTLY DISABLE #757 while every test that
# asserts on the job object still passed at construction time.
persistent  # noqa: F821
LimitFlags  # noqa: F821


# @vulture-class: fp
# ── #755 sqlite chokepoint: the memo's reset seam ──
# CONSUMER: mcp/tests/security/test_schema_memo_survives_file_replacement_755.py
# (its `project` fixture clears the memo before and after every test), which is
# outside vulture's scan root.
#
# CORRECTION, 2026-08-12. This entry previously said "ZERO references... dead
# code, delete with #755's sign-off". That diagnosis was WRONG and worth keeping
# the record of: schema_memo_clear was never dead code, it was an UNWIRED SAFETY
# SEAM. Vulture reported the symptom accurately — nothing called the thing that
# invalidates the schema memo — and I wrote the wrong cause next to it. Hours
# later Gate 2b failed 39 times with `no such table`, which is exactly what an
# un-invalidatable schema memo produces. An allowlist entry asserting "dead"
# over a live safety mechanism is worse than no entry: it tells the next reader
# the question is already settled.
schema_memo_clear  # noqa: F821


# @vulture-class: fp
# ── #754 part B — deferred empire-audit mirror: TEST-ONLY OBSERVABILITY ──
# Both are consumed by mcp/tests/runtime/test_empire_mirror_deferred_754.py,
# which is outside vulture's scan roots (it scans server/aidocs_mcp + this
# file). They exist SO the deferral can be tested at all: a queue you cannot
# measure is a queue you cannot prove empties, and the load-bearing test
# (test_nothing_is_dropped_under_load, 327 events at 5x the batch threshold)
# accounts for every row through exactly these two.
#   mirror_queue_depth   -> test_empire_mirror_deferred_754.py:79,161,176,190
#   drop_pending_mirrors -> test_empire_mirror_deferred_754.py:51 (fixture reset)
mirror_queue_depth  # noqa: F821
drop_pending_mirrors  # noqa: F821

# @vulture-class: fp
# ── local backlog 984 — a RETIRED primitive kept as a LOUD TOMBSTONE ─────
# `prune_dead_conductor_bindings` deleted per-conductor bindings whenever the
# pid in `bound_by_boot_token` was not alive — and that pid is the MCP SERVER's
# OWN. Daemon lifetime is not actor lifetime, so it unbound every connected
# WebMCP agent on every restart. Measured live on the gate: one conversation
# lost its row while another, stamped by the running daemon, survived.
#
# Operator ruling 2026-08-30: "RETIRE IT rather than leave a dangerous unused
# primitive." It is RETAINED — raising NotImplementedError that NAMES the
# invariant — rather than deleted outright, for two reasons vulture cannot see:
#   * any surviving caller fails LOUDLY instead of quietly finding a name that
#     no longer exists;
#   * the reasoning stays attached to the thing it retired, which is where the
#     next person wanting a pid-based prune will actually read it.
#
# NOT dead in the sense vulture means: tests/runtime/
# test_prune_dead_conductor_bindings.py exercises it as a retirement guard (it
# raises, mutates nothing before raising, stays inert on repeat) and an AST scan
# there PROVES no production caller exists. This entry RECORDS that evidence; it
# is not a waiver of it — if the guard is deleted, this comment is a lie.
prune_dead_conductor_bindings  # noqa: F821

