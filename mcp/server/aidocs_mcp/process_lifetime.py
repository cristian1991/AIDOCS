"""Every spawned process is BORN with a lifetime (#757).

OPERATOR RULING 2026-08-06: "tools should have their lifetime, i give life, not
take it." There is NO REAPER. Nothing patrols for orphans and decides when they
die. A process receives its lifetime at creation and cannot outlive it.

THE DEFECT THIS REPLACES. An ai_test run detached at the client-idle guard and
reported "CONTINUES server-side under its 900s timeout". Ninety minutes later
six pytest processes were still alive, two having burned 2,480 CPU-SECONDS each,
and they had to be killed by hand. The 900s ceiling governed the RUN RECORD, not
the PROCESS TREE: when the client detached, nothing owned the xdist workers. A
timeout that expires a row instead of a process is a note-to-self.

Killing the parent does not help and is exactly what already happened -- the
workers are children of the runner, not of the client. On Windows a JOB OBJECT
is the only construct that binds a whole tree to one lifetime.

HOW A LIFETIME IS GRANTED (Windows):
  1. create the job, with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  2. spawn CREATE_SUSPENDED, so the child cannot fork a grandchild before it is
     bound -- assigning after the process is already running leaves a race in
     which a grandchild escapes the job;
  3. AssignProcessToJobObject -- and the job is NON-BREAKAWAY, so a descendant
     cannot opt out of the lifetime it inherited;
  4. resume, then arm a supervisor-owned wall-clock timer.

WHY NtResumeProcess AND NOT ResumeThread: Python's subprocess CLOSES the thread
handle returned by CreateProcess, so the textbook resume is unavailable. The
NT-level call takes the PROCESS handle, which Popen does expose, and resumes
every thread -- so genuine create-suspended semantics survive without
reimplementing subprocess.

KILL_ON_JOB_CLOSE IS THE SUPERVISOR-DEATH GUARANTEE, and it is not a fallback:
the job handle lives in the supervising process. If that process dies for any
reason -- crash, taskkill, power of a hostile agent -- the handle closes and
Windows terminates the whole tree. A bounded run therefore CANNOT outlive its
supervisor, with no sweeper involved.

PERSISTENT runs (daemons the operator deliberately wants to outlive a request)
are the explicit opposite: no job, no deadline, no kill. They must be asked for
by name -- ``Lifetime.persistent()`` -- and are never the default. Bounded is
the default precisely because the expensive mistake is the one that survives.

POSIX: job objects do not exist. The equivalent is a process GROUP plus
killpg(), which this module uses so the contract is the same everywhere; the
create-suspended step is unnecessary because setsid/setpgrp binds the group at
exec time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

_IS_WINDOWS = sys.platform == "win32"

# The operator's ceiling. A bounded run may ask for LESS; it may never ask for
# more. A caller that wants forever must say `persistent`, which is a different
# and deliberately louder request.
MAX_BOUNDED_SECONDS = 30 * 60

CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = 1


@dataclass(frozen=True)
class Lifetime:
    """How long a spawn is entitled to live, decided at birth."""

    seconds: float | None  # None == persistent
    label: str = ""

    @property
    def bounded(self) -> bool:
        return self.seconds is not None

    @classmethod
    def bounded_for(cls, seconds: float, label: str = "") -> Lifetime:
        """A run that dies on schedule. Clamped to MAX_BOUNDED_SECONDS.

        Clamped rather than rejected: a caller asking for 10 hours has made a
        judgement error, and refusing the spawn outright would push callers to
        route around this module -- which is how the orphans happened.
        """
        if seconds is None or seconds <= 0:
            seconds = MAX_BOUNDED_SECONDS
        return cls(seconds=min(float(seconds), float(MAX_BOUNDED_SECONDS)), label=label)

    @classmethod
    def persistent(cls, label: str = "") -> Lifetime:
        """A run the operator deliberately wants to outlive the request."""
        return cls(seconds=None, label=label)


class _WindowsJob:
    """A job object holding one process tree. Closing it kills the tree."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.handle = self._k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _EXT()
        # KILL_ON_JOB_CLOSE and nothing else: no BREAKAWAY flag is set, so a
        # descendant cannot detach itself from the lifetime it inherited.
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._k32.SetInformationJobObject(
            self.handle,
            _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, proc_handle: int) -> None:
        if not self._k32.AssignProcessToJobObject(self.handle, proc_handle):
            raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def active_processes(self) -> int:
        """How many processes in this tree are still alive."""
        ctypes = self._ctypes
        from ctypes import wintypes

        class _ACC(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        if not self.handle:
            return 0
        acc = _ACC()
        if not self._k32.QueryInformationJobObject(
            self.handle,
            _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(acc),
            ctypes.sizeof(acc),
            None,
        ):
            return 0
        return int(acc.ActiveProcesses)

    def terminate(self) -> None:
        self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        # Closing is itself lethal (KILL_ON_JOB_CLOSE). That is the point.
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _resume(proc) -> None:
    """Resume a CREATE_SUSPENDED process via its PROCESS handle."""
    import ctypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess(int(proc._handle))  # noqa: SLF001 -- Popen's own handle


def spawn_with_lifetime(
    argv,
    *,
    lifetime: Lifetime,
    popen=None,
    **popen_kwargs,
):
    """Spawn ``argv`` bound to ``lifetime``. Returns the Popen.

    The returned proc carries ``_aidocs_job`` (the job holding the tree,
    Windows) and
    ``_aidocs_deadline_timer``, so a supervisor can hand ownership on and a
    test can assert on the machinery instead of on wall-clock luck.
    """
    popen = popen or subprocess.Popen

    if not lifetime.bounded:
        # PERSISTENT: no job, no timer, no kill. Explicitly asked for.
        proc = popen(argv, **popen_kwargs)
        proc._aidocs_job = None
        proc._aidocs_deadline_timer = None
        return proc

    job = None
    if _IS_WINDOWS:
        try:
            job = _WindowsJob()
        except OSError:
            job = None  # fall through to timer-only; still bounded, just coarser
        flags = int(popen_kwargs.pop("creationflags", 0)) | CREATE_SUSPENDED
        proc = popen(argv, creationflags=flags, **popen_kwargs)
        if job is not None:
            try:
                job.assign(int(proc._handle))  # noqa: SLF001
            except OSError:
                job.close()
                job = None
        try:
            _resume(proc)
        except Exception:  # noqa: BLE001
            # A process that cannot be resumed must not be left suspended
            # forever -- that is a different flavour of the same bug.
            proc.kill()
            raise
    else:
        # POSIX: bind the tree into its own process group at exec time.
        #
        # setdefault, NOT a literal. Callers legitimately supply this
        # themselves -- code_runner_detached's detach kwargs are exactly
        # {"start_new_session": True} on POSIX -- and a second value for one
        # keyword is a TypeError at the call, so EVERY detached run on Linux
        # failed to spawn (Gate 2b, 2026-08-12). The Windows branch above
        # already POPS creationflags and merges it; this branch forgot the
        # symmetry. An explicit False stays the caller's decision.
        popen_kwargs.setdefault("start_new_session", True)
        proc = popen(argv, **popen_kwargs)

    def _expire() -> None:
        if job is not None:
            job.terminate()
            job.close()
        elif _IS_WINDOWS:
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass

    timer = threading.Timer(float(lifetime.seconds), _expire)
    timer.daemon = True
    timer.start()

    proc._aidocs_job = job
    proc._aidocs_deadline_timer = timer
    return proc


def tree_is_empty(proc) -> bool:
    """True when NO descendant of ``proc`` is still alive.

    The ledger may not record a run as finished while this is False: a run
    whose parent exited but whose xdist workers are still burning CPU is not
    finished, and calling it finished is what hid the orphans for 90 minutes.
    """
    job = getattr(proc, "_aidocs_job", None)
    # A CLOSED job handle must fall through to the process check. Querying a
    # closed handle returned a stale ActiveProcesses=1 and reported a tree that
    # had already been killed as still alive -- a diagnostic that lies in the
    # safe direction is still a diagnostic that lies.
    if job is not None and getattr(job, "handle", None):
        return job.active_processes() == 0
    return proc.poll() is not None


def await_tree_exit(proc, timeout: float | None = None) -> bool:
    """Wait for the whole tree, not just the parent. True if it fully exited."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if tree_is_empty(proc):
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def release(proc) -> None:
    """Cancel the deadline and drop the job handle for a completed run."""
    timer = getattr(proc, "_aidocs_deadline_timer", None)
    if timer is not None:
        timer.cancel()
    job = getattr(proc, "_aidocs_job", None)
    if job is not None:
        job.close()
        proc._aidocs_job = None
