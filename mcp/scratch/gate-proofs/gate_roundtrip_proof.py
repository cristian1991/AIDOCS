#!/usr/bin/env python3
"""XAACP round-trip proof ACROSS A REAL (locally-run) OUTER GATE.

WHY THIS FILE IS IN THE REPO. The 23/23 result this harness first produced was
AGENT-REPORTED from a temp path, which is not evidence anyone can check. It
lives here so any reader can run it and judge the result themselves.

WHAT IT STANDS UP (all of it real, none of it mocked):
  * a real outer gate, served on 127.0.0.1 over real HTTP JSON-RPC;
  * real bearer-token validation -- tokens minted by OuterGateTokenStore and
    resolved by the gate's own scoped auth resolver;
  * the real scope wall -- an under-scoped token's xaacp_send is refused with
    JSON-RPC -32001 insufficient_scope, and the same call on a token holding
    `xaacp_write` is not;
  * GATE-COMPOSED IDENTITY -- each caller's host_session_id is whatever the
    GATE composes from its _meta, discovered by asking the gate (its own
    refusal names it); no local guess is substituted;
  * gate-side xaacp_actors -- the directory, actor kinds and actor_ids are the
    rows the gate itself wrote.
Everything on the messaging legs crosses the wire:
  urllib -> ThreadingHTTPServer -> outer_gate_transport.dispatch -> scoped-token
  auth -> OuterGate.execute (-> _oge_scope_msg -> _oge_scope_edit scope wall)
  -> the canonical ai_msg implementation -> xaacp_actors.

THE TWO DELIBERATE DEVIATIONS, AND WHY:
  1. `release_trust.verify_release` is stubbed to succeed. A dev working tree's
     signed manifest is stale against the working copy by construction, so the
     SHA start-guard would refuse serve() outright.
  2. `package_trusted=True` is passed to OuterGate. For the same reason: an
     unsigned dev tree makes the live gate refuse every call with
     `package_untrusted` before any XAACP or scope code runs.
  Both are the RELEASE-INTEGRITY axis, not the identity/authorization axis
  under test, and both are exactly what every in-tree gate test does. Nothing
  else about the gate is relaxed: auth, scope, identity composition and routing
  all run for real.

WHAT IT THEREFORE DOES NOT PROVE: the production deployment's own
configuration and its release-integrity posture. A green run here says the
gate CODE admits, scopes and routes these calls correctly; it says nothing
about whether the deployed instance is configured or signed correctly.

Client shape copied from mcp/gate_checks/webmcp_smoke.py.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "mcp" / "server"))

TMP = Path(tempfile.mkdtemp(prefix="xaacp_gate_proof_"))
os.environ["AIDOCS_GLOBAL_CONFIG_DB"] = str(TMP / "global.sqlite3")
os.environ.setdefault("AIDOCS_GATE_REQUEST_TIMEOUT_S", "60")

GATE_HOME = TMP / "gate-root"
PROJ = TMP / "proj"
SESSION = "gateproof"
OTHER_SESSION = "gateproof-elsewhere"
LANE = "lane-1"
SUBAGENT_HOST_AGENT = "a26c3b2da0c816517"

#: Every check, in order. `verbatim` carries the UNTRUNCATED gate response for
#: the load-bearing checks so a reader of the receipt can judge the refusal
#: text itself rather than trusting this harness's boolean.
RESULTS: list[dict] = []

#: Token scopes actually used, per leg — recorded in the receipt so nobody has
#: to re-read the harness to know what credential each assertion ran on.
TOKEN_SCOPES: dict[str, list[str]] = {}

STARTED_UTC = datetime.now(timezone.utc).isoformat()


def check(label: str, ok: bool, detail: str = "", verbatim: object = None) -> bool:
    RESULTS.append(
        {
            "name": label,
            "pass": bool(ok),
            "detail": detail[:600] if detail else "",
            **({"verbatim_gate_response": verbatim} if verbatim is not None else {}),
        }
    )
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    return ok


def _aidocs_build() -> dict:
    try:
        import aidocs_mcp

        return {
            "version": aidocs_mcp._version_from_pyproject(),
            "build": aidocs_mcp._build_from_ticker(),
        }
    except Exception as exc:  # pragma: no cover - receipt metadata only
        return {"version": None, "build": None, "error": str(exc)[:200]}


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return out.stdout.strip() or f"unknown: {out.stderr.strip()[:200]}"
    except Exception as exc:  # pragma: no cover - receipt metadata only
        return f"unknown: {exc}"


def _source_fingerprint() -> dict:
    """WHAT BYTES ACTUALLY RAN — not merely which commit was checked out.

    Co-conductor review, 2026-09-04: the first receipt recorded
    ``git_head: f8e2d0583`` while the harness was still DIRTY, so the bytes it
    executed existed in no commit the receipt named. The provenance claim read
    stronger than it was. A HEAD sha alone can only ever say "the tree was on
    this commit"; it cannot say "and the file I ran is the one that commit
    contains".

    So the receipt now carries the sha256 of THIS FILE'S OWN BYTES, plus the
    blob sha git holds for it at HEAD, plus whether the tree was dirty and in
    which files. When ``harness_sha256_matches_head`` is true the run is
    reproducible from the named commit alone. When it is false the receipt says
    so in the same breath as the HEAD, so nobody has to infer it from a commit
    listing afterwards.
    """
    import hashlib

    here = Path(__file__).resolve()
    try:
        digest = hashlib.sha256(here.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - receipt metadata only
        digest = f"unreadable: {exc}"

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=30
            )
            return out.stdout.strip()
        except Exception as exc:  # pragma: no cover - receipt metadata only
            return f"unknown: {exc}"

    rel = "mcp/scratch/gate-proofs/gate_roundtrip_proof.py"
    # COMPARE THE WAY GIT COMPARES. The first cut of this hashed the working
    # file's RAW BYTES against `git show`'s text and called any difference a
    # mismatch -- but this repo's working copies are CRLF and its blobs are LF,
    # so that field could NEVER be true and would have shouted "unverified" on
    # every clean run. A check that cannot pass is worse than none: it trains
    # the reader to ignore it.
    #
    # `git hash-object` applies the same filters git applies on the way in, so
    # hashing the working file with it and comparing against the blob recorded
    # at HEAD is an exact, line-ending-agnostic answer to "is this the same
    # source?".
    head_blob = _git("rev-parse", f"HEAD:{rel}")
    working_blob = _git("hash-object", "--", str(here))
    dirty = [ln for ln in _git("status", "--porcelain").splitlines() if ln.strip()]
    return {
        "harness_path": rel,
        # The literal bytes executed, for anyone reproducing outside git.
        "harness_sha256": digest,
        "harness_blob_at_head": head_blob,
        "harness_blob_as_run": working_blob,
        "harness_matches_head": bool(head_blob) and head_blob == working_blob,
        # The tree may be dirty in OTHER files without weakening this receipt:
        # what it asserts is that THIS harness is the committed one.
        "tree_dirty": bool(dirty),
        "dirty_paths": dirty[:40],
    }


def _write_receipt() -> Path:
    """The EXECUTION RECEIPT — the thing a committed harness was still missing.

    A committed harness proves the test EXISTS; only a committed receipt proves
    it RAN, against which tree, on which build, with which scopes, and what the
    gate actually said. Written under mcp/scratch/gate-proofs/receipts/, which
    mcp/.gitignore deliberately un-ignores (`scratch/*` + `!scratch/gate-proofs/`),
    so it is committable — verified with `git check-ignore -v`.
    """
    passed = sum(1 for r in RESULTS if r["pass"])
    receipt = {
        "harness": "mcp/scratch/gate-proofs/gate_roundtrip_proof.py",
        "git_head": _git_head(),
        # A HEAD alone says which commit was checked out, NOT which bytes ran.
        "source": _source_fingerprint(),
        "started_utc": STARTED_UTC,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "aidocs": _aidocs_build(),
        "python": sys.version.split()[0],
        "token_scopes_per_leg": TOKEN_SCOPES,
        "deviations": [
            {
                "name": "release_trust.verify_release stubbed to succeed",
                "why": (
                    "a dev working tree's signed manifest is stale against the "
                    "working copy by construction, so the SHA start-guard would "
                    "refuse serve() outright"
                ),
                "axis": "release integrity — NOT the identity/authorization axis under test",
            },
            {
                "name": "package_trusted=True passed to OuterGate",
                "why": (
                    "an unsigned dev tree makes the live gate refuse every call "
                    "with `package_untrusted` before any XAACP or scope code runs"
                ),
                "axis": "release integrity — NOT the identity/authorization axis under test",
            },
        ],
        "checks": RESULTS,
        "counts": {
            "passed": passed,
            "total": len(RESULTS),
            "failed": len(RESULTS) - passed,
            "result": "PASS" if passed == len(RESULTS) else "FAIL",
        },
    }
    out_dir = REPO / "mcp" / "scratch" / "gate-proofs" / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = STARTED_UTC.replace(":", "").replace("-", "").split(".")[0].replace("+0000", "Z")
    path = out_dir / f"{stamp}-gate-roundtrip.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=False), encoding="utf-8")
    return path


# ── the gate-side setup (provisioning, not the thing under test) ────────────

def _provision():
    from aidocs_mcp import release_trust
    from aidocs_mcp.release_trust import ReleaseTrust

    # DEVIATION 1 (see the module docstring): a dev tree's signed manifest is
    # stale vs the working copy, so the SHA start-guard would refuse serve().
    # Same stub every in-tree gate test uses; integrity axis, not identity.
    release_trust.verify_release = lambda *a, **k: ReleaseTrust(
        ok=True, reason="local-gate-proof"
    )

    from aidocs_mcp.identity_store import IdentityStore
    from aidocs_mcp.mcp_server_runtime_helpers import stamp_commissioned
    from aidocs_mcp.outer_gate_projects import GateProjectStore
    from aidocs_mcp.outer_gate_token_store import OuterGateTokenStore

    (GATE_HOME / ".MEMORY" / ".index").mkdir(parents=True, exist_ok=True)
    (PROJ / ".MEMORY" / ".aidocs").mkdir(parents=True, exist_ok=True)
    (PROJ / ".MEMORY" / ".aidocs" / "index.aidocs").write_text("x", encoding="utf-8")
    (PROJ / ".MEMORY" / ".index").mkdir(parents=True, exist_ok=True)
    (PROJ / ".MEMORY" / "sessions" / SESSION).mkdir(parents=True, exist_ok=True)
    (PROJ / ".MEMORY" / "sessions" / SESSION / "SESSION.md").write_text(
        "# gateproof\n", encoding="utf-8"
    )
    stamp_commissioned(PROJ)

    user = IdentityStore().create_user(
        GATE_HOME,
        email=f"gateproof-{secrets.token_hex(6)}@example.invalid",
        password=secrets.token_urlsafe(24),
        role="admin",
    )

    store = GateProjectStore()
    proj_row = store.register(GATE_HOME, name="gateproof", root=PROJ, source="local")
    store.select(GATE_HOME, user.user_id, proj_row["project_id"])

    ts = OuterGateTokenStore()
    full = ts.mint_for_operator(
        GATE_HOME,
        user_id=user.user_id,
        role="admin",
        scope=["catalog", "tier_r_invoke", "status", "sync", "xaacp_write"],
        ttl_seconds=3600,
    )
    noscope = ts.mint_for_operator(
        GATE_HOME,
        user_id=user.user_id,
        role="admin",
        scope=["catalog", "tier_r_invoke", "status", "sync"],
        ttl_seconds=3600,
    )
    # #1021 CLOSED THE SEAT WORKAROUND. This harness used to mint a THIRD token
    # carrying `tier_m_edit` purely to get a seat, because ai_seat inherited the
    # blanket EDIT scope check. `_oge_scope_seat` now prices enter/co-enter/exit
    # at `xaacp_write`, so the seat rides the SAME `full` token as the messaging
    # legs -- which is the whole point of the ruling and is now asserted, over
    # HTTP, below.
    #
    # The tier_m_edit token survives ONLY as a NEGATIVE: it proves there is NO
    # TRANSITION GRANT, i.e. that the coupling was SEVERED rather than merely
    # widened. It deliberately does NOT carry xaacp_write.
    medit_only = ts.mint_for_operator(
        GATE_HOME,
        user_id=user.user_id,
        role="admin",
        scope=["catalog", "tier_r_invoke", "status", "tier_m_edit"],
        ttl_seconds=3600,
    )
    return user.user_id, full, noscope, medit_only


def _serve():
    from aidocs_mcp import outer_gate_transport as T
    from aidocs_mcp.outer_gate import OuterGate
    from aidocs_mcp.outer_gate_executor import build_read_executor
    from aidocs_mcp.outer_gate_transport import default_transport_audit
    from aidocs_mcp.runtime_service import RuntimeService
    from aidocs_mcp.service_hub import AidocsServiceHub

    # EXACTLY outer_gate_server.build_serving_gate, with ONE difference:
    # package_trusted=True (DEVIATION 2, see the module docstring). A dev
    # working tree is unsigned by construction, so the real integrity check
    # makes the live gate refuse every call with `package_untrusted` before any
    # XAACP/scope code runs. That axis is release integrity, not the axis under
    # test; every in-tree gate test stubs it the same way.
    hub = AidocsServiceHub(templates_root=GATE_HOME)
    gate = OuterGate(
        enabled=True,
        bind="127.0.0.1",
        package_trusted=True,
        manifest_entries=[],
        project_root=GATE_HOME,
        audit_sink=default_transport_audit(GATE_HOME),
        executor=build_read_executor(PROJ),
        exec_project_root=PROJ,
        hub=hub,
        runtime=RuntimeService(hub),
    )
    auth = T.make_scoped_auth(GATE_HOME)
    srv = T.serve(gate, project_root=GATE_HOME, host="127.0.0.1", port=0, auth_resolver=auth)
    host, port = srv.server_address[0], srv.server_address[1]
    return srv, f"http://{host}:{port}/v1/mcp"


# ── the JSON-RPC client (webmcp_smoke shape) ───────────────────────────────

def _payload(env: dict) -> dict:
    """The tool payload inside a JSON-RPC envelope, or {}."""
    try:
        return json.loads(env["result"]["content"][0]["text"])
    except Exception:
        return {}


class Caller:
    """One host identity speaking to the gate over HTTP."""

    def __init__(self, url: str, token: str, meta: dict, label: str) -> None:
        self.url, self.token, self.meta, self.label = url, token, meta, label
        self._id = 0

    def raw(self, name: str, args: dict) -> dict:
        self._id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": args, "_meta": dict(self.meta)},
            }
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {"_http": e.code, "_body": e.read().decode()[:600]}
        except (urllib.error.URLError, OSError) as e:
            return {"_error": "unreachable", "_detail": str(e)[:300]}

    def call(self, name: str, args: dict) -> dict:
        """A tool call that answers a server-issued confirm challenge once."""
        env = self.raw(name, args)
        payload = _payload(env)
        tok = str(payload.get("confirm_token") or "")
        if payload.get("_error") == "confirm_required" and tok:
            env = self.raw(name, {**args, "confirm_token": tok})
        return env

    def msg(self, **args) -> dict:
        """ai_msg through the gate; returns the PARSED tool payload, or the raw
        JSON-RPC envelope when the gate refused (so refusals stay verbatim)."""
        env = self.raw("ai_msg", args)
        if "error" in env or "_http" in env or "_error" in env:
            return {"__envelope__": env}
        try:
            return json.loads(env["result"]["content"][0]["text"])
        except Exception:
            return {"__envelope__": env}


def show(tag: str, payload: dict) -> str:
    text = json.dumps(payload, sort_keys=True)[:700]
    print(f"    >> {tag}: {text}")
    return text


def _msg_ids(payload: dict) -> list[str]:
    return [
        str(m.get("id") or m.get("message_id") or "")
        for m in (payload.get("messages") or [])
    ]


# ── binding the three host identities ──────────────────────────────────────

def _hsid_seen_by_gate(caller: Caller) -> str:
    """Ask the GATE who it thinks is calling. An unbound caller's own refusal
    names its composed host_session_id — no local guess, no substitution."""
    import re

    out = caller.msg(mode="xaacp_directory", session_id=SESSION)
    show(f"{caller.label} pre-bind directory", out)
    direct = str(out.get("missing_binding_host_session_id") or "").strip()
    if direct:
        return direct
    blob = json.dumps(out)
    m = re.search(r"host_session_id '([^']+)'", blob)
    return m.group(1) if m else ""


def _bind_managed(hsid: str) -> None:
    from aidocs_mcp.managed_mode_service import ManagedModeService
    from aidocs_mcp.mcp_server_runtime_helpers import (
        reset_request_host_identity,
        set_request_host_identity,
    )

    token = set_request_host_identity(hsid, host_kind="claude_code")
    try:
        ManagedModeService().set_mode(
            PROJ, session_id=SESSION, source="gate-roundtrip-proof", host_session_id=hsid
        )
    finally:
        reset_request_host_identity(token)


def _register_lane_worker(hsid: str) -> str:
    from aidocs_mcp.session_lane_agents_store import SessionLaneAgentsStore

    store = SessionLaneAgentsStore()
    worker_id = store.register_worker(
        PROJ, session_id=SESSION, lane_id=LANE, backend="claude_code", pid=os.getpid()
    )
    store.set_host_session_id(PROJ, worker_id, hsid)
    return worker_id

# ── the ChatGPT connector (real OAuth authorize + token exchange) ──────────

#: A stand-in for the operator-configured ChatGPT callback. Its EXACT value is
#: what the binding rests on (validate_authorize does exact set membership), so
#: any string works here as long as the same one is presented at /authorize.
CHATGPT_REDIRECT = "https://chatgpt.com/connector/oauth/GATEPROOFtoken1"

#: The scope a ChatGPT connector asks for on the POSITIVE leg. Deliberately
#: EXCLUDES tier_m_edit: the point of the leg is that messaging authority rides
#: on `xaacp_write` ALONE, with no edit authority anywhere near it.
CHATGPT_POSITIVE_REQUEST = ["catalog", "tier_r_invoke", "status", "sync", "xaacp_write"]

#: The NEGATIVE leg's request: edit authority, no messaging authority. If this
#: token could send, the scopes would merely have been RENAMED, not separated.
CHATGPT_NEGATIVE_REQUEST = ["catalog", "tier_r_invoke", "status", "tier_m_edit"]


def _pkce_pair() -> tuple[str, str]:
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _bind_chatgpt_connector() -> tuple[str, list[str]]:
    """Register the connector the way an operator does: by CONFIG, not by code.

    Then force its stored scope back to a PRE-RULING set. That is what makes the
    positive leg below load-bearing: if /authorize did not RECONCILE the client
    on every authorization, the connector would stay frozen at this stale scope
    forever and could never be granted `xaacp_write`, no matter what the
    compiled-in CHATGPT_SCOPES says. It is the exact defect #1019 fixed for the
    desktop client, and this run proves it closed for ChatGPT too.
    """
    import sqlite3

    from aidocs_mcp.config_store import ConfigStore
    from aidocs_mcp.outer_gate_oauth import CHATGPT_REDIRECT_CONFIG_KEY, OAuthStore

    ConfigStore().set(
        GATE_HOME,
        CHATGPT_REDIRECT_CONFIG_KEY,
        [CHATGPT_REDIRECT],
        scope="global",
        scope_key="",
    )
    store = OAuthStore()
    store.init_db(GATE_HOME)
    store.ensure_chatgpt_client(GATE_HOME)
    cid = store.chatgpt_client_id(GATE_HOME)
    stale = ["catalog", "status", "tier_r_invoke"]
    with sqlite3.connect(str(store.db_path(GATE_HOME))) as conn:
        conn.execute(
            "UPDATE oauth_clients SET scope_json=? WHERE client_id=?",
            (json.dumps(stale), cid),
        )
        conn.commit()
    frozen = sorted((store.get_client(GATE_HOME, cid) or {}).get("scope") or [])
    return cid, frozen


def _chatgpt_oauth_token(user_id: str, mcp_resource: str, requested: list[str]) -> dict:
    """Drive the REAL /authorize + /token code path as the ChatGPT client.

    Nothing is minted directly here: the credential this returns is the one an
    actual connector authorization produces, scope and all.
    """
    from aidocs_mcp.outer_gate_oauth import (
        AuthorizeRequest,
        OAuthStore,
        exchange_token,
        validate_authorize,
    )

    store = OAuthStore()
    cid = store.chatgpt_client_id(GATE_HOME)
    verifier, challenge = _pkce_pair()
    req = validate_authorize(
        store,
        GATE_HOME,
        {
            "client_id": cid,
            "redirect_uri": CHATGPT_REDIRECT,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": " ".join(requested),
            "resource": mcp_resource,
        },
        mcp_resource=mcp_resource,
    )
    if not isinstance(req, AuthorizeRequest):
        return {"ok": False, "stage": "authorize", "gate": dict(vars(req))}
    code = store.issue_code(GATE_HOME, req, user_id=user_id, role="admin")
    res = exchange_token(
        store,
        GATE_HOME,
        {
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": code,
            "redirect_uri": CHATGPT_REDIRECT,
            "code_verifier": verifier,
            "resource": mcp_resource,
        },
        mcp_resource=mcp_resource,
    )
    body = dict(res.body)
    token = str(body.pop("access_token", "") or "")
    body.pop("refresh_token", None)
    return {
        "ok": bool(res.ok) and bool(token),
        "stage": "token",
        "client_id": cid,
        "requested_scope": sorted(requested),
        "granted_at_authorize": sorted(req.scope),
        "registered_client_scope_after_reconcile": sorted(req.client_scope),
        # The token body VERBATIM, minus the two secrets — the only fields a
        # committed receipt must never carry.
        "token_response": body,
        "issued_scope": sorted(str(body.get("scope") or "").split()),
        "_token": token,
    }


# ── the WEB dashboard (real HTTP /authorize, then a real token) ────────────
#
# THE WEB LEG IS SHAPED DIFFERENTLY FROM THE CHATGPT ONE ON PURPOSE. ChatGPT
# reconciles INSIDE validate_authorize, so calling that function is enough to
# exercise it. The web client CANNOT be reconciled there: its registrar takes a
# base_url and puts it in the redirect allowlist, and the only base_url
# available inside the validator is the CALLER'S redirect_uri — seeding that
# would be redirect injection. So web is reconciled one frame out, in the
# authorize TRANSPORT HANDLER, from a SERVER-derived base_url. Calling
# validate_authorize directly would therefore skip the fix entirely and prove
# nothing: this leg goes over REAL HTTP to /oauth/authorize.

#: The scope the SERVED dashboard bundle requests (measured on the deployed
#: gate, build-info sha df094e2525).
WEB_BUNDLE_REQUEST = [
    "catalog",
    "tier_r_invoke",
    "status",
    "project_import",
    "sync",
    "xaacp_write",
]

#: An attacker-controlled callback, registered for nothing. The negative below
#: requires that presenting it at /authorize does NOT make it registered.
WEB_EVIL_REDIRECT = "https://evil.example.invalid/steal"


def _freeze_web_client(base_url: str) -> list[str]:
    """Register the web client, then force it back to the PRE-RULING scope.

    This is the deployed state, reproduced exactly (SSH-measured on release
    df094e252): the row EXISTS — so a create-only heal is a no-op — and its
    scope is frozen at an older build's four.
    """
    import sqlite3

    from aidocs_mcp.outer_gate_oauth import WEB_DASHBOARD_CLIENT_ID, OAuthStore

    store = OAuthStore()
    store.init_db(GATE_HOME)
    store.ensure_web_dashboard_client(GATE_HOME, base_url=base_url)
    stale = ["catalog", "project_import", "status", "tier_r_invoke"]
    with sqlite3.connect(str(store.db_path(GATE_HOME))) as conn:
        conn.execute(
            "UPDATE oauth_clients SET scope_json=? WHERE client_id=?",
            (json.dumps(stale), WEB_DASHBOARD_CLIENT_ID),
        )
        conn.commit()
    row = store.get_client(GATE_HOME, WEB_DASHBOARD_CLIENT_ID) or {}
    return sorted(row.get("scope") or [])


def _web_client_row() -> dict:
    from aidocs_mcp.outer_gate_oauth import WEB_DASHBOARD_CLIENT_ID, OAuthStore

    row = OAuthStore().get_client(GATE_HOME, WEB_DASHBOARD_CLIENT_ID) or {}
    return {
        "client_id": WEB_DASHBOARD_CLIENT_ID,
        "scope": sorted(row.get("scope") or []),
        "redirect_uris": list(row.get("redirect_uris") or []),
    }


def _http_authorize(origin: str, redirect_uri: str) -> dict:
    """A REAL HTTP GET /oauth/authorize as the browser dashboard makes it."""
    from aidocs_mcp.outer_gate_oauth import WEB_DASHBOARD_CLIENT_ID

    _, challenge = _pkce_pair()
    qs = urllib.parse.urlencode(
        {
            "client_id": WEB_DASHBOARD_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": " ".join(WEB_BUNDLE_REQUEST),
            "state": "webproof",
        }
    )
    req = urllib.request.Request(f"{origin}/oauth/authorize?{qs}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    return {
        "request": {"redirect_uri": redirect_uri, "scope": WEB_BUNDLE_REQUEST},
        "status": status,
        # The login page is a whole HTML document; a prefix is the useful part.
        "body_head": body[:300],
    }


def _web_oauth_token(user_id: str, mcp_resource: str, redirect_uri: str) -> dict:
    """Mint the credential a signed-in dashboard holds, on the RECONCILED row.

    The reconcile itself already happened over HTTP above; this only completes
    the code + PKCE exchange so the issued scope can be measured and USED.
    """
    from aidocs_mcp.outer_gate_oauth import (
        WEB_DASHBOARD_CLIENT_ID,
        AuthorizeRequest,
        OAuthStore,
        exchange_token,
        validate_authorize,
    )

    store = OAuthStore()
    verifier, challenge = _pkce_pair()
    req = validate_authorize(
        store,
        GATE_HOME,
        {
            "client_id": WEB_DASHBOARD_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": " ".join(WEB_BUNDLE_REQUEST),
            "resource": mcp_resource,
        },
        mcp_resource=mcp_resource,
    )
    if not isinstance(req, AuthorizeRequest):
        return {"ok": False, "stage": "authorize", "gate": dict(vars(req))}
    code = store.issue_code(GATE_HOME, req, user_id=user_id, role="admin")
    res = exchange_token(
        store,
        GATE_HOME,
        {
            "grant_type": "authorization_code",
            "client_id": WEB_DASHBOARD_CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": mcp_resource,
        },
        mcp_resource=mcp_resource,
    )
    body = dict(res.body)
    token = str(body.pop("access_token", "") or "")
    body.pop("refresh_token", None)
    return {
        "ok": bool(res.ok) and bool(token),
        "stage": "token",
        "client_id": WEB_DASHBOARD_CLIENT_ID,
        "requested_scope": sorted(WEB_BUNDLE_REQUEST),
        "registered_client_scope_after_reconcile": sorted(req.client_scope),
        "token_response": body,
        "issued_scope": sorted(str(body.get("scope") or "").split()),
        "_token": token,
    }


# ── the legs ───────────────────────────────────────────────────────────────

def leg(
    name: str,
    sender: Caller,
    receiver: Caller,
    target: str,
    *,
    send_lane: str = "",
    recv_lane: str = "",
) -> bool:
    print(f"\n-- leg: {name} --")
    send_args = dict(
        mode="xaacp_send",
        session_id=SESSION,
        target_actor_id=target,
        message_kind="question",
        body=f"{name}: ping",
    )
    if send_lane:
        send_args["lane_id"] = send_lane
    lane = recv_lane
    sent = sender.msg(**send_args)
    show("send", sent)
    ok_send = check(
        f"{name}: send admitted by the gate",
        bool(sent.get("ok")) and bool(sent.get("message_id")),
        json.dumps(sent)[:400],
    )
    mid = str(sent.get("message_id") or "")

    inbox_args = dict(mode="xaacp_inbox", session_id=SESSION, mark_read=True)
    if lane:
        inbox_args["lane_id"] = lane
    inbox = receiver.msg(**inbox_args)
    show("inbox", inbox)
    ids = _msg_ids(inbox)
    ok_inbox = check(
        f"{name}: receiver inbox carries the message",
        bool(mid) and mid in ids,
        json.dumps(inbox)[:400],
    )

    rep = receiver.msg(
        mode="xaacp_reply", session_id=SESSION, message_id=mid, decision="accepted",
        body=f"{name}: pong",
    )
    show("reply", rep)
    ok_reply = check(
        f"{name}: receiver reply admitted", bool(rep.get("ok")), json.dumps(rep)[:400]
    )
    return ok_send and ok_inbox and ok_reply


def main() -> int:
    print(f"scratch tree: {TMP}")
    uid, full, noscope, medit_only = _provision()
    print(f"minted operator token scopes: {full.scope}")
    print(f"minted under-scoped token   : {noscope.scope}")
    print(f"minted tier_m_edit-only token: {medit_only.scope}")
    TOKEN_SCOPES.update(
        {
            "messaging_legs + seat (positive)": list(full.scope),
            "under-scoped negative (no xaacp_write) + seat READ": list(noscope.scope),
            "no-transition-grant negative (tier_m_edit, no xaacp_write)": list(
                medit_only.scope
            ),
        }
    )
    srv, url = _serve()
    print(f"local outer gate: {url}")
    try:
        conductor = Caller(
            url, full.token,
            {"aidocs/hostSession": "cc-window-A", "aidocs/hostKind": "claude_code"},
            "conductor",
        )
        subagent = Caller(
            url, full.token,
            {
                "aidocs/hostSession": "cc-window-A",
                "aidocs/hostKind": "claude_code",
                "aidocs/hostAgent": SUBAGENT_HOST_AGENT,
            },
            "subagent",
        )
        lane = Caller(
            url, full.token,
            {"aidocs/hostSession": "lane-window-B", "aidocs/hostKind": "claude_code"},
            "lane-worker",
        )

        print("\n== discovering the gate-composed host identities ==")
        hsid_c = _hsid_seen_by_gate(conductor)
        hsid_s = _hsid_seen_by_gate(subagent)
        hsid_l = _hsid_seen_by_gate(lane)
        check("gate composed a host_session_id for the conductor", bool(hsid_c), hsid_c)
        check("subagent shares its parent's window", hsid_s == hsid_c, f"{hsid_s} vs {hsid_c}")
        check("lane worker has its own window", bool(hsid_l) and hsid_l != hsid_c, hsid_l)

        print("\n== binding each caller's session THROUGH the gate ==")
        for c in (conductor, lane):
            show(f"{c.label} ai_session(connect)",
                 c.raw("ai_session", {"mode": "connect", "session_id": SESSION}))
        # Belt and braces: the per-conductor managed binding is normally written
        # by the host's own SessionStart. Write it directly for any caller the
        # gate-side connect did not bind, so the legs test XAACP and not session
        # provisioning.
        _bind_managed(hsid_c)
        _bind_managed(hsid_l)
        worker_id = _register_lane_worker(hsid_l)
        print(f"lane worker_id = {worker_id}")

        print("\n== directory through the gate ==")
        d = conductor.msg(mode="xaacp_directory", session_id=SESSION)
        show("conductor directory (pre-traffic)", d)
        # every actor must speak once so its row exists
        subagent.msg(mode="xaacp_directory", session_id=SESSION)
        lane.msg(mode="xaacp_directory", session_id=SESSION)
        d = conductor.msg(mode="xaacp_directory", session_id=SESSION)
        show("conductor directory", d)
        actors = {a["actor_id"]: a for a in (d.get("actors") or [])}
        subs = [a for a in actors.values() if a.get("actor_kind") == "subagent"]
        conds = [
            a for a in actors.values()
            if a.get("actor_kind") not in ("subagent", "lane_worker")
            and a.get("host_session_id") == hsid_c
        ]
        check(
            "directory lists the subagent as its OWN actor "
            "(actor_kind=subagent, non-empty host_agent_id)",
            len(subs) == 1 and subs[0].get("host_agent_id") == SUBAGENT_HOST_AGENT,
            json.dumps(subs)[:400],
        )
        check(
            "subagent actor_id is distinct from the conductor's",
            bool(conds) and bool(subs) and conds[0]["actor_id"] != subs[0]["actor_id"],
            f"conductor={[c['actor_id'] for c in conds]} subagent={[s['actor_id'] for s in subs]}",
        )
        check(
            "directory lists the lane worker",
            any(a.get("actor_kind") in ("lane_worker", "worker") for a in actors.values()),
            json.dumps([a for a in actors.values()
                        if a.get("actor_kind") in ("lane_worker", "worker")])[:300],
        )

        sub_actor = subs[0]["actor_id"] if subs else ""
        cond_actor = conds[0]["actor_id"] if conds else ""

        # A lane worker may only address a SEAT upward (a lane cannot know the
        # conductor's derived actor_id), so the conductor takes its seat
        # THROUGH the gate before the upward leg.
        # ── #1021: THE SEAT PATH, OVER HTTP, WITH NO WORKAROUND ────────────
        #
        # THERE IS DELIBERATELY NO FALLBACK HERE ANY MORE. This block used to
        # mint a tier_m_edit token for the seat and, failing that, call
        # `conductor_comms.xaacp_claim_seat()` IN-PROCESS. Both hid the very
        # path under test: the first proved the OLD coupling still worked, the
        # second proved nothing at all while still letting the upward leg go
        # green. A fallback that conceals a broken gate path is worse than no
        # test, so a seat failure now fails LOUDLY and the run stops.
        #
        # NOTE ALSO: no ai_task(mode='begin') precedes the seat. That is check
        # (e) — ai_seat joined `_TASK_GATE_EXEMPT`, and until now that half of
        # #1021 had only ever been asserted under the audit-dev-mode bypass.
        # Seating here with NO active task exercises the real task gate.
        print("\n== #1021 (a)+(e): conductor seats over HTTP on xaacp_write, no task ==")
        assert "tier_m_edit" not in set(full.scope), (
            "the positive seat token must NOT carry tier_m_edit, or (a) proves nothing"
        )
        seat_env = conductor.call("ai_seat", {"mode": "enter", "session_id": SESSION})
        seat_payload = _payload(seat_env) or {}
        show("ai_seat(enter) on xaacp_write, NO active task", seat_payload or seat_env)
        seated = "error" not in seat_env and not str(seat_payload.get("error") or "")
        check(
            "#1021(a)+(e): ai_seat(enter) over HTTP on a token with xaacp_write and "
            "WITHOUT tier_m_edit, with NO active task, SUCCEEDS",
            seated,
            json.dumps(seat_env)[:400],
            verbatim=seat_env,
        )
        if not seated:
            _p = _write_receipt()
            raise SystemExit(
                "FATAL: the seat could not be established over HTTP. No local "
                "fallback exists by design (#1021) -- the upward lane leg below "
                "would otherwise be measured against a fabricated seat.\n"
                f"gate said: {json.dumps(seat_env)}\nreceipt: {_p}"
            )

        # co-enter: the second write mode, on the same scope, from a second
        # window (a co-conductor is a distinct host session by construction).
        coco = Caller(
            url, full.token,
            {"aidocs/hostSession": "cc-window-C", "aidocs/hostKind": "claude_code"},
            "co-conductor",
        )
        hsid_cc = _hsid_seen_by_gate(coco)
        _bind_managed(hsid_cc)
        co_env = coco.call("ai_seat", {"mode": "co-enter", "session_id": SESSION})
        co_payload = _payload(co_env) or {}
        show("ai_seat(co-enter) on xaacp_write", co_payload or co_env)
        check(
            "#1021(a): ai_seat(co-enter) over HTTP on xaacp_write (no tier_m_edit) "
            "is NOT refused",
            "error" not in co_env and not str(co_payload.get("error") or ""),
            json.dumps(co_env)[:400],
            verbatim=co_env,
        )

        # (b) THE NEGATIVE. tier_r_invoke but no xaacp_write must be refused,
        # and Law 311bf3e6 says the refusal must NAME the reachable remedy.
        print("\n== #1021 (b): no xaacp_write => ai_seat(enter) refused, naming it ==")
        weak_seat = Caller(
            url, noscope.token, dict(conductor.meta), "conductor(no xaacp_write)"
        )
        neg_env = weak_seat.raw("ai_seat", {"mode": "enter", "session_id": SESSION})
        show("ai_seat(enter) without xaacp_write", neg_env)
        neg_err = (neg_env.get("error") or {}) if isinstance(neg_env, dict) else {}
        check(
            "#1021(b): ai_seat(enter) WITHOUT xaacp_write is refused over HTTP "
            "with insufficient_scope (-32001)",
            neg_err.get("code") == -32001
            and neg_err.get("message") == "insufficient_scope",
            json.dumps(neg_env)[:400],
            verbatim=neg_env,
        )
        check(
            "#1021(b): the refusal NAMES 'xaacp_write' (error.data), so the "
            "remedy it points at is the one that actually works",
            "xaacp_write" in json.dumps(neg_env),
            json.dumps(neg_env)[:400],
            verbatim=neg_env,
        )

        # (c) NO TRANSITION GRANT — the load-bearing one. If tier_m_edit still
        # seated, the coupling would merely have been WIDENED, not severed.
        print("\n== #1021 (c): NO TRANSITION GRANT -- tier_m_edit alone is refused ==")
        medit_caller = Caller(
            url, medit_only.token, dict(conductor.meta), "conductor(tier_m_edit only)"
        )
        tg_env = medit_caller.raw("ai_seat", {"mode": "enter", "session_id": SESSION})
        show("ai_seat(enter) on tier_m_edit WITHOUT xaacp_write", tg_env)
        tg_err = (tg_env.get("error") or {}) if isinstance(tg_env, dict) else {}
        check(
            "#1021(c) NO TRANSITION GRANT: ai_seat(enter) on a token holding "
            "tier_m_edit but NOT xaacp_write is ALSO refused over HTTP",
            tg_err.get("code") == -32001
            and tg_err.get("message") == "insufficient_scope"
            and "xaacp_write" in json.dumps(tg_env),
            json.dumps(tg_env)[:400],
            verbatim=tg_env,
        )

        # (d) the read/write split is real: a seat READ on tier_r_invoke alone.
        print("\n== #1021 (d): seat READS need only tier_r_invoke ==")
        read_env = weak_seat.raw("ai_seat", {"mode": "status", "session_id": SESSION})
        show("ai_seat(status) on tier_r_invoke only", _payload(read_env) or read_env)
        read_err = (read_env.get("error") or {}) if isinstance(read_env, dict) else {}
        check(
            "#1021(d): ai_seat(mode='status') SUCCEEDS on a tier_r_invoke-only "
            "token (reads and writes are priced separately)",
            read_err.get("message") != "insufficient_scope" and "error" not in read_env,
            json.dumps(read_env)[:400],
            verbatim=read_env,
        )

        print("\n== the six legs ==")
        leg("conductor -> subagent", conductor, subagent, sub_actor)
        leg("subagent -> conductor", subagent, conductor, cond_actor)
        leg("conductor -> lane worker", conductor, lane, worker_id,
            send_lane=LANE, recv_lane=LANE)

        # ── #1022: RECIPIENT IDENTITY IS AUTHORITATIVE ─────────────────────
        # A worker MUST stamp its lane on send (its route IS its lane), so the
        # upward message below is stored with lane_id='lane-1'. Before the fix
        # xaacp_inbox matched `m.lane_id = ''` exactly, so the conductor's
        # lane-less read returned [] both BEFORE and AFTER delivery: accepted,
        # stored, invisible. It must now span every lane addressed to it.
        blind = conductor.msg(mode="xaacp_inbox", session_id=SESSION, mark_read=False)
        upward = lane.msg(
            mode="xaacp_send", session_id=SESSION, target_actor_id="conductor",
            lane_id=LANE, message_kind="question",
            body="lane worker -> conductor: upward report (laneless visibility)",
        )
        show("lane worker upward send", upward)
        upward_id = str(upward.get("message_id") or "")
        check(
            "#1022: the lane worker's upward send is admitted and stored",
            bool(upward.get("ok")) and bool(upward_id),
            json.dumps(upward)[:400],
        )
        after_blind = conductor.msg(mode="xaacp_inbox", session_id=SESSION,
                                    unread_only=False, mark_read=False)
        show("conductor laneless inbox (before/after the lane report)",
             {"before": blind, "after": after_blind})
        check(
            "#1022: the conductor's LANE-LESS inbox CONTAINS the lane worker's "
            "upward message (recipient identity, not lane, decides visibility)",
            upward_id in _msg_ids(after_blind),
            json.dumps(after_blind)[:400],
        )
        check(
            "#1022: the same message was NOT visible before it was sent "
            "(the laneless read is not returning everything indiscriminately)",
            upward_id not in _msg_ids(blind),
            json.dumps(blind)[:300],
        )

        # NEGATIVE 1 -- CROSS-ACTOR. Same session, same lane, addressed to the
        # SUBAGENT. The conductor's laneless read must not see it: dropping the
        # lane predicate must never widen across target_actor_id.
        #
        # Seeded store-level, deliberately. A gate-side send to a subagent ON a
        # lane is REFUSED outright ("no XAACP actor exists on the exact
        # session/lane route") -- which is a real second wall, but it means no
        # row would exist to be hidden, and a negative over an absent row proves
        # nothing. Writing the row directly is the STRONGER test: the message is
        # present in the store and must still be invisible to the gate-side read.
        from aidocs_mcp.conductor_comms import xaacp_send as _local_send

        other_actor_msg = _local_send(
            PROJ, session_id=SESSION, sender_actor_id=worker_id,
            target_actor_id=sub_actor, lane_id=LANE, message_kind="question",
            body="addressed to the SUBAGENT on lane-1, not the conductor",
            sender_actor_kind="lane_worker",
        )
        show("cross-actor seed (store-level, to the subagent, same lane)",
             other_actor_msg)
        other_actor_id = str(other_actor_msg.get("message_id") or "")
        check(
            "#1022 NEGATIVE setup: the cross-actor row really exists in the store",
            bool(other_actor_msg.get("ok")) and bool(other_actor_id),
            json.dumps(other_actor_msg)[:300],
        )
        leak_actor = conductor.msg(mode="xaacp_inbox", session_id=SESSION,
                                   unread_only=False, mark_read=False)
        show("conductor laneless inbox after the cross-actor seed", leak_actor)
        check(
            "#1022 NEGATIVE (cross-actor): a message addressed to ANOTHER actor "
            "in the same session and lane is NOT in the conductor's laneless inbox",
            bool(other_actor_id) and other_actor_id not in _msg_ids(leak_actor),
            json.dumps(leak_actor)[:400],
        )
        check(
            "#1022 NEGATIVE control: that same laneless read DOES still carry "
            "the conductor's own lane report (the negative is not vacuous)",
            upward_id in _msg_ids(leak_actor),
            json.dumps(_msg_ids(leak_actor)),
        )

        # NEGATIVE 2 -- CROSS-SESSION. Same target_actor_id, different session.
        # Seeded directly into the store because the gate binds a caller to one
        # session by design; the point is that even with the row present, the
        # gate-side laneless READ must not surface it.
        elsewhere = _local_send(
            PROJ, session_id=OTHER_SESSION, sender_actor_id=sub_actor,
            target_actor_id=cond_actor, lane_id=LANE, message_kind="question",
            body="same actor id, DIFFERENT session", sender_actor_kind="lane_worker",
        )
        show("cross-session seed (store-level)", elsewhere)
        elsewhere_id = str(elsewhere.get("message_id") or "")
        leak_session = conductor.msg(mode="xaacp_inbox", session_id=SESSION,
                                     unread_only=False, mark_read=False)
        show("conductor laneless inbox after the cross-session seed", leak_session)
        check(
            "#1022 NEGATIVE (cross-session): a message addressed to the SAME "
            "actor id in a DIFFERENT session is NOT in the conductor's laneless inbox",
            bool(elsewhere_id) and elsewhere_id not in _msg_ids(leak_session),
            json.dumps(leak_session)[:400],
        )

        # NARROWING STILL WORKS: naming a lane is a request, not a bug.
        narrowed = conductor.msg(mode="xaacp_inbox", session_id=SESSION,
                                 lane_id="lane-does-not-exist",
                                 unread_only=False, mark_read=False)
        show("conductor inbox narrowed to an unused lane", narrowed)
        check(
            "#1022: a lane-less reader that NAMES a lane still gets exactly "
            "that lane (an explicit filter still narrows)",
            upward_id not in _msg_ids(narrowed),
            json.dumps(narrowed)[:300],
        )

        # ── THE CHATGPT CONNECTOR LEG (operator ruling 2026-09-04) ─────────
        #
        # The other legs run on tokens minted DIRECTLY by OuterGateTokenStore,
        # which proves the scope wall but says nothing about whether a real
        # ChatGPT authorization ever ISSUES the messaging scope. That was the
        # actual defect: xaacp_write existed, the wall honoured it, and the
        # connector could still never obtain it — its client row was registered
        # without the scope and /authorize never reconciled an existing row.
        #
        # So this leg mints its credential the way ChatGPT does: real
        # validate_authorize, real auth code, real PKCE exchange_token. The
        # client row is deliberately FORCED BACK to a pre-ruling scope first
        # (see _bind_chatgpt_connector), so a green result here can only mean
        # the authorize-time reconcile ran.
        print("\n== ChatGPT connector: real OAuth authorize + token, then a real send ==")
        # RFC 8707: an OAuth-issued token is RESOURCE-BOUND, and the gate builds
        # the route's resource from the request's Host header as
        # `https://<host>/v1/mcp` -- always https, whatever scheme the socket
        # actually speaks. Mint against that exact string or every call comes
        # back 403 resource_mismatch before the scope wall is ever reached.
        cg_resource = f"https://{urllib.parse.urlsplit(url).netloc}/v1/mcp"
        cg_cid, cg_frozen = _bind_chatgpt_connector()
        print(f"chatgpt client_id={cg_cid} forced to stale scope {cg_frozen}")
        check(
            "ChatGPT setup: the connector row really was frozen at a pre-ruling "
            "scope (so the positive below cannot pass without a reconcile)",
            "xaacp_write" not in cg_frozen,
            json.dumps(cg_frozen),
            verbatim={"client_id": cg_cid, "stored_scope_before_authorize": cg_frozen},
        )

        cg_pos = _chatgpt_oauth_token(uid, cg_resource, CHATGPT_POSITIVE_REQUEST)
        cg_token = str(cg_pos.pop("_token", "") or "")
        show("chatgpt authorize+token (positive)", cg_pos)
        TOKEN_SCOPES["chatgpt connector, OAuth-issued (positive)"] = list(
            cg_pos.get("issued_scope") or []
        )
        cg_issued = set(cg_pos.get("issued_scope") or [])
        check(
            "ChatGPT connector: a FRESH authorization issues a token whose scope "
            "CONTAINS xaacp_write (the authorize-time reconcile reached it)",
            bool(cg_pos.get("ok")) and "xaacp_write" in cg_issued,
            json.dumps(cg_pos)[:400],
            verbatim=cg_pos,
        )
        check(
            "ChatGPT connector: that same issued scope does NOT contain "
            "tier_m_edit (messaging authority is not edit authority)",
            "tier_m_edit" not in cg_issued,
            json.dumps(sorted(cg_issued)),
            verbatim=cg_pos.get("token_response"),
        )
        check(
            "ChatGPT connector: the RECONCILED client row now carries xaacp_write "
            "(it was frozen without it one call ago)",
            "xaacp_write"
            in set(cg_pos.get("registered_client_scope_after_reconcile") or []),
            json.dumps(cg_pos.get("registered_client_scope_after_reconcile")),
            verbatim=cg_pos.get("registered_client_scope_after_reconcile"),
        )

        # And now the thing the operator actually asked for: a real send, over
        # HTTP, on that freshly authorized connector token.
        chatgpt = Caller(
            url, cg_token,
            {"aidocs/hostSession": "chatgpt-window-D", "aidocs/hostKind": "claude_code"},
            "chatgpt-connector",
        )
        hsid_cg = _hsid_seen_by_gate(chatgpt)
        _bind_managed(hsid_cg)
        chatgpt.msg(mode="xaacp_directory", session_id=SESSION)
        cg_send = chatgpt.msg(
            mode="xaacp_send",
            session_id=SESSION,
            target_actor_id=cond_actor,
            message_kind="question",
            body="chatgpt connector -> conductor: sent on an OAuth-issued token",
        )
        show("chatgpt xaacp_send on the OAuth-issued token", cg_send)
        check(
            "ChatGPT connector: ai_msg(xaacp_send) over HTTP on the "
            "OAuth-ISSUED token returns ok:true",
            bool(cg_send.get("ok")) and bool(cg_send.get("message_id")),
            json.dumps(cg_send)[:400],
            verbatim=cg_send,
        )

        # THE NEGATIVE: the SAME client, authorized for tier_m_edit and NOT
        # xaacp_write. If this could send, the two scopes would merely have been
        # renamed rather than separated — there must be NO TRANSITION GRANT.
        cg_neg = _chatgpt_oauth_token(uid, cg_resource, CHATGPT_NEGATIVE_REQUEST)
        cg_neg_token = str(cg_neg.pop("_token", "") or "")
        show("chatgpt authorize+token (tier_m_edit, no xaacp_write)", cg_neg)
        TOKEN_SCOPES["chatgpt connector, OAuth-issued (no-transition-grant negative)"] = (
            list(cg_neg.get("issued_scope") or [])
        )
        cg_neg_issued = set(cg_neg.get("issued_scope") or [])
        check(
            "ChatGPT NEGATIVE setup: the second authorization issued tier_m_edit "
            "and did NOT issue xaacp_write",
            bool(cg_neg.get("ok"))
            and "tier_m_edit" in cg_neg_issued
            and "xaacp_write" not in cg_neg_issued,
            json.dumps(cg_neg)[:400],
            verbatim=cg_neg,
        )
        cg_neg_caller = Caller(
            url, cg_neg_token, dict(chatgpt.meta), "chatgpt(tier_m_edit only)"
        )
        cg_neg_env = cg_neg_caller.raw(
            "ai_msg",
            {
                "mode": "xaacp_send",
                "session_id": SESSION,
                "target_actor_id": cond_actor,
                "message_kind": "question",
                "body": "must be refused: tier_m_edit is not messaging authority",
            },
        )
        show("chatgpt send on tier_m_edit WITHOUT xaacp_write", cg_neg_env)
        cg_neg_err = (cg_neg_env.get("error") or {}) if isinstance(cg_neg_env, dict) else {}
        check(
            "ChatGPT NO TRANSITION GRANT: xaacp_send on a ChatGPT-issued token "
            "holding tier_m_edit but NOT xaacp_write is REFUSED over HTTP with "
            "insufficient_scope (-32001), naming xaacp_write",
            cg_neg_err.get("code") == -32001
            and cg_neg_err.get("message") == "insufficient_scope"
            and "xaacp_write" in json.dumps(cg_neg_env),
            json.dumps(cg_neg_env)[:400],
            verbatim=cg_neg_env,
        )

        # ── THE WEB DASHBOARD LEG (#1022, SSH-measured 2026-09-04) ─────────
        #
        # THE LAST SURFACE. Desktop got its authorize-time reconcile in #1019
        # and ChatGPT above; the WEB dashboard was still broken IN PRODUCTION.
        # The served bundle requested six scopes, `ogcid_webdashboard` still
        # held four, and since the grant is `requested ∩ client.scope` a fresh
        # sign-in in a brand-new browser session still could not send:
        # "insufficient_scope - token lacks xaacp_write scope".
        #
        # ensure_web_dashboard_client DID run on every GET / — but with THAT
        # request's project_root, which need not be the root the authorize path
        # reads the client from. A reconcile against a database the grant never
        # consults changes nothing. The fix moved it to the READ SITE: the
        # authorize transport handler, from a SERVER-derived base_url.
        #
        # So this leg drives REAL HTTP /oauth/authorize (not validate_authorize
        # — that would skip the handler and prove nothing), against a row
        # deliberately forced back to the pre-ruling four.
        print("\n== web dashboard: real HTTP /authorize reconcile, then a real send ==")
        web_origin = f"http://{urllib.parse.urlsplit(url).netloc}"
        # The handler derives base_url from the request Host, ALWAYS https —
        # exactly as _ogt_webapp does. So the only registrable redirect is this.
        web_redirect = f"https://{urllib.parse.urlsplit(url).netloc}/"
        web_frozen = _freeze_web_client(web_redirect.rstrip("/"))
        print(f"web client forced to stale scope {web_frozen}")
        check(
            "WEB setup: the dashboard row really was frozen at the pre-ruling "
            "scope (so the positive below cannot pass without a reconcile)",
            "xaacp_write" not in web_frozen and "sync" not in web_frozen,
            json.dumps(web_frozen),
            verbatim={"stored_scope_before_authorize": web_frozen},
        )

        web_auth = _http_authorize(web_origin, web_redirect)
        show("web GET /oauth/authorize (positive)", web_auth)
        web_after = _web_client_row()
        check(
            "WEB: a real HTTP GET /oauth/authorize is served (200 login page)",
            web_auth["status"] == 200,
            json.dumps(web_auth)[:400],
            verbatim=web_auth,
        )
        check(
            "WEB: that authorize request RECONCILED the client row — it now "
            "carries xaacp_write, one call after being frozen without it",
            "xaacp_write" in web_after["scope"] and "sync" in web_after["scope"],
            json.dumps(web_after),
            verbatim=web_after,
        )

        web_pos = _web_oauth_token(uid, cg_resource, web_redirect)
        web_token = str(web_pos.pop("_token", "") or "")
        show("web authorize+token (positive)", web_pos)
        TOKEN_SCOPES["web dashboard, OAuth-issued (positive)"] = list(
            web_pos.get("issued_scope") or []
        )
        web_issued = set(web_pos.get("issued_scope") or [])
        check(
            "WEB: the ISSUED token scope CONTAINS xaacp_write (this is the exact "
            "grant that was missing in production)",
            bool(web_pos.get("ok")) and "xaacp_write" in web_issued,
            json.dumps(web_pos)[:400],
            verbatim=web_pos,
        )
        check(
            "WEB: that issued scope does NOT contain tier_m_edit (the dashboard "
            "gained messaging authority, not edit authority)",
            "tier_m_edit" not in web_issued,
            json.dumps(sorted(web_issued)),
            verbatim=web_pos.get("token_response"),
        )

        webapp = Caller(
            url, web_token,
            {"aidocs/hostSession": "web-window-E", "aidocs/hostKind": "claude_code"},
            "web-dashboard",
        )
        hsid_web = _hsid_seen_by_gate(webapp)
        _bind_managed(hsid_web)
        webapp.msg(mode="xaacp_directory", session_id=SESSION)
        web_send = webapp.msg(
            mode="xaacp_send",
            session_id=SESSION,
            target_actor_id=cond_actor,
            message_kind="question",
            body="web dashboard -> conductor: sent on an OAuth-issued token",
        )
        show("web xaacp_send on the OAuth-issued token", web_send)
        check(
            "WEB: ai_msg(xaacp_send) over HTTP on the OAuth-ISSUED dashboard "
            "token returns ok:true",
            bool(web_send.get("ok")) and bool(web_send.get("message_id")),
            json.dumps(web_send)[:400],
            verbatim=web_send,
        )

        # THE INJECTION NEGATIVE — the load-bearing one. If the reconcile used
        # the CALLER'S redirect_uri as base_url (the only one available from
        # caller input), this request would REGISTER the attacker's callback
        # and then deliver auth codes to it. The base_url must be SERVER-derived.
        web_inj = _http_authorize(web_origin, WEB_EVIL_REDIRECT)
        web_after_inj = _web_client_row()
        show("web GET /oauth/authorize with an ATTACKER redirect", web_inj)
        check(
            "WEB INJECTION NEGATIVE: an unregistered caller redirect_uri is "
            "refused with a plain 400 and NEVER redirected to",
            web_inj["status"] == 400,
            json.dumps(web_inj)[:400],
            verbatim=web_inj,
        )
        check(
            "WEB INJECTION NEGATIVE: the attacker's redirect_uri was NOT seeded "
            "into the client's allowlist — every registered redirect is "
            "server-derived",
            WEB_EVIL_REDIRECT not in web_after_inj["redirect_uris"]
            and web_after_inj["redirect_uris"] == [web_redirect],
            json.dumps(web_after_inj),
            verbatim=web_after_inj,
        )

        print("\n== the scope wall (real, over the wire) ==")
        weak = Caller(url, noscope.token, dict(conductor.meta), "conductor(no xaacp_write)")
        env = weak.raw(
            "ai_msg",
            {
                "mode": "xaacp_send",
                "session_id": SESSION,
                "target_actor_id": sub_actor,
                "message_kind": "question",
                "body": "must be refused",
            },
        )
        show("under-scoped send", env)
        err = (env.get("error") or {}) if isinstance(env, dict) else {}
        blob = json.dumps(env)
        check(
            "under-scoped send is refused with insufficient_scope (-32001)",
            err.get("code") == -32001 and err.get("message") == "insufficient_scope",
            blob[:400],
        )
        check("the refusal names the required scope 'xaacp_write'", "xaacp_write" in blob, blob[:400])

        # control: the same call on the full token is NOT scope-refused
        ctl = conductor.raw(
            "ai_msg",
            {
                "mode": "xaacp_send",
                "session_id": SESSION,
                "target_actor_id": sub_actor,
                "message_kind": "question",
                "body": "control",
            },
        )
        check(
            "the SAME call with xaacp_write is not scope-refused",
            "error" not in ctl,
            json.dumps(ctl)[:300],
        )
    finally:
        srv.shutdown()
        srv.server_close()

    passed = sum(1 for r in RESULTS if r["pass"])
    print(f"\n=== gate round-trip proof: {passed}/{len(RESULTS)} passed ===")
    for r in RESULTS:
        if not r["pass"]:
            print(f"  FAIL {r['name']}: {r['detail'][:300]}")
    receipt_path = _write_receipt()
    print(f"receipt: {receipt_path}")
    print(f"(scratch tree left at {TMP} — delete when done)")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
