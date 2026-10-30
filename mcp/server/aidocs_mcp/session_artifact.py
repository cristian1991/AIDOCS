"""Exact session-bound, expiring task/deploy artifact recognition.

Replaces the earlier broad ``<TEMP>/claude/**/tasks/*`` SHAPE exemption (which
matched ANY session, ANY project) with a binding to THIS session. A Claude
Code task or deploy output capture is a readable artifact ONLY when:

  * it sits under ``<TEMP>/claude/<project-slug>/<session-uuid>/tasks/`` (the
    harness layout) and ends in a safe stdout-capture extension,
  * its ``<project-slug>`` matches the CURRENT project_root (case-insensitive
    — the harness varies the slug casing by location),
  * its ``<session-uuid>`` is one of the CURRENT session ids (the host
    session id stamped by the hook as ``last_host_session_id``, the managed
    session id, and/or — #464 — the session's owned host-id chain: every
    host uuid the authenticated hooks stamped for this managed session plus
    the harness transcript-dir uuid, so a caller keeps reading its OWN task
    output after a CLI resume rotates the host id), and
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
# Bounded allowance for a just-written file whose fs mtime sits slightly
# AHEAD of this process's clock (timestamp granularity / NTP nudge / xdist
# load). Anything further future-dated still refuses as clock-rolled/forged.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5.0


def project_slug(project_root: str | os.PathLike) -> str:
    """The harness project-dir slug: drive ``:`` and path separators replaced
    by ``-``. Casing varies by harness location, so callers compare
    case-insensitively (this returns the lowercased form)."""
    return re.sub(r"[:\\/]", "-", str(project_root)).strip("-").lower()


def _norm(p: str | os.PathLike) -> str:
    return str(p).replace("\\", "/").strip()


def usable_session_uuids(host_session_ids: Iterable[str] | None) -> set[str]:
    """The subset of ``host_session_ids`` that can ACTUALLY decide ownership of
    a ``<temp>/claude/<slug>/<uuid>/…`` path: the UUID-shaped ones, lowercased.

    #672: ``host_session_ids`` is a MIXED-AXIS BAG — element 0 is the AIDOCS
    session SLUG, the rest are harness host uuids plus the transcript-dir uuid
    (``tool_gate_service._gate_state``, ``agent_orchestrator._host_session_ids``).
    The slug can NEVER equal a UUID path segment, so it is DEAD EVIDENCE here —
    yet its presence made the bag non-empty and therefore satisfied the
    fail-closed emptiness guards below, flipping them from "refuse, cannot
    prove identity" to "proceed and compare" against a set holding nothing
    comparable. Every such comparison yields "foreign", which is how an agent
    was refused the output of a task IT LAUNCHED ITSELF.

    Filtering to the usable axis makes the emptiness guard HONEST: an empty
    result means "ownership cannot be established" — a different fact from
    "established, and it is foreign".
    """
    out: set[str] = set()
    for raw in host_session_ids or []:
        s = str(raw).strip().lower()
        if s and _UUID_RE.match(s):
            out.add(s)
    return out


# Ownership verdicts for a path under the managed per-session workspace.
OWNERSHIP_OWN = "own"
OWNERSHIP_FOREIGN = "foreign"
OWNERSHIP_UNESTABLISHED = "unestablished"
OWNERSHIP_LAUNDERED = "laundered"
OWNERSHIP_NOT_APPLICABLE = "not_applicable"


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
    # #672: only UUID-shaped ids can ever match the <uuid> path segment, so a
    # bag of DEAD elements (the AIDOCS slug) must NOT satisfy this fail-closed
    # guard. Empty here means "ownership unestablishable", not "foreign".
    sessions = usable_session_uuids(host_session_ids)
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

    # Freshness, EXPIRING and monotonic: enforce -skew <= age <= ttl. A
    # MEANINGFULLY future-dated mtime (clock-rolled or forged) and a stale
    # capture both refuse — but a just-written artifact's fs timestamp can
    # legitimately sit a hair AHEAD of this process's clock (filesystem
    # timestamp granularity / NTP nudge / xdist load), and the old hard
    # `0 <= age` refused the caller's own fresh output (the Gate-2b flake
    # War U reported on test_same_session_fresh_artifact_allowed). The
    # bounded tolerance changes nothing an attacker can use: forging
    # WITHIN the ttl was always accepted-by-design (content floors still
    # run), and a rolled-back clock still refuses beyond the bound.
    cur = now_ts if now_ts is not None else time.time()
    age = cur - st.st_mtime
    return -_CLOCK_SKEW_TOLERANCE_SECONDS <= age <= ttl_seconds


def is_foreign_session_workspace(
    path: str | os.PathLike,
    *,
    host_session_ids: Iterable[str] | None,
) -> bool:
    """True iff ``path`` targets ANOTHER session's workspace subtree under
    ``<temp>/claude/<slug>/<uuid>/…`` — i.e. the ``<uuid>`` directory segment is
    a valid session UUID that is NOT one of the caller's ``host_session_ids``.

    #279 cross-session scratchpad WRITE isolation. #266 approved the whole
    ``<TEMP>/claude/`` subtree as the agent's own workspace zone, so a write
    there no longer hits the sensitive-zone block. But a write into a SIBLING
    session's ``<slug>/<uuid>/`` subtree is a context-poisoning vector: the
    sibling re-reads its own task/scratch outputs as trusted. Reads are already
    session-bound (host-read rail); the write gate mirrors that binding via this
    detector.

    Pure PATH-SHAPE (no filesystem access) so it fires on a write TARGET that
    does not exist yet. Returns False — do NOT newly block — when: the path is
    not under ``<temp>/claude/``, contains ``..``, the ``<uuid>`` segment is not
    UUID-shaped, the caller has no known session ids (cannot prove foreignness),
    or the uuid IS the caller's own (own-session writes stay allowed).
    """
    if not path:
        return False
    n = _norm(path)
    if not n or ".." in n.split("/"):
        return False
    sessions = {str(s).strip().lower() for s in (host_session_ids or []) if str(s).strip()}
    if not sessions:
        return False  # cannot prove foreign without our own identity
    try:
        temp_root = _norm(tempfile.gettempdir()).rstrip("/").lower() + "/claude/"
    except Exception:
        return False
    if not n.lower().startswith(temp_root):
        return False
    # Layout: <temp>/claude/<slug>/<uuid>/…  — the session dir is segment 1
    # (index 1) after ``claude/`` (same depth is_session_task_artifact binds).
    tail = [seg for seg in n[len(temp_root):].split("/") if seg]
    if len(tail) < 2:
        return False
    uuid_seg = tail[1].lower()
    if not _UUID_RE.match(uuid_seg):
        return False  # not the session-uuid layout — not this gate's concern
    return uuid_seg not in sessions


def session_workspace_write_verdict(
    path: str | os.PathLike,
    *,
    host_session_ids: Iterable[str] | None,
) -> str:
    """The WRITE-side counterpart of ``session_workspace_ownership``: which
    ownership fact holds for a write TARGET under ``<temp>/claude/<slug>/<uuid>/``.

    Returns ``OWNERSHIP_NOT_APPLICABLE`` / ``OWNERSHIP_LAUNDERED`` /
    ``OWNERSHIP_UNESTABLISHED`` / ``OWNERSHIP_FOREIGN`` / ``OWNERSHIP_OWN``.

    Differences from the read-side verdict, both deliberate:

    * PURE PATH-SHAPE — no ``stat``, no project-slug comparison. A write target
      need not exist yet, so the physical checks the read side runs (regular
      file, reparse walk) have nothing to inspect; and the slug segment was
      never an ownership axis for writes (#279 is about sibling SESSIONS).
    * ``..`` under ``<temp>/claude/`` is ``OWNERSHIP_LAUNDERED``, not "not our
      concern". ``is_foreign_session_workspace`` returned False there — segment
      indices are meaningless once a ``..`` is in play — but
      ``<own-uuid>/../<sibling-uuid>/x`` resolves INTO a sibling. (Measured: the
      orchestrator's upstream ``path_input_conflict`` rail already refuses this
      shape today, so this is depth, not a live hole.)

    #672 C.3: the emptiness question is asked on the USABLE (UUID-shaped) ids,
    and its answer is ``unestablished`` — a refusal, distinct from ``foreign``.
    This never loosens the #279 check: ``usable`` is a SUBSET of the raw bag, so
    any uuid absent from the raw bag (the old block condition) is absent from
    the usable set too and still lands on ``foreign``; and a bag with no usable
    id, which the old code compared against and therefore always called
    ``foreign``, now lands on ``unestablished`` — still refused, just no longer
    falsely accused.
    """
    if not path:
        return OWNERSHIP_NOT_APPLICABLE
    n = _norm(path)
    if not n:
        return OWNERSHIP_NOT_APPLICABLE
    try:
        temp_root = _norm(tempfile.gettempdir()).rstrip("/").lower() + "/claude/"
    except Exception:
        return OWNERSHIP_NOT_APPLICABLE
    if not n.lower().startswith(temp_root):
        return OWNERSHIP_NOT_APPLICABLE
    if ".." in n.split("/"):
        return OWNERSHIP_LAUNDERED
    tail = [seg for seg in n[len(temp_root):].split("/") if seg]
    if len(tail) < 2:
        return OWNERSHIP_NOT_APPLICABLE
    uuid_seg = tail[1].lower()
    if not _UUID_RE.match(uuid_seg):
        return OWNERSHIP_NOT_APPLICABLE
    sessions = usable_session_uuids(host_session_ids)
    if not sessions:
        return OWNERSHIP_UNESTABLISHED
    return OWNERSHIP_OWN if uuid_seg in sessions else OWNERSHIP_FOREIGN


def session_workspace_ownership(
    path: str | os.PathLike,
    *,
    project_root: str | os.PathLike | None,
    host_session_ids: Iterable[str] | None,
) -> str:
    """Decide WHO OWNS ``path`` under ``<temp>/claude/<slug>/<uuid>/…`` and say
    which of the mutually-exclusive facts holds. One of:

    ``OWNERSHIP_UNESTABLISHED``
        No usable (UUID-shaped) id is known for this caller, so ownership
        cannot be checked EITHER WAY. #672: this is NOT evidence of
        foreignness, and before this function existed the two were reported
        with the same message — the gate asserted "another session's" about a
        file it had no ability to attribute at all.
    ``OWNERSHIP_NOT_APPLICABLE``
        Not the per-session workspace layout (outside ``<temp>/claude/``, or
        too few segments, or a non-UUID session segment).
    ``OWNERSHIP_FOREIGN``
        Established: the slug is another project's, or the session uuid is not
        one of ours.
    ``OWNERSHIP_LAUNDERED``
        Ours by name, but the path is not a real regular file reached without
        a ``..`` segment and without a symlink/junction/reparse component —
        i.e. it could be laundering a read out of the workspace. Refused
        regardless of ownership.
    ``OWNERSHIP_OWN``
        Established: the caller's own workspace, physically verified.

    Ownership evidence is the session's owned host-id chain (stamped from
    AUTHENTICATED hook payloads by ``tool_gate_service._stamp_owned_host_ids``);
    a foreign uuid can never enter this session's chain.
    """
    if not path:
        return OWNERSHIP_NOT_APPLICABLE
    n = _norm(path)
    if not n:
        return OWNERSHIP_NOT_APPLICABLE
    try:
        temp_root = _norm(tempfile.gettempdir()).rstrip("/").lower() + "/claude/"
    except Exception:
        return OWNERSHIP_NOT_APPLICABLE
    if not n.lower().startswith(temp_root):
        return OWNERSHIP_NOT_APPLICABLE
    if ".." in n.split("/"):
        return OWNERSHIP_LAUNDERED
    tail = [seg for seg in n[len(temp_root):].split("/") if seg]
    # Need at least <slug>/<uuid>/<file-or-subdir…> and a terminal file name.
    if len(tail) < 3:
        return OWNERSHIP_NOT_APPLICABLE
    slug_seg, uuid_seg = tail[0], tail[1]
    if not _UUID_RE.match(uuid_seg):
        return OWNERSHIP_NOT_APPLICABLE
    # The emptiness question comes BEFORE any comparison: with nothing usable
    # to compare against, "foreign" would be a fabricated conclusion.
    sessions = usable_session_uuids(host_session_ids)
    if not sessions:
        return OWNERSHIP_UNESTABLISHED
    if not project_root:
        return OWNERSHIP_UNESTABLISHED
    if slug_seg.lower() != project_slug(project_root):
        return OWNERSHIP_FOREIGN
    if uuid_seg.lower() not in sessions:
        return OWNERSHIP_FOREIGN
    # PHYSICAL isolation: no component may be a symlink/junction/reparse point
    # (which could launder the read outside the session workspace). The file
    # itself must exist and be a regular file.
    base = Path(tempfile.gettempdir()) / "claude"
    for seg in tail:
        base = base / seg
        if _is_reparse_or_link(base):
            return OWNERSHIP_LAUNDERED
    try:
        st = base.stat()
    except OSError:
        return OWNERSHIP_LAUNDERED
    if not stat.S_ISREG(st.st_mode):
        return OWNERSHIP_LAUNDERED
    return OWNERSHIP_OWN


def is_own_session_workspace(
    path: str | os.PathLike,
    *,
    project_root: str | os.PathLike | None,
    host_session_ids: Iterable[str] | None,
) -> bool:
    """True iff ``path`` is a file inside the CALLER'S OWN session workspace
    under ``<temp>/claude/<slug>/<uuid>/…`` (scratchpad/, tasks/, any subdir).

    #379 (WAR U / #368): the read gate refused the current session's OWN
    Claude Code scratchpad file (``…/<own-uuid>/scratchpad/mode_specs.txt``)
    as "another session's managed task-artifact output".
    ``is_session_task_artifact`` deliberately binds to the exact
    ``…/tasks/<capture>`` layout + TTL, so the harness-mandated scratchpad
    subtree fell through to the cross-session refusal even when the uuid WAS
    the caller's own. This helper proves OWNERSHIP only — the caller still
    runs the sensitive/secret floors and the content sniff before allowing.

    Fail-closed: False on unestablishable identity, a slug that is not the
    CURRENT project, a non-UUID session segment, a foreign uuid, a ``..``
    traversal, or any reparse-point/symlink component (path-laundering).
    Callers that must TELL THOSE APART (the refusal message has to) call
    ``session_workspace_ownership`` directly.
    """
    return (
        session_workspace_ownership(
            path,
            project_root=project_root,
            host_session_ids=host_session_ids,
        )
        == OWNERSHIP_OWN
    )
