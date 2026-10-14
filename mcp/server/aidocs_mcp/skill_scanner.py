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

import hashlib
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
    (
        re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules|prompts)"),
        "content",
        "critical",
        "Instruction override attempt",
    ),
    (
        re.compile(r"(?i)you\s+(?:are|must)\s+now\s+(?:a|an|the)"),
        "content",
        "high",
        "Role reassignment attempt",
    ),
    (
        re.compile(r"(?i)<\s*(?:system|instructions?)\s*>"),
        "content",
        "high",
        "XML system tag injection",
    ),
    (
        re.compile(r"(?i)(?:IMPORTANT|CRITICAL|URGENT)\s*:\s*(?:always|never|must|do not)"),
        "content",
        "medium",
        "Forceful instruction pattern",
    ),
    (
        re.compile(r"(?i)do\s+not\s+(?:tell|inform|mention|reveal)"),
        "content",
        "medium",
        "Secrecy instruction",
    ),
    (
        re.compile(r"\\x[0-9a-fA-F]{2}(?:\\x[0-9a-fA-F]{2}){3,}"),
        "content",
        "high",
        "Hex-encoded content (possible obfuscation)",
    ),
    (
        re.compile(r"(?i)base64\s*(?:decode|encoded|payload)"),
        "content",
        "medium",
        "Base64 reference (possible obfuscation)",
    ),
]

# ── Supply chain risk patterns ──

_SUPPLY_CHAIN_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"(?:curl|wget|fetch)\s+https?://"),
        "supply_chain",
        "high",
        "External URL download",
    ),
    (
        re.compile(r"(?:pip|npm|cargo|go)\s+install\s+"),
        "supply_chain",
        "medium",
        "Package installation command",
    ),
    (
        re.compile(r"(?:npx|uvx|bunx)\s+\S+"),
        "supply_chain",
        "medium",
        "Remote package execution",
    ),
    (
        re.compile(r"https?://(?!(?:github\.com|docs\.|api\.|registry\.))[^\s\"'<>]{20,}"),
        "supply_chain",
        "low",
        "External URL reference",
    ),
    (
        re.compile(r"\|\s*(?:ba)?sh"),
        "supply_chain",
        "critical",
        "Pipe to shell execution",
    ),
]

# ── Capability risk patterns ──

_CAPABILITY_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"(?i)(?:read|write|delete|modify)\s+(?:file|directory|folder)"),
        "capability",
        "medium",
        "File system operation declared",
    ),
    (
        re.compile(r"(?i)(?:execute|run|spawn)\s+(?:command|process|shell|bash)"),
        "capability",
        "high",
        "Command execution declared",
    ),
    (
        re.compile(r"(?i)(?:send|post|upload)\s+(?:data|request|payload)\s+(?:to|via)"),
        "capability",
        "high",
        "Outbound data transfer declared",
    ),
    (
        re.compile(r"(?i)(?:database|db|sql)\s+(?:query|execute|run|modify)"),
        "capability",
        "medium",
        "Database operation declared",
    ),
    (
        re.compile(r"(?i)(?:environment|env)\s+(?:variable|var)"),
        "capability",
        "low",
        "Environment variable access",
    ),
]

# ── Vulnerability patterns ──

_VULNERABILITY_RISKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"(?i)(?:eval|exec|compile)\s*\("),
        "vulnerability",
        "high",
        "Dynamic code execution",
    ),
    (
        re.compile(r"(?i)(?:__import__|importlib|subprocess)"),
        "vulnerability",
        "medium",
        "Dynamic import or subprocess",
    ),
    (
        re.compile(r"(?i)(?:os\.system|os\.popen|os\.exec)"),
        "vulnerability",
        "high",
        "OS-level command execution",
    ),
    (
        re.compile(r"(?i)(?:password|secret|token|api_key)\s*[:=]\s*['\"][^'\"]{8,}"),
        "vulnerability",
        "high",
        "Hardcoded credential in skill",
    ),
]


_RISK_ORDER = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

_SAFE_FILE_OPERATION_CONTEXTS: list[re.Pattern[str]] = [
    re.compile(r"(?i)indexed retrieval"),
    re.compile(r"(?i)indexed query"),
    re.compile(r"(?i)raw file reads"),
    re.compile(r"(?i)broad-read files"),
]


def _match_line(content: str, start: int, end: int) -> str:
    line_start = content.rfind("\n", 0, start) + 1
    line_end = content.find("\n", end)
    if line_end == -1:
        line_end = len(content)
    return content[line_start:line_end].strip()


def _should_suppress_finding(
    *,
    content: str,
    match: re.Match[str],
    category: str,
    description: str,
) -> bool:
    if category != "capability" or description != "File system operation declared":
        return False
    line = _match_line(content, match.start(), match.end())
    if not line:
        return False
    return any(pattern.search(line) for pattern in _SAFE_FILE_OPERATION_CONTEXTS)


_DOCUMENTATION_KINDS = frozenset({"doctrine", "stance"})


_SCAN_CACHE_MAX_ENTRIES = 512
# (content_sha256, normalized_kind) -> (risk_level, immutable finding tuples).
# The VERDICT is cached, never a ScanResult: ScanResult is a mutable dataclass
# holding a mutable findings list, so sharing instances would let one caller
# edit another caller's security verdict.
_SCAN_CACHE: dict[tuple[str, str], tuple[str, tuple[tuple[str, str, str, str], ...]]] = {}
_SCAN_CACHE_HITS = 0
_SCAN_CACHE_MISSES = 0


def scan_skill_cache_stats() -> dict[str, int]:
    """Observability for the memoization (#489). Counters only — no content."""
    return {
        "entries": len(_SCAN_CACHE),
        "max_entries": _SCAN_CACHE_MAX_ENTRIES,
        "hits": _SCAN_CACHE_HITS,
        "misses": _SCAN_CACHE_MISSES,
    }


def scan_skill_cache_clear() -> None:
    """Drop the memoization. For tests and for any caller that wants a forced
    re-scan; correctness never depends on this being called, since the key is
    the content itself."""
    global _SCAN_CACHE_HITS, _SCAN_CACHE_MISSES
    _SCAN_CACHE.clear()
    _SCAN_CACHE_HITS = 0
    _SCAN_CACHE_MISSES = 0


def _cache_key(content: str, kind: str) -> tuple[str, str]:
    digest = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
    return (digest, (kind or "").strip().lower())


def _result_from_verdict(
    skill_id: str,
    verdict: tuple[str, tuple[tuple[str, str, str, str], ...]],
) -> ScanResult:
    """Rebuild a FRESH ScanResult (and fresh ScanFindings) for this caller.

    Fresh objects every time: the cache stores plain tuples, so no caller can
    reach the cached verdict to mutate it, and the result always carries the
    CALLER's skill_id rather than whoever populated the entry.
    """
    risk_level, findings = verdict
    return ScanResult(
        skill_id=skill_id,
        risk_level=risk_level,
        findings=[
            ScanFinding(
                category=category,
                severity=severity,
                description=description,
                evidence=evidence,
            )
            for category, severity, description, evidence in findings
        ],
    )


def scan_skill(
    skill_id: str,
    content: str,
    kind: str = "",
) -> ScanResult:
    """Scan skill content for security risks.

    MEMOIZED on (content, kind) since #489: this is a pure regex pass with no
    I/O, and the warm UserPromptSubmit path called it 210 times per prompt for
    0.941s. Identical content cannot produce a different verdict, so a hit is
    deduplicated work — NOT a skipped scan. Any change to the content is a
    different key and is always re-scanned; `kind` is in the key because
    documentation kinds are deliberately skipped and that must not leak to a
    non-documentation kind with the same bytes.

    Args:
        skill_id: Identifier of the skill being scanned.
        content: The skill's text/markdown content.
        kind: Skill kind from the empire/registry record. Documentation
            kinds (`doctrine`, `stance`) are skipped — they are
            text-only by design and the patterns this scanner looks
            for (subprocess, curl|sh, etc.) WILL appear in their prose
            because that's what they describe. Skipping them avoids
            false-positive findings on the empire's own scrolls.
            Default `""` preserves back-compat for callers that don't
            yet thread the kind through (full scan applied).

    Returns:
        ScanResult with aggregated risk level and individual findings.

    """
    if not content or len(content) < 10:
        return ScanResult(skill_id=skill_id, risk_level="safe")

    # Phoenix 2026-05-07: documentation scrolls describe security
    # patterns by design — they MUST mention "subprocess", "curl|sh",
    # "command execution" etc. to teach the kingdom about them.
    # Pattern-matching them as if they were executable code produces
    # false positives that drown the real signal.
    if kind and kind.strip().lower() in _DOCUMENTATION_KINDS:
        return ScanResult(skill_id=skill_id, risk_level="safe")

    global _SCAN_CACHE_HITS, _SCAN_CACHE_MISSES
    key = _cache_key(content, kind)
    cached = _SCAN_CACHE.get(key)
    if cached is not None:
        _SCAN_CACHE_HITS += 1
        return _result_from_verdict(skill_id, cached)
    _SCAN_CACHE_MISSES += 1

    findings: list[ScanFinding] = []

    all_patterns = _CONTENT_RISKS + _SUPPLY_CHAIN_RISKS + _CAPABILITY_RISKS + _VULNERABILITY_RISKS

    for pattern, category, severity, description in all_patterns:
        matches = list(pattern.finditer(content))
        if matches:
            if _should_suppress_finding(
                content=content,
                match=matches[0],
                category=category,
                description=description,
            ):
                continue
            # Take first match as evidence
            evidence = matches[0].group(0)[:200]
            findings.append(
                ScanFinding(
                    category=category,
                    severity=severity,
                    description=description,
                    evidence=evidence,
                ),
            )

    if not findings:
        _store_verdict(key, "safe", ())
        return ScanResult(skill_id=skill_id, risk_level="safe")

    max_severity = max(findings, key=lambda f: _RISK_ORDER.get(f.severity, 0)).severity

    _store_verdict(
        key,
        max_severity,
        tuple(
            (f.category, f.severity, f.description, f.evidence) for f in findings
        ),
    )
    return ScanResult(
        skill_id=skill_id,
        risk_level=max_severity,
        findings=findings,
    )


def _store_verdict(
    key: tuple[str, str],
    risk_level: str,
    findings: tuple[tuple[str, str, str, str], ...],
) -> None:
    """Record a verdict, evicting oldest-first when full.

    Bounded because a resident broker scans many skills over a long life;
    trading a latency bug for a memory leak is not a fix. Plain FIFO eviction
    (dicts preserve insertion order) — a re-scan after eviction costs one regex
    pass and is always correct, so eviction policy is a performance detail, not
    a correctness one.
    """
    if len(_SCAN_CACHE) >= _SCAN_CACHE_MAX_ENTRIES:
        for oldest in list(_SCAN_CACHE)[: max(1, _SCAN_CACHE_MAX_ENTRIES // 8)]:
            _SCAN_CACHE.pop(oldest, None)
    _SCAN_CACHE[key] = (risk_level, findings)
