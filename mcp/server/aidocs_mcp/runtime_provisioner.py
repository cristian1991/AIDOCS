"""AIDOCS-owned runtime provisioning — the enforcement-interpreter boundary.

A security-grade gate must NOT run under whatever ambient python (a project
venv, a system python on PATH, an ephemeral build env) happened to invoke
setup: that interpreter can vanish, be shadowed, or carry a different/again
patched ``aidocs_mcp``. Enforcement hooks must run under a STABLE, AIDOCS-owned
interpreter whose integrity we can vouch for.

Runtime tiers (best → worst), every one VERIFIED (must import ``aidocs_mcp``):

  * ``operator_pin`` — operator set ``AIDOCS_PYTHON`` explicitly. Owned by
    operator intent; distinguished from a runtime AIDOCS itself provisioned.
  * ``standalone``   — a pinned standalone CPython under ``~/.aidocs/runtime``
    installed from a pinned version/URL/SHA256 (or an offline archive with a
    pinned SHA256), atomically. The headline owned tier.
  * ``venv``         — a dedicated AIDOCS-managed venv built from a suitable
    base python. A TRUTHFUL DEGRADED Tier-1 path when no standalone pin is
    available for the platform.
  * ``ambient``      — ``sys.executable``. NOT owned. Hooks are NEVER installed
    against ambient silently — only under an explicit degraded/dev escape.

Everything that touches the network / filesystem extraction / pip is behind an
injected callable so the semantics (standalone preference, checksum-fail-closed,
ambient refusal, drift repair, idempotent + offline reinstall, truthful audit)
are provable without a real download. We ship NO unverified URLs/checksums: an
unconfigured platform honestly DEGRADES to venv rather than installing something
it cannot verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from aidocs_mcp import runtime_generations

RuntimeRunner = Callable[[list[str]], "tuple[int, str, str]"]

# A bare PEP 440-ish version token (e.g. "1.2.3", "0.4.0rc1"). Anything else in
# a package spec is treated as a local wheel/sdist/source path or a full pip
# requirement and installed verbatim.
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.\-+!_]*$")


def expected_aidocs_version() -> str | None:
    """The aidocs_mcp version THIS process is — the law/enforcement package an
    owned runtime must prove it carries. ``None`` only if our own import is
    somehow broken (then version enforcement is skipped, import is still
    required).
    """
    try:
        import aidocs_mcp

        return getattr(aidocs_mcp, "__version__", None)
    except Exception:
        return None


def local_source_root() -> Path | None:
    """The project dir of the SOURCE CHECKOUT this process is running from.

    Identified by ``<root>/pyproject.toml`` + ``<root>/server/aidocs_mcp/`` — i.e.
    the ``mcp/`` directory of the repo. Returns ``None`` when this process runs
    from an installed package rather than a checkout.

    #552: the published index is DELIBERATELY stale (operator 2026-07-26: "pip has
    very old versions, we didnt deploy to pip for a while, code is on github,
    source code is on this machine"), so a source checkout must provision its
    owned runtime from ITSELF — never from a version resolved against an index
    that will never carry it. Without this, `aidocs runtime --fix` fails with
    "Could not find a version that satisfies aidocs_mcp==<local version>", no
    enforcement hooks can be installed (they refuse an ambient interpreter, which
    is the correct stance), and native Read/Edit/Bash run UNGOVERNED on the very
    machine where an agent edits the gate code.
    """
    try:
        import aidocs_mcp

        pkg = Path(aidocs_mcp.__file__).resolve()
    except Exception:
        return None
    # .../<root>/server/aidocs_mcp/__init__.py → <root> is a few levels up.
    for candidate in list(pkg.parents)[1:4]:
        try:
            if (candidate / "pyproject.toml").is_file() and (
                candidate / "server" / "aidocs_mcp"
            ).is_dir():
                return candidate
        except OSError:
            continue
    return None


def installed_package_dir(home: Path | str | None = None) -> Path | None:
    """The ``aidocs_mcp`` package dir inside the AIDOCS-OWNED venv, or None.

    This is the code the ENFORCEMENT HOOKS execute — distinct from the live source
    the daemon imports. Resolved by locating site-packages under the owned venv
    rather than by asking the interpreter, because spawning it would put a
    subprocess on a path that runs during setup/status reporting.
    """
    try:
        base = Path(home) if home is not None else Path.home()
        venv = _venv_dir(base)
        candidates = [venv / "Lib" / "site-packages" / "aidocs_mcp"]  # Windows
        candidates += sorted(venv.glob("lib/python*/site-packages/aidocs_mcp"))  # POSIX
        for c in candidates:
            if c.is_dir():
                return c
    except Exception:  # noqa: BLE001 — a reporting helper never raises
        return None
    return None

def _stamp_provenance_current(src: Path, inst: Path) -> tuple[bool, str]:
    """Can the INSTALLED artefact prove it IS the current source package, when
    the bytes say otherwise? (2026-08-23)

    THE FALSE NEGATIVE THIS ANSWERS. ``compute_package_fingerprint`` is a raw
    sha256 over file bytes, deliberately and permanently so — it is the
    SIGNED-RELEASE fingerprint and package-integrity/trust is built on it, so it
    must stay byte-exact and is NOT touched here. But the deploy installs the
    local enforcement runtime from a detached, stamped SHIP STAGE, and a stage is
    a different build of the same commit:

      * 136 ``.py`` files byte-differ and newline-normalise-EQUAL (the stage
        worktree carries the other line ending). Measured directly.
      * 27 of 28 Vite content-hashed webapp assets differ, because the stage
        rebuilt them and content hashes moved.

    Neither is a code difference, and neither can ever be made to compare equal,
    so after a stage install the byte digest reports STALE FOREVER. That verdict
    is not merely noisy: ``package_fresh=False`` is the one value that defeats
    the no-op in ``_provision_venv``, so the next ``aidocs runtime --fix`` WITHOUT
    ``--package`` reinstalls from the checkout — which has no ``_build_stamp.py``
    — WIPING the stamp and returning ai_version's ``running`` axis to UNVERIFIED.
    A routine maintenance command would silently revert a shipped feature.

    THE PRECONDITIONS, AND WHY THEY ARE NOT "CLEAN TREE + COMMIT == HEAD".
    That shape was proposed first and MEASUREMENT REFUTED BOTH HALVES on this
    very repo, at rest, in the state the fix is for:

      * ``stamp.commit == HEAD`` was already false — HEAD 7493f06b9
        (``chore(deploy-reports)``) vs stamp 2447d4c8b. STRUCTURAL, not
        incidental: ``__init__._source_drift`` already documents that "the act of
        deploying CREATES a commit ... local != deploy after EVERY successful
        deploy, forever, by construction". A precondition that is false
        immediately after every deploy cannot fire when it is needed.
      * ``working tree clean`` was false too — 275 dirty paths, all of them under
        ``.MEMORY/`` or ``mcp/scratch/``, which ``_source_drift`` already excludes
        as NOT SOURCE for precisely this reason. Agents write memory
        continuously; that tree is never clean, so the rule would never fire.

    So both git questions are SCOPED TO THE SOURCE PACKAGE DIRECTORY, and the
    commit question is asked as CONTENT EQUIVALENCE SINCE THE BUILD rather than
    SHA identity with HEAD — the same correction ``_source_drift`` already made:

      1. the installed tree's stamp verdict is VERIFIED (its bytes still hash to
         what was recorded when they were built — so a tampered install, or one
         carrying a module retired from source, cannot reach here);
      2. the stamp names a FULL commit, not an abbreviation. ``write_build_stamp``
         refuses an empty or "unknown" commit but does NOT check its LENGTH, and
         git happily resolves a 6-character prefix -- so without this floor a
         stamp naming a prefix would short-circuit. An abbreviation is unique
         TODAY and can become ambiguous with the next object written; provenance
         pinned to a value that can start meaning something else is not
         provenance. (Conductor mutation gate M2 was analysed as equivalent-in-
         outcome because the EMPTY string fails at ``cat-file`` anyway; that holds
         for the empty string only, so this is pinned by test, not conceded.)
      3. ``src`` is inside a git work tree that KNOWS that commit;
      4. nothing was COMMITTED into the source package since it;
      5. nothing is UNCOMMITTED in the source package either (``--porcelain``
         reports untracked files as ``??``, so a brand-new module counts).

    ONE DIRECTION ONLY. Returns True only when all five are PROVEN; every other
    path returns False with a named reason and the byte comparison stands. It can
    upgrade a definite False to True and nothing else — it never manufactures a
    verdict where there was none, so "unknown is not a pass" is untouched.

    WHAT IT DELIBERATELY LETS THROUGH — read this before quoting a fresh. Git is
    the witness, so anything git cannot see is invisible here: above all
    ``templates/webapp/`` is a GITIGNORED BUILD OUTPUT, so a dashboard bundle
    built from different webapp sources is not detected by this rule. That is
    accepted knowingly — those hashes are not reproducible across two builds, so
    the byte comparison could never have distinguished a real change from a
    rebuild either. It also inherits the honesty of the packaging step: a stamp
    written over a tree that was not the commit it names would be believed.
    """
    try:
        from .build_stamp import VERIFIED as _VERIFIED
        from .build_stamp import build_stamp_verdict

        verdict = build_stamp_verdict(inst)
    except Exception as exc:  # noqa: BLE001 — cannot check == cannot claim
        return False, f"could not read the installed build stamp ({type(exc).__name__})"

    if verdict.get("verdict") != _VERIFIED:
        return False, (
            "the installed artefact cannot prove its own provenance "
            f"({verdict.get('verdict')}): {str(verdict.get('reason') or '')[:200]}"
        )
    commit = str(verdict.get("commit") or "").strip()
    if len(commit) < 7:
        return False, "the installed build stamp names no usable commit"

    try:
        # The ONE audited git helper (Article XXII) — already fingerprinted for
        # the spawn-surface seal and windowless on win32. A second git callsite
        # here would just be a new untracked tunnel. It RAISES on non-zero, which
        # is what makes "not a work tree" and "unknown commit" land in the
        # cannot-prove branch rather than silently reading as agreement.
        from .git_helpers import run_git_sync

        cwd = str(src)
        run_git_sync(cwd, "rev-parse", "--is-inside-work-tree", timeout=20)
        run_git_sync(cwd, "cat-file", "-e", f"{commit}^{{commit}}", timeout=20)
        # `-- .` with cwd=src scopes BOTH questions to the package directory.
        landed = run_git_sync(cwd, "diff", "--name-only", commit, "HEAD", "--", ".", timeout=20)
        dirty = run_git_sync(cwd, "status", "--porcelain", "--", ".", timeout=20)
    except Exception as exc:  # noqa: BLE001 — provenance we cannot CHECK is none
        return False, (
            f"cannot ask git whether the source package moved since {commit[:12]} "
            f"({type(exc).__name__}) — provenance that cannot be checked is not provenance"
        )

    moved = [ln for ln in landed.splitlines() if ln.strip()]
    unstaged = [ln for ln in dirty.splitlines() if ln.strip()]
    if moved or unstaged:
        return False, (
            f"the source package HAS changed since {commit[:12]}: "
            f"{len(moved)} committed, {len(unstaged)} uncommitted — this is real drift"
        )
    return True, (
        f"the installed artefact is the VERIFIED build of {commit[:12]}, and the source "
        "package has not changed since — nothing committed, nothing uncommitted. The "
        "remaining byte difference is build-shaped (line endings, rebuilt "
        "content-hashed assets under the gitignored webapp output), not code."
    )


def runtime_freshness(
    *,
    source_pkg: Path | str | None = None,
    installed_pkg: Path | str | None = None,
) -> dict:
    """Does the INSTALLED enforcement runtime match CURRENT SOURCE? (#569)

    THE THIRD DRIFT AXIS. Two already existed and neither answers this question:

        source    <-> deployed commit   __init__._source_drift   (git: am I running
                                                                  what is deployed?)
        installed <-> recorded          package_integrity        (tamper: was the
                                                                  install altered?)
        installed <-> source            HERE                     (does the gate
                                                                  enforce CURRENT law?)

    THE DEFECT THIS CLOSES (measured 2026-07-27/28): the installed runtime carried
    none of the day's commits — the retired agent-brief verb table still present,
    the MSYS argument-fidelity fix and the git-commit quoting fix both absent —
    while `aidocs setup` printed "trust chain proven end-to-end" and every check it
    ran passed. They all passed honestly: they compare INTERPRETER IDENTITY and
    installed-vs-RECORDED content. Nothing compared installed against SOURCE, so a
    day-stale hook runtime was indistinguishable from a current one. Because hooks
    load the installed package while the daemon loads live source, the gate
    enforced a policy the operator had repealed AND deployed, and reported its
    verdict as current. That one gap produced five separate misdiagnoses in a
    single session.

    "verified" and "fresh" are different words and must stay different: verified
    means unchanged since install, fresh means matches source. Conflating them is
    what hid this for a full day.

    UNKNOWN IS NOT A PASS — the contract is copied deliberately from
    ``_source_drift``: "Fail-quiet, never fail-green: ... NEVER a fabricated True.
    Unknown is not a pass." A freshness check that guessed True would be worse than
    none, because a surface would then print "proven" over an unverified runtime.

    Returns ``{"fresh": True|False|None, "source_fingerprint", "installed_fingerprint",
    "source_pkg", "installed_pkg", "note", "provenance"}``. ``provenance`` is
    populated only when the byte digests DISAGREED on the checkout axis and the
    in-artefact build stamp was therefore consulted — see
    ``_stamp_provenance_current``; it carries the reason either way, so a fresh
    reached over differing bytes always says how it got there.
    """
    out: dict = {
        "fresh": None,
        "source_fingerprint": None,
        "installed_fingerprint": None,
        "source_pkg": None,
        "installed_pkg": None,
        "note": "",
        "provenance": "",
    }

    src = Path(source_pkg) if source_pkg is not None else None
    if src is None:
        root = local_source_root()
        src = (Path(root) / "server" / "aidocs_mcp") if root else None
    inst = Path(installed_pkg) if installed_pkg is not None else installed_package_dir()

    out["source_pkg"] = str(src) if src else None
    out["installed_pkg"] = str(inst) if inst else None

    if src is None or not src.is_dir():
        out["note"] = "no source checkout resolved — cannot compare (not a source machine?)"
        return out
    if inst is None or not inst.is_dir():
        out["note"] = "no AIDOCS-owned runtime package found — nothing installed to compare"
        return out

    try:
        from .package_integrity import compute_package_fingerprint

        # Reuse the audited, cross-OS-deterministic protocol (pinned by
        # tests/deploy/test_fingerprint_cross_os_determinism.py). A second hashing
        # scheme here would be one more thing to keep in sync forever.
        # The version arg is folded into the digest, so pass ONE value to both
        # sides: this asks "does the CODE differ", not "do the labels differ" —
        # the version matching was exactly the false comfort that hid the drift.
        from .build_stamp import STAMP_REL

        # THE STAMP IS A LABEL, NOT LAW (2026-08-22). This asks "does the CODE
        # differ"; the in-artefact build stamp is provenance, and it is a .py
        # inside the package, so an unexcluded digest folds it in. A source
        # checkout never carries one (gitignored, generated per build) while a
        # stamped install always does — so comparing raw would read "stale"
        # FOREVER the moment the runtime is installed from the stamped ship
        # stage, and the only way back to "fresh" would be a reinstall from
        # the unstamped checkout, i.e. throwing the stamp away. Rule: the
        # reference decides. No stamp in the reference -> exclude it on both
        # sides (code only). Stamp in the reference (the frozen stage) ->
        # include it on both sides: "fresh" then means "installed == what was
        # shipped, stamp and all", which is exactly what step 5c must prove,
        # and what lands a NEW stamp even when no server code changed.
        # `exclude=` is #627 phase 3's seam, built for precisely this file.
        # ── THE STAMP IS NEVER IN THE BYTE DIGEST (2026-08-24) ────────────
        #
        # The rule used to be "the reference decides": a stamped reference (the
        # ship-stage) folded the stamp INTO both digests so "fresh" meant
        # "installed == what was shipped, stamp and all". That was correct while
        # the installed stamp was a VERBATIM COPY of the stage's.
        #
        # It stopped being true when #867 made the provisioner write its own
        # stamp — same commit/version/build (the provenance is carried), but a
        # fingerprint recomputed over the INSTALLED tree and its own built_at,
        # because the stamp must describe the bytes it actually sits on or
        # `build_stamp_verdict` reports MISMATCH forever.
        #
        # Two stamps that differ by design can never be byte-equal, so folding
        # them into the digest made parity UNREACHABLE: deploy dev-1236 shipped
        # green and step 5c returned rc=5 "still stale after runtime --fix".
        # test_runtime_freshness_stamp_aware.py predicted this exact outcome on
        # 2026-08-22 — "the two instruments (freshness, provenance) would fight
        # each other, and the one that decides installs would win."
        #
        # SO SPLIT THE QUESTION THE STAMP WAS ANSWERING. Freshness asks "does the
        # CODE differ" (this function's own docstring), and the stamp is not
        # code — exclude it on BOTH axes. What the stamped-reference axis really
        # needed was never byte-equality; it was "has the SHIPPED BUILD landed",
        # and that is a provenance question, answered below against
        # commit/version/build rather than against bytes that are allowed to
        # differ.
        excl: tuple[str, ...] = (STAMP_REL,)
        reference_is_stamped = (src / STAMP_REL).is_file()
        ver = expected_aidocs_version() or "0"
        src_fp = (compute_package_fingerprint(src, version=ver, exclude=excl) or {}).get(
            "fingerprint"
        )
        inst_fp = (compute_package_fingerprint(inst, version=ver, exclude=excl) or {}).get(
            "fingerprint"
        )
    except Exception as exc:  # noqa: BLE001 — cannot verify == say so, never assume clean
        out["note"] = f"could not fingerprint both trees: {type(exc).__name__}"
        return out

    out["source_fingerprint"] = src_fp
    out["installed_fingerprint"] = inst_fp
    if not src_fp or not inst_fp:
        out["note"] = "fingerprint unavailable for one tree — cannot tell"
        return out

    out["fresh"] = src_fp == inst_fp
    if out["fresh"] and reference_is_stamped:
        # HAS THE SHIPPED BUILD LANDED? The code matches, but a deploy that
        # changed no server code must still install so the NEW stamp (new build
        # number, new commit) lands instead of being skipped as "already
        # current" — the outcome the old stamp-in-digest rule was protecting.
        # Provenance is the honest way to ask it: commit/version/build are
        # CARRIED verbatim from the reference, so they compare exactly, while
        # built_at and the fingerprint are per-install and are not compared.
        # An installed tree with NO stamp answers "not landed", which is what
        # sends a first stamped install through the provision path.
        from .build_stamp import read_build_stamp

        def _provenance(pkg: Path) -> tuple:
            stamp = read_build_stamp(pkg)
            # A STAMP THAT CANNOT NAME A COMMIT IS NOT PROVENANCE — the rule
            # `write_build_stamp` enforces at the writer ("worse than no stamp
            # — it asserts that provenance was recorded when it was not").
            # `read_build_stamp` returns a MALFORMED DICT rather than None on a
            # parse failure, so testing `is None` alone let two unreadable
            # stamps compare equal on a blank commit and report the build as
            # landed. Same laundering, one layer in.
            if not stamp or not str(stamp.get("commit") or "").strip():
                # ABSENT OR UNPARSEABLE IS NOT "THE SAME AS YOURS". Returning a
                # blank tuple here would make two unreadable stamps compare
                # EQUAL and report the build as landed — unknown laundered into
                # a pass, on the one check that decides whether the shipped
                # runtime installs. Fall back to the raw bytes: absent vs
                # present differ, and two different unparseable stamps differ,
                # so the only way to be "landed" is to genuinely match.
                path = pkg / STAMP_REL
                try:
                    raw = path.read_text(encoding="utf-8") if path.is_file() else ""
                except OSError:
                    raw = "<unreadable>"
                return ("<raw>", raw)
            return (
                str(stamp.get("commit") or ""),
                str(stamp.get("version") or ""),
                stamp.get("build"),
            )

        if _provenance(src) != _provenance(inst):
            out["fresh"] = False
            out["note"] = (
                "the CODE matches but the shipped build has not landed: the "
                "installed stamp names a different commit/version/build than "
                "the reference"
            )
            return out
    if not out["fresh"] and not reference_is_stamped:
        # THE BYTES DISAGREE — ASK PROVENANCE BEFORE CALLING IT STALE (2026-08-23).
        # Only on the CHECKOUT axis (`excl` is set exactly when the reference
        # carries no stamp). When the reference IS stamped it is the deploy's
        # frozen ship-stage and "fresh" must keep meaning "installed == exactly
        # what was shipped, stamp and all" — short-circuiting there would skip
        # step 5c's reinstall and the new build number would never land, which is
        # the defect test_runtime_freshness_stamp_aware.py pins.
        #
        # A stage install byte-differs from the checkout FOREVER (line endings,
        # rebuilt content-hashed webapp assets), and that permanent False is what
        # drives `runtime --fix` to reinstall from the unstamped checkout and WIPE
        # the build stamp. See _stamp_provenance_current for the full measurement
        # and for what this deliberately does not catch.
        proven, why = _stamp_provenance_current(src, inst)
        out["provenance"] = why
        if proven:
            out["fresh"] = True
            # Two instruments now disagree (digest says differ, verdict says
            # fresh). Say WHY in the note, always — an unexplained override is
            # how an operator loses a night deciding which one to believe.
            out["note"] = why
            return out
    if not out["fresh"]:
        out["note"] = (
            "the enforcement runtime is STALE: the installed package differs from "
            "source, so hooks are enforcing older code than the daemon runs"
        )
    return out


def _resolve_package(
    package_spec: str | None,
    expected_version: str | None,
) -> tuple[str, str | None]:
    """Map a package spec → (pip target, version to ENFORCE at verify).

    * ``None`` → the LOCAL SOURCE TREE when this process runs from a checkout
      (#552); otherwise pin ``aidocs_mcp==<expected_version>`` (or bare
      ``aidocs_mcp`` when the expected version is unknown).
    * a bare version token → ``aidocs_mcp==<token>`` and enforce that version.
    * anything else (local wheel/sdist/source dir, full requirement) → install
      verbatim; we cannot predict its version, so we enforce none and record the
      actual version reported after install.

    THE SOURCE CASE STILL ENFORCES THE VERSION, and is therefore NOT routed
    through the generic verbatim branch. Ownership means AIDOCS controls the
    INTERPRETER; it says nothing about where the artifact came from. The checkout
    IS the code this process is running, so its version is knowable — and dropping
    enforcement would let an owned runtime carry different law than the process
    that provisioned it.

    An explicit operator spec always wins: an operator pinning a published version
    or handing over a wheel is making a deliberate choice.
    """
    if not package_spec:
        root = local_source_root()
        # Use the checkout ONLY when it provably CARRIES the version being
        # enforced — i.e. the caller is asking for the code this process is. If a
        # DIFFERENT version is requested, the local tree is the wrong artifact and
        # the index pin is correct. That keeps "pinned, not floating" true: the
        # path is substituted only where it is provably the same version.
        # Re-verify the marker even though local_source_root() checked it: a
        # bogus/monkeypatched detection must fall back rather than hand pip a
        # directory that is not a project (fail closed).
        if (
            root is not None
            and (Path(root) / "pyproject.toml").is_file()
            and (expected_version is None or expected_version == expected_aidocs_version())
        ):
            return str(root), expected_version
        if expected_version:
            return f"aidocs_mcp=={expected_version}", expected_version
        return "aidocs_mcp", None
    s = str(package_spec).strip()
    if _VERSION_RE.match(s):
        return f"aidocs_mcp=={s}", s
    return s, None


def vendored_mempalace_dir(pkg_target: str | None) -> Path | None:
    """The vendored palace engine to install ALONGSIDE ``pkg_target`` (#733).

    When the resolved install target is a SOURCE CHECKOUT (a directory with
    a pyproject.toml — the `<repo>/mcp` shape `_resolve_package` returns),
    the sibling ``<repo>/third_party/mempalace`` must go into the owned
    runtime too, exactly as the VPS deploy already does
    (deploy_aidocs_gate.sh:1342 installs '$VPS_TREE/third_party/mempalace'
    alongside '$VPS_TREE/mcp'). The local lane never did, so the pinned
    runtime venv could import aidocs_mcp but NEVER start the server.

    Returns None for a non-checkout target (index pin / wheel / bare name):
    there is no vendored tree to install from — the artifact itself must
    carry the engine, which verify_interpreter now checks.
    """
    if not pkg_target:
        return None
    try:
        root = Path(pkg_target)
        if not (root / "pyproject.toml").is_file():
            return None
        vendor = root.parent / "third_party" / "mempalace"
        if (vendor / "pyproject.toml").is_file() and (
            vendor / "mempalace" / "__init__.py"
        ).is_file():
            return vendor
    except OSError:
        return None
    return None


# ── locations ────────────────────────────────────────────────────────────
def runtime_root(home: Path | str) -> Path:
    return Path(home) / ".aidocs" / "runtime"


def manifest_path(home: Path | str) -> Path:
    return runtime_root(home) / "runtime.json"


def _standalone_dir(home: Path | str) -> Path:
    return runtime_root(home) / "cpython"


def _venv_dir(home: Path | str) -> Path:
    """WHERE THE SERVING VENV IS — the ACTIVE generation's, or the legacy tree.

    THE ONE SEAM (#1030). Every reader of the installed runtime goes through
    here — ``venv_python``, ``_tier_package_root``, the freshness probe,
    ``_stamp_provenance_current`` — so making this follow the activation
    pointer makes all of them generation-aware at once, instead of teaching
    each one separately and getting a different answer from each.

    With no pointer this is the legacy ``runtime/venv``, untouched: an install
    that predates generations keeps working, and the first generational
    provision is what moves it. See ``runtime_generations`` for why an
    unusable pointer resolves to nothing rather than guessing.

    This is a READ. Builds never write here — they write into a NEW generation
    and become visible only at the pointer flip.

    A BROKEN POINTER RESOLVES TO NOTHING SERVEABLE, never to the legacy tree.
    The first cut fell back to ``runtime/venv`` whenever the pointer failed to
    resolve, which merged two different situations: "no pointer, so the legacy
    tree IS the runtime" (correct) and "the operator activated a generation and
    it is missing/unsealed" (a substitution). On a migrated box the second is
    the dangerous one — ``runtime/venv`` there is the PRE-MIGRATION runtime, so
    the machine would silently resume enforcing old code while every surface
    reported a healthy venv tier.

    Returning the unresolvable generation directory keeps the signature Path
    (many readers) while making the failure HONEST: nothing resolves under it,
    so `venv_python` answers None, the tier walk records the miss in `checked`,
    and no reader is handed a different runtime than the one activated. See
    ``runtime_generations.serving_venv`` for the reasoned form.
    """
    base = Path(home)
    # ONE POINTER READ. The generation id comes back in the same snapshot as
    # the venv, so the failure branch below no longer needs a SECOND
    # `read_pointer` to name the directory — a flip landing between those two
    # reads would have named a different generation than the one that was
    # actually judged.
    served = runtime_generations.serving_venv(base)
    if served.venv is not None:
        return served.venv
    if served.reason == runtime_generations.REASON_NO_POINTER_NO_TREE:
        # Nothing has ever been provisioned. The legacy path is where a first
        # provision would land, and answering it here is not a substitution —
        # there is no other runtime to substitute FOR.
        return runtime_root(base) / "venv"
    named = (
        runtime_generations.generation_dir(base, served.generation_id)
        if served.generation_id
        else None
    )
    return named or runtime_generations.generations_root(base)


def _python_in(base: Path) -> str | None:
    """First existing python executable under an install dir (OS-agnostic).

    Covers both a flat layout and the ``python/`` top-level dir that
    python-build-standalone ``install_only`` archives unpack into (windows:
    ``python/python.exe``; unix: ``python/bin/python3``).
    """
    for rel in (
        "python.exe",
        "python3.exe",
        Path("Scripts") / "python.exe",
        Path("bin") / "python",
        Path("bin") / "python3",
        "python",
        "python3",
        Path("python") / "python.exe",
        Path("python") / "bin" / "python3",
        Path("python") / "bin" / "python",
    ):
        cand = base / rel
        if cand.is_file():
            return str(cand)
    return None


def standalone_python(home: Path | str) -> str | None:
    return _python_in(_standalone_dir(home))


def venv_python(home: Path | str) -> str | None:
    return _python_in(_venv_dir(home))


def platform_key() -> str:
    """Stable key for selecting a pinned standalone build."""
    return f"{sys.platform}-{platform.machine().lower()}"


# Pinned standalone builds keyed by ``platform_key()`` (sys.platform + machine).
# These are python-build-standalone ``install_only`` archives; every SHA256 was
# transcribed byte-for-byte from the release's published SHA256SUMS (and matches
# the GitHub asset digest). A platform NOT listed here still degrades honestly to
# venv (or an operator --offline-archive + --sha256). To refresh: bump
# _PBS_RELEASE/_PBS_PY and replace each sha256 from the new release's SHA256SUMS
# — never hand-edit a single hex char.
_PBS_RELEASE = "20260825"
_PBS_PY = "3.13.15"

# AIDOCS blesses exactly ONE CPython per release as its official owned standalone
# runtime. Anything else an operator runs under is "custom" provenance — still
# usable (offline archive / sha manifest / venv / operator pin) but reported as
# NOT the blessed build, so the distinction is always visible in doctor/setup.
BLESSED_PYTHON = _PBS_PY  # 3.13.15
# ── 2026-08-27 (#569 R1): 3.13 IS NOW BLESSED. Operator ruling: "the most
# 'security capable' version". That ruling had exactly ONE legal answer, because
# mcp/pyproject.toml declares `requires-python = ">=3.13,<3.14"` — evaluated with
# packaging.SpecifierSet, 3.12.13 does NOT satisfy it and 3.14.0 does not either.
#
# So the previous pin was not a defensible policy position awaiting a tiebreak:
# AIDOCS blessed an interpreter its own package CANNOT INSTALL ON, while listing
# the only acceptable line as "not blessed yet". Provisioning the blessed runtime
# and installing aidocs-mcp into it could never both succeed.
#
# The old comment said 3.13 was excluded "until the toolchain + dependency surface
# are validated against them". That precondition was already satisfied and unnoticed:
# the full suite (19,545 tests) runs on 3.13 every deploy — 3.13.12 on the operator
# box, 3.13.5 on the VPS — which IS the validation it asked for.
#
# 3.14 STAYS excluded, and now has a reason stronger than caution: pyproject's
# <3.14 ceiling forbids it. Operators may still run anything explicitly via
# --offline-archive/--sha256 (reported as "custom" provenance, never blessed).
NOT_BLESSED_YET = ("3.14",)


def is_blessed_version(version: str | None) -> bool:
    """True iff ``version`` is THE blessed CPython for this AIDOCS release."""
    return bool(version) and str(version) == BLESSED_PYTHON


_PBS_BASE = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{_PBS_RELEASE}/cpython-{_PBS_PY}%2B{_PBS_RELEASE}-"
)


def _pbs(triple: str, sha256: str) -> dict[str, str]:
    return {
        "version": _PBS_PY,
        "url": f"{_PBS_BASE}{triple}-install_only.tar.gz",
        "sha256": sha256,
    }


PINNED: dict[str, dict[str, str]] = {
    "win32-amd64": _pbs(
        "x86_64-pc-windows-msvc",
        "82a792c25550a421b29f381eaeafa6dccd1ffcbd97a1b1507b202f5df877cecf",
    ),
    "linux-x86_64": _pbs(
        "x86_64-unknown-linux-gnu",
        "8a70011ae25276a9925f89304cdc086466cd269ee6cfe68a9506694ca5ff4f9c",
    ),
    "linux-aarch64": _pbs(
        "aarch64-unknown-linux-gnu",
        "b298e34164582305be9629a0da50701358195ce30b639f5ed4bbc50c4768f048",
    ),
    "darwin-arm64": _pbs(
        "aarch64-apple-darwin",
        "d681f7cebf4885637242cba807d22f476b9ea8555ac2dc7307172426dbf161e1",
    ),
    "darwin-x86_64": _pbs(
        "x86_64-apple-darwin",
        "40eb292bb37f32639b1eb5736bef702081a2151eda1bb4e6171345a157babfa6",
    ),
}


def pinned_spec(key: str | None = None) -> dict[str, str] | None:
    spec = PINNED.get(key or platform_key())
    return dict(spec) if spec else None


def _asset_filename(spec: dict) -> str:
    """The upstream asset filename a PINNED URL points at (decoding %2B → +)."""
    from urllib.parse import unquote

    return unquote(str(spec.get("url", "")).rsplit("/", 1)[-1])


def _parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``<sha256>  <filename>`` checksum file into {filename: sha}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            out[parts[-1]] = parts[0].lower()
    return out


def _fetch_sha256sums(release: str) -> str:
    import urllib.request

    url = (
        "https://github.com/astral-sh/python-build-standalone/releases/"
        f"download/{release}/SHA256SUMS"
    )
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read().decode("utf-8", "replace")


def verify_pinned_against_upstream(
    sums_text: str | None = None,
    *,
    fetch: Callable[[str], str] | None = None,
    release: str | None = None,
    version: str | None = None,
    pinned: dict[str, dict[str, str]] | None = None,
) -> dict:
    """MAINTENANCE/CI check (NOT part of the offline suite): confirm every PINNED
    entry still matches the upstream python-build-standalone release. For each
    entry it checks the asset's release+version+triple (via the exact filename),
    that the asset EXISTS in the release SHA256SUMS, and that our recorded SHA256
    matches upstream. Pass ``sums_text`` to run offline (unit tests); otherwise
    the SHA256SUMS for ``release`` is fetched. Returns {ok, release, checked,
    results:[{platform, asset, ok, problems, upstream_sha}]}; ok=False on ANY
    mismatch or missing asset.
    """
    rel = release or _PBS_RELEASE
    ver = version or _PBS_PY
    pins = PINNED if pinned is None else pinned
    if sums_text is None:
        sums_text = (fetch or _fetch_sha256sums)(rel)
    sums = _parse_sha256sums(sums_text)
    results: list[dict] = []
    ok = True
    for key, spec in pins.items():
        fname = _asset_filename(spec)
        problems: list[str] = []
        if f"cpython-{ver}+{rel}-" not in fname:
            problems.append("release_or_version_mismatch")
        if str(spec.get("version")) != str(ver):
            problems.append(f"spec_version!={ver}")
        upstream = sums.get(fname)
        if upstream is None:
            problems.append("missing_upstream_asset")
        elif upstream != str(spec.get("sha256", "")).lower():
            problems.append(f"sha_mismatch:upstream={upstream}")
        if problems:
            ok = False
        results.append(
            {
                "platform": key,
                "asset": fname,
                "ok": not problems,
                "problems": problems,
                "upstream_sha": upstream,
            },
        )
    return {"ok": ok, "release": rel, "version": ver, "checked": len(results), "results": results}


# ── hashing + manifest ───────────────────────────────────────────────────
def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def runtime_fingerprint(python_path: str | None) -> str | None:
    """A re-checkable fingerprint of the interpreter executable: sha256 of its
    bytes + size. Recorded by the provisioner at verified-install time and
    recomputed when a manifest shortcut is offered, so a swapped/tampered
    interpreter can't ride a stale manifest past verification. ``None`` if the
    executable is missing/unreadable (⇒ no shortcut, fresh verify).
    """
    if not python_path:
        return None
    p = Path(python_path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        return f"sha256:{sha256_file(p)}:{size}"
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(home: Path | str) -> dict | None:
    p = manifest_path(home)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_manifest(home: Path | str, data: dict) -> None:
    _atomic_write(manifest_path(home), json.dumps(data, indent=2) + "\n")


# ── verification ─────────────────────────────────────────────────────────
# #733: the probe also answers "can this runtime RESOLVE the palace engine?"
# — importing aidocs_mcp first so its __init__ vendor wiring has run, then
# asking find_spec. A runtime that imports aidocs_mcp but cannot resolve
# mempalace can never start the server (mcp_server.create_server hard-imports
# it), so blessing it was the lie that hid two days of dead overlap-restarts.
#
# #913: AND "is the mempalace it resolved the one this build imports?" —
# because the #733 question above CANNOT catch the failure #913 is about. The
# skew that started that item was a module RENAME inside an unchanged package:
# `find_spec('mempalace')` succeeds perfectly while a submodule underneath has
# moved, so the runtime verifies, is put into service, and then loses the
# gate's palace axis at first use (hub.palace becomes None, #910). The verdict
# is computed by `check_vendored_mempalace` INSIDE the runtime being judged —
# which is the only interpreter whose sys.path can answer the question — and is
# three-valued (absent / ok / skewed); `verify_interpreter` decides what each
# state means.
#
# THE IMPORT IS GUARDED, AND THE GUARD IS NOT DEFENSIVE PADDING. This probe runs
# under the INSTALLED package, which during an upgrade is the OLD one, and an
# aidocs_mcp that predates `vendored_contract` would raise here. Unguarded, the
# whole probe would exit non-zero and verify_interpreter would refuse that
# runtime for being OLD rather than broken — a new failure mode invented by the
# check itself. `None` means "this build has no verdict to give", which is a
# different thing from any of the three states and is treated as such.
_VERIFY_SNIPPET = (
    "import json\n"
    "import aidocs_mcp as m\n"
    "import importlib.util as u\n"
    "try:\n"
    "    from aidocs_mcp.vendored_contract import check_vendored_mempalace as c\n"
    "    v = c()\n"
    "except Exception:\n"
    "    v = None\n"
    "print(json.dumps({'version': getattr(m, '__version__', None),"
    " 'mempalace': u.find_spec('mempalace') is not None,"
    " 'vendored': v}))\n"
)


_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# A pip INSTALL needs a different budget from a quick interpreter probe. 25s is
# right for `python -c <verify snippet>`; it is nowhere near enough to BUILD AND
# INSTALL a source tree (#552 — installing the local checkout replaced fetching a
# prebuilt wheel, and the flat timeout then failed with
# TimeoutExpired([... '-m','pip','install', '<repo>/mcp'], 25)). Keeping the probe
# tight matters: it runs on hook/setup paths where a hang is felt.
_INSTALL_TIMEOUT_S = 900


def _is_install_argv(argv: list[str]) -> bool:
    try:
        parts = [str(a) for a in argv]
    except Exception:
        return False
    return "pip" in parts and "install" in parts


def _default_runner(argv: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    if timeout is None:
        timeout = _INSTALL_TIMEOUT_S if _is_install_argv(argv) else 25
    try:
        # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
        from .shell_egress_service import audited_run

        proc = audited_run(
            argv,
            fingerprint=("runtime_provisioner.py", "_default_runner", "subprocess.run"),
            reason="runtime-provision-runner",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_WIN_NO_WINDOW,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:  # noqa: BLE001 — surfaced truthfully to caller
        return 127, "", repr(exc)


def verify_interpreter(
    python_path: str,
    *,
    runner: RuntimeRunner | None = None,
    expected_version: str | None = None,
) -> dict:
    """Run a tiny probe under ``python_path`` and judge the runtime it finds.

    THREE QUESTIONS, NOT ONE, and they fail for different reasons:
      * can it import ``aidocs_mcp``, and at what version;
      * can it RESOLVE the vendored palace engine at all (#733) — a runtime
        that cannot can never start the server;
      * is the engine it resolved the one THIS BUILD IMPORTS (#913) — the
        question the previous two cannot answer, because a renamed submodule
        leaves the package importable and the version unchanged.

    Fail-closed: any error → not ok. Returns {ok, imports, version, reason}.
    """
    run = runner or (lambda a: _default_runner(a))
    if not python_path:
        return {"ok": False, "imports": False, "version": None, "reason": "no_interpreter"}
    code, out, err = run([python_path, "-c", _VERIFY_SNIPPET])
    if code != 0:
        return {
            "ok": False,
            "imports": False,
            "version": None,
            "reason": f"import_failed:{(err or out).strip()[:200]}",
        }
    version = None
    try:
        payload = json.loads(out.strip().splitlines()[-1]) or {}
        version = payload.get("version")
    except Exception:
        return {"ok": False, "imports": True, "version": None, "reason": "probe_unparseable"}
    # #733: fail-closed on an engine-less runtime. `.get(..., True)` keeps an
    # OLD probe (no mempalace field) passing — a mid-upgrade older installed
    # package must not be refused for predating the question — while a probe
    # that ANSWERED False is a runtime that cannot start the server.
    if payload.get("mempalace", True) is False:
        return {
            "ok": False,
            "imports": True,
            "version": version,
            "reason": "mempalace_unresolvable: runtime imports aidocs_mcp but cannot "
            "resolve the vendored palace engine, so it can never start the server (#733)",
        }
    # #913: THE HALF-UPDATED RUNTIME, WHICH THE CHECK ABOVE CANNOT SEE.
    #
    # The updater ships aidocs_mcp and — until the install side consumes it —
    # leaves the vendored mempalace frozen at whatever was provisioned. When the
    # two halves disagree on a module NAME the package still resolves, so
    # `mempalace` above is True and this runtime used to verify. It then starts,
    # fails the import inside palace_hub_extension, sets hub.palace = None, and
    # the edit gate stops evaluating its palace blockers while still answering
    # allowed=True (#910). Catching that at FIRST IMPORT is catching it after it
    # has already happened; this is the site that decides whether the runtime is
    # fit to be used at all, so it is the site that must ask.
    #
    # THE THREE STATES DO NOT MEAN THE SAME THING HERE:
    #   absent  -> handled ABOVE as #733's case, and deliberately not re-graded.
    #              "not installed" and "installed wrong" are different facts and
    #              collapsing them is what hid #913; the operator sent after a
    #              version skew that does not exist is worse off than one told
    #              plainly that the engine is missing.
    #   ok      -> verifies.
    #   skewed  -> REFUSE, carrying the verdict's own remedy verbatim rather
    #              than a second, drifting summary of it (law 311bf3e6).
    #
    # A MISSING FIELD IS NOT A STATE. The probe runs under the INSTALLED
    # package, which during an upgrade is the older one and may predate this
    # question entirely; `None` there means "no verdict", and refusing it would
    # mean nothing that predates the check could ever be upgraded past it. That
    # is the same compatibility rule #733 wrote one branch up, for the same
    # reason. An UNRECOGNISED state is the opposite case and is refused: that is
    # a runtime that answered something this build cannot grade, and unknown is
    # not a pass.
    _vendored = payload.get("vendored")
    if isinstance(_vendored, dict):
        # The state NAMES come from the module that produces them, so a rename
        # there cannot leave this comparing against a string nothing emits any
        # more — which would read every runtime as unrecognised and refuse the
        # lot. Imported here rather than at module scope: this is the only site
        # that needs them and runtime_provisioner is on the boot path.
        from .vendored_contract import STATE_ABSENT, STATE_OK, STATE_SKEWED

        _state = str(_vendored.get("state") or "")
        if _state == STATE_SKEWED:
            _missing = ", ".join(str(m) for m in _vendored.get("missing") or [])
            _detail = str(_vendored.get("reason") or "") or (
                "the vendored palace engine is missing "
                f"{_missing or 'modules'} that this build imports (#913)"
            )
            return {
                "ok": False,
                "imports": True,
                "version": version,
                "reason": f"vendored_skew: {_detail}",
            }
        if _state not in (STATE_ABSENT, STATE_OK):
            return {
                "ok": False,
                "imports": True,
                "version": version,
                "reason": f"vendored_verdict_unrecognised:{_state!r} — the runtime "
                "graded its vendored palace engine with a state this build does "
                "not know, so it cannot be read as healthy (#913)",
            }
    if expected_version is not None and str(version) != str(expected_version):
        return {
            "ok": False,
            "imports": True,
            "version": version,
            "reason": f"version_mismatch:{version}!={expected_version}",
        }
    return {"ok": True, "imports": True, "version": version, "reason": ""}


# ── resolution (tiered, verified, fail-closed) ───────────────────────────
_USABLE_OWNED = ("operator_pin", "standalone", "venv")


def resolve_runtime(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    runner: RuntimeRunner | None = None,
    verify: bool = True,
    allow_ambient: bool = False,
    expected_version: str | None = None,
) -> dict:
    """Resolve the interpreter enforcement should run under. Walks tiers best →
    worst, VERIFYING each (must import aidocs_mcp) and returning the first that
    passes. Ambient is returned only when ``allow_ambient`` (explicit
    degraded/dev escape); otherwise an unverifiable owned tier yields
    ``tier='none'`` so callers fail closed. Always truthful: ``owned`` is True
    only for operator_pin/standalone/venv, ``degraded`` flags the venv/ambient
    paths, ``checked`` records every tier we tried and why it was rejected.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    checked: list[dict] = []

    def _try(path: str | None, tier: str, source: str) -> dict | None:
        if not path or not Path(path).is_file():
            checked.append({"tier": tier, "source": source, "reason": "missing"})
            return None
        v = (
            verify_interpreter(path, runner=runner, expected_version=expected_version)
            if verify
            else {"ok": True, "imports": True, "version": None, "reason": ""}
        )
        if not v["ok"]:
            checked.append({"tier": tier, "source": source, "reason": v["reason"]})
            return None
        return {
            "path": path,
            "tier": tier,
            "source": source,
            "owned": tier in _USABLE_OWNED,
            "verified": bool(verify and v["ok"]),
            "degraded": tier in ("venv", "ambient"),
            "version": v.get("version"),
            "reason": "",
            "checked": checked,
        }

    pinned = str(e.get("AIDOCS_PYTHON") or "").strip()
    # A standalone lives under runtime/cpython (provision target); also accept a
    # python placed directly under ~/.aidocs/runtime as an owned standalone.
    sa = standalone_python(base) or _python_in(runtime_root(base))
    for path, tier, source in (
        (pinned or None, "operator_pin", "env"),
        (sa, "standalone", "owned_runtime"),
        (venv_python(base), "venv", "owned_runtime"),
    ):
        hit = _try(path, tier, source)
        if hit is not None:
            return hit

    if allow_ambient:
        v = (
            verify_interpreter(sys.executable, runner=runner)
            if verify
            else {"ok": True, "version": None}
        )
        return {
            "path": sys.executable,
            "tier": "ambient",
            "source": "ambient",
            "owned": False,
            "verified": bool(verify and v.get("ok")),
            "degraded": True,
            "version": v.get("version"),
            "reason": "ambient_escape",
            "checked": checked,
        }
    # Fail closed: no verified owned runtime, ambient not permitted.
    return {
        "path": None,
        "tier": "none",
        "source": "none",
        "owned": False,
        "verified": False,
        "degraded": False,
        "reason": "no_verified_owned_runtime",
        "checked": checked,
    }


# Fields the provisioner MUST have written for a manifest to vouch for a runtime
# without a fresh subprocess verify. A hand-written/partial manifest lacks these
# and is rejected → fresh verify.
_MANIFEST_REQUIRED = (
    "verified",
    "verified_at",
    "tier",
    "kind",
    "package",
    "expected_version",
    "python",
    "fingerprint",
)


def manifest_vouches(
    manifest: dict | None,
    python_path: str,
    expected_version: str | None,
) -> tuple[bool, str]:
    """A manifest may shortcut verification ONLY if the provisioner wrote it with
    explicit proof AND it still matches reality:
      * ``verified is True`` and all required provenance fields present,
      * recorded ``python`` is exactly this interpreter,
      * recorded ``version`` matches the expected law version (when one is
        required),
      * the recorded executable ``fingerprint`` still recomputes equal (no
        swap/tamper since the verified install).
    Returns (ok, reason). Anything missing/mismatched ⇒ (False, why).
    """
    if not isinstance(manifest, dict):
        return False, "no_manifest"
    if manifest.get("verified") is not True:
        return False, "manifest_not_verified"
    for key in _MANIFEST_REQUIRED:
        if manifest.get(key) in (None, ""):
            return False, f"manifest_missing:{key}"
    if str(manifest.get("python")) != str(python_path):
        return False, "manifest_python_mismatch"
    if expected_version is not None and str(manifest.get("version")) != str(expected_version):
        return False, "manifest_version_mismatch"
    fp = runtime_fingerprint(python_path)
    if not fp or fp != manifest.get("fingerprint"):
        return False, "fingerprint_mismatch"
    return True, ""


def owned_runtime_trust(
    python_path: str,
    home: Path | str,
    *,
    runner: RuntimeRunner | None = None,
    expected_version: str | None = None,
) -> dict:
    """Decide whether an OWNED-LOOKING interpreter may be trusted to carry
    enforcement. Trust requires EITHER a provisioner-written, still-valid
    verified manifest (see ``manifest_vouches`` — lets us skip a subprocess on
    the hot path) OR a fresh verification proving it imports the EXPECTED
    aidocs_mcp version (not just any aidocs_mcp). A hand-written, stale, or
    wrong-fingerprint manifest CANNOT bypass the fresh verify.
    Returns {ok, basis, version, reason}; ok=False ⇒ hooks must NOT pin to it.
    """
    m = read_manifest(home)
    vouch, why = manifest_vouches(m, python_path, expected_version)
    if vouch:
        return {"ok": True, "basis": "manifest", "version": m.get("version"), "reason": ""}
    v = verify_interpreter(python_path, runner=runner, expected_version=expected_version)
    if v["ok"]:
        return {"ok": True, "basis": "fresh_verify", "version": v.get("version"), "reason": ""}
    return {"ok": False, "basis": "none", "version": v.get("version"), "reason": v["reason"] or why}


# ── provisioning ─────────────────────────────────────────────────────────
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _installed_pkg_in(root: Path) -> Path:
    """Where ``aidocs_mcp`` lives (or WOULD live) under one owned runtime tree.

    ALWAYS returns a path, never None: ``runtime_freshness(installed_pkg=None)``
    falls back to the runtime under the REAL ``Path.home()``, which is a tree the
    current call may not be provisioning at all (every test, and any
    ``--home``-scoped invocation). A non-existent path instead makes
    ``runtime_freshness`` answer honestly — "nothing installed to compare",
    i.e. ``fresh=None`` — about the tree actually under management.
    """
    candidates = [root / "Lib" / "site-packages" / "aidocs_mcp"]  # Windows
    try:
        candidates += sorted(root.glob("lib/python*/site-packages/aidocs_mcp"))  # POSIX
    except OSError:
        pass
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _reference_pkg_for_spec(package_spec: str | None) -> Path | None:
    """The package tree a CHECKOUT-SHAPED ``package_spec`` carries, or None.

    ``runtime --fix --package <dir>`` hands the provisioner an explicit artefact
    (the deploy hands over the frozen, STAMPED ship-stage — 2026-08-22). The
    freshness probe that gates the reinstall must then compare the install
    against THAT tree, not against whatever checkout this process happens to
    run from: measured against the checkout, a stage whose code equals the
    checkout's reads "already fresh", the reinstall is skipped, and the stamp
    the whole hand-over exists to deliver never lands.

    Only a directory carrying ``pyproject.toml`` + ``server/aidocs_mcp/`` is a
    reference — the same shape ``_resolve_package`` recognises as a source
    tree. A version token, an index requirement, a wheel path or a plain
    directory has no tree to compare against and yields None, which leaves
    every existing caller's behaviour byte-identical.
    """
    spec = str(package_spec or "").strip()
    if not spec:
        return None
    try:
        root = Path(spec)
        pkg = root / "server" / "aidocs_mcp"
        if root.is_dir() and (root / "pyproject.toml").is_file() and pkg.is_dir():
            return pkg
    except (OSError, ValueError):
        return None
    return None


def _package_freshness(root: Path, *, source_pkg: Path | str | None = None) -> bool | None:
    """Does the package installed under ``root`` match its REFERENCE? (#560)

    The reference is CURRENT SOURCE unless ``source_pkg`` names another tree
    (the artefact an explicit ``--package`` handed over — see
    ``_reference_pkg_for_spec``). True / False / None exactly as
    ``runtime_freshness`` reports it, where None means "cannot compare" — NOT a
    verdict of stale. Never raises: a freshness probe is observability, and
    observability must never break a provision run, so any failure degrades to
    None, which leaves the previous behaviour intact.
    """
    try:
        return runtime_freshness(
            source_pkg=source_pkg, installed_pkg=_installed_pkg_in(root)
        ).get("fresh")
    except Exception:  # noqa: BLE001 — see docstring: degrade, never propagate
        return None


def _purge_stale_build_tree(target: str) -> str | None:
    """Drop ``<checkout>/build/lib`` before installing FROM A SOURCE CHECKOUT.

    setuptools' ``build_py`` stages the package into ``build/lib`` and is purely
    ADDITIVE: it copies changed files in and NEVER removes files that were
    deleted from source. Every wheel built from a checkout that has ever built
    before therefore re-ships every module ever deleted from it — permanently.

    Measured 2026-07-28: five modules retired by the dead-code sweep — among
    them a retired gate module and a retired enforcement-bypass module, which
    are deliberately NOT named here (their names are excised package-wide and
    naming them would resurrect the very strings the excision seal forbids) —
    were absent from source, absent from git, present in
    ``mcp/build/lib/aidocs_mcp/``, and therefore present and IMPORTABLE in the
    enforcement runtime after a clean ``--force-reinstall``. A retired gate
    module that still ships is a retired policy that can still be loaded, and it
    also makes source-vs-installed parity permanently unreachable.

    The purge is WHOLESALE (the whole ``build/lib`` stage, a regenerated
    artifact) — it never needs a per-module list, so no retired module name has
    to be spelled anywhere in this package.

    Only ever touches a build artifact inside the checkout being installed, and
    only when that path is a real project. Best-effort: never blocks the install.
    """
    try:
        root = Path(target)
        if not (root.is_dir() and (root / "pyproject.toml").is_file()):
            return None
        stage = root / "build" / "lib"
        if not stage.is_dir():
            return None
        shutil.rmtree(stage, ignore_errors=True)
    except OSError:
        return None
    return str(stage)


def _new_generation_id(target: str) -> str:
    """A fresh, filesystem-safe generation id.

    CONTENT IS NOT ENOUGH TO NAME IT. The obvious id — a hash of the package
    bytes — collides with itself on the case this exists to serve: reinstalling
    the SAME version because the installed tree is suspect. That build must get
    its own directory, or it would be written into the generation currently
    serving, which is the mutation being removed.

    NOR IS A TIMESTAMP. The first cut was stamp + spec digest, and
    test_the_previous_generation_is_retained_for_rollback caught two builds
    inside one second producing the SAME id — which would have had the second
    build install straight into the active generation, the exact defect, dressed
    as a fresh one. Uniqueness therefore comes from `os.urandom`, and the stamp
    stays only because it makes a directory listing readable.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    nonce = hashlib.sha256(
        os.urandom(16) + str(target).encode("utf-8", "replace")
    ).hexdigest()[:10]
    return f"gen-{stamp}-{nonce}"


def _discard_generation(gen_root: Path | None) -> None:
    """Drop a generation that never became active.

    SAFE BY POSITION, not by care: this only ever names a directory built in
    this call and never activated, so there is no path by which it removes the
    serving runtime. That is the operator's first negative — no failure path
    may mutate what is currently serving — held by structure rather than by a
    check that could be forgotten.
    """
    if gen_root is not None:
        shutil.rmtree(gen_root, ignore_errors=True)


# ── THE IN-PLACE SWAP MACHINERY IS GONE (#1030) ────────────────────────────
#
# `_quarantine_installed_package`, `_restore_quarantined_package`,
# `_discard_quarantined_package`, `_is_our_wheel`, `_staged_wheels` and
# `_install_package_into_venv` were deleted here, not merely stopped being
# called. Together they were the "BUILD FIRST, SWAP LAST" apparatus: a
# `pip wheel` staging phase, an unforced dependency install, then a rename of
# the serving `aidocs_mcp` tree followed by one offline wheel unpack — plus a
# restore-on-failure path, because a runtime with NO package is worse than one
# with a stale package.
#
# All of it existed to make a mutation of the SERVING tree survivable. #1030
# removes the mutation: the refresh builds a whole new generation beside the
# running one and swaps a pointer. Keeping the machinery "just in case" would
# leave a second, unexercised way to write into the live runtime, which is the
# thing being removed — and its own docstring called it "NOT ATOMIC, MERELY
# NARROW".
#
# WHAT WAS KEPT, because it was never about the swap:
#   * `_purge_stale_build_tree` — setuptools' additive `build/lib` stage
#     reships modules DELETED from source, so a wheel built from a checkout can
#     carry retired law. That belongs to the SOURCE TREE and now runs at the
#     top of the generational build.
#   * `--no-cache-dir` on every install — it defeats a DIFFERENT trap (a cached
#     wheel built from older source carrying the same version), which is still
#     live. Pinned by test_the_repair_never_force_reinstalls_anything.
# `--force-reinstall` was NOT kept: it defeated pip's NAME+VERSION "already
# satisfied" check against bytes already in place, and a generation created
# seconds ago has none.
def _tier_package_root(home: Path, tier: str | None) -> Path | None:
    """The owned tree whose site-packages a RESOLVED tier actually loads, or None.

    Only the tiers AIDOCS itself provisions can be answered: an operator pin or
    an ambient interpreter lives wherever the operator put it, and guessing a
    site-packages location for them would produce a fabricated freshness verdict
    — worse than None, which honestly means "cannot compare".
    """
    if tier == "venv":
        return _venv_dir(home)
    if tier == "standalone":
        return _standalone_dir(home)
    return None


def _tier_python(home: Path, tier: str | None) -> str | None:
    """The interpreter of a tier AIDOCS itself provisions, or None."""
    if tier == "venv":
        return venv_python(home)
    if tier == "standalone":
        return standalone_python(home)
    return None


def _stamp_installed_package(
    home: Path,
    reference_pkg: Path | str | None,
    tier: str | None = None,
) -> tuple[bool, str]:
    """Give the freshly-installed package a stamp naming WHAT IT IS (#867).

    THE MEASURED BLINDNESS. Only the PACKAGING step ever wrote a stamp
    (``build_signed_release.py``, deploy gate step 2a) and both write into the
    SHIP STAGE. The local provisioning path installed those bytes and wrote no
    stamp, so ``ai_version``'s ``running`` axis has never been able to name the
    build the operator's own daemon serves — measured 2026-08-24:
    ``running.known=false``, ``_build_stamp.py`` absent, while ``deployed`` and
    ``released`` were both cleanly sealed at 2.5.1 build 186.

    CARRY THE PROVENANCE, RECOMPUTE THE FINGERPRINT. The stamp's commit/version/
    build belong to whoever BUILT the artefact; copying them is the point — a
    locally invented commit would be a different build wearing the same bytes.
    The FINGERPRINT is a different question and must be recomputed over the
    INSTALLED tree, because it answers "have these bytes changed since I stamped
    them" (the tamper axis), not "are they identical to the stage" (the freshness
    axis). ``build_stamp`` already insists on that split: "verified means
    unchanged since install, fresh means matches source." Copying the stage's
    fingerprint verbatim would report MISMATCH forever, since a stage install
    legitimately byte-differs (136 .py files newline-differ, 27 of 28 Vite
    content-hashed assets moved — measured in ``_stamp_provenance_current``).

    NEVER FABRICATE. When the install source carries no stamp there is nothing
    here that can honestly name a commit, and ``write_build_stamp`` refuses such
    input by design. The package is left UNSTAMPED with a named reason rather
    than stamped with a lie: "a stamp that cannot name what it was built from is
    worse than no stamp — it asserts that provenance was recorded when it was
    not."

    Returns ``(stamped, reason)``; ``reason`` is empty only on success.
    """
    # THE TIER DECIDES WHERE THE BYTES LANDED, AND ONLY SOME TIERS CAN ANSWER.
    #
    # `home` is the user-home-scoped root, NOT the runtime root: a venv tier
    # installs under <home>/.aidocs/runtime/venv and a standalone under its own
    # tree. Resolving `_installed_pkg_in(home)` directly names
    # <home>/Lib/site-packages/aidocs_mcp, which exists for NEITHER tier — a
    # directory that is simply never there, so the stamp would silently never
    # land. Caught by probe_867_real_provision.py against a REAL provision after
    # the unit tests passed: they built that path by hand and so encoded the
    # mistake instead of the layout.
    #
    # `_tier_package_root` returns None for an operator pin or an ambient
    # interpreter, and its own docstring says why that must stay None: those live
    # "wherever the operator put it, and guessing a site-packages location for
    # them would produce a fabricated freshness verdict — worse than None". The
    # same reasoning binds here, and harder: this feeds a WRITE, so a guess would
    # not merely mis-report, it would stamp a tree nobody asked us to stamp.
    root = _tier_package_root(Path(home), tier)
    if root is None:
        return False, f"tier_not_provisioned_by_aidocs:{tier or 'unknown'}"
    inst = _installed_pkg_in(root)
    if not inst.is_dir():
        return False, "installed_package_missing"

    ref = Path(reference_pkg) if reference_pkg else None
    if ref is None or not ref.is_dir():
        return False, "no_reference_package: nothing names what these bytes were built from"

    try:
        from .build_stamp import read_build_stamp as _read

        provenance = _read(ref)
    except Exception as exc:  # noqa: BLE001 — an unreadable reference stamps nothing
        return False, f"reference_stamp_unreadable:{type(exc).__name__}"
    if not provenance:
        return False, (
            "no_stamped_reference: the install source carries no build stamp, so "
            "nothing here can name the commit these bytes were built from"
        )

    try:
        from .build_stamp import write_build_stamp as _write

        _write(
            inst,
            commit=str(provenance.get("commit") or ""),
            version=str(provenance.get("version") or ""),
            build=provenance.get("build"),
            builder=str(provenance.get("builder") or "") or "provisioned@local",
        )
    except Exception as exc:  # noqa: BLE001 — write_build_stamp REFUSES a lie; honour it
        return False, f"stamp_refused:{type(exc).__name__}:{exc}"[:400]
    return True, ""


def _record_package_after_install(
    home: Path,
    res: dict,
    runner: RuntimeRunner | None,
    *,
    reference_pkg: Path | str | None = None,
) -> dict:
    """REPAIR AND RE-RECORD ARE ONE OPERATION (#589 fix 3).

    THE MEASURED DEFECT. Provisioning CHANGES THE INSTALLED BYTES while the
    recorded package-trust row still describes the previous install. Installed
    != recorded → ``package_integrity`` reports drift → ``claude_hook.main()``
    DECLINES, emits no verdict, and Claude Code treats a verdict-less hook as
    "proceed": ~110 ungoverned tool calls in an hour on 2026-07-28, silent apart
    from a breadcrumb file, and it recurred the second time the runtime was
    repaired. The documented remedy was a three-command dance whose third step
    (`runtime --record-package`) a human had to remember; forgetting it silently
    disables enforcement. A repair that leaves the system ungoverned until a
    second command is not a repair.

    WHY HERE AND NOT IN A CALLER. ``runtime_refresh``, the deploy's step 5c and
    `runtime --fix` all provision through ``provision_venv`` /
    ``provision_standalone``; fixing one caller leaves the others broken. The
    function that changed the bytes owns re-recording them.

    ONLY ON A REAL CHANGE. ``action == "installed"`` is the sole trigger. A
    no-op provision must NOT re-record: continuously blessing whatever is
    installed would mask genuine drift and destroy the whole security value.
    Re-recording is legitimate only as "I just put these exact bytes here
    deliberately".

    UNDER THE RUNTIME INTERPRETER — the trap that cost an hour on the night this
    was found. ``--record-package`` records the provenance of the interpreter it
    RUNS UNDER: under the dev venv it records ``dev_editable`` ("fingerprint is
    informational only"); under the runtime venv it records ``official_wheel``,
    which is what the HOOKS actually check. An operator ran it under the dev
    venv, it reported success, and the gate stayed off. So the re-record runs
    ``res["python"]`` — the very interpreter this provision just installed into
    — with ``--home`` pinned to the home it installed under.

    NEVER DESTROYS THE REPAIR. ``ok``/``action`` are untouched; the install
    happened either way. A recording failure is reported DISTINGUISHABLY —
    ``package_recorded: False`` plus a ``package_record_reason``, and ``reason``
    becomes ``package_record_failed:…`` — because a repaired runtime with a
    stale trust row IS the ungoverned state, and the caller must be able to see
    it rather than read ok=True and stop looking (#560 defect 4's shape).
    """
    if not isinstance(res, dict) or not res.get("ok") or res.get("action") != "installed":
        return res
    py = res.get("python") or _tier_python(home, res.get("tier"))
    if not py:
        res["package_recorded"] = False
        res["package_record_reason"] = "runtime_interpreter_unresolved"
        res["reason"] = "package_record_failed:runtime_interpreter_unresolved"
        return res
    # ── #867: STAMP FIRST, RECORD SECOND. THE ORDER IS LOAD-BEARING. ──────────
    #
    # The trust row FINGERPRINTS THE INSTALLED BYTES, and the stamp IS installed
    # bytes. Written afterwards it would change the tree the row describes, so
    # installed != recorded the instant the provision completes — which is
    # exactly the drift that makes `claude_hook.main()` DECLINE, emit no verdict,
    # and lets Claude Code treat a verdict-less hook as "proceed" (~110
    # ungoverned tool calls on 2026-07-28). This function exists to END that
    # state; stamping after recording would re-create it by hand.
    #
    # NEVER DESTROYS THE REPAIR, the same posture as the recording below: `ok`
    # and `action` are untouched and a failure is reported DISTINGUISHABLY,
    # because an install that cannot name its own build is precisely the
    # blindness #867 measured, and the caller must be able to SEE it rather than
    # read ok=True and stop looking.
    _stamped, _stamp_why = _stamp_installed_package(home, reference_pkg, res.get("tier"))
    res["build_stamped"] = _stamped
    if not _stamped:
        res["build_stamp_reason"] = _stamp_why

    try:
        from . import package_integrity as _pi

        out = _pi.record_selected_interpreter_trust(
            home,
            str(py),
            source="provision",
            runner=runner,
            record_home=home,
        )
    except Exception as exc:  # never let bookkeeping undo a good install
        out = {"recorded": False, "method": "unavailable", "reason": repr(exc)}
    res["package_recorded"] = bool(out.get("recorded"))
    res["package_record_method"] = out.get("method")
    if not res["package_recorded"]:
        why = str(out.get("reason") or "unknown")[:200]
        res["package_record_reason"] = why
        res["reason"] = f"package_record_failed:{why}"
    return res


def provision_venv(
    home: Path | str,
    *,
    base_python: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
) -> dict:
    """Build/repair a dedicated AIDOCS-managed venv (degraded Tier-1) from a
    suitable base python and install the INTENDED aidocs_mcp version (or a local
    wheel/source) into it — never a floating ``--upgrade``. Verifies the
    EXPECTED version after install. Idempotent: a still-valid venv is a no-op
    unless ``force`` — but a STALE package is never "still valid" (see
    ``_provision_venv``). Fail-closed + truthful report; the freshness verdict
    that drove the decision is reported as ``package_fresh``.

    When it ACTUALLY INSTALLS, the package-trust row is re-recorded here as part
    of the same operation — see ``_record_package_after_install`` for why a
    separate manual step was an enforcement outage.
    """
    # Measured against the artefact HANDED OVER when there is one (the deploy's
    # stamped ship-stage), else against the checkout — see _reference_pkg_for_spec.
    fresh = _package_freshness(
        _venv_dir(Path(home)), source_pkg=_reference_pkg_for_spec(package_spec)
    )
    res = _provision_venv(
        home,
        base_python=base_python,
        package_spec=package_spec,
        expected_version=expected_version,
        runner=runner,
        force=force,
        package_fresh=fresh,
    )
    # A repair path must report what it CHANGED, not merely that it succeeded
    # (#560 defect 4 was exactly a path returning ok=True having changed nothing),
    # and this body has many exits — so stamp the verdict on all of them here.
    if isinstance(res, dict):
        res["package_fresh"] = fresh
        _record_package_after_install(
            Path(home),
            res,
            runner,
            # #867: the artefact these bytes came FROM is the only thing that can
            # name the commit they were built from. `_reference_pkg_for_spec`
            # already resolves it — its own docstring says why this matters:
            # "the deploy hands over the frozen, STAMPED ship-stage ... the stamp
            # the whole hand-over exists to deliver never lands." Now it lands.
            # A spec that is not a source tree (a version token, an index
            # requirement, a wheel) yields None, and the install is left honestly
            # UNSTAMPED with a named reason rather than stamped with a guess.
            reference_pkg=_reference_pkg_for_spec(package_spec),
        )
    return res


def _provision_venv(
    home: Path | str,
    *,
    base_python: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
    package_fresh: bool | None = None,
) -> dict:
    """The venv provisioning body. See ``provision_venv`` for the contract.

    ``package_fresh`` is the installed-vs-source verdict and gates the no-op the
    same way ``_provision_standalone`` is gated: a DEFINITE False falls through
    and reinstalls; True and None (unknown) keep the cheap no-op.
    """
    base = Path(home)
    run = runner or (lambda a: _default_runner(a))
    target, enforce = _resolve_package(package_spec, expected_version)
    existing = venv_python(base)
    if existing and not force and package_fresh is not False:
        v = verify_interpreter(existing, runner=runner, expected_version=enforce)
        if v["ok"]:
            return {
                "ok": True,
                "tier": "venv",
                "action": "noop",
                "python": existing,
                "version": v.get("version"),
                "package": target,
                "degraded": True,
                "reason": "",
            }
    src = base_python or sys.executable
    # ── BUILD BESIDE, NEVER INTO (#1030) ──────────────────────────────────
    #
    # Everything below is written into a BRAND-NEW generation directory. The
    # runtime that is currently serving is not renamed, deleted or overwritten
    # at any point, so no hook can observe it missing, partial or mixed — the
    # three states runtime_generations documents. It becomes visible only at
    # the atomic pointer flip after verification, near the end of this
    # function.
    #
    # A FRESH VENV, AND THE FULL DEPENDENCY INSTALL THAT IMPLIES. #589 went to
    # real trouble to avoid rebuilding the dependency tree, because those
    # minutes were spent WITH THE SERVING PACKAGE MOVED ASIDE — the cost was an
    # enforcement outage, not wall-clock. Off the serving path that argument
    # disappears: a slow build is now merely slow, and the gate keeps enforcing
    # under the old generation for its whole duration. Paying honestly for an
    # independent tree beats seeding from the live one, which would embed the
    # old venv's interpreter paths and make B quietly depend on A.
    # STILL PURGED, AND STILL FOR THE SAME REASON. setuptools' additive
    # `build/lib` stage in a source checkout re-ships modules DELETED from
    # source, so a wheel built from it carries retired law. That purge used to
    # live inside _install_package_into_venv; it belongs to the SOURCE TREE,
    # not to the swap, and the swap is what went away — so it moves here rather
    # than leaving with it.
    _purge_stale_build_tree(target)
    gen_id = _new_generation_id(target)
    gen_root = runtime_generations.generation_dir(base, gen_id)
    if gen_root is None:  # pragma: no cover - _new_generation_id shapes it
        return {
            "ok": False,
            "tier": "venv",
            "action": "create_failed",
            "reason": f"generation_id_invalid:{gen_id}",
            "degraded": True,
        }
    build_venv = gen_root / runtime_generations.LEGACY_VENV_DIRNAME
    try:
        gen_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "tier": "venv",
            "action": "create_failed",
            "reason": f"generation_mkdir:{exc}",
            "degraded": True,
        }
    code, out, err = run([src, "-m", "venv", str(build_venv)])
    if code != 0:
        _discard_generation(gen_root)
        return {
            "ok": False,
            "tier": "venv",
            "action": "create_failed",
            "reason": f"venv_create:{(err or out).strip()[:200]}",
            "generation": gen_id,
            "degraded": True,
        }
    py = _python_in(build_venv)
    if not py:
        _discard_generation(gen_root)
        return {
            "ok": False,
            "tier": "venv",
            "action": "create_failed",
            "reason": "venv_python_missing",
            "generation": gen_id,
            "degraded": True,
        }
    # Pin the intended version/artifact — no floating --upgrade.
    #
    # A RELEASE NUMBER IS A TAG, NOT A BUILD IDENTITY (operator ruling; #560).
    # pip keys BOTH its "requirement already satisfied" check AND its built-wheel
    # cache on NAME+VERSION. A source change that does not bump the version is
    # therefore invisible to it: pip reports success having installed the OLD
    # content. Measured consequence — this machine's hooks kept enforcing stale
    # law through a full deploy while every label agreed (installed 2.5.1,
    # expected 2.5.1, source sha f02d6c9e != installed sha 3d98e9f7), and the
    # deploy's own prescribed remedy was inert for the same reason.
    #
    # This is the SAME trap already documented on the VPS side, where it cost
    # five deploys before `rm -rf $WH` + `--no-cache-dir` landed in
    # vps_custody.sh::materialize. The local provisioner never received that
    # fix, so the two lanes disagreed about what "installed" means.
    #
    #   --no-cache-dir     never serve a wheel built from older source that
    #                      happens to carry the same version.
    #   --force-reinstall  ONLY when the freshness probe already determined the
    #                      installed package differs from source. Reinstalling a
    #                      known-stale package is the entire point; doing it
    #                      unconditionally would make every provision pay a full
    #                      dependency reinstall to fix nothing. It is ALWAYS
    #                      paired with --no-deps and always applied to a wheel
    #                      already built — see _install_package_into_venv for the
    #                      enforcement outage that taught us the ordering.
    # AN EMPTY GENERATION HAS NOTHING TO FORCE. --force-reinstall existed to
    # defeat pip's NAME+VERSION "already satisfied" check against bytes that
    # were already there; in a venv created seconds ago nothing is there, so
    # the trap the comment above describes cannot fire. --no-cache-dir stays:
    # it is what stops pip serving a wheel BUILT from older source that happens
    # to carry the same version, which is a different trap and still live.
    code, out, err = run([py, "-m", "pip", "install", "--no-cache-dir", target])
    if code != 0:
        _discard_generation(gen_root)
        return {
            "ok": False,
            "tier": "venv",
            "action": "install_failed",
            "reason": f"pip_install:{(err or out).strip()[:200]}",
            "python": py,
            "package": target,
            "package_swap": "generation",
            "generation": gen_id,
            "degraded": True,
        }
    # #733: a checkout install must carry the vendored palace engine too —
    # the local mirror of the VPS deploy's paired install. --no-deps because
    # mempalace's runtime deps are already mirrored in mcp/pyproject.toml
    # (the RFC-4 block); resolving its own pins here would add a second,
    # unpinned dependency authority. --force-reinstall for the same reason
    # the main package needs it: pip keys "already satisfied" on
    # NAME+VERSION, and the vendored tree's version rarely bumps, so an
    # unforced install would silently keep stale engine bytes forever.
    vendor = vendored_mempalace_dir(target)
    if vendor is not None:
        code, out, err = run(
            [
                py,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--force-reinstall",
                "--no-deps",
                str(vendor),
            ],
        )
        if code != 0:
            _discard_generation(gen_root)
            return {
                "ok": False,
                "tier": "venv",
                "action": "install_failed",
                "reason": f"pip_install_mempalace:{(err or out).strip()[:200]}",
                "python": py,
                "package": target,
                "package_swap": "generation",
                "generation": gen_id,
                "degraded": True,
            }
    # VERIFY B BEFORE IT CAN SERVE ANYTHING. This runs B's own interpreter and
    # imports the package out of B, so "the generation works" is measured, not
    # assumed. It is also the operator's second negative: a failed verification
    # must leave A and the pointer byte-for-byte unchanged — and it does,
    # because A was never touched and the pointer is not written until below.
    v = verify_interpreter(py, runner=runner, expected_version=enforce)
    if not v["ok"]:
        _discard_generation(gen_root)
        return {
            "ok": False,
            "tier": "venv",
            "action": "verify_failed",
            "reason": v["reason"],
            "python": py,
            "package": target,
            "package_swap": "generation",
            "generation": gen_id,
            "degraded": True,
        }
    # SEAL, THEN FLIP. The marker is the LAST write of the build, so a kill at
    # any earlier point leaves a directory that can never be activated rather
    # than one that looks activatable. The flip itself is a single os.replace.
    if not runtime_generations.mark_complete(
        base,
        gen_id,
        {
            "python": py,
            "version": v.get("version"),
            "package": target,
            "built_at": _now(),
        },
    ):
        _discard_generation(gen_root)
        return {
            "ok": False,
            "tier": "venv",
            "action": "verify_failed",
            "reason": "generation_seal_failed",
            "python": py,
            "package": target,
            "package_swap": "generation",
            "generation": gen_id,
            "degraded": True,
        }
    activated, why = runtime_generations.activate(base, gen_id)
    if not activated:
        # B is complete and A is untouched: nothing is broken, the switch just
        # did not happen. Report it rather than leaving the caller to infer
        # from a success that changed nothing.
        return {
            "ok": False,
            "tier": "venv",
            "action": "activate_failed",
            "reason": f"generation_activate:{why}",
            "python": py,
            "package": target,
            "package_swap": "generation",
            "generation": gen_id,
            "degraded": True,
        }
    _write_manifest(
        base,
        {
            "tier": "venv",
            "kind": "venv",
            "python": py,
            "version": v.get("version"),
            "expected_version": enforce or v.get("version"),
            "base_python": src,
            "package": target,
            # A venv is a degraded fallback over an arbitrary base python — never the
            # blessed standalone, so always CUSTOM provenance.
            "source": "managed_venv",
            "blessed": False,
            "provenance_class": "custom",
            "installed_at": _now(),
            "verified": True,
            "verified_at": _now(),
            "fingerprint": runtime_fingerprint(py),
        },
    )
    # A is retained, NOT collected. Calls that were already inside A finish
    # there, and it is the rollback target until something later decides it is
    # safe to collect. Deleting it here would reintroduce the hazard this whole
    # change removes, one step further down the timeline.
    return {
        "ok": True,
        "tier": "venv",
        "action": "installed",
        "python": py,
        "version": v.get("version"),
        "package": target,
        "generation": gen_id,
        # How wide the no-package window was. "generation" means ZERO: the new
        # runtime was built beside the serving one and became visible in a
        # single pointer replace. The older values ("none"/"staged"/
        # "unstaged") described widths of an in-place swap that no longer
        # happens on this path.
        "package_swap": "generation",
        "degraded": True,
        "blessed": False,
        "provenance_class": "custom",
        "reason": "",
    }


def _default_extractor(archive: Path, dest: Path) -> None:
    import shutil

    shutil.unpack_archive(str(archive), str(dest))


def provision_standalone(
    home: Path | str,
    *,
    spec: dict | None = None,
    offline_archive: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
) -> dict:
    """Install a pinned standalone CPython atomically and put aidocs_mcp in it.

    Thin public face over ``_provision_standalone``: it computes the
    installed-vs-source freshness verdict, hands it to the body (which lets it
    gate the no-op), and stamps it onto the result as ``package_fresh`` on EVERY
    exit. A repair path must report what it CHANGED, not merely that it
    succeeded — #560 defect 4 is precisely a path that returned ok=True having
    changed nothing, so the verdict that decided the outcome has to be visible.

    Same tier-symmetric contract as ``provision_venv``: an install that actually
    happened re-records package trust in the same operation, under the runtime
    interpreter it just installed into (``_record_package_after_install``).
    """
    fresh = _package_freshness(_standalone_dir(Path(home)))
    res = _provision_standalone(
        home,
        spec=spec,
        offline_archive=offline_archive,
        package_spec=package_spec,
        expected_version=expected_version,
        downloader=downloader,
        extractor=extractor,
        installer=installer,
        runner=runner,
        force=force,
        package_fresh=fresh,
    )
    if isinstance(res, dict):
        res["package_fresh"] = fresh
        _record_package_after_install(
            Path(home),
            res,
            runner,
            # #867: the artefact these bytes came FROM is the only thing that can
            # name the commit they were built from. `_reference_pkg_for_spec`
            # already resolves it — its own docstring says why this matters:
            # "the deploy hands over the frozen, STAMPED ship-stage ... the stamp
            # the whole hand-over exists to deliver never lands." Now it lands.
            # A spec that is not a source tree (a version token, an index
            # requirement, a wheel) yields None, and the install is left honestly
            # UNSTAMPED with a named reason rather than stamped with a guess.
            reference_pkg=_reference_pkg_for_spec(package_spec),
        )
    return res


def _provision_standalone(
    home: Path | str,
    *,
    spec: dict | None = None,
    offline_archive: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
    package_fresh: bool | None = None,
) -> dict:
    """Install a pinned standalone CPython atomically and put aidocs_mcp in it.

    Order, all fail-closed:
      1. Resolve spec (arg → PINNED[platform]); none AND no offline archive →
         honest degrade (caller falls back to venv).
      2. Acquire the archive (offline path, else download the pinned URL).
      3. SHA256-verify against the pin. MISMATCH → abort, install NOTHING.
      4. Extract to a staging dir on the same filesystem as the target.
      5. Install aidocs_mcp INTO THE STAGING TREE and verify it imports there.
         Failure → discard the staging tree; the live runtime was never touched.
      6. Only then swap: rename the old tree away, rename the new one in. The
         no-runtime window is the gap between two renames, not an install.
      6. Write the manifest (tier=standalone) only on full success.
    Idempotent: a verified standalone already present is a no-op unless force —
    OR unless ``package_fresh`` is a definite False (see the branch below).
    """
    base = Path(home)
    operator_spec = spec is not None  # caller supplied their own pin
    spec = dict(spec) if spec else pinned_spec()
    pkg, enforce = _resolve_package(package_spec, expected_version)
    # OFFICIAL/BLESSED only when installed from OUR PINNED table at the blessed
    # version with no operator override. An operator offline archive or explicit
    # spec is preserved but recorded as CUSTOM provenance.
    blessed = bool(
        not operator_spec
        and not offline_archive
        and is_blessed_version((spec or {}).get("version")),
    )
    prov_class = "official" if blessed else "custom"
    existing = standalone_python(base)
    # #560 defect 4: idempotence used to be keyed on the INTERPRETER alone.
    # verify_interpreter compares a version LABEL and nothing compares package
    # CONTENT, so once a standalone existed at the expected version EVERY later
    # setup was a permanent no-op and every commit after the first install was
    # structurally invisible — a repealed gate rule kept enforcing for a full day
    # while setup printed "trust chain proven end-to-end". A DEFINITE False now
    # falls through and re-provisions. None must NOT: runtime_freshness answers
    # None whenever no source checkout resolves, which on a public install is
    # ALWAYS, so forcing on None would re-provision on every invocation for every
    # public user. That axis (installed-vs-published-release) belongs to
    # `aidocs update`, not here. Unknown is not a pass, but neither is it stale.
    if existing and not force and package_fresh is not False:
        v = verify_interpreter(existing, runner=runner, expected_version=enforce)
        if v["ok"]:
            return {
                "ok": True,
                "tier": "standalone",
                "action": "noop",
                "python": existing,
                "version": v.get("version"),
                "package": pkg,
                "owned": True,
                "blessed": blessed,
                "provenance_class": prov_class,
                "reason": "",
            }
    expected_sha = str((spec or {}).get("sha256") or "").strip().lower()
    url = str((spec or {}).get("url") or "").strip()
    if not offline_archive and not url:
        return {
            "ok": False,
            "tier": "standalone",
            "action": "no_pin",
            "reason": "no_pin_for_platform",
            "degraded_to": "venv",
        }
    if not expected_sha:
        return {
            "ok": False,
            "tier": "standalone",
            "action": "refused",
            "reason": "no_pinned_sha256",
            "degraded_to": "venv",
        }

    # Stage on the SAME filesystem as the target so the os.replace swap below is
    # atomic (and not a cross-disk move). The system temp dir is often on a
    # different drive than ~/.aidocs/runtime (e.g. C: temp vs a D: home), which
    # makes os.replace(staging, target) raise WinError 17 / cross-device EXDEV.
    rroot = runtime_root(base)
    rroot.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".aidocs-rt-stage-", dir=rroot))
    try:
        # The download target MUST keep the URL's extension:
        # shutil.unpack_archive infers the format from the FILENAME, so the
        # old bare `work / "dist"` made every successful, sha-verified
        # download explode with "Unknown archive format" (operator-hit
        # 2026-07-18 running `aidocs runtime --fix`).
        if offline_archive:
            archive = Path(offline_archive)
        else:
            from urllib.parse import unquote, urlparse

            _url_name = unquote(Path(urlparse(url).path).name).strip()
            archive = work / (_url_name or "dist.tar.gz")
        if offline_archive:
            if not archive.is_file():
                return {
                    "ok": False,
                    "tier": "standalone",
                    "action": "archive_missing",
                    "reason": f"offline_archive_not_found:{archive}",
                }
        else:
            dl = downloader or _default_download
            dl(url, archive)
        actual = sha256_file(archive)
        if actual != expected_sha:
            # FAIL CLOSED: never install an archive whose hash we can't vouch.
            return {
                "ok": False,
                "tier": "standalone",
                "action": "checksum_mismatch",
                "reason": f"sha256 {actual} != pinned {expected_sha}",
            }
        staging = work / "stage"
        staging.mkdir(parents=True, exist_ok=True)
        (extractor or _default_extractor)(archive, staging)
        target = _standalone_dir(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        # BUILD FIRST, SWAP LAST — the same law the venv tier learned the hard
        # way (see _install_package_into_venv). The old ordering swapped the
        # freshly EXTRACTED tree into place and only THEN pip-installed
        # aidocs_mcp into it, so the live enforcement runtime spent an entire pip
        # install with an interpreter that could not import the package. Hooks
        # run `<runtime>/python -m aidocs_mcp.claude_hook`; a hook that cannot
        # import emits no verdict, and Claude Code reads no verdict as "proceed".
        # Installing and verifying INSIDE the staging tree moves all of that
        # before the swap, and leaves the swap as two directory renames.
        staged_py = _python_in(staging)
        try:
            if installer is not None:
                ires = installer(staged_py or "")
                if not (ires or {}).get("ok", True):
                    raise RuntimeError(str(ires.get("reason")))
            elif staged_py:
                # --no-cache-dir for the same reason as the venv tier: the
                # interpreter here is freshly extracted so nothing is "already
                # satisfied", but pip's BUILT-WHEEL cache is keyed on
                # NAME+VERSION and would happily serve a wheel built from older
                # source under an unchanged version number. A release number is
                # a tag, not a build identity. No --force-reinstall: this tree
                # is brand new, so there is nothing to force past — and nothing
                # to move aside either, which is why this tier needs no
                # quarantine dance.
                _purge_stale_build_tree(pkg)
                code, out, err = (runner or (lambda a: _default_runner(a)))(
                    [staged_py, "-m", "pip", "install", "--no-cache-dir", pkg],
                )
                if code != 0:
                    raise RuntimeError(f"pip_install:{(err or out)[:200]}")
                # #733: a checkout install carries the vendored palace engine
                # too (see vendored_mempalace_dir / _provision_venv — same
                # rationale, staged tree so no force/quarantine needed here:
                # this tree is brand new).
                vendor = vendored_mempalace_dir(pkg)
                if vendor is not None:
                    code, out, err = (runner or (lambda a: _default_runner(a)))(
                        [
                            staged_py,
                            "-m",
                            "pip",
                            "install",
                            "--no-cache-dir",
                            "--no-deps",
                            str(vendor),
                        ],
                    )
                    if code != 0:
                        raise RuntimeError(
                            f"pip_install_mempalace:{(err or out)[:200]}"
                        )
            v = verify_interpreter(staged_py or "", runner=runner, expected_version=enforce)
            if not v["ok"]:
                raise RuntimeError(v["reason"])
        except Exception as exc:
            # NOTHING HAS BEEN SWAPPED. The previous runtime is untouched and
            # still enforcing, so there is no rollback to perform — the failure
            # costs a discarded staging tree and nothing else. (The old code had
            # to rmtree the half-installed target and restore a backup here.)
            return {
                "ok": False,
                "tier": "standalone",
                "action": "verify_failed",
                "reason": str(exc),
                "python": staged_py,
            }
        # THE SWAP — two renames on one filesystem, no install between them.
        # `os.replace` on a directory refuses an existing destination on Windows,
        # so the old tree is renamed AWAY first and the new one renamed IN. The
        # window with no runtime at `target` is the gap between these two calls:
        # a rename, not an install. Not a single atomic operation — Windows has
        # no atomic directory exchange — but microseconds rather than minutes,
        # and stated as such rather than dressed up as atomic.
        if target.exists():
            backup = target.with_name(f"cpython.old-{int(time.time())}")
            os.replace(target, backup)
        else:
            backup = None
        try:
            os.replace(staging, target)
        except OSError as exc:
            if backup is not None:
                try:
                    os.replace(backup, target)  # put the working runtime back
                except OSError:
                    pass
            return {
                "ok": False,
                "tier": "standalone",
                "action": "swap_failed",
                "reason": f"runtime_swap:{exc}",
                "python": staged_py,
            }
        py = standalone_python(base)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        _write_manifest(
            base,
            {
                "tier": "standalone",
                "kind": "standalone",
                "python": py,
                "version": v.get("version"),
                "expected_version": enforce or v.get("version"),
                "url": url or "(offline)",
                "sha256": expected_sha,
                "package": pkg,
                "source": "offline_archive" if offline_archive else "download",
                "blessed": blessed,
                "provenance_class": prov_class,
                "installed_at": _now(),
                "verified": True,
                "verified_at": _now(),
                "fingerprint": runtime_fingerprint(py),
            },
        )
        return {
            "ok": True,
            "tier": "standalone",
            "action": "installed",
            "python": py,
            "version": v.get("version"),
            "package": pkg,
            "owned": True,
            "blessed": blessed,
            "provenance_class": prov_class,
            "reason": "",
        }
    finally:
        # NOTE: no local `import shutil` here. A function-scoped import would
        # rebind the name for the WHOLE function, so the module-level shutil
        # used earlier (the post-swap backup cleanup) would raise
        # UnboundLocalError on any path that reaches it before this line.
        shutil.rmtree(work, ignore_errors=True)


def _default_download(url: str, dest: Path) -> None:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)


def spec_from_operator(
    *,
    offline_archive: str | None = None,
    sha256: str | None = None,
    url: str | None = None,
    manifest_file: str | None = None,
) -> dict | None:
    """Build a standalone install spec from explicit OPERATOR input for a
    platform PINNED has no entry for. Either a manifest JSON ({url?, sha256}) or
    an explicit --sha256 (+ optional --url). Returns None when nothing usable was
    given, so the caller still fails closed (no SHA ⇒ refused downstream).
    """
    if manifest_file:
        try:
            data = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(data, dict) and data.get("sha256"):
            return {
                "url": str(data.get("url") or url or "(offline)"),
                "sha256": str(data["sha256"]).strip().lower(),
            }
        return None
    if sha256:
        return {
            "url": str(url or "(offline)") if (url or offline_archive) else "",
            "sha256": str(sha256).strip().lower(),
        }
    return None


def ensure_runtime(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    spec: dict | None = None,
    offline_archive: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = None,
    base_python: str | None = None,
    allow_venv_fallback: bool = True,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
    force: bool = False,
) -> dict:
    """Provision the best available owned runtime: standalone first, then a
    degraded venv. Honest: reports which tier was achieved and why standalone
    was skipped (no pin / checksum / verify). Operator-pinned AIDOCS_PYTHON is
    respected by resolution and never overwritten by provisioning.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    # Operator pin wins — don't provision over an explicit, verified pin.
    pin = str(e.get("AIDOCS_PYTHON") or "").strip()
    if pin and Path(pin).is_file():
        v = verify_interpreter(pin, runner=runner, expected_version=expected_version)
        if v["ok"]:
            return {
                "ok": True,
                "tier": "operator_pin",
                "action": "respected",
                "python": pin,
                "version": v.get("version"),
                "owned": True,
                "reason": "",
            }
    sa = provision_standalone(
        base,
        spec=spec,
        offline_archive=offline_archive,
        package_spec=package_spec,
        expected_version=expected_version,
        downloader=downloader,
        extractor=extractor,
        installer=installer,
        runner=runner,
        force=force,
    )
    if sa.get("ok"):
        return sa
    if not allow_venv_fallback:
        return sa
    ve = provision_venv(
        base,
        base_python=base_python,
        package_spec=package_spec,
        expected_version=expected_version,
        runner=runner,
        force=force,
    )
    ve["standalone_skipped"] = sa.get("reason") or sa.get("action")
    return ve


def prepare_owned_runtime_for_setup(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    expected_version: str | None = "__current__",
    base_python: str | None = None,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
) -> dict:
    """Setup helper: resolve an owned runtime VERIFYING the EXPECTED aidocs_mcp
    law version, and PROVISION one (pinned to that version) when none verifies —
    so drift is caught at provision/resolve time, not left for hook verification.
    Never installs hooks and never escapes to ambient; the caller decides that.
    Returns {ok, tier, python, blessed, provenance_class, expected_version,
    provisioned, provision_result}.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    exp = expected_aidocs_version() if expected_version == "__current__" else expected_version
    rt = resolve_runtime(base, e, runner=runner, allow_ambient=False, expected_version=exp)
    provisioned: dict | None = None
    if rt.get("tier") == "none":
        provisioned = ensure_runtime(
            base,
            e,
            expected_version=exp,
            base_python=base_python,
            downloader=downloader,
            extractor=extractor,
            installer=installer,
            runner=runner,
        )
        rt = resolve_runtime(base, e, runner=runner, allow_ambient=False, expected_version=exp)
    manifest = read_manifest(base)
    blessed = bool(
        rt.get("tier") == "standalone"
        and rt.get("owned")
        and manifest
        and manifest.get("blessed") is True
        and str(manifest.get("python")) == str(rt.get("path"))
        and is_blessed_version(rt.get("version")),
    )
    return {
        "ok": rt.get("tier") != "none",
        "tier": rt.get("tier"),
        "python": rt.get("path"),
        "version": rt.get("version"),
        "blessed": blessed,
        "provenance_class": ("official" if blessed else "custom" if rt.get("owned") else "none"),
        "expected_version": exp,
        "provisioned": provisioned is not None,
        "provision_result": provisioned,
    }


# ── doctor / drift repair ────────────────────────────────────────────────
def _provenance(manifest: dict | None) -> dict:
    """A truthful, compact provenance view of how the runtime was obtained."""
    if not manifest:
        return {"source": "none", "kind": None, "version": None}
    return {
        "source": manifest.get("source"),
        "kind": manifest.get("kind"),
        "version": manifest.get("version"),
        "package": manifest.get("package"),
        "sha256": manifest.get("sha256"),
        "url": manifest.get("url"),
        "base_python": manifest.get("base_python"),
        "installed_at": manifest.get("installed_at"),
        "verified": bool(manifest.get("verified")),
        "verified_at": manifest.get("verified_at"),
        "fingerprint": manifest.get("fingerprint"),
        # official (blessed PINNED) vs operator-custom; default custom when an
        # older manifest predates the field.
        "blessed": bool(manifest.get("blessed")),
        "provenance_class": manifest.get("provenance_class")
        or ("official" if manifest.get("blessed") else "custom"),
    }


def doctor(
    home: Path | str | None = None,
    env: dict | None = None,
    *,
    fix: bool = False,
    rebuild: bool = False,
    offline_archive: str | None = None,
    base_python: str | None = None,
    spec: dict | None = None,
    sha256: str | None = None,
    url: str | None = None,
    manifest_file: str | None = None,
    package_spec: str | None = None,
    expected_version: str | None = "__current__",
    allow_ambient: bool = False,
    downloader: Callable[[str, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    installer: Callable[[str], dict] | None = None,
    runner: RuntimeRunner | None = None,
) -> dict:
    """Truthful runtime health + repair. ``--check`` (default) reports the
    resolved tier, manifest, drift, and PROVENANCE (source/version/sha/base).
    ``--fix`` provisions an owned runtime if none verifies, AND reinstalls an
    already-owned venv whose installed package has drifted from source
    (``package_fresh is False``); ``--rebuild`` forces a fresh standalone (then
    venv) install. Verification proves the EXPECTED
    aidocs_mcp version (this process's, unless overridden). When PINNED has no
    platform entry an operator supplies --offline-archive + --sha256 (or a
    manifest). Exit-code truth: ok iff a verified owned runtime resolves after.
    """
    base = Path(home) if home else Path.home()
    e = env if env is not None else os.environ
    exp = expected_aidocs_version() if expected_version == "__current__" else expected_version
    if spec is None:
        spec = spec_from_operator(
            offline_archive=offline_archive,
            sha256=sha256,
            url=url,
            manifest_file=manifest_file,
        )
    manifest = read_manifest(base)
    before = resolve_runtime(
        base,
        e,
        runner=runner,
        allow_ambient=allow_ambient,
        expected_version=exp,
    )
    drift = bool(
        manifest
        and manifest.get("python")
        and not (
            verify_interpreter(str(manifest["python"]), runner=runner, expected_version=exp)["ok"]
        ),
    )

    actions: list[dict] = []
    # THE THIRD DRIFT AXIS GATES REPAIR, NOT JUST REPORTING (#569 #586).
    #
    # `--fix` used to trigger on TIER ALONE — `before.tier in ("none",
    # "ambient")`. A runtime that resolves as an owned, verified venv therefore
    # never entered the repair branch at all, no matter how stale its code was:
    # `verified` means "unchanged since install", NOT "matches source". Measured
    # consequence (2026-07-28): `runtime --fix --json` returned
    # ok=True tier=venv owned=True verified=True with **actions: []** while the
    # installed fingerprint sat a day behind source, and every refresher and
    # deploy step above it read that as a successful repair. The pip
    # --no-cache-dir/--force-reinstall fix landed earlier was correct and inert,
    # because the pip call was never reached.
    #
    # A DEFINITE False is the only trigger. True and None (cannot compare) keep
    # the cheap no-op, exactly as `_provision_venv` gates its own no-op: a
    # freshness probe that cannot answer must not provoke a reinstall.
    fresh_root = _tier_package_root(base, before.get("tier"))
    # An explicit --package names the reference (2026-08-22): the deploy hands
    # over the stamped ship-stage, and "fresh vs the checkout" would skip the
    # very reinstall that lands the stamp. See _reference_pkg_for_spec.
    package_fresh = (
        _package_freshness(fresh_root, source_pkg=_reference_pkg_for_spec(package_spec))
        if fresh_root
        else None
    )

    actions: list[dict] = []
    if rebuild or (fix and before.get("tier") in ("none", "ambient")):
        res = ensure_runtime(
            base,
            e,
            spec=spec,
            offline_archive=offline_archive,
            package_spec=package_spec,
            expected_version=exp,
            base_python=base_python,
            downloader=downloader,
            extractor=extractor,
            installer=installer,
            runner=runner,
            force=bool(rebuild),
        )
        actions.append(res)
    elif fix and package_fresh is False and before.get("tier") == "venv":
        # Re-provision THE TIER THAT IS ALREADY RESOLVED rather than routing
        # through ensure_runtime: the runtime is fine, only its package is
        # stale, and escalating to the standalone tier would put a CPython
        # download on a repair path whose whole job is one pip install.
        # provision_venv re-probes freshness itself and reports `package_fresh`,
        # so the reinstall decision stays owned by one function.
        res = provision_venv(
            base,
            base_python=base_python,
            package_spec=package_spec,
            expected_version=exp,
            runner=runner,
        )
        actions.append(res)

    after = resolve_runtime(
        base,
        e,
        runner=runner,
        allow_ambient=allow_ambient,
        expected_version=exp,
    )
    manifest = read_manifest(base)
    owned_ok = bool(after.get("owned") and after.get("verified"))
    # BLESSED only when the resolved interpreter IS the manifest's official
    # standalone. operator_pin/venv/offline/ambient are all operator-custom.
    blessed = bool(
        owned_ok
        and after.get("tier") == "standalone"
        and manifest
        and manifest.get("blessed") is True
        and str(manifest.get("python")) == str(after.get("path"))
        and is_blessed_version(after.get("version")),
    )
    provenance_class = "official" if blessed else ("custom" if after.get("owned") else "none")
    report = {
        "ok": owned_ok,
        "tier": after.get("tier"),
        "python": after.get("path"),
        "owned": bool(after.get("owned")),
        "verified": bool(after.get("verified")),
        "degraded": bool(after.get("degraded")),
        "drift_detected": drift,
        "expected_version": exp,
        "resolved_version": after.get("version"),
        "blessed_version": BLESSED_PYTHON,
        "blessed": blessed,
        "provenance_class": provenance_class,
        "provenance": _provenance(manifest),
        "manifest": manifest,
        "checked": after.get("checked", []),
        "actions": actions,
        # The verdict that DROVE the repair decision, always reported: a repair
        # path must say what it saw, not merely that it succeeded. False here
        # with an empty `actions` list is the exact signature of the defect this
        # field exists to make impossible to miss.
        "package_fresh": package_fresh,
        "mode": "rebuild" if rebuild else ("fix" if fix else "check"),
    }
    # Trusted-code boundary: report the CANONICAL package trust (SQLite first,
    # runtime.json only as a degraded projection — truthfully labelled).
    try:
        from . import package_integrity as _pi

        v = _pi.verify_package_integrity(base)
        report["package_trust"] = {
            k: v.get(k)
            for k in (
                "ok",
                "provenance",
                "status",
                "mutable",
                "drifted",
                "unverified",
                "remote_trustworthy",
                "version",
                "trust_source",
                "stale_projection",
            )
        }
        # End-to-end selected-runtime trust-chain proof (home-side; no project
        # .mcp.json in the doctor context). Truthful trust_unrecorded/degraded.
        report["trust_chain"] = _pi.prove_trust_chain(
            base,
            project_root=None,
            python_path=after.get("path"),
            expected_version=exp,
        )
    except Exception as _exc:  # never break the doctor
        report["package_trust"] = {"ok": None, "error": repr(_exc)}
        report["trust_chain"] = {"ok": None, "error": repr(_exc)}
    if not owned_ok and after.get("tier") == "ambient":
        report["reason"] = (
            "Only ambient sys.executable resolves — enforcement hooks must not "
            "depend on it. Run `aidocs runtime --fix` (or --offline-archive) to "
            "provision an AIDOCS-owned runtime."
        )
    elif not owned_ok:
        report["reason"] = "No verified AIDOCS-owned runtime. Run `aidocs runtime --fix`."
    return report
