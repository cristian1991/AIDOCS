"""Canonical read-evidence model (#88, castle-maintenance Phase 1).

One structured shape for "what did this tool actually show the agent":

    evidence = {
        "path": "src/foo.py",          # project-relative, /-normalized
        "tool": "ai_get_lines",
        "evidence_type": "contiguous_range" | "sparse_lines" | "full_file"
                         | "symbol_range" | "metadata_only",
        "ranges": [[start, end], ...],  # 1-based inclusive; may be []
        "line_numbers": [int, ...],     # explicit sparse lines; may be []
    }

Emitters stamp it into the tool-call event payload (mcp_server middleware);
readers (the edit gate's read-before-edit checks) PREFER it and fall back to
the legacy ``line_range`` string for one release. The builder derives
evidence from tool ARGUMENTS, which matches exactly what the legacy readers
could prove — so flipping the readers onto this model is a zero-behavior
change by construction.

Pure module: no I/O, no store access.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "build_evidence",
    "evidence_covers_range",
    "evidence_is_file_level",
    "evidence_from_payload",
]

# Tools whose output shows body content with line attribution but whose
# exact line coverage is not knowable from ARGUMENTS (symbol/path inputs).
# They carry file-level evidence — same rule the legacy readers applied.
_FULL_FILE_TOOLS = frozenset(
    {
        "ai_get_symbol_snippet",
        "ai_bundle",
        "ai_slop",
        # "wrote it = read it" (#78): a successful edit proves loaded content
        # at FILE level only.
        "ai_str_replace",
        "ai_anchor_replace",
        "ai_edit_lines",
        "ai_insert_lines",
        "ai_batch_str_replace",
        "ai_batch_edit",
        "ai_create_file",
        "ai_replace",
    }
)

# Structure-only readers: symbol index / signatures, never full body.
# File-level fallback eligibility only — never range proof (#13/#58).
_METADATA_TOOLS = frozenset({"ai_get_outline", "ai_get_symbol_info"})


def _norm_path(raw: object) -> str:
    return str(raw or "").replace("\\", "/").lstrip("/")


def build_evidence(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """Derive the evidence stamp for a tool call from its arguments.

    Returns None when the call carries no read evidence (no path, or a tool
    outside the read/edit-evidence families).
    """
    if not isinstance(arguments, dict):
        return None
    tool = (tool_name or "").rsplit("__", 1)[-1].strip()
    path = _norm_path(arguments.get("path"))
    if not path:
        return None

    if tool == "ai_get_lines":
        try:
            start = int(arguments.get("start_line") or 1)
        except (TypeError, ValueError):
            start = 1
        end_raw = arguments.get("end_line")
        count_raw = arguments.get("count")
        end: int | None = None
        try:
            if end_raw is not None:
                end = int(end_raw)
            elif count_raw is not None:
                end = start + max(int(count_raw) - 1, 0)
        except (TypeError, ValueError):
            end = None
        if end is None:
            # Unbounded read from start — the legacy string stamped "start-?"
            # which the legacy parser treated as a single line; preserve that
            # conservative floor.
            end = start
        return {
            "path": path,
            "tool": tool,
            "evidence_type": "contiguous_range",
            "ranges": [[start, max(end, start)]],
            "line_numbers": [],
        }
    if tool in _FULL_FILE_TOOLS:
        return {
            "path": path,
            "tool": tool,
            "evidence_type": "full_file",
            "ranges": [],
            "line_numbers": [],
        }
    if tool in _METADATA_TOOLS:
        return {
            "path": path,
            "tool": tool,
            "evidence_type": "metadata_only",
            "ranges": [],
            "line_numbers": [],
        }
    return None


def evidence_from_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """The structured stamp from an event payload, or a LEGACY-derived one.

    Legacy fallback (kept one release, per #88): payloads carrying only
    ``target_path`` + ``line_range`` ("12-42" / "12-?") are converted to the
    same evidence shape, so readers only ever consume one model.
    """
    if not isinstance(payload, dict):
        return None
    ev = payload.get("evidence")
    if isinstance(ev, dict) and ev.get("path"):
        return ev
    target = _norm_path(payload.get("target_path"))
    if not target:
        return None
    line_range = str(payload.get("line_range") or "")
    if line_range:
        try:
            lo_str, hi_str = line_range.split("-", 1)
            lo = int(lo_str.strip())
            hi_clean = hi_str.strip().rstrip("?")
            hi = int(hi_clean) if hi_clean.isdigit() else lo
        except (ValueError, AttributeError):
            return {
                "path": target,
                "tool": "",
                "evidence_type": "metadata_only",
                "ranges": [],
                "line_numbers": [],
            }
        return {
            "path": target,
            "tool": "",
            "evidence_type": "contiguous_range",
            "ranges": [[lo, max(hi, lo)]],
            "line_numbers": [],
        }
    return {
        "path": target,
        "tool": "",
        "evidence_type": "full_file",
        "ranges": [],
        "line_numbers": [],
    }


def evidence_covers_range(evidence: dict[str, Any] | None, start: int, end: int) -> bool:
    """True iff the evidence PROVES the agent saw lines [start, end].

    Only explicit ranges / line numbers prove range coverage — full_file and
    metadata_only never do (same rule as the legacy exact-range check: body
    seen ≠ specific lines proven).
    """
    if not isinstance(evidence, dict):
        return False
    for pair in evidence.get("ranges") or []:
        try:
            lo, hi = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if lo <= start and hi >= end:
            return True
    lines = set()
    for n in evidence.get("line_numbers") or []:
        try:
            lines.add(int(n))
        except (TypeError, ValueError):
            continue
    return bool(lines) and set(range(start, end + 1)) <= lines


def evidence_is_file_level(evidence: dict[str, Any] | None) -> bool:
    """True iff the evidence shows the agent saw file CONTENT (any shape).

    metadata_only counts for the loose file-level rule — the legacy
    _file_was_read accepted outline/symbol_info — but never for ranges."""
    return isinstance(evidence, dict) and bool(evidence.get("path"))
