"""Operator-intent resolver — first vertical slice (/goal 2026-05-20).

Maps an authenticated operator's NATURAL-LANGUAGE request into a
structured, permission-gated, audited control-plane action.

## Where this sits in the pipeline

    UserPromptSubmit
      → existing NLP/intent extraction (intent_grant_detector,
        aidocs_nlp consumers)
      → OperatorIntentResolver            ← THIS MODULE
          → require host-bound OperatorContext (host_binding)
          → role / permission / surface policy check
          → exact-confirm gate (for dangerous routes)
          → canonical service call (ConfigStore — never raw sqlite/file)
          → audit (user_id, role, source, action, target, scope,
                   prompt_hash, status, reason)

## The seal, restated

  - NLP alone authorizes NOTHING. The resolver only proposes WHAT was
    requested; it never grants.
  - host_binding proves WHO. Without an approved binding the request is
    refused — there is no env / token fallback on this path.
  - operator_intent proves WHAT. A structured route, not free text,
    reaches the permission service.
  - the permission service decides IF ALLOWED. RBAC, via
    OperatorAuthService.require_permission.
  - the canonical service performs the mutation. ConfigStore.set, which
    carries its own config_write audit. The resolver NEVER opens a
    sqlite connection or writes a file directly.
  - audit records the seal — every terminal state (applied / refused /
    needs_confirmation / needs_exact_confirm) emits one row.
  - MCP tools and agents cannot self-unlock guardrails: an agent-
    originated principal is refused before any identity is even
    resolved. This path is reachable only from a human host prompt.
  - Operator intent is ORIGIN-BOUND. Text quoted, forwarded, generated,
    delegated, replayed, or sent to workers is INERT, even if it contains
    an exact operator phrase. Only a verified direct-human-submit origin
    (see is_authority_bearing_prompt_eligible) may enter detection or
    mutation — prompt shape is not authority, and NLP is not authority.

## Why the NLP service is injected, never bypassed

The match is computed from spaCy lemmas (Substance.verbs / .nouns),
NOT a parallel regex list. The single regex in `_keyword_fallback` is
a DEGRADED path used only when no spaCy pack is installed, and it is
flagged in the resolution (`parse_source="keyword_fallback"`) so the
audit chain can tell a real parse from a degraded one. Tests inject a
fake NLPService to prove the lemma path — not regex — drives matching.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .aidocs_nlp.service import NLPService
    from .operator_auth_service import OperatorContext


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OperatorIntentRoute:
    """One structured action an operator may request in natural language.

    A route is pure data: it never performs the mutation itself. The
    resolver matches a prompt to a route via NLP lemmas, then hands the
    route to the permission service and (if allowed) the canonical
    service named by ``canonical_service_route``.
    """

    route_id: str
    action: str  # "enable" | "disable" | ...
    target: str  # canonical setting path / entity id
    value: Any  # value the canonical service should write
    scope: str  # "session" | "project" | "global"
    required_permission: str
    canonical_service_route: str  # key into the canonical-service registry
    # Lemmas that MUST appear (lemmatized) for the route to fire. All
    # are required — partial matches lower confidence.
    trigger_lemmas: frozenset[str]
    # Lemmas that, when present, pin the scope. e.g. {"session"} → session.
    scope_lemmas: frozenset[str] = frozenset()
    # #258: OBJECT lemmas — the non-verb tokens naming what this route acts on
    # (e.g. {"bash","allowlist"}, {"decision","trace"}). When non-empty, at
    # least one MUST co-occur with the trigger verb or the route does not
    # classify — so a bare grant verb ("add", "enable") on an unrelated prompt
    # ("add to memory", "add a todo") stops mis-firing this route. Strictly
    # reduces false positives; every real request naming the object still fires.
    object_lemmas: frozenset[str] = frozenset()
    requires_exact_confirm: bool = False
    exact_confirm_phrase: str = ""
    # Read-only routes report state and NEVER mutate. They skip the
    # exact-confirm gate, call a reader (not a writer) on the canonical
    # seam, and audit a non-mutating terminal status ("reported").
    read_only: bool = False
    # Routes whose value is parsed from the prompt (not a fixed constant)
    # name a bounded extractor here. The extractor runs ONLY after NLP
    # classifies the route, and never decides intent — it only parses a
    # structured, validated value out of the already-classified prompt.
    value_extractor: str = ""


# Canonical setting the decision-trace routes mutate.
_DECISION_TRACE_SETTING = "security.emit_decision_trace"
_PERM_MANAGE_CONFIG = "admin.manage_config"
# Narrower read permission for status/report routes — a viewing
# capability, not config-mutation authority. The admin role holds it.
_PERM_VIEW_DASHBOARD = "admin.view_dashboard"
# Logical target name for the bash-allowlist route. The physical config
# key written is bash.allow.<command> — the logical name keeps the audit
# row stable regardless of which command was added.
_BASH_ALLOWLIST_TARGET = "bash.allowlist"


# First supported routes. The session route is the /goal's primary
# deliverable; the global route is a genuinely more dangerous variant
# (project-wide, persistent emission of redacted-but-revealing traces)
# so it carries an exact-confirm gate — demonstrating that dangerous
# routes cannot mutate on a paraphrase alone.
ROUTES: dict[str, OperatorIntentRoute] = {
    "enable_decision_trace_session": OperatorIntentRoute(
        route_id="enable_decision_trace_session",
        action="enable",
        target=_DECISION_TRACE_SETTING,
        value=True,
        scope="session",
        required_permission=_PERM_MANAGE_CONFIG,
        canonical_service_route="config_set",
        trigger_lemmas=frozenset({"enable", "decision", "trace"}),
        scope_lemmas=frozenset({"session"}),
        object_lemmas=frozenset({"decision", "trace"}),
        requires_exact_confirm=False,
    ),
    "add_bash_allowlist_session": OperatorIntentRoute(
        route_id="add_bash_allowlist_session",
        action="add",
        target=_BASH_ALLOWLIST_TARGET,
        value=None,  # filled by the bounded extractor post-classification
        scope="session",
        required_permission=_PERM_MANAGE_CONFIG,
        canonical_service_route="bash_allowlist_add",
        trigger_lemmas=frozenset({"add", "bash", "allowlist"}),
        scope_lemmas=frozenset({"session"}),
        object_lemmas=frozenset({"bash", "allowlist"}),
        requires_exact_confirm=True,
        value_extractor="bash_allowlist_add",
    ),
    "remove_bash_allowlist_session": OperatorIntentRoute(
        route_id="remove_bash_allowlist_session",
        action="remove",
        target=_BASH_ALLOWLIST_TARGET,
        value=None,  # filled by the bounded extractor post-classification
        scope="session",
        required_permission=_PERM_MANAGE_CONFIG,
        canonical_service_route="bash_allowlist_remove",
        trigger_lemmas=frozenset({"remove", "bash", "allowlist"}),
        scope_lemmas=frozenset({"session"}),
        object_lemmas=frozenset({"bash", "allowlist"}),
        requires_exact_confirm=True,
        value_extractor="bash_allowlist_remove",
    ),
    "status_bash_allowlist_session": OperatorIntentRoute(
        route_id="status_bash_allowlist_session",
        action="status",
        target=_BASH_ALLOWLIST_TARGET,
        value=None,
        scope="session",
        required_permission=_PERM_VIEW_DASHBOARD,
        canonical_service_route="bash_allowlist_read",
        trigger_lemmas=frozenset({"show", "bash", "allowlist"}),
        scope_lemmas=frozenset({"session"}),
        object_lemmas=frozenset({"bash", "allowlist"}),
        requires_exact_confirm=False,
        read_only=True,
    ),
    "status_decision_trace_session": OperatorIntentRoute(
        route_id="status_decision_trace_session",
        action="status",
        target=_DECISION_TRACE_SETTING,
        value=None,
        scope="session",
        required_permission=_PERM_VIEW_DASHBOARD,
        canonical_service_route="config_read",
        trigger_lemmas=frozenset({"show", "decision", "trace", "status"}),
        scope_lemmas=frozenset({"session"}),
        object_lemmas=frozenset({"decision", "trace"}),
        requires_exact_confirm=False,
        read_only=True,
    ),
    "disable_decision_trace_session": OperatorIntentRoute(
        route_id="disable_decision_trace_session",
        action="disable",
        target=_DECISION_TRACE_SETTING,
        value=False,
        scope="session",
        required_permission=_PERM_MANAGE_CONFIG,
        canonical_service_route="config_set",
        trigger_lemmas=frozenset({"disable", "decision", "trace"}),
        scope_lemmas=frozenset({"session"}),
        object_lemmas=frozenset({"decision", "trace"}),
        requires_exact_confirm=False,
    ),
    "enable_decision_trace_global": OperatorIntentRoute(
        route_id="enable_decision_trace_global",
        action="enable",
        target=_DECISION_TRACE_SETTING,
        value=True,
        scope="global",
        required_permission=_PERM_MANAGE_CONFIG,
        canonical_service_route="config_set",
        trigger_lemmas=frozenset({"enable", "decision", "trace"}),
        scope_lemmas=frozenset({"global", "everywhere", "all"}),
        object_lemmas=frozenset({"decision", "trace"}),
        requires_exact_confirm=True,
        exact_confirm_phrase="enable decision trace globally",
    ),
    # NOTE (2026-06-12): the `enable_dev_mode_session` route was removed
    # along with the `dev.dev_mode` setting. Self-edit / dev authority is
    # now derived from the DEV distribution flavour (global-locked), which
    # no NLP phrase can flip — so no recognize-and-refuse route is needed.
}


# ---------------------------------------------------------------------------
# Route-table invariant classification (hardening 2026-05-20)
# ---------------------------------------------------------------------------
# These sets make the route-table invariants machine-checkable (see
# tests/host/test_operator_intent_route_invariants.py). Adding a route or
# a new permission means updating the relevant set here — a deliberate,
# reviewed change rather than an implicit one.

# Permissions that authorize a MUTATION. Every mutating route's
# required_permission MUST be in this set.
MUTATION_PERMISSIONS: frozenset[str] = frozenset({_PERM_MANAGE_CONFIG})

# Permissions that authorize a READ. Every read-only route's
# required_permission MUST be in this set — a read route must never carry
# a mutation permission.
READ_PERMISSIONS: frozenset[str] = frozenset({_PERM_VIEW_DASHBOARD})

# Mutating routes intentionally NOT exact-confirm gated because they are
# safe, low-risk and trivially reversible (toggling redacted decision-
# trace emission for a single session). EVERY other mutating route MUST
# be exact-confirm gated. Adding a route here is a deliberate security
# decision — keep this set minimal and reviewed.
SAFE_MUTATION_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "enable_decision_trace_session",
        "disable_decision_trace_session",
    },
)

# Canonical service routes that write/read the route's ``target`` VERBATIM as a
# config setting path (vs. a logical/derived target like bash.allowlist, whose
# physical key is bash.allow.<command>). A route using one of these MUST have its
# target in SETTINGS_CATALOG — otherwise _is_dashboard_only_target() can't see a
# catalog entry and would return False (a silent T0 bypass for a future
# guardrail route). The route-invariant test enforces this membership.
CONFIG_BACKED_SERVICE_ROUTES: frozenset[str] = frozenset(
    {
        "config_set",
        "config_read",
    },
)


# Operator-intent is HUMAN-ONLY and fails closed: only an explicit
# verified-human principal proceeds; ANY other value (a known agent
# principal, an empty string, an 'unknown' attribution hiccup) is
# refused. This is an allowlist, not a denylist — a new principal type
# we have never seen must default to refused, never to allowed.
_HUMAN_PRINCIPALS: frozenset[str] = frozenset({"human", "operator"})

# Known agent/tool principals get the specific self-unlock reason so
# audits distinguish "a sub-agent tried to grant itself power" from a
# generic non-human signal. (Refusal is identical either way; only the
# audited reason differs.)
_AGENT_PRINCIPALS: frozenset[str] = frozenset(
    {
        "mcp",
        "agent",
        "subagent",
        "sub_agent",
        "lane_agent",
        "worker",
        "tool",
    },
)

# Below this, the prompt is too ambiguous to mutate — caller surfaces
# a confirmation instead of acting.
_CONFIDENCE_THRESHOLD = 0.75


# ---------------------------------------------------------------------------
# Resolution + outcome shapes
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResolvedIntent:
    """What the NLP layer believes the operator asked for. Carries NO
    authority — it is a proposal handed to the permission service.
    """

    route: OperatorIntentRoute
    action: str
    target: str
    value: Any
    scope: str
    confidence: float
    required_permission: str
    requires_exact_confirm: bool
    canonical_service_route: str
    matched_lemmas: tuple[str, ...]
    parse_source: str  # "spacy:en_core_web_sm" | "keyword_fallback" | "unavailable"
    # Set by extractor routes. extraction_error is "" on success; a
    # non-empty value (e.g. "compound_command") means the prompt
    # classified but its value failed validation and must be refused.
    extraction_error: str = ""
    # The exact phrase the operator must type to satisfy an exact-confirm
    # gate. Static for fixed routes; computed per-command for extractor
    # routes. Empty when the route needs no confirm.
    expected_confirm_phrase: str = ""


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    """Terminal state of one operator-intent evaluation."""

    status: str  # applied|reported|refused|needs_confirmation|needs_exact_confirm|no_intent
    reason: str
    mutated: bool = False
    route_id: str = ""
    action: str = ""
    target: str = ""
    scope: str = ""
    confidence: float = 0.0
    parse_source: str = ""
    prompt_hash: str = ""
    user_id: str = ""
    role: str = ""
    # Populated only by read-only (status) routes.
    read_value: Any = None
    provenance: str = ""
    # The structured value the route resolved to (e.g. the bash
    # allowlist {"command": ..., "scope": ...}). Lets the host render a
    # precise note without re-parsing.
    value: Any = None


def _is_dashboard_only_target(target: str) -> bool:
    """True if ``target`` is a T0 DASHBOARD-ONLY setting — a guardrail that, per
    security-gates.md §6, flips ONLY via the dashboard confirm-modal and can NEVER
    be set through the natural-language operator-intent path (no NLP phrase, no
    role, no exact-confirm lets an agent OR a human unlock it here). Fail-closed:
    if the catalog can't be read, treat as dashboard-only (refuse).
    """
    try:
        from .config_schema import SETTINGS_CATALOG as _CATALOG

        meta = _CATALOG.get(target)
    except Exception:
        return True
    if meta is None:
        return False
    return bool(meta.get("dashboard_only"))


def prompt_hash(prompt: str) -> str:
    """Stable, log-safe hash of the raw prompt (never store the prompt
    itself in the control-plane audit — only its hash).
    """
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Canonical service registry
# ---------------------------------------------------------------------------
def _canonical_config_set(
    project_root: Path,
    *,
    target: str,
    value: Any,
    scope: str,
    scope_key: str,
) -> None:
    """Canonical config mutation. Delegates to ConfigStore — the
    storage backend's public API, which emits its own
    config_write_internal audit. The resolver NEVER opens sqlite or
    writes a file; this single seam is the only mutation it performs.

    T0 seal (2026-05-25): dashboard-only settings are NEVER writable through
    the natural-language operator-intent path — not even for a verified
    human. Per the override taxonomy, AIDOCS guardrails (kill switches, raw-
    shell/edit unlocks, approved-roots, tier enforcement, …) flip ONLY via
    the dashboard's confirm-modal surface, so an NLP phrase can never toggle
    them. Refused before any write reaches the store.
    """
    try:
        from .config_schema import SETTINGS_CATALOG as _CATALOG

        _meta = _CATALOG.get(target)
    except Exception:
        _meta = None
    if _meta is not None and _meta.get("dashboard_only"):
        raise PermissionError(
            f"`{target}` is dashboard-only (T0) — it cannot be set through "
            f"the natural-language operator-intent path. Toggle it from the "
            f"AIDOCS dashboard, which is the sole authority for this setting.",
        )
    from .config_store import ConfigStore

    ConfigStore().set(
        project_root,
        target,
        value,
        scope=scope,
        scope_key=scope_key,
    )


def _canonical_bash_allowlist_add(
    project_root: Path,
    *,
    target: str,
    value: Any,
    scope: str,
    scope_key: str,
) -> None:
    """Canonical bash-allowlist mutation. The structured value carries
    the validated base command; we write the policy key
    ``bash.allow.<command> = ["*"]`` at the requested scope through
    ConfigStore — the same config seam every other write uses. The
    resolver never opens sqlite or writes a file. The base command was
    already validated by the extractor (single token, no metacharacters,
    not in the dangerous-command denylist) before reaching here.
    """
    command = ""
    if isinstance(value, dict):
        command = str(value.get("command") or "").strip()
    if not command:
        raise ValueError("bash_allowlist_add: empty command")
    from .config_store import ConfigStore

    ConfigStore().set(
        project_root,
        f"bash.allow.{command}",
        ["*"],
        scope=scope,
        scope_key=scope_key,
    )


def _canonical_bash_allowlist_remove(
    project_root: Path,
    *,
    target: str,
    value: Any,
    scope: str,
    scope_key: str,
) -> dict:
    """Canonical removal of a SESSION-scoped bash allowlist entry via the
    config seam (ConfigStore.delete). Deletes ONLY that session row —
    never an inherited project/global/factory entry. The delete's
    rowcount is the single race-safe source of truth for whether
    anything was removed.

      - row removed → {"status": "applied"}
      - nothing to remove → {"status": "noop",
                             "reason": "not_present_session_scope"}
    """
    command = ""
    if isinstance(value, dict):
        command = str(value.get("command") or "").strip()
    if not command:
        raise ValueError("bash_allowlist_remove: empty command")
    from .config_store import ConfigStore

    store = ConfigStore()
    key = f"bash.allow.{command}"
    # The delete result is the ONLY source of truth: ConfigStore.delete
    # is a single atomic DELETE that returns rowcount > 0. A pre-read +
    # delete would race (a concurrent delete between the two could yield
    # a false "applied"). The DELETE is scoped to scope=session +
    # scope_key, so it can only ever touch this session's row — never an
    # inherited project/global/factory entry.
    deleted = store.delete(
        project_root,
        key,
        scope="session",
        scope_key=scope_key,
    )
    if deleted:
        return {"status": "applied"}
    return {"status": "noop", "reason": "not_present_session_scope"}


# route key → callable(project_root, *, target, value, scope, scope_key)
# A service may return None (treated as applied) or a status dict
# {"status": "applied"|"noop", "reason": ...} for presence-aware ops.
CANONICAL_SERVICES: dict[str, Callable[..., Any]] = {
    "config_set": _canonical_config_set,
    "bash_allowlist_add": _canonical_bash_allowlist_add,
    "bash_allowlist_remove": _canonical_bash_allowlist_remove,
}


# ---------------------------------------------------------------------------
# Bounded value extractors — run ONLY after NLP classifies a route.
# ---------------------------------------------------------------------------
# A base command: starts with a letter, then letters/digits/_/-/./+ . NO
# whitespace, NO shell metacharacters. This is an allowlist character
# class — anything outside it is rejected, not sanitized.
_BASE_COMMAND_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.+-]*$")
# Shell metacharacters that must never appear in a base command.
_SHELL_METACHARS = (
    ";",
    "&",
    "|",
    ">",
    "<",
    "`",
    "$",
    "(",
    ")",
    "{",
    "}",
    "*",
    "?",
    "[",
    "]",
    "\\",
    '"',
    "'",
    "\n",
)
# Captures the command span between "add" and "to … bash allowlist".
_BASH_ADD_RE = re.compile(
    r"\badd\s+(.+?)\s+to\s+(?:the\s+)?bash\s+allow ?list",
    re.IGNORECASE,
)
# Captures the command span between "remove" and "from … bash allowlist".
_BASH_REMOVE_RE = re.compile(
    r"\bremove\s+(.+?)\s+from\s+(?:the\s+)?bash\s+allow ?list",
    re.IGNORECASE,
)


def _validate_and_normalize_command(candidate: str) -> tuple[str, str]:
    """Validate a single base-command candidate and normalize it to the
    runtime shape. Returns ``(normalized, error)`` — error="" on success.

    Reject rules (shared by add + remove): whitespace → compound_command;
    shell metacharacters or path separators → dangerous_command;
    non-base token → invalid_command; dangerous base command (denylist)
    → dangerous_command. Normalization mirrors bash_policy at runtime
    (lowercase, .exe stripped) so the key we touch is the key the gate
    reads.
    """
    candidate = (candidate or "").strip()
    if not candidate:
        return "", "no_command"
    if any(ch.isspace() for ch in candidate):
        return "", "compound_command"
    if any(ch in candidate for ch in _SHELL_METACHARS):
        return "", "dangerous_command"
    if "/" in candidate:
        return "", "dangerous_command"
    if not _BASE_COMMAND_RE.match(candidate):
        return "", "invalid_command"
    try:
        from .bash_policy import _normalize_basename

        normalized = _normalize_basename(candidate)
    except Exception:
        normalized = candidate.lower()
        normalized = normalized.removesuffix(".exe")
    if not normalized or not _BASE_COMMAND_RE.match(normalized):
        return "", "invalid_command"
    try:
        from .bash_policy import _JUDGE_DENYLIST

        if normalized in _JUDGE_DENYLIST:
            return "", "dangerous_command"
    except Exception:
        pass
    return normalized, ""


def _extract_bash_allowlist_add(prompt: str) -> tuple[Any, str, str]:
    """Bounded extractor for "add <command> to bash allowlist …".

    Returns ``(value, error, expected_confirm_phrase)``. Never lets an
    arbitrary shell string through: a single clean base command is the
    only accepted shape.
    """
    m = _BASH_ADD_RE.search(prompt or "")
    if not m:
        return None, "no_command", ""
    normalized, error = _validate_and_normalize_command(m.group(1))
    if error:
        return None, error, ""
    value = {"command": normalized, "scope": "session"}
    confirm = f"add {normalized} to bash allowlist for this session"
    return value, "", confirm


def _extract_bash_allowlist_remove(prompt: str) -> tuple[Any, str, str]:
    """Bounded extractor for "remove <command> from bash allowlist …".

    Same validation/normalization as the add extractor; only the verb,
    the capture regex, and the confirm phrasing differ.
    """
    m = _BASH_REMOVE_RE.search(prompt or "")
    if not m:
        return None, "no_command", ""
    normalized, error = _validate_and_normalize_command(m.group(1))
    if error:
        return None, error, ""
    value = {"command": normalized, "scope": "session"}
    confirm = f"remove {normalized} from bash allowlist for this session"
    return value, "", confirm


# extractor key → callable(prompt) -> (value, error, expected_confirm_phrase)
VALUE_EXTRACTORS: dict[str, Callable[[str], tuple[Any, str, str]]] = {
    "bash_allowlist_add": _extract_bash_allowlist_add,
    "bash_allowlist_remove": _extract_bash_allowlist_remove,
}


# Provenance layer name → operator-facing word.
_PROVENANCE_LABEL: dict[str, str] = {
    "factory": "default",
    "global": "global",
    "project": "project",
    "session": "session",
}


def _canonical_config_read(
    project_root: Path,
    *,
    target: str,
    scope: str,
    scope_key: str,
) -> tuple[Any, str]:
    """Canonical config READ. Delegates to LayeredConfigResolver — the
    same cascade ConfigStore.get_effective uses — so the resolver never
    opens sqlite or reads a file directly. Returns (effective_value,
    provenance) where provenance is one of default/global/project/
    session, reporting which layer supplied the value.
    """
    from .config_resolver import LayeredConfigResolver

    session_id = scope_key if scope == "session" else None
    resolved = LayeredConfigResolver().resolve(
        target,
        project_root,
        session_id=session_id,
    )
    layer = resolved.origin.get(target, "factory")
    return resolved.value, _PROVENANCE_LABEL.get(layer, layer or "default")


def _canonical_bash_allowlist_read(
    project_root: Path,
    *,
    target: str,
    scope: str,
    scope_key: str,
) -> tuple[Any, str]:
    """Canonical READ of the effective bash allowlist. Resolves the whole
    ``bash`` namespace through LayeredConfigResolver (no raw sqlite/file
    read) and partitions the allow entries by provenance layer.

    Returns ``(report, provenance)`` where report is::

        {
          "session":   [<cmds whose effective layer is session>],
          "inherited": [<cmds from project/global/factory>],
          "provenance": {<cmd>: <layer>},   # factory shown as "default"
        }

    so the caller can clearly distinguish session-scoped entries from
    inherited project/global/default ones.
    """
    from .config_resolver import LayeredConfigResolver

    session_id = scope_key if scope == "session" else None
    resolved = LayeredConfigResolver().resolve(
        "bash",
        project_root,
        session_id=session_id,
    )
    bash_cfg = resolved.value if isinstance(resolved.value, dict) else {}
    allow = bash_cfg.get("allow", {})
    allow = allow if isinstance(allow, dict) else {}
    origin = resolved.origin or {}

    session_entries: list[str] = []
    inherited: list[str] = []
    per_entry: dict[str, str] = {}
    for cmd in sorted(allow):
        layer = origin.get(f"bash.allow.{cmd}", "factory")
        label = _PROVENANCE_LABEL.get(layer, layer or "default")
        per_entry[cmd] = label
        if label == "session":
            session_entries.append(cmd)
        else:
            inherited.append(cmd)
    report = {
        "session": session_entries,
        "inherited": inherited,
        "provenance": per_entry,
    }
    return report, "session+inherited"


# route key → callable(project_root, *, target, scope, scope_key)
#             → (value, provenance)
CANONICAL_READERS: dict[str, Callable[..., tuple[Any, str]]] = {
    "config_read": _canonical_config_read,
    "bash_allowlist_read": _canonical_bash_allowlist_read,
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
class OperatorIntentResolver:
    """Resolves natural-language operator requests into gated mutations.

    Construct with optional service injection so tests can drive the
    spaCy lemma path and the canonical-service seam deterministically
    without a real model install or a real ConfigStore write.
    """

    def __init__(
        self,
        *,
        nlp_service: NLPService | None = None,
        canonical_services: dict[str, Callable[..., None]] | None = None,
        canonical_readers: dict[str, Callable[..., tuple[Any, str]]] | None = None,
        value_extractors: dict[str, Callable[[str], tuple[Any, str, str]]] | None = None,
        routes: dict[str, OperatorIntentRoute] | None = None,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD,
    ) -> None:
        self._nlp = nlp_service
        self._services = canonical_services or CANONICAL_SERVICES
        self._readers = canonical_readers or CANONICAL_READERS
        self._extractors = value_extractors or VALUE_EXTRACTORS
        self._routes = routes or ROUTES
        self._threshold = confidence_threshold

    # ── NLP parse ──────────────────────────────────────────────────
    def _get_nlp(self) -> NLPService | None:
        if self._nlp is not None:
            return self._nlp
        try:
            from .aidocs_nlp.service import get_service

            return get_service()
        except Exception:
            return None

    def _lemmas(self, prompt: str) -> tuple[set[str], str]:
        """Return (lemma_set, parse_source). Uses the spaCy Substance
        projection — verbs, nouns, adverbs, entities. Falls back to a
        normalized-token keyword set ONLY when no pack is available,
        flagged so audit can tell the two apart.
        """
        service = self._get_nlp()
        if service is not None:
            try:
                substance = service.analyze_substance(prompt)
            except Exception:
                substance = None
            if substance is not None:
                lemmas: set[str] = set()
                for collection in (
                    substance.verbs,
                    substance.nouns,
                    substance.adverbs,
                ):
                    for tok in collection:
                        lemma = (tok.lemma or tok.text or "").lower().strip()
                        if lemma:
                            lemmas.add(lemma)
                for ent in substance.entities:
                    for word in (ent.text or "").lower().split():
                        if word:
                            lemmas.add(word)
                # Identify which pipeline produced the parse for audit.
                source = "spacy"
                try:
                    doc = service.analyze(prompt)
                    if doc is not None and doc.pipeline_name:
                        source = doc.pipeline_name
                except Exception:
                    pass
                return lemmas, source
        return self._keyword_fallback(prompt), "keyword_fallback"

    @staticmethod
    def _keyword_fallback(prompt: str) -> set[str]:
        """Degraded path — normalized whitespace tokens. NOT a parallel
        intent parser: it carries no route logic, just lowercased words
        so a no-spaCy install still resolves the literal phrase.
        """
        cleaned = "".join(
            c if (c.isalnum() or c.isspace()) else " " for c in (prompt or "").lower()
        )
        return {w for w in cleaned.split() if w}

    def parse(self, prompt: str) -> ResolvedIntent | None:
        """Match a prompt to the best route via NLP lemmas. Returns the
        resolved intent (carrying confidence) or None when nothing
        matches at all. Confidence is the fraction of a route's required
        trigger lemmas present; scope lemmas refine which variant wins.
        """
        lemmas, parse_source = self._lemmas(prompt)
        if not lemmas:
            return None

        best: tuple[float, OperatorIntentRoute, tuple[str, ...]] | None = None
        for route in self._routes.values():
            required = route.trigger_lemmas
            if not required:
                continue
            matched = required & lemmas
            # #258: a grant VERB alone ("add", "enable") must not classify a
            # route. Require at least one OBJECT lemma (bash/allowlist,
            # decision/trace) to co-occur, so unrelated prompts like "add to
            # memory" / "add a todo" stop mis-firing the bash-allowlist route
            # (which then tripped an earlier auth gate and surfaced a spurious
            # refusal). Strictly reduces false positives — a real request that
            # names the object still matches (it contains the object lemma).
            if route.object_lemmas and not (route.object_lemmas & lemmas):
                continue
            base = len(matched) / len(required)
            if base <= 0:
                continue
            # Scope lemma presence is a tie-breaker bonus: a route whose
            # scope word appears outranks a sibling route on the same
            # trigger lemmas. Keeps "...for this session" from firing the
            # global variant and vice-versa.
            scope_bonus = 0.0
            if route.scope_lemmas:
                if route.scope_lemmas & lemmas:
                    scope_bonus = 0.05
                else:
                    # Required scope words absent → de-prioritize this
                    # variant so the better-scoped sibling wins.
                    scope_bonus = -0.10
            score = base + scope_bonus
            ranked = (score, route, tuple(sorted(matched)))
            if best is None or score > best[0]:
                best = ranked

        if best is None:
            return None
        score, route, matched = best
        confidence = max(0.0, min(1.0, score))

        # Default value/confirm come from the route. An extractor route
        # parses a structured value out of the (already-classified)
        # prompt and computes a per-command confirm phrase. The extractor
        # NEVER changes WHICH route fired — NLP already decided that.
        value: Any = route.value
        extraction_error = ""
        expected_confirm = route.exact_confirm_phrase
        if route.value_extractor:
            extractor = self._extractors.get(route.value_extractor)
            if extractor is None:
                extraction_error = "no_extractor"
            else:
                try:
                    value, extraction_error, conf = extractor(prompt)
                except Exception:
                    value, extraction_error, conf = None, "extractor_failed", ""
                if not extraction_error:
                    expected_confirm = conf

        return ResolvedIntent(
            route=route,
            action=route.action,
            target=route.target,
            value=value,
            scope=route.scope,
            confidence=confidence,
            required_permission=route.required_permission,
            requires_exact_confirm=route.requires_exact_confirm,
            canonical_service_route=route.canonical_service_route,
            matched_lemmas=matched,
            parse_source=parse_source,
            extraction_error=extraction_error,
            expected_confirm_phrase=expected_confirm,
        )

    # ── Full gated pipeline ────────────────────────────────────────
    def resolve_and_apply(
        self,
        prompt: str,
        *,
        project_root: Path,
        host_session_id: str,
        principal_type: str = "human",
        confirm_phrase: str = "",
        auth_service: object | None = None,
    ) -> IntentOutcome | None:
        """Run the full seal. Returns None when the prompt carries no
        operator intent at all (caller proceeds with normal flow);
        otherwise returns a terminal IntentOutcome and audits it.

        ``confirm_phrase`` is the exact text the operator typed to
        satisfy a dangerous route's exact-confirm gate (typically the
        same prompt, or a prior confirmation token).
        """
        phash = prompt_hash(prompt)

        # 1. NLP proposal. No intent → nothing to gate; return None so
        #    the caller's normal UserPromptSubmit flow continues. (We do
        #    NOT audit a non-event.)
        resolved = self.parse(prompt)
        if resolved is None:
            return None

        # A single audit closure carries the immutable per-call context
        # (project_root, resolved, phash, host_session_id) so every gate
        # records the SAME scope key that the mutation would/did use.
        def _audit(
            *,
            status,
            reason,
            ctx,
            mutated=False,
            read_value=None,
            provenance="",
            extra_payload=None,
        ):
            return self._audit_and_return(
                project_root,
                resolved,
                phash,
                status=status,
                reason=reason,
                ctx=ctx,
                principal_type=principal_type,
                host_session_id=host_session_id,
                mutated=mutated,
                read_value=read_value,
                provenance=provenance,
                extra_payload=extra_payload,
            )

        # 2. Human-only fail-closed guard. Operator-intent proceeds ONLY
        #    for an explicit verified-human principal — refused BEFORE
        #    resolving any identity. NLP authorizes nothing and a
        #    non-human principal authorizes nothing either. A known
        #    agent/tool principal gets the specific self-unlock reason;
        #    any other non-human value (empty / 'unknown') fails closed
        #    with a generic reason. An MCP tool or sub-agent therefore
        #    can never self-unlock a guardrail through this path.
        _p = (principal_type or "").strip().lower()
        if _p not in _HUMAN_PRINCIPALS:
            return _audit(
                status="refused",
                reason=(
                    "agent_cannot_self_authorize"
                    if _p in _AGENT_PRINCIPALS
                    else "non_human_principal"
                ),
                ctx=None,
            )

        # 3. host_binding proves WHO. No approved binding → refused.
        if auth_service is None:
            from .operator_auth_service import OperatorAuthService

            auth_service = OperatorAuthService()
        ctx: OperatorContext | None = None
        try:
            ctx = auth_service.resolve_operator_context_from_host_session(
                host_session_id,
                project_root,
            )
        except Exception:
            ctx = None
        if ctx is None:
            return _audit(
                status="refused",
                reason="unauthenticated_host_session",
                ctx=None,
            )

        # 4. Permission service decides IF ALLOWED.
        scope_id = self._scope_id(resolved.scope, host_session_id, project_root)
        try:
            allowed = auth_service.require_permission(
                ctx,
                resolved.required_permission,
                project_root,
                scope_type=("session" if resolved.scope == "session" else resolved.scope),
                scope_id=scope_id,
            )
        except Exception:
            allowed = False
        if not allowed:
            return _audit(
                status="refused",
                reason=f"missing_{resolved.required_permission.replace('.', '_')}",
                ctx=ctx,
            )

        # 4b. T0 dashboard-only guard. A guardrail target (dev_mode, kill
        #     switches, raw-shell/edit unlocks, approved-roots, tier enforcement)
        #     can NEVER flip through the natural-language operator-intent path —
        #     not even for a verified, permitted operator who types the exact
        #     confirm phrase. T0 overrides are dashboard-only by law. Refused HERE
        #     (after auth+permission so the layered reason is honest, before any
        #     confirm/mutation) with an explicit audited reason; the canonical
        #     service carries the same seal as defense-in-depth.
        if not resolved.route.read_only and _is_dashboard_only_target(resolved.target):
            return _audit(
                status="refused",
                reason="t0_dashboard_only",
                ctx=ctx,
            )

        # 5. Low confidence → confirm, never mutate.
        if resolved.confidence < self._threshold:
            return _audit(
                status="needs_confirmation",
                reason="low_confidence",
                ctx=ctx,
            )

        # 5b. Read-only (status) route. Authenticated + permitted +
        #     human + confident: report state. NO exact-confirm, NO
        #     mutation — call the reader on the canonical seam and audit
        #     a non-mutating terminal status ("reported").
        if resolved.route.read_only:
            reader = self._readers.get(resolved.canonical_service_route)
            if reader is None:
                return _audit(
                    status="refused",
                    reason="no_canonical_reader",
                    ctx=ctx,
                )
            try:
                value, provenance = reader(
                    project_root,
                    target=resolved.target,
                    scope=resolved.scope,
                    scope_key=(host_session_id if resolved.scope == "session" else ""),
                )
            except Exception as exc:
                return _audit(
                    status="refused",
                    reason=f"canonical_reader_failed:{exc}",
                    ctx=ctx,
                )
            return _audit(
                status="reported",
                reason="ok",
                ctx=ctx,
                read_value=value,
                provenance=provenance,
                extra_payload={
                    "effective_value": value,
                    "provenance": provenance,
                },
            )

        # 5c. Extractor validation. The route classified, but its parsed
        #     value failed validation (compound/dangerous/invalid/empty
        #     command). Refuse with the specific reason — only AFTER the
        #     auth gates, so validation behavior isn't exposed to
        #     unauthenticated callers, and BEFORE any mutation.
        if resolved.extraction_error:
            return _audit(
                status="refused",
                reason=resolved.extraction_error,
                ctx=ctx,
            )

        # 6. Dangerous route exact-confirm gate. Uses the per-resolution
        #    expected phrase (static for fixed routes, per-command for
        #    extractor routes) so a paraphrase never mutates.
        if resolved.requires_exact_confirm:
            want = (resolved.expected_confirm_phrase or "").strip().lower()
            got = (confirm_phrase or "").strip().lower()
            if not want or got != want:
                return _audit(
                    status="needs_exact_confirm",
                    reason="exact_confirm_required",
                    ctx=ctx,
                )

        # 7. Canonical service performs the mutation. Never raw sqlite.
        service = self._services.get(resolved.canonical_service_route)
        if service is None:
            return _audit(
                status="refused",
                reason="no_canonical_service",
                ctx=ctx,
            )
        try:
            result = service(
                project_root,
                target=resolved.target,
                value=resolved.value,
                scope=resolved.scope,
                scope_key=(host_session_id if resolved.scope == "session" else ""),
            )
        except Exception as exc:
            return _audit(
                status="refused",
                reason=f"canonical_service_failed:{exc}",
                ctx=ctx,
            )

        # 8. Audit the seal. A presence-aware service (e.g. remove) may
        #    return a status dict {"status": "noop", "reason": ...} to
        #    report a non-mutating outcome — a remove of an entry that
        #    was never at session scope. None means a plain applied write.
        if isinstance(result, dict) and result.get("status") == "noop":
            return _audit(
                status="noop",
                reason=result.get("reason", "noop"),
                ctx=ctx,
                mutated=False,
            )
        return _audit(status="applied", reason="ok", ctx=ctx, mutated=True)

    # ── helpers ────────────────────────────────────────────────────
    @staticmethod
    def _scope_id(scope: str, host_session_id: str, project_root: Path) -> str:
        if scope == "session" and host_session_id:
            return host_session_id
        return str(project_root).replace("\\", "/")

    def _audit_and_return(
        self,
        project_root: Path,
        resolved: ResolvedIntent,
        phash: str,
        *,
        status: str,
        reason: str,
        ctx: OperatorContext | None,
        principal_type: str,
        host_session_id: str = "",
        mutated: bool = False,
        read_value: Any = None,
        provenance: str = "",
        extra_payload: dict[str, Any] | None = None,
    ) -> IntentOutcome:
        user_id = getattr(ctx, "user_id", "") or ""
        role = getattr(ctx, "role", "") or ""
        source = getattr(ctx, "source", "operator_intent") or "operator_intent"
        # Audit must record the REAL principal — never default to
        # "human". An empty/whitespace principal is an unverified actor
        # and is stamped 'unknown' so a refused empty principal can never
        # be misattributed as a human operator in the forensic chain.
        audit_principal_type = str(principal_type or "").strip() or "unknown"
        # Audit must preserve the EXACT scope key the mutation used.
        # For a session-scoped route that is the host_session_id (the
        # ConfigStore scope_key); for project/global it is the canonical
        # scope id. Recording the literal "session" would lose which
        # host session received the seal.
        scope_id = self._scope_id(
            resolved.scope,
            host_session_id,
            project_root,
        )
        payload = {
            # The seal — every column mirrored into payload so JSON-only
            # audit consumers see the full tuple without joining against
            # the row columns.
            "user_id": user_id,
            "role": role,
            "source": source,
            "action": resolved.action,
            "target": resolved.target,
            "value": resolved.value,
            "scope": resolved.scope,
            "scope_id": scope_id,
            "host_session_id": host_session_id,
            "prompt_hash": phash,
            "status": status,
            "reason": reason,
            "confidence": resolved.confidence,
            "parse_source": resolved.parse_source,
            "principal_type": audit_principal_type,
            "route_id": resolved.route.route_id,
        }
        if extra_payload:
            payload.update(extra_payload)
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="operator_intent",
                source_kind="operator_intent",
                capability_name=resolved.route.route_id,
                action_kind=resolved.action,
                target_entity=resolved.target,
                status=status,
                user_id=user_id or None,
                effective_role=role or None,
                principal_type=audit_principal_type,
                scope_type=resolved.scope,
                scope_id=scope_id,
                payload=payload,
            )
        except Exception:
            # Audit is best-effort; never let an audit hiccup change the
            # control-plane decision.
            pass
        return IntentOutcome(
            status=status,
            reason=reason,
            mutated=mutated,
            route_id=resolved.route.route_id,
            action=resolved.action,
            target=resolved.target,
            scope=resolved.scope,
            confidence=resolved.confidence,
            parse_source=resolved.parse_source,
            prompt_hash=phash,
            user_id=user_id,
            role=role,
            read_value=read_value,
            provenance=provenance,
            value=resolved.value,
        )


# ---------------------------------------------------------------------------
# Prompt-origin gate (the origin-bound law)
# ---------------------------------------------------------------------------
# INVARIANT: Operator intent is ORIGIN-BOUND. Text quoted, forwarded,
# generated, delegated, replayed, or sent to workers is INERT, even if it
# contains an exact operator phrase.
#
# Prompt shape is NOT authority. NLP classification is NOT authority. Only
# a verified direct-human-submit ORIGIN may enter operator-intent
# detection or mutation. This gate runs BEFORE parse / grant / warn /
# mutate. It is intentionally tiny and dependency-free (no NLP) so it is
# available even when the classifier is degraded.

# Host event kinds that CAN carry a direct human submit. Worker /
# delegated / automation prompts ride other markers (below), not these.
_DIRECT_SUBMIT_EVENTS: frozenset[str] = frozenset(
    {
        "UserPromptSubmit",  # Claude Code
        "oc_chat_message",  # OpenCode interactive chat
        "prompt_mutate",  # generic CC-style host bridge
        "direct_human_submit",  # explicit host marker
    },
)

# source_surface values that mark a trusted interactive human surface.
# Empty is tolerated (CC/OpenCode omit the field today); a NON-empty,
# non-trusted value is rejected.
_TRUSTED_SOURCE_SURFACES: frozenset[str] = frozenset(
    {
        "interactive_user",
        "interactive",
        "user",
        "tui",
        "repl",
    },
)

# Origins that are NEVER operator authority — quoted/forwarded/generated/
# delegated/replayed/worker/automation text. Matched against
# source_surface AND delivery so either field can disqualify.
_NON_OPERATOR_ORIGINS: frozenset[str] = frozenset(
    {
        "worker",
        "subagent",
        "lane",
        "lane_agent",
        "tool",
        "mcp",
        "compaction",
        "compact",
        "handoff",
        "replay",
        "automation",
        "system",
        "developer",
        "assistant",
        "task",
        "delegated",
        "p_flag",
        "q_flag",
        "print",
        "headless",
        "-p",
        "-q",
    },
)


def is_authority_bearing_prompt_eligible(context: dict | None) -> bool:
    """True ONLY for a verified direct-human-submit prompt origin.

    This is the ONE origin gate for ALL authority-bearing prompt
    consumption — operator intent AND every NLP grant / confirmation /
    mutation pipeline (sticky grants, user-intent grants, per-turn intent
    state, DNT grants, config-set grants, lane-exit, intent-phrase
    dispatch, freeze/approval consumption). Returns False (inert) for
    anything that is not provably a human's interactive submit:

      - event_kind must be a direct-submit event
      - principal_type must be explicit human/operator
      - host_session_id AND project_root must be present
      - NO worker / subagent / lane marker (is_worker, is_subagent,
        worker_lane_id, AIDOCS_EXPERT_LANE_ID-derived)
      - source_surface, if present, must be a trusted interactive surface
      - neither source_surface nor delivery may be a non-operator origin
        (worker/-p/-q/compaction/handoff/replay/tool/system/developer/…)

    No NLP, no auth, no mutation. Pure origin verification.

    LAW: Prompt origin, not prompt shape, decides whether authority-
    bearing NLP/grant logic may run. Text quoted, forwarded, generated,
    delegated, replayed, or sent to workers is inert even if it contains
    an exact operator/grant phrase.
    """
    if not isinstance(context, dict):
        return False
    # Required presence.
    if not str(context.get("host_session_id") or "").strip():
        return False
    if not str(context.get("project_root") or "").strip():
        return False
    # Direct-submit event only.
    if str(context.get("event_kind") or "").strip() not in _DIRECT_SUBMIT_EVENTS:
        return False
    # Explicit human/operator principal only (fail closed on empty).
    principal = str(context.get("principal_type") or "").strip().lower()
    if principal not in {"human", "operator"}:
        return False
    # No worker / subagent / lane markers.
    if context.get("is_worker") or context.get("is_subagent"):
        return False
    if str(context.get("worker_lane_id") or "").strip():
        return False
    # source_surface: trusted-or-omitted only.
    surface = str(context.get("source_surface") or "").strip().lower()
    if surface and surface not in _TRUSTED_SOURCE_SURFACES:
        return False
    if surface in _NON_OPERATOR_ORIGINS:
        return False
    # delivery channel: reject delegated / -p / -q / automation deliveries.
    delivery = str(context.get("delivery") or "").strip().lower()
    if delivery in _NON_OPERATOR_ORIGINS:
        return False
    return True


# Operator intent is one specific authority-bearing consumer; it shares
# the general is_authority_bearing_prompt_eligible law directly; no alias is
# kept because a second name can drift or become an orphaned consumer.

# ---------------------------------------------------------------------------
# Cross-host support helpers
# ---------------------------------------------------------------------------
# Operator intent is SUPPORTED only on the Claude-hook path (the one place
# that resolves a host_binding OperatorContext and calls resolve_and_apply).
# Every other host must surface this EXACT warning when a prompt looks like
# an operator-intent request — an explicit "not supported, nothing changed"
# notice, never a silent noop and never a faked success.
OPERATOR_INTENT_UNSUPPORTED_NOTE: str = (
    "Operator intent is not supported in this host. No config/bash-policy "
    "change was made. Use Claude-hook path or dashboard."
)


def looks_like_operator_intent(
    prompt: str,
    *,
    nlp_service: NLPService | None = None,
    threshold: float | None = None,
) -> bool:
    """True when ``prompt`` classifies as an operator-intent route at or
    above the confidence threshold.

    Pure classification: NO auth, NO host_binding, NO mutation. This is
    the canonical detector hosts use to decide whether to show the
    unsupported-operator-intent warning — so the "is this an operator
    request?" judgment lives in ONE place (the resolver), never
    re-implemented per host.
    """
    resolver = OperatorIntentResolver(nlp_service=nlp_service)
    resolved = resolver.parse(prompt)
    if resolved is None:
        return False
    t = resolver._threshold if threshold is None else threshold
    return resolved.confidence >= t
