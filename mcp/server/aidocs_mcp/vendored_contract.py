"""#913 — IS THE VENDORED PACKAGE THE ONE THIS BUILD WAS WRITTEN AGAINST?

WHAT WENT WRONG, measured 2026-08-25. AIDOCS ships ``aidocs_mcp`` and IMPORTS
``mempalace``, which it vendors. Only one of those two halves is in the release
artefact: the pull allowlist is ``pyproject.toml`` + ``server/aidocs_mcp/**``, so
``third_party/mempalace`` has never been shipped by the updater. Every successful
self-update therefore advances one half and freezes the other, and nothing said
so. Measured directly after an update that reported OK:

    repo tree ......... third_party/mempalace/mempalace/palace_service.py
    owned runtime ..... ~/.aidocs/runtime/venv/.../mempalace/service.py
    release artefact .. no third_party/mempalace AT ALL

WHY A VERSION PIN WOULD NOT HAVE CAUGHT IT, and this is the whole reason this
module exists rather than a one-line comparison. The skew that started this was a
MODULE RENAME (``service`` -> ``palace_service``) inside an unchanged version. A
pin would have compared 3.5.0 against 3.5.0, reported agreement, and let the
broken import through. The question is not "which version is installed" but "are
the modules this build actually imports present in the copy that is installed" —
so that is what gets asked.

WHY ``import mempalace`` DOES NOT ANSWER IT EITHER. mcp_server.py:3405 already
carries ``import mempalace  # hard import, asserts vendored bundle``. That
assertion is real and it is not enough: the top-level package imports perfectly
while a submodule underneath it has moved. An assertion that cannot fail on the
failure you actually have is not covering it.

THREE-VALUED, BECAUSE "NOT INSTALLED" AND "INSTALLED WRONG" ARE DIFFERENT FACTS.
Running without mempalace is a SUPPORTED configuration — AIDOCS operates without
the palace tools and that is fine, expected, and must stay quiet. Running with a
mempalace that is missing modules this build imports is a DEFECT, and it is the
dangerous one precisely because it LOOKS like the supported case: the import
fails, ``hub.palace`` becomes None, and the gate's palace axis goes dark while
everything else reports healthy. Collapsing the two is what let this hide, so
they are never collapsed here.

    absent  -> mempalace is not installed at all. Benign. Stay quiet.
    ok      -> every contracted module resolves. Benign.
    skewed  -> mempalace IS installed but modules are missing. DEFECT, be loud.

THE LIST IS DECLARED HERE AND DERIVED IN A TEST. Deriving the import set at
runtime would mean parsing the package on every boot to answer a question that
only changes when someone edits an import. So the tuple is written down, and
tests/security/test_vendored_contract_913.py re-derives it from the source and
fails when the two disagree — the same split #909 used for the transport
contract, for the same reason: a hand-maintained list is a promise someone
eventually forgets, and forgetting THIS one means a skewed runtime reports itself
healthy.

RESOLUTION, NOT IMPORT. ``find_spec`` is used rather than ``import``, so asking
the question never executes module code. mempalace pulls chromadb and an ONNX
embedder; a health check that costs a model load is a health check nobody runs.
"""

from __future__ import annotations

import importlib.util

#: Every ``mempalace`` submodule that ``aidocs_mcp`` imports anywhere. Sorted so
#: the derivation test can compare without caring about order. Keep it in step
#: with the source — the test will not let you forget, which is the point.
MEMPALACE_CONTRACT_MODULES: tuple[str, ...] = (
    "mempalace.bridge_tx",
    "mempalace.conjoined_types",
    "mempalace.knowledge_graph",
    "mempalace.miner",
    "mempalace.palace",
    "mempalace.service",
)

#: The three states. Only ``SKEWED`` is a defect.
STATE_ABSENT = "absent"
STATE_OK = "ok"
STATE_SKEWED = "skewed"

#: Vendored trees that the release artefact must carry, as
#: ``(name, path relative to the RELEASE ROOT)``. The path names the IMPORTABLE
#: package (``third_party/mempalace/mempalace``), not the project directory
#: above it — the fingerprint is over the code that actually gets imported, so
#: that is what must be signed.
VENDORED_TREES: tuple[tuple[str, str], ...] = (
    ("mempalace", "third_party/mempalace/mempalace"),
)


def resolve_release_root(pkg_dir, *, trees: tuple[tuple[str, str], ...] = VENDORED_TREES):
    """Find the root that holds the vendored trees, for EITHER layout (#913).

    AIDOCS assembles the same content under two different path schemes, and
    vps_custody.sh:157-158 states both outright:

        custody: root=$CUSTODY/tree  server=mcp/server  tp=third_party
        release: root=$RELEASE       server=server      tp=third_party

    So relative to the PACKAGE the vendored trees sit at a DIFFERENT DEPTH on
    each side -- pkg.parents[1] on the release layout, pkg.parents[2] on the
    custody/source one. A single hardcoded parent silently finds nothing on the
    other layout, which would have made the signed claim simply never appear on
    the VPS deploy path: inert, green, and wrong.

    This is not a new rule; it is the SAME accommodation vps_custody.sh already
    makes one line further up, for pyproject.toml:

        local _pp="$_srv/../pyproject.toml"; [[ "$_srv" == "server" ]] && _pp="pyproject.toml"

    Resolution is by EVIDENCE rather than by guessing which layout we are on:
    the first ancestor that actually contains a vendored tree wins. Bounded to
    the two candidates the schemes above allow, so this can never wander up into
    a parent directory that merely happens to have a third_party/ in it.

    Falls back to ``pkg_dir.parents[1]`` when nothing is found, which yields an
    empty fingerprint map and therefore `unclaimed` -- the safe, unchanged
    behaviour, never a wrong claim.
    """
    from pathlib import Path as _Path

    pkg = _Path(pkg_dir)
    candidates = [p for i, p in enumerate(pkg.parents) if 1 <= i <= 2]
    for root in candidates:
        if any((root / rel).is_dir() for _name, rel in trees):
            return root
    return pkg.parents[1] if len(pkg.parents) > 1 else pkg


def compute_vendored_fingerprints(
    release_root,
    *,
    trees: tuple[tuple[str, str], ...] = VENDORED_TREES,
) -> dict[str, str]:
    """Fingerprint each vendored tree under ``release_root`` (#913 option 1).

    THE SIGNATURE DOES NOT COVER THESE TREES TODAY, and that is the whole reason
    this exists. ``verify_release`` digests ``compute_package_fingerprint(base)``
    where ``base`` is the aidocs_mcp PACKAGE; ``build_signed_release.
    stage_release_tree`` stages that package and nothing else. So the vendored
    dependency sits entirely outside the trust envelope — which is exactly why it
    was never in the served artefact, and why simply ADDING it to the archive
    would be a supply-chain hole rather than a fix: the client would verify the
    package, and unverified third-party code would ride along beside it.

    REUSES THE AUDITED PRIMITIVE. ``compute_package_fingerprint`` is generic over
    a directory and already solves the hard parts — cross-platform-deterministic
    ordering (posix relative-path strings, learned from a Windows-signed bundle
    that failed verification on Linux), the suffix allowlist, and the
    tree-inclusive law. A second hasher here would be the twin pattern applied to
    the one thing that must never disagree with itself.

    The TREE NAME is folded in as the version string so two vendored trees can
    never produce interchangeable digests — swapping one tree's bytes for
    another's would otherwise verify.

    Missing trees are OMITTED rather than recorded as empty. An absent entry
    means "this build does not carry that tree", which the consumer treats as
    unknown; a zero-file digest would be a positive claim that the tree is empty,
    and those are different facts.
    """
    from pathlib import Path as _Path

    from .package_integrity import compute_package_fingerprint

    root = _Path(release_root)
    out: dict[str, str] = {}
    for name, rel in trees:
        tree = root / rel
        if not tree.is_dir():
            continue
        out[name] = compute_package_fingerprint(tree, version=name)["fingerprint"]
    return out


#: Verdicts from :func:`check_vendored_signature`. Three-valued for the reason
#: everything else here is: a release built before this axis existed carries no
#: vendored entry, and "nobody claimed anything" is not "the claim matched".
SIG_MATCH = "match"
SIG_MISMATCH = "mismatch"
SIG_UNCLAIMED = "unclaimed"


def check_vendored_signature(release_root, signed_vendored: dict | None) -> dict:
    """Compare on-disk vendored trees against what the SIGNED manifest claims.

    ``signed_vendored`` is the authenticated manifest's optional ``vendored``
    map. It is OPTIONAL ON PURPOSE and the schema is deliberately NOT bumped:
    ``verify_release`` fail-closes on an unrecognised schema, so raising it would
    invalidate every already-signed release at once — the gate would verify
    nothing, serve nothing, and updates would stop. Fixing a shipping defect by
    breaking shipping is not a fix. So this axis is ADDITIVE, and a release that
    predates it degrades to ``unclaimed`` rather than to a failure.

    Returns ``{"state", "trees", "reason"}``:
        match     -> every claimed tree is present and digests as signed.
        mismatch  -> a claim exists and the bytes disagree. REFUSE.
        unclaimed -> the manifest makes no claim. Ship the package alone, as
                     before; do NOT ship the vendored tree unverified.
    """
    if not signed_vendored:
        return {
            "state": SIG_UNCLAIMED,
            "trees": {},
            "reason": (
                "this signed release predates vendored-tree coverage (#913), so "
                "there is no signed claim to check the vendored code against. "
                "The artefact ships the package alone, exactly as before. "
                "Serving the vendored tree here would hand out bytes the "
                "signature does not cover."
            ),
        }

    actual = compute_vendored_fingerprints(release_root)
    bad = {
        name: {"signed": claimed, "actual": actual.get(name, "<absent>")}
        for name, claimed in signed_vendored.items()
        if actual.get(name) != claimed
    }
    if bad:
        return {
            "state": SIG_MISMATCH,
            "trees": bad,
            "reason": (
                "VENDORED TREE DOES NOT MATCH THE SIGNED MANIFEST: "
                f"{sorted(bad)}. The signature authenticates this claim, so a "
                "disagreement means the vendored code on disk is not the code "
                "that was signed -- tampering, a partial copy, or a build that "
                "staged the trees inconsistently. REFUSING is the only safe "
                "answer: shipping or installing it would defeat the signature "
                "that detected the problem."
            ),
        }

    return {"state": SIG_MATCH, "trees": dict(actual), "reason": ""}


def _resolves(module: str) -> bool:
    """True iff ``module`` can be located WITHOUT executing it.

    Every failure mode is a miss. ``find_spec`` raises ModuleNotFoundError when a
    PARENT package is gone, ValueError on a half-initialised module, and
    AttributeError on some namespace-package edge cases — all of which mean the
    same thing to the caller: this build's import would not have worked.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def check_vendored_mempalace(
    modules: tuple[str, ...] = MEMPALACE_CONTRACT_MODULES,
) -> dict:
    """Three-valued verdict on the installed vendored mempalace.

    Returns ``{"state", "missing", "reason"}``. ``reason`` is empty for the two
    benign states and NAMES A REMEDY for the defect (law 311bf3e6) — including
    the part an operator most needs to know, which is that a plain runtime
    refresh does NOT fix this today, because the update path does not carry the
    vendored package at all (#913).
    """
    if not _resolves("mempalace"):
        return {"state": STATE_ABSENT, "missing": [], "reason": ""}

    missing = [name for name in modules if not _resolves(name)]
    if not missing:
        return {"state": STATE_OK, "missing": [], "reason": ""}

    return {
        "state": STATE_SKEWED,
        "missing": missing,
        "reason": (
            "VENDORED DEPENDENCY SKEW (#913): mempalace IS installed but does "
            f"not provide {', '.join(missing)}, which this build of aidocs_mcp "
            "imports. This is NOT the supported 'running without a palace' "
            "configuration -- it is a HALF-UPDATED RUNTIME, and left alone it "
            "silently disables the gate's palace axis (hub.palace becomes None, "
            "so exact_symbol and operator_pinned blockers stop being evaluated) "
            "while everything else reports healthy. "
            "REMEDY: re-provision the owned runtime from a tree whose "
            "third_party/mempalace matches this build. NOTE THAT `aidocs "
            "runtime --fix` ALONE DOES NOT DO THIS TODAY: the released artefact "
            "DOES now carry third_party/mempalace (#913 serving half), but "
            "nothing on the install path consumes it -- "
            "runtime_provisioner.vendored_mempalace_dir looks for the tree "
            "BESIDE the project it is installing, and on a pulled release the "
            "tree sits inside it. Until that is wired, the vendored copy must be "
            "provisioned from a matching checkout."
        ),
    }


# DELETED 2026-08-25: `vendored_contract_fields()`. It formatted this verdict for
# ai_version's provenance payload and NOTHING CONSUMED IT. Law 183074ae -- a
# capability with no consumer is not a capability -- and the deploy's vulture gate
# hard-failed on it (dev-318, exit 12, gate 1d).
#
# Recorded rather than quietly removed: I cited that same law earlier in this
# session to justify NOT committing a helper without a consumer, then committed
# this one anyway. The gate caught what I did not.
#
# Its two tests went with it, and they are the sharper half of the lesson:
# COVERAGE IS NOT A CONSUMER. They exercised it thoroughly, which is exactly how
# dead code survives -- anyone scanning "is this used?" finds calls and moves on.
# The gate counts callers that are not tests, which is the right question.
#
# Surfacing vendored state in ai_version is still worth doing; it would put
# #913's skew where operators already look. It belongs in the commit that WIRES
# it, with the golden-pin updates that adding a field to that payload requires.
