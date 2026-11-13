"""The gate's preflight switch (#1256): one setting, one reader, one write path.

Operator's rulings (DESIGN-web-security-settings-preflight-switch.md V2, V3):
per gate project, off until turned back on, proof is the existing login plus the
server confirm step, super_admin only. A token holding super_admin can script
both steps; that limit is accepted because agent isolation is a separate fix.

``preflight_enabled`` is the only reader of the key that changes behaviour:
PromptMutator.preflight_judge and xaacp_ingress_verdict.annotate_entries call it,
and nothing else does, so nothing else is turned off. It reads THIS project's row
only, so a session row, a tenant-global row or the operator's local project never
reach a gate project, and the other way round. Only a literal false disables:
missing, malformed or unreadable means on.

``run`` is the only writer, reached from the gate tool. The transport door
authenticates and hands it a ``Caller`` or the reason there is none, on every
call, so authority is re-read at both phases. A set goes, in this order:

  1. no caller -> refused and audited;
  2. arguments: real booleans and a reason;
  3. the view must still hold (expected_enabled against the state now), and the
     change must be a change. Both are refused before any consent is asked for,
     and a consent already minted is not spent;
  4. the consent: ``canonical_invocation.confirm_exchange``, single-use and bound
     to the user, the credential, the org, the project, the key, the new value,
     the expected old value and the reason. A token for anything else is refused
     and not consumed;
  5. ``ConfigStore.compare_and_set``: the compare again inside the write
     transaction, with the audit row in that same transaction.

The consent is spent before the compare-and-set: the two live in different
databases, and a change landing in between needs a new consent anyway.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEY = "security.preflight_enabled"
TOOL = "dashboard_preflight_switch"
MODES: tuple[str, ...] = ("status", "set")
#: The intent the consent is bound to (folded into its bind hash).
INTENT = "confirm preflight switch"
MAX_REASON_CHARS = 500


@dataclass(frozen=True)
class Caller:
    """The super_admin the door authenticated. Never built from a caller's claim."""

    user_id: str
    token_id: str  # the credential the consent is bound to


def preflight_enabled(project_root: Path | str) -> bool:
    """False only when this project's own row is a literal JSON false."""
    from .config_store import ConfigStore

    try:
        value = ConfigStore().get(Path(project_root), KEY, scope="project", scope_key="")
    except Exception:  # noqa: BLE001 -- malformed or unreadable means on
        return True
    return value is not False


def _binding(
    *, org_id: str, project_id: str, new: bool, expected: bool, reason: str
) -> dict[str, Any]:
    """Everything the human agrees to."""
    return {
        "org_id": org_id,
        "project_id": project_id,
        "key": KEY,
        "new": new,
        "expected_old": expected,
        "reason": reason,
    }


def audit_refusal(
    root: Path | str,
    *,
    blocked_by: str,
    user_id: str,
    project_id: str,
    org_id: str = "",
    mode: str = "",
    old: bool | None = None,
    new: Any = None,
    reason: Any = "",
) -> None:
    """Best-effort, like the sibling control-plane refusals; never carries a token.

    ``attempt_id`` keeps two identical refusals as two rows: the ledger's
    content-addressed event id would otherwise collapse them into one.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            Path(root),
            event_kind="preflight_switch_refused",
            source_kind=TOOL,
            capability_name=TOOL,
            action_kind=str(mode or "")[:40],
            target_entity=KEY,
            status="refused",
            payload={
                "attempt_id": uuid.uuid4().hex,
                "blocked_by": blocked_by,
                "user_id": user_id,
                "project_id": project_id,
                "org_id": org_id,
                "old": old,
                "new": new if isinstance(new, bool) else None,
                "reason": str(reason or "")[:MAX_REASON_CHARS],
            },
            user_id=user_id or None,
        )
    except Exception:  # noqa: BLE001 -- a refusal stands whether or not its row lands
        pass


def _summary(*, enabled: bool, reason: str, project_name: str, project_id: str) -> str:
    where = f"project {project_name!r} ({project_id})" if project_name else f"project {project_id}"
    if enabled:
        return f"About to turn preflight back ON for {where} on the gate. Reason: {reason!r}."
    return (
        f"About to turn preflight OFF for {where} on the gate, until it is turned back "
        "on. Prompts and messages from other agents will not be checked by it. Other "
        f"guards keep running. Reason: {reason!r}. The user must confirm."
    )


def run(
    project_root: Path | str,
    mode: str,
    *,
    caller: Caller | None,
    refusal_user_id: str = "",
    project_id: str,
    project_name: str = "",
    org_id: str = "",
    confirm_store: Any = None,
    enabled: Any = None,
    expected_enabled: Any = None,
    reason: Any = "",
    confirm_token: Any = "",
    now: float | None = None,
) -> dict[str, Any]:
    """One switch call. ``caller`` None means not a super_admin of this project."""
    from . import canonical_invocation as _ci
    from .config_store import ConfigStore
    from .execution_index_store import ExecutionIndexStore

    root = Path(project_root)
    mode = str(mode or "").strip().lower()
    user_id = caller.user_id if caller is not None else str(refusal_user_id or "")
    current = preflight_enabled(root)

    def refuse(blocked_by: str, detail: str, **extra: Any) -> dict[str, Any]:
        audit_refusal(
            root,
            blocked_by=blocked_by,
            user_id=user_id,
            project_id=project_id,
            org_id=org_id,
            mode=mode,
            old=current,
            new=enabled,
            reason=reason,
        )
        return {"ok": False, "_error": blocked_by, "_detail": detail, **extra}

    if caller is None or not str(caller.user_id or "").strip():
        return refuse(
            "super_admin_required",
            "the preflight switch is a super_admin control, in the org that owns the project",
        )
    if mode not in MODES:
        return refuse("unknown_mode", " | ".join(MODES))
    if mode == "status":
        return {
            "ok": True,
            "project_id": project_id,
            "project_name": project_name,
            "key": KEY,
            "enabled": current,
        }

    if not isinstance(enabled, bool) or not isinstance(expected_enabled, bool):
        return refuse("bad_args", "enabled and expected_enabled must be true or false")
    clean_reason = reason.strip() if isinstance(reason, str) else ""
    if not clean_reason:
        return refuse("reason_required", "say why the switch is changing")
    if len(clean_reason) > MAX_REASON_CHARS:
        return refuse("reason_too_long", f"at most {MAX_REASON_CHARS} characters")
    if expected_enabled != current:
        return refuse(
            "state_changed",
            "preflight is not in the state you were looking at; refresh and try again",
            enabled=current,
        )
    if enabled == current:
        return refuse("nothing_to_change", "the switch is already there", enabled=current)

    consent_refusal, _ = _ci.confirm_exchange(
        store=confirm_store,
        tool=TOOL,
        confirm_args=_binding(
            org_id=org_id,
            project_id=project_id,
            new=enabled,
            expected=expected_enabled,
            reason=clean_reason,
        ),
        intent=INTENT,
        operator=caller.user_id,
        project_id=project_id,
        context_key=caller.token_id,
        presented=str(confirm_token or ""),
        now=time.time() if now is None else now,
        detail=(
            "the preflight switch needs the operator's confirmation of exactly this "
            "change; confirm it, then call again with the server-issued confirm_token"
        ),
        summary=_summary(
            enabled=enabled, reason=clean_reason, project_name=project_name, project_id=project_id
        ),
    )
    if consent_refusal is not None:
        if consent_refusal.get("_error") == _ci.CONFIRM_REQUIRED:
            consent_refusal.update(ok=False, project_id=project_id, project_name=project_name)
            return consent_refusal
        return refuse(str(consent_refusal["_error"]), str(consent_refusal.get("_detail") or ""))

    def write_audit(conn: Any) -> None:
        ExecutionIndexStore().record_event_on_connection(
            conn,
            root,
            event_kind="preflight_switch_changed",
            source_kind=TOOL,
            capability_name=TOOL,
            action_kind="set",
            target_entity=KEY,
            status="applied",
            payload={
                "attempt_id": uuid.uuid4().hex,
                "user_id": caller.user_id,
                "project_id": project_id,
                "org_id": org_id,
                "old": expected_enabled,
                "new": enabled,
                "reason": clean_reason,
            },
            user_id=caller.user_id,
        )

    try:
        written = ConfigStore().compare_and_set(
            root,
            KEY,
            enabled,
            matches=lambda stored: (stored is not False) == expected_enabled,
            audit=write_audit,
        )
    except Exception as exc:  # noqa: BLE001 -- a write fault is an audited refusal, never a 502
        return refuse("write_failed", type(exc).__name__)
    if not written:
        return refuse(
            "state_changed",
            "preflight changed while this was being confirmed; refresh and try again",
            enabled=preflight_enabled(root),
        )
    return {
        "ok": True,
        "project_id": project_id,
        "project_name": project_name,
        "key": KEY,
        "enabled": enabled,
        "previous": expected_enabled,
    }
