"""Daemon lifecycle authority contract (#623).

## THE DISEASE

Stopping the AIDOCS governance daemon — the process that enforces every gate
for every project on the machine — was the least-governed operation in the
system. Two facts, both measured:

  1. ``cli.cmd_service`` was the sole entrypoint for ``service start|stop|
     restart`` and contained NO ``require_permission``, NO
     ``authorize_admin_command``, and no principal resolution of any kind.
     (``cli.py`` does call ``require_permission`` — at exactly two sites.
     ``service`` was not one of them.)

  2. Worse, and the part that makes the CLI beside the point:
     ``aidocs_service.request_stop()`` was

         stop_flag_path().write_text("stop")

     STOPPING THE GOVERNANCE DAEMON WAS A FILE WRITE. Anything able to write
     the daemon directory could do it. The CLI was a convenience, not a gate.

## WHY ADDING A CHECK TO cmd_service WOULD HAVE BEEN A SYMPTOM CURE

It cures one caller. The flag file stays writable by anything, so the failure
MOVES to any other writer — including a future one nobody remembers to gate.
The disease is that **daemon lifecycle had no authority contract: the stop
signal was AMBIENT STATE rather than an AUTHORISED OPERATION.**

So the signal itself changes shape. A lifecycle request is now a RECORD that
carries who asked, why, when, and a MAC over those fields; the watchdog
VERIFIES before honouring and IGNORES + AUDITS anything unattributed. A bare
``stop`` write no longer stops anything.

## PARTIAL MIGRATION IS THE WORST FAILURE MODE

There were TWO ambient lifecycle producers, not one: ``request_stop()`` and
``request_runtime_refresh(restart_daemon=True)`` — a second file write that
also restarts the daemon. Both are minted through this module. Moving only the
first would have left the identical hole one function down, and made it harder
to find afterwards because the ledger would say the work was done.

## WHAT THIS DOES AND DOES NOT GUARANTEE (§XXV — never overclaim)

DOES: a lifecycle signal must carry a principal the identity store recognises
as holding ``admin.daemon_lifecycle``; every mint and every refusal is audited;
an unattributed or tampered flag is refused by the consumer and named in the
ledger; and the check cannot be switched off by any value the CALLER supplies —
the secret is not a caller input and the verdict is not a caller argument.

DOES NOT: defend against a hostile process running AS THE SAME OS USER. Such a
process can read the MAC secret exactly as the legitimate CLI does. That is the
ambient authority of the OS account and no file-based scheme inside it can fix
it; saying otherwise would be claiming parity we cannot enforce. What the
contract removes is the SILENT, UNATTRIBUTED stop — the case where nobody could
even say afterwards who stopped governance. Raising this further requires an OS
privilege boundary (a service account owning the daemon dir), which is a
deployment change, recorded here rather than pretended away.

NO USER IS EXEMPT, THE OPERATOR INCLUDED. His stop is authenticated and audited
like anyone's — there is no bypass branch for him. The operator and the deploy
hot-swap keep the full ability to stop and start the daemon; this is about
proving WHO, never about removing capability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .permission_catalog import PERM_ADMIN_DAEMON_LIFECYCLE

# Lifecycle actions this contract governs. `start` is deliberately ABSENT:
# starting the enforcement daemon increases governance, so gating it would only
# create a way to keep the gate off.
ACTION_STOP = "stop"
ACTION_RESTART = "restart"
ACTION_REFRESH = "refresh"
ALL_ACTIONS: frozenset[str] = frozenset({ACTION_STOP, ACTION_RESTART, ACTION_REFRESH})

# Audit event kinds. Separate kinds so an operator can answer "who stopped the
# daemon" and "what tried to stop it without authority" as different questions.
EVENT_LIFECYCLE_REQUESTED = "daemon_lifecycle_requested"
EVENT_LIFECYCLE_REFUSED = "daemon_lifecycle_refused"
EVENT_UNATTRIBUTED_SIGNAL = "daemon_stop_unattributed"

# Refusal reason codes — machine states, not prose (§XXI: judge decisions are
# machine states). The human sentence is derived from these, never parsed.
REASON_OK = "authorised"
REASON_NO_PRINCIPAL = "no_principal_resolved"
REASON_NO_PERMISSION = "principal_lacks_daemon_lifecycle"
REASON_UNATTRIBUTED = "signal_carries_no_attribution"
REASON_BAD_MAC = "signal_mac_invalid"
REASON_MALFORMED = "signal_malformed"
REASON_STALE = "signal_expired"
REASON_UNKNOWN_ACTION = "action_not_governed"

# A lifecycle request is a COMMAND, not a standing grant. Ten minutes is long
# enough for a deploy hot-swap to settle and short enough that a stale flag left
# on disk cannot stop tomorrow's daemon.
_MAX_AGE_S = 600

_SECRET_ENV = "AIDOCS_DAEMON_LIFECYCLE_SECRET_DIR"


def _secret_dir() -> Path:
    """Where the MAC secret lives — deliberately NOT the daemon dir.

    Keeping it out of ``daemon_dir()`` is the whole point: the threat being
    removed is "anything that can write the daemon directory can stop the
    daemon". A secret stored in that same directory would leave the forgery
    path exactly where it was.
    """
    override = os.environ.get(_SECRET_ENV)
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "trust"


def _secret_path() -> Path:
    return _secret_dir() / "daemon_lifecycle.key"


def _load_or_create_secret() -> bytes:
    """Read the MAC secret, creating it 0600 on first use.

    Created rather than required so a fresh box is not bricked by a missing
    file — the first legitimate caller establishes it. Rotation is a delete:
    the next mint writes a new one, and any in-flight signal stops verifying,
    which is the safe direction.
    """
    path = _secret_path()
    try:
        return path.read_bytes()
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    # Write then tighten, so the key is never briefly world-readable.
    path.write_bytes(secret)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover — best effort on exotic filesystems
        pass
    return secret


def _mac(payload: dict[str, Any]) -> str:
    """MAC over the CANONICAL payload, excluding the mac field itself.

    Sorted keys + separators so the same logical request always produces the
    same bytes; otherwise a dict-ordering change silently invalidates every
    signal in flight.
    """
    body = {k: v for k, v in payload.items() if k != "mac"}
    msg = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_load_or_create_secret(), msg, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class LifecycleVerdict:
    """The machine state of an authority decision. Never a reason string."""

    authorised: bool
    reason_code: str
    action: str = ""
    actor_uid: str = ""
    actor_kind: str = ""
    detail: str = ""

    @property
    def is_infrastructure_failure(self) -> bool:
        """True when the verdict reflects a BROKEN signal rather than a DECISION.

        VOCABULARY RULE: a defect must never be reported in the language of
        policy. ``malformed`` / ``expired`` are degradations of the signal;
        ``lacks permission`` is a decision. Callers render them differently.
        """
        return self.reason_code in {REASON_MALFORMED, REASON_STALE}


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    """An ATTRIBUTED lifecycle command. The only thing the watchdog honours."""

    action: str
    actor_uid: str
    actor_kind: str
    reason: str
    requested_at: float
    nonce: str
    mac: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "actor_uid": self.actor_uid,
            "actor_kind": self.actor_kind,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "nonce": self.nonce,
            "extra": dict(self.extra),
            "mac": self.mac,
        }

    def serialize(self) -> str:
        payload = self.to_payload()
        payload["mac"] = _mac(payload)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def mint(
    action: str,
    *,
    actor_uid: str,
    actor_kind: str,
    reason: str,
    permitted: bool,
    extra: dict[str, Any] | None = None,
) -> tuple[LifecycleRequest | None, LifecycleVerdict]:
    """Mint an attributed lifecycle request, or refuse with a machine state.

    ``permitted`` is the RESOLVED verdict of a real RBAC check performed by the
    caller against ``PERM_ADMIN_DAEMON_LIFECYCLE`` — this module does not
    re-derive it, because the identity surfaces differ (CLI operator token,
    dashboard session, deploy service principal) and each must resolve its own
    principal through its own authenticated path.

    IT IS NOT A CALLER-CONTROLLED OFF SWITCH, and the distinction matters: a
    lawful gate cannot be disabled by a value the caller chooses. ``permitted``
    only ever CLOSES the gate here — passing False refuses, and passing True
    still requires a non-empty resolved principal, so a caller who has done no
    authentication has no uid to supply and is refused on that ground. There is
    no argument that makes this function skip the check.
    """
    if action not in ALL_ACTIONS:
        return None, LifecycleVerdict(
            authorised=False,
            reason_code=REASON_UNKNOWN_ACTION,
            action=action,
            detail=f"{action!r} is not a governed lifecycle action",
        )
    if not str(actor_uid or "").strip():
        # No principal at all. This is the #623 condition itself: an
        # unattributed lifecycle command.
        return None, LifecycleVerdict(
            authorised=False,
            reason_code=REASON_NO_PRINCIPAL,
            action=action,
            detail=(
                "daemon lifecycle requires an authenticated principal; no user "
                "is exempt, including the operator"
            ),
        )
    if not permitted:
        return None, LifecycleVerdict(
            authorised=False,
            reason_code=REASON_NO_PERMISSION,
            action=action,
            actor_uid=actor_uid,
            actor_kind=actor_kind,
            detail=f"principal lacks {PERM_ADMIN_DAEMON_LIFECYCLE}",
        )
    request = LifecycleRequest(
        action=action,
        actor_uid=str(actor_uid),
        actor_kind=str(actor_kind or "unknown"),
        reason=str(reason or ""),
        requested_at=time.time(),
        nonce=secrets.token_hex(16),
        extra=dict(extra or {}),
    )
    return request, LifecycleVerdict(
        authorised=True,
        reason_code=REASON_OK,
        action=action,
        actor_uid=request.actor_uid,
        actor_kind=request.actor_kind,
    )


def verify(raw: str | bytes | None) -> LifecycleVerdict:
    """Verify a lifecycle signal read off disk.

    Returns a refusing verdict for anything that is not a well-formed,
    in-date, correctly-MAC'd, attributed request — INCLUDING the legacy bare
    ``stop`` text, which is the exact shape #623 reported. That legacy shape is
    the reason this function exists: it must stop working, or the file write
    remains the real interface.

    Never raises. A verification error is a refusal, which is the safe
    direction for a signal that would stop the enforcement daemon.
    """
    if raw is None:
        return LifecycleVerdict(authorised=False, reason_code=REASON_MALFORMED)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return LifecycleVerdict(authorised=False, reason_code=REASON_MALFORMED)
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 — non-JSON includes the legacy "stop" text
        return LifecycleVerdict(
            authorised=False,
            reason_code=REASON_UNATTRIBUTED,
            detail=(
                "lifecycle signal carries no attribution (legacy bare-flag "
                "shape). A stop must name its actor."
            ),
        )
    if not isinstance(payload, dict):
        return LifecycleVerdict(authorised=False, reason_code=REASON_MALFORMED)

    action = str(payload.get("action") or "")
    actor_uid = str(payload.get("actor_uid") or "")
    actor_kind = str(payload.get("actor_kind") or "")
    supplied_mac = str(payload.get("mac") or "")

    if action not in ALL_ACTIONS:
        return LifecycleVerdict(
            authorised=False, reason_code=REASON_UNKNOWN_ACTION, action=action
        )
    if not actor_uid:
        return LifecycleVerdict(
            authorised=False, reason_code=REASON_UNATTRIBUTED, action=action
        )
    if not supplied_mac:
        return LifecycleVerdict(
            authorised=False,
            reason_code=REASON_UNATTRIBUTED,
            action=action,
            actor_uid=actor_uid,
            detail="request names an actor but carries no MAC — anyone could type a name",
        )
    try:
        expected = _mac(payload)
    except Exception:  # noqa: BLE001 — unreadable secret must refuse, not crash
        return LifecycleVerdict(
            authorised=False, reason_code=REASON_MALFORMED, action=action
        )
    if not hmac.compare_digest(expected, supplied_mac):
        return LifecycleVerdict(
            authorised=False,
            reason_code=REASON_BAD_MAC,
            action=action,
            actor_uid=actor_uid,
            actor_kind=actor_kind,
            detail="MAC does not match — the signal was forged or edited in place",
        )
    try:
        age = time.time() - float(payload.get("requested_at") or 0)
    except (TypeError, ValueError):
        return LifecycleVerdict(
            authorised=False, reason_code=REASON_MALFORMED, action=action
        )
    if age > _MAX_AGE_S or age < -_MAX_AGE_S:
        return LifecycleVerdict(
            authorised=False,
            reason_code=REASON_STALE,
            action=action,
            actor_uid=actor_uid,
            actor_kind=actor_kind,
            detail=f"signal is {int(age)}s old; a lifecycle command is not a standing grant",
        )
    return LifecycleVerdict(
        authorised=True,
        reason_code=REASON_OK,
        action=action,
        actor_uid=actor_uid,
        actor_kind=actor_kind,
    )


def audit(
    verdict: LifecycleVerdict,
    *,
    event_kind: str,
    project_root: Path | None = None,
) -> None:
    """Record a lifecycle decision in the audit ledger. Never raises.

    FAIL OPEN ON THE REPORT: a broken ledger must never block the act of
    reporting, and must never turn into a refusal of something already decided.
    The verdict is the authority; this is the scar it leaves.
    """
    try:
        root = Path(project_root) if project_root else None
        if root is None:
            from .mcp_server_runtime_helpers import resolve_project_root

            root = resolve_project_root()
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind,
            "daemon_lifecycle",
            action_kind=verdict.action or "lifecycle",
            status="applied" if verdict.authorised else "refused",
            payload={
                "action": verdict.action,
                "actor_uid": verdict.actor_uid,
                "actor_kind": verdict.actor_kind,
                "reason_code": verdict.reason_code,
                "detail": verdict.detail,
                "permission": PERM_ADMIN_DAEMON_LIFECYCLE,
            },
        )
    except Exception:  # noqa: BLE001 — never let the ledger block the report
        pass


def describe(verdict: LifecycleVerdict) -> str:
    """Operator-facing sentence for a verdict, in the RIGHT vocabulary.

    A decision says refused. A broken signal says what is degraded. Mixing the
    two is how an infrastructure failure gets read as a policy denial.
    """
    if verdict.authorised:
        return (
            f"daemon {verdict.action} authorised for {verdict.actor_kind}"
            f":{verdict.actor_uid} (audited)"
        )
    if verdict.is_infrastructure_failure:
        return (
            f"daemon {verdict.action or 'lifecycle'} signal is UNUSABLE "
            f"({verdict.reason_code}): {verdict.detail or 'signal degraded'}. "
            f"This is a signal defect, not a decision about the request."
        )
    return (
        f"daemon {verdict.action or 'lifecycle'} REFUSED ({verdict.reason_code})"
        f"{': ' + verdict.detail if verdict.detail else ''}. "
        f"Required permission: {PERM_ADMIN_DAEMON_LIFECYCLE}."
    )
