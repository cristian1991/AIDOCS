"""One discovery pass over shell-like providers — including the refused ones.

#561 phase 3 / #171 bullet 4.

WHY THIS EXISTS. Three systems each hold a different notion of "an acceptable
shell", and not one of them can enumerate a candidate it rejects:

  shell_resolver          known bash roots + sentinel probe; cmd.exe forbidden.
  shell_provider_identity _PROVIDER_BASENAMES + capability-matrix identity
                          contracts (only claude_code+bash actually has one).
  governed_shell_attest   signed per-candidate offers, Authenticode, system-owned
                          auto-enrolment.

All three DISCOVER by looking for real bash only. So a machine with WSL
installed shows the operator an empty picker: AIDOCS classified WSL as
shell-like on the interception side, then dropped it silently on the offer side.
#171 recorded that the ineligible list could not be "sourced from the existing
classification" because no classification result is ever produced for it.

This module answers one question — "what shell-like things are here, and for
each, may we use it and WHY?" — so a refusal is a fact the UI can render rather
than an absence the operator has to guess at.

TWO INVARIANTS, both load-bearing:

1. PURE ENUMERATION. Candidates are classified by path and name. Nothing here
   executes a candidate; probing and attestation stay in shell_resolver and
   governed_shell_attest respectively. This function is reachable from the
   dashboard, and a picker that spawns every shell it finds is a liability.

2. AN INELIGIBLE ENTRY IS NOT AN OFFER. It carries no nonce, signature or hash.
   Otherwise "here is WSL, you cannot use it" becomes "here is WSL, click to
   enable it" — precisely what the classification forbids. Attestation material
   belongs only to candidates that are already eligible, and only in
   governed_shell_attest.

The eligibility verdicts below are NOT new law. Each restates a decision already
made elsewhere, with a pointer, so this module stays a lens and never becomes a
second source of truth.

WHAT THE PASS RETURNS (#561 phase 3, completed once phase 2 landed): per
candidate, eligibility + reason + attestation + dialect. The last two arrived
after this module did — phase 2 shipped the dialect vocabulary two days later —
and each is THREADED from its owner rather than re-derived here:

  dialect      shell_provider_dialect.dialect_for_executable — the one place
               that names a binary's grammar. The family table below is keyed
               off THAT answer, so bash/sh/dash/ash/powershell/pwsh/cmd are
               spelled once in the tree and not twice. #561's complaint is one
               fact derived in several places until the copies disagree; a
               registry with its own basename table would have been a fifth.
  attestation  a STANDING, read from the enrolment on record — never material,
               and never earned here (see invariant 1: attesting means probing
               and signature-checking, both of which spawn).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import shell_provider_dialect as _dialect

# ── families and their standing verdicts ────────────────────────────────────

FAMILY_BASH = "bash"
FAMILY_CMD = "cmd"
FAMILY_POWERSHELL = "powershell"
FAMILY_WSL = "wsl"
FAMILY_SH = "sh"
FAMILY_ZSH = "zsh"
FAMILY_UNKNOWN = "unknown"

LOCATION_SYSTEM_OWNED = "system_owned"
LOCATION_USER_WRITABLE = "user_writable"
LOCATION_PATH_LOOKUP = "path_lookup"

# ── attestation STANDING (#561 phase 3) ─────────────────────────────────────
# Phase 3's pass returns "eligibility + reason + attestation". Attestation here
# is a STANDING — a word — and never attestation MATERIAL. Invariant 2 forbids a
# row carrying a nonce/signature/hash, and invariant 1 forbids earning one:
# governed_shell_attest proves a provider by probing it and reading its
# Authenticode signature, both of which SPAWN. This pass is dashboard-reachable,
# so it reports the enrolment already on record and attests nothing itself.
ATTESTATION_ENROLLED = "enrolled"
ATTESTATION_NOT_ENROLLED = "not_enrolled"
#: Read failed, or no project to read from — NOT evidence of absence.
ATTESTATION_UNKNOWN = "unknown"
#: The family can never be a provider, so attestation has nothing to say.
ATTESTATION_NOT_APPLICABLE = "not_applicable"

KNOWN_ATTESTATIONS: frozenset[str] = frozenset(
    {
        ATTESTATION_ENROLLED,
        ATTESTATION_NOT_ENROLLED,
        ATTESTATION_UNKNOWN,
        ATTESTATION_NOT_APPLICABLE,
    },
)


@dataclass(frozen=True)
class FamilyVerdict:
    """Whether a shell FAMILY may ever be a native provider, and why."""

    family: str
    eligible: bool
    reason: str


@dataclass(frozen=True)
class ShellCandidate:
    """A shell-like thing found on this machine, with a verdict attached.

    Deliberately carries no nonce/signature/hash: an entry here is an
    OBSERVATION, never an approvable offer (see invariant 2).

    dialect
        The GRAMMAR this binary parses in, straight from phase 2's
        ``dialect_for_executable``. Naming a candidate without naming its
        grammar is the half-answer #561 phase 1 removed from the audit row —
        "we found a shell" is not the same claim as "and the law that vets
        commands for it reasons in the grammar it will actually use".

    attestation
        A STANDING from ``KNOWN_ATTESTATIONS`` — a word, never material.
        Reported from the enrolment already on record; this pass attests
        nothing itself (invariant 1).
    """

    path: str
    basename: str
    family: str
    eligible: bool
    reason: str
    location: str
    dialect: str
    attestation: str


#: Standing verdict per family. Each reason restates an existing decision and
#: names where it lives, so this table can be audited against its sources.
_FAMILY_VERDICTS: dict[str, FamilyVerdict] = {
    FAMILY_BASH: FamilyVerdict(
        FAMILY_BASH,
        True,
        "real bash — the only family with a native-execution identity contract "
        "(shell_capability_matrix: claude_code+bash = host_verified_path). Still "
        "subject to attestation before use.",
    ),
    FAMILY_CMD: FamilyVerdict(
        FAMILY_CMD,
        False,
        "cmd.exe is NEVER a provider and no flag lifts it — an absolute refusal "
        "(shell_resolver._FORBIDDEN_BASENAMES_ALWAYS).",
    ),
    FAMILY_POWERSHELL: FamilyVerdict(
        FAMILY_POWERSHELL,
        False,
        "PowerShell has no native-execution identity contract yet "
        "(shell_capability_matrix reports IDENTITY_NONE; the resolver records it "
        "as rejected with 'powershell provider not implemented'). Detected and "
        "audited, not eligible.",
    ),
    FAMILY_WSL: FamilyVerdict(
        FAMILY_WSL,
        False,
        "wsl is a launcher, not a shell (shell_provider_identity): it hands the "
        "command to another system, so the binary AIDOCS could attest is the "
        "door rather than the shell that parses the command.",
    ),
    FAMILY_SH: FamilyVerdict(
        FAMILY_SH,
        False,
        "sh does not share bash parsing semantics, and the judge / bash_policy "
        "vet commands under bash grammar (shell_provider_identity). `sh` also "
        "names a ROLE, not an implementation — it resolves to dash, bash or "
        "busybox ash depending on the system.",
    ),
    FAMILY_ZSH: FamilyVerdict(
        FAMILY_ZSH,
        False,
        "zsh does not share bash parsing semantics; the core law vets commands "
        "under bash grammar (shell_provider_identity).",
    ),
    FAMILY_UNKNOWN: FamilyVerdict(
        FAMILY_UNKNOWN,
        False,
        "unrecognised shell-like binary — fail closed; AIDOCS cannot vouch for "
        "a grammar it does not model.",
    ),
}

#: dialect -> family. Phase 2's four modelled grammars map 1:1 onto families,
#: so a binary's family is READ OFF the grammar phase 2 already assigned it
#: rather than spelled out a second time here. #561's whole complaint is one
#: fact derived in several places and the copies disagreeing; a registry that
#: kept its own basename table would be another copy of exactly that fact.
_DIALECT_FAMILY: dict[str, str] = {
    _dialect.DIALECT_BASH: FAMILY_BASH,
    _dialect.DIALECT_POSIX_SH: FAMILY_SH,
    _dialect.DIALECT_POWERSHELL: FAMILY_POWERSHELL,
    _dialect.DIALECT_CMD: FAMILY_CMD,
}

#: The COMPLEMENT: names phase 2 deliberately answers UNKNOWN for, which still
#: need a family so #171 can explain them rather than drop them silently.
#: wsl is a launcher — it has no grammar of its own to model. zsh's grammar is
#: unmodelled, and phase 2 chose UNKNOWN over a guess. Adding anything here
#: that ``dialect_for_executable`` already recognises would re-introduce the
#: second spelling this table exists to avoid (pinned by a test).
_EXTRA_BASENAME_FAMILY: dict[str, str] = {
    "wsl": FAMILY_WSL,
    "zsh": FAMILY_ZSH,
}


def classify_family(family: str) -> FamilyVerdict:
    """The standing verdict for a family — defined even when none is installed.

    #171 needed exactly this: a verdict that exists independently of discovery,
    so the UI can explain a refusal for something the box may or may not have.
    """
    return _FAMILY_VERDICTS.get((family or "").strip().lower(), _FAMILY_VERDICTS[FAMILY_UNKNOWN])


def family_for_basename(name: str) -> str:
    """The family of a binary, resolved through phase 2 wherever phase 2 knows.

    Accepts a bare name or a full path. BOTH branches normalise through
    ``dialect_for_executable``'s own helper, which is what the previous version
    only CLAIMED: it delegated the first branch and then rolled its own
    ``Path(...).name`` for the fallback. ``Path`` does not treat ``\\`` as a
    separator on POSIX, so a Windows-style path resolved correctly for the
    families phase 2 knows (bash, sh) and fell to UNKNOWN for the registry-local
    complement (wsl, zsh) — on the Linux build host only. The parity control
    caught it, which is what a parity control is for.
    """
    normalized = _dialect.normalized_executable_name(str(name or ""))
    family = _DIALECT_FAMILY.get(_dialect.dialect_for_executable(str(name or "")))
    if family is not None:
        return family
    return _EXTRA_BASENAME_FAMILY.get(normalized, FAMILY_UNKNOWN)


# ── attestation standing (read-only; never attests) ─────────────────────────


def _enrolled_provider_path(project_root: Path | None) -> tuple[bool, str]:
    """The provider path already ENROLLED for this project, as ``(readable,
    path)``.

    A pure config read. ``readable=False`` means we could not find out — kept
    distinct from "read fine, nothing enrolled" so a failure never masquerades
    as a negative answer.
    """
    if project_root is None:
        return (False, "")
    try:
        from .config import get_setting
        from .governed_bash_service import KEY_PROVIDER_PATH

        return (
            True,
            str(get_setting(KEY_PROVIDER_PATH, project_root=project_root, default="") or ""),
        )
    except Exception:  # noqa: BLE001 — a standing we cannot read is UNKNOWN
        return (False, "")


def _attestation_standing(
    path: str,
    *,
    eligible: bool,
    enrolled_path: str,
    readable: bool,
) -> str:
    """The attestation STANDING of one candidate. Never material.

    EXACT path match only, after case/separator normalisation. A prefix or
    basename match would let a neighbouring binary inherit the standing of the
    enrolled one, which is the costume problem shell_provider_identity exists
    to refuse.
    """
    if not eligible:
        return ATTESTATION_NOT_APPLICABLE
    if not readable:
        return ATTESTATION_UNKNOWN
    if not enrolled_path:
        return ATTESTATION_NOT_ENROLLED
    if os.path.normcase(str(path)) == os.path.normcase(enrolled_path):
        return ATTESTATION_ENROLLED
    return ATTESTATION_NOT_ENROLLED


def _search_names() -> tuple[str, ...]:
    """Names worth LOOKING for — deliberately wider than what we accept.

    Detection is intentionally broader than execution identity
    (shell_provider_identity): we look for the refused families precisely so the
    refusal can be shown rather than inferred from an empty list.
    """
    if os.name == "nt":
        return ("bash.exe", "wsl.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "sh.exe", "zsh.exe")
    return ("bash", "sh", "dash", "zsh", "ash")


def _known_roots() -> list[tuple[Path, str]]:
    """(root, location-label) pairs, reusing governed_shell_attest's roots."""
    out: list[tuple[Path, str]] = []
    try:
        from .governed_shell_attest import _system_owned_roots, _user_writable_roots

        out.extend((r, LOCATION_SYSTEM_OWNED) for r in _system_owned_roots())
        out.extend((r, LOCATION_USER_WRITABLE) for r in _user_writable_roots())
    except Exception:  # noqa: BLE001 — discovery degrades, never raises
        pass
    return out


def _candidate_paths_in_root(root: Path) -> list[Path]:
    try:
        from .governed_shell_attest import _bash_in_root

        return list(_bash_in_root(root))
    except Exception:  # noqa: BLE001
        return []


def discover_shell_candidates(project_root: Path | None = None) -> list[ShellCandidate]:
    """Every shell-like binary found on this machine, each with a verdict.

    Pure enumeration — classification by path and name, nothing executed.
    Deterministic and de-duplicated so a UI can render it stably. Degrades to a
    shorter list rather than raising: a discovery pass that can fail the caller
    is worse than one that reports less.

    ``project_root`` is optional and only sharpens the attestation standing:
    without it every eligible candidate reads UNKNOWN rather than guessing.
    Existing callers pass nothing and are unaffected.
    """
    found: list[ShellCandidate] = []
    seen: set[str] = set()
    # Read ONCE for the whole pass — the enrolment cannot differ per candidate,
    # and re-reading per row would invite the copies-disagree problem at the
    # smallest possible scale.
    readable, enrolled_path = _enrolled_provider_path(project_root)

    def _add(path: Path, location: str) -> None:
        try:
            if not path.is_file():
                return
        except OSError:
            return
        key = str(path).lower()
        if key in seen:
            return
        seen.add(key)
        family = family_for_basename(path.name)
        verdict = classify_family(family)
        found.append(
            ShellCandidate(
                path=str(path),
                basename=path.name,
                family=family,
                eligible=verdict.eligible,
                reason=verdict.reason,
                location=location,
                # From the RESOLVED PATH, exactly as ResolvedShell.dialect is
                # derived, so the picker and the audit row cannot disagree
                # about which grammar a given binary parses in.
                dialect=_dialect.dialect_for_executable(str(path)),
                attestation=_attestation_standing(
                    str(path),
                    eligible=verdict.eligible,
                    enrolled_path=enrolled_path,
                    readable=readable,
                ),
            )
        )

    for root, location in _known_roots():
        for path in _candidate_paths_in_root(root):
            _add(path, location)

    for name in _search_names():
        try:
            hit = shutil.which(name)
        except Exception:  # noqa: BLE001
            hit = None
        if hit:
            _add(Path(hit), LOCATION_PATH_LOOKUP)

    found.sort(key=lambda c: (not c.eligible, c.family, c.path.lower()))
    return found


def ineligible_candidates(project_root: Path | None = None) -> list[ShellCandidate]:
    """The 'detected, not eligible' set — #171 bullet 4's data, as a query.

    Carries no attestation material by construction (see ShellCandidate), so it
    can be rendered as an explanation and never mistaken for an offer.
    """
    return [c for c in discover_shell_candidates(project_root) if not c.eligible]
