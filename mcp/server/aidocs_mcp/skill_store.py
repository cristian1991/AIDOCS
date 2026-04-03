from __future__ import annotations

import json
import re
from pathlib import Path

from aidocs_mcp import __version__
from aidocs_mcp.skill_provider import (
    BUNDLED_PROVIDER_ID,
    load_bundled_provider_skills,
    load_external_provider_skills,
    resolve_bundled_provider,
    resolve_external_provider,
    strip_frontmatter,
    validate_provider_id,
)
from aidocs_mcp.types import ExternalSkillProvider, SkillRecord

_PROVIDER_COMPATIBILITY: dict[str, dict[str, object]] = {
    "superpowers_external": {
        "compatible_versions": [">=5.0.0", "<6.0.0"],
        "choices": ["disable", "keep_enabled_anyway"],
    }
}
_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class SkillStore:
    def _provider_priority(self, provider: str) -> int:
        return {
            BUNDLED_PROVIDER_ID: 0,
            "project_local": 1,
        }.get(str(provider), 3)

    def _register_skill_payload(
        self, items: dict[str, dict[str, object]], payload: dict[str, object]
    ) -> None:
        skill_id = str(payload.get("skill_id") or "").strip()
        if not skill_id:
            return
        existing = items.get(skill_id)
        if existing is None or self._provider_priority(
            str(payload.get("provider") or "")
        ) < self._provider_priority(str(existing.get("provider") or "")):
            items[skill_id] = payload

    def _validated_selected_skills(
        self,
        project_root: Path,
        selected_skills: list[str],
    ) -> tuple[
        list[str], list[str], dict[str, dict[str, object]], dict[str, dict[str, object]]
    ]:
        available_skills = {
            str(item.get("skill_id") or ""): item
            for item in self.list_skills(project_root)
        }
        normalized_selected = self.normalize_selected_skill_ids(selected_skills)
        unknown_skill_ids: list[str] = []
        blocked_by_provider: dict[str, dict[str, object]] = {}
        for skill_id in normalized_selected:
            if not skill_id:
                continue
            skill = available_skills.get(skill_id)
            if not isinstance(skill, dict):
                unknown_skill_ids.append(skill_id)
                continue
            if not skill.get("selectable", True):
                provider_id = str(skill.get("provider") or "")
                if provider_id:
                    entry = blocked_by_provider.setdefault(
                        provider_id, {"provider": skill, "skill_ids": []}
                    )
                    entry["skill_ids"].append(skill_id)
        return (
            normalized_selected,
            unknown_skill_ids,
            blocked_by_provider,
            available_skills,
        )

    def _bundled_skill_aliases(self) -> dict[str, str]:
        provider = resolve_bundled_provider(self._built_in_dir())
        aliases: dict[str, str] = {}
        for record in load_bundled_provider_skills(provider, self._parse_frontmatter):
            canonical_skill_id = str(
                record.name or record.skill_id.rsplit("/", 1)[-1]
            ).strip()
            provider_skill_id = str(record.skill_id).strip()
            if canonical_skill_id:
                aliases[canonical_skill_id] = canonical_skill_id
            if provider_skill_id and canonical_skill_id:
                aliases[provider_skill_id] = canonical_skill_id
        return aliases

    def normalize_selected_skill_ids(self, selected_skills: list[str]) -> list[str]:
        bundled_aliases = self._bundled_skill_aliases()
        normalized: list[str] = []
        seen: set[str] = set()
        for item in selected_skills:
            skill_id = bundled_aliases.get(item.strip(), item.strip())
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            normalized.append(skill_id)
        return normalized

    def _parse_frontmatter(self, text: str) -> dict[str, str]:
        if not text.startswith("---\n"):
            return {}
        end = text.find("\n---\n", 4)
        if end == -1:
            return {}
        block = text[4:end]
        result: dict[str, str] = {}
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
        return result

    def _built_in_dir(self) -> Path:
        return Path(__file__).resolve().parents[3] / "core" / ".skills"

    def _project_dir(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "skills"

    def external_provider_registry_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "skill-providers.json"

    def legacy_external_provider_registry_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "skill-providers.json"

    def session_skill_state_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "skills.json"

    def _parse_semver(
        self, version: str | None
    ) -> tuple[tuple[int, int, int], tuple[tuple[int, object], ...] | None] | None:
        if not version:
            return None
        match = _SEMVER_RE.fullmatch(version.strip())
        if match is None:
            return None
        release = (
            int(match.group("major")),
            int(match.group("minor") or 0),
            int(match.group("patch") or 0),
        )
        prerelease_text = match.group("prerelease")
        if not prerelease_text:
            return release, None
        prerelease: list[tuple[int, object]] = []
        for part in prerelease_text.split("."):
            prerelease.append((0, int(part)) if part.isdigit() else (1, part.lower()))
        return release, tuple(prerelease)

    def _compare_semver(
        self,
        left: tuple[tuple[int, int, int], tuple[tuple[int, object], ...] | None],
        right: tuple[tuple[int, int, int], tuple[tuple[int, object], ...] | None],
    ) -> int:
        if left[0] != right[0]:
            return -1 if left[0] < right[0] else 1
        left_pre, right_pre = left[1], right[1]
        if left_pre == right_pre:
            return 0
        if left_pre is None:
            return 1
        if right_pre is None:
            return -1
        for left_part, right_part in zip(left_pre, right_pre):
            if left_part == right_part:
                continue
            if left_part[0] != right_part[0]:
                return -1 if left_part[0] < right_part[0] else 1
            return -1 if left_part[1] < right_part[1] else 1
        if len(left_pre) == len(right_pre):
            return 0
        return -1 if len(left_pre) < len(right_pre) else 1

    def _version_satisfies(self, version: str | None, constraint: str) -> bool:
        parsed_version = self._parse_semver(version)
        if parsed_version is None:
            return False
        operator = next(
            (
                item
                for item in (">=", "<=", ">", "<", "==")
                if constraint.startswith(item)
            ),
            None,
        )
        if operator is None:
            return False
        parsed_target = self._parse_semver(constraint[len(operator) :].strip())
        if parsed_target is None:
            return False
        comparison = self._compare_semver(parsed_version, parsed_target)
        if operator == ">=":
            return comparison >= 0
        if operator == "<=":
            return comparison <= 0
        if operator == ">":
            return comparison > 0
        if operator == "<":
            return comparison < 0
        return comparison == 0

    def _provider_skill_selectable(self, provider: ExternalSkillProvider) -> bool:
        return provider.compatibility_state in {
            "compatible",
            "incompatible_but_user_override",
        }

    def _compatibility_policy(self, provider_id: str) -> dict[str, object] | None:
        return _PROVIDER_COMPATIBILITY.get(provider_id)

    def _apply_provider_state(
        self, provider: ExternalSkillProvider
    ) -> ExternalSkillProvider:
        policy = self._compatibility_policy(provider.provider_id) or {}
        compatible_versions = [
            str(item)
            for item in policy.get("compatible_versions", [])
            if str(item).strip()
        ]
        choices = [str(item) for item in policy.get("choices", []) if str(item).strip()]
        compatibility_state = "compatible"
        if provider.user_choice == "disable":
            compatibility_state = "disabled"
        elif compatible_versions and not all(
            self._version_satisfies(provider.version, item)
            for item in compatible_versions
        ):
            compatibility_state = (
                "incompatible_but_user_override"
                if provider.user_choice == "keep_enabled_anyway"
                else "detected_incompatible"
            )
        return ExternalSkillProvider(
            provider_id=provider.provider_id,
            root_path=provider.root_path,
            version=provider.version,
            compatibility_state=compatibility_state,
            compatible_versions=compatible_versions,
            compatible_version_range=",".join(compatible_versions) or None,
            choices=choices,
            user_choice=provider.user_choice,
        )

    def _write_external_providers(
        self, project_root: Path, providers: list[ExternalSkillProvider]
    ) -> None:
        registry_path = self.external_provider_registry_path(project_root)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"providers": [item.to_dict() for item in providers]}
        registry_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _read_external_provider_payload(
        self, project_root: Path
    ) -> tuple[dict[str, object], Path | None]:
        registry_path = self.external_provider_registry_path(project_root)
        legacy_path = self.legacy_external_provider_registry_path(project_root)
        for candidate in (registry_path, legacy_path):
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                candidate == legacy_path
                and registry_path != legacy_path
                and not registry_path.exists()
            ):
                registry_path.parent.mkdir(parents=True, exist_ok=True)
                registry_path.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                try:
                    legacy_path.unlink()
                except OSError:
                    pass
            return payload if isinstance(payload, dict) else {}, candidate
        return {}, None

    def _skill_record_from_file(
        self,
        file: Path,
        *,
        provider: str,
        origin: str,
        source: str,
        skill_id: str | None = None,
    ) -> SkillRecord | None:
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        meta = self._parse_frontmatter(text)
        name = (meta.get("name") or file.stem).strip()
        if not name:
            return None
        return SkillRecord(
            provider=provider,
            skill_id=skill_id or name,
            name=name,
            description=meta.get("description") or "",
            path=str(file),
            origin=origin,
            source=source,
            tags=[
                item.strip()
                for item in (meta.get("tags") or "").split(",")
                if item.strip()
            ],
            content=strip_frontmatter(text),
            skill_kind=(meta.get("kind") or "helper").strip() or "helper",
        )

    def list_external_providers(
        self, project_root: Path
    ) -> list[ExternalSkillProvider]:
        payload, _ = self._read_external_provider_payload(project_root)
        providers = (
            payload.get("providers")
            if isinstance(payload.get("providers"), list)
            else []
        )
        result: list[ExternalSkillProvider] = []
        for item in providers:
            if not isinstance(item, dict):
                continue
            try:
                provider_id = validate_provider_id(str(item.get("provider_id") or ""))
            except ValueError:
                continue
            root_path = str(item.get("root_path") or "").strip()
            if not provider_id or not root_path:
                continue
            result.append(
                self._apply_provider_state(
                    ExternalSkillProvider(
                        provider_id=provider_id,
                        root_path=Path(root_path),
                        version=str(item.get("version") or "").strip() or None,
                        compatibility_state=str(
                            item.get("compatibility_state") or "unknown"
                        ).strip()
                        or "unknown",
                        compatible_versions=[
                            str(value) for value in item.get("compatible_versions", [])
                        ]
                        if isinstance(item.get("compatible_versions"), list)
                        else [],
                        compatible_version_range=str(
                            item.get("compatible_version_range") or ""
                        ).strip()
                        or None,
                        choices=[str(value) for value in item.get("choices", [])]
                        if isinstance(item.get("choices"), list)
                        else [],
                        user_choice=str(item.get("user_choice") or "").strip() or None,
                    )
                )
            )
        return result

    def get_external_provider(
        self, project_root: Path, provider_id: str
    ) -> ExternalSkillProvider:
        provider_key = validate_provider_id(provider_id)
        for provider in self.list_external_providers(project_root):
            if provider.provider_id == provider_key:
                return provider
        raise ValueError(f"Unknown external skill provider: {provider_key}")

    def _provider_status_payload(
        self, provider: ExternalSkillProvider
    ) -> dict[str, object]:
        return {
            "provider_id": provider.provider_id,
            "provider_state": provider.compatibility_state,
            "aidocs_version": __version__,
            "provider_version": provider.version,
            "compatible_versions": list(provider.compatible_versions),
            "compatible_version_range": provider.compatible_version_range,
            "choices": list(provider.choices),
            "user_choice": provider.user_choice,
        }

    def _structured_incompatible_selection_result(
        self,
        project_root: Path,
        session_id: str,
        blocked_skill_ids: list[str],
        provider_ids: list[str],
    ) -> dict[str, object]:
        providers = [
            self.get_external_provider(project_root, provider_id)
            for provider_id in provider_ids
        ]
        providers_payload = [
            self._provider_status_payload(provider) for provider in providers
        ]
        return {
            "ok": False,
            "error": "incompatible_provider",
            "session_id": session_id,
            "blocked_skill_ids": blocked_skill_ids,
            "provider": providers_payload[0] if providers_payload else None,
            "providers": providers_payload,
        }

    def register_external_provider(
        self, project_root: Path, *, provider_name: str, path: str
    ) -> dict[str, object]:
        provider = self._apply_provider_state(
            resolve_external_provider(provider_name, path, project_root)
        )
        providers = [
            item
            for item in self.list_external_providers(project_root)
            if item.provider_id != provider.provider_id
        ]
        providers.append(provider)
        providers.sort(key=lambda item: item.provider_id)
        self._write_external_providers(project_root, providers)
        return provider.to_dict()

    def set_external_provider_override(
        self, project_root: Path, provider_id: str, choice: str | None
    ) -> ExternalSkillProvider:
        provider_key = validate_provider_id(provider_id)
        normalized_choice = (choice or "").strip() or None
        providers = self.list_external_providers(project_root)
        updated: list[ExternalSkillProvider] = []
        matched = False
        for provider in providers:
            if provider.provider_id != provider_key:
                updated.append(provider)
                continue
            if normalized_choice not in {*provider.choices, None}:
                raise ValueError(f"Unsupported provider override: {choice}")
            matched = True
            updated_provider = self._apply_provider_state(
                ExternalSkillProvider(
                    provider_id=provider.provider_id,
                    root_path=provider.root_path,
                    version=provider.version,
                    compatibility_state=provider.compatibility_state,
                    compatible_versions=provider.compatible_versions,
                    compatible_version_range=provider.compatible_version_range,
                    choices=provider.choices,
                    user_choice=normalized_choice,
                )
            )
            updated.append(updated_provider)
        if not matched:
            raise ValueError(f"Unknown external skill provider: {provider_key}")
        updated.sort(key=lambda item: item.provider_id)
        self._write_external_providers(project_root, updated)
        return next(item for item in updated if item.provider_id == provider_key)

    def list_skills(self, project_root: Path) -> list[dict[str, object]]:
        items: dict[str, dict[str, object]] = {}
        bundled_provider = resolve_bundled_provider(self._built_in_dir())
        for record in load_bundled_provider_skills(
            bundled_provider, self._parse_frontmatter
        ):
            record_payload = record.to_dict()
            record_payload["provider_skill_id"] = record_payload["skill_id"]
            record_payload["skill_id"] = str(
                record_payload.get("name") or record.skill_id.rsplit("/", 1)[-1]
            )
            record_payload["provider_state"] = bundled_provider.compatibility_state
            record_payload["selectable"] = self._provider_skill_selectable(
                bundled_provider
            )
            self._register_skill_payload(items, record_payload)
        for source, directory in (("project", self._project_dir(project_root)),):
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.md")):
                record = self._skill_record_from_file(
                    file,
                    provider="project_local",
                    origin="project_local",
                    source=source,
                )
                if record is None:
                    continue
                self._register_skill_payload(items, record.to_dict())
        for provider in self.list_external_providers(project_root):
            records, warnings = load_external_provider_skills(
                provider, self._parse_frontmatter
            )
            for record in records:
                record_payload = record.to_dict()
                record_payload["provider_state"] = provider.compatibility_state
                record_payload["selectable"] = self._provider_skill_selectable(provider)
                self._register_skill_payload(items, record_payload)
            for warning in warnings:
                self._register_skill_payload(items, warning)
        return sorted(
            items.values(),
            key=lambda item: (
                self._provider_priority(str(item.get("provider"))),
                str(item.get("skill_id") or item.get("name") or ""),
            ),
        )

    def get_selected_skills(
        self, project_root: Path, session_id: str
    ) -> dict[str, object]:
        path = self.session_skill_state_path(project_root, session_id)
        if not path.is_file():
            return {
                "session_id": session_id,
                "path": str(path),
                "selected_skills": [],
                "invalid_selected_skills": [],
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        selected = (
            payload.get("selected_skills")
            if isinstance(payload.get("selected_skills"), list)
            else []
        )
        raw_selected = [str(item) for item in selected]
        normalized_selected = self.normalize_selected_skill_ids(raw_selected)
        available_skills = {
            str(item.get("skill_id") or ""): item
            for item in self.list_skills(project_root)
        }
        valid_selected = [
            skill_id for skill_id in normalized_selected if skill_id in available_skills
        ]
        invalid_selected = [
            skill_id
            for skill_id in normalized_selected
            if skill_id not in available_skills
        ]
        if valid_selected != raw_selected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"selected_skills": valid_selected}, indent=2) + "\n",
                encoding="utf-8",
            )
        return {
            "session_id": session_id,
            "path": str(path),
            "selected_skills": valid_selected,
            "invalid_selected_skills": invalid_selected,
        }

    def set_selected_skills(
        self, project_root: Path, session_id: str, selected_skills: list[str]
    ) -> dict[str, object]:
        (
            normalized_selected,
            unknown_skill_ids,
            blocked_by_provider,
            _available_skills,
        ) = self._validated_selected_skills(project_root, selected_skills)
        if unknown_skill_ids:
            raise ValueError("Unknown skill(s): " + ", ".join(unknown_skill_ids))
        blocked_skill_ids = [
            skill_id
            for item in blocked_by_provider.values()
            for skill_id in item["skill_ids"]
        ]
        if blocked_skill_ids:
            raise ValueError(
                f"Skill '{blocked_skill_ids[0]}' is not selectable in the current provider state."
            )
        path = self.session_skill_state_path(project_root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"selected_skills": normalized_selected}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return {
            "session_id": session_id,
            "path": str(path),
            "selected_skills": payload["selected_skills"],
        }

    def try_set_selected_skills(
        self, project_root: Path, session_id: str, selected_skills: list[str]
    ) -> dict[str, object]:
        (
            normalized_selected,
            unknown_skill_ids,
            blocked_by_provider,
            _available_skills,
        ) = self._validated_selected_skills(project_root, selected_skills)
        if unknown_skill_ids:
            return {
                "ok": False,
                "error": "unknown_skill",
                "session_id": session_id,
                "unknown_skill_ids": unknown_skill_ids,
            }
        blocked_skill_ids = [
            skill_id
            for item in blocked_by_provider.values()
            for skill_id in item["skill_ids"]
        ]
        blocked_provider_ids = sorted(blocked_by_provider)
        if blocked_skill_ids and blocked_provider_ids:
            return self._structured_incompatible_selection_result(
                project_root, session_id, blocked_skill_ids, blocked_provider_ids
            )
        result = self.set_selected_skills(project_root, session_id, selected_skills)
        result["ok"] = True
        return result
