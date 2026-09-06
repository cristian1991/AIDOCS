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
# Grammar divergence — not a danger category. See _SH_RULES.
CAT_DIALECT = "dialect"


# ── dialect vocabulary (#561 phase 2) ────────────────────────────────
# A DIALECT is the grammar a command will actually be parsed in. It is
# NOT the same thing as ``ShellCommandEnvelope.provider``, which is a
# host-SURFACE tag: shell_envelope deliberately maps sh/zsh/wsl onto
# provider=bash so PreToolUse intercepts them, and that breadth is right
# for interception and wrong for grammar. Phase 2 separates the two so
# the detector set follows the interpreter.
DIALECT_BASH = "bash"
DIALECT_POSIX_SH = "posix_sh"
DIALECT_POWERSHELL = "powershell"
DIALECT_CMD = "cmd"
DIALECT_UNKNOWN = "unknown"

KNOWN_DIALECTS: frozenset[str] = frozenset(
    {
        DIALECT_BASH,
        DIALECT_POSIX_SH,
        DIALECT_POWERSHELL,
        DIALECT_CMD,
        DIALECT_UNKNOWN,
    },
)

# Executable basename (lower-cased, ``.exe`` stripped) → dialect.
_EXECUTABLE_DIALECTS: dict[str, str] = {
    "bash": DIALECT_BASH,
    "sh": DIALECT_POSIX_SH,
    "dash": DIALECT_POSIX_SH,
    "ash": DIALECT_POSIX_SH,
    "powershell": DIALECT_POWERSHELL,
    "pwsh": DIALECT_POWERSHELL,
    "cmd": DIALECT_CMD,
}

# Envelope provider → dialect. Identity for the three providers that
# existed before phase 2, which is what keeps ``evaluate_provider``
# byte-identical for every caller not yet re-keyed onto a dialect.
_PROVIDER_DIALECTS: dict[str, str] = {
    "bash": DIALECT_BASH,
    "powershell": DIALECT_POWERSHELL,
    "cmd": DIALECT_CMD,
}


def normalized_executable_name(path: str) -> str:
    """The comparable basename of an executable path — THE one normaliser.

    Collapses both separator kinds, surrounding whitespace, case, and a trailing
    ``.exe``. It is a shared module-level function rather than an inline
    expression because ``shell_candidate_registry.family_for_basename`` needs the
    IDENTICAL answer, and when it rolled its own with ``Path(...).name`` the two
    drifted: ``Path`` does not treat ``\\`` as a separator on POSIX, so a
    Windows-style path classified as UNKNOWN on the Linux build host while
    passing on a Windows workstation. That divergence lived inside a docstring
    promising "one answer ... instead of two that could drift apart", which is
    exactly the shape this codebase keeps finding — a guarantee asserted in prose
    and contradicted in the branch below it.
    """
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name


def dialect_for_executable(path: str) -> str:
    """Grammar of the binary at ``path``. An unrecognised binary gets
    DIALECT_UNKNOWN and never a guess of bash — a wrong bash tag is
    exactly the audit-integrity shape #561 phase 1 removed.
    """
    return _EXECUTABLE_DIALECTS.get(normalized_executable_name(path), DIALECT_UNKNOWN)


def dialect_for_provider(provider: str) -> str:
    """Grammar implied by an envelope provider tag. This is the fallback
    for envelopes built before phase 2 gave them an explicit dialect.
    """
    return _PROVIDER_DIALECTS.get((provider or "").strip().lower(), DIALECT_UNKNOWN)


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


# ── posix-sh detector (#561 phase 2, finding 3) ──────────────────────
# THIS IS A DIVERGENCE DETECTOR, NOT A DANGER DETECTOR.
#
# A POSIX sh (dash/ash, and /bin/sh on Debian-family boxes) cannot do
# anything bash cannot; the exposure runs the other way. The bash core
# law — bash_policy's grammar plus the heuristic judge — reasons about a
# command in BASH grammar. When the interpreter is a POSIX sh, every
# construct below means something different there, or nothing at all.
# That is a PRECISION gap: the law's reading of the string and the
# shell's reading of the string diverge, so the law's conclusion does
# not transfer. #561 calibrates it as "uncharacterised — NOT a proven
# hole", and these rules are what make it characterisable: they NAME the
# divergence on the verdict.
#
# Every rule is therefore RISK_MEDIUM / CAT_DIALECT, which
# finding_disposition maps to ADVISORY — attached, blocking nothing.
# Escalating any of these to CONFIRM or DENY is a decision on field data
# (#561 phase 4), not something a refactor may help itself to: escalation
# would refuse commands that run today, and this seam is the only way
# agents execute anything.
_SH_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        # `(?!:)` spares the POSIX bracket expression `[[:alpha:]]`.
        r"\[\[(?!:)",
        "sh_double_bracket",
        "[[ ]] conditional — a bash keyword; POSIX sh has only [ ]",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"[<>]\(",
        "sh_process_substitution",
        "process substitution — bash-only",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"\$'",
        "sh_ansi_c_quoting",
        "ANSI-C quoting — bash-only; the escapes stay literal in POSIX sh",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"&>>?",
        "sh_ampersand_redirect",
        "combined stdout/stderr redirect — bash-only; POSIX sh reads it as background",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"\bfunction\s+[a-z_][a-z0-9_]*",
        "sh_function_keyword",
        "`function` keyword — bash-only; POSIX sh spells it name() { .. }",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"\{[^{}\s]*,[^{}\s]*\}",
        "sh_brace_expansion",
        "brace expansion — bash-only; POSIX sh leaves the braces literal",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"\bpipefail\b",
        "sh_pipefail",
        "pipefail option — bash-only; POSIX sh reports only the last exit status",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"(?:^|[;&|(]\s*)source\s+\S",
        "sh_source_builtin",
        "`source` builtin — the bash-only spelling of the POSIX dot command",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        r"\$\{[^}]*(?:\^\^|,,)[^}]*\}",
        "sh_case_modification",
        "case-modifying parameter expansion — bash-only",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
    (
        # `name=$(..)` is POSIX command substitution and must not match.
        r"\b[a-z_][a-z0-9_]*=\(",
        "sh_array_literal",
        "array literal — bash-only; POSIX sh has no arrays",
        RISK_MEDIUM,
        CAT_DIALECT,
    ),
)


def evaluate_posix_sh(command: str) -> list[DialectFinding]:
    """Name the bash-only constructs a POSIX sh will read differently.

    See _SH_RULES: divergence, advisory, never blocking.
    """
    return _scan(command, _SH_RULES)


def evaluate_powershell(command: str) -> list[DialectFinding]:
    return _scan(command, _PS_RULES)


def evaluate_cmd(command: str) -> list[DialectFinding]:
    return _scan(command, _CMD_RULES)


def evaluate_dialect(dialect: str, command: str) -> list[DialectFinding]:
    """Dispatch to the detector set for the GRAMMAR the command will be
    parsed in (#561 phase 2 — "dialect follows the interpreter").

    bash and unknown return [] here. bash law is owned by bash_policy +
    heuristic_judge via the core cascade, not by this module; and an
    unknown grammar gets no findings precisely because a detector written
    for another grammar would be reasoning about a string it cannot read
    — which is the whole defect this phase closes.
    """
    if dialect == DIALECT_POWERSHELL:
        return evaluate_powershell(command)
    if dialect == DIALECT_CMD:
        return evaluate_cmd(command)
    if dialect == DIALECT_POSIX_SH:
        return evaluate_posix_sh(command)
    return []


def evaluate_provider(provider: str, command: str) -> list[DialectFinding]:
    """Provider-keyed entry point, kept for callers that hold a provider
    tag and no dialect. Exactly ``evaluate_dialect`` over the
    provider→dialect map, so pre-phase-2 callers cannot have moved.
    """
    return evaluate_dialect(dialect_for_provider(provider), command)


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
    * everything else → ADVISORY (attached, non-blocking) — which is
      where every CAT_DIALECT divergence finding lands by design.
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
