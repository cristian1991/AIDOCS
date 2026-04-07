"""MCP Registry client — discover and search MCP servers from the official registry.

Registry: https://registry.modelcontextprotocol.io
API: v0.1 (read endpoints are unauthenticated)

Provides:
- search_servers: search by keyword, paginated
- get_server: get a specific server's latest version
- get_server_versions: list all versions of a server
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from urllib.error import URLError
from urllib.request import Request, urlopen


_REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0.1"
_TIMEOUT = 10  # seconds
_CACHE_TTL = 300  # 5 minutes


@dataclass(slots=True)
class RegistryServer:
    name: str
    description: str
    version: str
    title: str | None = None
    website_url: str | None = None
    repository: str | None = None
    packages: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "title": self.title,
            "website_url": self.website_url,
            "repository": self.repository,
            "packages": self.packages,
        }

    def install_commands(self) -> list[dict[str, str]]:
        """Generate install commands for each package type."""
        commands: list[dict[str, str]] = []
        for pkg in self.packages:
            reg_type = str(pkg.get("registryType", "")).lower()
            identifier = str(pkg.get("identifier", ""))
            version = str(pkg.get("version", ""))
            runtime_hint = str(pkg.get("runtimeHint", ""))

            if reg_type == "npm":
                runner = runtime_hint or "npx"
                cmd = f"{runner} {identifier}" + (f"@{version}" if version else "")
                commands.append({"type": "npm", "command": cmd, "transport": _transport_type(pkg)})
            elif reg_type == "pypi":
                runner = runtime_hint or "uvx"
                cmd = f"{runner} {identifier}" + (f"=={version}" if version else "")
                commands.append({"type": "pypi", "command": cmd, "transport": _transport_type(pkg)})
            elif reg_type == "oci":
                cmd = f"docker run {identifier}"
                commands.append({"type": "docker", "command": cmd, "transport": _transport_type(pkg)})

        return commands


def _transport_type(pkg: dict[str, object]) -> str:
    transport = pkg.get("transport", {})
    if isinstance(transport, dict):
        return str(transport.get("type", "stdio"))
    return "stdio"


@dataclass(slots=True)
class SearchResult:
    servers: list[RegistryServer]
    total_count: int
    next_cursor: str | None = None


# ── Cache ──

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> object | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry[0] < _CACHE_TTL:
            return entry[1]
        return None


def _cache_set(key: str, value: object) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


# ── HTTP helpers ──

def _fetch_json(url: str) -> dict[str, object]:
    """Fetch JSON from a URL with timeout."""
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "AIDOCS-MCP/1.0"})
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise ConnectionError(f"Registry request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from registry: {exc}") from exc


def _parse_server(data: dict[str, object]) -> RegistryServer:
    """Parse a server entry from registry response."""
    server_data = data.get("server", data)
    if isinstance(server_data, dict):
        packages_raw = server_data.get("packages", [])
        repo = server_data.get("repository")
        repo_url = None
        if isinstance(repo, dict):
            repo_url = str(repo.get("url", ""))
        elif isinstance(repo, str):
            repo_url = repo

        return RegistryServer(
            name=str(server_data.get("name", "")),
            description=str(server_data.get("description", "")),
            version=str(server_data.get("version", "")),
            title=str(server_data.get("title", "")) or None,
            website_url=str(server_data.get("websiteUrl", "")) or None,
            repository=repo_url or None,
            packages=list(packages_raw) if isinstance(packages_raw, list) else [],
        )
    raise ValueError("Invalid server data format")


# ── Public API ──

def search_servers(
    query: str = "",
    *,
    limit: int = 20,
    cursor: str | None = None,
) -> SearchResult:
    """Search the MCP registry for servers.

    Args:
        query: Search term (substring match on name/description).
        limit: Max results (1-100, default 20).
        cursor: Pagination cursor from previous result.

    Returns:
        SearchResult with matching servers and pagination info.
    """
    limit = max(1, min(limit, 100))
    cache_key = f"search:{query}:{limit}:{cursor}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    params = [f"limit={limit}"]
    if query:
        from urllib.parse import quote
        params.append(f"search={quote(query)}")
    if cursor:
        from urllib.parse import quote
        params.append(f"cursor={quote(cursor)}")

    url = f"{_REGISTRY_BASE}/servers?{'&'.join(params)}"
    data = _fetch_json(url)

    servers: list[RegistryServer] = []
    for entry in data.get("servers", []):
        if isinstance(entry, dict):
            try:
                servers.append(_parse_server(entry))
            except (ValueError, KeyError):
                continue

    metadata = data.get("metadata", {})
    next_cursor = str(metadata.get("nextCursor", "")) if isinstance(metadata, dict) else None

    result = SearchResult(
        servers=servers,
        total_count=int(metadata.get("count", len(servers))) if isinstance(metadata, dict) else len(servers),
        next_cursor=next_cursor or None,
    )
    _cache_set(cache_key, result)
    return result


def get_server(name: str, version: str = "latest") -> RegistryServer | None:
    """Get a specific server from the registry.

    Args:
        name: Server name (e.g. "io.github.user/server-name").
        version: Version string or "latest".

    Returns:
        RegistryServer or None if not found.
    """
    cache_key = f"server:{name}:{version}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    from urllib.parse import quote
    url = f"{_REGISTRY_BASE}/servers/{quote(name, safe='')}/versions/{quote(version, safe='')}"

    try:
        data = _fetch_json(url)
    except (ConnectionError, ValueError):
        return None

    try:
        server = _parse_server(data)
        _cache_set(cache_key, server)
        return server
    except (ValueError, KeyError):
        return None


def clear_cache() -> None:
    """Clear the registry cache."""
    with _cache_lock:
        _cache.clear()
