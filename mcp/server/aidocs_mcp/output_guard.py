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
    (
        "aws_access_key",
        re.compile(r"(?<![A-Za-z0-9/+=])AKIA[0-9A-Z]{16}(?![A-Za-z0-9/+=])"),
        "AWS Access Key ID",
    ),
    (
        "aws_secret_key",
        # Pure 40-hex strings (Git SHA-1s, hex digests) are identifiers, NOT AWS
        # secret keys — a real key uses the full base64 alphabet, so the odds of one
        # being all-hex are ~16^-40. The negative lookahead skips a pure-hex run of
        # exactly 40 so commit/archive SHAs in release+debug output stay visible,
        # while genuine base64-ish secrets still redact. (WebMCP bug report 6.)
        re.compile(
            r"(?<![A-Za-z0-9/+=])"
            r"(?![A-Fa-f0-9]{40}(?![A-Za-z0-9/+=]))"
            r"[A-Za-z0-9/+=]{40}"
            r"(?![A-Za-z0-9/+=])"
        ),
        "Possible AWS Secret Key",
    ),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{36,255}(?![A-Za-z0-9_])"),
        "GitHub Token",
    ),
    (
        "github_fine_grained",
        re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{22,255}(?![A-Za-z0-9_])"),
        "GitHub Fine-Grained PAT",
    ),
    (
        "generic_api_key",
        re.compile(
            r"""(?i)(?:api[_-]?key|apikey|api[_-]?secret|api[_-]?token)\s*[:=]\s*['"]([A-Za-z0-9\-_./+=]{20,})['"]""",
        ),
        "API Key in assignment",
    ),
    (
        "generic_password",
        re.compile(r"""(?i)(?:password|passwd|pwd)\s*[:=]\s*['"]([^\s'"]{8,})['"]"""),
        "Password in assignment",
    ),
    (
        "generic_secret",
        re.compile(
            r"""(?i)(?:secret|private[_-]?key)\s*[:=]\s*['"]([A-Za-z0-9\-_./+=]{20,})['"]""",
        ),
        "Secret in assignment",
    ),
    ("bearer_token", re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-_./+=]{20,}"), "Bearer Token"),
    ("basic_auth", re.compile(r"(?i)Basic\s+[A-Za-z0-9+/=]{20,}"), "Basic Auth Header"),
    (
        "connection_string",
        re.compile(
            r"(?i)(?:mongodb|postgres(?:ql)?|mysql|redis|amqp|mssql|mariadb|cockroachdb)://[^\s'\"]{10,}",
        ),
        "Database Connection String",
    ),
    (
        "jwt_token",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "JWT Token",
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        "Private Key Block",
    ),
    ("slack_token", re.compile(r"xox[bpoas]-[A-Za-z0-9\-]{10,}"), "Slack Token"),
    ("stripe_key", re.compile(r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"), "Stripe API Key"),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI API Key"),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}"), "Anthropic API Key"),
]

# ── Prompt injection patterns ──

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "system_override",
        re.compile(
            r"(?i)(?:ignore|forget|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|rules)",
        ),
        "Prompt injection: system override attempt",
    ),
    (
        "role_hijack",
        re.compile(
            r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|my)\s+(?:ai|assistant|agent|bot)",
        ),
        "Prompt injection: role hijack attempt",
    ),
    (
        "instruction_inject",
        re.compile(r"(?i)<\s*(?:system|instructions?|prompt)\s*>"),
        "Prompt injection: XML tag instruction injection",
    ),
    (
        "delimiter_escape",
        re.compile(r"(?i)```\s*(?:system|instructions?)\s*\n"),
        "Prompt injection: code block instruction injection",
    ),
    (
        "hidden_instruction",
        re.compile(r"(?i)(?:IMPORTANT|CRITICAL|URGENT)\s*:\s*(?:ignore|override|forget|disregard)"),
        "Prompt injection: hidden instruction",
    ),
]

# ── Sensitive content patterns ──

_SENSITIVE_CONTENT_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "env_file_content",
        re.compile(r"(?m)^[A-Z_]{3,}=\S{8,}$"),
        "Possible .env file content leaked",
    ),
    ("ssh_config", re.compile(r"(?i)(?:Host\s+\S+|IdentityFile\s+~/)"), "SSH config content"),
]


REDACTION_MARKER = "[REDACTED:{category}]"


def _distinct_char_classes(s: str) -> int:
    """How many of {lowercase, uppercase, digit} appear in ``s``.

    A base64-encoded random key (AWS secret = base64 of 30 random bytes)
    carries all three classes with ~99.9% probability. A dictionary/identifier
    run — an all-lowercase doctrine token, a CamelCase symbol — usually carries
    only one or two. Used to keep the format-free ``aws_secret_key`` rule from
    firing on ordinary 40-char word runs.
    """
    has_lower = any(c.islower() for c in s)
    has_upper = any(c.isupper() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return has_lower + has_upper + has_digit


# Shannon-entropy floor for the format-free aws_secret_key rule. Its char class
# includes '/' (valid base64), so a 40-char FILESYSTEM PATH run
# (mcp/templates/webapp/assets/OverviewPage) matches — a real FP class seen in a
# deploy log (2026-07-09). Measured: path runs cap at ~3.76 bits/char (dictionary
# segments + repeats), while random base64 keys floor at ~4.25 over 500 draws.
# 4.0 splits them dead-center — kills the path FPs (max 3.76) with a 0.24 margin
# while keeping every sampled real key (min 4.25) redacted with 0.25 margin.
_AWS_SECRET_MIN_ENTROPY = 4.0


def _shannon_entropy(s: str) -> float:
    """Bits/char Shannon entropy. High for random base64 secrets, low for
    dictionary/path runs (repeated chars + word structure)."""
    if not s:
        return 0.0
    from collections import Counter
    import math as _math

    n = len(s)
    return -sum((c / n) * _math.log2(c / n) for c in Counter(s).values())


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
            # Format-free 40-char base64 rule: a run carrying only ONE of
            # {lower, upper, digit} is a word/identifier run, not a key. Real
            # AWS secret keys mix all three. (Frontier: mixed-case-no-digit
            # CamelCase runs still match at 2 classes; tightening to 3 would
            # risk the ~0.1% of real keys with no digit and the existing
            # non-hex-base64 contract.)
            if category == "aws_secret_key" and _distinct_char_classes(matched_text) < 2:
                continue
            # Entropy floor: the base64 char class includes '/', so a 40-char
            # filesystem PATH run matches the aws_secret_key pattern. Path runs
            # are low-entropy (dictionary segments); real random keys are high.
            # (Deploy-log FP 2026-07-09: assets/OverviewPage-style paths.)
            if (
                category == "aws_secret_key"
                and _shannon_entropy(matched_text) < _AWS_SECRET_MIN_ENTROPY
            ):
                continue
            findings.append(
                GuardFinding(
                    category=f"credential:{category}",
                    severity="critical",
                    detail=description,
                    span=(start, end),
                ),
            )
            if redact:
                redaction_spans.append((start, end, category))

    # Prompt injection scan
    for category, pattern, description in _INJECTION_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            findings.append(
                GuardFinding(
                    category=f"injection:{category}",
                    severity="warning",
                    detail=description,
                    span=(start, end),
                ),
            )

    # Sensitive content scan (only flag, don't redact — too broad)
    for category, pattern, description in _SENSITIVE_CONTENT_PATTERNS:
        matches = list(pattern.finditer(text))
        # Only flag if multiple env-like lines found (single match is likely normal config output)
        if category == "env_file_content" and len(matches) < 3:
            continue
        for match in matches[:3]:  # cap at 3 findings per pattern
            findings.append(
                GuardFinding(
                    category=f"sensitive:{category}",
                    severity="info",
                    detail=description,
                    span=(match.start(), match.end()),
                ),
            )

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


# Text-bearing keys in a structured (dict) tool_response we redact in a
# shape-preserving copy. Anything else (counts, mime types, image bytes,
# page numbers) is left untouched.
_RESPONSE_TEXT_KEYS: tuple[str, ...] = (
    "text",
    "content",
    "output",
    "stdout",
    "stderr",
    "result",
    "data",
    "file",
    "fileContents",
    "body",
)


def redact_tool_response(
    response: object,
    *,
    redact: bool = True,
) -> tuple[object, int, list[str]]:
    """Return a SHAPE-PRESERVING redacted copy of a host tool_response.

    Handles the shapes Claude Code's ``updatedToolOutput`` accepts (which
    must match the original tool output shape):
      - ``str``       → redacted string.
      - ``dict``      → copy with text-bearing fields redacted; nested
                        dicts/lists/strings recursed.
      - ``list``      → element-wise redacted copy.
    Non-text leaves are returned unchanged.

    Returns ``(redacted_response, redaction_count, categories)``. When no
    credential is found the original object is returned (count 0).
    """
    categories: set[str] = set()

    def _walk(node: object) -> tuple[object, int]:
        if isinstance(node, str):
            guard = scan_text(node, redact=redact)
            if guard.redacted_text is not None and guard.redaction_count:
                for f in guard.findings:
                    categories.add(f.category)
                return guard.redacted_text, guard.redaction_count
            # Still record findings even if not redactable, for honesty.
            for f in guard.findings:
                categories.add(f.category)
            return node, 0
        if isinstance(node, dict):
            count = 0
            new: dict = {}
            for k, v in node.items():
                if isinstance(v, (str, dict, list)) and (
                    isinstance(v, (dict, list)) or k in _RESPONSE_TEXT_KEYS
                ):
                    rv, c = _walk(v)
                    new[k] = rv
                    count += c
                else:
                    new[k] = v
            return new, count
        if isinstance(node, list):
            count = 0
            out_list = []
            for item in node:
                rv, c = _walk(item)
                out_list.append(rv)
                count += c
            return out_list, count
        return node, 0

    redacted, total = _walk(response)
    if total == 0:
        return response, 0, sorted(categories)
    return redacted, total, sorted(categories)
