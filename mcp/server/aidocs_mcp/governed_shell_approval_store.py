"""Governed-shell approval authority: read-only effective-access proof + a
privileged-broker boundary for approval secrets.

Empire re-seal 2026-05-30 (co-conductor review of WIP 838676ddaf):

1. Authority is proven by a READ-ONLY native effective-access check
   (Windows AccessCheck over the current effective token, correct
   file-vs-directory write/delete/rename masks) — NEVER by attempting a real
   write. Undecidable → fail closed.

2. Approval secrets live ONLY in a privileged BROKER (a separate service that
   owns the signing key + pending + consumed state). The secret never enters
   the agent's env or process memory. There is NO env-secret stub and NO
   same-user disk-secret fallback. Until a real broker is connected, approvals
   are UNAVAILABLE (fail closed).
"""

from __future__ import annotations

import os
import stat as _stat
from pathlib import Path
from typing import Protocol

# Specific (post-generic-mapping) rights that mean "can replace / alter /
# rename / delete" an existing object. The two low bits are object-type
# OVERLOADED on Windows — the SAME numeric value means a content-altering
# right on a FILE but a pure CREATE right on a DIRECTORY:
#
#   0x02 = FILE_WRITE_DATA (file: overwrite content)  / FILE_ADD_FILE (dir: create a NEW child file)
#   0x04 = FILE_APPEND_DATA (file: append content)    / FILE_ADD_SUBDIRECTORY (dir: create a NEW child dir)
#
# For a FILE both are genuine hijack rights (they alter the binary the chain
# resolves to). For a DIRECTORY they only permit creating a NEW sibling/child
# and CANNOT delete, rename, or replace an EXISTING provider-chain entry — so
# counting them as "writable" falsely refuses a stock-Windows root (`C:\`
# grants Authenticated Users FILE_ADD_FILE / FILE_ADD_SUBDIRECTORY by default,
# yet no unprivileged user can replace `C:\Program Files`). The directory mask
# therefore drops 0x02 / 0x04 and CONSERVATIVELY retains every right that can
# replace / rename / delete / re-permission an existing entry.
_WRITE_RIGHTS_FILE = (
    0x00000002  # FILE_WRITE_DATA (overwrite content)
    | 0x00000004  # FILE_APPEND_DATA (append content)
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
)
_WRITE_RIGHTS_DIR = (
    # 0x02 / 0x04 (FILE_ADD_FILE / FILE_ADD_SUBDIRECTORY) intentionally EXCLUDED:
    # pure child/sibling creation cannot replace an existing chain entry.
    0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD (delete/rename any existing child)
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE (delete/rename the entry itself)
    | 0x00040000  # WRITE_DAC (re-permission, then do anything)
    | 0x00080000  # WRITE_OWNER (take ownership, then re-permission)
)
# Back-compat alias (file semantics) for any external reference.
_WRITE_RIGHTS = _WRITE_RIGHTS_FILE
_MAXIMUM_ALLOWED = 0x02000000


def _hijack_write_mask(is_dir: bool) -> int:
    """The object-type-correct set of rights that let the current token
    REPLACE / rename / delete / re-permission an EXISTING entry. Directories
    drop the pure-create bits (0x02 / 0x04); files keep the full mask."""
    return _WRITE_RIGHTS_DIR if is_dir else _WRITE_RIGHTS_FILE


def _granted_is_writable(granted: int, is_dir: bool) -> bool:
    """Pure decision seam: does a granted-access mask carry any hijack-bearing
    right for an object of this type? Testable without a live Win32 token."""
    return bool(granted & _hijack_write_mask(is_dir))


def effective_access_writable(path: Path) -> bool | None:
    """READ-ONLY effective-access: can the CURRENT effective token write /
    delete / rename `path`? No writes are performed.

    Windows: AccessCheck(MAXIMUM_ALLOWED) over a duplicated impersonation
    token against the path's security descriptor, then inspect the granted
    rights for any write/delete bit. POSIX: stat — group/other-writable or
    owned-by-the-agent-uid means writable. Returns True/False, or None
    (undecidable → caller fails closed)."""
    try:
        p = path if isinstance(path, Path) else Path(path)
        if os.name != "nt":
            try:
                st = p.stat()
            except OSError:
                return None
            if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
                return True
            uid = getattr(st, "st_uid", None)
            euid = os.geteuid()
            if uid is None:
                return None
            if euid == 0:
                return False  # root: treat as authority, not "agent-writable"
            return uid == euid
        return _win_effective_writable(p)
    except Exception:
        return None


def _win_effective_writable(path: Path) -> bool | None:
    """Windows read-only AccessCheck. None on any failure (fail closed)."""
    try:
        # Object type drives which rights count as a hijack (see
        # _hijack_write_mask). Fail CLOSED when the type is undecidable
        # (missing / inaccessible / neither-or-both) — never guess.
        try:
            is_dir = path.is_dir()
            is_file = path.is_file()
        except OSError:
            return None
        if is_dir == is_file:
            # Neither (missing/undecidable) or the impossible "both".
            return None

        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Prototypes (avoid 64-bit pointer truncation).
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.DuplicateToken.restype = wintypes.BOOL
        advapi32.DuplicateToken.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.AccessCheck.restype = wintypes.BOOL
        advapi32.AccessCheck.argtypes = [
            ctypes.c_void_p,  # security descriptor
            wintypes.HANDLE,  # client token
            wintypes.DWORD,  # desired access
            ctypes.c_void_p,  # generic mapping
            ctypes.c_void_p,  # privilege set
            ctypes.POINTER(wintypes.DWORD),  # privilege set length
            ctypes.POINTER(wintypes.DWORD),  # granted access
            ctypes.POINTER(wintypes.BOOL),  # access status
        ]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        SE_FILE_OBJECT = 1
        OWNER_SI = 0x00000001
        GROUP_SI = 0x00000002
        DACL_SI = 0x00000004
        TOKEN_QUERY = 0x0008
        TOKEN_DUPLICATE = 0x0002
        SecurityImpersonation = 2

        class GENERIC_MAPPING(ctypes.Structure):
            _fields_ = [
                ("GenericRead", wintypes.DWORD),
                ("GenericWrite", wintypes.DWORD),
                ("GenericExecute", wintypes.DWORD),
                ("GenericAll", wintypes.DWORD),
            ]

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class PRIVILEGE_SET(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", wintypes.DWORD),
                ("Control", wintypes.DWORD),
                ("Privilege", LUID_AND_ATTRIBUTES * 1),
            ]

        psd = ctypes.c_void_p()
        rc = advapi32.GetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            OWNER_SI | GROUP_SI | DACL_SI,
            None,
            None,
            None,
            None,
            ctypes.byref(psd),
        )
        if rc != 0 or not psd:
            return None
        try:
            proc_token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                TOKEN_QUERY | TOKEN_DUPLICATE,
                ctypes.byref(proc_token),
            ):
                return None
            imp_token = wintypes.HANDLE()
            try:
                if not advapi32.DuplicateToken(
                    proc_token, SecurityImpersonation, ctypes.byref(imp_token)
                ):
                    return None
                mapping = GENERIC_MAPPING(
                    0x00120089,  # FILE_GENERIC_READ
                    0x00120116,  # FILE_GENERIC_WRITE
                    0x001200A0,  # FILE_GENERIC_EXECUTE
                    0x001F01FF,  # FILE_ALL_ACCESS
                )
                priv = PRIVILEGE_SET()
                priv_len = wintypes.DWORD(ctypes.sizeof(PRIVILEGE_SET))
                granted = wintypes.DWORD(0)
                status = wintypes.BOOL(0)
                ok = advapi32.AccessCheck(
                    psd,
                    imp_token,
                    _MAXIMUM_ALLOWED,
                    ctypes.byref(mapping),
                    ctypes.byref(priv),
                    ctypes.byref(priv_len),
                    ctypes.byref(granted),
                    ctypes.byref(status),
                )
                if not ok:
                    return None
                if not status.value:
                    # Token granted nothing at all → cannot write.
                    return False
                return _granted_is_writable(granted.value, is_dir)
            finally:
                if imp_token:
                    kernel32.CloseHandle(imp_token)
                if proc_token:
                    kernel32.CloseHandle(proc_token)
        finally:
            if psd:
                kernel32.LocalFree(psd)
    except Exception:
        return None


def authority_ok(path: Path) -> tuple[bool, str]:
    """One entry is authoritative iff the current effective token CANNOT
    write it (read-only effective-access). Fail closed on undecidable."""
    w = effective_access_writable(path)
    if w is None:
        return (False, f"{path}: effective-access undecidable")
    if w:
        return (False, f"{path}: current token can write (effective-access)")
    return (True, f"{path}: current token cannot write")


def chain_inclusive(leaf: Path, anchor: Path) -> list[Path]:
    """leaf and every parent UP TO AND INCLUDING `anchor`. Never the
    filesystem root (unless anchor is). Raises if leaf is not under anchor.

    Walks the GIVEN (unresolved) paths so the chain is the real on-disk chain,
    and matches the anchor by EITHER unresolved equality OR resolved equality —
    so a symlinked prefix (e.g. a symlinked /tmp on the test host) cannot make
    a genuine descendant look out-of-tree."""
    # Do NOT reconstruct via Path(...): if a test fakes os.name='nt' on POSIX,
    # Path(x) would re-dispatch to WindowsPath and raise. The inputs are
    # already Path instances; use them as-is.
    if not isinstance(leaf, Path):
        leaf = Path(leaf)
    if not isinstance(anchor, Path):
        anchor = Path(anchor)
    try:
        anchor_res = anchor.resolve()
    except Exception:
        anchor_res = anchor
    chain = [leaf]
    cur = leaf
    while True:
        if cur == anchor:
            break
        try:
            if cur.resolve() == anchor_res:
                break
        except Exception:
            pass
        parent = cur.parent
        if parent == cur:
            raise ValueError(f"{leaf} is not under anchor {anchor}")
        cur = parent
        chain.append(cur)
    return chain


# ── Privileged approval broker boundary ─────────────────────────────


class ApprovalBroker(Protocol):
    """A separate PRIVILEGED service that owns the signing key + pending +
    consumed approval state. The agent never sees the secret. A real
    implementation connects to that service (named pipe / local socket with
    OS peer auth); it does NOT read a key from agent env or disk."""

    def issue_card(self, provider_path: str, sha256: str, ttl_seconds: int) -> dict | None:
        ...

    def verify_and_consume(self, card: dict) -> tuple[bool, str]:
        ...


_BROKER: ApprovalBroker | None = None


def set_broker(broker: ApprovalBroker | None) -> None:
    """Install the privileged broker (the real service connector, or a test
    double standing in for a separate privileged process)."""
    global _BROKER
    _BROKER = broker


def get_broker() -> ApprovalBroker | None:
    """The connected privileged broker, or None. An explicitly-installed
    broker (set_broker, e.g. tests) wins; otherwise connect to a configured
    privileged broker service ($AIDOCS_APPROVAL_BROKER_ENDPOINT). No
    endpoint / not reachable / not owner-authentic → None → approvals fail
    closed (no env-secret stub, no disk-secret fallback)."""
    if _BROKER is not None:
        return _BROKER
    try:
        from . import governed_shell_broker

        return governed_shell_broker.connect_broker()
    except Exception:
        return None


def approvals_available() -> tuple[bool, str]:
    if get_broker() is None:
        return (
            False,
            "no privileged approval broker connected — approvals unavailable "
            "(fail closed; secrets never live in agent env/process/disk)",
        )
    return (True, "privileged approval broker connected")
