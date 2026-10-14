"""ai_deploy signing-blob source — pluggable retrieval of the encrypted signing-key blob.

The dashboard sign-flow needs the password-sealed signing-key blob at sign time. WHERE it lives is
pluggable: a LOCAL FILE (the VPS-resident flow + testing) today, or a Google-Drive-OAuth fetch in
production. The blob is always the AES-256-GCM password-sealed key (ai_deploy_secret) — this layer
ONLY retrieves the bytes; it never holds the operator password or the plaintext key, and it never
caches the blob to disk. Fail-closed: any retrieval problem raises BlobSourceError (the sign aborts,
nothing is signed).

Swapping to Drive is a config change, not a code change at the call sites: set
AIDOCS_DEPLOY_BLOB_SOURCE=drive once the operator provisions the Google OAuth client; until then the
default 'file' source serves a locally-sealed blob (deploy_signflow.py seal).
"""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_FILE = "file"
SOURCE_DRIVE = "drive"

_ENV_SOURCE = "AIDOCS_DEPLOY_BLOB_SOURCE"
_ENV_PATH = "AIDOCS_DEPLOY_BLOB_PATH"


class BlobSourceError(Exception):
    """Named, fail-closed retrieval failure (missing config / file / empty / unprovisioned)."""


def fetch_blob(*, source: str = "", path: str = "") -> bytes:
    """Retrieve the encrypted signing-key blob bytes. ``source`` defaults to $AIDOCS_DEPLOY_BLOB_SOURCE
    or 'file'. For 'file', reads ``path`` or $AIDOCS_DEPLOY_BLOB_PATH. 'drive' is the Google-Drive-
    OAuth provider, wired when the operator provisions the OAuth client. Raises BlobSourceError on any
    failure — the caller MUST treat that as 'do not sign'."""
    src = (source or os.environ.get(_ENV_SOURCE) or SOURCE_FILE).strip().lower()
    if src == SOURCE_FILE:
        p = path or os.environ.get(_ENV_PATH, "")
        if not p:
            raise BlobSourceError(f"no blob path configured (set {_ENV_PATH} or pass path=)")
        fp = Path(p)
        if not fp.is_file():
            raise BlobSourceError(f"blob file not found: {p}")
        data = fp.read_bytes()
        if not data:
            raise BlobSourceError(f"blob file is empty: {p}")
        return data
    if src == SOURCE_DRIVE:
        raise BlobSourceError(
            "drive blob source not yet provisioned — needs the operator's Google OAuth client "
            "(set it up, then AIDOCS_DEPLOY_BLOB_SOURCE=drive)"
        )
    raise BlobSourceError(f"unknown blob source {src!r}")
