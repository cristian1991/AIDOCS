"""Windows named-pipe transport for the governed-shell approval broker, with
TOKEN peer authentication (pure ctypes — no pywin32).

  * Server: CreateNamedPipe loop; per connection, resolve the CLIENT's SID via
    GetNamedPipeClientProcessId → OpenProcess → token → TokenUser, and serve
    only an allowed SID.
  * Client: after CreateFile, authenticate the SERVER by the pipe's OWNER SID
    (GetSecurityInfo) against the ONE provisioned principal SID.

Length-prefixed JSON, same shape as the POSIX transport. Everything is wrapped
fail-closed; any ctypes error → refusal / None.
"""

from __future__ import annotations

import json
import struct

PIPE_PREFIX = r"\\.\pipe"

# Win32 constants.
_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE = -1
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_BUSY = 231
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TokenUser = 1
_SE_KERNEL_OBJECT = 6
_OWNER_SECURITY_INFORMATION = 0x00000001
_MAX_REQUEST = 1_000_000
_CONN_TIMEOUT = 10.0


_DLLS = None


def _dlls():
    """Cached (kernel32, advapi32) with prototypes set — REQUIRED so 64-bit
    handles/pointers are not truncated to 32-bit ints (the classic ctypes bug
    that silently returns NULL)."""
    global _DLLS
    if _DLLS is not None:
        return _DLLS
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    H = wintypes.HANDLE
    DW = wintypes.DWORD
    BOOL = wintypes.BOOL
    LPV = wintypes.LPVOID
    PDW = ctypes.POINTER(wintypes.DWORD)
    PH = ctypes.POINTER(wintypes.HANDLE)
    PV = ctypes.POINTER(ctypes.c_void_p)

    k.GetCurrentProcess.restype = H
    k.GetCurrentProcess.argtypes = []
    k.CloseHandle.restype = BOOL
    k.CloseHandle.argtypes = [H]
    k.LocalFree.restype = H
    k.LocalFree.argtypes = [H]
    k.OpenProcess.restype = H
    k.OpenProcess.argtypes = [DW, BOOL, DW]
    k.CreateNamedPipeW.restype = H
    k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, DW, DW, DW, DW, DW, DW, LPV]
    k.ConnectNamedPipe.restype = BOOL
    k.ConnectNamedPipe.argtypes = [H, LPV]
    k.DisconnectNamedPipe.restype = BOOL
    k.DisconnectNamedPipe.argtypes = [H]
    k.FlushFileBuffers.restype = BOOL
    k.FlushFileBuffers.argtypes = [H]
    k.CreateFileW.restype = H
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, DW, DW, LPV, DW, DW, H]
    k.ReadFile.restype = BOOL
    k.ReadFile.argtypes = [H, LPV, DW, PDW, LPV]
    k.WriteFile.restype = BOOL
    k.WriteFile.argtypes = [H, LPV, DW, PDW, LPV]
    k.GetNamedPipeClientProcessId.restype = BOOL
    k.GetNamedPipeClientProcessId.argtypes = [H, PDW]
    k.WaitNamedPipeW.restype = BOOL
    k.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, DW]

    a.OpenProcessToken.restype = BOOL
    a.OpenProcessToken.argtypes = [H, DW, PH]
    a.GetTokenInformation.restype = BOOL
    a.GetTokenInformation.argtypes = [H, ctypes.c_int, LPV, DW, PDW]
    a.ConvertSidToStringSidW.restype = BOOL
    a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    a.GetSecurityInfo.restype = DW
    a.GetSecurityInfo.argtypes = [H, ctypes.c_int, DW, PV, PV, PV, PV, PV]
    # Impersonation token-binding (no PID lookup).
    a.ImpersonateNamedPipeClient.restype = BOOL
    a.ImpersonateNamedPipeClient.argtypes = [H]
    a.OpenThreadToken.restype = BOOL
    a.OpenThreadToken.argtypes = [H, DW, BOOL, PH]
    a.RevertToSelf.restype = BOOL
    a.RevertToSelf.argtypes = []
    k.GetCurrentThread.restype = H
    k.GetCurrentThread.argtypes = []
    # SDDL helpers for explicit DACL creation + ACL proof.
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, DW, PV, PV,
    ]
    a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = BOOL
    a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, DW, DW, ctypes.POINTER(ctypes.c_wchar_p), PV,
    ]
    a.GetNamedSecurityInfoW.restype = DW
    a.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, DW, PV, PV, PV, PV, PV,
    ]
    # #502: writing a protected DACL onto an ALREADY-EXISTING path.
    # create_private_dir() can only give a NEW directory its ACL; a state
    # tree that predates the hardening (every installed host) needs the
    # DACL replaced in place, with inheritance cut, or it keeps whatever
    # ACEs it inherited from the profile.
    a.SetNamedSecurityInfoW.restype = DW
    a.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, DW, LPV, LPV, LPV, LPV,
    ]
    a.GetSecurityDescriptorDacl.restype = BOOL
    a.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(BOOL), PV, ctypes.POINTER(BOOL),
    ]
    k.CreateDirectoryW.restype = BOOL
    k.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, LPV]
    k.CancelIoEx.restype = BOOL
    k.CancelIoEx.argtypes = [H, LPV]

    _DLLS = (k, a)
    return _DLLS


def _k32():
    return _dlls()[0]


def _a32():
    return _dlls()[1]


def _full_pipe_name(endpoint: str) -> str:
    # Accept either a bare name or a full \\.\pipe\... path.
    if endpoint.lower().startswith("\\\\"):
        return endpoint
    return f"{PIPE_PREFIX}\\{endpoint}"


def _write_msg(handle, obj: dict) -> bool:
    import ctypes
    from ctypes import wintypes

    k = _k32()
    data = _pack(obj)
    written = wintypes.DWORD(0)
    ok = k.WriteFile(handle, data, len(data), ctypes.byref(written), None)
    return bool(ok) and written.value == len(data)


def _read_n(handle, n: int):
    import ctypes
    from ctypes import wintypes

    k = _k32()
    got = wintypes.DWORD(0)
    out = b""
    remaining = n
    while remaining > 0:
        chunk = ctypes.create_string_buffer(remaining)
        ok = k.ReadFile(handle, chunk, remaining, ctypes.byref(got), None)
        if not ok or got.value == 0:
            return None
        out += chunk.raw[: got.value]
        remaining -= got.value
        if len(out) > _MAX_REQUEST:
            return None
    return out


def _pack(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("!I", len(body)) + body


def _read_msg(handle):
    head = _read_n(handle, 4)
    if head is None:
        return None
    (n,) = struct.unpack("!I", head)
    if n <= 0 or n > _MAX_REQUEST:
        return None
    body = _read_n(handle, n)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _client_sid_of_pipe(handle) -> str | None:
    """The connecting client's SID via GetNamedPipeClientProcessId → token."""
    import ctypes
    from ctypes import wintypes

    k = _k32()
    a = _a32()
    try:
        pid = wintypes.DWORD(0)
        if not k.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
            return None
        hproc = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return None
        try:
            htok = wintypes.HANDLE()
            if not a.OpenProcessToken(hproc, _TOKEN_QUERY, ctypes.byref(htok)):
                return None
            try:
                length = wintypes.DWORD(0)
                a.GetTokenInformation(htok, _TokenUser, None, 0, ctypes.byref(length))
                buf = ctypes.create_string_buffer(length.value)
                if not a.GetTokenInformation(htok, _TokenUser, buf, length, ctypes.byref(length)):
                    return None
                # TOKEN_USER { SID_AND_ATTRIBUTES User { PSID Sid; DWORD Attrs } }
                psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                strsid = ctypes.c_wchar_p()
                if not a.ConvertSidToStringSidW(psid, ctypes.byref(strsid)):
                    return None
                try:
                    return strsid.value
                finally:
                    k.LocalFree(strsid)
            finally:
                k.CloseHandle(htok)
        finally:
            k.CloseHandle(hproc)
    except Exception:
        return None


def _client_sid_impersonated(handle) -> str | None:
    """The connecting client's SID via ImpersonateNamedPipeClient + the
    impersonation TOKEN (token-binding, NOT a racy PID lookup)."""
    import ctypes
    from ctypes import wintypes

    k = _k32()
    a = _a32()
    try:
        if not a.ImpersonateNamedPipeClient(handle):
            return None
        try:
            htok = wintypes.HANDLE()
            if not a.OpenThreadToken(k.GetCurrentThread(), _TOKEN_QUERY, True, ctypes.byref(htok)):
                return None
            try:
                length = wintypes.DWORD(0)
                a.GetTokenInformation(htok, _TokenUser, None, 0, ctypes.byref(length))
                buf = ctypes.create_string_buffer(length.value)
                if not a.GetTokenInformation(htok, _TokenUser, buf, length, ctypes.byref(length)):
                    return None
                psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                strsid = ctypes.c_wchar_p()
                if not a.ConvertSidToStringSidW(psid, ctypes.byref(strsid)):
                    return None
                try:
                    return strsid.value
                finally:
                    k.LocalFree(strsid)
            finally:
                k.CloseHandle(htok)
        finally:
            a.RevertToSelf()
    except Exception:
        try:
            a.RevertToSelf()
        except Exception:
            pass
        return None


def create_private_dir(path: str) -> bool:
    """Create a directory with an EXPLICIT, protected DACL granting full
    control ONLY to the owner, SYSTEM, and BUILTIN\\Administrators (no
    inheritance, no Users/Everyone). Returns True on create or if it already
    exists (authority is proven separately)."""
    import ctypes
    from ctypes import wintypes

    k = _k32()
    a = _a32()
    sddl = "D:PAI(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    psd = ctypes.c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(psd), None
    ):
        return False
    try:

        class SA(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        sa = SA(ctypes.sizeof(SA), psd, False)
        ok = k.CreateDirectoryW(path, ctypes.byref(sa))
        if not ok and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return True
        return bool(ok)
    finally:
        k.LocalFree(psd)


#: Protected DACLs granting full control to the OWNER, SYSTEM and
#: BUILTIN\\Administrators and to nobody else. "P" cuts inheritance, so a
#: permissive ancestor cannot re-grant write through an inherited ACE.
_PRIVATE_DIR_SDDL = "D:PAI(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
_PRIVATE_FILE_SDDL = "D:PAI(A;;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)"


def harden_path_dacl(path: str, *, is_dir: bool) -> bool:
    """Replace an EXISTING path's DACL with the private, protected one.

    Companion to :func:`create_private_dir`, which can only shape a
    directory it creates. Hosts that already have the state tree need the
    ACL rewritten in place — otherwise every registration keeps whatever
    write ACEs it inherited from the user profile, which is exactly the
    custody hole #502 is about. Returns True only on a proven success;
    every ctypes failure is a False (the caller then refuses).
    """
    import ctypes
    from ctypes import wintypes

    try:
        a = _a32()
        k = _k32()
        sddl = _PRIVATE_DIR_SDDL if is_dir else _PRIVATE_FILE_SDDL
        psd = ctypes.c_void_p()
        if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(psd), None
        ):
            return False
        try:
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            pdacl = ctypes.c_void_p()
            if not a.GetSecurityDescriptorDacl(
                psd,
                ctypes.byref(present),
                ctypes.byref(pdacl),
                ctypes.byref(defaulted),
            ):
                return False
            if not present:
                return False
            # SE_FILE_OBJECT=1; DACL_SECURITY_INFORMATION=0x4 with
            # PROTECTED_DACL_SECURITY_INFORMATION=0x80000000.
            rc = a.SetNamedSecurityInfoW(
                ctypes.create_unicode_buffer(str(path)),
                1,
                0x4 | 0x80000000,
                None,
                None,
                pdacl,
                None,
            )
            return rc == 0
        finally:
            k.LocalFree(psd)
    except Exception:
        return False


# Trustees that MAY hold a write-granting ACE on broker state (the POSITIVE
# allowlist): SYSTEM, BUILTIN\Administrators, the OWNER, CREATOR OWNER. The
# provisioned service principal's literal SID is added dynamically.
_ALLOWED_TRUSTEE_ABBREV = {"SY", "BA", "OW", "CO", "LA"}
# SDDL right tokens (or hex masks) that confer the ability to mutate.
_WRITE_RIGHT_ABBREV = {
    "FA", "FW", "GA", "GW", "KA", "WD", "WO", "DC", "DE", "CC", "SD", "WP",
}
_WRITE_MASK = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)  # deliberately NOT READ_CONTROL (0x20000) / SYNCHRONIZE (0x100000)


def _dacl_sddl(path: str) -> str | None:
    import ctypes

    a = _a32()
    k = _k32()
    DACL_SI = 0x00000004
    try:
        psd = ctypes.c_void_p()
        rc = a.GetNamedSecurityInfoW(path, 1, DACL_SI, None, None, None, None, ctypes.byref(psd))
        if rc != 0 or not psd:
            return None
        try:
            out = ctypes.c_wchar_p()
            if not a.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                psd, 1, DACL_SI, ctypes.byref(out), None
            ):
                return None
            try:
                return out.value or ""
            finally:
                k.LocalFree(out)
        finally:
            k.LocalFree(psd)
    except Exception:
        return None


def _ace_grants_write(rights: str) -> bool:
    r = rights.upper()
    if r.startswith("0X"):
        try:
            return bool(int(r, 16) & _WRITE_MASK)
        except Exception:
            return True  # unparseable mask → assume write (fail closed)
    return any(tok in _WRITE_RIGHT_ABBREV for tok in (r[i : i + 2] for i in range(0, len(r), 2)))


def _owner_sid(path: str) -> str | None:
    """The object's ACTUAL owner SID (GetNamedSecurityInfo OWNER). None on
    failure (caller fails closed)."""
    import ctypes

    a = _a32()
    k = _k32()
    try:
        ppsid = ctypes.c_void_p()
        ppsd = ctypes.c_void_p()
        rc = a.GetNamedSecurityInfoW(path, 1, 0x1, ctypes.byref(ppsid), None, None, None, ctypes.byref(ppsd))
        if rc != 0 or not ppsid:
            return None
        try:
            s = ctypes.c_wchar_p()
            if not a.ConvertSidToStringSidW(ppsid, ctypes.byref(s)):
                return None
            try:
                return s.value
            finally:
                k.LocalFree(s)
        finally:
            if ppsd:
                k.LocalFree(ppsd)
    except Exception:
        return None


# Well-known system principal SIDs that may own / write broker state.
_SYSTEM_SIDS = {"S-1-5-18", "S-1-5-32-544"}  # LocalSystem, BUILTIN\Administrators


def acl_state_authority_ok(path: str, service_sid: str | None = None) -> bool:
    """POSITIVE ACL PROOF with OWNER BINDING. First RESOLVE the object's actual
    owner SID and require it to be the PROVISIONED service principal, SYSTEM, or
    Administrators — an attacker-owned tree is refused regardless of its ACEs,
    and OWNER/CREATOR OWNER (OW/CO) ACEs are honored ONLY because the owner has
    been proven trusted (never accepted blind). Then require EVERY write-
    granting Allow ACE to name only that trusted set. Fail closed."""
    import re

    sddl = _dacl_sddl(path)
    if not sddl:
        return False
    principal = (service_sid or current_user_sid() or "").upper()
    trusted = ({principal} | _SYSTEM_SIDS) if principal else set(_SYSTEM_SIDS)
    # OWNER BINDING: resolve who actually owns the object; refuse if it is not
    # a trusted principal (so OW/CO ACEs cannot launder an attacker owner).
    owner = (_owner_sid(path) or "").upper()
    if owner not in trusted:
        return False
    allowed_sids = trusted
    # The DACL portion is "D:<flags>(ace)(ace)...".
    dacl = sddl
    if "D:" in sddl:
        dacl = sddl.split("D:", 1)[1].split("S:", 1)[0]
    for ace in re.findall(r"\(([^)]*)\)", dacl):
        parts = ace.split(";")
        if len(parts) < 6:
            return False  # unparseable ACE → fail closed
        atype, _flags, rights, _o, _i, trustee = parts[:6]
        if atype.upper().startswith("D"):
            continue  # deny ACEs only restrict
        if not _ace_grants_write(rights):
            continue
        t = trustee.strip().upper()
        if t in _ALLOWED_TRUSTEE_ABBREV or t in allowed_sids:
            continue
        return False  # write ACE to a non-allowlisted trustee
    return True


def path_acl_operator_only(path: str) -> bool:
    """A config file is operator-only iff (a) it is NOT a reparse point
    (no redirect) AND (b) the CURRENT (agent) token cannot write it."""
    try:
        if is_reparse_point(path):
            return False
        from pathlib import Path

        from .governed_shell_approval_store import effective_access_writable

        return effective_access_writable(Path(path)) is False
    except Exception:
        return False


def _parents_inclusive(path: str):
    from pathlib import Path

    cur = Path(path)
    while True:
        yield cur
        if str(cur) == str(cur.parent):
            break
        cur = cur.parent


def win_config_chain_secure(path: str) -> bool:
    """Config file + FULL parent chain: no reparse point anywhere, and the
    CURRENT (agent) token cannot write any entry. Reject an existing insecure
    tree. Fail closed."""
    from pathlib import Path

    from .governed_shell_approval_store import effective_access_writable

    for entry in _parents_inclusive(path):
        if is_reparse_point(str(entry)):
            return False
        if effective_access_writable(Path(entry)) is not False:  # True/None → refuse
            return False
    return True


def win_state_chain_secure(path: str, service_sid: str | None = None) -> bool:
    """Broker-state dir + FULL parent chain must be NON-AGENT-WRITABLE: no
    reparse point anywhere (junction/symlink redirect defense) AND every entry's
    owner + write-granting ACEs bind ONLY to the provisioned service principal /
    SYSTEM / Administrators (positive DACL + owner binding). A writable ancestor
    (which could rename/redirect the tree) → fail closed. Refuses an existing
    insecure tree."""
    for entry in _parents_inclusive(path):
        if is_reparse_point(str(entry)):
            return False
        if not acl_state_authority_ok(str(entry), service_sid):
            return False
    return True


def is_reparse_point(path: str) -> bool:
    """True if `path` is a reparse point (symlink/junction) — used to REFUSE a
    redirect rather than follow it. Fail closed (True) on error."""
    import ctypes

    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        from ctypes import wintypes

        k.GetFileAttributesW.restype = wintypes.DWORD
        k.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        attrs = k.GetFileAttributesW(path)
        if attrs == 0xFFFFFFFF:
            return True  # cannot stat → refuse
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except Exception:
        return True


# FILE_GENERIC_READ|FILE_GENERIC_WRITE minus FILE_CREATE_PIPE_INSTANCE (0x4):
# the IPC rights a CLIENT needs to connect+talk, but NOT to create a pipe
# instance or change the ACL.
_PIPE_CLIENT_RIGHTS = 0x0012019B


def _pipe_sddl(allowed_sids) -> str:
    """Least-privilege pipe DACL (protected): OWNER/SYSTEM/Admins full control;
    each provisioned agent SID gets ONLY the client IPC connect rights."""
    aces = ["(A;;FA;;;OW)", "(A;;FA;;;SY)", "(A;;FA;;;BA)"]
    for sid in allowed_sids:
        s = str(sid).strip()
        if s:
            aces.append(f"(A;;0x{_PIPE_CLIENT_RIGHTS:08X};;;{s})")
    return "D:P" + "".join(aces)


def _make_sa(sddl: str):
    """Build a SECURITY_ATTRIBUTES from an SDDL string, or (None, None)."""
    import ctypes
    from ctypes import wintypes

    a = _a32()
    psd = ctypes.c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(psd), None
    ):
        return (None, None)

    class SA(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    return (SA(ctypes.sizeof(SA), psd, False), psd)


def _known_folder_programdata() -> str | None:
    """The REAL ProgramData from the OS (SHGetFolderPathW), NOT the mutable
    %ProgramData% env var."""
    import ctypes
    from ctypes import wintypes

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        shell32.SHGetFolderPathW.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ]
        buf = ctypes.create_unicode_buffer(260)
        if shell32.SHGetFolderPathW(None, 0x23, None, 0, buf) == 0:  # CSIDL_COMMON_APPDATA
            return buf.value or None
    except Exception:
        return None
    return None


def installed_config_path() -> str | None:
    """Resolve the broker config path from an OS-derived, operator-controlled
    source: HKLM\\SOFTWARE\\AIDOCS\\GovernedShell\\ConfigPath (admin-only to
    set), else the known-folder ProgramData (OS API). NEVER the mutable env."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\AIDOCS\GovernedShell"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "ConfigPath")
            if val:
                return str(val)
    except Exception:
        pass
    pd = _known_folder_programdata()
    if pd:
        from pathlib import Path

        return str(Path(pd) / "AIDOCS" / "governed_shell" / "broker.json")
    return None


def _server_owner_sid_of_pipe(handle) -> str | None:
    """The pipe OWNER SID (the server that created it) via GetSecurityInfo."""
    import ctypes

    a = _a32()
    k = _k32()
    try:
        ppsid = ctypes.c_void_p()
        ppsd = ctypes.c_void_p()
        rc = a.GetSecurityInfo(
            handle,
            _SE_KERNEL_OBJECT,
            _OWNER_SECURITY_INFORMATION,
            ctypes.byref(ppsid),
            None,
            None,
            None,
            ctypes.byref(ppsd),
        )
        if rc != 0 or not ppsid:
            return None
        try:
            strsid = ctypes.c_wchar_p()
            if not a.ConvertSidToStringSidW(ppsid, ctypes.byref(strsid)):
                return None
            try:
                return strsid.value
            finally:
                k.LocalFree(strsid)
        finally:
            if ppsd:
                k.LocalFree(ppsd)
    except Exception:
        return None


def current_user_sid() -> str | None:
    """The current process user's SID string (the service's own principal)."""
    import ctypes
    from ctypes import wintypes

    k = _k32()
    a = _a32()
    try:
        htok = wintypes.HANDLE()
        if not a.OpenProcessToken(k.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(htok)):
            return None
        try:
            length = wintypes.DWORD(0)
            a.GetTokenInformation(htok, _TokenUser, None, 0, ctypes.byref(length))
            buf = ctypes.create_string_buffer(length.value)
            if not a.GetTokenInformation(htok, _TokenUser, buf, length, ctypes.byref(length)):
                return None
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            strsid = ctypes.c_wchar_p()
            if not a.ConvertSidToStringSidW(psid, ctypes.byref(strsid)):
                return None
            try:
                return strsid.value
            finally:
                k.LocalFree(strsid)
        finally:
            k.CloseHandle(htok)
    except Exception:
        return None


def serve(pipe_name: str, broker, allowed_sids, *, one_shot=False):
    """Named-pipe server with TOKEN peer-auth: only an allowed client SID is
    served. Length-prefixed JSON, same ops as POSIX."""
    import ctypes

    from .governed_shell_broker import BrokerStateError as _BrokerStateError
    from .governed_shell_broker import _handle_dict

    # EXPLICIT least-privilege pipe security descriptor, constructed FIRST and
    # FAIL-CLOSED: OWNER/SYSTEM/Admins retain full control (FA); each
    # provisioned agent SID gets ONLY the IPC connect rights (read+write, NOT
    # create-instance / change-control). If the SD cannot be constructed we
    # REFUSE to serve — never fall back to a default (None) DACL.
    sa, _psd = _make_sa(_pipe_sddl(allowed_sids))
    if sa is None:
        raise _BrokerStateError(
            "named-pipe security descriptor construction failed — refusing to "
            "serve (no default-DACL fallback)"
        )
    sa_ref = ctypes.byref(sa)
    k = _k32()
    full = _full_pipe_name(pipe_name)
    allowed = {s.upper() for s in allowed_sids}
    while True:
        handle = k.CreateNamedPipeW(
            ctypes.c_wchar_p(full),
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            0,
            sa_ref,
        )
        if handle is None or ctypes.c_void_p(handle).value == ctypes.c_void_p(_INVALID_HANDLE).value:
            return
        connected = k.ConnectNamedPipe(handle, None)
        if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
            k.CloseHandle(handle)
            if one_shot:
                return
            continue
        # Bounded I/O: a watchdog cancels pending I/O + disconnects if the peer
        # stalls, so a slow/stalled client cannot wedge the single-instance
        # service.
        import threading

        wedged = threading.Event()

        # Bind THIS connection's event+handle at def time: a late-scheduled
        # watchdog must never look up the loop variables after the next
        # iteration rebinds them, or it would cancel a healthy new connection
        # while the wedged one it was armed for leaks (B023).
        def _watchdog(wedged=wedged, handle=handle):
            if not wedged.wait(_CONN_TIMEOUT):
                try:
                    k.CancelIoEx(handle, None)
                    k.DisconnectNamedPipe(handle)
                except Exception:
                    pass

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()
        try:
            # Read FIRST — ImpersonateNamedPipeClient requires data to have
            # been read from the pipe (ERROR_CANNOT_IMPERSONATE otherwise).
            req = _read_msg(handle)
            # TOKEN-BINDING peer auth (impersonation), NOT a racy PID lookup.
            sid = _client_sid_impersonated(handle)
            if sid is None or sid.upper() not in allowed:
                _write_msg(handle, {"ok": False, "reason": "peer not authorized"})
            elif not isinstance(req, dict):
                _write_msg(handle, {"ok": False, "reason": "bad request"})
            else:
                _write_msg(handle, _handle_dict(broker, req))
        except Exception:
            pass
        finally:
            wedged.set()
            try:
                k.FlushFileBuffers(handle)
            except Exception:
                pass
            k.DisconnectNamedPipe(handle)
            k.CloseHandle(handle)
        if one_shot:
            return


class WindowsPipeBrokerClient:
    """ApprovalBroker proxy over a named pipe. Authenticates the SERVER by the
    pipe OWNER SID against the provisioned principal SID after connect."""

    def __init__(self, pipe_name: str, principal_sid: str):
        self._name = _full_pipe_name(pipe_name)
        self._principal = principal_sid.upper()

    def _rpc(self, req: dict) -> dict:
        import ctypes
        import time as _t

        k = _k32()
        last = {"ok": False, "reason": "broker pipe unreachable"}
        for _attempt in range(20):
            handle = k.CreateFileW(
                ctypes.c_wchar_p(self._name),
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                0,
                None,
            )
            invalid = (handle == _INVALID_HANDLE) or (handle in (0, None)) or (
                ctypes.c_void_p(handle).value == ctypes.c_void_p(_INVALID_HANDLE).value
            )
            if invalid:
                err = ctypes.get_last_error()
                if err == _ERROR_PIPE_BUSY:
                    k.WaitNamedPipeW(ctypes.c_wchar_p(self._name), 2000)
                    continue
                if err in (2, 231):  # FILE_NOT_FOUND / PIPE_BUSY → server cycling
                    _t.sleep(0.05)
                    continue
                last = {"ok": False, "reason": f"broker pipe unreachable (err {err})"}
                _t.sleep(0.05)
                continue
            try:
                owner = _server_owner_sid_of_pipe(handle)
                if owner is None or owner.upper() != self._principal:
                    return {"ok": False, "reason": "broker peer is not the provisioned principal"}
                if not _write_msg(handle, req):
                    last = {"ok": False, "reason": "write failed"}
                    _t.sleep(0.05)
                    continue
                resp = _read_msg(handle)
                if isinstance(resp, dict):
                    return resp
                last = {"ok": False, "reason": "no response"}
                _t.sleep(0.05)
            finally:
                k.CloseHandle(handle)
        return last

    def issue_card(self, provider_path: str, sha256: str, ttl_seconds: int):
        resp = self._rpc(
            {
                "op": "issue_card",
                "provider_path": provider_path,
                "sha256": sha256,
                "ttl_seconds": ttl_seconds,
            }
        )
        return resp.get("card") if resp.get("ok") else None

    def verify_and_consume(self, card: dict):
        resp = self._rpc({"op": "verify_and_consume", "card": card})
        return (bool(resp.get("ok")), str(resp.get("reason") or ""))


def connect(endpoint: str, principal_sid: str):
    return WindowsPipeBrokerClient(endpoint, principal_sid)
