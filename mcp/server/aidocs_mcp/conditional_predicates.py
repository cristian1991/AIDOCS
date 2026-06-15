"""Closed-set predicates for `if <predicate> then <action>` workflow
rule guards — Layer 3 workflow-extension slice.

The full grammar change (`_compile_rule` extension) is a Phase 2 task
that touches WorkflowActionService; this module lands the predicate
vocabulary so the grammar work can compose against a stable contract.

Every predicate is a PURE FUNCTION over a WorkflowContext dict. No
side effects, no network, no filesystem writes. Reads are allowed
(stat, env var lookup, git state read) so predicates can answer
"does X exist?" cleanly.

Closed set on purpose: arbitrary predicate registration would invite
unbounded complexity and make rule files impossible to audit. Operators
add predicates via PR, not config.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WorkflowContext = dict[str, Any]


@dataclass(frozen=True)
class PredicateSpec:
    """One predicate. ``name`` is the token used in rule grammar; ``fn``
    is the pure function; ``args_help`` documents the expected args for
    the rule parser.
    """

    name: str
    fn: Callable[[WorkflowContext, tuple[str, ...]], bool]
    args_help: str


def _file_exists(ctx: WorkflowContext, args: tuple[str, ...]) -> bool:
    if not args:
        return False
    project_root = ctx.get("project_root")
    target = Path(args[0])
    if project_root and not target.is_absolute():
        target = Path(project_root) / target
    try:
        return target.is_file()
    except OSError:
        return False


def _path_matches(ctx: WorkflowContext, args: tuple[str, ...]) -> bool:
    """True when ctx['path'] matches the supplied fnmatch glob.

    Used in rules like `if path_matches(tests/*.py) then run_tests`.
    """
    if not args:
        return False
    path = str(ctx.get("path") or "").replace("\\", "/")
    if not path:
        return False
    return fnmatch.fnmatch(path, args[0])


def _env_var_set(ctx: WorkflowContext, args: tuple[str, ...]) -> bool:
    """True when the named env var is set AND non-empty. Empty-string
    env vars are treated as unset per convention.
    """
    if not args:
        return False
    return bool(os.environ.get(args[0], "").strip())


def _git_clean(ctx: WorkflowContext, args: tuple[str, ...]) -> bool:
    """True when project_root is the toplevel of a clean git checkout,
    OR not a git repo at all.

    The `git-toplevel` probe checks whether project_root is actually
    the repo root — not just a subdir inside a larger repo. Without
    this check, a `pytest .pytest-tmp/test_x` living inside the
    AIDOCS git tree would falsely report dirty because the parent's
    untracked .pytest-tmp shows up in status.
    """
    project_root = ctx.get("project_root")
    if not project_root:
        return True
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return True
    if toplevel.returncode != 0:
        return True
    # project_root must be the repo root — subdirs of an unrelated
    # parent repo count as "non-git" for our purposes.
    try:
        resolved_root = Path(project_root).resolve()
        resolved_top = Path(toplevel.stdout.strip()).resolve()
        if resolved_root != resolved_top:
            return True
    except Exception:
        return True
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return True
    if result.returncode != 0:
        return True
    return not result.stdout.strip()


def _last_action_succeeded(ctx: WorkflowContext, args: tuple[str, ...]) -> bool:
    """True when ctx['last_action_exit_code'] is 0 (the chain's previous
    step succeeded). Missing key treated as "no prior action" → True so
    the first action in a chain can still run.
    """
    code = ctx.get("last_action_exit_code")
    if code is None:
        return True
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return False


_PREDICATES: dict[str, PredicateSpec] = {
    "file_exists": PredicateSpec(
        "file_exists",
        _file_exists,
        "file_exists(<relative-or-absolute-path>)",
    ),
    "path_matches": PredicateSpec(
        "path_matches",
        _path_matches,
        "path_matches(<fnmatch-glob>)",
    ),
    "env_var_set": PredicateSpec(
        "env_var_set",
        _env_var_set,
        "env_var_set(<ENV_NAME>)",
    ),
    "git_clean": PredicateSpec(
        "git_clean",
        _git_clean,
        "git_clean()",
    ),
    "last_action_succeeded": PredicateSpec(
        "last_action_succeeded",
        _last_action_succeeded,
        "last_action_succeeded()",
    ),
}


def known_predicate_names() -> tuple[str, ...]:
    """Stable contract for the rule parser + dashboard help text."""
    return tuple(sorted(_PREDICATES))


def evaluate_predicate(
    name: str,
    args: tuple[str, ...],
    ctx: WorkflowContext,
) -> bool:
    """Evaluate one predicate against a context. Unknown predicate →
    False (conservative default — unknown guards never authorize an
    action). Predicate exceptions also resolve to False for safety.
    """
    spec = _PREDICATES.get(str(name or "").strip().lower())
    if spec is None:
        return False
    try:
        return bool(spec.fn(ctx, tuple(args or ())))
    except Exception:
        return False


def predicate_help() -> dict[str, str]:
    """Map name → args_help for dashboards / docs generation."""
    return {spec.name: spec.args_help for spec in _PREDICATES.values()}
