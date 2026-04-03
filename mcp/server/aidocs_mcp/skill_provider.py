from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from aidocs_mcp.types import ExternalSkillProvider, SkillRecord


BUNDLED_PROVIDER_ID = "aidocs_bundled_superpowers"
BUNDLED_PROVIDER_ORIGIN = "bundled_provider"
EXTERNAL_PROVIDER_ORIGIN = "external_provider"
VALID_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
UNSUPPORTED_FRONTMATTER_PREFIXES = ("[", "{", "|", ">")


def validate_provider_id(provider_name: str) -> str:
    provider_id = provider_name.strip()
    if not provider_id or not VALID_PROVIDER_ID_RE.fullmatch(provider_id):
        raise ValueError("External skill providers require a valid provider id.")
    return provider_id


def _unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text.strip()
    end = text.find("\n---\n", 4)
    if end == -1:
        return text.strip()
    return text[end + 5 :].strip()


def parse_external_frontmatter(
    text: str,
    parse_frontmatter: Callable[[str], dict[str, str]],
    file: Path,
) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}

    for raw_line in text[4:end].splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace() or stripped.startswith("- "):
            raise ValueError(
                f"Unsupported external skill frontmatter in {file}: nested YAML values are not supported."
            )
        if ":" not in stripped:
            raise ValueError(
                f"Unsupported external skill frontmatter in {file}: expected single-line key:value metadata."
            )
        _, value = stripped.split(":", 1)
        plain_value = value.strip()
        if plain_value in {"", "[]", "{}"}:
            continue
        if plain_value[:1] in {"'", '"'}:
            if len(plain_value) < 2 or plain_value[-1] != plain_value[0]:
                raise ValueError(
                    f"Unsupported external skill frontmatter in {file}: unterminated quoted scalar value."
                )
            continue
        if plain_value.startswith(UNSUPPORTED_FRONTMATTER_PREFIXES):
            raise ValueError(
                f"Unsupported external skill frontmatter in {file}: only plain single-line scalar values are supported."
            )

    return {
        key: _unquote_scalar(value) for key, value in parse_frontmatter(text).items()
    }


def resolve_external_provider(
    provider_name: str, raw_path: str, project_root: Path
) -> ExternalSkillProvider:
    provider_id = validate_provider_id(provider_name)
    candidate = (
        (project_root / raw_path).resolve()
        if not Path(raw_path).is_absolute()
        else Path(raw_path).resolve()
    )
    if not raw_path or not raw_path.strip():
        raise ValueError("External skill providers require a local path.")
    if "://" in raw_path:
        raise ValueError("External skill providers require a local path.")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("External skill providers require a local path.")

    version = None
    manifest = candidate / "provider.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        manifest_version = payload.get("version")
        if isinstance(manifest_version, str) and manifest_version.strip():
            version = manifest_version.strip()

    return ExternalSkillProvider(
        provider_id=provider_id,
        root_path=candidate,
        version=version,
        compatibility_state="unknown",
    )


def resolve_bundled_provider(root_path: Path) -> ExternalSkillProvider:
    return ExternalSkillProvider(
        provider_id=BUNDLED_PROVIDER_ID,
        root_path=root_path.resolve(),
        version=None,
        compatibility_state="compatible",
    )


def load_bundled_provider_skills(
    provider: ExternalSkillProvider,
    parse_frontmatter: Callable[[str], dict[str, str]],
) -> list[SkillRecord]:
    if not provider.root_path.is_dir():
        return []

    records: list[SkillRecord] = []
    for file in sorted(provider.root_path.glob("*.md")):
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        meta = parse_frontmatter(text)
        name = (meta.get("name") or "").strip()
        if not name:
            continue
        records.append(
            SkillRecord(
                provider=provider.provider_id,
                skill_id=f"{provider.provider_id}/{name}",
                name=name,
                description=meta.get("description") or "",
                path=str(file),
                origin=BUNDLED_PROVIDER_ORIGIN,
                source=BUNDLED_PROVIDER_ORIGIN,
                tags=[
                    item.strip()
                    for item in (meta.get("tags") or "").split(",")
                    if item.strip()
                ],
                content=strip_frontmatter(text),
                skill_kind=(meta.get("kind") or "helper").strip() or "helper",
            )
        )
    return records


def load_external_provider_skills(
    provider: ExternalSkillProvider,
    parse_frontmatter: Callable[[str], dict[str, str]],
) -> tuple[list[SkillRecord], list[dict[str, object]]]:
    skills_dir = provider.root_path / "skills"
    if not skills_dir.is_dir():
        return [], []

    records: list[SkillRecord] = []
    warnings: list[dict[str, object]] = []
    for file in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            meta = parse_external_frontmatter(text, parse_frontmatter, file)
        except ValueError as exc:
            warnings.append(
                {
                    "provider": provider.provider_id,
                    "skill_id": f"{provider.provider_id}/__warning__/{file.parent.name}",
                    "name": f"{provider.provider_id} warning",
                    "description": str(exc),
                    "path": str(file),
                    "origin": "external_provider_warning",
                    "source": EXTERNAL_PROVIDER_ORIGIN,
                    "tags": [],
                    "selectable": False,
                    "warning": {
                        "kind": "malformed_frontmatter",
                        "message": str(exc),
                        "file": str(file),
                    },
                }
            )
            continue
        name = (meta.get("name") or file.parent.name).strip()
        if not name:
            continue
        records.append(
            SkillRecord(
                provider=provider.provider_id,
                skill_id=f"{provider.provider_id}/{name}",
                name=name,
                description=meta.get("description") or "",
                path=str(file),
                origin=EXTERNAL_PROVIDER_ORIGIN,
                source=EXTERNAL_PROVIDER_ORIGIN,
                tags=[
                    item.strip()
                    for item in (meta.get("tags") or "").split(",")
                    if item.strip()
                ],
                skill_kind=(meta.get("kind") or "helper").strip() or "helper",
            )
        )
    return records, warnings
