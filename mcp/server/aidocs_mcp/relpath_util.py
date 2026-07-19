"""Cheap root-relative posix path helper for hot loops.

The freshness walks need each file's path relative to a root, as a forward-slashed
string, exactly as ``child.relative_to(root).as_posix()`` would produce — but
without paying ``pathlib.relative_to`` (normalize + tuple-walk) per file.

Strategy: precompute a string prefix ONCE per root, then slice each child's
``as_posix()`` in the loop. The prefix handles the cases a naive ``len(root)+1``
slice gets wrong:

  * relative root (``project``)            -> prefix ``project/``
  * absolute nested root (``/a/b/proj``)   -> prefix ``/a/b/proj/``
  * drive root (``C:/``) / fs root (``/``) -> prefix is the root itself (it
    already ends in ``/``; no extra separator to add)
  * current dir (``.``)                    -> prefix is empty (strip nothing;
    pathlib drops the leading ``./`` from children anyway)
"""
from __future__ import annotations

from pathlib import Path


def posix_root_prefix(root: Path) -> str:
    """The prefix to strip from a child's ``as_posix()`` to get its path relative
    to ``root``. Returns ``""`` when nothing should be stripped (root is ``.``)."""
    rp = root.as_posix()
    if rp == ".":
        return ""
    # A root that already ends in '/' (filesystem root '/' or a drive root
    # 'C:/') needs no extra separator; otherwise append one.
    return rp if rp.endswith("/") else rp + "/"


def relpath_posix(child: Path, root_prefix: str) -> str:
    """``child`` relative to the root whose prefix is ``root_prefix`` (from
    :func:`posix_root_prefix`), as a forward-slashed string. Cheap: one
    ``as_posix()`` + slice, no ``pathlib.relative_to``.

    For paths genuinely under the root (the walk guarantees this) the result
    equals ``child.relative_to(root).as_posix()``. If a child is not under the
    prefix (shouldn't happen for walk output), the full posix path is returned
    rather than raising — a defensive passthrough, not a correctness path."""
    cp = child.as_posix()
    if not root_prefix:
        return cp
    if cp.startswith(root_prefix):
        return cp[len(root_prefix):]
    return cp
