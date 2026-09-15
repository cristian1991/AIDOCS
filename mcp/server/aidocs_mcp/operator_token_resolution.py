"""Machine-side operator token cache + the ONE token-resolution door (#421).

AUTHENTICATE ONCE per machine: ``aidocs operator-login`` mints a bearer token
(password-gated via ``IdentityStore.login``) and — by default — caches it in
a per-user file OUTSIDE any repo (``~/.aidocs/operator_token.json``,
overridable via the ``AIDOCS_TOKEN_CACHE`` env var). Every CLI surface that
needs an operator token then resolves it through ONE chain:

    1. env  ``AIDOCS_OPERATOR_TOKEN``
    2. CLI  ``--operator-token <token>``
    3. machine cache (if not expired; expired rows are pruned on read)

This never weakens the server-side gate: the resolved token is still
validated against ``identity_tokens`` on every use, and per-session binding
approval remains an explicit one-command consent — the cache only removes
the credential RE-ENTRY ceremony, not the consent.

Security posture:
  - The default path lives under the user's HOME; permissions are tightened
    to owner-only where the platform supports it (POSIX chmod 0600). On
    Windows the per-user profile directory is the boundary — best-effort,
    no ACL gymnastics in this slice.
  - A CUSTOM cache path that is world/group-readable is REFUSED
    (``PermissionError``): a bearer token is never written where other
    local users can read it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ENV_VAR = "AIDOCS_OPERATOR_TOKEN"
FLAG = "--operator-token"
CACHE_PATH_ENV = "AIDOCS_TOKEN_CACHE"

#: WHO ISSUED THE CACHED CREDENTIAL. This file is the SHARED credential store
#: — the Tauri kernel, the CLI and the hooks all read it so one login serves
#: all three — and until this label existed it recorded WHAT the token is but
#: never WHO VOUCHES FOR IT. Two incompatible kinds shared the slot:
#: a local `IdentityStore` session token, valid only against this machine, and
#: an `ogt_`-prefixed gate token, the only kind codenexus.cloud has issued.
#: Indistinguishable in that slot, so a cloud caller could only send whatever
#: was there and read an opaque 401 — unable to tell a revoked operator from a
#: credential the authority was never asked to mint.
ISSUER_LOCAL = "local_identity"
ISSUER_CODENEXUS = "codenexus_gate"

GATE_CRED_OK = "gate_issued"
GATE_CRED_ABSENT = "no_session"
GATE_CRED_LOCAL_ONLY = "local_identity_only"
GATE_CRED_UNKNOWN_VINTAGE = "issuer_unrecorded"
GATE_CRED_EXPIRED = "gate_token_expired"
#: The AUTHORITY REFUSED THIS EXACT CREDENTIAL and we recorded that it did.
#: Distinct from EXPIRED (our clock said so) and from LOCAL_ONLY (we knew
#: before asking): here the gate answered, and the answer was no. See
#: :func:`latch_gate_credential_rejected` for why this has to be sticky.
GATE_CRED_REJECTED = "gate_rejected_credential"

#: Named remedy for a local-only session (law 311bf3e6). The browser flow is
#: the door that yields a credential the gate recognises.
GATE_CRED_REMEDY = (
    "sign in through the Dashboard's CodeNexus browser flow — a local sign-in "
    "authenticates you to this machine only, and issues nothing the cloud has "
    "ever seen"
)

#: HOW OFTEN AUTHORIZATION IS RE-VERIFIED against the authority (#1000).
#:
#: ONE credential, TWO clocks — and they measure different things. The
#: credential's own expiry (`gate_token_expires_at`, 30 days like the login
#: token) says how long the bearer is VALID. This window says how long a
#: PERMISSIONS answer stays fresh: past it, the next cloud contact must be a
#: real question to the authority, whose 200 re-stamps the row and whose
#: 401/403 latches it (#992). "Recheck perms every hour, not just break the
#: token every hour and require relogin" — operator directive, verbatim.
#:
#: The recheck never WITHHOLDS the credential: a due recheck is a fact about
#: staleness, and the live call that follows is the recheck. Withholding would
#: turn an hour of offline into a sign-out (#508/#509).
AUTHZ_RECHECK_SECONDS = 3600
#: The cache-row key carrying the last accepted-answer instant.
AUTHZ_STAMP_KEY = "gate_authz_verified_at"
#: HOW OFTEN THE STAMP IS ACTUALLY REWRITTEN. Every 200 from the sitter
#: thread, the revocation probe and `link_project` files a stamp, and each
#: rewrite replaces the WHOLE row — a window in which a concurrent sign-in
#: (`write_cache`) or a #992 latch is silently reverted. A stamp younger than
#: this is fresh enough; the row is left alone.
AUTHZ_STAMP_DEBOUNCE_SECONDS = 300

#: THE WAY BACK IN WITHOUT A HUMAN (#1000). Measured 2026-09-04: the row held
#: a 30-day local token, an `ogt_` gate token dead within the hour, and NO
#: FIELD CONTAINING "refresh" ANYWHERE — so the only path to a live cloud
#: credential was a person at the Dashboard's browser flow. Every unattended
#: path (the sitter's sync, a lane worker, any background reconcile) therefore
#: failed PERMANENTLY the moment the hour elapsed.
#:
#: The gate has minted an `ogr_` refresh token on every authorization_code
#: exchange since #92 (`outer_gate_oauth._refresh_grant`, 30-day TTL,
#: single-use rotation) and the Tauri kernel already reads it out of the token
#: response. It was simply never written HERE, beside the credential the
#: daemon reads. These three keys are that omission repaired: the refresh
#: token, the client it was issued to (the gate fails the rotation closed when
#: the client drifts), and the SCOPE the cached bearer was minted with — the
#: baseline the hourly recheck compares against.
GATE_REFRESH_KEY = "gate_refresh_token"
GATE_CLIENT_KEY = "gate_client_id"
GATE_SCOPE_KEY = "gate_scope"

#: WHAT THE RECHECK SAW CHANGE, recorded so a permission change is observable
#: rather than merely applied. "On refresh it needs to check if any perms
#: changed" — operator directive, verbatim.
SCOPE_CHANGED_KEY = "gate_scope_changed_at"
SCOPE_ADDED_KEY = "gate_scope_added"
SCOPE_REMOVED_KEY = "gate_scope_removed"

#: ONE ATTEMPT PER WINDOW, remembered ACROSS PROCESSES. The Tauri kernel
#: spawns a fresh python per invoke, so in-process restraint is no restraint
#: at all — that is precisely how #992 turned a stale credential into a
#: four-hour CrowdSec ban. A recorded attempt is what makes "never a retry
#: loop" true for a process that has no memory of its predecessor.
RENEWAL_ATTEMPT_KEY = "gate_renewal_attempted_at"
RENEWAL_REASON_KEY = "gate_renewal_last_reason"


def default_cache_path() -> Path:
    """The per-user machine cache location (env-overridable for tests /
    multi-profile setups)."""
    override = str(os.environ.get(CACHE_PATH_ENV) or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "operator_token.json"


def _world_or_group_readable(path: Path) -> bool:
    """POSIX group/other read bits on an existing path. Windows has no
    POSIX mode bits worth trusting (st_mode is synthetic 0o666), so the
    check is a no-op there — the user profile dir is the boundary."""
    if os.name == "nt":
        return False
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return False
    return bool(mode & 0o077)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_cache(
    *,
    token: str,
    user_id: str,
    expires_at: str,
    project_root: str | Path = "",
    cache_path: str | Path | None = None,
    gate_token: str = "",
    gate_token_expires_at: str = "",
    issuer: str = "",
    gate_refresh_token: str = "",
    gate_client_id: str = "",
    gate_scope: str = "",
) -> Path:
    """Persist the bearer token to the machine cache. Returns the path.

    Owner-only perms best-effort (chmod 0600 on POSIX). A CUSTOM path
    (``cache_path`` given) that is world/group-readable — before or after
    the write — raises ``PermissionError`` and leaves no token behind.
    """
    if not token:
        raise ValueError("token is required")
    custom = cache_path is not None
    path = Path(cache_path) if custom else default_cache_path()
    if custom and _world_or_group_readable(path):
        raise PermissionError(
            f"refusing to cache the operator token to world/group-readable path: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": str(token),
        "user_id": str(user_id or ""),
        "expires_at": str(expires_at or ""),
        "project_root": str(project_root or "").replace("\\", "/"),
        "cached_at": _iso_now(),
        # WHO VOUCHES FOR THIS. Written even when empty, so a row's SILENCE
        # about its issuer is itself dateable: rows lacking the key predate
        # the label and are reported as unproven rather than guessed at.
        "issuer": str(issuer or ""),
    }
    # Kept ALONGSIDE the local token, never instead of it. Local admin auth
    # validates `token` against `identity_tokens`; overwriting it with a gate
    # token would fail every local path shut and lock the operator out of
    # their own dashboard.
    if gate_token:
        payload["gate_token"] = str(gate_token)
        # ITS OWN CLOCK. `expires_at` above belongs to the LOCAL token (30
        # days); a gate access token measured about an hour. One row, two
        # credentials, two lifetimes — conflating them would hand out a stale
        # bearer for a month.
        if gate_token_expires_at:
            payload["gate_token_expires_at"] = str(gate_token_expires_at)
        # THE UNATTENDED WAY BACK (#1000). Written only alongside a gate token,
        # because a refresh token without the credential it renews is a secret
        # with no purpose on this disk. ABSENT STAYS ABSENT (#627): a caller
        # that does not know the client or the scope writes neither, and the
        # renewal path reports "nothing to renew from" instead of guessing.
        if gate_refresh_token:
            payload[GATE_REFRESH_KEY] = str(gate_refresh_token)
        if gate_client_id:
            payload[GATE_CLIENT_KEY] = str(gate_client_id)
        if gate_scope:
            payload[GATE_SCOPE_KEY] = str(gate_scope)
    _atomic_write(path, payload)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    if custom and _world_or_group_readable(path):
        with contextlib.suppress(OSError):
            path.unlink()
        raise PermissionError(
            f"refusing to cache the operator token to world/group-readable path: {path}"
        )
    return path


@dataclass(frozen=True)
class GateCredential:
    """The cloud-facing view of the shared cache — with its provenance.

    Separate from :func:`resolve_operator_token` on purpose. That resolver
    answers "what proves me to THIS MACHINE" and its answer is validated
    against ``identity_tokens``; this one answers "what proves me to
    CODENEXUS", and the two are not interchangeable. Collapsing them is the
    bug this type exists to end: a local session token handed to the cloud
    earns an opaque 401 that reads identically to a revoked operator.
    """

    reason: str
    token: str = ""
    #: The AUTHORIZATION stamp (#1000): when the authority last accepted this
    #: credential, and whether that answer is now older than
    #: :data:`AUTHZ_RECHECK_SECONDS`. Informational for OK rows only — a
    #: due recheck never empties `token`; the caller's live contact IS the
    #: recheck, and :func:`record_gate_answer` files its result.
    verified_at: str = ""
    recheck_due: bool = False
    #: CAN THIS MACHINE GET A NEW BEARER WITHOUT A HUMAN (#1000). False means
    #: the row holds no refresh token, so a lapse really does require the
    #: Dashboard's browser flow — and the caller can say so before it lapses
    #: rather than discovering it at the moment every unattended path dies.
    renewable: bool = False
    #: The SCOPE the cached bearer was minted with, as the authority stated it.
    #: The baseline the hourly recheck compares its fresh answer against.
    scope: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.token)


def cached_gate_credential(cache_path: str | Path | None = None) -> GateCredential:
    """What this machine can present to CODENEXUS, and why not, when it can't.

    Four outcomes, deliberately distinct:

    ``GATE_CRED_OK``
        a gate-issued token was recorded at login; use it.
    ``GATE_CRED_LOCAL_ONLY``
        signed in, unexpired, and holding nothing the cloud ever issued. The
        operator's session is REAL — it just is not a cloud session, and
        saying so here is what turns a 401 into an answerable question.
    ``GATE_CRED_UNKNOWN_VINTAGE``
        a row written before the issuer label existed. ABSENCE IS ABSENCE
        (#627): it is not evidence of either kind, so the token is still
        offered — behaviour for existing installs is unchanged — while the
        ambiguity stops being silent.
    ``GATE_CRED_EXPIRED``
        a gate token is recorded and its OWN clock has run out. Distinct from
        LOCAL_ONLY because the remedy differs: this operator did reach the
        cloud once, and needs a refresh, not a first sign-in.
    ``GATE_CRED_ABSENT``
        nobody is signed in.
    """
    row = read_cache(cache_path)
    if not row:
        return GateCredential(reason=GATE_CRED_ABSENT)

    gate_token = str(row.get("gate_token") or "").strip()
    if gate_token:
        # THE ROW'S `expires_at` DESCRIBES THE LOCAL TOKEN, NOT THIS ONE.
        # Measured 2026-08-31 from the Dashboard's own webview store: gate
        # access tokens live about an HOUR (09:14:50 -> 10:14:50 on the
        # operator's last sign-in), while the local session token carries the
        # 30-day life. Letting the gate token inherit the row's expiry would
        # present an hour-old corpse as fresh for a month — the same
        # mismatched-validity defect this accessor exists to end, one level
        # down. So it carries its OWN expiry or none at all.
        gate_expiry = str(row.get("gate_token_expires_at") or "").strip()
        if gate_expiry and gate_expiry <= _iso_now():
            # AN EXPIRED CREDENTIAL IS EXACTLY WHERE `renewable` MATTERS (#1000).
            # No token is offered — the bearer is dead — but whether this
            # machine can replace it WITHOUT A HUMAN is the difference between
            # a lapse the daemon recovers from and one that needs a browser.
            # Reporting it only on the healthy branch would answer the question
            # everywhere except where it is asked.
            return GateCredential(
                reason=GATE_CRED_EXPIRED,
                renewable=bool(str(row.get(GATE_REFRESH_KEY) or "").strip()),
                scope=parse_scope(row.get(GATE_SCOPE_KEY)),
            )
        # THE AUTHORITY ALREADY REFUSED THIS EXACT TOKEN (#992). Handing it out
        # again is the retry loop that banned the operator's machine three
        # times in one day: seven 401s trip the gate's CrowdSec
        # http-generic-401-bf and drop ports 80/443 for four hours — including
        # the sign-in that would replace the bad credential.
        #
        # Fingerprinted, not compared by value, so the latch is scoped to the
        # credential the gate actually disowned: writing a NEW gate token
        # (i.e. signing in again) changes the fingerprint and the row is usable
        # again with no explicit unlatch step. That is the way back in, and it
        # is why this can be sticky without becoming a lockout.
        if _rejection_matches(row, gate_token):
            return GateCredential(reason=GATE_CRED_REJECTED)
        # No recorded expiry is UNPROVEN, not proven-fresh (#627). It is still
        # offered because the consumer makes a LIVE call — the authority is
        # the real check, and refusing here would strand every row written
        # before the expiry was plumbed through.
        #
        # THE HOURLY RECHECK (#1000). Offered either way; what changes is
        # whether the caller's contact must count as a fresh permissions
        # answer. Absent stamp = never verified since this sign-in = due.
        verified_at = str(row.get(AUTHZ_STAMP_KEY) or "").strip()
        return GateCredential(
            reason=GATE_CRED_OK,
            token=gate_token,
            verified_at=verified_at,
            recheck_due=_authz_recheck_due(verified_at),
            renewable=bool(str(row.get(GATE_REFRESH_KEY) or "").strip()),
            scope=parse_scope(row.get(GATE_SCOPE_KEY)),
        )

    if "issuer" not in row:
        return GateCredential(
            reason=GATE_CRED_UNKNOWN_VINTAGE,
            token=str(row.get("token") or "").strip(),
        )

    # The label is present and says nothing vouched for by the gate. Offering
    # the local token here would be the original defect.
    return GateCredential(reason=GATE_CRED_LOCAL_ONLY)


def _token_fingerprint(token: str) -> str:
    """A stable, non-reversible handle for a credential.

    The latch must identify WHICH token was refused without storing a second
    copy of it: the cache file is already the one credential-bearing artifact
    on the box, and writing the same secret twice widens the blast radius of
    every read that touches this row for no benefit.
    """
    import hashlib

    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def _rejection_matches(row: dict, gate_token: str) -> bool:
    """True when the row's latch names the credential we are about to offer."""
    latch = row.get("gate_token_rejected")
    if not isinstance(latch, dict):
        return False
    return str(latch.get("fingerprint") or "") == _token_fingerprint(gate_token)


def _authz_recheck_due(verified_at: str) -> bool:
    """True when the last accepted answer is absent or older than the window."""
    if not verified_at:
        return True
    floor = (datetime.now(UTC) - timedelta(seconds=AUTHZ_RECHECK_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return verified_at <= floor


def _atomic_write(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` through a PRIVATE temp file + rename.

    The temp name is unique per writer (`tempfile` in the same directory),
    never the shared ``<name>.tmp`` sibling: `write_cache` (a sign-in), the
    stamp and the latch can all be mid-write at once — sitter thread, probe,
    CLI — and one fixed sibling name means one writer truncating another's
    half-written payload before it is renamed into place. Raises OSError.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _load_row_with_gate_token(path: Path) -> tuple[dict, bytes] | None:
    """The raw cache row iff it carries a gate token, WITH the exact bytes it
    was parsed from — the snapshot :func:`_rewrite_row` compares against
    before it replaces anything. No prune, no expiry check: the stamp/latch
    writers must not delete a row as a side effect."""
    try:
        raw = path.read_bytes()
        row = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    if not str(row.get("gate_token") or "").strip():
        return None
    return row, raw


def _rewrite_row(path: Path, row: dict, loaded_from: bytes) -> bool:
    """Compare-and-swap rewrite. Best-effort; False when it did not land.

    THE ROW MAY HAVE MOVED UNDER US. Between the read and this replace a
    fresh sign-in may have written a NEW credential, or a #992 latch may have
    condemned this one; blindly replacing the file would revert either —
    the operator signs in and the old token comes back, or a refused token
    is un-latched and presented again (the CrowdSec ban). So the file is
    re-read just before the rename and the write is ABANDONED when its bytes
    no longer match what ``row`` was built from. The remaining window is the
    rename itself, not the whole HTTP round trip that preceded it.
    """
    try:
        if path.read_bytes() != loaded_from:
            return False
        _atomic_write(path, row)
    except OSError:
        return False
    return True


def stamp_gate_credential_verified(
    *,
    verified_at: str = "",
    cache_path: str | Path | None = None,
) -> bool:
    """Record that the AUTHORITY ACCEPTED the gate credential just now (#1000).

    The write side of the hourly recheck. Any cloud call that came back 200
    is a fresh permissions answer — the gate does not execute a tool for a
    credential it has revoked — so the caller files it here and the next hour
    of contacts need not be treated as unverified. ``verified_at`` is for
    tests and replay; production callers pass nothing and get "now".

    Does NOT clear a rejection latch. A latch is scoped to a fingerprint and
    only a new sign-in changes it; a stamp is a weaker claim and must not
    outrank a recorded refusal. Best-effort, never raising; True iff written.
    """
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    loaded = _load_row_with_gate_token(path)
    if loaded is None:
        return False
    row, raw = loaded
    explicit = str(verified_at or "").strip()
    if not explicit:
        # DEBOUNCE. A stamp minutes old already says what this one would; the
        # rewrite is pure race window (see AUTHZ_STAMP_DEBOUNCE_SECONDS).
        current = str(row.get(AUTHZ_STAMP_KEY) or "").strip()
        floor = (
            datetime.now(UTC) - timedelta(seconds=AUTHZ_STAMP_DEBOUNCE_SECONDS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if current and current > floor:
            return False
    row[AUTHZ_STAMP_KEY] = explicit or _iso_now()
    return _rewrite_row(path, row, raw)


def record_gate_answer(
    *,
    status: int,
    request_id: str = "",
    cache_path: str | Path | None = None,
) -> bool:
    """File ONE authority answer against the cached credential (#1000).

    The single door every cloud-facing caller uses after a live contact:

    * 200            -> :func:`stamp_gate_credential_verified`
    * 401 / 403      -> :func:`latch_gate_credential_rejected` (#992); the
                        latch wins over any stamp, so revocation is immediate
    * anything else  -> nothing. A 429, a 5xx or a timeout says nothing about
                        the CREDENTIAL; latching would lock the operator out
                        on the gate's own rate limit, stamping would vouch
                        for a permission nobody checked.

    Never retries, never raises. Returns True iff the row changed.
    """
    code = int(status or 0)
    if code == 200:
        return stamp_gate_credential_verified(cache_path=cache_path)
    if code in (401, 403):
        return latch_gate_credential_rejected(
            status=code, request_id=request_id, cache_path=cache_path
        )
    return False


def latch_gate_credential_rejected(
    *,
    status: int,
    request_id: str = "",
    reason: str = "",
    cache_path: str | Path | None = None,
) -> bool:
    """Record that the AUTHORITY refused the gate credential in the cache row.

    THE RETRY LOOP THAT HAD NO `for`. Nothing in AIDOCS retried a rejected
    credential in a loop; it simply re-presented the same one on every process
    spawn, because the only freshness memory was in-process and the Tauri
    kernel spawns a fresh python per invoke. Seven of those and the gate's
    CrowdSec `http-generic-401-bf` bans the source IP for four hours — which
    also blocks the sign-in that would fix it. Observed three times in one day
    (#992).

    WHY A 401 LATCHES BUT DOES NOT REVOKE. These are different claims and the
    distinction is load-bearing (#529 step 1): "this credential was refused" is
    a fact about a token, and it is what we record here; "this operator is out"
    is a fact about a person, needs the gate-side membership fingerprint that
    does not exist yet, and would execute `invalidate_operator`. Latching costs
    the operator one click; inferring revocation costs them their session.

    Best-effort and never raising: a latch that could not be written must not
    fail the caller's real work. Returns True iff the row was updated.
    """
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    loaded = _load_row_with_gate_token(path)
    if loaded is None:
        return False
    row, raw = loaded
    row["gate_token_rejected"] = _latch_record(
        str(row.get("gate_token") or "").strip(),
        status=status,
        request_id=request_id,
        reason=reason,
    )
    return _rewrite_row(path, row, raw)


#: The default latch cause: the authority answered 401/403 to a live request.
LATCH_AUTHORITY_REJECTED = "authority_rejected"


def _latch_record(
    token: str, *, status: int, request_id: str = "", reason: str = ""
) -> dict:
    """The #992 latch payload for one credential, fingerprinted not copied."""
    return {
        "fingerprint": _token_fingerprint(token),
        "status": int(status),
        "request_id": str(request_id or ""),
        "reason": str(reason or LATCH_AUTHORITY_REJECTED),
        "rejected_at": _iso_now(),
    }


def parse_scope(raw: object) -> tuple[str, ...]:
    """A scope string (space- or comma-delimited) as a sorted, de-duplicated
    tuple. The authority states scope as OAuth does — one string — and every
    comparison here is a SET question, so parsing it once at the boundary is
    what keeps "did the permissions change" from becoming a string diff."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = str(raw or "").replace(",", " ").split()
    return tuple(sorted({p.strip() for p in parts if p.strip()}))


def renewal_material(cache_path: str | Path | None = None) -> dict:
    """What an UNATTENDED renewal has to work with, read from the shared row.

    Returns ``{}`` when there is no signed-in row at all. Otherwise the refresh
    token, the client it was issued to and the scope the cached bearer carries
    — deliberately separate from :func:`cached_gate_credential`, which answers
    "what may I present"; this answers "what may I present it FOR REPLACEMENT
    with", and the two have different failure modes (an EXPIRED bearer offers
    no token and is still perfectly renewable).
    """
    row = read_cache(cache_path)
    if not row:
        return {}
    return {
        "refresh_token": str(row.get(GATE_REFRESH_KEY) or "").strip(),
        "client_id": str(row.get(GATE_CLIENT_KEY) or "").strip(),
        "scope": parse_scope(row.get(GATE_SCOPE_KEY)),
        "gate_token": str(row.get("gate_token") or "").strip(),
        "attempted_at": str(row.get(RENEWAL_ATTEMPT_KEY) or "").strip(),
        "last_reason": str(row.get(RENEWAL_REASON_KEY) or "").strip(),
    }


def renewal_attempt_due(cache_path: str | Path | None = None) -> bool:
    """True when no renewal has been ATTEMPTED inside the current window.

    NEVER A RETRY LOOP (#992). One attempt per recheck window, and the memory
    is on disk because the process that would remember it does not survive:
    the Tauri kernel spawns a fresh python per invoke, so an in-process guard
    guards nothing. Seven refused attempts trip the gate's CrowdSec
    `http-generic-401-bf` and ban the machine for four hours — including the
    sign-in that would fix it.
    """
    row = read_cache(cache_path)
    if not row:
        return True
    attempted = str(row.get(RENEWAL_ATTEMPT_KEY) or "").strip()
    if not attempted:
        return True
    floor = (datetime.now(UTC) - timedelta(seconds=AUTHZ_RECHECK_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return attempted <= floor


def record_renewal_attempt(
    *,
    reason: str,
    attempted_at: str = "",
    cache_path: str | Path | None = None,
) -> bool:
    """Record THAT a renewal was attempted, and how it went. Best-effort.

    Filed for every attempt, successful or not: it is the budget, not a
    failure log. ``attempted_at`` is for tests and replay.
    """
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    loaded = _load_row_with_gate_token(path)
    if loaded is None:
        return False
    row, raw = loaded
    row[RENEWAL_ATTEMPT_KEY] = str(attempted_at or "").strip() or _iso_now()
    row[RENEWAL_REASON_KEY] = str(reason or "")
    return _rewrite_row(path, row, raw)


def store_renewed_gate_credential(
    *,
    access_token: str,
    expires_at: str = "",
    refresh_token: str = "",
    scope: object = "",
    revoked_reason: str = "",
    cache_path: str | Path | None = None,
) -> dict:
    """Install a gate credential the AUTHORITY just re-issued, and report what
    changed about the operator's permissions while doing it (#1000).

    ONE compare-and-swap write carries all of it — the new bearer, its clock,
    the ROTATED refresh token (the gate consumes the presented one, so keeping
    the spent copy would make the next renewal impossible), the fresh authz
    stamp, and the scope diff. Splitting it would open the same window
    :func:`_rewrite_row` exists to close, three times over.

    A REMOVED permission latches the SUPERSEDED credential (#992). That bearer
    was minted carrying an authority its holder no longer has, so nothing may
    present it again; the latch names the old fingerprint, which is why the
    narrower credential written in the same breath is immediately usable. A
    narrowing is not a lockout.

    Returns a receipt: ``stored``, ``scope_changed``, ``scope_added``,
    ``scope_removed``. ``stored`` False means a concurrent writer moved the row
    (a fresh sign-in, a latch) and this renewal was abandoned rather than
    reverting them.
    """
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    loaded = _load_row_with_gate_token(path)
    if loaded is None:
        return {"stored": False, "scope_changed": False, "scope_added": (), "scope_removed": ()}
    row, raw = loaded
    superseded = str(row.get("gate_token") or "").strip()
    before = parse_scope(row.get(GATE_SCOPE_KEY))
    after = parse_scope(scope)
    # AN UNSTATED SCOPE IS NOT AN EMPTY ONE (#627). A gate that answered
    # without a `scope` field said nothing about permissions; treating that
    # silence as "everything was revoked" would latch a working credential.
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    if after:
        added = tuple(sorted(set(after) - set(before)))
        removed = tuple(sorted(set(before) - set(after)))
        row[GATE_SCOPE_KEY] = " ".join(after)

    row["gate_token"] = str(access_token)
    if expires_at:
        row["gate_token_expires_at"] = str(expires_at)
    else:
        row.pop("gate_token_expires_at", None)
    if refresh_token:
        row[GATE_REFRESH_KEY] = str(refresh_token)
    row["issuer"] = ISSUER_CODENEXUS
    # The authority just answered about THIS operator's permissions. That is
    # the hourly recheck, and this is its stamp.
    row[AUTHZ_STAMP_KEY] = _iso_now()
    row[RENEWAL_ATTEMPT_KEY] = _iso_now()
    row[RENEWAL_REASON_KEY] = "renewed"
    changed = bool(added or removed)
    if changed:
        row[SCOPE_CHANGED_KEY] = _iso_now()
        row[SCOPE_ADDED_KEY] = list(added)
        row[SCOPE_REMOVED_KEY] = list(removed)
    if removed:
        # WHY it was condemned is the caller's to name: this function knows a
        # scope shrank, but the vocabulary for a latch cause belongs to the
        # door that files it, so the reason travels in rather than being
        # invented here.
        row["gate_token_rejected"] = _latch_record(
            superseded, status=403, reason=revoked_reason
        )
    else:
        # The renewal replaced the credential the old latch named; carrying it
        # forward would condemn a token the authority just minted.
        row.pop("gate_token_rejected", None)
    return {
        "stored": _rewrite_row(path, row, raw),
        "scope_changed": changed,
        "scope_added": added,
        "scope_removed": removed,
    }


#: Where a probe answer is remembered ACROSS PROCESSES, beside the credential
#: it was asked about. See :func:`read_probe_memo`.
def probe_memo_path(cache_path: str | Path | None = None) -> Path:
    base = Path(cache_path) if cache_path is not None else default_cache_path()
    return base.with_name(base.name + ".probe.json")


def read_probe_memo(
    key: str, cache_path: str | Path | None = None
) -> dict | None:
    """The last authority answer for ``key``, if it is still fresh.

    WHY THIS EXISTS ON DISK. The in-process freshness cache was correct for a
    long-lived server and useless for the shape that actually calls this: the
    Tauri dashboard spawns a FRESH python CLI per invoke, so a 300-second
    throttle became one cloud round trip per authentication check. That is what
    turned a stale credential into a four-hour IP ban, and — once banned — a
    dropped port into a ten-second stall on every auth check, backed up behind
    the kernel's single spawn gate.

    The old note here said a probe is "evidence about a moment, not state to
    persist… a restart should ask again, not inherit a verdict it cannot date."
    The objection is answered rather than overruled: the memo carries its own
    ``observed_at``, so nothing inherits an undateable verdict — it inherits a
    dated one, and refuses it once stale.
    """
    memo_path = probe_memo_path(cache_path)
    try:
        blob = json.loads(memo_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    memo = blob.get(str(key))
    if not isinstance(memo, dict):
        return None
    if str(memo.get("fresh_until") or "") <= _iso_now():
        return None
    return memo


def write_probe_memo(
    key: str,
    memo: dict,
    fresh_seconds: int,
    cache_path: str | Path | None = None,
) -> bool:
    """Persist one probe answer under ``key``. Best-effort; never raises."""
    memo_path = probe_memo_path(cache_path)
    try:
        blob = json.loads(memo_path.read_text(encoding="utf-8"))
        if not isinstance(blob, dict):
            blob = {}
    except (OSError, ValueError):
        blob = {}
    stamped = dict(memo)
    stamped["fresh_until"] = (
        datetime.now(UTC) + timedelta(seconds=int(fresh_seconds))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    blob[str(key)] = stamped
    try:
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = memo_path.with_name(memo_path.name + ".tmp")
        tmp.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, memo_path)
    except OSError:
        return False
    return True


def read_cache(cache_path: str | Path | None = None) -> dict | None:
    """Read the cached token row, or None. EXPIRED rows are deleted on
    read (the prune contract) so a stale token never lingers on disk."""
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(row, dict) or not str(row.get("token") or "").strip():
        return None
    expires_at = str(row.get("expires_at") or "")
    # ISO-8601 UTC "%Y-%m-%dT%H:%M:%SZ" compares lexicographically.
    if not expires_at or expires_at <= _iso_now():
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return row


def clear_cache(cache_path: str | Path | None = None) -> bool:
    """Remove the cached token file (logout hygiene). True iff removed."""
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    try:
        path.unlink()
        return True
    except OSError:
        return False


def resolve_operator_token(
    args: list[str] | None = None,
    *,
    env_var: str = ENV_VAR,
    flag: str = FLAG,
    cache_path: str | Path | None = None,
) -> tuple[str, str]:
    """THE single token-resolution door. Returns ``(token, source)`` with
    source in {'env', 'flag', 'cache', ''}. Never raises."""
    tok = str(os.environ.get(env_var) or "").strip()
    if tok:
        return tok, "env"
    argv = list(args or [])
    if flag in argv:
        try:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                tok = str(argv[idx + 1] or "").strip()
                if tok:
                    return tok, "flag"
        except Exception:
            pass
    try:
        row = read_cache(cache_path)
    except Exception:
        row = None
    if row:
        return str(row["token"]).strip(), "cache"
    return "", ""
