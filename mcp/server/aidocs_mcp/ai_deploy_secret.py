"""ai_deploy secret gate — the CRYPTO core that opens the password-encrypted signing-key blob.

The release signing key never lives in plaintext at rest. It is stored as a password-encrypted blob
in auth-protected cloud storage; at deploy time ai_deploy's custom gate fetches it (OAuth — a
separate I/O layer) and opens it HERE with the operator's FRESH per-deploy password. This module is
the pure decrypt: blob bytes + password -> plaintext key, or a NAMED refusal. It never persists,
logs, or returns a partial key.

120% rationale (read 120%.md):
  §16  no new crypto guest — reuse the already-trusted+pinned `cryptography` (release_trust's Ed25519
       lib), not a fresh age/gpg dependency.
  §0/§6 fail-closed: AES-256-GCM is AUTHENTICATED — a wrong password OR a single tampered byte fails
       the open (InvalidTag) and raises DeploySecretError; there is NO silent-degrade to a wrong key.
  §12/§13 the returned key is the caller's to use transiently and wipe; nothing is written or logged
       here, and DeploySecretError carries no secret material.
  §5  server-side, mcp/-private; never public-mirrored.

Blob layout (binary, fixed offsets):
  MAGIC(8) | salt(16) | nonce(12) | ciphertext+GCM-tag(rest)
KDF: scrypt(password, salt) -> 32-byte key. AEAD: AES-256-GCM with MAGIC as associated data (binds
the format version into the authentication).
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"AIDOCSK1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
# scrypt cost params (memory-hard). Bound to the blob format version (MAGIC) — changing them is a
# format bump, not a silent tweak; old blobs must still open under their original params.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
_HEADER_LEN = len(MAGIC) + SALT_LEN + NONCE_LEN


class DeploySecretError(Exception):
    """Named, fail-closed refusal: malformed blob, wrong password, or tampered ciphertext.
    Carries NO secret material (safe to surface/audit)."""


def _derive(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        password.encode("utf-8")
    )


def encrypt_signing_key(
    plaintext_key: bytes,
    password: str,
    *,
    salt: bytes | None = None,
    nonce: bytes | None = None,
) -> bytes:
    """Operator-side (out-of-band): produce the blob to upload to cloud storage. salt/nonce are
    injectable ONLY for deterministic tests; in real use they are fresh os.urandom."""
    if not isinstance(plaintext_key, (bytes, bytearray)) or not plaintext_key:
        raise DeploySecretError("refuse: empty/invalid plaintext key")
    if not password:
        raise DeploySecretError("refuse: empty password")
    salt = salt if salt is not None else os.urandom(SALT_LEN)
    nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
    if len(salt) != SALT_LEN or len(nonce) != NONCE_LEN:
        raise DeploySecretError("refuse: bad salt/nonce length")
    key = _derive(password, salt)
    ct = AESGCM(key).encrypt(nonce, bytes(plaintext_key), MAGIC)
    return MAGIC + salt + nonce + ct


def decrypt_signing_key(blob: bytes, password: str) -> bytes:
    """Deploy-side: open the blob with the FRESH per-deploy password. Raises DeploySecretError —
    NEVER returns a partial/wrong key — on any of: non-bytes, short/truncated, bad magic, empty
    password, wrong password, or tampered ciphertext (the last three surface as the GCM auth
    failure, indistinguishable by design)."""
    if not isinstance(blob, (bytes, bytearray)):
        raise DeploySecretError("refuse: blob is not bytes")
    if len(blob) <= _HEADER_LEN:
        raise DeploySecretError("refuse: blob too short / truncated")
    if bytes(blob[: len(MAGIC)]) != MAGIC:
        raise DeploySecretError("refuse: bad blob magic (not an AIDOCS signing-key blob)")
    if not password:
        raise DeploySecretError("refuse: empty password")
    salt = bytes(blob[len(MAGIC) : len(MAGIC) + SALT_LEN])
    nonce = bytes(blob[len(MAGIC) + SALT_LEN : _HEADER_LEN])
    ct = bytes(blob[_HEADER_LEN:])
    key = _derive(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, MAGIC)
    except InvalidTag:
        raise DeploySecretError(
            "refuse: wrong password or tampered blob (authentication failed)"
        ) from None
