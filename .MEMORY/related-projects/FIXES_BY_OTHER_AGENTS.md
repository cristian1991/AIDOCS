# FIXES_BY_OTHER_AGENTS

Purpose: track fixes applied here that were caused by related project behavior/contracts, so upstream project agents can apply proper fixes there.

Entry format
- Date: YYYY-MM-DD
- Related Project: <name/path>
- Issue: <root cause from related project>
- Fix Applied Here: <what was changed in this project>
- Upstream Follow-up Needed: <what should be fixed in the related project>

## Entries
- Date: 2026-03-05
  - Related Project: CodeNexusAPI
  - Issue: Downstream deployments required repeated local patches because generator/runtime contracts were not fully propagated upstream.
  - Fix Applied Here: Added related-project collaboration memory contract and canonical handoff log path in AIDOCS.
  - Upstream Follow-up Needed: Ensure connected projects append issue+fix handoff entries whenever cross-project root cause is identified.
