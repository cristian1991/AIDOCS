"""Immutable runtime GENERATIONS and one atomic activation pointer (#1030).

THE DEFECT THIS REPLACES. `runtime_provisioner._install_package_into_venv`
phase C is "NOT ATOMIC, MERELY NARROW" by its own docstring: it renames the
serving `aidocs_mcp` tree aside and unpacks a wheel in its place. #589 already
moved the expensive build ahead of that rename, shrinking the outage from
minutes of C-extension compilation to one wheel unpack — and that is as far as
narrowing can go. WHILE THE SWAP MUTATES THE ONLY SERVING TREE, A NONZERO
WINDOW IS FUNDAMENTAL.

Two states can be observed inside that window, and they are not equally bad:

  MISSING   `import aidocs_mcp` fails outright. The external shim catches it
            and DENIES (#589/#616), so the gate holds. Costly, not unsafe.
  PARTIAL   the package is half-written: the hook ENTERS, then dies reaching
            for a submodule that has not landed. #932 measured four calls that
            ran UNGOVERNED this way, before the shim learned to convert a
            crash into a deny. A partial import is the dangerous one, because
            "loadable enough to start, not complete enough to finish" produces
            NO VERDICT.

MIXED is the third, and versioning only `aidocs_mcp/` would create it: a
dependency floor bump yields old-package-with-new-dependency, or the reverse.
So the unit of versioning is THE WHOLE RUNTIME — interpreter environment,
package and dependencies together.

THE SHAPE

    runtime/
      claude_hook_shim.py     stable stdlib-only launcher (never swapped)
      current.json            the activation pointer, replaced atomically
      generations/
        <gen-id>/             one COMPLETE runtime; immutable once activated
          venv/
          cpython/
      venv/                   LEGACY single tree — still honoured, see below

Update becomes: build B → install every byte into B → verify B by importing and
running it → verify provenance → atomically point current.json at B → new
calls use B, calls already inside A finish on A → keep A for rollback → collect
A later. NOTHING SERVING TRAFFIC IS RENAMED, DELETED OR OVERWRITTEN.

THE POINTER IS READ EXACTLY ONCE PER INVOCATION. A caller that re-read it
mid-flight could start under A and import the rest from B — the MIXED state
above, arrived at from the other direction. `read_pointer` is therefore a pure
read returning a snapshot, and callers hold that snapshot.

WHY A FILE AND NOT A SYMLINK. `os.replace` is atomic on POSIX and Windows
alike; a directory junction flip is not reliably atomic on Windows, and this
code must be correct on the operator's box, which is Windows.

MIGRATION IS NOT A FLAG DAY. With no `current.json`, `active_runtime` answers
with the legacy `runtime/venv` and says so in its reason. An install that
predates generations keeps working untouched, and the first generation-aware
refresh is what moves it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import NamedTuple

#: The pointer filename, relative to the runtime root. Duplicated verbatim in
#: `claude_hook_shim` — which is stdlib-only and lives OUTSIDE site-packages
#: precisely so a package swap cannot take it away, and therefore cannot import
#: this module. `test_runtime_generation_pointer_1030.py` holds the two copies
#: in lockstep, the same way the #589 posture table is held.
POINTER_FILENAME = "current.json"
GENERATIONS_DIRNAME = "generations"
LEGACY_VENV_DIRNAME = "venv"

#: A generation id is filesystem-safe and content-addressed by the caller. The
#: shape is validated on WRITE and on READ: a pointer naming `../..` would
#: otherwise activate an arbitrary directory.
GENERATION_ID_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: Reasons — one per cause, never a bare "unavailable". A refusal that cannot
#: say WHY is what sent readers chasing a remedy that could not help.
REASON_LEGACY_TREE = "no_pointer_using_legacy_venv"
REASON_NO_POINTER_NO_TREE = "no_pointer_and_no_legacy_venv"
REASON_POINTER_UNREADABLE = "pointer_unreadable"
REASON_POINTER_MALFORMED = "pointer_malformed"
REASON_GENERATION_ID_INVALID = "pointer_names_invalid_generation_id"
REASON_GENERATION_MISSING = "pointer_names_a_generation_that_is_not_on_disk"
REASON_GENERATION_INCOMPLETE = "generation_present_but_not_marked_complete"

#: Written into a generation ONLY after every byte is in place. Its absence is
#: what distinguishes a half-built directory from a usable runtime, so a kill
#: mid-build can never be mistaken for a finished generation.
COMPLETE_MARKER = "generation.complete.json"


# ── `ActiveRuntime` / `active_runtime` WERE HERE, AND WERE THE TRAP ─────────
#
# They resolved the pointer into one object and answered None on every failure.
# That single None covered two situations a caller MUST distinguish:
#
#   no pointer      a legacy install; `runtime/venv` IS the runtime, serving it
#                   is correct;
#   broken pointer  a generation was activated and is missing/unsealed.
#
# `runtime_provisioner._venv_dir` merged them exactly as the shape invited, and
# fell back to the legacy tree on a BROKEN pointer — which on a migrated box is
# the pre-migration runtime, so the machine would have resumed enforcing old
# code with every surface reporting a healthy venv tier.
#
# `serving_venv` replaces both and returns `(path_or_None, reason)`, so the two
# cases cannot collapse into one another silently. The old pair is deleted
# rather than left beside it: a helper whose None means two things, kept around
# right after that ambiguity caused a substitution bug, is a loaded trap for the
# next caller.


def runtime_root(home: Path | str) -> Path:
    """The runtime root for ``home``, honouring ``AIDOCS_RUNTIME_ROOT``.

    THE OVERRIDE IS NOT A CONVENIENCE — it is the shim's copy of this same
    contract, and the shim reads it. If only one of the two honoured it, an
    operator (or a test) redirecting the runtime would have the launcher and
    the updater disagreeing about which pointer is authoritative, which is the
    split-brain this module exists to prevent.
    """
    override = os.environ.get("AIDOCS_RUNTIME_ROOT")
    if override:
        return Path(override)
    return Path(home) / ".aidocs" / "runtime"


def pointer_path(home: Path | str) -> Path:
    return runtime_root(home) / POINTER_FILENAME


def generations_root(home: Path | str) -> Path:
    return runtime_root(home) / GENERATIONS_DIRNAME


def generation_dir(home: Path | str, generation_id: str) -> Path | None:
    """The directory for ``generation_id``, or ``None`` if the id is not a
    legal generation name. Never joins an unvalidated segment onto a path."""
    gid = str(generation_id or "").strip()
    if not GENERATION_ID_SHAPE.fullmatch(gid):
        return None
    return generations_root(home) / gid


def is_complete(home: Path | str, generation_id: str) -> bool:
    """Has every byte of this generation landed AND been sealed?

    A directory is not a generation until the marker exists. Killing the
    updater mid-build therefore leaves something that can never be activated,
    rather than something that looks activatable and is not.
    """
    gdir = generation_dir(home, generation_id)
    return bool(gdir and (gdir / COMPLETE_MARKER).is_file())


def mark_complete(home: Path | str, generation_id: str, detail: dict | None = None) -> bool:
    """Seal a fully-built generation. LAST write of the build, always."""
    gdir = generation_dir(home, generation_id)
    if gdir is None or not gdir.is_dir():
        return False
    payload = {"generation_id": generation_id, **(detail or {})}
    return _atomic_write_json(gdir / COMPLETE_MARKER, payload)


def read_pointer(home: Path | str) -> tuple[str, str]:
    """``(generation_id, reason)`` — a PURE read, never a repair.

    Empty id with a reason means "no generation is activated", which is a
    legitimate state on an install that predates generations; see
    ``active_runtime``, which turns that into the legacy tree.
    """
    p = pointer_path(home)
    if not p.is_file():
        return "", REASON_LEGACY_TREE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "", REASON_POINTER_UNREADABLE
    if not isinstance(raw, dict):
        return "", REASON_POINTER_MALFORMED
    gid = str(raw.get("generation_id") or "").strip()
    if not gid:
        return "", REASON_POINTER_MALFORMED
    if not GENERATION_ID_SHAPE.fullmatch(gid):
        return "", REASON_GENERATION_ID_INVALID
    return gid, ""




def loaded_generation(module_file: Path | str | None = None) -> str | None:
    """WHICH GENERATION THESE BYTES CAME FROM — read off the loaded path.

    ATTEST FROM WHAT WAS LOADED, NOT FROM THE POINTER. The pointer answers
    "what should serve now"; it can move at any instant, including between a
    child being spawned and anyone asking what that child is. A process that
    baselines itself by reading the pointer therefore records the wrong answer
    whenever it loses that race — it started under A, the pointer already said
    B, and it will call itself B and never notice it is stale.

    The path a module was imported FROM cannot lose that race: it is a fact
    about this process, fixed at import, and it stays true no matter how many
    times the pointer moves afterwards.

    Returns the generation id when the given file lives under
    ``.../generations/<id>/``, or None for a legacy install (and for anything
    unparseable — this is an attestation input, and a guess is worse than an
    honest "not from a generation").
    """
    try:
        parts = Path(module_file or __file__).resolve().parts
    except Exception:
        return None
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == GENERATIONS_DIRNAME:
            candidate = parts[i + 1]
            return candidate if GENERATION_ID_SHAPE.fullmatch(candidate) else None
    return None


class Serving(NamedTuple):
    """ONE POINTER SNAPSHOT: what may serve, which generation it is, and why not.

    THE ID BELONGS IN THE SNAPSHOT. The first cut returned only
    ``(venv, reason)``, so every caller that also needed the generation id had
    to call ``read_pointer`` a SECOND time — and both did:
    ``aidocs_service.child_python`` (to label the interpreter it had just
    resolved) and ``runtime_provisioner._venv_dir`` (to name the directory a
    broken pointer pointed at). A flip landing between those two reads returns
    A's interpreter LABELLED B, or the legacy python after B is already active.
    That is the same "the pointer can move between any two instants" hazard the
    frozen-argv work removed from the spawn path, still live one layer down —
    and a wrong LABEL is the more dangerous half, because every downstream
    attestation then agrees with itself while naming the wrong runtime.

    Returning all three from one read makes the second read unnecessary, and
    the tuple is immutable so the answer cannot drift while it is held.
    """

    venv: Path | None
    generation_id: str
    reason: str


def serving_venv(home: Path | str) -> Serving:
    """WHICH VENV MAY SERVE, which generation that is, and why not — one read.

    THE NO-SUBSTITUTION RULE, made explicit because it was previously implied
    and therefore broken. A single ``None`` answer covers two very different
    situations, and a caller that cannot tell them apart will merge them:

      NO POINTER      a legacy install that predates generations. The legacy
                      ``runtime/venv`` IS the runtime; serving it is correct,
                      and ``generation_id`` is "" because there genuinely is
                      none.
      BROKEN POINTER  the operator activated a specific generation and it is
                      unreadable, malformed, absent or unsealed. ``venv`` is
                      None, ``reason`` says which, and ``generation_id`` still
                      carries the id the pointer NAMED where one was legible —
                      so a caller can report or locate it WITHOUT going back to
                      the pointer for a second, possibly different answer.

    Falling back to the legacy tree in the SECOND case is a substitution, and
    on a migrated box it is the worst kind: ``runtime/venv`` there is the
    PRE-MIGRATION runtime, so the machine would quietly resume enforcing old
    code while every surface reported a healthy venv tier.

    Returns a DIRECTORY, not an interpreter: callers still probe it for a
    python, so "the generation is there but its venv is broken" stays their
    verdict rather than one this function guesses at.

    EXACTLY ONE ``read_pointer`` CALL, and
    ``test_supervisor_is_generation_aware_1030`` proves it adversarially by
    flipping the pointer between reads and counting them.
    """
    gid, reason = read_pointer(home)
    if not gid:
        if reason == REASON_LEGACY_TREE:
            legacy = runtime_root(home) / LEGACY_VENV_DIRNAME
            if legacy.is_dir():
                return Serving(legacy, "", REASON_LEGACY_TREE)
            return Serving(None, "", REASON_NO_POINTER_NO_TREE)
        # The pointer EXISTS and cannot be trusted. No fallback.
        return Serving(None, "", reason)
    gdir = generation_dir(home, gid)
    if gdir is None:
        return Serving(None, gid, REASON_GENERATION_ID_INVALID)
    if not gdir.is_dir():
        return Serving(None, gid, REASON_GENERATION_MISSING)
    if not is_complete(home, gid):
        return Serving(None, gid, REASON_GENERATION_INCOMPLETE)
    return Serving(gdir / LEGACY_VENV_DIRNAME, gid, "")


def activate(home: Path | str, generation_id: str) -> tuple[bool, str]:
    """Point ``current.json`` at a SEALED generation. ``(ok, reason)``.

    REFUSES to activate anything that is not complete, so a failed or
    interrupted build cannot be promoted by a stray call. The write itself is
    ``os.replace`` of a fully-written temp file: a reader either sees the whole
    old pointer or the whole new one, never a partial line.
    """
    gid = str(generation_id or "").strip()
    gdir = generation_dir(home, gid)
    if gdir is None:
        return False, REASON_GENERATION_ID_INVALID
    if not gdir.is_dir():
        return False, REASON_GENERATION_MISSING
    if not is_complete(home, gid):
        return False, REASON_GENERATION_INCOMPLETE
    ok = _atomic_write_json(pointer_path(home), {"generation_id": gid})
    return (ok, "" if ok else REASON_POINTER_UNREADABLE)


def _atomic_write_json(dest: Path, payload: dict) -> bool:
    """Write JSON so a reader never sees a partial file.

    Temp file in the SAME directory (os.replace is only atomic within a
    filesystem), flushed and fsynced before the rename, so a crash between
    write and replace leaves the previous file intact rather than a truncated
    one.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".aidocs-ptr-", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
    except Exception:
        return False
