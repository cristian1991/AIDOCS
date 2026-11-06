"""Operator answers to a governed-shell host ask (follow-up to #1215).

A static bash_policy ask on the Claude Code Bash tool is shown to the operator
as the host's own Yes/No prompt (``shell_adapter._ask_env``). A hook-originated
ask cannot add buttons and the hook never learns the answer, so the two
broader answers are TYPED by the operator in their next message:

  * THIS TURN -- the canonical AIDOCS per-turn intent grant, reused verbatim:
    ``i allow <base>`` (canonical_intent_registry.detect_bash_subcommand_grants_v2
    -> PromptMutator.apply_per_turn_intent_state -> query_gate
    user_intent_bash_subcommands -> bash_policy Phase 1/3 grant). Its scope is
    the BASE COMMAND for the current turn (any arguments), it never overrides
    the locked families or the unbypassable floors, and it is cleared on the
    next operator prompt. This module adds no turn mechanism; it only names the
    exact token that detector would match for this command.
  * ALWAYS -- new: ``allow always <id>``. ``<id>`` is a 6-hex nonce minted per
    ask and stored with the pending ask (session, command, proposed rules,
    time). It grants only when it names a FRESH (``PENDING_ASK_TTL_S``)
    pending ask of the SAME host session; it is single-use; a bare, stale,
    unknown or foreign token grants nothing and the operator is told so.

THE SEAL (always)
  * Tokens are read ONLY by ``apply_operator_shell_grants``, called ONLY from
    the UserPromptSubmit leg ``hook_pipeline._ups_shell_ask_grant_leg`` on the
    provenance-floored operator text (``strip_non_operator_text``: pasted
    content, injected harness blocks and peer deliveries are cut) and only
    when the origin gate (``is_authority_bearing_prompt_eligible``) held. Tool
    output, agent text, files and messages never reach it; no MCP tool imports
    this module (pinned by tests/security/test_shell_ask_operator_grant.py).
  * Requires a host-bound operator holding ``admin.manage_config`` (the
    permission of the bash-allowlist operator-intent route) and writes through
    ``ConfigStore.set`` -- the sanctioned config writer, which audits
    ``config_write_internal`` -- plus an ``operator_shell_allow_always`` audit
    row carrying the operator's user_id. Never Claude Code settings.

SCOPE OF "ALWAYS". AIDOCS allow rules are ``bash.allow.<base> = [patterns]``,
each pattern an fnmatch over a segment's argument text. The narrowest natural
rule is the EXACT argument text with glob metacharacters escaped, one rule per
segment that needed one. It is offered only when the simulated policy with
those rules actually allows the whole command.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from ._sqlite_connect import Durability
from ._sqlite_connect import connect as _canonical_connect

#: A pending ask is answerable for this long after it was shown.
PENDING_ASK_TTL_S = 30 * 60

_PERM_MANAGE_CONFIG = "admin.manage_config"
_NONCE_LEN_BYTES = 3  # 6 hex chars
# AFFIRMATIVE STANDALONE INSTRUCTION ONLY: the token must be a whole line of
# its own (optionally ending in `.` or `!`). Negated ("don't allow always x"),
# questioning ("should I allow always x?"), quoted or backticked text never
# matches -- fail closed on doubt.
_TOKEN_RE = re.compile(
    r"^ {0,3}allow[ \t]+always[ \t]+([0-9a-f]{6})[ \t]*[.!]?[ \t]*$",
    re.IGNORECASE,
)
_RULE_BASE_RE = re.compile(r"^[a-z0-9][a-z0-9_+-]*$")
#: The MAC key lives in the gate's protected key store (~/.aidocs/trust/...,
#: owner-only), never under the agent-writable project .MEMORY tree.
_MAC_KEY_NAME = "shell_ask_grant.key"
_MAC_DOMAIN = b"aidocs.shell_ask_grant.pending.v1"


def _now() -> float:
    return time.time()


# ── pure helpers ────────────────────────────────────────────────────────────


def exact_args_pattern(args: str) -> str:
    """An fnmatch pattern matching ``args`` and nothing else."""
    return "".join(f"[{ch}]" if ch in "*?[" else ch for ch in args)


_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_QUOTE_CHARS = frozenset("`'\"‘’“”")


def operator_instruction_lines(text: str) -> list[str]:
    """The lines of an operator prompt that can carry a typed INSTRUCTION.

    Shared-shape filter (mirrors the rules adopted for operator rank
    commands; kept generic so both can later share one home). Dropped:
      * every line inside a ``` / ~~~ fenced block, the fence lines included.
        An opener may carry an info string; the CLOSER is the same character,
        at least as long as the opener, with only trailing whitespace (so a
        ```python line inside a block does NOT close it). An unclosed fence
        swallows the rest of the prompt;
      * blockquote lines (``>`` after <= 3 spaces);
      * lines containing a backtick or a straight/curly quote;
      * indented lines (4+ leading spaces, or a leading tab: code / quoted).
    What remains are lines whose first token can be the command itself.
    """
    out: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in (text or "").splitlines():
        if fence_char:
            stripped = line.rstrip()
            lead = len(stripped) - len(stripped.lstrip(" "))
            body = stripped.lstrip(" ")
            if (
                lead <= 3
                and body
                and set(body) == {fence_char}
                and len(body) >= fence_len
            ):
                fence_char, fence_len = "", 0
            continue
        m = _FENCE_OPEN_RE.match(line)
        if m and not (m.group("fence")[0] == "`" and "`" in m.group("info")):
            fence_char, fence_len = m.group("fence")[0], len(m.group("fence"))
            continue
        if line.startswith("\t") or line.startswith("    "):
            continue
        if line.lstrip(" ").startswith(">"):
            continue
        if any(ch in _QUOTE_CHARS for ch in line):
            continue
        out.append(line)
    return out


def parse_always_tokens(text: str) -> list[str]:
    """The nonces of every affirmative, standalone ``allow always <id>`` line
    that survives ``operator_instruction_lines``."""
    seen: list[str] = []
    for line in operator_instruction_lines(text):
        m = _TOKEN_RE.match(line)
        if m:
            nonce = m.group(1).lower()
            if nonce not in seen:
                seen.append(nonce)
    return seen


def _mac_key() -> bytes:
    # A local machine-held key (neutral local_mac_key), never the gate's key
    # custody store; same on-disk location, so existing rows keep verifying.
    from .local_mac_key import mac_key

    return mac_key(_MAC_KEY_NAME)


def _row_mac(
    session_id: str, nonce: str, command: str, rules_json: str, created_at: float, request_id: str
) -> str:
    msg = json.dumps(
        [session_id, nonce, command, rules_json, repr(float(created_at)), request_id],
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_mac_key(), _MAC_DOMAIN + b"\x00" + msg, hashlib.sha256).hexdigest()


def _asking_segments(project_root: Path, command: str):
    """``(policy, [(base, args, matched_rule)])`` for each segment that, on
    its own, lands on a bash_policy ask."""
    from .bash_policy import (
        _base_command,
        evaluate_bash_policy,
        load_canonical_bash_policy,
        normalize_command_segments,
    )
    from .shell_data_windows import mask_data_windows

    policy = load_canonical_bash_policy(project_root=project_root) or {}
    ws = str(project_root)
    segments, _mult, _iter = normalize_command_segments(mask_data_windows(command))
    out = []
    for seg in segments:
        alone = evaluate_bash_policy(seg, policy, workspace_root=ws)
        if alone.get("verdict") != "ask":
            continue
        base, args = _base_command(seg)
        out.append((base, args, str(alone.get("matched_rule") or "")))
    return policy, out


# Bases a turn-wide `i allow <base>` must NEVER be advertised for (r0b2 safety
# ruling): an opaque interpreter / launcher runs a command line the deny-rule
# layer cannot see into (`wsl -e bash -c "..."` -- shell_wrapper_peel has no
# wsl), so a base-wide grant to it is "allow anything this turn". The ONE
# list: the wrapper/launcher tables the peeler already knows, the
# interpreter/shell tables command_read_intent already knows, plus the
# launchers neither covers. Interpreters count as opaque regardless of flags.
_EXTRA_OPAQUE_LAUNCHERS: frozenset[str] = frozenset(
    {
        "wsl", "powershell", "pwsh", "cmd", "watch", "fish", "busybox",
        "su", "sudo", "doas", "env", "xargs", "nohup", "timeout", "eval", "exec",
        "bash", "sh", "zsh", "dash", "ksh", "ash", "mksh",
        "python", "python2", "python3", "py", "node", "nodejs", "perl", "ruby",
    },
)


def _opaque_launchers() -> frozenset[str]:
    names = set(_EXTRA_OPAQUE_LAUNCHERS)
    try:
        from .shell_wrapper_peel import COMMAND_VALUE_OPTIONS, WRAPPER_OPTIONS

        names |= set(WRAPPER_OPTIONS) | set(COMMAND_VALUE_OPTIONS)
    except Exception:
        pass
    try:
        from .command_read_intent import _INTERPRETERS, _SHELL_WRAPPERS

        names |= set(_INTERPRETERS) | set(_SHELL_WRAPPERS)
    except Exception:
        pass
    return frozenset(names)


def is_opaque_launcher(base: str) -> bool:
    b = (base or "").strip().lower()
    if b.endswith(".exe"):
        b = b[:-4]
    return b in _opaque_launchers()


def opaque_launcher_bases(command: str) -> list[str]:
    """Bases of ``command``'s segments that are opaque interpreters/launchers.
    Fails closed: an unparseable command reports itself as opaque."""
    try:
        from .bash_policy import _base_command, normalize_command_segments
        from .shell_data_windows import mask_data_windows

        segments, _mult, _iter = normalize_command_segments(mask_data_windows(command))
    except Exception:
        return [command.split(maxsplit=1)[0] if command.strip() else "?"]
    out: list[str] = []
    for seg in segments:
        base, _args = _base_command(seg)
        if is_opaque_launcher(base) and base not in out:
            out.append(base)
    return out


def turn_grant_tokens(project_root: Path, command: str) -> list[str]:
    """The canonical per-turn grant tokens (``i allow <token>``) that the
    existing NL detector would match for the asking segments of ``command``
    -- verified against the detector itself; empty when any asking base is
    not grantable that way, or when ANY segment is an opaque launcher."""
    try:
        from .bash_policy import evaluate_bash_policy
        from .canonical_intent_registry import detect_bash_subcommand_grants_v2

        if opaque_launcher_bases(command):
            return []
        policy, asking = _asking_segments(project_root, command)
        tokens: list[str] = []
        for base, _args, _rule in asking:
            if detect_bash_subcommand_grants_v2(f"i allow {base}") != {base}:
                return []
            if base not in tokens:
                tokens.append(base)
        if not tokens:
            return []
        granted = evaluate_bash_policy(
            command, policy, tokens, workspace_root=str(project_root)
        )
        return tokens if granted.get("allowed") else []
    except Exception:
        return []


def proposed_permanent_rules(project_root: Path, command: str) -> list[list[str]]:
    """The exact AIDOCS allow rules that would make ``command`` pass the law,
    as ``[[base, pattern], ...]``; empty when no such rule set exists."""
    try:
        from .bash_policy import evaluate_bash_policy

        # r0b2: never a permanent rule for an opaque launcher -- the law cannot
        # see the command line it runs, so even an exact-text rule would bless
        # whatever today's incomplete wrapper law fails to refuse.
        if opaque_launcher_bases(command):
            return []
        policy, asking = _asking_segments(project_root, command)
        rules: list[list[str]] = []
        for base, args, rule_id in asking:
            # Only a default.ask is answered by an allow-table rule; a family
            # ask (commands.<base> = ask) outranks the table.
            if rule_id != "default.ask" or not _RULE_BASE_RE.match(base or ""):
                return []
            rule = [base, exact_args_pattern(args)]
            if rule not in rules:
                rules.append(rule)
        if not rules:
            return []
        simulated = _with_rules(policy, rules)
        if not evaluate_bash_policy(command, simulated, workspace_root=str(project_root)).get(
            "allowed"
        ):
            return []
        return rules
    except Exception:
        return []


def _entry_patterns(entry: Any) -> list[str]:
    if isinstance(entry, dict):
        return [str(p) for p in (entry.get("patterns") or [])]
    if isinstance(entry, (list, tuple)):
        return [str(p) for p in entry]
    return []


def _merged_entry(entry: Any, pattern: str) -> Any:
    patterns = _entry_patterns(entry)
    if pattern not in patterns:
        patterns.append(pattern)
    if isinstance(entry, dict):
        merged = dict(entry)
        merged["patterns"] = patterns
        return merged
    return patterns


def _with_rules(policy: dict, rules: list[list[str]]) -> dict:
    sim = copy.deepcopy(policy or {})
    allow = dict(sim.get("allow") or {})
    for base, pattern in rules:
        allow[base] = _merged_entry(allow.get(base), pattern)
    sim["allow"] = allow
    return sim


def _rules_text(rules: list[list[str]]) -> str:
    return "; ".join(f"AIDOCS rule `bash.allow.{b}` += `{p}`" for b, p in rules)


def render_ask_reason(
    command: str, nonce: str, rules: list[list[str]], turn_tokens: list[str]
) -> str:
    """The operator-facing prompt: command FIRST, then why, then the answers."""
    parts = [
        f"Command requested: {command}",
        "AIDOCS: no allow rule matched (default=ask).",
    ]
    answer = "Yes = allow once."
    opaque = opaque_launcher_bases(command)
    if opaque:
        names = ", ".join(f"`{b}`" for b in opaque)
        answer += (
            f" A turn-wide allow is not offered for shell/interpreter launchers "
            f"({names}); answer Yes for this one command."
        )
    elif turn_tokens:
        phrase = " ".join(f"`i allow {t}`" for t in turn_tokens)
        bases = ", ".join(f"`{t}`" for t in turn_tokens)
        answer += (
            f" For the rest of this turn, say {phrase} in your next message "
            f"(allows any {bases} command, any arguments, until your following message)."
        )
    if opaque:
        answer += " No permanent allow is offered for shell/interpreter launchers."
    elif nonce and rules:
        answer += (
            f" To allow it permanently ({_rules_text(rules)}, exact arguments only; "
            f"covers this exact command text only), type `allow always {nonce}`."
        )
    else:
        answer += " (A permanent allow is not offered for this command shape.)"
    parts.append(answer)
    return "\n".join(parts)


# ── pending-ask store ───────────────────────────────────────────────────────
#
# INTEGRITY: the sqlite file sits under the project's .MEMORY tree, which an
# agent can reach. Every row therefore carries an HMAC-SHA256 over
# (session_id, nonce, command, rules, created_at, request_id) keyed from the
# gate key store; a row that fails the MAC is refused, so a planted or edited
# row can never become a permanent rule. ``request_id`` is the PreToolUse
# tool_use_id of the gated request whose receipt preceded the ask.


class ShellAskGrantStore:
    _TABLE = "shell_ask_pending_v2"

    def db_path(self, project_root: Path) -> Path:
        return Path(project_root) / ".MEMORY" / ".index" / "shell_ask_grants.sqlite3"

    def _conn(self, project_root: Path):
        db = self.db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(db, durability=Durability.AUDIT)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} ("
            "session_id TEXT NOT NULL, nonce TEXT NOT NULL, command TEXT NOT NULL, "
            "rules TEXT NOT NULL, created_at REAL NOT NULL, request_id TEXT NOT NULL, "
            "mac TEXT NOT NULL, PRIMARY KEY (session_id, nonce))"
        )
        return conn

    def record_ask(
        self,
        project_root: Path,
        session_id: str,
        command: str,
        rules: list[list[str]],
        *,
        request_id: str = "",
    ) -> str:
        """Mint a nonce for this ask and store it MACed. Returns "" without a
        session or without a permanent rule to offer."""
        sid = str(session_id or "").strip()
        if not sid or not command or not rules:
            return ""
        rules_json = json.dumps(rules, separators=(",", ":"))
        rid = str(request_id or "")
        with self._conn(project_root) as conn:
            conn.execute(
                f"DELETE FROM {self._TABLE} WHERE created_at < ?",
                (_now() - PENDING_ASK_TTL_S,),
            )
            for _ in range(8):
                nonce = secrets.token_hex(_NONCE_LEN_BYTES)
                row = conn.execute(
                    f"SELECT 1 FROM {self._TABLE} WHERE session_id = ? AND nonce = ?",
                    (sid, nonce),
                ).fetchone()
                if row is None:
                    created = _now()
                    mac = _row_mac(sid, nonce, command, rules_json, created, rid)
                    conn.execute(
                        f"INSERT INTO {self._TABLE} (session_id, nonce, command, rules, "
                        "created_at, request_id, mac) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (sid, nonce, command, rules_json, created, rid, mac),
                    )
                    conn.commit()
                    return nonce
        return ""

    def _peek_ask(self, project_root: Path, session_id: str, nonce: str) -> bool:
        """Advisory only (for the operator note): is a fresh row present?"""
        if not self.db_path(project_root).is_file():
            return False
        with self._conn(project_root) as conn:
            row = conn.execute(
                f"SELECT created_at FROM {self._TABLE} WHERE session_id = ? AND nonce = ?",
                (session_id, nonce),
            ).fetchone()
        return row is not None and float(row[0]) >= _now() - PENDING_ASK_TTL_S

    def _take_ask(self, project_root: Path, session_id: str, nonce: str) -> dict | None:
        """ATOMICALLY consume the row (one DELETE ... RETURNING statement), then
        authenticate it. A second use finds nothing. Returns None when absent
        or stale, ``{"tampered": True}`` when the MAC does not verify."""
        if not self.db_path(project_root).is_file():
            return None
        with self._conn(project_root) as conn:
            row = conn.execute(
                f"DELETE FROM {self._TABLE} WHERE session_id = ? AND nonce = ? "
                "RETURNING session_id, nonce, command, rules, created_at, request_id, mac",
                (session_id, nonce),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        sid, rnonce, command, rules_json, created, rid, mac = (
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), float(row[4]),
            str(row[5]), str(row[6]),
        )
        if created < _now() - PENDING_ASK_TTL_S:
            return None
        want = _row_mac(sid, rnonce, command, rules_json, created, rid)
        if not hmac.compare_digest(want, mac):
            return {"tampered": True, "command": command}
        try:
            rules = json.loads(rules_json or "[]")
        except ValueError:
            return {"tampered": True, "command": command}
        return {
            "command": command,
            "rules": rules,
            "request_id": rid,
            "raw": (sid, rnonce, command, rules_json, created, rid, mac),
        }

    def _restore_ask(self, project_root: Path, raw: tuple) -> None:
        """Put a consumed row back VERBATIM (same MAC) after a failed write,
        so the operator's grant is not lost and can be retried."""
        with self._conn(project_root) as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO {self._TABLE} (session_id, nonce, command, rules, "
                "created_at, request_id, mac) VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(raw),
            )
            conn.commit()


# ── the permanent writer ────────────────────────────────────────────────────


class PermanentWriteFailed(RuntimeError):
    """The permanent rule could not be made effective; every write it made
    was rolled back (``partial`` names any key whose rollback failed)."""

    def __init__(self, why: str, partial: list[str] | None = None) -> None:
        super().__init__(why)
        self.why = why
        self.partial = list(partial or [])


def _write_permanent_shell_allow(
    project_root: Path,
    rules: list[list[str]],
    *,
    command: str,
    nonce: str,
    host_session_id: str,
    ctx: Any,
    request_id: str = "",
) -> None:
    """Write the exact rules through ConfigStore (audited) at project scope,
    verify by READ-BACK that the whole command is now allowed and that the
    effective allow entries equal the simulated ones, then record the
    operator-attributed grant. Any failure rolls every written key back to
    its prior project-scope value and raises PermanentWriteFailed."""
    from .bash_policy import evaluate_bash_policy, load_canonical_bash_policy
    from .config_store import ConfigStore

    policy = load_canonical_bash_policy(project_root=project_root) or {}
    allow = policy.get("allow") or {}
    # Group per base: ONE write per base carrying ALL of its patterns, so two
    # rules for the same base (`foo a && foo b`) never overwrite each other.
    targets: dict[str, Any] = {}
    for base, pattern in rules:
        if not _RULE_BASE_RE.match(base or ""):
            raise PermanentWriteFailed(f"invalid rule base {base!r}")
        targets[base] = _merged_entry(targets.get(base, allow.get(base)), pattern)
    simulated = _with_rules(policy, rules)

    from .execution_index_store import ExecutionIndexStore

    store = ConfigStore()
    previous: dict[str, Any] = {}
    written: list[str] = []
    failure = ""
    user_id = str(getattr(ctx, "user_id", "") or "")
    role = str(getattr(ctx, "role", "") or "")
    # ALL-OR-NOTHING: snapshot, write, read back, AUDIT. The audit row is part
    # of the unit -- a grant without its attributable ink is rolled back.
    try:
        for base in targets:
            key = f"bash.allow.{base}"
            previous[key] = store.get(project_root, key, scope="project")
        for base, entry in targets.items():
            key = f"bash.allow.{base}"
            # Intent BEFORE the write: a set that commits and then raises
            # must still be rolled back and verified.
            written.append(key)
            store.set(project_root, key, entry, scope="project")
        after = load_canonical_bash_policy(project_root=project_root) or {}
        eff_allow = after.get("allow") or {}
        sim_allow = simulated.get("allow") or {}
        if any(
            sorted(_entry_patterns(eff_allow.get(b))) != sorted(_entry_patterns(sim_allow.get(b)))
            for b in targets
        ):
            failure = "read-back: effective allow entries differ from the simulated policy"
        elif not evaluate_bash_policy(command, after, workspace_root=str(project_root)).get(
            "allowed"
        ):
            failure = "read-back: the command still does not evaluate as allowed"
        else:
            ExecutionIndexStore().record_event(
                project_root,
                event_kind="operator_shell_allow_always",
                source_kind="shell_ask_grant",
                session_id=host_session_id or None,
                capability_name="shell_ask_grant",
                action_kind="allow_always",
                target_entity="bash.allow",
                status="applied",
                user_id=user_id or None,
                effective_role=role or None,
                principal_type="human",
                scope_type="project",
                scope_id=str(project_root).replace("\\", "/"),
                payload={
                    "user_id": user_id,
                    "role": role,
                    "nonce": nonce,
                    "command": command,
                    "rules": rules,
                    "host_session_id": host_session_id,
                    "request_id": request_id,
                    "origin": "user_prompt_submit",
                },
            )
    except Exception as exc:
        failure = failure or f"config write or audit failed ({type(exc).__name__})"
    if not failure:
        return
    # Restore + verify EVERY target key that has a pre-write snapshot (writes
    # start only after all snapshots succeeded, so an unsnapshotted key was
    # never written). Not just `written`: a commit-then-raise is covered.
    for key in reversed(list(previous)):
        try:
            if store.get(project_root, key, scope="project") == previous[key]:
                continue
            if previous[key] is None:
                store.delete(project_root, key, scope="project")
            else:
                store.set(project_root, key, previous[key], scope="project")
        except Exception:
            pass
    # "Nothing granted" is claimed ONLY when the post-state is verified equal
    # to the pre-state; every key that differs (or cannot be read) is partial.
    partial: list[str] = []
    for key in previous:
        try:
            if store.get(project_root, key, scope="project") != previous.get(key):
                partial.append(key)
        except Exception:
            partial.append(key)
    raise PermanentWriteFailed(failure, partial)


def _operator_may_write(
    project_root: Path, host_session_id: str, window_key: str, auth_service: Any
) -> tuple[Any, str]:
    if auth_service is None:
        from .operator_auth_service import OperatorAuthService

        auth_service = OperatorAuthService()
    try:
        if window_key:
            ctx = auth_service.resolve_operator_context_from_window(window_key, project_root)
        else:
            ctx = auth_service.resolve_operator_context_from_host_session(
                host_session_id, project_root
            )
    except Exception:
        ctx = None
    if ctx is None:
        return None, "no authenticated operator is bound to this window"
    try:
        ok = auth_service.require_permission(
            ctx,
            _PERM_MANAGE_CONFIG,
            project_root,
            scope_type="project",
            scope_id=str(project_root).replace("\\", "/"),
        )
    except Exception:
        ok = False
    if not ok:
        return None, f"the operator lacks {_PERM_MANAGE_CONFIG}"
    return ctx, ""


# ── the ONE entry: the operator's own prompt ────────────────────────────────


def apply_operator_shell_grants(
    project_root: Path,
    host_session_id: str,
    operator_text: str,
    *,
    window_key: str = "",
    auth_service: Any = None,
) -> str:
    """Apply ``allow always <id>`` tokens found in the operator's
    provenance-floored prompt. Called ONLY from the UPS leg. Returns the
    additional-context note ("" when no token)."""
    sid = str(host_session_id or "").strip()
    nonces = parse_always_tokens(operator_text)
    if not sid or not nonces:
        return ""
    store = ShellAskGrantStore()
    notes: list[str] = []
    for nonce in nonces:
        token = f"`allow always {nonce}`"
        no_match = (
            f"AIDOCS: {token} matched no pending shell ask in this session "
            "(unknown, stale, foreign or already used); nothing was granted."
        )
        if not store._peek_ask(project_root, sid, nonce):
            notes.append(no_match)
            continue
        # WHO before consuming: a refused operator keeps the ask answerable.
        ctx, why = _operator_may_write(project_root, sid, window_key, auth_service)
        if ctx is None:
            notes.append(f"AIDOCS: {token}: permanent rule not written ({why}).")
            continue
        # Consume ATOMICALLY before anything is written; then authenticate.
        pending = store._take_ask(project_root, sid, nonce)
        if pending is None:
            notes.append(no_match)
            continue
        command = pending.get("command", "")
        if pending.get("tampered"):
            notes.append(
                f"AIDOCS: {token}: the pending ask record failed its integrity check "
                "(MAC mismatch); it was discarded and nothing was written."
            )
            continue
        rules = [list(r) for r in pending.get("rules") or [] if len(r) == 2]
        # Defense in depth: the rules must still be exactly what the law would
        # propose for this command now.
        if not rules or rules != proposed_permanent_rules(project_root, command):
            notes.append(
                f"AIDOCS: {token}: permanent rule for `{command}` not written "
                "(the proposed rule no longer matches the current policy)."
            )
            continue
        try:
            _write_permanent_shell_allow(
                project_root,
                rules,
                command=command,
                nonce=nonce,
                host_session_id=sid,
                ctx=ctx,
                request_id=str(pending.get("request_id") or ""),
            )
        except Exception as exc:
            why = getattr(exc, "why", "") or type(exc).__name__
            partial = list(getattr(exc, "partial", []) or [])
            if partial:
                # The post-state is NOT the pre-state: never claim "nothing
                # granted" and never re-arm the nonce for a half-applied grant.
                notes.append(
                    f"AIDOCS: {token}: PARTIAL STATE for `{command}` ({why}); rollback "
                    "could not restore "
                    + ", ".join(f"`{k}`" for k in partial)
                    + " to its prior value. Those keys may still be changed -- check "
                    "them on the dashboard. The ask was consumed and is NOT retryable."
                )
                continue
            restored = True
            try:
                store._restore_ask(project_root, pending["raw"])
            except Exception:
                restored = False
            note = (
                f"AIDOCS: {token}: NOTHING was granted for `{command}` ({why}); "
                "every rule written was rolled back and the policy verified unchanged."
            )
            note += (
                f" The ask is still answerable with `allow always {nonce}`."
                if restored
                else " The pending ask could not be restored; re-run the command to get a new id."
            )
            notes.append(note)
            continue
        notes.append(f"AIDOCS: Permanently allowed: `{command}` via {_rules_text(rules)}.")
    return "\n".join(notes)
