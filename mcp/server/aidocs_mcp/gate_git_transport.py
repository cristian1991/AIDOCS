"""The ONE credentialed git primitive for the gate surface.

Shared by ``outer_gate_projects._git`` (import/sync) and ``ai_git`` on the gate
surface (fetch/pull/push for a gate principal bound to a tenant).

CUSTODY (M2, unchanged from outer_gate_projects._git): the org GitHub credential
is resolved JUST-IN-TIME and handed to ONE git child through a per-invocation
credential helper carried in the child's ENVIRONMENT (``GIT_CONFIG_*``). It
never lands in argv (not ``ps``-visible), never in .git/config, never in a URL,
and is never handed to the general ``ai_run`` shell path — this module spawns
``git`` with a fixed list argv and no shell.

``run_gate_git`` additionally returns a result whose stderr/stdout are SCRUBBED
(the token value and any URL userinfo section) and whose failure carries a
category: auth | network | not_found | policy | other.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CATEGORY_AUTH = "auth"
CATEGORY_NETWORK = "network"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_POLICY = "policy"
CATEGORY_OTHER = "other"

_WIN_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_HELPER = (
    "!f() { echo username=x-access-token; "
    'echo "password=$AIDOCS_GIT_CREDENTIAL"; }; f'
)

# argv tokens that could re-point the credential helper or inject config for
# this one child. The primitive is fixed-argv: callers compose git verbs, never
# config overrides.
_FORBIDDEN_ARG_PREFIXES = ("--config-env", "--exec-path", "--upload-pack", "--receive-pack")
_FORBIDDEN_ARGS = frozenset({"-c"})

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

# scheme + "//" + userinfo + "@"  → scheme + "//***@"
_USERINFO_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*:/{2})[^/\s@]+@")


def scrub_git_text(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Remove credential values and URL userinfo from git output."""
    out = str(text or "")
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return _USERINFO_URL.sub(r"\1***@", out)


def classify_git_failure(text: str) -> str:
    """auth | network | not_found | policy | other — from git's own output."""
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


def resolve_credential(token: str | None, tenant_id: str | None) -> str:
    cred = (token or "").strip()
    if not cred and tenant_id:
        try:
            from .outer_gate_github_credential import resolve_org_github_token

            cred = (resolve_org_github_token(tenant_id) or "").strip()
        except Exception:  # noqa: BLE001 — no credential ⇒ plain/public git
            cred = ""
    return cred


def credential_env(cred: str, base: dict | None = None) -> dict:
    """The child environment: prompts disabled + (if cred) the env helper."""
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt for credentials
    env["GCM_INTERACTIVE"] = "never"
    if cred:
        env["AIDOCS_GIT_CREDENTIAL"] = cred
        # Append (never clobber) any inherited GIT_CONFIG_* entries.
        try:
            _n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        except ValueError:
            _n = 0
        env[f"GIT_CONFIG_KEY_{_n}"] = "credential.helper"
        env[f"GIT_CONFIG_VALUE_{_n}"] = _HELPER
        env["GIT_CONFIG_COUNT"] = str(_n + 1)
    return env


def argv_policy_refusal(args) -> str:
    """'' when ``args`` is an acceptable fixed argv, else the refusal reason."""
    if not isinstance(args, (list, tuple)) or not all(isinstance(a, str) for a in args):
        return "gate git argv must be a list of strings (no shell strings)"
    for a in args:
        if a in _FORBIDDEN_ARGS or a.startswith(_FORBIDDEN_ARG_PREFIXES):
            return f"gate git argv refuses config/transport override token {a!r}"
    return ""


def run_credentialed_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    token: str | None = None,
    tenant_id: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` with the org credential in the child env only.

    Raw ``CompletedProcess`` (the ``outer_gate_projects._git`` contract).
    """
    env = credential_env(resolve_credential(token, tenant_id))
    # #345: routed through audited_run (ledger row per spawn).
    from .shell_egress_service import audited_run

    return audited_run(
        ["git", *args],
        fingerprint=("gate_git_transport.py", "run_credentialed_git", "subprocess.run"),
        reason="gate-project-git",
        run=lambda *a, **kw: subprocess.run(*a, **kw),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        creationflags=_WIN_NO_WINDOW,
    )


@dataclass(frozen=True)
class GateGitResult:
    returncode: int
    stdout: str
    stderr: str  # scrubbed
    category: str  # "" on success
    credentialed: bool
    argv: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def reason(self) -> str:
        """Scrubbed, bounded failure reason ('' on success)."""
        if self.ok:
            return ""
        return (self.stderr.strip() or self.stdout.strip())[-400:]


def run_gate_git(
    args: list[str],
    *,
    cwd: Path | str | None,
    tenant_id: str | None,
    timeout: int = 60,
) -> GateGitResult:
    """Credentialed, scrubbed, categorized git for a gate principal's tenant."""
    refusal = argv_policy_refusal(args)
    if refusal:
        return GateGitResult(-1, "", refusal, CATEGORY_POLICY, False)
    cred = resolve_credential(None, tenant_id)
    try:
        cp = run_credentialed_git(
            list(args), cwd=Path(cwd) if cwd else None, token=cred or None, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return GateGitResult(
            -1, "", f"git {args[0] if args else ''} timed out after {timeout}s",
            CATEGORY_NETWORK, bool(cred), tuple(args),
        )
    except OSError as exc:
        return GateGitResult(
            -1, "", scrub_git_text(f"git could not start: {exc}", (cred,)),
            CATEGORY_OTHER, bool(cred), tuple(args),
        )
    out = scrub_git_text(cp.stdout or "", (cred,))
    err = scrub_git_text(cp.stderr or "", (cred,))
    rc = int(cp.returncode if cp.returncode is not None else -1)
    cat = "" if rc == 0 else classify_git_failure(f"{err}\n{out}")
    return GateGitResult(rc, out, err, cat, bool(cred), tuple(args))


# ── #762 push shape: explicit remote + src:dst refspec ─────────────────────
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
