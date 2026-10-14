"""CheckpointService — pre-mutation restore points for destructive ops.

Doctrine: a destructive mutation (deletion today; edit via the adapter seam
tomorrow) must be REVERSIBLE and AUDITED, not blind cleanup. Before content is
removed or overwritten, a restore point is created through one of two modes:

  * GIT mode — when the file is git-recoverable: inside a work tree, tracked,
    and CLEAN (so HEAD already holds its exact bytes). The restore point is the
    HEAD blob sha; restore streams it back with ``git cat-file``. This never
    touches the index or working tree, so a DIRTY worktree cannot corrupt the
    checkpoint (we only read HEAD, never stash/commit/reset).

  * QUARANTINE mode — when git is insufficient: no repo, git missing, or the
    file is untracked/dirty (HEAD lacks its exact bytes). The current bytes are
    captured into ``.MEMORY/.checkpoints/<id>/`` and recorded in a manifest
    (manifest-owned). ``create`` COPIES (leaves the original in place — the
    edit-adapter seam); ``quarantine_move`` MOVES (the deletion seam).

``create`` / ``quarantine_move`` return ok=False ONLY when neither git nor a
quarantine copy could secure the bytes. Callers MUST then downgrade
(quarantine/refuse) — never blind-delete.

This module is the shared contract for the deletion path (governed_deletion)
AND the future edit/delete convergence: an edit can snapshot via
``checkpoint_before_edit`` before overwriting, with no change to the existing
write guards.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

from .git_helpers import run_git_sync

_CHECKPOINTS_REL = Path(".MEMORY") / ".checkpoints"
_MANIFEST_NAME = "manifest.json"

MODE_GIT = "git"
MODE_QUARANTINE = "quarantine"

# Conservative (agent-safe) GC floors: an agent-driven cleanup can NEVER drop
# below this many most-recent checkpoints, nor prune anything younger than this
# age — so live/recent rollback state survives whatever keep/age is requested.
# Only an explicit aggressive (operator) GC lifts these floors.
_GC_CONSERVATIVE_KEEP_FLOOR = 5
_GC_CONSERVATIVE_MIN_AGE_SECONDS = 3600


@dataclass
class CheckpointResult:
    ok: bool
    checkpoint_id: str = ""
    mode_summary: str = ""  # "git" | "quarantine" | "mixed" | ""
    entries: list[dict] = field(default_factory=list)
    reason: str = ""  # failure reason when ok is False


@dataclass
class RestoreResult:
    ok: bool
    checkpoint_id: str = ""
    restored: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reason: str = ""


def safe_relpath(project_root: Path, path: str | Path) -> str | None:
    """Resolve ``path`` to a project-relative POSIX key, FAIL-CLOSED.

    Returns None (reject) for any of: an absolute path, a ``..`` traversal
    token, a symlink/parent that resolves OUTSIDE the project root, or an
    otherwise outside-root target. Only a path that physically lands inside
    the root after full symlink resolution yields a key. This is the single
    gate every checkpoint/delete/restore path must pass.

    Doctrine (2026-05-29 — clean-VPS Gate 2b cluster, doctrine_fuzz):
    reject Windows-style separators (``\\``), drive-letter prefixes
    (``C:/...``), and UNC roots (``\\\\server\\share``) UP FRONT, before
    any Path() parsing. Why: on POSIX, ``\\`` is a valid filename
    character — so ``Path("..\\windows\\x").parts`` returns a single
    component ``"..\\windows\\x"`` (no ``..`` token), which slips past
    the ``.. in p.parts`` check. The pre-existing tail then runs
    ``str(rel).replace("\\", "/")`` which normalizes the smuggled
    backslashes into a real ``../windows/x`` traversal in the returned
    POSIX key — a confused-deputy hole. AIDOCS uses POSIX-style
    project-relative keys everywhere; backslashes, drive letters, and
    UNC roots have no legitimate role at this entry point. Catching
    them at the very top is the smallest production fix that closes
    the hole without touching the rest of the resolution logic.
    """
    path_str = str(path)
    # Backslash → reject. Covers ..\windows\x, mixed \\, and any other
    # backslash-bearing payload. AIDOCS keys are always forward-slash.
    if "\\" in path_str:
        return None
    # Drive-letter prefix (e.g. "C:/abs", "C:abs") — on POSIX, Path()
    # treats "C:" as a relative directory component and never marks the
    # path absolute, so the is_absolute() branch below misses it. The
    # leading "<letter>:" pattern is unmistakably a Windows absolute
    # path; reject at the source.
    if len(path_str) >= 2 and path_str[1] == ":" and path_str[0].isalpha():
        return None
    p = Path(path)
    if p.is_absolute():
        return None
    if ".." in p.parts:
        return None
    try:
        root = Path(project_root).resolve()
        resolved = (root / p).resolve()
        rel = resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    rel_str = str(rel).replace("\\", "/")
    if not rel_str or rel_str == ".":
        return None
    return rel_str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CheckpointService:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)

    # ── locations ───────────────────────────────────────────────────
    def checkpoints_root(self) -> Path:
        return self.project_root / _CHECKPOINTS_REL

    def _new_checkpoint_dir(self) -> tuple[str, Path]:
        cpid = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
        cpdir = self.checkpoints_root() / cpid
        (cpdir / "files").mkdir(parents=True, exist_ok=True)
        return cpid, cpdir

    # ── git recoverability ──────────────────────────────────────────
    def git_recoverable_sha(self, relpath: str) -> str | None:
        """Return the HEAD blob sha iff ``relpath`` is git-recoverable
        (inside a work tree, tracked, and clean). None otherwise — including
        a dirty/untracked file or any git error. NEVER mutates index/worktree.
        """
        root = str(self.project_root)
        rel = relpath.replace("\\", "/")
        try:
            run_git_sync(root, "rev-parse", "--is-inside-work-tree")
            # Porcelain empty for a path ⟺ tracked AND clean (untracked shows
            # "?? path", modified shows " M path"). So empty ⟹ HEAD has it.
            status = run_git_sync(root, "status", "--porcelain", "--", rel)
            if status.strip() != "":
                return None
            sha = run_git_sync(root, "rev-parse", f"HEAD:{rel}").strip()
            return sha or None
        except Exception:
            return None

    def _git_cat_file_bytes(self, sha: str) -> bytes:
        """Read a blob's RAW bytes from the object store (no decode/strip, so
        trailing newlines and binary content survive a round-trip).
        """
        # #345: routed through audited_run (ledger row per spawn). Passthrough
        # lambda IS the registered AST callsite; kwargs pass through UNCHANGED
        # (check=True still raises CalledProcessError through audited_run).
        from .shell_egress_service import audited_run

        result = audited_run(
            ["git", "-c", "safe.directory=*", "cat-file", "-p", sha],
            fingerprint=("checkpoint_service.py", "_git_cat_file_bytes", "subprocess.run"),
            reason="checkpoint-git-cat-file",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            cwd=str(self.project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=20,
            creationflags=_WIN_NO_WINDOW,
        )
        return result.stdout

    # ── create a restore point (COPY semantics — original left in place) ─
    def create(
        self,
        paths: list[str],
        *,
        reason: str = "",
        provenance: dict | None = None,
        created_paths: list[str] | None = None,
    ) -> CheckpointResult:
        """Snapshot each path WITHOUT removing it (git ref or quarantine COPY).
        Use before an in-place overwrite (edit seam) or before a delete that
        will remove the original afterwards. ok=False ⟹ bytes could not be
        secured for at least one path → the caller must NOT proceed to delete.

        ``provenance`` (task/plan/session/lane/kind) is recorded in the manifest
        so the restore facade can filter restore points by context.

        ``created_paths`` names files the upcoming mutation will CREATE (they do
        not exist yet, so there are no bytes to snapshot). They are recorded so
        ``restore`` DELETES them — rolling back a creation, not just restoring an
        overwrite. Fail-closed: an unsafe created path aborts the checkpoint.
        """
        return self._capture(
            paths,
            reason=reason,
            move=False,
            provenance=provenance,
            created_paths=created_paths,
        )

    # ── quarantine MOVE — relocate originals into the manifest ──────────
    def quarantine_move(
        self,
        paths: list[str],
        *,
        reason: str = "",
        provenance: dict | None = None,
    ) -> CheckpointResult:
        """Relocate each path into the checkpoint store (rename, no extra disk)
        and record it in the manifest. The original location is emptied. This
        is the deletion downgrade for git-insufficient files and the fallback
        when a COPY checkpoint cannot be written.
        """
        return self._capture(paths, reason=reason, move=True, provenance=provenance)

    def _capture(
        self,
        paths: list[str],
        *,
        reason: str,
        move: bool,
        provenance: dict | None = None,
        created_paths: list[str] | None = None,
    ) -> CheckpointResult:
        # Validate the to-be-created paths up front, fail-closed: an unsafe
        # created path must never make restore delete outside the root.
        created_rels: list[str] = []
        for c in created_paths or []:
            crel = safe_relpath(self.project_root, c)
            if crel is None:
                return CheckpointResult(ok=False, reason=f"unsafe created_path rejected: {c!r}")
            created_rels.append(crel)
        existing: list[tuple[str, Path]] = []
        for p in paths:
            rel = safe_relpath(self.project_root, p)
            if rel is None:
                # FAIL CLOSED: an unsafe path (absolute/traversal/symlink-
                # escape/outside-root) is never checkpointed — and the whole
                # op fails so the caller cannot then proceed to delete it.
                return CheckpointResult(ok=False, reason=f"unsafe path rejected: {p!r}")
            abs_p = self.project_root / rel
            if abs_p.is_file():
                existing.append((rel, abs_p))
        if not existing:
            return CheckpointResult(ok=False, reason="no existing file to checkpoint")
        try:
            cpid, cpdir = self._new_checkpoint_dir()
        except OSError as exc:
            return CheckpointResult(ok=False, reason=f"cannot create checkpoint dir: {exc}")

        entries: list[dict] = []
        modes: set[str] = set()
        try:
            for idx, (rel, abs_p) in enumerate(existing):
                data = abs_p.read_bytes()
                entry: dict = {
                    "original_path": rel,
                    "sha256": _sha256_bytes(data),
                    "size": len(data),
                }
                sha = None if move else self.git_recoverable_sha(rel)
                if sha:
                    # GIT mode — HEAD holds the bytes; no copy needed.
                    entry["mode"] = MODE_GIT
                    entry["git_sha"] = sha
                    modes.add(MODE_GIT)
                else:
                    # QUARANTINE mode — secure the bytes in the manifest dir.
                    qfile = f"files/{idx}"
                    dest = cpdir / qfile
                    if move:
                        # Rename (atomic on one fs); needs no extra space —
                        # the meaningful downgrade when a copy would fail.
                        try:
                            os.replace(abs_p, dest)
                        except OSError:
                            shutil.move(str(abs_p), str(dest))
                    else:
                        shutil.copy2(abs_p, dest)
                    entry["mode"] = MODE_QUARANTINE
                    entry["quarantine_file"] = qfile
                    modes.add(MODE_QUARANTINE)
                entries.append(entry)
        except Exception as exc:
            # Roll back a partial quarantine dir; report failure so the caller
            # downgrades instead of deleting on an unsecured checkpoint.
            shutil.rmtree(cpdir, ignore_errors=True)
            return CheckpointResult(ok=False, reason=f"checkpoint capture failed: {exc}")

        manifest = {
            "checkpoint_id": cpid,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason": reason,
            "moved": move,
            "provenance": dict(provenance) if isinstance(provenance, dict) else {},
            "entries": entries,
            "created_paths": created_rels,
        }
        try:
            (cpdir / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(cpdir, ignore_errors=True)
            return CheckpointResult(ok=False, reason=f"cannot write manifest: {exc}")

        mode_summary = next(iter(modes)) if len(modes) == 1 else ("mixed" if modes else "")
        return CheckpointResult(
            ok=True,
            checkpoint_id=cpid,
            mode_summary=mode_summary,
            entries=entries,
        )

    # ── restore ─────────────────────────────────────────────────────
    def restore(self, checkpoint_id: str) -> RestoreResult:
        """Restore every entry of a checkpoint to its original path. Git
        entries stream from HEAD via cat-file; quarantine entries copy back.
        """
        # FAIL CLOSED on a malicious id: a checkpoint id is a single dir name,
        # never a path with separators/traversal that could escape the store.
        if (
            not checkpoint_id
            or "/" in checkpoint_id
            or "\\" in checkpoint_id
            or ".." in checkpoint_id
        ):
            return RestoreResult(
                ok=False,
                checkpoint_id=checkpoint_id,
                reason="invalid checkpoint id",
            )
        cpdir = self.checkpoints_root() / checkpoint_id
        manifest_path = cpdir / _MANIFEST_NAME
        if not manifest_path.is_file():
            return RestoreResult(
                ok=False,
                checkpoint_id=checkpoint_id,
                reason="checkpoint manifest not found",
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return RestoreResult(
                ok=False,
                checkpoint_id=checkpoint_id,
                reason=f"manifest unreadable: {exc}",
            )

        restored: list[str] = []
        missing: list[str] = []
        rejected: list[str] = []
        cpdir_resolved = cpdir.resolve()
        for entry in manifest.get("entries", []):
            raw = str(entry.get("original_path") or "")
            # FAIL CLOSED against a tampered/malicious manifest: the target
            # must still resolve INSIDE the project root (no absolute, no
            # traversal, no symlink escape). A poisoned original_path can
            # never make restore write outside the root.
            rel = safe_relpath(self.project_root, raw)
            if rel is None:
                rejected.append(raw)
                continue
            dest = self.project_root / rel
            try:
                if entry.get("mode") == MODE_GIT:
                    data = self._git_cat_file_bytes(str(entry.get("git_sha")))
                else:
                    qf = str(entry.get("quarantine_file") or "")
                    src = cpdir / qf
                    # the quarantine blob must stay WITHIN this checkpoint dir
                    if (
                        ".." in Path(qf).parts
                        or Path(qf).is_absolute()
                        or cpdir_resolved not in src.resolve().parents
                    ):
                        rejected.append(raw)
                        continue
                    if not src.is_file():
                        missing.append(rel)
                        continue
                    data = src.read_bytes()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                restored.append(rel)
            except Exception:
                missing.append(rel)
        # Roll back CREATIONS: delete files the checkpointed mutation created
        # (they had no pre-state to restore). DOUBLE fail-closed against a
        # poisoned manifest: (1) safe_relpath keeps the delete inside the root,
        # and (2) classify_deletion refuses to delete a PROTECTED / control-
        # authority / secret / sentinel path even if the manifest claims the
        # mutation "created" it. A poisoned created_paths can never delete .env,
        # control-plane files, or a DO-NOT-TOUCH file via "rollback".
        removed: list[str] = []
        try:
            from .governed_deletion import CAT_PROTECTED, classify_deletion
        except Exception:
            CAT_PROTECTED, classify_deletion = None, None  # type: ignore[assignment]
        for raw in manifest.get("created_paths", []) or []:
            rel = safe_relpath(self.project_root, str(raw or ""))
            if rel is None:
                rejected.append(str(raw))
                continue
            # Belt-and-braces: a rollback must NEVER delete AIDOCS's own state
            # (.aidocs / .MEMORY), even where classify_deletion treats some of
            # it as cleanable. A legit deslop creation is always a project file,
            # so this never blocks a real rollback.
            rl = rel.lower()
            if rl.startswith((".aidocs/", ".memory/")) or rl in (".aidocs", ".memory"):
                rejected.append(rel)
                continue
            # Fail closed if we cannot verify the path is unprotected.
            if classify_deletion is None:
                rejected.append(rel)
                continue
            try:
                cls, _why = classify_deletion(self.project_root, rel)
            except Exception:
                rejected.append(rel)
                continue
            if cls == CAT_PROTECTED:
                rejected.append(rel)
                continue
            dest = self.project_root / rel
            try:
                if dest.is_file():
                    dest.unlink()
                    removed.append(rel)
                # already-absent ⟹ creation already rolled back (idempotent)
            except OSError:
                missing.append(rel)
        bad = missing + rejected
        reason = ""
        if rejected:
            reason = "manifest rejected (unsafe entry); fail-closed"
        elif missing:
            reason = "some entries could not be restored"
        return RestoreResult(
            ok=not bad,
            checkpoint_id=checkpoint_id,
            restored=restored,
            missing=bad,
            removed=removed,
            reason=reason,
        )

    # ── manifest reads (facade support) ─────────────────────────────
    def read_manifest(self, checkpoint_id: str) -> dict | None:
        """Read a checkpoint manifest dict, or None if absent/unreadable. The
        checkpoint id must be a single dir name (fail-closed on traversal).
        """
        if (
            not checkpoint_id
            or "/" in checkpoint_id
            or "\\" in checkpoint_id
            or ".." in checkpoint_id
        ):
            return None
        mp = self.checkpoints_root() / checkpoint_id / _MANIFEST_NAME
        if not mp.is_file():
            return None
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return None

    def entry_bytes(self, checkpoint_id: str, entry: dict) -> bytes | None:
        """Return the snapshot bytes for one manifest entry (git blob or
        quarantine file), or None if unavailable. Origin-agnostic — the caller
        never needs to know whether it came from git or quarantine.
        """
        try:
            if entry.get("mode") == MODE_GIT:
                return self._git_cat_file_bytes(str(entry.get("git_sha")))
            qf = str(entry.get("quarantine_file") or "")
            if ".." in Path(qf).parts or Path(qf).is_absolute():
                return None
            src = self.checkpoints_root() / checkpoint_id / qf
            cpdir = (self.checkpoints_root() / checkpoint_id).resolve()
            if cpdir not in src.resolve().parents:
                return None
            return src.read_bytes() if src.is_file() else None
        except Exception:
            return None

    # ── audited lifecycle / GC law ──────────────────────────────────
    def list_checkpoints(self) -> list[str]:
        root = self.checkpoints_root()
        if not root.is_dir():
            return []
        ids = []
        for child in root.iterdir():
            if child.is_dir() and (child / _MANIFEST_NAME).is_file():
                ids.append(child.name)
        return sorted(ids)

    def gc(
        self,
        *,
        keep: int = 20,
        max_age_seconds: int | None = None,
        aggressive: bool = False,
        principal_type: str = "agent",
        user_id: str | None = None,
    ) -> dict:
        """Audited checkpoint cleanup — the ONLY sanctioned way to remove a
        checkpoint (the normal delete path refuses the checkpoint store).
        They age out by lifecycle policy: keep the N most-recent, and prune
        any older than max_age_seconds. Every prune is audited.

        Two regimes:
          * CONSERVATIVE (default; agent-safe): a hard KEEP FLOOR and MIN-AGE
            FLOOR are enforced, so no caller — whatever keep/age it passes —
            can prune recent/active restore points. This is the regime the
            user-safe checkpoint-gc surface uses; an agent can tidy old
            checkpoints but can never void live rollback state.
          * AGGRESSIVE (operator-only): floors are lifted, honoring the exact
            keep/max_age. Reserved for an authenticated operator; the agent
            CLI never sets it.

        Returns {ok, pruned, kept, mode}.
        """
        if not aggressive:
            keep = max(keep, _GC_CONSERVATIVE_KEEP_FLOOR)
            floor = _GC_CONSERVATIVE_MIN_AGE_SECONDS
            max_age_seconds = floor if max_age_seconds is None else max(max_age_seconds, floor)
        ids = self.list_checkpoints()  # sorted ascending (oldest first by id ts)
        now = time.time()
        # Protect the newest `keep` checkpoints FIRST, BEFORE any age pruning.
        # This is the invariant: the keep-floor preserves the most-recent
        # rollback state regardless of age, so a conservative (user-safe) GC
        # can never erase ALL checkpoints even when every one is old. Only the
        # checkpoints BEYOND the newest `keep` are age-pruning candidates.
        if keep > 0:
            protected = ids[len(ids) - keep :] if len(ids) > keep else list(ids)
            eligible = ids[: max(0, len(ids) - keep)]
        else:
            protected = []
            eligible = list(ids)
        to_prune: list[str] = []
        for cid in eligible:
            # max_age None ⇒ pure keep-N (everything beyond the floor is pruned);
            # max_age set ⇒ only prune eligible checkpoints older than max_age.
            if max_age_seconds is None:
                to_prune.append(cid)
                continue
            try:
                age = now - (self.checkpoints_root() / cid).stat().st_mtime
            except OSError:
                age = 0
            if age > max_age_seconds:
                to_prune.append(cid)
        pruned_set = set(to_prune)
        survivors = [c for c in ids if c not in pruned_set]
        assert all(p in survivors for p in protected)  # floor never pruned

        pruned: list[str] = []
        for cid in to_prune:
            try:
                shutil.rmtree(self.checkpoints_root() / cid, ignore_errors=False)
                pruned.append(cid)
            except OSError:
                pass
        mode = "aggressive" if aggressive else "conservative"
        _audit_checkpoint_gc(
            self.project_root,
            pruned=pruned,
            kept=len(survivors),
            mode=mode,
            principal_type=principal_type,
            user_id=user_id,
        )
        return {"ok": True, "pruned": pruned, "kept": len(survivors), "mode": mode}


def _audit_checkpoint_gc(
    project_root: Path,
    *,
    pruned: list[str],
    kept: int,
    mode: str,
    principal_type: str,
    user_id: str | None,
) -> None:
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            project_root,
            event_kind="checkpoint_gc",
            source_kind="checkpoint_service",
            capability_name="checkpoint_gc",
            action_kind="gc",
            target_entity=".MEMORY/.checkpoints",
            status="pruned" if pruned else "no_op",
            user_id=user_id,
            principal_type=principal_type,
            scope_id=str(project_root).replace("\\", "/"),
            payload={"pruned": pruned, "pruned_count": len(pruned), "kept": kept, "mode": mode},
        )
    except Exception:
        pass


# ── edit/delete convergence: adapter seam ───────────────────────────────
def checkpoint_before_edit(
    project_root: Path,
    path: str,
    *,
    reason: str = "edit",
    provenance: dict | None = None,
) -> CheckpointResult:
    """Adapter seam for the edit path to adopt the SAME checkpoint law without
    weakening any write guard. An edit may call this to create a pre-overwrite
    restore point (git ref if clean-tracked, else a quarantine COPY that
    leaves the file in place for the overwrite). It is a pure add-on: it never
    blocks, never mutates the target, and is independent of the existing
    file_ops protection pipeline — so wiring it in cannot weaken current
    guards. Returns the CheckpointResult; ok=False means no restore point was
    secured (the caller decides whether to proceed).
    """
    return CheckpointService(project_root).create([path], reason=reason, provenance=provenance)
