"""Governed deletion — deletion as an audited, reversible mutation.

Doctrine: deleting a file is a destructive MUTATION, never "cleanup". Every
governed delete is classified, run through the CheckpointService restore-point
contract, and audited with a truthful terminal outcome. Blind ``unlink`` of a
nontrivial file is never allowed.

Routing by classification:
  * PROTECTED / control-authority (forbidden-class paths, secrets, .git
    internals, .MEMORY control plane, DO-NOT-TOUCH sentinel files) → REFUSED,
    unless an explicit doctrine route (``allow_protected_route=True``) is
    passed by a caller that has its own authority; even then the file is
    checkpointed first.
  * REGENERABLE (AIDOCS-owned temp/generated/cache, proven by an ownership/
    regeneration pattern) → hard-deleted, recorded manifest-owned. No restore
    copy is needed because the bytes are reproducible.
  * NONTRIVIAL (source / unknown / everything else) → a restore point is
    required before removal:
       - git-recoverable (tracked + clean)  → GIT checkpoint, then delete
         (outcome ``checkpointed``; recoverable from HEAD).
       - otherwise (untracked / dirty / no repo) → QUARANTINE-move
         (outcome ``quarantined``; manifest-owned bytes).
    If the restore point cannot be secured, the op DOWNGRADES
    (quarantine → refuse) — it never blind-deletes.

Audit outcomes are distinct and truthful: ``refused`` | ``checkpointed`` |
``quarantined`` | ``deleted`` | ``restored``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .checkpoint_service import (
    CheckpointService,
    checkpoint_before_edit,
    safe_relpath,
)
from .protected_paths_classifier import (
    CLASS_CONFIRMABLE_GIT,
    CLASS_SECRETS_GATED,
    FORBIDDEN_CLASSES,
    classify_path,
)

# ── deletion classes ────────────────────────────────────────────────
CAT_PROTECTED = "protected"
CAT_REGENERABLE = "regenerable"
CAT_PROJECTION = "projection"  # generated projection; regenerable IF provable
CAT_NONTRIVIAL = "nontrivial"

# ── audit outcomes ──────────────────────────────────────────────────
OUTCOME_REFUSED = "refused"
OUTCOME_CHECKPOINTED = "checkpointed"
OUTCOME_QUARANTINED = "quarantined"
OUTCOME_DELETED = "deleted"
OUTCOME_RESTORED = "restored"

# Edit-checkpoint metadata statuses (destructive-mutation convergence).
EDIT_CHECKPOINTED = "checkpointed"
EDIT_SKIPPED_REGENERABLE = "skipped_regenerable"
EDIT_UNAVAILABLE = "checkpoint_unavailable"

# A nontrivial (non-regenerable) destructive delete must carry a meaningful
# intent string. Owned temp/generated cleanup is exempt (ergonomic).
_MIN_REASON_LEN = 6


def _meaningful_reason(reason: str) -> bool:
    return len((reason or "").strip()) >= _MIN_REASON_LEN


# Control-authority / control-plane prefixes that are NEVER routine deletes
# (the SQLite authority DB, the gate config, the AIDOCS marker dir, AND the
# checkpoint store itself — destroying an active checkpoint manifest would
# silently void a restore point). These back the same guards file_ops
# enforces for writes — deletion must not be a side door around them.
# Checkpoints age out ONLY through the audited CheckpointService.gc() law.
_CONTROL_AUTHORITY_PREFIXES = (
    ".memory/.index/",
    ".memory/config/",
    ".memory/.aidocs/",
    ".memory/.checkpoints/",
    # Trusted-code boundary: the AIDOCS owned runtime, the runtime manifest
    # (runtime.json — carries the package fingerprint/provenance), the law
    # package, and the host hook/adapter config must never be reachable by an
    # ordinary governed edit/delete. (Out-of-project copies are already
    # unreachable via safe_relpath; this protects any in-project instance.)
    ".aidocs/runtime/",
    ".aidocs/",
    ".claude/",
)

# AIDOCS-owned, reproducible artifacts: deletion only needs proof of
# ownership/regeneration, not a restore copy. Tight on purpose.
_REGENERABLE_PREFIXES = (
    ".memory/.runtime/",
    ".memory/.cache/",
)
_REGENERABLE_SUFFIXES = (".pyc", ".pyo")
_REGENERABLE_DIR_TOKENS = ("__pycache__/",)

# Generated host projections — regenerable ONLY if their source can rebuild
# them. .mcp.json is projected from the mcp_servers SQL table; it may be
# hard-deleted only when that source is provably regenerable, else quarantined.
_PROJECTION_EXACT = (".mcp.json",)


@dataclass
class DeletionResult:
    ok: bool
    outcome: str
    path: str
    category: str = ""
    reason: str = ""
    checkpoint_id: str = ""
    mode: str = ""  # checkpoint mode (git/quarantine) when applicable
    recoverable: bool = False
    audited: bool = True  # False ⟹ audit storage failed (see fallback log)


def _rel_posix(project_root: Path, path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = project_root / p
    try:
        r = p.resolve().relative_to(project_root.resolve())
    except Exception:
        r = Path(path)
    return str(r).replace("\\", "/")


def _is_regenerable(rel_lower: str) -> bool:
    if any(rel_lower.startswith(p) for p in _REGENERABLE_PREFIXES):
        return True
    if rel_lower.endswith(_REGENERABLE_SUFFIXES):
        return True
    if any(tok in rel_lower for tok in _REGENERABLE_DIR_TOKENS):
        return True
    return False


def _normalized_server_spec(spec: object) -> dict | None:
    """Normalize one on-disk mcpServers entry to the CANONICAL projection shape
    via the SAME renderer the writer uses (mcp_registry_store), so the proof's
    expected shape can never drift from what project_to_file emits. Returns None
    for any malformed shape or a spec carrying fields SQL cannot reproduce
    (e.g. env, cwd) — those must quarantine, never hard-delete.
    """
    if not isinstance(spec, dict):
        return None
    if set(spec.keys()) - {"type", "command", "args"}:
        return None  # extra server-spec field has no SQL source
    raw_args = spec.get("args")
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list):
        return None  # malformed args
    from .mcp_registry_store import canonical_server_fields

    return canonical_server_fields(spec.get("type"), spec.get("command"), raw_args)


def _projection_regenerable(project_root: Path, rel_lower: str) -> bool:
    """Prove a generated projection is COMPLETELY, CANONICALLY regenerable from
    its source before a hard-delete — not merely that server NAMES are covered.

    For .mcp.json the canonical post-delete regeneration is exactly what
    project_to_file would emit for an ABSENT file: ``{"mcpServers": {<name>:
    {type, command, args}}}`` built from the mcp_servers SQL table (no
    preserved top-level keys, because the file is gone). Hard-delete is allowed
    ONLY when the normalized on-disk content equals that canonical projection
    EXACTLY — same server set AND same per-server definition. Anything else —
    a same-name/different-command divergence, a server missing from SQL, an
    extra top-level key, an extra server-spec field, a malformed mcpServers
    shape, or a corrupt/unreadable file — is NOT exactly regenerable and
    returns False so the caller quarantines (never crashes, never deletes).
    """
    if rel_lower != ".mcp.json":
        return False
    try:
        from .mcp_registry_store import McpRegistryStore, render_projection_servers

        servers = McpRegistryStore().list_servers(project_root)
    except Exception:
        return False
    # Canonical projection SQL would emit for an absent file — built by the
    # SAME renderer project_to_file uses, so writer and proof cannot drift.
    projected = render_projection_servers(servers)
    mcp = project_root / ".mcp.json"
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
    except Exception:
        return False  # corrupt/unreadable → cannot prove reproducible
    if not isinstance(data, dict):
        return False
    # Only {"mcpServers": ...} is canonically regenerable — any other top-level
    # key (e.g. a hand-added "inputs" block) has no SQL source.
    if set(data.keys()) != {"mcpServers"}:
        return False
    on_disk_servers = data.get("mcpServers")
    if not isinstance(on_disk_servers, dict):
        return False  # malformed mcpServers shape (e.g. a list/string)
    normalized: dict[str, dict] = {}
    for name, spec in on_disk_servers.items():
        norm = _normalized_server_spec(spec)
        if norm is None:
            return False  # malformed / non-canonical server spec → quarantine
        normalized[str(name)] = norm
    # Exact canonical match: regeneration reproduces the file verbatim.
    return normalized == projected


def classify_deletion(project_root: Path, path: str | Path) -> tuple[str, str]:
    """Return (category, reason). Protection is checked FIRST so a control-
    authority/secret/sentinel file can never fall through to a cheaper route.
    """
    rel = _rel_posix(project_root, path)
    rel_lower = rel.lower()
    abs_p = project_root / rel

    # control-authority / control-plane paths
    if any(rel_lower.startswith(p) for p in _CONTROL_AUTHORITY_PREFIXES):
        return CAT_PROTECTED, f"control-authority path ({rel})"

    # session memory / authority: journals, SESSION.md, query-gate, skills, and
    # session-bound state under .MEMORY/sessions/. PROTECTED from GENERIC
    # deletion so no tool erases it as ordinary project junk. Stale-session
    # cleanup is CLEANUP-ONLY via the sanctioned admin path (session_deletion_law:
    # operator-auth + reason + audit + checkpoint + not-active), which uses
    # quarantine-move directly and does not consult this classifier.
    if rel_lower == ".memory/sessions" or rel_lower.startswith(".memory/sessions/"):
        return CAT_PROTECTED, (
            f"session memory/authority ({rel}) — cleanup only via the sanctioned "
            "admin session-deletion path"
        )

    # forbidden / secrets / git internals (reuse the write-side classifier)
    pc = classify_path(rel, project_root=project_root)
    if pc.classification in FORBIDDEN_CLASSES:
        return CAT_PROTECTED, f"{pc.classification}: {pc.reason}"
    if pc.classification in (CLASS_SECRETS_GATED, CLASS_CONFIRMABLE_GIT):
        return CAT_PROTECTED, f"{pc.classification}: {pc.reason}"

    # DO-NOT-TOUCH sentinel files
    try:
        from .protected_file import has_protection_sentinel

        if abs_p.is_file():
            head = abs_p.read_text(encoding="utf-8", errors="ignore")
            if has_protection_sentinel(head):
                return CAT_PROTECTED, "protected file (DO NOT TOUCH sentinel)"
    except Exception:
        pass

    # Generated host projection (regenerable only if its source can rebuild it)
    if rel_lower in _PROJECTION_EXACT:
        return CAT_PROJECTION, "generated host projection"

    # AIDOCS-owned reproducible artifacts
    if _is_regenerable(rel_lower):
        return CAT_REGENERABLE, "AIDOCS-owned regenerable/cache artifact"

    return CAT_NONTRIVIAL, "source/unknown (nontrivial)"


def _audit_fallback(project_root: Path, record: dict) -> None:
    """Durable fallback when the audit STORE is unavailable: append the record
    to .MEMORY/.checkpoints/_audit_fallback.log so a destructive mutation is
    never UNRECORDED. The checkpoints dir is itself delete-protected, so this
    record cannot be cleaned away by a normal delete.
    """
    try:
        d = project_root / ".MEMORY" / ".checkpoints"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "_audit_fallback.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _audit(
    project_root: Path,
    rel: str,
    *,
    action_kind: str,
    status: str,
    category: str = "",
    checkpoint_id: str = "",
    mode: str = "",
    reason: str = "",
    principal_type: str = "agent",
    user_id: str | None = None,
) -> bool:
    """Truthfully audit a governed-deletion outcome. Returns whether the audit
    LANDED in the execution ledger. On failure it is NOT silently swallowed: a
    durable fallback record is written and False is returned so the caller can
    surface ``audited=False`` rather than claim a clean audited mutation.
    """
    payload = {
        "path": rel,
        "category": category,
        "outcome": status,
        "checkpoint_id": checkpoint_id,
        "checkpoint_mode": mode,
        "reason": reason,
        "action_kind": action_kind,
        "principal_type": principal_type,
        "user_id": user_id,
    }
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="governed_deletion",
            source_kind="governed_deletion",
            capability_name="governed_delete",
            action_kind=action_kind,
            target_entity=rel,
            status=status,
            user_id=user_id,
            principal_type=principal_type,
            scope_id=str(project_root).replace("\\", "/"),
            payload=payload,
        )
        return True
    except Exception as exc:
        payload["audit_error"] = repr(exc)
        _audit_fallback(project_root, payload)
        return False


def governed_delete(
    project_root: Path,
    path: str | Path,
    *,
    reason: str = "",
    allow_protected_route: bool = False,
    principal_type: str = "agent",
    user_id: str | None = None,
    provenance: dict | None = None,
) -> DeletionResult:
    """Delete a file as an audited, reversible mutation. See module doctrine.
    Never blind-deletes a nontrivial file; downgrades to quarantine/refusal
    when a restore point cannot be secured.
    """
    project_root = Path(project_root)
    raw_rel = _rel_posix(project_root, path)

    def _emit(
        rel: str,
        *,
        ok: bool,
        outcome: str,
        category: str = "",
        reason: str = "",
        checkpoint_id: str = "",
        mode: str = "",
        recoverable: bool = False,
    ) -> DeletionResult:
        audited = _audit(
            project_root,
            rel,
            action_kind="delete",
            status=outcome,
            category=category,
            checkpoint_id=checkpoint_id,
            mode=mode,
            reason=reason,
            principal_type=principal_type,
            user_id=user_id,
        )
        return DeletionResult(
            ok=ok,
            outcome=outcome,
            path=rel,
            category=category,
            reason=reason,
            checkpoint_id=checkpoint_id,
            mode=mode,
            recoverable=recoverable,
            audited=audited,
        )

    # FAIL CLOSED on an unsafe target (absolute / traversal / symlink-escape /
    # outside-root) — refuse before any classification or disk touch.
    rel = safe_relpath(project_root, path)
    if rel is None:
        return _emit(
            raw_rel,
            ok=False,
            outcome=OUTCOME_REFUSED,
            reason="unsafe path (absolute/traversal/symlink/outside-root) — fail closed",
        )
    abs_p = project_root / rel
    if not abs_p.is_file():
        return _emit(rel, ok=False, outcome=OUTCOME_REFUSED, reason="path not found")

    category, why = classify_deletion(project_root, rel)

    # PROTECTED / control-authority — refuse unless an explicit doctrine route.
    if category == CAT_PROTECTED and not allow_protected_route:
        return _emit(rel, ok=False, outcome=OUTCOME_REFUSED, category=category, reason=why)

    # REGENERABLE — proven AIDOCS-owned reproducible artifact → hard-delete.
    if category == CAT_REGENERABLE:
        try:
            abs_p.unlink()
        except OSError as exc:
            return _emit(
                rel,
                ok=False,
                outcome=OUTCOME_REFUSED,
                category=category,
                reason=f"unlink failed: {exc}",
            )
        return _emit(
            rel,
            ok=True,
            outcome=OUTCOME_DELETED,
            category=category,
            reason=why,
            recoverable=False,
        )

    # PROJECTION — hard-delete ONLY when regeneration from source is proven;
    # otherwise fall through to the checkpoint/quarantine path (never a blind
    # hard-delete of a projection whose source can't rebuild it).
    if category == CAT_PROJECTION and _projection_regenerable(
        project_root,
        rel.lower(),
    ):
        try:
            abs_p.unlink()
        except OSError as exc:
            return _emit(
                rel,
                ok=False,
                outcome=OUTCOME_REFUSED,
                category=category,
                reason=f"unlink failed: {exc}",
            )
        return _emit(
            rel,
            ok=True,
            outcome=OUTCOME_DELETED,
            category=category,
            reason=f"{why}; regeneration from source proven",
            recoverable=False,
        )

    # NONTRIVIAL / PROJECTION-not-regenerable / PROTECTED-via-route:
    # a restore point is REQUIRED before removal — and so is meaningful intent.
    # (Regenerable temp/generated cleanup returned above without this gate, so
    # owned-artifact cleanup stays ergonomic.)
    if not _meaningful_reason(reason):
        return _emit(
            rel,
            ok=False,
            outcome=OUTCOME_REFUSED,
            category=category,
            reason="nontrivial delete requires a meaningful --reason describing intent",
        )
    svc = CheckpointService(project_root)
    if svc.git_recoverable_sha(rel):
        cp = svc.create([rel], reason=reason or f"governed_delete:{rel}", provenance=provenance)
        if cp.ok:
            try:
                abs_p.unlink()
            except OSError as exc:
                return _emit(
                    rel,
                    ok=False,
                    outcome=OUTCOME_REFUSED,
                    category=category,
                    checkpoint_id=cp.checkpoint_id,
                    mode=cp.mode_summary,
                    reason=f"unlink failed after checkpoint: {exc}",
                )
            return _emit(
                rel,
                ok=True,
                outcome=OUTCOME_CHECKPOINTED,
                category=category,
                checkpoint_id=cp.checkpoint_id,
                mode=cp.mode_summary,
                reason=why,
                recoverable=True,
            )
        # checkpoint create failed → downgrade to quarantine below

    q = svc.quarantine_move([rel], reason=reason or f"governed_delete:{rel}", provenance=provenance)
    if q.ok:
        return _emit(
            rel,
            ok=True,
            outcome=OUTCOME_QUARANTINED,
            category=category,
            checkpoint_id=q.checkpoint_id,
            mode=q.mode_summary,
            reason=why,
            recoverable=True,
        )

    # No restore point could be secured → REFUSE (never blind-delete).
    return _emit(
        rel,
        ok=False,
        outcome=OUTCOME_REFUSED,
        category=category,
        reason=f"no restore point: {q.reason}",
    )


def checkpoint_for_edit(
    project_root: Path,
    rel_path: str | Path,
    *,
    reason: str = "edit",
    provenance: dict | None = None,
) -> dict:
    """Truthful pre-mutation restore point for a governed EDIT (destructive-
    mutation convergence). ADDITIVE and NON-BLOCKING by contract: callers run
    this AFTER their existing write guards and just before the write — it never
    refuses an edit, never weakens a guard, and never raises.

    Routing mirrors deletion classification:
      * temp/generated/cache (regenerable) → skip ergonomically (no restore
        point needed; agents tidy freely).
      * source/nontrivial → create a git/quarantine restore point via the
        CheckpointService adapter (checkpoint_before_edit).
      * if a restore point cannot be secured → checkpoint_unavailable (truthful;
        the edit still proceeds — the write guard, not the checkpoint, governs).

    Returns metadata to attach to the edit result:
      {"status", "checkpointed": bool, "checkpoint_id": str, "mode": str}.
    """
    project_root = Path(project_root)
    rel = _rel_posix(project_root, rel_path)
    if _is_regenerable(rel.lower()):
        return {
            "status": EDIT_SKIPPED_REGENERABLE,
            "checkpointed": False,
            "checkpoint_id": "",
            "mode": "",
        }
    try:
        cp = checkpoint_before_edit(project_root, rel, reason=reason, provenance=provenance)
    except Exception:
        cp = None
    if cp is not None and getattr(cp, "ok", False):
        return {
            "status": EDIT_CHECKPOINTED,
            "checkpointed": True,
            "checkpoint_id": cp.checkpoint_id,
            "mode": cp.mode_summary,
        }
    return {"status": EDIT_UNAVAILABLE, "checkpointed": False, "checkpoint_id": "", "mode": ""}


def restore_deletion(
    project_root: Path,
    checkpoint_id: str,
    *,
    principal_type: str = "agent",
    user_id: str | None = None,
) -> DeletionResult:
    """Restore a governed-deletion checkpoint and audit the ``restored``
    outcome truthfully.
    """
    project_root = Path(project_root)
    res = CheckpointService(project_root).restore(checkpoint_id)
    target = res.restored[0] if res.restored else ""
    outcome = OUTCOME_RESTORED if res.ok else OUTCOME_REFUSED
    audited = _audit(
        project_root,
        target or checkpoint_id,
        action_kind="restore",
        status=outcome,
        checkpoint_id=checkpoint_id,
        reason=res.reason or f"restored {len(res.restored)} file(s)",
        principal_type=principal_type,
        user_id=user_id,
    )
    return DeletionResult(
        ok=res.ok,
        outcome=outcome,
        path=target,
        checkpoint_id=checkpoint_id,
        reason=res.reason,
        recoverable=res.ok,
        audited=audited,
    )
