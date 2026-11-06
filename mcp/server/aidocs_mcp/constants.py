MANAGED_MARKER = (
    "<!-- AIDOCS-MANAGED-ABOVE: write project-specific instructions below this line -->"
)

SESSION_TEMPLATE_NAME = "SESSION.md"
CONTEXT_TEMPLATE_NAME = "context.md"
PLAN_TEMPLATE_NAME = "PLAN.md"
HANDOFF_FILE_SUFFIX = ".handoff.md"

SESSION_SECTION_ORDER = [
    "Title",
    "Status",
    "Owner",
    "Goal",
    "Scope",
    "Key Memory Links",
    "Local Session Links",
    "Active Claims",
    "State",
    "Upcoming",
    "Blockers",
    "Last Updated",
]

VALID_SESSION_STATUSES = {"active", "paused", "blocked", "done"}

CONTEXT_SECTION_ORDER = [
    "Relevant Files",
    "Relevant Commands",
    "Relevant Snippets",
    "Session Facts",
    "Constraints",
]

PLAN_SECTION_ORDER = [
    "Purpose",
    "Scope",
    "Current State",
    "Partial Goals",
    "Steps",
    "End Goal",
    "Constraints",
    "Validation",
    "Next Steps",
]

HANDOFF_SECTION_ORDER = [
    "Purpose",
    "Current State",
    "What Was Done",
    "What Failed / Dead Ends",
    "What Matters Now",
    "Open Questions",
    "Risks and Blockers",
    "Relevant Files",
    "Estimated Effort",
    "Suggested Next Steps",
    "Steps",
    "Related Sessions",
    "Related Project Links",
    "Freshness",
]
