"""Link a LOCAL project root to a canonical cloud project — verified, then durable.

Registering a local project with its cloud project is the one act that writes
the cloud project id for this root. Until it is linked,
``project_binding_resolver`` answers ``local_unregistered`` for it, and XAACP
and the backlog stay local.

A TYPED project_id IS A CLAIM, NOT AN IDENTITY.
───────────────────────────────────────────────
Persisting one unverified would make a local text field an identity authority:
anyone who can reach the tool could type any project id. (A hand-set
``sync.vps_hub_project_id`` setting is an override for a host that must point
somewhere else, not the way a project gets linked.)

So the user NAMES A CANDIDATE and the AUTHORITY ANSWERS:

  1. resolve the user's own gate credential (never a service token);
  2. ask the gate, authenticated as them (``ai_project(mode='list')`` over the
     gate's ``/v1/mcp`` endpoint), which projects they are entitled to;
  3. accept the candidate only if it is IN that answer;
  4. persist the org_id THE AUTHORITY RETURNED, never one the caller supplied.

Step 4 is not a detail. The org is a property of the project, so taking it
from the caller would let a correct project id be filed under the wrong org.
``link_local`` does not accept an org from the tool surface at all; it only
ever receives what came back from the gate.

FAILS CLOSED, DISCRIMINATED. Every refusal names WHICH step could not be
completed, because "could not link" spans an expired credential, an offline
gate and a project the user has no claim to: three different next actions.
Nothing is persisted unless the authority affirmed the project.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

#: Discriminated refusals. A caller (and a user) must be able to tell
#: "you are not entitled to that project" from "we could not ask".
LINK_NO_CREDENTIAL = "no_operator_credential"
LINK_AUTHORITY_UNREACHABLE = "authority_unreachable"
LINK_AUTHORITY_REFUSED = "authority_refused"
#: The authority REJECTED the credential (HTTP 401 only) — distinct from refusing
#: the request. The gate answers 401 (for example `{"error": "unknown_token"}`)
#: for an unknown, expired, revoked or wrong-audience bearer; only that earns
#: the re-auth remedy and the credential latch. The user's next action is to
#: sign in again, and the refusal says so.
LINK_CREDENTIAL_REJECTED = "credential_rejected"
#: HTTP 403: the REQUEST was refused (scope, org, permission — or a proxy / WAF
#: in front of the gate). Not credential death: nothing is latched and the
#: user is not told to sign in again. Carries the gate's body + request id.
LINK_AUTHORIZATION_REFUSED = "authorization_refused"
LINK_NOT_ENTITLED = "project_not_entitled"
LINK_NO_ORG = "project_has_no_org"
LINK_PERSIST_FAILED = "persist_failed"


def _gate_url(project_root: Path) -> str:
    from .xaacp_authority import DEFAULT_GATE_URL

    try:
        from .config import get_setting

        base = str(
            get_setting("sync.vps_hub_url", project_root=project_root, default=DEFAULT_GATE_URL)
            or DEFAULT_GATE_URL
        )
    except Exception:  # noqa: BLE001 — config trouble ⇒ the default authority
        base = DEFAULT_GATE_URL
    base = base.rstrip("/")
    return base if base.endswith("/v1/mcp") else base + "/v1/mcp"


def _post(url: str, rpc: dict, token: str, timeout: float) -> dict:
    from .governed_egress import assert_egress_allowed

    host = urllib.parse.urlparse(url).hostname or ""
    assert_egress_allowed(url, purpose="local_cloud_link", allow_hosts=[host])
    req = urllib.request.Request(
        url,
        data=json.dumps(rpc, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "aidocs-local-cloud-link",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 governed above
        return json.loads(resp.read().decode("utf-8"))


def _http_error_body(exc: Any) -> str:
    """The authority's own words from an HTTP error, truncated and safe.

    Read once, defensively: the body may be unreadable, non-JSON, or absent, and
    a diagnostic that raises while reporting a failure is worse than no
    diagnostic. Never includes the credential — only what the server said back.
    """
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001
        return "no response body"
    text = raw.decode("utf-8", errors="replace")[:300] if isinstance(raw, bytes) else str(raw)[:300]
    return text.strip() or "empty response body"


def _rejected_status(said: str) -> int:
    """The HTTP status a `LINK_CREDENTIAL_REJECTED` reason carries."""
    head, _, _ = str(said or "").partition(":")
    try:
        return int(head)
    except ValueError:
        return 401


def _request_id_in(said: str) -> str:
    """The gate's request_id from its own error body, when it sent one."""
    try:
        body = json.loads(str(said or "").partition(":")[2])
    except (ValueError, TypeError):
        return ""
    return str(body.get("request_id") or "") if isinstance(body, dict) else ""


def _tool_result(reply: Any) -> dict:
    """Unwrap an MCP tools/call reply into the tool's own JSON payload."""
    if not isinstance(reply, dict):
        return {}
    if isinstance(reply.get("error"), dict):
        return {"_error": str(reply["error"].get("message") or "gate refusal")}
    try:
        return json.loads(reply["result"]["content"][0]["text"])
    except Exception:  # noqa: BLE001
        result = reply.get("result")
        return result if isinstance(result, dict) else {}


def entitled_projects(
    project_root: Path,
    *,
    token: str,
    http: Callable[[str, dict, str, float], dict] | None = None,
    timeout: float = 30.0,
) -> tuple[list[dict] | None, str]:
    """(projects, reason). `None` means WE COULD NOT ASK — never "none exist".

    The distinction is the whole point: an empty list means the operator is
    entitled to nothing, and `None` means the question went unanswered. Folding
    them together would turn an offline moment into "you have no claim to this
    project", which reads like a permissions verdict and is not one.
    """
    url = _gate_url(project_root)
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ai_project", "arguments": {"mode": "list"}},
    }
    caller = http or _post
    try:
        payload = _tool_result(caller(url, rpc, token, timeout))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # AUTHORIZATION, not the credential: carry the body for diagnosis.
            return None, f"{LINK_AUTHORIZATION_REFUSED}:{exc.code}:{_http_error_body(exc)}"
        if exc.code == 401:
            # CARRY WHAT THE AUTHORITY SAID. A refusal that names no cause sends
            # the user looking in the wrong place; the gate's own body
            # ("unknown_token", plus a request_id it can be traced by) is the
            # actionable half.
            # STATUS FIRST so the caller can record it, then the body.
            return None, f"{LINK_CREDENTIAL_REJECTED}:{exc.code}:{_http_error_body(exc)}"
        return None, LINK_AUTHORITY_UNREACHABLE
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None, LINK_AUTHORITY_UNREACHABLE
    except Exception:  # noqa: BLE001 — an unanswerable question is never an answer
        return None, LINK_AUTHORITY_UNREACHABLE
    if payload.get("_error"):
        return None, f"{LINK_AUTHORITY_REFUSED}:{payload.get('_error')}"
    rows = payload.get("projects")
    if not isinstance(rows, list):
        return None, LINK_AUTHORITY_UNREACHABLE
    return [r for r in rows if isinstance(r, dict)], ""


def link_project(
    project_root: Path,
    project_id: str,
    *,
    http: Callable[[str, dict, str, float], dict] | None = None,
    timeout: float = 30.0,
) -> dict:
    """Verify `project_id` against the operator's entitlement, then persist it.

    Returns a receipt on success, or a discriminated refusal. NOTHING IS WRITTEN
    unless the authority affirmed the project — a refusal at any step leaves the
    local registration exactly as it was.
    """
    root = Path(project_root).resolve()
    want = str(project_id or "").strip()
    if not want:
        return {
            "linked": False,
            "_error": LINK_NOT_ENTITLED,
            "_detail": "project_id is required to link a local root to a cloud project",
        }

    # THE GATE CREDENTIAL, NOT THE LOCAL ONE. `resolve_operator_token` answers
    # "what proves me to THIS MACHINE": a local `identity_tokens` row the gate
    # never issued, which the gate answers with 401. Repeated 401s can get a
    # machine rate-limited at the gate's edge, so an unusable credential
    # refuses HERE, before the socket, spending no request.
    from .operator_token_resolution import (
        GATE_CRED_OK,
        GATE_CRED_REMEDY,
        record_gate_answer,
    )

    # RENEW BEFORE PRESENTING. A credential whose hour has run out is
    # replaced from the stored refresh credential without a browser, and the
    # renewal's own `scope` is the hourly permission recheck. Live-and-fresh
    # returns the cached row untouched, so the ordinary path spends nothing.
    from .gate_credential_renewal import ensure_gate_credential

    cred = ensure_gate_credential(project_root=root).credential
    if cred.reason != GATE_CRED_OK or not cred.token:
        return {
            "linked": False,
            "_error": LINK_NO_CREDENTIAL,
            "_detail": (
                f"no credential this machine may present to the authority "
                f"({cred.reason}). A cloud link is an assertion about YOUR "
                f"entitlement, so it cannot be made without your identity — "
                f"{GATE_CRED_REMEDY}, then re-run."
            ),
        }
    token, token_source = cred.token, "gate_credential"

    rows, reason = entitled_projects(root, token=token, http=http, timeout=timeout)
    if rows is None:
        code, _, said = reason.partition(":")
        if code == LINK_CREDENTIAL_REJECTED:
            # LATCH IT: the authority disowned this exact
            # credential, and nothing may present it again until a new
            # sign-in replaces the row. The status rides in the reason.
            record_gate_answer(
                status=_rejected_status(said),
                request_id=_request_id_in(said),
                presented_token=token,
            )
            detail = (
                f"the authority REJECTED this credential (it answered: {said}). Your "
                f"operator token — resolved from {token_source!r} — is not one it "
                f"recognises, so sign in again and retry. Nothing was written, and "
                f"this says nothing about whether you are entitled to the project."
            )
        elif code == LINK_AUTHORIZATION_REFUSED:
            # NOT LATCHED, NOT A RE-AUTH: a 403 refuses this
            # request; the shared credential stays usable everywhere else.
            _status, _, body = said.partition(":")
            rid = _request_id_in(said)
            detail = (
                f"the authority refused this request (HTTP 403 — authorization, not "
                f"an invalid credential; it answered: {body or '(no body)'}"
                f"{f', request_id {rid}' if rid else ''}). The shared credential was "
                f"not latched. Nothing was written. Check this project's scope and "
                f"org entitlement for the operator on the gate."
            )
        elif code == LINK_AUTHORITY_REFUSED:
            detail = (
                f"the authority refused the entitlement query: {said}. Nothing was "
                f"written. This is a refusal of the QUESTION, not a verdict that you "
                f"lack the entitlement."
            )
        else:
            detail = (
                "could not reach the authority to ask which projects you are "
                "entitled to; nothing was written. This is NOT a statement that you "
                "lack the entitlement — the question went unanswered."
            )
        return {"linked": False, "_error": code, "_detail": detail}

    # The authority executed a tool for this credential: that is a fresh
    # permissions answer, and the hourly recheck stamp records it.
    record_gate_answer(status=200)

    match = next(
        (r for r in rows if str(r.get("project_id") or "").strip() == want),
        None,
    )
    if match is None:
        return {
            "linked": False,
            "_error": LINK_NOT_ENTITLED,
            "_detail": (
                f"the authority does not list {want!r} among the projects your "
                "credential is entitled to. Check the id with "
                "ai_project(mode='list') on the gate."
            ),
        }

    # THE ORG COMES FROM THE AUTHORITY'S ROW, never from the caller.
    org_id = str(match.get("org_id") or "").strip()
    if not org_id:
        return {
            "linked": False,
            "_error": LINK_NO_ORG,
            "_detail": (
                f"the authority lists {want!r} but reports no org for it. An org-less "
                "registration must keep presenting as org-less rather than being "
                "completed with a plausible substitute — resolve the project's org "
                "first."
            ),
        }

    try:
        from .local_project_registry import link_local

        stored = link_local(
            root,
            root=root,
            project_id=want,
            org_id=org_id,
            name=str(match.get("name") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — report the failure, never a half-link
        return {
            "linked": False,
            "_error": LINK_PERSIST_FAILED,
            "_detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }

    return {
        "linked": True,
        "project_root": str(root),
        "project_id": want,
        "org_id": org_id,
        "name": str(stored.get("name") or match.get("name") or ""),
        "verified_by": "authority",
        "credential_source": token_source,
        "durable": True,
        "summary": (
            f"{root.name} is now registered as {want} (org {org_id}). This survives "
            "host sessions, daemon restarts and deploys — it is a property of the "
            "project tree, not of this conversation."
        ),
    }
