"""THE one resolver for `distribution.flavor` (#953).

The catalog entry has always promised (config_schema.py:1561, :1571-1573):

    "`dev` is install-path-locked — code running from site-packages refuses to
     self-elevate to `dev` regardless of this setting."

NOTHING ENFORCED IT. Measured 2026-08-28 under the runtime interpreter — a
site-packages install, the exact condition that sentence names — all three
production readers returned 'dev':

    operator_auth_service._auth_status_flavor()    'dev'
    project_authority._flavor()                    'dev'
    shell_resolver._read_flavor(root)              'dev'

Each was a bare ``get_setting("distribution.flavor")`` with a fail-closed
except branch and no path check, and the promise existed ONLY as those
description strings — no function, no call site, no test. #840 in its purest
form: prose that becomes the next reader's false premise.

WHY IT COST NOTHING UNTIL NOW. Flavour grants nothing today — project_authority
._flavor() says so outright: "Display/audit metadata ONLY — flavor grants
nothing (#404: the local-admin passthrough is excised)." An unenforced lock on a
claim that authorises nothing is harmless. #952's design ends that: dev flavour
becomes the right to ATTEST LOCAL SOURCE as the enforcement runtime, the first
grant flavour has ever carried. From then on a single global config row would
let a production install bless arbitrary local bytes as the code governing its
own gate. This module is that design's prerequisite and lands before it.

#404 IS NOT REVERSED HERE, and the distinction matters enough to state: #404
excised flavour as authority over PRINCIPALS — who a caller is, what a caller
may do. That resolves from the authenticated principal via project_authority +
RBAC and is untouched. This module concerns what an INSTALL may claim to BE.
Different axis.

THE DISCRIMINATOR IS WHERE THE RUNNING PACKAGE RESOLVES FROM. Never "does this
machine have a checkout somewhere" — that is the question runtime_refresh's axis
selection asks (:134), and asking it is exactly why a signed-release install on
a box that merely HAS a dev tree got judged against that tree (#952). Presence
is not provenance. Here the only input is ``aidocs_mcp.__file__``: the location
of the code that is actually executing.

RECOGNITION IS POSITIVE, so the failure mode is demotion. `dev` is honoured only
when the layout is recognised AS a source checkout (``<repo>/mcp/server/
aidocs_mcp``). "Not obviously site-packages" is NOT a source checkout — a copy
dropped in /tmp would otherwise carry the dev grant.

DEMOTION, NOT REFUSAL. A `dev` row on an installed box yields ``DEMOTED_FLAVOR``
and keeps working as an ordinary install. It must not raise: a lock that bricks
a machine the first time it fires gets reverted, and then the security property
is gone permanently. It grants nothing extra either — DEMOTED_FLAVOR is the
catalog default, i.e. what a plain pip install already is, so demotion hands
back no authority a config writer did not already have. What it removes is dev's
EXTRA reach.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath

DEV = "dev"
SOLO = "solo"
CORPO = "corpo"

#: What a `dev` claim becomes when the running code is an installed package.
#: The catalog default — an ordinary single-operator install — chosen so the
#: demotion is a no-op for everyone except a self-elevation attempt.
DEMOTED_FLAVOR = SOLO

#: Directory names that mark an installed (wheel) layout. Checked explicitly in
#: addition to the positive test below: belt and braces, because this is the
#: exact layout that was measured claiming `dev`.
_INSTALLED_MARKERS = frozenset({"site-packages", "dist-packages"})


def is_source_checkout(pkg_dir: PurePath) -> bool:
    """Is ``pkg_dir`` the aidocs_mcp package of a SOURCE CHECKOUT?

    Pure — a function of the path alone, so it is testable against both
    layouts without touching an interpreter, a venv or the filesystem. That
    testability is the point: the reason the promise shipped unbuilt is that
    nothing ever asked this question from a site-packages process.

    TAKES ``PurePath``, NOT ``Path``, and the distinction is load-bearing for
    the tests rather than for production. ``Path`` is the HOST's flavour, so a
    Windows-style literal evaluated on Linux is not a path at all — the
    backslashes are ordinary filename characters and the whole string collapses
    to a single component. A cross-platform case must therefore name its
    flavour (``PureWindowsPath`` / ``PurePosixPath``) explicitly; accepting
    PurePath is what lets it. Measured the hard way: deploy dev-1377 failed
    Gate 2b on exactly that, green on Windows and red on the VPS.

    The repo layout is ``<repo>/mcp/server/aidocs_mcp`` (the same anchor
    host_services/path_resolver_service.py documents at :30-31), so BOTH
    ancestors are required. 'server' alone is a near-miss that must not pass.
    """
    try:
        parents = [p.name.lower() for p in pkg_dir.parents]
    except Exception:
        return False
    if _INSTALLED_MARKERS & set(parents):
        return False
    parent = pkg_dir.parent
    return parent.name == "server" and parent.parent.name == "mcp"


def running_from_source_checkout() -> bool:
    """Does the CURRENTLY EXECUTING aidocs_mcp live in a source checkout?

    Fail-closed: any failure to resolve our own location answers False, so an
    unanswerable question demotes rather than elevating. Unknown is not a pass.
    """
    try:
        return is_source_checkout(Path(__file__).resolve().parent)
    except Exception:
        return False


def _configured_flavor(project_root: Path | None = None) -> str:
    """The raw configured value. Seam for tests; never call this directly from
    production code — ``effective_flavor`` is the only honest reading."""
    from .config import get_setting

    raw = get_setting(
        "distribution.flavor",
        project_root=project_root,
        default=SOLO,
    )
    return str(raw or SOLO).strip().lower()


def effective_flavor(project_root: Path | None = None, *, on_error: str) -> str:
    """The install's flavour, with the `dev` claim path-locked.

    ``on_error`` is the caller's own fail-closed value and is REQUIRED — no
    default. The three original readers fail closed differently
    (operator_auth_service and project_authority to 'corpo', shell_resolver to
    'solo'); unifying the LOCK must not quietly unify the POSTURE, which is a
    separate behavioural change with its own blast radius. Making it required
    means a new caller has to state its posture rather than inherit one.
    """
    try:
        flavor = _configured_flavor(project_root)
    except Exception:
        return on_error
    if flavor != DEV:
        return flavor
    if running_from_source_checkout():
        return DEV
    _announce_demotion()
    return DEMOTED_FLAVOR


def _announce_demotion() -> None:
    """Leave a trail. A silent demotion is indistinguishable from a config that
    never said `dev`, and the operator needs to be able to tell "my dev box is
    misdetected" from "someone tried to elevate this install". Best-effort and
    never load-bearing: an audit failure must not decide a flavour.
    """
    if os.environ.get("AIDOCS_FLAVOR_DEMOTION_QUIET"):
        return
    try:
        from .aidocs_logging import get_logger

        get_logger(__name__).warning(
            "distribution.flavor='dev' DEMOTED to %r: the running aidocs_mcp "
            "resolves to %s, which is not a source checkout. `dev` is "
            "install-path-locked (#953) — an installed package cannot "
            "self-elevate. If this IS a contributor box, it is running the "
            "INSTALLED package rather than the checkout.",
            DEMOTED_FLAVOR,
            Path(__file__).resolve().parent,
        )
    except Exception:
        pass
