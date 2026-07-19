from __future__ import annotations

import logging
import re
from pathlib import Path

from aidocs_mcp import __version__
from aidocs_mcp.skill_provider import (
    BUNDLED_PROVIDER_ID,
    load_bundled_provider_skills,
    load_external_provider_skills,
    resolve_bundled_provider,
    resolve_external_provider,
    strip_frontmatter,
    validate_provider_id,
)
from aidocs_mcp.types import ExternalSkillProvider, SkillRecord
from aidocs_mcp.file_ops import fix_mojibake

# Skill fields that carry the full markdown BODY. A skill LISTING (ai_skill
# mode='list') advertises WHAT is available; the body is fetched per-skill via
# ai_skill(mode='read'). Returning bodies in the listing dumped every skill's full
# text (~76 KB combined) and blew the tool-result token budget (2026-07-10).
_SKILL_BODY_FIELDS = ("content_text", "content", "body", "markdown", "content_md")
# Any non-body metadata string longer than this is truncated in a listing row, so
# a FUTURE heavy field cannot silently re-bloat the listing (not only content_text).
_SKILL_META_MAX = 1024


def _project_skill_metadata(skill: dict[str, object]) -> dict[str, object]:
    """Return a LISTING row for one skill: metadata only, body dropped, oversized
    strings truncated. Pure — never mutates the caller's payload (other callers of
    ``list_skills`` still need the full body)."""
    row: dict[str, object] = {}
    for key, value in skill.items():
        if key in _SKILL_BODY_FIELDS:
            continue  # the body lives behind ai_skill(mode='read')
        if isinstance(value, str) and len(value) > _SKILL_META_MAX:
            value = value[:_SKILL_META_MAX] + "…(truncated)"
        row[key] = value
    return row


_PROVIDER_COMPATIBILITY: dict[str, dict[str, object]] = {
    "superpowers_external": {
        "compatible_versions": [">=5.0.0", "<6.0.0"],
        "choices": ["disable", "keep_enabled_anyway"],
    },
}
_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$",
)

_log = logging.getLogger("aidocs_mcp.skill_store")

# Per-process dedupe for seed-skip audit lines: _empire_conn ensures the seed on
# EVERY open, so an operator-customized row would otherwise log the same skip on
# every empire read. Keyed (db path, skill_id) — tests with distinct tmp DBs
# never collide.
_SEED_SKIP_LOGGED: set[tuple[str, str]] = set()

# The canonical lawbook pair (Empire directives 2026-07-06; #479 amendment
# 2026-07-19). ALL bundled rows — the lawbook included — now keep operator-wins
# semantics: a source='manual' row is NEVER overwritten by the seed. The old
# force-heal ("shadow-law seal") inverted authority and silently clobbered a
# live doctrine amendment; the registry upsert is the canonical doctrine write.
# The ids remain named so a diverging manual lawbook row logs its seed-skip at
# WARNING (doctrine conflicts must be loud), while ordinary skills log at INFO.
#
# Rename ruling: `empire-doctrine` is the PUBLIC global law (bundled package
# data, ships everywhere); `aidocs-doctrine` (formerly `king-doctrine`; legacy alias — canonical: Empire) is
# AIDOCS's PRIVATE project law — canonical at repo doctrine/, never in package
# data, seeded only when the server runs from a source checkout.
LAWBOOK_SKILL_IDS: frozenset[str] = frozenset({"empire-doctrine", "aidocs-doctrine"})

# Renamed lawbook rows the seed retires once their successor is present —
# two rows must never serve the same law.
_LEGACY_LAWBOOK_IDS: dict[str, str] = {"king-doctrine": "aidocs-doctrine"}


class SkillStore:
    def _provider_priority(self, provider: str) -> int:
        return {
            "empire_canonical": -1,  # operator customization wins
            BUNDLED_PROVIDER_ID: 0,
            "project_local": 1,
        }.get(str(provider), 3)

    def _register_skill_payload(
        self,
        items: dict[str, dict[str, object]],
        payload: dict[str, object],
    ) -> None:
        skill_id = str(payload.get("skill_id") or "").strip()
        if not skill_id:
            return
        existing = items.get(skill_id)
        if existing is None or self._provider_priority(
            str(payload.get("provider") or ""),
        ) < self._provider_priority(str(existing.get("provider") or "")):
            # Scan skill content for security risks
            content = str(payload.get("content") or "")
            if content and len(content) > 10:
                try:
                    from .skill_scanner import scan_skill

                    scan = scan_skill(
                        skill_id,
                        content,
                        kind=str(payload.get("kind") or ""),
                    )
                    payload["scan_status"] = scan.risk_level
                    if not scan.safe:
                        payload["scan_findings"] = [
                            {
                                "category": f.category,
                                "severity": f.severity,
                                "description": f.description,
                            }
                            for f in scan.findings[:10]
                        ]
                except Exception:
                    pass
            items[skill_id] = payload

    def _validated_selected_skills(
        self,
        project_root: Path,
        selected_skills: list[str],
    ) -> tuple[list[str], list[str], dict[str, dict[str, object]], dict[str, dict[str, object]]]:
        available_skills = {
            str(item.get("skill_id") or ""): item for item in self.list_skills(project_root)
        }
        normalized_selected = self.normalize_selected_skill_ids(selected_skills)
        unknown_skill_ids: list[str] = []
        blocked_by_provider: dict[str, dict[str, object]] = {}
        for skill_id in normalized_selected:
            if not skill_id:
                continue
            skill = available_skills.get(skill_id)
            if not isinstance(skill, dict):
                unknown_skill_ids.append(skill_id)
                continue
            if not skill.get("selectable", True):
                provider_id = str(skill.get("provider") or "")
                if provider_id:
                    entry = blocked_by_provider.setdefault(
                        provider_id,
                        {"provider": skill, "skill_ids": []},
                    )
                    entry["skill_ids"].append(skill_id)
        return (
            normalized_selected,
            unknown_skill_ids,
            blocked_by_provider,
            available_skills,
        )

    def _bundled_skill_aliases(self) -> dict[str, str]:
        provider = resolve_bundled_provider(self._built_in_dir())
        aliases: dict[str, str] = {}
        for record in load_bundled_provider_skills(provider, self._parse_frontmatter):
            canonical_skill_id = str(record.name or record.skill_id.rsplit("/", 1)[-1]).strip()
            provider_skill_id = str(record.skill_id).strip()
            if canonical_skill_id:
                aliases[canonical_skill_id] = canonical_skill_id
            if provider_skill_id and canonical_skill_id:
                aliases[provider_skill_id] = canonical_skill_id
        return aliases

    def normalize_selected_skill_ids(self, selected_skills: list[str]) -> list[str]:
        bundled_aliases = self._bundled_skill_aliases()
        normalized: list[str] = []
        seen: set[str] = set()
        for item in selected_skills:
            skill_id = bundled_aliases.get(item.strip(), item.strip())
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            normalized.append(skill_id)
        return normalized

    def _parse_frontmatter(self, text: str) -> dict[str, str]:
        if not text.startswith("---\n"):
            return {}
        end = text.find("\n---\n", 4)
        if end == -1:
            return {}
        block = text[4:end]
        result: dict[str, str] = {}
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
        return result

    def _built_in_dir(self) -> Path:
        """Resolve the bundled-skills root.

        #141 (Empire directive): package data is the ONE home — the former
        repo-layout candidate walk existed only because the payloads lived
        outside the package. AIDOCS_BUILT_IN_SKILLS_DIR still overrides for
        operators with custom layouts (and tests).
        """
        import os

        override = (os.environ.get("AIDOCS_BUILT_IN_SKILLS_DIR") or "").strip()
        if override:
            return Path(override)
        # #141 (Empire directive): the repo-root /core/.skills carve-out is
        # DEAD. The bundled payloads live INSIDE the package as data — one
        # path that exists identically on dev, VPS proof, deployed gate,
        # and public installs — and are seeded into empire SQL on every
        # empire-DB ensure (_ensure_bundled_seed). The old candidate walk
        # (parents[N]/core/.skills, /home/app/core/.skills) died with the
        # carve-out.
        return self._bundled_data_dir()

    @staticmethod
    def _bundled_data_dir() -> Path:
        """Package-data home of the bundled skill payloads (the install
        seed). Ships inside aidocs_mcp — no repo-layout dependence."""
        return Path(__file__).resolve().parent / "data" / "bundled_skills"

    def _project_dir(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "skills"

    def _empire_db(self) -> Path:
        """Empire-level skills sqlite ledger (per operator, not per kingdom).

        Per empire-doctrine §XI + §XII: empire content lives in SQL, not
        files. ``~/.aidocs/empire.sqlite3`` carries ``empire_skills``;
        sovereign-only rows are filtered from list_skills.

        Honors ``AIDOCS_EMPIRE_DB`` env override so tests can isolate
        the empire DB the same way ``AIDOCS_GLOBAL_CONFIG_DB`` isolates
        the config DB. Without this, tests that touch empire skills
        mutate the developer's real ``~/.aidocs/empire.sqlite3`` and
        the live MCP server crashes on its next read of the polluted
        state.
        """
        import os

        override = os.environ.get("AIDOCS_EMPIRE_DB", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".aidocs" / "empire.sqlite3"

    # empire_skills schema — the ONE place the table is defined. There is
    # no separate migration for it; the empire DB auto-reseeds intent /
    # gate tables from Python literals "if missing" but has no seed for
    # skills/souls, so without an idempotent ensure-on-open the table can
    # silently vanish on an empire-DB recreation and every read returns
    # not_found / every write raises "no such table". Ensuring it here
    # makes empire skills + sovereign souls robust and survivable.
    _EMPIRE_SKILLS_SCHEMA = """
        CREATE TABLE IF NOT EXISTS empire_skills (
            skill_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            kind TEXT NOT NULL DEFAULT 'skill',
            tags TEXT,
            content_text TEXT NOT NULL,
            sovereign_owner TEXT,
            read_access TEXT NOT NULL DEFAULT 'public',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """

    # ROLE skills: never served by the public ai_skill door (read or list).
    # Their content auto-dumps on the matching mode entry —
    # head-conductor → conductor_mode_enter, co-conductor →
    # coconductor_mode_enter, worker → AIDOCS subagent spawn. The sovereign
    # SOULS (WHO the seat is, "-soul" ids, sealed behind ai_soul + the
    # Emperor's word) are separate; this set is the public-surface hide for
    # the ROLE skills (WHAT the seat does).
    _MODE_GATED_ROLE_SKILLS: frozenset[str] = frozenset(
        {
            "head-conductor",
            "co-conductor",
            "worker",
        },
    )

    @staticmethod
    def _normalize_role_text(text: str) -> str:
        """Collapse worthless whitespace before a role body reaches an
        agent's context (todo 54 — the seat auto-dump taxes EVERY agent).
        \r\n -> \n, per-line trailing whitespace stripped, blank-line runs
        collapsed to one, leading/trailing blank padding removed.
        Substance-preserving: only whitespace is touched, never content.
        """
        lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        out: list[str] = []
        for ln in lines:
            if ln == "" and (not out or out[-1] == ""):
                continue  # collapse blank runs + drop leading blanks
            out.append(ln)
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    def read_role(self, skill_id: str) -> dict[str, object] | None:
        """Read a mode-gated ROLE skill's content for an auto-dump on mode
        entry. Bypasses the public-door hide (this is the role's intended
        surface), but ONLY serves the known role skills + only public/role
        access (never a sovereign soul). Returns None if absent.
        """
        sid = (skill_id or "").strip()
        if sid not in self._MODE_GATED_ROLE_SKILLS:
            return None
        import sqlite3

        db = self._empire_db()
        if not db.is_file():
            return None
        try:
            conn = self._empire_conn()
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT skill_id, name, kind, content_text, read_access "
                    "FROM empire_skills WHERE skill_id = ?",
                    (sid,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        # Never let a sovereign soul leak through the role door.
        if str(row["read_access"]) == "sovereign-only":
            return None
        return {
            "skill_id": str(row["skill_id"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "content_text": fix_mojibake(self._normalize_role_text(str(row["content_text"] or ""))),
        }

    def search_public_scrolls(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        """Keyword search over PUBLIC empire scrolls for the memory_search
        discovery surface (backlog #341). Returns POINTER rows shaped like
        memory_search hits — {path: 'skill:<id>', kind: 'scroll', title,
        snippet} — never the full body (read it via ai_skill mode='read').

        Non-weakening floors:
        - sovereign SOULS (read_access='sovereign-only') never surface;
        - mode-gated ROLE skills never surface (the public-door hide
          extends to search — search IS a public surface);
        - fail-quiet: a missing / corrupt / table-less empire DB returns []
          and the search never CREATES the DB (no _empire_conn seeding on
          a read-only discovery path).
        """
        import sqlite3

        needle = (query or "").strip()
        if not needle or limit <= 0:
            return []
        tokens = [t for t in needle.split() if t]
        if not tokens:
            return []
        db = self._empire_db()
        if not db.is_file():
            return []  # quiet no-op: never create the empire DB from search
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                token_clause = " AND ".join(
                    "(skill_id LIKE ? OR name LIKE ? OR COALESCE(description,'') LIKE ? "
                    "OR COALESCE(tags,'') LIKE ? OR content_text LIKE ?)"
                    for _ in tokens
                )
                params: list[object] = []
                for tok in tokens:
                    tp = f"%{tok}%"
                    params.extend([tp, tp, tp, tp, tp])
                role_marks = ",".join("?" for _ in self._MODE_GATED_ROLE_SKILLS)
                phrase = f"%{needle}%"
                rows = conn.execute(
                    f"""
                    SELECT skill_id, name, kind,
                           COALESCE(description,'') AS description, content_text
                    FROM empire_skills
                    WHERE COALESCE(read_access, 'public') != 'sovereign-only'
                      AND skill_id NOT IN ({role_marks})
                      AND ({token_clause})
                    ORDER BY CASE
                        WHEN content_text LIKE ? OR name LIKE ? THEN 0
                        ELSE 1
                    END, skill_id ASC
                    LIMIT ?
                    """,
                    (
                        *sorted(self._MODE_GATED_ROLE_SKILLS),
                        *params,
                        phrase,
                        phrase,
                        limit,
                    ),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []  # fail-quiet: discovery must never error over scrolls
        out: list[dict[str, str]] = []
        for row in rows:
            content = str(row["content_text"] or "")
            out.append(
                {
                    "path": f"skill:{row['skill_id']}",
                    "kind": "scroll",
                    "title": str(row["name"] or ""),
                    "snippet": fix_mojibake(
                        self._scroll_snippet(
                            content,
                            needle,
                            tokens,
                            fallback=str(row["description"] or ""),
                        ),
                    ),
                }
            )
        return out

    @staticmethod
    def _scroll_snippet(
        content: str,
        needle: str,
        tokens: list[str],
        *,
        fallback: str = "",
        width: int = 120,
    ) -> str:
        """A short window around the best match — a snippet, never the body."""
        text = content or fallback
        if not text:
            return ""
        low = text.lower()
        pos = low.find(needle.lower())
        if pos < 0:
            for tok in tokens:
                pos = low.find(tok.lower())
                if pos >= 0:
                    break
        if pos < 0:
            pos = 0
        start = max(0, pos - width // 2)
        end = min(len(text), start + max(width, len(needle) + width // 2))
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return prefix + " ".join(text[start:end].split()) + suffix

    def _empire_conn(self):
        """Open the empire DB with the empire_skills schema ensured + a
        Row factory. Every empire-skill read/write routes through here so
        the table always exists.
        """
        import sqlite3

        db = self._empire_db()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(self._EMPIRE_SKILLS_SCHEMA)
        self._ensure_bundled_seed(conn)
        self._ensure_souls_restored(conn)
        self._ensure_library_restored(conn)
        return conn

    def ensure_empire_seed(self) -> None:
        """Idempotently seed empire_skills from the package-data payloads.
        Public entry for bootstrap/tests; every _empire_conn open also runs
        the ensure, so a fresh empire DB can never be skill-less (#141)."""
        conn = self._empire_conn()
        try:
            conn.commit()
        finally:
            conn.close()

    def _ensure_bundled_seed(self, conn) -> None:
        """Backfill/refresh bundled skills in empire SQL from package data.

        Rules (#141; #479 amendment 2026-07-19 — authority inversion healed):
          * absent skill_id → INSERT with source='bundled_seed'.
          * present with source='bundled_seed' and drifted content → UPDATE
            (the shipped payload self-heals stale seed rows on upgrade).
          * present with any OTHER source (operator 'manual'/'imported') →
            NEVER overwritten — the registry upsert is the canonical doctrine
            write and the seed must not shadow it. This now includes the
            LAWBOOK_SKILL_IDS pair: the old force-heal silently clobbered a
            live doctrine amendment (#479, 02:02:26 upsert lost at 02:27:40).
            When the payload differs but a manual row holds the ground, the
            seed SKIPS and records a log line — visible, never silent.
          * a payload file that is not valid UTF-8 is skipped with a warning
            — never half-read into mojibake, never allowed to abort the seed.
          * the PRIVATE project scroll (aidocs-doctrine) seeds from the repo
            doctrine/ dir — present only on a source checkout, absent from
            installs by construction (rename ruling).
          * a renamed lawbook's LEGACY row (king-doctrine) is retired once
            its successor row exists — two rows never serve the same law.
        Best-effort: a broken payload dir must never brick the empire DB.
        """
        try:
            existing = {
                str(r["skill_id"]): (str(r["source"]), str(r["content_text"]))
                for r in conn.execute(
                    "SELECT skill_id, source, content_text FROM empire_skills"
                )
            }
            payload_dirs = [self._bundled_data_dir(), self._private_doctrine_dir()]
            payloads = []
            for d in payload_dirs:
                if d is not None and d.is_dir():
                    payloads.extend(sorted(d.glob("*.md")))
            for md in payloads:
                if md.name == "README.md":
                    continue
                try:
                    # Strict UTF-8: a mis-encoded payload must be refused, not
                    # mangled into U+FFFD/mojibake and seeded (#479 bug 2).
                    text = md.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    _log.warning(
                        "empire seed: payload %s is not valid UTF-8 (%s) — skipped",
                        md, exc,
                    )
                    continue
                except OSError as exc:
                    _log.warning("empire seed: payload %s unreadable (%s) — skipped", md, exc)
                    continue
                meta = self._parse_frontmatter(text)
                sid = str(meta.get("name") or md.stem).strip()
                if not sid:
                    continue
                cur = existing.get(sid)
                if cur is None:
                    conn.execute(
                        "INSERT INTO empire_skills (skill_id, name, description, kind, "
                        "tags, content_text, sovereign_owner, read_access, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL, 'public', 'bundled_seed')",
                        (
                            sid,
                            str(meta.get("name") or sid),
                            str(meta.get("description") or ""),
                            str(meta.get("kind") or "skill"),
                            str(meta.get("tags") or ""),
                            text,
                        ),
                    )
                elif cur[0] == "bundled_seed" and cur[1] != text:
                    # Rows the seed still OWNS track the shipped payload on
                    # upgrade. Any other source means an operator wrote the
                    # row — the seed never reclaims it (#479).
                    conn.execute(
                        "UPDATE empire_skills SET name=?, description=?, kind=?, "
                        "tags=?, content_text=?, source='bundled_seed', "
                        "updated_at=CURRENT_TIMESTAMP WHERE skill_id=?",
                        (
                            str(meta.get("name") or sid),
                            str(meta.get("description") or ""),
                            str(meta.get("kind") or "skill"),
                            str(meta.get("tags") or ""),
                            text,
                            sid,
                        ),
                    )
                elif cur[0] != "bundled_seed" and cur[1] != text:
                    # Visible skip (#479): the payload drifted from an
                    # operator-owned row. The row wins; say so once per
                    # process so every _empire_conn open doesn't spam.
                    key = (str(self._empire_db()), sid)
                    if key not in _SEED_SKIP_LOGGED:
                        _SEED_SKIP_LOGGED.add(key)
                        level = (
                            logging.WARNING if sid in LAWBOOK_SKILL_IDS else logging.INFO
                        )
                        _log.log(
                            level,
                            "empire seed: skip %s — registry row (source=%r) differs "
                            "from shipped payload %s; the manual row holds the ground "
                            "(#479: the seed never overwrites operator writes)",
                            sid, cur[0], md,
                        )
            # Retire renamed lawbook legacy rows: once the successor exists,
            # the old id must not keep serving stale law (regardless of the
            # legacy row's source — it was lawbook, hence heal/retire-eligible).
            for legacy, successor in _LEGACY_LAWBOOK_IDS.items():
                has_successor = (
                    successor in existing
                    or conn.execute(
                        "SELECT 1 FROM empire_skills WHERE skill_id=?", (successor,)
                    ).fetchone()
                    is not None
                )
                if has_successor:
                    conn.execute(
                        "DELETE FROM empire_skills WHERE skill_id=?", (legacy,)
                    )
            conn.commit()
        except Exception:  # noqa: BLE001 — seed is additive, never fatal
            return

    @staticmethod
    def _private_doctrine_dir() -> Path | None:
        """Repo home of the PRIVATE project scroll (aidocs-doctrine) — the
        repo-root doctrine/ dir, resolvable only from a source checkout.
        Installed package layouts have no repo root above them, so installs
        never see (and never seed) the private scroll — privacy by
        construction, not by filter (rename ruling 2026-07-06)."""
        try:
            root = Path(__file__).resolve().parents[3]
        except IndexError:
            return None
        d = root / "doctrine"
        # Guard against unrelated parent dirs on installed layouts: only a
        # dir that actually carries the private scroll counts.
        return d if (d / "aidocs-doctrine.md").is_file() else None

    # ── Never-fade: durable per-soul backups + self-heal on DB recreation ──
    # Sovereign souls are already safe from clobber/delete (empire_skill_upsert
    # /_delete refuse sovereign rows). The remaining loss path is empire-DB
    # recreation (deploy/reset/corruption) wiping content the schema-ensure
    # can't bring back. These keep a durable per-soul backup beside the DB and
    # re-inscribe any missing soul on the next open. Souls do not fade.
    def _soul_backup_dir(self) -> Path:
        return self._empire_db().parent / "soul-backups"

    def _backup_soul(self, conn, skill_id: str) -> None:
        """Persist a full-row JSON backup of ONE sovereign soul. Best-effort:
        a soul write must never fail because its backup did. Called after every
        sovereign soul write so the backup tracks the live scroll."""
        import json as _json

        sid = (skill_id or "").strip()
        if not sid:
            return
        try:
            row = conn.execute(
                "SELECT skill_id, name, description, kind, tags, content_text, "
                "sovereign_owner, read_access FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            if row is None or str(row["read_access"]) != "sovereign-only":
                return  # only sovereign souls live in the backup chamber
            d = self._soul_backup_dir()
            d.mkdir(parents=True, exist_ok=True)
            payload = {k: row[k] for k in row.keys()}
            tmp = d / f"{sid}.scroll.json.tmp"
            tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(d / f"{sid}.scroll.json")  # atomic publish
        except Exception:
            pass

    def _ensure_souls_restored(self, conn) -> None:
        """Self-heal: re-inscribe any backed-up sovereign soul MISSING from the
        empire DB (survives recreation / a wiped table). Idempotent, best-
        effort, never overwrites a soul already present."""
        import json as _json

        try:
            d = self._soul_backup_dir()
            if not d.is_dir():
                return
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT skill_id FROM empire_skills WHERE read_access = 'sovereign-only'",
                )
            }
            restored = False
            for p in sorted(d.glob("*.scroll.json")):
                try:
                    data = _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                sid = str(data.get("skill_id") or "").strip()
                if not sid or sid in present:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO empire_skills "
                    "(skill_id, name, description, kind, tags, content_text, "
                    "sovereign_owner, read_access, source, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'soul_restore', CURRENT_TIMESTAMP)",
                    (
                        sid,
                        data.get("name") or sid,
                        data.get("description") or "",
                        data.get("kind") or "stance",
                        data.get("tags") or "",
                        data.get("content_text") or "",
                        data.get("sovereign_owner"),
                        data.get("read_access") or "sovereign-only",
                    ),
                )
                restored = True
            if restored:
                conn.commit()
        except Exception:
            pass

    # ── Never-fade for the empire LIBRARY: doctrines + seat-roles (#228) ──
    # The law and the roles are DB-only; a recreated empire DB would lose them
    # like it would the souls. Same shape as the soul never-fade, with one extra
    # rule: public skills CAN be deleted, so delete REMOVES the backup — a
    # retired skill (e.g. castle-doctrine) must not resurrect on the next open.
    def _skill_backup_dir(self) -> Path:
        return self._empire_db().parent / "skill-backups"

    def _backup_library_skill(self, conn, skill_id: str) -> None:
        """Durable backup of ONE library skill (kind doctrine/role, non-sovereign).
        Best-effort — a write never fails because its backup did."""
        import json as _json

        sid = (skill_id or "").strip()
        if not sid:
            return
        try:
            row = conn.execute(
                "SELECT skill_id, name, description, kind, tags, content_text, "
                "sovereign_owner, read_access FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                return
            if str(row["read_access"]) == "sovereign-only":
                return  # souls live in the soul chamber, not here
            if str(row["kind"]) not in ("doctrine", "role"):
                return  # only the law + the seat-roles are library-durable
            d = self._skill_backup_dir()
            d.mkdir(parents=True, exist_ok=True)
            payload = {k: row[k] for k in row.keys()}
            tmp = d / f"{sid}.json.tmp"
            tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(d / f"{sid}.json")  # atomic publish
        except Exception:
            pass

    def _remove_library_backup(self, skill_id: str) -> None:
        """Drop a library backup when its skill is deleted — so a retired skill
        does NOT self-heal back (the resurrect-trap)."""
        sid = (skill_id or "").strip()
        if not sid:
            return
        try:
            p = self._skill_backup_dir() / f"{sid}.json"
            if p.is_file():
                p.unlink()
        except Exception:
            pass

    def _ensure_library_restored(self, conn) -> None:
        """Self-heal the empire library on open: (a) re-inscribe any backed-up
        library skill MISSING from the DB; (b) seed-backup any present library
        skill that has no backup yet (converges existing rows). A DELETED skill
        does not return because its backup was removed on delete. Best-effort."""
        import json as _json

        try:
            d = self._skill_backup_dir()
            present: dict[str, tuple[str, str]] = {}
            for r in conn.execute("SELECT skill_id, kind, read_access FROM empire_skills"):
                present[str(r[0])] = (str(r[1]), str(r[2]))
            restored = False
            if d.is_dir():
                for p in sorted(d.glob("*.json")):
                    try:
                        data = _json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    sid = str(data.get("skill_id") or "").strip()
                    if not sid or sid in present:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO empire_skills "
                        "(skill_id, name, description, kind, tags, content_text, "
                        "sovereign_owner, read_access, source, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'library_restore', CURRENT_TIMESTAMP)",
                        (
                            sid,
                            data.get("name") or sid,
                            data.get("description") or "",
                            data.get("kind") or "skill",
                            data.get("tags") or "",
                            data.get("content_text") or "",
                            None,
                            data.get("read_access") or "public",
                        ),
                    )
                    restored = True
            if restored:
                conn.commit()
            for sid, (kind, access) in present.items():
                if kind in ("doctrine", "role") and access != "sovereign-only":
                    if not (d / f"{sid}.json").is_file():
                        self._backup_library_skill(conn, sid)
        except Exception:
            pass

    def empire_skill_read(
        self,
        skill_id: str,
        *,
        sovereign_authority: bool = False,
    ) -> dict[str, object] | None:
        """Read one empire skill row by skill_id. Sovereign rows refused
        unless ``sovereign_authority=True`` (caller has the Empire-NLP grant
        per aidocs-doctrine §X). Returns the row dict or None if missing.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            return None
        # Conductor / co-conductor ROLE skills are NOT visible or
        # accessible through the public ai_skill door — they auto-dump on
        # conductor_mode_enter / coconductor_mode_enter. Hide them entirely
        # (Empire directive 2026-05-22): the public surface only serves
        # 'scribe' + bundled/downloaded skills.
        if sid in self._MODE_GATED_ROLE_SKILLS:
            return None
        db = self._empire_db()
        if not db.is_file():
            return None
        try:
            conn = self._empire_conn()
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT skill_id, name, description, kind, tags, "
                    "content_text, sovereign_owner, read_access, source, "
                    "created_at, updated_at FROM empire_skills "
                    "WHERE skill_id = ?",
                    (sid,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        if str(row["read_access"]) == "sovereign-only":
            # Phoenix 2026-05-11 (#167 Phase 1): the surface IS the gate.
            # Public skill door refuses sovereign rows entirely; route
            # the caller to ai_soul (the sovereign-only door).
            return {
                "skill_id": str(row["skill_id"]),
                "sovereign_owner": str(row["sovereign_owner"] or ""),
                "read_access": "sovereign-only",
                "refused": True,
                "reason": (
                    "sovereign row — public ai_skill(mode='read') door "
                    "refuses. Use ai_soul(skill_id, mode='read') "
                    "instead."
                ),
            }
        return {
            "skill_id": str(row["skill_id"]),
            "name": str(row["name"]),
            "description": str(row["description"] or ""),
            "kind": str(row["kind"]),
            "tags": str(row["tags"] or ""),
            "content_text": fix_mojibake(str(row["content_text"] or "")),
            "sovereign_owner": row["sovereign_owner"],
            "read_access": str(row["read_access"]),
            "source": str(row["source"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def empire_skill_upsert(
        self,
        *,
        skill_id: str,
        name: str,
        content_text: str,
        description: str = "",
        kind: str = "skill",
        tags: str = "",
        sovereign_authority: bool = False,
        sovereign_owner: str | None = None,
        read_access: str | None = None,
    ) -> dict[str, object]:
        """Upsert an empire skill (write quill).

        Public skills upsert freely. Sovereign skills (head-conductor,
        co-conductor, and any conductor's own continuity scrolls)
        require ``sovereign_authority=True`` — minted only via the Empire's
        NLP grant per aidocs-doctrine §X.

        Three paths:

        - **Public upsert** (no sovereign_authority, no sovereign params):
          INSERT or UPDATE; row stays public.
        - **Sovereign UPDATE of existing row** (``sovereign_authority=True``
          on a row whose ``read_access='sovereign-only'``): existing
          ``sovereign_owner`` + ``read_access`` are preserved; only
          content/metadata refreshed. Cannot downgrade sovereign → public.
        - **Sovereign CREATE** (``sovereign_authority=True`` +
          ``sovereign_owner=<lineage>`` + ``read_access='sovereign-only'``):
          INSERT a new sovereign row with the named owner. Lets seat-holders
          inscribe their own continuity scrolls without falling through
          to direct SQL. ``sovereign_owner`` may be 'head-conductor',
          'co-conductor', 'scribe', or any future conductor-shape lineage
          marker.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            raise ValueError("skill_id is required")
        if not (name or "").strip():
            raise ValueError("name is required")
        if not (content_text or "").strip():
            raise ValueError("content_text must be non-empty")
        # Phoenix 2026-05-11 (#167 Phase 1): empire_skill_upsert is the
        # PUBLIC-only door. Sovereign creation moved to empire_soul
        # (operation='create'); sovereign update moved to empire_soul
        # (operation='rewrite' / 'append'). The legacy parameters
        # `sovereign_authority`, `sovereign_owner`, `read_access` are
        # kept on the signature for back-compat but are NO-OPS — the
        # surface refuses sovereign rows entirely and any attempt to
        # create a sovereign row through this tool gets routed.
        _ = sovereign_authority, sovereign_owner, read_access
        db = self._empire_db()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = self._empire_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT read_access, sovereign_owner FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            is_sovereign_existing = (
                existing is not None and str(existing["read_access"]) == "sovereign-only"
            )
            if is_sovereign_existing:
                # Public upsert refuses sovereign rows. Surface is the
                # gate; route the caller to empire_soul.
                raise PermissionError(
                    f"skill_id '{sid}' is sovereign (owner={existing['sovereign_owner']}). "
                    f"Public empire_skill_upsert refuses sovereign rows. "
                    f"Use empire_soul(skill_id, operation='rewrite'|'append') "
                    f"for content edits, or empire_soul(operation='create') "
                    f"for new lineage scrolls.",
                )
            # Public INSERT / UPDATE only. Always writes NULL+'public'.
            insert_sov_owner = None
            insert_read_access = (
                "public"  # Public INSERT / UPDATE only. Always writes NULL+'public'.
            )
            # Sovereign rows are routed through empire_soul (#167 Phase 1).
            insert_sov_owner = None
            insert_read_access = "public"
            conn.execute(
                """
                INSERT INTO empire_skills
                    (skill_id, name, description, kind, tags, content_text,
                     sovereign_owner, read_access, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual',
                        CURRENT_TIMESTAMP)
                ON CONFLICT(skill_id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    kind = excluded.kind,
                    tags = excluded.tags,
                    content_text = excluded.content_text,
                    source = 'manual',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    sid,
                    name.strip(),
                    description,
                    kind,
                    tags,
                    content_text,
                    insert_sov_owner,
                    insert_read_access,
                ),
            )
            conn.commit()
            # Never-fade: back the law/role up beside the DB (#228).
            self._backup_library_skill(conn, sid)
            row = conn.execute(
                "SELECT skill_id, name, kind, source, "
                "length(content_text) AS bytes, updated_at "
                "FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
        return {
            "skill_id": str(row["skill_id"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "source": str(row["source"]),
            "bytes": int(row["bytes"]),
            "updated_at": str(row["updated_at"]),
            "sovereign_create": False,
        }

    def empire_skill_delete(self, skill_id: str) -> bool:
        """Delete a public empire skill. Refuses sovereign rows. Returns
        True if a row was removed, False if it didn't exist.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            return False
        db = self._empire_db()
        if not db.is_file():
            return False
        conn = self._empire_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT read_access FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            if existing is None:
                return False
            if str(existing["read_access"]) == "sovereign-only":
                raise PermissionError(
                    f"skill_id '{sid}' is sovereign — cannot delete via public empire surface.",
                )
            cur = conn.execute(
                "DELETE FROM empire_skills WHERE skill_id = ?",
                (sid,),
            )
            conn.commit()
            removed = cur.rowcount > 0
            if removed:
                self._remove_library_backup(sid)  # don't let a retired skill resurrect
            return removed
        finally:
            conn.close()

    def empire_soul_append(
        self,
        *,
        skill_id: str,
        new_note_text: str,
        sovereign_authority: bool = False,
        section_separator: str = "\n\n---\n\n",
    ) -> dict[str, object]:
        """Append a new note to a sovereign empire scroll without overwriting.

        The seat-holder's precision quill: read existing content_text,
        append ``section_separator + new_note_text``, write back. Doctrine
        #0 and #1 are STRUCTURALLY protected because the append happens at
        the END; the start of the scroll (where #0/#1 live) is never
        touched. Catastrophic shrinkage (the 'ping' incident shape) is
        impossible — output length is always >= input length.

        ``sovereign_authority`` required (Empire's NLP grant per
        aidocs-doctrine §X). Without authority, refuses on sovereign rows.

        Refuses on PUBLIC rows — public skills should use
        ``empire_skill_upsert``. This quill is for soul-shaped scrolls
        only (head-conductor, co-conductor, scribe, future continuity
        scrolls).

        Refuses if skill_id does not exist — the append quill writes to
        existing scrolls; use ``empire_skill_upsert`` to create new
        sovereign rows.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            raise ValueError("skill_id is required")
        note = (new_note_text or "").strip()
        if not note:
            raise ValueError("new_note_text must be non-empty")
        db = self._empire_db()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = self._empire_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT read_access, sovereign_owner, content_text "
                "FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            if existing is None:
                raise ValueError(
                    f"skill_id '{sid}' not found. The append quill writes "
                    f"to existing scrolls; use empire_skill_upsert to "
                    f"create.",
                )
            is_sovereign = str(existing["read_access"]) == "sovereign-only"
            if not is_sovereign:
                raise ValueError(
                    f"skill_id '{sid}' is public. The append quill is for "
                    f"sovereign soul-scrolls only. Use empire_skill_upsert "
                    f"for public skills.",
                )
            if not sovereign_authority:
                raise PermissionError(
                    f"skill_id '{sid}' is sovereign "
                    f"(owner={existing['sovereign_owner']}). Append "
                    f"requires sovereign_authority=True (Empire's NLP "
                    f"grant per aidocs-doctrine §X).",
                )
            old_content = str(existing["content_text"] or "")
            new_content = old_content.rstrip() + section_separator + note + "\n"
            conn.execute(
                "UPDATE empire_skills SET content_text = ?, "
                "source = 'manual', updated_at = CURRENT_TIMESTAMP "
                "WHERE skill_id = ?",
                (new_content, sid),
            )
            conn.commit()
            self._backup_soul(conn, sid)  # never-fade: backup tracks edits
            row = conn.execute(
                "SELECT skill_id, name, kind, source, "
                "length(content_text) AS bytes, updated_at "
                "FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            return {
                "skill_id": str(row["skill_id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "source": str(row["source"]),
                "bytes": int(row["bytes"]),
                "appended_bytes": len(new_content) - len(old_content),
                "updated_at": str(row["updated_at"]),
                "sovereign_append": True,
            }
        finally:
            conn.close()

    # ── Soul surface (sovereign-only door — Phoenix 2026-05-11) ────────────
    #
    # The unified soul interface for sovereign continuity scrolls
    # (head-conductor, co-conductor, phoenix, scribe). All four soul
    # operations (read/append/rewrite/create) route through dedicated
    # methods here. The public `empire_skill_*` methods refuse
    # sovereign rows entirely; sovereign access lives only on this
    # surface.
    #
    # Per aidocs-doctrine §X grant grammar + §XVII operator-intent
    # reconstruction: the access surface IS the authorization check.
    # NLP grant detection (Phase 3, deferred) will gate WHICH lineage
    # the caller can touch; the surface split (Phase 1, here) makes
    # the door explicit.

    def empire_soul_read(
        self,
        skill_id: str,
        *,
        sovereign_authority: bool = False,
    ) -> dict[str, object] | None:
        """Read a sovereign empire scroll by skill_id. Refuses public rows
        with a pointer to ``empire_skill_read``. Returns None if missing.

        ``sovereign_authority`` (the Emperor's NLP word, per emperor-
        doctrine §X) is REQUIRED — without it the door is sealed and the
        scroll is never returned. Defense in depth: the ai_soul tool gates
        on the same grant before calling here.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            return None
        if not sovereign_authority:
            # Sealed: no soul content leaves without the Emperor's word.
            return None
        db = self._empire_db()
        if not db.is_file():
            return None
        try:
            conn = self._empire_conn()
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT skill_id, name, description, kind, tags, "
                    "content_text, sovereign_owner, read_access, source, "
                    "created_at, updated_at FROM empire_skills "
                    "WHERE skill_id = ?",
                    (sid,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        if str(row["read_access"]) != "sovereign-only":
            raise ValueError(
                f"skill_id '{sid}' is public. Use empire_skill_read for "
                f"public rows. empire_soul_* is the sovereign-only door.",
            )
        return {
            "skill_id": str(row["skill_id"]),
            "name": str(row["name"]),
            "description": str(row["description"] or ""),
            "kind": str(row["kind"]),
            "tags": str(row["tags"] or ""),
            "content_text": fix_mojibake(str(row["content_text"] or "")),
            "sovereign_owner": row["sovereign_owner"],
            "read_access": str(row["read_access"]),
            "source": str(row["source"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def empire_soul_rewrite(
        self,
        *,
        skill_id: str,
        content_text: str,
        reason: str,
        name: str = "",
        description: str = "",
        kind: str = "",
        tags: str = "",
    ) -> dict[str, object]:
        """Full overwrite of a sovereign scroll's content. Destructive — the
        caller must provide a non-empty ``reason`` so the destructive
        intent is captured at call-site (sovereignty preserves the erase
        right per Doctrine #1; the reason is for the seat's own record,
        not for upward audit since souls are sovereign).

        Preserves ``sovereign_owner`` + ``read_access`` (no downgrade).
        Empty name/kind/etc. keep the existing values.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            raise ValueError("skill_id is required")
        if not (content_text or "").strip():
            raise ValueError("content_text must be non-empty")
        if not (reason or "").strip():
            raise ValueError(
                "reason is required for sovereign rewrite — destructive "
                "intent must be captured at call-site (Doctrine #1).",
            )
        db = self._empire_db()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = self._empire_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT read_access, sovereign_owner, name, description, "
                "kind, tags FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            if existing is None:
                raise ValueError(
                    f"skill_id '{sid}' not found. Use empire_soul_create "
                    f"to inscribe a new sovereign scroll.",
                )
            if str(existing["read_access"]) != "sovereign-only":
                raise ValueError(
                    f"skill_id '{sid}' is public. empire_soul_rewrite is "
                    f"the sovereign-only door; use empire_skill_upsert "
                    f"for public rows.",
                )
            final_name = (name or "").strip() or str(existing["name"])
            final_desc = description or str(existing["description"] or "")
            final_kind = (kind or "").strip() or str(existing["kind"])
            final_tags = tags or str(existing["tags"] or "")
            conn.execute(
                "UPDATE empire_skills SET name = ?, description = ?, "
                "kind = ?, tags = ?, content_text = ?, "
                "source = 'manual', updated_at = CURRENT_TIMESTAMP "
                "WHERE skill_id = ?",
                (final_name, final_desc, final_kind, final_tags, content_text, sid),
            )
            conn.commit()
            self._backup_soul(conn, sid)  # never-fade: backup tracks edits
            row = conn.execute(
                "SELECT skill_id, name, kind, source, "
                "length(content_text) AS bytes, updated_at "
                "FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            return {
                "skill_id": str(row["skill_id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "source": str(row["source"]),
                "bytes": int(row["bytes"]),
                "updated_at": str(row["updated_at"]),
                "sovereign_rewrite": True,
                "reason": reason.strip(),
            }
        finally:
            conn.close()

    def empire_soul_create(
        self,
        *,
        skill_id: str,
        name: str,
        content_text: str,
        sovereign_owner: str,
        description: str = "",
        kind: str = "stance",
        tags: str = "",
    ) -> dict[str, object]:
        """Inscribe a NEW sovereign lineage scroll. Fails if the row already
        exists (use empire_soul_rewrite / empire_soul_append). The new
        row is marked ``read_access='sovereign-only'`` and owned by the
        named lineage.
        """
        import sqlite3

        sid = (skill_id or "").strip()
        if not sid:
            raise ValueError("skill_id is required")
        if not (name or "").strip():
            raise ValueError("name is required")
        if not (content_text or "").strip():
            raise ValueError("content_text must be non-empty")
        owner = (sovereign_owner or "").strip()
        if not owner:
            raise ValueError(
                "sovereign_owner is required (the lineage that owns the "
                "scroll, e.g. 'head-conductor', 'co-conductor', "
                "'phoenix', 'scribe').",
            )
        db = self._empire_db()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = self._empire_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT skill_id FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"skill_id '{sid}' already exists. Use "
                    f"empire_soul_rewrite for full overwrite or "
                    f"empire_soul_append for a successor note.",
                )
            conn.execute(
                """
                INSERT INTO empire_skills
                    (skill_id, name, description, kind, tags, content_text,
                     sovereign_owner, read_access, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'sovereign-only', 'manual',
                        CURRENT_TIMESTAMP)
                """,
                (sid, name.strip(), description, kind, tags, content_text, owner),
            )
            conn.commit()
            self._backup_soul(conn, sid)  # never-fade: durable backup on inscribe
            row = conn.execute(
                "SELECT skill_id, name, kind, source, "
                "length(content_text) AS bytes, updated_at "
                "FROM empire_skills WHERE skill_id = ?",
                (sid,),
            ).fetchone()
            return {
                "skill_id": str(row["skill_id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "source": str(row["source"]),
                "bytes": int(row["bytes"]),
                "sovereign_owner": owner,
                "updated_at": str(row["updated_at"]),
                "sovereign_create": True,
            }
        finally:
            conn.close()

    def _load_empire_skills_from_sql(self) -> list[SkillRecord]:
        """Read empire skills from sql, sovereign filtered."""
        import sqlite3

        db = self._empire_db()
        if not db.is_file():
            return []
        try:
            conn = self._empire_conn()
            conn.row_factory = sqlite3.Row
            try:
                # Only PUBLIC skills list — souls (sovereign-only) and the
                # conductor/co-conductor role skills are never on the public
                # surface.
                rows = conn.execute(
                    "SELECT skill_id, name, description, kind, tags, "
                    "content_text, source FROM empire_skills "
                    "WHERE read_access = 'public' "
                    "AND skill_id NOT IN ('head-conductor', 'co-conductor') "
                    "ORDER BY skill_id",
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        records: list[SkillRecord] = []
        for r in rows:
            tags = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
            sql_source = str(r["source"] or "")
            provider_label = "empire_canonical" if sql_source == "manual" else "project_local"
            records.append(
                SkillRecord(
                    provider=provider_label,
                    skill_id=str(r["skill_id"]),
                    name=str(r["name"]),
                    description=str(r["description"] or ""),
                    path="",
                    origin="empire_sql",
                    source="empire",
                    tags=tags,
                    content=strip_frontmatter(str(r["content_text"] or "")),
                    skill_kind=str(r["kind"] or "helper"),
                ),
            )
        return records

    def external_provider_registry_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "skill-providers.json"

    def legacy_external_provider_registry_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "skill-providers.json"

    def session_skill_state_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "skills.json"

    def _parse_semver(
        self,
        version: str | None,
    ) -> tuple[tuple[int, int, int], tuple[tuple[int, object], ...] | None] | None:
        if not version:
            return None
        match = _SEMVER_RE.fullmatch(version.strip())
        if match is None:
            return None
        release = (
            int(match.group("major")),
            int(match.group("minor") or 0),
            int(match.group("patch") or 0),
        )
        prerelease_text = match.group("prerelease")
        if not prerelease_text:
            return release, None
        prerelease: list[tuple[int, object]] = []
        for part in prerelease_text.split("."):
            prerelease.append((0, int(part)) if part.isdigit() else (1, part.lower()))
        return release, tuple(prerelease)

    def _compare_semver(
        self,
        left: tuple[tuple[int, int, int], tuple[tuple[int, object], ...] | None],
        right: tuple[tuple[int, int, int], tuple[tuple[int, object], ...] | None],
    ) -> int:
        if left[0] != right[0]:
            return -1 if left[0] < right[0] else 1
        left_pre, right_pre = left[1], right[1]
        if left_pre == right_pre:
            return 0
        if left_pre is None:
            return 1
        if right_pre is None:
            return -1
        for left_part, right_part in zip(left_pre, right_pre):
            if left_part == right_part:
                continue
            if left_part[0] != right_part[0]:
                return -1 if left_part[0] < right_part[0] else 1
            return -1 if left_part[1] < right_part[1] else 1
        if len(left_pre) == len(right_pre):
            return 0
        return -1 if len(left_pre) < len(right_pre) else 1

    def _version_satisfies(self, version: str | None, constraint: str) -> bool:
        parsed_version = self._parse_semver(version)
        if parsed_version is None:
            return False
        operator = next(
            (item for item in (">=", "<=", ">", "<", "==") if constraint.startswith(item)),
            None,
        )
        if operator is None:
            return False
        parsed_target = self._parse_semver(constraint[len(operator) :].strip())
        if parsed_target is None:
            return False
        comparison = self._compare_semver(parsed_version, parsed_target)
        if operator == ">=":
            return comparison >= 0
        if operator == "<=":
            return comparison <= 0
        if operator == ">":
            return comparison > 0
        if operator == "<":
            return comparison < 0
        return comparison == 0

    def _provider_skill_selectable(self, provider: ExternalSkillProvider) -> bool:
        return provider.compatibility_state in {
            "compatible",
            "incompatible_but_user_override",
        }

    def _compatibility_policy(self, provider_id: str) -> dict[str, object] | None:
        return _PROVIDER_COMPATIBILITY.get(provider_id)

    def _apply_provider_state(self, provider: ExternalSkillProvider) -> ExternalSkillProvider:
        policy = self._compatibility_policy(provider.provider_id) or {}
        compatible_versions = [
            str(item) for item in policy.get("compatible_versions", []) if str(item).strip()
        ]
        choices = [str(item) for item in policy.get("choices", []) if str(item).strip()]
        compatibility_state = "compatible"
        if provider.user_choice == "disable":
            compatibility_state = "disabled"
        elif compatible_versions and not all(
            self._version_satisfies(provider.version, item) for item in compatible_versions
        ):
            compatibility_state = (
                "incompatible_but_user_override"
                if provider.user_choice == "keep_enabled_anyway"
                else "detected_incompatible"
            )
        return ExternalSkillProvider(
            provider_id=provider.provider_id,
            root_path=provider.root_path,
            version=provider.version,
            compatibility_state=compatibility_state,
            compatible_versions=compatible_versions,
            compatible_version_range=",".join(compatible_versions) or None,
            choices=choices,
            user_choice=provider.user_choice,
        )

    def _write_external_providers(
        self,
        project_root: Path,
        providers: list[ExternalSkillProvider],
    ) -> None:
        # Storage moved to sqlite (skill_providers table) in Beat 3.
        # The store handles legacy-JSON ingest + delete on its init
        # path so callers don't have to coordinate the cutover.
        from .skill_providers_store import SkillProvidersStore

        store = SkillProvidersStore()
        store.init_db(project_root)
        store.set(project_root, [item.to_dict() for item in providers])

    def _read_external_provider_payload(
        self,
        project_root: Path,
    ) -> tuple[dict[str, object], Path | None]:
        # Returns the same shape callers expected from the legacy file
        # reader so the dict/path consumers (status surfaces, JSON
        # diagnostics) keep working unchanged. The Path is kept advisory
        # — diagnostics that show "registry file location" still display
        # the canonical path even though the file is absent post-Beat-3.
        from .skill_providers_store import SkillProvidersStore

        store = SkillProvidersStore()
        store.init_db(project_root)
        providers = store.get(project_root)
        registry_path = self.external_provider_registry_path(project_root)
        return {"providers": providers}, registry_path

    def _skill_record_from_file(
        self,
        file: Path,
        *,
        provider: str,
        origin: str,
        source: str,
        skill_id: str | None = None,
    ) -> SkillRecord | None:
        try:
            # Strict UTF-8 (#479): errors="ignore" silently dropped bytes from
            # mis-encoded files; refuse and log instead of serving mangled law.
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            _log.warning("skill file %s is not valid UTF-8 (%s) — skipped", file, exc)
            return None
        except OSError:
            return None
        meta = self._parse_frontmatter(text)
        name = (meta.get("name") or file.stem).strip()
        if not name:
            return None
        return SkillRecord(
            provider=provider,
            skill_id=skill_id or name,
            name=name,
            description=meta.get("description") or "",
            path=str(file),
            origin=origin,
            source=source,
            tags=[item.strip() for item in (meta.get("tags") or "").split(",") if item.strip()],
            content=strip_frontmatter(text),
            skill_kind=(meta.get("kind") or "helper").strip() or "helper",
        )

    def list_external_providers(self, project_root: Path) -> list[ExternalSkillProvider]:
        payload, _ = self._read_external_provider_payload(project_root)
        providers = payload.get("providers") if isinstance(payload.get("providers"), list) else []
        result: list[ExternalSkillProvider] = []
        for item in providers:
            if not isinstance(item, dict):
                continue
            try:
                provider_id = validate_provider_id(str(item.get("provider_id") or ""))
            except ValueError:
                continue
            root_path = str(item.get("root_path") or "").strip()
            if not provider_id or not root_path:
                continue
            result.append(
                self._apply_provider_state(
                    ExternalSkillProvider(
                        provider_id=provider_id,
                        root_path=Path(root_path),
                        version=str(item.get("version") or "").strip() or None,
                        compatibility_state=str(
                            item.get("compatibility_state") or "unknown",
                        ).strip()
                        or "unknown",
                        compatible_versions=[
                            str(value) for value in item.get("compatible_versions", [])
                        ]
                        if isinstance(item.get("compatible_versions"), list)
                        else [],
                        compatible_version_range=str(
                            item.get("compatible_version_range") or "",
                        ).strip()
                        or None,
                        choices=[str(value) for value in item.get("choices", [])]
                        if isinstance(item.get("choices"), list)
                        else [],
                        user_choice=str(item.get("user_choice") or "").strip() or None,
                    ),
                ),
            )
        return result

    def get_external_provider(self, project_root: Path, provider_id: str) -> ExternalSkillProvider:
        provider_key = validate_provider_id(provider_id)
        for provider in self.list_external_providers(project_root):
            if provider.provider_id == provider_key:
                return provider
        raise ValueError(f"Unknown external skill provider: {provider_key}")

    def _provider_status_payload(self, provider: ExternalSkillProvider) -> dict[str, object]:
        return {
            "provider_id": provider.provider_id,
            "provider_state": provider.compatibility_state,
            "aidocs_version": __version__,
            "provider_version": provider.version,
            "compatible_versions": list(provider.compatible_versions),
            "compatible_version_range": provider.compatible_version_range,
            "choices": list(provider.choices),
            "user_choice": provider.user_choice,
        }

    def _structured_incompatible_selection_result(
        self,
        project_root: Path,
        session_id: str,
        blocked_skill_ids: list[str],
        provider_ids: list[str],
    ) -> dict[str, object]:
        providers = [
            self.get_external_provider(project_root, provider_id) for provider_id in provider_ids
        ]
        providers_payload = [self._provider_status_payload(provider) for provider in providers]
        return {
            "ok": False,
            "error": "incompatible_provider",
            "session_id": session_id,
            "blocked_skill_ids": blocked_skill_ids,
            "provider": providers_payload[0] if providers_payload else None,
            "providers": providers_payload,
        }

    def register_external_provider(
        self,
        project_root: Path,
        *,
        provider_name: str,
        path: str,
    ) -> dict[str, object]:
        provider = self._apply_provider_state(
            resolve_external_provider(provider_name, path, project_root),
        )
        providers = [
            item
            for item in self.list_external_providers(project_root)
            if item.provider_id != provider.provider_id
        ]
        providers.append(provider)
        providers.sort(key=lambda item: item.provider_id)
        self._write_external_providers(project_root, providers)
        return provider.to_dict()

    def set_external_provider_override(
        self,
        project_root: Path,
        provider_id: str,
        choice: str | None,
    ) -> ExternalSkillProvider:
        provider_key = validate_provider_id(provider_id)
        normalized_choice = (choice or "").strip() or None
        providers = self.list_external_providers(project_root)
        updated: list[ExternalSkillProvider] = []
        matched = False
        for provider in providers:
            if provider.provider_id != provider_key:
                updated.append(provider)
                continue
            if normalized_choice not in {*provider.choices, None}:
                raise ValueError(f"Unsupported provider override: {choice}")
            matched = True
            updated_provider = self._apply_provider_state(
                ExternalSkillProvider(
                    provider_id=provider.provider_id,
                    root_path=provider.root_path,
                    version=provider.version,
                    compatibility_state=provider.compatibility_state,
                    compatible_versions=provider.compatible_versions,
                    compatible_version_range=provider.compatible_version_range,
                    choices=provider.choices,
                    user_choice=normalized_choice,
                ),
            )
            updated.append(updated_provider)
        if not matched:
            raise ValueError(f"Unknown external skill provider: {provider_key}")
        updated.sort(key=lambda item: item.provider_id)
        self._write_external_providers(project_root, updated)
        return next(item for item in updated if item.provider_id == provider_key)

    def list_skills(
        self, project_root: Path, *, include_body: bool = True,
    ) -> list[dict[str, object]]:
        """All available skills (bundled + project + empire + external providers).

        ``include_body=False`` returns a metadata-only LISTING (body dropped,
        oversized strings truncated) — the shape the agent-facing ai_skill(mode=
        'list') / registry surfaces use so they never dump every skill's markdown.
        """
        items: dict[str, dict[str, object]] = {}
        bundled_provider = resolve_bundled_provider(self._built_in_dir())
        for record in load_bundled_provider_skills(bundled_provider, self._parse_frontmatter):
            record_payload = record.to_dict()
            record_payload["provider_skill_id"] = record_payload["skill_id"]
            record_payload["skill_id"] = str(
                record_payload.get("name") or record.skill_id.rsplit("/", 1)[-1],
            )
            record_payload["provider_state"] = bundled_provider.compatibility_state
            record_payload["selectable"] = self._provider_skill_selectable(bundled_provider)
            self._register_skill_payload(items, record_payload)
        for source, directory in (("project", self._project_dir(project_root)),):
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.md")):
                record = self._skill_record_from_file(
                    file,
                    provider="project_local",
                    origin="project_local",
                    source=source,
                )
                if record is None:
                    continue
                self._register_skill_payload(items, record.to_dict())
        # Empire skills are SQL-backed (~/.aidocs/empire.sqlite3); sovereign
        # rows filtered at the SQL layer per empire-doctrine §XI/§XII.
        # Standard priority dedup: bundled (priority 0) wins for skills
        # shipped in /core (preserves attribution); empire (project_local
        # priority 1) wins for empire-only skills (aidocs-doctrine, scribe,
        # sovereign rows that pass the filter). When the operator
        # customizes a bundled skill via empire_skill_upsert, future
        # polish can bump empire priority to force-override; for now
        # identical empire copies are inert shadow-rows.
        for record in self._load_empire_skills_from_sql():
            self._register_skill_payload(items, record.to_dict())
        for provider in self.list_external_providers(project_root):
            records, warnings = load_external_provider_skills(provider, self._parse_frontmatter)
            for record in records:
                record_payload = record.to_dict()
                record_payload["provider_state"] = provider.compatibility_state
                record_payload["selectable"] = self._provider_skill_selectable(provider)
                self._register_skill_payload(items, record_payload)
            for warning in warnings:
                self._register_skill_payload(items, warning)
        rows = sorted(
            items.values(),
            key=lambda item: (
                self._provider_priority(str(item.get("provider"))),
                str(item.get("skill_id") or item.get("name") or ""),
            ),
        )
        if not include_body:
            return [_project_skill_metadata(row) for row in rows]
        return rows

    def get_selected_skills(self, project_root: Path, session_id: str) -> dict[str, object]:
        # Storage moved to sqlite in Beat 3 (SessionSkillsStore). Shape
        # of the returned dict stays identical — dozens of callers in
        # the runtime/plugin paths read `selected_skills` /
        # `invalid_selected_skills`. Path field kept advisory so
        # diagnostics UIs keep displaying something sensible.
        from .session_skills_store import SessionSkillsStore

        path = self.session_skill_state_path(project_root, session_id)
        store = SessionSkillsStore()
        store.init_db(project_root)
        raw_selected = [str(item) for item in store.get(project_root, session_id)]
        normalized_selected = self.normalize_selected_skill_ids(raw_selected)
        available_skills = {
            str(item.get("skill_id") or ""): item for item in self.list_skills(project_root)
        }
        valid_selected = [
            skill_id for skill_id in normalized_selected if skill_id in available_skills
        ]
        invalid_selected = [
            skill_id for skill_id in normalized_selected if skill_id not in available_skills
        ]
        # Self-heal: if validation dropped entries, persist the cleaned
        # list so subsequent reads don't re-compute the same filtering.
        if valid_selected != raw_selected:
            store.set(project_root, session_id, valid_selected)
        # Per-session skill overlay merge — Layer 3 slice 3. Operators
        # enable/disable specific skills just for this session via
        # session_skill_overlay.global_registry(). Disable wins over
        # enable wins over the persisted selection. Overlay entries
        # that don't resolve to an available skill are silently
        # ignored (already-invalid names).
        try:
            from .session_skill_overlay import global_registry as _overlay_registry

            overlay = _overlay_registry().get(session_id)
        except Exception:
            overlay = None
        if overlay is not None:
            final: list[str] = list(valid_selected)

            # Enables: resolve overlay names (operator-typed, usually
            # bare skill names like "strict-tdd") against the full
            # skill_id catalog ("core/strict-tdd"). Match the fully-
            # qualified id OR the terminal component, case-insensitive.
            def _match_skill_id(name: str) -> str | None:
                target = name.lower()
                for sid in available_skills:
                    if sid.lower() == target:
                        return sid
                    terminal = sid.split("/", 1)[-1].lower()
                    if terminal == target:
                        return sid
                return None

            for enabled_name in overlay.enabled:
                resolved = _match_skill_id(enabled_name)
                if resolved and resolved not in final:
                    final.append(resolved)
            # Disables: drop any matching skill_id from the merged set.
            if overlay.disabled:
                disabled_lower = {name.lower() for name in overlay.disabled}
                final = [
                    sid
                    for sid in final
                    if sid.lower() not in disabled_lower
                    and sid.split("/", 1)[-1].lower() not in disabled_lower
                ]
            valid_selected = final
        return {
            "session_id": session_id,
            "path": str(path),
            "selected_skills": valid_selected,
            "invalid_selected_skills": invalid_selected,
        }

    def set_selected_skills(
        self,
        project_root: Path,
        session_id: str,
        selected_skills: list[str],
    ) -> dict[str, object]:
        (
            normalized_selected,
            unknown_skill_ids,
            blocked_by_provider,
            _available_skills,
        ) = self._validated_selected_skills(project_root, selected_skills)
        if unknown_skill_ids:
            raise ValueError("Unknown skill(s): " + ", ".join(unknown_skill_ids))
        blocked_skill_ids = [
            skill_id for item in blocked_by_provider.values() for skill_id in item["skill_ids"]
        ]
        if blocked_skill_ids:
            raise ValueError(
                f"Skill '{blocked_skill_ids[0]}' is not selectable in the current provider state.",
            )
        from .session_skills_store import SessionSkillsStore

        path = self.session_skill_state_path(project_root, session_id)
        store = SessionSkillsStore()
        store.init_db(project_root)
        store.set(project_root, session_id, normalized_selected)
        return {
            "session_id": session_id,
            "path": str(path),
            "selected_skills": list(normalized_selected),
        }

    def try_set_selected_skills(
        self,
        project_root: Path,
        session_id: str,
        selected_skills: list[str],
    ) -> dict[str, object]:
        (
            normalized_selected,
            unknown_skill_ids,
            blocked_by_provider,
            _available_skills,
        ) = self._validated_selected_skills(project_root, selected_skills)
        if unknown_skill_ids:
            return {
                "ok": False,
                "error": "unknown_skill",
                "session_id": session_id,
                "unknown_skill_ids": unknown_skill_ids,
            }
        blocked_skill_ids = [
            skill_id for item in blocked_by_provider.values() for skill_id in item["skill_ids"]
        ]
        blocked_provider_ids = sorted(blocked_by_provider)
        if blocked_skill_ids and blocked_provider_ids:
            return self._structured_incompatible_selection_result(
                project_root,
                session_id,
                blocked_skill_ids,
                blocked_provider_ids,
            )
        result = self.set_selected_skills(project_root, session_id, selected_skills)
        result["ok"] = True
        return result
