"""UNATTENDED gate-credential renewal + the hourly permission recheck (#1000).

OPERATOR DIRECTIVE, verbatim 2026-09-04: "i asked you for the sign in token to
be smarter, just refresh every 1 hour, not make me relog every hour. on refresh
it needs to check if any perms changed".

WHAT WAS ACTUALLY BROKEN, measured on 2026-09-04 by reading the cache row:

    token       (local session)  expires_at = 2026-10-03   30 days, valid
    gate_token  (ogt-prefixed)   gate_token_expires_at     ~1 hour, dead
    gate_authz_verified_at       2026-09-03T21:35:10Z
    REFRESH CREDENTIAL           absent - no key containing "refresh" existed

One row, two clocks: the local half had a month left while the cloud half died
in an hour, and because nothing on disk could replace the cloud half, the ONLY
way back was a human at the Dashboard's browser flow. It lapsed four times in
one working session and each lapse hard-blocked every cloud path - XAACP, the
backlog sync, entitlement checks - including the ones with nobody watching:
the sitter's reconcile, lane workers, any background job.

WHY THE OAUTH REFRESH GRANT, AND NOT THE OTHER TWO CANDIDATES
-------------------------------------------------------------
(a) CHOSEN - the OAuth refresh grant. The gate has ALREADY minted a refresh
    credential on every authorization_code exchange since #92:
    `outer_gate_oauth` returns an `ogr`-prefixed one from both grants,
    `/.well-known` advertises the refresh grant in `grant_types_supported`,
    `OuterGateTokenStore.mint_refresh` gives it a 30-DAY TTL
    (`_DEFAULT_REFRESH_TTL_SECONDS`), and `rotate_refresh` consumes it
    single-use on redemption. The Tauri kernel even reads it out of the token
    response and hands it to the webview, where `webAuth.renewIfNeeded` has
    been silently renewing the BROWSER's session with it all along. The only
    thing missing was persisting it beside the credential the DAEMON reads. So
    this needs no new gate endpoint, no new grant and no new trust
    relationship - the mechanism the operator is asking for already exists and
    was being thrown away one function call from where it was needed.

(b) REJECTED - minting a gate credential from the still-valid 30-day LOCAL
    session token. There is no such exchange, and building one would be the
    exact defect #990 exists to end: the local `token` is an `identity_tokens`
    row this machine issued to itself, and the cloud has never seen it. Letting
    it buy a cloud credential would make a local file an identity authority - a
    second authority (#972), and a worse one than a path, since anyone who can
    write the file could mint. `login_with_codenexus_principal` runs the other
    way round on purpose: the GATE attests, then the local token is stamped.

(c) REJECTED for now - a device / service principal. It is the right answer for
    a truly headless install with no human ever, and #1000's spec section keeps
    it. But it is a NEW credential kind with its own issuance, revocation and
    audit story on the gate side, and the operator's complaint is about THEIR
    session dying hourly. A service principal would not fix that: it would run
    the sitter as somebody else, and the permission recheck the operator asked
    for would then be checking the wrong principal's permissions.

THE RECHECK IS THE RENEWAL
--------------------------
"On refresh it needs to check if any perms changed." The refresh response
carries `scope` - the authority's fresh statement of what this operator may do
- so the renewal IS the permission answer, not a second call after it. The
scope is compared against what the cached bearer was minted with; a change is
persisted, dated and logged; and a REMOVED permission latches the superseded
credential (#992) so the wider bearer can never be presented again.

ONE ATTEMPT PER WINDOW, NEVER A LOOP. A refused renewal is recorded on disk
(the process that would remember it does not survive - the kernel spawns a
fresh python per invoke) and the credential is reported unusable with a reason
naming the remedy. Seven refusals would trip the gate's CrowdSec
`http-generic-401-bf` and ban the machine for four hours, including the sign-in
that would fix it. That is #992, and it is not repeated here.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from .operator_token_resolution import (
    GATE_CRED_EXPIRED,
    GATE_CRED_OK,
    GATE_CRED_REJECTED,
    GATE_CRED_REMEDY,
    GateCredential,
    cached_gate_credential,
    latch_gate_credential_rejected,
    record_renewal_attempt,
    renewal_attempt_due,
    renewal_material,
    store_renewed_gate_credential,
)

log = logging.getLogger(__name__)

#: Outcomes, deliberately discriminated. "Could not renew" spans an offline
#: moment, a spent refresh credential, a revoked operator and a row that never
#: had anything to renew from - four different next actions, and only one of
#: them is "sign in again".
RENEWAL_RENEWED = "renewed"
RENEWAL_NOT_DUE = "not_due"
RENEWAL_NOT_APPLICABLE = "no_gate_session"
RENEWAL_NO_REFRESH = "no_refresh_credential"
RENEWAL_ALREADY_ATTEMPTED = "attempted_this_window"
RENEWAL_LATCHED = "credential_latched"
RENEWAL_UNREACHABLE = "authority_unreachable"
RENEWAL_REFUSED = "renewal_refused"
RENEWAL_REJECTED = "credential_rejected"

#: The latch cause a permission REMOVAL writes (read back by callers and tests).
LATCH_PERMISSION_REVOKED = "permission_revoked"

#: Named remedy for a refusal the daemon cannot resolve on its own. Law
#: 311bf3e6: the door a refusal names must actually open.
SIGN_IN_REMEDY = (
    "sign in again through the Dashboard's CodeNexus browser flow - the stored "
    "refresh credential can no longer buy a bearer, and no unattended path can "
    "replace it"
)

#: The refresh grant asks for exactly one thing, once. Longer than this would
#: stall every caller behind a dead gate; shorter would fail a renewal on a
#: slow link and burn the window's single attempt.
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class RenewalOutcome:
    """What the renewal door did, and what the caller may present afterwards."""

    credential: GateCredential
    renewed: bool = False
    reason: str = ""
    detail: str = ""
    scope_changed: bool = False
    scope_added: tuple[str, ...] = field(default_factory=tuple)
    scope_removed: tuple[str, ...] = field(default_factory=tuple)


def token_endpoint(project_root: Path | str | None = None) -> str:
    """The gate's OAuth token endpoint, from the SAME setting every other cloud
    caller reads (`sync.vps_hub_url`, else the default authority)."""
    from .xaacp_authority import DEFAULT_GATE_URL

    base = DEFAULT_GATE_URL
    if project_root is not None:
        try:
            from .config import get_setting

            base = str(
                get_setting(
                    "sync.vps_hub_url",
                    project_root=Path(project_root),
                    default=DEFAULT_GATE_URL,
                )
                or DEFAULT_GATE_URL
            )
        except Exception:  # noqa: BLE001 - config trouble => the default authority
            base = DEFAULT_GATE_URL
    base = base.rstrip("/")
    if base.endswith("/v1/mcp"):
        base = base[: -len("/v1/mcp")]
    return base + "/oauth/token"


def _json_or_empty(raw: object) -> dict:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw or "")
        parsed = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _post_form(url: str, form: dict, timeout: float) -> tuple[int, dict]:
    """POST the refresh grant, governed. Returns ``(status, body)``.

    An HTTP ERROR is an ANSWER, not a transport failure: the gate's 400
    `invalid_grant` and its 401 `invalid_client` are exactly what this door
    must distinguish, so `HTTPError` is unwrapped into a status here and only
    real transport trouble is allowed to raise.
    """
    from .governed_egress import assert_egress_allowed

    host = urllib.parse.urlparse(url).hostname or ""
    assert_egress_allowed(url, purpose="gate_credential_renewal", allow_hosts=[host])
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "aidocs-gate-credential-renewal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 governed above
            return int(resp.status or 0), _json_or_empty(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = _json_or_empty(exc.read())
        except Exception:  # noqa: BLE001 - a diagnostic must not raise
            body = {}
        return int(exc.code), body


def _expiry_from(body: dict) -> str:
    """The renewed bearer's OWN clock from `expires_in`, or nothing.

    ABSENT STAYS ABSENT (#627): a gate that did not say gets no guessed
    default, and the row records an unproven expiry rather than a fictional
    one - the live call remains the real check.
    """
    try:
        seconds = int(body.get("expires_in") or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_gate_credential(
    *,
    project_root: Path | str | None = None,
    cache_path: str | Path | None = None,
    http: Callable[[str, dict, float], tuple[int, dict]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> RenewalOutcome:
    """THE smart-credential door: hand back a usable gate bearer, renewing it
    without a browser when it has lapsed or its hourly recheck has fallen due.

    Never raises. Never retries. Every path leaves the row in a state the next
    process can read and act on, because the next process is usually a
    different one.
    """
    cred = cached_gate_credential(cache_path)

    # A REFUSED CREDENTIAL IS NOT RENEWED (#992). Its refresh credential would
    # be a way around the latch, and the latch exists precisely to stop this
    # machine re-presenting what the authority disowned.
    if cred.reason == GATE_CRED_REJECTED:
        return RenewalOutcome(
            credential=cred,
            reason=RENEWAL_LATCHED,
            detail=(
                "the authority refused this credential and the refusal is on "
                f"record; {SIGN_IN_REMEDY}"
            ),
        )

    if cred.reason == GATE_CRED_OK and not cred.recheck_due:
        # Fresh answer, live bearer. Renewal is not a heartbeat.
        return RenewalOutcome(credential=cred, reason=RENEWAL_NOT_DUE)

    if cred.reason not in (GATE_CRED_OK, GATE_CRED_EXPIRED):
        return RenewalOutcome(
            credential=cred,
            reason=RENEWAL_NOT_APPLICABLE,
            detail=f"nothing cloud-issued to renew ({cred.reason}); {GATE_CRED_REMEDY}",
        )

    # `renewable` IS THIS QUESTION, asked of the accessor rather than
    # re-derived here: can this machine replace the bearer without a human.
    # Branching on it keeps one answer in one place — a caller that wants to
    # warn BEFORE the lapse reads the same flag and gets the same verdict.
    if not cred.renewable:
        return RenewalOutcome(
            credential=cred,
            reason=RENEWAL_NO_REFRESH,
            detail=(
                "this machine holds nothing it can exchange for a new bearer, so "
                f"no unattended path can replace the credential - {GATE_CRED_REMEDY}. "
                "A sign-in from a current build stores one and this stops being true."
            ),
        )

    material = renewal_material(cache_path)
    presented = str(material.get("refresh_token") or "")

    if not renewal_attempt_due(cache_path):
        return RenewalOutcome(
            credential=cred,
            reason=RENEWAL_ALREADY_ATTEMPTED,
            detail=(
                "a renewal was already attempted in this window and is not "
                f"retried (last outcome: {material.get('last_reason') or 'unknown'}); "
                f"{SIGN_IN_REMEDY} if it keeps failing"
            ),
        )

    url = token_endpoint(project_root)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": presented,
        "client_id": str(material.get("client_id") or ""),
    }
    caller = http or _post_form
    try:
        status, body = caller(url, form, timeout)
    except Exception:  # noqa: BLE001 - an unanswered question is never an answer
        # OFFLINE IS NOT A PERMISSION VERDICT (#508/#509). The bearer we hold is
        # untouched; only the attempt is recorded, so the next window tries.
        record_renewal_attempt(reason=RENEWAL_UNREACHABLE, cache_path=cache_path)
        log.info("gate credential renewal: authority unreachable; credential left as-is")
        return RenewalOutcome(
            credential=cached_gate_credential(cache_path),
            reason=RENEWAL_UNREACHABLE,
            detail=(
                "could not reach the authority to renew; this says nothing about "
                "your permissions and the next window will try again"
            ),
        )

    if status in (401, 403):
        # The authority disowned the pair. That IS a verdict about a credential.
        latch_gate_credential_rejected(
            status=status,
            request_id=str(body.get("request_id") or ""),
            cache_path=cache_path,
        )
        record_renewal_attempt(reason=RENEWAL_REJECTED, cache_path=cache_path)
        log.warning("gate credential renewal: authority rejected the credential (%s)", status)
        return RenewalOutcome(
            credential=cached_gate_credential(cache_path),
            reason=RENEWAL_REJECTED,
            detail=(
                f"the authority rejected this credential ({status}: "
                f"{body.get('error') or 'no reason given'}); {SIGN_IN_REMEDY}"
            ),
        )

    granted = str(body.get("access_token") or "").strip()
    if status != 200 or not granted:
        record_renewal_attempt(reason=RENEWAL_REFUSED, cache_path=cache_path)
        log.warning(
            "gate credential renewal refused (%s: %s)",
            status,
            body.get("error") or "no credential in response",
        )
        return RenewalOutcome(
            credential=cached_gate_credential(cache_path),
            reason=RENEWAL_REFUSED,
            detail=(
                f"the authority refused the renewal ({status}: "
                f"{body.get('error') or 'no credential in response'}). {SIGN_IN_REMEDY}"
            ),
        )

    receipt = store_renewed_gate_credential(
        access_token=granted,
        expires_at=_expiry_from(body),
        refresh_token=str(body.get("refresh_token") or ""),
        scope=body.get("scope") or "",
        # WHY a superseded bearer gets condemned when the scope shrinks. The
        # store knows THAT a permission went away; this door owns the
        # vocabulary for saying so on the #992 latch, and the constant exists
        # to be read back by whoever inspects the row later.
        revoked_reason=LATCH_PERMISSION_REVOKED,
        cache_path=cache_path,
    )
    if not receipt["stored"]:
        # A concurrent sign-in or latch moved the row. Abandoned, not forced -
        # reverting either is the bug `_rewrite_row` exists to prevent.
        record_renewal_attempt(reason=RENEWAL_REFUSED, cache_path=cache_path)
        return RenewalOutcome(
            credential=cached_gate_credential(cache_path),
            reason=RENEWAL_REFUSED,
            detail="the cache row changed under the renewal; the newer row was kept",
        )

    added = tuple(receipt["scope_added"])
    removed = tuple(receipt["scope_removed"])
    # NAME BOTH SETS. "Permissions changed" is not a finding an operator can
    # act on; the before and the after are.
    was = ", ".join(cred.scope) or "none recorded"
    now = ", ".join(cached_gate_credential(cache_path).scope) or "none recorded"
    if removed:
        log.warning(
            "gate credential renewal: PERMISSIONS REVOKED %s (granted: %s) - was [%s], "
            "now [%s]; the superseded credential is latched",
            ", ".join(removed),
            ", ".join(added) or "none",
            was,
            now,
        )
    elif added:
        log.info(
            "gate credential renewal: permissions granted %s - was [%s], now [%s]",
            ", ".join(added),
            was,
            now,
        )
    else:
        log.debug("gate credential renewal: permissions unchanged [%s]", now)

    if receipt["scope_changed"]:
        detail = "permissions changed: " + " ".join(
            part
            for part in (
                ("+" + ",".join(added)) if added else "",
                ("-" + ",".join(removed)) if removed else "",
            )
            if part
        )
    else:
        detail = "renewed with unchanged permissions"

    return RenewalOutcome(
        credential=cached_gate_credential(cache_path),
        renewed=True,
        reason=RENEWAL_RENEWED,
        detail=detail,
        scope_changed=bool(receipt["scope_changed"]),
        scope_added=added,
        scope_removed=removed,
    )
