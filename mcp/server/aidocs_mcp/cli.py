"""AIDOCS CLI — lightweight command-line interface for common operations.

Usage:
    aidocs init [path]         Initialize AIDOCS on a project
    aidocs status [path]       Show index stats, session info, module count
    aidocs dashboard [path]    Emit dashboard snapshot JSON for the desktop app
    aidocs config              Open aidocs.toml in $EDITOR
    aidocs config --opencode   Open aidocs-plugin.json
    aidocs config --languages  Open action_tokens/ directory
    aidocs sync [path]         Run code/schema/memory index sync
    aidocs benchmark [path]    Run repeatable benchmark scenarios
    aidocs descriptors [path]  Inspect or validate index language descriptors
    aidocs snapshots           Inspect local copied index snapshots
    aidocs version             Show version
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__
from .config_schema import (
    SETTINGS_CATALOG,
    is_setting_agent_editable,
    validate_setting_value,
)
from .language_descriptors import (
    descriptor_match_summary,
    descriptor_registry_summary,
    validate_language_descriptors,
)
from .project_registry_service import ProjectRegistryService


def _resolve_root(args: list[str]) -> Path:
    """Get project root from args or cwd."""
    positional = [arg for arg in args if not arg.startswith("--")]
    if positional:
        return Path(positional[0]).resolve()
    return Path.cwd()


def _wants_json(args: list[str]) -> bool:
    return "--json" in args


def _option_value(args: list[str], name: str, default: str) -> str:
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return default


def _write_json_output(path_value: str, payload: dict[str, object]) -> None:
    target = Path(path_value).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _parse_json_argument(args: list[str], name: str) -> object | None:
    if name not in args:
        return None
    idx = args.index(name)
    if idx + 1 >= len(args):
        return None
    raw = json.loads(args[idx + 1])
    # Coerce string representations of numbers/booleans to native types
    if isinstance(raw, str):
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                pass
    return raw


def _dashboard_runtime() -> tuple[object, object]:
    from .mcp_server import _resolve_templates_root, _resolve_script_root
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(), script_root=_resolve_script_root()
    )
    return hub, RuntimeService(hub=hub)


def _format_toml_value(value: object, value_type: str) -> str:
    if value_type == "boolean":
        return "true" if bool(value) else "false"
    if value_type == "integer":
        return str(int(value))
    if value_type == "string_list":
        joined = ", ".join(str(item) for item in value if str(item).strip())
        return json.dumps(joined)
    return json.dumps(str(value))


def _resolve_config_path_for_scope(
    project_root: Path, scope: str, session_id: str | None = None,
) -> Path:
    """Resolve aidocs.toml path for a given scope."""
    if scope == "session":
        if not session_id:
            raise ValueError("Session ID required for session-scoped settings.")
        return project_root / ".MEMORY" / "sessions" / session_id / "aidocs.toml"
    if scope == "user":
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA", "")) / "aidocs"
        else:
            xdg = os.environ.get("XDG_CONFIG_HOME")
            base = Path(xdg) / "aidocs" if xdg else Path.home() / ".config" / "aidocs"
        base.mkdir(parents=True, exist_ok=True)
        return base / "aidocs.toml"
    return project_root / "aidocs.toml"


def _update_project_config_value(
    project_root: Path, setting_path: str, value: object,
    scope: str = "project", session_id: str | None = None,
    dashboard: bool = False,
) -> Path:
    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None:
        raise ValueError(f"Unknown config setting: {setting_path}.")
    allowed = metadata.get("allowed_scopes", ["project"])
    if scope not in allowed:
        raise ValueError(f"Setting {setting_path} does not support scope '{scope}'. Allowed: {allowed}")
    # Dashboard is the user — skip agent-editable check for security_sensitive settings
    if not dashboard and not is_setting_agent_editable(
        setting_path, scope=scope, edit_mode="explicit_user_permitted"
    ):
        raise ValueError(
            f"Config setting requires controlled edit permission: {setting_path}."
        )
    # Coerce value to match expected type from catalog
    expected_type = metadata.get("type", "string")
    if expected_type == "integer" and isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            pass
    elif expected_type == "boolean" and isinstance(value, str):
        value = value.lower() in ("true", "1", "yes")
    validate_setting_value(setting_path, value)

    # Write to SQLite config store (single source of truth)
    from .config_store import ConfigStore
    store = ConfigStore()
    scope_key = session_id or "" if scope == "session" else ""
    db_scope = "user" if scope == "global" else scope
    store.set(project_root, setting_path, value, scope=db_scope, scope_key=scope_key)

    # Return the DB path for diagnostics
    return store.db_path(project_root)


def _result_size(value: object) -> int:
    if isinstance(value, dict):
        if isinstance(value.get("matches"), list):
            return len(value["matches"])
        if isinstance(value.get("items"), list):
            return len(value["items"])
        if isinstance(value.get("files"), list):
            return len(value["files"])
        if isinstance(value.get("symbols"), list):
            return len(value["symbols"])
        if isinstance(value.get("result"), list):
            return len(value["result"])
        if isinstance(value.get("result"), dict):
            return len(value["result"])
        return len(value)
    if isinstance(value, list):
        return len(value)
    return 0


def _raw_scan_matches(
    project_root: Path, query: str, limit: int = 20
) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    scanned_files = 0
    if not query.strip():
        return {"query": query, "scanned_files": 0, "matches": matches}

    words = [w.lower() for w in re.findall(r"[A-Za-z0-9_]+", query) if len(w) >= 3]
    if not words:
        words = [query.strip().lower()]

    skip_dirs = {
        ".git",
        ".MEMORY",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
    }
    text_exts = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".cs",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".json",
        ".toml",
        ".yml",
        ".yaml",
        ".md",
        ".sql",
        ".html",
        ".css",
        ".scss",
    }

    for path in project_root.rglob("*"):
        if len(matches) >= limit:
            break
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in text_exts:
            continue
        scanned_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        lower = text.lower()
        score = sum(1 for word in words if word in lower or word in path.name.lower())
        if score:
            matches.append(
                {
                    "path": path.relative_to(project_root).as_posix(),
                    "score": score,
                }
            )

    matches.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return {"query": query, "scanned_files": scanned_files, "matches": matches[:limit]}


def _classification_prompt_batches_for_set(name: str) -> dict[str, list[str]]:
    scenario_set = (name or "public").strip().lower()
    if scenario_set == "public":
        return {
            "en": [
                "something is off with the /aidocs startup path in OpenCode and I need to understand where the decision is really happening before I touch anything",
                "can you trace the thing that decides whether this becomes an edit task or just a read, because it feels inconsistent and I keep getting different behavior",
                "I need the likely runtime path for session/bootstrap handling, especially the parts that kick in after managed mode is already active",
            ],
            "es": [
                "algo raro esta pasando con el arranque de /aidocs en OpenCode y necesito entender donde se toma realmente la decision antes de tocar nada",
                "puedes rastrear la parte que decide si esto termina siendo una edicion o solo lectura porque el comportamiento se siente inconsistente",
            ],
            "de": [
                "ich brauche den eigentlichen runtime pfad fuer bootstrap und session handling, aber bitte nicht nur eine rohe textsuche durch das ganze repo",
                "irgendetwas entscheidet zu frueh ob das eine aenderung oder nur analyse ist; finde den relevanten codepfad",
            ],
            "ja": [
                "/aidocs の開始フローでどこが本当に判断しているのか知りたいです。関係ないファイルはできるだけ避けてください",
                "これが編集タスクになるのか調査だけなのかを決めている流れを追いたいです。最近かなり不安定です",
            ],
            "pt": [
                "tem alguma coisa estranha no fluxo de inicio do /aidocs e eu preciso entender onde a decisao acontece de verdade antes de mexer em qualquer coisa",
            ],
            "it": [
                "mi serve il percorso reale di bootstrap e sessione, non una ricerca generica nel repository e non un elenco di simboli poco utili",
            ],
        }
    raise ValueError(f"Unknown benchmark scenario set: {name}")


def _retrieval_scenarios_for_set(
    name: str, root: Path, hub: object
) -> list[dict[str, object]]:
    scenario_set = (name or "public").strip().lower()
    if scenario_set != "public":
        raise ValueError(f"Unknown benchmark scenario set: {name}")

    bundle_target = ""
    with hub.code.connect(root) as conn:
        row = conn.execute(
            "SELECT path FROM code_files ORDER BY path LIMIT 1"
        ).fetchone()
        if row and row[0]:
            bundle_target = str(row[0])

    scenarios = [
        {
            "name": "investigate-aidocs-entry-flow",
            "prompt": "the /aidocs startup path still feels slippery; show me the main code path that actually matters for bootstrap, command handling, and routing without drowning me in unrelated files",
            "runner": lambda: hub.code.investigate(root, "aidocs", limit=5),
        },
        {
            "name": "find-symbols-for-init-path",
            "prompt": "I need the symbols that are most likely to matter for init/bootstrap/setup, not a giant repo-wide text search and not every random helper with init in the name",
            "runner": lambda: hub.code.search_symbols(root, "init", limit=20),
        },
        {
            "name": "trace-runtime-service-usage",
            "prompt": "something around runtime service orchestration is deciding more than I expect; trace where RuntimeService actually gets used in the important paths",
            "runner": lambda: hub.code.trace_service_usage(
                root, "RuntimeService", limit=20
            ),
        },
        {
            "name": "bundle-session-subsystem",
            "prompt": "give me the subsystem-level picture for session handling because I care about the important boundaries and supporting structures, not just a single symbol",
            "runner": lambda: hub.code.get_subsystem_bundle(root, "session", limit=12),
        },
    ]
    if bundle_target:
        scenarios.append(
            {
                "name": "bundle-real-project-file",
                "prompt": "give me the real file-level context for something the project actually contains, not a theoretical example or a guessed path",
                "runner": lambda target=bundle_target: hub.code.get_file_bundle(
                    root, target
                ),
            }
        )
    return scenarios


def _schema_scenarios_for_set(
    name: str, root: Path, hub: object
) -> list[dict[str, object]]:
    scenario_set = (name or "public").strip().lower()
    if scenario_set != "public":
        raise ValueError(f"Unknown benchmark scenario set: {name}")

    entities: list[str] = []
    fields: list[str] = []
    with hub.schema.connect(root) as conn:
        entity_rows = conn.execute(
            "SELECT entity_name FROM schema_entities ORDER BY entity_name LIMIT 2"
        ).fetchall()
        field_rows = conn.execute(
            "SELECT field_name FROM schema_fields ORDER BY field_name LIMIT 2"
        ).fetchall()
        entities = [str(row[0]) for row in entity_rows if row and row[0]]
        fields = [str(row[0]) for row in field_rows if row and row[0]]

    scenarios: list[dict[str, object]] = []
    if entities:
        entity = entities[0]
        scenarios.append(
            {
                "name": "schema-entity-lookup",
                "prompt": "I need the schema entity that probably matters here, but I do not remember the exact shape and I only care about the real indexed definition",
                "runner": lambda entity_name=entity: hub.schema.get_entity(
                    root, entity_name
                ),
            }
        )
        scenarios.append(
            {
                "name": "schema-entity-search",
                "prompt": "find the likely schema entities for this concept without dumping the whole database model",
                "runner": lambda entity_name=entity: hub.schema.find_schema_entities(
                    root, query=entity_name, limit=10
                ),
            }
        )
    if fields:
        field = fields[0]
        scenarios.append(
            {
                "name": "schema-field-search",
                "prompt": "trace the field that sounds relevant here because I need to know which entity owns it and where it shows up",
                "runner": lambda field_name=field: hub.schema.find_schema_field(
                    root, field_name, limit=10
                ),
            }
        )
    return scenarios


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
    as_json = _wants_json(args)
    if not as_json:
        print(f"Initializing AIDOCS on: {root}")

    from .mcp_server import _resolve_templates_root
    from .service_hub import AidocsServiceHub
    from .runtime_service import RuntimeService

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    runtime = RuntimeService(hub=hub)

    result = runtime.project_init(root, init_git=False, create_remote=False)
    created = (
        result.get("created", []) if isinstance(result.get("created"), list) else []
    )
    mcp_result = (
        result.get("mcp_config", {})
        if isinstance(result.get("mcp_config"), dict)
        else {}
    )

    payload = {
        "ok": True,
        "project_root": str(root),
        "project_name": root.name,
        "initialized": bool(result.get("initialized", False)),
        "created_count": len(created),
        "created": created,
        "skipped": result.get("skipped", []),
        "git": result.get("git", {}),
        "origins": result.get("origins", {}),
        "repo_summary": result.get("repo_summary", {}),
        "mcp_config": mcp_result,
        "next_step": result.get("next_step"),
        "message": "Run '/aidocs' in your agent to activate managed mode.",
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Created {len(created)} files")
    for f in created[:10]:
        print(f"  + {f}")
    if len(created) > 10:
        print(f"  ... and {len(created) - 10} more")
    repo_summary = (
        result.get("repo_summary")
        if isinstance(result.get("repo_summary"), dict)
        else {}
    )
    bullets = (
        repo_summary.get("bullets")
        if isinstance(repo_summary.get("bullets"), list)
        else []
    )
    for bullet in bullets[:4]:
        print(f"  - {bullet}")
    print(f"MCP config: {mcp_result.get('action', 'unknown')}")
    print(f"\nRun '/aidocs' in your agent to activate managed mode.")
    return 0


def cmd_status(args: list[str]) -> int:
    """Show project status."""
    root = _resolve_root(args)
    as_json = _wants_json(args)

    from .code_index_store import CodeIndexStore
    from .schema_index_store import SchemaIndexStore
    from .session_store import SessionStore

    code = CodeIndexStore()
    schema = SchemaIndexStore()

    # Check if initialized
    if not (root / ".MEMORY").is_dir():
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "initialized": False,
                        "project_root": str(root),
                        "reason": "not_initialized",
                        "message": f"Not an AIDOCS project: {root}",
                    },
                    indent=2,
                )
            )
        else:
            print(f"Not an AIDOCS project: {root}")
            print("Run 'aidocs init' first.")
        return 1

    # Code index
    code.init_db(root)
    with code.connect(root) as conn:
        total_files = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
        parsed = conn.execute(
            "SELECT COUNT(*) FROM code_files WHERE parsed = 1"
        ).fetchone()[0]
        outlines = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]
        modules = conn.execute("SELECT COUNT(*) FROM code_modules").fetchone()[0]
        unknown = conn.execute(
            "SELECT COUNT(*) FROM code_files WHERE role IS NULL OR role = ''"
        ).fetchone()[0]

    # Schema
    schema.init_db(root)
    with schema.connect(root) as conn:
        entities = conn.execute("SELECT COUNT(*) FROM schema_entities").fetchone()[0]
        fields = conn.execute("SELECT COUNT(*) FROM schema_fields").fetchone()[0]

    # Sessions
    sessions_dir = root / ".MEMORY" / "sessions"
    session_count = 0
    if sessions_dir.is_dir():
        session_count = sum(
            1
            for d in sessions_dir.iterdir()
            if d.is_dir() and (d / "SESSION.md").is_file()
        )

    managed_mode: dict[str, object] = {
        "state": "not_configured",
        "active": False,
        "session_id": None,
    }
    config_path = root / ".MEMORY" / "config" / "aidocs-managed.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if cfg.get("active"):
                managed_mode = {
                    "state": "active",
                    "active": True,
                    "session_id": cfg.get("session_id", "none"),
                }
            else:
                managed_mode = {
                    "state": "inactive",
                    "active": False,
                    "session_id": cfg.get("session_id"),
                }
        except Exception:
            managed_mode = {"state": "unknown", "active": False, "session_id": None}

    payload = {
        "ok": True,
        "initialized": True,
        "project_root": str(root),
        "project_name": root.name,
        "code": {
            "files_indexed": total_files,
            "files_parsed": parsed,
            "symbols": outlines,
            "modules": modules,
            "unknown_roles": unknown,
            "unknown_roles_percent": (
                unknown * 100 // total_files if total_files else 0
            ),
        },
        "schema": {
            "entities": entities,
            "fields": fields,
        },
        "sessions": {
            "count": session_count,
        },
        "managed_mode": managed_mode,
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"AIDOCS Status: {root.name}")
    print(f"{'-' * 40}")
    print(f"  Files indexed:    {total_files} ({parsed} parsed)")
    print(f"  Symbols:          {outlines}")
    print(f"  Modules:          {modules}")
    print(
        f"  Unknown roles:    {unknown} ({unknown * 100 // total_files if total_files else 0}%)"
    )
    print(f"  Schema entities:  {entities}")
    print(f"  Schema fields:    {fields}")
    print(f"  Sessions:         {session_count}")
    if managed_mode["state"] == "active":
        print(f"  Managed mode:     active (session: {managed_mode['session_id']})")
    elif managed_mode["state"] == "inactive":
        print("  Managed mode:     inactive")
    elif managed_mode["state"] == "unknown":
        print("  Managed mode:     unknown")
    else:
        print("  Managed mode:     not configured")

    return 0


def cmd_config(args: list[str]) -> int:
    """Open config file in editor."""
    as_json = _wants_json(args)
    aidocs_root = _find_aidocs_root()
    if not aidocs_root:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "aidocs_root_not_found",
                        "message": "Cannot find AIDOCS installation. Set AIDOCS_PATH env var.",
                    },
                    indent=2,
                )
            )
        else:
            print("Cannot find AIDOCS installation. Set AIDOCS_PATH env var.")
        return 1

    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("code" if sys.platform == "win32" else "nano")
    )

    if "--opencode" in args:
        target = aidocs_root / "aidocs-plugin.json"
        target_kind = "opencode"
    elif "--languages" in args:
        target = aidocs_root / "action_tokens"
        target_kind = "languages"
    else:
        target = aidocs_root / "aidocs.toml"
        target_kind = "config"

    if not target.exists():
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "aidocs_root": str(aidocs_root),
                        "target_kind": target_kind,
                        "target": str(target),
                        "exists": False,
                        "message": f"Config not found: {target}",
                    },
                    indent=2,
                )
            )
        else:
            print(f"Config not found: {target}")
        return 1

    payload = {
        "ok": True,
        "aidocs_root": str(aidocs_root),
        "target_kind": target_kind,
        "target": str(target),
        "exists": True,
        "editor": editor,
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Opening: {target}")
    os.system(f'{editor} "{target}"')
    return 0


def cmd_sync(args: list[str]) -> int:
    """Run index sync."""
    root = _resolve_root(args)
    as_json = _wants_json(args)

    if not (root / ".MEMORY").is_dir():
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "initialized": False,
                        "project_root": str(root),
                        "reason": "not_initialized",
                        "message": f"Not an AIDOCS project: {root}",
                    },
                    indent=2,
                )
            )
        else:
            print(f"Not an AIDOCS project: {root}")
        return 1

    from .code_index_store import CodeIndexStore
    from .schema_index_store import SchemaIndexStore
    from .index_store import IndexStore
    from .session_store import SessionStore
    from .mcp_server import _resolve_templates_root

    t0 = time.time()
    sessions = SessionStore(templates_root=_resolve_templates_root())
    memory = IndexStore(session_store=sessions)
    mem_result = memory.sync_all(root)
    memory_seconds = round(time.time() - t0, 3)

    t1 = time.time()
    code = CodeIndexStore()
    code_count = code.sync_code_files(root)
    mod_count = code.sync_modules(root)
    code_seconds = round(time.time() - t1, 3)

    t2 = time.time()
    schema = SchemaIndexStore()
    schema_result = schema.sync_schema(root)
    entities = (
        schema_result.get("entities", 0) if isinstance(schema_result, dict) else 0
    )
    schema_seconds = round(time.time() - t2, 3)

    total_seconds = round(time.time() - t0, 3)
    payload = {
        "ok": True,
        "initialized": True,
        "project_root": str(root),
        "project_name": root.name,
        "memory": {
            "memory_files": mem_result.get("memory_files", 0),
            "sessions": mem_result.get("sessions", 0),
            "seconds": memory_seconds,
        },
        "code": {
            "files": code_count,
            "modules": mod_count,
            "seconds": code_seconds,
        },
        "schema": {
            "entities": entities,
            "seconds": schema_seconds,
        },
        "total_seconds": total_seconds,
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Syncing: {root.name}")
    print(
        f"  Memory:  {mem_result.get('memory_files', 0)} files, {mem_result.get('sessions', 0)} sessions ({memory_seconds:.1f}s)"
    )
    print(f"  Code:    {code_count} files, {mod_count} modules ({code_seconds:.1f}s)")
    print(f"  Schema:  {entities} entities ({schema_seconds:.1f}s)")
    print(f"  Total:   {total_seconds:.1f}s")
    return 0


def cmd_benchmark(args: list[str]) -> int:
    """Run repeatable benchmark scenarios."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    scenario_set = _option_value(args, "--scenario-set", "public")
    output_path = _option_value(args, "--out", "")
    try:
        iterations = max(1, int(_option_value(args, "--iterations", "100")))
    except ValueError:
        iterations = 100

    if not (root / ".MEMORY").is_dir():
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "initialized": False,
                        "project_root": str(root),
                        "reason": "not_initialized",
                        "message": f"Not an AIDOCS project: {root}",
                    },
                    indent=2,
                )
            )
        else:
            print(f"Not an AIDOCS project: {root}")
        return 1

    from .mcp_server import _resolve_templates_root, _resolve_script_root
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(), script_root=_resolve_script_root()
    )
    runtime = RuntimeService(hub=hub)
    try:
        prompt_batches = _classification_prompt_batches_for_set(scenario_set)
    except ValueError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "invalid_scenario_set",
                        "message": str(exc),
                    },
                    indent=2,
                )
            )
        else:
            print(str(exc))
        return 1

    t0 = time.perf_counter()
    sync_result = hub.index.sync_all(root)
    code_files = hub.code.sync_code_files(root)
    modules = hub.code.sync_modules(root)
    schema_result = hub.schema.sync_schema(root)
    sync_seconds = round(time.perf_counter() - t0, 3)

    try:
        retrieval_scenarios = _retrieval_scenarios_for_set(scenario_set, root, hub)
    except ValueError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "invalid_scenario_set",
                        "message": str(exc),
                    },
                    indent=2,
                )
            )
        else:
            print(str(exc))
        return 1
    try:
        schema_scenarios = _schema_scenarios_for_set(scenario_set, root, hub)
    except ValueError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "invalid_scenario_set",
                        "message": str(exc),
                    },
                    indent=2,
                )
            )
        else:
            print(str(exc))
        return 1

    t1 = time.perf_counter()
    counts: dict[str, int] = {}
    per_language: dict[str, dict[str, object]] = {}
    prompt_count = sum(len(items) for items in prompt_batches.values())
    total_classifications = iterations * prompt_count
    for language, prompts in prompt_batches.items():
        language_counts: dict[str, int] = {}
        for _ in range(iterations):
            for prompt in prompts:
                action_kind = str(
                    runtime.classify_prompt_action(prompt).get("action_kind")
                    or "understand"
                )
                counts[action_kind] = counts.get(action_kind, 0) + 1
                language_counts[action_kind] = language_counts.get(action_kind, 0) + 1
        per_language[language] = {
            "prompt_count": len(prompts),
            "total_classifications": len(prompts) * iterations,
            "action_kind_counts": language_counts,
        }
    classify_seconds = round(time.perf_counter() - t1, 3)

    retrieval_results = []
    retrieval_total_start = time.perf_counter()
    for scenario in retrieval_scenarios:
        start = time.perf_counter()
        result = scenario["runner"]()
        elapsed = round(time.perf_counter() - start, 3)
        retrieval_results.append(
            {
                "name": scenario["name"],
                "prompt": scenario["prompt"],
                "seconds": elapsed,
                "result_size": _result_size(result),
            }
        )
    retrieval_seconds = round(time.perf_counter() - retrieval_total_start, 3)

    comparative_scenarios = [
        {
            "name": "aidocs-entry-flow",
            "query": "aidocs bootstrap routing command",
            "indexed_runner": lambda: hub.code.investigate(root, "aidocs", limit=5),
        },
        {
            "name": "runtime-service-trace",
            "query": "RuntimeService orchestration session bootstrap",
            "indexed_runner": lambda: hub.code.trace_service_usage(
                root, "RuntimeService", limit=20
            ),
        },
    ]
    comparative_results = []
    comparative_total_start = time.perf_counter()
    for scenario in comparative_scenarios:
        indexed_start = time.perf_counter()
        indexed_result = scenario["indexed_runner"]()
        indexed_seconds = round(time.perf_counter() - indexed_start, 3)

        raw_start = time.perf_counter()
        raw_result = _raw_scan_matches(root, str(scenario["query"]), limit=20)
        raw_seconds = round(time.perf_counter() - raw_start, 3)

        comparative_results.append(
            {
                "name": scenario["name"],
                "query": scenario["query"],
                "indexed": {
                    "seconds": indexed_seconds,
                    "result_size": _result_size(indexed_result),
                },
                "raw": {
                    "seconds": raw_seconds,
                    "result_size": _result_size(raw_result.get("matches", [])),
                    "scanned_files": raw_result.get("scanned_files", 0),
                },
            }
        )
    comparative_seconds = round(time.perf_counter() - comparative_total_start, 3)

    schema_results = []
    schema_total_start = time.perf_counter()
    for scenario in schema_scenarios:
        start = time.perf_counter()
        result = scenario["runner"]()
        elapsed = round(time.perf_counter() - start, 3)
        schema_results.append(
            {
                "name": scenario["name"],
                "prompt": scenario["prompt"],
                "seconds": elapsed,
                "result_size": _result_size(result),
            }
        )
    schema_benchmark_seconds = round(time.perf_counter() - schema_total_start, 3)

    payload = {
        "ok": True,
        "project_root": str(root),
        "project_name": root.name,
        "scenario_set": scenario_set,
        "iterations": iterations,
        "sync": {
            "memory_files": sync_result.get("memory_files", 0)
            if isinstance(sync_result, dict)
            else 0,
            "sessions": sync_result.get("sessions", 0)
            if isinstance(sync_result, dict)
            else 0,
            "code_files": code_files,
            "modules": modules,
            "schema_entities": schema_result.get("entities", 0)
            if isinstance(schema_result, dict)
            else 0,
            "seconds": sync_seconds,
        },
        "classification": {
            "prompt_count": prompt_count,
            "total_classifications": total_classifications,
            "seconds": classify_seconds,
            "classifications_per_second": round(
                total_classifications / classify_seconds, 2
            )
            if classify_seconds > 0
            else None,
            "action_kind_counts": counts,
            "per_language": per_language,
        },
        "retrieval": {
            "scenario_count": len(retrieval_scenarios),
            "seconds": retrieval_seconds,
            "scenarios": retrieval_results,
        },
        "schema_benchmark": {
            "scenario_count": len(schema_scenarios),
            "seconds": schema_benchmark_seconds,
            "scenarios": schema_results,
        },
        "comparative": {
            "scenario_count": len(comparative_scenarios),
            "seconds": comparative_seconds,
            "scenarios": comparative_results,
        },
    }

    if output_path:
        _write_json_output(output_path, payload)

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Benchmark: {root.name}")
    print(f"{'-' * 40}")
    print(f"  Scenario set:    {scenario_set}")
    print(f"  Sync:            {sync_seconds:.3f}s")
    print(f"    Memory files:  {payload['sync']['memory_files']}")
    print(f"    Sessions:      {payload['sync']['sessions']}")
    print(f"    Code files:    {payload['sync']['code_files']}")
    print(f"    Modules:       {payload['sync']['modules']}")
    print(f"    Schema:        {payload['sync']['schema_entities']} entities")
    print(f"  Classification:  {classify_seconds:.3f}s")
    print(f"    Prompts:       {prompt_count} x {iterations} iterations")
    print(
        f"    Throughput:    {payload['classification']['classifications_per_second']} classifications/s"
    )
    for action_kind, count in sorted(counts.items()):
        print(f"    {action_kind}: {count}")
    for language, info in sorted(per_language.items()):
        print(
            f"    [{language}] prompts={info['prompt_count']} total={info['total_classifications']}"
        )
    print(f"  Retrieval:       {retrieval_seconds:.3f}s")
    for scenario in retrieval_results:
        print(
            f"    {scenario['name']}: {scenario['seconds']:.3f}s ({scenario['result_size']} results)"
        )
    print(f"  Schema bench:    {schema_benchmark_seconds:.3f}s")
    if schema_results:
        for scenario in schema_results:
            print(
                f"    {scenario['name']}: {scenario['seconds']:.3f}s ({scenario['result_size']} results)"
            )
    else:
        print("    no schema scenarios available for this project")
    print(f"  Comparative:     {comparative_seconds:.3f}s")
    for scenario in comparative_results:
        print(
            "    "
            f"{scenario['name']}: indexed={scenario['indexed']['seconds']:.3f}s/{scenario['indexed']['result_size']} "
            f"raw={scenario['raw']['seconds']:.3f}s/{scenario['raw']['result_size']} scanned={scenario['raw']['scanned_files']}"
        )
    if output_path:
        print(f"  Output:          {Path(output_path).resolve()}")
    return 0


def cmd_dashboard(args: list[str]) -> int:
    """Emit dashboard snapshot JSON for the desktop app."""
    root = _resolve_root(args)
    as_json = _wants_json(args) or "--json-output" in args
    session_id = _option_value(args, "--session", "").strip() or None
    output_path = _option_value(args, "--json-output", "").strip()

    if not (root / ".MEMORY").is_dir():
        payload = {
            "ok": False,
            "reason": "not_initialized",
            "project_root": str(root),
            "message": f"Not an AIDOCS project: {root}",
        }
        if as_json:
            if output_path:
                _write_json_output(output_path, payload)
            else:
                print(json.dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    _, runtime = _dashboard_runtime()
    payload = {
        "ok": True,
        "snapshot": runtime.dashboard_snapshot(root, session_id=session_id),
    }
    if as_json:
        if output_path:
            _write_json_output(output_path, payload)
        else:
            print(json.dumps(payload, indent=2, default=str))
        return 0

    snapshot = payload["snapshot"]
    print(f"Dashboard snapshot for: {root}")
    print(f"Sessions: {len(snapshot.get('sessions', []))}")
    print(f"Selected session: {snapshot.get('selected_session_id') or 'none'}")
    return 0


def cmd_dashboard_set_config(args: list[str]) -> int:
    """Persist one editable project config value for the dashboard."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    setting_path = _option_value(args, "--setting", "").strip()
    value = _parse_json_argument(args, "--value")

    if not setting_path:
        payload = {
            "ok": False,
            "reason": "missing_setting",
            "message": "--setting is required",
        }
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    scope = _option_value(args, "--scope", "project").strip()
    session_id_arg = _option_value(args, "--session", "").strip() or None

    try:
        config_path = _update_project_config_value(root, setting_path, value, scope=scope, session_id=session_id_arg, dashboard=True)
    except Exception as exc:
        payload = {
            "ok": False,
            "reason": "config_update_failed",
            "setting_path": setting_path,
            "message": str(exc),
        }
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    _, runtime = _dashboard_runtime()
    updated_value = runtime.effective_config(root).get(setting_path.split(".")[0])
    payload = {
        "ok": True,
        "setting_path": setting_path,
        "config_path": str(config_path),
        "snapshot": runtime.dashboard_snapshot(root),
        "message": f"Updated {setting_path}",
        "value_root": updated_value,
    }
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(payload["message"])
    return 0


def cmd_descriptors(args: list[str]) -> int:
    """Inspect or validate index language descriptors."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    match_path = _option_value(args, "--match", "")
    validate = "--validate" in args
    show_semantics = "--semantics" in args

    if show_semantics:
        from .language_descriptors import descriptor_semantics_summary

        payload = descriptor_semantics_summary()
    elif match_path:
        payload = descriptor_match_summary(root, match_path)
    elif validate:
        payload = validate_language_descriptors(root)
    else:
        payload = descriptor_registry_summary(root)

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    if show_semantics:
        families = (
            payload.get("outline_families")
            if isinstance(payload.get("outline_families"), list)
            else []
        )
        tags = (
            payload.get("semantic_tags")
            if isinstance(payload.get("semantic_tags"), list)
            else []
        )
        print(f"Built-in descriptors: {payload.get('built_in_descriptor_count', 0)}")
        print(
            f"  with extractor family: {payload.get('built_in_with_extractor_family', 0)}"
        )
        print(
            f"  with outline family:   {payload.get('built_in_with_outline_family', 0)}"
        )
        print(
            f"  with raw outlines:     {payload.get('built_in_with_outline_patterns', 0)}"
        )
        print(
            f"  with role semantics:   {payload.get('built_in_with_role_semantics', 0)}"
        )
        print(
            f"  with module hints:     {payload.get('built_in_with_module_hints', 0)}"
        )
        print(f"Outline families: {len(families)}")
        for item in families:
            print(f"  - {item}")
        print(f"Semantic tags: {len(tags)}")
        for item in tags:
            print(f"  - {item}")
        return 0

    if match_path:
        print(f"Descriptor match: {match_path}")
        print(f"  matched:   {payload.get('matched')}")
        print(f"  language:  {payload.get('language')}")
        if payload.get("predicted_role"):
            print(f"  role:      {payload.get('predicted_role')}")
        descriptor = (
            payload.get("descriptor")
            if isinstance(payload.get("descriptor"), dict)
            else {}
        )
        if descriptor:
            print(f"  source:    {descriptor.get('source')}")
            print(f"  tier:      {descriptor.get('tier')}")
            if descriptor.get("outline_family"):
                print(f"  family:    {descriptor.get('outline_family')}")
            if descriptor.get("role_hint"):
                print(f"  role_hint: {descriptor.get('role_hint')}")
            tags = (
                descriptor.get("semantic_tags")
                if isinstance(descriptor.get("semantic_tags"), list)
                else []
            )
            if tags:
                print(f"  tags:      {', '.join(str(tag) for tag in tags)}")
            embedded = (
                descriptor.get("embedded_semantics")
                if isinstance(descriptor.get("embedded_semantics"), list)
                else []
            )
            if embedded:
                print(f"  embeds:    {', '.join(str(item) for item in embedded)}")
        return 0

    if validate:
        print(
            f"Descriptor validation: {'ok' if payload.get('valid') else 'issues found'}"
        )
        print(f"  descriptors: {payload.get('count', 0)}")
        issues = (
            payload.get("issues") if isinstance(payload.get("issues"), list) else []
        )
        for issue in issues[:20]:
            if isinstance(issue, dict):
                print(f"  - {issue.get('path')}: {issue.get('issue')}")
        return 0

    print(f"Active descriptor registry: {payload.get('count', 0)} descriptors")
    descriptors = (
        payload.get("descriptors")
        if isinstance(payload.get("descriptors"), list)
        else []
    )
    for item in descriptors[:20]:
        if isinstance(item, dict):
            extensions = (
                item.get("extensions")
                if isinstance(item.get("extensions"), list)
                else []
            )
            sample_ext = ", ".join(extensions[:3]) if extensions else "-"
            style = (
                "tags"
                if item.get("uses_semantic_tags")
                else "raw"
                if item.get("uses_raw_outline_patterns")
                or item.get("uses_raw_role_patterns")
                else "basic"
            )
            print(
                f"  - {item.get('name')} ({item.get('source')}, tier={item.get('tier')}, style={style}, ext={sample_ext})"
            )
    return 0


def cmd_snapshots(args: list[str]) -> int:
    """Inspect local copied index snapshots."""
    root = _find_aidocs_root() or Path.cwd()
    as_json = _wants_json(args)
    manifest = (
        root / ".MEMORY" / "related-projects" / "index-snapshots" / "manifest.json"
    )
    if not manifest.is_file():
        payload = {"ok": False, "message": f"Snapshot manifest not found: {manifest}"}
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0
    snapshots = (
        payload.get("snapshots") if isinstance(payload.get("snapshots"), list) else []
    )
    print(f"Index snapshots: {len(snapshots)}")
    for item in snapshots:
        if isinstance(item, dict):
            print(
                f"  - {item.get('name')}: code={item.get('code_files')} schema={item.get('schema_entities')} workflow={item.get('workflow_rule_count')}"
            )
    return 0


def cmd_version(args: list[str]) -> int:
    """Show version."""
    if _wants_json(args):
        print(
            json.dumps(
                {"ok": True, "package": "aidocs-mcp", "version": __version__}, indent=2
            )
        )
        return 0
    print(f"aidocs-mcp {__version__}")
    return 0


def cmd_project_registry(args: list[str]) -> int:
    """Inspect the global MCP-touched project registry."""
    service = ProjectRegistryService()
    payload = {"ok": True, "projects": service.list_projects()}
    if _wants_json(args):
        print(json.dumps(payload, indent=2))
        return 0
    projects = payload["projects"] if isinstance(payload["projects"], list) else []
    print(f"Registered MCP projects: {len(projects)}")
    for item in projects:
        if isinstance(item, dict):
            print(
                f"  - {item.get('title') or item.get('project_root')}: {item.get('project_root')}"
            )
    return 0



def cmd_managed_mode_set(args: list[str]) -> int:
    """Enable managed mode for a project+session."""
    root = _resolve_root(args)
    session_id = None
    for i, arg in enumerate(args):
        if arg == "--session" and i + 1 < len(args):
            session_id = args[i + 1]
    if not session_id:
        print(json.dumps({"ok": False, "error": "Missing --session <id>"}))
        return 1

    from .mcp_server import _resolve_templates_root
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    result = hub.managed_mode.set_mode(root, session_id=session_id, source="dashboard")
    print(json.dumps({"ok": True, "managed_mode": result}))
    return 0


def cmd_managed_mode_clear(args: list[str]) -> int:
    """Disable managed mode for a project."""
    root = _resolve_root(args)

    from .mcp_server import _resolve_templates_root
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    result = hub.managed_mode.clear_mode(root)
    print(json.dumps({"ok": True, "managed_mode": result}))
    return 0


COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "dashboard": cmd_dashboard,
    "dashboard-set-config": cmd_dashboard_set_config,
    "config": cmd_config,
    "sync": cmd_sync,
    "benchmark": cmd_benchmark,
    "descriptors": cmd_descriptors,
    "project-registry": cmd_project_registry,
    "snapshots": cmd_snapshots,
    "version": cmd_version,
    "managed-mode-set": cmd_managed_mode_set,
    "managed-mode-clear": cmd_managed_mode_clear,
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
