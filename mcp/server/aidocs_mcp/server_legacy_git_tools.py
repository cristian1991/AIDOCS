from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .mcp_server_runtime_helpers import (
    current_calling_host_session_id,
    resolve_project_root,
)
from .project_authority import require_admin

# Above this many commits of divergence, git_diag skips the expensive
# per-file fast-path diff and reports a "large divergence" summary instead
# (the diff would be huge and slow). Was referenced but never defined
# (F821 / NameError whenever divergence exceeded it); pinned here.
_GIT_FAST_DIVERGENCE = 1000

# Truth label (#65, Empire 2026-06-20): git_fork_status / git_merge_plan compute
# ahead/behind via `rev-list local...upstream` against the LOCAL tracking ref and DO
# NOT fetch, so the counts can be stale vs the live remote. Every result carries this
# basis so a reader never mistakes a local-ref delta for a live-remote one.
_AHEAD_BEHIND_BASIS = (
    "ahead/behind computed vs the LOCAL tracking ref (no fetch performed) — may be "
    "stale relative to the live remote; run `git fetch` for live counts"
)

_DML_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT"})
_DDL_KEYWORDS = frozenset({"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT"})
_READ_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN", "SHOW", "VALUES"})
_PRIVILEGED_KEYWORDS = frozenset({"GRANT", "REVOKE", "VACUUM", "ANALYZE", "CLUSTER", "REINDEX"})

_PG_COMMAND_TAG_RE = re.compile(
    r"^(?P<tag>INSERT|UPDATE|DELETE|MERGE|COPY)\s+(?:\d+\s+)?(?P<rows>\d+)\s*$",
    re.MULTILINE,
)

_PG_REQUIRED_KEYS = ("host", "port", "database", "username", "password")


class PgConnectionResolutionError(Exception):
    def __init__(self, message: str, searched: list[str]):
        super().__init__(message)
        self.searched = searched


def _parse_dotnet_connection_string(conn_str: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in conn_str.split(";"):
        kv = part.strip().split("=", 1)
        if len(kv) != 2:
            continue
        key, val = kv[0].strip().lower(), kv[1].strip()
        if key == "host" or key == "server":
            parsed["host"] = val
        elif key in ("database", "db", "initial catalog"):
            parsed["database"] = val
        elif key in ("username", "user id", "user", "uid"):
            parsed["username"] = val
        elif key in ("password", "pwd"):
            parsed["password"] = val
        elif key == "port":
            parsed["port"] = val
    return parsed


def _parse_postgres_url(url: str) -> dict[str, str]:
    from urllib.parse import unquote, urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return {}
    if parsed.scheme not in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
        return {}
    out: dict[str, str] = {}
    if parsed.hostname:
        out["host"] = parsed.hostname
    if parsed.port:
        out["port"] = str(parsed.port)
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    if parsed.path and parsed.path.lstrip("/"):
        out["database"] = parsed.path.lstrip("/").split("?", 1)[0]
    return out


def _parse_any_connection_string(conn_str: str) -> dict[str, str]:
    stripped = conn_str.strip()
    if stripped.startswith(("postgres://", "postgresql://", "postgresql+")):
        return _parse_postgres_url(stripped)
    return _parse_dotnet_connection_string(stripped)


def _scan_env_files(root: Path) -> tuple[dict[str, str], str | None]:
    env_names = (".env", ".env.local", ".env.development", ".env.dev")
    for name in env_names:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        url_match: str | None = None
        discrete: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            key = k.strip().upper()
            val = v.strip().strip('"').strip("'")
            if key in ("DATABASE_URL", "POSTGRES_URL", "PG_URL"):
                url_match = val
            elif key in ("PGHOST", "POSTGRES_HOST", "DB_HOST"):
                discrete["host"] = val
            elif key in ("PGPORT", "POSTGRES_PORT", "DB_PORT"):
                discrete["port"] = val
            elif key in ("PGDATABASE", "POSTGRES_DB", "POSTGRES_DATABASE", "DB_NAME"):
                discrete["database"] = val
            elif key in ("PGUSER", "POSTGRES_USER", "DB_USER"):
                discrete["username"] = val
            elif key in ("PGPASSWORD", "POSTGRES_PASSWORD", "DB_PASSWORD", "DB_PASS"):
                discrete["password"] = val
        if url_match:
            parsed = _parse_postgres_url(url_match)
            if parsed:
                return (parsed, f"{name} (DATABASE_URL)")
        if discrete:
            return (discrete, f"{name} (discrete PG* vars)")
    return ({}, None)


def _scan_dotnet_appsettings(root: Path) -> tuple[dict[str, str], str | None]:
    import json as json_mod

    for settings_name in ("appsettings.Development.json", "appsettings.json"):
        for candidate in root.rglob(settings_name):
            try:
                settings = json_mod.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            conn_strings = settings.get("ConnectionStrings") or {}
            for key_name in ("DefaultConnection", "PostgresConnection", "PgConnection"):
                raw = conn_strings.get(key_name)
                if raw:
                    parsed = _parse_any_connection_string(raw)
                    if parsed:
                        rel = (
                            candidate.relative_to(root)
                            if candidate.is_relative_to(root)
                            else candidate
                        )
                        return (parsed, f"{rel} ({key_name})")
    return ({}, None)


def _scan_alembic_ini(root: Path) -> tuple[dict[str, str], str | None]:
    for candidate in root.rglob("alembic.ini"):
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("sqlalchemy.url"):
                _, _, val = stripped.partition("=")
                parsed = _parse_postgres_url(val.strip())
                if parsed:
                    rel = (
                        candidate.relative_to(root) if candidate.is_relative_to(root) else candidate
                    )
                    return (parsed, f"{rel} (sqlalchemy.url)")
    return ({}, None)


def _resolve_pg_connection(
    root: Path,
    override: str | None,
    *,
    config_store: Any = None,
) -> tuple[dict[str, str], str]:
    searched: list[str] = []

    if override:
        parsed = _parse_any_connection_string(override)
        missing = [k for k in _PG_REQUIRED_KEYS if k not in parsed]
        if missing:
            raise PgConnectionResolutionError(
                f"connection_string is missing required fields: {', '.join(missing)}",
                searched=["argument:connection_string"],
            )
        return (parsed, "argument:connection_string")

    if config_store is None:
        try:
            from .config_store import ConfigStore

            config_store = ConfigStore()
        except Exception:
            config_store = None

    if config_store is not None:
        searched.append("aidocs config: pg.connection_string")
        try:
            configured = config_store.get_effective(root, "pg.connection_string", default=None)
        except Exception:
            configured = None
        if configured:
            parsed = _parse_any_connection_string(str(configured))
            missing = [k for k in _PG_REQUIRED_KEYS if k not in parsed]
            if not missing:
                return (parsed, "aidocs config: pg.connection_string")

    searched.append(".NET appsettings.json / appsettings.Development.json")
    parsed, source = _scan_dotnet_appsettings(root)
    if parsed and not [k for k in _PG_REQUIRED_KEYS if k not in parsed]:
        return (parsed, source or "appsettings")

    searched.append(".env / .env.local / .env.development")
    parsed, source = _scan_env_files(root)
    if parsed and not [k for k in _PG_REQUIRED_KEYS if k not in parsed]:
        return (parsed, source or ".env")

    searched.append("alembic.ini (sqlalchemy.url)")
    parsed, source = _scan_alembic_ini(root)
    if parsed and not [k for k in _PG_REQUIRED_KEYS if k not in parsed]:
        return (parsed, source or "alembic.ini")

    raise PgConnectionResolutionError(
        "No PostgreSQL connection configured for this project. Set one via "
        "`aidocs config set pg.connection_string '<url>'` (project or global scope), "
        "pass connection_string= explicitly, or add it to appsettings.json / .env / alembic.ini.",
        searched=searched,
    )


def _classify_sql(sql: str) -> tuple[str, str]:
    stripped = sql.strip().lstrip("(").strip()
    tokens = stripped.split()
    if not tokens:
        return ("empty", "")
    first = tokens[0].upper().rstrip(";")
    if first in _READ_KEYWORDS:
        return ("read", first)
    if first in _DML_KEYWORDS:
        return ("dml", first)
    if first in _DDL_KEYWORDS:
        return ("ddl", first)
    if first in _PRIVILEGED_KEYWORDS:
        return ("privileged", first)
    return ("unknown", first)


def _run_psql(
    conn: dict[str, str],
    sql: str,
    *,
    tuples_only: bool,
    timeout: int = 30,
) -> tuple[int, str, str]:
    import os
    import subprocess
    import tempfile

    args = [
        "psql",
        "-h",
        conn["host"],
        "-p",
        conn["port"],
        "-U",
        conn["username"],
        "-d",
        conn["database"],
        "-v",
        "ON_ERROR_STOP=1",
    ]
    if tuples_only:
        args += ["-t", "-A", "-F", "\t"]
    args += ["-c", sql]

    env = {**os.environ, "PGPASSWORD": conn["password"]}
    out_path = err_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".db.out", delete=False) as f:
            out_path = f.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".db.err", delete=False) as f:
            err_path = f.name
        with (
            open(out_path, "w", encoding="utf-8") as out_fh,
            open(err_path, "w", encoding="utf-8") as err_fh,
        ):
            # #345: routed through audited_run (ledger row per spawn); kwargs UNCHANGED.
            from .shell_egress_service import audited_run

            result = audited_run(
                args,
                fingerprint=("server_legacy_git_tools.py", "_run_psql", "subprocess.run"),
                reason="legacy-psql-exec",
                run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
                stdout=out_fh,
                stderr=err_fh,
                text=True,
                timeout=timeout,
                # #333 Phase 2: daemon runs console-less (pythonw); without
                # this flag every spawn allocates a NEW visible console window.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
        stdout = Path(out_path).read_text(encoding="utf-8", errors="ignore")
        stderr = Path(err_path).read_text(encoding="utf-8", errors="ignore")
        return (result.returncode, stdout, stderr)
    finally:
        for p in (out_path, err_path):
            if p:
                try:
                    Path(p).unlink()
                except OSError:
                    pass


def _extract_affected_rows(stdout: str) -> int | None:
    total = 0
    matched = False
    for m in _PG_COMMAND_TAG_RE.finditer(stdout):
        total += int(m.group("rows"))
        matched = True
    return total if matched else None


def register_legacy_git_tools(
    *,
    server: Any,
    hub: Any,
    runtime: Any,
    resolve_related_root: Any,
    timed_git_async: Any,
    run_git_async: Any,
    git_timeout: int,
) -> None:
    from .tool_display import renders_as


    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Legacy Build Session Proposal",
        },
    )
    @renders_as("status", title="session proposal")
    def legacy_build_session_proposal(session_id: str | None = None) -> Any:
        """Build a non-destructive session proposal from legacy NOW/plans state."""
        return hub.legacy.build_session_proposal(resolve_project_root(), session_id=session_id)

    # ═══════════════════════════════════════════════════════════════════════
    # Database Query Tool
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "openWorldHint": False,
            "title": "Database Query",
        },
    )
    @renders_as("list", title="db query")
    def db_query(
        sql: str,
        connection_string: str | None = None,
        allow_dml: bool = False,
        allow_ddl: bool = False,
        dry_run: bool = True,
        expect_rows: int | None = None,
        max_rows: int = 1000,
    ) -> Any:
        """Execute SQL against the project's PostgreSQL database.

        Read-only by default. Pass allow_dml=True to permit INSERT/UPDATE/DELETE/MERGE,
        allow_ddl=True to permit CREATE/ALTER/DROP/TRUNCATE. Writes run inside a
        transaction that rolls back when dry_run is True (the default). Set
        dry_run=False to commit.

        Args:
            sql: SQL statement. For reads: SELECT/WITH/EXPLAIN. For writes: one statement at a time.
            connection_string: Override connection (.NET-style: 'Host=...;Database=...;Username=...;Password=...').
                               If omitted, reads from appsettings.json or falls back to localhost/dentalapp.
            allow_dml: Permit INSERT/UPDATE/DELETE/MERGE. Default False.
            allow_ddl: Permit CREATE/ALTER/DROP/TRUNCATE. Default False.
            dry_run: When True (default for writes), wrap in a transaction and ROLLBACK. When False, COMMIT.
            expect_rows: If set, fail the write if affected row count differs.
            max_rows: Hard ceiling on affected rows per call. Default 1000.

        """
        import subprocess

        # #300 defense-in-depth: db_query runs ARBITRARY SQL/DML/DDL and its
        # only guards are caller-controlled self-flags (allow_dml/allow_ddl) —
        # the outer_gate manifest explicitly flags 'no AIDOCS auth', and today
        # the sole real barrier is dashboard-mode registration (#291). Require
        # operator-admin authority PER CALL, fail-closed, before ANY DB access:
        # an AUTHENTICATED operator holding manage-config — every flavor,
        # #404 (no local passthrough). Defends against a future
        # registration bug or a lower-privileged dashboard session.
        _auth = require_admin(
            resolve_project_root(),
            operation="db_query",
            host_session_id=current_calling_host_session_id(),
        )
        if not _auth.ok:
            return {
                "error": f"db_query requires operator admin authority: {_auth.reason}",
                "blocked_by": _auth.get("blocked_by", "operator_auth"),
                "rows": [],
            }

        classification, first_word = _classify_sql(sql)
        if classification == "empty":
            return {"error": "Empty SQL statement", "rows": []}
        if classification == "privileged":
            return {
                "error": f"Privileged statement '{first_word}' is not supported via db_query",
                "rows": [],
            }
        if classification == "unknown":
            return {
                "error": f"Unrecognized or unsupported statement: {first_word}",
                "rows": [],
            }
        if classification == "dml" and not allow_dml:
            return {
                "error": f"{first_word} requires allow_dml=True",
                "hint": "Pass allow_dml=True (and dry_run=True first to preview affected rows before committing).",
                "rows": [],
            }
        if classification == "ddl" and not allow_ddl:
            return {
                "error": f"{first_word} requires allow_ddl=True",
                "hint": "Pass allow_ddl=True and confirm dry_run=True produces the expected plan before committing.",
                "rows": [],
            }

        resolved_root = resolve_project_root()
        try:
            conn, connection_source = _resolve_pg_connection(resolved_root, connection_string)
        except PgConnectionResolutionError as exc:
            return {
                "error": str(exc),
                "searched": exc.searched,
                "rows": [],
            }

        try:
            if classification == "read":
                rc, stdout, stderr = _run_psql(conn, sql, tuples_only=True)
                if rc != 0:
                    return {
                        "error": stderr.strip(),
                        "connection_source": connection_source,
                        "database": conn.get("database"),
                        "host": conn.get("host"),
                        "rows": [],
                    }
                lines = [line for line in stdout.split("\n") if line.strip()]
                return {
                    "row_count": len(lines),
                    "rows": lines[:200],
                    "connection_source": connection_source,
                    "database": conn.get("database"),
                    "host": conn.get("host"),
                }

            terminator = "ROLLBACK" if dry_run else "COMMIT"
            wrapped = f"BEGIN;\n{sql.rstrip().rstrip(';')};\n{terminator};"
            rc, stdout, stderr = _run_psql(conn, wrapped, tuples_only=False)
            if rc != 0:
                return {
                    "error": stderr.strip() or "psql reported a non-zero exit",
                    "rolled_back": True,
                    "connection_source": connection_source,
                    "database": conn.get("database"),
                    "host": conn.get("host"),
                    "rows": [],
                }

            affected = _extract_affected_rows(stdout)
            if affected is not None and affected > max_rows:
                return {
                    "error": f"Affected rows ({affected}) exceeded max_rows ({max_rows})",
                    "affected_rows": affected,
                    "rolled_back": True,
                    "connection_source": connection_source,
                    "database": conn.get("database"),
                    "host": conn.get("host"),
                    "rows": [],
                    "hint": "Increase max_rows or narrow the WHERE clause. This dry_run would have hit the ceiling.",
                }
            if expect_rows is not None and affected is not None and affected != expect_rows:
                return {
                    "error": f"Affected rows ({affected}) did not match expect_rows ({expect_rows})",
                    "affected_rows": affected,
                    "rolled_back": True,
                    "connection_source": connection_source,
                    "database": conn.get("database"),
                    "host": conn.get("host"),
                    "rows": [],
                }

            response: dict[str, Any] = {
                "statement": first_word,
                "classification": classification,
                "affected_rows": affected,
                "dry_run": dry_run,
                "committed": (not dry_run),
                "rolled_back": dry_run,
                "connection_source": connection_source,
                "database": conn.get("database"),
                "host": conn.get("host"),
                "rows": [],
            }
            if classification == "ddl":
                response["warning"] = f"DDL operation: {first_word}. Schema change was " + (
                    "simulated (rolled back)." if dry_run else "committed."
                )
            return response
        except FileNotFoundError:
            return {
                "error": "psql not found — install PostgreSQL client tools",
                "rows": [],
            }
        except subprocess.TimeoutExpired:
            return {"error": "Query timed out after 30 seconds", "rows": []}
        except Exception as exc:
            return {"error": str(exc), "rows": []}

    # ═══════════════════════════════════════════════════════════════════════
    # Git Analysis Tools
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool()
    async def git_diag(upstream: str = "upstream/main", local: str = "HEAD") -> dict[str, Any]:
        """Run a minimal git diagnostic inside the live MCP server process.

        Helps distinguish raw git problems from MCP runtime/process issues.
        """
        import os
        import platform
        import threading
        import time

        start = time.perf_counter()
        root = resolve_project_root()
        # Capture the AIDOCS project root BEFORE the .git child-walk below may
        # rebind `root` to a nested git repo — the two diag fields are distinct
        # (project_root vs git_root). `project_root` was referenced in the
        # result dict but never assigned (F821 / NameError on every git_diag).
        project_root = str(root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        try:
            merge_base = await run_git_async(
                str(root),
                "merge-base",
                local,
                upstream,
                timeout=git_timeout,
            )
            elapsed = round(time.perf_counter() - start, 3)
            return {
                "ok": True,
                "project_root": project_root,
                "git_root": str(root),
                "local_ref": local,
                "upstream_ref": upstream,
                "merge_base": merge_base[:40],
                "elapsed_seconds": elapsed,
                "runtime": {
                    "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "git_timeout": git_timeout,
                    "cwd": os.getcwd(),
                },
            }
        except Exception as exc:
            elapsed = round(time.perf_counter() - start, 3)
            return {
                "ok": False,
                "project_root": project_root,
                "git_root": str(root),
                "local_ref": local,
                "upstream_ref": upstream,
                "elapsed_seconds": elapsed,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "runtime": {
                    "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "git_timeout": git_timeout,
                    "cwd": os.getcwd(),
                },
            }

    @server.tool()
    @timed_git_async
    async def git_fork_status(
        upstream: str = "upstream/main",
        local: str = "HEAD",
        include_files: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Analyze the state of a fork vs upstream: how far behind, how many local changes, conflict risk.

        START HERE for fork/merge tasks. Returns commit counts and conflict predictions.
        Set include_files=True for full file lists (slower on large repos).
        Auto-detects the git root if project_root isn't one.

        Args:
            upstream: Upstream ref to compare against (e.g., "upstream/main", "upstream/dev").
            local: Local ref (default: HEAD).
            include_files: Include file-level details (slower). Default: False for fast overview.
            timeout: Optional seconds; tighten-only — effective = min(timeout,
                tools.git_functions_timeout knob). 0/absent = knob value.

        """
        import time

        start = time.perf_counter()
        step = "init"
        times: dict[str, float] = {}

        def mark(name: str) -> None:
            times[name] = round(time.perf_counter() - start, 3)

        # Find git root — check project_root itself first, then one level down
        root = resolve_project_root()
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break
        mark("root")

        async def git(*args: str, timeout: int = git_timeout) -> str:
            return await run_git_async(str(root), *args, timeout=timeout)

        try:
            # Merge base first
            step = "merge_base"
            merge_base = await git("merge-base", local, upstream, timeout=git_timeout)
            mark(step)
            if not merge_base:
                return {
                    "error": f"No merge base found between {local} and {upstream}. Run 'git fetch upstream' first.",
                    "debug": {"step": step, "times": times},
                }

            step = "counts"
            counts = await git(
                "rev-list",
                "--left-right",
                "--count",
                f"{local}...{upstream}",
                timeout=git_timeout,
            )
            mark(step)
            parts = counts.split()
            ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
            behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            divergence = ahead + behind

            result: dict[str, Any] = {
                "git_root": str(root),
                "merge_base": merge_base[:12],
                "behind": behind,
                "ahead": ahead,
                "local_ref": local,
                "upstream_ref": upstream,
                "divergence": divergence,
                "comparison_basis": _AHEAD_BEHIND_BASIS,
            }

            # File-level details (optional — can be slow on large repos)
            if include_files:
                step = "local_diff"
                local_changed = [
                    l
                    for l in (
                        await git(
                            "diff",
                            "--name-only",
                            "--no-renames",
                            merge_base,
                            local,
                            timeout=git_timeout,
                        )
                    ).splitlines()
                    if l.strip()
                ]
                mark(step)
                step = "upstream_diff"
                upstream_changed = [
                    l
                    for l in (
                        await git(
                            "diff",
                            "--name-only",
                            "--no-renames",
                            merge_base,
                            upstream,
                            timeout=git_timeout,
                        )
                    ).splitlines()
                    if l.strip()
                ]
                mark(step)
                local_set = set(local_changed)
                upstream_set = set(upstream_changed)
                conflict_candidates = sorted(local_set & upstream_set)

                result.update(
                    {
                        "local_stat": f"{len(local_changed)} files changed (exact)",
                        "upstream_stat": f"{len(upstream_changed)} files changed (exact)",
                        "local_changed_files": len(local_changed),
                        "upstream_changed_files": len(upstream_changed),
                        "conflict_candidates": len(conflict_candidates),
                        "conflict_files": conflict_candidates[:50],
                        "local_only_files": sorted(local_set - upstream_set)[:30],
                        "upstream_only_files": sorted(upstream_set - local_set)[:30],
                    },
                )
            elif divergence > _GIT_FAST_DIVERGENCE:
                step = "fast_path"
                result.update(
                    {
                        "local_stat": f"skipped fast-path due to large divergence ({divergence} commits)",
                        "upstream_stat": f"skipped fast-path due to large divergence ({divergence} commits)",
                        "local_changed_files_approx": None,
                        "upstream_changed_files_approx": None,
                        "note": (
                            "Fast path used for a large branch gap. "
                            "Set include_files=True for exact file lists, or narrow the comparison."
                        ),
                    },
                )
                mark(step)
            else:
                step = "local_shortstat"
                local_stat = await git(
                    "diff",
                    "--shortstat",
                    "--no-renames",
                    merge_base,
                    local,
                    timeout=git_timeout,
                )
                mark(step)
                step = "upstream_shortstat"
                upstream_stat = await git(
                    "diff",
                    "--shortstat",
                    "--no-renames",
                    merge_base,
                    upstream,
                    timeout=git_timeout,
                )
                mark(step)
                # Estimate file counts from shortstat (fast)
                import re as _re

                local_files = (
                    int(m.group(1)) if (m := _re.search(r"(\d+) files? changed", local_stat)) else 0
                )
                upstream_files = (
                    int(m.group(1))
                    if (m := _re.search(r"(\d+) files? changed", upstream_stat))
                    else 0
                )
                result.update(
                    {
                        "local_stat": local_stat or "no changes",
                        "upstream_stat": upstream_stat or "no changes",
                        "local_changed_files_approx": local_files,
                        "upstream_changed_files_approx": upstream_files,
                        "note": "Set include_files=True for file lists and conflict prediction (slower)",
                    },
                )

            result["summary"] = (
                f"{behind} commits behind, {ahead} ahead. "
                f"Local: {result.get('local_stat', 'n/a')}. "
                f"Upstream: {result.get('upstream_stat', 'n/a')}."
            )
            mark("done")
            result["debug"] = {"step": step, "times": times}
            return result
        except TimeoutError as exc:
            mark("timeout")
            return {"error": str(exc), "debug": {"step": step, "times": times}}
        except Exception as exc:
            mark("error")
            return {"error": str(exc), "debug": {"step": step, "times": times}}

    @server.tool()
    @timed_git_async
    async def git_upstream_changes(
        upstream: str = "upstream/main",
        path_filter: str | None = None,
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Summarize what changed upstream since the fork diverged.

        Groups changes by directory/module and shows commit messages.

        Args:
            upstream: Upstream ref.
            path_filter: Only show changes in this path (e.g., "packages/opencode/src/session/").
            timeout: Optional seconds; tighten-only — effective = min(timeout,
                tools.git_functions_timeout knob). 0/absent = knob value.

        """
        root = resolve_project_root()
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        async def git(*args: str, timeout: int = git_timeout) -> str:
            return await run_git_async(str(root), *args, timeout=timeout)

        try:
            merge_base = await git("merge-base", "HEAD", upstream)

            # Get commits with a clear separator format for reliable parsing
            log_args = [
                "log",
                "--format=COMMIT:%h %s",
                "--name-only",
                f"{merge_base}..{upstream}",
            ]
            if path_filter:
                log_args.extend(["--", path_filter])
            log_args.append(f"-{limit}")
            raw = await git(*log_args)

            commits: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("COMMIT:"):
                    if current:
                        commits.append(current)
                    rest = stripped[7:]
                    parts = rest.split(" ", 1)
                    current = {
                        "hash": parts[0],
                        "message": parts[1] if len(parts) > 1 else "",
                        "files": [],
                    }
                elif current:
                    current["files"].append(stripped)
            if current:
                commits.append(current)

            # Group files by top-level directory
            dir_changes: dict[str, int] = {}
            for c in commits:
                for f in c["files"]:
                    top = f.split("/")[0] if "/" in f else "(root)"
                    dir_changes[top] = dir_changes.get(top, 0) + 1

            return {
                "merge_base": merge_base,
                "commit_count": len(commits),
                "commits": commits[:limit],
                "changes_by_directory": dict(sorted(dir_changes.items(), key=lambda x: -x[1])[:20]),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @server.tool()
    @timed_git_async
    async def git_conflict_analysis(
        file_path: str,
        upstream: str = "upstream/main",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Deep analysis of a single file that will likely conflict during merge.

        Shows what changed locally vs upstream, with line-level diff context.

        Args:
            file_path: The file to analyze.
            upstream: Upstream ref.
            timeout: Optional seconds; tighten-only — effective = min(timeout,
                tools.git_functions_timeout knob). 0/absent = knob value.

        """
        root = resolve_project_root()
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        async def git(*args: str, timeout: int = git_timeout) -> str:
            return await run_git_async(str(root), *args, timeout=timeout)

        try:
            merge_base = await git("merge-base", "HEAD", upstream)

            local_diff = await git("diff", merge_base, "HEAD", "--", file_path)
            upstream_diff = await git("diff", merge_base, upstream, "--", file_path)

            # Count changes
            local_adds = sum(
                1 for l in local_diff.splitlines() if l.startswith("+") and not l.startswith("+++")
            )
            local_dels = sum(
                1 for l in local_diff.splitlines() if l.startswith("-") and not l.startswith("---")
            )
            upstream_adds = sum(
                1
                for l in upstream_diff.splitlines()
                if l.startswith("+") and not l.startswith("+++")
            )
            upstream_dels = sum(
                1
                for l in upstream_diff.splitlines()
                if l.startswith("-") and not l.startswith("---")
            )

            # Upstream commits that touched this file
            upstream_commits = await git(
                "log",
                "--oneline",
                f"{merge_base}..{upstream}",
                "--",
                file_path,
            )

            return {
                "file": file_path,
                "merge_base": merge_base,
                "local_changes": {"additions": local_adds, "deletions": local_dels},
                "upstream_changes": {
                    "additions": upstream_adds,
                    "deletions": upstream_dels,
                },
                "upstream_commits": upstream_commits.splitlines()[:20],
                "local_diff": local_diff[:3000] if local_diff else "(no local changes)",
                "upstream_diff": upstream_diff[:3000] if upstream_diff else "(no upstream changes)",
                "recommendation": (
                    "KEEP LOCAL"
                    if not upstream_diff
                    else "TAKE UPSTREAM"
                    if not local_diff
                    else "MANUAL MERGE REQUIRED — both sides changed this file"
                ),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @server.tool()
    @timed_git_async
    async def git_merge_plan(
        upstream: str = "upstream/main",
        local: str = "HEAD",
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Generate a merge plan: which files to keep, which to take from upstream, which need manual merge.

        Args:
            upstream: Upstream ref to merge from.
            timeout: Optional seconds; tighten-only — effective = min(timeout,
                tools.git_functions_timeout knob). 0/absent = knob value.

        """
        import time

        start = time.perf_counter()
        step = "init"
        times: dict[str, float] = {}

        def mark(name: str) -> None:
            times[name] = round(time.perf_counter() - start, 3)

        root = resolve_project_root()
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break
        mark("root")

        async def git(*args: str, timeout: int = git_timeout) -> str:
            return await run_git_async(str(root), *args, timeout=timeout)

        async def git_lines(*args: str, timeout: int = git_timeout) -> list[str]:
            return [l for l in (await git(*args, timeout=timeout)).splitlines() if l.strip()]

        try:
            step = "merge_base"
            merge_base = await git("merge-base", local, upstream, timeout=git_timeout)
            mark(step)
            step = "counts"
            counts = await git(
                "rev-list",
                "--left-right",
                "--count",
                f"{local}...{upstream}",
                timeout=git_timeout,
            )
            mark(step)
            parts = counts.split()
            ahead = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
            behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            divergence = ahead + behind

            step = "local_diff"
            local_changed = set(
                await git_lines(
                    "diff",
                    "--name-only",
                    "--no-renames",
                    merge_base,
                    local,
                    timeout=git_timeout,
                ),
            )
            mark(step)
            step = "upstream_diff"
            upstream_changed = set(
                await git_lines(
                    "diff",
                    "--name-only",
                    "--no-renames",
                    merge_base,
                    upstream,
                    timeout=git_timeout,
                ),
            )
            mark(step)

            keep_local: list[str] = []
            take_upstream: list[str] = []
            manual_merge: list[str] = []

            for f in sorted(local_changed | upstream_changed):
                in_local = f in local_changed
                in_upstream = f in upstream_changed
                if in_local and in_upstream:
                    manual_merge.append(f)
                elif in_local:
                    keep_local.append(f)
                else:
                    take_upstream.append(f)

            return {
                "merge_base": merge_base,
                "local_ref": local,
                "upstream_ref": upstream,
                "divergence": divergence,
                "comparison_basis": _AHEAD_BEHIND_BASIS,
                "mode": "exact",
                "keep_local": keep_local[:limit],
                "keep_local_count": len(keep_local),
                "take_upstream": take_upstream[:limit],
                "take_upstream_count": len(take_upstream),
                "manual_merge": manual_merge[:limit],
                "manual_merge_count": len(manual_merge),
                "strategy": (
                    f"Safe auto-merge: {len(take_upstream)} files (upstream only). "
                    f"Keep as-is: {len(keep_local)} files (local only). "
                    f"Manual review: {len(manual_merge)} files (both changed)."
                ),
                "debug": {"step": step, "times": times},
            }
        except Exception as exc:
            mark("error")
            return {"error": str(exc), "debug": {"step": step, "times": times}}
