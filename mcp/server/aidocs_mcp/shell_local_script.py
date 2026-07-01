"""Governed project-local SCRIPT-FILE execution.

The governed [bash] profile already permits arbitrary project-local code via
the interpreter allow-list (`python script.py`, `node x.js`, `make`, `cargo`):
those run repo-owned code whose contents the policy never inspects. A
project-local SHELL SCRIPT FILE (`bash ./scripts/build.sh`,
`bash mcp/scripts/deploy_aidocs_gate.sh --full`) is the SAME threat class — so
the goal requires it to "work through local Bash" too.

What stays forbidden (this is the line that keeps it from being ungated bash):
  * ``bash -c '<inline>'`` / any flag BEFORE the script — that is an arbitrary
    inline shell command, the real bypass; rejected.
  * a bare ``sh`` / ``bash`` reading stdin or interactive — no script file.
  * an ABSOLUTE / home / remote / ``..`` script path — must be project-local.
  * any shell metacharacter (chain / redirection / subshell / expansion /
    glob / substitution / quote) in the invocation — those enable writes,
    network, and escape. (Chain operators are already split off upstream; a
    surviving ``$()`` / backtick is also caught by the dangerous-chain rule.)
  * a script path that does not resolve to an existing file UNDER the
    workspace — no pre-authorising a not-yet-written path, no escape.

This runs AFTER the unbypassable deny-table + dangerous-chain phases in
``evaluate_bash_policy``, so ``bash s.sh && rm -rf /`` (the ``rm`` segment
hits the deny table) and ``bash s.sh | sh`` (the second ``sh`` has no script)
stay denied. The script's INTERNAL commands are not individually gated — that
is inherent to running a script, and identical to the already-permitted
``python script.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_INTERPRETERS: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "dash", "ash", "ksh"},
)

# Leading `VAR=value ` env assignments (e.g. `AIDOCS_DEPLOY_SKIP_BINARY_SCAN=1
# bash …`) — stripped before interpreter detection, mirroring bash_policy.
_ENV_PREFIX_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")

# Metacharacters that enable chain / redirection / subshell / expansion /
# glob / substitution / quoting. '=' is intentionally allowed (it appears in
# benign `--opt=val` script args and was already consumed from the env
# prefix). '/' '.' '-' ':' are path/flag characters, not metacharacters.
_FORBIDDEN_CHARS = set(";&|`$()<>{}[]*?~!\\\n\r\t\"'")


def _basename(token: str) -> str:
    t = token.strip().replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    return t.lower()


def is_governed_local_script(
    segment: str,
    workspace_root: str | Path,
) -> tuple[bool, str]:
    """True for ``<shell-interpreter> <project-local-script-file> [args]``.

    Default-deny. Returns (ok, reason). See the module docstring for the full
    set of forms this deliberately refuses.
    """
    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    s2 = _ENV_PREFIX_RE.sub("", s).strip()
    if not s2:
        return False, "empty after env-prefix strip"
    # No metacharacters anywhere in the invocation.
    if any(ch in _FORBIDDEN_CHARS for ch in s2):
        return False, "shell metacharacter (chain/redirection/subshell/expansion) not permitted"
    toks = s2.split()
    if not toks:
        return False, "no tokens"
    if _basename(toks[0]) not in _SCRIPT_INTERPRETERS:
        return False, "not a shell interpreter"
    # First non-`--` token must be the script FILE — a flag before it
    # (``-c``/``-lc``/``-s``/``-x`` …) means inline/stdin, which is refused.
    script: str | None = None
    for tok in toks[1:]:
        if tok == "--":
            continue
        if tok.startswith("-"):
            return False, (
                f"shell flag '{tok}' before a script file — inline (`-c`) / stdin "
                "forms are not permitted; only `<shell> <project-local-script> [args]`"
            )
        script = tok
        break
    if script is None:
        return False, "no script file (interactive / stdin shell is not permitted)"
    norm = script.replace("\\", "/")
    if (
        norm.startswith(("/", "~", "http://", "https://"))
        or (len(script) >= 2 and script[1] == ":")
        or Path(script).is_absolute()
    ):
        return False, "script must be a PROJECT-LOCAL relative path (no absolute/home/remote)"
    if ".." in norm.split("/"):
        return False, "parent traversal ('..') is not permitted"
    try:
        ws = Path(workspace_root).resolve()
        rp = (ws / script).resolve()
    except Exception:
        return False, "path unresolvable"
    if rp != ws and ws not in rp.parents:
        return False, f"script '{script}' resolves outside the workspace {ws}"
    if not rp.is_file():
        return False, f"script '{script}' is not an existing file"
    return True, f"governed project-local script: {script}"
