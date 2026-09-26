"""Blast-radius presenter (#1075 step 4) — ONE channel, bounded size.

The radius used to ride every edit ack as a raw dependent list: 60 paths for a
hub file, vendored pip copies included, repeated on every edit of that file.
Agents learned to route around it. This module decides WHERE an edit's blast
goes and HOW MUCH of it:

  * channel — ``stop`` when the host declares that Stop feedback reaches the
    agent (``host_capabilities.stop_feedback_to_agent``), else ``inline``.
    Never both: on a ``stop`` host the ack carries no dependent list; the
    obligation is already in the blast ledger for Stop to present.
  * size — the inline note is independent of the dependent count: counts,
    at most ``MAX_CALLERS`` production dependents, and one expand call.
    Vendored / scratch / third-party copies are never named, only counted.
  * anchored memories survive on both channels (capped); they are a pointer to
    project knowledge about THIS file, not an obligation.
"""

from __future__ import annotations

from pathlib import Path

CHANNEL_INLINE = "inline"
CHANNEL_STOP = "stop"
MAX_CALLERS = 5
MAX_MEMORIES = 3

_NON_PRODUCTION_PARTS = (
    "scratch",
    "_vendor",
    "vendor",
    "third_party",
    "site-packages",
    "node_modules",
    ".egg-info",
    "__pycache__",
)
_TEST_PARTS = ("tests", "test", "__tests__", "spec")


def classify_path(rel: str) -> str:
    """``production`` | ``test`` | ``other`` (vendored, scratch, generated)."""
    parts = [p.lower() for p in str(rel or "").replace("\\", "/").split("/") if p]
    if any(p in _NON_PRODUCTION_PARTS or p.endswith(".egg-info") for p in parts):
        return "other"
    name = parts[-1] if parts else ""
    if any(p in _TEST_PARTS for p in parts[:-1]) or name.startswith("test_") or ".test." in name or ".spec." in name:
        return "test"
    return "production"


def channel_for_host(host_kind: str | None) -> str:
    try:
        from .host_capabilities import STOP_FEEDBACK_NONE, stop_feedback_to_agent

        mode = stop_feedback_to_agent(host_kind)
    except Exception:
        return CHANNEL_INLINE
    return CHANNEL_INLINE if mode == STOP_FEEDBACK_NONE else CHANNEL_STOP


def resolve_host_kind(project_root: Path | None) -> str:
    try:
        from .agent_memory_epoch import resolve_host_identity

        kind, _sid = resolve_host_identity(
            host_kind=None, host_session_id=None, project_root=project_root
        )
        return str(kind or "")
    except Exception:
        return ""


def compact_note(radius: dict | None, *, channel: str) -> dict | None:
    """The bounded structured note for an edit ack, or ``None`` for nothing."""
    if not radius:
        return None
    target = str(radius.get("target") or "")
    memories = [
        {k: m.get(k) for k in ("memory_path", "title", "symbol") if m.get(k)}
        for m in (radius.get("anchored_memories") or [])[:MAX_MEMORIES]
        if isinstance(m, dict)
    ]
    count = radius.get("dependent_count")
    dependents = [str(d) for d in (radius.get("dependents") or [])]
    buckets: dict[str, list[str]] = {"production": [], "test": [], "other": []}
    for d in dependents:
        buckets[classify_path(d)].append(d)
    note: dict = {"target": target, "channel": channel}
    if radius.get("targets"):
        note["targets"] = radius["targets"]
    if count:
        note["dependent_count"] = count
        note["production"] = len(buckets["production"])
        note["tests"] = len(buckets["test"])
        note["other"] = len(buckets["other"])
        if radius.get("truncated"):
            note["truncated"] = True
        if channel == CHANNEL_INLINE:
            note["callers"] = buckets["production"][:MAX_CALLERS]
            note["expand"] = (
                "ai_bundle(mode='dependency', target=<file>)"
                if radius.get("targets")
                else f"ai_bundle(mode='dependency', target='{target}')"
            )
    if memories:
        note["anchored_memories"] = memories
    if not count and not memories:
        return None
    return note


MAX_TARGETS = 5


def merge_radii(radii: list[dict | None]) -> dict | None:
    """ONE radius for every path one invocation edited (r0b B2: a batch or a
    rename rendered only its LAST path). Dependents are de-duplicated across
    targets and never count a target itself; memories de-duplicated by path."""
    real = [r for r in radii if r]
    if not real:
        return None
    if len(real) == 1:
        return real[0]
    targets: list[str] = []
    for r in real:
        t = str(r.get("target") or "")
        if t and t not in targets:
            targets.append(t)
    deps: list[str] = []
    truncated = False
    for r in real:
        truncated = truncated or bool(r.get("truncated"))
        for d in r.get("dependents") or []:
            d = str(d)
            if d not in deps and d not in targets:
                deps.append(d)
    mems: list[dict] = []
    seen: set[str] = set()
    for r in real:
        for m in r.get("anchored_memories") or []:
            if isinstance(m, dict) and str(m.get("memory_path")) not in seen:
                seen.add(str(m.get("memory_path")))
                mems.append(m)
    shown = ", ".join(targets[:MAX_TARGETS]) + (f" +{len(targets) - MAX_TARGETS}" if len(targets) > MAX_TARGETS else "")
    return {
        "target": shown,
        "targets": len(targets),
        "dependent_count": len(deps),
        "dependents": deps,
        "truncated": truncated,
        "anchored_memories": mems,
    }


def summary_line(note: dict | None) -> str:
    """One ack line. On the stop channel only a pointer, never the list."""
    if not note:
        return ""
    target = note.get("target", "")
    bits: list[str] = []
    count = note.get("dependent_count")
    if count:
        more = "+" if note.get("truncated") else ""
        if note.get("channel") == CHANNEL_STOP:
            bits.append(f"blast: {target} has {count}{more} dependents — owed at Stop")
        else:
            callers = note.get("callers") or []
            extra = note.get("production", 0) - len(callers)
            named = ", ".join(callers) + (f" +{extra}" if extra > 0 else "")
            head = f"blast: {target} → {note.get('production', 0)} production"
            if named:
                head += f" ({named})"
            head += f", {note.get('tests', 0)} tests"
            if note.get("other"):
                head += f", {note['other']} vendored/scratch"
            bits.append(f"{head}{' (truncated)' if more else ''}; check them — expand: {note.get('expand')}")
    mems = note.get("anchored_memories") or []
    if mems:
        bits.append("memories: " + ", ".join(str(m.get("memory_path")) for m in mems))
    return " | ".join(bits)
