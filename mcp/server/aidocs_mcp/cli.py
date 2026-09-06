"""AIDOCS CLI — lightweight command-line interface for common operations.

Usage:
    aidocs setup [path]        Auto-configure MCP + hooks for your IDE (run this first!)
    aidocs doctor              Diagnose installation issues
    aidocs init [path]         Initialize AIDOCS on a project
    aidocs status [path]       Show index stats, session info, module count
    aidocs sync [path]         Run code/schema/memory index sync
    aidocs config              Open the AIDOCS Dashboard for settings
    aidocs dashboard [path]    Emit dashboard snapshot JSON for the desktop app
    aidocs benchmark [path]    Run repeatable benchmark scenarios
    aidocs version             Show version

Runtime / service (#769 (e) — both are LOAD-BEARING and neither was listed
here, though `service status/restart` is how an operator diagnoses a lockout
and `runtime --fix` is the remedy a dozen refusal messages name):
    aidocs runtime [--check]   Report the enforcement runtime: tier, ownership,
                               verification, drift. Exit 0 ok / 1 no owned
                               runtime / 3 STALE (installed package differs from
                               source — hooks enforce older code than the daemon)
    aidocs runtime --fix       Provision or REINSTALL the owned runtime; this is
                               what clears STALE. `runtime fix` also works.
    aidocs runtime --record-package
                               Re-trust the install after a legitimate upgrade
                               (run under the RUNTIME interpreter, not a dev
                               checkout — it records the interpreter it runs
                               under). `aidocs runtime --help` lists the rest.
    aidocs service <cmd>       Local daemon lifecycle (status / restart / ...)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

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


def _resolve_root(args: list[str] | None = None) -> Path:
    """Get project root from a positional path arg, else cwd.

    ``args`` is optional: callers that resolve from the current working
    directory (e.g. the admin subcommands, which use flag-style args whose
    VALUES must NOT be mistaken for a positional root) call it with no
    arguments.

    #553 -- FOURTH OCCURRENCE OF THE LOCKOUT CLASS, and the first fixed at the
    SHARED resolver. This used to be ``[a for a in args if not
    a.startswith("--")]``, which is a SPELLING test, not flag awareness: every
    ``--flag value`` pair puts a bare token in args, so every flag VALUE was a
    candidate project root. Reproduced end-to-end 2026-08-18 --
    ``cmd_checkpoint_gc(['--keep','20','--json'])`` in an empty directory
    returned ok and created ``20/.MEMORY/.index/aidocs.sqlite3`` (73728 bytes,
    initialised schema). An INTEGER became a project root with a real database.

    The three prior incidents (--mode 2026-07-03, --email 2026-07-17,
    --operator-token 2026-07-23) were each cured by giving ONE command family a
    private resolver, leaving this one -- shared by 37 commands -- untouched and
    its defect written verbatim in a test docstring. So this reuses
    ``_positional_args``, the flag-aware scanner already in this module, rather
    than becoming a fourth private copy of the same idea.

    NO ``is_dir()`` GATE HERE, deliberately. ``aidocs init <newdir>`` /
    ``setup <newdir>`` legitimately name a directory that does not exist yet --
    creating it is the point -- so gating on existence would close this defect
    and break project creation in the same stroke. Flag awareness alone closes
    every reproduced case, because a flag VALUE is not a positional whether or
    not it happens to exist.

    RESIDUAL, stated rather than hidden: a BOOLEAN flag absent from
    ``_VALUELESS_FLAGS`` will consume the token after it, so an intended path
    there resolves to cwd instead. That is a wrong-but-safe scope (the caller's
    own directory) where the old behaviour was to adopt the flag's value as a
    root and materialise a database in it. Add such flags to the set as they
    appear.
    """
    # AN EXPLICIT ROOT FLAG WINS, and omitting this broke ~20 tests on Gate 2b
    # (caught 2026-08-18 before it shipped). Many commands pass the root as
    # `--root <path>` / `--project-root <path>`; the OLD naive scan picked that
    # VALUE up by ACCIDENT, because it was the first bare token. Flag awareness
    # correctly skips a flag's value -- and in doing so removed a bug and an
    # accidental feature in the same stroke, silently resolving those callers to
    # cwd. `_resolve_root_flag_style` has honoured these two flags explicitly
    # since 2026-07-17 for exactly this reason; the shared resolver must too.
    _args = list(args or [])
    explicit = (
        _option_value(_args, "--project-root", "").strip()
        or _option_value(_args, "--root", "").strip()
    )
    if explicit:
        return Path(explicit).resolve()
    for candidate in _positional_args(_args, valueless=_VALUELESS_FLAGS):
        return Path(candidate).resolve()
    return Path.cwd()


def _wants_json(args: list[str]) -> bool:
    return "--json" in args


def _resolve_root_flag_style(args: list[str]) -> Path:
    """Project root for the binding surfaces (bindings, dashboard-binding-*):
    honor an explicit ``--project-root <path>`` first (the desktop dashboard
    passes it), then fall back to the shared positional-or-cwd resolution.

    Login commands (operator-login, dashboard-login) DO NOT use this — they
    resolve via ``_resolve_login_root`` (``--project-root`` else cwd, never
    positional) to avoid the 2026-07-17 lockout where a flag VALUE (the
    ``--email``!) was mistaken for a positional root, landing every login in a
    nonexistent project's empty identity DB. The binding commands carry no
    ``--email``, so the positional root many callers/tests pass is safe here.
    """
    # Explicit root flag wins: the desktop dashboard passes `--project-root`,
    # while the binding CLI + its tests pass `--root <path>`. BOTH are honored
    # here — otherwise the flag-aware scan below would treat `--root` as a
    # value-taking flag and SKIP its value, losing the project root and
    # authenticating the operator token against the wrong (cwd) identity DB.
    explicit = (
        _option_value(args, "--project-root", "").strip()
        or _option_value(args, "--root", "").strip()
    )
    if explicit:
        return Path(explicit).resolve()
    # Flag-AWARE positional scan (2026-07-17): `--binding-id hb_x` puts a
    # non-`--` flag VALUE in args, and the naive _resolve_root(args) took it
    # as the project root (proven: root resolved to <cwd>/hb_...). Skip every
    # `--flag value` pair (valueless flags consume only themselves) and honor
    # a surviving positional ONLY when it is a real directory; else cwd.
    valueless = frozenset({"--json", "--audit", "--no-cache"})
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            i += 1 if a in valueless else 2
            continue
        cand = Path(a)
        if cand.is_dir():
            return cand.resolve()
        i += 1
    return Path.cwd()


_ADMIN_SUBCOMMANDS = frozenset({"clear-freeze", "approve-escalation", "deny-escalation"})


# Valueless (boolean) flags: these take NO value, so the parser must not
# consume the token after them as a value (or a positional id like a
# freeze-id right after ``--json`` would be silently eaten).
_VALUELESS_ADMIN_FLAGS = frozenset({"--json"})

# The boolean flags the SHARED resolver must know about (#553). Kept separate
# from the admin set so widening it cannot change admin-command parsing: the
# admin resolver keeps its own narrower set and its existing behaviour. These
# three are the ones _resolve_root_flag_style already enumerated inline.
_VALUELESS_FLAGS = _VALUELESS_ADMIN_FLAGS | frozenset({"--audit", "--no-cache"})


def _positional_args(
    args: list[str],
    *,
    valueless: frozenset[str] | None = None,
) -> list[str]:
    """True positionals — skips ``--flag value`` pairs and bare flags so a
    flag VALUE (e.g. a freeze-id after ``--freeze-id``) is never mistaken
    for a positional. Boolean flags in ``valueless`` consume only
    themselves, so a following positional is preserved.

    ``valueless`` defaults to ``_VALUELESS_ADMIN_FLAGS`` so every existing
    caller keeps its exact behaviour; #553's shared resolver passes the wider
    ``_VALUELESS_FLAGS``.
    """
    vl = _VALUELESS_ADMIN_FLAGS if valueless is None else valueless
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in vl:
            i += 1
            continue
        if a.startswith("--"):
            i += 2  # value-taking flag: consume flag + its value
            continue
        if a.startswith("-"):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _resolve_admin_root(
    args: list[str],
    *,
    freeze_id: str = "",
    session_id: str = "",
    request_id: str = "",
) -> Path:
    """Resolve the project root that OWNS the freeze/escalation named in
    the admin command, by scanning the known-projects registry — so
    ``aidocs admin clear-freeze --freeze-id <id>`` (or --session-id, or a
    positional ``<esc_id>``) works from ANYWHERE, not just inside the
    project. Falls back to cwd when nothing matches.
    """
    freeze_id = freeze_id or _option_value(args, "--freeze-id")
    request_id = request_id or _option_value(args, "--request-id")
    session_id = session_id or _option_value(args, "--session-id")
    fid = freeze_id or request_id
    if not fid and not session_id:
        for a in _positional_args(args):
            if a not in _ADMIN_SUBCOMMANDS:
                fid = a
                break
    if not fid and not session_id:
        return _resolve_root()
    try:
        from .escalation_store import EscalationStore
        from .known_projects_store import KnownProjectsStore
        from .session_freeze_store import SessionFreezeStore

        sfs = SessionFreezeStore()
        esc = EscalationStore()
        for entry in KnownProjectsStore().list_projects():
            root_str = str(entry.get("project_root") or "").strip()
            if not root_str:
                continue
            root = Path(root_str)
            try:
                if fid and sfs.get_active_freeze_by_id(root, fid) is not None:
                    return root
                if session_id and sfs.list_active_freezes(
                    root,
                    session_id=session_id,
                ):
                    return root
                if fid and esc.get(root, fid) is not None:
                    return root
            except Exception:
                continue
    except Exception:
        pass
    return _resolve_root()


def _option_value(args: list[str], name: str, default: str = "") -> str:
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return default


def _resolve_login_root(args: list[str]) -> Path:
    """Project root for the login surfaces (operator-login, dashboard-login).

    Resolution order:
      1. explicit ``--project-root <path>`` — the desktop dashboard / off-repo
         callers always pass this, targeting the right identity DB.
      2. a positional arg that is an EXISTING directory — the long-standing
         ``operator-login <proj> --email ..`` form (and the test suite).
      3. CWD.

    Step 2 gates on ``is_dir()`` on PURPOSE: flag VALUES (the ``--email``, the
    password!) do not start with ``--``, so the bare positional heuristic used
    to pick the email string as the root — landing every ``operator-login
    --email a@b.c`` (no positional) in a nonexistent project's empty identity
    DB, i.e. 'invalid email or password' with CORRECT credentials (2026-07-17
    operator lockout). An email is not a directory, so it now falls through to
    CWD; a real project path is still honored."""
    pr = _option_value(args, "--project-root", "").strip()
    if pr:
        return Path(pr).resolve()
    for a in args:
        if a.startswith("-"):
            continue
        cand = Path(a)
        if cand.is_dir():
            return cand.resolve()
    return Path.cwd()


def _resolve_record_home(default_home: Path) -> tuple[Path, str]:
    """Pick the right base dir for `record_package_integrity`.

    The OUTER GATE verifies trust against its `gate-root` (`_project_root`
    on the gate object), NOT against `Path.home()`. If a deployment laid
    out a gate-root next to the running package or under the user's home,
    recording into `Path.home()` is a silent no-op that leaves every
    remote tool call refusing with `package_untrusted`. This resolver
    auto-detects the gate-root so `aidocs runtime --record-package`
    actually re-trusts the install operators care about. Returns
    `(home, source_label)`; the label is shown to the operator and used
    in tests. Search order (first hit wins):

      1. `<default_home>/aidocs-gate/gate-root/.aidocs/runtime/runtime_trust.db`
         — the canonical VPS layout (`/home/app/aidocs-gate/gate-root`).
      2. `<pkg-dir>/../gate-root/.aidocs/runtime/runtime_trust.db`
         — a gate-root sitting alongside the served package (the install
         pattern used when the gate is installed without a wrapping
         user-home, e.g. a system service under /opt).
      3. `<default_home>` — fall back to ordinary dev / installed-CLI
         behavior. Labelled "home (no gate-root detected)" so the
         operator notices when auto-detect found nothing.

    A gate-root candidate must contain the `runtime_trust.db` file to
    count — an empty `gate-root/` directory next to a fresh install
    isn't authoritative, and we don't want to seed a trust store at a
    path the gate never verifies against.
    """
    db_rel = Path(".aidocs") / "runtime" / "runtime_trust.db"
    # 1) <home>/aidocs-gate/gate-root  (the canonical VPS layout)
    cand = (default_home / "aidocs-gate" / "gate-root").resolve()
    if (cand / db_rel).is_file():
        return cand, f"auto-detected gate-root at {cand}"
    # 2) gate-root sitting next to the installed package
    try:
        pkg_dir = Path(__file__).resolve().parent
        cand2 = (pkg_dir.parent / "gate-root").resolve()
        if (cand2 / db_rel).is_file():
            return cand2, f"auto-detected gate-root at {cand2}"
    except Exception:
        pass
    # 3) fall back to the original behavior
    return default_home, f"home (no gate-root detected; using {default_home})"


def _write_json_output(path_value: str, payload: dict[str, object]) -> None:
    target = Path(path_value).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_safe_json_dumps(payload, indent=2) + "\n", encoding="utf-8")


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
    from .mcp_server import _resolve_script_root, _resolve_templates_root
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(),
        script_root=_resolve_script_root(),
    )
    return hub, RuntimeService(hub=hub)


def _coerce_setting_value(setting_path: str, value: object) -> object:
    """Deprecated alias — the implementation moved to config_schema (#747).

    Kept so existing CLI callers are untouched. The body moved next to
    SETTINGS_CATALOG, the thing it consults, because the operator surface
    needed the SAME rule and a second copy of a security-relevant coercion is
    how two surfaces come to disagree about what 'false' means.
    """
    from .config_schema import coerce_setting_value

    return coerce_setting_value(setting_path, value)


def _readback_setting(
    project_root: Path,
    setting_path: str,
    scope: str,
    session_id: str | None,
) -> object | None:
    """Read the value persisted at the EXACT (scope, scope_key) just
    written — the same key convention _update_project_config_value uses.
    Used for transactional write/readback verification: a write is only
    'verified' when the store reflects the coerced value.
    """
    from .config_store import ConfigStore

    scope_key = (session_id or "") if scope == "session" else ""
    return ConfigStore().get(
        project_root,
        setting_path,
        scope=scope,
        scope_key=scope_key,
    )


def _update_project_config_value(
    project_root: Path,
    setting_path: str,
    value: object,
    scope: str = "project",
    session_id: str | None = None,
    dashboard: bool = False,
) -> Path:
    metadata = SETTINGS_CATALOG.get(setting_path)
    if metadata is None:
        # Phase 5c (2026-05-02): the canonical bash.* namespace is
        # runtime policy tables, not operator-tunable catalog entries
        # (per Empire's "bash is not a catalog entry" decree). The dashboard
        # writes to these via the 3-state allow/deny/bubble UI; skip
        # catalog validation for that specific namespace, dashboard-only.
        if dashboard and setting_path.startswith("bash."):
            from .config_store import ConfigStore as _CS

            scope_key = session_id or "" if scope == "session" else ""
            _CS().set(
                project_root,
                setting_path,
                value,
                scope=scope,
                scope_key=scope_key,
            )
            return _CS().db_path(project_root, scope=scope)
        raise ValueError(f"Unknown config setting: {setting_path}.")
    allowed = metadata.get("allowed_scopes", ["project"])
    if scope not in allowed:
        raise ValueError(
            f"Setting {setting_path} does not support scope '{scope}'. Allowed: {allowed}",
        )
    # Dashboard is the user — skip agent-editable check for security_sensitive settings
    if not dashboard and not is_setting_agent_editable(
        setting_path,
        scope=scope,
        edit_mode="explicit_user_permitted",
    ):
        raise ValueError(f"Config setting requires controlled edit permission: {setting_path}.")
    # Coerce value to match expected type from catalog
    value = _coerce_setting_value(setting_path, value)
    validate_setting_value(setting_path, value)

    # Write to SQLite config store (single source of truth)
    from .config_store import ConfigStore

    store = ConfigStore()
    scope_key = session_id or "" if scope == "session" else ""
    store.set(project_root, setting_path, value, scope=scope, scope_key=scope_key)

    # Return the DB path for diagnostics — pass scope so we echo the file
    # that was actually written (global → ~/.aidocs/config.sqlite3).
    return store.db_path(project_root, scope=scope)


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


def _raw_scan_matches(project_root: Path, query: str, limit: int = 20) -> dict[str, object]:
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
                },
            )

    matches.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return {"query": query, "scanned_files": scanned_files, "matches": matches[:limit]}


BENCHMARK_SCENARIO_SETS = ("public", "aidocs")


def _classification_prompt_batches_for_set(name: str) -> dict[str, list[str]]:
    # Classification probes the prompt CLASSIFIER (project-independent), so the
    # same multilingual batch is used for every scenario set — both the toy
    # public smoke set and the AIDOCS-internal set exercise the same router.
    scenario_set = (name or "public").strip().lower()
    if scenario_set in BENCHMARK_SCENARIO_SETS:
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


def _project_matches_set(name: str, root: Path, hub: object) -> tuple[bool, str]:
    """Whether the target project actually CONTAINS the concepts a scenario set
    asks for. Lets the benchmark distinguish a real regression (matched project,
    empty result) from a scenario/project MISMATCH (wrong project for the set)
    instead of emitting a misleading failure. Returns (matches, reason).
    """
    scenario_set = (name or "public").strip().lower()
    if scenario_set == "public":
        with hub.code.connect(root) as conn:
            row = conn.execute("SELECT 1 FROM code_files LIMIT 1").fetchone()
        if row:
            return True, ""
        return False, "no indexed code files for the public smoke set"
    if scenario_set == "aidocs":
        # The AIDOCS-internal set only makes sense against the AIDOCS source
        # itself — probe a sentinel internal symbol before scoring its
        # scenarios so a toy project is reported as a mismatch, not a failure.
        probe = hub.code.search_symbols(root, "RuntimeService", limit=1)
        if _result_size(probe) > 0:
            return True, ""
        return (
            False,
            "project does not contain AIDOCS internals (RuntimeService symbol not found)",
        )
    return False, f"unknown scenario set: {name}"


def _retrieval_scenarios_for_set(name: str, root: Path, hub: object) -> list[dict[str, object]]:
    scenario_set = (name or "public").strip().lower()
    if scenario_set == "public":
        return _public_retrieval_scenarios(root, hub)
    if scenario_set == "aidocs":
        return _aidocs_retrieval_scenarios(root, hub)
    raise ValueError(f"Unknown benchmark scenario set: {name}")


def _public_retrieval_scenarios(root: Path, hub: object) -> list[dict[str, object]]:
    """Toy/public SMOKE scenarios — they target concepts the project ACTUALLY
    contains (derived from its own index), so a green run means the retrieval
    pipeline returned real results, not that a toy project happened to lack
    AIDOCS-specific concepts.
    """
    first_symbol = ""
    first_file = ""
    with hub.code.connect(root) as conn:
        srow = conn.execute(
            "SELECT symbol FROM code_outlines "
            "WHERE symbol IS NOT NULL AND symbol != '' ORDER BY symbol LIMIT 1",
        ).fetchone()
        if srow and srow[0]:
            first_symbol = str(srow[0])
        frow = conn.execute("SELECT path FROM code_files ORDER BY path LIMIT 1").fetchone()
        if frow and frow[0]:
            first_file = str(frow[0])

    scenarios: list[dict[str, object]] = []
    if first_symbol:
        scenarios.append(
            {
                "name": "find-present-symbol",
                "prompt": "find a symbol the project actually defines (smoke check "
                "that symbol search returns real results)",
                "expected_nonempty": True,
                "runner": lambda s=first_symbol: hub.code.search_symbols(root, s, limit=20),
            },
        )
    if first_file:
        scenarios.append(
            {
                "name": "bundle-present-file",
                "prompt": "give me the real file-level context for a file the "
                "project actually contains, not a guessed path",
                "expected_nonempty": True,
                "runner": lambda t=first_file: hub.code.get_file_bundle(root, t),
            },
        )
    return scenarios


def _aidocs_retrieval_scenarios(root: Path, hub: object) -> list[dict[str, object]]:
    """AIDOCS-INTERNAL retrieval scenarios — they query real AIDOCS concepts
    and so must run against the AIDOCS source itself (see _project_matches_set).
    Every scenario is expected to return results; an empty one on a MATCHED
    project is a real retrieval regression.
    """
    return [
        {
            "name": "investigate-aidocs-entry-flow",
            "prompt": "the /aidocs startup path still feels slippery; show me the main code path that actually matters for bootstrap, command handling, and routing without drowning me in unrelated files",
            "expected_nonempty": True,
            "runner": lambda: hub.code.investigate(root, "aidocs", limit=5),
        },
        {
            "name": "find-symbols-for-init-path",
            "prompt": "I need the symbols that are most likely to matter for init/bootstrap/setup, not a giant repo-wide text search and not every random helper with init in the name",
            "expected_nonempty": True,
            "runner": lambda: hub.code.search_symbols(root, "init", limit=20),
        },
        {
            "name": "trace-runtime-service-usage",
            "prompt": "something around runtime service orchestration is deciding more than I expect; trace where RuntimeService actually gets used in the important paths",
            "expected_nonempty": True,
            "runner": lambda: hub.code.trace_service_usage(root, "RuntimeService", limit=20),
        },
        {
            "name": "bundle-session-subsystem",
            "prompt": "give me the subsystem-level picture for session handling because I care about the important boundaries and supporting structures, not just a single symbol",
            "expected_nonempty": True,
            "runner": lambda: hub.code.get_subsystem_bundle(root, "session", limit=12),
        },
    ]


def _comparative_scenarios_for_set(name: str, root: Path, hub: object) -> list[dict[str, object]]:
    """Indexed-vs-raw latency comparison, with queries matched to the set's
    project so neither side is misleadingly empty.
    """
    scenario_set = (name or "public").strip().lower()
    if scenario_set == "aidocs":
        return [
            {
                "name": "aidocs-entry-flow",
                "query": "aidocs bootstrap routing command",
                "indexed_runner": lambda: hub.code.investigate(root, "aidocs", limit=5),
            },
            {
                "name": "runtime-service-trace",
                "query": "RuntimeService orchestration session bootstrap",
                "indexed_runner": lambda: hub.code.trace_service_usage(
                    root,
                    "RuntimeService",
                    limit=20,
                ),
            },
        ]
    # public: derive a present symbol so both indexed + raw have something real
    present = ""
    with hub.code.connect(root) as conn:
        srow = conn.execute(
            "SELECT symbol FROM code_outlines "
            "WHERE symbol IS NOT NULL AND symbol != '' ORDER BY symbol LIMIT 1",
        ).fetchone()
        if srow and srow[0]:
            present = str(srow[0])
    if not present:
        return []
    return [
        {
            "name": "present-symbol-lookup",
            "query": present,
            "indexed_runner": lambda s=present: hub.code.search_symbols(root, s, limit=20),
        },
    ]


def _schema_scenarios_for_set(name: str, root: Path, hub: object) -> list[dict[str, object]]:
    scenario_set = (name or "public").strip().lower()
    if scenario_set not in BENCHMARK_SCENARIO_SETS:
        raise ValueError(f"Unknown benchmark scenario set: {name}")
    # Schema scenarios are expected to return results ONLY for the AIDOCS set
    # (the source has an indexed schema). For the toy public set a project may
    # legitimately have no schema — that case is reported explicitly (no_schema)
    # rather than as an empty failure.
    expected = scenario_set == "aidocs"

    entities: list[str] = []
    fields: list[str] = []
    with hub.schema.connect(root) as conn:
        entity_rows = conn.execute(
            "SELECT entity_name FROM schema_entities ORDER BY entity_name LIMIT 2",
        ).fetchall()
        field_rows = conn.execute(
            "SELECT field_name FROM schema_fields ORDER BY field_name LIMIT 2",
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
                "expected_nonempty": expected,
                "runner": lambda entity_name=entity: hub.schema.get_schema_entity(
                    root,
                    entity_name,
                ),
            },
        )
        scenarios.append(
            {
                "name": "schema-entity-search",
                "prompt": "find the likely schema entities for this concept without dumping the whole database model",
                "expected_nonempty": expected,
                "runner": lambda entity_name=entity: hub.schema.find_schema_entities(
                    root,
                    query=entity_name,
                    limit=10,
                ),
            },
        )
    if fields:
        field = fields[0]
        scenarios.append(
            {
                "name": "schema-field-search",
                "prompt": "trace the field that sounds relevant here because I need to know which entity owns it and where it shows up",
                "expected_nonempty": expected,
                "runner": lambda field_name=field: hub.schema.find_schema_field(
                    root,
                    field_name,
                    limit=10,
                ),
            },
        )
    return scenarios


def _scenario_status(result_size: int, expected_nonempty: bool, project_match: bool) -> str:
    """Explicit, truthful per-scenario status — no empty result is left to look
    like an ambiguous failure.
    """
    if not project_match:
        return "skipped_mismatch"
    if result_size > 0:
        return "ok"
    return "empty_unexpected" if expected_nonempty else "empty_ok"


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
    # ONE-RUNTIME seal: cmd_setup threads its decided AIDOCS-owned interpreter
    # in via --interpreter so project_init's .mcp.json write uses the SAME
    # interpreter (not the ambient sys.executable). Strip the pair before
    # root resolution so it is never mistaken for the project path.
    interpreter: str | None = None
    if "--interpreter" in args:
        _i = args.index("--interpreter")
        if _i + 1 < len(args):
            interpreter = args[_i + 1]
            args = args[:_i] + args[_i + 2 :]
    root = _resolve_root(args)
    as_json = _wants_json(args)
    if not as_json:
        print(f"Initializing AIDOCS on: {root}")

    from .mcp_server import _resolve_templates_root
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    runtime = RuntimeService(hub=hub)

    result = runtime.project_init(
        root, init_git=False, create_remote=False, interpreter=interpreter
    )
    created = result.get("created", []) if isinstance(result.get("created"), list) else []
    mcp_result = result.get("mcp_config", {}) if isinstance(result.get("mcp_config"), dict) else {}

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
        print(_safe_json_dumps(payload, indent=2))
        return 0

    print(f"Created {len(created)} files")
    for f in created[:10]:
        print(f"  + {f}")
    if len(created) > 10:
        print(f"  ... and {len(created) - 10} more")
    repo_summary = (
        result.get("repo_summary") if isinstance(result.get("repo_summary"), dict) else {}
    )
    bullets = repo_summary.get("bullets") if isinstance(repo_summary.get("bullets"), list) else []
    for bullet in bullets[:4]:
        print(f"  - {bullet}")
    print(f"MCP config: {mcp_result.get('action', 'unknown')}")
    print("\nRun '/aidocs' in your agent to activate managed mode.")
    return 0


def cmd_status(args: list[str]) -> int:
    """Show project status."""
    root = _resolve_root(args)
    as_json = _wants_json(args)

    from .code_index_store import CodeIndexStore
    from .schema_index_store import SchemaIndexStore

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
                ),
            )
        else:
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
        unknown = conn.execute(
            "SELECT COUNT(*) FROM code_files WHERE role IS NULL OR role = ''",
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
            1 for d in sessions_dir.iterdir() if d.is_dir() and (d / "SESSION.md").is_file()
        )

    managed_mode: dict[str, object] = {
        "state": "not_configured",
        "active": False,
        "session_id": None,
    }
    # #437 Phase 1: managed-mode state is DB-canonical — read the sqlite
    # aidocs_managed row via AidocsManagedStore. The legacy
    # .MEMORY/config/aidocs-managed.json ghost read is removed; output
    # shape is unchanged for callers.
    try:
        from .aidocs_managed_store import AidocsManagedStore

        _managed = AidocsManagedStore()
        _managed.init_db(root)
        cfg = _managed.get(root)
        if cfg.get("active"):
            managed_mode = {
                "state": "active",
                "active": True,
                "session_id": cfg.get("session_id") or "none",
            }
        elif cfg.get("session_id") or cfg.get("last_updated"):
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
            "unknown_roles_percent": (unknown * 100 // total_files if total_files else 0),
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
        print(_safe_json_dumps(payload, indent=2))
        return 0

    print(f"AIDOCS Status: {root.name}")
    print(f"{'-' * 40}")
    print(f"  Files indexed:    {total_files} ({parsed} parsed)")
    print(f"  Symbols:          {outlines}")
    print(f"  Modules:          {modules}")
    print(f"  Unknown roles:    {unknown} ({unknown * 100 // total_files if total_files else 0}%)")
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
                ),
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
        # Language vocabulary is SQL-canonical — it lives in the empire
        # intent-tokens store (sqlite), seeded from the bundled factory DB.
        # The seed TOMLs were removed; manage vocab via the `aidocs intent-*`
        # CLI / dashboard, not a file.
        from .intent_tokens_store import empire_db_path

        target = empire_db_path()
        target_kind = "languages"
    else:
        # Settings are managed via the dashboard, not a TOML file.
        msg = "Settings are managed via the AIDOCS Dashboard. Launch with: aidocs dashboard"
        if as_json:
            print(json.dumps({"ok": False, "reason": "use_dashboard", "message": msg}, indent=2))
        else:
            print(msg)
        return 0

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
                ),
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
        print(_safe_json_dumps(payload, indent=2))
        return 0

    print(f"Opening: {target}")
    # Doctrine 2026-05-29 (Empire semgrep re-seal): replaced the
    # os.system editor launch with subprocess.run argv form. No
    # shell=True. $EDITOR may be a multi-word command like
    # `code --wait`, so we shlex.split it into argv head + flags
    # before appending the target path. The target is a resolved
    # AIDOCS path that already passed safe_relpath; even so we
    # never interpolate it into a shell string — it lands as its
    # own argv element where the kernel exec path treats it as a
    # literal filename. Failing-open on a missing editor would be
    # silent — return rc=1 with a helpful message instead.
    import shlex as _shlex
    # Imported UNALIASED: the audited fingerprint records the callee name the
    # AST actually sees, and the doctrine surfaces require it to be one of the
    # canonical spawn callees. `subprocess as _sp` made the honest fingerprint
    # `_sp.run`, which is both unrecognisable to the registry and unmatchable
    # against the callsite scan.
    import subprocess

    editor_argv = _shlex.split(editor)
    if not editor_argv:
        print(f"$EDITOR is empty or whitespace-only: {editor!r}")
        return 1
    try:
        # #1031: an operator-initiated $EDITOR launch is still a spawn, and it
        # belongs in the ledger like every other one. It was invisible only
        # because `**/cli.py` sat in the semgrep exclude list. The console here
        # is DELIBERATE — the operator is meant to see the editor — so it is
        # registered in DELIBERATE_CONSOLE_SPAWNS rather than made windowless.
        from .shell_egress_service import audited_run

        rc = audited_run(
            [*editor_argv, str(target)],
            fingerprint=("cli.py", "cmd_config", "subprocess.run"),
            reason="operator-initiated $EDITOR launch",
            run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603
            check=False,
        ).returncode
    except FileNotFoundError:
        print(f"editor not found on PATH: {editor_argv[0]!r}")
        return 1
    return rc


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
                ),
            )
        else:
            print(f"Not an AIDOCS project: {root}")
        return 1

    from .code_index_store import CodeIndexStore
    from .index_store import IndexStore
    from .mcp_server import _resolve_templates_root
    from .schema_index_store import SchemaIndexStore
    from .session_store import SessionStore

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
    entities = schema_result.get("entities", 0) if isinstance(schema_result, dict) else 0
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
        print(_safe_json_dumps(payload, indent=2))
        return 0

    print(f"Syncing: {root.name}")
    print(
        f"  Memory:  {mem_result.get('memory_files', 0)} files, {mem_result.get('sessions', 0)} sessions ({memory_seconds:.1f}s)",
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
                ),
            )
        else:
            print(f"Not an AIDOCS project: {root}")
        return 1

    from .mcp_server import _resolve_script_root, _resolve_templates_root
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(),
        script_root=_resolve_script_root(),
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
                ),
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

    # Does this project actually contain the concepts the set asks for? Drives
    # the regression-vs-mismatch distinction below.
    project_match, project_match_reason = _project_matches_set(scenario_set, root, hub)

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
                ),
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
                ),
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
                    runtime.classify_prompt_action(prompt).get("action_kind") or "understand",
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
        rsize = _result_size(result)
        expected = bool(scenario.get("expected_nonempty", False))
        retrieval_results.append(
            {
                "name": scenario["name"],
                "prompt": scenario["prompt"],
                "seconds": elapsed,
                "result_size": rsize,
                "expected_nonempty": expected,
                "status": _scenario_status(rsize, expected, project_match),
            },
        )
    retrieval_seconds = round(time.perf_counter() - retrieval_total_start, 3)

    comparative_scenarios = _comparative_scenarios_for_set(scenario_set, root, hub)
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
            },
        )
    comparative_seconds = round(time.perf_counter() - comparative_total_start, 3)

    schema_results = []
    schema_total_start = time.perf_counter()
    for scenario in schema_scenarios:
        start = time.perf_counter()
        result = scenario["runner"]()
        elapsed = round(time.perf_counter() - start, 3)
        rsize = _result_size(result)
        expected = bool(scenario.get("expected_nonempty", False))
        schema_results.append(
            {
                "name": scenario["name"],
                "prompt": scenario["prompt"],
                "seconds": elapsed,
                "result_size": rsize,
                "expected_nonempty": expected,
                "status": _scenario_status(rsize, expected, project_match),
            },
        )
    schema_benchmark_seconds = round(time.perf_counter() - schema_total_start, 3)
    # Explicit schema state: no_schema (none indexed) vs scored.
    if not schema_scenarios:
        schema_status = "no_schema"
    elif any(s["status"] == "empty_unexpected" for s in schema_results):
        schema_status = "regression"
    else:
        schema_status = "ok"

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
            "sessions": sync_result.get("sessions", 0) if isinstance(sync_result, dict) else 0,
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
            "classifications_per_second": round(total_classifications / classify_seconds, 2)
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
            "status": schema_status,
            "scenarios": schema_results,
        },
        "comparative": {
            "scenario_count": len(comparative_scenarios),
            "seconds": comparative_seconds,
            "scenarios": comparative_results,
        },
    }

    # Truthful verdict. A regression = the project MATCHED the set but an
    # expected-nonempty scenario came back empty. A mismatch = wrong project for
    # the set (no false regression). Both are surfaced explicitly; neither is
    # left as a misleading silent pass.
    regressions = [
        s["name"]
        for s in (retrieval_results + schema_results)
        if s.get("status") == "empty_unexpected"
    ]
    notes: list[str] = []
    if not project_match:
        notes.append(
            f"scenario/project mismatch: {project_match_reason}. The '"
            f"{scenario_set}' set was scored against a project that does not "
            "contain its concepts; scenarios were skipped, not failed.",
        )
    if schema_status == "no_schema":
        notes.append(
            "schema benchmark skipped: project has no indexed schema "
            "(expected for the toy public set).",
        )
    regression = bool(project_match and regressions)
    payload["project_match"] = project_match
    payload["project_match_reason"] = project_match_reason
    payload["regression"] = regression
    payload["regressions"] = regressions
    payload["notes"] = notes
    if not project_match:
        payload["ok"] = False
        payload["reason"] = "scenario_project_mismatch"
    elif regression:
        payload["ok"] = False
        payload["reason"] = "retrieval_regression"
    exit_code = 0 if payload["ok"] else 1

    if output_path:
        _write_json_output(output_path, payload)

    if as_json:
        print(_safe_json_dumps(payload, indent=2))
        return exit_code

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
        f"    Throughput:    {payload['classification']['classifications_per_second']} classifications/s",
    )
    for action_kind, count in sorted(counts.items()):
        print(f"    {action_kind}: {count}")
    for language, info in sorted(per_language.items()):
        print(
            f"    [{language}] prompts={info['prompt_count']} total={info['total_classifications']}",
        )
    print(f"  Retrieval:       {retrieval_seconds:.3f}s (project_match={project_match})")
    for scenario in retrieval_results:
        print(
            f"    {scenario['name']}: {scenario['seconds']:.3f}s ({scenario['result_size']} results) [{scenario['status']}]",
        )
    print(f"  Schema bench:    {schema_benchmark_seconds:.3f}s [{schema_status}]")
    if schema_results:
        for scenario in schema_results:
            print(
                f"    {scenario['name']}: {scenario['seconds']:.3f}s ({scenario['result_size']} results) [{scenario['status']}]",
            )
    else:
        print("    no schema scenarios available for this project")
    print(f"  Comparative:     {comparative_seconds:.3f}s")
    for scenario in comparative_results:
        print(
            "    "
            f"{scenario['name']}: indexed={scenario['indexed']['seconds']:.3f}s/{scenario['indexed']['result_size']} "
            f"raw={scenario['raw']['seconds']:.3f}s/{scenario['raw']['result_size']} scanned={scenario['raw']['scanned_files']}",
        )
    if output_path:
        print(f"  Output:          {Path(output_path).resolve()}")
    verdict = "PASS" if payload["ok"] else payload.get("reason", "fail").upper()
    print(f"  Verdict:         {verdict}")
    for note in notes:
        print(f"    note: {note}")
    if regressions:
        print(f"    regressions: {', '.join(regressions)}")
    return exit_code


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
                print(_safe_json_dumps(payload, indent=2))
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
            print(_safe_json_dumps(payload, indent=2, default=str))
        return 0

    snapshot = payload["snapshot"]
    print(f"Dashboard snapshot for: {root}")
    print(f"Sessions: {len(snapshot.get('sessions', []))}")
    print(f"Selected session: {snapshot.get('selected_session_id') or 'none'}")
    return 0


def dashboard_worker_serve(lines, emit, snapshot_fn) -> None:
    """Persistent dashboard worker protocol (dashboard-war (c)).

    One JSON request line in -> one JSON response line out. Transport-free
    pure core (tested in tests/runtime/test_dashboard_worker.py):

      request  {"id": N, "root": "...", "session_id": "..."|null, "live_only": bool?}
      response {"id": N, "ok": true, "snapshot": {...}}

    live_only (#210 slice-loads): the on-change refresh asks for ONLY the
    live sections; snapshot_fn receives it as a keyword and the runtime
    returns the compact slice (live_only=True marker) the frontend merges.
                / {"id": N, "ok": false, "error": "..."}

    A malformed line or a snapshot error answers with ok=false and the
    loop CONTINUES — one bad request must never kill the worker for the
    next one (the Rust side treats a dead worker as fall-back-to-spawn).
    """
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            emit(_safe_json_dumps({"ok": False, "error": f"bad request: {exc}"}))
            continue
        rid = req.get("id")
        root = str(req.get("root") or "").strip()
        if not root:
            emit(_safe_json_dumps({"id": rid, "ok": False, "error": "root required"}))
            continue
        try:
            snapshot = snapshot_fn(
                root,
                req.get("session_id") or None,
                live_only=bool(req.get("live_only")),
            )
            emit(_safe_json_dumps({"id": rid, "ok": True, "snapshot": snapshot}, default=str))
        except Exception as exc:  # noqa: BLE001 — worker must survive any request
            emit(_safe_json_dumps({"id": rid, "ok": False, "error": str(exc)}))


def cmd_dashboard_worker(args: list[str]) -> int:
    """Long-lived snapshot worker for the desktop dashboard: boots the
    runtime ONCE, then serves snapshot requests line-by-line over
    stdin/stdout — replacing the spawn-per-snapshot python storm."""
    _, runtime = _dashboard_runtime()

    def _snapshot(root: str, session_id: str | None, live_only: bool = False):
        return runtime.dashboard_snapshot(
            Path(root), session_id=session_id, live_only=live_only
        )

    def _emit(line: str) -> None:
        print(line, flush=True)

    dashboard_worker_serve(sys.stdin, _emit, _snapshot)
    return 0


# Build-source descriptor TOML directories (project-relative). These are
# the ONLY TOMLs whose authority still lives in the file: language index
# descriptors and the seed gate-message / intent-token sources that the
# package compiles into SQLite at build/seed time. They exist only inside
# the canonical AIDOCS source repo.
_SOURCE_DESCRIPTOR_TOML_DIRS = (
    "mcp/server/aidocs_mcp/index_languages",
    "mcp/server/aidocs_mcp/seed/gate_messages",
    "mcp/server/aidocs_mcp/seed/intent_tokens",
)


def _allowed_toml_targets(root: Path, session_id: str | None) -> dict[str, Path]:
    """TOML files the dashboard editor may WRITE, keyed by project-relative
    path. Authority audit (2026-05): every live config authority has moved
    off TOML —

      * aidocs.toml (project + session) — LEGACY: the tomllib config loader
        was removed; SQLite (config_settings) is canonical. Never editable;
        use typed Settings.
      * gate_messages/ (repo-root) — LEGACY: moved to the package seed +
        resolved_config_store (SQLite). The repo-root dir no longer exists.
      * intent_tokens/ (repo-root) — MIGRATION/build-source: SQL-canonical.

    The ONLY editable TOMLs are dev-flavor canonical-SOURCE build
    descriptors (index-language + seed sources), and only when the project
    IS the AIDOCS source repo on a dev-flavor install. Everywhere else the
    set is empty — the dashboard cannot write a stale TOML authority path.
    """
    try:
        from .enforcement import dev_mode_authorized

        if not dev_mode_authorized(root):
            return {}
    except Exception:
        return {}  # fail closed
    del session_id  # session aidocs.toml is legacy — never editable
    out: dict[str, Path] = {}
    for rel_dir in _SOURCE_DESCRIPTOR_TOML_DIRS:
        d = root / Path(rel_dir)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() == ".toml":
                out[f"{rel_dir}/{p.name}"] = p
    return out


def _toml_display_targets(root: Path, session_id: str | None) -> dict[str, Path]:
    """Every TOML the dashboard editor LISTS (legacy + descriptors), keyed
    by project-relative path. Editability is a separate question
    (_allowed_toml_targets) — this is just what shows in the list so legacy
    paths can be surfaced read-only with a deprecation note.
    """
    out: dict[str, Path] = {}
    proj_cfg = root / "aidocs.toml"
    if proj_cfg.is_file():
        out["aidocs.toml"] = proj_cfg
    sid = (session_id or "").strip()
    if sid:
        sess_cfg = root / ".MEMORY" / "sessions" / sid / "aidocs.toml"
        if sess_cfg.is_file():
            out[f".MEMORY/sessions/{sid}/aidocs.toml"] = sess_cfg
    for rel_dir in ("gate_messages", "intent_tokens", *_SOURCE_DESCRIPTOR_TOML_DIRS):
        d = root / Path(rel_dir)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() == ".toml":
                out[f"{rel_dir}/{p.name}"] = p
    return out


def _toml_legacy_reason(relative: str) -> str:
    """Why a listed TOML is read-only (its runtime authority moved off the
    file). Empty when the path is a live editable descriptor.
    """
    r = relative
    if r == "aidocs.toml" or (r.startswith(".MEMORY/sessions/") and r.endswith("/aidocs.toml")):
        return (
            "Legacy: project config authority moved to SQLite — edit "
            "via typed Settings, not this file."
        )
    if r.startswith("gate_messages/"):
        return "Legacy: gate messages moved to the package seed + SQLite (resolved_config_store)."
    if r.startswith("intent_tokens/"):
        return (
            "Migration/build-source: intent tokens are SQL-canonical; "
            "this TOML is build-source only."
        )
    if "index_languages/" in r or "/seed/" in r:
        return "Source descriptor: editable only in a dev-flavor canonical AIDOCS source repo."
    return "Read-only: authority for this setting is not this TOML file."


def cmd_dashboard_toml_editability(args: list[str]) -> int:
    """Read-only editability verdict for the dashboard TOML editor — the
    SINGLE authority the load/list side consults. Returns, per listed
    project-relative path, {editable, deprecated}: editable iff the path is
    in _allowed_toml_targets (dev-flavor canonical-source descriptors);
    every other listed path is read-only with a deprecation reason. The
    dashboard bridge merges this into the document list so a stale TOML
    authority path can never appear as an editable document.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    session_id = _option_value(args, "--session", "").strip() or None
    editable = set(_allowed_toml_targets(root, session_id).keys())
    verdict: dict[str, dict[str, object]] = {}
    for rel in _toml_display_targets(root, session_id):
        is_editable = rel in editable
        verdict[rel] = {
            "editable": is_editable,
            "deprecated": "" if is_editable else _toml_legacy_reason(rel),
        }
    payload = {"ok": True, "editability": verdict}
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        for rel, v in verdict.items():
            print(f"  {'RW' if v['editable'] else 'ro'}  {rel}")
    return 0


def cmd_dashboard_save_toml(args: list[str]) -> int:
    """Authenticated TOML-document write for the dashboard editor.

    Doctrine: the dashboard must have ONE config write authority. The Tauri
    bridge used to fs::write allowed TOML files directly, outside
    OperatorAuthService. This routes TOML edits through the same operator-
    auth wall as dashboard-set-config: admin-gated, allowlisted path,
    TOML-validated, audited. Content is read from --content-file to avoid
    argv limits.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-save-toml",
        as_json,
    )
    if _rc != -1:
        return _rc
    relative = _option_value(args, "--relative", "").strip()
    content_file = _option_value(args, "--content-file", "").strip()
    session_id = _option_value(args, "--session", "").strip() or None

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    if not relative or not content_file:
        return _emit(
            {
                "ok": False,
                "reason": "missing_args",
                "message": "--relative and --content-file are required",
            },
            1,
        )
    requested = relative.replace("\\", "/").strip()
    target = _allowed_toml_targets(root, session_id).get(requested)
    if target is None:
        return _emit(
            {
                "ok": False,
                "reason": "path_not_allowed",
                "message": (f"TOML path is not part of the dashboard control surface: {requested}"),
            },
            1,
        )
    try:
        content = Path(content_file).read_text(encoding="utf-8")
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "reason": "content_unreadable",
                "message": f"could not read content: {exc}",
            },
            1,
        )
    try:
        import tomllib

        tomllib.loads(content)
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "reason": "toml_invalid",
                "message": f"TOML validation failed for {requested}: {exc}",
            },
            1,
        )
    try:
        # Atomic temp+replace (shared with dashboard-mcp-config): a failed
        # write never leaves a half-written / corrupt TOML — the original
        # stays intact until the replace succeeds.
        _atomic_write_text(target, content)
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "reason": "write_failed",
                "message": f"could not write {target}: {exc}",
            },
            1,
        )
    _audit_admin_command_applied(
        root,
        "dashboard-save-toml",
        _ctx,
        setting_path=requested,
        scope="file",
    )
    return _emit(
        {
            "ok": True,
            "setting_path": requested,
            "message": f"Saved {requested}",
        },
        0,
    )


_MCP_NODE_WRAP = ("npx", "npm", "pnpm", "yarn", "bunx", "deno")
_MCP_TRANSPORTS = ("stdio", "http", "sse")


def _valid_mcp_name(name: str) -> str:
    """Validate an MCP server name (install AND delete). Returns "" when
    valid, else a human reason. A name with path separators / traversal /
    surrounding whitespace is never an addressable server key.
    """
    if not name:
        return "server name is required"
    if any(c in name for c in ("/", "\\", "..")) or name != name.strip():
        return f"invalid server name: {name!r}"
    return ""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: stage to a temp file in the
    SAME directory, fsync, then os.replace (atomic on a single filesystem).
    A crash or error never leaves a half-written / corrupt .mcp.json — the
    original stays intact until the replace succeeds.
    """
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


def _validate_mcp_server_def(
    name: str,
    command: str,
    mcp_args: object,
    transport: str,
) -> str:
    """Validate an MCP server definition as a capability-provider change.
    Returns "" when valid, else a human reason. A bad/abusive definition
    must never be persisted to .mcp.json.
    """
    name_reason = _valid_mcp_name(name)
    if name_reason:
        return name_reason
    if not command or not str(command).strip():
        return "server command is required"
    if not isinstance(mcp_args, list) or not all(isinstance(a, str) for a in mcp_args):
        return "server args must be a list of strings"
    if transport not in _MCP_TRANSPORTS:
        return f"invalid transport {transport!r} (allowed: {', '.join(_MCP_TRANSPORTS)})"
    return ""


def cmd_dashboard_mcp_config(args: list[str]) -> int:
    """List / install / delete / regenerate project MCP servers.

    Doctrine: the ``mcp_servers`` SQLite table is the SOLE source of truth;
    ``.mcp.json`` is a regenerable host PROJECTION of it. An MCP server is a
    CAPABILITY PROVIDER for the agent, so mutating the registry is a control-
    plane mutation — it routes through operator-auth law, not a raw Tauri
    fs::write. Solo/dev installs are local-mintable (the dashboard auto-mints
    the operator token); corpo is policy/admin-gated (real login).

    Reads are a SEPARATE user-safe command (``dashboard-mcp-list``) that
    reads from SQL, NOT from ``.mcp.json`` — so a stale/deleted/corrupt
    projection file can never make the dashboard show something other than
    canonical SQL state. This command is mutation-only.

    Mutation-vs-projection failure semantics (so SQL authority, the
    projection file, and the UI result can never diverge silently):
      * ``committed`` — whether the SQL authority change actually happened.
      * ``projection_stale`` — the SQL change committed but rewriting
        ``.mcp.json`` failed; the file lags SQL until ``--action regenerate``.
        Exit code stays 0 because the authoritative mutation succeeded, and
        ``list`` (SQL-backed) already reflects it. A SQL-write failure instead
        returns ``committed=False`` with a non-zero code and is never audited.

    Args: --action install|delete|regenerate, --name, and for install
    --command, --args <json list>, --transport stdio|http|sse.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    action = _option_value(args, "--action", "install").strip().lower()
    name = _option_value(args, "--name", "").strip()

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    if action not in ("install", "delete", "regenerate"):
        return _emit(
            {
                "ok": False,
                "reason": "bad_action",
                "message": "--action must be install, delete, or "
                "regenerate (use dashboard-mcp-list to "
                "list)",
            },
            1,
        )

    from .mcp_registry_store import McpRegistryStore

    store = McpRegistryStore()

    # Mutations route through operator-auth law.
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-mcp-config",
        as_json,
    )
    if _rc != -1:
        return _rc

    # Pure projection: rebuild .mcp.json from SQL. No authority mutation.
    if action == "regenerate":
        try:
            store.project_to_file(root)
        except Exception as exc:
            return _emit(
                {
                    "ok": False,
                    "reason": "projection_failed",
                    "message": f"could not regenerate .mcp.json: {exc}",
                },
                1,
            )
        return _emit(
            {"ok": True, "projection_stale": False, "message": "Regenerated .mcp.json from SQL"},
            0,
        )

    if not name:
        return _emit({"ok": False, "reason": "missing_name", "message": "--name is required"}, 1)

    if action == "delete":
        name_reason = _valid_mcp_name(name)
        if name_reason:
            return _emit({"ok": False, "reason": "validation_failed", "message": name_reason}, 1)
        try:
            existed = store.delete(root, name)  # SQL authority mutation
        except Exception as exc:
            return _emit(
                {
                    "ok": False,
                    "committed": False,
                    "reason": "write_failed",
                    "message": f"could not update MCP registry: {exc}",
                },
                1,
            )
        if not existed:
            # No-op: nothing in the registry → no projection rewrite/audit.
            return _emit(
                {
                    "ok": True,
                    "name": name,
                    "removed": False,
                    "committed": False,
                    "message": f"MCP server '{name}' not present",
                },
                0,
            )
        # Authority changed — audit BEFORE projecting so the audit reflects
        # the committed SQL mutation even if the file rewrite then fails.
        _audit_admin_command_applied(
            root,
            "dashboard-mcp-config",
            _ctx,
            setting_path=f"mcpServers/{name}",
            scope="sqlite",
            deleted=True,
        )
        try:
            store.project_to_file(root)
        except Exception as exc:
            return _emit(
                {
                    "ok": True,
                    "name": name,
                    "removed": True,
                    "committed": True,
                    "projection_stale": True,
                    "reason": "projection_stale",
                    "message": f"MCP server '{name}' removed from SQL; "
                    f".mcp.json not regenerated — run "
                    f"--action regenerate ({exc})",
                },
                0,
            )
        return _emit(
            {
                "ok": True,
                "name": name,
                "removed": True,
                "committed": True,
                "projection_stale": False,
                "message": f"MCP server '{name}' removed",
            },
            0,
        )

    # install / update
    command = _option_value(args, "--command", "").strip()
    transport = _option_value(args, "--transport", "stdio").strip() or "stdio"
    raw_args = _option_value(args, "--args", "").strip()
    mcp_args: object = []
    if raw_args:
        try:
            mcp_args = json.loads(raw_args)
        except Exception as exc:
            return _emit(
                {
                    "ok": False,
                    "reason": "args_invalid",
                    "message": f"--args must be a JSON list: {exc}",
                },
                1,
            )
    reason = _validate_mcp_server_def(name, command, mcp_args, transport)
    if reason:
        return _emit({"ok": False, "reason": "validation_failed", "message": reason}, 1)

    # Windows: node-ecosystem launchers ship as .cmd; wrap in cmd /c so
    # CreateProcess resolves them (mirrors the former bridge behavior).
    final_command, final_args = command, list(mcp_args)
    import sys as _sys

    if _sys.platform == "win32" and command in _MCP_NODE_WRAP:
        final_command = "cmd"
        final_args = ["/c", command, *mcp_args]

    try:
        store.upsert(
            root,
            name,
            command=final_command,
            args=final_args,
            transport=transport,
        )  # SQL authority mutation
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "committed": False,
                "reason": "write_failed",
                "message": f"could not write MCP registry: {exc}",
            },
            1,
        )
    # Authority changed — audit BEFORE projecting (see delete rationale).
    _audit_admin_command_applied(
        root,
        "dashboard-mcp-config",
        _ctx,
        setting_path=f"mcpServers/{name}",
        scope="sqlite",
    )
    try:
        store.project_to_file(root)  # .mcp.json is regenerated from SQL
    except Exception as exc:
        return _emit(
            {
                "ok": True,
                "name": name,
                "committed": True,
                "projection_stale": True,
                "reason": "projection_stale",
                "message": f"MCP server '{name}' installed in SQL; "
                f".mcp.json not regenerated — run --action "
                f"regenerate ({exc})",
            },
            0,
        )
    return _emit(
        {
            "ok": True,
            "name": name,
            "committed": True,
            "projection_stale": False,
            "message": f"MCP server '{name}' installed",
        },
        0,
    )


def cmd_dashboard_mcp_list(args: list[str]) -> int:
    """List project MCP servers from the canonical SQLite registry.

    Read-only and user-safe (no operator auth). Reads the mcp_servers SQL
    table — NOT .mcp.json — so a stale, deleted, or corrupt projection file
    can never hide or distort the registry the dashboard shows. Emits
    {"ok": True, "servers": [{name, type, command, args, source}, ...]}.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            for srv in payload.get("servers", []) or []:
                print(f"{srv.get('name')}\t{srv.get('command')}")
        return code

    from .mcp_registry_store import McpRegistryStore

    try:
        servers = McpRegistryStore().list_servers(root)
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "reason": "read_failed",
                "message": f"could not read MCP registry: {exc}",
            },
            1,
        )
    return _emit({"ok": True, "servers": servers}, 0)


def cmd_migrate_control_authority(args: list[str]) -> int:
    """Explicit, authenticated one-time import of legacy control files into
    the canonical SQLite stores.

    This is the ONLY sanctioned path that imports a pre-registry ``.mcp.json``
    (→ mcp_servers) or existing ``SESSION.md`` records (→ session_membership)
    into authority. Normal read/write paths NEVER do this — so a stale or
    freshly placed legacy file can never mint control authority through a
    read. Each store imports once and seals a marker; a second run is a
    no-op (already_migrated), so a later placed file cannot be re-imported.

    Admin-gated (solo/dev local-mintable; corpo login) because importing
    legacy files SETS control authority. Idempotent and safe to re-run.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "migrate-control-authority",
        as_json,
    )
    if _rc != -1:
        return _rc

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    from .mcp_registry_store import McpRegistryStore
    from .session_membership_store import SessionMembershipStore

    try:
        mcp_result = McpRegistryStore().migrate_legacy_once(root)
        sess_result = SessionMembershipStore().migrate_legacy_once(root)
    except Exception as exc:
        # Truthful FAILURE audit — a failed migration must not look applied,
        # and the unsealed marker preserves retry on the next run.
        _audit_admin_command(
            root,
            "migrate-control-authority",
            _ctx,
            status="failed",
            setting_path="control_authority/legacy_import",
            scope="sqlite",
            error=repr(exc),
        )
        return _emit(
            {
                "ok": False,
                "reason": "migration_failed",
                "message": f"legacy migration failed: {exc}",
            },
            1,
        )

    # Truthful APPLIED vs NO_OP: "applied" only if a store actually ran the
    # import this call (status == "migrated"); when both were already sealed
    # the run changed nothing → "no_op". A repeated migration therefore can
    # never mislead the audit into showing a fresh control-plane mutation.
    applied = mcp_result["status"] == "migrated" or sess_result["status"] == "migrated"
    mcp_skipped = mcp_result.get("skipped", [])
    sess_skipped = sess_result.get("skipped", [])
    _audit_admin_command(
        root,
        "migrate-control-authority",
        _ctx,
        status="applied" if applied else "no_op",
        setting_path="control_authority/legacy_import",
        scope="sqlite",
        mcp_status=mcp_result["status"],
        mcp_imported=mcp_result["imported"],
        mcp_skipped=mcp_skipped,
        sessions_status=sess_result["status"],
        sessions_imported=sess_result["imported"],
        sessions_skipped=sess_skipped,
    )
    return _emit(
        {
            "ok": True,
            "applied": applied,
            "mcp_servers": mcp_result,
            "sessions": sess_result,
            "message": (
                f"control-authority migration "
                f"({'applied' if applied else 'no-op'}): "
                f"mcp_servers={mcp_result['status']}"
                f"(+{mcp_result['imported']}, "
                f"skipped {len(mcp_skipped)}), "
                f"sessions={sess_result['status']}"
                f"(+{sess_result['imported']}, "
                f"skipped {len(sess_skipped)})"
            ),
        },
        0,
    )


def cmd_governed_delete(args: list[str]) -> int:
    """Governed deletion — the ONE agent/CLI surface for deleting a file.

    Deletion is an audited, reversible mutation, never blind cleanup. The
    handler classifies the target and routes it (checkpoint-then-delete /
    quarantine / hard-delete-if-regenerable / refuse) per the governed_deletion
    doctrine; unsafe paths (absolute/traversal/symlink-escape/outside-root) and
    protected/control-authority/checkpoint paths are refused. This surface does
    NOT expose the protected-route override — agents may clean owned temp and
    reversibly remove source, but can never force-delete a protected file.

    Args: --path <project-relative>, --reason <text>.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    rel = _option_value(args, "--path", "").strip()
    reason = _option_value(args, "--reason", "").strip()

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    if not rel:
        return _emit({"ok": False, "outcome": "refused", "message": "--path is required"}, 1)
    from dataclasses import asdict

    from .governed_deletion import governed_delete

    res = governed_delete(root, rel, reason=reason, principal_type="agent")
    payload = asdict(res)
    payload["message"] = f"{res.outcome}: {rel} ({res.reason})"
    return _emit(payload, 0 if res.ok else 1)


def cmd_governed_restore(args: list[str]) -> int:
    """Restore a governed-deletion checkpoint by id (audited ``restored``).
    Args: --checkpoint <id>.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    cpid = _option_value(args, "--checkpoint", "").strip()

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    if not cpid:
        return _emit({"ok": False, "outcome": "refused", "message": "--checkpoint is required"}, 1)
    from dataclasses import asdict

    from .governed_deletion import restore_deletion

    res = restore_deletion(root, cpid, principal_type="agent")
    payload = asdict(res)
    payload["message"] = f"{res.outcome}: {res.path or cpid} ({res.reason})"
    return _emit(payload, 0 if res.ok else 1)


def cmd_checkpoint_gc(args: list[str]) -> int:
    """Audited checkpoint lifecycle/GC — the ONLY sanctioned way to remove
    checkpoints (the normal delete path refuses the checkpoint store).

    This is the AGENT-SAFE (conservative) surface: a hard keep-floor and
    min-age floor are enforced inside the service, so no --keep/--max-age the
    agent passes can void recent/active rollback state — it can only tidy
    genuinely old checkpoints. Aggressive (floor-lifting) pruning is an
    operator-only capability and is NOT reachable from this surface.
    Args: --keep <N> (default 20), --max-age-seconds <S> (optional).
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    try:
        keep = int(_option_value(args, "--keep", "20") or "20")
    except ValueError:
        keep = 20
    raw_age = _option_value(args, "--max-age-seconds", "").strip()
    max_age = None
    if raw_age:
        try:
            max_age = int(raw_age)
        except ValueError:
            max_age = None
    from .checkpoint_service import CheckpointService

    out = CheckpointService(root).gc(keep=keep, max_age_seconds=max_age, principal_type="agent")
    out["message"] = f"checkpoint gc: pruned {len(out['pruned'])}, kept {out['kept']}"
    if as_json:
        print(_safe_json_dumps(out, indent=2, default=str))
    else:
        print(out["message"])
    return 0


def cmd_ai_restore(args: list[str]) -> int:
    """ai_restore — one restoration facade over all destructive-mutation
    history (git checkpoints, quarantine/tombstone, governed deletion + edit
    checkpoints, projection restores). The agent never needs to know whether a
    snapshot came from git or quarantine.

    --mode list|timeline|inspect|diff|nearest|restore
      list      filter by --path/--task/--plan/--session/--lane
      timeline  --path : current vs previous versions
      inspect   --checkpoint <id>
      diff      --checkpoint <id> [--path]
      nearest   --path [--before <iso>]
      restore   --checkpoint <id> --reason "<intent>" [--dry-run]
                (exact id required; a non-dry-run destructive restore REQUIRES
                a meaningful --reason; takes a pre-restore checkpoint; reuses
                path/protected/authority guards)

    Every mode exits nonzero when its result is ok=false (refused/failed/
    unsafe), so callers can trust the exit code, not just the JSON.
    """
    # Root = TRUE positional only (flag VALUES like "--mode inspect" or a
    # checkpoint id must never be mistaken for a root — the 2026-07-03 bug
    # where every documented flag-only invocation resolved root to
    # <cwd>/<mode> and refused real checkpoints as checkpoint_not_found).
    _pos = _positional_args(args)
    root = Path(_pos[0]).resolve() if _pos else Path.cwd()
    as_json = _wants_json(args)
    mode = _option_value(args, "--mode", "list").strip().lower()
    path = _option_value(args, "--path", "").strip() or None
    checkpoint = _option_value(args, "--checkpoint", "").strip()
    dry_run = "--dry-run" in args
    from . import restore_service as rs

    def _emit(payload: dict, code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(_safe_json_dumps(payload, indent=2, default=str))
        return code

    if mode == "list":
        out = rs.list_restore_points(
            root,
            path=path,
            task=_option_value(args, "--task", "").strip() or None,
            plan=_option_value(args, "--plan", "").strip() or None,
            session=_option_value(args, "--session", "").strip() or None,
            lane=_option_value(args, "--lane", "").strip() or None,
        )
        return _emit(out, 0 if out.get("ok") else 1)
    if mode == "timeline":
        if not path:
            return _emit({"ok": False, "reason": "--path required"}, 1)
        out = rs.timeline(root, path)
        return _emit(out, 0 if out.get("ok") else 1)
    if mode == "inspect":
        if not checkpoint:
            return _emit({"ok": False, "reason": "--checkpoint required"}, 1)
        out = rs.inspect(root, checkpoint)
        return _emit(out, 0 if out.get("ok") else 1)
    if mode == "diff":
        if not checkpoint:
            return _emit({"ok": False, "reason": "--checkpoint required"}, 1)
        out = rs.diff(root, checkpoint, path=path)
        return _emit(out, 0 if out.get("ok") else 1)
    if mode == "nearest":
        if not path:
            return _emit({"ok": False, "reason": "--path required"}, 1)
        out = rs.nearest(root, path, before=_option_value(args, "--before", "").strip() or None)
        return _emit(out, 0 if out.get("ok") else 1)
    if mode == "restore":
        if not checkpoint:
            return _emit(
                {
                    "ok": False,
                    "outcome": "refused",
                    "reason": "an exact --checkpoint id is required",
                },
                1,
            )
        out = rs.restore(
            root,
            checkpoint,
            dry_run=dry_run,
            reason=_option_value(args, "--reason", "").strip(),
            principal_type="agent",
        )
        return _emit(out, 0 if out.get("ok") else 1)
    return _emit({"ok": False, "reason": f"unknown --mode: {mode}"}, 1)


def cmd_index_sitter(args: list[str]) -> int:
    """ProjectIndexSitter control — keep the code index truthful for external
    (non-AIDOCS) file add/edit/delete.

    --status (default)  lifecycle + known-stale + backends (watchdog/polling)
    --check             report freshness; exits nonzero when stale (truthful)
    --sync-now / --fix  run a full reconcile now (adds new, refreshes edited,
                        removes deleted), then report
    """
    root = _resolve_root(args)

    def _emit(payload: dict, code: int) -> int:
        print(_safe_json_dumps(payload, indent=2, default=str))
        return code

    from . import project_index_sitter as sitter

    if "--status" in args or not any(a in args for a in ("--check", "--fix", "--sync-now")):
        return _emit(sitter.index_sitter_status(root), 0)

    from .mcp_server import _resolve_script_root, _resolve_templates_root
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(),
        script_root=_resolve_script_root(),
    )

    if "--check" in args:
        try:
            fresh = (hub.code.code_status(root) or {}).get("freshness") or {}
        except Exception as exc:
            return _emit({"ok": False, "reason": f"freshness failed: {exc}"}, 1)
        state = str(fresh.get("state") or "")
        known = sitter.is_index_known_stale(root)
        ok = state == "ready" and not known
        return _emit(
            {"ok": ok, "mode": "check", "freshness": fresh, "known_stale": known},
            0 if ok else 1,
        )

    # --fix / --sync-now → reconcile now
    res = sitter.reconcile(root, hub, trigger="cli")
    return _emit({"mode": "sync-now", **res}, 0 if res.get("ok") else 1)


#: `aidocs runtime` options that take no value.
_RUNTIME_FLAGS = frozenset({
    "--check", "--fix", "--rebuild", "--record-package", "--allow-ambient",
    "--verify-pins", "--json",
    # #973: reconcile the DEPLOYED external hook shim against the packaged
    # source. Exists as a NAMED argv precisely so the runtime refresh can invoke
    # it in a FRESH PROCESS after a package swap — an in-process call would run
    # the modules imported BEFORE the swap and redeploy the old bytes while
    # reporting success.
    "--reconcile-hook-shim",
})
#: options that consume the following token (or use --opt=value).
_RUNTIME_VALUE_OPTS = frozenset({
    "--home", "--offline-archive", "--base-python", "--sha256", "--url",
    "--manifest", "--package",
    # --source WAS MISSING AND IT COST A LOCKOUT. cmd_runtime reads it
    # (`_option_value(args, "--source") or "cli"`), and package_integrity
    # invokes `aidocs runtime --record-package --source <who>` to write the
    # trust row. #769 added this allowlist to REFUSE unknown arguments -- right
    # in principle -- but omitted one option the command genuinely consumes, so
    # the refusal fired on a legitimate call:
    #
    #   action: venv/installed ok=True package_record_failed: selected runtime
    #           record exited 2: Unknown argument for `aidocs runtime`: '--source'
    #   [runtime-refresh] COULD NOT re-record package trust
    #
    # Observed in the 2026-08-19 deploy log. The runtime refreshed fine; only
    # the TRUST RECORD failed, which is the state where the gate refuses every
    # tool call -- the "why does every deploy block AIDOCS" lockout. A stricter
    # gate that rejects a real argument does not fail safe, it fails SHUT.
    # test_runtime_argument_allowlist_covers_every_consumed_option now derives
    # the consumed set from the source so this cannot recur silently.
    "--source",
})
#: bare-word spellings accepted as their flag (#769 (b) — accept it, or refuse
#: loudly; silence is the one thing that is not allowed).
_RUNTIME_SUBCOMMAND_ALIASES = {
    "check": "--check",
    "fix": "--fix",
    "rebuild": "--rebuild",
    "record-package": "--record-package",
    "verify-pins": "--verify-pins",
}


def _normalise_runtime_args(args: list[str]) -> list[str] | int:
    """Map bare-word subcommands onto their flags and REFUSE the unrecognised.

    Returns the normalised argv, or an int EXIT CODE when the call was answered
    here (help, or a refusal). Refusing is the whole point: `aidocs runtime fix`
    used to match nothing, fall through to --check, and print a status report,
    so a typo became a silent wrong action and then a false bug report (#769).
    """
    out: list[str] = []
    expect_value = False
    for token in list(args or []):
        if expect_value:
            out.append(token)
            expect_value = False
            continue
        if token in ("--help", "-h", "help"):
            print(cmd_runtime.__doc__ or "aidocs runtime — see --check/--fix.")
            return 0
        if token in _RUNTIME_SUBCOMMAND_ALIASES:
            out.append(_RUNTIME_SUBCOMMAND_ALIASES[token])
            continue
        if token in _RUNTIME_FLAGS:
            out.append(token)
            continue
        base = token.split("=", 1)[0]
        if base in _RUNTIME_VALUE_OPTS:
            out.append(token)
            expect_value = "=" not in token
            continue
        known = sorted(_RUNTIME_FLAGS | _RUNTIME_VALUE_OPTS)
        hint = ""
        alias = _RUNTIME_SUBCOMMAND_ALIASES.get(token.lstrip("-"))
        if alias:
            hint = f" Did you mean `{alias}`?"
        print(
            f"Unknown argument for `aidocs runtime`: {token!r}.{hint}\n"
            f"  Valid: {', '.join(known)}\n"
            f"  Bare-word forms also accepted: "
            f"{', '.join(sorted(_RUNTIME_SUBCOMMAND_ALIASES))}\n"
            "  Refusing rather than falling through to --check: a silently "
            "ignored argument runs the WRONG command and reports success "
            "(#769).",
        )
        return 2
    if expect_value:
        print(
            f"Missing value for the last option in: {' '.join(args)}\n"
            "  Refusing rather than guessing.",
        )
        return 2
    return out


def cmd_runtime(args: list[str]) -> int:
    """AIDOCS-owned runtime doctor — the enforcement-interpreter boundary.

    --check (default)   report the resolved tier (operator_pin/standalone/venv/
                        ambient/none), ownership, verification, manifest, drift;
                        exits nonzero unless a verified AIDOCS-owned runtime
                        resolves.
    --fix               provision an owned runtime if none verifies (standalone
                        when pinned, else degraded venv) — AND reinstall an
                        already-owned venv whose installed package no longer
                        matches source (`package_fresh: false`), because
                        "verified" only means unchanged since install.
    --record-package    stamp the CURRENT aidocs_mcp package fingerprint +
                        provenance into runtime.json (the trusted-code boundary).
                        Run after a legitimate upgrade to re-trust the install.
                        Auto-detects an outer-gate `gate-root` (canonical at
                        `<home>/aidocs-gate/gate-root`, or next to the served
                        package) and records there; falls back to `Path.home()`
                        only when no gate-root exists. Override with `--home`.
    --home P            override the base dir for --record-package. Use the
                        gate-root the gate verifies against (e.g.
                        `/home/app/aidocs-gate/gate-root` on the canonical VPS).
    --rebuild           force a fresh standalone (then venv) reinstall.
    --offline-archive P install the standalone from a local archive P (no
                        network); its SHA256 must match --sha256/--manifest.
    --sha256 H          pinned SHA256 for --offline-archive when PINNED has no
                        platform entry (no SHA ⇒ refused, fail-closed).
    --manifest P        JSON file with {url?, sha256} instead of --sha256.
    --url U             record the source URL for an offline archive.
    --package SPEC      aidocs_mcp version to pin (e.g. 1.2.3) or a local
                        wheel/sdist/source path; default pins this build's
                        version.
    --base-python P     base interpreter for the degraded venv fallback.
    --allow-ambient     permit ambient to count (dev escape; still reported).
    --verify-pins       MAINTENANCE: fetch the upstream python-build-standalone
                        SHA256SUMS and verify every PINNED url/sha/version/triple
                        (network); exits nonzero on any mismatch/missing asset.
    """
    # #769 (a)/(b): REFUSE what we do not understand. main() already refuses an
    # unknown top-level command ("Unknown command: {cmd}") while this parser
    # refused nothing, so `aidocs runtime fix` matched no option, fell through
    # to --check, printed a STATUS REPORT, and the repair was never called.
    # The operator then filed "the fix command does not fix" — a FALSE BUG
    # REPORT manufactured by a silent fallthrough, which cost a later
    # verification pass to undo. A CLI that refuses an unknown command in one
    # breath and ignores an unknown subcommand in the next will keep producing
    # exactly that.
    args = _normalise_runtime_args(args)
    if isinstance(args, int):
        return args

    as_json = _wants_json(args)
    from . import runtime_provisioner as rp

    if "--reconcile-hook-shim" in args:
        # #973. Reconcile the DEPLOYED external shim against the packaged
        # source. Invoked by the runtime refresh in a FRESH process right after
        # a package swap, so the bytes written here are the NEWLY INSTALLED
        # ones — the whole reason this is a subcommand and not an in-process
        # call at the swap site.
        from .claude_hooks_install import ensure_hook_shim, shim_path, shim_source

        placed = ensure_hook_shim()
        try:
            fresh = bool(
                placed and Path(placed).read_text(encoding="utf-8") == shim_source()
            )
        except OSError:
            fresh = False
        rep = {
            "ok": fresh,
            "shim": str(placed or shim_path(Path.home())),
            "placed": placed is not None,
            "fresh": fresh,
        }
        if as_json:
            print(_safe_json_dumps(rep, indent=2))
        elif fresh:
            print(f"hook shim reconciled: {rep['shim']}")
        else:
            # Fail-closed and LOUD: the deployed shim is what governs every
            # host-native tool. Silence here would leave the box enforcing
            # whatever stale copy is on disk with nothing saying so.
            print(
                f"hook shim NOT reconciled: {rep['shim']} — the deployed copy "
                "does not match the packaged source, so host-native tools are "
                "governed by stale code.",
            )
        return 0 if fresh else 1

    if "--verify-pins" in args:
        try:
            rep = rp.verify_pinned_against_upstream()
        except Exception as exc:  # network/parse failure
            msg = {"ok": False, "error": repr(exc)}
            print(
                _safe_json_dumps(msg, indent=2)
                if as_json
                else f"pin verify FAILED to fetch: {exc!r}",
            )
            return 2
        if as_json:
            print(_safe_json_dumps(rep, indent=2, default=str))
        else:
            flag = "\033[32mOK\033[0m" if rep["ok"] else "\033[31mFAIL\033[0m"
            print(
                f"PINNED vs upstream [{flag}] release={rep['release']} "
                f"version={rep['version']} checked={rep['checked']}",
            )
            for r in rep["results"]:
                mark = "ok" if r["ok"] else "FAIL " + ",".join(r["problems"])
                print(f"  {r['platform']}: {mark}")
        return 0 if rep["ok"] else 1

    if "--record-package" in args:
        from pathlib import Path as _Path

        from . import package_integrity as _pi

        _src = _option_value(args, "--source") or "cli"
        # Home resolution: explicit --home wins; otherwise auto-detect the
        # outer-gate's gate-root if one exists adjacent to a typical
        # aidocs-gate install (e.g. /home/<user>/aidocs-gate/gate-root or
        # ./gate-root next to the running package); fall back to Path.home()
        # for ordinary dev / installed-CLI usage. This closes BACKLOG C.17:
        # the gate verifies trust against gate-root, so recording into
        # Path.home() while a gate-root exists is a silent no-op that
        # leaves every tool call refusing with `package_untrusted`.
        _explicit = _option_value(args, "--home").strip()
        if _explicit:
            _home = _Path(_explicit).resolve()
            _home_source = "explicit (--home)"
        else:
            _home, _home_source = _resolve_record_home(_Path.home())
        # Stamp the resolved home so callers (and tests) can audit which
        # store the record went into. Doctrine: if there's a gate-root we
        # MUST use it; ignoring it would re-introduce the silent-no-op bug.
        m = _pi.record_package_integrity(_home, source=_src)
        m = dict(m)
        m["resolved_home"] = str(_home)
        m["home_source"] = _home_source
        rep = {
            k: m.get(k)
            for k in (
                "package_provenance",
                "package_version",
                "package_fingerprint",
                "package_files",
                "package_mutable",
                "package_recorded_at",
            )
        }
        if as_json:
            rep["resolved_home"] = m["resolved_home"]
            rep["home_source"] = m["home_source"]
            print(_safe_json_dumps(rep, indent=2, default=str))
        else:
            print("recorded package integrity into runtime.json:")
            print(f"  home       : {m['resolved_home']}  ({m['home_source']})")
            print(f"  provenance : {rep['package_provenance']}  (mutable={rep['package_mutable']})")
            print(f"  version    : {rep['package_version']}")
            print(f"  files      : {rep['package_files']}")
            print(f"  fingerprint: {rep['package_fingerprint']}")
            if rep["package_mutable"]:
                print(
                    "  \033[33mnote:\033[0m editable/dev install — never "
                    "remote-trustworthy; fingerprint is informational only.",
                )
            if not m.get("recorded"):
                print(
                    "  \033[31m✗\033[0m canonical DB write FAILED — trust "
                    "UNRECORDED (runtime.json marked projection_failed).",
                )
        # Exit nonzero when the canonical DB row was NOT written, so a parent
        # (e.g. setup invoking the selected runtime) detects the failure.
        return 0 if m.get("recorded") else 1

    fix = "--fix" in args
    rebuild = "--rebuild" in args
    allow_ambient = "--allow-ambient" in args
    offline = _option_value(args, "--offline-archive") or None
    base_python = _option_value(args, "--base-python") or None
    sha256 = _option_value(args, "--sha256") or None
    url = _option_value(args, "--url") or None
    manifest_file = _option_value(args, "--manifest") or None
    package = _option_value(args, "--package") or None

    report = rp.doctor(
        fix=fix,
        rebuild=rebuild,
        offline_archive=offline,
        base_python=base_python,
        sha256=sha256,
        url=url,
        manifest_file=manifest_file,
        package_spec=package,
        allow_ambient=allow_ambient,
    )

    if as_json:
        print(_safe_json_dumps(report, indent=2, default=str))
    else:
        tier = report.get("tier")
        flag = "\033[32mOK\033[0m" if report["ok"] else "\033[31mFAIL\033[0m"
        print(
            f"AIDOCS runtime [{flag}] tier={tier} "
            f"owned={report['owned']} verified={report['verified']} "
            f"degraded={report['degraded']}",
        )
        if report.get("python"):
            print(f"  interpreter: {report['python']}")
        prov = report.get("provenance") or {}
        pc = report.get("provenance_class")
        label = (
            "OFFICIAL/blessed"
            if report.get("blessed")
            else f"operator-custom ({pc})"
            if pc == "custom"
            else pc
        )
        print(
            f"  provenance: {label} | source={prov.get('source')} "
            f"version={prov.get('version')} package={prov.get('package')}",
        )
        print(
            f"  blessed CPython: {report.get('blessed_version')} "
            f"| resolved: {report.get('resolved_version')} "
            f"| expected aidocs_mcp: {report.get('expected_version')}",
        )
        if report.get("drift_detected"):
            print("  \033[33m!\033[0m drift: manifest runtime no longer verifies")
        if report.get("package_fresh") is False:
            print(
                "  \033[33m!\033[0m STALE: the installed enforcement package "
                "differs from source (hooks run older code than the daemon)",
            )
        for act in report.get("actions", []):
            # #932: SAY HOW WIDE THE NO-PACKAGE WINDOW WAS. provision_venv has
            # returned `package_swap` ("none" | "staged" | "unstaged") at four
            # sites since it was written, and NOTHING has ever printed it — so
            # the one fact that distinguishes a ~1s staged unpack from the
            # UNBOUNDED unstaged fallback was populated, carried, and invisible
            # (183074ae: a diagnostic with no consumer at the point of need).
            #
            # It matters because during that window EVERY tool call is denied.
            # Measured 2026-08-27: 37s of package absence, where the staged path
            # documents "milliseconds to a second or two" — a gap nobody could
            # attribute, because this line did not carry the answer.
            #
            # runtime_refresh emits this child's stdout tail into watchdog.log,
            # so printing it here is what puts the swap mode in the durable log
            # the next investigation will read.
            swap = act.get("package_swap")
            swap_note = f" package_swap={swap}" if swap else ""
            print(
                f"  action: {act.get('tier')}/{act.get('action')} "
                f"ok={act.get('ok')}{swap_note} {act.get('reason', '')}".rstrip(),
            )
        if report.get("reason"):
            print(f"  {report['reason']}")
    if not report["ok"]:
        return 1
    # #769 (c): the STALE banner printed from `package_fresh is False` on a
    # branch completely independent of the exit code, so `ok=True` + STALE +
    # `exit 0` came out in one breath and a script checking the status could
    # not see staleness AT ALL. Staleness is a SPLIT ENFORCEMENT BRAIN — the
    # daemon runs source while hooks run the installed copy, so a gate fix can
    # be live in one and absent in the other. That must not read as success.
    #
    # A DISTINCT code (3), not 1: the runtime resolved and verified, so this is
    # not the same condition as "no owned runtime". `!= 0` catches both, which
    # is what an operator's `if ! aidocs runtime --check` wants, while a caller
    # that cares can tell them apart. Nothing in the tree branches on this
    # command's exit code today (the deploy uses aidocs_mcp.runtime_refresh,
    # which has its own rc contract) — checked before changing it.
    if report.get("package_fresh") is False:
        # FOLLOW-UP 2026-08-19, found in a real deploy log. `report` is produced
        # by rp.doctor(fix=...), and package_fresh is measured BEFORE the repair
        # — so a --fix run that SUCCESSFULLY un-staled the runtime still exited
        # 3, and runtime_refresh logged:
        #
        #     [runtime-refresh] `runtime --fix` failed (rc=3)
        #     [runtime-refresh] axis=source after: fresh=True ... (matching)
        #     [runtime-refresh] OK — the enforcement runtime is current.
        #
        # "failed" and "OK" about the same action, one line apart. The original
        # note here said "nothing in the tree branches on this exit code" —
        # runtime_refresh does, and it reports it as a failure. That was wrong
        # when written and it printed a false red on every deploy.
        #
        # So on a repair run, ANSWER FROM THE STATE AFTER THE REPAIR. --check is
        # unchanged: it measures and reports, and 3 still means STALE there.
        # A repair that did NOT clear staleness still exits 3, which is the
        # signal worth keeping.
        if fix or rebuild:
            try:
                from .runtime_provisioner import (
                    _reference_pkg_for_spec,
                    runtime_freshness,
                )

                # #889: ASK THE TREE THE REPAIR ACTUALLY INSTALLED FROM.
                #
                # The zero-argument call above measured against the LOCAL
                # CHECKOUT. That was right in 2026-08-19, when a repair could
                # only ever come from the checkout — and wrong from 2026-08-22,
                # when the deploy began handing over its frozen, stamped SHIP
                # STAGE (`--package <stage>/mcp`). A STAGE INSTALL BYTE-DIFFERS
                # FROM THE CHECKOUT FOREVER: 136 `.py` files newline-differ and
                # 27 of 28 Vite content-hashed assets moved because the stage
                # rebuilt them (measured in `_stamp_provenance_current`). So on
                # every deploy the re-measure asked a question whose only
                # possible answer was "stale", fell through, and re-printed the
                # very false red this branch was added to remove:
                #
                #     [runtime-refresh] `runtime --fix` failed (rc=3)
                #     [runtime-refresh] axis=source after: fresh=True (matching)
                #     [runtime-refresh] OK — the enforcement runtime is current.
                #
                # `_reference_pkg_for_spec(None)` yields None, so a repair with
                # no hand-over keeps measuring against the checkout exactly as
                # before — every existing caller is byte-identical.
                #
                # FAIL-CLOSED IS UNTOUCHED. This changes WHICH TREE is compared,
                # never whether a refusal is allowed: `is True` still means a
                # `False` (genuinely still stale) and a `None` (cannot tell)
                # both fall through to 3. Unknown is not a pass.
                if runtime_freshness(source_pkg=_reference_pkg_for_spec(package)).get(
                    "fresh"
                ) is True:
                    return 0
            except Exception:  # noqa: BLE001 — cannot re-measure: keep the old answer
                pass
        return 3
    return 0


def cmd_dashboard_set_config(args: list[str]) -> int:
    """Persist one editable project config value for the dashboard.

    Phase-1 auth wall (2026-05-20 +1): requires an authenticated
    operator context. The ``dashboard=True`` flag is no longer
    authority — the bearer token + RBAC permission are. Tokens
    come from ``--operator-token`` or ``AIDOCS_OPERATOR_TOKEN``.
    operator_only / security_sensitive settings additionally require
    ``admin.manage_config`` permission via RBAC.

    Per the plan §3, identity_resolver remains attribution-only:
    audit row carries user_id / role / source resolved from env
    fallback so a refused mutation still attributes who tried;
    AUTHORIZATION uses OperatorAuthService.authorize_config_mutation.
    """
    root = _resolve_login_root(args)  # noqa: E501 flag-value-as-root guard
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
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    scope = _option_value(args, "--scope", "project").strip()
    session_id_arg = _option_value(args, "--session", "").strip() or None
    # Optional operator reason (the dashboard T0 confirm dialog supplies it for
    # dashboard-only/security-sensitive changes); carried into the applied audit.
    reason_arg = _option_value(args, "--reason", "").strip()

    # ── Phase-1 auth gate ──
    from .operator_auth_service import OperatorAuthService

    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    operator_ctx = (
        auth.authenticate(
            token,
            root,
            source="dashboard",
        )
        if token
        else None
    )
    allowed, reason = auth.authorize_config_mutation(
        operator_ctx,
        setting_path,
        root,
        scope_type=scope,
        scope_id=(session_id_arg or str(root).replace("\\", "/")),
    )
    if not allowed:
        # Emit a refused-mutation audit row so the chain records
        # WHO tried and was rejected. identity_resolver fills in
        # the env-fallback attribution; the row carries
        # status='refused' and the reason so dashboards can show
        # unauthorized attempts without granting authority.
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                root,
                event_kind="config_set",
                source_kind="dashboard_admin",
                capability_name="dashboard-set-config",
                action_kind="config_write",
                target_entity=setting_path,
                status="refused",
                payload={
                    "key": setting_path,
                    "scope": scope,
                    "reason": reason,
                    "operator_authenticated": operator_ctx is not None,
                    "token_present": bool(token),
                    "source": "dashboard_admin",
                },
            )
        except Exception:
            pass
        message_map = {
            "unauthenticated": _unauthenticated_refusal_message(
                "dashboard-set-config",
                token_present=bool(token),
                # Only true when nothing was presented. Saying it to an
                # operator whose Dashboard token was just REJECTED would
                # point at the surface that produced the bad credential.
                absent_tail=(" The Dashboard UI attaches its own token automatically."),
            ),
            "missing_admin_manage_config": (
                f"dashboard-set-config refused: setting "
                f"'{setting_path}' requires admin.manage_config "
                f"permission. Sign in as ADMIN/SUPERADMIN."
            ),
            "unknown_setting": (f"dashboard-set-config refused: unknown setting '{setting_path}'."),
        }
        payload = {
            "ok": False,
            "reason": reason,
            "blocked_by": "operator_auth",
            "setting_path": setting_path,
            # See _unauthenticated_refusal_message: the Dashboard needs the
            # same present-vs-absent fact the message branches on, so it can
            # render the right remedy instead of a bare reason code (#747).
            "token_present": bool(token),
            "message": message_map.get(
                reason,
                f"dashboard-set-config refused: {reason}",
            ),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1
    # ── End auth gate ──

    # ── Operator Surface guard (admin surface): the authenticated dashboard
    # admin MAY write dashboard-only / security-sensitive keys (that is this
    # surface's purpose), but NEVER a service-managed, deprecated,
    # hidden-owned, or unknown key — those would half-enable a system or
    # write a dead/unrecognized key. guard_raw_write fails closed on
    # unknown and refuses the guardrail classes; routes to expert/profile.
    from . import operator_surface as _osurf

    _g = _osurf.guard_raw_write(setting_path, action="set")
    if not _g["allowed"]:
        payload = {
            "ok": False,
            "reason": _g["reason"],
            "blocked_by": "operator_surface",
            "setting_path": setting_path,
            "message": _g["message"],
            "redirect": _g["redirect"],
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    try:
        config_path = _update_project_config_value(
            root,
            setting_path,
            value,
            scope=scope,
            session_id=session_id_arg,
            dashboard=True,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "reason": "config_update_failed",
            "setting_path": setting_path,
            "message": str(exc),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # Transactional readback: the write only counts if the store now
    # reflects the coerced value at the exact (scope, scope_key). Guards
    # against a silent persistence failure where the UI would otherwise
    # claim "saved"/"enabled". A mismatch is a hard failure.
    expected_value = _coerce_setting_value(setting_path, value)
    readback_value = _readback_setting(root, setting_path, scope, session_id_arg)
    if readback_value != expected_value:
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                root,
                event_kind="config_set",
                source_kind="dashboard_admin",
                capability_name="dashboard-set-config",
                action_kind="config_write",
                target_entity=setting_path,
                status="failed",
                payload={"key": setting_path, "scope": scope, "reason": "readback_mismatch"},
            )
        except Exception:
            pass
        payload = {
            "ok": False,
            "reason": "readback_mismatch",
            "blocked_by": "readback_verification",
            "setting_path": setting_path,
            "expected": expected_value,
            "readback_value": readback_value,
            "message": (
                f"Write to {setting_path} did not verify: store shows "
                f"{readback_value!r}, expected {expected_value!r}."
            ),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload["message"])
        return 1

    _, runtime = _dashboard_runtime()
    updated_value = runtime.effective_config(root).get(setting_path.split(".")[0])

    # Control-plane audit (sealed 2026-05-20): explicit config_set
    # event with source_kind='dashboard_admin' so dashboard
    # mutations are distinguishable from CLI ('cli'/'cli_dev') and
    # MCP ('mcp_call') writes in the audit trail. The triple
    # (user_id, effective_role, source_kind) makes every admin
    # mutation attributable in the forensic chain.
    try:
        from .config_schema import SETTINGS_CATALOG as _SETTINGS
        from .execution_index_store import ExecutionIndexStore
        from .identity_resolver import current_effective_role, current_user

        meta = _SETTINGS.get(setting_path)
        sensitive = bool(meta and meta.get("security_sensitive"))
        # Resolve attribution. Phase-1 contract (2026-05-20 +1):
        # prefer the AUTHENTICATED operator context — that's the
        # operator the gate actually authorized. Fall back to
        # identity_resolver ONLY for attribution display (the
        # mutation IS authenticated; we're just naming the actor).
        if operator_ctx is not None:
            uid = operator_ctx.user_id
            role = operator_ctx.role
            ptype = "human"
        else:
            # Should not reach here — the auth gate above refused
            # unauthenticated callers. Defensive fallback.
            try:
                uid, _email, ptype = current_user(root)
            except Exception:
                uid, ptype = "", "human"
            try:
                # #576 D1: an unresolvable actor is UNKNOWN, never
                # super_admin — this value is stamped into the audit row.
                from .identity_resolver import UNKNOWN_ROLE

                role = current_effective_role(root, uid) if uid else UNKNOWN_ROLE
            except Exception:
                role = "unknown"
        # Blast-radius labeling (2026-05-25): make a coarser/broadening write
        # explicit in the forensic trail — a global write reaches every
        # project on the install, not just this one.
        _blast = _osurf.blast_radius(scope, setting_path)
        ExecutionIndexStore().record_event(
            root,
            event_kind="config_set",
            source_kind="dashboard_admin",
            capability_name="dashboard-set-config",
            action_kind="config_write",
            target_entity=setting_path,
            status="applied",
            user_id=uid or None,
            effective_role=role,
            principal_type=ptype,
            scope_type=scope,
            scope_id=(session_id_arg or str(root).replace("\\", "/")),
            payload={
                "key": setting_path,
                "scope": scope,
                "dashboard_only": bool(meta and meta.get("dashboard_only")),
                "security_sensitive": sensitive,
                "new": "[REDACTED]" if sensitive else value,
                "blast_radius": _blast["radius"],
                "broadening": _blast["broadening"],
                "reason": reason_arg,
                # Triple mirror so JSON-only consumers see the
                # (user_id, role, source) tuple without joining
                # against the row columns.
                "user_id": uid,
                "role": role,
                "source": "dashboard_admin",
                "principal_type": ptype,
            },
        )
    except Exception:
        # Forensic audit is best-effort; never block the dashboard
        # write on an audit-emit hiccup.
        pass

    _blast_result = _osurf.blast_radius(scope, setting_path)
    payload = {
        "ok": True,
        "verified": True,
        "readback_value": readback_value,
        "setting_path": setting_path,
        "config_path": str(config_path),
        "snapshot": runtime.dashboard_snapshot(root),
        "message": f"Updated {setting_path}",
        "value_root": updated_value,
        "blast_radius": _blast_result["radius"],
        "broadening": _blast_result["broadening"],
    }
    if _blast_result["broadening"]:
        payload["warning"] = _blast_result["warning"]
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        print(payload["message"])
    return 0


def cmd_dashboard_batch_config(args: list[str]) -> int:
    """Apply multiple config set/delete operations in one call. Reads JSON from --batch."""
    root = _resolve_login_root(args)  # noqa: E501 flag-value-as-root guard
    as_json = _wants_json(args)
    batch_json = _option_value(args, "--batch", "").strip()

    if not batch_json:
        # Try reading from stdin
        import sys

        if not sys.stdin.isatty():
            batch_json = sys.stdin.read().strip()

    if not batch_json:
        payload = {
            "ok": False,
            "reason": "missing_batch",
            "message": "--batch JSON or stdin required",
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    try:
        operations = json.loads(batch_json)
    except Exception as exc:
        payload = {"ok": False, "reason": "invalid_json", "message": str(exc)}
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    if not isinstance(operations, list):
        operations = [operations]

    from .config_store import ConfigStore
    from .operator_auth_service import OperatorAuthService

    # ── Phase-1 auth gate (batch) ──
    # Same authority model as cmd_dashboard_set_config: a single
    # authenticated operator context covers the whole batch. Each
    # operation is still authorized per-setting (a batch can mix
    # safe + operator_only keys; the operator must hold
    # admin.manage_config for the operator_only ones).
    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    operator_ctx = (
        auth.authenticate(
            token,
            root,
            source="dashboard",
        )
        if token
        else None
    )

    store = ConfigStore()
    results: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    for op in operations:
        action = str(op.get("action", "set")).strip()
        setting_path = str(op.get("setting_path", "")).strip()
        scope = str(op.get("scope", "project")).strip()
        session_id = op.get("session_id") or None
        scope_key = str(session_id or "") if scope == "session" else ""

        if not setting_path:
            errors.append({"setting_path": "", "action": action, "error": "missing setting_path"})
            continue

        # Per-operation authorization. set + delete both mutate
        # config posture, so both require the operator gate.
        allowed, reason = auth.authorize_config_mutation(
            operator_ctx,
            setting_path,
            root,
            scope_type=scope,
            scope_id=(str(session_id) if session_id else str(root).replace("\\", "/")),
        )
        if not allowed:
            errors.append(
                {
                    "setting_path": setting_path,
                    "action": action,
                    "error": f"operator_auth: {reason}",
                    "blocked_by": "operator_auth",
                },
            )
            # Audit the refused attempt.
            try:
                from .execution_index_store import ExecutionIndexStore

                ExecutionIndexStore().record_event(
                    root,
                    event_kind="config_set",
                    source_kind="dashboard_admin",
                    capability_name="dashboard-batch-config",
                    action_kind="config_write",
                    target_entity=setting_path,
                    status="refused",
                    payload={
                        "key": setting_path,
                        "scope": scope,
                        "action": action,
                        "reason": reason,
                        "operator_authenticated": operator_ctx is not None,
                        "token_present": bool(token),
                        "source": "dashboard_admin",
                    },
                )
            except Exception:
                pass
            continue

        # Operator Surface guard (per op, admin surface): refuse the
        # guardrail classes (service-managed/deprecated/hidden-owned) and
        # unknown keys on both set and delete; admin dashboard-only writes
        # are allowed (this is the authenticated admin surface).
        from . import operator_surface as _osurf

        _g = _osurf.guard_raw_write(setting_path, action=action)
        if not _g["allowed"]:
            errors.append(
                {
                    "setting_path": setting_path,
                    "action": action,
                    "error": f"{_g['reason']}: {_g['message']}",
                    "blocked_by": "operator_surface",
                },
            )
            continue

        try:
            if action == "delete":
                deleted = store.delete(root, setting_path, scope=scope, scope_key=scope_key)
                results.append(
                    {
                        "setting_path": setting_path,
                        "action": "delete",
                        "deleted": deleted,
                    },
                )
            else:
                value = op.get("value")
                _update_project_config_value(
                    root,
                    setting_path,
                    value,
                    scope=scope,
                    session_id=session_id,
                    dashboard=True,
                )
                from . import operator_surface as _osurf_b

                _blast_b = _osurf_b.blast_radius(scope, setting_path)
                _row = {
                    "setting_path": setting_path,
                    "action": "set",
                    "value": value,
                    "blast_radius": _blast_b["radius"],
                    "broadening": _blast_b["broadening"],
                }
                if _blast_b["broadening"]:
                    _row["warning"] = _blast_b["warning"]
                results.append(_row)
                # Success audit row with the authenticated operator's
                # (user_id, role, source) triple + blast-radius labeling.
                try:
                    from .config_schema import SETTINGS_CATALOG as _SC
                    from .execution_index_store import ExecutionIndexStore

                    _m = _SC.get(setting_path)
                    _sens = bool(_m and _m.get("security_sensitive"))
                    ExecutionIndexStore().record_event(
                        root,
                        event_kind="config_set",
                        source_kind="dashboard_admin",
                        capability_name="dashboard-batch-config",
                        action_kind="config_write",
                        target_entity=setting_path,
                        status="applied",
                        user_id=operator_ctx.user_id,
                        effective_role=operator_ctx.role,
                        principal_type="human",
                        scope_type=scope,
                        scope_id=(str(session_id) if session_id else str(root).replace("\\", "/")),
                        payload={
                            "key": setting_path,
                            "scope": scope,
                            "security_sensitive": _sens,
                            "new": "[REDACTED]" if _sens else value,
                            "blast_radius": _blast_b["radius"],
                            "broadening": _blast_b["broadening"],
                            "user_id": operator_ctx.user_id,
                            "role": operator_ctx.role,
                            "source": "dashboard_admin",
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            errors.append({"setting_path": setting_path, "action": action, "error": str(exc)})

    _, runtime = _dashboard_runtime()
    payload = {
        "ok": len(errors) == 0,
        "results": results,
        "errors": errors,
        "snapshot": runtime.dashboard_snapshot(root),
        "message": f"Applied {len(results)} operations"
        + (f", {len(errors)} errors" if errors else ""),
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        print(payload["message"])
    return 0 if not errors else 1




def _require_authenticated_cli_principal(
    args: list[str],
    root: Path,
    command: str,
    as_json: bool,
) -> tuple[object, int]:
    """Require login for a CLI mutation without requiring admin RBAC.

    An explicitly supplied token is validated as the caller's credential and
    never falls back when invalid. Without an explicit token, the shared
    project-authority seam may resolve an environment token, enabled approved
    binding, or current machine login. Machine presence and install flavor are
    never authority.
    """
    from .operator_auth_service import OperatorAuthService

    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    principal: object = None
    user_id = ""
    if token:
        ctx = auth.authenticate(token, root, source="cli")
        if ctx is not None and ctx.user_id:
            principal = ctx
            user_id = ctx.user_id
    else:
        try:
            from .project_authority import _authenticated_uid

            user_id = str(_authenticated_uid(root) or "").strip()
            principal = user_id or None
        except Exception:
            user_id = ""
            principal = None
    if user_id:
        return principal, -1

    payload = {
        "ok": False,
        "reason": "unauthenticated",
        "blocked_by": "login_required",
        "command": command,
        "message": (
            f"{command} refused: login is required. Sign in with "
            "`aidocs operator-login` or use the Dashboard/Codenexus login "
            "flow, then retry."
        ),
    }
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="control_plane_mutation",
            source_kind="cli",
            capability_name=command,
            action_kind="login_gate",
            target_entity=command,
            status="refused",
            payload={
                "command": command,
                "reason": "unauthenticated",
                "token_present": bool(token),
            },
        )
    except Exception:
        pass
    if as_json:
        print(_safe_json_dumps(payload, indent=2))
    else:
        print(payload["message"])
    return None, 1


def _unauthenticated_refusal_message(
    command: str,
    *,
    token_present: bool,
    absent_tail: str = "",
) -> str:
    """The ``unauthenticated`` refusal text, branched on whether a credential
    was actually presented.

    #442 fix 2 / #747 defect 1. ``authorize_admin_command`` and
    ``authorize_config_mutation`` both answer ``"unauthenticated"`` for two
    different states: nothing resolved at all, and something resolved that
    ``authenticate()`` then REJECTED (expired, revoked, or minted for a
    different install). Printing the no-credential text for both states the
    WRONG CAUSE — and the remedy it names, go and look at your token sources,
    cannot work in the second state, because those sources are exactly what
    produced the credential that was just refused (law 311bf3e6).

    ``token_present`` is the same value both call sites already record in
    their refused-audit payload, so this only reads a fact the gate had all
    along. Nothing here changes WHO gets through: both branches refuse.

    ``absent_tail`` carries guidance that is only true when nothing was
    presented (e.g. "the Dashboard attaches its own token"), so it is never
    appended to the rejected branch.
    """
    if token_present:
        return (
            f"{command} refused: an operator credential WAS presented and was "
            f"REJECTED — it is expired, revoked, or was issued for a different "
            f"install. Nothing is missing, so re-checking where the credential "
            f"comes from will not help. Replace it: sign in again with 'aidocs "
            f"operator-login' (as ADMIN/SUPERADMIN)."
        )
    return (
        f"{command} refused: no operator token found in env "
        f"AIDOCS_OPERATOR_TOKEN, the --operator-token flag, or the machine "
        f"token cache. Sign in once with 'aidocs operator-login' (as "
        f"ADMIN/SUPERADMIN) — the token is cached for this machine until it "
        f"expires.{absent_tail}"
    )


def _require_operator_for_admin_command(
    args: list[str],
    root: Path,
    command: str,
    as_json: bool,
) -> tuple[object, int]:
    """Shared auth gate for admin-only dashboard CLI commands that are
    NOT a single config setting (skill toggle/delete, managed-mode,
    setup, RBAC). Returns ``(operator_ctx, exit_code)``:

      - On allow: (OperatorContext, -1) — caller proceeds.
      - On refusal: (None, 1) after printing the refusal payload and
        emitting a status='refused' audit row.

    Mirrors the dashboard-set-config gate so every admin-only surface
    enforces the same operator_token + admin.manage_config wall.

    #404 (2026-07-16): EVERY install flavor requires an explicit operator
    token here — there is no dev-flavor local super-admin auto-mint. Sign
    in via the Dashboard and pass --operator-token / AIDOCS_OPERATOR_TOKEN.
    """
    from .operator_auth_service import OperatorAuthService

    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    ctx = auth.authenticate(token, root, source="dashboard") if token else None
    allowed, reason = auth.authorize_admin_command(
        ctx,
        root,
        scope_id=str(root).replace("\\", "/"),
    )
    if allowed:
        return ctx, -1
    # Refusal: audit + message.
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="control_plane_mutation",
            source_kind="dashboard_admin",
            capability_name=command,
            action_kind="admin_command",
            target_entity=command,
            status="refused",
            payload={
                "command": command,
                "reason": reason,
                "operator_authenticated": ctx is not None,
                "token_present": bool(token),
                "source": "dashboard_admin",
            },
        )
    except Exception:
        pass
    msg = {
        "unauthenticated": _unauthenticated_refusal_message(
            command,
            token_present=bool(token),
        ),
        "missing_admin_manage_config": (
            f"{command} refused: requires admin.manage_config "
            f"permission. Sign in as ADMIN/SUPERADMIN."
        ),
    }.get(reason, f"{command} refused: {reason}")
    payload = {
        "ok": False,
        "reason": reason,
        "blocked_by": "operator_auth",
        "command": command,
        # The datum that separates "nothing was presented" from "what was
        # presented got refused". It is in the audit row above; surfacing it
        # here lets the Dashboard branch on the same fact the message does
        # instead of re-deriving it (#747 defect 1).
        "token_present": bool(token),
        "message": msg,
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2))
    else:
        print(payload["message"])
    return None, 1


def _audit_admin_command(
    root: Path,
    command: str,
    ctx: object,
    *,
    status: str,
    **extra: object,
) -> None:
    """Emit an audit row for an admin-only command with the authenticated
    operator's (user_id, role, source) triple and a TRUTHFUL ``status``
    (applied / no_op / failed). Use this when the outcome is not always
    "applied" — e.g. an idempotent migration whose re-run is a no-op must
    not be audited as if it changed authority.
    """
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="control_plane_mutation",
            source_kind="dashboard_admin",
            capability_name=command,
            action_kind="admin_command",
            target_entity=command,
            status=status,
            user_id=getattr(ctx, "user_id", None),
            effective_role=getattr(ctx, "role", None),
            principal_type="human",
            scope_id=str(root).replace("\\", "/"),
            payload={
                "command": command,
                "user_id": getattr(ctx, "user_id", ""),
                "role": getattr(ctx, "role", ""),
                "source": "dashboard_admin",
                "status": status,
                **extra,
            },
        )
    except Exception:
        pass


def _audit_admin_command_applied(root: Path, command: str, ctx: object, **extra: object) -> None:
    """Emit the success ("applied") audit row for an admin-only command."""
    _audit_admin_command(root, command, ctx, status="applied", **extra)


def cmd_dashboard_capability_profiles(args: list[str]) -> int:
    """Return capability-profile groupings + the live Governed Bash
    posture (read-only). The catalog stays the source of values; this is
    presentation grouping only.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    from .capability_profiles import list_profiles
    from .governed_bash_service import posture_card

    payload = {
        "ok": True,
        "profiles": list_profiles(),
        "governed_bash": posture_card(root),
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        print(f"{len(payload['profiles'])} capability profiles")
    return 0


def cmd_operator_surface(args: list[str]) -> int:
    """Operator Surface Catalog — doctrine-level control profiles over the
    raw config ledger. Subcommands via flags:
      (default)         list profiles (id, title, danger, managed_by).
      --status <id>     resolve one profile's current state.
      --inspect <key>   full provenance + scope cascade + owning profile.
      --rows            split the catalog into normal + advanced_raw rows.
      --apply <id>      apply a profile (operator-auth gated). Dangerous
                        profiles also need --confirm and --reason. Values
                        via --values <json>; Governed Bash via --action
                        enable|disable plus --provider-path/--hash-pin/
                        --require-os-signature.
      --expert-set <k>  Advanced Raw expert edit of one key (operator-auth
                        gated); --value <json>, --confirm for T0 keys.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    from . import operator_surface as osurf

    inspect_key = _option_value(args, "--inspect", "").strip()
    status_id = _option_value(args, "--status", "").strip()
    apply_id = _option_value(args, "--apply", "").strip()
    expert_key = _option_value(args, "--expert-set", "").strip()
    want_rows = "--rows" in args
    session_id = _option_value(args, "--session-id", "").strip() or None
    scope = _option_value(args, "--scope", "global").strip() or "global"

    if apply_id or expert_key:
        ctx, code = _require_operator_for_admin_command(args, root, "operator-surface", as_json)
        if ctx is None:
            return code
        confirm = _option_value(args, "--confirm", "")
        if apply_id:
            values = None
            vraw = _option_value(args, "--values", "").strip()
            if vraw:
                try:
                    values = json.loads(vraw)
                except Exception as exc:
                    res = {"ok": False, "error": "invalid_values_json", "message": str(exc)}
                    print(_safe_json_dumps(res, indent=2, default=str))
                    return 1
            res = osurf.apply_profile(
                root,
                apply_id,
                values=values,
                operator_authenticated=True,
                confirm_token=confirm,
                reason=_option_value(args, "--reason", ""),
                scope=scope,
                action=_option_value(args, "--action", "enable"),
                provider_path=(_option_value(args, "--provider-path", "").strip() or None),
                hash_pin=(_option_value(args, "--hash-pin", "").strip() or None),
                require_os_signature="--require-os-signature" in args,
            )
        else:
            res = osurf.expert_set(
                root,
                expert_key,
                _parse_json_argument(args, "--value"),
                operator_authenticated=True,
                confirm_token=confirm,
                scope=scope,
            )
        print(_safe_json_dumps({"ok": res.get("ok", False), **res}, indent=2, default=str))
        return 0 if res.get("ok") else 1

    if want_rows:
        payload = {"ok": True, **osurf.settings_rows(root, session_id=session_id)}
    elif inspect_key:
        payload = {"ok": True, **osurf.inspect_key(root, inspect_key, session_id=session_id)}
    elif status_id:
        payload = osurf.resolve_status(root, status_id, session_id=session_id)
    else:
        payload = {
            "ok": True,
            "profiles": [
                {
                    "id": p.id,
                    "title": p.title,
                    "doctrine_area": p.doctrine_area,
                    "danger": p.danger,
                    "managed_by": p.managed_by,
                    "advanced_only": p.advanced_only,
                    "keys": list(p.keys),
                    "hidden_owned_keys": list(p.hidden_owned_keys),
                }
                for p in osurf.list_profiles()
            ],
        }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    elif inspect_key:
        print(
            f"{inspect_key}: effective={payload.get('effective_value')!r} "
            f"(from {payload.get('effective_source')}) "
            f"owner={payload.get('owning_profile')}",
        )
    elif status_id:
        print(f"{status_id}: {payload.get('status')}")
    else:
        for p in payload["profiles"]:
            print(f"  [{p['danger']:>8}] {p['id']:<20} {p['title']}")
    return 0


def cmd_governed_bash_status(args: list[str]) -> int:
    """Report the re-derived Governed Bash security posture (read-only).
    `verified` is the single bit the dashboard trusts to show ENABLED.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    from .governed_bash_service import posture_card
    from .governed_shell_attest import live_execution_posture

    card = posture_card(root)
    # SINGLE AUTHORITY (Empire re-seal 2026-05-30): live_execution_posture is
    # the ONLY ENABLED bit. The legacy posture_card stays as READ-ONLY
    # diagnostics — it never independently drives ENABLED.
    live = live_execution_posture(root)
    diagnostics = {
        "route": live.get("route"),
        "ok": live.get("ok"),
        "reason": live.get("reason"),
        "repair": live.get("repair"),
        "checks": live.get("checks", {}),
    }
    verified = bool(live.get("ok"))
    # `verified` is overridden to the live bit; card fields ride along as
    # read-only diagnostics (checks/flags/provider_path the panel renders).
    payload = {
        "ok": True,
        **card,
        "verified": verified,
        "legacy_verified": card.get("verified"),
        "live_execution_posture": diagnostics,
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        cap = card.get("host_capability", {})
        print(f"Governed Bash: {('ENABLED' if verified else 'DISABLED')} (live_ok={verified})")
        print(f"  provider path : {card.get('provider_path') or '(none)'}")
        print(f"  trusted roots : {', '.join(card.get('trusted_roots') or []) or '(none)'}")
        print(
            f"  host capability: native_safe="
            f"{cap.get('native_safe')} "
            f"output_replacement={cap.get('output_replacement')}",
        )
        print(f"  selected route : {card.get('selected_route')}")
        if card.get("repair_reason"):
            print(f"  repair        : {card['repair_reason']}")
        print(f"  live posture   : route={diagnostics['route']} ok={diagnostics['ok']}")
        if diagnostics.get("reason"):
            print(f"  live reason    : {diagnostics['reason']}")
    return 0


def cmd_governed_bash_enable(args: list[str]) -> int:
    """Atomically enable Governed Bash (operator-auth gated). Validates the
    provider, writes every setting, readback-verifies, returns posture.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    ctx, code = _require_operator_for_admin_command(
        args,
        root,
        "governed-bash-enable",
        as_json,
    )
    if ctx is None:
        return code
    scope = _option_value(args, "--scope", "global").strip() or "global"
    card_json = _option_value(args, "--approval-card-json", "").strip() or None

    # THE one control (Empire re-seal 2026-05-30): with NO approval card, this
    # is "Allow shell tools validated and supported by AIDOCS" — auto-discover
    # the canonical Git Bash, generate the SHA-256 pin, auto-enroll. No
    # path/hash/signature ceremony. An unknown / PATH-only provider returns a
    # SIGNED, single-use candidate approval card; the operator then approves
    # one EXACT card via --approval-card-json. The legacy --provider-path
    # side door is REMOVED: there is no bare-path approval.
    from .governed_shell_attest import approve_exact_path, enable_supported

    if card_json is None:
        res = enable_supported(
            root,
            operator_authenticated=True,
            scope=scope,
        )
    else:
        import json as _json

        try:
            card = _json.loads(card_json)
        except Exception:
            card = None
        # Approval is bound to a signed, single-use control-plane card — a
        # malformed / forged / replayed / swapped card is refused; the pin is
        # GENERATED from the approved file (never operator-typed).
        res = approve_exact_path(
            root,
            "",
            operator_authenticated=True,
            scope=scope,
            card=card,
        )
    if res.get("ok"):
        _audit_admin_command_applied(
            root,
            "governed-bash-enable",
            ctx,
            scope=scope,
        )
    if as_json:
        print(_safe_json_dumps(res, indent=2, default=str))
    else:
        print(res.get("message", f"governed-bash enable ok={res.get('ok')}"))
    # Auth already enforced by the gate above; a validation/readback
    # failure is a structured result (ok:false + checks), not a process
    # error — exit 0 so the dashboard receives the posture to render.
    return 0


def cmd_governed_bash_disable(args: list[str]) -> int:
    """Disable Governed Bash — clear enforcement flags (operator gated)."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    ctx, code = _require_operator_for_admin_command(
        args,
        root,
        "governed-bash-disable",
        as_json,
    )
    if ctx is None:
        return code
    scope = _option_value(args, "--scope", "global").strip() or "global"
    from .governed_bash_service import disable

    res = disable(root, operator_authenticated=True, scope=scope)
    if res.get("ok"):
        _audit_admin_command_applied(
            root,
            "governed-bash-disable",
            ctx,
            scope=scope,
        )
    if as_json:
        print(_safe_json_dumps(res, indent=2, default=str))
    else:
        print(f"governed-bash disable ok={res.get('ok')}")
    return 0  # auth enforced by the gate; result carried in `ok`


def cmd_dashboard_delete_config(args: list[str]) -> int:
    """Delete a scope-specific config override (revert to parent scope)."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    # Auth wall: delete mutates config posture → admin-only.
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-delete-config",
        as_json,
    )
    if _rc != -1:
        return _rc
    setting_path = _option_value(args, "--setting", "").strip()
    scope = _option_value(args, "--scope", "project").strip()
    session_id_arg = _option_value(args, "--session", "").strip() or None

    if not setting_path:
        payload = {
            "ok": False,
            "reason": "missing_setting",
            "message": "--setting is required",
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # Operator Surface guard: a service-managed key must not be deleted
    # from the raw path (deleting one Governed Bash flag = half-disable);
    # use the profile's disable action instead.
    from . import operator_surface as _osurf

    _g = _osurf.guard_raw_write(setting_path, action="delete")
    if not _g["allowed"]:
        payload = {
            "ok": False,
            "reason": _g["reason"],
            "blocked_by": "operator_surface",
            "setting_path": setting_path,
            "message": _g["message"],
            "redirect": _g["redirect"],
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    try:
        from .config_store import ConfigStore

        store = ConfigStore()
        scope_key = session_id_arg or "" if scope == "session" else ""
        deleted = store.delete(root, setting_path, scope=scope, scope_key=scope_key)
    except Exception as exc:
        payload = {
            "ok": False,
            "reason": "config_delete_failed",
            "setting_path": setting_path,
            "message": str(exc),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    _audit_admin_command_applied(
        root,
        "dashboard-delete-config",
        _ctx,
        setting_path=setting_path,
        scope=scope,
        deleted=bool(deleted),
    )
    _, runtime = _dashboard_runtime()
    payload = {
        "ok": True,
        "setting_path": setting_path,
        "deleted": deleted,
        "snapshot": runtime.dashboard_snapshot(root),
        "message": f"Deleted {setting_path} override at scope={scope}"
        if deleted
        else f"No override found for {setting_path} at scope={scope}",
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
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
        print(_safe_json_dumps(payload, indent=2))
        return 0

    if show_semantics:
        families = (
            payload.get("outline_families")
            if isinstance(payload.get("outline_families"), list)
            else []
        )
        tags = (
            payload.get("semantic_tags") if isinstance(payload.get("semantic_tags"), list) else []
        )
        print(f"Built-in descriptors: {payload.get('built_in_descriptor_count', 0)}")
        print(f"  with extractor family: {payload.get('built_in_with_extractor_family', 0)}")
        print(f"  with outline family:   {payload.get('built_in_with_outline_family', 0)}")
        print(f"  with raw outlines:     {payload.get('built_in_with_outline_patterns', 0)}")
        print(f"  with role semantics:   {payload.get('built_in_with_role_semantics', 0)}")
        print(f"  with module hints:     {payload.get('built_in_with_module_hints', 0)}")
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
            payload.get("descriptor") if isinstance(payload.get("descriptor"), dict) else {}
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
        print(f"Descriptor validation: {'ok' if payload.get('valid') else 'issues found'}")
        print(f"  descriptors: {payload.get('count', 0)}")
        issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
        for issue in issues[:20]:
            if isinstance(issue, dict):
                print(f"  - {issue.get('path')}: {issue.get('issue')}")
        return 0

    print(f"Active descriptor registry: {payload.get('count', 0)} descriptors")
    descriptors = payload.get("descriptors") if isinstance(payload.get("descriptors"), list) else []
    for item in descriptors[:20]:
        if isinstance(item, dict):
            extensions = item.get("extensions") if isinstance(item.get("extensions"), list) else []
            sample_ext = ", ".join(extensions[:3]) if extensions else "-"
            style = (
                "tags"
                if item.get("uses_semantic_tags")
                else "raw"
                if item.get("uses_raw_outline_patterns") or item.get("uses_raw_role_patterns")
                else "basic"
            )
            print(
                f"  - {item.get('name')} ({item.get('source')}, tier={item.get('tier')}, style={style}, ext={sample_ext})",
            )
    return 0


def cmd_snapshots(args: list[str]) -> int:
    """Inspect local copied index snapshots."""
    root = _find_aidocs_root() or Path.cwd()
    as_json = _wants_json(args)
    manifest = root / ".MEMORY" / "related-projects" / "index-snapshots" / "manifest.json"
    if not manifest.is_file():
        payload = {"ok": False, "message": f"Snapshot manifest not found: {manifest}"}
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if as_json:
        print(_safe_json_dumps(payload, indent=2))
        return 0
    snapshots = payload.get("snapshots") if isinstance(payload.get("snapshots"), list) else []
    print(f"Index snapshots: {len(snapshots)}")
    for item in snapshots:
        if isinstance(item, dict):
            print(
                f"  - {item.get('name')}: code={item.get('code_files')} schema={item.get('schema_entities')} workflow={item.get('workflow_rule_count')}",
            )
    return 0


def cmd_version(args: list[str]) -> int:
    """Show version."""
    if _wants_json(args):
        print(json.dumps({"ok": True, "package": "aidocs-mcp", "version": __version__}, indent=2))
        return 0
    print(f"aidocs-mcp {__version__}")
    return 0


def cmd_project_registry(args: list[str]) -> int:
    """Inspect the global MCP-touched project registry."""
    service = ProjectRegistryService()
    payload = {"ok": True, "projects": service.list_projects()}
    if _wants_json(args):
        print(_safe_json_dumps(payload, indent=2))
        return 0
    projects = payload["projects"] if isinstance(payload["projects"], list) else []
    print(f"Registered MCP projects: {len(projects)}")
    for item in projects:
        if isinstance(item, dict):
            print(
                f"  - {item.get('title') or item.get('project_root')}: {item.get('project_root')}",
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
        print(_safe_json_dumps({"ok": False, "error": "Missing --session <id>"}))
        return 1

    from .mcp_server import _resolve_templates_root
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    result = hub.managed_mode.set_mode(root, session_id=session_id, source="dashboard")
    print(_safe_json_dumps({"ok": True, "managed_mode": result}))
    return 0


def cmd_managed_mode_clear(args: list[str]) -> int:
    """Disable managed mode for a project.

    #438: clear_mode alone deletes ONLY the deprecated singleton row —
    per-conductor ROW EXISTENCE is the real binding. The disable path
    therefore also severs the calling conductor's per-conductor row
    (``--host-session <id>``; falls back to the request-scoped identity
    when the CLI runs inside an identified host context).
    """
    root = _resolve_root(args)
    host_session_id = ""
    for i, arg in enumerate(args):
        if arg == "--host-session" and i + 1 < len(args):
            host_session_id = args[i + 1]
    if not host_session_id:
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        host_session_id = (current_calling_host_session_id() or "").strip()

    from .mcp_server import _resolve_templates_root
    from .service_hub import AidocsServiceHub

    hub = AidocsServiceHub(templates_root=_resolve_templates_root())
    unbind = hub.managed_mode.unbind_current_conductor(root, host_session_id)
    result = hub.managed_mode.clear_mode(root)
    print(
        _safe_json_dumps(
            {"ok": True, "managed_mode": result, "unbind": unbind},
        ),
    )
    return 0


def cmd_doctor(args: list[str]) -> int:
    """Diagnose AIDOCS installation — check every prerequisite."""
    import shutil
    import subprocess

    fix = "--fix" in args
    issues: list[str] = []

    def check(label: str, ok: bool, fix_hint: str = "") -> None:
        if ok:
            print(f"  \033[32m[PASS]\033[0m {label}")
        else:
            print(f"  \033[31m[FAIL]\033[0m {label}")
            if fix_hint:
                print(f"         {fix_hint}")
            issues.append(label)

    def warn(label: str, hint: str = "") -> None:
        print(f"  \033[33m[WARN]\033[0m {label}")
        if hint:
            print(f"         {hint}")

    print("\nAIDOCS Doctor\n")

    # 1. Python version
    ver = sys.version_info
    check(
        f"Python {ver.major}.{ver.minor}.{ver.micro}",
        ver >= (3, 11),
        "Python 3.11+ required. Install from python.org or your package manager.",
    )

    # 2. aidocs-mcp package
    pkg_ok = False
    try:
        import aidocs_mcp

        pkg_ok = True
        check(f"aidocs-mcp installed ({getattr(aidocs_mcp, '__version__', '?')})", True)
    except ImportError:
        check("aidocs-mcp installed", False, "Run: pip install aidocs-mcp")

    # 3. tree-sitter
    try:
        import tree_sitter  # noqa: F401

        check("tree-sitter available", True)
    except ImportError:
        warn(
            "tree-sitter not installed",
            "Optional but recommended: pip install tree-sitter",
        )

    # 4. Detect agent hosts
    print("\n  Agent Hosts:")
    claude_cli = shutil.which("claude")
    has_vscode_claude = False
    for ext_dir in [
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".vscode-insiders" / "extensions",
    ]:
        if ext_dir.is_dir():
            for d in ext_dir.iterdir():
                if d.is_dir() and "claude" in d.name.lower():
                    has_vscode_claude = True
                    break
    if claude_cli:
        check("Claude Code CLI", True)
    if has_vscode_claude:
        check("VS Code Claude extension", True)
    if not claude_cli and not has_vscode_claude:
        check(
            "Claude Code (CLI or VS Code)",
            False,
            "Install from: https://claude.ai/download",
        )

    # 5. MCP config
    mcp_locations = [
        Path.home() / ".claude" / "mcp.json",
        Path.cwd() / ".mcp.json",
    ]
    mcp_found = False
    aidocs_configured = False
    for mcp_path in mcp_locations:
        if mcp_path.is_file():
            mcp_found = True
            try:
                import json

                data = json.loads(mcp_path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers", {})
                if "aidocs" in servers:
                    aidocs_configured = True
                    check(f"MCP config: aidocs entry in {mcp_path.name}", True)
            except Exception:
                pass
    if not mcp_found:
        check("MCP config file", False, "Run: aidocs setup")
    elif not aidocs_configured:
        check("MCP config: aidocs entry", False, "Run: aidocs setup")

    # 6. MCP server starts
    if pkg_ok:
        try:
            # #345: routed through audited_run (ledger row per spawn) +
            # CREATE_NO_WINDOW (output is captured; nothing to watch live).
            from .shell_egress_service import audited_run

            result = audited_run(
                [
                    sys.executable,
                    "-c",
                    "from aidocs_mcp.mcp_server import create_server; print('ok')",
                ],
                fingerprint=("cli.py", "cmd_doctor", "subprocess.run"),
                reason="doctor-import-probe",
                run=lambda *a, **kw: subprocess.run(*a, **kw),
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            check(
                "MCP server importable",
                result.returncode == 0 and "ok" in result.stdout,
                result.stderr.strip()[:200] if result.returncode != 0 else "",
            )
        except Exception as e:
            check("MCP server importable", False, str(e)[:200])

    # 7. Hooks
    print("\n  Hooks:")
    hooks_path = Path.home() / ".claude" / "settings.json"
    if hooks_path.is_file():
        try:
            import json

            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {})
            for event in [
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostCompact",
            ]:
                groups = hooks.get(event, [])
                has_aidocs = False
                for g in groups if isinstance(groups, list) else [groups]:
                    for h in g.get("hooks", []) if isinstance(g, dict) else []:
                        cmd = h.get("command", "") if isinstance(h, dict) else ""
                        if "aidocs_mcp" in cmd or "claude-hook" in cmd:
                            has_aidocs = True
                check(f"Hook: {event}", has_aidocs, "Run: aidocs setup")
        except Exception:
            warn("Could not parse hooks config")
    else:
        warn(f"No hooks config at {hooks_path}", "Run: aidocs setup")

    # 8. Project check (cwd)
    print("\n  Project:")
    cwd = Path.cwd()
    memory_dir = cwd / ".MEMORY"
    if memory_dir.is_dir():
        check(f"Project initialized: {cwd.name}", True)
    else:
        warn(f"No AIDOCS project in {cwd}", "Run: aidocs init")

    # 9. Registry-wide project health (disk <-> registry cross-check).
    # Catches half-inits (aborted project_init), adopted-uncommissioned
    # registry drift, legacy projects awaiting the stamp, and stale rows.
    print("\n  Projects (registry-wide):")
    try:
        from .project_doctor import diagnose, heal

        health = diagnose(extra_roots=[str(cwd)] if memory_dir.is_dir() else None)
        for h in health:
            if h.status == "healthy":
                check(f"{h.root}", True)
            elif h.status == "legacy":
                warn(f"{h.root} — legacy (no stamp yet)", "Heals at next bootstrap, or run: aidocs doctor --fix")
            else:
                note = "; ".join(h.notes)
                check(f"{h.root} — {h.status}", False, note)
        if fix:
            report = heal(health)
            if report["stamped_forward"]:
                print(f"         stamped forward: {len(report['stamped_forward'])} project(s)")
            if report["pruned_stale_rows"]:
                print(f"         pruned stale registry rows: {report['pruned_stale_rows']}")
            for root in report["needs_operator"]:
                warn(
                    f"half-init needs a decision: {root}",
                    "Adopt it: run /aidocs (or `aidocs init`) there — or delete its .MEMORY debris.",
                )
            for failure in report["failures"]:
                warn(f"heal failure: {failure}")
    except Exception as exc:
        warn(f"project health scan unavailable: {exc}")

    # Summary
    print()
    if issues:
        print(f"\033[31m{len(issues)} issue(s) found.\033[0m")
        if fix:
            # `--fix` re-runs setup because the setup wizard is idempotent:
            # rewriting hooks, MCP config, and .gitignore closes the gap
            # between a broken install and a working one without forcing the
            # user to know which subcommand restores which piece.
            print("Running `aidocs setup --auto` to repair...")
            return cmd_setup(["--auto"])
        print("Run `aidocs doctor --fix` to auto-repair, or rerun `aidocs setup`.")
        return 1
    print("\033[32mAll checks passed.\033[0m")
    return 0


def _authorize_daemon_lifecycle(action: str, args: list[str]):
    """Resolve a principal for a daemon lifecycle act, or refuse (#623).

    Returns a minted ``LifecycleRequest`` on success, or None after printing a
    refusal. Every outcome — authorised or refused — lands in the audit ledger,
    so "who stopped governance" is always answerable. Before this existed,
    ``cmd_service`` resolved no principal at all and the stop was a bare file
    write, which made the daemon the least-governed process in the system.

    NO USER IS EXEMPT, INCLUDING THE OPERATOR. There is deliberately no
    operator bypass branch here: his stop authenticates through the same
    ``OperatorAuthService`` path and is audited identically. The capability is
    untouched — the operator and the deploy hot-swap keep stopping and starting
    the daemon exactly as before, via the machine token cache, the
    ``AIDOCS_OPERATOR_TOKEN`` env var, or ``--operator-token``. What changed is
    that the act now has a name attached.
    """
    from .daemon_lifecycle_authority import (
        EVENT_LIFECYCLE_REFUSED,
        EVENT_LIFECYCLE_REQUESTED,
        audit,
        describe,
        mint,
    )
    from .operator_auth_service import OperatorAuthService
    from .permission_catalog import PERM_ADMIN_DAEMON_LIFECYCLE

    root = _resolve_root_flag_style(args)
    reason = _option_value(args, "--reason", "").strip() or f"aidocs service {action}"

    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    ctx = auth.authenticate(token, root, source="cli") if token else None

    actor_uid = ""
    actor_kind = "unauthenticated"
    permitted = False
    if ctx is not None:
        actor_uid = str(getattr(ctx, "user_id", "") or "")
        actor_kind = "operator"
        try:
            permitted = bool(
                auth.require_permission(
                    ctx,
                    PERM_ADMIN_DAEMON_LIFECYCLE,
                    root,
                    scope_type="project",
                    scope_id=str(root).replace("\\", "/"),
                ),
            )
        except Exception:
            permitted = False

    request, verdict = mint(
        action,
        actor_uid=actor_uid,
        actor_kind=actor_kind,
        reason=reason,
        permitted=permitted,
    )
    audit(
        verdict,
        event_kind=EVENT_LIFECYCLE_REQUESTED if verdict.authorised else EVENT_LIFECYCLE_REFUSED,
        project_root=root,
    )
    if request is None:
        print(describe(verdict))
        print(
            "  ↳ Sign in once with 'aidocs operator-login', or pass "
            "--operator-token. The daemon is STILL RUNNING."
        )
        return None
    return request


def cmd_service(args: list[str]) -> int:
    """AIDOCS-owned daemon supervision (#249): `aidocs service <sub>`.

    start   — spawn the watchdog (detached) which supervises the local HTTP
              daemon: restart-on-crash w/ backoff, crash-loop breaker,
              drain+restart on deploy. Claude Code auto-reconnects HTTP MCP
              servers, so with this running the /mcp ritual is dead.
    stop    — ask the watchdog to terminate the daemon and exit, then WAIT for
              the port to be released (so `stop` finishing means it stopped).
    restart — stop, wait for the port, then start. Exists because back-to-back
              stop/start was the only documented recovery and it raced itself
              (#591): start bound the port before the old watchdog let go and
              died with WSAEADDRINUSE, leaving the machine with no daemon.
    status  — health file + liveness probe.
    run     — run the watchdog in the FOREGROUND (what `install` registers;
              also handy under any external supervisor).
    install — register `service run` as a Windows logon task (schtasks) or
              print the systemd/launchd unit hint elsewhere.
    uninstall — remove the logon task.
    update-check — query the release channel and report {current, latest,
              update_available} (check-only, never installs; fail-soft).
    """
    import subprocess

    from .aidocs_service import (
        daemon_dir,
        request_stop,
        run_watchdog,
        service_status,
        supervisor_refusal,
        supervisor_runtime,
        wait_for_port_free,
    )

    sub = args[0] if args else "status"
    port = 8748
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    task_name = "AIDOCS-Daemon-Watchdog"

    def _spawn_watchdog() -> int:
        """Detach a watchdog for ``port``. Shared by `start` and `restart` so the
        two gestures cannot drift apart (#591 D5)."""
        # #727 (A). The watchdog HOSTS THE HOOK BROKER, so whichever interpreter
        # it starts under IS the enforcement runtime. supervisor_runtime() answers
        # BOTH questions this line has to answer: pythonw.exe (GUI-subsystem, so
        # the detached watchdog never flashes a console/Terminal tab) AND the
        # AIDOCS-OWNED runtime (so enforcement never binds to whichever checkout
        # PATH resolved). windowless_python(), which stood here since #249,
        # answers only the first -- it resolves next to sys.executable, which is
        # how `aidocs service restart` came to run the broker on live source.
        # DETACHED_PROCESS keeps it out of our console.
        #
        # FAIL CLOSED. If no owned runtime VERIFIES, we refuse rather than start
        # the broker's host on whatever PATH produced. A supervisor that cannot
        # prove it is the pinned artifact must not silently become the
        # enforcement runtime -- a daemon that is up but enforcing an unreviewed
        # tree is worse than one that is honestly down, because only the second
        # one tells you.
        rt = supervisor_runtime()
        if not rt.get("owned") or not rt.get("path"):
            print(supervisor_refusal(rt))
            return 1
        cmd = [rt["path"], "-m", "aidocs_mcp.cli", "service", "run", "--port", str(port)]
        creation = 0
        if os.name == "nt":
            creation = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        log = (daemon_dir() / "watchdog.out").open("a", encoding="utf-8")
        # #345: routed through audited_popen (ledger row per spawn). The
        # DETACHED_PROCESS creationflags are deliberate and pass through
        # UNCHANGED — a detached child allocates NO console at all, which is
        # the windowless guarantee here (CREATE_NO_WINDOW must not be mixed
        # with DETACHED_PROCESS). Passthrough lambda IS the registered AST
        # callsite ('cli.py','_spawn_watchdog','subprocess.Popen').
        from .shell_egress_service import audited_popen

        proc = audited_popen(
            cmd,
            fingerprint=("cli.py", "_spawn_watchdog", "subprocess.Popen"),
            reason="watchdog-service-start",
            popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
            creationflags=creation,  # DETACHED_PROCESS — no console by construction
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
        print(f"watchdog started (pid={proc.pid}); daemon will serve http://127.0.0.1:{port}/mcp")
        # DO NOT PRINT A `{"url": ...}` ENTRY HERE. This line did, until
        # 2026-09-02, and that bare {"type": "http", "url": ...} shape is the
        # exact thing #787 exists to remove: a url entry inherits no
        # environment, so the request carries NO per-window identity and
        # `correlate_host_session` becomes the only identity route -- and it
        # refuses on >=2 live bindings, which two open windows guarantee
        # permanently (runtime_project_support_service.py:181-196; downstream
        # #783, #785 cause 2, #786).
        #
        # THE GENERATOR WAS FIXED AT #787 AND THIS ADVICE WAS NOT. So the CLI
        # went on telling operators to hand-write the broken shape that the
        # rest of the system had just stopped emitting -- the same "the fix
        # shipped and nothing pointed at it" failure #787 itself describes,
        # one layer up, in the message the operator actually reads.
        #
        # The entry is GENERATED, never hand-written: interpreter path,
        # AIDOCS_MCP_ENDPOINT and AIDOCS_PROJECT_ROOT are resolved by one
        # writer (`project_ensure_mcp_config` ->
        # RuntimeProjectSupportService.ensure_claude_mcp_config), idempotent,
        # pinned by test_daemon_entry_goes_through_the_shim_787.
        print(
            "the .mcp.json aidocs entry is GENERATED, not hand-written -- "
            "run project_ensure_mcp_config if it is missing or stale "
            "(idempotent; it writes the stdio shim entry, not a url)"
        )
        print(
            "then `/mcp reconnect` in EVERY open window: the shim captures the "
            "conversation id AT SPAWN and no AIDOCS command can restart it, so "
            "a window that skips this keeps relaying the id it held before "
            "this restart (#833 layer 3)"
        )
        return 0

    if sub == "run":
        state = run_watchdog(port)
        print(f"watchdog exited: {state}")
        return 0 if state == "stopped" else 1
    if sub == "start":
        health = service_status()
        if health.get("status") == "up" and health.get("daemon_alive"):
            print(f"already running (pid={health.get('pid')}, port={health.get('port')})")
            return 0
        return _spawn_watchdog()
    if sub == "stop":
        lifecycle = _authorize_daemon_lifecycle("stop", args)
        if lifecycle is None:
            return 1
        request_stop(lifecycle)
        # #591 D5: `stop` used to be a request with no confirmation, so the ONLY
        # documented restart (stop then start) raced the port release and the
        # incoming watchdog died on WSAEADDRINUSE. Waiting here makes the exit of
        # `stop` mean the thing its name implies.
        freed = wait_for_port_free(port)
        print(
            f"stopped (port {port} released)"
            if freed
            else f"stop requested, but port {port} is STILL held — something is not draining"
        )
        return 0 if freed else 1
    if sub == "restart":
        # The gesture the operator was already performing by hand, made atomic
        # (#591 D5): stop, WAIT for the listener to actually go, then start.
        # Without the wait this is exactly the losing race it replaces.
        #
        # #623: restart is gated on the SAME authority as stop, because it
        # stops the daemon first. Gating `stop` alone would have left `restart`
        # as an ungoverned synonym — the failure moving one branch down instead
        # of vanishing.
        lifecycle = _authorize_daemon_lifecycle("restart", args)
        if lifecycle is None:
            return 1
        request_stop(lifecycle)
        if not wait_for_port_free(port):
            print(
                f"restart aborted: port {port} never freed — refusing to spawn a "
                f"watchdog that would only die on bind"
            )
            return 1
        print(f"stopped (port {port} released); starting...")
        return _spawn_watchdog()
    if sub == "status":
        health = service_status()
        print(json.dumps(health, indent=2))
        return 0 if health.get("status") == "up" else 1
    if sub == "update-check":
        from .aidocs_service import check_for_update

        print(json.dumps(check_for_update(), indent=2))
        return 0
    if sub == "install":
        if os.name != "nt":
            print("non-Windows: run `aidocs service run` under systemd/launchd/pm2.")
            return 0
        # #727 (A) / window posture: a PERSISTED launcher must not bake in
        # whichever interpreter happened to run `install`. sys.executable here
        # is console-subsystem python.exe (so Windows gives it a console) AND
        # whatever shell the operator used -- measured live, a Startup launcher
        # written 2026-07-06 pinned C:\Python314\python.exe and ran the
        # broker-hosting watchdog on the system Python for six weeks.
        #
        # FAIL CLOSED, and this matters MORE here than at the transient spawn:
        # what install writes is pinned until someone reinstalls it, so an
        # unowned interpreter baked in here survives every reboot. That is the
        # difference between the six-week bug and a bad afternoon.
        #
        # Resolved ONCE and reused by both launchers below. Resolving twice
        # would re-run the tier walk and let the schtasks command and the
        # Startup fallback name different interpreters -- the same two-copies
        # problem this whole item is about, in miniature.
        rt = supervisor_runtime()
        if not rt.get("owned") or not rt.get("path"):
            print(supervisor_refusal(rt))
            return 1
        sup = rt["path"]
        run_cmd = f'"{sup}" -m aidocs_mcp.cli service run --port {port}'
        # #345: routed through audited_run + CREATE_NO_WINDOW (output captured;
        # this branch is Windows-only so the flag is unconditionally real).
        from .shell_egress_service import audited_run

        result = audited_run(
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", task_name, "/TR", run_cmd],
            fingerprint=("cli.py", "cmd_service", "subprocess.run"),
            reason="service-schtasks-install",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            print(result.stdout.strip())
            return 0
        # No-admin fallback (hit live 2026-07-06: schtasks 'Access is denied'
        # in restricted shells): a Startup-folder launcher needs no elevation.
        startup = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
        if startup.is_dir():
            launcher = startup / "aidocs-daemon.cmd"
            launcher.write_text(
                "@echo off\n"
                # `start /min` MINIMISES a console, it does not prevent one:
                # python.exe is console-subsystem, so the operator gets a
                # visible python window at every logon. The owned runtime's
                # pythonw.exe is GUI-subsystem and gets no console, ever.
                f'start "" /min "{sup}"'
                f" -m aidocs_mcp.cli service run --port {port}\n",
                encoding="utf-8",
            )
            print(f"schtasks denied; installed Startup launcher: {launcher}")
            return 0
        print(result.stderr.strip() or "schtasks failed and no Startup folder found")
        return result.returncode
    if sub == "uninstall":
        if os.name != "nt":
            print("non-Windows: nothing registered by `install` here.")
            return 0
        # #345: routed through audited_run + CREATE_NO_WINDOW (see install).
        from .shell_egress_service import audited_run

        result = audited_run(
            ["schtasks", "/Delete", "/F", "/TN", task_name],
            fingerprint=("cli.py", "cmd_service", "subprocess.run"),
            reason="service-schtasks-uninstall",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(result.stdout.strip() or result.stderr.strip())
        return result.returncode
    print("usage: aidocs service {start|stop|restart|status|run|install|uninstall} [--port N]")
    return 2


def _prompt_yn(question: str, default: bool = False) -> bool:
    """Prompt user for yes/no. Returns False if non-interactive (safe default)."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        if not sys.stdin.isatty():
            return False
        answer = input(f"  {question} {hint} ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return default


def _run_install(cmd: list[str], label: str) -> bool:
    """Run an install command with live output."""
    import subprocess

    print(f"  Installing {label}...")
    try:
        # #345: routed through audited_run (ledger row per spawn). DELIBERATE
        # CONSOLE: output streams live to the operator's own terminal — no
        # CREATE_NO_WINDOW here, registered in shell_egress_service.
        # DELIBERATE_CONSOLE_SPAWNS ('cli.py','_run_install').
        from .shell_egress_service import audited_run

        result = audited_run(
            cmd,
            fingerprint=("cli.py", "_run_install", "subprocess.run"),
            reason="setup-dependency-install-live",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            timeout=300,
        )
        if result.returncode == 0:
            print(f"  \033[32m+\033[0m {label} installed")
            return True
        print(f"  \033[31mx\033[0m {label} install failed (exit code {result.returncode})")
        return False
    except FileNotFoundError:
        print(f"  \033[31mx\033[0m Command not found: {cmd[0]}")
        return False
    except Exception as e:
        print(f"  \033[31mx\033[0m {label} install error: {e}")
        return False


def decide_setup_interpreter(
    args: list[str],
    *,
    env: dict | None = None,
    prepare: Any = None,
    ambient_path: str | None = None,
    expected_version: str = "__current__",
) -> dict:
    """ONE verified interpreter decision for setup, computed BEFORE any host
    config (.mcp.json / Claude hooks / Codex hooks) is written, so every adapter
    points at the SAME owned aidocs_mcp law-version runtime.

    Returns {python_path, owned_ready, ambient_escape, refuse, expected_version,
    prep}. ``refuse=True`` ⟹ no owned runtime AND no explicit --allow-ambient →
    setup must NOT write any enforcement/MCP config.
    """
    import os as _os

    from . import runtime_provisioner as _rp

    e = env if env is not None else _os.environ
    escape = (
        "--allow-ambient" in args
        or "--ambient" in args
        or str(e.get("AIDOCS_ALLOW_AMBIENT_HOOKS") or "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    exp = _rp.expected_aidocs_version() if expected_version == "__current__" else expected_version
    prep_fn = prepare or _rp.prepare_owned_runtime_for_setup
    try:
        prep = prep_fn(expected_version=exp)
    except Exception as exc:  # noqa: BLE001 — provisioning/network failure
        prep = {"ok": False, "tier": "none", "python": None, "error": repr(exc)}
    if prep.get("ok"):
        return {
            "python_path": prep["python"],
            "owned_ready": True,
            "ambient_escape": escape,
            "refuse": False,
            "expected_version": exp,
            "prep": prep,
        }
    if escape:
        amb = ambient_path
        if amb is None:
            from .claude_hooks_install import resolve_aidocs_interpreter

            amb = resolve_aidocs_interpreter()["path"]
        return {
            "python_path": amb,
            "owned_ready": False,
            "ambient_escape": True,
            "refuse": False,
            "expected_version": exp,
            "prep": prep,
        }
    return {
        "python_path": None,
        "owned_ready": False,
        "ambient_escape": False,
        "refuse": True,
        "expected_version": exp,
        "prep": prep,
    }


def host_adapter_commands(python_path: str) -> dict:
    """The SINGLE shared host-adapter command derivation. Every host config —
    the .mcp.json ``command``, the Claude hook command, the Codex hook command —
    derives from THIS one interpreter, so they can never diverge. The hook
    command is forward-slashed (Claude/Codex parse it as a shell string); the
    .mcp.json command is the raw path (read as JSON, properly escaped).
    """
    fwd = str(python_path).replace("\\", "/")
    return {
        "mcp_command": python_path,
        "hook_cmd": f"{fwd} -m aidocs_mcp.claude_hook",
    }


def cmd_setup(args: list[str]) -> int:
    """Interactive setup wizard — detects, installs, and configures everything."""
    import shutil
    import subprocess

    project_root = Path(args[0]).resolve() if args and not args[0].startswith("-") else Path.cwd()
    auto = "--auto" in args or "--yes" in args  # non-interactive mode
    # Prefer an AIDOCS-OWNED, pinned interpreter over ambient sys.executable so
    # the security-grade gate doesn't depend on a project/system python that
    # can vanish or be shadowed. Falls back to sys.executable (flagged) until a
    # runtime is provisioned (AIDOCS_PYTHON or ~/.aidocs/runtime).
    from .claude_hooks_install import resolve_aidocs_interpreter

    _interp = resolve_aidocs_interpreter()
    python_path = _interp["path"]
    errors: list[str] = []

    # ── ONE verified interpreter decision, BEFORE any host config write ──
    # Provision/select an AIDOCS-owned, law-version runtime up front so .mcp.json,
    # Claude hooks, and Codex hooks all point at the SAME interpreter. Refuse to
    # write any enforcement/MCP config against ambient unless --allow-ambient.
    _decision = decide_setup_interpreter(args)
    if _decision["refuse"]:
        print("\n\033[31m✗ AIDOCS Setup refused\033[0m")
        print("  No AIDOCS-owned runtime, and provisioning did not produce one.")
        print(
            "  Refusing to write enforcement/MCP config (.mcp.json, Claude/"
            "Codex hooks) against ambient python.",
        )
        print(
            "  Re-run `aidocs setup --allow-ambient` for an explicit degraded "
            "install, or `aidocs runtime --fix` (or --offline-archive) first.",
        )
        return 1
    python_path = _decision["python_path"]
    _ambient_escape = bool(_decision["ambient_escape"])
    _owned_ready = bool(_decision["owned_ready"])
    _exp_law = _decision["expected_version"]
    if _decision.get("prep", {}).get("provisioned"):
        _pr = _decision["prep"].get("provision_result") or {}
        print(
            f"  provisioned AIDOCS-owned runtime: {_pr.get('tier')}/"
            f"{_pr.get('action')} (law={_exp_law})",
        )
    if not _owned_ready:
        print(
            f"  \033[33m!\033[0m --allow-ambient: host config will use AMBIENT "
            f"python ({python_path}); enforcement not AIDOCS-owned.",
        )
    # Record canonical trust for the SELECTED interpreter that the host adapters
    # below will LAUNCH — not necessarily the process running setup. If setup
    # runs from ambient but provisioned/selected an owned runtime, that runtime
    # records ITS OWN interpreter+package, so the first MCP start sees no
    # interpreter_drift. Editable/dev records truthfully as status=dev.
    from . import package_integrity as _pi

    _trust = _pi.record_selected_interpreter_trust(Path.home(), python_path, source="setup")
    _trust_row: dict = {}
    if _trust.get("recorded"):
        try:
            from .runtime_trust_store import RuntimeTrustStore

            _trust_row = RuntimeTrustStore(Path.home()).current() or {}
        except Exception:
            _trust_row = {}
    else:
        print(
            f"  \033[33m!\033[0m runtime trust UNRECORDED for the selected "
            f"interpreter ({_trust.get('reason')}). First MCP start will be "
            f"'unverified' — local OK, remote refused.",
        )
        errors.append(f"trust: unrecorded ({_trust.get('reason')})")
    # the SINGLE shared command derivation used by every host adapter below
    _host_cmds = host_adapter_commands(python_path)

    project_root.mkdir(parents=True, exist_ok=True)
    print("\n\033[1mAIDOCS Setup Wizard\033[0m\n")
    print(f"  Project: {project_root}")
    # Report the SELECTED interpreter's ACTUAL post-decision state (tier from the
    # provision/resolve decision; provenance/status from the trust row we just
    # recorded), not the stale pre-decision resolution.
    _prep = _decision.get("prep") or {}
    _tier = _prep.get("tier") or ("ambient" if not _owned_ready else "unknown")
    _owned_label = f"AIDOCS-owned:{_tier}" if _owned_ready else "ambient — not AIDOCS-owned"
    print(f"  Python:  {python_path} ({_owned_label})")
    if _trust_row:
        print(
            f"  Trust:   provenance={_trust_row.get('provenance')} "
            f"status={_trust_row.get('status')} "
            f"remote_trustworthy={_trust_row.get('remote_trustworthy')} "
            f"[{_trust.get('method')}]",
        )
    else:
        print("  Trust:   unrecorded (unverified — local OK, remote refused)")

    # Hooks are written with the current interpreter's absolute path. A temp
    # or ephemeral-venv interpreter will point settings.json at a path that
    # disappears the next time the venv is cleaned, leaving the user with a
    # silent hook failure. Warn up front so the user knows to re-run setup
    # from a stable interpreter.
    python_lower = str(python_path).lower()
    if any(
        segment in python_lower
        for segment in ("/tmp/", "\\temp\\", "\\tmp\\", "/private/var/folders/")
    ):
        print(
            "  \033[33m!\033[0m Python path looks temporary — hooks written here "
            "will break when this directory is cleaned.",
        )
        print(
            "         Install AIDOCS with a stable interpreter (system python, "
            "~/.aidocs/python, or a persistent venv).",
        )

    # ══════════════════════════════════════════════
    # 1. Agent CLI detection + install
    # ══════════════════════════════════════════════
    print("\n  \033[1m1. Agent Hosts\033[0m")

    # Claude Code CLI
    claude_cli = shutil.which("claude")
    has_vscode_claude = False
    for ext_dir in [
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".vscode-insiders" / "extensions",
    ]:
        if ext_dir.is_dir():
            for d in ext_dir.iterdir():
                if d.is_dir() and "claude" in d.name.lower():
                    has_vscode_claude = True
                    break

    if claude_cli:
        print(f"  \033[32m+\033[0m Claude Code CLI: {claude_cli}")
    elif has_vscode_claude:
        print("  \033[32m+\033[0m VS Code Claude extension found")
        npm = shutil.which("npm")
        if npm:
            if auto or _prompt_yn("Install Claude Code CLI? (needed for conductor/lane agents)"):
                _run_install(
                    ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                    "Claude Code CLI",
                )
                claude_cli = shutil.which("claude")
        else:
            print(
                "  \033[33m!\033[0m npm not found — install Claude CLI manually: npm install -g @anthropic-ai/claude-code",
            )
    else:
        print("  \033[33m!\033[0m No Claude Code detected")
        npm = shutil.which("npm")
        if npm:
            if auto or _prompt_yn("Install Claude Code CLI?"):
                _run_install(
                    ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                    "Claude Code CLI",
                )
                claude_cli = shutil.which("claude")
        else:
            print("         Install from: https://claude.ai/download")

    # Codex CLI
    codex_cli = shutil.which("codex")
    if codex_cli:
        print(f"  \033[32m+\033[0m Codex CLI: {codex_cli}")
    else:
        npm = shutil.which("npm")
        if npm:
            if auto or _prompt_yn("Install Codex CLI? (OpenAI's coding agent)"):
                _run_install(["npm", "install", "-g", "@openai/codex"], "Codex CLI")
                codex_cli = shutil.which("codex")
        else:
            print("  \033[90m-\033[0m Codex CLI not found (optional: npm install -g @openai/codex)")

    # Node.js check (needed for npm installs above)
    node = shutil.which("node")
    if not node:
        print(
            "  \033[33m!\033[0m Node.js not found — needed for Claude/Codex CLI. Install from: https://nodejs.org",
        )

    # ══════════════════════════════════════════════
    # 2. Python path configuration
    # ══════════════════════════════════════════════
    print("\n  \033[1m2. Python Configuration\033[0m")

    # Check if python is in PATH (not just this process)
    python_in_path = shutil.which("python") or shutil.which("python3")
    if python_in_path:
        print(f"  \033[32m+\033[0m Python in PATH: {python_in_path}")
    else:
        print("  \033[33m!\033[0m Python not in system PATH")
        print(f"         Using direct path: {python_path}")
        print("         Hooks will use the absolute path (works but less portable)")

    # ══════════════════════════════════════════════
    # 3. MCP configuration
    # ══════════════════════════════════════════════
    print("\n  \033[1m3. MCP Configuration\033[0m")
    mcp_path = project_root / ".mcp.json"
    # ONE-WRITER seal (2026-06): setup no longer hand-writes .mcp.json. The
    # canonical writer (ensure_claude_mcp_config → SQL mcp_servers registry →
    # project_to_file, with the DECIDED AIDOCS-owned interpreter) runs in
    # section 5 — AFTER the .MEMORY check. Writing it here would create the
    # registry DB under .MEMORY/.index and falsely mark a fresh project as
    # already-initialized, skipping init.
    print(f"  \033[2m(written by the canonical writer during init → {mcp_path})\033[0m")

    # .gitignore
    gitignore = project_root / ".gitignore"
    if gitignore.is_file():
        content = gitignore.read_text(encoding="utf-8")
        if ".mcp.json" not in content:
            with open(gitignore, "a", encoding="utf-8") as f:
                f.write("\n# AIDOCS local MCP config (contains absolute paths)\n.mcp.json\n")
            print("  \033[32m+\033[0m Added .mcp.json to .gitignore")
    else:
        gitignore.write_text(
            "# AIDOCS local MCP config (contains absolute paths)\n.mcp.json\n",
            encoding="utf-8",
        )
        print("  \033[32m+\033[0m Created .gitignore with .mcp.json")

    # ══════════════════════════════════════════════
    # 4. Hooks
    # ══════════════════════════════════════════════
    print("\n  \033[1m4. Hook Configuration\033[0m")
    # ALL host adapters use the ONE decided interpreter (see the decision block
    # near the top of cmd_setup). hook_cmd is the shared forward-slash command.
    hook_cmd = _host_cmds["hook_cmd"]

    # Single canonical, self-healing, idempotent installer (shared with the
    # passive on-hook drift repair) — forward-slash python path, preserves
    # user keys, repairs/backs-up a corrupt settings.json.
    from .claude_hooks_install import ensure_claude_hooks

    _hook_res = ensure_claude_hooks(
        python_path=python_path,
        allow_ambient=(_ambient_escape and not _owned_ready),
    )
    print(f"  \033[32m+\033[0m {_hook_res['path']} ({_hook_res['action']})")
    _tier = _hook_res.get("tier", "ambient")
    if not _hook_res.get("ok"):
        print(f"  \033[31m✗\033[0m hooks NOT installed: {_hook_res.get('reason')}")
    elif not _hook_res.get("owned"):
        print(
            f"  \033[33m!\033[0m hooks pinned to AMBIENT python ({_tier}) via "
            "explicit escape; enforcement depends on an interpreter AIDOCS does "
            "not own. Run `aidocs runtime --fix` to provision an owned runtime.",
        )
    else:
        from . import runtime_provisioner as _rp

        _prov = _rp._provenance(_rp.read_manifest(Path.home()))
        _bless = "OFFICIAL/blessed" if _prov.get("blessed") else "operator-custom"
        print(
            f"    (interpreter tier: {_tier}, AIDOCS-owned; {_bless}; "
            f"source={_prov.get('source')} version={_prov.get('version')})",
        )
    if _hook_res.get("backup"):
        print(f"    (backed up unrecoverable prior file to {_hook_res['backup']})")

    # Codex hooks (same format, different location)
    codex_cli = shutil.which("codex")
    if codex_cli:
        codex_hooks_dir = Path.home() / ".codex"
        codex_hooks_dir.mkdir(parents=True, exist_ok=True)
        codex_hooks_path = codex_hooks_dir / "hooks.json"
        from .claude_hooks_install import CODEX_HOOKS, merge_aidocs_hook_groups

        # #808: this block used to BUILD the document from literals and write it
        # WHOLE — no read of the existing file, no backup — so every `aidocs
        # setup` destroyed whatever hooks the operator had configured here. It
        # now goes through the same read-merge-write the Claude settings writer
        # uses, so keys AIDOCS did not author survive.
        _codex_res = merge_aidocs_hook_groups(codex_hooks_path, hook_cmd, CODEX_HOOKS)
        print(f"  \033[32m+\033[0m {codex_hooks_path} (Bash interception only — Codex limitation)")
        if _codex_res.get("backup"):
            print(f"    (backed up unreadable prior file to {_codex_res['backup']})")

    # ══════════════════════════════════════════════
    # 4b. OpenCode plugin installation
    # ══════════════════════════════════════════════
    opencode_cli = shutil.which("opencode")
    if opencode_cli:
        # Find the aidocs.js plugin source — try repo, then bundled in package
        plugin_source = (
            Path(__file__).resolve().parent.parent.parent.parent / "core" / "plugins" / "aidocs.js"
        )
        if not plugin_source.is_file():
            # Bundled in pip package
            plugin_source = Path(__file__).resolve().parent / "data" / "opencode_plugin.js"
        if not plugin_source.is_file():
            plugin_source = None

        if plugin_source and plugin_source.is_file():
            # Install to global OC plugins dir
            if sys.platform == "win32":
                oc_plugins_dir = Path.home() / ".config" / "opencode" / "plugins"
            else:
                oc_plugins_dir = Path.home() / ".config" / "opencode" / "plugins"

            oc_plugins_dir.mkdir(parents=True, exist_ok=True)
            target = oc_plugins_dir / "aidocs.js"
            from .claude_hooks_install import install_file_preserving_user_copy

            # #808: was a bare copy2 with no backup and no user-modification
            # check — a regression against core/scripts/install_manifest.py,
            # which always wrote a .backup before overwriting.
            _plug_res = install_file_preserving_user_copy(plugin_source, target)
            print(f"  \033[32m+\033[0m OpenCode plugin: {target} ({_plug_res['action']})")
            if _plug_res.get("backup"):
                print(f"    (your previous plugin was saved to {_plug_res['backup']})")
        else:
            print("  \033[33m!\033[0m OpenCode detected but aidocs.js plugin not found in package")

    # ══════════════════════════════════════════════
    # 5. Project initialization
    memory_dir = project_root / ".MEMORY"
    if not memory_dir.is_dir():
        print("\n  \033[1m5. Project Initialization\033[0m")
        # Thread the DECIDED interpreter so project_init's .mcp.json write uses
        # it (not ambient sys.executable) — the fresh-project overwrite fix.
        cmd_init([str(project_root), "--interpreter", python_path])
        print("  \033[32m+\033[0m AIDOCS project initialized")
    else:
        print("\n  \033[1m5. Project\033[0m")
        # Already initialized — DO NOT re-init, but refresh the .mcp.json
        # projection with the DECIDED interpreter via the SAME canonical writer
        # (so re-setup repairs a stale/ambient interpreter without a full init).
        from .mcp_server import _resolve_templates_root as _tr_setup
        from .runtime_service import RuntimeService as _RT_setup
        from .service_hub import AidocsServiceHub as _Hub_setup

        _RT_setup(
            hub=_Hub_setup(templates_root=_tr_setup())
        ).ensure_claude_mcp_config(project_root, python_path)
        print("  \033[32m+\033[0m Already initialized (.mcp.json refreshed)")

    # ══════════════════════════════════════════════
    # 6. Verification
    # ══════════════════════════════════════════════
    print("\n  \033[1m6. Verification\033[0m")
    try:
        # #345: routed through audited_run + CREATE_NO_WINDOW (captured probe).
        from .shell_egress_service import audited_run

        result = audited_run(
            [
                python_path,
                "-c",
                "from aidocs_mcp.mcp_server import create_server; print('ok')",
            ],
            fingerprint=("cli.py", "cmd_setup", "subprocess.run"),
            reason="setup-verify-import-probe",
            run=lambda *a, **kw: subprocess.run(*a, **kw),
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if result.returncode == 0 and "ok" in result.stdout:
            print("  \033[32m+\033[0m MCP server starts successfully")
        else:
            err = result.stderr.strip()[:200]
            print(f"  \033[31mx\033[0m MCP server failed: {err}")
            errors.append("MCP server")
    except Exception as e:
        print(f"  \033[31mx\033[0m MCP verification failed: {e}")
        errors.append("MCP server")

    if claude_cli:
        print("  \033[32m+\033[0m Claude CLI ready for conductor")
    elif has_vscode_claude:
        print("  \033[33m!\033[0m Claude CLI not installed — conductor lane agents require it")

    # ══════════════════════════════════════════════
    # Trust-chain proof (end-to-end, against the real artifacts just written)
    # ══════════════════════════════════════════════
    print("\n  \033[1mTrust chain\033[0m (selected runtime ↔ host configs ↔ DB):")
    try:
        _chain = _pi.prove_trust_chain(
            Path.home(),
            project_root=project_root,
            python_path=python_path,
            expected_version=_exp_law,
        )
    except Exception as _exc:
        _chain = {
            "ok": False,
            "trust_recorded": False,
            "degraded": True,
            "reason": f"proof error: {_exc!r}",
            "checks": {},
        }
    _labels = {
        "path_equality": "python_path == .mcp.json == Claude == Codex",
        "selected_imports_expected": "selected runtime imports expected aidocs_mcp",
        "db_interpreter_matches": "DB interpreter fingerprint == selected exe",
        "runtime_json_is_projection": "runtime.json is a labelled projection only",
        "no_interpreter_drift": "first MCP start: no interpreter/package drift",
    }
    for _key, _desc in _labels.items():
        _c = (_chain.get("checks") or {}).get(_key, {})
        _mark = "\033[32m✓\033[0m" if _c.get("ok") else "\033[31m✗\033[0m"
        # For the host-config equality line, show per-adapter
        # matched / absent / mismatched so an absent adapter (e.g. Codex not
        # installed) never reads as "verified".
        if _key == "path_equality":
            _st = _c.get("statuses") or {}
            _color = {"matched": "\033[32m", "absent": "\033[90m", "mismatched": "\033[31m"}
            _parts = " ".join(
                f"{_color.get(_st.get(a, 'absent'), '')}{a}:{_st.get(a, 'absent')}\033[0m"
                for a in ("mcp", "claude", "codex")
            )
            print(f"    {_mark} {_desc}  [{_parts}]")
        else:
            print(f"    {_mark} {_desc}")
    if _chain.get("ok"):
        print("  \033[32m✓ trust chain proven end-to-end.\033[0m")
    elif not _chain.get("trust_recorded"):
        print(
            f"  \033[33m! trust_unrecorded:\033[0m {_chain.get('reason')} "
            "— local OK, remote refused (NOT pretending trust is recorded).",
        )
        errors.append(f"trust_chain: unrecorded ({_chain.get('reason')})")
    else:
        print(f"  \033[33m! trust chain degraded/incomplete:\033[0m {_chain.get('reason')}")
        errors.append(f"trust_chain: {_chain.get('reason')}")

    # ══════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════
    if errors:
        print(
            f"\n\033[31mSetup completed with {len(errors)} error(s).\033[0m Run `aidocs doctor` for details.\n",
        )
        return 1

    print(f"\n\033[32m{'=' * 50}\033[0m")
    print("\033[32m  Setup complete!\033[0m")
    print(f"\033[32m{'=' * 50}\033[0m")
    print("\n  Installed agents:")
    if claude_cli:
        print("    claude  — Claude Code CLI")
    if has_vscode_claude:
        print("    vscode  — Claude Code extension")
    if codex_cli:
        print("    codex   — OpenAI Codex CLI")
    print("\n  Next steps:")
    print("  1. Open this project in your IDE")
    print("  2. Start a new agent session")
    print("  3. Type \033[1m/aidocs\033[0m to begin")
    print("\n  Troubleshooting: \033[1maidocs doctor\033[0m\n")
    return 0


def _dashboard_skill_record_event(runtime: object):
    """The execution-audit sink for a dashboard skill act, or None.

    Best-effort by design: ``record_skill_write_act`` treats None as "no
    ledger reachable" and an audit failure never decides the act.
    """
    try:
        return runtime.hub.execution.record_event  # type: ignore[union-attr]
    except Exception:
        return None


def _audit_dashboard_skill_act(
    root: Path,
    runtime: object,
    *,
    skill_id: str,
    operation: str,
    session_id: str = "",
    outcome: str,
    reason: str = "",
) -> None:
    """One act-audit row for a skill act arriving through a dashboard door.

    #676: reuses ``record_skill_write_act`` — the SAME audit shape the
    `ai_skill` door emits — so a skill act is equally legible whichever door
    it came through. Only the door label (``source_kind``) differs.
    """
    from .skill_store import record_skill_write_act

    record_skill_write_act(
        _dashboard_skill_record_event(runtime),
        root,
        session_id=session_id,
        skill_id=skill_id,
        operation=operation,
        outcome=outcome,
        reason=reason,
        source_kind="dashboard",
        capability_name="aidocs-cli",
    )


def _guard_dashboard_skill_act(
    root: Path,
    runtime: object,
    *,
    skill_id: str,
    operation: str,
    session_id: str = "",
    kind: str = "",
) -> dict | None:
    """Lawbook guard + act audit for the NON-``ai_skill`` skill doors (#676).

    `ai_skill` is not the only admission surface that can remove law: the
    dashboard commands below (and the Tauri desktop, which is a thin shell
    over exactly these commands) reach the skill catalog on their own. A
    dashboard delete of the lawbook scroll is the #615 hole through a
    different door, and it was neither refused nor recorded.

    REUSES the guards as they stand — ``refuse_lawbook_write_from_public_door``
    for the verdict and ``record_skill_write_act`` for the ledger. No second
    parallel guard, and the lawbook id set is never restated here (#461 cause
    A); a future lawbook row is covered the moment it is named in
    ``LAWBOOK_SKILL_IDS``.

    PLACEMENT: this is the ADMISSION path, exactly like the `ai_skill` door.
    It is deliberately NOT pushed down into ``SkillStore`` — the seed and the
    operator-gated promotion path write the lawbook through the store
    primitive legitimately, and guarding the primitive would refuse the
    operator along with the agent.

    Returns a refusal payload when the act must not proceed, else None.
    Every outcome is audited, refusals loudest.
    """
    from .skill_store import (
        refuse_law_kind_from_public_door,
        refuse_lawbook_write_from_public_door,
    )

    # #677: the upload door carries a BODY, so it also carries a caller-supplied
    # `kind`. The kind guard is the payload half the delete/toggle doors have no
    # use for (they name an existing row); passing kind="" leaves them exactly
    # as #676 left them. Same shared helpers, one audit path.
    refusal = refuse_lawbook_write_from_public_door(skill_id) or (
        refuse_law_kind_from_public_door(kind) if str(kind or "").strip() else None
    )
    if not refusal:
        return None
    _audit_dashboard_skill_act(
        root,
        runtime,
        skill_id=skill_id,
        operation=operation,
        session_id=session_id,
        outcome="refused",
        reason="law_write_refused",
    )
    return {
        "ok": False,
        "reason": "law_write_refused",
        "skill_id": skill_id,
        "message": refusal,
    }


def cmd_dashboard_toggle_skill(args: list[str]) -> int:
    """Toggle a skill on/off for the active session."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    # Auth wall: skill governance is admin-only.
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-toggle-skill",
        as_json,
    )
    if _rc != -1:
        return _rc
    skill_id = _option_value(args, "--skill", "").strip()
    session_id_arg = _option_value(args, "--session", "").strip() or None
    enabled = "--enable" in args
    disabled = "--disable" in args

    if not skill_id:
        payload = {
            "ok": False,
            "reason": "missing_skill",
            "message": "--skill is required",
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    _, runtime = _dashboard_runtime()
    from .managed_mode_service import resolve_managed_session

    # #1027 authority door: this id SELECTS the session whose skill selection
    # is written below, so the binding must name a session that may act.
    session_id = session_id_arg or resolve_managed_session(runtime.hub.managed_mode, root)
    if not session_id:
        payload = {
            "ok": False,
            "reason": "no_session",
            "message": "No active session. Run /aidocs first.",
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    try:
        # Get current selected skills
        current = runtime.hub.skills.get_selected_skills(root, session_id)
        current_ids = (
            [str(s) for s in current.get("selected_skills", [])]
            if isinstance(current, dict)
            else []
        )

        if disabled:
            new_ids = [s for s in current_ids if s != skill_id]
        elif enabled:
            if skill_id not in current_ids:
                new_ids = current_ids + [skill_id]
            else:
                new_ids = current_ids
        # Toggle
        elif skill_id in current_ids:
            new_ids = [s for s in current_ids if s != skill_id]
        else:
            new_ids = current_ids + [skill_id]

        # #676: the guard is DIRECTIONAL. Adding law to a session is always
        # allowed; STRIPPING it is the act that takes law away from every
        # agent in that session, so only removal reaches the lawbook guard.
        if skill_id not in new_ids:
            _law_refusal = _guard_dashboard_skill_act(
                root,
                runtime,
                skill_id=skill_id,
                operation="toggle_disable",
                session_id=session_id,
            )
            if _law_refusal is not None:
                if as_json:
                    print(_safe_json_dumps(_law_refusal, indent=2))
                else:
                    print(_law_refusal["message"])
                return 1

        runtime.set_session_skills(root, session_id, new_ids)
        _audit_dashboard_skill_act(
            root,
            runtime,
            skill_id=skill_id,
            operation="toggle_enable" if skill_id in new_ids else "toggle_disable",
            session_id=session_id,
            outcome="ok",
        )
        _audit_admin_command_applied(
            root,
            "dashboard-toggle-skill",
            _ctx,
            skill_id=skill_id,
            enabled=skill_id in new_ids,
        )
        payload = {
            "ok": True,
            "skill_id": skill_id,
            "enabled": skill_id in new_ids,
            "selected_skills": new_ids,
            "snapshot": runtime.dashboard_snapshot(root, session_id=session_id),
            "message": f"Skill '{skill_id}' {'enabled' if skill_id in new_ids else 'disabled'}",
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "reason": "toggle_failed",
            "skill_id": skill_id,
            "message": str(exc),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload["message"])
        return 1

    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        print(payload["message"])
    return 0


def cmd_dashboard_delete_skill(args: list[str]) -> int:
    """Delete a user-uploaded skill file."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    # Auth wall: skill governance is admin-only.
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-delete-skill",
        as_json,
    )
    if _rc != -1:
        return _rc
    skill_id = _option_value(args, "--skill", "").strip()
    session_id_arg = _option_value(args, "--session", "").strip() or None

    if not skill_id:
        payload = {
            "ok": False,
            "reason": "missing_skill",
            "message": "--skill is required",
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # #676: the lawbook guard runs BEFORE anything is unlinked. The desktop
    # app reaches this exact command, so guarding here guards both doors.
    _, runtime = _dashboard_runtime()
    _law_refusal = _guard_dashboard_skill_act(
        root,
        runtime,
        skill_id=skill_id,
        operation="delete",
        session_id=session_id_arg or "",
    )
    if _law_refusal is not None:
        if as_json:
            print(_safe_json_dumps(_law_refusal, indent=2))
        else:
            print(_law_refusal["message"])
        return 1

    skill_dir = root / ".MEMORY" / "skills"
    # Find the skill file by name or skill_id
    deleted_path = None
    for candidate_name in [f"{skill_id}.md", skill_id]:
        candidate = skill_dir / candidate_name
        if candidate.is_file():
            candidate.unlink()
            deleted_path = str(candidate)
            break

    if not deleted_path:
        payload = {
            "ok": False,
            "reason": "not_found",
            "skill_id": skill_id,
            "message": f"Skill file not found: {skill_id}",
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # Also remove from selected skills if present
    from .managed_mode_service import resolve_managed_session

    # #1027 authority door: this id SELECTS the session written to below.
    session_id = session_id_arg or resolve_managed_session(runtime.hub.managed_mode, root)
    if session_id:
        try:
            current = runtime.hub.skills.get_selected_skills(root, session_id)
            current_ids = (
                [str(s) for s in current.get("selected_skills", [])]
                if isinstance(current, dict)
                else []
            )
            new_ids = [s for s in current_ids if s != skill_id]
            if len(new_ids) != len(current_ids):
                runtime.set_session_skills(root, session_id, new_ids)
        except Exception:
            pass

    _audit_dashboard_skill_act(
        root,
        runtime,
        skill_id=skill_id,
        operation="delete",
        session_id=session_id,
        outcome="ok",
    )
    _audit_admin_command_applied(
        root,
        "dashboard-delete-skill",
        _ctx,
        skill_id=skill_id,
        deleted_path=deleted_path,
    )
    payload = {
        "ok": True,
        "skill_id": skill_id,
        "deleted_path": deleted_path,
        "snapshot": runtime.dashboard_snapshot(root, session_id=session_id or None),
        "message": f"Deleted skill: {skill_id}",
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        print(payload["message"])
    return 0


def _upload_skill_file_name_refusal(raw: str) -> str | None:
    """Refusal text when ``raw`` is not a safe skill file name, else None.

    #677: the Tauri door joined a CALLER-SUPPLIED name onto `.MEMORY/skills/`
    and `fs::write`-d it. `../../x.md` escapes the skills directory entirely —
    a path-traversal write primitive independent of the shadowing question.

    STRUCTURAL, not filtered: the name must be EXACTLY its own basename and
    carry no separators, no drive letter, no `..` component. A blocklist of
    traversal spellings is the shape that misses the next one.
    """
    name = str(raw or "").strip()
    if not name:
        return "--file is required"
    if name != Path(name).name or "/" in name or "\\" in name:
        return (
            f"refusing file name {name!r}: a skill file name must be a bare "
            "basename. A caller-supplied path reaching the skills directory is "
            "a traversal write, not an upload."
        )
    if name in {".", ".."} or name.startswith("."):
        return f"refusing file name {name!r}: not a skill file name"
    if Path(name).suffix.lower() != ".md":
        return (
            f"refusing file name {name!r}: skills are markdown. Only `.md` "
            "files are served as skills, so any other suffix is a file drop "
            "into the project, not a skill upload."
        )
    return None


def cmd_dashboard_upload_skill(args: list[str]) -> int:
    """Upload a user-authored skill file — the GUARDED door (#677).

    Until this command existed, the desktop app's `upload_skill` wrote
    caller-supplied CONTENT under a caller-supplied FILENAME straight into
    `.MEMORY/skills/` with a bare `fs::write`: no admin wall, no doctrine scan,
    no audit, no lawbook check, no sanitization. A skill is instruction text
    every later agent follows AS IF IT WERE LAW, and that door never learned
    about #615 or #676 because it never touched the CLI at all.

    Same shape as the other dashboard skill doors, so Tauri can shell out to it
    (`run_json_cli_checked`) exactly like `delete_skill_blocking` does and the
    desktop is covered for free — a guard reimplemented in Rust is a second
    guard that drifts.

    The guard order matters:
      1. admin wall (skill governance is not ordinary work),
      2. file-name sanitation (structural basename, `.md` only),
      3. lawbook guard on the EFFECTIVE skill id — which is the frontmatter
         `name`, NOT the file stem, so a basename-only check leaves the door
         open (`_skill_record_from_file`),
      4. law-kind guard on the frontmatter `kind` — caller-supplied, and the
         scanner is EXEMPT on doctrine kinds (#615's attacker-selected
         exemption),
      5. doctrine scan of the body; UNKNOWN REFUSES on write.
    Nothing is written until every one of them passes.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    # Auth wall: skill governance is admin-only.
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-upload-skill",
        as_json,
    )
    if _rc != -1:
        return _rc

    def _emit(payload: dict, rc: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return rc

    file_name = _option_value(args, "--file", "").strip()
    session_id = _option_value(args, "--session", "").strip()
    content_file = _option_value(args, "--content-file", "").strip()
    if content_file:
        # The desktop hands the body over a temp file, not argv: a skill body
        # is arbitrarily long and a command line is not.
        try:
            content = Path(content_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _emit(
                {
                    "ok": False,
                    "reason": "unreadable_content",
                    "message": f"Could not read --content-file: {exc}",
                },
                1,
            )
    else:
        content = _option_value(args, "--content", "")

    name_refusal = _upload_skill_file_name_refusal(file_name)
    if name_refusal:
        return _emit(
            {
                "ok": False,
                "reason": "invalid_file_name",
                "file_name": file_name,
                "message": name_refusal,
            },
            1,
        )

    _, runtime = _dashboard_runtime()
    # The EFFECTIVE skill id is what `list_skills` will key this file under —
    # frontmatter `name` first, stem only as the fallback. Derived from the
    # store's own parser so the two can never disagree about which id a file
    # claims.
    meta = runtime.hub.skills._parse_frontmatter(content)
    stem = Path(file_name).stem
    skill_id = str(meta.get("name") or stem).strip() or stem
    kind = str(meta.get("kind") or "").strip()

    _law_refusal = _guard_dashboard_skill_act(
        root,
        runtime,
        skill_id=skill_id,
        operation="upload",
        session_id=session_id,
        kind=kind,
    )
    if _law_refusal is None and skill_id.lower() != stem.lower():
        # A file whose frontmatter claims an innocent id but whose NAME is the
        # lawbook still lands at `.MEMORY/skills/aidocs-doctrine.md`. Both the
        # claimed id and the filename are checked; either one is enough.
        _law_refusal = _guard_dashboard_skill_act(
            root,
            runtime,
            skill_id=stem,
            operation="upload",
            session_id=session_id,
        )
    if _law_refusal is not None:
        return _emit(_law_refusal, 1)

    # #615 target 2 — the checker runs on WRITE. UNKNOWN IS NOT A PASS.
    from .skill_store import scan_public_skill_write

    _scan_refusal, _scan_meta = scan_public_skill_write(skill_id, content, kind)
    _scan_status = str(_scan_meta.get("scan_status") or "")
    if _scan_refusal:
        _audit_dashboard_skill_act(
            root,
            runtime,
            skill_id=skill_id,
            operation="upload",
            session_id=session_id,
            outcome="refused",
            reason="skill_scan_refused",
        )
        return _emit(
            {
                "ok": False,
                "reason": "skill_scan_refused",
                "skill_id": skill_id,
                "message": _scan_refusal,
                **_scan_meta,
            },
            1,
        )

    skill_dir = root / ".MEMORY" / "skills"
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        target = skill_dir / file_name
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        _audit_dashboard_skill_act(
            root,
            runtime,
            skill_id=skill_id,
            operation="upload",
            session_id=session_id,
            outcome="refused",
            reason="write_failed",
        )
        return _emit(
            {
                "ok": False,
                "reason": "write_failed",
                "skill_id": skill_id,
                "message": str(exc),
            },
            1,
        )

    _audit_dashboard_skill_act(
        root,
        runtime,
        skill_id=skill_id,
        operation="upload",
        session_id=session_id,
        outcome="ok",
    )
    _audit_admin_command_applied(
        root,
        "dashboard-upload-skill",
        _ctx,
        skill_id=skill_id,
        path=str(target),
    )
    return _emit(
        {
            "ok": True,
            "skill_id": skill_id,
            "path": str(target),
            "message": f"Uploaded skill: {skill_id}",
            **_scan_meta,
        },
        0,
    )


def cmd_admin(args: list[str]) -> int:
    """Admin command group: `aidocs admin <subcmd> [opts]`.

    Subcommands:
      - clear-freeze --freeze-id <id> --reason <text>
      - clear-freeze --session-id <sid> --reason <text>
      - approve-escalation <esc_id> --reason <text>
      - deny-escalation <esc_id> --reason <text>
    """
    if not args:
        print(
            "aidocs admin <subcmd> [opts]\n"
            "  clear-freeze --freeze-id <id> --reason <text>\n"
            "  clear-freeze --session-id <sid> --reason <text>\n"
            "  approve-escalation <esc_id> --reason <text>\n"
            "  deny-escalation <esc_id> --reason <text>",
        )
        return 0
    sub = args[0]
    rest = args[1:]
    if sub == "clear-freeze":
        return _cmd_admin_clear_freeze(rest)
    if sub == "approve-escalation":
        return _cmd_admin_approve_escalation(rest)
    if sub == "deny-escalation":
        return _cmd_admin_deny_escalation(rest)
    print(f"Unknown admin subcommand: {sub}")
    return 1


def _cmd_admin_clear_freeze(args: list[str]) -> int:
    """Clear a session freeze WITHOUT minting a grant.

    --freeze-id <id> (preferred) OR --session-id <sid>.
    --reason <text> always required for audit.
    --approver-email <email> always required (#404: no break-glass path).
    """
    import json

    from .identity_store import IdentityStore
    from .permission_catalog import PERM_ADMIN_CLEAR_FREEZE
    from .rbac_store import RBACStore
    from .session_freeze_store import SessionFreezeStore

    freeze_id = _option_value(args, "--freeze-id") or ""
    session_id = _option_value(args, "--session-id") or ""
    reason = _option_value(args, "--reason") or ""
    approver_email = _option_value(args, "--approver-email") or ""
    json_out = _wants_json(args)

    if not freeze_id and not session_id:
        msg = "Provide --freeze-id (preferred) or --session-id."
        if json_out:
            print(
                json.dumps(
                    {"ok": False, "blocked_by": "no_target", "error": msg},
                ),
            )
        else:
            print(msg)
        return 2
    if not reason.strip():
        msg = "--reason is required for audit."
        if json_out:
            print(
                json.dumps(
                    {"ok": False, "blocked_by": "no_reason", "error": msg},
                ),
            )
        else:
            print(msg)
        return 2

    root = _resolve_admin_root(
        args,
        freeze_id=freeze_id,
        session_id=session_id,
    )
    if root is None:
        print("AIDOCS root not found; run inside an AIDOCS project.")
        return 2

    # #404: identity is ALWAYS required — there is no kill-switch or
    # dev-flavor identity-less path. The approver must resolve to a real
    # user holding rbac.admin_clear_freeze.
    approver_user_id: str | None = None
    approver_label = "admin-cli"
    if not approver_email:
        # DERIVE the approver from the AUTHENTICATED principal (2026-07-25).
        #
        # This command already requires a valid operator token (env /
        # --operator-token / machine cache) before reaching here, so the caller's
        # identity is PROVEN. Demanding --approver-email on top of that asked a
        # signed-in admin to retype an address the process had already verified —
        # which is why the dashboard prompted for an email on every approve/deny.
        #
        # It was also weaker, not stronger: a typed email is UNVERIFIED input, so
        # the acting admin could name a DIFFERENT admin as the approver and the
        # audit chain would record them. Deriving from the token makes the
        # recorded approver the principal who actually acted. The permission
        # check (rbac.admin_clear_freeze) and the relational ladder below are
        # unchanged — this fixes WHO we believe, not WHETHER they are allowed.
        try:
            from .operator_auth_service import OperatorAuthService

            _svc = OperatorAuthService()
            _tok = OperatorAuthService.resolve_token_from_args(args)
            _ctx = _svc.authenticate(_tok, root) if _tok else None
            if _ctx is not None and getattr(_ctx, "email", ""):
                approver_email = str(_ctx.email).strip()
            else:
                # DB-backed machine login (#443) — the signed-in operator on this
                # box, same source dashboard_auth_status trusts.
                _uid = str(_svc.resolve_machine_login(root) or "").strip()
                if _uid:
                    _u = IdentityStore().get_user_by_id(root, _uid)
                    approver_email = str(getattr(_u, "email", "") or "").strip()
        except Exception:
            approver_email = ""
    if not approver_email:
        msg = (
            "--approver-email required (no authenticated operator found to "
            "derive it from; sign in with `aidocs login`)."
        )
        if json_out:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "blocked_by": "no_admin_identity",
                        "error": msg,
                    },
                ),
            )
        else:
            print(msg)
        return 2
    identity = IdentityStore()
    approver = identity.get_user_by_email(root, approver_email)
    if approver is None:
        msg = f"Unknown approver: {approver_email}"
        if json_out:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "blocked_by": "unknown_approver",
                        "error": msg,
                    },
                ),
            )
        else:
            print(msg)
        return 2
    rbac = RBACStore()
    if not rbac.has_permission(
        root,
        approver.user_id,
        PERM_ADMIN_CLEAR_FREEZE,
    ):
        msg = f"Approver lacks {PERM_ADMIN_CLEAR_FREEZE}"
        if json_out:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "blocked_by": "missing_permission",
                        "error": msg,
                    },
                ),
            )
        else:
            print(msg)
        return 2
    approver_user_id = approver.user_id
    approver_label = approver.email

    sfs = SessionFreezeStore()
    target = None
    if freeze_id:
        target = sfs.get_active_freeze_by_id(root, freeze_id)
        if target is None:
            msg = f"No active freeze with id {freeze_id}"
            if json_out:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "blocked_by": "no_active_freeze",
                            "error": msg,
                        },
                    ),
                )
            else:
                print(msg)
            return 1
        if session_id and session_id != target.session_id:
            msg = f"freeze_id {freeze_id} is bound to session {target.session_id}, not {session_id}"
            if json_out:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "blocked_by": "freeze_session_mismatch",
                            "error": msg,
                        },
                    ),
                )
            else:
                print(msg)
            return 1
    else:
        candidates = sfs.list_active_freezes(
            root,
            session_id=session_id,
        )
        if not candidates:
            msg = f"No active freeze for session {session_id}"
            if json_out:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "blocked_by": "no_active_freeze",
                            "error": msg,
                        },
                    ),
                )
            else:
                print(msg)
            return 1
        if len(candidates) > 1:
            ids = [c.request_id for c in candidates]
            msg = f"Session {session_id} has {len(candidates)} active freezes; specify --freeze-id"
            if json_out:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "blocked_by": "ambiguous_freeze",
                            "error": msg,
                            "candidate_freeze_ids": ids,
                        },
                    ),
                )
            else:
                print(msg)
                print("candidates:", ", ".join(ids))
            return 1
        target = candidates[0]

    # Relational floor (the "ladder"), layered ON TOP of the capability gate:
    # no self-clear unless org-admin; an admin's freeze only by an org-admin;
    # an operator's by any admin. Fail-closed tiers (#404: no break-glass skip).
    from .clear_freeze_service import freeze_clear_ladder_block

    _floor = freeze_clear_ladder_block(
        root,
        approver_user_id=approver_user_id or "",
        target_user_id=str(getattr(target, "user_id", "") or ""),
    )
    if _floor is not None:
        if json_out:
            print(json.dumps(_floor))
        else:
            print(_floor["error"])
        return 1

    # Clear through the ONE audited primitive (ledger-first; no
    # decide/clear/audit split-brain) shared with the MCP + chat paths.
    from .clear_freeze_service import ClearFreezeService
    from .clear_freeze_service import clear_freeze_origin_for_cli as _clear_freeze_origin

    result = ClearFreezeService().clear_with_audit(
        root,
        target_freeze=target,
        reason=reason,
        approver_label=approver_label,
        approver_user_id=approver_user_id,
        source_kind="cli_admin",
        cleared_event_kind="freeze_admin_cleared",
        permission_name=PERM_ADMIN_CLEAR_FREEZE,
        extra_payload={"via": "cli"},
        # Origin (operator directive 2026-07-15, TTY refinement 2026-07-16):
        # a REAL resolved operator identity (approver_user_id) OR a live
        # interactive TTY = operator → RESET. An agent cannot present a TTY
        # (ai_run / Bash tool / CC `!` commands are all pipe-attached), so
        # TTY = a human at a real terminal. Otherwise the clear is an agent
        # self-cancel → STRIKE. The CC `!` path (user-typed, but no TTY) is
        # compensated host-side: see SecurityViolationService.
        # void_self_cancel_after_local_clear. (No hub here → best-effort
        # strike audit + notice.)
        clear_origin=_clear_freeze_origin(approver_user_id),
    )

    ok = result.cleared and result.status == "cleared"
    out = {
        "ok": ok,
        "freeze_id": result.request_id,
        "session_id": result.session_id,
        "cleared": result.cleared,
        "escalation_status": result.escalation_status,
        "minted_grant": False,
        "status": result.status,
        "message": result.message,
    }
    if json_out:
        print(json.dumps(out))
    elif ok:
        print(
            f"Cleared freeze {result.request_id} on session "
            f"{result.session_id} (escalation: {result.escalation_status})",
        )
    else:
        # No false-success herald: report the real outcome.
        print(f"clear-freeze did NOT clear: {result.message}")
    return 0 if ok else 1


def _cmd_admin_approve_escalation(args: list[str]) -> int:
    """Approve an escalation request: mints grant + clears freeze."""
    import json

    from .clear_freeze_service import lift_freeze_for_escalation_decision
    from .escalation_store import EscalationStore
    from .identity_store import IdentityStore
    from .permission_catalog import PERM_RBAC_APPROVE_ESCALATIONS
    from .rbac_store import RBACStore

    request_id = ""
    if args and not args[0].startswith("-"):
        request_id = args[0]
        args = args[1:]
    request_id = request_id or _option_value(args, "--request-id") or ""
    reason = _option_value(args, "--reason") or ""
    approver_email = _option_value(args, "--approver-email") or ""
    json_out = _wants_json(args)
    if not request_id:
        print("Provide <request_id> or --request-id <id>.")
        return 2
    if not approver_email:
        print("--approver-email required (approval needs identity).")
        return 2

    root = _resolve_admin_root(args, request_id=request_id)
    if root is None:
        print("AIDOCS root not found; run inside an AIDOCS project.")
        return 2

    identity = IdentityStore()
    approver = identity.get_user_by_email(root, approver_email)
    if approver is None:
        print(f"Unknown approver: {approver_email}")
        return 2
    rbac = RBACStore()
    if not rbac.has_permission(
        root,
        approver.user_id,
        PERM_RBAC_APPROVE_ESCALATIONS,
    ):
        print(f"Approver lacks {PERM_RBAC_APPROVE_ESCALATIONS}")
        return 2

    escalations = EscalationStore()
    target = escalations.get(root, request_id)
    if target is None:
        print(f"Unknown request: {request_id}")
        return 1

    decided = escalations.decide(
        root,
        request_id,
        approve=True,
        approver_user_id=approver.user_id,
        approver_label=approver.email,
        reason=reason,
    )
    if decided is None:
        print("Request is not pending (already decided or expired).")
        return 1

    grant = None
    if decided.session_id and decided.requester_user_id:
        try:
            grant = escalations.create_grant(
                root,
                request_id=request_id,
                user_id=decided.requester_user_id,
                machine_id=decided.machine_id or "",
                session_id=decided.session_id or "",
                permission_name=decided.gate_permission,
                approved_by_user_id=approver.user_id,
            )
        except Exception:
            grant = None

    # #663: APPROVE forgives the conduct — lift + audited strike reset, one act.
    freeze_cleared = 0
    try:
        freeze_cleared = lift_freeze_for_escalation_decision(
            root,
            request_id,
            decision="approve",
            approver_user_id=approver.user_id,
            approver_label=approver.email,
            reason=reason,
            source_kind="cli_admin_approve_escalation",
        )
    except Exception:
        freeze_cleared = 0

    out = {
        "ok": True,
        "status": decided.status,
        "request_id": decided.request_id,
        "decided_at": decided.decided_at,
        "grant_id": grant.grant_id if grant else None,
        "freeze_cleared": int(freeze_cleared),
    }
    if json_out:
        print(json.dumps(out))
    else:
        print(
            f"Approved {request_id}; grant={out['grant_id']}; "
            f"freeze_cleared={out['freeze_cleared']}",
        )
    return 0


def _cmd_admin_deny_escalation(args: list[str]) -> int:
    """Deny an escalation request and clear the related freeze.

    AUTH PARITY (2026-05-26): mirrors _cmd_admin_approve_escalation —
    requires --approver-email AND rbac.approve_escalations permission.
    The earlier "deny accepts anonymous" path silently allowed an
    unprivileged caller to dismiss a real escalation (clearing the
    freeze + closing the record) with no audit identity. Denying an
    escalation is an authority decision, not a cleanup; same approval
    permission gates both verbs.
    """
    import json

    from .clear_freeze_service import lift_freeze_for_escalation_decision
    from .escalation_store import EscalationStore
    from .identity_store import IdentityStore
    from .permission_catalog import PERM_RBAC_APPROVE_ESCALATIONS
    from .rbac_store import RBACStore

    request_id = ""
    if args and not args[0].startswith("-"):
        request_id = args[0]
        args = args[1:]
    request_id = request_id or _option_value(args, "--request-id") or ""
    reason = _option_value(args, "--reason") or ""
    approver_email = _option_value(args, "--approver-email") or ""
    json_out = _wants_json(args)
    if not request_id:
        print("Provide <request_id> or --request-id <id>.")
        return 2
    if not approver_email:
        print("--approver-email required (deny needs identity).")
        return 2

    root = _resolve_admin_root(args, request_id=request_id)
    if root is None:
        print("AIDOCS root not found; run inside an AIDOCS project.")
        return 2

    identity = IdentityStore()
    approver = identity.get_user_by_email(root, approver_email)
    if approver is None:
        print(f"Unknown approver: {approver_email}")
        return 2
    rbac = RBACStore()
    if not rbac.has_permission(
        root,
        approver.user_id,
        PERM_RBAC_APPROVE_ESCALATIONS,
    ):
        # Same permission as approve — authority over escalation outcomes
        # is one privilege, not two. A denier without approve authority
        # could otherwise mass-dismiss legitimate requests.
        print(f"Approver lacks {PERM_RBAC_APPROVE_ESCALATIONS}")
        return 2

    escalations = EscalationStore()
    decided = escalations.decide(
        root,
        request_id,
        approve=False,
        approver_user_id=approver.user_id,
        approver_label=approver.email,
        reason=reason,
    )
    if decided is None:
        print("Request is not pending.")
        return 1
    # #663: DENY lifts the lock but is NOT absolution — strikes stand.
    freeze_cleared = 0
    try:
        freeze_cleared = lift_freeze_for_escalation_decision(
            root,
            request_id,
            decision="deny",
            approver_user_id=approver.user_id,
            approver_label=approver.email,
            reason=reason,
            source_kind="cli_admin_deny_escalation",
        )
    except Exception:
        freeze_cleared = 0
    out = {
        "ok": True,
        "status": decided.status,
        "request_id": decided.request_id,
        "decided_at": decided.decided_at,
        "freeze_cleared": int(freeze_cleared),
    }
    if json_out:
        print(json.dumps(out))
    else:
        print(f"Denied {request_id}; freeze_cleared={out['freeze_cleared']}")
    return 0


def cmd_config_set(args: list[str]) -> int:
    """CLI control-plane: USER/DEV power-user config writes.

    AIDOCS control-plane model (sealed 2026-05-20):

      - Dashboard → authenticated SUPERADMIN/ADMIN surface. Owns
        T0/security/operator-only settings, dev_mode, kill-switch,
        and anything flagged ``dashboard_only`` or
        ``security_sensitive``.
      - CLI       → USER/DEV power-user surface. May write safe
        project/session settings only; refuses dashboard_only and
        security_sensitive with an explicit "open Dashboard as
        admin" message.
      - MCP tool  → untrusted agent surface. Already gated by
        RBAC + user-intent grants + dashboard_only refusal (see
        server_project_admin_tools.config_set).

    Usage:
      aidocs config set <key> <value> [--scope project|session|global]
                                      [--session <id>] [--json]
    """
    as_json = _wants_json(args)
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) < 2:
        msg = "Usage: aidocs config set <key> <value> [--scope ...]"
        if as_json:
            print(
                _safe_json_dumps(
                    {
                        "ok": False,
                        "reason": "missing_args",
                        "message": msg,
                    },
                    indent=2,
                ),
            )
        else:
            print(msg)
        return 2

    key = positional[0].strip()
    raw_value = positional[1]
    scope = _option_value(args, "--scope", "project").strip()
    session_id = _option_value(args, "--session", "").strip() or None

    meta = SETTINGS_CATALOG.get(key)
    if meta is None:
        payload = {
            "ok": False,
            "reason": "unknown_setting",
            "key": key,
            "message": (
                f"Unknown config setting: {key}. Run `aidocs config` for the dashboard surface."
            ),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # `_resolve_root` returns the first positional as the project
    # root, but config-set's first positional is the SETTING KEY.
    # Use an explicit --root flag, falling back to cwd.
    root_flag = _option_value(args, "--root", "").strip()
    root = Path(root_flag).resolve() if root_flag else Path.cwd()

    _principal, _auth_rc = _require_authenticated_cli_principal(
        args,
        root,
        "aidocs config-set",
        as_json,
    )
    if _auth_rc != -1:
        return _auth_rc

    # #404: the dev-flavor CLI bypass for dashboard_only /
    # security_sensitive settings is EXCISED. The CLI is never the
    # admin surface for these keys on any flavor — they are reserved
    # for the authenticated Dashboard admin surface.
    try:
        from .config import get_setting as _gs

        flavor = str(_gs("distribution.flavor", default="solo") or "").strip().lower()
    except Exception:
        flavor = "solo"
    dev_flavor = False  # retired seam (#404): never unlocks

    if meta.get("dashboard_only") or meta.get("security_sensitive"):
        reason = (
            "dashboard_only_setting" if meta.get("dashboard_only") else "security_sensitive_setting"
        )
        payload = {
            "ok": False,
            "reason": reason,
            "key": key,
            "blocked_by": "control_plane",
            "message": (
                f"CLI refused to write {key!r}: this setting is "
                f"reserved for the authenticated AIDOCS Dashboard "
                f"admin surface (flavor='{flavor}'). Open the "
                f"Dashboard (`aidocs dashboard`) and sign in as "
                f"ADMIN/SUPERADMIN to change it."
            ),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # Operator Surface guard — applies even in dev-flavor: the CLI may write
    # dashboard_only/security_sensitive in a contributor build, but NEVER a
    # service-managed, deprecated, hidden-owned, or unknown key. guard_raw_
    # write fails closed on unknown and refuses the guardrail classes; route
    # those through `operator-surface --expert-set` or the owning profile.
    from . import operator_surface as _osurf

    _g = _osurf.guard_raw_write(key, action="set")
    if not _g["allowed"]:
        payload = {
            "ok": False,
            "reason": _g["reason"],
            "key": key,
            "blocked_by": "operator_surface",
            "message": _g["message"],
            "redirect": _g["redirect"],
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1
    # Coerce JSON-shaped values for boolean/integer settings; raw
    # string passes through for string types.
    value: object = raw_value
    expected = meta.get("type", "string")
    if expected == "integer":
        try:
            value = int(raw_value)
        except ValueError:
            pass
    elif expected == "boolean":
        value = str(raw_value).lower() in ("true", "1", "yes", "on")
    elif expected == "string_list":
        try:
            value = json.loads(raw_value)
        except Exception:
            value = [s for s in raw_value.split(",") if s]

    try:
        # dev-flavor CLI invocations are treated as the dashboard-
        # admin write path so dashboard_only / security_sensitive
        # settings pass through the same validation. Solo/corpo
        # CLI invocations stay on the agent-editable path (the
        # block above already refused dashboard_only there).
        config_path = _update_project_config_value(
            root,
            key,
            value,
            scope=scope,
            session_id=session_id,
            dashboard=dev_flavor,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "reason": "config_update_failed",
            "key": key,
            "message": str(exc),
        }
        if as_json:
            print(_safe_json_dumps(payload, indent=2))
        else:
            print(payload["message"])
        return 1

    # Emit a CLI audit row. source_kind is 'cli_dev' for dev-flavor
    # writes (which CAN flip dashboard_only/security_sensitive
    # settings) and 'cli' for solo/corpo writes (safe settings
    # only). Distinct from 'mcp_call' (agent) and 'dashboard_admin'
    # (Tauri dashboard UI). IdentityResolver auto-stamps user_id
    # + principal_type per the record_event signature.
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="config_set",
            source_kind="cli_dev" if dev_flavor else "cli",
            capability_name="aidocs config set",
            action_kind="config_write",
            target_entity=key,
            status="applied",
            payload={
                "key": key,
                "scope": scope,
                "flavor": flavor,
                "security_sensitive": bool(meta.get("security_sensitive")),
                "dashboard_only": bool(meta.get("dashboard_only")),
            },
        )
    except Exception:
        pass

    payload = {
        "ok": True,
        "key": key,
        "scope": scope,
        "config_path": str(config_path),
        "message": f"Updated {key}",
    }
    if as_json:
        print(_safe_json_dumps(payload, indent=2, default=str))
    else:
        print(payload["message"])
    return 0


# -- LOGIN: ONE implementation, several historical entry points ---------------
#
# Consolidated 2026-07-25 (operator directive). operator-login, dashboard-login
# and dashboard-login-oauth were three ~85-line copies of the SAME three steps:
# VERIFY a principal, MINT a token, CACHE it machine-side. Triplicated auth is
# exactly where checks drift apart -- the 2026-07-23 lockout was a root-resolver
# bug that lived in one copy and not its siblings -- so the steps now exist once
# and the historical names are thin aliases differing ONLY in their defaults.
#
# METHOD is the only real axis:
#   password   - IdentityStore.login, the password-gated mint.
#   codenexus  - a gate-attested principal (#207 section 3); the bearer is
#                verified LIVE against the gate BEFORE anything is minted
#                (#404: never mint without a verified principal).
#
# The aliases keep their published contracts (exit codes, token-echo policy,
# flags) so every caller keeps working unchanged: the Tauri kernel, the gate
# refusal messages that tell an operator to run `operator-login`, and the suite.


def _login_core(
    args: list[str],
    *,
    method: str,
    always_emit_token: bool = False,
    allow_no_cache: bool = False,
) -> int:
    """VERIFY -> MINT -> CACHE -> EMIT. The single login implementation."""
    root = _resolve_login_root(args)
    as_json = _wants_json(args)

    def _fail(reason: str, message: str, code: int = 1) -> int:
        payload = {"ok": False, "reason": reason, "message": message}
        print(_safe_json_dumps(payload, indent=2) if as_json else message)
        return code

    from .identity_store import IdentityStore

    extra: dict = {}

    # WHO WILL VOUCH for the credential about to be cached. Set explicitly in
    # each branch — an unset issuer must stay empty rather than default to a
    # kind, since a wrong label is worse than an absent one here.
    from .operator_token_resolution import ISSUER_CODENEXUS, ISSUER_LOCAL

    _verified_gate_token = ""
    _gate_token_expires_at = ""
    _gate_refresh_token = ""
    _gate_client_id = ""
    _gate_scope = ""
    _login_issuer = ""

    # -- 1. VERIFY the principal -- the ONLY method-specific step --
    if method == "password":
        # RETIREMENT ATTEMPTED 2026-08-31 AND REVERTED — recorded so the next
        # author does not repeat it (charter R1, backlog 990).
        #
        # The charter says Tauri OAuth is the only HUMAN login, so this form
        # looks retirable. It is not, YET, and the reason is the same ordering
        # constraint #507 already taught: retiring an auth path before its
        # substitute exists turns the fix INTO the next lockout.
        #
        # `cmd_login` is currently the only HEADLESS token on-ramp. Removing it
        # broke five tests that are guarding real paths, not legacy:
        #     TestOperatorLoginOnRamp::test_operator_login_cli_mints_validatable_token
        #     TestOperatorLoginOnRamp::test_operator_login_cli_bad_password_mints_nothing
        #     TestWrongAuthorityIsNotWrongPassword (x2 — #507's own messages)
        #     TestBindingApprovalDeadlock::test_approval_succeeds_from_logged_in_dashboard_with_empty_env
        # The last is a LOCKOUT GUARD. A failing lockout guard is the test doing
        # its job; retargeting it to green would be the defect, not the fix.
        #
        # THE EARLIER NOTE HERE CLAIMED "BOTH R6 SITES AND R1 UNBLOCK TOGETHER,
        # OR NEITHER DOES". That was wrong, and it is worth keeping visible
        # because it stalled the requirement for a whole cycle: the two sites
        # have OPPOSITE dependency structures.
        #
        #   THIS site is the ON-RAMP. It PRODUCES the machine-cached device
        #     session, so it cannot retire before an OAuth issuer replaces it —
        #     retiring the producer is exactly what deadlocked binding approval.
        #   `outer_gate_server.cmd_mint` was a CONSUMER. It only ever needed a
        #     session that already exists, so it needed no substitute at all,
        #     and its password RETIRED 2026-08-31 (see
        #     tests/security/test_mint_has_no_password_authority_990.py).
        #
        # Lumping a producer with its consumer made a finished job look blocked.
        # What still gates THIS site is narrower than it looked: an OAuth-issued
        # token on-ramp for CI, headless installs and dashboard approval — none
        # of which are humans, and about which the charter's "only human login"
        # says nothing.
        email = _option_value(args, "--email", "").strip()
        password = _option_value(args, "--password", "").strip()
        if not email or not password:
            return _fail("missing_args", "--email and --password are required", 2)
        store = IdentityStore()
        tok = store.login(root, email, password)
        if tok is None:
            # #507 MINIMUM FIX — a wrong AUTHORITY is not a wrong PASSWORD.
            #
            # This branch checks credentials against the LOCAL bcrypt store
            # only. There is no email+password path to CodeNexus anywhere in
            # the CLI (--method codenexus verifies an already-minted gate token
            # and takes no password), so an operator whose account lives in the
            # cloud CANNOT satisfy this form with ANY password. Reporting that
            # as "invalid email or password" told the operator their correct
            # password was wrong and sent them to reset a credential that was
            # never being checked — half of the 2026-07-25 total lockout.
            #
            # So: when the email matches no local account at all, name the
            # authority instead of blaming the credential. When the account DOES
            # exist locally the message stays the opaque credential failure —
            # a real wrong password must not become an existence oracle, and
            # this branch is the ONLY place the two cases are distinguished.
            #
            # The FULL question (retire the local form for cloud identities vs.
            # add a gate password endpoint) is an operator decision recorded on
            # #507; the charter DECIDED it, and the paragraph above records why
            # the decision cannot land until its substitute does.
            if store.get_user_by_email(root, email) is None:
                return _fail(
                    "no_local_account",
                    f"no local account for {email}; this looks like a CodeNexus "
                    f"identity — sign in with CodeNexus (the browser OAuth flow). "
                    f"This form only checks the LOCAL operator store, so no "
                    f"password can succeed here for a cloud account.",
                )
            return _fail("invalid_credentials", "invalid email or password")
        # LOCAL, and labelled as such. This machine vouches for it; the cloud
        # has never seen it and will refuse it.
        _login_issuer = ISSUER_LOCAL
    elif method == "codenexus":
        gate_token = _option_value(args, "--gate-token", "").strip()
        email = _option_value(args, "--email", "").strip().lower()
        gate_url = _option_value(
            args, "--gate-url", "https://mcp.codenexus.cloud"
        ).strip()
        if not gate_token or not email:
            return _fail("missing_args", "--gate-token and --email are required", 2)
        if not _verify_gate_token(gate_url, gate_token):
            return _fail(
                "gate_token_invalid",
                "the gate did not accept this token - sign in again (it may have "
                "expired, been revoked, or the gate is unreachable)",
            )
        tok = IdentityStore().login_with_codenexus_principal(root, email)
        if tok is None:
            # LAW 311bf3e6 — THE NAMED REMEDY MUST BE REACHABLE.
            #
            # This said "create one first (aidocs create-user --email ...)".
            # THERE IS NO `create-user` COMMAND: the string appears nowhere else
            # in the tree, in no command table, and never has. So the failure an
            # operator hits when their cloud account has no local identity sent
            # them to run something that does not exist — the same shape as
            # #507's "invalid email or password", one branch over.
            #
            # It is NOT repaired by adding the command. Operator ruling
            # 2026-08-31: "there is no 'locally created user' - users are stored
            # on codenexus.cloud". The local identity REQUIREMENT is what is
            # wrong here, and retiring it is local backlog 989 — a behavioural
            # change to live auth that is deliberately not made in passing.
            # Until then this states the situation truthfully and names a door
            # that actually opens.
            return _fail(
                "no_local_identity",
                f"'{email}' authenticated with CodeNexus, but this machine has no "
                f"enabled local identity for it, and this CLI form still requires "
                f"one. Use the browser sign-in (the Dashboard / CodeNexus OAuth "
                f"flow), which issues a gate token without needing a local "
                f"operator row. Removing this requirement is tracked as local "
                f"backlog 989 — do not look for a command to create a local user; "
                f"there is none, and there is not meant to be one.",
            )
        extra["email"] = email
        # THE GATE ALREADY VOUCHED FOR THIS ONE, above, via _verify_gate_token.
        # Carrying it into the cache is the whole point: without it the shared
        # store holds only a local session token and every cloud call 401s
        # while the operator is legitimately signed in.
        _verified_gate_token = gate_token
        # ITS OWN LIFETIME, when the caller knows it. A gate access token
        # measured about an HOUR against the local token's 30 days, so the row
        # must not let the short one inherit the long one's expiry. Optional:
        # the Tauri kernel has `expires_in` in the token response and can pass
        # it; until it does, an unrecorded expiry reads as UNPROVEN and the
        # live call remains the real check.
        _gate_token_expires_at = _option_value(
            args, "--gate-token-expires-at", ""
        ).strip()
        # THE UNATTENDED WAY BACK (#1000). The gate has returned a refresh
        # credential from this very exchange since #92 and the kernel already
        # reads it out of the response; it was handed to the WEBVIEW (which has
        # been silently renewing the browser session with it) and never to the
        # machine cache the daemon reads. So the operator's Dashboard tab
        # renewed itself while every unattended path died on the hour.
        #
        # Optional, like the expiry above: a caller that does not know these
        # writes none of them, and `gate_credential_renewal` reports "nothing
        # to renew from" instead of guessing. Passed on argv for the same
        # reason --gate-token is (the kernel invokes this CLI directly); the
        # process table exposure is the pre-existing shape of this door, not a
        # new one, and moving BOTH to stdin is tracked on #1000's spec.
        _gate_refresh_token = _option_value(args, "--gate-refresh-token", "").strip()
        _gate_client_id = _option_value(args, "--gate-client-id", "").strip()
        _gate_scope = _option_value(args, "--gate-scope", "").strip()
        _login_issuer = ISSUER_CODENEXUS
    else:
        return _fail(
            "bad_method", f"unknown --method {method!r} (password|codenexus)", 2
        )

    # -- 2. CACHE machine-side (#421 ONCE-ONLY login; default ON) --
    cached = False
    cache_file = ""
    if not (allow_no_cache and "--no-cache" in args):
        cache_path_opt = _option_value(args, "--cache-path", "").strip() or None
        try:
            from .operator_token_resolution import write_cache

            cache_file = str(
                write_cache(
                    token=tok.token,
                    user_id=tok.user_id,
                    expires_at=tok.expires_at,
                    project_root=str(root),
                    cache_path=cache_path_opt,
                    # THE VERIFIED GATE TOKEN WAS BEING THROWN AWAY. The
                    # codenexus branch checks a real gate token against the
                    # authority, then caches only the LOCAL session token
                    # `login_with_codenexus_principal` returns — so the shared
                    # store never held a cloud-usable credential by EITHER
                    # route, and every cloud call earned an opaque 401 while
                    # the operator was, correctly, "logged in". Kept alongside
                    # the local one; see write_cache on why not instead of.
                    gate_token=_verified_gate_token,
                    gate_token_expires_at=_gate_token_expires_at,
                    issuer=_login_issuer,
                    gate_refresh_token=_gate_refresh_token,
                    gate_client_id=_gate_client_id,
                    gate_scope=_gate_scope,
                ),
            )
            cached = True
        except PermissionError as exc:
            # World/group-readable CUSTOM path: refuse loudly. The token row was
            # minted but NOT persisted anywhere readable.
            return _fail("cache_refused", str(exc))
        except Exception:
            cached = False  # best-effort: an IO hiccup never blocks login

    # -- 3. EMIT --
    if cached:
        message = (
            f"Operator token minted and cached at {cache_file} - subsequent "
            f"aidocs commands use it automatically until {tok.expires_at}."
        )
    else:
        message = (
            "Operator token minted (not cached). Export it: set "
            "AIDOCS_OPERATOR_TOKEN=<token> (or pass --operator-token <token>)."
        )
    payload = {
        "ok": True,
        "user_id": tok.user_id,
        "expires_at": tok.expires_at,
        "cached": cached,
        "cache_path": cache_file,
        "message": message,
        **extra,
    }
    # The bearer is NEVER echoed unless asked: the machine cache is the
    # transport, stdout is not. operator-login is the historical exception --
    # its whole purpose is handing you a token to export.
    if always_emit_token or "--emit-token" in args:
        payload["token"] = tok.token
    if as_json:
        print(_safe_json_dumps(payload, indent=2))
    elif always_emit_token:
        print(tok.token)
    else:
        who = extra.get("email") or tok.user_id
        print(f"Signed in as {who} (expires {tok.expires_at}).")
    return 0


def cmd_login(args: list[str]) -> int:
    """Sign in - THE canonical login command.

        aidocs login --email <e> --password <pw>
        aidocs login --method codenexus --gate-token <t> --email <e>
                     [--gate-url <u>]

    Common flags: --project-root <p>, --json, --emit-token, --no-cache,
    --cache-path <f>. Defaults to --method password.

    Supersedes operator-login / dashboard-login / dashboard-login-oauth, which
    remain as aliases over the same core (see _login_core).
    """
    method = _option_value(args, "--method", "").strip().lower() or "password"
    return _login_core(args, method=method, allow_no_cache=True)


def cmd_operator_login(args: list[str]) -> int:
    """Deprecated alias of ``aidocs login`` (password method).

    Kept as a NAME: gate refusal messages instruct operators to run it, and
    scripts capture its stdout for the token - so it alone echoes the bearer by
    default and honours --no-cache.
    """
    return _login_core(
        args, method="password", always_emit_token=True, allow_no_cache=True
    )


def cmd_dashboard_login(args: list[str]) -> int:
    """Deprecated alias of ``aidocs login`` (password method), desktop defaults.

    Empire directive 2026-07-17 (1 dashboard = 1 user = bind): the machine cache
    is ALWAYS written and the bearer is echoed only under --emit-token (the
    Tauri side uses that to seed its in-process cache).
    """
    return _login_core(args, method="password")


def _verify_gate_token(gate_url: str, gate_token: str) -> bool:
    """Is this bearer LIVE at the gate right now? Fail-closed on any doubt.

    The proof is a real authenticated call: the gate validates audience, scope,
    expiry and revocation on every request, so a 200 means the token is good AT
    THIS MOMENT — which is exactly what a local mint must not assume on faith.
    """
    import json as _json
    import urllib.error
    import urllib.request

    if not gate_url or not gate_token:
        return False
    payload = _json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "project_list", "arguments": {}}}
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — fixed https gate from the app kernel
        gate_url.rstrip("/") + "/v1/mcp",
        data=payload,
        headers={
            "Authorization": f"Bearer {gate_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return int(getattr(resp, "status", 0)) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def cmd_dashboard_login_oauth(args: list[str]) -> int:
    """Deprecated alias of ``aidocs login --method codenexus``.

    Kept as a NAME because the Tauri kernel's ``webmcp_oauth_complete`` shells
    out to it by this exact string. Same defaults as dashboard-login: the bearer
    rides the machine cache and is echoed only under --emit-token.
    """
    return _login_core(args, method="codenexus")


def cmd_dashboard_auth_token(args: list[str]) -> int:
    """Mint a local operator token for the desktop dashboard.

    Solo/dev flavor: bootstraps the local super_admin operator (no
    login) and issues a bearer token the Tauri app attaches to every
    sensitive CLI call. Corpo flavor: returns login_required.

    Read-only-ish from the operator's view (it issues a credential),
    but it is the bridge that lets the Tauri side satisfy the auth
    wall without a password UI on a single-operator box.
    """
    _ = _resolve_root(args)
    as_json = _wants_json(args)
    payload = {
        "ok": False,
        "reason": "login_required",
        "message": (
            "#404: local operator tokens are no longer auto-minted for any "
            "flavor. Sign in via the Dashboard to obtain an operator token."
        ),
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 1


def cmd_dashboard_binding_create(args: list[str]) -> int:
    """Host side (/aidocs): create a pending operator binding and
    print the pairing code. The host chat shows ONLY the code +
    status — never a password or token.
    """
    root = _resolve_root_flag_style(args)
    as_json = _wants_json(args)
    host_kind = _option_value(args, "--host-kind", "unknown").strip()
    host_session_id = _option_value(args, "--host-session", "").strip()
    aidocs_session = _option_value(args, "--session", "").strip()
    requested_identity = _option_value(args, "--identity", "").strip()
    if not host_session_id:
        payload = {
            "ok": False,
            "reason": "missing_host_session",
            "message": "--host-session is required",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    from .host_operator_binding_store import HostOperatorBindingStore

    binding_id, code = HostOperatorBindingStore().create_pending(
        root,
        host_kind=host_kind,
        host_session_id=host_session_id,
        aidocs_session_id=aidocs_session,
        requested_identity=requested_identity,
    )
    payload = {
        "ok": True,
        "binding_id": binding_id,
        "pairing_code": code,
        "status": "pending",
        "message": (
            f"Pairing code: {code}. Open the AIDOCS Dashboard and "
            f"approve 'Bind to me' to authorize this session."
        ),
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_binding_list(args: list[str]) -> int:
    """Dashboard: list pending bindings awaiting operator approval."""
    root = _resolve_root_flag_style(args)
    as_json = _wants_json(args)
    from .host_operator_binding_store import HostOperatorBindingStore

    pending = HostOperatorBindingStore().list_pending(root)
    rows = [
        {
            "binding_id": b.binding_id,
            "host_kind": b.host_kind,
            "host_session_id": b.host_session_id,
            "aidocs_session_id": b.aidocs_session_id,
            "requested_identity": b.requested_identity,
            "created_at": b.created_at,
            "expires_at": b.expires_at,
        }
        for b in pending
    ]
    payload = {"ok": True, "pending": rows, "count": len(rows)}
    print(_safe_json_dumps(payload, indent=2) if as_json else f"{len(rows)} pending binding(s)")
    return 0


def cmd_bindings(args: list[str]) -> int:
    """One-glance binding surface (#421): pending + approved host-operator
    bindings for the current project, with age + expiry. Pure read.

        aidocs bindings [--audit] [--json]

    --audit additionally flags rows whose shape was NOT produced by
    HostOperatorBindingStore (foreign id prefix / format drift) so the
    operator can review external-writer rows. Review-only: this command
    changes nothing; use dashboard-binding-revoke to act on a finding.
    """
    root = _resolve_root_flag_style(args)
    as_json = _wants_json(args)
    _principal, _auth_rc = _require_authenticated_cli_principal(
        args,
        root,
        "aidocs bindings",
        as_json,
    )
    if _auth_rc != -1:
        return _auth_rc
    audit = "--audit" in args
    import time as _time
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from .host_operator_binding_store import (
        HostOperatorBindingStore,
        foreign_format_flags,
    )

    def _age_seconds(created_at: str) -> int | None:
        try:
            created = _dt.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC)
        except (ValueError, TypeError):
            return None
        return max(0, int(_time.time() - created.timestamp()))

    bindings = HostOperatorBindingStore().list_bindings(root)
    rows = []
    foreign: list[str] = []
    for b in bindings:
        row = {
            "binding_id": b.binding_id,
            "status": b.status,
            "host_kind": b.host_kind,
            "host_session_id": b.host_session_id,
            "operator_user_id": b.operator_user_id,
            "created_at": b.created_at,
            "age_seconds": _age_seconds(b.created_at),
            "expires_at": b.expires_at,
            "approved_at": b.approved_at,
        }
        if audit:
            flags = foreign_format_flags(b)
            row["audit_flags"] = flags
            if flags:
                foreign.append(b.binding_id)
        rows.append(row)
    payload: dict = {"ok": True, "bindings": rows, "count": len(rows)}
    if audit:
        payload["foreign_rows"] = foreign
        if foreign:
            payload["message"] = (
                f"{len(foreign)} binding row(s) show foreign-writer format "
                f"drift — review them (nothing was changed): {foreign}"
            )
    if as_json:
        print(_safe_json_dumps(payload, indent=2))
    else:
        for r in rows:
            age = r["age_seconds"]
            age_txt = f"{age}s" if age is not None else "?"
            line = (
                f"{r['binding_id']}  {r['status']:<9} {r['host_kind']:<12} "
                f"session={r['host_session_id']}  operator={r['operator_user_id'] or '-'}  "
                f"age={age_txt}  expires={r['expires_at']}"
            )
            if audit and r.get("audit_flags"):
                line += "  FLAGS=" + ",".join(r["audit_flags"])
            print(line)
        if not rows:
            print("no pending or approved bindings for this project")
        elif audit and foreign:
            print(payload["message"])
    return 0


def cmd_dashboard_binding_approve(args: list[str]) -> int:
    """Dashboard admin: approve a pending binding ('Bind to me').

    Requires an authenticated operator token. Atomically binds the
    operator's user_id to the host session + audits user_id/role/
    source.
    """
    root = _resolve_root_flag_style(args)
    as_json = _wants_json(args)
    binding_id = _option_value(args, "--binding-id", "").strip()
    pairing_code = _option_value(args, "--code", "").strip() or None
    if not binding_id:
        payload = {
            "ok": False,
            "reason": "missing_binding_id",
            "message": "--binding-id is required",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    # Auth wall: only an authenticated operator may approve.
    from .operator_auth_service import OperatorAuthService

    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    ctx = auth.authenticate(token, root, source="dashboard") if token else None
    if ctx is None:
        # Audit refused attempt.
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                root,
                event_kind="host_binding_approve",
                source_kind="dashboard_admin",
                capability_name="dashboard-binding-approve",
                action_kind="bind",
                target_entity=binding_id,
                status="refused",
                payload={
                    "binding_id": binding_id,
                    "reason": "unauthenticated",
                    "token_present": bool(token),
                },
            )
        except Exception:
            pass
        payload = {
            "ok": False,
            "reason": "unauthenticated",
            "blocked_by": "operator_auth",
            # #508: the remedy order matters. The Dashboard's "Bind to me"
            # button rides the SAME resolution chain that just came up empty,
            # so offering it first sent a locked-out operator back into the
            # loop that locked them out. Lead with the shell command, which
            # is the only route that can create a credential from nothing.
            "message": (
                "binding approval refused: no operator token found in env "
                "AIDOCS_OPERATOR_TOKEN, the --operator-token flag, or the "
                "machine token cache — nobody is signed in on this machine. "
                "Run 'aidocs operator-login --email <you> --password <pw>' in "
                "a shell (it caches the token machine-wide, and every surface "
                "then rides that one login). The Dashboard's 'Bind to me' "
                "button CANNOT fix this state: it resolves the operator token "
                "through this same chain, so with no cached login it has "
                "nothing to send."
            ),
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1

    from .host_operator_binding_store import HostOperatorBindingStore

    ok, reason = HostOperatorBindingStore().approve(
        root,
        binding_id=binding_id,
        operator_user_id=ctx.user_id,
        approved_by_role=ctx.role,
        pairing_code=pairing_code,
    )
    # Audit (applied or refused) with the operator triple.
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="host_binding_approve",
            source_kind="dashboard_admin",
            capability_name="dashboard-binding-approve",
            action_kind="bind",
            target_entity=binding_id,
            status="applied" if ok else "refused",
            user_id=ctx.user_id if ok else None,
            effective_role=ctx.role if ok else None,
            principal_type="human",
            payload={
                "binding_id": binding_id,
                "reason": reason,
                "user_id": ctx.user_id,
                "role": ctx.role,
                "source": "dashboard_admin",
            },
        )
    except Exception:
        pass
    if not ok:
        payload = {
            "ok": False,
            "reason": reason,
            "binding_id": binding_id,
            "message": f"binding approval failed: {reason}",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    payload = {
        "ok": True,
        "binding_id": binding_id,
        "operator_user_id": ctx.user_id,
        "role": ctx.role,
        "message": f"Bound operator {ctx.user_id} to the host session",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_binding_revoke(args: list[str]) -> int:
    """Dashboard admin: revoke a binding."""
    root = _resolve_root_flag_style(args)
    as_json = _wants_json(args)
    binding_id = _option_value(args, "--binding-id", "").strip()
    if not binding_id:
        payload = {
            "ok": False,
            "reason": "missing_binding_id",
            "message": "--binding-id is required",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    from .operator_auth_service import OperatorAuthService

    auth = OperatorAuthService()
    token = OperatorAuthService.resolve_token_from_args(args)
    ctx = auth.authenticate(token, root, source="dashboard") if token else None
    if ctx is None:
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                root,
                event_kind="host_binding_revoke",
                source_kind="dashboard_admin",
                capability_name="dashboard-binding-revoke",
                action_kind="bind",
                target_entity=binding_id,
                status="refused",
                payload={
                    "binding_id": binding_id,
                    "reason": "unauthenticated",
                    "token_present": bool(token),
                },
            )
        except Exception:
            pass
        payload = {
            "ok": False,
            "reason": "unauthenticated",
            "blocked_by": "operator_auth",
            "message": (
                "binding revoke refused: no operator token found in env "
                "AIDOCS_OPERATOR_TOKEN, the --operator-token flag, or the "
                "machine token cache. Sign in once with "
                "'aidocs operator-login'."
            ),
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    # Owner-or-admin: the binding's owner may revoke their own bind;
    # an operator with admin.manage_config may revoke any binding
    # (and decline unowned-pending ones).
    is_admin = auth.require_permission(
        ctx,
        "admin.manage_config",
        root,
        scope_type="project",
        scope_id=str(root).replace("\\", "/"),
    )
    from .host_operator_binding_store import HostOperatorBindingStore

    ok, reason = HostOperatorBindingStore().revoke_with_owner_check(
        root,
        binding_id=binding_id,
        requester_user_id=ctx.user_id,
        is_admin=is_admin,
    )
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="host_binding_revoke",
            source_kind="dashboard_admin",
            capability_name="dashboard-binding-revoke",
            action_kind="bind",
            target_entity=binding_id,
            status="applied" if ok else "refused",
            user_id=ctx.user_id,
            effective_role=ctx.role,
            principal_type="human",
            payload={
                "binding_id": binding_id,
                "revoked": ok,
                "reason": reason,
                "is_admin": is_admin,
                "user_id": ctx.user_id,
                "role": ctx.role,
                "source": "dashboard_admin",
            },
        )
    except Exception:
        pass
    if not ok:
        # not_owner / pending_requires_admin are authorization
        # refusals; not_found / already_resolved are state outcomes.
        blocked = reason in ("not_owner", "pending_requires_admin")
        payload = {
            "ok": False,
            "reason": reason,
            "binding_id": binding_id,
            "blocked_by": "binding_ownership" if blocked else None,
            "message": f"binding revoke failed: {reason}",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    payload = {
        "ok": True,
        "binding_id": binding_id,
        "revoked": True,
        "reason": reason,
        "message": f"Revoked binding {binding_id}",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_auth_status(args: list[str]) -> int:
    """Report whether an operator token is currently valid + its role.
    The Tauri app calls this to render the signed-in operator banner
    without minting a new token.

    Root resolution uses ``_resolve_login_root`` (2026-07-23). ``_resolve_root``
    treats a bare positional as the project root, and a flag VALUE is bare — so
    ``--operator-token <tok>`` resolved the root to ``<cwd>/<tok>``: a
    nonexistent project with an empty identity DB. The token was never found
    there, so this returned ``login_required`` for a PERFECTLY VALID token, and
    the desktop dashboard threw the operator back to the password form
    immediately after a successful sign-in. Identical to the 2026-07-17
    ``--email`` lockout documented on ``_resolve_login_root``; that fix never
    reached this command.
    """
    root = _resolve_login_root(args)
    as_json = _wants_json(args)
    from .operator_auth_service import OperatorAuthService

    token = OperatorAuthService.resolve_token_from_args(args)
    status = OperatorAuthService().auth_status(token, root)
    payload = {"ok": True, **status}
    print(
        _safe_json_dumps(payload, indent=2)
        if as_json
        else ("authenticated" if status["authenticated"] else "unauthenticated"),
    )
    return 0


def cmd_dashboard_auth_logout(args: list[str]) -> int:
    """Revoke the supplied operator token + GC expired token rows.
    The Tauri app calls this on logout / app exit so cached tokens
    don't accumulate as live identity_tokens rows.
    """
    root = _resolve_login_root(args)  # noqa: E501 flag-value-as-root guard
    as_json = _wants_json(args)
    from .operator_auth_service import OperatorAuthService

    token = OperatorAuthService.resolve_token_from_args(args)
    result = OperatorAuthService().logout(token, root)
    payload = {
        "ok": True,
        **result,
        "message": (f"Revoked={result['revoked']}, purged={result['purged']} expired"),
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_gate_msg_set(args: list[str]) -> int:
    """Admin-only: upsert a gate-message refusal string."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-gate-msg-set",
        as_json,
    )
    if _rc != -1:
        return _rc
    key = _option_value(args, "--key", "").strip()
    body = _option_value(args, "--body", "")
    lang = _option_value(args, "--lang", "en").strip() or "en"
    if not key:
        payload = {"ok": False, "reason": "missing_key", "message": "--key is required"}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    from .intent_tokens_store import upsert_gate_message

    result = upsert_gate_message(key, body, lang=lang, source="operator")
    _audit_admin_command_applied(
        root,
        "dashboard-gate-msg-set",
        _ctx,
        key=key,
        lang=lang,
    )
    payload = {
        "ok": True,
        "key": key,
        "lang": lang,
        **result,
        "message": f"Upserted gate message {key} ({lang})",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_gate_msg_delete(args: list[str]) -> int:
    """Admin-only: delete a gate-message refusal string."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-gate-msg-delete",
        as_json,
    )
    if _rc != -1:
        return _rc
    key = _option_value(args, "--key", "").strip()
    lang = _option_value(args, "--lang", "en").strip() or "en"
    if not key:
        payload = {"ok": False, "reason": "missing_key", "message": "--key is required"}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    from .intent_tokens_store import delete_gate_message

    deleted = delete_gate_message(key, lang)
    _audit_admin_command_applied(
        root,
        "dashboard-gate-msg-delete",
        _ctx,
        key=key,
        lang=lang,
        deleted=bool(deleted),
    )
    payload = {
        "ok": True,
        "key": key,
        "lang": lang,
        "deleted": deleted,
        "message": f"Deleted gate message {key} ({lang})",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_vocab_set(args: list[str]) -> int:
    """Admin-only: seed/upsert intent-token vocab rows for a kind."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-vocab-set",
        as_json,
    )
    if _rc != -1:
        return _rc
    lang = _option_value(args, "--lang", "en").strip() or "en"
    kind = _option_value(args, "--kind", "").strip()
    rows_json = _option_value(args, "--rows", "").strip()
    if not kind or not rows_json:
        payload = {
            "ok": False,
            "reason": "missing_args",
            "message": "--kind and --rows (JSON) are required",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    try:
        rows = json.loads(rows_json)
    except Exception as exc:
        payload = {"ok": False, "reason": "invalid_json", "message": str(exc)}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    replace = "--replace" in args
    from .intent_tokens_store import delete_parent_rows, seed_kind_rows

    deleted = 0
    if replace:
        # Upsert (replace-group) semantics matching the Tauri
        # vocab_upsert_group: delete each row's (parent_key,
        # parent_mode) group before re-seeding.
        for r in rows:
            if not isinstance(r, dict):
                continue
            pk = str(r.get("parent_key", "") or "")
            pm = str(r.get("parent_mode", "") or "")
            if pk:
                try:
                    deleted += delete_parent_rows(lang, kind, pk, pm)
                except Exception:
                    pass
    inserted = seed_kind_rows(lang, kind, rows, source="operator")
    _audit_admin_command_applied(
        root,
        "dashboard-vocab-set",
        _ctx,
        kind=kind,
        lang=lang,
        inserted=inserted,
        deleted=deleted,
    )
    payload = {
        "ok": True,
        "kind": kind,
        "lang": lang,
        "inserted": inserted,
        "deleted": deleted,
        "message": f"Seeded {inserted} vocab rows for {kind} ({lang})",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_vocab_delete(args: list[str]) -> int:
    """Admin-only: delete an intent-token vocab group."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-vocab-delete",
        as_json,
    )
    if _rc != -1:
        return _rc
    lang = _option_value(args, "--lang", "en").strip() or "en"
    kind = _option_value(args, "--kind", "").strip()
    parent_key = _option_value(args, "--parent-key", "").strip()
    parent_mode = _option_value(args, "--parent-mode", "").strip()
    if not kind or not parent_key:
        payload = {
            "ok": False,
            "reason": "missing_args",
            "message": "--kind and --parent-key are required",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    from .intent_tokens_store import delete_parent_rows

    deleted = delete_parent_rows(lang, kind, parent_key, parent_mode)
    _audit_admin_command_applied(
        root,
        "dashboard-vocab-delete",
        _ctx,
        kind=kind,
        lang=lang,
        parent_key=parent_key,
        deleted=deleted,
    )
    payload = {
        "ok": True,
        "kind": kind,
        "lang": lang,
        "parent_key": parent_key,
        "deleted": deleted,
        "message": f"Deleted vocab group {kind}/{parent_key} ({lang})",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_palace_maintenance(args: list[str]) -> int:
    """Admin-only: run guarded MemPalace maintenance (the dashboard action).

    The Tauri dashboard invokes this with the cached operator token
    (--operator-token), same pattern as other gated admin commands. The
    full authority gate (authenticated dashboard admin holding
    admin.palace_maintenance AND security.allow_palace_maintenance, or
    dev.dev_mode) lives in run_palace_maintenance — this command just
    forwards the token. corpo without login → run_palace_maintenance
    returns blocked_by=operator_auth (login_required equivalent).
    """
    from .operator_auth_service import OperatorAuthService
    from .server_palace_tools import run_palace_maintenance

    root = _resolve_login_root(args)  # noqa: E501 flag-value-as-root guard
    as_json = _wants_json(args)
    mode = _option_value(args, "--mode", "backfill_legacy_memory_drawers").strip()
    session_id = _option_value(args, "--session", "").strip() or None
    dry_run = "--dry-run" in args
    force = "--force" in args
    token = OperatorAuthService.resolve_token_from_args(args) or None
    hub, runtime = _dashboard_runtime()
    out = run_palace_maintenance(
        hub,
        runtime,
        root,
        mode=mode,
        dry_run=dry_run,
        force=force,
        session_id=session_id,
        operator_token=token,
    )
    if not out.get("ok"):
        # Map the no-auth refusal to a login_required signal for the UI.
        if out.get("blocked_by") == "operator_auth":
            out["login_required"] = True
    msg = out.get("reason") or (
        f"palace maintenance {mode}: "
        + ", ".join(
            f"{k}={out[k]}"
            for k in (
                "scanned",
                "retired_legacy",
                "reingested",
                "failed",
                "lookup_lag",
            )
            if k in out
        )
    )
    print(_safe_json_dumps(out, indent=2) if as_json else msg)
    return 0 if out.get("ok") else 1


def cmd_dashboard_clear_degraded(args: list[str]) -> int:
    """Admin-only: clear the degraded-state flag for a session."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-clear-degraded",
        as_json,
    )
    if _rc != -1:
        return _rc
    session_id = _option_value(args, "--session", "").strip()
    if not session_id:
        payload = {"ok": False, "reason": "missing_session", "message": "--session is required"}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    _, runtime = _dashboard_runtime()
    try:
        runtime.hub.query_gate.clear_degraded_state(root, session_id)
    except Exception as exc:
        payload = {"ok": False, "reason": "clear_failed", "message": str(exc)}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    _audit_admin_command_applied(
        root,
        "dashboard-clear-degraded",
        _ctx,
        session_id=session_id,
    )
    payload = {
        "ok": True,
        "session_id": session_id,
        "message": f"Cleared degraded state for {session_id}",
    }
    print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
    return 0


def cmd_dashboard_delete_session(args: list[str]) -> int:
    """Admin-only: delete a session via the dashboard governance surface."""
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-delete-session",
        as_json,
    )
    if _rc != -1:
        return _rc
    session_id = _option_value(args, "--session", "").strip()
    if not session_id:
        payload = {"ok": False, "reason": "missing_session", "message": "--session is required"}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    reason = _option_value(args, "--reason", "").strip()
    import shutil

    from .checkpoint_service import CheckpointService
    from .operator_auth_service import OperatorAuthService
    from .session_deletion_law import run_session_deletion

    _, runtime = _dashboard_runtime()
    hub = runtime.hub
    # Derive an EXPLICIT permission verdict from the authenticated context rather
    # than asserting a blanket grant. The admin wall above is the first gate;
    # this independent re-check makes session_deletion_law a true backstop — if
    # the wall is refactored or bypassed, an unauthenticated (_ctx None) or
    # unprivileged caller is still refused by the law itself.
    has_perm = False
    if _ctx is not None:
        try:
            has_perm = bool(
                OperatorAuthService().require_permission(
                    _ctx,
                    "admin.manage_config",
                    root,
                    scope_type="project",
                    scope_id=str(root).replace("\\", "/"),
                ),
            )
        except Exception:
            has_perm = False
    session_path = hub.sessions.session_path(root, session_id)
    if not session_path.exists():
        payload = {
            "ok": False,
            "reason": "not_found",
            "session_id": session_id,
            "message": f"Session not found: {session_id}",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    # Active/current bound session is never deletable until unbound.
    try:
        from .managed_mode_service import resolve_managed_session

        # #1027 authority door, WITH THE DENY PRESERVED: a binding that names
        # no usable session must not switch this delete-protection guard off,
        # so fall back to the raw bound id rather than let a stale bind unlock
        # deletion of the session it points at.
        bound = resolve_managed_session(hub.managed_mode, root) or str(
            hub.managed_mode.get_mode(root).get("session_id") or "",
        )
        is_active = bound == session_id
    except Exception:
        is_active = False

    def _list_files() -> list[str]:
        out: list[str] = []
        for p in session_path.rglob("*"):
            if p.is_file():
                out.append(p.relative_to(root).as_posix())
        return out

    def _quarantine(files: list[str]):
        return CheckpointService(root).quarantine_move(
            files,
            reason=f"dashboard-delete-session: {reason}",
            provenance={"kind": "session_cleanup", "session_id": session_id},
        )

    def _record(event_kind: str, status: str, extra: dict | None = None) -> None:
        hub.execution.record_event(
            root,
            event_kind=event_kind,
            source_kind="session_deletion_law",
            capability_name="dashboard-delete-session",
            action_kind="delete",
            target_entity=f".MEMORY/sessions/{session_id}",
            status=status,
            principal_type="human",
            payload={
                "session_id": session_id,
                "reason": reason,
                "user": getattr(_ctx, "user_id", ""),
                **(extra or {}),
            },
        )

    res = run_session_deletion(
        session_id=session_id,
        reason=reason,
        is_active=is_active,
        ctx=_ctx,
        has_permission=has_perm,  # explicit verdict; law is the backstop
        list_files=_list_files,
        quarantine_move=_quarantine,
        remove_dir=lambda: shutil.rmtree(session_path, ignore_errors=True),
        record_intent=lambda: _record("session_deletion_intent", "intent"),
        record_result=lambda cp: _record(
            "session_deletion_result",
            "deleted",
            {"checkpoint_id": getattr(cp, "checkpoint_id", "")},
        ),
    )

    if not res.get("ok"):
        print(
            _safe_json_dumps(res, indent=2)
            if as_json
            else f"Refused ({res.get('blocked_by')}): {res.get('error')}",
        )
        return 1
    # REVOKE THE SESSION-SCOPED GRANTS. dashboard-create-session mints a
    # session-scoped `session_owner` role for the creator; this command used to
    # touch RBAC not at all, so the binding outlived the session it described.
    # Session ids are OPERATOR-CHOSEN and therefore reusable, so a surviving
    # binding means recreating a session under a previously-used id silently
    # hands ownership to whoever owned the deleted one.
    #
    # Runs AFTER the deletion succeeded: revoking grants for a session that is
    # still there would strip a live owner. Best-effort and reported rather than
    # raising — the session IS deleted by this point, so failing the command
    # would misreport an applied deletion as a failure. The count is surfaced in
    # the result so an orphaned grant is visible instead of silent.
    grants_revoked = -1
    try:
        from .rbac_store import RBACStore

        grants_revoked = RBACStore().revoke_scope(
            root,
            scope_type="session",
            scope_id=session_id,
        )
    except Exception:
        grants_revoked = -1  # -1 = revocation did not complete; grants may be orphaned
    res["grants_revoked"] = grants_revoked
    _audit_admin_command_applied(
        root,
        "dashboard-delete-session",
        _ctx,
        session_id=session_id,
        deleted=True,
        grants_revoked=grants_revoked,
    )
    res["message"] = f"Deleted session {session_id} (checkpoint {res.get('checkpoint_id')})"
    print(_safe_json_dumps(res, indent=2) if as_json else res["message"])
    return 0


def cmd_dashboard_create_session(args: list[str]) -> int:
    """Admin-only: create a session via the dashboard governance surface.

    Mirrors ai_session(mode='create'): the authenticated operator gate, then
    create_session (mints SQL membership), then a least-privilege
    ``session_owner`` grant for the operator with TRUTHFUL owner_grant status
    (granted / failed→degraded). A refused create returns ok:false with
    blocked_by so the dashboard surfaces the refusal, not a false success.
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-create-session",
        as_json,
    )
    if _rc != -1:
        return _rc
    title = _option_value(args, "--title", "").strip()
    session_id = _option_value(args, "--session", "").strip()
    goal = _option_value(args, "--goal", "").strip()
    if not title and not session_id:
        payload = {
            "ok": False,
            "reason": "missing_title",
            "message": "--title or --session is required",
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    import re as _re
    from datetime import date as _date

    sid = session_id
    if not sid:
        slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
        sid = f"{_date.today().isoformat()}-{slug}"
    _, runtime = _dashboard_runtime()
    hub = runtime.hub
    owner_uid = getattr(_ctx, "user_id", "") or ""
    try:
        session = hub.sessions.create_session(
            root,
            session_id=sid,
            title=title or sid,
            owner=owner_uid or "operator",
            goal=goal or title or sid,
        )
    except Exception as exc:
        payload = {"ok": False, "reason": "create_failed", "message": str(exc)}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    # Session-owner grant (least-privilege session_owner role), truthful status.
    owner_grant = "not_required"
    if owner_uid:
        owner_grant = "failed"
        try:
            from .permission_catalog import PERM_ADMIN_MANAGE_SESSIONS as _PMS
            from .permission_catalog import seed_rbac
            from .rbac_store import RBACStore

            rb = RBACStore()
            role = rb.get_role_by_name(root, "session_owner")
            if role is None:
                seed_rbac(root)
                role = rb.get_role_by_name(root, "session_owner")
            if role is None:
                role = rb.get_role_by_name(root, "admin")
            if role is not None:
                rb.assign_role_to_user_scoped(
                    root,
                    owner_uid,
                    role.role_id,
                    scope_type="session",
                    scope_id=sid,
                    authored_by_user_id="__bootstrap__",
                )
                if rb.has_permission(root, owner_uid, _PMS, scope_type="session", scope_id=sid):
                    owner_grant = "granted"
        except Exception:
            owner_grant = "failed"
    degraded = owner_grant == "failed"
    _audit_admin_command(
        root,
        "dashboard-create-session",
        _ctx,
        status="allowed_degraded" if degraded else "applied",
        session_id=sid,
        owner_grant=owner_grant,
    )
    res: dict = {"ok": True, "session_id": session.session_id, "owner_grant": owner_grant}
    if owner_uid:
        res["owner_user_id"] = owner_uid
    if degraded:
        res["ownership_degraded"] = True
        res["warning"] = (
            f"session '{sid}' was created and is a SQL member, but the "
            f"session_owner grant for '{owner_uid}' did NOT take — ownership "
            f"is degraded; grant the session_owner role at session scope."
        )
    res["message"] = f"Created session {sid}"
    print(_safe_json_dumps(res, indent=2) if as_json else res["message"])
    return 0


def cmd_dashboard_connect_session(args: list[str]) -> int:
    """Admin-only: bind managed mode to an existing session via the dashboard.

    The operator gate first; then SQL session_membership is the sole authority
    for which session may be bound (a refused/non-member connect returns
    ok:false with blocked_by — never a false 'connected').
    """
    root = _resolve_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "dashboard-connect-session",
        as_json,
    )
    if _rc != -1:
        return _rc
    session_id = _option_value(args, "--session", "").strip()
    if not session_id:
        payload = {"ok": False, "reason": "missing_session", "message": "--session is required"}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    from .session_membership_store import SessionMembershipStore

    if not SessionMembershipStore().ensure_member_or_heal(root, session_id):
        payload = {
            "ok": False,
            "connected": False,
            "blocked_by": "session_not_in_project",
            "session_id": session_id,
            "message": (
                f"session '{session_id}' is not a member of this project "
                f"(SQL session_membership is the sole authority)."
            ),
        }
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    _, runtime = _dashboard_runtime()
    try:
        runtime.hub.managed_mode.set_mode(
            root,
            session_id=session_id,
            source="dashboard-connect-session",
        )
    except Exception as exc:
        payload = {"ok": False, "connected": False, "reason": "bind_failed", "message": str(exc)}
        print(_safe_json_dumps(payload, indent=2) if as_json else payload["message"])
        return 1
    _audit_admin_command_applied(root, "dashboard-connect-session", _ctx, session_id=session_id)
    res = {
        "ok": True,
        "connected": True,
        "session_id": session_id,
        "message": f"Connected to session {session_id}",
    }
    print(_safe_json_dumps(res, indent=2) if as_json else res["message"])
    return 0


def cmd_dashboard_memory_capture(args: list[str]) -> int:
    """Governed memory capture for the desktop dashboard form (#200, war d).

    ONE write path: delegates to the SAME governed API the memory_capture
    agent tool and the web gate use (MemoryStore.capture_memory via
    outer_gate_memory_write) — durability rubric, kind aliasing, sovereign
    guard and sqlite-canonical storage all apply. The dashboard shows truth;
    this surface writes it under the same law.

    Args: --kind <kind> --content <text> [--target-hint <rel-path>].
    """
    # Flag-style args ONLY — flag VALUES must not be mistaken for a
    # positional root (see _resolve_root docstring), so resolve from cwd.
    root = _resolve_root()
    as_json = _wants_json(args)
    kind = _option_value(args, "--kind", "").strip()
    content = _option_value(args, "--content", "").strip()
    target_hint = _option_value(args, "--target-hint", "").strip() or None

    def _emit(payload: dict[str, object], code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    if not kind or not content:
        return _emit(
            {"ok": False, "reason": "missing_args", "message": "--kind and --content are required"},
            1,
        )
    from .outer_gate_memory_write import memory_capture_web

    out = memory_capture_web(str(root), kind, content, target_hint)
    if out.get("ok"):
        out["message"] = f"Captured {kind} memory -> {out.get('target', '')}"
        return _emit(out, 0)
    out["message"] = str(out.get("_detail", "memory capture rejected"))
    return _emit(out, 1)


def cmd_dashboard_backlog(args: list[str]) -> int:
    """Governed backlog CRUD for the dashboard (2026-07-30 operator charter).

    ONE write path: delegates to ``dashboard_backlog_service.dashboard_backlog``,
    which authenticates the operator token, checks the backlog.read /
    backlog.write / backlog.remove permission, audits the attempt, and only then
    touches the store. The dashboard is a CLIENT here, not an authority — there
    is no dashboard-is-trusted shortcut (sealed law: no user is exempt from the
    gates, including the operator).

    Args: --action list|get|add|update|remove
          [--id N] [--content TEXT] [--status S] [--priority P] [--kind K]
          [--tags a,b] [--reason TEXT] [--limit N] [--operator-token TOKEN]

    A refusal exits non-zero with ``{"ok": false, "blocked_by": "operator_auth",
    "reason": "...", "required_permission": "..."}`` and NO items key — the UI
    must render it as a refusal, never as an empty backlog.
    """
    from .operator_auth_service import OperatorAuthService

    root = _resolve_root()
    as_json = _wants_json(args)
    action = _option_value(args, "--action", "").strip()
    raw_id = _option_value(args, "--id", "").strip()
    raw_limit = _option_value(args, "--limit", "").strip()
    raw_tags = _option_value(args, "--tags", "").strip()

    def _emit(payload: dict, code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message", ""))
        return code

    kwargs: dict = {}
    if raw_id:
        try:
            kwargs["backlog_id"] = int(raw_id)
        except ValueError:
            return _emit(
                {"ok": False, "reason": "bad_id", "message": f"--id must be an integer, got {raw_id!r}"},
                1,
            )
    if raw_limit:
        try:
            kwargs["limit"] = int(raw_limit)
        except ValueError:
            return _emit(
                {"ok": False, "reason": "bad_limit", "message": "--limit must be an integer"},
                1,
            )
    if raw_tags:
        kwargs["tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()]
    for flag, key in (
        ("--content", "content"),
        ("--status", "status"),
        ("--priority", "priority"),
        ("--kind", "kind"),
        ("--reason", "reason"),
    ):
        val = _option_value(args, flag, "")
        if val != "":
            kwargs[key] = val

    from .dashboard_backlog_service import dashboard_backlog

    out = dashboard_backlog(
        root,
        action,
        # The token is resolved from the SAME ladder every other dashboard
        # command uses (--operator-token / env / machine cache). The service
        # authenticates it; nothing here asserts an identity.
        token=OperatorAuthService.resolve_token_from_args(args),
        **kwargs,
    )
    if out.get("ok"):
        out.setdefault("message", f"backlog {action} ok")
        return _emit(out, 0)
    return _emit(out, 1)


def cmd_tenant_reconcile(args: list[str]) -> int:
    """Record and perform the operator's reconciliation ruling for ONE
    diverged tenant clone (#602).

    ``sync_tenant_clones`` reports a DIVERGED clone and refuses to resolve it,
    because discarding tenant-side commits is data loss on someone else's
    project. This is the door its remedy names — the only sanctioned way to
    act on that ruling, so the one operation that touches a tenant's history
    stops happening outside the audit trail.

      --gate-root <p>   the gate root the sweep scans
      --clone <p>       ONE clone: <gate-root>/tenants/<t>/projects/<p>
      --decision <d>    REQUIRED: merge_no_ff (rebase is refused by name)
      --ruling-ref <s>  optional citation for the ruling being executed
      --root <p>        the AIDOCS project whose audit ledger holds the receipt

    The ACTOR on the receipt is the authenticated operator resolved from the
    token — never a self-asserted string, so the receipt cannot name someone
    who did not sign in.
    """
    from .outer_gate_projects import ProjectError, reconcile_tenant_clone

    root = _resolve_root(args)
    as_json = _wants_json(args)

    def _emit(payload: dict, code: int) -> int:
        if as_json:
            print(_safe_json_dumps(payload, indent=2, default=str))
        else:
            print(payload.get("message") or _safe_json_dumps(payload, default=str))
        return code

    ctx, rc = _require_operator_for_admin_command(args, root, "tenant-reconcile", as_json)
    if rc != -1:
        return rc

    gate_root = _option_value(args, "--gate-root", "").strip()
    clone = _option_value(args, "--clone", "").strip()
    decision = _option_value(args, "--decision", "").strip()
    if not gate_root or not clone or not decision:
        return _emit(
            {
                "ok": False,
                "blocked_by": "missing_argument",
                "message": (
                    "tenant-reconcile requires --gate-root, --clone and an "
                    "explicit --decision (merge_no_ff). There is no default "
                    "decision: a default is a policy chosen by whoever forgot "
                    "to pass the flag."
                ),
            },
            1,
        )

    try:
        out = reconcile_tenant_clone(
            gate_root,
            clone,
            decision=decision,
            ruled_by=str(getattr(ctx, "user_id", "") or ""),
            audit_root=root,
            ruling_ref=_option_value(args, "--ruling-ref", "").strip(),
        )
    except ProjectError as exc:
        _audit_admin_command(
            root, "tenant-reconcile", ctx, status="failed",
            blocked_by=exc.blocked_by, clone=clone,
        )
        return _emit(
            {"ok": False, "blocked_by": exc.blocked_by, "message": exc.reason, "clone": clone},
            1,
        )

    ok = out["status"] in ("reconciled", "already_converged")
    if ok:
        _audit_admin_command_applied(
            root, "tenant-reconcile", ctx,
            clone=out["repo"], outcome=out["status"],
            before_sha=out["before_sha"], after_sha=out["after_sha"],
        )
    else:
        # A conflict changed NOTHING — auditing it as "applied" would be a lie.
        _audit_admin_command(
            root, "tenant-reconcile", ctx, status="no_op",
            clone=out["repo"], outcome=out["status"], conflicts=out["conflicts"],
        )
    out["ok"] = ok
    out["message"] = (
        f"tenant-reconcile {out['status']}: {out['repo']} "
        f"{out['before_sha'][:12]} -> {out['after_sha'][:12]}"
        + (f" conflicts={out['conflicts']}" if out["conflicts"] else "")
    )
    return _emit(out, 0 if ok else 1)


def _gate_admin_command_entry(args: list[str]) -> int:
    """Gate the `admin` (RBAC/freeze/escalation) command family.
    Wraps cmd_admin behind the operator auth wall.
    """
    # Resolve the root that owns the freeze/escalation from its id (works
    # from anywhere), so the wall's dev-flavor check + audit + the
    # subcommand all agree on the same project. Falls back to cwd.
    root = _resolve_admin_root(args)
    as_json = _wants_json(args)
    _ctx, _rc = _require_operator_for_admin_command(
        args,
        root,
        "admin",
        as_json,
    )
    if _rc != -1:
        return _rc
    return cmd_admin(args)


def apply_operator_grant(
    root: Path, session_id: str, tool: str, *, sticky: bool
) -> dict[str, object]:
    """#149: out-of-band operator grant. Writes the grant DIRECTLY to the same
    query_gate store the agent reads — the operator's machine+project presence
    IS the authority (identical to typing the grant phrase in-prompt, which
    needs no RBAC). Policy still applies at the store: bash is never grantable,
    and raw shell/file tools cannot be STICKY (T0 doctrine, #99) — a refused
    grant simply does not land, which we detect + report. Additive: existing
    grants are preserved. No agent surface exists for this (CLI-only), so an
    agent cannot self-grant."""
    from .query_gate import QueryGateStore

    t = (tool or "").strip()
    if not t:
        return {"ok": False, "error": "tool required", "landed": False}
    qg = QueryGateStore()
    scope = "sticky" if sticky else "session"
    if sticky:
        qg.set_user_intent_tools(root, session_id, [t], sticky=True)
        active = list(qg._load_sticky(root, session_id))
    else:
        existing = set(qg.get_user_intent_tools(root, session_id))
        qg.set_user_intent_tools(root, session_id, sorted(existing | {t}))
        active = list(qg.get_user_intent_tools(root, session_id))
    landed = t in active
    # Audit the out-of-band grant into the tamper-evident execution chain — an
    # operator granting agent authority from a side terminal must be visible in
    # the audit log, same as an in-prompt grant. event_kind carries "grant" so
    # the authority-sink audit recognizes strong coverage. Best-effort: an audit
    # failure never blocks the grant.
    try:
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            event_kind="operator_out_of_band_grant",
            source_kind="cli.apply_operator_grant",
            session_id=session_id or None,
            capability_name=t,
            action_kind="grant",
            target_entity=f"{t}:{scope}",
            status="granted" if landed else "refused",
            payload={
                "tool": t,
                "scope": scope,
                "landed": landed,
                "session_id": session_id,
                "channel": "aidocs_grant_cli",
            },
        )
    except Exception:
        pass
    result: dict[str, object] = {
        "ok": landed,
        "tool": t,
        "scope": scope,
        "landed": landed,
        "active": sorted(active),
        "session_id": session_id,
    }
    if not landed:
        result["error"] = (
            f"`{t}` was refused by policy and did not land — bash is never "
            "grantable; raw shell/file tools (grep/read/edit/write/...) cannot "
            "be STICKY. Use session scope for a per-turn raw-tool grant, or a "
            "tier-2 scoped grant for narrowed sticky bash."
        )
    return result


def cmd_grant(args: list[str]) -> int:
    """#149: `aidocs grant <tool> [--sticky] [--session <id>] [--project <path>]`
    — out-of-band operator grant for the responsive case (grant lands without
    waiting for the agent to yield an UPS)."""
    import json

    tool = ""
    if args and not args[0].startswith("-"):
        tool = args[0]
        args = args[1:]
    tool = tool or _option_value(args, "--tool") or ""
    sticky = "--sticky" in args
    session_id = _option_value(args, "--session") or ""
    json_out = _wants_json(args)
    if not tool.strip():
        print("Provide <tool>. Usage: aidocs grant <tool> [--sticky] [--session <id>]")
        return 2
    root = _resolve_admin_root(args)
    if root is None:
        print("AIDOCS root not found; run inside an AIDOCS project.")
        return 2
    _principal, _auth_rc = _require_authenticated_cli_principal(
        args,
        root,
        "aidocs grant",
        json_out,
    )
    if _auth_rc != -1:
        return _auth_rc
    if not session_id:
        # #437 Phase 1: resolve the bound managed session from the sqlite
        # aidocs_managed row (DB-canonical) — the legacy
        # aidocs-managed.json ghost read is removed.
        try:
            from .aidocs_managed_store import AidocsManagedStore

            _managed = AidocsManagedStore()
            _managed.init_db(root)
            session_id = str(_managed.get(root).get("session_id") or "").strip()
        except Exception:
            session_id = ""
    if not session_id:
        print("No managed session bound; pass --session <id> or start a managed session first.")
        return 2
    result = apply_operator_grant(root, session_id, tool, sticky=sticky)
    if json_out:
        print(json.dumps(result))
    elif result["ok"]:
        active = ", ".join(str(x) for x in (result.get("active") or []))
        print(f"✓ granted `{result['tool']}` ({result['scope']}) to session {session_id}. Active: {active}")
    else:
        print(f"✗ {result.get('error', 'grant refused')}")
    return 0 if result["ok"] else 1


COMMANDS = {
    "admin": _gate_admin_command_entry,
    "grant": cmd_grant,
    "init": cmd_init,
    "status": cmd_status,
    "dashboard": cmd_dashboard,
    "dashboard-worker": cmd_dashboard_worker,
    "dashboard-set-config": cmd_dashboard_set_config,
    "dashboard-save-toml": cmd_dashboard_save_toml,
    "dashboard-toml-editability": cmd_dashboard_toml_editability,
    "dashboard-mcp-config": cmd_dashboard_mcp_config,
    "dashboard-mcp-list": cmd_dashboard_mcp_list,
    "migrate-control-authority": cmd_migrate_control_authority,
    "governed-delete": cmd_governed_delete,
    "governed-restore": cmd_governed_restore,
    "checkpoint-gc": cmd_checkpoint_gc,
    "ai-restore": cmd_ai_restore,
    "index-sitter": cmd_index_sitter,
    "tenant-reconcile": cmd_tenant_reconcile,
    "runtime": cmd_runtime,
    "dashboard-delete-config": cmd_dashboard_delete_config,
    "dashboard-batch-config": cmd_dashboard_batch_config,
    "dashboard-capability-profiles": cmd_dashboard_capability_profiles,
    "operator-surface": cmd_operator_surface,
    "governed-bash-status": cmd_governed_bash_status,
    "governed-bash-enable": cmd_governed_bash_enable,
    "governed-bash-disable": cmd_governed_bash_disable,
    "dashboard-toggle-skill": cmd_dashboard_toggle_skill,
    "dashboard-delete-skill": cmd_dashboard_delete_skill,
    "dashboard-upload-skill": cmd_dashboard_upload_skill,
    # Canonical login (2026-07-25). The three names below are aliases over the
    # SAME core and stay for compatibility (Tauri kernel, gate refusal text).
    "login": cmd_login,
    "operator-login": cmd_operator_login,
    "dashboard-login": cmd_dashboard_login,
    "dashboard-login-oauth": cmd_dashboard_login_oauth,
    "dashboard-auth-token": cmd_dashboard_auth_token,
    "dashboard-auth-status": cmd_dashboard_auth_status,
    "dashboard-auth-logout": cmd_dashboard_auth_logout,
    "dashboard-binding-create": cmd_dashboard_binding_create,
    "dashboard-binding-list": cmd_dashboard_binding_list,
    "bindings": cmd_bindings,
    "dashboard-binding-approve": cmd_dashboard_binding_approve,
    "dashboard-binding-revoke": cmd_dashboard_binding_revoke,
    "dashboard-gate-msg-set": cmd_dashboard_gate_msg_set,
    "dashboard-gate-msg-delete": cmd_dashboard_gate_msg_delete,
    "dashboard-vocab-set": cmd_dashboard_vocab_set,
    "dashboard-vocab-delete": cmd_dashboard_vocab_delete,
    "dashboard-memory-capture": cmd_dashboard_memory_capture,
    "dashboard-backlog": cmd_dashboard_backlog,
    "dashboard-clear-degraded": cmd_dashboard_clear_degraded,
    "dashboard-palace-maintenance": cmd_dashboard_palace_maintenance,
    "dashboard-delete-session": cmd_dashboard_delete_session,
    "dashboard-create-session": cmd_dashboard_create_session,
    "dashboard-connect-session": cmd_dashboard_connect_session,
    "config": cmd_config,
    "config-set": cmd_config_set,
    "sync": cmd_sync,
    "benchmark": cmd_benchmark,
    "descriptors": cmd_descriptors,
    "project-registry": cmd_project_registry,
    "snapshots": cmd_snapshots,
    "version": cmd_version,
    "managed-mode-set": cmd_managed_mode_set,
    "managed-mode-clear": cmd_managed_mode_clear,
    "doctor": cmd_doctor,
    "service": cmd_service,
    "setup": cmd_setup,
    "--version": cmd_version,
    "-v": cmd_version,
}


def _force_utf8_stdout() -> None:
    """Pin stdout/stderr to UTF-8 so JSON payloads are not mangled by the
    Windows cp1252 default encoding. Without this, `print(_safe_json_dumps(...))`
    encodes as cp1252 + surrogateescape, and any non-cp1252 character in a
    path, file content, or symbol name becomes broken bytes on the wire.
    errors='replace' keeps us from crashing on truly bad upstream data.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_REPLACEMENT_CHAR = "\ufffd"


def _sanitize_for_json(value: Any) -> Any:
    """Strip lone UTF-16 surrogate code points from every string reachable
    through `value`. Required because json.dumps(ensure_ascii=True) on a
    string containing a bare surrogate emits `\\udcXX` as ASCII — valid
    Python, invalid JSON — and serde_json on the Rust dashboard side
    crashes with `lone leading surrogate in hex escape`. Surrogates enter
    via mis-decoded file content (UTF-8 bytes read as cp1252+surrogate
    escape) and stale code-index cache entries that captured the corruption.
    """
    if isinstance(value, str):
        if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            return value
        return "".join(_REPLACEMENT_CHAR if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in value)
    if isinstance(value, dict):
        return {_sanitize_for_json(k): _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = [_sanitize_for_json(item) for item in value]
        return cleaned if isinstance(value, list) else tuple(cleaned)
    return value


def _safe_json_dumps(payload: Any, **kwargs: Any) -> str:
    return json.dumps(_sanitize_for_json(payload), **kwargs)


def main() -> None:
    _force_utf8_stdout()
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
