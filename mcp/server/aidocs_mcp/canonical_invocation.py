"""Canonical mutation invocation + consumable confirmation.

Sealed design §3c (exact-confirm token binding) + §4a (one invocation service).

ONE service that BOTH direct wrappers and `aidocs_call` route mutations through,
so the two routes are provably equivalent (same handler, same confirm authority)
and a confirm token issued on either route is consumable exactly once across both.

The confirm token is:
  * single-use (consumed on first success; replay denied),
  * TTL-bound (expiry denied),
  * cryptographically bound to the EXACT invocation identity —
    (operator, project, session, tool, normalized arguments, mutation intent) —
    so it cannot be replayed against a different operator/project/session/tool
    (cross-context) or a mutated argument set (mismatch).

All refusals are NAMED and fail-closed (default deny on any uncertainty).

This module is the AUTHORITY. Projections (the gate edit path, the local
wrappers) migrate onto it incrementally; it does not change the public registry
contract (`public_schema(spec)` derivation and aliases are untouched).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CONFIRM_TTL_SECONDS = 300

# ── Named, closed-set refusal reasons (fail-closed) ─────────────────
CONFIRM_REQUIRED = "confirm_required"  # missing token on a gated tool → phase one
CONFIRM_UNKNOWN = "confirm_unknown"  # token not found
CONFIRM_REPLAYED = "confirm_replayed"  # already consumed (single-use violation)
CONFIRM_EXPIRED = "confirm_expired"  # past TTL
CONFIRM_CROSS_CONTEXT = "confirm_cross_context"  # operator/project/session/tool differ
CONFIRM_MISMATCH = "confirm_mismatch"  # normalized args / intent differ (mutated op)


@dataclass(frozen=True)
class Refusal:
    blocked_by: str
    reason: str


def normalize_args(args: dict | None) -> str:
    """Canonical, stable serialization of host-supplied arguments. Stable key
    order + compact separators so the SAME logical operation hashes identically
    and any mutation changes the hash (binding defeats replay-against-mutation)."""
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


def _bind_hash(
    *,
    operator: str,
    project_id: str,
    session_id: str,
    tool: str,
    normalized_args: str,
    intent: str,
) -> str:
    h = hashlib.sha256()
    for part in (operator, project_id, session_id, tool, normalized_args, intent):
        h.update(b"\x1f")
        h.update(str(part).encode("utf-8"))
    return h.hexdigest()


class ConfirmStore:
    """SQLite-backed single-use confirm-token store bound to the full invocation
    identity. Route-agnostic: a token issued on the direct route is consumable on
    the aidocs_call route and vice-versa (one service), but ONLY for the identical
    binding — any difference fails closed with a named reason."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS canonical_confirmations (
                    token TEXT PRIMARY KEY,
                    bind_hash TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0
                )
                """,
            )
            conn.commit()

    def issue(
        self,
        *,
        operator: str,
        project_id: str,
        session_id: str,
        tool: str,
        normalized_args: str,
        intent: str,
        now: float,
        ttl: int = CONFIRM_TTL_SECONDS,
    ) -> str:
        """Mint a single-use handle bound to the exact invocation identity."""
        self.init_db()
        token = "editconf-" + secrets.token_hex(4)
        bind = _bind_hash(
            operator=operator,
            project_id=project_id,
            session_id=session_id,
            tool=tool,
            normalized_args=normalized_args,
            intent=intent,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO canonical_confirmations (token, bind_hash, operator, "
                "project_id, session_id, tool, args_hash, intent, issued_at, "
                "expires_at, consumed) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (
                    token,
                    bind,
                    operator,
                    project_id,
                    session_id,
                    tool,
                    hashlib.sha256(normalized_args.encode()).hexdigest(),
                    intent,
                    now,
                    now + ttl,
                ),
            )
            conn.commit()
        return token

    def consume(
        self,
        token: str,
        *,
        operator: str,
        project_id: str,
        session_id: str,
        tool: str,
        normalized_args: str,
        intent: str,
        now: float,
    ) -> Refusal | None:
        """Single-use. Returns None (consumed OK) ONLY when the token exists, is
        unexpired, unconsumed, and matches the EXACT binding. Otherwise a NAMED
        fail-closed Refusal. The check order makes each failure attributable:
        unknown → replayed → expired → cross-context → mismatch."""
        if not token:
            return Refusal(CONFIRM_REQUIRED, "no confirm_token presented")
        self.init_db()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM canonical_confirmations WHERE token=?",
                (token,),
            ).fetchone()
            if row is None:
                return Refusal(CONFIRM_UNKNOWN, "confirm_token not recognized")
            if int(row["consumed"]):
                return Refusal(CONFIRM_REPLAYED, "confirm_token already consumed (single-use)")
            if float(row["expires_at"]) <= now:
                return Refusal(CONFIRM_EXPIRED, "confirm_token expired")
            # Cross-context: identity fields differ (incl. a token reused across
            # a different tool / project / session / operator).
            if (
                str(row["operator"]) != operator
                or str(row["project_id"]) != project_id
                or str(row["session_id"]) != session_id
                or str(row["tool"]) != tool
            ):
                return Refusal(CONFIRM_CROSS_CONTEXT, "confirm_token bound to a different context")
            # Mismatch: same context but mutated arguments / intent.
            expected = _bind_hash(
                operator=operator,
                project_id=project_id,
                session_id=session_id,
                tool=tool,
                normalized_args=normalized_args,
                intent=intent,
            )
            if str(row["bind_hash"]) != expected:
                return Refusal(CONFIRM_MISMATCH, "confirm_token bound to different arguments/intent")
            # All good — consume single-use.
            conn.execute(
                "UPDATE canonical_confirmations SET consumed=1 WHERE token=?",
                (token,),
            )
            conn.commit()
            return None


def invoke(
    *,
    tool: str,
    public_args: dict,
    handler,
    operator: str,
    project_id: str,
    session_id: str,
    intent: str = "",
    requires_confirm: bool,
    confirm_token: str | None,
    store: ConfirmStore,
    now: float,
):
    """THE canonical invocation path. Direct wrappers and aidocs_call BOTH call
    this — the only place "name + args → result" happens for a mutation.

    Two-phase for `requires_confirm` tools:
      phase one (no token)  → mint a token bound to THIS exact operation; return
                              {_error: confirm_required, confirm_token}.
      phase two (token)     → consume single-use, binding-checked; on success run
                              the handler exactly once; else NAMED refusal.
    Non-confirm tools run the handler directly. The handler is the SAME object on
    both routes, so direct==aidocs_call equivalence holds by construction.
    """
    norm = normalize_args(public_args)
    if requires_confirm:
        if not confirm_token:
            token = store.issue(
                operator=operator,
                project_id=project_id,
                session_id=session_id,
                tool=tool,
                normalized_args=norm,
                intent=intent,
                now=now,
            )
            return {
                "_error": CONFIRM_REQUIRED,
                "_detail": f"{tool} is confirmation-gated; re-invoke with confirm_token",
                "tool": tool,
                "confirm_token": token,
            }
        refusal = store.consume(
            confirm_token,
            operator=operator,
            project_id=project_id,
            session_id=session_id,
            tool=tool,
            normalized_args=norm,
            intent=intent,
            now=now,
        )
        if refusal is not None:
            return {"_error": refusal.blocked_by, "_detail": refusal.reason, "tool": tool}
    return handler(**public_args)
