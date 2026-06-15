"""ai_delete(path, reason) — governed file deletion service.

Built 2026-05-27 per the canonical design at
``.MEMORY/sessions/2026-04-22-oc-plugin-testing/plans/lane-control-architecture.md``
(authored 2026-04-24). The design lay dormant for ~5 weeks while the
rm-floor block left agents with no governed delete path; this service
closes that gap.

Shape:
    delete(path, reason, …) → {"ok": True,  "trash_id": ..., "size_bytes": ...}
    delete(path, reason, …) → {"ok": True,  "already_deleted": True,
                                 "trash_id": <prior>, "size_bytes": <recorded>}
    delete(path, reason, …) → {"ok": False, "error": "too_large",
                                 "size_bytes": ..., "ceiling_bytes": ...}
    delete(path, reason, …) → {"ok": False, "error": <typed>, ...}

Behavior (per design):
  * Single file only — no glob, no batch, no directory tree
  * Canonical project-relative path — resolves via
    Path(path).resolve().relative_to(project_root)
  * Moves to .TRASH/<YYYY-MM-DD>/<random-hex>-<basename>
  * Hard 25 MB per-file ceiling (config: delete.max_trash_bytes)
  * Per-project .TRASH/ total-bytes cap (config:
    delete.max_trash_total_bytes; default 500 MB; <=0 disables).
    Refuses with typed trash_quota_exceeded BEFORE the move when the
    next delete would exceed the cap.
  * Optional .TRASH/ item-count cap (config:
    delete.max_trash_items; default 1000; <=0 disables).
    Refuses with typed trash_quota_exceeded when the count cap is
    already at the limit.
  * Idempotent within 15 min (config: delete.idempotency_window_minutes)
  * Reuses every existing gate: require_active_task (caller's job),
    protected_file_registry, security zone, reserved-path machinery,
    heuristic_judge (post-recovery audit), execution_events
  * NO new codepaths invented — every refusal mode is an existing one

The trash directory `.TRASH/` lives at project root, parallel to
`.MEMORY/`. Treated as forbidden-class infrastructure by
protected_paths_classifier (2026-05-27): raw Read / Grep / Write /
ai_get_lines on `.TRASH/*` are refused. Governed access is only via
ai_delete (writes-only, moves INTO .TRASH/) and the future admin-
only ai_trash_sweep tool. This prevents agent rehydration of deleted
secrets by reading the trash contents after the operator thought
they were gone.
"""

from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Config defaults ─────────────────────────────────────────────────
DEFAULT_MAX_TRASH_BYTES: int = 25 * 1024 * 1024  # 25 MB per-file
DEFAULT_IDEMPOTENCY_WINDOW_MINUTES: int = 15

# Per-project trash containment (2026-05-27 sealing): caps the TOTAL
# bytes + item count under .TRASH/. Without these the agent could
# slowly accumulate gigabytes of trash and the operator would have no
# operational backstop. Both default to non-zero; set <= 0 to disable.
DEFAULT_MAX_TRASH_TOTAL_BYTES: int = 500 * 1024 * 1024  # 500 MB project-wide
DEFAULT_MAX_TRASH_ITEMS: int = 1000  # files under .TRASH/

# ── Regenerable-artifact patterns (2026-05-27 follow-on) ────────────
# Files matching these shapes are CACHE/BUILD output: deterministic to
# regenerate, no original content worth recovering. ai_delete on these
# bypasses .TRASH/ entirely (hard-delete + audit) so the trash quota
# doesn't fill up with deterministic noise. Doctrine: auto-trashable
# stuff should NOT go to trash. Operator's `reason` field is still
# required (audit hardening A), but the disposition is `hard_deleted`
# instead of `trashed` — no trash_id, no recovery path advertised.
#
# CONSERVATIVE list: each entry is unambiguously regenerable. When in
# doubt, leave OUT — falling through to the trash path is the safe
# default. To extend (e.g. node_modules/, *.so) ship a follow-up PR
# that explicitly justifies why the new pattern is regenerable.

# Directory tokens — matched as path segments (preceded + followed
# by '/'). Forward-slash-normalized; lowercased on both sides at
# match time (case-insensitive — see _classify_regenerable).
#
# DOCTRINE (2026-05-27 narrowing): only TOOL-MANAGED CACHE DIRS make
# this list. The earlier version included `dist`, `build`, `target`,
# `out` — those names are ambiguous: a project might use `target/`
# for build output (Rust/Maven convention) OR as the destination
# for generated data (`target/results-2026-05-27.json`). Hard-
# deleting an ambiguous dir's contents is the EXACT failure mode
# the trash system exists to prevent.
#
# These names now go through the regular trash path (recoverable via
# `mv` if the operator misjudges). The 25 MB per-file ceiling + the
# project-wide trash quota + the operator's `reason` field are
# sufficient safety on the trash path.
#
# What DOES make the list: directories whose contents are EXCLUSIVELY
# managed by a specific tool, deterministically regenerable, and
# carry no user data. The criterion is "literally a tool cache."
_REGENERABLE_DIR_TOKENS: tuple[str, ...] = (
    # Python bytecode + test/type/lint caches
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "htmlcov",  # pytest-cov HTML output — pure derived data
    # Web framework build caches (each is name-specific to its tool;
    # operator using these names for non-cache purposes would be
    # deeply unconventional)
    ".next",  # Next.js build cache
    ".nuxt",  # Nuxt build cache
    ".svelte-kit",  # SvelteKit build cache
    ".parcel-cache",  # Parcel bundler cache
    ".turbo",  # Turborepo cache
    # JVM ecosystem cache
    ".gradle",  # Gradle cache (NOT `target/` which conflicts with Maven user dirs)
    # NOTE: dist, build, target, out INTENTIONALLY OMITTED — see
    # doctrine comment above. They go to trash and are recoverable.
)

# Basenames — exact match against the filename portion.
_REGENERABLE_BASENAMES: frozenset[str] = frozenset(
    {
        ".coverage",
        "tsconfig.tsbuildinfo",
        ".eslintcache",
    },
)

# File extensions — matched against the trailing extension.
_REGENERABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".o",
        ".obj",
        ".class",
    },
)

# Reserved-path prefixes — never deleteable. These live at project
# root and represent infra/state the agent must NEVER touch via this
# tool (a separate admin path handles their lifecycle).
_RESERVED_PREFIXES: tuple[str, ...] = (
    ".git",
    ".MEMORY",
    ".TRASH",
    ".aidocs",
    ".github",  # workflows + CODEOWNERS — admin-only changes
    ".venv",  # local venvs — operator's tooling, not the agent's
    "node_modules",
)

# Filename patterns that are NEVER deleteable regardless of location.
# Mirrors the secrets-classifier list but tightened for delete-context
# (here, "delete is wrong" matters MORE than for read — a deleted .env
# can take production down).
_FORBIDDEN_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        "credentials",  # AWS credentials shape
        "id_rsa",  # SSH private keys
        "id_ed25519",
        "id_ecdsa",
        "known_hosts",
        "authorized_keys",
        ".htpasswd",
        "release_pubkey.pem",  # AIDOCS' own trust material
        "release_manifest.json",
        "release_manifest.json.sig",
    },
)


@dataclass(frozen=True)
class DeleteResult:
    """Structured outcome from FileDeleteService.delete.

    ok:               True iff the call succeeded (including
                      idempotent already_deleted=True cases).
    trash_id:         Relative path under .TRASH/ where the file
                      landed (or where it landed previously, for
                      idempotent repeats). None on refusal.
    size_bytes:       The file's size at delete-time (or at the time
                      of the prior delete, for idempotent repeats).
    already_deleted:  True iff the call detected a prior delete of
                      the same path within the idempotency window
                      and returned the prior trash_id.
    error:            Typed error code on refusal; one of:
                        "too_large", "target_missing", "outside_project",
                        "absolute_path_refused", "reserved_path",
                        "forbidden_basename", "no_session_id",
                        "io_error".
    reason:           Operator's reason string (echoed back for audit).
    rel_posix:        The canonical project-relative path, computed
                      once at the top of the call and used everywhere
                      downstream. None on early refusal.
    disposition:      How the delete was carried out (2026-05-27
                      follow-on). Either:
                        "trashed"      — moved to .TRASH/<date>/<hex>-
                                          <basename>, recoverable via mv
                        "hard_deleted" — file matched a regenerable
                                          pattern (cache/build artifact),
                                          removed via os.remove with NO
                                          trash entry. trash_id is None.
    regenerable_reason: When disposition == "hard_deleted", names the
                      pattern that classified the file as regenerable
                      (e.g. "__pycache__ dir", "*.pyc extension"). For
                      audit forensics + dashboards.
    """

    ok: bool
    trash_id: str | None = None
    size_bytes: int | None = None
    already_deleted: bool = False
    error: str | None = None
    error_message: str = ""
    reason: str = ""
    rel_posix: str | None = None
    disposition: str = "trashed"
    regenerable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape matching the design return contract."""
        out: dict[str, Any] = {"ok": self.ok}
        if self.trash_id is not None:
            out["trash_id"] = self.trash_id
        if self.size_bytes is not None:
            out["size_bytes"] = self.size_bytes
        if self.already_deleted:
            out["already_deleted"] = True
        if self.error:
            out["error"] = self.error
            out["error_message"] = self.error_message
        if self.reason:
            out["reason"] = self.reason
        if self.rel_posix:
            out["path"] = self.rel_posix
        # Disposition is only emitted on successful deletes (the
        # operator/agent cares: did this go to trash or get hard-
        # deleted?). On refusals it's omitted — the error tells the
        # story.
        if self.ok:
            out["disposition"] = self.disposition
            if self.regenerable_reason:
                out["regenerable_reason"] = self.regenerable_reason
        return out


class FileDeleteService:
    """ai_delete dispatcher — trash-based, single-file, idempotent.

    Stateless other than via execution_events (idempotency check
    queries the audit log). Hub is required for event recording +
    config reads.
    """

    def __init__(self, hub: Any) -> None:
        self.hub = hub

    # ── Public entry point ─────────────────────────────────────────

    def delete(
        self,
        *,
        project_root: Path,
        path: str,
        reason: str,
        session_id: str = "",
        actor: str = "agent",
        tool_name: str = "ai_delete",
    ) -> DeleteResult:
        """Delete a single project-relative file by moving it to .TRASH/.

        Refuses on every design failure mode. See DeleteResult.error
        for the typed reasons. Records an execution_events row on
        every outcome (success, refusal, idempotent repeat) so the
        audit chain reconstructs end-to-end.
        """
        project_root = project_root.resolve()
        reason = (reason or "").strip()
        original_input = path

        # ── 1. Canonical project-relative path resolution ─────────
        resolved, rel_posix, err = self._resolve_canonical(project_root, path)
        if err is not None:
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error=err.error or "outside_project",
                error_message=err.error_message,
            )
            return err

        # ── 2. Reserved-prefix sandbox ────────────────────────────
        if self._is_reserved(rel_posix):
            r = DeleteResult(
                ok=False,
                error="reserved_path",
                error_message=(
                    f"path is under a reserved prefix; cannot be deleted via ai_delete: {rel_posix}"
                ),
                reason=reason,
                rel_posix=rel_posix,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="reserved_path",
                error_message=r.error_message,
            )
            return r

        # ── 3. Forbidden basename (secrets / trust material) ──────
        if resolved.name.lower() in {n.lower() for n in _FORBIDDEN_BASENAMES}:
            r = DeleteResult(
                ok=False,
                error="forbidden_basename",
                error_message=(
                    f"filename {resolved.name!r} is on the forbidden list "
                    f"(secrets / trust material); ai_delete refuses"
                ),
                reason=reason,
                rel_posix=rel_posix,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="forbidden_basename",
                error_message=r.error_message,
            )
            return r

        # ── 4. Protected-file registry check ──────────────────────
        if self._is_protected_in_registry(project_root, rel_posix):
            r = DeleteResult(
                ok=False,
                error="reserved_path",
                error_message=(
                    "path is registered in protected_file_registry; "
                    "clear the protection first if intentional"
                ),
                reason=reason,
                rel_posix=rel_posix,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="reserved_path",
                error_message=r.error_message,
            )
            return r

        # ── 5. Idempotency check (must precede missing-file check) ─
        # If the file is missing AND there's a recent matching delete
        # event, this is a kill+respawn replay. Return the prior
        # trash_id deterministically.
        if not resolved.exists():
            prior = self._find_recent_delete_event(
                project_root,
                session_id=session_id,
                rel_posix=rel_posix,
            )
            if prior is not None:
                self._audit_idempotent_repeat(
                    project_root,
                    session_id=session_id,
                    tool_name=tool_name,
                    actor=actor,
                    rel_posix=rel_posix,
                    reason=reason,
                    prior_trash_id=prior["trash_id"],
                )
                return DeleteResult(
                    ok=True,
                    trash_id=prior["trash_id"],
                    size_bytes=prior.get("size_bytes"),
                    already_deleted=True,
                    reason=reason,
                    rel_posix=rel_posix,
                )
            r = DeleteResult(
                ok=False,
                error="target_missing",
                error_message=(
                    f"target path does not exist and no recent delete event matches: {rel_posix}"
                ),
                reason=reason,
                rel_posix=rel_posix,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="target_missing",
                error_message=r.error_message,
            )
            return r

        # ── 6. Single-file invariant ──────────────────────────────
        if not resolved.is_file():
            r = DeleteResult(
                ok=False,
                error="not_a_file",
                error_message=(
                    f"ai_delete is single-file only; refusing to delete "
                    f"a directory / symlink-to-directory: {rel_posix}"
                ),
                reason=reason,
                rel_posix=rel_posix,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="not_a_file",
                error_message=r.error_message,
            )
            return r

        # ── 7. Size ceiling ───────────────────────────────────────
        size_bytes = resolved.stat().st_size
        ceiling = self._max_trash_bytes(project_root)
        if size_bytes > ceiling:
            r = DeleteResult(
                ok=False,
                error="too_large",
                error_message=(
                    f"file is {size_bytes} bytes; ai_delete ceiling is "
                    f"{ceiling}. Operator deletes manually if intentional."
                ),
                reason=reason,
                rel_posix=rel_posix,
                size_bytes=size_bytes,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="too_large",
                error_message=r.error_message,
                extra={"size_bytes": size_bytes, "ceiling_bytes": ceiling},
            )
            return r

        # ── 7a. Regenerable-artifact hard-delete (2026-05-27 follow-on) ─
        # Cache / build / bytecode files have no original content worth
        # recovering — putting them in .TRASH/ is pure quota waste.
        # Classify, and if regenerable, hard-delete via os.remove +
        # audit with disposition="hard_deleted". The cascade above
        # (canonical path / reserved prefix / forbidden basename /
        # protected registry / single-file invariant / size ceiling)
        # already ran — this is the LAST branch before the trash move.
        regen_reason = self._classify_regenerable(rel_posix, resolved.name)
        if regen_reason is not None:
            ok, err = self._hard_delete(resolved)
            if not ok:
                r = DeleteResult(
                    ok=False,
                    error="io_error",
                    error_message=err or "hard-delete failed",
                    reason=reason,
                    rel_posix=rel_posix,
                    size_bytes=size_bytes,
                )
                self._audit_refusal(
                    project_root,
                    session_id=session_id,
                    tool_name=tool_name,
                    actor=actor,
                    original_input=original_input,
                    reason=reason,
                    error="io_error",
                    error_message=r.error_message,
                )
                return r
            self._audit_hard_delete(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                rel_posix=rel_posix,
                reason=reason,
                size_bytes=size_bytes,
                regenerable_reason=regen_reason,
            )
            return DeleteResult(
                ok=True,
                size_bytes=size_bytes,
                reason=reason,
                rel_posix=rel_posix,
                disposition="hard_deleted",
                regenerable_reason=regen_reason,
            )

        # ── 7b. Per-project .TRASH/ quota (2026-05-27 sealing) ─────
        # Without these caps an agent could accumulate gigabytes of
        # trash and the operator has no operational backstop. Both
        # checks are BEFORE the move so a refusal leaves the source
        # file in place — the deletion is not "started then rolled
        # back," it never began. Audit row carries both the current
        # trash state + the offending file's size for forensics.
        quota_refusal = self._check_quota(
            project_root,
            incoming_size=size_bytes,
            rel_posix=rel_posix,
            reason=reason,
            session_id=session_id,
            tool_name=tool_name,
            actor=actor,
            original_input=original_input,
        )
        if quota_refusal is not None:
            return quota_refusal

        # ── 8. Move to .TRASH/<YYYY-MM-DD>/<hex>-<basename> ───────
        trash_id, error = self._move_to_trash(project_root, resolved, rel_posix)
        if trash_id is None:
            r = DeleteResult(
                ok=False,
                error="io_error",
                error_message=error or "filesystem move failed",
                reason=reason,
                rel_posix=rel_posix,
                size_bytes=size_bytes,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="io_error",
                error_message=r.error_message,
            )
            return r

        # ── 9. Record successful delete (this is what idempotency reads) ─
        self._audit_success(
            project_root,
            session_id=session_id,
            tool_name=tool_name,
            actor=actor,
            rel_posix=rel_posix,
            trash_id=trash_id,
            size_bytes=size_bytes,
            reason=reason,
        )

        return DeleteResult(
            ok=True,
            trash_id=trash_id,
            size_bytes=size_bytes,
            reason=reason,
            rel_posix=rel_posix,
        )

    # ── Internal helpers ───────────────────────────────────────────

    def _resolve_canonical(
        self,
        project_root: Path,
        path: str,
    ) -> tuple[Path | None, str | None, DeleteResult | None]:
        """Resolve to canonical project-relative path or return a typed
        refusal. Refuses absolute paths and `..`-escape attempts.
        Returns (resolved_abs, rel_posix, None) on success.
        """
        if not path or not path.strip():
            return (
                None,
                None,
                DeleteResult(
                    ok=False,
                    error="outside_project",
                    error_message="empty path",
                ),
            )
        p = Path(path.strip())
        if p.is_absolute():
            return (
                None,
                None,
                DeleteResult(
                    ok=False,
                    error="absolute_path_refused",
                    error_message=(
                        f"ai_delete refuses absolute paths; pass a project-"
                        f"relative path. Got: {path!r}"
                    ),
                ),
            )
        candidate = (project_root / p).resolve()
        try:
            rel = candidate.relative_to(project_root)
        except ValueError:
            return (
                None,
                None,
                DeleteResult(
                    ok=False,
                    error="outside_project",
                    error_message=(
                        f"resolved path is outside project_root "
                        f"(parent-traversal or symlink escape): {path!r}"
                    ),
                ),
            )
        return candidate, rel.as_posix(), None

    def _is_reserved(self, rel_posix: str) -> bool:
        """Refuse anything under a reserved top-level dir.

        Case-insensitive match (2026-05-27 sealing): an agent must not
        be able to bypass the .TRASH/ / .MEMORY/ / .git/ / .aidocs/
        protection by creating or naming a case-variant directory
        (`.trash/`, `.Trash/`, `.MEMORY/` lowercase, etc.) on a
        case-sensitive POSIX filesystem. Both sides are lowercased
        at compare time.
        """
        first = rel_posix.split("/", 1)[0] if rel_posix else ""
        first_lc = first.lower()
        return any(p.lower() == first_lc for p in _RESERVED_PREFIXES)

    def _is_protected_in_registry(
        self,
        project_root: Path,
        rel_posix: str,
    ) -> bool:
        """Best-effort query of protected_file_registry. On any error,
        fail OPEN here (the reserved-prefix + forbidden-basename checks
        above are the structural protection; the registry is a soft
        operator-managed list).
        """
        try:
            from .protected_file_registry_store import (
                ProtectedFileRegistryStore,
            )

            store = ProtectedFileRegistryStore()
            return bool(store.is_protected(project_root, rel_posix))
        except Exception:
            return False

    def _max_trash_bytes(self, project_root: Path) -> int:
        try:
            from .config import get_setting

            raw = get_setting(
                "delete.max_trash_bytes",
                project_root=project_root,
            )
            if raw is None:
                return DEFAULT_MAX_TRASH_BYTES
            return int(raw)
        except Exception:
            return DEFAULT_MAX_TRASH_BYTES

    def _idempotency_window_seconds(self, project_root: Path) -> int:
        try:
            from .config import get_setting

            raw = get_setting(
                "delete.idempotency_window_minutes",
                project_root=project_root,
            )
            if raw is None:
                return DEFAULT_IDEMPOTENCY_WINDOW_MINUTES * 60
            return int(raw) * 60
        except Exception:
            return DEFAULT_IDEMPOTENCY_WINDOW_MINUTES * 60

    # ── Quota helpers (2026-05-27 sealing) ─────────────────────────

    def _max_trash_total_bytes(self, project_root: Path) -> int:
        """Project-wide .TRASH/ byte cap. <= 0 disables."""
        try:
            from .config import get_setting

            raw = get_setting(
                "delete.max_trash_total_bytes",
                project_root=project_root,
            )
            if raw is None:
                return DEFAULT_MAX_TRASH_TOTAL_BYTES
            return int(raw)
        except Exception:
            return DEFAULT_MAX_TRASH_TOTAL_BYTES

    def _max_trash_items(self, project_root: Path) -> int:
        """Project-wide .TRASH/ item-count cap. <= 0 disables."""
        try:
            from .config import get_setting

            raw = get_setting(
                "delete.max_trash_items",
                project_root=project_root,
            )
            if raw is None:
                return DEFAULT_MAX_TRASH_ITEMS
            return int(raw)
        except Exception:
            return DEFAULT_MAX_TRASH_ITEMS

    def _compute_trash_state(
        self,
        project_root: Path,
    ) -> tuple[int, int]:
        """Walk .TRASH/ once and return (total_bytes, file_count).

        Returns (0, 0) when .TRASH/ doesn't exist yet (first delete in
        the project — that IS the truthful empty state, not a failure).

        Raises OSError on filesystem walk failure (2026-05-27 gate-
        proof goal: fail CLOSED, not open). The prior implementation
        returned (0, 0) on any error, which would silently let an
        over-quota delete through if e.g. permission was momentarily
        denied on the trash root. The caller catches the OSError and
        refuses with typed `trash_quota_unavailable`.

        Per-file stat failures are still tolerated (a missing file
        during walk is fine — just skip it). The CATASTROPHIC failure
        case is "I can't see the trash at all" — that one fails closed.
        """
        trash_root = project_root / ".TRASH"
        # No trash dir = empty state. NOT a failure.
        if not trash_root.exists() or not trash_root.is_dir():
            return 0, 0
        total = 0
        count = 0
        # rglob errors propagate as OSError to the caller (fail closed).
        for entry in trash_root.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    # Per-file stat failure: skip this entry but keep
                    # counting. A single unstat-able file shouldn't
                    # wedge the whole measurement.
                    pass
                count += 1
        return total, count

    def _check_quota(
        self,
        project_root: Path,
        *,
        incoming_size: int,
        rel_posix: str,
        reason: str,
        session_id: str,
        tool_name: str,
        actor: str,
        original_input: str,
    ) -> DeleteResult | None:
        """Refuse with trash_quota_exceeded when the incoming delete
        would push .TRASH/ over the configured caps. Returns None
        (proceed) when caps are disabled OR the move fits.
        """
        max_bytes = self._max_trash_total_bytes(project_root)
        max_items = self._max_trash_items(project_root)

        # Both <= 0 means caps disabled — skip the measurement walk.
        if max_bytes <= 0 and max_items <= 0:
            return None

        # Fail CLOSED on trash measurement failure (2026-05-27 gate-
        # proof goal). The prior impl returned (0, 0) on OSError,
        # which would silently let an over-quota delete through if
        # the trash dir was momentarily unreadable.
        try:
            current_bytes, current_count = self._compute_trash_state(project_root)
        except OSError as e:
            msg = (
                f".TRASH/ quota measurement failed (filesystem unreadable): "
                f"{e}. Refusing the delete out of caution — re-try after the "
                f"operator inspects .TRASH/ permissions / disk health."
            )
            r = DeleteResult(
                ok=False,
                error="trash_quota_unavailable",
                error_message=msg,
                reason=reason,
                rel_posix=rel_posix,
                size_bytes=incoming_size,
            )
            self._audit_refusal(
                project_root,
                session_id=session_id,
                tool_name=tool_name,
                actor=actor,
                original_input=original_input,
                reason=reason,
                error="trash_quota_unavailable",
                error_message=msg,
                extra={
                    "size_bytes": incoming_size,
                    "underlying_error": str(e)[:300],
                },
            )
            return r

        projected_bytes = current_bytes + incoming_size
        projected_count = current_count + 1

        bytes_exceeded = max_bytes > 0 and projected_bytes > max_bytes
        items_exceeded = max_items > 0 and projected_count > max_items
        if not (bytes_exceeded or items_exceeded):
            return None

        # Pick the most-informative dimension for the message.
        if bytes_exceeded and items_exceeded:
            detail = (
                f"both byte cap ({projected_bytes} > {max_bytes}) and "
                f"item cap ({projected_count} > {max_items}) would be "
                f"exceeded"
            )
        elif bytes_exceeded:
            detail = (
                f"byte cap would be exceeded: {projected_bytes} bytes "
                f"after move > {max_bytes} cap "
                f"(currently {current_bytes}, incoming {incoming_size})"
            )
        else:  # items_exceeded
            detail = (
                f"item cap would be exceeded: {projected_count} files "
                f"after move > {max_items} cap "
                f"(currently {current_count})"
            )

        msg = (
            f".TRASH/ quota exceeded — {detail}. Run the operator-only "
            f"ai_trash_sweep tool (or `rm -rf .TRASH/<old-date>` "
            f"manually) to recover capacity."
        )
        result = DeleteResult(
            ok=False,
            error="trash_quota_exceeded",
            error_message=msg,
            reason=reason,
            rel_posix=rel_posix,
            size_bytes=incoming_size,
        )
        self._audit_refusal(
            project_root,
            session_id=session_id,
            tool_name=tool_name,
            actor=actor,
            original_input=original_input,
            reason=reason,
            error="trash_quota_exceeded",
            error_message=msg,
            extra={
                "size_bytes": incoming_size,
                "current_trash_bytes": current_bytes,
                "current_trash_items": current_count,
                "projected_trash_bytes": projected_bytes,
                "projected_trash_items": projected_count,
                "max_trash_total_bytes": max_bytes,
                "max_trash_items": max_items,
            },
        )
        return result

    def _classify_regenerable(
        self,
        rel_posix: str,
        basename: str,
    ) -> str | None:
        """Return a human-readable reason string if the file is a
        regenerable cache/build artifact, else None.

        Order of checks:
          1. Exact basename match (.coverage, .eslintcache, etc.)
          2. File extension (.pyc, .o, .class, etc.)
          3. Path-segment match for cache/build directories
             (__pycache__/, dist/, build/, target/, .pytest_cache/, ...)

        Conservative: any uncertain shape falls through to the trash
        path. Extending this list = a separate, explicitly-justified PR.
        """
        # 1. Exact basename
        if basename in _REGENERABLE_BASENAMES:
            return f"basename: {basename}"

        # 2. File extension
        if "." in basename:
            ext = "." + basename.rsplit(".", 1)[-1]
            if ext in _REGENERABLE_EXTENSIONS:
                return f"extension: {ext}"

        # 3. Directory segment — `/__pycache__/` etc. anywhere in path.
        # Case-insensitive (both sides lowercased) so a case-variant
        # `__PYCACHE__/` an operator might create on a case-sensitive
        # FS still hard-deletes correctly. Tokens are stored lowercase
        # (see _REGENERABLE_DIR_TOKENS) so only the path needs lower().
        rel = rel_posix.lower()
        for token in _REGENERABLE_DIR_TOKENS:
            tok_lc = token.lower()
            seg = f"/{tok_lc}/"
            if seg in f"/{rel}" or rel.startswith(f"{tok_lc}/"):
                return f"under {token}/"
        return None

    def _hard_delete(self, resolved: Path) -> tuple[bool, str | None]:
        """os.remove the resolved file. Returns (True, None) on success
        or (False, error_message) on OSError. Refuses directories at
        the OS level — even though _classify_regenerable can match a
        path under a regenerable dir, the SINGLE-FILE invariant from
        step 6 above already filtered out directory targets.
        """
        try:
            resolved.unlink()
            return True, None
        except OSError as e:
            return False, f"hard-delete failed: {e}"

    def _move_to_trash(
        self,
        project_root: Path,
        resolved: Path,
        rel_posix: str,
    ) -> tuple[str | None, str | None]:
        """Move file to .TRASH/<YYYY-MM-DD>/<hex>-<basename>.

        Returns (trash_id, None) on success. trash_id is the path
        relative to project_root (e.g. ".TRASH/2026-05-27/abc123-foo.py").
        """
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        trash_dir = project_root / ".TRASH" / date_str
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return None, f"mkdir .TRASH/{date_str}/ failed: {e}"

        # 8-byte hex prefix to disambiguate same-basename deletes in
        # the same day. Cryptographically random (secrets.token_hex)
        # not because we need security but because random.* might be
        # seeded predictably in tests.
        token = secrets.token_hex(4)
        dest_name = f"{token}-{resolved.name}"
        dest = trash_dir / dest_name

        try:
            shutil.move(str(resolved), str(dest))
        except OSError as e:
            return None, f"move {rel_posix!r} → {dest.name!r} failed: {e}"

        trash_id = dest.relative_to(project_root).as_posix()
        return trash_id, None

    def _find_recent_delete_event(
        self,
        project_root: Path,
        *,
        session_id: str,
        rel_posix: str,
    ) -> dict[str, Any] | None:
        """Query execution_events for a recent successful ai_delete on
        the same rel_posix. Returns None when no match within the
        idempotency window. On any query error, returns None (caller
        falls into the target_missing refusal — safe default).
        """
        if not session_id:
            return None
        window_s = self._idempotency_window_seconds(project_root)

        # Query BOTH event kinds — kill+respawn safety must hold for
        # regenerable hard-deletes the same way it does for trash
        # moves. Without this, a worker that hard-deleted __pycache__/
        # then got killed would refuse on respawn even though the
        # operation finished.
        def _query(event_kind: str) -> list[dict[str, Any]] | None:
            try:
                rec = getattr(self.hub.execution, "recent_events", None)
                if callable(rec):
                    return list(
                        rec(
                            project_root,
                            event_kind=event_kind,
                            session_id=session_id,
                            within_seconds=window_s,
                            limit=20,
                        )
                        or [],
                    )
            except Exception:
                pass
            try:
                from .execution_index_store import ExecutionIndexStore

                store = ExecutionIndexStore()
                return list(
                    store.recent_events(
                        project_root,
                        event_kind=event_kind,
                        session_id=session_id,
                        within_seconds=window_s,
                        limit=20,
                    )
                    or [],
                )
            except Exception:
                return None

        for ek in ("file_deleted", "file_hard_deleted"):
            rows = _query(ek)
            if not rows:
                continue
            for row in rows:
                payload = row.get("payload") or {}
                if payload.get("rel_posix") == rel_posix and payload.get("ok"):
                    return {
                        "trash_id": payload.get("trash_id"),
                        "size_bytes": payload.get("size_bytes"),
                        "disposition": payload.get(
                            "disposition",
                            "hard_deleted" if ek == "file_hard_deleted" else "trashed",
                        ),
                    }
        return None

    # ── Audit emission ─────────────────────────────────────────────

    def _audit_success(
        self,
        project_root: Path,
        *,
        session_id: str,
        tool_name: str,
        actor: str,
        rel_posix: str,
        trash_id: str,
        size_bytes: int,
        reason: str,
    ) -> None:
        self._emit(
            project_root,
            event_kind="file_deleted",
            session_id=session_id,
            capability_name=tool_name,
            action_kind="delete",
            target_entity=rel_posix,
            status="completed",
            payload={
                "ok": True,
                "rel_posix": rel_posix,
                "trash_id": trash_id,
                "size_bytes": size_bytes,
                "reason": reason[:500],
                "actor": actor,
                "disposition": "trashed",
            },
        )

    def _audit_hard_delete(
        self,
        project_root: Path,
        *,
        session_id: str,
        tool_name: str,
        actor: str,
        rel_posix: str,
        reason: str,
        size_bytes: int,
        regenerable_reason: str,
    ) -> None:
        """Audit a regenerable-artifact hard-delete. Distinct event
        kind (file_hard_deleted) so dashboards can split trash vs
        hard-delete activity AND so idempotency search can find
        both kinds on kill+respawn replays.
        """
        self._emit(
            project_root,
            event_kind="file_hard_deleted",
            session_id=session_id,
            capability_name=tool_name,
            action_kind="delete",
            target_entity=rel_posix,
            status="completed",
            payload={
                "ok": True,
                "rel_posix": rel_posix,
                "size_bytes": size_bytes,
                "reason": reason[:500],
                "actor": actor,
                "disposition": "hard_deleted",
                "regenerable_reason": regenerable_reason,
            },
        )

    def _audit_idempotent_repeat(
        self,
        project_root: Path,
        *,
        session_id: str,
        tool_name: str,
        actor: str,
        rel_posix: str,
        reason: str,
        prior_trash_id: str | None,
    ) -> None:
        self._emit(
            project_root,
            event_kind="file_delete_noop",
            session_id=session_id,
            capability_name=tool_name,
            action_kind="delete",
            target_entity=rel_posix,
            status="noop",
            payload={
                "ok": True,
                "already_deleted": True,
                "rel_posix": rel_posix,
                "prior_trash_id": prior_trash_id,
                "reason": reason[:500],
                "actor": actor,
            },
        )

    def _audit_refusal(
        self,
        project_root: Path,
        *,
        session_id: str,
        tool_name: str,
        actor: str,
        original_input: str,
        reason: str,
        error: str,
        error_message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "ok": False,
            "original_input": original_input[:300],
            "reason": reason[:500],
            "error": error,
            "error_message": error_message[:300],
            "actor": actor,
        }
        if extra:
            payload.update(extra)
        self._emit(
            project_root,
            event_kind="file_delete_refused",
            session_id=session_id,
            capability_name=tool_name,
            action_kind="delete",
            target_entity=original_input[:300],
            status="blocked",
            payload=payload,
        )

    def _emit(
        self,
        project_root: Path,
        *,
        event_kind: str,
        session_id: str,
        capability_name: str,
        action_kind: str,
        target_entity: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        """Best-effort event emission. Hub may be None or lack the
        execution store in some tests — fail-open in that case so
        the delete itself isn't blocked by an audit failure.
        """
        try:
            self.hub.execution.record_event(
                project_root,
                event_kind=event_kind,
                source_kind="file_delete_service",
                session_id=session_id,
                capability_name=capability_name,
                action_kind=action_kind,
                target_entity=target_entity,
                status=status,
                payload=payload,
            )
        except Exception:
            pass
