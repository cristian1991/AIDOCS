"""#836 - a backlog entry never learns that it was fixed. This tells it.

THE MEASURED PROBLEM. An audit of 27 open critical+urgent items on 2026-08-19
found SEVEN describing defects that no longer existed - a 26% false-open rate at
the top two priorities:

    #744  fixed the SAME DAY it was filed, by its own named ALTERNATIVE
          (6fa5cf89d) - read as a live DEPLOY BLOCKER for 17 days
    #507  fixed the same day its OUTCOME section was written, own number
    #593  fixed under ticket #599, two days after its last update
    #812  corrected 4 hours after its last edit, own number
    #758  wired under #787, 11 days later
    #491  cured under #207/#509
    #489 / #754  delivery blockers fixed by deploy plumbing under #609/#627

THE MECHANISM IS ALWAYS THE SAME: the commit knows the item number, the item
does not know the commit. `git log --grep="#744"` finds nothing, because the fix
landed as `revert(739)`. Attribution flows one way and the backlog sits
downstream of a link nobody is required to complete.

WHY IT IS NOT BOOKKEEPING. A stale critical consumes exactly the attention the
real ones need. #744 was picked up as "critical, deploy BLOCKED" and a whole
investigation ran against a problem that had not existed for 17 days - the item
even instructed its reader to measure something whose subject had been deleted.
An entry that outlives its defect actively misdirects.

IT FLAGS, IT NEVER CLOSES. Three of those seven were only PARTLY satisfied, so a
robot that closed on a commit match would have destroyed real remaining work.
The output is a PROMPT TO VERIFY. Closing stays a judgement call - which is how
#744, #507 and #593 were actually closed: evidence re-checked by hand first.

CHEAP BY CONSTRUCTION. ONE `git log` bounded by the oldest item's updated_at,
parsed once and memoized against HEAD plus a TTL. A per-item `git log --grep`
would be N subprocesses on a hot listing path; this is one, and on a repeat call
inside the window it is zero.

FAIL-QUIET. No git, a detached tree, a broken date - every failure returns "no
markers", never an exception and never a false "this is stale". A staleness
hint that can break the backlog listing would be worse than the staleness.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

#: How long a scan stays warm. Long enough that a burst of list calls pays for
#: one git invocation; short enough that a commit landing mid-session is seen.
SCAN_TTL_S = 300.0

#: Ticket references a commit can carry. The '#' is REQUIRED:
#:   "fix(#744): ...", "... (#507)", "closes #593", "Resolves #744."
#:
#: A BARE "(\d+)" BRANCH WAS TRIED AND REMOVED, 2026-08-19, because it produced
#: false positives on the real backlog within minutes: #672 was flagged by a
#: commit reading "fix(lane-worker): auto-bind binds the WORKER ... (#720 b)",
#: and any line reference of the form `module.py(672)` in a commit body matches
#: it too. A staleness flag that cries wolf is worse than none - it teaches the
#: reader to skip the column, which is exactly how the 26% false-open rate went
#: unnoticed in the first place. Precision over recall here: a missed stale item
#: stays as it is today, a false one costs a verification and some trust.
_TICKET_RE = re.compile(r"#(\d{1,6})\b")

_CACHE: dict[str, object] = {"head": "", "at": 0.0, "map": {}}


_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo_root: Path, *args: str, timeout: float = 25.0) -> str:
    # #345: routed through audited_run so this git spawn lands a process-audit
    # ledger row (coverage-true-by-construction spawn seal). The run= lambda IS
    # the registered direct-run AST callsite (LEGACY_SUBPROCESS_FINGERPRINTS:
    # backlog_staleness.py/_git); kwargs pass through byte-identically.
    #
    # The first version of this module called subprocess.run DIRECTLY and reddened
    # four security seals at once (spawn census, spawn-surface seal, egress
    # chokepoint doctrine, legacy-callsite fingerprints). A new spawn anywhere in
    # this tree must be routed and registered - the seals exist so that "we added
    # one little subprocess" cannot happen quietly, and they worked.
    #
    # CREATE_NO_WINDOW because the daemon runs under pythonw: an unflagged console
    # child pops a VISIBLE WINDOW on the operator's screen, and this fires on a
    # backlog listing.
    from .shell_egress_service import audited_run

    try:
        out = audited_run(
            ["git", "-C", str(repo_root), *args],
            fingerprint=("backlog_staleness.py", "_git", "subprocess.run"),
            reason=(
                "backlog staleness scan - one bounded `git log` over commit "
                "subjects/bodies; fixed subcommands, no shell, no agent input, "
                "read-only, fail-quiet"
            ),
            run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_WIN_NO_WINDOW,
        )
    except Exception:
        return ""
    return out.stdout if out.returncode == 0 else ""


def _head(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "HEAD").strip()


def scan_commits(repo_root: Path, since: str = "") -> dict[int, list[dict]]:
    """{ticket -> [{sha, date, subject}]} from ONE git log pass.

    `since` bounds the walk to the oldest item we could possibly flag. Without
    it this would parse the entire history on every cold call for no gain.
    """
    args = ["log", "--no-merges", "--format=%H%x1f%cI%x1f%s%x1f%b%x1e"]
    if since:
        args.append(f"--since={since}")
    raw = _git(repo_root, *args)
    found: dict[int, list[dict]] = {}
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        parts = record.strip().split("\x1f")
        if len(parts) < 3:
            continue
        sha, date, subject = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        seen: set[int] = set()
        for m in _TICKET_RE.finditer(f"{subject}\n{body}"):
            raw_id = m.group(1) or m.group(2)
            try:
                num = int(raw_id)
            except (TypeError, ValueError):
                continue
            # 1-6 digits is deliberately loose for #N, but a bare "(N)" needs
            # at least two digits or every "(1)" in prose becomes a ticket.
            if num <= 0 or num in seen:
                continue
            seen.add(num)
            found.setdefault(num, []).append(
                {"sha": sha[:9], "date": date, "subject": subject[:120]}
            )
    return found


def _cached_scan(repo_root: Path, since: str) -> dict[int, list[dict]]:
    head = _head(repo_root)
    now = time.monotonic()
    if (
        head
        and _CACHE.get("head") == head
        and now - float(_CACHE.get("at") or 0.0) < SCAN_TTL_S
    ):
        return _CACHE.get("map") or {}  # type: ignore[return-value]
    found = scan_commits(repo_root, since=since)
    _CACHE.update({"head": head, "at": now, "map": found})
    return found


def stale_markers(repo_root: Path, items: list[dict]) -> dict[int, str]:
    """{item_id -> "sha subject"} for OPEN items a commit claims and the item
    never acknowledges.

    THE SIGNAL IS "NOBODY WROTE BACK", NOT "SOMETHING LANDED LATELY". Both rules
    were measured against the real backlog on 2026-08-19:

        commit dated AFTER updated_at   ->   8 of 230 flagged
        commit sha NOT cited in body    ->  10 of 230 flagged

    The date rule is the one #836 originally proposed and it is WRONG for this
    corpus, for a reason worth keeping: an audit pass that merely RE-READS an
    item bumps its updated_at, which then hides every commit predating the
    audit. #744's fix landed 2026-08-02 while its updated_at was 2026-08-18, so
    the date rule would have missed the exact case that motivated the feature.

    "Cited" is the honest test because this codebase's convention is to record
    the sha in the entry when a fix is folded back. An item whose body already
    names the commit has been reconciled by a human; one that does not has not.

    Falls back to the date rule when no body is available (a slim listing),
    because a weak signal beats none.
    """
    if not items:
        return {}
    dates = [str(i.get("updated_at") or "") for i in items if i.get("updated_at")]
    since = min(dates) if dates else ""
    found = _cached_scan(Path(repo_root), since)
    if not found:
        return {}
    out: dict[int, str] = {}
    for item in items:
        try:
            ident = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        commits = found.get(ident)
        if not commits:
            continue
        body = str(item.get("content") or item.get("content_preview") or "")
        if body:
            candidates = [c for c in commits if c["sha"][:7] not in body]
        else:
            updated = str(item.get("updated_at") or "")
            candidates = [
                c for c in commits if not updated or c["date"][:10] > updated[:10]
            ]
        if candidates:
            newest = sorted(candidates, key=lambda c: c["date"])[-1]
            out[ident] = f"{newest['sha']} {newest['subject']}"
    return out


def annotate(repo_root: Path, items: list[dict]) -> list[dict]:
    """Attach `possibly_stale` in place. Never raises; returns ``items``.

    The wording is deliberately hedged. This is EVIDENCE THAT SOMETHING LANDED,
    not a verdict that the item is done - and three of the seven items that
    motivated this were only PARTLY satisfied.
    """
    try:
        markers = stale_markers(Path(repo_root), items)
    except Exception:
        return items
    for item in items:
        try:
            marker = markers.get(int(item.get("id") or 0))
        except (TypeError, ValueError):
            marker = None
        if marker:
            item["possibly_stale"] = marker
    return items
