"""FETCH THE BUILD THE AUTHORITY SERVES. The missing link of the updater (#903).

WHAT WAS MISSING, measured on the operator's box 2026-08-24. The campaign's
premise was "everything is built, just wire the producer to the consumer". Four
links of five were true. The chain is: AIDOCS pulls -> verifies -> installs ->
restarts itself.

    decide   check_build_axis          WORKS (current=188, latest=189)
    ask      request_runtime_refresh   WORKS
    PULL     -                         DID NOT EXIST, ON EITHER SIDE
    verify   release_trust.verify_release  exists, over a tree ALREADY ON DISK
    install  runtime_provisioner       exists, FROM A LOCAL TREE
    restart  the watchdog              WORKS

`build_authority` fetched only `/v1/version` -- the axes. The authority
published WHAT BUILD IT IS and never THE BUILD. So the updater was a decision
with no mechanism: it could say "you are behind" and had nothing to install
from.

THE OBSERVED CONSEQUENCE, and why this is not a theoretical gap. The operator ran
`aidocs runtime --fix` while the authority served build 189. It reported
"OK ... owned=True verified=True" and installed NOTHING, because its reference
was `C:/Users/.../Temp/aidocs_ship_stage_79/mcp` -- A LEFTOVER TEMP SHIP-STAGE
FROM AN EARLIER DEPLOY, itself at build 188. It compared the install against a
stale tree it happened to find, saw them equal, and declared success. Clear TEMP
and the reference vanishes entirely. That is the
"freshness-by-byte-diff-of-two-trees" this campaign was ordered to retire: the
COMPARISON is now a build/stamp comparison against the authority, but the INSTALL
SOURCE was still whatever tree turned up on disk.

WHAT THIS MODULE IS, AND WHAT IT REFUSES TO BE. It fetches an archive and returns
a VERIFIED package directory, or it raises. It is deliberately the smallest
possible surface, because what it feeds is INSTALLING REMOTE CODE INTO THE
PACKAGE THAT ENFORCES:

  * It NEVER returns an unverified tree. `verify_release` is the existing gate
    (signature + signed manifest + per-file digests); this module does not grow a
    second opinion beside it (doctrine XXII), it hands the extracted tree over
    and refuses on anything but ok=True.
  * It NEVER installs. Extraction and installation are different authorities; the
    provisioner owns the second one, and it already refuses raw pip and
    --force-reinstall (#557).
  * It extracts with `filter="data"` and re-checks every member itself. A tar can
    name `../../` or an absolute path or a symlink pointing anywhere, and an
    updater that unpacks a hostile archive into a home directory is a worse bug
    than the one it fixes.
  * It fails CLOSED on every doubt -- unreachable, wrong status, oversized,
    malformed, unverifiable. Unknown is not a pass, and here "unknown" would mean
    executing someone else's code.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

#: The wire contract served by GET /v1/build (outer_gate_transport).
BUILD_ARTIFACT_SCHEMA = "aidocs-build-artifact/1"

#: The route that serves the gate's OWN running build — the private dev channel.
#:
#: NAMED FOR WHAT IT SERVES (#903, corrected). This was "/v1/release", which was
#: wrong and dangerously so: the gate serves the build it is RUNNING, i.e. the
#: DEPLOYED axis, not a blessed public release. On the operator's own gate the
#: two happen to be identical — the "release manifest" there is the signature
#: over the deployed tree — which is exactly why the misnomer went unnoticed. A
#: public client asking for "the release" would have silently received whatever
#: was deployed last. The public channel is the signed, tagged release published
#: to the release channel; this is not it.
BUILD_PATH = "/v1/build"

#: A release tree is a few MB. A cap that a legitimate artefact never reaches is
#: the cheapest defence against a decompression bomb aimed at an updater that
#: runs unattended on every install.
MAX_ARCHIVE_BYTES = 96 * 1024 * 1024
MAX_UNPACKED_BYTES = 384 * 1024 * 1024

#: Longer than the version probe's 4s: this is an artefact, not a header, and it
#: runs in the watchdog rather than in an interactive tool.
FETCH_TIMEOUT_S = 120.0


class PullRefused(Exception):
    """No trustworthy artefact was obtained. NEVER raised with a tree in hand."""


def _http_get(url: str, timeout: float) -> tuple[int, bytes]:
    """Seam over urllib (tests replace it). Egress is governed, like every fetch.

    GATED THE SAME WAY AS build_authority, deliberately: same host, same shape.
    `assert_egress_allowed` is keyword-only on `purpose` and `allow_hosts`, and
    the allowlist is derived FROM THE URL WE WERE ASKED TO FETCH -- so this gate
    proves the request is going where the caller said, and cannot silently widen
    to a second host.

    MEASURED 2026-08-25: the first version called `assert_egress_allowed(url)`
    with neither argument, which is a TypeError on every real fetch. It survived
    the whole test suite AND a mutation gate because every test injects a fake
    `http` callable, so this function -- the only code here that touches the
    network -- was never once executed. It failed on the first live pull. A seam
    that exists to be replaced in tests is a seam nothing tests.
    """
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen

    from .governed_egress import assert_egress_allowed

    assert_egress_allowed(
        url,
        purpose="release_pull",
        allow_hosts=[urlparse(url).hostname or ""],
    )
    req = Request(  # noqa: S310 - host allowlisted by the egress gate above
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "aidocs-release-pull"},
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - same
        return int(getattr(resp, "status", 200) or 200), resp.read(MAX_ARCHIVE_BYTES + 1)


def _safe_members(tar: tarfile.TarFile, dest: Path):
    """Yield members that unpack INSIDE dest and carry no surprises.

    `filter="data"` already rejects most of this on 3.12+, and this re-checks it
    anyway. The two are not redundant: the filter is a Python-version-dependent
    default and this is the invariant the module promises. A tar member is
    attacker-controlled input in exactly the same way a request body is.
    """
    root = dest.resolve()
    total = 0
    for m in tar.getmembers():
        if m.issym() or m.islnk():
            raise PullRefused(f"archive contains a link member: {m.name!r}")
        if not (m.isfile() or m.isdir()):
            raise PullRefused(f"archive contains a non-regular member: {m.name!r}")
        name = m.name.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise PullRefused(f"archive member escapes the destination: {m.name!r}")
        target = (root / name).resolve()
        if not str(target).startswith(str(root)):
            raise PullRefused(f"archive member resolves outside the destination: {m.name!r}")
        total += int(getattr(m, "size", 0) or 0)
        if total > MAX_UNPACKED_BYTES:
            raise PullRefused("archive unpacks to more than the permitted size")
        yield m


def fetch_release_archive(
    url: str, *, http=None, timeout: float = FETCH_TIMEOUT_S
) -> tuple[bytes, dict]:
    """The archive bytes and the build the server SAYS they are, or raise.

    THE WIRE IS JSON, NOT THE RAW TAR (measured in production 2026-08-24). The
    first version served the gzip as the HTTP body and every request came back
    502: the gate's `TransportResponse.raw_body` is typed `str | None` and its
    webapp path 404s binary extensions on purpose to keep it so. Rather than
    widen the public gate's write path to carry an artefact, the build travels
    base64 inside a JSON envelope that also NAMES ITSELF:

        {"schema": "aidocs-build-artifact/1", "build": 191,
         "commit": "...", "version": "2.5.1", "archive_b64": "..."}

    The envelope is a CLAIM, exactly like the axes at /v1/version. It is useful
    because it lets a client refuse a build it did not ask for BEFORE downloading
    meaning into it, but it decides nothing: the signature over the unpacked tree
    remains the only thing that authorises an install.
    """
    import base64

    get = http or _http_get
    try:
        status, body = get(url, timeout)
    except PullRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is one refusal
        raise PullRefused(f"release fetch failed: {type(exc).__name__}: {exc}") from exc
    if status != 200:
        raise PullRefused(f"release fetch returned HTTP {status}")
    if not body:
        raise PullRefused("release fetch returned an empty body")
    if len(body) > MAX_ARCHIVE_BYTES:
        raise PullRefused("release archive exceeds the permitted size")

    try:
        envelope = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PullRefused(f"build response is not JSON: {type(exc).__name__}") from exc
    if not isinstance(envelope, dict):
        raise PullRefused("build response is not a JSON object")
    schema = str(envelope.get("schema") or "")
    if schema != BUILD_ARTIFACT_SCHEMA:
        # An unknown schema is refused, never read as blanks: a client that
        # guesses at a shape it does not know will read absent fields as absent
        # GUARANTEES, which is how a downgrade check quietly stops checking.
        raise PullRefused(
            f"build response schema {schema or '(none)'} is not {BUILD_ARTIFACT_SCHEMA}"
        )
    raw_b64 = envelope.get("archive_b64")
    if not isinstance(raw_b64, str) or not raw_b64:
        raise PullRefused("build response carries no archive")
    try:
        archive = base64.b64decode(raw_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise PullRefused(f"build archive is not valid base64: {type(exc).__name__}") from exc
    if not archive:
        raise PullRefused("build response decoded to an empty archive")
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise PullRefused("release archive exceeds the permitted size")
    return archive, envelope


def pull_release(
    *,
    url: str,
    dest: Path | str,
    http=None,
    verify=None,
    timeout: float = FETCH_TIMEOUT_S,
    expect_commit: str | None = None,
    min_build: int | None = None,
) -> Path:
    """Fetch, unpack and VERIFY. Returns the package dir, or raises PullRefused.

    The returned path is the `aidocs_mcp` directory of a release whose signature
    and signed manifest both check out -- exactly what the provisioner wants as
    its `--package`/`--reference`, and never anything less.

    AUTHENTIC IS NOT THE SAME AS CORRECT (#903). The first version of this
    checked only that the artefact was genuinely signed, which a VALIDLY SIGNED
    OLDER BUILD also is. Anything able to serve a stale-but-genuine artefact
    could therefore pin an install to it forever, and the updater would report
    success every time. Two separate guards, because they stop two separate
    things:

      ``expect_commit``  IDENTITY -- is this the build the axis NAMED? Catches a
                         swap between "the authority says 189" and "here are the
                         bytes", including a cache or mirror serving something
                         else that is nonetheless properly signed.
      ``min_build``      MONOTONICITY -- never move BACKWARDS. This is the guard
                         that survives an authority which has itself been made to
                         name an old build: identity would agree, and this still
                         refuses. Pass the currently installed build.

    A build number that cannot be read while ``min_build`` is set is a REFUSAL,
    not a pass: an artefact that cannot say which build it is cannot prove it is
    not a downgrade.
    """
    dest = Path(dest)
    body, envelope = fetch_release_archive(url, http=http, timeout=timeout)

    # THE CHEAP CHECK FIRST, on the server's own claim. This is not the
    # authority — the signature is — but refusing a build we did not ask for
    # before unpacking it costs nothing and narrows what ever reaches the disk.
    claimed = str(envelope.get("commit") or "")
    if expect_commit and claimed and claimed != str(expect_commit):
        raise PullRefused(
            f"the build on offer is not the one the authority named: expected "
            f"{str(expect_commit)[:12]}…, offered {claimed[:12]}… — refused before download"
        )

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tar:
            members = list(_safe_members(tar, dest))
            try:
                tar.extractall(dest, members=members, filter="data")
            except TypeError:  # pragma: no cover - Python without the data filter
                tar.extractall(dest, members=members)  # noqa: S202 - members pre-vetted above
    except PullRefused:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        raise PullRefused(f"release archive is unreadable: {type(exc).__name__}: {exc}") from exc

    # WHAT COMES BACK IS THE PROJECT, WHAT GETS VERIFIED IS THE PACKAGE (#908).
    # Two different things, and conflating them is what made the first version
    # useless: it returned the package dir, which pip cannot install because it
    # has no pyproject.toml. The provisioner wants a PROJECT; verify_release
    # wants a PACKAGE. The archive carries both, in the layout the deploy
    # already stages and the installer already accepts.
    pkg = _locate_package(dest)
    if pkg is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise PullRefused("release archive contains no aidocs_mcp package directory")
    project = _locate_project(dest)
    if project is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise PullRefused(
            "release archive carries no pyproject.toml, so nothing in it can be "
            "INSTALLED — refusing an artefact that could only ever be verified"
        )

    checker = verify
    if checker is None:
        from .release_trust import verify_release as checker  # noqa: N813

    trust = checker(pkg)
    if not getattr(trust, "ok", False):
        shutil.rmtree(dest, ignore_errors=True)
        raise PullRefused(
            "the fetched release did not verify: "
            f"{getattr(trust, 'reason', 'no reason given')} — refusing to install it"
        )

    try:
        _check_identity(trust=trust, expect_commit=expect_commit, min_build=min_build)
    except PullRefused:
        # Same rule as an unverifiable tree: a refused artefact must not survive
        # on disk, where a later step could find it and adopt it as a source.
        shutil.rmtree(dest, ignore_errors=True)
        raise

    # #913 -- THE VENDORED HALF IS VERIFIED SEPARATELY, BECAUSE THE PACKAGE
    # SIGNATURE DOES NOT COVER IT.
    #
    # verify_release above digests the aidocs_mcp PACKAGE. If this artefact also
    # carries third_party/, those bytes passed through the same transport under
    # the same TLS and the same envelope -- and NONE of that is the signature.
    # Trusting them because the package verified would be exactly the mistake
    # this whole item is about: a signature that covers one half being read as
    # cover for both.
    #
    # So the vendored trees are compared against the AUTHENTICATED manifest's
    # own claim (trust.vendored, which verify_release reads only after checking
    # the signature). Three-valued:
    #   match     -> the archive's vendored bytes are the signed ones. Install.
    #   mismatch  -> REFUSE THE WHOLE ARTEFACT and delete it. A claim exists and
    #                the bytes disagree, which is the tampering case; unlike the
    #                SERVING side -- which withholds the unvouched half and still
    #                serves the vouched one -- a client has no reason to install
    #                half an artefact it has evidence against.
    #   unclaimed -> the release predates this axis. Nothing to check. Any
    #                vendored bytes that somehow rode along are NOT installed by
    #                the provisioner, which takes the project dir located above.
    from .vendored_contract import SIG_MISMATCH, check_vendored_signature

    vsig = check_vendored_signature(project, getattr(trust, "vendored", None))
    if vsig["state"] == SIG_MISMATCH:
        shutil.rmtree(dest, ignore_errors=True)
        raise PullRefused(vsig["reason"])

    return project


def _check_identity(*, trust, expect_commit: str | None, min_build: int | None) -> None:
    """Is this the build we were promised, and is it not a step backwards?"""
    got_commit = str(getattr(trust, "commit", "") or "")
    if expect_commit and got_commit != str(expect_commit):
        raise PullRefused(
            f"the fetched build is not the one the authority named: expected "
            f"{str(expect_commit)[:12]}…, got {got_commit[:12] or '(none)'}… — "
            "a properly signed artefact is still the WRONG artefact if it is not "
            "the one that was promised"
        )

    if min_build is None:
        return
    got_build = getattr(trust, "build_number", None)
    if not isinstance(got_build, int) or isinstance(got_build, bool):
        raise PullRefused(
            "the fetched build does not name its build number, so it cannot prove "
            f"it is not a downgrade from build {min_build} — unknown is not a pass"
        )
    if got_build < int(min_build):
        raise PullRefused(
            f"DOWNGRADE REFUSED: the fetched build {got_build} is older than the "
            f"installed build {min_build}. A validly signed OLD build is exactly "
            "what a downgrade attack looks like"
        )


def _locate_project(root: Path) -> Path | None:
    """The INSTALLABLE root inside the unpacked tree — the dir with pyproject.toml.

    Distinct from `_locate_package` on purpose: pip installs a project, and
    `release_trust.verify_release` checks a package. The artefact carries both
    and the caller needs to name which one it is asking for.
    """
    if (root / "pyproject.toml").is_file():
        return root
    for cand in sorted(root.rglob("pyproject.toml")):
        return cand.parent
    return None


def package_dir_for(project: Path) -> Path | None:
    """The package inside an installable project root, for the freshness axis."""
    return _locate_package(project)


def _locate_package(root: Path) -> Path | None:
    """The `aidocs_mcp` dir inside the unpacked tree, wherever the archive put it."""
    direct = root / "aidocs_mcp"
    if (direct / "__init__.py").is_file():
        return direct
    for cand in root.rglob("aidocs_mcp"):
        if (cand / "__init__.py").is_file():
            return cand
    return None
