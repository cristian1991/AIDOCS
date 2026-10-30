"""Local ai_git policy helpers -- a neutral public module.

Design decision (2026-10-29): no public module imports a gate file. Local
``ai_git`` (mcp_server) needs a handful of pure helpers that historically
lived in the gate's git modules: output scrubbing and failure categories, the
#762 explicit push shape, and the audit-ledger door. They move here unchanged;
``gate_git_transport`` and ``gate_git_ops`` re-export them, so the gate and the
local surface keep ONE implementation. None of them holds a credential or
decides an authority question.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

GitLocal = Callable[[list[str]], "tuple[int, str]"]

CATEGORY_AUTH = "auth"
CATEGORY_NETWORK = "network"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_POLICY = "policy"
CATEGORY_OTHER = "other"

_AUTH_MARKERS = (
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "authentication failed",
    "invalid username or password",
    "invalid username or token",
    "bad credentials",
    "could not read credential",
    "permission to ",  # "Permission to o/r.git denied to user"
    "error: 403",
)
_NETWORK_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "operation timed out",
    "ssl",
    "early eof",
    "the remote end hung up",
    "timed out",
)
_NOT_FOUND_MARKERS = (
    "not found",
    "does not appear to be a git repository",
    "couldn't find remote ref",
    "no such remote",
)
_POLICY_MARKERS = (
    "protected branch",
    "[rejected]",
    "non-fast-forward",
    "pre-receive hook declined",
    "gh006",
    "gh013",
    "push declined",
)

# scheme + "//" + userinfo + "@"  -> scheme + "//***@"
_USERINFO_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*:/{2})[^/\s@]+@")


def scrub_git_text(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Remove credential values and URL userinfo from git output."""
    out = str(text or "")
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return _USERINFO_URL.sub(r"\1***@", out)


def classify_git_failure(text: str) -> str:
    """auth | network | not_found | policy | other -- from git's own output."""
    low = str(text or "").lower()
    if any(m in low for m in _AUTH_MARKERS):
        return CATEGORY_AUTH
    if any(m in low for m in _NETWORK_MARKERS):
        return CATEGORY_NETWORK
    if any(m in low for m in _NOT_FOUND_MARKERS):
        return CATEGORY_NOT_FOUND
    if any(m in low for m in _POLICY_MARKERS):
        return CATEGORY_POLICY
    return CATEGORY_OTHER


# -- #762 push shape: explicit remote + src:dst refspec ----------------------
_REF_OK = re.compile(r"^[A-Za-z0-9._/\-]+$")


def refspec_refusal(remote: str, refspec: str) -> str:
    """'' when (remote, refspec) is an explicit, non-destructive push shape."""
    r = str(remote or "").strip()
    s = str(refspec or "").strip()
    if not r or r.startswith("-") or not _REF_OK.match(r):
        return f"push requires an explicit remote name; got {remote!r}"
    if not s:
        return "push requires an explicit source:destination refspec"
    if s.startswith("+"):
        return f"force refspec {s!r} refused (a leading '+' force-updates the remote)"
    if s.startswith(":"):
        return f"delete refspec {s!r} refused (an empty source deletes the remote ref)"
    if s.startswith("-"):
        return f"refspec {s!r} refused (flags such as --force/--mirror/--delete are not accepted)"
    if s.count(":") != 1:
        return f"refspec {s!r} must be exactly source:destination"
    src, dst = s.split(":", 1)
    if not src or not dst:
        return f"refspec {s!r} must name both source and destination"
    for part in (src, dst):
        if (
            part.startswith("-")
            or not _REF_OK.match(part)
            or ".." in part
            or part.endswith(".lock")
        ):
            return f"refspec part {part!r} is not a plain ref name"
    return ""


def derive_push_plan(remote: str, refspec: str, git_local: GitLocal) -> dict[str, Any]:
    """#762: an explicit remote + source:destination refspec, or a refusal.

    Both empty => derived from the CURRENT branch and its tracked upstream.
    One given without the other => ambiguous, refused. Detached HEAD / no
    upstream => refused. Force (+), delete (:dst) and flag shapes => refused.
    Returns {"ok": True, remote, refspec, source, destination, derived} or
    {"ok": False, "error": ...}.
    """
    r = str(remote or "").strip()
    s = str(refspec or "").strip()
    derived = False
    if bool(r) != bool(s):
        return {
            "ok": False,
            "error": (
                "git push refused: pass BOTH `remote` and `refspec` "
                "(source:destination), or neither to derive them from the current "
                "branch's tracked upstream (#762)."
            ),
        }
    if not r:
        derived = True
        rc, br = git_local(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = (br or "").strip()
        if rc != 0 or not branch or branch == "HEAD":
            return {
                "ok": False,
                "error": "git push refused: detached HEAD — no current branch to push (#762).",
            }
        rc, up = git_local(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{u}}"]
        )
        upstream = (up or "").strip()
        if rc != 0 or "/" not in upstream:
            return {
                "ok": False,
                "error": (
                    f"git push refused: branch {branch!r} has no tracked upstream; "
                    "pass remote + refspec explicitly (#762)."
                ),
            }
        r, dst = upstream.split("/", 1)
        s = f"{branch}:{dst}"
    refusal = refspec_refusal(r, s)
    if refusal:
        return {"ok": False, "error": f"git push refused: {refusal} (#762)"}
    src, dst = s.split(":", 1)
    return {
        "ok": True,
        "remote": r,
        "refspec": s,
        "source": src,
        "destination": dst,
        "derived": derived,
    }


def ledger(hub: Any, project_root: Path, *, critical: bool, **fields: Any) -> bool:
    """One audit row through the shared audit door. A critical row with no hub
    is a refusal (False); a best-effort row with no hub is not."""
    from .enforcement_pkg.audit_critical import (
        record_audit_best_effort,
        record_audit_critical,
    )

    fn = record_audit_critical if critical else record_audit_best_effort
    if hub is None:
        return not critical
    return fn(hub, project_root, **fields)
