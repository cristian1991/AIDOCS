"""Prove the PUBLIC EXPORT is COHERENT, not merely leak-free.

WHY THIS EXISTS (operator ruling, 2026-07-28)
=============================================
``core/scripts/verify-public-tree.py`` answers exactly one question: does the
published tree contain a path from the denylist?  That is the *secrecy*
property.  It says nothing about the *coherence* property — whether the app
that remains, once every non-allowlisted file is gone, still runs and still
tells the truth about what it can do.

The operator's words: "make sure the code still works 120% without the parts
we took out (example: some messages still talk about ai_run on local even if
it's only webgate)".  Two distinct failure modes are in scope here and neither
is caught by a denylist:

  1. A kept module imports a dropped module.  The export builds, the denylist
     is clean, and the package explodes on first import in a user's venv.
  2. A kept tool DESCRIPTION advertises a capability that only the dropped
     private webgate could serve.  An agent reads tool descriptions and acts
     on them, so a stale description is not a cosmetic defect — it is a lie
     the machine believes.

MEASURE, NEVER ASSUME
=====================
Every check in this file operates on an EXPORTED TREE ON DISK, never on the
manifest.  A test that asserts "the manifest lists X" proves nothing about the
artifact: the manifest is the intent, the tree is the fact.  So layer 1 walks
the exported .py files, and layers 2-4 install and run what the export
produces.

Function-local imports are checked as well as module-level ones.  This is not
thoroughness for its own sake: this repo carries ~2900 function-local imports
across ~284 files (deliberately — the server's cold-start budget depends on
deferring heavy modules), so a module-level-only closure pass would inspect a
small minority of the actual edges and report a clean bill of health for a
tree that cannot serve a request.

THE FIVE LAYERS (weakest first; each is independently runnable)
==============================================================
L1 imports   Static import closure over the exported tree.  Deterministic, no
             install needed.  An import whose target is absent from the export
             but PRESENT in the private tree is an export defect by
             construction — the manifest dropped something still referenced.
L2 install   Build sdist+wheel FROM THE EXPORTED TREE, install into a fresh
             venv, import the package, run the CLI.  A traceback here ends the
             argument.
L3 surface   Enumerate the tool surface from that clean venv and diff it
             against the private tree's surface.  A tool present in one and
             absent in the other is the same class of defect as an advertised-
             but-absent capability.
L4 suite     Run a bounded slice of the PRIVATE test suite against the
             INSTALLED EXPORTED package.  Tests are excluded from the export,
             so they necessarily come from the private tree.  This layer is
             valid EVEN IF THE TESTS ARE NOT TRUSTED: a fabricated test can
             pass wrongly, but a missing import makes a test ERROR during
             collection.  For this defect class error-vs-pass carries the
             signal and assertion outcomes are ignored.
L5 dangling  Text sweep of the exported tree for the names of every dropped
             module, for capability words the public build may not be able to
             honour, and for private infrastructure path shapes.  Classified
             by severity, because prose in a design doc and a lie inside a
             tool description are not the same finding.

Usage:
    python core/scripts/verify_public_export_coherence.py            # L1+L5
    python core/scripts/verify_public_export_coherence.py --layers all
    python core/scripts/verify_public_export_coherence.py --layers 1,5 --json
    python core/scripts/verify_public_export_coherence.py --dest D:/tmp/pub --keep

``--dest`` MUST be outside the repo; the exporter empties it before writing,
and pointing it at a tracked directory would destroy the working tree.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_export  # noqa: E402

# ---------------------------------------------------------------------------
# Import-closure configuration
# ---------------------------------------------------------------------------

# Directories that behave as sys.path roots for this repo's own code.  These
# are not guesses: mcp/server is the wheel's package-dir (pyproject sets
# package-dir {"" = "server"}), and the standalone scripts under core/scripts
# and mcp/scripts do `sys.path.insert(0, Path(__file__).parent)` and then
# import their siblings by bare name, so each script directory is a root too.
_EXPLICIT_PATH_ROOTS = ("mcp/server", "mcp", ".")

# Import targets that are legitimately absent from a source tree and must not
# be reported: the package's own installed distribution name, and modules that
# only exist once something is built.
_IGNORE_TOPLEVEL = frozenset({
    "aidocs_mcp_build_marker",
})

# ---------------------------------------------------------------------------
# L5 sweep configuration
# ---------------------------------------------------------------------------

# Capability words that may only be servable by the dropped private webgate.
# A hit is not automatically a defect — the word may appear in a doc that
# correctly describes the cloud product.  The classifier below grades by WHERE
# the hit lands, because a tool description is machine-actionable and a
# markdown paragraph is not.
_CAPABILITY_WORDS = (
    "serveragent",
    "remoteagent",
    "webmcp",
    "mcp.codenexus.cloud",
    "gate-root",
)

# Private infrastructure shapes.  Any hit is at minimum an information leak
# about the operator's own deployment, regardless of file type.
_PRIVATE_SHAPES = (
    "159.195.",
    "codenexus-dev-root",
    "aidocs_release_ed25519",
    "Backups/configs",
    "/opt/aidocs",
)

# Extensions worth sweeping.  Binary and generated assets are skipped so the
# report is not drowned in base64 noise.
_SWEEP_EXTS = frozenset({
    ".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".sh", ".ps1", ".cmd", ".bat", ".ts", ".tsx", ".js", ".jsx", ".html",
    ".css", ".sql", ".rs", ".cs",
})

_SKIP_DIR_PARTS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "target", "dist",
    "build", ".MEMORY", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


# ---------------------------------------------------------------------------
# Results plumbing
# ---------------------------------------------------------------------------

@dataclass
class LayerResult:
    name: str
    ok: bool
    summary: str
    findings: list[dict] = field(default_factory=list)
    skipped: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "layer": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "summary": self.summary,
            "findings": self.findings,
            "detail": self.detail[-8000:],
        }


def _iter_files(root: Path, exts: frozenset[str] | None = None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_PARTS]
        for name in filenames:
            p = Path(dirpath) / name
            if exts is not None and p.suffix.lower() not in exts:
                continue
            yield p


# ---------------------------------------------------------------------------
# LAYER 1 — import closure
# ---------------------------------------------------------------------------

def _path_roots(tree: Path, files: list[Path]) -> list[Path]:
    """sys.path roots for first-party resolution, in precedence order.

    Beyond the explicit roots, every directory that contains a bare .py script
    is treated as a root.  Reason: a script that lives next to its helper and
    imports it by bare name resolves through its own directory, and if only
    package roots were considered every such edge would be misreported as an
    unresolved third-party import, burying the real findings.

    ``files`` is supplied by the caller rather than re-walked here, so the
    PRIVATE side can pass its git-tracked list and never see an untracked
    stale venv (see _module_index for the measured incident).
    """
    roots: list[Path] = []
    for rel in _EXPLICIT_PATH_ROOTS:
        p = (tree / rel).resolve()
        if p.is_dir():
            roots.append(p)
    seen = set(roots)
    for py in files:
        d = py.parent.resolve()
        if d not in seen:
            seen.add(d)
            roots.append(d)
    return roots


def _module_index(tree: Path, files: list[Path] | None = None,
                  ) -> tuple[set[str], set[Path]]:
    """Every dotted module name importable from ``tree`` via its path roots.

    Returns (dotted names, package directories).  A directory counts as a
    package for dotted-name purposes even without __init__.py, because the
    exporter can strip a namespace package's marker files while leaving the
    submodules, and reporting those submodules as missing would be wrong.

    ``files`` lets the caller supply the file list.  The PRIVATE side must
    supply git-tracked files only.  Measured reason: this repo carries
    ``mcp/.venv.py312-backup/`` — a stale interpreter-migration venv that the
    ``.venv`` directory-name skip does not match.  Indexing it made every
    third-party top-level (fastmcp, pydantic, tomli, cryptography,
    tree_sitter) look like a first-party module the export had dropped, which
    turned 40+ ordinary dependency imports into false EXPORT_DEFECTs and made
    the walk take twenty minutes.  git-tracked is also exactly the basis the
    exporter selects from, so the two sides are compared on equal terms.
    """
    if files is None:
        files = list(_iter_files(tree, frozenset({".py"})))
    roots = _path_roots(tree, files)
    names: set[str] = set()
    pkgdirs: set[Path] = set()
    for py in files:
        rp = py.resolve()
        for root in roots:
            try:
                rel = rp.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.parts)
            if parts[-1] == "__init__.py":
                parts = parts[:-1]
                if parts:
                    pkgdirs.add(rp.parent)
            else:
                parts[-1] = parts[-1][:-3]
            if not parts:
                continue
            names.add(".".join(parts))
            for i in range(1, len(parts)):
                names.add(".".join(parts[:i]))
    for py in files:
        pkgdirs.add(py.parent.resolve())
    return names, pkgdirs


def _tracked_py(root: Path) -> list[Path]:
    """git-tracked .py files under ``root`` — the private-side index basis."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                         capture_output=True, text=True, check=True)
    res: list[Path] = []
    for rel in out.stdout.split("\0"):
        if rel.endswith(".py"):
            p = root / rel
            if p.is_file():
                res.append(p)
    return res


def _stdlib_names() -> frozenset[str]:
    extra = {"pkg_resources", "setuptools", "pip", "wheel", "_typeshed"}
    return frozenset(set(sys.stdlib_module_names) | extra)


def _site_packages_toplevels(interpreter_dirs: list[Path]) -> set[str]:
    found: set[str] = set()
    for sp in interpreter_dirs:
        if not sp.is_dir():
            continue
        for entry in sp.iterdir():
            n = entry.name
            if n.endswith((".dist-info", ".egg-info", ".egg-link", ".pth")):
                continue
            if entry.is_dir():
                found.add(n)
            elif entry.suffix in (".py", ".pyd", ".so"):
                found.add(n.split(".")[0])
    return found


def _external_toplevels(private_root: Path) -> frozenset[str]:
    """Top-level names that are third-party rather than first-party.

    Three sources, unioned, because no single one is sufficient:
      * the PRIVATE dev venv's site-packages (mcp/.venv) — the environment the
        code is actually developed and gated against;
      * the running interpreter's site-packages;
      * the distribution names declared in mcp/pyproject.toml, normalised to
        import form, so a dependency that is merely DECLARED and not installed
        anywhere on this box is still recognised as external.
    A name landing here is not an export question at all — layer 2 is what
    proves the declared dependency set is complete.
    """
    dirs: list[Path] = []
    for base in (private_root / "mcp" / ".venv", Path(sys.prefix)):
        for sub in ("Lib/site-packages", "lib/site-packages"):
            dirs.append(base / sub)
        for lib in (base / "lib").glob("python3.*/site-packages"):
            dirs.append(lib)
    for key in ("purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            dirs.append(Path(p))
    found = _site_packages_toplevels(dirs)

    pyproject = private_root / "mcp" / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        proj = data.get("project", {})
        reqs = list(proj.get("dependencies", []) or [])
        for group in (proj.get("optional-dependencies", {}) or {}).values():
            reqs.extend(group or [])
        for spec in reqs:
            dist = re.split(r"[<>=!~;\[\s]", str(spec).strip(), 1)[0]
            if not dist:
                continue
            found.add(dist.replace("-", "_").lower())
            found.add(dist.replace("-", "_"))
            found.add(dist.lower())
    return frozenset(found)


def _resolve_relative(py: Path, tree: Path, level: int, module: str | None,
                      pkgdirs: set[Path]) -> str | None:
    """Turn a `from ..x import y` into a filesystem probe result.

    Returns None when it resolves, or a human-readable target when it does
    not.  Relative imports are resolved on the FILESYSTEM rather than through
    dotted names because that is what the interpreter does, and a package
    whose __init__.py was dropped by the export would otherwise silently
    resolve by name.
    """
    base = py.parent.resolve()
    for _ in range(level - 1):
        base = base.parent
    try:
        base.relative_to(tree.resolve())
    except ValueError:
        return f"{'.' * level}{module or ''} (escapes export root)"
    if not module:
        return None if base.is_dir() else f"{'.' * level} (missing package dir)"
    target = base
    for part in module.split("."):
        target = target / part
    if (target.with_suffix(".py")).is_file():
        return None
    if target.is_dir():
        return None
    return f"{'.' * level}{module}"


def layer1_import_closure(tree: Path, private_root: Path) -> LayerResult:
    pub_files = sorted(_iter_files(tree, frozenset({".py"})))
    pub_names, pub_pkgdirs = _module_index(tree, pub_files)
    priv_names, _ = _module_index(private_root, _tracked_py(private_root))
    stdlib = _stdlib_names()
    external = _external_toplevels(private_root)

    findings: list[dict] = []
    scanned = 0
    edges = 0
    local_edges = 0
    syntax_errors: list[dict] = []

    for py in pub_files:
        scanned += 1
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
            mod = ast.parse(src, filename=str(py))
        except SyntaxError as exc:
            syntax_errors.append({
                "file": py.relative_to(tree).as_posix(),
                "line": exc.lineno or 0,
                "error": str(exc),
            })
            continue

        # Function-local imports are the majority of edges in this repo, so
        # walk the whole tree and record depth instead of only body-level.
        func_lineno: set[int] = set()
        for node in ast.walk(mod):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        func_lineno.add(id(sub))

        for node in ast.walk(mod):
            if isinstance(node, ast.ImportFrom):
                edges += 1
                is_local = id(node) in func_lineno
                local_edges += int(is_local)
                if node.level:
                    unresolved = _resolve_relative(
                        py, tree, node.level, node.module, pub_pkgdirs)
                    if unresolved:
                        findings.append(_finding(
                            py, tree, node.lineno, unresolved, is_local,
                            priv_names, relative=True))
                    continue
                target = node.module or ""
                _check_absolute(target, py, tree, node.lineno, is_local,
                                pub_names, priv_names, stdlib, external,
                                findings)
            elif isinstance(node, ast.Import):
                is_local = id(node) in func_lineno
                for alias in node.names:
                    edges += 1
                    local_edges += int(is_local)
                    _check_absolute(alias.name, py, tree, node.lineno,
                                    is_local, pub_names, priv_names, stdlib,
                                    external, findings)

    hard = [f for f in findings if f["verdict"] == "EXPORT_DEFECT"]
    ok = not hard and not syntax_errors
    summary = (
        f"{scanned} .py files, {edges} import edges "
        f"({local_edges} function-local); "
        f"{len(hard)} EXPORT_DEFECT, "
        f"{len(findings) - len(hard)} unresolved-but-also-absent-privately, "
        f"{len(syntax_errors)} syntax errors"
    )
    return LayerResult("L1 import closure", ok, summary,
                       findings=hard + [f for f in findings
                                        if f["verdict"] != "EXPORT_DEFECT"]
                       + [{**s, "verdict": "SYNTAX_ERROR"}
                          for s in syntax_errors])


def _finding(py: Path, tree: Path, lineno: int, target: str, is_local: bool,
             priv_names: set[str], relative: bool = False) -> dict:
    # A relative import that fails in the export is judged an export defect
    # outright: relative targets live inside the package, so their absence can
    # only come from the manifest dropping a sibling.
    verdict = "EXPORT_DEFECT" if relative else "UNRESOLVED_BOTH"
    # Parenthesised deliberately: `not relative and A or B` binds as
    # `(not relative and A) or B`, which promoted every unresolved
    # third-party import to EXPORT_DEFECT the moment its top-level name
    # appeared anywhere in the private index.
    if relative or (target in priv_names or target.split(".")[0] in priv_names):
        verdict = "EXPORT_DEFECT"
    return {
        "file": py.relative_to(tree).as_posix(),
        "line": lineno,
        "target": target,
        "scope": "function-local" if is_local else "module-level",
        "verdict": verdict,
    }


def _check_absolute(target: str, py: Path, tree: Path, lineno: int,
                    is_local: bool, pub_names: set[str], priv_names: set[str],
                    stdlib: frozenset[str], external: frozenset[str],
                    findings: list[dict]) -> None:
    if not target:
        return
    top = target.split(".")[0]
    if top in stdlib or top in _IGNORE_TOPLEVEL:
        return
    if target in pub_names:
        return
    # `from pkg.mod import name` where pkg.mod is a package whose __init__ was
    # dropped still resolves if the directory survives.
    if top in pub_names and target not in priv_names:
        return
    if top in external:
        # Present as an installed dependency; not an export-tree question.
        # Still flag when the FULL dotted target is a first-party module that
        # the export dropped and a same-named dependency happens to shadow it.
        if target in priv_names and target not in pub_names:
            findings.append(_finding(py, tree, lineno, target, is_local,
                                     priv_names))
        return
    if target in priv_names or top in priv_names:
        findings.append(_finding(py, tree, lineno, target, is_local,
                                 priv_names))
        return
    findings.append({
        "file": py.relative_to(tree).as_posix(),
        "line": lineno,
        "target": target,
        "scope": "function-local" if is_local else "module-level",
        "verdict": "UNRESOLVED_BOTH",
    })


# ---------------------------------------------------------------------------
# LAYER 2 — clean-venv install from the exported tree
# ---------------------------------------------------------------------------

# Importing the package entrypoint proves almost nothing: `import aidocs_mcp`
# touches a handful of modules, and this repo defers most imports to call time.
# The module-level `from .outer_gate_project_acl import ...` in
# outer_gate_execution_authority.py is invisible to an entrypoint import and
# only detonates when something first reaches that module — which, for a user,
# is the middle of a tool call. So walk the INSTALLED package and import every
# module individually. This is the layer that turns L1's static claim into an
# executed fact.
_IMPORT_ALL_SNIPPET = r"""
import importlib, json, pkgutil, sys, traceback
import aidocs_mcp
failures = []
ok = 0
for mod in pkgutil.walk_packages(aidocs_mcp.__path__, aidocs_mcp.__name__ + "."):
    name = mod.name
    try:
        importlib.import_module(name)
        ok += 1
    except BaseException as exc:
        tb = traceback.format_exc().strip().splitlines()
        failures.append({
            "module": name,
            "error": f"{type(exc).__name__}: {exc}",
            "last": tb[-1] if tb else "",
        })
print("RESULT " + json.dumps({"imported_ok": ok, "failures": failures}))
"""


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900,
         env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, f"NOT FOUND: {exc}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _console_script(work: Path, name: str) -> Path:
    d = work / "venv" / ("Scripts" if os.name == "nt" else "bin")
    exe = d / (f"{name}.exe" if os.name == "nt" else name)
    return exe if exe.exists() else d / name


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")


def layer2_source_import_all(tree: Path, work: Path,
                             private_root: Path) -> LayerResult:
    """Import every EXPORTED module using the dev venv's dependencies.

    Independent of wheel-building, which needs network and minutes and can fail
    for reasons unrelated to coherence. Here the only variable is WHICH SOURCE
    FILES EXIST: interpreter and third-party deps come from the private
    mcp/.venv, and PYTHONPATH points at the EXPORTED tree. So any ImportError
    is attributable to the export having dropped a file — exactly the question.

    STATE ISOLATION: importing ~400 modules would otherwise run module-init
    code against the operator's real ~/.aidocs (identity DB, empire DB, config
    store). HOME/USERPROFILE and AIDOCS_EMPIRE_DB are redirected into the
    throwaway workspace so a verification run can never mutate live sovereign
    state. Nothing in this harness may write to the operator's home.
    """
    vpy = _private_interpreter(private_root)
    snip = work / "_import_all_src.py"
    snip.write_text(_IMPORT_ALL_SNIPPET, encoding="utf-8")
    sandbox = work / "fakehome"
    (sandbox / ".aidocs").mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "mcp" / "server")
    env["HOME"] = str(sandbox)
    env["USERPROFILE"] = str(sandbox)
    env["AIDOCS_EMPIRE_DB"] = str(sandbox / ".aidocs" / "empire.sqlite3")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    rc, out = _run([str(vpy), str(snip)], timeout=1800, env=env)
    findings: list[dict] = []
    imported_ok = 0
    parsed = False
    for line in out.splitlines():
        if line.startswith("RESULT "):
            try:
                data = json.loads(line[len("RESULT "):])
            except json.JSONDecodeError:
                continue
            parsed = True
            imported_ok = data.get("imported_ok", 0)
            for f in data.get("failures", []):
                findings.append({**f, "verdict": "EXPORT_DEFECT"})
    if not parsed:
        return LayerResult(
            "L2a exported-source import (all modules)", False,
            f"probe did not report (rc={rc}) — interpreter {vpy}",
            detail=out[-6000:])
    return LayerResult(
        "L2a exported-source import (all modules)", not findings,
        f"{imported_ok} exported modules imported cleanly, "
        f"{len(findings)} FAILED (interpreter {vpy.name}, deps from mcp/.venv)",
        findings=findings, detail=out[-8000:])


def layer2_clean_install(tree: Path, work: Path) -> LayerResult:
    log: list[str] = []
    pkg_dir = tree / "mcp"
    if not (pkg_dir / "pyproject.toml").is_file():
        return LayerResult("L2 clean-venv install", False,
                           "mcp/pyproject.toml absent from the export",
                           skipped=False)

    dist = work / "dist"
    rc, out = _run([sys.executable, "-m", "build", "--outdir", str(dist)],
                   cwd=pkg_dir, timeout=1500)
    log.append(f"$ python -m build (exported mcp/)\n[rc={rc}]\n{out}")
    if rc != 0:
        return LayerResult("L2 clean-venv install", False,
                           f"build FAILED from the exported tree (rc={rc})",
                           detail="\n".join(log))

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    log.append(f"artifacts: {[w.name for w in wheels + sdists]}")
    if not wheels:
        return LayerResult("L2 clean-venv install", False,
                           "build produced no wheel",
                           detail="\n".join(log))

    venv = work / "venv"
    rc, out = _run([sys.executable, "-m", "venv", str(venv)], timeout=300)
    log.append(f"$ venv create\n[rc={rc}]\n{out}")
    if rc != 0:
        return LayerResult("L2 clean-venv install", False, "venv create failed",
                           detail="\n".join(log))
    vpy = _venv_python(venv)

    rc, out = _run([str(vpy), "-m", "pip", "install", "--no-input",
                    str(wheels[-1])], timeout=1800)
    log.append(f"$ pip install {wheels[-1].name}\n[rc={rc}]\n{out[-4000:]}")
    if rc != 0:
        return LayerResult("L2 clean-venv install", False,
                           f"pip install of the exported wheel FAILED (rc={rc})",
                           detail="\n".join(log))

    findings: list[dict] = []
    probes = [
        ("import aidocs_mcp", [str(vpy), "-c", "import aidocs_mcp; print(aidocs_mcp.__file__)"]),
        ("import server module", [str(vpy), "-c", "import aidocs_mcp.mcp_server as m; print(len(dir(m)))"]),
        # The console script, not `python -m aidocs_mcp`: the package ships no
        # __main__, so `-m` fails for a reason that says nothing about the
        # export. That false FAIL was reported once before being caught here.
        ("cli --version", [str(_console_script(work, "aidocs")), "--version"]),
    ]
    for label, cmd in probes:
        rc, out = _run(cmd, timeout=600)
        log.append(f"$ {label}\n[rc={rc}]\n{out[-3000:]}")
        if rc != 0:
            findings.append({"probe": label, "rc": rc,
                             "tail": out[-1200:], "verdict": "FAIL"})

    # The decisive probe: import EVERY module of the installed package.
    snip = work / "_import_all.py"
    snip.write_text(_IMPORT_ALL_SNIPPET, encoding="utf-8")
    rc, out = _run([str(vpy), str(snip)], timeout=1200)
    log.append(f"$ import every module\n[rc={rc}]\n{out[-6000:]}")
    imported_ok = 0
    for line in out.splitlines():
        if line.startswith("RESULT "):
            try:
                data = json.loads(line[len("RESULT "):])
            except json.JSONDecodeError:
                continue
            imported_ok = data.get("imported_ok", 0)
            for f in data.get("failures", []):
                findings.append({**f, "probe": "import-every-module",
                                 "verdict": "FAIL"})

    ok = not findings
    return LayerResult(
        "L2 clean-venv install", ok,
        f"wheel built + installed into a clean venv; {imported_ok} modules "
        f"imported cleanly; "
        + ("all probes passed" if ok else f"{len(findings)} probe(s) FAILED"),
        findings=findings, detail="\n".join(log))


# ---------------------------------------------------------------------------
# LAYER 3 — tool surface diff
# ---------------------------------------------------------------------------

# Tool enumeration must BUILD a server, not read a module global: tools are
# registered by create_server(), so a module-level `mcp` object can legitimately
# hold zero of them. The first version of this snippet did exactly that and
# reported "public=0 private=0 tools; 0 asymmetric" as a PASS — a vacuous
# comparison of two empty sets. An enumeration that finds nothing is
# INCONCLUSIVE, never clean, and layer 3 now refuses to pass on an empty set.
_SURFACE_SNIPPET = r"""
import asyncio, json, sys
out = {"tools": [], "how": None}
try:
    from aidocs_mcp import mcp_server as ms
except Exception as exc:
    print("RESULT " + json.dumps({"error": f"import failed: {type(exc).__name__}: {exc}"}))
    sys.exit(0)

def _names_from(obj):
    if isinstance(obj, dict):
        return set(obj.keys())
    if isinstance(obj, (list, tuple, set)):
        got = set()
        for t in obj:
            n = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if n:
                got.add(n)
        return got
    return set()

srv = None
for factory in ("create_server", "build_server", "make_server"):
    fn = getattr(ms, factory, None)
    if callable(fn):
        for kwargs in ({"tools_profile": "full"}, {}):
            try:
                srv = fn(**kwargs)
                out["how"] = f"{factory}({kwargs})"
                break
            except Exception:
                continue
    if srv is not None:
        break
if srv is None:
    srv = getattr(ms, "mcp", None) or getattr(ms, "server", None)
    out["how"] = "module global"

names = set()
if srv is not None:
    getter = getattr(srv, "get_tools", None)
    if callable(getter):
        try:
            got = getter()
            if asyncio.iscoroutine(got):
                got = asyncio.run(got)
            names |= _names_from(got)
        except Exception as exc:
            out["get_tools_error"] = f"{type(exc).__name__}: {exc}"
    mgr = getattr(srv, "_tool_manager", None)
    if mgr is not None and not names:
        for attr in ("_tools", "tools"):
            names |= _names_from(getattr(mgr, attr, None))
out["tools"] = sorted(names)
print("RESULT " + json.dumps(out))
"""


def layer3_surface(tree: Path, work: Path, private_root: Path) -> LayerResult:
    """Compare the tool surface the EXPORT can emit with the private one.

    Runs the same introspection snippet twice — once under the clean venv
    holding the exported wheel, once under the private tree — and diffs.  A
    tool only one side can register means the two builds do not describe the
    same product.
    """
    vpy = _venv_python(work / "venv")
    if not vpy.is_file():
        return LayerResult("L3 tool surface", False,
                           "no clean venv (run layer 2 first)", skipped=True)
    snip = work / "_surface.py"
    snip.write_text(_SURFACE_SNIPPET, encoding="utf-8")

    rc_pub, out_pub = _run([str(vpy), str(snip)], timeout=900)
    priv_py = _private_interpreter(private_root)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(private_root / "mcp" / "server")
    rc_priv, out_priv = _run([str(priv_py), str(snip)], timeout=900, env=env)

    def _parse(txt: str) -> dict:
        for line in reversed(txt.strip().splitlines()):
            line = line.strip()
            if line.startswith("RESULT "):
                line = line[len("RESULT "):]
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"error": "no json on stdout", "raw": txt[-1500:]}

    pub = _parse(out_pub)
    priv = _parse(out_priv)
    log = [f"[public rc={rc_pub}]\n{out_pub[-3000:]}",
           f"[private rc={rc_priv}] ({priv_py})\n{out_priv[-3000:]}"]

    if "error" in pub:
        return LayerResult("L3 tool surface", False,
                           f"exported package could not be introspected: {pub['error']}",
                           findings=[{"side": "public", **pub}],
                           detail="\n".join(log))
    if "error" in priv:
        return LayerResult("L3 tool surface", True,
                           "public surface enumerated; private side unavailable "
                           f"({priv.get('error')}) so no diff was possible",
                           findings=[{"side": "private", **priv,
                                      "verdict": "NOT_COMPARED"}],
                           detail="\n".join(log))

    p, q = set(pub["tools"]), set(priv["tools"])
    # An empty enumeration is INCONCLUSIVE. 0 == 0 is not agreement, and
    # reporting it as a pass is how an unmeasured seam gets called clean.
    if not p or not q:
        return LayerResult(
            "L3 tool surface", False,
            f"INCONCLUSIVE — enumerated public={len(p)} private={len(q)} "
            f"tools; an empty set cannot be diffed "
            f"(public how={pub.get('how')!r}, private how={priv.get('how')!r})",
            findings=[{"side": "public", "how": pub.get("how"),
                       "error": pub.get("get_tools_error"),
                       "verdict": "NOT_MEASURED"},
                      {"side": "private", "how": priv.get("how"),
                       "error": priv.get("get_tools_error"),
                       "verdict": "NOT_MEASURED"}],
            detail="\n".join(log))
    findings = (
        [{"tool": t, "verdict": "PRIVATE_ONLY"} for t in sorted(q - p)]
        + [{"tool": t, "verdict": "PUBLIC_ONLY"} for t in sorted(p - q)]
    )
    return LayerResult(
        "L3 tool surface",
        not findings,
        f"public={len(p)} private={len(q)} tools; {len(findings)} asymmetric",
        findings=findings, detail="\n".join(log))


def _private_interpreter(private_root: Path) -> Path:
    """The interpreter the private tree actually runs under.

    Prefers mcp/.venv: the pyproject pins a specific interpreter floor, and
    introspecting the private surface with a different interpreter than the
    one it is developed on would make an interpreter difference look like an
    export defect.
    """
    cand = private_root / "mcp" / ".venv"
    vp = _venv_python(cand)
    return vp if vp.is_file() else Path(sys.executable)


# ---------------------------------------------------------------------------
# LAYER 4 — private suite against the exported package
# ---------------------------------------------------------------------------

def layer4_private_suite(work: Path, private_root: Path,
                         slice_paths: list[str]) -> LayerResult:
    """Point private tests at the INSTALLED EXPORTED package.

    ONLY collection errors and import errors are treated as export defects.
    Assertion failures are reported but never fail the layer: a test may fail
    for reasons that have nothing to do with the export (machine state, an
    unrelated regression, or simply being a bad test), whereas an ERROR from a
    missing module can only mean the export dropped something the code needs.
    """
    vpy = _venv_python(work / "venv")
    if not vpy.is_file():
        return LayerResult("L4 private suite vs exported pkg", False,
                           "no clean venv (run layer 2 first)", skipped=True)
    # pytest-xdist is required because mcp/pyproject.toml's [tool.pytest]
    # addopts carries `-n --dist=loadgroup`; without the plugin pytest exits 4
    # (usage error) before collecting anything, which looks like a clean run.
    rc, out = _run([str(vpy), "-m", "pip", "install", "--no-input", "pytest",
                    "pytest-timeout", "pytest-xdist"], timeout=900)
    if rc != 0:
        return LayerResult("L4 private suite vs exported pkg", False,
                           "could not install pytest into the clean venv",
                           detail=out[-4000:])

    env = dict(os.environ)
    # No PYTHONPATH to mcp/server: the point is that the tests resolve
    # aidocs_mcp from the INSTALLED WHEEL, not from the private sources.
    env.pop("PYTHONPATH", None)
    # `-o addopts=` clears the repo's ini addopts so this probe controls its own
    # flags (the repo default fans out across xdist workers, which serialises
    # badly with a one-file slice and hides tracebacks).
    rc, out = _run([str(vpy), "-m", "pytest", "-p", "no:cacheprovider",
                    "-o", "addopts=", "-q", "--no-header", "--co",
                    *slice_paths],
                   cwd=private_root / "mcp", timeout=1200, env=env)
    collect_log = f"$ pytest --co (collection only)\n[rc={rc}]\n{out[-6000:]}"
    collect_errors = _extract_errors(out)

    rc2, out2 = _run([str(vpy), "-m", "pytest", "-p", "no:cacheprovider",
                      "-o", "addopts=", "-q", "--no-header", "--timeout=120",
                      *slice_paths],
                     cwd=private_root / "mcp", timeout=1800, env=env)
    run_log = f"$ pytest (run)\n[rc={rc2}]\n{out2[-8000:]}"
    run_errors = _extract_errors(out2)

    findings = ([{"phase": "collect", **e} for e in collect_errors]
                + [{"phase": "run", **e} for e in run_errors])
    hard = [f for f in findings if f["verdict"] == "EXPORT_DEFECT"]

    # pytest exit codes: 0 passed, 1 tests failed, 2 interrupted, 3 internal,
    # 4 USAGE ERROR, 5 no tests collected. Only 0 and 1 mean the suite actually
    # ran. Anything else means pytest never reached collection, so "0 import
    # errors" is an absence of measurement, not a clean result — and reporting
    # it as PASS is precisely how an unverified seam gets blessed. This fired
    # for real: rc=4 was first reported as a green layer.
    if rc2 not in (0, 1) or rc not in (0, 1, 5):
        return LayerResult(
            "L4 private suite vs exported pkg", False,
            f"INCONCLUSIVE — pytest never ran the slice "
            f"(collect rc={rc}, run rc={rc2}; 4=usage error, 3=internal, "
            f"5=nothing collected). No conclusion may be drawn.",
            findings=findings + [{"verdict": "NOT_MEASURED",
                                  "collect_rc": rc, "run_rc": rc2}],
            detail=collect_log + "\n\n" + run_log)

    return LayerResult(
        "L4 private suite vs exported pkg",
        not hard,
        f"collection rc={rc}, run rc={rc2}; "
        f"{len(hard)} import/collection error(s) implicating the export",
        findings=findings, detail=collect_log + "\n\n" + run_log)


_ERR_RE = re.compile(
    r"(ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]"
    r"|ImportError: cannot import name ['\"]([^'\"]+)['\"]"
    r"|E\s+ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"])")


def _extract_errors(out: str) -> list[dict]:
    seen: set[str] = set()
    res: list[dict] = []
    for m in _ERR_RE.finditer(out):
        text = m.group(0)
        if text in seen:
            continue
        seen.add(text)
        res.append({"error": text, "verdict": "EXPORT_DEFECT"})
    for line in out.splitlines():
        if line.startswith("ERROR ") or " errors" in line and "error" in line:
            if line.startswith("ERROR "):
                res.append({"error": line.strip(), "verdict": "REVIEW"})
    return res


# ---------------------------------------------------------------------------
# LAYER 5 — dangling-reference sweep
# ---------------------------------------------------------------------------

def _dropped_module_names(tree: Path, private_root: Path) -> list[str]:
    """Module stems the export dropped, measured by diffing the two trees.

    Derived from the filesystem rather than read out of SURGICAL_EXCLUDES, so
    the sweep stays correct if the manifest changes and cannot be fooled by a
    manifest entry that does not actually drop anything.

    TWO NOISE FILTERS, both from a measured failure. The first run of this
    sweep produced 1592 hits of which essentially none were real, because:

      * vendored ``third_party/mempalace/`` is "dropped" (it is not shipped by
        design) and contributes an entire generic namespace — `config`, `cli`,
        `service`, `metrics`, `registry`, `palace`, `base`, `ids`;
      * bare-stem SUBSTRING matching then hit `"version"` in every one of the
        thousands of lines of apps/aidocs-dashboard/package-lock.json.

    So: vendored and generated trees are excluded from the dropped set, and a
    stem must be DISTINCTIVE enough that a hit means something. A short common
    English word as a module name cannot be searched for textually — that is a
    limit of the method and is recorded in the report rather than papered over.
    """
    pub = {p.relative_to(tree).as_posix()
           for p in _iter_files(tree, frozenset({".py"}))}
    # git-tracked only: an untracked stale venv under the private root would
    # otherwise contribute thousands of third-party module stems as "dropped",
    # and the sweep would drown in hits for names like `abc` and `types`.
    priv = {p.relative_to(private_root).as_posix()
            for p in _tracked_py(private_root)}
    dropped = priv - pub
    stems: set[str] = set()
    for rel in dropped:
        name = Path(rel).stem
        if name in ("__init__", "conftest") or name.startswith("test_"):
            continue
        if rel.startswith("mcp/tests/") or "/tests/" in rel:
            continue
        # Vendored dependency source is not "ours" and its module names are
        # generic; excluding it is what separates signal from 1500 lockfile
        # hits.
        if rel.startswith("third_party/") or "/third_party/" in rel:
            continue
        if name.startswith("_"):
            continue
        # Distinctiveness: a compound identifier, or a long one.  `outer_gate`
        # and every `outer_gate_*` clear this; `sync`, `cli`, `wal` do not.
        if "_" not in name and "-" not in name and len(name) < 12:
            continue
        stems.add(name)
    return sorted(stems)


# Generated/vendored artifacts are excluded from the sweep: a lockfile or a
# minified bundle cannot contain a hand-written capability claim, and matching
# them buries the findings that can.
_SWEEP_SKIP_FILES = (
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
    "poetry.lock", "uv.lock", "composer.lock",
)


def _sweep_skip(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    if base in _SWEEP_SKIP_FILES:
        return True
    if base.endswith((".min.js", ".min.css", ".map")):
        return True
    # Built dashboard bundles carry hashed filenames and no authored prose.
    return "/assets/" in rel and re.search(r"-[A-Za-z0-9_]{8}\.(js|css)$", rel) is not None


def soul_exposure_probe(tree: Path) -> LayerResult:
    """Can any SOUL CONTENT or sovereign material reach the export?

    Soul machinery (empire_soul_gate.py, ai_soul, skill_store soul handling)
    is PUBLIC by operator decision and is NOT touched here.  The question is
    only whether soul CONTENT — the rows an operator's sovereign identity
    lives in — can ride along.  Content is claimed to live exclusively in
    ~/.aidocs/empire.sqlite3 (identity_db.py:21), i.e. machine-global user-home
    state outside the repo.  That claim is not evidence, so this probe looks
    for the carriers that could contradict it: a shipped database file, a
    soul-named artifact, a .MEMORY tree, or SQL that writes literal soul rows.
    """
    findings: list[dict] = []
    db_ext = {".sqlite3", ".sqlite", ".db", ".db3"}
    for p in _iter_files(tree):
        rel = p.relative_to(tree).as_posix()
        if p.suffix.lower() in db_ext:
            findings.append({"path": rel, "verdict": "DB_FILE_SHIPPED"})
        low = rel.lower()
        if "soul" in low:
            findings.append({
                "path": rel,
                "verdict": ("SOUL_MACHINERY_PUBLIC_BY_DECISION"
                            if low.endswith(".py") else "SOUL_ARTIFACT"),
            })
        if "/.memory/" in "/" + low or low.startswith(".memory/"):
            findings.append({"path": rel, "verdict": "MEMORY_TREE_SHIPPED"})

    seed_re = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+souls\b", re.I)
    for p in _iter_files(tree, frozenset({".py", ".sql"})):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if seed_re.search(line):
                findings.append({
                    "path": p.relative_to(tree).as_posix(), "line": i,
                    "verdict": "SOUL_ROW_WRITE_SITE",
                })

    bad = [f for f in findings
           if f["verdict"] in ("DB_FILE_SHIPPED", "SOUL_ARTIFACT",
                               "MEMORY_TREE_SHIPPED")]
    return LayerResult(
        "Souls — content exposure probe", not bad,
        f"{len(findings)} soul/state-related paths; {len(bad)} that could "
        f"carry CONTENT rather than machinery",
        findings=findings)


def _classify(rel: str, line: str, needle: str) -> str:
    """Grade a hit by how much damage it can do.

    TOOL_DESCRIPTION is the worst class and gets its own verdict: an agent
    reads those strings and acts on them, so a description naming a capability
    the public build cannot serve produces wrong ACTIONS, not just confusion.
    A refusal/error message is next, because a user is told to do something
    impossible.  Prose in documentation is last.
    """
    low = line.strip().lower()
    if needle in _PRIVATE_SHAPES:
        return "PRIVATE_INFRA_LEAK"
    if rel.endswith(".py"):
        if re.search(r'(description|help|docstring)\s*=', low):
            return "TOOL_DESCRIPTION"
        if re.search(r'(raise|return)\s+.*(error|refus|denied|not allowed)', low):
            return "REFUSAL_MESSAGE"
        if low.startswith("#") or low.startswith('"""') or low.startswith("'''"):
            return "CODE_COMMENT"
        if re.search(r'''["'].*%s''' % re.escape(needle), low):
            return "USER_FACING_STRING"
        return "CODE_IDENTIFIER"
    if rel.endswith(".md") or rel.endswith(".txt"):
        return "DOC_PROSE"
    return "OTHER"


def layer5_dangling(tree: Path, private_root: Path,
                    max_hits_per_needle: int = 40) -> LayerResult:
    dropped = _dropped_module_names(tree, private_root)
    needles = [(n, "dropped-module") for n in dropped]
    needles += [(n, "capability-word") for n in _CAPABILITY_WORDS]
    needles += [(n, "private-shape") for n in _PRIVATE_SHAPES]

    counts: dict[str, int] = {}
    findings: list[dict] = []
    lowered = {n.lower(): (n, kind) for n, kind in needles}

    for f in sorted(_iter_files(tree, _SWEEP_EXTS)):
        rel = f.relative_to(tree).as_posix()
        if _sweep_skip(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        present = [v for k, v in lowered.items() if k in low]
        if not present:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            ll = line.lower()
            for needle, kind in present:
                if needle.lower() not in ll:
                    continue
                counts[needle] = counts.get(needle, 0) + 1
                if counts[needle] > max_hits_per_needle:
                    continue
                findings.append({
                    "needle": needle,
                    "needle_kind": kind,
                    "file": rel,
                    "line": lineno,
                    "text": line.strip()[:220],
                    "verdict": _classify(rel, line, needle),
                })

    worst = [f for f in findings
             if f["verdict"] in ("PRIVATE_INFRA_LEAK", "TOOL_DESCRIPTION",
                                 "REFUSAL_MESSAGE")]
    return LayerResult(
        "L5 dangling reference sweep",
        not worst,
        f"{len(dropped)} dropped module stems + {len(_CAPABILITY_WORDS)} "
        f"capability words + {len(_PRIVATE_SHAPES)} private shapes swept; "
        f"{len(findings)} hits, {len(worst)} in an actionable class",
        findings=findings,
        detail="hit counts: " + json.dumps(counts, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_DEFAULT_SLICE = [
    "tests/host/test_interpreter_pin.py",
    "tests/unit",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prove the public export is coherent, not just leak-free.")
    ap.add_argument("--dest", default="",
                    help="export dir (MUST be outside the repo). "
                         "Default: a temp dir, removed unless --keep.")
    ap.add_argument("--layers", default="1,5",
                    help="comma list of 1..5, or 'all'")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp workspace for inspection")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ref", default="",
                    help="git ref to export (default: working tree)")
    ap.add_argument("--slice", default=",".join(_DEFAULT_SLICE),
                    help="L4 pytest targets, relative to mcp/")
    args = ap.parse_args(argv)

    layers = ({1, 2, 3, 4, 5} if args.layers.strip() == "all"
              else {int(x) for x in args.layers.split(",") if x.strip()})

    root = public_export.repo_root(Path(__file__).resolve().parent)
    work = Path(args.dest).resolve() if args.dest else Path(
        tempfile.mkdtemp(prefix="aidocs-pubcoh-"))
    try:
        work.relative_to(root)
    except ValueError:
        pass
    else:
        print(f"REFUSING: --dest {work} is inside the repo {root}; the "
              f"exporter empties it and would destroy the working tree.",
              file=sys.stderr)
        return 2
    work.mkdir(parents=True, exist_ok=True)
    tree = work / "tree"

    results: list[LayerResult] = []
    files = public_export.export(root, tree, args.ref)
    denied = public_export.verify_no_denied(tree)
    results.append(LayerResult(
        "L0 export built", not denied,
        f"{len(files)} files exported to {tree}; "
        + ("denylist clean" if not denied else f"{len(denied)} DENIED PATHS"),
        findings=[{"path": d, "verdict": "DENYLIST_HIT"} for d in denied]))

    if 1 in layers:
        results.append(layer1_import_closure(tree, root))
    if 2 in layers:
        results.append(layer2_source_import_all(tree, work, root))
        results.append(layer2_clean_install(tree, work))
    if 3 in layers:
        results.append(layer3_surface(tree, work, root))
    if 4 in layers:
        results.append(layer4_private_suite(
            work, root, [s for s in args.slice.split(",") if s.strip()]))
    if 5 in layers:
        results.append(layer5_dangling(tree, root))
        results.append(soul_exposure_probe(tree))

    payload = {
        "repo": str(root),
        "workspace": str(work),
        "layers_run": sorted(layers),
        "ok": all(r.ok for r in results),
        "results": [r.as_dict() for r in results],
    }
    # Always persist the COMPLETE finding set.  The human printer caps its
    # output, and a capped list is how a real finding becomes invisible — an
    # unknown finding is indistinguishable from a missing one.
    dump = work / "coherence-findings.json"
    dump.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_human(payload)
        print(f"full findings: {dump}")

    if not args.keep and not args.dest:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"\nworkspace kept: {work}")
    return 0 if payload["ok"] else 1


def _print_human(payload: dict) -> None:
    print(f"repo      : {payload['repo']}")
    print(f"workspace : {payload['workspace']}")
    print()
    for r in payload["results"]:
        tag = "SKIP" if r["skipped"] else ("PASS" if r["ok"] else "FAIL")
        print(f"[{tag}] {r['layer']}")
        print(f"       {r['summary']}")
        # Verdict histogram + actionable-first ordering. Without this the
        # severe classes (a tool description that lies, a real secret) sort
        # in among hundreds of harmless prose hits and get cut by the cap.
        hist: dict[str, int] = {}
        for f in r["findings"]:
            v = str(f.get("verdict", "?"))
            hist[v] = hist.get(v, 0) + 1
        if hist:
            print("       verdicts: " + ", ".join(
                f"{k}={v}" for k, v in sorted(hist.items(),
                                              key=lambda kv: -kv[1])))
        rank = {"PRIVATE_INFRA_LEAK": 0, "TOOL_DESCRIPTION": 1,
                "REFUSAL_MESSAGE": 2, "USER_FACING_STRING": 3}
        ordered = sorted(r["findings"],
                         key=lambda f: rank.get(str(f.get("verdict")), 9))
        for f in ordered[:80]:
            print(f"       - {json.dumps(f, sort_keys=True)[:400]}")
        if len(r["findings"]) > 80:
            print(f"       ... {len(r['findings']) - 80} more")
        # A failing layer without its evidence is not actionable.
        if not r["ok"] and not r["skipped"] and r["detail"]:
            print("       --- detail tail ---")
            for line in r["detail"].strip().splitlines()[-25:]:
                print(f"       | {line}")
        print()
    print("OVERALL:", "PASS" if payload["ok"] else "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
