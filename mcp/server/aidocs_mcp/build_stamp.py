"""#627 PHASE 3 — THE CONTRACT BETWEEN THE TWO ARTEFACTS.

AIDOCS has two artefacts: the SOURCE CHECKOUT the tests import, and the
INSTALLED RUNTIME the daemon serves. Until now nothing bound them, so "fixed"
was an unfalsifiable claim — a commit could be landed, deployed and still not be
what answered a tool call, and every instrument built to notice returned an
empty string that read as "fine".

Phases 1-2 removed the ``parents[N]`` arithmetic and taught every ai_version
mode to say UNVERIFIED out loud. That made AIDOCS honest about not knowing. It
did not make it KNOW, because provenance still lived BESIDE the artefact
(``mcp/.deploy-reports/`` next to the checkout, ``trust/`` copied onto the gate
by the deploy) and an installed copy has neither beside it.

THE CURE HERE: provenance that TRAVELS INSIDE THE ARTEFACT.

    _build_stamp.py   {schema, commit, version, built_at, builder, fingerprint}

written by the PACKAGING STEP from the commit being built, never committed to
the repo, and read back by any copy of the package with no git, no repo and no
seal beside it.

WHICH OF THE FOUR STATES THIS COVERS — read this before quoting a VERIFIED.

  1. COMMITTED — in git.                      NOT covered. Nothing here reads git.
  2. SHIPPED   — the installed bytes are the built bytes.   COVERED: the stamp
     names the commit that produced the bytes, and the fingerprint proves the
     bytes on disk are still those bytes.
  3. LOADED    — the RUNNING process is executing those bytes.   **NOT COVERED,
     AND IT CANNOT BE.** A daemon caches modules at import; a restart is
     required and a restart alone does not re-install. Every check in this
     module reads the FILESYSTEM, and the filesystem cannot see what a
     long-lived process loaded an hour ago. A VERIFIED here means "the artefact
     on disk is intact and can name itself", never "the daemon is running it".
  4. TRUSTED   — the recorded identity describes the installed bytes.   COVERED,
     and this is the state whose silent failure disabled a gate for ~110 tool
     calls: a runtime repair changed installed bytes while the recorded row
     still described the previous install. A stamp whose fingerprint no longer
     matches is MISMATCH — loudly, with both digests shown.

WHY A ``.py`` AND NOT A JSON ASSET. ``trust/**`` is deliberately absent from the
wheel's package-data (shipping the STALE in-tree manifest would be a worse lie
than shipping none — see pyproject). Any new data file would need a package-data
entry and could fall out of a distribution the same way. A ``.py`` inside the
package is carried by ``packages.find`` unconditionally: provenance that cannot
be left behind.

IT IS PARSED, NEVER IMPORTED. ``ast.literal_eval`` on the one assignment: a
provenance reader must not execute the artefact it has been asked to judge, and
import caching would make a second tree in the same process answer with the
first tree's stamp.

IT IS GENERATED, NEVER COMMITTED. A stamp checked into source is correct for
exactly one commit and then lies with authority — precisely what the in-tree
release manifest already does (it reads 2.3.0b5 / c0ee833a while HEAD is
elsewhere). ``.gitignore`` keeps it out; a test asserts git does not track it.

NOTHING HERE EVER FABRICATES. ``write_build_stamp`` REFUSES a stamp with no
commit (a present-but-empty provenance field is the exact disease this item
names), and the reader returns UNVERIFIED with a reason rather than a blank
when there is nothing to read.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

STAMP_REL = "_build_stamp.py"
#: Current schema. Bumped to /2 when the BUILD TICKER joined the stamp (option B,
#: operator ruling 2026-08-21): a three-segment version PLUS a separate integer
#: build, never a fourth version segment.
STAMP_SCHEMA = "aidocs-build-stamp/2"
#: The pre-ticker schema, STILL ACCEPTED AS VALID PROVENANCE and deliberately so.
#: A /1 stamp names a real commit over real bytes; rejecting it as "unknown
#: schema" would destroy true provenance on every artefact installed before this
#: change, in order to announce a field that postdates it. It reports build=None
#: -- an admitted unknown, never a fabricated 0.
STAMP_SCHEMA_LEGACY = "aidocs-build-stamp/1"
_KNOWN_SCHEMAS = (STAMP_SCHEMA, STAMP_SCHEMA_LEGACY)
_ASSIGN = "BUILD_STAMP = "

# Verdicts. Three values, never two, never a bare bool: "cannot tell" and "no"
# must not be the same answer — that equivalence IS #627.
VERIFIED = "VERIFIED"
MISMATCH = "MISMATCH"
UNVERIFIED = "UNVERIFIED"


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _coerce_build(raw: object) -> int | None:
    """The recorded build ticker as an int, or None when it is not one.

    None means "this stamp does not carry a build number" (a schema/1 artefact).
    It is NOT coerced to 0: a zero renders as data and reads as an answer, which
    is the same disease as the empty-string provenance field this module exists
    to refuse. ``bool`` is excluded explicitly because ``isinstance(True, int)``
    is True in Python and ``build: true`` is not a build number.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 1 else None


def stamp_path(pkg_dir: Path | str | None = None) -> Path:
    """Where the stamp lives — INSIDE the package, resolved from this module's
    own location. No parent-counting: the stamp is a sibling of this file by
    construction, which is true in every install layout."""
    base = Path(pkg_dir) if pkg_dir is not None else _package_dir()
    return base / STAMP_REL


def stamp_fingerprint(pkg_dir: Path | str | None = None, *, version: str) -> str:
    """The fingerprint the stamp records and the reader recomputes.

    Excludes the stamp itself — a digest containing the stamp could not equal
    the value written into the stamp, so the self-check would be permanently
    MISMATCH and would therefore be ignored, which is worse than no check.
    """
    from .package_integrity import compute_package_fingerprint

    base = Path(pkg_dir) if pkg_dir is not None else _package_dir()
    return str(
        compute_package_fingerprint(base, version=version, exclude=(STAMP_REL,))[
            "fingerprint"
        ]
    )


def write_build_stamp(
    pkg_dir: Path | str,
    *,
    commit: str,
    version: str,
    build: int,
    builder: str = "",
    built_at: str = "",
) -> dict:
    """PRODUCER SIDE. Write the stamp into ``pkg_dir``; return what was written.

    Called by the packaging step BEFORE the release manifest is fingerprinted
    and signed, so the signature covers the stamp. Refuses to write a stamp
    that cannot name a commit: an empty provenance field that is nonetheless
    PRESENT is the worst of the three states, because its presence asserts the
    question was answered.
    """
    sha = (commit or "").strip()
    if not sha or sha.lower() == "unknown":
        raise ValueError(
            "refusing to write a build stamp with no commit: a stamp that "
            "cannot name what it was built from is worse than no stamp — it "
            "asserts that provenance was recorded when it was not"
        )
    ver = (version or "").strip()
    if not ver or ver == "0.0.0":
        raise ValueError(
            f"refusing to write a build stamp with no usable version ({version!r}) "
            "— the fingerprint folds the version in, so an unknown version makes "
            "the recorded digest unreproducible"
        )
    if _coerce_build(build) is None:
        raise ValueError(
            f"refusing to write a build stamp with no usable build number ({build!r}) "
            "— the ticker is what lets a CLIENT INSTALL name which build it is "
            "running, on a machine where no deploy script has ever run. A missing or "
            "fabricated value would put that answer back in the deploy's hands, which "
            "is the defect this field exists to end. Pass the committed ticker."
        )
    base = Path(pkg_dir)
    stamp = {
        "schema": STAMP_SCHEMA,
        "commit": sha,
        "version": ver,
        "build": int(build),
        "builder": builder or "",
        "built_at": built_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fingerprint": stamp_fingerprint(base, version=ver),
    }
    body = (
        "# GENERATED BY THE PACKAGING STEP — DO NOT COMMIT, DO NOT EDIT.\n"
        "# aidocs #627 phase 3: provenance that travels inside the artefact.\n"
        "# Read by aidocs_mcp.build_stamp via ast.literal_eval, never imported.\n"
        + _ASSIGN
        + json.dumps(stamp, sort_keys=True, indent=2)
        + "\n"
    )
    stamp_path(base).write_text(body, encoding="utf-8")
    return stamp


def read_build_stamp(pkg_dir: Path | str | None = None) -> dict | None:
    """READER SIDE. The stamp as data, or None when there is none.

    Parses the module with ``ast`` and literal-evaluates the value bound to
    ``BUILD_STAMP`` — the assignment is located STRUCTURALLY, so trailing
    statements (a hand-appended line, an editor's stray text) cannot corrupt the
    read and, above all, CANNOT RUN. Nothing here imports or executes the
    artefact it has been asked to judge, and import caching cannot make a second
    tree answer with the first tree's stamp.

    Never raises: a provenance reader that can crash is one more way to learn
    nothing. A present-but-unparseable stamp is reported as an EMPTY stamp
    (which the verdict turns into a named UNVERIFIED), never as "absent" —
    "someone wrote something unreadable here" and "nothing was ever written"
    are different facts.
    """
    malformed = {"schema": "", "commit": "", "version": "", "fingerprint": ""}
    try:
        text = stamp_path(pkg_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    if _ASSIGN not in text:
        return None
    try:
        module = ast.parse(text)
    except SyntaxError:
        return malformed
    found = [
        node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "BUILD_STAMP" for t in node.targets)
    ]
    if not found:
        return malformed
    try:
        value = ast.literal_eval(found[-1])
    except (SyntaxError, ValueError):
        return malformed
    return value if isinstance(value, dict) else malformed


def build_stamp_verdict(pkg_dir: Path | str | None = None) -> dict:
    """THE THREE-VALUED ANSWER. VERIFIED / MISMATCH / UNVERIFIED(reason).

    WHAT A ``VERIFIED`` DOES AND DOES NOT ENTITLE A CALLER TO SAY. It says: the
    package tree on disk carries a stamp, and the bytes present hash to the
    value recorded when they were built — so this artefact can name its commit
    and has not been modified since. It does NOT say the code is COMMITTED
    anywhere, and it emphatically does not say it is LOADED: the process
    answering may have imported different bytes before those on disk changed,
    and no filesystem check can see that. LOADED needs a probe of the running
    process; this is not one.

    Never raises. Never returns an empty string as a verdict.
    """
    stamp = read_build_stamp(pkg_dir)
    if stamp is None:
        return {
            "verdict": UNVERIFIED,
            "reason": (
                "no build stamp inside this artefact "
                f"(aidocs_mcp/{STAMP_REL} absent) — it was not produced by a "
                "packaging step, so it cannot name the commit it was built from"
            ),
            "commit": "",
            "version": "",
            "build": None,
            "built_at": "",
            "expected_fingerprint": "",
            "actual_fingerprint": "",
        }
    commit = str(stamp.get("commit") or "")
    version = str(stamp.get("version") or "")
    expected = str(stamp.get("fingerprint") or "")
    out = {
        "verdict": UNVERIFIED,
        "reason": "",
        "commit": commit,
        "version": version,
        "build": _coerce_build(stamp.get("build")),
        "built_at": str(stamp.get("built_at") or ""),
        "expected_fingerprint": expected,
        "actual_fingerprint": "",
    }
    if str(stamp.get("schema") or "") not in _KNOWN_SCHEMAS:
        out["reason"] = (
            "the build stamp is malformed or written by an unknown schema "
            f"({stamp.get('schema')!r}) — it cannot be checked, so it proves nothing"
        )
        return out
    if not commit or not expected:
        out["reason"] = (
            "the build stamp carries no commit or no fingerprint — a present "
            "but empty provenance record proves nothing and must not read as "
            "an answer"
        )
        return out
    try:
        actual = stamp_fingerprint(pkg_dir, version=version)
    except Exception as exc:  # noqa: BLE001 — cannot verify == say so
        out["reason"] = (
            f"could not recompute this artefact's fingerprint ({type(exc).__name__}) "
            "— the stamp names a commit but nothing confirms the bytes are still "
            "the bytes it was written over"
        )
        return out
    out["actual_fingerprint"] = actual
    if actual == expected:
        out["verdict"] = VERIFIED
        out["reason"] = (
            f"this artefact was built from {commit[:12]} and its bytes still "
            "match the stamp (on-disk integrity only — this does NOT prove the "
            "running process loaded them)"
        )
        return out
    out["verdict"] = MISMATCH
    out["reason"] = (
        f"MISMATCH: the stamp names {commit[:12]} but this artefact's bytes have "
        f"changed since it was built (recorded {expected}, present {actual}). The "
        "commit named above describes code that is no longer here."
    )
    return out
