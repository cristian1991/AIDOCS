"""Gate-grant confirmation — catch mistaken "allow X" typos.

When the setting `security.explicit_confirm_on_grant` is true and the
user's prompt triggered a user-intent grant detection (raw shell,
raw edits, bash subcommand, protected-edit, lane-exit, etc.), we
hold the grant in a pending state and fire an AskUserQuestion
popup for explicit confirmation. If the user picks "no" or
cancels, the grant never actually activates — the typo is caught
before the agent runs the dangerous tool.

Off by default because:
- Most grants are intentional (user typed the phrase deliberately).
- Confirmation popups break flow for experienced operators.
- The deny-table + dangerous-chain + judge layers already catch
  genuinely dangerous stuff; this setting is belt-and-suspenders
  for operators who want the extra friction.

Off by default, on for corporate profiles per operator choice.

No integration yet — this module provides the settings reader and
the PendingGrant struct. Wiring into claude_hook._grant_user_intent_tools
happens in a follow-up once we decide HOW to surface the question
(hook return shape + AskUserQuestion vs additionalContext prompt
vs dashboard modal).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONFIRM_SETTING_KEY = "security.explicit_confirm_on_grant"


@dataclass(frozen=True)
class PendingGrantConfirmation:
    """A user-intent grant detected in this turn but not yet applied.

    gate_label: human-readable gate identifier for the popup
        ("raw shell", "protected-file edit", etc.).
    gate_permissions: the RBAC permission names (or equivalent internal
        markers) that would be granted.
    phrase: the exact grant phrase the user typed, shown in the
        popup so the user can recognize their own intent.
    sticky: whether the grant would be session-wide (sticky) or
        per-turn.
    """

    gate_label: str
    gate_permissions: tuple[str, ...]
    phrase: str
    sticky: bool


def explicit_confirm_required(project_root: Path) -> bool:
    """Read the setting. Fail-closed on any error — silence here
    means 'no extra friction', which is the documented default.
    """
    try:
        from .config import get_setting

        return bool(
            get_setting(
                CONFIRM_SETTING_KEY,
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False
