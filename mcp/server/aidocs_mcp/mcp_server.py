from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP  # type-only: annotations reference it (F821 seal)

import asyncio
import functools
import json
import signal
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import uuid4

from .config import (
    TOOLS_CALL_TIMEOUT,
    TOOLS_GIT_TIMEOUT,
    TOOLS_MAX_TIMEOUT,
    _load_action_hook_defaults,
    render_interaction_text,
)
from .config_schema import available_config_edit_modes, self_edit_available_in_profile
from .file_ops import (
    anchor_replace as _file_anchor_replace,
)
from .file_ops import (
    batch_edit as _file_batch_edit,
)
from .file_ops import (
    batch_str_replace as _file_batch_str_replace,
)
from .file_ops import (
    create_file as _file_create_file,
)
from .file_ops import (
    edit_lines as _file_edit_lines,
)
from .file_ops import (
    extract_block as _file_extract_block,
)
from .file_ops import (
    get_lines as _file_get_lines,
)
from .file_ops import (
    read_raw as _file_read_raw,
)
from .file_ops import (
    str_replace as _file_str_replace,
)
from .git_helpers import run_git_sync as _run_git_sync
from .managed_mode_service import (
    explain_managed_session,
    resolve_managed_session,
)
# The server tool-registration modules (register_*_tools) and tool_display's
# `renders_as` decorator are imported INSIDE create_server, NOT here. They
# transitively pull the fastmcp/mcp SDK (~2.8s cold) which is needed ONLY when a
# server is actually created. Deferring keeps `import aidocs_mcp.mcp_server` cheap
# for helper-only / tooling imports (CLI and the Claude hook never import this
# module and are already SDK-free); a broken tool module still fails loudly at
# create_server() — the real server entrypoint. See the import block there.
from .mcp_server_runtime_helpers import (
    all_capabilities as _all_capabilities,
)
from .mcp_server_runtime_helpers import (
    all_procedures as _all_procedures,
)
from .mcp_server_runtime_helpers import (
    capture_enabled as _capture_enabled,
)
from .mcp_server_runtime_helpers import (
    project_root_from_args as _project_root_from_args,
)
from .mcp_server_runtime_helpers import (
    registered_tools as _registered_tools,
)
from .mcp_server_runtime_helpers import (
    resolve_related_root as _resolve_related_root,
)
from .mcp_server_runtime_helpers import (
    summarize_tool_result as _summarize_tool_result,
)
from .project_info_store import ProjectInfoStore
from .project_registry_service import ProjectRegistryService
from .runtime_service import RuntimeService
from .service_hub import AidocsServiceHub
from .skill_provider import BUNDLED_PROVIDER_ID
from .skill_resolution import (
    match_selected_skill_id_for_trigger as _match_selected_skill_id_for_trigger,
)

_BUNDLED_OVERRIDE_PROVIDER_ID = "superpowers_external"
_RUNTIME_OWNED_OVERRIDE_MODES = {"aidocs_runtime_owned"}


def _resolve_session_id(hub: Any, project_root: Path) -> str:
    """Resolve the session id for tools that accept an optional ``session_id`` arg.

    Falls back to the active managed-mode session. Raises a clear error when
    no session is bound rather than letting a downstream NameError surface.

    Dental-app feedback 2026-04-17: 11 callsites used this helper but it was
    never defined — every one threw a NameError when session_id was omitted.
    """
    sid = ""
    try:
        sid = resolve_managed_session(hub.managed_mode, project_root)
    except Exception:
        pass
    if not sid:
        # NAME A DOOR THAT EXISTS AND OPENS FROM HERE (law 311bf3e6). This
        # tested "no session is BOUND" and offered `/aidocs`, which
        # COMMISSIONS an unmanaged project — the wrong door on a project that
        # is already managed, and one the refused caller cannot open anyway
        # (a slash command runs through the Skill tool, which no bootstrap
        # exemption covers). ai_session is BOOTSTRAP_EXEMPT, so it opens in
        # exactly the state this refusal describes.
        raise ValueError(
            "No active session. Pass session_id explicitly, or bind one: "
            "`ai_session(mode='list')` to see them, then "
            "`ai_session(mode='connect', session_id='<id>')`.",
        )
    return sid


def _resolve_failure_duty_id(hub: Any, project_root: Path) -> str:
    """The identity the FAILURE LEDGER keys duty on — the host session uuid.

    Not interchangeable with _resolve_session_id. The ledger is written by the
    Stop hook, which stamps current_duty / causal_origin with the HOST SESSION
    UUID ("74a03862-..."), while _resolve_session_id returns the MANAGED-MODE
    session id — the AIDOCS session NAME ("2026-07-11-dead-code-hygiene-fable").
    apply_disposition compares those with `!=`, so defaulting to the wrong axis
    made an agent unable to triage its OWN failures:

      mode='list'  -> `blockers: 0` while the Stop hook blocked the turn on 37
                      named failures. An empty list is indistinguishable from a
                      clean ledger, so the read path failed SILENTLY.
      mode='fixed' -> "failure ... is under another session's duty
                      ('74a03862-...'); cannot dispose cross-session" — refusing
                      the very session that owned it.

    Falls back to the managed session id when no host session is resolvable (a
    CLI/daemon caller), so non-hook contexts keep working. The cross-session
    guard itself is untouched and still refuses a genuinely foreign identity.
    Same identity-axis family as #555 (rotating actor id orphaning its own runs).

    THE SUBAGENT LINK (operator ruling 2026-08-29) is composed by
    `failure_stewardship.compose_failure_duty_id`, the SAME function the Stop
    hook writes through — a second copy of that formula here is exactly how a
    writer and a reader come to disagree, which is the bug the rest of this
    docstring describes. With no agent id it returns the host session id byte for
    byte, so every row already in the ledger keeps matching.
    """
    try:
        from .failure_stewardship import compose_failure_duty_id
        from .mcp_server_runtime_helpers import (
            current_calling_agent_id,
            current_calling_host_kind,
            current_calling_host_session_id,
        )

        sid = str(current_calling_host_session_id() or "").strip()
        if sid:
            return compose_failure_duty_id(
                project_root=project_root,
                host_session_id=sid,
                agent_id=current_calling_agent_id(),
                host_kind=current_calling_host_kind() or "claude_code",
            )
    except Exception:  # noqa: BLE001 — fall back rather than fail the tool
        pass
    return _resolve_session_id(hub, project_root)


def _git_result_fields(result: Any) -> dict[str, Any]:
    """ai_git's response body — each fact stated exactly ONCE (#565/#543).

    The old shape was ``{op, success, exit_code, output, duration}`` on every
    call. Three of those five were waste or worse:

    * ``op`` echoed the caller's own argument back at them.
    * ``exit_code: 0`` beside ``success: true`` encoded ONE FACT TWICE.
      Redundant fields can DIVERGE, and nothing in the type says which wins
      for ``success: true, exit_code: 1`` — so every consumer invents its own
      answer and they will not all agree. Three separate instances of exactly
      that drift were measured elsewhere in this project (backlog count vs
      status filter, merge vs get, service_status vs runtime_freshness).
    * ``duration`` was unrequested telemetry at 17 significant digits, on an
      operation whose timing the caller cannot act on.

    ``exit_code`` survives ONLY on failure, where it is no longer a duplicate
    but the detail ``success: false`` cannot carry. So the redundant pair never
    coexists and divergence is unrepresentable — debloating must drop what does
    not change behaviour, never the diagnostic.

    CORRECTION (#608): they were NOT one fact, and the divergence declared
    unrepresentable was measured — ``success:false`` beside ``exit_code:0`` on a
    commit that HAD landed, because the output guard read git's 40-hex object
    ids as credential-shaped and the withheld ECHO was reported as a failed
    ACTION. ``success`` is the command's outcome (``action_success``); whether
    its output could be shown is ``output_status``, a separate named field. An
    agent that retries on ``success:false`` was walking toward a session
    freeze, so this distinction is not cosmetic.

    Readers checked before removal: ``mcp/gate_checks/webmcp_smoke.py`` reads
    only ``success`` and ``output`` (both kept); the dashboard never reads
    ``exit_code``; no test asserts on a git response's op/exit_code/duration.
    """
    action_success = getattr(result, "action_success", None)
    if action_success is None:
        action_success = result.success
    output_status = str(getattr(result, "output_status", "clean") or "clean")
    fields: dict[str, Any] = {
        "success": action_success,
        "output": result.stdout_preview or result.stderr_preview,
    }
    if output_status.startswith("withheld"):
        # The `output` above is a NOTICE, not the command's echo. Say so, so no
        # reader parses the notice as data and no agent retries a landed action.
        fields["output_withheld"] = True
        fields["output_status"] = output_status
        fields["exit_code"] = result.exit_code
    elif not action_success:
        fields["exit_code"] = result.exit_code
    return fields


def _git_commit_command(message: str) -> str:
    """Build `git commit -m <message>` with the message quoted as DATA.

    This was an f-string — `git commit -m "{message}"` — so any double quote in
    the prose ended the argument. git_ops refuses shell metacharacters in `path`
    and `range` by name, but `message` is free prose by definition and went in
    raw. It fired while committing the #561 fix: quoted fragments became git
    PATHSPECS (the argv position that decides which files a commit touches) and
    post-newline fragments became shell commands. It was loud only because the
    fragments happened to be nonsense.

    execute_shell's own waiver already states the rule this callsite broke:
    "use shlex.quote() on every interpolated value". POSIX quoting is the
    correct quoting here because #561 phase 1 made a named bash the interpreter
    on every platform — under the old cmd.exe fallback it would not have been.
    """
    import shlex

    if not message:
        return "echo 'message required for commit'"
    # #684 SINK SCREEN. A commit message is agent prose that never becomes a
    # file, so neither the write guard nor the pre-commit scanner ever sees
    # it — which is how a double-encoded em-dash reached a commit. REPAIR
    # rather than refuse: refusing here would block a commit on a false
    # positive and wedge the agent, and the repair is signature-only and
    # non-destructive (Romanian/Italian diacritics pass untouched).
    from .agent_prose_screen import repair_agent_prose

    message = repair_agent_prose(message, sink="git_commit_message")
    return "git commit -m " + shlex.quote(message)


def _git_commit_message_refusal(message: str) -> str | None:
    """Refusal reason when a commit message carries credential material.

    A commit message is PUBLISHED, PERMANENT text: pushed to a remote and
    unrevocable short of a history rewrite. It is also the one caller-supplied
    blob on the ai_git path that no screen reads. The destructive floor
    (bash_policy.py:1623-1625) and the heuristic judge (heuristic_judge.py:397)
    both grade the surface returned by shell_data_windows.mask_data_windows,
    which BLANKS the -m value by design -- and `shlex.quote` in
    `_git_commit_command` guarantees the message IS that value, so masking
    removes it from every screen by construction. The pre-commit gitleaks hook
    does not cover it either: that hook scans staged FILES, and a message is
    not a file. `repair_agent_prose` reads it but only repairs mojibake and
    never refuses.

    That masking is CORRECT for its own question ("what does this EXECUTE?") --
    prose quoting `rm -rf /` must not be judged as running it. This floor
    answers the other question ("what does this PUBLISH?"), which needs the
    content the masker removes, so it reads the message directly rather than
    the command string.

    Reuses the canonical detector (output_guard.scan_text) exactly as
    access_gate._content_is_secret does at access_gate.py:756-761 --
    credential:* only. The broad sensitive:* env/ssh heuristics are excluded to
    keep false positives off honest prose, and injection-shaped prose is NOT
    refused here: fencing what an agent READS is a separate control, and
    refusing prose at write time is an operator policy call this floor
    deliberately leaves open.

    `persist=True` because this text is headed into a git-committed artifact --
    the exact condition _PERSIST_ONLY_PATTERNS is reserved for
    (output_guard.py:284-287), same rationale as scrub_persisted_text. It adds
    the AIDOCS daemon boot-token family, which is precisely what leaks into a
    debugging commit message.

    Fail-safe: any detector error returns None. This floor may never be the
    reason an honest commit cannot be made.
    """
    if not message or not message.strip():
        return None
    try:
        from .output_guard import scan_text

        findings = scan_text(message, redact=False, persist=True).findings
    except Exception:
        return None
    hits = sorted({f.detail for f in findings if f.category.startswith("credential:")})
    if not hits:
        return None
    return (
        "git_ops op=commit refused: the commit message carries credential "
        f"material ({', '.join(hits)}). A commit message is pushed to the "
        "remote and is permanent -- a key committed here cannot be "
        "unpublished, only rotated. Nothing else screens this text: the shell "
        "gate masks the -m value as data, and the gitleaks pre-commit hook "
        "scans staged files, not messages.\n"
        "Do instead:\n"
        "  * remove the credential from the message and commit again;\n"
        "  * if the key is real and was exposed, ROTATE it -- refusing this "
        "commit does not undo an earlier one."
    )


def _coerce_to_list(value: list[str] | str | None) -> list[str] | None:
    """Coerce a JSON-encoded string to a list, or pass through lists/None."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return value


# ── Tool timeout infrastructure ──────────────────────────────────────

_tool_executor = ThreadPoolExecutor(max_workers=4)


# Duplicate-call dedup moved to execution_index_store: content-addressed
# event_id / run_id collide on PK within a 2-second bucket, so duplicate
# writes from MCP wire doubling are dropped by ON CONFLICT DO NOTHING.
# The MCP wrapper no longer tries to detect duplicates itself — FastMCP
# middleware re-entry and sibling-task context semantics made that
# layer unreliable.


def _run_with_timeout(fn, timeout_seconds: int, *args, **kwargs) -> Any:
    """Run a sync function with a timeout. Returns result or raises TimeoutError.

    The ThreadPoolExecutor worker inherits no ContextVars from the
    submitting task — capture the caller's context with copy_context()
    and run the function inside it, so related_project wrappers'
    project-root override and any other CV-based routing is preserved.
    """
    import contextvars as _cv

    ctx = _cv.copy_context()
    future = _tool_executor.submit(ctx.run, fn, *args, **kwargs)
    # 0 (or any non-positive) means UNLIMITED — wait with no deadline.
    wait = timeout_seconds if (timeout_seconds and timeout_seconds > 0) else None
    try:
        return future.result(timeout=wait)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(
            f"Tool call timed out after {timeout_seconds}s. Use timeout= parameter for longer operations (0 = unlimited).",
        )


def _resolve_timeout(
    kwargs: dict,
    default: int | None = None,
    *,
    allow_caller_timeout: bool = False,
) -> int:
    """Extract and validate timeout from kwargs, falling back to category default.

    Returns the effective timeout in seconds, where **0 means UNLIMITED**
    (no watchdog). Semantics:
      - When ``allow_caller_timeout`` is False, or no caller ``timeout=``
        is supplied, the category ``default`` is used (which may itself be
        0 = unlimited if the operator configured it so).
      - A caller ``timeout=0`` explicitly requests unlimited.
      - A positive caller timeout is capped at the live ``tools.max_timeout``
        ceiling — unless that ceiling is itself 0 (unlimited), in which case
        the caller value passes through uncapped.
      - A negative caller timeout is invalid and falls back to the default.
    """
    timeout = kwargs.pop("timeout", None)
    try:
        fallback = int(default if default is not None else TOOLS_CALL_TIMEOUT)
    except (TypeError, ValueError):
        fallback = TOOLS_CALL_TIMEOUT
    fallback = max(0, fallback)  # 0 = unlimited; never negative
    if not allow_caller_timeout or timeout is None:
        return fallback
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return fallback
    if timeout < 0:
        return fallback
    if timeout == 0:
        return 0  # caller explicitly requests unlimited
    # Read the ceiling live so dashboard changes to tools.max_timeout
    # take effect without MCP restart. Fallback to the import-time
    # constant only if the config store is unreachable. A ceiling of 0
    # means "no ceiling" — the caller value passes through uncapped.
    max_ceiling = TOOLS_MAX_TIMEOUT
    try:
        from .config import get_setting
        from .mcp_server_runtime_helpers import (
            resolve_current_session_id,
            resolve_project_root,
        )

        try:
            root = resolve_project_root()
        except Exception:
            root = None
        live_max = get_setting(
            "tools.max_timeout",
            project_root=root,
            session_id=resolve_current_session_id(root) or None,
            default=TOOLS_MAX_TIMEOUT,
        )
        if live_max is not None:
            max_ceiling = int(live_max)
    except Exception:
        pass
    if max_ceiling and max_ceiling > 0:
        return min(timeout, max_ceiling)
    return timeout


def _live_config_timeout(config_key: str, fallback_timeout: int) -> int:
    """Read a timeout knob LIVE from the dashboard config (scope-cascaded).

    Called on every tool invocation so a dashboard edit takes effect on
    the next call with no MCP restart. Falls back to the schema-default
    integer only when the config store is unreachable (fail-open: a
    broken store must not un-bound or break tools).
    """
    try:
        from .config import get_setting
        from .mcp_server_runtime_helpers import (
            resolve_current_session_id,
            resolve_project_root,
        )

        try:
            root = resolve_project_root()
        except Exception:
            root = None
        value = get_setting(
            config_key,
            project_root=root,
            session_id=resolve_current_session_id(root) or None,
            default=fallback_timeout,
        )
        return int(value) if value is not None else fallback_timeout
    except Exception:
        return fallback_timeout


def _make_timed_decorator(
    config_key: str,
    fallback_timeout: int,
    *,
    allow_caller_timeout: bool = False,
):
    """Factory for timed tool decorators with a specific default timeout.

    Failures RAISE ToolError so the host UI renders a red/amber chip.
    Returning a dict with 'error' was green-success in the UI because
    the wire message said the call completed — the same bug we fixed
    for the edit tools. Surfacing timeouts + exceptions as real
    failures lets operators notice a tool is broken without needing
    to destructure every response.

    allow_caller_timeout=True passes the caller's `timeout=` kwarg
    through to `_resolve_timeout`, capped at TOOLS_MAX_TIMEOUT. Used
    by indexer tools where large monorepo scans can legitimately
    exceed the 30s default. Non-indexer tools leave it False so
    agents can't escape the dashboard-level timeout ceiling on
    other expensive-and-stuck calls.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            timeout = _resolve_timeout(
                kwargs,
                default=_live_config_timeout(config_key, fallback_timeout),
                allow_caller_timeout=allow_caller_timeout,
            )
            try:
                return _run_with_timeout(fn, timeout, *args, **kwargs)
            except TimeoutError as exc:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(f"Tool timed out after {timeout}s: {exc}")
            except Exception as exc:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(f"Tool failed: {exc}")

        return wrapper

    return decorator


# Category-specific decorators. Each reads its dashboard-configured
# timeout on every call via get_setting (scope-cascaded) — edit the
# value in the dashboard and the next tool call picks it up with no
# MCP restart. Fallback integers match the schema defaults; they
# only apply when the config store is unreachable.
# General tools: 10s default, caller `timeout=` is NOT honored — this
# preserves the dashboard-owned ceiling on arbitrary-and-stuck tools.
timed_tool = _make_timed_decorator("tools.tool_call_timeout", 10)
timed_sync = _make_timed_decorator("tools.sync_write_timeout", 60)
# Discovery tools (ai_find / ai_investigate / ai_bundle / ai_trace /
# ai_schema) advertise a `timeout` param and can legitimately run past
# the 10s default on a large repo — they HONOR a caller-supplied timeout=,
# capped at tools.max_timeout. Same default knob as general tools
# (tools.tool_call_timeout); only the caller-timeout policy differs.
timed_discovery = _make_timed_decorator(
    "tools.tool_call_timeout",
    10,
    allow_caller_timeout=True,
)
# Index-sync variant: its own dashboard knob (tools.index_sync_timeout,
# default 120s) — a full reindex of a large repo routinely exceeds the
# general sync default. Honors caller `timeout=` up to tools.max_timeout.
timed_indexer = _make_timed_decorator(
    "tools.index_sync_timeout",
    120,
    allow_caller_timeout=True,
)


_GIT_SAFE_DIR = ["-c", "safe.directory=*"]
_GIT_TIMEOUT = 10


def _make_timed_async_decorator(
    config_key: str,
    fallback_timeout: int,
    *,
    allow_caller_timeout: bool = False,
):
    """Factory for async timed tool decorators.

    Same semantics as the sync variant: the timeout knob is read LIVE
    from the dashboard config on every call (#338 — it was previously
    bound to an import-time constant, so dashboard edits were dead),
    and timeouts / failures raise ToolError so the host UI renders red.
    Dict-with-error returns used to render green-success — a silent
    failure mode.

    allow_caller_timeout=True honors a caller-supplied `timeout=`
    (capped at tools.max_timeout), matching the sync `timed_tool`.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            timeout = _resolve_timeout(
                kwargs,
                default=_live_config_timeout(config_key, fallback_timeout),
                allow_caller_timeout=allow_caller_timeout,
            )
            try:
                # 0 = unlimited: wait_for(None) waits with no deadline.
                _wait = timeout if (timeout and timeout > 0) else None
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=_wait)
            except TimeoutError:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(
                    f"Tool call timed out after {timeout}s. Pass timeout= for slower operations.",
                )
            except Exception as exc:
                from .mcp_server_runtime_helpers import _raise_tool_error

                _raise_tool_error(f"Tool failed: {exc}")

        return wrapper

    return decorator


# Timeout authority map (#338) — who bounds what:
#   sync general tools    → timed_tool       (tools.tool_call_timeout, 10s)
#   sync discovery tools  → timed_discovery  (tools.tool_call_timeout, caller timeout= honored)
#   sync write tools      → timed_sync       (tools.sync_write_timeout, 60s)
#   index sync            → timed_indexer    (tools.index_sync_timeout, caller timeout= honored)
#   async git tools       → timed_git_async  (tools.git_functions_timeout, 30s;
#                            caller timeout= honored TIGHTEN-ONLY, #340:
#                            min(caller, knob); 0/absent = knob)
#     (git_fork_status / git_upstream_changes / git_conflict_analysis /
#      git_merge_plan in server_legacy_git_tools.py — the only async tools)
# There are no async general tools; the former `timed_tool_async` and the
# sync `timed_git` were born unused in 6e9193cb and deleted in #338. Raw
# `git` subprocesses spawned via _run_git carry their own per-process
# deadline (_GIT_TIMEOUT, 10s) inside the tool-level ceiling above.
# Fallback = TOOLS_GIT_TIMEOUT (import-time schema default, 30) — used
# only when the config store is unreachable at call time.
_timed_git_async_base = _make_timed_async_decorator(
    "tools.git_functions_timeout",
    TOOLS_GIT_TIMEOUT,
    allow_caller_timeout=True,
)


def timed_git_async(fn):
    """Honor-BOUNDED caller timeout for the async git tools (#340).

    The 4 git tools advertise `timeout=`, but this decorator was built with
    allow_caller_timeout=False — `_resolve_timeout` popped the caller's
    value and silently dropped it. The API lied. Now the caller value is
    honored, tighten-only:

        effective = min(caller_value, live tools.git_functions_timeout knob)
        caller absent / 0 / invalid -> knob value (the pre-#340 behavior)

    Unlike the discovery/indexer decorators, `timeout=0` is NOT unlimited
    here — 0/absent falls back to the knob, so a caller can never weaken
    the #338 policy floor (only the dashboard knob can raise it).
    """
    inner = _timed_git_async_base(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        raw = kwargs.pop("timeout", None)
        try:
            raw = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            raw = None
        if raw is not None and raw > 0:
            knob = _live_config_timeout("tools.git_functions_timeout", TOOLS_GIT_TIMEOUT)
            # knob<=0 = category default is unlimited: the caller value
            # tightens from unlimited, so it passes through as-is.
            kwargs["timeout"] = min(raw, knob) if knob and knob > 0 else raw
        # raw absent/0/invalid: no timeout kwarg — `inner` resolves the
        # live knob default itself, exactly the pre-#340 path.
        return await inner(*args, **kwargs)

    return wrapper


_GIT_FAST_DIVERGENCE = 500
_GIT_SAMPLE_DIVERGENCE = 1500


# Reads that reveal CURRENT line numbers — enough to safely line-edit again after a
# prior edit shifted them. Discovery-only tools (ai_find/ai_text_search) are excluded:
# they surface match locations, not the file's live line layout.
_RELOCK_READ_TOOLS = frozenset(
    {"ai_get_lines", "ai_bundle", "ai_get_symbol_snippet", "ai_get_outline"}
)


def _release_turn_edit_lock(
    hub: AidocsServiceHub,
    project_root: Path,
    path: str,
) -> None:
    """Release ONLY the per-turn line-edit lock for a freshly re-read file.

    #476 attempt-32 split: releasing the relock and MINTING a
    known_exact_paths grant are different authorities. ai_get_lines under
    the read gate must release the lock (the refusal prescribes exactly
    that re-read) WITHOUT widening the session's granted-path set — the
    lane-context pin (test_query_gate_ux) proves a lane-owned read must
    leave known_exact_paths untouched. Discovery tools still grant via
    _grant_known_exact_path_read, which calls this for _RELOCK_READ_TOOLS.
    """
    session_id = resolve_managed_session(hub.managed_mode, project_root)
    if not session_id:
        return
    try:
        hub.query_gate.remove_turn_edited_file(
            project_root, str(session_id), str(path).replace("\\", "/").strip()
        )
    except Exception:
        pass


def _grant_known_exact_path_read(
    hub: AidocsServiceHub,
    project_root: Path,
    tool_name: str,
    path: str,
) -> None:
    """Grant per-file read access via AccessGate."""
    from .access_gate import AccessGate

    session_id = resolve_managed_session(hub.managed_mode, project_root)
    if not session_id:
        return
    AccessGate.grant_discovery(hub.query_gate, project_root, str(session_id), tool_name, [path])
    # Re-read unlock (2026-06-17): a fresh READ of the file via an investigation tool
    # gives the agent current line numbers, so the per-turn line-edit lock can release
    # for THIS file — the next line-edit is safe again (no need to read the whole file;
    # a targeted ai_get_lines of the edit region is enough). Only READ tools release it;
    # an edit/create that grants the path must NOT (that's what set the lock).
    if tool_name in _RELOCK_READ_TOOLS:
        _release_turn_edit_lock(hub, project_root, path)


def _evict_known_exact_path(hub: AidocsServiceHub, project_root: Path, path: str) -> None:
    """Remove a path from session known_exact_paths.

    Phoenix 2026-05-12 (Empire directive): used after line-based edits
    (ai_replace mode=lines / mode=symbol / ai_insert_lines) where the
    file's line numbers shift drastically. Forces the agent to re-read
    before the next line operation — re-reads re-grant the path with
    fresh content. Counterpart to _grant_known_exact_path_read.
    """
    session_id = resolve_managed_session(hub.managed_mode, project_root)
    if not session_id:
        return
    try:
        state = hub.query_gate.get(project_root, str(session_id)) or {}
        known = [p for p in (state.get("known_exact_paths") or []) if p != path]
        hub.query_gate.set(
            project_root,
            str(session_id),
            known_exact_paths=known,
        )
        # #474 tranche 2 (War Y): eviction is LINE-FRESHNESS hygiene, not
        # discovery revocation — the session provably knows this file (it
        # just edited it). Record it in the per-session surfaced ledger so
        # the re-read the eviction prescribes is ADMITTED instead of
        # bouncing through a redundant re-discovery round-trip. The
        # turn-edit lock still blocks the next line-EDIT until that fresh
        # re-read happens. Lane contexts are excluded (lane discovery must
        # not become durable session discovery).
        if not state.get("current_lane_id"):
            try:
                from .session_response_ledger import record_surfaced_files

                record_surfaced_files(
                    project_root,
                    str(session_id),
                    [str(path).replace("\\", "/").strip()],
                )
            except Exception:
                pass
    except Exception:
        # Best-effort — a failed eviction leaves the path granted (no
        # worse than pre-fix state); next edit still works, just without
        # the re-read discipline this time.
        pass


def _post_edit_reindex_and_grant(
    hub: AidocsServiceHub,
    project_root: Path,
    tool_name: str,
    path: str,
    evict_known_path: bool = False,
) -> dict[str, Any]:
    """After a successful edit: grant read access + reindex + invalidate config caches if needed.

    Returns a structured status so successful writes cannot silently bypass indexing.

    Empire directive 2026-05-12: line-based edits (ai_replace mode=lines,
    mode=symbol, plus ai_insert_lines) shift the file's line numbers
    drastically. The agent's cached known_exact_path is stale the moment
    the edit lands. Pass evict_known_path=True from those call sites —
    the path is REMOVED from known_exact_paths after the edit, forcing
    the agent to re-read (ai_get_lines / ai_bundle) before the next
    line-based operation. Re-reads re-grant the path with the fresh
    content. This is what makes line-edits safe to expose without
    granted-only gating.
    """
    if evict_known_path:
        _evict_known_exact_path(hub, project_root, path)
    else:
        _grant_known_exact_path_read(hub, project_root, tool_name, path)
    canonical = path.replace("\\", "/").strip()
    # Folder-sitter marker (2026-04-21): stamp this write so the
    # filesystem watcher (when enabled via observability.watch_user_drops)
    # doesn't trigger a redundant re-index. We just re-indexed below.
    try:
        from .folder_sitter import mark_self_write

        try:
            _mtime_ns = (project_root / canonical).stat().st_mtime_ns
        except Exception:
            _mtime_ns = None
        mark_self_write(canonical, mtime_ns=_mtime_ns)
    except Exception:
        pass
    # #76 (2026-04-27): only verify index refresh for extensions the
    # code indexer actually tracks. Markdown, SQL, plain text, JSON
    # blobs, etc. are written to disk but the indexer doesn't extract
    # symbols from them — `synced=0` is the correct outcome there,
    # not a failure. Pre-fix this surfaced a red error chip on every
    # .md / .sql edit that confused agents and operators.
    _INDEXED_EXTS = {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".cs",
        ".cshtml",
        ".go",
        ".rs",
        ".java",
        ".dart",
        ".swift",
        ".kt",
        ".scala",
        ".rb",
        ".php",
    }
    from pathlib import Path as _PathExt

    ext = _PathExt(canonical).suffix.lower()
    if ext in _INDEXED_EXTS:
        try:
            # Reindex for its side-effect (incremental sync). synced==0 is NOT a
            # create/edit failure: the write SUCCEEDED, and an indexed-ext file
            # may legitimately have no extractable symbols — a default-only ESM
            # module, an empty/trivial file. ONLY a sync EXCEPTION (below) is a
            # real indexing failure. (#193, caught by the webmcp smoke harness:
            # a valid .mjs `export default {…}` produced 0 rows and ai_create_file
            # was wrongly refused with "post-edit index refresh produced no rows".)
            # #224: incremental=True takes the scoped fast-path — reindex ONLY
            # this edited/created file instead of walking the whole tree
            # (~12s → ~50ms per edit on a 2200-file project).
            hub.code.sync_code_files(project_root, paths=[canonical], incremental=True)
        except Exception as exc:
            return {
                "ok": False,
                "path": canonical,
                "error": f"post-edit index refresh failed for `{canonical}`: {exc}",
            }
    # Invalidate config caches when config files are edited
    if canonical.endswith(".toml") or canonical.endswith("workflow-actions.json"):
        try:
            from .config import reload_config_caches

            reload_config_caches()
        except Exception:
            pass
    # ── #448 Consumer C: blast-radius tracing (mandatory awareness) ──
    # Emperor 2026-07-18: before/when acting on a code target the agent
    # is HANDED its blast radius — the reverse-dependency closure over
    # code_edges (import ∪ semantic_ref, the owned stores the §XXXII
    # LSP joint materializes into; the guest never runs inline here).
    # Performance-bounded (depth cap + memoized per file+mtime) and
    # fail-quiet: an absent/stale index degrades to no radius, never a
    # failed edit. The radius rides (a) the tool_edit_completed audit
    # payload below and (b) the edit response rail via the one-shot
    # stash drained by tool_display.edit_result.
    _blast_radius = None
    try:
        from .semantic_enrichment import (
            attach_anchored_memories,
            blast_radius_for_file,
            stash_radius_note,
        )

        _blast_radius = blast_radius_for_file(project_root, canonical)
        # #375 Phase 3 (B): editing a leaf surfaces its ANCHORED MEMORIES
        # on the same rail — bounded, fail-quiet, advisory (the blocking
        # surface stays edit_memory_gate's).
        _blast_radius = attach_anchored_memories(
            project_root, canonical, _blast_radius
        )
        stash_radius_note(_blast_radius)
    except Exception:
        _blast_radius = None
    # Audit gap fill (2026-04-21): stamp a tool_edit_completed event
    # cross-referencing the most recent edit_history row for this path.
    # Lets audit queries join tool calls to the actual diff bytes
    # without a timestamp-fuzzy-match. Best-effort.
    try:
        from .edit_history import EditHistoryStore

        recent = EditHistoryStore().list_edits(
            project_root,
            file_path=canonical,
            limit=1,
        )
        edit_id = None
        if recent:
            edit_id = (
                recent[0].get("edit_id")
                if isinstance(recent[0], dict)
                else getattr(recent[0], "edit_id", None)
            )
        session_id_audit = resolve_managed_session(hub.managed_mode, project_root) or None
        hub.execution.record_event(
            project_root,
            event_kind="tool_edit_completed",
            source_kind="mcp_edit_tool",
            session_id=session_id_audit,
            capability_name=tool_name,
            action_kind="edit",
            target_entity=canonical,
            status="applied",
            payload={
                "tool_name": tool_name,
                "path": canonical,
                "edit_history_id": edit_id,
                # #448 Consumer C: reverse-dependency radius of the edited
                # file (None when the index is unavailable — fail-quiet).
                "blast_radius": _blast_radius,
            },
        )
    except Exception:
        pass
    out: dict[str, Any] = {"ok": True, "path": canonical}
    if _blast_radius is not None:
        out["blast_radius"] = _blast_radius
    return out


def _require_indexed_read_gate(
    hub: AidocsServiceHub,
    project_root: Path,
    exact_path: str | None = None,
    known_exact_path: bool = False,
) -> dict[str, Any] | None:
    """Check read gate via AccessGate — blocks indexed files from raw read.

    All indexed files are blocked from raw read. Agents must use indexed
    tools (ai_get_lines, ai_find, etc.). This is the primary read gate
    replacing the deprecated known_exact_path bypass.
    """
    from .access_gate import AccessGate, GateContext

    session_id = resolve_managed_session(hub.managed_mode, project_root)
    if not session_id:
        return None

    state = hub.query_gate.get(project_root, str(session_id))

    # #474 tranche 2 (War Y): attach the per-SESSION surfaced-files
    # ledger as a discovery fallback. The per-task known_exact_paths
    # stays primary; the ledger can only ADMIT past the two discovery
    # refusal points inside check_read (never sensitive paths, never
    # protected config, inert in lane contexts). Fail-closed: any
    # ledger failure leaves the key absent → exactly today's behavior.
    try:
        from .session_response_ledger import surfaced_files

        _surfaced = surfaced_files(project_root, str(session_id))
        # The turn-edited set rides along so _ledger_admits can honor the
        # king directive: a file line-edited THIS turn is never ledger-
        # admitted (general get() omits the column by design).
        _turn_edited = hub.query_gate.get_turn_edited_files(project_root, str(session_id))
        state = dict(state) if isinstance(state, dict) else {}
        state["session_surfaced_paths"] = _surfaced
        state["turn_edited_files"] = list(_turn_edited or [])
    except Exception:
        pass

    # Check if target is indexed
    is_indexed = False
    if exact_path:
        target = str(exact_path).replace("\\", "/").lstrip("/")
        try:
            is_indexed = hub.code._is_indexed_file(project_root, target)
        except Exception:
            is_indexed = False

    decision = AccessGate.check_read(
        GateContext(
            managed=True,
            session_id=str(session_id),
            dev_mode=False,
            allow_config_edit=False,
            gate_enforce=True,
            gate_state=state,
        ),
        exact_path or "",
        known_exact_path=known_exact_path,
        is_indexed=is_indexed,
    )
    if decision.allowed:
        # War FF (#474 tranche 2, half (c)): a known_exact_path=true read
        # the gate admits at the per-file read_gate level IS a surfacing —
        # record it to the session ledger so the SECOND read never needs
        # the flag. War Y invariants verbatim: admit-only (widens reads
        # only, never edits), lane-inert (level=="read_gate" is reached
        # only outside a lane context and we re-check current_lane_id),
        # sensitive-supremacy (a sensitive path never reaches an allowed
        # read_gate decision — check_read refuses it earlier), fail-closed
        # (any ledger failure records nothing → today's stricter flow).
        if (
            known_exact_path
            and exact_path
            and decision.level == "read_gate"
            and isinstance(state, dict)
            and not state.get("current_lane_id")
        ):
            try:
                from .session_response_ledger import record_surfaced_files

                _surf_target = str(exact_path).replace("\\", "/").lstrip("/")
                if (project_root / _surf_target).is_file():
                    record_surfaced_files(project_root, str(session_id), [_surf_target])
            except Exception:
                pass
        return None
    reason = str(decision.reason or render_interaction_text("interaction.errors.indexed_read_gate"))
    return {
        "enforcement": {
            "stage": "gate",
            "decision": "block",
            "reason": reason,
            "user_message": reason,
        },
        "error": reason,
    }


def _apply_trace_depth(payload: dict[str, Any], mode: str, max_depth: int | None) -> dict[str, Any]:
    if not max_depth or max_depth <= 0:
        return payload
    m = mode.strip().lower()
    if m in {"service", "component", "field_flow", "setting"} and isinstance(
        payload.get("matches"),
        list,
    ):
        order = {"definition": 1, "reference": 2, "file_match": 3}
        result = dict(payload)
        result["matches"] = [
            item
            for item in payload["matches"]
            if order.get(str(item.get("source")), 3) <= max_depth
        ]
        return result
    if m == "api_to_ui":
        result = dict(payload)
        if max_depth == 1:
            result["logic"] = []
            result["ui"] = []
        elif max_depth == 2:
            result["ui"] = []
        return result
    return payload


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_EVENT_KIND_TO_PHASE: dict[str, str] = {
    "tool_call_started": "started",
    "tool_call_completed": "completed",
    "tool_call_failed": "failed",
}


def _runner_receipt_fields(result: object) -> dict[str, object]:
    """Derive audit-receipt fields ({command, exit_code}) from a tool RESULT.

    #324: the verification/evidence gate harvests `command` + `exit_code`
    from tool_call_completed event payloads. ai_run stamps `command` from
    its ARGUMENT; but ai_test — the governed runner agents are DIRECTED to
    use — resolves its command internally (framework + argv), so its runs
    never reached the evidence gate and task_complete blocked with
    "evidence.commands_run is empty" even though the runner audited the run.
    This helper turns a governed-runner result (framework/args/rc) into the
    same receipt shape, so runner receipts satisfy the gate automatically.

    Shapes handled (all best-effort, never raises):
      * {"exit_code": N}                      → exit_code (ai_run family)
      * {"rc": N}                             → exit_code (ai_test verdict)
      * {"done": True, "ok": bool}            → 0/1 (inline-finish path)
      * ToolResult.structured_content         → same rules on the dict
      * framework + args + a COMPLETED verdict → command "pytest … -n 2"
        (a detached/still-running result has no rc and mints NO command —
        an unfinished suite is not evidence).
    """
    fields: dict[str, object] = {}
    try:
        data: dict | None
        if isinstance(result, dict):
            data = result
        else:
            structured = getattr(result, "structured_content", None)
            data = structured if isinstance(structured, dict) else None
        if data is None:
            return fields
        exit_candidate = None
        if "exit_code" in data:
            exit_candidate = data.get("exit_code")
        elif "rc" in data:
            exit_candidate = data.get("rc")
        elif data.get("done") is True and "ok" in data:
            exit_candidate = 0 if data.get("ok") else 1
        if exit_candidate is not None:
            try:
                fields["exit_code"] = int(exit_candidate)
            except (TypeError, ValueError):
                pass
        fw = data.get("framework")
        args = data.get("args")
        if (
            "exit_code" in fields
            and isinstance(fw, str)
            and fw
            and isinstance(args, list)
            and args
        ):
            command = " ".join(str(a) for a in args).strip()
            if command.startswith("-m "):
                command = command[3:].strip()
            if command:
                fields["command"] = command[:500]
    except Exception:
        return fields
    return fields


def _guard_stamp_slot(result: object) -> dict | None:
    """The dict on a tool RESULT that agent-visible stamps go into, or None.

    Same shape resolution as ``_runner_receipt_fields`` / ``index_staleness.
    stamp_tool_result``: a plain dict result is its own slot; a FastMCP
    ``ToolResult`` envelope carries one at ``.structured_content``.
    """
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structured_content", None)
    return structured if isinstance(structured, dict) else None


def _scan_and_mark_tool_result(result: object) -> tuple[object, str]:
    """Run the output guard over a tool result and MARK the result itself.

    #648/#651: injection-category findings used to be AUDIT-ONLY — they went to
    ``payload_summary`` and the tool_call_log while the agent got the content
    untouched and unlabelled. The scan already knew; the reader did not. This
    stamps the guard's verdict where the agent can see it:

      * injection findings -> ``scan_status="findings"`` + ``content_warning``
        (the ``<aidocs-content-warning>`` banner naming the content DATA /
        NON-AUTHORITATIVE). The payload is NOT altered and NOT withheld.
      * guard disabled, scan raised, or ``scanned=False`` -> ``scan_status=
        "unknown"`` + the unknown banner. UNKNOWN IS NEVER CLEAN, and silence
        is never an option — but it is never a refusal either.
      * scanned and injection-free -> nothing stamped (``clean``), so the mark
        stays a signal instead of noise on every read.

    ANNOTATE-AND-PROCEED is deliberate, twice over. (1) This repo stores
    injection strings AS DATA — the scanner's own pattern table, the red-team
    corpus, the security tests — so a read of them must still return them,
    merely marked; a false positive that BLOCKED would hard-stop every reader,
    operator included. (2) Failing closed at this chokepoint would recreate the
    #634 deadlock class. Credential redaction is unchanged: it still happens
    inside ``scan_tool_result``, in place, and still fail-closes where it did.

    NO CALLER-CHOSEN BYPASS (#615): the only parameter is the result. Policy is
    read from operator CONFIG here, not passed in — if the caller could choose
    whether the check applies, the check would not exist. Config may switch the
    scan off; it cannot buy silence, because off is stamped ``unknown``.

    LIMIT, stated honestly: only a dict slot is stamped. A result with no dict
    (no ``structured_content``) keeps its payload unchanged and is AUDIT-ONLY —
    the guard verdict is still returned to the chokepoint and still logged, but
    that agent sees no banner. The already-serialized ``.content`` text blocks
    are likewise not rewritten (the same limitation the central freshness stamp
    carries). Returns ``(GuardResult, scan_status)``.
    """
    from . import output_guard as _og
    from .config import OUTPUT_GUARD_ENABLED, OUTPUT_GUARD_REDACT

    guard_result: object
    status: str
    if not OUTPUT_GUARD_ENABLED:
        guard_result, status = _og.GuardResult(scanned=False), _og.SCAN_STATUS_UNKNOWN
    else:
        try:
            guard_result = _og.scan_tool_result(result, redact=OUTPUT_GUARD_REDACT)
        except Exception:  # noqa: BLE001 — a broken guard must not eat the call
            guard_result = _og.GuardResult(scanned=False)
        if not getattr(guard_result, "scanned", False):
            status = _og.SCAN_STATUS_UNKNOWN
        elif _og.injection_findings(guard_result):
            status = _og.SCAN_STATUS_FINDINGS
        else:
            status = _og.SCAN_STATUS_CLEAN

    if status != _og.SCAN_STATUS_CLEAN:
        slot = _guard_stamp_slot(result)
        if slot is not None:
            try:
                slot["scan_status"] = status
                slot["content_warning"] = _og.format_content_provenance_notice(
                    _og.injection_findings(guard_result),
                    status,
                )
            except Exception:  # noqa: BLE001 — marking never breaks a result
                pass
    return guard_result, status


def _record_tool_execution_state(
    hub: AidocsServiceHub,
    project_root: Path,
    *,
    run_id: str,
    capability_name: str,
    session_id: str | None,
    status: str,
    event_kind: str,
    metadata: dict[str, object],
    completed_at: str | None = None,
) -> None:
    # Thin wrapper around tool_call_log.record_run + record. The helper
    # hides the phase → event_kind vocabulary so dashboards keep seeing
    # the same wire format while writers converge on one interface.
    from .tool_call_log import record as _log_record
    from .tool_call_log import record_run as _log_run

    _log_run(
        hub,
        project_root,
        name=capability_name,
        session_id=session_id,
        status=status,
        metadata=metadata,
        run_id=run_id,
        completed_at=completed_at,
    )
    phase = _EVENT_KIND_TO_PHASE.get(event_kind, event_kind)
    _log_record(
        hub,
        project_root,
        phase=phase,
        name=capability_name,
        payload=metadata,
        session_id=session_id,
        action_kind="mcp_tool_call",
        status=status,
        run_id=run_id,
    )


# ── #222: sovereign-content tools ────────────────────────────────────
# A soul (sovereign continuity scroll) is private to its seat. The ACT of
# touching one is fully audited (empire_soul_gate.record_soul_act: who /
# when / soul_id / operation / grant_id / outcome) — the SCROLL is not.
#
# That floor held only for the soul_act row. The same call also flows
# through this generic instrumentation, whose args/result previews would
# otherwise copy the scroll body into `execution_events` and render it
# verbatim in the operator-facing execution dashboard. Tools listed here
# have their previews withheld: the act stays auditable (tool_name, mode,
# session, status, byte counts), the body never enters the row.
_SOVEREIGN_CONTENT_TOOLS = frozenset({"ai_soul"})
_SOVEREIGN_PREVIEW_WITHHELD = "[sovereign content withheld: audited by act, never by body]"


def _tool_result_preview(result: Any, tool_name: str) -> tuple[int, str]:
    result_bytes = 0
    result_text_preview = ""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                result_bytes += len(text.encode("utf-8"))
                text_parts.append(text)
        full_text = "\n".join(text_parts)
        result_text_preview = full_text[:1000] if len(full_text) > 1000 else full_text
    # Byte accounting is kept (it carries no scroll text); only the body goes.
    if str(tool_name or "") in _SOVEREIGN_CONTENT_TOOLS:
        return result_bytes, _SOVEREIGN_PREVIEW_WITHHELD
    return result_bytes, result_text_preview


def _tool_args_preview(args_str: str, tool_name: str) -> str:
    """Argument preview for the tool-call audit row. Sovereign-content
    tools carry the scroll body in their arguments (mode='append' /
    'rewrite' / 'create'), so their preview is withheld entirely."""
    if str(tool_name or "") in _SOVEREIGN_CONTENT_TOOLS:
        return _SOVEREIGN_PREVIEW_WITHHELD
    return args_str[:500] if len(args_str) > 500 else args_str


async def _run_git(cwd: str, *args: str, timeout: int = _GIT_TIMEOUT) -> str:
    """Run a git command from inside an async context by offloading to a thread."""
    import asyncio
    from functools import partial

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_run_git_sync, cwd, *args, timeout=timeout))


def _resolve_templates_root() -> Path | None:
    """Locate the bundled session-templates dir (must contain context.md).

    CONVERGED 2026-07-30 (#628): delegates to the single canonical resolver
    in ``host_services.path_resolver_service`` — one probe order for every
    host (env AIDOCS_PATH → package walk-up → source checkout → project
    tree), gated on ``context.md``.

    Returns ``None`` when no real tree exists. It used to FABRICATE
    ``parents[3]/core/.MEMORY/.aidocs/templates`` in that case, which in an
    installed layout (``<runtime>/venv/Lib/site-packages/aidocs_mcp/``)
    resolves to ``<venv>/core/…`` — a path nothing lives at, surfacing far
    away as a bare ENOENT that killed every session create machine-wide.
    Construction-time consumers accept ``None``; the one template READ
    re-resolves with the project root via
    ``path_resolver_service.require_context_template``.
    """
    from .host_services.path_resolver_service import find_templates_root

    # __file__ is read through the module global so tests can relocate this
    # package into a synthetic layout (repo / gate release / installed venv).
    return find_templates_root(package_file=__file__)


def _resolve_script_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "core" / "scripts"


def _session_summary_to_dict(summary: Any) -> dict[str, Any]:
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "status": summary.status,
        "owner": summary.owner,
        "last_updated": summary.last_updated,
    }


def _build_skill_mode_metadata(
    state: dict[str, Any] | None,
    override_store: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    triggered = state.get("triggered")
    selected_skills = [str(item) for item in state.get("selected_skills", []) if str(item).strip()]
    active_skills = [str(item) for item in state.get("active_skills", []) if str(item).strip()]
    provider_states = (
        state.get("provider_states") if isinstance(state.get("provider_states"), dict) else {}
    )
    active_skill_modes: dict[str, str] = {}
    selected_skill_modes: dict[str, str] = {}
    decisions: list[dict[str, Any]] = []
    for item in triggered if isinstance(triggered, list) else []:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        override_mode = str(item.get("override_mode") or "").strip()
        runtime_owned_capability = (
            item.get("runtime_owned_capability")
            if isinstance(item.get("runtime_owned_capability"), dict)
            else None
        )
        provider = str(item.get("provider") or "").strip()
        runtime_provider = str(item.get("runtime_provider") or provider).strip() or provider
        if not skill_id or not override_mode:
            continue
        selected_skill_id = _match_selected_skill_id_for_trigger(
            selected_skills=selected_skills,
            skill_id=skill_id,
            provider=provider,
            runtime_provider=runtime_provider,
            provider_states=provider_states,
            override_store=override_store,
        )
        if runtime_owned_capability is None:
            active_skill_modes[skill_id] = override_mode
        if selected_skill_id:
            selected_skill_modes[selected_skill_id] = override_mode
        decisions.append(
            {
                "skill_id": skill_id,
                "selected_skill_id": selected_skill_id,
                "override_mode": override_mode,
                "provider": item.get("provider"),
                "runtime_provider": item.get("runtime_provider"),
                "runtime_owned_capability": runtime_owned_capability,
            },
        )
    for selected_skill_id in selected_skills:
        if selected_skill_id in selected_skill_modes:
            continue
        if "/" in selected_skill_id:
            provider_id, selected_name = selected_skill_id.split("/", 1)
        elif BUNDLED_PROVIDER_ID in provider_states:
            provider_id, selected_name = (
                _BUNDLED_OVERRIDE_PROVIDER_ID,
                selected_skill_id,
            )
        else:
            continue
        override_mode = None
        resolved_skill_id = selected_skill_id
        provider = provider_id
        runtime_provider = provider_id
        if override_store is not None:
            decision = override_store.resolve(provider_id, selected_name)
            override_mode = str(decision.mode or "").strip()
            runtime_owned_capability = None
            if override_mode in _RUNTIME_OWNED_OVERRIDE_MODES:
                resolved_skill_id = str(decision.skill_id or selected_name)
                runtime_provider = "aidocs_runtime"
                runtime_owned_capability = {
                    "capability_id": str(decision.runtime_capability_id or "").strip(),
                    "source": "aidocs_runtime",
                    "reason": str(decision.reason or "").strip(),
                    "mode": override_mode,
                    "selected_skill_id": selected_skill_id,
                    "provider": provider_id,
                }
            elif override_mode == "provider_content_aidocs_runtime":
                resolved_skill_id = selected_skill_id
                runtime_provider = "aidocs"
        elif selected_name in active_skills and selected_skill_id not in active_skills:
            override_mode = "aidocs_runtime_owned"
            resolved_skill_id = selected_name
            runtime_provider = "aidocs_runtime"
            runtime_owned_capability = {
                "capability_id": selected_name,
                "source": "aidocs_runtime",
                "reason": "runtime-owned workflow authority",
                "mode": override_mode,
                "selected_skill_id": selected_skill_id,
                "provider": provider_id,
            }
        else:
            runtime_owned_capability = None
        if not override_mode:
            continue
        if (
            runtime_owned_capability is None
            and resolved_skill_id not in active_skills
            and selected_skill_id not in active_skills
        ):
            continue
        selected_skill_modes[selected_skill_id] = override_mode
        if runtime_owned_capability is None:
            active_skill_modes[resolved_skill_id] = override_mode
        decisions.append(
            {
                "skill_id": resolved_skill_id,
                "selected_skill_id": selected_skill_id,
                "override_mode": override_mode,
                "provider": provider,
                "runtime_provider": runtime_provider,
                "runtime_owned_capability": runtime_owned_capability,
            },
        )
    if not active_skill_modes and not selected_skill_modes:
        return None
    return {
        "active_skill_modes": active_skill_modes,
        "selected_skill_modes": selected_skill_modes,
        "decisions": decisions,
    }


def _annotate_imported_skill_state(imported_skill_state: Any, override_store: Any = None) -> Any:
    if not isinstance(imported_skill_state, dict):
        return imported_skill_state
    mode_metadata = _build_skill_mode_metadata(imported_skill_state, override_store=override_store)
    if mode_metadata is None:
        return imported_skill_state
    return {
        **imported_skill_state,
        "mode_metadata": mode_metadata,
    }


def _annotate_skill_result(payload: dict[str, Any], override_store: Any = None) -> dict[str, Any]:
    result = dict(payload)
    mode_metadata = _build_skill_mode_metadata(result, override_store=override_store)
    if mode_metadata is not None:
        result["override_modes"] = dict(mode_metadata["active_skill_modes"])
    imported_skill_state = result.get("imported_skill_state")
    if isinstance(imported_skill_state, dict):
        result["imported_skill_state"] = _annotate_imported_skill_state(
            imported_skill_state,
            override_store=override_store,
        )
        if "override_modes" not in result and isinstance(result["imported_skill_state"], dict):
            imported_mode_metadata = result["imported_skill_state"].get("mode_metadata")
            if isinstance(imported_mode_metadata, dict):
                result["override_modes"] = dict(
                    imported_mode_metadata.get("active_skill_modes") or {},
                )
    return result


def _build_server_instructions() -> str:
    """Load MCP server instructions from gate_messages TOML config."""
    return render_interaction_text("interaction.mcp_server.instructions")


# Claude CLI's long-lived programmatic chat mode requires stream-json on both
# pipes. Mirrors apps/aidocs-dashboard/src-tauri/src/main.rs (conductor_start,
# conductor_send, handle_claude_stream_event) so the Rust dashboard and this
# Python MCP path stay wire-compatible.


def _claude_identity_prompt(project_root: Path, session_id: str) -> str:
    """Build the --append-system-prompt identity string for the Claude conductor."""
    project_name = project_root.name or "project"
    root_display = str(project_root)
    return (
        f"You are the AIDOCS conductor for project '{project_name}' at "
        f"'{root_display}'. The user is working in AIDOCS session "
        f"'{session_id}'. Use mcp__aidocs__ai_session(mode='list') to enumerate sessions "
        f"and mcp__aidocs__ai_session(mode='connect') to bind to one; never glob "
        f"/.MEMORY/sessions/. When the user asks about the current project or "
        f"session, answer with the identity above."
    )


def _claude_stream_json_user_envelope(text: str) -> str:
    """Serialize a user message for Claude's --input-format stream-json."""
    envelope = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    # Newline terminator is required — CLI reads one JSON object per line.
    return json.dumps(envelope, ensure_ascii=False) + "\n"


def _claude_build_cli_args(cli_path: str, identity_prompt: str, model: str = "") -> list[str]:
    """Build Claude CLI argv for long-lived stream-json chat.

    --verbose is mandatory whenever --output-format stream-json is set.
    """
    args = [
        cli_path,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--append-system-prompt",
        identity_prompt,
    ]
    if model and model.strip():
        args.extend(["--model", model.strip()])
    return args


def _parse_claude_stream_event(line: str) -> list[dict[str, str]]:
    """Convert a stream-json stdout line into conductor output entries.

    Non-JSON lines are surfaced on stderr so CLI banners or migration warnings
    are not silently swallowed.
    """
    stripped = line.strip()
    if not stripped:
        return []
    try:
        envelope = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return [{"stream": "stderr", "text": f"[non-json stdout] {stripped}"}]

    if not isinstance(envelope, dict):
        return []

    kind = envelope.get("type")
    if kind == "assistant":
        message = envelope.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        entries: list[dict[str, str]] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        entries.append({"stream": "stdout", "text": text})
        return entries
    if kind == "result":
        # Only surface result envelopes that carry an error — success results
        # would duplicate assistant text already emitted.
        if envelope.get("is_error") is True:
            text = envelope.get("result")
            if isinstance(text, str) and text:
                return [{"stream": "stderr", "text": text}]
        return []
    return []


def _extract_claude_session_id(line: str) -> str | None:
    """Return claude_session_id from a system/init envelope so resumes can pin it."""
    try:
        envelope = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("type") != "system":
        return None
    if envelope.get("subtype") != "init":
        return None
    sid = envelope.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _start_conductor_output_reader(
    proc: Any,
    output_buf: list,
    lock: Any,
    max_lines: int,
    backend: str = "",
    state: dict[str, Any] | None = None,
) -> None:
    """Background reader thread. Lifted to module scope so tests can exercise it
    without spinning up the full FastMCP server.
    """
    import threading
    import time as _time

    def _append(entry: dict[str, Any]) -> None:
        with lock:
            output_buf.append(entry)
            if len(output_buf) > max_lines:
                del output_buf[: len(output_buf) - max_lines]

    def _reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                if backend == "claude":
                    # Init subtype carries claude_session_id; capturing it here
                    # avoids a second pass over the envelope in the parser.
                    sid = _extract_claude_session_id(line)
                    if sid and state is not None:
                        state["claude_session_id"] = sid
                    for produced in _parse_claude_stream_event(line):
                        _append(
                            {
                                "text": produced["text"],
                                "timestamp": _time.time(),
                                "stream": produced["stream"],
                            },
                        )
                else:
                    _append(
                        {
                            "text": line.rstrip("\n\r"),
                            "timestamp": _time.time(),
                            "stream": "stdout",
                        },
                    )
        except (ValueError, OSError):
            # Pipe close on shutdown is normal — surfacing it would flood the
            # buffer with one noise entry per conductor_stop.
            pass

    def _err_reader():
        try:
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                _append(
                    {
                        "text": line.rstrip("\n\r"),
                        "timestamp": _time.time(),
                        "stream": "stderr",
                    },
                )
        except (ValueError, OSError):
            pass

    t1 = threading.Thread(target=_reader, daemon=True)
    t2 = threading.Thread(target=_err_reader, daemon=True)
    t1.start()
    t2.start()


_conductor_process: dict[str, Any] = {}
_conductor_output: list[dict[str, Any]] = []


def reconcile_conductor_actors(project_root=None) -> dict:
    """THE ONE HOME for destructive conductor-actor cleanup (#982).

    Operator ruling 2026-08-30: "Put cleanup in one explicit
    lifecycle/reconciliation home that owns actor liveness. Boot may invoke that
    reconciler if appropriate, but 'server object was constructed' is not the
    trigger and daemon PID is not the evidence."

    THE ONLY DEATH EVIDENCE IS THE LEASE/LIVENESS ORACLE — evidence about the
    ACTOR:
        lease True                         -> LIVE, kept
        lease False, session proven        -> DEAD/phantom, pruned
        unreadable / unknown / no evidence -> UNPROVABLE, KEPT

    `bound_by_boot_token` is FORENSIC PROVENANCE ONLY. The pid in it is the MCP
    server's own, so it records WHICH DAEMON WROTE THE ROW and can never be
    evidence that an ACTOR died. The rung that used to read it — and the
    separate pid-residual sweep that consumed the same verdict — are both gone.

    Never raises: reconciliation is hygiene and must not block a boot.
    """
    out: dict = {"performed": False}
    try:
        from .mcp_server_runtime_helpers import resolve_project_root
        from .window_lease import conductor_liveness_oracle

        root = project_root if project_root is not None else resolve_project_root()
        from .managed_mode_service import ManagedModeService

        store = ManagedModeService()._store
        result = store.prune_phantom_conductor_bindings(
            root,
            is_live=conductor_liveness_oracle(root),
        )
        # #880 lease lifecycle: reap windows that are PROVABLY gone — AFTER the
        # prune, not before, and the order is the point. The prune's oracle
        # reads this table, so reaping first would delete a row and then judge a
        # binding by its absence inside the same pass. Running it after means
        # the reap only affects the NEXT reconciliation, by which time the
        # window really is gone. The reaper deletes only on positive proof.
        from .window_binding_store import WindowBindingStore

        reap = WindowBindingStore().reap_dead_windows(root)
        out = {
            "performed": True,
            "pruned": result["pruned"],
            "kept_live": result["kept_live"],
            "unprovable": result["unprovable"],
            "total_after": result["total_after"],
            "lease_reaped": reap["reaped"],
            "lease_reap_skipped": reap["skipped"],
        }
    except Exception as exc:  # noqa: BLE001 — hygiene never blocks a boot
        out = {"performed": False, "ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return out


def create_server(
    dashboard_mode: bool = False,
    tools_profile: str = "full",
    expose_deploy: bool = False,
    expose_run: bool = False,
) -> Any:
    """Build the AIDOCS MCP server.

    expose_deploy (Empire directive 2026-07-06): register the ai_deploy /
    ai_deploy_output tools. DEFAULT False — deploy is NONEXISTENT on the
    local MCP surface an agent talks to (the stdio/dashboard serve entry
    passes False), so a local agent can neither see nor call it. Only the
    gate's INTERNAL execution servers (tool_interface._delegate and the
    transport EDIT dispatch) opt in with True, so the VPS webmcp path can
    still run a super_admin-authorized, 2FA-gated deploy. Advertising is
    registry-driven (outer_gate_catalog), independent of this flag. Fail-
    safe: a call site that forgets the flag HIDES deploy, never exposes it.


    dashboard_mode: when True, register dashboard-only tools
    (conductor_start / conductor_send / conductor_stop /
    conductor_output). When False (default, every agent-side MCP),
    those tools are skipped at registration so they never appear in
    the agent's tool list. Set via --dashboard CLI flag by the
    dashboard's Tauri spawner.

    tools_profile: "full" (default — the local stdio/agent server,
    behavior UNCHANGED) registers every tool incl. the RFC-4 palace
    bundle (hard ``import mempalace``). "read_only" registers the
    canonical code/read tool surface but SKIPS the palace bundle, so a
    minimal deployment (e.g. the loopback Outer Gate's read-only Tier-R
    executor) can run the canonical read tools without vendoring
    mempalace or the palace vector stack. It never adds any tool the
    full profile lacks.
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "FastMCP is not installed. Install the MCP package dependencies before running the server.",
        ) from exc

    # Tool-registration modules + the renders_as decorator are imported HERE
    # (server creation), not at module top: they transitively pull the fastmcp/mcp
    # SDK, needed only when a server is built. `import aidocs_mcp.mcp_server` stays
    # SDK-free for helper/tooling imports; a broken tool module still fails loudly
    # at this real server entrypoint. Behavior/registration order unchanged.
    from .server_audit_tools import register_audit_tools
    from .server_code_edit_tools import register_code_edit_tools
    from .server_code_tools import register_code_tools
    from .server_legacy_git_tools import register_legacy_git_tools
    from .server_memory_index_tools import register_memory_index_tools
    from .server_plan_task_tools import register_plan_task_tools
    from .server_project_admin_tools import register_project_admin_tools
    from .server_rbac_tools import register_rbac_tools
    from .server_run_tools import register_run_tools
    from .server_runtime_context_tools import register_runtime_context_tools
    from .server_session_tools import register_session_tools
    from .server_skill_tools import register_skill_tools
    from .tool_display import renders_as

    hub = AidocsServiceHub(
        templates_root=_resolve_templates_root(),
        script_root=_resolve_script_root(),
    )
    runtime = RuntimeService(hub)
    # Register the canonical runtime for library code (file_ops /
    # protected_file_ops grant reads) that can't take it via DI — see
    # runtime_bootstrap_service.get_runtime (phantom-import fix 2026-06-11).
    from .runtime_bootstrap_service import set_runtime as _set_runtime_singleton

    _set_runtime_singleton(runtime)
    server = FastMCP("AIDOCS MCP", instructions=_build_server_instructions())
    server._aidocs_test_hub = hub  # test access only

    # Doctrine 2026-05-29 (Empire re-seal — lifecycle injection):
    # bind the ShellEgressService singleton's lifecycle preflight
    # handle to the live hub's require_active_task helper. This
    # flips step 5 of the cascade from honest-fail-closed-unwired
    # to honest-allow-when-active-task-present for agent_reachable
    # shell egress. The closure captures `hub` by reference so
    # subsequent hub state changes are reflected automatically.
    try:
        from pathlib import Path as _Path
        from .mcp_server_runtime_helpers import shell_egress_lifecycle_preflight as _selp
        from .shell_egress_service import default_service as _shell_egress

        def _lifecycle_preflight(cwd: str, tool_name: str) -> dict | None:
            # Strict shell-egress wrapper — never fail-opens on
            # managed_mode/query_gate infrastructure errors; returns
            # structured 'error'/'reason' dicts that ShellEgress
            # translates to lifecycle_preflight_error vs
            # lifecycle_no_active_task respectively. See
            # shell_egress_lifecycle_preflight doctrine.
            return _selp(hub, _Path(cwd) if cwd else _Path("."), tool_name)

        _shell_egress().bind_lifecycle_preflight(_lifecycle_preflight)
    except Exception:
        # Best-effort: a failure here leaves the service in its
        # honest-fail-closed-unwired posture for agent_reachable
        # calls. The service's refused_reason='lifecycle_
        # preflight_unwired' surfaces the gap to the operator.
        pass

    # Phoenix 2026-05-07: universal notification injection. EVERY
    # @server.tool() registration after this line is wrapped so its
    # response carries pending run_notifications + lane_completion_
    # reviews — closes the gap where dict-returning tools bypassed
    # tool_display.py's per-decorator drain. Must be installed BEFORE
    # any tool is registered. Per emperor 2026-05-07 directive.
    from .notification_injector import install_universal_notification_injection

    install_universal_notification_injection(server)

    # #204 item-2 (dashboard-war c): universal cancel-safety. Every SYNC tool
    # registered after this line is offloaded to a worker thread so a blocking
    # body can never wedge the event loop — the class fix generalizing the
    # 4-palace-tool offload (e4a106e1). Installed AFTER the injector so a
    # sync fn reaches the injector already async (both patch server.tool).
    from .cancel_safety_offload import install_universal_sync_offload

    install_universal_sync_offload(server)

    # ── Boot self-test (2026-04-22) ──
    # Run the managed_mode reconnect cycle against a scratch tmp
    # project. If set_mode → get_mode → connect → get_mode ever
    # leaves requires_reconnect=True, the server has a latent
    # deadlock bug and we log loud + set `_boot_self_test` for the
    # dashboard. We do NOT refuse to start — blocking server startup
    # on an internal bug would itself be a new way AIDOCS could lock
    # operators out. Instead log, mark degraded, rely on the
    # enforcement-disabled kill switch as last resort.
    #
    # Call the store directly, NOT the service — the service's
    # set_mode has a side effect (sets module-global default project
    # root). Using the store bypasses that so our scratch tmpdir
    # doesn't leak into subsequent tool calls.
    server._boot_self_test = {"passed": False, "reason": "not_run"}
    try:
        import tempfile as _bst_tempfile

        with _bst_tempfile.TemporaryDirectory(prefix="aidocs-bst-") as _bst_dir:
            from pathlib import Path as _BstPath

            _bst_root = _BstPath(_bst_dir) / "proj"
            (_bst_root / ".MEMORY").mkdir(parents=True)
            # Direct store calls — no set_default_project_root leak.
            from .managed_mode_service import (
                _MCP_SERVER_BOOT_TOKEN as _BST_TOKEN,
            )

            hub.managed_mode._store.init_db(_bst_root)
            hub.managed_mode._store.set(
                _bst_root,
                session_id="bst-session",
                source="boot_self_test",
                boot_token=_BST_TOKEN,
            )
            _bst_row = hub.managed_mode._store.get(_bst_root)
            _bst_passed = (
                _bst_row.get("active") is True and _bst_row.get("bound_by_boot_token") == _BST_TOKEN
            )
            if not _bst_passed:
                raise RuntimeError(f"boot-token round-trip broken: {_bst_row}")
            server._boot_self_test = {
                "passed": True,
                "bound_token_matches_current": True,
            }
        # CONSTRUCTION IS NOT AN ACTOR LIFECYCLE EVENT (#982, operator ruling
        # 2026-08-30: "Also remove destructive actor cleanup from
        # create_server(). create_server() is construction, not an actor
        # lifecycle event ... Constructing an object must never delete durable
        # actor/session authority.").
        #
        # This function used to prune conductor bindings inline. It is called
        # from SIX production sites — the serve entry, the read-only gate
        # executor's lazy `_ensure_server`, the manifest builder, the transport,
        # runtime_service and tool_interface's registry bootstrap — so "a server
        # object was constructed" fired a destructive sweep at moments that have
        # nothing to do with any actor's life. That is why a live WebMCP
        # conversation could lose its binding with NO daemon restart between a
        # working call and a refused one.
        #
        # THE PYTEST GUARD THAT USED TO LIVE HERE WAS THE STANDING ADMISSION:
        # "without this guard, every test_*.py that touches a server silently
        # deletes per_conductor rows from the operator's live DB". The hazard was
        # known and fenced for tests only; production kept it.
        #
        # Cleanup now lives in `reconcile_conductor_actors()` and is invoked from
        # the SERVE ENTRY (boot), never from construction.
        server._conductor_binding_prune = {
            "performed": False,
            "reason": "construction_is_not_a_lifecycle_event",
        }
        # Lane 1.5 (2026-05-04): sweep expired session_freeze rows at boot so a
        # self_approve lock with a passed TTL cannot survive an MCP restart.
        # Cheap, idempotent. Q2 doctrine 2026-05-04: no boot sweep on freeze.
        #
        # #982: this used to sit inside the `else:` arm of the conductor-prune
        # PYTEST GUARD, so under pytest it silently did not run either — a
        # second behaviour riding on a guard that was only ever about the prune.
        # With that guard gone it is unconditional, which is what its own
        # "cheap, idempotent, no_sweep policy" description always implied.
        try:
            server._freeze_boot_sweep = {
                "ok": True,
                "rows_deleted": 0,
                "policy": "no_sweep",
            }
        except Exception as _fs_exc:
            server._freeze_boot_sweep = {
                "ok": False,
                "reason": f"{type(_fs_exc).__name__}: {_fs_exc}",
            }
    except Exception as _bst_exc:
        import sys as _bst_sys

        server._boot_self_test = {
            "passed": False,
            "reason": f"{type(_bst_exc).__name__}: {_bst_exc}",
        }
        try:
            print(
                f"[aidocs boot self-test FAILED] {_bst_exc!r} — "
                f"the server will still start, but the reconnect "
                f"cycle is broken. There is no in-app bypass (#404); "
                f"investigate and fix the failing self-test.",
                file=_bst_sys.stderr,
            )
        except Exception:
            pass
    # Per-server dedup state (was module-global; moved to instance so tests
    # that spin up fresh servers get fresh caches — otherwise a prior
    # test's identical-args tool call serves cached results into the next
    # test's assertions).

    # Concise helper for tool implementations: _project_root("path") -> Path
    # Raises ValueError if no root available (no session, no default)
    from .mcp_server_runtime_helpers import resolve_project_root as _project_root

    raw_server_tool = server.tool

    # CURATED TOOL DESCRIPTIONS — from sqlite, NOT TOML (name corrected 2026-07-26).
    #
    # _load_action_hook_defaults() reads gate_message_strings out of
    # empire.sqlite3; the TOML walker it replaced is long gone. The old
    # "toml_desc" naming and its duplicated comment survived and actively
    # misled the investigation into #533 — the label is now the fact.
    #
    # WHY THE ABSENCE IS ANNOUNCED. These descriptions OVERRIDE each tool's
    # docstring, so when they are missing every tool advertises its RAW SOURCE
    # DOCSTRING instead — and griffe parses that docstring's `Args:` block into
    # per-parameter help, producing a MATERIALLY DIFFERENT wire inputSchema for
    # every mode-dispatched tool. That silently shipped the wrong surface: the
    # golden pin was captured in an environment without the catalog, so it pinned
    # the fallback as if it were intended (#533).
    #
    # empire.sqlite3 is MACHINE-GLOBAL USER-HOME state (~/.aidocs/), not in the
    # repo and not in the deploy payload, so "absent" is a REAL and reachable
    # state — a fresh box, a stripped custody env, the VPS proof root. It is not
    # fatal (the server still serves), but it must never be silent again.
    _tool_descriptions: dict[str, str] = {}
    _tool_desc_load_error: str = ""
    try:
        hooks = _load_action_hook_defaults()
        descs = hooks.get("tool_descriptions")
        if isinstance(descs, dict):
            _tool_descriptions = {k: str(v) for k, v in descs.items() if isinstance(v, str)}
    except Exception as _exc:  # noqa: BLE001 — never fatal; recorded and announced
        _tool_desc_load_error = repr(_exc)
    if not _tool_descriptions:
        import logging as _tool_desc_logging

        _tool_desc_logging.getLogger("aidocs.tool_surface").warning(
            "TOOL SURFACE DEGRADED: no curated tool descriptions loaded "
            "(gate_message_strings in empire.sqlite3)%s. Every tool will "
            "advertise its RAW SOURCE DOCSTRING, which changes the wire "
            "inputSchema of mode-dispatched tools. Provision the catalog; do "
            "NOT regenerate surface goldens against this state (#533).",
            f" — load error {_tool_desc_load_error}" if _tool_desc_load_error else "",
        )

    # ── Deferred tool loading ──
    # Tool tiers defined in agent_orchestrator.py (agent-agnostic)
    from .agent_orchestrator import is_eager_tool as _is_eager

    _deferred_tool_names: set[str] = set()

    # Lane-worker tool-scope filter (2026-04-24): when this MCP server
    # is running as a stdio child of a spawned lane worker (detected
    # via AIDOCS_EXPERT_LANE_ID env var set by agent_expert_service),
    # shrink the registered tool surface to the worker's allowlist.
    # Reason: MiniMax-free and other small-context models choke on
    # 124 AIDOCS tool schemas in the system context before work even
    # starts; claude workers dodge this via CLI-side --allowedTools
    # but opencode has no equivalent, so we must cut the surface at
    # the MCP layer. Allowlist source: AIDOCS_EXPERT_LANE_ALLOWED env
    # (JSON list set by spawner) → fallback to the same defaults the
    # claude CLI gets (_DEFAULT_LANE_CLI_TOOLS mirror). Conductor-only
    # tools are blocked separately by access_gate._CONDUCTOR_ONLY_TOOLS
    # so nothing here can escalate privilege.
    import json as _json_lane_scope
    import os as _os_lane_scope

    _worker_lane_scope: set[str] | None = None
    if _os_lane_scope.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
        allowed_raw = _os_lane_scope.environ.get(
            "AIDOCS_EXPERT_LANE_ALLOWED",
            "",
        ).strip()
        if allowed_raw:
            try:
                parsed = _json_lane_scope.loads(allowed_raw)
                if isinstance(parsed, list):
                    _worker_lane_scope = {str(x).strip() for x in parsed if str(x).strip()}
            except (_json_lane_scope.JSONDecodeError, TypeError, ValueError):
                _worker_lane_scope = None
        if _worker_lane_scope is None:
            # Fallback: mirror the claude CLI default lane allowlist
            # so the two backends advertise the same surface shape.
            try:
                from .agent_expert_service import _DEFAULT_LANE_CLI_TOOLS

                _worker_lane_scope = set(_DEFAULT_LANE_CLI_TOOLS)
            except Exception:
                _worker_lane_scope = None
        # Tools every lane worker needs regardless of explicit scope —
        # the boot sequence + self-observation. Without these the
        # worker can't bind, can't fetch its brief, can't report.
        if _worker_lane_scope is not None:
            # ai_session(mode='connect') is the single boot door (paved-road,
            # 2026-05-12 mode-collapse) — detects worker env vars and binds +
            # delivers lane plan in one call. ai_task covers begin/update/
            # complete/status via mode dispatch.
            _worker_lane_scope |= {
                "ai_session",
                "ai_task",
                # WAR D (#452 / #217): messagerie surfaces are always
                # registered for a lane worker — the unread-message block
                # tells the worker to drain ai_lane_inbox; an unregistered
                # drain surface would deadlock the worker. ai_lane_send /
                # ai_lane_state are the STOP-and-signal-conductor path.
                "ai_lane_inbox",
                "ai_lane_send",
                "ai_lane_state",
            }

    # Admin / dashboard-only tools (2026-04-24): agent never needs
    # these — they're management/telemetry surfaces. Skipped at
    # registration when dashboard_mode=False so they vanish from
    # the agent-facing tool list. Dashboard MCP server still sees them.
    _ADMIN_ONLY_TOOLS: set[str] = {
        # rbac / identity / escalation
        "rbac_approve_escalation",
        "rbac_deny_escalation",
        "rbac_list_pending_escalations",
        "rbac_list_roles",
        "rbac_list_users",
        "rbac_user_permissions",
        # denial / audit telemetry — aggregates + session-wide views
        # stay admin/dashboard-only. Per-lane visibility is served by
        # audit_events_for_task(task_id) (stays agent-visible) and
        # lane_mailbox_peek / conductor_message_history. Session-wide
        # query_last / events_get / leaderboard are dashboard surfaces
        # that dump artifacts, not real-time lane scoping.
        "denial_tier_stats",
        "denial_trend_24h",
        "most_denied_commands",
        "recent_denials_for_session",
        "verify_audit_chain",
        "tool_use_leaderboard",
        # metrics
        "metrics_prometheus",
        "metrics_snapshot",
        "dashboard_snapshot",
        "circuit_breaker_reset",
        "circuit_breaker_status",
        # execution clear/prune (destructive ops admin)
        "execution_clear_token_usage",
        "execution_clear_tool_calls",
        "execution_prune",
        "archive_sessions_now",
        # admin_clear_reconnect STAYS agent-visible (2026-04-24 revert):
        # it's the break-glass tool for managed-mode desync. Hiding it
        # behind dashboard_mode traps the agent when the desync bug
        # hits (which is the ONLY time the tool is needed).
        # backlog / nudge / archive housekeeping
        "backlog_inbox",
        "inactive_session_nudge",
        "list_archive_candidates",
        # protect_file / list_protected_files — folded into
        # ai_protect(mode=...) which stays agent-visible for the
        # user-intent DO-NOT-TOUCH flow.
        # mcp registry (dashboard MCP-install UI)
        "mcp_registry_get",
        "mcp_registry_search",
        "mcp_tool_catalog",
        # roadmap (product/roadmap UI)
        "roadmap_feedback_update",
        "roadmap_layer_progress",
        # action surface analysis (dashboard insights)
        "action_surface_assess",
        "action_surface_compare",
        "action_surface_current_session_bundle",
        "action_surface_session_bundle",
        "action_surface_status_bundle",
        # config admin
        "config_diff_from_default",
        "config_edit_policy_get",
        "config_validation_report",
        # edit history / rollback (dashboard diff UI)
        "edit_diff_summary",
        "edit_history_grep",
        "edit_history_list",
        "edit_rollback",
        "edit_rollback_batch",
        "edit_session_overlap",
        # file / touch heatmaps (dashboard analytics)
        "file_age_histogram",
        "files_touched",
        "files_touched_heatmap",
        "hot_files_with_no_test",
        "untouched_code_files",
        "recent_commits_touching_file",
        "recent_errors_scan",
        "dependency_freshness",
        # index language descriptors (dashboard config)
        "index_language_descriptor_match_get",
        "index_language_descriptor_semantics_get",
        "index_language_descriptors_get",
        "index_language_descriptors_validate",
        # procedure / capability (indexer internals)
        "procedure_capability_link_status",
        "procedure_capability_links_get",
        "procedure_definitions_get",
        "procedure_index_status",
        "capability_definitions_get",
        "capability_index_status",
        # semantic index admin (agent uses semantic_search)
        "semantic_index_status",
        "semantic_index_sync",
        "schema_index_sync",
        # memory admin / diagnostics
        "memory_content_check",
        "memory_doc_word_count",
        "memory_shape_check",
        "memory_stale_finder",
        # project admin / diagnostics
        "project_audit_snapshot",
        "project_freshness",
        "project_health_score",
        "project_size_report",
        "project_progress_dashboard",
        "project_check_legacy",
        "project_inspect_legacy",
        "project_origins_get",
        "project_registry_list",
        "project_sync_indexes",
        # session admin / export / analytics
        "session_artifacts_normalize",
        "session_compare",
        "session_compliance_get",
        "session_export_markdown",
        "session_handoff_completeness",
        "session_handoff_get",
        "session_handoff_step_update",
        "session_handoff_steps_get",
        "session_handoff_steps_normalize",
        "session_handoff_update",
        "session_owner_summary",
        "session_prune_stale_claims",
        "session_start_state_get",
        "session_status_badge",
        "session_timeline",
        # task analytics (agent uses begin/update/complete/status)
        "task_breadcrumbs",
        "task_open_or_blocked",
        "task_progress_streak",
        "task_velocity",
        # skill admin
        "skill_override_registry_get",
        "skill_provider_override_set",
        "skill_provider_status_get",
        # workflow admin
        "workflow_actions_compile",
        "workflow_step_chronograph",
        "workflow_triggers_for_action",
        # git admin (agent uses git_ops)
        "git_conflict_analysis",
        "git_diag",
        "git_fork_status",
        "git_merge_plan",
        "git_upstream_changes",
        # execution admin / session-wide telemetry — dashboard surfaces.
        # Conductor uses audit_events_for_task(task_id) for per-lane
        # visibility, not these session-wide aggregates that dump into
        # sqlite artifacts.
        "execution_event_record",
        "execution_run_record",
        "execution_events_get",
        "execution_runs_get",
        "execution_index_status",
        "execution_loop_next",
        "execution_mode_select",
        "execution_query_compliance",
        "execution_query_last",
        "execution_query_summary",
        "execution_usage_by_identity",
        # planning docs / normalize (dashboard plan UI)
        "planning_docs_list",
        "plan_normalize_prose",
        "plan_step_drift",
        "plan_validate",
        "plan_preflight",
        # misc admin
        "memory_routing_orphans",
        "reserved_filename_check",
        "workflow_definition_list",
        "workflow_definition_add",
        "workflow_definition_update",
        "workflow_definition_remove",
        "legacy_build_session_proposal",
        "mode_clear",
        "mode_get",
        "mode_set",
        "db_query",
        # Runtime-auto tools (2026-04-24): called by host/runtime at
        # bootstrap/reconnect/session-switch, NOT by agent directly.
        # Hidden to save agent context.
        "project_bootstrap_or_resume",
        "project_init",
        "project_ensure_mcp_config",
        "project_check",
        "project_fix",
        "project_status",
        "index_sync",
        "index_status",
        "runtime_preflight",
        "classify_prompt",
        "handle_prompt",
        "orchestrate",
        "route_prompt",
        "context_budget_check",
        "context_compact",
    }

    # Hidden helpers — Python defs that keep @server.tool for shape
    # but skip MCP registration because they're folded into the
    # ai_slop(mode=...) dispatcher — maintenance / cleanup / refactor.
    # (pebble cleanup): replaced the `_NAME_REMAP` dict-with-empty-
    # string-values pattern; a set is what it always meant.
    # ── Mode-dispatch schemas live in mode_schema.py (Empire directive 2026-05-12) ──
    # FastMCP's auto-schema emits a FLAT JSONSchema from the function
    # signature — every param appears optional/nullable, the dispatcher
    # `mode` doesn't gate per-mode required-sets. For mode-dispatch
    # tools (ai_replace, ai_find, ai_run, ai_soul, ai_skill, ai_msg
    # /ai_lane families, etc.) this means the schema lies: it doesn't
    # tell the agent "mode='send' REQUIRES to_roles+body". The rules
    # live only in docstrings.
    #
    # Fix (SSOT war 2026-07): per-mode required + optional params are
    # declared ONCE, on the tool's @tool(modes={...}) declaration in
    # tool_interface.py (ToolSpec.modes). After registration,
    # _apply_mode_schemas walks every Tool, reads ToolSpec.modes for
    # that name (legacy fn._mode_specs from a local @modes decorator is
    # only a fallback), and enriches the tool's `parameters` JSONSchema
    # with the mode enum + per-mode required sets. The WebMCP gate's
    # schema_for() reads the same ToolSpec.modes — both surfaces are
    # projections of the one declaration.

    # SINGLE SOURCE: the hidden-from-both carve-out is owned by the canonical
    # catalog resolver (outer_gate_catalog.HIDDEN_EVERYWHERE), so local MCP and
    # the Outer Gate share one authority — no drift between a local hidden list
    # and the gate's classification.
    from .outer_gate_catalog import (
        CONSOLIDATOR_DELEGATE_IMPLS as _CONSOLIDATOR_DELEGATE_IMPLS,
        HIDDEN_EVERYWHERE as _HIDDEN_TOOLS,
    )

    def _taxonomy_tool(*args: Any, **kwargs: Any) -> Any:
        explicit_name = kwargs.pop("name", None)
        eager = kwargs.pop("eager", None)  # explicit override

        def decorator(func: Any) -> Any:
            tool_name = explicit_name or func.__name__
            if tool_name.startswith("aidocs_"):
                tool_name = tool_name.removeprefix("aidocs_")
            # Skip registration for helpers folded into dispatchers.
            # Function stays Python-callable; just not MCP-exposed.
            if (
                tool_name in _HIDDEN_TOOLS
                or tool_name in _CONSOLIDATOR_DELEGATE_IMPLS
            ):
                # C.20: consolidator delegate targets are implementation
                # bindings, not FastMCP tools. Their exact closures register in
                # tool_interface._IMPLS and remain callable through the parent
                # consolidator without polluting the registered surface.
                return func
            # Admin-only gate (2026-04-24): if the tool is in the
            # _ADMIN_ONLY_TOOLS set AND we're not in dashboard mode,
            # skip registration. Function stays Python-callable;
            # just not exposed via MCP to the agent. The dashboard
            # MCP server (dashboard_mode=True) still registers them.
            if tool_name in _ADMIN_ONLY_TOOLS and not dashboard_mode:
                return func
            # Lane-worker scope filter (2026-04-24): when this MCP
            # server instance is running inside a spawned lane worker
            # (AIDOCS_EXPERT_LANE_ID env set at create_server time),
            # skip registration for any tool not in the worker's
            # allowlist. Keeps the model's tool-schema budget small
            # enough for small-context backends like MiniMax.
            if _worker_lane_scope is not None and tool_name not in _worker_lane_scope:
                return func
            # Override the docstring with the CURATED description (sqlite, not
            # TOML — see the loader note above). When absent the raw docstring
            # stands, which is a different advertised schema; that case is
            # announced once at load time rather than per tool.
            curated_desc = _tool_descriptions.get(tool_name)
            if curated_desc and func.__doc__:
                func.__doc__ = curated_desc
            # Track deferred tools using agent-agnostic tier classification
            is_eager_val = eager if eager is not None else _is_eager(tool_name)
            if not is_eager_val:
                _deferred_tool_names.add(tool_name)
            # Inject visibility tags for FastMCP filtering
            existing_tags = kwargs.get("tags", set())
            if not existing_tags:
                existing_tags = set()
                kwargs["tags"] = existing_tags
            if is_eager_val:
                existing_tags.add("eager")
            else:
                existing_tags.add("deferred")
            return raw_server_tool(*args, name=tool_name, **kwargs)(func)

        return decorator

    server.tool = _taxonomy_tool

    # ── Post-registration: Apply visibility filtering ──
    # After all @server.tool() decorators run, disable deferred tools
    # so they only appear when explicitly surfaced via tool discovery.
    # Uses FastMCP's native tags-based filtering.
    _eager_tag = {"eager"}
    _deferred_tag = {"deferred"}
    # Disable all deferred tools at startup (runtime-visible default)
    # Note: This is a one-time filter at server bootstrap; AIDOCS authorization
    # still gates actual execution, this only controls initial tool surface.
    if _deferred_tool_names:
        # Can't await here (sync module scope) — defer to first list_tools call
        # Store deferred names for lazy finalize
        server._aidocs_deferred_to_disable = _deferred_tool_names.copy()
        server._aidocs_eager_tag = _eager_tag
        server._aidocs_deferred_tag = _deferred_tag

    # Store tier metadata for introspection
    server._aidocs_deferred_tools = _deferred_tool_names
    server._aidocs_filter_applied = False  # Track if we've applied filtering

    # Wrap list_tools to apply deferred filtering on first call
    # Use _aidocs_deferred_tools (persists) not _aidocs_deferred_to_disable (gets cleared)
    _original_list_tools = server.list_tools

    async def _filtered_list_tools(*, run_middleware: bool = True):
        # Trust Claude Code's native ToolSearch (2026-04-24): with
        # ENABLE_TOOL_SEARCH=true set in the host environment, Claude
        # Code's own defer_loading / ToolSearch mechanism handles the
        # "known but not schema-loaded" pattern for MCP tools. Our
        # previous server.disable() call fought that by removing
        # deferred tools from the host's view entirely, breaking
        # ToolSearch discovery (select:... returned 'no match') and
        # making enable()-based dynamic surfacing impossible.
        #
        # New policy: keep every tool registered + enabled. Host
        # decides what to load eagerly vs. via ToolSearch. Deferred
        # metadata (_aidocs_deferred_tools) stays as a category tag
        # for introspection. Call-time enforcement (PreToolUse +
        # sticky NLP grants in user_intent_tools) is the sole
        # authoritative gate.
        #
        # Operator setup requirement: ENABLE_TOOL_SEARCH=true in
        # the agent's environment for the token-saving behaviour.
        # Without it, all 250+ schemas load at connect time — ~100k
        # tokens. Installer should set this.
        #
        # SINGLE SOURCE: route the local tool list through the canonical catalog
        # resolver's hidden-from-both carve-out, so local visibility and the gate
        # share one authority. (These are also not registered, so this is a
        # belt-and-suspenders enforcement, not a behavior change.)
        from .outer_gate_catalog import agent_list_hidden, local_hidden

        # The AGENT (full) profile additionally hides gate-served-only impls
        # + consolidator delegate targets; the read-executor / dashboard
        # builds keep them listed (the gate must see what it executes).
        _hidden = agent_list_hidden if tools_profile == "full" else local_hidden
        listed = await _original_list_tools(run_middleware=run_middleware)
        try:
            return [t for t in listed if not _hidden(getattr(t, "name", ""))]
        except TypeError:
            return listed

    server.list_tools = _filtered_list_tools

    project_registry = ProjectRegistryService()
    project_info_store = ProjectInfoStore()

    # ── Dynamic tool surfacing (2026-04-24) ─────────────────────────
    # FastMCP emits notifications/tools/list_changed from server.enable()
    # when called inside an active request context. We leverage that:
    # sticky NLP grants bump a per-session generation counter; every
    # incoming tool call checks the counter against the in-process
    # "last synced" value; on delta we read the sticky grants and
    # enable any deferred tool that isn't enabled yet. FastMCP then
    # emits list_changed to the host, which re-fetches and exposes the
    # tool. Ungranted deferred tools stay disabled → stay hidden → the
    # pre-existing PreToolUse gate remains authoritative for call-time.
    # Last-synced generation per session (process-local — fresh on restart)
    _grants_last_synced: dict[str, int] = {}

    def _sync_deferred_tool_enable(project_root: Any, session_id: str) -> None:
        """Compare sqlite generation to in-process cache; enable any
        newly-granted deferred tools so FastMCP emits list_changed.

        No-op if counter hasn't advanced. No-op on any store error
        (the call_tool wrapper must never fail on housekeeping).
        """
        if not session_id:
            return
        try:
            current_gen = hub.query_gate.get_grants_generation(
                project_root,
                session_id,
            )
        except Exception:
            return
        key = f"{project_root}::{session_id}"
        last = _grants_last_synced.get(key, -1)
        if current_gen == last:
            return
        try:
            granted = set(
                hub.query_gate.get_user_intent_tools(
                    project_root,
                    session_id,
                )
                or [],
            )
        except Exception:
            return
        deferred = set(getattr(server, "_aidocs_deferred_tools", None) or [])

        def _bare(n: str) -> str:
            for prefix in ("mcp__aidocs__",):
                if n.startswith(prefix):
                    return n[len(prefix) :]
            return n

        # Convert bare names back to the registered tool names by
        # matching against the deferred set.
        to_enable: list[str] = []
        for deferred_name in deferred:
            bare = _bare(deferred_name)
            if deferred_name in granted or bare in granted:
                to_enable.append(deferred_name)
        if to_enable:
            try:
                # enable() inside an active request context triggers
                # FastMCP's auto notifications/tools/list_changed emit.
                server.enable(names=set(to_enable), components={"tool"})
            except Exception:
                pass
        _grants_last_synced[key] = current_gen

    original_call_tool = server.call_tool

    async def _instrumented_call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: Any = None,
        run_middleware: bool = True,
        task_meta: Any = None,
    ) -> Any:
        # Duplicate tool-call suppression now lives at the execution
        # store's INSERT boundary (content-addressed event_id/run_id +
        # ON CONFLICT DO NOTHING). The wrapper used to try to dedup
        # here but the task-context semantics around FastMCP's
        # middleware chain made it unreliable — see the dedup comment
        # near _DEDUP_BUCKET_SECONDS in execution_index_store.py.
        #
        # #224 follow-up: enter the request-scoped config cache for the WHOLE
        # call. Profiling showed the gate cascade resolves ~37 config settings
        # per tool call, each re-reading the same layer DBs from scratch
        # (~120ms/call, >half the per-call cost). request_config_scope() reads
        # each layer's rows ONCE and filters in memory — verdict-identical
        # (config WRITES call invalidate_request_config_scope(), so a config
        # mutation inside the call still observes fresh values). Measured the
        # gate cascade 164ms → 79ms median.
        from .config_resolver import request_config_scope

        # #758 probe: record what the CLIENT actually sends (headers, request
        # _meta, clientInfo) on a real tool call. Inert unless AIDOCS_META_PROBE
        # is set. Observes only -- it is not a capture path, because identity
        # must be captured by AIDOCS, never asserted by the occupant.

        # #758: CAPTURE the caller's host identity from the request itself.
        #
        # The daemon is stateless by design (#435) so there is no transport
        # session to identify a caller -- measured: four tool calls produced four
        # different ctx.session_id values. Its only other source was the
        # query-gate bridge written by claude_hook during UPS, which is why a
        # slow hook broker did not degrade authorization, it DELETED it.
        #
        # The per-window stdio shim inherits CLAUDE_CODE_SESSION_ID at spawn and
        # stamps it here. CAPTURED, NEVER ASSERTED: the value lives in another
        # process's environment and never enters model context, so an agent
        # cannot claim another conductor's identity by restating it. This is the
        # same stamp outer_gate_executor.py already does from its token -- one
        # mechanism, three transports.
        #
        # A request WITHOUT the header behaves exactly as before (no stamp), so
        # a direct HTTP client is byte-identical to today.
        _identity_token = None
        # #876 phase 1: WHICH WINDOW. Its own token, reset on its own line,
        # because it is its own axis -- a conversation is a lease ON a window,
        # and folding the two into one token would re-create the conflation
        # this work exists to undo.
        _window_token = None
        # #880 phase 2: WHY the lease answered as it did, request-scoped so the
        # refusal site and the identity stamp cannot disagree. Its own token for
        # the same reason as above: a leaked reason would explain THIS request's
        # refusal with the PREVIOUS request's cause.
        _lease_token = None
        # #1015: the attribution refusal, stashed rather than raised inline.
        # The identity block below swallows every exception BY DESIGN (identity
        # capture must never break a call), so a ToolError raised inside it
        # would be eaten and the call would proceed unattributed -- the exact
        # failure this closes.
        _attribution_refusal = ""
        try:
            from fastmcp.server.dependencies import get_http_headers

            from .mcp_server_runtime_helpers import (
                current_request_window_key,
                set_request_host_identity,
                stamp_request_window,
            )
            from .stdio_shim import (
                HEADER_HOST_KIND,
                HEADER_HOST_SESSION,
                HEADER_TRANSPORT_CONTRACT,
                HEADER_TRANSPORT_STAMP,
                HEADER_WINDOW,
                record_transport_stamp,
            )

            _hdrs = {k.lower(): v for k, v in (get_http_headers() or {}).items()}
            # #833: record whether the SHIM relaying this call loaded the package
            # that is on disk now. Recorded, never enforced: refusing here would
            # turn a stale transport into a dead window, and the caller has no way
            # to fix it mid-request. ai_version reports it so the gap is NAMED --
            # the failure this closes was never that the remedy was unreachable,
            # it was that nothing told anyone to apply it.
            # #909: the CONTRACT decides the verdict; the tree stamp rides along
            # as diagnostics. A shim running older code is harmless and expected
            # after every update - only a changed HEADER CONTRACT can stop it
            # serving, and that is what this now measures.
            record_transport_stamp(
                (_hdrs.get(HEADER_TRANSPORT_STAMP.lower()) or "").strip(),
                (_hdrs.get(HEADER_TRANSPORT_CONTRACT.lower()) or "").strip(),
            )
            # #876: the WINDOW, stamped BEFORE the identity block and gated on
            # nothing -- the same treatment the transport stamp gets, for the
            # same reason. A window with no session id still IS a window, and it
            # is exactly the unidentified window that gets the least diagnosis
            # today. `HEADER_WINDOW` is named here (rather than the lowercased
            # literal) so the wire name has ONE definition, in stdio_shim, at
            # both ends of the wire.
            _window_token = stamp_request_window(_hdrs, header=HEADER_WINDOW)
            # ── #880 PHASE 2: THE LEASE IS THE AUTHORITY, THE HEADER IS NOT ──
            #
            # The header is the shim's SPAWN-TIME SNAPSHOT. Measured: it stayed
            # on `3a3a4a10` across THREE conversation rotations
            # (/resume, /resume, /clear) and only moved on an explicit /mcp,
            # which respawns the shim. Believing it is the whole staleness
            # defect (#876), and `channels_agree` could never see it because it
            # compared the header against a value DERIVED from the header.
            #
            # So identity is resolved from the WINDOW the request proved it came
            # from, via the conversation SessionStart bound to that window --
            # host-stated, and rewritten on every firing, so it cannot go stale.
            #
            # NO FALLBACK (`system/invariants.md`, operator ruling). When the
            # lease refuses, the identity stamped is the HONEST EMPTY, never
            # `_hsid`. `set_request_host_identity("")` marks the request
            # identity-SCOPED with no sid, so `current_calling_host_session_id`
            # returns #672's honest empty and every existing authority reader
            # refuses -- WITHOUT ANY OF THEM LEARNING A NEW AXIS. That is why
            # `resolve_host_identity`'s ladder, the chain and
            # `last_host_session_id` are untouched by this change: the axis
            # swap happens once, here, at the stamp.
            #
            # The REASON is stashed request-scoped so the refusal site and the
            # identity stamp cannot disagree about WHY -- "no window key on this
            # request" and "no conversation bound to this window" are different
            # failures with different remedies, and today's
            # `managed_mode_not_active` says neither.
            _hsid = (_hdrs.get(HEADER_HOST_SESSION.lower()) or "").strip()
            _window = current_request_window_key()
            if _hsid or _window:
                from .window_lease import (
                    resolve_request_lease,
                    set_request_lease_reason,
                )

                _leased, _lease_reason = resolve_request_lease(_project_root())
                _lease_token = set_request_lease_reason(_lease_reason)
                # ── #1007: THE SUBAGENT AXIS, taken from the call claim ──
                #
                # The transport carries no agent_id and cannot: the shim is
                # one process per window, shared by every subagent in it. The
                # in-subagent PreToolUse hook holds agent_id and the exact
                # (name, arguments) this request now carries, and recorded a
                # one-shot claim keyed on them (conductor_comms). Taken here,
                # against the LEASED conversation only -- a request with no
                # proven identity has no claims to take.
                #
                # ── #1015: AND IT FAILS CLOSED ──
                #
                # Operator ruling 2026-09-04: "make ambiguous/missing subagent
                # attribution fail closed instead of inheriting the parent".
                # The claim now also carries an explicit MAIN-THREAD MARKER,
                # so `main_thread` and `no claim at all` are different facts
                # and the daemon can tell the conductor's own call (allowed,
                # unchanged) from a subagent call it cannot attribute
                # (refused). A host that has never claimed at all -- lane
                # workers, the Outer Gate, any non-CC surface, none of which
                # has a PreToolUse hook -- is `unclaimed_host` and behaves
                # exactly as before.
                #
                # The refusal is raised BEFORE the identity stamp on purpose:
                # a call we refuse must never have worn an identity at all.
                _host_agent_id = ""
                if _leased:
                    _verdict = None
                    try:
                        from .conductor_comms import (
                            xaacp_attribution_refusal_message,
                            xaacp_resolve_call_attribution,
                        )

                        _verdict = xaacp_resolve_call_attribution(
                            _project_root(),
                            host_session_id=_leased,
                            tool_name=str(name),
                            arguments=arguments,
                        )
                    except Exception:  # noqa: BLE001 -- see below
                        # A BROKEN attribution store is not evidence of a
                        # subagent, and refusing every call on a sqlite hiccup
                        # would take the product down. The honest empty keeps
                        # the pre-#1007 behaviour for this one call.
                        _verdict = None
                    if _verdict is not None and not _verdict.get("ok", True):
                        # Raised OUTSIDE the identity try/except (which
                        # swallows everything by design) -- stash and rethrow
                        # after it.
                        _attribution_refusal = xaacp_attribution_refusal_message(_verdict)
                    elif _verdict is not None:
                        _host_agent_id = str(_verdict.get("host_agent_id") or "")
                # A REFUSED call is never stamped: it must not wear the
                # conductor's identity for even the instant before the raise,
                # and an unstamped request cannot leak a token either.
                if not _attribution_refusal:
                    _identity_token = set_request_host_identity(
                        _leased,
                        host_kind=(_hdrs.get(HEADER_HOST_KIND.lower()) or "unknown").strip(),
                        agent_id=_host_agent_id,
                    )
        except Exception:  # noqa: BLE001 -- identity capture must never break a call
            _identity_token = None

        # #1015: the one exception to "identity never breaks a call". An
        # unattributable SUBAGENT call is not a call with a missing nicety, it
        # is a call whose authority cannot be established -- and the old
        # behaviour handed it the conductor's. Refuse, and say which of the two
        # causes it was. Raised here, outside the swallow-everything block.
        if _attribution_refusal:
            if _window_token is not None:
                try:
                    from .mcp_server_runtime_helpers import reset_request_window_key

                    reset_request_window_key(_window_token)
                except Exception:  # noqa: BLE001
                    pass
            if _lease_token is not None:
                try:
                    from .window_lease import reset_request_lease_reason

                    reset_request_lease_reason(_lease_token)
                except Exception:  # noqa: BLE001
                    pass
            from .mcp_server_runtime_helpers import _raise_tool_error

            _raise_tool_error(_attribution_refusal)

        # Probe AFTER the stamp: it records what the TOOL will see, which is the
        # question. Recording before the stamp would always show an empty
        # resolution and prove nothing. Inert unless AIDOCS_META_PROBE is set.
        try:
            from ._host_identity_probe import capture as _probe_identity

            _probe_identity("instrumented_call_tool", str(name))
        except Exception:  # noqa: BLE001
            pass

        try:
            with request_config_scope():
                return await _real_instrumented_call_tool(
                    self,
                    name,
                    arguments,
                    version=version,
                    run_middleware=run_middleware,
                    task_meta=task_meta,
                )
        finally:
            if _identity_token is not None:
                try:
                    from .mcp_server_runtime_helpers import reset_request_host_identity

                    reset_request_host_identity(_identity_token)
                except Exception:  # noqa: BLE001
                    pass
            # #876: released on its own line, unconditionally when it was taken.
            # A leaked window binding would hand the NEXT request on this worker
            # the PREVIOUS window -- the staleness shape this work is undoing.
            if _window_token is not None:
                try:
                    from .mcp_server_runtime_helpers import reset_request_window_key

                    reset_request_window_key(_window_token)
                except Exception:  # noqa: BLE001
                    pass
            # #880 phase 2: the lease REASON, released on its own line for the
            # same reason as the window above. A leaked reason is worse than a
            # leaked value: it would explain THIS request's refusal with the
            # PREVIOUS request's cause, which is precisely the "cannot tell from
            # where" the reasons exist to prevent.
            if _lease_token is not None:
                try:
                    from .window_lease import reset_request_lease_reason

                    reset_request_lease_reason(_lease_token)
                except Exception:  # noqa: BLE001
                    pass
            # #468: release the deferred palace-embedder warm once a tool
            # call has fully completed (idempotent Event.set — near-free on
            # every subsequent call). The warm's chromadb→numpy C-extension
            # import chain, running concurrently with the FIRST call's
            # machinery, wedged fresh servers for minutes; the warm daemon
            # parks on this signal instead of racing the first call.
            try:
                from .server_palace_tools import notify_tool_call_completed

                notify_tool_call_completed()
            except Exception:  # noqa: BLE001 — perf signal must never break a call
                pass

    async def _real_instrumented_call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        version: Any = None,
        run_middleware: bool = True,
        task_meta: Any = None,
    ) -> Any:
        # Auto-fill root from managed session default if not provided

        # Only inject if the tool declares a root/project_root param (key present in args)
        if isinstance(arguments, dict) and ("root" in arguments or "project_root" in arguments):
            if not arguments.get("root") and not arguments.get("project_root"):
                from .mcp_server_runtime_helpers import _last_known_project_root

                if _last_known_project_root is not None:
                    arguments["root"] = str(_last_known_project_root)
        project_root = _project_root_from_args(arguments)
        if not _capture_enabled(name, arguments) or project_root is None:
            return await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        # Universal task gate (2026-05-17, Empire directive: "every command
        # sits behind task_begin, no bypass for ANYTHING including
        # reads"). Fires from the central middleware so reads, writes,
        # AND shell (ai_run) are all attributable. Exemptions are the
        # bootstrap/lifecycle set in
        # mcp_server_runtime_helpers._TASK_GATE_EXEMPT.
        #
        # Project resolution: prefer the arg-derived root; fall back to
        # the session's last-known root so reads that don't carry an
        # explicit project_root (most of them) still hit the gate.
        # Unmanaged projects fail open inside require_active_task — the
        # gate only fires when managed_mode is active.
        # The interactive task gate attributes calls to a conductor's
        # task_begin. The read_only profile is used ONLY by the loopback Outer
        # Gate's read executor, where attribution is ALREADY enforced at the
        # outer boundary (authenticated scoped token + mandatory durable audit
        # with operator/token_id/tool/verdict). Applying the conductor task gate
        # there would wrongly refuse a stateless remote read of a project that
        # merely has a conductor session open. Full profile is unchanged.
        _gate_root = project_root
        if _gate_root is None:
            from .mcp_server_runtime_helpers import _last_known_project_root

            _gate_root = _last_known_project_root
        if tools_profile != "read_only" and _gate_root is not None:
            from .mcp_server_runtime_helpers import require_active_task

            require_active_task(hub, _gate_root, name)
            # require_active_task raises ToolError on refusal; if we
            # get here the gate passed.

        if not _capture_enabled(name, arguments) or project_root is None:
            return await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )

        managed = hub.managed_mode.get_mode(project_root)
        managed_session_id = str(managed.get("session_id") or "").strip() or None
        # Split-brain seal (2026-05-24): a STALE bind (active but the bound
        # session is not a SQL member) must not surface sticky grants or
        # attribute tool calls to a session require_session would refuse.
        # Treat it as unattributed — membership is the sole authority.
        if managed_session_id and managed.get("stale_bind"):
            managed_session_id = None
        # Dynamic tool surfacing: sync in-process enable state with
        # sqlite sticky grants before this call proceeds. If the hook
        # wrote new grants since our last sync, server.enable() fires
        # here → FastMCP auto-emits list_changed → host re-fetches →
        # the tool appears (or already appeared for this call if the
        # host supports live refresh).
        if managed_session_id:
            _sync_deferred_tool_enable(project_root, managed_session_id)
        # Resolve session attribution for this tool call without forcing everyone
        # through managed mode. Priority:
        #   1) explicit session_id in tool arguments — caller knows best
        #   2) session hint on MCP task_meta (_meta) if the client forwarded one
        #   3) managed-mode fallback — only when the user has opted in
        #   4) None — record honestly as unattributed
        session_id: str | None = None
        if isinstance(arguments, dict):
            arg_sid = arguments.get("session_id")
            if isinstance(arg_sid, str) and arg_sid.strip():
                session_id = arg_sid.strip()
        if session_id is None and task_meta is not None:
            meta_obj: Any = task_meta
            if not isinstance(meta_obj, dict):
                meta_obj = getattr(task_meta, "meta", None) or getattr(task_meta, "_meta", None)
            if isinstance(meta_obj, dict):
                for key in ("session_id", "aidocs_session_id", "sessionId"):
                    val = meta_obj.get(key)
                    if isinstance(val, str) and val.strip():
                        session_id = val.strip()
                        break
        if session_id is None:
            session_id = managed_session_id
        project_registry.record_project(
            project_root,
            managed_session_id=managed_session_id,
            title=project_root.name,
        )
        # Mirror the project metadata into the per-project sqlite so the
        # big-boss DB carries title + first-seen + last-seen alongside
        # the other Beat-3 stores. Errors must NOT take the tool call
        # down; project-info is observability, not load-bearing.
        try:
            project_info_store.init_db(project_root)
            project_info_store.record(project_root, title=project_root.name)
        except Exception:
            pass
        run_id = f"mcp-{uuid4()}"
        args_bytes = len(json.dumps(arguments, default=str)) if isinstance(arguments, dict) else 0
        # Build rich payload for tool call tracking
        args_str = json.dumps(arguments, default=str) if isinstance(arguments, dict) else "{}"
        args_bytes = len(args_str.encode("utf-8"))
        args_preview = _tool_args_preview(args_str, name)
        # Determine host/agent identity for usage tracking
        import os as _os

        _host_id = (
            (_os.environ.get("CLAUDE_CODE_VERSION", "") and "claude_code")
            or (_os.environ.get("OPENCODE_VERSION", "") and "opencode")
            or "unknown"
        )
        _agent_id = str(managed.get("agent_id") or "main") if isinstance(managed, dict) else "main"
        _lane_id = None
        if session_id:
            _gate = hub.query_gate.get(project_root, session_id)
            _lane_id = _gate.get("current_lane_id")
            if _lane_id:
                _agent_id = f"lane:{_lane_id}"

        payload_summary: dict[str, object] = {
            "tool_name": name,
            "argument_keys": sorted(arguments.keys()) if isinstance(arguments, dict) else [],
            "args_preview": args_preview,
            "tokens_out_estimate": max(1, args_bytes // 4),
            "host_id": _host_id,
            "agent_id": _agent_id,
        }
        # Structured metadata for specific tool types
        if isinstance(arguments, dict):
            if arguments.get("path"):
                payload_summary["target_path"] = str(arguments["path"])[:200]
                # #391 freshness stamp: record the file's mtime_ns at read time
                # so the line-edit gate (_lines_were_read) can prove a covering
                # read is still CURRENT. A line-number edit is only safe if the
                # file hasn't drifted since the read that grounded it; comparing
                # this stamp to the file's mtime_ns at edit time catches drift
                # exactly (same filesystem clock, sub-second, no truncation).
                # Best-effort — a missing/unstattable path simply omits it.
                try:
                    import os as _os

                    payload_summary["read_file_mtime_ns"] = _os.stat(
                        _os.path.join(str(project_root), str(arguments["path"]))
                    ).st_mtime_ns
                except Exception:
                    pass
            if arguments.get("start_line"):
                # LEGACY string — kept one release (#88); readers prefer the
                # structured `evidence` stamp below.
                payload_summary["line_range"] = (
                    f"{arguments.get('start_line')}-{arguments.get('end_line', '?')}"
                )
            # #88 canonical read-evidence stamp: {path, tool, evidence_type,
            # ranges, line_numbers} — the single model the edit gate's
            # read-before-edit checks consume.
            try:
                from .read_evidence import build_evidence as _build_evidence

                _ev = _build_evidence(name, arguments)
                if _ev is not None:
                    payload_summary["evidence"] = _ev
            except Exception:
                pass  # evidence is additive audit metadata, never load-bearing here
            if arguments.get("query"):
                payload_summary["query"] = str(arguments["query"])[:100]
            if arguments.get("mode"):
                payload_summary["mode"] = str(arguments["mode"])
            # Edit-specific: capture old/new for diff view
            if arguments.get("old_str"):
                payload_summary["old_str"] = str(arguments["old_str"])[:500]
            if arguments.get("new_str"):
                payload_summary["new_str"] = str(arguments["new_str"])[:500]
            if arguments.get("new_content"):
                payload_summary["new_content"] = str(arguments["new_content"])[:500]
            # ai_run: capture the literal command so the
            # verification_gate can auto-record commands_run from the
            # event log instead of trusting agent-reported evidence.
            if arguments.get("command"):
                payload_summary["command"] = str(arguments["command"])[:500]
        # ── #441 Phase 1: durable INTENT audit BEFORE execution ──
        # The tool_call_started row is the spec's TOOL-ATTEMPT record.
        # For MUTATING tiers a failed intent write REFUSES the call
        # (fail closed, nothing executed) so a mid-execution interrupt
        # (process kill / ^C / disconnect) can never yield an executed-
        # but-unaudited mutation. Read tiers tolerate a failed intent
        # write (doctrine: no state change a post-hoc audit could miss).
        from .local_intent_audit import (
            intent_audit_or_refuse,
            result_audit_deferred,
            tool_is_mutating,
        )

        _is_mutating_tier = tool_is_mutating(name)
        intent_audit_or_refuse(
            lambda: _record_tool_execution_state(
                hub,
                project_root,
                run_id=run_id,
                capability_name=name,
                session_id=session_id,
                status="started",
                event_kind="tool_call_started",
                metadata=payload_summary,
            ),
            is_mutating=_is_mutating_tier,
            tool_name=name,
        )
        # ── Lane tool enforcement: block tools not in lane's allowed list ──
        if session_id:
            _lane_gate_state = hub.query_gate.get(project_root, session_id)
            if _lane_gate_state.get("current_lane_id"):
                from .access_gate import AccessGate, GateContext

                _lane_ctx = GateContext(
                    managed=True,
                    session_id=session_id,
                    dev_mode=False,
                    allow_config_edit=False,
                    gate_enforce=True,
                    gate_state=_lane_gate_state,
                )
                _lane_decision = AccessGate.check_lane_tool(_lane_ctx, name)
                if not _lane_decision.allowed:
                    # WAR D (#452) Task 4: the refusal lands in
                    # execution_events with actor attribution (record_event
                    # stamps user_id/agent_epoch), not just in the raised
                    # error text.
                    try:
                        hub.execution.record_event(
                            project_root,
                            event_kind="lane_tool_refused",
                            source_kind="mcp",
                            session_id=session_id,
                            capability_name=name,
                            action_kind="security",
                            target_entity=str(
                                _lane_gate_state.get("current_lane_id") or "",
                            ),
                            status="blocked",
                            payload={"reason": str(_lane_decision.reason or "")[:600]},
                        )
                    except Exception:
                        pass
                    raise RuntimeError(
                        _lane_decision.reason or f"Tool '{name}' blocked by lane policy.",
                    )
                # ── §VII file-scope enforcement (WAR D #452 Task 2) ──
                # AccessGate.check_edit is the ONE edit gate; this is its
                # MCP chokepoint wiring for single-path edit tools. Only
                # the lane_isolation level acts here — sensitive/config/
                # discovery tiers are enforced by their own existing seams.
                from .cross_agent_coordination import _PATH_EDIT_TOOLS

                _bare_name = name.strip().lower()
                for _pfx in ("mcp__aidocs__", "mcp__"):
                    if _bare_name.startswith(_pfx):
                        _bare_name = _bare_name[len(_pfx):]
                        break
                if _bare_name in _PATH_EDIT_TOOLS and isinstance(arguments, dict):
                    _edit_target = str(
                        arguments.get("path") or arguments.get("file_path") or "",
                    ).strip()
                    if _edit_target:
                        _edit_decision = AccessGate.check_edit(_lane_ctx, _edit_target)
                        if (
                            not _edit_decision.allowed
                            and _edit_decision.level == "lane_isolation"
                        ):
                            try:
                                hub.execution.record_event(
                                    project_root,
                                    event_kind="lane_write_refused",
                                    source_kind="mcp",
                                    session_id=session_id,
                                    capability_name=name,
                                    action_kind="security",
                                    target_entity=_edit_target[:300],
                                    status="blocked",
                                    payload={
                                        "lane_id": str(
                                            _lane_gate_state.get("current_lane_id")
                                            or "",
                                        ),
                                        "reason": str(
                                            _edit_decision.reason or "",
                                        )[:600],
                                    },
                                )
                            except Exception:
                                pass
                            raise RuntimeError(
                                _edit_decision.reason
                                or f"Write to '{_edit_target}' blocked by lane isolation.",
                            )
                # ── #217 messages-as-blockers (worker-side, WAR D widened) ──
                # An unread conductor→lane mailbox prompt BLOCKS the lane
                # worker's next tool call. One gate: conductor_comms.
                # lane_read_gate_check (drain/bind surfaces exempt inside).
                # Cleared by draining the inbox (ai_lane_inbox consume-on-
                # read for the own worker, or UPS injection at wake).
                import os as _os_217

                _worker_id_217 = _os_217.environ.get("AIDOCS_EXPERT_ID", "").strip()
                if _worker_id_217:
                    from .conductor_comms import lane_read_gate_check

                    try:
                        _rg_217 = lane_read_gate_check(
                            project_root,
                            worker_id=_worker_id_217,
                            tool_name=name,
                        )
                    except Exception:
                        _rg_217 = {"blocked": False}
                    if _rg_217.get("blocked"):
                        try:
                            hub.execution.record_event(
                                project_root,
                                event_kind="lane_msg_read_block",
                                source_kind="mcp",
                                session_id=session_id,
                                capability_name=name,
                                action_kind="security",
                                target_entity=_worker_id_217,
                                status="blocked",
                                payload={
                                    "mailbox_id": _rg_217.get("mailbox_id"),
                                    "lane_id": str(
                                        _lane_gate_state.get("current_lane_id") or "",
                                    ),
                                },
                            )
                        except Exception:
                            pass
                        raise RuntimeError(
                            "unread conductor message: read ai_lane_inbox first — "
                            + str(_rg_217.get("refusal") or ""),
                        )

        # ── Circuit breaker: check if external MCP server is in cooldown ──
        _external_mcp = name.startswith("mcp__") and not name.startswith("mcp__aidocs__")
        if _external_mcp:
            from .circuit_breaker import get_breaker as _get_breaker

            _breaker = _get_breaker()
            _server_id = name.split("__")[1] if "__" in name else name
            _can_exec, _block_reason = _breaker.can_execute(_server_id)
            if not _can_exec:
                raise RuntimeError(_block_reason)

        try:
            result = await original_call_tool(
                name,
                arguments,
                version=version,
                run_middleware=run_middleware,
                task_meta=task_meta,
            )
        except Exception as exc:
            # #441 Phase 3 (failure leg): OUTCOME audit failure must never
            # mask the tool's own exception — the attempt is already
            # intent-audited, so a lost failure row degrades, not raises.
            # (Captured before the closure: `as exc` is implicitly deleted
            # at except-block end, so a closure over it is fragile.)
            _err_type = type(exc).__name__
            result_audit_deferred(
                lambda: _record_tool_execution_state(
                    hub,
                    project_root,
                    run_id=run_id,
                    capability_name=name,
                    session_id=session_id,
                    status="failed",
                    event_kind="tool_call_failed",
                    metadata={**payload_summary, "error_type": _err_type},
                    completed_at=_utc_timestamp(),
                ),
                tool_name=name,
            )
            from .metrics import get_collector as _get_metrics_err

            _get_metrics_err().record_tool_call(
                tool_name=name,
                status="failed",
                session_id=session_id,
            )
            if _external_mcp:
                _breaker.record_failure(_server_id)
            raise

        # Circuit breaker: record success for external MCP tools
        if _external_mcp:
            _breaker.record_success(_server_id)

        # ── Output Guard: scan tool result for credentials/injections ──
        # Credential redaction happens in place inside the scan; injection
        # findings are now MARKED on the result too (#648/#651) instead of
        # living only in the audit trail — see _scan_and_mark_tool_result for
        # the annotate-and-proceed rationale and the stamped-slot limit.
        guard_summary: dict[str, object] | None = None
        guard_result, guard_scan_status = _scan_and_mark_tool_result(result)
        if guard_result.scanned and not guard_result.clean:
            guard_summary = guard_result.summary()
            payload_summary["output_guard"] = guard_summary
            # Record output guard findings as execution event
            from .tool_call_log import record as _log_record

            _log_record(
                hub,
                project_root,
                phase="guard_finding",
                name=name,
                payload={
                    "finding_count": len(guard_result.findings),
                    "redaction_count": guard_result.redaction_count,
                    "categories": list({f.category for f in guard_result.findings}),
                    "scan_status": guard_scan_status,
                    "max_severity": max(
                        (f.severity for f in guard_result.findings),
                        key=lambda s: {"info": 0, "warning": 1, "critical": 2}.get(s, 0),
                        default="info",
                    ),
                },
                session_id=session_id,
                source="output_guard",
                action_kind="security",
                status="redacted" if guard_result.redaction_count > 0 else "flagged",
            )
        elif guard_scan_status == "unknown":
            # An UNSCANNED result is a fact worth auditing, not a silence:
            # the payload_summary must not read as "guard clean".
            payload_summary["output_guard"] = {
                "scanned": False,
                "scan_status": guard_scan_status,
            }

        # ── Central index-staleness stamp (cheap; NO SHA walk) ──
        # One place stamps honest code/memory freshness onto every index-backed
        # read tool's result (INDEX_BACKED_TOOLS), so the ~13 code tools + memory
        # tools that previously served unstamped now carry the same signal
        # ai_find/ai_slop already did. Shape-preserving; best-effort.
        from .index_staleness import stamp_tool_result as _stamp_index_staleness

        result = _stamp_index_staleness(result, name, project_root, arguments)

        # ── Metrics: record tool call ──
        from .metrics import get_collector as _get_metrics

        _metrics = _get_metrics()

        result_summary = _summarize_tool_result(result)
        result_bytes, result_text_preview = _tool_result_preview(result, name)
        tokens_in_estimate = max(1, result_bytes // 4)
        tokens_out_estimate = max(1, len(str(arguments).encode("utf-8")) // 4)
        payload_summary["tokens_in_estimate"] = tokens_in_estimate
        payload_summary["result_preview"] = result_text_preview
        # Extract exit_code for tool results that carry one (ai_run,
        # ai_run_output, ai_run_status). Lets verification_gate
        # count only commands whose runs actually passed. Without this
        # the gate trusts any recorded command-run regardless of outcome,
        # which means agents can satisfy the gate with failing tests.
        # (Bug fix 2026-04-20.)
        #
        # #324 extension: governed-runner results (ai_test) resolve their
        # command INTERNALLY — no `command` argument — so also stamp the
        # resolved command from the result (framework + args + rc). The
        # evidence gate then accepts the runner's own audited receipt and
        # agents never hand-transcribe what the audit chain already holds.
        try:
            _receipt = _runner_receipt_fields(result)
            if "exit_code" in _receipt:
                payload_summary["exit_code"] = _receipt["exit_code"]
            if "command" in _receipt and not payload_summary.get("command"):
                payload_summary["command"] = _receipt["command"]
        except Exception:
            pass

        _metrics.record_tool_call(
            tool_name=name,
            status="completed",
            session_id=session_id,
            tokens_in_estimate=tokens_in_estimate,
            tokens_out_estimate=tokens_out_estimate,
        )
        if guard_result.scanned:
            _metrics.record_guard_scan(
                clean=guard_result.clean,
                redaction_count=guard_result.redaction_count,
                findings=[
                    {"category": f.category, "severity": f.severity} for f in guard_result.findings
                ],
            )

        # ── #441 Phase 3: RESULT audit AFTER execution — degraded, never
        # fail-closed retroactively. The mutation (if any) already stands
        # behind its durable intent row; a failed result write must not
        # swallow the tool result into an error.
        #
        # 2026-08-23: nor DELAY it. This write used to sit synchronously in
        # front of `return result`, so a saturated audit DB spent the whole
        # bounded retry budget (4 attempts x 10s, twice — record_run AND
        # record_event) before degrading. The degrade note was correct and
        # the operator still saw a finished tool call as a dead hang, and
        # cancelled it. Deferred: the row still lands (or degrades loudly)
        # on the background writer, and the response leaves now.
        result_audit_deferred(
            lambda: _record_tool_execution_state(
                hub,
                project_root,
                run_id=run_id,
                capability_name=name,
                session_id=session_id,
                status="completed",
                event_kind="tool_call_completed",
                metadata={**payload_summary, "result_summary": result_summary},
                completed_at=_utc_timestamp(),
            ),
            tool_name=name,
        )
        return result

    server.call_tool = MethodType(_instrumented_call_tool, server)

    register_session_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_sync=timed_sync,
        annotate_skill_result=_annotate_skill_result,
        session_summary_to_dict=_session_summary_to_dict,
        coerce_to_list=_coerce_to_list,
    )

    register_skill_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        annotate_skill_result=_annotate_skill_result,
    )

    register_plan_task_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_sync=timed_sync,
    )

    register_runtime_context_tools(
        server=server,
        hub=hub,
    )

    register_memory_index_tools(
        server=server,
        hub=hub,
        timed_sync=timed_sync,
        timed_indexer=timed_indexer,
    )
    register_code_edit_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        post_edit_reindex_and_grant=_post_edit_reindex_and_grant,
        release_turn_edit_lock=_release_turn_edit_lock,
        file_get_lines=_file_get_lines,
        file_read_raw=_file_read_raw,
        file_create_file=_file_create_file,
        file_edit_lines=_file_edit_lines,
        file_batch_edit=_file_batch_edit,
        file_str_replace=_file_str_replace,
        file_batch_str_replace=_file_batch_str_replace,
        anchor_replace=_file_anchor_replace,
        available_config_edit_modes=available_config_edit_modes,
        self_edit_available_in_profile=self_edit_available_in_profile,
    )

    register_code_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_tool=timed_tool,
        timed_discovery=timed_discovery,
        timed_sync=timed_sync,
        grant_known_exact_path_read=_grant_known_exact_path_read,
        post_edit_reindex_and_grant=_post_edit_reindex_and_grant,
        apply_trace_depth=_apply_trace_depth,
        resolve_related_root=_resolve_related_root,
        file_extract_block=_file_extract_block,
        file_get_lines=_file_get_lines,
        file_create_file=_file_create_file,
        file_edit_lines=_file_edit_lines,
        file_batch_edit=_file_batch_edit,
        file_str_replace=_file_str_replace,
        file_batch_str_replace=_file_batch_str_replace,
        available_config_edit_modes=available_config_edit_modes,
        self_edit_available_in_profile=self_edit_available_in_profile,
        registered_tools=lambda: _registered_tools(server),
        all_procedures=lambda root: _all_procedures(hub, root),
        all_capabilities=lambda root: _all_capabilities(hub, root),
    )

    register_project_admin_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        timed_sync=timed_sync,
        resolve_related_root=lambda root, name: _resolve_related_root(hub, root, name),
    )

    register_rbac_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    register_audit_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    from .server_todo_backlog_tools import register_todo_backlog_tools

    register_todo_backlog_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    # ai_issues (#449 v1, WAR F): immutable local issue filing — the
    # non-admin refusal-footer's real lever (write-once file + pathspec
    # git commit; intent-gated by the literal confirm='file-issue').
    from .issue_filing_service import register_issue_filing_tools

    register_issue_filing_tools(
        server=server,
        hub=hub,
        runtime=runtime,
    )

    # ai_deploy is registered ONLY when expose_deploy=True (the gate's
    # internal execution servers). On the local/agent surface it is absent
    # entirely — hidden and uncallable (Empire directive 2026-07-06).
    if expose_deploy:
        from .server_deploy_tools import register_deploy_tools

        register_deploy_tools(
            server=server,
            hub=hub,
            runtime=runtime,
        )

    # ai_test always; the ai_run trio ONLY when expose_run (same fail-safe
    # direction as expose_deploy — forgetting the flag hides the runner).
    # ONE RUNNER PER SURFACE (operator ruling 2026-07-26): harness agents
    # (local | serveragent | remoteagent) use their own governed shell; only a
    # no-shell WebMCP caller reaches a runner through the gate.
    register_run_tools(
        expose_run=expose_run,
        server=server,
        hub=hub,
        runtime=runtime,
    )

    register_legacy_git_tools(
        server=server,
        hub=hub,
        runtime=runtime,
        resolve_related_root=lambda root, name: _resolve_related_root(hub, root, name),
        timed_git_async=timed_git_async,
        run_git_async=_run_git,
        git_timeout=_GIT_TIMEOUT,
    )

    # ── RFC-4 Palace integration ──
    # Attach hub.palace + register the 4-tool agent surface
    # (ai_palace_search, ai_palace_status, ai_palace_diary_read/write)
    # + the clustered ai_recall tool.
    #
    # RFC-4: mempalace is a HARD bundled dependency — vendored under
    # third_party/mempalace and wired onto sys.path by aidocs_mcp
    # __init__.py. If ``import mempalace`` fails here, the install is
    # broken and the server MUST refuse to start — EVERY profile. Empire
    # ruling 2026-07-06: mempalace is ALWAYS part of AIDOCS; the former
    # read_only soft probe ("minimal deploy may omit palace") is dead —
    # it could ship a catalog advertising palace/recall tools the runtime
    # lacked (phoenix WebMCP report: "Unknown tool" / unknown_arg). Bugs
    # inside palace_hub_extension, server_palace_tools, or
    # server_recall_tools propagate by design.
    # #733: routed through the guard so an unresolvable engine fails with a
    # message naming every path searched (both postures), not a bare
    # ModuleNotFoundError. Still a hard refusal — nothing is softened.
    from . import require_mempalace

    require_mempalace()
    # #738: freeze what THIS process loaded, once, at boot. Everything after this
    # point may import lazily; only a stamp taken HERE can answer "what am I
    # running" without falling back to a git HEAD read that describes the disk,
    # not the memory. Idempotent by design — a later call cannot re-stamp, and
    # that idempotency IS the correctness property.
    from .process_stamp import capture_process_stamp

    capture_process_stamp()
    import mempalace  # noqa: F401 — hard import, asserts vendored bundle

    from .palace_hub_extension import register_palace_in_hub
    from .server_palace_tools import (
        register_palace_tools,
        warm_palace_embedder,
    )
    from .server_recall_tools import register_recall_tools

    register_palace_in_hub(hub)
    if getattr(hub, "palace", None) is not None:
        register_palace_tools(server=server, hub=hub, runtime=runtime)
        register_recall_tools(server=server, hub=hub, runtime=runtime)
        # Warm the ONNX embedder in the background so the first operator
        # palace call is not cold (the cold-init wait that, when cancelled,
        # crashed the MCP connection).
        warm_palace_embedder(hub=hub, runtime=runtime)

    # ── Metrics + Output Guard tools ──

    @server.tool()
    @renders_as("status", title="metrics")
    async def metrics_snapshot() -> Any:
        """Return current MCP server metrics (token usage, tool calls, output guard stats)."""
        from .metrics import get_collector

        return get_collector().snapshot()

    @server.tool()
    async def metrics_prometheus() -> str:
        """Return metrics in Prometheus text exposition format for /metrics scraping."""
        from .metrics import get_collector

        return get_collector().render_prometheus()

    # ── MCP Registry Browser tools ──

    @server.tool()
    @renders_as("list", title="mcp registry search")
    async def mcp_registry_search(query: str = "", limit: int = 20) -> Any:
        """Search the official MCP server registry. Returns matching servers with install commands."""
        from .mcp_registry import search_servers

        try:
            result = search_servers(query, limit=limit)
            return {
                "servers": [s.to_dict() for s in result.servers],
                "total_count": result.total_count,
                "next_cursor": result.next_cursor,
                "install_commands": {s.name: s.install_commands() for s in result.servers},
            }
        except (ConnectionError, ValueError) as exc:
            return {"error": str(exc)}

    @server.tool()
    @renders_as("status", title="mcp registry entry")
    async def mcp_registry_get(name: str) -> Any:
        """Get details for a specific MCP server from the registry."""
        from .mcp_registry import get_server

        try:
            server_info = get_server(name)
            if server_info is None:
                return {"error": f"Server '{name}' not found in registry."}
            return {
                **server_info.to_dict(),
                "install_commands": server_info.install_commands(),
            }
        except (ConnectionError, ValueError) as exc:
            return {"error": str(exc)}

    # ── Circuit Breaker tools ──

    @server.tool()
    @renders_as("status", title="circuit breakers")
    async def circuit_breaker_status() -> Any:
        """Show circuit breaker states for all tracked MCP servers."""
        from .circuit_breaker import get_breaker

        return {"breakers": get_breaker().get_all_states()}

    @server.tool()
    @renders_as("status", title="circuit breaker reset")
    async def circuit_breaker_reset(server_id: str) -> Any:
        """Manually reset a circuit breaker for an MCP server."""
        from .circuit_breaker import get_breaker

        get_breaker().reset(server_id)
        return {"reset": server_id, "state": get_breaker().get_state(server_id)}

    # ── Notification queue tools (Phoenix 2026-05-09) ──

    @server.tool()
    @renders_as("status", title="notifications cleared")
    async def ai_notifications_clear(
        session_id: str = "",
        run_id: str = "",
    ) -> Any:
        """Clear pending 📣 run-done notifications.

        The 📣 surface persists across every tool call until the
        agent reads each run's output (auto-dismiss in ai_run_output)
        — but long fire-and-forget chains (sleeps, status probes,
        marker writes the agent doesn't follow up on) leave permanent
        visual noise. This tool is the conductor's escape hatch.

        Args:
            session_id: AIDOCS session whose notifications to clear.
                Defaults to the conductor's bound session.
            run_id: When set, clear only this run's notification.
                When empty, clear ALL notifications for this session.

        Records owned by OTHER sessions are never touched.

        Does NOT clear 📋 lane completion review pending entries —
        those need conductor verdict via ai_review
        (per aidocs-doctrine §VIII).

        """
        from . import run_notifications as _rn

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        if not sid:
            return {
                "ok": False,
                "error": (
                    "no session_id resolved — pass session_id explicitly "
                    "or call `ai_session(mode='connect', session_id='<id>')` first"
                ),
            }
        from .mcp_server_runtime_helpers import current_calling_agent_context_id
        import os as _os_notifications

        cleared = _rn.dismiss_for_session(
            project_root,
            session_id=sid,
            run_id=run_id or "",
            agent_context_id=current_calling_agent_context_id(project_root),
            lane_id=_os_notifications.environ.get(
                "AIDOCS_EXPERT_LANE_ID",
                "",
            ).strip(),
        )
        return {
            "ok": True,
            "session_id": sid,
            "run_id": run_id or None,
            "cleared": cleared,
        }

    # ── Skill Scanner + Context Compaction tools ──

    # C.20: hidden implementation binding only; ToolSpec owns metadata.
    async def skill_scan(skill_id: str, content: str, kind: str = "") -> str:
        """Scan skill content for security risks (prompt injection, supply chain, capabilities).

        Pass `kind` (e.g. 'doctrine', 'stance', 'skill') so documentation
        scrolls that describe security patterns by design aren't flagged
        as if they ran them. Empty `kind` runs the full scan.
        """
        from .skill_scanner import scan_skill

        result = scan_skill(skill_id, content, kind=kind)
        return result.summary()

    from . import tool_interface as _ti_c20_skill_scan

    _ti_c20_skill_scan.register_impl("skill_scan", skill_scan)

    @server.tool()
    async def context_budget_check(session_id: str = "") -> str:
        """Check context budget for a session — journal size, estimated tokens, recommendations."""
        from .context_compaction import check_context_budget

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        session_path = project_root / ".MEMORY" / "sessions" / sid
        result = check_context_budget(session_path, sid)
        return result.to_dict()

    @server.tool()
    async def context_compact(session_id: str = "", keep_recent: int = 10) -> str:
        """Compact session context — extract key decisions, prune old journal entries. Resets token counters (new context window)."""
        from .context_compaction import compact_session_context

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        session_path = project_root / ".MEMORY" / "sessions" / sid
        result = compact_session_context(session_path, sid, keep_recent=keep_recent)
        # Reset token usage — compaction means new context window
        try:
            from .execution_index_store import ExecutionIndexStore

            # #885: a watermark, not a DELETE. Compaction means a new context
            # window, which is a reason to stop COUNTING the old tokens -- not
            # a reason to remove the append-only rows that recorded them.
            reset = ExecutionIndexStore().reset_token_usage_counter(
                project_root,
                session_id=sid,
                reason="context_compact — new context window",
                actor="context_compact",
            )
            result_dict = result.to_dict()
            result_dict["tokens_reset"] = reset["event_id"]
        except Exception:
            result_dict = result.to_dict()
            result_dict["tokens_reset"] = 0
        return result_dict

    # ── Edit History / Rollback tools ──

    @server.tool()
    @renders_as("list", title="edit history")
    async def edit_history_list(file_path: str = "", session_id: str = "", limit: int = 20) -> Any:
        """List recent file edits for rollback. Optionally filter by file or session."""
        from .edit_history import EditHistoryStore

        project_root = _project_root()
        store = EditHistoryStore()
        edits = store.list_edits(
            project_root,
            file_path=file_path or None,
            session_id=session_id or None,
            limit=limit,
        )
        return {"edits": [e.to_dict() for e in edits]}

    @server.tool()
    @renders_as("list", title="edit history grep")
    async def edit_history_grep(
        pattern: str,
        in_old: bool = True,
        in_new: bool = True,
        file_glob: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> Any:
        """Forensic grep across edit history for old/new content.

        Bonus 2026-04-19. "What changed this line last?" used to be a
        manual journal trawl — this exposes a SQL LIKE scan over the
        existing audit trail. file_glob uses SQL LIKE syntax (% wildcard).
        """
        from .edit_history import EditHistoryStore

        project_root = _project_root()
        edits = EditHistoryStore().grep_edits(
            project_root,
            pattern=pattern,
            in_old=bool(in_old),
            in_new=bool(in_new),
            file_glob=file_glob or None,
            session_id=session_id or None,
            limit=int(limit),
        )
        return {"edits": [e.to_dict() for e in edits]}

    @server.tool()
    @renders_as("list", title="files touched")
    async def files_touched(session_id: str = "") -> Any:
        """Summary of all files modified in this session — who edited what, how many times."""
        from .edit_history import EditHistoryStore

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        summary = EditHistoryStore().files_touched_summary(project_root, session_id=sid or None)
        return {"files": summary, "total": len(summary)}

    @server.tool()
    @renders_as("list", title="tool catalog")
    async def mcp_tool_catalog(
        name_contains: str = "",
        include_schema: bool = False,
        limit: int = 200,
    ) -> Any:
        """Self-introspection: list all registered MCP tools.

        Bonus 2026-04-19. Gap: agents using deferred-tool patterns burn
        ToolSearch calls just to discover what's available. This returns
        the canonical catalog from FastMCP's component registry. Pass
        name_contains to filter; include_schema=True attaches each
        tool's parameter shape (heavier — default off).
        """
        tools = _registered_tools(server) or []
        needle = name_contains.strip().lower()
        out: list[dict[str, Any]] = []
        for t in tools:
            name = str(getattr(t, "name", "") or getattr(t, "_name", "") or "")
            if not name:
                continue
            if needle and needle not in name.lower():
                continue
            entry: dict[str, Any] = {
                "name": name,
                "description": (
                    str(getattr(t, "description", "") or "").strip().splitlines()[0]
                    if getattr(t, "description", None)
                    else ""
                )[:240],
            }
            if include_schema:
                schema = getattr(t, "input_schema", None) or getattr(t, "parameters", None)
                if schema is not None:
                    entry["schema"] = schema
            out.append(entry)
            if len(out) >= int(limit):
                break
        out.sort(key=lambda e: str(e.get("name", "")))
        return {"total": len(out), "tools": out}

    @server.tool()
    @renders_as("status", title="session badge")
    async def session_status_badge(session_id: str = "") -> Any:
        """Compact session-state badge for editor status bars.

        Bonus 2026-04-19. Returns {active, session_id, current_task,
        last_tool, last_journal_at, lane_workers_running} — the minimum
        an editor needs to render "🟢 working on X" or "🔴 blocked"
        without 3+ separate calls. ~200B response.
        """
        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        if not sid:
            return {"active": False, "session_id": None}
        result: dict[str, Any] = {"active": True, "session_id": sid}
        try:
            gate = hub.query_gate.get(project_root, sid) or {}
            result["last_tool"] = gate.get("last_tool")
            result["current_lane_id"] = gate.get("current_lane_id")
        except Exception:
            pass
        try:
            entries = hub.sessions.read_journal(project_root, sid) or []
            if entries:
                last = entries[-1]
                result["last_journal_at"] = str(last.get("timestamp", ""))
                result["last_journal_kind"] = str(last.get("action_kind", ""))
        except Exception:
            pass
        try:
            session = hub.sessions.read_session(project_root, sid)
            if session and getattr(session, "sections", None):
                state = session.sections.get("State", []) or []
                for line in state:
                    s = str(line).lstrip("- ").strip()
                    if s:
                        result["current_task"] = s[:120]
                        break
        except Exception:
            pass
        try:
            from .session_lane_agents_store import SessionLaneAgentsStore

            workers = (
                SessionLaneAgentsStore().get_lane_agents(
                    project_root,
                    session_id=sid,
                    state_filter="running",
                )
                or []
            )
            result["lane_workers_running"] = len(workers)
        except Exception:
            result["lane_workers_running"] = 0
        return result

    # ── Semantic Search tools ──

    # C.20: hidden implementation binding only; ToolSpec owns metadata.
    @renders_as("list", title="semantic search")
    async def semantic_search(query: str, limit: int = 10) -> Any:
        """Search code by meaning, not just keywords. Finds 'authentication flow' even if code doesn't contain those exact words.

        Requires sentence-transformers: pip install sentence-transformers
        Run semantic_index_sync first to build the index.
        """
        from .semantic_search import index_status, search

        status = index_status(_project_root())
        if not status.get("available"):
            return {
                "results": [],
                "error": "sentence-transformers not installed.",
                "install": "pip install sentence-transformers",
                "hint": "After installing, run semantic_index_sync to build the index.",
            }
        results = search(_project_root(), query, limit=limit)
        if not results:
            return {
                "results": [],
                "hint": "No results. Run semantic_index_sync to build the index.",
            }
        return {"results": results, "total": len(results)}

    from . import tool_interface as _ti_c20_semantic

    _ti_c20_semantic.register_impl("semantic_search", semantic_search)

    @server.tool()
    @renders_as("status", title="semantic index sync")
    async def semantic_index_sync(max_files: int = 500) -> Any:
        """Build semantic search index from code files. Embeds file contents for meaning-based search."""
        from .semantic_search import sync_from_code_index

        return sync_from_code_index(_project_root(), max_files=max_files)

    @server.tool()
    @renders_as("status", title="semantic index")
    async def semantic_index_status() -> Any:
        """Check semantic search index status — model availability, indexed files/chunks."""
        from .semantic_search import index_status

        return index_status(_project_root())

    @server.tool()
    @renders_as("status", title="edit rollback")
    async def edit_rollback(edit_id: str = "") -> Any:
        """Rollback a specific edit — restore the file to its state before the edit."""
        from .edit_history import EditHistoryStore

        if not edit_id:
            return {"success": False, "message": "edit_id is required."}
        project_root = _project_root()
        result = EditHistoryStore().rollback(project_root, edit_id)
        return result.to_dict()

    # ── Code Runner — unified detached runner lives in server_run_tools
    # (ai_run + ai_run_status + ai_run_output + ai_run_kill).
    # The old sync wrappers (code_run_command, code_test_project,
    # code_build_project) were deleted 2026-04-20 — all shell work goes
    # through the single detached path so agents never get stuck
    # reading a sync command's output. bash_policy judge + test/build
    # renderer pipeline live on the unified spawn.

    # ── Subagent / Lane Agent Managed Mode ──

    # `eager=True` alone does NOT reach the host: since the 2026-04-24
    # "trust Claude Code's native ToolSearch" policy, _filtered_list_tools
    # keeps every tool registered + enabled and the eager/deferred tag is
    # introspection metadata only. `anthropic/alwaysLoad` is the pin the
    # host honours. ai_session is the ONE tool every agent must reach
    # before it can do anything else — and an unbound agent cannot
    # ToolSearch for its schema, because the gate that refuses it is the
    # gate ai_session exists to clear. It ships with its schema or it is
    # not a door (2026-08-13; same law as tool_gate_service.BOOTSTRAP_EXEMPT).
    @server.tool(eager=True, meta={"anthropic/alwaysLoad": True})
    @renders_as("status", title="ai_session")
    async def ai_session(
        mode: str,
        session_id: str = "",
        title: str = "",
        goal: str = "",
        owner: str = "",
        scope: str = "-",
        status: str = "active",
        predecessor_session_id: str | None = None,
        agent_id: str = "",
        run_id: str = "",
        claim_mode: str = "active",
        stale_after_minutes: int = 30,
        patch: dict[str, list[str]] | None = None,
        selected_skills: list[str] | None = None,
        include_code_bundle: bool = False,
        include_tests: bool = False,
        journal_last_n: int = 10,
        confirm_token: str = "",
    ) -> Any:
        """Unified session-lifecycle tool — one tool, ten modes (Empire directive 2026-05-12).

        Modes:
          connect       — bind the calling agent to a session (paved-road entry).
                          Optional: session_id (else uses last-bound/most-recent).
          list          — list sessions from project-local /.MEMORY/sessions/.
          create        — create a new session. Required: title.
          claim         — add advisory claim. Required: session_id, agent_id, run_id.
                          Optional: claim_mode ('active' default).
          claim_status  — list claims + staleness. Required: session_id.
          release       — release a claim. Required: session_id, agent_id.
          update        — update structured SESSION.md sections. Required: session_id, patch.
          resume        — collaboration-oriented resume bundle. Required: session_id.
          skills_get    — list selected skills. Required: session_id.
          skills_set    — set selected skills. Required: session_id, selected_skills.

        Per-mode required-sets enforced by @modes; runtime branches by `mode`.
        """
        m = (mode or "").strip().lower()
        project_root = _project_root()
        # ONE VERB. `bind` was a synonym of `connect` here until 2026-08-28 —
        # one branch, two schema entries, and two gate lists that had to name
        # both forever. `connect` survives; bind/bound is reserved for a
        # different concept (operator: "a 'bound' project can mean something
        # else (later)").
        #
        # THE IMPL DELIBERATELY DOES NOT STILL ACCEPT `bind`, tempting as a
        # transition window is: confirm_modes is ("connect",), and the gate
        # matches on the mode STRING, so an impl that answered to `bind` would
        # answer WITHOUT the confirmation `connect` must satisfy. A kindness to
        # old callers would have been a bypass of the two-phase.
        if m == "connect":
            # #916 -- A BINDING TOOL IS TWO-PHASE. Operator directive 2026-08-25:
            # "binding tools need to be two-phased".
            #
            # connect/bind REBINDS THE CONTEXT every later tool call operates
            # against -- the same act project_select and ai_project(bind) both
            # gate. It was the one binding door with NO confirmation of any kind:
            # no confirm_token here, and no destructiveHint in the web catalog
            # (outer_gate_catalog.py:304 lists only project_select /
            # session_select), so neither the AIDOCS phase-one nor the host's
            # confirmation card ever fired. Measured from the web agent the same
            # day: ai_session(mode='connect', session_id='phoenix') succeeded
            # silently. One authority change, two doors, one guarded -- the twin
            # pattern aimed at a consent gate.
            #
            # MIRRORS ai_project(bind) DELIBERATELY (:4147-4179) rather than
            # inventing a third confirm mechanism: same _normalize_voice match,
            # same `_error: confirm_required` payload shape, same speakable
            # phrase style. Doctrine XXII -- the sibling binding tool already
            # solved this and its solution is the one to reuse.
            #
            # PER MODE, NOT PER TOOL, and that distinction is the whole design.
            # The spec-level `confirm=TWO_PHASE` gates an ENTIRE spec, and
            # ai_session has ten modes -- list, status, claim_status, resume and
            # the rest are reads that must never pop a card. A confirmation on
            # every call is one the operator learns to clear without reading, and
            # then the one that mattered goes through with the rest (#915 is that
            # failure in another surface). Gating only the modes that REBIND is
            # what keeps the prompt meaningful.
            # GATE SURFACE ONLY, and the carve-out is not laziness -- it is what
            # the control is FOR. The risk is a REMOTE agent silently re-pointing
            # the context; locally, connect IS the bootstrap: `/aidocs`, the
            # SessionStart hook and the paved-road entry all call it, and the
            # operator running `/aidocs` has already performed the deliberate
            # act a confirmation exists to capture. Gating those would demand a
            # phrase to START A SESSION -- test_session_connect_default_path_834
            # calls that regression "the paved road is broken again", and it has
            # been broken before.
            #
            # A confirmation that fires during bootstrap is also the fastest way
            # to teach an operator to echo the phrase reflexively, which would
            # hand back exactly the consent this is meant to secure (#915).
            # THE CONTRACT IS DECLARED, NOT SPELLED HERE. tool_interface's
            # ai_session spec carries gate_confirm=TWO_PHASE,
            # gate_phrase="confirm session bind" and
            # confirm_modes=("connect", "bind"); the phrase and the surface/mode
            # split both come from THAT row. An earlier version hardcoded the
            # phrase right here, which worked but left the registry silent about
            # a confirmation the tool genuinely requires -- a control the SSOT
            # cannot see is one nobody can audit, which is the same twin-pattern
            # complaint #916 was filed about, one layer down.
            #
            # This is the SECOND enforcement point, not a second CONTRACT. The
            # gate transport already refuses at the boundary
            # (_ogt_pt_registry_dispatch); this catches any remote path that
            # reaches the impl without crossing it. Both call the same guard
            # against the same spec, so they cannot drift into disagreeing about
            # the phrase -- which is precisely how the two-door bug in #916
            # happened in the first place.
            from .mcp_server_runtime_helpers import current_gate_principal as _cgp
            from . import tool_interface as _ti
            from .mcp_server_runtime_helpers import (
                current_gate_confirmation as _cgc,
            )

            if isinstance(_cgp(), dict):
                # #939: ASK WHETHER THIS INVOCATION WAS CONFIRMED AT THE
                # BOUNDARY -- do not re-check confirm_token. The transport
                # STRIPS that argument before invoking the impl, so the previous
                # version's re-check always saw None and always refused:
                # ai_session(connect) returned confirm_required to phase two
                # forever. Passing the token through would not help either --
                # the handle is SINGLE-USE and the boundary already consumed it,
                # so no second layer can re-verify the value.
                #
                # So the second enforcement point changed KIND rather than
                # disappearing: an impl reached WITHOUT crossing the boundary
                # sees no confirmation and refuses, which is #916's threat
                # preserved. The ARGUMENT binding is not re-derived here --
                # ConfirmStore already refuses a handle whose args or intent
                # differ, and a second spelling of that rule is the twin
                # pattern this fix exists to remove. Full reasoning on #939.
                _confirmed = _cgc()
                if not (_confirmed and _confirmed[0] == "ai_session"):
                    _spec = _ti.REGISTRY.get("ai_session")
                    _target = (
                        (session_id or "").strip() or "the last-bound/most-recent session"
                    )
                    _refusal = {
                        "_error": "confirm_required",
                        "_detail": (
                            "ai_session is a confirmation-gated action; ask the "
                            "user, then re-invoke with the exact server-issued "
                            "confirm_token"
                        ),
                        "action": f"ai_session {m}",
                        "session_id": (session_id or "").strip(),
                        "summary": (
                            f"About to bind THIS host session to {_target}. Managed "
                            "mode and every later ai_* call will operate against it. "
                            "Ask the user before re-invoking with confirm_token."
                        ),
                    }
                    if _spec is None:
                        _refusal["_detail"] = (
                            "ai_session has no registry spec, so its confirm "
                            "contract cannot be resolved (refusing closed)"
                        )
                    return _refusal
            return await session_connect(session_id=session_id)
        if m == "list":
            summaries = hub.sessions.list_sessions(project_root)
            return [_session_summary_to_dict(item) for item in summaries]
        if m == "create":
            # Authorization boundary (2026-05-25): an agent may create a
            # session ONLY inside an authenticated, authorized project.
            # require_admin = an authenticated operator holding
            # admin.manage_sessions — every flavor, #404 (no local-admin
            # passthrough). This is
            # the SAME shared authorization model as connect/bind — creation is
            # not a side-door. (create_session itself mints SQL membership;
            # owner grant + audit follow.)
            from .permission_catalog import PERM_ADMIN_MANAGE_SESSIONS
            from .project_authority import (
                _authenticated_uid as _auth_uid,
            )
            from .project_authority import (
                audit as _pa_audit,
            )
            from .project_authority import (
                require_admin as _require_admin,
            )

            _auth = _require_admin(
                project_root,
                permission=PERM_ADMIN_MANAGE_SESSIONS,
                operation="session_create",
            )
            if not _auth.get("ok"):
                return {
                    "created": False,
                    "blocked_by": _auth.get("blocked_by"),
                    "error": (
                        f"session create refused: {_auth.get('reason')} "
                        f"(agents may create sessions only in an authenticated, "
                        f"authorized project)"
                    ),
                }
            # TWO-PHASE AS WELL AS PERM-BASED (operator ruling 2026-08-29:
            # "both create and delete should be two-phased and perm-based").
            # create had only the permission above; a confirmation and an
            # authorization answer different questions — "may you" versus "did
            # someone actually ask for this" — and neither substitutes for the
            # other. On the gate this is the minted handle (confirm_modes);
            # locally it is the spoken phrase, which is the same debt recorded
            # at the delete branch below.
            # IMPORTED HERE, not borrowed from the connect branch. `_cgc` is
            # bound by a function-level import inside `if m == "connect"`, which
            # makes it a LOCAL of ai_session — so referencing it from any other
            # branch raises UnboundLocalError at runtime. It did: create and
            # delete both crashed until this import was added.
            from .mcp_server_runtime_helpers import (
                current_gate_confirmation as _cgc_new,
            )

            _confirmed_new = _cgc_new()
            if not (_confirmed_new and _confirmed_new[0] == "ai_session"):
                from .tool_interface import _normalize_voice as _nv_c

                _expected_new = "confirm session create"
                if _nv_c(confirm_token) != _nv_c(_expected_new):
                    return {
                        "_error": "confirm_required",
                        "action": "ai_session create",
                        "session_id": (session_id or "").strip(),
                        "confirm_token": _expected_new,
                        "summary": (
                            f"About to CREATE session {(title or session_id)!r} "
                            f"in {project_root}. Ask the user before "
                            f"re-invoking with confirm_token."
                        ),
                    }
            import re as _re
            from datetime import date as _date

            sid = session_id
            if not sid:
                slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
                sid = f"{_date.today().isoformat()}-{slug}"
            own = owner
            if not own:
                try:
                    managed_state = hub.managed_mode.get_mode(project_root)
                    own = str(managed_state.get("source") or "").strip() or "agent"
                except Exception:
                    own = "agent"
            session = hub.sessions.create_session(
                project_root,
                session_id=sid,
                title=title,
                owner=own,
                goal=goal or title,
                scope=scope,
                status=status,
                predecessor_session_id=predecessor_session_id,
            )
            # Session-owner grant (corpo): system-mint a SESSION-SCOPED admin
            # role for the authenticated creator so require_session stage-2b
            # (session-level RBAC) recognizes them as owner of THIS session
            # only (#404: every flavor authenticates — no local-admin
            # passthrough).
            #
            # Owner-grant TRUTH (2026-05-25): the result reports the grant's
            # real outcome — "not_required" (defensive: no uid resolved),
            # "granted", or "failed".
            # A failed grant in corpo means the session EXISTS (SQL member) but
            # ownership is DEGRADED; we surface that honestly rather than
            # claiming a clean create, and the audit carries the degraded
            # status. The truth is never swallowed.
            owner_grant = "not_required"
            owner_uid = ""
            try:
                _uid = _auth_uid(project_root)
                if _uid:
                    owner_uid = _uid
                    owner_grant = "failed"  # pessimistic until proven granted
                    from .rbac_store import RBACStore

                    _rb = RBACStore()
                    # Mint the LEAST-PRIVILEGE, honestly-named session_owner
                    # role (manage_sessions only) — the name the operator/audit
                    # sees now matches the authority granted. Seed if missing
                    # (idempotent); fall back to admin only if a pre-seal DB
                    # has not been reseeded (admin ⊇ manage_sessions, so the
                    # owner is still authorized — legacy compatibility).
                    _owner_role = _rb.get_role_by_name(project_root, "session_owner")
                    # Re-seed when the ROLE is missing OR the permission CATALOG
                    # has grown since this install was seeded (2026-07-25 fix).
                    #
                    # Previously this keyed ONLY on session_owner being absent, so
                    # an install that already had the role never re-seeded — and a
                    # permission ADDED to the catalog later never reached it. That
                    # is not cosmetic: security.preflight_failsafe (the super_admin
                    # carve-out that turns a forbidden pre-flight verdict into an
                    # advisory instead of a session freeze) was in the catalog but
                    # absent from the DB, so has_permission() returned False and a
                    # PROVEN super_admin was frozen exactly like an anonymous
                    # attacker — repeatedly, with only an operator able to clear it.
                    # seed_rbac is documented idempotent and safe on every boot.
                    _catalog_stale = False
                    try:
                        from .permission_catalog import ALL_PERMISSIONS

                        _catalog_stale = len(_rb.list_permissions(project_root)) < len(
                            ALL_PERMISSIONS
                        )
                    except Exception:
                        _catalog_stale = False
                    if _owner_role is None or _catalog_stale:
                        try:
                            from .permission_catalog import seed_rbac

                            seed_rbac(project_root)
                            _owner_role = _rb.get_role_by_name(project_root, "session_owner")
                        except Exception:
                            _owner_role = _owner_role or None
                    if _owner_role is None:
                        _owner_role = _rb.get_role_by_name(project_root, "admin")
                    if _owner_role is not None:
                        _rb.assign_role_to_user_scoped(
                            project_root,
                            _uid,
                            _owner_role.role_id,
                            scope_type="session",
                            scope_id=sid,
                            authored_by_user_id="__bootstrap__",
                        )
                        # Verify the grant actually took (membership of the
                        # perm), not just that the call did not raise.
                        if _rb.has_permission(
                            project_root,
                            _uid,
                            PERM_ADMIN_MANAGE_SESSIONS,
                            scope_type="session",
                            scope_id=sid,
                        ):
                            owner_grant = "granted"
            except Exception:
                owner_grant = "failed"
            degraded = owner_grant == "failed"
            _pa_audit(
                project_root,
                operation="session_create",
                target=sid,
                status="allowed_degraded" if degraded else "allowed",
                reason=("session_created_owner_grant_failed" if degraded else "session_created"),
            )
            result = {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
                "owner_grant": owner_grant,
            }
            if owner_uid:
                result["owner_user_id"] = owner_uid
            if degraded:
                result["ownership_degraded"] = True
                result["warning"] = (
                    f"session '{sid}' was created and is a SQL member, but the "
                    f"session-owner grant for '{owner_uid}' did NOT take — "
                    f"ownership is degraded; grant the `session_owner` role "
                    f"(admin.manage_sessions) at session scope via the "
                    f"dashboard before relying on it."
                )
            return result
        if m == "claim":
            session = hub.sessions.claim_session(
                project_root,
                session_id,
                agent_id=agent_id,
                run_id=run_id,
                mode=claim_mode,
            )
            return {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            }
        if m == "claim_status":
            claims = hub.sessions.list_claims(
                project_root,
                session_id,
                stale_after_minutes=stale_after_minutes,
            )
            return {"session_id": session_id, "claims": claims}
        if m == "release":
            session = hub.sessions.release_claim(
                project_root,
                session_id,
                agent_id=agent_id,
                run_id=run_id or None,
            )
            return {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            }
        if m == "update":
            session = hub.sessions.update_session(project_root, session_id, patch or {})
            return {
                "session_id": session.session_id,
                "path": str(session.path),
                "sections": session.sections,
            }
        if m == "resume":
            return runtime.session_resume_bundle(
                project_root,
                session_id=session_id,
                include_code_bundle=include_code_bundle,
                include_tests=include_tests,
                journal_last_n=journal_last_n,
            )
        if m == "skills_get":
            return hub.skills.get_selected_skills(project_root, session_id)
        if m == "skills_set":
            return runtime.set_session_skills(project_root, session_id, selected_skills or [])
        if m == "status":
            # WHICH SESSION IS *THIS HOST* BOUND TO — folded from session_current
            # 2026-08-28 (operator: "the agent-level tool for ai_session should
            # read its current session").
            #
            # The GATE answers a DIFFERENT question with the same word: the
            # token's SELECTED session in the caller's tenant. That arm is
            # outer_gate_transport._ogt_pt_ai_session, and this body never runs
            # there — the same two-surface split ai_project has. Answering from
            # managed-mode state on a surface with no host hooks would return
            # empty fields that look like an answer (#935).
            from .mcp_server_runtime_helpers import current_calling_host_session_id

            _host_sid = current_calling_host_session_id()
            _state = hub.managed_mode.get_mode(project_root, host_session_id=_host_sid)
            _bound = str(_state.get("session_id") or "")
            return {
                "session_id": _bound,
                # An explicit boolean, because "" is the honest answer for an
                # unbound host and a caller should not have to infer that from
                # an empty string.
                "managed": bool(_bound),
                "mode": _state.get("mode"),
                "source": _state.get("source"),
                "host_session_id": _host_sid,
                "project_root": str(project_root),
            }
        if m == "delete":
            # NO LOCAL EQUIVALENT, said plainly instead of silently reporting
            # ORG-PERMISSION BASED, not surface-based. OPERATOR RULING
            # 2026-08-28: "ai_session mode delete/create is org perm-based (who
            # has perms to create or delete sessions on the loaded project)."
            #
            # MY FIRST CUT REFUSED THIS LOCALLY as "no local implementation,
            # deletion is an operator act on the dashboard". Wrong axis:
            # `create` two branches up ALREADY runs require_admin on
            # admin.manage_sessions ("every flavor, #404 — no local-admin
            # passthrough"), and the gate arm expresses the same rule through
            # is_org_admin. Deletion is not a different SURFACE from creation;
            # it is the same surface under the same permission.
            import re as _del_re
            import shutil as _del_shutil

            from .permission_catalog import PERM_ADMIN_MANAGE_SESSIONS as _PMS
            from .project_authority import require_admin as _require_admin

            _target_sid = (session_id or "").strip()
            if not _del_re.fullmatch(r"[A-Za-z0-9._-]{1,128}", _target_sid):
                return {
                    "ok": False,
                    "_error": "bad_session_id",
                    "_detail": "session_id must match [A-Za-z0-9._-]{1,128}",
                }
            _auth = _require_admin(
                project_root,
                permission=_PMS,
                operation="session_delete",
            )
            if not _auth.get("ok"):
                return {
                    "ok": False,
                    "blocked_by": _auth.get("blocked_by"),
                    "_error": "not_authorized",
                    "_detail": (
                        f"session delete refused: {_auth.get('reason')} "
                        f"(requires {_PMS} on this project)"
                    ),
                }
            # A PER-MODE, LOCAL-SURFACE CONFIRMATION THE SPEC CANNOT DECLARE, so
            # it lives here and says so rather than pretending to be absent.
            #
            # ON THE GATE THIS IS A MINTED TOKEN, NOT A PHRASE (2026-08-29).
            # confirm_modes now carries ("connect","create","delete"), so a
            # REMOTE delete is confirmed by _ogt_registry_confirm BEFORE this
            # body runs, with a single-use handle bound to this tool and these
            # args. The transport then STRIPS confirm_token — the registry
            # consumed it — which is why this reads the EXECUTION SCOPE and not
            # the argument. Re-reading the argument is #939's unimplementable
            # second check that "always saw None and always refused".
            #
            # THE LOCAL FALLBACK IS STILL A SPOKEN PHRASE, and that is DEBT, not
            # design: #939 established that a published phrase is satisfiable
            # from documentation. It survives here only because the local
            # surface has no minting boundary at all today — see the
            # phrase-removal war filed 2026-08-29. Until that lands, an
            # irreversible local delete asks for something rather than nothing.
            from .mcp_server_runtime_helpers import (
                current_gate_confirmation as _cgc_del,
            )

            _confirmed_del = _cgc_del()
            if not (_confirmed_del and _confirmed_del[0] == "ai_session"):
                from .tool_interface import _normalize_voice as _nv

                _expected_del = "confirm session delete"
                if _nv(confirm_token) != _nv(_expected_del):
                    return {
                        "_error": "confirm_required",
                        "action": "ai_session delete",
                        "session_id": _target_sid,
                        "confirm_token": _expected_del,
                        "summary": (
                            f"About to PERMANENTLY delete session "
                            f"{_target_sid!r} in {project_root}. Ask the user "
                            f"before re-invoking with confirm_token."
                        ),
                    }
            _del_base = (Path(project_root) / ".MEMORY" / "sessions").resolve()
            _del_dir = (_del_base / _target_sid).resolve()
            # CONFINED BY PARENT IDENTITY, not by string prefix — the gate arm's
            # rule reused verbatim (doctrine XXII). A resolved child whose parent
            # is not exactly the sessions base is a traversal, however spelled.
            if _del_base != _del_dir.parent or not _del_dir.is_dir():
                return {
                    "ok": False,
                    "_error": "unknown_session",
                    "_detail": f"no such session {_target_sid!r} in this project",
                }
            try:
                _del_shutil.rmtree(_del_dir)
            except OSError as _del_e:
                return {
                    "ok": False,
                    "_error": "session_delete_failed",
                    "_detail": str(_del_e)[:200],
                }
            return {"ok": True, "deleted": _target_sid, "confirmed": True}
        return {
            "error": (
                f"unknown mode: {mode!r} (valid: connect|status|list|create|delete|"
                "claim|claim_status|release|update|resume|skills_get|skills_set)"
            ),
        }

    @server.tool(eager=True)
    @renders_as("status", title="ai_project")
    async def ai_project(
        mode: str,
        project_root: str = "",
        project_id: str = "",
        confirm_token: str = "",
    ) -> Any:
        """Bind THIS host session to an AIDOCS-enabled project — the local
        mirror of the outer gate's project_select.

        Modes: connect | status | unbind | list | sessions. The connect is keyed per
        host_session_id (cross-user isolated), idle-TTL'd, and re-roots
        every later ai_* call via resolve_project_root(). RBAC-gated via
        project_authority.require_cross_project; escalates on deny. See
        tool_interface.ai_project for the full contract.
        """
        from . import project_bind_service as _pbs
        from .mcp_server_runtime_helpers import current_calling_host_session_id
        from .session_project_bind_store import DEFAULT_BIND_TTL_MINUTES

        m = (mode or "").strip().lower()
        sid = current_calling_host_session_id()
        if m == "status":
            return _pbs.status_project(host_session_id=sid)
        if m == "unbind":
            return _pbs.unbind_project(host_session_id=sid)
        if m == "list":
            from .cross_project_ops import project_list as _plist

            bound = _pbs.status_project(host_session_id=sid).get("bound_project_root")
            res = _plist(include_session_counts=False)
            for proj in res.get("projects", []):
                proj["bound_to_this_session"] = str(proj.get("project_root")) == str(bound)
            res["bound_project_root"] = bound
            return res
        if m == "sessions":
            # FOLDED FROM project_list_sessions 2026-08-28. A CROSS-PROJECT
            # READ: it NAMES its target tree rather than assuming the bound one,
            # and _gate_cross_project enforces "requires an approved relation +
            # permission" on it. So this DELEGATES — listing the sessions
            # directory here would be a second copy of the read WITHOUT the
            # check that makes it safe, which is the twin pattern the whole
            # consolidation exists to remove (doctrine XXII).
            from .cross_project_ops import project_list_sessions as _pls
            from .server_project_admin_tools import _gate_cross_project

            _sess_target = (project_root or "").strip() or str(_project_root())
            _sess_refusal = _gate_cross_project(_sess_target, "project_list_sessions")
            if _sess_refusal is not None:
                return _sess_refusal
            return _pls(_sess_target)
        # RENAMED bind -> connect 2026-08-28 so both twins use ONE verb, and
        # so bind/bound stays free for a different concept (operator: "a
        # 'bound' project can mean something else (later)").
        if m == "connect":
            # #537: the GATE half takes project_id and is served by
            # outer_gate_transport._ogt_pt_ai_project — this body never runs
            # there. Refused here symmetrically, so a caller who mixes the two
            # surfaces' identifiers is told which one THIS surface binds rather
            # than having the argument silently ignored.
            target = (project_root or "").strip()
            wanted_pid = (project_id or "").strip()
            if wanted_pid and not target:
                # UNCHANGED #537 refusal. project_id ALONE is still the gate's
                # identifier and still refused here, so a caller who mixes the
                # two surfaces is told which one THIS surface binds rather than
                # having the argument silently ignored.
                return {
                    "bound": False,
                    "error": (
                        "project_id is the GATE identifier; this local surface binds "
                        "a project_root (a filesystem tree) for THIS host session. "
                        "Pass project_root=<path>, or list trees with "
                        "ai_project(mode='list'). To DURABLY link a local tree to a "
                        "canonical cloud project, pass BOTH project_root and "
                        "project_id."
                    ),
                }
            if wanted_pid and target:
                # THE CLOUD-LINK ACT (local backlog 988). Both identifiers
                # together is the one unambiguous spelling for "this tree IS
                # that cloud project" — which is why it does not overload either
                # single-argument form.
                #
                # TWO FACTS, TWO LIFETIMES, and they are deliberately not merged:
                #   durable    project_root -> (project_id, org_id), in the
                #              registration store, surviving host sessions,
                #              daemon restarts, deploys and reconnects.
                #   temporary  host_session -> project_root, idle-TTL'd, below.
                # Writing the durable fact through the TTL'd store would let a
                # convenience expire an identity.
                from . import local_cloud_link as _lcl
                from .tool_interface import _normalize_voice as _nv_link

                expected_link = "confirm cloud link"
                if _nv_link(confirm_token) != _nv_link(expected_link):
                    return {
                        "linked": False,
                        "_error": "confirm_required",
                        "action": "ai_project connect (cloud link)",
                        "project_root": target,
                        "project_id": wanted_pid,
                        "confirm_token": expected_link,
                        "summary": (
                            f"About to DURABLY register {target!r} as cloud project "
                            f"{wanted_pid!r}. This is not a session bind — it persists "
                            f"across restarts and deploys, and every later identity "
                            f"question about this tree answers with it. The id is "
                            f"VERIFIED against your entitlement before anything is "
                            f"written. Ask the user before re-invoking with "
                            f"confirm_token."
                        ),
                    }
                linked = _lcl.link_project(Path(target), wanted_pid)
                if not linked.get("linked"):
                    return linked
                # Linked. Also perform the ordinary session bind, so `connect`
                # keeps meaning "and point me at it" — reported separately,
                # because they are separate facts.
                linked["session_bind"] = _pbs.bind_project(
                    host_session_id=sid,
                    conductor_root=_project_root(),
                    target_root=target,
                )
                return linked
            if not target:
                return {
                    "bound": False,
                    "error": "project_root is required for mode='connect'",
                }
            # Two-phase confirm (mirrors outer-gate project_select): the
            # agent cannot silently re-root the session — the operator must
            # echo the action phrase. require_cross_project is the AUTHORITY;
            # this is the deliberate-act confirmation on top of it.
            # Voice-friendly (2026-06-21): a speakable, action-bound phrase (the
            # path stays in the human summary, NEVER in the spoken token — paths
            # are never voice-normalized), matched voice-tolerantly.
            from .tool_interface import _normalize_voice as _nv

            expected = "confirm project bind"
            if _nv(confirm_token) != _nv(expected):
                return {
                    "_error": "confirm_required",
                    "action": "ai_project connect",
                    "project_root": target,
                    "confirm_token": expected,
                    "ttl_minutes": DEFAULT_BIND_TTL_MINUTES,
                    "summary": (
                        f"About to bind THIS host session to {target!r} "
                        f"(idle TTL {DEFAULT_BIND_TTL_MINUTES}m). Every later "
                        f"ai_* call will re-root there. Ask the user before "
                        f"re-invoking with confirm_token."
                    ),
                }
            return _pbs.bind_project(
                host_session_id=sid,
                conductor_root=_project_root(),
                target_root=target,
            )
        return {
            "error": (
                f"ai_project: unknown mode {mode!r} "
                "(connect|status|unbind|list|sessions)"
            )
        }

    # Internal helper. Tool surface removed 2026-05-12 — ai_session(mode='connect').
    @renders_as("status", title="managed mode")
    async def session_connect(
        session_id: str = "",
    ) -> Any:
        """Bind to a work session — paved-road entry, three branches.

        CONDUCTOR (no AIDOCS_EXPERT_ID env): activates managed mode for
        the selected session, returns plans-list-with-status (the shelf).
        Conductors pick a scroll via ai_plan(mode="read", name=...).

        LANE WORKER (AIDOCS_EXPERT_ID + AIDOCS_EXPERT_LANE_ID env):
        verifies session_lane_agents row, activates managed mode,
        latches sub_agent flag, returns THIS lane's plan body.
        Workers don't see the topology — their slice is their kingdom.

        ORPHAN WORKER (WORKER_ID without LANE_ID): identity-strict
        refusal. Lane spawns must have both env vars; missing one is
        a misconfigured spawn or env-strip privilege escalation attempt.

        Identity is read internally — never from arguments. Both env
        vars are set by agent_expert_service at spawn time and cannot
        be forged by the agent inside the subprocess.

        Replaces lane_worker_bind + get_lane_plan (paved-road entry,
        2026-05-02). Mirrors the session_select / session_start /
        session_read / plan_connect removal pattern.
        """
        import os as _os

        worker_id_env = _os.environ.get("AIDOCS_EXPERT_ID", "").strip()
        lane_id_env = _os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip()

        # Branch: ORPHAN WORKER
        if worker_id_env and not lane_id_env:
            return {
                "connected": False,
                "error": (
                    "Worker spawn missing AIDOCS_EXPERT_LANE_ID env. "
                    "Identity-strict refusal: lane workers must have "
                    "both AIDOCS_EXPERT_ID and AIDOCS_EXPERT_LANE_ID. "
                    "Missing LANE_ID = misconfigured spawn (or env-strip "
                    "privilege escalation attempt). The conductor must "
                    "respawn with both env vars set."
                ),
            }

        project_root = _project_root()

        # Branch: LANE WORKER
        if worker_id_env and lane_id_env:
            import sqlite3 as _sqlite_lwb

            from ._sqlite_connect import connect as _canonical_connect
            from .execution_index_store import ExecutionIndexStore

            _store_lwb = ExecutionIndexStore()
            _store_lwb.init_db(project_root)
            matching_row: dict[str, str] | None = None
            try:
                # read_only=True: a pure SELECT of the lane-worker registry,
                # run right after init_db has guaranteed the file exists. It
                # also carried the #756 leak — `with sqlite3.connect(p) as c:`
                # commits the transaction and NEVER closes the handle; the
                # canonical connect's factory closes on __exit__.
                with _canonical_connect(
                    str(_store_lwb.db_path(project_root)), read_only=True,
                ) as _c:
                    _c.row_factory = _sqlite_lwb.Row
                    _row = _c.execute(
                        "SELECT worker_id, session_id, lane_id, state "
                        "FROM session_lane_agents "
                        "WHERE worker_id = ? AND lane_id = ?",
                        (worker_id_env, lane_id_env),
                    ).fetchone()
                    if _row is not None:
                        matching_row = {
                            "worker_id": str(_row["worker_id"]),
                            "session_id": str(_row["session_id"] or ""),
                            "lane_id": str(_row["lane_id"] or ""),
                            "state": str(_row["state"] or ""),
                        }
            except Exception:
                matching_row = None
            if matching_row is None:
                return {
                    "connected": False,
                    "error": (
                        f"worker_id {worker_id_env!r} not found in "
                        f"session_lane_agents for lane {lane_id_env!r}. "
                        f"Refusing to bind — the spawn registry has no "
                        f"matching row (spawn aborted or id tampering)."
                    ),
                }
            sid = matching_row["session_id"].strip()
            if not sid:
                return {
                    "connected": False,
                    "error": "worker row has no session_id; cannot bind.",
                }
            # #816: a lane worker is a CALLER like any other — it reaches the
            # daemon through its own shim with its own host session id. Binding
            # only the singleton left the worker unbound against the gate, which
            # reads per-conductor rows, so a worker could be told it connected
            # and then have every gated tool refuse it.
            from .mcp_server_runtime_helpers import current_calling_host_session_id

            _worker_hsid = (current_calling_host_session_id() or "").strip()
            if not resolve_managed_session(
                hub.managed_mode,
                project_root,
                host_session_id=_worker_hsid,
                strict=bool(_worker_hsid),
            ):
                hub.managed_mode.set_mode(
                    project_root,
                    session_id=sid,
                    source="session_connect.lane_worker",
                    host_session_id=_worker_hsid,
                    restamp_singleton=(not _worker_hsid),
                    authenticate_host=True,
                )
            from .protected_file_runtime import latch_sub_agent_call_on

            latch_sub_agent_call_on()
            plans_dir = project_root / ".MEMORY" / "sessions" / sid / "plans"
            matched = None
            if plans_dir.is_dir():
                for plan_path in sorted(plans_dir.glob("*.md")):
                    try:
                        plan_text = plan_path.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    sections = hub.sessions._parse_sections(plan_text)
                    steps_source = sections.get("Steps") or sections.get("Lane graph") or []
                    try:
                        phases, lanes = hub.sessions._parse_lane_aware_steps(steps_source)
                    except Exception:
                        continue
                    for lane in lanes:
                        if lane.lane_id == lane_id_env:
                            matched = (plan_path, phases, lane)
                            break
                    if matched is not None:
                        break
            if matched is None:
                # Delegated single-task lane (no plan required,
                # 2026-07-11): the backlog tracking entry IS the brief.
                # Resolve it BEFORE refusing an unplanned lane_id —
                # otherwise a delegated worker binds into a dead
                # 'lane_id not found in any plan' refusal.
                from types import SimpleNamespace as _DelegatedNS

                from .server_plan_task_tools import delegated_lane_brief

                _dl_brief = delegated_lane_brief(project_root, lane_id_env)
                if _dl_brief is not None:
                    matched = (
                        None,
                        [],
                        _DelegatedNS(
                            lane_id=lane_id_env,
                            phase_id="delegated",
                            depends_on=[],
                            steps=[
                                _DelegatedNS(
                                    status="pending",
                                    text=str(_dl_brief["content"]),
                                ),
                            ],
                            files=[],
                        ),
                    )
            if matched is None:
                if not plans_dir.is_dir():
                    return {
                        "connected": False,
                        "error": (
                            f"plans dir missing: {plans_dir}. Conductor "
                            f"must author a lane-aware plan before "
                            f"dispatching workers."
                        ),
                    }
                return {
                    "connected": False,
                    "error": (
                        f"lane_id '{lane_id_env}' not found in any "
                        f"plan under {plans_dir}. Conductor may have "
                        f"dispatched a lane that isn't declared."
                    ),
                }
            plan_path, phases, lane = matched
            try:
                _clear_fn = getattr(
                    hub.query_gate,
                    "clear_pending_initial_brief",
                    None,
                )
                if callable(_clear_fn):
                    _clear_fn(project_root, sid)
            except Exception:
                pass
            # Phoenix 2026-05-10 (#164 fix): opencode `run` is
            # single-turn. The previous response shape ended with
            # `next: task_begin` — model treated task_begin as the
            # terminal action and the worker exited mid-plan. The
            # imperative below bakes the full execution loop into
            # one response so the single turn carries the whole
            # lane through to task_complete.
            #
            # Phoenix 2026-05-11: drain the lane mailbox here so
            # conductor messages (notably §VIII deny rationales
            # written by lane_resume_dispatcher) surface in the
            # response payload. The CLI bootstrap-as-message arg
            # is dead-letter for opencode (witnessed 2026-05-11);
            # mailbox is the canonical delivery channel. When a
            # message is present, the response shape promotes it
            # above the plan and the imperative text references
            # it explicitly so the model cannot miss the override.
            _mailbox_msg = None
            try:
                from .lane_mailbox_store import LaneMailboxStore

                _mailbox_msg = LaneMailboxStore().take(
                    project_root,
                    worker_id=worker_id_env,
                )
            except Exception:
                _mailbox_msg = None
            _imperative_prefix = ""
            _conductor_directive = None
            if _mailbox_msg and _mailbox_msg.get("prompt"):
                _conductor_directive = {
                    "from": "conductor",
                    "written_at": str(_mailbox_msg.get("written_at") or ""),
                    "message": str(_mailbox_msg.get("prompt") or ""),
                }
                # NAMED A RETIRED TOOL UNTIL 2026-08-17. This prompt told a
                # recycled worker to "re-submit lane_request_completion_review"
                # — a tool RETIRED 2026-05-08 (see the note at :5196: "The
                # §VIII flow now lives inside task_complete itself ... Lane
                # workers call task_complete normally; capture happens
                # automatically"). So the ONE instruction a denied worker
                # receives, on the path it takes ONLY after a conductor sent it
                # back, named a door that does not exist.
                #
                # Law 311bf3e6: a named remedy must be REACHABLE. This is the
                # same defect class as #786 (three named remedies forming a
                # cycle) and #777 (a header teaching an override the script
                # refuses), and it is worse than a missing instruction: the
                # worker spends its recycled run looking for a tool nobody has,
                # then dies "never called task_complete" — which reads as a
                # WORKER fault and is a DISPATCH fault. That misreading cost
                # this project a day of lane debugging.
                #
                # Found by the doctrine symbol-resolution census: doctrine §VIII
                # still describes the retired mechanism, and this is its live
                # code twin. The doctrine half is EVIDENCE for the operator;
                # this half was a bug and is fixed here.
                _imperative_prefix = (
                    "*** CONDUCTOR DIRECTIVE PRESENT *** Read the "
                    "`conductor_directive` field at the top of this "
                    "response BEFORE acting on the plan. The directive "
                    "is BINDING — it supersedes any prior interpretation "
                    "of the plan as already-satisfied. Apply its "
                    "instruction first, then call task_complete with the "
                    "updated work — the completion review is captured "
                    "automatically and the conductor re-reviews from there. "
                    "Do NOT just re-run the plan from scratch and ignore "
                    "the directive — that's the failure mode this "
                    "channel exists to prevent. "
                )
            return {
                "connected": True,
                "session_id": sid,
                "lane_id": lane.lane_id,
                "project_root": str(project_root),
                "conductor_directive": _conductor_directive,
                "imperative": (
                    _imperative_prefix + "EXECUTE THE FULL LANE IN THIS RESPONSE. Do not "
                    "stop mid-plan. Do not announce next steps and "
                    "wait — there is no next turn. The chain is: "
                    "(1) call mcp__aidocs__ai_task(mode='begin') with the "
                    "session_id and a brief goal from the plan "
                    "steps; (2) read each file in `plan.files` "
                    "using ai_get_lines / ai_get_symbol_snippet / "
                    "ai_bundle — these establish read-evidence the "
                    "edit gates require; (3) apply the edits using "
                    "ai_replace (modes anchor/string/symbol/lines) "
                    "or ai_create_file as the work "
                    "demands; (4) if the lane has verification "
                    "commands, run them via ai_run; (5) call "
                    "mcp__aidocs__ai_task(mode='complete') with a result "
                    "summary. ONLY ai_task(mode='complete') ends this turn. "
                    "If you hit a blocker you cannot resolve, call "
                    "ai_task(mode='complete') with a clear blocker summary "
                    "and claimed_done=false rather than stopping "
                    "silently. Do not narrate intentions; act."
                ),
                "next_steps": [
                    "task_begin",
                    "read each file in plan.files",
                    "apply edits",
                    "run verification (if any)",
                    "task_complete",
                ],
                "plan": {
                    "lane_id": lane.lane_id,
                    "phase_id": lane.phase_id,
                    "depends_on": list(lane.depends_on),
                    "steps": [{"status": s.status, "text": s.text} for s in lane.steps],
                    "files": list(lane.files),
                },
            }

        # Branch: CONDUCTOR
        from .agent_memory_epoch import stamp_host_identity
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        cli_session_id = (current_calling_host_session_id() or "").strip()
        # Recovery: claude_hook subprocess updates query_gate.last_host_session_id
        # on each UPS, but its module-level global dies when the hook exits.
        # The MCP server's global is NEVER stamped from the hook side. So
        # when the global is empty, recover from query_gate (which the hook
        # reliably wrote to) and stamp our own global so subsequent tool
        # calls in this same MCP process see it. Per empire's
        # gate-invariants #50/#54 sub-clause: per-conductor mapping is
        # the authoritative path; this is what makes it actually work.
        # Fixed 2026-05-06.
        if not cli_session_id and session_id and session_id.strip():
            try:
                # Direct accessor — gate.get() omits last_host_session_id
                # by design (only returns lane state). Fixed 2026-05-07.
                cli_session_id = hub.query_gate.get_last_host_session_id(
                    project_root,
                    session_id.strip(),
                )
            except Exception:
                cli_session_id = ""
            # #785 OPTION B (operator ruling 2026-08-17) — THE RECOVERY MAY NOT
            # GUESS. last_host_session_id is keyed by the WORK SESSION, not by
            # the window, so when two windows share one session it resolves to
            # whichever wrote LAST. MEASURED 2026-08-17 19:46: a connect issued
            # from b6a187cf bound 74a03862 — the operator's OTHER window —
            # answered connected:true, and left the real caller refused
            # managed_mode_not_active until an unrelated hook happened to bind
            # it. That is most-recently-active-wins: refused as #54 option A
            # ("the mutable last-writer shape this war exists to end") and
            # refused again by #785's ruling.
            #
            # The recovery KEEPS its real case — ONE window, whose id the MCP
            # server's global never received from the hook side — and is
            # refused only where it cannot be right. Binding a stranger is
            # worse than binding nobody: it reports success, so there is no
            # symptom to act on, and _stamp_owned_host_ids then appends the
            # caller's ids to ANOTHER window's chain, which is APPEND-ONLY and
            # can never be un-appended (managed_mode_service.py:276-282).
            if cli_session_id:
                # THE RECOVERED VALUE IS NOT AN IDENTITY, and no roster check
                # can make it one. It is keyed by the WORK SESSION, so it
                # answers "who wrote here last", never "who is calling". An
                # unidentified caller cannot be shown to own it under ANY state
                # of the conductor roster -- with one window it is merely
                # PROBABLY right, which is not a basis for minting an identity.
                # So there is no count to check and no safe branch to keep.
                return {
                    "connected": False,
                    "session_id": session_id.strip(),
                    "project_root": str(project_root),
                    "blocked_by": "host_identity_ambiguous",
                    "error": (
                        f"This request carries no host identity of its own, so the "
                        f"calling window cannot be identified. The only candidate "
                        f"available is the last host session stamped on "
                        f"'{session_id.strip()}', which names whoever wrote there "
                        f"LAST -- not the caller. Refusing rather than binding it: a "
                        f"wrong bind reports success while leaving you refused "
                        f"managed_mode_not_active, and permanently appends your ids "
                        f"to another window's chain. REMEDY: this project reaches "
                        f"the daemon WITHOUT the stdio shim, so no request can carry "
                        f"a stable per-window identity -- regenerate .mcp.json to "
                        f"the shim entry and reconnect (#787)."
                    ),
                }
        if cli_session_id:
            try:
                # #587-A: stamp the SESSION ID **and** the host KIND, and record
                # the pair durably. This site sees the inbound request, so it is
                # where the authoritative kind exists; passing only the session
                # id (as this did) is what left `host_kind` with no source on the
                # stdio path and forced every consumer to sniff the environment.
                stamp_host_identity(project_root, host_session_id=cli_session_id)
            except Exception:
                pass
        conductor_root = project_root
        cross_project_name: str | None = None
        if session_id and session_id.strip():
            clean_sid = session_id.strip()
            try:
                related = hub.related.list_related_projects(conductor_root)
            except Exception:
                related = []
            for entry in related or []:
                rp_name = str(entry.get("name") or "").strip()
                rp_path = str(entry.get("path") or "").strip()
                if not rp_name or not rp_path:
                    continue
                try:
                    rp_root = Path(rp_path)
                    if not rp_root.is_dir():
                        continue
                    rp_sessions = hub.sessions.list_sessions(rp_root)
                except Exception:
                    continue

                def _sid_of(s):
                    return str(
                        getattr(s, "session_id", None)
                        or (s.get("session_id") if isinstance(s, dict) else None)
                        or "",
                    ).strip()

                if any(_sid_of(s) == clean_sid for s in rp_sessions or []):
                    # Cross-project rebind is a privilege boundary, NOT a
                    # silent name-match. Require the target commissioned +
                    # an approved relation + permission (authenticated
                    # RBAC — every flavor, #404). Defeats the confused-deputy
                    # where a session-name match silently moves the
                    # conductor into a more-privileged project.
                    from .project_authority import (
                        require_cross_project_session,
                    )

                    _xp = require_cross_project_session(
                        conductor_root,
                        rp_root,
                        clean_sid,
                        operation="session_connect_cross_project",
                        host_session_id=cli_session_id or "",
                    )
                    if not _xp.get("ok"):
                        return {
                            "connected": False,
                            "blocked_by": _xp.get("blocked_by"),
                            "error": (
                                f"cross-project bind into '{rp_name}' refused: {_xp.get('reason')}"
                            ),
                        }
                    project_root = rp_root
                    cross_project_name = rp_name
                    from . import mcp_server_runtime_helpers as _h

                    _h._last_known_project_root = rp_root
                    break
        # Local bind must target a session that BELONGS to the active
        # project. Cross-project binds were membership-checked above via
        # list_sessions; a non-cross bind with a session_id that isn't
        # this project's is a session-name collision / foreign-session
        # bind attempt — refuse.
        if session_id and session_id.strip() and cross_project_name is None:
            # Bounded self-heal BEFORE the authority gate: an unmigrated
            # legacy session (on-disk, predates the registry, marker absent)
            # is imported once here so require_session — a pure, fail-closed
            # read — recognizes it. After the seal this is a no-op and a
            # non-member stays refused (file presence never becomes authority).
            from .session_membership_store import SessionMembershipStore

            SessionMembershipStore().ensure_member_or_heal(project_root, session_id.strip())
            from .project_authority import require_session

            _ss = require_session(
                project_root,
                session_id.strip(),
                host_session_id=cli_session_id or "",
                operation="session_connect",
            )
            if not _ss.get("ok"):
                return {
                    "connected": False,
                    "blocked_by": _ss.get("blocked_by"),
                    "stage": _ss.get("stage"),
                    "error": (
                        f"session bind refused ({_ss.get('stage')} stage): {_ss.get('reason')}"
                    ),
                }
        _mm_sid, _mm_reason = explain_managed_session(
            hub.managed_mode,
            project_root,
            host_session_id=cli_session_id,
        )
        # KEEP THE DENY DISTINCT FROM THE CONTINUE. The already-active branch
        # below runs its OWN membership guard and answers `session_not_in_project`
        # for a binding that names a session which is not a member. Collapsing a
        # stale bind straight to "unmanaged" would skip that refusal and silently
        # re-bind the ghost, so the reason is routed rather than discarded.
        _mm_stale = _mm_reason.startswith("stale_bind:")
        _mm_bound = _mm_sid or (_mm_reason.split(":", 1)[1] if _mm_stale else "")
        _mm_active = bool(_mm_bound) or _mm_reason == "managed_binding_names_no_session"

        def _build_conductor_payload(
            sid: str,
            *,
            already_active: bool,
        ) -> dict[str, object]:
            try:
                plans_list = hub.sessions.list_session_plans(project_root, sid)
            except Exception:
                plans_list = []
            payload: dict[str, object] = {
                "connected": True,
                "session_id": sid,
                "lane_id": None,
                "project_root": str(project_root),
                "plans": plans_list,
                "message": (
                    f"Managed mode {'active' if already_active else 'activated'} "
                    f"on session {sid}"
                    + (
                        f" in registered project '{cross_project_name}'"
                        if cross_project_name
                        else ""
                    )
                    + ". Use AIDOCS tools."
                ),
            }
            if already_active:
                payload["already_active"] = True
            if cross_project_name:
                payload["cross_project"] = cross_project_name
            # #785 DOOR 2 (2026-08-18) -- SAY WHEN THE RECEIPT IS NOT A BINDING.
            #
            # With no host identity on the request, get_mode(root, "") does not
            # answer "is the CALLER bound?": managed_mode_service.py:288 guards
            # its strict unresolvable_host_session refusal on `if sid:`, so an
            # EMPTY id is not a named caller and falls through to the SINGLETON
            # fallback at :311, which reports active=True whenever ANY window
            # bound the singleton. The already-active branch then restamps with
            # an empty host id, and #786's verification is itself guarded on
            # `if cli_session_id` -- skipped for exactly the callers that cannot
            # be bound. So connect answered already_active: true to a caller
            # every gated tool refuses.
            #
            # OBSERVED LIVE 2026-08-17, twice: connect answered
            # already_active: true and admin_clear_reconnect reported
            # cleared+restamped, while every gated tool stayed refused (issue
            # 20260817T205357Z-1501240d). Three named remedies, three SUCCESS
            # receipts, no change. The report's own conclusion: "success leaves
            # no symptom to act on".
            #
            # WHY THIS WARNS RATHER THAN REFUSES. Refusing every identity-less
            # connect was tried first and REGRESSED TWO PINNED CONTRACTS
            # (test_tool_already_active_branch_clears_query_gate,
            # test_session_connect_rebinds_when_cross_project_authorized): a
            # shim-less project would be unable to connect AT ALL, which is
            # every existing install until its .mcp.json is regenerated. That is
            # a breaking change and an operator's call, not a fix to smuggle in
            # under a bug number. The DEFECT here is the silence, not the bind:
            # the receipt claimed a binding it had not made. It now says so.
            if not cli_session_id:
                payload["host_identity"] = "absent"
                payload["per_conductor_bound"] = False
                payload["warning"] = (
                    "NO PER-CONDUCTOR BINDING WAS WRITTEN FOR THIS REQUEST. "
                    "Managed mode is active for the PROJECT (the singleton), "
                    "which means some window is bound -- not that you are. The "
                    "PreToolUse gate reads the per-conductor row for the CALLING "
                    "host session, so gated tools may refuse this session with "
                    "managed_mode_not_active even though this receipt says "
                    "connected. CAUSE: this project reaches the daemon without "
                    "the stdio shim, so the request carries no stable per-window "
                    "identity. REMEDY: regenerate .mcp.json to the shim entry "
                    "and reconnect (#787)."
                )
            # #816 -- THE BIND VERIFIED AND THE NEXT CALL STILL REFUSED.
            #
            # Door 2 above covers an ABSENT identity, and the recovery path
            # refuses an AMBIGUOUS one. Neither covers the case the operator hit:
            # an identity that is PRESENT, binds cleanly, verifies as
            # per_conductor -- and is still not who calls next.
            #
            # current_calling_host_session_id() falls back to a PROCESS-GLOBAL
            # stamp when the request carries no identity of its own. That
            # fallback is deliberate (legacy single-window stdio hosts depend on
            # it) and it is the right answer for attribution. It is the WRONG
            # answer for a PROMISE: the stamp does not travel with the caller, so
            # on a shared daemon it can name a different window, and the next
            # tool call resolves its own identity and finds no row.
            #
            # Every existing check asks "did I bind what I saw?" -- all of them
            # pass here, because the bind is genuinely correct for the id it was
            # given. None asks "will what I saw still be me next time?". That is
            # the whole 20-minute confusion: connected:true, then
            # managed_mode_not_active, with nothing wrong in between.
            #
            # WARNS RATHER THAN REFUSES, for the reason recorded in Door 2 above:
            # refusing identity-less connects regressed two pinned contracts and
            # would lock out every install whose .mcp.json predates the shim.
            # The defect is the silence, not the bind.
            elif cli_session_id:
                try:
                    from .mcp_server_runtime_helpers import (
                        request_scoped_host_session_id,
                    )

                    _req_sid = request_scoped_host_session_id()
                except Exception:
                    _req_sid = ""
                if not _req_sid:
                    payload["host_identity"] = "process_stamp"
                    payload["per_conductor_bound"] = True
                    payload["warning"] = (
                        f"BOUND '{cli_session_id}', BUT THIS REQUEST CARRIED NO "
                        f"IDENTITY OF ITS OWN. That id came from the server's "
                        f"process-global stamp, which does not travel with you: "
                        f"your NEXT tool call resolves its own identity and may "
                        f"present a different one, and the gate reads the "
                        f"per-conductor row for THAT id -- so gated tools can "
                        f"refuse managed_mode_not_active even though this receipt "
                        f"says connected. VERIFY with ai_agents: if it reports "
                        f"your caller is 'provably connected but has no binding', "
                        f"this is that. CAUSE: the request reached the daemon "
                        f"without a per-window identity header, so there is no "
                        f"stable id to bind. REMEDY: ensure .mcp.json runs the "
                        f"stdio shim and RECONNECT the MCP server so the running "
                        f"process is the shim (a regenerated file does not change "
                        f"an already-spawned one) -- #787/#816."
                    )
                else:
                    # #816 DOOR 3 -- THE TRANSPORT IDENTITY IS STALE.
                    #
                    # Doors 1 and 2 cover an ABSENT identity and a PROCESS-STAMP
                    # identity. Neither covers the case measured 2026-08-20: an
                    # identity that is PRESENT, request-scoped, and simply NOT
                    # THE ONE THE GATE WILL EVALUATE.
                    #
                    # Two independent channels carry "who is calling":
                    #   TRANSPORT  stdio_shim captures CLAUDE_CODE_SESSION_ID AT
                    #              SPAWN and relays that snapshot forever. The
                    #              shim lives as long as the window and NO AIDOCS
                    #              COMMAND CAN RESTART IT (#833 layer 3).
                    #   HOOK       claude_hook runs per tool call with the host's
                    #              REAL live conversation id and stamps it into
                    #              query_gate.last_host_session_id on every UPS
                    #              (see :4361-4364). The PreToolUse gate reads
                    #              the per-conductor row for THIS id.
                    #
                    # While they agree nothing is wrong. When the host's session
                    # id changes under a live shim they diverge PERMANENTLY, and
                    # connect binds one while the gate reads the other. Measured
                    # on this project: two live conductor rows for one session,
                    # 9037bb6a written by session_connect_restamp and b6a187cf
                    # (the id every refusal named) written by
                    # user_prompt_submit_auto_activate. Every gated tool refused
                    # for forty minutes while connect answered connected:true.
                    #
                    # WARN, NEVER REFUSE -- settled twice already. #785 door 2
                    # tried refusing and regressed two pinned contracts; #833
                    # refused a stale transport and turned a silent problem into
                    # a dead window "which the caller cannot fix mid-request".
                    # And NEVER bind the hook-observed id as well: that is the
                    # identity substitution #785 Option B forbids outright.
                    #
                    # The defect is the SILENCE. #833 again: "the remedy turned
                    # out to be cheap and reachable all along -- /mcp reconnect
                    # respawns the shim and costs nothing. The defect was that
                    # NOTHING TOLD ANYONE TO DO IT."
                    try:
                        _hook_seen = (
                            hub.query_gate.get_last_host_session_id(project_root, sid)
                            or ""
                        ).strip()
                    except Exception:  # noqa: BLE001 - a disclosure must never break connect
                        _hook_seen = ""
                    # An EMPTY stamp is UNPROVABLE, not divergent: a session's
                    # first connect legitimately precedes any UPS. Same asymmetry
                    # managed_mode_service.py:429-434 applies to the host-id chain.
                    if _hook_seen and _hook_seen.lower() != cli_session_id.strip().lower():
                        # CORRECTED 2026-08-20 13:30 after this fired a FALSE
                        # POSITIVE in production. The first version set
                        # gate_will_evaluate=_hook_seen and stated the gate would
                        # refuse naming that id. MEASURED on the post-restart
                        # replay: it named 74a03862 (a stamp from a window three
                        # days old, still the most recent on this session's row)
                        # while the gate evaluated b6a187cf and PASSED it.
                        #
                        # last_host_session_id is NOT the gate's input. The gate
                        # reads the id in the HOOK PAYLOAD -- the live
                        # conversation id -- and the last stamp merely happens to
                        # be the most recent one written, which can belong to
                        # another window entirely. I used a proxy for the reader's
                        # input without checking it WAS the reader's input.
                        #
                        # So this now reports the FACT (two channels disagree) and
                        # never predicts the verdict. A specific, scary, wrong
                        # warning on a healthy connect is worse than silence: it
                        # teaches the operator to ignore the one that matters.
                        payload["host_identity"] = "transport_differs_from_last_stamp"
                        payload["per_conductor_bound"] = True
                        payload["last_hook_stamp"] = _hook_seen
                        payload["warning"] = (
                            f"NOTE -- the identity this request carried is not the "
                            f"last one an authenticated hook stamped. Bound "
                            f"'{cli_session_id}' (relayed by the stdio shim, which "
                            f"captured it AT SPAWN); the last authenticated hook "
                            f"stamp for session '{sid}' is '{_hook_seen}'. THIS IS "
                            f"OFTEN HARMLESS -- the last stamp can simply belong to "
                            f"an older window of the same session, and the gate "
                            f"reads the id in the LIVE HOOK PAYLOAD, which this "
                            f"receipt cannot see. No prediction is made about the "
                            f"verdict. BUT IF your next gated call is refused "
                            f"managed_mode_not_active naming an id you do not "
                            f"recognise, this mismatch is why, and re-running "
                            f"connect will not help: it binds the same relayed id "
                            f"again. CAUSE: this "
                            f"window's session id changed after the shim was "
                            f"spawned; the shim relays a snapshot and cannot be "
                            f"restarted by any AIDOCS command (#833 layer 3). "
                            f"REMEDY: /mcp reconnect -- it respawns the shim, "
                            f"which recaptures the current id. VERIFY with "
                            f"ai_agents: MORE THAN ONE live conductor row for "
                            f"this session, exactly one of which has "
                            f"live_source 'caller', is the signature -- the "
                            f"others are older windows still passing their pid "
                            f"liveness check. Their 'source' values vary "
                            f"(session_connect, session_connect_restamp and "
                            f"chain_attested_heal have all been observed), so "
                            f"no particular pair of sources confirms or rules "
                            f"out this state (#816)."
                        )
            return payload

        if _mm_active:
            sid = session_id or _mm_bound
            if sid:
                # Stale active bind guard: managed mode may report active for a
                # session that is NOT a member (e.g. a pre-registry bind that
                # survived restarts via restamp). Heal once if it is an
                # unmigrated legacy session; if it still isn't a member, refuse
                # to restamp a ghost the authority gate would reject.
                from .session_membership_store import SessionMembershipStore

                if not SessionMembershipStore().ensure_member_or_heal(project_root, sid):
                    return {
                        "connected": False,
                        "session_id": sid,
                        "project_root": str(project_root),
                        "blocked_by": "session_not_in_project",
                        "stale_bind": True,
                        "error": (
                            f"managed mode was bound to '{sid}' but it is not "
                            f"a member of this project — refusing to restamp a "
                            f"stale bind. Connect an existing session, or run "
                            f"`aidocs migrate-control-authority` if this is an "
                            f"unmigrated legacy session."
                        ),
                    }
                try:
                    # #786: authenticate_host=True. This IS an explicit bind
                    # boundary -- membership and RBAC are settled above -- so a
                    # first-time window must ENROL rather than have its id
                    # silently dropped. Without it, set_mode's C1b block takes
                    # the else branch (managed_mode_service.py:478), blanks
                    # host_session_id, skips the per-conductor write at :485,
                    # and writes only the singleton. The PreToolUse gate reads
                    # ONLY the per-conductor row (:288-300 gives a caller that
                    # NAMES a host session with no row active=False), so connect
                    # answered green for a bind no tool would ever honour.
                    _restamped = hub.managed_mode.set_mode(
                        project_root,
                        session_id=sid,
                        source="session_connect_restamp",
                        host_session_id=cli_session_id,
                        authenticate_host=True,
                    )
                    # #786 (C): REPORT THE BIND THAT ACTUALLY LANDED. set_mode
                    # may still decline the per-conductor write -- a per-request
                    # transport token is refused by design (#599 C1) -- and the
                    # old code returned "connected": True without ever looking.
                    # An unbound caller that is TOLD it is bound has no symptom
                    # to act on; it just watches every tool refuse.
                    if cli_session_id and str(
                        (_restamped or {}).get("resolved_via") or "",
                    ) != "per_conductor":
                        return {
                            "connected": False,
                            "session_id": sid,
                            "project_root": str(project_root),
                            "blocked_by": "host_identity_not_bound",
                            "error": (
                                f"connect wrote no per-conductor binding for host "
                                f"session '{cli_session_id}' on '{sid}', so every "
                                f"gated tool will refuse managed_mode_not_active. "
                                f"The identity was rejected as a per-request "
                                f"transport token, which is not an actor. If this "
                                f"window reaches the daemon without the stdio shim "
                                f"there is no stable host identity to bind (#787)."
                            ),
                        }
                except Exception as _restamp_err:
                    # Don't silently lie about a successful bind when the
                    # persist failed. Pre-2026-05-06 this was `pass` and
                    # caused the split-bind bug (singleton stale, agent
                    # told "ok"). Surface the error instead.
                    return {
                        "connected": False,
                        "session_id": sid,
                        "project_root": str(project_root),
                        "error": (
                            f"session_connect could not restamp managed_mode: "
                            f"{_restamp_err!r}. Binding NOT persisted; "
                            f"downstream session-bound tools will see the "
                            f"previous (stale) session. Investigate "
                            f"managed_mode.set_mode."
                        ),
                        "persist_failed": True,
                    }
                try:
                    hub.query_gate.clear_requires_reconnect(
                        project_root,
                        sid,
                    )
                except Exception:
                    pass
            hub.query_gate.set(project_root, sid, current_lane_id=None)
            from .protected_file_runtime import set_sub_agent_call

            set_sub_agent_call(False)
            try:
                # ProjectIndexSitter is the single owner of external-file
                # freshness (the legacy folder_sitter watcher is retired).
                from .project_index_sitter import ensure_index_sitter

                ensure_index_sitter(project_root, hub)
                # Backlog auto-sync: continuous git-backed replication + convergent
                # display ids (operator directive 2026-07-20). Fail-open.
                from .backlog_sync_sitter import ensure_backlog_sync

                ensure_backlog_sync(project_root, hub)
            except Exception as _exc:
                # #12 (Empire 2026-06-20): do NOT swallow silently — a sitter that
                # fails to start means the index goes unmaintained (the ai_find
                # cold-timeout root). Log it so the failure is visible.
                import logging

                logging.getLogger("aidocs.index_sitter").warning(
                    "ensure_index_sitter failed at session_connect (already-active): %r",
                    _exc,
                )
            return _build_conductor_payload(sid, already_active=True)
        sid = session_id
        if not sid:
            # list_sessions returns SessionSummary DATACLASSES (slots=True), not
            # dicts -- the 'list' branch above proves it by mapping every item
            # through _session_summary_to_dict. Calling .get() on them raised
            # "'SessionSummary' object has no attribute 'get'" and broke the
            # ENTIRE no-session_id path, which is the documented default: "bind
            # to the most recent active session". Observed live 2026-08-19.
            #
            # The sibling at the cross-project branch already guards this with
            # `s.get(...) if isinstance(s, dict) else None` -- one call site knew
            # the shape was mixed and the other did not, which is the twin
            # pattern this codebase keeps paying for. Normalised through the ONE
            # existing converter rather than adding a third way to read these
            # fields.
            sessions = hub.sessions.list_sessions(project_root)
            summaries = [
                s if isinstance(s, dict) else _session_summary_to_dict(s)
                for s in sessions
            ]
            active = [s for s in summaries if s.get("status") == "active"]
            if active:
                sid = str(active[0].get("session_id") or "")
        if not sid:
            return {
                "connected": False,
                "reason": "No active session found. The conductor should specify session_id.",
            }
        # #786: authenticate_host=True + verify. Same reasoning as the restamp
        # branch above -- this is an explicit bind boundary, so a first-time
        # window ENROLS instead of having its identity dropped, and the caller
        # is told the truth about what landed. The service method
        # (ManagedModeService.connect) has done both since #720; this tool never
        # called it, so the hardening sat on a path production does not take.
        _bound = hub.managed_mode.set_mode(
            project_root,
            session_id=sid,
            source="session_connect",
            host_session_id=cli_session_id,
            authenticate_host=True,
        )
        if cli_session_id and str((_bound or {}).get("resolved_via") or "") != "per_conductor":
            return {
                "connected": False,
                "session_id": sid,
                "project_root": str(project_root),
                "blocked_by": "host_identity_not_bound",
                "error": (
                    f"connect wrote no per-conductor binding for host session "
                    f"'{cli_session_id}' on '{sid}', so every gated tool will "
                    f"refuse managed_mode_not_active. The identity was rejected "
                    f"as a per-request transport token, which is not an actor. "
                    f"If this window reaches the daemon without the stdio shim "
                    f"there is no stable host identity to bind (#787)."
                ),
            }
        try:
            # ProjectIndexSitter is the single owner of external-file freshness
            # (the legacy folder_sitter watcher is retired).
            from .project_index_sitter import ensure_index_sitter

            ensure_index_sitter(project_root, hub)
            # Backlog auto-sync: continuous git-backed replication + convergent
            # display ids (operator directive 2026-07-20). Fail-open.
            from .backlog_sync_sitter import ensure_backlog_sync

            ensure_backlog_sync(project_root, hub)
        except Exception as _exc:
            # #12 (Empire 2026-06-20): do NOT swallow silently — a sitter that fails to
            # start leaves the index unmaintained (the ai_find cold-timeout root).
            import logging

            logging.getLogger("aidocs.index_sitter").warning(
                "ensure_index_sitter failed at session_connect: %r", _exc,
            )
        return _build_conductor_payload(sid, already_active=False)

    @server.tool(eager=True)
    @renders_as("status", title="admin clear reconnect")
    async def aidocs_admin_clear_reconnect(session_id: str = "") -> Any:
        """Clear both reconnect flags in one idempotent call.

        CONDUCTOR-ONLY. Sub-agents cannot call this tool — refuses
        when AIDOCS_EXPERT_ID env var is set (subagent-process
        marker). A trapped sub-agent should report back to the
        conductor via conductor_ask / mailbox and let the conductor
        clear the lane, not self-unbind from its own sandbox.

        The reconnect gate has two triggers:
          * session_query_gate.requires_reconnect (fresh CLI session)
          * aidocs_managed.bound_by_boot_token mismatch (fresh MCP
            server process)

        This tool clears the first and re-stamps the second so the
        next PreToolUse passes. Safe to call repeatedly. Idempotent.

        Use when `session_connect` itself is being refused (a
        future AIDOCS-internal bug). In normal operation you don't
        need this — regular session_connect already does both
        clears in one call.
        """
        # Conductor-only — refuse subagent callers. A subagent with
        # AIDOCS_EXPERT_ID set in its env is by construction a
        # lane-scoped worker; letting it unbind reconnect state
        # could let it escape the lane sandbox. Route via the
        # conductor instead.
        import os as _os_admin

        if _os_admin.environ.get("AIDOCS_EXPERT_ID", "").strip():
            return {
                "ok": False,
                "error": (
                    "aidocs_admin_clear_reconnect is conductor-only. "
                    "Sub-agents cannot self-unbind — if your tools "
                    "are being refused, report to the conductor via "
                    "conductor_ask / conductor_check_response / "
                    "your mailbox, and let the conductor investigate."
                ),
                "blocked_by": "subagent_refused",
            }
        # #816: resolve the CALLING host session. Without it every write below
        # lands on the DEPRECATED project singleton, which no gate reads, and
        # the hatch reports success while the caller stays locked out. This is
        # the same shape #786 fixed in session_start; it was left standing here,
        # on the one tool whose docstring says to use it "when session_connect
        # itself is being refused" — i.e. the last door.
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        _hsid = (current_calling_host_session_id() or "").strip()
        project_root = _project_root()
        managed = hub.managed_mode.get_mode(
            project_root,
            host_session_id=_hsid,
            strict=bool(_hsid),
        )
        sid = session_id or str(managed.get("session_id") or "").strip()
        if not sid and _hsid:
            # A locked-out caller has no per-conductor row, so the strict read
            # above resolves nothing and leaves sid empty — which used to skip
            # both clears entirely and still answer ok:true. Fall back to the
            # project's current session so the hatch has something to bind.
            sid = str(hub.managed_mode.get_mode(project_root).get("session_id") or "").strip()
        cleared: dict[str, Any] = {"tool": "aidocs_admin_clear_reconnect"}

        # Session-scoped fresh-CLI flag.
        if sid:
            try:
                hub.query_gate.clear_requires_reconnect(project_root, sid)
                cleared["query_gate.requires_reconnect"] = "cleared"
            except Exception as exc:
                cleared["query_gate.requires_reconnect"] = f"error: {exc}"

        # Process-scoped boot-token re-stamp.
        if sid:
            try:
                # #816 clause A: bind the CALLER, not the singleton.
                # authenticate_host enrols a window that is a stranger to the
                # session's chain — the state after every restart — which is the
                # whole reason this hatch gets called.
                _restamped = hub.managed_mode.set_mode(
                    project_root,
                    session_id=sid,
                    source="aidocs_admin_clear_reconnect",
                    host_session_id=_hsid,
                    restamp_singleton=(not _hsid),
                    authenticate_host=True,
                )
                # #816 clause B: say what LANDED, never what was attempted. The
                # unconditional "restamped" below this line is what cost the
                # operator forty minutes on 2026-08-20 — it removed the only
                # symptom that would have pointed anywhere useful.
                _landed = (
                    not _hsid
                    or str((_restamped or {}).get("resolved_via") or "") == "per_conductor"
                )
                cleared["managed_mode.bound_by_boot_token"] = (
                    "restamped"
                    if _landed
                    else "NOT restamped: no per-conductor row landed for this "
                    "host session, so the gate will keep refusing "
                    "managed_mode_not_active"
                )
                cleared["per_conductor_bound"] = bool(_landed and _hsid)
            except Exception as exc:
                cleared["managed_mode.bound_by_boot_token"] = f"error: {exc}"

        # Audit event — operator used the escape hatch. Loud by design.
        try:
            hub.execution.record_event(
                project_root,
                event_kind="admin_clear_reconnect",
                source_kind="admin_escape_hatch",
                session_id=sid or None,
                capability_name="aidocs_admin_clear_reconnect",
                action_kind="admin_escape",
                target_entity="reconnect_gate",
                status="cleared",
                payload=dict(cleared),
            )
        except Exception:
            pass

        return {
            "ok": True,
            "session_id": sid or None,
            "project_root": str(project_root),
            "cleared": cleared,
            "message": (
                "Both reconnect flags cleared. If tools still refuse, "
                "the issue is elsewhere (force-wakeup, lane scope, "
                "RBAC) — call aidocs_orchestrate for a full state "
                "diagnostic."
            ),
        }

    # ── ai_preflight — battlefield briefing before architecture-creation
    # work. Phase B-lite (2026-05-14): anti-reinvention guard. Surfaces
    # existing wheels, inspect-first files, known traps, tests, and
    # do-not-create warnings drawn from the kingdom DB (capabilities,
    # code outlines, memory pages, memory links). Pure read, no
    # side effects; returns structured JSON + markdown card.
    @server.tool()
    @renders_as("status", title="ai_preflight")
    async def ai_preflight(task: str) -> Any:
        """Battlefield briefing for a task — anti-reinvention pre-check.

        Args:
            task: free-text description of what you intend to build /
                  change / investigate. The richer the description,
                  the better the surface (seed extraction → spaCy
                  parses verbs/nouns/entities, then queries the
                  kingdom index for existing wheels).

        Returns: {"structured": dict, "markdown": str}
          structured keys:
            - existing_wheels: top capabilities matching task seeds
            - inspect_first: top files/symbols to read before writing
            - known_traps: relevant doctrine / caveat / bug-history pages
            - tests_to_run: tests that gate the affected area
            - do_not_create_warnings: explicit "X exists, don't create Y"
            - confidence: low/medium/high based on source coverage
            - missing_info: seeds with no matches (gaps to investigate)

        Per aidocs-doctrine (Empire directive 2026-05-14): preflight is NOT search. It's
        the briefing that prevents reinvention. Run it BEFORE you
        create services / MCP tools / dashboard pages / detectors /
        scripts / config namespaces / schema tables / worker launchers.

        """
        from . import preflight_service as _pf

        project_root = _project_root()
        return _pf.preflight(project_root, task or "")

    # ── ai_vocab / ai_gate_msg — dashboard-facing CRUD for the empire    # ── ai_gate_msg — dashboard-facing CRUD for the empire
    # gate-message strings. Mode-dispatched. Phase 4a (2026-05-14)
    # of the TOML→sqlite migration.
    #
    # ai_vocab was REMOVED 2026-05-16 (Empire directive): dashboard
    # keeps full CRUD via Tauri Rust commands (vocab_list_kinds,
    # vocab_upsert_group, etc.); agents have no legitimate write
    # path for the 8 non-domain vocab kinds (action_token,
    # intent_guard, tool_alias, intent_phrase, skill_trigger,
    # memory_route, tool_discovery, plan_vague_pattern) — those are
    # operator decisions, not memories. Domain anchors get
    # auto-registered through memory_capture when anchor_kind='domain'
    # (see record_memory_capture in server_code_tools.py).

    @server.tool()
    @renders_as("status", title="ai_gate_msg")
    async def ai_gate_msg(
        mode: str,
        key: str = "",
        body: str = "",
        lang: str = "en",
        source: str = "operator",
    ) -> Any:
        """Empire gate-message CRUD — backs the dashboard editor.

        Modes:
          list(lang)              — return all gate messages for one lang
          get(key, lang)          — return one body, with en fallback
          upsert(key, body, lang) — delete + insert; returns {deleted, inserted}
          delete(key, lang)       — drop one message
        """
        from . import intent_tokens_store as _store

        m = (mode or "").strip().lower()
        if m == "list":
            return {"rows": _store.list_gate_messages(lang), "lang": lang}
        if m == "get":
            if not key:
                return {"error": "key required"}
            return {"key": key, "body": _store.get_gate_message(key, lang)}
        if m == "upsert":
            if not key or not body:
                return {"error": "key + body required"}
            return _store.upsert_gate_message(key, body, lang=lang, source=source)
        if m == "delete":
            if not key:
                return {"error": "key required"}
            return {"deleted": _store.delete_gate_message(key, lang)}
        return {"error": f"unknown mode: {mode!r}"}

    # lane_worker_bind + get_lane_plan REMOVED 2026-05-02 (Empire
    # directive - paved-road entry). Their work folded into
    # session_connect: workers call session_connect(); env-detect
    # routes them to the lane-worker branch which does the spawn-
    # registry verify (was lane_worker_bind), the managed-mode
    # bind, the sub_agent latch, and returns the lane plan body
    # in the same response (was get_lane_plan). Single tool,
    # single trip. _build_worker_prompt now returns 'session_connect'.
    # Mirrors session_select / session_start / session_read /
    # plan_connect (all 2026-04-28 through 2026-05-02 paved-road
    # cleanup).

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='enter').
    @renders_as("status", title="conductor mode")
    async def conductor_mode_enter(
        session_id: str = "",
        verbose: bool = False,
        confirm_token: str = "",
    ) -> Any:
        """Enter conductor mode — current agent becomes the session conductor.

        Returns TERSE confirmation by default (role + session id + file
        paths for SESSION.md, journal, and each plan file). Pass
        verbose=True on cold-resume to ALSO get SESSION.md body,
        recent journal tail, first plan body, files-touched summary.

        Previously this tool dumped ~9 KB every call even when the
        conductor was already bound and just re-asserting the role —
        burned context on repeat calls. (2026-04-20 slim.)
        """
        from . import conductor_doctrine as _conductor_doctrine

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)

        # Persist the binding — the function is named conductor_mode_ENTER,
        # not "tell me what session looks like." Prior versions only updated
        # an in-memory dict; the singleton + per-conductor map went untouched,
        # creating split-bind where task_begin saw a stale session even
        # after conductor_mode_enter "succeeded." Fixed 2026-05-06.
        from .agent_memory_epoch import stamp_host_identity
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        _host_sid = current_calling_host_session_id()
        if _host_sid:
            from .conductor_comms import xaacp_claim_seat_authority

            _seat_claim = xaacp_claim_seat_authority(
                project_root,
                session_id=sid,
                role="conductor",
                confirm_token=confirm_token,
            )
            if not _seat_claim.get("ok"):
                return {
                    **_seat_claim,
                    "mode": "conductor",
                    "session_id": sid,
                    "seat_role": "conductor",
                    "seat_claimed": False,
                }
        # RECOVERY REMOVED 2026-08-21 (#859). This used to fall back to
        # query_gate.get_last_host_session_id when the request carried no
        # identity, then stamp_host_identity() it — which writes the process
        # global _calling_conductor_host_session_id, which is the 3rd tier of
        # current_calling_host_session_id, which becomes ai_agents'
        # live_source="caller". So a recovered value LAUNDERED ITSELF into the
        # identity the audit calls "provably live", and from there into
        # msg_role_map, which hands out this very seat.
        #
        # The recovered value cannot be an identity: it is keyed by the WORK
        # SESSION, so it answers "who wrote here last", never "who is calling"
        # (#54 option A, refused again by #785 Option B). MEASURED 2026-08-21:
        # it was FROZEN for three days at another window's uuid, because
        # prompt_mutator:1439 skips its only writer for per_conductor sessions.
        #
        # #672 already made current_calling_host_session_id return the honest ""
        # instead of falling through to that same process global. This block
        # re-introduced the fallthrough one layer up. Now removed: "" means
        # "cannot prove identity", the seat still enters (role registration
        # below is best-effort by contract), and nothing is minted for a
        # window that did not call.
        if _host_sid:
            try:
                # #587-A: stamp identity, not just the session id — see the
                # conductor branch above.
                stamp_host_identity(project_root, host_session_id=_host_sid)
            except Exception:
                pass
            # #215: MAP the bound conductor's host_session_id → 'conductor' in
            # msg_role_map so msg_resolve_caller_role returns its REAL seat.
            # Required now that the resolver fails CLOSED (unmapped → non-seat);
            # without this the legitimate conductor would lose its seat too.
            # Best-effort — the bind proceeds regardless.
            try:
                from .conductor_comms import msg_register_role

                msg_register_role(
                    project_root,
                    _host_sid,
                    "conductor",
                    session_id=sid,
                )
            except Exception:
                pass
        try:
            hub.managed_mode.set_mode(
                project_root,
                session_id=sid,
                source="conductor_mode_enter",
                host_session_id=_host_sid,
            )
        except Exception as _set_err:
            # Surface persist failure rather than silently lying about bind.
            return {
                "mode": "conductor",
                "session_id": sid,
                "project_root": str(project_root),
                "error": (
                    f"conductor_mode_enter could not persist binding: "
                    f"{_set_err!r}. The session WAS NOT bound; downstream "
                    f"task lifecycle calls will see whatever the previous "
                    f"binding was. Investigate managed_mode.set_mode."
                ),
                "persist_failed": True,
            }
        try:
            hub.query_gate.clear_requires_reconnect(project_root, sid)
        except Exception:
            pass

        import time as _t_cme

        _conductor_process["inline"] = {
            "project_root": str(project_root),
            "session_id": sid,
            "entered_at": _t_cme.time(),
        }

        session_path = project_root / ".MEMORY" / "sessions" / sid
        plan_dir = session_path / "plans"
        plan_files: list[str] = []
        if plan_dir.is_dir():
            plan_files = [
                str(p.relative_to(project_root)).replace("\\", "/")
                for p in sorted(plan_dir.glob("*.md"))
            ]

        def _rel_if_exists(path) -> str | None:
            if not path.is_file():
                return None
            return str(path.relative_to(project_root)).replace("\\", "/")

        terse = {
            "mode": "conductor",
            "session_id": sid,
            "project_root": str(project_root),
            "session_md_path": _rel_if_exists(session_path / "SESSION.md"),
            "journal_path": _rel_if_exists(session_path / "journal.md"),
            "plan_files": plan_files,
            # Role text is rendered from the canonical conductor_doctrine
            # (the SINGLE source, tool-truth-enforced by
            # test_conductor_doctrine_tool_truth) — never inline phantom names.
            "responsibilities": _conductor_doctrine.conductor_responsibilities(),
            "next": _conductor_doctrine.conductor_next_hint(),
        }

        # The ROLE (what the head-conductor seat does) auto-dumps on entry.
        # WHO the seat-holder is lives in the sovereign soul, opened only by
        # the Emperor's word — never dumped here.
        try:
            _role = hub.skills.read_role("head-conductor")
            if _role and _role.get("content_text"):
                terse["role"] = _role["content_text"]
        except Exception:
            pass

        if not verbose:
            return terse

        # Verbose path — full dump. Only on explicit opt-in.
        context_parts: list[str] = [
            "== CONDUCTOR MODE ACTIVE ==",
            f"Session: {sid}",
            f"Project: {project_root}",
        ]
        if isinstance(terse.get("role"), str) and terse["role"]:
            context_parts.extend(["", "== HEAD-CONDUCTOR ROLE ==", terse["role"]])
        try:
            session_md = session_path / "SESSION.md"
            if session_md.is_file():
                context_parts.extend(
                    [
                        "",
                        "== SESSION STATE ==",
                        session_md.read_text(encoding="utf-8")[:2000],
                    ],
                )
            journal = session_path / "journal.md"
            if journal.is_file():
                context_parts.extend(
                    [
                        "",
                        "== RECENT JOURNAL ==",
                        journal.read_text(encoding="utf-8")[-1500:],
                    ],
                )
            if plan_dir.is_dir():
                for plan_file in sorted(plan_dir.glob("*.md"))[:1]:
                    context_parts.extend(
                        [
                            "",
                            f"== PLAN ({plan_file.name}) ==",
                            plan_file.read_text(encoding="utf-8")[:2000],
                        ],
                    )
        except Exception:
            pass
        try:
            from .edit_history import EditHistoryStore

            touched = EditHistoryStore().files_touched_summary(
                project_root,
                session_id=sid,
            )
            if touched:
                files_list = "\n".join(f"  {f['file']} ({f['edits']} edits)" for f in touched[:20])
                context_parts.extend(["", "== FILES MODIFIED ==", files_list])
        except Exception:
            pass
        try:
            agent_rules = hub.workflow.get_agent_workflow_rules(project_root)
            if agent_rules:
                rules_text = "\n".join(f"  - {r}" for r in agent_rules)
                context_parts.extend(
                    [
                        "",
                        "== AGENT WORKFLOW RULES ==",
                        rules_text,
                    ],
                )
        except Exception:
            pass

        return {
            **terse,
            "context": "\n".join(context_parts),
        }

    # Internal helper — ai_seat(mode='co-enter'). #167 Phase 4: the
    # co-conductor seat's entry. ROLE auto-dumps; SOUL never does (§XII);
    # managed mode is untouched (the conductor owns the session binding —
    # the co-seat observes and verifies, it does not rebind).
    @renders_as("status", title="co-conductor mode")
    async def coconductor_mode_enter(session_id: str = "", confirm_token: str = "") -> Any:
        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        _host_sid = current_calling_host_session_id()
        if _host_sid:
            from .conductor_comms import xaacp_claim_seat_authority

            _seat_claim = xaacp_claim_seat_authority(
                project_root,
                session_id=sid,
                role="co_conductor",
                confirm_token=confirm_token,
            )
            if not _seat_claim.get("ok"):
                return {
                    **_seat_claim,
                    "mode": "co_conductor",
                    "session_id": sid,
                    "seat_role": "co_conductor",
                    "seat_claimed": False,
                }
        # RECOVERY REMOVED 2026-08-21 (#859) — same laundering edge as
        # conductor_mode_enter above, seating the CO-conductor instead. A value
        # recovered from the work-session slot is "who wrote here last", and
        # msg_register_role below would have granted that window this seat.
        # "" now means "cannot prove identity": seat entry still proceeds
        # (registration is best-effort by contract), it simply maps no seat.
        comms_registered = False
        if _host_sid:
            # Map the seat in msg_role_map so the fail-closed caller-role
            # resolver (#215) returns the REAL seat. Best-effort — seat
            # entry proceeds regardless.
            try:
                from .conductor_comms import msg_register_role

                msg_register_role(
                    project_root,
                    _host_sid,
                    "co_conductor",
                    session_id=sid,
                )
                comms_registered = True
            except Exception:
                comms_registered = False
        terse: dict[str, Any] = {
            "mode": "co_conductor",
            "session_id": sid,
            "project_root": str(project_root),
            "comms_role_registered": comms_registered,
        }
        # #501: the SHIPPED doctrine id is 'co-conductor-doctrine' (bundled
        # payload) — the seat delivers it through the SAME read_role door the
        # head-conductor seat uses; no second delivery mechanism. An
        # operator-inscribed 'co-conductor' role row still wins when present
        # (same manual-beats-bundled_seed authority rule as #479).
        for _role_id in ("co-conductor", "co-conductor-doctrine"):
            try:
                _role = hub.skills.read_role(_role_id)
            except Exception:
                continue
            if _role and _role.get("content_text"):
                terse["role"] = _role["content_text"]
                terse["role_skill_id"] = _role_id
                break
        return terse

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='exit').
    @renders_as("status", title="conductor mode")
    async def conductor_mode_exit() -> Any:
        """Clear the inline-conductor marker set by conductor_mode_enter.

        Leaves any subprocess-spawned conductor untouched. Useful for
        switching the current session's role back to a normal agent
        without restarting the MCP server. No-op when no inline marker
        is set.
        """
        prev = _conductor_process.pop("inline", None)
        if prev is None:
            return {"exited": False, "reason": "no inline conductor was active"}
        return {
            "exited": True,
            "prior_session_id": prev.get("session_id"),
            "prior_project_root": prev.get("project_root"),
        }

    @renders_as("status", title="ai_lane_exit")
    async def ai_lane_exit(session_id: str = "") -> Any:
        """Self-serve escape hatch when the conductor is trapped in a
        worker-left lane scope without waiting for an operator turn.

        Background: when a lane worker calls `session_connect` and
        finishes without clearing the shared `current_lane_id` on the
        session_query_gate row, every subsequent conductor tool call
        sees the worker's lane filter and gets refused with
        'not in lane allowed files'. Until now the only fixes were the
        operator typing 'exit lane' or setting conductor.auto_exit_lane
        in the dashboard — both require the operator to act. This tool
        lets the conductor recover on its own.

        Hard-gated off worker processes via AIDOCS_EXPERT_LANE_ID env
        check — workers must never be able to self-escape their own
        lane sandbox.
        """
        import os as _os_self_exit

        if _os_self_exit.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
            return {
                "exited": False,
                "reason": (
                    "worker processes cannot self-exit a lane — env AIDOCS_EXPERT_LANE_ID is set"
                ),
            }
        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        if not sid:
            return {"exited": False, "reason": "no active session"}
        try:
            current = hub.query_gate.get(project_root, sid) or {}
            prior_lane = str(current.get("current_lane_id") or "").strip()
        except Exception:
            prior_lane = ""
        if not prior_lane:
            return {
                "exited": False,
                "reason": "no lane was bound on this session",
            }
        try:
            hub.query_gate.set(
                project_root,
                sid,
                last_tool="conductor_lane_exit",
                current_lane_id=None,
                lane_exact_paths=[],
            )
        except Exception as exc:
            return {
                "exited": False,
                "reason": f"query_gate update failed: {exc}",
            }
        try:
            hub.execution.record_event(
                project_root,
                event_kind="lane_exit_grant",
                session_id=sid,
                source_kind="conductor_lane_exit_self_serve",
                payload={"prior_lane": prior_lane},
            )
        except Exception:
            pass
        return {
            "exited": True,
            "prior_lane": prior_lane,
        }

    # Phoenix 2026-05-08: lane_request_completion_review RETIRED.
    # The §VIII flow now lives inside task_complete itself via
    # _capture_lane_worker_task_complete (server_plan_task_tools).
    # Lane workers call task_complete normally; capture happens
    # automatically; worker exits clean; conductor reviews via the
    # 📋 surface; ai_review approves silently
    # (no resume) or denies with rationale (host-CLI resume of the
    # worker's host_session_id, full memory restored). Two tools
    # collapsed into one.

    # @server.tool removed (120% clause B): folded into ai_lane(action='review').
    async def ai_review(
        review_id: str,
        verdict: str,
        message: str = "",
    ) -> Any:
        """[CONDUCTOR-SIDE NON-BLOCKING] Decide a pending lane
        completion review. Per aidocs-doctrine §VIII the conductor
        is NEVER blocked — pending reviews surface in the 📋 block
        on every tool call envelope, and this tool resolves one of
        them.

        verdict ∈ {'approved', 'denied'}.

        For 'denied', message SHOULD describe what to fix (or
        reference a plan edit the conductor will make). The lane
        receives the message verbatim and acts on it.

        Returns the updated review row, or {error: ...} when the
        review_id is missing or already decided.

        Phoenix, 2026-05-07.
        """
        from . import lane_completion_review_store as _lcr

        project_root = _project_root()
        if verdict not in ("approved", "denied"):
            return {
                "error": (f"verdict must be 'approved' or 'denied', got {verdict!r}"),
            }
        sid = _resolve_session_id(hub, project_root)
        result = _lcr.submit_verdict(
            project_root,
            review_id=review_id,
            verdict=verdict,
            message=message,
            conductor_session_id=sid or "",
        )
        # Phoenix 2026-05-08: on DENY, spawn the host's resume CLI
        # (claude --resume <hs> / opencode -s <hs> / codex resume <hs>)
        # with a bootstrap prompt carrying the rationale. The
        # captured worker comes back with full session memory + the
        # new directive. APPROVE = no resume; the captured task is
        # truly done. Approve is silent for the worker.
        if verdict == "denied" and isinstance(result, dict) and not result.get("error"):
            try:
                from .lane_resume_dispatcher import resume_worker_on_deny

                resume_info = resume_worker_on_deny(
                    project_root,
                    review_row=result,
                    conductor_message=message,
                )
                result["resume_dispatched"] = bool(resume_info.get("dispatched"))
                if resume_info.get("error"):
                    result["resume_error"] = resume_info["error"]
            except Exception as _resume_exc:
                result["resume_error"] = (
                    f"resume dispatcher failed: {_resume_exc!r}. "
                    "Worker NOT resumed; conductor must intervene."
                )
        # Phoenix 2026-05-10 (#166 fix): on APPROVE, transition the
        # lane state forward → IMPLEMENTATION_DONE → COMPLETED so
        # downstream `depends_on` lanes become runnable. Previously
        # approve recorded the verdict but never closed the lane —
        # the sequence gate stuck and plan_conductor_status kept
        # reporting the lane as runnable. The opencode worker has
        # already exited by the time the conductor approves
        # (single-turn), so there's no live task_complete to
        # transition the lane — approve IS the canonical close.
        if verdict == "approved" and isinstance(result, dict) and not result.get("error"):
            try:
                lane_id_t = str(result.get("lane_id") or "")
                worker_id_t = str(result.get("worker_id") or "")
                work_session_id = ""
                if worker_id_t:
                    import sqlite3 as _sql_t

                    from ._sqlite_connect import connect as _canonical_connect_t
                    from .execution_index_store import ExecutionIndexStore as _EIS_t

                    _store_t = _EIS_t()
                    _store_t.init_db(project_root)
                    # read_only=True — a single SELECT after init_db, and the
                    # same never-closed `with` handle as the site above.
                    with _canonical_connect_t(
                        str(_store_t.db_path(project_root)), read_only=True,
                    ) as _c_t:
                        _c_t.row_factory = _sql_t.Row
                        _row_t = _c_t.execute(
                            "SELECT session_id FROM session_lane_agents "
                            "WHERE worker_id = ? ORDER BY started_at "
                            "DESC LIMIT 1",
                            (worker_id_t,),
                        ).fetchone()
                        if _row_t is not None:
                            work_session_id = str(_row_t["session_id"] or "")
                if not work_session_id:
                    work_session_id = str(result.get("session_id") or "")
                if lane_id_t and work_session_id:
                    from .types import LaneState as _LS_t

                    transitioned: list[str] = []
                    skipped: list[dict[str, str]] = []
                    # Walk the full state graph forward. Lanes may
                    # be stuck at any earlier step because earlier
                    # transitions (dispatch→RUNNING, review→
                    # AWAITING_REVIEW) catch exceptions silently
                    # and don't always fire. Each transition that
                    # is invalid for current state is skipped; the
                    # ones that fit advance the state. #303: record
                    # WHY each hop is skipped so a PARTIAL walk that
                    # stops short of COMPLETED is not silent.
                    for target in (
                        _LS_t.READY,
                        _LS_t.RUNNING,
                        _LS_t.AWAITING_REVIEW,
                        _LS_t.IMPLEMENTATION_DONE,
                        _LS_t.COMPLETED,
                    ):
                        try:
                            runtime._conductor_state.transition_lane(
                                project_root,
                                work_session_id,
                                lane_id_t,
                                target,
                            )
                            transitioned.append(target.value)
                        except Exception as _hop_exc:
                            skipped.append(
                                {"target": target.value, "reason": repr(_hop_exc)[:200]},
                            )
                    result["lane_transitioned_to"] = transitioned
                    # #303: a partial close (COMPLETED not reached) must be
                    # VISIBLE. The #166 guard only catches a WHOLESALE failure
                    # (outer except → lane_transition_error); a walk that
                    # advanced some hops then got stuck reported a silently
                    # short lane_transitioned_to. Surface the incomplete flag +
                    # the skipped-hop reasons so the conductor sees the stuck
                    # hop, not a silent partial close.
                    if _LS_t.COMPLETED.value not in transitioned:
                        result["lane_transition_incomplete"] = True
                        result["lane_transition_skipped"] = skipped
            except Exception as _tx_exc:
                result["lane_transition_error"] = f"approve lane-transition failed: {_tx_exc!r}"
        return result

    from . import tool_interface as _ti_reg_review

    _ti_reg_review.register_impl("ai_review", ai_review)

    @renders_as("status", title="ai_plan_template")
    async def ai_plan_template() -> Any:
        """Return the lane-aware plan template for conductor agents.

        Output is markdown that conductors paste into a new plan file
        under .MEMORY/sessions/<sid>/plans/<beat-name>.md and fill in.
        Sections align with what plan_create_from_spec /
        plan_conductor_status / plan_dispatch_next consume: file
        scopes, tool scopes, requires-dependencies, verification
        commands, per-lane checkboxes.

        Conductor-only: workers (AIDOCS_EXPERT_LANE_ID env set) get a
        refusal instead of the template — workers don't author plans.
        """
        from .plan_template import (
            conductor_only_refusal_reason,
            render_plan_template,
        )

        refusal = conductor_only_refusal_reason()
        if refusal:
            return {"ok": False, "reason": refusal}
        return {
            "ok": True,
            "template": render_plan_template(),
            "save_to": (".MEMORY/sessions/<session-id>/plans/<beat-name>.md"),
            "next_step": (
                "Save the filled-in template to a .md file and call "
                "plan_create_from_spec(session_id=..., spec_path=<path>) "
                "to materialize the lane graph. Lane-aware plans "
                "(starts with '# Plan' + contains '## Why'/'## Lane "
                "graph'/etc.) are written verbatim — no re-parsing."
            ),
        }

    # ── Git operations tool ──

    def _is_workflow_action_satisfied(project_root: Path, action: dict) -> bool:
        """Internal helper — check if an action is verified via its verify
        spec or satisfaction table. De-registered as an MCP tool 2026-04-23
        (leading underscore + single-file internal caller; no agent use).
        """
        result = hub.workflow.verify_action(project_root, action)
        return bool(result.get("verified"))

    @server.tool()
    @renders_as("status", title="workflow action")
    async def workflow_action_satisfy(action_id: str, evidence: str) -> Any:
        """Mark a workflow action as completed with evidence. Call after doing the required work.

        The evidence is logged for audit. Example:
          workflow_action_satisfy("rule-01-01-before_git_commit-advisory", "Updated README.md with v2.2.0b changes")
        """
        project_root = _project_root()
        return hub.workflow.satisfy_action(project_root, action_id, evidence)

    @server.tool(eager=True)
    @renders_as("status", title="git")
    async def ai_git(
        op: str = "status",
        message: str = "",
        count: int = 10,
        branch: str = "",
        path: str = "",
        range: str = "",
    ) -> Any:
        """Basic git operations. op: status, log, diff, add, commit, push, pull, branch, stash.

        op=add REQUIRES explicit path. No `-A` default — see backlog #8
        for why (silently stages everything, including untracked noise).
        Path must not contain shell metacharacters (`;` `&` `|` `` ` `` `$`
        newline, quotes, backslash, `<` `>` `*` `?`) — refuse rather than
        quote, since Windows shell-quoting is fragile.

        op=commit message: quoted, NOT refused. A commit message is free prose —
        quotes, `$`, backticks and newlines are all legitimate in it — so the
        refuse-don't-quote rule that fits `path` would make the tool unusable
        here. It is shlex.quote'd instead (see _git_commit_command), which is
        well-defined because #561 phase 1 made a named bash the interpreter on
        every platform.

        `branch` IS REFUSED, not honoured (#762). It was accepted and never
        read: op=push runs a bare `git push`, i.e. the current branch's tracked
        upstream. Passing it now fails BEFORE git runs rather than silently
        pushing somewhere else. op="branch" is a different verb — it LISTS
        branches (`git branch -a`) and takes no argument.

        op=status (2026-04-27): also reports ahead/behind vs upstream
        when tracking is set. Operators see "you have N unpushed commits"
        without leaving the tool.

        op=log range=... (2026-04-27): accepts a git range like
        "origin/main..HEAD" (unpushed commits) or "HEAD~5..HEAD" (last 5).
        Same shell-metachar validation as path. Empty range falls back
        to the legacy `-N` recent-commits behavior.

        Examples:
          git_ops(op="status")
          git_ops(op="log", count=5)
          git_ops(op="log", range="origin/main..HEAD")
          git_ops(op="diff")
          git_ops(op="add", path="src/foo.py")
          git_ops(op="commit", message="fix: bug")
          git_ops(op="push")
          git_ops(op="pull")
          git_ops(op="branch")
          git_ops(op="stash")

        """
        from .code_runner import ai_run
        from .git_origin_drift import (
            LOCAL_HEAD_BASIS,
            compute_origin_drift,
            github_credential_remedy,
            is_git_auth_failure,
            local_head_note,
            record_drift,
        )

        project_root = _project_root()
        o = op.strip().lower()
        forbidden_metachars = set(";&|`$\n\r\"'\\<>*?")

        # #762 — `branch` NEVER STEERED ANYTHING. It was declared here and read
        # nowhere: cmd_map["push"] is the bare string "git push", so a push
        # always went to the CURRENT branch's tracked upstream. A caller who
        # passed branch="sol/feature" believing it was pushing to a side branch
        # advanced origin/main instead, silently, visible only afterwards
        # (observed 2026-07-10; re-verified in source twice on 2026-08-17).
        #
        # THE TRAP THAT KEPT IT ALIVE: cmd_map DOES contain a "branch" key --
        # but it maps to `git branch -a`, an unrelated LIST verb. A reader
        # skimming for the word finds a match and concludes the argument is
        # wired.
        #
        # REFUSING IS THE FIX AVAILABLE HERE, and it is deliberately not the
        # whole repair. Honouring the argument needs an unambiguous surface
        # (remote / source_ref / destination_ref, plus a pre-execution report of
        # the refspec and fast-forward status) -- a NEW surface, still open on
        # #762. What must not continue meanwhile is a caller believing it steers
        # the destination: law 183074ae, a capability with no consumer is not a
        # capability. An argument that cannot fail loudly is worse than no
        # argument, because nothing ever inspects it.
        if branch.strip():
            return {
                "ok": False,
                "error": (
                    f"git_ops refused: the `branch` argument is NOT WIRED and has "
                    f"never been (backlog #762). It is accepted and then ignored — "
                    f"op=push runs a bare `git push`, which targets the CURRENT "
                    f"branch's tracked upstream, NOT branch={branch.strip()!r}. "
                    f"Callers that passed it have advanced the trunk while "
                    f"believing they pushed to a side branch.\n"
                    f"Do instead:\n"
                    f"  * to push somewhere explicit — run the refspec directly in "
                    f"a shell: `git push origin HEAD:refs/heads/<branch>`. It "
                    f"states the destination in the command, so it cannot be "
                    f"misread the way this argument was;\n"
                    f"  * to LIST branches — git_ops(op='branch') (that op takes no "
                    f"argument; it runs `git branch -a`);\n"
                    f"  * to push the current branch — git_ops(op='push') with no "
                    f"`branch`."
                ),
            }

        def _git_drift_runner(argv: list[str]) -> tuple[int, str]:
            """Adapt ai_run to the compute_origin_drift runner contract.

            Bounded (15s) so a slow/unreachable remote can never hang the
            status read. Returns (returncode, stdout). The argv is composed of
            git-internal tokens only (fetch/rev-list + a ref) — no caller
            input, so no shell-metachar exposure."""
            res = ai_run(project_root, "git " + " ".join(argv), timeout=15)
            rc = res.exit_code if res.exit_code is not None and res.exit_code >= 0 else (
                0 if res.success else 1
            )
            return rc, (res.stdout_preview or "")

        # op=add: require an explicit, metachar-free path. Historically
        # fell through to `git add -A` (backlog #8 — silently staged
        # 800+ .runs/*.log artifacts in one call).
        add_cmd: str | None = None
        if o == "add":
            p = (path or "").strip()
            if not p:
                return {
                    "ok": False,
                    "error": (
                        "git_ops op=add requires an explicit path. "
                        "-A default is disabled (backlog #8). "
                        "Example: git_ops(op='add', path='src/foo.py')."
                    ),
                }
            if any(c in forbidden_metachars for c in p):
                return {
                    "ok": False,
                    "error": (
                        f"git_ops op=add refused: path contains shell "
                        f"metacharacter. Paths with {sorted(forbidden_metachars)!s} "
                        f"are rejected (Windows-safe; quoting is fragile). "
                        f"Stage via shell directly if legitimately needed."
                    ),
                }
            add_cmd = f"git add -- {p}"

        # op=log range=...: validate the same way as path. Empty range
        # falls back to legacy `-N` recent-commits.
        log_cmd = f"git log --oneline -{count}"
        if o == "log" and range:
            r = range.strip()
            if any(c in forbidden_metachars for c in r):
                return {
                    "ok": False,
                    "error": (
                        f"git_ops op=log refused: range contains shell "
                        f"metacharacter. Ranges with {sorted(forbidden_metachars)!s} "
                        f"are rejected (Windows-safe; quoting is fragile)."
                    ),
                }
            log_cmd = f"git log --oneline -{count} {r}"

        # Credential floor on the message (see _git_commit_message_refusal).
        # Placed BEFORE cmd_map because that dict literal is evaluated eagerly
        # and composes the commit command on EVERY op, not just op=commit.
        # Returns the structured refusal used by the `branch` / `add` / `log`
        # guards above rather than an `echo`, which would exit 0 and report a
        # phantom success with no commit made.
        if o == "commit":
            _message_refusal = _git_commit_message_refusal(message)
            if _message_refusal:
                return {"ok": False, "error": _message_refusal}

        cmd_map = {
            "status": "git status --short",
            "log": log_cmd,
            "diff": "git diff --stat",
            "diff_staged": "git diff --cached --stat",
            "add": add_cmd,
            "commit": _git_commit_command(message),
            "push": "git push",
            "pull": "git pull",
            "branch": "git branch -a",
            "stash": "git stash",
            "stash_pop": "git stash pop",
            "stash_list": "git stash list",
            "fetch": "git fetch --all",
            "remote": "git remote -v",
        }

        cmd = cmd_map.get(o)
        if not cmd:
            return {"error": f"Unknown op: {op}. Available: {', '.join(sorted(cmd_map.keys()))}"}

        # Check for "before_" workflow triggers on commit/push
        if o in ("commit", "push"):
            triggers = [f"before_git_{o}"]
            if o == "commit":
                triggers.append("before_git_push")
            pending = []
            for trigger_name in triggers:
                pending.extend(hub.workflow.pending_actions_for_trigger(project_root, trigger_name))
            if pending:
                # Filter out actions that have been satisfied
                unsatisfied = [
                    a for a in pending if not _is_workflow_action_satisfied(project_root, a)
                ]
                if unsatisfied:
                    actions_text = "; ".join(
                        str(a.get("source_segment") or a.get("kind", "action")) for a in unsatisfied
                    )
                    return {
                        "op": o,
                        "blocked": True,
                        "reason": f"Workflow rule requires: {actions_text} — complete these before {o}. Then call workflow_action_satisfy(action_id, evidence) to proceed.",
                        "pending_actions": unsatisfied,
                    }

        result = ai_run(project_root, cmd, timeout=30)
        response: dict[str, Any] = _git_result_fields(result)

        # #671 D3 (law 311bf3e6) — a network op that dies on "could not read
        # Username for 'https://github.com': terminal prompts disabled" tells the
        # agent nothing. Replace the raw git text with a refusal that NAMES the
        # credential AIDOCS expects and where it is configured. This surface
        # deliberately does NOT inject a credential: ai_git runs its command
        # through the general shell-egress runner, and putting an org-wide token
        # into the environment of an arbitrary shell command would hand it to
        # every other command that path runs. The credential-carrying route is
        # project_sync, which runs git directly under a per-invocation helper.
        # Prompts stay disabled either way; nothing here reads a credential.
        if o in ("pull", "push", "fetch") and not result.success:
            _txt = f"{result.stderr_preview or ''}\n{result.stdout_preview or ''}"
            if is_git_auth_failure(_txt):
                response["ok"] = False
                response["blocked"] = True
                response["reason"] = github_credential_remedy(
                    org_id="", action=f"git_ops(op={o!r})"
                ) + (
                    " For a locally-checked-out project, authenticate the checkout "
                    "out-of-band (a configured git credential helper on the host) and "
                    "retry; for a gate-imported project use project_sync, which "
                    "supplies the org credential itself."
                )

        # op=status enrichment (2026-04-27): also report ahead/behind
        # vs upstream so operators see "N unpushed commits" inline.
        # Best-effort — failures here must never break the status read.
        if o == "status" and result.success:
            try:
                # Concrete audit hashes (co-conductor request 2026-07-06): the
                # exact loaded working HEAD + the tracked-upstream tip, so an
                # auditor can verify state by sha rather than trusting the
                # current/behind labels (which disagreed across surfaces —
                # phoenix report). Full 40-char shas; best-effort, never break
                # the status read. NOTE: git_ops runs on ANY selected project
                # (or the exec-root default), NOT just AIDOCS — so the upstream
                # is named neutrally (`upstream_hash`), never "private": the
                # tracked main can be public for a non-AIDOCS project. The
                # `tracking` field names exactly which ref it is (e.g.
                # origin/main), so the reader knows whether it's private.
                _hl = ai_run(project_root, "git rev-parse HEAD", timeout=10)
                if _hl.success and (_hl.stdout_preview or "").strip():
                    response["hash_loaded"] = (_hl.stdout_preview or "").strip()
                # Current branch name.
                br = ai_run(
                    project_root,
                    "git rev-parse --abbrev-ref HEAD",
                    timeout=10,
                )
                cur_branch = (br.stdout_preview or "").strip()
                if br.success and cur_branch and cur_branch != "HEAD":
                    response["branch"] = cur_branch
                    # Upstream tracking name (returns non-zero when no upstream).
                    up = ai_run(
                        project_root,
                        f"git rev-parse --abbrev-ref --symbolic-full-name {cur_branch}@{{u}}",
                        timeout=10,
                    )
                    if up.success and (up.stdout_preview or "").strip():
                        upstream = (up.stdout_preview or "").strip()
                        response["tracking"] = upstream
                        # The tracked-upstream tip (the tip of `tracking`, e.g.
                        # origin/main). Pairs with hash_loaded so the auditor
                        # sees loaded-vs-upstream by exact sha. Neutral name —
                        # for AIDOCS this IS private main (origin is private);
                        # for another project it may be a public main. The
                        # `tracking` field above disambiguates.
                        _mp = ai_run(
                            project_root, f"git rev-parse {upstream}", timeout=10
                        )
                        if _mp.success and (_mp.stdout_preview or "").strip():
                            response["upstream_hash"] = (
                                _mp.stdout_preview or ""
                            ).strip()
                        # #190 FIX: a FETCH-ONLY refresh of the remote-tracking
                        # ref BEFORE counting, so ahead/behind reflect the LIVE
                        # origin — not "behind the last fetch". Fetch-only:
                        # never merge/reset/checkout (drift helper guarantees
                        # it). Fail-safe + bounded: an unreachable remote yields
                        # origin_check=unreachable, never a false behind:0.
                        drift = compute_origin_drift(
                            _git_drift_runner, upstream=upstream
                        )
                        response["origin_check"] = drift["origin_check"]
                        response["git_sync"] = drift["git_sync"]
                        response["behind"] = drift["behind_origin"]
                        response["ahead"] = drift["ahead_origin"]
                        # Persist as the SINGLE git_sync source of truth so
                        # project_status / project_index_status / project_current
                        # report the same verified value (phoenix bug 2).
                        record_drift(project_root, drift)
                        # Surface drift PROMINENTLY when behind the live origin.
                        bo = drift["behind_origin"]
                        if isinstance(bo, int) and bo > 0:
                            response["behind_origin"] = (
                                f"{bo} (run project refresh / project_sync to resync)"
                            )
                        elif drift["origin_check"] == "unreachable":
                            response["behind_origin"] = (
                                "origin unreachable — counts are vs the last "
                                "fetch, not confirmed-current"
                            )
                    else:
                        # No tracked upstream (e.g. source=local). Git-currency
                        # does not apply — say so explicitly, never imply current.
                        response["tracking"] = None
                        response["origin_check"] = "n/a"
                        response["git_sync"] = "n/a"
                        response["ahead"] = None
                        response["behind"] = None
                        record_drift(
                            project_root,
                            {
                                "git_sync": "n/a",
                                "behind_origin": None,
                                "ahead_origin": None,
                                "origin_check": "n/a",
                            },
                        )
            except Exception:
                # Defensive: any failure leaves the base status output
                # intact, just without ahead/behind enrichment.
                pass

        # op=log labeling (#190): the log reports the LOCAL HEAD of this clone,
        # which a behind-origin clone makes stale. Label it so it is never
        # mistaken for the remote's HEAD, and note the behind-origin gap when
        # there is one (best-effort, fetch-only, fail-safe).
        if o == "log" and result.success:
            response["head_basis"] = LOCAL_HEAD_BASIS
            try:
                br = ai_run(project_root, "git rev-parse --abbrev-ref HEAD", timeout=10)
                cur_branch = (br.stdout_preview or "").strip()
                if br.success and cur_branch and cur_branch != "HEAD":
                    up = ai_run(
                        project_root,
                        f"git rev-parse --abbrev-ref --symbolic-full-name {cur_branch}@{{u}}",
                        timeout=10,
                    )
                    upstream = (up.stdout_preview or "").strip() if up.success else ""
                    if upstream:
                        drift = compute_origin_drift(_git_drift_runner, upstream=upstream)
                        note = local_head_note(drift["behind_origin"], upstream=upstream)
                        if note:
                            response["note"] = note
                        response["origin_check"] = drift["origin_check"]
            except Exception:
                # Labeling is best-effort; never break the log read.
                pass

        return response

    # ── Conductor process management ──
    # _conductor_process and _conductor_output are module-level (above
    # create_server) so agent_orchestrator can see them via getattr on
    # the module. Closure-local dicts were invisible to external readers.

    _conductor_output_lock = __import__("threading").Lock()
    _MAX_CONDUCTOR_OUTPUT = 500  # keep last 500 lines

    _start_output_reader = _start_conductor_output_reader

    # Dashboard-only tool registration gate. When dashboard_mode=False,
    # _dash_tool() is a no-op decorator so the tool function is never
    # handed to FastMCP — the agent's list_tools response doesn't show
    # it at all. When dashboard_mode=True, it's server.tool() exactly.
    # Agents must never see conductor_start / conductor_send /
    # conductor_stop / conductor_output (those manage long-lived CLI
    # subprocesses the dashboard controls).
    if dashboard_mode:

        def _dash_tool(*a, **kw):
            return server.tool(*a, **kw)
    else:

        def _dash_tool(*_a, **_kw):
            def _skip(fn):
                return fn

            return _skip

    @_dash_tool()
    @renders_as("status", title="conductor start")
    async def conductor_start(
        session_id: str = "",
        backend: str = "claude",
        model: str = "",
    ) -> Any:
        """Start a persistent long-lived conductor agent for a session.

        Backends: claude (interactive stdin), codex (interactive stdin), opencode (serve mode).
        Sends tasks via conductor_send. Manages lane agents, resolves conflicts.
        Stop with conductor_stop.
        """
        import shutil
        import subprocess

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)

        # Check for existing conductor
        existing = _conductor_process.get("process")
        if existing and existing.poll() is None:
            return {
                "started": False,
                "reason": "Conductor already running.",
                "backend": _conductor_process.get("backend"),
            }

        # Claim session
        from .agent_orchestrator import AgentOrchestrator

        orch = AgentOrchestrator(runtime)
        claim = orch.conductor_claim(project_root, sid, f"conductor-{backend}")
        if not claim.get("claimed"):
            return {"started": False, **claim}

        cli_name = {"claude": "claude", "codex": "codex", "opencode": "opencode"}.get(backend)
        if not cli_name:
            return {
                "started": False,
                "reason": f"Unknown backend: {backend}. Use 'claude', 'codex', or 'opencode'.",
            }
        cli_path = shutil.which(cli_name)
        if not cli_path:
            return {"started": False, "reason": f"{cli_name} CLI not found."}

        # Per-machine ceiling check (2026-04-21). Conductor + workers
        # share the same cap — this refusal fires when the host is
        # saturated, regardless of which project the conductor belongs
        # to. Registration happens AFTER successful Popen below.
        try:
            from .config import get_setting
            from .host_concurrency_store import check_machine_capacity

            machine_cap_raw = get_setting(
                "run.max_live_processes_per_machine",
                project_root=project_root,
                default=4,
            )
            machine_cap = int(machine_cap_raw) if machine_cap_raw is not None else 4
        except Exception:
            machine_cap = 4
        try:
            machine_decision = check_machine_capacity(
                max_processes=machine_cap,
                kind="conductor",
            )
        except Exception:
            machine_decision = {"ok": True}
        if not machine_decision.get("ok"):
            return {
                "started": False,
                "reason": machine_decision.get("error", "machine concurrency ceiling reached"),
                "blocked_by": "machine_concurrency",
                "live_count": machine_decision.get("live_count"),
                "max_processes": machine_decision.get("max_processes"),
            }

        # Build rich context briefing from session state. Tool references come
        # from the canonical conductor_doctrine (tool-truth-enforced) — never
        # inline phantom names.
        from . import conductor_doctrine as _conductor_doctrine

        context_parts = [
            f"You are the AIDOCS conductor for project at '{project_root}', session '{sid}'.",
            "",
            "== YOUR ROLE ==",
            "You are a persistent, long-lived session conductor. You stay alive between tasks.",
            # Doctrine XXII: the role text has ONE literal home. This used to
            # inline a second, divergent copy (doer-first) so the persistent
            # conductor and the seat payload read different doctrine.
            _conductor_doctrine.conductor_responsibilities(),
            "",
            "== HOW YOU WORK ==",
            "1. User sends a task (e.g. 'Fix the login page password field')",
            "2. You investigate the codebase yourself using AIDOCS tools (ai_investigate, ai_find, ai_bundle)",
            ("3. DEFAULT: brief the best-scoped agent and COMMAND it — dispatch through the SINGLE "
            "surface ai_lane(action=…): spawn to dispatch, guide to nudge a running worker, pause, "
            "resume a stalled worker (by lane), kill a runaway (by lane), review to decide a lane's "
            "completion. One front still gets an agent; parallel fronts get several."),
            ("4. EXCEPTION: edit directly (ai_replace / ai_batch_edit, ai_test) only when the operator "
            "asks, when delegation is unavailable or riskier, when the change is tiny and inseparable "
            "from your current investigation, or in an emergency — then return to command."),
            ("5. Monitor lanes you dispatched: ai_lane(action='status') + ai_lane(action='events') "
            "per worker (by lane); ai_seat(action='overview') for all lanes, pending questions, activity"),
            "6. When done: report what changed, what was tested, what needs attention",
            "7. Wait for the next task — don't exit",
            "",
            *_conductor_doctrine.conductor_onboarding(),
            "(session audit trail lives in execution_events sqlite, written automatically)",
            "",
            "== RULES ==",
            "- Always use AIDOCS indexed tools before reading files",
            "- Log significant decisions to session journal",
            "- When dispatching lanes: set clear scope (allowed files), verify results",
            "- When stuck: ask the operator via ai_qa(action='ask')",
            "- Never exit unless the user says to stop",
        ]

        # Load session context if available
        try:
            session_path = project_root / ".MEMORY" / "sessions" / sid
            # Session overview
            session_md = session_path / "SESSION.md"
            if session_md.is_file():
                content = session_md.read_text(encoding="utf-8")[:2000]
                context_parts.extend(["", "== SESSION STATE ==", content])
            # Recent journal
            journal = session_path / "journal.md"
            if journal.is_file():
                entries = journal.read_text(encoding="utf-8")[-1500:]
                context_parts.extend(["", "== RECENT JOURNAL ==", entries])
            # Plan if exists
            plan_dir = session_path / "plans"
            if plan_dir.is_dir():
                for plan_file in sorted(plan_dir.glob("*.md"))[:1]:
                    plan_content = plan_file.read_text(encoding="utf-8")[:2000]
                    context_parts.extend(["", f"== PLAN ({plan_file.name}) ==", plan_content])
            # Files touched in this session
            try:
                from .edit_history import EditHistoryStore

                touched = EditHistoryStore().files_touched_summary(project_root, session_id=sid)
                if touched:
                    files_list = "\n".join(
                        f"  {f['file']} ({f['edits']} edits, agents: {','.join(f['agents']) or 'unknown'})"
                        for f in touched[:20]
                    )
                    context_parts.extend(["", "== FILES MODIFIED THIS SESSION ==", files_list])
            except Exception:
                pass
        except Exception:
            pass

        # Inject agent workflow rules for conductor to enforce
        try:
            agent_rules = hub.workflow.get_agent_workflow_rules(project_root)
            if agent_rules:
                rules_text = "\n".join(f"  - {r}" for r in agent_rules)
                context_parts.extend(
                    [
                        "",
                        "== AGENT WORKFLOW RULES (enforce on lane agents) ==",
                        rules_text,
                    ],
                )
        except Exception:
            pass

        initial_prompt = "\n".join(context_parts)

        try:
            model_flag = model.strip() if model else ""
            if backend == "claude":
                identity_prompt = _claude_identity_prompt(project_root, sid)
                cmd_args = _claude_build_cli_args(cli_path, identity_prompt, model_flag)
                # CREATE_NO_WINDOW on Windows matches the Tauri dashboard so
                # the CLI does not flash a console when spawned from a GUI.
                popen_kwargs: dict[str, Any] = {
                    "cwd": str(project_root),
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "bufsize": 1,
                }
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                # #345: routed through audited_popen — conductor CLI spawns
                # were a ledger gap (window-safe already, but invisible).
                # Passthrough lambda IS the registered AST callsite; kwargs
                # pass through UNCHANGED.
                from .shell_egress_service import audited_popen

                child = audited_popen(
                    cmd_args,
                    fingerprint=("mcp_server.py", "conductor_start", "subprocess.Popen"),
                    reason="conductor-claude-spawn",
                    session_id=sid,
                    popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
                    **popen_kwargs,
                )
            elif backend == "opencode":
                import random

                oc_port = random.randint(10000, 60000)
                oc_popen_kwargs: dict[str, Any] = {
                    "cwd": str(project_root),
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                }
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    oc_popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                from .shell_egress_service import audited_popen

                child = audited_popen(
                    [cli_path, "serve", "--port", str(oc_port)],
                    fingerprint=("mcp_server.py", "conductor_start", "subprocess.Popen"),
                    reason="conductor-opencode-serve",
                    session_id=sid,
                    popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
                    **oc_popen_kwargs,
                )
                _conductor_process["opencode_port"] = oc_port
            else:  # codex
                cmd_args = [cli_path]
                if model_flag:
                    cmd_args.extend(["-m", model_flag])
                codex_popen_kwargs: dict[str, Any] = {
                    "cwd": str(project_root),
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                }
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    codex_popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                from .shell_egress_service import audited_popen

                child = audited_popen(
                    cmd_args,
                    fingerprint=("mcp_server.py", "conductor_start", "subprocess.Popen"),
                    reason="conductor-codex-spawn",
                    session_id=sid,
                    popen=lambda *a, **kw: subprocess.Popen(*a, **kw),
                    **codex_popen_kwargs,
                )
            _conductor_process["process"] = child
            _conductor_process["backend"] = backend
            _conductor_process["session_id"] = sid
            _conductor_process["project_root"] = str(project_root)

            # Register the machine-wide slot with the subprocess pid so
            # a crashed conductor (process gone before conductor_stop
            # ran) is swept automatically by the next capacity check.
            try:
                from .host_concurrency_store import HostConcurrencyStore

                _conductor_process["machine_slot_key"] = f"conductor-{child.pid}"
                HostConcurrencyStore().register(
                    worker_key=_conductor_process["machine_slot_key"],
                    kind="conductor",
                    pid=child.pid,
                    project_root=project_root,
                    session_id=sid,
                )
            except Exception:
                pass

            with _conductor_output_lock:
                _conductor_output.clear()
            _start_output_reader(
                child,
                _conductor_output,
                _conductor_output_lock,
                _MAX_CONDUCTOR_OUTPUT,
                backend=backend,
                state=_conductor_process,
            )

            try:
                if backend == "claude":
                    # Claude's stream-json input format rejects raw text; an
                    # unwrapped prompt would close the process immediately.
                    child.stdin.write(_claude_stream_json_user_envelope(initial_prompt))
                else:
                    child.stdin.write(initial_prompt + "\n")
                child.stdin.flush()
            except Exception:
                pass

            return {
                "started": True,
                "backend": backend,
                "session_id": sid,
                "pid": child.pid,
                "mode": "interactive",
            }
        except Exception as exc:
            return {"started": False, "reason": str(exc)}

    @_dash_tool()
    @renders_as("status", title="conductor send")
    async def conductor_send(message: str) -> Any:
        """Send a message/command to the running conductor agent."""
        proc = _conductor_process.get("process")
        if not proc or proc.poll() is not None:
            _conductor_process.clear()
            return {"sent": False, "reason": "No conductor running."}

        backend = _conductor_process.get("backend", "claude")

        # OpenCode: send via `opencode run --attach`
        if backend == "opencode":
            import shutil

            oc_port = _conductor_process.get("opencode_port")
            oc_cli = shutil.which("opencode")
            if not oc_port or not oc_cli:
                return {"sent": False, "reason": "OpenCode port/CLI not available"}
            try:
                result = __import__("subprocess").run(
                    [oc_cli, "run", "--attach", f"http://localhost:{oc_port}", message],
                    cwd=_conductor_process.get("project_root", "."),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                return {
                    "sent": True,
                    "message": message[:200],
                    "output": result.stdout[:500],
                }
            except Exception as exc:
                return {"sent": False, "reason": str(exc)}

        try:
            if backend == "claude":
                # Raw text would be rejected by --input-format stream-json and
                # kill the CLI mid-turn.
                proc.stdin.write(_claude_stream_json_user_envelope(message))
            else:
                proc.stdin.write(message + "\n")
            proc.stdin.flush()
            return {"sent": True, "message": message[:200]}
        except (BrokenPipeError, OSError) as exc:
            return {"sent": False, "reason": str(exc)}

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='status').
    @renders_as("status", title="conductor")
    async def conductor_status() -> Any:
        """Check if the conductor agent is running."""
        proc = _conductor_process.get("process")
        inline = _conductor_process.get("inline")
        if not proc:
            if inline:
                return {
                    "running": True,
                    "mode": "inline",
                    "session_id": inline.get("session_id"),
                    "project_root": inline.get("project_root"),
                    "entered_at": inline.get("entered_at"),
                }
            return {"running": False}
        if proc.poll() is not None:
            proc_sid = _conductor_process.get("session_id")
            exit_code = proc.returncode
            # Only clear the subprocess keys; keep 'inline' if set.
            for k in ("process", "backend", "session_id", "claude_session_id", "opencode_port"):
                _conductor_process.pop(k, None)
            if inline:
                return {
                    "running": True,
                    "mode": "inline",
                    "session_id": inline.get("session_id"),
                    "project_root": inline.get("project_root"),
                    "last_subprocess_exit_code": exit_code,
                    "last_subprocess_session_id": proc_sid,
                }
            return {"running": False, "exit_code": exit_code}
        return {
            "running": True,
            "mode": "subprocess",
            "backend": _conductor_process.get("backend"),
            "session_id": _conductor_process.get("session_id"),
            "pid": proc.pid,
            "claude_session_id": _conductor_process.get("claude_session_id"),
        }

    @_dash_tool()
    @renders_as("status", title="conductor stop")
    async def conductor_stop() -> Any:
        """Stop the running conductor agent and release session claim."""
        import subprocess  # local: `except subprocess.TimeoutExpired` below

        proc = _conductor_process.get("process")
        if not proc:
            return {"stopped": False, "reason": "No conductor running."}

        sid = _conductor_process.get("session_id", "")
        backend = _conductor_process.get("backend", "")
        project_root_str = _conductor_process.get("project_root", "")
        machine_slot_key = _conductor_process.get("machine_slot_key", "")

        # Graceful stop
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Release the per-machine slot even if the claim release below
        # errors out. Safe on double-call thanks to unregister's no-op.
        if machine_slot_key:
            try:
                from .host_concurrency_store import HostConcurrencyStore

                HostConcurrencyStore().unregister(worker_key=machine_slot_key)
            except Exception:
                pass

        _conductor_process.clear()

        # Release session claim
        if sid and project_root_str:
            try:
                from .agent_orchestrator import AgentOrchestrator

                orch = AgentOrchestrator(runtime)
                orch.conductor_release(Path(project_root_str), sid, f"conductor-{backend}")
            except Exception:
                pass

        return {"stopped": True, "session_id": sid}

    # ── Conductor communication tools ──

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='ask').
    @renders_as("status", title="conductor ask")
    async def conductor_ask(
        question: str,
        lane_id: str = "default",
        wait: bool = False,
        timeout: int = 120,
        category: str = "question",
        requested_path: str = "",
        session_id: str = "",
    ) -> Any:
        """Ask the conductor/operator a question. If wait=True, blocks until answered or timeout.

        For scope requests: set category='scope_request' and requested_path='path/to/file'.
        Auto-resolves if no lane conflict exists (no conductor intervention needed).
        """
        from .conductor_comms import agent_ask, auto_resolve_scope_request

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)

        # Auto-resolve scope requests when possible
        if category == "scope_request" and requested_path:
            result = agent_ask(
                project_root,
                lane_id,
                question,
                category=category,
                session_id=sid,
                wait=False,
            )
            msg_id = result.get("id", "")
            if msg_id:
                auto = auto_resolve_scope_request(
                    project_root,
                    msg_id,
                    lane_id,
                    requested_path,
                    session_id=sid,
                )
                if auto.get("auto_resolved"):
                    return {
                        "id": msg_id,
                        "status": "answered",
                        "response": f"Auto-approved: '{requested_path}' added to your scope.",
                        "auto_resolved": True,
                    }
            # Conflict or error — fall through to normal flow

        return agent_ask(
            project_root,
            lane_id,
            question,
            category=category,
            session_id=sid,
            wait=wait,
            timeout=float(timeout),
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='check').
    @renders_as("status", title="conductor response")
    async def conductor_check_response(message_id: str) -> Any:
        """Check if a previously submitted question has been answered."""
        from .conductor_comms import check_response

        return check_response(_project_root(), message_id)

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='answer').
    @renders_as("status", title="conductor answer")
    async def conductor_answer(message_id: str, response: str) -> Any:
        """Answer an agent's pending question (called by conductor or dashboard)."""
        from .conductor_comms import answer_question

        return answer_question(_project_root(), message_id, response)

    @server.tool()
    @renders_as("status", title="ai_seat")
    async def ai_seat(
        mode: str,
        session_id: str = "",
        verbose: bool = False,
        confirm_token: str = "",
    ) -> Any:
        """Unified conductor seat operations — one tool, four modes (Empire directive 2026-05-12).

        mode='enter'    — current agent becomes the session conductor (binds + persists).
                         Optional: session_id, verbose. Returns terse confirmation by
                         default; verbose=True adds SESSION.md body + journal tail.
        mode='co-enter' — current agent takes the CO-CONDUCTOR seat (#167 Phase 4):
                         auto-dumps the co-conductor ROLE scroll and maps the caller's
                         host session to the 'co_conductor' comms role. Does NOT rebind
                         managed mode — the conductor owns the session binding.
                         Souls never auto-load (§XII); the seat's soul opens only
                         through ai_soul on the Empire's word.
        mode='exit'     — clear the inline-conductor marker. No-op if no inline binding.
        mode='status'   — check if the conductor agent is running (process/inline info).
        mode='overview' — full conductor situational awareness: all lanes, states,
                         pending questions, recent activity. Optional: session_id.
        """
        m = (mode or "").strip().lower()
        if m == "enter":
            return await conductor_mode_enter(
                session_id=session_id, verbose=verbose, confirm_token=confirm_token
            )
        if m in ("co-enter", "co_enter", "coenter"):
            return await coconductor_mode_enter(
                session_id=session_id, confirm_token=confirm_token
            )
        if m == "exit":
            return await conductor_mode_exit()
        if m == "status":
            return await conductor_status()
        if m == "overview":
            return await conductor_overview(session_id=session_id)
        return {
            "error": (
                f"unknown mode: {mode!r} "
                "(valid: enter|co-enter|exit|status|overview)"
            )
        }

    @server.tool()
    @renders_as("status", title="ai_qa")
    async def ai_qa(
        mode: str,
        question: str = "",
        message_id: str = "",
        response: str = "",
        lane_id: str = "",
        wait: bool = False,
        timeout: int = 120,
        category: str = "question",
        requested_path: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> Any:
        """Unified Q&A channel — one tool, five modes (Empire directive 2026-05-12).

        mode='ask'     — agent asks the conductor/operator a question.
                        Required: question. Optional: lane_id, wait, timeout,
                        category, requested_path, session_id. If wait=True, blocks
                        until answered or timeout. Set category='scope_request'
                        + requested_path to auto-approve no-conflict scope grants.
        mode='answer'  — conductor/operator answers a pending question.
                        Required: message_id, response.
        mode='check'   — check if a previously submitted question has been answered.
                        Required: message_id.
        mode='pending' — list all pending agent questions awaiting verdict.
                        Optional: session_id.
        mode='history' — list message history (per-lane or all). Optional: lane_id, limit.
        """
        m = (mode or "").strip().lower()
        if m == "ask":
            return await conductor_ask(
                question=question,
                lane_id=lane_id or "default",
                wait=wait,
                timeout=timeout,
                category=category,
                requested_path=requested_path,
                session_id=session_id,
            )
        if m == "answer":
            return await conductor_answer(message_id=message_id, response=response)
        if m == "check":
            return await conductor_check_response(message_id=message_id)
        if m == "pending":
            return await conductor_pending_questions(session_id=session_id)
        if m == "history":
            return await conductor_message_history(lane_id=lane_id, limit=limit)
        return {"error": f"unknown mode: {mode!r} (valid: ask|answer|check|pending|history)"}

    @server.tool()
    @renders_as("status", title="ai_failures")
    async def ai_failures(
        mode: str = "list",
        signature: str = "",
        proof_command: str = "",
        proof_log: str = "",
        baseline_sha: str = "",
        followup_ref: str = "",
        operator_alert: str = "",
        operator: str = "",
        reason: str = "",
        session_id: str = "",
        include_all: bool = False,
    ) -> Any:
        """Failure-stewardship disposition surface — the agent-callable
        CONSUMER half of the failure ledger (the Stop hook is the producer).

        mode='list'              — failures THIS session owns (seal blockers), plus the
                                   still-open rows under ANOTHER duty with the reachable
                                   next step for each (blocked_elsewhere, #673). The full
                                   ledger ships only with include_all=True (#852).
        mode='fixed'             — fixed this session. Required: signature. Proof: proof_command + proof_log.
        mode='preserve_baseline' — proven pre-existing. Required: signature, baseline_sha + proof_log.
        mode='quarantine'        — skip with a written follow-up. Required: signature, followup_ref.
        mode='escalate'          — operator decision needed. Required: signature, operator_alert.
        mode='waiver'            — operator authority. Required: signature, operator, reason.
        mode='autoclear'         — mark every blocker this session owns FIXED on an observed green run.

        signature accepts the full sha or an unambiguous short prefix.
        Session-scoped: cannot dispose a failure under another session's duty.
        """
        from . import failure_stewardship as fs

        project_root = _project_root()
        # The LEDGER keys duty on the host session uuid (the Stop hook writes
        # it); _resolve_session_id returns the managed-mode AIDOCS session NAME.
        # Defaulting to the latter made an agent unable to see or dispose its own
        # failures — see _resolve_failure_duty_id.
        sid = session_id or _resolve_failure_duty_id(hub, project_root)
        m = (mode or "list").strip().lower()
        if m in ("list", "blockers", ""):
            return fs.list_session_failures(project_root, sid, include_all=include_all)
        try:
            return fs.apply_disposition(
                project_root,
                sid,
                action=m,
                signature=signature,
                proof_command=proof_command,
                proof_log=(proof_log or "").encode("utf-8", "replace"),
                baseline_sha=baseline_sha,
                followup_ref=followup_ref,
                operator_alert=operator_alert,
                operator=operator,
                reason=reason,
            )
        except fs.StewardshipError as exc:
            return {"ok": False, "error": str(exc), "action": m}

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "AI Whoami"},
    )
    @renders_as("status", title="ai_whoami")
    async def ai_whoami() -> Any:
        """Who does each identity channel say is calling, RIGHT NOW, on THIS call?

        A DEBUGGING INSTRUMENT, added 2026-08-21 after the operator measured a
        /clear lockout and found four different host ids in one transcript:
        the window's env said bc8bd9e3, the shim relayed 3d93740d, a warning
        blamed 74a03862, and the gate refused naming bc8bd9e3. No single tool
        showed them side by side, so every explanation was an agent's inference.
        This shows them side by side so the operator can read the divergence
        directly.

        EVERY VALUE NAMES ITS SOURCE. Nothing here is predicted or interpreted:
          request_header   the X-Aidocs-Host-Session header on THIS request,
                           i.e. what the shim ACTUALLY SENT — read from the
                           live HTTP request, not from any store. Empty means
                           this call did not arrive via the shim.
          resolved_caller  what current_calling_host_session_id() resolves to
                           for this request (header, else process fallback).
          process_global   the shared _calling_conductor_host_session_id stamp,
                           reported separately so a fallthrough is visible.
          slot             query_gate.last_host_session_id for the bound
                           session — the single-slot field (#859: FROZEN for
                           per_conductor sessions; shown so its staleness is
                           obvious, never used to decide anything here).
          chain            the session's owned host-id chain (#464).
          env              the DAEMON's CLAUDE_CODE_SESSION_ID, which is NOT the
                           window's — included precisely so that difference is
                           visible (the daemon is a shared process).
        Read-only, no org/project selection needed, no side effects.
        """
        import os as _os

        from fastmcp.server.dependencies import get_http_headers

        from .mcp_server_runtime_helpers import (
            _calling_conductor_host_session_id as _pg_sid,
        )
        from .mcp_server_runtime_helpers import (
            _request_host_session_id as _req_sid_var,
        )
        from .mcp_server_runtime_helpers import (
            current_calling_host_session_id,
        )
        from .stdio_shim import HEADER_HOST_KIND, HEADER_HOST_SESSION

        out: dict[str, Any] = {"ok": True}
        try:
            _hdrs = {k.lower(): v for k, v in (get_http_headers() or {}).items()}
        except Exception:  # noqa: BLE001 -- a diagnostic must never raise
            _hdrs = {}
        out["request_header"] = {
            "host_session_id": (_hdrs.get(HEADER_HOST_SESSION.lower()) or "").strip(),
            "host_kind": (_hdrs.get(HEADER_HOST_KIND.lower()) or "").strip(),
            "source": f"HTTP header {HEADER_HOST_SESSION} on this call (what the shim sent)",
        }
        out["request_contextvar"] = {
            "host_session_id": (_req_sid_var.get() or ""),
            # THIS LABEL SAID "set from the header above" AND THAT WAS FALSE.
            # mcp_server.py:2608 stamps `_leased` -- the WINDOW LEASE -- not the
            # header; the header only decides WHETHER to stamp at all. The
            # divergence a reader sees between these two rows is #880 working,
            # not a propagation bug, and the old label made correct behaviour
            # look broken. In the ONE tool built to end identity guesswork, a
            # provenance string that names a derivation which does not exist is
            # worse than no string: it manufactures the wrong investigation.
            "source": (
                "_request_host_session_id ContextVar -- set from the WINDOW "
                "LEASE (#880), NOT from the header above. A header is "
                "CLIENT-ASSERTED; the lease is host-attested via the "
                "conversation SessionStart bound to this window. They differ "
                "whenever the window is new or has rotated, and that is "
                "correct. See lease_reason below when the lease could not "
                "answer."
            ),
        }
        out["resolved_caller"] = {
            "host_session_id": (current_calling_host_session_id() or ""),
            # ALSO WRONG for the same reason: this reads the ContextVar above
            # (the lease), never the header. And "else process fallback" skipped
            # the middle rung entirely -- #672's honest empty, which exists so a
            # scoped request that knows no session refuses instead of borrowing
            # the shared stamp of a DIFFERENT window on this multi-tenant daemon.
            "source": (
                "current_calling_host_session_id(): the request ContextVar "
                "above (the lease), else \"\" when the request is "
                "identity-SCOPED but knows no session (#672 -- an honest empty, "
                "never a borrow), else the shared process stamp"
            ),
        }
        out["process_global"] = {
            "host_session_id": (_pg_sid or ""),
            "source": "_calling_conductor_host_session_id (shared, last-writer-wins)",
        }
        out["env"] = {
            "CLAUDE_CODE_SESSION_ID": (_os.environ.get("CLAUDE_CODE_SESSION_ID") or ""),
            "source": "the DAEMON process's own environment -- not the window's",
        }
        project_root = _project_root()
        sid = ""
        try:
            managed = hub.managed_mode.get_mode(project_root)
            if managed.get("active"):
                sid = str(managed.get("session_id") or "").strip()
        except Exception:  # noqa: BLE001
            sid = ""
        out["bound_session"] = sid
        if sid:
            try:
                out["slot"] = {
                    "last_host_session_id": hub.query_gate.get_last_host_session_id(
                        project_root, sid
                    ),
                    "source": "session_query_gate.last_host_session_id (single slot; "
                    "#859: frozen for per_conductor sessions -- DIAGNOSTIC ONLY)",
                }
            except Exception as exc:  # noqa: BLE001
                out["slot"] = {"error": str(exc)}
            try:
                out["chain"] = {
                    "host_session_ids": list(
                        hub.query_gate.get_host_session_id_chain(project_root, sid) or []
                    ),
                    "source": "session_query_gate.host_session_id_chain (#464, append-only)",
                }
            except Exception as exc:  # noqa: BLE001
                out["chain"] = {"error": str(exc)}
        # The one-line verdict, stated as a FACT about two observed values.
        hdr = out["request_header"]["host_session_id"]
        res = out["resolved_caller"]["host_session_id"]
        # #880 PATCH 4. `bool(hdr) and hdr == res` was TWO defects in one line.
        #
        # The first is gone as of phase 2: `res` used to BE the header, so this
        # compared the header to itself and printed `true` beside a divergence
        # it could not see -- measured `true` while the header was THREE
        # conversation rotations stale. `res` is now the LEASE, so the equality
        # is finally a comparison of two channels.
        #
        # The second is this line's own: it answers a BOOLEAN to a three-state
        # question. With no lease to compare against, `hdr == res` is False, and
        # False here reads as "the channels DISAGREE" -- a conflict reported
        # where the truth is that nothing could be checked. #588 D5 applied to
        # identity: report what was observed, or say it could not be checked.
        # Never manufacture the alarming answer out of an absent one.
        from .window_lease import channels_agree as _channels_agree
        from .window_lease import current_request_lease_reason

        out["channels_agree"] = _channels_agree(hdr, res)
        # WHY, when the answer is not True. An unverifiable verdict with no
        # cause is the same dead end as `managed_mode_not_active`: "no window
        # key on this request" and "no conversation bound to this window" are
        # different failures with different remedies.
        _lease_reason = current_request_lease_reason()
        if _lease_reason:
            out["lease_reason"] = _lease_reason
        out["note"] = (
            "To compare against the WINDOW's live id, run `echo $CLAUDE_CODE_SESSION_ID` "
            "in that window: Bash spawns fresh and reads the env NOW, whereas the shim "
            "read it ONCE at its own spawn. If they differ, the shim is stale and "
            "/mcp reconnect respawns it."
        )
        return out

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "AI Gate Explain"},
    )
    @renders_as("status", title="ai_gate_explain")
    async def ai_gate_explain(
        mode: str = "explain",
        blocked_by: str = "",
        matched_rule: str = "",
        risk_class: str = "",
        user_intent_detected: bool = False,
        needs_confirmation: bool = True,
        command: str = "",
        prompt: str = "",
        tool_name: str = "ai_run",
    ) -> Any:
        """For WHAT KIND of block, WHAT HAPPENS. A read-only diagnostic added
        2026-08-25 after a plain CSS append (``printf >> x.css``) froze a
        session under ``run_destructive`` and no surface could say why, or
        what any other refusal would have cost.

        EVERY CONSEQUENCE NAMES THE CODE THAT DECIDES IT, and every number is
        computed by the same functions the gate runs (verdict_class,
        judge_taxonomy, heuristic_judge, security_violation_service,
        violation_severity, operation_classes). Nothing is minted, stamped,
        struck or written. It does NOT read live session state (strike counts,
        an active freeze, the stamped prompt tokens), so it states what the
        code maps an input to — not what a particular live call will get.

        mode='explain'  blocked_by (+ matched_rule / risk_class /
                        user_intent_detected / needs_confirmation) -> cost.
        mode='command'  command (+ prompt) -> each hop on both intent branches.
        mode='matrix'   the whole table.
        """
        from . import refusal_explainer as _rx

        m = str(mode or "explain").strip().lower()
        try:
            if m == "matrix":
                return {"ok": True, "mode": m, **_rx.refusal_matrix(_project_root())}
            if m == "command":
                if not str(command or "").strip():
                    return {"ok": False, "error": "mode='command' requires command=<shell text>"}
                return {
                    "ok": True,
                    "mode": m,
                    **_rx.explain_command(
                        command,
                        tool_name=tool_name or "ai_run",
                        prompt=prompt or "",
                        project_root=_project_root(),
                    ),
                }
            if m != "explain":
                return {"ok": False, "error": f"unknown mode {mode!r}; use explain | command | matrix"}
            if not any((blocked_by, matched_rule, risk_class)):
                return {
                    "ok": False,
                    "error": (
                        "mode='explain' needs at least one of blocked_by / matched_rule / "
                        "risk_class (copy them from the refusal); or use mode='command'"
                    ),
                }
            return {
                "ok": True,
                "mode": m,
                **_rx.explain_refusal(
                    blocked_by=blocked_by,
                    matched_rule=matched_rule,
                    risk_class=risk_class,
                    user_intent_detected=bool(user_intent_detected),
                    needs_confirmation=bool(needs_confirmation),
                ),
            }
        except Exception as exc:  # noqa: BLE001 -- a diagnostic must never raise
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "AI Version"},
    )
    @renders_as("status", title="ai_version")
    async def ai_version() -> Any:
        """Which AIDOCS build is this, and what is newest deployed / released?

        ONE QUESTION, ONE ANSWER — no modes, no refusals (operator ruling
        2026-08-21). The previous surface hid four truth-sources behind a
        `mode` parameter and answered a plain "what version is this runtime"
        with `{"refused": true, "requested_mode": "local"}` — a denial from the
        one tool whose job is to say which code is running.

        Three axes, always all three, because they are DIFFERENT QUESTIONS and
        conflating them is how a release answer gets read as a local one:

          running   what THIS PROCESS loaded — frozen at boot, in memory, so it
                    survives "is the fix actually live?" (disk is not memory,
                    #738)
          deployed  what the last deploy SEALED
          released  the last BLESSED build, from the signed manifest

        Each carries `version` (three-segment semver) and `build` (the ticker,
        a SEPARATE integer — never a fourth segment). The build number travels
        INSIDE the artefact, so a client install that has never run a deploy
        script can still name which build it runs.

        An axis that cannot be established says `known: false` WITH A REASON.
        That is an answer, not a refusal. Benign, read-only; no org/project
        selection needed.
        """
        from . import build_info

        return build_info()

    @server.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False, "title": "AI Process Audit"},
    )
    @renders_as("status", title="ai_process_audit")
    async def ai_process_audit(
        mode: str = "tail",
        n: int = 20,
        key: str = "",
        session_id: str = "",
    ) -> Any:
        """Read-only runtime process-audit ledger (backlog #335 Phase 1) —
        what subprocesses did this server spawn, why, and when. Rows are
        recorded by shell_egress_service.audited_popen (spawn + reap);
        the fingerprint ENFORCEMENT gate (LEGACY_SUBPROCESS_FINGERPRINTS)
        is a separate, untouched authority — this tool only reads the
        observability ledger. Modes: tail (newest-first n) / list
        (oldest-first n) / by-callsite (key = '::'-joined fingerprint) /
        by-session (session_id) / stats (totals + groupings) / census
        (the COMPLETE spawn map — every static callsite -> fingerprint
        -> reason -> window posture -> audited/registered, joined with
        runtime ledger spawn counts; #335 one organism)."""
        from .process_audit_store import process_audit_query

        return process_audit_query(mode=mode, n=n, key=key, session_id=session_id)

    @server.tool()
    @renders_as("status", title="ai_msg")
    async def ai_msg(
        mode: str,
        to_roles: str = "",
        body: str = "",
        in_reply_to: str = "",
        message_id: str = "",
        unread_only: bool = True,
        mark_read: bool = False,
        limit: int = 50,
        session_id: str = "",
        target_actor_id: str = "",
        lane_id: str = "",
        message_kind: str = "",
        correlation_id: str = "",
        decision: str = "",
        timeout_seconds: float = 0.0,
        after_cursor: int = 0,
        wake: bool = False,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
        confirm_token: str = "",
        reason: str = "",
    ) -> Any:
        """One inter-agent channel for role messages and actor-routed XAACP."""
        from .conductor_comms import ai_msg_dispatch

        kwargs = {
            "mode": mode,
            "to_roles": to_roles,
            "body": body,
            "in_reply_to": in_reply_to,
            "message_id": message_id,
            "unread_only": unread_only,
            "mark_read": mark_read,
            "limit": limit,
            "session_id": session_id,
            "target_actor_id": target_actor_id,
            "lane_id": lane_id,
            "message_kind": message_kind,
            "correlation_id": correlation_id,
            "decision": decision,
            "timeout_seconds": timeout_seconds,
            "after_cursor": after_cursor,
            "wake": wake,
            "metadata": metadata,
            "ttl_seconds": ttl_seconds,
            "confirm_token": confirm_token,
            "reason": reason,
        }
        if str(mode or "").strip().lower() in {"xaacp_wait", "wait_next"}:
            return await asyncio.to_thread(ai_msg_dispatch, _project_root(), **kwargs)
        return ai_msg_dispatch(_project_root(), **kwargs)

    from . import tool_interface as _ti_reg_msg

    _ti_reg_msg.register_impl("ai_msg", ai_msg)


    # @server.tool removed (120% clause B): folded into ai_lane(action='guide').
    async def ai_guidance(lane_id: str, message: str, session_id: str = "") -> Any:
        """Send guidance to a lane agent. Agent sees it on next tool call via hook injection."""
        from .conductor_comms import send_guidance

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return send_guidance(project_root, lane_id, message, session_id=sid)

    from . import tool_interface as _ti_reg_guidance

    _ti_reg_guidance.register_impl("ai_guidance", ai_guidance)

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='pending').
    @renders_as("list", title="pending questions")
    async def conductor_pending_questions(session_id: str = "") -> Any:
        """List all pending agent questions awaiting conductor/operator response."""
        from .conductor_comms import get_pending_questions

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return {"questions": get_pending_questions(project_root, sid)}

    @renders_as("status", title="ai_lane_control")
    async def ai_lane_control(
        lane_id: str,
        state: str = "active",
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Control a lane: set state to 'active', 'paused', or 'canceled'."""
        from .conductor_comms import set_lane_state

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return set_lane_state(project_root, lane_id, state, reason=reason, session_id=sid)

    @renders_as("status", title="lane state")
    async def ai_lane_state(
        state: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Update lane worker state (running → done / failed / crashed).

        Env-guarded: reads AIDOCS_EXPERT_ID from the process environment.
        Workers call this to signal progress; refuses when the env var is
        missing or the worker_id does not match a session_lane_agents row.
        """
        import os as _os

        worker_id = _os.environ.get("AIDOCS_EXPERT_ID", "").strip()
        if not worker_id:
            return {
                "ok": False,
                "error": "missing_env",
                "message": (
                    "AIDOCS_EXPERT_ID env var missing. This tool is "
                    "only callable by lane sub-agents spawned through "
                    "the agent worker service."
                ),
            }
        return runtime.lane_update_state(_project_root(), worker_id, state, metadata=metadata)

    # Internal helper. Tool surface removed 2026-05-12 — ai_seat(mode='overview').
    @renders_as("status", title="conductor overview")
    async def conductor_overview(session_id: str = "") -> Any:
        """Full conductor situational awareness: all lanes, states, pending questions, recent activity. One call, full picture."""
        from .conductor_comms import get_all_lanes_status

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return get_all_lanes_status(project_root, sid)

    @renders_as("list", title="lane workers")
    async def ai_lane_agents(
        include_graveyard: bool = False,
        session_id: str = "",
        state: str = "",
        host_session_id: str = "",
        mine: bool = False,
    ) -> Any:
        """List connected cross-agent coordination state for the selected
        project (all chats): each agent's bound session/lane, host_session_id,
        liveness, owned files (its in-flight write scope), and backend, read
        from existing session_lane_agents state. Filters: session_id / state /
        host_session_id; mine=true keeps only YOUR host id; include_graveyard
        returns terminal/stale agents SEPARATELY for audit. A file owned by
        another LIVE agent is refused at the edit gate
        (cross_agent_scope_conflict). No file changes.
        """
        from .cross_agent_coordination import roster_view as _roster_view

        _hsid = (host_session_id or "").strip()
        if mine and not _hsid:
            try:
                from .mcp_server_runtime_helpers import current_calling_host_session_id

                _hsid = (current_calling_host_session_id() or "").strip()
            except Exception:
                _hsid = ""
        return _roster_view(
            _project_root(),
            include_graveyard=include_graveyard,
            session_id=session_id,
            state=state,
            host_session_id=_hsid,
        )

    @server.tool()
    @renders_as("list", title="connected agents")
    async def ai_agents(
        include_dead: bool = False,
        role: str = "",
        session_id: str = "",
    ) -> Any:
        """Role-based audit of the CONNECTED agents on the selected project --
        the actual interactive agents (conductors), keyed by host_session_id /
        agent identity, NOT lane subagents. Each shows its messagerie role
        (conductor/co_conductor/king), bound work session, liveness (its MCP
        process), agent_memory_epoch identity, and its spawned lane workers
        nested. Filters: role / session_id; include_dead also lists agents
        whose liveness could not be confirmed (for audit). For the lane-worker
        roster use ai_lane(action='agents'). No file changes.

        READ roster_status BEFORE reading agents (#603). 'ok' means the list
        is authoritative WITHIN THE SCOPE DECLARED BY roster_scope (see below)
        and an empty one really does mean no such agent is connected.
        'degraded' means some agents are verified but the list is known to be
        incomplete. 'unavailable' means NOTHING could be verified -- an empty
        agents list there must NOT be read as "the project is idle". The
        unconfirmed bindings are listed under 'unverifiable' and are never
        counted as live. Liveness is only ever asserted where it can be proven:
        each entry's live_source says how ('caller' -- the agent issuing this
        call, or 'boot_token_pid').

        roster_scope IS NOT DECORATION, AND #911 IS WHY IT EXISTS. This
        previously read "'ok' means the list is authoritative and an empty one
        really does mean nobody is connected." The arithmetic behind it was
        correct -- and that sentence was false, because it quantified over
        AGENTS while the computation quantifies over BINDINGS. Measured
        2026-08-25: four agents live on one box (a conductor, a host-spawned
        subagent, and two sub-subagents it spawned), all four issuing AIDOCS
        tool calls; this returned ONE, with lane_workers=[] and
        roster_status='ok'. The three host-spawned agents hold no binding row,
        no roster row and no lane scope, and the daemon structurally cannot
        enumerate them.

        A category the daemon cannot see must be DECLARED, not omitted under a
        confident 'ok' -- that is this codebase's own unknown-is-not-a-pass rule
        applied to a roster. The fix is a scope declaration rather than a
        permanent 'degraded': the list IS authoritative for what it covers, and
        flagging it degraded forever would be its own dishonesty and would train
        every reader to ignore the field.
        """
        from .agent_audit import connected_agents_audit
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        # #603: the caller is the ONE agent whose liveness is beyond doubt --
        # it is issuing this very call. Pass it in so a conductor always sees
        # itself even when the MCP generation that stamped its binding is
        # gone, and so a caller absent from the registry marks the roster
        # incomplete instead of answering with a confident empty list.
        _audit = connected_agents_audit(
            _project_root(),
            include_dead=include_dead,
            role=role,
            session_id=session_id,
            caller_host_session_id=(current_calling_host_session_id() or "").strip(),
        )
        # #911 -- DECLARE THE SCOPE IN THE PAYLOAD, not only in the docstring.
        # A prose-only correction would be #840 (prose-is-not-the-thing): the
        # reader that most needs this is an agent parsing the result, and it does
        # not read docstrings. Stated as data, a caller can act on it.
        if isinstance(_audit, dict):
            _audit["roster_scope"] = {
                "covers": ["conductors", "lane_workers"],
                "excludes": ["host_spawned_subagents"],
                "note": (
                    "roster_status describes BINDINGS, not the set of agents "
                    "running. Agents spawned by the HOST (e.g. Claude Code's "
                    "Agent/Task tool) are neither conductors nor lane workers: "
                    "they hold no binding row and the daemon cannot enumerate "
                    "them, yet they DO issue tool calls -- attributed to the "
                    "host_session_id they were spawned under. An empty or short "
                    "list is therefore never proof that no agent is working "
                    "(#911)."
                ),
            }
        return _audit

    @server.tool()
    @renders_as("status", title="ai_resolve_scope")
    async def ai_resolve_scope(
        message_id: str,
        lane_id: str,
        requested_path: str,
        session_id: str = "",
    ) -> Any:
        """Auto-resolve a scope expansion request if no lane conflict exists. Approves and expands scope automatically, or flags conflict for manual resolution."""
        from .conductor_comms import auto_resolve_scope_request

        project_root = _project_root()
        sid = session_id or _resolve_session_id(hub, project_root)
        return auto_resolve_scope_request(
            project_root,
            message_id,
            lane_id,
            requested_path,
            session_id=sid,
        )

    # Internal helper. Tool surface removed 2026-05-12 — ai_qa(mode='history').
    @renders_as("list", title="conductor messages")
    async def conductor_message_history(lane_id: str = "", limit: int = 50) -> Any:
        """Get conductor message history for a lane or all lanes."""
        from .conductor_comms import get_message_history

        return {"messages": get_message_history(_project_root(), lane_id, limit)}

    @server.tool()
    @renders_as("status", title="ai_resolve_backend")
    async def ai_resolve_backend(task_type: str, session_id: str = "") -> Any:
        """Resolve the best host + model + think_mode for a task type.

        Uses conductor.task_routing config to match task types to agent routes.
        Task types: refactor, implement, design, test, docs, research, debug, review, deploy.

        Example config in aidocs.toml:
          [conductor]
          task_routing = '{"implement":{"host":"claude","model":"claude-sonnet-4-6","think_mode":"low"},"design":{"host":"opencode","model":"google/gemini-2.5-pro","think_mode":"high"}}'
        """
        from .conductor_comms import resolve_backend_for_task

        return resolve_backend_for_task(_project_root(), task_type, session_id or None)

    @_dash_tool()
    @renders_as("status", title="conductor output")
    async def conductor_output(since: float = 0, limit: int = 100) -> Any:
        """Get recent conductor agent output (stdout/stderr). Dashboard polls this for live view.

        Args:
            since: Unix timestamp — only return lines after this time. Pass 0 for all.
            limit: Max lines to return.

        """
        with _conductor_output_lock:
            if since > 0:
                lines = [l for l in _conductor_output if l["timestamp"] > since]
            else:
                lines = list(_conductor_output)
        lines = lines[-limit:]
        proc = _conductor_process.get("process")
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "lines": lines,
            "total_buffered": len(_conductor_output),
        }

    # ── Execution management tools ──

    def _audit_delete(operation: str, reason: str, deleter: Any) -> Any:
        """AUDEL gate (audit_deletion_law): authenticated operator + admin
        permission + non-subagent + reason + audit-of-audit, then delete. Used by
        the three execution-audit deletion tools. Auto-prune on project-sync
        calls the SERVICE directly and is unaffected by this tool-level gate.

        SECURITY: there is NO operator_token argument — authentication is resolved
        ONLY from the calling host-session→operator-context binding, so no secret
        ever enters tool arguments (and therefore can't leak through MCP/tool-call
        logs, execution events, dashboard traces, renderers, or model history).
        """
        import os as _os

        from .audit_deletion_law import run_audit_deletion
        from .operator_auth_service import OperatorAuthService

        root = _project_root()
        svc = OperatorAuthService()
        ctx = None
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_host_session_id,
            )

            host_sid = current_calling_host_session_id()
            if host_sid:
                ctx = svc.resolve_operator_context_from_host_session(host_sid, root)
        except Exception:
            ctx = None
        has_perm = False
        if ctx is not None:
            try:
                has_perm = bool(
                    svc.require_permission(
                        ctx,
                        "admin.manage_config",
                        root,
                        scope_type="project",
                        scope_id=str(root).replace("\\", "/"),
                    ),
                )
            except Exception:
                has_perm = False
        is_subagent = bool(
            _os.environ.get("AIDOCS_EXPERT_ID") or _os.environ.get("AIDOCS_EXPERT_LANE_ID"),
        )
        user = getattr(ctx, "user_id", "") if ctx is not None else ""

        def _record(event_kind: str, status: str) -> None:
            hub.execution.record_event(
                root,
                event_kind=event_kind,
                source_kind="audit_deletion_law",
                capability_name=f"execution_{operation}",
                action_kind=operation,
                target_entity="execution_audit",
                status=status,
                principal_type="human",
                payload={"operation": operation, "reason": reason, "user": user},
            )

        return run_audit_deletion(
            operation=operation,
            reason=reason,
            ctx=ctx,
            has_permission=has_perm,
            is_subagent=is_subagent,
            record_intent=lambda: _record("audit_deletion_intent", "intent"),
            record_result=lambda _r: _record("audit_deletion_result", "deleted"),
            deleter=deleter,
        )

    @server.tool()
    @renders_as("status", title="execution clear tokens")
    async def execution_clear_token_usage(
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Clear token usage audit data (AUDEL-gated: host-session operator-auth +
        admin + reason + audit-of-audit; subagents/unauthenticated refused. No
        token argument — auth comes from the host-session binding only).
        """
        sid = session_id.strip() or None

        def _del() -> dict:
            # #885: this no longer deletes. It appends a token_usage_reset
            # watermark and the token queries floor on it. The AUDEL gate is
            # kept over it deliberately: hiding audit figures from a report is
            # still an operator act that must carry a reason and a name, even
            # though the evidence now survives it.
            return {
                "cleared": True,
                "session_id": sid,
                **hub.execution.reset_token_usage_counter(
                    _project_root(),
                    session_id=sid,
                    reason=reason,
                    actor="execution_clear_token_usage",
                ),
            }

        return _audit_delete("clear_token_usage", reason, _del)

    @server.tool()
    @renders_as("status", title="execution clear tool calls")
    async def execution_clear_tool_calls(
        reason: str = "",
        session_id: str = "",
    ) -> Any:
        """Clear tool-call audit events (AUDEL-gated; host-session auth only)."""
        sid = session_id.strip() or None

        def _del() -> dict:
            result = hub.execution.clear_tool_calls(_project_root(), session_id=sid)
            return {"cleared": True, **result, "session_id": sid}

        return _audit_delete("clear_tool_calls", reason, _del)

    @server.tool()
    @renders_as("status", title="execution prune")
    async def execution_prune(
        reason: str = "",
        keep_days: int = 7,
        max_events: int = 0,
    ) -> Any:
        """Prune old execution audit events (AUDEL-gated; host-session auth only).
        keep_days deletes by age, max_events caps total count.
        """

        def _del() -> dict:
            project_root = _project_root()
            result: dict[str, object] = {}
            if keep_days > 0:
                result["by_age"] = hub.execution.prune_old_events(project_root, keep_days=keep_days)
            if max_events > 0:
                result["by_size"] = hub.execution.prune_to_max_size(
                    project_root,
                    max_events=max_events,
                )
            if not result:
                result = hub.execution.auto_prune(project_root)
            result["current_counts"] = hub.execution.event_count(project_root)
            return result

        return _audit_delete("prune", reason, _del)

    @server.tool()
    @renders_as("status", title="execution usage")
    async def execution_usage_by_identity() -> Any:
        """Get token/tool usage broken down by host and agent identity."""
        project_root = _project_root()
        return {
            "by_host": hub.execution.usage_by_host(project_root),
            "by_agent": hub.execution.usage_by_agent(project_root),
            "counts": hub.execution.event_count(project_root),
        }

    # ── C.20 direct registry dispatch for INLINE legacy siblings ────
    # (tool-surface map §7.4, 2026-07-10): the ai_lane/ai_plan consolidators
    # dispatch these five inline deprecation-window names by literal legacy
    # name via tool_interface._delegate; registering the same closures makes
    # that dispatch in-process instead of a ~150ms create_server round-trip.
    # Same pattern as the ai_review/ai_guidance register_impl sites above and
    # the server_plan_task_tools C.20 block. Idempotent — the latest
    # create_server invocation wins. Zero behavior change: these are the exact
    # objects the @server.tool registrations above bind.
    # Coverage: test_c20_direct_dispatch (full legacy-sibling section).
    from . import tool_interface as _ti_c20_inline

    _ti_c20_inline.register_impl("ai_lane_exit", ai_lane_exit)
    _ti_c20_inline.register_impl("ai_plan_template", ai_plan_template)
    _ti_c20_inline.register_impl("ai_lane_control", ai_lane_control)
    _ti_c20_inline.register_impl("ai_lane_state", ai_lane_state)
    _ti_c20_inline.register_impl("ai_lane_agents", ai_lane_agents)

    # Patch tool descriptions from TOML — sync, runs before server starts
    # ── GATE_ONLY → BOTH migration (Empire directive 2026-05-29) ──────
    #
    # Doctrine: the three consolidators (ai_lane / ai_plan / ai_worker)
    #           were declared in tool_interface with surface=GATE_ONLY
    #           pending stdio binding. This block completes the
    #           migration — they're now wired on stdio, surface flipped
    #           to BOTH in the @tool decorator, and the migration
    #           doctrine ledger is cleared.
    # Why:      keeping consolidators GATE_ONLY forever defeats the
    #           point of consolidation. The legacy ai_lane_* / ai_plan_*
    #           / ai_worker_* bindings remain registered for a
    #           deprecation window — clients pinned to the old names
    #           keep working — but the canonical surface is the
    #           consolidator from this commit forward.
    # Apply:    each wrapper mirrors the registry function's signature
    #           and docstring so FastMCP introspection emits the
    #           same schema/description as the gate's catalog
    #           (test_gate_only_catalog_schema.py keeps the two surfaces
    #           on the same source of truth). The body delegates to
    #           tool_interface.<name> which routes by `action=...` to
    #           the underlying legacy bindings via _delegate.
    if tools_profile != "read_only":
        from . import tool_interface as _ti_cons

        @server.tool()
        async def ai_lane(
            action: str,
            session_id: str = "",
            lane_id: str = "",
            worker_id: str = "",
            state: str = "",
            metadata: dict = None,
            reason: str = "",
            tools: list = None,
            prompt: str = "",
            limit: int = 20,
            confirm_token: str = "",
            backend: str = "",
            model: str = "",
            verbose: bool = False,
            review_id: str = "",
            verdict: str = "",
            tail: bool = True,
            adhoc: bool = False,
            allowed_files: list = None,
            allowed_tools: list = None,
            verification: str = "",
        ) -> Any:
            # BY-LANE resolution (120% clause B): the worker-targeting conductor
            # actions take lane_id; resolve the live/most-recent worker here
            # (the wrapper holds runtime) before the consolidator dispatches.
            # Explicit worker_id always wins.
            # WAR D (#452) Task 3: "send"/"inbox" added — a message sent BY
            # LANE previously landed on worker_id="" (an unreadable mailbox
            # row: stored, never deliverable to the lane's actual worker).
            if (
                action in ("status", "kill", "resume", "events", "send", "inbox")
                and not worker_id
                and lane_id
            ):
                resolved = runtime._agent_expert.resolve_worker_for_lane(lane_id)
                if resolved:
                    worker_id = resolved
            if action == "activity":
                # #289 lane observability: audit-ledger-backed activity view
                # (execution_events — the dashboard's own table). Dispatched
                # here straight to the register_impl-bound impl; uses only
                # existing ai_lane params so the public inputSchema and the
                # gate goldens do not drift.
                return _ti_cons._delegate(
                    "ai_lane_activity",
                    session_id=session_id,
                    lane_id=lane_id,
                    worker_id=worker_id,
                    limit=limit,
                )
            if action == "delegate":
                # Delegated single-task lane (no plan required): spawns one
                # tracked lane worker whose brief + outcome live on a
                # project-backlog tracking entry. Dispatched here straight
                # to the register_impl-bound impl; uses only existing
                # ai_lane params so the public inputSchema and the gate
                # goldens do not drift. The gate-surface consolidator
                # branch (tool_interface.ai_lane) is conductor-owned and
                # filed as a follow-up backlog entry (mirror of the
                # action='activity' split).
                # MODE 1 (backlog-redirect): lane_id MUST be forwarded. Without
                # it an adoption request silently degrades into a mode-2 MINT --
                # a duplicate entry carrying the real worker while the operator
                # believes item #N is being worked, and a brief that refers to a
                # body it does not contain. Observed twice live (#505, #521).
                #
                # THIS is the live path for a local MCP call; tool_interface.ai_lane
                # is the gate-surface twin. BOTH forward lane_id and BOTH are pinned
                # in test_delegated_single_lane.py, because fixing only one twin is
                # exactly how mode 1 shipped "green" and stayed unreachable: 27 tests
                # passed against the consolidator branch while every real dispatch
                # came through here and fell back to minting (#522).
                return _ti_cons._delegate(
                    "ai_lane_delegate",
                    session_id=session_id,
                    prompt=prompt,
                    backend=backend,
                    model=model,
                    lane_id=lane_id,
                )
            return _ti_cons.ai_lane(
                action=action,
                session_id=session_id,
                lane_id=lane_id,
                worker_id=worker_id,
                state=state,
                metadata=metadata,
                reason=reason,
                tools=tools,
                prompt=prompt,
                limit=limit,
                confirm_token=confirm_token,
                backend=backend,
                model=model,
                verbose=verbose,
                review_id=review_id,
                verdict=verdict,
                tail=tail,
                # #779: the spawner's authority + the ad-hoc lane's scope. THIS
                # is the live path for a local MCP call; tool_interface.ai_lane
                # is the gate-surface twin, and the note on the delegate branch
                # above says why both must move together -- #522 shipped "green"
                # with 27 tests passing against the consolidator while every
                # real dispatch came through here. #779 repeated it exactly:
                # 10 tests green against the twin, and the live call was
                # rejected at the schema before reaching any of it.
                adhoc=adhoc,
                allowed_files=allowed_files,
                allowed_tools=allowed_tools,
                verification=verification,
            )

        # Pin description from the registry spec so the host sees the
        # full consolidator docstring (modes / dispatch rules) instead
        # of an empty docstring on the wrapper.
        ai_lane.__doc__ = _ti_cons._TOOLS["ai_lane"].description

        @server.tool()
        async def ai_plan(
            action: str,
            session_id: str = "",
            lane_id: str = "",
            spec_text: str = "",
            spec_path: str = "",
            scope: str = "",
            constraints: list = None,
            file_path: str = "",
            paused_lane_id: str = "",
            conflicting_lane_id: str = "",
            target_lane_id: str = "",
            signal_kind: str = "",
            detail: str = "",
            packet_result: dict = None,
            packet_result_path: str = "",
            reason: str = "",
            timeout: int = 0,
            view: str = "",
            transition: str = "",
            coord: str = "",
            template_only: bool = False,
            backend: str = "",
            model: str = "",
            target_project: str = "",
        ) -> Any:
            return _ti_cons.ai_plan(
                action=action,
                session_id=session_id,
                lane_id=lane_id,
                spec_text=spec_text,
                spec_path=spec_path,
                scope=scope,
                constraints=constraints,
                file_path=file_path,
                paused_lane_id=paused_lane_id,
                conflicting_lane_id=conflicting_lane_id,
                target_lane_id=target_lane_id,
                signal_kind=signal_kind,
                detail=detail,
                packet_result=packet_result,
                packet_result_path=packet_result_path,
                reason=reason,
                timeout=timeout,
                view=view,
                transition=transition,
                coord=coord,
                template_only=template_only,
                backend=backend,
                model=model,
                target_project=target_project,
            )

        ai_plan.__doc__ = _ti_cons._TOOLS["ai_plan"].description

        @server.tool()
        async def ai_worker(
            action: str,
            worker_id: str = "",
            lane_id: str = "",
            reason: str = "",
            verbose: bool = False,
            confirm_token: str = "",
        ) -> Any:
            # BY-LANE resolution (120% clause B: "resume + kill by lane, not
            # opaque worker_id"). A conductor passes lane_id; we resolve the
            # live (or most-recent) worker for that lane. Explicit worker_id
            # still wins. The kill confirm token binds to the RESOLVED id.
            if not worker_id and lane_id:
                worker_id = runtime._agent_expert.resolve_worker_for_lane(lane_id) or ""
                if not worker_id:
                    return {
                        "_error": "no_worker_for_lane",
                        "_detail": f"no worker found for lane_id={lane_id!r}",
                    }
            return _ti_cons.ai_worker(
                action=action,
                worker_id=worker_id,
                reason=reason,
                verbose=verbose,
                confirm_token=confirm_token,
            )

        ai_worker.__doc__ = _ti_cons._TOOLS["ai_worker"].description

    _patch_tool_descriptions_sync(server)
    from .mode_schema import apply_mode_schemas as _apply_mode_schemas

    _apply_mode_schemas(server)

    # related_project_* auto-wrappers disabled 2026-04-24:
    # the 269 wrapper clones were bloating the tool surface
    # (~50% of all registered tools). Cross-project invocation
    # now flows through the handful of hand-written related_project
    # entry tools (register, list, unregister, handoff, ai_search,
    # symbol_bundle, subsystem_bundle, compare_concept); those stay
    # because they have explicit implementations in
    # server_project_admin_tools.py. The auto-wrapper function
    # below is retained for future per-tool opt-in wrapping.
    return server


def _auto_register_related_project_wrappers(server: FastMCP, hub: Any) -> None:
    """Emit one related_project_<tool> per conductor-facing tool.

    For each tool X the conductor registers, a sibling tool
    `related_project_X(target_project, **X.args)` is added that:
      1. Resolves target_project via the registry (fails if unknown
         or unregistered).
      2. Refuses if target_project resolves to the conductor's own
         root (meaningless — use X directly).
      3. Sets the ContextVar override to the target root.
      4. Calls the original tool fn with the rest of the args.
      5. Clears the override in a finally.

    Wraps every tool (reads, writes, indexing) — the registry entry
    IS the authorization: if the user registered it, the tool can
    run against it.

    Skipped: tools that already start with `related_project_` (avoid
    doubling), `project_init` (needs an explicit absolute path), the
    registry-management tools (`related_project_register/list/unregister`
    — they're self-referential).
    """
    import asyncio
    import inspect
    import keyword
    import re

    from .mcp_server_runtime_helpers import (
        resolve_project_root,
        with_target_project_root,
    )

    _SKIP_PREFIXES = ("related_project_",)
    _SKIP_EXACT = {
        "project_init",
    }

    # Hard allowlist for any string that lands in the exec'd source.
    # FastMCP's own registration should already guarantee identifier
    # rules for tool/param names, but exec deserves belt-and-suspenders
    # validation — if any component fails this regex we skip the tool
    # instead of compiling arbitrary text. Single source of truth for
    # the safety audit: every variable substituted into the source
    # template passes _IDENT before exec runs.
    _IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def _is_safe_ident(s: str) -> bool:
        return bool(s) and bool(_IDENT.match(s)) and not keyword.iskeyword(s)

    try:
        existing_tools = asyncio.run(server.list_tools())
    except RuntimeError:
        # Outer loop running (typical in pytest-asyncio / uvicorn).
        # Run list_tools in a dedicated thread with its own fresh loop
        # so we don't touch the caller's running loop.
        import threading

        result_box: dict[str, Any] = {}

        def _run_in_thread() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                coro = server.list_tools()
                result_box["tools"] = loop.run_until_complete(coro)
            except Exception as exc:
                result_box["error"] = exc
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        t = threading.Thread(target=_run_in_thread)
        t.start()
        t.join()
        existing_tools = result_box.get("tools", [])

    wrapped_count = 0
    for tool in existing_tools:
        name = getattr(tool, "name", "")
        if not name or name in _SKIP_EXACT:
            continue
        if any(name.startswith(p) for p in _SKIP_PREFIXES):
            continue
        fn = getattr(tool, "fn", None)
        if fn is None or not callable(fn):
            continue
        is_async = inspect.iscoroutinefunction(fn)

        # Skip if the tool already exposes target_project (signature
        # collision — those are cross-project aware already).
        sig = inspect.signature(fn)
        if any(p.name == "target_project" for p in sig.parameters.values()):
            continue

        # Safety audit: every string that will land in the exec'd
        # source must pass a strict identifier check. If FastMCP let
        # a weird name through (it shouldn't — tool/param names must
        # be identifiers — but exec is the one spot we can't trust
        # upstream invariants for), we skip the tool outright.
        if not _is_safe_ident(name):
            try:
                import sys as _sys

                _sys.stderr.write(
                    f"[related_project] skip {name!r}: non-identifier tool name, unsafe for exec\n",
                )
            except Exception:
                pass
            continue
        if not all(
            _is_safe_ident(p.name)
            for p in sig.parameters.values()
            if p.kind
            not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ):
            try:
                import sys as _sys

                _sys.stderr.write(
                    f"[related_project] skip {name}: non-identifier param name, unsafe for exec\n",
                )
            except Exception:
                pass
            continue

        orig_desc = (tool.description or f"Run {name}").rstrip()
        wrapper_desc = (
            f"Cross-project wrapper: run `{name}` against a registered "
            f"related project. target_project must be the name passed "
            f"to related_project_register. Conductor's own project "
            f"is refused — call `{name}` directly for that.\n\n"
            f"Original tool description:\n{orig_desc}"
        )
        wrapper_name = f"related_project_{name}"
        # Belt + suspenders: even though `name` passed _is_safe_ident
        # above, confirm the final wrapper_name is ALSO a clean
        # identifier before we build source code around it.
        if not _is_safe_ident(wrapper_name):
            continue

        # Build per-tool wrapper with a real signature (FastMCP
        # refuses **kwargs-only functions). Use exec to synthesize
        # an async function whose params are `target_project` +
        # each of the original tool's params by name — then close
        # over the dispatcher that does the rerouting.
        orig_params = [
            p
            for p in sig.parameters.values()
            if p.kind
            not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]

        # Build parameter list source: `target_project: str, p1=..., p2=..., ...`
        # Keep it simple — no annotations (FastMCP builds schemas from
        # annotations but losing them just means less schema detail,
        # tool still runs). Defaults must render as literal source:
        # int/float/bool/None or plain printable single-line strings.
        # Anything else → no default (making it required). Safe; agents
        # already pass these. Explicit rejection of strings with
        # newlines, control chars, or non-printable bytes keeps the
        # exec audit story simple: "every default is a trivially-safe
        # literal."
        def _fmt_default(v):
            if v is inspect.Parameter.empty:
                return None
            if v is None or isinstance(v, bool):
                return repr(v)
            if isinstance(v, (int, float)):
                return repr(v)
            if isinstance(v, str):
                # Reject anything that could smuggle a newline /
                # control char into the source. repr() already
                # escapes these but belt-and-suspenders for exec.
                if not v.isprintable() or "\n" in v or "\r" in v:
                    return None
                return repr(v)
            return None  # complex default → treat as required

        # Annotation preservation: FastMCP builds JSON-schema types
        # from annotations, and the MCP host coerces incoming args
        # based on that schema. Dropping annotations made every arg
        # arrive as str, breaking any tool that expected int/bool
        # (ai_get_lines start_line>=1, etc.). Pass the annotation
        # OBJECT through the exec namespace — resolves to the same
        # class the original tool had, without stringifying.
        def _annotation_key(p_name: str) -> str:
            return f"__ann_{p_name}"

        ann_slots: dict[str, Any] = {}
        parts = ["target_project: str"]
        passthrough_args = []
        for p in orig_params:
            d = _fmt_default(p.default)
            if p.annotation is not inspect.Parameter.empty:
                ann_key = _annotation_key(p.name)
                ann_slots[ann_key] = p.annotation
                ann_suffix = f": {ann_key}"
            else:
                ann_suffix = ""
            if d is None:
                parts.append(f"{p.name}{ann_suffix}")
            else:
                parts.append(f"{p.name}{ann_suffix}={d}")
            passthrough_args.append(f"{p.name}={p.name}")
        param_src = ", ".join(parts)
        pass_src = ", ".join(passthrough_args)
        src = (
            f"async def {wrapper_name}({param_src}):\n"
            f"    return await _dispatcher(target_project, dict("
            f"{pass_src}))\n"
        )
        # Seed ns with common annotation names so FastMCP's type-hint
        # resolver (which may re-eval string annotations in the
        # wrapper's __globals__) finds them. __ann_X still carries
        # the exact annotation objects for param-level substitution;
        # these names are for downstream eval on the wrapper source.
        from pathlib import Path as _ann_Path
        from typing import (
            Any as _ann_Any,
        )
        from typing import (
            Literal as _ann_Literal,
        )
        from typing import (
            Optional as _ann_Optional,
        )
        from typing import (
            Union as _ann_Union,
        )

        ns: dict[str, Any] = dict(ann_slots)
        ns["Any"] = _ann_Any
        ns["Literal"] = _ann_Literal
        ns["Optional"] = _ann_Optional
        ns["Union"] = _ann_Union
        ns["Path"] = _ann_Path

        def _make_dispatcher(_fn, _is_async, _tool_name):
            async def _dispatch(_target_project: str, _kwargs: dict):
                if not _target_project:
                    return {
                        "ok": False,
                        "error": (
                            "target_project is required. Pass the name "
                            "registered via related_project_register."
                        ),
                    }
                conductor_root = resolve_project_root()
                target_root = hub.related.resolve_related_project_path(
                    conductor_root,
                    _target_project,
                )
                if target_root is None:
                    return {
                        "ok": False,
                        "error": (
                            f"target_project '{_target_project}' not "
                            f"registered (or path no longer exists). "
                            f"Call related_project_list to see known "
                            f"targets."
                        ),
                    }
                try:
                    if target_root.resolve() == conductor_root.resolve():
                        return {
                            "ok": False,
                            "error": (
                                f"target_project '{_target_project}' "
                                f"resolves to the conductor's own root. "
                                f"Call `{_tool_name}` directly."
                            ),
                        }
                except OSError:
                    pass
                with with_target_project_root(target_root):
                    if _is_async:
                        return await _fn(**_kwargs)
                    return _fn(**_kwargs)

            return _dispatch

        dispatcher = _make_dispatcher(fn, is_async, name)
        ns["_dispatcher"] = dispatcher
        # Doctrine 2026-05-29 (Empire semgrep re-seal): replaced
        # `exec(src, ns)` with compile() + types.FunctionType so
        # the no-eval rule is honored. Belt-and-suspenders identifier
        # validation BEFORE compile — defense in depth against any
        # future code path that lets an agent-supplied name into
        # `wrapper_name` or a parameter name. None of those paths
        # exist today (names come from server-registered tool specs
        # and inspect.signature on a real Python function), but the
        # validation makes that property structural, not incidental.
        if not wrapper_name.isidentifier():
            continue
        if not all(p.name.isidentifier() for p in orig_params):
            continue
        try:
            import types as _types

            module_code = compile(src, "<aidocs-related-wrapper>", "exec")
            # The compiled module's co_consts holds the inner code
            # objects; find the one whose co_name matches wrapper_name.
            fn_code = next(
                (
                    c
                    for c in module_code.co_consts
                    if isinstance(c, _types.CodeType) and c.co_name == wrapper_name
                ),
                None,
            )
            if fn_code is None:
                continue
            wrapper = _types.FunctionType(fn_code, ns, name=wrapper_name)
            wrapper.__doc__ = wrapper_desc
            # Replace string annotation placeholders (__ann_X) on the
            # wrapper with the actual annotation objects so FastMCP's
            # get_type_hints/eval doesn't re-resolve them in an empty
            # namespace and raise NameError.
            resolved_ann: dict[str, Any] = {}
            for p_name_iter, ann_obj in (
                (p.name, p.annotation)
                for p in orig_params
                if p.annotation is not inspect.Parameter.empty
            ):
                resolved_ann[p_name_iter] = ann_obj
            if resolved_ann:
                resolved_ann["target_project"] = str
                wrapper.__annotations__ = resolved_ann
        except SyntaxError:
            # Param name collision with Python keyword, etc. — skip.
            continue
        try:
            from fastmcp.tools.tool import Tool as _Tool

            tool_obj = _Tool.from_function(
                wrapper,
                name=wrapper_name,
                description=wrapper_desc,
            )
            server.add_tool(tool_obj)
            wrapped_count += 1
        except Exception as exc:
            # Some tools may collide or have incompatible signatures;
            # log once so we can diagnose but keep going — originals
            # still work and the cross-project surface is best-effort.
            try:
                import sys as _sys

                _sys.stderr.write(
                    f"[related_project] skip {name}: {type(exc).__name__}: {str(exc)[:100]}\n",
                )
            except Exception:
                pass
            continue

    try:
        import sys as _sys

        _sys.stderr.write(f"[related_project] auto-wrapped {wrapped_count} tools\n")
    except Exception:
        pass


def _patch_tool_descriptions_sync(server: FastMCP) -> None:
    """Override tool docstrings with terse agent-facing descriptions from TOML."""
    from .config import _ACTION_HOOK_DEFAULTS, _get_dotted

    descriptions = _get_dotted(_ACTION_HOOK_DEFAULTS, "tool_descriptions")
    if not isinstance(descriptions, dict):
        return
    provider = getattr(server, "_local_provider", None)
    if provider is None:
        return
    components = getattr(provider, "_components", None)
    if not isinstance(components, dict):
        return

    for short_name, desc in descriptions.items():
        if not isinstance(desc, str):
            continue
        key = f"tool:{short_name}@"
        tool = components.get(key)
        if tool is not None and hasattr(tool, "description"):
            tool.description = desc


def enforce_package_integrity_or_refuse(home=None, *, gate=None, env=None) -> dict:
    """Fail-closed trusted-code boundary for startup. Returns the verdict when
    the install may run (editable/unverified/matched); RAISES SystemExit on
    proven drift of a non-editable install. Drift can only be overridden by the
    explicit ``AIDOCS_ALLOW_PACKAGE_DRIFT`` escape (never silent).
    """
    import os as _os
    from pathlib import Path as _Path

    from . import package_integrity as _pi

    env = _os.environ if env is None else env
    gate = gate or _pi.startup_integrity_gate
    home = _Path.home() if home is None else home
    try:
        v = gate(home)
    except Exception as exc:  # integrity check itself broke
        # Cannot verify ⟹ cannot trust the dangerous (remote) surface, but do
        # not brick local startup on an internal error; report unverified.
        return {
            "ok": True,
            "drifted": False,
            "unverified": True,
            "remote_trustworthy": False,
            "reason": f"integrity check failed: {exc!r}",
        }
    if not v.get("ok"):
        escape = str(env.get("AIDOCS_ALLOW_PACKAGE_DRIFT") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not escape:
            import sys as _sys

            print(
                "\033[31m✗ AIDOCS refusing to start — package integrity drift\033[0m\n"
                f"    {v.get('reason')}\n"
                "    The installed aidocs_mcp code differs from the verified "
                "runtime manifest. After a legitimate upgrade run "
                "`aidocs runtime --record-package`; otherwise this is tampering.\n"
                "    (override only if you understand the risk: "
                "AIDOCS_ALLOW_PACKAGE_DRIFT=1)",
                file=_sys.stderr,
            )
            raise SystemExit(3)
        v = {**v, "drift_overridden": True}
    return v


# #435 (traced 2026-07-25): the daemon serves STATELESS http. FastMCP's default
# (stateless_http=False) mints an Mcp-Session-Id held in ONE process's in-memory
# StreamableHTTPSessionManager._server_instances. That makes a session
# PROCESS-BOUND, which is fatal here: on a deploy the watchdog overlap-restarts
# and flips LoopbackProxy to a new backend, and because uvicorn closes idle
# keep-alive connections after 5s the operator's next tool call always rides a
# FRESH socket — into a process that never minted their session id -> HTTP
# 400/404 -> "MCP server disconnected" on EVERY deploy. The #432 connection-aware
# drain keeps ALREADY-OPEN sockets on the old backend but cannot save a new one,
# which is why drops continued after it shipped. Stateless removes the bound
# state entirely: no session id, so ANY backend can answer ANY request and the
# hot-swap is invisible. Pinned by tests/host/test_daemon_hotswap_session_survival.py.
#
# KNOWN TRADEOFF (measured, not hand-waved): stateless serves POST/DELETE only,
# so there is no standalone GET SSE stream for OUT-OF-BAND server pushes. AIDOCS
# uses no elicitation/sampling/progress, and run notifications ride tool RESULTS,
# but the dynamic tool-surfacing path above (~line 2080) leans on FastMCP's
# auto notifications/tools/list_changed from server.enable(). Measured 2026-07-25:
# that notification reaches the client on NEITHER transport when it is emitted
# during a POST (it is not written to the request's own SSE stream), so this
# change is PARITY for the in-request case; what stateless gives up is only the
# push that a held-open GET stream could have carried. Deferred tools still
# surface the documented way — ToolSearch returns their schemas in its RESULT —
# and worst case a freshly granted tool appears in the host's list one reconnect
# later. That is a far smaller cost than losing the whole session every deploy.
DAEMON_STATELESS_HTTP = True


def main() -> None:
    import argparse
    import atexit
    import os

    parser = argparse.ArgumentParser(description="Run the AIDOCS MCP server.")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help=(
            "Identify this MCP process as the AIDOCS dashboard. Unlocks "
            "dashboard-only tools (conductor_start / conductor_send / "
            "conductor_stop / conductor_output) that are hidden from "
            "agent MCP clients. Only the dashboard's Tauri spawner "
            "should pass this flag."
        ),
    )
    # Daemon mode (#249): serve MCP over local HTTP instead of stdio. Claude
    # Code auto-reconnects HTTP servers (it never restarts crashed stdio ones),
    # so with the aidocs service watchdog this closes the crash->reconnect loop
    # and kills the /mcp-after-deploy ritual. Loopback-only by design.
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve MCP over HTTP on 127.0.0.1 (daemon mode) instead of stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8748,
        help="Port for --http mode (default 8748).",
    )
    args, _ = parser.parse_known_args()

    # Shut down thread pool on exit; handle SIGTERM/SIGHUP for graceful stop (Windows lacks SIGHUP)
    def _cleanup():
        _tool_executor.shutdown(wait=False, cancel_futures=True)

    atexit.register(_cleanup)

    # On Unix, handle SIGHUP/SIGTERM for graceful shutdown
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    # Trusted-code boundary: refuse to start on proven package drift.
    enforce_package_integrity_or_refuse()

    # The agent-facing serve entry: deploy is HIDDEN here (local MCP surface).
    server = create_server(dashboard_mode=bool(args.dashboard), expose_deploy=False)
    # #982: actor reconciliation belongs to BOOT, not to construction. This is
    # the daemon actually starting — the one moment where sweeping actors whose
    # LEASE proves them gone is a lifecycle event rather than a side effect of
    # somebody needing a server object. Never raises; hygiene must not block a
    # boot, and a skipped reconciliation only means stale rows survive, which is
    # the lesser evil this subsystem explicitly chooses over stranding live work.
    server._conductor_binding_prune = reconcile_conductor_actors()
    if args.http:
        import logging as _logging

        from .aidocs_service import write_daemon_health

        # Bench 2026-07-06: uvicorn access-logging added measurable per-call
        # overhead (and log spam) — one line per POST. Warnings/errors only.
        _logging.getLogger("uvicorn.access").setLevel(_logging.WARNING)
        write_daemon_health(port=int(args.port), pid=os.getpid())
        # #280 Phase 2: connection-scoped project resolution. The shared daemon
        # serves many project windows from ONE process, so each request must
        # resolve to ITS OWN root. The middleware reads a validated (commissioned)
        # ?root= / X-AIDOCS-Project-Root declaration per request and scopes the
        # existing _target_project_root_override for that call. DORMANT until the
        # registry emits ?root= URLs — a request with no declaration is a
        # transparent pass-through, so stdio and single-tenant HTTP are unchanged.
        try:
            from .project_scope import make_project_scope_middleware

            server.add_middleware(make_project_scope_middleware())
            # #280 clause 3: activate strict refusal (rootless multi-tenant call
            # → actionable error, never a process-global) from config. OFF unless
            # the operator set mcp.multitenant_strict AFTER regenerating scoped
            # ?root= URLs — otherwise every not-yet-scoped window would refuse.
            try:
                from .config import get_setting
                from .mcp_server_runtime_helpers import set_multitenant_strict

                set_multitenant_strict(
                    bool(get_setting("mcp.multitenant_strict", default=False)),
                )
            except Exception:
                pass
        except Exception:
            _logging.getLogger("aidocs.daemon").warning(
                "project-scope middleware unavailable; shared daemon runs "
                "WITHOUT per-connection isolation (single-tenant only)",
                exc_info=True,
            )
        # Loopback-only: never bind beyond 127.0.0.1 — remote access is the
        # outer gate's job (auth'd), not the local daemon's.
        # stateless_http (#435): see DAEMON_STATELESS_HTTP — keeps the operator's
        # session alive across a deploy's backend hot-swap.
        server.run(
            transport="http",
            host="127.0.0.1",
            port=int(args.port),
            stateless_http=DAEMON_STATELESS_HTTP,
        )
    else:
        server.run()


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
