"""Gate project registry + session/project selectors + GitHub import.

The connector exposes the CANONICAL tool catalog plus project/session selectors,
but EXECUTION stays controlled by gate auth + deny-by-default grants. This module
owns the gate's view of projects and the per-token current selection. State lives
in the control-plane (identity) DB, co-located with tokens — never the install-
wide registry, so the public gate has its own sealed namespace.

Principles (all fail-closed):
  * A forged/unknown project_id resolves to NOTHING (deny) — selection only ever
    binds to a REGISTERED gate project with a known, confined root. No raw path
    from a caller can become a project root.
  * AutoDeployBase stays the default project.
  * GitHub import clones ONLY a strictly-validated public github.com URL, or (for
    a private repo) a CodeNexus-OWNED credential (env deploy-token) — NEVER a
    caller-supplied token, and never ChatGPT's GitHub connector token. A private
    repo without CodeNexus credentials fails closed.
  * Selectors are read/registry surfaces; mutation (edit) still requires the
    tier_m_edit grant AND the selected project to be registered+trusted+indexed.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Strict github.com repo URL — prevents SSRF / arbitrary-host clone.
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$",
)
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

# Env var holding a CodeNexus-OWNED GitHub token/deploy credential (PAT or
# GitHub-App installation token). NEVER a caller/ChatGPT token.
_CODENEXUS_GIT_TOKEN_ENV = "CODENEXUS_GIT_TOKEN"


def _iso_now() -> str:
    return datetime.fromtimestamp(time.time(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ProjectError(Exception):
    blocked_by: str
    reason: str = ""

    def __str__(self) -> str:
        return self.reason or self.blocked_by


def validate_github_url(url: str) -> tuple[str, str]:
    """Return (owner, repo) for a strict public github.com URL, else raise."""
    m = _GITHUB_URL_RE.match(str(url or "").strip())
    if not m:
        raise ProjectError("bad_github_url", "only https://github.com/<owner>/<repo> is accepted")
    return m.group("owner"), m.group("repo")


class GateProjectStore:
    """Registry of gate projects + per-token current selection (identity DB)."""

    def __init__(self, projects_base: Path | str | None = None) -> None:
        # Where imported repos are cloned. Default sibling of the auth home.
        self._base = Path(projects_base) if projects_base else None

    def db_path(self, project_root: Path) -> Path:
        from .identity_store import IdentityStore

        return IdentityStore().db_path(project_root)

    def init_db(self, home: Path) -> None:
        from .identity_store import IdentityStore

        IdentityStore().init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gate_projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'local',
                    github_url TEXT NOT NULL DEFAULT '',
                    github_ref TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gate_selection (
                    token_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """,
            )
            conn.commit()

    # ── registration ───────────────────────────────────────────────────────
    def ensure_default(self, home: Path, *, name: str, root: Path) -> str:
        """Register the default project (AutoDeployBase) idempotently; returns id."""
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT project_id FROM gate_projects WHERE is_default=1").fetchone()
            if row:
                return str(row["project_id"])
            pid = "ogp_default"
            conn.execute(
                "INSERT INTO gate_projects (project_id, name, root, source, "
                "is_default, created_at) VALUES (?,?,?,?,1,?)",
                (pid, name, str(Path(root).resolve()), "local", _iso_now()),
            )
            conn.commit()
            return pid

    def register(
        self,
        home: Path,
        *,
        name: str,
        root: Path,
        source: str = "local",
        github_url: str = "",
        github_ref: str = "",
    ) -> dict:
        self.init_db(home)
        pid = "ogp_" + secrets.token_hex(8)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.execute(
                "INSERT INTO gate_projects (project_id, name, root, source, "
                "github_url, github_ref, created_at) VALUES (?,?,?,?,?,?,?)",
                (pid, name, str(Path(root).resolve()), source, github_url, github_ref, _iso_now()),
            )
            conn.commit()
        return self.get(home, pid)

    def get(self, home: Path, project_id: str) -> dict | None:
        if not project_id:
            return None
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM gate_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def list(self, home: Path) -> list[dict]:
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM gate_projects ORDER BY is_default DESC, created_at",
                ).fetchall()
            ]

    def default_id(self, home: Path) -> str | None:
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            row = conn.execute("SELECT project_id FROM gate_projects WHERE is_default=1").fetchone()
        return str(row[0]) if row else None

    # ── per-token selection ──────────────────────────────────────────────────
    def current(self, home: Path, token_id: str) -> dict | None:
        """The token's selected project, or the default. Forged/stale selection
        falls back to default (never to an unregistered root).
        """
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.row_factory = sqlite3.Row
            sel = conn.execute(
                "SELECT project_id, session_id FROM gate_selection WHERE token_id=?",
                (token_id,),
            ).fetchone()
        pid = sel["project_id"] if sel else None
        proj = self.get(home, pid) if pid else None
        if proj is None:
            did = self.default_id(home)
            proj = self.get(home, did) if did else None
        if proj is not None and sel is not None:
            proj = {**proj, "session_id": sel["session_id"]}
        return proj

    def select(self, home: Path, token_id: str, project_id: str) -> dict:
        proj = self.get(home, project_id)
        if proj is None:
            raise ProjectError("unknown_project", f"no such gate project: {project_id}")
        self.init_db(home)
        with sqlite3.connect(str(self.db_path(home))) as conn:
            # Switching projects clears any prior session_id — the old session
            # belonged to a different project root.
            conn.execute(
                "INSERT INTO gate_selection (token_id, project_id, session_id, "
                "updated_at) VALUES (?,?,'',?) ON CONFLICT(token_id) DO UPDATE "
                "SET project_id=excluded.project_id, session_id='', "
                "updated_at=excluded.updated_at",
                (token_id, project_id, _iso_now()),
            )
            conn.commit()
        return proj

    def select_session(self, home: Path, token_id: str, session_id: str) -> None:
        self.init_db(home)
        # A project must be explicitly selected first.
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.row_factory = sqlite3.Row
            sel = conn.execute(
                "SELECT project_id FROM gate_selection WHERE token_id=?",
                (token_id,),
            ).fetchone()
        pid = (sel["project_id"] if sel else "") or ""
        if not pid:
            raise ProjectError(
                "project_required",
                "session_select requires an explicit project "
                "selection first; call project_select first",
            )
        proj = self.get(home, pid)
        if proj is None:
            raise ProjectError(
                "project_required",
                "the selected project no longer exists; call project_select first",
            )
        # PHANTOM-BINDING GUARD (2026-06-13): bind ONLY a session that actually
        # exists as a workspace dir under the selected project — the same
        # existence test session_list/session_create use. Without this,
        # select_session bound any string, so session_current echoed a session
        # (e.g. 'gpt') that session_list never showed — a phantom that erodes
        # trust in the whole session surface. Mirrors how select() validates
        # the project id.
        sess_dir = Path(proj["root"]) / ".MEMORY" / "sessions" / session_id
        if not session_id or not sess_dir.is_dir():
            raise ProjectError(
                "unknown_session",
                f"no such session {session_id!r} in project "
                f"{proj.get('name', pid)!r}. Create it with session_create, or "
                f"pick one from session_list — session_select binds an EXISTING "
                f"session only.",
            )
        with sqlite3.connect(str(self.db_path(home))) as conn:
            conn.execute(
                "UPDATE gate_selection SET session_id=?, updated_at=? WHERE token_id=?",
                (session_id, _iso_now(), token_id),
            )
            conn.commit()


# ── project status (trust/index) — reuses the read-executor checks ───────────
def project_status(root: Path) -> dict:
    from .outer_gate_executor import _index_db_path, _index_populated, _is_aidocs_project, _is_stale

    root = Path(root)
    aidocs = _is_aidocs_project(root)
    populated = _index_populated(root) if aidocs else False
    stale = _is_stale(root) if populated else False
    rows = 0
    if populated:
        try:
            con = sqlite3.connect(f"file:{_index_db_path(root)}?mode=ro", uri=True)
            try:
                rows = int(con.execute("SELECT COUNT(*) FROM code_files").fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error:
            rows = 0
    return {
        "is_aidocs": aidocs,
        "indexed": populated,
        "stale": stale,
        "code_files": rows,
        "ready": bool(aidocs and populated and not stale),
    }


# ── GitHub import (public, or CodeNexus-owned creds only) ────────────────────
def _git(
    args: list[str],
    *,
    cwd: Path | None = None,
    token: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt for credentials
    env["GCM_INTERACTIVE"] = "never"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def register_from_github_url(
    store: GateProjectStore,
    home: Path,
    *,
    url: str,
    ref: str | None,
    projects_base: Path,
    allow_private: bool = True,
    credential: str | None = None,
    allow_env_fallback: bool = True,
) -> dict:
    """Clone a PUBLIC github.com repo (or a private one ONLY with a CodeNexus-owned
    token from the environment), register it, and return the project. Never uses a
    caller-supplied credential.
    """
    owner, repo = validate_github_url(url)
    if ref is not None and not _REF_RE.match(str(ref)):
        raise ProjectError("bad_ref", "invalid git ref")
    projects_base = Path(projects_base)
    projects_base.mkdir(parents=True, exist_ok=True)
    pid = "ogp_" + secrets.token_hex(8)
    dest = (projects_base / pid).resolve()
    # confinement: dest must be inside projects_base
    if projects_base.resolve() not in dest.parents and dest != projects_base.resolve():
        raise ProjectError("bad_path", "clone dest escapes projects base")
    clean_url = f"https://github.com/{owner}/{repo}.git"
    args = ["clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    # 1) try PUBLIC (no credentials).
    r = _git([*args, clean_url, str(dest)])
    if r.returncode != 0:
        # 2) private? Use the per-tenant ORG credential (resolved JIT by the
        #    transport from the bound org / tenant_id). The shared platform env token
        #    is a LEGACY/local fallback only — when a tenant is bound the transport
        #    passes allow_env_fallback=False, so one org can never clone with another
        #    tenant's (or the platform's) credentials. Never a caller-supplied token.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        token = (credential or "").strip()
        if not token and allow_env_fallback:
            token = os.environ.get(_CODENEXUS_GIT_TOKEN_ENV, "").strip()
        if not (allow_private and token):
            raise ProjectError(
                "repo_inaccessible",
                "public clone failed and no GitHub credential is available for this "
                "org (connect a per-org GitHub credential; a caller/ChatGPT token is "
                "never used)",
            )
        auth_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        r2 = _git([*args, auth_url, str(dest)])
        if r2.returncode != 0:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            raise ProjectError("repo_inaccessible", "clone failed even with CodeNexus credentials")
    # Bootstrap + index the clone into a USABLE AIDOCS project. Fail-closed: if
    # init/index fails or the repo has no supported source, remove the clone and
    # do NOT register a broken/unusable project.
    try:
        bootstrap_and_index(dest)
    except ProjectError:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise
    name = f"{owner}/{repo}" + (f"@{ref}" if ref else "")
    return store.register(
        home,
        name=name,
        root=dest,
        source="github",
        github_url=clean_url,
        github_ref=ref or "",
    )


def bootstrap_and_index(root: Path) -> dict:
    """Make a freshly-cloned tree a USABLE AIDOCS project: write the minimal
    marker, run the canonical full-tree indexer, and verify readiness
    (marker + populated index + correct schema version + non-stale). Fail-closed
    at every stage — a repo with no supported source, an init failure, or an
    index failure NEVER yields a 'ready' project.
    """
    root = Path(root)
    # 1) minimal AIDOCS marker (the same path AIDOCS detection requires).
    try:
        d = root / ".MEMORY" / ".aidocs"
        d.mkdir(parents=True, exist_ok=True)
        marker = d / "index.aidocs"
        if not marker.exists():
            marker.write_text("# AIDOCS Session Entry\n", encoding="utf-8")
    except OSError as e:
        raise ProjectError("init_failed", f"could not initialize AIDOCS marker: {e}")
    # 2) canonical full-tree index (also creates the index DB + index_meta).
    try:
        from .code_index_store import CodeIndexStore

        rows = int(CodeIndexStore().sync_code_files(root, paths=None))
    except Exception as e:  # noqa: BLE001 — any indexer failure fails closed
        raise ProjectError("index_failed", f"indexer failed: {e}")
    # 3) verify readiness — ready ONLY when all checks pass.
    st = project_status(root)
    if not st["is_aidocs"]:
        raise ProjectError("init_failed", "marker missing after init")
    if not st["indexed"] or st["code_files"] <= 0:
        raise ProjectError(
            "index_empty",
            "no supported source files indexed (repo has nothing to index)",
        )
    if st["stale"]:
        raise ProjectError("project_index_stale", "index reported stale after build")
    return {"ready": True, "rows": rows, "code_files": st["code_files"]}


@dataclass(frozen=True)
class GrantDecision:
    allow: bool
    reason: str = ""


def resolve_execution_grant(*, principal: dict, project: dict | None, kind: str) -> GrantDecision:
    """Deny-by-default execution-grant resolution — the gate's dashboard-grant.
    A grant is the operator-minted scoped token (role + scope) resolved against
    the CURRENTLY-SELECTED, registered, ready project. Anything missing/expired/
    revoked/stale/untrusted/unindexed/wrong-scope ⇒ deny. ``kind`` ∈ {read, edit}.

    (Token expiry/revocation is enforced upstream by OuterGateTokenStore.validate;
    a revoked/expired token never produces a principal, so it can never grant.)
    """
    if not isinstance(principal, dict) or not principal.get("user_id"):
        return GrantDecision(False, "no_principal")
    if project is None:
        return GrantDecision(False, "no_project_selected")
    scope = set(principal.get("scope") or [])
    role = str(principal.get("effective_role") or "").strip().lower()
    st = project_status(Path(project["root"]))
    if not st["is_aidocs"]:
        return GrantDecision(False, "project_untrusted")
    if not st["indexed"]:
        return GrantDecision(False, "project_unindexed")
    if st["stale"]:
        return GrantDecision(False, "project_index_stale")
    if kind == "read":
        if "tier_r_invoke" not in scope:
            return GrantDecision(False, "insufficient_scope")
        return GrantDecision(True)
    if kind == "edit":
        from .outer_gate_edit import EDIT_ROLES

        if "tier_m_edit" not in scope:
            return GrantDecision(False, "insufficient_scope")
        if role not in EDIT_ROLES:
            return GrantDecision(False, "edit_role_required")
        return GrantDecision(True)
    if kind == "run":
        # project_run only (NOT tier_m_edit / project_import). The shell tool's
        # own law (bash_policy/heuristic_judge/freeze/destructive floor) gates the
        # COMMAND; this gate gates ACCESS to the runner + project binding.
        if "project_run" not in scope:
            return GrantDecision(False, "insufficient_scope")
        return GrantDecision(True)
    return GrantDecision(False, "unknown_grant_kind")


def sync_project(store: GateProjectStore, home: Path, project_id: str) -> dict:
    proj = store.get(home, project_id)
    if proj is None:
        raise ProjectError("unknown_project", f"no such gate project: {project_id}")
    if proj["source"] != "github":
        raise ProjectError("not_syncable", "only github-sourced projects sync")
    root = Path(proj["root"])
    if not (root / ".git").is_dir():
        raise ProjectError("repo_inaccessible", "project is not a git checkout")
    token = os.environ.get(_CODENEXUS_GIT_TOKEN_ENV, "").strip()
    # set-url with token only if present (private); else plain fetch (public).
    r = _git(["fetch", "--depth", "1", "origin"], cwd=root, token=token)
    if r.returncode != 0:
        raise ProjectError("repo_inaccessible", f"fetch failed: {r.stderr[:200]}")
    ref = proj["github_ref"] or "HEAD"
    rr = _git(
        ["reset", "--hard", f"origin/{ref}" if proj["github_ref"] else "FETCH_HEAD"],
        cwd=root,
    )
    if rr.returncode != 0:
        raise ProjectError("repo_inaccessible", f"reset failed: {rr.stderr[:200]}")
    # Re-bootstrap + re-index after sync so the project stays ready (fail-closed).
    bi = bootstrap_and_index(root)
    return {
        "ok": True,
        "project_id": project_id,
        "synced_at": _iso_now(),
        "code_files": bi["code_files"],
    }
