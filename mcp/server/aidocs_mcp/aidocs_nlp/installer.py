"""Language pack installer — wraps `spacy.cli.download` via subprocess.

Subprocess approach (not in-process `spacy.cli.download(name)`) so:
  - install runs without blocking the MCP server's event loop;
  - failures don't crash the parent (pip can OOM on big models);
  - progress is streamable line-by-line for dashboard UX.

The allowlist gate runs BEFORE the subprocess fires — operators can't
sneak arbitrary spacy.cli.download() calls past the security check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .language_registry import DEFAULT_PACKS, LanguagePack


@dataclass
class InstallProgress:
    install_id: str
    pack: LanguagePack
    state: str  # "starting" | "running" | "done" | "failed"
    bytes_downloaded: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    last_message: str = ""
    error: str = ""


def is_pack_allowed(
    pack: LanguagePack,
    extra_allowlist: Iterable[str] = (),
) -> bool:
    """Allowlist gate. DEFAULT_PACKS always allowed; per-project /
    per-operator `nlp.pack_allowlist_extra` adds names (used for
    custom domain models).
    """
    if pack in DEFAULT_PACKS:
        return True
    if pack.model_name in set(extra_allowlist):
        return True
    return False


class Installer:
    """Spawns spacy model installs; tracks progress; cancellable."""

    def __init__(self):
        self._installs: dict[str, InstallProgress] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._callbacks: dict[str, list[Callable[[InstallProgress], None]]] = {}

    def start(
        self,
        pack: LanguagePack,
        extra_allowlist: Iterable[str] = (),
    ) -> InstallProgress:
        if not is_pack_allowed(pack, extra_allowlist):
            raise PermissionError(
                f"Pack {pack.model_name} not in allowlist. Add to "
                f"nlp.pack_allowlist_extra if you trust the source.",
            )
        install_id = uuid.uuid4().hex[:12]
        progress = InstallProgress(
            install_id=install_id,
            pack=pack,
            state="starting",
            started_at=time.time(),
        )
        with self._lock:
            self._installs[install_id] = progress
        threading.Thread(
            target=self._run,
            args=(install_id,),
            daemon=True,
        ).start()
        return progress

    def status(self, install_id: str) -> InstallProgress | None:
        with self._lock:
            return self._installs.get(install_id)

    def cancel(self, install_id: str) -> bool:
        with self._lock:
            process = self._processes.get(install_id)
        if process is None:
            return False
        try:
            process.terminate()
            return True
        except Exception:
            return False

    def subscribe(
        self,
        install_id: str,
        callback: Callable[[InstallProgress], None],
    ) -> None:
        with self._lock:
            self._callbacks.setdefault(install_id, []).append(callback)

    def list_active(self) -> tuple[InstallProgress, ...]:
        with self._lock:
            return tuple(p for p in self._installs.values() if p.state in ("starting", "running"))

    def _run(self, install_id: str) -> None:
        with self._lock:
            progress = self._installs[install_id]
        progress.state = "running"
        self._notify(install_id, progress)
        try:
            # spacy.cli.download is just `python -m spacy download MODEL`.
            cmd = [sys.executable, "-m", "spacy", "download", progress.pack.model_name]
            env = os.environ.copy()
            env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            with self._lock:
                self._processes[install_id] = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                progress.last_message = line.rstrip()
                # pip emits "MB/s" lines and a final "Successfully installed".
                self._notify(install_id, progress)
            proc.wait()
            if proc.returncode == 0:
                progress.state = "done"
            else:
                progress.state = "failed"
                progress.error = f"spacy download exited {proc.returncode}"
        except Exception as exc:
            progress.state = "failed"
            progress.error = str(exc)
        finally:
            progress.finished_at = time.time()
            with self._lock:
                self._processes.pop(install_id, None)
            self._notify(install_id, progress)

    def uninstall(self, pack: LanguagePack) -> tuple[bool, str]:
        """`pip uninstall -y <model_pkg>`. Returns (success, output)."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", pack.model_name],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return (result.returncode == 0, result.stdout + result.stderr)
        except Exception as exc:
            return (False, str(exc))

    def _notify(self, install_id: str, progress: InstallProgress) -> None:
        with self._lock:
            callbacks = list(self._callbacks.get(install_id, ()))
        for cb in callbacks:
            try:
                cb(progress)
            except Exception:
                pass
