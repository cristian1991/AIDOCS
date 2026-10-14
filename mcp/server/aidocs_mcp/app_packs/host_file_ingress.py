"""HostFileIngress -- the host file capability fetch, RFC 0003 v4.6.1 §14 (build plan S16).

A file-taking application tool (v1: ``crm_upload``, §6.9.3) receives from the
AI host a file parameter in the HOST's file contract (:data:`HOST_FILE_PARAM_SCHEMA`:
``{download_url, file_id, mime_type?, file_name?}``; legacy ``name`` is still
read). The host is shown that contract, never the application's relay schema
(:mod:`.mcp_catalog`), because the application never receives it. The
``download_url`` is a short-lived SIGNED capability. This module turns it into
bytes under the §14.1 floor, then hands the relay (:mod:`.transport`) an
:class:`IngestedFile` whose stable facts (§14.2) -- ``sha256``, ``length``,
the SNIFFED ``content_type`` and the provider ``file_id`` -- are what the
request assertion binds (§14.3).

The §14.1 fetch policy, as built:

* **HTTPS only, port 443 only.** Any other network scheme is
  ``host_file_provider_not_allowed``; an opaque host reference
  (``chat_upload://...``, ``/mnt/data/...``), userinfo or a malformed URL is
  ``host_file_unresolvable``.
* **Provider allowlist.** The host must equal, or be a subdomain of, an entry
  of ``allowed_hosts`` (empty = refuse everything). The check runs through the
  governed egress chokepoint (:func:`~aidocs_mcp.governed_egress.assert_egress_allowed`,
  purpose :data:`EGRESS_PURPOSE`), which audits ONLY the host.
* **Address policy.** An IP-literal host is vetted first. A name is resolved
  ONCE per hop and EVERY returned address must be globally routable: loopback,
  RFC 1918 / ULA private, link-local, CGNAT, multicast, reserved, unspecified
  and the cloud-metadata addresses are refused (``host_file_address_refused``);
  IPv4 embedded in IPv4-mapped / 6to4 / NAT64 addresses is unwrapped and
  vetted, Teredo is refused. The connection is then made to exactly the vetted
  address (TLS verified against the hostname), so there is no DNS-rebinding
  window between check and connect.
* **Redirects: re-vetted, at most 3.** Chosen over "refuse all" because signed
  host-file URLs commonly bounce once to a CDN; §14.1 explicitly allows a
  bounded count with every target revalidated. Every hop repeats the scheme,
  port, allowlist, resolution and address checks. A hop off policy is
  ``host_file_redirect_refused``; a hop to a refused ADDRESS is
  ``host_file_address_refused`` (distinct semantics stay distinct, §16.3).
* **GET only, no caller cookies / Authorization**, ``Accept-Encoding:
  identity`` (a ``Content-Encoding`` is refused: the hashed bytes are the
  relayed bytes), at most 64 headers / 16 KiB.
* **Hard streaming cap** (§14.5: 25 MiB, a pack may only tighten). The body is
  read in 64 KiB chunks and refused one chunk past the cap. ``Content-Length``
  is advisory: a declared length over the cap refuses before reading; a body
  longer or shorter than its declared length is refused; a lying small length
  never admits more than the cap.
* **Timeouts** (§14.5): connect 2 s, read-idle 10 s, total 60 s (tighten only).

Content policy (§14.2, and the CRM's own ``Upload.Sniff``): the type is
SNIFFED from the magic bytes -- JPEG, PNG, WebP or PDF only. Neither the
server's ``Content-Type`` nor the host-declared ``mime_type`` is trusted; a
host-declared type that the bytes contradict is refused. Generic declarations
(absent, empty, ``application/octet-stream``) and the ``image/jpg`` alias are
not contradictions. An unsupported, contradicted or empty file is
``app_validation_failed`` (no ``host_file_*`` code names content, §16.3).

Spool (§14.3): bytes go to memory up to a small threshold, then to a private
temp file. Whatever the outcome the spool is destroyed: on any fetch failure
before returning, and on :meth:`IngestedFile.close` (also a context manager).
There is no durable AIDOCS copy (§14.3, §18.8).

Redaction (§14.1 "signed URL redacted from judge/audit/error/result/
idempotency"): the URL is never logged (this module does not log), never
placed in an exception message or ``repr``, and never relayed: the
application receives :func:`relay_file_descriptor` -- a content-addressed
``aidocs-relay:sha256:<hex>`` token in the schema's ``download_url`` slot
(the CRM validates the field's presence but never dereferences it) -- so the
body AIDOCS signs, pins in the pending record and derives the idempotency key
from carries no capability.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import ipaddress
import logging
import os
import re
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from ..governed_egress import EgressRefused, assert_egress_allowed

_log = logging.getLogger(__name__)
_LOG_SAFE = re.compile(r"[^A-Za-z0-9.:\[\]+-]")


def _log_safe(value: Any, limit: int) -> str:
    """A host or scheme for the operator log: allowlisted characters, bounded."""
    return _LOG_SAFE.sub("_", str(value or ""))[:limit] or "-"

__all__ = [
    "APP_VALIDATION_FAILED",
    "CONNECT_TIMEOUT_S",
    "EGRESS_PURPOSE",
    "HOST_FILE_ADDRESS_REFUSED",
    "HOST_FILE_PARAM_SCHEMA",
    "HOST_FILE_PROVIDER_NOT_ALLOWED",
    "HOST_FILE_REDIRECT_REFUSED",
    "HOST_FILE_TOO_LARGE",
    "HOST_FILE_UNRESOLVABLE",
    "IDLE_TIMEOUT_S",
    "MAX_FILE_BYTES",
    "MAX_REDIRECTS",
    "READ_CHUNK_BYTES",
    "RELAY_URL_PREFIX",
    "SUPPORTED_CONTENT_TYPES",
    "TOTAL_TIMEOUT_S",
    "HostFileIngress",
    "HostFileRef",
    "HostFileRefused",
    "IngestedFile",
    "HOST_FILE_PROVENANCE_CHATGPT",
    "HOST_FILE_PROVENANCE_GENERIC",
    "HOST_FILE_PROVIDER_ALIASES",
    "address_allowed",
    "derive_host_file_provenance",
    "expand_host_file_hosts",
    "host_catalog_schema",
    "host_input_schema",
    "parse_host_file_param",
    "relay_arguments",
    "relay_file_descriptor",
    "sniff_content_type",
]

# §16.3 codes (AIDOCS-produced only, §16.4).
HOST_FILE_UNRESOLVABLE = "host_file_unresolvable"
HOST_FILE_PROVIDER_NOT_ALLOWED = "host_file_provider_not_allowed"
HOST_FILE_REDIRECT_REFUSED = "host_file_redirect_refused"
HOST_FILE_ADDRESS_REFUSED = "host_file_address_refused"
HOST_FILE_TOO_LARGE = "host_file_too_large"
APP_VALIDATION_FAILED = "app_validation_failed"

EGRESS_PURPOSE = "app_pack_host_file_ingress"
MIB = 1024 * 1024
# §14.5 platform ceilings (a pack / configuration may only tighten).
MAX_FILE_BYTES = 25 * MIB
CONNECT_TIMEOUT_S = 2.0
IDLE_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 60.0
MAX_REDIRECTS = 3
READ_CHUNK_BYTES = 64 * 1024
MEMORY_THRESHOLD_BYTES = 1 * MIB
HEADERS_MAX_COUNT = 64
HEADERS_MAX_AGGREGATE_BYTES = 16 * 1024
DOWNLOAD_URL_MAX_CHARS = 4096  # the bundle's file schema bound
RELAY_URL_PREFIX = "aidocs-relay:sha256:"
SUPPORTED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "application/pdf"})

_TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "application/x-pdf": "application/pdf"}
_UNDECLARED_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_NETWORK_SCHEMES = frozenset({"http", "ftp", "ftps", "ws", "wss", "gopher", "file", "sftp"})
_FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
FILE_NAME_MAX_CHARS = 255
MIME_TYPE_MAX_CHARS = 100

# The host's file-parameter contract (OpenAI Apps ``openai/fileParams``): the
# shape the host is SHOWN for a declared file parameter and injects at call
# time. download_url + file_id are required, mime_type + file_name optional,
# nothing else. Bounds mirror what parse_host_file_param accepts. The host
# only binds a file when the advertised schema matches this exactly.
HOST_FILE_PARAM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "download_url": {"type": "string", "maxLength": DOWNLOAD_URL_MAX_CHARS},
        "file_id": {"type": "string", "maxLength": 200},
        "mime_type": {"type": "string", "maxLength": MIME_TYPE_MAX_CHARS},
        "file_name": {"type": "string", "maxLength": FILE_NAME_MAX_CHARS},
    },
    "required": ["download_url", "file_id"],
    "additionalProperties": False,
}


def host_input_schema(schema: Any, file_params: Iterable[str]) -> dict:
    """``schema`` as the HOST speaks it: every definition of each declared file
    parameter (top level, and each ``oneOf`` / ``anyOf`` branch) replaced by
    :data:`HOST_FILE_PARAM_SCHEMA`, and each file parameter declared at the top
    level so the host finds it as a top-level argument. A file parameter that
    EVERY branch of a ``oneOf`` / ``anyOf`` requires is also named in the root
    ``required``: the host hydrates file params from the root (OpenAI Apps
    reference: root properties + root required), and the root requirement is
    redundant, so the accepted set is unchanged. The structure is never moved
    (a lifted branch would break ``#/oneOf/0/...`` pointers). A fresh copy;
    ``schema`` is untouched; no file params = an unchanged copy.

    ONE schema serves both sides of the host boundary: what tools/list SHOWS
    the host (:mod:`.mcp_catalog`) and what the RAW host arguments are admitted
    against before any fetch (runtime / write path). The application's own
    schema then judges the RELAYED arguments (transport, after
    :func:`relay_arguments`)."""
    out = json.loads(json.dumps(schema)) if isinstance(schema, Mapping) else {"type": "object"}
    params = list(file_params)
    if not params:
        return out
    for node in [out, *out.get("oneOf", []), *out.get("anyOf", [])]:
        props = node.get("properties") if isinstance(node, dict) else None
        if isinstance(props, dict):
            for p in params:
                if p in props:
                    props[p] = json.loads(json.dumps(HOST_FILE_PARAM_SCHEMA))
    top = out.setdefault("properties", {})
    for p in params:
        top[p] = json.loads(json.dumps(HOST_FILE_PARAM_SCHEMA))
    for key in ("oneOf", "anyOf"):
        branches = out.get(key)
        if not (isinstance(branches, list) and branches and all(isinstance(b, dict) for b in branches)):
            continue
        for p in params:
            if all(p in (b.get("required") or []) for b in branches):
                required = out.setdefault("required", [])
                if isinstance(required, list) and p not in required:
                    required.append(p)
    return out


def host_catalog_schema(schema: Any, file_params: Iterable[str]) -> dict:
    """What tools/list ADVERTISES: :func:`host_input_schema`, with each
    ``oneOf`` / ``anyOf`` branch's copy of a file param left unconstrained
    (``{}``) so the ROOT declaration alone governs it. ChatGPT rewrites the root
    file param to a string, validates the model's file REFERENCE against that,
    then hydrates it into the file object -- it does not rewrite a branch copy,
    which therefore must not demand an object (live, MCH-0001). Advertising
    only: runtime admission keeps :func:`host_input_schema`, so an unhydrated
    reference never reaches ingress."""
    out = host_input_schema(schema, file_params)
    params = list(file_params)
    for branch in [*out.get("oneOf", []), *out.get("anyOf", [])]:
        props = branch.get("properties") if isinstance(branch, dict) else None
        if isinstance(props, dict):
            for p in params:
                if p in props:
                    props[p] = {}
    return out

# -- named host-file providers -------------------------------------------------------
# ChatGPT's hydrated download_url points at OpenAI's Azure blob storage, one
# account per region, and the region varies PER FILE (live: polandcentral and
# ukwest for one account). "@openai" expands to a CURATED set of EXACT hosts
# observed from genuine ChatGPT hydration -- never synthesized from region
# names and never a wildcard: Azure account names are globally first-come, so
# an unobserved name proves no ownership. Add a host here only from a
# host_file_refused log line produced by a real ChatGPT upload.
HOST_FILE_PROVIDER_ALIASES: Mapping[str, tuple] = {
    "@openai": (
        "files.oaiusercontent.com",
        "oaisdmntprpolandcentral.blob.core.windows.net",  # observed 2026-10-01
        "oaisdmntprukwest.blob.core.windows.net",  # observed 2026-10-01
        "oaisdmntprdenmarkeast.blob.core.windows.net",  # observed 2026-10-01
    ),
}


def expand_host_file_hosts(entries: Iterable[str]) -> tuple:
    """The allowlist as exact lower-case host names: a known ``@alias`` expands to
    its host set, any other entry is taken as a host, an unknown ``@alias`` is
    dropped (fail closed). Order-preserving, de-duplicated."""
    out: list[str] = []
    for entry in entries:
        name = str(entry or "").strip().lower()
        if not name:
            continue
        hosts = HOST_FILE_PROVIDER_ALIASES.get(name, ()) if name.startswith("@") else (name,)
        out.extend(h for h in hosts if h not in out)
    return tuple(out)


# -- host-file provenance (r0b2 design co-sign 9cf90cf5) --------------------------------
# CHATGPT_HOST_FILE: the call's validated OAuth client is the STRICTLY configured
# ChatGPT connector AND holds the dedicated host_file_hydration capability (live;
# deliberately NOT gate_only, which governs native paths). Derived once at the
# app tools/call boundary from server-authenticated state (never _meta, arguments
# or host names) and carried frozen to ingress. Only under it are OpenAI's
# per-region hydration accounts admitted by the bounded matcher below; any other
# value is GENERIC (exact-host allowlist only). Every other fetch control holds.
HOST_FILE_PROVENANCE_GENERIC = "generic"
HOST_FILE_PROVENANCE_CHATGPT = "chatgpt_host_file"
_OPENAI_HYDRATION_HOST = re.compile(r"oaisdmntpr[a-z0-9]{1,14}\.blob\.core\.windows\.net")
_OPENAI_HYDRATION_EXACT = frozenset({"files.oaiusercontent.com"})
_CLIENT_ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def derive_host_file_provenance(client_id: Any, *, configured_client_id: Any, capability: bool) -> str:
    """CHATGPT_HOST_FILE iff the validated client id equals a well-formed,
    explicitly configured ChatGPT client id and that client holds the
    host_file_hydration capability; GENERIC otherwise (including every
    malformed or empty input)."""
    cid = client_id if type(client_id) is str else ""
    configured = configured_client_id if type(configured_client_id) is str else ""
    if (
        capability is True
        and _CLIENT_ID_SHAPE.fullmatch(configured)
        and cid == configured
    ):
        return HOST_FILE_PROVENANCE_CHATGPT
    return HOST_FILE_PROVENANCE_GENERIC


def _openai_hydration_host(host: str) -> bool:
    return host in _OPENAI_HYDRATION_EXACT or _OPENAI_HYDRATION_HOST.fullmatch(host) is not None


_USER_AGENT = "aidocs-host-file-ingress/1"
# Addresses refused although some are globally routable (cloud metadata / wireserver).
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "169.254.169.254/32",  # AWS / GCP / Azure / OpenStack IMDS
        "168.63.129.16/32",  # Azure wireserver (public range)
        "100.100.100.200/32",  # Alibaba metadata
        "fd00:ec2::254/128",  # AWS IMDS over IPv6
    )
)
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


class HostFileRefused(Exception):
    """A refused host-file fetch. ``code``: a §16.3 code; ``reason``: a fixed
    local diagnostic. Neither ever carries the URL or any backend bytes."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _refuse(code: str, reason: str) -> HostFileRefused:
    return HostFileRefused(code, reason)


# -- the host parameter ------------------------------------------------------------------


@dataclass(frozen=True)
class HostFileRef:
    """The parsed host file parameter. ``download_url`` is excluded from repr."""

    download_url: str = field(repr=False)
    file_id: Optional[str] = None
    name: Optional[str] = field(default=None, repr=False)
    mime_type: Optional[str] = None


def parse_host_file_param(value: Any) -> HostFileRef:
    if not isinstance(value, Mapping):
        raise _refuse(HOST_FILE_UNRESOLVABLE, "file_param_malformed")
    url = value.get("download_url")
    if type(url) is not str or not 0 < len(url) <= DOWNLOAD_URL_MAX_CHARS:
        raise _refuse(HOST_FILE_UNRESOLVABLE, "file_param_malformed")
    file_id = value.get("file_id")
    name = value.get("file_name")  # the host contract's name
    if name is None:
        name = value.get("name")  # legacy shape
    mime = value.get("mime_type")
    return HostFileRef(
        download_url=url,
        file_id=file_id if type(file_id) is str and _FILE_ID.fullmatch(file_id) else None,
        name=name if type(name) is str and len(name) <= FILE_NAME_MAX_CHARS else None,
        mime_type=mime if type(mime) is str and len(mime) <= MIME_TYPE_MAX_CHARS else None,
    )


# -- content ---------------------------------------------------------------------------


def sniff_content_type(head: bytes) -> Optional[str]:
    """The type from the magic bytes -- the same four rules as the CRM's ``Upload.Sniff``."""
    b = bytes(head[:16])
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"%PDF":
        return "application/pdf"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_type(value: Optional[str]) -> str:
    if value is None:
        return ""
    base = value.split(";", 1)[0].strip().lower()
    return _TYPE_ALIASES.get(base, base)


# -- addresses -------------------------------------------------------------------------


def _ip_allowed(ip: Any) -> bool:
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _ip_allowed(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _ip_allowed(ip.sixtofour)
        if ip.teredo is not None:
            return False  # a tunnel endpoint: never a host-file provider
        if any(ip in net for net in _NAT64):
            return _ip_allowed(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if any(ip.version == net.version and ip in net for net in _DENIED_NETWORKS):
        return False
    if (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    ):
        return False
    return bool(ip.is_global)  # also refuses CGNAT 100.64/10 and other non-global space


def address_allowed(address: Any) -> bool:
    """True iff ``address`` is a globally routable, non-metadata unicast address."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    return _ip_allowed(ip)


def _ip_literal(host: str) -> Any:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


# -- the network layer (injectable) ------------------------------------------------------


def default_resolver(host: str, port: int) -> list[str]:
    seen: list[str] = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ONE vetted address; TLS verified against the hostname."""

    def __init__(self, host: str, port: int, *, address: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = address
        self._pinned_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._pinned_context.wrap_socket(sock, server_hostname=self.host)


def default_connector(host: str, port: int, address: str, timeout: float) -> http.client.HTTPConnection:
    """An UNCONNECTED HTTPS connection to ``address`` for ``host`` (no proxy, no redirects)."""
    context = ssl.create_default_context()  # CERT_REQUIRED + hostname check
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return _PinnedHTTPSConnection(host, port, address=address, timeout=timeout, context=context)


# -- the spool -------------------------------------------------------------------------


class _Spool:
    """Memory up to ``threshold`` bytes, then one private temp file. Always destroyable."""

    def __init__(self, directory: Optional[Path], threshold: int) -> None:
        self._dir = directory
        self._threshold = threshold
        self._mem = bytearray()
        self._file: Any = None
        self.path: Optional[Path] = None
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ValueError("spool destroyed")
        if self._file is None and len(self._mem) + len(data) > self._threshold:
            fd, name = tempfile.mkstemp(prefix="aidocs-hostfile-", suffix=".spool", dir=self._dir)
            self.path = Path(name)
            self._file = os.fdopen(fd, "w+b")
            self._file.write(self._mem)
            self._mem = bytearray()
        if self._file is not None:
            self._file.write(data)
        else:
            self._mem += data

    def iter_chunks(self, size: int) -> Iterator[bytes]:
        if self.closed:
            raise ValueError("spool destroyed")
        if self._file is None:
            mem = self._mem
            for i in range(0, len(mem), size):
                yield bytes(mem[i : i + size])
            return
        self._file.flush()
        self._file.seek(0)
        while True:
            if self.closed:
                raise ValueError("spool destroyed")
            chunk = self._file.read(size)
            if not chunk:
                return
            yield chunk

    def destroy(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._mem = bytearray()
        f, self._file = self._file, None
        try:
            if f is not None:
                f.close()
        finally:
            if self.path is not None:
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass


class IngestedFile:
    """The fetched, checked bytes plus their stable facts (§14.2). Ephemeral:
    :meth:`close` destroys the spool; use it as a context manager."""

    def __init__(self, spool: _Spool, *, sha256: str, length: int, content_type: str, file_id: Optional[str]) -> None:
        self._spool = spool
        self.sha256 = sha256
        self.length = length
        self.content_type = content_type
        self.file_id = file_id

    @property
    def closed(self) -> bool:
        return self._spool.closed

    @property
    def spool_path(self) -> Optional[Path]:
        """The on-disk spool (``None`` while held in memory); for audit of destruction only."""
        return self._spool.path

    def iter_chunks(self, size: int = READ_CHUNK_BYTES) -> Iterator[bytes]:
        return self._spool.iter_chunks(size)

    def close(self) -> None:
        self._spool.destroy()

    def __enter__(self) -> "IngestedFile":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # last-resort destruction; close() is the contract
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def __repr__(self) -> str:
        return (
            f"<IngestedFile sha256={self.sha256} length={self.length} "
            f"content_type={self.content_type} closed={self.closed}>"
        )


# -- what the application receives ----------------------------------------------------------


def relay_file_descriptor(ingested: IngestedFile) -> dict:
    """The file parameter as relayed: content-addressed, capability-free (§14.1, §14.2)."""
    out = {"download_url": RELAY_URL_PREFIX + ingested.sha256, "mime_type": ingested.content_type}
    if ingested.file_id is not None:
        out["file_id"] = ingested.file_id
    return out


def relay_arguments(arguments: Mapping[str, Any], file_params: Sequence[str], ingested: IngestedFile) -> dict:
    """``arguments`` with the ONE file parameter replaced by :func:`relay_file_descriptor`.

    Idempotent (a relayed body relays to itself), so a retry or a pinned
    resend carries the same bytes and the same ``body_sha256``.
    """
    if not isinstance(arguments, Mapping):
        raise TypeError("arguments must be an object")
    params = tuple(file_params)
    if len(params) != 1:
        raise ValueError("exactly one file parameter per call (§14.5)")
    if params[0] not in arguments:
        raise ValueError("the file parameter is absent")
    out = dict(arguments)
    out[params[0]] = relay_file_descriptor(ingested)
    return out


# -- the ingress ----------------------------------------------------------------------------


def _bounded(name: str, value: Any, ceiling: float) -> float:
    if type(value) not in (int, float) or not 0 < value <= ceiling:
        raise ValueError(f"{name} must be in (0, {ceiling}] (§14.5: limits may only tighten)")
    return float(value)


class HostFileIngress:
    """Fetch one host file capability under the §14.1 floor (see the module doc)."""

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        resolver: Optional[Callable[[str, int], Sequence[str]]] = None,
        connector: Optional[Callable[[str, int, str, float], Any]] = None,
        max_bytes: int = MAX_FILE_BYTES,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
        max_redirects: int = MAX_REDIRECTS,
        spool_dir: Optional[Any] = None,
        memory_threshold: int = MEMORY_THRESHOLD_BYTES,
        egress_audit: Optional[Callable[[dict], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        if isinstance(allowed_hosts, str):
            raise ValueError("allowed_hosts must be a collection of host names")
        hosts = tuple(str(h).strip().lower() for h in allowed_hosts)
        if not all(hosts):
            raise ValueError("allowed_hosts entries must be non-empty")
        self._allowed = hosts
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FILE_BYTES:
            raise ValueError(f"max_bytes must be 1..{MAX_FILE_BYTES} (§14.5)")
        self._max = max_bytes
        self._connect = _bounded("connect_timeout_s", connect_timeout_s, CONNECT_TIMEOUT_S)
        self._idle = _bounded("idle_timeout_s", idle_timeout_s, IDLE_TIMEOUT_S)
        self._total = _bounded("total_timeout_s", total_timeout_s, TOTAL_TIMEOUT_S)
        if type(max_redirects) is not int or not 0 <= max_redirects <= MAX_REDIRECTS:
            raise ValueError(f"max_redirects must be 0..{MAX_REDIRECTS} (§14.1)")
        self._max_redirects = max_redirects
        if type(memory_threshold) is not int or memory_threshold < 0:
            raise ValueError("memory_threshold must be a non-negative int")
        self._threshold = min(memory_threshold, MEMORY_THRESHOLD_BYTES)
        self._spool_dir = Path(spool_dir) if spool_dir is not None else None
        self._resolver = resolver or default_resolver
        self._connector = connector or default_connector
        self._egress_audit = egress_audit
        self._monotonic = monotonic or time.monotonic

    def __repr__(self) -> str:
        return f"<HostFileIngress providers={len(self._allowed)} max_bytes={self._max}>"

    # -- one hop -------------------------------------------------------------------------

    def _vet(self, url: str, hop: int, provenance: str = HOST_FILE_PROVENANCE_GENERIC) -> tuple[str, int, str, str]:
        """(host, port, request-target, vetted address) for ``url``; raise on any breach.
        Under CHATGPT_HOST_FILE provenance an OpenAI hydration host (bounded
        matcher) is admitted in addition to the exact allowlist, at every hop."""
        off_policy = HOST_FILE_REDIRECT_REFUSED if hop else None
        try:
            parts = urlsplit(url)
            port = parts.port
            host = parts.hostname
        except ValueError:
            raise _refuse(off_policy or HOST_FILE_UNRESOLVABLE, "url_malformed") from None
        scheme = parts.scheme.lower()
        if scheme != "https":
            if off_policy:
                raise _refuse(off_policy, "redirect_scheme_refused")
            if scheme in _NETWORK_SCHEMES:
                raise _refuse(HOST_FILE_PROVIDER_NOT_ALLOWED, "scheme_not_https")
            raise _refuse(HOST_FILE_UNRESOLVABLE, "reference_not_a_url")
        if "@" in parts.netloc or not host:
            raise _refuse(off_policy or HOST_FILE_UNRESOLVABLE, "url_authority_refused")
        if port not in (None, 443):
            raise _refuse(off_policy or HOST_FILE_PROVIDER_NOT_ALLOWED, "port_not_443")
        literal = _ip_literal(host)
        if literal is not None and not _ip_allowed(literal):
            raise _refuse(HOST_FILE_ADDRESS_REFUSED, "address_refused")
        url_host = f"[{host}]" if ":" in host else host
        try:
            # the chokepoint audits the HOST only; the signed path/query never reaches it
            allowed = self._allowed
            if provenance == HOST_FILE_PROVENANCE_CHATGPT and literal is None and _openai_hydration_host(host):
                allowed = (*allowed, host)
            assert_egress_allowed(
                f"https://{url_host}/", purpose=EGRESS_PURPOSE, allow_hosts=allowed, audit=self._egress_audit
            )
        except EgressRefused:
            raise _refuse(off_policy or HOST_FILE_PROVIDER_NOT_ALLOWED, "provider_not_allowed") from None
        if literal is not None:
            addresses = [str(literal)]
        else:
            try:
                addresses = list(self._resolver(host, 443))
            except Exception:  # noqa: BLE001 -- resolution failure: nothing sent
                raise _refuse(HOST_FILE_UNRESOLVABLE, "resolution_failed") from None
            if not addresses:
                raise _refuse(HOST_FILE_UNRESOLVABLE, "resolution_empty")
            if not all(type(a) is str and address_allowed(a) for a in addresses):
                raise _refuse(HOST_FILE_ADDRESS_REFUSED, "address_refused")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return host, 443, target, addresses[0]

    def _arm(self, conn: Any, deadline: float) -> None:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _refuse(HOST_FILE_UNRESOLVABLE, "total_timeout")
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(min(self._idle, remaining))
            except OSError:
                pass

    def _open(self, host: str, port: int, address: str) -> Any:
        try:
            conn = self._connector(host, port, address, self._connect)
            conn.connect()
        except Exception:  # noqa: BLE001 -- nothing was sent
            raise _refuse(HOST_FILE_UNRESOLVABLE, "connect_failed") from None
        return conn

    def _get(self, conn: Any, target: str, deadline: float) -> Any:
        self._arm(conn, deadline)
        try:
            conn.request(
                "GET",
                target,
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "User-Agent": _USER_AGENT,
                    "Connection": "close",
                },
            )
            self._arm(conn, deadline)
            return conn.getresponse()
        except HostFileRefused:
            raise
        except (OSError, http.client.HTTPException):
            raise _refuse(HOST_FILE_UNRESOLVABLE, "exchange_failed") from None

    @staticmethod
    def _check_headers(resp: Any) -> Optional[int]:
        """Bounded headers; identity encoding only; the declared length (advisory)."""
        try:
            headers = list(resp.getheaders())
        except Exception:  # noqa: BLE001
            raise _refuse(HOST_FILE_UNRESOLVABLE, "headers_unreadable") from None
        aggregate = sum(len(str(k)) + len(str(v)) for k, v in headers)
        if len(headers) > HEADERS_MAX_COUNT or aggregate > HEADERS_MAX_AGGREGATE_BYTES:
            raise _refuse(HOST_FILE_UNRESOLVABLE, "headers_over_bound")
        encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            raise _refuse(HOST_FILE_UNRESOLVABLE, "content_encoding_refused")
        declared = resp.getheader("Content-Length")
        if declared is None:
            return None
        declared = str(declared).strip()
        if not declared.isdigit() or len(declared) > 15:
            raise _refuse(HOST_FILE_UNRESOLVABLE, "content_length_malformed")
        return int(declared)

    # -- the fetch -----------------------------------------------------------------------

    def fetch(self, file_param: Any, *, provenance: str = HOST_FILE_PROVENANCE_GENERIC) -> IngestedFile:
        """Fetch, cap, hash and sniff one host file. The caller owns (and must
        close) the returned :class:`IngestedFile`; on any refusal nothing survives.
        ``provenance`` is the frozen value derived at admission; anything other
        than CHATGPT_HOST_FILE is GENERIC."""
        if provenance != HOST_FILE_PROVENANCE_CHATGPT:
            provenance = HOST_FILE_PROVENANCE_GENERIC
        ref = parse_host_file_param(file_param)
        deadline = self._monotonic() + self._total
        url = ref.download_url
        for hop in range(self._max_redirects + 1):
            try:
                host, port, target, address = self._vet(url, hop, provenance)
            except HostFileRefused as exc:
                # The operator's only view of WHICH provider was refused: scheme +
                # host (what the egress audit holds), never the signed path/query.
                try:
                    parts = urlsplit(url)
                    scheme, refused_host = parts.scheme, parts.hostname
                    port = parts.port if parts.port is not None else "default"
                except ValueError:
                    scheme, refused_host, port = "", "", "invalid"
                _log.warning("host_file_refused code=%s reason=%s hop=%d scheme=%s host=%s port=%s provenance=%s",
                             exc.code, exc.reason, hop, _log_safe(scheme, 16), _log_safe(refused_host, 255),
                             _log_safe(port, 16), provenance)
                raise
            if self._monotonic() >= deadline:
                raise _refuse(HOST_FILE_UNRESOLVABLE, "total_timeout")
            conn = self._open(host, port, address)
            try:
                resp = self._get(conn, target, deadline)
                status = getattr(resp, "status", None)
                if type(status) is int and 300 <= status < 400:
                    location = resp.getheader("Location")
                    if hop >= self._max_redirects or type(location) is not str or not location.strip():
                        raise _refuse(HOST_FILE_REDIRECT_REFUSED, "redirect_refused")
                    try:
                        url = urljoin(url, location.strip())
                    except ValueError:
                        raise _refuse(HOST_FILE_REDIRECT_REFUSED, "redirect_malformed") from None
                    if len(url) > DOWNLOAD_URL_MAX_CHARS:
                        raise _refuse(HOST_FILE_REDIRECT_REFUSED, "redirect_malformed")
                    continue
                if status != 200:
                    raise _refuse(HOST_FILE_UNRESOLVABLE, "status_refused")
                declared = self._check_headers(resp)
                if declared is not None and declared > self._max:
                    raise _refuse(HOST_FILE_TOO_LARGE, "declared_length_over_cap")
                return self._spool_body(conn, resp, ref, declared, deadline)
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        raise _refuse(HOST_FILE_REDIRECT_REFUSED, "redirect_refused")  # pragma: no cover

    def _spool_body(self, conn: Any, resp: Any, ref: HostFileRef, declared: Optional[int], deadline: float) -> IngestedFile:
        spool = _Spool(self._spool_dir, self._threshold)
        try:
            digest = hashlib.sha256()
            total = 0
            head = b""
            while True:
                self._arm(conn, deadline)
                try:
                    chunk = resp.read(READ_CHUNK_BYTES)
                except (OSError, http.client.HTTPException):
                    raise _refuse(HOST_FILE_UNRESOLVABLE, "read_failed") from None
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max:  # the hard cap, whatever Content-Length said
                    raise _refuse(HOST_FILE_TOO_LARGE, "body_over_cap")
                digest.update(chunk)
                spool.write(chunk)
                if len(head) < 16:
                    head = (head + chunk)[:16]
            if declared is not None and total != declared:
                raise _refuse(HOST_FILE_UNRESOLVABLE, "length_mismatch")
            if total == 0:
                raise _refuse(APP_VALIDATION_FAILED, "host_file_empty")
            sniffed = sniff_content_type(head)
            if sniffed is None:
                raise _refuse(APP_VALIDATION_FAILED, "host_file_type_unsupported")
            claimed = _normalize_type(ref.mime_type)
            if claimed not in _UNDECLARED_TYPES and claimed != sniffed:
                raise _refuse(APP_VALIDATION_FAILED, "host_file_type_mismatch")
            return IngestedFile(spool, sha256=digest.hexdigest(), length=total, content_type=sniffed, file_id=ref.file_id)
        except BaseException:
            spool.destroy()
            raise
