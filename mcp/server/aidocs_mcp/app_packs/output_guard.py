"""The model-facing response floor -- RFC 0003 v4.6.1 §6.7, §12.4, §12.5, §13 (build plan S13).

Backend output is untrusted (§13). Nothing a backend returns reaches the model
until every check below has passed, and every refusal is the ONE platform code
``app_backend_invalid_response`` (§13.1, §13.2). A refusal carries a fixed
local ``reason`` only; it never echoes the backend's bytes.

* **Byte caps, streamed** (§12.4, §12.5): the body is read in bounded chunks
  and refused one chunk past the cap. A ``Content-Length`` is never trusted
  and never consulted. The cap applies to the wire bytes AND, independently,
  to the decompressed bytes: ``gzip`` is decoded incrementally with a bounded
  output window, so a small-compressed / huge-decompressed body is refused
  without ever being materialized. Any other ``Content-Encoding`` (including
  stacked encodings) is refused.
* **Headers and content type** (§12.4, §12.5): at most 64 headers, 4 KiB per
  field name / value, 16 KiB aggregate; ``application/json`` only (UTF-8).
* **JSON bounds** (§12.5): strict UTF-8, no BOM, no duplicate member names
  (wire §1.1), no non-finite numbers, nesting depth <= 32 (measured on the
  bytes BEFORE parsing, so the parser never recurses past it), <= 10,000
  parsed nodes, an object at top level.
* **Envelope and declared errors** (§6.7, §13.2): a boolean ``ok``; an error
  result carries ``error.code``; a code starting ``app_`` is a spoof (only
  AIDOCS produces platform codes, §16.4) and an undeclared code is refused --
  this Layer 1 check does not rely on the pack's own enum.
* **Output schema** (§13.1): the body is validated against the EXACT pinned
  mode ``output_schema`` by a closed, fail-closed validator (below).
* **Sensitive-output backstop** (§13.3): every value that a successfully
  applied subschema annotates with ``x-aidocs-requires-capability`` must be
  ``null`` (or absent) unless that capability is in the call's
  ``capabilities_used`` (§7.5.1). A non-null value is refused.

The validator is CLOSED: it implements the JSON Schema 2020-12 keywords the
bundle floor needs and refuses (``output_schema_unusable``) a schema that uses
any other keyword, any non-local ``$ref`` or an unknown ``format``, instead of
silently ignoring it. ``jsonschema`` is deliberately not used: it is not a
declared dependency of this package, it ignores unknown keywords, and the PII
backstop needs the annotations of the subschemas that actually applied.
"""
from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional, Protocol

__all__ = [
    "APP_BACKEND_INVALID_RESPONSE",
    "CAPABILITY_ANNOTATION",
    "HEADERS_MAX_AGGREGATE_BYTES",
    "HEADERS_MAX_COUNT",
    "HEADER_FIELD_MAX_BYTES",
    "JSON_MAX_DEPTH",
    "JSON_MAX_NODES",
    "READ_CHUNK_BYTES",
    "RESPONSE_BODY_MAX_BYTES",
    "GuardedOutput",
    "ResponseLimits",
    "ResponseRefused",
    "check_content_type",
    "check_headers",
    "declared_error_codes_from_bundle",
    "guard_output",
    "parse_bounded_json",
    "read_bounded_body",
    "schema_accepts",
]

APP_BACKEND_INVALID_RESPONSE = "app_backend_invalid_response"  # §13.1, §16.3
CAPABILITY_ANNOTATION = "x-aidocs-requires-capability"  # §13.3

KIB = 1024
# §12.5 platform hard ceilings. Configuration may tighten, never loosen.
RESPONSE_BODY_MAX_BYTES = 512 * KIB
JSON_MAX_DEPTH = 32
JSON_MAX_NODES = 10_000
HEADERS_MAX_COUNT = 64
HEADERS_MAX_AGGREGATE_BYTES = 16 * KIB
HEADER_FIELD_MAX_BYTES = 4 * KIB
READ_CHUNK_BYTES = 16 * KIB

_PLATFORM_PREFIX = "app_"
_VALIDATION_BUDGET = 500_000  # evaluation steps; stops a pathological schema/instance pair


class ResponseRefused(Exception):
    """The backend response is refused before model exposure (§12.4, §13)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{APP_BACKEND_INVALID_RESPONSE}: {reason}")
        self.code = APP_BACKEND_INVALID_RESPONSE
        self.reason = reason


def _refuse(reason: str) -> ResponseRefused:
    return ResponseRefused(reason)


@dataclass(frozen=True)
class ResponseLimits:
    """§12.4 / §12.5 response bounds. Every value is 1 .. its platform ceiling."""

    body_max_bytes: int = RESPONSE_BODY_MAX_BYTES
    decompressed_max_bytes: int = RESPONSE_BODY_MAX_BYTES
    json_max_depth: int = JSON_MAX_DEPTH
    json_max_nodes: int = JSON_MAX_NODES
    headers_max_count: int = HEADERS_MAX_COUNT
    headers_max_aggregate_bytes: int = HEADERS_MAX_AGGREGATE_BYTES
    header_field_max_bytes: int = HEADER_FIELD_MAX_BYTES

    def __post_init__(self) -> None:
        ceilings = {
            "body_max_bytes": RESPONSE_BODY_MAX_BYTES,
            "decompressed_max_bytes": RESPONSE_BODY_MAX_BYTES,
            "json_max_depth": JSON_MAX_DEPTH,
            "json_max_nodes": JSON_MAX_NODES,
            "headers_max_count": HEADERS_MAX_COUNT,
            "headers_max_aggregate_bytes": HEADERS_MAX_AGGREGATE_BYTES,
            "header_field_max_bytes": HEADER_FIELD_MAX_BYTES,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be 1..{ceiling} (§12.5: limits may only tighten)")


@dataclass(frozen=True)
class GuardedOutput:
    """A response that passed the floor. ``error_code`` is a DECLARED pack code or None."""

    ok: bool
    body: Mapping[str, Any]
    error_code: Optional[str] = None


# -- headers / content type (§12.4, §12.5) --------------------------------------------


def check_headers(headers: Iterable[tuple[str, str]], limits: ResponseLimits) -> None:
    """Refuse more than 64 headers, a field over 4 KiB, or over 16 KiB in aggregate."""
    count = 0
    total = 0
    for name, value in headers:
        count += 1
        if count > limits.headers_max_count:
            raise _refuse("headers_over_count")
        n = len(str(name).encode("utf-8", "surrogateescape"))
        v = len(str(value).encode("utf-8", "surrogateescape"))
        if n > limits.header_field_max_bytes or v > limits.header_field_max_bytes:
            raise _refuse("header_field_over_cap")
        total += n + v + 4  # "name: value\r\n"
        if total > limits.headers_max_aggregate_bytes:
            raise _refuse("headers_over_aggregate")


def check_content_type(value: Any) -> None:
    """``application/json`` with at most a ``charset=utf-8`` parameter."""
    if type(value) is not str:
        raise _refuse("content_type_refused")
    parts = [p.strip() for p in value.split(";")]
    if parts[0].lower() != "application/json":
        raise _refuse("content_type_refused")
    for param in parts[1:]:
        key, sep, val = param.partition("=")
        if not sep or key.strip().lower() != "charset" or val.strip().strip('"').lower() != "utf-8":
            raise _refuse("content_type_refused")


# -- bounded, streamed body (§12.4, §12.5) ----------------------------------------------


class _Readable(Protocol):
    def read(self, n: int = -1) -> bytes: ...


def _encoding(content_encoding: Any) -> Optional[str]:
    if content_encoding is None:
        return None
    if type(content_encoding) is not str:
        raise _refuse("content_encoding_refused")
    value = content_encoding.strip().lower()
    if value in ("", "identity"):
        return None
    if value == "gzip":
        return "gzip"
    raise _refuse("content_encoding_refused")  # deflate, br, stacked, unknown


def read_bounded_body(
    stream: _Readable,
    *,
    content_encoding: Any,
    limits: ResponseLimits,
    before_read: Any = None,
) -> bytes:
    """Stream the body; refuse one chunk past the wire cap or the decompressed cap.

    ``before_read`` (optional) is called before every read; the transport uses
    it to enforce its idle / total deadlines.
    """
    encoding = _encoding(content_encoding)
    decomp = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    out = bytearray()
    wire = 0
    while True:
        if before_read is not None:
            before_read()
        want = min(READ_CHUNK_BYTES, limits.body_max_bytes - wire + 1)
        chunk = stream.read(want)
        if not chunk:
            break
        wire += len(chunk)
        if wire > limits.body_max_bytes:
            raise _refuse("response_body_over_cap")
        if decomp is None:
            out += chunk
            if len(out) > limits.decompressed_max_bytes:
                raise _refuse("response_decompressed_over_cap")
            continue
        if decomp.eof:
            raise _refuse("response_encoding_invalid")  # bytes after the gzip member
        data = chunk
        try:
            while data:
                room = limits.decompressed_max_bytes - len(out) + 1  # always >= 1 (0 = unlimited in zlib)
                out += decomp.decompress(data, room)
                if len(out) > limits.decompressed_max_bytes:
                    raise _refuse("response_decompressed_over_cap")
                data = decomp.unconsumed_tail
                if decomp.eof:
                    break
        except zlib.error:
            raise _refuse("response_encoding_invalid") from None
        if decomp.eof and decomp.unused_data:
            raise _refuse("response_encoding_invalid")
    if decomp is not None and not decomp.eof:
        raise _refuse("response_encoding_invalid")  # truncated member
    return bytes(out)


# -- JSON bounds (§12.5; wire §1.1) -------------------------------------------------------


def _depth_scan(raw: bytes, max_depth: int) -> None:
    depth = 0
    in_string = False
    escaped = False
    for b in raw:
        if in_string:
            if escaped:
                escaped = False
            elif b == 0x5C:  # backslash
                escaped = True
            elif b == 0x22:
                in_string = False
        elif b == 0x22:
            in_string = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > max_depth:
                raise _refuse("json_depth_over_cap")
        elif b in (0x5D, 0x7D):
            depth -= 1


class _Duplicate(Exception):
    pass


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Duplicate
        out[key] = value
    return out


def _no_constants(_name: str) -> Any:
    raise ValueError("non-finite number")


def _count_nodes(value: Any, limit: int) -> None:
    stack = [value]
    count = 0
    while stack:
        cur = stack.pop()
        count += 1
        if count > limit:
            raise _refuse("json_nodes_over_cap")
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def parse_bounded_json(raw: bytes, limits: ResponseLimits) -> dict:
    """Strict, bounded parse of a response body into a JSON object."""
    if type(raw) is not bytes:
        raise _refuse("json_invalid")
    _depth_scan(raw, limits.json_max_depth)
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except _Duplicate:
        raise _refuse("json_duplicate_member") from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _refuse("json_invalid") from None
    if type(value) is not dict:
        raise _refuse("json_not_object")
    _count_nodes(value, limits.json_max_nodes)
    return value


# -- declared error vocabulary (§6.7) ----------------------------------------------------


def declared_error_codes_from_bundle(bundle: Any) -> frozenset:
    """The pack's declared ``application_error_codes`` -- the typed S2 projection only.

    :func:`~.bundle.load_bundle` parses and validates that list ONCE into
    :attr:`~.bundle.PackBundle.application_error_codes` (unique, non-empty,
    and an ``app_*`` entry refuses installation, §6.7). This is a reader of
    that projection and nothing else: ``canonical_bytes`` is never re-parsed
    here. Fails closed: a missing or malformed projection -- anything but a
    ``frozenset`` of non-empty, non-``app_*`` strings -- yields the EMPTY set,
    so every backend error is then undeclared.
    """
    codes = getattr(bundle, "application_error_codes", None)
    if type(codes) is not frozenset:
        return frozenset()
    if not all(type(c) is str and c and not c.startswith(_PLATFORM_PREFIX) for c in codes):
        return frozenset()
    return codes


# -- the closed schema validator (§13.1, §13.3) ------------------------------------------


class _SchemaUnusable(Exception):
    pass


_ANNOTATION_KEYWORDS = frozenset(
    {"$schema", "$defs", "$comment", "title", "description", "examples", "default", "deprecated", "readOnly", "writeOnly"}
)
_ASSERTION_KEYWORDS = frozenset(
    {
        "$ref", "type", "enum", "const",
        "properties", "patternProperties", "additionalProperties", "required", "minProperties", "maxProperties",
        "items", "minItems", "maxItems", "uniqueItems",
        "minLength", "maxLength", "pattern", "format",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
        CAPABILITY_ANNOTATION,
    }
)
_KNOWN_KEYWORDS = _ANNOTATION_KEYWORDS | _ASSERTION_KEYWORDS
_TYPES = frozenset({"null", "boolean", "object", "array", "number", "integer", "string"})
_DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:\S*")


def _is_number(v: Any) -> bool:
    return type(v) in (int, float)


def _json_type(v: Any, name: str) -> bool:
    if name == "null":
        return v is None
    if name == "boolean":
        return type(v) is bool
    if name == "object":
        return type(v) is dict
    if name == "array":
        return type(v) is list
    if name == "string":
        return type(v) is str
    if name == "number":
        return _is_number(v)
    if name == "integer":
        return type(v) is int or (type(v) is float and v.is_integer())
    raise _SchemaUnusable


def _json_equal(a: Any, b: Any) -> bool:
    if type(a) is bool or type(b) is bool:
        return type(a) is bool and type(b) is bool and a is b
    if _is_number(a) and _is_number(b):
        return a == b
    if type(a) is not type(b):
        return False
    if type(a) is list:
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if type(a) is dict:
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return a == b


def _format_ok(fmt: Any, value: str) -> bool:
    try:
        if fmt == "date-time":
            m = _DATE_TIME.fullmatch(value)
            if m is None:
                return False
            y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
            datetime(y, mo, d, h, mi, min(s, 59))  # RFC 3339 allows a leap second
            return s <= 60
        if fmt == "date":
            m = _DATE.fullmatch(value)
            if m is None:
                return False
            date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return True
    except ValueError:
        return False
    if fmt == "email":
        return _EMAIL.fullmatch(value) is not None
    if fmt == "uri":
        return _URI.fullmatch(value) is not None
    raise _SchemaUnusable  # an unknown format is never silently an annotation


class _Validator:
    """Returns the list of (capability, value) annotations, or None when invalid."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.budget = _VALIDATION_BUDGET
        self.patterns: dict[str, re.Pattern] = {}

    def _pointer(self, ref: Any) -> Any:
        if type(ref) is not str or not ref.startswith("#"):
            raise _SchemaUnusable
        if ref == "#":
            return self.root
        if not ref.startswith("#/"):
            raise _SchemaUnusable
        target = self.root
        for raw in ref[2:].split("/"):
            part = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and part in target:
                target = target[part]
            elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
                target = target[int(part)]
            else:
                raise _SchemaUnusable
        return target

    def _pattern(self, pattern: Any) -> re.Pattern:
        if type(pattern) is not str:
            raise _SchemaUnusable
        compiled = self.patterns.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern)
            except re.error:
                raise _SchemaUnusable from None
            self.patterns[pattern] = compiled
        return compiled

    def _int_kw(self, schema: dict, key: str) -> Optional[int]:
        value = schema.get(key)
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise _SchemaUnusable
        return value

    def validate(self, schema: Any, inst: Any) -> Optional[list]:
        self.budget -= 1
        if self.budget < 0:
            raise _SchemaUnusable
        if schema is True:
            return []
        if schema is False:
            return None
        if type(schema) is not dict or set(schema) - _KNOWN_KEYWORDS:
            raise _SchemaUnusable
        notes: list = []

        def sub(s: Any, i: Any) -> bool:
            r = self.validate(s, i)
            if r is None:
                return False
            notes.extend(r)
            return True

        if "$ref" in schema and not sub(self._pointer(schema["$ref"]), inst):
            return None
        if "type" in schema:
            types = schema["type"] if type(schema["type"]) is list else [schema["type"]]
            if not types or not all(type(t) is str and t in _TYPES for t in types):
                raise _SchemaUnusable
            if not any(_json_type(inst, t) for t in types):
                return None
        if "const" in schema and not _json_equal(schema["const"], inst):
            return None
        if "enum" in schema:
            if type(schema["enum"]) is not list:
                raise _SchemaUnusable
            if not any(_json_equal(e, inst) for e in schema["enum"]):
                return None
        if type(inst) is str and not self._string(schema, inst):
            return None
        if _is_number(inst) and not self._number(schema, inst):
            return None
        if type(inst) is list and not self._array(schema, inst, sub):
            return None
        if type(inst) is dict and not self._object(schema, inst, sub):
            return None
        if not self._combinators(schema, inst, notes):
            return None
        if CAPABILITY_ANNOTATION in schema:
            cap = schema[CAPABILITY_ANNOTATION]
            if type(cap) is not str or not cap:
                raise _SchemaUnusable
            notes.append((cap, inst))
        return notes

    def _string(self, schema: dict, inst: str) -> bool:
        n = len(inst)  # code points
        lo, hi = self._int_kw(schema, "minLength"), self._int_kw(schema, "maxLength")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            return False
        if "pattern" in schema and self._pattern(schema["pattern"]).search(inst) is None:
            return False
        if "format" in schema and not _format_ok(schema["format"], inst):
            return False
        return True

    def _number(self, schema: dict, inst: Any) -> bool:
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if key in schema and not _is_number(schema[key]):
                raise _SchemaUnusable
        if "minimum" in schema and inst < schema["minimum"]:
            return False
        if "maximum" in schema and inst > schema["maximum"]:
            return False
        if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
            return False
        if "exclusiveMaximum" in schema and inst >= schema["exclusiveMaximum"]:
            return False
        if "multipleOf" in schema:
            m = schema["multipleOf"]
            if m <= 0:
                raise _SchemaUnusable
            q = inst / m
            if not float(q).is_integer():
                return False
        return True

    def _array(self, schema: dict, inst: list, sub: Any) -> bool:
        lo, hi = self._int_kw(schema, "minItems"), self._int_kw(schema, "maxItems")
        if (lo is not None and len(inst) < lo) or (hi is not None and len(inst) > hi):
            return False
        if schema.get("uniqueItems") is True:
            for i, a in enumerate(inst):
                if any(_json_equal(a, b) for b in inst[i + 1:]):
                    return False
        elif "uniqueItems" in schema and schema["uniqueItems"] is not False:
            raise _SchemaUnusable
        if "items" in schema:
            if type(schema["items"]) not in (dict, bool):
                raise _SchemaUnusable
            for item in inst:
                if not sub(schema["items"], item):
                    return False
        return True

    def _object(self, schema: dict, inst: dict, sub: Any) -> bool:
        required = schema.get("required", [])
        if type(required) is not list or not all(type(r) is str for r in required):
            raise _SchemaUnusable
        if any(r not in inst for r in required):
            return False
        lo, hi = self._int_kw(schema, "minProperties"), self._int_kw(schema, "maxProperties")
        if (lo is not None and len(inst) < lo) or (hi is not None and len(inst) > hi):
            return False
        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        if type(props) is not dict or type(patterns) is not dict:
            raise _SchemaUnusable
        for key, value in inst.items():
            matched = False
            if key in props:
                matched = True
                if not sub(props[key], value):
                    return False
            for pat, pschema in patterns.items():
                if self._pattern(pat).search(key) is not None:
                    matched = True
                    if not sub(pschema, value):
                        return False
            if not matched and "additionalProperties" in schema:
                if not sub(schema["additionalProperties"], value):
                    return False
        return True

    def _combinators(self, schema: dict, inst: Any, notes: list) -> bool:
        for key in ("allOf", "anyOf", "oneOf"):
            if key in schema and (type(schema[key]) is not list or not schema[key]):
                raise _SchemaUnusable
        for s in schema.get("allOf", []):
            r = self.validate(s, inst)
            if r is None:
                return False
            notes.extend(r)
        if "anyOf" in schema:
            passed = [r for r in (self.validate(s, inst) for s in schema["anyOf"]) if r is not None]
            if not passed:
                return False
            for r in passed:
                notes.extend(r)
        if "oneOf" in schema:
            passed = [r for r in (self.validate(s, inst) for s in schema["oneOf"]) if r is not None]
            if len(passed) != 1:
                return False
            notes.extend(passed[0])
        if "not" in schema and self.validate(schema["not"], inst) is not None:
            return False
        if "if" in schema:
            cond = self.validate(schema["if"], inst)
            if cond is not None:
                notes.extend(cond)
                branch = schema.get("then", True)
            else:
                branch = schema.get("else", True)
            r = self.validate(branch, inst)
            if r is None:
                return False
            notes.extend(r)
        return True


# -- the floor ---------------------------------------------------------------------------


def schema_accepts(schema: Any, instance: Any) -> bool:
    """True only when ``instance`` validates against ``schema`` with the same
    closed validator; an unusable schema is False (fail closed). Used by the
    transport for the §6.5 pre-egress input check."""
    if not isinstance(schema, Mapping):
        return False
    try:
        return _Validator(schema).validate(dict(schema), instance) is not None
    except (_SchemaUnusable, RecursionError):
        return False


def guard_output(
    body: Any,
    *,
    mode: Any,
    declared_codes: frozenset,
    capabilities_used: Iterable[str],
) -> GuardedOutput:
    """§13.2 envelope + declared code, §13.1 exact mode schema, §13.3 backstop."""
    if type(body) is not dict:
        raise _refuse("response_not_object")
    ok = body.get("ok")
    if type(ok) is not bool:
        raise _refuse("envelope_ok_missing")
    error_code: Optional[str] = None
    if ok:
        if "error" in body:
            raise _refuse("envelope_conflict")
    else:
        error = body.get("error")
        code = error.get("code") if type(error) is dict else None
        if type(code) is not str or not code:
            raise _refuse("envelope_error_missing")
        if code.startswith(_PLATFORM_PREFIX):
            raise _refuse("backend_emitted_platform_code")  # §13.2: only AIDOCS emits app_*
        if code not in declared_codes:
            raise _refuse("undeclared_error_code")
        error_code = code
    schema = getattr(mode, "output_schema", None)
    if not isinstance(schema, Mapping):
        raise _refuse("output_schema_unusable")
    try:
        notes = _Validator(schema).validate(dict(schema), body)
    except (_SchemaUnusable, RecursionError):
        raise _refuse("output_schema_unusable") from None
    if notes is None:
        raise _refuse("output_schema_mismatch")
    used = frozenset(capabilities_used)
    for capability, value in notes:
        if capability not in used and value is not None:
            raise _refuse("sensitive_field_unauthorized")
    return GuardedOutput(ok=ok, body=body, error_code=error_code)
