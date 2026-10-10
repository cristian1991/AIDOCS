"""Durable authority stores over :class:`~.authority_db.AppAuthorityDB`.

Each class is the SQLite twin of an in-memory store and keeps that store's
public API, so the service on top is unchanged (build plan S3 / S6 / S7 / S11 /
S14; r0b2 rulings 1-5):

* :class:`SqliteBindingBackend`  -- the S7 binding rows (``BindingStore(backend=...)``);
  compare-and-swap on ``binding_version``; the one-ACTIVE-per-(tenant,
  toolspace) rule is a partial UNIQUE index, so it holds across processes.
* :class:`SqlitePairingState`    -- S7 pairing offers and reviews
  (``PairingService(state=...)``). The pairing secret and the review handle are
  stored ONLY as SHA-256 verifiers, with expiry and spent state. The rest of an
  offer is public (the offered AIDOCS public JWK, nonces, times). A confirmed
  review leaves a bounded confirmation receipt (#1134 lost-ACK), keyed by the
  handle's verifier, so a retried pair-confirm replays the same JWS.
* :class:`SqlitePlatformReplayStore` -- S18 platform-control JTI verifiers;
  consume-once and restart-safe, with expired rows purged on admission.
* :class:`SqliteSeatPinStore`    -- S6 pins (keyed digest only; UNIQUE both ways).
* :class:`SqliteFreezeStore`     -- S11 AppScope freezes.
* :class:`SqlitePendingStore`    -- S14 pending mutation records. ``create``
  COMMITS before it returns, so the record is durable before any egress
  (write-ahead); transitions are compare-and-swap on the state.
* :class:`SqliteOrgKeyDirectory` -- the S3 org request-signing key DIRECTORY
  (``OrgRequestKeyset`` API): kid, purpose, public JWK, RFC 7638 thumbprint,
  state (next / current / retiring / retired / revoked), epochs and rotation
  metadata. There is NO private-key column: custody is CodeNexus's
  (:mod:`.vault_key_source`).

Every write runs in :meth:`AppAuthorityDB.transaction` and therefore joins a
service's ambient transaction, which is what puts a transition and its
DECISION row in ONE commit.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import asdict, fields
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric import ec

from . import jws
from .app_freeze import FreezeRecord, FreezeTarget
from .authority_db import AppAuthorityDB, require_same_authority
from .binding_store import BindingRecord, BindingRefused, BindingState, BindingTrust
from .pending import PendingRecord, PendingRefused, PendingState, next_pending_record
from .scope import AppScope
from .seat_pin import ClaimOutcome, SeatPin, mismatch_outcome
from .signer import (
    KEY_CURRENT,
    KEY_NEXT,
    KEY_RETIRED,
    KEY_RETIRING,
    KEY_REVOKED,
    PURPOSE_ORG_REQUEST,
    RETIRE_GRACE_S,
    CurrentSigner,
    KeysetError,
    NextSigner,
    _keyset_kid,
)

__all__ = [
    "SqliteBindingBackend",
    "SqliteFreezeStore",
    "SqliteLinkStore",
    "SqliteOrgKeyDirectory",
    "SqlitePairingState",
    "SqlitePlatformReplayStore",
    "SqlitePendingStore",
    "SqliteSeatPinStore",
    "binding_from_dict",
    "binding_to_dict",
    "trust_from_dict",
    "trust_to_dict",
]


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


class _Store:
    """Common: the owning DB, its transaction, and the same-DB sink check."""

    def __init__(self, db: AppAuthorityDB) -> None:
        if not isinstance(db, AppAuthorityDB):
            raise TypeError("a durable authority store needs an AppAuthorityDB")
        self.db = db

    def transaction(self) -> contextlib.AbstractContextManager:
        return self.db.transaction()

    def check_sink(self, sink: Any) -> None:
        require_same_authority(self.db, sink)


# -- S7 bindings ------------------------------------------------------------------------------------------


def trust_to_dict(trust: BindingTrust) -> dict:
    out = _trust_core(trust)
    # #1134 seal: present only once sealed, so every pre-seal row keeps its exact bytes.
    if trust.binding_generation is not None:
        out["binding_generation"] = trust.binding_generation
        out["authority_sha256"] = trust.authority_sha256
    return out


def _trust_core(trust: BindingTrust) -> dict:
    return {
        "aidocs_issuer": trust.aidocs_issuer,
        "aidocs_origin": trust.aidocs_origin,
        "aidocs_kid": trust.aidocs_kid,
        "aidocs_thumbprint": trust.aidocs_thumbprint,
        "link_return_uri": trust.link_return_uri,
        "binding_control_key": dict(trust.binding_control_key),
        "entitlement_keys": [dict(k) for k in trust.entitlement_keys],
        "entitlement_keyset_version": trust.entitlement_keyset_version,
        "entitlement_revocation_epoch": trust.entitlement_revocation_epoch,
        "transcript_sha256": trust.transcript_sha256,
    }


def trust_from_dict(d: Mapping[str, Any]) -> BindingTrust:
    return BindingTrust(**{**d, "entitlement_keys": tuple(dict(k) for k in d["entitlement_keys"])})


def binding_to_dict(rec: BindingRecord) -> dict:
    return {
        "binding_id": rec.binding_id,
        "tenant_id": rec.tenant_id,
        "toolspace_id": rec.toolspace_id,
        "pack_id": rec.pack_id,
        "contract_digest": rec.contract_digest,
        "api_version": rec.api_version,
        "namespace": rec.namespace,
        "tool_names": sorted(rec.tool_names),
        "application_origin": rec.application_origin,
        "audience": rec.audience,
        "state": rec.state.value,
        "binding_version": rec.binding_version,
        "trust": trust_to_dict(rec.trust) if rec.trust is not None else None,
        "provisioned_aidocs_kids": sorted(rec.provisioned_aidocs_kids),
    }


def binding_from_dict(d: Mapping[str, Any]) -> BindingRecord:
    return BindingRecord(
        **{
            **d,
            "tool_names": frozenset(d["tool_names"]),
            "state": BindingState(d["state"]),
            "trust": trust_from_dict(d["trust"]) if d.get("trust") is not None else None,
            "provisioned_aidocs_kids": frozenset(d["provisioned_aidocs_kids"]),
        }
    )


class SqliteBindingBackend(_Store):
    """Durable rows for :class:`~.binding_store.BindingStore` (``backend=``)."""

    def get(self, binding_id: str) -> Optional[BindingRecord]:
        with self.db.reading() as conn:
            row = conn.execute("SELECT record_json FROM bindings WHERE binding_id = ?", (binding_id,)).fetchone()
        return binding_from_dict(json.loads(row[0])) if row else None

    def all(self) -> tuple[BindingRecord, ...]:
        with self.db.reading() as conn:
            rows = conn.execute("SELECT record_json FROM bindings ORDER BY rowid").fetchall()
        return tuple(binding_from_dict(json.loads(r[0])) for r in rows)

    def insert(self, rec: BindingRecord) -> None:
        with self.db.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO bindings (binding_id, tenant_id, toolspace_id, state, binding_version, record_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (rec.binding_id, rec.tenant_id, rec.toolspace_id, rec.state.value, rec.binding_version,
                     _dumps(binding_to_dict(rec))),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingRefused("binding_id_collision") from exc

    def replace(self, new: BindingRecord, *, expected_version: int) -> None:
        """Compare-and-swap on ``binding_version``; the file enforces one ACTIVE."""
        with self.db.transaction() as conn:
            try:
                cur = conn.execute(
                    "UPDATE bindings SET state = ?, binding_version = ?, record_json = ?"
                    " WHERE binding_id = ? AND binding_version = ?",
                    (new.state.value, new.binding_version, _dumps(binding_to_dict(new)), new.binding_id,
                     expected_version),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingRefused("active_binding_exists") from exc
            if cur.rowcount != 1:
                raise BindingRefused("version_conflict")


# -- S7 pairing state (verifiers only) ----------------------------------------------------------------------


class SqlitePairingState(_Store):
    """Durable offers / reviews for :class:`~.pairing.PairingService` (``state=``)."""

    def put_offer(self, secret_sha256: str, offer: Any) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE pairing_offers SET spent = 1 WHERE binding_id = ?", (offer.binding_id,))
            conn.execute(
                "INSERT INTO pairing_offers (secret_sha256, tenant_id, binding_id, expires_at, spent, offer_json)"
                " VALUES (?, ?, ?, ?, 0, ?)",
                (secret_sha256, offer.tenant_id, offer.binding_id, offer.expires_at,
                 _dumps({k: v for k, v in asdict(offer).items() if k != "spent"})),
            )

    def _offer(self, conn: sqlite3.Connection, secret_sha256: str) -> tuple[Any, bool]:
        from .pairing import _Offer

        row = conn.execute(
            "SELECT offer_json, spent FROM pairing_offers WHERE secret_sha256 = ?", (secret_sha256,)
        ).fetchone()
        if row is None:
            return None, False
        return _Offer(**json.loads(row[0]), spent=bool(row[1])), bool(row[1])

    def spend_offer(self, secret_sha256: str) -> tuple[Any, bool]:
        """ATOMIC: the offer for this verifier and whether it was ALREADY spent;
        it is spent now either way (RFC §5.8: spent by the first attempt)."""
        with self.db.transaction() as conn:
            offer, was_spent = self._offer(conn, secret_sha256)
            if offer is not None:
                conn.execute("UPDATE pairing_offers SET spent = 1 WHERE secret_sha256 = ?", (secret_sha256,))
                offer.spent = True
        return offer, was_spent

    def put_review(self, review_id: str, pending: Any, secret_sha256: str) -> None:
        review = {k: v for k, v in asdict(pending.review).items() if k != "review_id"}
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO pairing_reviews (review_sha256, secret_sha256, tenant_id, expires_at, trust_json,"
                " review_json) VALUES (?, ?, ?, ?, ?, ?)",
                (_sha256(review_id), secret_sha256, pending.offer.tenant_id, pending.offer.expires_at,
                 _dumps(trust_to_dict(pending.trust)), _dumps(review)),
            )

    def claim_review(self, review_id: str, *, now: int, lease_s: int, claim_token: str) -> tuple[Any, str]:
        """#1134 lost-ACK: CLAIM the review for one confirm (committed at once).

        ``("claimed", pending)`` -- this call holds the claim (``claim_token``),
        either fresh or re-claimed because the previous claim's lease lapsed (a
        crashed confirm). ``("busy", pending)`` -- another confirm holds a live
        claim. ``("none", None)`` -- no such review. The review row is deleted
        only by :meth:`finish_review` in the confirm's final transaction.
        """
        from .pairing import PairingReview, _PendingReview

        if type(review_id) is not str or not review_id.isascii() or type(now) is not int:
            return "none", None
        with self.db.transaction() as conn:
            key = _sha256(review_id)
            row = conn.execute(
                "SELECT secret_sha256, trust_json, review_json, claim_token, claimed_at FROM pairing_reviews"
                " WHERE review_sha256 = ?", (key,)
            ).fetchone()
            if row is None:
                return "none", None
            offer, _spent = self._offer(conn, row[0])
            busy = row[3] is not None and row[4] is not None and now <= row[4] + lease_s
            if not busy:
                conn.execute(
                    "UPDATE pairing_reviews SET claim_token = ?, claimed_at = ? WHERE review_sha256 = ?",
                    (claim_token, now, key),
                )
        if offer is None:
            return "none", None
        review = json.loads(row[2])
        review["entitlement_fingerprints"] = tuple(review["entitlement_fingerprints"])
        pending = _PendingReview(offer, trust_from_dict(json.loads(row[1])),
                                 PairingReview(review_id=review_id, **review), row[0])
        return ("busy" if busy else "claimed"), pending

    def finish_review(self, review_id: str, claim_token: str) -> bool:
        """Delete the review IF this claim still holds it (joins the ambient transaction)."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM pairing_reviews WHERE review_sha256 = ? AND claim_token = ?",
                (_sha256(review_id), claim_token),
            )
        return cur.rowcount == 1

    def mark_outcome(self, secret_sha256: str, outcome: str) -> None:
        """Record the ceremony's terminal outcome ONCE (closed CHECK; never overwritten).

        Joins the caller's ambient transaction, so ``confirmed`` commits with the
        trust install and its audit row or not at all.
        """
        from .pairing import OUTCOME_CONFIRMED, OUTCOME_FAILED

        if outcome not in (OUTCOME_CONFIRMED, OUTCOME_FAILED):
            raise ValueError("unknown ceremony outcome")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE pairing_offers SET outcome = ? WHERE secret_sha256 = ? AND outcome IS NULL",
                (outcome, secret_sha256),
            )

    def ceremony(self, binding_id: str) -> Any:
        """READ-ONLY (#1134 pair-status): the binding's latest offer, its pending review, its outcome.

        The review handle itself is never stored; only its verifier is returned.
        """
        from .pairing import _Ceremony

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT secret_sha256, outcome FROM pairing_offers WHERE binding_id = ? ORDER BY rowid DESC LIMIT 1",
                (binding_id,),
            ).fetchone()
            if row is None:
                return None
            offer, _spent = self._offer(conn, row[0])
            review = conn.execute(
                "SELECT review_sha256, review_json FROM pairing_reviews WHERE secret_sha256 = ?", (row[0],)
            ).fetchone()
        if review is None:
            return _Ceremony(offer, row[0], outcome=row[1])
        return _Ceremony(offer, row[0], review[0], json.loads(review[1]), row[1])

    def put_receipt(self, review_id: str, receipt: Any) -> None:
        """#1134 lost-ACK: store the confirmation receipt of ``review_id`` (verifier only).

        Joins the caller's ambient transaction: it commits with the trust
        install, the ``confirmed`` outcome and the confirmation row, or not at
        all. A second receipt for one review is an integrity error (fail closed).
        """
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO pairing_confirm_receipts (review_sha256, tenant_id, binding_id, confirmation,"
                " transcript_sha256, contract_digest, created_at, keep_until) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_sha256(review_id), receipt.tenant_id, receipt.binding_id, receipt.confirmation,
                 receipt.transcript_sha256, receipt.contract_digest, receipt.created_at, receipt.keep_until),
            )

    def receipt(self, review_id: str, now: int) -> Any:
        """READ-ONLY: the live receipt of exactly this review, or ``None``."""
        from .pairing import _ConfirmReceipt

        if type(review_id) is not str or not review_id.isascii() or type(now) is not int:
            return None
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT tenant_id, binding_id, confirmation, transcript_sha256, contract_digest, created_at,"
                " keep_until FROM pairing_confirm_receipts"
                " WHERE review_sha256 = ? AND keep_until >= ?",
                (_sha256(review_id), now),
            ).fetchone()
        return _ConfirmReceipt(*row) if row else None

    def purge_expired(self, now: int, *, grace_s: int = 86_400) -> int:
        """Drop offers / reviews whose window closed more than ``grace_s`` ago,
        and confirmation receipts past their own ``keep_until`` (never earlier)."""
        with self.db.transaction() as conn:
            n = conn.execute("DELETE FROM pairing_reviews WHERE expires_at < ?", (now - grace_s,)).rowcount
            n += conn.execute("DELETE FROM pairing_offers WHERE expires_at < ?", (now - grace_s,)).rowcount
            n += conn.execute("DELETE FROM pairing_confirm_receipts WHERE keep_until < ?", (now,)).rowcount
        return int(n)


# -- S18 platform-control replay -----------------------------------------------------------------------------


class SqlitePlatformReplayStore(_Store):
    """Durable consume-once JTI verifiers for CodeNexus platform control.

    Contract (r0b2): True = recorded fresh; False = DUPLICATE only. Invalid
    input or any storage fault RAISES, so it can never read as a replay.
    """

    def consume(self, jti: str, *, keep_until: int, now: int) -> bool:
        if type(jti) is not str or not jti.isascii() or not jti:
            raise ValueError("jti is not a valid verifier key")
        if type(keep_until) is not int or type(now) is not int or keep_until < now:
            raise ValueError("replay window is invalid")
        digest = _sha256(jti)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM platform_control_replay WHERE expires_at < ?", (now,))
            try:
                conn.execute(
                    "INSERT INTO platform_control_replay (jti_sha256, expires_at) VALUES (?, ?)",
                    (digest, keep_until),
                )
            except sqlite3.IntegrityError:
                return False
        return True


# -- S6 seat pins ---------------------------------------------------------------------------------------------


class SqliteSeatPinStore(_Store):
    """Durable :class:`~.seat_pin.SeatPinStore`: keyed digest only, one-to-one both ways."""

    @staticmethod
    def _pin(row: Any) -> Optional[SeatPin]:
        return SeatPin(row[0], row[1], row[2], int(row[3])) if row else None

    def claim(self, pin: SeatPin) -> ClaimOutcome:
        if type(pin) is not SeatPin:
            raise TypeError("claim takes a SeatPin")
        with self.db.transaction() as conn:
            by_seat = self._pin(conn.execute(
                "SELECT tenant_id, sub, digest, pinned_at FROM seat_pins WHERE tenant_id = ? AND sub = ?",
                (pin.tenant_id, pin.sub),
            ).fetchone())
            by_host = conn.execute(
                "SELECT sub FROM seat_pins WHERE tenant_id = ? AND digest = ?", (pin.tenant_id, pin.digest)
            ).fetchone()
            if by_seat is None and by_host is None:
                conn.execute(
                    "INSERT INTO seat_pins (tenant_id, sub, digest, pinned_at) VALUES (?, ?, ?, ?)",
                    (pin.tenant_id, pin.sub, pin.digest, pin.pinned_at),
                )
                return ClaimOutcome.PINNED
            if by_seat is not None and by_seat.digest == pin.digest and by_host is not None and by_host[0] == pin.sub:
                return ClaimOutcome.MATCHED
            # The refusal's side, from this same transaction's reads (no later re-read).
            return mismatch_outcome(
                seat_held_by_other_subject=by_seat is not None and by_seat.digest != pin.digest,
                subject_held_by_other_seat=by_host is not None and by_host[0] != pin.sub,
            )

    def get_by_seat(self, tenant_id: str, sub: str) -> Optional[SeatPin]:
        with self.db.reading() as conn:
            return self._pin(conn.execute(
                "SELECT tenant_id, sub, digest, pinned_at FROM seat_pins WHERE tenant_id = ? AND sub = ?",
                (tenant_id, sub),
            ).fetchone())

    def pins(self) -> list[SeatPin]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT tenant_id, sub, digest, pinned_at FROM seat_pins ORDER BY tenant_id, sub"
            ).fetchall()
        return [SeatPin(r[0], r[1], r[2], int(r[3])) for r in rows]

    def reset(self, tenant_id: str, sub: str, digest: str) -> Optional[SeatPin]:
        """Remove the seat's pin iff it still carries ``digest`` (CAS)."""
        with self.db.transaction() as conn:
            pin = self._pin(conn.execute(
                "SELECT tenant_id, sub, digest, pinned_at FROM seat_pins WHERE tenant_id = ? AND sub = ?",
                (tenant_id, sub),
            ).fetchone())
            if pin is None or pin.digest != digest:
                return None
            conn.execute("DELETE FROM seat_pins WHERE tenant_id = ? AND sub = ? AND digest = ?", (tenant_id, sub, digest))
            return pin


# -- S11 freezes ---------------------------------------------------------------------------------------------


class SqliteFreezeStore(_Store):
    """Durable :class:`~.app_freeze.FreezeStore` keyed by :attr:`FreezeTarget.key`."""

    @staticmethod
    def _key(target: FreezeTarget) -> str:
        if type(target) is not FreezeTarget:
            raise TypeError("a freeze is keyed by a FreezeTarget")
        return _dumps(list(target.key))

    @staticmethod
    def _record(row: Any) -> Optional[FreezeRecord]:
        if row is None:
            return None
        target = FreezeTarget(tenant_id=row[0], toolspace_id=row[1], sub=row[2])
        return FreezeRecord(target=target, frozen_at=int(row[3]), reason=row[4], binding_id=row[5], connector_id=row[6])

    def get(self, target: FreezeTarget) -> Optional[FreezeRecord]:
        with self.db.reading() as conn:
            return self._record(conn.execute(
                "SELECT tenant_id, toolspace_id, sub, frozen_at, reason, binding_id, connector_id FROM freezes"
                " WHERE target_key = ?",
                (self._key(target),),
            ).fetchone())

    def put(self, record: FreezeRecord) -> None:
        if type(record) is not FreezeRecord:
            raise TypeError("put takes a FreezeRecord")
        t = record.target
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO freezes (target_key, tenant_id, toolspace_id, sub, frozen_at, reason,"
                " binding_id, connector_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._key(t), t.tenant_id, t.toolspace_id, t.sub, record.frozen_at, record.reason,
                 record.binding_id, record.connector_id),
            )

    def delete(self, target: FreezeTarget) -> Optional[FreezeRecord]:
        with self.db.transaction() as conn:
            key = self._key(target)
            rec = self._record(conn.execute(
                "SELECT tenant_id, toolspace_id, sub, frozen_at, reason, binding_id, connector_id FROM freezes"
                " WHERE target_key = ?",
                (key,),
            ).fetchone())
            conn.execute("DELETE FROM freezes WHERE target_key = ?", (key,))
            return rec


# -- S14 pending mutation records ----------------------------------------------------------------------------

_PENDING_JSON_FIELDS = tuple(f.name for f in fields(PendingRecord) if f.name not in ("scope", "body"))


class SqlitePendingStore(_Store):
    """Durable :class:`~.pending.PendingStore` (also the S9 ``PendingRecordOracle``)."""

    @staticmethod
    def _to_json(rec: PendingRecord) -> str:
        d = {name: getattr(rec, name) for name in _PENDING_JSON_FIELDS}
        d["state"] = rec.state.value
        d["capabilities_used"] = list(rec.capabilities_used)
        return _dumps(d)

    @staticmethod
    def _from_row(row: Any) -> PendingRecord:
        d = json.loads(row[3])
        d["state"] = PendingState(d["state"])
        d["capabilities_used"] = tuple(d["capabilities_used"])
        return PendingRecord(scope=AppScope(row[0], row[1], row[2]), body=bytes(row[4]), **d)

    def create(self, record: PendingRecord) -> None:
        """Committed before return: the write-ahead record every egress needs."""
        if not isinstance(record, PendingRecord):
            raise PendingRefused("record_invalid", "not a PendingRecord")
        if record.state is not PendingState.IN_FLIGHT:
            raise PendingRefused("record_invalid", "a record opens in_flight")
        s = record.scope
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM pending_mutations WHERE mutation_ref = ?", (record.mutation_ref,)).fetchone():
                raise PendingRefused("mutation_ref_collision")
            if conn.execute(
                "SELECT 1 FROM pending_mutations WHERE idempotency_key = ?", (record.idempotency_key,)
            ).fetchone():
                raise PendingRefused("idempotency_key_collision")
            conn.execute(
                "INSERT INTO pending_mutations (mutation_ref, idempotency_key, tenant_id, sub, toolspace_id, state,"
                " state_since, record_json, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.mutation_ref, record.idempotency_key, s.tenant_id, s.sub, s.toolspace_id,
                 record.state.value, record.state_since, self._to_json(record), record.body),
            )

    def _get(self, conn: sqlite3.Connection, scope: AppScope, mutation_ref: str) -> Optional[PendingRecord]:
        row = conn.execute(
            "SELECT tenant_id, sub, toolspace_id, record_json, body FROM pending_mutations"
            " WHERE mutation_ref = ? AND tenant_id = ? AND sub = ? AND toolspace_id = ?",
            (mutation_ref, scope.tenant_id, scope.sub, scope.toolspace_id),
        ).fetchone()
        return self._from_row(row) if row else None

    def get(self, scope: AppScope, mutation_ref: str) -> Optional[PendingRecord]:
        """Only a record under EXACTLY this AppScope; anything else is None."""
        if type(scope) is not AppScope or type(mutation_ref) is not str:
            return None
        with self.db.reading() as conn:
            return self._get(conn, scope, mutation_ref)

    def transition(
        self,
        scope: AppScope,
        mutation_ref: str,
        *,
        expected: PendingState,
        to: PendingState,
        now: int,
        receipt: Optional[Mapping[str, Any]] = None,
        failure_code: Optional[str] = None,
        application_error: Optional[Mapping[str, Any]] = None,
    ) -> PendingRecord:
        with self.db.transaction() as conn:
            cur = self._get(conn, scope, mutation_ref) if type(scope) is AppScope and type(mutation_ref) is str else None
            new = next_pending_record(cur, expected=expected, to=to, now=now, receipt=receipt,
                                      failure_code=failure_code, application_error=application_error)
            done = conn.execute(
                "UPDATE pending_mutations SET state = ?, state_since = ?, record_json = ?"
                " WHERE mutation_ref = ? AND state = ?",
                (new.state.value, new.state_since, self._to_json(new), mutation_ref, expected.value),
            )
            if done.rowcount != 1:  # pragma: no cover -- serialized by BEGIN IMMEDIATE
                raise PendingRefused("state_conflict")
            return new

    def resolve(self, scope: AppScope, mutation_ref: str):
        rec = self.get(scope, mutation_ref)
        return rec.view() if rec is not None else None


# -- S8 human links ------------------------------------------------------------------------------------------


class SqliteLinkStore(_Store):
    """Durable :class:`~.link.LinkStore`: AIDOCS's ``(tenant_id, binding_id, sub)`` link rows.

    ``record_link`` joins the ambient transaction, so the ``app_identity_linked``
    DECISION row written through this file's sink commits with it.
    """

    def record_link(self, link: Any) -> None:
        from .link import LinkRecord

        if type(link) is not LinkRecord:
            raise TypeError("record_link takes a LinkRecord")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_links (tenant_id, binding_id, sub, linked_at) VALUES (?, ?, ?, ?)",
                (link.tenant_id, link.binding_id, link.sub, int(link.linked_at)),
            )

    def get(self, tenant_id: str, binding_id: str, sub: str) -> Any:
        from .link import LinkRecord

        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT tenant_id, binding_id, sub, linked_at FROM app_links"
                " WHERE tenant_id = ? AND binding_id = ? AND sub = ?",
                (tenant_id, binding_id, sub),
            ).fetchone()
        return LinkRecord(row[0], row[1], row[2], int(row[3])) if row else None

    def records(self) -> tuple:
        from .link import LinkRecord

        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT tenant_id, binding_id, sub, linked_at FROM app_links ORDER BY rowid"
            ).fetchall()
        return tuple(LinkRecord(r[0], r[1], r[2], int(r[3])) for r in rows)


# -- S3 the org request-signing key DIRECTORY (public material only) ----------------------------------------


class SqliteOrgKeyDirectory(_Store):
    """Durable twin of :class:`~.signer.OrgRequestKeyset` (the ``SignerDirectory``).

    Holds kid, purpose, public JWK, thumbprint, state, epochs and rotation
    metadata. It never sees a private key; the kid is always AIDOCS's choice.
    """

    def __init__(self, db: AppAuthorityDB, *, purpose: str = PURPOSE_ORG_REQUEST) -> None:
        super().__init__(db)
        self.purpose = purpose

    def __repr__(self) -> str:
        return "<SqliteOrgKeyDirectory>"

    # -- helpers ----------------------------------------------------------------------

    @staticmethod
    def _org(org_id: Any) -> str:
        if type(org_id) is not str or not org_id:
            raise KeysetError("org_id must be a non-empty string")
        return org_id

    @staticmethod
    def _kid(kid: Any) -> str:
        return _keyset_kid(kid)

    @staticmethod
    def _public(key: Any, kid: str) -> tuple[str, str]:
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise KeysetError("keyset material must be a P-256 public key")
        jwk = jws.public_jwk(key, kid)
        return _dumps(jwk), jws.jwk_thumbprint(jwk)

    @staticmethod
    def _now(now: Any) -> int:
        if type(now) is not int:
            raise KeysetError("now must be integer seconds since the Unix epoch")
        return now

    def _rows(self, conn: sqlite3.Connection, org_id: str) -> list[tuple]:
        return conn.execute(
            "SELECT kid, public_jwk, state, retire_after FROM signing_keys WHERE tenant_id = ? AND purpose = ?"
            " ORDER BY added_epoch, kid",
            (org_id, self.purpose),
        ).fetchall()

    def _bump(self, conn: sqlite3.Connection, org_id: str) -> int:
        row = conn.execute(
            "SELECT keyset_epoch FROM signing_keysets WHERE tenant_id = ? AND purpose = ?", (org_id, self.purpose)
        ).fetchone()
        epoch = (int(row[0]) + 1) if row else 1
        conn.execute(
            "INSERT OR REPLACE INTO signing_keysets (tenant_id, purpose, keyset_epoch) VALUES (?, ?, ?)",
            (org_id, self.purpose, epoch),
        )
        return epoch

    def _effective(self, org_id: str, kid: str, state: str, retire_after: Any, now: int) -> str:
        if state == KEY_RETIRING and retire_after is not None and now > int(retire_after):
            # Monotonic: once past its grace the old kid is retired for good (persisted).
            with self.db.transaction() as wconn:
                wconn.execute(
                    "UPDATE signing_keys SET state = ?, retire_after = NULL WHERE tenant_id = ? AND purpose = ?"
                    " AND kid = ? AND state = ?",
                    (KEY_RETIRED, org_id, self.purpose, kid, KEY_RETIRING),
                )
            return KEY_RETIRED
        return state

    # -- transitions ----------------------------------------------------------------------

    def bootstrap(self, org_id: str, kid: str, public_key: ec.EllipticCurvePublicKey) -> None:
        org_id, kid = self._org(org_id), self._kid(kid)
        jwk, thumb = self._public(public_key, kid)
        with self.db.transaction() as conn:
            if self._rows(conn, org_id):
                raise KeysetError("org already has a request-signing keyset; rotate instead")
            epoch = self._bump(conn, org_id)
            conn.execute(
                "INSERT INTO signing_keys (tenant_id, purpose, kid, public_jwk, thumbprint, state, added_epoch,"
                " state_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (org_id, self.purpose, kid, jwk, thumb, KEY_CURRENT, epoch, epoch),
            )

    def add_next(self, org_id: str, kid: str, public_key: ec.EllipticCurvePublicKey) -> None:
        org_id, kid = self._org(org_id), self._kid(kid)
        jwk, thumb = self._public(public_key, kid)
        with self.db.transaction() as conn:
            rows = self._rows(conn, org_id)
            if not rows:
                raise KeysetError("org has no request-signing keyset")
            if any(r[2] == KEY_NEXT for r in rows):
                raise KeysetError("a rotation is already in progress")
            if any(r[0] == kid for r in rows):
                raise KeysetError("kid was already used in this org")
            if any(jws.jwk_thumbprint(json.loads(r[1])) == thumb for r in rows):
                raise KeysetError("public key was already used in this org")
            epoch = self._bump(conn, org_id)
            conn.execute(
                "INSERT INTO signing_keys (tenant_id, purpose, kid, public_jwk, thumbprint, state, added_epoch,"
                " state_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (org_id, self.purpose, kid, jwk, thumb, KEY_NEXT, epoch, epoch),
            )

    def flip(self, org_id: str, *, now: int) -> None:
        """§5.8 step 3: the next key becomes current; the old one starts retiring."""
        now = self._now(now)
        with self.db.transaction():
            self._flip_rows(self._org(org_id), now=now)

    def _flip_rows(self, org_id: str, *, now: int) -> None:
        with self.db.transaction() as conn:
            rows = self._rows(conn, org_id)
            if not rows:
                raise KeysetError("org has no request-signing keyset")
            nxt = [r[0] for r in rows if r[2] == KEY_NEXT]
            if not nxt:
                raise KeysetError("no provisioned next key to flip to")
            epoch = self._bump(conn, org_id)
            conn.execute(
                "UPDATE signing_keys SET state = ?, retire_after = ?, state_epoch = ?"
                " WHERE tenant_id = ? AND purpose = ? AND state = ?",
                (KEY_RETIRING, now + RETIRE_GRACE_S, epoch, org_id, self.purpose, KEY_CURRENT),
            )
            conn.execute(
                "UPDATE signing_keys SET state = ?, state_epoch = ? WHERE tenant_id = ? AND purpose = ? AND kid = ?",
                (KEY_CURRENT, epoch, org_id, self.purpose, nxt[0]),
            )

    def revoke(self, org_id: str, kid: str) -> None:
        """Emergency REVOKE: the kid is refused immediately, whatever its state."""
        org_id, kid = self._org(org_id), self._kid(kid)
        with self.db.transaction() as conn:
            rows = self._rows(conn, org_id)
            if not rows:
                raise KeysetError("org has no request-signing keyset")
            if kid not in {r[0] for r in rows}:
                raise KeysetError("kid is not in this org's keyset")
            epoch = self._bump(conn, org_id)
            conn.execute(
                "UPDATE signing_keys SET state = ?, retire_after = NULL, state_epoch = ?"
                " WHERE tenant_id = ? AND purpose = ? AND kid = ?",
                (KEY_REVOKED, epoch, org_id, self.purpose, kid),
            )

    # -- reads ----------------------------------------------------------------------------

    def epoch(self, org_id: str) -> int:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT keyset_epoch FROM signing_keysets WHERE tenant_id = ? AND purpose = ?", (org_id, self.purpose)
            ).fetchone()
        return int(row[0]) if row else 0

    def key_state(self, org_id: str, kid: str, *, now: int) -> str:
        now = self._now(now)
        org_id = self._org(org_id)
        with self.db.reading() as conn:
            rows = self._rows(conn, org_id)
        if not rows:
            raise KeysetError("org has no request-signing keyset")
        for r_kid, _jwk, state, retire_after in rows:
            if r_kid == kid:
                return self._effective(org_id, r_kid, state, retire_after, now)
        raise KeysetError("kid is not in this org's keyset")

    def public_jwk(self, org_id: str, kid: str) -> Optional[dict]:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT public_jwk FROM signing_keys WHERE tenant_id = ? AND purpose = ? AND kid = ?",
                (org_id, self.purpose, kid),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def has_keyset(self, org_id: str) -> bool:
        """Whether the org has EVER had a request-signing key (any state)."""
        with self.db.reading() as conn:
            return bool(self._rows(conn, org_id))

    def current(self, org_id: str, purpose: str) -> Optional[CurrentSigner]:
        """The ONE current signer (the :class:`~.signer.SignerDirectory` protocol)."""
        if purpose != self.purpose or type(org_id) is not str:
            return None
        with self.db.reading() as conn:
            for kid, jwk, state, _ra in self._rows(conn, org_id):
                if state == KEY_CURRENT:
                    return CurrentSigner(kid=kid, public_key=jws.public_key_from_jwk(json.loads(jwk)))
        return None

    def next_keys(self, org_id: str, purpose: str) -> tuple[NextSigner, ...]:
        if purpose != self.purpose or type(org_id) is not str:
            return ()
        with self.db.reading() as conn:
            rows = self._rows(conn, org_id)
        return tuple(
            NextSigner(kid=kid, public_key=jws.public_key_from_jwk(json.loads(jwk)))
            for kid, jwk, state, _ra in rows
            if state == KEY_NEXT
        )

    def verification_keyset(self, org_id: str, *, now: int) -> dict[str, ec.EllipticCurvePublicKey]:
        """Kids trusted at ``now``: next, current, and retiring within the grace."""
        now = self._now(now)
        with self.db.reading() as conn:
            rows = self._rows(conn, org_id) if type(org_id) is str else []
        out: dict[str, ec.EllipticCurvePublicKey] = {}
        for kid, jwk, state, retire_after in rows:
            if self._effective(org_id, kid, state, retire_after, now) in (KEY_NEXT, KEY_CURRENT, KEY_RETIRING):
                out[kid] = jws.public_key_from_jwk(json.loads(jwk))
        return out
