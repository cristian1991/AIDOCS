"""Host-agnostic read-memory surfacing service.

This service owns the **policy** layer for memory surfacing on three
events:

1. ``surface_on_read``  — pre-read x-ray goggles. Fires when a file-
   reading tool is about to execute, surfacing anchored memories so
   the agent sees doctrine BEFORE it reads.
2. ``surface_on_edit``  — post-edit downstream goggles. Fires after
   an edit completes, surfacing memories anchored to the edited
   file/symbol so the agent sees downstream callers + DNT pairs.
3. ``surface_on_prompt`` — prompt-time tool & memory hints. Fires on
   user-prompt-submit, surfacing AIDOCS tools the prompt suggests
   plus memory entries relevant to the prompt content.

The **data** layer (``memory_discovery.discover_memory_for_symbol`` /
``discover_relevant_memory`` / ``format_memory_hints``) was already
host-agnostic. This module owns the **policy** layer that was
previously scattered across ``claude_hook.py`` (lines 2883–2931,
3027–3081, 4294–4376) and absent from the OpenCode JS plugin and
the OpenAI Agents adapter.

The return shape (``SurfacingResult``) is host-agnostic: a tuple of
advisory lines + a why-tag. Each host adapter renders those lines
into its own envelope shape (Claude Code → ``hookSpecificOutput
.additionalContext``; OpenCode → ``sessionPromptContext``; OpenAI
Agents → context block; etc.).

Per aidocs-doctrine §XI memory injection triggers, §XIII evidence
model: surfacing is deterministic (no LLM), bounded (caps), and
fails closed-silent (any data-layer error drops the hint, the
calling tool proceeds).
"""

from __future__ import annotations

import concurrent.futures as _cf
import contextvars as _cv
import hashlib
import os
import threading as _threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Once-per-epoch dedup for surfaced memory hints
# ---------------------------------------------------------------------------
#
# A given memory injects ONCE per agent_memory_epoch — the agent retains it
# in host context until compaction rotates the epoch, at which point it
# re-emits. Without this, the x-ray goggles re-surfaced the same anchored
# memory on every single file read. Mirrors the helper-skill / DNT-banner
# contract and reuses the same epoch-keyed store (dnt_banners_shown) with a
# distinct ``memhint:`` marker namespace — no new migration.
#
# Fail-open: an unresolvable epoch (no host session) or any store error
# surfaces the hints unconditionally — better a duplicate than a silent
# drop of doctrine the agent needs.

_MEMHINT_MARKER_PREFIX = "memhint:"


def _detect_host_kind() -> str:
    if os.environ.get("CLAUDE_CODE_VERSION", "").strip():
        return "claude_code"
    if os.environ.get("OPENCODE_VERSION", "").strip():
        return "opencode"
    return "unknown"


def _resolve_surfacing_epoch(
    project_root: Path,
    *,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> str:
    """Current agent_memory_epoch, or "" when no host session resolves
    (caller then surfaces unconditionally — no dedup possible).
    """
    sid = (host_session_id or "").strip()
    if not sid:
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            sid = (current_calling_host_session_id() or "").strip()
        except Exception:
            sid = ""
    if not sid:
        return ""
    kind = (host_kind or "").strip() or _detect_host_kind()
    try:
        from .agent_memory_epoch import current_epoch

        return current_epoch(
            project_root,
            host_kind=kind,
            host_session_id=sid,
        )
    except Exception:
        return ""


def _resolve_managed_session_id(
    project_root: Path,
    host_session_id: str | None = None,
) -> str:
    """Managed session_id for the calling conductor, or "" — used to thread
    session scope into the surfacing-budget config read.
    """
    sid = (host_session_id or "").strip()
    try:
        from .managed_mode_service import ManagedModeService

        if not sid:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            sid = (current_calling_host_session_id() or "").strip()
        mode = ManagedModeService().get_mode(project_root, host_session_id=sid)
        if mode.get("active"):
            return str(mode.get("session_id") or "").strip()
    except Exception:
        pass
    return ""


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _hint_content_signal(project_root: Path, hint: Any) -> str:
    """A real content/version signal for a memory hint, so an edit to the
    memory BODY re-emits within the same epoch even when the heading/why is
    unchanged.

    SQLite-only no-scroll seal (2026-06): the signal derives ONLY from the
    canonical store + the hint itself — it NEVER reads the on-disk ``.MEMORY``
    Markdown bytes. A body edit reaches the runtime through ``memory_capture``
    → ``memory_index`` (the source of truth), so the canonical content signal
    changes and the hint re-emits; the file on disk is not consulted.

    Resolution order:
      1. an explicit content/version attribute on the hint
         (content_hash / checksum / updated_at);
      2. the canonical memory_index entry content (read_entry(path).content);
      3. the hint's own ``why`` (itself surfaced FROM the sqlite row);
      4. an explicit SQL-only degraded sentinel (stable per path) — never a
         disk read.
    """
    for attr in ("content_hash", "checksum", "updated_at"):
        v = getattr(hint, attr, None)
        if v:
            return f"{attr}:{v}"
    path = str(getattr(hint, "path", "") or "")
    if path:
        # Canonical store content (source of truth; changes on body edit).
        try:
            from .memory_sqlite_store import read_entry

            entry = read_entry(project_root, path, include_inactive=True)
            if entry is not None and entry.content:
                return "content:" + _sha16(entry.content)
        except Exception:
            pass
    why = str(getattr(hint, "why", "") or "")
    if why:
        return "why:" + _sha16(why)
    # SQL-only degraded sentinel: the canonical row carried no content and the
    # hint no `why`. Keep dedup deterministic per path WITHOUT touching disk —
    # the no-scroll seal forbids reading the on-disk Markdown at runtime.
    return "sqlonly-degraded:" + _sha16(path)


def _hint_marker(project_root: Path, hint: Any) -> str:
    """Stable dedup key: path + content/version signal, so a body edit
    re-emits within the same epoch (hot-reload).
    """
    path = str(getattr(hint, "path", "") or "")
    return f"{_MEMHINT_MARKER_PREFIX}{path}:{_hint_content_signal(project_root, hint)}"


def _dedup_and_cap_hints(
    project_root: Path,
    hints: list,
    *,
    limit: int | None = None,
    host_kind: str | None = None,
    host_session_id: str | None = None,
) -> list:
    """Once-per-epoch dedup with a display cap, in three explicit steps:

      1. FILTER — drop hints already surfaced this epoch (no marking yet);
      2. CAP    — keep at most ``limit`` hints that will actually display;
      3. MARK   — record ONLY the displayed (capped) hints as shown.

    Hints capped out of view are NOT marked, so they can surface on a later
    call instead of being silently suppressed forever.

    Fail-open: an unresolvable epoch (no host session) or any store error
    returns the (capped) hints unmarked — surfacing never silently
    disappears on a hiccup; ``limit`` still bounds the display.
    """

    def _cap(items: list) -> list:
        return items if (limit is None or limit <= 0) else items[:limit]

    if not hints:
        return list(hints)
    epoch = _resolve_surfacing_epoch(
        project_root,
        host_kind=host_kind,
        host_session_id=host_session_id,
    )
    if not epoch:
        return _cap(list(hints))
    try:
        from .protected_file_registry_store import ProtectedFileRegistryStore

        store = ProtectedFileRegistryStore()
    except Exception:
        return _cap(list(hints))

    # 1. FILTER already-seen (no marking).
    unseen: list = []
    for h in hints:
        try:
            if store.was_banner_shown(
                project_root,
                epoch_id=epoch,
                dnt_id=_hint_marker(project_root, h),
            ):
                continue
        except Exception:
            # Fail-open: surface this hint rather than drop it.
            unseen.append(h)
            continue
        unseen.append(h)

    # 2. CAP to what will actually display.
    visible = _cap(unseen)

    # 3. MARK only the visible hints as shown.
    for h in visible:
        try:
            store.mark_banner_shown(
                project_root,
                epoch_id=epoch,
                dnt_id=_hint_marker(project_root, h),
            )
        except Exception:
            pass
    return visible


# Fail-open time budget for the data-layer discovery call. Surfacing is
# advisory — if memory discovery is slow (large knowledge graph), skip it
# rather than slow every file read. Tunable via tools.memory_surfacing_
# timeout_ms (dashboard); this is the fallback when config is unreachable.
_DEFAULT_SURFACING_BUDGET_MS: int = 500
_SURFACING_MAX_WORKERS: int = 2
_surfacing_executor = _cf.ThreadPoolExecutor(
    max_workers=_SURFACING_MAX_WORKERS,
    thread_name_prefix="mem-surfacing",
)
# Bounded in-flight guard. future.cancel() cannot stop a RUNNING discovery
# (a worker that blew the budget keeps executing), and submitting more work
# would queue unbounded behind the stuck workers. We track in-flight count
# and skip surfacing immediately when all workers are occupied — a stuck
# discovery holds its slot until it actually finishes (done-callback frees
# it), so at most _SURFACING_MAX_WORKERS discoveries run at once and nothing
# queues.
_surfacing_inflight: int = 0
_surfacing_inflight_lock = _threading.Lock()


def _surfacing_budget_seconds(
    project_root: Path,
    session_id: str | None = None,
) -> float:
    """Budget in seconds. 0 means UNLIMITED (no budget); negative/invalid
    falls back to the default. Session-scoped: an active managed session's
    override of tools.memory_surfacing_timeout_ms wins over project/global.
    """
    try:
        from .config import get_setting

        val = get_setting(
            "tools.memory_surfacing_timeout_ms",
            project_root=project_root,
            session_id=session_id or None,
            default=_DEFAULT_SURFACING_BUDGET_MS,
        )
        ms = int(val) if val is not None else _DEFAULT_SURFACING_BUDGET_MS
    except Exception:
        ms = _DEFAULT_SURFACING_BUDGET_MS
    if ms == 0:
        return 0.0  # unlimited — no surfacing budget
    if ms < 0:
        ms = _DEFAULT_SURFACING_BUDGET_MS
    return ms / 1000.0


def _discover_within_budget(
    project_root: Path,
    *,
    symbol_name,
    file_path,
    max_hops: int = 1,
    session_id: str | None = None,
) -> list:
    """Run ``discover_memory_for_symbol`` under a fail-open time budget.

    On timeout: returns ``[]`` (surfacing is skipped, never blocks the
    calling tool). Other exceptions propagate to the caller, which maps
    them to the ``data_layer_error`` empty result. The worker runs inside
    a copied context so discovery's project-root override / CV-based
    routing is preserved.

    Bounded: if both surfacing workers are already occupied, returns ``[]``
    immediately instead of queueing — surfacing is advisory and must never
    pile up behind a stuck discovery.
    """
    from .memory_discovery import discover_memory_for_symbol

    global _surfacing_inflight
    with _surfacing_inflight_lock:
        if _surfacing_inflight >= _SURFACING_MAX_WORKERS:
            return []  # all workers busy — skip immediately, do not queue
        _surfacing_inflight += 1

    def _release(_f) -> None:
        global _surfacing_inflight
        with _surfacing_inflight_lock:
            _surfacing_inflight -= 1

    budget = _surfacing_budget_seconds(project_root, session_id)
    ctx = _cv.copy_context()
    try:
        future = _surfacing_executor.submit(
            ctx.run,
            lambda: discover_memory_for_symbol(
                project_root,
                symbol_name=symbol_name,
                file_path=file_path,
                max_hops=max_hops,
            ),
        )
    except Exception:
        # Submit failed — free the slot we reserved and fail open.
        _release(None)
        return []
    # Free the slot only when the worker actually finishes (a timed-out but
    # still-running discovery keeps its slot until completion).
    future.add_done_callback(_release)
    try:
        # budget <= 0 means unlimited — wait with no deadline.
        return future.result(timeout=budget if budget > 0 else None) or []
    except _cf.TimeoutError:
        future.cancel()  # no-op if already running; slot frees on completion
        return []


# ---------------------------------------------------------------------------
# Tool name vocabulary (closed sets — aidocs-doctrine §VI edit-tool ecology)
# ---------------------------------------------------------------------------


# Tools whose execution reads file content. Pre-read goggles fire for
# these; non-file tools (Bash, ai_session, ai_find, etc.) get no
# x-ray hint to avoid wasting a query.
FILE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "edit",
        "notebookedit",
        "ai_get_symbol_snippet",
        "ai_get_lines",
        "ai_bundle",
        "ai_replace",
        "ai_str_replace",
    },
)

# Tools whose execution mutates file content. Post-edit downstream
# goggles fire for these so the agent sees doctrine attached to the
# just-changed symbol/file.
EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "edit",
        "notebookedit",
        "ai_replace",
        "ai_str_replace",
    },
)

# Caps. Surfacing >3 hints per event spams context; >5 tool
# suggestions overwhelms the agent. These caps are doctrinal —
# aidocs-doctrine §XI "memory injection triggers" / once-per-
# epoch dedup means we choose few and good over many and weak.
MAX_MEMORY_HINTS_PER_EVENT: int = 3
MAX_TOOL_SUGGESTIONS_PER_PROMPT: int = 5

# Severity → display marker. Kept here (not in memory_discovery)
# because rendering is a policy choice, not a data-layer one.
SEVERITY_MARKERS: dict[str, str] = {
    "critical": "[CRITICAL] ",
    "high": "[HIGH] ",
    "normal": "",
    "low": "",
}


# ---------------------------------------------------------------------------
# Result shapes (frozen — hosts may not mutate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfacingResult:
    """Host-agnostic surfacing output.

    ``advisory_lines`` is a tuple of single-line strings. Each line is
    already formatted (markers, prefixes, parentheses). The host
    decides where to put them — Claude Code joins with ``\\n`` and
    drops into ``hookSpecificOutput.additionalContext``; OpenCode
    drops into ``sessionPromptContext``; OpenAI Agents wraps in a
    system message.

    ``why`` identifies which surfacing path produced this result —
    one of ``pretool_xray`` / ``posttool_downstream`` / ``prompt_hints``
    / ``empty``. Used for audit and for parity tests across hosts.

    ``hint_count`` is the count of HINT lines (memory + tool combined).
    Hosts may suppress emission when ``hint_count == 0``.
    """

    advisory_lines: tuple[str, ...] = ()
    why: str = "empty"
    hint_count: int = 0

    @classmethod
    def empty(cls, why: str = "empty") -> SurfacingResult:
        return cls(advisory_lines=(), why=why, hint_count=0)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReadMemorySurfacer:
    """Policy layer for read-memory surfacing.

    Bound to a runtime so it can resolve the hub's stores
    (managed_mode, query_gate, execution_events) without each call
    threading them through. The data layer
    (``memory_discovery.discover_memory_for_symbol`` etc.) is called
    directly — it's already stateless and host-agnostic.

    Stateless apart from the runtime binding. Safe to call from
    multiple host adapter contexts in the same process.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Tool-name normalization (shared by every event)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tool_name(tool_name: str) -> str:
        """Strip MCP prefixes + lowercase. Matches the form used in
        FILE_READ_TOOLS / EDIT_TOOLS. Host-portable: Claude Code
        names look like ``mcp__aidocs__ai_get_lines``, OpenCode like
        ``ai_get_lines`` already; both normalize to the same key.
        """
        if not tool_name:
            return ""
        norm = tool_name.strip().lower()
        for prefix in ("mcp__aidocs__", "mcp__"):
            if norm.startswith(prefix):
                return norm[len(prefix) :]
        return norm

    @staticmethod
    def _extract_target(tool_input: Any) -> tuple[str, str]:
        """Pull (file_path, symbol_name) from a tool_input dict.
        Returns ('', '') when tool_input is malformed or empty.
        """
        if not isinstance(tool_input, dict):
            return ("", "")
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        symbol_name = str(tool_input.get("symbol") or "")
        return (file_path, symbol_name)

    @staticmethod
    def _format_memory_line(hint: Any, *, prefix: str = "") -> str:
        """Format a discover_memory_for_symbol hint into one display
        line: ``🧠 [SEVERITY] why (path)``. Optional prefix slotted
        between the emoji and the marker (e.g. 'downstream: ').
        """
        sev = (getattr(hint, "severity", "") or "normal").lower()
        marker = SEVERITY_MARKERS.get(sev, "")
        why = str(getattr(hint, "why", "") or "")
        path = str(getattr(hint, "path", "") or "")
        body = f"{prefix}{marker}{why} ({path})"
        return f"🧠 {body}".rstrip()

    # ------------------------------------------------------------------
    # Event 1: pre-read x-ray goggles
    # ------------------------------------------------------------------

    def surface_on_read(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        project_root: Path,
        host_kind: str | None = None,
        host_session_id: str | None = None,
    ) -> SurfacingResult:
        """Pre-read goggles. Empty SurfacingResult when:
          - tool is not file-reading, OR
          - tool_input has no file/symbol target, OR
          - the unit has neither anchored memory nor DNT protection, OR
          - every hint was already surfaced this epoch (once-per-epoch).
        Fail-mode: any exception is swallowed and returns empty — the
        calling tool MUST be allowed to proceed even when surfacing fails.
        """
        if self._normalize_tool_name(tool_name) not in FILE_READ_TOOLS:
            return SurfacingResult.empty("not_file_read")
        file_path, symbol_name = self._extract_target(tool_input)
        if not (file_path or symbol_name):
            return SurfacingResult.empty("no_target")
        # DNT protection on the SAME read-goggles rail (Slice 2): a protected
        # unit warns on READ, not just on edit/search. Own once-per-epoch dedup
        # (dnt_banners_shown); resolved independently of memory so a DNT-only
        # file still surfaces. Fail-quiet.
        dnt_banner = ""
        if file_path:
            try:
                from .dnt_banner_injector import maybe_dnt_banner_for_read

                dnt_banner = maybe_dnt_banner_for_read(project_root, file_path) or ""
            except Exception:
                dnt_banner = ""
        # #205 Slice 4: SYMBOL-scoped protection surfaces at symbol precision
        # on the same rail — reading a protected FUNCTION warns even when the
        # file has no file-level DNT family. Fail-quiet, own epoch dedup.
        symbol_notice = ""
        if file_path:
            try:
                from .dnt_banner_injector import maybe_symbol_protection_notice

                symbol_notice = (
                    maybe_symbol_protection_notice(
                        project_root,
                        file_path,
                        symbol=symbol_name or "",
                        host_kind=host_kind,
                        host_session_id=host_session_id,
                    )
                    or ""
                )
            except Exception:
                symbol_notice = ""
        if symbol_notice:
            dnt_banner = (dnt_banner + "\n" + symbol_notice) if dnt_banner else symbol_notice
        # Anchored memory hints. Fail-open time budget: a slow discovery is
        # skipped, never blocks the read (tools.memory_surfacing_timeout_ms).
        try:
            hints = _discover_within_budget(
                project_root,
                symbol_name=symbol_name,
                file_path=file_path,
                max_hops=1,
                session_id=_resolve_managed_session_id(
                    project_root,
                    host_session_id,
                ),
            )
        except Exception:
            hints = []
            discovery_errored = True
        else:
            discovery_errored = False
        had_hints = bool(hints)
        # Once-per-epoch dedup + display cap on the memory hints (DNT keeps its
        # own dedup). Capped-out hints stay unmarked for a later call.
        if hints:
            hints = _dedup_and_cap_hints(
                project_root,
                list(hints),
                limit=MAX_MEMORY_HINTS_PER_EVENT,
                host_kind=host_kind,
                host_session_id=host_session_id,
            )
        mem_lines = tuple(self._format_memory_line(h) for h in (hints or []))
        if not dnt_banner and not mem_lines:
            if discovery_errored:
                return SurfacingResult.empty("data_layer_error")
            if had_hints:
                return SurfacingResult.empty("epoch_deduped")
            return SurfacingResult.empty("no_hints")
        # Protection first (it gates action), then anchored memory.
        lines = ((dnt_banner,) if dnt_banner else ()) + mem_lines
        return SurfacingResult(
            advisory_lines=lines,
            why="pretool_xray",
            hint_count=len(lines),
        )

    # ------------------------------------------------------------------
    # Event 2: post-edit downstream goggles
    # ------------------------------------------------------------------

    def surface_on_edit(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        project_root: Path,
        host_kind: str | None = None,
        host_session_id: str | None = None,
    ) -> SurfacingResult:
        """Post-edit downstream goggles. Same policy as surface_on_read
        but fires only for EDIT_TOOLS and prefixes lines with
        ``downstream: `` so the agent knows these are post-change
        callers/DNT-pairs rather than pre-read context. Shares the
        once-per-epoch memory-hint namespace with surface_on_read so a
        given memory injects once per epoch regardless of trigger.
        """
        if self._normalize_tool_name(tool_name) not in EDIT_TOOLS:
            return SurfacingResult.empty("not_edit")
        file_path, symbol_name = self._extract_target(tool_input)
        if not (file_path or symbol_name):
            return SurfacingResult.empty("no_target")
        try:
            hints = _discover_within_budget(
                project_root,
                symbol_name=symbol_name,
                file_path=file_path,
                max_hops=1,
                session_id=_resolve_managed_session_id(
                    project_root,
                    host_session_id,
                ),
            )
        except Exception:
            return SurfacingResult.empty("data_layer_error")
        if not hints:
            return SurfacingResult.empty("no_hints")
        hints = _dedup_and_cap_hints(
            project_root,
            list(hints),
            limit=MAX_MEMORY_HINTS_PER_EVENT,
            host_kind=host_kind,
            host_session_id=host_session_id,
        )
        if not hints:
            return SurfacingResult.empty("epoch_deduped")
        lines = tuple(self._format_memory_line(h, prefix="downstream: ") for h in hints)
        return SurfacingResult(
            advisory_lines=lines,
            why="posttool_downstream",
            hint_count=len(lines),
        )

    # ------------------------------------------------------------------
    # Event 3: prompt-time tool & memory hints
    # ------------------------------------------------------------------

    def surface_on_prompt(
        self,
        *,
        prompt: str,
        project_root: Path | None,
        action_kind: str | None = None,
        already_used_tools: Iterable[str] | None = None,
        host_kind: str | None = None,
        host_session_id: str | None = None,
    ) -> SurfacingResult:
        """Prompt-time surfacing. Two layers:
          - NLP tool surfacing via ``intent_grant_detector.detect_grant``
            (lemma-based, no LLM). Suppresses tools already used in
            session (caller passes ``already_used_tools``).
            Cap at MAX_TOOL_SUGGESTIONS_PER_PROMPT.
          - Memory hints via ``discover_relevant_memory`` /
            ``format_memory_hints``. One line per relevant memory file.

        When ``project_root`` is None, only tool surfacing runs (no
        memory data layer to consult).

        Callers (host adapters) are expected to compute
        ``already_used_tools`` themselves — it's a cheap read of
        runtime state and threading it in keeps this service
        stateless on session identity. Hosts that don't need dedup
        pass ``None`` and get the full surface.
        """
        if not prompt or not prompt.strip():
            return SurfacingResult.empty("empty_prompt")

        # `! aidocs admin clear-freeze` mis-strike compensation (fix 2 of the
        # 2026-07-16 bug; operator chose BOTH fixes): a user-typed `!` command
        # is indistinguishable from agent shell INSIDE the CLI process, so an
        # unauthenticated clear records an agent self-cancel strike. The
        # distinguishing signal exists only here: the command's output arrives
        # in THIS user prompt as a local-command block (unforgeable by agent
        # shell, which produces tool results, never user messages). Seeing the
        # receipt, void the mis-attributed strike. Fail-quiet always.
        if project_root is not None:
            try:
                from .security_violation_service import SecurityViolationService

                if SecurityViolationService.prompt_shows_local_clear_freeze(prompt):
                    _m = self.runtime.hub.managed_mode.get_mode(project_root)
                    _sid = str(_m.get("session_id") or "") if _m.get("active") else ""
                    if _sid:
                        SecurityViolationService(
                            self.runtime.hub
                        ).void_self_cancel_after_local_clear(
                            project_root,
                            session_id=_sid,
                            host_session_id=str(host_session_id or ""),
                            host_kind=str(host_kind or ""),
                        )
            except Exception:
                pass

        lines: list[str] = []

        # Tool surfacing — full contract from tool_interface, deduped ONCE per
        # agent_memory_epoch via the rebuilt current_epoch FUNCTION
        # (_resolve_surfacing_epoch). Resets when on_post_compact bumps the
        # epoch, so a freshly-compacted agent re-sees the contract. This is the
        # epoch-keyed banner store (same as memory hints) — NOT the session-
        # sticky used/already sets, which never reset (a tool surfaced once
        # stayed gone for the whole session).
        try:
            from .intent_grant_detector import detect_grant

            granted = sorted(detect_grant(prompt).granted_tools)
        except Exception:
            granted = []
        if granted and project_root is not None:
            # Drop tools already USED this session (a use-record filter, NOT an
            # identity key) — then dedup the rest ONCE per epoch below.
            granted = [t for t in granted if t not in set(already_used_tools or ())]
            epoch = _resolve_surfacing_epoch(
                project_root,
                host_kind=host_kind,
                host_session_id=host_session_id,
            )
            store = None
            if epoch:
                try:
                    from .protected_file_registry_store import ProtectedFileRegistryStore

                    store = ProtectedFileRegistryStore()
                except Exception:
                    store = None
            if store is not None:
                unseen: list[str] = []
                for t in granted:
                    try:
                        if store.was_banner_shown(
                            project_root, epoch_id=epoch, dnt_id=f"tool:{t}"
                        ):
                            continue
                    except Exception:
                        pass
                    unseen.append(t)
                fresh = unseen[:MAX_TOOL_SUGGESTIONS_PER_PROMPT]
            else:
                # No epoch (no host session) -> surface unconditionally, capped.
                fresh = granted[:MAX_TOOL_SUGGESTIONS_PER_PROMPT]
            if fresh:
                try:
                    from .tool_interface import full_contract

                    contracts = [c for c in (full_contract(t) for t in fresh) if c]
                    if contracts:
                        lines.append(
                            "Tools for this request (full contract; shown once "
                            "per epoch, from tool_interface):\n" + "\n".join(contracts)
                        )
                    else:
                        lines.append(", ".join(fresh) + " suggested")
                except Exception:
                    lines.append(", ".join(fresh) + " suggested")
                if store is not None:  # MARK only the surfaced tools, this epoch
                    for t in fresh:
                        try:
                            store.mark_banner_shown(
                                project_root, epoch_id=epoch, dnt_id=f"tool:{t}"
                            )
                        except Exception:
                            pass

        # Memory hints
        if project_root is not None:
            try:
                from .memory_discovery import format_memory_hints
                from .unified_recall import unified_memory_hints

                # Palace handle + context for the semantic-recall lane (best-
                # effort: a palace/ctx hiccup just disables semantic recall, the
                # keyword + lemma lanes still fire).
                _palace = getattr(self.runtime.hub, "palace", None)
                _palace_ctx = None
                if _palace is not None:
                    try:
                        from .palace_hub_extension import build_palace_context

                        _palace_ctx = build_palace_context(
                            self.runtime.hub,
                            self.runtime,
                            tool_name="memory_discovery.semantic_recall",
                        )
                    except Exception:
                        _palace = None
                        _palace_ctx = None
                memory_hints = unified_memory_hints(
                    prompt,
                    project_root,
                    action_kind=action_kind,
                    palace=_palace,
                    hub_ctx=_palace_ctx,
                )
                # Once-per-epoch: a memory surfaced this epoch (via prompt
                # OR read goggles) is not re-surfaced until compaction.
                # discover_relevant_memory already capped to its own limit,
                # and format_memory_hints renders every hint it receives, so
                # all returned hints are displayed → mark them all (limit=None).
                memory_hints = _dedup_and_cap_hints(
                    project_root,
                    list(memory_hints or []),
                    limit=None,
                    host_kind=host_kind,
                    host_session_id=host_session_id,
                )
                memory_line = format_memory_hints(memory_hints)
                if memory_line:
                    lines.append(memory_line)
            except Exception:
                # Memory layer hiccup must never break prompt hints.
                pass

        if not lines:
            return SurfacingResult.empty("no_hints")
        return SurfacingResult(
            advisory_lines=tuple(lines),
            why="prompt_hints",
            hint_count=len(lines),
        )

    # ------------------------------------------------------------------
    # Structured knowledge entry-points for code discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _discovery_contexts(
        result: Any,
        explicit_paths: Iterable[str] = (),
    ) -> list[tuple[str, str]]:
        """Extract bounded (path, symbol) contexts without reading file bodies."""
        contexts: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _add(path: Any, symbol: Any = "") -> None:
            rel = str(path or "").replace("\\", "/").strip()
            sym = str(symbol or "").strip()
            if not rel or rel.startswith(".MEMORY/"):
                return
            key = (rel, sym)
            if key not in seen and len(contexts) < 12:
                seen.add(key)
                contexts.append(key)

        for path in explicit_paths or ():
            _add(path)

        def _walk(node: Any) -> None:
            if len(contexts) >= 12:
                return
            if isinstance(node, dict):
                path = next(
                    (
                        node.get(key)
                        for key in ("path", "file_path", "source_path", "source_file")
                        if node.get(key)
                    ),
                    "",
                )
                symbol = node.get("symbol") or node.get("qualified_symbol") or ""
                _add(path, symbol)
                for key, value in node.items():
                    if key not in {"knowledge_entry_points", "knowledge_source_contract"}:
                        _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(result)
        return contexts

    def decorate_discovery_result(
        self,
        result: Any,
        *,
        query: str,
        project_root: Path,
        explicit_paths: Iterable[str] = (),
    ) -> Any:
        """Attach terse high-confidence memory/KG pointers to a discovery result.

        Code index remains code truth; doctrine remains project law; memory is
        evidence/advice; palace/KG is a rebuildable projection.  Only pointer
        metadata crosses this boundary — never a memory body.  Any uncertainty,
        privacy finding, stale canonical row, or sovereign path drops silently.
        """
        if not isinstance(result, (dict, list)):
            return result
        try:
            from .unified_recall import unified_knowledge_pointers

            contexts = self._discovery_contexts(result, explicit_paths)
            palace = getattr(getattr(self.runtime, "hub", None), "palace", None)
            pointers = unified_knowledge_pointers(
                query=query,
                project_root=project_root,
                contexts=contexts,
                palace=palace,
            )
            if not pointers:
                return result

            contract = {
                "code_index": "code_truth",
                "doctrine": "project_law",
                "memory": "evidence_advice",
                "palace": "rebuildable_projection",
            }
            if isinstance(result, dict):
                result["knowledge_entry_points"] = pointers
                result["knowledge_source_contract"] = contract
            else:
                first = next((item for item in result if isinstance(item, dict)), None)
                if first is not None:
                    first["knowledge_entry_points"] = pointers
                    first["knowledge_source_contract"] = contract
            return result
        except Exception:
            return result

    # ------------------------------------------------------------------
    # Structured-output helper for read tools
    # ------------------------------------------------------------------

    def format_for_read_tool_output(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        project_root: Path,
        prefix: str = "📎 anchored memory:",
    ) -> str:
        """Format a single ``prefix`` + advisory-lines block for
        attachment to a read-tool's structured output.

        Used by ``ai_get_lines`` / ``ai_get_symbol_snippet`` /
        ``ai_bundle`` (and any future read tool) so the agent sees
        the same memory hints whether the tool ran via host hook
        (pre-read goggles in ``ToolGate.evaluate_tool``) or via
        direct MCP call (no hook fires — this method does).

        Returns an empty string when no hints surfaced; tools can
        unconditionally concatenate the return value.
        """
        result = self.surface_on_read(
            tool_name=tool_name,
            tool_input=tool_input,
            project_root=project_root,
        )
        if not result.advisory_lines:
            return ""
        return prefix + "\n" + "\n".join(result.advisory_lines)

    # ------------------------------------------------------------------
    # Helpers for hosts that want runtime-derived dedup state
    # ------------------------------------------------------------------

