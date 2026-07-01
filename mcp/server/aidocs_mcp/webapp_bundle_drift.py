"""Web-dashboard bundle drift detector (dashboard-war (b)).

The gate serves the web dashboard from COMMITTED build artifacts at
``mcp/server/aidocs_mcp/templates/webapp/`` (built by
``apps/aidocs-dashboard`` ``npm run sync:web``, which stamps the bundle
with ``build-info.json``). Nothing in the deploy pipeline rebuilt that
bundle, so the served frontend silently drifted from the desktop app —
the exact class the public-mirror guard kills for the public repo.

Drift definition (honest, not paranoid):

  current — no dashboard-relevant source changed since the bundle's
            build sha (unrelated commits moving HEAD do NOT stale it).
  stale   — apps/aidocs-dashboard/** (EXCLUDING src-tauri/, the
            desktop-only Rust shell) changed after the build sha.
  unknown — missing/malformed stamp, or the git query failed.
            Fail-safe: surfaced, never a crash, never a silent
            'current'.

Pure core: the git query is injected (``changed_paths_fn(base, head)``
returns the changed paths between the two commits) so the decision
logic is deterministic and unit-testable. ``changed_paths_via_git`` is
the real-git binding used by mcp/scripts/check_webapp_bundle.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The web bundle is built from the dashboard frontend source. The Rust
# shell (src-tauri/) is desktop-only and never enters the web bundle.
DASHBOARD_SOURCE_PREFIX = "apps/aidocs-dashboard/"
_TAURI_ONLY_PREFIX = "apps/aidocs-dashboard/src-tauri/"

BUILD_INFO_NAME = "build-info.json"


def _read_bundle_sha(webapp_dir: Path) -> str | None:
    try:
        raw = (webapp_dir / BUILD_INFO_NAME).read_text(encoding="utf-8")
        info = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    sha = info.get("sha") if isinstance(info, dict) else None
    if isinstance(sha, str) and sha.strip() and sha.strip() != "unknown":
        return sha.strip()
    return None


def _is_web_relevant(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.startswith(DASHBOARD_SOURCE_PREFIX) and not p.startswith(_TAURI_ONLY_PREFIX)


def compute_webapp_bundle_drift(
    webapp_dir: Path,
    *,
    head_sha: str,
    changed_paths_fn: Callable[[str, str], list[str]],
) -> dict[str, Any]:
    """Classify the served web bundle against HEAD. See module doc for
    the current/stale/unknown contract."""
    bundle_sha = _read_bundle_sha(Path(webapp_dir))
    if bundle_sha is None:
        return {
            "status": "unknown",
            "bundle_sha": None,
            "head_sha": head_sha,
            "changed": [],
            "reason": f"no readable {BUILD_INFO_NAME} in {webapp_dir}",
        }
    if head_sha.startswith(bundle_sha) or bundle_sha.startswith(head_sha):
        return {
            "status": "current",
            "bundle_sha": bundle_sha,
            "head_sha": head_sha,
            "changed": [],
            "reason": "bundle built at HEAD",
        }
    try:
        changed_all = list(changed_paths_fn(bundle_sha, head_sha))
    except Exception as exc:  # noqa: BLE001 — fail-safe to 'unknown', by contract
        return {
            "status": "unknown",
            "bundle_sha": bundle_sha,
            "head_sha": head_sha,
            "changed": [],
            "reason": f"git query failed: {exc}",
        }
    changed = [p for p in changed_all if _is_web_relevant(p)]
    if changed:
        return {
            "status": "stale",
            "bundle_sha": bundle_sha,
            "head_sha": head_sha,
            "changed": changed,
            "reason": f"{len(changed)} dashboard source file(s) changed since bundle build",
        }
    return {
        "status": "current",
        "bundle_sha": bundle_sha,
        "head_sha": head_sha,
        "changed": [],
        "reason": "no dashboard source change since bundle build",
    }


def drift_warning(result: dict[str, Any]) -> str | None:
    """One operator-facing line for stale/unknown; None when current."""
    status = result.get("status")
    if status == "current":
        return None
    if status == "stale":
        return (
            f"web dashboard bundle is STALE: built @{result.get('bundle_sha')}, "
            f"{len(result.get('changed') or [])} dashboard file(s) changed since "
            f"(HEAD {result.get('head_sha')}). Run `npm run sync:web` in "
            f"apps/aidocs-dashboard and commit templates/webapp."
        )
    return (
        f"web dashboard bundle freshness UNKNOWN: {result.get('reason')}. "
        f"Rebuild via `npm run sync:web` to restore the stamp."
    )


# NOTE (§6 chokepoint law): the real-git binding for changed_paths_fn lives in
# mcp/scripts/check_webapp_bundle.py — deploy-time tooling, outside the server
# runtime. This module stays PURE (no subprocess) so the shell-egress semgrep
# law holds: server code never spawns outside ShellEgressService.
