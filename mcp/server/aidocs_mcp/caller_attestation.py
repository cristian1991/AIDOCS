"""WHICH CALLER, per surface — and never WHO, the token, or the session.

THE AXIS. Empire law promoted-cc6c4ac686ee keeps four facts apart: WHO (the
authenticated user), WHICH CALLER (the surface instance issuing this call),
WHICH SESSION (the managed binding) and WHICH LANE. This module owns the
second one, and only the second one.

WHY A SECOND CLASS EXISTS. ``window_key`` — ``<host pid>:<host creation
filetime>`` — is measured from the operating system in the hook process and
cannot be forged by anything that does not already own the box. It is the
strongest caller fact AIDOCS has, and it exists on exactly one surface: a
window a human started on this machine. A remote OAuth caller has no process
here, so ``window_from_payload`` correctly returns nothing for it.

THE MEASURED DEFECT (2026-09-05). The remote surface already had a correct
answer and the run path threw it away. ``outer_gate_transport`` composes the
client's ``openai/session`` conversation claim WITH the authenticated
principal into ``web-<digest>`` (``webmcp_identity.compose_host_identity``)
and scopes it to the request — then ``outer_gate`` hands the executor
``"ogh_" + token_id``. THAT IS THE TOKEN RESTATED, and it is wrong in both
directions: two browser tabs on one token are two callers reported as one, and
a token refresh reports one caller as two.

THE TWO CLASSES, AND WHY THEY MUST NOT SHARE A NAME.

  os_window            value: ``<pid>:<creation filetime>``
                       attested by: this host's own process table, measured in
                       the hook process, never re-derived downstream.

  remote_conversation  value: the COMPOSED ``web-``/``mcp-`` host session
                       attested by: ``webmcp_identity``, which digests the
                       client's conversation claim together with the
                       authenticated principal. The claim alone is worth
                       nothing — anyone reaching the endpoint can type any
                       value into ``_meta`` — and the composition is what makes
                       it a fact: the same claim under a different principal is
                       a DIFFERENT caller, so a claim can only ever address
                       conversations its own principal already owns.

The second is weaker than the first: it is attested by our own composition
rather than by the operating system. Giving it its own class name is the point
— no consumer can quietly treat them as interchangeable, and no future
"attested window" check can be satisfied by a remote conversation that merely
looks like one.

A caller key is therefore NAMESPACED (``win:...`` / ``rconv:...``). Two values
that mean different things must not be able to compare equal, and a bare value
column would let them: the class is part of the key rather than an attribute
hanging beside it.

FAIL CLOSED, AND NAME THE SURFACE THAT OWED ONE. An absent attestation is not
an empty string passed onward — it is a refusal that says which surface failed
to supply what, so an operator reading it can tell "this window could not be
measured" from "this remote client sent no conversation claim" from "this
remote caller was not authenticated, so there was nothing to compose against".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .webmcp_identity import (
    HOST_SESSION_PREFIX as _WEB_PREFIX,
)
from .webmcp_identity import (
    NATIVE_HOST_SESSION_PREFIX as _NATIVE_PREFIX,
)
from .window_key import WINDOW_KEY_SHAPE

# ── the classes ──────────────────────────────────────────────────────────────
#: Measured from the OS process table in the hook process. The strong class.
CLASS_OS_WINDOW = "os_window"
#: ChatGPT / OpenAI MCP: authenticated WHO composed with the client's
#: ``openai/session`` conversation claim.
CLASS_OPENAI_CONVERSATION = "openai_conversation"
#: A native remote AIDOCS edge: authenticated WHO composed with the edge's own
#: ``aidocs/hostSession`` caller claim.
CLASS_NATIVE_REMOTE = "native_remote_conversation"

#: THREE ATTESTED SURFACES, THREE NAMES (operator ruling 2026-09-05). The two
#: remote classes are composed the same way and are equally strong, and it
#: would be tempting to call them one thing and tell them apart by looking at
#: the value's prefix. That is exactly the habit that produced this programme's
#: worst defect -- a guarantee asserted in prose while the code decided by
#: inspection. The CLAIM ORIGIN differs (a third-party client's conversation
#: versus an AIDOCS edge's own caller id), an operator reading an audit row
#: needs to know which, so each gets a name and carries it in the key.
_PREFIX = {
    CLASS_OS_WINDOW: "win",
    CLASS_OPENAI_CONVERSATION: "rconv",
    CLASS_NATIVE_REMOTE: "nconv",
}
_CLASS_BY_PREFIX = {v: k for k, v in _PREFIX.items()}


# ── the shapes ───────────────────────────────────────────────────────────────
# WINDOW_KEY_SHAPE is IMPORTED, not restated: window_key MINTS that value, so it
# owns the shape, and a second copy here would drift out of agreement with the
# minter exactly as a stale covering-file name once did.

#: The composed remote caller ids. THE PREFIXES ARE NOT SPELLED OUT HERE --
#: they are webmcp_identity's own constants, because that module mints the
#: values. A literal "web-" here would be a second definition free to drift
#: from the composer, which is the failure mode this file keeps refusing.
_OPENAI_SHAPE = re.compile(re.escape(_WEB_PREFIX) + r"[0-9a-f]{32}")
_NATIVE_SHAPE = re.compile(re.escape(_NATIVE_PREFIX) + r"[0-9a-f]{32}")
_SHAPE_BY_CLASS = {
    CLASS_OPENAI_CONVERSATION: _OPENAI_SHAPE,
    CLASS_NATIVE_REMOTE: _NATIVE_SHAPE,
}

# ── the refusals. ONE NAME PER CAUSE ─────────────────────────────────────────
# A single "no attestation" would reproduce the defect this spine exists to
# remove: an empty answer that cannot say where it came from or what to do.

#: Local surface, and the hook payload carried no measured window.
REASON_NO_OS_WINDOW = "local_surface_supplied_no_measured_window"
#: A window value arrived but is not ``<pid>:<filetime>``.
REASON_MALFORMED_WINDOW = "os_window_key_malformed"
#: Remote surface, and no composed caller id — webmcp_identity returned "".
#: THE TWO HALVES ARE NOT DISTINGUISHED HERE ON PURPOSE: the composition needs
#: both an authenticated principal and a conversation claim, and only the
#: composer knows which was missing. `ai_whoami` shows that via the uncomposed
#: attribution (#935); this module refuses to guess at it.
REASON_NO_REMOTE_CONVERSATION = "remote_surface_supplied_no_composed_conversation"
#: A remote value arrived whose shape proves it did not come from the
#: composition, i.e. it carries no principal binding.
REASON_UNCOMPOSED_REMOTE_VALUE = "remote_caller_value_not_principal_composed"
#: A stored caller key whose namespace names no class we know.
REASON_UNKNOWN_CLASS = "caller_attestation_class_unknown"
#: A stored key whose namespace and value name DIFFERENT origins.
REASON_CLASS_VALUE_DISAGREE = "caller_key_class_disagrees_with_value"


@dataclass(frozen=True)
class CallerAttestation:
    """WHICH CALLER, plus the class saying how strongly it is known.

    FROZEN because an attestation is a MEASUREMENT, not a working variable. A
    consumer able to mutate the class in flight could downgrade an OS window
    to a remote conversation or — far worse — upgrade the reverse, which is
    precisely the substitution the class distinction exists to prevent.
    """

    attestation_class: str
    value: str

    @property
    def key(self) -> str:
        """The namespaced storage/lookup key, e.g. ``win:13336:1343…``."""
        return f"{_PREFIX[self.attestation_class]}:{self.value}"

    @property
    def is_os_window(self) -> bool:
        """True only for the OS-measured class. Asked by callers that require
        the strong attestation and must refuse the weaker ones by NAME rather
        than by comparing prefixes themselves."""
        return self.attestation_class == CLASS_OS_WINDOW



def os_window(window_key: str) -> tuple[CallerAttestation | None, str]:
    """``(attestation, reason)`` for a MEASURED OS window key."""
    value = str(window_key or "").strip()
    if not value:
        return None, REASON_NO_OS_WINDOW
    if not WINDOW_KEY_SHAPE.fullmatch(value):
        return None, REASON_MALFORMED_WINDOW
    return CallerAttestation(CLASS_OS_WINDOW, value), ""


def remote_conversation(composed_host_session: str) -> tuple[CallerAttestation | None, str]:
    """``(attestation, reason)`` for a COMPOSED remote caller id.

    THE CLASS IS READ FROM THE COMPOSER'S OWN PREFIX, not chosen by the caller.
    ``webmcp_identity`` stamps ``web-`` for a ChatGPT/OpenAI conversation and
    ``mcp-`` for a native AIDOCS edge, and it is the only thing that knows
    which composition it just performed. Letting a call site declare the class
    would let the two origins be relabelled as each other at the one place
    where an audit reader needs them apart.

    THE ARGUMENT IS THE COMPOSITION'S OUTPUT, NEVER THE RAW CLAIM. Passing a
    client's ``openai/session`` value here is the client-asserted-fact bug in a
    new costume, and the shape check makes that mistake loud rather than
    silent: a raw claim does not match either prefix, so it is refused with
    ``REASON_UNCOMPOSED_REMOTE_VALUE`` instead of accepted as identity.
    """
    value = str(composed_host_session or "").strip()
    if not value:
        return None, REASON_NO_REMOTE_CONVERSATION
    for klass, shape in _SHAPE_BY_CLASS.items():
        if shape.fullmatch(value):
            return CallerAttestation(klass, value), ""
    return None, REASON_UNCOMPOSED_REMOTE_VALUE


def parse_key(caller_key: str) -> tuple[CallerAttestation | None, str]:
    """Read a stored namespaced key back into an attestation.

    Splits on the FIRST colon only. An OS window value contains a colon of its
    own, so a greedy split would truncate every window key ever stored -- and
    would do it silently, producing a well-formed-looking key for a window that
    was never measured.

    THE NAMESPACE AND THE VALUE MUST AGREE. A stored ``rconv:mcp-<digest>``
    names an OpenAI conversation while carrying a native edge's value, so one
    of the two halves is wrong and nothing here can say which. Trusting either
    would relabel one attested origin as the other at exactly the place an
    audit reader relies on them being distinct, so the row is refused.
    """
    raw = str(caller_key or "").strip()
    prefix, sep, value = raw.partition(":")
    if not sep:
        return None, REASON_UNKNOWN_CLASS
    klass = _CLASS_BY_PREFIX.get(prefix)
    if klass is None:
        return None, REASON_UNKNOWN_CLASS
    if klass == CLASS_OS_WINDOW:
        return os_window(value)
    attestation, reason = remote_conversation(value)
    if attestation is None:
        return None, reason
    if attestation.attestation_class != klass:
        return None, REASON_CLASS_VALUE_DISAGREE
    return attestation, ""
