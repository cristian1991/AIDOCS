"""ShellIR #1213 P1: non-executing Bash structural parser *spike*.

Only a structural AST with source-byte spans is returned. This module has NO
policy verdict, execution capability, grant, attestation, source-file custody,
or semantic command/argv resolver. In particular STRUCTURAL_COMPLETE is NOT
EXACT_STATIC and is NEVER an authorization result.

The independent existing shell_command_insight guest and production gates
remain unchanged. No consumer may import this spike as an allow-capable gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
from typing import Any


GRAMMAR_PACKAGE = "tree-sitter-bash"
EXPECTED_GRAMMAR_VERSION = "0.25.1"
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024
DEFAULT_MAX_NODES = 4096


class StructureStatus(str, Enum):
    STRUCTURAL_COMPLETE = "structural_complete"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED_INPUT = "unsupported_input"
    UNSUPPORTED_DIALECT = "unsupported_dialect"
    PARSER_UNAVAILABLE = "parser_unavailable"
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True)
class NodeSpan:
    """One real tree-sitter AST node; parent_index is a PREORDER index."""

    kind: str
    start_byte: int
    end_byte: int
    parent_index: int | None
    is_error: bool = False
    is_missing: bool = False


@dataclass(frozen=True)
class StructuralParseResult:
    """Diagnostic structural evidence only, never policy or execution proof."""

    status: StructureStatus
    reason: str
    source_sha256: str
    source_length: int
    grammar_version: str = ""
    grammar_sha256: str = ""
    tree_sitter_version: str = ""
    structure_sha256: str = ""
    nodes: tuple[NodeSpan, ...] = ()
    node_count: int = 0
    authorizes_execution: bool = field(default=False, init=False)


@dataclass(frozen=True)
class _Backend:
    parser: Any
    grammar_version: str
    grammar_sha256: str
    tree_sitter_version: str


def _load_parser() -> _Backend | None:
    """Load *pinned* local grammar evidence, or return None. Never run shell.

    Version equality is deliberately strict. Hash the actual loaded native
    grammar library; this is EVIDENCE of test-env provenance, not a signed
    production attestation or an approved shipping dependency.
    """
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_bash as bash

        grammar_version = metadata.version(GRAMMAR_PACKAGE)
        if grammar_version != EXPECTED_GRAMMAR_VERSION:
            return None
        core_version = metadata.version("tree-sitter")
        module_dir = Path(bash.__file__).resolve().parent
        candidates = [
            p for p in module_dir.glob("_binding*")
            if p.is_file() and p.suffix.lower() in (".so", ".pyd", ".dll", ".dylib")
        ]
        if len(candidates) != 1:
            return None
        grammar_sha = sha256(candidates[0].read_bytes()).hexdigest()
        language = Language(bash.language())
        parser = Parser(language)
        return _Backend(parser, grammar_version, grammar_sha, core_version)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, AttributeError):
        return None


def _refuse(
    status: StructureStatus,
    reason: str,
    source_hash: str,
    source_length: int,
) -> StructuralParseResult:
    return StructuralParseResult(status, reason, source_hash, source_length)


def parse_source(
    source: bytes,
    *,
    dialect: str,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> StructuralParseResult:
    """Parse inert UTF-8 Bash SOURCE BYTES. No expansion or policy evaluation.

    Reject malformed input / parser absence / any ERROR or MISSING node as a
    terminal *structural result*, with no partial-success graph. Source-byte
    and node ceilings are a prototype budget, NOT CPU/memory containment:
    production must implement independently reviewed process-time limits
    before this compiler may handle untrusted input in an allow-path.
    """
    if not isinstance(source, bytes):
        return _refuse(StructureStatus.UNSUPPORTED_INPUT, "source_must_be_bytes", "", 0)

    source_hash = sha256(source).hexdigest()
    length = len(source)
    if dialect != "bash":
        return _refuse(
            StructureStatus.UNSUPPORTED_DIALECT, "only_bash_grammar_supported",
            source_hash, length,
        )
    if (
        not isinstance(max_source_bytes, int)
        or not isinstance(max_nodes, int)
        or max_source_bytes < 1
        or max_nodes < 1
        or length > max_source_bytes
    ):
        return _refuse(
            StructureStatus.LIMIT_EXCEEDED, "source_or_node_budget_exceeded",
            source_hash, length,
        )
    if b"\x00" in source:
        return _refuse(StructureStatus.UNSUPPORTED_INPUT, "nul_byte", source_hash, length)
    try:
        source.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return _refuse(StructureStatus.UNSUPPORTED_INPUT, "invalid_utf8", source_hash, length)

    backend = _load_parser()
    if backend is None:
        return _refuse(
            StructureStatus.PARSER_UNAVAILABLE, "missing_or_unpinned_bash_grammar",
            source_hash, length,
        )
    try:
        tree = backend.parser.parse(source)
        root = tree.root_node
        if (
            root is None
            or root.start_byte != 0
            or root.end_byte != length
            or root.has_error
        ):
            return _refuse(
                StructureStatus.PARSE_ERROR, "bash_ast_error_or_incomplete_source",
                source_hash, length,
            )

        # Iterative full node walk, including punctuation and unnamed nodes.
        # The parser may have allocated its AST before this limit is checked:
        # this spike MUST NOT become the production untrusted-input isolator.
        stack: list[tuple[Any, int | None]] = [(root, None)]
        nodes: list[NodeSpan] = []
        while stack:
            node, parent = stack.pop()
            if len(nodes) >= max_nodes:
                return _refuse(
                    StructureStatus.LIMIT_EXCEEDED, "ast_node_limit_exceeded",
                    source_hash, length,
                )
            span = NodeSpan(
                kind=str(node.type),
                start_byte=int(node.start_byte),
                end_byte=int(node.end_byte),
                parent_index=parent,
                is_error=bool(node.is_error or node.type == "ERROR"),
                is_missing=bool(node.is_missing),
            )
            if (
                span.is_error
                or span.is_missing
                or span.start_byte < 0
                or span.end_byte < span.start_byte
                or span.end_byte > length
            ):
                return _refuse(
                    StructureStatus.PARSE_ERROR, "bash_ast_error_missing_or_bad_span",
                    source_hash, length,
                )
            index = len(nodes)
            nodes.append(span)
            for child in reversed(node.children):
                stack.append((child, index))

        payload = {
            "schema": "shellir-structural-spike/1",
            "grammar": backend.grammar_sha256,
            "source": source_hash,
            "nodes": [
                [n.kind, n.start_byte, n.end_byte, n.parent_index, n.is_error, n.is_missing]
                for n in nodes
            ],
        }
        structural_hash = sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
        return StructuralParseResult(
            status=StructureStatus.STRUCTURAL_COMPLETE,
            reason="structure_only_not_semantics_or_policy",
            source_sha256=source_hash,
            source_length=length,
            grammar_version=backend.grammar_version,
            grammar_sha256=backend.grammar_sha256,
            tree_sitter_version=backend.tree_sitter_version,
            structure_sha256=structural_hash,
            nodes=tuple(nodes),
            node_count=len(nodes),
        )
    except Exception:
        # This is a spike, so no raw exception or input can reach audit.
        # Crucially never return a partial "good" prefix after an error.
        return _refuse(
            StructureStatus.PARSE_ERROR, "bash_parser_internal_failure",
            source_hash, length,
        )
