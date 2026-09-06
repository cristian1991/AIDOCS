"""VPS sync authority — client half of the #442 sanctioned build (WAR H).

The codenexus VPS (https://mcp.codenexus.cloud) is the AUTHORITATIVE sync hub:
the SERVER authorizes the actor (identity spine #360, RBAC union #283) and
assigns canonical order, so a forged high-HLC file can never win. This module
implements the CLIENT side on the EXISTING ``SyncTransport`` seam (§XXII
extend-don't-fork — ``sync_store.py``'s ABC anticipated exactly this impl):

* ``VpsApiTransport(SyncTransport)`` — wraps the git outbox (local-first) and
  adds ``submit_events`` / ``pull_events`` against the VPS endpoint contract.
* ``GitEventTransport`` DEMOTES to an offline OUTBOX: local writes still land
  as immutable event files (offline floor: no VPS ⇒ everything works exactly
  as today); on reconcile they are submitted to the VPS for re-authorization.
  Unreceipted files are NEVER folded authoritative on their own — the
  receipted-only fold in ``sync_store``/store hydrate is UNTOUCHED.
* ``reconcile_outbox`` — best-effort background reconcile: submit the outbox,
  record a receipt (via the EXISTING ``record_receipt`` — ONE authority
  ledger, no fork) for each event the server accepts, pull server-ordered
  remote events, and let the normal receipted-only fold apply them. It never
  raises and never blocks a read/write.

ENDPOINT CONTRACT (client codes against this; the test double implements it)
────────────────────────────────────────────────────────────────────────────
POST {base}/sync/events
  Auth: ``Authorization: Bearer <token>`` (a gate credential; the SERVER maps
  token → principal → org/seat and authorizes per event — file-asserted
  ``actor`` is a CLAIM the server verifies, never trusts).
  Body: {"project_id": str, "events": [SyncEvent-shaped dicts]}
  200:  {"receipts": [{"event_id","stream","entity_id","server_hlc"}...],
         "rejected": [{"event_id","reason"}...]}
  Semantics: idempotent on event_id (re-submitting an accepted event returns
  the SAME receipt); each accepted event gets a server-assigned canonical
  position (server_hlc); rejection reasons include "unauthorized_actor",
  "tenant_mismatch", "malformed_event".

GET {base}/sync/events?since=<cursor>&project_id=<id>
  200:  {"events": [SyncEvent dicts, server-ordered, project_id stamped],
         "cursor": <opaque next-cursor>}
  Semantics: returns ONLY events of the authenticated principal's authorized
  project; ``since`` is the opaque cursor from the previous pull ("" = from
  the beginning); server HLC/order wins — the client observes it.

SERVER HALF — SHIPPED (verified 2026-07-25). THE "STOPPED" NOTE BELOW IS HISTORY
────────────────────────────────────────────────────────────────────────────
The server half described further down as "STOPPED / not shipped here" HAS
SINCE LANDED, in ``outer_gate_transport.py``:

  * ``RC_SYNC_EVENTS`` route class (:126) with its ``RefusalEntry`` (:172)
  * ``classify`` admits POST/GET on the sync route (:255) — every other verb
    still falls through to ``RC_REFUSED_MUTATION``, so deny-by-default holds
  * scope binding ``RC_SYNC_EVENTS -> {SCOPE_SYNC}`` (:391)
  * handler ``_ogt_sync_events`` (:5161), dispatched at :5550

Client-side reconcile is wired into every ``sync_once``
(``backlog_sync_sitter.py:215``).

WHY THIS PARAGRAPH EXISTS: the stale "STOPPED" text below made this war read
as though its whole server half were unbuilt, which is how #445 kept being
sized as a fable-class war when its ACTUAL remaining gaps are narrow: (1) no
ON-LOGIN resync trigger — triggers are ``poll`` and ``mutation`` only
(``outer_gate_transport.py`` :391/:393), and the sanctioned build calls for
resync ON LOGIN plus reactive on cross-device change; (2)
``sync.vps_hub_enabled`` still defaults OFF (:301). A docstring that
understates what shipped costs real re-investigation every time someone picks
the item up — that is why it is corrected here rather than deleted.

The spec text is KEPT VERBATIM below because it remains the accurate
description of the contract the handler implements, and the local double in
``mcp/tests/memory/test_vps_api_transport.py`` still implements those exact
semantics as an executable reference.

HISTORICAL STOP EVIDENCE (superseded — kept for the reasoning, not the status)
────────────────────────────────────────────────────────────────────────────
STOP evidence: ``outer_gate_transport.classify`` is a closed deny-by-default
route set (health/openapi/webapp/oauth/tools/mcp/invoke; everything else 404
or refused), and authorization resolves seats against the CodeNexus postgres
(``_CODENEXUS_DSN_ENV`` / ``_codenexus_authenticate`` / ``_codenexus_resolve_seat``).
Mounting ``/sync/events`` therefore requires CodeNexus-side decisions this
repo cannot see: (1) WHERE the VPS canonically stores the per-project event
log + receipt ledger (a postgres table vs the per-project store DB on the
VPS); (2) WHICH RBAC scope class sync ingest falls under; (3) the
token→org→project tenancy binding used to refuse cross-tenant submits
server-side (§XXII). Per the war brief, the server half ships as this spec:

  * Route: add ``RC_SYNC`` to ``classify`` — POST/GET ``/{API}/sync/events``
    (POST=submit, GET=pull); both require an authenticated principal
    (fail-closed, same bearer resolution as tool invoke) + a ``sync.events``
    scope; classify stays pure/deny-by-default for every other verb.
  * Handler (``handle_sync_tool``-shaped, next to ``handle_project_tool``):
    resolve principal → seat → authorized project set; REFUSE any event whose
    ``project_id`` is not in that set (tenancy floor, server-enforced);
    verify the event's ``actor`` against the principal (server authorizes the
    actor — a self-asserted actor in the file body is ignored); assign
    ``server_hlc`` from the server's own HybridLogicalClock (canonical
    order); INSERT OR IGNORE by event_id (replay floor); record the receipt
    in the server's ledger; audit via ``default_transport_audit``.
  * Pull: SELECT events WHERE project_id = authorized AND seq > since
    ORDER BY seq; return an opaque seq cursor.

The local double in ``mcp/tests/memory/test_vps_api_transport.py`` implements
these exact semantics, so the server handler has an executable reference.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from .sync_store import (
    _PROCESS_CLOCK,
    STREAMS,
    GitEventTransport,
    SyncEvent,
    SyncTransport,
    record_quarantine,
    record_receipt,
)

DEFAULT_BASE_URL = "https://mcp.codenexus.cloud"


class VpsSyncError(RuntimeError):
    """The VPS was unreachable or answered outside the contract. Callers on the
    best-effort paths (append / reconcile_outbox) swallow this — local-first is
    a hard floor, the hub is never a hard dependency.

    CARRIES THE HTTP STATUS AND THE GATE'S REFUSAL WORD. Before this, a 403
    from the gate collapsed into the bare class name and nothing upstream
    could tell "the token lacks the sync scope" from "no route to host" — so
    the same token was re-presented on every poll and the operator saw only
    ``VpsSyncError``. ``status`` is 0 when no HTTP answer arrived;
    ``refusal`` is the body's ``error`` word ("" when absent).
    """

    def __init__(self, message: str, *, status: int = 0, refusal: str = "") -> None:
        super().__init__(message)
        self.status = int(status or 0)
        self.refusal = str(refusal or "")


def _refusal_word(raw: bytes | str | None) -> str:
    """The ``error`` word from a gate refusal body, or "". Never raises."""
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    return str(data.get("error") or "").strip() if isinstance(data, dict) else ""


@dataclass(frozen=True)
class SyncReceipt:
    """One server verdict for one submitted event. ``accepted`` ⇒ the server
    authorized the actor + tenant and assigned ``server_hlc`` (canonical
    order); the client has recorded the local receipt. Not accepted ⇒ the
    event stays unreceipted (quarantined by the normal fold) with ``reason``."""

    event_id: str
    stream: str
    entity_id: str
    accepted: bool
    server_hlc: str = ""
    reason: str = ""


class VpsApiTransport(SyncTransport):
    """SyncTransport impl where the VPS is the authority and the git event
    files are the offline OUTBOX.

    * ``append`` writes the local outbox FIRST (write-once, exactly as today —
      the offline floor), then best-effort submits to the VPS; a dead VPS
      never fails or delays the local write.
    * ``read`` reads the local outbox only — reads never touch the network.
    * ``http`` is the injectable channel: ``http(method, url, body_dict|None)
      -> (status:int, body:dict)``. Tests inject a local double; production
      uses the urllib default with a bearer token.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        project_id: str = "",
        http=None,
        timeout: float = 10.0,
    ) -> None:
        self._root = Path(project_root)
        self._outbox = GitEventTransport(self._root)
        self._base = str(base_url).rstrip("/")
        self._token = token
        self._project_id = str(project_id)
        self._http = http or self._urllib_http
        self._timeout = timeout

    # ── SyncTransport seam (local-first) ────────────────────────────────────
    def append(self, event: SyncEvent) -> None:
        self._outbox.append(event)  # the outbox write is the contract; never skipped
        try:
            self.submit_events([event])
        except Exception:
            pass  # offline/unauthorized ⇒ stays queued; reconcile_outbox retries

    def read(self, stream: str) -> list[SyncEvent]:
        return self._outbox.read(stream)

    # ── HTTP channel (injectable) ───────────────────────────────────────────
    def _urllib_http(self, method: str, url: str, body: dict | None):
        import urllib.request

        # In-process egress law (#195): every network call-site gates through
        # governed_egress at runtime — fail-closed to the sync hub's host only.
        from urllib.parse import urlparse as _urlparse

        from .governed_egress import assert_egress_allowed

        assert_egress_allowed(
            url,
            purpose="vps_sync",
            allow_hosts=[_urlparse(self._base).hostname or ""],
        )
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "aidocs-sync"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — https hub URL
            return resp.status, json.loads(resp.read().decode("utf-8"))

    # ── VPS authority calls ─────────────────────────────────────────────────
    def submit_events(self, batch: list[SyncEvent]) -> list[SyncReceipt]:
        """POST the batch for server authorization. Each event the SERVER
        accepts gets a local receipt via the EXISTING ``record_receipt`` (one
        authority ledger); the local clock observes the server HLC so later
        local events sort after the canonical order. Rejected events get NO
        receipt — the receipted-only fold keeps quarantining them exactly as
        today. Raises VpsSyncError when the hub is unreachable/off-contract."""
        if not batch:
            return []
        by_id = {e.event_id: e for e in batch}
        payload = {
            "project_id": self._project_id,
            "events": [asdict(e) for e in batch],
        }
        status, body = self._call("POST", f"{self._base}/sync/events", payload)
        if status != 200 or not isinstance(body, dict):
            raise VpsSyncError(
                f"submit_events: HTTP {status}",
                status=status,
                refusal=str(body.get("error") or "") if isinstance(body, dict) else "",
            )
        out: list[SyncReceipt] = []
        for r in body.get("receipts", []) or []:
            eid = str(r.get("event_id", ""))
            ev = by_id.get(eid)
            if ev is None:
                continue  # a receipt for an event we did not submit is off-contract noise
            rec = SyncReceipt(
                event_id=eid,
                stream=str(r.get("stream", ev.stream)),
                entity_id=str(r.get("entity_id", ev.entity_id)),
                accepted=True,
                server_hlc=str(r.get("server_hlc", "")),
            )
            record_receipt(self._root, rec.stream, rec.event_id, rec.entity_id)
            if rec.server_hlc:
                _PROCESS_CLOCK.observe(rec.server_hlc)  # server HLC wins
            out.append(rec)
        for r in body.get("rejected", []) or []:
            eid = str(r.get("event_id", ""))
            ev = by_id.get(eid)
            if ev is None:
                continue
            out.append(
                SyncReceipt(
                    event_id=eid,
                    stream=ev.stream,
                    entity_id=ev.entity_id,
                    accepted=False,
                    reason=str(r.get("reason", "rejected")),
                )
            )
        return out

    def pull_events(self, since: str | None = None) -> list[SyncEvent]:
        """GET server-ordered events after ``since`` (None ⇒ the durable local
        cursor). Applied events land in the outbox (write-once ⇒ re-pull is
        idempotent), get a receipt (server-authorized ⇒ authoritative), and
        the local clock observes their HLC. TENANCY FLOOR: an event whose
        ``project_id`` does not match this client's project is REFUSED —
        never written, never receipted — and logged to the quarantine ledger.
        Returns the applied events; the normal fold materializes them."""
        cursor = self._read_cursor() if since is None else str(since)
        url = (
            f"{self._base}/sync/events"
            f"?since={quote(cursor)}&project_id={quote(self._project_id)}"
        )
        status, body = self._call("GET", url, None)
        if status != 200 or not isinstance(body, dict):
            raise VpsSyncError(
                f"pull_events: HTTP {status}",
                status=status,
                refusal=str(body.get("error") or "") if isinstance(body, dict) else "",
            )
        applied: list[SyncEvent] = []
        foreign: list[SyncEvent] = []
        for d in body.get("events", []) or []:
            try:
                ev = SyncEvent(**d)
                ev.validate()
            except (TypeError, ValueError):
                continue  # off-contract event never breaks the pull
            if ev.project_id != self._project_id:
                foreign.append(ev)  # tenancy refusal — detectable, never applied
                continue
            self._outbox.append(ev)  # write-once ⇒ replay-idempotent
            record_receipt(self._root, ev.stream, ev.event_id, ev.entity_id)
            _PROCESS_CLOCK.observe(ev.hlc)  # server order wins locally
            applied.append(ev)
        for stream in STREAMS:
            bad = [e for e in foreign if e.stream == stream]
            if bad:
                record_quarantine(self._root, stream, bad)
        new_cursor = body.get("cursor")
        if since is None and isinstance(new_cursor, str) and new_cursor:
            self._write_cursor(new_cursor)
        return applied

    def _call(self, method: str, url: str, body: dict | None):
        try:
            return self._http(method, url, body)
        except VpsSyncError:
            raise
        except urllib.error.HTTPError as exc:
            # urlopen RAISES on 4xx/5xx. The status and the gate's refusal
            # word are the whole diagnosis; losing them here is what made a
            # scope refusal look like a network fault.
            try:
                raw = exc.read() or b""
            except Exception:  # noqa: BLE001 — a body-less refusal is still one
                raw = b""
            code = int(getattr(exc, "code", 0) or 0)
            raise VpsSyncError(
                f"{method} {url}: HTTP {code}", status=code, refusal=_refusal_word(raw)
            ) from exc
        except Exception as exc:  # network/parse ⇒ one typed, swallowable error
            raise VpsSyncError(f"{method} {url}: {type(exc).__name__}: {exc}") from exc

    # ── durable pull cursor ─────────────────────────────────────────────────
    def _cursor_path(self) -> Path:
        return self._root / ".MEMORY" / "sync" / ".vps_cursor"

    def _read_cursor(self) -> str:
        try:
            p = self._cursor_path()
            return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
        except OSError:
            return ""

    def _write_cursor(self, cursor: str) -> None:
        try:
            p = self._cursor_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(cursor), encoding="utf-8")
        except OSError:
            pass  # cursor loss only means a wider (idempotent) re-pull


def reconcile_outbox(
    project_root: Path, transport: VpsApiTransport, *, clean_start_hlc: str = ""
) -> dict:
    """Best-effort background reconcile of the git outbox against the VPS.

    Walks EVERY local event file (receipted local history is re-submitted too
    — the server is idempotent on event_id, so the hub converges to a superset
    of every device's outbox; unreceipted files — offline-arrived or forged —
    are submitted for RE-AUTHORIZATION): the server accepts ⇒ ``record_receipt``
    lands and the NORMAL fold applies them on the next hydrate; the server
    rejects ⇒ no receipt, the event stays quarantined exactly as today. Then
    pulls server-ordered remote events. NEVER raises and mutates nothing on
    failure — no VPS means local behavior is byte-identical to today (hard
    floor: local-first offline)."""
    summary = {
        "ok": True,
        "submitted": 0,
        "accepted": 0,
        "rejected": 0,
        "pulled": 0,
        "skipped_pre_clean_start": 0,
        "error": "",
        # The HTTP status behind a failure (0 = none) and the gate's refusal
        # word ("" = none), so the caller can FILE a 401 and NAME a 403.
        "status": 0,
        "refusal": "",
    }
    root = Path(project_root)
    try:
        outbox = GitEventTransport(root)
        for stream in STREAMS:
            events = outbox.read(stream)
            if clean_start_hlc:
                # START CLEAN: never re-submit history from before enablement.
                # HLCs are zero-padded "digits:digits", so lexical > is correct.
                _before = len(events)
                events = [e for e in events if e.hlc > clean_start_hlc]
                summary["skipped_pre_clean_start"] += _before - len(events)
            if not events:
                continue
            summary["submitted"] += len(events)
            for rec in transport.submit_events(events):
                if rec.accepted:
                    summary["accepted"] += 1
                else:
                    summary["rejected"] += 1
        summary["pulled"] = len(transport.pull_events())
    except Exception as exc:
        return {
            **summary,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "status": int(getattr(exc, "status", 0) or 0),
            "refusal": str(getattr(exc, "refusal", "") or ""),
        }
    return summary
