"""Operator authentication service — Phase-1 control-plane auth wall.

Per `.MEMORY/plans/aidocs_control_plane_auth_plan.md` §3 and /goal
2026-05-20. Before this module:

  - cmd_dashboard_set_config trusted ``dashboard=True`` (a CLI flag)
    as authority. Any caller invoking the dashboard-* CLI commands
    inherited admin-grade authority without authentication.
  - identity_resolver auto-stamped ``user_id`` and ``effective_role``
    on audit rows from env vars (AIDOCS_OPERATOR_ID, fallback
    'operator' / 'super_admin'). This is ATTRIBUTION only — never
    authorization, but the existing code conflated the two.

This service makes the distinction explicit:

  OperatorAuthService.authenticate(token, project_root) → OperatorContext
                                                        | None

    Validates a real bearer token against the IdentityStore.
    Returns ``None`` when the token is missing/expired/disabled —
    callers MUST treat None as unauthenticated, never as 'safe
    fallback to env identity'.

  OperatorAuthService.require_permission(ctx, perm, project_root, *,
                                         scope_type, scope_id)
        → bool

    True iff ``ctx`` is an authenticated operator AND RBAC says
    they hold ``perm`` at the requested scope.

  identity_resolver remains canonical for AUDIT-ROW ATTRIBUTION
  (current_user / current_effective_role). It is NEVER consulted
  for authorization in this module. The two surfaces serve different
  jobs and the boundary is now enforced.

## Sources of an operator token

  1. ``--operator-token <hex>`` CLI flag (Dashboard Tauri command
     attaches this on every sensitive call).
  2. ``AIDOCS_OPERATOR_TOKEN`` env var (CI / scripted dashboard
     batches).

A token in neither place means UNAUTHENTICATED — config_set
operator_only / security_sensitive mutations refuse closed.

## Why no fallback to env-only identity

The previous shape allowed an unauthenticated caller with
AIDOCS_OPERATOR_ID set to inherit ``effective_role='super_admin'``
on audit rows. A hostile shell with that env set could write any
config and the audit chain attributed the change to whatever
operator id the env carried. Removing the fallback closes that
gap: env-only identity audits as ``user_id=''`` and the
authorization gate refuses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

OperatorSource = Literal[
    "dashboard",
    "cli",
    "operator_intent",
    "env",
    "host_binding",
]


# ---------------------------------------------------------------------------
# Control-plane command classification (sweep 2026-05-20)
# ---------------------------------------------------------------------------
#
# Every dashboard / control-plane mutating surface is classified so the
# auth-coverage contract test can prove no admin/operator-only mutation
# is reachable without an authenticated operator. Three classes:
#
#   "read_only"   — no state mutation; no auth required.
#   "user_safe"   — mutation a USER/DEV may perform without admin
#                   authority (e.g. session lifecycle for their own
#                   work). May still require *some* identity but not
#                   admin.manage_config.
#   "admin_only"  — operator/admin-only mutation. Requires an
#                   authenticated operator_context with
#                   admin.manage_config (or a setting-specific
#                   permission). Refused without a bearer token.
#   "mixed_self_gated" — one command whose read sub-modes are free but
#                   whose mutating sub-modes (flags) gate THEMSELVES via
#                   _require_operator_for_admin_command inside the handler
#                   (e.g. `operator-surface --apply / --expert-set`). The
#                   contract test asserts every mutating flag of such a
#                   command routes through that auth gate.
#
def _auth_status_flavor() -> str:
    """Install flavor for auth-status reporting, GLOBAL-only read.
    Fail-closed to 'corpo' (the strict flavor) so a read error never
    reports the relaxed solo/dev local-mintable mode.

    #953: delegates to distribution_flavor.effective_flavor, which
    path-locks the `dev` claim. This used to read the setting raw and so
    reported 'dev' for an install running out of site-packages — measured
    2026-08-28. The fail-closed value is unchanged and passed explicitly.
    """
    from .distribution_flavor import CORPO, effective_flavor

    return effective_flavor(on_error=CORPO)



# CLI subcommands (Python — gated + tested in this module's callers):
CLI_COMMAND_CLASS: dict[str, str] = {
    # Config control plane
    "dashboard-set-config": "admin_only",
    "dashboard-batch-config": "admin_only",
    "dashboard-delete-config": "admin_only",
    # TOML document editor — same single config write authority as the
    # typed Settings surface (no Tauri fs::write side door).
    "dashboard-save-toml": "admin_only",
    # Read-only editability verdict consumed by the TOML editor load side.
    "dashboard-toml-editability": "read_only",
    # Project MCP capability provider (.mcp.json) install/delete — a
    # control-plane mutation (adds agent tools), so admin-gated. Every
    # flavor requires an authenticated operator token (#404).
    "dashboard-mcp-config": "admin_only",
    # MCP registry READ — lists servers from the canonical SQLite table
    # (not .mcp.json); no mutation, user-safe, no auth.
    "dashboard-mcp-list": "read_only",
    # Persistent snapshot worker (dashboard-war (c)) — a long-lived stdin/
    # stdout loop serving the SAME read the `dashboard` command serves
    # (runtime.dashboard_snapshot), just without a fresh process per call.
    # Pure read; no mutation surface.
    "dashboard-worker": "read_only",
    # Explicit one-time legacy import (.mcp.json/SESSION.md → SQL). Sets
    # control authority, so admin-gated; the ONLY sanctioned legacy-ingest
    # path (reads never ingest). Idempotent (seals a per-store marker).
    "migrate-control-authority": "admin_only",
    # Governed deletion surface — reversible, audited mutation a user/agent
    # may perform on their own work (temp cleanup; checkpointed source removal).
    # Refuses protected/control-authority/checkpoint/unsafe paths internally;
    # never exposes the protected-route override. Restore + audited GC pair.
    "governed-delete": "user_safe",
    "governed-restore": "user_safe",
    "checkpoint-gc": "user_safe",
    # Unified restoration facade (list/timeline/inspect/diff/nearest/restore).
    # Read modes don't mutate; restore is reversible (pre-restore checkpoint)
    # and reuses path/protected/authority guards — agent-safe, no admin.
    "ai-restore": "user_safe",
    # Index-freshness sitter: status/check are reads; sync-now/fix reconcile the
    # code index (reindex of the project's own files) — user-safe, no admin.
    "index-sitter": "user_safe",
    # Recorded reconciliation of ONE diverged tenant clone (#602). Classify
    # against the SAME taxonomy the dashboard-backlog entry below reasons
    # over — and land on the opposite answer, because the object acted on is
    # the opposite. dashboard-backlog is authenticated_user precisely because
    # "a user curating their OWN project backlog is normal work"; this writes
    # a merge commit into ANOTHER TENANT'S git history. So:
    #   • not read_only         — it commits to a repository;
    #   • not user_safe         — user_safe tolerates no identity at all, and
    #                             an unattributable actor makes the receipt
    #                             (the whole point of the command) worthless;
    #   • not authenticated_user — that class is "your own work, your own
    #                             scope". Nothing here is the caller's own;
    #                             holding a per-user permission over one's own
    #                             project must not reach a different tenant's;
    #   • not mixed_self_gated  — there are no free read sub-modes; the single
    #                             action mutates.
    # admin_only, the class held by the control-plane governance family
    # (dashboard-set-config, migrate-control-authority, admin): the caller is
    # exercising authority over property that is not theirs, which is the
    # definition of governance rather than work. The command self-enforces via
    # _require_operator_for_admin_command and takes its receipt's ACTOR from
    # the authenticated context, never from a caller-supplied string.
    "tenant-reconcile": "admin_only",
    # Skills governance
    "dashboard-toggle-skill": "admin_only",
    "dashboard-delete-skill": "admin_only",
    # #677: upload is the only skill door that carries a BODY — instruction
    # text every later agent follows as if it were law. Same class as its
    # siblings; not authenticated_user, because a skill is not the caller's
    # own work product once it is in the catalog.
    "dashboard-upload-skill": "admin_only",
    # Gate-message governance (operator-owned refusal copy)
    "dashboard-gate-msg-set": "admin_only",
    "dashboard-gate-msg-delete": "admin_only",
    # Governed memory capture (dashboard form, #200) — the user writing their
    # own project memory through the SAME doctrine-guarded API the
    # memory_capture agent tool uses (durability rubric, sovereign guard,
    # sqlite-canonical). Guards live in MemoryStore.capture_memory.
    "dashboard-memory-capture": "user_safe",
    # Vocab / intent-token governance
    "dashboard-vocab-set": "admin_only",
    "dashboard-vocab-delete": "admin_only",
    # Degraded-state clear — operator recovery action
    "dashboard-clear-degraded": "admin_only",
    "dashboard-palace-maintenance": "admin_only",
    # Governed Bash control plane (flips security-sensitive native-shell
    # flags) — admin_only; status/profiles are read-only.
    "governed-bash-enable": "admin_only",
    "governed-bash-disable": "admin_only",
    "governed-bash-status": "read_only",
    # operator-surface: list/status/inspect/rows are read-only; the
    # --apply / --expert-set sub-actions MUTATE and self-gate via
    # _require_operator_for_admin_command inside the command. Classified
    # mixed_self_gated; a contract test pins that every mutating flag
    # routes through that auth gate (not merely "read_only by default").
    "operator-surface": "mixed_self_gated",
    "dashboard-capability-profiles": "read_only",
    # Session create / connect / delete via the dashboard governance surface
    "dashboard-delete-session": "admin_only",
    "dashboard-create-session": "admin_only",
    "dashboard-connect-session": "admin_only",
    # RBAC / freeze / escalation — admin command family.
    "admin": "admin_only",
    # ── USER/DEV-safe work surfaces (per control-plane plan §0:
    #    "USER/DEV: setup/status/doctor/sync/session/safe config") ──
    "managed-mode-set": "user_safe",  # session bind — user's own work
    "managed-mode-clear": "user_safe",  # session unbind
    "setup": "user_safe",  # plan: USER/DEV does setup
    # Runtime provisioning/doctor: USER/DEV provisions their own AIDOCS-owned
    # enforcement interpreter under ~/.aidocs/runtime (setup-adjacent). --check
    # is read-only; --fix/--rebuild install into the user's own home.
    "runtime": "user_safe",
    # Local daemon supervision (#249): the USER/DEV manages their OWN local
    # AIDOCS runtime daemon (watchdog under ~/.aidocs) — start/stop/status/run/
    # install/uninstall of the user's own process, exactly the `runtime`
    # sibling. No operator-auth token gate (cmd_service spawns directly), and
    # `service run` is a DETACHED watchdog that cannot do interactive auth —
    # so admin_only would be a FALSE label. USER/DEV-safe like setup/runtime.
    "service": "user_safe",
    "config-set": "user_safe",  # flavor-gated control-plane refusal
    # #149 out-of-band operator grant channel (`aidocs grant <tool>`). USER-safe:
    # it writes a user-intent tool grant to the operator's OWN bound managed
    # session — the identical authority as typing the grant phrase in-prompt
    # (which needs no RBAC; operator presence IS the authority). Self-scoped (no
    # cross-user/session reach) and policy-filtered at the query_gate sink (bash
    # is never grantable; raw tools cannot be sticky, #99). Not a control-plane /
    # config mutation, so NOT admin_only.
    "grant": "user_safe",
    "sync": "user_safe",
    "init": "user_safe",
    "project-registry": "user_safe",
    "dashboard-auth-token": "user_safe",  # bootstrap-token bridge
    # Password login → token mint: the auth BOUNDARY itself. It cannot
    # require a token (you need it to GET a token); the password verify
    # inside IdentityStore.login is the gate.
    # Canonical login (2026-07-25) + its compatibility aliases. Same auth-
    # BOUNDARY class: they cannot require a token, because they are how you
    # obtain one; the verify step inside each method is the gate.
    "login": "user_safe",
    "operator-login": "user_safe",
    # Desktop dashboard sign-in (1 dashboard = 1 user = bind). Same auth
    # BOUNDARY class as operator-login: password verify inside
    # IdentityStore.login is the gate; it cannot itself require a token.
    "dashboard-login": "user_safe",
    # CodeNexus OAuth → local-token stamp (#207 §3). SAME auth-boundary class:
    # it verifies the gate-attested bearer inside the command and cannot itself
    # require a local token (it is how you obtain one).
    "dashboard-login-oauth": "user_safe",
    "dashboard-auth-status": "read_only",  # validates a token
    "dashboard-auth-logout": "user_safe",  # revokes own token
    "dashboard-binding-create": "user_safe",  # host /aidocs pairing
    "dashboard-binding-list": "read_only",  # pending queue
    # #421 one-glance surface: pending + approved bindings with age/expiry;
    # --audit flags foreign-writer format drift. Pure read, review-only.
    "bindings": "read_only",
    # Binding approval is AUTHENTICATED-USER, not admin-only: any
    # authenticated operator binds their OWN host session to
    # themselves (operator_user_id = ctx.user_id). They cannot bind
    # to another user. Unauthenticated callers are refused. Forcing
    # admin approval for every session pair would contradict the
    # plan's USER/DEV session ownership.
    "dashboard-binding-approve": "authenticated_user",
    "dashboard-binding-revoke": "authenticated_user",
    # Governed backlog CRUD (2026-07-30). Classified like the binding-approve
    # siblings above and for the same reason: EVERY action — including the
    # list/get READS — refuses an unauthenticated caller and is authorized per
    # action against backlog.read / backlog.write / backlog.remove inside
    # dashboard_backlog_service.dashboard_backlog. So:
    #   • not read_only  — add/update/remove mutate the backlog store;
    #   • not mixed_self_gated — that class means "read sub-modes are FREE";
    #     here the reads are gated too, and labeling them free would be a lie
    #     that also exempts the command from the mutating-bridge registry;
    #   • not admin_only — a user curating their OWN project backlog is normal
    #     work, not admin governance (see TAURI_USER_SAFE_MUTATIONS below); a
    #     non-admin holding backlog.* may use it without admin.manage_config;
    #   • not user_safe  — user_safe tolerates no identity at all, and this
    #     command has no unauthenticated path.
    "dashboard-backlog": "authenticated_user",
    # ── Read-only ──
    "dashboard": "read_only",  # launches the Tauri UI; no mutation
    "config": "read_only",  # opens editor / prints dashboard hint
    "status": "read_only",
    "doctor": "read_only",
    "descriptors": "read_only",
    "snapshots": "read_only",
    "version": "read_only",
    "benchmark": "read_only",
}

# Admin-governance Tauri commands that USED to write sqlite directly
# from Rust. As of 2026-05-20 +2 their Rust handlers route through
# the authenticated CLI subcommands (dashboard-vocab-*,
# dashboard-gate-msg-*, dashboard-delete-session,
# dashboard-clear-degraded) with an operator token resolved via
# `resolve_operator_token`. The coverage contract test asserts the
# Rust handlers no longer contain direct rusqlite writes for these.
TAURI_ADMIN_GOVERNANCE_COMMANDS: frozenset[str] = frozenset(
    {
        "vocab_upsert_group",
        "vocab_delete_group",
        "gate_msg_upsert",
        "gate_msg_delete",
        "delete_session",
        "clear_degraded_state",
    },
)

# USER/DEV-safe Tauri mutations — a user managing their OWN project
# backlog / todos is normal work, not admin governance (plan §0:
# USER/DEV owns project/session work). These do NOT require an
# operator token; they remain direct writes intentionally.
TAURI_USER_SAFE_MUTATIONS: frozenset[str] = frozenset(
    {
        "tauri_backlog_add",
        "tauri_backlog_update",
        "tauri_backlog_remove",
        "tauri_todo_update",
        "tauri_todo_remove",
    },
)


# Commands whose COMMANDS handler is a documented wrapper that cannot be
# source-introspected meaningfully (e.g. a thin dispatch shim). EXPLICIT
# allowlist — a command is exempted from the introspection / mutation belts
# only by being listed here with a reason, never by silent skip. Empty
# today (every classified command resolves to a real, introspectable fn).
CLASSIFICATION_WRAPPER_ALLOWLIST: frozenset[str] = frozenset()


# Named write/audit/store/file mutation primitives (lowercase; matched
# case-insensitively). Presence in a read_only handler's source means it
# mutates and is mis-classified.
_NAMED_HANDLER_MUTATION_MARKERS: tuple[str, ...] = (
    "_require_operator_for_admin_command",  # admin write gate
    "_audit_admin_command_applied",  # applied-mutation audit
    "_update_project_config_value",  # config write
    "_atomic_write_text",  # file write
    ".write_text(",
    ".write_bytes(",  # file write
    "configstore().set(",
    "store.set(",  # sqlite config write
    "upsert_gate_message",
    "delete_gate_message",  # gate-msg store
    "seed_kind_rows",
    "delete_parent_rows",  # vocab store
    ".executemany(",  # batch SQL write path
)

# Raw SQL write statements — case-insensitive and whitespace/newline
# tolerant (\s spans newlines), SQL-keyword specific so SELECT-only reads
# and prose do not trip. UPDATE requires the `<table> SET` shape; the
# others require their canonical INTO/FROM/TABLE clause.
import re as _re  # noqa: E402

_SQL_WRITE_RE = _re.compile(
    r"\b(?:"
    r"insert\s+into|insert\s+or\b|replace\s+into|"
    r"delete\s+from|"
    r"update\s+\S+\s+set\b|"
    r"create\s+table|drop\s+table|alter\s+table|truncate\s+table"
    r")",
    _re.IGNORECASE,
)


def detect_handler_mutations(source: str) -> list[str]:
    """Return the mutation markers found in a handler's source — empty for a
    pure read handler. Case-insensitive, whitespace/newline tolerant, and
    SQL-aware (raw INSERT/UPDATE/DELETE/REPLACE/CREATE/DROP/ALTER/TRUNCATE
    across lowercase + multiline), plus the named store/file/config/audit
    write markers. SELECT and .get/.get_setting/list_/posture reads do not
    match.

    SOURCE-TEXT FALSE-POSITIVE POLICY (intentional, not a bug):
    This scans the handler's RAW SOURCE — comments and docstrings included.
    A write marker or raw-SQL-write SHAPE inside a comment or example WILL
    flag the handler. That is by design: a read_only handler should contain
    no mutation-looking code AND no mutation-looking comments/examples
    (`# ConfigStore().set(...)`, `# insert into t ...`). If the belt flags a
    comment, the fix is to reword/remove the comment or reclassify the
    command — do NOT loosen the detector to ignore comments. (Mutating VERBS
    in plain prose are fine: "does not update or delete anything" carries no
    SQL write SHAPE and no named marker, so it does not match — see
    test_prose_update_not_a_false_positive. Only an actual marker / SQL
    write shape, even in a comment, flags.)
    """
    src = source or ""
    src_l = src.lower()
    hits = [m for m in _NAMED_HANDLER_MUTATION_MARKERS if m in src_l]
    if _SQL_WRITE_RE.search(src):
        hits.append("raw_sql_write")
    return hits


def unintrospectable_commands(
    command_class: dict[str, str],
    commands: dict,
    *,
    wrapper_allowlist: frozenset[str] = CLASSIFICATION_WRAPPER_ALLOWLIST,
) -> dict[str, str]:
    """Classified commands that do NOT resolve to an introspectable handler
    and are not allowlisted as documented wrappers. Fails CLOSED: a command
    we cannot introspect cannot be proven clean, so it must be surfaced
    (not silently skipped). Returns {command: reason}.
    """
    import inspect

    out: dict[str, str] = {}
    for cmd in command_class:
        if cmd in wrapper_allowlist:
            continue
        fn = commands.get(cmd)
        if fn is None:
            out[cmd] = "no_handler"
            continue
        try:
            inspect.getsource(fn)
        except (OSError, TypeError):
            out[cmd] = "introspection_failed"
    return out


def read_only_mutation_offenders(
    command_class: dict[str, str],
    commands: dict,
    *,
    wrapper_allowlist: frozenset[str] = CLASSIFICATION_WRAPPER_ALLOWLIST,
) -> dict[str, list[str]]:
    """read_only commands whose handler mutates (or cannot be introspected,
    which fails closed). Returns {command: markers}. A clean read handler
    yields nothing.
    """
    import inspect

    out: dict[str, list[str]] = {}
    for cmd, cls in command_class.items():
        if cls != "read_only" or cmd in wrapper_allowlist:
            continue
        fn = commands.get(cmd)
        if fn is None:
            out[cmd] = ["no_handler"]
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            out[cmd] = ["introspection_failed"]  # fail closed
            continue
        hits = detect_handler_mutations(src)
        if hits:
            out[cmd] = hits
    return out


def mutating_cli_commands() -> frozenset[str]:
    """CLI subcommands that MUTATE state — the registry the dashboard-bridge
    contract test uses to require run_json_cli_checked, regardless of
    whether the command attaches an operator token. This is broader than
    'token-attaching': it also covers user-safe mutations (e.g.
    managed-mode-set/clear) that change state without an admin token, so a
    refused/failed one still surfaces as a UI error rather than a false
    success.

    Derived from CLI_COMMAND_CLASS: everything that is not read-only and not
    'mixed_self_gated' (mixed commands mutate only in specific flag modes;
    those flag-mode bridges are covered by the token-attaching contract).
    """
    return frozenset(
        cmd
        for cmd, cls in CLI_COMMAND_CLASS.items()
        if cls not in ("read_only", "mixed_self_gated")
    )


# Back-compat alias (kept so older callers/tests resolve). Points at
# the admin-governance set — the surfaces that needed gating.
TAURI_DIRECT_SQLITE_MUTATIONS = TAURI_ADMIN_GOVERNANCE_COMMANDS


@dataclass(frozen=True)
class OperatorContext:
    """An authenticated operator. Carry by reference across the
    sensitive write path; never construct from raw env values.
    Use ``OperatorAuthService.authenticate`` only.
    """

    user_id: str
    email: str
    role: str
    source: OperatorSource
    token_id: str  # sha256[:16] of the token hash — log-safe id
    authenticated_at: str
    permissions: tuple[str, ...] = field(default_factory=tuple)


class OperatorAuthService:
    """Stateless service. Reads from IdentityStore + RBACStore.
    Never caches authenticated state — each call re-validates.
    """

    @staticmethod
    def resolve_token_from_args(
        args: list[str],
        *,
        env_var: str = "AIDOCS_OPERATOR_TOKEN",
        flag: str = "--operator-token",
    ) -> str:
        """Pull the operator token through the ONE resolution door
        (#421): env > --operator-token flag > machine token cache
        (``operator_token_resolution``). Returns empty string when no
        source carries a value. Never raises. Expired cache rows are
        pruned on read; the resolved token is still validated against
        identity_tokens by every caller — this only picks the CANDIDATE.
        """
        try:
            from .operator_token_resolution import resolve_operator_token

            token, _source = resolve_operator_token(
                args,
                env_var=env_var,
                flag=flag,
            )
            return token
        except Exception:
            return str(os.environ.get(env_var) or "").strip()

    def authenticate(
        self,
        token: str,
        project_root: Path,
        *,
        source: OperatorSource = "dashboard",
    ) -> OperatorContext | None:
        """Resolve a bearer token to an OperatorContext or None.

        None means UNAUTHENTICATED. Callers must refuse the mutation;
        no env-identity fallback substitutes for a real token.
        """
        if not token:
            return None
        try:
            from .identity_store import IdentityStore
        except Exception:
            return None
        store = IdentityStore()
        user = store.validate_token(project_root, token)
        if user is None:
            return None
        # Derive a log-safe token id (avoid leaking the full token).
        import hashlib

        token_id = hashlib.sha256(
            token.encode("utf-8"),
        ).hexdigest()[:16]
        # Snapshot effective permissions at global scope so callers
        # can audit-render the permission set without re-querying.
        perms: tuple[str, ...] = ()
        try:
            from .rbac_store import RBACStore

            rbac = RBACStore()
            eff = rbac.effective_permissions(
                project_root,
                user.user_id,
                scope_type="global",
                scope_id=None,
            )
            if isinstance(eff, dict):
                perms = tuple(p for p, v in eff.items() if bool(v))
            else:
                perms = tuple(eff)
        except Exception:
            perms = ()
        from datetime import datetime

        return OperatorContext(
            user_id=user.user_id,
            email=user.email,
            role=user.role,
            source=source,
            token_id=token_id,
            authenticated_at=datetime.now(UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ",
            ),
            permissions=perms,
        )

    # (#404, 2026-07-16: the local-operator token auto-mint is removed —
    # operator tokens are never auto-minted for any flavor. Operators
    # sign in and present a real token / approved host binding.)

    def resolve_machine_login(self, project_root: Path) -> str:
        """Machine-level login resolution (#443): user_id of the machine's
        signed-in user, or "" when nobody is logged in.

        A ``dashboard-login`` / ``operator-login`` mint (IdentityStore.login)
        leaves a row in ``identity_tokens``; the newest live row IS the
        machine's login. Pure DB read against aidocs_identity.sqlite3 —
        NEVER reads ~/.aidocs/operator_token.json or any other file
        (operator directive: auth is DB-backed, no files). A row counts
        only while non-revoked (revocation deletes the row) and
        non-expired, and only for a non-disabled user. Fail-closed to ""
        on any error.
        """
        try:
            from ._sqlite_connect import connect as _canonical_connect
            from .identity_store import IdentityStore, _iso_now

            store = IdentityStore()
            db = store.db_path(project_root)
            if not db.is_file():
                return ""
            with _canonical_connect(str(db), row_factory=False, read_only=True) as conn:
                row = conn.execute(
                    "SELECT t.user_id "
                    "FROM identity_tokens t "
                    "JOIN identity_users u ON u.user_id = t.user_id "
                    "WHERE t.expires_at > ? AND u.disabled = 0 "
                    "ORDER BY t.issued_at DESC, t.rowid DESC "
                    "LIMIT 1",
                    (_iso_now(),),
                ).fetchone()
            return str(row[0]) if row and row[0] else ""
        except Exception:
            return ""

    def resolve_operator_context_from_host_session(
        self,
        host_session_id: str,
        project_root: Path,
    ) -> OperatorContext | None:
        """Resolve an approved host binding to a live, enabled user context.

        A binding row is not identity by itself. The referenced user must still
        exist and remain enabled on every authorization check; stale, deleted,
        disabled, or unreadable identities fail closed.
        """
        if not host_session_id:
            return None
        try:
            from .host_operator_binding_store import HostOperatorBindingStore

            uid = HostOperatorBindingStore().resolve_operator(
                project_root,
                host_session_id,
            )
        except Exception:
            return None
        if not uid:
            return None

        try:
            from .identity_store import IdentityStore

            user = IdentityStore().get_user_by_id(project_root, uid)
        except Exception:
            return None
        if user is None or user.disabled:
            return None

        role = ""
        try:
            from .identity_resolver import UNKNOWN_ROLE, current_effective_role

            role = current_effective_role(project_root, uid)
            # #576 D1: UNKNOWN_ROLE is the resolver saying "no assignment
            # matched", which is exactly the case this fallback already
            # covered when the resolver returned ''. Behavior preserved;
            # only the marker changed.
            if role == UNKNOWN_ROLE:
                role = ""
        except Exception:
            role = ""
        if not role:
            role = user.role

        perms: tuple[str, ...] = ()
        try:
            from .rbac_store import RBACStore

            eff = RBACStore().effective_permissions(
                project_root,
                uid,
                scope_type="global",
                scope_id=None,
            )
            perms = (
                tuple(p for p, value in eff.items() if bool(value))
                if isinstance(eff, dict)
                else tuple(eff)
            )
        except Exception:
            perms = ()
        return OperatorContext(
            user_id=uid,
            email=user.email,
            role=role,
            source="host_binding",
            token_id="host-bound",
            authenticated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            permissions=perms,
        )

    # ── THE WINDOW SPINE (operator ruling 2026-09-04) ──────────────────
    #
    # WHO binds to the ATTESTED STABLE WINDOW. `window_key` is
    # `<host pid>:<host creation filetime>`, stamped in the hook process by
    # `window_key.stamp_payload_window` and READ, never derived, downstream.
    # It survives what a conversation id does not: measured across two
    # /resume, one /clear and one /mcp reconnect (#880).
    #
    # THE MEASURED DEFECT this replaces. On 2026-09-04 a signed-in super_admin
    # was refused `unauthenticated_host_session` while
    # `resolve_operator_context_from_host_session` answered with their full
    # context under the PREVIOUS conversation id. The binding was sound; the
    # KEY had rotated out from under it.
    #
    # SEPARATE AXES, SEPARATE COLUMNS (empire law promoted-cc6c4ac686ee):
    # the user proves WHO, the window proves WHICH CALLER, the managed binding
    # proves WHICH SESSION. Nothing below reads a session id.

    #: A stamp written when the window was created, by a named creation path.
    WINDOW_SOURCE_STAMP = "window_stamp"
    #: An approved pairing, now keyed on the window rather than a conversation.
    WINDOW_SOURCE_BINDING = "window_binding"
    #: A remote surface's provenance is DERIVED FROM ITS CLASS, never a single
    #: shared literal. An OperatorContext carries its source into audit rows and
    #: refusals; reporting a ChatGPT conversation, a native AIDOCS edge and an
    #: OS window under one name would erase the distinction exactly where an
    #: operator reading the trail depends on it. Derived rather than enumerated
    #: so a class added later cannot be silently filed under an existing name.
    @staticmethod
    def _source_for_class(attestation_class: str) -> str:
        return f"{attestation_class}_binding"

    def resolve_operator_context_from_caller(
        self,
        attestation,
        project_root: Path,
        *,
        actor_is_derived: bool | None = None,
    ) -> OperatorContext | None:
        """The operator of THIS CALLER, or ``None``. No fallback, no widening.

        ONE RESOLVER, TWO SURFACES, AND THE SURFACE DECIDES WHICH RUNGS EXIST.

          os_window (local)    1. the WHO STAMP written when the window came
                                  into existence
                                  (``window_conversation_state.bound_user_id``)
                                  -- a plain column read, not a resolution;
                               2. an APPROVED PAIRING keyed on the caller,
                                  including a legacy row this window adopts
                                  once from its OWN attested lease.

          remote_conversation  2. the approved pairing, and ONLY that.

        THE MISSING RUNGS ON THE REMOTE SURFACE ARE THE POINT. There is no WHO
        stamp because no SessionStart hook ever fired for a caller that has no
        process on this box, and there is no legacy adoption because the remote
        surface has no pre-window rows to migrate. Supplying either would mean
        answering a remote caller from a fact measured about a local one.

        AND THEN IT STOPS, on both surfaces. No further rung: not the managed
        session, not the conversation claim on its own, not the token, and not
        ``resolve_machine_login``. The machine login is the specific
        substitution the operator forbade -- it answers "who last signed in on
        this box", so defaulting to it would silently make every remote caller
        the local operator.

        THE TOKEN IS NOT THE CALLER. The gate previously handed the executor
        ``"ogh_" + token_id``, which is the token restated: two browser tabs on
        one token are two callers reported as one, and a refresh reports one
        caller as two. The remote attestation is the COMPOSED conversation --
        the client's claim digested together with the authenticated principal
        -- so the same claim under a different principal is a different caller,
        and a claim can only ever address conversations its own principal owns.

        ``actor_is_derived`` IS THE INHERITANCE NEGATIVE, ONE LEVEL DOWN, and it
        holds on BOTH surfaces. A Claude Code subagent SHARES its parent's
        window and a lane worker descends from one, so a naive read would hand
        the operator's super_admin to every agent that caller ever spawns --
        including ones spawned with no human involved. A derived actor gets
        ``None`` and must resolve its OWN attenuated actor identity.

        THE STAMP IS ONCE; THE STANDING IS LIVE. A binding row is not identity
        by itself -- the referenced user must still exist and still be enabled
        on EVERY check, so a sign-out or a disable propagates immediately and a
        stamp never outlives the principal's authority.
        """
        from .caller_attestation import CLASS_OS_WINDOW

        # MEASURED, NOT TRUSTED. `operator_intent_resolver`'s _HUMAN_PRINCIPALS
        # guard reads a principal_type its CALLER supplied, so a Claude Code
        # subagent -- which shares the parent's window and can be stamped
        # 'human' by a window-derived caller -- would sail through it. The
        # measurement is taken here instead: `resolve_task_actor` answers
        # (actor_id, lane_id, is_worker) and returns False only for the human
        # caller, catching both the lane worker (AIDOCS_EXPERT_LANE_ID) and the
        # in-process subagent latch (protected_file_runtime.is_sub_agent_call).
        #
        # AN UNANSWERABLE MEASUREMENT IS A DERIVED ACTOR. Fail-closed: the cost
        # of a false 'derived' is one honest refusal; the cost of a false
        # 'human' is the operator's super_admin handed to an agent-spawned
        # worker with no human in the loop.
        if actor_is_derived is None:
            try:
                from .task_actor_identity import resolve_task_actor

                actor_is_derived = bool(resolve_task_actor(project_root)[2])
            except Exception:
                actor_is_derived = True
        if actor_is_derived:
            return None
        if attestation is None:
            return None

        uid, via = "", ""
        klass = getattr(attestation, "attestation_class", "")
        is_os_window = klass == CLASS_OS_WINDOW
        source = (
            self.WINDOW_SOURCE_BINDING
            if is_os_window
            else self._source_for_class(klass)
        )

        if is_os_window:
            try:
                from .window_binding_store import WindowBindingStore

                uid, via = WindowBindingStore().window_operator(
                    project_root, attestation.value
                )
            except Exception:
                uid, via = "", ""
            if uid:
                source = self.WINDOW_SOURCE_STAMP

        if not uid:
            try:
                from .host_operator_binding_store import HostOperatorBindingStore

                store = HostOperatorBindingStore()
                uid = store.resolve_operator_by_caller(project_root, attestation.key) or ""
                if not uid and is_os_window:
                    # Legacy adoption is LOCAL-ONLY: it meets a window's own
                    # attested lease with a pre-window binding row. A remote
                    # caller has neither, so there is nothing to adopt and
                    # reaching for one would be inventing a fact.
                    uid = (
                        self._adopt_legacy_binding(store, project_root, attestation.value)
                        or ""
                    )
            except Exception:
                uid = ""
        if not uid:
            return None
        return self._operator_context_for_uid(
            uid,
            project_root,
            source=source,
            token_id=f"window:{via}" if via else f"{source}-bound",
        )

    def resolve_operator_context_from_window(
        self,
        window_key: str,
        project_root: Path,
        *,
        actor_is_derived: bool | None = None,
    ) -> OperatorContext | None:
        """The operator of THIS OS WINDOW, or ``None``.

        The local surface's spelling of
        ``resolve_operator_context_from_caller``. Kept as its own name because
        most callers are local and should not have to build an attestation to
        ask the ordinary question -- and because a malformed window must be
        refused by the module that owns the shape rather than concatenated into
        a key here.
        """
        from .caller_attestation import os_window

        attestation, _reason = os_window(window_key)
        if attestation is None:
            return None
        return self.resolve_operator_context_from_caller(
            attestation, project_root, actor_is_derived=actor_is_derived
        )

    def _adopt_legacy_binding(
        self,
        store: object,
        project_root: Path,
        window: str,
    ) -> str | None:
        """One-time adoption of a pre-window binding BY THIS WINDOW ONLY.

        The conversations offered are read from THIS window's own lease row —
        the current claim and the one it displaced, both written by SessionStart
        from the host's own payload. A conversation this window merely shares a
        SESSION with is never offered, and the store's ``window_key = ''``
        guard means an already-adopted row cannot be taken a second time.
        """
        try:
            from .window_binding_store import WindowBindingStore

            row = WindowBindingStore().window_conversation(project_root, window)
        except Exception:
            return None
        if not row:
            return None
        candidates = tuple(
            c
            for c in (
                str(row.get("host_session_id") or "").strip(),
                str(row.get("previous_host_session_id") or "").strip(),
            )
            if c
        )
        if not candidates:
            return None
        try:
            return store.adopt_window(  # type: ignore[attr-defined]
                project_root,
                window_key=window,
                host_session_ids=candidates,
            )
        except Exception:
            return None

    def _operator_context_for_uid(
        self,
        uid: str,
        project_root: Path,
        *,
        source: str,
        token_id: str,
    ) -> OperatorContext | None:
        """A live context for ``uid``, or ``None``. THE STANDING CHECK.

        Shared by every window/binding resolution so the liveness rules cannot
        drift apart one caller at a time: the user must still exist and must
        still be enabled, and the role and permissions are read fresh on every
        call rather than carried on the stamp.
        """
        if not uid:
            return None
        try:
            from .identity_store import IdentityStore

            user = IdentityStore().get_user_by_id(project_root, uid)
        except Exception:
            return None
        if user is None or user.disabled:
            return None

        role = ""
        try:
            from .identity_resolver import UNKNOWN_ROLE, current_effective_role

            role = current_effective_role(project_root, uid)
            if role == UNKNOWN_ROLE:
                role = ""
        except Exception:
            role = ""
        if not role:
            role = user.role

        perms: tuple[str, ...] = ()
        try:
            from .rbac_store import RBACStore

            eff = RBACStore().effective_permissions(
                project_root,
                uid,
                scope_type="global",
                scope_id=None,
            )
            perms = (
                tuple(p for p, value in eff.items() if bool(value))
                if isinstance(eff, dict)
                else tuple(eff)
            )
        except Exception:
            perms = ()
        return OperatorContext(
            user_id=uid,
            email=user.email,
            role=role,
            source=source,
            token_id=token_id,
            authenticated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            permissions=perms,
        )

    def resolve_operator_context_from_machine_login(
        self,
        project_root: Path,
    ) -> OperatorContext | None:
        """#443 third rung: an OperatorContext for the machine's signed-in
        user (resolve_machine_login), or None when nobody is logged in.

        Mirrors resolve_operator_context_from_host_session — user row,
        effective role, and RBAC permissions resolved identically; only
        the ``source`` differs ('machine_login') so audits can tell a
        machine-login authority grant from a token or a binding. DB-backed
        end-to-end; fail-closed to None on any error.
        """
        uid = self.resolve_machine_login(project_root)
        if not uid:
            return None
        user = None
        try:
            from .identity_store import IdentityStore

            user = IdentityStore().get_user_by_id(project_root, uid)
        except Exception:
            user = None
        role = ""
        try:
            from .identity_resolver import UNKNOWN_ROLE, current_effective_role

            role = current_effective_role(project_root, uid)
            if role == UNKNOWN_ROLE:  # #576 D1 — see the host_session twin
                role = ""
        except Exception:
            role = ""
        if not role and user is not None:
            role = user.role
        perms: tuple[str, ...] = ()
        try:
            from .rbac_store import RBACStore

            eff = RBACStore().effective_permissions(
                project_root,
                uid,
                scope_type="global",
                scope_id=None,
            )
            perms = (
                tuple(p for p, v in eff.items() if bool(v)) if isinstance(eff, dict) else tuple(eff)
            )
        except Exception:
            perms = ()
        return OperatorContext(
            user_id=uid,
            email=(user.email if user is not None else ""),
            role=role,
            source="machine_login",
            token_id="machine-login",
            authenticated_at=datetime.now(UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ",
            ),
            permissions=perms,
        )

    def auth_status(
        self,
        token: str,
        project_root: Path,
    ) -> dict:
        """Validate a token and return its operator status. Used by the
        dashboard auth-status hook so the UI can show "signed in as
        <role>" without re-minting.

        Returns {authenticated, auth_mode, login_required, user_id, email,
        role, flavor}. #404: login is unconditionally required — there is
        no "local_mintable" state; unauthenticated is always
        "login_required" on EVERY flavor.
        """
        ctx = self.authenticate(token, project_root) if token else None
        if ctx is not None:
            return {
                "authenticated": True,
                "auth_mode": "authenticated",
                "login_required": False,
                "user_id": ctx.user_id,
                "email": ctx.email,
                "role": ctx.role,
                "flavor": _auth_status_flavor(),
            }
        flavor = _auth_status_flavor()
        return {
            "authenticated": False,
            "auth_mode": "login_required",
            "login_required": True,
            "user_id": "",
            "role": "",
            "flavor": flavor,
            "message": "Login required — sign in to obtain an operator token.",
        }

    def logout(self, token: str, project_root: Path) -> dict:
        """Revoke a single operator token (dashboard logout / app
        exit) and GC any expired rows. Returns {revoked, purged}.
        """
        revoked = False
        purged = 0
        try:
            from .identity_store import IdentityStore

            store = IdentityStore()
            if token:
                revoked = store.revoke_token(project_root, token)
            purged = store.purge_expired_tokens(project_root)
        except Exception:
            pass
        return {"revoked": bool(revoked), "purged": int(purged)}

    def require_permission(
        self,
        ctx: OperatorContext | None,
        permission: str,
        project_root: Path,
        *,
        scope_type: str = "global",
        scope_id: str | None = None,
    ) -> bool:
        """Authoritative permission check. Returns True iff ``ctx``
        is authenticated AND RBAC grants ``permission`` at the
        requested scope.

        ``ctx is None`` ALWAYS returns False — there is no
        env-fallback path. The identity_resolver surface is for
        audit attribution only.
        """
        if ctx is None or not ctx.user_id:
            return False
        try:
            from .rbac_store import RBACStore

            return RBACStore().has_permission(
                project_root,
                ctx.user_id,
                permission,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        except Exception:
            # Fail closed on RBAC errors — never grant on lookup hiccup.
            return False

    def authorize_admin_command(
        self,
        ctx: OperatorContext | None,
        project_root: Path,
        *,
        permission: str = "admin.manage_config",
        scope_type: str = "project",
        scope_id: str | None = None,
    ) -> tuple[bool, str]:
        """Gate for admin-only CLI commands that aren't a single config
        setting (skill toggle/delete, managed-mode set/clear, setup,
        RBAC). Returns ``(allowed, reason)``.

          ctx is None         → (False, 'unauthenticated')
          missing permission  → (False, 'missing_<permission>')
          otherwise           → (True, 'ok')
        """
        if ctx is None:
            return False, "unauthenticated"
        ok = self.require_permission(
            ctx,
            permission,
            project_root,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if not ok:
            return False, f"missing_{permission.replace('.', '_')}"
        return True, "ok"

    def authorize_config_mutation(
        self,
        ctx: OperatorContext | None,
        setting_path: str,
        project_root: Path,
        *,
        scope_type: str = "project",
        scope_id: str | None = None,
    ) -> tuple[bool, str]:
        """High-level gate for config mutations from the dashboard
        write path. Returns ``(allowed, reason)``.

        Rules:
          1. operator_only OR security_sensitive settings require an
             authenticated operator AND ``admin.manage_config``
             permission.
          2. Safe (catalog-defined operator_editable) settings still
             require SOME authenticated operator — the dashboard
             surface is admin-only, never anonymous. Use the CLI
             ``config-set`` subcommand for unauthenticated agent-
             editable scope writes.
          3. Unknown settings → refused (let the catalog-validation
             layer surface a clearer message).

        The previous ``dashboard=True`` flag is REMOVED from the
        authority signal — it remains as a parameter shape for
        catalog-validation bypass on legacy bash.* paths only.
        """
        try:
            from .config_schema import SETTINGS_CATALOG
        except Exception:
            return False, "config_schema_unavailable"
        meta = SETTINGS_CATALOG.get(setting_path)
        if meta is None:
            return False, "unknown_setting"
        if ctx is None:
            return False, "unauthenticated"

        operator_only = bool(meta.get("dashboard_only"))
        sensitive = bool(meta.get("security_sensitive"))
        if operator_only or sensitive:
            ok = self.require_permission(
                ctx,
                "admin.manage_config",
                project_root,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if not ok:
                return False, "missing_admin_manage_config"
        # Safe settings still require an authenticated dashboard
        # operator. CLI safe writes use the un-tokened agent-
        # editable path through cmd_config_set in solo/corpo.
        return True, "ok"
