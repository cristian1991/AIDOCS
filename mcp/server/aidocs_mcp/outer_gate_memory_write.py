"""Governed memory write for the web-dashboard capture form (#200, war d).

ONE write path (Empire doctrine): this delegates to MemoryStore.capture_memory —
the SAME API the memory_capture agent tool uses — so the durability rubric,
kind aliasing, sovereign guard and sqlite-canonical storage apply identically
over the gate. No second write engine, no drift.

Fails CLOSED to a structured dict; never lets an exception (or a traceback)
reach the transport/web client.
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
