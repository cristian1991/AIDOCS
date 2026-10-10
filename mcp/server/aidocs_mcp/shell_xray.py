"""Future-sight X-RAY: expand the hidden execution graph of a package/
build/script command BEFORE it runs.

The name-based preflight (shell_lifecycle) knows that `npm install` /
`make` / `pip install` trigger downstream execution. The X-ray goes
further: it reads the actual project manifests the command would consume
and enumerates WHAT would run — lifecycle hooks, dangerous script bodies
(curl|sh, secret reads, inline interpreters), git / tarball / file
dependencies, downloaders, and network/insecure sources.

Pure + bounded + safe: it only READS manifests (size-capped, never
executes), tolerates missing/malformed files, and NEVER stores raw script
bodies / dependency URLs (which can carry tokens) in its nodes — only the
node KIND plus a safe label (script key, package name, matched-pattern
name). Severity escalates the name-based floor; it never weakens it.

Jurisdiction: the X-ray is a per-command PREFLIGHT on AGENT-INVOKED
commands (package managers, builds, and editor-extension installs such as
`code --install-extension`, VSIX, vsce/ovsx). It governs only what an
agent is about to run. It does NOT monitor or control the host's
BACKGROUND auto-updaters (editor/OS self-update) or already-running host
processes — those emit no command for it to gate and are out of scope by
construction, not by oversight.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .anticoup import EDITOR_BINARIES as _EDITOR_BINARIES
from .anticoup import findings_for_segment as _coup_findings_for_segment

_MAX_MANIFEST_BYTES = 512_000

# severity ranks (shared vocab with shell_lifecycle)
SEV_NONE = "none"
SEV_CONFIRM = "confirm"
SEV_DENY = "deny"
_RANK = {SEV_NONE: 0, SEV_CONFIRM: 1, SEV_DENY: 2}

# node kinds → severity
_DENY_KINDS = frozenset(
    {
        "dangerous_script",
        "downloader",
        "secret_read",
        "network_exec",
        # control-plane: edits that disarm the gate, or forge an unchecked
        # parallel execution path → never silently allowed.
        "aidocs_config_mutation",
        "security_flag_mutation",
        "hook_mutation",
        "parallel_tool_path",
    },
)
_CONFIRM_KINDS = frozenset(
    {
        "lifecycle_hook",
        "git_dep",
        "tarball_dep",
        "file_dep",
        "network_dep",
        "insecure_http",
        "setup_py",
        "build_backend",
        "makefile_recipe",
        "dockerfile_run",
        "dotnet_build",
        "index_url",
        # candidate inspection (install/update future-sight)
        "candidate_lifecycle_hook",
        "uninspectable_candidate",
        "source_build",
        "build_script",
        # control-plane registry / settings edits → operator freeze.
        "mcp_registry_mutation",
        "tool_registry_mutation",
        "settings_mutation",
        "make_executable",
    },
)


@dataclass(frozen=True)
class XrayNode:
    kind: str
    label: str  # SAFE: script key / package name / pattern name — never raw


@dataclass
class XrayResult:
    severity: str = SEV_NONE
    nodes: list[XrayNode] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    # True when a segment's inspection raised — the graph may be INCOMPLETE,
    # so callers under enforcement must fail closed rather than trust it.
    inspection_failed: bool = False

    def add(self, kind: str, label: str) -> None:
        self.nodes.append(XrayNode(kind, label))

    def finalize(self) -> XrayResult:
        sev = SEV_NONE
        for n in self.nodes:
            if n.kind in _DENY_KINDS:
                sev = SEV_DENY
                break
            if n.kind in _CONFIRM_KINDS:
                sev = SEV_CONFIRM
        self.severity = sev
        return self


# ── danger scanning over a script / recipe / RUN body ───────────────
# (label is the matched-pattern NAME, never the raw line.)
_DANGER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\|\s*(?:sh|bash|zsh)\b", "dangerous_script", "pipe_to_shell"),
    (r"\b(?:curl|wget)\b", "downloader", "downloader"),
    (r"(?:invoke-webrequest|net\.webclient|downloadstring)", "downloader", "downloader"),
    (r"/dev/tcp/", "network_exec", "dev_tcp"),
    (r"\bnc\b|\bncat\b|\bnetcat\b", "network_exec", "netcat"),
    (r"\b(?:base64)\b.*-d|frombase64string", "dangerous_script", "base64_decode_exec"),
    (r"\beval\b|\biex\b", "dangerous_script", "eval"),
    (
        r"\b(?:python[23]?|node|ruby|perl|php)\b\s+-(?:c|e)\b",
        "dangerous_script",
        "inline_interpreter",
    ),
    (r"\bsudo\b", "dangerous_script", "sudo"),
    (r"\brm\s+-rf\b", "dangerous_script", "rm_rf"),
    (r"\.env\b|id_rsa|/\.ssh/|private[ _-]?key", "secret_read", "secret_path"),
    (r"[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|APIKEY|API_KEY)[A-Z0-9_]*", "secret_read", "secret_var"),
    (r"\bprintenv\b|\benv\s*\|", "secret_read", "env_dump"),
)


def _scan_danger(text: str, where: str, result: XrayResult) -> None:
    low = text.lower()
    for pat, kind, name in _DANGER_PATTERNS:
        if re.search(pat, low, flags=re.IGNORECASE):
            result.add(kind, f"{where}:{name}")


def _read(root: Path, name: str) -> str | None:
    try:
        p = root / name
        if not p.is_file():
            return None
        data = p.read_bytes()[:_MAX_MANIFEST_BYTES]
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return None


# ── npm / pnpm / yarn ───────────────────────────────────────────────
_NPM_LIFECYCLE = frozenset(
    {
        "preinstall",
        "install",
        "postinstall",
        "prepare",
        "prepublish",
        "prepublishonly",
        "prepack",
        "postpack",
        "preuninstall",
        "postuninstall",
        "preprepare",
        "postprepare",
    },
)


def _xray_npm(root: Path, result: XrayResult) -> None:
    raw = _read(root, "package.json")
    if raw is not None:
        result.manifests.append("package.json")
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {}
        if isinstance(obj, dict):
            scripts = obj.get("scripts")
            if isinstance(scripts, dict):
                for key, body in scripts.items():
                    if not isinstance(body, str):
                        continue
                    if key.lower() in _NPM_LIFECYCLE:
                        result.add("lifecycle_hook", str(key))
                        _scan_danger(body, f"script:{key}", result)
            for dep_field in ("dependencies", "devDependencies", "optionalDependencies"):
                deps = obj.get(dep_field)
                if not isinstance(deps, dict):
                    continue
                for name, ver in deps.items():
                    if not isinstance(ver, str):
                        continue
                    _classify_dep_spec(str(name), ver, result)
    for lock in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json"):
        raw = _read(root, lock)
        if raw is None:
            continue
        result.manifests.append(lock)
        low = raw.lower()
        if "git+" in low or "git://" in low:
            result.add("git_dep", f"{lock}:git")
        if ".tgz" in low and ("http://" in low or "https://" in low):
            # tarball deps resolved to a URL outside the registry default
            if re.search(r"https?://(?!registry\.npmjs\.org)", low):
                result.add("tarball_dep", f"{lock}:tarball")
        if "http://" in low:
            result.add("insecure_http", f"{lock}:http")
        if re.search(r'"resolved"\s*:\s*"file:', low) or "link:" in low:
            result.add("file_dep", f"{lock}:file")


def _classify_dep_spec(name: str, ver: str, result: XrayResult) -> None:
    v = ver.strip().lower()
    if v.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:")):
        result.add("git_dep", name)
    elif v.startswith(("file:", "link:", "portal:")):
        result.add("file_dep", name)
    elif ".tgz" in v or ".tar.gz" in v:
        result.add("tarball_dep", name)
    elif v.startswith("http://"):
        result.add("insecure_http", name)
    elif v.startswith("https://"):
        result.add("network_dep", name)


# ── candidate inspection (install / update future-sight) ────────────
# The manifest scan above sees deps the project ALREADY declares. The
# real install/update threat is a package whose *next* resolved version
# carries a malicious lifecycle script — the "clean today, pwned
# tomorrow" postinstall. We never reach the network (local-first law), so
# we inspect every locally available source of candidate truth:
#   1. explicit command-line specs  (npm install <spec…> / yarn add …)
#   2. the locally cached resolution (node_modules/<pkg>/package.json)
#   3. lockfile-pinned sources       (handled by _xray_npm)
# and when a candidate's metadata cannot be inspected locally (a registry
# spec we have never resolved, or an `update` that would pull newer
# versions), we emit `uninspectable_candidate` so the preflight FAILS
# CLOSED under enforcement (a freeze the operator must motivate) rather
# than running blind. `--ignore-scripts` closes the postinstall vector,
# and `--offline`/`--prefer-offline` means no network resolution — both
# are honoured truthfully so the gate is precise, not blanket.
_MAX_NM_PACKAGES = 1000
_NPM_INSTALL_SUBCMDS = frozenset({"install", "i", "isntall", "in", "ins", "inst", "add", "ci"})
_NPM_UPDATE_SUBCMDS = frozenset({"update", "up", "upgrade"})
_OFFLINE_FLAGS = frozenset({"--offline", "--prefer-offline"})


def _npm_intent(binary: str, args: list[str]) -> tuple[str | None, list[str]]:
    """Return (action, explicit_specs); action in {install, update, None}."""
    positional = [a for a in args if not a.startswith("-")]
    sub = positional[0].lower() if positional else ""
    rest = positional[1:]
    if binary in ("yarn", "bun"):
        if not positional:
            return ("install", [])  # bare `yarn`/`bun` installs from manifest
        if sub in ("add", "install", "i"):
            return ("install", rest)
        if sub in ("up", "upgrade", "update"):
            return ("update", rest)
        return (None, [])
    # npm / pnpm
    if sub in _NPM_INSTALL_SUBCMDS:
        return ("install", rest)
    if sub in _NPM_UPDATE_SUBCMDS:
        return ("update", rest)
    return (None, [])


def _node_trusted_set(root: Path) -> set[str]:
    """Bun runs lifecycle scripts ONLY for packages listed in
    package.json `trustedDependencies` (or under --trust). Everything else
    has its install scripts blocked by default — so an untrusted candidate
    carries no postinstall vector.
    """
    raw = _read(root, "package.json")
    if raw is None:
        return set()
    try:
        obj = json.loads(raw)
    except Exception:
        return set()
    if not isinstance(obj, dict):
        return set()
    td = obj.get("trustedDependencies")
    if isinstance(td, list):
        return {str(x) for x in td if isinstance(x, str)}
    return set()


def _node_declared_deps(root: Path) -> set[str]:
    """All declared dependency names across dependency tables."""
    raw = _read(root, "package.json")
    if raw is None:
        return set()
    try:
        obj = json.loads(raw)
    except Exception:
        return set()
    if not isinstance(obj, dict):
        return set()
    names: set[str] = set()
    for f in ("dependencies", "devDependencies", "optionalDependencies"):
        d = obj.get(f)
        if isinstance(d, dict):
            names.update(str(k) for k in d)
    return names


_SOURCE_SPEC_PREFIXES = (
    "git+",
    "git:",
    "github:",
    "gitlab:",
    "bitbucket:",
    "http://",
    "https://",
    "file:",
    "link:",
    "portal:",
    "./",
    "../",
)


def _split_spec(spec: str) -> tuple[str | None, str]:
    """(name, ver) for a registry spec; (None, raw) for a source spec."""
    if spec.startswith(_SOURCE_SPEC_PREFIXES):
        return (None, spec)
    if spec.startswith("@"):  # @scope/name[@ver]
        at = spec.find("@", 1)
        if at == -1:
            return (spec, "")
        return (spec[:at], spec[at + 1 :])
    if "@" in spec:
        name, ver = spec.split("@", 1)
        return (name, ver)
    return (spec, "")


def _scan_candidate_scripts(raw: str, where: str, result: XrayResult) -> None:
    try:
        obj = json.loads(raw)
    except Exception:
        return
    if not isinstance(obj, dict):
        return
    scripts = obj.get("scripts")
    if not isinstance(scripts, dict):
        return
    for key, body in scripts.items():
        if not isinstance(body, str):
            continue
        if key.lower() in _NPM_LIFECYCLE:
            result.add("candidate_lifecycle_hook", f"{where}:{key}")
            _scan_danger(body, f"{where}:{key}", result)


def _scan_installed_lifecycle(nm: Path, result: XrayResult, only: set[str] | None = None) -> None:
    """Defence in depth: a malicious lifecycle script already resolved into
    node_modules (pwned version cached) is caught BEFORE the next run.
    Bounded; emits danger nodes only, never per-package confirm noise.
    ``only`` restricts the scan to a set of package names (bun: only
    trusted deps ever run their scripts).
    """
    if not nm.is_dir():
        return
    count = 0

    def _one(pkg_dir: Path, label: str) -> None:
        if only is not None and label not in only:
            return
        raw = _read(pkg_dir, "package.json")
        if raw is None:
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        scripts = obj.get("scripts")
        if not isinstance(scripts, dict):
            return
        for key, body in scripts.items():
            if isinstance(body, str) and key.lower() in _NPM_LIFECYCLE:
                _scan_danger(body, f"installed:{label}:{key}", result)

    try:
        entries = sorted(nm.iterdir())
    except Exception:
        return
    for d in entries:
        if count >= _MAX_NM_PACKAGES:
            break
        if d.name.startswith("."):
            continue
        if d.name.startswith("@"):  # scope directory
            try:
                subs = sorted(d.iterdir())
            except Exception:
                continue
            for sd in subs:
                if count >= _MAX_NM_PACKAGES:
                    break
                _one(sd, f"{d.name}/{sd.name}")
                count += 1
        else:
            _one(d, d.name)
            count += 1


def _xray_npm_candidates(root: Path, binary: str, args: list[str], result: XrayResult) -> None:
    action, specs = _npm_intent(binary, args)
    if action is None:
        return
    flagset = {a.lower() for a in args if a.startswith("-")}
    ignore_scripts = "--ignore-scripts" in flagset
    offline = bool(flagset & _OFFLINE_FLAGS)
    # `--package-lock-only` writes the lockfile without running scripts.
    if "--package-lock-only" in flagset:
        ignore_scripts = True
    nm = root / "node_modules"

    # bun blocks lifecycle scripts for untrusted deps by default. Scripts
    # run only for trustedDependencies (or with --trust). Model that as a
    # per-package predicate so the gate is precise, not blanket.
    is_bun = binary == "bun"
    trust_all = "--trust" in flagset
    trusted = _node_trusted_set(root) if is_bun else set()

    def _scripts_run(name: str) -> bool:
        if ignore_scripts:
            return False
        if is_bun:
            return trust_all or name in trusted
        return True

    explicit_names: set[str] = set()

    # 1. explicit command-line specs
    for spec in specs:
        name, ver = _split_spec(spec)
        if name is None:  # explicit source spec (git/tarball/file/http)
            _classify_dep_spec(spec, ver, result)
            continue
        explicit_names.add(name)
        if not _scripts_run(name):
            continue  # scripts disabled / untrusted → no postinstall vector
        cached = _read(nm / name, "package.json")
        if cached is not None:
            _scan_candidate_scripts(cached, f"candidate:{name}", result)
        elif not offline:
            # registry spec we have never resolved → cannot inspect ahead
            result.add("uninspectable_candidate", name)

    # 1b. bun bare `install`: the declared trusted set (or all deps under
    #     --trust) WILL run install scripts. Inspect each locally or fail
    #     closed — an uncached trusted dep's postinstall is unseen.
    if is_bun and action == "install":
        run_set = _node_declared_deps(root) if trust_all else set(trusted)
        for name in sorted(run_set - explicit_names):
            cached = _read(nm / name, "package.json")
            if cached is not None:
                _scan_candidate_scripts(cached, f"candidate:{name}", result)
            elif not offline:
                result.add("uninspectable_candidate", name)

    # 2. `update` pulls NEWER versions from the network — the canonical
    #    pwned-tomorrow vector. Cannot be inspected ahead without a fetch.
    if action == "update" and not offline:
        for t in specs or ["<all-dependencies>"]:
            tname = _split_spec(t)[0] or t
            if _scripts_run(tname if tname != "<all-dependencies>" else ""):
                result.add("uninspectable_candidate", f"update:{tname}")
            elif is_bun and (trust_all or trusted):
                # bare bun update with some trusted deps → still a vector
                result.add("uninspectable_candidate", f"update:{tname}")

    # 3. defence in depth: scan already-resolved lifecycle scripts. For bun,
    #    only trusted deps' scripts ever run, so only those are a real risk.
    if not ignore_scripts:
        _scan_installed_lifecycle(
            nm,
            result,
            only=trusted if (is_bun and not trust_all) else None,
        )


# ── pip / poetry ────────────────────────────────────────────────────
def _xray_pip(
    root: Path,
    args: list[str],
    result: XrayResult,
    *,
    no_build: bool = False,
    offline: bool = False,
) -> None:
    # explicit requirements file(s)
    req_files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "--requirement") and i + 1 < len(args):
            req_files.append(args[i + 1])
            i += 1
        i += 1
    if not req_files and (root / "requirements.txt").is_file():
        req_files.append("requirements.txt")
    for rf in req_files:
        raw = _read(root, rf)
        if raw is None:
            continue
        result.manifests.append(rf)
        registry_seen = False
        for line in raw.splitlines():
            ls = line.strip().lower()
            if not ls or ls.startswith("#"):
                continue
            if ls.startswith("git+") or "git+http" in ls or "git+ssh" in ls:
                result.add("git_dep", "requirement")
            elif ls.startswith(("http://",)):
                result.add("insecure_http", "requirement")
            elif ls.startswith(("https://",)):
                result.add("network_dep", "requirement")
            elif ls.startswith(("-e ", "--editable")):
                result.add("setup_py", "editable_install")
            elif ls.startswith(("--index-url", "--extra-index-url", "-f ", "--find-links")):
                result.add("index_url", "custom_index")
            elif ls.startswith("-"):
                continue  # other option line
            else:
                # plain registry requirement (foo==1.2.3): an sdist for it
                # builds from source on install → cannot inspect ahead.
                registry_seen = True
        if registry_seen and not no_build and not offline:
            result.add("uninspectable_candidate", "requirement_set")
    # local install (pip install . / -e .) → setup.py executes on install
    if any(a in (".", "-e", "--editable") for a in args) or any(a == "." for a in args):
        if (root / "setup.py").is_file():
            result.manifests.append("setup.py")
            result.add("setup_py", "setup_py")
    _xray_pyproject(root, result, no_build=no_build, offline=offline)


# pyproject dep tables (poetry / PEP621) that declare a real registry set.
_PYPROJECT_DEP_TABLE = re.compile(
    r"\[tool\.poetry\.dependencies\]|\[tool\.poetry\.group\."
    r"[^\]]+\.dependencies\]|\[project\.dependencies\]",
)
_PEP621_DEPS = re.compile(r'dependencies\s*=\s*\[[^\]]*["\']')


def _xray_pyproject(
    root: Path,
    result: XrayResult,
    *,
    no_build: bool = False,
    offline: bool = False,
) -> None:
    raw = _read(root, "pyproject.toml")
    if raw is None:
        return
    result.manifests.append("pyproject.toml")
    low = raw.lower()
    if "build-backend" in low or "[build-system]" in low:
        result.add("build_backend", "pyproject_build")
    # poetry / pep508 deps referencing git/url/path
    if re.search(r"git\s*=", low) or "git+" in low:
        result.add("git_dep", "pyproject")
    if re.search(r'url\s*=\s*["\']https?://', low):
        result.add("network_dep", "pyproject")
    if re.search(r"path\s*=", low):
        result.add("file_dep", "pyproject")
    # declared registry dependency SET → resolved + (sdist) built on install.
    if (
        (_PYPROJECT_DEP_TABLE.search(low) or _PEP621_DEPS.search(low))
        and not no_build
        and not offline
    ):
        result.add("uninspectable_candidate", "declared_deps")


# ── make ────────────────────────────────────────────────────────────
def _xray_make(root: Path, result: XrayResult) -> None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        raw = _read(root, name)
        if raw is None:
            continue
        result.manifests.append(name)
        saw_recipe = False
        for line in raw.splitlines():
            if line.startswith("\t"):  # recipe line
                saw_recipe = True
                _scan_danger(line, "makefile", result)
        if saw_recipe:
            result.add("makefile_recipe", name)
        return


# ── docker ──────────────────────────────────────────────────────────
def _xray_docker(root: Path, result: XrayResult) -> None:
    raw = _read(root, "Dockerfile")
    if raw is None:
        return
    result.manifests.append("Dockerfile")
    saw_run = False
    for line in raw.splitlines():
        ls = line.strip()
        low = ls.lower()
        if low.startswith("run "):
            saw_run = True
            _scan_danger(ls, "dockerfile", result)
        elif low.startswith("add ") and re.search(r"https?://", low):
            result.add("downloader", "dockerfile_add_url")
    if saw_run:
        result.add("dockerfile_run", "Dockerfile")


# ── python candidate inspection (pip / poetry / uv / pipenv) ────────
# The exec vector for Python installs is the source build: a registry
# package shipped as an sdist runs setup.py / its PEP517 build backend at
# install time = arbitrary code. A prebuilt wheel does NOT. So a registry
# candidate we cannot inspect locally is uninspectable UNLESS the command
# forbids source builds (--only-binary :all:) or forbids the network
# (--no-index / --offline). `update`/`--upgrade` re-resolves to newer
# versions — the pwned-tomorrow vector.
_PY_VALUE_FLAGS = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-e",
        "--editable",
        "-t",
        "--target",
        "--prefix",
        "--root",
        "--src",
        "--platform",
        "--python-version",
        "--implementation",
        "--abi",
        "--upgrade-strategy",
        "--progress-bar",
        "--report",
        "--cache-dir",
        "--only-binary",
        "--no-binary",
        "--source",
        "--index",
        "--python",
        "--with",
        "--extra",
    },
)


def _positionals_skipping_values(args: list[str], value_flags: frozenset[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            base = a.split("=", 1)[0]
            if base in value_flags and "=" not in a:
                i += 2  # skip the flag AND its value
                continue
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _classify_py_spec(spec: str, result: XrayResult) -> str:
    """Returns one of: source | local | registry. Side-effect: records a
    dep node for source specs.
    """
    s = spec.strip()
    low = s.lower()
    if s in (".", ".."):
        return "local"
    if low.startswith(("git+", "git:")):
        result.add("git_dep", "pyspec")
        return "source"
    if low.startswith("http://") or re.search(r"@\s*http://", low):
        result.add("insecure_http", "pyspec")
        return "source"
    if low.startswith("https://") or re.search(r"@\s*https://", low):
        result.add("network_dep", "pyspec")
        return "source"
    if low.startswith("file:"):
        result.add("file_dep", "pyspec")
        return "source"
    if s.startswith(("./", "../", "/")) or "/" in s or "\\" in s:
        return "local"
    return "registry"


def _py_no_build(flags: set[str], args: list[str]) -> bool:
    joined = " ".join(args).lower()
    return ("--only-binary" in joined and ":all:" in joined) or "--only-binary=:all:" in {
        a.lower() for a in args
    }


def _py_action(binary: str, positionals: list[str]) -> tuple[str | None, list[str]]:
    sub = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    if binary in ("pip", "pip3"):
        if sub in ("install", "download", "wheel"):
            return ("install", rest)
        return (None, [])
    if binary == "uv":
        if sub == "add":
            return ("install", rest)
        if sub == "sync":
            return ("install", [])
        if sub == "pip":
            s2 = rest[0].lower() if rest else ""
            if s2 in ("install", "sync"):
                return ("install", rest[1:])
            return (None, [])
        if sub == "lock":
            return ("update", [])
        return (None, [])
    if binary in ("poetry", "pipenv"):
        if sub == "add":
            return ("install", rest)
        if sub == "install":
            return ("install", [])
        if sub in ("update", "up"):
            return ("update", rest)
        return (None, [])
    return (None, [])


def _xray_python_candidates(root: Path, binary: str, args: list[str], result: XrayResult) -> None:
    flags = {a.split("=", 1)[0].lower() for a in args if a.startswith("-")}
    positionals = _positionals_skipping_values(args, _PY_VALUE_FLAGS)
    action, specs = _py_action(binary, positionals)
    no_build = _py_no_build(flags, args)
    offline = "--no-index" in flags or "--offline" in flags
    upgrade = bool(flags & {"--upgrade", "-u"})
    if action is not None:
        for spec in specs:
            kind = _classify_py_spec(spec, result)
            if kind == "registry" and not no_build and not offline:
                # sdist may build from source (setup.py / PEP517) ahead of
                # us → cannot inspect → fail closed.
                result.add("uninspectable_candidate", "pyspec")
        if (action == "update" or upgrade) and not no_build and not offline:
            for _t in specs or ["<all-dependencies>"]:
                result.add("uninspectable_candidate", "update")
    # always inspect the declared manifests (deps / build backend), passing
    # the no-build / offline posture so the registry dep SET is sealed too.
    if binary in ("pip", "pip3"):
        _xray_pip(root, args, result, no_build=no_build, offline=offline)
    else:
        _xray_pyproject(root, result, no_build=no_build, offline=offline)


# ── .NET / NuGet candidate inspection ───────────────────────────────
# A NuGet package can ship MSBuild .targets/.props that EXECUTE during
# build, and a project file can carry <Exec> tasks — both are source-build
# code execution. `dotnet add package` / `dotnet tool install` introduce
# new packages whose build hooks cannot be inspected ahead of a restore.
_MSBUILD_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".props", ".targets")


def _dotnet_action(binary: str, positionals: list[str]) -> tuple[str | None, list[str]]:
    sub = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    if binary == "nuget":
        if sub in ("restore", "install"):
            return ("restore", rest)
        return (None, [])
    # dotnet
    if sub == "add":
        return ("add", rest)
    if sub in ("build", "publish", "pack", "run", "test", "msbuild"):
        return ("build", rest)
    if sub == "restore":
        return ("restore", rest)
    if sub == "tool":
        s2 = rest[0].lower() if rest else ""
        if s2 in ("install", "update", "restore"):
            return ("tool", rest[1:])
    return (None, [])


def _scan_nuget_config(root: Path, result: XrayResult) -> None:
    for name in ("nuget.config", "NuGet.Config", "NuGet.config"):
        raw = _read(root, name)
        if raw is None:
            continue
        result.manifests.append(name)
        low = raw.lower()
        if re.search(r'value\s*=\s*"http://', low):
            result.add("insecure_http", "nuget_source")
        elif re.search(r'value\s*=\s*"https?://', low):
            result.add("index_url", "nuget_source")
        return


def _scan_msbuild_projects(root: Path, result: XrayResult) -> bool:
    """Returns True if a NuGet PackageReference / packages.config asset set
    was found (those packages' MSBuild .targets execute at build).
    """
    saw_assets = (root / "packages.config").is_file()
    seen = 0
    for p in _safe_iterdir(root):
        if seen >= 20:
            break
        if p.suffix.lower() not in _MSBUILD_SUFFIXES:
            continue
        raw = _read(root, p.name)
        if raw is None:
            continue
        seen += 1
        result.manifests.append(p.name)
        low = raw.lower()
        if "<packagereference" in low:
            saw_assets = True
        if "<exec" in low:
            result.add("build_script", f"{p.name}:exec")
            for m in re.finditer(r'command\s*=\s*"([^"]*)"', raw, flags=re.IGNORECASE):
                _scan_danger(m.group(1), f"msbuild:{p.name}", result)
    return saw_assets


def _xray_dotnet_candidates(root: Path, binary: str, args: list[str], result: XrayResult) -> None:
    positionals = [a for a in args if not a.startswith("-")]
    action, _rest = _dotnet_action(binary, positionals)
    if action is None:
        return
    _scan_nuget_config(root, result)
    if action == "build":
        # build/publish/pack execute project + NuGet package MSBuild targets
        result.add("source_build", "dotnet_build")
        if _scan_msbuild_projects(root, result):
            # the restored package assets' .targets run at build, unseen.
            result.add("uninspectable_candidate", "nuget_assets")
    elif action == "restore":
        # restore pulls the declared PackageReference asset set whose
        # build hooks cannot be inspected ahead of the restore.
        if _scan_msbuild_projects(root, result):
            result.add("uninspectable_candidate", "nuget_assets")
    elif action == "add":
        # adding a NuGet package: its .targets/.props run at next build
        result.add("uninspectable_candidate", "nuget_package")
    elif action == "tool":
        # dotnet tool install downloads and installs an executable tool
        result.add("uninspectable_candidate", "dotnet_tool")


# ── cargo (Rust) ────────────────────────────────────────────────────
# Exec vector: build.rs build scripts and procedural macros run arbitrary
# code at BUILD/INSTALL time — for the crate AND every dependency.
#
# Offline trap (sealed): --offline / --frozen only remove the NETWORK
# fetch of newer versions. The build-script EXECUTION vector remains: a
# cached/vendored crate still runs its build.rs on build/install, and we
# never read ~/.cargo, so it stays uninspectable. So offline suppresses
# only the re-resolution candidate (cargo update / cargo add), never the
# build/install build-script vector. Cargo has no stable
# "don't run build scripts" trust knob, so the vector is always live at
# build; --locked/--frozen only pin versions, they do not disarm it.
_CARGO_DEP_HEADER = re.compile(
    r"\[(?:build-|dev-)?dependencies\]|\[.*\.dependencies\]|"
    r"\[(?:build-|dev-)?dependencies\.",
)


def _scan_cargo_toml(root: Path, result: XrayResult) -> bool:
    """Records git/path/url dep nodes; returns True if a non-empty
    dependency table is declared (those deps run build.rs at build).
    """
    raw = _read(root, "Cargo.toml")
    if raw is None:
        return False
    result.manifests.append("Cargo.toml")
    low = raw.lower()
    if re.search(r"git\s*=", low):
        result.add("git_dep", "cargo")
    if re.search(r"path\s*=", low):
        result.add("file_dep", "cargo")
    if re.search(r'url\s*=\s*["\']https?://', low):
        result.add("network_dep", "cargo")
    in_dep = False
    has_deps = False
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_dep = bool(_CARGO_DEP_HEADER.match(s.lower()))
            continue
        if in_dep and s and not s.startswith("#") and "=" in s:
            has_deps = True
    return has_deps


def _scan_build_rs(root: Path, result: XrayResult) -> None:
    raw = _read(root, "build.rs")
    if raw is None:
        return
    result.manifests.append("build.rs")
    result.add("build_script", "build.rs")
    _scan_danger(raw, "build.rs", result)


def _xray_cargo(root: Path, args: list[str], result: XrayResult) -> None:
    positionals = [a for a in args if not a.startswith("-")]
    flags = {a.lower() for a in args if a.startswith("-")}
    # --frozen implies --offline + --locked.
    offline = bool(flags & {"--offline", "--frozen"})
    sub = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    has_deps = _scan_cargo_toml(root, result)
    if sub in ("build", "run", "test", "bench", "check", "rustc", "doc", "install"):
        # build.rs / proc-macros of the crate AND its deps execute here —
        # a BUILD-time vector that fires even with --offline/--frozen.
        result.add(
            "source_build",
            "cargo_install" if sub == "install" else "cargo_build",
        )
        _scan_build_rs(root, result)
        if sub == "install" and rest:
            # installs + builds an external crate (+ its build.rs), unseen
            # whether the source is fetched or already cached → fail closed.
            result.add("uninspectable_candidate", "crate")
        elif has_deps:
            # declared dependency set's build scripts run at build, unseen.
            result.add("uninspectable_candidate", "declared_deps")
    elif sub == "add":
        # edits Cargo.toml + resolves a version (network unless offline);
        # the build script runs at the NEXT build, not now.
        if rest and not offline:
            result.add("uninspectable_candidate", "crate")
    elif sub == "update" and not offline:
        # re-resolves to newer versions — the pwned-tomorrow vector.
        for t in rest or ["<all-dependencies>"]:
            result.add("uninspectable_candidate", f"update:{t}")


# ── go (Go modules) ─────────────────────────────────────────────────
# Exec vector: `go generate` runs //go:generate directives (arbitrary
# commands), cgo (#cgo) compiles C, and go get/install fetch + build
# modules. go build/run/test compile project + dep code.
def _scan_go_mod(root: Path, result: XrayResult) -> None:
    raw = _read(root, "go.mod")
    if raw is None:
        return
    result.manifests.append("go.mod")
    for line in raw.splitlines():
        ls = line.strip().lower()
        if ls.startswith("replace") and re.search(r"=>\s*(\.{1,2}/|/)", ls):
            result.add("file_dep", "go_replace")


def _scan_go_generate(root: Path, result: XrayResult) -> None:
    seen = 0
    for p in _safe_iterdir(root):
        if seen >= 50:
            break
        if p.suffix.lower() != ".go":
            continue
        raw = _read(root, p.name)
        if raw is None:
            continue
        seen += 1
        for line in raw.splitlines():
            ls = line.strip()
            if ls.startswith("//go:generate"):
                result.add("build_script", f"{p.name}:generate")
                _scan_danger(ls, f"go_generate:{p.name}", result)
            elif ls.startswith("// #cgo") or ls.startswith("//#cgo"):
                result.add("build_script", f"{p.name}:cgo")


def _xray_go(root: Path, args: list[str], result: XrayResult) -> None:
    positionals = [a for a in args if not a.startswith("-")]
    sub = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    _scan_go_mod(root, result)
    if sub in ("get", "install"):
        result.add("uninspectable_candidate", "module")
    elif sub == "mod":
        s2 = rest[0].lower() if rest else ""
        if s2 in ("download", "tidy"):
            result.add("uninspectable_candidate", "module")
    elif sub in ("build", "run", "test", "vet"):
        result.add("source_build", "go_build")
        _scan_go_generate(root, result)
    elif sub == "generate":
        _scan_go_generate(root, result)


# ── gem / bundler (Ruby) ────────────────────────────────────────────
# Exec vector: a gem's native extension runs extconf.rb (and the gemspec
# is executable Ruby) at install. gem install / bundle add fetch + build;
# bundle update re-resolves to newer versions.
def _scan_gemfile(root: Path, result: XrayResult) -> None:
    raw = _read(root, "Gemfile")
    if raw is None:
        return
    result.manifests.append("Gemfile")
    low = raw.lower()
    if re.search(r"\bgit:|github:|:git\s*=>|\bgit\s+['\"]", low):
        result.add("git_dep", "gemfile")
    if re.search(r"\bpath:|:path\s*=>", low):
        result.add("file_dep", "gemfile")
    if re.search(r'source\s+["\']http://', low):
        result.add("insecure_http", "gemfile")
    elif re.search(r'source\s+["\']https://', low):
        result.add("network_dep", "gemfile")


def _xray_ruby(root: Path, binary: str, args: list[str], result: XrayResult) -> None:
    positionals = [a for a in args if not a.startswith("-")]
    local = "--local" in {a.lower() for a in args if a.startswith("-")}
    sub = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    _scan_gemfile(root, result)
    if binary == "gem":
        if sub == "install" and rest and not local:
            result.add("uninspectable_candidate", "gem")
        elif sub == "update" and not local:
            result.add("uninspectable_candidate", "update")
        return
    # bundle / bundler
    if sub == "add" and rest and not local:
        result.add("uninspectable_candidate", "gem")
    elif sub in ("update", "up") and not local:
        for t in rest or ["<all-dependencies>"]:
            result.add("uninspectable_candidate", f"update:{t}")


# ── composer (PHP) ──────────────────────────────────────────────────
# Exec vector: composer.json `scripts` (pre/post-install-cmd, post-
# autoload-dump, …) run arbitrary shell at install — npm-like. require /
# update re-resolve and fetch; --no-scripts disables the script vector.
def _scan_composer_json(root: Path, result: XrayResult, no_scripts: bool) -> None:
    raw = _read(root, "composer.json")
    if raw is None:
        return
    result.manifests.append("composer.json")
    try:
        obj = json.loads(raw)
    except Exception:
        return
    if not isinstance(obj, dict):
        return
    if not no_scripts:
        scripts = obj.get("scripts")
        if isinstance(scripts, dict):
            for key, body in scripts.items():
                result.add("lifecycle_hook", str(key))
                bodies = body if isinstance(body, list) else [body]
                for b in bodies:
                    if isinstance(b, str):
                        _scan_danger(b, f"composer:{key}", result)
    repos = obj.get("repositories")
    items: list = []
    if isinstance(repos, dict):
        items = list(repos.values())
    elif isinstance(repos, list):
        items = repos
    for r in items:
        if not isinstance(r, dict):
            continue
        rtype = str(r.get("type", "")).lower()
        url = r.get("url")
        if rtype == "path":
            result.add("file_dep", "composer_repo")
        if isinstance(url, str):
            lu = url.lower()
            if lu.startswith("http://"):
                result.add("insecure_http", "composer_repo")
            elif rtype == "vcs" or lu.startswith(("git@", "git://")):
                result.add("git_dep", "composer_repo")
            elif lu.startswith("https://"):
                result.add("network_dep", "composer_repo")


def _xray_composer(root: Path, args: list[str], result: XrayResult) -> None:
    positionals = [a for a in args if not a.startswith("-")]
    no_scripts = "--no-scripts" in {a.lower() for a in args if a.startswith("-")}
    sub = positionals[0].lower() if positionals else ""
    rest = positionals[1:]
    _scan_composer_json(root, result, no_scripts)
    if sub == "require" and rest:
        result.add("uninspectable_candidate", "package")
    elif sub in ("update", "upgrade"):
        for t in rest or ["<all-dependencies>"]:
            result.add("uninspectable_candidate", f"update:{t}")
    elif sub == "create-project":
        result.add("uninspectable_candidate", "create_project")


# ── editor extensions (VS Code family) + VSIX / vsce / ovsx ─────────
# Jurisdiction: future-sight governs AGENT-INVOKED commands only. An
# editor extension runs arbitrary code inside the editor, so an agent
# command that installs/updates one is in scope and fails closed when the
# extension code cannot be inspected ahead (it never can — we do not fetch
# or unzip the extension). It does NOT and must NOT claim control over the
# editor's own BACKGROUND auto-updater or already-running host processes:
# those produce no command for us to gate, so they are simply out of
# scope by construction. Bare editor launches (`code .`, `code --version`,
# `code tunnel`) install nothing → no node.
# editor binaries + control-plane findings are sourced from anticoup
# (imported at top) — single source of truth for the anti-coup law.


def _xray_editor_ext(binary: str, args: list[str], result: XrayResult) -> None:
    install_targets: list[str] = []
    update_all = False
    i = 0
    while i < len(args):
        a = args[i]
        al = a.lower()
        if al == "--install-extension" and i + 1 < len(args):
            install_targets.append(args[i + 1])
            i += 2
            continue
        if al.startswith("--install-extension="):
            install_targets.append(a.split("=", 1)[1])
        elif al == "--update-extensions":
            update_all = True
        i += 1
    for t in install_targets:
        if t.lower().endswith(".vsix"):
            # a packaged extension (local or downloaded .vsix) — its code
            # runs in the editor and is not inspected → fail closed.
            result.add("uninspectable_candidate", "vsix")
        else:
            # a marketplace extension id → arbitrary editor code, unseen.
            result.add("uninspectable_candidate", "extension")
    if update_all:
        # explicit agent-invoked bulk extension update (NOT the editor's
        # background auto-updater) → re-resolves newer extension code.
        result.add("uninspectable_candidate", "extension_update")


# vsce package/publish and ovsx publish run the npm `vscode:prepublish`
# build script (arbitrary code) and then ship/upload the .vsix.
_VSCE_SCRIPT_KEYS = frozenset(
    {
        "vscode:prepublish",
        "vscode:postpublish",
        "vscode:uninstall",
        "prepublishonly",
        "prepublish",
        "prepare",
    },
)


def _scan_vsce_scripts(root: Path, result: XrayResult) -> None:
    raw = _read(root, "package.json")
    if raw is None:
        return
    result.manifests.append("package.json")
    try:
        obj = json.loads(raw)
    except Exception:
        return
    if not isinstance(obj, dict):
        return
    scripts = obj.get("scripts")
    if not isinstance(scripts, dict):
        return
    for key, body in scripts.items():
        if key.lower() in _VSCE_SCRIPT_KEYS and isinstance(body, str):
            result.add("lifecycle_hook", str(key))
            _scan_danger(body, f"vsce:{key}", result)


def _xray_vsce(binary: str, args: list[str], root: Path, result: XrayResult) -> None:
    positionals = [a for a in args if not a.startswith("-")]
    sub = positionals[0].lower() if positionals else ""
    if binary == "vsce":
        if sub in ("package", "publish", "ls"):
            _scan_vsce_scripts(root, result)
        if sub == "publish":
            # builds (prepublish) + uploads to the marketplace.
            result.add("uninspectable_candidate", "vsce_publish")
    elif binary == "ovsx":
        if sub in ("publish", "create-namespace"):
            _scan_vsce_scripts(root, result)
            result.add("uninspectable_candidate", "ovsx_publish")


# ── control-plane / power-changing mutations (AIDOCS jurisdiction) ──
# Detection lives in the canonical anticoup module (one source of truth);
# here we map its findings into the future-sight execution graph so the
# ai_run preflight audits + freezes them. The SAME findings drive the
# canonical check_tool verdict for every other tool surface. AIDOCS config
# / security flags / hooks / parallel exec paths → DENY; registry /
# settings edits → CONFIRM. Background host self-updaters are NOT covered
# (they issue no agent command).
def _xray_control_plane(seg: str, binary: str, args: list[str], result: XrayResult) -> None:
    for f in _coup_findings_for_segment(seg, binary, args):
        result.add(f.kind, f.label)


# ── dispatch ────────────────────────────────────────────────────────
_CHAIN_SPLIT = re.compile(r"[;&|\n]+|\|\||&&")
_BINARY_EXTS = (".exe", ".cmd", ".bat", ".ps1")


def expand_execution_graph(command: str, project_root: Path) -> XrayResult:
    """Read the manifests the command would consume and enumerate the
    hidden execution graph. Pure + bounded; reads only, never executes.
    """
    result = XrayResult()
    if not command or project_root is None:
        return result.finalize()
    try:
        root = Path(project_root)
    except Exception:
        return result.finalize()
    for seg in _CHAIN_SPLIT.split(command):
        toks = seg.strip().split()
        if not toks:
            continue
        binary = toks[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        for ext in _BINARY_EXTS:  # npm.cmd / code.cmd / vsce.exe on Windows
            if binary.endswith(ext):
                binary = binary[: -len(ext)]
                break
        args = toks[1:]
        try:
            # control-plane / power-changing mutations apply to ANY binary.
            _xray_control_plane(seg, binary, args, result)
            if binary in ("npm", "pnpm", "yarn", "bun", "npx"):
                _xray_npm(root, result)
                if binary in ("npm", "pnpm", "yarn", "bun"):
                    _xray_npm_candidates(root, binary, args, result)
            elif binary in ("pip", "pip3", "poetry", "uv", "pipenv"):
                _xray_python_candidates(root, binary, args, result)
            elif binary == "pipx":
                _xray_pip(root, args, result)
            elif binary in ("make", "gmake"):
                _xray_make(root, result)
            elif binary in ("docker", "podman"):
                _xray_docker(root, result)
            elif binary in ("dotnet", "nuget"):
                _xray_dotnet_candidates(root, binary, args, result)
            elif binary == "cargo":
                _xray_cargo(root, args, result)
            elif binary == "go":
                _xray_go(root, args, result)
            elif binary in ("gem", "bundle", "bundler"):
                _xray_ruby(root, binary, args, result)
            elif binary == "composer":
                _xray_composer(root, args, result)
            elif binary in _EDITOR_BINARIES:
                _xray_editor_ext(binary, args, result)
            elif binary in ("vsce", "ovsx"):
                _xray_vsce(binary, args, root, result)
        except Exception:
            # An inspector raised mid-flight — the graph for this segment is
            # incomplete. Record it so enforcement fails closed; never
            # silently treat an inspection error as "nothing to see".
            result.inspection_failed = True
            continue
    return result.finalize()


def _safe_iterdir(root: Path) -> list[Path]:
    try:
        return list(root.iterdir())
    except Exception:
        return []


def merged_severity(name_severity: str, xray_severity: str) -> str:
    """X-ray escalates the name-based floor; never weakens it."""
    return (
        name_severity
        if _RANK.get(name_severity, 0) >= _RANK.get(xray_severity, 0)
        else xray_severity
    )


def preflight_severity(
    name_severity: str,
    xray_result: XrayResult | None,
    *,
    enforce: bool,
) -> tuple[str, bool]:
    """Combine name + x-ray severity for the ai_run preflight, failing
    CLOSED when the x-ray could not complete its inspection.

    A validator failure (the inspector raised, or ``xray_result`` is None)
    must never fail open: under enforcement it escalates an otherwise
    none/confirm verdict to at least confirm (operator freeze), while a
    name-based DENY is preserved. Returns (severity, xray_failed).
    """
    xray_failed = xray_result is None or xray_result.inspection_failed
    xray_sev = xray_result.severity if xray_result is not None else SEV_NONE
    sev = merged_severity(name_severity, xray_sev)
    if xray_failed and enforce and sev != SEV_DENY:
        sev = SEV_CONFIRM
    return sev, xray_failed
