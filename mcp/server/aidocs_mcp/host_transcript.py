"""Own-project Claude Code transcript recognition (ai_read_jsonl carve-out).

Claude Code writes each session transcript to
``<home>/.claude/projects/<slug>/<session-uuid>.jsonl`` where ``<slug>`` is the
project path with EVERY non-alphanumeric character replaced by ``-``
(``D:\\Projects\\Active\\AIDOCS`` -> ``D--Projects-Active-AIDOCS``; verified
against real dirs: ``DevSoft.Asistenza`` -> ``DevSoft-Asistenza``). Drive-letter
casing varies (``d--...`` vs ``D--...``), so comparison is case-insensitive
where the filesystem is (``os.path.normcase``).

The slug is LOSSY (``A.B``, ``A-B`` and ``A_B`` all collide), so slug equality
is NOT project identity. Identity comes from the file NAME: it must be
``<uuid>.jsonl`` for a host session UUID OWNED by the bound managed session.
The caller (the orchestrator) computes that owned-id bag from its
``_host_session_ids`` chain and passes it in; this module never reads
hub/session state. Only UUID-shaped entries count
(``session_artifact.usable_session_uuids``); no usable UUID -> fail closed.

The SEC-004 zone rail classifies these files UNKNOWN_EXTERNAL and refuses them.
``is_own_project_transcript`` is the ONE narrow exception, used by the
orchestrator ONLY for the read-only tool(s) in ``TRANSCRIPT_READ_TOOLS``. It is
True only when ALL hold:

  * the input is absolute and has NO ``..`` segment (no laundering, even one
    that collapses back into the own dir);
  * the name is ``<owned-uuid>.jsonl`` (stem, lowercased, in the usable owned
    UUID set);
  * lexically, the file is a DIRECT child of
    ``<home>/.claude/projects/<slug-of-bound-project-root>``;
  * canonically (symlinks/junctions resolved), the own dir is still a direct
    child of the resolved ``<home>/.claude/projects`` with the same name, and
    the resolved file is a regular file DIRECTLY inside the resolved own dir
    with the same name.

Refused: other projects' dirs, ~/.claude/.credentials.json / settings /
history, the projects root itself, arbitrary or foreign-UUID direct children,
nested files (subagent dirs), non-jsonl, relative paths, missing files, any
symlink/junction escape, and everything when the owned bag has no usable UUID.
Pure; no I/O beyond stat/resolve.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from .session_artifact import usable_session_uuids

# Tools allowed to use the carve-out. Read-only by construction; never add a
# mutating tool here.
TRANSCRIPT_READ_TOOLS: frozenset[str] = frozenset({"ai_read_jsonl"})


def claude_transcript_slug(project_root: str | os.PathLike) -> str:
    """Claude Code's transcript-dir slug for ``project_root``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project_root))


def _same(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b)),
    )


def _root_slugs(project_root: str | os.PathLike) -> list[str]:
    root = Path(project_root)
    out = [claude_transcript_slug(root.absolute())]
    try:
        out.append(claude_transcript_slug(root.resolve()))
    except (OSError, RuntimeError):
        pass
    return out


def is_own_project_transcript(
    target: str | os.PathLike | None,
    *,
    project_root: str | os.PathLike,
    owned_session_ids: Iterable[str] | None,
    home: str | os.PathLike | None = None,
) -> bool:
    """True iff ``target`` is an OWNED transcript of the BOUND project (see module doc)."""
    try:
        owned = usable_session_uuids(owned_session_ids)
        if not owned:
            return False  # ownership cannot be established -> fail closed
        raw = str(target or "").strip()
        if not raw or not str(project_root or "").strip():
            return False
        p = Path(raw)
        if not p.is_absolute() or ".." in p.parts:
            return False
        if p.suffix.lower() != ".jsonl" or p.stem.lower() not in owned:
            return False
        projects = (Path(home) if home else Path.home()) / ".claude" / "projects"
        projects_real = projects.resolve(strict=True)
        lexical_parent = Path(os.path.abspath(p)).parent
        for slug in _root_slugs(project_root):
            own_lex = projects / slug
            if not _same(lexical_parent, own_lex):
                continue
            own_real = own_lex.resolve(strict=True)
            if not _same(own_real.parent, projects_real):
                return False  # own dir is a link out of the projects root
            if os.path.normcase(own_real.name) != os.path.normcase(slug):
                return False  # own dir is a link to ANOTHER project's dir
            f_real = p.resolve(strict=True)
            if not _same(f_real.parent, own_real):
                return False  # file is a link out of the own dir
            if os.path.normcase(f_real.name) != os.path.normcase(p.name):
                return False  # file is a link to a DIFFERENT (unowned) transcript
            return f_real.is_file()
        return False
    except (OSError, RuntimeError, ValueError):
        return False
