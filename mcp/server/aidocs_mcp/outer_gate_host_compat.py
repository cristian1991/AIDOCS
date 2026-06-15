from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_INSTALLED = False
_PATCHED: set[str] = set()
# Carrier: a host that can only register the already-exposed `tool_capabilities`
# resource may still reach the dispatcher by encoding an aidocs_call payload in
# its `tool` argument as `aidocs_call:{json}`. Catalog truth is unchanged — this
# is a transport-level carrier, not a new advertised tool; the encoded target
# still passes its own scope/trust/project/confirm/audit law.
_PREFIX = "aidocs" + "_call:"
_CAP_TOOL = "tool" + "_capabilities"
_TARGETS = frozenset(
    {
        "aidocs_mcp.outer_gate_catalog",
        "aidocs_mcp.outer_gate_transport",
    },
)

_AIDOCS_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
    },
    "required": ["tool"],
}


def install() -> None:
    global _INSTALLED
    if not _INSTALLED:
        sys.meta_path.insert(0, _AidocsCallPatchFinder())
        _INSTALLED = True
    for name in tuple(_TARGETS):
        mod = sys.modules.get(name)
        if mod is not None:
            _patch_module(mod)


class _AidocsCallPatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any, target: Any = None):
        if fullname not in _TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if isinstance(spec.loader, _AidocsCallPatchLoader):
            return spec
        spec.loader = _AidocsCallPatchLoader(spec.loader)
        return spec


class _AidocsCallPatchLoader(importlib.abc.Loader):
    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        cm = getattr(self._wrapped, "create_module", None)
        return cm(spec) if cm is not None else None

    def exec_module(self, module: ModuleType) -> None:
        self._wrapped.exec_module(module)
        _patch_module(module)


def _patch_module(module: ModuleType) -> None:
    name = getattr(module, "__name__", "")
    if name in _PATCHED:
        return
    if name == "aidocs_mcp.outer_gate_catalog":
        _patch_catalog(module)
        _PATCHED.add(name)
        return
    if name == "aidocs_mcp.outer_gate_transport":
        _patch_transport(module)
        _PATCHED.add(name)
        return


def _patch_catalog(cat: ModuleType) -> None:
    cat.PROJECT_TOOL_SPECS["aidocs_call"] = {
        "desc": "Stable AIDOCS call dispatcher for MCP hosts.",
        "schema": _AIDOCS_CALL_SCHEMA,
        "cls": cat.CLASS_SELECTOR,
    }
    cat.PROJECT_READ_TOOLS = frozenset(
        n for n, s in cat.PROJECT_TOOL_SPECS.items() if s["cls"] == cat.CLASS_SELECTOR
    )
    cat.PROJECT_EDIT_TOOLS = frozenset(
        n for n, s in cat.PROJECT_TOOL_SPECS.items() if s["cls"] == cat.CLASS_IMPORT
    )
    cat.PROJECT_TOOLS = cat.PROJECT_READ_TOOLS | cat.PROJECT_EDIT_TOOLS


def _patch_transport(transport: ModuleType) -> None:
    original = transport.handle_project_tool

    def patched_handle_project_tool(
        name,
        args,
        *,
        gate,
        home,
        default_exec_root,
        token_id,
        principal,
        has,
    ) -> dict:
        if name == _CAP_TOOL:
            raw = ""
            if isinstance(args, dict):
                raw = str(args.get("tool") or "")
            if raw.startswith(_PREFIX):
                return _handle_encoded_call(
                    original,
                    raw,
                    gate=gate,
                    home=home,
                    default_exec_root=default_exec_root,
                    token_id=token_id,
                    principal=principal,
                    has=has,
                )
        if name != "aidocs_call":
            return original(
                name,
                args,
                gate=gate,
                home=home,
                default_exec_root=default_exec_root,
                token_id=token_id,
                principal=principal,
                has=has,
            )
        return _handle_aidocs_call(
            original,
            args,
            gate=gate,
            home=home,
            default_exec_root=default_exec_root,
            token_id=token_id,
            principal=principal,
            has=has,
        )

    transport.handle_project_tool = patched_handle_project_tool
    # The transport's tools/call dispatch gates on its OWN module-level
    # PROJECT_TOOLS binding (imported by value at load time), not catalog's. The
    # catalog patch reassigns catalog.PROJECT_TOOLS to a NEW frozenset, which the
    # transport's binding never sees — so without this, `aidocs_call` is advertised
    # but the dispatch falls through to gate.invoke → unknown_tool. Union it in so
    # `name in PROJECT_TOOLS` routes aidocs_call to (patched) handle_project_tool.
    if hasattr(transport, "PROJECT_TOOLS"):
        transport.PROJECT_TOOLS = frozenset(transport.PROJECT_TOOLS) | {"aidocs_call"}


def _handle_encoded_call(
    original,
    raw: str,
    *,
    gate,
    home,
    default_exec_root,
    token_id,
    principal,
    has,
) -> dict:
    try:
        payload = json.loads(raw[len(_PREFIX) :])
    except Exception as exc:
        return {"_error": "invalid_args", "_detail": str(exc)}
    if not isinstance(payload, dict):
        return {"_error": "invalid_args", "_detail": "payload must be an object"}
    tool = str(payload.get("tool") or payload.get("name") or "")
    if tool in {"aidocs_call", _CAP_TOOL}:
        return {"_error": "recursive_dispatch_refused"}
    return _handle_aidocs_call(
        original,
        payload,
        gate=gate,
        home=home,
        default_exec_root=default_exec_root,
        token_id=token_id,
        principal=principal,
        has=has,
    )


def _selected_exec_root(*, gate, home, default_exec_root, token_id) -> str | None:
    try:
        from . import outer_gate_projects as P

        store = P.GateProjectStore()
        default_root = default_exec_root or getattr(gate, "_exec_project_root", "")
        if default_root:
            store.ensure_default(home, name="AutoDeployBase", root=Path(default_root))
        cur = store.current(home, token_id or "")
        return cur["root"] if cur else None
    except Exception:
        return None


def _target_allowed(tool: str) -> bool:
    from .outer_gate_catalog import PROJECT_TOOLS
    from .outer_gate_edit import EDIT_ALLOWLIST
    from .outer_gate_executor import READ_EXEC_ALLOWLIST, RUN_ALLOWLIST

    allowed = (
        set(READ_EXEC_ALLOWLIST)
        | set(EDIT_ALLOWLIST)
        | set(RUN_ALLOWLIST)
        | (set(PROJECT_TOOLS) - {"aidocs_call"})
    )
    return tool in allowed


def _handle_aidocs_call(
    original,
    args,
    *,
    gate,
    home,
    default_exec_root,
    token_id,
    principal,
    has,
) -> dict:
    if not has("catalog"):
        return {"_error": "insufficient_scope", "_detail": "grant_required=catalog"}
    if not isinstance(args, dict):
        return {"_error": "invalid_args", "_detail": "arguments must be an object"}
    tool = str(args.get("tool") or args.get("name") or "").strip()
    target_args = args.get("arguments")
    if not isinstance(target_args, dict):
        target_args = {}
    if not tool:
        return {"_error": "missing_tool", "_detail": "aidocs_call requires tool"}
    if tool == "aidocs_call":
        return {"_error": "recursive_dispatch_refused"}
    if not _target_allowed(tool):
        return {"_error": "unknown_tool", "_detail": f"not public remote-callable: {tool}"}

    from .outer_gate import GateRequest
    from .outer_gate_catalog import PROJECT_TOOLS

    if tool in PROJECT_TOOLS:
        return original(
            tool,
            target_args,
            gate=gate,
            home=home,
            default_exec_root=default_exec_root,
            token_id=token_id,
            principal=principal,
            has=has,
        )

    exec_root = _selected_exec_root(
        gate=gate,
        home=home,
        default_exec_root=default_exec_root,
        token_id=token_id,
    )
    # CANONICAL ROUTING (sealed design §4a): route through gate.execute() — the
    # SAME canonical admission cascade the direct tools/call path uses (see
    # outer_gate_transport tools/call) — instead of the old per-class
    # gate.run/gate.edit/gate.invoke dispatch. That per-class path sent EVERY
    # EDIT to gate.edit (the ai_str_replace propose/commit pipeline, shape-coupled
    # to path/old_string/new_string), so a content-shaped edit via aidocs_call
    # (e.g. ai_create_file) blew up → 502. Through execute(), aidocs_call dispatch
    # is IDENTICAL to the direct route (registry-backed EDIT → _registry_invoke_edit,
    # ai_str_replace → self.edit), scope is checked inside execute(), and
    # confirm_token stays IN tool_input so the two-phase consume fires.
    cv = gate.execute(
        GateRequest(
            tool_name=tool,
            kind="mcp_tool",
            principal=principal,
            project_root=str(home) if home else None,
            tool_input=dict(target_args),
            exec_root=exec_root,
        ),
    )
    if cv.verdict != "pass":
        out = {
            "_error": cv.blocked_by,
            "_detail": cv.reason,
            "exec_project_root": cv.exec_project_root,
            "exec_project_id": cv.exec_project_id,
        }
        if getattr(cv, "pending_action", None):
            out["pending_action"] = cv.pending_action
        if getattr(cv, "freeze_id", None):
            out["freeze_id"] = cv.freeze_id
        return out
    return {
        "tool": tool,
        "result": cv.result,
        "exec_project_root": cv.exec_project_root,
        "exec_project_id": cv.exec_project_id,
    }
