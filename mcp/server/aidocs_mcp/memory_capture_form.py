"""Governed memory write for a dashboard capture form (#200, war d) -- neutral.

ONE write path (project rule): this delegates to MemoryStore.capture_memory --
the SAME API the memory_capture agent tool uses -- so the durability rubric,
kind aliasing, sovereign guard and sqlite-canonical storage apply identically
for the desktop dashboard (cli dashboard-memory-capture) and the web gate.

Design decision (2026-10-29): no public module imports a gate file. The body is
local memory logic, so it lives here; outer_gate_memory_write re-exports it for
the web gate.

Fails CLOSED to a structured dict; never lets an exception (or a traceback)
reach the caller.
"""

from __future__ import annotations

from pathlib import Path


def memory_capture_web(
    root: str,
    kind: str,
    content: str,
    target_hint: str | None = None,
) -> dict:
    from .memory_store import MemoryStore

    try:
        res = MemoryStore().capture_memory(Path(root), kind, content, target_hint)
    except (ValueError, RuntimeError) as e:
        # Doctrine rejections (non-durable kind/content, sovereign target,
        # reserved filename) and the sqlite fail-closed path.
        return {"ok": False, "_error": "memory_rejected", "_detail": str(e)[:400]}
    except Exception as e:  # noqa: BLE001 — write fails closed, never leaks
        return {"ok": False, "_error": "memory_capture_failed", "_detail": str(e)[:200]}
    try:
        rel = res.target_file.relative_to(Path(root) / ".MEMORY").as_posix()
    except ValueError:
        rel = res.target_file.name
    return {"ok": True, "target": rel, "checksum": res.sqlite_checksum}
