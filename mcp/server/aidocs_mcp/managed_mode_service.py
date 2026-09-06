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


# ── THE ONE DOOR FOR AUTHORITY (#1027 phase 2) ──────────────────────────────
#
# Free functions, not methods on ManagedModeService — see the note above
# `get_mode` for why (duck-typed doubles in ~60 test files; MagicMock ones fail
# SILENTLY rather than loudly). These take the service, so a double needs
# nothing but the `get_mode` it already has.


def explain_managed_session(
    managed_mode,
    project_root: Path,
    *,
    host_session_id: str = "",
    strict: bool = False,
) -> tuple[str, str]:
    """``(session_id, reason)`` — the session that may AUTHORIZE or SCOPE work.

    Empty session id means "no session may act here", and the reason is ALWAYS
    populated when it is empty: a refusal that cannot say why is what sent
    readers chasing a re-read that could never help.

    ``managed_mode`` is any object exposing ``get_mode`` — the real service, or
    a test double. The rule lives HERE, in one place, rather than in each of
    the ~110 call sites that used to read ``active`` for themselves.
    """
    # PASS ONLY WHAT THE CALLER ASKED FOR. `strict` is forwarded ONLY when it
    # is set, because that is what the call sites did before this door existed
    # — most never mentioned it. Sending `strict=False` unconditionally is not
    # a no-op: plenty of `get_mode` implementations (and every test double
    # written against the common signature `(root, host_session_id=None)`)
    # take no such kwarg, so the extra argument raises TypeError, which the
    # gates' fail-closed `except` then converts into a DENY. Measured: doing it
    # the tidy way turned ~30 passing tests into denials.
    kwargs: dict[str, object] = {}
    if host_session_id:
        kwargs["host_session_id"] = host_session_id
    if strict:
        kwargs["strict"] = True
    state = managed_mode.get_mode(project_root, **kwargs)
    return judge_managed_state(state, project_root)


def judge_managed_state(state, project_root: Path) -> tuple[str, str]:
    """``(session_id, reason)`` for a state the caller ALREADY fetched.

    Same rule as ``explain_managed_session``, without the read. For sites that
    legitimately keep their own ``get_mode`` call — because they also need
    ``resolved_via``, ``security_profile`` or ``membership_valid`` — this is
    how they get the authority answer WITHOUT a second read.

    That second read is not free of consequence even where the request memo
    makes it cheap: `test_host_state_resolves_managed_session_by_conductor`
    counts the calls and saw each host resolved twice.
    """
    # READ IT THE WAY THE CALL SITES DID — duck-typed, not isinstance-checked.
    # An `isinstance(state, dict)` guard looks tidier and is a SEMANTIC CHANGE:
    # MagicMock-based runtimes (used by whole security suites) are not dicts,
    # so the guard silently reclassified them as unmanaged and turned a read
    # the gate should BLOCK into a continue. Measured on
    # test_tool_gate_session_artifact. A refactor must not move behaviour for
    # any shape it did not previously reject.
    try:
        active = state.get("active")
        raw_sid = state.get("session_id")
        stale = state.get("stale_bind")
        resolved_via = state.get("resolved_via")
    except AttributeError:
        return "", "managed_mode_unreadable"
    if not active:
        return "", str(resolved_via or "") or "managed_mode_inactive"
    sid = str(raw_sid or "").strip()
    if not sid:
        return "", "managed_binding_names_no_session"
    # `is True`, not truthiness: `_annotate_membership` writes a real bool, and
    # a MagicMock attribute is truthy for everything — testing loosely here
    # would make every mock-backed caller look like a ghost session.
    if stale is True and _membership_is_authoritative(project_root):
        # ACTIVE AND STALE IS THE DANGEROUS COMBINATION, not inactive: the
        # binding LOOKS live and names a session that is not a member, so every
        # read keyed on it lands somewhere that does not exist.
        return "", f"stale_bind:{sid}"
    return sid, ""


def resolve_managed_session(
    managed_mode,
    project_root: Path,
    *,
    host_session_id: str = "",
    strict: bool = False,
) -> str:
    """The managed session that may AUTHORIZE or SCOPE work, or ``""``.

    Use this anywhere the answer DECIDES something: gating a tool, stamping
    attribution, choosing whose state to read or write. Use ``get_mode`` only
    to DESCRIBE a binding to a human.
    """
    return explain_managed_session(
        managed_mode, project_root, host_session_id=host_session_id, strict=strict
    )[0]


def _membership_is_authoritative(project_root: Path) -> bool:
    """Can a MISSING membership row prove this session is a ghost?

    STALENESS REQUIRES EVIDENCE. ``stale_bind`` means "no membership row",
    which is only damning where membership is the authority. A project never
    SEALED has not established that authority, so an absent row there says
    nothing at all — refusing on it would deny real work on the strength of a
    table nobody populated.

    ``is_sealed`` is the store's own existing notion of exactly this, and it is
    what ``test_get_mode_marks_stale_bind_for_non_member`` seals FIRST before
    expecting a ghost to be flagged. Measured on the live project: sealed, 48
    members, ``ubermega`` a member and ``probe-593`` not — the real ghost is
    caught.

    DO NOT READ THIS AS A BROAD SAFETY VALVE. Measured 2026-09-05, four
    sequences on fresh tmp projects:

        untouched                  -> sealed False
        ensure_schema only         -> sealed False
        set_mode WITHOUT register  -> sealed TRUE, member False  (ghost)
        register THEN set_mode     -> sealed False, member True

    ``set_mode`` SEALS when it cannot heal the session into membership, so the
    unsealed state barely survives contact with a binding. In practice this
    predicate spares almost nothing: a binding written for a session that was
    never registered IS treated as a ghost, which is the intent — but the
    protection it offers "projects that do not use membership" is close to
    theoretical, not the broad amnesty an earlier version of this docstring
    implied. Production is unaffected because session creation registers
    (session_store.py:1267) and set_mode heals a session that exists.

    An unreadable store answers False: unable to prove a ghost is not the same
    as proving one, and this predicate only ever ADDS a refusal.
    """
    try:
        from .session_membership_store import SessionMembershipStore

        return bool(SessionMembershipStore().is_sealed(project_root))
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

    # ── THE ONE DOOR FOR AUTHORITY (#1027 phase 2) ───────────────────────────
    #
    # THE DOOR IS A FREE FUNCTION -- `resolve_managed_session` /
    # `explain_managed_session`, at module scope below -- NOT a method here.
    # That is deliberate, and it was measured:
    #
    # `managed_mode` is reached through DUCK-TYPED DOUBLES in ~60 test files
    # (`SimpleNamespace(get_mode=...)` and `MagicMock()` hubs). Adding a METHOD
    # to that interface forces every one of them to be migrated, and the two
    # double styles fail differently:
    #   * SimpleNamespace raises AttributeError, which the gates' fail-closed
    #     `except` converts into a DENY -- a test-double gap wearing a security
    #     refusal;
    #   * MagicMock silently returns a MagicMock, which flows into production
    #     as a fake session id and only surfaces at some later assertion. That
    #     one is worse: it does not fail, it lies.
    #
    # A free function that TAKES the service needs nothing from a double but
    # the `get_mode` it already has. Same single rule, same single place, no
    # interface change, and the silent-MagicMock hazard cannot arise.
    #
    # `get_mode` below stays DIAGNOSTIC. It reports what the store holds,
    # annotates `membership_valid` / `stale_bind`, and deliberately leaves
    # `active` alone -- three tests pin that contract BY NAME
    # (test_identityless_caller_still_gets_the_singleton,
    # test_gate_strict_isolates_tenants_but_preserves_local_singleton,
    # test_get_mode_marks_stale_bind_for_non_member), and an attempt to make it
    # refuse broke 28 tests. Diagnostics MUST still see a broken binding; that
    # is how an operator finds out it is broken.
    #
    # But annotate-don't-act asked EVERY reader to remember a rule it could not
    # see, and readers did not:
    #   * the DASHBOARD trusted `active` and raised FileNotFoundError on every
    #     per-session read until #1012 taught it to re-check `list_sessions`;
    #   * READ-GROUNDING trusted `active` and resolved edits against a session
    #     that did not exist, refusing every ai_replace while naming a remedy
    #     that could not work.
    #
    # Two consumers, two bespoke defences, one shared cause -- and ~70 more
    # `managed.get("active")` reads that had never been audited.

    def get_mode(
        self,
        project_root: Path,
        *,
        host_session_id: str = "",
        strict: bool = False,
    ) -> dict[str, object]:
        """DIAGNOSTIC managed-mode state. NOT an authorization signal.

        For anything that decides what happens -- gating, scoping, attribution
        -- call ``resolve_managed_session`` / ``explain_managed_session``
        instead (module-scope free functions, above). This method
        reports a binding faithfully INCLUDING a broken one, because hiding
        that is how an operator loses the ability to diagnose it.

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
        # A READ MUST BE ABLE TO SAY NO. init_db here created the store that would
        # answer the question, so "is managed mode active?" adopted any directory it
        # was asked about — measured 2026-07-28: an empty folder gained
        # .MEMORY/.index/aidocs.sqlite3 from a SessionStart that only ever ASKED.
        # Where there is no `.MEMORY`, "not managed" is the correct, complete answer
        # and costs no write. init_db still runs for adopted projects (it migrates
        # schema), so nothing about the managed path changes.
        from ._sqlite_index_store_base import ProjectNotAdopted

        try:
            self._store.init_db(project_root)
        except ProjectNotAdopted:
            return {
                "active": False,
                "session_id": None,
                "source": None,
                "resolved_via": "not_an_aidocs_project",
            }
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
                # DO NOT derive requires_reconnect from the boot token here.
                # Tried 2026-08-06 and reverted the same hour: claude_hook runs
                # in a SEPARATE SUBPROCESS per tool call, so its module-level
                # token can never match the server's stamp and this would raise
                # requires_reconnect on every PreToolUse forever -- the exact
                # bug the 2026-04-23 note below already records. The authoritative
                # fresh-CLI signal is session_query_gate.requires_reconnect,
                # which is explicit and cross-process safe.
                state["requires_reconnect"] = False
                self._annotate_membership(project_root, state)
                return state

        # Gate-strict (#253 §XIX): WebMCP tool-authorization resolves ONLY
        # per-conductor above. With no per-conductor binding for this identity we
        # return INACTIVE here — never the global singleton — so one tenant's
        # managed-mode activation cannot leak to another. Local callers
        # (strict=False, the default) keep the singleton path below unchanged.
        #
        # #1001 (Landing 2 of #880): NO HEAL BEFORE THE REFUSAL. Until
        # 2026-09-03 `_heal_chain_attested_binding` ran here and could
        # manufacture the missing row from the project singleton. A missing
        # per-conductor row is now exactly that -- the caller has not selected
        # a session -- and the refusal names the missing binding and the
        # remedy. Only ai_session(mode='connect') writes a binding.
        missing_binding = {
            "missing_binding_host_session_id": sid,
            "remedy": "ai_session(mode='connect')",
        }
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
                **(missing_binding if sid else {}),
            }
            return strict_state

        # UNKNOWN IS NOT A PASS (#672 fail-open 1). A caller that NAMED a host
        # session we cannot resolve is REFUSED — it never receives the project
        # singleton's session, because that is ANOTHER ACTOR'S IDENTITY handed
        # back as if it were the caller's own. The substitution did not merely
        # misidentify: tool_gate_service._stamp_owned_host_ids then WROTE the
        # caller's host ids into that other session's owned host-id chain,
        # CONTAMINATING it. And agent_context_id — the respawn key, the freeze
        # scope key, the epoch root — is f(project, host_kind, host_session_id),
        # so a borrowed session corrupts its inputs as well.
        #
        # An identity-LESS caller (sid == "") is a DIFFERENT question and keeps
        # the singleton below: it claims to be no one, so nothing is being
        # substituted FOR it. That is the legacy single-session stdio host case
        # documented at mcp_server_runtime_helpers._last_known_for_caller.
        if sid:
            return {
                "active": False,
                "session_id": "",
                "source": None,
                "resolved_via": "unresolvable_host_session",
                "path": str(self.config_path(project_root)),
                "security_profile": "dev" if _is_dev_mode(project_root) else "release",
                "current_boot_token": _MCP_SERVER_BOOT_TOKEN,
                "requires_reconnect": False,
                "membership_valid": None,
                "stale_bind": False,
                **missing_binding,
            }

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

    # `_heal_chain_attested_binding` (source='chain_attested_heal') lived here
    # from #816 until #1001 (2026-09-03, Landing 2 of #880). It manufactured a
    # per-conductor row for a caller that had none, taking WHICH SESSION from
    # the project singleton on the strength of a window lease. The lease proves
    # the caller is a live window; it does not prove which session that window
    # selected -- the singleton is a broader layer, and empire law
    # promoted-cc6c4ac686ee forbids healing identity from one. MEASURED: window
    # f4c093aa carried source='chain_attested_heal' for ubermega while its own
    # XAACP route answered redteam. A missing row now REFUSES, naming the
    # binding that is missing and the remedy ai_session(mode='connect'); the
    # #816 lockout it was built for was closed at the source by #880 phase 2.

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
        authenticate_host: bool = False,
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
        # #599/#54-C1 WRITER HYGIENE. A host_session_id that IS this request's
        # TRANSPORT TOKEN is a connection handle, not an actor — under
        # DAEMON_STATELESS_HTTP it is minted per request, so accepting it here
        # shreds ONE window into N "live" conductor bindings (measured: 83 on
        # one root). correlate_host_session refuses on >=2 live bindings, so a
        # polluted registry makes the #599 correlation refuse essentially
        # always — the registry the join reads was being poisoned by the very
        # rotation the join exists to cure. Refuse the phantom row; the legacy
        # singleton below is still written, so no caller loses its answer.
        if host_session_id:
            try:
                from .mcp_server_runtime_helpers import (
                    current_request_transport_session_id,
                )

                if host_session_id == (current_request_transport_session_id() or None):
                    host_session_id = ""
            except Exception:
                pass
        # #599/#54-C1b THE SAME PROOF, AT THE WRITER. The transport-token check
        # above only catches an id this request happens to be carrying; the
        # measured wreckage (51 phantom bindings, 0 genuine ones) was written by
        # callers that had already laundered a rotating id through
        # current_calling_host_session_id(). A pruner that has to run again next
        # week has not fixed anything, so the durable proof the pruner uses is
        # applied here too: the #464 AUTHENTICATED host-id chain, whose entries
        # come only from authenticated hook stamps for THIS session's row.
        #
        # If the session HAS a chain and this id is not in it, the id is not
        # this session's actor and must not mint a binding.
        #
        # AVAILABILITY, and the reason this is not symmetric: an EMPTY chain
        # refuses NOTHING. A session's first-ever bind legitimately happens
        # before any hook has stamped anything, and refusing it would break
        # every new session. UNPROVABLE is permissive at the writer and
        # conservative at the pruner — it never buys a destructive act and never
        # buys a confident answer.
        if host_session_id:
            try:
                from .session_query_gate_store import SessionQueryGateStore

                # ── THE LEASE DECIDES, NOT THE CHAIN (#892 phase 5) ────────
                #
                # Operator ruling 2026-08-23: "retire the chain and the slot,
                # use the lease". The question is unchanged — may this id MINT a
                # binding — but the chain answered it badly in both directions:
                # cap-16 FIFO EVICTS a live window (#880 measured ~4 slots in
                # one evening), and append-only REMEMBERS windows long dead. The
                # lease says whether this id is a window NOW, which is the
                # question actually being asked.
                #
                # THE ASYMMETRY IS PRESERVED EXACTLY. The old rule was "an EMPTY
                # chain refuses NOTHING", because a session's first bind
                # legitimately precedes any hook stamp and refusing it would
                # break every new session. Only a definite False refuses here;
                # None — unreadable, or nothing recorded yet — stays permissive.
                # UNPROVABLE is permissive at the writer and conservative at the
                # pruner: it never buys a destructive act and never buys a
                # confident answer.
                #
                # The import above is still used: the enrolment below writes to
                # the chain, which remains populated for the readers not yet
                # migrated (tool_gate_service.py:1800 is its dominant writer).
                from .window_binding_store import WindowBindingStore

                _store = WindowBindingStore()
                # COMPLETENESS FIRST, and it is load-bearing HERE in a way it
                # was not in `classify`. This is a single lookup with no
                # per-session rule behind it, so an EMPTY store would answer
                # False for every id and refuse the first-ever bind of every new
                # session — exactly the availability failure "an EMPTY chain
                # refuses NOTHING" existed to prevent. Caught by a test.
                _leased = (
                    _store.conversation_is_bound(project_root, host_session_id)
                    if _store.has_any_conversation(project_root) is True
                    else None
                )
                # ── TWO AXES, TWO STORES (#599 C1b restored 2026-08-24) ───────
                #
                # Retiring the chain here was over-applied, and three tests said
                # so: "C1b no longer guards anything", the C1b partial mitigation
                # failing, and a new window no longer enrolling into a session
                # that already has a chain.
                #
                # THE TWO STORES ANSWER DIFFERENT QUESTIONS AND NEITHER CAN
                # ANSWER THE OTHER'S:
                #   the LEASE is WINDOW-scoped  -> "is this id a live window NOW?"
                #   the CHAIN is SESSION-scoped -> "is this id a member of THIS
                #                                   session's authenticated set?"
                # C1b asks MEMBERSHIP. A parent conductor is a perfectly live
                # window, so the lease answers True for it and the rebind sails
                # through — which is exactly how a subagent repointed its
                # parent's row. Retiring the chain was right for the three
                # LIVENESS sites; this one was never a liveness question.
                #
                # THE ASYMMETRY IS UNCHANGED ON BOTH AXES: an EMPTY chain refuses
                # NOTHING, just as an unprovable lease refuses nothing. Only a
                # POSITIVE denial — a chain that exists and does not contain this
                # id — refuses, so a session's first-ever bind still works.
                #
                # THIS IS A DEBT, AND IT HAS A NAMED CREDITOR: it keeps one chain
                # reader alive against the "no more chains" directive. #897's
                # agent tree is what retires it — once a session's membership can
                # be read from the tree, this check moves there and the chain
                # goes with the last of its readers.
                _chain = SessionQueryGateStore().get_host_session_id_chain(
                    project_root,
                    sid_bind,
                )
                _chain_denies = bool(_chain) and host_session_id.strip().lower() not in {
                    str(c).strip().lower() for c in _chain
                }
                if _leased is False or _chain_denies:
                    # A NEW WINDOW IS NOT A PHANTOM (2026-08-16). Dropping the id
                    # here is right for an IMPLICIT write, and catastrophic for an
                    # EXPLICIT bind: a session that any earlier window worked in
                    # carries a chain, so a fresh Claude Code window — the state
                    # after every restart, update, or simply opening a second
                    # window — could never enter it. The per-conductor write was
                    # skipped, the singleton still written, and connect answered
                    # connected=True while every strict reader refused
                    # managed_mode_not_active. PERMANENTLY: the chain only grows
                    # from authenticated stamps, and the gate refuses the very
                    # calls that would stamp one. Measured on this project:
                    # phoenix/ubermega chain_len=3, caller absent from both,
                    # per-conductor row None, every tool refused with hooks on.
                    #
                    # At an explicit bind boundary the caller has already passed
                    # membership + RBAC, and its id came from the TRANSPORT
                    # (stdio_shim captures CLAUDE_CODE_SESSION_ID at spawn; it
                    # never enters model context), so enrolling it grants no
                    # authority a caller could forge. #599's real phantom source
                    # is the ROTATING transport token, refused just above and
                    # never reaching here.
                    if authenticate_host:
                        SessionQueryGateStore().record_host_session_ids(
                            project_root,
                            sid_bind,
                            [host_session_id],
                        )
                    else:
                        host_session_id = ""
            except Exception:
                # Unreadable ledger is UNPROVABLE, not a verdict: bind as before.
                pass
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
        # #720 fix (a), 2026-08-14: ADOPT THE CALLING HOST SESSION.
        #
        # The guard below only works if the caller supplies its own identity.
        # The item asked "FIRST THING TO ESTABLISH: does the ai_session tool
        # path pass the CALLER's host_session_id through to connect, or pass
        # empty?" It passes empty. So `strict` was False, the lookup fell back
        # to the PROJECT SINGLETON, an active singleton short-circuited as
        # already_active=True, set_mode was SKIPPED, and no per-conductor row
        # was ever written for the caller. Strict tool-auth readers then
        # evaluated the CALLER's host session, found nothing, and refused
        # managed_mode_not_active right after a "successful" connect. The
        # advertised remedy reported success and changed nothing the gate
        # would honour (law 311bf3e6: a named remedy must be reachable).
        #
        # Measured 2026-08-14: this exact state opened a conductor session with
        # every gated tool refusing while session_start answered already_active,
        # and it is what made the #599 suite vacuous from the other direction.
        #
        # The identity comes from the TRANSPORT, never from the model.
        # stdio_shim captures CLAUDE_CODE_SESSION_ID at spawn and it never
        # enters model context ("a boundary the occupant can restate is not a
        # boundary"), so adopting it grants no authority a caller could forge.
        # An explicit argument still wins: that is the caller's own override.
        #
        # A caller with NO identity resolves to "" and keeps the legacy
        # singleton path unchanged, which is #672's deliberate carve-out:
        # nothing is being substituted for a caller that claims to be no one.
        #
        # #906, 2026-08-25: THE RESOLUTION MOVED TO resolve_conductor_key().
        # This used to call current_calling_host_session_id() directly, and so
        # did the tool gate's reader, while the GATE's session_select path passed
        # the bare OAuth user_id instead -- one row key, spelled three ways. On
        # the web surface with no conversation claim in params._meta the stamp is
        # "", so this wrote no per-conductor row at all while session_select
        # wrote one under the principal; whichever the reader matched, the other
        # caller's binds were invisible. Every side now derives the key from that
        # single function.
        #
        # LOCAL BEHAVIOUR IS UNCHANGED, bit for bit: resolve_conductor_key
        # returns the stamp whenever there is one, and local dispatch has no gate
        # principal, so its second rung cannot fire here. What changes is only
        # the remote-without-conversation-claim case, which previously resolved
        # to nobody.
        if not host_session_id:
            try:
                from .mcp_server_runtime_helpers import resolve_conductor_key

                host_session_id = (resolve_conductor_key()[0] or "").strip()
            except Exception:
                host_session_id = ""
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

        # A bind stamped by a PREVIOUS server process is not a live bind: the
        # boot token exists precisely to invalidate it. Short-circuiting here
        # returned already_active=True while writing nothing, so the gate — which
        # checks the CURRENT process's binding — kept refusing. Falling through
        # re-stamps, which is what "connect" was always supposed to mean.
        #
        # #816, 2026-08-20: THE PARAGRAPH ABOVE WAS TRUE AND THE CONDITION BELOW
        # DID NOT IMPLEMENT IT. It tested active + member + session-match and
        # never once looked at the token, so a binding from a dead process
        # short-circuited exactly as if it were live. Measured that morning on
        # the conductor's own session after `aidocs service restart`: connect
        # answered already_active=true, wrote nothing, and every gated tool kept
        # refusing managed_mode_not_active. #840, prose-is-not-the-thing, on the
        # most load-bearing path in the product.
        #
        # An EMPTY token also fails this test, deliberately. A row that cannot
        # prove which process stamped it is not evidence of a live bind, and the
        # cost of being wrong is asymmetric: falling through re-stamps (cheap,
        # idempotent, and what connect means), while short-circuiting strands
        # the caller with a green receipt and no way out.
        #
        # SAFE IN THE HOOK SUBPROCESS TOO. get_mode's comment at :242-249 warns
        # that claude_hook runs in a separate process whose module token can
        # never match the server's — which is why requires_reconnect must not be
        # derived from the token there. Here the mismatch merely causes a
        # re-stamp rather than a refusal, so the failure mode that warning
        # guards against cannot occur on this path.
        bound_token = str(current.get("bound_by_boot_token") or "").strip()
        current_is_live = bound_token == _MCP_SERVER_BOOT_TOKEN
        if (
            is_active
            and current_is_member
            and current_is_live
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
        # authenticate_host: this IS the explicit bind boundary — membership and
        # RBAC are already settled above, so a first-time window enrols instead
        # of being silently dropped (see set_mode's C1b note).
        bound = self.set_mode(
            project_root,
            session_id=target_sid,
            source=source,
            host_session_id=host_session_id,
            restamp_singleton=restamp_singleton,
            authenticate_host=True,
        )
        # NEVER ANSWER GREEN FOR A BIND THE GATE WILL NOT HONOUR (law 311bf3e6:
        # a named remedy must be reachable). set_mode may still decline the
        # per-conductor write — a per-request transport token is refused by
        # design — and the old code reported "connected": True with
        # resolved_via="per_conductor" read off the LOCAL variable rather than
        # off what persisted. That is the exact lie that cost an operator an
        # hour on 2026-08-16: connect said success, every tool refused, and the
        # advertised remedy was the call that had just "succeeded". Report the
        # binding that actually landed.
        bound_via = str(bound.get("resolved_via") or "")
        if host_session_id and bound_via != "per_conductor":
            return {
                "connected": False,
                "session_id": target_sid,
                "blocked_by": "host_identity_not_bound",
                "error": (
                    f"connect wrote no per-conductor binding for host session "
                    f"'{host_session_id}' on '{target_sid}', so every gated "
                    f"tool would refuse managed_mode_not_active. The identity "
                    f"was rejected as a per-request transport token, which is "
                    f"not an actor. Re-call with the stable host identity the "
                    f"stdio shim captures at spawn."
                ),
            }
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
            "resolved_via": bound_via or "singleton_fallback",
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
