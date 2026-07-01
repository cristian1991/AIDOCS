"""Authorization for ai_deploy — the HIGHEST-authority tool (remote crown-deploy trigger).

Pure, fail-closed decision: may THIS principal trigger a deploy of THIS ref against the
currently-bound project? THREE gates, ALL required (any fail → refused, nothing enqueued):

  1. SUPER_ADMIN — STRICTER than ai_backlog's is_org_admin: the gate-resolved principal's
     effective_role must be exactly "super_admin" (an org OWNER/ADMIN who is NOT super_admin
     is refused). Uses request.principal ONLY — NEVER identity_resolver's super_admin fallback,
     which is blind to the OAuth principal on the remote gate and would let lesser accounts in.
  2. AIDOCS_PRIVATE BINDING — the bound project's git origin must be the AIDOCS source repo.
     Never deploy a drifted/default binding (e.g. AutoDeployBase) — that would deploy the wrong
     repo. (Binding drift is fixed, but this is defense-in-depth at the trigger.)
  3. REF ALLOWLIST — only an explicitly-allowed ref (default: 'main') may be deployed.

This module is pure (no I/O, no enqueue, no VPS) so the security core is unit-testable in
isolation and proven adversarially before any wiring. The enqueue + the OuterGate.execute
wiring + the daemon are separate layers built on top.
"""

from __future__ import annotations

import re
from typing import Any

# The bound project's origin must be EXACTLY the AIDOCS source repo, parsed canonically
# (host + owner/repo), never a substring. A substring match is forgeable: a lookalike origin
# (https://evil.com/cristian1991/AIDOCS_PRIVATE, https://github.com/attacker/AIDOCS_PRIVATE,
# https://github.com/cristian1991/AIDOCS_PRIVATE_FAKE, https://github.com.evil.com/...) all
# CONTAIN the fragment yet are NOT the source repo. We parse the git remote into (host, path)
# and require host == github.com AND path == cristian1991/aidocs_private exactly.
AIDOCS_PRIVATE_HOST = "github.com"
AIDOCS_PRIVATE_PATH = "cristian1991/aidocs_private"  # owner/repo, lowercased, no .git suffix
# Retained for back-compat references; the AUTHORITATIVE check is canonical (below).
AIDOCS_PRIVATE_ORIGIN_FRAGMENT = "cristian1991/AIDOCS_PRIVATE"

# Only these refs may be deployed. Kept tiny on purpose (a remote deploy trigger must not be
# able to ship an arbitrary branch). Override per-call if a signed-tag scheme is added later.
DEFAULT_ALLOWED_REFS = frozenset({"main"})

# Refusal codes (stable; surfaced in the CanonicalVerdict + audited).
REFUSAL_SUPER_ADMIN = "super_admin_required"
REFUSAL_BINDING = "wrong_project_binding"
REFUSAL_REF = "ref_not_allowed"


def is_super_admin(principal: dict[str, Any] | None) -> bool:
    """STRICT super-admin check for ai_deploy: effective_role == 'super_admin' AND
    is_org_admin(principal), read from the GATE-resolved principal only.

    effective_role=='super_admin' already implies is_org_admin (the acl OR's it in), so the
    is_org_admin call is defense-in-depth — it keeps the gate honest if the acl logic ever
    changes, and it never relaxes the check. NEVER consults identity_resolver
    (current_effective_role), whose solo-flavor super_admin fallback is blind to the OAuth
    principal on the remote gate.
    """
    if not isinstance(principal, dict):
        return False
    if str(principal.get("effective_role") or "").strip().lower() != "super_admin":
        return False
    from .outer_gate_project_acl import is_org_admin

    return bool(is_org_admin(principal))


def parse_git_origin(origin: str | None) -> "tuple[str | None, str | None]":
    """Parse a git remote URL into (host, owner_repo_path), both lowercased, normalized
    (PAT/user stripped, port stripped, trailing .git + slashes removed). Returns (None, None)
    for anything unparseable. Pure + fail-closed. Handles the standard remote forms:
        https://github.com/cristian1991/AIDOCS_PRIVATE(.git)
        https://<user>:<token>@github.com/cristian1991/AIDOCS_PRIVATE(.git)   (PAT-embedded)
        ssh://git@github.com[:22]/cristian1991/AIDOCS_PRIVATE(.git)
        git@github.com:cristian1991/AIDOCS_PRIVATE(.git)                       (scp-like)
    """
    s = str(origin or "").strip()
    if not s:
        return None, None
    netloc: str | None = None
    path: str | None = None
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/]+)/(.+)$", s)  # scheme://netloc/path
    if m:
        netloc, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^([^/]+):(.+)$", s)  # scp-like: [user@]host:owner/repo
        if m and "/" in m.group(2):
            netloc, path = m.group(1), m.group(2)
    if netloc is None or path is None:
        return None, None
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0].strip().lower()  # drop user[:token]@ + :port
    p = path.strip().strip("/")
    if p.lower().endswith(".git"):
        p = p[:-4]
    p = p.strip("/").lower()
    if not host or not p:
        return None, None
    return host, p


def origin_is_aidocs_private(origin: str | None) -> bool:
    """True iff the git origin is EXACTLY the AIDOCS source repo, by CANONICAL parse
    (host == github.com AND path == cristian1991/aidocs_private). A lookalike that merely
    CONTAINS the fragment (wrong host, wrong owner, suffix like _FAKE, host like
    github.com.evil.com) is REFUSED. Empty/unparseable → False (fail closed)."""
    host, path = parse_git_origin(origin)
    return host == AIDOCS_PRIVATE_HOST and path == AIDOCS_PRIVATE_PATH


def authorize_deploy(
    principal: dict[str, Any] | None,
    *,
    bound_origin: str | None,
    ref: str | None,
    allowed_refs: "frozenset[str] | set[str]" = DEFAULT_ALLOWED_REFS,
) -> dict[str, Any]:
    """Fail-closed authorization for an ai_deploy trigger. Returns a dict with ``ok`` and, on
    refusal, a stable ``refusal`` code + a truthful ``reason``. ``ok=True`` ONLY when all three
    gates pass; on the first failure it returns immediately (nothing downstream should enqueue)."""
    if not is_super_admin(principal):
        return {
            "ok": False,
            "refusal": REFUSAL_SUPER_ADMIN,
            "reason": (
                "ai_deploy requires a super_admin principal (the strictest authority); an org "
                "OWNER/ADMIN that is not super_admin is refused"
            ),
        }
    if not origin_is_aidocs_private(bound_origin):
        return {
            "ok": False,
            "refusal": REFUSAL_BINDING,
            "reason": (
                f"ai_deploy refuses: bound project origin {bound_origin!r} is not AIDOCS_PRIVATE "
                "— rebind to AIDOCS_PRIVATE before deploying (never deploy a drifted/default binding)"
            ),
        }
    r = str(ref or "").strip()
    if r not in allowed_refs:
        return {
            "ok": False,
            "refusal": REFUSAL_REF,
            "reason": f"ai_deploy ref {r!r} is not in the allowlist {sorted(allowed_refs)}",
        }
    return {"ok": True, "refusal": "", "reason": "authorized", "ref": r}
