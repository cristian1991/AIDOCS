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

    #630: effective_role=='super_admin' NO LONGER implies is_org_admin — the acl stopped
    OR'ing the two facts and now COMPARES them, so a principal whose bound org row says
    MEMBER/VIEWER is refused even with a super_admin platform role. The is_org_admin call
    is therefore load-bearing, not merely defense-in-depth: it is what makes that veto
    reach ai_deploy, the highest-authority tool on the gate. It never relaxes the check.
    NEVER consults identity_resolver
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


def is_commit_sha(value: str | None) -> bool:
    """True iff `value` is a full 40-hex git commit object name (fail-closed on anything else — a
    short sha, branch name, tag, or empty string is NOT a pinned commit)."""
    return re.fullmatch(r"[0-9a-fA-F]{40}", str(value or "").strip()) is not None


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


def validate_deploy_tree(
    request: dict[str, Any] | None,
    *,
    bound_origin: str | None,
    allowed_refs: "frozenset[str] | set[str]" = DEFAULT_ALLOWED_REFS,
) -> dict[str, Any]:
    """RUNNER-side defense-in-depth re-validation (Blockers C/D). The gate already authorized the
    trigger via authorize_deploy (which also enforces super_admin), but the privileged runner never
    trusts the queued request blindly: it independently re-checks, fail-closed, the two things it can
    verify against the tree it is about to deploy — (1) the ref is allowlisted, (2) the tree's git
    origin is AIDOCS_PRIVATE, (3) the request pins an immutable 40-hex commit_sha (§5 — deploy the
    commit the operator approved, never a mutable ref that may have moved since). A malformed /
    foreign / wrong-repo / unpinned request never reaches a deploy. Pure (no I/O). No super_admin
    check here: the runner holds no principal — that gate ran at the trigger."""
    if not isinstance(request, dict):
        return {"ok": False, "reason": "request is not a JSON object"}
    ref = str(request.get("ref") or "").strip()
    if ref not in allowed_refs:
        return {"ok": False, "reason": f"runner refuses ref {ref!r}: not in the allowlist {sorted(allowed_refs)}"}
    if not origin_is_aidocs_private(bound_origin):
        return {"ok": False, "reason": f"runner refuses: bound tree origin {bound_origin!r} is not AIDOCS_PRIVATE"}
    commit_sha = str(request.get("commit_sha") or "").strip()
    if not is_commit_sha(commit_sha):
        return {
            "ok": False,
            "reason": (
                f"runner refuses: request has no pinned 40-hex commit_sha ({commit_sha!r}) — a deploy "
                "must pin the immutable commit the operator approved, never a mutable ref alone"
            ),
        }
    return {"ok": True, "ref": ref, "commit_sha": commit_sha}
