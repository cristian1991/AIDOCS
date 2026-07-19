"""Shell parse-tree SECOND OPINION (#472 tranche; doctrine XXXII guest oracle).

The regex/split floor in ``bash_policy`` is the OWNER of the shell verdict.
This module is a GUEST ORACLE: it parses a command into its real syntax tree
(tree-sitter-bash) and reports STRUCTURE the regex layer might miss — command
substitutions, redirection targets, true argv boundaries. Per doctrine XXXII
the guest sharpens evidence but never holds the pen:

  * where the parse tree proves a dangerous STRUCTURE the regex floor did not
    flag, the STRICTER reading wins (an extra refusal — it can only ADD deny,
    never authorize);
  * parser unavailable OR parse error ⇒ return None (fail-OPEN to the owned
    regex floor, whose behavior is the current shipped floor — so this seam
    NEVER weakens anything);
  * it is wired as an ADDITIONAL pre-check in ``_evaluate_bash_policy_decision``,
    not a replacement for any phase.

STATUS (2026-07): tree-sitter-bash grammar is a pinned AIDOCS dependency class
(doctrine XXXI tree-sitter*), but the grammar binary is not present in every
runtime. When absent, ``get_shell_insight`` returns None and the evaluator is
byte-identical to the regex-only floor (proven by
tests/security/test_shell_parser_oracle_472.py). Completing the grammar
enrollment + the divergence-catch assertions is TRANCHE 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class ShellInsight:
    """Structural facts the parse tree proves about a command."""

    substitutions: tuple[str, ...] = ()  # inner text of $()/`` command subs
    redirect_targets: tuple[str, ...] = ()  # file targets of >/>> (not fd dups)
    has_process_substitution: bool = False  # <(...) / >(...)
    heredoc_consumers: tuple[str, ...] = field(default_factory=tuple)
    parsed: bool = False  # True only when a real parse tree was built


@lru_cache(maxsize=1)
def _load_parser() -> Any | None:
    """Return a tree-sitter Parser for bash, or None when unavailable.

    Tries the standalone ``tree_sitter_bash`` grammar first, then
    ``tree_sitter_languages`` bundle. ANY failure → None (fail-open). Cached
    so the import/attempt cost is paid at most once per process.
    """
    try:
        from tree_sitter import Parser
    except Exception:
        return None
    # Preferred: dedicated grammar package.
    try:
        import tree_sitter_bash as _tsb
        from tree_sitter import Language

        lang = Language(_tsb.language())
        parser = Parser(lang)
        return parser
    except Exception:
        pass
    # Fallback: aggregate language pack.
    try:
        from tree_sitter_languages import get_parser

        return get_parser("bash")
    except Exception:
        return None


def parser_available() -> bool:
    """True when a real bash parse tree can be built in this runtime."""
    return _load_parser() is not None


def _walk(node: Any):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        # children is a list on tree-sitter nodes
        for child in reversed(n.children):
            stack.append(child)


def get_shell_insight(command: str) -> ShellInsight | None:
    """Parse ``command`` and return structural facts, or None when the parser
    is unavailable or the parse fails (fail-open to the regex floor).

    Never raises — any internal error degrades to None so the owned floor
    remains the sole authority.
    """
    if not command or not command.strip():
        return None
    parser = _load_parser()
    if parser is None:
        return None
    try:
        tree = parser.parse(command.encode("utf-8"))
        root = tree.root_node
        if root is None:
            return None
        subs: list[str] = []
        redirects: list[str] = []
        procsub = False
        heredocs: list[str] = []
        src = command.encode("utf-8")

        def _text(n: Any) -> str:
            try:
                return src[n.start_byte : n.end_byte].decode("utf-8", "replace")
            except Exception:
                return ""

        for n in _walk(root):
            t = n.type
            if t == "command_substitution":
                inner = _text(n).strip()
                for pre, suf in (("$(", ")"), ("`", "`")):
                    if inner.startswith(pre) and inner.endswith(suf):
                        inner = inner[len(pre) : len(inner) - len(suf)]
                        break
                subs.append(inner.strip())
            elif t == "process_substitution":
                procsub = True
            elif t in ("file_redirect", "redirected_statement", "file_redirect_operator"):
                # Capture the destination filename child(ren).
                for child in n.children:
                    if child.type in ("word", "string", "raw_string", "concatenation"):
                        redirects.append(_text(child).strip("\"'"))
            elif t == "heredoc_redirect":
                # The consumer is the command the heredoc feeds.
                heredocs.append(_text(n).strip())

        return ShellInsight(
            substitutions=tuple(s for s in subs if s),
            redirect_targets=tuple(r for r in redirects if r),
            has_process_substitution=procsub,
            heredoc_consumers=tuple(heredocs),
            parsed=True,
        )
    except Exception:
        return None


def parse_tree_stricter_refusal(command: str) -> dict[str, Any] | None:
    """Guest-oracle SECOND OPINION: return a block dict when the parse tree
    proves a dangerous STRUCTURE (a command substitution, a process
    substitution, or a redirect to a rooted/out-of-workspace path). Returns
    None when the parser is unavailable, the parse is clean, or the parse
    failed — the regex floor (which already ran) remains authoritative.

    This can only ADD a refusal on top of the regex floor: callers invoke it
    AFTER the regex substitution / redirect phases, so a clean parse changes
    nothing and a divergence (parser sees a construct regex missed) escalates
    to the stricter deny.
    """
    insight = get_shell_insight(command)
    if insight is None or not insight.parsed:
        return None

    def _block(rule: str, why: str) -> dict[str, Any]:
        return {
            "allowed": False,
            "reason": (
                f"Command blocked by shell parse-tree oracle `{rule}` — {why} "
                "(structure proven by the real syntax tree, not visible to the "
                "regex floor)."
            ),
            "matched_rule": f"parse_oracle.{rule}",
        }

    if insight.substitutions:
        return _block(
            "command_substitution",
            "an embedded command substitution runs a hidden command",
        )
    if insight.has_process_substitution:
        return _block(
            "process_substitution",
            "a process substitution executes an embedded command",
        )
    for tgt in insight.redirect_targets:
        norm = tgt.replace("\\", "/")
        rooted = (
            norm.startswith(("/", "~"))
            or (len(tgt) >= 2 and tgt[1] == ":")
        )
        if rooted:
            return _block(
                "redirect_target",
                f"a redirect writes to a rooted out-of-workspace path ({tgt!r})",
            )
    return None
