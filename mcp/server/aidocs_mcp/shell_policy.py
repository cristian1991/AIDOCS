"""ShellPolicy — the single evaluation contract for shell-like surfaces.

Doctrine (Batch 1):
  * AIDOCS owns the law; native shell is transport only.
  * No shell path may bypass this policy.
  * ai_run is canonical/reference/fallback and is NOT demoted.
  * Native transport is only eligible when the capability matrix proves
    the host can hard-deny before execution; otherwise the verdict is
    ``fallback_to_ai_run`` (fail closed). Unknown host/provider also
    fails closed.

ShellPolicy does NOT execute anything and never returns command output.
It COMPOSES existing law (it does not reimplement it):

  1. transport/provider sanity            (envelope-derived)
  2. dialect detector set                 (shell_provider_dialect)  ← PS/cmd/sh
  3. read-bypass detector                 (command_read_intent)
  4. core cascade: bash_policy + judge +  (gate_tool.enforce_tool_call)
     tool_policy + freeze/confirmable          ← parity with ai_run
  5. capability + enable gate for native  (shell_capability_matrix + config)
  6. output-guard strictness class        (matrix → output_guard_mode)

The verdict separates ``law_decision`` (allow/deny/confirmable — the
provider-aware law outcome, identical for the same command across
transports) from ``decision`` (final, which may downgrade an allowed
native call to ``fallback_to_ai_run`` when native is unproven/disabled).
This is what makes "ai_run vs native Bash produce the same verdict"
testable while keeping native off by default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import shell_capability_matrix as _matrix
from . import shell_provider_dialect as _dialect
from .shell_envelope import (
    PROVIDER_UNKNOWN,
    TRANSPORT_AI_RUN,
    TRANSPORT_UNKNOWN,
    ShellCommandEnvelope,
)

# law decisions (provider-aware, transport-independent)
LAW_ALLOW = "allow"
LAW_DENY = "deny"
LAW_CONFIRMABLE = "confirmable"

# final decisions
DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_CONFIRMABLE = "confirmable"
DECISION_FALLBACK = "fallback_to_ai_run"
DECISION_CAPABILITY_UNSUPPORTED = "capability_unsupported"
DECISION_DETACH_REQUIRED = "detach_required"  # reserved for Batch 2

# output-guard strictness modes
OUTPUT_HOST_REPLACE = "host_replace"  # host redacts pre-context
OUTPUT_PRE_EXEC_STRICT = "pre_exec_strict"  # no pre-context replacement
OUTPUT_OWNED = "owned"  # ai_run owns the return value

# DOCTRINE — Batch 2 wiring guard:
# OUTPUT_PRE_EXEC_STRICT is, in Batch 1, ONLY a label. No native
# execution path is wired here. When Batch 2 wires native execution, the
# executor MUST refuse to run a host_native command whose verdict carries
# output_guard_mode == OUTPUT_PRE_EXEC_STRICT unless a concrete strict
# pre-exec rule is implemented (e.g. deny commands that write secrets to
# files the host cannot redact). Until that rule exists, OUTPUT_PRE_EXEC_
# STRICT means "do not execute natively; fall back to ai_run". This guard
# is asserted by test_output_pre_exec_strict_is_label_only_no_execution.


@dataclass(frozen=True)
class ShellVerdict:
    decision: str
    law_decision: str
    provider: str
    transport: str
    blocked_by: str = ""
    reason: str = ""
    output_guard_mode: str = ""
    findings: list[_dialect.DialectFinding] = field(default_factory=list)
    freeze_state: dict[str, Any] | None = None


# A law delegate maps an envelope to an EnforceResult-shaped object
# exposing ``.refusal`` (None → allow; dict → deny/confirmable). Default
# wires the SAME cascade ai_run uses, so bash verdicts are parity by
# construction. Injectable for hermetic tests.
LawDelegate = Callable[[ShellCommandEnvelope], Any]


def _default_law_delegate(
    *,
    hub: Any,
    runtime: Any,
) -> LawDelegate:
    def _delegate(envelope: ShellCommandEnvelope) -> Any:
        from .gate_tool import enforce_tool_call

        return enforce_tool_call(
            hub,
            envelope.project_root,
            "ai_run",
            {"command": envelope.command},
            fail_closed=True,
            include_freeze=True,
            runtime=runtime,
        )

    return _delegate


class ShellPolicy:
    def __init__(self, *, law_delegate: LawDelegate | None = None) -> None:
        self._law_delegate = law_delegate

    def evaluate(
        self,
        envelope: ShellCommandEnvelope,
        *,
        hub: Any = None,
        runtime: Any = None,
        gate_state: dict[str, Any] | None = None,
        native_enabled: bool | None = None,
    ) -> ShellVerdict:
        provider = envelope.provider
        transport = envelope.transport

        # 0. Empty/blank command is a hard validation deny for every
        #    transport, before dialect / read / core law run.
        if not envelope.command or not envelope.command.strip():
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by="validation",
                reason="empty or blank command",
            )

        # 1. Sanity: unknown transport/provider on a non-ai_run surface
        #    fails closed to ai_run fallback. ai_run is always canonical.
        if transport == TRANSPORT_UNKNOWN or (envelope.is_native and provider == PROVIDER_UNKNOWN):
            return ShellVerdict(
                decision=DECISION_FALLBACK,
                law_decision=LAW_ALLOW,  # not evaluated; not authorized either
                provider=provider,
                transport=transport,
                blocked_by="capability_unsupported",
                reason=(
                    "unknown shell transport/provider — fail closed to "
                    "ai_run fallback; AIDOCS cannot vouch for the law path"
                ),
            )

        # 2. DIALECT detectors (PowerShell / cmd / posix-sh). A critical
        #    finding is a hard deny regardless of enable flag — falling back
        #    to ai_run would just run the dangerous command another way.
        #
        #    #561 phase 2: the detector set follows the INTERPRETER, not the
        #    interception tag. `provider` folds sh/zsh/wsl into "bash" so
        #    PreToolUse catches them at all; using it to pick a detector set
        #    meant a native `sh` surface was graded entirely by bash-only law.
        #    An envelope carrying no dialect (hand-built by a pre-phase-2
        #    caller) derives one from its provider, so those callers' verdicts
        #    cannot have moved.
        dialect = envelope.dialect or _dialect.dialect_for_provider(provider)
        findings = _dialect.evaluate_dialect(dialect, envelope.command)
        disp, finding = _dialect.policy_disposition(findings)
        if disp == _dialect.DISP_DENY and finding is not None:
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by=f"provider_policy_{provider}",
                reason=f"{provider} dangerous form: {finding.description} (rule {finding.rule_id})",
                findings=findings,
            )
        if disp == _dialect.DISP_CONFIRM and finding is not None:
            return ShellVerdict(
                decision=DECISION_CONFIRMABLE,
                law_decision=LAW_CONFIRMABLE,
                provider=provider,
                transport=transport,
                blocked_by=f"provider_policy_{provider}",
                reason=f"{provider} form requires confirmation: "
                f"{finding.description} (rule {finding.rule_id})",
                findings=findings,
            )

        # 2b. Future-sight preflight — the ONE shared strict-lifecycle
        #     authority (shell_lifecycle.lifecycle_preflight) consumed
        #     IDENTICALLY by this native path and direct MCP ai_run: name-based
        #     classification + manifest X-RAY evidence + a future_sight_preflight
        #     AUDIT (emitted here too, via hub — not only on the ai_run side).
        #     With the default flag OFF it PROCEEDS (still audits the hidden
        #     graph): routine git commit/push/merge, project-local scripts,
        #     interpreters, builds, and tests are NOT frozen merely because they
        #     may fire hooks. With strict enforcement ON the same authority
        #     denies (deny-severity) or returns a confirmable that
        #     ShellEnforcement mints as exactly ONE freeze. Neither transport
        #     sees a weaker law — same severity, same x-ray, same audit.
        _lc_action = None
        _lc = None
        try:
            from .config import get_setting
            from .shell_lifecycle import (
                ACTION_CONFIRM,
                ACTION_DENY,
                lifecycle_preflight,
            )

            _lc = lifecycle_preflight(
                envelope.command,
                project_root=envelope.project_root,
                enforce=bool(
                    get_setting(
                        "tools.shell_lifecycle_preflight_enforce",
                        project_root=envelope.project_root,
                        default=False,
                    ),
                ),
                hub=hub,
                session_id=(envelope.host_session_id or envelope.managed_session_id or ""),
            )
            _lc_action = _lc.action
        except Exception as _lc_exc:
            # Config lookup / lifecycle authority / x-ray import RAISED — the
            # strict-lifecycle inspection could NOT complete. Never silently
            # continue (that would run an uninspected command as a native
            # spawn). Fail closed with a governed fallback: an explicit DENY
            # whose native rendering offers the ai_run continuation, so the
            # command can still run under full MCP governance but never as an
            # uninspected native execution.
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by="lifecycle_inspection_unavailable",
                reason=(
                    "strict-lifecycle inspection could not complete "
                    f"({type(_lc_exc).__name__}); refusing native spawn and "
                    "routing to governed ai_run."
                ),
                findings=findings,
            )
        if _lc_action == ACTION_DENY and _lc is not None:
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by=f"lifecycle_{_lc.family}",
                reason=f"hidden execution chain: {_lc.reason}",
                findings=findings,
            )
        if _lc_action == ACTION_CONFIRM and _lc is not None:
            return ShellVerdict(
                decision=DECISION_CONFIRMABLE,
                law_decision=LAW_CONFIRMABLE,
                provider=provider,
                transport=transport,
                blocked_by=f"lifecycle_{_lc.family}",
                reason=f"hidden execution chain requires confirmation: {_lc.reason}",
                findings=findings,
            )

        # 3. Read-bypass: same host-read law the Read tool enforces.
        #    Fails CLOSED — an undecidable / raising read detector denies.
        read_status, read_reason = self._read_bypass(envelope, gate_state)
        if read_status == "error":
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by="read_bypass_unavailable",
                reason=read_reason,
                findings=findings,
            )
        if read_status == "blocked":
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by="read_bypass",
                reason=read_reason,
                findings=findings,
            )

        # 4. Core cascade (bash_policy + judge + tool_policy + freeze).
        #    Identical call for every transport → bash verdict parity.
        law_decision, law_blocked_by, law_reason, freeze_state = self._core_law(
            envelope,
            hub=hub,
            runtime=runtime,
        )
        if law_decision == LAW_DENY:
            return ShellVerdict(
                decision=DECISION_DENY,
                law_decision=LAW_DENY,
                provider=provider,
                transport=transport,
                blocked_by=law_blocked_by or "core_law",
                reason=law_reason,
                findings=findings,
            )
        if law_decision == LAW_CONFIRMABLE:
            return ShellVerdict(
                decision=DECISION_CONFIRMABLE,
                law_decision=LAW_CONFIRMABLE,
                provider=provider,
                transport=transport,
                blocked_by=law_blocked_by or "confirmable",
                reason=law_reason,
                findings=findings,
                freeze_state=freeze_state,
            )

        # law_decision == LAW_ALLOW from here.
        # 5/6. Transport gating + output strictness.
        if transport == TRANSPORT_AI_RUN:
            return ShellVerdict(
                decision=DECISION_ALLOW,
                law_decision=LAW_ALLOW,
                provider=provider,
                transport=transport,
                output_guard_mode=OUTPUT_OWNED,
                findings=findings,
                reason=law_reason,
            )

        # Native (host_native / monitor): dialect + capability + enable gate.
        #
        # #561 phase 2 — a native surface whose GRAMMAR is not bash may not be
        # executed natively. Everything upstream of here (bash_policy, the
        # heuristic judge, the destructive floor) reasons in bash grammar; a
        # native `sh`/`zsh`/`wsl` surface would have been cleared by a reading
        # of the string its interpreter does not share. This is deliberately
        # placed AFTER the core law, never before: a dangerous command must
        # keep its DENY, because re-routing a denial to ai_run would just run
        # it another way. Nothing stops running — the fallback re-enters via
        # ai_run, where phase 1 pinned a resolved bash, so the command is
        # executed by the very interpreter the law modelled.
        if dialect != _dialect.DIALECT_BASH:
            return ShellVerdict(
                decision=DECISION_FALLBACK,
                law_decision=LAW_ALLOW,
                provider=provider,
                transport=transport,
                blocked_by="dialect_not_bash",
                reason=(
                    f"native surface '{envelope.tool_name}' parses in "
                    f"'{dialect}', but the law that cleared this command reasons "
                    "in bash grammar — routing to ai_run, which runs it under "
                    "the resolved bash"
                ),
                findings=findings,
            )

        if not _matrix.is_native_safe(envelope.host, provider):
            return ShellVerdict(
                decision=DECISION_FALLBACK,
                law_decision=LAW_ALLOW,
                provider=provider,
                transport=transport,
                blocked_by="capability_unsupported",
                reason=(
                    f"native {provider} on host '{envelope.host}' is not "
                    "proven safe (command-visibility / PreToolUse hard-deny) "
                    "— fail closed to ai_run"
                ),
                findings=findings,
            )

        if native_enabled is None:
            native_enabled = self._read_native_enabled(envelope.project_root)
        if not native_enabled:
            # #319: provider attestation off → the ONLY remaining native path is
            # local-solo machine-presence on the proven claude_code+bash adapter
            # (machine presence IS the authority). Corpo/remote/unknown/opencode
            # fall back to ai_run, where AIDOCS owns the spawn.
            from .governed_shell_attest import machine_presence_native_ok

            _mp_ok, _mp_reason = machine_presence_native_ok(
                envelope.host, provider, envelope.project_root,
            )
            if not _mp_ok:
                return ShellVerdict(
                    decision=DECISION_FALLBACK,
                    law_decision=LAW_ALLOW,
                    provider=provider,
                    transport=transport,
                    blocked_by="native_not_enabled",
                    reason=(
                        "native shell provider disabled "
                        "(tools.native_shell_provider_enabled=false) — "
                        "routing to ai_run"
                    ),
                    findings=findings,
                )
            # machine-presence authorized → proceed to the native allow path.

        cap = _matrix.lookup(envelope.host, provider)
        output_mode = (
            OUTPUT_HOST_REPLACE
            if cap and cap.posttooluse_output_replacement
            else OUTPUT_PRE_EXEC_STRICT
        )
        return ShellVerdict(
            decision=DECISION_ALLOW,
            law_decision=LAW_ALLOW,
            provider=provider,
            transport=transport,
            output_guard_mode=output_mode,
            findings=findings,
            reason=law_reason,
        )

    # ── law composition helpers ─────────────────────────────────────
    def _read_bypass(
        self,
        envelope: ShellCommandEnvelope,
        gate_state: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """Return (status, reason). status ∈ {"clear","blocked","error"}.

        FAILS CLOSED: if the read detector raises or returns an
        undecidable result, status is "error" so ShellPolicy denies —
        an unavailable read gate must never silently allow a command that
        could read secrets/undiscovered source.
        """
        try:
            from .command_read_intent import evaluate_command_read_policy

            # Bind the session-artifact recognizer so `tail`/`cat`/`grep` of
            # THIS session's own task/deploy output resolves on the native
            # command-read path too (parity with the Read tool + ai_run). The
            # envelope carries the authoritative host + managed session ids.
            _gs = dict(gate_state) if isinstance(gate_state, dict) else {}
            _gs.setdefault("project_root", str(envelope.project_root))
            _gs.setdefault(
                "host_session_ids",
                [s for s in (envelope.host_session_id, envelope.managed_session_id) if s],
            )
            decision = evaluate_command_read_policy(
                envelope.command,
                _gs,
            )
        except Exception as exc:
            return (
                "error",
                f"read-bypass detector unavailable ({type(exc).__name__}); failing closed",
            )
        if decision is None:
            return ("error", "read-bypass detector returned no decision; failing closed")
        if getattr(decision, "blocked", False):
            return ("blocked", getattr(decision, "reason", "") or "read gate refused")
        return ("clear", "")

    def _core_law(
        self,
        envelope: ShellCommandEnvelope,
        *,
        hub: Any,
        runtime: Any,
    ) -> tuple[str, str, str, dict[str, Any] | None]:
        delegate = self._law_delegate or _default_law_delegate(
            hub=hub,
            runtime=runtime,
        )
        result = delegate(envelope)
        refusal = getattr(result, "refusal", None)
        if not refusal:
            return LAW_ALLOW, "", "", None
        blocked_by = str(refusal.get("blocked_by") or "")
        reason = str(refusal.get("reason") or "refused")
        freeze_state = refusal.get("freeze_state")
        if freeze_state or "confirm" in blocked_by.lower():
            return LAW_CONFIRMABLE, blocked_by, reason, freeze_state
        return LAW_DENY, blocked_by, reason, None

    def _read_native_enabled(self, project_root) -> bool:
        try:
            from .config import get_setting

            return bool(
                get_setting(
                    "tools.native_shell_provider_enabled",
                    project_root=project_root,
                    default=False,
                ),
            )
        except Exception:
            return False
