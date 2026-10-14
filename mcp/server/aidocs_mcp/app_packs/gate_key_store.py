"""The gate's protected key store for AIDOCS-held app-pack keys (#1134 B6; r0b2 f52809cd-58b).

Matches the established gate-held secret pattern (``daemon_lifecycle_authority``:
a dedicated ``~/.aidocs/trust`` directory, never a project or daemon dir, files
created owner-only 0600), and tightens it:

* **Location.** ``$AIDOCS_APP_KEY_DIR`` if set, else
  ``~/.aidocs/trust/app_keys``. The directory is created 0700.
* **Atomic create-if-absent.** :meth:`GateKeyStore.create_if_absent` writes a
  complete owner-only temp file (``O_EXCL``, fsync) and ``os.link``\\s it to the
  final name, which fails if the name exists: racing workers converge on ONE
  file, and nobody ever reads a half-written key.
* **Explicit replacement** (:meth:`replace`) only under the store lock
  (:meth:`locked`), via temp + ``os.replace``.

Keys here are DEDICATED and independent of ``AIDOCS_INTERNAL_S2S_SECRET``:
rotating the S2S credential never changes a stored digest or a published kid.
Nothing here is ever logged or returned over a wire.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import time
from pathlib import Path
from typing import Iterator, Optional

__all__ = ["ENV_KEY_DIR", "GateKeyStore", "GateKeyStoreUnavailable", "default_gate_key_dir"]

ENV_KEY_DIR = "AIDOCS_APP_KEY_DIR"
_LOCK_NAME = ".lock"
_LOCK_STALE_S = 30.0
_LOCK_WAIT_S = 10.0


class GateKeyStoreUnavailable(RuntimeError):
    """The key store cannot be read or written. Fail closed; never a fallback key."""


def default_gate_key_dir() -> Path:
    override = os.environ.get(ENV_KEY_DIR)
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "trust" / "app_keys"


def _valid_name(name: str) -> str:
    if type(name) is not str or not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("key file name")
    return name


class GateKeyStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else default_gate_key_dir()

    def _ensure_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):  # best effort on Windows / exotic filesystems
            self.root.chmod(0o700)

    def _write_temp(self, data: bytes) -> Path:
        tmp = self.root / f".tmp-{secrets.token_hex(8)}"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
        return tmp

    def path(self, name: str) -> Path:
        return self.root / _valid_name(name)

    def read(self, name: str) -> Optional[bytes]:
        try:
            return self.path(name).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GateKeyStoreUnavailable("read") from exc

    def create_if_absent(self, name: str, data: bytes) -> bytes:
        """Store ``data`` under ``name`` unless it exists; return the STORED bytes."""
        final = self.path(name)
        existing = self.read(name)
        if existing is not None:
            return existing
        try:
            self._ensure_dir()
            tmp = self._write_temp(data)
            try:
                os.link(str(tmp), str(final))
            except FileExistsError:
                pass  # another worker won; its bytes are THE bytes
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(str(tmp))
            return final.read_bytes()
        except OSError as exc:
            raise GateKeyStoreUnavailable("create") from exc

    def replace(self, name: str, data: bytes) -> None:
        """Explicit overwrite; callers hold :meth:`locked`."""
        try:
            self._ensure_dir()
            tmp = self._write_temp(data)
            os.replace(str(tmp), str(self.path(name)))
        except OSError as exc:
            raise GateKeyStoreUnavailable("replace") from exc

    def delete(self, name: str) -> None:
        try:
            os.unlink(str(self.path(name)))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GateKeyStoreUnavailable("delete") from exc

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """A cross-process exclusive lock (``O_EXCL`` lock file) for explicit transitions."""
        try:
            self._ensure_dir()
        except OSError as exc:
            raise GateKeyStoreUnavailable("lock") from exc
        lock = self.root / _LOCK_NAME
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                        os.unlink(str(lock))
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise GateKeyStoreUnavailable("lock_timeout") from None
                time.sleep(0.01)
            except OSError as exc:
                raise GateKeyStoreUnavailable("lock") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.unlink(str(lock))
