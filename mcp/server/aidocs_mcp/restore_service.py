"""ai_restore — the single restoration facade over AIDOCS destructive history.

One agent-friendly entry over EVERY restore point. Every destructive mutation
in AIDOCS — git checkpoints, quarantine/tombstone snapshots, governed-deletion
restores, governed-edit checkpoints (str_replace/edit_lines), and projection
quarantines — lands as a manifest under ``.MEMORY/.checkpoints/``. This facade
reads those manifests so an agent can find and restore a previous version
WITHOUT knowing whether the bytes came from git or quarantine.

Modes:
  list      — restore points, filtered by path/task/plan/session/lane/context.
  timeline  — for one path: the current (on-disk) version vs the ordered
              previous snapshots.
  inspect   — one checkpoint's manifest + per-entry restorability status.
  diff      — unified diff between a checkpoint's snapshot and current on-disk.
  nearest   — the most-recent restore point for a path (optionally before a ts).
  restore   — restore a checkpoint by EXACT id, after taking a pre-restore
              checkpoint of current state, reusing the path-safety /
              protected / control-authority rules; fail-closed and audited.

Status truth (so agents know what can/cannot be restored):
  checkpointed           — a restorable snapshot exists.
  checkpoint_unavailable — a recorded point whose bytes can't be read back.
  skipped_regenerable    — (edit-result vocabulary) no point was taken because
                           the target was temp/generated; nothing to restore.
"""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from .checkpoint_service import CheckpointService, safe_relpath
from .governed_deletion import (
    CAT_PROTECTED,
    EDIT_CHECKPOINTED,
    EDIT_UNAVAILABLE,
    classify_deletion,
)

STATUS_CHECKPOINTED = EDIT_CHECKPOINTED  # "checkpointed"
STATUS_UNAVAILABLE = EDIT_UNAVAILABLE  # "checkpoint_unavailable"
# safety redactions — applied CONSISTENTLY across every mode (read + restore).
STATUS_UNSAFE = "unsafe_redacted"  # outside-root/traversal/symlink
STATUS_PROTECTED = "protected_redacted"  # protected/control-authority/secret

# A non-dry-run destructive restore must carry a meaningful intent string
# (mirrors the deletion reason-gate).
_MIN_REASON_LEN = 6


def _meaningful_reason(reason: str) -> bool:
    return len((reason or "").strip()) >= _MIN_REASON_LEN


def _entry_guard(project_root: Path, original_path: object) -> tuple[str, str | None]:
    """Classify a manifest entry's target for SAFE handling in every mode.

    Returns (guard, rel):
      ("ok", rel)               — safe, non-protected; bytes/diff may be shown.
      ("unsafe_redacted", None) — outside-root/traversal/symlink (never resolve
                                  or read it).
      ("protected_redacted", rel) — protected/control-authority/secret; never
                                  expose its bytes/diff/hash.
    Fail-closed: any classification error redacts.
    """
    rel = safe_relpath(project_root, str(original_path or ""))
    if rel is None:
        return STATUS_UNSAFE, None
    try:
        category, _why = classify_deletion(project_root, rel)
    except Exception:
        return STATUS_PROTECTED, rel  # fail closed
    if category == CAT_PROTECTED:
        return STATUS_PROTECTED, rel
    return "ok", rel


def _safe_entry_view(
    project_root: Path,
    svc: CheckpointService,
    cid: str,
    entry: dict,
) -> dict:
    """An entry rendered for a READ mode — redacting unsafe/protected targets so
    no read mode can leak outside-root or protected checkpoint bytes/metadata.
    """
    raw = entry.get("original_path")
    guard, rel = _entry_guard(project_root, raw)
    if guard != "ok":
        # Redacted: expose only that it exists + why it's withheld. No resolved
        # path for unsafe targets, no size/sha/bytes for protected ones.
        view = {"mode": entry.get("mode"), "status": guard, "redacted": True}
        if guard == STATUS_PROTECTED and rel is not None:
            view["path"] = rel
        return view
    return {
        "path": rel,
        "mode": entry.get("mode"),
        "size": entry.get("size"),
        "sha256": entry.get("sha256"),
        "status": _entry_status(svc, cid, entry),
    }


# restore outcomes (audited)
OUTCOME_PREVIEWED = "previewed"
OUTCOME_RESTORED = "restored"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"


def _norm(project_root: Path, path: str) -> str:
    rel = safe_relpath(project_root, path)
    if rel is not None:
        return rel
    return str(path).replace("\\", "/")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoints_ascending(svc: CheckpointService) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for cid in svc.list_checkpoints():  # sorted ascending (oldest first)
        m = svc.read_manifest(cid)
        if m:
            out.append((cid, m))
    return out


def _entry_status(svc: CheckpointService, cid: str, entry: dict) -> str:
    return STATUS_CHECKPOINTED if svc.entry_bytes(cid, entry) is not None else STATUS_UNAVAILABLE


def _matches(
    manifest: dict,
    *,
    path: str | None,
    task: str | None,
    plan: str | None,
    session: str | None,
    lane: str | None,
) -> bool:
    prov = manifest.get("provenance") or {}
    for key, val in (("task", task), ("plan", plan), ("session", session), ("lane", lane)):
        if val is not None and str(prov.get(key) or "") != str(val):
            return False
    if path is not None:
        if not any(e.get("original_path") == path for e in manifest.get("entries", [])):
            return False
    return True


def _audit(
    project_root: Path,
    *,
    mode: str,
    status: str,
    checkpoint_id: str,
    paths: list[str],
    reason: str,
    principal_type: str,
    user_id: str | None,
) -> None:
    """Truthful audit of a restore-facade outcome (requested/refused/restored/
    failed). Best-effort.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="restore_facade",
            source_kind="ai_restore",
            capability_name="ai_restore",
            action_kind=mode,
            target_entity=checkpoint_id or (paths[0] if paths else ""),
            status=status,
            user_id=user_id,
            principal_type=principal_type,
            scope_id=str(project_root).replace("\\", "/"),
            payload={
                "mode": mode,
                "status": status,
                "checkpoint_id": checkpoint_id,
                "paths": paths,
                "reason": reason,
            },
        )
    except Exception:
        pass


# ── read modes ──────────────────────────────────────────────────────
def list_restore_points(
    project_root: Path,
    *,
    path: str | None = None,
    task: str | None = None,
    plan: str | None = None,
    session: str | None = None,
    lane: str | None = None,
    limit: int = 50,
) -> dict:
    project_root = Path(project_root)
    svc = CheckpointService(project_root)
    rel = _norm(project_root, path) if path else None
    rows: list[dict] = []
    for cid, m in reversed(_checkpoints_ascending(svc)):  # newest first
        if not _matches(m, path=rel, task=task, plan=plan, session=session, lane=lane):
            continue
        entries = [_safe_entry_view(project_root, svc, cid, e) for e in m.get("entries", [])]
        rows.append(
            {
                "checkpoint_id": cid,
                "created_at": m.get("created_at"),
                "reason": m.get("reason"),
                "provenance": m.get("provenance") or {},
                "entries": entries,
            },
        )
        if len(rows) >= limit:
            break
    return {"ok": True, "mode": "list", "count": len(rows), "restore_points": rows}


def timeline(project_root: Path, path: str) -> dict:
    project_root = Path(project_root)
    svc = CheckpointService(project_root)
    # SAFETY: fail closed for an unsafe path; redact bytes/hash for a
    # protected/secret one (still show that snapshots exist, just not content).
    guard, rel = _entry_guard(project_root, path)
    if guard == STATUS_UNSAFE:
        return {"ok": False, "mode": "timeline", "reason": "unsafe_path", "path": str(path)}
    redacted = guard == STATUS_PROTECTED
    snaps: list[dict] = []
    for cid, m in _checkpoints_ascending(svc):  # oldest → newest
        for e in m.get("entries", []):
            if e.get("original_path") == rel:
                snap = {
                    "checkpoint_id": cid,
                    "created_at": m.get("created_at"),
                    "version": "previous",
                    "reason": m.get("reason"),
                }
                if redacted:
                    snap["status"] = STATUS_PROTECTED
                    snap["redacted"] = True
                else:
                    snap["status"] = _entry_status(svc, cid, e)
                    snap["size"] = e.get("size")
                    snap["sha256"] = e.get("sha256")
                snaps.append(snap)
    if snaps:
        snaps[-1]["latest_snapshot"] = True  # clearest "previous" to roll back to
    abs_p = project_root / rel
    if redacted:
        current = {
            "version": "current",
            "on_disk": abs_p.is_file(),
            "status": STATUS_PROTECTED,
            "redacted": True,
        }
    elif abs_p.is_file():
        data = abs_p.read_bytes()
        current = {"version": "current", "on_disk": True, "size": len(data), "sha256": _sha(data)}
    else:
        current = {"version": "current", "on_disk": False, "note": "file absent on disk (deleted)"}
    out = {"ok": True, "mode": "timeline", "path": rel, "current": current, "previous": snaps}
    if redacted:
        out["redacted"] = True
    return out


def inspect(project_root: Path, checkpoint_id: str) -> dict:
    project_root = Path(project_root)
    svc = CheckpointService(project_root)
    m = svc.read_manifest(checkpoint_id)
    if not m:
        return {
            "ok": False,
            "mode": "inspect",
            "reason": "checkpoint_not_found",
            "checkpoint_id": checkpoint_id,
        }
    entries = [_safe_entry_view(project_root, svc, checkpoint_id, e) for e in m.get("entries", [])]
    return {
        "ok": True,
        "mode": "inspect",
        "checkpoint_id": checkpoint_id,
        "created_at": m.get("created_at"),
        "reason": m.get("reason"),
        "provenance": m.get("provenance") or {},
        "entries": entries,
    }


def diff(project_root: Path, checkpoint_id: str, path: str | None = None) -> dict:
    project_root = Path(project_root)
    svc = CheckpointService(project_root)
    m = svc.read_manifest(checkpoint_id)
    if not m:
        return {
            "ok": False,
            "mode": "diff",
            "reason": "checkpoint_not_found",
            "checkpoint_id": checkpoint_id,
        }
    want = _norm(project_root, path) if path else None
    diffs: list[dict] = []
    for e in m.get("entries", []):
        raw = e.get("original_path")
        if want is not None and raw != want:
            continue
        # SAFETY: never read snapshot OR current bytes for an unsafe/protected
        # target — a diff would otherwise leak outside-root or secret content.
        guard, rel = _entry_guard(project_root, raw)
        if guard != "ok":
            entry_out = {"status": guard, "redacted": True}
            if rel is not None:
                entry_out["path"] = rel
            diffs.append(entry_out)
            continue
        snap = svc.entry_bytes(checkpoint_id, e)
        if snap is None:
            diffs.append({"path": rel, "status": STATUS_UNAVAILABLE})
            continue
        abs_p = project_root / rel
        cur = abs_p.read_bytes() if abs_p.is_file() else b""
        try:
            udiff = "".join(
                difflib.unified_diff(
                    snap.decode("utf-8", "replace").splitlines(keepends=True),
                    cur.decode("utf-8", "replace").splitlines(keepends=True),
                    fromfile=f"checkpoint:{rel}",
                    tofile=f"current:{rel}",
                ),
            )
        except Exception:
            udiff = ""  # binary / undecodable
        diffs.append(
            {"path": rel, "status": STATUS_CHECKPOINTED, "changed": snap != cur, "diff": udiff},
        )
    return {"ok": True, "mode": "diff", "checkpoint_id": checkpoint_id, "diffs": diffs}


def nearest(
    project_root: Path,
    path: str,
    *,
    before: str | None = None,
) -> dict:
    project_root = Path(project_root)
    svc = CheckpointService(project_root)
    guard, rel = _entry_guard(project_root, path)
    if guard == STATUS_UNSAFE:
        return {"ok": False, "mode": "nearest", "path": str(path), "reason": "unsafe_path"}
    best: tuple[str, dict] | None = None
    for cid, m in _checkpoints_ascending(svc):  # ascending → last match = newest
        if before and str(m.get("created_at") or "") > before:
            continue
        if any(e.get("original_path") == rel for e in m.get("entries", [])):
            best = (cid, m)
    if best is None:
        return {"ok": False, "mode": "nearest", "path": rel, "reason": "no_restore_point"}
    cid, m = best
    return {
        "ok": True,
        "mode": "nearest",
        "path": rel,
        "checkpoint_id": cid,
        "created_at": m.get("created_at"),
        "reason": m.get("reason"),
    }


# ── destructive mode: restore ───────────────────────────────────────
def restore(
    project_root: Path,
    checkpoint_id: str,
    *,
    reason: str = "",
    dry_run: bool = False,
    principal_type: str = "agent",
    user_id: str | None = None,
) -> dict:
    """Restore a checkpoint by EXACT id. Requires inspection/dry-run or an
    exact id (this API only accepts an exact id — a path-only restore must be
    resolved via nearest/list first, so an ambiguous restore cannot happen).

    A non-dry-run (destructive) restore additionally requires a MEANINGFUL
    ``reason`` describing intent, which is audited.

    Fail-closed: refuses if the checkpoint is missing, if any entry path is
    unsafe (outside-root/traversal/symlink), protected/control-authority, or if
    its bytes are unavailable. Before overwriting current state it takes a
    PRE-RESTORE checkpoint so the restore is itself reversible. Audited.
    """
    project_root = Path(project_root)
    svc = CheckpointService(project_root)

    def _refuse(reason: str, **extra) -> dict:
        _audit(
            project_root,
            mode="restore",
            status=OUTCOME_REFUSED,
            checkpoint_id=checkpoint_id,
            paths=[],
            reason=reason,
            principal_type=principal_type,
            user_id=user_id,
        )
        return {
            "ok": False,
            "mode": "restore",
            "outcome": OUTCOME_REFUSED,
            "checkpoint_id": checkpoint_id,
            "reason": reason,
            **extra,
        }

    if not checkpoint_id:
        return _refuse("an exact checkpoint_id is required")
    m = svc.read_manifest(checkpoint_id)
    if not m:
        return _refuse("checkpoint_not_found")

    # Validate every entry against the SAME path-safety + protected/control-
    # authority rules the write/delete paths use. Fail closed on any violation.
    targets: list[str] = []
    for e in m.get("entries", []):
        raw = str(e.get("original_path") or "")
        rel = safe_relpath(project_root, raw)
        if rel is None:
            return _refuse(f"unsafe restore target (outside-root/traversal/symlink): {raw!r}")
        category, why = classify_deletion(project_root, rel)
        if category == CAT_PROTECTED:
            return _refuse(f"protected/control-authority target refused: {rel} ({why})")
        if svc.entry_bytes(checkpoint_id, e) is None:
            return _refuse(f"checkpoint_unavailable for {rel} — bytes cannot be read back")
        targets.append(rel)

    if dry_run:
        _audit(
            project_root,
            mode="restore",
            status="requested",
            checkpoint_id=checkpoint_id,
            paths=targets,
            reason="dry_run preview",
            principal_type=principal_type,
            user_id=user_id,
        )
        return {
            "ok": True,
            "mode": "restore",
            "outcome": OUTCOME_PREVIEWED,
            "checkpoint_id": checkpoint_id,
            "would_restore": targets,
            "dry_run": True,
        }

    # A destructive (non-dry-run) restore requires meaningful intent, audited.
    if not _meaningful_reason(reason):
        return _refuse(
            "destructive restore requires a meaningful reason "
            "describing intent (use dry_run to preview)",
        )

    # PRE-RESTORE checkpoint of the CURRENT state we are about to overwrite, so
    # the restore is reversible. If it cannot be secured, fail closed.
    existing_now = [t for t in targets if (project_root / t).is_file()]
    pre_id = ""
    if existing_now:
        pre = svc.create(
            existing_now,
            reason=f"pre_restore:{checkpoint_id}",
            provenance={"kind": "pre_restore", "source_checkpoint": checkpoint_id},
        )
        if not pre.ok:
            return _refuse(
                f"pre_restore checkpoint failed ({pre.reason}); "
                "refusing to overwrite without a safety net",
            )
        pre_id = pre.checkpoint_id

    res = svc.restore(checkpoint_id)
    if res.ok:
        _audit(
            project_root,
            mode="restore",
            status=OUTCOME_RESTORED,
            checkpoint_id=checkpoint_id,
            paths=res.restored,
            reason=f"{reason.strip()} | pre_restore={pre_id}",
            principal_type=principal_type,
            user_id=user_id,
        )
        return {
            "ok": True,
            "mode": "restore",
            "outcome": OUTCOME_RESTORED,
            "checkpoint_id": checkpoint_id,
            "restored": res.restored,
            "pre_restore_checkpoint_id": pre_id,
        }
    _audit(
        project_root,
        mode="restore",
        status=OUTCOME_FAILED,
        checkpoint_id=checkpoint_id,
        paths=res.restored,
        reason=res.reason,
        principal_type=principal_type,
        user_id=user_id,
    )
    return {
        "ok": False,
        "mode": "restore",
        "outcome": OUTCOME_FAILED,
        "checkpoint_id": checkpoint_id,
        "restored": res.restored,
        "missing": res.missing,
        "reason": res.reason,
        "pre_restore_checkpoint_id": pre_id,
    }
