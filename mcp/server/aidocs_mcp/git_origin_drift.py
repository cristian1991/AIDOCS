"""Origin-drift detection for git-bound projects (backlog #190, CRITICAL).

The trust bug: ahead/behind computed from the LOCAL remote-tracking ref
(``origin/main``) WITHOUT fetching first reports a clone that is days behind
real GitHub as ``behind: 0`` — it is only "behind the last fetch", not behind
the live remote. The project-status ``ready``/``stale`` flags compound the lie:
they reflect INDEX freshness only, so a behind-origin clone reads as
ready/non-stale and the operator trusts a stale tree as source-of-truth.

This module is the single, pure, fully-unit-testable source of truth for the
fix. It is import-light (no gate/DB deps) so both the git_ops tool and the
project-status surfaces can wire to it.

Two axes, named separately:
  * INDEX freshness — ``ready`` / ``stale`` (computed elsewhere; index re-sync).
  * GIT currency    — ``git_sync`` / ``behind_origin`` / ``origin_check`` (here).

``compute_origin_drift`` does a FETCH-ONLY refresh of the remote-tracking ref
BEFORE counting (it NEVER merges/resets/checks-out — it must not touch the
working tree), is fail-safe when the remote is unreachable (``origin_check:
unreachable`` rather than a false ``behind: 0`` or a hard error), and is
``n/a`` when there is no upstream (source=local projects are unaffected).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

# A git runner: takes a git argv (WITHOUT the leading "git") and returns
# ``(returncode, stdout)``. It MAY raise (e.g. a process/timeout failure);
# callers of compute_origin_drift do not need to guard — drift handling treats
# a raise as an unreachable remote, never a crash.
GitRunner = Callable[[list[str]], "tuple[int, str]"]

ORIGIN_CHECK_OK = "ok"
ORIGIN_CHECK_UNREACHABLE = "unreachable"
ORIGIN_CHECK_NA = "n/a"

# git_ops log must label its output so a reader never mistakes the LOCAL HEAD
# for the remote's HEAD (the log reports HEAD of the local clone, which a
# behind-origin clone makes stale).
LOCAL_HEAD_BASIS = (
    "local HEAD of this clone (NOT origin's HEAD) — a behind-origin clone shows "
    "a stale tip; run git_ops(op='status') for the live origin delta"
)


# ── GitHub credential failure: name the remedy, never the credential (#671) ──
#
# Law 311bf3e6 — a refusal must name what to do about it. Raw git text
# ("fatal: could not read Username for 'https://github.com': terminal prompts
# disabled") tells an agent nothing about WHICH credential AIDOCS expected or
# where it is configured, so a failed sync reads as an unexplained outage.
#
# Both helpers below are PURE text: they never receive, hold, resolve or emit a
# credential value. The remedy names the credential's IDENTITY and LOCATION
# only. Interactive prompting stays disabled everywhere — the answer to a
# missing credential is to supply it out-of-band, never to ask the terminal.

# Env var NAME (never a value) holding the CodeNexus-OWNED GitHub credential.
GITHUB_CREDENTIAL_ENV = "CODENEXUS_GIT_TOKEN"  # gitleaks:allow (env NAME)

# Substrings that identify a git failure as CREDENTIAL-shaped. Deliberately
# narrow: "repository not found" / "permission denied" are EXCLUDED because they
# also fire on a typo or an SSH remote, and mislabelling those as a credential
# problem would send an agent to fix the wrong thing.
_GIT_AUTH_FAILURE_MARKERS = (
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "authentication failed",
    "invalid username or password",
    "invalid username or token",
    "bad credentials",
    "could not read credential",
)


def is_git_auth_failure(text: str) -> bool:
    """True when git's own output identifies a CREDENTIAL failure.

    Reads git stderr/stdout only — never a credential value.
    """
    low = str(text or "").lower()
    return any(m in low for m in _GIT_AUTH_FAILURE_MARKERS)


def github_credential_remedy(*, org_id: str = "", action: str = "the git operation") -> str:
    """The refusal text for a credential-shaped git failure.

    Names the credential AIDOCS expected, where it is configured, and what to
    do next. Contains NO credential value and no ``token@host`` URL — only the
    org id (already known to the caller) and the env var NAME.

    An EMPTY ``org_id`` means no org is bound to the project, which is a
    different fault from "org bound but no credential connected" — the message
    says which, so a tenancy-resolution bug cannot keep wearing a credential
    failure's costume.
    """
    if org_id:
        who = (
            f"the CodeNexus-owned GitHub credential for org {org_id!r} (resolved "
            "just-in-time per request, never persisted, and never a caller-supplied "
            "or ChatGPT-connector token)"
        )
        where = (
            f"Connect a GitHub credential for org {org_id!r} in CodeNexus "
            "(org settings -> GitHub), then retry."
        )
    else:
        who = (
            "the CodeNexus-owned GitHub credential of the org that owns this "
            "project — but this project carries NO org, so no per-org credential "
            "could be resolved"
        )
        where = (
            "Bind the project to its org (re-import it, or have an operator stamp "
            f"its org), or on a local/unbound install set the {GITHUB_CREDENTIAL_ENV} "
            "environment variable on the AIDOCS host and restart it."
        )
    return (
        f"{action} could not authenticate to github.com. AIDOCS uses {who}. {where} "
        "Interactive git prompts stay DISABLED by design — AIDOCS supplies a "
        "credential or refuses. No AIDOCS surface ever displays the credential."
    )


def _remote_and_branch(upstream: str) -> tuple[str, str]:
    """Split an upstream ref like ``origin/main`` into (remote, branch).

    Handles branch names containing slashes (``origin/feature/x`` ->
    ``("origin", "feature/x")``). A bare name with no slash is assumed to be a
    branch on ``origin``.
    """
    up = (upstream or "").strip()
    if "/" in up:
        remote, branch = up.split("/", 1)
        return remote, branch
    return ("origin", up)


def _parse_left_right(out: str) -> tuple[int | None, int | None]:
    """Parse ``git rev-list --left-right --count <upstream>...HEAD`` output.

    With ``<upstream>...HEAD`` the LEFT count is commits only on upstream
    (= how far the local HEAD is BEHIND) and the RIGHT count is commits only
    on HEAD (= how far AHEAD). Returns ``(behind, ahead)`` or ``(None, None)``
    if the output is unparseable.
    """
    parts = (out or "").strip().split()
    if len(parts) == 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
        return int(parts[0]), int(parts[1])
    return None, None


def compute_origin_drift(
    run: GitRunner,
    *,
    upstream: str | None,
    attempt_fetch: bool = True,
) -> dict[str, Any]:
    """Best-effort, FETCH-ONLY drift of the local HEAD vs the LIVE origin ref.

    Args:
        run: ``run(argv) -> (returncode, stdout)``; may raise.
        upstream: the tracked upstream ref (e.g. ``origin/main``). Falsy means
            no upstream (source=local) — returns ``n/a`` and runs NO git.
        attempt_fetch: refresh the remote-tracking ref before counting. Always
            True in production; the param exists only to make the fetch-skip
            path testable.

    Returns a dict with:
        origin_check: "ok" (fetch confirmed live) | "unreachable" (fetch
            failed; counts fall back to last-known and are NOT trusted as
            current) | "n/a" (no upstream).
        behind_origin / ahead_origin: int | None.
        git_sync: "current" | "behind" | "ahead" | "diverged" | "unknown" | "n/a".
        fetched: bool — whether the fetch succeeded.
        commands: list[list[str]] — the git argvs issued (fetch-only audit).

    Contract guarantees:
        * FETCH-ONLY: only ``fetch`` and ``rev-list`` are ever issued — never
          merge/reset/checkout/pull. The working tree is never mutated.
        * Fail-safe: a fetch that fails (offline/auth) or raises yields
          ``origin_check: unreachable`` and ``git_sync`` is never "current"
          (a 0/0 against a stale ref is NOT reported as confirmed-current).
    """
    commands: list[list[str]] = []

    if not upstream:
        # source=local / no tracked upstream — git-currency does not apply.
        return {
            "origin_check": ORIGIN_CHECK_NA,
            "behind_origin": None,
            "ahead_origin": None,
            "git_sync": "n/a",
            "fetched": False,
            "commands": commands,
        }

    remote, branch = _remote_and_branch(upstream)

    fetched = False
    origin_check = ORIGIN_CHECK_UNREACHABLE
    if attempt_fetch:
        # FETCH-ONLY: --no-tags keeps it lean; this updates the remote-tracking
        # ref ONLY. It must never merge/reset — there is no such flag here.
        fetch_argv = ["fetch", "--no-tags", remote, branch] if branch else ["fetch", "--no-tags", remote]
        try:
            rc, _out = run(fetch_argv)
            commands.append(fetch_argv)
            if rc == 0:
                fetched = True
                origin_check = ORIGIN_CHECK_OK
        except Exception:
            # Offline / process failure: stay UNREACHABLE (fail-safe), never crash.
            fetched = False
            origin_check = ORIGIN_CHECK_UNREACHABLE

    behind: int | None = None
    ahead: int | None = None
    count_argv = ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"]
    try:
        rc, out = run(count_argv)
        commands.append(count_argv)
        if rc == 0:
            behind, ahead = _parse_left_right(out)
    except Exception:
        behind, ahead = None, None

    git_sync = _classify(behind, ahead, fetched=origin_check == ORIGIN_CHECK_OK)

    return {
        "origin_check": origin_check,
        "behind_origin": behind,
        "ahead_origin": ahead,
        "git_sync": git_sync,
        "fetched": fetched,
        "commands": commands,
    }


def _classify(behind: int | None, ahead: int | None, *, fetched: bool) -> str:
    if behind is None or ahead is None:
        return "unknown"
    if behind > 0 and ahead > 0:
        return "diverged"
    if behind > 0:
        return "behind"
    if ahead > 0:
        return "ahead"
    # behind == 0 and ahead == 0 — only trustworthy as "current" if we actually
    # refreshed the ref. A 0/0 against a STALE local ref must never be sold as
    # confirmed-current (that is the exact #190 lie).
    return "current" if fetched else "unknown"


def local_head_note(behind_origin: int | None, upstream: str = "origin/main") -> str | None:
    """An actionable note for git_ops log when the local HEAD is behind origin.

    Returns None when not behind (or unknown), so callers only attach it when
    it carries signal.
    """
    if not behind_origin or behind_origin <= 0:
        return None
    return (
        f"local HEAD is {behind_origin} behind {upstream} — this log shows the "
        f"clone's tip, not the remote's. Run project refresh / project_sync to resync."
    )


def merge_git_sync(index_status: dict[str, Any], drift: dict[str, Any]) -> dict[str, Any]:
    """Compose the INDEX-freshness axis with the GIT-currency axis as TWO
    separately-named signals.

    ``index_status`` is the output of ``project_status`` (carries ``ready`` /
    ``stale`` — INDEX freshness only). ``drift`` is ``compute_origin_drift``
    output. The merged dict preserves the index axis verbatim and adds the git
    axis (``git_sync`` / ``behind_origin`` / ``ahead_origin`` / ``origin_check``)
    plus two derived currency flags:

        git_current: True only when the remote was reached AND in sync; None
            when git-currency does not apply (source=local).
        current: the HONEST combined currency — index-fresh AND git-current.
            For a local/no-upstream project it mirrors index readiness (there
            is no origin to be behind). Index-fresh-but-behind-origin is NOT
            ``current`` — the #190 fix: index freshness never implies git
            currency.
    """
    out = dict(index_status)
    gs = drift.get("git_sync")
    out["git_sync"] = gs
    out["behind_origin"] = drift.get("behind_origin")
    out["ahead_origin"] = drift.get("ahead_origin")
    out["origin_check"] = drift.get("origin_check")

    index_ready = bool(index_status.get("ready"))
    if gs == "n/a":
        out["git_current"] = None
        out["current"] = index_ready
    else:
        git_current = gs == "current"
        out["git_current"] = git_current
        out["current"] = bool(index_ready and git_current)
    return out


# ── Verified-drift cache: the SINGLE source of truth for git_sync ─────────────
#
# Phoenix bug 2: git_ops(op='status') computed a real fetch-verified drift while
# project_status hardcoded git_sync="unverified" — two answers for the same
# project at the same moment. Fix: the git_ops path RECORDS its verified drift
# here; the hot-path/network-free project surfaces READ it back, but only while
# it is still trustworthy: fresh (TTL), same HEAD as when verified, and the
# origin was actually reached (or genuinely n/a). Anything else is honestly
# "unverified" — meaning unverifiable, not "different code path didn't bother".

DRIFT_CACHE_MAX_AGE_S: float = 900.0
_VERIFIED_ORIGIN_CHECKS = (ORIGIN_CHECK_OK, ORIGIN_CHECK_NA)


def _drift_cache_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "git_drift.json"


def _read_head_sha(project_root: Path) -> str | None:
    """Resolve the current HEAD commit sha from the filesystem (no subprocess —
    this runs on hot, network-free status paths). Returns None when it cannot
    be resolved (no repo, unborn branch, unreadable)."""
    git_dir = Path(project_root) / ".git"
    try:
        head = git_dir.joinpath("HEAD").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if not head:
        return None
    if not head.startswith("ref: "):
        # Detached HEAD — the sha itself.
        return head or None
    ref = head[5:].strip()
    if not ref:
        return None
    try:
        sha = git_dir.joinpath(*ref.split("/")).read_text(
            encoding="utf-8", errors="ignore"
        ).strip()
        if sha:
            return sha
    except OSError:
        pass
    # Loose ref missing — check packed-refs ("<sha> <ref>" lines).
    try:
        for line in git_dir.joinpath("packed-refs").read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        pass
    return None


def record_drift(
    project_root: Path, drift: dict[str, Any], *, now: float | None = None
) -> None:
    """Persist a compute_origin_drift result as the shared git_sync source of
    truth. Best-effort: recording must never break the status read."""
    try:
        payload = {
            "git_sync": drift.get("git_sync"),
            "behind_origin": drift.get("behind_origin"),
            "ahead_origin": drift.get("ahead_origin"),
            "origin_check": drift.get("origin_check"),
            "checked_at": float(now if now is not None else time.time()),
            "head": _read_head_sha(project_root),
        }
        path = _drift_cache_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def last_verified_drift(
    project_root: Path,
    *,
    max_age_s: float = DRIFT_CACHE_MAX_AGE_S,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Read back the recorded drift IF it is still a verified answer:
      * origin_check was "ok" (fetch confirmed live) or "n/a" (no upstream —
        git-currency genuinely does not apply). "unreachable" is never trusted.
      * recorded within ``max_age_s``.
      * the HEAD sha is unchanged since verification (a commit/checkout/reset
        invalidates the verdict). Skipped for "n/a" (no upstream to drift from
        is HEAD-independent only in the no-repo sense; if HEAD is unresolvable
        for a real answer, the cache is not trusted).
    Returns None (⇒ caller reports "unverified") otherwise."""
    try:
        raw = _drift_cache_path(project_root).read_text(encoding="utf-8", errors="ignore")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("origin_check") not in _VERIFIED_ORIGIN_CHECKS:
        return None
    try:
        checked_at = float(data.get("checked_at"))
    except (TypeError, ValueError):
        return None
    current = float(now if now is not None else time.time())
    if current - checked_at > max_age_s or current < checked_at - 60.0:
        return None
    if data.get("origin_check") == ORIGIN_CHECK_OK:
        recorded_head = data.get("head")
        live_head = _read_head_sha(project_root)
        if not recorded_head or not live_head or recorded_head != live_head:
            return None
    return {
        "git_sync": data.get("git_sync"),
        "behind_origin": data.get("behind_origin"),
        "ahead_origin": data.get("ahead_origin"),
        "origin_check": data.get("origin_check"),
        "checked_at": checked_at,
    }
