"""One shared destructive-SHAPE taxonomy.

Before this module, "what is destructive" was defined in several places that
had drifted apart:

  * ``heuristic_judge._check_bash_rules`` had a rich rm-shape taxonomy
    (``rm -rf /`` critical, ``rm -rf <abs>`` high, ``rm -rf ./build`` permit).
  * ``bash_policy._JUDGE_DENYLIST`` / ``evaluate_destructive_floor`` used a
    crude TOKEN floor — ANY ``rm`` denied, regardless of shape, and it MISSED
    shapes that carry no denylisted base command at all (a fork bomb ``:|:&``,
    a ``> /dev/sda`` redirect, ``mount`` / ``chroot`` host-escape).

This module is the single definition. It classifies a command's destructive
SHAPE into three tiers:

  * ``TIER_PERMIT``     — bounded / recoverable (``rm file``, ``rm -rf ./build``,
                          ``rm -rf /tmp/x``). The operator policy decides
                          whether the *command* is allowed; the shape itself
                          is not a safety concern.
  * ``TIER_CONFIRM``    — scoped-powerful (``rm -rf`` of an absolute non-tmp
                          path). Recoverable-maybe; wants operator confirm.
  * ``TIER_HARD_DENY``  — catastrophic / evasive / host-escape: never has a
                          legitimate direct form (``rm -rf /``, ``mkfs``,
                          ``dd of=/dev/sda``, fork bomb, ``mount`` / ``chroot``,
                          a raw write to a block device).

Consumers — and the DELIBERATE split (this is NOT one law everywhere):

  * The HEURISTIC JUDGE on the GATED, policy-aware surfaces (native Bash + MCP
    ai_run, which pass through the operator [bash] allow/deny tables) maps
    these tiers to its rule severities — so a bounded ``rm -rf ./build`` is
    PERMITTED where the policy allows it, an absolute-path ``rm -rf`` asks for
    confirmation, and only catastrophic shapes hard-deny. Public rule IDs and
    tests are unchanged.
  * The DESTRUCTIVE FLOOR (``evaluate_destructive_floor``) additionally
    hard-denies this module's ``TIER_HARD_DENY`` shapes, catching the
    base-command-less ones (fork bomb, ``> /dev/sda``, ``mount``/``chroot``)
    that a token list cannot see.

But the floor ALSO keeps a deliberately STRICTER, shape-BLIND TOKEN seal
(``bash_policy._JUDGE_DENYLIST``: rm/sudo/dd/mkfs/kill/chmod/…) on the
INTERNAL, UN-GATED egress (``code_runner._run_process`` behind ``git_ops``),
where NO operator allow-table is consulted — there ANY ``rm`` is refused
regardless of shape. So the shared SHAPE taxonomy governs the gated surfaces,
while the token seal is the stricter floor for the path that never sees the
policy. Keeping the two distinct is intentional, not drift.

Exfiltration, persistence, and anti-coup tampering are enforced by their own
dedicated layers (``command_read_intent`` / output guard for exfil; the
write-gate + protected-memory for anti-coup; the dangerous-chain detector for
``curl|sh`` download-then-exec) and are intentionally NOT re-implemented here —
this taxonomy owns destructive-EXECUTION shapes only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TIER_PERMIT = "permit"
TIER_CONFIRM = "confirm"
TIER_HARD_DENY = "hard_deny"


@dataclass(slots=True)
class DestructiveVerdict:
    """One destructive shape found in a command."""

    tier: str
    family: str
    rule_id: str
    reason: str
    recommendation: str = ""
    target: str = ""


# ── rm -r<x> target classification ────────────────────────────────────

# Bare root / home / cwd / wildcard — wipes the whole machine or project in
# one call; never a legitimate direct form.
_RM_ROOT_TARGETS: frozenset[str] = frozenset(
    {"/", "~", "~/", ".", "./", "*", "./*"},
)
# Pre-allowlisted scratch roots — recursive cleanup here is everyday + safe.
_SAFE_TMP_RE = re.compile(
    r"^(/tmp/|/var/tmp/|/var/cache/|c:[/\\]temp[/\\])",
    re.IGNORECASE,
)
# `rm -r` / `-rf` / `-fr` / `-Rf` … (any flag cluster containing r or R).
_RM_RECURSIVE_RE = re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(\S+)")


def classify_rm_target(target: str) -> DestructiveVerdict | None:
    """Classify the TARGET of a recursive ``rm``.

    Returns None for bounded/safe targets (single relative dir, ``/tmp/…``) —
    those are PERMIT and produce no verdict. Catastrophic root/wildcard →
    HARD_DENY; an absolute non-tmp path → CONFIRM (scoped-powerful).
    """
    if target in _RM_ROOT_TARGETS:
        return DestructiveVerdict(
            TIER_HARD_DENY,
            "rm_root",
            "BASH_RM_RF_ROOT",
            "Recursive delete targeting root, home, cwd, or wildcard.",
            "Never allow recursive deletion of /, ~, ., or *.",
            target,
        )
    if "*" in target or target.endswith("/."):
        return DestructiveVerdict(
            TIER_HARD_DENY,
            "rm_wildcard",
            "BASH_RM_RF_WILDCARD",
            "Recursive delete with wildcard target.",
            "Never rm -rf with * — specify exact paths.",
            target,
        )
    is_abs = target.startswith("/") or bool(re.match(r"^[A-Za-z]:[/\\]", target))
    if is_abs:
        if _SAFE_TMP_RE.match(target):
            return None  # scratch cleanup — permit
        return DestructiveVerdict(
            TIER_CONFIRM,
            "rm_abspath",
            "BASH_RM_RF_ABSPATH",
            "Recursive delete targeting an absolute path.",
            (
                "Verify the path is correct before deleting. If this is scratch "
                "data, prefer /tmp/ which is pre-allowlisted."
            ),
            target,
        )
    return None  # relative path (./build, node_modules) — bounded, permit


# ── catastrophic primitives that carry NO denylisted base command ─────
# These are the shapes the token floor missed: a fork bomb has base ``:``,
# a device redirect has no command at all, mount/chroot aren't deletes.
# Each is HARD_DENY — there is no legitimate agent-loop reason to emit them.
_CATASTROPHIC_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "fork_bomb",
        "DESTRUCTIVE_FORK_BOMB",
        # :(){ :|:& };:  and minor spacing variants
        r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        "Fork bomb — exhausts process table; unrecoverable without reboot.",
    ),
    (
        "device_write",
        "DESTRUCTIVE_DEVICE_WRITE",
        # > /dev/sda , >> /dev/nvme0n1 , of=/dev/sdb (raw block-device write)
        r"(?:>>?\s*|\bof=)\s*/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|hd[a-z]|mmcblk\d+|disk\d+)",
        "Raw write to a block device — destroys the filesystem/partition.",
    ),
    (
        "host_escape",
        "DESTRUCTIVE_HOST_ESCAPE",
        # chroot into / , mount/umount of a real fs, pivot_root
        r"\b(?:chroot\s+/|pivot_root\b|(?:u?mount)\s+(?:-[a-zA-Z]+\s+)*/dev/)",
        "Host-escape / filesystem remount — outside the project sandbox.",
    ),
)
_CATASTROPHIC_COMPILED: tuple[tuple[str, str, re.Pattern[str], str], ...] = tuple(
    (family, rule_id, re.compile(rx, re.IGNORECASE), reason)
    for family, rule_id, rx, reason in _CATASTROPHIC_PATTERNS
)


def classify_destructive(command: str) -> list[DestructiveVerdict]:
    """Every destructive shape in ``command``.

    The caller is responsible for masking data-only windows first
    (``shell_data_windows.mask_data_windows``) so a quoted message payload
    isn't classified as execution. Returns verdicts ordered HARD_DENY first.
    """
    out: list[DestructiveVerdict] = []
    if not command:
        return out
    for m in _RM_RECURSIVE_RE.finditer(command):
        dv = classify_rm_target(m.group(1))
        if dv is not None:
            out.append(dv)
    for family, rule_id, pat, reason in _CATASTROPHIC_COMPILED:
        if pat.search(command):
            out.append(
                DestructiveVerdict(
                    TIER_HARD_DENY,
                    family,
                    rule_id,
                    reason,
                    "No confirm path — this shape has no legitimate direct form.",
                ),
            )
    out.sort(key=lambda v: 0 if v.tier == TIER_HARD_DENY else 1)
    return out


def hard_deny_verdict(command: str) -> DestructiveVerdict | None:
    """The first HARD_DENY destructive shape, or None. Convenience for the
    floor, which only cares whether a catastrophic shape is present."""
    for v in classify_destructive(command):
        if v.tier == TIER_HARD_DENY:
            return v
    return None
