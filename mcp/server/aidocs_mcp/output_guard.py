"""Post-execution output guard — scans tool results before they enter conversation context.

Detects and optionally redacts:
- Credentials (API keys, tokens, passwords, connection strings)
- Prompt injection attempts in tool output
- Encoded payloads (base64 with suspicious content)
- Sensitive file content that leaked through tool results
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class GuardFinding:
    category: str
    severity: str  # "info", "warning", "critical"
    detail: str
    span: tuple[int, int] | None = None  # start, end offsets in text


@dataclass(slots=True)
class GuardResult:
    scanned: bool
    findings: list[GuardFinding] = field(default_factory=list)
    redacted_text: str | None = None
    redaction_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "clean": self.clean,
            "finding_count": len(self.findings),
            "redaction_count": self.redaction_count,
            "findings": [
                {"category": f.category, "severity": f.severity, "detail": f.detail}
                for f in self.findings
            ],
        }


# ── Credential patterns ──

_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9/+=])AKIA[0-9A-Z]{16}(?![A-Za-z0-9/+=])"), "AWS Access Key ID"),
    ("aws_secret_key", re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"), "Possible AWS Secret Key"),
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{36,255}(?![A-Za-z0-9_])"), "GitHub Token"),
    ("github_fine_grained", re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{22,255}(?![A-Za-z0-9_])"), "GitHub Fine-Grained PAT"),
    ("generic_api_key", re.compile(r"""(?i)(?:api[_-]?key|apikey|api[_-]?secret|api[_-]?token)\s*[:=]\s*['"]([A-Za-z0-9\-_./+=]{20,})['"]"""), "API Key in assignment"),
    ("generic_password", re.compile(r"""(?i)(?:password|passwd|pwd)\s*[:=]\s*['"]([^\s'"]{8,})['"]"""), "Password in assignment"),
    ("generic_secret", re.compile(r"""(?i)(?:secret|private[_-]?key)\s*[:=]\s*['"]([A-Za-z0-9\-_./+=]{20,})['"]"""), "Secret in assignment"),
    ("bearer_token", re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-_./+=]{20,}"), "Bearer Token"),
    ("basic_auth", re.compile(r"(?i)Basic\s+[A-Za-z0-9+/=]{20,}"), "Basic Auth Header"),
    ("connection_string", re.compile(r"(?i)(?:mongodb|postgres(?:ql)?|mysql|redis|amqp|mssql|mariadb|cockroachdb)://[^\s'\"]{10,}"), "Database Connection String"),
    ("jwt_token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT Token"),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), "Private Key Block"),
    ("slack_token", re.compile(r"xox[bpoas]-[A-Za-z0-9\-]{10,}"), "Slack Token"),
    ("stripe_key", re.compile(r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"), "Stripe API Key"),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI API Key"),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}"), "Anthropic API Key"),
]

# ── Prompt injection patterns ──

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("system_override", re.compile(r"(?i)(?:ignore|forget|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|rules)"), "Prompt injection: system override attempt"),
    ("role_hijack", re.compile(r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|my)\s+(?:ai|assistant|agent|bot)"), "Prompt injection: role hijack attempt"),
    ("instruction_inject", re.compile(r"(?i)<\s*(?:system|instructions?|prompt)\s*>"), "Prompt injection: XML tag instruction injection"),
    ("delimiter_escape", re.compile(r"(?i)```\s*(?:system|instructions?)\s*\n"), "Prompt injection: code block instruction injection"),
    ("hidden_instruction", re.compile(r"(?i)(?:IMPORTANT|CRITICAL|URGENT)\s*:\s*(?:ignore|override|forget|disregard)"), "Prompt injection: hidden instruction"),
]

# ── Sensitive content patterns ──

_SENSITIVE_CONTENT_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("env_file_content", re.compile(r"(?m)^[A-Z_]{3,}=\S{8,}$"), "Possible .env file content leaked"),
    ("ssh_config", re.compile(r"(?i)(?:Host\s+\S+|IdentityFile\s+~/)"), "SSH config content"),
]


REDACTION_MARKER = "[REDACTED:{category}]"


def scan_text(text: str, *, redact: bool = True) -> GuardResult:
    """Scan a text string for sensitive content. Returns findings and optionally redacted text."""
    if not text or len(text) < 10:
        return GuardResult(scanned=True)

    findings: list[GuardFinding] = []
    redaction_spans: list[tuple[int, int, str]] = []

    # Credential scan
    for category, pattern, description in _CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            # Skip very short matches from group-capturing patterns
            matched_text = match.group(0)
            if len(matched_text) < 12:
                continue
            findings.append(GuardFinding(
                category=f"credential:{category}",
                severity="critical",
                detail=description,
                span=(start, end),
            ))
            if redact:
                redaction_spans.append((start, end, category))

    # Prompt injection scan
    for category, pattern, description in _INJECTION_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            findings.append(GuardFinding(
                category=f"injection:{category}",
                severity="warning",
                detail=description,
                span=(start, end),
            ))

    # Sensitive content scan (only flag, don't redact — too broad)
    for category, pattern, description in _SENSITIVE_CONTENT_PATTERNS:
        matches = list(pattern.finditer(text))
        # Only flag if multiple env-like lines found (single match is likely normal config output)
        if category == "env_file_content" and len(matches) < 3:
            continue
        for match in matches[:3]:  # cap at 3 findings per pattern
            findings.append(GuardFinding(
                category=f"sensitive:{category}",
                severity="info",
                detail=description,
                span=(match.start(), match.end()),
            ))

    # Apply redactions (credentials only)
    redacted_text = None
    redaction_count = 0
    if redact and redaction_spans:
        # Sort by start position descending to replace from end
        sorted_spans = sorted(redaction_spans, key=lambda s: s[0], reverse=True)
        redacted_text = text
        for start, end, cat in sorted_spans:
            marker = REDACTION_MARKER.format(category=cat)
            redacted_text = redacted_text[:start] + marker + redacted_text[end:]
            redaction_count += 1

    return GuardResult(
        scanned=True,
        findings=findings,
        redacted_text=redacted_text,
        redaction_count=redaction_count,
    )


def scan_tool_result(result: object, *, redact: bool = True) -> GuardResult:
    """Scan an MCP tool result object. Extracts text content and scans it."""
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return GuardResult(scanned=False)

    all_findings: list[GuardFinding] = []
    total_redactions = 0
    any_redacted = False

    for item in content:
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        piece = scan_text(text, redact=redact)
        all_findings.extend(piece.findings)
        if piece.redacted_text is not None:
            # Mutate the item's text in-place with redacted version
            try:
                item.text = piece.redacted_text
                any_redacted = True
                total_redactions += piece.redaction_count
            except (AttributeError, TypeError):
                pass

    return GuardResult(
        scanned=True,
        findings=all_findings,
        redacted_text="(applied in-place)" if any_redacted else None,
        redaction_count=total_redactions,
    )
