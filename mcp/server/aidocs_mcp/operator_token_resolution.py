"""Machine-side operator token cache + the ONE token-resolution door (#421).

AUTHENTICATE ONCE per machine: ``aidocs operator-login`` mints a bearer token
(password-gated via ``IdentityStore.login``) and — by default — caches it in
a per-user file OUTSIDE any repo (``~/.aidocs/operator_token.json``,
overridable via the ``AIDOCS_TOKEN_CACHE`` env var). Every CLI surface that
needs an operator token then resolves it through ONE chain:

    1. env  ``AIDOCS_OPERATOR_TOKEN``
    2. CLI  ``--operator-token <token>``
    3. machine cache (if not expired; expired rows are pruned on read)

This never weakens the server-side gate: the resolved token is still
validated against ``identity_tokens`` on every use, and per-session binding
approval remains an explicit one-command consent — the cache only removes
the credential RE-ENTRY ceremony, not the consent.

Security posture:
  - The default path lives under the user's HOME; permissions are tightened
    to owner-only where the platform supports it (POSIX chmod 0600). On
    Windows the per-user profile directory is the boundary — best-effort,
    no ACL gymnastics in this slice.
  - A CUSTOM cache path that is world/group-readable is REFUSED
    (``PermissionError``): a bearer token is never written where other
    local users can read it.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

ENV_VAR = "AIDOCS_OPERATOR_TOKEN"
FLAG = "--operator-token"
CACHE_PATH_ENV = "AIDOCS_TOKEN_CACHE"

def default_cache_path() -> Path:
    """The per-user machine cache location (env-overridable for tests /
    multi-profile setups)."""
    override = str(os.environ.get(CACHE_PATH_ENV) or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "operator_token.json"


def _world_or_group_readable(path: Path) -> bool:
    """POSIX group/other read bits on an existing path. Windows has no
    POSIX mode bits worth trusting (st_mode is synthetic 0o666), so the
    check is a no-op there — the user profile dir is the boundary."""
    if os.name == "nt":
        return False
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return False
    return bool(mode & 0o077)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_cache(
    *,
    token: str,
    user_id: str,
    expires_at: str,
    project_root: str | Path = "",
    cache_path: str | Path | None = None,
) -> Path:
    """Persist the bearer token to the machine cache. Returns the path.

    Owner-only perms best-effort (chmod 0600 on POSIX). A CUSTOM path
    (``cache_path`` given) that is world/group-readable — before or after
    the write — raises ``PermissionError`` and leaves no token behind.
    """
    if not token:
        raise ValueError("token is required")
    custom = cache_path is not None
    path = Path(cache_path) if custom else default_cache_path()
    if custom and _world_or_group_readable(path):
        raise PermissionError(
            f"refusing to cache the operator token to world/group-readable path: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": str(token),
        "user_id": str(user_id or ""),
        "expires_at": str(expires_at or ""),
        "project_root": str(project_root or "").replace("\\", "/"),
        "cached_at": _iso_now(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    if custom and _world_or_group_readable(path):
        with contextlib.suppress(OSError):
            path.unlink()
        raise PermissionError(
            f"refusing to cache the operator token to world/group-readable path: {path}"
        )
    return path


def read_cache(cache_path: str | Path | None = None) -> dict | None:
    """Read the cached token row, or None. EXPIRED rows are deleted on
    read (the prune contract) so a stale token never lingers on disk."""
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(row, dict) or not str(row.get("token") or "").strip():
        return None
    expires_at = str(row.get("expires_at") or "")
    # ISO-8601 UTC "%Y-%m-%dT%H:%M:%SZ" compares lexicographically.
    if not expires_at or expires_at <= _iso_now():
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return row


def clear_cache(cache_path: str | Path | None = None) -> bool:
    """Remove the cached token file (logout hygiene). True iff removed."""
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    try:
        path.unlink()
        return True
    except OSError:
        return False


def resolve_operator_token(
    args: list[str] | None = None,
    *,
    env_var: str = ENV_VAR,
    flag: str = FLAG,
    cache_path: str | Path | None = None,
) -> tuple[str, str]:
    """THE single token-resolution door. Returns ``(token, source)`` with
    source in {'env', 'flag', 'cache', ''}. Never raises."""
    tok = str(os.environ.get(env_var) or "").strip()
    if tok:
        return tok, "env"
    argv = list(args or [])
    if flag in argv:
        try:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                tok = str(argv[idx + 1] or "").strip()
                if tok:
                    return tok, "flag"
        except Exception:
            pass
    try:
        row = read_cache(cache_path)
    except Exception:
        row = None
    if row:
        return str(row["token"]).strip(), "cache"
    return "", ""
