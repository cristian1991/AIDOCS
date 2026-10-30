"""A local, machine-held MAC key: create once, read forever -- neutral public.

Design decision (2026-10-29): app_packs (and its gate key custody store) is
private, and no public module may import it. The shell ask-grant ledger needs
ONE local HMAC key to seal its persisted "allow always" rows; that is a generic local
helper, not gate custody, so it lives here.

Same location and file discipline the store used before, so keys already on
disk keep verifying: ``$AIDOCS_APP_KEY_DIR`` if set, else
``~/.aidocs/trust/app_keys``; the directory is created 0700 and the file
0600. Creation is atomic create-if-absent: a complete owner-only temp file
(``O_EXCL``, fsync) is ``os.link``-ed to the final name, which fails if the
name exists, so racing workers converge on ONE key and nobody reads a
half-written one. The key is never logged or returned over a wire.
"""
from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

ENV_KEY_DIR = "AIDOCS_APP_KEY_DIR"


class LocalMacKeyUnavailable(RuntimeError):
    """The key cannot be read or written. Fail closed; never a fallback key."""


def key_dir() -> Path:
    override = os.environ.get(ENV_KEY_DIR)
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "trust" / "app_keys"


def _valid_name(name: str) -> str:
    if type(name) is not str or not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("key file name")
    return name


def mac_key(name: str, *, size: int = 32, root: Path | None = None) -> bytes:
    """The stored key ``name``, created with ``size`` random bytes if absent."""
    base = Path(root) if root is not None else key_dir()
    final = base / _valid_name(name)
    try:
        return final.read_bytes()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LocalMacKeyUnavailable("read") from exc
    try:
        base.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):  # best effort on Windows / exotic filesystems
            base.chmod(0o700)
        tmp = base / f".tmp-{secrets.token_hex(8)}"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(size))
            os.fsync(fd)
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
        try:
            os.link(str(tmp), str(final))
        except FileExistsError:
            pass  # another worker won; its bytes are THE bytes
        finally:
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
        return final.read_bytes()
    except OSError as exc:
        raise LocalMacKeyUnavailable("create") from exc
