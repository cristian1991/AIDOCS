"""Read-access policy for hard-protected data files.

READS are permitted by default (king 2026-06-13) — the dangerous direction is
EDIT, which the gate fences elsewhere. This module adds the OPTIONAL tightening
the king asked for: an operator-configured read DENY-LIST plus a per-role /
per-user read ACL that re-opens specific denied files to named principals.

Resolution order for a read:
  1. The hard-protected AUTHORITY (admin holding security.hard_protected)
     always reads — config can't lock the admin out of their own data.
  2. If the path matches NO deny glob → allowed (the default).
  3. If it matches a deny glob → allowed ONLY if an ACL entry whose pattern
     also matches grants this principal's role or user; else refused.

Deterministic + pure (no I/O, no identity resolution) so it is trivially
testable; the read pipeline resolves role/user/authority and the config lists,
then calls this. Globs use fnmatch (``*`` crosses ``/``), matching
``hard_protected_paths`` semantics.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(slots=True)
class ReadAccessDecision:
    allowed: bool
    reason: str = ""
    blocked_by: str = ""


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lower()


def _glob_match(normalized: str, pattern: str) -> bool:
    cand = (pattern or "").replace("\\", "/").lower()
    if not cand:
        return False
    if fnmatch.fnmatch(normalized, cand):
        return True
    # A leading `**/` should also match at depth 0 (fnmatch's `**/` otherwise
    # requires at least one directory segment). Match the remainder against the
    # full path and the basename.
    if cand.startswith("**/"):
        remainder = cand[3:]
        if fnmatch.fnmatch(normalized, remainder):
            return True
        if fnmatch.fnmatch(normalized.rsplit("/", 1)[-1], remainder):
            return True
    base = normalized.rsplit("/", 1)[-1]
    return "/" not in cand and fnmatch.fnmatch(base, cand)


def _acl_grants(
    normalized: str,
    *,
    role: str,
    user: str,
    acl: Iterable[Mapping[str, object]],
) -> bool:
    """True iff some ACL entry whose ``pattern`` matches the path lists this
    principal's role or user.
    """
    role_l = (role or "").lower()
    user_l = (user or "").lower()
    for entry in acl or ():
        if not isinstance(entry, Mapping):
            continue
        pattern = str(entry.get("pattern", ""))
        if not _glob_match(normalized, pattern):
            continue
        roles = entry.get("roles") or ()
        users = entry.get("users") or ()
        if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes)):
            if any(str(r).lower() == role_l for r in roles):
                return True
        if isinstance(users, Sequence) and not isinstance(users, (str, bytes)):
            if user_l and any(str(u).lower() == user_l for u in users):
                return True
    return False


def evaluate_read(
    path: str,
    *,
    role: str,
    user: str,
    is_authority: bool,
    deny_globs: Iterable[str] = (),
    acl: Iterable[Mapping[str, object]] = (),
) -> ReadAccessDecision:
    """Decide whether ``role``/``user`` may READ ``path``.

    ``is_authority`` is True when the principal holds the admin
    security.hard_protected permission (resolved upstream). See module docstring
    for the order.
    """
    normalized = _norm(path)
    if is_authority:
        return ReadAccessDecision(allowed=True)
    if not normalized:
        return ReadAccessDecision(allowed=True)

    denied = any(_glob_match(normalized, g) for g in (deny_globs or ()))
    if not denied:
        return ReadAccessDecision(allowed=True)

    if _acl_grants(normalized, role=role, user=user, acl=acl):
        return ReadAccessDecision(allowed=True)

    return ReadAccessDecision(
        allowed=False,
        blocked_by="read_deny_list",
        reason=(
            f"'{path}' is on the hard-protected read deny-list and your role "
            f"('{role}') / user is not on its read ACL. An admin can grant read "
            f"via security.read_acl or read it for you."
        ),
    )
