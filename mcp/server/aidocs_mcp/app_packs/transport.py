"""ExternalAppTransport + the tool-call request assertion -- RFC 0003 v4.6.1 §5.4, §12, §13 (build plan S13).

Wire: companion rev 8 §7 (tool-call assertion encodings), Appendix A.6
(byte-exact signing input), §1.6 (canonical origins; loopback ``http`` only in
a TEST-ONLY harness).

**Destination.** A call goes to exactly one place: the AIDOCS application
binding's PINNED ``application_origin`` (operator binding, §5.4) joined with
the pinned bundle's contract-declared ``relative_endpoint`` (§6.4, §12.1).
:meth:`ExternalAppTransport.call` has no URL, host, header or proxy parameter,
so no model, business argument or backend answer can nominate a destination.
Before ANY socket (DNS included) the transport:

1. checks the pinned origin is canonical under the wire §1.6 profile, and --
   outside the test-only harness -- is not a loopback host (build plan R3:
   "reaching localhost is not in scope");
2. re-checks the endpoint is a bare relative path and that origin + endpoint
   re-parses to exactly that origin (exact (scheme, host, port) equality);
3. routes the URL through the governed egress chokepoint
   (:func:`~aidocs_mcp.governed_egress.assert_egress_allowed`, purpose
   :data:`EGRESS_PURPOSE`) with the pinned host as the ONLY allowlist entry.

Then it resolves the name ONCE and refuses a resolution that yields a
loopback, unspecified, link-local, multicast or reserved address (§5.4 "DNS /
address resolution must satisfy binding policy"; private RFC 1918 addresses
stay allowed, §5.4), and connects to exactly that address with TLS verified
against the pinned hostname (no re-resolution between check and connect).
``http.client`` is used directly: it never reads proxy environment variables
and never follows a redirect. A 3xx is refused, never followed (§5.4).

**Request.** ``POST <relative_endpoint>``; the body is exactly the RFC 8785
bytes of the normalized arguments, so ``body_sha256`` covers the bytes on the
wire (§12.1). Headers are a fixed set; ``Authorization: Bearer <JWS>`` carries
the assertion signed by the org request signer (S3) as ``aidocs-request+jwt``
with lifetime 60 s (§12.3). Absent host facts are omitted, never faked.

**Bounds.** Connect 2.0 s, read-idle 5.0 s, total 15.0 s; request body 256 KiB
(§12.5). Configuration may tighten, never loosen. The response floor is
:mod:`.output_guard` (§12.4, §13).

**Delivery truth.** A failure before the connection is up is
``app_backend_unavailable`` with ``possibly_delivered=False``. Once bytes may
have left, a network failure, timeout or 5xx is ``app_backend_unavailable``
for a read and ``app_outcome_unknown`` for a mutation (§12.5: never blindly
retried; the §11.4 resolver owns what happens next), always with
``possibly_delivered=True``. Nothing is retried here.

**Recovery (S14).** ``recovery=True`` is the ONE recovery-aware form, used only
by the S14 resolver for the RFC §11.2 step 5 resend after the step 3 re-check:
it sets the wire §7 / §7.1 claim ``recovery: "checked_resend"`` (the only v1
value; there is no parameter for the value), is refused on a non-mutation, and
admits a ``DISABLED`` binding (the post-active state until S19) ONLY with a
recovery-marked entitlement (wire §6.5); an ``ACTIVE`` binding still takes only
an ordinary entitlement. Every ordinary call (``recovery=False``) keeps the S13
law: no claim, ACTIVE bindings only, never a marked entitlement.

**File-taking modes (S16, RFC §14.3, wire §7).** A mode whose bundle declares
``file_params`` takes ``host_file``: an :class:`~.host_file_ingress.IngestedFile`
already fetched under the §14.1 floor. The file parameter in the arguments is
replaced by the capability-free relay descriptor
(:func:`~.host_file_ingress.relay_arguments`), and the request is
``multipart/form-data`` exactly as the CRM's ``AgentApi.HandleUpload`` parses
it: an ``arguments`` form value (the RFC 8785 bytes ``body_sha256`` covers --
the CRM hashes ``JCS(arguments)``) and a ``file`` file part (the spooled bytes,
a fixed ``filename`` and the SNIFFED content type). The assertion adds
``content_sha256`` / ``content_length`` / ``content_type`` binding those bytes.
File bytes are a separate budget from the §12.5 JSON cap, which still applies
to the arguments; the relay runs under the §14.5 upload timeouts (idle 10 s,
total 60 s -- the CRM's upload window). A file-less call to a file mode, or a
file on a mode without ``file_params``, is refused before egress. This module
never closes ``host_file``: its owner destroys the spool (``try/finally``).

Not here (explicit inputs until those slices land): binding resolution from a
connector (S4b) -- a resolved :class:`BindingRecord` is an argument; the
verified entitlement (S9) is an argument; the pending record and mutation
identity are S14's (``pending.py``).
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import itertools
import re
import secrets
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..governed_egress import EgressRefused, assert_egress_allowed
from . import jws
from .binding_store import BindingRecord, BindingState
from .entitlements import Entitlement
from .host_file_ingress import MAX_FILE_BYTES, SUPPORTED_CONTENT_TYPES, IngestedFile, relay_arguments
from .jcs import JcsError, canonicalize, jcs_sha256_hex
from .origin import CanonicalOrigin, OriginPolicy, OriginRefused, parse_origin, parse_url, same_origin
from .output_guard import (
    GuardedOutput,
    ResponseLimits,
    ResponseRefused,
    check_content_type,
    check_headers,
    declared_error_codes_from_bundle,
    guard_output,
    parse_bounded_json,
    read_bounded_body,
    schema_accepts,
)
from .scope import AppScope
from .signer import SignerError, SignerUnavailable

__all__ = [
    "APP_BACKEND_INVALID_RESPONSE",
    "APP_BACKEND_UNAVAILABLE",
    "APP_CAPABILITY_REQUIRED",
    "APP_CONTRACT_MISMATCH",
    "APP_CONVERSATION_CONTEXT_UNAVAILABLE",
    "APP_INTERNAL",
    "APP_OUTCOME_UNKNOWN",
    "APP_PACK_UNAVAILABLE",
    "APP_VALIDATION_FAILED",
    "ASSERTION_LIFETIME_S",
    "EGRESS_PURPOSE",
    "RECOVERY_CHECKED_RESEND",
    "ExternalAppTransport",
    "TransportConfig",
    "TransportRefused",
    "TransportResult",
    "build_tool_assertion_claims",
    "default_connector",
    "default_resolver",
]

# §16.3 platform codes (AIDOCS-produced only, §16.4).
APP_BACKEND_INVALID_RESPONSE = "app_backend_invalid_response"
APP_BACKEND_UNAVAILABLE = "app_backend_unavailable"
APP_CAPABILITY_REQUIRED = "app_capability_required"
APP_CONTRACT_MISMATCH = "app_contract_mismatch"
APP_CONVERSATION_CONTEXT_UNAVAILABLE = "app_conversation_context_unavailable"
APP_INTERNAL = "app_internal"
APP_OUTCOME_UNKNOWN = "app_outcome_unknown"
APP_PACK_UNAVAILABLE = "app_pack_unavailable"
APP_VALIDATION_FAILED = "app_validation_failed"

EGRESS_PURPOSE = "app_pack_transport"
RECOVERY_CHECKED_RESEND = "checked_resend"  # wire §7 / §7.1: the only v1 value
# Binding states a checked resend may target (wire §7.1, §6.5; RFC §5.7).
_RECOVERY_STATES = frozenset({BindingState.ACTIVE, BindingState.DISABLED})
ASSERTION_LIFETIME_S = 60  # §12.3 ceiling; exp - iat
KIB = 1024
# §12.5 platform hard ceilings.
CONNECT_TIMEOUT_S = 2.0
READ_IDLE_TIMEOUT_S = 5.0
TOTAL_TIMEOUT_S = 15.0
REQUEST_BODY_MAX_BYTES = 256 * KIB
# §14.5 application relay ceilings for a file-taking call (the CRM's upload window).
UPLOAD_IDLE_TIMEOUT_S = 10.0
UPLOAD_TOTAL_TIMEOUT_S = 60.0
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_UPLOAD_FILENAMES = {  # fixed: a host-supplied name never reaches a header
    "image/jpeg": "upload.jpg",
    "image/png": "upload.png",
    "image/webp": "upload.webp",
    "application/pdf": "upload.pdf",
}

_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")  # wire §7
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@-]+")  # §6.4, as the S2 validator
_LOOPBACK_NAMES = frozenset({"localhost"})


class TransportRefused(Exception):
    """A call refused or failed. ``code`` is a §16.3 platform code; ``reason`` a
    fixed local diagnostic that never echoes backend bytes, tokens or keys.

    ``egress_attempted``: a socket (DNS included) was used for this call.
    ``possibly_delivered``: request bytes may have reached the application.
    """

    def __init__(
        self, code: str, reason: str, *, egress_attempted: bool = False, possibly_delivered: bool = False
    ) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.egress_attempted = egress_attempted
        self.possibly_delivered = possibly_delivered


def _pre(code: str, reason: str) -> TransportRefused:
    return TransportRefused(code, reason, egress_attempted=False, possibly_delivered=False)


@dataclass(frozen=True)
class TransportConfig:
    """§12.5 bounds for one transport. Values may only TIGHTEN the ceilings.

    ``test_only_allow_loopback`` is the wire §1.6 TEST-ONLY harness switch: it
    admits ``http`` on loopback and loopback addresses. It is never shipped,
    never the default and never satisfies M1 acceptance (build plan R3). Use
    :meth:`test_only_loopback` so every use is visibly marked.
    """

    connect_timeout_s: float = CONNECT_TIMEOUT_S
    read_idle_timeout_s: float = READ_IDLE_TIMEOUT_S
    total_timeout_s: float = TOTAL_TIMEOUT_S
    request_body_max_bytes: int = REQUEST_BODY_MAX_BYTES
    response_limits: ResponseLimits = field(default_factory=ResponseLimits)
    test_only_allow_loopback: bool = False
    upload_idle_timeout_s: float = UPLOAD_IDLE_TIMEOUT_S
    upload_total_timeout_s: float = UPLOAD_TOTAL_TIMEOUT_S

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("connect_timeout_s", CONNECT_TIMEOUT_S),
            ("read_idle_timeout_s", READ_IDLE_TIMEOUT_S),
            ("total_timeout_s", TOTAL_TIMEOUT_S),
            ("upload_idle_timeout_s", UPLOAD_IDLE_TIMEOUT_S),
            ("upload_total_timeout_s", UPLOAD_TOTAL_TIMEOUT_S),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= ceiling:
                raise ValueError(f"{name} must be in (0, {ceiling}] (§12.5: limits may only tighten)")
        body = self.request_body_max_bytes
        if type(body) is not int or not 1 <= body <= REQUEST_BODY_MAX_BYTES:
            raise ValueError(f"request_body_max_bytes must be 1..{REQUEST_BODY_MAX_BYTES}")
        if not isinstance(self.response_limits, ResponseLimits):
            raise ValueError("response_limits must be a ResponseLimits")
        if type(self.test_only_allow_loopback) is not bool:
            raise ValueError("test_only_allow_loopback must be a bool")

    @classmethod
    def test_only_loopback(cls, **overrides: Any) -> "TransportConfig":
        """TEST-ONLY (wire §1.6 harness): loopback ``http``. Never a product path."""
        return cls(test_only_allow_loopback=True, **overrides)

    @property
    def origin_policy(self) -> OriginPolicy:
        return OriginPolicy.test_only_loopback() if self.test_only_allow_loopback else OriginPolicy()


@dataclass(frozen=True)
class TransportResult:
    """A response that passed the §13 floor, plus audit-safe digests (§16.2)."""

    output: GuardedOutput
    status: int
    jti: str
    body_sha256: str
    response_sha256: str
    content_sha256: Optional[str] = None  # a file-taking call: the relayed bytes (§14.3)


# -- the request assertion (§12.1, wire §7) ------------------------------------------------


def _text(value: Any) -> bool:
    return type(value) is str and value != ""


def _sorted_caps(values: Iterable[str]) -> list[str]:
    caps = list(values)
    if not all(_text(c) for c in caps) or len(set(caps)) != len(caps):
        raise _pre(APP_CAPABILITY_REQUIRED, "capabilities_malformed")
    return sorted(caps)  # wire §1.1: unique, ascending code-point order


def _pinned_mode(binding: BindingRecord, bundle: Any, tool: str, mode: str, *, recovery: bool = False) -> Any:
    """The mode spec, from the bundle this ACTIVE binding pins; fail closed.

    A checked recovery resend (``recovery=True``) may also target a DISABLED
    binding (wire §7.1); every other state is refused either way.
    """
    if not isinstance(binding, BindingRecord):
        raise _pre(APP_PACK_UNAVAILABLE, "binding_unresolved")
    allowed = _RECOVERY_STATES if recovery is True else frozenset({BindingState.ACTIVE})
    if binding.state not in allowed or binding.trust is None:
        raise _pre(APP_PACK_UNAVAILABLE, "binding_not_active")
    if getattr(bundle, "pack_id", None) != binding.pack_id:
        raise _pre(APP_PACK_UNAVAILABLE, "bundle_not_pinned")
    if getattr(bundle, "contract_digest", None) != binding.contract_digest:
        raise _pre(APP_CONTRACT_MISMATCH, "contract_digest_mismatch")
    if tool not in binding.tool_names:
        raise _pre(APP_PACK_UNAVAILABLE, "tool_not_bound")
    tspec = bundle.tools.get(tool) if isinstance(getattr(bundle, "tools", None), Mapping) else None
    mspec = tspec.modes.get(mode) if tspec is not None else None
    if mspec is None:
        raise _pre(APP_PACK_UNAVAILABLE, "mode_unknown")
    if mspec.platform_handler is not None:
        # wire §10.2: answered by the AIDOCS resolver; its endpoint is never called.
        raise _pre(APP_PACK_UNAVAILABLE, "platform_handled_mode")
    return mspec


def build_tool_assertion_claims(
    *,
    binding: BindingRecord,
    bundle: Any,
    scope: AppScope,
    entitlement: Entitlement,
    tool: str,
    mode: str,
    body: Mapping[str, Any],
    call_id: str,
    jti: str,
    now: int,
    capabilities_used: Iterable[str],
    idempotency_key: Optional[str] = None,
    session_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    host_kind: Optional[str] = None,
    host_conversation_ref: Optional[str] = None,
    recovery: bool = False,
    content_sha256: Optional[str] = None,
    content_length: Optional[int] = None,
    content_type: Optional[str] = None,
) -> dict:
    """The §12.1 / wire §7 claim set for one tool call. Pre-egress only.

    A mode declaring ``file_params`` REQUIRES the three §14.3 content claims
    (the CRM refuses an upload assertion without them); every other mode
    refuses them.

    Every entitlement claim comes from the verified entitlement; every binding
    claim from the resolved binding; ``iss`` is the binding's paired
    ``aidocs_issuer``. Absent optional facts are omitted, never faked.
    ``recovery=True`` builds the S14 checked resend (wire §7.1).
    """
    if type(recovery) is not bool:
        raise _pre(APP_INTERNAL, "recovery_flag_malformed")  # the value is never caller-chosen
    mspec = _pinned_mode(binding, bundle, tool, mode, recovery=recovery)
    if not isinstance(scope, AppScope) or (scope.tenant_id, scope.toolspace_id) != (
        binding.tenant_id,
        binding.toolspace_id,
    ):
        raise _pre(APP_PACK_UNAVAILABLE, "scope_not_this_binding")
    if not isinstance(entitlement, Entitlement):
        raise _pre(APP_PACK_UNAVAILABLE, "entitlement_not_ordinary")
    # wire §6.5: a marked entitlement only for a checked resend to a NON-active
    # binding; an active binding (and every ordinary call) takes an unmarked one.
    marked_needed = recovery and binding.state is not BindingState.ACTIVE
    if entitlement.recovery is not marked_needed:
        raise _pre(APP_PACK_UNAVAILABLE, "entitlement_not_ordinary" if not recovery else "entitlement_marker_mismatch")
    if (
        entitlement.tenant_id,
        entitlement.binding_id,
        entitlement.sub,
        entitlement.pack_id,
        entitlement.contract_digest,
    ) != (binding.tenant_id, binding.binding_id, scope.sub, binding.pack_id, binding.contract_digest):
        raise _pre(APP_PACK_UNAVAILABLE, "entitlement_not_this_seat")
    if not all(_text(v) for v in (call_id, jti)) or type(now) is not int:
        raise _pre(APP_INTERNAL, "call_facts_malformed")

    if not isinstance(body, Mapping):
        raise _pre(APP_VALIDATION_FAILED, "body_not_object")
    granted = _sorted_caps(entitlement.capabilities_granted)
    used = _sorted_caps(capabilities_used)
    if not set(used) <= set(granted) or not set(_branch_required(mspec, body)) <= set(used):
        raise _pre(APP_CAPABILITY_REQUIRED, "capabilities_not_covered")

    mutation = mspec.idempotency == "required"
    if mutation:
        if type(idempotency_key) is not str or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise _pre(APP_VALIDATION_FAILED, "idempotency_key_invalid")
    elif idempotency_key is not None:
        raise _pre(APP_VALIDATION_FAILED, "idempotency_key_on_read")
    if recovery and not mutation:
        raise _pre(APP_VALIDATION_FAILED, "recovery_on_non_mutation")  # wire §7.1, A.12

    if mspec.requires_conversation_context and not _text(host_conversation_ref):
        raise _pre(APP_CONVERSATION_CONTEXT_UNAVAILABLE, "host_conversation_ref_absent")  # §12.1

    try:
        body_sha256 = hashlib.sha256(canonicalize(dict(body))).hexdigest()
    except (JcsError, TypeError, ValueError):
        raise _pre(APP_VALIDATION_FAILED, "body_not_canonicalizable") from None

    claims: dict[str, Any] = {
        "iss": binding.trust.aidocs_issuer,
        "sub": scope.sub,
        "aud": binding.audience,
        "iat": now,
        "exp": now + ASSERTION_LIFETIME_S,
        "jti": jti,
        "tenant_id": binding.tenant_id,
        "call_id": call_id,
        "binding_id": binding.binding_id,
        "binding_version": binding.binding_version,
        "pack_id": binding.pack_id,
        "contract_digest": binding.contract_digest,
        "app_principal_id": entitlement.app_principal_id,
        "grants_version": entitlement.grants_version,
        "grants_digest": entitlement.grants_digest,
        "capabilities_granted": granted,
        "capabilities_used": used,
        "tool": tool,
        "mode": mode,
        "body_sha256": body_sha256,
    }
    optional = {
        "idempotency_key": idempotency_key if mutation else None,
        "session_id": session_id,
        "actor_id": actor_id,
        "host_kind": host_kind,
        "host_conversation_ref": host_conversation_ref,
    }
    for name, value in optional.items():
        if value is None:
            continue
        if not _text(value):
            raise _pre(APP_VALIDATION_FAILED, f"{name}_malformed")
        claims[name] = value
    content = (content_sha256, content_length, content_type)
    if tuple(getattr(mspec, "file_params", ()) or ()):
        if not (
            type(content_sha256) is str
            and _SHA256_HEX.fullmatch(content_sha256)
            and type(content_length) is int
            and 1 <= content_length <= MAX_FILE_BYTES
            and content_type in SUPPORTED_CONTENT_TYPES
        ):
            raise _pre(APP_VALIDATION_FAILED, "content_claims_invalid")  # §14.3
        claims["content_sha256"] = content_sha256
        claims["content_length"] = content_length
        claims["content_type"] = content_type
    elif content != (None, None, None):
        raise _pre(APP_VALIDATION_FAILED, "content_claims_on_non_file_mode")
    if recovery:
        claims["recovery"] = RECOVERY_CHECKED_RESEND
    return claims


def _branch_required(mspec: Any, body: Mapping[str, Any]) -> tuple:
    """The required capabilities of the ONE branch this body selects (§6.3.2).

    A union mode selects its branch by the const discriminator value in the
    body; a non-union mode has exactly one branch. No match fails closed.
    """
    branches = tuple(getattr(mspec, "branches", ()) or ())
    if mspec.discriminator is None:
        if len(branches) != 1:
            raise _pre(APP_VALIDATION_FAILED, "branch_unresolved")
        return tuple(branches[0].required_capabilities)
    if mspec.discriminator not in body:
        raise _pre(APP_VALIDATION_FAILED, "branch_unresolved")
    try:
        wanted = canonicalize(body[mspec.discriminator])
        hits = [b for b in branches if canonicalize(b.discriminator_value) == wanted]
    except (JcsError, TypeError, ValueError):
        hits = []
    if len(hits) != 1:
        raise _pre(APP_VALIDATION_FAILED, "branch_unresolved")
    return tuple(hits[0].required_capabilities)


# -- destination checks (§5.4, §6.4, wire §1.6) ---------------------------------------------


def _is_relative_path(value: Any) -> bool:
    if type(value) is not str or not value.startswith("/") or len(value) < 2:
        return False
    return all(s not in (".", "..") and _PATH_SEGMENT.fullmatch(s) for s in value[1:].split("/"))


def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _address_allowed(address: str, *, allow_loopback: bool) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return allow_loopback
    return not (ip.is_unspecified or ip.is_link_local or ip.is_multicast or ip.is_reserved)


def default_resolver(host: str, port: int) -> list[str]:
    """Resolve once; every returned address is checked before any connect."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ONE pre-checked address; TLS verified against the pinned hostname."""

    def __init__(self, host: str, port: int, *, address: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = address
        self._pinned_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._pinned_context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """TEST-ONLY (wire §1.6 harness): plain HTTP to one pre-checked loopback address."""

    def __init__(self, host: str, port: int, *, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)


def default_connector(origin: CanonicalOrigin, address: str, timeout: float) -> http.client.HTTPConnection:
    """An UNCONNECTED connection to ``address`` for the pinned ``origin``."""
    if origin.scheme == "https":
        context = ssl.create_default_context()  # CERT_REQUIRED + hostname check (§5.4)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return _PinnedHTTPSConnection(origin.host, origin.port, address=address, timeout=timeout, context=context)
    if origin.scheme == "http":
        return _PinnedHTTPConnection(origin.host, origin.port, address=address, timeout=timeout)
    raise ValueError("scheme outside the origin profile")


# -- the file relay body (RFC §14.3, wire §7) --------------------------------------------


def _multipart(arguments_json: bytes, host_file: IngestedFile) -> tuple[bytes, str, Iterable[bytes], int]:
    """``multipart/form-data`` as the CRM reads it (``form["arguments"]``, ``form.Files["file"]``).

    Returns ``(head, content_type, stream, stream_length)``: ``head`` is the
    ``arguments`` part (its value is exactly ``arguments_json``, the JCS bytes
    ``body_sha256`` covers) plus the ``file`` part headers; ``stream`` yields the
    spooled file bytes then the closing delimiter. The boundary is 128 random
    bits (RFC 2046 §5.1.1): unguessable, so file bytes cannot forge a part.
    The filename is fixed per sniffed type -- no host-supplied name reaches a
    header.
    """
    boundary = "aidocs-" + secrets.token_hex(16)
    ctype = host_file.content_type
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="arguments"\r\n'
        "Content-Type: application/json; charset=utf-8\r\n"
        "\r\n"
    ).encode("ascii") + arguments_json + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{_UPLOAD_FILENAMES[ctype]}"\r\n'
        f"Content-Type: {ctype}\r\n"
        "\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    stream = itertools.chain(host_file.iter_chunks(), (tail,))
    return head, f"multipart/form-data; boundary={boundary}", stream, host_file.length + len(tail)


# -- the transport ---------------------------------------------------------------------


class _Deadline(Exception):
    pass


class ExternalAppTransport:
    """Bounded POST to the binding's pinned origin + the bundle's relative endpoint."""

    def __init__(
        self,
        signer: Any,
        *,
        config: Optional[TransportConfig] = None,
        clock: Optional[Callable[[], int]] = None,
        jti_factory: Optional[Callable[[], str]] = None,
        resolver: Optional[Callable[[str, int], Sequence[str]]] = None,
        connector: Optional[Callable[[CanonicalOrigin, str, float], Any]] = None,
        egress_audit: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._signer = signer
        self._config = config if config is not None else TransportConfig()
        if not isinstance(self._config, TransportConfig):
            raise ValueError("config must be a TransportConfig")
        self._clock = clock or (lambda: int(time.time()))
        self._jti = jti_factory or (lambda: "jti_" + secrets.token_urlsafe(24))
        self._resolver = resolver or default_resolver
        self._connector = connector or default_connector
        self._egress_audit = egress_audit

    def __repr__(self) -> str:
        return f"<ExternalAppTransport test_only_loopback={self._config.test_only_allow_loopback}>"

    # -- pre-egress ------------------------------------------------------------------

    def _destination(self, binding: BindingRecord, endpoint: Any) -> tuple[CanonicalOrigin, str]:
        policy = self._config.origin_policy
        try:
            origin = parse_origin(binding.application_origin, policy=policy)
        except OriginRefused:
            raise _pre(APP_PACK_UNAVAILABLE, "pinned_origin_not_canonical") from None
        if binding.application_origin not in (origin.serialized, origin.serialized + "/"):  # §1.6 rule 7
            raise _pre(APP_PACK_UNAVAILABLE, "pinned_origin_not_canonical")
        if not self._config.test_only_allow_loopback and _is_loopback_host(origin.host):
            raise _pre(APP_PACK_UNAVAILABLE, "pinned_origin_loopback")
        if not _is_relative_path(endpoint):
            raise _pre(APP_PACK_UNAVAILABLE, "endpoint_not_relative")
        url = origin.serialized + endpoint
        try:
            reparsed, path = parse_url(url, policy=policy)
        except OriginRefused:
            raise _pre(APP_PACK_UNAVAILABLE, "endpoint_not_relative") from None
        if not same_origin(reparsed, origin) or path != endpoint:
            raise _pre(APP_PACK_UNAVAILABLE, "endpoint_leaves_origin")
        return origin, url

    def _address(self, origin: CanonicalOrigin) -> str:
        try:
            addresses = list(self._resolver(origin.host, origin.port))
        except Exception:  # noqa: BLE001 -- resolution failure fails closed, nothing sent
            raise TransportRefused(APP_BACKEND_UNAVAILABLE, "resolution_failed", egress_attempted=True) from None
        allow_loopback = self._config.test_only_allow_loopback
        if not addresses or not all(type(a) is str and _address_allowed(a, allow_loopback=allow_loopback) for a in addresses):
            raise TransportRefused(APP_PACK_UNAVAILABLE, "resolved_address_refused", egress_attempted=True)
        return addresses[0]

    # -- the call --------------------------------------------------------------------

    def call(
        self,
        *,
        binding: BindingRecord,
        bundle: Any,
        scope: AppScope,
        entitlement: Entitlement,
        tool: str,
        mode: str,
        arguments: Mapping[str, Any],
        call_id: str,
        capabilities_used: Iterable[str],
        idempotency_key: Optional[str] = None,
        session_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        host_kind: Optional[str] = None,
        host_conversation_ref: Optional[str] = None,
        recovery: bool = False,
        host_file: Optional[IngestedFile] = None,
    ) -> TransportResult:
        if type(recovery) is not bool:
            raise _pre(APP_INTERNAL, "recovery_flag_malformed")
        mspec = _pinned_mode(binding, bundle, tool, mode, recovery=recovery)
        origin, url = self._destination(binding, mspec.relative_endpoint)
        file_params = tuple(getattr(mspec, "file_params", ()) or ())
        if file_params:
            # §14.3: only bytes that went through HostFileIngress, still spooled.
            if not isinstance(host_file, IngestedFile) or host_file.closed:
                raise _pre(APP_VALIDATION_FAILED, "host_file_missing")
            if not isinstance(arguments, Mapping):
                raise _pre(APP_VALIDATION_FAILED, "input_schema_mismatch")
            try:
                arguments = relay_arguments(arguments, file_params, host_file)
            except (TypeError, ValueError):
                raise _pre(APP_VALIDATION_FAILED, "host_file_param_missing") from None
        elif host_file is not None:
            raise _pre(APP_VALIDATION_FAILED, "host_file_not_declared")
        if not isinstance(arguments, Mapping) or not schema_accepts(mspec.input_schema, dict(arguments)):
            raise _pre(APP_VALIDATION_FAILED, "input_schema_mismatch")  # §6.5: validated before transport
        try:
            body = canonicalize(dict(arguments))
        except (JcsError, TypeError, ValueError):
            raise _pre(APP_VALIDATION_FAILED, "body_not_canonicalizable") from None
        if len(body) > self._config.request_body_max_bytes:
            raise _pre(APP_VALIDATION_FAILED, "request_body_over_cap")  # the JSON only: file bytes are §14.5's
        content: dict[str, Any] = {}
        if file_params:
            content = {
                "content_sha256": host_file.sha256,
                "content_length": host_file.length,
                "content_type": host_file.content_type,
            }
        used = frozenset(capabilities_used)
        jti = self._jti()
        claims = build_tool_assertion_claims(
            binding=binding, bundle=bundle, scope=scope, entitlement=entitlement, tool=tool, mode=mode,
            body=arguments, call_id=call_id, jti=jti, now=self._clock(), capabilities_used=used,
            idempotency_key=idempotency_key, session_id=session_id, actor_id=actor_id,
            host_kind=host_kind, host_conversation_ref=host_conversation_ref, recovery=recovery, **content,
        )
        try:
            token = self._signer.sign(binding.tenant_id, jws.TYP_REQUEST, claims)
        except (SignerUnavailable, SignerError):
            raise _pre(APP_INTERNAL, "request_signer_unavailable") from None
        try:
            assert_egress_allowed(url, purpose=EGRESS_PURPOSE, allow_hosts=(origin.host,), audit=self._egress_audit)
        except EgressRefused:
            raise _pre(APP_PACK_UNAVAILABLE, "egress_refused") from None
        address = self._address(origin)
        declared = declared_error_codes_from_bundle(bundle)
        mutation = mspec.idempotency == "required"
        if file_params:
            head, content_type, stream, stream_length = _multipart(body, host_file)
            status, raw_body = self._exchange(
                origin, address, mspec.relative_endpoint, head, token, mutation=mutation,
                content_type=content_type, stream=stream, stream_length=stream_length, upload=True,
            )
        else:
            status, raw_body = self._exchange(origin, address, mspec.relative_endpoint, body, token, mutation=mutation)
        result = self._floor(status, raw_body, mspec, declared, used, jti=jti, body_sha256=claims["body_sha256"])
        if file_params:
            result = TransportResult(
                output=result.output, status=result.status, jti=result.jti, body_sha256=result.body_sha256,
                response_sha256=result.response_sha256, content_sha256=host_file.sha256,
            )
        return result

    def _exchange(
        self,
        origin: CanonicalOrigin,
        address: str,
        endpoint: str,
        body: bytes,
        token: str,
        *,
        mutation: bool,
        content_type: str = "application/json",
        stream: Optional[Iterable[bytes]] = None,
        stream_length: int = 0,
        upload: bool = False,
    ) -> tuple[int, Any]:
        """POST ``body`` then (a file relay) ``stream``; ``Content-Length`` is exact.

        ``upload=True`` runs under the §14.5 relay timeouts instead of §12.5's.
        """
        cfg = self._config
        idle_s = cfg.upload_idle_timeout_s if upload else cfg.read_idle_timeout_s
        total_s = cfg.upload_total_timeout_s if upload else cfg.total_timeout_s
        start = time.monotonic()
        deadline = start + total_s
        try:
            conn = self._connector(origin, address, cfg.connect_timeout_s)
            conn.connect()
        except Exception:  # noqa: BLE001 -- nothing was sent
            raise TransportRefused(APP_BACKEND_UNAVAILABLE, "connect_failed", egress_attempted=True) from None
        sock = conn.sock

        def arm() -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline
            try:
                sock.settimeout(min(idle_s, remaining))
            except OSError:
                pass

        def lost(reason: str) -> TransportRefused:
            code = APP_OUTCOME_UNKNOWN if mutation else APP_BACKEND_UNAVAILABLE
            return TransportRefused(code, reason, egress_attempted=True, possibly_delivered=True)

        try:
            try:
                arm()
                conn.putrequest("POST", endpoint, skip_accept_encoding=True)
                for name, value in (
                    ("Content-Type", content_type),
                    ("Accept", "application/json"),
                    ("Accept-Encoding", "gzip"),
                    ("Authorization", "Bearer " + token),
                    ("Content-Length", str(len(body) + stream_length)),
                    ("Connection", "close"),
                ):
                    conn.putheader(name, value)
                conn.endheaders(body)
                if stream is not None:
                    sent = 0
                    for chunk in stream:
                        arm()
                        conn.send(chunk)
                        sent += len(chunk)
                    if sent != stream_length:
                        raise lost("upload_length_mismatch")
                arm()
                resp = conn.getresponse()
            except _Deadline:
                raise lost("total_timeout") from None
            except (OSError, http.client.HTTPException, ValueError):  # ValueError: a spool gone mid-relay
                raise lost("exchange_failed") from None
            status = resp.status
            if 300 <= status < 400:
                raise TransportRefused(
                    APP_BACKEND_INVALID_RESPONSE, "redirect_refused", egress_attempted=True, possibly_delivered=True
                )
            if status >= 500:
                raise lost("backend_status_5xx")
            if status != 200 and not 400 <= status < 500:
                raise TransportRefused(
                    APP_BACKEND_INVALID_RESPONSE, "status_refused", egress_attempted=True, possibly_delivered=True
                )
            limits = cfg.response_limits
            try:
                check_headers(resp.getheaders(), limits)
                check_content_type(resp.getheader("Content-Type"))
                raw = read_bounded_body(
                    resp, content_encoding=resp.getheader("Content-Encoding"), limits=limits, before_read=arm
                )
            except ResponseRefused as exc:
                raise TransportRefused(exc.code, exc.reason, egress_attempted=True, possibly_delivered=True) from None
            except _Deadline:
                raise lost("total_timeout") from None
            except (OSError, http.client.HTTPException):
                raise lost("read_failed") from None
            return status, raw
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _floor(
        self, status: int, raw: bytes, mspec: Any, declared: frozenset, used: frozenset, *, jti: str, body_sha256: str
    ) -> TransportResult:
        limits = self._config.response_limits
        try:
            parsed = parse_bounded_json(raw, limits)
            output = guard_output(parsed, mode=mspec, declared_codes=declared, capabilities_used=used)
            if status != 200 and output.ok:
                raise ResponseRefused("status_body_mismatch")
            response_sha256 = jcs_sha256_hex(parsed)
        except ResponseRefused as exc:
            raise TransportRefused(exc.code, exc.reason, egress_attempted=True, possibly_delivered=True) from None
        except (JcsError, TypeError, ValueError):
            raise TransportRefused(
                APP_BACKEND_INVALID_RESPONSE, "response_not_canonicalizable", egress_attempted=True, possibly_delivered=True
            ) from None
        return TransportResult(output=output, status=status, jti=jti, body_sha256=body_sha256, response_sha256=response_sha256)
