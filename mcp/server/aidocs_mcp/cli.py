"""AIDOCS CLI — lightweight command-line interface for common operations.

Usage:
    aidocs init [path]         Initialize AIDOCS on a project
    aidocs status [path]       Show index stats, session info, module count
    aidocs config              Open aidocs.toml in $EDITOR
    aidocs config --opencode   Open aidocs-plugin.json
    aidocs config --languages  Open action_tokens/ directory
    aidocs sync [path]         Run code/schema/memory index sync
    aidocs version             Show version
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_root(args: list[str]) -> Path:
    """Get project root from args or cwd."""
    if args:
        return Path(args[0]).resolve()
    return Path.cwd()


def _find_aidocs_root() -> Path | None:
    """Find the AIDOCS installation root."""
    env = os.environ.get("AIDOCS_PATH")
    if env and Path(env).is_dir():
        return Path(env)
    # Walk up from this file
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "aidocs.toml").is_file():
        return candidate
    return None


def cmd_init(args: list[str]) -> int:
    """Initialize AIDOCS on a project."""
    root = _resolve_root(args)
    print(f"Initializing AIDOCS on: {root}")

    from .mcp_server import _resolve_templates_root
    from .service_hub import AidocsServiceHub
    from .runtime_service import RuntimeService

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    runtime = RuntimeService(hub=hub)

    # Create structure
    from .mcp_server import create_server  # need the project_init logic
    # Use the hub directly instead
    import shutil

    memory_template = hub.sessions.templates_root.parent / "templates" / "memory"
    memory_dest = root / ".MEMORY"
    created = []

    if memory_template.is_dir():
        for src_file in memory_template.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(memory_template)
                dest = memory_dest / rel
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_file), str(dest))
                    created.append(str(rel))

    # Router
    router = memory_dest / ".aidocs" / "index.aidocs"
    if not router.exists():
        router.parent.mkdir(parents=True, exist_ok=True)
        src_router = hub.sessions.templates_root.parent / "index.aidocs"
        if src_router.is_file():
            shutil.copy2(str(src_router), str(router))
        else:
            router.write_text("# AIDOCS Session Entry\n\nRead /.MEMORY/INDEX.md next.\n", encoding="utf-8")
        created.append(".aidocs/index.aidocs")

    # AGENTS.md / CLAUDE.md
    for name in ["AGENTS.md", "CLAUDE.md"]:
        dest = root / name
        if not dest.exists():
            src = hub.sessions.templates_root.parents[1] / name
            if src.is_file():
                shutil.copy2(str(src), str(dest))
            else:
                dest.write_text(f"# {name.replace('.md','')}\n\nAIDOCS-managed project.\n", encoding="utf-8")
            created.append(name)

    # .mcp.json
    mcp_result = runtime.ensure_claude_mcp_config(root)

    print(f"Created {len(created)} files")
    for f in created[:10]:
        print(f"  + {f}")
    if len(created) > 10:
        print(f"  ... and {len(created) - 10} more")
    print(f"MCP config: {mcp_result.get('action', 'unknown')}")
    print(f"\nRun '/aidocs' in your agent to activate managed mode.")
    return 0


def cmd_status(args: list[str]) -> int:
    """Show project status."""
    root = _resolve_root(args)

    from .code_index_store import CodeIndexStore
    from .schema_index_store import SchemaIndexStore
    from .session_store import SessionStore

    code = CodeIndexStore()
    schema = SchemaIndexStore()

    # Check if initialized
    if not (root / ".MEMORY").is_dir():
        print(f"Not an AIDOCS project: {root}")
        print("Run 'aidocs init' first.")
        return 1

    # Code index
    code.init_db(root)
    with code.connect(root) as conn:
        total_files = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
        parsed = conn.execute("SELECT COUNT(*) FROM code_files WHERE parsed = 1").fetchone()[0]
        outlines = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]
        modules = conn.execute("SELECT COUNT(*) FROM code_modules").fetchone()[0]
        unknown = conn.execute("SELECT COUNT(*) FROM code_files WHERE role IS NULL OR role = ''").fetchone()[0]

    # Schema
    schema.init_db(root)
    with schema.connect(root) as conn:
        entities = conn.execute("SELECT COUNT(*) FROM schema_entities").fetchone()[0]
        fields = conn.execute("SELECT COUNT(*) FROM schema_fields").fetchone()[0]

    # Sessions
    sessions_dir = root / ".MEMORY" / "sessions"
    session_count = 0
    if sessions_dir.is_dir():
        session_count = sum(1 for d in sessions_dir.iterdir() if d.is_dir() and (d / "SESSION.md").is_file())

    # Print
    print(f"AIDOCS Status: {root.name}")
    print(f"{'-' * 40}")
    print(f"  Files indexed:    {total_files} ({parsed} parsed)")
    print(f"  Symbols:          {outlines}")
    print(f"  Modules:          {modules}")
    print(f"  Unknown roles:    {unknown} ({unknown * 100 // total_files if total_files else 0}%)")
    print(f"  Schema entities:  {entities}")
    print(f"  Schema fields:    {fields}")
    print(f"  Sessions:         {session_count}")

    # Managed mode
    config_path = root / ".MEMORY" / "config" / "aidocs-managed.json"
    if config_path.is_file():
        import json
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if cfg.get("active"):
                print(f"  Managed mode:     active (session: {cfg.get('session_id', 'none')})")
            else:
                print(f"  Managed mode:     inactive")
        except Exception:
            print(f"  Managed mode:     unknown")
    else:
        print(f"  Managed mode:     not configured")

    return 0


def cmd_config(args: list[str]) -> int:
    """Open config file in editor."""
    aidocs_root = _find_aidocs_root()
    if not aidocs_root:
        print("Cannot find AIDOCS installation. Set AIDOCS_PATH env var.")
        return 1

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("code" if sys.platform == "win32" else "nano")

    if "--opencode" in args:
        target = aidocs_root / "aidocs-plugin.json"
    elif "--languages" in args:
        target = aidocs_root / "action_tokens"
    else:
        target = aidocs_root / "aidocs.toml"

    if not target.exists():
        print(f"Config not found: {target}")
        return 1

    print(f"Opening: {target}")
    os.system(f'{editor} "{target}"')
    return 0


def cmd_sync(args: list[str]) -> int:
    """Run index sync."""
    root = _resolve_root(args)

    if not (root / ".MEMORY").is_dir():
        print(f"Not an AIDOCS project: {root}")
        return 1

    import time
    from .code_index_store import CodeIndexStore
    from .schema_index_store import SchemaIndexStore
    from .index_store import IndexStore
    from .session_store import SessionStore
    from .mcp_server import _resolve_templates_root

    print(f"Syncing: {root.name}")

    t0 = time.time()
    sessions = SessionStore(templates_root=_resolve_templates_root())
    memory = IndexStore(session_store=sessions)
    mem_result = memory.sync_all(root)
    print(f"  Memory:  {mem_result.get('memory_files', 0)} files, {mem_result.get('sessions', 0)} sessions ({time.time() - t0:.1f}s)")

    t1 = time.time()
    code = CodeIndexStore()
    code_count = code.sync_code_files(root)
    mod_count = code.sync_modules(root)
    print(f"  Code:    {code_count} files, {mod_count} modules ({time.time() - t1:.1f}s)")

    t2 = time.time()
    schema = SchemaIndexStore()
    schema_result = schema.sync_schema(root)
    entities = schema_result.get("entities", 0) if isinstance(schema_result, dict) else 0
    print(f"  Schema:  {entities} entities ({time.time() - t2:.1f}s)")

    print(f"  Total:   {time.time() - t0:.1f}s")
    return 0


def cmd_version(args: list[str]) -> int:
    """Show version."""
    print("aidocs-mcp 1.1.0")
    return 0


COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "config": cmd_config,
    "sync": cmd_sync,
    "version": cmd_version,
    "--version": cmd_version,
    "-v": cmd_version,
}


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd in COMMANDS:
        sys.exit(COMMANDS[cmd](args[1:]))
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
