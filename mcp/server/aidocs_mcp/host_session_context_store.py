"""Host-session metadata + lean resurface-pointer memory (#620).

No new authority and no new SQLite tables: model/window metadata lives beside
the existing host compaction row, while pointer identifiers reuse the existing
marker ledger. Sovereign permission remains exclusively in ``empire_soul_gate``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_RESOURCE_KINDS = frozenset({"skill", "soul"})
_POINTER_PREFIX = "seat-resource:"
_MAX_POINTERS = 32
_CLAUDE_CONTEXT_FLOOR = 200_000


def infer_context_window(
    *,
    host_kind: str = "",
    model_id: str = "",
    explicit_context_window: object = 0,
) -> int:
    """Return a safe value used only for full-vs-lean seat classification."""
    try:
        explicit = int(explicit_context_window or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    host = str(host_kind or "").strip().lower()
    model = str(model_id or "").strip().lower()
    if host == "claude_code" or model.startswith("claude-"):
        return _CLAUDE_CONTEXT_FLOOR
    return 0


def _pointer_ledger_id(
    project_root: Path,
    *,
    host_session_id: str,
    host_kind: str = "",
) -> str:
    """Use the canonical stable agent-context axis, never a raw epoch alias."""
    hsid = str(host_session_id or "").strip()
    if not hsid:
        return ""
    # #587-A: this WAS a seventh ad-hoc ladder (explicit → profile row → the
    # `_detect_host_kind` shim, which returns the fabricated "unknown"). Every
    # one of those rungs now lives inside `resolve_host_identity`, including the
    # persisted-profile lookup this file pioneered — so this delegates instead of
    # keeping a private copy that could drift. The `_detect_host_kind` shim call
    # is gone with it: it would have handed back "unknown", the exact bucket the
    # fail-closed branch below exists to refuse.
    from .agent_memory_epoch import resolve_host_identity

    resolved_kind = resolve_host_identity(
        host_kind=host_kind,
        host_session_id=hsid,
        project_root=project_root,
    )[0]
    if not resolved_kind:
        # Fail CLOSED, not into a fabricated "unknown" bucket: derive_agent_context_id's
        # id-tree honesty contract forbids an "unknown" host_kind, and bucketing
        # distinct hosts under one id would let pointers cross host boundaries.
        # No resolvable host identity ⇒ no pointer ledger (same "" sentinel as an
        # empty host_session_id above).
        return ""
    from .agent_memory_epoch import derive_agent_context_id

    return derive_agent_context_id(
        host_kind=resolved_kind,
        project_root=project_root,
        host_session_id=hsid,
    )


class HostSessionContextStore:
    """Non-authority profile + pointer facade over existing stores."""

    def record_profile(
        self,
        project_root: Path,
        *,
        host_session_id: str,
        host_kind: str = "",
        model_id: str = "",
        context_window: object = 0,
    ) -> dict[str, Any] | None:
        hsid = str(host_session_id or "").strip()
        if not hsid:
            return None
        from .agent_memory_epoch import record_host_context_profile

        window = infer_context_window(
            host_kind=host_kind,
            model_id=model_id,
            explicit_context_window=context_window,
        )
        result = record_host_context_profile(
            project_root,
            # #587-E: the `or "unknown"` that used to sit here was the write side
            # of the placeholder — it stamped a fabricated kind into the row that
            # `resolve_host_identity` now READS BACK as authority. A caller with
            # no kind records nothing; the resolver then answers an honest "".
            host_kind=str(host_kind or "").strip(),
            host_session_id=hsid,
            model_id=str(model_id or "").strip(),
            context_window=window,
        )
        return dict(result) if result else None

    def get_profile(
        self,
        project_root: Path,
        *,
        host_session_id: str,
        host_kind: str = "",
    ) -> dict[str, Any] | None:
        from .agent_memory_epoch import get_host_context_profile

        result = get_host_context_profile(
            project_root,
            host_session_id=str(host_session_id or "").strip(),
            host_kind=str(host_kind or "").strip(),
        )
        return dict(result) if result else None

    def record_pointer(
        self,
        project_root: Path,
        *,
        host_session_id: str,
        resource_kind: str,
        resource_id: str,
        host_kind: str = "",
    ) -> bool:
        hsid = str(host_session_id or "").strip()
        kind = str(resource_kind or "").strip().lower()
        rid = str(resource_id or "").strip()
        if (
            not hsid
            or kind not in _RESOURCE_KINDS
            or not _RESOURCE_ID.fullmatch(rid)
        ):
            return False
        ledger_id = _pointer_ledger_id(
            project_root,
            host_session_id=hsid,
            host_kind=host_kind,
        )
        if not ledger_id:
            return False
        from .protected_file_registry_store import ProtectedFileRegistryStore

        ProtectedFileRegistryStore().mark_banner_shown(
            project_root,
            epoch_id=ledger_id,
            dnt_id=f"{_POINTER_PREFIX}{kind}:{rid}",
        )
        return True

    def list_pointers(
        self,
        project_root: Path,
        *,
        host_session_id: str,
        host_kind: str = "",
    ) -> tuple[str, ...]:
        hsid = str(host_session_id or "").strip()
        if not hsid:
            return ()
        ledger_id = _pointer_ledger_id(
            project_root,
            host_session_id=hsid,
            host_kind=host_kind,
        )
        if not ledger_id:
            return ()
        from .protected_file_registry_store import ProtectedFileRegistryStore

        markers = ProtectedFileRegistryStore().list_banners_shown(
            project_root,
            epoch_id=ledger_id,
            prefix=_POINTER_PREFIX,
        )
        pointers = tuple(
            marker[len(_POINTER_PREFIX):]
            for marker in markers
            if marker[len(_POINTER_PREFIX):]
        )
        return pointers[:_MAX_POINTERS]
