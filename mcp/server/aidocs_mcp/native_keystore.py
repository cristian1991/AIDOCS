"""#1074 round 6 -- local custody of the per-install native key.

The install's P-256 private key never leaves this machine and is never
written in the clear (coordinator O5: "ctypes DPAPI accepted; fail closed on
protect/unprotect failure; NEVER a plaintext fallback").

Backends
    Windows  :class:`DpapiFileBackend` -- ``CryptProtectData`` (user scope,
             app-specific entropy, no UI) over ctypes; the file holds only the
             DPAPI blob. Copying it to another machine or another Windows user
             does not decrypt. Honest limit: code running as THIS user on THIS
             machine can unprotect it -- a per-install credential identity, not
             a hardware-bound one (CNG/TPM non-exportable keys are a follow-up,
             O4).
    other    :class:`KeyringBackend` -- the OS keystore through ``keyring``
             (macOS Keychain, Secret Service). No keyring => refuse.

Slots: ``pending`` (generated before the interactive sign-in, bound into the
authorize request by its JWK thumbprint) and ``active`` (after the gate
confirmed the enrollment). The non-secret metadata (install id, key id, JKT)
sits in a small JSON file beside the operator credential cache.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .native_proof import jwk_thumbprint, public_jwk_of

#: DPAPI optional entropy: binds the blob to THIS purpose.
ENTROPY = b"aidocs-native-install/v1"
KEYRING_SERVICE = "aidocs-native-install"
SLOT_PENDING = "pending"
SLOT_ACTIVE = "active"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class KeystoreError(RuntimeError):
    """The key could not be protected / unprotected / found. Never degraded."""


# -- Windows DPAPI over ctypes ----------------------------------------------------


def _dpapi_call(fn_name: str, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise KeystoreError("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    src, _src_buf = _blob(bytes(data))
    ent, _ent_buf = _blob(ENTROPY)
    out = _Blob()
    fn = getattr(crypt32, fn_name)
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise KeystoreError(f"{fn_name} failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call("CryptProtectData", data)


def dpapi_unprotect(blob: bytes) -> bytes:
    return _dpapi_call("CryptUnprotectData", blob)


class DpapiFileBackend:
    """One DPAPI blob per slot under ``directory``. Never writes clear bytes."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory else Path.home() / ".aidocs" / "native_install"

    def _path(self, slot: str) -> Path:
        return self.directory / f"{slot}.bin"

    def put(self, slot: str, data: bytes) -> None:
        blob = dpapi_protect(bytes(data))  # raises => nothing is written
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(slot)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(blob)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def get(self, slot: str) -> bytes | None:
        path = self._path(slot)
        if not path.exists():
            return None
        return dpapi_unprotect(path.read_bytes())

    def delete(self, slot: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(slot).unlink()


class KeyringBackend:
    """The OS keystore through ``keyring``. Unavailable => KeystoreError."""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service

    def _kr(self):
        try:
            import keyring  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise KeystoreError("no OS keystore (keyring) is available") from exc
        if keyring is None:
            raise KeystoreError("no OS keystore (keyring) is available")
        return keyring

    def put(self, slot: str, data: bytes) -> None:
        try:
            self._kr().set_password(self.service, slot, base64.b64encode(bytes(data)).decode("ascii"))
        except KeystoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise KeystoreError(f"keystore write failed: {type(exc).__name__}") from exc

    def get(self, slot: str) -> bytes | None:
        try:
            raw = self._kr().get_password(self.service, slot)
        except KeystoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise KeystoreError(f"keystore read failed: {type(exc).__name__}") from exc
        return base64.b64decode(raw) if raw else None

    def delete(self, slot: str) -> None:
        with contextlib.suppress(Exception):
            self._kr().delete_password(self.service, slot)


class NativeInstallUnsupported(KeystoreError):
    """This platform has no PACKAGED OS keystore for the install key (#1074 r6
    Phase B). Refused explicitly at sign-in -- never a plaintext fallback."""


def native_install_supported(platform: str | None = None) -> bool:
    """Windows (DPAPI) is the only packaged keystore today. ``keyring`` is not
    shipped, so macOS / Linux refuse rather than half-work on a dev box that
    happens to have it installed."""
    return (platform or sys.platform) == "win32"


def unsupported_message(platform: str | None = None) -> str:
    return f"native install credential not supported on {platform or sys.platform} yet"


def default_backend():
    # Phase B judgment call: refuse off Windows. Flip this to KeyringBackend()
    # only once keyring is packaged with the install.
    if not native_install_supported():
        raise NativeInstallUnsupported(unsupported_message())
    return DpapiFileBackend()


def default_meta_path() -> Path:
    from .operator_token_resolution import default_cache_path

    return default_cache_path().with_name("native_install.json")


# -- the keystore ------------------------------------------------------------


@dataclass(frozen=True)
class ActiveKey:
    install_id: str
    key_id: str
    jkt: str
    private_key: ec.EllipticCurvePrivateKey


def _der(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _load(der: bytes) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_der_private_key(der, password=None)
    except Exception as exc:  # noqa: BLE001
        raise KeystoreError("stored key is unreadable") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise KeystoreError("stored key is not a P-256 key")
    return key


class NativeKeystore:
    def __init__(self, backend=None, meta_path: str | Path | None = None) -> None:
        self.backend = backend if backend is not None else default_backend()
        self.meta_path = Path(meta_path) if meta_path else default_meta_path()

    def _read_meta(self) -> dict:
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_meta(self, data: dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_name(self.meta_path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def generate_pending(self) -> dict:
        """A NEW key in the pending slot. Returns ``{jkt, public_jwk}`` -- the
        only things that ever leave this process."""
        key = ec.generate_private_key(ec.SECP256R1())
        self.backend.put(SLOT_PENDING, _der(key))
        jwk = public_jwk_of(key)
        meta = self._read_meta()
        meta["pending_jkt"] = jwk_thumbprint(jwk)
        self._write_meta(meta)
        return {"jkt": meta["pending_jkt"], "public_jwk": jwk}

    def pending_private_key(self) -> ec.EllipticCurvePrivateKey | None:
        der = self.backend.get(SLOT_PENDING)
        return _load(der) if der else None

    def discard_pending(self) -> None:
        self.backend.delete(SLOT_PENDING)
        meta = self._read_meta()
        if meta.pop("pending_jkt", None) is not None:
            self._write_meta(meta)

    def activate_pending(self, install_id: str, key_id: str) -> None:
        der = self.backend.get(SLOT_PENDING)
        if not der:
            raise KeystoreError("no pending key to activate")
        key = _load(der)
        self.backend.put(SLOT_ACTIVE, der)
        self.backend.delete(SLOT_PENDING)
        self._write_meta({
            "install_id": str(install_id),
            "key_id": str(key_id),
            "jkt": jwk_thumbprint(public_jwk_of(key)),
        })

    def active(self) -> ActiveKey | None:
        meta = self._read_meta()
        iid, kid = str(meta.get("install_id") or ""), str(meta.get("key_id") or "")
        if not iid or not kid:
            return None
        der = self.backend.get(SLOT_ACTIVE)
        if not der:
            return None
        key = _load(der)
        jkt = jwk_thumbprint(public_jwk_of(key))
        if jkt != meta.get("jkt"):
            raise KeystoreError("active key does not match its recorded thumbprint")
        return ActiveKey(install_id=iid, key_id=kid, jkt=jkt, private_key=key)

    def clear_active(self) -> None:
        self.backend.delete(SLOT_ACTIVE)
        meta = self._read_meta()
        for k in ("install_id", "key_id", "jkt"):
            meta.pop(k, None)
        self._write_meta(meta)

    # -- R6-P1: which key the NEXT enrollment exchange proves -----------------
    #
    # A fresh interactive sign-in on a healthy install RE-AUTHORIZES that same
    # install: it presents the ACTIVE key's JKT, so the gate reuses the install
    # id and key id and mints a new token family for it. (Re-auth is not a
    # logout and not a key rotation: the key and the install are unchanged.)
    # Only an explicit new install -- none active, the active key missing or
    # unreadable, the install revoked, or the operator asking -- uses a NEW
    # pending key. The choice is recorded here and read back by the exchange;
    # the key itself never leaves this object's callers' process, and the only
    # thing it is used for there is the code-bound enrollment proof.

    def begin_reauth(self) -> dict | None:
        """Mark the ACTIVE key as the one to prove at the next exchange.
        Returns ``{jkt, public_jwk}``, or None when no active key exists.
        Raises :class:`KeystoreError` when the active key is unreadable."""
        act = self.active()
        if act is None:
            return None
        meta = self._read_meta()
        meta["enroll_with"] = SLOT_ACTIVE
        meta["enroll_jkt"] = act.jkt
        self._write_meta(meta)
        return {"jkt": act.jkt, "public_jwk": public_jwk_of(act.private_key)}

    def begin_new_install(self) -> dict:
        """A NEW pending key for an explicit new install."""
        out = self.generate_pending()
        meta = self._read_meta()
        meta["enroll_with"] = SLOT_PENDING
        meta["enroll_jkt"] = out["jkt"]
        self._write_meta(meta)
        return out

    def enrollment_key(self) -> tuple[ec.EllipticCurvePrivateKey | None, str]:
        """``(key, mode)`` for the enrollment the last ``begin_*`` prepared;
        mode is ``"reauth"`` or ``"new_install"``; ``(None, "")`` when none."""
        meta = self._read_meta()
        want, jkt = meta.get("enroll_with"), meta.get("enroll_jkt")
        if want == SLOT_ACTIVE:
            act = self.active()
            if act is None or act.jkt != jkt:
                return None, ""
            return act.private_key, "reauth"
        if want == SLOT_PENDING:
            key = self.pending_private_key()
            if key is None or jwk_thumbprint(public_jwk_of(key)) != jkt:
                return None, ""
            return key, "new_install"
        return None, ""

    def _clear_enrollment_marks(self) -> None:
        meta = self._read_meta()
        marks = [k for k in ("enroll_with", "enroll_jkt") if k in meta]
        for k in marks:
            meta.pop(k)
        if marks:
            self._write_meta(meta)

    def complete_enrollment(self, mode: str, install_id: str, key_id: str) -> None:
        """Record the gate's confirmation. A re-auth must come back with the
        SAME ids (the gate reused the install); anything else is refused and
        the active key is left exactly as it was."""
        if mode == "new_install":
            self.activate_pending(install_id, key_id)
            return
        if mode == "reauth":
            act = self.active()
            if act is None or (act.install_id, act.key_id) != (str(install_id), str(key_id)):
                raise KeystoreError("re-auth was not confirmed for this install")
            self._clear_enrollment_marks()
            return
        raise KeystoreError("no enrollment in progress")

    def abandon_enrollment(self) -> None:
        """Drop a pending new-install key; NEVER touches the active key."""
        self.discard_pending()
        self._clear_enrollment_marks()
