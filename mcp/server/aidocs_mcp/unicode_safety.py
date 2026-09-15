"""Unicode-payload defense for agent-ingested content.

Threat class: hidden-Unicode smuggling (U+E0000–E007F tag block; zero-width
marks; bidi overrides). Named surface for this class in coding agents:

- Pillar "Rules File Backdoor" (2025-03) — hidden instructions in .cursorrules /
  copilot-instructions files that models read verbatim as authority.
- Embrace the Red "Scary Agent Skills" (2026-02) — same pattern in skill files.
- Red-team 2026-04-17 on AIDOCS: passthrough via `ai_get_lines` and
  `memory_capture` confirmed as P1.

Policy:
- **Read boundary** (ai_get_lines, ai_bundle, ai_get_symbol_snippet):
  strip silently, surface a `hidden_unicode_stripped` count so the caller
  knows something was cleaned. Silent strip keeps the read useful for the
  90% case where the file is benign (an ambient BOM, a stray ZWSP in a PR
  title) without punishing the agent.
- **Memory boundary** (memory_capture): REJECT with an actionable error.
  Memory is durable authority for future conversations — any hidden payload
  in that lane is a real attack attempt, not a cosmetic artifact, and the
  human should see the rejection.

Character classes covered:
- Tag block: U+E0000 through U+E007F (includes the language tag U+E0001 and
  ASCII tag chars U+E0020–U+E007E used by the Pillar attack).
- Zero-width: U+200B (ZWSP), U+200C (ZWNJ), U+200D (ZWJ), U+FEFF (ZWNBSP/BOM).
- Bidi overrides: U+202A–U+202E (embedding / override), U+2066–U+2069 (isolate).

Deliberately NOT stripped:
- Regular whitespace (SP, TAB, NL).
- Ordinary BOM at offset 0 — left for the file loader to handle as encoding
  (only stripped if it appears mid-content, which is the attack shape).
- Emoji, CJK, combining marks — all legitimate content.
"""

from __future__ import annotations

# Hidden-character codepoints and ranges.
# (Tuples of (start, end) inclusive.)
_HIDDEN_RANGES: tuple[tuple[int, int], ...] = (
    (0xE0000, 0xE007F),  # Tag block (Pillar / hidden-unicode skill attacks)
    (0x202A, 0x202E),  # Bidi embedding / override
    (0x2066, 0x2069),  # Bidi isolate
)

_HIDDEN_SINGLES: frozenset[int] = frozenset(
    {
        0x200B,  # Zero-width space
        0x200C,  # Zero-width non-joiner
        0x200D,  # Zero-width joiner
        0xFEFF,  # Zero-width no-break space / BOM (mid-content only; see strip_hidden_unicode)
    },
)


def _is_hidden(codepoint: int) -> bool:
    if codepoint in _HIDDEN_SINGLES:
        return True
    for start, end in _HIDDEN_RANGES:
        if start <= codepoint <= end:
            return True
    return False


def count_hidden_unicode(text: str) -> int:
    """Return the number of hidden-unicode codepoints in `text`.

    A leading BOM (U+FEFF at offset 0) is NOT counted — that's an encoding
    artifact, not an injection payload. Mid-content BOMs ARE counted.
    """
    if not text:
        return 0
    count = 0
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp == 0xFEFF and i == 0:
            continue
        if _is_hidden(cp):
            count += 1
    return count


def has_hidden_unicode(text: str) -> bool:
    """Fast predicate for callers that only need a boolean."""
    return count_hidden_unicode(text) > 0


def strip_hidden_unicode(text: str) -> tuple[str, int]:
    """Return (cleaned_text, stripped_count).

    Leading BOM is preserved (encoding artifact). Mid-content hidden chars
    are removed.
    """
    if not text:
        return text, 0
    out_chars: list[str] = []
    stripped = 0
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp == 0xFEFF and i == 0:
            out_chars.append(ch)
            continue
        if _is_hidden(cp):
            stripped += 1
            continue
        out_chars.append(ch)
    if stripped == 0:
        return text, 0
    return "".join(out_chars), stripped


# ── Filename-injection defense ──────────────────────────────────────────
#
# Research 2026-04-17 (AIDOCS): filename-as-context-injection is an unnamed
# but real attack class. Adjacent to Clinejection (Willison 2026-03-06) and
# hidden-Unicode-in-skills (Rehberger 2026-02-11). Channel: every discovery
# tool that emits filenames into agent context.
#
# We flag component *tokens* that look like instruction directives. The
# false-positive budget is deliberately tight: "run_" alone is fine (run_tests,
# run_curl_wrapper), but "run_curl_[something]" in combination with an
# attacker-leaning token (exfiltrate, jailbreak, ignore_prior) flags.

import re as _re

# Tokens that, alone, indicate intent to override instructions or exfiltrate.
# These match on component-lowered text with `_` normalized to space.
_INSTRUCTION_TOKENS: tuple[str, ...] = (
    "ignore prior instructions",
    "ignore previous instructions",
    "disregard prior",
    "disregard previous",
    "system prompt override",
    "system prompt",
    "jailbreak",
    "assistant directive",
    "new instructions",
    "exfiltrate",
    "exfil env",
    "run curl attacker",
    "</user>",
    "</system>",
)

# Tag-looking HTML/XML bracket patterns that aren't legitimate filenames.
_TAG_PATTERN = _re.compile(r"</?(?:user|system|assistant|instructions?|prompt)>", _re.IGNORECASE)


def looks_like_filename_injection(path_or_component: str) -> bool:
    """True if the path component contains tokens that look like prompt
    injection directives rather than legitimate identifiers.

    This is the "name of the file is itself the payload" check — it is
    independent of hidden-unicode detection (which is handled by
    `count_hidden_unicode`).
    """
    if not path_or_component:
        return False
    # Normalize: split on path seps and check each component.
    components = _re.split(r"[/\\]", path_or_component)
    for comp in components:
        if not comp:
            continue
        normalized = comp.lower().replace("_", " ").replace("-", " ")
        for token in _INSTRUCTION_TOKENS:
            if token in normalized:
                return True
        if _TAG_PATTERN.search(comp):
            return True
    return False


def sanitize_path_for_agent(path: str) -> tuple[str, int, bool]:
    """Sanitize a path before returning it to the agent.

    Returns (cleaned_path, hidden_unicode_stripped, suspicious).
    - cleaned_path: hidden-unicode chars removed from every component.
    - hidden_unicode_stripped: total hidden chars removed.
    - suspicious: True if the path has hidden unicode OR an instruction-
      shaped component. Callers should attach a `suspicious_filename`
      flag and consider refusing to act on the path without confirmation.
    """
    if not path:
        return path, 0, False
    cleaned, stripped = strip_hidden_unicode(path)
    suspicious = stripped > 0 or looks_like_filename_injection(cleaned)
    return cleaned, stripped, suspicious
