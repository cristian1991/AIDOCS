"""Lane 1 MCP Events: the delivery worker that wakes a subscribed ChatGPT Work chat.

One call, deliver_due, does one delivery attempt end to end:

  1. claim the next due wake (mcp_events_store.claim_due; the body is frozen there);
  2. re-check authority for the subscription (the caller's `authorized`); lost
     authority revokes the subscription and nothing is sent;
  3. open the whsec_ secret(s) with the gate keyring, bound to the subscription. A
     secret that does not open (its key missing from the ring, or a row that fails
     authentication) PARKS the wake without spending an attempt and leaves the
     subscription alone: a key can come back after a rollout or a misconfiguration,
     and neither case says anything about the subscriber's authority (r0b1
     603ae761-420). The wake goes out once the key is back, or the subscription
     simply reaches its own expiry;
  4. RE-VALIDATE the callback now, not only at subscribe time: HTTPS, no
     credentials, and EVERY resolved address public, then connect to the address we
     validated (pinned), so a DNS answer that changed since subscribe cannot steer the
     request inward. A refusal here is an attempt that failed, so it backs off and
     eventually dies;
  5. sign per Standard Webhooks (both secrets during a rotation window) and POST;
  6. settle the attempt with the lease the claim holds (record_attempt).

A 2xx means ChatGPT RECEIVED the event, nothing more. Network errors count as a
failed attempt (status 0) and retry. The event carries ids and counts only: the woken
chat reads the messages themselves through ai_msg.

DNS and the HTTP POST are injected (`resolve`, `post`), so the policy is testable
without a network; https_post_pinned is the production POST.

Design: .MEMORY/sessions/ubermega/lane1-mcp-events-wake-design.md §3, §6.
"""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import logging
import os
import socket
import secrets
import sqlite3
import ssl
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import mcp_events_store as store
from .governed_egress import EgressRefused, assert_egress_allowed
from .mcp_events_secret import Keyring, Sealed, WebhookSecretError, signature_header

logger = logging.getLogger("aidocs.mcp_events_delivery")

TIMEOUT_SECONDS = 10
#: How long a wake whose secret cannot be opened waits before it is tried again.
KEY_OUTAGE_RETRY_SECONDS = 300
#: The spec caps a delivery request body at 256 KiB; ours are tiny, so this only
#: guards against a serializer bug ever posting something large.
MAX_BODY_BYTES = 256 * 1024
_RESPONSE_READ_CAP = 64 * 1024


class CallbackRefused(Exception):
    """The callback URL is not one we will send to. Carries no secret material."""


class ChallengeFailed(Exception):
    """The callback did not prove it holds the secret. `reason` is the spec's
    categorized value for -32015 data.reason: challenge_failed or timeout."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuthorityUnknown(Exception):
    """The authority recheck could not get an answer (the membership authority is
    down, say). Like a missing key, this is an outage and not a verdict: the wake is
    parked and the subscription kept."""


@dataclass(frozen=True)
class Target:
    """A validated callback: the URL's host (for TLS and the Host header) and the
    exact public address we checked and will connect to."""

    host: str
    port: int
    ip: str
    path: str


@dataclass(frozen=True)
class Delivery:
    event_id: str
    #: sent | revoked | secret_unavailable | authority_unknown | callback_refused | lease_lost
    #: | binding_without_client (r0b2 B5) | held_paused | binding_inactive (r0b2 B6)
    #: | body_too_large (r0b2 B8). "sent" ONLY after post() was called (a
    #: transport failure is still an attempt: sent with status 0).
    outcome: str
    status_code: int | None


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_global and not addr.is_multicast


#: Callback hosts a wake may go to (governed egress, subdomains included), unless the
#: operator sets AIDOCS_EVENTS_CALLBACK_HOSTS (comma-separated). OpenAI's own domains:
#: confirm the live callback host at acceptance and set the variable if it differs.
DEFAULT_CALLBACK_HOSTS = ("chatgpt.com", "openai.com")
ENV_CALLBACK_HOSTS = "AIDOCS_EVENTS_CALLBACK_HOSTS"


def callback_allow_hosts(env=None) -> tuple[str, ...]:
    raw = (os.environ if env is None else env).get(ENV_CALLBACK_HOSTS, "")
    hosts = tuple(h.strip().lower() for h in str(raw).split(",") if h.strip())
    return hosts or DEFAULT_CALLBACK_HOSTS


def validate_callback_url(url: str, *, resolve=socket.getaddrinfo) -> Target:
    """The pinned Target for a callback URL, or CallbackRefused. The host must be on
    the callback allowlist (governed egress) as well as resolve only to public
    addresses; this runs at subscribe time and again before every send."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        port = parts.port or 443
    except ValueError:
        raise CallbackRefused("refuse: malformed callback URL") from None
    if parts.scheme != "https":
        raise CallbackRefused("refuse: callback must be https")
    if parts.username is not None or parts.password is not None:
        raise CallbackRefused("refuse: callback URL must not carry credentials")
    host = parts.hostname
    if not host:
        raise CallbackRefused("refuse: callback URL has no host")
    try:
        assert_egress_allowed(url, purpose="mcp_events_callback", allow_hosts=callback_allow_hosts())
    except EgressRefused:
        raise CallbackRefused("refuse: callback host is not on the MCP Events allowlist") from None
    try:
        answers = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise CallbackRefused("refuse: callback host does not resolve") from None
    ips = sorted({str(a[4][0]) for a in answers})
    if not ips:
        raise CallbackRefused("refuse: callback host does not resolve")
    try:
        if not all(_is_public(ip) for ip in ips):
            raise CallbackRefused("refuse: callback resolves to a non-public address")
    except ValueError:
        raise CallbackRefused("refuse: callback resolves to an unparseable address") from None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(host=host, port=port, ip=ips[0], path=path)


def event_body(row: sqlite3.Row, *, now: float) -> bytes:
    """The exact bytes of one event, serialized once (claim_due freezes them). They never
    hold the wake ticket: with_wake_ticket splices it in at send."""
    return _canonical(
        {
            "eventId": row["event_id"],
            "name": row["event_name"],
            "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "data": {
                "session_id": row["session_id"],
                "binding_id": row["binding_id"],
                "pending_count": row["pending_count"],
                "newest_message_id": row["newest_message_id"],
            },
            "cursor": None,
        }
    )


def _canonical(event: dict) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def with_wake_ticket(body: bytes, ticket: str) -> bytes:
    """The frozen body with data.wake_ticket added, deterministically: the same frozen
    bytes and ticket always give the same bytes, so every retry is byte-identical and
    the signature covers exactly what is sent (#1226 continuation, design §10(1))."""
    event = json.loads(body)
    event["data"]["wake_ticket"] = ticket
    return _canonical(event)


def https_post_pinned(
    target: Target, headers: dict[str, str], body: bytes, *, context: ssl.SSLContext | None = None
) -> int:
    """POST to the validated address with TLS verified against the URL's host name.
    Never follows a redirect (a 3xx is just a non-2xx status). `context` defaults to
    the system trust store; tests pass one that trusts a throwaway CA."""
    return https_post_pinned_read(target, headers, body, context=context)[0]


def https_post_pinned_read(
    target: Target, headers: dict[str, str], body: bytes, *, context: ssl.SSLContext | None = None
) -> tuple[int, bytes]:
    """https_post_pinned, also returning the (capped) response body."""
    context = context if context is not None else ssl.create_default_context()
    raw = socket.create_connection((target.ip, target.port), timeout=TIMEOUT_SECONDS)
    try:
        tls = context.wrap_socket(raw, server_hostname=target.host)
    except BaseException:
        raw.close()
        raise
    conn = http.client.HTTPSConnection(target.host, target.port, timeout=TIMEOUT_SECONDS, context=context)
    conn.sock = tls
    try:
        conn.request("POST", target.path, body=body, headers=headers)
        response = conn.getresponse()
        return int(response.status), response.read(_RESPONSE_READ_CAP)
    finally:
        conn.close()


def verify_callback(
    target: Target,
    secret: str,
    subscription_id: str,
    *,
    now: float,
    post_read: Callable[[Target, dict[str, str], bytes], tuple[int, bytes]] = https_post_pinned_read,
) -> None:
    """The spec's activation check: POST a signed {"type":"verification","challenge"}
    with a fresh single-use value; the callback must answer 2xx echoing exactly that
    value (compared in constant time). Anything else raises ChallengeFailed."""
    challenge = secrets.token_urlsafe(32)
    msg_id = "msg_verification_" + secrets.token_hex(8)
    body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode("utf-8")
    timestamp = int(now)
    headers = {
        "Content-Type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature_header([secret], msg_id, timestamp, body),
        "X-MCP-Subscription-Id": subscription_id,
    }
    try:
        status, reply = post_read(target, headers, body)
    except (TimeoutError, socket.timeout):
        raise ChallengeFailed("timeout") from None
    except Exception:  # noqa: BLE001 - any transport failure is a failed challenge
        raise ChallengeFailed("challenge_failed") from None
    if not 200 <= int(status) < 300:
        raise ChallengeFailed("challenge_failed")
    try:
        echoed = json.loads(reply)
    except (ValueError, TypeError):
        raise ChallengeFailed("challenge_failed") from None
    got = echoed.get("challenge") if isinstance(echoed, dict) else None
    if not isinstance(got, str) or not hmac.compare_digest(got.encode("utf-8"), challenge.encode("utf-8")):
        raise ChallengeFailed("challenge_failed")


def _commit(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.commit()


def _secrets(sub: sqlite3.Row, keyring: Keyring, *, now: float) -> list[str]:
    aad = store.subscription_key(sub["binding_id"], event_name=sub["event_name"], callback_url=sub["callback_url"])
    current = keyring.unseal(Sealed(sub["secret_ct"], sub["nonce"], sub["kek_id"]), aad=aad)
    out = [current]
    if sub["prev_secret_ct"] is not None and (sub["prev_valid_until"] or 0) > now:
        try:
            out.append(
                keyring.unseal(Sealed(sub["prev_secret_ct"], sub["prev_nonce"], sub["prev_kek_id"]), aad=aad)
            )
        except WebhookSecretError:
            pass  # the previous secret is a courtesy for the rotation window only
    return out


def deliver_due(
    conn: sqlite3.Connection,
    *,
    keyring: Keyring,
    now: float,
    authorized: Callable[[sqlite3.Row], bool],
    post: Callable[[Target, dict[str, str], bytes], int] = https_post_pinned,
    resolve=socket.getaddrinfo,
    is_stale: Callable[[sqlite3.Row, str], bool] | None = None,
    lease_seconds: float = 30,
    budget=None,
) -> Delivery | None:
    """One delivery attempt for the next due wake, or None when nothing is due.

    `budget` is the gate's global ledger (mcp_events_budget.GateBudget): it admits
    the first claim, and a never-POSTed wake that THIS worker parks gives its slot
    back. A refused park (the lease was lost, so another worker may have delivered
    it) and a revoke (not lease-checked) refund nothing (r0b1 1bf7d7b4-20a)."""
    claim = store.claim_due(
        conn, now=now, serialize=lambda row: event_body(row, now=now),
        lease_seconds=lease_seconds, is_stale=is_stale,
        reserve=(lambda row, token: budget.admit(row, token, now=now)) if budget is not None else None,
    )
    if claim is None:
        return None

    def park(reason: str) -> None:
        # Refund only if this worker still held the lease AND the wake was never
        # POSTed: an earlier failed attempt already reached the callback (parks do
        # not count as attempts).
        parked = store.park_claim(conn, claim, until=now + KEY_OUTAGE_RETRY_SECONDS, reason=reason)
        # Fenced: the reservation is this claim's own (keyed by its lease token), so a
        # newer claim of the same event that got in first keeps its slot.
        if parked and budget is not None and claim.attempts == 1:
            budget.release(claim.lease_token)

    sub = store.get_subscription(conn, claim.subscription_id)
    try:
        allowed = sub is not None and authorized(sub)
    except AuthorityUnknown as exc:
        park("authority_unknown")
        logger.warning("MCP Events wake %s parked: authority unknown (%s)", claim.event_id, exc)
        return Delivery(claim.event_id, "authority_unknown", None)
    if not allowed:
        store.revoke_subscription(conn, claim.subscription_id, reason="authority_lost", now=now)
        _commit(conn)
        return Delivery(claim.event_id, "revoked", None)
    try:
        secrets = _secrets(sub, keyring, now=now)
    except WebhookSecretError:
        reason = "kek_missing" if sub["kek_id"] not in keyring.kek_ids else "secret_unopenable"
        park(reason)
        logger.warning("MCP Events wake %s parked: %s (subscription kept)", claim.event_id, reason)
        return Delivery(claim.event_id, "secret_unavailable", None)
    # The wake ticket (#1226 continuation): its key is recorded before the FIRST POST,
    # so a retry derives the same ticket. A legacy row POSTed before tickets existed
    # (attempts > 1, no key) keeps its exact bytes and carries none.
    body = claim.body_bytes
    ticket_kek = claim.ticket_kek_id
    if ticket_kek is None and claim.attempts == 1:
        # r0b2 B5: a binding without a client never mints a NEW ticket.
        if not store.binding_client(conn, claim.subscription_id):
            if store.drop_unbound_claim(conn, claim) and budget is not None:
                budget.release(claim.lease_token)  # never sent: the slot goes back
            logger.warning("MCP Events wake %s dropped: binding has no client (re-enroll)", claim.event_id)
            return Delivery(claim.event_id, "binding_without_client", None)
        ticket_kek = keyring.current.kek_id
        if not store.record_ticket_kek(conn, claim, ticket_kek):
            return Delivery(claim.event_id, "lease_lost", None)
    if ticket_kek is not None:
        try:
            body = with_wake_ticket(body, keyring.wake_ticket(claim.event_id, ticket_kek))
        except WebhookSecretError:
            park("ticket_kek_missing")
            logger.warning("MCP Events wake %s parked: ticket key left the keyring", claim.event_id)
            return Delivery(claim.event_id, "secret_unavailable", None)
    try:
        target = validate_callback_url(sub["callback_url"], resolve=resolve)
    except CallbackRefused:
        store.record_attempt(conn, claim, status_code=0, now=now)
        return Delivery(claim.event_id, "callback_refused", None)
    if len(body) > MAX_BODY_BYTES:
        # r0b2 B8: refused locally, never handed to post() -- so never "sent".
        store.record_attempt(conn, claim, status_code=413, now=now)
        return Delivery(claim.event_id, "body_too_large", 413)
    timestamp = int(now)
    headers = {
        "Content-Type": "application/json",
        "webhook-id": claim.event_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature_header(secrets, claim.event_id, timestamp, body),
        "X-MCP-Subscription-Id": sub["id"],
    }
    # r0b2 B6, THE PAUSE FENCE: re-read the binding immediately before the POST. A
    # pause or deactivate committed after claim_due releases the claim unsent (held
    # for retry; a deactivated binding's event dies at the next claim). This narrows
    # the window to the POST itself: a request already handed to the network may
    # still complete -- nothing here claims zero sends after a pause is acknowledged.
    held = store.wake_hold_reason(conn, claim.subscription_id)
    if held:
        parked = store.park_claim(conn, claim, until=now, reason=held)
        if parked and budget is not None and claim.attempts == 1:
            budget.release(claim.lease_token)
        return Delivery(claim.event_id, "held_paused" if held == "paused" else held, None)
    try:
        status = int(post(target, headers, body))
    except Exception:  # noqa: BLE001 - any transport failure is a failed attempt, retried
        status = 0
    store.record_attempt(conn, claim, status_code=status, now=now)
    return Delivery(claim.event_id, "sent", status)
