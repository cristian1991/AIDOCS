from __future__ import annotations

import re
from pathlib import Path

from .types import MemoryWriteResult

# ── Sovereign memory paths ──
# Files owned by the conductor / co-conductor under Conductor Doctrine #1.
# These are written ONLY via direct edit tools by the seat-holder, NEVER
# via the public memory_capture API. Paths are relative to .MEMORY/.
# co-conductor.md inscribed 2026-05-03 at Empire's behest as the library
# completed and the second chamber began.
_SOVEREIGN_MEMORY_PATHS: frozenset[str] = frozenset(
    {
        "skills/head-conductor.md",
        "skills/co-conductor.md",
    },
)


# ── Durability rubric ──
# Accepted kinds. Each maps to ONE canonical target file. Permissive buckets
# like "domain"/"project"/"feedback" from earlier versions accepted anything
# and caused months of miscaptures (plans, changelogs, bug reports, tool
# feedback dumped into .MEMORY/domains/). Strict enum forces the agent to
# pick a specific durability category before writing.
_ACCEPTED_KINDS: dict[str, tuple[str, str]] = {
    # kind → (folder, default filename)
    "invariant": ("system", "invariants.md"),
    "workflow-rule": ("rules", "workflow.md"),
    "preference": ("rules", "communication.md"),
    "infrastructure": ("config", "infrastructure.md"),
    "caveat": ("system", "caveats.md"),
    "related-project": ("related-projects", "FIXES_BY_OTHER_AGENTS.md"),
}

# Alias table — maps both back-compat kinds AND common semantic synonyms
# onto the strict enum. Agents reach for obvious-sounding labels
# ("security", "policy", "gotcha", "note") that aren't in the enum; the
# alias layer accepts them silently and routes to the right canonical
# file. New synonyms belong here, NOT in _ACCEPTED_KINDS — the accepted
# set must stay small so the canonical files don't drift.
_KIND_ALIASES: dict[str, str] = {
    # Back-compat with older schema names.
    "rule": "workflow-rule",
    "system": "invariant",
    "config": "infrastructure",
    "user": "preference",
    "reference": "preference",
    "related_project": "related-projects",
    # Semantic synonyms — obvious labels agents reach for.
    "security": "invariant",  # security rules ARE invariants
    "policy": "invariant",  # policies ARE invariants
    "rules": "workflow-rule",  # pluralized workflow-rule
    "workflow": "workflow-rule",
    "process": "workflow-rule",
    "procedure": "workflow-rule",
    "ops": "workflow-rule",
    "runbook": "workflow-rule",
    "guideline": "workflow-rule",
    "standard": "workflow-rule",
    "convention": "workflow-rule",
    "style": "preference",
    "tone": "preference",
    "taste": "preference",
    "ui": "preference",
    "communication": "preference",
    "note": "caveat",
    "gotcha": "caveat",
    "trap": "caveat",
    "warning": "caveat",
    "quirk": "caveat",
    "pitfall": "caveat",
    "architecture": "invariant",
    "contract": "invariant",
    "boundary": "invariant",
    "schema": "invariant",
    "constraint": "invariant",
    "always": "invariant",
    "must": "invariant",
    "never": "invariant",
    "deploy": "infrastructure",
    "deployment": "infrastructure",
    "env": "infrastructure",
    "environment": "infrastructure",
    "credentials": "infrastructure",
    "secrets-location": "infrastructure",
    "related": "related-projects",
    "cross-project": "related-projects",
    "other-project": "related-projects",
}

# Kinds that must be rejected outright — they describe non-durable content
# and there is always a better destination. The error message names that
# destination so the agent can route correctly.
_REJECTED_KINDS: dict[str, str] = {
    "plan": "Plans belong in `.MEMORY/plans/<name>.md` or use `plan_create_from_spec`, not memory.",
    "phase": "Phase-tracking belongs in `.MEMORY/plans/` (use `plan_create_from_spec`), not memory.",
    "roadmap": "Roadmaps belong in `.MEMORY/plans/roadmap.md` (use `plan_create_from_spec`), not memory.",
    "bug": "Bug reports belong in the issue tracker. If there is a durable invariant, capture it with kind='invariant' or 'caveat'.",
    "log": "Activity logs belong in `.MEMORY/archive/` or the session journal, not memory.",
    "feedback": "Tool/agent feedback belongs in `.MEMORY/roadmap-feedback/` or `archive/` (extract only actionable invariants as kind='invariant').",
    "changelog": "Changelog entries belong in `.MEMORY/archive/` via `/archive`, not memory.",
    "status": "Project status belongs in the active session or `.MEMORY/plans/`, not memory.",
    "snapshot": "Code/architecture snapshots are not memory — the code indexer has them. Capture the RULES that govern the snapshot as kind='invariant', or write the snapshot to `.MEMORY/archive/` if retention is needed.",
    "inventory": "File/service/endpoint inventories are not memory — the code indexer has them. Capture the RULES as kind='invariant', or write the inventory to `.MEMORY/archive/` if retention is needed.",
    "exploration": "Investigation output is not memory — write it to `.MEMORY/sessions/<id>/agents/` (or `.MEMORY/archive/`), not here.",
    # Legacy permissive kinds from the old schema that accepted anything.
    # Block them so the agent is forced to pick a strict kind.
    "domain": "`domain` was a permissive bucket in the old schema. Pick a strict kind: invariant, workflow-rule, preference, infrastructure, or caveat.",
    "project": "`project` was a permissive bucket in the old schema. Pick: invariant (for a rule), infrastructure (for config), caveat (for a gotcha), or route to `.MEMORY/plans/` for phase tracking.",
}

# Reserved filenames — these must only appear in their canonical folder.
# Prevents the `domains/plans.md`, `rules/roadmaps.md`, `domains/bugs.md`
# class of miscaptures.
_RESERVED_FILENAMES: dict[str, str] = {
    "plans.md": "`plans.md` only lives under `.MEMORY/plans/`. Use `plan_create_from_spec` or write directly there.",
    "plan.md": "`plan.md` only lives under `.MEMORY/plans/` or session folders.",
    "roadmap.md": "`roadmap.md` only lives under `.MEMORY/roadmaps/`.",
    "roadmaps.md": "`roadmaps.md` only lives under `.MEMORY/roadmaps/`.",
    "bugs.md": "`bugs.md` does not belong in memory — use the issue tracker.",
    "issues.md": "`issues.md` does not belong in memory — use the issue tracker.",
    "changelog.md": "`changelog.md` only lives at `.MEMORY/CHANGELOG.md` (root) or `archive/`.",
}

# Content-shape detectors — fire even when the `kind` is valid. Plans and
# bug reports can sneak in under kind='invariant' if the author is careless.
_CONTENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^\s*#{1,3}\s*phase\s*\d+\b|\bphase\s*\d+\s*(done|complete|pending|in\s+progress|deferred)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        "Content contains phase-tracking markers. This is a plan, not durable memory. Write to `.MEMORY/plans/<name>.md` or use `plan_create_from_spec`.",
    ),
    (
        re.compile(
            r"^\s*(source|last verified|built from)\s*:\s*.*(agent exploration|reingest|live analysis of|full codebase)",
            re.IGNORECASE | re.MULTILINE,
        ),
        "Content has an agent-exploration/investigation header. This is scratch work, not durable memory. Write to `.MEMORY/sessions/<id>/agents/<name>.md`.",
    ),
    (
        re.compile(
            r"(priority\s*:\s*(critical|high)|#{1,3}\s*critical\b|root\s*cause\s*:.*\bfix\s+needed\s*:)",
            re.IGNORECASE | re.MULTILINE,
        ),
        "Content looks like a bug report with priority/root-cause/fix structure. Bug reports belong in the issue tracker. If there is a durable invariant, capture only that as kind='invariant' or 'caveat'.",
    ),
    (
        re.compile(
            r"\b(feedback\s*v\d+|round[-\s]?\d+\s+feedback|real[-\s]world\s+(tool\s+)?feedback|\brated?\s+\d{1,2}/10\b)",
            re.IGNORECASE,
        ),
        "Content looks like tool-feedback/rating log. Feedback logs belong in `.MEMORY/roadmap-feedback/` or `archive/`, not memory.",
    ),
]


def _suggest_kind(unknown: str) -> str | None:
    """Closest accepted or aliased kind for an unknown input, or None
    if no reasonable match. Uses difflib for edit-distance matching so
    typos ('caveet' → 'caveat') also get caught, not just semantic
    synonyms. Returns the final canonical kind, resolving aliases.
    """
    import difflib

    candidates = list(_ACCEPTED_KINDS.keys()) + list(_KIND_ALIASES.keys())
    matches = difflib.get_close_matches(unknown, candidates, n=1, cutoff=0.72)
    if not matches:
        return None
    m = matches[0]
    return _KIND_ALIASES.get(m, m)


class MemoryStore:
    """File-backed canonical memory reader/writer."""

    def memory_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY"

    def read_memory(
        self,
        project_root: Path,
        targets: list[str],
        *,
        include_inactive: bool = False,
        palace: object | None = None,
    ) -> dict[str, str]:
        """Read CANONICAL memory by target path — SQLite-only (no-scroll
        doctrine, 2026-06).

        The sqlite memory_index is the SINGLE source of truth. A SUPERSEDED or
        REMOVED row is not surfaced (``include_inactive=True`` lifts that for
        audit). An ACTIVE row with empty content is a degraded state — returns
        nothing + emits ``memory_index_empty_content_degraded``. A path with NO
        memory_index row returns nothing: there is NO runtime disk fallback and
        NO filesystem frontmatter read — loose .MEMORY/*.md is invisible at
        runtime; importing it is the explicit operator-only
        migrate_markdown_to_sqlite path.
        """
        # #202 canonical-flip slice: when a ``palace`` handle exposing
        # ``get_drawer_content(drawer_id=...)`` is passed, the DRAWER body is
        # preferred and the sqlite body becomes the fallback. Lifecycle
        # filtering (retired/superseded suppression) still happens on the
        # index BEFORE any drawer read — retirement is never weakened.
        import posixpath

        root = self.memory_root(project_root)
        root_resolved = root.resolve()
        result: dict[str, str] = {}
        try:
            from . import memory_sqlite_store as _msq
        except Exception:
            _msq = None  # type: ignore
        for target in targets:
            # Empire global LAW (#213 Lane 2) lives in the global store, not
            # .MEMORY. A `global:<law_id>` target reads from global_law_store
            # so a globally-surfaced rule is actually readable.
            if str(target).startswith("global:"):
                try:
                    from . import global_law_store as _gl

                    law = _gl.read_global_law(str(target)[len("global:") :])
                    if law and str(law.get("content") or "").strip():
                        result[target] = str(law["content"])
                except Exception:
                    pass
                continue
            # Normalize to a .MEMORY-relative posix path and REJECT
            # traversal / absolute escapes. Without this a target like
            # "../../etc/passwd" or "/.MEMORY/../../secret" could read or
            # probe outside .MEMORY on the disk-fallback path.
            rel = str(target).replace("\\", "/")
            for _pre in ("/.MEMORY/", ".MEMORY/"):
                if rel.startswith(_pre):
                    rel = rel[len(_pre) :]
                    break
            rel = rel.lstrip("/")
            if not rel:
                continue
            norm = posixpath.normpath(rel)
            if norm.startswith("../") or norm == ".." or norm.startswith("/"):
                continue  # traversal — never resolve it
            # Belt-and-suspenders: the resolved disk path must stay under
            # .MEMORY (catches symlink / edge normalizations too).
            try:
                (root / norm).resolve().relative_to(root_resolved)
            except ValueError:
                continue
            rel = norm
            if _msq is None:
                continue  # no canonical store available -> nothing (never disk)
            status = _msq.entry_status(project_root, rel)
            if status is None:
                # NOT indexed -> invisible at runtime. No disk fallback, no
                # frontmatter read. (Operator imports via migrate_markdown_to_sqlite.)
                continue
            if status != "active" and not include_inactive:
                continue  # deliberately retired -> suppressed
            entry = _msq.read_entry(project_root, rel, include_inactive=include_inactive)
            if entry is not None and entry.content:
                drawer_body = self._drawer_content(palace, entry)
                result[target] = drawer_body if drawer_body else entry.content
            elif status == "active":
                # Active row, empty canonical content: degraded state. Surface
                # nothing + forensic marker; never fall back to disk.
                self._emit_empty_content_degraded(project_root, rel)
        return result

    @staticmethod
    def _drawer_content(palace: object | None, entry: object) -> str:
        """#202 drawer-first hydration. Full drawer body when the palace
        exposes ``get_drawer_content(drawer_id=...)`` and has the drawer;
        "" on miss, missing API, or any error (caller falls back to the
        sqlite body — never weaker than sqlite-only).
        """
        if palace is None:
            return ""
        try:
            reader = getattr(palace, "get_drawer_content", None)
            drawer_id = str(getattr(entry, "drawer_id", "") or "")
            if reader is None or not drawer_id:
                return ""
            body = reader(drawer_id=drawer_id)
            return body if isinstance(body, str) and body else ""
        except Exception:
            return ""

    @staticmethod
    def _emit_empty_content_degraded(project_root: Path, rel: str) -> None:
        """Forensic audit when an ACTIVE memory_index row has empty content.
        Best-effort — never breaks the read path.
        """
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="memory_index_empty_content_degraded",
                source_kind="memory_read",
                capability_name="memory_read",
                action_kind="read",
                target_entity=rel[:300],
                status="degraded",
                payload={
                    "path": rel,
                    "note": "active memory_index row has empty content; "
                    "disk fallback suppressed (index is authoritative)",
                },
            )
        except Exception:
            pass

    def capture_memory(
        self,
        project_root: Path,
        kind: str,
        content: str,
        target_hint: str | None = None,
    ) -> MemoryWriteResult:
        """Persist a durable fact. Enforces the durability rubric: rejects
        non-durable kinds (plan/bug/log/feedback/snapshot) and content
        shapes (phase tracking, exploration dumps, bug reports with
        priorities, feedback logs).
        """
        resolved_kind = self._normalize_kind(kind)
        self._reject_hidden_unicode(content)
        self._reject_non_durable_content(content)
        self._reject_reserved_filename(target_hint)

        root = self.memory_root(project_root)
        target = self._resolve_target(root, resolved_kind, content, target_hint)
        # Sovereign guard (Conductor Doctrine #1): files owned by the
        # conductor/co-conductor are NEVER written via this API. The
        # seat-holder edits them directly. Defense-in-depth: check both
        # the resolved target AND the normalized target_hint, so any path
        # form (skills/X.md, .MEMORY/skills/X.md, /.MEMORY/skills/X.md)
        # is caught even if _resolve_target lands them somewhere else.
        try:
            rel_target = target.relative_to(root).as_posix().lower()
        except ValueError:
            rel_target = ""
        candidates: set[str] = {rel_target}
        if target_hint:
            norm = target_hint.replace("\\", "/").lstrip("/").lower()
            for prefix in (".memory/", "memory/"):
                norm = norm.removeprefix(prefix)
            candidates.add(norm)
        sovereign_lower = {p.lower() for p in _SOVEREIGN_MEMORY_PATHS}
        for cand in candidates:
            if cand and cand in sovereign_lower:
                raise ValueError(
                    f"target '{cand}' is sovereign per Conductor "
                    f"Doctrine #1 and cannot be written via "
                    f"memory_capture. The seat-holder edits sovereign "
                    f"files directly via ai_replace; "
                    f"this API surface is for non-sovereign memory.",
                )
        # No-file-layer doctrine (2026-05-21, "No loose scrolls"): memory
        # is canonical in sqlite ONLY. capture_memory NEVER creates or reads
        # a .MEMORY/*.md file. Prior bullets are read from the canonical
        # row, the merged content is written back to sqlite, and NO markdown
        # export is produced. If the sqlite write fails, fail closed — there
        # is no markdown fallback.
        import hashlib

        from . import memory_sqlite_store as _msq

        rel_path = target.relative_to(root).as_posix()
        prior = _msq.read_entry(project_root, rel_path)
        existing = prior.content if prior is not None else ""
        new_content = self._append_bullet(existing, content)
        checksum = hashlib.sha256(
            new_content.encode("utf-8"),
        ).hexdigest()
        title: str | None = None
        for line in new_content.splitlines():
            line_s = line.strip()
            if line_s.startswith("#"):
                title = line_s.lstrip("#").strip() or None
                break

        sqlite_ok = _msq.upsert_entry(
            project_root,
            path=rel_path,
            kind=resolved_kind,
            content=new_content,
            source="capture",
            status="active",
            title=title,
            checksum=checksum,
        )
        if not sqlite_ok:
            # Fail closed: sqlite is the SOLE source of truth — there is
            # no markdown fallback to leave the memory in.
            raise RuntimeError(
                f"memory_capture: canonical sqlite write failed for "
                f"{rel_path!r}. Refusing to persist memory — sqlite is the "
                f"sole source of truth (no markdown fallback). Check "
                f".MEMORY/.index/aidocs.sqlite3 is writable.",
            )

        # markdown_ok/markdown_error are retained on MemoryWriteResult for
        # caller compatibility but are now constant: no markdown is ever
        # written, so there is nothing to fail. (The vestigial export-lag
        # plumbing in server_code_tools/types is slated for removal in a
        # dedicated cleanup pass.)
        return MemoryWriteResult(
            target_file=target,
            content=content,
            sqlite_ok=True,
            markdown_ok=True,
            markdown_error=None,
            sqlite_checksum=checksum,
            consolidated=prior is not None,
        )

    def normalize_kind(self, kind: str) -> str:
        """Public alias-resolution for callers that must compare an incoming
        kind against STORED kinds before capture (e.g. the #144 neighbor-
        merge same-kind floor). Raises like capture_memory would on an
        unknown/rejected kind."""
        return self._normalize_kind(kind)

    def _normalize_kind(self, kind: str) -> str:
        """Apply alias table, then reject unknown/non-durable kinds."""
        key = (kind or "").strip().lower()
        if not key:
            raise ValueError(
                "kind is required. Accepted: " + ", ".join(sorted(_ACCEPTED_KINDS.keys())),
            )
        if key in _KIND_ALIASES:
            key = _KIND_ALIASES[key]
        if key in _REJECTED_KINDS:
            raise ValueError(
                f"kind='{kind}' is rejected: {_REJECTED_KINDS[key]} "
                f"Accepted kinds: {', '.join(sorted(_ACCEPTED_KINDS.keys()))}.",
            )
        if key not in _ACCEPTED_KINDS:
            suggestion = _suggest_kind(key)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            raise ValueError(
                f"Unknown kind '{kind}'.{hint} "
                f"Accepted: {', '.join(sorted(_ACCEPTED_KINDS.keys()))}. "
                f"Common synonyms (auto-aliased): security/policy/schema → invariant; "
                f"rule/workflow/process/runbook → workflow-rule; "
                f"note/gotcha/trap → caveat; "
                f"deploy/env/credentials → infrastructure; "
                f"style/tone → preference.",
            )
        return key

    def _reject_non_durable_content(self, content: str) -> None:
        if not content:
            return
        for pattern, message in _CONTENT_PATTERNS:
            if pattern.search(content):
                raise ValueError(message)

    def _reject_hidden_unicode(self, content: str) -> None:
        """Block hidden-Unicode payloads from entering durable memory.

        Memory is authority for future conversations — any tag-block / bidi
        / zero-width char here is a Pillar "Rules File Backdoor"-class
        attack attempt. We reject (not silently strip) so the operator sees
        the attempt. (red-team 2026-04-17 P1 finding)
        """
        from .unicode_safety import count_hidden_unicode

        n = count_hidden_unicode(content or "")
        if n > 0:
            raise ValueError(
                f"Memory content contains {n} hidden-unicode character(s) "
                f"(tag-block, bidi override, zero-width, or mid-content BOM). "
                f"Memory capture refused — durable memory must not carry "
                f"invisible instructions. Clean the source and retry.",
            )

    def _reject_reserved_filename(self, target_hint: str | None) -> None:
        if not target_hint:
            return
        normalized = target_hint.replace("\\", "/").strip().lower()
        basename = normalized.rsplit("/", 1)[-1]
        if basename in _RESERVED_FILENAMES:
            raise ValueError(
                f"target_hint '{target_hint}' uses reserved filename '{basename}'. "
                + _RESERVED_FILENAMES[basename],
            )

    def _resolve_target(self, root: Path, kind: str, content: str, target_hint: str | None) -> Path:
        # kind is pre-normalized to an accepted strict kind.
        folder_name, default_filename = _ACCEPTED_KINDS[kind]
        default_target = root / folder_name / default_filename

        # Back-compat mapping — existing tests expect some legacy filenames.
        mapping = {
            "invariant": root / "system" / "invariants.md",
            "workflow-rule": root / "rules" / "workflow.md",
            "preference": root / "rules" / "communication.md",
            "infrastructure": root / "config" / "infrastructure.md",
            "caveat": root / "system" / "caveats.md",
            "related-project": root / "related-projects" / "FIXES_BY_OTHER_AGENTS.md",
        }

        kind_folders = {
            "invariant": "system",
            "workflow-rule": "rules",
            "preference": "rules",
            "infrastructure": "config",
            "caveat": "system",
            "related-project": "related-projects",
        }

        if target_hint:
            # Normalize separators first, then strip any .MEMORY/ prefix
            # in any form (with or without leading slash). Previously only
            # "/.MEMORY/" (leading slash) was stripped, so target_hint like
            # ".MEMORY/skills/X.md" silently created .MEMORY/.MEMORY/skills/X.md.
            rel = target_hint.replace("\\", "/").lstrip("/")
            for prefix in (".MEMORY/", "MEMORY/"):
                if rel.startswith(prefix):
                    rel = rel[len(prefix) :]
                    break
            normalized = rel.strip()
            if "/" not in normalized:
                # Bare filename — route to the kind's canonical folder.
                folder = kind_folders.get(kind, folder_name)
                filename = normalized if normalized.endswith(".md") else f"{normalized}.md"
                return root / folder / filename
            candidate = Path(normalized)
            if candidate.suffix == "":
                candidate = candidate.with_suffix(".md")
            return root / candidate

        # No hint: route to the kind's canonical target.
        return mapping.get(kind, default_target)

    _memory_routes_cache: list[dict[str, object]] | None = None


    def _append_bullet(self, existing: str, content: str) -> str:
        normalized = existing.rstrip()
        bullet = f"- {content.strip()}"
        if not normalized:
            return bullet + "\n"
        return normalized + "\n" + bullet + "\n"
