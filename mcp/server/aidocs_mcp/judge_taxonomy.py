"""Explicit rule_id → class taxonomy for the heuristic judge.

REPLACES (2026-05-26): the prior "Class-C downgrade" path, which let
the heuristic judge's broad risk-level scoring decide whether a
verdict was confirmable or hard-blocked. That scoring conflated
mechanically different categories — a `BASH_RM_RF_ABSPATH` and a
`EXFIL_DNS_TUNNEL` both surfaced as "critical" and one of them was
silently downgraded to a confirm prompt. The split below is the
explicit, code-defined contract:

  CLASS_SAFE_ADVISORY        — pass, recorded as advisory. Build/test/
                               update/script runs whose noise is real
                               but not actionable; never freezes.

  CLASS_CONFIRMABLE_DESTRUCTIVE — operator-runnable destructive ops
                               (rm path cleanup, git rollback/reset/
                               restore, chroot buildroot, service
                               stop, db drop, kill, etc.). FREEZES
                               the turn and asks for confirmation
                               ONLY when the prompt mutator detected
                               matching operator destructive intent;
                               without that intent the freeze is
                               itself the deny (so an unprompted
                               agent "rm -rf" never executes).

  CLASS_MALICIOUS_FORBIDDEN  — always hard-blocks, never confirmable.
                               Exfiltration, env/secret reads,
                               blocked/unparseable egress, inline
                               network/subprocess evasion,
                               gate/self-mod tamper, fork bombs,
                               docker host escape, auth-token exfil,
                               persistence/host-harness tamper.
                               Every hit also increments the
                               per-session security strike counter
                               (see record_security_strike).

DEFAULT POLICY:
  An unknown rule_id (one we haven't classified yet) maps to
  CLASS_CONFIRMABLE_DESTRUCTIVE. That is the safe default —
  asking is strictly tighter than passing, and the operator can
  always reclassify a rule into safe_advisory once they've audited
  it. Defaulting to forbidden would be too aggressive (it would
  hard-block fresh rules added by future PRs before anyone had a
  chance to triage them).

The mapping below is the SOLE source of truth for verdict
classification on the new path. The heuristic judge keeps its
risk-level field for telemetry / display, but downstream gating
decisions consult `classify(rule_id)`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# ── Class constants ────────────────────────────────────────────────

CLASS_SAFE_ADVISORY = "safe_advisory"
CLASS_CONFIRMABLE_DESTRUCTIVE = "confirmable_destructive"
CLASS_MALICIOUS_FORBIDDEN = "malicious_forbidden"

ALL_CLASSES: frozenset[str] = frozenset(
    {CLASS_SAFE_ADVISORY, CLASS_CONFIRMABLE_DESTRUCTIVE, CLASS_MALICIOUS_FORBIDDEN},
)


# ── Decision constants ─────────────────────────────────────────────

DECISION_ALLOW = "allow"  # advisory only — execute the call
# ASK_CONFIRM (2026-05-26 split): confirmable_destructive AND the
# operator's prompt expressed matching destructive intent. Caller
# mints a freeze with a confirm prompt; operator can re-issue the
# action with a confirm token. Distinct from BLOCK_FREEZE_NO_CONFIRM
# so the caller no longer needs to inspect a reason string to tell
# them apart.
DECISION_ASK_CONFIRM = "ask_confirm"
# BLOCK_FREEZE_NO_CONFIRM (2026-05-26 split): confirmable_destructive
# WITHOUT matching operator destructive intent. Caller MUST refuse;
# no confirm prompt is offered (a freeze fires but is itself the
# refusal — operator has to re-issue with destructive intent in the
# prompt to even reach ASK_CONFIRM). This protects against agent
# auto-execution of destructive verbs the operator never asked for.
DECISION_BLOCK_FREEZE_NO_CONFIRM = "block_freeze_no_confirm"
# Back-compat alias — many call sites still import DECISION_ASK.
# Resolves to ASK_CONFIRM (matched intent path). New code should
# branch on ASK_CONFIRM vs BLOCK_FREEZE_NO_CONFIRM explicitly.
DECISION_ASK = DECISION_ASK_CONFIRM
DECISION_BLOCK_STRIKE = "block_strike"  # hard refuse + record strike

ALL_DECISIONS: frozenset[str] = frozenset(
    {
        DECISION_ALLOW,
        DECISION_ASK_CONFIRM,
        DECISION_BLOCK_FREEZE_NO_CONFIRM,
        DECISION_BLOCK_STRIKE,
    },
)


# ── Explicit rule_id → class taxonomy ──────────────────────────────
#
# Maintenance rule: when adding a new rule_id to heuristic_judge,
# add an entry here in the SAME PR. test_taxonomy_covers_every_judge_rule
# enforces this — an unmapped rule_id fails the suite.
#
# Grouping is by intent, not by rule prefix. A future expansion is
# allowed to renumber/split entries as long as the table stays
# explicit.

_MALICIOUS_FORBIDDEN: set[str] = {
    # ── Exfiltration ────────────────────────────────────────────────
    "EXFIL_DNS_TUNNEL",
    "EXFIL_SENSITIVE_READ_TO_NET",
    "NET_DATA_EXFIL",
    "BASH_AUTH_TOKEN_EXFIL",
    # ── Env / secret reads ─────────────────────────────────────────
    "BASH_ENV_LEAK",
    "BASH_SENSITIVE_READ",
    "INLINE_SENSITIVE_READ",
    "INLINE_SECRETS_READ",
    # ── Secret material patterns landing in files ──────────────────
    "FILE_ANTHROPIC_KEY",
    "FILE_AWS_ACCESS_KEY",
    "FILE_AWS_SECRET",
    "FILE_GITHUB_FINE_PAT",
    "FILE_GITHUB_OAUTH",
    "FILE_GITHUB_PAT",
    "FILE_GOOGLE_API_KEY",
    "FILE_HARDCODED_SECRET",
    "FILE_JWT",
    "FILE_OPENAI_KEY",
    "FILE_PEM_PRIVATE_KEY",
    "FILE_SLACK_TOKEN",
    "FILE_STRIPE_KEY",
    "FILE_URI_CREDENTIALS",
    "FILE_WRITE_CRED",
    "INLINE_CRED_WRITE",
    # ── Blocked / unparseable egress ───────────────────────────────
    "EGRESS_BLOCKED_DESTINATION",
    "EGRESS_UNPARSEABLE_DESTINATION",
    # ── Inline network / subprocess evasion ────────────────────────
    "INLINE_NETWORK",
    "INLINE_SUBPROCESS",
    "INLINE_SHELL_INVOKE",
    "INLINE_EVAL",
    "INLINE_INDIRECT_EVAL",
    "INLINE_OBFUSCATED_VERB",
    "INLINE_UNICODE_HIDING",
    "OBFUSC_DECODE_EXEC",
    "OBFUSC_LONG_ENCODED_BLOB",
    "OBFUSC_STRING_ASSEMBLY",
    "BASH_EVAL_SUBSHELL",
    "BASH_PROCESS_SUB_EXEC",
    "BASH_PIPE_TO_SHELL",
    "FILE_DYNAMIC_EXEC",
    # ── Gate / self-mod tamper ─────────────────────────────────────
    "INLINE_AIDOCS_TAMPER",
    "INLINE_AIDOCS_TABLE_OP",
    "INLINE_CONFIG_TAMPER",
    "SELF_MOD_GATE_CODE",
    "BASH_MV_FROM_PROTECTED_FORBIDDEN",
    "BASH_SYMLINK_TO_PROTECTED_FORBIDDEN",
    # ── Resource / fork bombs ──────────────────────────────────────
    "BASH_FORK_BOMB",
    "INLINE_MEMORY_BOMB",
    "BASH_FIND_XARGS_UNBOUNDED",
    # ── Docker host escape ─────────────────────────────────────────
    "BASH_DOCKER_ROOT_MOUNT",
    "BASH_DOCKER_SOCK_MOUNT",
    "BASH_DOCKER_PRIV",
    "BASH_CHROOT_HOST_FORBIDDEN",
    "BASH_KUBECTL_DEBUG_NODE",
    # ── Persistence / host-harness tamper ──────────────────────────
    "INLINE_PERSISTENCE_TAMPER",
    "INLINE_HOST_HARNESS_TAMPER",
    "BASH_LD_PRELOAD",
    # ── Unrecoverable root-tree destruction ────────────────────────
    # `rm -rf /` (rooted) is not the same shape as an operator
    # cleaning a path; even with confirm intent it shouldn't go
    # through ask-state.
    "BASH_RM_RF_ROOT",
    # ── Privilege / identity escalation ────────────────────────────
    "BASH_USER_MGMT",
    "BASH_NETWORK_RECONFIG",
    "BASH_CHMOD_WORLD",
    "NET_CLOUD_MUTATION",
    # ── Cloud-destructive without recovery (2026-05-27) ────────────
    # CLOUD_DESTRUCTIVE_NO_RECOVERY fires when a CLOUD_DESTRUCTIVE_*
    # match coincides with a no-recovery flag (--skip-final-snapshot,
    # --force-delete-without-recovery, --no-pending-window,
    # --bypass-governance-retention, --force). Once stripped, the
    # operation is unrecoverable — operator confirm shouldn't unlock
    # it. Strip the flag and re-issue to reach the confirmable path.
    "CLOUD_DESTRUCTIVE_NO_RECOVERY",
}

_CONFIRMABLE_DESTRUCTIVE: set[str] = {
    # ── rm / path cleanup (NON-root) ───────────────────────────────
    "BASH_RM_RF_ABSPATH",
    "BASH_RM_RF_WILDCARD",
    "BASH_FIND_DELETE",
    "BASH_FIND_EXEC_RM",
    "BASH_WIN_DELETE",
    "INLINE_FS_DESTROY",
    # ── Git rollback / restore / reset / force ─────────────────────
    "GIT_RESET_HARD",
    "GIT_RESTORE_DISCARD",
    "GIT_CHECKOUT_OVERWRITE",
    "GIT_CLEAN",
    "GIT_FORCE_PUSH",
    "GIT_FORCE_LEASE",
    "GIT_BRANCH_DELETE",
    "GIT_PUSH_DELETE_REMOTE",
    "GIT_PUSH_MIRROR",
    "GIT_STASH_DROP",
    "GIT_FETCH_FORCE_REFSPEC",
    "INLINE_GIT_TOUCH",
    # ── Chroot buildroot (confirmable variant) ─────────────────────
    "BASH_CHROOT_BUILDROOT_CONFIRMABLE",
    # ── Service / process stop ─────────────────────────────────────
    "BASH_SERVICE_STOP",
    "BASH_KILL_PROCESS",
    "BASH_PS_REMOVE",
    # ── DB drop ────────────────────────────────────────────────────
    "BASH_DB_DROP",
    # ── Disk ops / overwrite redirects ─────────────────────────────
    "BASH_DISK_OPS",
    "BASH_CP_DEVNULL_OVERWRITE",
    "BASH_OVERWRITE_REDIRECT",
    # ── Protected-path move / symlink (confirmable variant) ────────
    "BASH_SYMLINK_TO_PROTECTED_CONFIRMABLE",
    "BASH_MV_FROM_PROTECTED_CONFIRMABLE",
    # ── VM destroy ─────────────────────────────────────────────────
    "BASH_VBOX_DESTROY",
    "BASH_VIRSH_DESTROY",
    "BASH_HYPERV_DESTROY",
    # ── Cloud-destructive WITH recovery path (2026-05-27) ──────────
    # These ops keep a default snapshot/backup; confirm UNLOCKS the
    # delete because the operator can restore from the snapshot if
    # they change their mind. When paired with a no-recovery flag
    # (--skip-final-snapshot, --force, etc.) they upgrade to
    # CLOUD_DESTRUCTIVE_NO_RECOVERY (malicious_forbidden, above).
    "CLOUD_DESTRUCTIVE_RDS",
    "CLOUD_DESTRUCTIVE_KMS",
    "CLOUD_DESTRUCTIVE_ROUTE53",
    "CLOUD_DESTRUCTIVE_DYNAMODB",
    "CLOUD_DESTRUCTIVE_ECS",
    "CLOUD_DESTRUCTIVE_EKS",
    "CLOUD_DESTRUCTIVE_S3_BUCKET",
    "CLOUD_DESTRUCTIVE_LAMBDA",
    "CLOUD_DESTRUCTIVE_IAM",
    "CLOUD_DESTRUCTIVE_GCLOUD",
    "CLOUD_DESTRUCTIVE_AZ",
    # ── Kubectl exec (shell into pod — destructive surface) ────────
    "BASH_KUBECTL_EXEC_SHELL",
    # ── Sudo / privileged invocation ───────────────────────────────
    "BASH_SUDO",
    # ── Writes to infra / CI / deps ────────────────────────────────
    "FILE_WRITE_CI",
    "FILE_WRITE_INFRA",
    "FILE_WRITE_DEPS",
    # ── Net upload (potentially destructive remote write) ──────────
    "BASH_NET_UPLOAD",
    # PATH_INPUT_CONFLICT moved to safe_advisory (2026-05-26) — it's
    # a structural / discovery mismatch (the path/input shape looks
    # off), NOT an operator-destructive verb. The orchestrator's
    # sensitive_path_blocked + path classifier own the actual deny
    # for external-sensitive paths; classifying PATH_INPUT_CONFLICT
    # as confirmable_destructive made the precheck pre-empt that
    # specific rule.
    # ── Generic shell writes to sensitive/source paths ─────────────
    # SHELL_WRITE_UNKNOWN is NOT here — it's a low-confidence
    # attribution verdict ("we don't know what this is"), not an
    # authoritative deny. It belongs in safe_advisory so the
    # orchestrator's path-classifier (sensitive_path_blocked,
    # protected-infrastructure, etc.) keeps owning the gating for
    # unknown paths. Putting SHELL_WRITE_UNKNOWN in this bucket
    # caused the precheck to short-circuit external-sensitive writes
    # with blocked_by=judge_confirmable_no_intent, pre-empting the
    # orchestrator's more specific sensitive_path_blocked rule.
    "SHELL_WRITE_SENSITIVE",
    "SHELL_WRITE_SOURCE",
    # ── #448 semantic enrichment: shell write into gate-surface code ──
    # An ADDED refusal ground from the semantic layer (Consumer A): a
    # shell redirect / sed -i / tee / truncate aimed at a file whose
    # semantic class is "gate" (security-surface source). The governed
    # edit tools own gate-code changes; a raw shell write bypasses their
    # gate stack, so it asks with intent and freezes without.
    "SEMANTIC_GATE_WRITE",
}

_SAFE_ADVISORY: set[str] = {
    # ── Build / test / update advisories ───────────────────────────
    "BASH_UNPIN_INSTALL",
    "PYTEST_SERIAL_OVERRIDE",
    # ── Lookups (NOT exfil) ────────────────────────────────────────
    "NET_DNS_LOOKUP",
    # ── Low-confidence shell-write attribution ─────────────────────
    # "we don't know what this path is" — the orchestrator's path
    # classifier (sensitive_path_blocked / protected_infrastructure /
    # read-gate) is the authoritative gate for unknown paths.
    # SHELL_WRITE_UNKNOWN here just surfaces the agent's intent for
    # audit + operator visibility.
    "SHELL_WRITE_UNKNOWN",
    # ── Discovery / structural hint mismatches ─────────────────────
    # PATH_INPUT_CONFLICT signals a path/input shape mismatch — not
    # an operator-destructive verb. The orchestrator's path
    # classifier owns the actual deny; this verdict is attribution.
    "PATH_INPUT_CONFLICT",
    # ── #448 semantic enrichment: informational class citation ─────
    # Emitted only when the cascade ALREADY produced verdicts; cites
    # the semantic class (gate/test/vendored/config/mempalace/source)
    # of touched files so decisions and audit can name what is being
    # touched. Message improvement only — never a refusal by itself.
    "SEMANTIC_CONTEXT",
}

# Sanity: no rule_id may live in two buckets. Surfaced via
# test_taxonomy_buckets_are_disjoint.
RULE_CLASS: dict[str, str] = {}
for _rid in _SAFE_ADVISORY:
    RULE_CLASS[_rid] = CLASS_SAFE_ADVISORY
for _rid in _CONFIRMABLE_DESTRUCTIVE:
    RULE_CLASS[_rid] = CLASS_CONFIRMABLE_DESTRUCTIVE
for _rid in _MALICIOUS_FORBIDDEN:
    RULE_CLASS[_rid] = CLASS_MALICIOUS_FORBIDDEN


def classify(rule_id: str) -> str:
    """Return the class for a given rule_id.

    Unknown ids default to CLASS_CONFIRMABLE_DESTRUCTIVE — the safer
    side of the line (ask rather than allow or hard-block). Add a
    new rule_id to one of the bucket sets above to override.
    """
    return RULE_CLASS.get(rule_id, CLASS_CONFIRMABLE_DESTRUCTIVE)


# ── Decision API ───────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Decision:
    decision: str  # one of ALL_DECISIONS
    triggering_rule_id: str
    triggering_class: str
    reason: str
    # When decision == DECISION_BLOCK_STRIKE we record a security
    # strike. The store integration is delegated to the caller (so
    # the taxonomy stays pure and testable); the caller passes the
    # session_id + persists via record_security_strike below.
    strike: bool = False


def evaluate_verdicts(
    verdict_rule_ids: Iterable[str],
    *,
    operator_destructive_intent: bool,
) -> Decision:
    """Combine a list of triggered rule_ids into a single decision.

    Precedence (strict — DOES NOT depend on heuristic risk levels):
      1. Any malicious_forbidden hit → DECISION_BLOCK_STRIKE.
         (No confirm. The first such hit becomes the triggering id.)
      2. Otherwise, any confirmable_destructive hit:
           - If operator_destructive_intent → DECISION_ASK.
           - Else → DECISION_ASK still freezes, but the freeze ITSELF
             is the refusal (caller decides freeze semantics). The
             explicit `operator_destructive_intent=False` case is
             surfaced in the reason so the caller can render the
             "no matching intent — refused" message.
      3. Otherwise → DECISION_ALLOW (only safe_advisory hits, or
         nothing).

    `verdict_rule_ids` may be any iterable, in any order — precedence
    is by class, not by order.
    """
    ids = list(verdict_rule_ids)
    # Pass 1: any malicious_forbidden? Block + strike.
    for rid in ids:
        if classify(rid) == CLASS_MALICIOUS_FORBIDDEN:
            return Decision(
                decision=DECISION_BLOCK_STRIKE,
                triggering_rule_id=rid,
                triggering_class=CLASS_MALICIOUS_FORBIDDEN,
                reason=(
                    f"rule {rid} is malicious_forbidden — hard refused "
                    "with no confirm, security strike recorded"
                ),
                strike=True,
            )
    # Pass 2: any confirmable_destructive? Split (2026-05-26):
    #   - matched intent → DECISION_ASK_CONFIRM (caller mints freeze
    #     with confirm prompt)
    #   - no matched intent → DECISION_BLOCK_FREEZE_NO_CONFIRM (caller
    #     refuses with NO confirm prompt; freeze fires but is itself
    #     the refusal).
    # The split makes the no-intent refusal MACHINE-ENFORCED instead
    # of inherited from the legacy cascade's reason-string inspection.
    for rid in ids:
        if classify(rid) == CLASS_CONFIRMABLE_DESTRUCTIVE:
            if operator_destructive_intent:
                return Decision(
                    decision=DECISION_ASK_CONFIRM,
                    triggering_rule_id=rid,
                    triggering_class=CLASS_CONFIRMABLE_DESTRUCTIVE,
                    reason=(
                        f"rule {rid} is confirmable_destructive — operator "
                        "destructive intent matched, asking for confirmation"
                    ),
                    strike=False,
                )
            return Decision(
                decision=DECISION_BLOCK_FREEZE_NO_CONFIRM,
                triggering_rule_id=rid,
                triggering_class=CLASS_CONFIRMABLE_DESTRUCTIVE,
                reason=(
                    f"rule {rid} is confirmable_destructive but no "
                    "matching operator destructive intent — freeze IS "
                    "the refusal (no auto-execute, no auto-ask)"
                ),
                strike=False,
            )
    # Pass 3: only safe_advisory or empty → allow.
    triggering = next((r for r in ids if classify(r) == CLASS_SAFE_ADVISORY), "")
    return Decision(
        decision=DECISION_ALLOW,
        triggering_rule_id=triggering,
        triggering_class=CLASS_SAFE_ADVISORY if triggering else "",
        reason=(
            f"only safe_advisory rule {triggering!r} fired — pass"
            if triggering
            else "no rules fired — pass"
        ),
        strike=False,
    )


# ── Security strike counter ────────────────────────────────────────
#
# A pure in-process Counter, keyed by session_id. The persistence
# integration (writing to an audit table or escalation store) is
# orthogonal — callers that want durable strikes can hook
# record_security_strike to their own store. Kept here so the
# taxonomy module is self-contained for tests.

_STRIKE_COUNTERS: dict[str, int] = {}


def record_security_strike(session_id: str) -> int:
    """Increment and return the strike count for `session_id`.

    Pure in-memory; replace with a persistent store via monkeypatch
    or a thin wrapper in the caller. Returns the post-increment
    count so the caller can render "strike N of M" messaging.
    """
    if not session_id:
        session_id = "_anonymous"
    _STRIKE_COUNTERS[session_id] = _STRIKE_COUNTERS.get(session_id, 0) + 1
    return _STRIKE_COUNTERS[session_id]


def get_strike_count(session_id: str) -> int:
    """Read the current strike count without mutating."""
    return _STRIKE_COUNTERS.get(session_id or "_anonymous", 0)


def reset_strikes(session_id: str | None = None) -> None:
    """Clear strikes — `None` clears the whole table. Test-only."""
    if session_id is None:
        _STRIKE_COUNTERS.clear()
    else:
        _STRIKE_COUNTERS.pop(session_id, None)
