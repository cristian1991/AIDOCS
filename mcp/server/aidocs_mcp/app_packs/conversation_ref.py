"""``host_conversation_ref`` -- RFC 0003 v4.6.1 §12.1, §6.9.4, Appendix A H2 (build plan S15).

An opaque, AIDOCS-derived correlation id for the CURRENT host conversation,
derived at CALL TIME from exactly two inputs:

* the authenticated AIDOCS principal (the validated bearer's ``user_id``, via
  :func:`~aidocs_mcp.webmcp_identity.principal_anchor`), and
* the host conversation claim ``openai/session`` from the request ``_meta``.

It REUSES the #1074 derivation primitive
(:func:`~aidocs_mcp.webmcp_identity.compose_host_identity`) and NEVER its
selection authority: nothing here reads or writes the gate's surface binding
store, an org / project / session selection, or any other durable state. It
is pure: same principal + same claim -> same ref (so it survives token
rotation), a different claim or a different principal -> a different ref.

The primitive's output (AIDOCS' own host-session key) is re-hashed under a
distinct domain tag, so the application never learns the value AIDOCS uses to
key its own surfaces.

Absent, never faked (§12.1). No authenticated principal, or a missing /
malformed conversation claim, derives ``None``; there is no fallback to any
other claim, the transport token, a native AIDOCS envelope or an earlier ref
(Appendix A H2). ``None`` handed to
:meth:`~.transport.ExternalAppTransport.call` on a mode declaring
``requires_conversation_context`` is refused ``app_conversation_context_unavailable``
before egress.

The ref is EVIDENCE (§3.6): a call fact and row metadata, a routing /
default-subject key for the application, never permission and never a member
of :class:`~.scope.AppScope`.
"""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Mapping, Optional

from .. import webmcp_identity

__all__ = [
    "CONVERSATION_CLAIM",
    "CONVERSATION_CLAIM_MAX_BYTES",
    "HOST_CONVERSATION_REF_PREFIX",
    "derive_host_conversation_ref",
]

#: The ONE host conversation claim (Appendix A H2). Named by the #1074 module.
CONVERSATION_CLAIM = webmcp_identity.META_SESSION
HOST_CONVERSATION_REF_PREFIX = "hcr_"
#: No sealed number bounds the claim; S15 encoding choice (flagged): a claim
#: over this many UTF-8 bytes is MALFORMED and derives nothing.
CONVERSATION_CLAIM_MAX_BYTES = 512
_DOMAIN = b"aidocs-app-host-conversation-ref/v1\x00"


def _well_formed_claim(meta: Any) -> Optional[str]:
    """The raw conversation claim if it is well formed, else ``None``.

    Checked on the RAW value (the #1074 reader strips whitespace, which would
    let ``" conv"`` and ``"conv"`` collapse): a non-empty string with no
    surrounding whitespace, no control characters, within the byte bound.
    """
    if not isinstance(meta, Mapping):
        return None
    claim = meta.get(CONVERSATION_CLAIM)
    if type(claim) is not str or not claim or claim != claim.strip():
        return None
    if len(claim.encode("utf-8", "surrogatepass")) > CONVERSATION_CLAIM_MAX_BYTES:
        return None
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in claim):
        return None
    return claim


def derive_host_conversation_ref(*, principal: Any, meta: Any) -> Optional[str]:
    """The §12.1 ``host_conversation_ref`` for this call, or ``None`` (never faked).

    ``principal`` is the authenticated principal dict the gate resolved for
    THIS request; ``meta`` is that request's ``params._meta``.
    """
    if isinstance(meta, Mapping) and webmcp_identity.is_native_aidocs_meta(dict(meta)):
        # The primitive would answer from the native envelope instead: a
        # different source. No substitution, no fallback (H2).
        return None
    claim = _well_formed_claim(meta)
    if claim is None:
        return None
    if not webmcp_identity.principal_anchor(principal if isinstance(principal, dict) else None):
        return None
    # Only the conversation claim reaches the primitive: no other host claim
    # can steer the derivation.
    host_key, _kind = webmcp_identity.compose_host_identity(principal=principal, meta={CONVERSATION_CLAIM: claim})
    if not host_key.startswith(webmcp_identity.HOST_SESSION_PREFIX):
        return None
    digest = hashlib.sha256(_DOMAIN + host_key.encode("utf-8")).hexdigest()
    return HOST_CONVERSATION_REF_PREFIX + digest
