from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from fnmatch import fnmatch
from hashlib import md5
from pathlib import Path, PurePosixPath

from .config import INDEX_ENABLED_LANGUAGES
from .language_descriptor_tags import semantic_tag_defs
from .outline_extractors import outline_family_names
from .pattern_templates import expand_match_to_pattern

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


@dataclass(slots=True)
class LanguageDescriptor:
    name: str
    extensions: tuple[str, ...]
    suffixes: tuple[str, ...]
    include_globs: tuple[str, ...]
    extractor_family: str | None
    outline_family: str | None
    outline_patterns: tuple[tuple[str, str], ...]
    line_patterns: tuple[tuple[str, str], ...]
    role_hint: str | None
    role_patterns: tuple[tuple[str, str], ...]
    module_hints: tuple[str, ...]
    semantic_tags: tuple[str, ...]
    embedded_semantics: tuple[str, ...]
    tier: str
    source: str
    # Extended fields (loaded from TOML, used by code_index_store)
    entry_points: tuple[str, ...]
    role_name_suffixes: tuple[tuple[str, str], ...]  # (suffix, role)
    role_name_prefixes: tuple[tuple[str, str], ...]  # (prefix, role)
    role_exact_names: tuple[tuple[str, str], ...]  # (name, role)
    role_suffix_patterns: tuple[tuple[str, str], ...]  # (file_suffix, role)
    role_path_tokens: tuple[
        tuple[str, str | None, str],
        ...,
    ]  # (token, optional_contains_suffix, role)
    layer_tokens: dict[str, tuple[str, ...]]  # {layer: (token, ...)}
    component_semantics: dict[str, tuple[str, ...]] = dataclasses.field(
        default_factory=dict,
    )  # {category: (pattern, ...)}


# Auto-derived from LanguageDescriptor field names — any field change invalidates cache
_DESCRIPTOR_SCHEMA_VERSION: str = md5(
    ",".join(f.name for f in dataclasses.fields(LanguageDescriptor)).encode(),
).hexdigest()[:8]

_CACHE: dict[str, tuple[str, tuple[tuple[str, int], ...], dict[str, object]]] = {}


def _enabled_languages() -> set[str] | None:
    value = INDEX_ENABLED_LANGUAGES.strip().lower()
    if value == "all":
        return None
    return {item.strip() for item in INDEX_ENABLED_LANGUAGES.split(",") if item.strip()}


def _descriptor_dirs(project_root: Path) -> list[Path]:
    return [Path(__file__).resolve().parent / "index_languages", project_root / "index_languages"]


def builtin_descriptor_root() -> Path:
    return Path(__file__).resolve().parent


def _fingerprint(project_root: Path) -> tuple[tuple[str, int], ...]:
    items: list[tuple[str, int]] = []
    for directory in _descriptor_dirs(project_root):
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.toml")):
            if file.stem.startswith("_"):
                continue
            try:
                stat = file.stat()
            except OSError:
                continue
            items.append((str(file.resolve()), int(stat.st_mtime_ns)))
    return tuple(items)


def _normalize_list(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return ()


def _load_descriptor_file(file: Path, source: str) -> LanguageDescriptor | None:
    if tomllib is None:
        return None
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    derived_name = file.name[:-5] if file.name.lower().endswith(".toml") else file.stem
    name = str(data.get("name") or derived_name).strip()
    if not name:
        return None
    extensions = tuple(
        ext if ext.startswith(".") else f".{ext}" for ext in _normalize_list(data.get("extensions"))
    )
    suffixes = tuple(
        sfx if sfx.startswith(".") else f".{sfx}" for sfx in _normalize_list(data.get("suffixes"))
    )
    include_globs = _normalize_list(data.get("include_globs"))
    extractor_family = str(data.get("extractor_family") or "").strip() or None
    outline_family = str(data.get("outline_family") or "").strip() or None
    outline_patterns_raw = data.get("outline_patterns", [])
    outline_patterns: list[tuple[str, str]] = []
    if isinstance(outline_patterns_raw, list):
        for item in outline_patterns_raw:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if pattern and kind:
                outline_patterns.append((pattern, kind))
    line_patterns_raw = data.get("line_patterns", [])
    line_patterns: list[tuple[str, str]] = []
    if isinstance(line_patterns_raw, list):
        for item in line_patterns_raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if not kind:
                continue
            # Try explicit pattern first (escape hatch for power users)
            pattern = str(item.get("pattern") or "").strip()
            # If no pattern, try named match template
            if not pattern and item.get("match"):
                pattern = expand_match_to_pattern(item) or ""
            if pattern and kind:
                line_patterns.append((pattern, kind))
    role_hint = str(data.get("role_hint") or "").strip() or None
    role_patterns_raw = data.get("role_patterns", [])
    role_patterns: list[tuple[str, str]] = []
    if isinstance(role_patterns_raw, list):
        for item in role_patterns_raw:
            if not isinstance(item, dict):
                continue
            glob = str(item.get("glob") or "").strip()
            role = str(item.get("role") or "").strip()
            if glob and role:
                role_patterns.append((glob, role))
    module_hints = _normalize_list(data.get("module_hints"))
    semantic_tags = _normalize_list(data.get("semantic_tags"))
    embedded_semantics = _normalize_list(data.get("embedded_semantics"))
    tag_defs = semantic_tag_defs()
    merged_module_hints = list(module_hints)
    merged_role_patterns = list(role_patterns)
    for tag in semantic_tags:
        tag_def = tag_defs.get(tag)
        if not tag_def:
            continue
        for hint in tag_def.get("module_hints", []):
            if hint not in merged_module_hints:
                merged_module_hints.append(hint)
        for glob, role in tag_def.get("role_patterns", []):
            if (glob, role) not in merged_role_patterns:
                merged_role_patterns.append((glob, role))
    tier = str(data.get("tier") or ("rich" if source == "built_in" else "heuristic")).strip()

    # ── Extended fields ──
    entry_points = _normalize_list(data.get("entry_points"))

    def _parse_pair_list(key: str, field_a: str, field_b: str) -> list[tuple[str, str]]:
        raw = data.get(key, [])
        result: list[tuple[str, str]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                a = str(item.get(field_a) or "").strip()
                b = str(item.get(field_b) or "").strip()
                if a and b:
                    result.append((a, b))
        return result

    role_name_suffixes = _parse_pair_list("role_name_suffixes", "suffix", "role")
    role_name_prefixes = _parse_pair_list("role_name_prefixes", "prefix", "role")
    role_exact_names = _parse_pair_list("role_exact_names", "name", "role")
    role_suffix_patterns = _parse_pair_list("role_suffix_patterns", "suffix", "role")

    role_path_tokens_raw = data.get("role_path_tokens", [])
    role_path_tokens: list[tuple[str, str | None, str]] = []
    if isinstance(role_path_tokens_raw, list):
        for item in role_path_tokens_raw:
            if not isinstance(item, dict):
                continue
            token = str(item.get("token") or "").strip()
            role = str(item.get("role") or "").strip()
            contains_suffix = str(item.get("contains_suffix") or "").strip() or None
            if token and role:
                role_path_tokens.append((token, contains_suffix, role))

    component_semantics_raw = data.get("component_semantics", {})
    component_semantics: dict[str, tuple[str, ...]] = {}
    if isinstance(component_semantics_raw, dict):
        for category, patterns in component_semantics_raw.items():
            component_semantics[str(category)] = _normalize_list(patterns)

    layer_tokens_raw = data.get("layer_tokens", {})
    layer_tokens: dict[str, tuple[str, ...]] = {}
    if isinstance(layer_tokens_raw, dict):
        for layer, tokens in layer_tokens_raw.items():
            layer_tokens[str(layer)] = _normalize_list(tokens)

    return LanguageDescriptor(
        name=name,
        extensions=extensions,
        suffixes=suffixes,
        include_globs=include_globs,
        extractor_family=extractor_family,
        outline_family=outline_family,
        outline_patterns=tuple(outline_patterns),
        line_patterns=tuple(line_patterns),
        role_hint=role_hint,
        role_patterns=tuple(merged_role_patterns),
        module_hints=tuple(merged_module_hints),
        semantic_tags=semantic_tags,
        embedded_semantics=embedded_semantics,
        tier=tier,
        source=source,
        entry_points=entry_points,
        role_name_suffixes=tuple(role_name_suffixes),
        role_name_prefixes=tuple(role_name_prefixes),
        role_exact_names=tuple(role_exact_names),
        role_suffix_patterns=tuple(role_suffix_patterns),
        role_path_tokens=tuple(role_path_tokens),
        layer_tokens=layer_tokens,
        component_semantics=component_semantics,
    )


def load_language_descriptors(
    project_root: Path,
    *,
    force_reload: bool = False,
) -> dict[str, object]:
    root_key = str(project_root.resolve())
    cached = _CACHE.get(root_key)
    # Fast path: if we have a cached result with matching schema version, skip fingerprint I/O
    if cached and cached[0] == _DESCRIPTOR_SCHEMA_VERSION and not force_reload:
        return cached[2]
    # Slow path: check file fingerprints (only on first load or forced reload)
    fingerprint = _fingerprint(project_root)
    if cached and cached[0] == _DESCRIPTOR_SCHEMA_VERSION and cached[1] == fingerprint:
        return cached[2]

    enabled = _enabled_languages()
    descriptors: dict[str, LanguageDescriptor] = {}
    for directory in _descriptor_dirs(project_root):
        if not directory.is_dir():
            continue
        source = (
            "built_in" if directory == builtin_descriptor_root() / "index_languages" else "project"
        )
        for file in sorted(directory.glob("*.toml")):
            if file.stem.startswith("_"):
                continue
            descriptor = _load_descriptor_file(file, source=source)
            if descriptor is None:
                continue
            if enabled is not None and descriptor.name not in enabled:
                continue
            descriptors[descriptor.name] = descriptor

    extension_map: dict[str, str] = {}
    suffix_map: dict[str, str] = {}
    include_globs: list[tuple[str, str]] = []
    for descriptor in descriptors.values():
        for ext in descriptor.extensions:
            extension_map[ext.lower()] = descriptor.name
        for suffix in descriptor.suffixes:
            suffix_map[suffix.lower()] = descriptor.name
        for pattern in descriptor.include_globs:
            include_globs.append((pattern, descriptor.name))

    payload = {
        "descriptors": descriptors,
        "extensions": extension_map,
        "suffixes": suffix_map,
        "include_globs": include_globs,
    }
    _CACHE[root_key] = (_DESCRIPTOR_SCHEMA_VERSION, fingerprint, payload)
    return payload


def validate_language_descriptors(project_root: Path) -> dict[str, object]:
    issues: list[dict[str, object]] = []
    descriptors: list[dict[str, object]] = []
    seen_extensions: dict[str, str] = {}
    seen_suffixes: dict[str, str] = {}
    known_families = set(outline_family_names())
    known_tags = set(semantic_tag_defs().keys())

    for directory in _descriptor_dirs(project_root):
        if not directory.is_dir():
            continue
        source = (
            "built_in" if directory == builtin_descriptor_root() / "index_languages" else "project"
        )
        for file in sorted(directory.glob("*.toml")):
            if file.stem.startswith("_"):
                continue  # Skip config files like _index_config.toml
            entry = {"path": str(file), "source": source, "valid": True, "name": None, "issues": []}
            if tomllib is None:
                entry["valid"] = False
                entry["issues"].append("tomllib not available")
                issues.append({"path": str(file), "issue": "tomllib not available"})
                descriptors.append(entry)
                continue
            try:
                data = tomllib.loads(file.read_text(encoding="utf-8", errors="ignore"))
            except Exception as exc:
                entry["valid"] = False
                entry["issues"].append(f"parse_error: {exc}")
                issues.append({"path": str(file), "issue": f"parse_error: {exc}"})
                descriptors.append(entry)
                continue

            name = str(data.get("name") or file.stem).strip()
            entry["name"] = name
            if not name:
                entry["valid"] = False
                entry["issues"].append("missing name")
                issues.append({"path": str(file), "issue": "missing name"})

            extensions = _normalize_list(data.get("extensions"))
            suffixes = _normalize_list(data.get("suffixes"))
            include_globs = _normalize_list(data.get("include_globs"))
            outline_family = str(data.get("outline_family") or "").strip() or None
            outline_patterns_raw = data.get("outline_patterns", [])
            line_patterns_raw = data.get("line_patterns", [])
            semantic_tags = _normalize_list(data.get("semantic_tags"))

            if not extensions and not suffixes and not include_globs:
                entry["valid"] = False
                entry["issues"].append(
                    "descriptor must declare extensions, suffixes, or include_globs",
                )
                issues.append({"path": str(file), "issue": "missing discovery keys"})

            for ext in extensions:
                normalized = ext if ext.startswith(".") else f".{ext}"
                previous = seen_extensions.get(normalized.lower())
                if previous and previous != name:
                    entry["issues"].append(
                        f"extension collision: {normalized} already owned by {previous}",
                    )
                seen_extensions[normalized.lower()] = name

            for suffix in suffixes:
                normalized = suffix if suffix.startswith(".") else f".{suffix}"
                previous = seen_suffixes.get(normalized.lower())
                if previous and previous != name:
                    entry["issues"].append(
                        f"suffix collision: {normalized} already owned by {previous}",
                    )
                seen_suffixes[normalized.lower()] = name

            if (
                outline_family
                and str(outline_family).strip()
                and not isinstance(outline_family, str)
            ):
                entry["issues"].append("outline_family must be a string")
            if outline_family and outline_family not in known_families:
                entry["issues"].append(f"unknown outline_family: {outline_family}")
            extractor_family = str(data.get("extractor_family") or "").strip() or None
            if extractor_family and extractor_family not in {
                "python_ast",
                "javascript_ast",
                "typescript_ast",
                "jsx_ast",
                "tsx_ast",
                "csharp_rich",
                "razor_rich",
                "resx_rich",
                "css_rich",
            }:
                entry["issues"].append(f"unknown extractor_family: {extractor_family}")

            for tag in semantic_tags:
                if tag not in known_tags:
                    entry["issues"].append(f"unknown semantic_tag: {tag}")

            if outline_patterns_raw and not isinstance(outline_patterns_raw, list):
                entry["issues"].append("outline_patterns must be a list")
            if line_patterns_raw and not isinstance(line_patterns_raw, list):
                entry["issues"].append("line_patterns must be a list")

            descriptors.append(entry)
            for issue in entry["issues"]:
                issues.append({"path": str(file), "issue": issue})

    return {
        "count": len(descriptors),
        "valid": not any(not bool(item["valid"]) for item in descriptors),
        "descriptors": descriptors,
        "issues": issues,
    }


def descriptor_semantics_summary() -> dict[str, object]:
    built_in = load_language_descriptors(builtin_descriptor_root())["descriptors"]
    built_in_items = [
        item
        for item in built_in.values()
        if isinstance(item, LanguageDescriptor) and item.source == "built_in"
    ]
    embedded = sorted({value for item in built_in_items for value in item.embedded_semantics})
    return {
        "extractor_families": [
            "python_ast",
            "javascript_ast",
            "typescript_ast",
            "jsx_ast",
            "tsx_ast",
            "csharp_rich",
            "razor_rich",
            "resx_rich",
            "css_rich",
        ],
        "outline_families": outline_family_names(),
        "semantic_tags": sorted(semantic_tag_defs().keys()),
        "embedded_semantics": embedded,
        "built_in_descriptor_count": len(built_in_items),
        "built_in_with_extractor_family": sum(
            1 for item in built_in_items if item.extractor_family
        ),
        "built_in_with_outline_family": sum(1 for item in built_in_items if item.outline_family),
        "built_in_with_outline_patterns": sum(
            1 for item in built_in_items if item.outline_patterns
        ),
        "built_in_with_role_semantics": sum(
            1 for item in built_in_items if item.role_hint or item.role_patterns
        ),
        "built_in_with_module_hints": sum(1 for item in built_in_items if item.module_hints),
    }


def descriptor_match_summary(project_root: Path, relative_path: str) -> dict[str, object]:
    rel = relative_path.replace("\\", "/")
    suffix = Path(rel).suffix.lower()
    code_language = language_for_custom_descriptor(project_root, rel, suffix)
    descriptor = descriptor_for_language(project_root, rel, suffix)
    predicted_role = role_from_descriptor(project_root, rel, suffix)
    return {
        "path": rel,
        "suffix": suffix,
        "language": code_language,
        "matched": code_language is not None,
        "predicted_role": predicted_role,
        "descriptor": {
            "name": descriptor.name,
            "tier": descriptor.tier,
            "source": descriptor.source,
            "extensions": list(descriptor.extensions),
            "suffixes": list(descriptor.suffixes),
            "include_globs": list(descriptor.include_globs),
            "extractor_family": descriptor.extractor_family,
            "outline_family": descriptor.outline_family,
            "line_patterns": [
                {"pattern": pattern, "kind": kind} for pattern, kind in descriptor.line_patterns
            ],
            "role_hint": descriptor.role_hint,
            "semantic_tags": list(descriptor.semantic_tags),
            "module_hints": list(descriptor.module_hints),
            "embedded_semantics": list(descriptor.embedded_semantics),
            "role_patterns": [
                {"glob": glob, "role": role} for glob, role in descriptor.role_patterns
            ],
            "uses_semantic_tags": bool(descriptor.semantic_tags),
            "uses_raw_outline_patterns": bool(descriptor.outline_patterns),
            "uses_raw_role_patterns": bool(descriptor.role_patterns)
            and not bool(descriptor.semantic_tags),
        }
        if descriptor is not None
        else None,
    }


def language_from_snapshot(
    snapshot: dict[str, object],
    relative_path: str,
    suffix: str,
) -> str | None:
    """Pure language match against an already-loaded descriptor snapshot
    (the dict returned by load_language_descriptors). No I/O, no resolve — so a
    caller iterating many files can load the snapshot ONCE and match per file.

    Matching order is identical to language_for_custom_descriptor: suffix
    endswith, then exact extension, then include-glob fnmatch.
    """
    extensions = snapshot["extensions"] if isinstance(snapshot.get("extensions"), dict) else {}
    suffixes = snapshot["suffixes"] if isinstance(snapshot.get("suffixes"), dict) else {}
    include_globs = snapshot["include_globs"] if isinstance(snapshot.get("include_globs"), list) else []

    rel = relative_path.replace("\\", "/")
    lower_rel = rel.lower()

    for known_suffix, code_language in suffixes.items():
        if lower_rel.endswith(known_suffix):
            return str(code_language)

    if suffix.lower() in extensions:
        return str(extensions[suffix.lower()])

    for pattern, code_language in include_globs:
        if fnmatch(rel, pattern):
            return str(code_language)
    return None


def language_for_custom_descriptor(
    project_root: Path,
    relative_path: str,
    suffix: str,
) -> str | None:
    # Loads the snapshot (cached) then delegates to the pure matcher — single
    # source of matching logic shared with the hoisted freshness loop.
    return language_from_snapshot(
        load_language_descriptors(project_root),
        relative_path,
        suffix,
    )


def descriptor_for_language(
    project_root: Path,
    relative_path: str,
    suffix: str,
) -> LanguageDescriptor | None:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    code_language = language_for_custom_descriptor(project_root, relative_path, suffix)
    if not code_language:
        return None
    descriptor = descriptors.get(code_language)
    return descriptor if isinstance(descriptor, LanguageDescriptor) else None


def descriptor_registry_summary(project_root: Path) -> dict[str, object]:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    items = []
    for name in sorted(descriptors):
        descriptor = descriptors[name]
        if not isinstance(descriptor, LanguageDescriptor):
            continue
        items.append(
            {
                "name": descriptor.name,
                "extensions": list(descriptor.extensions),
                "suffixes": list(descriptor.suffixes),
                "include_globs": list(descriptor.include_globs),
                "extractor_family": descriptor.extractor_family,
                "outline_family": descriptor.outline_family,
                "outline_patterns": [
                    {"pattern": pattern, "kind": kind}
                    for pattern, kind in descriptor.outline_patterns
                ],
                "line_patterns": [
                    {"pattern": pattern, "kind": kind} for pattern, kind in descriptor.line_patterns
                ],
                "role_hint": descriptor.role_hint,
                "role_patterns": [
                    {"glob": glob, "role": role} for glob, role in descriptor.role_patterns
                ],
                "module_hints": list(descriptor.module_hints),
                "semantic_tags": list(descriptor.semantic_tags),
                "embedded_semantics": list(descriptor.embedded_semantics),
                "uses_semantic_tags": bool(descriptor.semantic_tags),
                "uses_raw_outline_patterns": bool(descriptor.outline_patterns),
                "uses_raw_role_patterns": bool(descriptor.role_patterns)
                and not bool(descriptor.semantic_tags),
                "tier": descriptor.tier,
                "source": descriptor.source,
            },
        )
    return {
        "count": len(items),
        "descriptors": items,
    }


def language_for_builtin_descriptor(relative_path: str, suffix: str) -> str | None:
    return language_for_custom_descriptor(builtin_descriptor_root(), relative_path, suffix)


def outline_patterns_for_language(project_root: Path, language: str) -> list[tuple[str, str]]:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    descriptor = descriptors.get(language)
    if not isinstance(descriptor, LanguageDescriptor):
        return []
    if descriptor.outline_patterns:
        return list(descriptor.outline_patterns)
    return []


def line_patterns_for_language(project_root: Path, language: str) -> list[tuple[str, str]]:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    descriptor = descriptors.get(language)
    if not isinstance(descriptor, LanguageDescriptor):
        return []
    return list(descriptor.line_patterns)


def outline_family_for_language(project_root: Path, language: str) -> str | None:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    descriptor = descriptors.get(language)
    if not isinstance(descriptor, LanguageDescriptor):
        return None
    return descriptor.outline_family


def extractor_family_for_language(project_root: Path, language: str) -> str | None:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    descriptor = descriptors.get(language)
    if not isinstance(descriptor, LanguageDescriptor):
        return None
    return descriptor.extractor_family


def role_from_descriptor(project_root: Path, relative_path: str, suffix: str) -> str | None:
    descriptor = descriptor_for_language(project_root, relative_path, suffix)
    if descriptor is None:
        return None
    rel = relative_path.replace("\\", "/")
    posix = PurePosixPath(rel)

    def _matches(glob: str) -> bool:
        lowered_glob = glob.lower()
        candidates = {lowered_glob}
        if "/**/" in lowered_glob:
            candidates.add(lowered_glob.replace("/**/", "/"))
        if lowered_glob.startswith("**/"):
            candidates.add(lowered_glob[3:])
        for candidate in candidates:
            if fnmatch(rel.lower(), candidate) or posix.match(candidate):
                return True
        return False

    for glob, role in descriptor.role_patterns:
        if _matches(glob):
            return role
    return descriptor.role_hint


def module_hints_from_descriptors(project_root: Path) -> set[str]:
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    result: set[str] = set()
    for descriptor in descriptors.values():
        if isinstance(descriptor, LanguageDescriptor):
            result.update(item.lower() for item in descriptor.module_hints)
    return result


# ── Global index config (_index_config.toml) ──

_CONFIG_CACHE: dict[str, dict[str, object]] = {}


def load_index_config() -> dict[str, object]:
    """Load the global _index_config.toml from the built-in index_languages directory."""
    cached = _CONFIG_CACHE.get("__global__")
    if cached:
        return cached

    config_path = Path(__file__).resolve().parent / "index_languages" / "_index_config.toml"
    if tomllib is None or not config_path.is_file():
        return {}
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}

    result: dict[str, object] = {
        "path_weight_positive": _normalize_list(data.get("path_weights", {}).get("positive")),
        "path_weight_negative": _normalize_list(data.get("path_weights", {}).get("negative")),
        "path_weight_positive_score": int(data.get("path_weights", {}).get("positive_score", 20)),
        "path_weight_negative_score": int(data.get("path_weights", {}).get("negative_score", -35)),
        "skip_dirs": set(_normalize_list(data.get("module_detection", {}).get("skip_dirs"))),
        "hint_dirs": set(_normalize_list(data.get("module_detection", {}).get("hint_dirs"))),
        "module_manifests": dict(data.get("module_manifests", {})),
        "default_layer_tokens": {
            layer: _normalize_list(tokens)
            for layer, tokens in data.get("default_layer_tokens", {}).items()
        },
        "role_relevance": dict(data.get("role_relevance", {})),
    }
    _CONFIG_CACHE["__global__"] = result
    return result


def layer_from_descriptor(project_root: Path, relative_path: str, suffix: str) -> str | None:
    """Infer layer (data/api/logic/ui) from the language descriptor's layer_tokens."""
    descriptor = descriptor_for_language(project_root, relative_path, suffix)
    if descriptor is None:
        return None
    lower = relative_path.lower()
    for layer, tokens in descriptor.layer_tokens.items():
        for token in tokens:
            if token in lower:
                return layer
    return None


def role_from_descriptor_extended(
    project_root: Path,
    relative_path: str,
    suffix: str,
    path_parts: list[str] | None = None,
) -> str | None:
    """Extended role inference using all descriptor fields (globs, name suffixes, prefixes, exact names, path tokens).

    Returns the first match, or descriptor.role_hint as fallback.
    The glob-based role_patterns are checked first (via role_from_descriptor).
    """
    descriptor = descriptor_for_language(project_root, relative_path, suffix)
    if descriptor is None:
        return None

    # 1. Glob-based role_patterns (already handled by role_from_descriptor)
    glob_role = role_from_descriptor(project_root, relative_path, suffix)
    if glob_role:
        return glob_role

    stem = Path(relative_path).stem.lower()
    # For dotted stems like "Program.cs" → "program", "Edit.cshtml.cs" already handled by suffix
    logical_stem = stem.split(".", 1)[0] if "." in stem else stem
    lower_path = relative_path.lower()

    # 2. File suffix patterns (path ends with)
    for file_suffix, role in descriptor.role_suffix_patterns:
        if lower_path.endswith(file_suffix.lower()):
            return role

    # 3. Exact name match
    for name, role in descriptor.role_exact_names:
        if logical_stem == name.lower():
            return role

    # 4. Name suffix match
    for name_suffix, role in descriptor.role_name_suffixes:
        if logical_stem.endswith(name_suffix.lower()):
            return role

    # 5. Name prefix match
    for prefix, role in descriptor.role_name_prefixes:
        if stem.startswith(prefix.lower()):
            return role

    # 6. Path token match
    if path_parts is None:
        path_parts = lower_path.split("/")
    for token, contains_suffix, role in descriptor.role_path_tokens:
        if token.lower() in path_parts:
            if contains_suffix:
                if logical_stem.endswith(contains_suffix.lower()):
                    return role
            else:
                return role

    # 7. Fallback to role_hint
    return descriptor.role_hint


def entry_points_from_descriptors(project_root: Path) -> dict[str, str]:
    """Return {filename: language} for all entry points across all descriptors."""
    data = load_language_descriptors(project_root)
    descriptors = data["descriptors"] if isinstance(data.get("descriptors"), dict) else {}
    result: dict[str, str] = {}
    for descriptor in descriptors.values():
        if isinstance(descriptor, LanguageDescriptor):
            for ep in descriptor.entry_points:
                result[ep] = descriptor.name
    return result
