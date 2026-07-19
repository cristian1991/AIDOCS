"""Tenant-project RBAC bootstrap + heal-forward bridge (#439).

ROOT CAUSE (backlog #439): tenant projects on the VPS gate are commissioned by
``outer_gate_projects.bootstrap_and_index`` (marker + code index + commission
stamp) which NEVER seeds the per-project RBAC store — ``seed_rbac`` only runs
on the local paths (session-create owner-grant, dashboard CLI,
``bootstrap_local_superadmin`` — itself flavor-gated to dev/solo and granting
``operator@local``, never the tenant admin). The project's
``.MEMORY/.index/aidocs_identity.sqlite3`` therefore holds ZERO ``rbac_roles``
/ ``rbac_user_roles`` rows, so ``outer_gate_freeze_recovery.clear_freeze``
layer 2 (``rbac.admin_clear_freeze`` on the PROJECT's store) refuses every
credential in existence — a tenant-store freeze was uncleareable via any
governed path (repro: esc_8adce91b276d72e5, 2026-07-17).

THE FIX (two prongs, both fail-closed):

* **Bootstrap-on-create** — ``bootstrap_project_creator_rbac``: at project
  creation the CREATING user's server-side AUTHENTICATED identity (#443 chain:
  ``principal["user_id"]`` resolved by the transport from the verified token —
  NEVER an agent-supplied id) is seeded as the project's org-admin
  (``super_admin`` role at global scope in the project's own store). The
  existing freeze-clear ladder then works for the tenant's own admin with no
  new clearing surface. Idempotent: re-running never duplicates a grant and
  never demotes anyone (grant-only, INSERT-or-skip).

* **Heal-on-touch** — ``heal_project_rbac_on_touch``: existing tenant projects
  (created before this fix) heal on their first GOVERNED touch by an
  authenticated org OWNER/ADMIN of the owning org. One-shot, stamped
  (``rbac_bootstrap_meta`` in the project identity DB — mirrors the
  commission-stamp / freeze_strike_notice adoption-bridge precedent): once
  stamped, later touches change nothing (a later different admin is NOT
  auto-granted; grants past the bootstrap go through the normal RBAC surfaces).

FAIL-CLOSED posture: when the creator identity cannot be resolved, the project
still creates but RBAC stays EMPTY — exactly the pre-fix state, freezes stay
operator-escalation — and a loud posture note is returned + audited. The
bootstrap NEVER grants a default/wildcard admin, only the caller's
authenticated identity. No stamp is written on the skip, so the heal bridge
remains available for a real admin later.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = [
    "bootstrap_project_creator_rbac",
    "heal_project_rbac_on_touch",
    "is_rbac_bootstrapped",
    "UNBOOTSTRAPPED_POSTURE",
]

_STAMP_TABLE = "rbac_bootstrap_meta"
_STAMP_KEY = "tenant_rbac_bootstrap"

# The loud posture note (constraint: fail-closed skip keeps current behavior
# but SAYS SO — the operator sees why a freeze on this project would need
# root-console escalation).
UNBOOTSTRAPPED_POSTURE = (
    "rbac_unbootstrapped: no creator identity could be resolved, so this "
    "project's RBAC store was left EMPTY (no admin can clear freezes here via "
    "any governed path — freezes require operator/root-console escalation "
    "until an org OWNER/ADMIN touches the project and the heal-forward "
    "bootstrap runs)"
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _identity_db_path(project_root: Path) -> Path:
    from .rbac_store import RBACStore

    return RBACStore().db_path(Path(project_root))


def is_rbac_bootstrapped(project_root: Path) -> bool:
    """True iff the one-shot bootstrap stamp is present in the project's
    identity DB. Missing DB / missing table / any error ⇒ False (the bridge
    stays available — never fails toward 'already done')."""
    try:
        db = _identity_db_path(project_root)
        if not db.exists():
            return False
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {_STAMP_TABLE} WHERE key = ?",
                (_STAMP_KEY,),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _store_has_any_role_grants(project_root: Path) -> bool:
    """True iff ANY user holds ANY role in this project's RBAC store — the
    signal that the store was deliberately configured. Missing DB / table ⇒
    False (the truly-empty pre-fix state)."""
    try:
        db = _identity_db_path(project_root)
        if not db.exists():
            return False
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT 1 FROM rbac_user_roles LIMIT 1").fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _write_stamp(project_root: Path, *, user_id: str, source: str) -> None:
    db = _identity_db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_STAMP_TABLE} "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        conn.execute(
            f"INSERT OR IGNORE INTO {_STAMP_TABLE} (key, value) VALUES (?, ?)",
            (
                _STAMP_KEY,
                json.dumps(
                    {"at": _now(), "user_id": user_id, "source": source},
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()


def _audit(
    project_root: Path,
    *,
    event_kind: str,
    status: str,
    user_id: str,
    payload: dict,
) -> None:
    """Best-effort audit row — posture visibility, never a failure mode."""
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            Path(project_root),
            event_kind=event_kind,
            source_kind="tenant_rbac_bootstrap",
            session_id=None,
            capability_name="rbac_bootstrap",
            action_kind="bootstrap",
            target_entity=str(payload.get("role") or ""),
            status=status,
            payload=payload,
            user_id=user_id or None,
        )
    except Exception:  # noqa: BLE001 — audit is best-effort here
        pass


def bootstrap_project_creator_rbac(
    project_root: Path,
    *,
    creator_user_id: str,
    creator_label: str = "",
    source: str = "project_create",
) -> dict[str, Any]:
    """Deterministically bootstrap a project's RBAC store with the CREATOR's
    authenticated identity as its org-admin (``super_admin`` at global scope).

    * ``creator_user_id`` MUST be the server-side authenticated principal's
      user id (#443 chain) — callers must never pass an agent-supplied value.
    * Idempotent: seed roles converge to the catalog; the grant is
      check-before-insert (no duplicate rows) and nothing is ever revoked or
      demoted. A re-touch after the stamp is a no-op.
    * FAIL-CLOSED: an empty/unresolvable creator grants NOTHING and writes NO
      stamp (RBAC stays byte-identical to the pre-fix empty state, freeze
      clearing stays operator-escalation) — with a loud posture note, returned
      AND audited.
    """
    root = Path(project_root)
    uid = str(creator_user_id or "").strip()
    if not uid:
        _audit(
            root,
            event_kind="tenant_rbac_bootstrap_skipped",
            status="degraded",
            user_id="",
            payload={"posture": UNBOOTSTRAPPED_POSTURE, "source": source},
        )
        return {
            "bootstrapped": False,
            "granted": False,
            "posture": UNBOOTSTRAPPED_POSTURE,
            "source": source,
        }

    if is_rbac_bootstrapped(root):
        # One-shot: the stamp is authoritative. Never re-grant (a later caller
        # must go through the normal RBAC surfaces), never demote.
        return {
            "bootstrapped": True,
            "granted": False,
            "already": True,
            "source": source,
        }

    from .permission_catalog import seed_rbac
    from .rbac_store import RBACStore

    seed_rbac(root)
    store = RBACStore()
    granted = store.assign_role_to_user_by_name(
        root,
        uid,
        "super_admin",
        authored_by_user_id="__bootstrap__",
    )
    _write_stamp(root, user_id=uid, source=source)
    _audit(
        root,
        event_kind="tenant_rbac_bootstrapped",
        status="applied",
        user_id=uid,
        payload={
            "role": "super_admin",
            "granted": bool(granted),
            "creator_label": creator_label,
            "source": source,
        },
    )
    return {
        "bootstrapped": True,
        "granted": bool(granted),
        "role": "super_admin",
        "user_id": uid,
        "source": source,
    }


def heal_project_rbac_on_touch(
    project_root: Path,
    *,
    principal: dict | None,
) -> dict[str, Any] | None:
    """Heal-forward bridge for EXISTING tenant projects (#439): on the first
    governed touch by an authenticated org OWNER/ADMIN, bootstrap the empty
    RBAC store with THAT admin's authenticated identity. One-shot (stamped);
    non-admin or identity-less touches heal nothing (fail-closed — the bridge
    waits for a real org admin, it never grants a default identity).

    Fail-quiet by contract: any error returns None so a heal problem can never
    break the governed operation it rides on. Returns the bootstrap receipt
    when a heal actually ran, else None.
    """
    try:
        root = Path(project_root)
        if is_rbac_bootstrapped(root):
            return None
        from .outer_gate_project_acl import is_org_admin

        if not is_org_admin(principal):
            return None
        uid = (
            str((principal or {}).get("user_id") or "").strip()
            if isinstance(principal, dict)
            else ""
        )
        if not uid:
            return None
        # NO ESCALATION over a CONFIGURED store: if anyone already holds any
        # role here, the store was deliberately set up (e.g. an org OWNER was
        # intentionally given only 'admin'-tier) — healing must never promote
        # past that. Adopt the store as established (stamp, one-shot) and
        # grant NOTHING; refusals on it keep standing exactly as before.
        if _store_has_any_role_grants(root):
            _write_stamp(root, user_id=uid, source="heal_adopt_existing")
            _audit(
                root,
                event_kind="tenant_rbac_adopted_existing",
                status="applied",
                user_id=uid,
                payload={"note": "configured store adopted; no grant minted"},
            )
            return None
        return bootstrap_project_creator_rbac(
            root,
            creator_user_id=uid,
            source="heal_on_touch",
        )
    except Exception:  # noqa: BLE001 — heal must never break the ride-along op
        return None
