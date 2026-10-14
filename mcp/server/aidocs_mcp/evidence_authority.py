"""ONE SHAPE FOR "THE AUTHORITY COULD NOT REACH ITS EVIDENCE".

DRT-06 / DRT-07 / DRT-08 (scratch/new/doctrine-split/code-bugs.md) are three
instances of a single defect: an enforcement gate whose evidence store is
unreachable reports PERMISSION. `except Exception: return True` and
`palace_axis in DEFECT_STATES -> allowed=True` are the same sentence written
twice. Empire law: UNKNOWN IS NEVER LAUNDERED INTO A PASS.

What this module does NOT do is force one policy onto all three. The trust
models genuinely differ:

* A deliberately absent palace (`PALACE_NOT_WIRED`) is a SUPPORTED
  configuration. It stays permissive and is not this module's business.
* A CONFIGURED authority that cannot answer is a defect. Its authorization
  consequence must differ from "asked and got nothing".
* An evidence-store outage on the read-before-edit path is TRANSIENT here
  (measured: 79 `database is locked` errors in one day of concurrent lanes),
  so the honest refusal carries a RETRY route rather than demanding operator
  intervention.

Three rungs, and the names are the contract:

``POLICY_CLOSED``
    Refuse the governed action. The refusal must carry the strongest causal
    evidence available at the boundary (law: a refusal never turns a known
    cause into an unattributed one) and a reachable recovery route.
``POLICY_DEGRADED``
    Allow, but the verdict is STAMPED degraded and may never claim grounded
    enforcement. A caller must be able to tell "proven" from "could not
    check" without guessing.
``POLICY_OPEN``
    The pre-fix behaviour. Retained ONLY as a named, operator-chosen escape
    hatch so a box whose authority is broken is never bricked with no way
    out. It is never a default.

``format_unavailable_reason`` is the one place the operator-facing sentence is
built, so all three gates name the cause, the axis and the route in the same
words.
"""

from __future__ import annotations

from pathlib import Path

#: Authorization consequence of "the authority is configured but unavailable".
POLICY_CLOSED = "closed"
POLICY_DEGRADED = "degraded_allow"
POLICY_OPEN = "open"

POLICY_VALUES = (POLICY_CLOSED, POLICY_DEGRADED, POLICY_OPEN)

#: Machine-readable marker that opens every degraded/refused reason string
#: produced here. Downstream callers (and tests) detect the state by this
#: token rather than by prose, so a reworded message cannot silently turn a
#: "could not check" into something that reads like a clean pass.
UNAVAILABLE_MARKER = "EVIDENCE-AUTHORITY-UNAVAILABLE"


def resolve_unavailable_policy(
    key: str,
    project_root: Path | None,
    *,
    session_id: str | None = None,
    default: str = POLICY_CLOSED,
) -> str:
    """The configured policy for one unavailable-authority axis.

    An unreadable or garbage config value resolves to ``default`` -- which is
    CLOSED, not OPEN. A config layer we cannot read is itself an unknown, and
    an unknown must not widen authority.
    """
    try:
        from .config import get_setting

        raw = get_setting(
            key,
            project_root=project_root,
            session_id=session_id or None,
            default=default,
        )
    except Exception:  # noqa: BLE001 - an unreadable policy must not widen authority
        return default
    value = str(raw or "").strip().lower()
    return value if value in POLICY_VALUES else default


def format_unavailable_reason(
    *,
    axis: str,
    cause: BaseException | str,
    recovery: str,
    policy: str,
) -> str:
    """The single sentence every unavailable-authority boundary emits.

    Carries, in order: the marker, WHICH axis could not be reached, the
    STRONGEST causal evidence available here (the exception type and message,
    not a flattened "unavailable"), the policy that produced this outcome, and
    the recovery route. Dropping the cause would turn a known failure into an
    unattributed refusal, which the refusal law forbids outright.
    """
    if isinstance(cause, BaseException):
        detail = f"{type(cause).__name__}: {cause}"
    else:
        detail = str(cause or "cause not recorded")
    return (
        f"{UNAVAILABLE_MARKER} [{axis}] policy={policy} - "
        f"the authority that proves this could not be reached: {detail}. "
        f"This is NOT a proven pass and NOT a proven failure. {recovery}"
    )


def is_unavailable_reason(reason: str | None) -> bool:
    """True for a verdict produced by an unreachable authority.

    Why a bool is not enough: ``(True, 'file was read this session')`` and
    ``(True, 'events-unavailable')`` were indistinguishable to every caller of
    the grounding helpers, which is DRT-07 in one line.
    """
    return UNAVAILABLE_MARKER in str(reason or "")
