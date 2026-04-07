"""Tool policies — admin-defined allow/deny rules for tool access.

Policies are glob-pattern based rules evaluated in priority order (first match wins).
Stored in aidocs.toml under [policies.tools]:

    [[policies.tools]]
    pattern = "code_edit_*"
    action = "allow"
    priority = 10

    [[policies.tools]]
    pattern = "bash"
    action = "ask"
    priority = 20

Actions:
    allow  — tool call proceeds (default if no policy matches)
    deny   — tool call blocked with reason
    ask    — tool call requires user confirmation (advisory)
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ToolPolicy:
    pattern: str
    action: str  # "allow", "deny", "ask"
    priority: int = 0
    reason: str = ""

    def matches(self, tool_name: str) -> bool:
        return fnmatch.fnmatch(tool_name.lower(), self.pattern.lower())

    def to_dict(self) -> dict[str, object]:
        return {
            "pattern": self.pattern,
            "action": self.action,
            "priority": self.priority,
            "reason": self.reason,
        }


@dataclass(slots=True)
class PolicyDecision:
    action: str  # "allow", "deny", "ask"
    matched_policy: ToolPolicy | None = None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def blocked(self) -> bool:
        return self.action == "deny"


def _load_policies_from_config(project_root: Path) -> list[ToolPolicy]:
    """Load tool policies from aidocs.toml [policies.tools] array."""
    config_path = project_root / "aidocs.toml"
    if not config_path.is_file():
        return []

    try:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return []

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        policies_section = data.get("policies", {})
        if not isinstance(policies_section, dict):
            return []

        tool_policies = policies_section.get("tools", [])
        if not isinstance(tool_policies, list):
            return []

        result: list[ToolPolicy] = []
        for entry in tool_policies:
            if not isinstance(entry, dict):
                continue
            pattern = str(entry.get("pattern", "")).strip()
            action = str(entry.get("action", "allow")).strip().lower()
            if not pattern or action not in ("allow", "deny", "ask"):
                continue
            result.append(ToolPolicy(
                pattern=pattern,
                action=action,
                priority=int(entry.get("priority", 0)),
                reason=str(entry.get("reason", "")),
            ))

        # Sort by priority descending (higher priority = checked first)
        result.sort(key=lambda p: p.priority, reverse=True)
        return result

    except Exception:
        return []


# Cache per project root
_policy_cache: dict[str, tuple[float, list[ToolPolicy]]] = {}


def get_policies(project_root: Path) -> list[ToolPolicy]:
    """Get cached policies for a project. Refreshes if file changed."""
    config_path = project_root / "aidocs.toml"
    cache_key = str(project_root)

    try:
        mtime = config_path.stat().st_mtime if config_path.is_file() else 0.0
    except OSError:
        mtime = 0.0

    cached = _policy_cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    policies = _load_policies_from_config(project_root)
    _policy_cache[cache_key] = (mtime, policies)
    return policies


def evaluate_tool(project_root: Path, tool_name: str) -> PolicyDecision:
    """Evaluate a tool call against project policies. First match wins."""
    name = tool_name.strip().lower()
    # Strip MCP prefix
    for prefix in ("mcp__aidocs__", "mcp__playwright__"):
        if name.startswith(prefix):
            name = name[len(prefix):]

    policies = get_policies(project_root)
    for policy in policies:
        if policy.matches(name):
            return PolicyDecision(
                action=policy.action,
                matched_policy=policy,
                reason=policy.reason or f"Matched policy: {policy.pattern} → {policy.action}",
            )

    # Default: allow
    return PolicyDecision(action="allow")


def clear_cache(project_root: Path | None = None) -> None:
    """Clear policy cache for a project or all projects."""
    if project_root:
        _policy_cache.pop(str(project_root), None)
    else:
        _policy_cache.clear()
