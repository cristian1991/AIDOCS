"""Local operator CLI for managed sealed application-pack versions.

This command is intentionally NOT an MCP tool or remote control route. It is a
host/operator import surface replacing the repeated gate.env edit + gate restart
when a reviewed contract version is added.

Import always requires the operator to provide the exact expected SHA-256.
There is no delete, remove, unseal or force command; the managed store is
append-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

from .pack_store import ManagedPackStore, PackImportRefused, managed_pack_db_path

__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "main",
    "managed_pack_db_path",
    "run",
]

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        raise _UsageError(message or "")


def _parser() -> _Parser:
    parser = _Parser(prog="pack_cli", add_help=False, allow_abbrev=False)
    parser.add_argument("--gate-root", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    imp = sub.add_parser("import", add_help=False, allow_abbrev=False)
    imp.add_argument("--file", required=True)
    imp.add_argument("--sha256", required=True)

    sub.add_parser("list", add_help=False, allow_abbrev=False)
    return parser


def _write(stdout: TextIO, value: dict) -> None:
    stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    stdout.flush()


def _usage(stdout: TextIO) -> int:
    _write(
        stdout,
        {"ok": False, "error": {"code": "usage", "message": "Invalid command or arguments."}},
    )
    return EXIT_USAGE


def _refused(stdout: TextIO, code: str) -> int:
    _write(
        stdout,
        {"ok": False, "error": {"code": code, "message": "The pack import was refused."}},
    )
    return EXIT_REFUSED


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

    if args.cmd == "import":
        if not _HEX64.fullmatch(args.sha256):
            return _usage(stdout)
        try:
            data = Path(args.file).read_bytes()
        except OSError:
            return _refused(stdout, "bundle_unreadable")
        try:
            store = ManagedPackStore(managed_pack_db_path(root))
            result = store.import_bundle(
                data,
                expected_sha256=args.sha256,
                now=int(clock()),
            )
        except PackImportRefused as exc:
            return _refused(stdout, exc.code)
        _write(
            stdout,
            {
                "ok": True,
                "pack_id": result.pack_id,
                "contract_digest": result.contract_digest,
                "imported": result.imported,
            },
        )
        return EXIT_OK

    try:
        store = ManagedPackStore(managed_pack_db_path(root))
        versions = store.versions()
        generation = store.generation()
    except PackImportRefused as exc:
        return _refused(stdout, exc.code)

    _write(
        stdout,
        {
            "ok": True,
            "generation": generation,
            "versions": [
                {
                    "pack_id": row.pack_id,
                    "contract_digest": row.contract_digest,
                    "imported_at": row.imported_at,
                }
                for row in versions
            ],
        },
    )
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None, *, stdout: TextIO = sys.stdout) -> int:
    return run(sys.argv[1:] if argv is None else argv, stdout=stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
