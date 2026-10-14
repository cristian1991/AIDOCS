"""Install-wide startup steps every AIDOCS process runs at its entry (#1095).

WHY. A migration of install-wide state cannot ride a read (a read-only tool
must not mutate) and cannot ride one process's boot either: the same store is
read by the MCP server, the CLI (and the watchdog it runs), the hook broker,
the in-process hook fallback, the runtime refresher, the host-adapter CLI and
the outer gate. Whichever of them starts first on an upgraded install must
already see migrated state. So each process ENTRY calls
`run_process_bootstrap()` before it does anything else, and
tests/security/test_project_registry_is_not_written_by_reads_1095.py pins that
every package entry either calls it or carries a written exemption.

CONTRACT. Every step is idempotent and cheap when there is nothing to do (a
stat, not a write), and never raises: a failed step is logged and retried by
the next process start. A step never runs from a read path.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("aidocs.process_bootstrap")


def run_process_bootstrap() -> None:
    """Run the install-wide startup steps. Never raises."""
    try:
        from .known_projects_store import migrate_legacy_registry

        migrate_legacy_registry()
    except Exception:  # noqa: BLE001 -- never block a process start
        _log.exception("known-projects legacy registry migration failed")
