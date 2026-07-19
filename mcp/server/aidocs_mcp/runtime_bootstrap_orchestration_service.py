from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import _parse_bool


class RuntimeBootstrapOrchestrationService:
    def __init__(self, runtime: Any, logger: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub
        self._logger = logger

    def project_bootstrap_or_resume(
        self,
        project_root: Path,
        session_id: str | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
    ) -> dict[str, object]:
        agents = project_root / "AGENTS.md"
        claude = project_root / "CLAUDE.md"
        memory_root = project_root / ".MEMORY"

        initialized = memory_root.is_dir() and (agents.is_file() or claude.is_file())
        if not initialized:
            result = {
                "stage": "setup_required",
                "ready": False,
                "next_step": "project_init",
                "reason": "missing AIDOCS project structure",
            }
            result["report"] = self.runtime._build_bootstrap_report(result)
            return result

        # HEAL-FORWARD (marker->sqlite migration, 2026-07-04): an existing
        # project commissioned before the cut carries only the legacy .aidocs
        # marker. Detection already reads it as managed (is_aidocs_managed), but
        # stamp it forward here — the natural per-resume write-point — so the
        # project converges onto the commission stamp and the legacy read can be
        # retired (Stage 6). Idempotent: no-op once stamped, and never touches a
        # project that lacks the governance-bearing legacy marker.
        try:
            from .mcp_server_runtime_helpers import heal_legacy_commission

            heal_legacy_commission(project_root)
        except Exception as exc:
            self._logger.debug("heal_legacy_commission (non-fatal): %s", exc)

        repaired = None
        structure_gaps = self.runtime.project_structure_gaps(project_root)
        if structure_gaps:
            repaired = self.runtime.project_init(project_root, init_git=False, create_remote=False)

        # Ensure .mcp.json is present for Claude Code (idempotent)
        try:
            self.runtime.ensure_claude_mcp_config(project_root)
        except Exception as exc:
            self._logger.debug("Failed to ensure .mcp.json: %s", exc)

        # Ensure the empire's cross-project doctrine LAW is seeded in the global
        # tier (#231) — idempotent: self-heals a wiped empire + seeds fresh
        # installs so the law surfaces in every project. Cheap existence check.
        try:
            from .doctrine_global_law_seed import ensure_doctrine_global_law

            ensure_doctrine_global_law()
        except Exception as exc:
            self._logger.debug("ensure_doctrine_global_law: %s", exc)

        # #167 Phase 3: seed the soul-lineage evocation shapes into the empire
        # intent registry (idempotent) so lineage detection is registry-canonical
        # and multi-language (§XIV), not a hardcoded English regex. Self-heals a
        # wiped empire; the legacy regex remains the availability fallback.
        try:
            from .empire_soul_gate import ensure_lineage_registry_seed

            ensure_lineage_registry_seed()
        except Exception as exc:
            self._logger.debug("ensure_lineage_registry_seed: %s", exc)

        # Legacy migration: import old aidocs.toml into SQLite for pre-v2.3 projects.
        # New projects never create a TOML — settings go directly to SQLite via dashboard.
        try:
            from .config_store import ConfigStore

            _config_store = ConfigStore()
            toml_path = project_root / "aidocs.toml"
            if toml_path.is_file():
                _config_store.import_from_toml(
                    project_root,
                    toml_path,
                    scope="project",
                    overwrite=False,
                )
        except Exception as exc:
            self._logger.debug("Legacy TOML import: %s", exc)

        # Bootstrap distinction (operator doctrine 2026-04-30):
        #   first-ever bootstrap = managed-mode bootstrap fires AND
        #     index has never been synced for this project →
        #     full _sync_bootstrap_indexes (memory + code + schema +
        #     workflow + capabilities + procedures + links).
        #   per-launch bootstrap = managed-mode bootstrap fires on
        #     a project where the index already exists → light
        #     status read only. Per-edit incremental sync handles
        #     ongoing freshness; ai_index_sync is the operator's
        #     explicit re-sync for external-change recovery.
        # Detection signal: code_files table empty for this project.
        # No marker file, no staleness heuristic — the index data
        # itself is the source of truth.
        first_ever = self._is_first_ever_bootstrap(project_root)
        if first_ever:
            sync_result = self.runtime._sync_bootstrap_indexes(
                project_root,
                include_tests=include_tests,
            )
            sync_result["first_ever_bootstrap"] = True
            # Stamp completion AFTER the sync so a partial / crashed
            # first-ever stays detectable as first-ever on retry.
            try:
                self.hub.managed_mode.stamp_bootstrap_completed(project_root)
            except Exception as exc:
                self._logger.debug(
                    "stamp_bootstrap_completed failed (non-fatal): %s",
                    exc,
                )
        else:
            sync_result = self._light_bootstrap_indexes(project_root)
            sync_result["first_ever_bootstrap"] = False

        legacy_state = self.hub.legacy.inspect_legacy(project_root)
        sessions = self.hub.sessions.list_sessions(project_root)
        if legacy_state.get("legacy_present") and len(sessions) == 0:
            proposal = self.hub.legacy.build_session_proposal(project_root, session_id=session_id)
            result = {
                "stage": "migration_required",
                "ready": False,
                "initialized": True,
                "indexes_synced": True,
                "repaired": repaired,
                "sync": sync_result,
                "legacy": legacy_state,
                "proposal": proposal,
                "next_step": "issue_stop_for_migration_choice",
            }
            result["report"] = self.runtime._build_bootstrap_report(result)
            return result

        session_result = self.runtime.session_start(
            project_root,
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            sync_indexes=False,
            include_tests=include_tests,
            hydrate=False,  # Bootstrap path: lightweight only
        )

        if include_code_bundle and not session_result.get("requires_session_selection"):
            selected = session_result.get("selected_session") or {}
            selected_session_id = selected.get("session_id")
            if isinstance(selected_session_id, str) and selected_session_id.strip():
                session_result["ai_bundle"] = self.runtime._refresh_session_code_bundle(
                    project_root,
                    session_id=selected_session_id,
                    include_tests=include_tests,
                    sync_indexes=False,
                )

        result = {
            "stage": "session_active"
            if not session_result.get("requires_session_selection")
            else "session_selection_required",
            "ready": not session_result.get("requires_session_selection"),
            "initialized": True,
            "indexes_synced": True,
            "repaired": repaired,
            "repo_summary": self.runtime.repo_summary(project_root),
            "sync": sync_result,
            "session": session_result,
        }
        selected = (
            session_result.get("selected_session")
            if isinstance(session_result.get("selected_session"), dict)
            else {}
        )
        selected_session_id = str(selected.get("session_id") or "").strip() or None
        result["project_overview"] = self.runtime._build_project_overview(
            project_root,
            repo_summary=result.get("repo_summary")
            if isinstance(result.get("repo_summary"), dict)
            else None,
            selected_session_id=selected_session_id,
            stage=str(result.get("stage") or "unknown"),
            ready=bool(result.get("ready")),
        )
        if selected_session_id:
            result["session_overview"] = session_result.get("session_overview")
            result["skills_overview"] = session_result.get("skills_overview")
            selected_sections = (
                selected.get("sections") if isinstance(selected.get("sections"), dict) else {}
            )
            goal_values = self.runtime._clean_bullets(selected_sections.get("Goal", []))
            result["plan_overview"] = self.runtime._build_default_plan_overview(
                session_id=selected_session_id,
                end_goal=goal_values[0] if goal_values else None,
            )

        # Without rules injection, AIDOCS operates in MCP-tool-only mode —
        # the agent can use indexed retrieval but does not follow any /.MEMORY/rules/ directives.
        effective_config = self.runtime.effective_config(
            project_root,
            session_id=selected_session_id,
        )
        agent_config = (
            effective_config.get("agent") if isinstance(effective_config.get("agent"), dict) else {}
        )
        inject_rules = _parse_bool(agent_config.get("inject_rules_on_bootstrap"), default=True)
        if inject_rules:
            rules = self.runtime._load_project_rules(project_root)
            if rules:
                result["rules"] = rules

        # Persist resolved config into aidocs.sqlite3.resolved_config so
        # host plugins (OC, Cursor) can read the fully-merged config
        # without re-implementing TOML resolution and without a JSON
        # sidecar drift risk. (Migrated from resolved-config.json 2026-04-20.)
        try:
            self.runtime._config_resolver.write_resolved_config(
                project_root=project_root,
                session_id=selected_session_id,
            )
        except Exception as exc:
            self._logger.debug("Failed to persist resolved_config to sqlite: %s", exc)

        result["report"] = self.runtime._build_bootstrap_report(result)
        return result

    def _is_first_ever_bootstrap(self, project_root: Path) -> bool:
        """True iff bootstrap has never completed for this project.

        Signal (operator doctrine 2026-04-30): the
        aidocs_managed.bootstrap_completed_at column. NULL → first-
        ever; non-NULL → previously bootstrapped, per-launch path
        applies. This is INDEPENDENT of code-file count — a fresh
        project with zero source files still completes a bootstrap
        (memory, schema, capabilities, procedures, etc.).

        Per-edit incremental sync (post_edit_reindex_and_grant)
        keeps the indexes fresh during normal work; ai_index_sync is
        the operator's explicit re-sync for external-change recovery
        (git pull, manual edits). Per-launch managed-mode bootstrap
        on a previously-bootstrapped project should NOT re-sync —
        that's wasteful and pre-fix made every host relaunch feel
        slow.
        """
        try:
            stamp = self.hub.managed_mode.get_bootstrap_completed_at(
                project_root,
            )
        except Exception:
            # Probe failure → fail safe to first-ever (over-sync is
            # safer than skipping a needed sync; operator can always
            # force re-sync via ai_index_sync).
            return True
        return stamp is None

    def _light_bootstrap_indexes(self, project_root: Path) -> dict[str, object]:
        """Read-only status snapshot for the per-launch bootstrap path.

        Mirrors _sync_bootstrap_indexes' return shape so downstream
        consumers (report builder, dashboard) see consistent keys —
        but does NOT trigger any sync work. Skipped on a populated
        index because per-edit incremental sync already keeps it
        fresh; full sync is only needed first-ever or on operator
        demand via ai_index_sync.
        """
        palace_heal = self._heal_empty_palace(project_root)
        try:
            result: dict[str, object] = {
                "memory": self.hub.index.status(project_root),
                "code_manifest": {
                    "code_files": self.hub.code.code_status(project_root),
                    "modules": self.hub.code.get_modules(project_root),
                },
                "schema": self.hub.schema.schema_status(project_root),
                "execution": self.hub.execution.execution_status(project_root),
            }
            if palace_heal is not None:
                result["palace_ingest"] = palace_heal
            return result
        except Exception as exc:
            self._logger.debug("light bootstrap indexes status read failed: %s", exc)
            return {"memory": None, "error": str(exc)}

    def _heal_empty_palace(self, project_root: Path) -> dict | None:
        """One-shot palace projection heal for the per-launch path.

        The full ingest runs only on FIRST-EVER bootstrap, so a project
        bootstrapped before the palace wiring landed has an empty palace
        forever — and _load_project_rules (which serves bootstrap rules
        from palace drawers, degrading closed) silently returns nothing.
        When the palace exists but holds zero drawers while canonical
        memory rows exist, re-run the full ingest once. Idempotent: a
        populated palace skips immediately; deterministic drawer ids
        make a double ingest safe. Best-effort — never blocks bootstrap.
        """
        palace = getattr(self.hub, "palace", None)
        if palace is None:
            return None
        try:
            from .memory_sqlite_store import (
                list_entries,
                palace_ingest_from_canonical,
            )
            from .palace_hub_extension import build_palace_context

            ctx = build_palace_context(
                self.hub,
                self.runtime,
                tool_name="bootstrap.palace_heal",
            )
            status = palace.status(hub_ctx=ctx)
            if int(getattr(status, "total_drawers", 0) or 0) > 0:
                return None
            if not list_entries(project_root):
                return None
            stats = palace_ingest_from_canonical(
                project_root,
                palace,
                hub_ctx=ctx,
            )
            self._logger.info("palace heal ingest: %s", stats)
            return {"healed": True, **stats}
        except Exception:
            self._logger.exception("palace heal ingest failed")
            return None
