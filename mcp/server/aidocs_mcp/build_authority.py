"""The build AUTHORITY — where ai_version's ``deployed`` and ``released`` come from.

OPERATOR DIRECTIVE (2026-08-22): "deploy and release build numbers should come
from the SERVER. the local version comes from code, the deployed and released
versions come from the SERVER (via web requests or something)."

WHY THIS SPLIT IS THE HONEST ONE. A runtime can only ever know what it was
BUILT from — that is the in-artefact stamp, and it answers ``running``. What is
DEPLOYED (serving on the gate right now) and what is RELEASED (the last blessed
build) are facts about the gate, not about the machine asking. Until this
module, the client read both from LOCAL trust artefacts — a deploy seal beside
a checkout, a release manifest inside the package — which an installed runtime
does not have and, by construction, never will. "UNVERIFIED forever" was not
an honest unknown; it was the wrong machine being asked.

So the client ASKS. ``GET <authority>/v1/version`` returns the gate's own
``authority_build_info()`` — its running axis (frozen in memory at boot), its
disk stamp, its signed manifest — and the client maps those onto its
``deployed`` / ``released`` axes, naming the source on each. A gate that is
serving older bytes than it was handed (the false-restart shape measured
2026-08-22, pid 16336) shows up here as the truth it is, because memory, not a
seal file, is what answers.

TWO HALVES, NO SELF-FETCH. ``authority_build_info()`` (server side) is local
reads only and is what the endpoint serves; ``build_info()`` (client side)
fetches. The gate's own ai_version tool calls the server half — a process that
fetched from itself through its own edge would be a hang waiting for a busy
transport.

GOVERNED EGRESS (#195). The URL is DERIVED from the configured base — never
agent-supplied — and every call gates through ``assert_egress_allowed`` with
the authority's own host as the only allowed one. ``AIDOCS_BUILD_AUTHORITY_URL=
off`` is the air-gap posture: no call is made and the axes say they were
disabled, which is a different fact from unreachable and from absent.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from .governed_egress import EgressRefused, assert_egress_allowed

#: The wire contract between the two halves. Bump when the axis shape changes;
#: a client refuses an unknown schema rather than reading a new shape as blanks.
AUTHORITY_SCHEMA = "aidocs-build-axes/1"
#: The open transport route (outer_gate_transport.RC_VERSION serves it).
VERSION_PATH = "/v1/version"
#: Same host the sync hub already talks to; "the server" has one name.
DEFAULT_AUTHORITY_URL = "https://mcp.codenexus.cloud"
#: Operator override. A URL points elsewhere; ``off`` disables the fetch.
ENV_AUTHORITY_URL = "AIDOCS_BUILD_AUTHORITY_URL"
#: The configured hub is the authority unless the env says otherwise.
SETTING_AUTHORITY_URL = "sync.vps_hub_url"
#: ai_version is interactive; a slow authority must degrade to an honest
#: unknown, never hold the tool hostage.
FETCH_TIMEOUT_S = 4.0

_DISABLED_WORDS = frozenset({"off", "0", "false", "none", "disabled", "no"})


class AuthorityUnreachable(Exception):
    """No usable answer from the authority: network, HTTP status, timeout."""


class AuthorityMalformed(ValueError):
    """The authority answered, but not in the build-axes contract."""


def _setting(key: str, default: str) -> str:
    """Config seam (tests replace it). Factory + global layers only — this
    question has no project scope."""
    try:
        from .config import get_setting

        value = get_setting(key, project_root=None, default=default)
        return str(value) if value else default
    except Exception:  # noqa: BLE001 — a config read must never break version reporting
        return default


def authority_url() -> str:
    """Where to ask. ``""`` means DISABLED (the caller reports it as such).

    Order: env override (``off`` or a URL) > the configured sync-hub URL >
    the default public gate. Trailing slashes stripped so path joins are
    byte-stable.
    """
    raw = os.environ.get(ENV_AUTHORITY_URL)
    if raw is not None:
        raw = raw.strip()
        if raw.lower() in _DISABLED_WORDS:
            return ""
        if raw:
            return raw.rstrip("/")
    value = _setting(SETTING_AUTHORITY_URL, DEFAULT_AUTHORITY_URL)
    return str(value or DEFAULT_AUTHORITY_URL).strip().rstrip("/")


def _urllib_http(url: str, timeout: float):
    """Default channel: ``(status, body_bytes)``. Injected in tests."""
    import urllib.request

    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "aidocs-version"},
    )
    # The host was allowlisted by assert_egress_allowed in the caller — the only
    # host this can ever reach is the configured authority's own.
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — allowlisted host
        return resp.status, resp.read()


def fetch_authority_axes(
    base_url: str,
    *,
    timeout: float = FETCH_TIMEOUT_S,
    http=None,
) -> dict:
    """GET the authority's build axes. Raises, never returns a blank.

    ``AuthorityUnreachable`` — nothing usable came back (the caller reports an
    honest unknown naming the host). ``AuthorityMalformed`` — something came
    back that is not the contract (reported by name, never read as blanks).
    ``EgressRefused`` propagates untouched: a refused host is a policy verdict,
    not a network failure, and must not be relabelled as one.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise AuthorityUnreachable("no authority URL configured")
    url = base + VERSION_PATH
    host = assert_egress_allowed(
        url,
        purpose="build_authority",
        allow_hosts=[urlparse(base).hostname or ""],
    )
    try:
        status, body = (http or _urllib_http)(url, timeout)
    except EgressRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — every transport failure is one fact: unreachable
        raise AuthorityUnreachable(f"{type(exc).__name__}: {exc}") from exc
    if int(status) != 200:
        raise AuthorityUnreachable(f"HTTP {status} from {host}")
    try:
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 — non-JSON is one fact: malformed
        raise AuthorityMalformed(f"non-JSON body from {host}: {type(exc).__name__}") from exc
    schema = data.get("schema") if isinstance(data, dict) else None
    if schema != AUTHORITY_SCHEMA:
        raise AuthorityMalformed(
            f"unexpected schema {schema!r} from {host} (wanted {AUTHORITY_SCHEMA!r})"
        )
    for axis in ("running", "deployed", "released"):
        if not isinstance(data.get(axis), dict):
            raise AuthorityMalformed(f"axis {axis!r} missing from {host}'s payload")
    return data
