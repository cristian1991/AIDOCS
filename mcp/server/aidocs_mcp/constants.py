MANAGED_MARKER = "<!-- AIDOCS-MANAGED-ABOVE: write project-specific instructions below this line -->"

SESSION_TEMPLATE_NAME = "SESSION.md"
CONTEXT_TEMPLATE_NAME = "context.md"

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
