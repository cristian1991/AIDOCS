"""LOCAL ENFORCEMENT RUNTIME refresh: detect always, install only when asked (#569).

WHY THIS EXISTS (operator, 2026-07-28): "fix the deploy gate to refresh the runtime, that
was the whole idea to 'load the new code', that's why we even created the deamon." Then, on
seeing the first draft land in the deploy gate only: "maybe just have 1 process handle this,
same as deamon, because dev aidocs doesnt have the gate at all, but it should be able to
update and refresh its code" / "or yeah the watchdog refreshes".

MEASURED GAP. Nothing local ever reinstalled the enforcement runtime. deploy_aidocs_gate.sh
(4934 lines) has no local install step — its only package-integrity call runs `sudo -u app`
ON THE VPS against $VPS_GATE_DIR/gate-root. So a deploy shipped new code to production while
all 7 Claude Code hooks kept executing ~/.aidocs/runtime/venv/Scripts/pythonw.exe, whose
installed package was two days stale: the gate refused an agent dispatch TWICE under a policy
the operator had already repealed AND deployed (54f562a25). `aidocs setup` blessed that
runtime because all five of its checks compare interpreter IDENTITY and none compares package
CONTENT to source. "Deployed" and "enforced locally" are different claims, and only one of
them was true.

WHY IT LIVES IN THE PACKAGE AND NOT IN mcp/scripts/. The first draft was a script there, and
public_export_manifest.py excludes that directory WHOLESALE ("every script under mcp/scripts/
is private by default ... an explicit KEEP_AFTER_EXCLUDE entry is required to ship one"). A
refresher that cannot reach a public or dev install repairs exactly one machine — and the
crown gate is private too, so a dev checkout with no gate would have had no refresher at all.
Shipping it as a module also keeps ONE implementation for both callers (the gate and the
watchdog), instead of two that must agree forever.

WHY THE WATCHDOG DRIVES IT AND NOT THE DAEMON. The refresher must OUTLIVE the refreshed. A
process cannot cleanly restart itself, so the daemon is a refresh TARGET; the watchdog is the
only local process that is not one (see aidocs_service.run_watchdog, which already restarts
its child and survives its crashes).

THE TWO-AXIS TRAP — this module's real subject.
``runtime_provisioner.runtime_freshness()`` answers ONLY the DEV question, "does the installed
package match the SOURCE TREE?", because it resolves ``local_source_root()`` — which returns
None on a release install with no checkout. On such a machine it says fresh=None, "cannot
tell": correct per its fail-quiet contract, but NOT a defect signal. A refresher wired
straight to it would, on every public install, either never fire or fire forever. The RELEASE
axis is a different comparison (installed vs the PUBLISHED release) and its data already
exists in ``aidocs_service.read_update_state()`` (current/latest/channel/release_url). So the
detector SELECTS the axis by whether a source root resolves and REPORTS which one it used —
a surface that tells every public user "cannot tell" is precisely the
alarm-that-never-stops that ``__init__._source_drift`` warns about in its own docstring.

ORDER MATTERS — measure, install, RE-MEASURE, then judge. `setup` got this wrong by recording
without comparing, which re-blessed stale bytes three times (trust DB rows 3/4/5, identical
fingerprint). A refresh that trusts its own install without re-checking is the same defect
wearing a helpful face.

EXIT CODES (the gate reads these): 0 = fresh, already or after a successful refresh.
3 = cannot tell (never a pass). 4 = drift under --check-only. 5 = still stale after a
refresh. 6 = the SOURCE moved while the refresh ran (#842): the reference fingerprint
changed between measure and re-measure — concurrent editing, not a stale runtime; the
refresh retries once against the settled tree before returning this. Non-zero means the
local runtime does NOT match its reference (or could not be proven to), so a deploy must
not be reported successful over it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The two questions "is my runtime current?" can mean. Kept as constants because every
# surface must say WHICH ONE it answered — see the two-axis trap above.
AXIS_SOURCE = "source"  # installed package vs the local SOURCE CHECKOUT (dev machines)
AXIS_RELEASE = "release"  # installed version vs the PUBLISHED release (public installs)

# The sanctioned provisioning command, as DATA so a test can assert the argv instead of
# grepping this file's prose. A first draft of the test did grep the source and tripped on the
# docstring explaining that raw pip is NOT used — a DOC-MATCH fake test, the exact species
# being swept out of this suite. Behaviour is assertable; prose is not.
#
# WHY THE CLI AND NOT RAW PIP: `aidocs runtime --fix` is the operator-documented remedy and
# the path #552 fixed to build from the LOCAL TREE rather than the deliberately-stale
# published index. #557 records what raw `pip install --force-reinstall` cost: the trust
# record desynced (runtime --record-package wrote runtime.json, a degraded projection, NOT the
# canonical DB) and the gate withheld verdicts for ~90 minutes across 74 ungoverned calls.
# Installing without recording is the incident, not the fix.
PROVISION_ARGV: tuple[str, ...] = ("-m", "aidocs_mcp.cli", "runtime", "--fix")


def _freshness(reference_pkg: str | None = None) -> dict:
    """The SOURCE axis probe: installed package vs the checkout — or vs the
    REFERENCE tree a caller names (2026-08-22: the deploy's frozen, stamped
    ship-stage, so "fresh" means "installed == what was shipped" rather than
    "== a checkout that may be dirty under #612")."""
    from . import runtime_provisioner as rp

    if reference_pkg:
        return rp.runtime_freshness(source_pkg=reference_pkg)
    return rp.runtime_freshness()


def _source_root():
    """Whether this machine HAS a source checkout — i.e. which axis applies."""
    from . import runtime_provisioner as rp

    return rp.local_source_root()


def _update_state() -> dict | None:
    """The RELEASE axis data, already collected by the watchdog's update checker."""
    from . import aidocs_service

    return aidocs_service.read_update_state()


def _fmt(r: dict) -> str:
    src = (r.get("source_fingerprint") or "?")[:23]
    ins = (r.get("installed_fingerprint") or "?")[:23]
    return f"fresh={r.get('fresh')} source={src}… installed={ins}…"


def freshness_report(reference_pkg: str | None = None) -> dict:
    """Is the enforcement runtime current — on whichever axis this machine HAS?

    Returns ``{"axis", "fresh": True|False|None, "note", "detail"}``. ``fresh`` keeps the
    ``_source_drift`` contract on both axes: None means "cannot tell" and is NEVER a pass.
    ``axis`` is part of the answer, not decoration — "stale against source" and "behind the
    published release" are different facts with different remedies, and a surface that
    silently mixes them teaches its reader to ignore it.

    ``reference_pkg`` (2026-08-22) names the tree "fresh" is measured against — the
    deploy hands over its frozen, stamped ship-stage. It is the SOURCE axis by
    definition (a tree is a source), answered even on a box with no checkout.
    Without it, the existing selection stands, and ``_freshness`` is called the
    zero-argument way every existing caller and test double expects.
    """
    out: dict = {"axis": AXIS_SOURCE, "fresh": None, "note": "", "detail": {}}

    if reference_pkg or _source_root() is not None:
        detail = _freshness(reference_pkg) if reference_pkg else _freshness()
        out["detail"] = detail
        out["fresh"] = detail.get("fresh")
        out["note"] = detail.get("note") or ""
        return out

    # No checkout: the source axis is unanswerable BY CONSTRUCTION here, so asking it would
    # manufacture a permanent "cannot tell" for every public user. Ask the release question
    # instead, off data the watchdog's check-only updater already persisted.
    out["axis"] = AXIS_RELEASE
    state = _update_state()
    out["detail"] = state or {}
    if not state:
        out["note"] = "no release check on record yet — run `aidocs service update-check`"
        return out
    if state.get("error"):
        out["note"] = f"release channel unreachable: {state['error']}"
        return out
    if not state.get("latest") or not state.get("current"):
        out["note"] = "release channel answered without a comparable version — cannot tell"
        return out
    out["fresh"] = not bool(state.get("update_available"))
    if not out["fresh"]:
        out["note"] = (
            f"a newer release is published: installed {state.get('current')} < "
            f"{state.get('latest')} ({state.get('release_url') or state.get('channel')})"
        )
    return out


def _sync_package_trust(detail: dict, axis: str, *, check_only: bool, emit) -> dict | None:
    """Make the TRUST ROW describe the bytes we just proved are current (#627).

    THE GAP THIS CLOSES, measured 2026-08-18 — twice in one operator session,
    each time halting it. `provision_venv` already re-records after IT installs
    (`_record_package_after_install`, #589 fix 3), and that helper is correct.
    But a deploy installs the new wheel by its OWN route, so by the time this
    refresher looks, installed ALREADY equals source: it reports "already
    current — nothing to do", never provisions, and the re-record therefore
    never fires. The trust row keeps describing the PREVIOUS install, so
    `package_integrity` sees drift and the gate fail-closes on every tool call
    (#589, correctly). Operator, verbatim: "why every deploy now blocks AIDOCS?"

    So the two artefacts #627 names — the installed package and the recorded
    trust — need a handshake at the one moment we can honestly make it: AFTER
    freshness has been PROVEN. That ordering is this module's own law ("measure,
    install, RE-MEASURE, then judge ... a refresh that trusts its own install
    without re-checking is the same defect wearing a helpful face"). We are not
    trusting an install here; we are recording bytes a comparison just proved
    match their reference.

    WHICH FINGERPRINT COUNTS (#889, 2026-08-23 -- this recurred three times in
    one day AFTER the fix above). `read_manifest` is runtime.json, the
    PROJECTION; the gate resolves trust from the CANONICAL DB. Comparing only
    the projection let this function read agreement where the gate saw drift,
    and write nothing. It now asks `verify_package_integrity` for the gate's
    OWN verdict and repairs on either signal, so "nothing to do" can never
    again mean "nobody asked the store that refuses".

    EACH AXIS RECORDS ON ITS OWN EVIDENCE (#914, corrected 2026-08-26).

    THE PARAGRAPH THAT USED TO SIT HERE WAS STALE AND SAID THE OPPOSITE of the
    code below it: "ONLY ON THE SOURCE AXIS ... The release axis carries no
    installed fingerprint to compare, so there is nothing to prove and nothing is
    written." That was true until #914 shipped, and it then described a behaviour
    the function no longer had. A triage pass flagged it as grounds to RE-OPEN a
    closed item -- prose contradicting the code it documents is exactly #840, and
    it cost a re-audit.

    What actually happens: the SOURCE axis compares fingerprints and repairs on
    disagreement, as before. The RELEASE axis has no installed fingerprint to
    compare, so it proves itself a different way -- `verify_release_under_selected
    _interpreter` checks the Ed25519 signature under the OWNED interpreter, and
    the trust row is written ONLY on `checked and ok`. An unchecked or failed
    signature records nothing and is announced.

    So the old rule's INTENT survives intact: nothing is blessed without proof,
    which is the masking `_record_package_after_install` warns against. What
    changed is that "proof" on the release axis is a signature rather than a
    fingerprint comparison. Equal fingerprints still mean the ledger agrees, and
    writing an audited row to say nothing changed is still noise.

    UNDER THE OWNED RUNTIME INTERPRETER, never this one. `--record-package`
    stamps the provenance of the interpreter it RUNS UNDER: this module is
    invoked by the deploy under the DEV venv, which would record
    ``dev_editable`` — reporting success while leaving the hooks refusing. That
    is the trap the CLI's own remedy text warns about, and the reason this
    routes through ``record_selected_interpreter_trust`` (which spawns the
    selected runtime and then CONFIRMS the row in the authoritative DB rather
    than trusting the subprocess's word).

    NEVER CHANGES THE VERDICT. A refresh that reached "current" stays "current";
    this only reports. But a FAILURE here is announced loudly, because a current
    runtime with a stale trust row IS the locked-out state, and silence would
    leave the operator to discover it as a wall of refusals.
    """
    release_proof = ""
    if axis != AXIS_SOURCE:
        # #914 -- EACH AXIS RECORDS ONLY ON PROOF APPROPRIATE TO THAT AXIS.
        #
        # The source axis is admitted below because a fingerprint comparison has
        # already PROVEN the installed bytes match their reference. The release
        # axis has no installed fingerprint, so for a long time it was refused
        # outright -- correctly, since recording without proof is the blanket
        # "bless whatever is installed" that `_record_package_after_install`
        # exists to refuse.
        #
        # BUT THE RELEASE AXIS DOES HOLD PROOF, AND A STRONGER ONE: the artefact
        # is verified against its Ed25519-SIGNED MANIFEST. Fingerprint equality
        # proves two local trees match; a signature proves the bytes are the ones
        # the release was built and signed from. Refusing the stronger evidence
        # while accepting the weaker was never principled -- it was that the
        # verdict could not be OBTAINED safely, because `verify_release` called
        # in THIS process judges whichever tree this process imported (the dev
        # checkout when the deploy drives it). That is the mistake #889 made
        # three times in one day; see its ROUND 2/ROUND 3 notes below.
        #
        # `verify_release_under_selected_interpreter` closes exactly that gap, so
        # the release axis is now admitted WHEN AND ONLY WHEN the OWNED RUNTIME
        # says its own install verifies against the signature. Unknown is not a
        # pass: `checked=False` ("could not ask") is refused just like a failed
        # signature, and both are ANNOUNCED rather than returning None in silence
        # -- the silent dead-end #910 was about, one file over.
        #
        # WHY THIS MATTERS BEYOND TIDINESS: `_record_package_after_install` only
        # fires on `action == "installed"` (runtime_provisioner.py:1568), so a
        # NO-OP provision records nothing. Release axis + no-op provision + a
        # trust row already behind meant NEITHER recorder ran, and the operator
        # got a wall of refusals with no repair available. That is the shape of
        # the dev-1883 lockout.
        _rel_axis = axis == AXIS_RELEASE
        if not check_only or _rel_axis:
            # ASK THE OWNED RUNTIME WHETHER ITS INSTALL IS SIGNATURE-GOOD (#914).
            # This does NOT record anything -- it turns "I cannot prove what I
            # would be recording" into a real diagnostic, which is the single
            # most useful fact for deciding whether `--record-package` is safe to
            # run. Three-valued, like every other axis here: verified / NOT
            # verified / could not ask. `checked=False` is the third state and is
            # never folded into either of the first two.
            #
            # Cross-interpreter ON PURPOSE: verify_release resolves the package
            # of the process it runs in, and this module runs under the DEV venv
            # when the deploy drives it. An in-process call would report
            # confidently about the checkout instead of the governed install --
            # #889's mistake, three times in one day.
            # Imported under DISTINCT names on purpose. `_pi` and `rp` are bound
            # further down this function, which makes them locals for the WHOLE
            # scope -- touching them up here is an UnboundLocalError, not a
            # module reference. Caught by the existing #627/#889 release-axis
            # tests, which is exactly what they are for.
            from pathlib import Path as _PathHere

            from . import package_integrity as _pkg_integrity
            from . import runtime_provisioner as _provisioner

            _sig = _pkg_integrity.verify_release_under_selected_interpreter(
                str(_provisioner.venv_python(_PathHere.home()) or "")
            )
            if not _sig.get("checked"):
                _note = (
                    "could not be established "
                    f"({_sig.get('reason') or 'no reason given'}) -- unknown, "
                    "which is not the same as bad"
                )
            elif _sig.get("ok"):
                _note = (
                    "VERIFIES against its signed manifest "
                    f"(build {_sig.get('build_number')}, commit "
                    f"{str(_sig.get('commit') or '')[:12]}), so re-recording "
                    "would be blessing signature-checked bytes"
                )
            else:
                _note = (
                    "DOES NOT verify against its signed manifest "
                    f"({_sig.get('reason') or 'no reason given'}) -- do NOT "
                    "re-record until that is understood"
                )

            # PROOF ACCEPTED -> the release axis is admitted to the repair below.
            # Only on a CHECKED and OK signature: "could not ask" and "the
            # signature failed" both fall through to the announcement, because
            # unknown is not a pass and a failed signature is evidence AGAINST
            # these bytes -- recording either would be the blanket blessing.
            if _rel_axis and _sig.get("checked") and _sig.get("ok"):
                release_proof = str(_sig.get("fingerprint") or "") or "signed"
                if not check_only:
                    emit(
                        "[runtime-refresh] release axis ADMITTED for trust "
                        f"repair: the installed package {_note}."
                    )
            elif check_only:
                # check_only asks, it does not act. It already prints its own
                # stale-trust line further down, so adding one here would warn
                # twice on every green deploy -- and a warning printed twice is
                # one nobody reads, which is how the real one gets missed.
                return None
            else:
                emit(
                    "[runtime-refresh] trust re-record SKIPPED: this runtime is "
                    f"on the {axis!r} axis, which carries no installed "
                    "fingerprint to compare, so the repair below cannot prove "
                    f"what it would be recording. The installed package {_note}."
                    " IF the gate starts refusing tool calls with package "
                    "drift, this is why, and the fix is: "
                    '"<runtime python>" -m aidocs_mcp.cli runtime --record-package'
                )
        # FALL THROUGH ONLY WITH PROOF IN HAND. Every other path above has
        # already said why it is declining, so this stays silent -- the one
        # place in this function where returning None without a word is correct,
        # because the word was already spoken.
        if not release_proof:
            return None
    # On the release axis the SIGNATURE fingerprint stands in for the freshness
    # digest: it is what the repair below will report as the bytes it recorded,
    # and it is the value the proof actually covers.
    installed_fp = (detail or {}).get("installed_fingerprint") or release_proof
    if not installed_fp:
        # Same rule, different cause: the axis is right but the measurement is
        # missing, so there is still nothing to prove. Announce rather than
        # vanish -- a caller reading `trust: null` cannot otherwise tell "nothing
        # to do" from "could not tell".
        if not check_only:
            emit(
                "[runtime-refresh] trust re-record SKIPPED: no installed "
                "fingerprint in this freshness report, so there is nothing to "
                "compare the trust row against. If the gate refuses with package "
                'drift, run: "<runtime python>" -m aidocs_mcp.cli runtime '
                "--record-package"
            )
        return None

    from pathlib import Path

    from . import package_integrity as _pi
    from . import runtime_provisioner as rp

    base = Path.home()
    # ── ONE AUTHORITY, ONE SCHEME (#889, 2026-08-24) ───────────────────────
    #
    # There used to be a second opinion here: read `runtime.json`'s
    # `package_fingerprint` (the PROJECTION) and compare it to the freshness
    # digest, then skip the repair only if BOTH that comparison and the gate
    # agreed. Two problems, and the second one bit:
    #
    #   1. It asked a question the gate does not ask. The gate resolves trust
    #      from the CANONICAL DB, which is why #889 already had to add the
    #      `verify_under_selected_interpreter` call below.
    #   2. It compared DIFFERENT FILE SETS. `detail["installed_fingerprint"]` is
    #      the freshness digest, which since #867 EXCLUDES the build stamp, while
    #      the trust row carries the FULL package digest. Measured on the
    #      operator's box, same package, same second:
    #          freshness  = sha256:c80bf380...
    #          gate/trust = sha256:4fac8fde...
    #      Permanently unequal, so the skip could never fire, this function
    #      re-recorded on every refresh, and the operator kept being told to run
    #      `--record-package` by hand.
    #
    # The campaign's rule is "refuse a second hashing scheme", and the honest
    # reading is that the SECOND OPINION was the defect, not its arithmetic:
    # ASK THE GATE, ACT ON THE GATE. A reconciled second digest would still be
    # two answers waiting to disagree again the next time either side changes
    # what it hashes.

    # ── #889: ASK THE GATE, DO NOT INFER ITS VERDICT ────────────────────
    # `read_manifest` is runtime.json -- the PROJECTION. The gate resolves
    # trust with source=="db", the CANONICAL trust DB (package_integrity.py
    # :459). Those are two stores, and comparing the one the gate does NOT
    # read is how this recurred on 2026-08-23, three times in one day: the
    # projection matched the installed bytes, this function concluded "the
    # ledger already agrees" and wrote nothing, and the gate refused every
    # tool call against a DB row nobody had looked at.
    #
    # That is #627's own headline arriving one layer down -- "two artefacts,
    # no contract". The contract has to be with the artefact THE GATE READS,
    # so we ask it for its verdict instead of deriving our own.
    #
    # ROUND 2 (#889, same day): asking `verify_package_integrity(base)` HERE was
    # itself the bug I had just fixed elsewhere. This module runs under the DEV
    # venv when the deploy drives it, and measured there the gate reports
    # provenance='dev_editable', trust_source='live_provenance', drifted=False
    # UNCONDITIONALLY -- "editable/dev install; integrity not pinned". A
    # confident answer about the WRONG INSTALL is worse than no answer. The
    # verdict that matters belongs to the OWNED RUNTIME interpreter -- what the
    # hooks execute, and the only install the drift check is pinned against.
    py = rp.venv_python(base)
    verdict = _pi.verify_under_selected_interpreter(base, py)
    # CANNOT CONFIRM AGREEMENT IS NOT AGREEMENT. `checked=False` means no
    # verdict was obtained and must never read as "clean". Erring toward
    # recording is safe HERE and only here: we are past the source-axis guard
    # with an installed fingerprint in hand, so a comparison has already PROVEN
    # these bytes match their reference -- we record something measured, never
    # bless something unexamined. Erring toward silence produced the lockout.
    #
    # ROUND 3 (#889, 2026-08-24) — AND A VERDICT WITH NO ROW BEHIND IT IS NOT
    # AGREEMENT EITHER. Round 2 above asked the right INTERPRETER but the spawn
    # inherited the deploy's `PYTHONPATH=<repo>/mcp/server`, which outranks
    # site-packages, so the runtime interpreter imported `aidocs_mcp` from the
    # DEV CHECKOUT anyway and answered from the editable short-circuit:
    #     provenance='dev_editable', trust_source='live_provenance',
    #     drifted=False   ("MUTABLE; integrity not pinned")
    # -- reached without hashing a byte or opening the trust store. `drifted`
    # was False, this read it as agreement, wrote nothing (`"trust": null` in
    # the deploy report), and the hook then refused every tool call against a DB
    # row nobody had synced. THAT is why `--record-package` was still being run
    # by hand after every green deploy.
    #
    # The leak is fixed at its source (`_default_subprocess_runner` scrubs the
    # import axis). This defines agreement POSITIVELY so the same class of
    # non-answer can never again pass as clean: `drifted=False` is returned by
    # THREE different states -- the row matched, there is no row
    # (`unverified`), and the install is editable so no row was consulted
    # (`mutable`). Only the first is agreement. UNKNOWN IS NOT A PASS.
    gate_agrees = (
        bool(verdict.get("checked"))
        and not verdict.get("drifted")
        and not verdict.get("unverified")
        and not verdict.get("mutable")
    )

    if gate_agrees:
        return None

    if check_only:
        emit(
            "[runtime-refresh] PACKAGE TRUST IS STALE: the installed package is "
            "current, but the recorded trust row still describes different bytes, "
            "so the gate will refuse every tool call. Not repairing it under "
            "--check-only. Run without --check-only, or: "
            '"<runtime python>" -m aidocs_mcp.cli runtime --record-package'
        )
        return {"recorded": False, "reason": "check_only", "would_record": True}

    if not py:  # `py` was resolved above, before the gate was asked
        emit(
            "[runtime-refresh] package trust is stale and the OWNED RUNTIME "
            "interpreter could not be resolved — cannot re-record. The hooks will "
            "keep failing closed until `runtime --record-package` is run under it."
        )
        return {"recorded": False, "reason": "runtime_interpreter_unresolved"}

    out = _pi.record_selected_interpreter_trust(
        base,
        str(py),
        source="runtime-refresh",
        record_home=base,
    )
    if out.get("recorded"):
        # RECORD, THEN RE-MEASURE. This module's own law, applied to the trust
        # row instead of the bytes: "measure, install, RE-MEASURE, then judge
        # ... a refresh that trusts its own install without re-checking is the
        # same defect wearing a helpful face." A record that reports success
        # while the gate still refuses is exactly the state that halted the
        # operator three times in one day, and the only way to tell those two
        # apart is to go and look.
        after = _pi.verify_under_selected_interpreter(base, py)
        out["verified"] = bool(after.get("checked")) and not after.get("drifted")
        if out["verified"]:
            emit(
                "[runtime-refresh] package trust re-recorded and CONFIRMED under "
                f"the owned runtime ({installed_fp[:23]}…) — the gate and "
                "the install now agree."
            )
        else:
            emit(
                "[runtime-refresh] package trust was recorded but the gate STILL "
                f"refuses under the owned runtime (checked={after.get('checked')}, "
                f"reason={after.get('reason') or 'unknown'}). Tool calls will be "
                "DENIED until this is resolved; the operator remedy is "
                "`runtime --record-package` under the runtime interpreter."
            )
    else:
        emit(
            "[runtime-refresh] COULD NOT re-record package trust "
            f"(reason={out.get('reason') or 'unknown'}). The runtime is current but "
            "the gate will refuse every tool call until this is recorded under the "
            "owned runtime interpreter."
        )
    return out


def _owned_runtime_has_mempalace() -> bool:
    """Does the OWNED RUNTIME already carry the vendored palace engine?

    FAIL-CLOSED: anything other than a positive sighting returns False, because
    the cost of being wrong is a runtime that installs cleanly and cannot boot.
    """
    from pathlib import Path

    from . import runtime_provisioner as rp

    try:
        # Derived from the OWNED interpreter rather than a tier name: this path
        # only ever targets the venv AIDOCS provisions, and asking for a tier by
        # string returned None on the first attempt (silently refusing every
        # update, which is fail-closed in the useless direction).
        py = rp.venv_python(Path.home())
        if not py:
            return False
        installed = rp._installed_pkg_in(Path(py).parents[1])
        return bool(installed) and (Path(installed).parent / "mempalace").is_dir()
    except Exception:  # noqa: BLE001 - a guard that cannot look must not admit
        return False


def _expected_build(url: str, svc) -> tuple[str | None, int | None]:
    """What the authority NAMED, and the floor we must not fall below (#903).

    Returns ``(expect_commit, min_build)``, either of which may be None when it
    could not be established. None means "cannot check that axis" — it never
    means "any artefact will do": the pull's own guards decide what to do with a
    missing expectation, and the signature check is unconditional either way.
    """
    expect_commit: str | None = None
    min_build: int | None = None
    try:
        from . import _deployed_from_authority

        named = _deployed_from_authority(svc._fetch_authority_axes(), url, "unreachable")
        commit = str(named.get("commit") or "")
        expect_commit = commit or None
    except Exception:  # noqa: BLE001 — an unreadable axis is not an excuse to skip the pull
        expect_commit = None
    try:
        installed = svc._installed_build()
        min_build = installed if isinstance(installed, int) and not isinstance(installed, bool) else None
    except Exception:  # noqa: BLE001
        min_build = None
    return expect_commit, min_build


def _authority_reference(emit) -> str | None:
    """Fetch the AUTHORITY's signed build and return its package dir, or None.

    The install source of last resort, and on a client machine the ONLY one:
    there is no checkout, no ship stage and no git. `pull_release` verifies the
    signature before handing anything back, so a tree that reaches the
    provisioner has already proven it is the build the authority published.

    Never raises. Every failure is None, and None means the caller falls through
    to a path that can still reach rc=3 — a fetch that did not happen must never
    look like a fetch that succeeded.
    """
    from pathlib import Path

    from .release_pull import BUILD_PATH, PullRefused, pull_release

    try:
        from . import aidocs_service as _svc
        from .build_authority import authority_url

        url = authority_url()
        if not url:
            return None  # the operator disabled the authority; that is an answer
        # WHAT WE WERE PROMISED, so the pull can refuse anything else (#903).
        # Identity alone is not enough against an authority that has itself been
        # made to name an old build, so the installed build is handed over as a
        # floor: never move backwards.
        expect_commit, min_build = _expected_build(url, _svc)
        # AN UPDATE ARTEFACT, NOT A PROVISION ARTEFACT (#908). aidocs_mcp
        # declares mempalace's runtime DEPS but not the vendored mempalace
        # module itself, so installing this artefact over a runtime that already
        # has mempalace is complete and correct, while installing it into a
        # runtime WITHOUT mempalace yields a venv that imports aidocs_mcp and
        # CANNOT START THE SERVER -- the exact failure vendored_mempalace_dir
        # records, and the worst kind available here because the install
        # SUCCEEDS and every check passes.
        #
        # CORRECTED 2026-08-26 (#840 -- a comment must match what executes).
        # This used to say the published artefact does not ship
        # third_party/mempalace. IT DOES, and has since #913's serving half: the
        # deploy stages third_party/ into the release dir
        # (deploy_aidocs_gate.sh:5268), build_signed_release records a `vendored`
        # claim over it, and outer_gate_transport._release_archive_bytes ships
        # the tree whenever the signed claim matches. Measured 2026-08-26 by
        # building and signing a release-layout tree from this repo: 95 vendored
        # members in the archive, verdict `match`.
        #
        # WHAT HAS NOT CHANGED IS THE INSTALL SIDE, WHICH IS WHY THIS GUARD
        # STAYS. runtime_provisioner.vendored_mempalace_dir looks for the tree at
        # <project>/../third_party/mempalace and requires a pyproject.toml beside
        # it; on a pulled release the tree sits INSIDE the project and the signed
        # bytes are the importable package alone, so it returns None and the
        # shipped engine is never installed. The artefact now CARRIES the cure
        # and this path still cannot APPLY it.
        if not _owned_runtime_has_mempalace():
            emit(
                "[runtime-refresh] REFUSING the authority's build: this runtime "
                "does not already carry the vendored mempalace engine, and "
                "nothing on this install path consumes the one the artefact now "
                "ships (#913). Installing it would produce a runtime that "
                "imports aidocs_mcp and cannot start. Provision from a full "
                "source tree first; this path UPDATES, it does not provision."
            )
            return None
        dest = Path.home() / ".aidocs" / "cache" / "release-pull"
        project = pull_release(
            url=url.rstrip("/") + BUILD_PATH,
            dest=dest,
            expect_commit=expect_commit,
            min_build=min_build,
        )
    except PullRefused as exc:
        emit(f"[runtime-refresh] the authority's build was REFUSED: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — a broken fetch must not stop the refresh
        emit(f"[runtime-refresh] could not fetch the authority's build: {exc!r}")
        return None
    from .release_pull import package_dir_for

    pkg = package_dir_for(project)
    if pkg is None:
        emit("[runtime-refresh] pulled build carries no package dir to measure against")
        return None
    emit(f"[runtime-refresh] pulled the authority's SIGNED build → {project}")
    return str(project), str(pkg)


def refresh_runtime(
    *,
    check_only: bool = False,
    emit=print,
    package_spec: str | None = None,
    reference_pkg: str | None = None,
) -> dict:
    """Measure → (provision) → RE-MEASURE → judge. The one implementation, two callers.

    ``emit`` is the output seam: the deploy gate wants it on stdout (its verdicts must stream
    live and unpiped — §XVIII), the watchdog wants it in watchdog.log. Returns
    ``{"code", "verdict", "axis", "before", "after"}``; ``code`` is the process exit code the
    gate keys on.

    ``package_spec`` / ``reference_pkg`` (2026-08-22): what to INSTALL and what "fresh" is
    MEASURED against. The deploy passes its frozen, stamped ship-stage for both, so the
    local runtime receives the same bytes the VPS does — stamp included, which is what lets
    ai_version's ``running`` axis name its own build on a dev box — and is judged against
    that stage rather than a live checkout someone may be saving into (#612 / #842). Both
    default to None, which is byte-for-byte the previous behaviour (install from the
    checkout, measure against the checkout).
    """
    # ── THE AUTHORITY IS THE SOURCE (#903) ──────────────────────────────
    # When no caller named a stamped stage, the build to install comes from the
    # AUTHORITY, not from whatever tree happens to be on this disk.
    #
    # Measured 2026-08-24: with the authority serving build 189, `runtime --fix`
    # reported "OK ... verified=True" and installed nothing, because the tree it
    # measured against was a LEFTOVER TEMP SHIP-STAGE two builds old. Before
    # that it would have used the live checkout — which carries no stamp
    # (gitignored, generated per build), so an install from it can never name
    # its own build, and the campaign forbids it outright.
    #
    # The deploy still passes its own frozen, stamped ship-stage and is
    # untouched: it names both, so nothing is fetched. This only fires for the
    # caller that had nothing — the watchdog acting on "you are behind".
    #
    # FAIL-SOFT INTO FAIL-CLOSED: a refused or unreachable pull returns None and
    # the existing path runs, which reaches rc=3 "cannot tell" rather than
    # inventing a source. Unknown is not a pass; it is also not an excuse to
    # install something unverified.
    if package_spec is None and reference_pkg is None:
        pulled = _authority_reference(emit)
        if pulled:
            # INSTALL the project; MEASURE against the package. Setting both to
            # one path is what shipped first, and it handed pip a directory with
            # no pyproject.toml.
            package_spec, reference_pkg = pulled

    _package_args: tuple[str, ...] = ("--package", str(package_spec)) if package_spec else ()
    # Zero-arg call when no reference: every pre-existing caller and test double
    # installs `freshness_report` / `_freshness` as zero-arg callables.
    report = freshness_report(reference_pkg) if reference_pkg else freshness_report()
    before = report.get("detail") or {}
    emit(f"[runtime-refresh] axis={report['axis']} before: {_fmt(before)}")
    if report.get("note"):
        emit(f"[runtime-refresh]   {report['note']}")

    # UNKNOWN IS NOT A PASS (the _source_drift contract, copied deliberately). A freshness
    # check that cannot tell must not be read as agreement — a machine with no source
    # checkout and no release answer is a legitimate state, but it is NOT a proven-current
    # one, so it cannot silently satisfy a caller that is about to report success.
    if report.get("fresh") is None:
        emit(
            "[runtime-refresh] CANNOT TELL whether the enforcement runtime is current — "
            "refusing to report success on an unverified runtime."
        )
        return {
            "code": 3,
            "verdict": "unknown",
            "axis": report["axis"],
            "before": before,
            "after": None,
        }

    if report.get("fresh") is True:
        emit("[runtime-refresh] already current — nothing to do.")
        # ...for the CODE. The trust row can still be stale: a deploy installs
        # the wheel by its own route, so we arrive here with nothing to
        # provision and the re-record inside provision_venv never fires (#627).
        trust = _sync_package_trust(
            before, report["axis"], check_only=check_only, emit=emit
        )
        return {
            "code": 0,
            "verdict": "fresh",
            "axis": report["axis"],
            "before": before,
            "after": before,
            "trust": trust,
        }

    if check_only:
        emit(
            "[runtime-refresh] DRIFT (check-only): the hooks are enforcing code that "
            "differs from its reference. Run without --check-only to refresh."
        )
        return {
            "code": 4,
            "verdict": "drift",
            "axis": report["axis"],
            "before": before,
            "after": None,
        }

    emit("[runtime-refresh] drift detected — provisioning the owned runtime")
    # #345 passthrough-lambda seam. An earlier revision called subprocess.run
    # DIRECTLY here and waived the rule, arguing that routing through the egress
    # service would invert the dependency — the refresher repairs the runtime that
    # service runs under, so it must not require that runtime to be healthy.
    # THAT ARGUMENT WAS WRONG, and the process-audit census proved it: this was the
    # ONLY raw_unaudited callsite in the entire package (54 audited, 1 raw).
    # `audited_run` forwards run_kwargs UNCHANGED, so behaviour is byte-identical to
    # a direct call, and the dependency-inversion concern applies only to
    # ShellEgressService.execute(), which this deliberately does not use.
    # NOT a justification, recorded as debt: today `audited` means "appears in the
    # observability ledger", not "passed the policy cascade", and recording is
    # BEST-EFFORT. Both are defects under the 2026-07-28 ruling that audited must
    # mean BOTH and that best-effort is "yeah maybe not" — an unrecorded spawn is an
    # ungoverned spawn nobody hears about. This callsite joins the 52 others so it is
    # no longer the single raw exception; it does not inherit a clean bill of health.
    # The lambda keeps the literal `subprocess.run` token in this file so the AST
    # doctrine scan still sees a registered callsite, which is what the paired
    # LEGACY_SUBPROCESS_FINGERPRINTS row matches. Both are TEMPORARY: #575 retires
    # this file into `aidocs doctor` and the row goes with it.
    from .shell_egress_service import audited_run

    # WINDOW POSTURE — a regression the spawn seals caught on this very edit, and
    # the sharpest evidence yet for the debt recorded just above. Routing through
    # `audited_run` put this call in the LEDGER but gave it none of the platform
    # popen flags that ShellEgressService.execute() applies: the passthrough lambda
    # forwards run_kwargs "unchanged", and unchanged included having no
    # creationflags at all. Under the pythonw daemon — which has no console of its
    # own — an unflagged console child POPS A VISIBLE WINDOW in the operator's
    # face, from a watchdog-driven path with nobody watching. So "audited" bought
    # observability and silently cost a user-visible property, which is exactly the
    # failure mode of audited meaning only "in the ledger".
    # Declared locally, not imported: every other spawn site in this package
    # declares its own so a low-level spawn takes no dependency, and THIS module
    # above all must keep working when the runtime it exists to repair is broken.
    # creationflags=0 is safe on POSIX — subprocess only rejects a NON-zero value.
    _WIN_NO_WINDOW = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    )

    # #842: A REFERENCE THAT MOVED MID-FLIGHT IS CONCURRENT EDITING, NOT STALENESS.
    # Measured 2026-08-19 (deploy of b6165ce51, PROMOTED and serving): concurrent
    # agents saved into the source tree while this ran under the deploy's step 5c.
    # The provision installed the pre-edit tree, the re-measure compared against
    # the post-edit tree, and the verdict was rc=5 "still stale after 'runtime
    # --fix'" — the exact signature of a broken provisioner, for a runtime the
    # named remedy declared "already current — nothing to do" seconds later. The
    # source fingerprint distinguishes the two: if it CHANGED between measure and
    # re-measure, the input moved under us — so retry ONCE against the settled
    # tree, and if it moved AGAIN return the distinct source_moved verdict (6)
    # instead of crying stale. Fail-closed posture unchanged: 6 is still non-zero
    # (parity was NOT proven); it just names the true cause. #612's ruling ("we
    # should be able to actually work while a deploy runs") is why the moved case
    # deserves its own name rather than masquerading as provisioner rot.
    prev = before
    after_report: dict = {}
    after: dict = {}
    for _attempt in (1, 2):
        proc = audited_run(
            [sys.executable, *PROVISION_ARGV, *_package_args],
            fingerprint="runtime_refresh.py::refresh_runtime::subprocess.run",
            reason="runtime-refresh-provision",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=900,
            creationflags=_WIN_NO_WINDOW,
        )
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        if tail.strip():
            emit(tail.rstrip())
        if proc.returncode != 0:
            emit(f"[runtime-refresh] `runtime --fix` failed (rc={proc.returncode})")

        # RE-MEASURE. The install's own exit code is NOT the verdict: `setup` exits 0
        # while leaving the runtime stale, which is the whole reason this module
        # exists. Only a fresh comparison settles it — and conversely, an installer
        # that failed its own reporting may still have landed the code, so rc != 0
        # does not settle it either.
        after_report = freshness_report(reference_pkg) if reference_pkg else freshness_report()
        after = after_report.get("detail") or {}
        emit(f"[runtime-refresh] axis={after_report['axis']} after:  {_fmt(after)}")
        if after_report.get("fresh") is True:
            emit("[runtime-refresh] OK — the enforcement runtime is current.")
            # RECONCILE THE DEPLOYED EXTERNAL SHIM (#973). The package swap is
            # what CREATES the drift — the shim lives OUTSIDE site-packages so
            # pip cannot take it away, which also means pip never updates it —
            # so the swap is the operation that must repair it. Measured: a shim
            # from Aug 23 still executing after the build-221 swap, emitting the
            # pre-#932 update text and sending its reader at the wrong remedy.
            #
            # A FRESH PROCESS, NOT AN IN-PROCESS CALL. This module imported
            # `claude_hooks_install` BEFORE the replacement, so calling
            # `ensure_hook_shim()` here would read `shim_source()` off the OLD
            # module and redeploy the OLD bytes — while reporting success. The
            # named argv re-imports the package that was just installed.
            #
            # Best-effort by design: a refresh that repaired the runtime must
            # not be downgraded to a failure because a secondary reconciliation
            # could not run. It is reported either way, and the hook self-repair
            # (#973 step 1) heals the same file on the next hook run.
            try:
                # NO passthrough lambda here, deliberately — unlike the
                # provision call above. That one keeps a literal
                # `subprocess.run` token so the AST doctrine scan still sees its
                # REGISTERED legacy callsite; it is grandfathered debt with a
                # LEGACY_SUBPROCESS_FINGERPRINTS row and a semgrep waiver.
                # A NEW spawn should not inherit that debt. `audited_run`
                # defaults to subprocess.run inside shell_egress_service, which
                # is "the sanctioned chokepoint" — so routing through it adds no
                # direct-run callsite, needs no `# nosemgrep`, and buys no new
                # baseline row. Caught by the spawn seals on the first deploy of
                # this change, which is exactly what those seals are for.
                _rc = audited_run(
                    [sys.executable, "-m", "aidocs_mcp.cli", "runtime",
                     "--reconcile-hook-shim"],
                    fingerprint="runtime_refresh.py::refresh_runtime::subprocess.run",
                    reason="runtime-refresh-reconcile-hook-shim",
                    capture_output=True,
                    text=True,
                    timeout=120,
                    creationflags=_WIN_NO_WINDOW,
                )
                if _rc.returncode == 0:
                    emit("[runtime-refresh] external hook shim reconciled.")
                else:
                    emit(
                        "[runtime-refresh] external hook shim NOT reconciled "
                        f"(rc={_rc.returncode}) — host-native tools stay governed by "
                        "the deployed copy until the next hook run heals it."
                    )
            except Exception as _exc:  # noqa: BLE001 — never fail a good refresh
                emit(
                    "[runtime-refresh] external hook shim reconciliation could not "
                    f"run ({type(_exc).__name__}); the hook self-repair covers it."
                )
            # provision_venv re-records when IT installs; this covers the case where
            # the bytes arrived another way and the row is still behind (#627).
            trust = _sync_package_trust(
                after, after_report["axis"], check_only=check_only, emit=emit
            )
            return {
                "code": 0,
                "verdict": "refreshed",
                "axis": after_report["axis"],
                "before": before,
                "after": after,
                "trust": trust,
            }
        _prev_fp = (prev or {}).get("source_fingerprint")
        _after_fp = after.get("source_fingerprint")
        _moved = bool(_prev_fp and _after_fp and _prev_fp != _after_fp)
        if not _moved:
            break
        if _attempt == 1:
            emit(
                "[runtime-refresh] the SOURCE TREE CHANGED while refreshing "
                f"({_prev_fp[:23]}… -> {_after_fp[:23]}…) — concurrent editing, not "
                "staleness. Retrying once against the settled tree."
            )
            prev = after
            continue
        emit(
            "[runtime-refresh] SOURCE MOVED AGAIN during the retry — concurrent "
            "edits are outrunning the refresh. This is NOT a stale-runtime fault; "
            "re-run once the tree settles. "
            f"note={after_report.get('note') or '(none)'}"
        )
        return {
            "code": 6,
            "verdict": "source_moved",
            "axis": after_report["axis"],
            "before": before,
            "after": after,
        }
    emit(
        "[runtime-refresh] STILL NOT CURRENT after the refresh. The hooks would keep "
        "enforcing stale law, so this run must not be reported as successful. "
        f"note={after_report.get('note') or '(none)'}"
    )
    return {
        "code": 5,
        "verdict": "still_stale",
        "axis": after_report["axis"],
        "before": before,
        "after": after,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refresh the local AIDOCS enforcement runtime.")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="report drift and exit non-zero; never install (for --tests lanes)",
    )
    ap.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    ap.add_argument(
        "--package",
        default=None,
        help=(
            "what to install: a checkout-shaped dir (the deploy passes its frozen, "
            "STAMPED ship-stage), a wheel, or a version — handed to `runtime --fix "
            "--package` verbatim. Default: the local checkout, as before."
        ),
    )
    ap.add_argument(
        "--reference",
        default=None,
        help=(
            "the aidocs_mcp package dir 'fresh' is measured against (the deploy passes "
            "<stage>/mcp/server/aidocs_mcp). Default: the local checkout, as before."
        ),
    )
    ap.add_argument(
        "--report",
        default=None,
        help=(
            "also write the transcript + verdict here (the deploy passes "
            "mcp/.deploy-reports/runtime-refresh.summary.txt). Fail-soft: a report "
            "that cannot be written never changes the exit code."
        ),
    )
    args = ap.parse_args(argv)

    # #889 action item 2: THE EVIDENCE MUST OUTLIVE THE DEPLOY'S STDOUT.
    #
    # `emit` is a documented seam with two consumers already — "the deploy gate
    # wants it on stdout (its verdicts must stream live and unpiped — §XVIII),
    # the watchdog wants it in watchdog.log". A report FILE is a third, and this
    # CLI boundary is where the deploy actually invokes the refresh, so the tee
    # lives here and `refresh_runtime` is untouched.
    #
    # WHY IT EXISTS AT ALL: the before/after freshness lines and the verdict went
    # ONLY to a stdout captured in a per-session task file no other session can
    # read. This item took four deploys partly for that reason, and a fifth round
    # on 2026-08-24 reconstructing from source a root cause the transcript would
    # have stated outright.
    transcript: list[str] = []

    def _tee(message: str = "") -> None:
        transcript.append(str(message))
        print(message)

    result = refresh_runtime(
        check_only=args.check_only,
        package_spec=args.package,
        reference_pkg=args.reference,
        emit=_tee if args.report else print,
    )
    if args.report:
        _write_refresh_report(args.report, transcript, result)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    return int(result["code"])


def _write_refresh_report(path: str, transcript: list[str], result: dict) -> None:
    """Persist the run's transcript and verdict. NEVER raises.

    WRITTEN ON EVERY OUTCOME, not only success: the failing run is the one whose
    evidence is actually needed, and a report present only when things went well
    reproduces the original defect instead of closing it.

    FAIL-SOFT BY CONTRACT. Evidence capture is observability. Turning an
    unwritable directory into a failed deploy would be an outage manufactured by
    its own bookkeeping, so every error here is swallowed — the verdict already
    reached the caller through the return value and stdout.
    """
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = (
            "# aidocs_mcp.runtime_refresh — local enforcement runtime refresh\n"
            "# Written by the refresh itself so the evidence survives the deploy's\n"
            "# stdout (#889 action item 2). Transcript first, then the verdict.\n\n"
            + "\n".join(transcript).rstrip()
            + "\n\n"
            + json.dumps(result, indent=2, default=str)
            + "\n"
        )
        target.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001 — see docstring: a log must never fail a deploy
        pass


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
