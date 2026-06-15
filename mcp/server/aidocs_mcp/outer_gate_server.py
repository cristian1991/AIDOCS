"""Outer Gate server entrypoint + scoped-token admin CLI.

Deployment runner (loopback-only) and the token-mint/revoke admin surface. This
is the ONLY place that constructs a LIVE OuterGate for serving — it is never
imported by the MCP stdio path, so local behavior is untouched.

  # run the loopback gate (codenexus deployment)
  python -m aidocs_mcp.outer_gate_server serve --project-root /home/app/AutoDeployBase \
      --host 127.0.0.1 --port 8787

  # mint a short-lived scoped token (authenticated admin)
  python -m aidocs_mcp.outer_gate_server mint --project-root <p> \
      --email admin@x --password - --scope catalog,status,tier_r_invoke --ttl 3600

  # revoke
  python -m aidocs_mcp.outer_gate_server revoke --project-root <p> --token-id <id>
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path


def build_serving_gate(project_root: Path, exec_project_root: Path | None = None):
    """Build the LIVE gate for serving: enabled, loopback bind, package-trust
    resolved by the real integrity check against project_root's signed trust row
    (unsigned/tampered/stale code ⇒ the gate refuses every request — the deed
    holds). A DURABLE audit sink is wired so admitted invocations satisfy the
    gate's audit-as-law floor.

    Execution: a REAL adapter runs the canonical AIDOCS tool implementations
    bound to ``exec_project_root`` (defaults to ``project_root``) — which must be
    a signed, indexed AIDOCS project or every call fails closed. Three SEPARATE,
    independently-scoped surfaces, each reusing the canonical law:
      * Tier-R reads (``tier_r_invoke``): allowlisted, side-effect-free.
      * Tier-M surgical edit (``tier_m_edit``): ``ai_str_replace`` via a two-phase
        content-bound confirm, protected-path + self-edit refusals, post-edit
        reindex, mutation/result audit.
      * Tier-M run (``project_run``): detached ``ai_run`` reusing bash_policy /
        heuristic_judge / freeze / output guard, host-session-isolated.
    Each surface requires its OWN scope (none implies another); config/freeze/
    escalation and any tool outside these allowlists remain unreachable.

    Host compatibility: install the stable ``aidocs_call`` dispatcher BEFORE the
    transport is imported, so MCP hosts (ChatGPT) get one always-present tool that
    routes to the canonical gate.invoke/edit/run law (no law bypass — every target
    still passes its own scope/trust/project/confirm/audit checks).
    """
    from .outer_gate_host_compat import install as _install_host_compat

    _install_host_compat()
    from .outer_gate import OuterGate
    from .outer_gate_executor import build_read_executor
    from .outer_gate_transport import default_transport_audit
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

    exec_root = exec_project_root or project_root
    hub = AidocsServiceHub(templates_root=project_root)
    runtime = RuntimeService(hub)
    return OuterGate(
        enabled=True,
        bind="127.0.0.1",
        project_root=project_root,
        audit_sink=default_transport_audit(project_root),
        executor=build_read_executor(exec_root),
        exec_project_root=exec_root,
        hub=hub,
        runtime=runtime,
    )


def _load_codenexus_git_token(project_root: Path) -> None:
    """Load the CodeNexus-OWNED GitHub token into CODENEXUS_GIT_TOKEN from an
    app-owned secret FILE when the env var isn't already set. Keeps the secret out
    of process args / pm2 env dumps (it lives 0600 on disk) while preserving the
    'CODENEXUS_GIT_TOKEN env' interface the importer reads. Never a caller token.
    """
    import os

    if os.environ.get("CODENEXUS_GIT_TOKEN"):
        return
    candidates = []
    f = os.environ.get("CODENEXUS_GIT_TOKEN_FILE")
    if f:
        candidates.append(Path(f))
    candidates.append(project_root.parent / ".gittoken")  # sibling of auth home
    for p in candidates:
        try:
            tok = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tok:
            os.environ["CODENEXUS_GIT_TOKEN"] = tok
            return


def cmd_serve(args: argparse.Namespace) -> int:
    from . import outer_gate_transport as T

    project_root = Path(args.project_root).resolve()
    exec_root = (
        Path(args.exec_project_root).resolve()
        if getattr(args, "exec_project_root", None)
        else project_root
    )
    _load_codenexus_git_token(project_root)
    host = args.host
    if not T.is_loopback_bind(host):
        print(f"refusing non-loopback bind {host!r}", file=sys.stderr)
        return 2
    gate = build_serving_gate(project_root, exec_root)
    auth = T.make_scoped_auth(project_root)
    server = T.serve(gate, project_root=project_root, host=host, port=args.port, auth_resolver=auth)
    bound = server.server_address
    print(
        f"outer-gate serving on http://{bound[0]}:{bound[1]} "
        f"(loopback-only; scoped-token auth; auth-home={project_root}; "
        f"exec-project={exec_root})",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def _authenticate_admin(project_root: Path, email: str, password: str):
    """Authenticate an operator and require admin/super_admin. Returns
    (user_id, role) or raises.
    """
    from .identity_store import IdentityStore

    user = IdentityStore().authenticate(project_root, email=email, password=password)
    if user is None:
        raise PermissionError("authentication failed")
    if str(user.role).lower() not in {"admin", "super_admin"}:
        raise PermissionError(f"role {user.role!r} cannot mint gate tokens")
    return user.user_id, user.role


def cmd_mint(args: argparse.Namespace) -> int:
    from .outer_gate_token_store import OuterGateTokenStore

    project_root = Path(args.project_root).resolve()
    password = args.password
    if password == "-":
        password = getpass.getpass("operator password: ")
    try:
        uid, role = _authenticate_admin(project_root, args.email, password)
    except PermissionError as exc:
        print(f"mint refused: {exc}", file=sys.stderr)
        return 1
    scope = [s.strip() for s in (args.scope or "").split(",") if s.strip()] or None
    try:
        minted = OuterGateTokenStore().mint_for_operator(
            project_root,
            user_id=uid,
            role=role,
            audience=args.audience,
            scope=scope,
            ttl_seconds=int(args.ttl),
        )
    except (PermissionError, ValueError) as exc:
        print(f"mint refused: {exc}", file=sys.stderr)
        return 1
    # The plaintext token is printed ONCE (never stored).
    print(f"token_id: {minted.token_id}")
    print(f"audience: {minted.audience}")
    print(f"scope:    {','.join(minted.scope)}")
    print(f"expires:  {minted.expires_at}")
    print(f"token:    {minted.token}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    from .outer_gate_token_store import OuterGateTokenStore

    project_root = Path(args.project_root).resolve()
    ok = OuterGateTokenStore().revoke(
        project_root,
        token=args.token or "",
        token_id=args.token_id or "",
    )
    print("revoked" if ok else "no matching active token")
    return 0 if ok else 1


def cmd_oauth_register_client(args: argparse.Namespace) -> int:
    """Manually register an OAuth client (no DCR). Prints client_id (+ secret for
    a confidential client). For ChatGPT's MCP connector, pass its exact callback
    URL as --redirect-uri and use --auth-method none (public PKCE client).
    """
    from .outer_gate_oauth import OAuthStore

    project_root = Path(args.project_root).resolve()
    redirects = [u.strip() for u in (args.redirect_uri or []) if u.strip()]
    try:
        out = OAuthStore().register_client(
            project_root,
            redirect_uris=redirects,
            auth_method=args.auth_method,
            scope=[s.strip() for s in (args.scope or "").split(",") if s.strip()] or None,
        )
    except ValueError as exc:
        print(f"client registration refused: {exc}", file=sys.stderr)
        return 1
    print(f"client_id:     {out['client_id']}")
    print(f"auth_method:   {out['auth_method']}")
    print(f"redirect_uris: {', '.join(out['redirect_uris'])}")
    print(f"scope:         {','.join(out['scope'])}")
    if "client_secret" in out:
        print(f"client_secret: {out['client_secret']}  (store securely; shown once)")
    return 0


def cmd_oauth_update_client(args: argparse.Namespace) -> int:
    """Replace a registered client's scope allowlist and/or set the first-party
    force_full_scope override. The operator path for granting tier_m_edit to the
    already-configured ChatGPT connector and for upgrading a connector that
    hardcodes a read-only scope request to its full registered scope.
    """
    from .outer_gate_oauth import OAuthStore

    project_root = Path(args.project_root).resolve()
    store = OAuthStore()
    scope = [s.strip() for s in (args.scope or "").split(",") if s.strip()]
    ffs = getattr(args, "force_full_scope", None)
    if not scope and ffs is None:
        print("nothing to update: pass --scope and/or --force-full-scope", file=sys.stderr)
        return 1
    try:
        if scope:
            store.update_client_scope(project_root, args.client_id, scope=scope)
        if ffs is not None:
            store.set_force_full_scope(project_root, args.client_id, enabled=(ffs == "on"))
        out = store.get_client(project_root, args.client_id)
    except ValueError as exc:
        print(f"client update refused: {exc}", file=sys.stderr)
        return 1
    if out is None:
        print(f"unknown client_id {args.client_id!r}", file=sys.stderr)
        return 1
    print(f"client_id: {out['client_id']}")
    print(f"scope:     {','.join(out['scope'])}")
    print(f"force_full_scope: {out.get('force_full_scope')}")
    return 0


def cmd_oauth_list_clients(args: argparse.Namespace) -> int:
    from .outer_gate_oauth import OAuthStore

    project_root = Path(args.project_root).resolve()
    for c in OAuthStore().list_clients(project_root):
        print(
            f"{c['client_id']}  [{c['auth_method']}]  "
            f"scope={','.join(c['scope'])}  "
            f"force_full_scope={c.get('force_full_scope')}  "
            f"redirects={','.join(c['redirect_uris'])}",
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="outer_gate_server")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument(
        "--project-root",
        required=True,
        help="auth/trust home (tokens, identity, signed trust DB)",
    )
    s.add_argument(
        "--exec-project-root",
        default=None,
        help="signed/indexed AIDOCS project bound for Tier-R execution "
        "(defaults to --project-root)",
    )
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(func=cmd_serve)

    m = sub.add_parser("mint")
    m.add_argument("--project-root", required=True)
    m.add_argument("--email", required=True)
    m.add_argument("--password", default="-")
    m.add_argument("--scope", default="catalog,status,tier_r_invoke")
    m.add_argument("--audience", default="codenexus-outer-gate")
    m.add_argument("--ttl", default="3600")
    m.set_defaults(func=cmd_mint)

    r = sub.add_parser("revoke")
    r.add_argument("--project-root", required=True)
    r.add_argument("--token", default="")
    r.add_argument("--token-id", default="")
    r.set_defaults(func=cmd_revoke)

    oc = sub.add_parser("oauth-register-client")
    oc.add_argument("--project-root", required=True)
    oc.add_argument(
        "--redirect-uri",
        action="append",
        required=True,
        help="exact callback URL to allowlist (repeatable)",
    )
    oc.add_argument("--auth-method", default="none", choices=["none", "client_secret_post"])
    oc.add_argument("--scope", default="catalog,status,tier_r_invoke")
    oc.set_defaults(func=cmd_oauth_register_client)

    ou = sub.add_parser(
        "oauth-update-client",
        help="replace an existing OAuth client's scope allowlist "
        "(e.g. add tier_m_edit) and/or set the first-party "
        "force-full-scope override, without changing client_id",
    )
    ou.add_argument("--project-root", required=True)
    ou.add_argument("--client-id", required=True)
    ou.add_argument(
        "--scope",
        default="",
        help="comma-separated new scope allowlist (subset of gate vocab)",
    )
    ou.add_argument(
        "--force-full-scope",
        choices=["on", "off"],
        default=None,
        help="on: issue this client its full registered scope even when "
        "it explicitly requests a narrower set (first-party connector "
        "that hardcodes a read-only scope request)",
    )
    ou.set_defaults(func=cmd_oauth_update_client)

    ol = sub.add_parser(
        "oauth-list-clients",
        help="list active OAuth clients (id, auth method, scope)",
    )
    ol.add_argument("--project-root", required=True)
    ol.set_defaults(func=cmd_oauth_list_clients)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
