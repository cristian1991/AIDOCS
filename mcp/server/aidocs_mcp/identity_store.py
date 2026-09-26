"""Identity store — Layer 9 corporate-ready deliverable C-1.

First Layer 9 foundation: user identities + session tokens + role
enumeration. No SSO/SAML yet (that's C-3) and no project ACLs (C-4)
— this module just owns the primitive types and storage so the
rest of Layer 9 can compose against a stable shape.

Storage: sqlite table `identity_users` per project. Password hashes
use bcrypt when available; fall back to a clearly-flagged SHA256+salt
path for environments that can't install bcrypt (dev machines) so
tests can run without the native extension.

Role is stored as free-text. Validation/hierarchy lives in
rbac_store (C-2) which owns the dynamic role catalog + permission
matrix. This module only persists the string tag so identity rows
remain valid across role renames.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

# ── Roles ──

ROLE_ADMIN = "admin"
ROLE_CONDUCTOR = "conductor"
ROLE_OBSERVER = "observer"

#: Marker written into password_hash for a GATE-PROVISIONED identity (#207/#509).
#: It matches NO scheme _verify_password understands, so such a row can never be
#: authenticated by any password — the identity authenticates through CodeNexus.
#: It is also how we RECOGNISE gate principals, which the first-principal rule
#: below depends on: a legacy local account must not demote the machine owner.
GATE_PROVISIONED_HASH = "gate-provisioned$no-local-password"

VALID_ROLES: frozenset[str] = frozenset(
    {
        ROLE_ADMIN,
        ROLE_CONDUCTOR,
        ROLE_OBSERVER,
    },
)


# ── Data types ──


@dataclass(frozen=True)
class User:
    """One identity row.

    user_id: stable UUID-ish identifier; never reused.
    email: canonical login. Lowercased + trimmed on write.
    role: must be in VALID_ROLES.
    created_at: ISO-8601 UTC timestamp.
    disabled: admin can revoke without deleting history.
    """

    user_id: str
    email: str
    role: str
    created_at: str
    disabled: bool = False


@dataclass(frozen=True)
class SessionToken:
    """Opaque bearer token tying a session to a user.

    token: high-entropy random string; hashed at rest.
    user_id: owning user.
    issued_at / expires_at: ISO-8601 UTC timestamps. Tokens live for
        AUTOLOGIN_TTL_SECONDS (30 days, #509) — the operator logs in
        once a month, and only an invalidation EVENT cuts it short.
    """

    token: str
    user_id: str
    issued_at: str
    expires_at: str


# ── Hashing ──

#: THE TOKEN-LIFETIME LAW (#509, operator ruling 2026-07-25).
#:
#: After a successful login the token is valid for AUTOLOGIN for 30 DAYS
#: unless it is INVALIDATED. Invalidation is EVENT-DRIVEN and the events are
#: exhaustive: the user is removed from the project, the user is banned by the
#: platform, the user's permissions change. Only then must they log in again.
#:
#: Consequences that this constant exists to enforce:
#:   * IDLE TIME IS NOT AN INVALIDATION EVENT within 30 days. The previous
#:     12-hour TTL signed the operator out for doing nothing, which the law
#:     forbids and which the operator experienced as a random logout.
#:   * A PROJECT SWAP IS NOT AN INVALIDATION EVENT. That half is enforced by
#:     the machine-global identity home (#488, identity_db.py) — one ledger,
#:     so a token minted while project A was open still resolves under B.
AUTOLOGIN_TTL_SECONDS = 30 * 24 * 3600

#: Historical name kept so every existing call site / default keeps working.
_DEFAULT_TOKEN_TTL_SECONDS = AUTOLOGIN_TTL_SECONDS

#: Verdicts from the invalidation AUTHORITY (see below). Deliberately named,
#: because `None` vs `False` is the whole security decision here.
REVOCATION_REVOKED: bool | None = True  # affirmative "this operator is out"
REVOCATION_LIVE: bool | None = False  # affirmative "this operator is fine"
REVOCATION_UNKNOWN: bool | None = None  # NO ANSWER — never an invalidation

# ─────────────────────────────────────────────────────────────────────────────
# #529 / #662 — THE REVOCATION CHANNEL SEAM, under the PROJECTION model.
#
# THE RULING (#662, operator, 2026-07-30): permissions and projects are
# (user, project, org) facts owned by the codenexus server. **The local store
# is a PROJECTION, not an authority.** A projection answers "I hold this" —
# never "this is true".
#
# THE CARRIER DEFECT, the same disease as #440's commission stamp: a carrier
# with fewer states than the outcome it reports. `-> bool | None` carries the
# VERDICT correctly (revoked / live / no-answer) but carries NO FRESHNESS, so
# an UNKNOWN cannot distinguish "never asked" (today's real state — there is
# no channel) from "asked seconds ago, gate unreachable" (legitimately
# fail-soft) from "asked 40 days ago" (a projection quietly rotting). Per
# #662: a projection that cannot report its own freshness is the #627 disease
# in the authority layer.
#
# THE FIX IS THE CARRIER, not the call site: `RevocationProbe` adds the
# freshness axes; `revocation_authority_verdict` becomes a thin DERIVATION of
# it with its signature and its answer byte-identical to before, so the
# existing security tests that monkeypatch it keep passing unchanged and no
# consumer is re-audited. Landing the real channel remains a change of
# `probe_revocation_authority` ALONE.
#
# UNRESOLVED MARKER: honest EMPTY STRING per axis. Never the literal
# "unknown", never a shared sentinel.
#
# ── THE CHANNEL DESIGN (what `probe_revocation_authority` will do) ──
# PULL, never push. The operator's machine is not addressable from the server
# (NAT / offline / asleep), and a failed push is indistinguishable from
# "nobody was revoked" — which is the fail-OPEN direction. So the machine
# asks, opportunistically, on gate contact it was already making.
#
#   1. The gate publishes a per-user membership+perms FINGERPRINT plus a
#      monotonic `authority_revision` for the (user, project, org) tuple.
#      Ban and project-removal are immediate REVOKED answers; a permissions
#      change moves the fingerprint (operator ruling 2026-07-25).
#   2. A probe records verdict + observed_at + authority_revision + source.
#      A revision that MOVED means the projection must refetch perms; it does
#      NOT by itself mean revoked.
#   3. REVOKED -> call the already-landed local executor
#      `IdentityStore.invalidate_operator` (flag first, then drop tokens, then
#      revoke gate refresh tokens; idempotent).
#   4. Authority flows DOWN. The projection MUST NEVER write authority back
#      to the server — replication is bidirectional at the same tier, but a
#      projection is strictly downstream of the authority it projects.
#
# FAIL DIRECTIONS, PER CONSUMER, justified per consumer:
#   * autologin / `validate_token` -> FAIL-SOFT. Only an affirmative REVOKED
#     signs anyone out. Staleness is REPORTABLE, never a sign-out condition.
#     Fail-closed here re-creates the #509 lockout, and a blanket fail-closed
#     on an unresolvable authority is a denial of service on the operator.
#     Signing in is identification, NOT a grant.
#   * permission GRANTS -> FAIL-CLOSED on unresolved. A local miss is
#     UNRESOLVED, never "permitted" and never "denied-as-fact".
#   The two directions are opposite ON PURPOSE. Direction belongs to the
#   consumer, and a lane that flipped an authority fail-closed blanket-wide
#   broke six security tests by causing a lockout rather than a tightening.
# ─────────────────────────────────────────────────────────────────────────────

#: Honest unresolved marker for a projection axis that has no answer.
#: An EMPTY STRING, per axis — never "unknown", never a shared sentinel.
UNRESOLVED_AXIS = ""


@dataclass(frozen=True)
class RevocationProbe:
    """One question put to the invalidation AUTHORITY, and what came back.

    Every field is a bool / str / None by contract: a probe is a verdict
    SHAPE, never a credential carrier. No token, cookie, password or seed
    may ever ride in this record.
    """

    #: REVOCATION_REVOKED / REVOCATION_LIVE / REVOCATION_UNKNOWN.
    verdict: bool | None
    #: ISO-8601 instant the authority actually ANSWERED. ``UNRESOLVED_AXIS``
    #: when it never did — which is NOT the same as "answered nothing".
    observed_at: str
    #: The authority's monotonic revision for this (user, project, org) fact.
    #: ``UNRESOLVED_AXIS`` when unresolved. A MOVED revision means refetch
    #: the projection; it does not by itself mean revoked.
    authority_revision: str
    #: Which authority answered (e.g. "codenexus"). ``UNRESOLVED_AXIS`` when
    #: no channel answered at all.
    source: str
    #: Named cause, always non-empty. An unresolved authority with no named
    #: cause is a BUG, not a policy outcome.
    reason: str


def projection_freshness_is_unresolved(probe: RevocationProbe) -> bool:
    """True iff this projection cannot vouch for its own freshness.

    REPORTABLE, never enforcing — see the per-consumer fail directions above.
    A caller may surface staleness, refuse a GRANT on it, or refetch; a
    caller may NOT sign the operator out on it.
    """
    return probe.observed_at == UNRESOLVED_AXIS or probe.authority_revision == UNRESOLVED_AXIS


#: How long a probe answer stays fresh enough to reuse. `validate_token` runs on
#: EVERY authenticated call, and the channel design says the machine asks
#: "opportunistically, on gate contact it was already making" — so the probe must
#: never put a network round-trip on that hot path. Within the window the last
#: answer is reused, with its own `observed_at` carried forward so staleness stays
#: visible instead of being hidden by the cache.
REVOCATION_PROBE_FRESH_SECONDS = 300

#: SOCKET BUDGET FOR ONE PROBE. Was 10s, and that number is what turned a
#: CrowdSec ban into "the dashboard is broken": under a ban the gate DROPS
#: 80/443 rather than refusing, so every probe burned the full timeout — and
#: the Tauri kernel runs these behind a single SPAWN_GATE mutex, so the stalls
#: queued up and surfaced as "dashboard load timed out / MCP disconnected".
#: A liveness probe is an OPPORTUNISTIC nicety on a hot path; it may never be
#: worth more than a couple of seconds of an operator's time, and timing out
#: costs nothing but an UNKNOWN, which keeps them signed in.
REVOCATION_PROBE_TIMEOUT_SECONDS = 3

#: Last answer per operator: {user_id: (monotonic_deadline, RevocationProbe)}.
#: Process-local ON PURPOSE — a probe is evidence about a moment, not state to
#: persist. A restart should ask again, not inherit a verdict it cannot date.
_REVOCATION_PROBE_CACHE: dict[str, tuple[float, "RevocationProbe"]] = {}


def _probe_the_gate(user_id: str, email: str) -> "RevocationProbe":
    """One real question to the authority. No caching, no policy — just the ask."""
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    del email  # the credential identifies the operator; the address is not proof

    now = _iso_now()
    try:
        from .operator_token_resolution import (
            GATE_CRED_OK,
            GATE_CRED_REMEDY,
            cached_gate_credential,
            read_cache,
            record_gate_answer,
            stamp_gate_credential_verified,
        )
        from .xaacp_authority import DEFAULT_GATE_URL
    except Exception:  # noqa: BLE001 — no client machinery ⇒ nothing to ask with
        return RevocationProbe(
            verdict=REVOCATION_UNKNOWN,
            observed_at=UNRESOLVED_AXIS,
            authority_revision=UNRESOLVED_AXIS,
            source=UNRESOLVED_AXIS,
            reason="no_client_machinery: cannot reach an authority from here",
        )

    row = None
    try:
        row = read_cache()
    except Exception:  # noqa: BLE001
        row = None

    # ONLY A LABELLED, GATE-ISSUED, UNREJECTED CREDENTIAL GOES OVER THE WIRE
    # FROM HERE (#992). This path is BACKGROUND — `validate_token` runs it on
    # every authenticated call, with no operator watching and no way to consent
    # — so it gets the strictest rule available: anything short of
    # `GATE_CRED_OK` is declined BEFORE the socket, not explained after a 401.
    #
    # This is deliberately STRICTER than `cached_gate_credential` itself, and
    # the asymmetry is the point. That accessor still OFFERS an unlabelled
    # (`GATE_CRED_UNKNOWN_VINTAGE`) token — correct for a foreground caller the
    # operator asked for, and pinned by its own suite, because absence of a
    # label is not evidence of the wrong kind (#627). But "unproven" is exactly
    # what must not be SPENT UNPROMPTED: on the operator's machine that
    # unlabelled row held a LOCAL session token, so every background probe
    # earned HTTP 401 `unknown_token` — and seven of those trip the gate's
    # CrowdSec `http-generic-401-bf`, a four-hour IP ban that drops 443 and
    # blocks the very sign-in that would replace the bad credential. Observed
    # three times in one day.
    #
    # The fail direction is unchanged: every refusal below is UNKNOWN, and
    # UNKNOWN never invalidates (#508/#509 — offline must not mean signed out).
    cred = cached_gate_credential()
    if cred.reason != GATE_CRED_OK or not cred.token:
        return RevocationProbe(
            verdict=REVOCATION_UNKNOWN,
            observed_at=UNRESOLVED_AXIS,
            authority_revision=UNRESOLVED_AXIS,
            source=UNRESOLVED_AXIS,
            reason=(
                f"{cred.reason}: no credential this machine may present to the "
                f"authority unprompted — {GATE_CRED_REMEDY}"
            ),
        )

    token = cred.token

    # ONLY ANSWER ABOUT THE OPERATOR WE ACTUALLY HOLD A CREDENTIAL FOR.
    # The device credential belongs to whoever signed in on this machine; the
    # row being validated may be a DIFFERENT operator. Answering anyway would
    # let one operator's liveness vouch for another's — and on a REVOKED verdict
    # that mis-attribution executes `invalidate_operator` against the wrong
    # person. Identity has no fallback.
    holder = str((row or {}).get("user_id") or "").strip()
    if not holder or holder != str(user_id or "").strip():
        return RevocationProbe(
            verdict=REVOCATION_UNKNOWN,
            observed_at=UNRESOLVED_AXIS,
            authority_revision=UNRESOLVED_AXIS,
            source=UNRESOLVED_AXIS,
            reason="credential_is_for_another_operator: refusing a borrowed verdict",
        )
    url = str(DEFAULT_GATE_URL or "").rstrip("/") + "/v1/mcp"
    payload = _json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ai_project", "arguments": {"mode": "list"}},
        }
    ).encode("utf-8")
    try:
        from .governed_egress import assert_egress_allowed

        assert_egress_allowed(
            url,
            purpose="revocation_probe",
            allow_hosts=[urllib.parse.urlparse(url).hostname or ""],
        )
        req = urllib.request.Request(  # noqa: S310 — governed above; fixed authority
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=REVOCATION_PROBE_TIMEOUT_SECONDS
        ) as resp:
            live = int(getattr(resp, "status", 0)) == 200
        if live:
            # The authority accepted the credential: file the hourly
            # authorization stamp (#1000) so the next hour of contacts is
            # known-verified rather than unproven.
            with contextlib.suppress(Exception):
                stamp_gate_credential_verified()
    except urllib.error.HTTPError as exc:
        # FILE THE ANSWER through the one door (#1000): a 401/403 LATCHES the
        # credential (#992 — re-presenting it is the un-looped retry that
        # banned the operator's own machine); a 429 (CrowdSec), a 5xx (gate
        # deploy) or anything else says nothing about the TOKEN and must not
        # latch it — that would lock the operator out on the gate's own outage.
        with contextlib.suppress(Exception):
            record_gate_answer(
                status=int(getattr(exc, "code", 0) or 0),
                request_id=str(
                    (getattr(exc, "headers", None) or {}).get("x-request-id") or ""
                ),
            )
        # A REJECTED CREDENTIAL IS NOT AN OPERATOR VERDICT, and this is the one
        # inference that must not be made here. 401/403 says THIS TOKEN was
        # refused — which a rotation, an audience change or an expiry produces
        # just as readily as a ban. Reading it as REVOKED would execute
        # `invalidate_operator` (dropping every token and the gate refresh
        # token) against an operator who is merely holding a stale credential:
        # the #509 lockout, arrived at through the door marked security.
        #
        # Ban / project-removal / permission-change need the gate-side
        # membership+perms FINGERPRINT the channel design specifies (step 1),
        # which does not exist server-side yet. Until it does, this channel can
        # affirm LIVE and must not claim REVOKED.
        return RevocationProbe(
            verdict=REVOCATION_UNKNOWN,
            observed_at=now,
            authority_revision=UNRESOLVED_AXIS,
            source="codenexus",
            reason=(
                f"credential_rejected_http_{exc.code}: token-scoped, not an "
                "operator verdict; REVOKED needs the membership fingerprint (#529 step 1)"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — unreachable is NEVER an invalidation
        return RevocationProbe(
            verdict=REVOCATION_UNKNOWN,
            observed_at=UNRESOLVED_AXIS,
            authority_revision=UNRESOLVED_AXIS,
            source=UNRESOLVED_AXIS,
            reason=f"authority_unreachable: {type(exc).__name__}",
        )

    if live:
        return RevocationProbe(
            verdict=REVOCATION_LIVE,
            observed_at=now,
            authority_revision=UNRESOLVED_AXIS,
            source="codenexus",
            reason="authority_accepted_the_operator_credential",
        )
    return RevocationProbe(
        verdict=REVOCATION_UNKNOWN,
        observed_at=now,
        authority_revision=UNRESOLVED_AXIS,
        source="codenexus",
        reason="authority_answered_without_accepting: no operator verdict",
    )


def probe_revocation_authority(user_id: str, email: str) -> RevocationProbe:
    """Ask the invalidation AUTHORITY about this operator. THE CHANNEL SEAM.

    STATUS — the LIVE half of the channel is landed; REVOKED is not, and the
    difference is deliberate rather than unfinished.

    WHAT THIS NOW ANSWERS. An authenticated call to the gate with the device
    credential. A 200 is an affirmative `REVOCATION_LIVE` with a real
    `observed_at` and `source`, which closes the #662 carrier defect this seam
    was built for: an UNKNOWN can finally be told apart from a LIVE, and a
    freshness instant exists to reason about.

    WHAT IT STILL WILL NOT CLAIM, and why that is not laziness. `REVOKED` means
    "this operator is out" — ban, project removal, permission change — and it
    EXECUTES `invalidate_operator`, dropping every token and the gate refresh
    token. The only negative signal available today is an HTTP 401/403, which
    says THIS TOKEN was refused; a rotation, an audience change or a plain
    expiry produce it just as readily as a ban. Promoting that to an operator
    verdict would sign out a live operator holding a stale credential — the #509
    lockout, reached through the door marked security. Affirmative revocation
    needs the gate-side membership+perms FINGERPRINT the channel design
    specifies (step 1); that is server work, and until it lands this channel
    affirms LIVE and abstains otherwise.

    NEVER ON THE HOT PATH. `validate_token` runs on every authenticated call, so
    answers are reused for `REVOCATION_PROBE_FRESH_SECONDS` — the design's
    "opportunistically, on gate contact it was already making". The cached probe
    carries its own `observed_at`, so reuse never disguises staleness.
    """
    import time as _time

    key = str(user_id or "").strip()
    if key:
        hit = _REVOCATION_PROBE_CACHE.get(key)
        if hit is not None and hit[0] > _time.monotonic():
            return hit[1]
        # ACROSS PROCESSES, TOO (#992). The in-process cache above was written
        # for a long-lived server and is useless for the shape that actually
        # drives this: the Tauri kernel spawns a FRESH python CLI per invoke
        # (`run_json_cli`), so the 300-second window never once hit and every
        # authentication check became a cloud round trip. That is how a stale
        # credential reached the gate seven times and banned the machine.
        #
        # The memo carries its own `observed_at`, so this inherits a DATED
        # answer, never an undateable one — the objection the process-local
        # note raised is answered rather than overruled.
        memo = _read_probe_memo(key)
        if memo is not None:
            _REVOCATION_PROBE_CACHE[key] = (
                _time.monotonic() + REVOCATION_PROBE_FRESH_SECONDS,
                memo,
            )
            return memo

    probe = _probe_the_gate(user_id, email)
    if key:
        _REVOCATION_PROBE_CACHE[key] = (
            _time.monotonic() + REVOCATION_PROBE_FRESH_SECONDS,
            probe,
        )
        _write_probe_memo(key, probe)
    return probe


def _read_probe_memo(key: str) -> "RevocationProbe | None":
    """The persisted answer for ``key``, rehydrated, or None when stale/absent.

    Fail-soft in every direction: a missing, corrupt or unreadable memo simply
    means "ask again", never a verdict.
    """
    try:
        from .operator_token_resolution import read_probe_memo

        memo = read_probe_memo(key)
    except Exception:  # noqa: BLE001 — a broken memo is an absent memo
        return None
    if not isinstance(memo, dict):
        return None
    verdict_label = str(memo.get("verdict") or "")
    # Only LIVE and UNKNOWN are ever memoised. REVOKED EXECUTES an
    # invalidation, so replaying one from disk would let a stale file sign an
    # operator out — a persisted answer may keep a session, never end one.
    verdict = REVOCATION_LIVE if verdict_label == "live" else REVOCATION_UNKNOWN
    return RevocationProbe(
        verdict=verdict,
        observed_at=str(memo.get("observed_at") or UNRESOLVED_AXIS),
        authority_revision=str(memo.get("authority_revision") or UNRESOLVED_AXIS),
        source=str(memo.get("source") or UNRESOLVED_AXIS),
        reason=str(memo.get("reason") or "") + " [reused: cross-process memo]",
    )


def _write_probe_memo(key: str, probe: "RevocationProbe") -> None:
    """Persist LIVE/UNKNOWN answers. Best-effort; a failure just costs a probe."""
    if probe.verdict is REVOCATION_REVOKED:
        return  # never replayable — see _read_probe_memo
    with contextlib.suppress(Exception):
        from .operator_token_resolution import write_probe_memo

        write_probe_memo(
            key,
            {
                "verdict": "live" if probe.verdict is REVOCATION_LIVE else "unknown",
                "observed_at": probe.observed_at,
                "authority_revision": probe.authority_revision,
                "source": probe.source,
                "reason": probe.reason,
            },
            REVOCATION_PROBE_FRESH_SECONDS,
        )


def revocation_authority_verdict(user_id: str, email: str) -> bool | None:
    """Ask the INVALIDATION AUTHORITY whether this operator's tokens are dead.

    Returns ``REVOCATION_REVOKED`` / ``REVOCATION_LIVE`` / ``REVOCATION_UNKNOWN``.

    THOSE THREE ARE SENTINELS, NOT STRINGS — ``True`` / ``False`` / ``None``
    respectively, which is why this returns ``bool | None``. Written down
    because the names read like an enum of strings and I "corrected" the
    annotation to ``-> str`` on exactly that assumption (2026-08-31, reverted
    here). Two consequences the names hide:

      * ``if verdict:`` means "is REVOKED", not "has an answer";
      * LIVE and UNKNOWN are BOTH falsy, so ``if not verdict:`` silently
        conflates "the authority said you are fine" with "nobody answered" —
        the one distinction this module exists to preserve.

    Compare against the named constants (or ``is``), never for truthiness.

    THE VERDICT VIEW of :func:`probe_revocation_authority` — a DERIVATION, not
    a second path to the same fact. One authority fact derived twice with
    independent fail directions is #630's disease; this function must never
    grow its own channel logic.

    FAIL-SOFT ON UNREACHABLE, FAIL-CLOSED ONLY ON REVOKED. This is the ONE
    place in AIDOCS where the usual fail-closed instinct is WRONG, and it is
    written down here because someone will otherwise "fix" it back:

        Membership, bans and permissions live on CodeNexus (#207/#662), so the
        GATE is the authority for invalidation — not any local database. The
        local token is therefore a 30-DAY CACHE OF A GATE ANSWER, and under
        #662 the local store is a PROJECTION of server-owned authority. If the
        gate cannot be reached we have NO EVIDENCE of invalidation, and absence
        of evidence must never be read as invalidation: OFFLINE MUST NOT MEAN
        SIGNED OUT. Turning this into fail-closed re-creates the #509 lockout,
        where an operator holding a perfectly live 30-day token was thrown back
        to a login form because a check could not complete.

    Only an AFFIRMATIVE ``REVOCATION_REVOKED`` invalidates.

    STATUS — the local half is landed, the gate channel is NOT, so this
    returns ``REVOCATION_UNKNOWN`` unconditionally and the enforced
    invalidation events are the LOCAL positive ones: an operator disabled in
    the identity home (ban / removal) and an explicitly revoked token. A
    permissions change at the gate is NOT yet enforced — tracked as #529. The
    seam is wired and pinned by
    tests/security/test_dashboard_auth_lockout_cluster.py and
    tests/governance/test_revocation_projection.py, so landing the gate
    channel is a change of :func:`probe_revocation_authority` only, with no
    re-audit of this function's callers.
    """
    return probe_revocation_authority(user_id, email).verdict


def _hash_password(password: str) -> str:
    """Return a hash-string suitable for storage.

    Tries bcrypt first (industry standard), falls back to a salted
    SHA256 with a clear scheme marker so the verifier can tell which
    family it got. The fallback is only for test/dev environments
    that haven't installed bcrypt — production installs must include
    it.
    """
    try:
        import bcrypt  # type: ignore[import-not-found]
    except ImportError:
        salt = secrets.token_hex(16)
        digest = hashlib.sha256(
            (salt + password).encode("utf-8"),
        ).hexdigest()
        return f"sha256${salt}${digest}"
    else:
        hashed = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        )
        return "bcrypt$" + hashed.decode("utf-8")


def _verify_password(password: str, stored: str) -> bool:
    """Check a plaintext password against a stored hash."""
    if not password or not stored:
        return False
    if stored.startswith("bcrypt$"):
        try:
            import bcrypt  # type: ignore[import-not-found]
        except ImportError:
            return False
        return bool(
            bcrypt.checkpw(
                password.encode("utf-8"),
                stored.removeprefix("bcrypt$").encode("utf-8"),
            ),
        )
    if stored.startswith("sha256$"):
        try:
            _, salt, digest = stored.split("$", 2)
        except ValueError:
            return False
        expected = hashlib.sha256(
            (salt + password).encode("utf-8"),
        ).hexdigest()
        return hmac.compare_digest(expected, digest)
    return False


def _hash_token(token: str) -> str:
    """Tokens are also hashed at rest — stolen DB doesn't grant
    bearer access. Uses SHA256 (fast + adequate; tokens have full
    entropy so bcrypt's work-factor protection is wasted).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Store ──


class IdentityStore:
    """MACHINE-GLOBAL user/token store (#488).

    The operator is one person on one machine, so WHO THEY ARE cannot be a
    per-project fact: a token minted in project A was previously validated
    against project B's database, which is why a real account read as
    "invalid email or password" after switching projects. The home now sits
    beside the other machine-global stores (see identity_db.py for the full
    tier split and the legacy import).
    """

    def db_path(self, project_root: Path) -> Path:
        from .identity_db import identity_db_path

        return identity_db_path(project_root)

    def _read_conn(self, project_root: Path, *, require_table: str | None = None):
        """Open the store READ-ONLY, or None when it does not exist (#553).

        Read methods used to call init_db(), which mkdirs and CREATES the
        database — so merely LOOKING something up materialised a store. The admin
        path scans EVERY registered project to find an id's owner
        (cli._resolve_admin_root), so one lookup littered every project plus
        whatever cwd the fallback resolved to, and the doctor then reports that
        debris as `half_init`. A read answers "what is there", and "nothing is
        there" is a valid answer — only an explicit write may create.

        Returns None for an absent file AND for a present-but-tableless file (an
        interrupted creation, or another store's file at that path): empty, and
        deliberately NOT repaired on a read path.

        ``require_table`` closes the gap between "file exists" and "MY schema
        exists". aidocs_identity.sqlite3 is SHARED — SessionFreezeStore creates
        it for `session_freeze` — so an identity read could find a file with
        tables but WITHOUT identity_users and raise "no such table" instead of
        answering. Callers that relied on the old init-on-read to create their
        schema (bootstrap_local_superadmin looks a user up BEFORE creating one)
        then died on a bare lookup. A missing table is not an error: it means no
        rows were ever recorded, which is exactly None/empty. The caller's WRITE
        path still creates the schema when it actually inserts.
        """
        path = self.db_path(project_root)
        if not path.is_file():
            return None
        try:
            # read_only=True is the canonical connect's `file:...?mode=ro`
            # (#755, 2026-08-18). It also sets row_factory=sqlite3.Row and
            # applies the pragmas a reader CAN take -- synchronous,
            # busy_timeout, foreign_keys -- which this call site had none of.
            conn = _canonical_connect(path, read_only=True)
            probe = (
                ("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1", (require_table,))
                if require_table
                else ("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1", ())
            )
            if conn.execute(*probe).fetchone() is None:
                conn.close()
                return None
        except sqlite3.Error:
            return None
        return conn

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # #746: aidocs_identity.sqlite3 is SHARED, so whichever store creates it
        # decides the journal mode every later connection inherits (journal_mode
        # lives in the FILE HEADER). Every creator therefore goes through the one
        # canonical connect (#755) -- WAL by luck is not WAL by design. It also
        # turns foreign_keys ON, which SQLite defaults OFF per connection and
        # without which the identity_tokens -> identity_users FK below is inert.
        with _canonical_connect(path, durability=_Durability.RUNTIME) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS identity_users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS identity_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES identity_users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_tokens_user
                    ON identity_tokens(user_id);
            """)
            conn.commit()
        # #488: adopt this project's pre-global identity rows once (empty
        # tables only — an operator's delete is never undone). No-op after
        # the first successful import and on fresh machines.
        from .identity_db import adopt_legacy_project_identity

        adopt_legacy_project_identity(project_root)

    def create_user(
        self,
        project_root: Path,
        email: str,
        password: str,
        role: str,
    ) -> User:
        """Register a new user. Raises ValueError on invalid role / empty
        email / duplicate email.
        """
        email = str(email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError(f"invalid email: {email!r}")
        role = str(role or "").strip()
        if not role:
            raise ValueError("role is required")
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r} (allowed: {sorted(VALID_ROLES)})")
        if not password or len(password) < 8:
            raise ValueError("password must be ≥ 8 characters")
        self.init_db(project_root)
        user_id = "u_" + secrets.token_hex(12)
        now = _iso_now()
        hashed = _hash_password(password)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            try:
                conn.execute(
                    "INSERT INTO identity_users (user_id, email, role, "
                    "password_hash, created_at, disabled) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (user_id, email, role, hashed, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"email already registered: {email}") from exc
        return User(
            user_id=user_id,
            email=email,
            role=role,
            created_at=now,
            disabled=False,
        )

    def get_user_by_email(
        self,
        project_root: Path,
        email: str,
    ) -> User | None:
        conn = self._read_conn(project_root, require_table="identity_users")
        if conn is None:
            return None
        with closing(conn):
            row = conn.execute(
                "SELECT user_id, email, role, created_at, disabled "
                "FROM identity_users WHERE email = ?",
                (str(email).strip().lower(),),
            ).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=bool(row["disabled"]),
        )

    def get_user_by_id(
        self,
        project_root: Path,
        user_id: str,
    ) -> User | None:
        """Resolve a user row by user_id. Returns None when absent.

        Used by host-session binding resolution so a bound
        OperatorContext carries email/role straight from the user
        row instead of re-deriving them piecemeal.
        """
        if not user_id:
            return None
        conn = self._read_conn(project_root, require_table="identity_users")
        if conn is None:
            return None
        with closing(conn):
            row = conn.execute(
                "SELECT user_id, email, role, created_at, disabled "
                "FROM identity_users WHERE user_id = ?",
                (str(user_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=bool(row["disabled"]),
        )

    def authenticate(
        self,
        project_root: Path,
        email: str,
        password: str,
    ) -> User | None:
        """Return the User on correct password, None otherwise.

        Constant-time-ish: always hashes a dummy password on
        user-not-found to avoid leaking email existence via
        timing. Disabled users never authenticate regardless of
        password correctness.
        """
        conn = self._read_conn(project_root, require_table="identity_users")
        if conn is None:
            return None
        with closing(conn):
            row = conn.execute(
                "SELECT user_id, email, role, password_hash, created_at, disabled "
                "FROM identity_users WHERE email = ?",
                (str(email).strip().lower(),),
            ).fetchone()
        if row is None:
            # Prevent timing-based email enumeration.
            _verify_password(password, "sha256$deadbeef$0" * 2)
            return None
        if row["disabled"]:
            _verify_password(password, row["password_hash"])
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=False,
        )

    def login(
        self,
        project_root: Path,
        email: str,
        password: str,
        ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
    ) -> "SessionToken | None":
        """The SANCTIONED, password-gated mint (2026-07-16 security fix).

        A bearer token can be obtained ONLY by presenting a correct password:
        verify via ``authenticate`` (which handles unknown-email timing + disabled
        users), and mint a token ONLY on success. Returns None — and mints NOTHING —
        on any failure. This is the single sanctioned door to a token; the bare
        ``user_id`` mint below is an INTERNAL primitive, not a public API.

        Closes the mandatory-login (#404) side door: previously ``issue_token`` minted
        from a bare user_id with no proof of identity, so any caller reaching the mint
        got a token for any account (incl. super_admin). Python cannot stop a
        code-execution attacker from calling the internal primitive directly; that is
        the Rust enforcement kernel's job (#417). This gates the SANCTIONED surface.
        """
        user = self.authenticate(project_root, email, password)
        if user is None:
            return None
        return self._issue_token_for_authenticated_user(
            project_root, user.user_id, ttl_seconds
        )

    def login_with_codenexus_principal(
        self,
        project_root: Path,
        email: str,
        ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
    ) -> "SessionToken | None":
        """The SECOND sanctioned mint: a CODENEXUS-AUTHENTICATED principal.

        #207 (KING RULING) names "codenexus-authenticated web session" as a valid
        principal at the auth seam, and its absorbed #420/#421 require that such a
        login STAMP a local token. Without this door the desktop dashboard asked
        for two independent logins — a cloud sign-in that satisfied nothing local,
        then a local password form — and the second was unreachable for an
        operator whose authority lives in codenexus.

        CALLER CONTRACT — this is an authentication boundary only when honoured:
        the caller MUST have just verified, against the GATE, that the bearer it
        holds is live, and MUST pass the email the GATE attested for it. Never
        pass an email supplied by a webview, a tool argument, or a config file:
        that would re-open the #404 side door this door is modelled to avoid.
        The single sanctioned caller is ``aidocs dashboard-login-oauth``, which
        performs that verification immediately before calling in.

        PROVISIONING (operator ruling 2026-07-25): a gate-verified principal with
        no local row IS provisioned, passwordless. The old behaviour returned None
        here — "never for provisioning a local operator that does not exist" — and
        that was a dead end under the one-authority ruling: the operator's accounts
        live on CodeNexus, local password accounts are FORBIDDEN, so a local row
        could never come into existence and this door could never open. Observed
        live: the operator signed in with CodeNexus successfully and the dashboard
        kept showing the connect screen forever, because no local token was ever
        stamped.

        The row provisioned here is NOT an account. It carries NO password hash, so
        it can never be authenticated locally — it is a local RECORD of an identity
        the GATE attested. That is what "one authority" means: CodeNexus decides who
        you are; this table only remembers what it was told.

        FIRST-PRINCIPAL RULE: the first gate-verified principal on a machine is
        provisioned ADMIN (this is the "1 dashboard = 1 user = bind" directive — a
        personal desktop's first verified operator is its owner). Any LATER,
        different principal is provisioned as an OBSERVER, so a second person
        signing in on the same box cannot silently inherit the owner's authority.
        Widening a later principal is then a deliberate, audited act, not a side
        effect of logging in.

        Still returns None — minting NOTHING — when the account exists and is
        DISABLED. A ban is a positive act by the authority and it is honoured here.
        """
        user = self.get_user_by_email(project_root, email)
        if user is not None and user.disabled:
            return None
        if user is None:
            # No local row for a GATE-ATTESTED email: provision it, passwordless.
            # The caller contract above guarantees this email came from the gate's
            # own response over TLS, never from a webview or a tool argument.
            # FIRST-PRINCIPAL RULE counts only GATE-PROVISIONED rows, never
            # legacy local accounts. A machine carrying an old
            # local-operator@... row would otherwise demote its own owner to
            # observer on first cloud sign-in — which is exactly what happened
            # on the operator's box.
            existing = [
                u
                for u in self.list_users(project_root)
                if not u.disabled and self._is_gate_provisioned(project_root, u.user_id)
            ]
            role = ROLE_ADMIN if not existing else ROLE_OBSERVER
            try:
                user = self.create_user(
                    project_root,
                    email=email,
                    password=secrets.token_urlsafe(32),
                    role=role,
                )
            except ValueError:
                # Lost a provisioning race, or the email is malformed. Re-read
                # rather than minting blind; still None if it truly is not there.
                user = self.get_user_by_email(project_root, email)
            else:
                self._mark_gate_provisioned(project_root, user.user_id)
            if user is None or user.disabled:
                return None
            # The random password above is never returned, never stored in
            # plaintext, and never needed: this principal authenticates through
            # the GATE. It exists only because create_user requires one, and it is
            # deliberately unguessable so the row cannot be used as a local
            # password login — which the ruling forbids.
        return self._issue_token_for_authenticated_user(
            project_root, user.user_id, ttl_seconds
        )

    def _mark_gate_provisioned(self, project_root: Path, user_id: str) -> None:
        """Replace a provisioned row's password hash with the sentinel.

        create_user REQUIRES a password, so provisioning generates a random one.
        Leaving that hash in place would mean a (practically unguessable, but
        real) local password exists for an identity the operator ruled must have
        none. The sentinel matches no scheme _verify_password knows, so the row
        becomes unauthenticatable locally AND recognisable as a gate principal.
        """
        try:
            with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
                conn.execute(
                    "UPDATE identity_users SET password_hash = ? WHERE user_id = ?",
                    (GATE_PROVISIONED_HASH, user_id),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def _is_gate_provisioned(self, project_root: Path, user_id: str) -> bool:
        """True iff this row was provisioned by a gate-verified sign-in."""
        try:
            with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
                row = conn.execute(
                    "SELECT password_hash FROM identity_users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        except sqlite3.Error:
            return False
        return bool(row) and str(row[0] or "") == GATE_PROVISIONED_HASH

    def _issue_token_for_authenticated_user(
        self,
        project_root: Path,
        user_id: str,
        ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
    ) -> SessionToken:
        """INTERNAL token-mint primitive. NOT an authentication boundary — it trusts
        that the caller has ALREADY verified the principal. The ONLY sanctioned caller
        is ``login`` (password-verified). Never wire a public/agent-reachable path to
        this directly: doing so re-opens the mandatory-login side door. Underscore-
        marked to signal that; true tamper-proof enforcement is the Rust kernel (#417).
        """
        self.init_db(project_root)
        token = secrets.token_urlsafe(32)
        issued = time.time()
        expires = issued + max(60, int(ttl_seconds))
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.execute(
                "INSERT INTO identity_tokens "
                "(token_hash, user_id, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    _hash_token(token),
                    user_id,
                    _iso(issued),
                    _iso(expires),
                ),
            )
            conn.commit()
        return SessionToken(
            token=token,
            user_id=user_id,
            issued_at=_iso(issued),
            expires_at=_iso(expires),
        )

    def validate_token(
        self,
        project_root: Path,
        token: str,
    ) -> User | None:
        """Resolve a bearer token to its owning user. Returns None on
        unknown / expired / disabled-user / affirmatively-revoked tokens.

        THE 30-DAY AUTOLOGIN CONTRACT (#509). This is the SINGLE authority for
        "is the operator still signed in", and it answers from exactly three
        positive facts:

          1. the token row exists in the MACHINE-GLOBAL identity home (#488) —
             so a project swap cannot make a live token look absent;
          2. its own ``expires_at`` (30 days from login) has not passed — idle
             time inside that window is NOT an invalidation event;
          3. the invalidation AUTHORITY has not affirmatively revoked the
             operator (``revocation_authority_verdict``: an UNREACHABLE
             authority is fail-SOFT and keeps the operator signed in — see
             that function for why fail-closed here is the bug, not the fix).
             An affirmative REVOKED does not merely refuse this call: it
             EXECUTES ``invalidate_operator`` so the revocation survives the
             authority going quiet (#529 step 3). Without that, fail-soft
             would silently un-revoke on the next unreachable moment.

        No caller may re-derive this verdict per project, and nothing may infer
        invalidation from missing or unreachable evidence.
        """
        if not token:
            return None
        self.init_db(project_root)
        token_hash = _hash_token(token)
        now_iso = _iso_now()
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT u.user_id, u.email, u.role, u.created_at, u.disabled, "
                "t.expires_at "
                "FROM identity_tokens t "
                "JOIN identity_users u ON u.user_id = t.user_id "
                "WHERE t.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        if str(row["expires_at"]) <= now_iso:
            return None
        if row["disabled"]:
            # A POSITIVE local invalidation: the operator was banned/removed.
            return None
        # The authority gets the last word — but ONLY when it actually answers
        # "revoked". Any other outcome (live, no answer, gate unreachable)
        # leaves the 30-day token standing. Never turn this into a truthiness
        # check: REVOCATION_UNKNOWN is None and must not sign anyone out.
        try:
            verdict = revocation_authority_verdict(row["user_id"], row["email"])
        except Exception:
            verdict = REVOCATION_UNKNOWN  # an exploding authority is an absent one
        if verdict is REVOCATION_REVOKED:
            # EXECUTE the revocation, do not merely refuse this one request
            # (#529 channel design step 3; the executor landed with no caller).
            #
            # Returning None alone denies THIS call and leaves the projection
            # live: the row undisabled, sibling token rows intact, the gate
            # refresh token intact. The revocation would then hold only while
            # the authority kept repeating it — and because this consumer is
            # deliberately FAIL-SOFT, the first unreachable-gate moment answers
            # UNKNOWN and signs the revoked operator BACK IN. That is the
            # fail-OPEN direction. Executing makes the revocation STICK, which
            # is precisely what lets the autologin path stay fail-soft without
            # leaking authority.
            #
            # NO SECOND DERIVATION (#630): this acts on the ONE verdict already
            # obtained above. It must never re-ask the authority or reach past
            # `revocation_authority_verdict` to the probe — that would be a
            # second independent path to one authority fact, and it would
            # bypass the monkeypatches the security tests install on the
            # verdict function.
            try:
                self.invalidate_operator(
                    project_root,
                    row["user_id"],
                    reason="revocation authority answered REVOKED (#529)",
                )
            except Exception:
                # The DENIAL is already decided by the verdict; the executor
                # only makes it durable. A failing executor must never
                # resurrect the session — that would make a failed revocation
                # look like a granted one. Best-effort here, then deny.
                pass
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=False,
        )

    def revoke_token(self, project_root: Path, token: str) -> bool:
        """Drop a single token. Returns True iff present. Operators
        wipe stolen tokens with this; full-user logout uses
        revoke_all_tokens().
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "DELETE FROM identity_tokens WHERE token_hash = ?",
                (_hash_token(token),),
            )
            conn.commit()
        return cur.rowcount > 0

    def revoke_all_tokens(self, project_root: Path, user_id: str) -> int:
        """Drop every token for a user. Returns count. Call after
        password change / role change / admin lockout.
        """
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "DELETE FROM identity_tokens WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
        return cur.rowcount

    def purge_expired_tokens(self, project_root: Path) -> int:
        """Delete every expired token row. Returns count purged.

        Token-lifecycle GC (2026-05-20): without this, every
        ``issue_token`` accretes a row that never leaves the table —
        the dashboard minting a token per admin command grew
        identity_tokens unbounded. Callers (mint path, logout, app
        exit) run this so the table stays bounded by the number of
        currently-live sessions.
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "DELETE FROM identity_tokens WHERE expires_at <= ?",
                (now_iso,),
            )
            conn.commit()
        return cur.rowcount

    def count_tokens(
        self,
        project_root: Path,
        user_id: str | None = None,
    ) -> int:
        """Count token rows (optionally for one user). Used by tests
        + diagnostics to assert the table doesn't grow unbounded.
        """
        conn = self._read_conn(project_root, require_table="identity_tokens")
        if conn is None:
            return 0
        with closing(conn):
            if user_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM identity_tokens WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM identity_tokens",
                ).fetchone()
        return int(row[0]) if row else 0

    def set_disabled(
        self,
        project_root: Path,
        user_id: str,
        disabled: bool,
    ) -> bool:
        """Flip the disabled flag. Returns True iff the user exists."""
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), row_factory=False) as conn:
            cur = conn.execute(
                "UPDATE identity_users SET disabled = ? WHERE user_id = ?",
                (1 if disabled else 0, user_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def invalidate_operator(
        self,
        project_root: Path,
        user_id: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        """IMMEDIATE revocation for a BAN or a PROJECT-REMOVAL (#509).

        The TOKEN-LIFETIME LAW above says a 30-day token stands "unless it is
        INVALIDATED", and names the events: removed from the project, banned by
        the platform, permissions change. Operator ruling 2026-07-25 split those:
        **ban and project-removal revoke immediately; a permissions change does
        NOT.** Perms need no revocation because
        ``OperatorAuthService.require_permission`` resolves every check through a
        LIVE ``RBACStore().has_permission`` lookup — nothing is cached on the
        token, so a changed permission lands on the next call while the session
        survives. Revoking on a perms edit would sign the operator out for a
        minor grant change, which the law does not ask for.

        Every primitive this composes already existed (``set_disabled``,
        ``revoke_all_tokens``) with NO production caller, each hidden in
        vulture_allowlist.py as a "false positive" though only tests consumed
        them — so the missing invalidation was invisible on every deploy.

        WHY ONE OPERATION AND NOT TWO CALLS. The flag and the tokens must move
        together. Half-applied states are both wrong in dangerous ways:
          * flag set, tokens alive  -> ``validate_token`` rejects on the flag, but
            any code path reading tokens directly still sees live rows;
          * tokens dropped, flag clear -> a banned operator simply logs in again.
        Ordered flag-FIRST so that if the process dies between the two writes the
        surviving state is the SAFE one (rejecting) rather than the permissive one.

        Returns ``{"user_id", "disabled", "tokens_revoked", "reason"}``.
        IDEMPOTENT: re-invalidating reports ``tokens_revoked=0`` and stays
        disabled — the gate may deliver the same revocation more than once.

        Raises ValueError on an unknown user or an empty reason. An unknown user
        must not report success: that would let a failed revocation look applied.
        This does NOT delete the row — the audit trail is the point.
        """
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("user_id is required to invalidate an operator")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError(
                "a reason is required to invalidate an operator (audited security action)",
            )
        # Flag first (see the ordering note above), and it doubles as the
        # existence check — set_disabled returns False when no row matched.
        if not self.set_disabled(project_root, user_id, True):
            raise ValueError(f"unknown user_id: {user_id!r}")
        tokens_revoked = self.revoke_all_tokens(project_root, user_id)
        # GATE REFRESH TOKENS DIE TOO. Local tokens and gate refresh tokens are
        # two doors to the same session: drop the local ones and leave a refresh
        # token live, and the banned operator simply exchanges it for a fresh
        # access token — the revocation would be cosmetic. The refresh store is
        # co-located in this same control-plane DB (its db_path delegates here),
        # so this is one operation rather than a layering violation. Imported
        # locally because outer_gate_token_store imports THIS module.
        from .outer_gate_token_store import OuterGateTokenStore

        gate_refresh_revoked = OuterGateTokenStore().revoke_refresh_for_user(
            project_root,
            user_id,
        )
        return {
            "user_id": user_id,
            "disabled": True,
            "tokens_revoked": int(tokens_revoked),
            # Reported, not swallowed: an incomplete revocation must never look
            # complete to the caller that has to decide whether the ban took.
            "gate_refresh_revoked": int(gate_refresh_revoked),
            "reason": reason,
        }

    def list_users(self, project_root: Path) -> list[User]:
        """Every user row, disabled or not."""
        conn = self._read_conn(project_root, require_table="identity_users")
        if conn is None:
            return []
        with closing(conn):
            rows = conn.execute(
                "SELECT user_id, email, role, created_at, disabled "
                "FROM identity_users ORDER BY created_at ASC",
            ).fetchall()
        return [
            User(
                user_id=r["user_id"],
                email=r["email"],
                role=r["role"],
                created_at=r["created_at"],
                disabled=bool(r["disabled"]),
            )
            for r in rows
        ]


# ── helpers ──


def _iso_now() -> str:
    return _iso(time.time())


def _iso(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(
        epoch,
        tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
