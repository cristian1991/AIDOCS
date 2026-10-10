"""ai_git op=switch — the governed branch checkout/create path.

`branch` is honoured ONLY by op=switch (every other op keeps the #762 refusal:
never accept-and-ignore). The name is validated strictly BEFORE git runs
(refuse, don't quote), git then decides (`git check-ref-format --branch`),
and the switch runs as `git switch --no-guess <b>` or `git switch -c <b>` —
never --force / --discard-changes, never a path checkout. If git refuses
(dirty tree conflict, branch already exists, unknown branch) its error is
surfaced verbatim with ok=False.

The receipt is read back AFTER the switch from `git rev-parse --abbrev-ref
HEAD` + `git rev-parse HEAD`; ok is True only when HEAD is the requested
branch.

Seam: ``git_run(argv) -> (rc, stdout, stderr)`` so this is testable against a
real tmp repo without a server.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

GitRun = Callable[[list[str]], "tuple[int, str, str]"]

_ALLOWED = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_BRANCH_LEN = 200


def branch_name_refusal(name: str) -> str:
    """'' when ``name`` is an acceptable branch name; otherwise the reason.

    Stricter than git check-ref-format: a conservative charset (no shell
    metacharacters, spaces, '@', '~', '^', ':', '?', '*', '[', '\\')."""
    b = str(name or "")
    if not b.strip():
        return "branch is required"
    if b != b.strip():
        return "branch must not have leading/trailing whitespace"
    if len(b) > MAX_BRANCH_LEN:
        return f"branch longer than {MAX_BRANCH_LEN} chars"
    if not _ALLOWED.match(b):
        return "branch may contain only letters, digits, '.', '_', '-', '/'"
    if b.startswith("-"):
        return "branch must not start with '-'"
    if b.startswith("/") or b.endswith("/") or "//" in b:
        return "branch must not start/end with '/' or contain '//'"
    if ".." in b:
        return "branch must not contain '..'"
    if b.endswith("."):
        return "branch must not end with '.'"
    if b == "HEAD":
        return "branch must not be 'HEAD'"
    for comp in b.split("/"):
        if comp.startswith(".") or comp.endswith(".lock"):
            return "a branch path component must not start with '.' or end with '.lock'"
    return ""


_IN_PROGRESS_MARKERS = (
    ("MERGE_HEAD", "merge"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase/am"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)

LivenessProbe = Callable[[], "list[str]"]
AuditHook = Callable[[str, "dict[str, Any]"], bool]
#: () -> CONTEXT MANAGER (``contextlib.AbstractContextManager[Any]``) held from
#: BEFORE the liveness/clean samples through the HEAD receipt
#: (branch_transition_barrier.holding). Entering it raises when another holder
#: owns the project's barrier. The return is spelled ``Any`` rather than the
#: protocol on purpose: naming ``AbstractContextManager`` costs a
#: TYPE_CHECKING-only import that exists solely to satisfy the annotation
#: string, and the contract above is the authority either way.
BarrierFactory = Callable[[], Any]


def switch_argv(branch: str, create: bool) -> list[str]:
    """The ONLY two git argv shapes ai_git runs (the egress waiver matches these).

    ``--no-overwrite-ignore``: by default `git switch` silently OVERWRITES an
    ignored local file when the target branch tracks that path, and
    `git status --porcelain` never shows ignored files, so the clean-tree check
    cannot see the loss (r0b send-back, blocker 1). With the flag git refuses
    and names the file. For -c the new branch has the current tree, so nothing
    can be overwritten; the flag is passed anyway for one uniform shape. It
    goes BEFORE -c because -c consumes the next token as its value."""
    if create:
        return ["switch", "--no-overwrite-ignore", "-c", branch]
    return ["switch", "--no-guess", "--no-overwrite-ignore", branch]


def _refuse(b: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "op": "switch",
        "requested_branch": b,
        "switch_ran": False,
        "error": f"git switch refused: {reason}",
        **extra,
    }


def run_git_switch(
    branch: str,
    create: bool,
    git_run: GitRun,
    *,
    project_root: Path | str | None = None,
    liveness: LivenessProbe | None = None,
    switch_exec: GitRun | None = None,
    audit: AuditHook | None = None,
    barrier: BarrierFactory | None = None,
) -> dict[str, Any]:
    """Safe branch switch.

    0. the project's BRANCH-TRANSITION BARRIER is acquired first (``barrier()``;
       unwired or already held elsewhere REFUSES) and held until the HEAD
       receipt is read, so no other governed tool call / edit / shell run /
       switch can start between the samples below and the switch (TOCTOU);
    then, sampled UNDER the barrier:
    B. no other live actor on this project — ``liveness()`` returns the
       blocking actors; it raising, or not being wired, REFUSES (fail closed);
    A. no code loss — `git status --porcelain` empty (tracked staged/unstaged
       AND untracked non-ignored), no merge/rebase/cherry-pick/revert/bisect in
       progress, and the switch runs with --no-overwrite-ignore so an IGNORED
       local file the target branch tracks is refused by git, not overwritten;
    the switch itself runs through ``switch_exec`` (default ``git_run``), never
    with --force/--discard-changes/--merge. ``audit('requested', ...)`` must
    succeed before the switch when an audit hook is wired; a failed result row
    is reported as ``audit_degraded=True`` (the switch may already have run).
    """
    b = str(branch or "")
    why = branch_name_refusal(b)
    if why:
        return _refuse(b, why)

    # 0 — branch-transition barrier (fail closed). Taken before ANY git
    # subprocess runs, so a concurrent switch is refused with the barrier's
    # own reason rather than a downstream egress refusal.
    if barrier is None:
        return _refuse(b, "the branch-transition barrier is not wired, so concurrent mutations cannot be excluded")
    try:
        cm = barrier()
        cm.__enter__()
    except Exception as exc:  # noqa: BLE001 - held elsewhere, or store failure
        return _refuse(b, f"could not acquire the project's branch-transition barrier: {exc}")
    try:
        return _switch_under_barrier(b, create, git_run, project_root, liveness, switch_exec, audit)
    finally:
        cm.__exit__(None, None, None)


def _switch_under_barrier(
    b: str,
    create: bool,
    git_run: GitRun,
    project_root: Path | str | None,
    liveness: LivenessProbe | None,
    switch_exec: GitRun | None,
    audit: AuditHook | None,
) -> dict[str, Any]:
    rc, _out, err = git_run(["check-ref-format", "--branch", b])
    if rc != 0:
        return _refuse(
            b, f"git check-ref-format rejected {b!r}", git_error=(err or "").strip()[-400:]
        )

    # B — concurrent actors (fail closed), sampled under the barrier.
    if liveness is None:
        return _refuse(b, "liveness authority is not wired, so concurrent agents cannot be ruled out")
    try:
        blockers = list(liveness() or [])
    except Exception as exc:  # noqa: BLE001 - fail closed, name the cause
        return _refuse(
            b,
            f"could not determine whether other agents are active on this project ({exc!r}); "
            "refusing rather than risk breaking a concurrent agent",
        )
    if blockers:
        return _refuse(
            b,
            "other live actor(s) are working on this project and a branch switch would change "
            "files under them: " + "; ".join(blockers[:10])
            + ". Retry when they have finished.",
            blocking_actors=blockers,
        )

    # A — no code loss, sampled under the barrier.
    src, status_out, status_err = git_run(["status", "--porcelain", "--untracked-files=normal"])
    if src != 0:
        return _refuse(b, "git status failed, cannot prove the tree is clean",
                       git_error=(status_err or "").strip()[-400:])
    dirty = [ln for ln in (status_out or "").splitlines() if ln.strip()]
    if dirty:
        return _refuse(
            b,
            f"the working tree has {len(dirty)} uncommitted/untracked change(s); commit or "
            "stash them first (nothing is discarded by ai_git): " + ", ".join(dirty[:10]),
            dirty=dirty[:50],
        )
    grc, git_dir, gerr = git_run(["rev-parse", "--git-dir"])
    if grc != 0 or not (git_dir or "").strip():
        return _refuse(b, "could not locate the git directory", git_error=(gerr or "").strip()[-400:])
    gd = Path((git_dir or "").strip().splitlines()[0])
    if not gd.is_absolute():
        if project_root is None:
            return _refuse(b, "project_root unknown, cannot check for an in-progress operation")
        gd = Path(project_root) / gd
    for marker, label in _IN_PROGRESS_MARKERS:
        if (gd / marker).exists():
            return _refuse(b, f"a {label} is in progress ({marker}); finish or abort it first")

    frc, from_branch, _ = git_run(["rev-parse", "--abbrev-ref", "HEAD"])
    fsrc, from_sha, _ = git_run(["rev-parse", "HEAD"])
    argv = switch_argv(b, create)
    receipt: dict[str, Any] = {
        "op": "switch",
        "requested_branch": b,
        "create": bool(create),
        "command": "git " + " ".join(argv),
        "from_branch": (from_branch or "").strip() if frc == 0 else None,
        "from_sha": (from_sha or "").strip() if fsrc == 0 else None,
    }
    if audit is not None and not audit("requested", dict(receipt)):
        return _refuse(b, "the switch audit row could not be written", switch_ran=False)

    rc, out, err = (switch_exec or git_run)(argv)
    receipt["switch_ran"] = True
    receipt["exit_code"] = rc
    if rc != 0:
        receipt["git_error"] = ((err or "") + (out or "")).strip()[-800:]
    hrc, head_branch, _ = git_run(["rev-parse", "--abbrev-ref", "HEAD"])
    hsrc, head_sha, _ = git_run(["rev-parse", "HEAD"])
    receipt["head_branch"] = (head_branch or "").strip() if hrc == 0 else None
    receipt["head_sha"] = (head_sha or "").strip() if hsrc == 0 else None
    receipt["ok"] = rc == 0 and receipt["head_branch"] == b
    if not receipt["ok"]:
        receipt["error"] = (
            f"git switch failed: HEAD is {receipt['head_branch']!r}, not {b!r}"
            + (f" — git: {receipt['git_error']}" if receipt.get("git_error") else "")
        )
    if audit is not None:
        try:
            receipt["audit_recorded"] = bool(audit("ok" if receipt["ok"] else "failed", dict(receipt)))
        except Exception:  # noqa: BLE001 - result row is best-effort
            receipt["audit_recorded"] = False
        receipt["audit_degraded"] = not receipt["audit_recorded"]
        if receipt["audit_degraded"]:
            receipt["audit_warning"] = (
                "the post-switch audit row could NOT be written; the receipt above is the only "
                "record of this switch's outcome"
            )
    return receipt


# ── liveness authorities (B) ─────────────────────────────────────────────


def _same_root(a: Any, b: Path) -> bool:
    try:
        return bool(a) and Path(str(a)).resolve() == b.resolve()
    except Exception:  # noqa: BLE001
        return str(a) == str(b)


def _current_host_kind() -> str:
    """The CALLER's own host kind from its request identity (the channel
    ai_whoami reports: web_mcp for a WebMCP caller). '' when unstamped."""
    try:
        from .mcp_server_runtime_helpers import current_calling_host_kind

        kind = str(current_calling_host_kind() or "").strip()
        return "" if kind == "unknown" else kind
    except Exception:  # noqa: BLE001
        return ""


def _xaacp_actors_for_host(project_root: Path, host_session_id: str) -> list[dict[str, Any]]:
    """Every XAACP actor row registered under ``host_session_id`` (any session).

    Raises on store failure — the caller refuses (fail closed)."""
    from .conductor_comms import _connect

    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT actor_id, host_agent_id, session_id FROM xaacp_actors WHERE host_session_id = ?",
            (host_session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def project_liveness_blockers(
    project_root: Path,
    *,
    caller_host_session_id: str,
    caller_agent_id: str = "",
    caller_actor_id: str = "",
    caller_host_kind: str = "",
    session_id: str = "",
    same_host_actors: Callable[[], "list[dict[str, Any]]"] | None = None,
) -> list[str]:
    """Every OTHER live actor on this project, from the authorities that exist.

    Raises on any authority failure — the caller refuses (fail closed).

      1. connected_agents_audit — conductors bound to this project. A roster
         that is not 'ok' (unverifiable bindings) blocks: unknown is not idle.
         Any live conductor other than the caller blocks. A caller that is a
         SUBAGENT (host agent id set) is blocked by its own parent conductor,
         which shares the tree and the host session.
      2. session_lane_agents (cross_agent_coordination.connected_agents) —
         every live lane worker on the project, including the caller's own.
      3. HostConcurrencyStore — live spawned processes registered for this
         project root on this machine.
      4. XAACP actors sharing the caller's host session (actor-level self).
      5. receive leases for the session — any other actor with a waiting,
         working or overdue (= cannot tell) lease.
    """
    root = Path(project_root)
    caller = (caller_host_session_id or "").strip()
    blockers: list[str] = []

    from .agent_audit import connected_agents_audit

    audit = connected_agents_audit(root, caller_host_session_id=caller)
    # ONLY LIVE ACTORS BLOCK. `roster_status` is degraded/unavailable whenever a
    # HISTORICAL binding cannot be probed — on a long-lived project that is
    # permanent (11 such rows on ubermega), and treating it as a blocker made
    # switching impossible for everyone forever. An unverifiable row is not
    # evidence of a running actor; liveness is proven POSITIVELY elsewhere in
    # this function (live conductors, lane workers, host processes, receive
    # leases, host presence, mutation leases). A roster read that RAISES still
    # fails closed — that is a store failure, and the caller refuses.
    for a in audit.get("agents", []) or []:
        hsid = str(a.get("host_session_id") or "")
        if hsid != caller or not caller:
            blockers.append(
                f"conductor {hsid or '?'} (role={a.get('role')}, live via {a.get('live_source')})"
            )
        elif caller_agent_id:
            blockers.append(
                f"parent conductor {hsid} of calling subagent {caller_agent_id} shares this tree"
            )

    # ACTOR-LEVEL SELF (r0b send-back, blocker 3). A host_session_id is NOT an
    # actor: a conductor and its subagents (and re-bound generations) share one,
    # and liveness is graded per host session, so a same-hsid actor that is idle
    # between calls is indistinguishable from the caller at that level. Self is
    # proven only by ACTOR id. Every other XAACP actor registered under the
    # caller's host session blocks, and an unresolvable caller actor with any
    # such row blocks too (fail closed on ambiguity).
    if caller:
        me = str(caller_actor_id or "").strip()
        if same_host_actors is None:
            same_host_actors = lambda: _xaacp_actors_for_host(root, caller)  # noqa: E731
        rows = list(same_host_actors() or [])
        for r in rows:
            aid = str(r.get("actor_id") or "").strip()
            if me and aid == me:
                continue
            agent = str(r.get("host_agent_id") or "").strip()
            if caller_agent_id and agent == caller_agent_id:
                continue
            blockers.append(
                f"actor {aid or '?'} shares host session {caller} with the caller "
                f"(host_agent_id={agent or '-'}, session={r.get('session_id') or '-'}) and cannot be "
                "proven to be the caller"
                + ("" if me else " (the caller's own actor id is unresolved)")
            )

    from .cross_agent_coordination import connected_agents

    for w in connected_agents(root, live_only=True):
        blockers.append(
            f"lane worker {w.get('worker_id')} (lane={w.get('lane_id')}, state={w.get('state')})"
        )

    from .host_concurrency_store import HostConcurrencyStore

    for p in HostConcurrencyStore().list_live():
        if _same_root(p.get("project_root"), root):
            blockers.append(
                f"live process {p.get('kind')} {p.get('worker_key')} (pid={p.get('pid')})"
            )

    # NATIVE-HOST ATTESTATION, PROVEN NOT ASSUMED (r0b 3e084f64 + the follow-up
    # on unproven hosts). A host whose adapter has no matched native-tool
    # pre/post lease channel can have an edit already running that no lease
    # represents, and an unknown host is not an idle one. So the rule is
    # INVERTED: every live actor on this tree — INCLUDING THE CALLER — must
    # RESOLVE to an attested host kind, and an unresolved/empty kind blocks.
    # Evidence: the roster's own host_kind, else the presence row the actor's
    # adapter wrote before letting a native tool run.
    from .branch_transition_barrier import (
        ATTESTED_NATIVE_LIFECYCLE_HOSTS,
        canonical_host_kind,
        caller_is_gate_only_surface,
        host_kind_by_session,
        is_attested_host,
        native_surface_sessions,
        server_mediated_sessions,
        unattested_hosts,
    )

    _by_session = host_kind_by_session(root)
    # PROVENANCE, not spelling: sessions whose execution we have seen run inside
    # this process through the gate's dispatch. The gate mints a per-client kind
    # (`webmcp_<client id>`, see outer_gate_executor.resolve_web_host_kind), so
    # no name set could recognise them — but their calls are leased here.
    # STRUCTURALLY gate-only actors only (transport-created web actors), and
    # never a session that also shows direct evidence of an unattested native
    # surface — that evidence OUTRANKS "this session once used the gate".
    _gate_only = server_mediated_sessions(root)
    _native_surface = native_surface_sessions(root)
    _caller_mediated = caller_is_gate_only_surface()
    _seen_sessions: set[str] = set()
    # LIVE rows only (audit["agents"]); `unverifiable` rows are historical
    # bindings, not running actors — see the note above.
    for a in list(audit.get("agents", []) or []):
        hsid = str(a.get("host_session_id") or "")
        _seen_sessions.add(hsid)
        kind = canonical_host_kind(str(a.get("host_kind") or "")) or _by_session.get(hsid, "")
        if hsid not in _native_surface and (
            hsid in _gate_only or (hsid == caller and _caller_mediated)
        ):
            continue  # gate-only surface: it has NO native execution channel
        if not kind and hsid == caller:
            # The CALLER's own kind comes from its own request identity — the
            # same channel ai_whoami reports (web_mcp for a WebMCP caller) —
            # before anything falls back to unknown.
            kind = canonical_host_kind(caller_host_kind or _current_host_kind())
        if not is_attested_host(kind):
            blockers.append(
                f"actor {hsid or '?'} runs on host {kind or 'UNRESOLVED'}, which has no attested "
                "native-tool lease lifecycle (attested: "
                + ", ".join(sorted(ATTESTED_NATIVE_LIFECYCLE_HOSTS))
                + "; or proven server-mediated execution), so an already-running "
                "native edit cannot be excluded"
            )
    for h in unattested_hosts(root):
        # NOT suppressed by _seen_sessions when the row is direct evidence of a
        # native surface: r0b's ordering bug — a session cleared as gate-only
        # must not silence the very row proving it also ran a native tool.
        if (
            str(h.get("host_session_id") or "") in _seen_sessions
            and str(h.get("host_session_id") or "") not in _native_surface
        ):
            continue
        blockers.append(
            f"host {h.get('host_kind')} (session {h.get('host_session_id') or '-'}) is attached to "
            "this work tree and has no attested native-tool lease lifecycle"
        )

    # (No in-flight tool counter here: r0b 1b60a1275 hole 2 showed it was partial
    # telemetry -- ai_git and the edit tools were never counted. In-flight
    # MUTATIONS are excluded structurally instead: the barrier drains the shared
    # project mutation leases every governed write/subprocess holds.)

    if session_id:
        from .conductor_comms import xaacp_resolve_caller_actor
        from .receive_lease_store import (
            STATE_OVERDUE,
            STATE_WAITING,
            STATE_WORKING,
            ReceiveLeaseStore,
        )

        me = str(caller_actor_id or "").strip() or xaacp_resolve_caller_actor(root)
        for lease in ReceiveLeaseStore().list_actors(root, session_id=session_id):
            if lease.get("actor_id") == me:
                continue
            if lease.get("state") in (STATE_WAITING, STATE_WORKING, STATE_OVERDUE):
                blockers.append(
                    f"actor {lease.get('actor_id')} holds a {lease.get('state')} receive lease"
                )
    return blockers
