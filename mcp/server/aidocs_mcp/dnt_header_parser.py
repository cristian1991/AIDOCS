"""Parser for structured DNT (Do Not Touch) headers.

DNT headers carry load-bearing context about why a file is protected:
incident history, paired files, forbidden actions, allowed actions,
baseline commit, agent runbook on edit. Operator-named structure
(handoff #62, 2026-04-27):

    // dnt-id:        paper-sheet-render
    // dnt-master:    src/.../FormPdfService.Render.cs
    // dnt-pair:      src/.../LayoutRenderer.Rows.cs
    // dnt-baseline:  commit 06067a4 (pre-deslop)
    // dnt-cost:      days of rework per regression
    // dnt-incident:  "deslop unification" collapsed mode-specific renderers
    // dnt-forbid:    re-unify renderers
    // dnt-forbid:    change flex-wrap on Puppeteer header/footer template
    // dnt-allow:     bug fixes scoped to one renderer with regression test

Header is informational; SQL registry is source of truth for the gate
cascade. Parser extracts fields from disk, sync feeds them to registry.

Multi-line values: lines starting with whitespace continue the previous
field's value (e.g. dnt-incident split across lines). Repeated keys
(dnt-pair, dnt-incident, dnt-forbid, dnt-allow) accumulate as lists.

Comment styles supported (key:value extraction works in all):
- // line comments (C#, JS, TS, etc.)
- # line comments (Python, shell, YAML)
- <!-- ... --> block comments (HTML, XML, Razor — handled by stripping
  the wrappers before line-parsing)
- /* ... */ block comments (C, CSS — handled similarly)
- @* ... *@ Razor comments
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Repeating-list field names (multiple lines collapse into a list).
_LIST_FIELDS = frozenset(
    {
        "dnt-pair",
        "dnt-incident",
        "dnt-forbid",
        "dnt-allow",
    },
)

# Single-value field names. Operator-clarified scope (2026-04-27):
# dnt-on-edit is intentionally NOT a stored field. The DNT banner's
# presence at first read IS the "stop and read" signal — encoding a
# literal runbook string was redundant.
_SCALAR_FIELDS = frozenset(
    {
        "dnt-id",
        "dnt-master",
        "dnt-baseline",
        "dnt-cost",
    },
)

_ALL_FIELDS = _LIST_FIELDS | _SCALAR_FIELDS

# How many lines from the file head to scan. DNT headers should appear
# at the top; scanning past line 200 catches stragglers but bounds cost.
_HEADER_SCAN_LINES = 200

# Comment-prefix patterns. Order matters: longer prefixes first so
# `<!--` is tried before `<` (which it isn't here, but defensive).
_COMMENT_PREFIX_PATTERNS = (
    r"^\s*//\s*",  # C / C# / JS line comment
    r"^\s*#\s*",  # Python / shell line comment
    r"^\s*\*\s*",  # Inside /* ... */ block comment, leading *
    r"^\s*<!--\s*",  # HTML block comment opener
    r"^\s*-->\s*",  # HTML block comment closer (rare in header)
    r"^\s*/\*\s*",  # Block comment opener
    r"^\s*\*/\s*",  # Block comment closer
    r"^\s*@\*\s*",  # Razor comment opener
    r"^\s*\*@\s*",  # Razor comment closer
)


@dataclass
class DntHeader:
    """Parsed structured DNT header."""

    dnt_id: str = ""
    master: str = ""
    pair: list[str] = field(default_factory=list)
    baseline: str = ""
    cost: str = ""
    incident: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    # Original header text (the comment block as found on disk),
    # cached for banner display. Empty when no header was found.
    raw_header_text: str = ""

    @property
    def is_present(self) -> bool:
        """True when at least dnt-id is set — indicates a real DNT header."""
        return bool(self.dnt_id)

    @property
    def role(self) -> str:
        """Master files have their own dnt-master pointing at themselves
        (or no master line). Satellites have dnt-master pointing at a
        different path. Returns 'master' / 'satellite' / 'unknown'.
        """
        if not self.is_present:
            return "unknown"
        if not self.master:
            return "master"
        # Compare normalized: master pointing at self → master role.
        # Caller normalizes paths before this check.
        return "satellite"


def _strip_comment_prefix(line: str) -> str:
    """Remove the leading comment marker so key:value lines are parseable."""
    for pat in _COMMENT_PREFIX_PATTERNS:
        m = re.match(pat, line)
        if m:
            return line[m.end() :]
    return line


def _is_continuation(line: str) -> bool:
    """A continuation line: a comment line that carries actual content
    text but DOESN'T start with `dnt-X:`. Used to glue multi-line
    values together.

    Detection criteria (all must hold):
    - Line was a comment (prefix-strip changed the line)
    - Post-strip content is non-empty after .strip()
    - Doesn't start with a `dnt-X:` key
    - Has at least one alphanumeric char (decoration-only lines like
      `// ════════════` or `*@` close markers don't qualify)
    """
    stripped = _strip_comment_prefix(line)
    if not stripped or not stripped.strip():
        return False
    if re.match(r"^\s*dnt-[a-z-]+\s*:", stripped):
        return False
    if stripped == line:
        return False
    # Decoration-only: no alphanumeric characters → not real content.
    if not re.search(r"[A-Za-z0-9]", stripped.strip()):
        return False
    return True


def parse_dnt_header(text: str) -> DntHeader:
    """Parse a DNT header from the top of `text`. Returns an empty
    DntHeader (is_present=False) when no `dnt-id:` line is found in
    the first _HEADER_SCAN_LINES lines.

    Tolerant of:
    - Mixed comment styles in the same header (rare but possible)
    - Whitespace variations
    - Unknown dnt-* keys (silently ignored — forward-compat)
    - Multi-line values via continuation lines

    Refuses (returns is_present=False):
    - Headers without a dnt-id line (not structured; treat as legacy)
    """
    if not text:
        return DntHeader()

    lines = text.splitlines()[:_HEADER_SCAN_LINES]
    header = DntHeader()
    raw_header_lines: list[str] = []
    in_header = False
    last_field: str = ""
    last_value_idx: int = -1  # index into pair/incident/forbid/allow lists
    last_scalar_field: str = ""

    for line in lines:
        stripped = _strip_comment_prefix(line).rstrip()
        # Field line: `dnt-key: value`
        kv_match = re.match(r"^\s*(dnt-[a-z-]+)\s*:\s*(.*)$", stripped)
        if kv_match:
            key = kv_match.group(1).lower()
            value = kv_match.group(2).strip()
            if key not in _ALL_FIELDS:
                # Unknown dnt-* key — record header line for cache but
                # don't bind to a field.
                if in_header:
                    raw_header_lines.append(line)
                last_field = ""
                continue
            in_header = True
            raw_header_lines.append(line)
            last_field = key
            if key in _LIST_FIELDS:
                if key == "dnt-pair":
                    header.pair.append(value)
                    last_value_idx = len(header.pair) - 1
                elif key == "dnt-incident":
                    header.incident.append(value)
                    last_value_idx = len(header.incident) - 1
                elif key == "dnt-forbid":
                    header.forbid.append(value)
                    last_value_idx = len(header.forbid) - 1
                elif key == "dnt-allow":
                    header.allow.append(value)
                    last_value_idx = len(header.allow) - 1
                last_scalar_field = ""
            else:
                # Scalar field
                if key == "dnt-id":
                    header.dnt_id = value
                elif key == "dnt-master":
                    header.master = value
                elif key == "dnt-baseline":
                    header.baseline = value
                elif key == "dnt-cost":
                    header.cost = value
                last_scalar_field = key
                last_value_idx = -1
            continue

        # Continuation line — append to last field's value if we're in
        # the header. Stop the header when we hit a non-comment, non-
        # continuation line after entering.
        if in_header and _is_continuation(line) and last_field:
            raw_header_lines.append(line)
            cont_text = stripped.strip()
            if last_field in _LIST_FIELDS and last_value_idx >= 0:
                target_list = {
                    "dnt-pair": header.pair,
                    "dnt-incident": header.incident,
                    "dnt-forbid": header.forbid,
                    "dnt-allow": header.allow,
                }[last_field]
                target_list[last_value_idx] = (
                    target_list[last_value_idx] + " " + cont_text
                ).strip()
            elif last_scalar_field == "dnt-id":
                header.dnt_id = (header.dnt_id + " " + cont_text).strip()
            elif last_scalar_field == "dnt-master":
                header.master = (header.master + " " + cont_text).strip()
            elif last_scalar_field == "dnt-baseline":
                header.baseline = (header.baseline + " " + cont_text).strip()
            elif last_scalar_field == "dnt-cost":
                header.cost = (header.cost + " " + cont_text).strip()
            continue

        # Comment-only line inside the header (like a separator
        # `// ════════` or `// ⚠️ DNT`) — keep in raw, don't bind.
        # Detected by checking if the post-strip line is empty or
        # decoration-only.
        if in_header:
            stripped_full = _strip_comment_prefix(line).strip()
            # Decoration: empty post-strip OR all non-alphanumeric
            # (boxes, separators, emoji headers).
            if not stripped_full or not re.search(r"[A-Za-z0-9]", stripped_full):
                raw_header_lines.append(line)
                continue
            # Real content with no key:value shape — header ended.
            break

    header.raw_header_text = "\n".join(raw_header_lines)
    return header


def render_banner_digest(header: DntHeader) -> str:
    """Render a tight digest banner for first-of-session reads. ~10 lines.

    Full header is available via ai_protect mode='get'. Banner
    surfaces the highest-leverage fields: id, master, pair count,
    cost, top forbid, top allow.
    """
    if not header.is_present:
        return ""
    lines: list[str] = [
        "⚠️  DNT FILE — DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST",
        f"   dnt-id:      {header.dnt_id}",
    ]
    if header.master:
        lines.append(f"   dnt-master:  {header.master}")
    if header.pair:
        n_pair = len(header.pair)
        lines.append(
            f"   dnt-pair:    {header.pair[0]}" + (f"  (+{n_pair - 1} more)" if n_pair > 1 else ""),
        )
    if header.cost:
        lines.append(f"   dnt-cost:    {header.cost}")
    if header.forbid:
        lines.append(f"   dnt-forbid:  {header.forbid[0]}")
        if len(header.forbid) > 1:
            lines.append(
                f"                (+{len(header.forbid) - 1} more — see ai_protect mode='get')",
            )
    if header.allow:
        lines.append(f"   dnt-allow:   {header.allow[0]}")
        if len(header.allow) > 1:
            lines.append(
                f"                (+{len(header.allow) - 1} more — see ai_protect mode='get')",
            )
    lines.append(
        "   STOP. Read this header. Cite which dnt-allow line covers "
        "the edit. If none, ask the user before any tool call.",
    )
    return "\n".join(lines)
