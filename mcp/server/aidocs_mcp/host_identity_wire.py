"""Wire names for the AIDOCS host-identity envelope on MCP requests.

An AIDOCS edge that forwards a local agent's tool call to a remote AIDOCS MCP
server describes the calling host in the request's JSON-RPC ``params._meta``,
under the keys below. They are CLAIMS: the receiving server composes them with
the caller it authenticated, and none of them selects or grants anything on
its own.

The two prefixes mark the origin of a composed host-session id, so an audit
row shows at a glance whether a session came from a native AIDOCS edge or a
web MCP client.

This module holds names only. It imports nothing.
"""

from __future__ import annotations

#: The calling host's own session id, as the edge sees it.
META_AIDOCS_HOST_SESSION = "aidocs/hostSession"
#: The calling host's kind (for example ``claude_code``).
META_AIDOCS_HOST_KIND = "aidocs/hostKind"
#: Which subagent of that host session is speaking. Absent means the main thread.
META_AIDOCS_HOST_AGENT = "aidocs/hostAgent"
#: The canonical project id the edge believes it is bound to.
META_AIDOCS_PROJECT_ID = "aidocs/projectId"
#: The AIDOCS session id the edge believes it is bound to.
META_AIDOCS_SESSION_ID = "aidocs/sessionId"
#: An internal XAACP transport op the edge itself builds. It travels only in
#: ``_meta``, never in tool arguments, which an agent shapes.
META_AIDOCS_STREAM_OP = "aidocs/streamOp"

#: Prefix of a host-session id composed for a native AIDOCS edge.
NATIVE_HOST_SESSION_PREFIX = "mcp-"
#: Prefix of a host-session id composed for a web MCP client.
HOST_SESSION_PREFIX = "web-"

#: The environment flag that switches on per-run subagent identity for web MCP
#: clients. Off by default. A process that sets it without the component that
#: answers it refuses the affected XAACP calls rather than guess.
WEB_RUN_AXIS_ENV = "AIDOCS_WEBMCP_WINDOW_RUN_AXIS"
