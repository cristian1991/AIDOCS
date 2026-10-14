from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_git_sync(cwd: str, *args: str, timeout: int = 10) -> str:
    out_path = err_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".out", delete=False) as handle:
            out_path = handle.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".err", delete=False) as handle:
            err_path = handle.name
        with (
            open(out_path, "w", encoding="utf-8") as out_fh,
            open(err_path, "w", encoding="utf-8") as err_fh,
        ):
            # #345: routed through audited_run (ledger row per spawn).
            # Passthrough lambda IS the registered AST callsite; kwargs UNCHANGED.
            from .shell_egress_service import audited_run

            result = audited_run(
                ["git", "-c", "safe.directory=*", *args],
                fingerprint=("git_helpers.py", "run_git_sync", "subprocess.run"),
                reason="git-porcelain-helper",
                run=lambda *a, **kw: subprocess.run(*a, **kw),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=_WIN_NO_WINDOW,
            )
        stdout = Path(out_path).read_text(encoding="utf-8", errors="ignore").strip()
        stderr = Path(err_path).read_text(encoding="utf-8", errors="ignore").strip()
    finally:
        for path in (out_path, err_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    if result.returncode != 0:
        message = (stderr or stdout or f"git exited with code {result.returncode}").strip()
        raise RuntimeError(message)
    return stdout
