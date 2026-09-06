"""Governed file rename — the move AIDOCS could not previously make (#958).

OPERATOR, 2026-08-28: "ai_file(modes) create, rename, and maybe merge ai_delete
into it as the 'delete' mode? — because i want for example to rename
test_session_bind_is_two_phase_916.py to test_session_connect_is_two_phase_916.py"

MEASURED THE SAME HOUR, which is why this exists. That rename had no governed
path at all:
  * `git mv` is REFUSED by the edit gate, correctly — a shell write "bypasses
    every gate by hitting the FS at OS level".
  * No rename/move service existed anywhere under server/aidocs_mcp.
    FileDeleteService had no counterpart.
  * So the only route was ai_create_file + ai_delete, which rewrites the ENTIRE
    FILE THROUGH THE MODEL. For a 280-line security test that meant retyping
    every assertion from three paginated reads. It happened to be safe because
    the result was diffed against the .TRASH copy — but "reconstruct it and
    hope" is not a rename primitive, and the next agent will not necessarily
    diff.

THE REFUSAL SURFACE IS DELETE'S, REUSED RATHER THAN RESTATED (doctrine XXII —
one logic, one home). This module deliberately calls FileDeleteService's
validators instead of copying the four checks: a second spelling of a security
ladder is one that drifts, and the ladder here is the same question asked twice.
Reaching for the underscore-prefixed methods of a sibling in the same package is
the price of not duplicating them, and it is paid knowingly.

BOTH ENDPOINTS ARE VALIDATED, WHICH IS THE PART A DELETE NEVER HAD TO DO. A
rename has a destination, and a destination is an attack surface a deletion does
not possess: the same call that moves a file OUT of a reserved prefix can move
one INTO it. `.MEMORY/`, `.git/`, `.aidocs/`, `.TRASH/` and the forbidden
basenames (.env*, credentials, id_rsa, authorized_keys, release trust files) are
therefore checked on the SOURCE and on the DESTINATION, with the same
case-insensitive comparison that sealed the delete path in 2026-05-27.

NO CLOBBER, EVER. A rename onto an existing path is refused rather than
overwriting: a move that destroys a file is a delete wearing a rename's name,
and it would slip past both the .TRASH/ recovery the delete path guarantees and
the `reason` the audit records.

WHAT THIS DOES NOT DO: directories (the single-file invariant delete already
holds), globs, or batches. One file, named explicitly, both ends checked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .file_delete_service import _FORBIDDEN_BASENAMES, FileDeleteService


@dataclass
class RenameResult:
    """Typed outcome. `ok` is the only success signal; every refusal carries a
    machine-readable `error` and a sentence a caller can act on."""

    ok: bool
    src: str = ""
    dst: str = ""
    error: str = ""
    error_message: str = ""
    reason: str = ""
    #: Memories anchored to the SOURCE path. Reported, never fatal — see
    #: FileRenameService.rename for why this warns instead of refusing.
    anchored_memories: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok}
        if self.src:
            out["src"] = self.src
        if self.dst:
            out["dst"] = self.dst
        if self.error:
            out["error"] = self.error
        if self.error_message:
            out["error_message"] = self.error_message
        if self.reason:
            out["reason"] = self.reason
        if self.anchored_memories:
            out["anchored_memories"] = self.anchored_memories
        return out


class FileRenameService:
    """Rename one project-relative file to another project-relative path."""

    def __init__(
        self,
        hub: Any = None,
        delete_service: FileDeleteService | None = None,
    ) -> None:
        # COMPOSED, not subclassed: a rename is not a kind of delete, and
        # inheriting would expose delete()/restore() on this surface.
        #
        # `hub` is threaded through because FileDeleteService requires it ("hub
        # is required for event recording + config reads"). The three validators
        # borrowed here — _resolve_canonical, _is_reserved,
        # _is_protected_in_registry — touch none of that, so a validation-only
        # caller may pass None. It is passed anyway rather than hardcoding None,
        # so that if a validator ever DOES reach for the hub this keeps working
        # instead of failing in a path only a refusal exercises.
        self._v = delete_service or FileDeleteService(hub)

    # ── validation ──────────────────────────────────────────────────────────

    def _validate(
        self,
        project_root: Path,
        path: str,
        *,
        endpoint: str,
    ) -> tuple[Path | None, str, RenameResult | None]:
        """Delete's ladder, run against ONE endpoint.

        `endpoint` ("source"/"destination") is threaded only into the message,
        because a refusal that does not say WHICH end was rejected sends the
        caller re-reading their own arguments to find out.
        """
        resolved, rel_posix, refusal = self._v._resolve_canonical(project_root, path)
        if refusal is not None:
            return None, "", RenameResult(
                ok=False,
                error=refusal.error or "outside_project",
                error_message=f"{endpoint}: {refusal.error_message}",
            )
        assert resolved is not None and rel_posix is not None

        if self._v._is_reserved(rel_posix):
            return None, "", RenameResult(
                ok=False,
                error="reserved_path",
                error_message=(
                    f"{endpoint} {rel_posix!r} is under a reserved prefix "
                    f"(.git/, .MEMORY/, .TRASH/, .aidocs/, …); ai_file refuses "
                    f"to rename into or out of one"
                ),
            )

        if resolved.name.lower() in {n.lower() for n in _FORBIDDEN_BASENAMES}:
            return None, "", RenameResult(
                ok=False,
                error="forbidden_basename",
                error_message=(
                    f"{endpoint} filename {resolved.name!r} is on the forbidden "
                    f"list (secrets / trust material); ai_file refuses"
                ),
            )

        if self._v._is_protected_in_registry(project_root, rel_posix):
            return None, "", RenameResult(
                ok=False,
                error="reserved_path",
                error_message=(
                    f"{endpoint} {rel_posix!r} is in the protected-file registry"
                ),
            )

        return resolved, rel_posix, None

    # ── the operation ───────────────────────────────────────────────────────

    def rename(
        self,
        project_root: Path,
        src: str,
        dst: str,
        *,
        reason: str = "",
    ) -> RenameResult:
        """Move `src` to `dst`, both project-relative, both validated."""
        project_root = Path(project_root).resolve()

        src_abs, src_rel, refusal = self._validate(project_root, src, endpoint="source")
        if refusal is not None:
            return refusal
        dst_abs, dst_rel, refusal = self._validate(
            project_root, dst, endpoint="destination"
        )
        if refusal is not None:
            return refusal
        assert src_abs is not None and dst_abs is not None

        if src_rel == dst_rel:
            return RenameResult(
                ok=False,
                src=src_rel,
                dst=dst_rel,
                error="same_path",
                error_message="source and destination are the same path",
            )
        if not src_abs.exists():
            return RenameResult(
                ok=False,
                src=src_rel,
                dst=dst_rel,
                error="not_found",
                error_message=f"no such file: {src_rel}",
            )
        if src_abs.is_dir():
            return RenameResult(
                ok=False,
                src=src_rel,
                dst=dst_rel,
                error="is_a_directory",
                error_message=(
                    f"{src_rel} is a directory; ai_file renames a single file "
                    f"(the same single-file invariant ai_delete holds)"
                ),
            )
        if dst_abs.exists():
            # NEVER CLOBBER. Overwriting here would be a delete with no .TRASH
            # entry, no `reason`, and no file_deleted audit row — every recovery
            # guarantee the delete path makes, silently skipped.
            return RenameResult(
                ok=False,
                src=src_rel,
                dst=dst_rel,
                error="destination_exists",
                error_message=(
                    f"{dst_rel} already exists; ai_file will not overwrite it. "
                    f"Delete it explicitly first if that is the intent."
                ),
            )

        anchors = self._anchored_memories(project_root, src_rel)

        dst_abs.parent.mkdir(parents=True, exist_ok=True)
        # os.replace, not Path.rename: atomic on both platforms for a
        # same-filesystem move, and it does not silently differ on Windows the
        # way rename() does when the destination exists. The no-clobber check
        # above is the guarantee; this is belt to its braces.
        os.replace(src_abs, dst_abs)

        self._audit(project_root, src_rel, dst_rel, reason, anchors)
        return RenameResult(
            ok=True,
            src=src_rel,
            dst=dst_rel,
            reason=reason,
            anchored_memories=anchors,
        )

    # ── reporting ───────────────────────────────────────────────────────────

    def _anchored_memories(self, project_root: Path, rel_posix: str) -> list[dict]:
        """Memories anchored to the path being moved.

        REPORTED, NOT FATAL, and the asymmetry is deliberate. A rename that
        refused whenever an anchor existed would make the most-documented files
        the hardest to rename — precisely backwards. But an anchor silently
        pointing at a path that no longer exists is how a knowledge base rots,
        and unlike a delete (which announces itself) a rename looks like nothing
        happened. So the caller is handed the list and can re-anchor.

        Best-effort: a failure to LOOK must not fail the rename itself. But the
        lookup is a REAL one — edit_memory_gate.query_anchored_memories is the
        same function the edit path uses, called with file_path only, which per
        its own contract returns "all rows with file_path=fp (both kinds)". A
        plausible-looking call to a method that does not exist would sit inside
        this except and report "no anchors" forever, which is worse than not
        reporting at all.
        """
        try:
            from .edit_memory_gate import query_anchored_memories

            rows = query_anchored_memories(project_root, file_path=rel_posix)
            return [
                {
                    "target_path": r.target_path,
                    "title": r.title,
                    "severity": r.severity,
                    "anchor_symbol": r.anchor_symbol,
                }
                for r in (rows or [])
            ]
        except Exception:  # noqa: BLE001 — advisory only
            return []

    def _audit(
        self,
        project_root: Path,
        src_rel: str,
        dst_rel: str,
        reason: str,
        anchors: list[dict],
    ) -> None:
        """Record `file_renamed`. Registered in EVENT_KIND_RETENTION as a
        DECISION, matching `file_deleted` — both answer "who moved this and
        why" long after the fact, which is the question a forensic reader
        actually brings to a vanished path."""
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                "file_renamed",
                "file_rename_service",
                status="ok",
                payload={
                    "src": src_rel,
                    "dst": dst_rel,
                    "reason": reason,
                    "anchored_memory_count": len(anchors),
                },
            )
        except Exception:  # noqa: BLE001 — the move already happened
            pass
