"""ai_issues — immutable local issue filing (#449 v1, WAR F).

The refusal-footer's non-admin leg (tool_gate_service.false_positive_
affordance) points callers that CANNOT pull the ai_backlog lever at
this tool instead: file ONE immutable issue describing the refusal so
the operator reviews it out-of-band.

v1 contract (local immutable + git-committed; the remote PR push /
UPS user-intent leg is v2 — deliberately NOT built here):

* mode='file' writes ONE write-once file
  ``.MEMORY/issues/<utc-ts>-<8hex>.json`` — {issue_id, content, tags,
  actor, attribution, created_at, content_hash} — then git add+commits
  ONLY that file on the CURRENT branch via the governed git helper
  (git_helpers.run_git_sync). No push. The commit is what makes the
  filing immutable-in-history (#440/#441 spirit): rewriting the file
  after the fact diverges from the committed blob.
* INTENT GATE: filing demands the literal param confirm='file-issue'.
  Without it the call returns a two-phase prompt naming the exact
  phrase and writes NOTHING (mirrors ai_delete's two-phase confirm —
  an immutable, git-committed artifact must never be created as a
  side effect of a blindly-copied hint).
* mode='list' is a terse inventory: [{issue_id, snippet<=100ch,
  created_at, actor}].

Actor resolution: the AUTHENTICATED uid via the project_authority
identity seam (authorization-grade — dashboard token / approved host
binding / machine login). The ambient audit-ATTRIBUTION identity
(identity_resolver.current_user) is recorded separately under
``attribution`` and is never promoted to ``actor``: attribution can
fall back to env identity / a bootstrapped local user, so conflating
the two would let an unauthenticated caller impersonate an
authenticated filing (same doctrine as _authenticated_uid's docstring).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The literal intent phrase mode='file' demands. The refusal-footer hint
# deliberately OMITS it — the first (unconfirmed) call returns this
# phrase, so filing always takes one explicit, stated-intent step.
CONFIRM_PHRASE = "file-issue"

ISSUES_DIRNAME = (".MEMORY", "issues")
_SNIPPET_CHARS = 100
_COMMIT_SUBJECT_CHARS = 60


def issues_dir(project_root: Path) -> Path:
    return Path(project_root).joinpath(*ISSUES_DIRNAME)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_actor(project_root: Path) -> tuple[str, dict[str, str]]:
    """(actor, attribution) — authenticated uid vs ambient attribution.

    actor: project_authority._authenticated_uid or '' (fail-closed —
    never the attribution identity).
    attribution: best-effort ambient audit-attribution fields
    (identity_resolver.current_user), attribution-only by doctrine.
    """
    actor = ""
    try:
        from . import project_authority

        actor = project_authority._authenticated_uid(Path(project_root)) or ""
    except Exception:
        actor = ""
    attribution: dict[str, str] = {}
    try:
        from .identity_resolver import current_user

        uid, email, ptype = current_user(Path(project_root))
        attribution = {
            "user_id": str(uid or ""),
            "email": str(email or ""),
            "principal_type": str(ptype or ""),
        }
    except Exception:
        attribution = {}
    return actor, attribution


def confirm_prompt() -> dict[str, Any]:
    """The two-phase prompt returned when mode='file' lacks the phrase."""
    return {
        "ok": False,
        "confirm_required": True,
        "confirm": CONFIRM_PHRASE,
        "error": (
            "filing an issue is an immutable, git-committed act — explicit "
            f"intent required. Re-invoke with confirm='{CONFIRM_PHRASE}' "
            "(literal) to file it. Nothing was written."
        ),
    }


def file_issue(
    project_root: Path,
    *,
    content: str,
    tags: list[str] | None = None,
    confirm: str = "",
) -> dict[str, Any]:
    """File ONE immutable issue. Two-phase: no phrase → prompt, no write."""
    body_text = (content or "").strip()
    if not body_text:
        return {"ok": False, "error": "content required (non-empty)"}
    if (confirm or "").strip() != CONFIRM_PHRASE:
        return confirm_prompt()

    root = Path(project_root)
    target_dir = issues_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    created_at = _utc_now_iso()
    ts_slug = created_at.replace("-", "").replace(":", "")
    issue_id = f"{ts_slug}-{uuid.uuid4().hex[:8]}"
    path = target_dir / f"{issue_id}.json"

    actor, attribution = _resolve_actor(root)
    record: dict[str, Any] = {
        "issue_id": issue_id,
        "content": body_text,
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "actor": actor,
        "attribution": attribution,
        "created_at": created_at,
    }
    # content_hash = sha256 over the canonical json of the body WITHOUT
    # the hash field itself, so any later mutation of any field is
    # detectable by recomputing.
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False)
    record["content_hash"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    try:
        # mode='x' — WRITE-ONCE at the OS level: an existing path refuses.
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
    except FileExistsError:
        return {
            "ok": False,
            "error": f"issue file already exists (write-once, immutable): {path.name}",
        }

    result: dict[str, Any] = {
        "ok": True,
        "issue_id": issue_id,
        "path": path.relative_to(root).as_posix(),
        "actor": actor,
        "created_at": created_at,
        "content_hash": record["content_hash"],
        # Truthful v1 label: the remote PR push is v2, not built.
        "pushed": False,
    }
    result.update(_commit_issue_file(root, path, body_text))
    return result


def verify_issue_hash(path: Path) -> bool:
    """Recompute content_hash from the stored body; True iff it matches."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    stored = str(record.pop("content_hash", ""))
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return stored == "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _commit_issue_file(root: Path, path: Path, body_text: str) -> dict[str, Any]:
    """git add + commit ONLY the issue file, current branch, NO push (v1).

    Pathspec-limited commit (`git commit -m msg -- <file>`) so unrelated
    staged work is never swept into the issue commit. A commit failure is
    reported truthfully (committed=False + error) — the on-disk file
    stands, but the immutable-in-history property is NOT claimed.
    """
    from .git_helpers import run_git_sync

    rel = path.relative_to(root).as_posix()
    first_line = body_text.splitlines()[0][:_COMMIT_SUBJECT_CHARS]
    try:
        run_git_sync(str(root), "add", "--", rel)
        run_git_sync(str(root), "commit", "-m", f"issue: {first_line}", "--", rel)
        sha = run_git_sync(str(root), "rev-parse", "HEAD").strip()
        return {"committed": True, "commit": sha}
    except Exception as exc:  # noqa: BLE001 — truthful degradation, never fake-green
        return {"committed": False, "commit_error": str(exc)}


def list_issues(project_root: Path) -> list[dict[str, Any]]:
    """Terse inventory of filed issues, newest first."""
    target_dir = issues_dir(Path(project_root))
    if not target_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(target_dir.glob("*.json"), reverse=True):
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            # An unreadable file is still inventory — name it, don't hide it.
            out.append({"issue_id": entry.stem, "snippet": "<unreadable>", "created_at": "", "actor": ""})
            continue
        body = str(record.get("content") or "")
        out.append(
            {
                "issue_id": str(record.get("issue_id") or entry.stem),
                "snippet": body[:_SNIPPET_CHARS],
                "created_at": str(record.get("created_at") or ""),
                "actor": str(record.get("actor") or ""),
            }
        )
    return out


def register_issue_filing_tools(*, server: Any, hub: Any, runtime: Any) -> None:
    """Back-compat shim — the registration MOVED to server_issue_tools.py so the
    outer-gate manifest's server_*_tools.py glob discovers it (glob-not-manual-
    list law, test_outer_gate_manifest). Service logic stays in this module.
    """
    from .server_issue_tools import register_issue_filing_tools as _impl

    _impl(server=server, hub=hub, runtime=runtime)

