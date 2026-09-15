"""BacklogSyncSitter — automatic, continuous backlog replication.

Operator directive (2026-07-20): backlog must sync AUTOMATICALLY — not on
command, not on `sync`, not on deploy. This is the background service that makes
the git-backed event log (``.MEMORY/sync/events/backlog/``) continuously
bidirectional between the local host session and the VPS gate:

  * PUSH: debounced after each backlog mutation — commit the new append-only
    event files and ``git push`` (throttled; a burst batches into one push).
  * PULL: on a poll interval — ``git fetch``+merge the events dir, ADOPT the
    events that demonstrably arrived over the authenticated remote (see the
    provenance rule below), then ``hydrate_from_events`` (which RE-DERIVES the
    convergent display_id, so #N converges across stores), then run the
    LWW-collision detector and emit a ``backlog_lww_superseded`` audit for each
    same-field lost update (the loser_value is preserved → recoverable, never
    silent).

  * PROVENANCE (#376): auto-sync is the one path allowed to grant authority to an
    event it did not itself emit, so it must PROVE where that event came from. An
    event is adopted only when its file is present in the upstream remote-tracking
    tree; one first seen without that provenance is refused permanently. The file
    is never deleted — it stays quarantined with a clear status and remains
    adoptable through the explicit operator recovery path.

Doctrine borrowed from project_index_sitter + the a7b92940 no-op-flood lesson:
  * FAIL-OPEN: a sync cycle NEVER blocks or fails a backlog write; every git op
    is best-effort with the mutation already durable locally.
  * NO-OP SILENT: a pull/push that changed nothing emits NO audit event.
  * append-only + idempotent events → git merges are conflict-free (distinct
    filenames per event_id); the derived sqlite is never synced.
  * DEFER TO THE DEPLOY (#600) — now BELT-AND-BRACES ONLY (#612): a deploy used
    to read the LIVE branch when it pushed, so a sitter commit mid-deploy broke
    it. Since #612 the deploy PINS its target sha and an origin tip that moved
    past the pin is healthy, so this deferral is no longer load-bearing. It is
    KEPT deliberately (a safety net is not removed in the same change that
    rewrites the deploy gate) and costs at most one poll interval. Every cycle
    asks ``deploy_edit_window.head_freeze_owner`` and suspends its HEAD-moving
    git ops while a deploy owns the freeze. DEFERRED, NEVER DROPPED — the event
    files stay on disk and the next cycle after release commits all of them.
    Retire-by plan: see the HEAD-freeze block in deploy_edit_window.py.

Every git seam is injectable (``git_runner``) so the reconcile logic is unit
tested without touching a real remote.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

_LWW_EVENT = "backlog_lww_superseded"
_SYNC_EVENT = "backlog_autosync"


_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _default_git(project_root: Path, args: list[str], *, timeout: float = 20.0) -> tuple[int, str, str]:
    # #345: routed through audited_run so this git spawn lands a process-audit
    # ledger row (coverage-true-by-construction spawn seal). The run= lambda IS
    # the registered direct-run AST callsite (LEGACY_SUBPROCESS_FINGERPRINTS:
    # backlog_sync_sitter.py/_default_git); kwargs pass through byte-identically.
    from .shell_egress_service import audited_run

    proc = audited_run(
        ["git", "-C", str(project_root), *args],
        fingerprint=("backlog_sync_sitter.py", "_default_git", "subprocess.run"),
        reason="backlog-autosync git on the event-log dir — fixed subcommands, no shell, no agent input, fail-open",
        run=lambda *a, **kw: subprocess.run(*a, **kw),  # noqa: S603
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_WIN_NO_WINDOW,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _collision_key(c: dict) -> str:
    return f"{c['entity_id']}|{c['field']}|{c['loser_hlc']}|{c['loser_actor']}|{c['winner_hlc']}"


def _already_audited(hub: Any, project_root: Path, key: str) -> bool:
    """Persistent dedup: a collision already surfaced must not re-emit every
    poll. Best-effort — on any read error, assume NOT audited (a duplicate
    audit is far better than a swallowed lost-update)."""
    try:
        hub.execution.init_db(project_root)
        with hub.execution.connect(project_root) as conn:
            row = conn.execute(
                "SELECT 1 FROM execution_events WHERE event_kind = ? AND target_entity = ? LIMIT 1",
                (_LWW_EVENT, key),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def emit_lww_audits(project_root: Path, hub: Any, collisions: list[dict]) -> int:
    """Emit one ``backlog_lww_superseded`` audit per NOT-yet-seen collision.
    Returns the count emitted. Fail-open per collision."""
    emitted = 0
    for c in collisions:
        key = _collision_key(c)
        if _already_audited(hub, project_root, key):
            continue
        try:
            hub.execution.record_event(
                project_root,
                event_kind=_LWW_EVENT,
                source_kind="backlog_sync_sitter",
                capability_name="backlog",
                action_kind="lww_superseded",
                target_entity=key,
                status="superseded",
                principal_type="system",
                payload=dict(c),
            )
            emitted += 1
        except Exception:
            continue
    return emitted


def _event_file_name(event_id: str) -> str:
    """The on-disk filename GitEventTransport gives an event id. Provenance is
    matched on FILENAME because that is what a git tree listing reports."""
    from .sync_store import GitEventTransport

    return GitEventTransport._safe_name(str(event_id)) + ".json"


def _remote_event_file_names(project_root: Path, git, stream: str = "backlog") -> set[str]:
    """Event filenames present in the UPSTREAM remote-tracking tree.

    This is the authenticated-remote provenance check (#376). ``@{u}`` is the
    remote-tracking ref, which only advances through fetch/pull from the
    configured remote — so a file listed here demonstrably arrived over that
    transport, while a file merely written into the working tree does not appear.

    FAILS CLOSED on purpose. No upstream configured, a detached HEAD, or any git
    error yields an EMPTY set, so nothing is adopted this cycle. That is the safe
    direction and it loses nothing: the event files remain on disk and are adopted
    by a later cycle once upstream resolves, or deliberately via operator recovery.
    """
    rel = f".MEMORY/sync/events/{stream}"
    try:
        code, out, _err = git(project_root, ["ls-tree", "-r", "--name-only", "@{u}", "--", rel])
    except Exception:  # noqa: BLE001 — a provenance probe must never break a cycle
        return set()
    if code != 0:
        return set()
    names: set[str] = set()
    for line in (out or "").splitlines():
        entry = line.strip().strip('"')
        if entry.endswith(".json"):
            names.add(entry.rsplit("/", 1)[-1])
    return names


def _head_freeze_owner(project_root: Path) -> str:
    """Non-empty when a crown deploy owns the HEAD freeze (#600).

    Fail-OPEN, matching the rest of this module: if the reader itself cannot be
    consulted the cycle proceeds as before. The reader is pure marker reads and
    is internally fail-quiet, so this only catches an import-level break.
    """
    try:
        from .deploy_edit_window import head_freeze_owner

        return head_freeze_owner(project_root) or ""
    except Exception:  # noqa: BLE001 — the guard must never break a sync cycle
        return ""


def sync_once(
    project_root: Path,
    hub: Any,
    *,
    trigger: str = "poll",
    git_runner: Callable[[Path, list[str]], tuple[int, str, str]] | None = None,
    do_pull: bool = True,
    do_push: bool = True,
) -> dict:
    """One reconcile cycle: pull → hydrate (re-derive convergent #N) → detect &
    audit LWW collisions → push. FAIL-OPEN and NO-OP-SILENT (emits the
    ``backlog_autosync`` cycle event only when something actually changed).
    Returns a summary dict; never raises."""
    from . import project_backlog_store, sync_store

    git = git_runner or _default_git
    pulled = pushed = False
    lww_emitted = 0
    errors: list[str] = []

    # #600 DEFER, NEVER DROP — retained as BELT-AND-BRACES since #612. While a
    # crown deploy owns the HEAD freeze, every git op that MOVES HEAD is
    # suspended: commit+push (the measured non-fast-forward that discarded a
    # whole gate cycle) and pull alike. The local hydrate below still runs: it
    # only reads the event files and writes the derived sqlite, so it never
    # touches HEAD.
    #
    # NOT LOAD-BEARING ANY MORE (#612): the deploy pins its target sha, pushes
    # exactly that refspec, and accepts an origin tip that has advanced past the
    # pin — so none of these ops can break a running deploy. Kept because a
    # safety net does not get removed in the same change that rewrites the gate,
    # and because deferring costs one poll interval and loses nothing. The
    # retire-by plan lives with the reader (deploy_edit_window.py).
    #
    # No event can be lost by deferring, BY CONSTRUCTION: the push step is
    # driven by `git status --porcelain` over the event dir, not by an
    # in-memory queue. The append-only files stay on disk, and the first cycle
    # after the freeze lifts sees every one of them and commits the lot. The
    # poll loop guarantees such a cycle arrives (poll_seconds, default 30s)
    # whether or not another mutation ever follows.
    deferred_by = _head_freeze_owner(project_root)
    if deferred_by:
        do_pull = do_push = False

    if do_pull:
        try:
            code, out, err = git(project_root, ["pull", "--no-edit", "--no-rebase"])
            if code == 0 and "up to date" not in out.lower():
                pulled = True
            elif code != 0 and err.strip():
                errors.append(f"pull:{err.strip()[:120]}")
        except Exception as exc:  # noqa: BLE001 — fail-open
            errors.append(f"pull:{exc!r}")

    # Re-derive from the (possibly updated) event log, then surface lost updates.
    try:
        events = sync_store.GitEventTransport(project_root).read("backlog")
        _authoritative, incoming = sync_store.split_by_authority(
            project_root, "backlog", events
        )
        # PROVENANCE-SCOPED ADOPTION (#376, 2026-07-29). The 2026-07-20 version
        # adopted EVERY unreceipted file on disk, justified by "the trust boundary
        # is the authenticated git remote". That boundary was named but never
        # CHECKED, so a file merely written into the working tree was adopted on
        # the next poll — the whole ungoverned-write hole, reopened past the
        # receipt model. The boundary is now enforced instead of asserted:
        #
        #   ADOPT   an event whose file is present in the UPSTREAM remote-tracking
        #           tree — it demonstrably travelled the authenticated remote, so
        #           legitimate peer edits still apply and display_id still converges.
        #   REFUSE  an event first seen with no such provenance, and remember that
        #           refusal STICKILY (sync_store.record_unverified). Sticky matters
        #           because the push below commits this same dir: without it, a file
        #           we refused would return "remote-present" next cycle and we would
        #           launder our own forgery.
        #
        # Refusal is not deletion. The file stays on disk, the fold quarantines it
        # with a clear status, and the explicit operator recovery path
        # (rebuild_from_events(adopt_incoming=True)) can still adopt it deliberately.
        if incoming:
            remote_names = _remote_event_file_names(project_root, git, "backlog")
            already_refused = sync_store.unverified_event_ids(project_root, "backlog")
            fresh = [e for e in incoming if e.event_id not in already_refused]
            adoptable = {
                e.event_id for e in fresh if _event_file_name(e.event_id) in remote_names
            }
            unproven = [e for e in fresh if e.event_id not in adoptable]
            if unproven:
                sync_store.record_unverified(project_root, "backlog", unproven)
            if adoptable:
                sync_store.adopt_event_ids(project_root, "backlog", adoptable)
        project_backlog_store.hydrate_from_events(project_root)
        # Collisions over the FULL event set: a local-vs-remote same-field
        # overwrite is a lost update to surface whether or not it was just adopted.
        collisions = sync_store.detect_lww_field_collisions(events)
        lww_emitted = emit_lww_audits(project_root, hub, collisions)
    except Exception as exc:  # noqa: BLE001 — fail-open
        errors.append(f"hydrate:{exc!r}")

    if do_push:
        try:
            rel = ".MEMORY/sync/events/backlog"
            code, out, _err = git(project_root, ["status", "--porcelain", "--", rel])
            if code == 0 and out.strip():
                git(project_root, ["add", "--", rel])
                cm, _o, _ce = git(
                    project_root,
                    ["commit", "-m", "chore(backlog): autosync event log", "--", rel],
                )
                if cm == 0:
                    pc, _po, pe = git(project_root, ["push"])
                    if pc == 0:
                        pushed = True
                    elif pe.strip():
                        errors.append(f"push:{pe.strip()[:120]}")
        except Exception as exc:  # noqa: BLE001 — fail-open
            errors.append(f"push:{exc!r}")

    changed = pulled or pushed or lww_emitted > 0
    # NO-OP SILENT: only audit the cycle when it did real work (flood lesson).
    if changed:
        try:
            hub.execution.record_event(
                project_root,
                event_kind=_SYNC_EVENT,
                source_kind="backlog_sync_sitter",
                capability_name="backlog",
                action_kind="sync",
                target_entity=".MEMORY/sync/events/backlog",
                status="synced",
                principal_type="system",
                payload={
                    "trigger": trigger,
                    "pulled": pulled,
                    "pushed": pushed,
                    "lww_superseded": lww_emitted,
                    "errors": errors,
                },
            )
        except Exception:
            pass
    return {
        "ok": not errors,
        "pulled": pulled,
        "pushed": pushed,
        "lww_superseded": lww_emitted,
        "changed": changed,
        "errors": errors,
        "trigger": trigger,
        # #600: "" normally; the freeze owner's description while a deploy holds
        # HEAD. NOT an error and NOT audited — a deferred cycle did no work, so
        # the no-op-silent rule applies (a 20min deploy would otherwise emit an
        # event every poll: exactly the a7b92940 flood). This field is the
        # detectable trace, surfaced through the sitter's status().
        "deferred_by": deferred_by,
        # #442: submit the outbox to the AUTHORITATIVE hub + pull server-ordered
        # events. Runs AFTER the git cycle so the outbox is already complete.
        "vps_hub": _vps_hub_reconcile(project_root),
        # P0 of the server-authoritative backlog: refresh the read-only snapshot
        # of the codenexus-held backlog and report the local-vs-server delta.
        # NON-DESTRUCTIVE — never touches project_backlog (still the writer of
        # record this phase). Unbound projects are a no-op.
        "hub_backlog": _hub_backlog_snapshot(project_root),
    }


def _hub_backlog_snapshot(project_root: Path) -> dict:
    """Delegates to the OUTBOX SERVICE — this is no longer the drain's home.

    P1 step 1 (operator ruling 2026-08-30, "Split sitter first: outbox drain
    becomes independent service; then kill Git replication half"). The body that
    lived here now lives in `backlog_outbox_service.drain_once`, because this
    sitter also owns the GIT replication half and that half is scheduled for
    deletion. Deleting the sitter with the drain still inside it would remove
    the only thing that flushes the outbox, silently — writes would keep landing
    locally and simply never reach the server.

    KEPT AS A DELEGATE rather than removed from the cycle: the sitter is still
    the thing running on this box today, so the drain must keep happening on its
    poll. One implementation, two callers — copying it into the new module and
    leaving this one behind is the twin-drift this codebase keeps paying for.

    Semantics are unchanged by the move: still P0, still read-only toward
    `project_backlog`, still a no-op for unbound projects, still never raises.
    """
    from . import backlog_outbox_service as _outbox

    return _outbox.drain_once(project_root)


def _clean_start_hlc(project_root: Path) -> str:
    """START CLEAN (operator ruling 2026-07-21).

    The pre-existing outbox and the 37,567 historical quarantined events are
    NOT adopted. On the FIRST enabled cycle we watermark the highest local HLC
    and persist it; only events STRICTLY NEWER are ever submitted to the hub.
    Convergence therefore starts at enablement and the stale churn stays put —
    it is never deleted (still inspectable), just never replayed.

    Idempotent: once written the watermark never moves, so enabling/disabling
    the flag cannot silently widen what gets submitted. Best-effort — an
    unreadable marker returns "" (submit-all), never an exception.
    """
    marker = Path(project_root) / ".MEMORY" / "sync" / ".vps_clean_start"
    try:
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip()
        from .sync_store import STREAMS, GitEventTransport

        outbox = GitEventTransport(Path(project_root))
        hi = ""
        for stream in STREAMS:
            for ev in outbox.read(stream):
                if ev.hlc > hi:
                    hi = ev.hlc
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(hi, encoding="utf-8")
        return hi
    except Exception:  # noqa: BLE001 — never break a sync cycle on the marker
        return ""


#: WHERE a sync bearer came from. Only a GATE-issued one is filed back through
#: `record_gate_answer` — an env/config override is a different secret and its
#: refusal says nothing about the operator's cached credential.
TOKEN_SOURCE_ENV = "env"
TOKEN_SOURCE_CONFIG = "config"
TOKEN_SOURCE_GATE = "gate_credential"


def _sync_hub_token_with_source(
    project_root: Path, observe: dict | None = None
) -> tuple[str, str]:
    """(token, source) the background sync may present to the hub, or ("", "").

    THE LADDER, senior first:

    1. env ``AIDOCS_OPERATOR_TOKEN`` — a provisioned secret is an explicit act.
    2. config ``sync.vps_hub_token`` — the override for the odd host
       (operator ruling 2026-07-21); expected empty on a developer box.
    3. the shared cache's GATE-ISSUED credential, and only when
       :func:`cached_gate_credential` says ``GATE_CRED_OK``.

    Step 3 is the #1002 gap-2 fix: before it, a signed-in developer box read
    as `no_token_or_project_id` on every cycle. It is also stricter than the
    foreground rule on purpose — an unlabelled legacy row (UNKNOWN_VINTAGE)
    is offered to a foreground caller (#627) but contributes NOTHING here,
    because unproven is exactly what a background path must not SPEND: on
    this operator's machine that row held a local token, and presenting it
    unprompted is what earned three bans in one day (#992). Expired,
    local-only and latched rows contribute nothing for the same reason.

    WHO-layer only (law cc6c4ac686ee): this proves the operator, never a
    session, seat or lane — those the hub derives from its own registries.
    """
    import os

    env = str(os.environ.get("AIDOCS_OPERATOR_TOKEN", "") or "").strip()
    if env:
        return env, TOKEN_SOURCE_ENV
    try:
        from .config import get_setting

        cfg = str(
            get_setting("sync.vps_hub_token", project_root=project_root, default="") or ""
        ).strip()
    except Exception:  # noqa: BLE001 — config is optional on this path
        cfg = ""
    if cfg:
        return cfg, TOKEN_SOURCE_CONFIG
    from .operator_token_resolution import GATE_CRED_OK

    # RENEW BEFORE PRESENTING (#1000). THIS is the path the operator's
    # complaint was actually about: a background reconcile with nobody at the
    # keyboard, going dark an hour after each sign-in and staying dark until a
    # human opened a browser. `ensure_gate_credential` returns the cached
    # credential untouched when it is live and its hourly permissions answer is
    # fresh — the ordinary 30-second cycle spends no request — and otherwise
    # exchanges the stored refresh credential for a new bearer, comparing the
    # scopes it comes back with against the ones the old bearer carried.
    #
    # One attempt per window, recorded on disk: a refused renewal here must
    # never become the retry loop that banned this machine (#992).
    from .gate_credential_renewal import ensure_gate_credential

    outcome = ensure_gate_credential(project_root=project_root)
    if observe is not None:
        # A PERMISSION CHANGE NOBODY SURFACES HAS NOT BEEN CHECKED (law
        # 183074ae; operator directive: "on refresh it needs to check if any
        # perms changed"). This is the surface an operator already reads when
        # they ask why sync is or is not converging, so the answer belongs
        # here — not only in a log line and a JSON field on disk.
        observe["gate_credential_renewed"] = bool(outcome.renewed)
        observe["gate_credential_renewable"] = bool(outcome.credential.renewable)
        if outcome.reason:
            observe["gate_renewal_reason"] = outcome.reason
        if outcome.scope_changed:
            observe["gate_permissions_changed"] = True
            observe["gate_permissions_granted"] = list(outcome.scope_added)
            observe["gate_permissions_revoked"] = list(outcome.scope_removed)
    cred = outcome.credential
    if cred.reason != GATE_CRED_OK:
        return "", ""
    return str(cred.token or ""), TOKEN_SOURCE_GATE


# ── a gate refusal of the SYNC bearer: named, filed, and not repeated ─────────
#
# MEASURED: step 3 above hands the cached Dashboard credential to
# `/sync/events`, which requires SCOPE_SYNC. A Dashboard token never carries
# it, so the gate answered 403 insufficient_scope on every 30s poll, the
# same token was re-presented every time, and the operator saw `VpsSyncError`.
#
# A scope or tenancy refusal is NOT a credential refusal (#992 must not
# latch on it), but it IS a stable fact about THIS token on THIS project —
# so it is remembered per (token fingerprint, project) and the token is not
# offered again until it changes. A 401 goes through `record_gate_answer`
# (latched, and `cached_gate_credential` withholds it from then on).
SKIP_TOKEN_LACKS_SYNC_SCOPE = "gate_token_lacks_sync_scope"
SKIP_TOKEN_TENANT_MISMATCH = "gate_token_tenant_mismatch"
SKIP_TOKEN_REJECTED = "gate_token_rejected"
SKIP_TOKEN_RESOURCE_MISMATCH = "gate_token_resource_mismatch"

SKIP_REMEDY: dict[str, str] = {
    SKIP_TOKEN_LACKS_SYNC_SCOPE: (
        "the gate accepted the credential but it carries no `sync` scope — a "
        "Dashboard sign-in token never does; provide a sync-scoped bearer "
        "(AIDOCS_OPERATOR_TOKEN or sync.vps_hub_token) or run the event-stream "
        "reconcile on the VPS. The credential is NOT revoked and was not latched; "
        "it will not be re-presented here until it changes"
    ),
    SKIP_TOKEN_TENANT_MISMATCH: (
        "the gate accepted the credential but this project is not inside the "
        "operator's tenancy — check which org/project this directory is "
        "connected to; the token will not be re-presented here until it changes"
    ),
    SKIP_TOKEN_RESOURCE_MISMATCH: (
        "the gate accepted the credential but it is bound to a different resource "
        "than this route — a gate that predates the desktop sync-route binding "
        "refuses it here; the credential is NOT revoked, was not latched, and "
        "stays valid for XAACP; it will not be re-presented here until it changes"
    ),
    SKIP_TOKEN_REJECTED: (
        "the authority refused the bearer — for a gate credential the #992 latch "
        "now withholds it; sign in to CODENEXUS again from the Dashboard"
    ),
}

_REFUSAL_BY_WORD: dict[str, str] = {
    "insufficient_scope": SKIP_TOKEN_LACKS_SYNC_SCOPE,
    "tenant_mismatch": SKIP_TOKEN_TENANT_MISMATCH,
    # A route-scoped refusal, not a credential one (2026-09-15): latching it
    # killed XAACP over /v1/mcp, where the same token is valid.
    "resource_mismatch": SKIP_TOKEN_RESOURCE_MISMATCH,
}

#: (token fingerprint, project_id) -> skip reason. Process-local on purpose:
#: a restart re-tries once, which is the cheapest correct recovery.
_SCOPE_REFUSALS: dict[tuple[str, str], str] = {}


def _token_fp(token: str) -> str:
    import hashlib

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]


def _file_sync_refusal(
    *, status: int, refusal: str, token: str, source: str, project_id: str
) -> str:
    """Name a 401/403 from `/sync/events`, file it where it belongs, and
    remember it so the same bearer is not re-presented. Returns the skip
    reason ("" when the status is not a refusal). Never raises."""
    code = int(status or 0)
    if code == 401:
        reason = SKIP_TOKEN_REJECTED
    elif code == 403:
        reason = _REFUSAL_BY_WORD.get(str(refusal or ""), SKIP_TOKEN_REJECTED)
    else:
        return ""
    if reason == SKIP_TOKEN_REJECTED and source == TOKEN_SOURCE_GATE:
        # A credential refusal of the OPERATOR'S credential: the #992 latch is
        # the memory — `cached_gate_credential` withholds it from now on.
        try:
            from .operator_token_resolution import record_gate_answer

            record_gate_answer(status=code)
        except Exception:  # noqa: BLE001 — filing must never break the cycle
            pass
    # Every refusal — scope, tenancy, or an override secret's 401 — is pinned
    # to the bearer that earned it, so no poll spends a request on it again.
    _SCOPE_REFUSALS[(_token_fp(token), str(project_id))] = reason
    return reason


def _vps_hub_reconcile(project_root: Path) -> dict:
    """#442 sanctioned build, client side: submit the git OUTBOX to the
    authoritative VPS hub for re-authorization, then pull server-ordered
    events. The server issues the receipts, so the UNTOUCHED receipted-only
    fold applies what it accepts — that is what makes local and WebMCP
    converge under ONE codenexus account.

    DEFAULT OFF (``sync.vps_hub_enabled``) and FAIL-OPEN: with the flag off, no
    token, or no reachable hub, this is a no-op and local behaviour is
    byte-identical to today (hard floor: local-first/offline — the VPS is a
    hub + backup, never a hard dependency). Never raises.

    A 401/403 FROM THE HUB IS NAMED, FILED AND NOT REPEATED (see
    `_file_sync_refusal`): the summary carries `status`, `refusal` (the
    gate's word), `refused` (the discriminated skip reason) and `remedy`;
    the next poll reports `skipped=<that reason>` without spending a request
    until the bearer changes.
    """
    out: dict = {"enabled": False}
    try:
        from .config import get_setting

        if not bool(
            get_setting("sync.vps_hub_enabled", project_root=project_root, default=False)
        ):
            return out
        out["enabled"] = True

        from .sync_vps import DEFAULT_BASE_URL, VpsApiTransport, reconcile_outbox

        base = str(
            get_setting("sync.vps_hub_url", project_root=project_root, default=DEFAULT_BASE_URL)
            or DEFAULT_BASE_URL
        )
        # DERIVED, not typed (2026-07-21): the project's own registration knows
        # its id. The config key is only an override for a host that must submit
        # somewhere else, so it is consulted first but expected to be empty.
        from .backlog_hub_client import binding as _hub_binding

        project_id = str(
            get_setting("sync.vps_hub_project_id", project_root=project_root, default="") or ""
        ) or _hub_binding(project_root)[1]
        if not project_id:
            out["skipped"] = "no_project_id"
            return out
        token, source = _sync_hub_token_with_source(project_root, observe=out)
        if not token:
            # Detectable, not silent: the operator sees WHY nothing converged —
            # and an unusable credential refuses HERE, before the socket, so
            # no request feeds the gate's 401 budget (#992).
            from .operator_token_resolution import cached_gate_credential

            out["skipped"] = f"no_gate_credential:{cached_gate_credential().reason}"
            return out
        if source == TOKEN_SOURCE_GATE:
            # #1000: the hourly authorization recheck is informational here.
            # A due recheck never withholds the token: this contact IS the
            # recheck, and `_file_sync_refusal` files the gate's answer.
            from .operator_token_resolution import cached_gate_credential

            out["authz_recheck_due"] = bool(cached_gate_credential().recheck_due)
        remembered = _SCOPE_REFUSALS.get((_token_fp(token), str(project_id)), "")
        if remembered:
            # This bearer already earned a refusal on this project.
            # Re-presenting it would spend a request to learn the same fact;
            # back off until the token changes.
            out["skipped"] = remembered
            out["remedy"] = SKIP_REMEDY.get(remembered, "")
            return out
        transport = VpsApiTransport(
            project_root, base_url=base, token=token, project_id=project_id
        )
        out.update(
            reconcile_outbox(
                project_root, transport, clean_start_hlc=_clean_start_hlc(project_root)
            )
        )
        refused = _file_sync_refusal(
            status=int(out.get("status") or 0),
            refusal=str(out.get("refusal") or ""),
            token=token,
            source=source,
            project_id=project_id,
        )
        if refused:
            out["refused"] = refused
            out["remedy"] = SKIP_REMEDY.get(refused, "")
    except Exception as exc:  # noqa: BLE001 — sync must never break a local write
        out["error"] = type(exc).__name__
    return out


#: Where the set of project roots this machine has ever synced is remembered,
#: so a RESTARTED process can start their sitters at boot without waiting for
#: somebody to call ai_session(connect) (#1063). Overridable for tests.
ROOTS_REGISTRY_ENV = "AIDOCS_BACKLOG_SYNC_ROOTS_FILE"

#: The ceiling on the drain backoff. A refused credential is retried at most
#: this often, so a dead sign-in costs one no-socket check per 15 minutes,
#: never a hot loop -- and the queue is untouched either way.
DEFAULT_MAX_BACKOFF_SECONDS = 900

#: The named reason a GATE drainer holds its queue (#1063, r0a item 4). Queue
#: rows carry project_id but NO author, and at gate boot there is no
#: authenticated principal -- `_resolve_uid` would attribute them to the
#: daemon's local operator. So they are HELD, visibly, never drained under
#: the wrong principal.
HELD_UNATTRIBUTED = "held_unattributed_gate_writes"

#: Audit event emitted when a recorded credential refusal is superseded by
#: live proof (a newer credential generation, or a later accepted gate call).
REFUSAL_SUPERSEDED_EVENT = "backlog_sync_refusal_superseded"

_CREDENTIAL_REASONS = frozenset({"gate_credential_unusable", "auth_refused", "hub_unconfigured"})


def _count_all_pending(project_root: Path) -> int | None:
    """Pending intents in this root's queue across EVERY project_id. Read-only."""
    try:
        from . import backlog_write_queue as _q
        from ._sqlite_connect import connect as _connect

        db = _q._db_path(Path(project_root))
        if not db.is_file():
            return 0
        with _connect(str(db), row_factory=False) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='backlog_write_queue'"
            ).fetchone()
            if not row or not row[0]:
                return 0
            return int(conn.execute(
                "SELECT COUNT(*) FROM backlog_write_queue WHERE state = 'pending'"
            ).fetchone()[0])
    except Exception:  # noqa: BLE001 -- unknown is None, never 0
        return None


def _drain_error(result: dict) -> str | None:
    """The drain verdict carried by one cycle: "" ok, a reason when refused,
    None when the cycle made no drain attempt at all (unbound / no queue)."""
    hb = result.get("hub_backlog") if isinstance(result, dict) else None
    drain = hb.get("drain") if isinstance(hb, dict) else None
    if not isinstance(drain, dict) or not drain.get("bound") or not drain.get("submitted"):
        err = hb.get("error") if isinstance(hb, dict) else None
        return str(err) if err else None
    return str(drain.get("error") or "")


class BacklogSyncSitter:
    """Per-project background replicator: poll-pull + debounced push + drain.

    ``drain_only`` runs ONLY the outbox drain (backlog_outbox_service.drain_once)
    on the poll -- the shape the GATE boots (#1063 ruling 6), where starting the
    git replication half against the gate checkout is not this change's call.
    """

    def __init__(
        self,
        project_root: Path,
        hub: Any,
        *,
        poll_seconds: int = 30,
        push_debounce_ms: int = 1500,
        drain_only: bool = False,
        max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
        hold_unattributed: bool = False,
    ) -> None:
        self.project_root = Path(project_root)
        self.hub = hub
        self.poll_seconds = max(5, int(poll_seconds))
        self.push_debounce_ms = max(200, int(push_debounce_ms))
        self.drain_only = bool(drain_only)
        self.max_backoff_seconds = max(self.poll_seconds, int(max_backoff_seconds))
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._push_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._started = False
        self._last_result: dict = {}
        self._consecutive_failures = 0
        self._last_attempt_reason = ""
        self.hold_unattributed = bool(hold_unattributed)
        self._held_count: int | None = None
        self._refused: dict = {}
        self._refusal_superseded_at = ""

    def start(self) -> bool:
        if self._started:
            return True
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name=f"backlog-sync-poll:{self.project_root.name}",
        )
        self._poll_thread.start()
        self._started = True
        return True

    def stop(self) -> None:
        self._stop.set()
        self._started = False
        with self._lock:
            if self._push_timer is not None:
                try:
                    self._push_timer.cancel()
                except Exception:
                    pass
                self._push_timer = None

    def _run_cycle(self) -> dict:
        if self.hold_unattributed:
            # NEVER drain_queue here: it would resolve the author from the
            # daemon's local login. Count, name, hold.
            self._held_count = _count_all_pending(self.project_root)
            return {"trigger": "poll", "mode": "held", "held": self._held_count, "reason": HELD_UNATTRIBUTED}
        if self.drain_only:
            from . import backlog_outbox_service as _outbox

            return {"trigger": "poll", "mode": "drain_only", "hub_backlog": _outbox.drain_once(self.project_root)}
        return sync_once(self.project_root, self.hub, trigger="poll")

    def _record_cycle(self, result: dict) -> None:
        """Fold one cycle's drain verdict into the backoff state (#1063 ruling 2).
        A refusal grows the wait; a clean drain resets it; a cycle that did not
        attempt a drain leaves the state alone."""
        self._last_result = result if isinstance(result, dict) else {}
        err = _drain_error(self._last_result)
        if err is None:
            return
        self._last_attempt_reason = err
        if err:
            self._consecutive_failures += 1
            if err in _CREDENTIAL_REASONS and not self._refused:
                from datetime import UTC, datetime

                from . import backlog_hub_client as _hub

                self._refused = {
                    "reason": err,
                    "credential_fingerprint": _hub.credential_identity()["fingerprint"],
                    "last_attempt_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
        else:
            self._consecutive_failures = 0
            self._refused = {}

    def _supersede_refusal_if_proven(self) -> bool:
        """r0a rule: a credential refusal clears -- with audit -- when the shared
        door holds a NEWER credential generation, or when an authenticated gate
        call over it succeeded after the refusal. Nothing is re-presented to
        find out; this reads the cache only."""
        if not self._refused:
            return False
        from . import backlog_hub_client as _hub

        if not _hub.refusal_superseded(self._refused):
            return False
        from datetime import UTC, datetime

        self._refusal_superseded_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        superseded, self._refused = self._refused, {}
        self._consecutive_failures = 0
        if self.hub is not None:
            try:
                self.hub.execution.record_event(
                    self.project_root,
                    event_kind=REFUSAL_SUPERSEDED_EVENT,
                    source_kind="backlog_sync_sitter",
                    capability_name="backlog",
                    action_kind="sync",
                    target_entity="backlog_drain",
                    status="superseded",
                    principal_type="system",
                    payload={
                        "refused_reason": superseded.get("reason"),
                        "refused_at": superseded.get("last_attempt_at"),
                        "refused_fingerprint": superseded.get("credential_fingerprint"),
                        "current_fingerprint": _hub.credential_identity()["fingerprint"],
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return True

    def _next_wait(self) -> int:
        """Poll interval, doubled per consecutive refusal, BOUNDED -- and reset
        at once when live proof supersedes a credential refusal."""
        self._supersede_refusal_if_proven()
        n = self._consecutive_failures
        if n <= 0:
            return self.poll_seconds
        return int(min(self.poll_seconds * (2 ** min(n, 16)), self.max_backoff_seconds))

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._next_wait()):
            try:
                self._record_cycle(self._run_cycle())
            except Exception as exc:  # noqa: BLE001 -- the loop must outlive a bad cycle
                self._last_result = {"ok": False, "errors": [f"cycle:{type(exc).__name__}"]}

    def notify_mutation(self) -> None:
        """Debounced push trigger — call after a backlog write. A burst of
        mutations batches into ONE push. No-op for a drain-only sitter."""
        if self.drain_only:
            return
        with self._lock:
            if self._push_timer is None or not self._push_timer.is_alive():
                self._push_timer = threading.Timer(
                    self.push_debounce_ms / 1000.0,
                    lambda: sync_once(
                        self.project_root, self.hub, trigger="mutation", do_pull=False
                    ),
                )
                self._push_timer.daemon = True
                self._push_timer.start()

    def status(self) -> dict:
        return {
            "running": self._started,
            "mode": "drain_only" if self.drain_only else "full",
            "poll_seconds": self.poll_seconds,
            "push_debounce_ms": self.push_debounce_ms,
            "last_result": self._last_result,
            "consecutive_failures": self._consecutive_failures,
            "backoff_seconds": self._next_wait(),
            "last_attempt_reason": self._last_attempt_reason,
            "refusal_superseded_at": self._refusal_superseded_at,
            "held_reason": HELD_UNATTRIBUTED if self.hold_unattributed else "",
            "held_count": self._held_count if self.hold_unattributed else None,
        }


# ── registry + config ────────────────────────────────────────────────────
_INSTANCES: dict[str, BacklogSyncSitter] = {}
_INSTANCES_LOCK = threading.Lock()
_REGISTRY_LOCK = threading.Lock()


def _key(project_root: Path) -> str:
    try:
        return str(Path(project_root).resolve()).replace("\\", "/")
    except Exception:
        return str(project_root).replace("\\", "/")


def _config(project_root: Path) -> tuple[bool, int, int]:
    try:
        from .config import get_setting

        enabled = bool(
            get_setting("observability.backlog_autosync", project_root=project_root, default=True)
        )
        poll = int(
            get_setting(
                "observability.backlog_autosync_poll_seconds",
                project_root=project_root,
                default=30,
            )
            or 30
        )
        debounce = int(
            get_setting(
                "observability.backlog_autosync_push_debounce_ms",
                project_root=project_root,
                default=1500,
            )
            or 1500
        )
    except Exception:
        enabled, poll, debounce = True, 30, 1500
    return enabled, poll, debounce


def _roots_registry_path() -> Path:
    import os

    override = os.environ.get(ROOTS_REGISTRY_ENV, "").strip()
    if override:
        return Path(override)
    from .aidocs_service import daemon_dir

    return daemon_dir() / "backlog_sync_roots.json"


def _remembered_roots() -> list[str]:
    try:
        import json

        data = json.loads(_roots_registry_path().read_text(encoding="utf-8"))
        return [str(r) for r in data if isinstance(r, str)] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 -- no registry is an empty one
        return []


def _remember_root(project_root: Path) -> None:
    """Durably note that this root syncs, so the NEXT process boots it.
    Best-effort: failing to remember must never fail the ensure."""
    try:
        import json

        key = _key(project_root)
        with _REGISTRY_LOCK:
            roots = _remembered_roots()
            if key in roots:
                return
            roots.append(key)
            path = _roots_registry_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(roots), indent=2), encoding="utf-8")
            tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def ensure_backlog_sync(
    project_root: Path,
    hub: Any,
    *,
    drain_only: bool = False,
    hold_unattributed: bool = False,
) -> bool:
    """Start the sitter for ``project_root`` when enabled. IDEMPOTENT: exactly
    one sitter per project per process -- a second ensure (connect, boot, or a
    drain-only boot) returns the running one. Since #1063 this is called at
    PROCESS BOOT (`boot_backlog_sync`) and kept on connect as an ensure."""
    enabled, poll, debounce = _config(project_root)
    if not enabled:
        stop_backlog_sync(project_root)
        return False
    key = _key(project_root)
    with _INSTANCES_LOCK:
        existing = _INSTANCES.get(key)
        if existing is not None and existing._started:
            started = True
        else:
            sitter = BacklogSyncSitter(
                project_root,
                hub,
                poll_seconds=poll,
                push_debounce_ms=debounce,
                drain_only=drain_only,
                hold_unattributed=hold_unattributed,
            )
            if hold_unattributed:
                sitter._held_count = _count_all_pending(project_root)
            started = sitter.start()
            if started:
                _INSTANCES[key] = sitter
    if started and not drain_only:
        _remember_root(project_root)
    return started


def _seed_roots() -> list[Path]:
    """Roots a FIRST boot can know without any registry (r0a item 3): the
    project the signed-in operator's cache row names, and the env/discovered
    project root. Only the paths are read; never the credential."""
    seeds: list[Path] = []
    try:
        from .operator_token_resolution import read_cache

        pr = str((read_cache() or {}).get("project_root") or "").strip()
        if pr:
            seeds.append(Path(pr))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .mcp_server_runtime_helpers import _env_project_root

        env_root = _env_project_root()
        if env_root is not None:
            seeds.append(Path(env_root))
    except Exception:  # noqa: BLE001
        pass
    return seeds


def boot_backlog_sync(
    hub: Any,
    *,
    roots: list[Path] | None = None,
    drain_only: bool = False,
    hold_unattributed: bool = False,
) -> dict[str, bool]:
    """PROCESS-BOOT start (#1063 ruling 1). Ensures a sitter for every named
    root, or -- with none named -- every root a previous process remembered
    PLUS every seed root (the signed-in operator's project, the env/discovered
    root) that has PENDING queued writes, so the FIRST boot after deploy needs
    no connect. A root that no longer exists is skipped. Never raises."""
    out: dict[str, bool] = {}
    if roots is not None:
        candidates = [Path(r) for r in roots]
    else:
        candidates = [Path(r) for r in _remembered_roots()]
        known = {_key(c) for c in candidates}
        for seed in _seed_roots():
            if _key(seed) in known:
                continue
            if _count_all_pending(seed):
                candidates.append(seed)
                known.add(_key(seed))
    for root in candidates:
        try:
            if not (root / ".MEMORY").is_dir():
                continue
            out[_key(root)] = bool(
                ensure_backlog_sync(root, hub, drain_only=drain_only, hold_unattributed=hold_unattributed)
            )
        except Exception:  # noqa: BLE001
            out[_key(root)] = False
    return out


def notify_backlog_mutation(project_root: Path) -> None:
    """Debounced-push hook the store calls after a backlog write. No-op when no
    sitter is running (e.g. autosync disabled)."""
    with _INSTANCES_LOCK:
        sitter = _INSTANCES.get(_key(project_root))
    if sitter is not None:
        sitter.notify_mutation()


def stop_backlog_sync(project_root: Path) -> None:
    key = _key(project_root)
    with _INSTANCES_LOCK:
        sitter = _INSTANCES.pop(key, None)
    if sitter is not None:
        sitter.stop()


def backlog_sync_status(project_root: Path) -> dict | None:
    """The running sitter's status for ``project_root``, or None when none is
    running. Consumed by `backlog_outbox_service` (cutover_status, every
    ai_backlog receipt, and the sync alarm)."""
    with _INSTANCES_LOCK:
        sitter = _INSTANCES.get(_key(project_root))
    return sitter.status() if sitter is not None else None
