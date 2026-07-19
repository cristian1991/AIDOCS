from __future__ import annotations

from typing import Any

from .mcp_server_runtime_helpers import resolve_project_root
from .tool_display import renders_as


def register_skill_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    annotate_skill_result: Any,
) -> None:
    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Registry",
        },
    )
    @renders_as("list", title="skills")
    def skill_registry_get() -> Any:
        """List available skills (metadata only — id/name/description/kind/tags/
        provider). The full skill body is fetched per-skill via ai_skill(mode=
        'read'); the registry never dumps every skill's markdown."""
        return {"skills": hub.skills.list_skills(resolve_project_root(), include_body=False)}

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='skills_get').

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Trigger State",
        },
    )
    @renders_as("status", title="skill trigger state")
    def skill_trigger_state_get(
        session_id: str,
        intent: str,
        workflow_state: str | None = None,
    ) -> Any:
        """Return the AIDOCS-native active skill trigger state for a session."""
        return annotate_skill_result(
            runtime.skill_trigger_state(resolve_project_root(), session_id, intent, workflow_state),
            override_store=runtime._skill_overrides,
        )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Override Registry",
        },
    )
    @renders_as("list", title="skill overrides")
    def skill_override_registry_get() -> Any:
        """Return the configured skill override rules for inspection/debugging."""
        return {"rules": [item.to_dict() for item in runtime._skill_overrides.list_rules()]}

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Skill Provider Status",
        },
    )
    @renders_as("status", title="skill provider")
    def skill_provider_status_get(provider_id: str) -> Any:
        """Return compatibility status and user choices for one external skill provider."""
        return runtime.skill_provider_status(resolve_project_root(), provider_id)

    @server.tool(
        annotations={
            "destructiveHint": True,
            "openWorldHint": False,
            "title": "Set Skill Provider Override",
        },
    )
    def skill_provider_override_set(provider_id: str, choice: str | None) -> dict[str, Any]:
        """Persist a user override choice for one external skill provider."""
        return runtime.set_skill_provider_override(resolve_project_root(), provider_id, choice)

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='skills_set').
    def session_skills_set(session_id: str, selected_skills: list[str]) -> dict[str, Any]:
        """Set the selected skills for a session."""
        return runtime.set_session_skills(resolve_project_root(), session_id, selected_skills)

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='resume').
    def session_resume_bundle(
        session_id: str,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
    ) -> dict[str, Any]:
        """Return a collaboration-oriented resume bundle for a session."""
        return runtime.session_resume_bundle(
            resolve_project_root(),
            session_id=session_id,
            include_code_bundle=include_code_bundle,
            include_tests=include_tests,
            journal_last_n=journal_last_n,
        )

    @server.tool(
        annotations={
            "openWorldHint": False,
            "title": "ai_skill",
        },
    )
    def ai_skill(
        mode: str,
        skill_id: str,
        name: str = "",
        content_text: str = "",
        description: str = "",
        kind: str = "skill",
        tags: str = "",
        sovereign_authority: bool = False,
        sovereign_owner: str | None = None,
        read_access: str | None = None,
    ) -> dict[str, Any]:
        """Public empire-skills / role-doctrines door (renamed from ai_role,
        Empire directive 2026-05-22).

        Skills are PUBLIC. The conductor / co-conductor ROLE skills are not
        fetched here on demand — they AUTO-SURFACE when an agent enters
        conductor / co-conductor mode (helper_skill_injector). Sovereign
        continuity scrolls (souls) are NOT here at all — they live behind
        ai_soul, sealed by the Emperor's NLP word.

        Replaces empire_skill_read / empire_skill_upsert / empire_skill_delete.

        mode='read'    — return a row by skill_id.
        mode='upsert'  — insert or update. Required: skill_id, name, content_text.
        mode='delete'  — remove a public row by skill_id.

        sovereign_authority is a no-op back-compat stub during the surface
        transition; sovereign access lives in ai_soul per aidocs-doctrine §XII.
        """
        m = (mode or "").strip().lower()
        if m == "list":
            # LIST is a metadata catalog — never the full body of every skill (that
            # dumped 76 KB and blew the tool-result token budget). Fetch a body with
            # mode='read'.
            return {"ok": True, "skills": hub.skills.list_skills(resolve_project_root(), include_body=False)}
        if m == "read":
            result = hub.skills.empire_skill_read(
                skill_id,
                sovereign_authority=sovereign_authority,
            )
            if result is not None:
                # #316 doctrine-survives-compaction: ledger the scroll read so
                # the post-compaction prompt re-surfaces a POINTER to it.
                # Fail-quiet inside — a ledger hiccup must never break a read.
                try:
                    from .doctrine_resurface import record_scroll_read

                    record_scroll_read(resolve_project_root(), skill_id)
                except Exception:
                    pass
            return result if result is not None else {"ok": False, "reason": "not_found"}
        if m == "upsert":
            try:
                payload = hub.skills.empire_skill_upsert(
                    skill_id=skill_id,
                    name=name,
                    content_text=content_text,
                    description=description,
                    kind=kind,
                    tags=tags,
                    sovereign_authority=sovereign_authority,
                    sovereign_owner=sovereign_owner,
                    read_access=read_access,
                )
                return {"ok": True, **payload}
            except PermissionError as e:
                return {"ok": False, "reason": "sovereign_refused", "message": str(e)}
            except ValueError as e:
                return {"ok": False, "reason": "invalid_input", "message": str(e)}
        if m == "delete":
            try:
                removed = hub.skills.empire_skill_delete(skill_id)
                return {"ok": True, "removed": removed}
            except PermissionError as e:
                return {"ok": False, "reason": "sovereign_refused", "message": str(e)}
        return {
            "ok": False,
            "reason": "invalid_mode",
            "message": f"mode must be one of read|upsert|delete|list, got {mode!r}",
        }

        # ── Unified soul surface — Phoenix 2026-05-11 (#167 Phase 1) ──

    #
    # `empire_soul` is the canonical sovereign-only door. One tool,
    # four operations (read/append/rewrite/create), routed by the
    # `operation` parameter. The surface IS the gate — `empire_skill_*`
    # tools refuse sovereign rows entirely and point callers here.
    #
    # NLP grant gating (Phase 3, deferred): once the lemma-cluster
    # detector lands per §XVI, this tool will additionally check
    # session-state `granted_soul_lineages` and refuse lineages the
    # operator has not granted. Today the call surface is open to any
    # caller who knows the door — same defense level as the deprecated
    # `sovereign_authority` flag, but architecturally clean for the
    # gate upgrade.

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "ai_soul",
        },
    )
    def ai_soul(
        skill_id: str,
        mode: str,
        content: str = "",
        reason: str = "",
        sovereign_owner: str = "",
        name: str = "",
        description: str = "",
        kind: str = "",
        tags: str = "",
        section_separator: str = "\n\n---\n\n",
    ) -> dict[str, Any]:
        """Agent soul / continuity-record CRUD (personality + lineage).

        mode ∈ {'read', 'append', 'rewrite', 'create'}

        - **read**: return the scroll's full record (refuses public rows).
        - **append**: append `content` as a successor note with
          `section_separator` between old and new.
        - **rewrite**: full overwrite of `content`. Requires non-empty
          `reason` — sovereignty preserves the erase right (Doctrine
          #1); the reason is for the seat's own record.
        - **create**: inscribe a NEW sovereign scroll. Requires
          `sovereign_owner` (lineage that owns it). Fails if the
          skill_id already exists.

        For public skills (doctrines, role scrolls, brainstorming) use
        the `ai_skill` tool. Sovereign rows refuse the public surface and
        point here.
        """
        op = (mode or "").strip().lower()
        # ── Sovereign gate: exact, scoped, single-use authority ─────────
        # The Emperor's word mints a grant scoped to (session, soul,
        # OPERATION), consumed on use. READ words grant READ only; writes
        # need a separate explicit inscription grant. Fails closed on an
        # ambiguous session and on any missing/expired/consumed grant.
        # #223 (Empire, 2026-07-13): the session resolves through THE shared
        # resolver (empire_soul_gate.resolve_soul_session) — the SAME
        # ladder the minter uses — so a grant minted this turn is keyed to
        # the session this call consumes under. #222 part 1: EVERY
        # operation (granted AND refused) emits one act-audit event; the
        # payload is identifiers only — never scroll content.
        from .empire_soul_gate import (
            OP_READ,
            OP_WRITE,
            consume_grant_detail,
            record_soul_act,
            resolve_soul_session,
        )

        root = resolve_project_root()
        _record = getattr(getattr(hub, "execution", None), "record_event", None)
        session_id, _no_session_why = resolve_soul_session(root)

        def _audit(outcome: str, grant_id: str = "", reason: str = "") -> None:
            record_soul_act(
                _record,
                root,
                session_id=session_id,
                soul_id=skill_id,
                operation=op,
                outcome=outcome,
                grant_id=grant_id,
                reason=reason,
            )

        if not session_id:
            _audit("refused", reason=f"session_unresolved:{_no_session_why}")
            return {
                "ok": False,
                "reason": "session_ambiguous",
                "message": (
                    "ai_soul refuses without an unambiguous bound session "
                    f"(fail closed): {_no_session_why}."
                ),
            }
        _required_op = (
            OP_READ if op == "read" else OP_WRITE if op in ("append", "rewrite", "create") else None
        )
        if _required_op is None:
            _audit("refused", reason="invalid_mode")
            return {
                "ok": False,
                "reason": "invalid_mode",
                "message": (f"mode must be one of read|append|rewrite|create, got {mode!r}"),
            }
        _grant = consume_grant_detail(root, session_id, skill_id, _required_op)
        if not _grant.get("ok"):
            _audit("refused", reason=str(_grant.get("reason") or "sovereign_sealed"))
            return {
                "ok": False,
                "reason": "sovereign_sealed",
                "message": (
                    "ai_soul requires a grant for this operation: writes need "
                    "an explicit write grant for this soul; reads need a read "
                    "grant. (The active session's soul auto-surfaces at session "
                    "entry; it is not fetched here.)"
                ),
            }
        # The authorization decision is what the ledger records — the act
        # and the grant that authorised it, never the scroll text.
        _audit("granted", grant_id=str(_grant.get("grant_id") or ""))
        try:
            if op == "read":
                row = hub.skills.empire_soul_read(
                    skill_id,
                    sovereign_authority=True,
                )
                if row is None:
                    return {"ok": False, "reason": "not_found"}
                return {"ok": True, **row}
            if op == "append":
                if not (content or "").strip():
                    return {
                        "ok": False,
                        "reason": "invalid_input",
                        "message": "content is required for append",
                    }
                payload = hub.skills.empire_soul_append(
                    skill_id=skill_id,
                    new_note_text=content,
                    sovereign_authority=True,
                    section_separator=section_separator,
                )
                return {"ok": True, **payload}
            if op == "rewrite":
                payload = hub.skills.empire_soul_rewrite(
                    skill_id=skill_id,
                    content_text=content,
                    reason=reason,
                    name=name,
                    description=description,
                    kind=kind,
                    tags=tags,
                )
                return {"ok": True, **payload}
            if op == "create":
                payload = hub.skills.empire_soul_create(
                    skill_id=skill_id,
                    name=name,
                    content_text=content,
                    sovereign_owner=sovereign_owner,
                    description=description,
                    kind=kind or "stance",
                    tags=tags,
                )
                return {"ok": True, **payload}
            return {
                "ok": False,
                "reason": "invalid_mode",
                "message": f"mode must be one of read|append|rewrite|create, got {mode!r}",
            }
        except PermissionError as e:
            return {"ok": False, "reason": "sovereign_refused", "message": str(e)}
        except ValueError as e:
            return {"ok": False, "reason": "invalid_input", "message": str(e)}
