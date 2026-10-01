"""Application Tool Pack bundle parser and install-time validator (RFC 0003 S2).

Law: RFC 0003 v4.6.1 §0.3 (safe-only floor), §5.6 (install-time validation
order), §6.1 (envelope), §6.2 (``contract_digest``), §6.3 / §6.3.1 / §6.3.2
(``ExternalModeSpec``, safe-mode admission, closed unions with ONE const
discriminator per mode, per-branch effect metadata), §6.4 (relative endpoints
only), §6.6 (closed-world schema floor, measured on the fully resolved
schema), §12.5 (pack bounds), §18.13 (every flip refuses); wire rev 8 §10.2 /
§10.3 (``platform_handler`` + kept ``relative_endpoint``; a bare relative
``internal_endpoints.receipt_lookup``).

Layer 1 only: nothing here knows CRM semantics. Every refusal raises
:class:`BundleRefused` with a stable ``code``; nothing is coerced or repaired.

Encoding choices the sealed text leaves open (flagged, not law):

* effect metadata lives in the mode's ``branches`` list, one entry per union
  branch, matched by ``discriminator_value`` (``null`` for a non-union mode);
* §12.5 schema BYTES are the RFC 8785 bytes of the resolved schema; DEPTH and
  NODES count schema objects (subschema nesting), not raw JSON containers;
* prior-state retention is ``"unlimited"`` or an integer number of days;
* the platform caps for ``max_affected_entities`` / ``max_fan_out`` have no
  number in the sealed text; per the S2 review ruling the v1 defaults are
  explicit (now 10 / 0; affected was 2 until the operator raised it for the
  v4.7 batch prospect add) and a missing cap FAILS CLOSED, never "unlimited"
  (:class:`PlatformCaps`);
* ``"release_label": "DEMO/NON-v1"`` is an M1 internal/demo ENCODING only
  (build plan "Gates"); it is not part of any sealed v1 bundle format, and a
  bundle carrying it can never register as the sealed v1 pack.

Namespace: a tool name must carry the pack's declared namespace prefix
(§3.3, explicit: "A pack declares a namespace prefix and may expose only tool
names allowed by that namespace"). Namespace UNIQUENESS is not checked here:
it holds within one toolspace and fails at binding activation (§3.3, S7).

Review obligation (§6.3.2 effect homogeneity): this parser admits a branch
from its DECLARED metadata plus the structural union checks. Parser metadata
alone does not prove that every input a branch accepts has the same effect
(e.g. a data enum that secretly selects behaviour). That proof is the L2
review of the pinned artifact (§19), not this module.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .jcs import JcsError, canonicalize, canonicalize_json_text

__all__ = [
    "BOUNDS",
    "BranchSpec",
    "BundleRefused",
    "DelegatedTerms",
    "DEMO_RELEASE_LABEL",
    "FLOOR_EFFECT_CLASSES",
    "ModeSpec",
    "PackBundle",
    "PlatformCaps",
    "SAFE_EFFECT_CLASSES",
    "ToolSpec",
    "load_bundle",
]

BUNDLE_FORMAT = "aidocs.application-tool-pack/v1"  # §6.1
CANONICALIZATION = "RFC8785-JCS"  # §6.1, §6.2
ENTITLEMENT_AUTHORITIES = frozenset({"delegated_application"})  # §7.2: the only v1 value
TOOL_CLASSES = frozenset({"read", "edit"})  # §6.3
IDEMPOTENCY = frozenset({"none", "required"})  # §6.3
PLATFORM_HANDLERS = frozenset({"mutation_resolver"})  # wire §10.2: the only v1 value
MAX_FILE_PARAMS = 1  # §14.5: one file parameter per call
DEMO_RELEASE_LABEL = "DEMO/NON-v1"  # build plan "Gates": bundle identity
RELEASE_LABELS = frozenset({DEMO_RELEASE_LABEL})
# §6.1 requires audience_name but states no length; this bound is an S2
# encoding choice (flagged), not sealed law.
AUDIENCE_NAME_MAX_BYTES = 256
ENTITLEMENT_AUDIENCE = "aidocs-pack-entitlements"  # §7.3: aud = aidocs-pack-entitlements
# Neither RFC §7 nor wire §1.4 / §6 numbers a ceiling for
# max_attestation_ttl_seconds (wire §1.4 bounds the attestation BY it). S2
# encoding choice (flagged, reviewable): 900 s, the §6.1 illustrative value and
# the accepted CRM fixture's value, i.e. the smallest that admits the fixture.
MAX_ATTESTATION_TTL_CEILING_SECONDS = 900

SAFE_EFFECT_CLASSES = frozenset({"read", "draft", "internal_note", "reversible_edit"})  # §6.3.2
FLOOR_EFFECT_CLASSES = frozenset(
    {"destroy", "irreversible_replace", "external_side_effect", "permission_change", "value_transfer"}
)
RETENTION_FLOOR_DAYS = 7  # §6.3.1 condition 3
RETENTION_UNLIMITED = "unlimited"
UNDO_RECEIPT_FIELDS = ("undo_available_until", "undo_surface")  # §6.3.1, §13.6, §18.13

KIB = 1024
BOUNDS = MappingProxyType(
    {  # §12.5 "Pack bounds (checked at installation)"
        "tools_per_pack": 32,
        "modes_per_tool": 8,
        "modes_per_pack": 128,
        "input_schema_bytes": 32 * KIB,
        "output_schema_bytes": 64 * KIB,
        "event_schema_bytes": 32 * KIB,
        "aggregate_schema_bytes": 512 * KIB,
        "schema_depth": 12,
        "schema_nodes": 2048,
        "tool_description_bytes": 2 * KIB,
        "mode_description_bytes": 1 * KIB,
    }
)
# Resolution work budget (JSON values produced while inlining $ref). Stops a
# ref fan-out bomb before the §12.5 node count is even measured.
_RESOLVE_BUDGET = 200_000

_MODE_MINIMUM = (  # §6.3 "Each mode declares at minimum"
    "input_schema",
    "output_schema",
    "required_capabilities",
    "confirm",
    "idempotency",
    "requires_conversation_context",
)
_BRANCH_METADATA = (  # §6.3.2 per-branch effect metadata
    "branch_id",
    "discriminator_value",
    "effect_classes",
    "reversible",
    "external_effect",
    "downstream_irreversible",
    "max_affected_entities",
    "max_fan_out",
    "required_capabilities",
)
_SUBSCHEMA_MAPS = frozenset({"properties", "patternProperties", "dependentSchemas", "$defs"})
_SUBSCHEMA_LISTS = frozenset({"oneOf", "anyOf", "allOf", "prefixItems"})
_SUBSCHEMA_ONE = frozenset(
    {
        "items", "additionalProperties", "not", "if", "then", "else", "contains",
        "propertyNames", "unevaluatedItems", "unevaluatedProperties", "additionalItems",
    }
)
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@-]+$")


class BundleRefused(ValueError):
    """Installation refuses the bundle. ``code`` is stable; ``where`` locates it."""

    def __init__(self, code: str, where: str, detail: str = "") -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code} at {where}" + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class PlatformCaps:
    """Platform caps named by §6.3.1 / §6.3.2 / §18.13 (hard ceilings, §12.5).

    v1 defaults are explicit (reviewer ruling on S2): the smallest values that
    admit the accepted CRM fixture. Configuration may tighten them; a ``None``
    or otherwise invalid cap is NOT "unlimited": installation refuses with
    ``platform_caps_unconfigured``.
    """

    # 2 -> 10 (operator, 2026-10-01): the v4.7 batch prospect add writes up
    # to 10 rows in ONE all-or-nothing, reversible, internal-only call.
    max_affected_entities: int | None = 10
    max_fan_out: int | None = 0


@dataclass(frozen=True)
class BranchSpec:
    branch_id: str
    discriminator_value: Any
    effect_classes: tuple[str, ...]
    reversible: bool
    external_effect: bool
    downstream_irreversible: bool
    max_affected_entities: int
    max_fan_out: int
    required_capabilities: tuple[str, ...]
    prior_state_retention: Any

    @property
    def read_only(self) -> bool:
        return set(self.effect_classes) == {"read"}


@dataclass(frozen=True)
class ModeSpec:
    name: str
    discriminator: str | None
    branches: tuple[BranchSpec, ...]
    relative_endpoint: str
    platform_handler: str | None
    idempotency: str
    requires_conversation_context: bool
    input_schema: Mapping[str, Any] = field(repr=False)
    output_schema: Mapping[str, Any] = field(repr=False)
    # §6.3 / §14: the host-file parameters of this mode (at most one, §14.5).
    file_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool_class: str
    description: str
    modes: Mapping[str, ModeSpec]
    # §6.3: the ExternalToolSpec's host-file parameters -- the ONE validated
    # authority (r0b2 B3). Every mode of the tool declares exactly this list;
    # tools/list emits its host hint from here, never from raw bundle JSON.
    file_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegatedTerms:
    """``identity.delegated`` (§6.1, §7.2, §7.3; wire §5, §6)."""

    entitlements_endpoint: str
    entitlement_audience: str
    max_attestation_ttl_seconds: int


@dataclass(frozen=True)
class PackBundle:
    pack_id: str
    namespace: str
    api_version: str
    audience_name: str
    capabilities: tuple[str, ...]
    delegated: DelegatedTerms
    contract_digest: str
    release_label: str | None
    tools: Mapping[str, ToolSpec]
    receipt_lookup: str | None
    # §6.7: the declared backend error vocabulary, parsed ONCE here. It is the
    # only reader of that list: nothing downstream re-parses canonical_bytes.
    application_error_codes: frozenset[str]
    canonical_bytes: bytes = field(repr=False)

    @property
    def is_demo(self) -> bool:
        return self.release_label == DEMO_RELEASE_LABEL


# --------------------------------------------------------------------------
# entry point


def load_bundle(data: bytes | bytearray | str | Mapping[str, Any], *, caps: PlatformCaps | None = None) -> PackBundle:
    """Parse, validate and digest a pack bundle; refuse on any violation."""
    caps = PlatformCaps() if caps is None else caps
    _check_caps(caps)
    try:
        if isinstance(data, (bytes, bytearray, str)):
            canonical = canonicalize_json_text(data)
        elif isinstance(data, Mapping):
            canonical = canonicalize(dict(data))
        else:
            raise BundleRefused("bundle_not_json", "$", f"unsupported input {type(data).__name__}")
    except JcsError as exc:
        raise BundleRefused("bundle_not_json", "$", str(exc)) from exc
    doc = json.loads(canonical)
    if not isinstance(doc, dict):
        raise BundleRefused("bundle_invalid", "$", "bundle is not an object")
    digest = hashlib.sha256(canonical).hexdigest()  # §6.2
    return _Validator(doc, caps).run(digest, canonical)


def _check_caps(caps: Any) -> None:
    """Fail closed: every cap is a configured integer (>= 1 / >= 0)."""
    if not isinstance(caps, PlatformCaps):
        raise BundleRefused("platform_caps_unconfigured", "caps", repr(caps))
    if not _is_int(caps.max_affected_entities) or caps.max_affected_entities < 1:
        raise BundleRefused("platform_caps_unconfigured", "caps.max_affected_entities")
    if not _is_int(caps.max_fan_out) or caps.max_fan_out < 0:
        raise BundleRefused("platform_caps_unconfigured", "caps.max_fan_out")


def _capabilities(value: Any) -> tuple[str, ...]:
    """§6.1 ``capabilities``: a non-empty list of unique non-empty strings."""
    if (
        not isinstance(value, list)
        or not value
        or not all(_nonempty_str(v) for v in value)
        or len(set(value)) != len(value)
    ):
        raise BundleRefused("bundle_capabilities_invalid", "$.capabilities", repr(value)[:80])
    return tuple(value)


_ABSENT = object()
PLATFORM_ERROR_PREFIX = "app_"  # §6.7 / §16.4: AIDOCS-produced platform codes only


def _application_error_codes(value: Any) -> frozenset[str]:
    """§6.7 ``application_error_codes``: a list of unique non-empty strings.

    The list is REQUIRED (it may be empty, as in the §6.1 example): §6.7
    compiles the closed output error enum from it, so an absent list is not
    read as "declares nothing". An ``app_*`` entry is a platform code the
    backend may never emit (§6.7), so it refuses the whole bundle; it is never
    silently dropped.
    """
    where = "$.application_error_codes"
    if (
        value is _ABSENT
        or not isinstance(value, list)
        or not all(_nonempty_str(v) for v in value)
        or len(set(value)) != len(value)
    ):
        raise BundleRefused("bundle_error_codes_invalid", where, repr(value)[:80] if value is not _ABSENT else "required")
    reserved = sorted(v for v in value if v.startswith(PLATFORM_ERROR_PREFIX))
    if reserved:
        raise BundleRefused("application_error_code_reserved", where, repr(reserved)[:80])
    return frozenset(value)


def _delegated(value: Any) -> DelegatedTerms:
    """``identity.delegated`` for the only v1 authority, delegated_application."""
    where = "$.identity.delegated"
    if not isinstance(value, dict):
        raise BundleRefused("bundle_delegated_invalid", where, "required object")
    endpoint = value.get("entitlements_endpoint")
    if not _is_relative_path(endpoint):  # wire §5: endpoint paths come from the bundle, §6.4
        raise BundleRefused("bundle_delegated_invalid", f"{where}.entitlements_endpoint", repr(endpoint)[:80])
    audience = value.get("entitlement_audience")
    if audience != ENTITLEMENT_AUDIENCE:  # §7.3
        raise BundleRefused("bundle_delegated_invalid", f"{where}.entitlement_audience", repr(audience)[:80])
    ttl = value.get("max_attestation_ttl_seconds")
    if not _is_int(ttl) or not 1 <= ttl <= MAX_ATTESTATION_TTL_CEILING_SECONDS:
        raise BundleRefused("bundle_attestation_ttl_invalid", f"{where}.max_attestation_ttl_seconds", repr(ttl))
    return DelegatedTerms(endpoint, audience, ttl)


# --------------------------------------------------------------------------
# validation


class _Validator:
    def __init__(self, doc: dict[str, Any], caps: PlatformCaps) -> None:
        self.doc = doc
        self.caps = caps
        self.aggregate_bytes = 0
        self.capabilities: tuple[str, ...] = ()
        self.delegated: DelegatedTerms | None = None
        self.error_codes: frozenset[str] = frozenset()

    def run(self, digest: str, canonical: bytes) -> PackBundle:
        doc = self.doc
        self._envelope()
        tools_doc = doc["tools"]
        self._counts(tools_doc)
        tools: dict[str, ToolSpec] = {}
        for name, spec in tools_doc.items():
            tools[name] = self._tool(name, spec)
        self._events(doc.get("events", {}))
        if self.aggregate_bytes > BOUNDS["aggregate_schema_bytes"]:
            raise BundleRefused(
                "bound_aggregate_schema_bytes", "$", f"{self.aggregate_bytes} bytes"
            )
        receipt_lookup = self._receipt_lookup(tools)
        return PackBundle(
            pack_id=doc["pack_id"],
            namespace=doc["namespace"],
            api_version=doc["api_version"],
            audience_name=doc["audience_name"],
            capabilities=self.capabilities,
            delegated=self.delegated,
            contract_digest=digest,
            release_label=doc.get("release_label"),
            tools=MappingProxyType(tools),
            receipt_lookup=receipt_lookup,
            application_error_codes=self.error_codes,
            canonical_bytes=canonical,
        )

    # -- envelope (§6.1, §6.2, §7.2) -------------------------------------

    def _envelope(self) -> None:
        doc = self.doc
        if doc.get("format") != BUNDLE_FORMAT:
            raise BundleRefused("bundle_format_unknown", "$.format", repr(doc.get("format")))
        if doc.get("canonicalization") != CANONICALIZATION:
            raise BundleRefused("bundle_canonicalization_unknown", "$.canonicalization")
        if "contract_digest" in doc:
            # §6.2: self-referential digest fields are "excluded by the format
            # rule", which names none; fail closed instead of guessing.
            raise BundleRefused("bundle_self_digest_unpinned", "$.contract_digest")
        for key in ("pack_id", "namespace", "api_version"):
            if not _nonempty_str(doc.get(key)):
                raise BundleRefused("bundle_invalid", f"$.{key}", "required non-empty string")
        audience = doc.get("audience_name")  # §6.1 envelope; S7 pins it from here
        if not _nonempty_str(audience) or _utf8_len(audience) > AUDIENCE_NAME_MAX_BYTES:
            raise BundleRefused("bundle_audience_invalid", "$.audience_name", repr(audience)[:80])
        identity = doc.get("identity")
        authority = identity.get("entitlement_authority") if isinstance(identity, dict) else None
        if authority not in ENTITLEMENT_AUTHORITIES:
            raise BundleRefused(
                "bundle_entitlement_authority_unknown", "$.identity.entitlement_authority"
            )
        self.capabilities = _capabilities(doc.get("capabilities"))
        self.delegated = _delegated(identity.get("delegated"))
        self.error_codes = _application_error_codes(doc.get("application_error_codes", _ABSENT))
        if "release_label" in doc and doc["release_label"] not in RELEASE_LABELS:
            raise BundleRefused("release_label_unknown", "$.release_label")
        tools = doc.get("tools")
        if not isinstance(tools, dict) or not tools:
            raise BundleRefused("bundle_invalid", "$.tools", "required non-empty object")
        if not isinstance(doc.get("events", {}), dict):
            raise BundleRefused("bundle_invalid", "$.events", "must be an object")
        if not isinstance(doc.get("internal_endpoints", {}), dict):
            raise BundleRefused("bundle_invalid", "$.internal_endpoints", "must be an object")

    # -- §12.5 counts ----------------------------------------------------

    def _counts(self, tools: dict[str, Any]) -> None:
        if len(tools) > BOUNDS["tools_per_pack"]:
            raise BundleRefused("bound_tools_per_pack", "$.tools", f"{len(tools)} tools")
        total = 0
        for name, spec in tools.items():
            modes = spec.get("modes") if isinstance(spec, dict) else None
            if not isinstance(modes, dict) or not modes:
                raise BundleRefused("tool_invalid", f"$.tools.{name}.modes", "required non-empty object")
            if len(modes) > BOUNDS["modes_per_tool"]:
                raise BundleRefused("bound_modes_per_tool", f"$.tools.{name}", f"{len(modes)} modes")
            total += len(modes)
        if total > BOUNDS["modes_per_pack"]:
            raise BundleRefused("bound_modes_per_pack", "$.tools", f"{total} modes")

    # -- tools (§6.3) -----------------------------------------------------

    def _tool(self, name: str, spec: dict[str, Any]) -> ToolSpec:
        where = f"$.tools.{name}"
        if spec.get("name") != name:
            raise BundleRefused("tool_invalid", f"{where}.name", "must equal its key")
        if not name.startswith(self.doc["namespace"] + "_"):  # §3.3 namespace prefix
            raise BundleRefused("tool_namespace_mismatch", where)
        tool_class = spec.get("class")
        if tool_class not in TOOL_CLASSES:
            raise BundleRefused("tool_class_unknown", f"{where}.class", repr(tool_class))
        description = spec.get("description")
        if not isinstance(description, str):
            raise BundleRefused("tool_invalid", f"{where}.description", "required string")
        if _utf8_len(description) > BOUNDS["tool_description_bytes"]:
            raise BundleRefused("bound_tool_description", f"{where}.description")
        if "registered_schema" in spec:
            self._schema(spec["registered_schema"], f"{where}.registered_schema", None, None)
        modes = {
            mode_name: self._mode(f"{where}.modes.{mode_name}", mode_name, mode)
            for mode_name, mode in spec["modes"].items()
        }
        file_params = self._tool_file_params(where, spec, modes)
        return ToolSpec(name, tool_class, description, MappingProxyType(modes), file_params)

    @staticmethod
    def _tool_file_params(where: str, spec: dict[str, Any], modes: Mapping[str, ModeSpec]) -> tuple[str, ...]:
        """§6.3: the tool-level ``file_params`` is the authority, and EVERY mode
        must declare exactly the same list (r0b2 B3). Top-level only, mode-level
        only or any mismatch is refused, so tools/list (the host hint) and
        tools/call (the mode that relays bytes) can never disagree."""
        at = f"{where}.file_params"
        raw = spec.get("file_params", [])
        if not isinstance(raw, list) or not all(_nonempty_str(p) for p in raw) or len(set(raw)) != len(raw):
            raise BundleRefused("file_params_invalid", at)
        declared = tuple(raw)
        for mode_name, mode in modes.items():
            if mode.file_params != declared:
                raise BundleRefused(
                    "file_params_mismatch", at, f"mode {mode_name!r} declares {list(mode.file_params)!r}"[:120]
                )
        return declared

    def _mode(self, where: str, name: str, mode: Any) -> ModeSpec:
        if not isinstance(mode, dict):
            raise BundleRefused("mode_invalid", where, "must be an object")
        if "relative_endpoint" not in mode:  # §6.3 minimum; kept under a platform_handler (wire §10.2)
            raise BundleRefused("relative_endpoint_missing", where)
        endpoint = mode["relative_endpoint"]
        if not _is_relative_path(endpoint):  # §6.4
            raise BundleRefused("relative_endpoint_not_relative", f"{where}.relative_endpoint", repr(endpoint))
        for key in _MODE_MINIMUM:
            if key not in mode:
                raise BundleRefused("mode_field_missing", f"{where}.{key}")
        if mode["confirm"] != "none":  # §0.3, §6.3, §18.13
            raise BundleRefused("confirm_not_none", f"{where}.confirm", repr(mode["confirm"]))
        if mode["idempotency"] not in IDEMPOTENCY:
            raise BundleRefused("mode_field_invalid", f"{where}.idempotency")
        if not isinstance(mode["requires_conversation_context"], bool):
            raise BundleRefused("mode_field_invalid", f"{where}.requires_conversation_context")
        if not _str_list(mode["required_capabilities"]):
            raise BundleRefused("mode_field_invalid", f"{where}.required_capabilities")
        self._declared(mode["required_capabilities"], f"{where}.required_capabilities")
        handler = None
        if "platform_handler" in mode:  # wire §10.2: closed discriminator
            handler = mode["platform_handler"]
            if handler not in PLATFORM_HANDLERS:
                raise BundleRefused("platform_handler_unknown", f"{where}.platform_handler", repr(handler))
        description = mode.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise BundleRefused("mode_field_invalid", f"{where}.description")
            if _utf8_len(description) > BOUNDS["mode_description_bytes"]:
                raise BundleRefused("bound_mode_description", f"{where}.description")

        input_schema = mode["input_schema"]
        output_schema = mode["output_schema"]
        self._schema(input_schema, f"{where}.input_schema", BOUNDS["input_schema_bytes"], "bound_input_schema_bytes")
        resolved_output = self._schema(
            output_schema, f"{where}.output_schema", BOUNDS["output_schema_bytes"], "bound_output_schema_bytes"
        )
        discriminator, consts = self._union(where, mode)
        branches = self._branches(where, mode, discriminator, consts)
        if any(not b.read_only for b in branches) and not _has_undo_receipt(resolved_output):
            raise BundleRefused("receipt_undo_fields_missing", f"{where}.output_schema")
        return ModeSpec(
            name=name,
            discriminator=discriminator,
            branches=branches,
            relative_endpoint=endpoint,
            platform_handler=handler,
            idempotency=mode["idempotency"],
            requires_conversation_context=mode["requires_conversation_context"],
            input_schema=input_schema,
            output_schema=output_schema,
            file_params=self._file_params(where, mode, input_schema),
        )

    # -- host-file parameters (§6.3, §14, §14.5) ------------------------------

    @staticmethod
    def _file_params(where: str, mode: dict[str, Any], input_schema: Any) -> tuple[str, ...]:
        """A mode's ``file_params``: unique names of top-level input properties
        that are host-file objects requiring ``download_url``; at most ONE
        (§14.5 "file parameters: one per call")."""
        if "file_params" not in mode:
            return ()
        raw = mode["file_params"]
        at = f"{where}.file_params"
        if not isinstance(raw, list) or not all(_nonempty_str(p) for p in raw) or len(set(raw)) != len(raw):
            raise BundleRefused("file_params_invalid", at)
        if len(raw) > MAX_FILE_PARAMS:
            raise BundleRefused("bound_file_params", at, f"{len(raw)} file parameters")
        props = input_schema.get("properties") if isinstance(input_schema, dict) else None
        for name in raw:
            sub = props.get(name) if isinstance(props, dict) else None
            ok = (
                isinstance(sub, dict)
                and sub.get("type") == "object"
                and isinstance(sub.get("properties"), dict)
                and "download_url" in sub["properties"]
                and isinstance(sub.get("required"), list)
                and "download_url" in sub["required"]
            )
            if not ok:
                raise BundleRefused("file_params_invalid", at, repr(name)[:80])
        return tuple(raw)

    # -- closed unions (§6.3.2) -------------------------------------------

    def _union(self, where: str, mode: dict[str, Any]) -> tuple[str | None, list[Any]]:
        schema = mode["input_schema"]
        declared = mode.get("discriminator")
        one_of = schema.get("oneOf") if isinstance(schema, dict) else None
        if one_of is None:
            if declared is not None:
                raise BundleRefused("union_discriminator_undeclared", f"{where}.discriminator", "no union")
            return None, [None]
        if not _nonempty_str(declared):
            raise BundleRefused("union_discriminator_undeclared", f"{where}.discriminator")
        if not isinstance(one_of, list) or not one_of:
            raise BundleRefused("union_invalid", f"{where}.input_schema.oneOf")
        consts: list[Any] = []
        seen: set[bytes] = set()
        for i, branch in enumerate(one_of):
            bwhere = f"{where}.input_schema.oneOf[{i}]"
            if not isinstance(branch, dict):
                raise BundleRefused("union_invalid", bwhere)
            props = branch.get("properties")
            required = branch.get("required")
            if not isinstance(props, dict) or not isinstance(required, list):
                raise BundleRefused("union_invalid", bwhere, "branch declares its own required / properties")
            if declared not in required:
                raise BundleRefused("union_discriminator_not_required", bwhere)
            disc_schema = props.get(declared)
            if not isinstance(disc_schema, dict) or "const" not in disc_schema:
                raise BundleRefused("union_branch_const_missing", bwhere)
            if branch.get("additionalProperties") is not False:
                raise BundleRefused("union_branch_not_closed", bwhere)
            key = canonicalize(disc_schema["const"])
            if key in seen:
                raise BundleRefused("union_const_not_distinct", bwhere, repr(disc_schema["const"]))
            seen.add(key)
            consts.append(disc_schema["const"])
        # One discriminator per mode: no other property may carry a const
        # that varies across branches (a hidden second routing axis).
        names = {n for b in one_of for n in b["properties"]} - {declared}
        for prop in sorted(names):
            values: set[bytes] = set()
            has_const = False
            for branch in one_of:
                if prop not in branch["properties"]:
                    continue
                fixed = _fixed_value(branch["properties"][prop])
                if fixed is _NOT_FIXED:
                    values.add(b"\x00not-fixed")
                else:
                    has_const = True
                    values.add(canonicalize(fixed))
            if has_const and len(values) > 1:
                raise BundleRefused("union_second_discriminator", f"{where}.input_schema", prop)
        return declared, consts

    # -- per-branch effect metadata and the safe floor (§6.3.1, §6.3.2) ---

    def _branches(
        self, where: str, mode: dict[str, Any], discriminator: str | None, consts: list[Any]
    ) -> tuple[BranchSpec, ...]:
        meta = mode.get("branches")
        if not isinstance(meta, list) or not meta:
            raise BundleRefused("effect_metadata_missing", f"{where}.branches")
        by_value: dict[bytes, dict[str, Any]] = {}
        const_keys = {canonicalize(c) for c in consts}
        for i, entry in enumerate(meta):
            bwhere = f"{where}.branches[{i}]"
            if not isinstance(entry, dict):
                raise BundleRefused("effect_metadata_invalid", bwhere)
            for key in _BRANCH_METADATA:
                if key not in entry:
                    raise BundleRefused("effect_metadata_missing", f"{bwhere}.{key}")
            value_key = canonicalize(entry["discriminator_value"])
            if value_key not in const_keys or value_key in by_value:
                raise BundleRefused(
                    "effect_metadata_unmatched", f"{bwhere}.discriminator_value",
                    repr(entry["discriminator_value"]),
                )
            by_value[value_key] = entry
        specs: list[BranchSpec] = []
        branch_ids: set[str] = set()
        for const in consts:
            entry = by_value.get(canonicalize(const))
            if entry is None:
                raise BundleRefused("effect_metadata_missing", f"{where}.branches", f"no metadata for {const!r}")
            spec = self._branch(f"{where}.branches[{const!r}]", entry)
            if spec.branch_id in branch_ids:
                raise BundleRefused("effect_branch_id_duplicate", f"{where}.branches", spec.branch_id)
            branch_ids.add(spec.branch_id)
            specs.append(spec)
        return tuple(specs)

    def _branch(self, where: str, e: dict[str, Any]) -> BranchSpec:
        if not _nonempty_str(e["branch_id"]):
            raise BundleRefused("effect_metadata_invalid", f"{where}.branch_id")
        classes = e["effect_classes"]
        if not _str_list(classes) or not classes:
            raise BundleRefused("effect_metadata_invalid", f"{where}.effect_classes")
        if FLOOR_EFFECT_CLASSES.intersection(classes):  # §0.3
            raise BundleRefused("effect_class_floor", f"{where}.effect_classes", repr(classes))
        if not SAFE_EFFECT_CLASSES.issuperset(classes):  # closed vocabulary
            raise BundleRefused("effect_class_unknown", f"{where}.effect_classes", repr(classes))
        for flag in ("reversible", "external_effect", "downstream_irreversible"):
            if not isinstance(e[flag], bool):
                raise BundleRefused("effect_metadata_invalid", f"{where}.{flag}")
        if e["reversible"] is not True:
            raise BundleRefused("effect_not_reversible", f"{where}.reversible")
        if e["external_effect"] is not False:
            raise BundleRefused("effect_external", f"{where}.external_effect")
        if e["downstream_irreversible"] is not False:
            raise BundleRefused("effect_downstream_irreversible", f"{where}.downstream_irreversible")
        affected = e["max_affected_entities"]
        if not _is_int(affected) or affected < 1:
            raise BundleRefused("effect_max_affected_invalid", f"{where}.max_affected_entities", repr(affected))
        if affected > self.caps.max_affected_entities:
            raise BundleRefused("effect_max_affected_over_cap", f"{where}.max_affected_entities")
        fan_out = e["max_fan_out"]
        if not _is_int(fan_out) or fan_out < 0:
            raise BundleRefused("effect_max_fan_out_invalid", f"{where}.max_fan_out", repr(fan_out))
        if fan_out > self.caps.max_fan_out:
            raise BundleRefused("effect_max_fan_out_over_cap", f"{where}.max_fan_out")
        if not _str_list(e["required_capabilities"]):
            raise BundleRefused("effect_metadata_invalid", f"{where}.required_capabilities")
        self._declared(e["required_capabilities"], f"{where}.required_capabilities")
        spec = BranchSpec(
            branch_id=e["branch_id"],
            discriminator_value=e["discriminator_value"],
            effect_classes=tuple(classes),
            reversible=True,
            external_effect=False,
            downstream_irreversible=False,
            max_affected_entities=affected,
            max_fan_out=fan_out,
            required_capabilities=tuple(e["required_capabilities"]),
            prior_state_retention=e.get("prior_state_retention"),
        )
        if not spec.read_only:  # §6.3.1 condition 3; reads satisfy the law trivially
            if "prior_state_retention" not in e:
                raise BundleRefused("effect_metadata_missing", f"{where}.prior_state_retention")
            if not _retention_ok(e["prior_state_retention"]):
                raise BundleRefused(
                    "prior_state_retention_below_floor", f"{where}.prior_state_retention",
                    repr(e["prior_state_retention"]),
                )
        return spec

    def _declared(self, required: list[str], where: str) -> None:
        """§7.2 / §7.3: only the pack's declared capability vocabulary."""
        undeclared = sorted(set(required) - set(self.capabilities))
        if undeclared:
            raise BundleRefused("capability_undeclared", where, repr(undeclared))

    # -- events (§12.5) ----------------------------------------------------

    def _events(self, events: dict[str, Any]) -> None:
        for name, spec in events.items():
            where = f"$.events.{name}"
            if not isinstance(spec, dict):
                raise BundleRefused("event_invalid", where)
            description = spec.get("description")
            if isinstance(description, str) and _utf8_len(description) > BOUNDS["mode_description_bytes"]:
                raise BundleRefused("bound_event_description", f"{where}.description")
            if "payload_schema" not in spec:
                raise BundleRefused("event_invalid", f"{where}.payload_schema", "required")
            self._schema(
                spec["payload_schema"], f"{where}.payload_schema",
                BOUNDS["event_schema_bytes"], "bound_event_schema_bytes",
            )

    # -- internal_endpoints.receipt_lookup (wire §10.2 / §10.3) -----------

    def _receipt_lookup(self, tools: Mapping[str, ToolSpec]) -> str | None:
        endpoints = self.doc.get("internal_endpoints", {})
        needs = any(
            m.platform_handler == "mutation_resolver" for t in tools.values() for m in t.modes.values()
        )
        if "receipt_lookup" not in endpoints:
            if needs:  # "There is no default"
                raise BundleRefused("receipt_lookup_missing", "$.internal_endpoints.receipt_lookup")
            return None
        value = endpoints["receipt_lookup"]
        if not _is_relative_path(value):  # a BARE relative path string; no object form
            raise BundleRefused("receipt_lookup_not_bare_path", "$.internal_endpoints.receipt_lookup", repr(value))
        return value

    # -- schemas (§6.6, §12.5) ----------------------------------------------

    def _schema(self, schema: Any, where: str, limit: int | None, code: str | None) -> Any:
        if not isinstance(schema, dict):
            raise BundleRefused("schema_invalid", where, "must be an object")
        _check_refs(schema, schema, where)
        resolved = _Resolver(schema, where).resolve(schema)
        for name, sub in schema.get("$defs", {}).items() if isinstance(schema.get("$defs"), dict) else ():
            _Resolver(schema, where).resolve(sub)  # unreachable cycles still refuse
        size = len(canonicalize(resolved))
        if limit is not None and size > limit:
            raise BundleRefused(code or "bound_schema_bytes", where, f"{size} bytes")
        if _schema_depth(resolved) > BOUNDS["schema_depth"]:
            raise BundleRefused("bound_schema_depth", where)
        if _schema_nodes(resolved) > BOUNDS["schema_nodes"]:
            raise BundleRefused("bound_schema_nodes", where)
        self.aggregate_bytes += size
        return resolved


# --------------------------------------------------------------------------
# $ref handling: local JSON pointers only, cycles refused


def _check_refs(node: Any, root: dict[str, Any], where: str) -> None:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "$ref" in cur:
                ref = cur["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#"):
                    raise BundleRefused("schema_ref_not_local", where, repr(ref))
                _pointer(root, ref, where)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _pointer(root: dict[str, Any], ref: str, where: str) -> Any:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        raise BundleRefused("schema_ref_unresolved", where, ref)
    target: Any = root
    for raw in ref[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and part in target:
            target = target[part]
        elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
            target = target[int(part)]
        else:
            raise BundleRefused("schema_ref_unresolved", where, ref)
    return target


class _Resolver:
    def __init__(self, root: dict[str, Any], where: str) -> None:
        self.root = root
        self.where = where
        self.budget = _RESOLVE_BUDGET

    def resolve(self, node: Any, active: tuple[str, ...] = ()) -> Any:
        self.budget -= 1
        if self.budget < 0:
            raise BundleRefused("bound_schema_nodes", self.where, "resolution budget exhausted")
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref in active:
                    raise BundleRefused("schema_ref_cycle", self.where, ref)
                target = self.resolve(_pointer(self.root, ref, self.where), active + (ref,))
                siblings = {k: self.resolve(v, active) for k, v in node.items() if k not in ("$ref", "$defs")}
                if not siblings:
                    return target
                merged = dict(target) if isinstance(target, dict) else {"$ref-target": target}
                merged.update(siblings)
                return merged
            return {k: self.resolve(v, active) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [self.resolve(v, active) for v in node]
        return node


def _subschemas(schema: dict[str, Any]) -> list[Any]:
    kids: list[Any] = []
    for key, value in schema.items():
        if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
            kids.extend(value.values())
        elif key in _SUBSCHEMA_LISTS and isinstance(value, list):
            kids.extend(value)
        elif key in _SUBSCHEMA_ONE:
            if isinstance(value, dict):
                kids.append(value)
            elif isinstance(value, list):
                kids.extend(value)
    return [k for k in kids if isinstance(k, dict)]


def _schema_depth(schema: Any) -> int:
    if not isinstance(schema, dict):
        return 0
    return 1 + max((_schema_depth(k) for k in _subschemas(schema)), default=0)


def _schema_nodes(schema: Any) -> int:
    if not isinstance(schema, dict):
        return 0
    return 1 + sum(_schema_nodes(k) for k in _subschemas(schema))


def _has_undo_receipt(resolved: Any) -> bool:
    """Some object schema declares AND requires both undo receipt fields."""
    stack = [resolved]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            props = cur.get("properties")
            required = cur.get("required")
            if (
                isinstance(props, dict)
                and isinstance(required, list)
                and all(f in props and f in required for f in UNDO_RECEIPT_FIELDS)
            ):
                return True
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return False


# --------------------------------------------------------------------------
# small predicates

_NOT_FIXED = object()


def _fixed_value(prop: Any) -> Any:
    # Only ``const`` is a routing axis. A data ``enum`` (even one-valued, like
    # the CRM link ``type`` of lead_intent) cannot be told apart from a hidden
    # behaviour selector mechanically; §6.3.2 leaves that to review.
    if isinstance(prop, dict) and "const" in prop:
        return prop["const"]
    return _NOT_FIXED


def _is_relative_path(value: Any) -> bool:
    """§6.4: an absolute-path reference on the binding's pinned origin only."""
    if not isinstance(value, str) or not value.startswith("/") or len(value) < 2:
        return False
    segments = value[1:].split("/")
    return all(s not in (".", "..") and _PATH_SEGMENT.match(s) for s in segments)


def _retention_ok(value: Any) -> bool:
    if value == RETENTION_UNLIMITED:
        return True
    return _is_int(value) and value >= RETENTION_FLOOR_DAYS


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))
