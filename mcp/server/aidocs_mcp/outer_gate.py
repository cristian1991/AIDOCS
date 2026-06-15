"""Outer Gate — canonical admission + execution surface.

SINGLE verdict surface (`execute`): every tool call (Tier-R read, Tier-M edit,
Tier-M run, selector/control-plane) flows through ONE method that returns a
`CanonicalVerdict`. Adapters (GPT-web MCP, OpenCode, Claude, dashboard) call
only `execute()` and translate the verdict to host-native primitives — no
adapter owns admission law.

  * `discover()` — advertise the eligible surface (separate from execute; no
    verdict needed for metadata).
  * `execute(GateRequest)` → `CanonicalVerdict` — THE canonical admission +
    execution call. Returns pass / confirmable_freeze / forbidden. All existing
    `invoke()/edit()/run()` methods are kept as internal routing targets but
    are NOT the adapter surface.

Contract:
  * Disabled by default (``outer_gate.enabled``=False), loopback-only bind.
  * A real audit sink MUST be configured when enabled — unconfigured refuses.
  * Lookup is namespaced by (kind, name) so collisions fail closed.
  * NO model-carried confirmation tokens: edits flow through the single-call
    `apply()` path server-side. The handle is minted and consumed internally.
  * Confirmable-class actions (e.g. dangerous RUN) resolve through the canonical
    freeze_service / operator-approval path — never a model token.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import outer_gate_manifest as manifest

# Stdlib-only at import (the heavy canonical server is lazy-loaded inside the
# executor), so importing this keeps the gate import pure-stdlib.
from .outer_gate_executor import ExecutorRefusal as _ExecutorRefusal
from .outer_gate_executor import exec_project_id as _exec_project_id

if TYPE_CHECKING:
    from .runtime_service import RuntimeService
    from .service_hub import AidocsServiceHub

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# P2 invocation surface: read-proven Tier-R only. Tier-A/M are discoverable but
# NOT invokable until their later phases.
INVOKABLE_TIERS = frozenset({manifest.TIER_R})


def cross_kind_collisions(entries: list[dict]) -> dict[str, list[str]]:
    """Name → [kinds…] for any tool/command name used under more than one kind.
    Namespacing by (kind,name) means these don't overwrite, but they are
    reported so a collision is never invisible.
    """
    kinds: dict[str, set[str]] = {}
    for e in entries:
        kinds.setdefault(e["name"], set()).add(e["kind"])
    return {n: sorted(ks) for n, ks in kinds.items() if len(ks) > 1}


@dataclass
class GateRequest:
    tool_name: str
    kind: str = "mcp_tool"  # namespaced lookup: (kind, name)
    principal: dict | None = None
    project_root: str | None = None
    tool_input: dict = field(default_factory=dict)
    # SERVER-RESOLVED selected exec project root (from the token's gate selection,
    # validated against the registry). NOT a client-supplied path — the transport
    # sets it from GateProjectStore.current(); a caller can only influence it via
    # project_select (which rejects unknown project_ids). None ⇒ the gate's
    # configured default exec project.
    exec_root: str | None = None


@dataclass
class GateResult:
    ok: bool
    blocked_by: str = ""
    reason: str = ""
    result: Any = None
    audit_recorded: bool = False
    principal_user: str = ""
    exec_project_root: str = ""
    exec_project_id: str = ""


@dataclass
class CanonicalVerdict:
    """Single verdict emitted by `OuterGate.execute()` — shared by ALL adapters.

    Every tool call returns this one shape. The adapter (GPT-web MCP, OpenCode,
    Claude hook, dashboard) translates it to host-native primitives:
      * MCP: verdict="forbidden" → JSON-RPC error; "pass" → result with
        structuredContent; "confirmable_freeze" → pending confirmation surface.
      * OpenCode: maps blocked_by to gate block reason.
      * Any adapter: verdict + blocked_by + reason are enough to decide the
        host action; audit_id, freeze_id, pending_action add structured detail.

    Verdict taxonomy — determined by the gate's internal classification:
      * ``"pass"``               — action executed; ``result`` carries the output.
      * ``"confirmable_freeze"`` — action would be allowed but needs operator
        approval (freeze_service escalation); ``freeze_id`` references the
        pending escalation; ``pending_action`` describes what the operator sees.
      * ``"forbidden"``          — action denied; ``blocked_by`` + ``reason``
        explain why. The denial is already audited.
    """

    verdict: str
    blocked_by: str = ""
    reason: str = ""
    risk_class: str = ""
    audit_id: str = ""
    freeze_id: str | None = None
    pending_action: Any | None = None
    result: Any = None
    principal_user: str = ""
    exec_project_root: str = ""
    exec_project_id: str = ""


# Executor(manifest_entry, GateRequest) -> Any. Default is a SAFE STUB; this
# skeleton wires no real execution and never performs a mutation.
Executor = Callable[[dict, GateRequest], Any]


def _default_executor(entry: dict, request: GateRequest) -> dict:
    if entry.get("tier") != manifest.TIER_R:
        # Only read-proven Tier-R is invokable in P2; refuse anything else even
        # if it somehow reached here.
        raise RuntimeError(f"non_invokable_tier:{entry.get('tier')}")
    return {"ok": True, "stub": True, "tool": entry["name"], "tier": entry["tier"]}


class OuterGate:
    """In-process admission boundary. No network, no token issuer."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        bind: str | None = None,
        project_root: Path | None = None,
        executor: Executor | None = None,
        audit_sink: Callable[[dict], None] | None = None,
        manifest_entries: list[dict] | None = None,
        package_trusted: bool | None = None,
        exec_project_root: Path | str | None = None,
        hub: AidocsServiceHub | None = None,
        runtime: RuntimeService | None = None,
        legacy_fallback: bool = False,
    ) -> None:
        self._enabled = self._read_enabled(project_root) if enabled is None else bool(enabled)
        # Trusted-code boundary: the remote surface may only be exposed by an
        # official, fingerprint-matched install. Editable/drifted/unverified code
        # is NOT remote-trustworthy. None ⇒ resolve from package_integrity
        # (fail-closed on any error); tests inject an explicit verdict.
        self._package_trusted = package_trusted
        # The home whose canonical runtime-trust DB is authoritative for THIS
        # gate's remote-trust decision. Recorded (signed) trust is read back from
        # here; None ⇒ no DB to consult ⇒ unverified ⇒ never remote-trustworthy.
        self._project_root = project_root
        # The REAL execution project bound for Tier-R reads (separate from the
        # auth/trust home above). Defaults to the auth home when not given (the
        # single-project deployment). Its id is stamped on every result + audit.
        exec_root = exec_project_root if exec_project_root is not None else project_root
        self._exec_project_root = str(exec_root) if exec_root else ""
        self._exec_project_id = _exec_project_id(exec_root) if exec_root else ""
        self._bind = bind if bind is not None else self._read_bind(project_root)
        self._executor = executor or _default_executor
        # NO default no-op sink: an unconfigured sink must REFUSE, not silently
        # pass audit. None ⇒ not configured.
        self._audit_sink = audit_sink
        # LAZY manifest: building it is an AST scan of the whole source tree
        # (~hundreds of ms). The manifest is consulted ONLY after preconditions
        # pass (discover/invoke), so a precondition refusal (disabled /
        # non-loopback / package_untrusted / unauthenticated) must NOT pay for
        # it. Built on first real use via _ensure_index(). Law-preserving: the
        # entries never affect a refusal verdict. Doctrine: cheap refusal.
        self._manifest_entries_arg = manifest_entries
        self._by_key: dict[tuple[str, str], dict] = {}
        self._ambiguous: set[tuple[str, str]] = set()
        self._entries: list[dict] = []
        self._index_built = False
        # Canonical gate cascade hub/runtime (shared law state via SQLite).
        # When provided, execute() routes through gate_tool.enforce_tool_call
        # before dispatching to the execution handler. When absent, the gate
        # returns canonical_gate_unavailable unless legacy_fallback=True
        # (used only by the transport protocol tests).
        from .runtime_service import RuntimeService as _Rt
        from .service_hub import AidocsServiceHub as _Hub

        self._hub: _Hub | None = hub
        self._runtime: _Rt | None = runtime
        self._hub: _Hub | None = hub
        self._runtime: _Rt | None = runtime
        self._legacy_fallback = legacy_fallback
        if hub is not None and runtime is None:
            self._runtime = _Rt(hub)

    def _ensure_index(self) -> None:
        """Build the namespaced (kind, name) manifest index on first use. A true
        (kind,name) duplicate is marked ambiguous and fails closed on invoke.
        """
        if self._index_built:
            return
        # Live gate: route through the fingerprint-bound manifest cache (keyed by
        # the SAME package trust-chain fingerprint that authorizes the gate's
        # decision). Verdict-identical to build_manifest and fail-closed — an
        # unfingerprintable source recomputes rather than serving a stale cache.
        # Injected entries (tests) bypass the cache.
        entries = (
            self._manifest_entries_arg
            if self._manifest_entries_arg is not None
            else manifest.build_manifest_cached(fingerprint=manifest.manifest_source_fingerprint())
        )
        for e in entries:
            key = (e["kind"], e["name"])
            if key in self._by_key:
                self._ambiguous.add(key)
            self._by_key[key] = e
        self._entries = entries
        self._index_built = True

    # ── config ──
    @staticmethod
    def _read_enabled(project_root: Path | None) -> bool:
        try:
            from .config import get_setting

            return bool(get_setting("outer_gate.enabled", project_root=project_root, default=False))
        except Exception:
            return False  # fail-closed: unreadable config ⇒ gateway OFF

    @staticmethod
    def _read_bind(project_root: Path | None) -> str:
        try:
            from .config import get_setting

            return str(
                get_setting("outer_gate.bind", project_root=project_root, default="127.0.0.1")
                or "127.0.0.1",
            )
        except Exception:
            return "127.0.0.1"

    def is_enabled(self) -> bool:
        return self._enabled and self._bind in _LOOPBACK

    @staticmethod
    def _invokable_now(entry: dict) -> bool:
        return bool(entry["remote_eligible"]) and entry["tier"] in INVOKABLE_TIERS

    # ── discovery (eligible surface, with invokable_now flag) ──
    def discover(self, principal: dict | None = None) -> GateResult:
        gate = self._preconditions(principal)
        if gate is not None:
            return gate
        self._ensure_index()
        pub = []
        for e in self._entries:
            if not e["remote_eligible"]:
                continue
            pub.append(
                {
                    k: e[k]
                    for k in (
                        "name",
                        "kind",
                        "tier",
                        "enforcement_binding",
                        "data_scope",
                        "requires_auth",
                        "requires_project_binding",
                        "requires_audit",
                        "schema",
                    )
                }
                | {"invokable_now": self._invokable_now(e)},
            )
        return GateResult(
            ok=True,
            result=pub,
            principal_user=str((principal or {}).get("user_id", "")),
        )

    # ── canonical execution (single admission + execution surface) ──
    def execute(self, request: GateRequest) -> CanonicalVerdict:
        gate = self._preconditions(request.principal)
        if gate is not None:
            return self._to_verdict(gate)
        self._ensure_index()
        pr = manifest.resolve_remote_principal(request.principal)
        if not pr["ok"]:
            return CanonicalVerdict(
                verdict="forbidden",
                blocked_by="unauthenticated",
                reason=pr["reason"],
                exec_project_root=self._exec_project_root,
                exec_project_id=self._exec_project_id,
            )
        user = pr["user_id"]
        eff_root = request.exec_root or self._exec_project_root
        eff_id = _exec_project_id(request.exec_root) if request.exec_root else self._exec_project_id
        name = request.tool_name

        # ── 1. Scope check BEFORE canonical cascade ──
        # Under-scoped tokens must never reach freeze/session/cascade logic.
        from .outer_gate_edit import EDIT_ALLOWLIST
        from .outer_gate_executor import RUN_ALLOWLIST

        scope = set((request.principal or {}).get("scope") or [])
        if name in RUN_ALLOWLIST:
            if "project_run" not in scope:
                return CanonicalVerdict(
                    verdict="forbidden",
                    blocked_by="insufficient_scope",
                    reason="token lacks project_run scope",
                    principal_user=user,
                    exec_project_root=eff_root,
                    exec_project_id=eff_id,
                )
        elif name in EDIT_ALLOWLIST:
            if "tier_m_edit" not in scope:
                return CanonicalVerdict(
                    verdict="forbidden",
                    blocked_by="insufficient_scope",
                    reason="token lacks tier_m_edit scope",
                    principal_user=user,
                    exec_project_root=eff_root,
                    exec_project_id=eff_id,
                )
        elif "tier_r_invoke" not in scope:
            return CanonicalVerdict(
                verdict="forbidden",
                blocked_by="insufficient_scope",
                reason="token lacks tier_r_invoke scope",
                principal_user=user,
                exec_project_root=eff_root,
                exec_project_id=eff_id,
            )

        # ── 2. Canonical gate cascade via the shared enforce_tool_call ──
        if self._hub is not None:
            from .gate_tool import enforce_tool_call

            ef = enforce_tool_call(
                self._hub,
                Path(eff_root),
                tool_name=name,
                tool_input=request.tool_input or {},
                include_freeze=True,
                runtime=self._runtime,
            )
            if ef.refusal is not None:
                refusal = ef.refusal
                freeze_state = refusal.get("freeze_state")
                blocked_by = str(refusal.get("blocked_by", ""))
                if freeze_state or blocked_by in (
                    "session_frozen",
                    "judge_confirm_required",
                ):
                    req_id = ""
                    if isinstance(freeze_state, dict):
                        req_id = str(freeze_state.get("request_id", ""))
                    return CanonicalVerdict(
                        verdict="confirmable_freeze",
                        freeze_id=req_id,
                        pending_action={
                            "action": "confirm",
                            "id": req_id,
                            "reason": refusal.get(
                                "permissionDecisionReason",
                                refusal.get("reason", ""),
                            ),
                        },
                        blocked_by=blocked_by,
                        reason=refusal.get(
                            "permissionDecisionReason",
                            refusal.get("reason", ""),
                        ),
                        exec_project_root=eff_root,
                        exec_project_id=eff_id,
                    )
                return CanonicalVerdict(
                    verdict="forbidden",
                    blocked_by=blocked_by,
                    reason=refusal.get("reason", ""),
                    exec_project_root=eff_root,
                    exec_project_id=eff_id,
                )
            # refusal is None → allowed; execute via the appropriate handler.
            if name in RUN_ALLOWLIST:
                res = self.run(request)
                return self._to_verdict(res, user=user, eff_root=eff_root, eff_id=eff_id)
            if name in EDIT_ALLOWLIST:
                # Registry-driven EDIT entries skip the ai_str_replace-
                # specific propose/commit pipeline (which is shape-
                # coupled to path/old_string/new_string) and dispatch
                # through fastmcp's call_tool instead. The underlying
                # impls enforce their own audit + path-protection +
                # size limits; the gate adds scope + exec-root binding
                # + two-phase confirm (per registry spec).
                #
                # Doctrine 2026-05-29: GATE_ONLY tools route the same
                # way as BOTH — they're consolidators (ai_lane/ai_plan/
                # ai_worker) declared in the registry, gate-published,
                # stdio-pending. Routing them to self.edit() (the
                # propose/commit pipeline for ai_str_replace) would
                # blow up at impl time; they take action=... not
                # path/old/new. The check is "is this a registry-
                # backed EDIT?", not "is this BOTH?".
                from . import tool_interface as _ti

                _spec = _ti.get(name)
                if (
                    _spec is not None
                    and _spec.surface in (_ti.BOTH, _ti.GATE_ONLY)
                    and _spec.cls == _ti.EDIT
                ):
                    res = self._registry_invoke_edit(request, _spec)
                else:
                    res = self.edit(request)
                return self._to_verdict(res, user=user, eff_root=eff_root, eff_id=eff_id)
            res = self.invoke(request)
            return self._to_verdict(res, user=user, eff_root=eff_root, eff_id=eff_id)

        # ── 3. No hub — fail closed unless legacy test mode ──
        if self._legacy_fallback:
            # Legacy test path (transport protocol tests): old dispatch.
            # Scope is already verified above; run/edit/invoke also gate internally.
            if name in RUN_ALLOWLIST:
                res = self.run(request)
                return self._to_verdict(res, user=user, eff_root=eff_root, eff_id=eff_id)
            if name in EDIT_ALLOWLIST:
                res = self.edit(request)
                return self._to_verdict(res, user=user, eff_root=eff_root, eff_id=eff_id)
            res = self.invoke(request)
            return self._to_verdict(res, user=user, eff_root=eff_root, eff_id=eff_id)

        return CanonicalVerdict(
            verdict="forbidden",
            blocked_by="canonical_gate_unavailable",
            reason="hub/runtime not wired; gate cascade unavailable",
            exec_project_root=eff_root,
            exec_project_id=eff_id,
        )

    def _to_verdict(
        self,
        gr: GateResult,
        *,
        user: str = "",
        eff_root: str = "",
        eff_id: str = "",
    ) -> CanonicalVerdict:
        if gr.ok:
            return CanonicalVerdict(
                verdict="pass",
                result=gr.result,
                principal_user=user or gr.principal_user,
                exec_project_root=eff_root or gr.exec_project_root,
                exec_project_id=eff_id or gr.exec_project_id,
            )
        return CanonicalVerdict(
            verdict="forbidden",
            blocked_by=gr.blocked_by,
            reason=gr.reason,
            principal_user=user or gr.principal_user,
            exec_project_root=eff_root or gr.exec_project_root,
            exec_project_id=eff_id or gr.exec_project_id,
        )  # ── invocation (admission pipeline) ──

    def invoke(self, request: GateRequest) -> GateResult:
        gate = self._preconditions(request.principal)
        if gate is not None:
            return gate
        self._ensure_index()  # only AFTER preconditions pass
        user = manifest.resolve_remote_principal(request.principal)["user_id"]
        # Effective exec project: the SERVER-RESOLVED selected root (never CWD /
        # request.project_root / a raw path), else the configured default. Audit
        # attribution reflects the project actually executed against.
        eff_root = request.exec_root or self._exec_project_root
        eff_id = _exec_project_id(request.exec_root) if request.exec_root else self._exec_project_id

        key = (request.kind, request.tool_name)
        if key in self._ambiguous:
            return self._refuse("ambiguous_entry", f"{key} is registered more than once", user)
        entry = self._by_key.get(key)
        if entry is None:
            return self._refuse(
                "unknown_tool",
                f"no such tool: kind={request.kind} name={request.tool_name}",
                user,
            )
        # THE rule: only remote_eligible tools are reachable (refuses
        # needs_review / Tier-L / unknown / unbound M-or-A alike).
        if not entry["remote_eligible"]:
            return self._refuse(
                "not_remote_eligible",
                f"{request.tool_name} is {entry['tier']}/"
                f"{entry['enforcement_binding']} — not remote-eligible",
                user,
            )
        # No Tier-M mutation path in this pass.
        if entry["tier"] == manifest.TIER_M:
            return self._refuse("tier_m_disabled", "Tier-M mutation path not built yet", user)
        # P2 INVOCATION-ONLY for read-proven Tier-R: Tier-A is eligible (so it is
        # DISCOVERABLE) but NOT invokable until its phase ships.
        if not self._invokable_now(entry):
            return self._refuse(
                "tier_not_invokable",
                f"{request.tool_name} ({entry['tier']}) is discoverable but not "
                f"invokable in P2 (Tier-R only)",
                user,
            )
        # Covenant: a real audit sink MUST be configured when enabled.
        if self._audit_sink is None:
            return self._refuse(
                "audit_not_configured",
                "outer_gate is enabled but no audit sink is configured; refusing "
                "to invoke unaudited",
                user,
            )
        # Covenant: project binding.
        if entry["requires_project_binding"] and not request.project_root:
            return self._refuse(
                "project_binding_required",
                f"{request.tool_name} requires a bound project",
                user,
            )
        # Covenant: mandatory audit, fail-closed.
        audited = manifest.audit_or_refuse(
            lambda: self._audit_sink(
                {
                    "event": "outer_gate_invoke",
                    "tool": request.tool_name,
                    "kind": request.kind,
                    "user": user,
                    "tier": entry["tier"],
                    # auth/trust home (where tokens + signed trust live) vs the REAL
                    # execution project the read runs against — recorded separately.
                    "project_root": request.project_root,
                    "auth_home": request.project_root,
                    "exec_project_root": self._exec_project_root,
                    "exec_project_id": self._exec_project_id,
                },
            ),
        )
        if not audited["ok"]:
            return self._refuse("audit_failed", audited["reason"], user)
        try:
            result = self._executor(entry, request)
        except _ExecutorRefusal as ref:
            # A fail-closed refusal from a real executor (unindexed/stale/unknown
            # arg/binding mismatch/not-allowlisted) — surface its precise reason
            # so the durable audit records WHY, not an opaque execution_error.
            r = self._refuse(ref.blocked_by, ref.reason, user)
            return GateResult(
                ok=False,
                blocked_by=r.blocked_by,
                reason=r.reason,
                principal_user=user,
                exec_project_root=eff_root,
                exec_project_id=eff_id,
            )
        except Exception as exc:  # noqa: BLE001
            return self._refuse("execution_error", repr(exc), user)
        return GateResult(
            ok=True,
            result=result,
            audit_recorded=True,
            principal_user=user,
            exec_project_root=eff_root,
            exec_project_id=eff_id,
        )

    def _registry_invoke_edit(self, request: GateRequest, spec) -> GateResult:
        """Dispatch a registry-declared EDIT tool by calling the
        declared function directly (`spec.fn`), bound to the selected
        exec project root.

        The function body owns its own per-call logic — confirm
        checks, per-action branching (consolidators like `ai_lane`),
        and the actual delegation to the underlying impl via
        `tool_interface._delegate`. We pass through whatever it
        returns; refusal payloads carry `_error` keys the gate maps
        to JSON-RPC errors.

        The propose/commit pipeline in `outer_gate_edit.apply` is
        ai_str_replace-specific (shape: path/old_string/new_string).
        Registry entries bypass it — each impl owns its own audit +
        path protection + size limits. The gate's contribution here
        is scope (`tier_m_edit` was checked upstream), exec-root
        binding, and routing to spec.fn (which handles confirm).
        """
        gate = self._preconditions(request.principal)
        if gate is not None:
            return gate
        pr = manifest.resolve_remote_principal(request.principal)
        if not pr["ok"]:
            return self._refuse("unauthenticated", pr["reason"], "")
        user = pr["user_id"]
        eff_exec = request.exec_root or self._exec_project_root
        if not eff_exec:
            return self._refuse("edit_unconfigured", "no exec project selected", user)
        # Filter args to the interface function's accepted parameters.
        # The MCP host may send unknown args (some clients forward the
        # full tools/list schema verbatim, including registry-only
        # fields the impl doesn't accept). Passing them through would
        # raise TypeError inside spec.fn.
        import inspect
        from pathlib import Path as _P

        from . import tool_interface as _ti
        from .mcp_server_runtime_helpers import with_target_project_root

        args = dict(request.tool_input or {})
        # Registry-level two-phase confirm. For non-consolidator
        # entries with `confirm=TWO_PHASE`, the dispatcher enforces
        # the deterministic phrase BEFORE calling spec.fn. Consolidators
        # (ai_lane / ai_worker / etc.) declare `confirm=NO_CONFIRM` at
        # the registry layer because their per-action confirm logic
        # lives in the function body.
        if spec.confirm == _ti.TWO_PHASE:
            # CANONICAL CONSUMABLE CONFIRM (sealed design §3c). The old
            # deterministic-phrase check (confirm_token == build_confirm_phrase)
            # was REPLAYABLE — the same phrase re-applied indefinitely. Registry
            # EDIT routes now consume a SINGLE-USE token bound to the exact
            # (operator, project, session, tool, normalized args, intent), with
            # named fail-closed refusals. Same authority the gate edit (str-
            # replace) path uses → one consumable-confirm contract for every edit.
            import time as _time

            from . import canonical_invocation as _ci
            from .identity_store import IdentityStore

            _auth_home = _P(request.project_root) if request.project_root else _P(eff_exec)
            _store = _ci.ConfirmStore(IdentityStore().db_path(_auth_home))
            _operator = user
            _project_id = _exec_project_id(eff_exec)
            _session_id = str(
                (request.principal or {}).get("session_id")
                or (request.principal or {}).get("token_id")
                or "",
            )
            _confirm_args = {
                k: v for k, v in args.items() if k not in ("confirm_token", "edit_confirmation_id")
            }
            _norm = _ci.normalize_args(_confirm_args)
            _intent = _ti.build_confirm_phrase(spec, args)  # human-confirmed mutation intent
            _ctok = str(args.get("confirm_token") or args.get("edit_confirmation_id") or "")
            _now = _time.time()
            if not _ctok:
                _token = _store.issue(
                    operator=_operator,
                    project_id=_project_id,
                    session_id=_session_id,
                    tool=spec.name,
                    normalized_args=_norm,
                    intent=_intent,
                    now=_now,
                )
                return GateResult(
                    ok=False,
                    blocked_by="confirm_required",
                    reason=(
                        f"{spec.name}: confirmation required; re-invoke with "
                        f"confirm_token={_token!r} (single-use, {int(_ci.CONFIRM_TTL_SECONDS)}s)"
                    ),
                    result={
                        "_error": "confirm_required",
                        "action": spec.name,
                        "confirm_token": _token,
                        "summary": (
                            f"About to invoke {spec.name} with args={args!r}. "
                            f"The user must confirm before this change."
                        ),
                    },
                    principal_user=user,
                    exec_project_root=str(eff_exec),
                    exec_project_id=_exec_project_id(eff_exec),
                )
            _refusal = _store.consume(
                _ctok,
                operator=_operator,
                project_id=_project_id,
                session_id=_session_id,
                tool=spec.name,
                normalized_args=_norm,
                intent=_intent,
                now=_now,
            )
            if _refusal is not None:
                return GateResult(
                    ok=False,
                    blocked_by=_refusal.blocked_by,
                    reason=f"{spec.name}: {_refusal.reason}",
                    result={"_error": _refusal.blocked_by, "_detail": _refusal.reason},
                    principal_user=user,
                    exec_project_root=str(eff_exec),
                    exec_project_id=_exec_project_id(eff_exec),
                )
        sig = inspect.signature(spec.fn)
        accepted = set(sig.parameters)
        impl_args = {k: v for k, v in args.items() if k in accepted}
        try:
            with with_target_project_root(_P(eff_exec)):
                payload = spec.fn(**impl_args)
        except Exception as exc:  # noqa: BLE001 — fail closed on edits
            return self._refuse("edit_error", f"{spec.name}: {exc!r}", user)
        if not isinstance(payload, dict):
            payload = {"result": payload}
        # `_error` in the payload signals a refusal (confirm_required,
        # bad action, etc.) from the function body. Map to a refusal
        # GateResult so the transport surfaces it as a JSON-RPC error.
        if "_error" in payload:
            return GateResult(
                ok=False,
                blocked_by=str(payload.get("_error") or "tool_refused"),
                reason=str(payload.get("_detail") or payload.get("summary") or ""),
                result=payload,
                principal_user=user,
                exec_project_root=str(eff_exec),
                exec_project_id=_exec_project_id(eff_exec),
            )
        return GateResult(
            ok=True,
            result=payload,
            audit_recorded=True,
            principal_user=user,
            exec_project_root=str(eff_exec),
            exec_project_id=_exec_project_id(eff_exec),
        )

    def edit(self, request: GateRequest) -> GateResult:
        """Tier-M surgical-edit — SINGLE-CALL only. AIDOCS runs the full
        propose+commit law internally (diff + intent/mutation/result audit +
        binding + canonical engine + reindex) and applies the edit in one shot.
        The model never carries or resends a confirmation handle. No external
        confirm_token is accepted.
        """
        gate = self._preconditions(request.principal)
        if gate is not None:
            return gate
        pr = manifest.resolve_remote_principal(request.principal)
        if not pr["ok"]:
            return self._refuse("unauthenticated", pr["reason"], "")
        user = pr["user_id"]
        if self._project_root is None:
            return self._refuse("edit_unconfigured", "no auth home configured for edits", user)
        # Effective exec project: SERVER-RESOLVED selected root (never CWD /
        # request.project_root / a raw path), else the configured default.
        eff_exec = request.exec_root or self._exec_project_root
        if not eff_exec:
            return self._refuse("edit_unconfigured", "no exec project selected/configured", user)
        from pathlib import Path as _P

        from . import outer_gate_edit as _edit

        kw = dict(
            project_root=_P(self._project_root),
            exec_root=_P(eff_exec),
            principal=request.principal,
            tool=request.tool_name,
            args=request.tool_input,
            audit_sink=self._audit_sink,
        )
        try:
            out = _edit.apply(**kw)
        except _edit.EditRefusal as ref:
            return GateResult(
                ok=False,
                blocked_by=ref.blocked_by,
                reason=ref.reason,
                principal_user=user,
                exec_project_root=str(eff_exec),
                exec_project_id=_exec_project_id(eff_exec),
            )
        except Exception as exc:  # noqa: BLE001 — never leak a mutation error open
            return self._refuse("edit_error", repr(exc), user)
        return GateResult(
            ok=True,
            result=out,
            audit_recorded=True,
            principal_user=user,
            exec_project_root=str(eff_exec),
            exec_project_id=_exec_project_id(eff_exec),
        )

    def run(self, request: GateRequest) -> GateResult:
        """Tier-M RUN path — detached shell via the CANONICAL ai_run + its full
        law (bash_policy/heuristic_judge/freeze/destructive floor run INSIDE the
        tool). The gate adds: signed-trust precondition, project_run scope, project
        binding, and cwd bound to the SERVER-RESOLVED selected exec project (never
        CWD/request.project_root/raw path). Separate from invoke (Tier-R) and edit
        (tier_m_edit).
        """
        gate = self._preconditions(request.principal)
        if gate is not None:
            return gate
        pr = manifest.resolve_remote_principal(request.principal)
        if not pr["ok"]:
            return self._refuse("unauthenticated", pr["reason"], "")
        user = pr["user_id"]
        scope = set((request.principal or {}).get("scope") or [])
        if "project_run" not in scope:
            return self._refuse("insufficient_scope", "token lacks project_run scope", user)
        eff_exec = request.exec_root or self._exec_project_root
        if not eff_exec:
            return self._refuse("run_unconfigured", "no exec project selected/configured", user)
        runner = getattr(self._executor, "run", None)
        if not callable(runner):
            return self._refuse("run_unavailable", "this gate has no run-capable executor", user)
        if self._audit_sink is None:
            return self._refuse(
                "audit_not_configured",
                "run refused: no durable audit sink configured",
                user,
            )
        # Mandatory gate audit (run attribution) BEFORE handing to the runner; the
        # runner's own audit records command-level detail.
        eff_id = _exec_project_id(eff_exec)
        audited = manifest.audit_or_refuse(
            lambda: self._audit_sink(
                {
                    "event": "outer_gate_run",
                    "tool": request.tool_name,
                    "kind": "mcp_tool",
                    "user": user,
                    "tier": "M",
                    "auth_home": request.project_root,
                    "exec_project_root": str(eff_exec),
                    "exec_project_id": eff_id,
                    "command": (request.tool_input or {}).get("command", ""),
                },
            ),
        )
        if not audited["ok"]:
            return self._refuse("audit_failed", audited["reason"], user)
        try:
            from . import outer_gate_executor as _ex

            # Per-token isolation: host_session_id is THE control boundary (two
            # tokens ⇒ two host ids ⇒ neither can read/kill the other's runs; the
            # same token may control its own runs across sessions). session_id +
            # host_session_id are keyed to the token.
            _tid = str((request.principal or {}).get("token_id") or "anon")
            result = runner(
                request.tool_name,
                request.tool_input or {},
                exec_root=eff_exec,
                session_id="ogr_" + _tid,
                host_session_id="ogh_" + _tid,
            )
        except _ex.ExecutorRefusal as ref:
            return GateResult(
                ok=False,
                blocked_by=ref.blocked_by,
                reason=ref.reason,
                principal_user=user,
                exec_project_root=str(eff_exec),
                exec_project_id=eff_id,
            )
        except Exception as exc:  # noqa: BLE001 — never leak a run error open
            return self._refuse("run_error", repr(exc), user)
        return GateResult(
            ok=True,
            result=result,
            audit_recorded=True,
            principal_user=user,
            exec_project_root=str(eff_exec),
            exec_project_id=eff_id,
        )

    # ── helpers ──
    def _preconditions(self, principal: dict | None) -> GateResult | None:
        if not self._enabled:
            return self._refuse("gateway_disabled", "outer_gate.enabled is False", "")
        if self._bind not in _LOOPBACK:
            return self._refuse("non_loopback_bind", f"bind {self._bind!r} is not loopback", "")
        if not self._is_package_trusted():
            return self._refuse(
                "package_untrusted",
                "running code is not a verified official install (editable / "
                "drifted / unverified) — remote surface refused",
                "",
            )
        principal_ok = manifest.resolve_remote_principal(principal)
        if not principal_ok["ok"]:
            return self._refuse("unauthenticated", principal_ok["reason"], "")
        return None

    def _is_package_trusted(self) -> bool:
        if self._package_trusted is not None:
            return bool(self._package_trusted)
        try:
            from . import package_integrity as _pi

            # Pass the gate's home so the canonical (signed) trust row in the
            # runtime-trust DB is actually consulted. ONLY a recorded DB row with
            # remote_trustworthy=True AND no fingerprint/interpreter drift grants
            # remote trust (see verify_package_integrity); a signed release is
            # what set that flag at record time.
            v = _pi.verify_package_integrity(self._project_root)
            return bool(v.get("remote_trustworthy"))
        except Exception:
            return False  # fail-closed: cannot verify ⇒ not trusted

    def _refuse(self, blocked_by: str, reason: str, user: str) -> GateResult:
        return GateResult(
            ok=False,
            blocked_by=blocked_by,
            reason=reason,
            principal_user=user,
            exec_project_root=self._exec_project_root,
            exec_project_id=self._exec_project_id,
        )


def invokable_now_names(entries: list[dict] | None = None) -> set[str]:
    """The P2 reachable set: read-proven Tier-R eligible entries."""
    entries = entries if entries is not None else manifest.build_manifest()
    return {e["name"] for e in entries if e["remote_eligible"] and e["tier"] in INVOKABLE_TIERS}


def discoverable_eligible_names(entries: list[dict] | None = None) -> set[str]:
    entries = entries if entries is not None else manifest.build_manifest()
    return {e["name"] for e in entries if e["remote_eligible"]}
