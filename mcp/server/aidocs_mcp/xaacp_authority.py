"""Cross-surface XAACP authority adapter.

Local/unbound projects keep ``conductor_comms`` SQLite as their authority.
A cloud-bound project must not fork message truth by checkout root, so an
AIDOCS edge forwards the existing ``ai_msg`` contract to the authenticated
MCP gate. The gate executes the SAME XAACP implementation on its canonical
project copy; this module adds transport, not a second state machine.

The model never supplies transport identity or credentials. ``hostSession`` /
``hostKind`` are read from the current AIDOCS request context and carried in
MCP ``_meta``; the gate composes them with the authenticated principal just as
it already does for ``openai/session``. Project/session claims are constraints
only. This adapter first resolves WHICH SESSION from the caller's own managed
binding (never the gate's per-user selection, #1001), reads the gate's
authoritative selected project, and refuses any disagreement before calling
``ai_msg``.
"""
from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEFAULT_GATE_URL = "https://mcp.codenexus.cloud"


def _error(status: str, detail: str, **extra: Any) -> dict:
    return {"ok": False, "status": status, "error": detail, **extra}


class RemoteMcpXaacpAuthority:
    """Forward XAACP through the canonical gate-side ``ai_msg`` implementation."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        project_id: str,
        http: Callable[[dict, dict[str, str]], dict] | None = None,
        timeout: float = 30.0,
    ) -> None:
        base = str(base_url or DEFAULT_GATE_URL).rstrip("/")
        self._url = base if base.endswith("/v1/mcp") else base + "/v1/mcp"
        self._token = str(token or "").strip()
        self._project_id = str(project_id or "").strip()
        self._http = http or self._urllib_call
        self._timeout = float(timeout)

    def _urllib_call(self, rpc: dict, headers: dict[str, str]) -> dict:
        from .governed_egress import assert_egress_allowed

        host = urllib.parse.urlparse(self._url).hostname or ""
        assert_egress_allowed(self._url, purpose="xaacp_authority", allow_hosts=[host])
        req = urllib.request.Request(
            self._url,
            data=json.dumps(rpc, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 governed above
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _result_payload(reply: dict) -> dict:
        if not isinstance(reply, dict):
            return _error("unavailable", "xaacp hub returned a non-object response")
        if isinstance(reply.get("error"), dict):
            err = reply["error"]
            data = err.get("data")
            if isinstance(data, dict):
                return {"ok": False, "status": "refused", "error": str(err.get("message") or "gate refusal"), **data}
            # #1019: A NON-DICT `data` IS THE DETAIL, AND IT USED TO BE THROWN
            # AWAY. `message or data` means that whenever the gate sent BOTH a
            # short code and a sentence, the sentence lost -- so an
            # insufficient_scope refusal reached the operator as the bare word
            # "insufficient_scope" while the gate had actually said WHICH scope
            # ("token lacks xaacp_write scope", built by _oge_scope_edit).
            # Measured 2026-09-04: diagnosing one such refusal took a bundle
            # rebuild and a source trace to learn what the gate had already
            # said. The code stays the `error` (callers match on it); the
            # sentence is carried alongside as `detail` instead of being
            # dropped. Same absence-vs-negative collapse as #997, one layer up.
            detail = str(data).strip() if data not in (None, "") else ""
            message = str(err.get("message") or "").strip()
            # NB `_error`'s second positional IS the `error` field, so the
            # sentence rides beside it under its own name rather than through
            # that slot.
            if message and detail and detail != message:
                return {
                    "ok": False,
                    "status": "refused",
                    "error": message,
                    "detail": detail,
                }
            return _error("refused", message or detail or "gate refusal")
        try:
            text = reply["result"]["content"][0]["text"]
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else _error("unavailable", "xaacp hub returned a non-object tool result")
        except Exception:
            result = reply.get("result")
            return result if isinstance(result, dict) else _error("unavailable", "xaacp hub returned an unreadable tool result")

    def _tool_call(self, name: str, args: dict, meta: dict[str, str]) -> dict:
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args, "_meta": meta},
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "aidocs-xaacp-edge",
        }
        # FILE THE AUTHORITY'S VERDICT (#1000/#992). `record_gate_answer` is,
        # by its own docstring, "the single door every cloud-facing caller uses
        # after a live contact" — and XAACP was not using it. The #1000 spec
        # left this as a follow-up and it was never wired.
        #
        # THIS IS NOT OBSERVABILITY. A token the gate REJECTS BEFORE its local
        # expiry left no trace, so `cached_gate_credential` kept calling it
        # usable and passive inbox polling re-presented it on every subsequent
        # tool call. That is precisely the retry shape #992 exists to prevent,
        # and precisely what earns the four-hour CrowdSec ban: seven 401s trip
        # `http-generic-401-bf`, including the sign-in that would have fixed it.
        # Latching turns an unbounded retry into exactly one.
        #
        # 200 stamps, 401/403 latch, EVERYTHING ELSE FILES NOTHING — a 429, a
        # 5xx or a timeout says nothing about the credential, and latching on
        # one would lock the operator out on the gate's own rate limit. That
        # discrimination lives in record_gate_answer; this call site only
        # reports the status honestly.
        from .operator_token_resolution import record_gate_answer

        try:
            reply = self._http(rpc, headers)
        except urllib.error.HTTPError as exc:
            request_id = ""
            try:
                request_id = str(exc.headers.get("X-Request-Id") or "")
            except Exception:  # noqa: BLE001 -- a missing header must not mask the verdict
                request_id = ""
            with contextlib.suppress(Exception):
                record_gate_answer(status=int(exc.code), request_id=request_id)
            if exc.code in (401, 403):
                # NAME THE CAUSE. "HTTP 401" reads as a transport hiccup; this
                # is an authorization verdict, and the remedy is a sign-in.
                return _error(
                    "unavailable",
                    f"xaacp hub rejected this machine's gate credential "
                    f"(HTTP {exc.code}); it is latched, so no further request "
                    f"will be spent on it. Sign in again from the Dashboard.",
                )
            return _error("unavailable", f"xaacp hub HTTP {exc.code}")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            return _error("unavailable", f"xaacp hub unreachable: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 -- remote authority must fail closed, never fork local
            return _error("unavailable", f"xaacp hub failed: {type(exc).__name__}")
        # urlopen raises on every non-2xx, so reaching here IS the 200 answer.
        with contextlib.suppress(Exception):
            record_gate_answer(status=200)
        return self._result_payload(reply)

    def dispatch(self, project_root: Path, **payload: Any) -> dict:
        from .mcp_server_runtime_helpers import (
            current_calling_agent_id,
            current_calling_host_kind,
            current_calling_host_session_id,
        )
        from .webmcp_identity import (
            META_AIDOCS_HOST_AGENT,
            META_AIDOCS_HOST_KIND,
            META_AIDOCS_HOST_SESSION,
            META_AIDOCS_PROJECT_ID,
            META_AIDOCS_SESSION_ID,
        )

        hsid = str(current_calling_host_session_id() or "").strip()
        hkind = str(current_calling_host_kind() or "").strip()
        hagent = str(current_calling_agent_id() or "").strip()
        requested_session = str(payload.get("session_id") or "").strip()
        bridge_confirm = str(payload.get("confirm_token") or "").strip()
        if not hsid or not hkind or hkind == "unknown":
            return _error("forbidden", "caller has no canonical host identity for XAACP forwarding")
        if not requested_session:
            return _error("invalid", "XAACP requires explicit session_id")

        # WHICH SESSION COMES FROM THE CALLER'S MANAGED BINDING (#1001, empire
        # law promoted-cc6c4ac686ee, 2026-09-03): "Managed binding proves WHICH
        # AIDOCS SESSION that caller selected ... no layer is allowed to invent,
        # inherit, heal, or substitute identity from a broader layer."
        #
        # THE DEFECT THIS REPLACES. This adapter asked the gate
        # `ai_session(mode='status')` and compared the answer against the
        # request. That answer is the TOKEN'S per-user selected session -- a
        # broader layer than the caller -- so after a fresh Dashboard login a
        # window bound to 'ubermega' was refused its own session with
        # bound_session_id='redteam' while `ai_session(status)` on the same
        # window said ubermega. Two surfaces, one caller, two answers.
        #
        # The gate's selection is now NEVER consulted here. A binding decides;
        # no binding is an honest refusal naming the missing binding and its
        # remedy, never a substituted selection. The gate side must apply the
        # same rule to its own copy (spec: .MEMORY/sessions/ubermega/
        # SPEC-1001-xaacp-session-from-binding.md).
        from .managed_mode_service import ManagedModeService, resolve_managed_session

        try:
            bound_session = resolve_managed_session(
                ManagedModeService(), project_root, host_session_id=hsid
            )
        except Exception as exc:  # noqa: BLE001 -- an unreadable binding is not a pass
            return _error(
                "forbidden",
                f"caller's managed binding could not be read: {type(exc).__name__}",
                host_session_id=hsid,
            )
        if not bound_session:
            return _error(
                "forbidden",
                "caller has no managed binding for this host session; XAACP "
                "resolves WHICH SESSION from that binding and never from the "
                "gate's per-user selection",
                host_session_id=hsid,
                missing_binding_host_session_id=hsid,
                remedy=f"ai_session(mode='connect', session_id='{requested_session}')",
            )
        if bound_session != requested_session:
            return _error(
                "forbidden",
                "XAACP session_id does not match the caller's managed binding",
                bound_session_id=bound_session,
                host_session_id=hsid,
                resolved_via="managed_binding",
            )

        meta = {
            META_AIDOCS_HOST_SESSION: hsid,
            META_AIDOCS_HOST_KIND: hkind,
            META_AIDOCS_PROJECT_ID: self._project_id,
            META_AIDOCS_SESSION_ID: requested_session,
        }
        # #1007: WHO, one layer finer. A subagent inherits `hsid` from its
        # parent, so without this key the gate could only ever re-derive the
        # conductor and every subagent's directory row collapsed onto its
        # parent's actor. The key is OMITTED, never sent as "", because
        # set_request_host_identity stores blank as absent: an empty string on
        # the wire would be a third spelling of "no subagent" for the gate to
        # get wrong.
        if hagent:
            meta[META_AIDOCS_HOST_AGENT] = hagent

        # Project is server authority. The caller's value only constrains the
        # request; disagreement refuses before any message read/write.
        pstat = self._tool_call("ai_project", {"mode": "status"}, meta)
        # A FAILED PREFLIGHT IS NOT A PROJECT DISAGREEMENT.
        #
        # This is #997's defect, still alive one call up. When the preflight
        # fails — an unreachable hub, a rejected credential, a refusal — the
        # reply carries no "project" key, `selected_project` falls to "", and
        # the branch below announced "gate project selection does not match
        # this local project's binding". The operator was then told to go and
        # reconcile a project binding that was never wrong, while the real
        # cause (auth, transport) went unnamed. An absence read as a NEGATIVE:
        # the same collapse #997 fixed for the credential read.
        #
        # The failure is PROPAGATED verbatim instead. It already carries the
        # status, the error and — since #1019 — the gate's own sentence as
        # `detail`, so the caller learns what actually happened.
        # EXPLICIT FAILURE ONLY. The first cut demanded `pstat.get("ok")` be
        # truthy, which is wrong in the other direction: a successful
        # `ai_project` reply need not carry an `ok` key at all, so every
        # healthy preflight was read as a failure. Absence is not a negative —
        # the same mistake this branch exists to fix, made while fixing it.
        # `_error` always sets ok=False, so a genuine failure is unambiguous.
        if isinstance(pstat, dict) and pstat.get("ok") is False:
            return dict(pstat)
        selected_project = str((pstat.get("project") or {}).get("project_id") or "") if isinstance(pstat.get("project"), dict) else ""
        if not selected_project or selected_project != self._project_id:
            return _error(
                "forbidden",
                "XAACP gate project selection does not match this local project's binding",
                bound_project_id=self._project_id,
                selected_project_id=selected_project,
            )

        args = {
            "mode": payload.get("mode", ""),
            "session_id": requested_session,
            "target_actor_id": payload.get("target_actor_id", ""),
            "lane_id": payload.get("lane_id", ""),
            "message_kind": payload.get("message_kind", ""),
            "body": payload.get("body", ""),
            "message_id": payload.get("message_id", ""),
            "correlation_id": payload.get("correlation_id", ""),
            "in_reply_to": payload.get("reply_to_id", ""),
            "decision": payload.get("decision", ""),
            "timeout_seconds": payload.get("timeout_seconds", 0.0),
            "unread_only": payload.get("unread_only", True),
            "mark_read": payload.get("mark_read", False),
            "limit": payload.get("limit", 50),
            "wake": payload.get("wake", False),
            "metadata": payload.get("metadata"),
            "ttl_seconds": payload.get("ttl_seconds"),
            "reason": payload.get("reason", ""),
        }
        out = self._tool_call("ai_msg", args, meta)
        diagnostic = json.dumps(out, sort_keys=True, default=str).lower()
        needs_bind = (
            "managed_mode_inactive" in diagnostic
            or "no canonical xaacp actor/session binding" in diagnostic
        )
        if not needs_bind:
            return out

        # Binding this caller's gate-side actor to ITS OWN BOUND SESSION
        # (`requested_session` == the local managed binding, proven above) is
        # explicit operator consent. Reuse ai_session(connect)'s existing
        # two-phase handle; never invent a bridge-only confirmation mechanism.
        # This is a BIND of the caller, not a read of the gate's selection.
        connect_args = {"mode": "connect", "session_id": requested_session}
        if bridge_confirm:
            connect_args["confirm_token"] = bridge_confirm
        bound = self._tool_call("ai_session", connect_args, meta)
        if bound.get("confirmed") is True and str(bound.get("session_id") or "") == requested_session:
            return self._tool_call("ai_msg", args, meta)
        if str(bound.get("_error") or "") == "confirm_required" and bound.get("confirm_token"):
            return {
                "ok": False,
                "status": "confirm_required",
                "error": "XAACP remote actor must be bound to the caller's bound session on the gate before cross-surface messaging",
                "action": "xaacp_bridge_connect",
                "session_id": requested_session,
                "confirm_token": bound.get("confirm_token"),
                "summary": bound.get("summary", ""),
            }
        return {
            "ok": False,
            "status": "forbidden",
            "error": "XAACP remote actor session bind failed",
            "bind_result": bound,
        }



    def claim_seat(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str,
        confirm_token: str = "",
    ) -> dict:
        """Claim a conductor/co-conductor seat through the gate's ai_seat door.

        The public messaging surface never exposes a seat-claim mode. We first
        establish the same authenticated actor/session binding XAACP messaging
        uses, then invoke the gate's existing seat lifecycle tool.
        """
        seat = str(role or "").strip().lower()
        if seat not in {"conductor", "co_conductor"}:
            return _error("invalid", "remote XAACP seat claims are conductor/co_conductor only")
        sid = str(session_id or "").strip()
        ready = self.dispatch(
            project_root,
            mode="xaacp_directory",
            session_id=sid,
            confirm_token=str(confirm_token or ""),
        )
        if not ready.get("ok"):
            return ready

        from .mcp_server_runtime_helpers import (
            current_calling_host_kind,
            current_calling_host_session_id,
        )
        from .webmcp_identity import (
            META_AIDOCS_HOST_KIND,
            META_AIDOCS_HOST_SESSION,
            META_AIDOCS_PROJECT_ID,
            META_AIDOCS_SESSION_ID,
        )

        hsid = str(current_calling_host_session_id() or "").strip()
        hkind = str(current_calling_host_kind() or "").strip()
        if not hsid or not hkind or hkind == "unknown":
            return _error("forbidden", "caller has no canonical host identity for remote seat claim")
        meta = {
            META_AIDOCS_HOST_SESSION: hsid,
            META_AIDOCS_HOST_KIND: hkind,
            META_AIDOCS_PROJECT_ID: self._project_id,
            META_AIDOCS_SESSION_ID: sid,
        }
        args = {
            "mode": "enter" if seat == "conductor" else "co-enter",
            "session_id": sid,
        }
        if confirm_token:
            args["confirm_token"] = str(confirm_token)
        out = self._tool_call("ai_seat", args, meta)
        if not isinstance(out, dict):
            return _error("unavailable", "gate ai_seat returned a non-object")
        if out.get("error") or out.get("status") in {"forbidden", "occupied", "unavailable", "invalid", "refused"}:
            return out
        if str(out.get("mode") or "") not in {"conductor", "co_conductor"}:
            return _error("refused", "gate ai_seat did not confirm the requested seat", seat_result=out)
        return {
            "ok": True,
            "status": "claimed",
            "session_id": sid,
            "role": seat,
            "remote_result": out,
        }

    def _seat_lifecycle_call(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str,
        seat_args: dict,
    ) -> dict:
        """The shared door for the seat lifecycle modes that are not a claim.

        ONE HOME OR NONE. The measured defect was a succession that went to
        local SQLite while the claim went to the gate: the gate's registry held
        the seat, the local copy did not, and the operator was told 'occupied'
        and 'vacant' about the same seat inside one minute. Succession and
        release now travel the SAME authenticated route the claim does.
        """
        seat = str(role or "").strip().lower()
        if seat and seat not in {"conductor", "co_conductor"}:
            return _error("invalid", "remote XAACP seats are conductor/co_conductor only")
        sid = str(session_id or "").strip()
        ready = self.dispatch(project_root, mode="xaacp_directory", session_id=sid)
        if not ready.get("ok"):
            return ready

        from .mcp_server_runtime_helpers import (
            current_calling_host_kind,
            current_calling_host_session_id,
        )
        from .webmcp_identity import (
            META_AIDOCS_HOST_KIND,
            META_AIDOCS_HOST_SESSION,
            META_AIDOCS_PROJECT_ID,
            META_AIDOCS_SESSION_ID,
        )

        hsid = str(current_calling_host_session_id() or "").strip()
        hkind = str(current_calling_host_kind() or "").strip()
        if not hsid or not hkind or hkind == "unknown":
            return _error(
                "forbidden", "caller has no canonical host identity for a remote seat call"
            )
        meta = {
            META_AIDOCS_HOST_SESSION: hsid,
            META_AIDOCS_HOST_KIND: hkind,
            META_AIDOCS_PROJECT_ID: self._project_id,
            META_AIDOCS_SESSION_ID: sid,
        }
        out = self._tool_call("ai_seat", {**seat_args, "session_id": sid}, meta)
        if not isinstance(out, dict):
            return _error("unavailable", "gate ai_seat returned a non-object")
        return out

    def succeed_seat(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str,
        reason: str = "",
    ) -> dict:
        """Inherit a seat on the gate — the authority that owns the registry.

        No grace knob crosses the wire: the gate applies its own floor, and a
        claimant that could name the grace could name zero.
        """
        return self._seat_lifecycle_call(
            project_root,
            session_id=session_id,
            role=role,
            seat_args={"mode": "succeed", "role": str(role or "").strip().lower(),
                       "reason": str(reason or "")},
        )

    def release_seat(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str = "",
        reason: str = "",
    ) -> dict:
        """Hand back the CALLER'S OWN seat on the gate. No actor crosses the wire."""
        return self._seat_lifecycle_call(
            project_root,
            session_id=session_id,
            role=role,
            seat_args={"mode": "exit", "role": str(role or "").strip().lower()},
        )

    #: Bounded transport retry for operator_append (#1061).
    OPERATOR_APPEND_MAX_ATTEMPTS = 3

    def operator_append(
        self,
        *,
        session_id: str,
        target_actor_id: str,
        body: str,
        causal_ref: str,
        operator_principal: dict,
        transport: str,
    ) -> dict:
        """Append an operator-origin stream entry ON THE AUTHORITY (#1061).

        Executed gate-side by ``conductor_comms.xaacp_operator_append`` behind
        the gate-resolved org-admin principal check; this edge only carries
        the request with its own gate credential. Idempotent at the authority
        on (session_id, origin='operator', causal_ref), so a transport retry
        can never create a second entry. Transport failure: bounded retry, then
        a named ``unavailable``.
        """
        from .webmcp_identity import META_AIDOCS_PROJECT_ID, META_AIDOCS_SESSION_ID

        sid = str(session_id or "").strip()
        if not sid:
            return _error("invalid", "operator_append requires session_id")
        meta = {META_AIDOCS_PROJECT_ID: self._project_id, META_AIDOCS_SESSION_ID: sid}
        args = {
            "mode": "wait_next",
            "session_id": sid,
            "timeout_seconds": 0.0,
            "metadata": {
                "_stream_op": {
                    "op": "operator_append",
                    "target_actor_id": str(target_actor_id or ""),
                    "body": str(body or ""),
                    "causal_ref": str(causal_ref or ""),
                    "operator_principal": dict(operator_principal or {}),
                    "transport": str(transport or ""),
                }
            },
        }
        last: dict = {}
        for _attempt in range(self.OPERATOR_APPEND_MAX_ATTEMPTS):
            last = self._tool_call("ai_msg", args, meta)
            if not (
                isinstance(last, dict)
                and last.get("ok") is False
                and last.get("status") == "unavailable"
                and "unreachable" in str(last.get("error") or "")
            ):
                return last
        return {**last, "attempts": self.OPERATOR_APPEND_MAX_ATTEMPTS}


class UnavailableXaacpAuthority:
    """A bound project whose single XAACP authority cannot be reached/configured."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def dispatch(self, project_root: Path, **payload: Any) -> dict:
        return _error("unavailable", self.reason)

    def operator_append(self, **_kwargs: Any) -> dict:
        return _error("unavailable", self.reason)

    def claim_seat(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str,
        confirm_token: str = "",
    ) -> dict:
        return _error("unavailable", self.reason)

    # AN UNREACHABLE AUTHORITY IS NOT A LICENCE TO WRITE THE LOCAL COPY. Every
    # seat-lifecycle door refuses identically here; a fallback on any one of
    # them is how the registry forks into two universes that both say ok.
    def succeed_seat(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str,
        reason: str = "",
    ) -> dict:
        return _error("unavailable", self.reason)

    def release_seat(
        self,
        project_root: Path,
        *,
        session_id: str,
        role: str = "",
        reason: str = "",
    ) -> dict:
        return _error("unavailable", self.reason)

def remote_authority_for(project_root: Path):
    """Return remote authority for a cloud-bound project, ``None`` iff unbound/local.

    Critical anti-fork rule: once a canonical project id exists, missing hub
    credentials produce an UNAVAILABLE authority, never a fallback local write.
    """
    from .mcp_server_runtime_helpers import current_gate_principal

    # Gate execution IS the canonical copy. Forwarding again would recurse.
    if current_gate_principal():
        return None

    root = Path(project_root)
    project_id = ""
    try:
        from .config import get_setting

        project_id = str(get_setting("sync.vps_hub_project_id", project_root=root, default="") or "").strip()
    except Exception:
        project_id = ""
    if not project_id:
        # CANONICAL IDENTITY, NOT THE BACKLOG'S CONVERGENCE VERDICT (#981).
        #
        # This read `backlog_hub_client.binding(root)[1]`, and `binding()`
        # returns ("", "") unless BOTH org_id AND project_id resolve. So a
        # checkout that KNEW ITS CANONICAL PROJECT ID but could not locally
        # resolve the org fell through to `return None` — "unbound/local" — and
        # XAACP forked into local SQLite.
        #
        # MEASURED: the local agent answered xaacp_directory with 36 workers and
        # 2 conductor seats while the gate, for the same session, saw only the
        # two WebMCP actors. Two populated directories, both ok:true, describing
        # different universes.
        #
        # The two helpers answer DIFFERENT QUESTIONS. `binding()` asks "is this
        # project converged enough to exchange backlog rows with a hub", which
        # legitimately requires an org. XAACP asks "WHICH PROJECT AM I".
        # Identity does not require convergence, and borrowing the stricter
        # verdict turned a missing org into a missing project.
        #
        # Note the anti-fork rule in this function's own docstring was already
        # implemented correctly for CREDENTIALS (no token => Unavailable). The
        # org gate defeated it one line earlier, at identity, where None still
        # means local.
        try:
            from . import project_binding_resolver

            project_id = str(project_binding_resolver.resolve(root).project_id or "").strip()
        except Exception:  # noqa: BLE001 — an unanswerable identity is NOT local
            project_id = ""
    if not project_id:
        # NO canonical id at all: a genuinely local project. The local-only
        # floor is untouched — AIDOCS keeps working with no cloud project,
        # offline, forever.
        return None

    # THE GATE CREDENTIAL, NOT THE LOCAL ONE. `resolve_operator_token` answers
    # "what proves me to THIS MACHINE" — a local `identity_tokens` row the gate
    # has never issued. Presenting it to the hub is a guaranteed 401, and this
    # function used to do exactly that on every XAACP call.
    #
    # What the operator saw was not "your credential is wrong". `_tool_call`
    # flattens every failure into an error dict, `dispatch` reads the missing
    # "project" key as an EMPTY SELECTION, and reports it as
    # "gate project selection does not match this local project's binding" —
    # an auth failure wearing a project conflict's clothes. Measured
    # 2026-09-01: the same gate tool called with the LABELLED credential
    # returned the project correctly.
    #
    # And the 401s are not free: seven trip the gate's CrowdSec
    # `http-generic-401-bf` and ban the machine for four hours, including the
    # sign-in that would refresh the credential (#992). So an unusable
    # credential must refuse HERE, before the socket, spending no request —
    # the same rule `_probe_the_gate` follows for the same reason.
    # RENEW BEFORE PRESENTING (#1000), the same door local_cloud_link and the
    # backlog sitter already use. `ensure_gate_credential` is, by its own
    # commit, "the one door every cloud caller goes through"; XAACP called
    # `cached_gate_credential` directly and so turned an EXPIRED-BUT-RENEWABLE
    # credential into a terminal `gate_token_expired`. An unattended surface
    # went dark an hour after each sign-in and stayed dark until a human opened
    # a browser — for a credential the machine could have refreshed itself.
    #
    # THIS DOES NOT WEAKEN THE RULE ABOVE. That comment forbids spending a
    # request on a credential the authority will REJECT; a renewal is not that
    # request — it is the one that ends the rejection, and #992's sting is
    # precisely that the ban blocks the sign-in which would have fixed it.
    # `ensure_gate_credential` returns the cached credential untouched when it
    # is live and its hourly permission answer is fresh, so the ordinary call
    # spends nothing, and it self-limits to ONE attempt per window recorded on
    # disk — the retry loop that banned this machine cannot re-form here.
    from .gate_credential_renewal import ensure_gate_credential
    from .operator_token_resolution import GATE_CRED_OK, GATE_CRED_REMEDY

    # THE DOOR IS THE SOLE DECISION. Every credential state reaches it — expired
    # above all. That is the #1000 bypass this change exists to end: an
    # expired-but-renewable credential must renew, not go terminal.
    #
    # TWO WRONG TURNS ON THE WAY HERE, both recorded because the second one
    # only looked right:
    #
    #   * The first cut read `cached_gate_credential` and treated an expired
    #     credential as terminal — the #1000 bypass this change exists to end.
    #   * The second added a cached PRE-CHECK that refused REJECTED / ABSENT /
    #     LOCAL_ONLY / no-refresh before the door. MEASURED 2026-09-07 against a
    #     copy of the live cache, that was wrong twice over. The door already
    #     answers unusable for absent and local-only on its own, so those
    #     branches were dead weight; and refusing on REJECTED was actively
    #     harmful — it would refuse forever after ONE 401 even when a refresh
    #     could repair it. #992's latch exists to stop the same rejected token
    #     being PRESENTED again; a renewal mints a DIFFERENT token, which is the
    #     remedy, not the retry. The bound on renewal attempts is the door's own
    #     one-per-window record on disk, not a caller's guesswork.
    #
    # Reading it twice was also a defect in itself: the cached read and the door
    # do not share a read, so any divergence produced a caller that had
    # CLASSIFIED one credential and PRESENTED another — the substitute-from-a-
    # broader-layer shape #1001 forbids for session identity, and it made
    # behaviour depend on what else had run in the process.
    outcome = ensure_gate_credential(project_root=root)
    cred = outcome.credential
    if cred.reason != GATE_CRED_OK or not cred.token:
        # DISTINGUISH THE TWO DEAD ENDS, because they have different remedies
        # and only one of them is the operator's to act on.
        if getattr(cred, "renewable", False):
            remedy = (
                "renewal was attempted and did not restore it"
                f"{': ' + outcome.reason if outcome.reason else ''}"
            )
        else:
            remedy = (
                "there is no refresh credential to renew from — sign in again "
                f"from the Dashboard: {GATE_CRED_REMEDY}"
            )
        # THE ORIGINAL CLAUSE IS LOAD-BEARING AND STAYS VERBATIM. #54 and #981
        # match on "refusing to fork XAACP into local SQLite" — the promise
        # that a credential outage never silently becomes a second, divergent
        # local mailbox. The renewal remedy is APPENDED; it does not reword the
        # guarantee it sits beside.
        return UnavailableXaacpAuthority(
            f"project is cloud-bound but this machine holds no usable gate "
            f"credential ({cred.reason}); refusing to fork XAACP into local "
            f"SQLite, and spending no request on a credential the authority "
            f"will not accept. {remedy}"
        )
    token = cred.token
    try:
        from .config import get_setting

        base_url = str(get_setting("sync.vps_hub_url", project_root=root, default=DEFAULT_GATE_URL) or DEFAULT_GATE_URL)
    except Exception:
        base_url = DEFAULT_GATE_URL
    return RemoteMcpXaacpAuthority(base_url=base_url, token=token, project_id=project_id)
