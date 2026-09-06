"""Vulture FUTURE-DEBT surface — code intentionally not yet wired (#426).

Doctrine (king, 2026-07-17): TWO suppression surfaces feed the deploy
gate's vulture lane (Gate 1d):
  - mcp/vulture_allowlist.py   = FALSE POSITIVES ONLY. Vulture is WRONG:
      the symbol IS consumed (tests, dynamic dispatch, getattr). Hidden
      from deploy output.
  - mcp/vulture_future_debt.py = THIS FILE. Vulture is RIGHT that no
      production consumer exists — but the absence is INTENTIONAL and
      tracked. Every entry carries a `direction=` marker + evidence:
        direction=add    -> a consumer is coming (staged wiring, next slice)
        direction=remove -> the symbol is scheduled for deletion
      These entries ALWAYS SURFACE in the deploy report as non-blocking
      "future debt" lines (the Gate-1d future-debt ledger, appended to
      mcp/.deploy-reports/vulture.summary.txt) — never hidden, never
      blocking.
  Anything matching NEITHER surface = a bug (hard-fail, as always).

THE DEBT ITSELF NOW LIVES IN THE BACKLOG (operator ruling 2026-07-25:
"move everything from vulture debt into backlog"). Each entry below is a
POINTER, not a narrative: the evidence, the reason the absence is
intentional, the named coming consumer and the done-when all live in the
referenced ai_backlog item, where they can be prioritised, assigned and
closed like any other work. A comment in a suppression file cannot be
scheduled; a backlog item can.

Entries stay here only because vulture must see the symbol referenced or
the gate hard-fails — this file is scanner plumbing, and the backlog is
the source of truth. When the tracked consumer lands (direction=add) or
the deletion ships (direction=remove), the entry dies in the SAME commit
that closes its backlog item.

Usage: vulture mcp/server/aidocs_mcp mcp/vulture_allowlist.py mcp/vulture_future_debt.py

This file is consumed by vulture only; never imported by runtime code.
"""

# ── #1030 blue/green runtime generations: the pointer authority landed first ──
# runtime_generations.py + claude_hook_shim's independent pointer read are the
# LAUNCH side, exercised by tests/host/test_runtime_generation_pointer_1030.py.
# The BUILD side landed too: runtime_provisioner builds a generation, verifies
# it, seals it and flips the pointer.
# (the four #1030 entries that stood here died with the commit that gave them
# production consumers — runtime_provisioner now builds, seals and activates
# generations, exactly as direction=add promised)

require_epoch  # noqa: F821 (direction=add — backlog #525: fail-CLOSED epoch guard with no production consumer; flip dnt_banner/helper_skill/read_memory_surfacer/agent_audit off fail-open current_epoch)
# PREMISE CORRECTED 2026-08-28, direction FLIPPED add -> remove. The old text
# read "the #283 session-select wiring never landed" — FALSE in current source:
# session selection landed through a DIFFERENT STORE entirely, so this setter is
# not awaiting a consumer.
#
# AND THE DEAD THING IS BIGGER THAN THIS SYMBOL, traced the same day:
#   WRITE  outer_gate_transport.py:2672  set(..., session_id="")  — always empty,
#          deliberately ("Project selection … CLEARS any prior selected session")
#   READ   outer_gate_transport.py:5198  reads _bind["org_id"] ONLY
# Nothing writes a non-empty session_id into this store and nothing reads the
# field. GateBindingStore's whole SESSION DIMENSION is vestigial — the setter,
# the column, and the explicit ""-argument at the single write site.
#
# So the real change is schema-shaped (set() signature, get()'s dict shape, four
# field tests), not "delete a method", and it is tracked on #526 as such rather
# than done inside a debt sweep. The entry stays until that lands.
set_session  # noqa: F821 (direction=remove — backlog #526: vestigial; GateBindingStore's session dimension is written empty and never read, and goes as one piece)
# PREMISE CORRECTED 2026-08-28, direction FLIPPED add -> remove. The old text
# read "awaiting the Slice 2 drain path" — that wait ENDED: Slice 2 landed and
# drains via `client.evict(project_root)`, per-project, never through this
# door-level helper. So no consumer is coming.
#
# BUT IT IS NOT DELETABLE, and an earlier note in this file said it was —
# corrected the same day after reading the test BODY instead of its call sites.
# test_lsp_door_fail_open.py:20-26 uses it in an AUTOUSE TEARDOWN FIXTURE, not
# as a subject: it clears the module-level _POOL before and after every test in
# the file. `client.evict(project_root)` CANNOT substitute — that drops ONE
# project's server, while this takes _POOL_LOCK and clears the whole pool. The
# function's own docstring says what it is: "Tear down every pooled server (TEST
# HELPER + process shutdown)."
#
# THE CLASSIFICATION IS GENUINELY AWKWARD, recorded rather than forced: vulture
# is RIGHT (no production consumer), so it is not an allowlist false positive;
# and "consumed only by tests" is explicitly NOT grounds for the allowlist here
# (see the active_keys entry below, citing test_operator_invalidation). But
# "future debt" means "a consumer is COMING", and none is. It is TEST-ONLY
# INFRASTRUCTURE and this project has no surface that says so. direction=remove
# stands — it should eventually go — but the blocker is a TEST-ISOLATION
# SUBSTITUTE, not a production consumer.
evict_all_projects  # noqa: F821 (direction=remove — backlog #527: test-only pool reset; Slice 2 landed via client.evict(project_root), but deleting this breaks test isolation until a substitute exists)
# invalidate_operator: ENTRY RETIRED 2026-08-27 (#529 step 3). The tracked
# consumer LANDED — IdentityStore.validate_token now executes the revocation
# on an affirmative REVOKED verdict instead of merely refusing one request —
# so vulture is no longer right that no production consumer exists, and this
# file's own rule ("the entry dies in the SAME commit that closes its backlog
# item") retires it. #529 stays OPEN for the gate-side channel; the surviving
# REVOCATION_LIVE entry below is that remaining half.
REVOCATION_LIVE  # noqa: F821 (direction=add — backlog #529: the three-state contract cannot express "still live" until that channel answers)
# ground: OrgAdminVerdict.ground (#630 instance 1, landed ba372a6c1). Vulture is
# RIGHT — tests read it, no PRODUCTION code does, and this file's surface is for
# exactly that. NOT the allowlist: claiming it is consumed would be false.
# CAVEAT, recorded because it is a real cost of this entry: `ground` is a common
# identifier, so this reference masks any OTHER unused variable of that name.
# It is the narrowest available lever — vulture matches by NAME — and the entry
# dies with the consumer, but until then a second `ground` elsewhere goes unseen.
ground  # noqa: F821 (direction=add — backlog #630: named grounds exist so an admission can say WHY and a refusal can name the two facts that disagreed; the refusal-render consumer is instance 2's work)
active_bash_subcommands_for_session  # noqa: F821 (direction=remove — backlog #530: went dead when the sticky bash union was removed from prompt_mutator; dies with sticky_grants_store)
validate_worker_token  # noqa: F821 (direction=add — backlog #532: sandbox worker-token capability check for the remote-agent relay (#180), which does not exist yet. Moved here from vulture_allowlist.py: it had zero references INCLUDING tests, so "false positive" was the wrong classification for a safety-floor validator.)
active_keys  # noqa: F821 (direction=add — backlog #489: EvalGate key-registry count. Production ENFORCES the bound (hook_broker.py:390/:394 prune self._keys against _KEY_REGISTRY_MAX) but nothing READS the count; the named coming consumer is the broker health surface added alongside the timings reader — health["hook_broker"] should report live session-key count next to queue/eval percentiles, which is exactly what an operator debugging concurrency needs. Test-only consumption today, and per test_operator_invalidation.py "consumed only by tests" is NOT grounds for the allowlist — hence future debt, not fp.)
# request_runtime_refresh: ENTRY RETIRED 2026-08-28 (#575 producer half). The
# tracked consumer LANDED — aidocs_service.py:2361 calls
# `(request or request_runtime_refresh)(restart_daemon=True)` from production,
# :2311 documents it ("ASKS, through the same request_runtime_refresh the deploy
# uses"), and release_pull.py:9 records the ask path as WORKS. #868 wired it.
# MEASURED before removing the suppression, which is the only honest way to
# retire one: `vulture --min-confidence 60 server/aidocs_mcp vulture_allowlist.py`
# (i.e. WITHOUT this file) no longer reports the symbol at all, while the five
# other entries audited alongside it still do. Vulture itself now agrees there is
# a consumer, so this line suppressed nothing — dead weight in a file whose whole
# purpose is to be true. Same retirement as invalidate_operator above, same rule:
# "the entry dies in the SAME commit that closes its backlog item."
# SNAPSHOT REMOVED 2026-08-28. The old text ended "20 of 117 items are rated so
# far" — the backlog now reports 309 active, so the figure had rotted and would
# rot again whatever it were set to. A debt entry that cites a LIVE-CHANGING
# STATISTIC is guaranteed to become false; the durable fact is that nothing
# sorts by kind, and the count belongs in #573 where it can be re-measured, not
# frozen in a suppression comment. Claim otherwise unchanged and re-verified:
# vulture still reports KIND_ORDER unused, so no consumer has landed.
KIND_ORDER  # noqa: F821 (direction=add — backlog #573: canonical ordering for the five `kind` values, for the severity x kind triage grid. The consumer is the ranked/grouped backlog surface that has not landed; nothing sorts by kind yet.)
freeze_is_security_class  # noqa: F821 (direction=add — backlog #574: the conductor-clears-its-subagent ladder is the named consumer — it must ask "is this freeze security-class?" and refuse to clear if so. Blocked on #571 GAP A: rung 2 still freezes, so strikes and freezes remain fused at the rung the ruling separates.)
# issues_strike / freezes_agent / agent_cancellable: ENTRIES RETIRED 2026-08-28
# (#574). All three tracked consumers LANDED, in one place —
# refusal_explainer.py:293-295, the ai_gate_explain output builder:
#     "issues_strike":     vc.issues_strike(resolved),
#     "freezes_agent":     vc.freezes_agent(resolved),
#     "agent_cancellable": vc.agent_cancellable(resolved),
# That is production, not a test: it is what an operator reads when asking the
# gate to explain a refusal. The predicate pair the entries described ("so a
# caller never re-derives the outcome from a class string") now has exactly that
# caller.
#
# MEASURED, never assumed — running vulture WITHOUT this file no longer reports
# any of the three, while `freeze_is_security_class` ABOVE still IS reported.
# The #574 family is therefore PARTIALLY wired, and the surviving entry is the
# unwired half rather than an oversight.
#
# WATCH THE NAME COLLISION when auditing this family: `is_security_class` is
# consumed at refusal_explainer.py:292; `freeze_is_security_class` is a
# DIFFERENT function and nothing consumes it. Confusing the two would retire a
# live suppression and hard-fail Gate 1d.
