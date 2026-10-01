"""First-read summary for Razor (.cshtml / .razor) files.

Doctrine (2026-05-28 Empire directive): when an agent first reads a
Razor file, surface the structural context it would otherwise have
to discover manually — total line count, every <partial>/PartialAsync
reference, the resolved partial file path, the partial's line count,
and the invocation line(s) in the parent. Same context a human gets
from VS2022's "Go to Definition" + file-tree view, in one banner.

Why: Razor projects are partial-heavy by convention — a single page
can reach into 4-15 partials. Without this summary, an agent reading
a 2754-line view has no idea (a) how big the file is to plan its
reads, (b) what partials it pulls in, (c) where each partial lives.
The first-read banner means ONE round-trip surfaces what otherwise
takes 5-10 ai_find / read operations.

Apply: callers invoke ``razor_first_read_summary(project_root, file_
relpath)``. It returns a multi-line banner string ready to prepend
to the read output. Empty string when the file isn't a Razor file
or partial resolution fails — never raises, never blocks the read.
"""

from __future__ import annotations

import re
from pathlib import Path

# Partial name extraction patterns — same shape the canonical regex
# extractor uses, kept here so this module is self-contained.
_PARTIAL_TAG = re.compile(r'<partial\s+name="([^"]+)"', re.IGNORECASE)
_PARTIAL_ASYNC = re.compile(r'Html\.PartialAsync\(\s*"([^"]+)"')
_RENDER_PARTIAL = re.compile(r'@(?:await\s+)?Html\.RenderPartialAsync\(\s*"([^"]+)"')


def razor_first_read_summary(project_root: Path, rel_path: str) -> str:
    """Build a first-read banner for a Razor file.

    Format:
        [Razor] <relpath>  |  <total> lines  |  <n> partials
          -> <PartialName> (<lines>L)  invoked @ line <a>, <b>
          -> <PartialName> (UNRESOLVED — searched <paths>)
        ─────

    Returns "" if rel_path doesn't point to a .cshtml/.razor or the
    file can't be read.
    """
    suffix = Path(rel_path).suffix.lower()
    if suffix not in (".cshtml", ".razor"):
        return ""
    full = project_root / rel_path
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    total_lines = text.count("\n") + (0 if text.endswith("\n") else 1)

    # Collect every partial reference with its line numbers. Multiple
    # invocations of the same partial are merged into one entry.
    refs: dict[str, list[int]] = {}
    for ln, line in enumerate(text.splitlines(), start=1):
        for rx in (_PARTIAL_TAG, _PARTIAL_ASYNC, _RENDER_PARTIAL):
            for m in rx.finditer(line):
                name = m.group(1)
                refs.setdefault(name, []).append(ln)

    if not refs:
        # No partials — still surface the line count, that's useful on
        # its own when planning reads.
        return f"[Razor] {rel_path}  |  {total_lines} lines  |  0 partials\n─────"

    # Resolve each partial. Search order matches ASP.NET Razor's
    # documented partial discovery:
    #   (1) sibling of the calling view
    #   (2) /Pages/Shared/  (Razor Pages convention)
    #   (3) /Views/Shared/  (MVC convention)
    #   (4) any Areas-style /Pages/<Area>/Shared/
    # Both bare-name and underscore-prefixed forms (the AIDOCS
    # convention for partials) are tried.
    caller_dir = (project_root / rel_path).parent
    shared_dirs = _shared_dirs(project_root)

    lines: list[str] = [
        f"[Razor] {rel_path}  |  {total_lines} lines  |  {len(refs)} partial reference(s)",
    ]
    for name, positions in refs.items():
        resolved_path, resolved_lines, searched = _resolve_partial(
            name,
            caller_dir,
            shared_dirs,
            project_root,
        )
        positions_text = ", ".join(str(p) for p in positions)
        if resolved_path is not None:
            rel = resolved_path.relative_to(project_root).as_posix()
            lines.append(
                f"  -> {name} : {rel} ({resolved_lines}L)  invoked @ line {positions_text}",
            )
        else:
            searched_short = ", ".join(s.relative_to(project_root).as_posix() for s in searched[:3])
            more = f" + {len(searched) - 3} more" if len(searched) > 3 else ""
            lines.append(
                f"  -> {name} : UNRESOLVED  invoked @ line {positions_text}  "
                f"(searched: {searched_short}{more})",
            )
    lines.append("─────")
    return "\n".join(lines)


def _shared_dirs(project_root: Path) -> list[Path]:
    """ASP.NET partial shared-directory candidates anywhere in the tree.

    Looks for any directory literally named 'Shared' under Pages/ or
    Views/ — covers both Razor Pages + MVC + Areas layouts without
    hard-coding paths. Cheap: directory-only walk, no file reads.
    """
    found: list[Path] = []
    for marker in ("Pages", "Views"):
        for p in project_root.rglob(marker):
            if not p.is_dir():
                continue
            for shared in p.rglob("Shared"):
                if shared.is_dir():
                    found.append(shared)
    return found


def _resolve_partial(
    name: str,
    caller_dir: Path,
    shared_dirs: list[Path],
    project_root: Path,
) -> tuple[Path | None, int, list[Path]]:
    """Try every candidate path; return (resolved_path, lines, searched).

    ``name`` may be a bare partial name ('UserCard'), an underscore-
    prefixed name ('_UserCard'), or a project-relative path
    ('Shared/_UserCard.cshtml'). We normalize and try all conventional
    forms.
    """
    searched: list[Path] = []

    # Variants of the name to try.
    candidates: list[str] = []
    n_clean = name.strip()
    base = Path(n_clean).name
    candidates.append(n_clean)
    if not n_clean.endswith(".cshtml"):
        candidates.append(n_clean + ".cshtml")
    if not base.startswith("_"):
        underscore = (Path(n_clean).parent / ("_" + base)).as_posix()
        candidates.append(underscore)
        if not n_clean.endswith(".cshtml"):
            candidates.append(underscore + ".cshtml")

    # Resolution roots to try, in order.
    roots: list[Path] = [caller_dir]
    roots.extend(shared_dirs)
    # If the name looks absolute-ish (contains a slash), also try from
    # project root.
    if "/" in n_clean or "\\" in n_clean:
        roots.insert(1, project_root)

    for root in roots:
        for cand in candidates:
            full = (root / cand).resolve()
            searched.append(full)
            if full.is_file():
                try:
                    txt = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                n_lines = txt.count("\n") + (0 if txt.endswith("\n") else 1)
                return (full, n_lines, searched)
    return (None, 0, searched)
