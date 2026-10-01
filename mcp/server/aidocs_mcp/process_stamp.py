"""#738 — WHAT DID **THIS PROCESS** LOAD? (the LOADED state, #627 phase 4)

THE DEFECT THIS EXISTS TO KILL. ``ai_version(mode='local')`` promised "the build
ACTUALLY RUNNING on this process" and delivered a LIVE GIT HEAD READ. Measured
2026-08-01: the daemon booted at 21:27, two commits landed after, and the tool
reported ``38f2f3ae`` — a commit the running process has never loaded. Python
caches modules at import: a long-lived process runs the code as it stood when it
started. **DISK IS NOT MEMORY.** The answer was wrong in the REASSURING
direction — it named the NEWEST commit, so it would confirm "yes, that fix is
live" for a fix that is not running, to the one operator who already suspected
staleness. That conflation cost this project two days (#733).

WHY NOT ``build_stamp.py`` — ASKED AND ANSWERED (doctrine VI). ``build_stamp``
is the right and only mechanism for the SHIPPED state, and it says so itself:

    "3. LOADED — the RUNNING process is executing those bytes.  **NOT COVERED,
       AND IT CANNOT BE.** ... Every check in this module reads the FILESYSTEM,
       and the filesystem cannot see what a long-lived process loaded an hour
       ago."                                   -- build_stamp.py, lines 29-34

That is not a gap to be filled there; it is a statement that the subject is
different. A filesystem read taken NOW can never answer a question about MEMORY
taken THEN. So this module adds no second on-disk stamp, writes nothing, and
owns no file: it CONSUMES ``build_stamp`` for its identity and holds the answer
in memory. One stamping mechanism still, one new reader of it.

THE WHOLE MECHANISM IS ONE IDEA: capture ONCE, AT BOOT, IN MEMORY.
``capture_process_stamp()`` is IDEMPOTENT — a second call returns the first
answer unchanged. That idempotency is not an optimisation, it IS the correctness
property: if a later call could re-stamp, the identity would drift back toward
whatever is on disk now, which is the bug.

NO HELPFUL FALLBACK. When the stamp was never taken, ``running_identity()``
reports UNVERIFIED with an empty commit — it does NOT quietly return git HEAD.
An admitted unknown beats a confident wrong answer, and this tool is consulted
precisely when someone already suspects staleness. The posture is copied from
``deploy_build_info()``'s ``stamp_verdict: UNVERIFIED``.
"""

from __future__ import annotations

import os
import sys
import time

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"

# Where the identity came from — recorded so a reader can weigh it.
ORIGIN_BUILD_STAMP = "artefact-build-stamp"
ORIGIN_SOURCE_HEAD = "source-head-at-boot"

_STAMP: dict | None = None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _source_head_at_boot() -> tuple[str, str]:
    """(commit, version) of the SOURCE CHECKOUT, read through the one existing
    resolver. Called only from ``capture_process_stamp`` — i.e. AT BOOT, which
    is the only moment at which a disk read and this process's memory are
    knowably the same thing. Attributes are looked up on the module (not
    imported by name) so a test can substitute them."""
    import aidocs_mcp

    root = aidocs_mcp._source_checkout_root()
    if root is None:
        return "", ""
    return aidocs_mcp._git_head_commit(root) or "", aidocs_mcp._version_from_pyproject()


def _source_build_at_boot() -> int | None:
    """The committed build ticker of the SOURCE CHECKOUT, read at boot.

    DELIBERATELY SEPARATE from ``_source_head_at_boot`` rather than a third
    element of its tuple: that helper's 2-tuple shape is monkeypatched by
    several tests, and widening it would break every patcher for a field they
    do not care about. A new question gets a new function.
    """
    import aidocs_mcp

    return aidocs_mcp._build_from_ticker()


def capture_process_stamp(*, source: str = "boot") -> dict:
    """Freeze the identity THIS PROCESS LOADED. Call once, from server startup.

    IDEMPOTENT BY DESIGN (see module docstring): later calls return the first
    answer. ``source`` records the caller's claim about WHEN it ran; only
    ``"boot"`` can yield VERIFIED, because a capture taken minutes into the
    process's life cannot distinguish the bytes that were imported from bytes
    that replaced them afterwards — the exact confusion this module exists to
    end. Never raises: a provenance reader that can crash is one more way to
    learn nothing.
    """
    global _STAMP
    if _STAMP is not None:
        return _STAMP
    _STAMP = _establish(source)
    return _STAMP


def _establish(source: str) -> dict:
    out = {
        "verdict": UNVERIFIED,
        "reason": "",
        "commit": "",
        "version": "",
        "build": None,
        "origin": "",
        "captured_at": _utc_now(),
        "capture_source": source,
        "pid": os.getpid(),
    }
    # 1. THE ARTEFACT CAN NAME ITSELF — the installed/deployed posture. Reuse
    #    build_stamp rather than inventing a parallel provenance path.
    try:
        from .build_stamp import build_stamp_verdict

        verdict = build_stamp_verdict()
    except Exception as exc:  # noqa: BLE001 — cannot verify == say so
        verdict = {"verdict": UNVERIFIED, "reason": f"{type(exc).__name__}", "commit": ""}
    if verdict.get("verdict") == VERIFIED and verdict.get("commit"):
        out["commit"] = str(verdict["commit"])
        out["version"] = str(verdict.get("version") or "")
        # The artefact names its own build — the whole point of the ticker
        # living inside the stamp rather than on the deploy machine.
        out["build"] = verdict.get("build")
        out["origin"] = ORIGIN_BUILD_STAMP
        if source == "boot":
            out["verdict"] = VERIFIED
            out["reason"] = (
                f"this process imported the artefact stamped {out['commit'][:12]}; "
                "the stamp was read at process start, before anything on disk "
                "could change underneath it"
            )
            return out
        out["reason"] = _late_reason(source)
        return out

    # 2. NO STAMP — a source checkout (dev box, editable install). git HEAD is
    #    a legitimate identity HERE AND ONLY HERE, because it is read AT BOOT.
    commit, version = _source_head_at_boot()
    if commit:
        out["commit"] = commit
        out["version"] = version
        # Symmetric with `version`: on a checkout the committed ticker is as
        # much a part of this tree's identity as the version in pyproject.
        out["build"] = _source_build_at_boot()
        out["origin"] = ORIGIN_SOURCE_HEAD
        if source == "boot":
            out["verdict"] = VERIFIED
            out["reason"] = (
                f"no build stamp in this artefact (source checkout); HEAD was "
                f"{commit[:12]} when this process started, so that is the code it "
                "imported. Later commits do NOT move this — a restart does."
            )
            return out
        out["reason"] = _late_reason(source)
        return out

    out["reason"] = (
        "UNVERIFIED: this process's identity could not be established — the "
        f"artefact carries no build stamp ({verdict.get('reason') or 'no stamp'}) "
        "and there is no source checkout to read HEAD from at boot. Reporting "
        "nothing rather than guessing: on a long-lived process, disk is not "
        "memory."
    )
    return out


def _late_reason(source: str) -> str:
    return (
        f"UNVERIFIED: the process stamp was taken lazily ({source!r}), not at "
        "process start, so it cannot distinguish the bytes this process imported "
        "from bytes that replaced them afterwards"
    )


def process_stamp() -> dict:
    """The frozen stamp, or the honest never-captured verdict. Never captures."""
    if _STAMP is not None:
        return dict(_STAMP)
    return {
        "verdict": UNVERIFIED,
        "reason": (
            "UNVERIFIED: no process stamp was taken at startup, so what THIS "
            "PROCESS loaded is unknown. Not falling back to git HEAD: on a "
            "long-lived process that reports the newest commit, not the loaded "
            "one — a confident wrong answer in the reassuring direction (#738)."
        ),
        "commit": "",
        "version": "",
        "build": None,
        "origin": "",
        "captured_at": "",
        "capture_source": "",
        "pid": os.getpid(),
    }


def reset_process_stamp_for_test() -> None:
    """TEST SEAM ONLY. Production has exactly one boot per process.

    GUARDED, and the guard is the point (#738, caught by vulture 2026-08-02).
    This module's whole value is that the stamp is taken ONCE at boot and cannot
    be re-taken: "a later call cannot re-stamp, and that idempotency IS the
    correctness property". An unguarded reset hands any caller the exact lie the
    stamp exists to refuse — reset, re-capture at a later HEAD, and the process
    now claims to have loaded code it never imported. That is #738's defect with
    a shipped function to reproduce it.
    Vulture flagged this as unused because it runs against the ship stage, which
    carries mcp/server/** but not mcp/tests/** — a test-only helper living in
    shipped code IS dead code from the artefact's point of view. The finding was
    correct; the answer is a guard, not an allowlist waiver.
    """
    if "pytest" not in sys.modules:  # pragma: no cover - the guard, not a branch
        raise RuntimeError(
            "reset_process_stamp_for_test() is a test seam and refuses outside a "
            "test run: resetting a captured stamp would let this process claim a "
            "commit it never loaded, which is the #738 defect itself."
        )
    global _STAMP
    _STAMP = None


def running_identity(source_head: str = "") -> dict:
    """The fields ``local_build_info()`` must report for the RUNNING build.

    Three separate questions, three separate fields — never two of them
    collapsed into one:

        commit       what did THIS PROCESS load?        (the frozen stamp)
        source_head  what is on disk right now?         (live git HEAD)
        (caller)     what shipped?                      (deployed_commit)

    ``build`` and ``created_at`` come back as the string ``UNVERIFIED`` when
    nothing establishes them, never as ``0`` / ``""`` — a zero and a blank
    render as data and read as answers (#738 secondary; #627's own principle).

    ``build`` is the OPTION B ticker (operator 2026-08-21): an integer carried
    beside the three-segment version, never folded into it.
    """
    stamp = process_stamp()
    verified = stamp["verdict"] == VERIFIED
    return {
        "commit": stamp["commit"] if verified else "",
        "source_head": source_head or "",
        "running_verdict": stamp["verdict"],
        "running_note": stamp["reason"],
        "running_origin": stamp["origin"] or UNVERIFIED,
        "running_since": stamp["captured_at"] or UNVERIFIED,
        "build": stamp.get("build") if verified and stamp.get("build") else UNVERIFIED,
        "created_at": stamp["captured_at"] if verified else UNVERIFIED,
    }
