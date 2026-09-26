"""RFC 8785 JSON Canonicalization Scheme (JCS) -- #1089, RFC 0003 §6.2 / D9.

The ONE canonicalizer for every EXTERNAL-APPLICATION digest (contract_digest,
body_sha256, preview_digest, grants_digest, the normalized business request
digest). A backend written in another language must reproduce these bytes
exactly, so this module follows RFC 8785 literally:

* numbers use the ECMAScript ``Number.prototype.toString`` form (RFC 8785
  §3.2.2.3): shortest round-trip digits, ``1e+21`` / ``1e-7`` exponent
  thresholds, ``-0`` serialized as ``0``;
* strings escape only the RFC 8785 §3.2.2.2 classes (``\\b \\t \\n \\f \\r
  \\" \\\\`` and the remaining C0 controls as lowercase ``\\u00xx``); every
  other code point is emitted literally as UTF-8;
* object members are sorted by their UTF-16 code units (§3.2.3), not by code
  point;
* output is UTF-8 with no whitespace.

Everything outside I-JSON (RFC 7493) is REFUSED with :class:`JcsError`, never
coerced: NaN / infinity, non-string keys, lone surrogates, duplicate keys,
integers whose magnitude exceeds 2**53 (not exactly representable as an IEEE
754 double), and any non-JSON Python value. Nothing is stringified -- that is
the native ``canonical_invocation.normalize_args`` behavior this module exists
to keep out of cross-language digests (RFC 0003 §6.2.1).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

__all__ = [
    "JcsError",
    "MAX_SAFE_INTEGER_MAGNITUDE",
    "canonicalize",
    "canonicalize_json_text",
    "jcs_sha256_hex",
]

MAX_SAFE_INTEGER_MAGNITUDE = 2**53

_SHORT_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


class JcsError(ValueError):
    """The value cannot be canonicalized under RFC 8785 / I-JSON."""


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of a JSON-shaped value.

    Accepted: ``dict`` with ``str`` keys, ``list``, ``str``, ``int`` (|n| <=
    2**53), finite ``float``, ``bool`` and ``None``. Anything else raises
    :class:`JcsError`.
    """
    parts: list[str] = []
    _serialize(value, parts)
    return "".join(parts).encode("utf-8")


def canonicalize_json_text(text: str | bytes) -> bytes:
    """Parse JSON text strictly (I-JSON) and return its RFC 8785 bytes.

    Refuses duplicate member names (after escape decoding), the non-standard
    ``NaN`` / ``Infinity`` literals, integers beyond 2**53 and malformed text.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JcsError(f"JSON text is not valid UTF-8: {exc}") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_refuse_duplicate_pairs,
            parse_constant=_refuse_constant,
            parse_int=_parse_int,
            parse_float=float,
        )
    except JcsError:
        raise
    except (ValueError, RecursionError) as exc:
        raise JcsError(f"malformed JSON text: {exc}") from exc
    return canonicalize(parsed)


def jcs_sha256_hex(value: Any) -> str:
    """SHA-256 (lowercase hex) of ``canonicalize(value)``."""
    return hashlib.sha256(canonicalize(value)).hexdigest()


# -- parsing hooks ---------------------------------------------------------


def _refuse_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise JcsError(f"duplicate object member name: {key!r}")
        out[key] = val
    return out


def _refuse_constant(name: str) -> Any:
    raise JcsError(f"non-finite number literal is not JSON: {name}")


def _parse_int(literal: str) -> int:
    value = int(literal)
    _check_int(value)
    return value


# -- serialization ---------------------------------------------------------


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif type(value) is int:
        _check_int(value)
        out.append(str(value))
    elif type(value) is float:
        out.append(_serialize_number(value))
    elif type(value) is str:
        out.append(_serialize_string(value))
    elif type(value) is list:
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif type(value) is dict:
        _serialize_object(value, out)
    else:
        raise JcsError(f"value of type {type(value).__name__} is not JSON")


def _serialize_object(obj: dict, out: list[str]) -> None:
    members: dict[str, Any] = {}
    for key, val in obj.items():
        if type(key) is not str:
            raise JcsError(f"object member name must be a string, got {type(key).__name__}")
        norm = _normalize_text(key)
        if norm in members:
            raise JcsError(f"duplicate object member name: {norm!r}")
        members[norm] = val
    out.append("{")
    for i, key in enumerate(sorted(members, key=_utf16_sort_key)):
        if i:
            out.append(",")
        out.append(_escape(key))
        out.append(":")
        _serialize(members[key], out)
    out.append("}")


def _utf16_sort_key(key: str) -> bytes:
    # Big-endian UTF-16 bytes compare exactly as the code-unit sequence does.
    return key.encode("utf-16-be")


def _check_int(value: int) -> None:
    if abs(value) > MAX_SAFE_INTEGER_MAGNITUDE:
        raise JcsError(f"integer {value} exceeds 2**53 and is not exactly an IEEE 754 double")


def _normalize_text(text: str) -> str:
    """Join explicit surrogate pairs; refuse any surrogate left unpaired."""
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        try:
            text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise JcsError("string contains a lone surrogate") from exc
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
            raise JcsError("string contains a lone surrogate")
    return text


def _serialize_string(text: str) -> str:
    return _escape(_normalize_text(text))


def _escape(text: str) -> str:
    buf = ['"']
    for ch in text:
        cp = ord(ch)
        short = _SHORT_ESCAPES.get(cp)
        if short is not None:
            buf.append(short)
        elif cp < 0x20:
            buf.append(f"\\u{cp:04x}")
        else:
            buf.append(ch)
    buf.append('"')
    return "".join(buf)


def _serialize_number(value: float) -> str:
    """ECMAScript Number::toString for a finite double (RFC 8785 §3.2.2.3)."""
    if not math.isfinite(value):
        raise JcsError(f"non-finite number is not JSON: {value!r}")
    if value == 0.0:
        return "0"  # covers -0
    sign = "-" if value < 0 else ""
    # Python's repr is the shortest round-trip digit string, closest to the
    # true value on ties -- the same digit choice ECMAScript mandates.
    text = repr(abs(value))
    if "e" in text:
        mantissa, exp_text = text.split("e")
        exponent = int(exp_text)
    else:
        mantissa, exponent = text, 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = int_part + frac_part
    point = len(int_part) + exponent  # ECMAScript "n"
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k = len(digits)
    n = point
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        body = digits + "e" + exp if k == 1 else digits[0] + "." + digits[1:] + "e" + exp
    return sign + body
