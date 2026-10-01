"""The toolspace-scoped MCP catalog of one installed pack (RFC 0003 §4.1, §6; build plan S4b).

An application connector advertises EXACTLY the tools of the bundle pinned by
its active AIDOCS application binding -- never a native ``ai_*`` tool, never a
tool of another toolspace. Each descriptor is taken from the pinned bundle
bytes (``registered_schema``, ``annotations``, ``description``) verbatim, with
ONE projection: a tool that declares ``file_params`` carries the host
file-parameter hint (``openai/fileParams``), and each declared file parameter
is shown in the HOST's file contract (:data:`.host_file_ingress.HOST_FILE_PARAM_SCHEMA`),
not the application's schema for it. The application never receives the
host's file object -- AIDOCS fetches it and relays a content-addressed
descriptor in its place (§14) -- so its schema for that slot describes the
relay, and showing it to the host made the host refuse to bind files. The
pinned bundle itself is never modified.
"""
from __future__ import annotations

import json
from typing import Any

from .host_file_ingress import host_catalog_schema, host_input_schema

__all__ = ["HOST_FILE_PARAMS_META", "host_catalog_schema", "host_input_schema", "mcp_tools"]

HOST_FILE_PARAMS_META = "openai/fileParams"


def mcp_tools(pack: Any) -> list[dict]:
    """MCP ``tools/list`` entries for ``pack`` (a validated ``PackBundle``)."""
    raw = json.loads(pack.canonical_bytes)
    declared = set(pack.tools)
    out: list[dict] = []
    raw_tools = raw.get("tools") or {}
    # The bundle keys tools by name (§6.1); tolerate a list form defensively.
    tool_list = list(raw_tools.values()) if isinstance(raw_tools, dict) else list(raw_tools)
    for tool in tool_list:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if name not in declared:
            continue  # only what the validator admitted
        # The host hint comes from the VALIDATED ToolSpec (r0b2 B3), never from
        # the raw bundle JSON, so it always equals what tools/call relays.
        file_params = pack.tools[name].file_params
        entry: dict = {
            "name": name,
            "description": str(tool.get("description") or ""),
            "inputSchema": host_catalog_schema(tool.get("registered_schema") or {"type": "object"}, file_params),
            "annotations": dict(tool.get("annotations") or {}),
        }
        if file_params:
            entry["_meta"] = {HOST_FILE_PARAMS_META: list(file_params)}
        out.append(entry)
    out.sort(key=lambda t: t["name"])
    return out
