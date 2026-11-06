"""Public-mirror staleness detection — the 120% seal on the public-rot class.

THE WAR (2026-06-29): the public mirror (cristian1991/AIDOCS) silently fell ~2 weeks behind private
main — GitHub Actions had been disabled and the sync is deliberate-publish, so NOTHING detected the
drift. §0 of the doctrine: a class that can SILENTLY degrade is not 120%-won. Re-publishing fixed the
symptom; THIS module fixes the class — a pure, fail-safe detector of how far the public mirror trails
private main, so the rot can never go unseen again. Surfaced at the cheapest gate (deploy / status).

Mechanism (mirrors git_origin_drift, #190): the public mirror's HEAD commit records the last-mirrored
private SHA in its message ("sync: mirror from private (<sha>)"). We FETCH-ONLY (never merge), parse
that SHA, and count how many private commits are unmirrored. Fail-safe: 'unknown' rather than a false
'current' — an undetected mirror is treated as drift to investigate, never silently blessed.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# run(argv-without-leading-"git") -> (returncode, stdout). MAY raise; callers need not guard.
GitRunner = Callable[[list[str]], "tuple[int, str]"]

# The exact message the sync-to-public workflow writes on each mirror push.
MIRROR_MSG_RE = re.compile(r"sync:\s*mirror from private\s*\(([0-9a-fA-F]{7,40})\)", re.IGNORECASE)

STATUS_CURRENT = "current"
STATUS_BEHIND = "behind"
STATUS_UNKNOWN = "unknown"


def parse_mirrored_sha(public_head_message: str | None) -> str | None:
    """Extract the last-mirrored private SHA from the public HEAD commit subject, or None."""
    m = MIRROR_MSG_RE.search(public_head_message or "")
    return m.group(1) if m else None


def compute_public_mirror_drift(
    run: GitRunner,
    *,
    private_remote: str = "origin",
    private_branch: str = "main",
    public_remote: str = "public",
    attempt_fetch: bool = True,
) -> dict[str, Any]:
    """FETCH-ONLY drift of the public mirror behind private main. Returns:

        status: "current" | "behind" | "unknown"
        behind: int | None  (private commits not yet mirrored)
        mirrored_sha: str | None  (the last private SHA the public HEAD recorded)
        fetched: bool ; reason: str (on unknown) ; commands: list[list[str]]

    Fail-safe: any unparseable/failed step yields "unknown" (NEVER a false "current"). Only "fetch"
    and read-only "log"/"rev-list" are issued — never merge/reset/checkout/push.
    """
    commands: list[list[str]] = []
    fetched = False

    if attempt_fetch:
        for rem, br in ((private_remote, private_branch), (public_remote, "main")):
            argv = ["fetch", "--no-tags", rem, br]
            try:
                rc, _out = run(argv)
                commands.append(argv)
                if rc == 0:
                    fetched = True
            except Exception:
                pass

    pub_msg_argv = ["log", "-1", "--format=%s", f"{public_remote}/main"]
    mirrored: str | None = None
    try:
        rc, out = run(pub_msg_argv)
        commands.append(pub_msg_argv)
        if rc == 0:
            mirrored = parse_mirrored_sha(out)
    except Exception:
        mirrored = None
    if not mirrored:
        return {
            "status": STATUS_UNKNOWN,
            "behind": None,
            "mirrored_sha": None,
            "reason": "could not read the public mirror's last-mirrored sha (no 'sync: mirror from private (...)' on public HEAD)",
            "fetched": fetched,
            "commands": commands,
        }

    count_argv = ["rev-list", "--count", f"{mirrored}..{private_remote}/{private_branch}"]
    behind: int | None = None
    try:
        rc, out = run(count_argv)
        commands.append(count_argv)
        if rc == 0 and (out or "").strip().isdigit():
            behind = int(out.strip())
    except Exception:
        behind = None
    if behind is None:
        return {
            "status": STATUS_UNKNOWN,
            "behind": None,
            "mirrored_sha": mirrored,
            "reason": f"could not count private commits since {mirrored[:12]} (unfetched mirror sha or rev-list failure)",
            "fetched": fetched,
            "commands": commands,
        }

    return {
        "status": STATUS_CURRENT if behind == 0 else STATUS_BEHIND,
        "behind": behind,
        "mirrored_sha": mirrored,
        "fetched": fetched,
        "commands": commands,
    }


def drift_warning(drift: dict[str, Any]) -> str | None:
    """Operator-facing line when the mirror is behind or unverifiable; None when current.
    Returning None on 'current' lets callers attach the warning only when it carries signal."""
    st = drift.get("status")
    if st == STATUS_BEHIND:
        sha = str(drift.get("mirrored_sha") or "")[:12]
        return (
            f"public mirror is {drift.get('behind')} commit(s) behind private main "
            f"(last mirrored {sha}) — publish: a [sync-public] commit or `gh workflow run sync-to-public.yml`"
        )
    if st == STATUS_UNKNOWN:
        return f"public-mirror freshness UNKNOWN — {drift.get('reason')} — verify the mirror manually"
    return None
