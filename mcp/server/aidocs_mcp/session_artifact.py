"""Exact session-bound, expiring task/deploy artifact recognition.

Replaces the earlier broad ``<TEMP>/claude/**/tasks/*`` SHAPE exemption (which
matched ANY session, ANY project) with a binding to THIS session. A Claude
Code task or deploy output capture is a readable artifact ONLY when:

  * it sits under ``<TEMP>/claude/<project-slug>/<session-uuid>/tasks/`` (the
    harness layout) and ends in a safe stdout-capture extension,
  * its ``<project-slug>`` matches the CURRENT project_root (case-insensitive
    — the harness varies the slug casing by location),
  * its ``<session-uuid>`` is one of the CURRENT session ids (the host
    session id stamped by the hook as ``last_cli_session_id``, and/or the
    managed session id), and
  * it is FRESH — file mtime within the TTL (expiring: a stale capture from a
    previous run refuses).

Everything else refuses: another session's UUID, another project's slug, a
stale artifact, a secret-named file (no safe extension), a ``..`` traversal,
or an arbitrary TEMP capture. The sensitive/secret/traversal floors still
hard-deny secrets regardless of this check.

Side-effect free except a single ``os.stat`` for the freshness check;
unit-testable on path strings + temp files.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

# Safe stdout/stderr capture extensions. Secret-bearing extensions
# (.env/.pem/.key) are deliberately absent; the sensitive floor blocks those
# by name regardless.
_SAFE_TASK_EXTS: tuple[str, ...] = (
    ".output",
    ".out",
    ".log",
    ".txt",
    ".json",
    ".jsonl",
    ".err",
    ".status",
)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)

# Task/deploy logs are transient — expire within a day. A capture older than
# this is "stale" and refuses (re-run to get a fresh one).
DEFAULT_TTL_SECONDS = 86_400


def project_slug(project_root: str | os.PathLike) -> str:
    """The harness project-dir slug: drive ``:`` and path separators replaced
    by ``-``. Casing varies by harness location, so callers compare
    case-insensitively (this returns the lowercased form)."""
    return re.sub(r"[:\\/]", "-", str(project_root)).strip("-").lower()


def _norm(p: str | os.PathLike) -> str:
    return str(p).replace("\\", "/").strip()


# Windows reparse-point flag (junctions + symlinks both carry it). Absent on
# POSIX, where S_ISLNK is the symlink signal.
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse_or_link(p: Path) -> bool:
    """True when ``p`` itself (NOT its target) is a symlink, junction, or other
    reparse point — i.e. path-laundering. lstat does not follow links; an
    unstat-able component fails closed (treated as a reparse hazard)."""
    try:
        st = p.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs and (attrs & _REPARSE))


def is_session_task_artifact(
    path: str | os.PathLike,
    *,
    project_root: str | os.PathLike | None,
    host_session_ids: Iterable[str] | None,
    now_ts: float | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """True iff ``path`` is the CURRENT session's fresh task/deploy output.

    See the module docstring for the exact binding. Returns False (refuse) on
    any mismatch, an unparseable path, a missing file, or empty session ids.
    """
    if not path or not project_root:
        return False
    n = _norm(path)
    if not n or ".." in n.split("/"):
        return False
    sessions = {str(s).strip().lower() for s in (host_session_ids or []) if str(s).strip()}
    if not sessions:
        return False
    try:
        temp_root = _norm(tempfile.gettempdir()).rstrip("/").lower() + "/claude/"
    except Exception:
        return False
    if not n.lower().startswith(temp_root):
        return False

    # EXACT layout: <temp>/claude/<slug>/<uuid>/tasks/<single-capture-file>.
    # Exactly four segments after `claude/` — no nested smuggling, no extra
    # directories, no slug/uuid appearing as an incidental segment elsewhere.
    tail = [seg for seg in n[len(temp_root):].split("/") if seg]
    if len(tail) != 4:
        return False
    slug_seg, uuid_seg, tasks_seg, fname = tail
    if slug_seg.lower() != project_slug(project_root):
        return False
    if not _UUID_RE.match(uuid_seg) or uuid_seg.lower() not in sessions:
        return False
    if tasks_seg.lower() != "tasks":
        return False
    if not fname or not fname.lower().endswith(_SAFE_TASK_EXTS):
        return False

    # PHYSICAL isolation: every component from <temp>/claude down to the file
    # must be a real directory / regular file — never a symlink, junction, or
    # other reparse point (which could launder a read into another session,
    # another project, or outside TEMP entirely).
    base = Path(tempfile.gettempdir()) / "claude"
    for seg in (slug_seg, uuid_seg, tasks_seg, fname):
        base = base / seg
        if _is_reparse_or_link(base):
            return False
    try:
        st = base.stat()  # follows nothing now (no reparse survived the walk)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False  # non-regular: dir, FIFO, device, socket

    # Freshness, EXPIRING and monotonic: enforce 0 <= age <= ttl. A future-dated
    # mtime (age < 0, clock-rolled or forged) and a stale capture both refuse.
    cur = now_ts if now_ts is not None else time.time()
    age = cur - st.st_mtime
    return 0 <= age <= ttl_seconds
