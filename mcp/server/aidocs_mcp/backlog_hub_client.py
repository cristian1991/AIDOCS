"""Read-through client for the SERVER-AUTHORITATIVE backlog (P0).

Operator ruling 2026-07-21: backlog is org-scoped collaborative state — the same
category as orgs/perms/seats, which are already authoritative in the codenexus DB
and resolved on login. Backlog joins them; AIDOCS keeps a CACHE, not a peer.

P0 IS DELIBERATELY READ-ONLY AND NON-DESTRUCTIVE
────────────────────────────────────────────────
Local ``project_backlog`` is STILL the writer of record in this phase, so the
server snapshot is stored in a SEPARATE cache table and NEVER merged into it.
That makes convergence *observable* (``cache_status`` reports the delta) without
being able to lose a single local row. The write path — where the server becomes
the writer and the local table becomes a true cache — is P1.

BOUND vs UNBOUND (the universal rule)
─────────────────────────────────────
A project is BOUND when it RESOLVES to a codenexus project the authenticated
principal is entitled to. Unbound / local-only projects never call the hub at
all and behave exactly as they do today — no server authority, no network
dependency, fully offline. Everything here fail-closes to "unbound" on any doubt.

THE BINDING IS DERIVED, NOT TYPED (operator ruling 2026-07-21)
──────────────────────────────────────────────────────────────
This module briefly required the operator to hand-enter ``sync.hub_org_id`` +
``sync.vps_hub_project_id``. That was a mistake of the same shape as building a
write queue with no producer: the facts were ALREADY KNOWN and I added
configuration for them anyway, in a design whose own premise is "identity
resolves on login". Both are now derived:

  * **project** — from the canonical project-context resolver
    (``project_binding_resolver``): on the gate, the authenticated principal's
    SELECTED project; locally, this box's own project registration. Registering
    / connecting / selecting a project is the one real act; it already writes
    the id. #972 removed this module's own rival derivation — see
    ``registered_binding``.
  * **org** — from that registration's ``org_id`` (org is a derived property of a
    project — see #283), and failing that from the LOGIN: the local identity
    store holds the signed-in email, codenexus holds the same email, and
    ``/api/internal/identity/orgs`` closes the join. Exactly one entitled org ⇒
    bound; zero or several ⇒ unbound (an ambiguous account must be resolved by a
    human, never guessed).

The two config keys survive ONLY as explicit overrides for the odd host that
must point somewhere else. Nothing in the normal flow asks anyone to set them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
from dataclasses import dataclass
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ._sqlite_connect import connect as _canonical_connect

_INTERNAL_URL_ENV = "AIDOCS_CODENEXUS_INTERNAL_URL"
_INTERNAL_SECRET_ENV = "AIDOCS_INTERNAL_S2S_SECRET"  # gitleaks:allow (env NAME)
_TIMEOUT_S = 8.0

# ── WHY convergence is unknown — a DISCRIMINATED reason, never one label ──────
#
# OPERATOR RULING 2026-08-30: "don't collapse all converged: None into one
# 'never compared' state. Need explicit reason: unbound_project /
# hub_unconfigured / hub_unreachable / auth_refused / never_fetched / maybe
# cache_error. Otherwise new label still hides cause."
#
# That is the correction to a correction. `converged: None` already hid the
# cause; replacing it with a single "never compared" would have moved the same
# collapse one level up and felt like progress. These six are distinguishable AT
# THE POINT EACH IS KNOWN, and they need DIFFERENT REPAIRS — a registration, two
# env vars, a network, a credential, patience, or a broken local db. A reader who
# cannot tell them apart cannot act on any of them.
REASON_UNBOUND = "unbound_project"
REASON_UNCONFIGURED = "hub_unconfigured"
REASON_UNREACHABLE = "hub_unreachable"
REASON_AUTH_REFUSED = "auth_refused"
REASON_NEVER_FETCHED = "never_fetched"
REASON_CACHE_ERROR = "cache_error"
# #972: the gate-side sibling of REASON_UNCONFIGURED. A project IS selected and
# authoritative, its row just carries no org — and on the gate no credential can
# supply one, because the machine-login fallback is deliberately not consulted
# there. Naming it separately keeps REASON_UNCONFIGURED's remedy true.
REASON_SELECTION_NO_ORG = "selected_project_has_no_org"
# #1002 gap 1: the S2S pair is absent (a developer box) AND the operator's
# gate credential cannot be presented — signed in locally only, expired, or
# already refused by the authority (#992 latch). Distinct from UNCONFIGURED
# (nothing at all: no S2S pair, nobody signed in) because the repair differs:
# sign in to CODENEXUS again, not "export two env vars".
REASON_GATE_CREDENTIAL = "gate_credential_unusable"
# A 403 FROM THE GATE IS NOT ALWAYS A CREDENTIAL REFUSAL. `/v1/backlog`
# answers 403 `insufficient_scope` when the token is perfectly valid but
# carries no `sync` scope (a Dashboard token never does — it holds
# `catalog tier_r_invoke status project_import`; RC_BACKLOG requires
# SCOPE_SYNC), and 403 `tenant_mismatch` when the principal is fine and the
# project simply is not theirs. Filing either as a credential refusal (#992
# latch) locked the operator's ONLY cloud credential machine-wide, and a fresh
# sign-in re-latched on the next poll. Named apart so the remedy is true.
REASON_INSUFFICIENT_SCOPE = "insufficient_scope"
REASON_TENANT_MISMATCH = "tenant_mismatch"
# `submit_intents` needs an author. Missing one is a WHO problem — nobody is
# signed in on this box, or the login could not be resolved — not "this
# directory is unbound". The old guard folded it into REASON_UNBOUND and sent
# the operator to register a project that was already registered.
REASON_OPERATOR_UNIDENTIFIED = "operator_unidentified"

#: The gate's 403 refusal words that say NOTHING about the credential itself,
#: mapped to the discriminated reason each earns. Any other 403 keeps the
#: #992 behaviour (auth refused, latched): fail closed on the unknown.
_NON_CREDENTIAL_403: dict[str, str] = {
    "insufficient_scope": REASON_INSUFFICIENT_SCOPE,
    "tenant_mismatch": REASON_TENANT_MISMATCH,
}

#: WHICH ROUTE carried (or would carry) a hub call. The VPS keeps the
#: loopback S2S route; a developer box has only the operator-facing gate
#: route (`/v1/backlog`, `outer_gate_transport._ogt_backlog`), which forwards
#: with the S2S secret the gate holds. Reported so a reader can tell "drained
#: through the gate" from "drained on the VPS" without guessing.
ROUTE_S2S = "s2s"
ROUTE_GATE = "gate"
#: The remedy for each, so the reason names an act rather than a condition
#: (law 311bf3e6 — a named remedy must be reachable).
REASON_REMEDY: dict[str, str] = {
    REASON_UNBOUND: (
        "this directory is not connected to a cloud project — register/connect "
        "it; nothing identifies WHICH project it is"
    ),
    REASON_UNCONFIGURED: (
        f"no credential can reach the hub — on the VPS set {_INTERNAL_URL_ENV} "
        f"and {_INTERNAL_SECRET_ENV} where the daemon runs; on a developer box "
        "sign in to CODENEXUS from the Dashboard so a gate-issued credential is "
        "cached, and the drain goes through the gate's /v1/backlog route"
    ),
    REASON_GATE_CREDENTIAL: (
        "the operator's gate credential cannot be presented (see `credential` "
        "for which way: local-only session, expired, or refused by the "
        "authority) — sign in to CODENEXUS again from the Dashboard; the "
        "service-to-service pair is absent on this surface so nothing else can "
        "carry the outbox"
    ),
    REASON_UNREACHABLE: "the hub did not answer — network, DNS or the host is down",
    REASON_AUTH_REFUSED: (
        "the hub refused the credential — the S2S secret is wrong, expired, or "
        "not entitled to this org/project"
    ),
    REASON_NEVER_FETCHED: (
        "bound and configured, but no successful fetch has happened yet — wait "
        "one sitter poll, or call ai_backlog(mode='cutover_status') again"
    ),
    REASON_CACHE_ERROR: (
        "the local snapshot could not be written — the reason is a LOCAL "
        "storage fault, not a server one"
    ),
    REASON_SELECTION_NO_ORG: (
        "the selected project is registered without an owning org — stamp the "
        "project's org; credentials are NOT the cause here and setting them "
        "would change nothing"
    ),
    REASON_INSUFFICIENT_SCOPE: (
        "the gate accepted the credential but it carries no `sync` scope — a "
        "Dashboard sign-in token never does; obtain a sync-scoped gate "
        "credential (or run the drain on the VPS, where the S2S pair is). "
        "The credential is NOT revoked and has not been latched"
    ),
    REASON_TENANT_MISMATCH: (
        "the gate accepted the credential but this project does not resolve "
        "inside the operator's tenancy — check which org/project this "
        "directory is connected to; the credential itself is fine"
    ),
    REASON_OPERATOR_UNIDENTIFIED: (
        "the project is bound but nothing identifies WHO is submitting — "
        "sign in to CODENEXUS on this box so writes carry an author"
    ),
}


def _db_path(project_root: Path) -> Path:
    # The SAME per-project store DB the backlog lives in — no new database.
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _override(key: str, project_root: Path) -> str:
    """An explicitly-set config override, or "". Never raises."""
    try:
        from .config import get_setting

        return str(get_setting(key, project_root=project_root, default="") or "").strip()
    except Exception:  # noqa: BLE001 — config trouble ⇒ derive instead
        return ""


def registered_binding(project_root: Path) -> tuple[str, str]:
    """(org_id, project_id) for this call, else ("", "") — ASKED, NOT DERIVED.

    #972. This function used to ANSWER the question itself: it took a path,
    opened the machine-global identity DB and scanned ``gate_projects`` for a
    row whose ``root`` matched. That made backlog a SECOND authority on "which
    project/org am I operating on?", beside the gate's authenticated selection —
    and the two disagreed. MEASURED on the gate: ``ai_whoami`` reported
    cristian1991/AIDOCS_PRIVATE selected while ``cutover_status`` on the same
    surface answered ``unbound_project``, because #516 had moved
    ``gate_projects`` out of the file this scan reads.

    The repair was NOT to point the scan at the tenant file. Operator, verbatim:
    "Separate tenant DB FILES are correct isolation. Separate ANSWERS to 'what
    project/org am I operating on?' are not." So the derivation is GONE from
    here entirely and ``project_binding_resolver`` owns it — gate calls resolve
    from the authenticated principal + selected project, local calls from the
    local registration, and nothing in this module opens a registry.

    ("", "") still means UNBOUND to every caller, unchanged. WHY it is unbound
    is available, discriminated, from ``binding_context``.
    """
    return _binding_context(project_root).as_tuple()


def _binding_context(project_root: Path):
    """The full resolver answer (org, project, source, reason). Never raises."""
    from . import project_binding_resolver

    try:
        return project_binding_resolver.resolve(project_root)
    except Exception:  # noqa: BLE001 — an unanswerable context is UNBOUND, never a guess
        return project_binding_resolver.ProjectBinding(
            reason=project_binding_resolver.REASON_LOCAL_REGISTRY_ERROR,
        )


def binding_context(project_root: Path) -> dict:
    """WHICH resolver answered, and why it could not — for diagnostics.

    Kept as a plain dict so a surface can splice it into a payload without
    importing the resolver, and so an added field cannot break a caller.
    """
    b = _binding_context(project_root)
    return {
        "org_id": b.org_id,
        "project_id": b.project_id,
        "source": b.source,
        "reason": b.reason,
        "remedy": b.remedy(),
    }


def _norm_root(root: Any) -> str:
    """Case/symlink-folded path key, used HERE only as a memo key (below).

    ONE implementation, owned by ``project_binding_resolver``, so a second
    spelling of "the same directory" cannot drift away from the one the local
    registration matches on.
    """
    from .project_binding_resolver import norm_root

    return norm_root(root)


# Resolving the org costs a network round-trip, and `binding()` is on the write
# path (every backlog emit). Memoised per (root, email) for the process: a login
# change produces a different key, so a stale org can never outlive the session
# that earned it.
_ORG_MEMO: dict[tuple[str, str], str] = {}


def login_email(project_root: Path) -> str:
    """Email of the machine's signed-in operator, or "". Never raises."""
    try:
        from .identity_store import IdentityStore
        from .operator_auth_service import OperatorAuthService

        uid = str(OperatorAuthService().resolve_machine_login(Path(project_root)) or "").strip()
        if not uid:
            return ""
        user = IdentityStore().get_user_by_id(Path(project_root), uid)
        return str(getattr(user, "email", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def org_from_identity(project_root: Path) -> str:
    """The signed-in operator's org, derived from their LOGIN. "" when it is not
    unambiguous.

    Exactly one entitled org ⇒ that org. Zero ⇒ unbound. Several ⇒ ALSO unbound:
    which of an operator's orgs a project belongs to is a real decision, and
    guessing it would write one org's backlog into another's tenancy.
    """
    email = login_email(project_root)
    if not email:
        return ""
    memo_key = (_norm_root(project_root), email)
    if memo_key in _ORG_MEMO:
        return _ORG_MEMO[memo_key]
    orgs = _fetch_orgs(email)
    resolved = str(orgs[0].get("id") or "").strip() if orgs and len(orgs) == 1 else ""
    if orgs is not None:
        # Only memoise an ANSWER. A failed lookup (None) must be retried, or a
        # single offline moment would pin the project as unbound for the session.
        _ORG_MEMO[memo_key] = resolved
    return resolved


def _fetch_orgs(email: str) -> list[dict] | None:
    """GET the orgs for a login email. None = could not ask (never an exception)."""
    base = os.environ.get(_INTERNAL_URL_ENV, "").strip()
    secret = os.environ.get(_INTERNAL_SECRET_ENV, "").strip()
    if not base or not secret or not email:
        return None
    url = (
        base.rstrip("/")
        + "/api/internal/identity/orgs?email="
        + urllib.parse.quote(str(email), safe="")
    )
    req = urllib.request.Request(  # noqa: S310 — fixed internal host from operator env
        url,
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            if getattr(resp, "status", 200) != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    orgs = data.get("orgs") if isinstance(data, dict) else None
    return orgs if isinstance(orgs, list) else None


def binding(project_root: Path) -> tuple[str, str]:
    """(org_id, project_id) for a BOUND project, else ("", "").

    DERIVED, in this order:
      1. explicit config overrides (escape hatch; normally unset),
      2. the canonical project context — its id, and its org,
      3. the signed-in operator's org, when the context carries none —
         LOCAL SURFACE ONLY, see below.

    Fail-closed: a partial binding is treated as UNBOUND, so a project that only
    half-resolves keeps working locally instead of half-talking to a hub.

    WHY RUNG 3 IS LOCAL-ONLY (#972). ``org_from_identity`` resolves the org from
    THE MACHINE'S signed-in login. On the operator's own box that IS the caller.
    On the GATE it is the daemon's local operator — not the authenticated
    principal making this request — so consulting it there would answer "which
    org is this project in?" from a second, unrelated authority, and could
    attribute one tenant's project to whoever last signed in on the host. It is
    also exactly the "widen something until a bound verdict appears" move the
    operator forbade: a gate registration carrying no org must keep presenting
    as org-less, so the NEXT blocker is reported honestly rather than papered
    over by a plausible-looking substitute.

    The surface check is a ContextVar read, not a second registry lookup —
    asking WHICH SURFACE this is costs nothing and touches no store.
    """
    reg_org, reg_pid = registered_binding(project_root)

    pid = _override("sync.vps_hub_project_id", project_root) or reg_pid
    if not pid:
        # No context and no override: nothing identifies WHICH cloud project
        # this directory is. That is the one genuinely unknown fact, and it is
        # answered by connecting/selecting the project, not by typing a key.
        return ("", "")

    org = _override("sync.hub_org_id", project_root) or reg_org
    if not org and not _on_gate_surface():
        org = org_from_identity(project_root)
    return (org, pid) if org else ("", "")


def _on_gate_surface() -> bool:
    """Is a gate dispatch in scope for THIS call? A ContextVar read, no I/O."""
    from .project_binding_resolver import gate_principal

    return gate_principal() is not None


def is_bound(project_root: Path) -> bool:
    org_id, _ = binding(project_root)
    return bool(org_id)


@dataclass(frozen=True)
class HubRoute:
    """HOW this process can reach the backlog hub, or why it cannot.

    `kind` is ROUTE_S2S / ROUTE_GATE, or "" with `reason` (+ `credential`)
    saying why neither is usable. `token` is the bearer for that route.
    """

    kind: str
    base: str = ""
    token: str = ""
    reason: str = ""
    credential: str = ""


def _gate_base(project_root: Path | None = None) -> str:
    from .sync_vps import DEFAULT_BASE_URL

    try:
        return str(_override("sync.vps_hub_url", project_root) or DEFAULT_BASE_URL)
    except Exception:  # noqa: BLE001
        return DEFAULT_BASE_URL


def hub_route(project_root: Path | None = None) -> HubRoute:
    """Resolve the route WITHOUT spending a request (#1002 gap 1).

    ORDER: the S2S pair first — the VPS has it and its loopback route is the
    authority's own door. Absent that, the operator's GATE credential from the
    shared cache, presented to the gate's `/v1/backlog` (which re-checks
    tenancy and forwards with the secret it holds). NEVER the local identity
    token: `cached_gate_credential` only ever returns a gate-issued one, and
    a latched (#992) or local-only row comes back with no token and a reason.
    """
    base = os.environ.get(_INTERNAL_URL_ENV, "").strip()
    secret = os.environ.get(_INTERNAL_SECRET_ENV, "").strip()
    if base and secret:
        return HubRoute(kind=ROUTE_S2S, base=base.rstrip("/"), token=secret)
    from .operator_token_resolution import (
        GATE_CRED_ABSENT,
        GATE_CRED_OK,
        cached_gate_credential,
    )

    cred = cached_gate_credential()
    if cred.reason == GATE_CRED_OK and cred.token:
        return HubRoute(
            kind=ROUTE_GATE,
            base=_gate_base(project_root).rstrip("/"),
            token=cred.token,
            credential=cred.reason,
        )
    reason = REASON_UNCONFIGURED if cred.reason == GATE_CRED_ABSENT else REASON_GATE_CREDENTIAL
    return HubRoute(kind="", reason=reason, credential=cred.reason)


def _file_gate_answer(route: HubRoute, status: int) -> None:
    """After a live contact THROUGH THE GATE: 200 stamps the hourly recheck
    (#1000), 401 — or a 403 that IS an auth refusal — latches the credential
    (#992). A 403 for scope or tenancy is NOT filed: it says nothing about
    the credential (see `_refusal_reason`). S2S answers say nothing about
    the operator's credential and are not filed either."""
    if route.kind != ROUTE_GATE:
        return
    try:
        from .operator_token_resolution import record_gate_answer

        record_gate_answer(status=int(status))
    except Exception:  # noqa: BLE001 — filing must never fail the caller
        pass


def _refusal_word(raw: bytes | str | None) -> str:
    """The gate's `error` word from a refusal body, or "". Never raises."""
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    return str(data.get("error") or "").strip() if isinstance(data, dict) else ""


def _refusal_reason(status: int, raw_body: bytes | str | None) -> str:
    """WHICH reason a non-200 earns, from the status AND the body.

    401 and an opaque 403 are the authority refusing the credential
    (REASON_AUTH_REFUSED, latched by the caller). A 403 whose body names a
    scope or tenancy refusal earns its own reason and must NOT latch. 5xx /
    404 / anything else stays unreachable rather than guessed.
    """
    code = int(status or 0)
    if code == 401:
        return REASON_AUTH_REFUSED
    if code == 403:
        return _NON_CREDENTIAL_403.get(_refusal_word(raw_body), REASON_AUTH_REFUSED)
    return REASON_UNREACHABLE


def _file_refusal(route: HubRoute, status: int, raw_body: bytes | str | None) -> str:
    """Classify a non-200, file it against the credential ONLY when it is
    about the credential, and return the discriminated reason."""
    reason = _refusal_reason(status, raw_body)
    if reason == REASON_AUTH_REFUSED:
        _file_gate_answer(route, status)
    return reason


def _http_error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read() or b""
    except Exception:  # noqa: BLE001 — a body-less error is still an answer
        return b""


def fetch_backlog(
    org_id: str,
    project_id: str,
    outcome: dict | None = None,
    project_root: Path | None = None,
) -> list[dict] | None:
    """GET the authoritative backlog. None = unavailable (never an exception).

    None and [] are DIFFERENT: None means "could not ask" (offline, unconfigured,
    refused) and the caller must fall back to local; [] means the server
    authoritatively answered "this project has no backlog".

    `outcome` IS AN OUT-PARAMETER, and it exists because None collapsed SIX
    distinguishable failures into one value: no url, no secret, no org, no
    project, a non-200, and a transport exception. A caller could not tell "you
    never configured this" from "the credential was refused" from "the network
    is down" — three states with three different repairs.

    Pass a dict and it comes back carrying `reason`. OPTIONAL on purpose, so the
    one production caller that needs the discrimination asks for it and nothing
    else has to change.

    `project_root` reaches `hub_route` so a PROJECT-SCOPED `sync.vps_hub_url`
    override is honoured on the gate route — `_vps_hub_reconcile` already
    honoured it and this path silently did not.

    IT IS NOT FREE FOR EVERY SEAM, and claiming otherwise was wrong: a test that
    patches this with a FIXED-arity lambda breaks on an added argument
    (test_sqlite_connection_lifecycle did, twice, and was updated to accept and
    ignore it). Seams written `*_a, **_k` are unaffected. Recorded because
    "purely additive" is the kind of claim that is easy to make and easy to be
    wrong about.
    """

    def _out(reason: str) -> None:
        if outcome is not None:
            outcome["reason"] = reason

    if not org_id or not project_id:
        _out(REASON_UNBOUND)
        return None
    route = hub_route(project_root)
    if outcome is not None:
        outcome["route"] = route.kind
        outcome["credential"] = route.credential
    if not route.kind:
        _out(route.reason)
        return None
    if route.kind == ROUTE_S2S:
        url = (
            route.base
            + "/api/internal/backlog?orgId="
            + urllib.parse.quote(str(org_id), safe="")
            + "&projectId="
            + urllib.parse.quote(str(project_id), safe="")
        )
    else:
        # The gate resolves the org from the principal; orgId is not sent.
        url = (
            route.base
            + "/v1/backlog?projectId="
            + urllib.parse.quote(str(project_id), safe="")
        )
    req = urllib.request.Request(  # noqa: S310 — fixed hub host from operator env/config
        url,
        headers={"Authorization": f"Bearer {route.token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            if status != 200:
                # 401 (or an opaque 403) is a CREDENTIAL problem, a 403 for
                # scope/tenancy is not, and 5xx/404 is neither; telling them
                # apart is the difference between "sign in again", "get a
                # sync-scoped token" and "the hub is unwell".
                _out(_file_refusal(route, status, resp.read()))
                return None
            _file_gate_answer(route, 200)
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # urlopen RAISES on 4xx/5xx
        code = int(getattr(exc, "code", 0) or 0)
        _out(_file_refusal(route, code, _http_error_body(exc)))
        return None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        _out(REASON_UNREACHABLE)
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        # A 200 whose body is not the contract is not a reachable hub answering
        # honestly — it is something else on that URL.
        _out(REASON_UNREACHABLE)
        return None
    _out("")
    return items


def submit_intents(
    org_id: str,
    project_id: str,
    updated_by: str,
    intents: list[dict],
    outcome: dict | None = None,
    project_root: Path | None = None,
) -> dict | None:
    """POST queued intents for server adjudication. None = unavailable.

    Returns the server's {applied, conflicts, rejected}. Conflicts are the
    server saying "this intent was composed against a row that has since moved"
    — they are reported back to the operator, never merged away.

    TWO ROUTES (#1002 gap 1). The S2S pair reaches the loopback internal route
    directly (the VPS). Without it, a gate-issued operator credential reaches
    the gate's `/v1/backlog`, which re-checks tenancy and forwards with the
    secret it holds. Before this, a developer box answered `hub_unavailable`
    on every poll and 55 intents could never leave the machine.

    `outcome` (optional, like `fetch_backlog`'s) comes back with `reason`,
    `route` and `credential` so the caller can say WHY nothing was sent.
    """

    def _out(reason: str) -> None:
        if outcome is not None:
            outcome["reason"] = reason

    if not org_id or not project_id:
        _out(REASON_UNBOUND)
        return None
    if not updated_by:
        # A WHO problem, not a WHICH-project one: the binding is fine, nobody
        # is identified to author the writes. Split from UNBOUND because the
        # remedy is "sign in", never "register the project".
        _out(REASON_OPERATOR_UNIDENTIFIED)
        return None
    route = hub_route(project_root)
    if outcome is not None:
        outcome["route"] = route.kind
        outcome["credential"] = route.credential
    if not route.kind:
        _out(route.reason)
        return None
    if not intents:
        _out("")
        return {"applied": [], "conflicts": [], "rejected": []}
    body: dict[str, Any] = {
        "projectId": project_id,
        "updatedBy": updated_by,
        "intents": intents,
    }
    if route.kind == ROUTE_S2S:
        body["orgId"] = org_id
        url = route.base + "/api/internal/backlog"
    else:
        # orgId deliberately NOT sent: the gate resolves it from the
        # principal's entitlement and ignores a caller-supplied one.
        url = route.base + "/v1/backlog"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — fixed hub host from operator env/config
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {route.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 200) or 200)
            if status != 200:
                _out(_file_refusal(route, status, resp.read()))
                return None
            _file_gate_answer(route, 200)
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        _out(_file_refusal(route, code, _http_error_body(exc)))
        return None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        _out(REASON_UNREACHABLE)
        return None
    if not isinstance(data, dict):
        _out(REASON_UNREACHABLE)
        return None
    _out("")
    return data


def drain_queue(project_root: Path, updated_by: str = "") -> dict:
    """Submit queued intents and fold the verdicts back. Never raises.

    Unbound projects and an unreachable hub are BOTH no-ops that leave the queue
    intact — an offline drain must never look like a successful one.

    WHY is a DISCRIMINATED reason (#1002 gap 3), never `hub_unavailable`: that
    one word covered "no credential on this box", "the gate refused it" and
    "the network is down", three different repairs. And it is PERSISTED
    (`last_drain_outcome`) because `cutover_status` asks long after the poll
    that discovered it, often from another process.
    """
    out: dict[str, Any] = {"bound": False, "submitted": 0, "error": ""}
    org_id, project_id = binding(Path(project_root))
    if not org_id:
        return out
    out["bound"] = True
    try:
        from . import backlog_write_queue as _q

        items = _q.pending(Path(project_root), project_id)
        if not items:
            return out
        out["submitted"] = len(items)
        uid = updated_by or _resolve_uid(Path(project_root))
        outcome: dict[str, Any] = {}
        verdicts = submit_intents(
            org_id, project_id, uid, items, outcome, project_root=Path(project_root)
        )
        out["route"] = str(outcome.get("route") or "")
        out["credential"] = str(outcome.get("credential") or "")
        if verdicts is None:
            reason = str(outcome.get("reason") or REASON_UNREACHABLE)
            out["error"] = reason  # queue untouched: retried next cycle
            out["remedy"] = REASON_REMEDY.get(reason, "")
            _record_drain_outcome(Path(project_root), project_id, out)
            return out
        out.update(_q.apply_verdicts(Path(project_root), verdicts))
        _record_drain_outcome(Path(project_root), project_id, out)
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
        _record_drain_outcome(Path(project_root), project_id, out)
    return out


def _resolve_uid(project_root: Path) -> str:
    """Authenticated operator uid for attribution (best-effort)."""
    try:
        from .operator_auth_service import OperatorAuthService

        return str(OperatorAuthService().resolve_machine_login(project_root) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _ensure_cache_table(conn: sqlite3.Connection) -> None:
    # SEPARATE from project_backlog on purpose: P0 must not be able to mutate
    # the local writer-of-record.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backlog_server_cache ("
        " global_id TEXT PRIMARY KEY,"
        " project_id TEXT NOT NULL,"
        " display_id INTEGER,"
        " status TEXT,"
        " priority TEXT,"
        " updated_at TEXT,"
        " payload TEXT NOT NULL,"
        " fetched_at TEXT NOT NULL)"
    )


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    """Where the last fetch OUTCOME lives.

    `auth_refused` and `hub_unreachable` are only knowable INSIDE a fetch, and
    `cache_status` is asked long after that fetch returned — often in a
    different process. Without somewhere durable to put it, those two reasons
    could only ever be reported by the one call that discovered them, and every
    later reader would be back to an undifferentiated "unknown".
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backlog_cache_meta ("
        " project_id TEXT PRIMARY KEY,"
        " last_reason TEXT,"
        " last_attempt_at TEXT,"
        " last_success_at TEXT)"
    )


def _record_fetch_outcome(conn: sqlite3.Connection, project_id: str, reason: str, stamp: str) -> None:
    """Upsert the last outcome. `reason` empty == the fetch succeeded."""
    _ensure_meta_table(conn)
    if reason:
        conn.execute(
            "INSERT INTO backlog_cache_meta (project_id, last_reason, last_attempt_at) "
            "VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "last_reason = excluded.last_reason, "
            "last_attempt_at = excluded.last_attempt_at",
            (str(project_id), reason, stamp),
        )
    else:
        conn.execute(
            "INSERT INTO backlog_cache_meta "
            "(project_id, last_reason, last_attempt_at, last_success_at) "
            "VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "last_reason = '', last_attempt_at = excluded.last_attempt_at, "
            "last_success_at = excluded.last_success_at",
            (str(project_id), "", stamp, stamp),
        )


def _last_fetch_reason(conn: sqlite3.Connection, project_id: str) -> tuple[str, bool]:
    """(last_reason, ever_succeeded). No row == never attempted."""
    _ensure_meta_table(conn)
    row = conn.execute(
        "SELECT last_reason, last_success_at FROM backlog_cache_meta WHERE project_id = ?",
        (str(project_id),),
    ).fetchone()
    if not row:
        return ("", False)
    return (str(row[0] or ""), bool(row[1]))


def _ensure_drain_meta_table(conn: sqlite3.Connection) -> None:
    """Where the last DRAIN outcome lives (#1002 gap 3) — the submit-side twin
    of `backlog_cache_meta`. Raw sqlite reads of the queue are refused to
    agents (forbidden_aidocs_path), so without this the 55 pending rows' fate
    was unreadable from any tool."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backlog_drain_meta ("
        " project_id TEXT PRIMARY KEY,"
        " last_reason TEXT,"
        " last_route TEXT,"
        " last_credential TEXT,"
        " last_submitted INTEGER,"
        " last_attempt_at TEXT,"
        " last_success_at TEXT)"
    )


def _record_drain_outcome(project_root: Path, project_id: str, out: dict) -> None:
    """Upsert the last drain outcome. Best-effort: recording WHY must never
    break the drain that produced it."""
    try:
        db = _db_path(Path(project_root))
        db.parent.mkdir(parents=True, exist_ok=True)
        stamp = _iso_now()
        reason = str(out.get("error") or "")
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_drain_meta_table(conn)
            if reason:
                conn.execute(
                    "INSERT INTO backlog_drain_meta (project_id, last_reason, last_route,"
                    " last_credential, last_submitted, last_attempt_at)"
                    " VALUES (?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET"
                    " last_reason = excluded.last_reason,"
                    " last_route = excluded.last_route,"
                    " last_credential = excluded.last_credential,"
                    " last_submitted = excluded.last_submitted,"
                    " last_attempt_at = excluded.last_attempt_at",
                    (
                        str(project_id),
                        reason,
                        str(out.get("route") or ""),
                        str(out.get("credential") or ""),
                        int(out.get("submitted") or 0),
                        stamp,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO backlog_drain_meta (project_id, last_reason, last_route,"
                    " last_credential, last_submitted, last_attempt_at, last_success_at)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET"
                    " last_reason = '',"
                    " last_route = excluded.last_route,"
                    " last_credential = excluded.last_credential,"
                    " last_submitted = excluded.last_submitted,"
                    " last_attempt_at = excluded.last_attempt_at,"
                    " last_success_at = excluded.last_success_at",
                    (
                        str(project_id),
                        "",
                        str(out.get("route") or ""),
                        str(out.get("credential") or ""),
                        int(out.get("submitted") or 0),
                        stamp,
                        stamp,
                    ),
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


def last_drain_outcome(project_root: Path, project_id: str) -> dict:
    """The last recorded drain for this project — reason, route, credential
    state, remedy, timestamps. `attempted: False` when no drain has ever
    recorded itself (a different fact from "the last one succeeded")."""
    out: dict[str, Any] = {
        "attempted": False,
        "reason": "",
        "route": "",
        "credential": "",
        "remedy": "",
        "submitted": 0,
        "last_attempt_at": "",
        "last_success_at": "",
    }
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return out
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_drain_meta_table(conn)
            row = conn.execute(
                "SELECT last_reason, last_route, last_credential, last_submitted,"
                " last_attempt_at, last_success_at FROM backlog_drain_meta"
                " WHERE project_id = ?",
                (str(project_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — diagnostic; never raises
        return out
    if not row:
        return out
    out.update(
        attempted=True,
        reason=str(row[0] or ""),
        route=str(row[1] or ""),
        credential=str(row[2] or ""),
        submitted=int(row[3] or 0),
        last_attempt_at=str(row[4] or ""),
        last_success_at=str(row[5] or ""),
    )
    out["remedy"] = REASON_REMEDY.get(out["reason"], "")
    return out


def refresh_cache(project_root: Path, *, now: str = "") -> dict:
    """Refresh the local snapshot of the authoritative backlog.

    Returns a summary; NEVER raises and never touches ``project_backlog``.
    Unbound projects are a no-op.
    """
    out: dict[str, Any] = {"bound": False, "fetched": 0, "cached": 0, "error": ""}
    org_id, project_id = binding(Path(project_root))
    if not org_id:
        return out
    out["bound"] = True
    # `hub_unavailable` was itself a collapse — it reported that the fetch
    # failed and never which of the four ways. The outcome box carries the
    # discriminated reason back, and it is PERSISTED so a later `cache_status`
    # in another process can still say why.
    _outcome: dict[str, Any] = {}
    items = fetch_backlog(org_id, project_id, _outcome, project_root=Path(project_root))
    stamp = now or _iso_now()
    if items is None:
        reason = str(_outcome.get("reason") or REASON_UNREACHABLE)
        out["error"] = reason
        out["remedy"] = REASON_REMEDY.get(reason, "")
        try:
            db = _db_path(Path(project_root))
            db.parent.mkdir(parents=True, exist_ok=True)
            with _canonical_connect(str(db), row_factory=False) as conn:
                _record_fetch_outcome(conn, project_id, reason, stamp)
                conn.commit()
        except Exception:  # noqa: BLE001 — recording WHY must not break the caller
            pass
        return out
    out["fetched"] = len(items)
    try:
        db = _db_path(Path(project_root))
        db.parent.mkdir(parents=True, exist_ok=True)
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_cache_table(conn)
            # Full replacement for THIS project: the server snapshot is the
            # truth, and a stale local cache row must not outlive it.
            conn.execute("DELETE FROM backlog_server_cache WHERE project_id = ?", (project_id,))
            for it in items:
                gid = str(it.get("globalId") or "").strip()
                if not gid:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO backlog_server_cache "
                    "(global_id, project_id, display_id, status, priority, updated_at,"
                    " payload, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        gid,
                        project_id,
                        it.get("displayId"),
                        str(it.get("status") or ""),
                        str(it.get("priority") or ""),
                        str(it.get("updatedAt") or ""),
                        json.dumps(it, sort_keys=True, ensure_ascii=False),
                        stamp,
                    ),
                )
                out["cached"] += 1
            _record_fetch_outcome(conn, project_id, "", stamp)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — a cache write never breaks a read
        # A LOCAL storage fault, and it must not read as a server one: the hub
        # answered fine, we failed to write it down. Different reason, different
        # repair.
        out["error"] = REASON_CACHE_ERROR
        out["remedy"] = REASON_REMEDY[REASON_CACHE_ERROR]
        out["detail"] = type(exc).__name__
    return out


def cached_updated_at(project_root: Path, global_id: str) -> str:
    """The server ``updatedAt`` we last saw for an item, or "".

    This is the correct BASE for an edit's optimistic-concurrency check: the
    intent means "I changed this based on the server state I last observed". If
    the server has moved on since, that is precisely the conflict we want
    surfaced. "" (never seen) ⇒ no base check, e.g. a freshly created item.
    """
    try:
        db = _db_path(Path(project_root))
        if not db.is_file():
            return ""
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_cache_table(conn)
            row = conn.execute(
                "SELECT updated_at FROM backlog_server_cache WHERE global_id = ?",
                (str(global_id),),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""
    except Exception:  # noqa: BLE001
        return ""


def server_read_authority(project_root: Path) -> bool:
    """Is the SERVER the authoritative answer for a read on this project?

    STEP 4 of the cutover (operator ruling 2026-08-30): "server becomes
    authoritative read truth ... authority flips by SEMANTICS, not destructive
    storage mutation." Nothing here mutates anything; this decides WHO ANSWERS.

    BOUND IS NOT THE PRECONDITION. A SUCCESSFUL FETCH IS, and the distinction is
    the whole safety of the flip. A project can be bound long before any fetch
    succeeds — offline, hub unreachable, credentials not yet stamped — and its
    cache is then EMPTY. Handing authority to an empty cache would answer "you
    have no backlog" to a project with a full one: silent, total, and
    indistinguishable from a genuinely empty project. So authority is claimed
    only once the server has actually been heard from.

    Fail-closed in the direction that PRESERVES the reader's data: anything
    unknown, unreadable or never-fetched leaves the LOCAL store answering, which
    is the behaviour that existed before this function and cannot lose rows.
    """
    try:
        _org, project_id = binding(Path(project_root))
        if not project_id:
            return False  # unbound: the local-only floor, untouched forever
        # "" is the success reason; every discriminated failure (never_fetched,
        # unreachable, auth_refused, cache_error, unconfigured) means the
        # snapshot is absent or stale and must NOT be treated as truth.
        return not _last_fetch_reason(Path(project_root))
    except Exception:  # noqa: BLE001 — an unanswerable question is NOT authority
        return False


@dataclass(frozen=True)
class MigrationDebt:
    """What we know about PRE-BINDING local rows — including "we cannot tell".

    Local backlog 985. The predecessor returned a bare `list[dict]`, so an
    unbound project, a missing DB, a missing table and an outright exception all
    answered `[]` — the identical value a fully-migrated project produces. That
    matters because emptiness is the PRECONDITION FOR RULING 6, the destructive
    step that demotes `project_backlog` to a projection and later cleans it. The
    live unregistered checkout answered `[]` while holding 841 unmigrated rows.

    WHY THE VALUE CARRIES THE HONESTY, not the caller (operator ruling
    2026-08-31). A boundness check bolted onto the destructive caller protects
    exactly the callers that remember it, and ruling 6 is not the only future
    reader of this list. Making the type unable to express "empty" without also
    expressing "known" moves the guarantee from discipline to structure.

    CONTRAST WITH `server_read_authority`, which folds every failure into one
    `False` and is RIGHT to. Its False means "the local store keeps answering",
    so unknowns land where nothing can be lost. Empty debt means "nothing is
    left to preserve", so unknowns land where things are DELETED. Same collapse,
    opposite blast radius.

    `reason` is why we CANNOT tell, and is empty exactly when `known` is True —
    "known, with a reason" is not a state.
    """

    known: bool
    reason: str = ""
    items: tuple[dict, ...] = ()

    def satisfies_ruling_6(self) -> bool:
        """May `project_backlog` be demoted/cleaned on this evidence?

        The conjunction lives HERE so no caller can get it half-right. An
        unknown state with no items is precisely the shape the old bug
        produced, and it must never read as permission.
        """
        return bool(self.known) and not self.items

    def as_payload(self) -> dict:
        """Tool-boundary shape. All three fields survive: a serialiser that
        dropped `known` would restore the defect at the surface while the value
        stayed honest underneath."""
        return {
            "known": bool(self.known),
            "reason": str(self.reason or ""),
            "items": [dict(i) for i in self.items],
        }


# Why we could not tell. Named rather than spelled inline at each exit, so a
# new failure mode cannot be added as a bare `return []` again.
DEBT_UNBOUND = "unbound_project"
DEBT_NO_STORE = "no_local_store"
DEBT_NO_TABLE = "no_local_table"
DEBT_READ_FAILED = "read_failed"


def legacy_local_unmigrated(project_root: Path) -> MigrationDebt:
    """PRE-BINDING local rows the server has never seen — MIGRATION DEBT.

    Operator ruling 3: such a row "must remain visible as explicit
    legacy_local_unmigrated / migration debt, not normal authoritative backlog
    and not silently hidden". Those are the two failure directions this bucket
    sits between, and both are real: counting it as authoritative would make
    this surface disagree with the server about how many items exist (the exact
    divergence the cutover removes), while hiding it would orphan history the
    flip promised not to destroy.

    NOT THE SAME AS A REFUSED WRITE. `local_unaccepted` (a3cf2ea43) holds
    intents the server REJECTED; this holds rows never OFFERED — they predate
    the binding, so they never entered `backlog_write_queue` at all. Collapsing
    the two would show one row in two buckets and ask the operator to rule on it
    twice, with different vocabularies.

    THIS LIST EMPTYING IS THE PRECONDITION FOR RULING 6: only once every local
    row is accounted for may `project_backlog` be demoted to a pure projection
    and later cleaned. So it is a work queue, not a warning.
    """
    try:
        from . import backlog_write_queue as _q

        _org, project_id = binding(Path(project_root))
        if not project_id:
            # UNBOUND, not clean. Nothing has ever been offered to a server, so
            # every local row is potentially debt — the opposite of what the
            # empty list this used to return would tell a destructive caller.
            return MigrationDebt(known=False, reason=DEBT_UNBOUND)
        queued = {
            str(r.get("globalId") or "")
            for r in (_q.conflicts(Path(project_root), project_id) or [])
        }
        db = _db_path(Path(project_root))
        if not db.is_file():
            # No store to read. Not evidence of a completed migration.
            return MigrationDebt(known=False, reason=DEBT_NO_STORE)
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_cache_table(conn)
            try:
                rows = conn.execute(
                    "SELECT b.global_id, b.status FROM project_backlog b "
                    "LEFT JOIN backlog_server_cache c "
                    "  ON c.global_id = b.global_id AND c.project_id = ? "
                    "WHERE c.global_id IS NULL ORDER BY b.global_id ASC",
                    (str(project_id),),
                ).fetchall()
            except sqlite3.Error:
                # This branch used to read "no local table: nothing to
                # migrate". That inference holds only if the table never
                # existed; it is equally consistent with a schema that moved or
                # a DB that failed to open, and neither is permission to delete.
                return MigrationDebt(known=False, reason=DEBT_NO_TABLE)
    except Exception:  # noqa: BLE001 — a debt view never breaks a read
        # Swallowing the error stays right. Reporting the swallow as an EMPTY
        # DEBT LIST is what made a crash indistinguishable from a clean
        # migration.
        return MigrationDebt(known=False, reason=DEBT_READ_FAILED)
    return MigrationDebt(
        known=True,
        items=tuple(
            {
                "globalId": str(gid),
                "status": str(status or ""),
                "reason": "never_offered_to_server",
            }
            for gid, status in rows
            if str(gid) and str(gid) not in queued
        ),
    )


def cache_status(project_root: Path) -> dict:
    """The observable convergence signal: local vs server counts + drift.

    This is the whole point of P0 — it makes the 74-vs-49 class of divergence
    VISIBLE before any write path changes hands.
    """
    out: dict[str, Any] = {
        "bound": False,
        "local_open": 0,
        "server_open": 0,
        "server_total": 0,
        "converged": None,
    }
    org_id, project_id = binding(Path(project_root))
    out["bound"] = bool(org_id)
    db = _db_path(Path(project_root))
    if not db.is_file():
        return out
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            _ensure_cache_table(conn)
            # The LOCAL count is queried independently: a project that has no
            # project_backlog table yet (fresh clone, cache-first bootstrap) must
            # still get accurate SERVER numbers. Folding both into one try let a
            # missing local table zero out the server side and report a
            # misleading "0 vs 0" — found by the first live run against dev,
            # which the unit tests missed because they always seeded the table.
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM project_backlog WHERE status = 'open'"
                ).fetchone()
                out["local_open"] = int(row[0]) if row else 0
            except sqlite3.Error:
                out["local_open"] = 0
                out["local_missing"] = True
            if out["bound"]:
                r2 = conn.execute(
                    "SELECT COUNT(*) FROM backlog_server_cache "
                    "WHERE project_id = ? AND status = 'open'",
                    (project_id,),
                ).fetchone()
                out["server_open"] = int(r2[0]) if r2 else 0
                r3 = conn.execute(
                    "SELECT COUNT(*) FROM backlog_server_cache WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                out["server_total"] = int(r3[0]) if r3 else 0
                last_reason, ever = _last_fetch_reason(conn, project_id)
                # CONVERGENCE IS ONLY MEANINGFUL AFTER A SUCCESSFUL FETCH.
                # This used to compute `local_open == server_open`
                # unconditionally once bound — so a project that had never
                # fetched reported converged=False off an empty cache (302 vs 0)
                # and CLAIMED DIVERGENCE where the truth was "never compared".
                # A false negative is worse than the honest unknown it replaced.
                if ever and not last_reason:
                    out["converged"] = out["local_open"] == out["server_open"]
                else:
                    out["unknown_reason"] = last_reason or (
                        REASON_NEVER_FETCHED if ever is False else REASON_UNREACHABLE
                    )
            else:
                out["unknown_reason"] = _unbound_reason(Path(project_root))
    except Exception:  # noqa: BLE001 — status is diagnostic; never raises
        return out
    if out.get("converged") is None and out.get("unknown_reason"):
        out["remedy"] = REASON_REMEDY.get(str(out["unknown_reason"]), "")
    return out


def _unbound_reason(project_root: Path) -> str:
    """Unbound for WHICH reason — the context, the row's org, or the credentials.

    OPERATOR, 2026-08-30: "missing env vars do not inherently make binding()
    empty ... It explains is_bound=False only if registration also lacks
    org_id." So the causes are ORDERED, and reporting them as one would send the
    reader to the wrong repair.

    #972 ADDS A THIRD CASE, and it exists because the second one became a LIE on
    the gate. ``REASON_UNCONFIGURED`` means "``org_from_identity`` was the
    fallback and it needs the S2S pair" — true on a local box, false on the gate,
    where ``binding()`` no longer consults the machine login at all (a gate org
    comes from the authenticated selection or from nowhere). Emitting it there
    would name a remedy that changes nothing: two env vars for a code path that
    is not reached. So a gate call with a selected project whose row carries no
    org says exactly that instead — the repair is to stamp the project's org,
    not to touch credentials.
    """
    reg_org, reg_pid = registered_binding(project_root)
    pid = _override("sync.vps_hub_project_id", project_root) or reg_pid
    if not pid:
        return REASON_UNBOUND  # binding() returns here; env is never reached
    org = _override("sync.hub_org_id", project_root) or reg_org
    if org:
        return REASON_UNBOUND  # a pid and an org, yet unbound: not a config gap
    if _on_gate_surface():
        # The selection resolved a project; the row simply has no org
        # (GateProjectStore.register defaults org_id=""). Nothing about
        # credentials is in question.
        return REASON_SELECTION_NO_ORG
    # A registration exists but carries no org, so `org_from_identity` was the
    # fallback — and THAT is what needs the S2S credentials.
    base = os.environ.get(_INTERNAL_URL_ENV, "").strip()
    secret = os.environ.get(_INTERNAL_SECRET_ENV, "").strip()
    return REASON_UNBOUND if (base and secret) else REASON_UNCONFIGURED


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
