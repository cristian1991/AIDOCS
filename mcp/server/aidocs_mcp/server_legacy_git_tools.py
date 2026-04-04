from __future__ import annotations

from pathlib import Path
from typing import Any


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
    def legacy_read_runtime(root: str) -> dict[str, Any]:
        """Inspect legacy NOW/plans state without mutating the project."""
        return hub.legacy.inspect_legacy(Path(root))

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Legacy Build Session Proposal",
        }
    )
    def legacy_build_session_proposal(
        root: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """Build a non-destructive session proposal from legacy NOW/plans state."""
        return hub.legacy.build_session_proposal(Path(root), session_id=session_id)

    # ═══════════════════════════════════════════════════════════════════════
    # Database Query Tool
    # ═══════════════════════════════════════════════════════════════════════

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "title": "Database Query",
        }
    )
    def db_query(
        root: str,
        sql: str,
        connection_string: str | None = None,
    ) -> dict[str, Any]:
        """Execute a read-only SQL query against the project's PostgreSQL database.

        Safety: Only SELECT statements are allowed. DDL/DML (INSERT, UPDATE, DELETE, DROP, etc.) is blocked.

        Args:
            project_root: Project root path (used to auto-detect connection string from appsettings.json).
            sql: SQL query to execute (SELECT only).
            connection_string: Override connection string (format: 'Host=...;Database=...;Username=...;Password=...').
                             If not provided, reads from appsettings.json or defaults to localhost/dentalapp.
        """
        import subprocess, json as json_mod

        # Safety: block non-SELECT statements
        stripped = sql.strip().lstrip("(").strip()
        first_word = stripped.split()[0].upper() if stripped.split() else ""
        if first_word not in ("SELECT", "WITH", "EXPLAIN"):
            return {
                "error": f"Only SELECT/WITH/EXPLAIN queries allowed, got: {first_word}",
                "rows": [],
            }

        # Resolve connection params
        root = Path(root)
        host = "localhost"
        port = "5432"
        database = "dentalapp"
        username = "postgres"
        password = "admin"

        if connection_string:
            # Parse .NET-style connection string
            for part in connection_string.split(";"):
                kv = part.strip().split("=", 1)
                if len(kv) == 2:
                    key, val = kv[0].strip().lower(), kv[1].strip()
                    if key == "host":
                        host = val
                    elif key in ("database", "db"):
                        database = val
                    elif key in ("username", "user id", "user"):
                        username = val
                    elif key == "password":
                        password = val
                    elif key == "port":
                        port = val
        else:
            # Try to read from appsettings.json
            for settings_file in ["appsettings.Development.json", "appsettings.json"]:
                candidates = list(root.rglob(settings_file))
                for candidate in candidates:
                    try:
                        settings = json_mod.loads(
                            candidate.read_text(encoding="utf-8", errors="ignore")
                        )
                        conn_str = (settings.get("ConnectionStrings") or {}).get(
                            "DefaultConnection"
                        )
                        if conn_str:
                            for part in conn_str.split(";"):
                                kv = part.strip().split("=", 1)
                                if len(kv) == 2:
                                    key, val = kv[0].strip().lower(), kv[1].strip()
                                    if key == "host":
                                        host = val
                                    elif key in ("database", "db"):
                                        database = val
                                    elif key in ("username", "user id", "user"):
                                        username = val
                                    elif key == "password":
                                        password = val
                                    elif key == "port":
                                        port = val
                            break
                    except Exception:
                        continue

        env = {**__import__("os").environ, "PGPASSWORD": password}
        try:
            import tempfile as _tf

            _db_out = _db_err = None
            try:
                with _tf.NamedTemporaryFile(
                    mode="w", suffix=".db.out", delete=False
                ) as f:
                    _db_out = f.name
                with _tf.NamedTemporaryFile(
                    mode="w", suffix=".db.err", delete=False
                ) as f:
                    _db_err = f.name
                with open(_db_out, "w") as out_fh, open(_db_err, "w") as err_fh:
                    result = subprocess.run(
                        [
                            "psql",
                            "-h",
                            host,
                            "-p",
                            port,
                            "-U",
                            username,
                            "-d",
                            database,
                            "-t",
                            "-A",
                            "-F",
                            "\t",
                            "-c",
                            sql,
                        ],
                        stdout=out_fh,
                        stderr=err_fh,
                        text=True,
                        timeout=30,
                        env=env,
                    )
                stdout = (
                    Path(_db_out).read_text(encoding="utf-8", errors="ignore").strip()
                )
                stderr = (
                    Path(_db_err).read_text(encoding="utf-8", errors="ignore").strip()
                )
            finally:
                import os as _os

                for p in (_db_out, _db_err):
                    if p:
                        try:
                            _os.unlink(p)
                        except OSError:
                            pass
            if result.returncode != 0:
                return {"error": stderr, "rows": []}

            lines = [line for line in stdout.split("\n") if line.strip()]
            return {"row_count": len(lines), "rows": lines[:200]}
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
    async def git_diag(
        root: str,
        upstream: str = "upstream/main",
        local: str = "HEAD",
    ) -> dict[str, Any]:
        """Run a minimal git diagnostic inside the live MCP server process.

        Helps distinguish raw git problems from MCP runtime/process issues.
        """
        import os
        import platform
        import threading
        import time

        start = time.perf_counter()
        root = Path(root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break

        try:
            merge_base = await run_git_async(
                str(root), "merge-base", local, upstream, timeout=git_timeout
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
        root: str,
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
        """

        import time

        start = time.perf_counter()
        step = "init"
        times: dict[str, float] = {}

        def mark(name: str) -> None:
            times[name] = round(time.perf_counter() - start, 3)

        # Find git root — check project_root itself first, then one level down
        root = Path(root)
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
                    }
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
                    }
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
                    int(m.group(1))
                    if (m := _re.search(r"(\d+) files? changed", local_stat))
                    else 0
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
                    }
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
        root: str,
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
        """
        import subprocess

        root = Path(root)
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
                f"--format=COMMIT:%h %s",
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
                "changes_by_directory": dict(
                    sorted(dir_changes.items(), key=lambda x: -x[1])[:20]
                ),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @server.tool()
    @timed_git_async
    async def git_conflict_analysis(
        root: str,
        file_path: str,
        upstream: str = "upstream/main",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Deep analysis of a single file that will likely conflict during merge.

        Shows what changed locally vs upstream, with line-level diff context.

        Args:
            file_path: The file to analyze.
            upstream: Upstream ref.
        """
        import subprocess

        root = Path(root)
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
                1
                for l in local_diff.splitlines()
                if l.startswith("+") and not l.startswith("+++")
            )
            local_dels = sum(
                1
                for l in local_diff.splitlines()
                if l.startswith("-") and not l.startswith("---")
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
                "log", "--oneline", f"{merge_base}..{upstream}", "--", file_path
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
                "upstream_diff": upstream_diff[:3000]
                if upstream_diff
                else "(no upstream changes)",
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
        root: str,
        upstream: str = "upstream/main",
        local: str = "HEAD",
        limit: int = 50,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Generate a merge plan: which files to keep, which to take from upstream, which need manual merge.

        Args:
            upstream: Upstream ref to merge from.
        """
        import subprocess
        import time

        start = time.perf_counter()
        step = "init"
        times: dict[str, float] = {}

        def mark(name: str) -> None:
            times[name] = round(time.perf_counter() - start, 3)

        root = Path(root)
        if not (root / ".git").exists():
            for child in root.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    root = child
                    break
        mark("root")

        async def git(*args: str, timeout: int = git_timeout) -> str:
            return await run_git_async(str(root), *args, timeout=timeout)

        async def git_lines(*args: str, timeout: int = git_timeout) -> list[str]:
            return [
                l for l in (await git(*args, timeout=timeout)).splitlines() if l.strip()
            ]

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
                )
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
                )
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
