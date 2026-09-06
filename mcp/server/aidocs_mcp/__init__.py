"""AIDOCS MCP file-backed services."""

from __future__ import annotations

import sys
from pathlib import Path

# Arm the SQLite connect sampler before any store module is imported, so the
# global wrapper is in place for the direct-connect callers too. Entirely inert
# unless AIDOCS_SQLITE_SAMPLE is set (P0 runaway 2026-08-05).
#
# The phrase above deliberately avoids spelling the raw call: #755's chokepoint
# pin scans TEXT, not the AST, so even a mention inside a comment registers this
# file as a bypass. A guard that cannot tell prose from code is still worth
# keeping (it never misses a real one) -- so the prose moves instead.
from ._sqlite_connect_sampler import install_global_hook as _install_sqlite_sampler

_install_sqlite_sampler()

# RFC-4 / #733: ``import mempalace`` must resolve to the AIDOCS-CONTROLLED
# copy of the palace engine, in BOTH postures:
#
#   SOURCE CHECKOUT   <repo>/third_party/mempalace holds the vendored tree.
#                     It is found by a bounded MARKER WALK (no parents[N]
#                     arithmetic — the fixed-depth form was exactly the #733
#                     bug: correct in a checkout, silently wrong under
#                     site-packages) and prepended to sys.path so the
#                     in-repo copy wins over any installed distribution.
#
#   INSTALLED         no checkout exists on disk. The engine must have been
#                     installed INTO the environment as a distribution — the
#                     runtime provisioner installs the vendored tree
#                     alongside aidocs_mcp (mirroring the VPS deploy). Here
#                     ``importlib.util.find_spec`` answers.
#
# If NEITHER resolves, this install cannot start the server. That fact is
# recorded here and raised loudly by ``require_mempalace()`` at server
# startup — NOT at package import, deliberately: `python -m aidocs_mcp.cli
# runtime --fix` is the documented repair path and must keep importing on
# the very machines this failure describes. Raising here would brick the
# repair tool and make the defect self-sealing (the #726 pattern).
_MEMPALACE_SEARCHED: list[str] = []


def _locate_vendor_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``third_party/mempalace`` that
    actually CONTAINS the engine package. Returns None when no ancestor
    carries it (the installed posture)."""
    for ancestor in start.parents:
        vendor = ancestor / "third_party" / "mempalace"
        _MEMPALACE_SEARCHED.append(str(vendor))
        try:
            if (vendor / "mempalace" / "__init__.py").is_file():
                return vendor
        except OSError:
            continue
    return None


def _wire_vendored_mempalace() -> str | None:
    """Resolve the palace engine; returns a human-readable provenance note,
    or None when unresolvable (recorded, not raised — see above)."""
    vendor = _locate_vendor_root(Path(__file__).resolve())
    if vendor is not None:
        p = str(vendor)
        if p not in sys.path:
            sys.path.insert(0, p)
        return f"vendored-checkout:{p}"
    import importlib.util

    try:
        if importlib.util.find_spec("mempalace") is not None:
            return "installed-distribution"
    except (ImportError, ValueError):
        pass
    return None


_MEMPALACE_RESOLUTION = _wire_vendored_mempalace()


def require_mempalace() -> None:
    """Assert the palace engine is resolvable — loud, early, specific.

    Called by ``create_server()`` before its hard ``import mempalace`` so a
    broken install fails at startup with a message naming what was looked
    for and where, instead of a bare ModuleNotFoundError (#733: that bare
    error cost two days of silent overlap-restart failures)."""
    if _MEMPALACE_RESOLUTION is not None:
        return
    searched = "\n  ".join(_MEMPALACE_SEARCHED) or "(no ancestors walked)"
    raise ModuleNotFoundError(
        "AIDOCS cannot resolve the vendored palace engine 'mempalace' (#733).\n"
        "Looked for third_party/mempalace/mempalace/__init__.py under every "
        "ancestor of this package:\n  "
        f"{searched}\n"
        "and found no installed 'mempalace' distribution either "
        f"(sys.prefix={sys.prefix}).\n"
        "This runtime cannot start the MCP server. Remedy: on a machine with "
        "a source checkout run `aidocs runtime --fix` (it installs the "
        "vendored engine into the owned runtime); a pip-only install needs a "
        "release whose wheel ships mempalace.",
        name="mempalace",
    )


def _version_from_pyproject() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:
        return "0.0.0"

    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        version = project.get("version")
        return str(version) if version else "0.0.0"
    except Exception:
        return "0.0.0"


#: The committed build ticker, beside pyproject.toml so it is read exactly the
#: way the version is. It is a BUILD INPUT, not a deploy side effect (option B,
#: operator ruling 2026-08-21) — see _build_from_ticker.
BUILD_NUMBER_REL = "BUILD_NUMBER"


def _build_from_ticker() -> int | None:
    """The COMMITTED build ticker from the source tree, or None.

    WHY THIS IS COMMITTED AND THE OLD ONE WAS NOT. The ticker used to live in
    an untracked ``.webmcp_build_seq`` at the repo root, incremented only by
    ``deploy_aidocs_gate.sh``. That makes the build number a property of THE
    DEPLOY MACHINE: a client who installs a release runs no deploy script, so
    their artefact could never name its build. Committing the ticker makes it a
    build INPUT that any build — ours, CI's, or a client's — bakes into the
    stamp, which is what 'runtime-owned' means.

    Read only on a SOURCE CHECKOUT. On an installed artefact ``parents[2]`` is
    not a checkout, the read fails, and None comes back — correctly, because
    there the build stamp answers instead. Never raises, never fabricates: an
    unreadable or non-positive ticker is None, never 0.
    """
    path = Path(__file__).resolve().parents[2] / BUILD_NUMBER_REL
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def _version_from_release_manifest() -> str:
    """Read version from the signed release manifest shipped with the
    deployed package. This is the authoritative version source on the
    server, where pyproject.toml does not ship (only aidocs_mcp/ does).
    Until 2026-05-27 the footer on the live login page rendered '0.0.0'
    because pyproject was missing and the fallback below didn't know
    about the manifest — the manifest is the canonical reply now.
    """
    import json

    manifest = Path(__file__).resolve().parent / "trust" / "release_manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version")
        return str(v) if v else "0.0.0"
    except Exception:
        return "0.0.0"


def release_build_info() -> dict:
    """The deployed build's identity, read directly from the signed release manifest (pure display
    accessor, no crypto verification): build_number (monotonic, incremented per --deploy), commit,
    version, created_at, builder. A missing manifest / field degrades to safe defaults. Powers the
    gate's serverInfo.version stamp + the ai_version tool so the operator can see, at a glance,
    exactly which build is live on webmcp."""
    import json

    manifest = Path(__file__).resolve().parent / "trust" / "release_manifest.json"
    info = {"build_number": 0, "commit": "", "version": "", "created_at": "", "builder": "", "fingerprint": ""}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return info
    if not isinstance(data, dict):
        return info
    bn = data.get("build_number")
    try:
        info["build_number"] = int(bn) if bn is not None else 0
    except (TypeError, ValueError):
        info["build_number"] = 0
    info["commit"] = str(data.get("commit") or "")
    info["version"] = str(data.get("version") or "")
    info["created_at"] = str(data.get("created_at") or "")
    info["builder"] = str(data.get("builder") or "")
    info["fingerprint"] = str(data.get("fingerprint") or "")
    return info


def _git_head_commit(repo_root: Path) -> str:
    """Best-effort git HEAD sha by reading .git files directly (no subprocess in
    the import path). Handles loose refs, packed-refs, and detached HEAD; empty
    string on a deployed package with no .git."""
    try:
        git_dir = repo_root / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:40]  # detached HEAD holds the sha directly
        ref = head.split(":", 1)[1].strip()
        loose = git_dir / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()[:40]
        packed = git_dir / "packed-refs"  # packed-refs fallback
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith(("#", "^")) and line.endswith(ref):
                    return line.split(" ", 1)[0].strip()[:40]
    except Exception:
        pass
    return ""


# NOT SOURCE. Excluded from the drift answer — and the exclusion list is itself
# a place noise can creep back in, so keep it PRINCIPLED and short:
#   mcp/.deploy-reports — the crown gate GENERATES these during a run and commits
#     them AFTER promoting (doctrine XVI), so a deploy structurally ends with
#     local HEAD one commit ahead of the commit it just shipped. The deploy's own
#     bookkeeping can never be "unshipped code".
#   .MEMORY — memory/data, not code: backlog rows, session journals, sync events.
#     Every ai_backlog write mints an event file, so counting these would make
#     source_in_sync permanently false — which would recreate the exact defect
#     this function exists to kill (a signal that always says "different" cannot
#     be read, and real drift hides inside the permanent noise).
# The question this answers is narrow ON PURPOSE: "is the CODE I am running the
# CODE that is deployed?"
_NON_SOURCE_PATHS = ("mcp/.deploy-reports", ".MEMORY")


def _source_drift(repo_root: Path | None, deployed_commit: str) -> dict:
    """Does the LOCAL SOURCE differ from what is DEPLOYED? (Not: do the SHAs.)

    THE BUG THIS FIXES (operator, 2026-07-13: "why are local and deploy commit
    sha different? this is a bug no?"). Comparing raw SHAs is USELESS here,
    because the act of deploying CREATES a commit: the gate promotes commit X,
    then commits its own reports + session journal as X+1 (doctrine XVI —
    generated reports are committed before success is declared). So local !=
    deploy after EVERY successful deploy, forever, by construction.

    A signal that ALWAYS says "different" cannot be read — and real drift
    (genuinely unshipped source) becomes invisible inside the permanent noise.
    That is the same disease as an alarm that never stops: you stop hearing it.
    The operator hit exactly this — two SHAs, no way to tell which case it was.

    So answer the question he ACTUALLY has — "is the code I am running the code
    that is deployed?" — by diffing SOURCE, excluding the deploy's own
    bookkeeping, and counting UNCOMMITTED work too (an uncommitted edit is
    unshipped by definition, and is the case that actually bites).

    Fail-quiet, never fail-green: no git / no deployed commit / a broken compare
    -> in_sync is None ("cannot tell"), NEVER a fabricated True. Unknown is not
    a pass.
    """
    # #627: `unshipped` starts as None, NOT []. An empty list reads as "nothing
    # is unshipped" — a clean bill of health — while on every cannot-compare
    # path below it actually means "I never looked". That is the same lie as
    # commit:"", and it is exactly the shape a real divergence hides in. A list
    # appears ONLY once a comparison has actually run.
    out: dict = {"in_sync": None, "unshipped": None, "note": ""}
    if repo_root is None or not deployed_commit or not (repo_root / ".git").exists():
        out["note"] = "no git checkout or no deployed commit on record — cannot compare"
        return out
    try:
        # Reuse the ONE audited git helper (Article XXII): it is already
        # fingerprinted for the spawn-surface seal and windowless on win32.
        # A second git callsite here would just be a new untracked tunnel.
        from .git_helpers import run_git_sync

        ex = [f":(exclude){p}" for p in _NON_SOURCE_PATHS]
        root = str(repo_root)
        committed_out = run_git_sync(
            root, "diff", "--name-only", f"{deployed_commit}..HEAD", "--", ".", *ex, timeout=20
        )
        uncommitted_out = run_git_sync(
            root, "status", "--porcelain", "--", ".", *ex, timeout=20
        )
    except Exception as exc:  # noqa: BLE001 — cannot verify == say so; never assume clean
        out["note"] = f"could not compare source with the deployed commit: {type(exc).__name__}"
        return out

    shipped_delta = [f for f in committed_out.splitlines() if f.strip()]
    # porcelain v1 is exactly TWO status chars then whitespace then the path —
    # slice at 2 and strip, never 3: a staged entry ("M  path") and an unstaged
    # one (" M path") pad differently, and a fixed 3-slice silently eats the
    # first character of the filename in one of them (observed: 'cp/server/...').
    dirty = [ln[2:].strip() for ln in uncommitted_out.splitlines() if ln.strip()]
    unshipped = sorted({*shipped_delta, *dirty})
    out["unshipped"] = unshipped[:20]
    out["in_sync"] = not unshipped
    if not unshipped:
        out["note"] = (
            "IN SYNC — the running source is exactly what is deployed. Local HEAD is "
            "ahead only by the deploy's OWN bookkeeping commit (reports + session "
            "journal), which every successful deploy creates by construction."
        )
    else:
        out["note"] = (
            f"UNSHIPPED SOURCE — {len(unshipped)} file(s) differ from the deployed "
            f"commit (committed-but-not-deployed, or uncommitted). This is REAL "
            f"drift, not deploy bookkeeping."
        )
    return out


def local_build_info() -> dict:
    """The build ACTUALLY RUNNING on this process — pyproject version + live git
    HEAD from the source checkout. On a dev box (editable install) the signed
    release manifest lags real code (only `--deploy` rewrites it) while the
    marker-poll watchdog has already reloaded newer code, so this reports the
    live truth. builder='live@source' + build_number 0 flag an unsealed run.

    Carries `deployed_commit` + `source_in_sync` so the SHA difference from
    mode=deploy is READABLE instead of alarming: see _source_drift."""
    # #627: was Path(__file__).resolve().parents[3] — the same fixed-depth
    # arithmetic as the .deploy-reports defect, one directory further up. From
    # the installed runtime it names a directory that is not a checkout, so the
    # git reads below silently produced "" and null. Resolve by marker; consume
    # the None.
    repo_root = _source_checkout_root()
    deployed = deploy_build_info().get("commit") or ""
    drift = _source_drift(repo_root, deployed)
    fresh = _runtime_freshness_verdict()
    # #738: `commit` USED TO BE A LIVE GIT HEAD READ while this surface claimed to
    # describe "the build ACTUALLY RUNNING on this process". Measured 2026-08-01:
    # the daemon started 21:27, two commits landed after, and this reported the
    # NEWEST commit — one the running process had never imported. Python caches
    # modules at import; DISK IS NOT MEMORY. It was wrong in the reassuring
    # direction, so it would confirm "yes, that fix is live" for a fix that was
    # not running — which is how two days were lost to #726/#733.
    # build_stamp.py already documented this exact gap and called it uncoverable:
    # "the RUNNING process is executing those bytes. NOT COVERED, AND IT CANNOT
    # BE ... the filesystem cannot see what a long-lived process loaded an hour
    # ago." True of a filesystem read taken LATER — but not of the process asking
    # ITSELF. process_stamp captures identity AT BOOT, in memory, which is the one
    # vantage point that can answer it.
    # Three questions, three fields, never conflated again:
    #   commit      — what THIS PROCESS loaded
    #   source_head — what is on disk now
    #   deployed_commit — what shipped
    from .process_stamp import running_identity

    _head = _git_head_commit(repo_root) if repo_root is not None else ""
    running = running_identity(source_head=_head)
    return {
        "mode": "local",
        "build": running["build"],
        "commit": running["commit"],
        "version": _version_from_pyproject(),
        "created_at": running["created_at"],
        "source_head": _head,
        "running_verdict": running["running_verdict"],
        "running_note": running["running_note"],
        "running_origin": running["running_origin"],
        "running_since": running["running_since"],
        # #627: this WAS the unconditional literal "live@source". Measured on the
        # INSTALLED copy it came back as builder="live@source" WITH
        # source_root="…/runtime/venv/Lib" — a payload whose label contradicts its
        # own source_root. A label that cannot be false is not a signal, so derive
        # it: only a real source tree may claim it came from source.
        "builder": "live@source" if _has_source_checkout() else "UNVERIFIED@installed",
        "source_root": str(Path(__file__).resolve().parents[2]),
        "deployed_commit": deployed,
        "source_in_sync": drift["in_sync"],
        "sync_note": drift["note"],
        "unshipped": drift["unshipped"],
        # #627 — the axis this surface exists for. source_in_sync answers
        # "checkout vs deployed commit" (git); these answer "is the code being
        # SERVED the code that was deployed", which is the question a silent
        # divergence actually hides in. See _runtime_freshness_verdict.
        "runtime_fresh": fresh["fresh"],
        "runtime_note": fresh["note"],
        # #833 — THE THIRD LAYER. runtime_fresh answers "is the DAEMON serving
        # the deployed code". It cannot answer "is the SHIM relaying to it
        # running that code too", because the shim is spawned by the host and
        # outlives every remedy AIDOCS can perform. Measured 2026-08-19: a fix
        # was committed, deployed, installed, verified present in the artefact,
        # and the watchdog restarted -- and the corruption continued for another
        # hour through a two-hour-old shim, with every field above reporting
        # green. Three-valued like the rest: None is "cannot tell", never a pass.
        **_transport_freshness_fields(),
    }


def _transport_freshness_fields() -> dict:
    """#833. The last transport verdict, or an honest unknown.

    Daemon-local by construction: the verdict is recorded per request by
    mcp_server, so a process that has served no shim request has nothing to
    report and says so rather than claiming freshness it never observed.
    """
    try:
        from .stdio_shim import last_transport_verdict

        verdict = last_transport_verdict()
        return {
            "transport_fresh": verdict.get("fresh"),
            "transport_note": verdict.get("reason") or "",
        }
    except Exception:  # noqa: BLE001 — a provenance reader never raises
        return {"transport_fresh": None, "transport_note": "unreadable"}


def _source_checkout_root() -> Path | None:
    """The repo root of the SOURCE CHECKOUT, by MARKER — never by counting.

    Same cure, same reason as ``_deploy_reports_dir`` (see its docstring for the
    full disease). ``local_source_root()`` returns the ``mcp/`` dir, so the repo
    root is its parent; a tree that is not a checkout yields None rather than a
    confidently-wrong path whose reads come back empty.
    """
    try:
        from .runtime_provisioner import local_source_root

        root = local_source_root()
    except Exception:  # noqa: BLE001 — a provenance reader never raises
        return None
    return root.parent if root is not None else None


def _deploy_reports_dir() -> Path | None:
    """Where the deploy WROTE its seal — found by MARKER, never by counting.

    THE DISEASE (#627). This was ``Path(__file__).resolve().parents[2] /
    ".deploy-reports"``. A fixed parent index encodes ONE install layout as if
    it were a universal truth: it lands on ``<repo>/mcp`` from the checkout and
    on ``<venv>/Lib`` from the installed runtime, where nothing of the sort
    exists. The reader then found nothing and returned ``""`` — so an
    UNANSWERABLE question and a NEGATIVE answer were the same bytes, and the one
    instrument built to detect a silent divergence failed silently itself.

    WHY NOT SPECIAL-CASE THE VENV. ``if running_from_venv: look over there``
    would move the failure to the next root layout (a wheel install, a
    container, a relocated venv) rather than remove it. Any fix that has to
    enumerate install layouts is a symptom patch, because the defect is the
    ARITHMETIC, not the particular number.

    THE CURE. The read path does no arithmetic at all. It asks the one
    marker-gated resolver — ``local_source_root()``, which SEARCHES for
    ``<root>/pyproject.toml`` + ``<root>/server/aidocs_mcp/`` — and consumes its
    ``None``. There is no depth constant left to be wrong about, so no layout
    can break it: a tree that is not a checkout resolves to None, and None is a
    LOUD refusal handled by the honest-verdict backstop below, never an empty
    string dressed as data.
    """
    try:
        from .runtime_provisioner import local_source_root

        root = local_source_root()
    except Exception:  # noqa: BLE001 — a provenance reader never raises
        return None
    if root is None:
        return None
    reports = root / ".deploy-reports"
    return reports if reports.is_dir() else None


def _runtime_freshness_verdict() -> dict:
    """THE THIRD DRIFT AXIS, consumed (#569 comparator, #627 surface).

    ``source_in_sync`` compares the CHECKOUT against the DEPLOY MARKER — a git
    question. It cannot see the divergence that actually bites: the daemon
    serves ``~/.aidocs/runtime/venv``, so code can be committed, promoted and
    "deployed" while the process answering tool calls still runs the old bytes.
    Measured 2026-07-30: HEAD carried the #627 cure and the live ai_version
    returned the pre-cure payload — every git-based signal said IN SYNC and was
    right. The comparator for that axis already existed and nothing consumed it.

    Never raises, never fabricates: True/False/None with a reason attached to
    each, because 'cannot tell' must not read as 'fine'.
    """
    try:
        from .runtime_provisioner import runtime_freshness

        r = runtime_freshness()
        fresh = r.get("fresh")
        note = str(r.get("note") or "")
    except Exception as exc:  # noqa: BLE001 — cannot verify == say so
        return {
            "fresh": None,
            "note": (
                "could not compare the installed enforcement runtime with source "
                f"({type(exc).__name__}) — this process cannot tell whether the "
                "code it serves is the code that was deployed"
            ),
        }
    if fresh is True and not note:
        note = "the installed enforcement runtime matches source"
    if fresh is None and not note:
        note = "installed-vs-source freshness is unresolved — cannot tell"
    return {"fresh": fresh, "note": note}


def _release_manifest_absence_reason() -> str:
    """Why ``release`` mode has nothing to report — always a NAMED reason.

    The manifest rides INSIDE the package (``aidocs_mcp/trust/``), which is the
    correct shape: provenance that travels with the artefact needs no arithmetic
    and is right in every layout by construction. What was missing is what
    happens when it is absent — ``release_build_info()`` degraded to zeros and
    empty strings with no reason at all, and on a deployed surface the signed
    manifest is the ONLY axis that can answer at all (there is no checkout and
    no deploy seal beside an installed release). So the one source a served
    gate leans on was the one that could not say it knew nothing.

    2026-08-21: modes are gone; ``build_info()`` reports all three axes and
    marks an unestablished one ``known: false`` with this reason attached.
    """
    manifest = Path(__file__).resolve().parent / "trust" / "release_manifest.json"
    try:
        present = manifest.is_file()
    except OSError as exc:
        return f"the signed release manifest is unreadable ({type(exc).__name__})"
    if not present:
        return (
            "no signed release manifest inside this artefact "
            "(aidocs_mcp/trust/release_manifest.json absent) — this build cannot "
            "name the commit, version or build_number it is running"
        )
    return (
        "the signed release manifest is present but names no commit — it is "
        "malformed or was written without provenance"
    )


def deploy_build_info() -> dict:
    """WHAT THE LAST `--deploy` SEALED — plus every source that could dispute it.

    THE ANSWERING ORDER IS THE CONTRACT (fixed #745, 2026-08-18):
      1. the DEPLOY SEAL under mcp/.deploy-reports/ — crown.reports-head is the
         truth-sealed tested commit and status.json carries the tally. This is
         the artefact the mode is named for, so where it exists it answers.
      2. the IN-ARTEFACT BUILD STAMP — only on a surface with no seal beside it
         (a wheel install ships neither .deploy-reports nor .git), where it is
         the strongest thing that travelled with the bytes.
      3. the SIGNED RELEASE — the served gate re-verifies the live code against
         the manifest it shipped with (release_trust.verify_release), which is
         what makes `running_verified` the load-bearing "is the deployed code
         the sealed code?" signal.

    IT USED TO ASK THE STAMP FIRST AND RETURN. Measured 2026-08-18: the stamp
    named 01a477e2ab34 while crown.reports-head named 676a5092e55a, and this
    surface reported the stamp, with tests="" and status "verified: …". The
    stamp was honest about ITSELF; the ROUTING answered a different question
    under the label of the question asked, and did it in the reassuring
    direction to the one caller who already suspected staleness.

    NOTHING IS PICKED SILENTLY ANYMORE. `seal_commit` and `stamp_commit` are
    both reported, `provenance_source` names who answered, and when the two
    disagree `provenance_conflict` is True and `status` says DISPUTED with both
    SHAs in it. Corroboration is stated only when a VERIFIED stamp independently
    names the SAME commit as the seal. `tests_stale` flags #742's stale-marker
    trap — that directory keeps green artefacts across runs, so a tally older
    than the head beside it describes an earlier run.
    """
    import json

    reports = _deploy_reports_dir()
    info = {
        "mode": "deploy",
        "commit": "",
        "tests": "",
        "status": "",
        "fingerprint": "",
        "running_verified": None,
        # #627 phase 3 — WHICH truth-source answered. Three surfaces can answer
        # this question and they are not equally strong; a payload that does not
        # say which one spoke forces the next reader to guess, which is how a
        # blank came to read as "fine" in the first place.
        "provenance_source": "none",
        # THE READER ASKS FOR THESE AND THIS PRODUCER NEVER EMITTED THEM
        # (2026-08-30). `_deployed_axis` reads `version`, `build_number` and
        # `created_at`; none of the three existed in this dict, so the axis
        # reported version="" and build=UNVERIFIED on every surface — including
        # the gate, where the answering stamp CARRIES all three. Measured on the
        # live gate: deployed{source:"build-stamp", version:"", build:"UNVERIFIED"}
        # beside running{origin:"artefact-build-stamp", version:"2.5.1", build:217},
        # both read from the SAME stamp. Declared here so the contract between
        # the two functions is visible in one place rather than inferred.
        "version": "",
        "build_number": None,
        "created_at": "",
        "stamp_verdict": "UNVERIFIED",
        # #745 — TWO SOURCES CAN ANSWER, AND THEY CAN DISAGREE. Both are now
        # reported side by side under their own names, so "which one answered"
        # and "did the other one agree" are readable facts rather than an
        # accident of evaluation order.
        "seal_commit": "",
        "stamp_commit": "",
        "provenance_conflict": False,
        "tests_stale": False,
    }
    # ── THE IN-ARTEFACT STAMP IS READ, NEVER RETURNED FROM ────────────────────
    # #627 phase 3 put the stamp first because it is the only source that travels
    # WITH the bytes. That was right for the wheel-install posture and WRONG as a
    # dispatch order: it RETURNED before crown.reports-head was ever opened, so
    # the mode named "deploy" answered with "what were these bytes built from" —
    # a different question — under the label of the question asked (#745).
    # Reading it here and deciding below costs nothing and keeps both answers.
    #
    # SCOPE OF WHAT A STAMP PROVES: the bytes ON DISK are the bytes that were
    # built (SHIPPED + TRUSTED). It is NOT a signature, and it says nothing about
    # whether the RUNNING process loaded them (LOADED) — a daemon caches modules
    # at import. running_verified therefore stays None on a VERIFIED stamp: that
    # field is reserved for the signature check, and 'unsigned self-consistency'
    # must not be promoted into 'verified against the root of trust'.
    try:
        from .build_stamp import build_stamp_verdict

        _st = build_stamp_verdict()
    except Exception as exc:  # noqa: BLE001 — a provenance reader never raises
        _st = {
            "verdict": "UNVERIFIED",
            "reason": f"build stamp unreadable ({type(exc).__name__})",
            "commit": "",
            "expected_fingerprint": "",
        }
    info["stamp_verdict"] = str(_st.get("verdict") or "UNVERIFIED")
    info["stamp_commit"] = str(_st.get("commit") or "")[:40]
    # Held aside rather than assigned: they belong to the STAMP, so they are
    # published only on the branches where the stamp is what answered. Copying
    # them onto a SEAL answer would present one source's numbers as another's —
    # the #745 mistake this function was rewritten to stop making.
    _stamp_version = str(_st.get("version") or "")
    _stamp_build = _st.get("build")
    _stamp_built_at = str(_st.get("built_at") or "")
    _stamp_fp = str(_st.get("expected_fingerprint") or "")
    _stamp_reason = str(_st.get("reason") or "")
    if info["stamp_verdict"] == "MISMATCH":
        # A POSITIVE DISPROOF, not a shrug. The bytes were measured and they are
        # not the bytes the stamp was written over — exactly the state that
        # silently disabled a gate for ~110 tool calls. This one DOES harden into
        # running_verified=False, because something was established, and it is
        # true about this artefact whatever the deploy seal beside it says.
        info["running_verified"] = False

    # ── 1. THE DEPLOY SEAL — the artefact this mode is NAMED FOR ──────────────
    # mode='deploy' asks "what did the last --deploy SEAL?". That is
    # .deploy-reports/crown.reports-head plus the tally in status.json, and
    # nothing else answers it. On the served gate the directory does not exist
    # (it never ships inside the package) and the stamp/manifest below answer
    # instead — but where the seal EXISTS it is the answer to the question asked.
    _seal_at = None
    _tally_at = None
    if reports is not None:
        try:
            _crown = reports / "crown.reports-head"
            info["seal_commit"] = _crown.read_text(encoding="utf-8").strip()[:40]
            _seal_at = _crown.stat().st_mtime
        except Exception:
            pass
        try:
            _status_path = reports / "status.json"
            st = json.loads(_status_path.read_text(encoding="utf-8"))
            info["tests"] = str(st.get("message") or "")
            _tally_at = _status_path.stat().st_mtime
        except Exception:
            pass

    if info["seal_commit"]:
        info["commit"] = info["seal_commit"]
        info["provenance_source"] = "deploy-seal"
        # #742's STALE-MARKER TRAP: .deploy-reports keeps green artefacts across
        # runs, so status.json read {"message":"17631 passed"} after a FAILED
        # deploy AND after a later successful one with a different tally.
        # Presence is not authority. A tally written BEFORE the head it sits
        # beside describes a different run and must not be quoted as this one's.
        if _seal_at is not None and _tally_at is not None and _tally_at < _seal_at - 1:
            info["tests_stale"] = True
        _seal_line = f"deploy seal: crown.reports-head names {info['seal_commit'][:12]}"
        _seal_line += (
            f"; status.json tally {info['tests']!r}"
            if info["tests"]
            else "; no readable test tally beside it"
        )
        if info["stamp_commit"] and info["stamp_commit"] != info["seal_commit"]:
            # THE #745 CASE, SAID OUT LOUD. Measured 2026-08-18: the stamp named
            # 01a477e2a (168 commits back) while the seal named 676a5092e, and
            # the tool answered with the stamp and called it verified. Two
            # sources disagreeing IS the finding; picking one silently is how a
            # reader concluded "REAL drift, not deploy bookkeeping" — backwards.
            info["provenance_conflict"] = True
            info["status"] = (
                f"DISPUTED: {_seal_line}, but this artefact's in-artefact build "
                f"stamp names {info['stamp_commit'][:12]} (stamp verdict "
                f"{info['stamp_verdict']}). They answer DIFFERENT questions — the "
                "seal says what the deploy sealed, the stamp says what these bytes "
                "were built from — and nothing here verified either against the "
                "other. Treat neither as confirmed."
            )
        elif info["stamp_commit"] and info["stamp_verdict"] == "VERIFIED":
            info["status"] = (
                f"CORROBORATED: {_seal_line}, and the in-artefact build stamp "
                f"independently names the same commit ({_stamp_reason or 'build stamp'})."
            )
        else:
            info["status"] = (
                f"{_seal_line}. NOT CORROBORATED: the in-artefact build stamp could "
                f"not confirm it ({info['stamp_verdict']}"
                + (f": {_stamp_reason}" if _stamp_reason else "")
                + ")."
            )
        if info["tests_stale"]:
            info["status"] += (
                " TALLY MAY BE STALE: status.json is OLDER than crown.reports-head, "
                "so the test numbers above describe an EARLIER run than the sealed "
                "commit (#742 — that directory keeps green artefacts across runs)."
            )
        # `fingerprint` stays empty on a seal answer: a seal carries no digest,
        # and borrowing the stamp's would present one source's measurement as
        # the other's. The stamp's own values are in stamp_commit/stamp_verdict.
        return info

    # ── 2. NO SEAL BESIDE THIS ARTEFACT — the wheel-install posture #627 built
    # the stamp for. Here the stamp IS the strongest thing that travelled with
    # the bytes, so it answers.
    if info["stamp_verdict"] == "VERIFIED":
        info["commit"] = info["stamp_commit"]
        info["fingerprint"] = _stamp_fp
        info["provenance_source"] = "build-stamp"
        info["status"] = f"verified: {_stamp_reason or 'in-artefact build stamp'}"
        info["tests"] = ""
        # The stamp answered, so its version/build/built_at travel with it.
        info["version"] = _stamp_version
        info["build_number"] = _stamp_build
        info["created_at"] = _stamp_built_at
        return info
    if info["stamp_verdict"] == "MISMATCH":
        info["commit"] = info["stamp_commit"]
        info["fingerprint"] = _stamp_fp
        info["provenance_source"] = "build-stamp"
        info["status"] = _stamp_reason or "MISMATCH"
        info["tests"] = ""
        # Reported on a MISMATCH too: they describe the same stamp the commit
        # came from, and withholding them here would make a tampered artefact
        # look LESS legible than a clean one at exactly the moment a reader
        # needs every field it can get.
        info["version"] = _stamp_version
        info["build_number"] = _stamp_build
        info["created_at"] = _stamp_built_at
        return info
    # Served-surface fallback: ONLY when .deploy-reports gave nothing (i.e. the
    # deployed package, which never ships that dir) — never on a dev checkout,
    # where the seal above is the truth and a lingering local manifest would make
    # verify_release compare against stale bytes. There the gate runs the SIGNED
    # release, so recompute the running fingerprint and verify it against the
    # shipped manifest: `running_verified` answers "is the live code the sealed
    # code?", and commit/fingerprint let you compare against mode=local/release.
    if not info["commit"]:
        try:
            from .release_trust import verify_release

            rt = verify_release()
            reason = str(getattr(rt, "reason", "") or "")
            if not reason.startswith("unsigned"):
                info["commit"] = str(getattr(rt, "commit", "") or "")[:40]
                info["fingerprint"] = str(getattr(rt, "fingerprint", "") or "")
                info["running_verified"] = bool(getattr(rt, "ok", False))
                info["status"] = "verified" if rt.ok else f"UNVERIFIED: {reason}"
                info["tests"] = str(reason)
                if info["commit"]:
                    info["provenance_source"] = "release-manifest"
            else:
                # #627 — THE SHARPEST LINE IN THE DISEASE. This branch used to be
                # silent. "unsigned: release trust artifacts absent" is EXACTLY
                # what an installed copy returns, because the wheel does not ship
                # aidocs_mcp/trust/. So the one case that most needs a loud answer
                # was the one case that fell through to an all-blank payload, and
                # a CALLER COULD NOT TELL "no seal here" FROM "nothing is wrong".
                #
                # Measured on this box, the installed artefact answered every mode
                # with ""/null while mcp/.deploy-reports/crown.reports-head named a
                # real commit — the marker was never empty; the READER was blind.
                #
                # Absence is now reported AS absence. Nothing is fabricated: no
                # commit is invented, running_verified stays None (not False —
                # "cannot tell" is not "tampered"), and the reason is the seal's
                # own words.
                info["status"] = f"UNVERIFIED: {reason}"
                info["tests"] = reason
        except Exception as exc:  # noqa: BLE001 — the reason is the payload
            # #627: a raising verifier must not read as a clean bill of health
            # either. Same law: unanswerable is a verdict, not a blank.
            info["status"] = f"UNVERIFIED: release trust check errored ({type(exc).__name__})"
    # #627 FINAL BACKSTOP — the honest-verdict invariant for this surface: if no
    # commit could be established, SOMETHING must say why. An all-blank payload
    # is the bug the operator hit; it let "cannot tell" pass for "fine".
    if not info["commit"] and not info["status"]:
        info["status"] = (
            "UNVERIFIED: no in-artefact build stamp, no deploy seal beside this "
            "artefact and no shipped release manifest — this process cannot name "
            "the commit it is running"
        )
    return info


def _has_source_checkout() -> bool:
    """True only when a REAL source tree is present (pyproject readable AND a
    resolvable git HEAD). The DEPLOYED package ships aidocs_mcp/ ONLY — no
    pyproject.toml, no .git — so mode=local there would report version 0.0.0 /
    commit '' (garbage). This is the guard that keeps the default honest on
    both surfaces."""
    root = _source_checkout_root()  # #627: was parents[3] — resolve by marker
    return (
        root is not None
        and _version_from_pyproject() != "0.0.0"
        and bool(_git_head_commit(root))
    )


#: What an axis reports for a field it cannot establish. A STRING, never 0 or
#: "" — a zero renders as data and reads as an answer, which is the disease
#: #627 exists to end.
_UNVERIFIED = "UNVERIFIED"


def _running_axis() -> dict:
    """What is THIS PROCESS running?

    The only axis that can be answered from MEMORY rather than from disk, and
    therefore the only one that survives the question "is the fix actually
    live?". See process_stamp: the identity is frozen at boot, because a
    long-lived process runs what it IMPORTED and disk is not memory (#738).
    """
    from .process_stamp import VERIFIED, process_stamp

    stamp = process_stamp()
    known = stamp.get("verdict") == VERIFIED and bool(stamp.get("commit"))
    build = stamp.get("build")
    out = {
        "known": known,
        "version": str(stamp.get("version") or ""),
        "build": build if isinstance(build, int) else _UNVERIFIED,
        "commit": str(stamp.get("commit") or ""),
        "at": str(stamp.get("captured_at") or ""),
        "origin": str(stamp.get("origin") or ""),
        "pid": stamp.get("pid"),
    }
    # THE DIAGNOSTICS THE DELETED MODE USED TO CARRY. Collapsing the modes must
    # not silently drop three instruments that each cost this project a session:
    #   #627  source_in_sync / unshipped  — is the checkout what was deployed?
    #   #738  running_verdict             — did THIS process load those bytes?
    #   #833  transport_fresh             — is the SHIM relaying to fresh code?
    # They are properties OF WHAT IS RUNNING, so this is their home now — and
    # consuming local_build_info() here is why it is not dead code. Measured
    # 2026-08-21: the first deploy of this redesign failed Gate 1d with
    # "unused function 'local_build_info'", which was the honest report of a
    # REGRESSION (the modes were its only production caller), not a lint nit.
    live = local_build_info()
    for key in (
        "source_head",
        "builder",
        "source_root",
        "deployed_commit",
        "source_in_sync",
        "sync_note",
        "unshipped",
        "runtime_fresh",
        "runtime_note",
        "running_verdict",
        "running_note",
        "transport_fresh",
        "transport_note",
    ):
        if key in live:
            out[key] = live[key]
    if not known:
        out["why"] = str(
            stamp.get("reason")
            or "no process stamp was taken at startup, so what this process "
            "loaded is unknown"
        )
    return out


def _deployed_axis() -> dict:
    """What did the last deploy SEAL?

    ABSENT ON A CLIENT INSTALL, and that is a FACT ABOUT THE SURFACE rather
    than a caller error: nobody runs a deploy script on the machine where a
    release is installed. It reports known=False with the reason, never a
    refusal — the caller asked a legitimate question and gets a true answer.
    """
    try:
        info = deploy_build_info()
    except Exception as exc:  # noqa: BLE001 — a provenance reader never raises
        return {
            "known": False,
            "version": "",
            "build": _UNVERIFIED,
            "commit": "",
            "at": "",
            "why": f"the deploy seal could not be read ({type(exc).__name__})",
        }
    commit = str(info.get("commit") or "")
    build = info.get("build_number")
    out = {
        "known": bool(commit),
        "version": str(info.get("version") or ""),
        "build": build if isinstance(build, int) and build > 0 else _UNVERIFIED,
        "commit": commit,
        "at": str(info.get("created_at") or ""),
        # WHICH truth-source answered (seal / build-stamp / disputed). Without
        # it the next reader is guessing again.
        "source": str(info.get("provenance_source") or ""),
    }
    if not out["known"]:
        out["why"] = str(
            info.get("status")
            or "no deploy seal beside this artefact — nothing was deployed from "
            "here, which is the normal state of an installed release"
        )
    return out


def _released_axis() -> dict:
    """The last BLESSED build, as THIS artefact's signed manifest records it.

    SERVER-SIDE READER (2026-08-22). On the gate this is the authority's own
    answer and is what ``GET /v1/version`` serves. On a client it is only what
    the local copy shipped with — which goes stale the moment the next release
    lands — so the client's ``released`` axis no longer comes from here; see
    ``build_info``.
    """
    info = release_build_info()
    commit = str(info.get("commit") or "")
    build = info.get("build_number")
    out = {
        "known": bool(commit),
        "version": str(info.get("version") or ""),
        "build": build if isinstance(build, int) and build > 0 else _UNVERIFIED,
        "commit": commit,
        "at": str(info.get("created_at") or ""),
        "builder": str(info.get("builder") or ""),
    }
    if not out["known"]:
        out["why"] = _release_manifest_absence_reason()
    return out


# ── THE AUTHORITY HALF (operator directive 2026-08-22) ──────────────────────
#
# "deploy and release build numbers should come from the SERVER. the local
# version comes from code, the deployed and released versions come from the
# SERVER (via web requests or something)."
#
# ``authority_build_info`` is the SERVER composition — local artefact reads
# only — and is what the gate serves at GET /v1/version. ``build_info`` is the
# CLIENT composition: ``running`` from THIS artefact, ``deployed`` +
# ``released`` FETCHED from the authority. build_authority.py carries the
# reasoning and the governed fetch; the short form is that a runtime can only
# ever know what it was BUILT from, while what is deployed and released are
# facts about the gate — so the gate is asked.

#: Injection seam (tests). None = build_authority.fetch_authority_axes.
_AUTHORITY_FETCH = None


def authority_build_info() -> dict:
    """What THIS process can attest to, as the build authority — LOCAL READS ONLY.

    Served by the gate at ``GET /v1/version`` and returned by the gate's own
    ai_version tool. It MUST NOT fetch: a gate that asked itself over HTTPS
    through its own edge would hang the moment the transport is busy, and the
    two halves would be a loop instead of a contract.

        running   what this process loaded (memory, frozen at boot)
        deployed  what the disk beside it says was deployed (seal, else stamp)
        released  the signed manifest inside this artefact
    """
    from .build_authority import AUTHORITY_SCHEMA

    return {
        "schema": AUTHORITY_SCHEMA,
        "running": _running_axis(),
        "deployed": _deployed_axis(),
        "released": _released_axis(),
    }


def _validate_authority_payload(payload: object) -> str:
    """'' when ``payload`` is the build-axes contract, else WHY it is not.

    The fetch validates too; this re-checks at the seam so an injected or
    future channel cannot hand the composer a shape it then reads as blanks.
    """
    from .build_authority import AUTHORITY_SCHEMA

    if not isinstance(payload, dict):
        return f"payload is {type(payload).__name__}, not an object"
    schema = payload.get("schema")
    if schema != AUTHORITY_SCHEMA:
        return f"unexpected schema {schema!r} (wanted {AUTHORITY_SCHEMA!r})"
    for axis in ("running", "deployed", "released"):
        if not isinstance(payload.get(axis), dict):
            return f"axis {axis!r} missing"
    return ""


def _authority_payload() -> tuple[dict | None, str, str]:
    """Ask the authority ONCE. Returns ``(payload_or_None, url, why_if_none)``.

    Never raises. Disabled, unreachable and malformed are three DIFFERENT facts
    about the authority and each is named, so the dependent axes can say which
    one they are reporting rather than a blank that reads as "no data".
    """
    from . import build_authority as _ba

    try:
        url = _ba.authority_url()
    except Exception:  # noqa: BLE001 — a config read must not crash version reporting
        url = _ba.DEFAULT_AUTHORITY_URL
    if not url:
        return (
            None,
            "",
            (
                f"build authority fetch disabled ({_ba.ENV_AUTHORITY_URL}=off) — this "
                "runtime was told not to ask the server, so deployed/released are "
                "unknown here by choice, not by failure"
            ),
        )
    fetch = _AUTHORITY_FETCH or _ba.fetch_authority_axes
    try:
        payload = fetch(url)
    except _ba.AuthorityMalformed as exc:
        return None, url, f"build authority {url} answered outside the contract: {exc}"
    except Exception as exc:  # noqa: BLE001 — every other failure is ONE fact: unreachable
        return None, url, f"build authority {url} unreachable: {exc}"
    bad = _validate_authority_payload(payload)
    if bad:
        return None, url, f"build authority {url} answered outside the contract: {bad}"
    return payload, url, ""


def _authority_host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or url
    except Exception:  # noqa: BLE001
        return url


def _unknown_axis(source: str, why: str) -> dict:
    return {
        "known": False,
        "version": "",
        "build": _UNVERIFIED,
        "commit": "",
        "at": "",
        "source": source,
        "why": why,
    }


def _axis_build(raw: object):
    """The build as the wire carried it — an int ≥ 1 — else UNVERIFIED. ``bool``
    is excluded because ``isinstance(True, int)`` and ``build: true`` is not a
    build number (build_stamp._coerce_build's rule, kept identical)."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else _UNVERIFIED


def _deployed_from_authority(payload: dict | None, url: str, why: str) -> dict:
    """The CLIENT's ``deployed`` axis = what the SERVER PROCESS LOADED.

    MEMORY BEATS DISK. The gate's ``running`` axis is frozen at boot and says
    what is actually serving; its ``deployed`` axis is read from disk and says
    what was put there. When they disagree, the gate is serving older bytes
    than it was handed — the false-restart shape measured 2026-08-22 (pid
    16336 kept serving after the deploy printed "restarted") — and that is
    REPORTED (``provenance_conflict``, ``server_seal_commit``), never picked
    silently (#745). Disk is the fallback only when memory is unknown, and is
    labelled as the weaker answer by its ``source`` suffix.
    """
    host = _authority_host(url)
    if payload is None:
        return _unknown_axis(f"authority:{host}", why)
    running = payload.get("running") or {}
    disk = payload.get("deployed") or {}
    if running.get("known") and running.get("commit"):
        chosen, origin = running, "running"
    elif disk.get("known") and disk.get("commit"):
        chosen, origin = disk, "deployed"
    else:
        return _unknown_axis(
            f"authority:{host}",
            f"build authority {url} reached, but it could not name what it is "
            f"running (running: {running.get('why') or 'unknown'}; disk: "
            f"{disk.get('why') or 'unknown'})",
        )
    commit = str(chosen.get("commit") or "")
    seal_commit = str(disk.get("commit") or "")
    return {
        "known": True,
        "version": str(chosen.get("version") or ""),
        "build": _axis_build(chosen.get("build")),
        "commit": commit,
        "at": str(chosen.get("at") or ""),
        "source": f"authority:{host}:{origin}",
        "server_seal_commit": seal_commit,
        "provenance_conflict": bool(
            origin == "running" and seal_commit and seal_commit != commit
        ),
    }


def _released_from_authority(payload: dict | None, url: str, why: str) -> dict:
    """The CLIENT's ``released`` axis = the SERVER's signed release manifest."""
    host = _authority_host(url)
    if payload is None:
        return _unknown_axis(f"authority:{host}", why)
    rel = payload.get("released") or {}
    out = {
        "known": bool(rel.get("known") and rel.get("commit")),
        "version": str(rel.get("version") or ""),
        "build": _axis_build(rel.get("build")),
        "commit": str(rel.get("commit") or ""),
        "at": str(rel.get("at") or ""),
        "builder": str(rel.get("builder") or ""),
        "source": f"authority:{host}:release_manifest",
    }
    if not out["known"]:
        out["why"] = str(
            rel.get("why")
            or f"build authority {url} carries no signed release manifest it can name"
        )
    return out


def build_info() -> dict:
    """ONE QUESTION, ONE ANSWER — no modes, no refusals (operator, 2026-08-21).

    THE DEFECT THIS REPLACES. ai_version grew four truth-sources behind a
    ``mode`` parameter, each with its own refusal path. Asked plainly which
    version this window's runtime is, it answered
    ``{"refused": true, "requested_mode": "local"}`` — a denial, to the one
    person entitled to the answer, from the surface whose entire job is to say
    which code is running. The ruling was exact: "no fucking around, no 300000
    modes, no denies".

    THREE AXES, ALWAYS ALL THREE — AND WHERE EACH COMES FROM (2026-08-22):

        running   what THIS PROCESS loaded      (memory, frozen at boot; the
                                                 in-artefact build stamp)
        deployed  what the SERVER is running    (FETCHED: the authority's own
                                                 running axis — memory beats
                                                 disk, conflicts reported)
        released  the last blessed build        (FETCHED: the authority's
                                                 signed manifest)

    "deploy and release build numbers should come from the SERVER. the local
    version comes from code" (operator). A runtime can only ever know what it
    was BUILT from; what is deployed and released are facts about the gate,
    and reading them from a local seal or a local manifest copy was asking the
    wrong machine — and it went stale the moment the next release landed,
    which is exactly when ai_version must say "you are behind".

    They are DIFFERENT QUESTIONS, so they are separate objects and are never
    collapsed into one "version" — conflating them is how an operator reads a
    release answer believing it is local. Each server-sourced axis names its
    ``source`` ("authority:<host>:running" / ":deployed" / ":release_manifest").

    AN AXIS THAT CANNOT BE ESTABLISHED SAYS ``known: False`` WITH A REASON.
    That is not a refusal. A refusal means "I did not answer you"; ``known:
    False`` means "I answered: it is not knowable here" — and for the fetched
    axes the reason says WHICH kind of unknowable: the authority was DISABLED
    (``AIDOCS_BUILD_AUTHORITY_URL=off``, the air-gap posture), UNREACHABLE
    (named host, named error), or MALFORMED (named schema). ``authority``
    carries the URL asked and whether it answered.

    NUMBERING IS OPTION B. The semantic version stays THREE segments and the
    build ticker is a separate integer field. ``2.5.1.179`` would make every
    PEP 440 parser, version comparator and release URL wrong in order to pack
    two independent facts into one string.
    """
    payload, url, why = _authority_payload()
    authority: dict = {"url": url, "reachable": payload is not None}
    if why:
        authority["why"] = why
    return {
        "running": _running_axis(),
        "deployed": _deployed_from_authority(payload, url, why),
        "released": _released_from_authority(payload, url, why),
        "authority": authority,
    }


def get_version() -> str:
    # Dev-box source of truth: pyproject.toml (always current at build).
    version = _version_from_pyproject()
    if version != "0.0.0":
        return version
    # Server source of truth: the signed release manifest.
    version = _version_from_release_manifest()
    if version != "0.0.0":
        return version
    # Lazy: importlib.metadata costs ~100 ms to import and is only needed in this
    # rare fallback (neither pyproject nor manifest readable). Importing it at
    # module top made every claude_hook process spawn pay ~100 ms for nothing.
    # Deferred here keeps __version__ identical while removing that cost from
    # the hot hook path.
    try:
        from importlib import metadata

        return metadata.version("aidocs-mcp")
    except Exception:
        return "0.0.0"


def __getattr__(name: str):
    """PEP 562 lazy ``__version__`` (#342 thin-hook budget): computing the
    version means reading pyproject.toml / the release manifest / .git — file
    I/O every ``python -m aidocs_mcp.claude_hook`` spawn paid at package import
    even though the hook never looks at the version. ``from aidocs_mcp import
    __version__`` and ``aidocs_mcp.__version__`` still work — they just pay on
    first access instead of at import.
    """
    if name == "__version__":
        return get_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
