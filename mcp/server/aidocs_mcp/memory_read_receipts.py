"""DRT-06 — AN ACKNOWLEDGEMENT TOKEN IS NOT A READ.

`edit_memory_gate` blocks a governed edit until every anchored memory's
``route_id`` appears in ``ack_memory_ids``, and then allows it. That checks
SYNTAX. The source says it outright
(scratch/new/doctrine-split/code-bugs.md:220): "An agent can theoretically copy
the refusal's IDs into the retry without reading the memory content."

It is not theoretical. MEASURED on the live estate 2026-09-10: HIGH-priority
memory pointers surfaced on nearly every tool result for a whole session
(`global:empire-doctrine/II-120pct`, `VIII-kind-law`, `XIII-overrides` -- legacy ids,
now `120pct-enforceable`, `kind-law`, `overrides-are-signals` --
`system/invariants.md`, `rules/standards.md`), each carrying its exact
``memory_read`` call, and the acting seat read NONE of them while continuing to
edit. The ack list proved that the agent could retype an integer.

SO THE RECEIPT IS SERVER-MINTED, NEVER CALLER-SUPPLIED.

``mint_memory_read_receipts`` writes one audit row per memory body actually
RETURNED by the read door. ``verify_memory_read_receipts`` looks for those rows.
There is no token parameter anywhere in this module's public surface, which is
the point: a receipt the caller could pass is the ID-copying hole with extra
steps. The only way to create one is to have had the content handed to you by
the read door.

SCOPE = actor + memory epoch + memory id + CONTENT REVISION + REPRESENTATION,
not project/session. A read performed by a DIFFERENT actor, in a DIFFERENT
memory epoch (epochs rotate on compaction, i.e. exactly when the agent loses
the content from context), OF A DIFFERENT BODY, or through a surface that only
showed a FRAGMENT, does not satisfy the gate.

THE CONTENT AXIS, and why the first cut of this module was still bypassable
(r0b, 2026-09-10): bind only actor + epoch + id and this sequence passes —

    read memory M            -> receipt minted
    M IS THEN CHANGED        (operator edit, capture append, law amended)
    retry the edit           -> receipt for the OLD body still matches

"A copied ID plus a receipt to different content is the same semantic bypass
wearing cryptography." THE RECEIPT MUST DIE WHEN THE VERSION IT PROVES IS NO
LONGER THE VERSION GOVERNING THE EDIT. So the receipt carries a hash of the
exact body that was handed over, the verifier re-derives the CURRENT body
through the SAME resolver the read door uses, and a mismatch is a distinct
``RECEIPT_STALE`` state whose refusal says the doctrine CHANGED rather than
"you never read it" — two different obligations with two different discharges.

THE REPRESENTATION AXIS. A receipt is mintable only by the canonical reader
handing over a FULL BODY. A search hit, a surfacing banner or any other snippet
surface proves that one line went past the agent's eyes, which is precisely how
a whole session of HIGH-priority pointers can be "surfaced" and ignored. Such a
surface may mint a receipt only by declaring its representation, and the
verifier accepts only representations in ``ACCEPTED_REPRESENTATIONS`` — which
today is full bodies alone. Nothing in the tree mints a snippet receipt yet;
the axis exists so that ADDING such a surface cannot silently qualify.

THE ONE HONEST WEAKNESS, STATED RATHER THAN HIDDEN. Both identity axes come
from resolvers that legitimately return "" when the host session is not
stamped (``agent_memory_epoch.resolve_epoch`` documents its own fail-open;
``task_actor_identity.stable_actor_id`` returns "" for an unidentifiable host
and ``UNPROVEN_SUBAGENT_ACTOR`` for a subagent missing its axis). Measured in a
bare process: both are "". Treating ""=="" as a match would be precisely the
unknown-laundering this module exists to end, so such a match is reported as
``PROVEN_PATH_ONLY`` with the unproven axes NAMED. The path axis still has to
be satisfied by a real read — so the copy-the-IDs hole is closed even on an
unidentified host — while the cross-actor / cross-epoch guarantee is marked
unproven instead of claimed. What would settle it: a stamped host session
(``resolve_host_identity`` returning a real kind+id) on the calls that mint and
check the receipt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Audit event_kind for a minted receipt. Distinct from the read tool's own
#: ``tool_call_completed`` row on purpose: that row is written for EVERY call
#: that returns, refusals included (#893), so it proves a call happened, not
#: that a body was handed over.
RECEIPT_EVENT_KIND = "memory_read_receipt"

#: Verdict states. Named so a caller cannot confuse them for booleans.
RECEIPT_PROVEN = "proven"  # every axis matched, none of them empty
RECEIPT_PROVEN_PATH_ONLY = "proven_path_only"  # real read of the CURRENT body;
#                                               actor/epoch axes unproven
RECEIPT_STALE = "stale"  # read proven, but of a body that has since CHANGED
RECEIPT_ABSENT = "absent"  # no read of this memory by anyone, ever
RECEIPT_STORE_UNAVAILABLE = "store_unavailable"  # could not look: see `cause`

#: What a receipt claims to have shown the agent. Only a FULL BODY qualifies:
#: a snippet proves a fragment went past, not that the doctrine was read.
REPRESENTATION_FULL_BODY = "full_body"
REPRESENTATION_SNIPPET = "snippet"

#: The representations the edit gate accepts as "read". Widening this set is a
#: security decision and must be argued for, not inferred from a new surface
#: happening to call the mint function.
ACCEPTED_REPRESENTATIONS = frozenset({REPRESENTATION_FULL_BODY})

_MAX_RECEIPT_SCAN = 500


def body_revision(text: str) -> str:
    """Content revision of one memory body.

    A hash, not an mtime or a row version: the receipt has to survive a
    reindex, a palace/sqlite failover and a re-capture that rewrites the row
    without changing a byte, while dying the instant a byte DOES change.
    """
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:32]


def resolve_palace_reader(hub: Any, project_root: Path) -> Any:
    """The drawer-content adapter ``read_memory`` takes, or None.

    ONE RESOLVER, shared by the minting read door and by the verifier's
    current-body lookup. If these two resolved the body differently, a read
    through the palace-first door would hash one body and the verifier would
    hash another -- a permanent, undiagnosable refusal. (#202 wiring.)
    """
    try:
        if getattr(hub, "palace", None) is None:
            return None
        control = getattr(hub, "palace_control", None)
        if control is not None and bool(control.is_palace_disabled(project_root)):
            return None
        from .memory_sqlite_store import PalaceDrawerReader

        return PalaceDrawerReader(project_root)
    except Exception:  # noqa: BLE001 - degrade to the sqlite-only body
        return None


def current_body_revisions(
    hub: Any,
    project_root: Path,
    memory_paths: list[str],
) -> tuple[dict[str, str], str]:
    """({path: revision}, cause). A path absent from the dict is UNKNOWN.

    ``cause`` is non-empty when the lookup itself failed, so the caller can
    refuse with the real reason instead of an unattributed "stale".
    """
    paths = [p for p in (normalize_memory_path(x) for x in (memory_paths or [])) if p]
    if not paths:
        return {}, ""
    try:
        bodies = hub.memory.read_memory(
            project_root, paths, palace=resolve_palace_reader(hub, project_root)
        )
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"
    out: dict[str, str] = {}
    for key, text in (bodies or {}).items():
        if not isinstance(text, str):
            continue
        out[normalize_memory_path(str(key))] = body_revision(text)
    return out, ""


def normalize_memory_path(path: str) -> str:
    """Canonical form of a memory target path for receipt matching.

    The leading ``.MEMORY/`` is stripped. MEASURED while wiring this: memory
    bodies are stored and read WITHOUT that prefix (``read_memory`` returns []
    for ``.MEMORY/doctrines/x.md`` and the body for ``doctrines/x.md``), while
    a ``memory_routes.target_path`` can carry it. Matching the two spellings as
    different memories would make the remedy the refusal NAMES unreachable --
    the agent would read the doctrine, mint a receipt under one spelling, and
    be refused against the other forever. Law 311bf3e6: a named remedy must be
    reachable.
    """
    cleaned = str(path or "").replace("\\", "/").strip().lstrip("/")
    prefix = ".MEMORY/"
    while cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    return cleaned


def resolve_receipt_identity(project_root: Path) -> tuple[str, str, tuple[str, ...]]:
    """(actor_id, memory_epoch, unproven_axes) for the CURRENT call.

    Both halves are resolved SERVER-SIDE through the canonical authorities.
    Neither is reachable from a tool argument, which is the whole security
    property. ``unproven_axes`` names every axis that came back empty or
    refused, so the caller can say which guarantee it does not have.
    """
    unproven: list[str] = []
    actor = ""
    epoch = ""
    try:
        from .task_actor_identity import identity_is_unproven, stable_actor_id

        actor = stable_actor_id(project_root)
        if identity_is_unproven(actor):
            # A subagent that cannot be told apart from its conductor. Its
            # "actor id" is its parent's, so it is not an axis at all.
            actor = ""
            unproven.append("actor:subagent_axis_missing")
        elif not actor:
            unproven.append("actor:host_session_unstamped")
    except Exception as exc:  # noqa: BLE001 - an unresolvable axis is unproven, not absent
        actor = ""
        unproven.append(f"actor:resolver_failed:{type(exc).__name__}")
    try:
        from .agent_memory_epoch import resolve_epoch

        epoch = resolve_epoch(project_root) or ""
        if not epoch:
            unproven.append("epoch:host_session_unstamped")
    except Exception as exc:  # noqa: BLE001
        epoch = ""
        unproven.append(f"epoch:resolver_failed:{type(exc).__name__}")
    return actor, epoch, tuple(unproven)


def mint_memory_read_receipts(
    hub: Any,
    project_root: Path,
    bodies: dict[str, str],
    *,
    session_id: str | None = None,
    source_tool: str = "memory_read",
    representation: str = REPRESENTATION_FULL_BODY,
) -> list[str]:
    """Record that THIS actor, in THIS epoch, was handed THESE exact bodies.

    ``bodies`` maps memory path -> the text actually handed over, because the
    receipt binds the CONTENT REVISION of that text. A caller that has only
    paths and no bodies cannot mint, by construction: it has not shown the
    agent anything.

    ``representation`` must name what was shown. A snippet surface must pass
    ``REPRESENTATION_SNIPPET`` and will NOT satisfy the edit gate -- see
    ACCEPTED_REPRESENTATIONS. Lying about it is not a caller-side attack
    surface, because every mint site is server code inside this package.

    Called from the read door AFTER the store returned content, and only for
    the paths whose body was actually returned -- a requested-but-suppressed
    target (retired memory) mints nothing, because nothing was read.

    Returns the paths it minted for. A failure to mint is swallowed: the read
    itself must not fail because the audit write did, and the CONSEQUENCE of a
    missing receipt is a later refusal to edit, never a silent grant.
    """
    minted: list[str] = []
    revisions = {
        normalize_memory_path(k): body_revision(v)
        for k, v in (bodies or {}).items()
        if normalize_memory_path(k) and isinstance(v, str)
    }
    if not revisions:
        return minted
    actor, epoch, unproven = resolve_receipt_identity(project_root)
    for path in revisions:
        try:
            hub.execution.record_event(
                project_root,
                event_kind=RECEIPT_EVENT_KIND,
                source_kind="memory",
                session_id=session_id or None,
                capability_name=source_tool,
                action_kind="read",
                target_entity=path[:300],
                status="completed",
                payload={
                    "memory_path": path,
                    # Stamped from the server resolvers above. The row's own
                    # agent_epoch column is stamped independently by
                    # record_event_on_connection; this copy is what the
                    # verifier compares, so mint and check read ONE resolver.
                    "receipt_actor_id": actor,
                    "receipt_memory_epoch": epoch,
                    # The axes that kill the receipt when the law moves or the
                    # agent only glimpsed it.
                    "receipt_body_revision": revisions[path],
                    "receipt_representation": str(representation or ""),
                    "receipt_unproven_axes": list(unproven),
                },
            )
            minted.append(path)
        except Exception:  # noqa: BLE001 - see docstring: never fail the read
            continue
    return minted


@dataclass(frozen=True)
class ReceiptVerdict:
    """What the audit store can prove about one set of blocking memories.

    ``state`` is the axis-level summary; ``proven`` / ``missing`` are the
    per-path split the refusal needs in order to name a reachable remedy.
    """

    state: str
    proven: frozenset[str]
    missing: frozenset[str]
    #: Memories this actor DID read, whose body has since changed. A separate
    #: set because it is a separate obligation with a separate discharge: "the
    #: law you read was amended, read it again" is not "you never read it".
    stale: frozenset[str] = frozenset()
    unproven_axes: tuple[str, ...] = ()
    cause: str = ""

    @property
    def is_unavailable(self) -> bool:
        return self.state == RECEIPT_STORE_UNAVAILABLE


def verify_memory_read_receipts(
    hub: Any,
    project_root: Path,
    memory_paths: list[str],
) -> ReceiptVerdict:
    """Which of ``memory_paths`` this actor provably READ at this epoch.

    Matching is AND over five axes:
      1. ``memory_path`` -- the exact blocking memory, not a sibling.
      2. ``receipt_representation`` -- in ACCEPTED_REPRESENTATIONS. A snippet
         receipt never qualifies; a receipt with no representation at all is a
         pre-axis row and is treated as NOT accepted, because an absent claim
         is not a full-body claim.
      3. ``receipt_body_revision`` -- equal to the CURRENT revision of that
         memory. This is the axis r0b found missing: without it, a receipt for
         a body that has since been amended still passes.
      4. ``receipt_actor_id`` -- equal to the current actor AND non-empty.
      5. ``receipt_memory_epoch`` -- equal to the current epoch AND non-empty.

    For 4 and 5, two non-empty values must be EQUAL; two empty values are NOT
    a match (that is the unknown-laundering this exists to stop) -- they
    downgrade the state to ``RECEIPT_PROVEN_PATH_ONLY`` and name the unproven
    axes. Axes 1-3 have no such rung: they are never unknowable without the
    lookup itself failing, which is ``RECEIPT_STORE_UNAVAILABLE``.

    Deliberately NOT filtered by session_id: the receipt's scope is actor +
    epoch + memory id, and a session label is neither. Filtering by session
    would ALSO re-open the #930 failure mode where the recorder and the reader
    disagree about which session is current and no read can ever match.
    """
    wanted = {p for p in (normalize_memory_path(x) for x in (memory_paths or [])) if p}
    if not wanted:
        return ReceiptVerdict(RECEIPT_PROVEN, frozenset(), frozenset())
    actor, epoch, unproven = resolve_receipt_identity(project_root)
    current, body_cause = current_body_revisions(hub, project_root, sorted(wanted))
    unknown_bodies = wanted - set(current)
    if unknown_bodies:
        # WHAT VERSION GOVERNS THIS EDIT IS UNKNOWN, so no receipt can be
        # matched against it. Not stale, not absent: unknown.
        return ReceiptVerdict(
            RECEIPT_STORE_UNAVAILABLE,
            frozenset(),
            frozenset(wanted),
            unproven_axes=unproven,
            cause=(
                body_cause
                or (
                    "the CURRENT body of "
                    + ", ".join(sorted(unknown_bodies))
                    + " could not be resolved, so the version in force at edit "
                    "time is unknown"
                )
            ),
        )
    try:
        events = hub.execution.list_events(
            project_root,
            query=RECEIPT_EVENT_KIND,
            limit=_MAX_RECEIPT_SCAN,
        )
    except Exception as exc:  # noqa: BLE001
        # UNKNOWN STAYS UNKNOWN. The caller decides the authorization
        # consequence by policy; this function never answers "proven" for a
        # store it could not read.
        return ReceiptVerdict(
            RECEIPT_STORE_UNAVAILABLE,
            frozenset(),
            frozenset(wanted),
            unproven_axes=unproven,
            cause=f"{type(exc).__name__}: {exc}",
        )
    proven_full: set[str] = set()
    proven_path_only: set[str] = set()
    stale: set[str] = set()
    #: Memories this actor only ever GLIMPSED. Tracked so the refusal can name
    #: the reachable remedy ("open the body", not "read it for the first
    #: time"), which is the whole reason the snippet representation is a named
    #: value instead of just "anything that is not full_body".
    snippet_refused: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("event_kind") or "") != RECEIPT_EVENT_KIND:
            continue
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        path = normalize_memory_path(str(payload.get("memory_path") or ""))
        if path not in wanted:
            continue
        row_actor = str(payload.get("receipt_actor_id") or "")
        row_epoch = str(payload.get("receipt_memory_epoch") or "")
        row_revision = str(payload.get("receipt_body_revision") or "")
        row_representation = str(payload.get("receipt_representation") or "")
        # A receipt minted BY SOMEBODY ELSE, or in another epoch, is not this
        # actor's proof. Both mismatches are hard rejections -- the whole
        # point of scoping the receipt.
        if row_actor and actor and row_actor != actor:
            continue
        if row_epoch and epoch and row_epoch != epoch:
            continue
        # A glimpse is not a reading. An absent claim is not a full-body claim.
        if row_representation not in ACCEPTED_REPRESENTATIONS:
            # THE PROHIBITION IS ENFORCED HERE, AND IT NAMES ITSELF. Rejecting
            # is not enough: an actor who searched the doctrine and got a
            # snippet must be told that the snippet was SEEN AND REFUSED, not
            # left to read "absent" and conclude the receipt store lost its
            # read. `REPRESENTATION_SNIPPET` is referenced by this live
            # rejection authority, which is what makes the rule a rule rather
            # than a comment: widen ACCEPTED_REPRESENTATIONS and this branch
            # stops firing, so the constant cannot drift out of enforcement.
            if row_representation == REPRESENTATION_SNIPPET:
                snippet_refused.add(path)
            continue
        # THE CONTENT AXIS. A receipt for a body that has since been amended
        # proves a STALE understanding, and is recorded as such rather than
        # silently discarded -- the agent needs to be told the law MOVED, not
        # that it never read it.
        if row_revision != current.get(path, ""):
            stale.add(path)
            continue
        if row_actor and actor and row_epoch and epoch:
            proven_full.add(path)
        else:
            # One or both axes are empty on one side, so equality proves
            # nothing about actor or epoch. The READ is still proven.
            proven_path_only.add(path)
    proven = proven_full | proven_path_only
    missing = wanted - proven
    stale_only = (stale & missing) - proven
    if stale_only:
        # Stale leads: it is the more specific, more actionable diagnosis, and
        # reporting it as "absent" would send the agent looking for a read it
        # already performed.
        state = RECEIPT_STALE
    elif missing:
        state = RECEIPT_ABSENT
    elif proven_path_only:
        state = RECEIPT_PROVEN_PATH_ONLY
    else:
        state = RECEIPT_PROVEN
    glimpsed = sorted(snippet_refused & missing)
    return ReceiptVerdict(
        state,
        frozenset(proven),
        frozenset(missing),
        stale=frozenset(stale_only),
        unproven_axes=unproven if proven_path_only or missing else (),
        # NOT a `cause` for the store being unreadable -- the store answered
        # fine. This says WHY the answer is "no": a `REPRESENTATION_SNIPPET`
        # receipt exists and was refused. Reported only when the refusal is
        # actually what is blocking the path, so it can never soften a verdict.
        cause=(
            "a snippet-only read receipt exists for "
            + ", ".join(glimpsed)
            + f" and is REFUSED: only {REPRESENTATION_FULL_BODY} proves the "
            "doctrine was read. Open the full body, not a search result."
            if glimpsed
            else ""
        ),
    )
