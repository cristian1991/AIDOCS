"""In-place CONTRACT UPGRADE proof (operator 2026-10-01).

A contract change must never need a re-pair. The gate moves an ACTIVE
binding's ``contract_digest`` F -> T only on the APPLICATION's proof that its
owner approved exactly that move.

r0b2 ruling (#1132): the proof is CONTROL-PLANE authority, so it is an
application -> AIDOCS control assertion exactly like ``link_redeem``: typ
``aidocs-control+jwt``, verified ONLY against the binding's pinned
BINDING-CONTROL keyset (an entitlement key is refused, §5.8 purpose
separation), ``aud`` = the pinned AIDOCS origin, purpose
``contract_upgrade_proof``, lifetime <= 60 s, an EXACT claim set, and the
``(iss, kid, jti)`` consumed only after every other check passed.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Protocol

from . import jws

__all__ = [
    "CLAIMS",
    "MAX_PROOF_LIFETIME_S",
    "NONCE_RE",
    "PURPOSE_CONTRACT_UPGRADE_PROOF",
    "ContractUpgradeRefused",
    "JtiCache",
    "JtiReplayStore",
    "verify_upgrade_proof",
]

PURPOSE_CONTRACT_UPGRADE_PROOF = "contract_upgrade_proof"
#: Wire §1.4 / §8.4: ``exp - iat`` <= 60 s for a control assertion.
MAX_PROOF_LIFETIME_S = 60
CLAIMS = frozenset({
    "aud", "approved_at", "approved_by", "binding_id", "exp", "from_digest", "iat", "iss", "jti",
    "nonce", "purpose", "tenant_id", "to_digest",
})
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TEXT = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# the audit-metadata id shape, so the consumed jti can be recorded as evidence
_JTI = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


class ContractUpgradeRefused(ValueError):
    """The proof is refused; ``reason`` is a diagnostic, never model-facing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JtiReplayStore(Protocol):
    def consume(self, key: tuple[str, str, str], *, keep_until: int, now: int) -> bool: ...


class JtiCache:
    """``(iss, kid, jti)`` single use, kept until ``exp`` + skew (wire §1.4).
    In-process defence in depth: structurally a used proof cannot flip twice
    anyway (it binds ``from_digest`` + the gate's per-attempt nonce, and the
    flip moves ``from_digest``)."""

    def __init__(self) -> None:
        self._seen: dict[tuple, int] = {}
        self._lock = threading.Lock()

    def consume(self, key: tuple, *, keep_until: int, now: int) -> bool:
        with self._lock:
            for k in [k for k, until in self._seen.items() if until < now]:
                del self._seen[k]
            if key in self._seen:
                return False
            self._seen[key] = keep_until
            return True


def verify_upgrade_proof(
    proof: Any, *, binding: Any, to_digest: str, nonce: str, now: int, jtis: JtiReplayStore
) -> dict:
    """Every check, in order; returns the verified claims or raises
    :class:`ContractUpgradeRefused`. ``binding`` is the CURRENT record: its
    trust supplies the pinned binding-control keyset and AIDOCS origin, its
    digest is ``from``. The jti is consumed LAST, after every other check."""
    trust = getattr(binding, "trust", None)
    if trust is None:
        raise ContractUpgradeRefused("binding_not_paired")
    if type(nonce) is not str or not NONCE_RE.fullmatch(nonce):
        raise ContractUpgradeRefused("nonce_invalid")
    try:
        # ONLY the pinned binding-control key: an entitlement key's kid is not in this keyset.
        verified = jws.verify_compact(proof, expected_typ=jws.TYP_CONTROL, keyset=trust.binding_control_keyset)
    except jws.JwsError:
        raise ContractUpgradeRefused("proof_jose_refused") from None
    p = verified.payload
    if set(p) != CLAIMS:
        raise ContractUpgradeRefused("proof_claim_members")
    try:
        jws.check_lifetime(p, now=now, max_lifetime_s=MAX_PROOF_LIFETIME_S)
    except jws.JwsError:
        raise ContractUpgradeRefused("proof_time_bounds") from None
    if not now < p["exp"]:
        raise ContractUpgradeRefused("proof_expired")
    expected = {
        "iss": binding.pack_id,
        "aud": trust.aidocs_origin,
        "purpose": PURPOSE_CONTRACT_UPGRADE_PROOF,
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "from_digest": binding.contract_digest,
        "to_digest": to_digest,
        "nonce": nonce,
    }
    for name, want in expected.items():
        if type(p[name]) is not str or p[name] != want:
            raise ContractUpgradeRefused(f"proof_{name}_mismatch")
    if not _HEX64.fullmatch(p["from_digest"]) or not _HEX64.fullmatch(p["to_digest"]):
        raise ContractUpgradeRefused("proof_digest_shape")
    if type(p["approved_by"]) is not str or not _TEXT.fullmatch(p["approved_by"]):
        raise ContractUpgradeRefused("proof_approved_by")
    if type(p["approved_at"]) is not int or p["approved_at"] > p["iat"] + jws.MAX_CLOCK_SKEW_S:
        raise ContractUpgradeRefused("proof_approved_at")
    if type(p["jti"]) is not str or not _JTI.fullmatch(p["jti"]):
        raise ContractUpgradeRefused("proof_jti")
    keep = p["exp"] + jws.MAX_CLOCK_SKEW_S
    try:
        fresh = jtis.consume(
            (p["iss"], verified.header["kid"], p["jti"]),
            keep_until=keep,
            now=now,
        )
    except Exception:
        raise ContractUpgradeRefused("proof_jti_store_unavailable") from None
    if not fresh:
        raise ContractUpgradeRefused("proof_jti_replay")
    return dict(p)
