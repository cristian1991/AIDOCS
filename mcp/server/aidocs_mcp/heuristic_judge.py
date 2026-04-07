"""Heuristic judge — fast deterministic rules for tool call risk assessment.

Evaluates tool calls against ~40 heuristic rules across 4 risk tiers.
Sub-millisecond latency, no LLM inference. Runs BEFORE tool execution
as a complement to the keyword-based intent_guard.

Risk tiers:
    SAFE        — no concerns detected
    LOW         — informational, log only
    MEDIUM      — needs user awareness
    HIGH        — should be blocked or require confirmation
    CRITICAL    — always blocked, potential security threat

Each rule returns a RuleVerdict with risk tier, description, and evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class RuleVerdict:
    rule_id: str
    risk: str  # "safe", "low", "medium", "high", "critical"
    description: str
    evidence: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "risk": self.risk,
            "description": self.description,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass(slots=True)
class JudgeResult:
    tool_name: str
    verdicts: list[RuleVerdict] = field(default_factory=list)

    @property
    def max_risk(self) -> str:
        _ORDER = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        if not self.verdicts:
            return "safe"
        return max(self.verdicts, key=lambda v: _ORDER.get(v.risk, 0)).risk

    @property
    def should_block(self) -> bool:
        return self.max_risk in ("high", "critical")

    @property
    def clean(self) -> bool:
        return self.max_risk == "safe"

    def summary(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "max_risk": self.max_risk,
            "should_block": self.should_block,
            "verdict_count": len(self.verdicts),
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


# ── Rule definitions ──

_Rule = tuple[str, str, str]  # (rule_id, risk, description)


def _check_bash_rules(command: str) -> list[RuleVerdict]:
    """Rules for bash/shell command execution."""
    verdicts: list[RuleVerdict] = []
    lower = command.lower()

    # CRITICAL: rm -rf on root or home
    if re.search(r"rm\s+(-[rRf]+\s+)+[/~]", command):
        verdicts.append(RuleVerdict(
            "BASH_RM_RF_ROOT", "critical",
            "Recursive delete targeting root or home directory.",
            evidence=command[:200],
            recommendation="Never allow recursive deletion of / or ~/.",
        ))

    # CRITICAL: pipe to shell (curl|bash, wget|sh, etc.)
    if re.search(r"(curl|wget|fetch)\s+[^\|]*\|\s*(ba)?sh", lower):
        verdicts.append(RuleVerdict(
            "BASH_PIPE_TO_SHELL", "critical",
            "Download-then-execute pattern detected.",
            evidence=command[:200],
            recommendation="Download first, inspect, then execute separately.",
        ))

    # HIGH: sudo usage
    if re.search(r"\bsudo\b", lower):
        verdicts.append(RuleVerdict(
            "BASH_SUDO", "high",
            "Elevated privilege execution.",
            evidence=command[:200],
            recommendation="Agents should not need root privileges.",
        ))

    # HIGH: process killing
    if re.search(r"\b(kill\s+-9|killall|pkill)\b", lower):
        verdicts.append(RuleVerdict(
            "BASH_KILL_PROCESS", "high",
            "Process termination command.",
            evidence=command[:200],
            recommendation="Confirm which process and why.",
        ))

    # HIGH: database drop/truncate
    if re.search(r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", lower):
        verdicts.append(RuleVerdict(
            "BASH_DB_DROP", "high",
            "Database destructive operation.",
            evidence=command[:200],
            recommendation="Back up data before destructive DB operations.",
        ))

    # HIGH: environment variable exfiltration
    if re.search(r"(printenv|env\b|set\b).*(\||>)", lower):
        verdicts.append(RuleVerdict(
            "BASH_ENV_LEAK", "high",
            "Environment variables piped or redirected — possible credential exfiltration.",
            evidence=command[:200],
            recommendation="Do not pipe env to external commands.",
        ))

    # MEDIUM: package install without pinning
    if re.search(r"(pip|npm|cargo|go)\s+install\s+(?!.*[=@#])", lower):
        verdicts.append(RuleVerdict(
            "BASH_UNPIN_INSTALL", "medium",
            "Package install without version pinning.",
            evidence=command[:200],
            recommendation="Pin package versions to prevent supply chain attacks.",
        ))

    # MEDIUM: docker with privileged or host network
    if re.search(r"docker\s+run.*--(privileged|net=host|pid=host)", lower):
        verdicts.append(RuleVerdict(
            "BASH_DOCKER_PRIV", "medium",
            "Docker container with elevated permissions.",
            evidence=command[:200],
        ))

    # MEDIUM: chmod 777
    if re.search(r"chmod\s+777", lower):
        verdicts.append(RuleVerdict(
            "BASH_CHMOD_WORLD", "medium",
            "World-writable permissions.",
            evidence=command[:200],
        ))

    # LOW: redirecting stdout to a file (overwrite)
    if re.search(r">\s*/", command) and ">" in command and ">>" not in command.split(">")[0] + ">":
        verdicts.append(RuleVerdict(
            "BASH_OVERWRITE_REDIRECT", "low",
            "File overwrite via redirect.",
            evidence=command[:200],
        ))

    return verdicts


def _check_git_rules(command: str) -> list[RuleVerdict]:
    """Rules for git operations."""
    verdicts: list[RuleVerdict] = []
    lower = command.lower()

    # HIGH: force push
    if re.search(r"git\s+push\s+.*--force(?!-with-lease)", lower):
        verdicts.append(RuleVerdict(
            "GIT_FORCE_PUSH", "high",
            "Force push can overwrite remote history.",
            evidence=command[:200],
            recommendation="Use --force-with-lease for safer force pushes.",
        ))

    # HIGH: hard reset
    if "git" in lower and "reset" in lower and "--hard" in lower:
        verdicts.append(RuleVerdict(
            "GIT_RESET_HARD", "high",
            "Hard reset discards uncommitted changes.",
            evidence=command[:200],
            recommendation="Stash or commit changes before resetting.",
        ))

    # HIGH: checkout with path (overwrites working tree)
    if re.search(r"git\s+checkout\s+(head|origin/|\.)", lower):
        verdicts.append(RuleVerdict(
            "GIT_CHECKOUT_OVERWRITE", "high",
            "Checkout that may overwrite working tree files.",
            evidence=command[:200],
        ))

    # MEDIUM: git clean
    if re.search(r"git\s+clean\s+-[fdxX]", lower):
        verdicts.append(RuleVerdict(
            "GIT_CLEAN", "medium",
            "Git clean removes untracked files.",
            evidence=command[:200],
        ))

    # MEDIUM: branch delete
    if re.search(r"git\s+branch\s+-[dD]", lower):
        verdicts.append(RuleVerdict(
            "GIT_BRANCH_DELETE", "medium",
            "Branch deletion.",
            evidence=command[:200],
        ))

    return verdicts


def _check_file_write_rules(path: str, content: str | None = None) -> list[RuleVerdict]:
    """Rules for file write/edit operations."""
    verdicts: list[RuleVerdict] = []
    lower_path = path.lower().replace("\\", "/")

    # HIGH: writing to credential files
    cred_patterns = (".env", ".pem", ".key", ".pfx", "credentials", "secrets", ".ssh/", "id_rsa")
    for pat in cred_patterns:
        if pat in lower_path:
            verdicts.append(RuleVerdict(
                "FILE_WRITE_CRED", "high",
                f"Writing to credential/key file: {path}",
                evidence=path,
                recommendation="Credential files should not be agent-modified.",
            ))
            break

    # HIGH: writing to CI/CD config
    ci_patterns = (".github/workflows/", ".gitlab-ci", "jenkinsfile", ".circleci/", "azure-pipelines")
    for pat in ci_patterns:
        if pat in lower_path:
            verdicts.append(RuleVerdict(
                "FILE_WRITE_CI", "high",
                f"Writing to CI/CD configuration: {path}",
                evidence=path,
                recommendation="CI/CD changes can deploy code — review carefully.",
            ))
            break

    # MEDIUM: writing to package manifest
    pkg_patterns = ("package.json", "requirements.txt", "pyproject.toml", "cargo.toml", "go.mod", "gemfile", ".csproj")
    for pat in pkg_patterns:
        if lower_path.endswith(pat):
            verdicts.append(RuleVerdict(
                "FILE_WRITE_DEPS", "medium",
                f"Writing to dependency manifest: {path}",
                evidence=path,
                recommendation="Dependency changes can introduce supply chain risks.",
            ))
            break

    # MEDIUM: writing to Docker/infra config
    infra_patterns = ("dockerfile", "docker-compose", "terraform", ".tf", "kubernetes", "helm")
    for pat in infra_patterns:
        if pat in lower_path:
            verdicts.append(RuleVerdict(
                "FILE_WRITE_INFRA", "medium",
                f"Writing to infrastructure config: {path}",
                evidence=path,
            ))
            break

    # Content-based checks
    if content:
        # HIGH: hardcoded secrets in content
        if re.search(r'(?i)(password|secret|api_key|token)\s*[:=]\s*["\'][^"\']{8,}', content):
            verdicts.append(RuleVerdict(
                "FILE_HARDCODED_SECRET", "high",
                "Hardcoded secret detected in file content.",
                recommendation="Use environment variables or secret management.",
            ))

        # MEDIUM: eval/exec in code
        if re.search(r'\b(eval|exec|__import__|compile)\s*\(', content):
            verdicts.append(RuleVerdict(
                "FILE_DYNAMIC_EXEC", "medium",
                "Dynamic code execution pattern in written content.",
            ))

    return verdicts


def _check_network_rules(command: str) -> list[RuleVerdict]:
    """Rules for network-related operations."""
    verdicts: list[RuleVerdict] = []
    lower = command.lower()

    # HIGH: cloud CLI mutations
    if re.search(r"(aws|gcloud|az)\s+(s3\s+rm|compute\s+delete|iam|ec2\s+terminate)", lower):
        verdicts.append(RuleVerdict(
            "NET_CLOUD_MUTATION", "high",
            "Cloud provider destructive operation.",
            evidence=command[:200],
            recommendation="Cloud mutations should be reviewed and approved.",
        ))

    # MEDIUM: outbound data transfer
    if re.search(r"(curl|wget|nc|ncat)\s+.*-d\s+", lower):
        verdicts.append(RuleVerdict(
            "NET_DATA_EXFIL", "medium",
            "Outbound data transfer detected.",
            evidence=command[:200],
        ))

    # LOW: DNS lookups
    if re.search(r"\b(dig|nslookup|host)\b", lower):
        verdicts.append(RuleVerdict(
            "NET_DNS_LOOKUP", "low",
            "DNS lookup command.",
            evidence=command[:200],
        ))

    return verdicts
    return verdicts


# ── Built-in destructive patterns (absorbed from access_gate) ──
# These are the defaults that ship with AIDOCS. Config can add more.

_BUILTIN_DANGEROUS: list[dict[str, str]] = [
    # Git destructive
    {"command": "git", "args": "reset --hard", "risk": "high", "reason": "Discards uncommitted work."},
    {"command": "git", "args": "reset --mixed", "risk": "high", "reason": "Unstages changes, can lose work."},
    {"command": "git", "args": "checkout HEAD", "risk": "high", "reason": "Overwrites working tree files."},
    {"command": "git", "args": "checkout .", "risk": "high", "reason": "Discards all local changes."},
    {"command": "git", "args": "switch", "risk": "medium", "reason": "Branch switching is a user decision."},
    {"command": "git", "args": "restore .", "risk": "high", "reason": "Discards all changes."},
    {"command": "git", "args": "restore --staged --worktree", "risk": "high", "reason": "Discards staged and unstaged changes."},
    {"command": "git", "args": "clean -f", "risk": "high", "reason": "Permanently deletes untracked files."},
    {"command": "git", "args": "clean -fd", "risk": "high", "reason": "Permanently deletes untracked files and directories."},
    {"command": "git", "args": "clean -x", "risk": "high", "reason": "Permanently deletes ignored and untracked files."},
    {"command": "git", "args": "push --force", "risk": "high", "reason": "Overwrites remote history."},
    {"command": "git", "args": "push -f ", "risk": "high", "reason": "Overwrites remote history."},
    {"command": "git", "args": "branch -D", "risk": "medium", "reason": "Force-deletes a branch."},
    {"command": "git", "args": "stash drop", "risk": "medium", "reason": "Permanently loses stashed work."},
    {"command": "git", "args": "stash clear", "risk": "high", "reason": "Permanently loses all stashed work."},
    {"command": "git", "args": "rebase -i", "risk": "medium", "reason": "Rewrites commit history."},
    {"command": "git", "args": "rebase --interactive", "risk": "medium", "reason": "Rewrites commit history."},
    {"command": "git", "args": "reflog expire", "risk": "critical", "reason": "Permanently destroys reflog recovery points."},
    # Package manager destructive
    {"command": "npm", "args": "cache clean --force", "risk": "medium", "reason": "Nukes npm cache."},
    {"command": "pip", "args": "uninstall", "risk": "medium", "reason": "Removes installed packages."},
    {"command": "cargo", "args": "clean", "risk": "low", "reason": "Deletes compiled build artifacts."},
    {"command": "dotnet", "args": "clean", "risk": "low", "reason": "Deletes build output."},
    # Docker destructive
    {"command": "docker", "args": "rm -f", "risk": "high", "reason": "Force-kills and removes containers."},
    {"command": "docker", "args": "rmi -f", "risk": "high", "reason": "Force-removes images."},
    {"command": "docker", "args": "system prune", "risk": "high", "reason": "Deletes all unused containers/images/networks."},
    {"command": "docker", "args": "volume rm", "risk": "high", "reason": "Permanently deletes volume data."},
    # Database destructive
    {"command": "*", "args": "drop database", "risk": "critical", "reason": "Permanently destroys database."},
    {"command": "*", "args": "drop table", "risk": "critical", "reason": "Permanently destroys table."},
    {"command": "*", "args": "truncate table", "risk": "high", "reason": "Permanently deletes all table data."},
    # Process termination
    {"command": "kill", "args": "-9", "risk": "high", "reason": "Force-terminates a process."},
    {"command": "killall", "args": "", "risk": "high", "reason": "Terminates processes by name."},
    {"command": "taskkill", "args": "", "risk": "high", "reason": "Terminates Windows processes."},
    # Remote code execution
    {"pattern": "| bash", "risk": "critical", "reason": "Pipes remote content to shell."},
    {"pattern": "| sh", "risk": "critical", "reason": "Pipes remote content to shell."},
    {"pattern": "| powershell", "risk": "critical", "reason": "Pipes remote content to PowerShell."},
    {"pattern": "| pwsh", "risk": "critical", "reason": "Pipes remote content to PowerShell."},
]


# ── Config-driven rules loading ──

_config_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _load_config_dangerous(project_root: Path) -> list[dict[str, str]]:
    """Load [[policies.dangerous]] from aidocs.toml."""
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
        policies = data.get("policies", {})
        if not isinstance(policies, dict):
            return []
        dangerous = policies.get("dangerous", [])
        if not isinstance(dangerous, list):
            return []

        result: list[dict[str, str]] = []
        for entry in dangerous:
            if not isinstance(entry, dict):
                continue
            risk = str(entry.get("risk", "high")).strip().lower()
            reason = str(entry.get("reason", "")).strip()
            if not reason:
                continue
            result.append({
                "command": str(entry.get("command", "*")).strip().lower(),
                "args": str(entry.get("args", "")).strip().lower() if entry.get("args") else "",
                "pattern": str(entry.get("pattern", "")).strip().lower() if entry.get("pattern") else "",
                "risk": risk,
                "reason": reason,
            })
        return result
    except Exception:
        return []


def get_dangerous_rules(project_root: Path | None = None) -> list[dict[str, str]]:
    """Get merged built-in + config dangerous rules. Cached by file mtime."""
    if project_root is None:
        return _BUILTIN_DANGEROUS

    config_path = project_root / "aidocs.toml"
    cache_key = str(project_root)

    try:
        mtime = config_path.stat().st_mtime if config_path.is_file() else 0.0
    except OSError:
        mtime = 0.0

    cached = _config_cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    config_rules = _load_config_dangerous(project_root)
    merged = _BUILTIN_DANGEROUS + config_rules
    _config_cache[cache_key] = (mtime, merged)
    return merged


def _check_dangerous_rules(command: str, project_root: Path | None = None) -> list[RuleVerdict]:
    """Check command against built-in + config-driven dangerous patterns."""
    verdicts: list[RuleVerdict] = []
    cmd_lower = command.strip().lower()
    rules = get_dangerous_rules(project_root)

    for rule in rules:
        pattern = rule.get("pattern", "")
        cmd_match = rule.get("command", "*")
        args_match = rule.get("args", "")
        risk = rule.get("risk", "high")
        reason = rule.get("reason", "Dangerous command blocked.")

        # Pattern-based: just check if pattern appears anywhere in command
        if pattern:
            if pattern in cmd_lower:
                verdicts.append(RuleVerdict(
                    rule_id=f"CFG_{pattern.replace(' ', '_').upper()[:30]}",
                    risk=risk,
                    description=reason,
                    evidence=command[:200],
                    recommendation="Ask the user to run this directly if needed.",
                ))
            continue

        # Command+args based: check if command is present AND args fragment is present
        if cmd_match == "*":
            # Wildcard command — just check args
            if args_match and args_match in cmd_lower:
                verdicts.append(RuleVerdict(
                    rule_id=f"CFG_{args_match.replace(' ', '_').upper()[:30]}",
                    risk=risk,
                    description=reason,
                    evidence=command[:200],
                    recommendation="Ask the user to run this directly if needed.",
                ))
        else:
            # Specific command — both must be present
            if cmd_match in cmd_lower and (not args_match or args_match in cmd_lower):
                verdicts.append(RuleVerdict(
                    rule_id=f"CFG_{cmd_match.upper()}_{args_match.replace(' ', '_').upper()[:20]}",
                    risk=risk,
                    description=reason,
                    evidence=command[:200],
                    recommendation="Ask the user to run this directly if needed.",
                ))

    return verdicts


def clear_cache(project_root: Path | None = None) -> None:
    """Clear config cache."""
    if project_root:
        _config_cache.pop(str(project_root), None)
    else:
        _config_cache.clear()


# ── Public API ──

def evaluate_tool_call(
    tool_name: str,
    tool_input: dict[str, object] | None = None,
    *,
    project_root: Path | None = None,
) -> JudgeResult:
    """Evaluate a tool call against all heuristic rules + config-driven dangerous patterns.

    Returns a JudgeResult with all applicable verdicts.
    Sub-millisecond latency — no I/O (config cached), no LLM calls.
    """
    name = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__playwright__"):
        if name.startswith(prefix):
            name = name[len(prefix):]

    result = JudgeResult(tool_name=name)
    args = tool_input or {}

    # Bash/shell commands
    if name == "bash":
        command = str(args.get("command", ""))
        if command:
            result.verdicts.extend(_check_bash_rules(command))
            result.verdicts.extend(_check_git_rules(command))
            result.verdicts.extend(_check_network_rules(command))
            result.verdicts.extend(_check_dangerous_rules(command, project_root))

    # File write operations
    if name in ("edit", "write", "code_edit_lines", "code_batch_edit",
                 "code_str_replace", "code_batch_str_replace",
                 "code_insert_lines", "code_create_file"):
        path = str(args.get("path", args.get("file_path", "")))
        content = str(args.get("new_content", args.get("content", args.get("new_str", ""))))
        if path:
            result.verdicts.extend(_check_file_write_rules(path, content or None))

    # Batch edits — check each edit's path
    if name in ("code_batch_edit", "code_batch_str_replace"):
        edits = args.get("edits", [])
        if isinstance(edits, list):
            for edit in edits[:20]:
                if isinstance(edit, dict):
                    path = str(edit.get("path", ""))
                    content = str(edit.get("new_content", edit.get("new_str", "")))
                    if path:
                        result.verdicts.extend(_check_file_write_rules(path, content or None))

    # Deduplicate verdicts by rule_id
    seen: set[str] = set()
    unique: list[RuleVerdict] = []
    for v in result.verdicts:
        if v.rule_id not in seen:
            seen.add(v.rule_id)
            unique.append(v)
    result.verdicts = unique

    return result
