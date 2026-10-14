"""Local operator CLI for the #1134 organization -> AIDOCS tenant mapping.

Like :mod:`.pack_cli`, this is intentionally NOT an MCP tool or a remote
control route: it is a host-local operator / deploy surface. Access to the
gate host and its ``--gate-root`` is the migration authority; ``--operator``
names the person or deploy identity acting, and is recorded immutably.

Commands (one JSON line on stdout; exit 0 ok / 1 refused / 2 usage)::

    python -m aidocs_mcp.app_packs.tenant_mapping_cli --gate-root <root> \\
        seed-existing-tenant-mapping --organization-id <cn org> --tenant-id <existing tenant> \\
        --operator <who> --reason legacy_app_install_adoption --event-id <correlation id> \\
        [--legacy-grant-id <grt_...>] [--binding-id <bnd_...>] [--connector-id <cnx_...>] \\
        [--resource <connector resource>]

    python -m aidocs_mcp.app_packs.tenant_mapping_cli --gate-root <root> \\
        verify --organization-id <cn org> --tenant-id <expected tenant>

``seed-existing-tenant-mapping`` adopts a tenant whose authority DB already
exists (else ``tenant_unknown``) through
:meth:`~.org_tenant_map.SqliteOrganizationTenantMap.seed_existing_tenant_mapping`
(mapping + audit in one transaction; exact retry idempotent). ``verify`` is the
deploy PRE-FLIP check: it reads the pair back through the production resolver
and refuses ``mapping_absent`` / ``mapping_mismatch``. There is no plain
``seed``, delete or overwrite command.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from .authority_db import AuthorityPathError, app_authority_db_path
from .org_tenant_map import (
    REASON_LEGACY_APP_INSTALL_ADOPTION,
    OrganizationMappingRefused,
    SqliteOrganizationTenantMap,
    TenantAdoptionRecord,
    org_tenant_map_db_path,
    require_tenant_mapping,
)

__all__ = ["EXIT_OK", "EXIT_REFUSED", "EXIT_USAGE", "main", "run"]

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        raise _UsageError(message or "")


def _parser() -> _Parser:
    parser = _Parser(prog="tenant_mapping_cli", add_help=False, allow_abbrev=False)
    parser.add_argument("--gate-root", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    seed = sub.add_parser("seed-existing-tenant-mapping", add_help=False, allow_abbrev=False)
    seed.add_argument("--organization-id", required=True)
    seed.add_argument("--tenant-id", required=True)
    seed.add_argument("--operator", required=True)
    seed.add_argument("--reason", required=True, choices=[REASON_LEGACY_APP_INSTALL_ADOPTION])
    seed.add_argument("--event-id", required=True)
    seed.add_argument("--legacy-grant-id")
    seed.add_argument("--binding-id")
    seed.add_argument("--connector-id")
    seed.add_argument("--resource")

    verify = sub.add_parser("verify", add_help=False, allow_abbrev=False)
    verify.add_argument("--organization-id", required=True)
    verify.add_argument("--tenant-id", required=True)
    return parser


def _write(stdout: TextIO, value: dict) -> None:
    stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    stdout.flush()


def _usage(stdout: TextIO) -> int:
    _write(stdout, {"ok": False, "error": {"code": "usage", "message": "Invalid command or arguments."}})
    return EXIT_USAGE


def _refused(stdout: TextIO, code: str) -> int:
    _write(stdout, {"ok": False, "error": {"code": code, "message": "The tenant mapping request was refused."}})
    return EXIT_REFUSED


def _tenant_exists(root: Path, tenant_id: str) -> bool:
    try:
        return app_authority_db_path(root, tenant_id, create=False).is_file()
    except (AuthorityPathError, OSError, ValueError):
        return False


def _seed(args: Any, root: Path, stdout: TextIO, now: int) -> int:
    if not _tenant_exists(root, args.tenant_id):
        return _refused(stdout, "tenant_unknown")
    record = TenantAdoptionRecord(
        organization_id=args.organization_id,
        aidocs_tenant_id=args.tenant_id,
        operator=args.operator,
        event_id=args.event_id,
        reason=args.reason,
        legacy_grant_id=args.legacy_grant_id,
        binding_id=args.binding_id,
        connector_id=args.connector_id,
        resource=args.resource,
    )
    store = SqliteOrganizationTenantMap(org_tenant_map_db_path(root))
    receipt = store.seed_existing_tenant_mapping(record, now=now)
    # Read back through the production resolver before reporting success.
    require_tenant_mapping(store, receipt.organization_id, receipt.aidocs_tenant_id)
    _write(
        stdout,
        {
            "ok": True,
            "authority_mapping": {
                "organization_id": receipt.organization_id,
                "aidocs_tenant_id": receipt.aidocs_tenant_id,
            },
            "event_id": receipt.event_id,
            "created": receipt.created,
            "seeded_at": receipt.seeded_at,
        },
    )
    return EXIT_OK


def _verify(args: Any, root: Path, stdout: TextIO) -> int:
    store = SqliteOrganizationTenantMap(org_tenant_map_db_path(root))
    require_tenant_mapping(store, args.organization_id, args.tenant_id)
    _write(
        stdout,
        {
            "ok": True,
            "authority_mapping": {"organization_id": args.organization_id, "aidocs_tenant_id": args.tenant_id},
            "adoption": store.adoption_record(args.organization_id),
        },
    )
    return EXIT_OK


def run(
    argv: Sequence[str],
    *,
    stdout: TextIO,
    clock: Callable[[], float] = time.time,
) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except _UsageError:
        return _usage(stdout)

    root = Path(args.gate_root)
    if not root.is_dir():
        return _refused(stdout, "store_unavailable")
    try:
        if args.cmd == "seed-existing-tenant-mapping":
            return _seed(args, root, stdout, int(clock()))
        return _verify(args, root, stdout)
    except OrganizationMappingRefused as exc:
        return _refused(stdout, exc.code)


def main(argv: Optional[Sequence[str]] = None, *, stdout: TextIO = sys.stdout) -> int:
    return run(sys.argv[1:] if argv is None else argv, stdout=stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
