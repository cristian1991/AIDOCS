from __future__ import annotations

import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from .aidocs_managed_store import AidocsManagedStore


class SessionNotMemberError(RuntimeError):
    """Raised when a bind/restamp targets a session that is not a member of
    the project (SQL ``session_membership``), after a bounded self-heal had
    its chance. Managed mode must never report ACTIVE for a session that
    ``require_session`` would refuse — binding such a session is the
    split-brain this guard closes. Fail-closed: callers surface the refusal
    instead of persisting a ghost bind.
    """

    def __init__(self, project_root: Path, session_id: str) -> None:
        self.project_root = project_root
        self.session_id = session_id
        super().__init__(
            f"session '{session_id}' is not a member of project "
            f"{project_root} (managed-mode bind refused; session-membership "
            f"is the sole authority — run `aidocs migrate-control-authority` "
            f"if this is an unmigrated legacy session)",
        )


# Per-process boot token. Generated ONCE at module import, never
# rotates for the life of this MCP server process. When the server
# restarts, this string is brand new and no longer matches whatever
# aidocs_managed.bound_by_boot_token has on disk — which is how the
# gate detects "fresh server needs reconnect." Format: pid + boot
# epoch + random suffix so even a pid-reuse collision can't match.
_MCP_SERVER_BOOT_TOKEN: str = f"mcp-{os.getpid()}-{int(time.time())}-{secrets.token_hex(4)}"


def current_boot_token() -> str:
    """Per-process MCP server boot token. Stable for this process,
    unique across restarts. Exposed for tests + the gate check.
    """
    return _MCP_SERVER_BOOT_TOKEN


def _is_dev_mode(project_root: Path) -> bool:
    """Return whether this project is a DEV-flavour (contributor) build.

    Authority change (2026-06-12): the `dev.dev_mode` config toggle is
    removed; the "dev" security profile is now derived from the DEV
    distribution flavour alone. Centralized so tests can monkeypatch this
    single function rather than the whole config resolver.
    """
    try:
        from .enforcement import is_dev_flavor

        return bool(is_dev_flavor(project_root))
    except Exception:
        return False


class ManagedModeService:
    """Project-local managed-mode state for normal prompt routing.

    Storage moved to sqlite in Beat 3 (``AidocsManagedStore``). Service
    keeps its original public shape; callers didn't have to change. The
    legacy ``.MEMORY/config/aidocs-managed.json`` ingest + delete is
    handled by the store's init path.
    """

    def __init__(self) -> None:
        self._store = AidocsManagedStore()

    def config_path(self, project_root: Path) -> Path:
        # Legacy path kept advisory so existing diagnostics UIs that
        # show "managed-mode file location" keep displaying something
        # sensible. Actual storage lives in aidocs.sqlite3 post-Beat 3.
        return project_root / ".MEMORY" / "config" / "aidocs-managed.json"

    def get_mode(
        self,
        project_root: Path,
        *,
        host_session_id: str = "",
    ) -> dict[str, object]:
        """Resolve managed-mode state, honoring conductor identity.

        #58 (canonical 2026-04-26; renamed 2026-05-01): when
        host_session_id is provided and a per-conductor mapping exists
        for it, that mapping is the source of truth — singleton is
        bypassed entirely. Singleton path runs only when
        host_session_id is unavailable OR when no per-conductor row
        exists for the calling conductor.

        `host_session_id` (canonical, agent_memory_epoch.py contract) is
        the per-conductor identity. The deprecated `cli_session_id` kwarg
        alias was removed (identity-spine 2026-06-29).

        See security-gates.md §0.5 #50/#54 sub-clause "conductor-bound
        state keying" for the resolution-precedence contract.
        """
        self._store.init_db(project_root)
        sid = (host_session_id or "").strip()
        # #58 conductor-aware resolution: check per-conductor mapping
        # first when identity is available. This path is the
        # authoritative one going forward.
        if sid:
            per = self._store.get_per_conductor(
                project_root,
                cli_session_id=sid,
            )
            if per is not None:
                state: dict[str, object] = {
                    "active": True,
                    "session_id": per.get("session_id"),
                    "source": per.get("source"),
                    "activated_at": per.get("activated_at"),
                    "last_updated": per.get("last_updated"),
                    "bound_by_boot_token": per.get("bound_by_boot_token"),
                    "cli_session_id": per.get("cli_session_id"),
                    "resolved_via": "per_conductor",
                }
                state["path"] = str(self.config_path(project_root))
                state["security_profile"] = "dev" if _is_dev_mode(project_root) else "release"
                state["current_boot_token"] = _MCP_SERVER_BOOT_TOKEN
                state["requires_reconnect"] = False
                self._annotate_membership(project_root, state)
                return state

        # DEPRECATED — see #58 sub-clause. Singleton fallback below
        # runs only when no conductor identity is in the call chain
        # OR when the calling conductor has no per-conductor binding
        # yet. New code MUST NOT extend the singleton path.
        # SQLite is the SOLE source of truth. The legacy JSON is NEVER read
        # back to rehydrate authority — a deleted/empty sqlite must read as
        # inactive, not be resurrected from a file (hard authority law,
        # 2026-05). AidocsManagedStore.init_db already ingests+deletes any
        # legacy JSON; the file is, at most, an exported record.
        state = dict(self._store.get(project_root))
        state["resolved_via"] = "singleton_fallback"
        state["path"] = str(self.config_path(project_root))
        state["security_profile"] = "dev" if _is_dev_mode(project_root) else "release"
        # Boot-token surfacing (2026-04-23): the token is still stamped
        # on set_mode() for forensic/debug purposes, but we no longer
        # derive `requires_reconnect` from it here. The previous design
        # used a Python module-level token to detect fresh MCP server
        # processes — but claude_hook.py runs in a separate subprocess
        # per tool call, so its module-level token NEVER matches the
        # MCP server's stamp, raising requires_reconnect on every single
        # PreToolUse indefinitely. The fresh-CLI signal lives in
        # session_query_gate.requires_reconnect, which is explicit and
        # cross-process safe; that's the authoritative signal now.
        state["current_boot_token"] = _MCP_SERVER_BOOT_TOKEN
        state["requires_reconnect"] = False
        self._annotate_membership(project_root, state)
        return state

    def _annotate_membership(
        self,
        project_root: Path,
        state: dict[str, object],
    ) -> None:
        """Stamp ``membership_valid`` / ``stale_bind`` onto a resolved
        managed-mode state. CHEAP read only (one PK lookup) — NEVER heals
        here (get_mode is on the hot prompt path). A bind is ``stale`` when
        managed mode reports active for a session that is NOT a SQL member;
        consumers (tool-call attribution/surfacing, already-active fast
        paths, session_start restamp) must treat a stale bind as
        not-authorized rather than trusting the active flag — closing the
        'active but require_session refuses' split-brain. Healing happens at
        the explicit bind boundaries, not on this read.
        """
        sid = str(state.get("session_id") or "").strip()
        active = bool(state.get("active"))
        if not active or not sid:
            state["membership_valid"] = False if active else None
            state["stale_bind"] = False
            return
        try:
            from .session_membership_store import SessionMembershipStore

            is_member = SessionMembershipStore().is_member(project_root, sid)
        except Exception:
            is_member = False
        state["membership_valid"] = is_member
        state["stale_bind"] = not is_member

    def set_mode(
        self,
        project_root: Path,
        session_id: str,
        source: str = "/aidocs",
        *,
        host_session_id: str = "",
    ) -> dict[str, object]:
        """Bind managed-mode for `session_id`. Writes both the
        per-conductor mapping (when cli_session_id is provided —
        authoritative path) AND the legacy singleton (deprecated
        fallback for callers that don't yet plumb identity).

        See security-gates.md §0.5 #50/#54 sub-clause for the
        write-both contract.

        Membership note (2026-05-24 split-brain seal): set_mode is the
        low-level persist and stays mechanical so existing callers/tests are
        unaffected. It does a BEST-EFFORT bounded heal (register legacy
        on-disk sessions exactly once, marker-gated) so reconnecting to a
        real legacy session converges to membership — but it does NOT refuse
        here. The fail-closed authority is enforced at the BIND BOUNDARIES
        (session_connect/session_start/ManagedModeService.connect call
        ``ensure_active_session_or_refuse`` first) and at every read consumer
        via ``get_mode``'s ``membership_valid``/``stale_bind`` flags, so
        managed mode is never TRUSTED active for a session require_session
        would refuse.
        """
        sid_bind = (session_id or "").strip()
        try:
            from .session_membership_store import SessionMembershipStore

            SessionMembershipStore().ensure_member_or_heal(project_root, sid_bind)
        except Exception:
            pass
        self._store.init_db(project_root)
        # #58 authoritative write: per-conductor mapping when identity
        # is provided. This is the path future-direction reads will hit.
        if host_session_id:
            self._store.set_per_conductor(
                project_root,
                cli_session_id=host_session_id,
                session_id=session_id,
                source=source,
                boot_token=_MCP_SERVER_BOOT_TOKEN,
            )
        # DEPRECATED — see #58 sub-clause. Singleton write kept for
        # back-compat: code paths that don't have cli_session_id still
        # need a "current session for this project" answer. New
        # consumers MUST use cli_session_id and read via per-conductor.
        state: dict[str, object] = dict(
            self._store.set(
                project_root,
                session_id=session_id,
                source=source,
                boot_token=_MCP_SERVER_BOOT_TOKEN,
            ),
        )
        state["path"] = str(self.config_path(project_root))
        state["current_boot_token"] = _MCP_SERVER_BOOT_TOKEN
        state["requires_reconnect"] = False
        if host_session_id:
            state["cli_session_id"] = host_session_id
            state["resolved_via"] = "per_conductor"
        # No shadow-write: sqlite is the sole authority and must not be
        # mirrored to a file that could later rehydrate it (hard authority
        # law, 2026-05). The legacy JSON is never written or read back.
        # Binding managed mode is the signal that subsequent tool calls
        # may omit ``root``; the resolver falls back to this default
        # whenever no other discovery hint exists.
        from .mcp_server_runtime_helpers import set_default_project_root

        set_default_project_root(project_root)
        return state

    def connect(
        self,
        project_root: Path,
        requested_session_id: str,
        source: str = "ai_session",
        *,
        host_session_id: str = "",
    ) -> dict[str, object]:
        """Bind managed-mode for the calling conductor.

        #58 (canonical 2026-04-26): when cli_session_id is provided,
        the bind affects ONLY that conductor's per-conductor mapping;
        other conductors on the same project keep their own bindings
        untouched. The singleton is also updated for back-compat
        fallback, but it is no longer authoritative.
        """
        current = self.get_mode(project_root, host_session_id=host_session_id)
        current_sid = str(current.get("session_id") or "")
        is_active = bool(current.get("active"))
        # An active bind is only trustworthy as 'already connected' when the
        # bound session is still a SQL member. A stale bind (active but
        # non-member) must NOT short-circuit as connected — fall through to
        # the rebind path, where set_mode heals (bounded) or fail-closes.
        current_is_member = bool(current.get("membership_valid"))

        if (
            is_active
            and current_is_member
            and (not requested_session_id or requested_session_id == current_sid)
        ):
            return {
                "connected": True,
                "session_id": current_sid,
                "already_active": True,
                "switched_from": None,
                "resolved_via": current.get("resolved_via"),
            }

        target_sid = requested_session_id or current_sid
        if not target_sid:
            return {
                "connected": False,
                "reason": "no_session_id",
            }

        # Fail-closed bind boundary: heal a legacy session once, else refuse.
        # Membership is the sole authority — managed mode must not go active
        # for a session require_session would reject.
        from .session_membership_store import SessionMembershipStore

        if not SessionMembershipStore().ensure_member_or_heal(project_root, target_sid):
            return {
                "connected": False,
                "session_id": target_sid,
                "blocked_by": "session_not_in_project",
                "reason": (
                    f"session '{target_sid}' is not a member of this project "
                    f"(membership is the sole authority). If it is an "
                    f"unmigrated legacy session, run "
                    f"`aidocs migrate-control-authority`."
                ),
            }
        self.set_mode(
            project_root,
            session_id=target_sid,
            source=source,
            host_session_id=host_session_id,
        )
        return {
            "connected": True,
            "session_id": target_sid,
            "already_active": False,
            "switched_from": current_sid
            if (is_active and current_sid and current_sid != target_sid)
            else None,
            "resolved_via": "per_conductor" if host_session_id else "singleton_fallback",
        }

    def clear_mode(self, project_root: Path) -> dict[str, object]:
        self._store.init_db(project_root)
        state: dict[str, object] = dict(self._store.clear(project_root))
        state["path"] = str(self.config_path(project_root))
        # Defensive cleanup: remove any stale legacy JSON so the project
        # never carries a file that looks like managed state. It is never
        # read back as authority, but leaving it is misleading.
        path = self.config_path(project_root)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return state

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── First-ever bootstrap timestamp (2026-04-30) ──
    # Surface the underlying store's bootstrap_completed_at column on
    # the service so callers (RuntimeBootstrapOrchestrationService)
    # don't poke at the private _store. NULL → first-ever bootstrap;
    # non-NULL → per-launch path. See aidocs_managed_store doctrine
    # comment for the operator-doctrine context.

    def get_bootstrap_completed_at(
        self,
        project_root: Path,
    ) -> str | None:
        self._store.init_db(project_root)
        return self._store.get_bootstrap_completed_at(project_root)

    def stamp_bootstrap_completed(self, project_root: Path) -> None:
        self._store.init_db(project_root)
        self._store.stamp_bootstrap_completed(
            project_root,
            when=self._timestamp(),
        )
