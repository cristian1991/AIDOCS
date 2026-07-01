from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .git_helpers import run_git_sync as _run_git_sync

# Fixed, zero-information commissioning marker written to
# ``.MEMORY/.aidocs/index.aidocs``. Its PATH existence is governance-bearing;
# its CONTENT is never read or parsed by AIDOCS (routing is in-code via SQL
# memory_routes; durable memory is canonical in the sqlite memory_index).
_COMMISSIONING_MARKER = (
    "# AIDOCS commissioning marker\n\n"
    "This file's PRESENCE marks an AIDOCS-managed project root; its path\n"
    "(`.MEMORY/.aidocs/index.aidocs`) is governance-bearing — commissioning,\n"
    "access-gate, and known-projects checks gate on its existence.\n\n"
    "Its CONTENT is intentionally inert and is never read or parsed by AIDOCS.\n"
    "Routing is in-code (SQL `memory_routes`); durable memory is canonical in\n"
    "the sqlite `memory_index` — query it with `memory_search` / `memory_read`.\n"
    "Session-entry instructions are announced by the AIDOCS MCP server on connect.\n\n"
    "<!-- AIDOCS-MANAGED-ABOVE: write project-specific instructions below this line -->\n"
)


# ── Process-wide runtime singleton ────────────────────────────────────
# file_ops and protected_file_ops have imported `get_runtime` from this
# module since #236 (2026-05-12) to reach the sqlite-backed grant stores
# — but the function NEVER EXISTED. The ImportError was swallowed by
# their `except Exception` nets, so the entire cross-process grant read
# path (protect / unprotect / protected-edit) silently fell back to
# module-level lists that cannot cross the hook↔server process boundary:
# ai_protect grants were unreadable BY CONSTRUCTION on every host
# (operator repro: DentalApp, 2026-06-11). create_server registers the
# real runtime here; library code lazily builds one when unregistered
# (hook subprocess case — the stores are sqlite-backed, so a parallel
# instance reads the same truth).
_RUNTIME_SINGLETON: Any = None


def set_runtime(runtime: Any) -> None:
    """Register the canonical RuntimeService (called by create_server)."""
    global _RUNTIME_SINGLETON
    _RUNTIME_SINGLETON = runtime


def get_runtime() -> Any:
    """Return the process-wide RuntimeService, lazily building one."""
    global _RUNTIME_SINGLETON
    if _RUNTIME_SINGLETON is None:
        from .mcp_server import _resolve_script_root, _resolve_templates_root
        from .runtime_service import RuntimeService
        from .service_hub import AidocsServiceHub

        _RUNTIME_SINGLETON = RuntimeService(
            AidocsServiceHub(
                templates_root=_resolve_templates_root(),
                script_root=_resolve_script_root(),
            ),
        )
    return _RUNTIME_SINGLETON


class RuntimeBootstrapService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    def _seed_factory_memory_into_index(self, root) -> dict:
        """Seed bundled default PROJECT MEMORY from the shipped SQLite-native
        seed artifact (``aidocs_mcp/seed/project_memory.sqlite3``) into the
        CANONICAL sqlite memory_index — REPAIR-IDEMPOTENTLY — and project
        deterministic memory_routes/keywords for any seed row that carries
        keywords. Reads NO factory Markdown and NEVER writes .MEMORY/*.md.
        SQLite-only doctrine (2026-06).

        The seed DB is generated at BUILD time from
        ``templates/memory/**/*.{md,aidocs}`` by
        ``core/scripts/build_project_memory_seed.py`` and shipped as package
        data. It is DISTINCT from ``seed/factory.sqlite3`` (global NLP/gate
        vocabulary) because it seeds a different live DB — the per-project
        ``memory_index``. The bootstrap only OPENS it (read-only SELECT), so the
        last bootstrap Markdown parse is gone. Active canonical rows are later
        projected into MemPalace drawers by the palace ingest path; this seeder
        does not touch MemPalace.

        Repair-idempotent: when the canonical row ALREADY exists, the content
        upsert is skipped (no churn) but the route/keywords are STILL ensured — so
        a project whose row survived but whose route was lost gets its discovery
        wiring repaired on the next bootstrap. Route projection failures are
        ACCOUNTED, not swallowed: returns
        ``{seeded, routes_ensured, route_failures: [path, ...]}``.
        """
        import sqlite3 as _sqlite3

        from . import memory_sqlite_store as _msq

        empty = {"seeded": 0, "routes_ensured": 0, "route_failures": []}
        seed_db = self._project_memory_seed_path()
        if not seed_db.is_file():
            return empty
        try:
            seed_conn = _sqlite3.connect(str(seed_db))
        except _sqlite3.Error:
            return empty
        try:
            seed_rows = seed_conn.execute(
                "SELECT path, kind, content, keywords, trigger, priority "
                "FROM project_memory_seed ORDER BY path",
            ).fetchall()
        except _sqlite3.Error:
            return empty
        finally:
            seed_conn.close()

        seeded = 0
        routes_ensured = 0
        route_failures: list[str] = []
        for rel, kind, content, keywords_csv, trigger, priority in seed_rows:
            # Content: upsert only when the canonical row is ABSENT (no churn
            # of operator-edited content on re-bootstrap).
            if _msq.entry_status(root, rel) is None:
                if _msq.upsert_entry(
                    root, path=rel, kind=kind, content=content, source="factory",
                ):
                    seeded += 1
            # Route/keywords: ALWAYS ensure (repair path) — independent of
            # whether the content row was just created or already existed.
            keywords = tuple(k for k in str(keywords_csv or "").split(",") if k)
            if not keywords:
                continue
            try:
                self.hub.index.upsert_memory_route(
                    root, rel, trigger=(trigger or "topic"),
                    priority=(priority or "normal"),
                    source="capture", keywords=keywords,
                )
                routes_ensured += 1
            except Exception:
                route_failures.append(rel)  # visible, not swallowed
        return {"seeded": seeded, "routes_ensured": routes_ensured,
                "route_failures": route_failures}

    @staticmethod
    def _project_memory_seed_path() -> Path:
        """Shipped SQLite-native project-memory seed DB (package data). Distinct
        from intent_tokens_store.factory_db_path() — that seeds the global
        empire vocabulary; this seeds the per-project memory_index."""
        return Path(__file__).resolve().parent / "seed" / "project_memory.sqlite3"

    def project_init(
        self,
        project_root: Path,
        init_git: bool = True,
        create_remote: bool = False,
        interpreter: str | None = None,
    ) -> dict[str, object]:
        root = project_root
        # Refuse initializing an AIDOCS project nested inside an existing
        # one (anti sub-project / confused-deputy: a fake sub-project must
        # not be able to mint itself under a real project's tree).
        # FAIL-CLOSED: if the ancestor safety check cannot run, refuse —
        # never proceed on an unverifiable check.
        try:
            from .project_authority import assert_no_commissioned_ancestor

            _guard = assert_no_commissioned_ancestor(root)
        except Exception as exc:
            raise ValueError(
                f"Refusing project_init: ancestor safety check failed "
                f"({exc!r}). Cannot prove {root} is not nested inside an "
                "existing AIDOCS project.",
            ) from exc
        if not _guard.get("ok"):
            raise ValueError(
                f"Refusing project_init: {root} is nested inside an "
                f"existing AIDOCS project at {_guard.get('ancestor')}. "
                "Initialize at the real project root, not a subdir.",
            )
        if not root.is_dir():
            root.mkdir(parents=True, exist_ok=True)

        created: list[str] = []
        skipped: list[str] = []

        templates_root = self.hub.sessions.templates_root
        aidocs_bundle_root = templates_root.parent
        memory_dest = root / ".MEMORY"
        aidocs_dest = memory_dest / ".aidocs"

        # SQLite-only doctrine (2026-06): create the .MEMORY dir skeleton (for the
        # sqlite index + .aidocs routing), but NEVER write .MEMORY/*.md defaults.
        # Bundled default memory content is seeded IDEMPOTENTLY into the canonical
        # memory_index (keyed by path PK -> re-bootstrap is a no-op) from the
        # shipped seed/project_memory.sqlite3 artifact — no factory Markdown is read.
        for d in [
            ".MEMORY/.aidocs",
            ".MEMORY/.index",
            ".MEMORY/sessions",
            ".MEMORY/archive/sessions",
        ]:
            (root / d).mkdir(parents=True, exist_ok=True)
        # Marker->sqlite migration: the AIDOCS-managed signal is a DELIBERATE
        # commission stamp INSIDE the sqlite index (index_meta['aidocs_commissioned']),
        # NOT the db file's existence (any store touch creates the file). Init the
        # code-index schema, then stamp — both SYNCHRONOUS at commission, so
        # is_aidocs_managed is TRUE the instant a project is commissioned yet stays
        # FALSE for a non-AIDOCS project whose db appears incidentally.
        self.hub.code.init_db(root)
        self.hub.code.mark_commissioned(root)
        seed_report = self._seed_factory_memory_into_index(root)
        if seed_report["seeded"]:
            created.append(
                f".MEMORY (memory_index: {seed_report['seeded']} factory rows, "
                f"{seed_report['routes_ensured']} routes)",
            )
        if seed_report["route_failures"]:
            # Visible accounting — never a silently-swallowed partial projection.
            skipped.append(
                "factory route projection FAILED for: "
                + ", ".join(seed_report["route_failures"]),
            )

        for src_file in aidocs_bundle_root.glob("*.aidocs"):
            self.runtime._copy_missing_file(
                src_file,
                aidocs_dest / src_file.name,
                f".MEMORY/.aidocs/{src_file.name}",
                created,
                skipped,
            )
        self.runtime._copy_missing_tree(
            aidocs_bundle_root / "personalities",
            aidocs_dest / "personalities",
            ".MEMORY/.aidocs/personalities",
            created,
            skipped,
        )

        # Stage 5c (no-file-layer): workflow-rules.md / workflow-actions.md
        # are NO LONGER scaffolded. Workflow rule/action definitions are
        # SQL-canonical (workflow_definitions table), edited via the
        # workflow_definition_* dashboard tools. Existing projects' legacy
        # markdown is migrated into sqlite on the first compile, then ignored.

        # `.MEMORY/.aidocs/index.aidocs` is a FIXED, zero-information
        # COMMISSIONING MARKER. Its PATH existence is governance-bearing
        # (commissioning / access-gate / known-projects gate on it), but its
        # CONTENT is never read or parsed anywhere — routing is in-code (SQL
        # memory_routes) and durable memory is canonical in the sqlite
        # memory_index. The bundle copy (copied by the *.aidocs glob above) is
        # the same inert marker; this block only synthesizes it as a fallback.
        router = aidocs_dest / "index.aidocs"
        if not router.exists():
            router.parent.mkdir(parents=True, exist_ok=True)
            router.write_text(_COMMISSIONING_MARKER, encoding="utf-8")
            created.append(".MEMORY/.aidocs/index.aidocs")

        for tmpl_name in ["AGENTS.md", "CLAUDE.md"]:
            dest = root / tmpl_name
            src = templates_root.parents[1] / tmpl_name
            managed_content = ""
            if src.is_file():
                managed_content = src.read_text(encoding="utf-8")
            else:
                managed_content = f"<!-- AIDOCS:BEGIN -->\n# {tmpl_name.replace('.md', '')}\n\nAIDOCS-managed project.\n<!-- AIDOCS:END -->\n"

            if dest.exists():
                existing = dest.read_text(encoding="utf-8")
                end_tag = "<!-- AIDOCS:END -->"
                end_idx = existing.find(end_tag)
                if end_idx >= 0:
                    # Preserve user section after AIDOCS:END
                    user_section = existing[end_idx + len(end_tag) :].lstrip("\r\n")
                    if user_section.strip():
                        managed_content = managed_content.rstrip() + "\n\n" + user_section
                else:
                    # No tags — entire existing file is user content; fold into user section.
                    managed_content = managed_content.rstrip() + "\n\n" + existing
                # Always backup when overwrite changes content (king directive 2026-05-12):
                # protects against silent loss of hand-edits inside the managed section,
                # malformed tags, or any other deviation from the canonical template.
                if existing.strip() != managed_content.strip():
                    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                    backup_path = dest.parent / f"{dest.name}.backup-{ts}"
                    backup_path.write_text(existing, encoding="utf-8")
                    created.append(f"{tmpl_name}.backup-{ts}")
                dest.write_text(managed_content, encoding="utf-8")
                created.append(f"{tmpl_name} (updated)")
            else:
                dest.write_text(managed_content, encoding="utf-8")
                created.append(tmpl_name)

        # ── Disable CC auto-memory — AIDOCS memory_capture is the only path ──
        import json as _json

        claude_local_settings = root / ".claude" / "settings.local.json"
        claude_local_settings.parent.mkdir(parents=True, exist_ok=True)
        if claude_local_settings.exists():
            try:
                existing_settings = _json.loads(
                    claude_local_settings.read_text(encoding="utf-8") or "{}",
                )
            except Exception:
                existing_settings = {}
        else:
            existing_settings = {}
        if existing_settings.get("autoMemoryEnabled") is not False:
            existing_settings["autoMemoryEnabled"] = False
            claude_local_settings.write_text(
                _json.dumps(existing_settings, indent=2) + "\n",
                encoding="utf-8",
            )
            created.append(".claude/settings.local.json")
        else:
            skipped.append(".claude/settings.local.json")

        git_result: dict[str, object] = {"action": "none"}
        if init_git and not (root / ".git").exists():
            try:
                toplevel = _run_git_sync(str(root), "rev-parse", "--show-toplevel")
                git_result = {"action": "already_in_repo", "root": toplevel}
            except FileNotFoundError:
                git_result = {"action": "skipped", "reason": "git not installed"}
            except RuntimeError:
                try:
                    _run_git_sync(str(root), "init")
                    gitignore = root / ".gitignore"
                    if not gitignore.exists():
                        gitignore.write_text(
                            "# AIDOCS defaults\n/.MEMORY/.index/\nnode_modules/\ndist/\n__pycache__/\n.venv/\n*.pyc\n.env\n",
                            encoding="utf-8",
                        )
                        created.append(".gitignore")
                    _run_git_sync(str(root), "add", "-A")
                    _run_git_sync(
                        str(root),
                        "commit",
                        "-m",
                        "chore: initialize project with AIDOCS",
                    )
                    git_result = {"action": "initialized", "initial_commit": True}
                except Exception as exc:
                    git_result = {"action": "failed", "reason": str(exc)}
            except Exception as exc:
                git_result = {"action": "failed", "reason": str(exc)}

        if create_remote and git_result.get("action") == "initialized":
            try:
                output = _run_git_sync(str(root), "remote", "get-url", "origin")
                git_result["remote"] = {
                    "created": False,
                    "reason": f"Remote already exists: {output}",
                }
            except RuntimeError:
                try:
                    import tempfile as _tf

                    _gh_out = None
                    try:
                        with _tf.NamedTemporaryFile(mode="w", suffix=".gh.out", delete=False) as _f:
                            _gh_out = _f.name
                        with open(_gh_out, "w", encoding="utf-8") as _fh:
                            result = subprocess.run(
                                [
                                    "gh",
                                    "repo",
                                    "create",
                                    root.name,
                                    "--private",
                                    "--source",
                                    str(root),
                                    "--push",
                                ],
                                cwd=str(root),
                                stdin=subprocess.DEVNULL,
                                stdout=_fh,
                                stderr=subprocess.DEVNULL,
                                text=True,
                                timeout=30,
                                check=False,
                            )
                        result.stdout = (
                            Path(_gh_out).read_text(encoding="utf-8", errors="ignore").strip()
                        )
                    finally:
                        if _gh_out:
                            try:
                                os.unlink(_gh_out)
                            except OSError:
                                pass
                    git_result["remote"] = {
                        "created": result.returncode == 0,
                        "name": root.name,
                        "url": (result.stdout or "").strip(),
                        "reason": (result.stderr or "").strip() if result.returncode != 0 else "",
                    }
                except FileNotFoundError:
                    git_result["remote"] = {
                        "created": False,
                        "reason": "gh CLI not installed",
                    }
                except Exception as exc:
                    git_result["remote"] = {"created": False, "reason": str(exc)}

        mcp_config_result = self.runtime.ensure_claude_mcp_config(root, interpreter)
        return {
            "initialized": True,
            "created": created,
            "skipped": skipped,
            "git": git_result,
            "origins": self.runtime.project_origins(root),
            "repo_summary": self.runtime.repo_summary(root),
            "mcp_config": mcp_config_result,
            "next_step": "Call project_bootstrap_or_resume to activate managed mode and select a session.",
        }
