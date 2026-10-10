#!/usr/bin/env python3
"""Copy the shipped SESSION-SCAFFOLD template into the wheel's package data.

DEV/BUILD-TIME tool, mirroring ``build_project_memory_seed.py``. Reads the
canonical template at ``core/.MEMORY/.aidocs/templates/context.md`` and writes it
to ``mcp/server/aidocs_mcp/data/context.md`` so a pip-installed runtime — one
with no repo checkout anywhere above it — can still scaffold a session.

WHY THIS EXISTS (backlog #656). ``core/`` is outside the wheel's package dir, so
an installed-only host had no template tree at all. That is a LIFECYCLE DEADLOCK,
not an inconvenience: ``ai_task(mode='begin')`` refuses without a SESSION.md,
nearly every read/edit/discovery tool refuses without an active task,
``ai_create_file`` — the offered way to write a SESSION.md — also refuses without
an active task, and raw ``Write`` is redirected to ``ai_create_file``. The only
unblocked route is ``ai_session(mode='create')``, which needs this file. An
installed agent without it cannot bootstrap itself out of the hole.

(#666, closed 2026-08-19, fixed the adjacent LIE: the resolver used to fabricate
``<venv>/core/.MEMORY/.aidocs/templates/context.md`` and ENOENT on it. It now
raises ``TemplatesRootUnresolved`` naming what it probed. That made the failure
honest; this makes it stop happening.)

EXACTLY ONE FILE, DELIBERATELY. ``session_store.py`` calls it out in-code —
"#628: the ONE template read in the codebase". Session creation makes ``plans/``,
``agents/`` and ``artifacts/`` itself and renders SESSION.md from in-code
sections, so ``context.md`` is the whole payload. Copying the rest of the
template tree would ship bytes nothing reads. (The ``templates/memory/**`` half is
already shipped, separately and as SQL, by ``build_project_memory_seed.py``.)

FLAT IN ``data/``, NOT A SUBDIRECTORY. ``path_resolver_service._tree_candidates``
yields ``base / "data"`` and ``_is_real`` tests ``candidate / "context.md"``. A
copy at ``data/session_templates/context.md`` would ship correctly and never be
found — a fix that looks right and does nothing.

DRIFT. The copy is committed, like ``seed/factory.sqlite3``. That is only safe
because ``tests/architecture/test_session_template_packaging_656.py`` asserts it
is BYTE-IDENTICAL to the source, so a drifted copy cannot be committed. Never
hand-edit the shipped file — edit the source and re-run this script. A stale
template is worse than a missing one: a missing one fails loudly and names its
remedy, a stale one scaffolds a plausible wrong context.md nobody notices.

Regenerate after editing the template:

    python core/scripts/build_session_templates.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "core" / ".MEMORY" / ".aidocs" / "templates" / "context.md"
TARGET = REPO_ROOT / "mcp" / "server" / "aidocs_mcp" / "data" / "context.md"


def main() -> int:
    if not SOURCE.is_file():
        print(f"ERROR: canonical template missing: {SOURCE}", file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)

    if TARGET.is_file() and TARGET.read_bytes() == SOURCE.read_bytes():
        print(f"already current: {TARGET.relative_to(REPO_ROOT)}")
        return 0

    # copyfile, not copy2: metadata is irrelevant to a text payload and copying
    # mtime across makes a regenerated file look untouched to a reviewer.
    shutil.copyfile(SOURCE, TARGET)
    print(
        f"wrote {TARGET.relative_to(REPO_ROOT)} "
        f"({TARGET.stat().st_size} bytes) from {SOURCE.relative_to(REPO_ROOT)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
