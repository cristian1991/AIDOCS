"""WHO is calling on a surface with no hooks (#906, and the third writer #848 names).

THE PROBLEM, stated by #848 from a live reproduction: `current_calling_host_session_id()`
reads a per-request ContextVar written ONLY by the UserPromptSubmit hook path, or a
process global written ONLY by the stdio bridge. "An MCP tool call on a surface with no
shim feeds neither slot." The outer gate is exactly that surface — `host_support_matrix`
already classifies it `mcp-only`, "no host hooks; only the MCP tools/call boundary" — so
every web caller resolved to an EMPTY host session, managed mode could not bind, and
`ai_agents` refused with `managed_mode_inactive`. Correct behaviour on absent identity;
the defect was that identity was absent when the host had, in fact, supplied one.

WHAT THE HOST SUPPLIES, on every tools/call, in JSON-RPC `params._meta`:

    openai/subject        a stable anonymized USER
    openai/session        an anonymized CHAT / CONVERSATION
    openai/organization   an anonymized ChatGPT ORG

The gate parsed the request and threw all three away.

═══ THE RULE THAT SHAPES EVERYTHING BELOW ═══

THESE ARE CLIENT-SUPPLIED CLAIMS, NOT AUTHENTICATED FACTS. Anyone who can reach the
endpoint can type any value into `_meta`. The gate already holds this line for the
adjacent case — the backlog route's own docstring says "the caller's projectId is a
CLAIM ... never one supplied by the caller" — and this module holds it for identity:

  * A CLAIM NEVER GRANTS PERMISSION. The bearer token stays the sole authority for what
    the caller may DO. `openai/subject` is not a login, and nothing here touches a role,
    a scope or an entitlement. This establishes WHO, never WHAT.

  * THE BINDING KEY COMPOSES THE CLAIM WITH THE AUTHENTICATED PRINCIPAL. Keyed on
    `openai/session` alone, any caller could assert someone else's conversation id and
    adopt their managed-mode binding — unauthenticated session fixation, opened by a
    feature whose entire purpose was to end a lockout. Composed, the same claim under a
    different token is a DIFFERENT session, so the claim can only ever address
    conversations the caller already owns.

  * NO PRINCIPAL, NO IDENTITY. An unauthenticated request has nothing to compose
    against; it gets "" rather than an id derived from the claim alone.

  * NO FALLBACK. Operator law, 2026-08-23: "fallbacks can stamp wrong data and we cannot
    tell from where. identity has no fallback." A request that carried no conversation
    claim HAS no host session. It does not borrow one from the transport token, the
    process, or the last request that happened to run on this worker — which is the whole
    reason the window axis is a ContextVar with no ladder.

  * THE DERIVATION IS ONE-WAY. The id is a digest, not a join of the two inputs, so the
    anonymized subject/session the host chose to anonymise are not re-exposed in a field
    that lands in logs, audit rows and error messages.
"""

from __future__ import annotations

import hashlib

#: The documented client `_meta` keys. Named once so a typo cannot silently
#: become "the host sent nothing".
META_SUBJECT = "openai/subject"
META_SESSION = "openai/session"
META_ORGANIZATION = "openai/organization"

#: AIDOCS-owned metadata carried by an AIDOCS edge when it forwards a native
#: local/remote/server agent through the same stateless MCP gate used by WebMCP.
#: These are CLAIMS, exactly like openai/session: the gate composes identity
#: with the authenticated principal and uses project/session only as equality
#: constraints against its own authoritative selection. They never select or
#: grant anything by themselves.
META_AIDOCS_HOST_SESSION = "aidocs/hostSession"
META_AIDOCS_HOST_KIND = "aidocs/hostKind"
#: #1007 THE SUBAGENT AXIS ACROSS THE WIRE. A CC subagent INHERITS its parent's
#: host session, so the composed host-session claim alone cannot tell parent
#: from child and the gate re-derived the conductor for every forwarded call.
#: This names WHICH child of that already-composed session is speaking. Like
#: every other claim here it selects and grants NOTHING: it only sharpens an
#: identity the principal already earned. Absent ⇒ the parent, byte for byte.
META_AIDOCS_HOST_AGENT = "aidocs/hostAgent"
META_AIDOCS_PROJECT_ID = "aidocs/projectId"
META_AIDOCS_SESSION_ID = "aidocs/sessionId"
NATIVE_HOST_SESSION_PREFIX = "mcp-"
#: Marks ids minted here, so a host session id's ORIGIN is readable at a glance
#: in an audit row rather than inferred from its shape.
HOST_SESSION_PREFIX = "web-"

#: What `host_kind` an mcp-only web caller is recorded as.
HOST_KIND = "web_mcp"

#: Enough digest to make collision irrelevant, short enough to read in a log.
_DIGEST_CHARS = 32


def _claim(meta: dict | None, key: str) -> str:
    """One `_meta` value as a clean string, or "". Never raises."""
    if not isinstance(meta, dict):
        return ""
    value = meta.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def aidocs_project_claim(meta: dict | None) -> str:
    """Native AIDOCS project's claimed canonical id, for equality-check only."""
    return _claim(meta, META_AIDOCS_PROJECT_ID)


def aidocs_session_claim(meta: dict | None) -> str:
    """Native AIDOCS session claim, for equality-check only."""
    return _claim(meta, META_AIDOCS_SESSION_ID)


def aidocs_host_agent_claim(meta: dict | None) -> str:
    """The CC per-subagent ``agent_id`` this request claims, or "" (#1007).

    "" is the complete answer for the main thread and for every host that has
    no subagent axis at all — there is no fallback rung, exactly as
    ``current_calling_agent_id`` has none: a borrowed agent id could only ever
    name a stale sibling.
    """
    return _claim(meta, META_AIDOCS_HOST_AGENT)


def is_native_aidocs_meta(meta: dict | None) -> bool:
    """Whether this request declares the AIDOCS edge identity envelope."""
    return bool(_claim(meta, META_AIDOCS_HOST_SESSION) or _claim(meta, META_AIDOCS_HOST_KIND))


def compose_host_identity(*, principal: dict | None, meta: dict | None) -> tuple[str, str]:
    """Return ``(host_session_id, host_kind)`` for web or native AIDOCS MCP.

    Native AIDOCS metadata is deliberately a claim, not authority. The opaque
    local host-session value is never reused directly: it is domain-separated
    and hashed with the authenticated user id and claimed host kind, so another
    principal cannot address the same actor by copying the claim. A partial
    native envelope fails closed instead of falling through to WebMCP identity.
    """
    anchor = principal_anchor(principal)
    native_sid = _claim(meta, META_AIDOCS_HOST_SESSION)
    native_kind = _claim(meta, META_AIDOCS_HOST_KIND)
    # Asks the NAMED predicate rather than re-deriving it. This guard used to
    # read `if native_sid or native_kind:` — byte-identical in meaning to
    # `is_native_aidocs_meta`, which sat directly above it with no caller. Two
    # homes for one question: vulture caught the unused half, and the half that
    # ran was the unnamed one. Wiring it is the smaller repair than deleting a
    # documented predicate, and it leaves exactly one definition of "does this
    # request declare the AIDOCS edge envelope".
    if is_native_aidocs_meta(meta):
        if not anchor or not native_sid or not native_kind:
            return "", native_kind or "unknown"
        digest = hashlib.sha256(
            b"aidocs-native-mcp-host-session\x00"
            + anchor.encode("utf-8")
            + b"\x00"
            + native_kind.encode("utf-8")
            + b"\x00"
            + native_sid.encode("utf-8")
        ).hexdigest()[:_DIGEST_CHARS]
        return f"{NATIVE_HOST_SESSION_PREFIX}{digest}", native_kind

    conversation = _claim(meta, META_SESSION)
    if not anchor or not conversation:
        return "", HOST_KIND
    digest = hashlib.sha256(
        b"aidocs-webmcp-host-session\x00"
        + anchor.encode("utf-8")
        + b"\x00"
        + conversation.encode("utf-8")
    ).hexdigest()[:_DIGEST_CHARS]
    return f"{HOST_SESSION_PREFIX}{digest}", HOST_KIND


def principal_anchor(principal: dict | None) -> str:
    """The AUTHENTICATED half of the composition, or "" when there is none.

    `default_principal_resolver` returns `user_id` for a validated bearer token and
    None for a missing/invalid/expired one, so "" here means "no authenticated
    caller" — the case that must NOT produce an identity.
    """
    if not isinstance(principal, dict):
        return ""
    if not principal.get("authenticated"):
        return ""
    return str(principal.get("user_id") or "").strip()


def compose_host_session_id(*, principal: dict | None, meta: dict | None) -> str:
    """Compatibility read of the composed host-session id."""
    return compose_host_identity(principal=principal, meta=meta)[0]


def attribution(principal: dict | None, meta: dict | None) -> dict:
    """What to record about this caller, for AUDIT only — never for a decision."""
    host_session_id, host_kind = compose_host_identity(principal=principal, meta=meta)
    return {
        "host_session_id": host_session_id,
        "host_kind": host_kind,
        "principal": principal_anchor(principal),
        "subject_claim": _claim(meta, META_SUBJECT),
        "session_claim": _claim(meta, META_SESSION),
        "org_claim": _claim(meta, META_ORGANIZATION),
        "aidocs_host_session_claim": _claim(meta, META_AIDOCS_HOST_SESSION),
        "aidocs_host_kind_claim": _claim(meta, META_AIDOCS_HOST_KIND),
        "aidocs_host_agent_claim": aidocs_host_agent_claim(meta),
        "aidocs_project_claim": aidocs_project_claim(meta),
        "aidocs_session_claim": aidocs_session_claim(meta),
        "claims_are_unauthenticated": True,
    }


def meta_from_rpc(msg: dict | None) -> dict:
    """`params._meta` out of a JSON-RPC message, or {}. Never raises."""
    if not isinstance(msg, dict):
        return {}
    params = msg.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}
