"""Future-sight preflight: detect HIDDEN execution chains (B-series).

A command can look benign yet trigger arbitrary downstream execution —
package-manager install/run scripts, build files (Makefile/Gradle), test/
task runners (pytest loads conftest.py, tox/nox build envs), CI/deploy
tools, git lifecycle hooks, inline interpreters (python -c), local scripts
(./run.sh), or shell `source`. This module classifies that risk BEFORE a
command runs, for BOTH the native and ai_run paths.

It is a DETECTOR + classifier only — pure, no execution, no side effects.
Callers map the result onto their transport:
  * native  → any hidden chain ⇒ not eligible (fall back to ai_run);
  * ai_run  → deny / confirm (freeze) / allow.

Severity:
  deny    — arbitrary/remote code exec (interpreters, downloaders, package
            installs, CI/deploy, local scripts, source).
  confirm — lifecycle that is commonly a legitimate dev action but still
            runs project-defined code (builds, test/task runners, git hooks).
  none    — no hidden chain detected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEVERITY_NONE = "none"
SEVERITY_CONFIRM = "confirm"
SEVERITY_DENY = "deny"

FAMILY_NONE = ""
FAMILY_PACKAGE = "package_manager"
FAMILY_BUILD = "build"
FAMILY_SCRIPT_RUNNER = "script_runner"
FAMILY_CI_DEPLOY = "ci_deploy"
FAMILY_WRAPPER = "wrapper_downloader"
FAMILY_INLINE = "inline_interpreter"
FAMILY_LOCAL_SCRIPT = "local_script"
FAMILY_SHELL_SOURCE = "shell_source"
FAMILY_GIT_HOOK = "git_hook_trigger"


@dataclass(frozen=True)
class LifecycleResult:
    severity: str
    family: str
    reason: str
    binary: str = ""


# download-and-run wrappers (fetch remote code then execute) → deny
_WRAPPERS = frozenset(
    {
        "npx",
        "pnpx",
        "bunx",
        "uvx",
        "pipx",
        "yarn-dlx",
    },
)
# package managers — install/run/exec all run lifecycle/project scripts
_PACKAGE = frozenset(
    {
        "npm",
        "pnpm",
        "yarn",
        "pip",
        "pip3",
        "poetry",
        "pipenv",
        "conda",
        "mamba",
        "cargo",
        "gem",
        "bundle",
        "bundler",
        "composer",
        "go",
        "dotnet",
        "nuget",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "pacman",
        "apk",
        "sbt",
        "uv",
        "rebar3",
        "mix",
        "stack",
        "cabal",
        "opam",
    },
)
# build tools — run build scripts / makefiles
_BUILD = frozenset(
    {
        "make",
        "gmake",
        "cmake",
        "meson",
        "ninja",
        "bazel",
        "buck",
        "buck2",
        "scons",
        "rake",
        "ant",
        "msbuild",
        "gradle",
        "mvn",
        "waf",
        "xmake",
    },
)
# task / test runners — run project-defined code (pytest → conftest.py)
_SCRIPT_RUNNER = frozenset(
    {
        "tox",
        "nox",
        "pytest",
        "py.test",
        "jest",
        "mocha",
        "vitest",
        "ava",
        "karma",
        "gulp",
        "grunt",
        "just",
        "task",
        "invoke",
        "inv",
        "mage",
        "doit",
        "pre-commit",
        "robot",
        "behave",
        "cucumber",
        "rspec",
        "phpunit",
        "ctest",
    },
)
# CI / deploy / infra — apply remote/system state
_CI_DEPLOY = frozenset(
    {
        "terraform",
        "tofu",
        "ansible",
        "ansible-playbook",
        "helm",
        "kubectl",
        "docker",
        "docker-compose",
        "podman",
        "nerdctl",
        "vagrant",
        "pulumi",
        "serverless",
        "sls",
        "dvc",
        "dbt",
        "skaffold",
        "argocd",
        "flux",
        "packer",
        "nomad",
        "kubeadm",
        "oc",
        "eksctl",
    },
)
# language interpreters — run scripts or inline code
_INTERPRETERS = frozenset(
    {
        "python",
        "python2",
        "python3",
        "node",
        "nodejs",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "rscript",
        "lua",
        "tclsh",
        "groovy",
        "scala",
        "elixir",
        "iex",
    },
)
# git subcommands that fire hooks / mutate + lifecycle
_GIT_HOOK_SUBCMDS = frozenset(
    {
        "commit",
        "merge",
        "rebase",
        "checkout",
        "switch",
        "pull",
        "push",
        "cherry-pick",
        "revert",
        "am",
        "apply",
        "clone",
        "submodule",
        "filter-branch",
        "gc",
        "fetch",
    },
)

# chain operators we split on to inspect each segment's leading binary.
_CHAIN_SPLIT = re.compile(r"[;&|\n]+|\|\||&&")


def _basename(token: str) -> str:
    t = token.strip()
    # local script (./x, ../x, x/y as a relative exec) handled by caller;
    # here strip a path to get the program name.
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    t = t.removesuffix(".exe")
    return t.lower()


def _segment_result(seg: str) -> LifecycleResult | None:
    toks = seg.split()
    if not toks:
        return None
    first = toks[0]
    low_first = first.lower()

    # local script execution: ./run.sh, ../x, bin/x (relative w/ slash and
    # not an absolute system path)
    if low_first.startswith("./") or low_first.startswith("../"):
        return LifecycleResult(SEVERITY_DENY, FAMILY_LOCAL_SCRIPT, "executes a local script", first)

    # shell source / dot-include
    if low_first in ("source", "."):
        return LifecycleResult(
            SEVERITY_DENY,
            FAMILY_SHELL_SOURCE,
            "sources a shell script",
            low_first,
        )

    binary = _basename(first)

    if binary in _WRAPPERS:
        return LifecycleResult(
            SEVERITY_DENY,
            FAMILY_WRAPPER,
            f"{binary} downloads and runs remote code",
            binary,
        )
    if binary in _INTERPRETERS:
        return LifecycleResult(
            SEVERITY_DENY,
            FAMILY_INLINE,
            f"{binary} runs a script / inline code",
            binary,
        )
    if binary in _PACKAGE:
        return LifecycleResult(
            SEVERITY_DENY,
            FAMILY_PACKAGE,
            f"{binary} runs package/lifecycle scripts",
            binary,
        )
    if binary in _CI_DEPLOY:
        return LifecycleResult(
            SEVERITY_DENY,
            FAMILY_CI_DEPLOY,
            f"{binary} applies CI/deploy/infra state",
            binary,
        )
    if binary in _BUILD:
        return LifecycleResult(
            SEVERITY_CONFIRM,
            FAMILY_BUILD,
            f"{binary} runs a build script",
            binary,
        )
    if binary in _SCRIPT_RUNNER:
        return LifecycleResult(
            SEVERITY_CONFIRM,
            FAMILY_SCRIPT_RUNNER,
            f"{binary} runs project-defined code",
            binary,
        )
    if binary == "git":
        sub = ""
        for t in toks[1:]:
            if not t.startswith("-"):
                sub = t.lower()
                break
        if sub in _GIT_HOOK_SUBCMDS:
            return LifecycleResult(
                SEVERITY_CONFIRM,
                FAMILY_GIT_HOOK,
                f"git {sub} can fire repo hooks",
                binary,
            )
    return None


ACTION_PROCEED = "proceed"
ACTION_DENY = "deny"
ACTION_CONFIRM = "confirm"


def evaluate_lifecycle(command: str, *, enforce: bool) -> tuple[str, LifecycleResult]:
    """Map a command to a transport-agnostic action + its classification.
    When ``enforce`` is False (observe mode), always PROCEED (callers still
    audit the classification); when True, deny/confirm per severity.
    """
    lc = classify_execution_chain(command)
    if lc.severity == SEVERITY_NONE or not enforce:
        return ACTION_PROCEED, lc
    if lc.severity == SEVERITY_DENY:
        return ACTION_DENY, lc
    return ACTION_CONFIRM, lc


@dataclass(slots=True)
class PreflightVerdict:
    """Result of the shared strict-lifecycle preflight authority — consumed
    identically by native ShellPolicy and direct MCP ai_run."""

    action: str  # proceed / deny / confirm
    family: str
    reason: str
    severity: str
    name_severity: str
    xray_severity: str
    xray_failed: bool
    binary: str


def lifecycle_preflight(
    command: str,
    *,
    project_root,
    enforce: bool,
    hub=None,
    session_id: str = "",
):
    """ONE shared strict-lifecycle preflight authority for BOTH transports.

    Name-based classification (``classify_execution_chain``) + manifest X-RAY
    evidence (``shell_xray.expand_execution_graph`` / ``preflight_severity``,
    which escalates but never weakens the name floor and fails CLOSED under
    enforcement) + a ``future_sight_preflight`` AUDIT (when a hub is given;
    observe mode still audits the hidden graph). Returns a transport-agnostic
    ``PreflightVerdict``; the caller renders a ``confirm`` as its own
    exactly-once freeze (ai_run → freeze_service; native → DECISION_CONFIRMABLE
    → ShellEnforcement's single freeze minter). Neither transport is weaker:
    both see the same combined severity, the same x-ray evidence, and the same
    audit.
    """
    import hashlib

    lc = classify_execution_chain(command)
    xray = None
    xray_failed = False
    severity = lc.severity
    try:
        from .shell_xray import expand_execution_graph, preflight_severity

        try:
            xray = expand_execution_graph(command, project_root)
        except Exception:
            xray = None
        severity, xray_failed = preflight_severity(lc.severity, xray, enforce=enforce)
    except Exception:
        # X-ray machinery UNAVAILABLE (import error / validator load failure):
        # this is an INCOMPLETE inspection, not a clean pass. Route it through
        # the SAME fail-closed law (preflight_severity with xray=None) so under
        # enforcement an otherwise none/confirm verdict escalates to confirm and
        # a name DENY is preserved. If even preflight_severity cannot be
        # imported, escalate manually — never fail open, never silently proceed.
        xray_failed = True
        try:
            from .shell_xray import preflight_severity as _ps

            severity, xray_failed = _ps(lc.severity, None, enforce=enforce)
        except Exception:
            severity = lc.severity
            if enforce and severity != SEVERITY_DENY:
                severity = SEVERITY_CONFIRM
    name_sev = lc.severity
    xray_sev = getattr(xray, "severity", SEVERITY_NONE) if xray is not None else SEVERITY_NONE
    fam = lc.family or ("inspection_incomplete" if xray_failed else "package_xray")
    reason = lc.reason or (
        "x-ray inspection could not complete"
        if xray_failed
        else "hidden execution graph from manifests"
    )
    if severity == SEVERITY_NONE or not enforce:
        action = ACTION_PROCEED
    elif severity == SEVERITY_DENY:
        action = ACTION_DENY
    else:
        action = ACTION_CONFIRM
    # Audit parity: both transports emit the SAME future_sight_preflight event
    # when a hub is available. Observe mode ALWAYS audits the hidden graph.
    if hub is not None and severity != SEVERITY_NONE:
        try:
            graph_nodes = (
                [{"kind": n.kind, "label": n.label} for n in xray.nodes]
                if xray is not None
                else []
            )
            hub.execution.record_event(
                project_root,
                event_kind="future_sight_preflight",
                source_kind="lifecycle_preflight",
                session_id=session_id or None,
                capability_name="shell",
                action_kind="preflight",
                target_entity="shell_lifecycle",
                status=("enforced" if enforce else "observed"),
                payload={
                    "family": fam,
                    "name_severity": name_sev,
                    "xray_severity": xray_sev,
                    "severity": severity,
                    "xray_failed": xray_failed,
                    "binary": lc.binary,
                    "command_hash": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                    "enforced": enforce,
                    "execution_graph": graph_nodes,
                    "manifests": (list(xray.manifests) if xray is not None else []),
                },
            )
        except Exception:
            pass
    return PreflightVerdict(action, fam, reason, severity, name_sev, xray_sev, xray_failed, lc.binary)


_SEV_RANK = {SEVERITY_NONE: 0, SEVERITY_CONFIRM: 1, SEVERITY_DENY: 2}


def classify_execution_chain(command: str) -> LifecycleResult:
    """Classify the worst hidden-execution risk across all chain segments.
    Pure; no execution.
    """
    c = (command or "").strip()
    if not c:
        return LifecycleResult(SEVERITY_NONE, FAMILY_NONE, "")
    worst: LifecycleResult | None = None
    for seg in _CHAIN_SPLIT.split(c):
        seg = seg.strip()
        if not seg:
            continue
        r = _segment_result(seg)
        if r is None:
            continue
        if worst is None or _SEV_RANK[r.severity] > _SEV_RANK[worst.severity]:
            worst = r
    if worst is None:
        return LifecycleResult(SEVERITY_NONE, FAMILY_NONE, "")
    return worst
