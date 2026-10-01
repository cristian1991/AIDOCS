"""Canonical public contract for the two config_set authority sinks."""

from __future__ import annotations

CONFIG_SET_DESCRIPTION = """Set one configuration value through the active authority surface.

setting_path names the configuration key. value is required and may be any JSON
value. scope defaults to project; scope_key identifies the scoped row when the
chosen scope requires one. Local stdio calls remain guarded by project RBAC,
the dashboard config-edit switch, and a current user-intent grant. WebMCP calls
remain restricted to org administrators and require their two-phase action
confirmation: the first call returns confirm_required with the exact speakable
phrase to echo back (confirm_token='confirm config set').
""".strip()

CONFIG_SET_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
    "title": "Config Set",
}
