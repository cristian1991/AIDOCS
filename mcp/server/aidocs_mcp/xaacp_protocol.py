"""The XAACP gate protocol marker (operator decision, review/xaacp-full-delivery).

Every reply on the ai_msg surface carries ``xaacp_protocol`` (an integer) and
``xaacp_features`` (a sorted list of feature names). A client that is about to
use a feature REQUIRES a marker that names it. A missing or older marker means
an old gate, and the client fails closed: nothing is sent and nothing is
resent. There is no compatibility path.

Why a marker is needed: a pre-XAACP gate filtered unknown arguments to the
impl signature and EXECUTED the call (measured on origin/main 2d1572bc9). A
`subject`, `receipt` or `page_token` was dropped silently and the call
answered ok, so only a positive, machine-checkable marker can prove the gate
understands them.
"""

from __future__ import annotations

from typing import Any

#: Bumped whenever the ai_msg wire contract gains a feature a client may rely on.
XAACP_PROTOCOL_VERSION = 2

#: The features protocol 2 guarantees. Each name is a wire argument (or an
#: argument inside a forwarded stream op) the gate honours, not drops.
XAACP_PROTOCOL_FEATURES: tuple[str, ...] = ("page_token", "receipt", "subject")

MARKER_VERSION_KEY = "xaacp_protocol"
MARKER_FEATURES_KEY = "xaacp_features"


def stamp(reply: Any) -> Any:
    """Add the marker to one ai_msg reply (dicts only; never overwrites)."""
    if isinstance(reply, dict):
        reply.setdefault(MARKER_VERSION_KEY, XAACP_PROTOCOL_VERSION)
        reply.setdefault(MARKER_FEATURES_KEY, list(XAACP_PROTOCOL_FEATURES))
    return reply


def check(reply: Any, required: tuple[str, ...] = XAACP_PROTOCOL_FEATURES) -> str:
    """'' when ``reply`` carries a marker that covers ``required``; else why not."""
    if not isinstance(reply, dict) or MARKER_VERSION_KEY not in reply:
        return "the gate's reply carries no xaacp_protocol marker"
    version = reply.get(MARKER_VERSION_KEY)
    if isinstance(version, bool) or not isinstance(version, int):
        return f"the gate's xaacp_protocol marker is malformed ({version!r})"
    if version < XAACP_PROTOCOL_VERSION:
        return f"the gate speaks xaacp_protocol {version}; this client requires {XAACP_PROTOCOL_VERSION}"
    features = reply.get(MARKER_FEATURES_KEY)
    if not isinstance(features, list):
        return "the gate's xaacp_features marker is missing or malformed"
    missing = [f for f in required if f not in features]
    if missing:
        return f"the gate does not declare the feature(s) {', '.join(missing)}"
    return ""
