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
# ── ai_deploy_daemon back-compat param ──
# `deploy_runner` is asserted directly by test_ai_deploy_daemon.py for
# back-compat callers. Keep until that test is retired.
deploy_runner  # noqa: F821


# @vulture-class: foundation
# ── co_conductor deferred stub API ──
# `cot_excerpt` is part of the co-conductor-deferred stub API surface.
# Keep until the stub is implemented or removed.
cot_excerpt  # noqa: F821


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
compute_webapp_bundle_drift  # noqa: F821 (function @ webapp_bundle_drift.py:58) deploy tooling — called by mcp/scripts/check_webapp_bundle.py + security tests
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
_invalidate_cache  # noqa: F821 (function @ _dev_trace.py:99)
check_bash_allowed  # noqa: F821 (function @ access_gate.py:1013)
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
_.revoke_all_tokens  # noqa: F821 (method @ identity_store.py:402)
_.set_disabled  # noqa: F821 (method @ identity_store.py:456)
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
tier_m_execution_authorized  # noqa: F821 (function @ outer_gate_execution_authority.py:61)
VALID_TIERS  # noqa: F821 (variable @ outer_gate_manifest.py:48)
VALID_DATA_SCOPES  # noqa: F821 (variable @ outer_gate_manifest.py:49)
KNOWN_DUPLICATES  # noqa: F821 (variable @ outer_gate_manifest.py:58)
discover_registered_tools  # noqa: F821 (function @ outer_gate_manifest.py:715)
manifest_mcp_names  # noqa: F821 (function @ outer_gate_manifest.py:893)
remote_manifest  # noqa: F821 (function @ outer_gate_manifest.py:900)
FORBIDDEN_WORKER_ENV  # noqa: F821 (variable @ outer_gate_sandbox.py:46)
reap_stale_workers  # noqa: F821 (function @ outer_gate_sandbox.py:340)
minted_by  # noqa: F821 (variable @ outer_gate_token_store.py:115)
_.revoke_refresh_for_user  # noqa: F821 (method @ outer_gate_token_store.py:430)
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
three_phase_audited_execute  # noqa: F821 (function @ outer_gate_audit.py:58)
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
_.validate_worker_token  # noqa: F821 (method @ outer_gate_sandbox.py:228) zero refs 2026-07-12; outer_gate_* = safety floor
tenant_projects_dir  # noqa: F821 (function @ outer_gate_tenancy.py:84) zero refs 2026-07-12; outer_gate_* = safety floor
_.purge_expired  # noqa: F821 (method @ outer_gate_token_store.py:554) zero refs 2026-07-12; outer_gate_* = safety floor (token hygiene)
require_password_for_gate  # noqa: F821 (function @ permission_catalog.py:442) zero refs 2026-07-12; RBAC/auth surface — owner sign-off
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
CAPSULE_FIELDS  # noqa: F821 (variable @ scripts/crown_receipt.py:44 — WIRED by the #354 war (2026-07-18): validate_capsule_fields enforces the exact field set on every capsule canonicalization (operator_capsule), fail-closed both directions; tests: test_crown_receipt.py::test_capsule_*. Now genuinely referenced — the next vulture sweep should stop reporting it, at which point this row can be dropped.)


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
# (session_query_gate_store.rotate_current_turn_id); the surfaces below
# await the dispatch-chokepoint adoption phase recorded on #467 and are
# exercised end-to-end by the adversarial battery until then.
_.get_turn  # noqa: F821 (method @ causal_turn_store.py)
_.transition_turn  # noqa: F821 (method @ causal_turn_store.py)
_.record_instruction  # noqa: F821 (method @ causal_turn_store.py)
_.verify_instruction_chain  # noqa: F821 (method @ causal_turn_store.py)
_.record_interrupt  # noqa: F821 (method @ causal_turn_store.py)
_.mark_cancellation_observed  # noqa: F821 (method @ causal_turn_store.py)
_.list_interrupts  # noqa: F821 (method @ causal_turn_store.py)
_.get_seal  # noqa: F821 (method @ causal_turn_store.py)
_.verify_turn_seal  # noqa: F821 (method @ causal_turn_store.py)
_.recover_open_turns  # noqa: F821 (method @ causal_turn_store.py)
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
