"""Real read-only Tier-R execution adapter for the Outer Gate.

This replaces the safe-stub admission executor with one that runs the CANONICAL
AIDOCS tool implementations — but ONLY a curated, side-effect-free, read-proven
Tier-R subset, and ONLY through the same `gate.invoke` admission path (trust,
scope, audit, project-binding, Tier-R-only all enforced upstream in OuterGate).

Defense in depth — this adapter independently refuses anything that is not a
read-verified Tier-R tool on its OWN allowlist, so even a manifest regression
cannot make a mutation reachable here. There is NO code path from this module to
an edit/config/freeze/escalation tool: the allowlist is a frozenset of pure index
reads and the dispatcher passes arguments verbatim to FastMCP's `call_tool` for
exactly those names.

Binding: every call is bound to a single configured EXECUTION project root (the
signed, indexed project the gate serves). The bound root must be a real AIDOCS
project with a populated, non-stale code index, else the call fails closed
(`project_not_aidocs` / `project_unindexed` / `project_index_stale`). Binding is
applied via the `with_target_project_root` ContextVar so `resolve_project_root()`
inside the canonical tools resolves to the bound project deterministically.

The heavy FastMCP server (the canonical tool registry) is built lazily, once per
process, and reused. Calls are serialized under a lock — the shared service hub is
not assumed thread-safe, and read latency is acceptable for this surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

# The ONLY tools this adapter will ever execute: pure, side-effect-free index
# reads that are ALSO remote-eligible read-verified in outer_gate_manifest
# (verified=="read" — the gate refuses anything that is not, e.g. ai_get_lines is
# verified=="decorator" so it never reaches here). Adding a name is a deliberate,
# reviewed act; nothing else is reachable.
READ_EXEC_ALLOWLIST = frozenset(
    {
        "ai_find",
        "ai_search",
        "ai_text_search",
        "ai_investigate",
        "ai_trace",
        "ai_bundle",
        "ai_get_outline",
        "ai_get_symbol_snippet",
        "ai_get_symbol_info",
        "ai_get_dependencies",
    },
)


def _extended_read_exec_allowlist() -> frozenset[str]:
    """The legacy hand-rolled set + every registry-declared read tool.
    Computed once at module load; the registry is appended later as
    the C.20 migration ports more tools. New entries auto-join the
    gate's read surface without touching this file.
    """
    try:
        from . import tool_interface as _ti

        return READ_EXEC_ALLOWLIST | _ti.read_exec_names()
    except Exception:
        return READ_EXEC_ALLOWLIST


READ_EXEC_ALLOWLIST = _extended_read_exec_allowlist()


def exec_project_id(root: Path | str | None) -> str:
    """A stable, content-light identifier for the bound execution project, for
    audit attribution: ``<basename>:<sha256(resolved-path)[:12]>``. Distinct from
    the auth/trust home — two gates serving different exec projects from the same
    auth home produce different ids. Empty string when no exec root is set.
    """
    if not root:
        return ""
    p = Path(root).resolve()
    return f"{p.name}:" + hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:12]


# ── per-tenant host_kind (#410) ──────────────────────────────────────────────
# The web gate historically stamped ONE shared host_kind="generic_mcp" for every
# tenant, so web agents were indistinguishable by host KIND (isolation rested
# entirely on the per-token host_session_id). The host kind is now derived from
# the AUTHENTICATED OAuth client identity (principal["client_id"], bound at
# token mint), sanitized to an identifier-safe, bounded form so it is stable
# inside agent_context_id / epoch / aidocs_session_id derivations.
#
# Fallback is the historical "generic_mcp" — NEVER a fabricated "unknown"
# (see derive_agent_context_id id-tree honesty, 2026-07-16). The "webmcp_"
# prefix namespaces resolved kinds away from host_support_matrix's first-class
# kinds and aliases (a client named "claude" must never alias to claude_code).
_HOST_KIND_UNSAFE_RE = re.compile(r"[^a-z0-9_]+")
_HOST_KIND_MAX_LEN = 48
WEB_HOST_KIND_FALLBACK = "generic_mcp"
WEB_HOST_KIND_PREFIX = "webmcp_"


def resolve_web_host_kind(principal: dict | None) -> str:
    """Per-client host_kind from the authenticated principal's OAuth client_id.

    Sanitized (lowercase, [a-z0-9_], bounded length) so it is stable in identity
    derivations; unresolvable (legacy/direct-mint token, no client_id) falls back
    to the historical shared "generic_mcp" — never an invented identity.
    """
    cid = ""
    if isinstance(principal, dict):
        cid = str(principal.get("client_id") or "")
    kind = _HOST_KIND_UNSAFE_RE.sub("_", cid.strip().lower())
    kind = kind.strip("_")[:_HOST_KIND_MAX_LEN].rstrip("_")
    if not kind:
        return WEB_HOST_KIND_FALLBACK
    return WEB_HOST_KIND_PREFIX + kind


# The ONLY shell tools the gate exposes — the canonical detached runner + its
# output/kill, run THROUGH their existing implementation (which itself enforces
# enforce_tool_call → bash_policy + heuristic_judge + freeze + lifecycle preflight
# + destructive floor). The gate adds project_run scope + project binding on top.
RUN_ALLOWLIST = frozenset({"ai_run", "ai_run_output", "ai_run_kill"})


class ExecutorRefusal(Exception):
    """A fail-closed refusal raised by the executor. OuterGate.invoke maps it to a
    GateResult refusal with this precise blocked_by/reason (instead of an opaque
    execution_error), so unindexed/stale/unknown-arg states are auditable.
    """

    def __init__(self, blocked_by: str, reason: str = "") -> None:
        super().__init__(reason or blocked_by)
        self.blocked_by = blocked_by
        self.reason = reason


def _index_db_path(root: Path) -> Path:
    return root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _is_aidocs_project(root: Path) -> bool:
    # Single chokepoint: the commission stamp inside the sqlite index —
    # not the deprecated .aidocs file, not the db file's mere existence.
    from .mcp_server_runtime_helpers import is_aidocs_managed

    return is_aidocs_managed(root)


def _index_populated(root: Path) -> bool:
    db = _index_db_path(root)
    if not db.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
        finally:
            con.close()
        return int(n) > 0
    except sqlite3.Error:
        return False


def _index_version_mismatch(root: Path) -> bool:
    """PERSISTENT staleness: the on-disk index was built by a DIFFERENT code-index
    schema than the running code expects (index_meta.code_index_version !=
    CodeIndexStore.INDEX_VERSION). Unlike the in-process known-stale flag (which a
    gate process that runs no sitter never sets), this is detectable straight from
    the deployed artifact — a stale deploy that shipped an old index against newer
    code fails closed. Cheap: one indexed row read.
    """
    db = _index_db_path(root)
    if not db.is_file():
        return False  # unindexed is handled by _index_populated, not here
    try:
        from .code_index_store import CodeIndexStore

        expected = CodeIndexStore.INDEX_VERSION
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM index_meta WHERE key='code_index_version'",
            ).fetchone()
        finally:
            con.close()
        stored = row[0] if row else None
        return stored is not None and str(stored) != str(expected)
    except Exception:
        return False


def _is_stale(root: Path) -> bool:
    # Persistent (deploy-detectable) staleness: incompatible index schema version.
    if _index_version_mismatch(root):
        return True
    # In-process staleness: a sitter observed an unreconciled external change.
    try:
        from .project_index_sitter import is_index_known_stale

        return bool(is_index_known_stale(root))
    except Exception:
        # Staleness is a best-effort signal; its absence is not, by itself, a
        # reason to admit — marker + populated-index are the hard gates above.
        return False


def _result_payload(tool_result: Any) -> Any:
    """Extract a JSON-serializable payload from a FastMCP ToolResult. Prefer the
    structured content; then the raw .data (so a tool's own return dict — e.g.
    ai_run's {run_id, done, tail} — surfaces, making ai_run_output usable even for
    inline-completed runs); otherwise parse the text content blocks. Never return a
    pydantic model (only basic JSON types from .data).
    """
    sc = getattr(tool_result, "structured_content", None)
    if sc is not None:
        return sc
    data = getattr(tool_result, "data", None)
    if isinstance(data, (dict, list, str, int, float, bool)):
        try:
            json.dumps(data)
            return data
        except (TypeError, ValueError):
            pass
    blocks = getattr(tool_result, "content", None) or []
    out: list[Any] = []
    for b in blocks:
        txt = getattr(b, "text", None)
        if txt is None:
            continue
        try:
            out.append(json.loads(txt))
        except (ValueError, TypeError):
            out.append(txt)
    if len(out) == 1:
        return out[0]
    return out


class ReadOnlyTierRExecutor:
    """Callable `(entry, request) -> payload` for OuterGate's executor seam."""

    def __init__(
        self,
        exec_project_root: Path | str,
        *,
        allowlist: frozenset[str] = READ_EXEC_ALLOWLIST,
        server_factory=None,
    ) -> None:
        self._root = Path(exec_project_root).resolve()
        self._allow = frozenset(allowlist)
        self._server_factory = server_factory
        self._server = None
        self._schemas: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── canonical server (built once, reused) ──
    def _ensure_server(self):
        if self._server is None:
            if self._server_factory is not None:
                self._server = self._server_factory()
            else:
                from .mcp_server import create_server

                # read_only profile: canonical code/read tools without the palace
                # bundle (no vendored mempalace / vector stack needed).
                # expose_deploy=True: this is the gate's INTERNAL read executor —
                # it must carry ai_deploy_output (the deploy-status read) so the
                # gate can serve it. Never agent-facing (Empire directive 2026-07-06).
                self._server = create_server(tools_profile="read_only", expose_deploy=True)
        return self._server

    def _schema(self, name: str) -> dict:
        if name not in self._schemas:
            srv = self._ensure_server()
            tool = asyncio.run(srv.get_tool(name))
            self._schemas[name] = dict(getattr(tool, "parameters", {}) or {})
        return self._schemas[name]

    # ── validation ──
    def _validate_entry(self, entry: dict, name: str) -> None:
        # Defense in depth: independent of OuterGate's tier checks.
        if entry.get("tier") != "R" or entry.get("verified") != "read":
            raise ExecutorRefusal(
                "not_read_verified_tier_r",
                f"{name} is not a read-verified Tier-R tool — refusing to execute",
            )
        if name not in self._allow:
            raise ExecutorRefusal(
                "tool_not_exec_allowlisted",
                f"{name} is not on the read-execution allowlist",
            )

    def _validate_args(self, name: str, args: dict) -> None:
        schema = self._schema(name)
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in args:
                raise ExecutorRefusal("missing_required_arg", f"{name} requires '{req}'")
        for key in args:
            if key not in props:
                raise ExecutorRefusal("unknown_arg", f"{name}: unexpected argument '{key}'")

    def _validate_binding(self, root: Path) -> None:
        try:
            aidocs_ok = _is_aidocs_project(root)
            index_ok = _index_populated(root) if aidocs_ok else False
            stale = _is_stale(root) if aidocs_ok and index_ok else False
        except OSError as exc:
            # errno 13 / EACCES etc: the gate runtime cannot ACCESS the bound
            # exec-root (a provisioning perm mismatch — runtime uid vs exec-root
            # owner), NOT a policy denial. Surface it TRUTHFULLY + actionably and
            # fail closed, instead of leaking a raw PermissionError to the agent
            # (#253: file/run/search/index-status all EACCES share THIS seam).
            raise ExecutorRefusal(
                "exec_root_unreadable",
                f"the gate runtime cannot access the bound project tree {root} "
                f"(OS error: {exc}). Filesystem-permission mismatch — the exec-root "
                f"must be readable+executable by the gate runtime user — NOT a policy "
                f"denial. Fix the mount/ownership; no tool can run until then.",
            ) from exc
        if not aidocs_ok:
            raise ExecutorRefusal(
                "project_not_aidocs",
                f"bound project {root} is not an AIDOCS project",
            )
        if not index_ok:
            raise ExecutorRefusal(
                "project_unindexed",
                f"bound project {root} has no populated code index",
            )
        if stale:
            raise ExecutorRefusal(
                "project_index_stale",
                f"bound project {root} index is stale — refusing (reconcile first)",
            )

    # ── the executor seam ──
    def __call__(self, entry: dict, request) -> Any:
        name = entry.get("name", "")
        self._validate_entry(entry, name)
        # Execution binds to the SERVER-RESOLVED selected project (request.exec_root,
        # set by the transport from the token's validated gate selection) or, when
        # absent, this adapter's configured default. It is NEVER bound to a client-
        # supplied path — request.exec_root comes from GateProjectStore.current()
        # (a registered project_id), and request.project_root (the auth home) is
        # ignored for execution. The bound root is then re-validated below
        # (aidocs/indexed/non-stale) and all paths are confined to it.
        req_exec = getattr(request, "exec_root", None)
        root = Path(req_exec).resolve() if req_exec else self._root
        self._validate_binding(root)
        args = dict(getattr(request, "tool_input", None) or {})
        self._validate_args(name, args)
        return self._run(name, args, root)

    def run(
        self,
        tool: str,
        args: dict,
        *,
        exec_root: Path | str,
        session_id: str = "",
        host_session_id: str = "",
        host_kind: str = "",
    ) -> Any:
        """Run a RUN_ALLOWLIST shell tool (ai_run/ai_run_output/ai_run_kill)
        THROUGH its canonical implementation, cwd-bound to the selected exec
        project. The tool's own law (enforce_tool_call → bash_policy +
        heuristic_judge + freeze + lifecycle preflight + destructive floor) runs
        inside call_tool — the gate never bypasses it. Binding + args validated;
        fail-closed on a non-ready project.

        A remote managed-mode session (session_id) is bound to the exec project
        so the canonical ai_run's managed-mode/attribution law is satisfied — the
        run context IS a real session, never the process CWD/global state.
        """
        if tool not in RUN_ALLOWLIST:
            raise ExecutorRefusal(
                "tool_not_run_allowlisted",
                f"{tool} is not an allowlisted run tool",
            )
        # Fail closed: no invented session. The gate refuses sessionless tenant
        # RUNs (no_session_selected); this floor guarantees NO caller — present
        # or future — can reach a manufactured "ogr_remote" context (webmcp,
        # 2026-07-05). The run context must be a real, operator-selected session.
        # Checked before binding/schema so the refusal never depends on server
        # availability.
        sid = str(session_id or "").strip()
        if not sid:
            raise ExecutorRefusal(
                "no_session_selected",
                "run requires a selected session; select or create one "
                "(session_select / session_create) before running",
            )
        root = Path(exec_root).resolve()
        self._validate_binding(root)
        a = dict(args or {})
        self._validate_args(tool, a)
        hid = host_session_id or sid
        from .mcp_server_runtime_helpers import (
            reset_request_host_identity,
            set_request_host_identity,
            with_target_project_root,
        )

        with self._lock:
            srv = self._ensure_server()
            # Per-token host_session_id is the ISOLATION boundary: ai_run_output/
            # kill refuse a run whose stamped session doesn't resolve from the
            # caller's host_session_id. Two tokens ⇒ two host ids ⇒ isolated; the
            # SAME token (same host id) may control its own runs across sessions.
            # host_kind (#410) is the caller-resolved per-client kind (see
            # resolve_web_host_kind); empty falls back to the historical shared
            # "generic_mcp" so local/legacy callers are byte-identical.
            identity_token = set_request_host_identity(
                hid,
                host_kind=(host_kind or WEB_HOST_KIND_FALLBACK),
            )
            try:
                with with_target_project_root(root):
                    self._ensure_remote_context(srv, root, sid, hid)
                    tool_result = asyncio.run(srv.call_tool(tool, a))
            finally:
                reset_request_host_identity(identity_token)
        return _result_payload(tool_result)

    @staticmethod
    def _ensure_remote_context(srv, root: Path, session_id: str, host_session_id: str = "") -> None:
        """Establish — via the CANONICAL services, NOT naked writes — the same
        lifecycle artifacts a local host would for this remote session: a managed-
        mode binding and a REAL active task (canonical ai_task begin, which records
        an audited task + sets current_task_id through the real service). Idempotent
        (reuses an existing active task) + isolated per session_id (which the gate
        scopes by token_id + selected project). ai_run then passes require_active_
        task / enforce_tool_call / orchestrator / freeze / preflight UNCHANGED.
        """
        from .managed_mode_service import ManagedModeService
        from .query_gate import QueryGateStore

        try:
            # Canonical session creation (idempotent): a real session with its
            # SESSION.md/handoff scaffolding, via ai_session — not a bare mkdir.
            if not (root / ".MEMORY" / "sessions" / session_id / "SESSION.md").exists():
                asyncio.run(
                    srv.call_tool(
                        "ai_session",
                        {
                            "mode": "create",
                            "session_id": session_id,
                            "title": "Outer Gate remote run",
                            "goal": "remote run context",
                        },
                    ),
                )
            ManagedModeService().set_mode(
                root,
                session_id,
                source="outer_gate",
                host_session_id=host_session_id or session_id,
            )
            # Idempotent: only open a task if this session has none active.
            if not QueryGateStore().get_current_task_id(root, session_id):
                asyncio.run(
                    srv.call_tool(
                        "ai_task",
                        {
                            "mode": "begin",
                            "session_id": session_id,
                            "goal": "Outer Gate remote run context",
                        },
                    ),
                )
        except Exception as e:  # noqa: BLE001 — can't establish ⇒ refuse, never run
            raise ExecutorRefusal(
                "run_context_failed",
                f"could not establish remote run context: {e}",
            )
        # Fail closed: the CANONICAL begin must have produced a real active task —
        # never fall back to a naked current_task_id write.
        if not QueryGateStore().get_current_task_id(root, session_id):
            raise ExecutorRefusal(
                "run_context_failed",
                "canonical task begin did not establish an active task",
            )

    def _run(self, name: str, args: dict, root: Path) -> Any:
        from .mcp_server_runtime_helpers import with_target_project_root

        with self._lock:
            srv = self._ensure_server()
            with with_target_project_root(root):
                tool_result = asyncio.run(srv.call_tool(name, args))
        return _result_payload(tool_result)


def build_read_executor(exec_project_root: Path | str, **kw):
    """Construct the read-only Tier-R executor for OuterGate(executor=...)."""
    return ReadOnlyTierRExecutor(exec_project_root, **kw)
