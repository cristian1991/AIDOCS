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

from . import spawn_authority

PACKAGE_ROOT = Path(__file__).resolve().parent
#: The packaged vendored engine (#925). Third-party bytes that ship INSIDE the
#: package so the release signature covers them. Spelled here rather than
#: imported from runtime_provisioner to keep this scanner free of that import.
_VENDOR_DIRNAME = "_vendor"


def _vendored_exempt(rel: str) -> bool:
    """Is this vendored path an EXACT, PROVEN-UNREACHABLE capability module?

    Not "is it vendored" — that question is what let upstream's spawns vanish
    from this census (#925g). A vendored module is exempt only while
    `contract_import_closure` still shows it outside the import closure of the
    modules aidocs_mcp actually imports. Anything else vendored is censused.

    Fail-closed: if the proof cannot be computed, nothing is exempt and the
    census reports more rather than less.
    """
    if not (rel == _VENDOR_DIRNAME or rel.startswith(f"{_VENDOR_DIRNAME}/")):
        return False
    try:
        from .vendored_contract import (
            VENDORED_UNREACHABLE_CAPABILITY_MODULES,
            contract_import_closure,
        )

        named = {r for r, _reason in VENDORED_UNREACHABLE_CAPABILITY_MODULES}
        suffix = rel[len(_VENDOR_DIRNAME) + 1 :]
        if suffix not in named:
            return False
        closure = contract_import_closure(PACKAGE_ROOT / _VENDOR_DIRNAME / "mempalace")
        module = suffix.split("/", 1)[1].removesuffix(".py")
        return module not in closure
    except Exception:  # noqa: BLE001 — a proof that cannot run is not a proof
        return False

#: THE ONE CALLSITE THAT CANNOT ROUTE THROUGH THE CHOKEPOINT (#1030) — now
#: DERIVED from `spawn_authority`, where it is a FIELD on the callsite rather
#: than a fifth list keyed back to it by (relpath, function) (#1031 phase 2).
#:
#: ``audited_run`` lives in ``shell_egress_service``, inside the package. The
#: hook shim exists precisely BECAUSE that package can be absent: it is
#: stdlib-only, it is copied VERBATIM to ``~/.aidocs/runtime/`` outside
#: site-packages, and the copy that actually executes runs BEFORE any aidocs
#: import is attempted. Requiring it to import the auditor would recreate #616
#: — the enforcement entry point living inside the tree being replaced.
#:
#: Its spawn is also not the kind the ledger exists to watch. It has exactly
#: one shape: re-enter THIS SAME FILE under the interpreter of the generation
#: the activation pointer names. No argv from a caller, no shell, no user
#: input, and the process it starts is the hook itself — whose every action is
#: then governed normally, by the runtime it just entered.
#:
#: NAMED, NOT EXCLUDED BY CATEGORY. Each entry is one (relpath, function) pair
#: with a written reason, and ``test_spawn_census`` pins that the set stays
#: exactly this one — a second raw spawn anywhere, including elsewhere in that
#: same file, still fails the organ.
UNAUDITABLE_PRE_IMPORT_SPAWNS: dict[tuple[str, str], str] = {
    (s.relpath, s.enclosing_fn): s.unauditable_why
    for s in spawn_authority.SPAWN_SITES
    if s.unauditable_why
}

# RE-EXPORTED FROM spawn_authority (#1031 phase 2), not redeclared. These are
# facts about the seam — what counts as a spawn, and what makes one sanctioned
# — and they were previously stated here AND restated in the semgrep rule's
# YAML one directory over, where the two could disagree about a whole spawn
# family. This module's own comment warned about exactly that ("a future spawn
# via a side family lands in raw_unaudited") while the rule kept its own list.
# The rule's callee patterns are now GENERATED from CORE_SPAWN_CALLEES.
AUDITED_WRAPPERS = spawn_authority.AUDITED_WRAPPERS
PASSTHROUGH_KWARGS = spawn_authority.PASSTHROUGH_KWARGS
CHOKEPOINT_RELPATH = spawn_authority.CHOKEPOINT_RELPATH
CORE_SPAWN_CALLEES = spawn_authority.CORE_SPAWN_CALLEES
EXTENDED_SPAWN_CALLEES = spawn_authority.EXTENDED_SPAWN_CALLEES
ALL_SPAWN_CALLEES = spawn_authority.ALL_SPAWN_CALLEES
WINDOWLESS_TOKENS = spawn_authority.WINDOWLESS_TOKENS


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
    pre_import_exempt: list[dict[str, Any]],
) -> None:
    """Fold one file's spawn nodes into the census accumulators."""
    for kind, node, callee in _iter_spawn_nodes(scan):
        if kind == "raw":
            fn = scan.enclosing_fn(node)
            row = {
                "relpath": scan.rel,
                "line": node.lineno,
                "enclosing_fn": fn,
                "callee": callee,
            }
            why = UNAUDITABLE_PRE_IMPORT_SPAWNS.get((scan.rel, fn))
            if why is not None:
                # STILL ON THE MAP, just in its own column. Dropping it would
                # make the census claim a completeness it no longer had; the
                # whole doctrine here is that absence from the map is evidence.
                pre_import_exempt.append({**row, "reason": why})
                continue
            raw_unaudited.append(row)
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
        "pre_import_exempt": len(census.get("pre_import_exempt") or []),
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
    pre_import_exempt: list[dict[str, Any]] = []
    vendored_exempt: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    entry_counts: Counter = Counter()

    for py in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = py.relative_to(PACKAGE_ROOT).as_posix()
        # THE CENSUS COUNTS THE VENDORED ENGINE TOO (#925g).
        #
        # An earlier pass skipped `_vendor` outright. That made this census
        # claim completeness while omitting `os.execve`, `subprocess.Popen` and
        # urllib egress that ship inside the signed package — and a census with
        # a silent hole is worse than no census, because it is quoted as proof.
        #
        # The vendored tree is walked. Only modules PROVEN unreachable from the
        # six mempalace modules aidocs_mcp imports are marked exempt, by name,
        # with the proof recomputed here rather than trusted — so a module that
        # becomes reachable re-enters the census automatically.
        if _vendored_exempt(rel):
            # ITS OWN BUCKET, deliberately. These are NOT pre-import exemptions:
            # that list is the #345 contract for exactly one named callsite that
            # must spawn before the audit layer exists, and folding a second,
            # unrelated class into it would destroy the invariant its test
            # guards. Reported separately so the census stays complete while
            # each exemption keeps its own meaning.
            vendored_exempt.append(
                {
                    "relpath": rel,
                    "reason": "vendored: unreachable from MEMPALACE_CONTRACT_MODULES",
                    "class": "vendored_unreachable",
                }
            )
            continue
        try:
            scan = _FileScan(py, rel)
        except SyntaxError as exc:
            parse_errors.append(f"{rel}: {exc}")
            continue
        _collect_file(
            scan, deliberate, registry, entries, raw_unaudited, entry_counts, pre_import_exempt
        )

    deliberate_used = {
        (e["relpath"], e["enclosing_fn"])
        for e in entries
        if e["window_posture"] == "deliberate-console"
    }
    census: dict[str, Any] = {
        "entries": entries,
        "raw_unaudited": raw_unaudited,
        "pre_import_exempt": pre_import_exempt,
        # #925g: the vendored engine's own capability modules, each named and
        # exempt only while proven unreachable from MEMPALACE_CONTRACT_MODULES.
        # Reported rather than omitted — a census that drops executable paths in
        # silence is quoted as proof of something it never examined.
        "vendored_exempt": vendored_exempt,
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
