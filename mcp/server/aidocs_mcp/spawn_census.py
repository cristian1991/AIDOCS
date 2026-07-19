"""One-organism spawn map (#335) — the single census surface.

"Why did a process spawn" must have ONE answer. This module renders the
COMPLETE static spawn map of ``mcp/server/aidocs_mcp/``:

    callsite (relpath:line, enclosing fn)
        -> fingerprint (LEGACY_SUBPROCESS_FINGERPRINTS key)
        -> reason (the human WHY passed to audited_popen/audited_run)
        -> window posture (windowless evidence | deliberate-console | none)
        -> audited? (routed through the #334 chokepoint)
        -> registry row (reachability / owner / rationale)

by AST-scanning the shipped package source and joining the two static
registries in ``shell_egress_service``:

  * ``LEGACY_SUBPROCESS_FINGERPRINTS`` — the enforcement allow-list
    (untouched authority; this module only READS it), and
  * ``DELIBERATE_CONSOLE_SPAWNS`` — the sanctioned console exceptions.

The runtime ledger join (per-fingerprint spawn counts + orphan
fingerprints) is layered on by
``process_audit_store.process_audit_query(mode="census")`` so the same
surface answers both the static question ("what CAN spawn, and how")
and the runtime question ("what DID spawn, how often").

PURE READ — this module never spawns anything. The census test
(``tests/security/test_spawn_census.py``) wires the map as an ORGAN:
an unaudited, unregistered, fingerprint-less, reason-less, or
posture-less callsite fails the suite, so the map can never silently
go stale (#349 post-mortem: an instrument whose coverage is unpinned
produces confidently wrong answers).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent

AUDITED_WRAPPERS = {"audited_popen", "audited_run"}
PASSTHROUGH_KWARGS = {"popen", "run"}
CHOKEPOINT_RELPATH = "shell_egress_service.py"

# The classic spawn-callee set (mirrors the #345 seal test) ...
CORE_SPAWN_CALLEES = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "os.system",
}
# ... plus the blind-spot families the seal does not enumerate. A future
# spawn via a side family lands in ``raw_unaudited`` and fails the organ
# test instead of silently blinding the census.
EXTENDED_SPAWN_CALLEES = {
    "os.popen",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.startfile",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}
ALL_SPAWN_CALLEES = CORE_SPAWN_CALLEES | EXTENDED_SPAWN_CALLEES

# Evidence tokens that an enclosing scope suppresses (or deliberately
# detaches from) the Windows console — same vocabulary as the seal test.
WINDOWLESS_TOKENS = (
    "CREATE_NO_WINDOW",
    "_WIN_NO_WINDOW",
    "_win_no_window",
    "0x08000000",
    "DETACHED_PROCESS",
    "_popen_kwargs_for_platform",
)


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Name):
        return node.id
    return None


def _wrapper_name(node: ast.AST) -> str | None:
    nm = _name_of(node)
    return nm.rsplit(".", 1)[-1] if nm else None


class _FileScan:
    """One parsed package file: parent links for scope resolution."""

    def __init__(self, path: Path, rel: str):
        self.rel = rel
        self.src = path.read_text(encoding="utf-8", errors="replace")
        self.tree = ast.parse(self.src)
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent

    def enclosing_fn(self, node: ast.AST) -> str:
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = self.parents.get(cur)
        return "<module>"

    def enclosing_scope_src(self, node: ast.AST) -> str:
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return ast.get_source_segment(self.src, cur) or ""
            cur = self.parents.get(cur)
        return self.src

    def is_sanctioned_passthrough(self, call: ast.Call) -> bool:
        """True when ``call`` is the body of a lambda passed as
        popen=/run= to audited_popen/audited_run — the chokepoint seam."""
        lam = self.parents.get(call)
        if not isinstance(lam, ast.Lambda) or lam.body is not call:
            return False
        kw = self.parents.get(lam)
        if not isinstance(kw, ast.keyword) or kw.arg not in PASSTHROUGH_KWARGS:
            return False
        outer = self.parents.get(kw)
        return isinstance(outer, ast.Call) and _wrapper_name(outer.func) in AUDITED_WRAPPERS


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _fingerprint_literal(call: ast.Call) -> tuple[str, ...] | None:
    node = _kw(call, "fingerprint")
    if node is None:
        return None
    if isinstance(node, ast.Tuple):
        parts = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        return tuple(parts) if len(parts) == len(node.elts) else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return tuple(node.value.split("::"))
    return None


def _reason_literal(call: ast.Call) -> str | None:
    node = _kw(call, "reason")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _wrapped_callee(call: ast.Call) -> str | None:
    """The subprocess callee inside the passthrough lambda, when present."""
    seam = _kw(call, "popen") or _kw(call, "run")
    if isinstance(seam, ast.Lambda) and isinstance(seam.body, ast.Call):
        return _name_of(seam.body.func)
    return None


def _window_posture(scope_src: str, rel: str, fn: str, deliberate: dict) -> str:
    why = deliberate.get((rel, fn))
    if why is not None:
        return "deliberate-console"
    for tok in WINDOWLESS_TOKENS:
        if tok in scope_src:
            return f"windowless ({tok})"
    return "none"


def _iter_spawn_nodes(scan: _FileScan):
    """Yield ("raw", node, callee) for direct spawn calls outside the
    chokepoint seam and ("audited", node, callee) for audited_* calls.
    Passthrough-lambda callees are skipped — they ARE the seam and are
    represented by their enclosing audited_* entry."""
    for node in ast.walk(scan.tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _name_of(node.func)
        if callee in ALL_SPAWN_CALLEES:
            if not scan.is_sanctioned_passthrough(node):
                yield "raw", node, callee
        elif _wrapper_name(node.func) in AUDITED_WRAPPERS:
            yield "audited", node, callee


def _registry_fields(row: tuple | None) -> dict[str, Any]:
    if row is None:
        return {"reachability": None, "owner": None, "rationale": None}
    return {"reachability": row[5], "owner": row[6], "rationale": row[7]}


def _audited_entry(
    scan: _FileScan,
    node: ast.Call,
    deliberate: dict[tuple[str, str], str],
    registry: dict[tuple[str, ...], tuple],
) -> tuple[tuple[str, ...] | None, dict[str, Any]]:
    """One map entry for an audited_popen/audited_run callsite."""
    fp = _fingerprint_literal(node)
    fn = scan.enclosing_fn(node)
    triple = fp[:3] if fp and len(fp) >= 3 else None
    row = registry.get(triple) if triple else None
    posture = _window_posture(scan.enclosing_scope_src(node), scan.rel, fn, deliberate)
    entry = {
        "relpath": scan.rel,
        "line": node.lineno,
        "enclosing_fn": fn,
        "callee": _wrapped_callee(node) or (triple[2] if triple else None),
        "fingerprint": "::".join(fp) if fp else None,
        "reason": _reason_literal(node),
        "window_posture": posture,
        "audited": True,
        # The chokepoint file itself is outside the registry's scan
        # (its calls ARE the seam) — registered by role.
        "registered": bool(row) or scan.rel == CHOKEPOINT_RELPATH,
        "deliberate_console_why": deliberate.get((scan.rel, fn)),
        **_registry_fields(row),
    }
    return triple, entry


def _collect_file(
    scan: _FileScan,
    deliberate: dict[tuple[str, str], str],
    registry: dict[tuple[str, ...], tuple],
    entries: list[dict[str, Any]],
    raw_unaudited: list[dict[str, Any]],
    entry_counts,
) -> None:
    """Fold one file's spawn nodes into the census accumulators."""
    for kind, node, callee in _iter_spawn_nodes(scan):
        if kind == "raw":
            raw_unaudited.append(
                {
                    "relpath": scan.rel,
                    "line": node.lineno,
                    "enclosing_fn": scan.enclosing_fn(node),
                    "callee": callee,
                }
            )
            continue
        triple, entry = _audited_entry(scan, node, deliberate, registry)
        if triple:
            entry_counts[triple] += 1
        entries.append(entry)


def _summarize(census: dict[str, Any], registry_rows: int) -> dict[str, int]:
    from collections import Counter

    postures = Counter(e["window_posture"].split(" ")[0] for e in census["entries"])
    return {
        "callsites_audited": len(census["entries"]),
        "raw_unaudited": len(census["raw_unaudited"]),
        "windowless": postures.get("windowless", 0),
        "deliberate_console": postures.get("deliberate-console", 0),
        "posture_none": postures.get("none", 0),
        "registry_rows": registry_rows,
        "registry_unmatched": len(census["registry_unmatched"]),
        "deliberate_unused": len(census["deliberate_unused"]),
        "parse_errors": len(census["parse_errors"]),
    }


def spawn_census() -> dict[str, Any]:
    """Render the complete static spawn map. See module docstring.

    Returns::

        {
          "entries":       [ {relpath, line, enclosing_fn, callee,
                              fingerprint, reason, window_posture,
                              audited, registered, reachability, owner,
                              rationale, deliberate_console_why}, ... ],
          "raw_unaudited": [ {relpath, line, enclosing_fn, callee}, ... ],
          "registry_unmatched": [fingerprint keys with no audited callsite],
          "deliberate_unused":  [(relpath, fn) rows no callsite uses],
          "parse_errors":  [ "relpath: error", ... ],
          "summary":       { counts },
        }
    """
    # Lazy import: shell_egress_service imports process_audit_store, and
    # process_audit_store's census mode imports THIS module — top-level
    # imports here would close an import cycle.
    from collections import Counter

    from .shell_egress_service import (
        DELIBERATE_CONSOLE_SPAWNS,
        LEGACY_SUBPROCESS_FINGERPRINTS,
    )

    deliberate = {(r[0], r[1]): r[2] for r in DELIBERATE_CONSOLE_SPAWNS}
    registry: dict[tuple[str, ...], tuple] = {}
    registry_counts: Counter = Counter()
    for row in LEGACY_SUBPROCESS_FINGERPRINTS:
        registry.setdefault(row[:3], row)
        registry_counts[row[:3]] += 1

    entries: list[dict[str, Any]] = []
    raw_unaudited: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    entry_counts: Counter = Counter()

    for py in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = py.relative_to(PACKAGE_ROOT).as_posix()
        try:
            scan = _FileScan(py, rel)
        except SyntaxError as exc:
            parse_errors.append(f"{rel}: {exc}")
            continue
        _collect_file(scan, deliberate, registry, entries, raw_unaudited, entry_counts)

    deliberate_used = {
        (e["relpath"], e["enclosing_fn"])
        for e in entries
        if e["window_posture"] == "deliberate-console"
    }
    census: dict[str, Any] = {
        "entries": entries,
        "raw_unaudited": raw_unaudited,
        "registry_unmatched": sorted(
            "::".join(triple)
            for triple, n in (registry_counts - entry_counts).items()
            for _ in range(n)
        ),
        "deliberate_unused": sorted(set(deliberate) - deliberate_used),
        "parse_errors": parse_errors,
    }
    census["summary"] = _summarize(census, len(LEGACY_SUBPROCESS_FINGERPRINTS))
    return census
