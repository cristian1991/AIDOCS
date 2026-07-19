"""Tier-M surgical-edit bridge for the Outer Gate — confirmation-gated, audited.

This is the FIRST mutation path through the gate. It is deliberately NOT part of
``OuterGate.invoke`` (which stays sealed Tier-R-only); it is a SEPARATE two-phase
flow so the read surface and its guards are untouched.

Sealed properties (all fail-closed):
  * ONLY canonical surgical tools on an explicit allowlist (``ai_str_replace``);
    never raw shell, never broad/whole-file writes. Mutation runs through the
    canonical ``file_ops.str_replace`` engine — the same code the local tool uses.
  * Operator authority: the token's LIVE role must be an operator/admin role AND
    carry the ``tier_m_edit`` scope (never granted to OAuth/connector tokens).
  * Exact project binding + path confinement: the target path must resolve INSIDE
    the configured exec project. No request value can mutate outside it.
  * Signed package trust + non-stale, populated index are preconditions (reused
    from the read executor); stale/unindexed/untrusted ⇒ refuse.
  * TWO-PHASE confirmation: phase 1 (propose) computes a diff WITHOUT writing,
    durably records a PRE-MUTATION INTENT audit, and returns a single-use,
    content-bound confirmation token. Phase 2 (commit) requires that exact token,
    re-checks everything, records a MUTATION audit, runs the engine, REINDEXES
    (fail-closed), and records a RESULT audit. An edit can never execute without a
    prior audited proposal of the exact same diff.

Until a standing scoped-grant model is sealed, EVERY edit requires this two-phase
confirmation — there is no auto-approve.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .outer_gate_executor import (
    _index_populated,
    _is_aidocs_project,
    _is_stale,
    exec_project_id,
)

# The ONLY tools this bridge will ever execute. Surgical, single-file, reversible
# string replacement. Expanding this is a deliberate, reviewed act.
EDIT_ALLOWLIST = frozenset({"ai_str_replace"})


def _extended_edit_allowlist() -> frozenset[str]:
    """Legacy ai_str_replace + every registry-declared EDIT tool whose
    surface puts it on the gate.

    Doctrine (2026-05-29): GATE_ONLY tools are gate-advertised by
    design — they reflect a staged migration where the gate publishes
    the consolidator (e.g. ai_lane) while stdio still binds the legacy
    children (ai_lane_*). The edit-execute path MUST include them or
    callers see "ai_lane advertised in tools/list but rejected at
    tools/call" — exactly the surface-lie class of bug the GATE_ONLY
    state was introduced to prevent.

    The registry entries DON'T go through the propose/commit pipeline
    below — they route through `OuterGate._registry_invoke_edit` which
    binds the exec project root and dispatches via fastmcp's
    `call_tool`. The underlying impls enforce their own audit + path
    protection + size limits; the gate only adds scope check +
    exec_root binding + two-phase confirm (per registry spec).
    """
    try:
        from . import tool_interface as _ti

        return EDIT_ALLOWLIST | frozenset(
            n
            for n, s in _ti.REGISTRY.items()
            if s.surface in (_ti.BOTH, _ti.GATE_ONLY) and s.cls == _ti.EDIT
        )
    except Exception:
        return EDIT_ALLOWLIST


EDIT_ALLOWLIST = _extended_edit_allowlist()

# The LIVE role required to edit (resolved at validate time, not the token stamp).
EDIT_ROLES = frozenset({"operator", "co_conductor", "admin", "super_admin"})

_CONFIRM_TTL_SECONDS = 300
_DIFF_AUDIT_CAP = 8000  # store at most this many diff chars inline; always hash


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_now() -> str:
    return _iso(_now())


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EditRefusal(Exception):
    blocked_by: str
    reason: str = ""

    def __str__(self) -> str:  # dataclass + Exception friendliness
        return self.reason or self.blocked_by


# ── path confinement ─────────────────────────────────────────────────────────
def resolve_in_project(exec_root: Path | str, rel_path: str) -> Path:
    """Resolve rel_path INSIDE exec_root. Rejects absolute paths and any traversal
    that escapes the project (fail-closed). Returns the resolved absolute path.
    """
    root = Path(exec_root).resolve()
    p = root / rel_path
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as e:
        raise EditRefusal("bad_path", f"unresolvable path: {e}")
    if resolved != root and root not in resolved.parents:
        raise EditRefusal("bad_path", f"path {rel_path!r} escapes the exec project")
    return resolved


# ── diff preview (no write) ──────────────────────────────────────────────────
def _content_binding(tool: str, rel_path: str, args: dict) -> str:
    """Stable hash binding a confirmation to the EXACT proposed edit, so a commit
    cannot deviate from what was proposed/audited.
    """
    payload = json.dumps(
        {
            "tool": tool,
            "path": rel_path,
            "old": args.get("old_string"),
            "new": args.get("new_string"),
            "replace_all": bool(args.get("replace_all", False)),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return _sha(payload)


def compute_str_replace_preview(abs_path: Path, rel_path: str, args: dict) -> dict:
    """Compute the unified diff of an ai_str_replace WITHOUT writing. Fail-closed
    on missing file, absent old_string, or non-unique match (unless replace_all).
    """
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or new is None:
        raise EditRefusal("invalid_args", "old_string and new_string are required")
    if not abs_path.is_file():
        raise EditRefusal("bad_path", f"file not found: {rel_path}")
    try:
        before = abs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise EditRefusal("bad_path", f"unreadable file: {e}")
    count = before.count(old)
    if count == 0:
        raise EditRefusal("no_match", "old_string not found in file")
    if count > 1 and not bool(args.get("replace_all", False)):
        raise EditRefusal(
            "ambiguous_match",
            f"old_string occurs {count}× — pass replace_all or narrow it",
        )
    after = before.replace(old, new) if args.get("replace_all") else before.replace(old, new, 1)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        ),
    )
    return {"diff": diff, "replacements": (count if args.get("replace_all") else 1)}


# ── confirmation store (single-use, content-bound, short-lived) ──────────────
class EditConfirmStore:
    def db_path(self, project_root: Path) -> Path:
        from .identity_store import IdentityStore

        return IdentityStore().db_path(project_root)

    def init_db(self, project_root: Path) -> None:
        from .identity_store import IdentityStore

        IdentityStore().init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS edit_confirmations (
                    confirm_hash TEXT PRIMARY KEY,
                    operator TEXT NOT NULL,
                    exec_project_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_binding TEXT NOT NULL,
                    diff_sha TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    confirm_id TEXT
                )
                """,
            )
            # Migration: add the public low-entropy handle column to a pre-existing
            # table (idempotent). The OLD high-entropy confirm_token still validates
            # via confirm_hash for any in-flight legacy proposal.
            try:
                conn.execute("ALTER TABLE edit_confirmations ADD COLUMN confirm_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()

    def issue(
        self,
        project_root: Path,
        *,
        operator: str,
        exec_pid: str,
        tool: str,
        path: str,
        content_binding: str,
        diff_sha: str,
    ) -> str:
        """Issue a PUBLIC, LOW-ENTROPY, NON-BEARER confirmation handle
        (`editconf-<8hex>`). It is NOT a secret: the security boundary is the
        authenticated operator + exact operator/project/tool/path/content binding
        + single-use + TTL re-checked at commit — NOT the handle's unguessability.
        A short id-shaped handle (vs the old high-entropy `ogec_…`) avoids being
        misclassified as an AccessToken by MCP hosts. The PK confirm_hash is
        derived from the handle so legacy `confirm_hash` lookups still function.
        """
        eid = "editconf-" + secrets.token_hex(4)
        issued = _now()
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO edit_confirmations (confirm_hash, operator, "
                "exec_project_id, tool, path, content_binding, diff_sha, "
                "issued_at, expires_at, confirm_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _sha(eid),
                    operator,
                    exec_pid,
                    tool,
                    path,
                    content_binding,
                    diff_sha,
                    _iso(issued),
                    _iso(issued + _CONFIRM_TTL_SECONDS),
                    eid,
                ),
            )
            conn.commit()
        return eid

    def consume(
        self,
        project_root: Path,
        token: str,
        *,
        operator: str,
        exec_pid: str,
        tool: str,
        path: str,
        content_binding: str,
    ) -> bool:
        """Single-use. True ONLY if the handle exists, is unexpired/unconsumed, and
        EXACTLY matches operator+project+tool+path+content (fail-closed). Accepts
        BOTH the new low-entropy handle (matched on confirm_id) AND a legacy
        high-entropy confirm_token (matched on confirm_hash=sha(token)).
        """
        if not token:
            return False
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            # New handle: direct confirm_id match. Legacy token: confirm_hash match.
            row = conn.execute(
                "SELECT * FROM edit_confirmations WHERE confirm_id=? OR confirm_hash=?",
                (token, _sha(token)),
            ).fetchone()
            if row is None or int(row["consumed"]):
                return False
            if str(row["expires_at"]) <= _iso_now():
                return False
            if (
                str(row["operator"]) != operator
                or str(row["exec_project_id"]) != exec_pid
                or str(row["tool"]) != tool
                or str(row["path"]) != path
                or str(row["content_binding"]) != content_binding
            ):
                return False
            conn.execute(
                "UPDATE edit_confirmations SET consumed=1 WHERE confirm_hash=?",
                (str(row["confirm_hash"]),),
            )
            conn.commit()
            return True


# ── authorization + binding checks shared by both phases ─────────────────────
def _authorize(principal: dict, tool: str) -> None:
    from .outer_gate_scopes import SCOPE_TIER_M_EDIT

    role = str((principal or {}).get("effective_role") or "").strip().lower()
    if role not in EDIT_ROLES:
        raise EditRefusal(
            "edit_role_required",
            f"role {role or '∅'} cannot edit (operator/admin only)",
        )
    scope = set((principal or {}).get("scope") or [])
    if SCOPE_TIER_M_EDIT not in scope:
        raise EditRefusal("insufficient_scope", f"token lacks {SCOPE_TIER_M_EDIT} scope")
    if tool not in EDIT_ALLOWLIST:
        raise EditRefusal(
            "tool_not_edit_allowlisted",
            f"{tool} is not an allowlisted surgical edit tool",
        )


def _check_binding(exec_root: Path) -> None:
    if not _is_aidocs_project(exec_root):
        raise EditRefusal(
            "project_not_aidocs",
            f"exec project {exec_root} is not an AIDOCS project",
        )
    if not _index_populated(exec_root):
        raise EditRefusal(
            "project_unindexed",
            f"exec project {exec_root} has no populated code index",
        )
    if _is_stale(exec_root):
        raise EditRefusal("project_index_stale", f"exec project {exec_root} index is stale")


def _audit(
    sink,
    event: str,
    *,
    principal: dict,
    exec_root: Path,
    tool: str,
    path: str,
    diff: str = "",
    verdict: str = "",
    result: str = "",
) -> None:
    if sink is None:
        raise EditRefusal("audit_not_configured", "edit refused: no durable audit sink configured")
    diff_sha = _sha(diff) if diff else ""
    event_row = {
        "event": event,
        "operator": (principal or {}).get("user_id") or "",
        "session": (principal or {}).get("token_id") or "",
        "exec_project_root": str(exec_root),
        "exec_project_id": exec_project_id(exec_root),
        "tool": tool,
        "path": path,
        "diff": (diff[:_DIFF_AUDIT_CAP] if diff else ""),
        "diff_sha": diff_sha,
        "verdict": verdict,
        "result": result,
    }
    try:
        sink(event_row)
    except Exception as e:  # noqa: BLE001 — a mutation that can't be audited fails
        raise EditRefusal("audit_failed", f"edit refused: durable audit failed: {e}")


# ── phase 1: propose (diff + intent audit + confirm token) ───────────────────
def propose(
    *,
    project_root: Path,
    exec_root: Path,
    principal: dict,
    tool: str,
    args: dict,
    audit_sink,
) -> dict:
    _authorize(principal, tool)
    _check_binding(exec_root)
    rel = str(args.get("path") or "")
    if not rel:
        raise EditRefusal("invalid_args", "path is required")
    abs_path = resolve_in_project(exec_root, rel)
    preview = compute_str_replace_preview(abs_path, rel, args)
    diff = preview["diff"]
    # PRE-MUTATION intent audit (fail-closed: no audit ⇒ no proposal).
    _audit(
        audit_sink,
        "outer_gate_edit_intent",
        principal=principal,
        exec_root=exec_root,
        tool=tool,
        path=rel,
        diff=diff,
        verdict="proposed",
    )
    # CANONICAL CONSUMABLE CONFIRM (sealed design §3c) — same authority the
    # registry EDIT path uses, so EVERY edit route shares one consumable-token
    # contract. Single-use, TTL, bound to (operator, project, session, tool,
    # normalized args, mutation intent=diff_sha). The diff is the intent: a file
    # changed since proposal yields a different diff → confirm_mismatch at commit.
    import time as _time

    from . import canonical_invocation as _ci
    from .identity_store import IdentityStore

    _store = _ci.ConfirmStore(IdentityStore().db_path(project_root))
    _session = str(principal.get("session_id") or principal.get("token_id") or "")
    _norm = _ci.normalize_args(
        {k: v for k, v in args.items() if k not in ("confirm_token", "edit_confirmation_id")},
    )
    token = _store.issue(
        operator=str(principal.get("user_id") or ""),
        project_id=exec_project_id(exec_root),
        session_id=_session,
        tool=tool,
        normalized_args=_norm,
        intent=_sha(diff),
        now=_time.time(),
    )
    return {
        "ok": True,
        "phase": "proposed",
        "tool": tool,
        "path": rel,
        "diff": diff,
        "replacements": preview["replacements"],
        "edit_confirmation_id": token,
        "expires_in": _CONFIRM_TTL_SECONDS,
        "note": "review the diff, then call again with the SAME args plus "
        "edit_confirmation_id to apply",
    }


# ── phase 2: commit (validate confirm → mutate → reindex → result audit) ─────
def commit(
    *,
    project_root: Path,
    exec_root: Path,
    principal: dict,
    tool: str,
    args: dict,
    confirm_token: str,
    audit_sink,
) -> dict:
    _authorize(principal, tool)
    _check_binding(exec_root)
    rel = str(args.get("path") or "")
    if not rel:
        raise EditRefusal("invalid_args", "path is required")
    abs_path = resolve_in_project(exec_root, rel)
    # Re-preview BEFORE consume so the token binding (intent=diff_sha) reflects
    # the CURRENT file: a file changed since proposal yields a different diff, so
    # the canonical consume returns confirm_mismatch (named) instead of a silent
    # stale apply.
    preview = compute_str_replace_preview(abs_path, rel, args)
    diff = preview["diff"]
    import time as _time

    from . import canonical_invocation as _ci
    from .identity_store import IdentityStore

    _store = _ci.ConfirmStore(IdentityStore().db_path(project_root))
    _session = str(principal.get("session_id") or principal.get("token_id") or "")
    _norm = _ci.normalize_args(
        {k: v for k, v in args.items() if k not in ("confirm_token", "edit_confirmation_id")},
    )
    _refusal = _store.consume(
        str(confirm_token or ""),
        operator=str(principal.get("user_id") or ""),
        project_id=exec_project_id(exec_root),
        session_id=_session,
        tool=tool,
        normalized_args=_norm,
        intent=_sha(diff),
        now=_time.time(),
    )
    if _refusal is not None:
        # Canonical named reasons (confirm_unknown/replayed/expired/cross_context/
        # mismatch). Preserve the legacy alias so any caller asserting the old
        # umbrella reason still matches.
        _alias = "unconfirmed_edit"
        raise EditRefusal(_refusal.blocked_by, f"{_alias}: {_refusal.reason}")
    # MUTATION audit BEFORE the write (intent-to-execute, durable).
    _audit(
        audit_sink,
        "outer_gate_edit_mutation",
        principal=principal,
        exec_root=exec_root,
        tool=tool,
        path=rel,
        diff=diff,
        verdict="executing",
    )
    # Run the CANONICAL surgical engine (same code the local tool uses).
    from .file_ops import str_replace as _engine

    res = _engine(
        exec_root,
        rel,
        args["old_string"],
        args["new_string"],
        replace_all=bool(args.get("replace_all", False)),
    )
    if not res.get("success"):
        _audit(
            audit_sink,
            "outer_gate_edit_result",
            principal=principal,
            exec_root=exec_root,
            tool=tool,
            path=rel,
            verdict="failed",
            result=str(res.get("error") or "edit_failed"),
        )
        raise EditRefusal("edit_failed", str(res.get("error") or "edit failed"))
    # Post-edit REINDEX — fail closed if it can't refresh (no silent stale index).
    reindex_ok, reindex_note = _reindex(exec_root, rel)
    if not reindex_ok:
        _audit(
            audit_sink,
            "outer_gate_edit_result",
            principal=principal,
            exec_root=exec_root,
            tool=tool,
            path=rel,
            verdict="reindex_failed",
            result=reindex_note,
        )
        raise EditRefusal(
            "reindex_failed",
            f"file modified but index refresh failed: {reindex_note}",
        )
    _audit(
        audit_sink,
        "outer_gate_edit_result",
        principal=principal,
        exec_root=exec_root,
        tool=tool,
        path=rel,
        diff=diff,
        verdict="committed",
        result="success",
    )
    return {
        "ok": True,
        "phase": "committed",
        "tool": tool,
        "path": rel,
        "replacements": int(res.get("replacements") or 0),
        "lines_changed": int(res.get("lines_changed") or 0),
        "reindexed": reindex_note,
    }


def apply(
    *,
    project_root: Path,
    exec_root: Path,
    principal: dict,
    tool: str,
    args: dict,
    audit_sink,
) -> dict:
    """SINGLE-CALL edit — YOLO with law. The agent submits intent ONCE; AIDOCS
    carries the whole two-phase law INTERNALLY (diff preview + intent audit +
    binding + canonical engine protected/sensitive/self-edit refusals + mutation
    audit + reindex + result audit) and applies the edit in this one call. No
    public confirmation handle is ever returned or required — the propose handle
    is minted and consumed entirely server-side, so the model never receives or
    resends token-like material.

    Outcomes:
      * SAFE      → applied, phase=committed (forbidden refusals raise EditRefusal
                    from the canonical engine: protected/gate-config/sensitive/
                    self-edit/out-of-project/no_match/ambiguous).
      * FORBIDDEN → EditRefusal (hard, fail-closed).
    Confirmable-class actions (today: dangerous RUN commands, not surgical edits)
    are governed by the canonical freeze_service operator-approval card — an
    internal pending state resolved by the OPERATOR, never a model-carried token.
    Surgical str_replace law is binary (safe-apply / hard-forbid), so no per-edit
    freeze is minted here; the freeze path remains available for any future
    confirmable edit class.
    """
    pr = propose(
        project_root=project_root,
        exec_root=exec_root,
        principal=principal,
        tool=tool,
        args=dict(args),
        audit_sink=audit_sink,
    )
    handle = pr["edit_confirmation_id"]  # internal only — never surfaced
    return commit(
        project_root=project_root,
        exec_root=exec_root,
        principal=principal,
        tool=tool,
        args=dict(args),
        confirm_token=handle,
        audit_sink=audit_sink,
    )


def _reindex(exec_root: Path, rel: str) -> tuple[bool, str]:
    """Reindex the edited file via the canonical code index. Non-indexed
    extensions legitimately sync 0 rows (treated as ok). Any error ⇒ fail.
    """
    _INDEXED = {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".cs",
        ".cshtml",
        ".go",
        ".rs",
        ".java",
        ".dart",
        ".swift",
        ".kt",
        ".scala",
        ".rb",
        ".php",
    }
    canonical = rel.replace("\\", "/").strip()
    if Path(canonical).suffix.lower() not in _INDEXED:
        return True, "non_indexed_ext"
    try:
        from .code_index_store import CodeIndexStore

        synced = int(CodeIndexStore().sync_code_files(Path(exec_root), paths=[canonical]))
    except Exception as e:  # noqa: BLE001
        return False, f"reindex_error:{e}"
    if synced <= 0:
        return False, "reindex_no_rows"
    return True, f"reindexed:{synced}"
