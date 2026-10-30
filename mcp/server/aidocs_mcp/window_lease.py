"""THE LEASE — ``window -> conversation``, and the ONLY answer to "who is
calling" on the MCP tool path (#880 phase 2).

WHAT CHANGED, AND WHY IT HAD TO. A tool call arrives over HTTP carrying a
conversation id in ``X-Aidocs-Host-Session``. That value is a CACHE: the stdio
shim read ``CLAUDE_CODE_SESSION_ID`` ONCE, at its own spawn, and has relayed the
same string ever since. ``/resume`` rotates the conversation without respawning
the shim, so the header goes stale silently — measured 2026-08-23, FOUR distinct
host ids in one call, with ``channels_agree`` reporting ``true`` throughout
because it compared the header against a value derived from the header (#876).

The window does NOT rotate. Across two ``/resume``, one ``/clear`` and one
``/mcp`` reconnect the key ``13336:134319313179516362`` was identical
throughout. And SessionStart — which fires on every one of those three events —
STATES the current conversation in a payload the host writes fresh each firing.

So the resolution inverts. Not "what did the transport cache", but:

    the request states its WINDOW  ->  the window's LEASE names the CONVERSATION

and the lease was written from the host's own fresh statement, by the hook,
before any tool call happened.

IDENTITY HAS NO FALLBACK (``system/invariants.md``; design rule 2026-08-23: a
fallback can stamp wrong data with no record of where it came from). When the
lease cannot answer, THIS MODULE REFUSES.
It does not substitute the header, the process-global conductor stamp, the #464
chain, ``last_host_session_id``, or an environment variable. NONE OF THOSE NAMES
APPEAR IN THIS FILE, and ``TestTheLeaseHasNoBackDoor`` asserts that as source
text rather than trusting the sentence.

THE REFUSAL NAMES WHICH AXIS IS MISSING, and that is the whole difference
between a diagnosable refusal and the lockout the operator reproduced. "No
window key on this request" and "no conversation bound to this window" are
DIFFERENT FAILURES WITH DIFFERENT REMEDIES:

  * no window key      -> the caller cannot be placed in a window at all. The
                          remedy is about the TRANSPORT and the HOST.
  * no conversation    -> the window is known; nothing has bound it. The remedy
                          requires NOTHING of the caller — SessionStart binds
                          autonomously on the next start / ``/resume`` /
                          ``/clear``.

Today's ``managed_mode_not_active`` says neither, and points at
``ai_session(mode='connect')`` — a GATED tool — which is the circular dependency
measured on 2026-08-23 (#880, item 4).

THE LOCKOUT IS SOLVED BY CONSTRUCTION, NOT BY A GUARD. Phase 1 made the hook
bind the window (``hook_pipeline.on_session_start`` ->
``window_binding_store.record_session_start_window``). NO GATED TOOL IS EVER
REQUIRED TO BECOME BOUND, so the "refused for being unbound, whose remedy is
itself refused" cycle has no way to form. ``TestTheBindNeedsNoToolCall`` pins
that property rather than describing it.

A HOST WITH NO MEASURED DERIVATION STILL HAS NO WINDOW KEY, and that is still
an operator decision rather than a hole this module patches.
``window_key.derive_window_key`` now derives on win32 AND on linux (a ``/proc``
ancestry walk), so the lockout that used to threaten every non-win32 host is
narrowed to macOS, a procfs-less container, and anything else with no measured
derivation. Under this module every such request resolves
``REASON_NO_WINDOW_KEY`` and refuses. There is deliberately NO "if no window
key, use X" branch here, because writing one is precisely how the defect this
programme exists to remove gets reinstated.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

# ── THE SHAPE OF A WINDOW KEY ────────────────────────────────────────────
#
# TWO POSITIVE DECIMAL INTEGERS, COLON SEPARATED, OR IT IS NOT A WINDOW KEY.
# ``<host pid>:<its age>``. Both halves, always: pids are recycled, and a bare
# pid would let a NEW process inherit a DEAD window's lease — and therefore its
# conversation and its authority.
#
# Checked on the READ side as well as the write side ON PURPOSE. #880 records
# what "no format validation" cost the chain: ``auth-truth-614``, a synthetic
# test id, seated permanently in an authority structure. A junk key that is
# already seated must never be able to satisfy a lookup, whatever let it in.
#
# IMPORTED, NOT RE-DECLARED. The pattern is owned by ``window_key``, the module
# that MINTS a key. A private copy here would drift from the minter's an edit
# at a time, and the only symptom would be a key the writer accepted that this
# resolver refuses — a row that exists and can never be used.
from .window_key import WINDOW_KEY_SHAPE

# ── THE REASONS. FOUR CAUSES, FOUR NAMES. ────────────────────────────────
#
# One generic "identity unavailable" would destroy exactly the provenance this
# work exists to restore. Each of these has a DIFFERENT remedy and a different
# party who can act on it; collapsing them re-creates ``managed_mode_not_active``
# under a new spelling.

#: The request could not be placed in a window at all.
REASON_NO_WINDOW_KEY = "no_window_key_on_this_request"
#: The window is identified; nothing has ever bound a conversation to it here.
REASON_NO_CONVERSATION_BOUND = "no_conversation_bound_to_this_window"
#: The lease is per-project and this request resolved no project to read.
REASON_NO_PROJECT_ROOT = "no_project_root_to_read_the_lease_from"
#: A window key arrived that is not a window key (a seated or forged junk id).
REASON_MALFORMED_WINDOW_KEY = "window_key_is_not_a_window_key"
#: The window DID bind, and another window's claim on the same conversation
#: released it (#880's one-conversation-one-window rule). Distinct from
#: NO_CONVERSATION_BOUND on purpose -- see that entry's REFUSAL_TEXT.
REASON_CLAIM_RELEASED = "conversation_claim_released_to_another_window"
#: #1192: the window runs SEVERAL sessions (OpenCode) and this call carried no
#: session claim, so WHICH of them is calling is unknown. Never "the last one
#: announced".
REASON_NO_SESSION_CLAIM = "no_session_claim_on_a_multi_session_window"
#: #1192: the call's session claim is not a member of THIS request's window --
#: another instance's session, an unannounced one, or a forged one.
REASON_SESSION_NOT_IN_WINDOW = "session_claim_not_a_member_of_this_window"
#: A re-entry of a request carried a claim different from the one the request
#: holds -- or carried one when the request holds none. Refused, named, with
#: no identity at all: never the outer claim, never the new one.
REASON_SESSION_CLAIM_CONFLICT = "session_claim_conflicts_with_this_request"

#: #1192: the reserved MCP ARGUMENT the OpenCode plugin stamps on every AIDOCS
#: tool call with the calling session, OVERWRITING any value the model put
#: there (``opencode_plugin.js`` declares the same name; a contract test pins
#: the two). The daemon POPS it before any tool sees its arguments. It is a
#: CLAIM, never an authority: it can only select among the sessions this
#: request's own window announced through SessionStart.
SESSION_CLAIM_ARGUMENT = "_aidocs_host_session"

#: What a resolved conversation's provenance is called, wherever it is reported.
LEASE_SOURCE = "window_conversation_state — bound by SessionStart from the host's own payload (#880)"

#: ``channels_agree`` when the comparison COULD NOT BE PERFORMED. Never ``True``.
UNVERIFIABLE = "unverifiable"

#: Every reason, with the remedy that actually applies to it. Prose lives here,
#: once, so the two failures cannot drift into one message by editing accident.
REFUSAL_TEXT: dict[str, str] = {
    REASON_NO_WINDOW_KEY: (
        "This request carries NO WINDOW KEY, so there is no window whose "
        "conversation lease could be read. You are not being refused for being "
        "unbound — AIDOCS cannot tell WHICH WINDOW you are. CAUSE: either the "
        "request did not arrive through the stdio shim (a direct HTTP client "
        "sends no X-Aidocs-Window), or this host has no measured window "
        "derivation at all — window_key.py is win32-only and answers "
        "'not_win32_no_measured_derivation_for_this_host' everywhere else. "
        "REMEDY: reach the daemon through the stdio shim from a host that has a "
        "measured derivation. NOTHING IS SUBSTITUTED HERE: the transport's "
        "cached conversation id is the stale value #876 measured, and the "
        "process-global stamp is a different actor on a shared daemon (#672)."
    ),
    REASON_NO_CONVERSATION_BOUND: (
        "This request's WINDOW is identified, but NO CONVERSATION IS BOUND TO "
        "IT in this project. That is a different failure from the one above and "
        "it has a different remedy: NOTHING IS REQUIRED OF YOU. SessionStart "
        "binds the window by itself — it fires on window start, on /resume and "
        "on /clear, all three MEASURED to rotate the conversation id — and it "
        "does so from the hook, before any tool call, so no gated tool is ever "
        "needed to become bound (#880 item 4). If a window never binds, the "
        "SessionStart hook is not firing or is not reaching this project; THAT "
        "is the fault to chase, not your identity."
    ),
    REASON_NO_PROJECT_ROOT: (
        "The window -> conversation lease is stored PER PROJECT and this "
        "request resolved no project root, so there is no store to read it "
        "from. This says nothing about who you are; it says AIDOCS does not "
        "know where to look."
    ),
    REASON_MALFORMED_WINDOW_KEY: (
        "This request carried a window key that is not a window key. A window "
        "key is '<host pid>:<host creation filetime>' — two positive decimal "
        "integers. Refused rather than looked up: #880 records how "
        "'auth-truth-614', a synthetic test id, came to sit permanently in an "
        "authority structure that never validated its format."
    ),
    REASON_CLAIM_RELEASED: (
        "This window DID bind a conversation and its claim was RELEASED when "
        "another window claimed the same conversation (#880's "
        "one-conversation-one-window rule, window_binding_store's release "
        "UPDATE). The row is still here and names what it lost in "
        "previous_host_session_id. THIS IS NOT 'SessionStart never fired' — it "
        "fired and was overruled — so chasing the hook is the wrong "
        "investigation. MEASURED 2026-08-25: a LIVE window sat leaseless this "
        "way while the window that took its conversation had already died and "
        "been reaped, leaving the conversation held by nobody. REMEDY: a new "
        "SessionStart (window start / resume / clear) re-claims it; the "
        "release itself no longer displaces a provably-live holder."
    ),
    REASON_NO_SESSION_CLAIM: (
        "This request's window runs SEVERAL host sessions (an OpenCode "
        "instance) and the call did not say which one is calling. Refused "
        "rather than answered with the session announced last, which would be "
        "another session's identity. CAUSE: the OpenCode plugin did not stamp "
        "this MCP call -- an old plugin, or a call that bypassed it. REMEDY: "
        "install the current plugin (`aidocs setup`) and restart OpenCode."
    ),
    REASON_SESSION_CLAIM_CONFLICT: (
        "A re-entry of this request carried a session claim that is not the "
        "request's own -- a different session, or a claim where the outermost "
        "call made none. A claim is established once, by the outermost call; "
        "refused rather than letting a nested call choose an identity."
    ),
    REASON_SESSION_NOT_IN_WINDOW: (
        "The call names a host session that THIS window never announced. A "
        "session belongs to the window whose SessionStart recorded it; a "
        "session from another OpenCode instance, an unannounced one, or a "
        "forged claim resolves to nobody. REMEDY: none from the caller; if the "
        "session is genuinely this window's, its first prompt re-announces it."
    ),
}


def _refusal_text(reason: str) -> str:
    """The remedy prose for ``reason``. Unknown reasons get NO invented remedy.

    An unrecognised reason is reported as itself. Inventing a plausible remedy
    for a cause this module does not know is the "cannot tell from where"
    defect, one layer up.
    """
    return REFUSAL_TEXT.get(reason) or (
        f"Host identity could not be resolved from the window lease "
        f"({reason or 'reason not stated'}). No remedy is offered because this "
        f"module does not recognise that cause."
    )


def pop_session_claim(arguments: Any) -> tuple[Any, str]:
    """``(arguments_without_the_claim, claim)``. Never mutates the caller's dict.

    A claim that is not a non-empty string is NO claim. Popped whatever its
    value, so the reserved name never reaches a tool's schema validation.
    """
    if not isinstance(arguments, dict) or SESSION_CLAIM_ARGUMENT not in arguments:
        return arguments, ""
    rest = dict(arguments)
    raw = rest.pop(SESSION_CLAIM_ARGUMENT, None)
    claim = raw.strip() if isinstance(raw, str) else ""
    return rest, claim


#: #1192 LIVE (build 325): the claim, request-scoped. FastMCP re-enters
#: ``server.call_tool`` for one tools/call -- the outer call runs the middleware
#: chain and its terminal calls ``call_tool(..., run_middleware=False)`` again,
#: with the ARGUMENTS THE OUTER CALL ALREADY STRIPPED. Popped only from the
#: arguments, the claim was therefore present on the outer resolution and
#: ABSENT on the inner one, which is the one the tool runs under; an OpenCode
#: window then refused ``no_session_claim``. The outermost pop records it here
#: so every re-entry of the same request resolves with the same claim.
_request_session_claim: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_session_claim",
    default=None,
)

#: How deep inside ONE request's call_tool chain this context is. 0 = the
#: OUTERMOST external call -- the only place a claim may be ESTABLISHED (r0b1
#: on 405789802: re-binding on every invocation let a nested call carrying B
#: overwrite a request that claimed A, or introduce a claim of its own).
_request_call_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_request_call_depth",
    default=0,
)

class SessionClaimResetError(RuntimeError):
    """The request's claim scope could not be cleared. FAIL CLOSED.

    A swallowed reset could leave a claim bound for whatever runs next in this
    context, which is a stale identity. The call that owned the scope errors
    instead, under this name.
    """


def set_request_session_claim(claim: str):
    """Bind THIS request's session claim. Reset in ``finally``."""
    return _request_session_claim.set(str(claim or "").strip() or None)


def reset_request_session_claim(token) -> None:
    """Release the binding, or raise ``SessionClaimResetError``. Never silent."""
    try:
        _request_session_claim.reset(token)
    except Exception as exc:  # noqa: BLE001 -- re-raised, named
        raise SessionClaimResetError(
            f"session claim scope could not be cleared: {type(exc).__name__}: {exc}"
        ) from exc


def current_request_session_claim() -> str:
    """The claim the outermost call of THIS request established, or ``""``."""
    return _request_session_claim.get() or ""


def current_request_call_depth() -> int:
    return int(_request_call_depth.get() or 0)


def enter_request_call():
    """Mark this context as INSIDE a request's call chain. Reset in ``finally``."""
    return _request_call_depth.set(current_request_call_depth() + 1)


def exit_request_call(token) -> None:
    """Leave the call chain, or raise ``SessionClaimResetError``. Never silent."""
    try:
        _request_call_depth.reset(token)
    except Exception as exc:  # noqa: BLE001 -- re-raised, named
        raise SessionClaimResetError(
            f"call-depth scope could not be cleared: {type(exc).__name__}: {exc}"
        ) from exc


def session_claim_conflict(popped: str) -> str:
    """For a RE-ENTRY that popped ``popped``: the refusal reason, or ``""``.

    Equal to the held claim is fine (and is never re-bound). Anything else
    non-empty -- a different claim, or a claim where the request holds none --
    is a conflict.
    """
    claim = str(popped or "").strip()
    if not claim:
        return ""
    return "" if claim == current_request_session_claim() else REASON_SESSION_CLAIM_CONFLICT


def resolve_leased_conversation(
    project_root: Path | str | None,
    *,
    window_key: str | None,
    session_claim: str | None = None,
) -> tuple[str, str]:
    """``(conversation, reason)`` for one window. EXACTLY ONE is non-empty.

    THE ONLY RESOLUTION. There is no ladder, no second rung, and no argument
    that can make this function answer from anything but the row keyed on
    ``window_key``. A window with no row is ``("", REASON_NO_CONVERSATION_BOUND)``
    even when the store holds rows for a dozen other windows — "the newest
    session we have for this project" is a cross-tenant identity leak wearing a
    bugfix costume (#876's own correction, and #672's hardening).
    """
    window = str(window_key or "").strip()
    if not window:
        return "", REASON_NO_WINDOW_KEY
    if not WINDOW_KEY_SHAPE.fullmatch(window):
        return "", REASON_MALFORMED_WINDOW_KEY
    if project_root is None:
        return "", REASON_NO_PROJECT_ROOT
    try:
        root = Path(str(project_root))
    except Exception:  # noqa: BLE001 -- an unusable root is an honest refusal
        return "", REASON_NO_PROJECT_ROOT

    try:
        from .window_binding_store import WindowBindingStore

        row = WindowBindingStore().window_conversation(root, window)
    except Exception:  # noqa: BLE001 -- an unreadable store proves nothing
        row = {}
    if not isinstance(row, dict) or not row:
        return "", REASON_NO_CONVERSATION_BOUND

    # THE ROW MUST BE THE ROW THAT WAS ASKED FOR. This looks redundant against
    # a ``WHERE window_key = ?`` and is not: it is the assertion that survives
    # somebody widening that query, adding an ORDER BY / LIMIT 1 "recovery", or
    # writing a helper that answers with the most recent row. A lookup that can
    # return ANOTHER WINDOW'S CONVERSATION is the whole defect, restored.
    if str(row.get("window_key") or "").strip() != window:
        return "", REASON_NO_CONVERSATION_BOUND

    # #1192: A MULTI-SESSION WINDOW ANSWERS PER SESSION. Its lease is the SET
    # of sessions its own SessionStart announced; the call's claim selects one
    # of THEM or nobody. The single-lease column below is never consulted for
    # such a window -- it names whichever session announced last, which is
    # exactly the cross-session answer the operator ruled out.
    #
    # THE WINDOW'S OWN HOST KIND DECIDES, NOT THE SIZE OF ITS SET (r0b1 on
    # 9d773a60c). "Zero members" used to mean "single-lease", so an OpenCode
    # window whose member write was lost -- or a legacy/migrated row -- fell
    # through to the last-announced column with no claim at all. An OpenCode
    # window is per-session ALWAYS; an empty set refuses.
    claim = str(session_claim or "").strip()
    try:
        from .window_binding_store import WindowBindingStore as _Store
        from .window_binding_store import is_per_session_host

        per_session, member = _Store().window_session_membership(root, window, claim)
        per_session = per_session or is_per_session_host(row.get("host_kind"))
    except Exception:  # noqa: BLE001 -- an unreadable member set proves nothing
        return "", REASON_NO_CONVERSATION_BOUND
    if per_session:
        if not claim:
            return "", REASON_NO_SESSION_CLAIM
        if not member:
            return "", REASON_SESSION_NOT_IN_WINDOW
        return claim, ""

    conversation = str(row.get("host_session_id") or "").strip()
    if not conversation:
        # TWO OPPOSITE STATES WORE ONE REASON, and it cost a live investigation
        # on 2026-08-25. "No row at all" means SessionStart never ran for this
        # window. "A row whose claim is blank" means SessionStart DID run, bound
        # it, and the one-conversation-one-window release later cleared it --
        # opposite causes, opposite remedies. Reporting the second as the first
        # sends the reader to chase a hook that fired correctly.
        #
        # previous_host_session_id is what separates them: the release writes
        # the displaced conversation there, so a non-empty previous on a blank
        # claim IS the signature of a release.
        if str(row.get("previous_host_session_id") or "").strip():
            return "", REASON_CLAIM_RELEASED
        return "", REASON_NO_CONVERSATION_BOUND
    return conversation, ""


# ── The request-scoped answer ────────────────────────────────────────────
#
# CONTEXTVAR, NOT A PROCESS GLOBAL — #672's lesson, one axis up: on a
# multi-tenant daemon a process global is A DIFFERENT ACTOR. Resolved ONCE per
# request so the refusal site and the identity stamp cannot disagree about why.
_request_lease_reason: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_lease_reason",
    default=None,
)


def set_request_lease_reason(reason: str):
    """Record WHY this request has no leased identity. Reset in ``finally``."""
    return _request_lease_reason.set(str(reason or "").strip() or None)


def reset_request_lease_reason(token) -> None:
    """Release the binding. Never raises: it runs in a ``finally`` on the hot path."""
    try:
        _request_lease_reason.reset(token)
    except Exception:  # noqa: BLE001
        pass


def current_request_lease_reason() -> str:
    """Why this request's lease could not answer, or ``""`` when it could."""
    return _request_lease_reason.get() or ""


def resolve_request_lease(
    project_root: Path | str | None,
    *,
    session_claim: str | None = None,
) -> tuple[str, str]:
    """``(conversation, reason)`` for THE CURRENT REQUEST's window.

    Reads the window from the request-scoped ContextVar the transport stamped
    (``X-Aidocs-Window``) and nothing else. A request that proved no window has
    no window; it does not borrow one from the conversation header, the
    transport token, or the last request that ran on this worker.
    """
    from .mcp_server_runtime_helpers import current_request_window_key

    return resolve_leased_conversation(
        project_root,
        window_key=current_request_window_key(),
        session_claim=session_claim,
    )


def lease_refusal(
    reason: str,
    *,
    window_key: str = "",
    tool_name: str = "",
) -> dict[str, Any]:
    """The axis-naming refusal. ``blocked_by`` IS the axis, never a generic id.

    Shaped like the other daemon refusals (``ok``/``blocked_by``/``error``) so a
    caller can surface it unchanged, and carrying ``window_key`` so an operator
    reading a refusal can see which window was — or was not — identified.
    """
    return {
        "ok": False,
        "blocked_by": reason or REASON_NO_WINDOW_KEY,
        "rule_id": reason or REASON_NO_WINDOW_KEY,
        "window_key": str(window_key or "").strip(),
        "tool": str(tool_name or "").strip(),
        "error": _refusal_text(reason),
        "source": LEASE_SOURCE,
    }


def describe_lease(
    project_root: Path | str | None,
    *,
    window_key: str | None,
) -> dict[str, Any]:
    """The lease as a NAMED CHANNEL, for instruments that must cite a source.

    ``ai_whoami``'s contract is that every value says where it came from. This
    returns the conversation AND the reason it is absent, never one without the
    other, so the instrument can print a blank without inventing a cause.
    """
    window = str(window_key or "").strip()
    conversation, reason = resolve_leased_conversation(
        project_root, window_key=window
    )
    return {
        "window_key": window,
        "host_session_id": conversation,
        "reason": reason,
        "source": LEASE_SOURCE,
    }


def channels_agree(header_value: str, leased_value: str) -> bool | str:
    """Do the TRANSPORT's cached conversation and the LEASE's agree?

    THIS IS THE COMPARISON THAT WAS NOT BEING PERFORMED. The previous version
    compared ``request_header`` against ``resolved_caller`` — and
    ``resolved_caller`` WAS the header, so it compared the header to itself. It
    reported ``true`` for a stale shim, a fresh shim and a resumed window alike;
    the operator measured it saying ``true`` while the header was THREE
    conversation rotations behind. A check that cannot fail is not a check, and
    this one was printed as reassurance beside the divergence it could not see.

    Returns ``UNVERIFIABLE`` — never ``True`` — when there is no lease to
    compare against. #588 D5's doctrine applied to identity: report what was
    actually observed, or say it could not be checked.
    """
    leased = str(leased_value or "").strip()
    if not leased:
        return UNVERIFIABLE
    header = str(header_value or "").strip()
    return bool(header) and header == leased


def conductor_liveness_oracle(project_root: Path | str | None):
    """The `is_live` oracle for `AidocsManagedStore.classify_conductor_bindings`.

    Returns a callable `(row) -> True | False | None`:
      * ``True``  — that conductor's id holds a lease on at least one window.
      * ``False`` — the lease store was READ and does not carry it. Positive
        evidence of "not a window", which is what makes a phantom prunable.
      * ``None``  — could not be established. UNPROVABLE; the caller must never
        read it as a denial, because its False bucket gets DELETED.

    WHY THIS LIVES HERE AND IS PASSED IN. `aidocs_managed_store` is one of the
    six modules `test_the_window_never_reaches_an_authority_predicate` scans for
    the window axis, and that guard is deliberate (#880 item 4: the bind must
    stay reachable by an UNBOUND window). Handing the classifier a callable
    keeps its vocabulary its own; the window axis stays on this side of the
    line, where it is already lawful.

    WHAT THIS REPLACES, and why the swap is sound rather than a rename. The
    retired rung read the #464 chain: in it meant LIVE, absent from a non-empty
    chain meant DEAD. It was doing TWO jobs with one predicate —

      * correctly pruning phantom bindings minted by rotating request ids
        (#599; without it `correlate_host_session` refuses on >=2 live bindings,
        the #787 lockout), and
      * incorrectly killing a LIVE window the cap-16 FIFO chain had evicted
        (#892, measured: the binding was deleted at boot and the window was
        refused managed_mode_not_active with no way back).

    The lease separates them because it does not evict. A live window that the
    chain dropped still HAS its SessionStart row, so it answers True. A phantom
    request id never had one, so it answers False. Same two verdicts the chain
    gave, from a source where absence actually means something.
    """
    from .window_binding_store import WindowBindingStore

    def _is_live(row: object) -> bool | None:
        try:
            sid = str((row or {}).get("cli_session_id") or "").strip()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 -- a shape we cannot read proves nothing
            return None
        if not sid or project_root is None:
            return None
        try:
            # NO EMPTY-STORE GUARD HERE, DELIBERATELY. One was written and then
            # REMOVED after a mutation gate proved it could not change any
            # outcome: the classifier's per-session rule already requires that
            # the oracle have attested SOMETHING about a row's session before a
            # False is admissible as death, and an empty store attests nothing,
            # so every row grades UNPROVABLE anyway. Deleting the guard was
            # mutating it into a no-op; a check that cannot fail is not a check,
            # and leaving it would have been an unfalsifiable claim in a
            # docstring. The protection is real -- it just lives in
            # `classify_conductor_bindings`, with a test that fails without it.
            return WindowBindingStore().conversation_is_bound(Path(project_root), sid)
        except Exception:  # noqa: BLE001 -- unreadable is UNPROVABLE, never a denial
            return None

    return _is_live
