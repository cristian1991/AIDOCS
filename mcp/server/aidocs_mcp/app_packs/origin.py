"""Canonical origins -- wire companion rev 8 §1.6 (v1 origin profile).

Every origin (``application_origin``, ``aidocs_origin``, the origin part of
``link_return_uri`` / ``redirect_uri``) is handled as a parsed value in its ONE
canonical spelling. No parser normalization is relied on: a value that is not
already canonical is refused, never repaired (IDNA mapping, case folding,
trailing dots and percent-encoding are all refused).

Comparison is exact equality of the tuple (scheme, host, effective port).
Prefix, suffix and substring matching are never used.

The only relaxation is :meth:`OriginPolicy.test_only_loopback`: the §1.6 test
harness may allow loopback ``http`` by EXPLICIT configuration. It is TEST-ONLY,
never shipped, never the default, and never satisfies M1 acceptance (build
plan R3). :data:`PRODUCTION_ORIGIN_POLICY` is the default everywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PRODUCTION_ORIGIN_POLICY",
    "CanonicalOrigin",
    "OriginPolicy",
    "OriginRefused",
    "parse_origin",
    "parse_url",
    "same_origin",
]

_LABEL = r"[a-z0-9-]+"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*"
# scheme://host[:port][path] -- no userinfo, no query, no fragment.
_URL_RE = re.compile(rf"^(?P<scheme>https?)://(?P<host>{_HOST})(?::(?P<port>[1-9][0-9]{{0,4}}))?(?P<path>/[^?#]*)?$")
# RFC 3986 path characters (pchar + "/"); percent-encoding allowed in the path only.
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/%-]*$")
_DEFAULT_PORTS = {"https": 443, "http": 80}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


class OriginRefused(ValueError):
    """The value is not in the v1 canonical origin profile. Never repaired."""


@dataclass(frozen=True)
class OriginPolicy:
    """``allow_loopback_http`` is TEST-ONLY (wire §1.6 harness); never shipped."""

    allow_loopback_http: bool = False

    @classmethod
    def test_only_loopback(cls) -> "OriginPolicy":
        """TEST-ONLY: accept ``http`` for loopback hosts. Never a product path."""
        return cls(allow_loopback_http=True)


PRODUCTION_ORIGIN_POLICY = OriginPolicy()


@dataclass(frozen=True)
class CanonicalOrigin:
    scheme: str
    host: str
    port: int  # the EFFECTIVE port

    @property
    def serialized(self) -> str:
        """``scheme://host`` or ``scheme://host:port``; the default port omitted."""
        if self.port == _DEFAULT_PORTS[self.scheme]:
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"


def _parse(value: Any, policy: OriginPolicy) -> tuple[CanonicalOrigin, str]:
    if not isinstance(policy, OriginPolicy):
        raise OriginRefused("origin policy is not an OriginPolicy")
    if type(value) is not str or not value.isascii():
        raise OriginRefused("not an ASCII URL")
    m = _URL_RE.fullmatch(value)
    if m is None:
        raise OriginRefused("not in the v1 canonical origin profile")
    scheme, host = m.group("scheme"), m.group("host")
    if scheme == "http" and not (policy.allow_loopback_http and host in _LOOPBACK_HOSTS):
        raise OriginRefused("scheme must be https")
    port_text = m.group("port")
    port = int(port_text) if port_text is not None else _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        raise OriginRefused("port out of range")
    path = m.group("path") or ""
    if path and not _PATH_RE.fullmatch(path):
        raise OriginRefused("path carries characters outside RFC 3986")
    return CanonicalOrigin(scheme, host, port), path


def parse_origin(value: Any, *, policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY) -> CanonicalOrigin:
    """A value DECLARED as an origin: no path, or exactly ``/`` (rule 7)."""
    origin, path = _parse(value, policy)
    if path not in ("", "/"):
        raise OriginRefused("an origin has no path")
    return origin


def parse_url(value: Any, *, policy: OriginPolicy = PRODUCTION_ORIGIN_POLICY) -> tuple[CanonicalOrigin, str]:
    """A full URL (``link_return_uri``, ``redirect_uri``): its canonical origin and path.

    The §8.2 path policy is the caller's, applied only AFTER the origin check.
    """
    return _parse(value, policy)


def same_origin(a: CanonicalOrigin, b: CanonicalOrigin) -> bool:
    """Exact (scheme, host, effective port) equality; nothing else."""
    return isinstance(a, CanonicalOrigin) and isinstance(b, CanonicalOrigin) and (
        (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)
    )
