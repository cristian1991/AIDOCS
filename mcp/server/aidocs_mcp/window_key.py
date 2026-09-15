"""THE WINDOW KEY (#876 phase 1) — which HOST WINDOW is this process inside?

WHAT IT ANSWERS, AND WHY NOTHING ELSE COULD. Every identity channel AIDOCS had
names a CONVERSATION, and a conversation is not durable: measured 2026-08-23 in
one live window, ``/resume`` rotated it, ``/clear`` rotated it again, and
``/mcp`` respawned the shim onto a third value. Across all three the Claude Code
process was UNCHANGED::

                    BEFORE          AFTER           action
    conversation    b6a187cf...  -> bc8bd9e3...     /resume
    conversation    bc8bd9e3...  -> 7d525acd...     /clear
    header          3a3a4a10...  -> 7d525acd...     /mcp  (shim respawns)
    window key      13336:134319313179516362 -- IDENTICAL THROUGHOUT

Two concurrent windows had DISJOINT Claude Code pids (16716 with 9 python
descendants, 13336 with 2), so the key discriminates between windows as well as
surviving within one. The stdio shim and every hook process are spawned by that
same ``claude.exe``, which is what lets two unrelated processes agree on one
window without any channel between them.

TWO COMPONENTS, NEVER ONE. ``<host pid>:<host creation filetime>``. Windows
recycles pids. A key that is a bare pid lets a NEW process inherit a DEAD
window's identity -- and once phase 2 hangs a conversation lease off this key,
that means inheriting its conversation and its authority. The creation time is
not decoration; it is the half that makes the pid an identity instead of a slot.

IDENTITY HAS NO FALLBACK (operator law, 2026-08-23, verbatim: "fallbacks can
stamp wrong data and we cannot tell from where. identity has no fallback").
When this module cannot prove which window it is in it returns ``""`` AND A
REASON, and it substitutes NOTHING -- not the conversation id, not the bare pid,
not a placeholder. It cannot even read a conversation id: no host-session
environment variable is named anywhere in this file, and a test asserts that.
The same posture as ``resolve_host_identity``'s honest empties
(``agent_memory_epoch.py:685-686``), extended with the reason, because "we
cannot tell from where" is precisely the defect being removed.

PORTABILITY IS SOLVED PER HOST, NEVER BY FALLBACK. The ancestry-walk IDEA is
portable; each IMPLEMENTATION is measured, or at least measurable, on the box it
serves. THREE now, one shared walk::

    win32   CreateToolhelp32Snapshot + GetProcessTimes    creation FILETIME
    linux   /proc/<pid>/stat  fields 4 and 22             starttime, ticks
    darwin  proc_pidinfo(PROC_PIDTBSDINFO) via libproc    tvsec/tvusec, µs

all through ctypes or plain reads, all stdlib. A host with no branch -- a BSD, a
remote/cloud session, a differently-launched wrapper -- HAS NO WINDOW KEY. That
is an honest limitation with a name, not a licence to guess.

WHY DARWIN WAS URGENT RATHER THAN NICE. The operator is migrating OFF Windows,
so the only MEASURED derivation was the platform being abandoned. While phase 1
is additive that is merely lopsided; the moment #880 makes the key
AUTHORITATIVE it is a LOCKOUT -- no key, no lease, every gated tool refuses, and
the refusal cannot be healed because healing it would itself be a gated call.

STDLIB ONLY, DELIBERATELY. ``stdio_shim`` imports this module and is stdlib-only
by construction (it runs in whatever interpreter the HOST was configured with, so
a third-party import here turns dependency drift into "MCP server failed to
start"). Keep it that way: ``ctypes`` is stdlib and is fine, package machinery
is not.

PHASE 1 IS ADDITIVE. Nothing in AIDOCS reads this value to make a decision. It
is derived, forwarded and recorded so that phase 2 (#880, window-anchored
conversation leases) has measured ground to stand on.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Callable

# ── WHICH IMAGE *IS* THE WINDOW, PER HOST ─────────────────────────────────
#
# Matched by exact name, case-insensitively. A LOOKALIKE
# (``claude-code-helper.exe``) is a different program and must not be mistaken
# for the window, so this is an equality test and never a substring test.

#: win32. MEASURED on this box 2026-08-23: two live windows, pids 13336 and
#: 16716, both named ``claude.exe``.
HOST_PROCESS_NAME = "claude.exe"

#: linux. ⚠ **NOT MEASURED.** Every other value in this module was read off a
#: live process; this one was not, because the box it was written on is win32
#: and has no ``/proc`` to read. It is spelled here as a named constant beside
#: its win32 sibling precisely so that ONE MEASUREMENT ON A LINUX HOST settles
#: it: from a shell inside a Claude Code window, read ``/proc/<pid>/comm``
#: upward from ``$PPID`` and compare.
#:
#: THE TWO WAYS THIS CAN BE WRONG ARE NOT SYMMETRIC:
#:
#:   * TOO NARROW (the host is really ``node``, or a longer name the kernel
#:     truncated) — the walk finds no ancestor and answers
#:     ``("", REASON_NO_HOST_ANCESTOR)``. An honest empty, and the host is no
#:     worse off than it is with no derivation at all.
#:   * TOO BROAD — which is why ``node`` IS DELIBERATELY NOT LISTED, even
#:     though it is the likelier ``comm`` for a JS entrypoint. ``node`` would
#:     match ANY node ancestor, mint a live and plausible and WRONG key, and
#:     hand one window's lease to another window's conversation. A plausible
#:     wrong answer is worse than no answer, so the narrow guess is the only
#:     admissible one.
#:
#: ``/proc/<pid>/comm`` is TRUNCATED BY THE KERNEL to ``TASK_COMM_LEN - 1`` =
#: 15 characters. A host image with a longer name must be spelled here in its
#: TRUNCATED form, not its real one.
POSIX_HOST_PROCESS_NAME = "claude"

# Bounded so a corrupt or cyclic snapshot cannot hang a hook process. Real
# chains measured on this box are 3-5 deep.
_MAX_ANCESTRY_DEPTH = 32

# ── THE SHAPE OF A WINDOW KEY ─────────────────────────────────────────────
#
# TWO POSITIVE DECIMAL INTEGERS, COLON SEPARATED, OR IT IS NOT A WINDOW KEY.
#
# DEFINED HERE, IN THE MODULE THAT MINTS THE KEY, and imported by everything
# that stores or resolves one. #880 measured what the absence of this cost the
# chain — "append-only with NO FORMAT VALIDATION, which is how auth-truth-614,
# a synthetic test id, is seated permanently in an authority structure" — and a
# SECOND COPY of the pattern costs the same thing more slowly: two regexes for
# one authority format drift apart an edit at a time, and the drift only
# surfaces as a key the writer accepted that the resolver refuses, i.e. a row
# that exists and can never be used.
WINDOW_KEY_SHAPE = re.compile(r"[1-9][0-9]*:[1-9][0-9]*")

# ── The reasons. EIGHT CAUSES, EIGHT NAMES ────────────────────────────────
# One generic "unavailable" would reproduce the operator's objection: an empty
# answer that cannot say where it came from. A caller (and phase 2's watcher)
# must be able to tell "this host has no derivation" from "this process could
# not be read" from "the ancestry is broken".

#: This host has NO MEASURED DERIVATION AT ALL — a BSD, a remote/cloud session,
#: a differently-launched wrapper. NO LONGER RETURNED ON LINUX OR MACOS: both
#: have their own derivation below, and answering with this name there would be
#: the "cannot tell from where" defect in miniature — a string that is true
#: about win32 and false about why the caller has no key.
REASON_NOT_WIN32 = "not_win32_no_measured_derivation_for_this_host"
#: linux, and ``/proc`` is not there to read (a container built without it, a
#: hardened mount). A DIFFERENT REMEDY from the one above — this host HAS a
#: derivation and is missing its procfs — so it gets a different name.
REASON_NO_PROCFS = "procfs_unavailable_on_this_linux_host"
#: macOS, and ``libproc`` will not load or does not export what the derivation
#: reads. THE THIRD DISTINCT REMEDY, and the reason this is not folded into
#: either name above: "this host has no derivation" is nothing to do, "mount
#: procfs" is a linux instruction, and this one is a dyld/runtime question on a
#: box that HAS a derivation. One generic "unavailable" would send all three
#: readers down the wrong path.
REASON_NO_LIBPROC = "libproc_unavailable_on_this_darwin_host"
# Read-side only: the hook payload carried no derivation to read.
REASON_NO_PAYLOAD_WINDOW = "no_window_in_hook_payload"
REASON_NO_PROCESS_TABLE = "process_table_unavailable"
REASON_NO_HOST_ANCESTOR = "no_claude_code_ancestor"
REASON_NO_CREATION_TIME = "process_creation_time_unavailable"
REASON_ANCESTRY_BROKEN = "ancestry_link_refuted_by_creation_times"

# ── The sources. EVERY ANSWER NAMES THE DERIVATION THAT PRODUCED IT ───────
#
# There is more than one derivation now, so a HARDCODED label would be a FORGED
# PROVENANCE: a key walked out of ``/proc`` and reported as
# ``win32_ancestry_walk`` is precisely the "we cannot tell from where" this
# module exists to remove.
SOURCE_WIN32_ANCESTRY = "win32_ancestry_walk"
SOURCE_POSIX_ANCESTRY = "linux_proc_ancestry_walk"
SOURCE_DARWIN_ANCESTRY = "darwin_libproc_ancestry_walk"
#: The measurement seam supplied the table and the times. The WALK is the same
#: walk, but the MEASUREMENT is the caller's rather than this host's, and a
#: detail that claimed otherwise would be a provenance forgeable by anything
#: that reached the seam.
SOURCE_INJECTED_SEAM = "injected_measurement_seam"


def _as_filetime(value: object) -> int | None:
    """A creation time is a POSITIVE INTEGER, or it is ABSENT.

    NORMALISES THE VALUE; IT IS NOT THE REFUSAL. Each read site keeps its own
    ``return "", REASON_NO_CREATION_TIME``, so the provenance of a refusal
    stays at the site that refuses — and the fabrication mutant (delete that
    guard and the walk mints ``"16716:None"``) stays killed by the test that
    kills it today. ``None`` in, ``None`` out.

    WHY ``isinstance(value, bool)`` IS CHECKED FIRST: a bool IS an int in
    Python, so ``True`` survives BOTH a truthiness test AND an
    ``isinstance(value, int)`` test, and formats into a key as
    ``"13336:True"`` — a well-formed-LOOKING window identity that was never
    measured. Measured on shipped code, together with ``3.5`` ->
    ``"13336:3.5"``, ``-1`` -> ``"13336:-1"`` (an int, and truthy, so only the
    POSITIVITY test excludes it), and the worst of the set, a DIGIT STRING ->
    ``"13336:134319313179516362"``, which is indistinguishable from a real key
    to every format check downstream. That last one is why this is a TYPE
    check and not a shape check on the finished key.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _win32_process_table() -> list[tuple[int, int, str]] | None:
    """``[(pid, ppid, exe_name)]`` from CreateToolhelp32Snapshot, or None.

    None -- never a partial list -- when the snapshot cannot be taken, so the
    caller reports "I could not look" instead of "there was no ancestor".
    """
    import ctypes
    from ctypes import wintypes

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        )

    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap in (0, -1, 0xFFFFFFFF):
            return None
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        rows: list[tuple[int, int, str]] = []
        try:
            if not k32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                rows.append(
                    (
                        int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        entry.szExeFile.decode("mbcs", "replace"),
                    )
                )
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
        finally:
            k32.CloseHandle(snap)
        return rows or None
    except Exception:  # noqa: BLE001 -- an unreadable box is an honest empty
        return None


def _win32_creation_filetime(pid: int) -> int | None:
    """The process's creation FILETIME as a raw 64-bit int, or None.

    None when the process cannot be opened or timed. NOT 0: zero is what a
    failed ``GetProcessTimes`` leaves in the struct, and treating it as a value
    would put a constant into every key.
    """
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return None
        try:
            created, exited = _FILETIME(), _FILETIME()
            kernel, user = _FILETIME(), _FILETIME()
            ok = k32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            raw = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return raw or None
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


# ── THE LINUX DERIVATION (``/proc``) ──────────────────────────────────────
#
# WHY THIS EXISTS AT ALL. Until now ``derive_window_key`` answered
# ``("", REASON_NOT_WIN32)`` on every non-win32 host. That was a correct and
# harmless limitation while PHASE 1 WAS ADDITIVE. It stops being harmless the
# moment #880 makes the window key AUTHORITATIVE: no window key means no lease,
# no lease means every gated tool refuses, and — the part that makes it a
# LOCKOUT rather than an inconvenience — NOTHING CAN HEAL IT, because the act
# of healing would itself be a gated call. A Linux VPS would be bricked on
# arrival. This is the derivation that makes the programme deployable.
#
# THE SAME IDEA, MEASURED DIFFERENTLY. The win32 walk climbs ppids and proves
# each link with a creation FILETIME. Linux has both facts in
# ``/proc/<pid>/stat``: field 4 is the ppid, and field 22 (``starttime``) is
# the process's start time in CLOCK TICKS SINCE BOOT. ``starttime`` is the
# analogue of the filetime for every purpose this module has — it is FIXED for
# the life of the process, it makes a recycled pid distinguishable from the
# process it replaced, and it increases monotonically with age, so the
# "a parent younger than its child proves a recycled ppid" guard is the same
# comparison on the same kind of number.
#
# TICKS SINCE **BOOT**, NOT SINCE AN EPOCH. Two different boots can mint the
# same ``<pid>:<starttime>`` pair. That is not a defect HERE: a window key
# identifies a window on a LIVE host, its whole lifetime is inside one boot,
# and every reader of the lease is a process on that same running kernel. It
# WOULD be a defect if a key were persisted across a reboot and then TRUSTED,
# which is why #880's watcher must check liveness rather than assume it.

#: Where the process table lives. Named so a test can point it elsewhere.
_PROC = "/proc"

# ── THE COMM TRAP ────────────────────────────────────────────────────────
#
# ``/proc/<pid>/stat`` LOOKS like whitespace-separated fields and is not.
# FIELD 2 IS THE EXECUTABLE NAME IN PARENTHESES AND IT MAY CONTAIN SPACES AND
# PARENTHESES::
#
#     1234 (my prog (v2)) S 1200 1234 ...
#
# ``text.split()[3]`` gives ``"(v2))"`` for that process, not the ppid. This is
# THE classic bug in naive /proc parsers, and here it would not merely fail —
# it would CLIMB THE WRONG CHAIN, because a mis-indexed field is still a number
# often enough to look plausible. The only correct parse is to find the LAST
# ``)`` and index from there: a pid contains no parenthesis, and every field
# after comm is a number or a single letter.
#
# Fields are numbered as proc(5) numbers them, 1-based, and converted to an
# index in ONE place. An off-by-one is then a visible edit to a named constant
# rather than an invisible one to a subscript.
_STAT_FIRST_FIELD_AFTER_COMM = 3  # field 3 is `state`, the first one we see
_STAT_PPID_FIELD = 4
_STAT_STARTTIME_FIELD = 22


def _stat_index(field_number: int) -> int:
    """proc(5)'s 1-based field number -> an index into the post-comm split."""
    return field_number - _STAT_FIRST_FIELD_AFTER_COMM


def _proc_stat_fields(text: str) -> list[str] | None:
    """A ``/proc/<pid>/stat`` line's fields FROM FIELD 3 ONWARD, or None.

    Split on the LAST ``)`` — see THE COMM TRAP above. None when there is no
    ``)`` at all: that is not a stat line, and it must not be guessed at.
    """
    close = text.rfind(")")
    if close < 0:
        return None
    return text[close + 1 :].split() or None


def _proc_comm(text: str) -> str:
    """The executable name out of a stat line.

    Between the FIRST ``(`` and the LAST ``)``, so a name containing either
    bracket survives intact. NOTE the kernel truncates this to
    ``TASK_COMM_LEN - 1`` = 15 characters.
    """
    opened = text.find("(")
    closed = text.rfind(")")
    if opened < 0 or closed < opened:
        return ""
    return text[opened + 1 : closed]


def _read_proc_stat(pid: int) -> str:
    """One ``/proc/<pid>/stat`` line, or "" when it cannot be read.

    "" — never a partial or invented line. A process that exits between the
    directory listing and this read is ORDINARY, not an error.
    """
    try:
        with open(
            f"{_PROC}/{int(pid)}/stat", encoding="utf-8", errors="replace"
        ) as handle:
            return handle.read()
    except (OSError, ValueError):
        return ""


def _posix_process_table() -> list[tuple[int, int, str]] | None:
    """``[(pid, ppid, comm)]`` read out of ``/proc``, or None.

    None -- never a partial list -- when ``/proc`` itself cannot be listed, so
    the caller reports "I could not look" instead of "there was no ancestor".
    An individual process that vanishes mid-scan is skipped: that is a race
    with reality, not a failure to look.
    """
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return None

    rows: list[tuple[int, int, str]] = []
    ppid_at = _stat_index(_STAT_PPID_FIELD)
    for entry in entries:
        # ``isdecimal`` and not ``isdigit``: ``isdigit`` also accepts
        # superscripts and other numerals, which ``int()`` then rejects or --
        # worse -- reads as a different number than the name looks like.
        if not entry.isdecimal():
            continue
        text = _read_proc_stat(int(entry))
        if not text:
            continue
        fields = _proc_stat_fields(text)
        if not fields or len(fields) <= ppid_at:
            continue
        try:
            rows.append((int(entry), int(fields[ppid_at]), _proc_comm(text)))
        except ValueError:
            continue
    return rows or None


def _posix_start_time(pid: int) -> int | None:
    """``starttime`` (field 22) in clock ticks since boot, or None.

    None -- never 0 -- when the process cannot be read or the field is not a
    number, for the same reason the win32 primitive never returns 0: an unknown
    that is expressible as a value ends up inside a key.
    """
    fields = _proc_stat_fields(_read_proc_stat(pid))
    started_at = _stat_index(_STAT_STARTTIME_FIELD)
    if not fields or len(fields) <= started_at:
        return None
    try:
        return int(fields[started_at]) or None
    except ValueError:
        return None


# ── THE DARWIN DERIVATION (``libproc``) ───────────────────────────────────
#
# WHY THIS EXISTS AT ALL. macOS answered ``("", REASON_NOT_WIN32)`` -- NO
# DERIVATION AT ALL -- while the operator migrates OFF Windows. That left the
# ONLY MEASURED derivation on the platform being abandoned, and #880 turns a
# missing key into a LOCKOUT rather than an inconvenience: no key -> no lease
# -> every gated tool refuses -> and NOTHING CAN HEAL IT, because the act of
# healing would itself be a gated call.
#
# THE SAME IDEA, MEASURED DIFFERENTLY AGAIN. macOS has no ``/proc``. libproc's
# ``proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof info)`` fills
# ``struct proc_bsdinfo``, which carries all three facts the shared walk needs:
#
#     pbi_ppid                            the parent link
#     pbi_start_tvsec / pbi_start_tvusec  a per-process age, fixed for life
#     pbi_name[32]                        the image name
#
# The two start fields compose to MICROSECONDS, which is the darwin analogue of
# the win32 creation FILETIME and of linux ``starttime``: FIXED for the life of
# the process, DIFFERENT for a recycled pid, and ordered so a parent's number is
# smaller than its child's. The monotonicity guard is therefore the same
# comparison on the same kind of number, and needs no darwin-specific copy.
#
# WALL CLOCK, NOT BOOT-RELATIVE. ``pbi_start_*`` is a timeval, so a BACKWARDS
# CLOCK STEP between two process starts can make a genuine parent look YOUNGER
# than its child. That direction is SAFE -- the guard refuses and the caller
# gets an honest empty. The guard exists to prevent the OTHER direction, a live
# and plausible and WRONG key, and a clock step cannot manufacture one of those.
#
# ``pbi_name`` AND NOT ``pbi_comm``. ``pbi_comm`` is ``MAXCOMLEN`` = 16 bytes
# and TRUNCATES; ``pbi_name`` is ``2 * MAXCOMLEN`` = 32. Reading the short one
# would make any host image longer than 15 characters unmatchable -- the "too
# narrow" failure. Too narrow degrades to an honest empty rather than to a wrong
# key, but it leaves the derivation UNREALISED on a host that has one, which is
# the whole point of building it.
#
# WHAT THIS MODULE STILL REFUSES TO DO, AND WHY -- because a darwin build is
# exactly where somebody proposes it. It WAS proposed, and it is WRONG:
#
#   "Identify the host as the topmost ancestor whose process environment block
#    carries this window's conversation id."
#
# Both mechanisms it would need -- the per-process block under ``/proc`` on
# linux, ``KERN_PROCARGS2`` via sysctl on darwin -- report a process's variables
# AS THEY WERE AT ``execve``, read out of the initial stack region. The library
# setter allocates new storage and never writes back there. The window process
# is the process that MINTS the conversation id: after its own exec, and again
# on every ``/clear`` (MEASURED, #876: ``bc8bd9e3 -> 7d525acd``). So the
# window's exec-time block CANNOT hold the current value, and the topmost
# process whose block DOES hold it is one of the window's CHILDREN -- every one
# of which was measured to CHURN between two consecutive tool calls
# (``11728->8612``, ``344->20856``, ``16812->11016``, ``14056->10348``,
# ``17252->19732``). The rule would select a live, plausible, WRONG,
# per-call-unstable process. That is the failure this module was built to make
# impossible, arriving through the front door.

#: darwin. The image name to look for. NOT MEASURED -- no mac exists on the box
#: this was written on -- and spelled as its own named constant beside its win32
#: and linux siblings precisely so ONE READING settles it: from a shell inside a
#: Claude Code window on macOS, read ``pbi_name`` upward from the parent pid.
#:
#: A SEPARATE CONSTANT FROM THE LINUX ONE, DELIBERATELY. The two carry different
#: bounds: linux ``comm`` is truncated by the kernel at 15 characters, darwin
#: ``pbi_name`` holds 31. A shared constant would silently impose the tighter
#: bound on the looser host, and the day one of them is measured the other would
#: be changed by accident.
#:
#: THE TWO WAYS THIS CAN BE WRONG ARE NOT SYMMETRIC, exactly as on linux. Too
#: narrow -> no ancestor matches -> ``REASON_NO_HOST_ANCESTOR``, an honest
#: empty. Too broad -> a live, plausible, WRONG key. So ``node`` is deliberately
#: absent here too, even though it is the likelier image for a JS entrypoint.
DARWIN_HOST_PROCESS_NAME = "claude"

#: ``proc_pidinfo`` flavour that fills ``struct proc_bsdinfo``.
_PROC_PIDTBSDINFO = 3

# ── THE STRUCT, FIELD BY FIELD ───────────────────────────────────────────
#
# ``struct proc_bsdinfo``, ``sys/proc_info.h``. Read at NAMED BYTE OFFSETS
# rather than through a ctypes ``Structure`` for one reason: the offsets are
# then TESTABLE ON ANY PLATFORM, against blobs built at the documented
# positions, on a machine with no darwin kernel anywhere near it. A ctypes
# Structure would only be checkable by running it on a mac.
#
#     offset  size  field
#          0    4   pbi_flags
#          4    4   pbi_status
#          8    4   pbi_xstatus
#         12    4   pbi_pid
#         16    4   pbi_ppid
#         20  4*7   pbi_uid, pbi_gid, pbi_ruid, pbi_rgid, pbi_svuid,
#                   pbi_svgid, rfu_1
#         48   16   pbi_comm[MAXCOMLEN]
#         64   32   pbi_name[2 * MAXCOMLEN]
#         96  4*6   pbi_nfiles, pbi_pgid, pbi_pjobc, e_tdev, e_tpgid, pbi_nice
#        120    8   pbi_start_tvsec
#        128    8   pbi_start_tvusec
#        136        (end)
#
# Every offset is a named constant so an off-by-one is a VISIBLE edit rather
# than an invisible subscript -- the same rule the /proc field numbers follow.
_BSDINFO_PID_OFFSET = 12
_BSDINFO_PPID_OFFSET = 16
_BSDINFO_COMM_OFFSET = 48
_BSDINFO_COMM_LEN = 16
_BSDINFO_NAME_OFFSET = 64
_BSDINFO_NAME_LEN = 32
_BSDINFO_START_SEC_OFFSET = 120
_BSDINFO_START_USEC_OFFSET = 128
_BSDINFO_SIZE = 136


def _bsdinfo_cstring(raw: bytes, offset: int, length: int) -> str:
    """A NUL-terminated char array out of the struct, as text."""
    return raw[offset : offset + length].split(b"\x00", 1)[0].decode("utf-8", "replace")


def _darwin_parse_bsdinfo(blob: object, pid: int) -> tuple[int, str, int] | None:
    """``(ppid, name, start_microseconds)`` out of a ``proc_bsdinfo``, or None.

    A PURE FUNCTION OVER BYTES, which is what makes the offsets above provable
    without a mac.

    IT VERIFIES ITSELF AGAINST THE PID IT WAS ASKED ABOUT. ``pbi_pid`` must
    equal ``pid``; the kernel always fills it. This is not belt-and-braces --
    it is what converts a WRONG BUILD into an honest empty on a real host
    instead of into a plausible wrong key. Shift the offsets, or meet a
    revision of the struct that reorders them, and the pid check fails FIRST,
    before any name or age can be believed.

    ``None`` -- never a partial tuple -- for a short, absent or non-bytes blob.
    """
    if not isinstance(blob, (bytes, bytearray)):
        return None
    raw = bytes(blob)
    if len(raw) < _BSDINFO_SIZE:
        return None

    def _u32(offset: int) -> int:
        return int.from_bytes(raw[offset : offset + 4], "little")

    def _u64(offset: int) -> int:
        return int.from_bytes(raw[offset : offset + 8], "little")

    if _u32(_BSDINFO_PID_OFFSET) != int(pid):
        return None

    # ``pbi_name`` first. Reading ``pbi_comm`` when the kernel left the long
    # name EMPTY is not a fallback across sources -- it is ONE kernel call and
    # ONE struct holding two spellings of ONE fact -- and it cannot WIDEN the
    # match, because ``pbi_comm`` is ``pbi_name`` truncated at 15 characters,
    # so an equality test against a short constant is satisfied only by a
    # process whose real name IS that constant.
    name = _bsdinfo_cstring(raw, _BSDINFO_NAME_OFFSET, _BSDINFO_NAME_LEN)
    if not name:
        name = _bsdinfo_cstring(raw, _BSDINFO_COMM_OFFSET, _BSDINFO_COMM_LEN)

    started = (
        _u64(_BSDINFO_START_SEC_OFFSET) * 1_000_000
        + _u64(_BSDINFO_START_USEC_OFFSET)
    )
    return _u32(_BSDINFO_PPID_OFFSET), name, started


# TWO dlopen SPELLINGS OF ONE LIBRARY IS NOT A FALLBACK. There is exactly ONE
# source here -- libproc -- and dyld may hand it over under the shared-cache
# name or only through the already-loaded libSystem that re-exports it. Both
# resolve THE SAME SYMBOLS from THE SAME implementation. If neither resolves,
# the answer is ``REASON_NO_LIBPROC`` and nothing else.
_LIBPROC_NAMES: tuple[str | None, ...] = ("libproc.dylib", None)

#: Distinguishes "not looked yet" from "looked, and it is not there". A plain
#: ``None`` sentinel would repeat the dlopen for every pid of every walk.
_LIBPROC_UNSET = object()
_LIBPROC: Any = _LIBPROC_UNSET


def _load_libproc() -> Any:
    """The libproc handle, or None. Runs at most once per process."""
    import ctypes

    for name in _LIBPROC_NAMES:
        try:
            lib = ctypes.CDLL(name)
            # BOTH symbols, or it is not the library this module needs. A
            # partial handle would fail later, mid-walk, as an exception rather
            # than as a named reason.
            if hasattr(lib, "proc_pidinfo") and hasattr(lib, "proc_listallpids"):
                return lib
        except Exception:  # noqa: BLE001 -- an unloadable library IS the reason
            continue
    return None


def _darwin_libproc() -> Any:
    global _LIBPROC
    if _LIBPROC is _LIBPROC_UNSET:
        _LIBPROC = _load_libproc()
    return _LIBPROC


def _darwin_bsdinfo(pid: int) -> bytes | None:
    """The raw ``proc_bsdinfo`` bytes for ``pid``, or None.

    A SHORT WRITE IS NOT A PARTIAL ANSWER. ``proc_pidinfo`` returns the number
    of bytes it filled; anything other than the whole struct means the kernel
    did not answer the question asked, and the rest of the buffer is whatever
    it happened to hold. Refuse rather than parse.
    """
    lib = _darwin_libproc()
    if lib is None:
        return None
    import ctypes

    try:
        buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
        written = int(
            lib.proc_pidinfo(
                ctypes.c_int(int(pid)),
                ctypes.c_int(_PROC_PIDTBSDINFO),
                ctypes.c_uint64(0),
                buf,
                ctypes.c_int(_BSDINFO_SIZE),
            )
        )
    except Exception:  # noqa: BLE001 -- an unreadable process is an honest empty
        return None
    if written != _BSDINFO_SIZE:
        return None
    return buf.raw


#: How many times the pid buffer may double before giving up. Bounded so a
#: pathological box cannot spin a hook process.
_DARWIN_PID_BUFFER_GROWTHS = 6


def _darwin_all_pids() -> list[int] | None:
    """Every pid on the box, or None when the table cannot be read.

    THE RETURN VALUE IS BYTES, AND THIS CODE DOES NOT HAVE TO BELIEVE THAT.
    ``proc_listallpids`` documents a byte count, but the buffer is grown and
    retried whenever a call fills it EXACTLY -- which is the signature of a
    truncated answer, and also what would happen if the probe had returned a
    COUNT and the allocation were four times too small. Either way the result
    is a COMPLETE table or None, never a silently partial one that the walk
    would then report as "there was no ancestor".
    """
    lib = _darwin_libproc()
    if lib is None:
        return None
    import ctypes

    try:
        needed = int(lib.proc_listallpids(None, ctypes.c_int(0)))
    except Exception:  # noqa: BLE001
        return None
    if needed <= 0:
        return None

    slots = max(int(needed) // ctypes.sizeof(ctypes.c_int), 1) + 64
    for _ in range(_DARWIN_PID_BUFFER_GROWTHS):
        try:
            buf = (ctypes.c_int * slots)()
            capacity = ctypes.sizeof(buf)
            # The ARRAY, not ``byref`` of it: ctypes already passes an array as
            # a pointer to its first element, which is the C ABI this call
            # wants — and it keeps the argument a plain buffer that a stub
            # library in a test can be handed and can write into, which is the
            # only way this function's body is reachable off a mac.
            written = int(lib.proc_listallpids(buf, ctypes.c_int(capacity)))
        except Exception:  # noqa: BLE001
            return None
        if written <= 0:
            return None
        if written >= capacity:
            slots *= 2
            continue
        count = written // ctypes.sizeof(ctypes.c_int)
        return [int(buf[i]) for i in range(count) if int(buf[i]) > 0]
    return None


def _darwin_process_table() -> list[tuple[int, int, str]] | None:
    """``[(pid, ppid, name)]`` from libproc, or None.

    None -- never a partial list -- when the pid table itself cannot be read,
    so the caller reports "I could not look" instead of "there was no
    ancestor". An individual process that exits between the listing and its
    ``proc_pidinfo`` is skipped: that is a race with reality, not a failure to
    look.
    """
    pids = _darwin_all_pids()
    if not pids:
        return None
    rows: list[tuple[int, int, str]] = []
    for pid in pids:
        parsed = _darwin_parse_bsdinfo(_darwin_bsdinfo(pid), pid)
        if parsed is None:
            continue
        ppid, name, _started = parsed
        rows.append((int(pid), int(ppid), name))
    return rows or None


def _darwin_start_time(pid: int) -> int | None:
    """The process's start time in MICROSECONDS, or None.

    None -- never 0 -- when the process cannot be read, for the same reason the
    win32 and linux primitives never return 0: an unknown that is expressible
    as a value ends up inside a key.
    """
    parsed = _darwin_parse_bsdinfo(_darwin_bsdinfo(pid), pid)
    if parsed is None:
        return None
    return parsed[2] or None


# ── IS THIS PROCESS THE HOST? ─────────────────────────────────────────────
#
# One predicate per host, because it is not the same question on each. win32
# filenames are case-insensitive, so folding is CORRECT there. POSIX names are
# case-sensitive, so folding there would WIDEN the match -- and too broad is
# the dangerous direction: it is the one that mints a live, plausible, WRONG
# key and hands one window's lease to another window's conversation.


def _win32_is_host(name: str) -> bool:
    return name.lower() == HOST_PROCESS_NAME


def _posix_is_host(name: str) -> bool:
    return name == POSIX_HOST_PROCESS_NAME


def _darwin_is_host(name: str) -> bool:
    # Its OWN predicate over its OWN constant, not an alias of the linux one:
    # the two constants carry different length bounds and will be measured on
    # different days, and a shared predicate is how one measurement silently
    # becomes an assertion about the other host.
    return name == DARWIN_HOST_PROCESS_NAME


def _named_is_host(want: str, *, fold: bool) -> Callable[[str], bool]:
    """A predicate for an EXPLICITLY NAMED host image (the measurement seam).

    ``fold`` is not a style knob: it carries the host's filename convention, so
    naming an image through the seam cannot quietly change whether the match is
    case sensitive underneath the caller.
    """
    if fold:
        wanted = want.lower()
        return lambda name: name.lower() == wanted
    return lambda name: name == want


def _host_derivation() -> tuple[
    Callable[[], list[tuple[int, int, str]] | None] | None,
    Callable[[int], int | None] | None,
    Callable[[str], bool] | None,
    str,
    str,
]:
    """This host's derivation: ``(table, times, is_host, source, reason)``.

    PORTABILITY IS SOLVED PER HOST, NEVER BY FALLBACK. Each branch is a
    derivation somebody measured on that kind of box. A host with no branch
    gets a REASON and no derivation -- it does not get somebody else's.

    Exactly one of ``source`` / ``reason`` is non-empty, mirroring the contract
    of the function it serves.
    """
    if sys.platform == "win32":
        return (
            _win32_process_table,
            _win32_creation_filetime,
            _win32_is_host,
            SOURCE_WIN32_ANCESTRY,
            "",
        )
    if sys.platform.startswith("linux"):
        if not os.path.isdir(_PROC):
            # A linux host WITHOUT procfs -- a container built without it, a
            # hardened mount. Distinct from "not linux" because the remedy is
            # distinct: this box HAS a derivation and is missing the filesystem
            # that derivation reads.
            return None, None, None, "", REASON_NO_PROCFS
        return (
            _posix_process_table,
            _posix_start_time,
            _posix_is_host,
            SOURCE_POSIX_ANCESTRY,
            "",
        )
    if sys.platform == "darwin":
        if _darwin_libproc() is None:
            # A macOS host whose libproc will not load or does not export what
            # this derivation reads. Distinct from "not darwin" and from "no
            # procfs" because the remedy is distinct again: this box HAS a
            # derivation and cannot reach the library that derivation calls.
            return None, None, None, "", REASON_NO_LIBPROC
        return (
            _darwin_process_table,
            _darwin_start_time,
            _darwin_is_host,
            SOURCE_DARWIN_ANCESTRY,
            "",
        )
    return None, None, None, "", REASON_NOT_WIN32


def _derive_with_source(
    pid: int | None,
    *,
    process_table: Callable[[], list[tuple[int, int, str]] | None] | None,
    creation_time: Callable[[int], int | None] | None,
    host_name: str | None,
) -> tuple[str, str, str]:
    """``(key, reason, source)``. THE ONE WALK, shared by every host.

    The walk is host-agnostic: it needs a parent map, a per-pid monotonic age,
    and a way to recognise the host. Only WHERE those three come from differs,
    which is why there is one of these and not one per platform -- a second
    copy of the monotonicity guard would be a second place for it to go
    missing, and #880's own mutation list includes "the guard removed on the
    posix path only".
    """
    injected = process_table is not None and creation_time is not None
    is_host: Callable[[str], bool]
    if injected:
        # The caller supplied the measurement. The walk is unchanged, but the
        # PROVENANCE is the caller's and must not claim to be this host's.
        source = SOURCE_INJECTED_SEAM
        # An injected walk with no name given keeps the win32 convention: the
        # seam's existing callers all feed win32-shaped tables.
        is_host = (
            _win32_is_host
            if host_name is None
            else _named_is_host(host_name, fold=True)
        )
    else:
        host_table, host_times, host_is, source, reason = _host_derivation()
        if reason:
            return "", reason, ""
        assert host_table is not None and host_times is not None
        assert host_is is not None
        process_table = process_table or host_table
        creation_time = creation_time or host_times
        # An explicitly named image is matched by THIS HOST's convention, so
        # naming one does not quietly change case sensitivity underneath it.
        is_host = (
            host_is
            if host_name is None
            else _named_is_host(host_name, fold=host_is is _win32_is_host)
        )

    assert process_table is not None and creation_time is not None

    rows = process_table()
    if not rows:
        return "", REASON_NO_PROCESS_TABLE, ""

    parents = {int(p): int(pp) for p, pp, _ in rows}
    names = {int(p): str(nm or "") for p, _, nm in rows}

    current = int(os.getpid() if pid is None else pid)
    current_created = _as_filetime(creation_time(current))
    if not current_created:
        return "", REASON_NO_CREATION_TIME, ""

    seen: set[int] = set()
    for _ in range(_MAX_ANCESTRY_DEPTH):
        if is_host(names.get(current, "")):
            return f"{current}:{current_created}", "", source
        seen.add(current)
        parent = parents.get(current)
        if not parent or parent in seen or parent not in parents:
            # The chain ends here: no ppid, a snapshot cycle, or a ppid that
            # names no live process (the usual case -- a dead ancestor, or the
            # system root). The host is simply not above us: a different
            # launcher, a wrapper, a remote session. Honest empty.
            #
            # ``parent not in parents`` is checked BEFORE asking for the
            # parent's creation time on purpose. Without it a chain that
            # merely LEFT the visible tree would be reported as
            # "creation time unavailable" -- a true statement about the wrong
            # thing, and exactly the "cannot tell from where" the reasons
            # exist to prevent.
            return "", REASON_NO_HOST_ANCESTOR, ""
        parent_created = _as_filetime(creation_time(parent))
        if not parent_created:
            return "", REASON_NO_CREATION_TIME, ""
        if parent_created > current_created:
            # PID REUSE INSIDE THE WALK. Neither host rewrites a child's ppid
            # field when the parent dies -- Windows leaves the dead pid sitting
            # there, and Linux reparents the child to init/the subreaper -- so
            # in both cases the pid the walk is about to climb to may since
            # have been REISSUED to an unrelated process. The walk would then
            # climb into a stranger's tree and land on somebody else's host
            # process: a live, plausible, WRONG key. A parent is always older
            # than its child, so a younger "parent" PROVES the link is broken.
            # Refuse rather than guess.
            return "", REASON_ANCESTRY_BROKEN, ""
        current, current_created = parent, parent_created

    return "", REASON_NO_HOST_ANCESTOR, ""


def derive_window_key(
    pid: int | None = None,
    *,
    process_table: Callable[[], list[tuple[int, int, str]] | None] | None = None,
    creation_time: Callable[[int], int | None] | None = None,
    host_name: str | None = None,
) -> tuple[str, str]:
    """``(window_key, reason)`` for ``pid`` (default: this process).

    EXACTLY ONE of the two is non-empty, always. A key is
    ``"<host pid>:<host age>"``; a reason is one of the ``REASON_*`` constants
    above. There is no third outcome and no fallback: an unprovable window is
    ``("", <reason>)``, never a substituted value from another axis.

    THE SECOND HALF IS AN AGE, MEASURED IN WHATEVER UNIT THE HOST CAN PROVE --
    a creation FILETIME on win32, ``starttime`` in clock ticks since boot on
    linux, ``pbi_start_tvsec``/``tvusec`` composed to microseconds on darwin.
    Its unit is deliberately NOT part of the contract: what the key needs from
    it is that it is FIXED for the life of the process and DIFFERENT for a
    recycled pid, and all three hosts supply that. Keys are never compared
    across hosts.

    ``process_table`` / ``creation_time`` are the MEASUREMENT SEAM, and
    ``host_name`` names the image to look for. Passing them supplies a
    derivation directly, which is how the tests reproduce pid reuse and broken
    ancestry instead of hoping for them, and how one box can exercise another
    host's matching. Passing NONE of them means "use the derivation for THIS
    host" -- win32, linux and darwin have one, each answering its own named
    reason when its measurement source is unavailable, and every other platform
    answers ``REASON_NOT_WIN32`` rather than borrowing one. They are not
    reachable from any tool surface.
    """
    key, reason, _source = _derive_with_source(
        pid,
        process_table=process_table,
        creation_time=creation_time,
        host_name=host_name,
    )
    return key, reason


def resolve_window_key(
    pid: int | None = None,
    *,
    process_table: Callable[[], list[tuple[int, int, str]] | None] | None = None,
    creation_time: Callable[[int], int | None] | None = None,
    host_name: str | None = None,
) -> str:
    """The key half of :func:`derive_window_key`. ``""`` when unprovable."""
    return derive_window_key(
        pid,
        process_table=process_table,
        creation_time=creation_time,
        host_name=host_name,
    )[0]


def window_key_detail(
    pid: int | None = None,
    *,
    process_table: Callable[[], list[tuple[int, int, str]] | None] | None = None,
    creation_time: Callable[[int], int | None] | None = None,
    host_name: str | None = None,
) -> dict[str, Any]:
    """The key WITH its provenance, for diagnostics that must name their source.

    Modelled on ``ai_whoami``'s labelled channels and on
    ``_stamp_provenance_current``'s ``(bool, reason)``: a bare value that cannot
    say where it came from is the thing being removed. On failure the component
    fields are 0 and ``source`` is "" -- there is nothing to report, and
    reporting a half-derived pid would be the bare-pid mutant by another route.

    ``source`` IS READ FROM THE DERIVATION, NEVER HARDCODED. There is more than
    one derivation now; a key walked out of ``/proc`` and stamped
    ``win32_ancestry_walk`` would be a FORGED provenance, and this field exists
    for the sole purpose of not having one.

    ``host_created_filetime`` keeps its name for compatibility with the rows
    and payloads already written under it. On linux the value it carries is
    ``starttime`` in clock ticks since boot; on darwin it is the process start
    time in microseconds -- the same ROLE, three different units. ``source`` is
    what says which, and is the field to read before comparing two of these
    across hosts (which nothing should do).
    """
    key, reason, source = _derive_with_source(
        pid,
        process_table=process_table,
        creation_time=creation_time,
        host_name=host_name,
    )
    if not key:
        return {
            "window_key": "",
            "host_pid": 0,
            "host_created_filetime": 0,
            "reason": reason,
            "source": "",
        }
    host_pid, _, created = key.partition(":")
    return {
        "window_key": key,
        "host_pid": int(host_pid),
        "host_created_filetime": int(created),
        "reason": "",
        "source": source,
    }


# ── Carrying the window across the process boundary ───────────────────────
#
# THE DERIVATION HAPPENS EXACTLY ONCE, IN THE PROCESS CLAUDE CODE SPAWNED, and
# then travels. It cannot be redone downstream, and the reason is a measured
# property of this system rather than a style preference:
#
#   `claude_hook` asks the RESIDENT BROKER to evaluate the event, and the broker
#   is hosted by the WATCHDOG (hook_broker.py:11 -- "the watchdog -- not
#   `mcp_server --http` -- hosts the broker"). So `hook_pipeline.on_session_start`
#   usually executes inside the watchdog. An ancestry walk there would read the
#   WATCHDOG's ancestry -- and `aidocs service start` is routinely run from a
#   Claude Code window's Bash, which makes the watchdog a DESCENDANT of a
#   claude.exe. The walk would then SUCCEED and name whichever window happened to
#   start the daemon, for every SessionStart from every window.
#
# A plausible wrong answer is worse than no answer. So: stamp in the hook
# process, read from the payload in the daemon, and never derive downstream.

#: The payload field the hook process stamps and the daemon reads.
PAYLOAD_FIELD = "aidocs_window"


def stamp_payload_window(
    payload: dict,
    *,
    pid: int | None = None,
    process_table: Callable[[], list[tuple[int, int, str]] | None] | None = None,
    creation_time: Callable[[int], int | None] | None = None,
) -> dict:
    """Record THIS process's window onto ``payload``, and return the detail.

    REPLACES the field wholesale rather than filling it in. The payload arrives
    on stdin; in practice only the host writes it, but a value this process did
    not measure is not a measurement, and deferring to one would make the field
    forgeable by anything that can reach the hook entrypoint.

    Always writes the field, including on failure — the REASON is the useful
    half of an unprovable window, and a missing field would be
    indistinguishable from an older hook binary that never stamped at all.
    """
    detail = window_key_detail(
        pid, process_table=process_table, creation_time=creation_time
    )
    try:
        payload[PAYLOAD_FIELD] = detail
    except Exception:  # noqa: BLE001 -- a hostile payload must not break a hook
        pass
    return detail


def window_from_payload(payload: object) -> tuple[str, str]:
    """``(window_key, reason)`` READ off a hook payload. Never derives.

    This is the ONLY way a daemon-side caller may learn the window, and it is a
    pure read for the reason spelled out above: the process asking is very
    likely not the process that lives in the window.
    """
    try:
        field = (payload or {}).get(PAYLOAD_FIELD)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        field = None
    if not isinstance(field, dict):
        return "", REASON_NO_PAYLOAD_WINDOW
    raw = field.get("window_key")
    if not isinstance(raw, str) or not raw.strip():
        # Prefer the stamper's own reason when it left one: it knows WHY far
        # better than this reader can.
        carried = field.get("reason")
        carried = carried.strip() if isinstance(carried, str) else ""
        return "", carried or REASON_NO_PAYLOAD_WINDOW
    return raw.strip(), ""
