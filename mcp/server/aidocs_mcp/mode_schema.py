"""Mode-dispatch tool schema enrichment for FastMCP.

Phoenix 2026-05-12 (Empire directive). Anthropic API rejects top-level
oneOf/anyOf/allOf on inputSchema, so we cannot emit a discriminated
schema. We enrich the FLAT schema instead:
* mode becomes an enum of declared values
* each param description names which modes require/use/ignore it

SSOT war 2026-07-15: the local @modes decorator is GONE. Per-mode contracts
are declared once, as modes={...} on the tool's @tool declaration in
tool_interface.py (ToolSpec.modes). Each spec may carry:
  required: [param, ...]
  optional: [param, ...]
  desc: one-line agent-facing description — WHAT the mode does and what the
        polymorphic primary param (query/target/...) means in that mode.
        Rendered into the mode enum description so agents never have to
        guess (operator directive 2026-06-11).
This module keeps the enrichment logic (enrich_flat_schema) and the local
FastMCP bridge (apply_mode_schemas); both read ToolSpec.modes.
"""

from __future__ import annotations


def _build_param_usage_map(mode_specs):
    usage = {}
    for mode_name, spec in mode_specs.items():
        for p in (spec or {}).get("required", []) or []:
            usage.setdefault(p, {"required_in": [], "optional_in": []})["required_in"].append(
                mode_name,
            )
        for p in (spec or {}).get("optional", []) or []:
            usage.setdefault(p, {"required_in": [], "optional_in": []})["optional_in"].append(
                mode_name,
            )
    return usage


def enrich_flat_schema(base_schema, mode_specs):
    import copy

    schema = copy.deepcopy(base_schema)
    props = schema.setdefault("properties", {})
    if "mode" in props and isinstance(props["mode"], dict):
        props["mode"]["enum"] = list(mode_specs.keys())
        existing = props["mode"].get("description", "")
        described = [(m, (spec or {}).get("desc", "")) for m, spec in mode_specs.items()]
        if any(d for _, d in described):
            # Per-mode one-liners (2026-06-11): mode names alone force
            # agents to guess what each mode does and what the primary
            # param means in it. Render a compact catalog instead.
            lines = [(f"{m!r} — {d}" if d else repr(m)) for m, d in described]
            msg = (
                "Dispatcher. Modes:\n"
                + "\n".join(lines)
                + "\nPer-mode required params are listed in each affected param description."
            )
        else:
            msg = (
                "Dispatcher. Pick one of: "
                + ", ".join(repr(m) for m in mode_specs.keys())
                + ". Per-mode required params are listed in this tool docstring and in each affected param description."
            )
        props["mode"]["description"] = (existing + " " + msg).strip() if existing else msg
    usage = _build_param_usage_map(mode_specs)
    for pname, info in usage.items():
        if pname not in props or not isinstance(props[pname], dict):
            continue
        req = info["required_in"]
        opt = info["optional_in"]
        bits = []
        if req:
            bits.append(
                ("Required when mode=" + ", ".join(repr(m) for m in req))
                if len(req) > 1
                else ("Required when mode=" + repr(req[0])),
            )
        if opt:
            bits.append(
                ("Optional in mode=" + ", ".join(repr(m) for m in opt))
                if len(opt) > 1
                else ("Optional in mode=" + repr(opt[0])),
            )
        # 'Ignored in mode=...' annotation removed 2026-05-12 (Empire
        # directive): for tools with many modes, listing every mode the
        # param ISN T used in inflates the schema by 30-50 percent. The agent
        # infers ignored from absence.
        ann = ". ".join(bits) + "." if bits else ""
        if ann:
            existing = props[pname].get("description", "")
            props[pname]["description"] = (existing + " " + ann).strip() if existing else ann
    return schema


def apply_mode_schemas(server):
    """Enrich the local FastMCP flat schemas with the mode enum + per-mode param
    usage. SSOT (#370): the mode declaration is read from tool_interface.ToolSpec
    .modes — the ONE canonical home — so the local surface and the WebMCP surface
    (tool_interface.schema_for, which reads the SAME ToolSpec.modes) enrich from
    one source. The migration is complete (2026-07-15): there is no fallback;
    a tool without ToolSpec.modes gets no mode enrichment anywhere."""
    from . import tool_interface as _ti

    rewritten = 0
    components = getattr(getattr(server, "_local_provider", None), "_components", {}) or {}
    for key, comp in list(components.items()):
        if not str(key).startswith("tool:"):
            continue
        fn = getattr(comp, "fn", None)
        if fn is None:
            continue
        name = str(getattr(comp, "name", "") or "")
        spec = _ti.get(name) if name else None
        specs = getattr(spec, "modes", None) if spec is not None else None
        if not specs:
            continue
        try:
            current = comp.parameters or {}
            comp.parameters = enrich_flat_schema(current, specs)
            rewritten += 1
        except Exception:
            continue
    return rewritten
