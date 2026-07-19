"""Tool Interface — every agent-callable function declared in ONE file.

Open this file. Read top to bottom. You see the entire agent contract:
public name, signature, docstring, surface (`both` / `local_only` /
`hidden`), class, tier, scope, confirm mode, annotations. Implementation
lives elsewhere — each function body is a one-line `_delegate(...)`
call that routes to the real impl via the local MCP server's
`call_tool`. When a later refactor lifts the inner-closure impls to
module-level functions, the delegate body becomes a direct import+call
without changing this file.

This module is a Java/C#-style interface declaration, not a dynamic
registry. The `_TOOLS` dict the `@tool` decorator populates is an
implementation detail consumers use to enumerate; the source of truth
is the `def` statements themselves, in file order.

Add a tool:
    1. Add a `@tool(...) def your_tool(...): ...` block below.
    2. Set surface/cls/tier/scope/confirm.
    3. The one-line body is `return _delegate("your_tool", **locals())`
       — `_delegate` strips `confirm_token` automatically so the
       underlying handler never sees it as a real arg.
    4. Done. `outer_gate_catalog.classify()` and the gate's tools/list
       both consult this file's `_TOOLS` dict immediately.

Two-phase confirm:
    Set `confirm=TWO_PHASE` and `phrase="confirm-X {arg}"`. The first
    call without `confirm_token` returns `_error="confirm_required"`
    + the resolved phrase; the second call with a matching
    `confirm_token` executes. Mirrors `profile_confirm_token` in
    operator_surface and the project_select/session_select pattern.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

# ── Surface / class / tier / confirm constants ──────────────────────
# Short uppercase names so the decorator calls below stay readable
# (`surface=BOTH` instead of `surface="both"`).

# Surfaces
BOTH = "both"  # advertised on local stdio AND outer gate
LOCAL_ONLY = "local_only"  # local stdio only; gate refuses tools/list + tools/call
GATE_ONLY = "gate_only"
# ^ Doctrine 2026-05-29 (Empire directive): explicit name for tools that
# the outer gate exposes but stdio does NOT. The previous surface set
# (BOTH / LOCAL_ONLY) had no way to declare "advertised on the gate
# while a stdio binding is intentionally pending." A consolidator tool
# in mid-migration (e.g. ai_lane/ai_plan/ai_worker) was declared as
# BOTH but actually unreachable via stdio — `surface=BOTH` was lying.
# GATE_ONLY makes the migration state honest: `surface=BOTH` now means
# "reachable on BOTH surfaces right now"; GATE_ONLY means "gate yes,
# stdio not yet — see _pending_migration_doctrine in the decorator."
HIDDEN = "hidden"  # hidden on every surface (rare — closure helpers)

# Classes (match outer_gate_catalog.CLASS_*)
READ = "read"
EDIT = "edit"
RUN = "run"
SELECTOR = "selector"
IMPORT = "import"
ADMIN = "admin"  # local-only break-glass / operator recovery

# Tiers (match outer_gate_manifest.TIER_*)
R = "R"
M = "M"
A = "A"
L = "L"

# Confirm modes
NO_CONFIRM = "none"
TWO_PHASE = "two_phase"


# ── Voice normalization (the core of the voice-friendly confirm change) ──
#
# Speech-to-text mangles punctuation, case, and spacing: "config set",
# "config-set", "config_set", and "configset" are the SAME spoken phrase but
# four different byte strings; "AIDOCS_PRIVATE" is spoken "aidocs private".
# `_normalize_voice` collapses a string to its bare alphanumeric skeleton
# (lowercase, every non-[a-z0-9] character DROPPED) so all of those forms
# compare equal. Confirm tokens and registry NAMEs are matched on the
# normalized form, NOT byte-exact.
#
# SECURITY BOUND: this is safe ONLY for the CLOSED set of action verbs
# (the `confirm <action>` phrases) and the registry NAME set (project/org
# names, resolved with collision detection by the caller). It is NEVER applied
# to filesystem PATHS — the confirm token is always the ACTION, never a
# filename, so path ambiguity can never enter the confirm step.
def _normalize_voice(s: object) -> str:
    """Lowercase + keep only [a-z0-9] (drop all whitespace/punctuation).

    So 'Config-Set', 'config set', 'config_set', 'CONFIGSET' all normalize to
    'configset', and 'AIDOCS_PRIVATE' == 'aidocs private' == 'aidocsprivate'.
    Empty / None → ''. Use ``_normalize_voice(a) == _normalize_voice(b)`` for
    voice-tolerant equality over the closed action/name sets only.
    """
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


# ── Internal: the @tool decorator + the dict it populates ───────────


@dataclass(frozen=True)
class ToolSpec:
    """The metadata `@tool(...)` attaches to a declared function. Same
    shape as the previous version; the difference is that consumers
    discover specs by walking `_TOOLS` (populated by the decorator at
    file-load time) instead of authoring a literal dict.
    """

    name: str
    description: str
    surface: str
    cls: str
    tier: str
    scope: str
    confirm: str
    phrase: str
    annotations: dict
    fn: Callable[..., Any]
    # ── Phase 1 unified-registry shape (sealed design §3) — all defaulted so
    #    existing declarations construct unchanged; populated incrementally. ──
    aliases: tuple[str, ...] = ()  # extra retrieval phrasings (class A; web+NLP)
    modes: dict | None = None  # per-mode oneOf spec → public_schema mode_schema
    injected_args: tuple[str, ...] = ()  # server-bound params EXCLUDED from public_schema
    remote_eligible: bool | None = None  # static; None ⇒ derive from class/allowlist
    deferral: str = "eager"  # eager | deferred (local harness + gate eager-advertise)
    # SSOT-07 (2026-07-15): declared RESULT shape. None ⇒ the catalog falls
    # back to the permissive object (legacy _OUTPUT_OBJ). Declare only shapes
    # that are TRUE — a wrong outputSchema is worse than a permissive one
    # (hosts validate results against it; phantom attributes crash UIs).
    output: dict | None = None


_TOOLS: dict[str, ToolSpec] = {}


# ── C.20 direct registry dispatch — module-level impl registry ─────
#
# Doctrine (2026-05-29 — C.20 advance step): consolidator dispatch
# from `tool_interface.ai_lane/ai_plan/ai_worker` goes through
# `_delegate(name)` which historically created a NEW MCP server via
# create_server() and ran `srv.call_tool(name, kwargs)` to invoke
# the legacy impls — those impls live as closures inside
# create_server() and aren't directly importable. The round-trip
# adds ~150ms per call (server build + fastmcp routing + thread
# hop) AND obscures the call graph (you can't grep "who calls
# ai_status" from a static read).
#
# This registry lifts the addressing problem: each impl module
# (e.g. server_plan_task_tools) calls `register_impl(name, fn)` for
# every legacy tool it defines. `_delegate(name)` then checks
# `_IMPLS[name]` first and, when present, invokes the impl DIRECTLY
# in-process — skipping the server build, the fastmcp dispatcher,
# the threaded loop hop. The closures still capture their original
# scope (hub / runtime / project_root_resolver / etc.) so behavior
# stays identical; only the routing latency changes.
#
# Migration policy: an impl module opts in by calling register_impl
# during its register_*_tools() registration step. While `_IMPLS` is
# empty for a given name, `_delegate` falls back to the legacy
# server round-trip — there's no flag-day, modules graduate one at
# a time. test_c20_direct_dispatch.py asserts parity between the
# two paths for every migrated impl.

_IMPLS: dict[str, Any] = {}


def register_impl(name: str, fn: Any) -> Any:
    """Register a legacy tool impl as the direct-dispatch target for
    its name. Returns the function unchanged so it can be used as a
    side-effect-free augmentation after the @server.tool() decorator.

    Idempotent: re-registering the same name overwrites (the latest
    create_server() call wins). NOT thread-safe — module imports
    happen on the main thread before workers start; if that ever
    changes, add a lock here.
    """
    if not name:
        raise ValueError("register_impl: name is required")
    _IMPLS[name] = fn
    return fn


def has_direct_impl(name: str) -> bool:
    """Whether name has been registered for direct dispatch."""
    return name in _IMPLS


def direct_impls() -> dict[str, Any]:
    """Snapshot of the direct-dispatch registry. For tests + audit
    surfaces that want to enumerate what's been migrated.
    """
    return dict(_IMPLS)


def tool(
    *,
    surface: str = BOTH,
    cls: str = "",
    tier: str = R,
    scope: str = "catalog",
    confirm: str = NO_CONFIRM,
    phrase: str = "",
    annotations: dict | None = None,
    aliases: tuple[str, ...] = (),
    modes: dict | None = None,
    injected_args: tuple[str, ...] = (),
    remote_eligible: bool | None = None,
    deferral: str = "eager",
    output: dict | None = None,
):
    """Decorator: register a declared function in `_TOOLS` and attach
    metadata to the function object (`fn._tool_spec`). The function's
    own docstring becomes the tool description. The function's
    parameter list IS the agent-facing schema; consumers introspect
    signatures via `inspect.signature` when building JSON Schema.

    Phase 1 unified-registry kwargs (sealed design §3) are all optional and
    default to today's behaviour; they are populated incrementally as
    projections migrate. `injected_args` names params bound server-side and
    excluded from `public_schema` (§3 / §4a).
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolSpec(
            name=fn.__name__,
            # inspect.cleandoc (not .strip()) — canonically DEDENT multi-line
            # docstrings so the advertised description matches the AST-extracted
            # form (ast.get_docstring(clean=True), which the outer-gate manifest
            # uses) byte-for-byte. .strip() only trims the outer edges and leaves
            # source indentation on continuation lines, which drove a host-
            # dependent resolution-pin drift (#299): reg_spec path kept the raw
            # 4-space indent while the manifest path was already dedented.
            description=inspect.cleandoc(fn.__doc__ or ""),
            surface=surface,
            cls=cls,
            tier=tier,
            scope=scope,
            confirm=confirm,
            phrase=phrase,
            annotations=dict(annotations or {}),
            fn=fn,
            aliases=tuple(aliases),
            modes=modes,
            injected_args=tuple(injected_args),
            remote_eligible=remote_eligible,
            deferral=deferral,
            output=output,
        )
        if spec.name in _TOOLS:
            raise RuntimeError(
                f"tool_interface: duplicate tool {spec.name!r} "
                f"(file order = source of truth; rename one)",
            )
        _TOOLS[spec.name] = spec
        fn._tool_spec = spec  # type: ignore[attr-defined]
        return fn

    return deco


# ── Delegate: route a call to the underlying impl ───────────────────


# Structural params forwarded to the handler EVEN WHEN EMPTY. session_id="" means
# "current session" and handlers require the arg present (they run their OWN
# emptiness check) — stripping it raised `TypeError: missing session_id` on
# ai_plan_status / ai_lane_delegate (VPS gate 2b, 2026-07-15).
_KEEP_EMPTY_KEYS: frozenset[str] = frozenset({"session_id"})
# Tools whose contract forwards their route/actor fields VERBATIM (incl. empty
# strings) — the "" heuristic must not touch them (e.g. ai_msg's xaacp actor
# route, which pins reply_to='' etc. into the forwarded packet).
_FORWARD_EMPTY_TOOLS: frozenset[str] = frozenset({"ai_msg"})


def _strip_unset_kwargs(kwargs: dict[str, Any], tool_name: str = "") -> dict[str, Any]:
    """Drop registry-contract + UNSET params before invoking a handler.

    #399 generalized empty-param guard (all tools, ONE chokepoint). Strips:
      - confirm_token — a registry-level contract, not an impl arg;
      - None          — never provided;
      - ""            — 'not provided'. The SSOT stubs declare optional params
                        with a "" default, so a modal call carries every OTHER
                        mode's params as empty strings (ai_backlog(mode='update')
                        arrives with content=""). Passing "" through lets a
                        handler/store misread it as a real value and destructively
                        write it (this is what blanked backlog bodies). Dropping
                        it makes the handler apply its OWN default.
    False / 0 / [] are MEANINGFUL and pass through untouched — only None and the
    empty string are treated as unset.
    """
    strip_empty = tool_name not in _FORWARD_EMPTY_TOOLS
    return {
        k: v
        for k, v in kwargs.items()
        if k != "confirm_token"
        and v is not None
        and not (strip_empty and v == "" and k not in _KEEP_EMPTY_KEYS)
    }


def _delegate(_tool_name: str, **kwargs: Any) -> Any:
    """Route a call from this interface module to the real handler.
    Strips `confirm_token` (a registry-level contract, not an impl
    arg) before invoking. Today the route goes through the local MCP
    server's `call_tool` because most impls are nested inside
    `create_server` closures and not directly importable; BACKLOG C.20
    proposes lifting those closures to module-level for a direct
    `fn(**kwargs)` dispatch that cuts ~150 ms per call.

    Doctrine (2026-05-29 — finish GATE_ONLY migration): this function
    is called from BOTH (a) the gate's outer_gate_executor in a fully-
    sync context AND (b) the stdio MCP server's @server.tool wrappers
    INSIDE a running asyncio event loop. The old `asyncio.run(...)`
    form worked for (a) but raised "asyncio.run() cannot be called
    from a running event loop" for (b). We now detect whether a loop
    is already running and use it; only outside any loop do we own
    one via `asyncio.run`.

    Returns the impl's payload as a dict (or `{"result": ...}` for
    non-dict returns).
    """
    impl_kwargs = _strip_unset_kwargs(kwargs, _tool_name)
    import asyncio
    import inspect

    from .mcp_server import create_server
    from .outer_gate_executor import _result_payload

    # ── C.20 direct-dispatch fast path ────────────────────────────
    # When the impl module has called register_impl(name, fn), invoke
    # the closure directly in-process. Skips server build + fastmcp
    # routing + thread hop. Same closure as the server registers, so
    # audit / scope / output-guard wrappers (applied by inner code
    # paths, not by fastmcp itself) all still fire.
    direct = _IMPLS.get(_tool_name)
    if direct is not None:
        if inspect.iscoroutinefunction(direct):
            # Async impl — reuse the running loop if present, otherwise
            # spin a one-shot via asyncio.run. Same loop-detection trick
            # as the server-path block below.
            try:
                asyncio.get_running_loop()
                import threading

                result_box: dict = {}

                def _worker():
                    try:
                        result_box["res"] = asyncio.run(direct(**impl_kwargs))
                    except BaseException as e:
                        result_box["err"] = e

                t = threading.Thread(target=_worker, daemon=True)
                t.start()
                t.join()
                if "err" in result_box:
                    raise result_box["err"]
                direct_res = result_box.get("res")
            except RuntimeError:
                direct_res = asyncio.run(direct(**impl_kwargs))
        else:
            direct_res = direct(**impl_kwargs)
        return direct_res if isinstance(direct_res, dict) else {"result": direct_res}

    # Internal execution server: expose_deploy=True so the gate can RUN a
    # registry-dispatched ai_deploy here (never agent-facing — this server's
    # tool list is not surfaced to any local agent; the gate calls tools by
    # explicit name). Empire directive 2026-07-06.
    srv = create_server(tools_profile="full", expose_deploy=True)
    coro = srv.call_tool(_tool_name, impl_kwargs)
    try:
        # If a loop is already running, do NOT call asyncio.run — that
        # raises RuntimeError. Spin a dedicated thread that owns its
        # own loop, runs the coroutine, and returns the result. This
        # is the same trick fastmcp uses internally for the stdio
        # bridge; replicating it here keeps the gate's sync path AND
        # the stdio wrapper's async path both correct.
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run.
        res = asyncio.run(coro)
    else:
        import threading

        result_box: dict = {}

        def _worker():
            try:
                result_box["res"] = asyncio.run(coro)
            except BaseException as e:
                result_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join()
        if "err" in result_box:
            raise result_box["err"]
        res = result_box.get("res")
    payload = _result_payload(res)
    return payload if isinstance(payload, dict) else {"result": payload}


def _confirm_required(spec: ToolSpec, args: dict) -> dict:
    """Build the standard `_error=confirm_required` payload — same
    shape as `project_select` / `session_select` so MCP hosts that
    learned to render the previous confirm responses render these the
    same way.
    """
    try:
        expected = spec.phrase.format(**args) if spec.phrase else ""
    except KeyError:
        expected = spec.phrase  # falls through; caller can't match
    return {
        "_error": "confirm_required",
        "_detail": (
            f"{spec.name} is confirmation-gated; ask the user before re-invoking with confirm_token"
        ),
        "action": spec.name,
        "confirm_token": expected,
        "summary": (
            f"About to invoke {spec.name} with args={args!r}. "
            f"The user must confirm before this change."
        ),
    }


def _check_confirm(spec: ToolSpec, kwargs: dict) -> dict | None:
    """Two-phase confirm gate. Returns the `confirm_required` payload
    when the caller hasn't echoed the expected phrase, else None.
    Each declared function calls this at the top before delegating.
    """
    if spec.confirm != TWO_PHASE:
        return None
    expected = build_confirm_phrase(spec, kwargs)
    # Voice-tolerant match: the action phrase is a CLOSED verb set, so
    # normalizing punctuation/case/space is safe (never a path).
    if _normalize_voice(kwargs.get("confirm_token")) != _normalize_voice(expected):
        return _confirm_required(spec, kwargs)
    return None


# ── Public accessors (consumed by outer_gate_catalog / transport) ────


_JSON_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_entry_for(ann) -> dict:
    """Map ONE parameter annotation to a JSON Schema property entry.

    Handles the three shapes a full-fidelity `@tool` declaration needs so an
    MCP host (ChatGPT) sees the real contract:
      * ``Literal["a", "b", ...]``  → ``{"type": <inferred>, "enum": [...]}``
        (the mode/kind enums the search tools advertise).
      * ``Annotated[T, "human description"]`` → entry for ``T`` + that
        ``description`` (per-param help text in tools/list).
      * ``Optional[T]`` / ``T | None`` → the entry for ``T`` (the None arm is
        the default-absent signal, already captured by `required`).
    A bare builtin maps via _JSON_TYPE_MAP; anything unknown stays "string"
    (the pre-existing permissive default — backward compatible).
    """
    import types as _types
    import typing

    # Annotated[T, meta...] — pull the first str metadata as the description.
    if hasattr(ann, "__metadata__"):
        meta = [m for m in ann.__metadata__ if isinstance(m, str)]
        entry = _json_entry_for(ann.__origin__)
        if meta:
            entry = {**entry, "description": meta[0]}
        return entry

    origin = typing.get_origin(ann)

    # Literal[...] — enum of allowed values; infer the json type from them.
    if origin is typing.Literal:
        vals = list(typing.get_args(ann))
        base = type(vals[0]) if vals else str
        return {"type": _JSON_TYPE_MAP.get(base, "string"), "enum": vals}

    # Optional[T] / Union[T, None] — unwrap to the single non-None arm. On
    # Python <3.14 a PEP-604 `T | None` has origin types.UnionType (distinct
    # from typing.Union); 3.14 unified them. Check BOTH so the gate's 3.13
    # runtime types `int | None` as 'integer', not the permissive 'string'.
    if origin is typing.Union or origin is getattr(_types, 'UnionType', object()):
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return _json_entry_for(non_none[0])
        return {"type": "string"}

    # Parametrized containers (list[str], dict[str, int], ...) — map by origin.
    if origin in (list, tuple, set, frozenset):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}

    return {"type": _JSON_TYPE_MAP.get(ann, "string")}


def flat_schema_for(spec: ToolSpec) -> dict:
    """Stage 1 of the canonical builder: the UN-enriched flat object schema
    derived purely from the declared function's signature. Exposed separately so
    the SSOT parity pin can assert schema_for == enrich(flat) without having to
    reverse-engineer (and double-apply) the enrichment."""
    import inspect
    import typing

    sig = inspect.signature(spec.fn)
    # This module uses `from __future__ import annotations` (PEP 563), so raw
    # signature annotations arrive as STRINGS. Resolve them to real types with
    # include_extras=True so Literal[...] enums and Annotated[...] descriptions
    # survive; fall back to the raw (string→permissive "string") on failure.
    try:
        hints = typing.get_type_hints(spec.fn, include_extras=True)
    except Exception:
        hints = {}
    props: dict = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        ann = hints.get(pname)
        if ann is None:
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
        if isinstance(ann, str):  # unresolved forward-ref → permissive
            ann = str
        props[pname] = _json_entry_for(ann)
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    out: dict = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def schema_for(spec: ToolSpec) -> dict:
    """Canonical JSON-Schema builder — the ONE source for BOTH the WebMCP gate
    catalog AND the local FastMCP surface (SSOT #370). Builds a flat object from
    the declared function's signature (Literal enums + Annotated descriptions via
    _json_entry_for), CLOSES it (additionalProperties:false, matching the local
    golden the gate side used to omit), and — for a mode-dispatched tool whose
    ToolSpec.modes is declared — enriches it with the mode enum + per-mode param
    usage via mode_schema.enrich_flat_schema, so the external (ChatGPT) schema
    carries the same mode contract the local agent sees. Both surfaces are then
    byte-identical projections of this one builder.
    """
    out = flat_schema_for(spec)
    # SSOT (#370): modes are declared canonically on the ToolSpec. When present,
    # the WebMCP schema carries the SAME mode enum + per-mode param usage the
    # local surface gets from mode_schema — one declaration, one enrichment.
    mode_specs = getattr(spec, "modes", None)
    if mode_specs:
        from .mode_schema import enrich_flat_schema

        out = enrich_flat_schema(out, mode_specs)
    # Closed object: external callers must not smuggle undeclared params. The
    # local golden already closed it; the gate side omitted it (co-co drift).
    out["additionalProperties"] = False
    return out


def public_schema(spec: ToolSpec) -> dict:
    """The HOST-FACING input schema (sealed design §3): the full handler schema
    MINUS the spec's `injected_args` (params the gate binds server-side and the
    host must never supply). This is the single authority a transport advertises;
    parity test #1 asserts every advertised schema equals this.
    """
    full = schema_for(spec)
    injected = set(spec.injected_args or ())
    if not injected:
        return full
    props = {k: v for k, v in (full.get("properties") or {}).items() if k not in injected}
    out: dict = {"type": "object", "properties": props}
    req = [r for r in (full.get("required") or []) if r not in injected]
    if req:
        out["required"] = req
    return out


def signature_param_names(spec: ToolSpec) -> set[str]:
    """The handler's real parameter names — the backend contract `public_schema`
    + `injected_args` are validated against (parity ledger)."""
    import inspect

    return set(inspect.signature(spec.fn).parameters)


def get(name: str) -> ToolSpec | None:
    """Return the declared spec for `name`, or None when the tool has
    not yet been migrated into this interface. Callers fall through to
    the legacy `@server.tool`-only path in that case.
    """
    return _TOOLS.get(name)


def all_specs() -> list[ToolSpec]:
    """Every declared tool in file order — what `outer_gate_catalog`
    enumerates to derive its advertised set.
    """
    return list(_TOOLS.values())


def is_local_only(name: str) -> bool | None:
    spec = _TOOLS.get(name)
    return None if spec is None else spec.surface == LOCAL_ONLY


def gate_advertised_names() -> frozenset[str]:
    """Names the gate publishes via tools/list. Union of BOTH and
    GATE_ONLY — the latter exists to honestly express "gate yes,
    stdio not yet" during a staged migration.
    """
    return frozenset(n for n, s in _TOOLS.items() if s.surface in (BOTH, GATE_ONLY))


def stdio_advertised_names() -> frozenset[str]:
    """Names the local stdio MCP server should publish via tools/list.
    Union of BOTH and LOCAL_ONLY; GATE_ONLY is excluded because it
    means "stdio not yet wired" by definition.
    """
    return frozenset(n for n, s in _TOOLS.items() if s.surface in (BOTH, LOCAL_ONLY))


def local_only_names() -> frozenset[str]:
    return frozenset(n for n, s in _TOOLS.items() if s.surface == LOCAL_ONLY)


def read_exec_names() -> frozenset[str]:
    """READ-class entries advertised on the gate. Consumed by
    `outer_gate_executor` to extend its `READ_EXEC_ALLOWLIST`, so a
    new read tool added here is automatically callable through the
    gate's read-executor without touching the allowlist file.
    """
    return frozenset(
        n for n, s in _TOOLS.items() if s.surface in (BOTH, GATE_ONLY) and s.cls == READ
    )


# ── Single-source surfacing: tool_interface IS the truth (2026-06-29) ──
# The UPS NLP path must derive keywords AND surface the full contract from THIS
# registry, not from a parallel terse catalog. These accessors are that bridge.


def tool_specs() -> dict[str, ToolSpec]:
    """Snapshot {name: ToolSpec} of the whole registry. The single source for
    NLP keyword derivation + UPS contract surfacing — no parallel catalogs."""
    return dict(_TOOLS)


def full_contract(name: str, *, max_chars: int = 600) -> str:
    """Render a tool's FULL agent-facing contract for UPS additionalContext:
    its name, description (the docstring), and modes. This is what surfaces
    once-per-epoch when the operator names a tool keyword — the real contract,
    not a terse stub. Empty string for unknown/undocumented tools."""
    spec = _TOOLS.get(name)
    if spec is None:
        return ""
    desc = " ".join((spec.description or "").split())
    if not desc:
        return ""
    line = f"{name}: {desc}"
    if spec.modes:
        try:
            modes = ", ".join(str(k) for k in spec.modes)
        except Exception:
            modes = ""
        if modes:
            line += f" (modes: {modes})"
    return line[:max_chars]


# Back-compat: callers used `tool_interface.REGISTRY[name]` and
# `build_confirm_phrase(spec, args)` before this refactor. Keep both
# working so nothing else in the tree needs touching.


def build_confirm_phrase(spec: ToolSpec, args: dict) -> str:
    if not spec.phrase:
        return ""
    try:
        return spec.phrase.format(**args)
    except KeyError:
        return spec.phrase


class _RegistryProxy(dict):
    """Read-only view onto `_TOOLS` exposed under the old name. New
    code should use `get()` / `all_specs()`; this proxy stays so the
    handful of pre-refactor call sites keep working unchanged.
    """

    def __getitem__(self, key):  # type: ignore[override]
        return _TOOLS[key]

    def __contains__(self, key):  # type: ignore[override]
        return key in _TOOLS

    def __iter__(self):  # type: ignore[override]
        return iter(_TOOLS)

    def __len__(self):  # type: ignore[override]
        return len(_TOOLS)

    def items(self):  # type: ignore[override]
        return _TOOLS.items()

    def values(self):  # type: ignore[override]
        return _TOOLS.values()

    def keys(self):  # type: ignore[override]
        return _TOOLS.keys()

    def get(self, key, default=None):  # type: ignore[override]
        return _TOOLS.get(key, default)


REGISTRY = _RegistryProxy()


# ════════════════════════════════════════════════════════════════════
# THE DECLARED AGENT-CALLABLE INTERFACE
# ════════════════════════════════════════════════════════════════════
# Add new tools below. File order = the order an operator browsing
# this file reads them. Group by class (READ → EDIT → RUN → SELECTOR →
# IMPORT → ADMIN) with a section header per class.
# ════════════════════════════════════════════════════════════════════


# ── SELECTOR / PROJECT CONTEXT ───────────────────────────────────────
# Gate-native context selectors are implemented by outer_gate_transport,
# but their public contract lives here. They are GATE_ONLY by design:
# stdio has its own managed-session lifecycle and must not advertise a
# phantom project_select binding.

@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    confirm=TWO_PHASE,
    phrase="confirm select project",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Select Project",
    },
)
def project_select(
    project_id: Annotated[
        str,
        "Registered project name or canonical ogp_<hex> id.",
    ],
    confirm_token: Annotated[
        str,
        "Omit on the first call; after human approval echo the exact server-issued token.",
    ] = "",
) -> dict:
    """Bind this token to a registered project.

    Changes the execution context every later tool call operates against.
    Two-phase confirmation is server-issued and consumable: call once
    without confirm_token, relay the returned summary to the user, then
    echo the exact returned token only after approval.
    """
    return _delegate(
        "project_select",
        project_id=project_id,
        confirm_token=confirm_token,
    )



# ── SELECTOR / LANE ─────────────────────────────────────────────────


@tool(
    # Migration finished 2026-05-29 — wired on stdio via create_server.
    # The legacy ai_lane_* siblings remain registered for a deprecation
    # window so existing clients keep working; this consolidator is the
    # canonical surface from here on. See test_consolidator_stdio_live.
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        # Doctrine 2026-05-29 (Empire seal): destructiveHint=False —
        # the consolidator routes through action= dispatch and the
        # impl arms (control / exit / grant / send) each enforce
        # their own scope/state checks; the host annotation is a UX
        # hint only. ONLY project_select/session_select advertise
        # destructiveHint=True (binding events). See
        # test_outer_gate_tools_list_metadata.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
        "title": "Lane Operations",
    },
)
def ai_lane(
    action: str,
    session_id: str = "",
    lane_id: str = "",
    worker_id: str = "",
    state: str = "",
    metadata: dict = None,
    reason: str = "",
    tools: list = None,
    prompt: str = "",
    limit: int = 20,
    confirm_token: str = "",
    backend: str = "",
    model: str = "",
    verbose: bool = False,
    review_id: str = "",
    verdict: str = "",
    tail: bool = True,
) -> dict:
    """Unified lane control + inspection — one tool for everything an
    agent does with lanes (Empire directive 2026-05-28: process-shaped
    work lives under its natural parent, no `ai_debug` catch-all).

    Modes:

      action="exit"       — self-escape: leave the current lane scope
                            without waiting for an operator turn.
                            Two-phase confirm `confirm-lane-exit <sid>`.
                            Hard-gated off worker processes (env check).

      action="state"      — worker self-reports lifecycle state
                            (running / done / failed / crashed) with
                            optional metadata. The orchestrator picks
                            it up on the next tick.

      action="summary"    — aggregate lane-worker states across one
                            or all sessions. Read-only.

      action="control"    — conductor: set a lane's state to 'active',
                            'paused', or 'canceled'. Cancel is the
                            destructive shape — two-phase confirm
                            `confirm-lane-cancel <lane_id>` for it.

      action="grant"      — conductor: delegate raw-tool access (read,
                            grep, bash, etc.) to a specific lane.
                            Two-phase confirm
                            `confirm-lane-grant <sid>:<lane_id>`.

      action="grant_scope" — conductor: additively widen a lane's gate
                            columns (#218). metadata={'kind':
                            'files'|'tools'}; items ride in `tools`.
                            files → lane_exact_paths; tools →
                            lane_extra_tools (extension, not replace).

      action="send"       — conductor: write a prompt to a parked
                            lane worker's mailbox. No confirm — the
                            worker has to consume it intentionally.
                            Resolves worker BY LANE when worker_id
                            is omitted.

      action="inbox"      — read a worker's mailbox history (pending
                            + consumed + expired). Read-only.

      action="agents"     — lane-worker roster for the session/project
                            (session_id, state filters; verbose=True also
                            lists the graveyard). Read-only. The conductor-
                            level connected-agent audit is the ai_agents tool.

    Conductor control (the SINGLE conductor surface — the scattered
    standalones fold in here; resume + kill resolve the worker BY LANE):

      action="spawn"      — dispatch a worker to `lane_id`
                            (`backend`, `model`).
      action="status"     — a lane's worker status (`lane_id` or
                            `worker_id`; `verbose`). Read-only.
      action="events"     — a lane worker's tool-call timeline
                            (`tail`, `limit`). Read-only.
      action="activity"   — audit-ledger-backed lane observability
                            (#289): per-worker recent tool events, counts,
                            and last-chat tail from execution_events (the
                            dashboard's own table). Read-only.
      action="delegate"   — ad-hoc single-task lane (#265): fire ONE
                            delegated task (`prompt`) to a fresh lane
                            worker with no plan scaffold; the worker is
                            tracked on a backlog entry its outcome writes
                            back to.
      action="kill"       — terminate the lane's worker (`reason`).
                            Two-phase confirm `confirm-worker-kill
                            <worker_id>`.
      action="resume"     — re-spawn a stalled worker's session
                            (`prompt` is the kicker).
      action="guide"      — nudge a RUNNING lane worker (`prompt` is
                            the message). No confirm.
      action="review"     — decide a lane completion review
                            (`review_id`, `verdict`, `reason`).
      action="pause"      — pause `lane_id`.

    The single conductor surface (120% clause B): ai_spawn / ai_status /
    ai_events / ai_kill / ai_resume / ai_guidance / ai_review are folded
    here and removed as standalone agent-callable tools (no aliases).
    """
    # ── exit ──────────────────────────────────────────
    if action == "exit":
        expected = f"confirm-lane-exit {session_id}"
        if (confirm_token or "") != expected:
            return {
                "_error": "confirm_required",
                "_detail": (
                    "ai_lane(action='exit') is confirm-gated; "
                    "ask the user before re-invoking with "
                    "confirm_token"
                ),
                "action": "exit",
                "session_id": session_id,
                "confirm_token": expected,
                "summary": (
                    f"About to exit the current lane for session "
                    f"{session_id or '<current>'}. The user must "
                    f"confirm before this change."
                ),
            }
        return _delegate("ai_lane_exit", session_id=session_id)
    # ── state (worker self-report) ────────────────────
    if action == "state":
        return _delegate("ai_lane_state", state=state, metadata=metadata)
    # ── summary (read-only aggregate) ─────────────────
    if action == "summary":
        return _delegate("ai_lane_summary", session_id=(session_id or None))
    # ── control (active/paused/canceled) ──────────────
    if action == "control":
        if state == "canceled":
            expected = f"confirm-lane-cancel {lane_id}"
            if (confirm_token or "") != expected:
                return {
                    "_error": "confirm_required",
                    "_detail": ("ai_lane(action='control', state='canceled') is confirm-gated"),
                    "action": "control",
                    "lane_id": lane_id,
                    "state": state,
                    "confirm_token": expected,
                    "summary": (
                        f"About to CANCEL lane {lane_id!r}. The user "
                        f"must confirm before this change."
                    ),
                }
        return _delegate(
            "ai_lane_control",
            lane_id=lane_id,
            state=state,
            reason=reason,
            session_id=session_id,
        )
    # ── grant (conductor delegates raw-tool access) ───
    if action == "grant":
        expected = f"confirm-lane-grant {session_id}:{lane_id}"
        if (confirm_token or "") != expected:
            return {
                "_error": "confirm_required",
                "_detail": ("ai_lane(action='grant') is confirm-gated"),
                "action": "grant",
                "session_id": session_id,
                "lane_id": lane_id,
                "confirm_token": expected,
                "summary": (
                    f"About to grant raw-tool access ({tools!r}) to "
                    f"lane {lane_id!r} in session {session_id!r}. "
                    f"The user must confirm before this change."
                ),
            }
        return _delegate(
            "ai_lane_grant",
            session_id=session_id,
            lane_id=lane_id,
            tools=tools,
            reason=reason,
        )
    # ── send (write to mailbox) ───────────────────────
    if action == "send":
        # WAR D (#452) Task 3: a send without a resolvable worker would
        # store an undeliverable mailbox row (worker_id="") and report ok.
        # Refuse instead — the wrapper resolves lane_id → worker_id; when
        # even that fails, the lane has no worker to deliver to.
        if not worker_id:
            return {
                "_error": "missing_worker",
                "_detail": (
                    "action='send' requires worker_id or a lane_id that "
                    "resolves to a live/most-recent worker — no worker "
                    "found; the message would be undeliverable"
                ),
                "lane_id": lane_id,
            }
        return _delegate("ai_lane_send", worker_id=worker_id, session_id=session_id, prompt=prompt)
    # ── inbox (read mailbox history) ──────────────────
    if action == "inbox":
        if not worker_id:
            return {
                "_error": "missing_worker",
                "_detail": (
                    "action='inbox' requires worker_id or a lane_id that "
                    "resolves to a live/most-recent worker"
                ),
                "lane_id": lane_id,
            }
        return _delegate("ai_lane_inbox", worker_id=worker_id, limit=limit)
    # ── grant_scope (#218: conductor widens a lane's gate columns) ────
    # Additive-only. kind ∈ {'files','tools'} (metadata={'kind': ...}),
    # items ride in `tools`. files → lane_exact_paths (+lane_scopes);
    # tools → lane_extra_tools (EXTENSION of the declared set).
    if action == "grant_scope":
        if not lane_id:
            return {
                "_error": "missing_lane_id",
                "_detail": "action='grant_scope' requires lane_id",
            }
        _gs_kind = str((metadata or {}).get("kind") or "").strip().lower()
        if _gs_kind not in ("files", "tools"):
            return {
                "_error": "missing_kind",
                "_detail": (
                    "action='grant_scope' requires metadata={'kind': "
                    "'files'|'tools'}; items ride in the `tools` list"
                ),
            }
        _gs_items = [str(t).strip() for t in (tools or []) if str(t).strip()]
        if not _gs_items:
            return {
                "_error": "missing_items",
                "_detail": (
                    "action='grant_scope' requires a non-empty `tools` "
                    "list carrying the items to grant"
                ),
            }
        return _delegate(
            "ai_lane_grant_scope",
            session_id=session_id,
            lane_id=lane_id,
            kind=_gs_kind,
            items=_gs_items,
            reason=reason,
        )
    # ── agents (lane-worker roster; read-only) ────────
    if action == "agents":
        return _delegate(
            "ai_lane_agents",
            session_id=session_id,
            state=state,
            include_graveyard=verbose,
        )
    # ══ CONDUCTOR CONTROL (C.B consolidation) ══════════════════════════
    # The scattered standalones (ai_spawn / ai_status / ai_events / ai_kill /
    # ai_resume / ai_guidance / ai_review) fold in here as the SINGLE conductor
    # surface. Worker-targeting actions take lane_id and the @server.tool
    # wrapper resolves the live/most-recent worker (resume + kill BY LANE).
    # ── spawn (dispatch a worker to a lane) ───────────
    if action == "spawn":
        if not lane_id:
            return {"_error": "missing_lane_id", "_detail": "action='spawn' requires lane_id"}
        return _delegate(
            "ai_spawn", session_id=session_id, lane_id=lane_id, backend=backend or "claude",
            model=model,
        )
    # ── status (a lane's worker; read-only) ───────────
    if action == "status":
        if not worker_id:
            return {
                "_error": "missing_worker",
                "_detail": "action='status' requires worker_id or a resolvable lane_id",
            }
        return _delegate("ai_status", worker_id=worker_id, verbose=verbose)
    # ── events (a lane worker's tool-call timeline) ───
    if action == "events":
        return _delegate(
            "ai_events", worker_id=worker_id, lane_id=lane_id, session_id=session_id,
            tail=tail, limit=limit,
        )
    # ── activity (#289: audit-ledger-backed lane observability) ───
    # Read-only. Same execution_events table the dashboard reads; uses only
    # existing ai_lane params so the public inputSchema + gate goldens hold.
    if action == "activity":
        return _delegate(
            "ai_lane_activity",
            session_id=session_id,
            lane_id=lane_id,
            worker_id=worker_id,
            limit=limit,
        )
    # ── delegate (#265: ad-hoc single-task lane, no plan) ───
    # Spawns ONE tracked lane worker whose brief + outcome live on a
    # project-backlog tracking entry (no plan scaffold). Uses only
    # existing ai_lane params so the public inputSchema + gate goldens
    # do not drift.
    if action == "delegate":
        return _delegate(
            "ai_lane_delegate",
            session_id=session_id,
            prompt=prompt,
            backend=backend,
            model=model,
        )
    # ── kill (terminate a lane's worker) ──────────────
    if action == "kill":
        if not worker_id:
            return {
                "_error": "missing_worker",
                "_detail": "action='kill' requires worker_id or a resolvable lane_id",
            }
        if not reason:
            return {"_error": "missing_reason", "_detail": "action='kill' requires reason"}
        expected = f"confirm-worker-kill {worker_id}"
        if (confirm_token or "") != expected:
            return {
                "_error": "confirm_required",
                "_detail": "ai_lane(action='kill') is confirm-gated — killing the wrong worker wipes in-flight state",
                "action": "kill",
                "worker_id": worker_id,
                "lane_id": lane_id,
                "confirm_token": expected,
                "summary": f"About to kill worker {worker_id!r} (lane {lane_id!r}, reason {reason!r}). Confirm first.",
            }
        return _delegate("ai_kill", worker_id=worker_id, reason=reason)
    # ── resume (re-spawn a stalled worker's session) ──
    if action == "resume":
        if not worker_id:
            return {
                "_error": "missing_worker",
                "_detail": "action='resume' requires worker_id or a resolvable lane_id",
            }
        return _delegate("ai_resume", worker_id=worker_id, prompt=prompt or "continue", model=model)
    # ── guide (nudge a RUNNING lane worker) ───────────
    if action == "guide":
        if not lane_id:
            return {"_error": "missing_lane_id", "_detail": "action='guide' requires lane_id"}
        if not prompt:
            return {"_error": "missing_prompt", "_detail": "action='guide' requires prompt (the guidance message)"}
        return _delegate("ai_guidance", lane_id=lane_id, message=prompt, session_id=session_id)
    # ── review (decide a lane completion review) ──────
    if action == "review":
        if not review_id or not verdict:
            return {
                "_error": "missing_review_args",
                "_detail": "action='review' requires review_id and verdict ('approved'|'denied')",
            }
        return _delegate("ai_review", review_id=review_id, verdict=verdict, message=(reason or prompt))
    # ── pause (pause a lane) ──────────────────────────
    if action == "pause":
        if not lane_id:
            return {"_error": "missing_lane_id", "_detail": "action='pause' requires lane_id"}
        return _delegate(
            "ai_lane_control", lane_id=lane_id, state="paused", reason=reason, session_id=session_id
        )
    return {
        "_error": "unknown_action",
        "_detail": (
            f"ai_lane: action={action!r} not recognized "
            "(expected: exit, state, summary, control, grant, send, inbox, agents, "
            "spawn, status, events, activity, delegate, kill, resume, guide, review, pause)"
        ),
    }


# ── PLAN ────────────────────────────────────────────────────────────


@tool(
    # Migration finished 2026-05-29 — wired on stdio via create_server.
    # Legacy ai_plan_* siblings remain registered for the deprecation
    # window. See test_consolidator_stdio_live.
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
        "title": "Plan Operations",
    },
)
def ai_plan(
    action: str,
    session_id: str = "",
    lane_id: str = "",
    spec_text: str = "",
    spec_path: str = "",
    scope: str = "",
    constraints: list = None,
    file_path: str = "",
    paused_lane_id: str = "",
    conflicting_lane_id: str = "",
    target_lane_id: str = "",
    signal_kind: str = "",
    detail: str = "",
    packet_result: dict = None,
    packet_result_path: str = "",
    reason: str = "",
    timeout: int = 0,
    view: str = "",
    transition: str = "",
    coord: str = "",
    template_only: bool = False,
    backend: str = "",
    model: str = "",
    target_project: str = "",
) -> dict:
    """Unified plan + lane orchestration. 13 ai_plan_* siblings →
    6 modes (operator directive 2026-05-28: aggressive consolidation
    over rename-only).

    Modes:

      action="create"
          Create a plan from `spec_text` or `spec_path`. Optional
          scope/constraints/timeout. Set `template_only=True` (via the
          `transition` arg as 'template') to return just the empty
          PLAN.md template instead of creating.

      action="inspect"
          Read-only view over a session's plan. `view='graph'` (DAG +
          dependencies) or `view='status'` (execution state).

      action="dispatch"
          Dispatch ready lanes to workers. Mutates worker state.

      action="report"
          Record a worker's completion packet (from `packet_result`
          or `packet_result_path`) against session_id.

      action="lifecycle"
          Lane state transitions. `transition=` ∈ {pause, resume,
          reopen, ready, unready, expand}. Single mutating verb for
          every per-lane state change.

      action="coordinate"
          Inter-lane coordination. `coord=` ∈ {overlap, signal}.
          `overlap` resolves file-overlap conflicts (paused_lane_id +
          conflicting_lane_id touching file_path); `signal` sends
          signal_kind from lane_id to target_lane_id with detail.

    cls=EDIT scope=tier_m_edit covers the worst-case mode; read modes
    (`inspect`) pay the same scope but their impls don't mutate. No
    registry-level confirm — orchestration primitives are gated by
    the impl's own checks; dangerous patterns (rm-style chains,
    pushed commits) only reach state via ai_run + bash_policy or
    ai_lane(action='control', state='canceled').
    """
    # ── create ────────────────────────────────────────
    if action == "create":
        if transition == "template" or template_only:
            return _delegate("ai_plan_template")
        return _delegate(
            "ai_plan_create",
            session_id=session_id,
            spec_text=spec_text,
            spec_path=spec_path,
            scope=(scope or None),
            constraints=constraints,
            timeout=(timeout or None),
        )
    # ── inspect (graph / status) ──────────────────────
    if action == "inspect":
        v = view or "graph"
        if v == "graph":
            return _delegate("ai_plan_graph", session_id=session_id, timeout=(timeout or None))
        if v == "status":
            return _delegate("ai_plan_status", session_id=session_id, timeout=(timeout or None))
        return {
            "_error": "unknown_view",
            "_detail": (
                f"ai_plan(action='inspect'): view={v!r} not recognized (expected: graph, status)"
            ),
        }
    # ── dispatch ──────────────────────────────────────
    if action == "dispatch":
        return _delegate("ai_plan_dispatch", session_id=session_id, timeout=(timeout or None))
    # ── spawn (one worker for one lane — finer than dispatch) ──
    if action == "spawn":
        return _delegate(
            "ai_spawn",
            session_id=session_id,
            lane_id=lane_id,
            backend=(backend or "claude"),
            timeout=(timeout or 600),
            target_project=target_project,
            model=model,
        )
    # ── report ────────────────────────────────────────
    if action == "report":
        return _delegate(
            "ai_plan_report",
            session_id=session_id,
            packet_result=packet_result,
            packet_result_path=packet_result_path,
            timeout=(timeout or None),
        )
    # ── lifecycle (pause/resume/reopen/ready/unready/expand) ─
    if action == "lifecycle":
        t = transition or ""
        if t == "pause":
            return _delegate("ai_plan_pause", session_id=session_id, lane_id=lane_id, reason=reason)
        if t == "resume":
            return _delegate(
                "ai_plan_resume",
                session_id=session_id,
                lane_id=lane_id,
                timeout=(timeout or None),
            )
        if t == "reopen":
            return _delegate(
                "ai_plan_reopen",
                session_id=session_id,
                lane_id=lane_id,
                reason=reason,
            )
        if t in ("ready", "unready"):
            return _delegate(
                "ai_plan_mark_ready",
                session_id=session_id,
                lane_id=lane_id,
                ready=(t == "ready"),
                timeout=(timeout or None),
            )
        if t == "expand":
            return _delegate(
                "ai_plan_expand",
                session_id=session_id,
                lane_id=lane_id,
                file_path=file_path,
                reason=reason,
            )
        return {
            "_error": "unknown_transition",
            "_detail": (
                f"ai_plan(action='lifecycle'): "
                f"transition={t!r} not recognized (expected: "
                "pause, resume, reopen, ready, unready, "
                "expand)"
            ),
        }
    # ── coordinate (overlap / signal) ─────────────────
    if action == "coordinate":
        c = coord or ""
        if c == "overlap":
            return _delegate(
                "ai_plan_overlap",
                session_id=session_id,
                paused_lane_id=paused_lane_id,
                conflicting_lane_id=conflicting_lane_id,
                file_path=file_path,
                timeout=(timeout or None),
            )
        if c == "signal":
            return _delegate(
                "ai_plan_signal",
                session_id=session_id,
                lane_id=lane_id,
                signal_kind=signal_kind,
                target_lane_id=target_lane_id,
                detail=detail,
                timeout=(timeout or None),
            )
        return {
            "_error": "unknown_coord",
            "_detail": (
                f"ai_plan(action='coordinate'): coord={c!r} "
                "not recognized (expected: overlap, signal)"
            ),
        }
    return {
        "_error": "unknown_action",
        "_detail": (
            f"ai_plan: action={action!r} not recognized "
            "(expected: create, inspect, dispatch, report, "
            "lifecycle, coordinate)"
        ),
    }


# ── WORKER ──────────────────────────────────────────────────────────


@tool(
    # Migration finished 2026-05-29 — wired on stdio via create_server.
    # Legacy worker-mgmt siblings remain registered for the deprecation
    # window. See test_consolidator_stdio_live.
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        # Doctrine 2026-05-29 (Empire seal): destructiveHint=False — see
        # ai_lane above for the rationale. AIDOCS owns refusal at the
        # impl layer (kill goes through worker_id resolution + audit);
        # the host hint stays non-destructive so it does NOT pop a
        # blanket confirm card on every status/list inspection.
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
        "title": "Worker Operations",
    },
)
def ai_worker(
    action: str,
    worker_id: str = "",
    lane_id: str = "",
    reason: str = "",
    verbose: bool = False,
    confirm_token: str = "",
) -> dict:
    """Worker management — status, list, kill, and resume for plan-task
    workers. Resolves the `ai_kill` (worker_id) vs `ai_run(action=
    'kill')` (run_id) name collision that the operator caught
    2026-05-28: workers and shell processes are different subsystems,
    one parent each.

    BY LANE: pass `lane_id` (no `worker_id`) and the live/most-recent
    worker for that lane is resolved automatically — a conductor thinks
    in lanes, not opaque worker ids (120% clause B). Explicit `worker_id`
    still wins.

    Modes:

      action="status"       — return status for the worker. Read-only.
                              `verbose=True` adds the full state dump.

      action="list"         — list every active worker (no args).
                              Read-only. `verbose=True` adds full state.

      action="kill"         — terminate the worker with `reason`.
                              Two-phase confirm
                              `confirm-worker-kill <worker_id>`
                              because killing the wrong worker can
                              wipe in-flight task state.

      action="resume"       — re-spawn the worker resuming its prior
                              opencode session (`reason` is the kicker
                              prompt).

    cls=EDIT scope=tier_m_edit. Read modes share the scope; kill needs
    confirm. The underlying impls live in server_plan_task_tools.py
    (ai_status / ai_jobs / ai_kill) and stay registered for local stdio
    until C.20's create_server-iterates-registry phase lands.
    """
    if action == "status":
        if not worker_id:
            return {"_error": "missing_worker_id", "_detail": "action='status' requires worker_id"}
        return _delegate("ai_status", worker_id=worker_id, verbose=verbose)
    if action == "list":
        return _delegate("ai_jobs", verbose=verbose)
    if action == "kill":
        if not worker_id:
            return {"_error": "missing_worker_id", "_detail": "action='kill' requires worker_id"}
        if not reason:
            return {
                "_error": "missing_reason",
                "_detail": ("action='kill' requires reason (audit-visible)"),
            }
        expected = f"confirm-worker-kill {worker_id}"
        if (confirm_token or "") != expected:
            return {
                "_error": "confirm_required",
                "_detail": (
                    "ai_worker(action='kill') is confirm-"
                    "gated — killing the wrong worker can "
                    "wipe in-flight task state"
                ),
                "action": "kill",
                "worker_id": worker_id,
                "confirm_token": expected,
                "summary": (
                    f"About to kill worker {worker_id!r} (reason: "
                    f"{reason!r}). The user must confirm before "
                    f"this change."
                ),
            }
        return _delegate("ai_kill", worker_id=worker_id, reason=reason)
    if action == "resume":
        if not worker_id:
            return {"_error": "missing_worker_id", "_detail": "action='resume' requires worker_id"}
        return _delegate("ai_resume", worker_id=worker_id, prompt=reason or "continue", model="")
    return {
        "_error": "unknown_action",
        "_detail": (
            f"ai_worker: action={action!r} not recognized (expected: status, list, kill, resume)"
        ),
    }


# ── ADMIN (local-only break-glass — never advertised on the gate) ────


# SSOT-05 #386/#288 (2026-07-18): surface=HIDDEN — operator break-glass is
# driven from the dashboard/CLI, never the agent tools/list. Still registered
# and callable (the CLI/dashboard path + two-phase confirm are unchanged).
@tool(
    surface=HIDDEN,
    cls=ADMIN,
    tier=A,
    scope="",
    confirm=TWO_PHASE,
    phrase="confirm clear freeze",
    annotations={
        "destructiveHint": True,
        "openWorldHint": False,
        "title": "Admin Clear Freeze (Break-Glass)",
    },
)
def admin_clear_freeze(
    freeze_id: str = "",
    session_id: str = "",
    reason: str = "",
    confirm_token: str = "",
) -> dict:
    """Break-glass: clear a session freeze WITHOUT minting a grant.

    Local-only — never exposed on the outer gate. The freeze gate is
    skipped by doctrine; this tool is how the operator EXITS a freeze.
    Even locally, requires a two-phase confirm so a misclick can't
    silently lift a real lockdown. Existing RBAC + audit + kill-switch
    logic in the underlying handler runs untouched after confirm.

    Args:
        freeze_id: Preferred — unambiguous lookup by request_id.
        session_id: Only valid if exactly one active freeze exists for
            that session.
        reason: Audit reason string.
        confirm_token: the speakable phrase 'confirm clear freeze'
            (case/punctuation/space-insensitive). Omit on the first call
            to receive the token in the confirm_required response.

    #301: the clearer identity is derived from the authenticated caller;
    there is no approver_email argument and no break-glass bypass (#404).
    """
    # The handler in server_rbac_tools already enforces the same
    # two-phase confirm at its top, so a direct delegate suffices —
    # consumers calling through tool_interface get the same gate.
    return _delegate(
        "admin_clear_freeze",
        freeze_id=freeze_id,
        session_id=session_id,
        reason=reason,
        confirm_token=confirm_token,
    )


@tool(
    surface=LOCAL_ONLY,
    cls=ADMIN,
    tier=A,
    scope="",
    confirm=NO_CONFIRM,
    annotations={
        "destructiveHint": False,
        "openWorldHint": False,
        "title": "Admin Clear Reconnect Lock",
    },
)
def admin_clear_reconnect(
    session_id: str = "",
    reason: str = "",
) -> dict:
    """Clear a reconnect-lock for an agent session.

    Local-only — never exposed on the outer gate. Less critical than
    `admin_clear_freeze` (it's about reconnect retry state, not active
    lockdown), so no confirm phrase is required.

    Args:
        session_id: Session whose reconnect-lock to clear.
        reason: Audit reason string.

    """
    return _delegate(
        "admin_clear_reconnect",
        session_id=session_id,
        reason=reason,
    )


# ── CONDUCTOR misc ──────────────────────────────────────────────────
# Standalone conductor / orchestration tools that don't fold cleanly
# under a parent (each is its own concept). All cls=EDIT scope=
# tier_m_edit (mutating) except ai_events / ai_guidance read.


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "send": {"required": ["to_roles", "body"], "optional": ["in_reply_to"], "desc": "post a role-addressed message to the named seat roles"},
        "inbox": {"required": [], "optional": ["unread_only"], "desc": "list role messages addressed to the calling seat"},
        "reply": {"required": ["message_id", "body"], "optional": [], "desc": "reply to a specific role message by message_id"},
        "xaacp_send": {"required": ["session_id", "target_actor_id", "lane_id", "message_kind", "body"], "optional": ["correlation_id", "in_reply_to", "metadata", "wake"], "desc": "actor-routed XAACP send: deliver a typed message to an exact session/lane actor"},
        "xaacp_inbox": {"required": ["session_id", "lane_id"], "optional": ["unread_only", "mark_read", "limit"], "desc": "list XAACP messages routed to the calling actor on a session/lane"},
        "xaacp_reply": {"required": ["message_id", "session_id", "decision"], "optional": ["body"], "desc": "record an XAACP decision (accepted/rejected/completed/blocked) on a pending message"},
        "xaacp_wait": {"required": ["message_id", "session_id"], "optional": ["timeout_seconds"], "desc": "block until an XAACP message reaches a terminal decision (or timeout)"},
        "xaacp_cancel": {"required": ["message_id", "session_id"], "optional": ["reason"], "desc": "cancel a pending XAACP message the calling actor originated"},
    },
)
def ai_msg(
    mode: str,
    to_roles: str = "",
    body: str = "",
    in_reply_to: str = "",
    message_id: str = "",
    unread_only: bool = True,
    mark_read: bool = False,
    limit: int = 50,
    session_id: str = "",
    target_actor_id: str = "",
    lane_id: str = "",
    message_kind: str = "",
    correlation_id: str = "",
    decision: str = "",
    timeout_seconds: float = 0.0,
    wake: bool = False,
    metadata: dict[str, Any] | None = None,
    reason: str = "",
) -> dict:
    """One inter-agent channel for role messages and actor-routed XAACP.

    Legacy modes send|inbox|reply preserve the seat-role contract.
    Actor-routed modes xaacp_send|xaacp_inbox|xaacp_reply|xaacp_wait|
    xaacp_cancel derive the calling actor in the implementation and require
    an exact session/lane route. Decision status is separate from transport
    truth: a rejected/blocked/canceled decision still returns ok=True.
    """
    return _delegate(
        "ai_msg",
        mode=mode,
        to_roles=to_roles,
        body=body,
        in_reply_to=in_reply_to,
        message_id=message_id,
        unread_only=unread_only,
        mark_read=mark_read,
        limit=limit,
        session_id=session_id,
        target_actor_id=target_actor_id,
        lane_id=lane_id,
        message_kind=message_kind,
        correlation_id=correlation_id,
        decision=decision,
        timeout_seconds=timeout_seconds,
        wake=wake,
        metadata=metadata,
        reason=reason,
    )


# Empire 2026-06-20: ai_task folded into the registry. It was a raw @server.tool
# (server_plan_task_tools) outside the governance system — callable locally but
# invisible to the registry/allowlists and unreachable on the gate, which is why
# the M-tier task-gate couldn't be satisfied remotely (#59). EDIT/M like ai_session:
# now advertised on BOTH surfaces; remote INVOCATION still waits on Tier-M enablement
# (Phase 2) — this makes it discoverable + governed, the prerequisite for #59.
#
# #83 HARD merge (Emperor ruling 2026-07-18, no-alias law): ai_task absorbs the
# former ai_todo surface, scope-driven. Todo modes add|list|remove (and 'update'
# when scope='task'|'session' is passed) route to the retained impl in
# server_todo_backlog_tools.build_todo_handler; lifecycle modes are unchanged
# (begin/complete required-sets and evidence-gate semantics identical). The
# grammar follows the house dispatcher pattern (ai_backlog/ai_memory): ONE mode
# enum, distinct names per operation, with the single colliding name ('update')
# disambiguated by the explicit scope param — scope is exclusively a todo-side
# param, so its presence is the deterministic todo signal. Tier/scope for the
# todo modes carries over exactly (ai_todo was EDIT/M/tier_m_edit; ai_task is
# EDIT/M/tier_m_edit — zero security delta).
@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
        "title": "Task Lifecycle + Todos",
    },
    modes={
        "begin": {"required": ["session_id"], "optional": ["goal", "state", "upcoming", "partial_goals", "end_goal", "blockers", "relevant_files", "relevant_commands", "relevant_snippets", "relevant_snippets_path", "session_facts", "session_facts_path", "constraints", "include_code_bundle", "include_tests"], "desc": "register task start before non-trivial work (goal recommended)"},
        "update": {"required": [], "optional": ["session_id", "state", "upcoming", "partial_goals", "end_goal", "blockers", "relevant_files", "relevant_commands", "relevant_snippets", "session_facts", "constraints", "include_code_bundle", "include_tests", "summary_only", "scope", "id", "content", "status", "tags", "urgency", "reason"], "desc": "no scope: record progress on the open task (session_id). With scope='task'|'session': patch a todo by id (status='done' marks it done)"},
        "complete": {"required": ["session_id", "result_summary"], "optional": ["next_status", "verification_evidence", "include_code_bundle", "include_tests"], "desc": "close the task with result_summary + verification evidence"},
        "status": {"required": ["session_id"], "optional": [], "desc": "peek at the open task"},
        "add": {"required": ["content"], "optional": ["tags", "scope", "urgency"], "desc": "create a todo owned by the current task (active task required)"},
        "list": {"required": [], "optional": ["scope", "include_done", "include_removed", "columns", "tags", "format"], "desc": "list todos — scope='task' (default, current task) or 'session'; format='table'|'csv' opt-in"},
        "remove": {"required": ["id"], "optional": ["reason", "scope"], "desc": "soft-remove a todo by id (reason >=8 chars)"},
    },
)
def ai_task(
    mode: str,
    session_id: str = "",
    goal: str | None = None,
    state: list[str] | None = None,
    upcoming: list[str] | None = None,
    partial_goals: list[str] | None = None,
    end_goal: str | None = None,
    blockers: list[str] | None = None,
    relevant_files: list[str] | None = None,
    relevant_commands: list[str] | None = None,
    relevant_snippets: list[str] | None = None,
    relevant_snippets_path: str = "",
    session_facts: list[str] | None = None,
    session_facts_path: str = "",
    constraints: list[str] | None = None,
    include_code_bundle: bool = False,
    include_tests: bool = False,
    summary_only: bool = True,
    result_summary: str = "",
    next_status: str = "done",
    verification_evidence: dict[str, Any] | None = None,
    tool_report: list[dict[str, Any]] | None = None,
    scope: str = "",
    content: str = "",
    id: int = 0,
    status: str = "",
    tags: list[str] | None = None,
    include_done: bool = False,
    include_removed: bool = False,
    reason: str = "",
    columns: list[str] | None = None,
    urgency: str = "",
    format: str = "",
) -> Any:
    """Unified work-item tool — task lifecycle + task-owned todos (#83).

    Lifecycle (the task itself; session_id resolves via host binding):
    mode='begin'    — register a new task (required: session_id).
    mode='update'   — record progress on the active task (no scope).
    mode='complete' — close the active task (required: session_id, result_summary).
    mode='status'   — read-only peek at the active task (required: session_id).

    Todos (items owned by the current task/session — the former ai_todo):
    mode='add'|'list'|'remove' — create/list/tombstone todo items;
    mode='update' with scope='task'|'session' — patch a todo by id
    (the explicit scope routes 'update' to the todo surface; without it,
    'update' is the lifecycle progress update). There is no 'done' mode:
    mark a todo done via mode='update', scope='task', status='done'.
    list accepts format='table'|'csv' (#20, opt-in; default structured).
    """
    return _delegate(
        "ai_task",
        mode=mode,
        session_id=session_id,
        goal=goal,
        state=state,
        upcoming=upcoming,
        partial_goals=partial_goals,
        end_goal=end_goal,
        blockers=blockers,
        relevant_files=relevant_files,
        relevant_commands=relevant_commands,
        relevant_snippets=relevant_snippets,
        relevant_snippets_path=relevant_snippets_path,
        session_facts=session_facts,
        session_facts_path=session_facts_path,
        constraints=constraints,
        include_code_bundle=include_code_bundle,
        include_tests=include_tests,
        summary_only=summary_only,
        result_summary=result_summary,
        next_status=next_status,
        verification_evidence=verification_evidence,
        tool_report=tool_report,
        scope=scope,
        content=content,
        id=id,
        status=status,
        tags=tags,
        include_done=include_done,
        include_removed=include_removed,
        reason=reason,
        columns=columns,
        urgency=urgency,
        format=format,
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "connect": {"required": [], "optional": ["session_id"], "desc": "bind this agent to a session (default: most recent active)"},
        "bind": {"required": [], "optional": ["session_id"], "desc": "alias of connect"},
        "list": {"required": [], "optional": [], "desc": "list the project's sessions"},
        "create": {"required": ["title"], "optional": ["goal", "session_id", "owner", "scope", "status", "predecessor_session_id"], "desc": "create a new session (title required)"},
        "claim": {"required": ["session_id", "agent_id", "run_id"], "optional": ["claim_mode"], "desc": "take the session worklock"},
        "claim_status": {"required": ["session_id"], "optional": ["stale_after_minutes"], "desc": "who holds the worklock + staleness"},
        "release": {"required": ["session_id", "agent_id"], "optional": ["run_id"], "desc": "release the session worklock"},
        "update": {"required": ["session_id", "patch"], "optional": [], "desc": "patch SESSION.md fields"},
        "resume": {"required": ["session_id"], "optional": ["include_code_bundle", "include_tests", "journal_last_n"], "desc": "full resume bundle: SESSION.md + journal tail (+ optional code bundle)"},
        "skills_get": {"required": ["session_id"], "optional": [], "desc": "read the session's selected skills"},
        "skills_set": {"required": ["session_id", "selected_skills"], "optional": [], "desc": "set the session's selected skills"},
    },
)
def ai_session(
    mode: str,
    session_id: str = "",
    title: str = "",
    goal: str = "",
    owner: str = "",
    scope: str = "-",
    status: str = "active",
    predecessor_session_id: str = "",
    agent_id: str = "",
    run_id: str = "",
    claim_mode: str = "active",
    stale_after_minutes: int = 30,
    patch: dict = None,
    selected_skills: list = None,
    include_code_bundle: bool = False,
) -> dict:
    """Session lifecycle — mode='connect'|'list'|'create'|'claim'|
    'claim_status'|'release'|'update'|'resume'|'skills_get'|'skills_set'
    (the impl's real accepted set). connect: bind the calling agent to a
    session ('bind' is an alias); list: list sessions; create: new session
    (title); claim/claim_status/release: advisory claims; update: patch
    SESSION.md sections; resume: resume bundle; skills_get/skills_set:
    selected-skill list. Multi-mode at the impl layer; registry forwards
    verbatim.
    """
    return _delegate(
        "ai_session",
        mode=mode,
        session_id=session_id,
        title=title,
        goal=goal,
        owner=owner,
        scope=scope,
        status=status,
        predecessor_session_id=(predecessor_session_id or None),
        agent_id=agent_id,
        run_id=run_id,
        claim_mode=claim_mode,
        stale_after_minutes=stale_after_minutes,
        patch=patch,
        selected_skills=selected_skills,
        include_code_bundle=include_code_bundle,
    )


@tool(
    surface=LOCAL_ONLY,
    cls=SELECTOR,
    tier=M,
    scope="tier_m_edit",
    confirm=TWO_PHASE,
    phrase="confirm project bind",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "ai_project (bind host session → project)",
    },
    modes={
        "bind": {"required": ["project_root"], "optional": ["confirm_token"], "desc": "project_root=path → bind THIS host session to that project (RBAC-gated)"},
        "status": {"required": [], "optional": [], "desc": "show the current project bind"},
        "unbind": {"required": [], "optional": [], "desc": "clear the project bind"},
        "list": {"required": [], "optional": [], "desc": "list known AIDOCS projects"},
    },
)
def ai_project(
    mode: str,
    project_root: str = "",
    confirm_token: str = "",
) -> dict:
    """Bind THIS host session to an AIDOCS-enabled project — the LOCAL
    mirror of the outer gate's project_select.

    Modes:
      bind   — bind host_session → project_root so every later ai_* call
               re-roots to that tree. Two-phase confirm; RBAC-gated via
               project_authority.require_cross_project (same-project always
               allowed; cross-tree needs commissioned + approved relation +
               permission). On deny, files an admin escalation.
      status — show this host session's current bind (if live).
      unbind — drop the bind (revert to cwd-discovery).
      list   — list AIDOCS-enabled projects, marking the bound one.

    The bind is keyed per host_session_id (cross-user isolated) with an
    idle TTL (default 30 min, dashboard-configurable) refreshed on activity.

    Args:
        mode: bind | status | unbind | list.
        project_root: target tree (required for bind).
        confirm_token: the speakable phrase 'confirm project bind'
            (case/punctuation/space-insensitive) — omit on the first
            bind call to receive it in the confirm_required response.
    Local-only: never advertised on the outer gate (which has project_select).
    """
    return _delegate(
        "ai_project",
        mode=mode,
        project_root=project_root,
        confirm_token=confirm_token,
    )


@tool(
    surface=BOTH,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "ai_version (live build stamp)",
    },
    modes={
        "local": {"required": [], "optional": []},
        "deploy": {"required": [], "optional": []},
        "release": {"required": [], "optional": []},
    },
)
def ai_version(mode: str = "local") -> dict:
    """The build currently live on this webmcp gate — build_number (a monotonic
    counter bumped on every deploy) plus commit, version, created_at and builder.
    Call it to see exactly what code is running on mcp.codenexus.cloud; no org or
    project selection needed. Modes: local (running source, default) / deploy
    (last --deploy seal) / release (signed manifest).
    """
    return _delegate("ai_version", mode=mode)


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    aliases=("conductor", "seat"),
    modes={
        "enter": {"required": [], "optional": ["session_id", "verbose"], "desc": "become the session conductor (verbose=True adds SESSION.md + journal tail)"},
        "exit": {"required": [], "optional": [], "desc": "clear the inline-conductor marker"},
        "status": {"required": [], "optional": [], "desc": "is a conductor running? (process/inline info)"},
        "overview": {"required": [], "optional": ["session_id"], "desc": "full situational awareness: lanes, states, pending questions"},
    },
)
def ai_seat(
    mode: str,
    session_id: str = "",
    verbose: bool = False,
) -> dict:
    """Agent seat lifecycle — mode='enter'|'exit'|'status'|'overview'
    (the impl's real accepted set). enter: become the session conductor
    (verbose=True adds SESSION.md body + journal tail); exit: clear the
    inline-conductor marker; status: is the conductor running; overview:
    full conductor situational awareness (lanes, states, pending Qs).
    Conductor-side primitive for binding which agent is at which seat.
    """
    return _delegate("ai_seat", mode=mode, session_id=session_id, verbose=verbose)


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "read": {"required": ["skill_id"], "optional": ["sovereign_authority"], "desc": "return a public skill row by skill_id"},
        "upsert": {"required": ["skill_id", "name", "content_text"], "optional": ["description", "kind", "tags", "sovereign_authority", "sovereign_owner", "read_access"], "desc": "insert or update a public skill"},
        "list": {"required": [], "optional": [], "desc": "list all skills in the catalog (bundled + registered)"},
        "delete": {"required": ["skill_id"], "optional": [], "desc": "remove a public skill row"},
    },
)
def ai_skill(
    mode: str,
    skill_id: str,
    name: str = "",
    content_text: str = "",
    description: str = "",
    kind: str = "skill",
    tags: str = "",
    sovereign_authority: bool = False,
    sovereign_owner: str = "",
    read_access: str = "",
) -> dict:
    """Public empire-skill door — mode='read'|'upsert'|'delete'|'list' (the impl's
    real accepted set; anything else returns invalid_mode). list: all skills in
    the catalog (bundled + registered). read: return a
    row by skill_id; upsert: insert/update (requires skill_id, name,
    content_text); delete: remove a row by skill_id. Sovereign-authority
    operations gate at the impl layer (souls live behind ai_soul).
    """
    return _delegate(
        "ai_skill",
        mode=mode,
        skill_id=skill_id,
        name=name,
        content_text=content_text,
        description=description,
        kind=kind,
        tags=tags,
        sovereign_authority=sovereign_authority,
        sovereign_owner=(sovereign_owner or None),
        read_access=(read_access or None),
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "read": {"required": ["skill_id"], "optional": [], "desc": "return a sovereign soul scroll by skill_id"},
        "append": {"required": ["skill_id", "content"], "optional": ["section_separator"], "desc": "append a content section to the scroll"},
        "rewrite": {"required": ["skill_id", "content", "reason"], "optional": ["name", "description", "kind", "tags"], "desc": "replace the scroll's content (reason required)"},
        "create": {"required": ["skill_id", "content", "sovereign_owner", "name"], "optional": ["description", "kind", "tags"], "desc": "create a new soul scroll (sovereign_owner required)"},
    },
)
def ai_soul(
    skill_id: str,
    mode: str,
    content: str = "",
    reason: str = "",
    sovereign_owner: str = "",
    name: str = "",
    description: str = "",
    kind: str = "",
    tags: str = "",
    section_separator: str = "\n\n---\n\n",
) -> dict:
    """Agent soul/personality CRUD — mode ∈ {read, append, rewrite, create}.
    `read` returns the soul record; `append`/`rewrite`/`create` write and
    require a write grant for that soul. Reads respect the privacy floor
    (souls are agent-personal, not operator-visible by default per RFC 003).
    """
    return _delegate(
        "ai_soul",
        skill_id=skill_id,
        mode=mode,
        content=content,
        reason=reason,
        sovereign_owner=sovereign_owner,
        name=name,
        description=description,
        kind=kind,
        tags=tags,
        section_separator=section_separator,
    )


# ai_todo REMOVED (#83 hard merge, Emperor ruling 2026-07-18 — no-alias law):
# the todo surface lives on as ai_task's todo modes (add|list|remove, and
# update with scope='task'|'session'); the impl stays in
# server_todo_backlog_tools.build_todo_handler, dispatched by ai_task. No
# alias, no shim — an 'ai_todo' registration reappearing here is a
# resurrection (pinned by tests/security/test_ai_task_todo_merge.py).


@tool(
    # Empire 2026-06-28: ai_deploy is the operator's OWN tool — remotely triggerable but
    # ONLY by a super_admin principal (his account), never anybody else regardless of
    # role, and ABSENT (hidden, not merely scope-blocked) from every non-super_admin
    # catalog. surface=BOTH keeps it advertised on the gate (super_admin discovers +
    # invokes it remotely); the super_admin-only catalog HIDING is enforced in
    # outer_gate_catalog.resolve() (SUPER_ADMIN_ONLY_TOOLS → in_catalog=False unless the
    # principal is super_admin). The super_admin invoke authority is enforced in
    # outer_gate.execute() (name=="ai_deploy" → super_admin + AIDOCS_PRIVATE + ref
    # allowlist). destructiveHint=False per the edit-tool doctrine — the registry's
    # TWO_PHASE confirm-token owns confirmation, not the host hint.
    # CONTRACT MOVED (Empire directive 2026-07-06, 727997d7): ai_deploy is
    # NONEXISTENT on the local stdio MCP surface (serve entry passes
    # expose_deploy=False) — it exists ONLY on the VPS gate/webmcp path, so
    # surface=GATE_ONLY is the truthful declaration. The gate's internal
    # execution servers opt in via expose_deploy=True; no local agent can
    # see or call it.
    surface=GATE_ONLY,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    # Consumable two-phase confirm: the first call (no confirm_token) returns
    # _error=confirm_required + the phrase; the second call with a matching token executes
    # ONCE. The token is bound (canonical_invocation._bind_hash) to operator + project + session
    # + tool + normalized_args (which include ref AND reason) + intent, so a confirm minted for
    # one (ref, reason, session) can never be replayed for another.
    confirm=TWO_PHASE,
    phrase="confirm-deploy {ref}",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
        "title": "AI Deploy (super_admin)",
    },
)
def ai_deploy(ref: str = "main", reason: str = "", confirm_token: str = "") -> dict:
    """Trigger a remote AIDOCS crown deploy of `ref` (default 'main'). HIGHEST-authority
    tool: super_admin ONLY, a SELECTED SESSION, the bound project must be AIDOCS_PRIVATE,
    `ref` must be allowlisted, a non-empty `reason` is REQUIRED, and a CONSUMABLE confirm
    (bound to operator+project+session+ref+reason) must be satisfied — all enforced at the
    gate (OuterGate.execute). Enqueues a request for the root deploy-runner daemon and returns
    a deploy_id; poll progress with ai_deploy_output. ai_deploy never signs and never holds a key.
    """
    return _delegate("ai_deploy", ref=ref, reason=reason, confirm_token=confirm_token)


@tool(
    # CONTRACT MOVED (Empire directive 2026-07-06, 727997d7): hidden from the
    # local stdio surface with ai_deploy (expose_deploy=False at the serve
    # entry); served ONLY by the gate's read-executor (expose_deploy=True).
    # GATE_ONLY is the truthful declaration.
    surface=GATE_ONLY,
    cls=READ,
    tier=R,
    scope="tier_r_invoke",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "AI Deploy Output",
    },
)
def ai_deploy_output(deploy_id: str = "") -> dict:
    """Read the status + log of a deploy enqueued by ai_deploy. Returns status
    (queued|running|ok|failed) plus the daemon log for `deploy_id`.
    """
    return _delegate("ai_deploy_output", deploy_id=deploy_id)


# ai_review removed (120% clause B): folded into ai_lane(action='review').
# Impl stays register_impl-bound for the consolidator's _delegate; no alias.


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "ask": {"required": ["question"], "optional": ["lane_id", "wait", "timeout", "category", "requested_path", "session_id"]},
        "answer": {"required": ["message_id", "response"], "optional": []},
        "check": {"required": ["message_id"], "optional": []},
        "pending": {"required": [], "optional": ["session_id"]},
        "history": {"required": [], "optional": ["lane_id", "limit"]},
    },
)
def ai_qa(
    mode: str,
    question: str = "",
    message_id: str = "",
    response: str = "",
    lane_id: str = "",
    wait: bool = False,
    timeout: int = 120,
    category: str = "question",
    requested_path: str = "",
    session_id: str = "",
    limit: int = 50,
) -> dict:
    """Unified Q&A channel — mode='ask'|'answer'|'check'|'pending'|
    'history' (the impl's real accepted set). ask: agent asks the
    conductor/operator (wait=True blocks until answered or timeout);
    answer: conductor answers a message_id; check: poll whether a
    question was answered; pending: list pending questions; history:
    per-lane or all message history.
    """
    return _delegate(
        "ai_qa",
        mode=mode,
        question=question,
        message_id=message_id,
        response=response,
        lane_id=lane_id,
        wait=wait,
        timeout=timeout,
        category=category,
        requested_path=requested_path,
        session_id=session_id,
        limit=limit,
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "list": {"required": [], "optional": ["session_id"]},
        "fixed": {"required": ["signature"], "optional": ["proof_command", "proof_log", "session_id"]},
        "preserve_baseline": {"required": ["signature", "baseline_sha"], "optional": ["proof_command", "proof_log", "session_id"]},
        "quarantine": {"required": ["signature", "followup_ref"], "optional": ["proof_command", "proof_log", "session_id"]},
        "escalate": {"required": ["signature", "operator_alert"], "optional": ["session_id"]},
        "waiver": {"required": ["signature", "operator", "reason"], "optional": ["session_id"]},
        "autoclear": {"required": [], "optional": ["proof_command", "session_id"]},
    },
)
def ai_failures(
    mode: str = "list",
    signature: str = "",
    proof_command: str = "",
    proof_log: str = "",
    baseline_sha: str = "",
    followup_ref: str = "",
    operator_alert: str = "",
    operator: str = "",
    reason: str = "",
    session_id: str = "",
) -> dict:
    """Failure-stewardship disposition surface — agent-callable CONSUMER
    of the failure ledger (the Stop hook is the producer). Without this an
    untriaged blocker the agent already fixed could wedge the turn-seal.

    mode='list' (default) — failures THIS session owns + the full ledger.
    mode='fixed'/'preserve_baseline'/'quarantine'/'escalate'/'waiver' —
    claim a disposition with proof. mode='autoclear' — mark this session's
    blockers FIXED on an observed green run. signature accepts a short
    prefix. Session-scoped: cannot dispose another session's duty.
    """
    return _delegate(
        "ai_failures",
        mode=mode,
        signature=signature,
        proof_command=proof_command,
        proof_log=proof_log,
        baseline_sha=baseline_sha,
        followup_ref=followup_ref,
        operator_alert=operator_alert,
        operator=operator,
        reason=reason,
        session_id=session_id,
    )


# ai_guidance removed (120% clause B): folded into ai_lane(action='guide').
# ai_events removed (120% clause B): folded into ai_lane(action='events').
# Impls stay register_impl-bound for the consolidator's _delegate; no aliases.


@tool(
    surface=BOTH,
    cls=IMPORT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def related_project_register(
    name: str,
    path: str,
    reason: str = "",
    notes: str = "",
) -> dict:
    """Register a related project so cross-project tools can find it
    by name. Mutates the related-projects registry; the read side
    (related_project_list / _code_search / etc.) already ships in
    the READ batch.
    """
    return _delegate("related_project_register", name=name, path=path, reason=reason, notes=notes)


# ── LOCAL-ONLY internals ────────────────────────────────────────────
# Tools registered for the local stdio agent but explicitly REFUSED
# on the outer gate. Each entry is non-negotiable for the gate
# surface — see per-tool rationale.

_LOCAL_INTERNAL_ANN = {
    "destructiveHint": False,
    "openWorldHint": False,
    "title": "Internal (local-only)",
}


@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations=_LOCAL_INTERNAL_ANN,
    modes={
        "list": {"required": [], "optional": ["lang"], "desc": "all gate messages for one lang"},
        "get": {"required": ["key"], "optional": ["lang"], "desc": "one message body, with en fallback"},
        "upsert": {"required": ["key", "body"], "optional": ["lang", "source"], "desc": "delete + insert one message"},
        "delete": {"required": ["key"], "optional": ["lang"], "desc": "drop one message"},
    },
)
def ai_gate_msg(
    mode: str,
    key: str = "",
    body: str = "",
    lang: str = "en",
    source: str = "operator",
) -> dict:
    """Empire gate-message CRUD — backs the dashboard editor
    (list/get/upsert/delete of the sqlite gate-message strings).
    LOCAL-ONLY: these strings are operator decisions surfaced to
    every session's gate refusals — a remote tenant rewriting them
    would let one session alter what every other session is told.

    2026-07-15 (SSOT war): this declaration previously described a
    different, never-shipped 'gate↔gate message bus' with a
    (channel, message) signature — a name-collision stub that made
    the registry lie about the real runtime tool. Now mirrors the
    actual dispatcher registered in mcp_server.py.
    """
    return _delegate("ai_gate_msg", mode=mode, key=key, body=body, lang=lang, source=source)


# ── SSOT-04 phase C (2026-07-15): stdio adapter fold-in ──────────────
# Five local-only tools that lived as bare @server.tool registrations
# with reasoned waiver entries ("catalog decision pending" /
# "migrate, then drop this entry" — tool_surface_waivers). Decision:
# LOCAL_ONLY — they are adapter/runtime surfaces with no remote call
# path; declaring them documents the status quo and retires the
# waivers. Flipping one to BOTH later is a one-field change here.


@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations=_LOCAL_INTERNAL_ANN,
)
def ai_backends() -> list:
    """List available worker backends (Claude SDK, OpenAI Codex, OpenCode)
    on this system."""
    return _delegate("ai_backends")


@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations=_LOCAL_INTERNAL_ANN,
)
def ai_models(backend: str) -> dict:
    """List available model slugs for a worker backend
    (opencode → live shell-out; claude/codex → known IDs)."""
    return _delegate("ai_models", backend=backend)


@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations=_LOCAL_INTERNAL_ANN,
)
def ai_deslop_apply(
    operation: str,
    reason: str,
    source_path: str = "",
    target_path: str = "",
    symbol: str = "",
    kind: str | None = None,
    start_line: int = 0,
    end_line: int = 0,
    target_position: str = "append",
    target_line: int | None = None,
    remove_from_source: bool = False,
    dry_run: bool = True,
) -> dict:
    """Guarded APPLY path for deslop refactors — the Tier-M counterpart of
    the read-only ai_slop scanner (extract_block | extract_symbol |
    refactor_extract). Local-only: not gate-invokable at all
    (test_ai_deslop_split / test_outer_gate_skeleton)."""
    return _delegate(
        "ai_deslop_apply",
        operation=operation,
        reason=reason,
        source_path=source_path,
        target_path=target_path,
        symbol=symbol,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
        target_position=target_position,
        target_line=target_line,
        remove_from_source=remove_from_source,
        dry_run=dry_run,
    )


@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations=_LOCAL_INTERNAL_ANN,
)
def planning_step_mark(path: str, line_number: int, status: str = "done") -> dict:
    """Toggle a checkbox in a planning doc. Status: done, open, skip,
    in_progress, blocked."""
    return _delegate(
        "planning_step_mark", path=path, line_number=line_number, status=status
    )


@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations=_LOCAL_INTERNAL_ANN,
)
def workflow_action_satisfy(action_id: str, evidence: str) -> dict:
    """Mark a workflow action as completed with evidence (audit-logged)."""
    return _delegate("workflow_action_satisfy", action_id=action_id, evidence=evidence)


# ── SSOT-04 phase C (cont.): session_start — the ONE non-ai_* name that is
# a HARD external contract, not debt. Claude Code's bootstrap probe fires the
# literal string `session_start` on every /mcp reconnect
# (capability_definitions_get → session_start); a 404 makes CC mark the MCP
# unhealthy and drop managed mode. The name is dictated by the host client and
# can NEVER become ai_*. Declared here (LOCAL_ONLY) purely so it stops being an
# off-registry straggler — the runtime tool stays the hand-written
# @server.tool(meta={"anthropic/alwaysLoad": True}) in server_session_tools.py,
# which this contract row does not replace (same additive pattern as the
# adapters above). This row carries the contract; the meta stays on the server
# registration.
@tool(
    surface=LOCAL_ONLY,
    cls="internal",
    tier=L,
    scope="",
    annotations={
        "destructiveHint": False,
        "openWorldHint": False,
        "title": "Session Start (Compat Alias)",
    },
)
def session_start(session_id: str | None = None) -> dict:
    """Compat alias for Claude Code's bootstrap probe. If managed mode is
    active, returns current state (idempotent); if inactive, auto-activates
    against the most recent active session. Full bootstrap =
    project_bootstrap_or_resume; lightweight bind = ai_session(connect).
    Exists ONLY because CC's hardcoded probe calls `session_start` on every
    /mcp reconnect — the name is a host-protocol contract, never ai_*."""
    return _delegate("session_start", session_id=session_id)


@tool(surface=LOCAL_ONLY, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def ai_concurrency_reset(reason: str = "") -> dict:
    """Clear live locks. A remote actor clearing locks while another
    operation is in flight is the classic invariant-break footgun.
    """
    return _delegate("ai_concurrency_reset", reason=reason)


# SSOT-05 #386/#288: surface=HIDDEN — host/bootstrap cache plumbing, not an
# agent surface. Registered + callable for the runtime; off every tools/list.
@tool(surface=HIDDEN, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def bump_agent_memory_epoch(host_kind: str, host_session_id: str) -> dict:
    """Cache-bust primitive: bump the compaction count for
    (host_kind, host_session_id). Remote callers shouldn't be
    invalidating cross-tenant cache lines on a shared gate host.

    2026-07-10: signature aligned to the real handler
    (server_session_tools.bump_agent_memory_epoch) — the handler is the
    source of truth; both params are required there. Pinned by
    test_catalog_schema_runtime_parity §5.
    """
    return _delegate(
        "bump_agent_memory_epoch",
        host_kind=host_kind,
        host_session_id=host_session_id,
    )


@tool(surface=LOCAL_ONLY, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def ai_preflight(tool: str = "", args: dict = None) -> dict:
    """Internal pre-call probe used by the gate to decide whether
    a tool call would succeed. Not an end-user surface.
    """
    return _delegate("ai_preflight", tool=tool, args=args)


@tool(surface=LOCAL_ONLY, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def ai_resolve_backend() -> dict:
    """Internal helper the gate already calls when resolving the
    active backend. Exposing it remotely is noise + a redundant
    code path.
    """
    return _delegate("ai_resolve_backend")


@tool(surface=LOCAL_ONLY, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def ai_resolve_scope(tool: str = "") -> dict:
    """Internal helper for scope resolution — same reasoning as
    ai_resolve_backend; the gate consults this internally.
    """
    return _delegate("ai_resolve_scope", tool=tool)


# SSOT-05 #386/#288: surface=HIDDEN — the gate runs verification at
# admit-time; the primitive is runtime plumbing, not an agent surface.
@tool(surface=HIDDEN, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def verification_gate(
    session_id: str,
    lane_id: str = None,
    verification_evidence: dict = None,
    timeout: int = None,
) -> dict:
    """Internal contract check: runtime-owned verification status for a
    session or lane. The gate runs verification at admit-time on every
    call; exposing the underlying primitive as a tool would let remote
    callers spam it standalone.

    2026-07-10: signature aligned to the real handler
    (server_plan_task_tools.verification_gate) — the prior declared
    (claim, evidence) params never existed on the handler. Pinned by
    test_catalog_schema_runtime_parity §5.
    """
    return _delegate(
        "verification_gate",
        session_id=session_id,
        lane_id=lane_id,
        verification_evidence=verification_evidence,
        timeout=timeout,
    )


# SSOT-05 #386/#288: skill infra probes surface=HIDDEN — ai_skill is the
# ONLY agent-facing skill surface; registry/scan/state stay runtime-callable.
@tool(surface=HIDDEN, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def skill_registry_get(skill_id: str = "") -> dict:
    """Skill subsystem registry introspection. ai_skill (invocation)
    is the agent surface; the registry/scan/state probes are infra.
    """
    return _delegate("skill_registry_get", skill_id=skill_id)


@tool(surface=HIDDEN, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def skill_scan() -> dict:
    """Re-scan the skills directory. Infra plumbing — agent doesn't
    add/remove skills from a remote tenant.
    """
    return _delegate("skill_scan")


@tool(surface=HIDDEN, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def skill_trigger_state_get(skill_id: str = "") -> dict:
    """Skill trigger state probe. Internal — exposed via ai_skill
    side effects, not standalone.
    """
    return _delegate("skill_trigger_state_get", skill_id=skill_id)


# Empire 2026-06-20: config_set is NOT a registry @tool. It's a CONTROL-PLANE op
# (handle_project_tool op #3) like config_view/session_delete — gate-advertised via
# PROJECT_TOOL_SPECS (selector) and authority-gated there (org OWNER/ADMIN/super_admin
# + two-phase confirm-config-set). Being a registry @tool(surface=local_only) made
# classify() return CLASS_INTERNAL, hiding it from the gate catalog so op #3 was
# unreachable. The local stdio host-config writer remains its own @server.tool
# (server_project_admin_tools) — full-trust local — independent of the gate org-config op.


@tool(surface=LOCAL_ONLY, cls="internal", tier=L, scope="", annotations=_LOCAL_INTERNAL_ANN)
def ai_notifications_clear(target: str = "") -> dict:
    """Clear local-host notifications. Operator UX on the gate host,
    not for remote tenants.
    """
    return _delegate("ai_notifications_clear", target=target)


# Empire 2026-06-20: ai_backlog promoted to the gate.
# 2026-06-21: the prior registry wrapper exposed ai_backlog as a PARAMETERLESS
# cls=READ listing, but the real @server.tool impl (server_todo_backlog_tools.py:425)
# is a multi-mode add|list|get|update|remove consolidator — exactly like its then-
# sibling ai_todo (since #83-merged into ai_task's todo modes). Exposed read-only,
# its write modes were either unreachable or routed as a "read", a surface-lie.
# Reclassified to the SAME mechanism the todo surface used:
# surface=BOTH, cls=EDIT, tier=M, scope=tier_m_edit. The registry EDIT path
# (outer_gate_edit._extended_edit_allowlist → OuterGate._registry_invoke_edit) gates
# it with tier_m_edit scope + exec-root binding + two-phase confirm, and the impl's
# own require_active_task (#82) + _audit_backlog cascade guards every write — the same
# guarding the todo surface relies on. The manifest keeps ai_backlog Tier-M/binding=none →
# never manifest-remote-eligible; the EDIT registry path is what makes it gate-callable.
#
# 2026-06-21 (per-mode gate): ai_backlog is now gate-callable with PER-MODE
# authorization enforced in OuterGate.execute() (the canonical admission surface):
#   READ  modes (list, get)         → tier_r_invoke suffices (NOT tier_m_edit).
#   WRITE modes (add, update, remove)→ tier_m_edit + is_org_admin(GATE principal).
# The admin authority is the GATE-resolved OAuth principal via
# outer_gate_project_acl.is_org_admin — NEVER identity_resolver's super_admin
# fallback. cls=EDIT keeps the registry EDIT dispatch (exec-root binding + audit);
# the per-mode carve-out relaxes reads off tier_m_edit and adds the write admin gate.
@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "add": {"required": ["content"], "optional": ["priority", "tags"], "desc": "add a durable project backlog item"},
        "list": {"required": [], "optional": ["status", "tag_filter", "tags", "include_removed", "include_merged", "limit", "include_tags", "include_preview"], "desc": "list backlog items (filters optional; merged hidden unless include_merged)"},
        "get": {"required": ["id"], "optional": ["body_offset", "body_limit"], "desc": "full backlog item body by id (paginated via body_offset/body_limit)"},
        "update": {"required": ["id"], "optional": ["content", "status", "priority", "tags", "reason", "allow_clear"], "desc": "patch a backlog item by id (non-destructive #399; clearing the body needs allow_clear=True)"},
        "remove": {"required": ["id"], "optional": ["reason"], "desc": "soft-remove a backlog item by id"},
        "merge": {"required": ["ids"], "optional": ["umbrella_id"], "desc": "merge >=2 open items into one umbrella (#450); absorbed rows become status='merged' + merged_into, reversible via update(status='open')"},
    },
)
def ai_backlog(
    mode: str,
    content: str = "",
    id: int = 0,
    status: str = "",
    priority: str = "",
    tags: list = None,
    tag_filter: str = "",
    include_removed: bool = False,
    include_merged: bool = False,
    limit: int = 0,
    reason: str = "",
    include_tags: bool = True,
    include_preview: bool = False,
    body_offset: int = 0,
    body_limit: int = 0,
    ids: list = None,
    umbrella_id: int = 0,
    allow_clear: bool = False,
    format: str = "",
) -> dict:
    """Project-owned durable backlog CRUD — mode='add'|'list'|'get'|'update'|
    'remove'|'merge'. `add` requires an active task (#82) and is audited;
    `list`/`get` are read surfaces. `merge` folds open items into an umbrella
    (#450, reversible). Same multi-mode read+write consolidator pattern as
    ai_task's todo modes (#83).
    list accepts format='table'|'csv' (#20, opt-in; default structured JSON).
    """
    return _delegate(
        "ai_backlog",
        mode=mode,
        content=content,
        id=id,
        status=status,
        priority=priority,
        tags=tags,
        tag_filter=tag_filter,
        include_removed=include_removed,
        include_merged=include_merged,
        limit=limit,
        reason=reason,
        include_tags=include_tags,
        include_preview=include_preview,
        body_offset=body_offset,
        body_limit=body_limit,
        ids=ids,
        umbrella_id=umbrella_id,
        allow_clear=allow_clear,
        format=format,
    )


# ── ai_issues: immutable issue filing (#449 v1, WAR F 2026-07-18) ──
# The refusal footer's NON-ADMIN leg points here (tool_gate_service.
# false_positive_affordance): callers who cannot pull the ai_backlog
# lever file ONE immutable issue instead — a write-once
# .MEMORY/issues/*.json git-committed (pathspec-only, current branch,
# NO push) so the filing is immutable-in-history (#440/#441 spirit).
# Registered exactly like its sibling ai_backlog: surface=BOTH,
# cls=EDIT, tier=M, scope=tier_m_edit — the registry EDIT path
# (outer_gate_edit._extended_edit_allowlist auto-includes BOTH+EDIT
# entries) gates gate-side calls with tier_m_edit + exec-root binding.
# confirm=NO_CONFIRM at the registry layer because the IMPL owns the
# two-phase intent gate: mode='file' demands the literal param
# confirm='file-issue' and returns the phrase-naming prompt without it
# (mirrors ai_delete's two-phase doctrine; keeping it in the impl means
# the gate and stdio surfaces share ONE confirm seam). The remote PR
# push (UPS user-intent leg) is v2 — not registered, not stubbed.
@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "file": {"required": ["content"], "optional": ["tags", "confirm"], "desc": "file ONE immutable issue (write-once + git-committed); demands the literal confirm='file-issue'"},
        "list": {"required": [], "optional": [], "desc": "terse issue inventory [{issue_id, snippet, created_at, actor}]"},
    },
)
def ai_issues(
    mode: str,
    content: str = "",
    tags: list = None,
    confirm: str = "",
) -> dict:
    """Immutable issue filing — mode='file'|'list'. `file` writes ONE
    write-once .MEMORY/issues/ record and git-commits ONLY that file
    (no push — v1); it demands the literal confirm='file-issue' and
    otherwise returns the two-phase prompt without writing. `list` is
    a terse inventory. No active task required — this is the
    refusal-report channel.
    """
    return _delegate(
        "ai_issues",
        mode=mode,
        content=content,
        tags=tags,
        confirm=confirm,
    )


# ── READ ─────────────────────────────────────────────────────────────
# All READ-class entries auto-extend `outer_gate_executor`'s
# `READ_EXEC_ALLOWLIST` at module load (see `read_exec_names()`
# above), so adding a tool here puts it on the gate's read surface
# without touching any other file. Annotations are uniform for the
# class (read-only, idempotent, closed-world); the gate's existing
# read-pipeline + binding + index-staleness gates run unchanged.

_READ_ANN = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_lines(
    path: str,
    start_line: int = 1,
    count: int = 30,
    known_exact_path: bool = False,
    symbol: str | None = None,
) -> dict:
    """Fallback line-range read of a file in the selected project.

    Prefer the indexed reads (`ai_get_symbol_snippet`, `ai_bundle`)
    when possible; this is the escape hatch for non-indexed text.
    `symbol=<name>` reads the symbol's full closure-span instead of
    start_line/count (whole-symbol shorthand, raised ceiling).
    """
    return _delegate(
        "ai_get_lines",
        path=path,
        start_line=start_line,
        count=count,
        known_exact_path=known_exact_path,
        symbol=symbol,
    )


@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    aliases=("audit", "agents", "conductor"),
)
def ai_agents(
    include_dead: bool = False,
    role: str = "",
    session_id: str = "",
) -> dict:
    """Role-based audit of the CONNECTED agents on the selected project --
    the actual interactive agents (conductors), keyed by host_session_id /
    agent identity, NOT lane subagents. Each agent shows its messagerie role
    (conductor / co_conductor / king), bound work session, liveness (its MCP
    process), agent_memory_epoch identity, and its spawned lane workers nested
    underneath. Filters: role / session_id narrow the list; include_dead also
    lists agents whose process has exited (for audit). For the lane-worker
    roster use ai_lane(action='agents'). No file changes.
    """
    return _delegate(
        "ai_agents",
        include_dead=include_dead,
        role=role,
        session_id=session_id,
    )


# ── Empire 2026-06-20: ai_find + ai_run folded into the registry ──────────
# Both already have @server.tool impls (server_code_tools / server_run_tools);
# these @tool stubs add their REGISTRY metadata (advertisement/classification)
# so the tool_interface is the single source — previously ai_find/ai_run lived
# only in outer_gate_catalog's hand-rolled allowlists. The @server.tool impls
# remain the handlers (same coexistence as ai_get_lines); the stub bodies just
# delegate. Signatures mirror the impls exactly so the schema is unchanged.
@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Code Find",
    },
    modes={
        "symbols": {"required": ["query"], "optional": ["kind", "role", "modified_since", "include_tests", "limit"], "desc": "query=symbol name or concept → ranked definitions (members fold under their container; line_end spans)"},
        "dependencies": {"required": ["query"], "optional": ["limit"], "desc": "query=FILE PATH → its import/dependency edges (aliases: deps, imports)"},
        "references": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=SYMBOL NAME → every usage/caller site (aliases: callers, usages)"},
        "text": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=literal text → matching lines across indexed files"},
        "regex": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=regular expression → matching lines"},
        "string": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=exact string → matching lines"},
        "routes": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept or controller → API endpoints, page routes"},
        "entrypoints": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept → bootstrap/main/provider entry symbols"},
        "api_consumers": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=endpoint or URL fragment → pages/scripts that call it"},
        "frontend_symbols": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=name → components, hooks, providers"},
        "data_structures": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept → classes/records/enums with members"},
        "initializers": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=FILE PATH (empty = all files) → init/DOMContentLoaded handlers"},
        "mutations": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=domain concept → create/update/delete flows"},
        "validation": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept → validators, required fields, validation logic"},
        "async": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept (empty = all) → async boundaries, Task patterns"},
        "policy": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept → authorization/RBAC/permission checks"},
        "touchpoints": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept → UI↔backend connection points"},
        "clusters": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=domain concept → cross-layer file grouping"},
        "transitions": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=concept → migration seams, adapters, compat layers"},
        "factories": {"required": ["query"], "optional": ["limit", "include_tests"], "desc": "query=name or concept → Create*/factory/setup helpers (include_tests auto-on)"},
    },
)
def ai_find(
    query: str,
    mode: str = "symbols",
    kind: str | None = None,
    role: str | None = None,
    modified_since: str | None = None,
    include_tests: bool | None = None,
    limit: int = 50,
    path: str | None = None,
    strict: bool = False,
    fuzz: bool = False,
    timeout: int | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Unified code find/search — the default code-discovery tool. Modes:
    symbols, references, dependencies, routes, entrypoints, api_consumers,
    frontend_symbols, data_structures, initializers, mutations, validation,
    async, policy, touchpoints, clusters, transitions, factories, text, regex,
    string. Prefer the structural modes over text/regex for code. `path`
    scopes text/regex/string to a path prefix, exact file, or glob.
    mode=symbols: strict=True returns exact+strong tier hits only;
    fuzz=True forces the permissive flood (all tiers, no collapse).
    """
    return _delegate(
        "ai_find",
        query=query,
        mode=mode,
        kind=kind,
        role=role,
        modified_since=modified_since,
        include_tests=include_tests,
        limit=limit,
        path=path,
        strict=strict,
        fuzz=fuzz,
        timeout=timeout,
    )


# Empire 2026-06-20: ai_run is intentionally NOT a registry @tool. It's the WebMCP
# shell protected by the CODE layer (bash_policy destructive-floor + heuristic judge
# + T0 confirm). As a registry stub it can't satisfy BOTH the run-annotation override
# (gate forces destructiveHint=False) AND the "remote shell needs its own seal"
# doctrine. It stays facade-advertised via RUN_ALLOWLIST; its real unlock is the
# EXECUTION layer — Tier-M enablement gated on rbac/tenancy (Phase 1 three-phase
# audit is the prerequisite). That assault is separate from the registry.


# Empire 2026-06-20: config_get folded into the registry. Doctrine said it "stays
# gate-exposed" but it was a raw @server.tool outside the registry — not advertised
# anywhere. READ-class, gate-exposed; read-only scope-cascade config read.
@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Get Config Setting",
    },
)
def config_get(key: str, session_id: str = "", default: Any = None) -> dict[str, Any]:
    """Read a setting from the AIDOCS SQLite config store with the full scope
    cascade (session > project > global > code default). Prefer this over reading
    aidocs.sqlite3 directly — it respects the cascade + dotted keys (bash.deny etc.).
    """
    return _delegate("config_get", key=key, session_id=session_id, default=default)


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_modules(kind: str = "") -> dict:
    """List detected project modules (workspaces, subprojects)."""
    return _delegate("ai_get_modules", kind=(kind or None))


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_get_module_files(
    module_path: str,
    modified_since: str = "",
    limit: int = 200,
) -> dict:
    """List indexed source files in a specific module. `modified_since`
    accepts 'today', '1h', '24h', '7d', or ISO datetime.
    """
    return _delegate(
        "ai_get_module_files",
        module_path=module_path,
        modified_since=(modified_since or None),
        limit=limit,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_index_status() -> dict:
    """Current derived code index status for the bound project."""
    return _delegate("ai_index_status")


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_recall(
    query: str,
    limit: int = 5,
    include_semantic_only: bool = True,
    smoke: bool = False,
) -> dict:
    """Unified recall over code + palace + KG, clustered by unit
    identity. Returns ranked clusters with provenance + snippets.
    """
    return _delegate(
        "ai_recall",
        query=query,
        limit=limit,
        include_semantic_only=include_semantic_only,
        smoke=smoke,
    )


# Memory-war unify (2026-07-16, operator): palace is an internal projection —
# agent retrieval is unified under ai_recall / ai_memory(search) (see
# unified_recall.py). GATE_ONLY + outer_gate_catalog._NOT_AGENT_ADVERTISED
# drop ai_palace_search/ai_palace_status from BOTH agent surfaces; the
# dashboard keeps its tools/call route (hidden, not dead).
@tool(surface=GATE_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_palace_search(
    query: str,
    wing: str = "",
    room: str = "",
    limit: int = 5,
    max_distance: float = 1.5,
) -> dict:
    """Hybrid BM25 + vector + closet-first search over the project
    palace. Returns metadata + snippets — NOT full verbatim content.
    """
    return _delegate(
        "ai_palace_search",
        query=query,
        wing=wing,
        room=room,
        limit=limit,
        max_distance=max_distance,
    )


@tool(surface=GATE_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_palace_status() -> dict:
    """Palace health probe: drawer/wing/room counts, KG counts,
    vector-disabled state, palace_disabled / kill_switch flags.
    """
    return _delegate("ai_palace_status")


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_palace_diary_read(
    agent_name: str,
    last_n: int = 10,
    wing: str = "",
    read_across_agents: bool = False,
) -> dict:
    """Read recent diary entries for `agent_name`. Diaries are
    agent-isolated by default; cross-agent reads require
    `read_across_agents=True`.
    """
    return _delegate(
        "ai_palace_diary_read",
        agent_name=agent_name,
        last_n=last_n,
        wing=wing,
        read_across_agents=read_across_agents,
    )


@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "text": {"required": ["path"], "optional": ["pages", "known_exact_path"], "desc": "extract text (pages: '1-5,8' or '' for all, max 50)"},
        "text_and_tables": {"required": ["path"], "optional": ["pages", "known_exact_path"], "desc": "extract text AND tables"},
    },
)
def ai_read_pdf(
    path: str,
    pages: str = "",
    mode: str = "text",
    known_exact_path: bool = False,
) -> dict:
    """Extract text / tables from a PDF. `mode` ∈ {'text',
    'text_and_tables'}. `pages` like '1-5,8' (max 50). Requires
    the 'office' extra.
    """
    return _delegate(
        "ai_read_pdf",
        path=path,
        pages=pages,
        mode=mode,
        known_exact_path=known_exact_path,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_read_docx(
    path: str,
    sections: str = "",
    known_exact_path: bool = False,
) -> dict:
    """Extract paragraphs + tables from a .docx in document order.
    `sections` like '1-3' limits to the first N Heading-1 sections.
    Requires the 'office' extra.
    """
    return _delegate(
        "ai_read_docx",
        path=path,
        sections=sections,
        known_exact_path=known_exact_path,
    )


@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "outline": {"required": ["path"], "optional": ["known_exact_path"], "desc": "sheets + headers overview"},
        "sheet": {"required": ["path", "sheet"], "optional": ["query", "known_exact_path"], "desc": "rows of one sheet (max 500)"},
        "formulas": {"required": ["path", "sheet"], "optional": ["known_exact_path"], "desc": "formulas of one sheet"},
        "trace": {"required": ["path", "sheet", "cell"], "optional": ["known_exact_path"], "desc": "parse the formula at sheet!cell"},
    },
)
def ai_read_excel(
    path: str,
    mode: str = "outline",
    sheet: str = "",
    cell: str = "",
    query: str = "",
    known_exact_path: bool = False,
) -> dict:
    """Inspect an Excel workbook (read-only). Modes: 'outline' (sheets
    + headers), 'sheet' (rows, max 500), 'formulas', 'trace'.
    """
    return _delegate(
        "ai_read_excel",
        path=path,
        mode=mode,
        sheet=sheet,
        cell=cell,
        query=query,
        known_exact_path=known_exact_path,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_read_jsonl(
    path: str,
    content_contains: str = "",
    offset: int = 0,
    limit: int = 50,
    known_exact_path: bool = False,
) -> dict:
    """Stream a JSONL file with field-level filter + projection."""
    return _delegate(
        "ai_read_jsonl",
        path=path,
        content_contains=content_contains,
        offset=offset,
        limit=limit,
        known_exact_path=known_exact_path,
    )


@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "tables": {"required": ["path"], "optional": ["known_exact_path"], "desc": "table names + row counts"},
        "schema": {"required": ["path"], "optional": ["table", "known_exact_path"], "desc": "CREATE statements (optionally one table)"},
        "query": {"required": ["path", "query"], "optional": ["limit", "known_exact_path"], "desc": "SELECT-only query, capped at limit"},
    },
)
def ai_read_sqlite(
    path: str,
    mode: str = "tables",
    table: str = "",
    query: str = "",
    limit: int = 100,
    known_exact_path: bool = False,
) -> dict:
    """Inspect or query a SQLite file (read-only). Modes: 'tables',
    'schema', 'query' (SELECT-only, capped at `limit`).
    """
    return _delegate(
        "ai_read_sqlite",
        path=path,
        mode=mode,
        table=table,
        query=query,
        limit=limit,
        known_exact_path=known_exact_path,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def ai_read_raw(
    path: str,
    offset_bytes: int = 0,
    encoding: str = "utf-8",
    known_exact_path: bool = False,
) -> dict:
    """Read a byte range of any file as text. Soft cap 512 KB per
    call; hard cap 8 MB. Paginate via `offset_bytes`.
    """
    return _delegate(
        "ai_read_raw",
        path=path,
        offset_bytes=offset_bytes,
        encoding=encoding,
        known_exact_path=known_exact_path,
    )


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def audit_events_for_task(task_id: str, limit: int = 200) -> dict:
    """Return every execution_event stamped with this task_id.
    `task_id` is minted by `task_begin` (SHA-based); every mutating
    tool call carries it, so 'what did this task actually do?' is
    one query.
    """
    return _delegate("audit_events_for_task", task_id=task_id, limit=limit)


@tool(
    surface=LOCAL_ONLY,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "tail": {"required": [], "optional": ["n"], "desc": "newest-first n ledger rows"},
        "list": {"required": [], "optional": ["n"], "desc": "oldest-first n ledger rows"},
        "by-callsite": {"required": ["key"], "optional": ["n"], "desc": "rows for one callsite fingerprint, e.g. 'aidocs_service.py::spawn::subprocess.Popen'"},
        "by-session": {"required": ["session_id"], "optional": ["n"], "desc": "rows stamped with one session_id"},
        "stats": {"required": [], "optional": [], "desc": "totals + open/reaped + per-callsite / per-reason counts"},
        "census": {"required": [], "optional": [], "desc": "the COMPLETE spawn map (#335): every static callsite -> fingerprint -> reason -> window posture -> audited/registered, joined with runtime ledger spawn counts"},
    },
)
def ai_process_audit(
    mode: str = "tail",
    n: int = 20,
    key: str = "",
    session_id: str = "",
) -> dict:
    """Read-only runtime process-audit ledger (#335): what subprocesses
    this server spawned, why, and when — recorded by audited_popen at
    spawn/reap time. Modes: 'tail' (newest-first n) / 'list'
    (oldest-first n) / 'by-callsite' (key = '::'-joined callsite
    fingerprint) / 'by-session' (session_id) / 'stats' / 'census'
    (the complete static spawn map joined with runtime counts —
    the one answer to "why did a process spawn").
    """
    return _delegate("ai_process_audit", mode=mode, n=n, key=key, session_id=session_id)


@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "entities": {"required": [], "optional": ["query", "limit"], "desc": "list/search schema entities"},
        "entity": {"required": ["query"], "optional": [], "desc": "one entity by name (fields + relations)"},
        "batch_entity": {"required": ["query"], "optional": [], "desc": "several entities: comma/newline-separated names"},
        "field": {"required": ["query"], "optional": ["limit"], "desc": "find a field/column across all entities"},
        "constructor": {"required": ["query"], "optional": ["include_related"], "desc": "constructor params for an entity"},
        "properties": {"required": ["query"], "optional": [], "desc": "property list for an entity"},
        "trace_flow": {"required": ["query"], "optional": ["limit"], "desc": "where an entity flows (usages across layers)"},
        "trace_path": {"required": ["query"], "optional": ["limit"], "desc": "relationship path: query='Source→Target'"},
    },
)
def ai_schema(
    query: str,
    mode: str = "entities",
    limit: int = 50,
    include_related: bool = False,
) -> dict:
    """Unified schema tool — replaces all schema_find / schema_get /
    schema_trace tools. Modes: 'entities', 'entity', 'field',
    'trace_flow', 'trace_path'.
    """
    return _delegate(
        "ai_schema",
        query=query,
        mode=mode,
        limit=limit,
        include_related=include_related,
    )


# 2026-06-21: semantic_search is a redundant legacy duplicate of mempalace's
# ai_palace_search (the real, provisioned hybrid BM25+vector search). It is
# dropped from the gate's advertised surface (surface=LOCAL_ONLY) — the local
# full-trust stdio agent keeps it, but the gate no longer advertises a second,
# unprovisioned semantic-search read. (Its index siblings semantic_index_sync /
# semantic_index_status are not registry READ entries, so nothing to drop there.)
# SSOT-05 #386/#288: surface=HIDDEN — redundant duplicate of the unified
# ai_recall/ai_memory(search) retrieval; hidden from every agent tools/list.
@tool(surface=HIDDEN, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def semantic_search(query: str, limit: int = 10) -> dict:
    """Search code by meaning, not just keywords. Requires
    sentence-transformers. Run `semantic_index_sync` first. LOCAL only —
    superseded on the gate by ai_palace_search.
    """
    return _delegate("semantic_search", query=query, limit=limit)


# ── ai_memory: the memory_* consolidator (SSOT hard cutover 2026-07-15) ──
# Surface-polymorphic like ai_backlog: cls=EDIT (registry EDIT dispatch binds
# exec-root + audit for the write modes), but the outer_gate per-mode carve-out
# relaxes read/search to tier_r_invoke. The four legacy impls stay registered as
# internal delegate targets (hidden from the agent surface); ai_memory is the
# only advertised memory tool.
@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    modes={
        "capture": {"required": ["kind", "content"], "optional": ["target_hint", "keywords", "severity", "trigger", "priority", "injection_mode", "anchor_symbols", "skip_fit_check", "verbose"], "desc": "persist a durable fact to project memory (FIT-checked; lean receipt by default, verbose=True for the full one)"},
        "read": {"required": ["targets"], "optional": ["include_inactive"], "desc": "read memory entries by target path(s) under .MEMORY/"},
        "search": {"required": ["query"], "optional": ["limit"], "desc": "search the memory store (text + semantic hybrid)"},
        "promote": {"required": ["reason"], "optional": ["content", "content_hash", "kind", "law_id", "keywords"], "desc": "promote a kingdom memory entry to the EMPIRE tier by content OR by content_hash (RBAC admin.manage_config)"},
        "candidates": {"required": [], "optional": [], "desc": "list pending empire-candidate rows ({hash, kind, snippet, captured_at}) promotable by content_hash"},
    },
)
def ai_memory(
    mode: str,
    kind: str = "",
    content: str = "",
    target_hint: str = "",
    keywords: list = None,
    severity: str = "normal",
    trigger: str = "topic",
    priority: str = "normal",
    injection_mode: str = "pointer",
    anchor_symbols: list = None,
    skip_fit_check: bool = False,
    targets: list = None,
    include_inactive: bool = False,
    query: str = "",
    limit: int = 10,
    law_id: str = "",
    reason: str = "",
    content_hash: str = "",
    verbose: bool = False,
) -> dict:
    """Unified project-memory tool — mode='capture'|'read'|'search'|'promote'|'candidates'.

    capture: persist a durable fact (a pre-add FIT check refuses an already-
      stored fact; portable rules/invariants/preferences are flagged for
      operator promotion to the EMPIRE tier). Write only what a fresh-machine
      agent still needs.
    read:    read entries by target path(s) under .MEMORY/.
    search:  unified retrieval — exact keyword rows lead, then law/route,
      KG, and palace-semantic lane hits.
    promote: promote a kingdom entry into the EMPIRE tier (must already exist
      in kingdom memory; portable; non-empty audited reason; admin authority).
        By-reference: content_hash= instead of content promotes the archived
        row's text verbatim (#451). candidates mode lists pending
        empire-candidate rows promotable by hash.
    """
    # Grant-vocab hygiene (2026-07-16), as a COMMENT so it never feeds the
    # pool itself: the DOCSTRING above becomes NLP tool-keywords
    # (_registry_tool_keywords: distinctive len>=8 df<=1 terms surface
    # grants). Keep incidental generic prose out of it — a single stray
    # domain word once made an unrelated operator sentence grant ai_memory.
    m = (mode or "").strip().lower()
    if m == "capture":
        return _delegate(
            "memory_capture",
            kind=kind,
            content=content,
            target_hint=(target_hint or None),
            keywords=keywords,
            severity=severity,
            trigger=trigger,
            priority=priority,
            injection_mode=injection_mode,
            anchor_symbols=anchor_symbols,
            skip_fit_check=skip_fit_check,
            verbose=verbose,
        )
    if m == "read":
        return _delegate("memory_read", targets=(targets or []), include_inactive=include_inactive)
    if m == "search":
        return _delegate("memory_search", query=query, limit=limit)
    if m == "promote":
        return _delegate(
            "memory_promote",
            content=content,
            kind=(kind or "workflow-rule"),
            law_id=law_id,
            keywords=keywords,
            reason=reason,
            content_hash=content_hash,
        )
    if m == "candidates":
        # #451: promote with no content and no hash → candidates listing.
        return _delegate(
            "memory_promote",
            content="",
            kind=(kind or "workflow-rule"),
            law_id="",
            keywords=None,
            reason="",
            content_hash="",
        )
    return {
        "_error": "unknown_mode",
        "_detail": f"ai_memory: mode={mode!r} not in capture/read/search/promote/candidates",
    }


# ── A.4 declare triage (2026-07-11, conductor task wsfvuygx7 → plan §A.4) ──
# Four live adapter tools gain canonical declarations. Surfaces/cls were
# chosen so outer_gate_catalog.classify()/advertised() output for each name
# is IDENTICAL to the pre-declaration state (pinned end-to-end by
# test_outer_gate_resolution_pin + test_a4_tool_surface_triage):
#   ai_bundle  — was already CLASS_READ via the hand-rolled
#                READ_EXEC_ALLOWLIST and already advertised; BOTH/READ
#                reproduces that exactly (read_exec_names union unchanged).
#   ai_slop    — was CLASS_DISCOVERABLE via its MCP_TIER_OVERRIDES manifest
#                entry and NOT advertised; cls is deliberately left EMPTY so
#                classify() falls through `spec.cls or CLASS_DISCOVERABLE`,
#                keeping it off read_exec_names() (cls==READ filter) and off
#                advertised() (discoverable is never advertised). Upgrading
#                it to a gate-served READ is a Track B decision, not this one.
#   workflow_actions_get / workflow_agent_rules — were CLASS_INTERNAL (no
#                gate structure served them); LOCAL_ONLY classifies
#                CLASS_INTERNAL, so the gate posture is byte-identical while
#                the stdio catalog gains both reads.


@tool(
    surface=BOTH,
    cls=READ,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "file": {"required": ["target"], "optional": ["limit", "include_content"], "desc": "target=FILE PATH → outline + deps + schema hints (include_content=N inlines first N lines, budget-labeled)"},
        "service": {"required": ["target"], "optional": ["limit"], "desc": "target=service FILE PATH → file + related backend neighbors"},
        "component": {"required": ["target"], "optional": ["limit"], "desc": "target=component FILE PATH or name → + imported frontend neighbors"},
        "query": {"required": ["target"], "optional": ["limit"], "desc": "target=concept → query hotspot + schema hints + relationship paths"},
        "subsystem": {"required": ["target"], "optional": ["limit"], "desc": "target=concept → broad cross-layer analysis"},
        "dependency": {"required": ["target"], "optional": ["limit"], "desc": "target=FILE PATH → file + resolved dependency chain"},
        "partial": {"required": ["target"], "optional": ["limit"], "desc": "target=C# TYPE NAME → all partial-class definitions"},
        "symbol": {"required": ["target"], "optional": ["limit"], "desc": "target=SYMBOL NAME → definition + references + schema matches"},
        "style": {"required": ["target"], "optional": ["limit"], "desc": "target=CSS CLASS NAME → selector matches"},
        "preset": {"required": ["target"], "optional": ["limit"], "desc": "target=preset name (csharp-partial, data-structure, ...) → preconfigured bundle"},
        "tree": {"required": ["target"], "optional": ["limit"], "desc": "target=component FILE PATH → recursive import tree"},
        "session": {"required": ["target", "session_id"], "optional": ["limit"], "desc": "target=focus hint + session_id → session-guided code bundle"},
        "context": {"required": ["target", "session_id"], "optional": ["limit"], "desc": "target=focus hint + session_id → ranked context bundle"},
    },
)
def ai_bundle(
    target: str,
    mode: str = "file",
    session_id: str | None = None,
    limit: int = 20,
    timeout: int | None = None,
    include_content: int = 0,
) -> dict:
    """File/subsystem structural overview over the selected project. No file changes."""
    # Docstring matches the gate's pre-declaration canonical-manifest text
    # VERBATIM so the pinned resolve() description stays byte-identical
    # (test_outer_gate_resolution_pin) — only the inputSchema gets richer.
    return _delegate(
        "ai_bundle",
        target=target,
        mode=mode,
        session_id=session_id,
        limit=limit,
        timeout=timeout,
        include_content=include_content,
    )


@tool(
    surface=BOTH,
    tier=R,
    scope="catalog",
    annotations=_READ_ANN,
    modes={
        "dead_code": {"required": ["path"], "optional": ["limit"], "desc": "unreferenced symbols in one file"},
        "stale_refs": {"required": ["symbols"], "optional": ["exclude_path", "include_tests", "limit"], "desc": "references to symbols that no longer exist"},
        "untested": {"required": [], "optional": ["glob", "limit"], "desc": "source files with no matching tests"},
        "duplicates": {"required": [], "optional": ["query", "limit"], "desc": "heuristic name+kind duplicates across 3+ files"},
        "clones": {"required": [], "optional": ["min_lines", "limit"], "desc": "project-wide structural clones (AST)"},
        "dead_code_project": {"required": [], "optional": [], "desc": "project-wide Python dead code (Vulture backend)"},
        "backends": {"required": [], "optional": [], "desc": "optional-backend availability report"},
        "hotspots": {"required": ["query"], "optional": ["limit"], "desc": "complexity/churn hotspots"},
        "query_hotspots": {"required": ["query"], "optional": ["limit"], "desc": "heavy DB-query files"},
        "mismatches": {"required": ["query"], "optional": ["limit"], "desc": "state/model representation conflicts"},
        "partial_group": {"required": ["query"], "optional": ["limit"], "desc": "all partial-class files for a C# type"},
        "partial_consumers": {"required": ["query"], "optional": ["limit"], "desc": "pages referencing a Razor partial"},
        "symbol_range": {"required": ["path", "symbol"], "optional": ["kind", "line_number"], "desc": "line span of one symbol"},
        "suggest_extract": {"required": ["path"], "optional": ["min_lines", "limit"], "desc": "extraction candidates in a file"},
        "preview_extract_deps": {"required": ["path", "start_line", "end_line"], "optional": [], "desc": "dependency preview for an extraction range"},
        "broken_refs": {"required": [], "optional": ["path", "limit"], "desc": "references whose definition is missing (ref-integrity)"},
    },
)
def ai_slop(
    mode: str,
    query: str = "",
    path: str = "",
    symbol: str = "",
    kind: str | None = None,
    start_line: int = 0,
    end_line: int = 0,
    symbols: list[str] | None = None,
    exclude_path: str | None = None,
    include_tests: bool = False,
    glob: str | None = None,
    min_lines: int = 20,
    line_number: int | None = None,
    limit: int = 50,
) -> dict:
    """Read-only slop FINDER (all mutating refactor modes live in the guarded
    ai_deslop_apply). This tool NEVER mutates and issues no write grants.
    Modes: dead_code, stale_refs, untested, duplicates, clones,
    dead_code_project, backends, hotspots, query_hotspots, mismatches,
    partial_group, partial_consumers, symbol_range, suggest_extract,
    preview_extract_deps, broken_refs.
    """
    return _delegate(
        "ai_slop",
        mode=mode,
        query=query,
        path=path,
        symbol=symbol,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
        symbols=symbols,
        exclude_path=exclude_path,
        include_tests=include_tests,
        glob=glob,
        min_lines=min_lines,
        line_number=line_number,
        limit=limit,
    )


@tool(surface=LOCAL_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def workflow_actions_get() -> dict:
    """Read the compiled runtime workflow artifact for a project if present."""
    return _delegate("workflow_actions_get")


@tool(surface=LOCAL_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def workflow_agent_rules() -> dict:
    """Get natural language workflow rules for the conductor to enforce on
    agents — human-written rules like 'After fixing a bug, write a test'.
    """
    return _delegate("workflow_agent_rules")


@tool(surface=BOTH, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def related_project_list() -> dict:
    """List all related projects registered for this conductor."""
    return _delegate("related_project_list")


# 2026-06-21: related_project_{code_search,symbol_bundle,subsystem_bundle,
# compare_concept} are LOCAL_ONLY, NOT BOTH. Per-handler audit found each eagerly
# REBUILDS a code (and, for bundles, schema) index on every call
# (server_project_admin_tools.py:1291/1312/1337/1353 hub.code.sync_code_manifest
# = DELETE+INSERT). They are genuine MUTATORS, not reads — advertising them on
# the gate's READ surface was a surface-lie. Dropped to LOCAL_ONLY so the gate
# refuses tools/list + tools/call but the local full-trust stdio agent keeps them.
# Manifest also classifies them Tier-M (see outer_gate_manifest.MCP_TIER_OVERRIDES).
@tool(surface=LOCAL_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def related_project_code_search(
    name: str,
    query: str,
    limit: int = 10,
) -> dict:
    """Code-text search inside a registered related project (eager index
    rebuild on call — LOCAL only).
    """
    return _delegate("related_project_code_search", name=name, query=query, limit=limit)


@tool(surface=LOCAL_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def related_project_symbol_bundle(name: str, symbol: str) -> dict:
    """Read a symbol bundle from a registered related project (eager index
    rebuild on call — LOCAL only).
    """
    return _delegate("related_project_symbol_bundle", name=name, symbol=symbol)


@tool(surface=LOCAL_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def related_project_subsystem_bundle(name: str, subsystem: str) -> dict:
    """Read a subsystem bundle from a registered related project (eager index +
    schema rebuild on call — LOCAL only).
    """
    return _delegate("related_project_subsystem_bundle", name=name, subsystem=subsystem)


@tool(surface=LOCAL_ONLY, cls=READ, tier=R, scope="catalog", annotations=_READ_ANN)
def related_project_compare_concept(
    name: str,
    concept: str,
    limit: int = 10,
) -> dict:
    """Compare a concept across a registered related project's indexed surfaces
    (eager index + schema rebuild on call — LOCAL only).
    """
    return _delegate("related_project_compare_concept", name=name, concept=concept, limit=limit)


# ── EDIT ─────────────────────────────────────────────────────────────
# All EDIT-class entries auto-extend `outer_gate_edit`'s
# `EDIT_ALLOWLIST` at module load. They DON'T go through the
# ai_str_replace-specific propose/commit pipeline (which is
# shape-coupled to path/old_string/new_string) — they route through
# `OuterGate._registry_invoke_edit`, which binds the exec project
# root and dispatches via fastmcp's `call_tool`. Each underlying
# impl already enforces its own protected-paths + size limits +
# audit; the gate adds scope (`tier_m_edit`) + exec-root binding +
# two-phase confirm for destructive shapes.

_EDIT_ANN_SAFE = {
    # All registry-backed edit tools — including the formerly
    # "destructive" set (ai_batch_edit / ai_batch_str_replace /
    # ai_delete / ai_palace_maintenance) — carry destructiveHint=False
    # because AIDOCS owns real-danger refusal internally
    # (judge / protected-paths / size caps / trash containment for
    # ai_delete / dashboard-admin gate for ai_palace_maintenance) AND
    # the registry layer adds confirm=TWO_PHASE on top with an exact
    # phrase the operator must echo. The host annotation is a UX hint
    # only; doctrinally NO advertised tool is destructiveHint=True
    # except the two CONNECT selectors (project_select, session_select)
    # whose host confirmation is the binding event for project/
    # session context. See outer_gate_catalog.py:_annotations and the
    # test_outer_gate_tools_list_metadata contract.
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
# Doctrine 2026-05-29 (Empire-directed seal — clean-VPS Gate 2b cluster,
# carve-out closure): _EDIT_ANN_DESTRUCTIVE used to set destructiveHint=
# True on ai_batch_edit, ai_batch_str_replace, ai_delete, and
# ai_palace_maintenance under a "temporary" carve-out waiting for
# gate-proof. The proofs the carve-out was waiting for now exist (live
# read-pipeline + native Claude PreToolUse trash-gate), so the carve-
# out is expired. Every edit tool now uses _EDIT_ANN_SAFE. The
# confirm=TWO_PHASE + exact confirm phrases are preserved on each
# @tool decorator below — confirmation is owned by AIDOCS at the
# registry layer, NOT by the host annotation.
_EDIT_ANN_DESTRUCTIVE = _EDIT_ANN_SAFE


@tool(surface=BOTH, cls=EDIT, tier=M, scope="tier_m_edit", annotations=_EDIT_ANN_SAFE)
def ai_create_file(
    path: str,
    content: str,
    config_edit_mode: str = "",
) -> dict:
    """Create a new file at a relative path with exact content.
    Refuses if the file already exists (creation, not overwrite).
    Bound to the selected project/session; refuses protected paths;
    the write is audited and reversible via edit_rollback.
    """
    return _delegate(
        "ai_create_file",
        path=path,
        content=content,
        config_edit_mode=(config_edit_mode or None),
    )


@tool(surface=BOTH, cls=EDIT, tier=M, scope="tier_m_edit", annotations=_EDIT_ANN_SAFE)
def ai_insert_lines(
    path: str,
    before_line: int,
    content: str,
    config_edit_mode: str = "",
) -> dict:
    """Insert content before a specific line in an existing file.
    Clearer than ai_replace(mode='lines') for a pure insert. Bound to the selected
    project/session; refuses protected paths; the edit is audited
    and reversible via edit_rollback.
    """
    return _delegate(
        "ai_insert_lines",
        path=path,
        before_line=before_line,
        content=content,
        config_edit_mode=(config_edit_mode or None),
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations=_EDIT_ANN_SAFE,
    modes={
        "string": {"required": ["old_string", "new_string"], "optional": ["replace_all", "config_edit_mode"], "desc": "old_string→new_string; old_string length capped by edit.str_replace_max_old_chars (default 1000; over that use anchor)"},
        "anchor": {"required": ["start_anchor", "end_anchor", "replacement"], "optional": ["config_edit_mode", "allow_partial_anchors"], "desc": "PRIMARY surgical edit: replaces BETWEEN two unique anchor strings (anchors stay in the file)"},
        "symbol": {"required": ["symbol", "new_body"], "optional": ["config_edit_mode"], "desc": "symbol='Class.method' → replace its whole body (index resolves the span)"},
        "lines": {"required": ["start_line", "end_line", "new_content"], "optional": ["config_edit_mode"], "desc": "replace start_line..end_line; file must be re-read before the next line edit"},
    },
)
def ai_replace(
    mode: str,
    path: str,
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    start_anchor: str = "",
    replacement: str = "",
    end_anchor: str = "",
    symbol: str = "",
    new_body: str = "",
    allow_partial_anchors: bool = False,
) -> dict:
    """Unified replace: mode='string' (old/new_string; old_string
    capped by edit.str_replace_max_old_chars, default 1000), 'anchor' (start_anchor + replacement + end_anchor span),
    'symbol' (index-resolved symbol body rewrite — requires `symbol`
    qualified name AND `new_body`). Bound to the selected project/
    session; refuses protected paths; the edit is audited and
    reversible via edit_rollback.
    """
    return _delegate(
        "ai_replace",
        mode=mode,
        path=path,
        old_string=(old_string or None),
        new_string=(new_string or None),
        replace_all=replace_all,
        start_anchor=(start_anchor or None),
        replacement=(replacement or None),
        end_anchor=(end_anchor or None),
        symbol=(symbol or None),
        new_body=(new_body or None),
        allow_partial_anchors=allow_partial_anchors,
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    # #384: no registry-wide confirm — a normal atomic batch (and every
    # dry_run) executes like any other edit. Confirmation lives at the
    # implementation layer: large_batch_confirm gates genuinely large
    # batches only.
    annotations=_EDIT_ANN_DESTRUCTIVE,
    modes={
        "line": {"required": ["edits"], "optional": ["dry_run", "atomic", "config_edit_mode", "large_batch_confirm", "ack_memory_ids", "ack_drawer_ids"], "desc": "line-range edits: [{path, edits:[{start_line,...}]}]"},
        "string": {"required": ["edits"], "optional": ["atomic", "config_edit_mode", "large_batch_confirm", "ack_memory_ids", "ack_drawer_ids"], "desc": "string-match replacements: [{path, old_string, new_string}]"},
    },
)
def ai_batch_edit(
    edits: list,
    mode: Literal["line", "string"] = "line",
    dry_run: bool = False,
    atomic: bool = True,
    config_edit_mode: str = "",
    large_batch_confirm: bool = False,
) -> dict:
    """Apply multiple edits atomically across one or more files.

    mode='line'   — line-range edits: [{path, start_line, end_line, new_content, mode?(insert|replace|auto), expect?}].
                    Strict: unknown/misspelled fields are rejected; new_content
                    must be present (pass '' to delete a range).
    mode='string' — string-match replacements: [{path, old_string, new_string}]
                    (folds in the former standalone ai_batch_str_replace).
    Two-phase confirm at the registry layer because bulk edits are
    high-blast-radius. dry_run=True returns the diff without applying.
    """
    return _delegate(
        "ai_batch_edit",
        edits=edits,
        mode=mode,
        dry_run=dry_run,
        atomic=atomic,
        config_edit_mode=(config_edit_mode or None),
        large_batch_confirm=large_batch_confirm,
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    confirm=TWO_PHASE,
    phrase="confirm delete",
    annotations=_EDIT_ANN_DESTRUCTIVE,
    modes={
        "delete": {"required": ["path", "reason"], "optional": [], "desc": "move a single file to .TRASH/ (regenerable artifacts hard-delete)"},
        "restore": {"required": ["trash_id"], "optional": ["path", "reason"], "desc": "move a trash entry back; path overrides the audited original destination"},
    },
)
def ai_delete(
    path: str = "",
    reason: str = "",
    mode: Literal["delete", "restore"] = "delete",
    trash_id: str = "",
    confirm_token: str = "",
) -> dict:
    """Delete a single project-relative file by moving it to the
    .TRASH/ recovery area, or restore a trashed file (#385). Two-phase
    confirm with the path in the phrase, so the operator sees exactly
    which file before agreeing. Single file only; no glob/batch/
    directory. Regenerable artifacts (build/cache dirs) hard-delete
    instead of going to trash and are not restorable.
    """
    return _delegate("ai_delete", path=path, reason=reason, mode=mode, trash_id=trash_id)


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    annotations=_EDIT_ANN_SAFE,
    modes={
        "add": {"required": ["path"], "optional": ["why", "pair_files", "dnt_id"], "desc": "protect one path (sentinel header + identity registry row)"},
        "remove": {"required": ["path"], "optional": [], "desc": "identity-checked unprotect (mismatch escalates to admin)"},
        "list": {"required": [], "optional": ["with_dnt"], "desc": "protected paths (with_dnt=True adds DNT family info)"},
        "add_batch": {"required": ["paths"], "optional": ["why"], "desc": "protect a list of paths"},
        "sync": {"required": [], "optional": [], "desc": "rebuild the registry from on-disk DNT headers"},
        "get": {"required": [], "optional": ["path", "dnt_id"], "desc": "full structured DNT record by path or dnt_id"},
    },
)
def ai_protect(
    mode: str,
    path: str = "",
    paths: list = None,
    why: str = "",
    pair_files: list = None,
    dnt_id: str = "",
    with_dnt: bool = False,
    symbol: str = "",
) -> dict:
    """DO NOT TOUCH file protection — writes a sentinel header into
    the file AND records the protecting user's identity. Only the
    same user (or admin+) can remove it. mode ∈ {add, remove, list,
    add_batch, sync, get} (the impl's real accepted set). add: protect
    one path; remove: identity-checked unprotect; list: protected paths
    (with_dnt=True surfaces DNT family info); add_batch: protect a
    `paths` list; sync: rebuild the registry from on-disk DNT headers;
    get: full structured DNT record by path or dnt_id.
    """
    return _delegate(
        "ai_protect",
        mode=mode,
        path=path,
        paths=paths,
        why=why,
        pair_files=pair_files,
        dnt_id=dnt_id,
        with_dnt=with_dnt,
        symbol=symbol,
    )


@tool(surface=BOTH, cls=EDIT, tier=M, scope="tier_m_edit", annotations=_EDIT_ANN_SAFE)
def ai_palace_diary_write(
    agent_name: str,
    entry: str,
    topic: str = "",
    wing: str = "",
) -> dict:
    """Write a diary entry for an agent. AAAK-encoded entries
    encouraged. Per RFC 003 §17.1 diaries are agent-isolated by
    default.
    """
    return _delegate(
        "ai_palace_diary_write",
        agent_name=agent_name,
        entry=entry,
        topic=topic,
        wing=wing,
    )


@tool(
    surface=BOTH,
    cls=EDIT,
    tier=M,
    scope="tier_m_edit",
    confirm=TWO_PHASE,
    phrase="confirm palace maintenance",
    annotations=_EDIT_ANN_DESTRUCTIVE,
    modes={
        "backfill_legacy_memory_drawers": {"required": [], "optional": ["dry_run", "force", "operator_token"], "desc": "reconcile pre-deterministic random-id drawers: retire legacy, re-ingest deterministic (admin-gated)"},
        "mine": {"required": ["path"], "optional": ["dry_run", "operator_token"], "desc": "mine a folder UNDER the project root into the palace (guarded ingest: credentials redacted, guard failures refuse the file, souls structurally excluded)"},
    },
)
def ai_palace_maintenance(
    mode: str,
    dry_run: bool = False,
    force: bool = False,
    operator_token: str = "",
    confirm_token: str = "",
    path: str = "",
) -> dict:
    """Guarded palace maintenance. AUTHENTICATED DASHBOARD ADMIN only
    at the impl layer; the gate adds two-phase confirm on top because
    maintenance ops can rewrite/compact the entire palace state.
    Modes: backfill_legacy_memory_drawers; mine (guarded folder ingest,
    ``path`` must resolve under the project root).
    """
    return _delegate(
        "ai_palace_maintenance",
        mode=mode,
        dry_run=dry_run,
        force=force,
        operator_token=operator_token,
        path=path,
    )



@tool(surface=BOTH, cls=EDIT, tier=M, scope="tier_m_edit", annotations=_EDIT_ANN_SAFE)
def handoff_create(
    target_project_root: str,
    target_session_id: str,
    purpose: list = None,
    current_state: list = None,
    what_was_done: list = None,
    what_failed: list = None,
    what_matters_now: list = None,
    open_questions: list = None,
    risks_and_blockers: list = None,
    relevant_files: list = None,
    estimated_effort: list = None,
    suggested_next_steps: list = None,
    related_sessions: list = None,
) -> dict:
    """Create a handoff record from the current session to a target
    project + session. Captures purpose, state, what was done /
    failed, blockers, next steps, etc.
    """
    return _delegate(
        "handoff_create",
        target_project_root=target_project_root,
        target_session_id=target_session_id,
        purpose=purpose,
        current_state=current_state,
        what_was_done=what_was_done,
        what_failed=what_failed,
        what_matters_now=what_matters_now,
        open_questions=open_questions,
        risks_and_blockers=risks_and_blockers,
        relevant_files=relevant_files,
        estimated_effort=estimated_effort,
        suggested_next_steps=suggested_next_steps,
        related_sessions=related_sessions,
    )


# ── RUN-shaped EDIT entries ─────────────────────────────────────────
# Tools that mutate project state but aren't file edits in the
# str_replace sense — index rebuilds, git operations, etc. They live
# under cls=EDIT for dispatch reasons (the EDIT registry path
# binds the exec project root + dispatches through call_tool, which
# is exactly what these need); the existing `gate.run()` path is
# shape-coupled to ai_run and isn't a useful host for non-shell
# maintenance. Scope stays `tier_m_edit` for now; a future commit
# can introduce `project_maintain` if the operator wants a distinct
# scope split (BACKLOG: micro-followup, low priority).


@tool(surface=BOTH, cls=EDIT, tier=M, scope="tier_m_edit", annotations=_EDIT_ANN_SAFE)
def ai_index_sync(
    include_tests: bool = False,
    timeout: int = 0,
) -> dict:
    """Rebuild the derived code file manifest + summary index. Safe
    to re-run (idempotent — recomputes the same state). `timeout=0`
    uses the default. Pass `include_tests=True` to index test files
    alongside source.
    """
    return _delegate("ai_index_sync", include_tests=include_tests, timeout=(timeout or None))


@tool(surface=BOTH, cls=EDIT, tier=M, scope="tier_m_edit", annotations=_EDIT_ANN_SAFE)
def ai_git(
    op: str = "status",
    message: str = "",
    count: int = 10,
    branch: str = "",
    path: str = "",
    range: str = "",
) -> dict:
    """Basic git operations on the bound project. `op` ∈ {status,
    log, diff, add, commit, push, pull, branch, stash}.

    op=status (also reports ahead/behind vs upstream).
    op=log (count, or range like 'origin/main..HEAD').
    op=diff.
    op=add REQUIRES explicit `path` — no `-A` default.
    op=commit REQUIRES `message`.
    op=push / op=pull / op=branch / op=stash.

    The impl rejects shell metacharacters in path/range and refuses
    operations the gate's bash_policy doesn't allow. Dangerous git
    chains (rm-style rebase, hard reset) are NOT exposed here —
    use ai_run with explicit operator intent for those.
    """
    return _delegate(
        "ai_git",
        op=op,
        message=message,
        count=count,
        branch=branch,
        path=path,
        range=range,
    )


# ── SSOT-04 phase B (2026-07-15): gate selector cluster fold-in ──────
# These names were advertised via hand-rolled PROJECT_TOOL_SPECS entries
# (tool_surface_waivers _GATE_SELECTOR_CLUSTER). Each row mirrors the
# transport handler's REAL accepted args; outer_gate_catalog's registry
# auto-extension + the SELECTOR relay branch now source schema/desc from
# here. Convergence: tool-surface map §8 step 6.

@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "sessions": {"type": "array", "items": {"type": "string"}},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "List Sessions",
    },
)
def session_list() -> dict:
    """List sessions in the selected project."""
    return _delegate("session_list")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "execution_session_id": {"type": "string"},
            "project_id": {"type": "string"},
            "project_name": {"type": "string"},
            "project_root": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Current Session",
    },
)
def session_current() -> dict:
    """Show the token's current session id."""
    return _delegate("session_current")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    # Transport enforces the fixed phrase 'confirm select session'
    # (outer_gate_transport ~2249) — declared here as governance truth.
    confirm=TWO_PHASE,
    phrase="confirm select session",
    output={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "project_id": {"type": "string"},
            "project_name": {"type": "string"},
            "project_root": {"type": "string"},
            "previous_session_id": {"type": "string"},
            "confirmed": {"type": "boolean"},
            "action": {"type": "string"},
            "confirm_token": {"type": "string"},
            "summary": {"type": "string"},
            "bind_result": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        # Host-UX law (test_outer_gate_tools_list_metadata): the CONNECT
        # actions (project_select / session_select) are the ONLY advertised
        # tools with destructiveHint=True — it is the host confirm-card hint
        # for context-binding, not a danger marker.
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Select Session",
    },
)
def session_select(session_id: str, confirm_token: str = "") -> dict:
    """Bind this token to a session id within the currently selected project.

    CHANGES the session-scoped audit + memory context that every later tool
    call writes/reads against. Two-phase confirm: first call WITHOUT
    confirm_token returns confirm_required + the exact confirm_token to
    echo; the assistant MUST relay that prompt to the user verbatim and
    only proceed with the confirm_token after the user agrees.
    """
    return _delegate("session_select", session_id=session_id, confirm_token=confirm_token)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "created": {"type": "boolean"},
            "scaffold": {"type": "string"},
            "bound": {"type": "boolean"},
            "project_id": {"type": "string"},
            "project_name": {"type": "string"},
            "project_root": {"type": "string"},
            "previous_session_id": {"type": "string"},
            "bind_result": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Create Session",
    },
)
def session_create(session_id: str = "", goal: str = "") -> dict:
    """Create a new session in the currently selected project (scaffolds
    .MEMORY/sessions/<id>/ with SESSION.md, context, plans/) and BIND this
    token to it. Idempotent: an existing session id is bound, not
    overwritten. Provide session_id (a slug) or omit it to auto-generate
    one; optional goal seeds the session header.
    """
    return _delegate("session_create", session_id=session_id, goal=goal)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    # Transport enforces the fixed phrase 'confirm session delete'
    # (outer_gate_transport ~1766) — declared here as governance truth.
    confirm=TWO_PHASE,
    phrase="confirm session delete",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "deleted": {"type": "string"},
            "confirmed": {"type": "boolean"},
            "action": {"type": "string"},
            "session_id": {"type": "string"},
            "confirm_token": {"type": "string"},
            "summary": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        # Host-UX law: destructiveHint is the CONNECT confirm-card hint,
        # reserved for project_select/session_select. session_delete's real
        # confirmation is the server-side two-phase confirm_token above —
        # the host card is not the enforcement mechanism.
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Delete Session",
    },
)
def session_delete(session_id: str, confirm_token: str = "") -> dict:
    """OWNER/ADMIN only: permanently delete a session workspace in the bound
    project. Two-phase confirm (confirm_token='confirm session delete');
    path-confined + audited.
    """
    return _delegate("session_delete", session_id=session_id, confirm_token=confirm_token)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "project": {"type": ["object", "null"], "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Current Project",
    },
)
def project_current() -> dict:
    """Show the token's selected project + status."""
    return _delegate("project_current")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "status": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Project Status",
    },
)
def project_status(project_id: str = "") -> dict:
    """Trust/index status of the current/given project."""
    return _delegate("project_status", project_id=project_id)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "status": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Project Index Status",
    },
)
def project_index_status(project_id: str = "") -> dict:
    """Index readiness (code_files, stale)."""
    return _delegate("project_index_status", project_id=project_id)


@tool(
    surface=GATE_ONLY,
    cls=IMPORT,
    tier=M,
    scope="project_import",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "project_id": {"type": "string"},
            "synced_at": {"type": "string"},
            "code_files": {"type": "integer"},
            "stash": {"type": "object", "additionalProperties": True},
            "warning": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        # Reaches the project's git origin over the network.
        "openWorldHint": True,
        "title": "Sync Project",
    },
)
def project_sync(project_id: str) -> dict:
    """Fetch+reset+re-index a github-sourced project."""
    return _delegate("project_sync", project_id=project_id)


@tool(
    surface=GATE_ONLY,
    cls=IMPORT,
    tier=M,
    scope="project_import",
    output={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "status": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        # Clones from github.com — open-world by definition.
        "openWorldHint": True,
        "title": "Register Project From GitHub URL",
    },
)
def project_register_from_github_url(url: str, ref: str = "") -> dict:
    """Import a PUBLIC github.com repo (clone+bootstrap+index); private
    only via CodeNexus credentials.
    """
    return _delegate("project_register_from_github_url", url=url, ref=ref)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "orgs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "selected_org_id": {"type": "string"},
            "persisted_org_id": {"type": "string"},
            "persisted_valid": {"type": "boolean"},
            "effective_status": {"type": "string"},
            "org_selection_explicit": {"type": "boolean"},
            "org_unselected_multi": {"type": "boolean"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "List Orgs",
    },
)
def org_list() -> dict:
    """List the orgs you belong to (org_id, role in that org,
    webmcp-entitled). These are the choices for org_select — which org
    this token acts as.
    """
    return _delegate("org_list")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    # Transport enforces the fixed phrase 'confirm select org'
    # (outer_gate_transport ~2826) — declared here as governance truth.
    confirm=TWO_PHASE,
    phrase="confirm select org",
    output={
        "type": "object",
        "properties": {
            "selected": {"type": "string"},
            "name": {"type": "string"},
            "role": {"type": "string"},
            "entitled": {"type": "boolean"},
            "confirmed": {"type": "boolean"},
            "action": {"type": "string"},
            "org_id": {"type": "string"},
            "target_name": {"type": "string"},
            "previous_org_id": {"type": "string"},
            "confirm_token": {"type": "string"},
            "summary": {"type": "string"},
            "candidates": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "available": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "seat_status": {"type": "string"},
            "upgrade_sku": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Select Org",
    },
)
def org_select(org_id: str, confirm_token: str = "") -> dict:
    """Bind this token to one of YOUR orgs (the tenant it acts as). All
    project reads/edits/registry + per-org config then scope to that org.
    A user may belong to several orgs (owner of one, member of another);
    this picks which one. Two-phase confirm: first call WITHOUT
    confirm_token returns confirm_required + the exact confirm_token to
    echo; the assistant relays it to the user and only proceeds with the
    token after they agree.
    """
    return _delegate("org_select", org_id=org_id, confirm_token=confirm_token)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "tools": {"type": "array", "items": {"type": "string"}},
            "tool_source": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Lane Scope",
    },
)
def lane_scope(sessionId: str = "", laneId: str = "") -> dict:
    """Per-lane conductor scope for the lane-detail accordion: the
    effective allowed tools + the lane's owned files.
    """
    return _delegate("lane_scope", sessionId=sessionId, laneId=laneId)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "class": {"type": "string"},
                        "tier": {"type": "string"},
                        "executable_now": {"type": "boolean"},
                        "blocked_by": {"type": ["string", "null"]},
                        "grant_required": {"type": ["string", "null"]},
                    },
                    "additionalProperties": True,
                },
            },
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Tool Catalog",
    },
)
def tool_catalog() -> dict:
    """The canonical tool catalog with honest metadata."""
    return _delegate("tool_catalog")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "class": {"type": "string"},
            "tier": {"type": "string"},
            "schema": {"type": "object", "additionalProperties": True},
            "executable_now": {"type": "boolean"},
            "grant_reason": {"type": ["string", "null"]},
            "grant_required": {"type": ["string", "null"]},
            "visible": {"type": "boolean"},
            "_error": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Tool Capabilities",
    },
)
def tool_capabilities(tool: str) -> dict:
    """Tier/binding/schema + executable_now for a tool."""
    return _delegate("tool_capabilities", tool=tool)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "results": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Skill Scan Results",
    },
)
def skill_scan_results(sessionId: str = "") -> dict:
    """Read the bound project's skills with per-skill security scan + activation tags."""
    return _delegate("skill_scan_results", sessionId=sessionId)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "report": {"type": ["object", "null"], "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Broken References",
    },
)
def broken_references(limit: int = 200) -> dict:
    """Ref-integrity report for the bound project: references whose token has no
    resolving definition (heuristic, regex-extracted, truth-labeled).
    """
    return _delegate("broken_references", limit=limit)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "servers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "List MCP Servers",
    },
)
def list_mcp_servers() -> dict:
    """List MCP servers installed in the bound project."""
    return _delegate("list_mcp_servers")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "servers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "next_cursor": {"type": ["string", "null"]},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Search MCP Registry",
    },
)
def mcp_registry_search(query: str = "", limit: int = 20, cursor: str = "") -> dict:
    """Search the public MCP registry for installable servers."""
    return _delegate("mcp_registry_search", query=query, limit=limit, cursor=cursor)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "path": {"type": "string"},
            "kind": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "source": {"type": "string"},
            "status": {"type": "string"},
            "updated_at": {"type": ["string", "null"]},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Memory KG Get",
    },
)
def memory_kg_get(path: str) -> dict:
    """Read ONE memory's full body (title/kind/content) by its canonical path."""
    return _delegate("memory_kg_get", path=path)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "nodes": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "edges": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "counts": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Memory KG Graph",
    },
)
def memory_kg_graph() -> dict:
    """Read the bound project's memory knowledge graph (memories, code units, keywords, edges)."""
    return _delegate("memory_kg_graph")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "groups": {"type": "object", "additionalProperties": True},
            "kind": {"type": "string"},
            "lang": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Vocab Get Grouped",
    },
)
def vocab_get_grouped(kind: str, lang: str) -> dict:
    """Read the bound tenant's intent-vocab groups for a (kind, lang)."""
    return _delegate("vocab_get_grouped", kind=kind, lang=lang)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "kinds": {"type": "array", "items": {"type": "string"}},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Vocab List Kinds",
    },
)
def vocab_list_kinds() -> dict:
    """List the available intent-vocab kinds."""
    return _delegate("vocab_list_kinds")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "langs": {"type": "array", "items": {"type": "string"}},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Vocab List Langs",
    },
)
def vocab_list_langs() -> dict:
    """List the languages present in the bound tenant's intent vocab."""
    return _delegate("vocab_list_langs")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    # Transport enforces the fixed phrase 'confirm capture'
    # (outer_gate_transport ~1648) — declared here as governance truth.
    confirm=TWO_PHASE,
    phrase="confirm capture",
    output={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "target": {"type": "string"},
            "checksum": {"type": "string"},
            "action": {"type": "string"},
            "confirm_token": {"type": "string"},
            "summary": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Dashboard Memory Capture",
    },
)
def dashboard_memory_capture(
    kind: str,
    content: str,
    target_hint: str = "",
    confirm_token: str = "",
) -> dict:
    """Org OWNER/ADMIN only: governed memory write for the dashboard capture
    form — delegates to the SAME MemoryStore.capture_memory doctrine path as
    the memory_capture agent tool (durability rubric, sovereign guard,
    sqlite-canonical). Two-phase confirm (confirm_token='confirm capture');
    intent audited before mutation. Agents use memory_capture under
    tier_m_edit instead — this surface never widens that boundary.
    """
    return _delegate(
        "dashboard_memory_capture",
        kind=kind,
        content=content,
        target_hint=target_hint,
        confirm_token=confirm_token,
    )


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "view": {"type": "string"},
            "data": {"type": "object", "additionalProperties": True},
            "project_root": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Dashboard View",
    },
)
def dashboard_view(view: str) -> dict:
    """OWNER/ADMIN only: read a control-plane panel view of the bound org
    (view in rbac | bash_policy | config). Tenant-scoped + audited; read-only.
    """
    return _delegate("dashboard_view", view=view)


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "snapshot": {"type": "object", "additionalProperties": True},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Dashboard Snapshot",
    },
)
def dashboard_snapshot() -> dict:
    """Full dashboard snapshot (overview, sessions, execution, tool calls,
    monitoring) for the gate's project — read-only. Powers the desktop
    dashboard's WebMCP (cloud) scope.
    """
    return _delegate("dashboard_snapshot")


@tool(
    surface=GATE_ONLY,
    cls=SELECTOR,
    tier=R,
    scope="catalog",
    output={
        "type": "object",
        "properties": {
            "settings": {"type": "object", "additionalProperties": True},
            "project_root": {"type": "string"},
            "_error": {"type": "string"},
            "_detail": {"type": "string"},
        },
        "additionalProperties": True,
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "title": "Config View",
    },
)
def config_view() -> dict:
    """OWNER/ADMIN only: view the bound org's config settings (read-only, with
    scope + metadata). Tenant-scoped + audited.
    """
    return _delegate("config_view")


# ════════════════════════════════════════════════════════════════════
# Migration progress (SSOT-04, 2026-07-15): every live agent-facing
# surface is now either DECLARED here or carries a reasoned permanent
# waiver (tool_surface_waivers.py — shell trio + config_set +
# session_start by Empire ruling, and the 21 lane/plan deprecation-window
# siblings pending window close). Phase B folded the 28-name gate
# selector cluster; phase C folded the stdio adapters (ai_backends,
# ai_models, ai_deslop_apply, planning_step_mark,
# workflow_action_satisfy). Still open: the
# create_server-iterates-registry phase that strips the original
# @server.tool decorators (BACKLOG C.20 final).
# ════════════════════════════════════════════════════════════════════


# ── Family-module registration (C.18 single-source bridge) ──────────
# tool_families/*.py declare the per-family web-promotion @tool specs.
# Imported HERE, at end-of-file, AFTER every registry primitive (`tool`,
# `BOTH`, tier consts, `_TOOLS`, …) AND this file's own declarations are
# defined — so a family module's `from .tool_interface import tool, BOTH`
# resolves against the already-populated symbols (no forward-reference),
# and its specs land in the same `_TOOLS` registry. No-op until the first
# family module exists.
from . import tool_families  # noqa: E402,F401
