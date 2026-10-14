"""Operator pairing script -- the M1 demo stand-in for the Connected Apps UI (S18).

Reviewer ruling: until the dashboard screen exists, the operator pairs an
AIDOCS application binding from the gate host with this script. It drives the
SAME S7 control-plane handlers (:class:`~.control_plane.ControlPlaneHandlers`)
the UI will use, so it inherits their credential-class check, org-admin check,
refusal codes and DECISION audit. It never touches a store directly, never
writes the database and is never an MCP tool (hidden or advertised).

Ceremony (RFC 0003 §5.8; wire rev 8 §3), matched to the CRM admin screen
(``Admin > Collegamento AIDOCS > Abbina con AIDOCS``):

1. ``pair`` issues the offer and prints it as ONE JSON line: the CRM admin
   pastes it into "Offerta AIDOCS".
2. The CRM shows the comparison code and "Risposta per AIDOCS" (one JSON
   object): the operator pastes it here (or passes ``--response-file``).
3. The script prints the exact org / pack / contract_digest / toolspace /
   application origin / audience / AIDOCS and CRM key fingerprints and the
   comparison code, and the operator must TYPE the comparison code the CRM
   shows. Anything else aborts. There is no ``--yes`` and no auto-confirm.
4. On acceptance the handlers confirm and the AIDOCS confirmation (a JWS) is
   printed: the CRM admin pastes it into "Incolla conferma AIDOCS".
5. ``activate`` is a separate act that also requires typing the binding id.

Usage (on the gate host)::

    python -m aidocs_mcp.app_packs.pair_cli --factory pkg.mod:build_handlers \\
        --operator <aidocs-user-id> pair --binding-id <bnd_...>

``--factory`` (or ``$AIDOCS_PAIR_CLI_FACTORY``) names a zero-argument callable
returning the gate's ``ControlPlaneHandlers`` over the DURABLE binding / pairing
stores. [FLAGGED] Those stores are in-memory in this slice; the script is only
meaningful once they are durable and shared with the gate process.

The operator acts on the admin-dashboard credential class as ``--operator``;
the handlers still require that user to be an OWNER/ADMIN of the org
(``AdminAuthority``). [FLAGGED] host-shell access stands in for the dashboard
session in the demo.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from typing import Any, Callable, Optional, Sequence, TextIO

from .binding_store import CREDENTIAL_ADMIN_DASHBOARD, ControlPlaneActor
from .control_plane import ControlPlaneHandlers

__all__ = ["EXIT_ABORTED", "EXIT_REFUSED", "EXIT_USAGE", "FACTORY_ENV", "main", "run"]

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3
FACTORY_ENV = "AIDOCS_PAIR_CLI_FACTORY"
MAX_PASTE_BYTES = 256 * 1024
MAX_PASTE_LINES = 2000


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """Never exits the process and never guesses: an unknown flag (``--yes``
    included) is a usage error."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        raise _UsageError(message or "")


def _parser() -> _Parser:
    p = _Parser(prog="pair_cli", add_help=False, allow_abbrev=False)
    p.add_argument("--operator", required=True, help="the AIDOCS user id of the org owner/admin")
    p.add_argument("--factory", default=None, help="module:callable returning the gate's ControlPlaneHandlers")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    c = sub.add_parser("create", add_help=False, allow_abbrev=False)
    c.add_argument("--tenant-id", required=True)
    c.add_argument("--pack-id", required=True)
    c.add_argument("--contract-digest", required=True)
    c.add_argument("--application-origin", required=True)
    pr = sub.add_parser("pair", add_help=False, allow_abbrev=False)
    pr.add_argument("--binding-id", required=True)
    pr.add_argument("--org-key-rotation", action="store_true")
    pr.add_argument("--response-file", default=None)
    a = sub.add_parser("activate", add_help=False, allow_abbrev=False)
    a.add_argument("--binding-id", required=True)
    # #1132: in-place contract upgrade (needs the application owner's approval
    # in the application first) and the missing disable act.
    u = sub.add_parser("contract-upgrade", add_help=False, allow_abbrev=False)
    u.add_argument("--binding-id", required=True)
    u.add_argument("--to-digest", required=True)
    d = sub.add_parser("disable", add_help=False, allow_abbrev=False)
    d.add_argument("--binding-id", required=True)
    return p


class _Io:
    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self._in = stdin
        self._out = stdout

    def say(self, text: str = "") -> None:
        self._out.write(text + "\n")
        self._out.flush()

    def ask(self, prompt: str) -> Optional[str]:
        """One line, or None on EOF."""
        self._out.write(prompt)
        self._out.flush()
        line = self._in.readline()
        return None if line == "" else line.rstrip("\r\n")

    def read_json(self) -> Optional[Any]:
        """Accumulate pasted lines until they parse as one JSON value (or EOF)."""
        buf: list[str] = []
        size = 0
        for _ in range(MAX_PASTE_LINES):
            line = self._in.readline()
            if line == "":
                return None
            buf.append(line)
            size += len(line)
            if size > MAX_PASTE_BYTES:
                raise ValueError("pasted response too large")
            text = "".join(buf).strip()
            if not text:
                buf.clear()
                continue
            try:
                return _loads(text)
            except _Duplicate:
                raise
            except ValueError:
                continue
        raise ValueError("pasted response did not parse")


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Duplicate("duplicate member name")
        out[key] = value
    return out


def _loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_duplicates)


def _refused(io: _Io, result: dict) -> int:
    err = result.get("error", {}) if isinstance(result, dict) else {}
    io.say(f"REFUSED: {err.get('code', 'unknown')} -- {err.get('message', '')}")
    if err.get("application_code"):  # #1132: the APPLICATION's own declared reason
        io.say(f"The application answered: {err['application_code']}")
    io.say("Nothing was confirmed.")
    return EXIT_REFUSED


def _normalize_code(text: str) -> str:
    return text.strip().replace(" ", "")


def _pair(io: _Io, handlers: ControlPlaneHandlers, actor: ControlPlaneActor, args: Any,
          clock: Callable[[], float]) -> int:
    res = handlers.issue_pairing_offer(
        actor, {"binding_id": args.binding_id, "org_key_rotation": bool(args.org_key_rotation)}, now=int(clock())
    )
    if not res.get("ok"):
        return _refused(io, res)
    offer = res["offer"]
    io.say(f"== AIDOCS pairing offer for binding {offer['binding_id']} (org {offer['tenant_id']}), "
           f"expires {offer['expires_at']} ==")
    io.say("Paste this ONE line into the CRM: Admin > Collegamento AIDOCS > Abbina con AIDOCS > Offerta AIDOCS.")
    io.say("It carries a single-use pairing secret: do not paste it anywhere else.")
    io.say(json.dumps(offer, separators=(",", ":"), ensure_ascii=True))
    io.say("")
    try:
        if args.response_file:
            io.say(f"Reading the CRM response from {args.response_file} ...")
            with open(args.response_file, encoding="utf-8") as fh:
                response = _loads(fh.read())
        else:
            io.say("Paste the CRM's 'Risposta per AIDOCS' (one JSON object), then press Enter:")
            response = io.read_json()
    except (OSError, ValueError) as exc:
        io.say(f"REFUSED: the CRM response is not one well-formed JSON object ({type(exc).__name__}).")
        io.say("Nothing was confirmed.")
        return EXIT_REFUSED
    if response is None:
        io.say("ABORTED: no response was pasted. Nothing was confirmed.")
        return EXIT_ABORTED
    res = handlers.redeem_pairing_response(actor, response, now=int(clock()))
    if not res.get("ok"):
        return _refused(io, res)
    r = res["review"]
    if r["binding_id"] != offer["binding_id"] or r["tenant_id"] != offer["tenant_id"]:  # pragma: no cover -- handler invariant
        io.say("REFUSED: the review does not match the offer. Nothing was confirmed.")
        return EXIT_REFUSED
    io.say("")
    io.say("Verify EVERY fact below against the CRM screen before accepting:")
    io.say(f"  org (tenant_id)            : {r['tenant_id']}")
    io.say(f"  binding                    : {r['binding_id']}")
    io.say(f"  pack                       : {r['pack_id']}")
    io.say(f"  contract_digest            : {offer['contract_digest']}")
    io.say(f"  toolspace                  : {r['toolspace_id']}")
    io.say(f"  application origin         : {r['application_origin']}")
    io.say(f"  audience                   : {r['audience']}")
    io.say(f"  AIDOCS key fingerprint     : {r['aidocs_fingerprint']}")
    io.say(f"  CRM binding-control key fp : {r['application_fingerprint']}")
    io.say(f"  CRM entitlement key fps    : {', '.join(r['entitlement_fingerprints'])}")
    io.say(f"  COMPARISON CODE            : {r['comparison_code']}")
    io.say("")
    typed = io.ask("To ACCEPT, type the comparison code exactly as the CRM shows it (anything else aborts): ")
    if typed is None or _normalize_code(typed) != _normalize_code(r["comparison_code"]):
        io.say("ABORTED: the code was not typed. Nothing was confirmed; issue a new offer to try again.")
        return EXIT_ABORTED
    res = handlers.confirm_pairing(actor, {"review_id": r["review_id"]}, now=int(clock()))
    if not res.get("ok"):
        return _refused(io, res)
    io.say("")
    io.say("CONFIRMED. Paste this AIDOCS confirmation into the CRM: 'Incolla conferma AIDOCS':")
    io.say(res["confirmation"])
    io.say("")
    io.say(f"Binding {r['binding_id']} is paired. Activate it with: pair_cli ... activate --binding-id {r['binding_id']}")
    return EXIT_OK


def _activate(io: _Io, handlers: ControlPlaneHandlers, actor: ControlPlaneActor, args: Any) -> int:
    typed = io.ask(f"Type the binding id '{args.binding_id}' to ACTIVATE it (anything else aborts): ")
    if typed is None or typed.strip() != args.binding_id:
        io.say("ABORTED: nothing was activated.")
        return EXIT_ABORTED
    res = handlers.activate_binding(actor, {"binding_id": args.binding_id})
    if not res.get("ok"):
        return _refused(io, res)
    io.say(json.dumps(res["binding"], indent=2, sort_keys=True))
    return EXIT_OK


def _contract_upgrade(io: _Io, handlers: ControlPlaneHandlers, actor: ControlPlaneActor, args: Any) -> int:
    body = {"binding_id": args.binding_id, "to_digest": args.to_digest}
    preview = handlers.preview_contract_upgrade(actor, body)
    if not preview.get("ok"):
        return _refused(io, preview)
    p = preview["preview"]
    io.say(f"Binding:        {p['binding_id']} (version {p['binding_version']})")
    io.say(f"From contract:  {p['from_digest']}")
    io.say(f"To contract:    {p['to_digest']}")
    for label, key in (("Tools added", "tools_added"), ("Tools removed", "tools_removed"),
                       ("Modes added", "modes_added"), ("Modes removed", "modes_removed")):
        io.say(f"{label + ':':<15} {', '.join(p[key]) if p[key] else '-'}")
    io.say("The application's owner must have approved exactly this contract first.")
    typed = io.ask(f"Type the binding id '{args.binding_id}' to UPGRADE it in place (anything else aborts): ")
    if typed is None or typed.strip() != args.binding_id:
        io.say("ABORTED: nothing was changed.")
        return EXIT_ABORTED
    res = handlers.upgrade_contract(actor, body)
    if not res.get("ok"):
        return _refused(io, res)
    io.say(json.dumps(res["binding"], indent=2, sort_keys=True))
    io.say("UPGRADED. Refresh the ChatGPT connector so it lists the new contract's tools.")
    return EXIT_OK


def _disable(io: _Io, handlers: ControlPlaneHandlers, actor: ControlPlaneActor, args: Any) -> int:
    typed = io.ask(f"Type the binding id '{args.binding_id}' to DISABLE it (anything else aborts): ")
    if typed is None or typed.strip() != args.binding_id:
        io.say("ABORTED: nothing was disabled.")
        return EXIT_ABORTED
    res = handlers.disable_binding(actor, {"binding_id": args.binding_id})
    if not res.get("ok"):
        return _refused(io, res)
    io.say(json.dumps(res["binding"], indent=2, sort_keys=True))
    return EXIT_OK


def _create(io: _Io, handlers: ControlPlaneHandlers, actor: ControlPlaneActor, args: Any) -> int:
    res = handlers.create_binding(actor, {
        "tenant_id": args.tenant_id,
        "pack_id": args.pack_id,
        "contract_digest": args.contract_digest,
        "application_origin": args.application_origin,
    })
    if not res.get("ok"):
        return _refused(io, res)
    io.say(json.dumps(res["binding"], indent=2, sort_keys=True))
    return EXIT_OK


def run(
    argv: Sequence[str],
    *,
    handlers: Optional[ControlPlaneHandlers],
    stdin: TextIO,
    stdout: TextIO,
    clock: Callable[[], float] = time.time,
) -> int:
    io = _Io(stdin, stdout)
    try:
        args = _parser().parse_args(list(argv))
    except _UsageError as exc:
        io.say(f"usage error: {exc}")
        return EXIT_USAGE
    if handlers is None:
        io.say(f"usage error: no control-plane factory (--factory or ${FACTORY_ENV}).")
        return EXIT_USAGE
    actor = ControlPlaneActor(user_id=args.operator, credential_class=CREDENTIAL_ADMIN_DASHBOARD)
    if args.cmd == "pair":
        return _pair(io, handlers, actor, args, clock)
    if args.cmd == "activate":
        return _activate(io, handlers, actor, args)
    if args.cmd == "contract-upgrade":
        return _contract_upgrade(io, handlers, actor, args)
    if args.cmd == "disable":
        return _disable(io, handlers, actor, args)
    return _create(io, handlers, actor, args)


def _load_factory(spec: Optional[str]) -> Optional[Callable[[], ControlPlaneHandlers]]:
    if not spec:
        return None
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise _UsageError("--factory must be module:callable")
    return getattr(importlib.import_module(module), attr)


def main(argv: Optional[Sequence[str]] = None, *, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    io = _Io(stdin, stdout)
    try:
        args, _rest = _parser().parse_known_args(argv)
        factory = _load_factory(args.factory or os.environ.get(FACTORY_ENV))
    except (_UsageError, ImportError, AttributeError) as exc:
        io.say(f"usage error: {exc}")
        return EXIT_USAGE
    handlers = factory() if factory is not None else None
    return run(argv, handlers=handlers, stdin=stdin, stdout=stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
