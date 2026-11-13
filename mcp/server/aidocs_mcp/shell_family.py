"""Command-family classification for governed native Bash.

DEMOTED 2026-06-05 — TELEMETRY + CAPABILITY-ROUTING EVIDENCE, NOT an
authorization gate. Native execution is the normal governed shell surface:
the canonical ai_run LAW (ShellPolicy + bash_policy + judge + anti-coup +
lifecycle/future-sight) decides allow / freeze / deny, and an
ACTION_EXECUTE_NATIVE verdict already means the command is permitted. This
module only classifies a command into a FAMILY (read / write / build / test
/ package / network / unknown) so the adapter can pick the right EXECUTION
SURFACE and so every native decision is explained by a stable family
vocabulary in the audit/receipt.

Capability-routing role (shell_adapter), NOT authorization:
  * READ (validated, bounded) → native-eligible surface (host-replaceable
    output is provable).
  * WRITE → native only with a host-EXPOSED strong identity contract; a
    path-silent host is bound read-only, so its writes route to ai_run.
  * build / test / package / network → governed ai_run (long-runners would
    wedge a detach_supported=false host; all are governed upstream too).
  * unknown / unclassifiable → fail closed (ai_run fallback).

The disposition here is retained as legacy evidence; the adapter no longer
treats it as a second allow/deny gate (the law already decided).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# families
FAMILY_READ = "read"
FAMILY_WRITE = "write"
FAMILY_BUILD = "build"
FAMILY_TEST = "test"
FAMILY_PACKAGE = "package"
FAMILY_NETWORK = "network"
FAMILY_UNKNOWN = "unknown"

# native dispositions
DISP_NATIVE_ELIGIBLE = "native_eligible"  # may run native if all gates pass
DISP_FALLBACK = "fallback_to_ai_run"  # governed → ai_run (fail closed)


@dataclass(frozen=True)
class FamilyDisposition:
    family: str
    disposition: str
    reason: str


_CHAIN_SPLIT = re.compile(r"[;&|\n]+|\|\||&&")
_BINARY_EXTS = (".exe", ".cmd", ".bat", ".ps1")

# write-effecting binaries (mutate the filesystem / permissions)
_WRITE_BINARIES = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "touch",
        "mkdir",
        "tee",
        "dd",
        "install",
        "ln",
        "truncate",
        "chmod",
        "chown",
        "chgrp",
        "shred",
        "unlink",
        "set-content",
        "add-content",
        "out-file",
        "new-item",
        "remove-item",
        "copy-item",
        "move-item",
    },
)
# network-effecting binaries (egress / remote)
_NETWORK_BINARIES = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "telnet",
        "socat",
        "invoke-webrequest",
        "invoke-restmethod",
    },
)
# build / test / package runners (defence in depth — usually already
# deny/confirm via the lifecycle x-ray before we get here)
_BUILD_BINARIES = frozenset(
    {
        "make",
        "gmake",
        "cmake",
        "ninja",
        "bazel",
        "gradle",
        "mvn",
        "rake",
        "msbuild",
        "meson",
        "scons",
        "ant",
        "cargo",
        "go",
        "dotnet",
        "tsc",
        "webpack",
        "vite",
        "rollup",
    },
)
_TEST_BINARIES = frozenset(
    {
        "pytest",
        "tox",
        "nox",
        "jest",
        "mocha",
        "vitest",
        "phpunit",
        "rspec",
        "ctest",
        "gradle",
        "go",  # go test / gradle test — coarse
    },
)
_PACKAGE_BINARIES = frozenset(
    {
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "npx",
        "pip",
        "pip3",
        "pipx",
        "poetry",
        "uv",
        "pipenv",
        "gem",
        "bundle",
        "bundler",
        "composer",
        "cargo",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "brew",
        "pacman",
        "apk",
        "nuget",
    },
)


def _normalize_binary(token: str) -> str:
    b = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    for ext in _BINARY_EXTS:
        if b.endswith(ext):
            return b[: -len(ext)]
    return b


def _segment_family(seg: str) -> str:
    toks = seg.strip().split()
    if not toks:
        return FAMILY_UNKNOWN
    binary = _normalize_binary(toks[0])
    args = [a.lower() for a in toks[1:]]
    low = seg.lower()
    # network first (highest egress risk)
    if binary in _NETWORK_BINARIES:
        return FAMILY_NETWORK
    if binary == "git" and any(a in ("push", "fetch", "pull", "clone", "remote") for a in args):
        return FAMILY_NETWORK
    # package managers
    if binary in _PACKAGE_BINARIES:
        return FAMILY_PACKAGE
    # test runners (before build, pytest etc.)
    if binary in _TEST_BINARIES and (
        binary in ("pytest", "tox", "nox", "jest", "mocha", "vitest", "phpunit", "rspec", "ctest")
        or "test" in args
    ):
        return FAMILY_TEST
    if binary in _BUILD_BINARIES:
        return FAMILY_BUILD
    # writes: redirection or a write-effecting binary
    if ">" in low or binary in _WRITE_BINARIES:
        return FAMILY_WRITE
    return FAMILY_READ  # provisional — validated separately


def classify_family(command: str) -> str:
    """Worst (most powerful) family across all chain segments. A chain is
    only as safe as its most powerful segment.
    """
    if not command or not command.strip():
        return FAMILY_UNKNOWN
    order = {
        FAMILY_NETWORK: 6,
        FAMILY_PACKAGE: 5,
        FAMILY_BUILD: 4,
        FAMILY_TEST: 3,
        FAMILY_WRITE: 2,
        FAMILY_READ: 1,
        FAMILY_UNKNOWN: 0,
    }
    worst = FAMILY_UNKNOWN
    saw_segment = False
    for seg in _CHAIN_SPLIT.split(command):
        if not seg.strip():
            continue
        saw_segment = True
        fam = _segment_family(seg)
        if order[fam] > order[worst]:
            worst = fam
    # an all-read chain reports READ; an empty/odd parse reports UNKNOWN.
    if not saw_segment:
        return FAMILY_UNKNOWN
    # never let a READ-floor mask: if worst stayed UNKNOWN but we saw
    # segments, treat as UNKNOWN (fail closed).
    return worst


def native_family_disposition(
    project_root,
    command: str,
    *,
    read_validated: bool,
) -> FamilyDisposition:
    """The family-level native disposition.

    ``read_validated`` is the caller's authoritative read-only catalog
    result (shell_readonly): only a validated, bounded read is native-
    eligible. Everything else — including an UNGUARDABLE read the catalog
    rejected — fails closed to ai_run.
    """
    try:
        family = classify_family(command)
    except Exception:
        return FamilyDisposition(
            FAMILY_UNKNOWN,
            DISP_FALLBACK,
            "family classification failed; failing closed to ai_run",
        )
    if family == FAMILY_READ:
        if read_validated:
            return FamilyDisposition(
                FAMILY_READ,
                DISP_NATIVE_ELIGIBLE,
                "validated bounded read-only command",
            )
        return FamilyDisposition(
            FAMILY_READ,
            DISP_FALLBACK,
            "read command not in the validated bounded read-only catalog "
            "(unguardable) — fail closed to ai_run",
        )
    if family in (FAMILY_PACKAGE, FAMILY_BUILD, FAMILY_TEST, FAMILY_NETWORK):
        return FamilyDisposition(
            family,
            DISP_FALLBACK,
            f"{family} flow is governed (future-sight x-ray + anti-coup); "
            "native execution is not unlocked — routing to ai_run",
        )
    if family == FAMILY_WRITE:
        return FamilyDisposition(
            FAMILY_WRITE,
            DISP_FALLBACK,
            "write flow is not unlocked for naked native execution — "
            "routing to ai_run (audited, receipted)",
        )
    return FamilyDisposition(
        FAMILY_UNKNOWN,
        DISP_FALLBACK,
        "unclassifiable command — fail closed to ai_run",
    )
