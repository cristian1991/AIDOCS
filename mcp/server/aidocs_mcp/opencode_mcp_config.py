"""OpenCode's aidocs MCP entry — the SAME shape Claude Code gets (#1202, #1192).

THE DEFECT. The operator's OpenCode entry was
``["…\\generations\\gen-…\\venv\\Scripts\\aidocs-mcp.exe", "-m",
"aidocs_mcp.mcp_server"]``. Two faults in one line:

  * ``aidocs-mcp`` is the console script for ``aidocs_mcp.mcp_server:main`` —
    the FULL in-process server, not the stdio shim — so no request ever carried
    the per-window identity header (ai_version: "no shim request seen") and
    #1192's OpenCode identity could not work however correct its code was;
  * it named one GENERATION, which the next update supersedes and GC removes.

THE CURE. One builder, :func:`opencode_mcp_entry`, emits what Claude Code's
``.mcp.json`` carries: ``<aidocs-python launcher> -m aidocs_mcp.stdio_shim``
with ``AIDOCS_PROJECT_ROOT`` and ``AIDOCS_MCP_ENDPOINT``. The launcher reads
``runtime/current.json`` at every spawn, so the entry never goes stale.
:func:`ensure_opencode_mcp_entry` read-merge-writes ONLY ``mcp.aidocs`` into a
plain-JSON config, with a backup, and refuses a JSONC file it cannot
round-trip rather than destroying the operator's comments.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import runtime_generations as rg
from . import runtime_launchers as rl

#: Same default the shim itself falls back to (stdio_shim.DEFAULT_ENDPOINT).
DEFAULT_ENDPOINT = "http://127.0.0.1:8748/mcp"


def _endpoint() -> str:
    try:
        from .runtime_project_support_service import _derived_daemon_url

        return _derived_daemon_url() or DEFAULT_ENDPOINT
    except Exception:  # noqa: BLE001 — config generation never wedges on a probe
        return DEFAULT_ENDPOINT


def opencode_mcp_entry(
    project_root: Path | str,
    python_path: str,
    *,
    endpoint: str | None = None,
    home: Path | str | None = None,
) -> dict:
    """The canonical OpenCode ``mcp.aidocs`` entry.

    PREFERRED (option B): the STATIC stdlib stdio shim at its fixed
    path, run by the hook shim's stable interpreter -- nothing in it can go
    stale at an update. Otherwise ``AIDOCS_PYTHON`` (the operator pin) or the
    generation-following launcher over the CURRENT sealed generation, with
    ``-m aidocs_mcp.stdio_shim`` (#1202, r0b2 P1). RAISES
    ``runtime_generations.CurrentGenerationError`` when none exists: a
    generation path goes stale at the next update, and an ambient interpreter
    may import a stale aidocs_mcp. ``python_path`` only locates the home;
    ``home`` names it directly (the migration sweep has no interpreter path).
    """
    if home is None:
        home = rl.home_of_generation_interpreter(python_path)
    try:
        command = rl.static_shim_argv(home)
    except rg.CurrentGenerationError:
        command = [*rl.owned_interpreter_argv(home, allow_generation_path=False), "-m", "aidocs_mcp.stdio_shim"]
    return {
        "type": "local",
        "enabled": True,
        "timeout": 120000,
        "command": command,
        "environment": {
            "AIDOCS_PROJECT_ROOT": str(project_root),
            "AIDOCS_MCP_ENDPOINT": endpoint or _endpoint(),
        },
    }


def ensure_opencode_mcp_entry(config_path: Path | str, entry: dict) -> dict:
    """Merge ``entry`` into ``mcp.aidocs`` of an OpenCode JSON config.

    Every other key survives. A prior file is backed up before it is changed.
    A file that is not plain JSON (JSONC comments, trailing commas) is
    REFUSED untouched — re-serialising it would silently delete the
    operator's comments, the #808 failure mode.
    """
    p = Path(config_path)
    data: dict = {}
    existed = p.is_file()
    if existed:
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError) as exc:
            return {"action": "refused", "path": str(p), "reason": f"not plain JSON ({type(exc).__name__}); edit mcp.aidocs by hand"}
        if not isinstance(data, dict):
            return {"action": "refused", "path": str(p), "reason": "top level is not a JSON object"}
    mcp = data.get("mcp")
    if mcp is None:
        mcp = {}
    if not isinstance(mcp, dict):
        return {"action": "refused", "path": str(p), "reason": "`mcp` is not an object"}
    if mcp.get("aidocs") == entry:
        return {"action": "no_change", "path": str(p)}
    backup = ""
    if existed:
        backup = str(p.with_name(f"{p.name}.aidocs-backup-{time.strftime('%Y%m%dT%H%M%S')}"))
        shutil.copy2(p, backup)
    mcp["aidocs"] = entry
    data["mcp"] = mcp
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".aidocs-oc-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"action": "updated" if existed else "created", "path": str(p), "backup": backup}
