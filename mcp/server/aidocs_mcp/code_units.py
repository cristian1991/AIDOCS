"""RFC-4 Phase 2 — code_index Unit-vending APIs.

Provides ``iter_units`` and ``get_unit_content`` over AIDOCS's existing
``code_outlines`` + ``code_files`` tables, producing Unit-shaped
records per RFC-4 §3.1.

Adopt by copying to ``aidocs_mcp/code_units.py``. Wire it via
``palace_hub_extension`` so PalaceService can call
``hub.code_index.iter_units(project_root)`` and
``hub.code_index.get_unit_content(unit_id)``.

Key design properties:

- ``unit_id`` is a stable SHA-256 of (project_uuid, normalized_path,
  kind, qualified_symbol). Stable across re-index cycles when the
  symbol's identity doesn't change.
- ``iter_units`` is a generator — large projects yield without
  loading every row in memory.
- ``get_unit_content`` honors protected_files + privacy invariants
  per RFC 003 §3.2: refused content is b"" with empty content_hash.
- ``line_end`` is the next outline's line - 1 within the same path
  (skipping rows nested inside the symbol, #478), or the file's
  total line_count.
- ``content_hash`` is computed on the bytes the caller would
  receive — so palace stale-tracking can detect content changes
  without re-running outline extraction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

# #755/#756: the ONE canonical connect. All three sites were
# `with sqlite3.connect ... as conn:` -- sqlite3's TRANSACTION context
# manager, which commits and NEVER closes the handle -- with no pragmas.
# The vendor only READS the code index (it is handed the db another
# component writes), so every site says read_only=True.
from ._sqlite_connect import connect as _canonical_connect

# RFC-4 §3.1 Unit + UnitContent types
from mempalace.conjoined_types import (
    BoundaryConfidence,
    PrivacyClass,
    Unit,
    UnitContent,
    UnitKind,
)

from .symbol_ranking import row_is_inside_scope

# ---------------------------------------------------------------------------
# Outline-kind mapping (AIDOCS → RFC-4)
# ---------------------------------------------------------------------------


# AIDOCS's outline_extractors emit kinds like 'function', 'class', etc.
# Map them to the RFC-4 UnitKind taxonomy. Unknown kinds fall through
# to 'outline_section'.
_KIND_MAP: dict[str, UnitKind] = {
    "function": "function",
    "fn": "function",
    "method": "method",
    "class": "class",
    "constant": "constant",
    "const": "constant",
    "module": "outline_section",
    "section": "md_section",
    "heading": "md_section",
}


def _aidocs_kind_to_unit_kind(raw: str) -> UnitKind:
    return _KIND_MAP.get((raw or "").lower(), "outline_section")


# ---------------------------------------------------------------------------
# CodeUnitVendor
# ---------------------------------------------------------------------------


class CodeUnitVendor:
    """Vends Unit and UnitContent over AIDOCS's code_index tables.

    Constructed once per project_root; reused across many calls.
    Thread-safe at the read level (each call opens its own
    sqlite connection).
    """

    def __init__(
        self,
        *,
        project_root: Path,
        index_db_path: Path,
        project_uuid: str,
        is_protected: Callable[[Path], bool] = lambda _p: False,
        get_privacy_class: Callable[[Path], PrivacyClass] = lambda _p: "internal",
    ):
        """Args:
        project_root: project root path (for relative path normalization).
        index_db_path: AIDOCS's ``.MEMORY/.index/aidocs.sqlite3``.
        project_uuid: stable project identity (from
            ``.MEMORY/project.uuid``).
        is_protected: callable that returns True if a given path
            is in protected_files_registry. Default: never protected.
        get_privacy_class: callable that returns the privacy class
            for a given path. Default: 'internal' for everything.

        """
        self.project_root = Path(project_root)
        self.index_db_path = Path(index_db_path)
        self.project_uuid = project_uuid
        self.is_protected = is_protected
        self.get_privacy_class = get_privacy_class

    # ------------------------------------------------------------------
    # Index version
    # ------------------------------------------------------------------

    def get_index_version(self) -> str:
        try:
            with _canonical_connect(
                str(self.index_db_path), read_only=True, row_factory=False
            ) as conn:
                row = conn.execute(
                    "SELECT value FROM index_meta WHERE key = 'code_index_version'",
                ).fetchone()
                return (row[0] if row else "") or ""
        except sqlite3.Error:
            return ""

    # ------------------------------------------------------------------
    # Unit ID computation
    # ------------------------------------------------------------------

    def compute_unit_id(
        self,
        *,
        source_file: str,
        kind: str,
        qualified_symbol: str,
    ) -> str:
        """Stable hash per RFC-4 §3.3.

        sha256(project_uuid + normalized_source_file + kind +
                qualified_symbol)[:32]
        """
        norm = source_file.replace("\\", "/")
        payload = f"{self.project_uuid}|{norm}|{kind}|{qualified_symbol}".encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    # ------------------------------------------------------------------
    # iter_units — generator over the project's outline + file index
    # ------------------------------------------------------------------

    def iter_units(self) -> Iterator[Unit]:
        """Yield one Unit per row in code_outlines, plus per-file fallback
        units for files that have no outline rows.

        Joins code_outlines to code_files for language/role context.
        line_end is the next NON-NESTED outline's line_number - 1
        within the same file (#478), or the file's line_count
        as a fallback when no next outline exists.
        """
        index_version = self.get_index_version()

        with _canonical_connect(
            str(self.index_db_path), read_only=True, row_factory=False
        ) as conn:
            # Ordered by (path, line_number); line_end is computed in
            # python below because "next outline - 1" must SKIP outline
            # rows nested inside the current symbol (#478, War S): nested
            # closures are indexed as first-class rows whose container
            # chain names the enclosing function, and a naive LEAD()
            # truncated the enclosing function's span at its first
            # nested def.
            rows = conn.execute(
                """
                SELECT
                    co.path,
                    co.symbol,
                    co.kind,
                    co.line_number,
                    co.container,
                    co.is_partial,
                    cf.language,
                    cf.role,
                    cf.line_count
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                ORDER BY co.path, co.line_number
                """,
            ).fetchall()

        for i, r in enumerate(rows):
            path = r[0]
            symbol = r[1]
            raw_kind = r[2]
            line_start = int(r[3] or 0)
            container = r[4] or ""
            is_partial = bool(r[5])
            language = r[6] or None
            role = r[7] or ""
            file_line_count = int(r[8] or 0)

            # Next outline in the SAME file that is NOT inside this
            # symbol's scope (container chain does not name it).
            next_line: int | None = None
            for j in range(i + 1, len(rows)):
                nxt = rows[j]
                if nxt[0] != path:
                    break
                nxt_container = str(nxt[4] or "")
                # #481: scope-CHAIN containment, not name membership (a member
                # named after its container — every C# constructor — otherwise
                # treats its siblings as nested body).
                if row_is_inside_scope(nxt_container, container, symbol):
                    continue  # nested inside this symbol — still its body
                next_line = int(nxt[3] or 0)
                break

            if next_line is not None:
                line_end = max(next_line - 1, line_start)
            else:
                line_end = max(file_line_count, line_start)

            qualified_symbol = f"{container}.{symbol}" if container else symbol
            unit_kind = _aidocs_kind_to_unit_kind(raw_kind)
            unit_id = self.compute_unit_id(
                source_file=path,
                kind=unit_kind,
                qualified_symbol=qualified_symbol,
            )

            parent_chain = tuple(container.split(".")) if container else ()
            role_tags = frozenset({role}) if role else frozenset()

            # Privacy class — resolved per file via the injected
            # callable. RFC 003 §10.1: privacy ratchet — never relax,
            # only stay or strengthen.
            try:
                privacy = self.get_privacy_class(self.project_root / path)
            except Exception:
                privacy = "internal"

            # Boundary confidence — code_outlines rows come from
            # AIDOCS's outline_extractors (regex / tree-sitter).
            # 'is_partial' signals less-confident parses.
            boundary_confidence: BoundaryConfidence = "heuristic" if is_partial else "structural"

            yield Unit(
                unit_id=unit_id,
                kind=unit_kind,
                source_file=path,
                line_start=line_start,
                line_end=line_end,
                language=language,
                role_tags=role_tags,
                parent_chain=parent_chain,
                content_hash="",  # filled by get_unit_content when bytes are read
                aidocs_index_version=index_version,
                privacy_class=privacy,
                boundary_confidence=boundary_confidence,
            )

    # ------------------------------------------------------------------
    # Per-file fallback iter (files with no outline rows)
    # ------------------------------------------------------------------

    def iter_file_fallback_units(self) -> Iterator[Unit]:
        """Yield one fallback_byte_window Unit per file that has no
        outline rows. Useful for files outline_extractors couldn't
        parse — they still get represented as a single Unit so the
        palace can drawer them under a coarse identity.
        """
        index_version = self.get_index_version()
        with _canonical_connect(
            str(self.index_db_path), read_only=True, row_factory=False
        ) as conn:
            rows = conn.execute(
                """
                SELECT cf.path, cf.language, cf.role, cf.line_count
                FROM code_files cf
                LEFT JOIN code_outlines co ON co.path = cf.path
                WHERE co.path IS NULL
                """,
            ).fetchall()

        for r in rows:
            path = r[0]
            language = r[1] or None
            role = r[2] or ""
            line_count = int(r[3] or 0)

            unit_id = self.compute_unit_id(
                source_file=path,
                kind="fallback_byte_window",
                qualified_symbol="<file>",
            )
            try:
                privacy = self.get_privacy_class(self.project_root / path)
            except Exception:
                privacy = "internal"

            yield Unit(
                unit_id=unit_id,
                kind="fallback_byte_window",
                source_file=path,
                line_start=1,
                line_end=max(line_count, 1),
                language=language,
                role_tags=frozenset({role}) if role else frozenset(),
                parent_chain=(),
                content_hash="",
                aidocs_index_version=index_version,
                privacy_class=privacy,
                boundary_confidence="fallback",
            )

    # ------------------------------------------------------------------
    # get_unit_content
    # ------------------------------------------------------------------

    def get_unit_content(self, unit_id: str) -> UnitContent | None:
        """Resolve unit_id → bytes via the gate cascade.

        Per RFC 003 §3.2 invariant:
          - allowed: content + non-empty content_hash
          - protected_refused / privacy_refused / etc: b"" + ""

        Returns None if the unit_id doesn't resolve to any known
        outline or fallback row.
        """
        # Reverse lookup: scan iter_units + iter_file_fallback_units
        # for matching unit_id. In production this would be a direct
        # SQL lookup with the unit_id materialized in a column; for
        # Phase 2 reference impl, the scan is fine.
        target: Unit | None = None
        for u in self.iter_units():
            if u.unit_id == unit_id:
                target = u
                break
        if target is None:
            for u in self.iter_file_fallback_units():
                if u.unit_id == unit_id:
                    target = u
                    break
        if target is None:
            return None

        source_path = self.project_root / target.source_file

        # Gate 1: protected file
        try:
            if self.is_protected(source_path):
                return UnitContent(
                    unit_id=unit_id,
                    content=b"",
                    content_hash="",
                    unit_export_status="protected_refused",
                    boundary_confidence=target.boundary_confidence,
                    refusal_reason=f"path {target.source_file} is in protected_file_registry",
                )
        except Exception as exc:
            return UnitContent(
                unit_id=unit_id,
                content=b"",
                content_hash="",
                unit_export_status="protected_refused",
                boundary_confidence=target.boundary_confidence,
                refusal_reason=f"protected_file probe raised: {exc}",
            )

        # Gate 2: privacy class
        if target.privacy_class in {"sensitive", "secrets_possible"}:
            return UnitContent(
                unit_id=unit_id,
                content=b"",
                content_hash="",
                unit_export_status="privacy_refused",
                boundary_confidence=target.boundary_confidence,
                refusal_reason=(f"unit privacy_class={target.privacy_class} above embedding floor"),
            )

        # Allowed — read bytes
        try:
            content = self._read_bytes(
                source_path,
                line_start=target.line_start,
                line_end=target.line_end,
            )
        except OSError as exc:
            return UnitContent(
                unit_id=unit_id,
                content=b"",
                content_hash="",
                unit_export_status="protected_refused",
                boundary_confidence=target.boundary_confidence,
                refusal_reason=f"could not read source: {exc}",
            )

        content_hash = hashlib.sha256(content).hexdigest()
        return UnitContent(
            unit_id=unit_id,
            content=content,
            content_hash=content_hash,
            unit_export_status="allowed",
            boundary_confidence=target.boundary_confidence,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_bytes(
        self,
        path: Path,
        *,
        line_start: int,
        line_end: int,
    ) -> bytes:
        """Read bytes for a line range. line_start and line_end are
        1-indexed and inclusive.

        Reads the file in text mode for line-counting precision,
        then re-encodes as UTF-8 bytes. For binary files (rare in a
        code index), the caller would have set privacy_class differently
        or the file wouldn't have appeared in code_outlines anyway.
        """
        if not path.is_file():
            raise OSError(f"not a file: {path}")

        text_lines: list[str] = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if i < line_start:
                    continue
                if i > line_end:
                    break
                text_lines.append(line)
        return "".join(text_lines).encode("utf-8")
