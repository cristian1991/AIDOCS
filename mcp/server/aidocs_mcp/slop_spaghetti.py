"""ai_slop(mode='spaghetti') — ARCHITECTURAL SLOP: an expensive call inside a
loop (the N+1 shape). #686 signal 1, validated against the KNOWN answer #688.

WHY THIS EXISTS. #688 is a ~10s-per-prompt regression that no tool found: the
memory surfacer runs the FULL spaCy pipeline ONCE PER CANDIDATE KEYWORD
(`_keyword_lemma_in_prompt` -> `_lemma_set` -> `svc.analyze`, profiled at 1022
`Language.__call__` calls / 8.30s per prompt). It was discovered by a human
READING a six-day-old comment. A static check should have caught it on day one.
So this module answers exactly one question, project-wide, from the AST:

    is a KNOWN-EXPENSIVE callee reached from inside a loop body, and is the
    loop's trip count large or unknown?

PRECISION OVER RECALL — a finder that cries wolf is ignored inside a week
(#686 says this explicitly). The suppression rules are therefore part of the
contract, not an implementation detail, and every suppression is REPORTED:

  * bounded_small_loop   — the loop iterates a literal/derived collection of
                           <= _SMALL_LOOP; 3 spaCy parses is not an N+1.
  * below_cost_threshold — unit cost x iterations under _MIN_COST_MS. A call
                           costing microseconds in a loop is not a finding.
  * A CACHE IS NOT A SUPPRESSION. `_keyword_lemma_in_prompt` DOES memoise into
    `_KW_LEMMA_CACHE`, and #688 still cost 8.3s/prompt — because the memo is
    keyed on the VALUE THAT VARIES PER ITERATION, so every one of ~1022 distinct
    keywords is a first touch in a fresh hook process. We cannot prove a cache
    key is loop-invariant from the AST, so a cache DEMOTES CONFIDENCE and is
    reported in `cache_note`; it never silently deletes the finding.

HONESTY ABOUT NUMBERS. Every finding says which term was MEASURED and which was
ESTIMATED. Only the spaCy unit cost is measured (10ms/parse on the operator box,
2026-08-01, corroborating hook_budget.py's 8.30s/1022 calls profile). Everything
else is an order-of-magnitude estimate and is labeled as one. Iterations are
'measured' only when the trip count is literal in the source.

READ-ONLY. Like every ai_slop mode: it finds, it never fixes.
"""

from __future__ import annotations

import ast
from typing import Any

# -- the expensive-callee catalogue ----------------------------------------
# (unit_ms, basis, category, why). Names are matched on the FINAL segment of the
# call target (`svc.analyze(...)` -> "analyze"), because the receiver varies.
# Deliberately NARROW: generic verbs ("run", "get", "post") are matched only via
# _DOTTED_EXPENSIVE, where the module qualifier makes them unambiguous.
_EXPENSIVE: dict[str, tuple[float, str, str, str]] = {
    # NLP - the #688 family. 10ms/parse MEASURED on the operator box 2026-08-01;
    # independently corroborated by hook_budget.py (8.30s / 1022 calls = 8.1ms).
    "analyze": (10.0, "measured", "nlp", "full spaCy pipeline per call"),
    "analyze_substance": (10.0, "measured", "nlp", "full spaCy pipeline per call"),
    "detect_language": (1.0, "estimated", "nlp", "lingua language detection"),
    "detect_language_of": (1.0, "estimated", "nlp", "lingua language detection"),
    "compute_language_confidence_values": (
        1.0,
        "estimated",
        "nlp",
        "lingua confidence pass",
    ),
    # embeddings / vector store - palace cold start ~2.3s; per-call is the warm
    # path only, and still two orders above a dict lookup.
    "embed": (15.0, "estimated", "embedding", "ONNX/embedder forward pass"),
    "embed_query": (15.0, "estimated", "embedding", "ONNX/embedder forward pass"),
    "embed_documents": (15.0, "estimated", "embedding", "ONNX/embedder forward pass"),
    # NOT "encode": measured against this repo it matched `str.encode('utf-8')`
    # (microseconds) far more often than an embedder, producing 37 of 74 findings,
    # all wrong. An ambiguous name in the catalogue poisons the whole tool.
    "hybrid_search": (20.0, "estimated", "vector", "palace/chroma vector query"),
    "semantic_search": (20.0, "estimated", "vector", "palace/chroma vector query"),
    "similarity_search": (20.0, "estimated", "vector", "palace/chroma vector query"),
    # DB - one statement is cheap; a statement PER ROW is the classic N+1.
    "execute": (0.5, "estimated", "db", "SQL statement per iteration"),
    "executemany": (0.5, "estimated", "db", "SQL statement per iteration"),
    "fetchall": (0.5, "estimated", "db", "result materialisation per iteration"),
    "fetchone": (0.5, "estimated", "db", "result fetch per iteration"),
    "connect": (2.0, "estimated", "db", "connection open per iteration"),
    # subprocess / network - process spawn dominates everything else here.
    "audited_run": (30.0, "estimated", "subprocess", "audited process spawn"),
    "check_output": (30.0, "estimated", "subprocess", "process spawn"),
    "check_call": (30.0, "estimated", "subprocess", "process spawn"),
    "Popen": (30.0, "estimated", "subprocess", "process spawn"),
    "urlopen": (50.0, "estimated", "network", "network round trip"),
    # filesystem - individually cheap; only ranks when the loop is big.
    "read_text": (0.3, "estimated", "filesystem", "file read per iteration"),
    "read_bytes": (0.3, "estimated", "filesystem", "file read per iteration"),
    "rglob": (5.0, "estimated", "filesystem", "recursive tree walk per iteration"),
    # NOT bare "walk": it matches `ast.walk` (an in-memory generator) as often as
    # `os.walk`. The filesystem form is caught by _DOTTED_EXPENSIVE["os.walk"].
}

# Qualified forms for callees whose bare name is too generic to match safely.
_DOTTED_EXPENSIVE: dict[str, tuple[float, str, str, str]] = {
    "subprocess.run": (30.0, "estimated", "subprocess", "process spawn"),
    "requests.get": (50.0, "estimated", "network", "network round trip"),
    "requests.post": (50.0, "estimated", "network", "network round trip"),
    "httpx.get": (50.0, "estimated", "network", "network round trip"),
    "httpx.post": (50.0, "estimated", "network", "network round trip"),
    "os.walk": (5.0, "estimated", "filesystem", "recursive tree walk per iteration"),
}

_SMALL_LOOP = 12  # <= this many literal iterations is not an N+1
_MIN_COST_MS = 250.0  # below this, say nothing - noise costs more than it saves
_ASSUMED_ITERATIONS = 100  # when cardinality is UNKNOWN; always labeled estimated
_MAX_DEPTH = 3  # how far to chase a project-local call chain
_CACHE_DECORATORS = ("lru_cache", "cache", "cached_property", "memoize")


def _dotted_of(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    parts.reverse()
    return ".".join(parts)


def _call_names(node: ast.Call) -> tuple[str, str]:
    """(final segment, dotted path) for a call target. ('', '') when neither is
    a plain name/attribute chain (e.g. a call on a subscript)."""
    fn = node.func
    parts: list[str] = []
    while isinstance(fn, ast.Attribute):
        parts.append(fn.attr)
        fn = fn.value
    if isinstance(fn, ast.Name):
        parts.append(fn.id)
    elif parts:
        parts.append("?")
    else:
        return "", ""
    parts.reverse()
    return parts[-1], ".".join(parts)


def _is_cached(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        dotted = _dotted_of(dec.func) if isinstance(dec, ast.Call) else _dotted_of(dec)
        if any(c in dotted for c in _CACHE_DECORATORS):
            return True
    return False


def _body_memoises(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Names an in-body memo dict (``_X_CACHE[...] = ...``) when present. Such a
    memo is REPORTED, never used to suppress - see the module docstring."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript):
                    name = _dotted_of(t.value)
                    if "cache" in name.lower() or "memo" in name.lower():
                        return name
    return ""


class _Project:
    """Project-local function table, so a call chain can be chased across files
    (the #688 chain is loop -> _keyword_lemma_in_prompt -> _lemma_set -> analyze,
    i.e. INVISIBLE to any single-hop check)."""

    def __init__(self, sources: list[tuple[str, str]]) -> None:
        # MODULE-LEVEL functions only, indexed PER FILE. Both restrictions are
        # precision fixes measured on this repo: a flat project-wide name table
        # resolved `m.start()` (a regex Match method) to an unrelated module's
        # `start()` and produced 2981 findings, nearly all garbage. A BARE-NAME
        # call can only be a module-level function, never someone's method.
        self.file_funcs: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        self.global_funcs: dict[
            str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]
        ] = {}
        self.trees: list[tuple[str, ast.Module]] = []
        self.const_len: dict[str, int] = {}
        for rel, text in sources:
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            self.trees.append((rel, tree))
            here = self.file_funcs.setdefault(rel, {})
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    here[node.name] = node
                    self.global_funcs.setdefault(node.name, []).append((rel, node))
            for node in tree.body:  # module-level literal collections only
                if isinstance(node, ast.Assign):
                    targets: list[ast.expr] = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                else:
                    continue
                val = node.value
                if not isinstance(val, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
                    continue
                n = len(val.keys) if isinstance(val, ast.Dict) else len(val.elts)
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.const_len[t.id] = n

    def _candidates(
        self, name: str, rel: str
    ) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
        """Definitions a BARE call `name(...)` in file `rel` could mean. Same file
        wins; otherwise only an UNAMBIGUOUS project-wide definition is chased. An
        ambiguous name resolves to NOTHING — an unresolved call is a far smaller
        error than a confidently wrong chain."""
        here = self.file_funcs.get(rel, {}).get(name)
        if here is not None:
            return [(rel, here)]
        defs = self.global_funcs.get(name, [])
        return list(defs) if len(defs) == 1 else []

    def resolve(
        self,
        node: ast.Call,
        rel: str,
        depth: int,
        seen: frozenset[str],
    ) -> tuple[tuple[float, str, str, str], list[str], str] | None:
        """(catalogue entry, call chain, cache note) for the most expensive
        reachable callee, or None. Chases project-local functions to _MAX_DEPTH."""
        name, dotted = _call_names(node)
        if not name:
            return None
        hit = _DOTTED_EXPENSIVE.get(dotted) or _EXPENSIVE.get(name)
        if hit is not None:
            return hit, [dotted or name], ""
        # ONLY a bare-name call is chaseable. `x.foo()` is a method on a value we
        # cannot type, so guessing which project `foo` it means manufactures
        # chains — that is exactly how the first draft produced 2981 findings.
        if isinstance(node.func, ast.Attribute):
            return None
        if depth >= _MAX_DEPTH or name in seen:
            return None
        best: tuple[tuple[float, str, str, str], list[str], str] | None = None
        for frel, fn in self._candidates(name, rel):
            note = ""
            if _is_cached(fn):
                note = f"{fn.name} is decorated with a functools cache"
            else:
                memo = _body_memoises(fn)
                if memo:
                    note = f"{fn.name} memoises into {memo}"
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                got = self.resolve(sub, frel, depth + 1, seen | {name})
                if got is None:
                    continue
                entry, chain, subnote = got
                cand = (entry, [name, *chain], note or subnote)
                if best is None or entry[0] > best[0][0]:
                    best = cand
        return best


def _cardinality(iter_node: ast.AST, proj: _Project) -> tuple[str, int, str]:
    """(label, iterations, basis). 'UNKNOWN' is a real answer, not a failure -
    it is what we honestly know about a dynamically-sized collection."""
    node = iter_node
    if isinstance(node, ast.Call):
        name, _ = _call_names(node)
        if (
            name == "range"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, int)
        ):
            v = int(node.args[0].value)
            return str(v), v, "measured"
        if name in ("items", "keys", "values") and isinstance(node.func, ast.Attribute):
            node = node.func.value
        else:
            return "UNKNOWN", _ASSUMED_ITERATIONS, "estimated"
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return str(len(node.elts)), len(node.elts), "measured"
    if isinstance(node, ast.Dict):
        return str(len(node.keys)), len(node.keys), "measured"
    if isinstance(node, ast.Name) and node.id in proj.const_len:
        n = proj.const_len[node.id]
        return f"{n} (len of module constant {node.id})", n, "measured"
    return "UNKNOWN", _ASSUMED_ITERATIONS, "estimated"


def _enclosing_map(tree: ast.Module) -> dict[int, str]:
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                out.setdefault(id(sub), node.name)
    return out


def _loops(tree: ast.Module) -> list[tuple[ast.AST, ast.AST | None, list[ast.AST]]]:
    found: list[tuple[ast.AST, ast.AST | None, list[ast.AST]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            found.append((node, node.iter, list(node.body)))
        elif isinstance(node, ast.While):
            found.append((node, None, list(node.body)))
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if node.generators:
                found.append((node, node.generators[0].iter, [node.elt]
                              if hasattr(node, "elt") else list(node.generators)))
    return found


def _demote(level: str) -> str:
    return {"high": "medium", "medium": "low"}.get(level, "low")


def find_n_plus_one(
    sources: list[tuple[str, str]],
    *,
    limit: int = 50,
    min_cost_ms: float = _MIN_COST_MS,
) -> dict[str, Any]:
    """Scan (rel_path, source) pairs for expensive calls inside loops.

    Returns {findings, suppressed, summary}. `findings` is ranked by estimated
    cost (unit_cost_ms x iterations) descending; `suppressed` carries every
    candidate the precision rules dropped, WITH the rule that dropped it - that
    list is as valuable as the findings when judging whether to trust the tool.
    """
    proj = _Project(sources)
    findings: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for rel, tree in proj.trees:
        enc = _enclosing_map(tree)
        for loop, iter_node, body in _loops(tree):
            label, iters, iters_basis = (
                _cardinality(iter_node, proj)
                if iter_node is not None
                else ("UNKNOWN", _ASSUMED_ITERATIONS, "estimated")
            )
            seen_callees: set[str] = set()
            for stmt in body:
                for call in ast.walk(stmt):
                    if not isinstance(call, ast.Call):
                        continue
                    name, _dotted = _call_names(call)
                    if not name or name in seen_callees:
                        continue
                    got = proj.resolve(call, rel, 0, frozenset())
                    if got is None:
                        continue
                    seen_callees.add(name)
                    (unit_ms, unit_basis, category, why), chain, cache_note = got
                    cost = unit_ms * iters
                    row: dict[str, Any] = {
                        "path": rel,
                        "line": getattr(call, "lineno", getattr(loop, "lineno", 0)),
                        "loop_line": getattr(loop, "lineno", 0),
                        "enclosing": enc.get(id(call), "<module>"),
                        "callee": name,
                        "expensive_via": " -> ".join(chain),
                        "category": category,
                        "why_expensive": why,
                        "cardinality": label,
                        "iterations": iters,
                        "iterations_basis": iters_basis,
                        "unit_cost_ms": unit_ms,
                        "unit_cost_basis": unit_basis,
                        "estimated_cost_ms": round(cost, 1),
                        "cache_note": cache_note or None,
                    }
                    if iters_basis == "measured" and iters <= _SMALL_LOOP:
                        suppressed.append(
                            {
                                **row,
                                "rule": "bounded_small_loop",
                                "reason": f"loop iterates {iters} times - bounded and small",
                            }
                        )
                        continue
                    if cost < min_cost_ms:
                        suppressed.append(
                            {
                                **row,
                                "rule": "below_cost_threshold",
                                "reason": f"{cost:.1f}ms < {min_cost_ms:.0f}ms budget",
                            }
                        )
                        continue
                    conf = "high" if unit_basis == "measured" else "medium"
                    if cache_note:
                        conf = _demote(conf)
                    row["confidence"] = conf
                    findings.append(row)
    findings.sort(key=lambda f: (-float(f["estimated_cost_ms"]), f["path"], f["line"]))
    return {
        "findings": findings[: max(1, int(limit))],
        "total_findings": len(findings),
        "suppressed": suppressed[:200],
        "total_suppressed": len(suppressed),
        "summary": {
            "files_scanned": len(proj.trees),
            "min_cost_ms": min_cost_ms,
            "assumed_iterations_when_unknown": _ASSUMED_ITERATIONS,
            "small_loop_threshold": _SMALL_LOOP,
        },
    }
