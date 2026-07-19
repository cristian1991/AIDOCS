"""WriteAuthorizer — peer of the controller, not a sub-controller.

Decides whether a SPECIFIC path/content write is allowed. Called by:
  - the controller's path_extraction / trust_zone / policy gates for
    write-class actions surfaced through `enforce()`
  - lower-level internal paths (rollback, session_store TOML writes,
    config writes) DIRECTLY, without the full controller pipeline

Audit emission is owned by whichever caller invoked the authorizer —
the controller path emits via Decision.audit_events; direct callers
emit via their own ExecutionIndexStore call. NO double audit.

Skeleton today; logic lands as path_trust_zone + protected-file
classifications migrate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class WriteAuthorization:
    """Outcome of authorize_write."""

    allowed: bool
    reason_code: str
    user_message: str = ""
    blocked_by: str = ""  # e.g. "protected_file" / "trust_zone_external"


class WriteAuthorizer:
    """Authorize a single path write. Does NOT mutate state."""

    def authorize_write(
        self,
        project_root: Path,
        target_path: Path,
    ) -> WriteAuthorization:
        """SKELETON: permissive ALLOW with an explicit reason code
        so callers know the authorizer is not yet wired.

        Real implementation: trust-zone classify, protected-file
        registry lookup, content-hint heuristics for sensitive
        materials.
        """
        return WriteAuthorization(
            allowed=True,
            reason_code="authorizer_not_wired",
            user_message=(
                "WriteAuthorizer skeleton — not yet wired. Legacy "
                "path_trust_zone / protected_file checks remain "
                "authoritative."
            ),
        )
