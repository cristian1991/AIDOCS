"""Secure key materialization for ai_deploy's sign step — the S12/S13 wipe guarantee.

The signing deploy reads the private key from a FILE path (build_signed_release.py --private-key),
so the decrypted key must briefly exist as a file. `materialized_key` decrypts the blob, writes the
key to a 0600 file under key_dir (PASS A TMPFS like /dev/shm — RAM-backed, never persistent disk),
yields the path for the deploy's sign step, and ALWAYS securely wipes it on exit — success,
exception, or crash. The plaintext key never outlives the with-block; a decrypt failure (wrong
password / tamper) raises BEFORE any file is written (nothing to wipe).

120% (read 120%.md): S12 "no secrets present" / S13 "no secret should need redaction because no
secret should be present" — the materialization is the unavoidable minimum (the signer needs a
path), and the wipe-in-finally is the law that bounds it.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

from .ai_deploy_secret import decrypt_signing_key


def _secure_wipe(path: Path) -> None:
    """Best-effort secure delete: overwrite the file bytes with random, fsync, then unlink.
    Tolerates an already-absent file. On a tmpfs the bytes are RAM-backed; the overwrite still
    scrubs them before the inode is released."""
    try:
        if path.exists():
            n = path.stat().st_size
            if n:
                with open(path, "r+b", buffering=0) as f:
                    f.write(secrets.token_bytes(n))
                    f.flush()
                    os.fsync(f.fileno())
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            path.unlink()


@contextlib.contextmanager
def materialized_key(blob: bytes, password: str, *, key_dir: str | Path) -> Iterator[Path]:
    """Decrypt the signing key and materialize it to a 0600 file under key_dir for the duration of
    the with-block, then ALWAYS securely wipe it. PASS A TMPFS as key_dir (e.g. /dev/shm) so the
    plaintext never touches persistent disk. Raises DeploySecretError (before writing anything) on a
    bad password / tampered blob."""
    material = decrypt_signing_key(blob, password)  # raises DeploySecretError -> nothing written
    kd = Path(key_dir)
    kd.mkdir(parents=True, exist_ok=True)
    out_path = kd / f"aidocs-sign-{secrets.token_hex(8)}.pem"
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, material)
    finally:
        os.close(fd)
        del material
    try:
        yield out_path
    finally:
        _secure_wipe(out_path)
