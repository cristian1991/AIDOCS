"""THE shape of every bearer secret AIDOCS ISSUES: one authority, one format.

ISSUED vs CALLER-SUPPLIED. Only a secret AIDOCS itself generates is shaped. A
caller-supplied secret keeps the caller's format and is stored by hash like any
other: an OAuth client_secret passed to ``OAuthStore.register_client`` (only the
generated default is shaped), and the Standard Webhooks ``whsec_`` secret a
subscriber hands the gate on events/subscribe (mcp_events_secret). Scanners find
those by their own vendors' rules, not by ``aidocs_``.

    aidocs_<kind>_<payload><checksum>

  FAMILY_PREFIX  ``aidocs_``. Vendor-unique, so one scanner rule covers every
                 kind, the way ``ghp_``/``gho_`` (GitHub), ``xoxb-`` (Slack) and
                 ``sk_live_`` (Stripe) make their tokens findable by anyone.
  kind           two lowercase letters, see KINDS.
  payload        PAYLOAD_BYTES (32) of ``secrets.token_bytes``, base62,
                 zero-padded to exactly 43 characters (62**43 > 2**256).
  checksum       CRC32 of the ASCII text before it (prefix, kind and payload),
                 base62, zero-padded to exactly 6 characters (62**6 > 2**32).

Fixed TOKEN_LENGTH 59. Base62 has no ``-`` or ``_``, so the token is one word to
every regex engine and a double click selects all of it.

WHY A CHECKSUM. A scanner can confirm a match offline with near-zero false
positives (a random 59-char lookalike passes with odds about 1 in 2**32), and
AIDOCS rejects a mistyped or forged token before any database lookup
(``admissible``).

BACKWARD COMPATIBILITY. Every validator finds a token by the hash of the whole
presented string, so the lookup is format-agnostic. ``admissible`` refuses ONLY
a string that claims the family prefix and fails the shape; any other string
(an ``ogt_``-style token or an unprefixed operator token minted before this
module) goes on to the hash lookup exactly as before, and keeps working until
it expires or is rotated. LEGACY_PREFIX records what each kind used to look
like, for the validators that gate on a prefix.

The detector regex (SCANNER_REGEX) is the one shipped to gitleaks and
trufflehog in docs/security/token-scanners/ (custom rules, not part of either
scanner's defaults: load them with --config); output_guard runs it too.
"""

from __future__ import annotations

import secrets
import zlib
from types import MappingProxyType

FAMILY_PREFIX = "aidocs_"
PAYLOAD_BYTES = 32
PAYLOAD_LENGTH = 43
CHECKSUM_LENGTH = 6
TOKEN_LENGTH = len(FAMILY_PREFIX) + 2 + 1 + PAYLOAD_LENGTH + CHECKSUM_LENGTH  # 59

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ALPHABET_SET = frozenset(_ALPHABET)
_HEAD = len(FAMILY_PREFIX) + 3  # "aidocs_" + kind + "_"

#: kind -> what it is and where it is minted.
KINDS = MappingProxyType(
    {
        "at": "gate access token (outer_gate_token_store._insert_access_row)",
        "rt": "gate refresh token (outer_gate_token_store._insert_refresh_row)",
        "cs": "OAuth client secret (outer_gate_oauth.register_client)",
        "ac": "OAuth authorization code (outer_gate_oauth.issue_code)",
        "wt": "sandbox worker token (outer_gate_sandbox.mint_worker_token)",
        "os": "operator session token (identity_store._issue_token_for_authenticated_user)",
        "ds": "deploy sign-link token (ai_deploy_states.new_sign_token)",
        "ps": "app pairing secret (app_packs.pairing.PairingService.issue_offer), crosses the CRM wire",
        "sc": "binding-seal challenge (app_packs.binding_seal.BindingSealService), crosses the CRM wire",
        "cr": "connect authz_request_id, the req (app_packs.connect.ConnectService.start), crosses the CRM wire",
        "cc": "connect browser cookie __Host-aidocs_atxn (app_packs.connect.ConnectService.start)",
        "ct": "connect continue token, the ret (app_packs.connect.ConnectService.chatgpt_connect)",
        "lt": "link confirmation ticket (app_packs.link.LinkService.authorize_page)",
        "lc": "link authorization code (app_packs.link.LinkService.authorize_confirm)",
    }
)

#: kind -> the prefix tokens of that kind carried BEFORE this shape ('' = none).
LEGACY_PREFIX = MappingProxyType(
    {
        "at": "ogt_", "rt": "ogr_", "cs": "ogc_", "ac": "ogac_", "wt": "ogwt_",
        "os": "", "ds": "",
        "ps": "",  # base64url of 32 bytes
        "sc": "",  # token_urlsafe(32)
        "cr": "", "cc": "", "ct": "",  # base64url of 32 bytes
        "lt": "", "lc": "",  # base64url of 32 bytes
    }
)

# WIRE CONSTRAINT: every kind is EXACTLY two lowercase letters, so every token is
# 59 chars. CAT read the deployed CRM: its RandomToken check (Trust.cs:255-257,
# :419-424) base64url-decodes the pairing secret, which a 59-char value passes
# (59 mod 4 = 3) and a 61-char one (a 4-letter kind) fails. Do not lengthen a kind.
if any(len(k) != 2 or not (k.isascii() and k.isalpha() and k.islower()) for k in KINDS):
    raise RuntimeError("token_shape kinds must be exactly two lowercase ASCII letters")

#: RE2- and Python-compatible; group 1 is the whole token. Compile with re.ASCII.
SCANNER_REGEX = r"\b(aidocs_[a-z]{2}_[0-9A-Za-z]{49})\b"


def _b62_encode(n: int, width: int) -> str:
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(_ALPHABET[r])
    return "".join(reversed(out)).rjust(width, "0")


def _b62_decode(digits: str) -> int:
    n = 0
    for ch in digits:
        n = n * 62 + _ALPHABET.index(ch)
    return n


def _checksum(body: str) -> str:
    return _b62_encode(zlib.crc32(body.encode("ascii")), CHECKSUM_LENGTH)


def prefix(kind: str) -> str:
    """``aidocs_<kind>_``; ValueError for an unknown kind."""
    if kind not in KINDS:
        raise ValueError(f"unknown AIDOCS secret kind {kind!r}")
    return f"{FAMILY_PREFIX}{kind}_"


def encode(kind: str, raw: bytes) -> str:
    """The family shape of ``kind`` over exactly PAYLOAD_BYTES caller-drawn random
    bytes: the seam for a service with an injected randomness source (pairing).
    ValueError for an unknown kind or anything but 32 bytes."""
    if type(raw) is not bytes or len(raw) != PAYLOAD_BYTES:
        raise ValueError(f"token_shape.encode needs exactly {PAYLOAD_BYTES} bytes")
    body = prefix(kind) + _b62_encode(int.from_bytes(raw, "big"), PAYLOAD_LENGTH)
    return body + _checksum(body)


def mint(kind: str) -> str:
    """A fresh secret of ``kind`` in the family shape. The ONE mint authority:
    every site calls it (or ``encode``) through the module (``token_shape.mint``)."""
    prefix(kind)  # refuse an unknown kind before drawing
    return encode(kind, secrets.token_bytes(PAYLOAD_BYTES))


def is_well_formed(token: object, kind: str | None = None) -> bool:
    """True iff ``token`` is in the family shape (of ``kind``, when given) and its
    checksum holds. Offline; touches nothing."""
    if type(token) is not str or len(token) != TOKEN_LENGTH or not token.startswith(FAMILY_PREFIX):
        return False
    k = token[len(FAMILY_PREFIX) : _HEAD - 1]
    if k not in KINDS or (kind is not None and k != kind) or token[_HEAD - 1] != "_":
        return False
    tail = token[_HEAD:]
    if not _ALPHABET_SET.issuperset(tail):
        return False
    if _b62_decode(tail[:PAYLOAD_LENGTH]) >= 2 ** (8 * PAYLOAD_BYTES):
        return False
    return secrets.compare_digest(_checksum(token[:-CHECKSUM_LENGTH]), token[-CHECKSUM_LENGTH:])


def admissible(token: object) -> bool:
    """The pre-lookup gate every validator runs first.

    False ONLY for a string that claims the family prefix and is malformed.
    Everything else, legacy formats included, is admissible: the hash lookup
    that follows decides it, exactly as before this shape existed."""
    if type(token) is not str or not token.startswith(FAMILY_PREFIX):
        return True
    return is_well_formed(token)


def has_kind_prefix(token: object, kind: str) -> bool:
    """True iff ``token`` starts with this kind's current OR legacy prefix."""
    if type(token) is not str:
        return False
    legacy = LEGACY_PREFIX[kind]
    return token.startswith(prefix(kind)) or bool(legacy and token.startswith(legacy))
