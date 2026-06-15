"""Canonical SQLite registry for project MCP servers.

.mcp.json is a HOST PROJECTION, not authority. The source of truth for
which MCP servers a project has is the ``mcp_servers`` table in the project
SQLite DB. install/delete mutate the table, then the whole table is
projected to .mcp.json. .mcp.json can be deleted at any time and rebuilt
from SQL alone — it is never replayed from install actions.

Read/write doctrine (legacy-ingest seal): normal read and write paths
(``list_servers``, ``upsert``, ``delete``) only ensure the schema exists —
they are otherwise SQL-only and side-effect-free, and they NEVER import a
legacy .mcp.json. A read can therefore never MINT control authority from a
file. The one-time legacy import lives behind the explicit, authenticated
migration entry point ``migrate_legacy_once`` (driven by the
``migrate-control-authority`` admin CLI command / bounded bootstrap phase).
It imports a pre-registry .mcp.json exactly once and seals a marker so a
later stale/placed file can never be re-imported.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ._sqlite_index_store_base import SQLiteIndexStoreBase

_VALID_TRANSPORTS = ("stdio", "http", "sse")


def canonical_server_fields(
    transport: object,
    command: object,
    args: object,
    env: object = None,
) -> dict:
    """The canonical per-server projection block {type, command, args[, env]}.

    THE single definition of how one mcpServers entry is rendered. Both the
    writer (project_to_file) and the governed-deletion regeneration proof use
    this (via render_projection_servers / on-disk normalization) so the written
    projection and the proof's expected projection can never drift apart.

    ``env`` is OMITTED when empty so a server with no env (the common case —
    e.g. every non-aidocs server) renders the identical 3-key shape it always
    has; the governed-deletion proof for those rows stays byte-identical. The
    aidocs server carries env (AIDOCS_PROJECT_ROOT [+ PYTHONPATH in source
    mode]) so its spawned MCP knows its project root.
    """
    block = {
        "type": str(transport or "stdio"),
        "command": str(command or ""),
        "args": [str(a) for a in (args or [])],
    }
    if isinstance(env, dict) and env:
        block["env"] = {str(k): str(v) for k, v in env.items()}
    return block


def render_projection_servers(servers: list[dict]) -> dict:
    """Render the canonical ``mcpServers`` block from SQL rows — the SINGLE
    source of the projection shape written to .mcp.json. project_to_file writes
    exactly this; the regeneration proof compares the on-disk file against
    exactly this.
    """
    return {
        str(s["name"]): canonical_server_fields(
            s.get("type"), s.get("command"), s.get("args"), s.get("env")
        )
        for s in servers
    }


def _valid_server_name(name: str) -> str:
    """Mirror cli._valid_mcp_name so migration enforces the SAME addressable-
    key guard the install path does — a name with separators/traversal/
    surrounding whitespace is never a valid server key.
    """
    if not name:
        return "server name is required"
    if any(c in name for c in ("/", "\\", "..")) or name != name.strip():
        return f"invalid server name: {name!r}"
    return ""


def _valid_server_spec(
    name: str,
    command: str,
    args: object,
    transport: str,
) -> str:
    """Validate a legacy .mcp.json entry as a capability-provider definition
    BEFORE it can enter SQL authority. Mirrors cli._validate_mcp_server_def.
    Returns "" when valid, else a human reason. An entry that fails here is
    skipped by migration — a bad/abusive legacy entry never becomes authority.
    """
    name_reason = _valid_server_name(name)
    if name_reason:
        return name_reason
    if not command or not str(command).strip():
        return "server command is required"
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "server args must be a list of strings"
    if transport not in _VALID_TRANSPORTS:
        return f"invalid transport {transport!r} (allowed: {', '.join(_VALID_TRANSPORTS)})"
    return ""


class McpRegistryStore(SQLiteIndexStoreBase):
    def ensure_schema(self, project_root: Path) -> None:
        """Create the registry tables if absent. Side-effect-free beyond
        idempotent schema creation — SAFE to call from read paths. NEVER
        imports from .mcp.json (that is reserved for migrate_legacy_once).
        """
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    name TEXT PRIMARY KEY,
                    type TEXT NOT NULL DEFAULT 'stdio',
                    command TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '[]',
                    env_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'operator',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_registry_meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                );
                """,
            )
            # Idempotent migration for pre-2026-06 project DBs created before
            # the env_json column existed. Guarded by a column-presence check
            # so re-running never raises "duplicate column".
            cols = {r[1] for r in conn.execute("PRAGMA table_info(mcp_servers)").fetchall()}
            if "env_json" not in cols:
                conn.execute(
                    "ALTER TABLE mcp_servers ADD COLUMN env_json TEXT NOT NULL DEFAULT '{}'"
                )

    # Back-compat alias: schema-only, NO legacy ingest. Kept so existing
    # callers that said init_db() get the side-effect-free behavior.
    def init_db(self, project_root: Path) -> None:
        self.ensure_schema(project_root)

    # ── explicit one-time legacy migration (NOT a read/write side effect) ─
    def migrate_legacy_once(self, project_root: Path) -> dict:
        """Import a pre-registry .mcp.json into SQL exactly ONCE, then seal.

        This is the ONLY path that reads .mcp.json into authority. It is
        invoked explicitly by the authenticated ``migrate-control-authority``
        command (or the bounded bootstrap phase) — never by ``list_servers``
        or a normal install/delete. A stale or freshly placed .mcp.json
        therefore cannot mint authority through a read; only this explicit
        call can, and only before the marker is sealed.

        Each legacy entry is VALIDATED as a capability-provider definition
        before import; entries that fail validation are SKIPPED (recorded in
        ``skipped``) so a bad/abusive legacy entry never becomes authority.

        Marker semantics: sealed only AFTER a successful import. A read
        OSError propagates WITHOUT sealing (retry next migration). A
        malformed/unparseable file is "nothing to import" → sealed (the file
        is not authority; do not loop on corruption). Skipped invalid entries
        do NOT block sealing — they are permanently invalid, not retryable.

        Returns {"status": already_migrated|migrated, "imported": N,
                 "skipped": [{"name", "reason"}, ...]}.
        """
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            done = conn.execute("SELECT 1 FROM mcp_registry_meta WHERE k = 'ingested'").fetchone()
        if done:
            return {"status": "already_migrated", "imported": 0, "skipped": []}
        mcp = project_root / ".mcp.json"
        imported = 0
        skipped: list[dict] = []
        if mcp.is_file():
            raw = mcp.read_text(encoding="utf-8")  # OSError → no seal, retry
            try:
                servers = json.loads(raw).get("mcpServers")
            except Exception:
                servers = None
            if isinstance(servers, dict):
                for name, spec in servers.items():
                    nm = str(name)
                    if not isinstance(spec, dict):
                        skipped.append({"name": nm, "reason": "entry is not an object"})
                        continue
                    command = str(spec.get("command") or "")
                    transport = str(spec.get("type") or "stdio")
                    args = spec.get("args")
                    if args is None:
                        args = []
                    spec_env = spec.get("env")
                    env_map = spec_env if isinstance(spec_env, dict) else {}
                    reason = _valid_server_spec(nm, command, args, transport)
                    if reason:
                        # Invalid entry must NEVER become authority — skip it.
                        skipped.append({"name": nm, "reason": reason})
                        continue
                    self.upsert(
                        project_root,
                        nm,
                        command=command,
                        args=[str(a) for a in args],
                        transport=transport,
                        source="imported",
                        env=env_map,
                        _skip_init=True,
                    )
                    imported += 1
        # Import succeeded (or nothing importable) — seal the marker.
        with self.session(project_root) as conn:
            conn.execute("INSERT OR REPLACE INTO mcp_registry_meta (k, v) VALUES ('ingested', '1')")
        return {"status": "migrated", "imported": imported, "skipped": skipped}

    # ── authority mutations ─────────────────────────────────────────
    def upsert(
        self,
        project_root: Path,
        name: str,
        *,
        command: str,
        args: list[str],
        transport: str = "stdio",
        source: str = "operator",
        env: dict | None = None,
        _skip_init: bool = False,
    ) -> None:
        if not _skip_init:
            self.ensure_schema(project_root)
        tr = transport if transport in _VALID_TRANSPORTS else "stdio"
        env_map = {str(k): str(v) for k, v in (env or {}).items()}
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT INTO mcp_servers (name, type, command, args_json, "
                "env_json, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET type=excluded.type, "
                "command=excluded.command, args_json=excluded.args_json, "
                "env_json=excluded.env_json, source=excluded.source, "
                "updated_at=excluded.updated_at",
                (
                    name,
                    tr,
                    command,
                    json.dumps(list(args)),
                    json.dumps(env_map),
                    source,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )

    def delete(self, project_root: Path, name: str) -> bool:
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            cur = conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
            return cur.rowcount > 0

    def list_servers(self, project_root: Path) -> list[dict]:
        # READ: schema-only, SQL-only, side-effect-free. Never imports the
        # legacy .mcp.json — a stale/placed file cannot leak into authority.
        self.ensure_schema(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT name, type, command, args_json, env_json, source "
                "FROM mcp_servers ORDER BY name",
            ).fetchall()
        out = []
        for r in rows:
            try:
                args = json.loads(r["args_json"])
            except Exception:
                args = []
            try:
                env = json.loads(r["env_json"]) if r["env_json"] else {}
            except Exception:
                env = {}
            out.append(
                {
                    "name": r["name"],
                    "type": r["type"],
                    "command": r["command"],
                    "args": args,
                    "env": env if isinstance(env, dict) else {},
                    "source": r["source"],
                },
            )
        return out

    # ── host projection (regenerable from SQL alone) ────────────────
    def project_to_file(self, project_root: Path) -> Path:
        """(Re)generate .mcp.json from the SQL table — a pure projection.
        Deleting .mcp.json and calling this rebuilds it from SQL state, with
        no replay of install actions.
        """
        servers = render_projection_servers(self.list_servers(project_root))
        mcp = project_root / ".mcp.json"
        # Preserve any non-mcpServers top-level keys an existing file carries.
        data: dict = {}
        if mcp.is_file():
            try:
                existing = json.loads(mcp.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    data = {k: v for k, v in existing.items() if k != "mcpServers"}
            except Exception:
                data = {}
        data["mcpServers"] = servers
        _atomic_write_text(mcp, json.dumps(data, indent=2) + "\n")
        return mcp


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic temp+replace write (no half-written projection)."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
