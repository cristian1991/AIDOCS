from __future__ import annotations

from ..csharp_outliner_artifact import OutlinerUnavailable

__all__ = ["OutlinerUnavailable", "extract_csharp_outline"]


def extract_csharp_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    """Extract a flat outline from C# source text — via the TRUSTED Roslyn
    outliner ONLY.

    There is deliberately NO regex fallback. The one that lived here missed
    every member of a one-line class (and any construct its patterns did not
    anticipate) while reporting success, so a missing toolchain looked exactly
    like an empty class. Now the absence is loud: ``OutlinerUnavailable`` (with
    a reason code) propagates to the indexer, which marks the file NOT_INDEXED,
    drops its old rows, and stamps discovery results with partial coverage.
    """
    from ..csharp_roslyn_client import roslyn_outline

    return roslyn_outline(text, ext=".cs")
