"""tool_families — per-family @tool declarations for the unified registry.

Each module in this package (discovery.py, lifecycle.py, conductor.py,
execution.py, …) declares additional ``@tool`` specs that register into the
SINGLE shared registry in ``tool_interface`` (``_TOOLS`` / ``REGISTRY``) at
import time. This package is the aggregation point: importing it imports
every family module, so ``tool_interface`` only has to ``from . import
tool_families`` once (it does, at end-of-file) and every family's specs land
in the one registry.

Auto-discovery (no hand-maintained import list): every non-underscore
submodule is imported here. A new family lane only has to DROP its
``<family>.py`` file — no edit to this file — which keeps parallel family
lanes conflict-free.
"""

from __future__ import annotations

import importlib
import pkgutil

for _mod in pkgutil.iter_modules(__path__):
    if not _mod.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_mod.name}")
