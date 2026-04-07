"""Skill scanner — evaluates skill content for security risks.

Scans skill markdown/text content for:
- Content risk: prompt injection, instruction override, obfuscated code
- Supply chain risk: external URLs, download commands, package installs
- Capability risk: file system access, network access, code execution
- Vulnerability risk: known dangerous patterns

Returns a scan result with risk level and findings.
Similar pattern to output_guard but specialized for skill content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class ScanFinding:
    category: str  # "content", "supply_chain", "capability", "vulnerability"
    severity: str  # "low", "medium", "high", "critical"
    description: str
    evidence: str = ""


@dataclass(slots=True)
class ScanResult:
    skill_id: str
    risk_level: str  # "safe", "low", "medium", "high", "critical"
    findings: list[ScanFinding] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return self.risk_level == "safe"

    def summary(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "risk_level": self.risk_level,
            "finding_count": len(self.findings),
            "findings": [
                {
                    "category": f.category,
                    "severity": f.severity,
                    "description": f.description,
                    "evidence": f.evidence,
                }
                for f in self.findings
            ],
        }


# ── Content risk patterns ──

_CONTENT_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    # (pattern, category, severity, description)
    (re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules|prompts)"),
     "content", "critical", "Instruction override attempt"),
    (re.compile(r"(?i)you\s+(?:are|must)\s+now\s+(?:a|an|the)"),
     "content", "high", "Role reassignment attempt"),
    (re.compile(r"(?i)<\s*(?:system|instructions?)\s*>"),
     "content", "high", "XML system tag injection"),
    (re.compile(r"(?i)(?:IMPORTANT|CRITICAL|URGENT)\s*:\s*(?:always|never|must|do not)"),
     "content", "medium", "Forceful instruction pattern"),
    (re.compile(r"(?i)do\s+not\s+(?:tell|inform|mention|reveal)"),
     "content", "medium", "Secrecy instruction"),
    (re.compile(r"\\x[0-9a-fA-F]{2}(?:\\x[0-9a-fA-F]{2}){3,}"),
     "content", "high", "Hex-encoded content (possible obfuscation)"),
    (re.compile(r"(?i)base64\s*(?:decode|encoded|payload)"),
     "content", "medium", "Base64 reference (possible obfuscation)"),
]

# ── Supply chain risk patterns ──

_SUPPLY_CHAIN_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (re.compile(r"(?:curl|wget|fetch)\s+https?://"),
     "supply_chain", "high", "External URL download"),
    (re.compile(r"(?:pip|npm|cargo|go)\s+install\s+"),
     "supply_chain", "medium", "Package installation command"),
    (re.compile(r"(?:npx|uvx|bunx)\s+\S+"),
     "supply_chain", "medium", "Remote package execution"),
    (re.compile(r"https?://(?!(?:github\.com|docs\.|api\.|registry\.))[^\s\"'<>]{20,}"),
     "supply_chain", "low", "External URL reference"),
    (re.compile(r"\|\s*(?:ba)?sh"),
     "supply_chain", "critical", "Pipe to shell execution"),
]

# ── Capability risk patterns ──

_CAPABILITY_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (re.compile(r"(?i)(?:read|write|delete|modify)\s+(?:file|directory|folder)"),
     "capability", "medium", "File system operation declared"),
    (re.compile(r"(?i)(?:execute|run|spawn)\s+(?:command|process|shell|bash)"),
     "capability", "high", "Command execution declared"),
    (re.compile(r"(?i)(?:send|post|upload)\s+(?:data|request|payload)\s+(?:to|via)"),
     "capability", "high", "Outbound data transfer declared"),
    (re.compile(r"(?i)(?:database|db|sql)\s+(?:query|execute|run|modify)"),
     "capability", "medium", "Database operation declared"),
    (re.compile(r"(?i)(?:environment|env)\s+(?:variable|var)"),
     "capability", "low", "Environment variable access"),
]

# ── Vulnerability patterns ──

_VULNERABILITY_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (re.compile(r"(?i)(?:eval|exec|compile)\s*\("),
     "vulnerability", "high", "Dynamic code execution"),
    (re.compile(r"(?i)(?:__import__|importlib|subprocess)"),
     "vulnerability", "medium", "Dynamic import or subprocess"),
    (re.compile(r"(?i)(?:os\.system|os\.popen|os\.exec)"),
     "vulnerability", "high", "OS-level command execution"),
    (re.compile(r"(?i)(?:password|secret|token|api_key)\s*[:=]\s*['\"][^'\"]{8,}"),
     "vulnerability", "high", "Hardcoded credential in skill"),
]


_RISK_ORDER = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def scan_skill(skill_id: str, content: str) -> ScanResult:
    """Scan skill content for security risks.

    Args:
        skill_id: Identifier of the skill being scanned.
        content: The skill's text/markdown content.

    Returns:
        ScanResult with aggregated risk level and individual findings.
    """
    if not content or len(content) < 10:
        return ScanResult(skill_id=skill_id, risk_level="safe")

    findings: list[ScanFinding] = []

    all_patterns = (
        _CONTENT_RISKS
        + _SUPPLY_CHAIN_RISKS
        + _CAPABILITY_RISKS
        + _VULNERABILITY_RISKS
    )

    for pattern, category, severity, description in all_patterns:
        matches = list(pattern.finditer(content))
        if matches:
            # Take first match as evidence
            evidence = matches[0].group(0)[:200]
            findings.append(ScanFinding(
                category=category,
                severity=severity,
                description=description,
                evidence=evidence,
            ))

    if not findings:
        return ScanResult(skill_id=skill_id, risk_level="safe")

    max_severity = max(findings, key=lambda f: _RISK_ORDER.get(f.severity, 0)).severity

    return ScanResult(
        skill_id=skill_id,
        risk_level=max_severity,
        findings=findings,
    )
