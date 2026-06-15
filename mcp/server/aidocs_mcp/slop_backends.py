"""Optional, free/local backends for project-wide slop reconnaissance.

DOCTRINE: a slop tool must either find the class of waste its name promises
ACROSS THE PROJECT, or clearly declare itself a narrow heuristic. Every finding
returned here carries a `source` (which engine produced it) and a `confidence`,
and degraded/absent backends report the truth (with an install hint) instead of
silently returning empty.

OPTIONAL-BACKEND POLICY: none of these engines are runtime dependencies of the
commercial AIDOCS package. They are detected at call time:
  * Built-in AST clone detector — NO dependency, always available.
  * Vulture (MIT) — project-wide Python dead code. `pip install aidocs-mcp[slop]`
    or `pip install vulture`. Detected via importlib; never imported at module
    load.
  * Semgrep (LGPL-2.1 CLI) / jscpd (MIT, Node) — detected on PATH; DESIGNED here,
    integration deferred (see backend_status).

Read-only: these inspect files / run analyzers; they NEVER mutate. Mutation stays
behind ai_deslop_apply.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import shutil
from typing import Any

# Confidence/kind vocabulary shared with ai_slop evidence labels.
KIND_HEURISTIC = "heuristic"


# ── backend availability (never imports heavy engines at module load) ──────
def backend_status() -> dict[str, Any]:
    """Truthful availability of each optional backend + how to enable it."""
    return {
        "ast_clones": {"available": True, "source": "aidocs_ast_clone", "needs": None},
        "vulture": {
            "available": importlib.util.find_spec("vulture") is not None,
            "source": "vulture",
            "needs": "pip install vulture  (or aidocs-mcp[slop])",
            "finds": "project-wide unused functions/classes/vars/imports (Python)",
        },
        "semgrep": {
            "available": (
                importlib.util.find_spec("semgrep") is not None
                or shutil.which("semgrep") is not None
            ),
            "source": "semgrep",
            "needs": "pip install semgrep  (CI/Linux lane)",
            "finds": "AIDOCS castle-law rules (core/semgrep/aidocs-laws.yml)",
            "status": "wired_report_first",
        },
        "jscpd": {
            "available": shutil.which("jscpd") is not None,
            "source": "jscpd",
            "needs": "npm i -g jscpd  (CLI on PATH)",
            "finds": "token-based cross-language clones (designed; deferred)",
            "status": "designed_not_wired",
        },
    }


# ── built-in AST structural clone detector (no dependency) ─────────────────
def _structural_signature(node: ast.AST) -> tuple[str, int]:
    """A name/literal-INDEPENDENT structural fingerprint of a function body, so a
    renamed copy (different identifiers/constants) still matches. Returns
    (sha256, node_count). Identifier names and constant values are deliberately
    excluded — only the AST shape (node-type sequence) is hashed.
    """
    parts: list[str] = []
    count = 0
    for sub in ast.walk(node):
        # skip the function's own name/args identifiers + literal values; keep
        # only node TYPES so structure (not naming) drives the match.
        parts.append(type(sub).__name__)
        count += 1
    sig = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return sig, count


def ast_clones(
    files: list[tuple[str, str]],
    *,
    min_nodes: int = 25,
    min_occurrences: int = 2,
) -> dict[str, Any]:
    """Find structurally-identical function bodies across files (catches copy-
    paste AND renamed copies). `files` = [(relpath, text)]. A clone cluster needs
    >= min_occurrences functions sharing a structural signature; functions below
    min_nodes are ignored as too-trivial to be meaningful.

    Confidence scales with block size (node_count). HEURISTIC: structural match
    is not proof of semantic duplication (two validators may share shape); review
    before extracting.
    """
    by_sig: dict[str, list[dict[str, Any]]] = {}
    parse_errors: list[str] = []
    for rel, text in files:
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            parse_errors.append(f"{rel}: {exc}")
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sig, n = _structural_signature(fn)
            if n < min_nodes:
                continue
            by_sig.setdefault(sig, []).append(
                {"path": rel, "symbol": fn.name, "line": fn.lineno, "nodes": n},
            )

    clusters = []
    for sig, members in by_sig.items():
        if len(members) < min_occurrences:
            continue
        files_involved = {m["path"] for m in members}
        nodes = members[0]["nodes"]
        # confidence: bigger shared blocks across more distinct files = higher.
        conf = min(0.95, 0.5 + 0.05 * len(members) + min(0.3, nodes / 400))
        clusters.append(
            {
                "signature": sig[:12],
                "occurrences": len(members),
                "distinct_files": len(files_involved),
                "renamed_clone": len({m["symbol"] for m in members}) > 1,
                "members": sorted(members, key=lambda m: (m["path"], m["line"])),
                "confidence": round(conf, 2),
            },
        )
    clusters.sort(key=lambda c: (c["occurrences"], c["confidence"]), reverse=True)
    return {
        "clusters": clusters,
        "total_clusters": len(clusters),
        "parse_errors": parse_errors,
        "evidence": {
            "kind": KIND_HEURISTIC,
            "source": "aidocs_ast_clone",
            "proves": "function bodies with an IDENTICAL AST structure across "
            "files (copy-paste or renamed copies)",
            "limitations": "structural match is NOT proof of semantic duplication "
            "(same-shaped but different-logic fns can collide); "
            f"ignores blocks < {min_nodes} AST nodes; Python only",
            "confidence_basis": "block size x distinct files",
        },
    }


def run_vulture(
    py_paths: list[str],
    *,
    min_confidence: int = 60,
    runner=None,
) -> dict[str, Any]:
    """Project-wide Python dead code via Vulture (optional). Returns honest
    `available: False` + install hint when Vulture is not installed — NEVER an
    empty success. `runner` is injectable for tests; default shells out to
    `python -m vulture`.
    """
    if importlib.util.find_spec("vulture") is None:
        return {
            "available": False,
            "source": "vulture",
            "findings": [],
            "install": "pip install vulture  (or aidocs-mcp[slop])",
            "evidence": {
                "kind": "unavailable",
                "source": "vulture",
                "proves": "nothing — optional backend not installed",
                "limitations": "install vulture for project-wide dead-code; "
                "ai_slop dead_code (intra-file) still works",
            },
        }
    import sys

    if runner is not None:
        # Injected subprocess seam (tests / explicit override): text output.
        argv = [sys.executable, "-m", "vulture", *py_paths, "--min-confidence", str(min_confidence)]
        try:
            code, out, _err = runner(argv)
        except Exception as exc:  # noqa: BLE001
            return {
                "available": True,
                "source": "vulture",
                "findings": [],
                "error": f"vulture invocation failed: {exc!r}",
            }
        findings = _parse_vulture(out)
    else:
        # In-process API — NO command line, so an arbitrarily large path set
        # (every .py in a big project) cannot overflow the OS argument limit
        # (Windows WinError 206 "filename or extension too long"). This is the
        # default path; it preserves vulture's whole-project reference
        # analysis (all paths scavenged together).
        try:
            import vulture as _vulture

            v = _vulture.Vulture(verbose=False)
            v.scavenge(list(py_paths))
            findings = [
                {
                    "path": str(it.filename),
                    "line": int(it.first_lineno),
                    "what": str(getattr(it, "message", "") or f"unused {it.typ} '{it.name}'"),
                    "confidence": int(it.confidence) / 100.0,
                }
                for it in v.get_unused_code(min_confidence=min_confidence)
            ]
            code = 0
        except Exception as exc:  # noqa: BLE001
            return {
                "available": True,
                "source": "vulture",
                "findings": [],
                "error": f"vulture invocation failed: {exc!r}",
            }
    return {
        "available": True,
        "source": "vulture",
        "exit_code": code,
        "findings": findings,
        "total": len(findings),
        "evidence": {
            "kind": KIND_HEURISTIC,
            "source": "vulture",
            "proves": "names with no detected references project-wide "
            "(functions/classes/vars/imports/unreachable)",
            "limitations": "static heuristic — DYNAMIC references (getattr / "
            "string dispatch / framework hooks / entry points) are "
            "missed; treat as candidates, never auto-delete; "
            "per-finding confidence is Vulture's own",
            "confidence_basis": "vulture --min-confidence",
        },
    }


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    import subprocess

    p = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout or "", p.stderr or ""


def aidocs_law_rules_path() -> str | None:
    """Resolve the AIDOCS castle-law Semgrep ruleset from the PACKAGE (so it works
    in an installed wheel and when ai_slop scans an unrelated project), NOT from
    the scanned project root. Returns None if not found (caller degrades honestly,
    never claims a clean scan).
    """
    from pathlib import Path

    pkg = Path(__file__).resolve().parent / "law_rules" / "aidocs-laws.yml"
    if pkg.is_file():
        return str(pkg)
    # dev checkout fallback: a repo-level copy, if one ever exists.
    repo = Path(__file__).resolve().parents[3] / "core" / "semgrep" / "aidocs-laws.yml"
    return str(repo) if repo.is_file() else None


def run_semgrep(
    paths: list[str],
    *,
    config: str | None = None,
    runner=None,
) -> dict[str, Any]:
    """AIDOCS castle-law scanner via Semgrep (optional). Returns honest
    `available: False` when semgrep is not installed OR not runnable on this OS
    (semgrep's engine is unreliable on native Windows — it is a CI/Linux lane).
    NEVER an empty success. `runner` injectable for tests.
    """
    import json

    if config is None:
        config = aidocs_law_rules_path()
    if not config:
        return {
            "available": False,
            "source": "semgrep",
            "findings": [],
            "reason": "AIDOCS castle-law ruleset not found in package",
            "evidence": {
                "kind": "unavailable",
                "source": "semgrep",
                "proves": "nothing — ruleset missing",
                "limitations": "law_rules/aidocs-laws.yml not packaged",
            },
        }
    if importlib.util.find_spec("semgrep") is None and shutil.which("semgrep") is None:
        return {
            "available": False,
            "source": "semgrep",
            "findings": [],
            "install": "pip install semgrep  (CI/Linux lane)",
            "evidence": {
                "kind": "unavailable",
                "source": "semgrep",
                "proves": "nothing — backend not installed",
                "limitations": "semgrep engine is a CI/Linux lane",
            },
        }
    import sys

    argv = [
        sys.executable,
        "-m",
        "semgrep",
        "scan",
        "--config",
        config,
        *paths,
        "--metrics=off",
        "--json",
    ]
    run = runner or _default_runner
    try:
        code, out, err = run(argv)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "source": "semgrep",
            "findings": [],
            "reason": f"semgrep invocation failed: {exc!r}",
            "evidence": {
                "kind": "unavailable",
                "source": "semgrep",
                "proves": "nothing",
                "limitations": str(exc)[:120],
            },
        }
    # semgrep: 0 = clean, 1 = findings; anything else = engine error (e.g. the
    # Windows exit-2 with no output) → degrade honestly, do NOT claim "clean".
    if code not in (0, 1) or not out.strip():
        return {
            "available": False,
            "source": "semgrep",
            "findings": [],
            "reason": f"semgrep not runnable here (exit {code}); CI/Linux lane",
            "evidence": {
                "kind": "unavailable",
                "source": "semgrep",
                "proves": "nothing — engine not runnable on this OS",
                "limitations": "run the semgrep lane in CI/Linux",
            },
        }
    try:
        data = json.loads(out)
    except Exception:
        return {
            "available": False,
            "source": "semgrep",
            "findings": [],
            "reason": "semgrep output not parseable",
        }
    findings = [
        {
            "rule": r.get("check_id", "").split(".")[-1],
            "path": r.get("path"),
            "line": (r.get("start") or {}).get("line"),
            "severity": (r.get("extra") or {}).get("severity"),
            "message": ((r.get("extra") or {}).get("message") or "").strip()[:300],
        }
        for r in data.get("results", [])
    ]
    return {
        "available": True,
        "source": "semgrep",
        "total": len(findings),
        "findings": findings,
        "engine_errors": len(data.get("errors", [])),
        "evidence": {
            "kind": KIND_HEURISTIC,
            "source": "semgrep",
            "proves": "code matching AIDOCS castle-law rules (security/law patterns)",
            "limitations": "report-first — INFO/WARNING rules can be noisy (e.g. "
            "best-effort except/pass); semgrep output is NOT "
            "authority, review each; never auto-fixed",
            "confidence_basis": "per-rule severity",
        },
    }


def _parse_vulture(out: str) -> list[dict[str, Any]]:
    """Parse Vulture text output: `path:line: unused X 'name' (NN% confidence)`."""
    import re

    findings: list[dict[str, Any]] = []
    pat = re.compile(
        r"^(?P<path>.+?):(?P<line>\d+):\s*(?P<what>.+?)\s*\((?P<conf>\d+)% confidence\)",
    )
    for raw in out.splitlines():
        m = pat.match(raw.strip())
        if not m:
            continue
        findings.append(
            {
                "path": m.group("path"),
                "line": int(m.group("line")),
                "what": m.group("what"),
                "confidence": int(m.group("conf")) / 100.0,
            },
        )
    return findings
