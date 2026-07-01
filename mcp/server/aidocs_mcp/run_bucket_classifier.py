"""Normalize a shell command into a bucket key for duration tracking.

Purpose: group "same-shape" runs so observed durations are predictive.
`pytest tests/host` should bucket the same across -q / -v / --tb=line
variations, but NOT share a bucket with `pytest tests/security` (those
are different targets with different runtimes).

Bucket keys are pipe-delimited segments with a canonical hierarchy:

    <canonical_tool>|<scope_kind>|<scope_value>[|<suffix>]

    canonical_tool: pytest | unittest | jest | go-test | cargo-test
                    | dotnet-build | npm-build | tsc | make | cargo-build
                    | go-build | gradle | mvn | probe | bash | sh

    scope_kind: full | dir | file | test-id | module | none

    scope_value: normalized path or test-id body

    suffix: one or more flag-driven modifiers, sorted alphabetically,
            that materially change runtime (e.g. "ignore:tests/conductor"
            flips ~600 tests out of the suite). Non-semantic flags
            (-q, -v, --tb=*) are stripped.

Examples:
    pytest tests/host                    → pytest|dir|tests/host
    pytest tests/host -q                 → pytest|dir|tests/host
    pytest -q --tb=no --ignore=tests/conductor → pytest|full|-|ignore:tests/conductor
    pytest tests/host/foo.py             → pytest|file|tests/host/foo.py
    pytest tests/host/foo.py::test_bar   → pytest|test-id|tests/host/foo.py::test_bar
    dotnet build src/App/App.csproj      → dotnet-build|module|src/App/App.csproj
    python -m pytest tests/              → pytest|dir|tests
    go test ./...                        → go-test|full|-
    echo hi                              → bash|probe|-

Parent chain (for fallback when exact bucket has <3 samples):
    pytest|dir|tests/host            → pytest|dir|tests  (drops one
                                        path segment)
    pytest|dir|tests                 → pytest|full|-
    pytest|full|-                    → pytest
    pytest                           → (root)

Callers aggregate stats across siblings of a parent prefix via LIKE.

"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# Flags that are pure display/verbosity adjustments — don't affect what
# tests/files get run, so don't bucket-separate.
_NONSEMANTIC_FLAGS = {
    "-q",
    "--quiet",
    "-v",
    "--verbose",
    "-vv",
    "-vvv",
    "--no-header",
    "--no-summary",
    "--no-cov",
    "-s",  # capture=no, doesn't change selection
    "--color",
    "--no-color",
    "--color=yes",
    "--color=no",
    "-x",
    "--exitfirst",  # stops early on fail — materially changes
    # fail-case duration but caller cares about
    # the normal path. Skip for now.
    "--tb=short",
    "--tb=long",
    "--tb=line",
    "--tb=auto",
    "--tb=no",
    "--tb=native",
    "--durations=0",
    "--durations=10",
    "--durations=20",
    "--durations-min=2.0",
    "--durations-min=1.0",
    "-n",
    "auto",  # xdist — changes duration but not selection
    "--dist=loadgroup",
    "--dist=loadfile",
    "--dist=worksteal",
    "--basetemp",
    "--strict-markers",
    "-p",
    "no:cacheprovider",
    "--cache-clear",
}

# Flags that DO affect runtime by changing scope. Captured as suffix.
_SCOPE_MODIFYING_FLAGS = {
    "--ignore",  # --ignore=tests/conductor removes a whole dir
    "--deselect",
    "-k",  # -k "not slow" changes selection
    "-m",  # -m slow filters by marker
}

# Test frameworks — command regexes already live in run_output_renderer,
# but we want narrower bucket names than "test-python".
_PYTEST_RE = re.compile(r"\bpytest\b|\bpython\s+-m\s+pytest\b", re.IGNORECASE)
_UNITTEST_RE = re.compile(r"\bpython\s+-m\s+unittest\b", re.IGNORECASE)
_JEST_RE = re.compile(r"\b(jest|vitest)\b", re.IGNORECASE)
_GO_TEST_RE = re.compile(r"\bgo\s+test\b", re.IGNORECASE)
_CARGO_TEST_RE = re.compile(r"\bcargo\s+test\b", re.IGNORECASE)

_DOTNET_BUILD_RE = re.compile(r"\bdotnet\s+build\b", re.IGNORECASE)
_NPM_BUILD_RE = re.compile(r"\b(npm|yarn|pnpm|bun)\s+(run\s+)?build\b", re.IGNORECASE)
_TSC_RE = re.compile(r"\btsc\b", re.IGNORECASE)
_CARGO_BUILD_RE = re.compile(r"\bcargo\s+build\b", re.IGNORECASE)
_GO_BUILD_RE = re.compile(r"\bgo\s+build\b", re.IGNORECASE)
_MAKE_RE = re.compile(r"\bmake\b", re.IGNORECASE)
_GRADLE_RE = re.compile(r"\bgradle\b", re.IGNORECASE)
_MVN_RE = re.compile(r"\bmvn\b", re.IGNORECASE)

_PYTEST_TEST_ID_RE = re.compile(r"::")


@dataclass(frozen=True)
class BucketKey:
    """Parsed bucket key with parent-chain helpers."""

    tool: str
    scope_kind: str
    scope_value: str
    suffix: tuple[str, ...]

    def key(self) -> str:
        base = f"{self.tool}|{self.scope_kind}|{self.scope_value or '-'}"
        if self.suffix:
            base += "|" + ",".join(self.suffix)
        return base

    def parent_chain(self) -> list[str]:
        """Return progressively-broader parent bucket keys, from
        nearest to root. Use with LIKE prefix to pull sibling stats
        when the exact key has too few samples.
        """
        chain: list[str] = []
        # Drop the suffix first — same scope, unknown flag-delta.
        if self.suffix:
            chain.append(f"{self.tool}|{self.scope_kind}|{self.scope_value or '-'}")
        # For dir/file scope, peel one path segment per step.
        if self.scope_kind in ("dir", "file") and self.scope_value:
            parts = self.scope_value.replace("\\", "/").strip("/").split("/")
            for i in range(len(parts) - 1, 0, -1):
                parent_path = "/".join(parts[:i])
                chain.append(f"{self.tool}|dir|{parent_path}")
        # Then "full" scope for the tool.
        chain.append(f"{self.tool}|full|-")
        # Then tool-level.
        chain.append(self.tool)
        # Drop duplicates while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for k in chain:
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
        return out


def _tokenize(command: str) -> list[str]:
    """Split a command into tokens. Windows paths with backslashes
    confuse shlex POSIX mode (it treats `\\` as an escape) — flip to
    non-POSIX when the command looks Windows-shaped or POSIX mode
    raises. Tokens are never shell-executed here; this is for bucket-
    key computation only, so whitespace split is the acceptable
    coarse fallback.
    """
    if "\\" in command:
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()


def _normalize_path(token: str) -> str:
    """Canonicalize a path-like token. Windows → forward slashes,
    strip trailing / and ./ prefix.
    """
    t = token.replace("\\", "/")
    t = t.removeprefix("./")
    t = t.rstrip("/")
    return t


def _canonical_tool(command: str) -> str:
    if _PYTEST_RE.search(command):
        return "pytest"
    if _UNITTEST_RE.search(command):
        return "unittest"
    if _JEST_RE.search(command):
        return "jest"
    if _GO_TEST_RE.search(command):
        return "go-test"
    if _CARGO_TEST_RE.search(command):
        return "cargo-test"
    if _DOTNET_BUILD_RE.search(command):
        return "dotnet-build"
    if _NPM_BUILD_RE.search(command):
        return "npm-build"
    if _TSC_RE.search(command):
        return "tsc"
    if _CARGO_BUILD_RE.search(command):
        return "cargo-build"
    if _GO_BUILD_RE.search(command):
        return "go-build"
    if _MAKE_RE.search(command):
        return "make"
    if _GRADLE_RE.search(command):
        return "gradle"
    if _MVN_RE.search(command):
        return "mvn"
    # Probe / shell fallback. Use first token as a coarse bucket so
    # `python -c` runs don't all collide with `git status` runs.
    tokens = _tokenize(command)
    if not tokens:
        return "probe"
    head = tokens[0].lower()
    if head in {"python", "python3", "py"}:
        if len(tokens) >= 2 and tokens[1] in {"-c", "-m"}:
            return f"python:{tokens[1]}"
        return "python"
    if head in {
        "git",
        "gh",
        "docker",
        "kubectl",
        "npm",
        "yarn",
        "pnpm",
        "bun",
        "node",
        "curl",
        "wget",
    }:
        return head
    return "bash"


def _classify_pytest_scope(tokens: list[str]) -> tuple[str, str, list[str]]:
    """Return (scope_kind, scope_value, suffix_parts) for a pytest-
    family command. Walks tokens, collecting positional path args and
    scope-modifying flags while dropping non-semantic flags.
    """
    positional: list[str] = []
    suffix: list[str] = []

    # Skip the command prefix tokens (pytest / python -m pytest).
    i = 0
    while i < len(tokens):
        t = tokens[i]
        tl = t.lower()
        if tl in {"python", "python3", "py"} and i + 1 < len(tokens) and tokens[i + 1] == "-m":
            i += 2
            continue
        if tl in {"pytest", "py.test"}:
            i += 1
            break
        i += 1

    while i < len(tokens):
        t = tokens[i]
        if t in _NONSEMANTIC_FLAGS:
            i += 1
            continue
        # `--ignore=tests/conductor` → suffix "ignore:tests/conductor"
        if "=" in t and t.split("=", 1)[0] in _SCOPE_MODIFYING_FLAGS:
            flag, val = t.split("=", 1)
            suffix.append(f"{flag.lstrip('-')}:{_normalize_path(val)}")
            i += 1
            continue
        if t in _SCOPE_MODIFYING_FLAGS and i + 1 < len(tokens):
            suffix.append(f"{t.lstrip('-')}:{_normalize_path(tokens[i + 1])}")
            i += 2
            continue
        if t.startswith("-"):
            # Unknown flag; drop as non-semantic by default (err on
            # the side of tighter buckets). If it turns out to change
            # runtime materially, it gets added to _SCOPE_MODIFYING.
            i += 1
            continue
        positional.append(_normalize_path(t))
        i += 1

    suffix.sort()

    if not positional:
        return ("full", "-", suffix)

    # If any positional has "::" it's a test-id (single test or class).
    for p in positional:
        if _PYTEST_TEST_ID_RE.search(p):
            return ("test-id", positional[0], suffix)

    # If the first positional ends in .py it's a file.
    first = positional[0]
    if first.endswith(".py"):
        return ("file", first, suffix)

    # Otherwise treat as directory scope.
    return ("dir", first, suffix)


def _classify_dotnet_build_scope(tokens: list[str]) -> tuple[str, str, list[str]]:
    # Look for .csproj / .sln / .fsproj positional.
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        tl = t.lower()
        if (
            tl.endswith(".csproj")
            or tl.endswith(".sln")
            or tl.endswith(".fsproj")
            or tl.endswith(".vbproj")
        ):
            return ("module", _normalize_path(t), [])
    return ("full", "-", [])


def _classify_generic_scope(tokens: list[str]) -> tuple[str, str, list[str]]:
    # Coarse: keep first non-flag positional after the tool head.
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        return ("arg", _normalize_path(t)[:80], [])
    return ("full", "-", [])


def bucket_key_for(command: str) -> BucketKey:
    """Compute the BucketKey for a shell command.

    Deterministic: same command text → same key across calls. Callers
    record observed durations under `bucket.key()` and look up parent
    aggregates via `bucket.parent_chain()` when the exact key is cold.
    """
    command = (command or "").strip()
    tool = _canonical_tool(command)
    tokens = _tokenize(command)

    if tool in {"pytest", "unittest"}:
        scope_kind, scope_value, suffix = _classify_pytest_scope(tokens)
    elif tool == "dotnet-build":
        scope_kind, scope_value, suffix = _classify_dotnet_build_scope(tokens)
    elif tool in {
        "jest",
        "go-test",
        "cargo-test",
        "tsc",
        "cargo-build",
        "go-build",
        "make",
        "gradle",
        "mvn",
        "npm-build",
    }:
        scope_kind, scope_value, suffix = _classify_generic_scope(tokens)
    else:
        scope_kind, scope_value, suffix = ("full", "-", [])

    return BucketKey(
        tool=tool,
        scope_kind=scope_kind,
        scope_value=scope_value,
        suffix=tuple(suffix),
    )


def outcome_for_exit_code(exit_code: int | None) -> str:
    """Classify a finished run's exit_code into the outcome tag stored
    alongside durations.

    Returns one of: pass | fail | timeout | unknown.
    Unknown observations are NOT recorded — they pollute the signal.
    """
    if exit_code is None:
        return "unknown"
    if exit_code == 0:
        return "pass"
    if exit_code == 124 or exit_code < 0:
        return "timeout"
    return "fail"
