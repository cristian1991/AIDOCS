"""ShellIR #1213 P2: pure, non-authorizing literal-word semantics spike.

This diagnostic prototype only maps AST-positioned source bytes to literal word
spellings or typed unknowns. It never executes commands, reads script paths,
resolves actual executables, issues policy grants or touches native host law.

LITERAL_WORDS_ONLY is NOT full semantic coverage, policy eligibility or
permission to execute. Structural uncertainty cannot fall back to regex.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256

from .shell_ir_parser_spike import StructureStatus, parse_source


class SemanticStatus(str, Enum):
    LITERAL_WORDS_ONLY = "literal_words_only"
    UNRESOLVED = "unresolved"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED_INPUT = "unsupported_input"
    UNSUPPORTED_DIALECT = "unsupported_dialect"
    PARSER_UNAVAILABLE = "parser_unavailable"
    LIMIT_EXCEEDED = "limit_exceeded"


class WordState(str, Enum):
    STATIC_LITERAL = "static_literal"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class WordProbe:
    start_byte: int
    end_byte: int
    state: WordState
    value: str | None = None
    quote_modes: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class CommandProbe:
    start_byte: int
    end_byte: int
    words: tuple[WordProbe, ...] = ()


@dataclass(frozen=True)
class SemanticProbe:
    status: SemanticStatus
    source_sha256: str
    commands: tuple[CommandProbe, ...] = ()
    uncertainty_codes: tuple[str, ...] = ()
    authorizes_execution: bool = field(default=False, init=False)
    eligible_for_policy: bool = field(default=False, init=False)


# AST shapes, not an allowlist. Unrecognized command children -> unknown.
_WORD_SHAPES = frozenset({
    "command_name", "word", "string", "raw_string", "concatenation",
    "simple_expansion", "expansion", "ansi_c_string",
    "command_substitution", "process_substitution", "arithmetic_expansion",
    "brace_expression", "array",
})
# Literal spellings for a deliberately tiny diagnostic control set, NOT a
# trusted dispatch identity or an execution allowlist: neither PATH nor
# aliases/functions nor startup environment are attested by this probe.
_DIAGNOSTIC_LITERAL_WORD_COMMANDS = frozenset({"echo", "printf"})
# These named carriers deserve more specific diagnostics, but unlisted
# executables are UNKNOWN too: this is never a permission denylist.
# These have unmodeled dispatch / nested body / startup/environment semantics.
_EXECUTION_CARRIERS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "cmd.exe", "pwsh",
    "powershell", "powershell.exe", "python", "python3", "node", "perl",
    "ruby", "php", "eval", "source", ".", "exec", "command", "env",
    "sudo", "timeout", "nice", "xargs", "find", "docker", "wsl",
})
_CWD_MUTATORS = frozenset({"cd", "pushd", "popd"})


def _word_from_source(data: bytes, start: int, end: int) -> WordProbe:
    """Parse a single AST-bounded word, not arbitrary shell command text."""
    source = data[start:end].decode("utf-8", "strict")
    value: list[str] = []
    modes: list[str] = []
    mode = "unquoted"
    i = 0

    def add_mode(name: str) -> None:
        if name not in modes:
            modes.append(name)

    def unresolved(code: str) -> WordProbe:
        return WordProbe(
            start_byte=start, end_byte=end, state=WordState.UNRESOLVED,
            value=None, quote_modes=tuple(modes or ("unquoted",)), reason=code,
        )

    while i < len(source):
        ch = source[i]
        if mode == "single":
            if ch == "'":
                mode = "unquoted"
            else:
                value.append(ch)
            i += 1
            continue

        if mode == "double":
            if ch == '"':
                mode = "unquoted"
                i += 1
                continue
            if ch == "\\":
                if i + 1 >= len(source):
                    return unresolved("unsupported_escape")
                following = source[i + 1]
                if following in ('$', chr(96), '"', "\\"):
                    value.append(following)
                    i += 2
                    continue
                if following == "\n":
                    return unresolved("line_continuation_unmodeled")
                value.append("\\")
                i += 1
                continue
            if ch == "$":
                if i + 1 < len(source) and source[i + 1] == "(":
                    return unresolved("nested_execution")
                return unresolved("dynamic_expansion")
            if ch == chr(96):
                return unresolved("nested_execution")
            value.append(ch)
            i += 1
            continue

        if ch == "'":
            add_mode("single")
            mode = "single"
            i += 1
            continue
        if ch == '"':
            add_mode("double")
            mode = "double"
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= len(source):
                return unresolved("unsupported_escape")
            if source[i + 1] == "\n":
                return unresolved("line_continuation_unmodeled")
            add_mode("escaped")
            value.append(source[i + 1])
            i += 2
            continue
        if ch == "$":
            if i + 1 < len(source) and source[i + 1] == "'":
                return unresolved("ansi_c_quoting_unmodeled")
            if i + 1 < len(source) and source[i + 1] == "(":
                return unresolved("nested_execution")
            return unresolved("dynamic_expansion")
        if ch == chr(96):
            return unresolved("nested_execution")
        if ch in ("*", "?", "[", "]"):
            return unresolved("glob_expansion")
        if ch in ("{", "}"):
            return unresolved("brace_expansion")
        if ch == "~" and (i == 0 or source[i - 1] in "=:"):
            return unresolved("tilde_expansion")
        if ch in (";", "|", "&", "<", ">", "(", ")") or ch.isspace():
            return unresolved("unexpected_shell_operator_in_word")
        add_mode("unquoted")
        value.append(ch)
        i += 1

    if mode != "unquoted":
        return unresolved("unterminated_quote")
    return WordProbe(
        start_byte=start, end_byte=end, state=WordState.STATIC_LITERAL,
        value="".join(value), quote_modes=tuple(modes or ("unquoted",)),
    )



def _source_definition_candidates(
    source: bytes,
    nodes: tuple,
) -> tuple[tuple[str, int], ...]:
    """Find possible local function/alias shadows from AST-proven constructs.

    This is conservative diagnostic evidence, not name resolution: for example,
    an alias may never actually expand, and a function may lie behind a branch.
    Returning a possible shadow can only make the diagnostic more unresolved.
    Never inspect whole raw shell text in place of the AST.
    """
    candidates: list[tuple[str, int]] = []
    for node in nodes:
        if node.kind not in ("function_definition", "command"):
            continue
        fragment = source[node.start_byte:node.end_byte].decode("utf-8")
        if node.kind == "function_definition":
            # The AST already proves this span is a function definition;
            # read its bounded header merely to label the possible name.
            header = fragment.partition("{")[0].split("\n", 1)[0].strip()
            if header.startswith("function "):
                header = header[len("function "):].strip()
            name = header.split("(", 1)[0].strip()
            if " " in name:
                name = name.split()[0]
            if name.isidentifier():
                candidates.append((name, node.start_byte))
        else:
            # Only an AST command with a literal, explicit alias declaration
            # is considered. Opaque quoting/options remain generally unknown.
            parts = fragment.split(None, 1)
            if len(parts) != 2 or parts[0] != "alias":
                continue
            for assignment in parts[1].split():
                alias, sep, _value = assignment.partition("=")
                if sep and alias.isidentifier():
                    candidates.append((alias, node.start_byte))
    return tuple(candidates)


def inspect_word_literals(source: bytes, *, dialect: str) -> SemanticProbe:
    """Return source-byte provenance and unknown effects, never an allow."""
    data = source if isinstance(source, bytes) else b""
    source_hash = sha256(data).hexdigest()
    structural = parse_source(source, dialect=dialect)
    rejected = {
        StructureStatus.PARSE_ERROR: SemanticStatus.PARSE_ERROR,
        StructureStatus.UNSUPPORTED_INPUT: SemanticStatus.UNSUPPORTED_INPUT,
        StructureStatus.UNSUPPORTED_DIALECT: SemanticStatus.UNSUPPORTED_DIALECT,
        StructureStatus.PARSER_UNAVAILABLE: SemanticStatus.PARSER_UNAVAILABLE,
        StructureStatus.LIMIT_EXCEEDED: SemanticStatus.LIMIT_EXCEEDED,
    }
    if structural.status in rejected:
        return SemanticProbe(
            status=rejected[structural.status], source_sha256=source_hash,
        )
    if structural.status != StructureStatus.STRUCTURAL_COMPLETE:
        return SemanticProbe(
            status=SemanticStatus.UNRESOLVED, source_sha256=source_hash,
            uncertainty_codes=("structural_status_unrecognized",),
        )

    nodes = structural.nodes
    children: dict[int, list[int]] = {}
    for idx, n in enumerate(nodes):
        if n.parent_index is not None:
            children.setdefault(n.parent_index, []).append(idx)

    codes: set[str] = set()
    kinds = {n.kind for n in nodes}
    if any("redirect" in k for k in kinds):
        codes.add("redirect_semantics_unmodeled")
    if any("command_substitution" in k or "process_substitution" in k for k in kinds):
        codes.add("nested_execution")
    if any("brace" in k and k not in ("{", "}") for k in kinds):
        codes.add("brace_expansion")
    if "variable_assignment" in kinds:
        codes.add("environment_semantics_unmodeled")
    if any(k in kinds for k in (
        "pipeline", "list", "if_statement", "for_statement",
        "while_statement", "case_statement", "function_definition",
        "subshell", "compound_statement", "test_command",
    )):
        codes.add("control_graph_unmodeled")

    found: list[CommandProbe] = []
    for idx, n in enumerate(nodes):
        if n.kind != "command":
            continue
        words: list[WordProbe] = []
        for child_index in children.get(idx, ()):
            ch = nodes[child_index]
            if ch.kind in _WORD_SHAPES:
                w = _word_from_source(data, ch.start_byte, ch.end_byte)
                words.append(w)
                if w.state == WordState.UNRESOLVED:
                    codes.add(w.reason)
            elif "assignment" in ch.kind:
                codes.add("environment_semantics_unmodeled")
            elif "redirect" in ch.kind:
                codes.add("redirect_semantics_unmodeled")
            elif ch.start_byte < ch.end_byte:
                if data[ch.start_byte:ch.end_byte].strip():
                    codes.add("unsupported_command_ast_child")
        found.append(CommandProbe(
            start_byte=n.start_byte, end_byte=n.end_byte, words=tuple(words),
        ))
        if not words:
            codes.add("command_word_missing")
        else:
            name = words[0].value
            # Literal source spelling is NOT provenance for an executable.
            # Mark all unknown/path/wrapper names unresolved, including
            # sources using their own aliases, functions and imported env.
            if name not in _DIAGNOSTIC_LITERAL_WORD_COMMANDS:
                codes.add("dispatch_identity_unproven")
            if name in _EXECUTION_CARRIERS:
                codes.add("nested_interpreter_unmodeled")
            if name in _CWD_MUTATORS:
                codes.add("cwd_semantics_unmodeled")

    possible_shadows = _source_definition_candidates(data, nodes)
    for command in found:
        if not command.words or command.words[0].value is None:
            continue
        spelled_name = command.words[0].value
        if any(
            name == spelled_name and definition_start < command.start_byte
            for name, definition_start in possible_shadows
        ):
            codes.add("dispatch_shadowed_by_source_definition")

    if not found:
        codes.add("no_executable_commands")
    if len(found) != 1:
        codes.add("control_graph_unmodeled")
    # Even the LITERAL_WORDS_ONLY state attests neither PATH nor env,
    # meaning it is not a trustworthy dispatch identity or permission.
    return SemanticProbe(
        status=(SemanticStatus.UNRESOLVED if codes
                else SemanticStatus.LITERAL_WORDS_ONLY),
        source_sha256=source_hash,
        commands=tuple(found),
        uncertainty_codes=tuple(sorted(codes)),
    )
