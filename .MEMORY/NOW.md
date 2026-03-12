# NOW

## Goal
-

## Active
-

## State
-

## Upcoming
- Continue command-surface simplification pass (cleanup/refactor ladder consolidation).


## Blockers
-

## Done
- Added `/memstart` for Claude and OpenCode as the explicit startup/restart warm-up command.
- Shifted the session-start order to `.aidocs/index.aidocs` -> `/.MEMORY/NOW.md` -> `/.MEMORY/INDEX.md` and aligned contracts, project routers, global bootstrap, drift checks, and Claude shims.
- Re-copied updated `.aidocs/` into all audited D-drive projects, fixed router entry order, and verified the audited projects pass the drift checker.
- Began `/memstart` command implementation planning and captured the startup-read intent in runtime memory.
- Aligned AIDOCS contracts so `build/` is the runtime/public root and `/project-update` explicitly recopies the core setup `.aidocs` files from there.
- Synced `build/` and refreshed live global bootstrap/command packs from `build/`.
- Captured new durable rule: `build/` is the runtime/public AIDOCS root, and `/project-update` must explicitly recopy core setup `.aidocs` files from there.
- Reviewed the current command surface and identified high-value next commands/features.
- Ran a lightweight sanity check on core routing/memory artifacts and a token-compaction review of key contract files.
- Began command-surface review and lightweight sanity/token-compaction check.
- Upgraded the public memory-drift checker interactive mode to use arrow-key drive/folder navigation with on-screen instructions.
- Synced the updated selector to `build/` and verified path-argument scan mode still works on `D:\Projects` and `D:\Games`.
- Began arrow-key navigation upgrade for the public memory-drift checker.
- Fully normalized the remaining partial legacy repos' `.aidocs/` trees.
- Added reusable public drift-check scripts in `scripts/` and `build/scripts/` with argument or interactive scan-root selection.
- Verified the drift checker on known-good roots and taught it to skip common generated folders.
- Began final normalization pass for partial legacy repos plus reusable drift-check tooling.
- Added `/agents/README.md` template and propagated `agents/README.md` to every audited D-drive project with `/.MEMORY/`.
- Hand-normalized routers and `/.MEMORY/INDEX.md` files across all audited D-drive projects; verification reports zero unlinked memory files.
- Added minimal `/.aidocs/index.aidocs` and `/.MEMORY/NOW.md` to legacy snapshot repos that lacked them.
- Verified and aligned the only existing live Claude project shims (`AIDOCS` and `DentalClinic-WebApp`) to redirect-only format with `/agents/` guidance.
- Began cross-project memory normalization after user requested `/agents/README.md` first, then live project repairs.
- Added project-root `/agents/` workspace and template scaffold for spawned-agent artifacts.
- Enforced spawned-agent filename rules: `/agents/YYYY-MM-DD-<topic>-plan.md` and `/agents/YYYY-MM-DD-<topic>-investigation.md`.
- Updated contracts, templates, docs, installer bootstrap, global live files, and the live AIDOCS Claude redirect shim to use the new `/agents/` rule.
- Captured new durable rule: spawned-agent plans/investigations belong in project-root `/agents/` with logical dated names.
- Refreshed live global bootstrap and global command packs from `build/`.
- Fixed installer Unicode handling so live global bootstrap files now render `🛑 STOP` correctly.
- Created live Claude project redirect shim for AIDOCS under `C:\Users\User\.claude\projects\d--Projects-Active-AIDOCS\memory\MEMORY.md`.
- Began live-environment refresh after user selected steps 1 and 3.
- Hardened memory routing: `/.MEMORY/INDEX.md` is now the declared main entry point; Claude external memory is redirect-only by contract.
- Patched routers, shared docs, templates, init/update command docs, installer source, and synced `build/`.
- Completed repo-wide audit of memory pathing, redirects, plan rules, and Claude bootstrap contradictions.
- Captured current memory-system directives in project memory before continuing the audit.
- Removed command-vendoring model from `project-init`/`project-update` and cleaned stale project command files.
- Added STOP newline format + related-project collaboration memory contract.
- Added editing-agent task-closure rule (daily log + NOW cleanup after edits complete).
- Reduced command token footprint and removed `/memory-update` from active command surface.
- Added STOP-in-script safety rule: halt multi-step execution immediately when STOP is triggered.
- Added per-project memory isolation rule for multi-repo/monorepo agents.
- Added archive lifecycle rule: NOW snapshots are moved to DONE and NOW is reset by `/archive`.
- Added memory write efficiency rule: token-minimal memory entries that preserve exact user intent.
