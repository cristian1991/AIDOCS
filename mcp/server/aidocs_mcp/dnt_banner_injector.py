"""DNT banner injection — #62 Phase 3.

Read tools call `maybe_dnt_banner_for_read(project_root, path)` (or
`maybe_dnt_banners_for_paths` for multi-file results). The helper:

- Looks up the file in the protected-file registry. None? Returns "".
- Resolves the current agent_memory_epoch (sha256 over host_kind +
  project_root + aidocs_work_session_id + host_session_id +
  compaction_count). Banner dedup keys on epoch, NOT on the legacy
  cli_session_id global. The dnt_banners_shown.cli_session_id column
  is reused: historical column name, but it now stores the derived
  agent_memory_epoch. See protected_file_registry_store.py.
- Checks `was_banner_shown(epoch, dnt_id)`. Already shown this epoch?
  Returns "" — banner is once-per-epoch per family.
- Marks the banner as shown.
- Returns a TERSE one-liner that names the family and points at
  ai_protect mode=get for the full payload. ~30 tokens — notification,
  not eulogy.

Failure modes fail-closed-quietly: any exception returns "" rather
than blowing up the read tool. Phase 1 guarantees on-disk reads
happen even when the registry is empty/down.

When epoch derivation can't resolve a host_session_id (no managed
bind, no host identity in the call chain), banner emits without
dedup — better noisy than silent on a DNT file.
"""

from __future__ import annotations

import os
from pathlib import Path

from .protected_file_registry_store import ProtectedFileRegistryStore


def _format_banner(dnt_id: str, full_record: dict | None = None) -> str:
    """Phoenix 2026-05-08: emit the FULL DNT header in additional
    context — not the old terse one-liner that forced a follow-up
    ai_protect(mode='get') round-trip. The agent now sees forbid/
    allow/pair/cost inline, no extra turn needed. Epoch dedup still
    fires once per family per epoch so this isn't spammy. When
    full_record is missing (find_family_by_path-only call site),
    fall back to the terse one-liner pointing at ai_protect.
    """
    if not full_record:
        return (
            f"⚠️ DNT: {dnt_id} — read-only OK. Before editing call: "
            f"ai_protect(mode='get', dnt_id='{dnt_id}')"
        )
    role = str(full_record.get("dnt_role") or "").strip()
    master = str(full_record.get("master") or "").strip()
    pair_files = list(full_record.get("pair_files") or [])
    forbid = list(full_record.get("forbid_list") or [])
    allow = list(full_record.get("allow_list") or [])
    incidents = list(full_record.get("incidents") or [])
    baseline = str(full_record.get("baseline") or "").strip()
    cost = str(full_record.get("cost") or "").strip()
    raw_header = str(full_record.get("full_header_text") or "").strip()

    lines = [f"⚠️ DNT FAMILY: {dnt_id} (role={role or 'unknown'})"]
    if master:
        lines.append(f"  master: {master}")
    if pair_files:
        lines.append("  pair-files:")
        for p in pair_files[:12]:
            lines.append(f"    - {p}")
        if len(pair_files) > 12:
            lines.append(f"    … {len(pair_files) - 12} more")
    if cost:
        lines.append(f"  cost: {cost}")
    if baseline:
        lines.append(f"  baseline: {baseline}")
    if forbid:
        lines.append("  ❌ forbid:")
        for f in forbid:
            lines.append(f"    - {f}")
    if allow:
        lines.append("  ✓ allow:")
        for a in allow:
            lines.append(f"    - {a}")
    if incidents:
        lines.append("  incidents:")
        for inc in incidents[:3]:
            lines.append(f"    - {inc}")
        if len(incidents) > 3:
            lines.append(f"    … {len(incidents) - 3} more")
    lines.append(
        "  → To edit any pair-file: must read all of them first; "
        "must cite the forbid list (do not violate); operator grant "
        "required for sub-agent edits.",
    )
    if raw_header and raw_header not in "\n".join(lines):
        # Operator's verbatim header — last so structured fields lead.
        lines.append("  ─ verbatim header ─")
        for hl in raw_header.splitlines()[:30]:
            lines.append(f"  {hl}")
    return "\n".join(lines)


def _detect_host_kind() -> str:
    """Best-effort host detection from env. Matches the convention used
    by _real_instrumented_call_tool. Returns 'unknown' when neither
    Claude nor OpenCode is detected.
    """
    if os.environ.get("CLAUDE_CODE_VERSION", "").strip():
        return "claude_code"
    if os.environ.get("OPENCODE_VERSION", "").strip():
        return "opencode"
    return "unknown"


def _resolve_epoch(project_root: Path) -> str:
    """Derive the current agent_memory_epoch for this call. Returns ""
    when host identity can't be resolved (no dedup possible — caller
    must emit unconditionally rather than swallow).

    Per locked spec, epoch keys on agent_context_id (host_kind +
    project_uuid + host_session_id) — NOT on aidocs_session_id which
    includes session_uuid. This is intentional: when the agent
    switches work sessions inside the same conversation its memory
    of "what it has already been told" must not reset. Banners stay
    deduped, read grants stay live, even across session_connect
    swaps. Compaction is the only natural reset point.
    """
    from .mcp_server_runtime_helpers import current_calling_host_session_id

    host_sid = (current_calling_host_session_id() or "").strip()
    if not host_sid:
        return ""
    host_kind = _detect_host_kind()
    from .agent_memory_epoch import current_epoch

    try:
        return current_epoch(
            project_root,
            host_kind=host_kind,
            host_session_id=host_sid,
        )
    except Exception:
        return ""


def maybe_dnt_banner_for_read(project_root: Path, path: str) -> str:
    """Return a banner string for `path`, or "" if no banner should be
    surfaced (not in any DNT family / already shown this epoch /
    lookup failed).
    """
    if not path:
        return ""
    try:
        store = ProtectedFileRegistryStore()
        fam = store.find_family_by_path(project_root, path)
        if fam is None:
            return ""
        dnt_id = fam[0]
        if not dnt_id:
            return ""
        epoch = _resolve_epoch(project_root)
        # No host identity → can't dedup. Emit anyway (better noisy
        # than silent on a DNT file) but skip the mark step.
        if epoch and store.was_banner_shown(
            project_root,
            epoch_id=epoch,
            dnt_id=dnt_id,
        ):
            return ""
        if epoch:
            store.mark_banner_shown(
                project_root,
                epoch_id=epoch,
                dnt_id=dnt_id,
            )
        # Phoenix 2026-05-08: fetch full master record so the banner
        # carries forbid/allow/pair/cost/header inline — no follow-up
        # ai_protect(mode='get') round-trip needed.
        full_record: dict | None = None
        try:
            master = store.get_family_master(project_root, dnt_id)
            if master is not None:
                full_record = {
                    "dnt_role": fam[1] if len(fam) > 1 else "",
                    "master": master.path,
                    "pair_files": list(master.pair_files),
                    "forbid_list": list(master.forbid_list),
                    "allow_list": list(master.allow_list),
                    "incidents": list(master.incidents),
                    "baseline": master.baseline,
                    "cost": master.cost,
                    "full_header_text": master.full_header_text,
                }
        except Exception:
            full_record = None
        return _format_banner(dnt_id, full_record=full_record)
    except Exception:
        return ""


def maybe_dnt_banners_for_paths(
    project_root: Path,
    paths: list[str],
) -> list[str]:
    """Multi-file variant. Walks paths, dedupes by family, returns at
    most one banner per family in encounter order. Each emitted family
    is marked as shown for the current epoch.

    Used by tools that surface content from multiple files in a single
    call (ai_search, ai_text_search, ai_investigate, ai_trace).
    """
    if not paths:
        return []
    try:
        store = ProtectedFileRegistryStore()
        epoch = _resolve_epoch(project_root)
        seen_in_call: set[str] = set()
        banners: list[str] = []
        for p in paths:
            if not p:
                continue
            fam = store.find_family_by_path(project_root, p)
            if fam is None:
                continue
            dnt_id = fam[0]
            if not dnt_id or dnt_id in seen_in_call:
                continue
            seen_in_call.add(dnt_id)
            if epoch and store.was_banner_shown(
                project_root,
                epoch_id=epoch,
                dnt_id=dnt_id,
            ):
                continue
            if epoch:
                store.mark_banner_shown(
                    project_root,
                    epoch_id=epoch,
                    dnt_id=dnt_id,
                )
            # Phoenix 2026-05-08: full-record inline (no round-trip).
            full_record: dict | None = None
            try:
                master = store.get_family_master(project_root, dnt_id)
                if master is not None:
                    full_record = {
                        "dnt_role": fam[1] if len(fam) > 1 else "",
                        "master": master.path,
                        "pair_files": list(master.pair_files),
                        "forbid_list": list(master.forbid_list),
                        "allow_list": list(master.allow_list),
                        "incidents": list(master.incidents),
                        "baseline": master.baseline,
                        "cost": master.cost,
                        "full_header_text": master.full_header_text,
                    }
            except Exception:
                full_record = None
            banners.append(_format_banner(dnt_id, full_record=full_record))
        return banners
    except Exception:
        return []


def maybe_symbol_protection_notice(
    project_root: Path,
    path: str,
    *,
    symbol: str = "",
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> str:
    """#205 symbol-granular protection notice for a {file, symbol} read.

    Surfaces when the registry carries a SYMBOL-scoped row covering this
    unit (whole-file rows stay the DNT/sentinel banner's job). Once-per-
    epoch dedup mirrors the DNT-banner contract (marker namespace
    ``symprot:``); no resolvable epoch -> emit unconditionally (fail-open:
    better a duplicate warning than a silent protected-function edit).
    Fail-quiet: any error returns "".
    """
    if not path:
        return ""
    try:
        store = ProtectedFileRegistryStore()
        rec = store.protection_for_unit(project_root, path, symbol=symbol)
        if rec is None or not rec.symbol:
            return ""
        if host_session_id:
            try:
                from .agent_memory_epoch import current_epoch

                epoch = current_epoch(
                    project_root,
                    host_kind=(host_kind or "").strip() or _detect_host_kind(),
                    host_session_id=host_session_id,
                )
            except Exception:
                epoch = ""
        else:
            epoch = _resolve_epoch(project_root)
        marker = f"symprot:{rec.path}#{rec.symbol}"
        if epoch and store.was_banner_shown(project_root, epoch_id=epoch, dnt_id=marker):
            return ""
        if epoch:
            store.mark_banner_shown(project_root, epoch_id=epoch, dnt_id=marker)
        why = f" — {rec.why}" if rec.why else ""
        return (
            f"⚠️ PROTECTED FUNCTION: {rec.symbol} in {rec.path}{why}. "
            "This symbol is registry-protected (ai_protect, symbol scope); "
            "do not modify it without the protector's grant."
        )
    except Exception:
        return ""
