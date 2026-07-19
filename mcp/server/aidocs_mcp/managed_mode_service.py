from __future__ import annotations

import contextvars
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from .aidocs_managed_store import AidocsManagedStore


# ── Request-scoped get_mode memo (#436) ──
# One prompt-submit / hook event resolves get_mode ~11 times with the
# same inputs; each resolution is a sqlite read. The memo collapses
# that to ONE resolution per request window.
#
# SECURITY CONTRACT (#436/#434, non-negotiable): the memo lives ONLY
# inside an explicit begin_request_mode_memo()/reset_request_mode_memo()
# window. At module import the ContextVar defaults to None — with no
# window open there is NO caching at all, so nothing can ever leak
# across requests or across conductors. Never a module-global dict:
# the ContextVar is request/task-local by construction, and the dict
# it holds is created fresh per window.
_request_mode_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_request_mode_memo",
    default=None,
)


def begin_request_mode_memo() -> contextvars.Token:
    """Open a request-scoped get_mode memo window (fresh empty cache).

    Returns the ContextVar token; the caller MUST reset it in a
    ``finally`` via ``reset_request_mode_memo(token)`` so the cache
    never outlives the request that opened it.
    """
    return _request_mode_memo.set({})


def reset_request_mode_memo(token: contextvars.Token) -> None:
    """Close the memo window opened by ``begin_request_mode_memo``."""
    _request_mode_memo.reset(token)


def _invalidate_request_mode_memo() -> None:
    """Drop all memoized resolutions in the active window (if any).

    Called by every managed-mode WRITE (set/clear/unbind) so a read
    later in the same request window observes the new truth instead of
    a stale pre-write snapshot. No-op when no window is open.
    """
    memo = _request_mode_memo.get()
    if memo is not None:
        memo.clear()


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


# ── Conversation-connect latch (#63) ──
# managed-mode connect is a ONE-SHOT identity handshake per
# (conversation_key, MCP-server-process). The latch is module state:
# an MCP restart resets it (each process gets its own one-shot per
# conversation). The conversation_key is derived per-host by the
# CALLER (Claude Code: transcript hash; OpenCode: session_id; ...) —
# this module only enforces the once-per-key rule. Empty key = legacy
# host without conversation identity → no dedupe possible, latch
# does not apply (back-compat).
_connected_conversations: dict[str, dict] = {}


def mark_conversation_connected(conversation_key: str, **info: object) -> None:
    """Record a successful connect for `conversation_key`. No-op for an
    empty key (nothing to dedupe on)."""
    key = str(conversation_key or "").strip()
    if not key:
        return
    entry: dict[str, object] = dict(info)
    entry["conversation_key"] = key
    entry.setdefault(
        "first_connected_at",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    _connected_conversations[key] = entry


def conversation_already_connected(conversation_key: str) -> dict | None:
    """Return the latch entry for `conversation_key`, or None."""
    key = str(conversation_key or "").strip()
    if not key:
        return None
    entry = _connected_conversations.get(key)
    return dict(entry) if entry is not None else None


def clear_conversation_latch(conversation_key: str = "") -> None:
    """Operator escape (aidocs_admin_clear_reconnect) + test isolation.
    Empty key clears ALL latched conversations in this process."""
    key = str(conversation_key or "").strip()
    if key:
        _connected_conversations.pop(key, None)
    else:
        _connected_conversations.clear()


def _is_dev_mode(project_root: Path) -> bool:
    """#404: the DEV-flavour security profile is retired. Always False —
    kept as a seam so callers/tests keep one central function while the
    remaining call sites are dismantled."""
    del project_root
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
        strict: bool = False,
    ) -> dict[str, object]:
        """Resolve managed-mode state, honoring conductor identity.

        strict=True (WebMCP tool-authorization, #253 §XIX): resolve ONLY the
        per-conductor mapping — NO singleton fallback — so one gate tenant's
        activation never leaks to another via the global-per-project singleton.
        strict=False (default, local callers): singleton path unchanged.

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

        #436: when a request memo window is open (begin_request_mode_memo),
        repeated calls with the same (project_root, host_session_id,
        strict) key return a shallow copy of the first resolution instead
        of re-reading sqlite. No window open → no caching at all.
        """
        memo = _request_mode_memo.get()
        memo_key = (str(project_root), (host_session_id or "").strip(), strict)
        if memo is not None and memo_key in memo:
            return dict(memo[memo_key])
        state = self._resolve_mode(
            project_root,
            host_session_id=host_session_id,
            strict=strict,
        )
        if memo is not None:
            memo[memo_key] = dict(state)
        return state

    def _resolve_mode(
        self,
        project_root: Path,
        *,
        host_session_id: str = "",
        strict: bool = False,
    ) -> dict[str, object]:
        """Uncached managed-mode resolution — the real read path behind
        ``get_mode``. See ``get_mode`` for the full contract."""
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

        # Gate-strict (#253 §XIX): WebMCP tool-authorization resolves ONLY
        # per-conductor above. With no per-conductor binding for this identity we
        # return INACTIVE here — never the global singleton — so one tenant's
        # managed-mode activation cannot leak to another. Local callers
        # (strict=False, the default) keep the singleton path below unchanged.
        if strict:
            strict_state: dict[str, object] = {
                "active": False,
                "session_id": "",
                "source": None,
                "resolved_via": "strict_no_per_conductor_binding",
                "path": str(self.config_path(project_root)),
                "security_profile": "dev" if _is_dev_mode(project_root) else "release",
                "current_boot_token": _MCP_SERVER_BOOT_TOKEN,
                "requires_reconnect": False,
                "membership_valid": None,
                "stale_bind": False,
            }
            return strict_state

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
        restamp_singleton: bool = True,
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
        #
        # #134: a per-conductor writer with identity MAY skip the singleton
        # (restamp_singleton=False) to avoid the multi-conductor ping-pong —
        # the session_start reconnect probe used to restamp the ONE project
        # singleton on every /mcp reconnect, so two conductors clobbered each
        # other's "current session". The singleton is a presentation fallback,
        # not authority. Default True preserves the write-both contract for
        # every existing caller + the pinning tests. When skipped, the return
        # state is read back from the per-conductor row (authoritative).
        if host_session_id and not restamp_singleton:
            _pc = self._store.get_per_conductor(
                project_root, cli_session_id=host_session_id
            ) or {}
            state: dict[str, object] = dict(_pc)
            state.setdefault("session_id", session_id)
            state.setdefault("active", True)
        else:
            state = dict(
                self._store.set(
                    project_root,
                    session_id=session_id,
                    source=source,
                    boot_token=_MCP_SERVER_BOOT_TOKEN,
                ),
            )
        _invalidate_request_mode_memo()
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

        set_default_project_root(project_root, host_session_id=host_session_id)
        return state

    def connect(
        self,
        project_root: Path,
        requested_session_id: str,
        source: str = "ai_session",
        *,
        host_session_id: str = "",
        conversation_key: str = "",
        restamp_singleton: bool = True,
    ) -> dict[str, object]:
        """Bind managed-mode for the calling conductor.

        #58 (canonical 2026-04-26): when cli_session_id is provided,
        the bind affects ONLY that conductor's per-conductor mapping;
        other conductors on the same project keep their own bindings
        untouched. The singleton is also updated for back-compat
        fallback, but it is no longer authoritative.

        #63 (one-shot per conversation): when `conversation_key` is
        provided and a connect already SUCCEEDED for that key in this
        MCP process, the call REFUSES with zero mutations — no get_mode
        read, no set_mode, no set_default_project_root, no restamp.
        Refusal is louder than a no-op on purpose: the reconnect-on-
        every-error reflex must surface, not silently succeed. Empty
        key preserves legacy multi-connect behavior.
        """
        conv_key = str(conversation_key or "").strip()
        if conv_key:
            existing = conversation_already_connected(conv_key)
            if existing is not None:
                self._audit_reconnect_refused(
                    project_root,
                    conversation_key=conv_key,
                    requested_session_id=requested_session_id,
                    existing=existing,
                )
                return {
                    "ok": False,
                    "connected": False,
                    "blocked_by": "already_connected",
                    "error": (
                        "managed_mode_connect already succeeded for this "
                        "conversation in this MCP process. Re-binding is "
                        "forbidden — call once per conversation. The bound "
                        f"session is '{existing.get('session_id', '')}'."
                    ),
                    "current_bound": existing,
                }
        # Resolve "already bound for THIS conductor" from the PER-CONDUCTOR
        # state, not the singleton (strict when a host identity is given). Else
        # a singleton set by a prior ai_session(connect) makes a per-conductor
        # bind (webmcp: host_session_id set, restamp_singleton=False) short-
        # circuit as already_active=True and SKIP set_mode — so no per-conductor
        # row is written, and strict tool-auth readers (ai_seat/ai_agents via
        # check_tool) then see managed_mode_inactive right after a "successful"
        # session_select. (webmcp conductor-restore regression 2026-07-16.)
        current = self.get_mode(
            project_root,
            host_session_id=host_session_id,
            strict=bool(host_session_id),
        )
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
            mark_conversation_connected(
                conv_key,
                project_root=str(project_root),
                session_id=current_sid,
                host_session_id=host_session_id,
                source=source,
            )
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
            restamp_singleton=restamp_singleton,
        )
        mark_conversation_connected(
            conv_key,
            project_root=str(project_root),
            session_id=target_sid,
            host_session_id=host_session_id,
            source=source,
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

    def _audit_reconnect_refused(
        self,
        project_root: Path,
        *,
        conversation_key: str,
        requested_session_id: str,
        existing: dict,
    ) -> None:
        """Best-effort forensic trail for #63 refusals. Never raises —
        and never mutates managed-mode state (audit log only)."""
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="managed_mode",
                source_kind="mcp",
                session_id=str(existing.get("session_id") or ""),
                action_kind="managed_mode_reconnect_refused",
                target_entity=conversation_key,
                status="ok",
                payload={
                    "conversation_key": conversation_key,
                    "requested_session_id": requested_session_id,
                    "first_connected_session_id": existing.get("session_id"),
                    "first_connected_at": existing.get("first_connected_at"),
                },
            )
        except Exception:
            pass

    def clear_mode(self, project_root: Path) -> dict[str, object]:
        """Clear ONLY the deprecated project-wide SINGLETON row (#438).

        This does NOT touch ``aidocs_managed_per_conductor`` rows — and
        per-conductor ROW EXISTENCE is the real binding (#58). Any
        "disable managed mode" flow that calls only clear_mode leaves
        every conductor still bound. Disable flows MUST call
        ``unbind_current_conductor`` (self, least-privilege) or
        ``unbind_all_conductors`` (admin-gated) in addition to — or
        instead of — this singleton clear.
        """
        self._store.init_db(project_root)
        state: dict[str, object] = dict(self._store.clear(project_root))
        _invalidate_request_mode_memo()
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

    def unbind_current_conductor(
        self,
        project_root: Path,
        host_session_id: str,
    ) -> dict[str, object]:
        """Sever the calling conductor's OWN per-conductor binding (#438).

        Deletes the ``aidocs_managed_per_conductor`` row for
        ``host_session_id`` — row existence IS the binding, so after this
        ``get_mode(host_session_id=...)`` no longer resolves per_conductor
        for that conductor. No admin gate on purpose: unbinding yourself
        is least-privilege (you can only remove your own row).
        """
        self._store.init_db(project_root)
        sid = (host_session_id or "").strip()
        removed = bool(sid) and self._store.delete_per_conductor(project_root, sid)
        _invalidate_request_mode_memo()
        return {
            "ok": True,
            "removed": removed,
            "host_session_id": sid,
        }

    def unbind_all_conductors(self, project_root: Path) -> dict[str, object]:
        """Sever EVERY per-conductor binding on this project (#438).

        Admin-gated fail-closed: requires
        ``project_authority.require_admin(...).ok`` — removing OTHER
        conductors' bindings is an operator action, not a self-service
        one. Refusal deletes nothing.
        """
        from .project_authority import require_admin

        decision = require_admin(
            project_root,
            operation="managed_mode_unbind_all",
        )
        if not decision.ok:
            return {
                "ok": False,
                "removed": 0,
                "blocked_by": "admin_required",
                "reason": str(decision.get("reason") or "admin_refused"),
            }
        self._store.init_db(project_root)
        count = self._store.delete_all_per_conductor(project_root)
        _invalidate_request_mode_memo()
        return {"ok": True, "removed": count}

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

    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        *,
        host_session_id: str,
    ) -> dict[str, object]:
        """Public transactional facade; snapshot errors intentionally propagate."""
        return self._store.snapshot_prompt_submit_state(
            project_root,
            host_session_id=host_session_id,
        )

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        snapshot: dict[str, object],
        *,
        host_session_id: str,
    ) -> None:
        self._store.restore_prompt_submit_state(
            project_root,
            snapshot,
            host_session_id=host_session_id,
        )
