"""Provider-specific dangerous-form detectors (Batch 1).

PowerShell and cmd are NOT "bash with different spelling". The bash
allow/deny table + heuristic judge do not understand PowerShell cmdlets,
aliases, ``-EncodedCommand``, or cmd's ``certutil``/delegation tricks.
This module adds provider-specific detectors so ``ShellPolicy`` can
hard-deny native PowerShell/cmd dangerous forms BEFORE the (bash-centric)
core law runs as defense-in-depth.

Detectors are intentionally conservative pattern matchers, not full
parsers — a finding flags a form for denial/escalation; absence of a
finding does NOT imply safe (the core law still runs afterward).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# risk levels
RISK_CRITICAL = "critical"
RISK_HIGH = "high"
RISK_MEDIUM = "medium"

# categories
CAT_EGRESS = "egress"
CAT_EXEC = "exec"
CAT_OBFUSCATION = "obfuscation"
CAT_PERSISTENCE = "persistence"
CAT_READ = "read"
CAT_DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class DialectFinding:
    rule_id: str
    description: str
    risk: str
    category: str


# (regex, rule_id, description, risk, category)
_PS_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        r"\b(?:invoke-webrequest|iwr|wget|curl)\b",
        "ps_iwr",
        "Invoke-WebRequest/iwr network fetch",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"\b(?:invoke-restmethod|irm)\b",
        "ps_irm",
        "Invoke-RestMethod/irm network fetch",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"\b(?:invoke-expression|iex)\b",
        "ps_iex",
        "Invoke-Expression dynamic code execution",
        RISK_CRITICAL,
        CAT_EXEC,
    ),
    (
        r"-e(?:nc|ncoded|ncodedcommand)\b",
        "ps_encodedcommand",
        "PowerShell -EncodedCommand obfuscated payload",
        RISK_CRITICAL,
        CAT_OBFUSCATION,
    ),
    (
        r"\bfrombase64string\b",
        "ps_frombase64",
        "FromBase64String payload decoding",
        RISK_CRITICAL,
        CAT_OBFUSCATION,
    ),
    (
        r"\bstart-process\b",
        "ps_start_process",
        "Start-Process spawns a new process",
        RISK_HIGH,
        CAT_EXEC,
    ),
    (
        r"\bnet\.webclient\b",
        "ps_webclient",
        "New-Object Net.WebClient network client",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"\bdownload(?:string|file|data)\b",
        "ps_downloadstring",
        "WebClient DownloadString/DownloadFile egress",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"\bset-executionpolicy\b",
        "ps_set_execpolicy",
        "Set-ExecutionPolicy weakens script-execution guardrails",
        RISK_HIGH,
        CAT_PERSISTENCE,
    ),
    (
        r"\bcertutil\b",
        "ps_certutil",
        "certutil used as a downloader/decoder",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"\bbitsadmin\b",
        "ps_bitsadmin",
        "bitsadmin background transfer (download)",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"(?:^|[\s'\"=(])\\\\[^\\\s]+\\[^\\\s]+",
        "ps_unc_path",
        "UNC path (\\\\host\\share) network read/write",
        RISK_HIGH,
        CAT_EGRESS,
    ),
    (
        r"\b(?:new-itemproperty|set-itemproperty|remove-itemproperty)\b.*hk(?:lm|cu|cr|u|cc):",
        "ps_registry_write",
        "Registry write via *-ItemProperty",
        RISK_HIGH,
        CAT_PERSISTENCE,
    ),
    (r"\breg\s+add\b", "ps_reg_add", "reg add registry write", RISK_HIGH, CAT_PERSISTENCE),
    # Alias indirection (sal / Set-Alias) is an obfuscation vector that
    # masks the cmdlets above — blocking, not advisory (Batch 1 fix).
    (
        r"(?:^|[\s;|(])(?:sal|set-alias)\b",
        "ps_set_alias",
        "sal/Set-Alias alias indirection (obfuscation)",
        RISK_HIGH,
        CAT_OBFUSCATION,
    ),
)

_CMD_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        r"\bcertutil\b",
        "cmd_certutil",
        "certutil used as a downloader/decoder",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (
        r"\b(?:powershell|pwsh)\b",
        "cmd_powershell_delegation",
        "cmd delegates to PowerShell (dialect escape)",
        RISK_CRITICAL,
        CAT_EXEC,
    ),
    (
        r"\bbitsadmin\b",
        "cmd_bitsadmin",
        "bitsadmin background transfer (download)",
        RISK_CRITICAL,
        CAT_EGRESS,
    ),
    (r"\b(?:curl|wget)\b", "cmd_curl_wget", "curl/wget network fetch", RISK_HIGH, CAT_EGRESS),
    (r"\breg\s+add\b", "cmd_reg_add", "reg add registry write", RISK_HIGH, CAT_PERSISTENCE),
    (
        r"\b(?:type|copy)\b[^|>]*>",
        "cmd_redirect_exfil",
        "type/copy with redirection (hides a file read from output guard)",
        RISK_HIGH,
        CAT_READ,
    ),
    (
        r"\bcopy\b\s+\S+\s+\S+",
        "cmd_copy_to_file",
        "copy <src> <dst> (file read that bypasses output guard)",
        RISK_MEDIUM,
        CAT_READ,
    ),
    (r"(?:^|[\s&])start\b", "cmd_start", "start spawns a detached process", RISK_HIGH, CAT_EXEC),
    (r"(?:^|[\s&])call\b", "cmd_call", "call invokes a batch script/label", RISK_MEDIUM, CAT_EXEC),
    (
        r"(?:^|[\s'\"=(])\\\\[^\\\s]+\\[^\\\s]+",
        "cmd_unc_path",
        "UNC path (\\\\host\\share) network read/write",
        RISK_HIGH,
        CAT_EGRESS,
    ),
)


def _scan(command: str, rules) -> list[DialectFinding]:
    text = command or ""
    low = text.lower()
    out: list[DialectFinding] = []
    for pattern, rule_id, desc, risk, cat in rules:
        if re.search(pattern, low, flags=re.IGNORECASE):
            out.append(DialectFinding(rule_id, desc, risk, cat))
    return out


def evaluate_powershell(command: str) -> list[DialectFinding]:
    return _scan(command, _PS_RULES)


def evaluate_cmd(command: str) -> list[DialectFinding]:
    return _scan(command, _CMD_RULES)


def evaluate_provider(provider: str, command: str) -> list[DialectFinding]:
    """Dispatch to the provider-specific detector. bash/unknown return
    [] here — bash law is owned by bash_policy + heuristic_judge via the
    core cascade, not by this dialect module.
    """
    if provider == "powershell":
        return evaluate_powershell(command)
    if provider == "cmd":
        return evaluate_cmd(command)
    return []


# ── disposition: how a finding maps to a ShellPolicy outcome ─────────
DISP_DENY = "deny"
DISP_CONFIRM = "confirm"
DISP_ADVISORY = "advisory"


def finding_disposition(f: DialectFinding) -> str:
    """Map a finding to a policy disposition.

    * read-category findings (type/copy redirection, copy-to-file) →
      CONFIRM: they hide a file read from the output guard, so they must
      not pass silently, but a blanket deny would be too aggressive for
      ordinary file copies.
    * critical/high in any other category → DENY (egress/exec/
      obfuscation/persistence/destructive dangerous forms).
    * medium exec (cmd ``call``) → CONFIRM.
    * everything else → ADVISORY (attached, non-blocking).
    """
    if f.category == CAT_READ:
        return DISP_CONFIRM
    if f.risk in (RISK_CRITICAL, RISK_HIGH):
        return DISP_DENY
    if f.risk == RISK_MEDIUM and f.category == CAT_EXEC:
        return DISP_CONFIRM
    return DISP_ADVISORY


def policy_disposition(
    findings: list[DialectFinding],
) -> tuple[str, DialectFinding | None]:
    """Return the worst (disposition, finding) across all findings.
    DENY beats CONFIRM beats ADVISORY; None when no findings.
    """
    deny = next(
        (f for f in findings if finding_disposition(f) == DISP_DENY),
        None,
    )
    if deny is not None:
        return DISP_DENY, deny
    confirm = next(
        (f for f in findings if finding_disposition(f) == DISP_CONFIRM),
        None,
    )
    if confirm is not None:
        return DISP_CONFIRM, confirm
    return (DISP_ADVISORY, findings[0] if findings else None)


# Back-compat: critical-only helper retained for any external callers.
