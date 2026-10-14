"""The pinned AGENT PLUGIN of an application pack + the client playbook (#1134 ``load_plugin``).

r0b2 rulings 3c62dcdf / db9cfd4b / 5f3224aa and the single-tool refinement. The
application vendor ships a SKILL-ONLY host plugin (``plugin.json`` +
``skills/<skill>/SKILL.md`` + optional ``skills/<skill>/references/*.md``; text
only, never an MCP server, app or script) INSIDE its sealed pack, in the
optional ``agent_plugin`` section. The pack's ``contract_digest`` pins it.

AIOS exposes ONE platform-native, read-only tool, :data:`LOAD_PLUGIN_TOOL`, on
the app connector, outside the pack's ``bundle.tools``. It returns the pinned
plugin material (re-verified here before every serve) and the client's current
playbook. It NEVER installs, creates, updates, connects or enables a host
plugin: the host's own plugin creator is a separate, explicit, user-authorized
act.

This module is pure (no I/O): the section validator, the canonical manifest,
the load_plugin result projection and the playbook RESPONSE interpreter. The
playbook control call itself rides the runtime's existing CRM control carrier
(``runtime._AppTransport.control``, governed egress), never a new egress site.

**``agent_plugin`` (closed members; every failure refuses the pack):**

* ``format_version`` = 1; ``plugin_name`` / ``skill_name`` kebab-case <= 64;
  ``display_name`` 1..128 UTF-8 bytes, no control characters; ``version``
  SemVer 2.0; ``manifest_sha256`` 64 lowercase hex;
* ``files`` = a non-empty list of exactly ``{path, length, sha256, content}``,
  ``content`` an inline UTF-8 string;
* text only: no NUL and no control character (Unicode ``Cc``) except
  ``\\n`` ``\\t`` ``\\r``; no lone surrogate;
* paths are normalized relative POSIX: no leading ``/``, no ``\\``, no ``:``,
  no empty / ``.`` / ``..`` segment, nothing symlink-ish (``.lnk``), no
  duplicate and no case-colliding (casefold) path;
* the shape: exactly one ``plugin.json``, exactly one
  ``skills/<skill_name>/SKILL.md`` and any number of
  ``skills/<skill_name>/references/<name>.md`` (one level, a name that does not
  start with a dot and is not the reserved ``playbook.md`` / ``local.md``, in
  any letter case); anything else
  (``mcp.json``, ``.app.json``, scripts, other skills, nested references) is
  refused;
* each file <= 64 KiB, all files <= 256 KiB (UTF-8 bytes);
* ``length`` and ``sha256`` are recomputed from ``content``;
* ``manifest_sha256`` = SHA-256 of the canonical manifest: one line
  ``<path>\\t<length>\\t<sha256>\\n`` per file, sorted by the path's UTF-8
  bytes, UTF-8 encoded;
* ``plugin.json`` is one JSON object (no duplicate member) whose ``name`` /
  ``version`` equal ``plugin_name`` / ``version`` and that declares no
  ``mcpServers`` / ``apps`` / ``hooks``; ``SKILL.md`` opens with a ``---``
  frontmatter block whose ``name`` equals ``skill_name``.

**Playbook (CAT contract).** ``POST <pinned origin><internal_endpoints.playbook>``,
an AIDOCS control assertion of purpose :data:`PLAYBOOK_PURPOSE` (the
assertion names tenant / binding / pack / contract_digest / sub), body exactly
``{}``. Responses:

* 200 exactly ``{version, sha256, updated_at, updated_by_role, text, pack_id,
  contract_digest}`` -> ``current`` once ``sha256(text)``, the caps and the
  binding identity all check;
* 404 ``{ok:false, error:{code:"crm_playbook_none", message}, pack_id,
  contract_digest}`` -> ``absent``;
* 409 ``{ok:false, error:{code:"crm_playbook_incompatible", message}, version,
  sha256, problems:[{tool, mode|null, reason}], pack_id, contract_digest}`` ->
  ``incompatible`` (the problems are surfaced; there is no text);
* anything else -> ``unavailable``. A stale playbook is never served as
  current: the pointer is fetched on every call.

**load_plugin** (operator ruling, final): no argument at all. One result: the
identity, the verified vendor ``files`` (never changed server-side), the
canonical ``manifest``, the ``playbook`` state, an ALWAYS-generated
``playbook_file`` (``skills/<skill_name>/references/playbook.md``: the
provenance header of :func:`playbook_file_content`, then the org text only when
``current``; length + sha256 over the exact installed bytes) listed SEPARATELY
from the vendor manifest so ``workflow_digest`` stays the vendor's pinned
``manifest_sha256``, ``org_package_digest`` (the manifest format over vendor
files + playbook.md), ``connector.mcp_url``, ``install_rules`` and the explicit
``installable`` / ``install_blocked_reason``: ``current`` and ``absent`` are
installable; ``unavailable`` / ``incompatible`` STOP (``playbook_unavailable`` /
``playbook_incompatible``), so an outage never replaces a working local plugin.

Header generation is TOTAL over accepted inputs: a plugin pack's ``pack_id`` and
the plugin ``version`` match ``[A-Za-z0-9._:+-]{1,128}`` (CAT's build bound) at
import, the other header sources are kebab names / fixed-width digests, and a
CRM playbook ``version`` outside 1..2^53-1 is ``unavailable``.

The ChatGPT app id of the CRM connector is created per user account, so only the
HOST can declare the plugin's app dependency (.app.json +
``extensions.com.openai.apps``). The vendor files never do. The host cannot
resolve an app by MCP URL, so the install rules bind ONLY to the app id the user
selected (app picker / @-mention, confirmed by the host's app lookup) and stop
otherwise; ``connector.mcp_url`` (the request's own canonical app resource,
server-derived) is for audit and future host resolvers, and must match any MCP
URL the host can show.

plugin.json must also be skill-only IN EFFECT: only the top-level members of
``_MANIFEST_TOP_KEYS``; ``interface`` (top level or under
``extensions.com.openai``) holds display keys only; ``extensions`` may hold only
``com.openai`` (dotted or nested) with ``interface`` and at most ``apps: null``;
no app / MCP / hook / connector / command / agent member at any depth.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .jcs import canonicalize

__all__ = [
    "LOAD_PLUGIN_TOOL",
    "PLAYBOOK_PURPOSE",
    "AgentPlugin",
    "AgentPluginRefused",
    "Playbook",
    "PlaybookCache",
    "PlaybookState",
    "PluginFile",
    "canonical_manifest",
    "connector_url_for",
    "header_token_ok",
    "interpret_playbook_response",
    "load_plugin_arguments_ok",
    "load_plugin_body",
    "load_plugin_tool",
    "manifest_digest",
    "no_plugin_body",
    "parse_agent_plugin",
    "playbook_request_body",
    "plugin_hint",
    "verify_agent_plugin",
]

FORMAT_VERSION = 1
KIB = 1024
FILE_MAX_BYTES = 64 * KIB
TOTAL_MAX_BYTES = 256 * KIB
NAME_MAX = 64
DISPLAY_NAME_MAX_BYTES = 128
PLAYBOOK_TEXT_MAX_BYTES = 16 * KIB
PLAYBOOK_PROBLEMS_MAX = 64
_PROBLEM_FIELD_MAX = 128
_PROBLEM_REASON_MAX = 512

LOAD_PLUGIN_TOOL = "load_plugin"
PLAYBOOK_FILE_NAME = "playbook.md"  # the generated file; reserved in the vendor shape
# Operator ruling (option B): user customisations live ONLY in this host-local
# file. load_plugin never generates it and a vendor bundle may never ship it.
LOCAL_FILE_NAME = "local.md"
_RESERVED_REFERENCE_NAMES = frozenset({PLAYBOOK_FILE_NAME, LOCAL_FILE_NAME})  # compared casefolded
PLAYBOOK_PURPOSE = "playbook_read"
PLAYBOOK_ROLES = frozenset({"owner", "manager"})
PLAYBOOK_NONE = "crm_playbook_none"
PLAYBOOK_INCOMPATIBLE = "crm_playbook_incompatible"

PLUGIN_MANIFEST = "plugin.json"
# r0b2 (review of 4b8969b30): the EFFECTIVE manifest must be skill-only. An
# allowlist, not a denylist: Agent Plugins 1.0 declares an app dependency at
# extensions.com.openai.apps (documented with ./.app.json), so any member that is
# not plain display metadata is refused. A deep key scan backs it up for nested
# capability declarations.
_MANIFEST_TOP_KEYS = frozenset({
    "$schema", "name", "version", "description", "author", "homepage", "repository", "license",
    "keywords", "skills", "interface", "extensions",
})
_INTERFACE_KEYS = frozenset({
    "displayName", "shortDescription", "longDescription", "developerName", "category", "capabilities",
    "websiteURL", "privacyPolicyURL", "termsOfServiceURL", "defaultPrompt", "brandColor", "composerIcon",
    "logo", "screenshots",
})
_OPENAI_EXTENSION_KEYS = frozenset({"interface", "apps"})  # apps admitted ONLY as null
_SKILLS_VALUES = frozenset({"./skills/", "./skills", "skills/", "skills"})
# Normalized (lower case, no ``_`` ``-`` ``.``) member names that declare a
# capability beyond skills, refused at ANY depth of plugin.json.
_CAPABILITY_KEYS = frozenset({
    "mcpservers", "mcp", "servers", "apps", "app", "hooks", "hook", "connectors", "connector",
    "commands", "agents", "lspservers", "outputstyles", "tools",
})
_SECTION_KEYS = frozenset(
    {"format_version", "plugin_name", "display_name", "version", "skill_name", "manifest_sha256", "files"}
)
_FILE_KEYS = frozenset({"path", "length", "sha256", "content"})
_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_REFERENCE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\.md")
_RFC3339_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
_ALLOWED_CONTROLS = frozenset("\n\t\r")

_PLAYBOOK_CURRENT_KEYS = frozenset(
    {"version", "sha256", "updated_at", "updated_by_role", "text", "pack_id", "contract_digest"}
)
_PLAYBOOK_NONE_KEYS = frozenset({"ok", "error", "pack_id", "contract_digest"})
_PLAYBOOK_INCOMPATIBLE_KEYS = frozenset(
    {"ok", "error", "version", "sha256", "problems", "pack_id", "contract_digest"}
)


class AgentPluginRefused(ValueError):
    """The ``agent_plugin`` section (or a served plugin) is refused. ``code`` is stable."""

    def __init__(self, code: str, where: str, detail: str = "") -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code} at {where}" + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class PluginFile:
    path: str
    length: int
    sha256: str
    content: str = field(repr=False)


@dataclass(frozen=True)
class AgentPlugin:
    """The validated, pinned plugin. ``files`` are in canonical (manifest) order."""

    format_version: int
    plugin_name: str
    display_name: str
    version: str
    skill_name: str
    manifest_sha256: str
    files: tuple[PluginFile, ...]


# -- the canonical manifest ---------------------------------------------------------------------


def _path_key(path: str) -> bytes:
    return path.encode("utf-8")


def canonical_manifest(entries: Iterable[tuple[str, int, str]]) -> bytes:
    """``<path>\\t<length>\\t<sha256>\\n`` per file, sorted by the path's UTF-8 bytes."""
    rows = sorted(entries, key=lambda e: _path_key(e[0]))
    return "".join(f"{path}\t{length}\t{sha}\n" for path, length, sha in rows).encode("utf-8")


def manifest_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    return hashlib.sha256(canonical_manifest(entries)).hexdigest()


def _entries(plugin: AgentPlugin) -> list[tuple[str, int, str]]:
    return [(f.path, f.length, f.sha256) for f in plugin.files]


# -- text / name predicates ---------------------------------------------------------------------


def _text_ok(text: Any) -> bool:
    """UTF-8 encodable text with no control character except \\n \\t \\r."""
    if type(text) is not str:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate is not text
        return False
    return not any(ch not in _ALLOWED_CONTROLS and unicodedata.category(ch) == "Cc" for ch in text)


def _kebab(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= NAME_MAX and _KEBAB.fullmatch(value) is not None


def _is_int(value: Any) -> bool:
    return type(value) is int


# -- the section validator ----------------------------------------------------------------------


def _invalid(where: str, detail: str = "") -> AgentPluginRefused:
    return AgentPluginRefused("agent_plugin_invalid", where, detail)


def _envelope(value: Any, where: str) -> None:
    if not isinstance(value, dict) or set(value) != _SECTION_KEYS:
        raise _invalid(where, "closed member set")
    if not _is_int(value["format_version"]) or value["format_version"] != FORMAT_VERSION:
        raise _invalid(f"{where}.format_version")
    for key in ("plugin_name", "skill_name"):
        if not _kebab(value[key]):
            raise _invalid(f"{where}.{key}", "kebab-case <= 64")
    name = value["display_name"]
    if type(name) is not str or not name or not _text_ok(name) or "\n" in name or "\r" in name \
            or "\t" in name or len(name.encode("utf-8")) > DISPLAY_NAME_MAX_BYTES:
        raise _invalid(f"{where}.display_name")
    if (
        type(value["version"]) is not str
        or len(value["version"]) > HEADER_TOKEN_MAX  # the header-token bound (CAT: 1..128)
        or _SEMVER.fullmatch(value["version"]) is None
    ):
        raise _invalid(f"{where}.version", "semver")
    if type(value["manifest_sha256"]) is not str or _HEX64.fullmatch(value["manifest_sha256"]) is None:
        raise _invalid(f"{where}.manifest_sha256")
    if not isinstance(value["files"], list) or not value["files"]:
        raise _invalid(f"{where}.files", "non-empty list")


def _check_path(path: Any, where: str) -> str:
    """Normalized relative POSIX, nothing symlink-ish. Returns the path."""
    refuse = AgentPluginRefused("agent_plugin_path_refused", where, repr(path)[:80])
    if type(path) is not str or not path or path.startswith("/") or "\\" in path or ":" in path:
        raise refuse
    if not _text_ok(path) or any(ch in path for ch in "\n\t\r"):
        raise refuse
    for segment in path.split("/"):
        if segment in ("", ".", "..") or _SEGMENT.fullmatch(segment) is None:
            raise refuse
        if segment.lower().endswith(".lnk"):
            raise refuse
    return path


def _shape(path: str, skill: str) -> str:
    """The file's role in the skill-only shape, or None."""
    if path == PLUGIN_MANIFEST:
        return "manifest"
    prefix = f"skills/{skill}/"
    if path == prefix + "SKILL.md":
        return "skill"
    ref_prefix = prefix + "references/"
    if path.startswith(ref_prefix):
        name = path[len(ref_prefix):]
        if (
            "/" not in name
            and _REFERENCE_NAME.fullmatch(name) is not None
            and not name.startswith(".")
            # reserved in ANY letter case (CAT's build agrees): the generated
            # playbook file and the host-local user customisation file
            and name.casefold() not in _RESERVED_REFERENCE_NAMES
        ):
            return "reference"
    return ""


def _file(entry: Any, skill: str, where: str) -> tuple[PluginFile, str]:
    if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
        raise _invalid(where, "closed member set {path, length, sha256, content}")
    path = _check_path(entry["path"], f"{where}.path")
    content = entry["content"]
    if type(content) is not str:
        raise _invalid(f"{where}.content", "inline UTF-8 string")
    if not _is_int(entry["length"]) or type(entry["sha256"]) is not str or _HEX64.fullmatch(entry["sha256"]) is None:
        raise _invalid(where, "length / sha256")
    if not _text_ok(content):
        raise AgentPluginRefused("agent_plugin_file_refused", f"{where}.content", "text only")
    raw = content.encode("utf-8")
    if len(raw) > FILE_MAX_BYTES:
        raise AgentPluginRefused("agent_plugin_too_large", where, f"{len(raw)} bytes")
    if entry["length"] != len(raw) or entry["sha256"] != hashlib.sha256(raw).hexdigest():
        raise AgentPluginRefused("agent_plugin_hash_mismatch", where)
    role = _shape(path, skill)
    if not role:
        raise AgentPluginRefused("agent_plugin_shape_refused", f"{where}.path", path[:80])
    return PluginFile(path, len(raw), entry["sha256"], content), role


def _no_duplicate_members(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate member")
        out[key] = value
    return out


def _check_manifest_json(content: str, plugin_name: str, version: str, where: str) -> None:
    refuse = AgentPluginRefused("agent_plugin_identity_mismatch", where, "plugin.json")
    try:
        doc = json.loads(content, object_pairs_hook=_no_duplicate_members)
    except ValueError:
        raise refuse from None
    if not isinstance(doc, dict) or doc.get("name") != plugin_name or doc.get("version") != version:
        raise refuse
    if not _skill_only(doc):
        raise AgentPluginRefused("agent_plugin_not_skill_only", where, "plugin.json")


def _norm_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch not in "_-.")


def _capability_free(value: Any, *, top: bool = False) -> bool:
    """No capability member at any depth (``extensions.com.openai.apps: null`` is
    the one tolerated spelling and is checked structurally by the caller)."""
    if isinstance(value, dict):
        for key, sub in value.items():
            if _norm_key(key) in _CAPABILITY_KEYS and not (key == "apps" and sub is None and not top):
                return False
            if not _capability_free(sub):
                return False
    elif isinstance(value, list):
        return all(_capability_free(v) for v in value)
    return True


def _display_value(value: Any) -> bool:
    if type(value) is str:
        return _text_ok(value)
    return isinstance(value, list) and all(type(v) is str and _text_ok(v) for v in value)


def _interface_ok(value: Any) -> bool:
    return isinstance(value, dict) and set(value) <= _INTERFACE_KEYS and all(
        _display_value(v) for v in value.values()
    )


def _extensions_ok(value: Any) -> bool:
    """Only ``com.openai`` (dotted or nested ``com`` -> ``openai``), holding only a
    display ``interface`` and, at most, ``apps: null``."""
    if not isinstance(value, dict):
        return False
    openai: list = []
    for key, sub in value.items():
        if key == "com.openai":
            openai.append(sub)
        elif key == "com" and isinstance(sub, dict) and set(sub) == {"openai"}:
            openai.append(sub["openai"])
        else:
            return False
    for ext in openai:
        if not isinstance(ext, dict) or not set(ext) <= _OPENAI_EXTENSION_KEYS:
            return False
        if "apps" in ext and ext["apps"] is not None:
            return False
        if "interface" in ext and not _interface_ok(ext["interface"]):
            return False
    return True


def _skill_only(doc: dict) -> bool:
    """The effective plugin.json declares skills and display metadata only."""
    if not set(doc) <= _MANIFEST_TOP_KEYS or not _capability_free(doc, top=True):
        return False
    if "skills" in doc and (type(doc["skills"]) is not str or doc["skills"] not in _SKILLS_VALUES):
        return False
    if "interface" in doc and not _interface_ok(doc["interface"]):
        return False
    return "extensions" not in doc or _extensions_ok(doc["extensions"])


def _frontmatter_name(content: str) -> Optional[str]:
    lines = content.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    name = None
    for line in lines[1:]:
        line = line.rstrip("\r")
        if line == "---":
            return name
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name" and name is None:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            name = value
    return None  # unterminated


def _check(plugin: AgentPlugin, roles: dict[str, str], where: str) -> None:
    """The whole-plugin checks shared by import and serve."""
    folded: set[str] = set()
    for f in plugin.files:
        key = f.path.casefold()
        if key in folded:
            raise AgentPluginRefused("agent_plugin_path_refused", f"{where}.files", f"duplicate {f.path!r}"[:80])
        folded.add(key)
    kinds = sorted(roles.values())
    if kinds.count("manifest") != 1 or kinds.count("skill") != 1:
        raise AgentPluginRefused("agent_plugin_shape_refused", f"{where}.files", "plugin.json + SKILL.md required")
    total = sum(f.length for f in plugin.files)
    if total > TOTAL_MAX_BYTES:
        raise AgentPluginRefused("agent_plugin_too_large", f"{where}.files", f"{total} bytes total")
    if manifest_digest(_entries(plugin)) != plugin.manifest_sha256:
        raise AgentPluginRefused("agent_plugin_manifest_mismatch", f"{where}.manifest_sha256")
    by_role = {roles[f.path]: f for f in plugin.files if roles[f.path] != "reference"}
    _check_manifest_json(by_role["manifest"].content, plugin.plugin_name, plugin.version, f"{where}.files")
    if _frontmatter_name(by_role["skill"].content) != plugin.skill_name:
        raise AgentPluginRefused("agent_plugin_identity_mismatch", f"{where}.files", "SKILL.md name")


def parse_agent_plugin(value: Any, where: str = "$.agent_plugin") -> AgentPlugin:
    """Validate the ``agent_plugin`` section; refuse (``AgentPluginRefused``) on any failure."""
    _envelope(value, where)
    skill = value["skill_name"]
    files: list[PluginFile] = []
    roles: dict[str, str] = {}
    for i, entry in enumerate(value["files"]):
        f, role = _file(entry, skill, f"{where}.files[{i}]")
        if f.path in roles:
            raise AgentPluginRefused("agent_plugin_path_refused", f"{where}.files[{i}].path", "duplicate")
        roles[f.path] = role
        files.append(f)
    plugin = AgentPlugin(
        format_version=value["format_version"],
        plugin_name=value["plugin_name"],
        display_name=value["display_name"],
        version=value["version"],
        skill_name=skill,
        manifest_sha256=value["manifest_sha256"],
        files=tuple(sorted(files, key=lambda f: _path_key(f.path))),
    )
    _check(plugin, roles, where)
    return plugin


def verify_agent_plugin(plugin: Any) -> None:
    """Serve-time re-verification: every rule again, every hash recomputed from content."""
    where = "served.agent_plugin"
    if not isinstance(plugin, AgentPlugin):
        raise _invalid(where)
    reparsed = parse_agent_plugin(
        {
            "format_version": plugin.format_version, "plugin_name": plugin.plugin_name,
            "display_name": plugin.display_name, "version": plugin.version, "skill_name": plugin.skill_name,
            "manifest_sha256": plugin.manifest_sha256,
            "files": [{"path": f.path, "length": f.length, "sha256": f.sha256, "content": f.content}
                      for f in plugin.files],
        },
        where,
    )
    if reparsed != plugin:  # canonical order / identity drift
        raise AgentPluginRefused("agent_plugin_manifest_mismatch", where)


# -- the platform tool --------------------------------------------------------------------------

def load_plugin_tool(plugin: AgentPlugin) -> dict:
    """The MCP ``tools/list`` descriptor of the one platform tool.

    Operator ruling (final): NO arguments at all -- no mode, URL, path, digest or
    binding. Read-only; it never installs anything itself."""
    return {
        "name": LOAD_PLUGIN_TOOL,
        "description": (
            f"Returns the official, verified {plugin.display_name} files and this company's current playbook "
            "so the agent can build and install/update the plugin with the host's plugin creator when the "
            "user asks. It never installs anything itself. Follow the returned install_rules (bump only the "
            "local plugin.json version on update, keep the playbook.md provenance header, never overwrite or "
            f"delete an existing skills/{plugin.skill_name}/references/local.md (the user's only customisation "
            "file))."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def load_plugin_arguments_ok(arguments: Any) -> bool:
    """The tool takes no input: only an empty argument object is admitted (fail closed)."""
    return isinstance(arguments, dict) and not arguments


def plugin_hint(plugin: AgentPlugin) -> str:
    """The short "plugin available" line for the app connector's server instructions."""
    return (
        f"Official {plugin.display_name} v{plugin.version} is available. If the user asks to install or "
        "update the CRM plugin, call load_plugin, verify the hashes, then create the plugin with the host's "
        "plugin creator."
    )


def no_plugin_body(*, pack_id: str, contract_digest: str) -> dict:
    return {"ok": True, "status": "no_plugin", "pack_id": pack_id, "contract_digest": contract_digest}


def playbook_file_path(plugin: AgentPlugin) -> str:
    """Where the generated playbook file goes in the agent's archive (reserved: no vendor file may use it)."""
    return f"skills/{plugin.skill_name}/references/{PLAYBOOK_FILE_NAME}"


PROVENANCE_MARKER = "<!-- crm-plugin-provenance v1"
# One safe token per header value: no space, newline, ``<`` or ``>`` (so never ``-->``).
# The SAME grammar and bound CAT enforces at contract build for pack_id and the
# plugin version; every header source is validated against it at IMPORT (or is
# a fixed-width digest / a bounded integer), so header generation is total.
_HEADER_VALUE = re.compile(r"[A-Za-z0-9._:+-]{1,128}")
HEADER_TOKEN_MAX = 128
# Install doctrine by playbook state: current / absent install; anything else STOPS.
_INSTALL_BLOCKED = {"unavailable": "playbook_unavailable", "incompatible": "playbook_incompatible"}


def header_token_ok(value: Any) -> bool:
    """``value`` can be written as a provenance header value."""
    return type(value) is str and _HEADER_VALUE.fullmatch(value) is not None

#: How the host installs / updates the package (r0b2): the vendor files are never
#: changed server-side; the LOCAL version bump is deployment metadata only.
INSTALL_RULES = (
    "Install or update the plugin only when the user explicitly asked; load_plugin itself never installs.",
    (
        "Install or update ONLY when installable is true; otherwise report install_blocked_reason. "
        "playbook.state current: install with the org text. "
        "playbook.state absent: the base-only plugin is valid; install it."
    ),
    (
        "playbook.state unavailable: STOP -- do not install or update; report it and retry later; the "
        "existing local plugin stays untouched."
    ),
    (
        "playbook.state incompatible: STOP -- surface the problems list to the user and do not install or "
        "update until the CRM playbook is fixed; the existing local plugin stays untouched."
    ),
    (
        "Verify every file's length and sha256, the workflow_digest over the vendor manifest and the "
        "org_package_digest over the vendor files plus playbook_file before building the plugin."
    ),
    (
        "Install the vendor files byte for byte and add playbook_file at its path; the vendor files are "
        "always installed verbatim."
    ),
    (
        "When updating an installed plugin, bump the LOCAL plugin.json version monotonically above the "
        "installed one (local deployment metadata, not CRM authority); this version bump and the app "
        "binding below are the only permitted local changes to a vendor file."
    ),
    # The host cannot resolve an app by MCP URL (ChatGPT host test): the binding
    # comes only from the user's explicit, structured selection of the connector.
    (
        "Bind .app.json ONLY to the ChatGPT app id the user selected through the host's app picker / "
        "@-mention of the CRM connector in this conversation (a structured app reference, not typed text), "
        "after confirming it with the host's app lookup; never pick an app by display name, search result, "
        "or guess. Then set plugin.json extensions.com.openai.apps to \"./.app.json\"."
    ),
    (
        "If the user hasn't selected the connector, ask them to mention it (e.g. '@<display_name> installa "
        "il plugin') and stop until they do."
    ),
    (
        "connector.mcp_url identifies this CRM's endpoint for audit and for future host resolvers; if the "
        "host can show an app's MCP URL, it must equal connector.mcp_url."
    ),
    (
        "Keep the provenance header at the top of playbook.md intact; it records the base plugin and the "
        "playbook identity the CRM's playbook stamp is compared against."
    ),
    (
        "User customisations live ONLY in the host-local file skills/<skill_name>/references/local.md, "
        "which is not part of the vendor bundle and is never generated by load_plugin. On install or "
        "update, never overwrite or delete an existing local.md. If none exists, do not create one."
    ),
    (
        "Instruction order: vendor base -> org playbook.md -> user local.md -> the current request; "
        "local.md cannot override the base safety floor, the AIOS guard or the CRM server."
    ),
)


def _header_value(value: Any) -> str:
    """A provenance header value: one safe token, else refuse (never repaired)."""
    text = str(value)
    if _HEADER_VALUE.fullmatch(text) is None:
        raise ValueError("value cannot be written into the provenance header")
    return text


def playbook_file_content(plugin: AgentPlugin, *, pack_id: str, contract_digest: str,
                          playbook: "PlaybookState") -> str:
    """The generated ``references/playbook.md`` -- deterministic, UTF-8, ``\\n`` lines:

    ``<!-- crm-plugin-provenance v1`` / ``plugin_name:`` / ``base_version:`` /
    ``workflow_digest:`` / ``pack_id:`` / ``contract_digest:`` / ``playbook_state:``
    then, for ``current`` and ``incompatible``, ``playbook_version:`` /
    ``playbook_sha256:`` (the ORG-TEXT sha, = the CRM stamp), then ``-->``. For
    ``current`` a blank line and the org text follow verbatim; every other state
    has no org text."""
    lines = [
        PROVENANCE_MARKER,
        f"plugin_name: {_header_value(plugin.plugin_name)}",
        f"base_version: {_header_value(plugin.version)}",
        f"workflow_digest: {_header_value(plugin.manifest_sha256)}",
        f"pack_id: {_header_value(pack_id)}",
        f"contract_digest: {_header_value(contract_digest)}",
        f"playbook_state: {_header_value(playbook.state)}",
    ]
    current = playbook.state == "current" and playbook.playbook is not None
    if current:
        version, sha = playbook.playbook.version, playbook.playbook.sha256
    elif playbook.state == "incompatible":
        version, sha = playbook.version, playbook.sha256
    else:
        version = sha = None
    if version is not None and sha is not None:
        lines += [f"playbook_version: {_header_value(version)}", f"playbook_sha256: {_header_value(sha)}"]
    header = "\n".join(lines + ["-->"]) + "\n"
    return header + "\n" + playbook.playbook.text if current else header


# The canonical app-connector MCP resource AIOS serves: https://<host>/<prefix>/apps/<connector_id>/mcp.
_CONNECTOR_URL = re.compile(
    r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::[0-9]{1,5})?(?:/[A-Za-z0-9_~-]+)*/apps/([A-Za-z0-9_-]{1,128})/mcp"
)


def connector_url_for(url: Any, connector_id: Any) -> bool:
    """``url`` is a well-formed canonical app-connector MCP URL naming exactly ``connector_id``."""
    if type(url) is not str or len(url) > 2048:
        return False
    match = _CONNECTOR_URL.fullmatch(url)
    return match is not None and (connector_id is None or match.group(1) == connector_id)


def load_plugin_body(plugin: AgentPlugin, *, pack_id: str, contract_digest: str, connector_mcp_url: str,
                     playbook: "PlaybookState") -> dict:
    """The complete closed package: identity, the verified vendor files (unchanged),
    the canonical vendor manifest, the playbook state, the ALWAYS-generated
    ``playbook_file`` (provenance header + org text when current; its length and
    sha256 over the exact installed bytes), ``org_package_digest`` (the same
    manifest format over vendor files + playbook.md) and ``install_rules``. The
    playbook file is NOT in the vendor manifest, so ``workflow_digest`` stays the
    vendor's pinned ``manifest_sha256``. ``connector.mcp_url`` is the app the host
    must bind the installed plugin to (the request's own canonical app resource,
    server-derived; the vendor files never declare an app)."""
    if not connector_url_for(connector_mcp_url, None):
        raise ValueError("connector_mcp_url is not a canonical app-connector MCP URL")
    raw = playbook_file_content(plugin, pack_id=pack_id, contract_digest=contract_digest,
                                playbook=playbook).encode("utf-8")
    playbook_file = {
        "path": playbook_file_path(plugin),
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content": raw.decode("utf-8"),
    }
    org_entries = _entries(plugin) + [(playbook_file["path"], playbook_file["length"], playbook_file["sha256"])]
    blocked = _INSTALL_BLOCKED.get(playbook.state, "playbook_unavailable") if playbook.state not in (
        "current", "absent") else None
    return {
        "ok": True,
        "status": "plugin",
        "format_version": plugin.format_version,
        "plugin_name": plugin.plugin_name,
        "display_name": plugin.display_name,
        "version": plugin.version,
        "pack_id": pack_id,
        "contract_digest": contract_digest,
        "workflow_digest": plugin.manifest_sha256,
        "manifest": canonical_manifest(_entries(plugin)).decode("utf-8"),
        "files": [
            {"path": f.path, "length": f.length, "sha256": f.sha256, "content": f.content} for f in plugin.files
        ],
        "playbook": playbook.body(),
        "playbook_file": playbook_file,
        "org_package_digest": manifest_digest(org_entries),
        "connector": {"mcp_url": connector_mcp_url},
        "install_rules": [
            rule.replace("<display_name>", plugin.display_name).replace("<skill_name>", plugin.skill_name)
            for rule in INSTALL_RULES
        ],
        # r0b2: explicit, so the host never infers it -- a transient CRM outage or an
        # incompatible playbook never replaces a working local plugin.
        "installable": blocked is None,
        "install_blocked_reason": blocked,
    }


# -- the playbook -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Playbook:
    version: int
    sha256: str
    updated_at: str
    updated_by_role: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class PlaybookState:
    """``current`` | ``absent`` | ``unavailable`` | ``incompatible``."""

    state: str
    playbook: Optional[Playbook] = None
    version: Optional[int] = None
    sha256: Optional[str] = None
    problems: tuple = ()

    def body(self) -> dict:
        if self.state == "current" and self.playbook is not None:
            p = self.playbook
            return {"state": "current", "version": p.version, "sha256": p.sha256, "updated_at": p.updated_at,
                    "updated_by_role": p.updated_by_role, "text": p.text}
        if self.state == "incompatible":
            return {"state": "incompatible", "version": self.version, "sha256": self.sha256,
                    "problems": [dict(p) for p in self.problems]}
        return {"state": self.state}


UNAVAILABLE = PlaybookState("unavailable")
ABSENT = PlaybookState("absent")


def playbook_request_body() -> bytes:
    """The request body is exactly ``{}`` (CAT contract): the control assertion
    already names tenant / binding / pack / contract_digest / sub and pins the
    body by ``body_sha256``; nothing here is request input."""
    return canonicalize({})


def _rfc3339_utc(value: Any) -> bool:
    if type(value) is not str or _RFC3339_UTC.fullmatch(value) is None:
        return False
    try:
        _dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def _identity(parsed: dict, pack_id: str, contract_digest: str) -> bool:
    return parsed.get("pack_id") == pack_id and parsed.get("contract_digest") == contract_digest


def _crm_error(parsed: dict, code: str) -> bool:
    err = parsed.get("error")
    return (
        parsed.get("ok") is False
        and isinstance(err, dict)
        and set(err) == {"code", "message"}
        and err["code"] == code
        and type(err["message"]) is str
    )


PLAYBOOK_VERSION_MAX = 2**53 - 1  # r0b2: a finite range (JSON-safe integer); over it is unavailable


def _version_ok(value: Any) -> bool:
    return _is_int(value) and 1 <= value <= PLAYBOOK_VERSION_MAX


def _current(parsed: dict) -> Optional[Playbook]:
    text = parsed["text"]
    if not _version_ok(parsed["version"]) or not _text_ok(text):
        return None
    raw = text.encode("utf-8")
    if len(raw) > PLAYBOOK_TEXT_MAX_BYTES:
        return None
    sha = parsed["sha256"]
    if type(sha) is not str or _HEX64.fullmatch(sha) is None or sha != hashlib.sha256(raw).hexdigest():
        return None
    if not _rfc3339_utc(parsed["updated_at"]) or parsed["updated_by_role"] not in PLAYBOOK_ROLES:
        return None
    return Playbook(parsed["version"], sha, parsed["updated_at"], parsed["updated_by_role"], text)


def _problems(value: Any) -> Optional[tuple]:
    if not isinstance(value, list) or len(value) > PLAYBOOK_PROBLEMS_MAX:
        return None
    out = []
    for p in value:
        if not isinstance(p, dict) or set(p) != {"tool", "mode", "reason"}:
            return None
        tool, mode, reason = p["tool"], p["mode"], p["reason"]
        if type(tool) is not str or not tool or len(tool) > _PROBLEM_FIELD_MAX or not _text_ok(tool):
            return None
        if mode is not None and (type(mode) is not str or len(mode) > _PROBLEM_FIELD_MAX or not _text_ok(mode)):
            return None
        if type(reason) is not str or len(reason) > _PROBLEM_REASON_MAX or not _text_ok(reason):
            return None
        out.append({"tool": tool, "mode": mode, "reason": reason})
    return tuple(out)


def interpret_playbook_response(status: Any, parsed: Any, *, pack_id: str, contract_digest: str) -> PlaybookState:
    """CAT's closed response contract -> a :class:`PlaybookState`. Anything unexpected is unavailable."""
    if not isinstance(parsed, dict) or not _identity(parsed, pack_id, contract_digest):
        return UNAVAILABLE
    keys = set(parsed)
    if status == 200 and keys == _PLAYBOOK_CURRENT_KEYS:
        playbook = _current(parsed)
        return PlaybookState("current", playbook) if playbook is not None else UNAVAILABLE
    if status == 404 and keys == _PLAYBOOK_NONE_KEYS and _crm_error(parsed, PLAYBOOK_NONE):
        return ABSENT
    if status == 409 and keys == _PLAYBOOK_INCOMPATIBLE_KEYS and _crm_error(parsed, PLAYBOOK_INCOMPATIBLE):
        problems = _problems(parsed["problems"])
        sha = parsed["sha256"]
        if problems is None or not _version_ok(parsed["version"]) or type(sha) is not str \
                or _HEX64.fullmatch(sha) is None:
            return UNAVAILABLE
        return PlaybookState("incompatible", version=parsed["version"], sha256=sha, problems=problems)
    return UNAVAILABLE


class PlaybookCache:
    """Verified playbook CONTENT keyed ``(tenant, binding_id, contract_digest, sha256)``.

    Only content: the CURRENT pointer is fetched on every call, and a failed
    fetch never falls back to a cached entry. Bounded (LRU)."""

    def __init__(self, max_entries: int = 256) -> None:
        self._max = max(1, int(max_entries))
        self._rows: "OrderedDict[tuple, Playbook]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Optional[Playbook]:
        with self._lock:
            row = self._rows.get(key)
            if row is not None:
                self._rows.move_to_end(key)
            return row

    def put(self, key: tuple, playbook: Playbook) -> Playbook:
        with self._lock:
            row = self._rows.get(key)
            if row is None or row != playbook:
                self._rows[key] = playbook
            self._rows.move_to_end(key)
            while len(self._rows) > self._max:
                self._rows.popitem(last=False)
            return self._rows[key]
