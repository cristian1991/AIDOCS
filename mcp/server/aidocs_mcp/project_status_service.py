from __future__ import annotations

import json
from pathlib import Path


class ProjectStatusService:
    """Deterministic evaluator for a structured project status model."""

    def __init__(self, hub) -> None:
        self.hub = hub

    def config_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "project-status.json"

    def read_model(self, project_root: Path) -> dict[str, object] | None:
        path = self.config_path(project_root)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return None

    def evaluate(self, project_root: Path) -> dict[str, object]:
        model = self.read_model(project_root)
        if model is None:
            return {
                "exists": False,
                "path": str(self.config_path(project_root)),
                "score": 0,
                "areas": [],
            }

        areas = model.get("areas", [])
        evaluated_areas: list[dict[str, object]] = []
        total_weight = 0
        weighted_sum = 0.0

        for area in areas:
            weight = int(area.get("weight", 1))
            checks = area.get("checks", [])
            evaluated_checks = [self._evaluate_check(project_root, check) for check in checks]
            passed = sum(1 for item in evaluated_checks if item["passed"])
            total = len(evaluated_checks)
            score = 100 if total == 0 else round((passed / total) * 100)
            state = "ready" if score == 100 else "partial" if score > 0 else "missing"
            evaluated = {
                "id": area.get("id"),
                "title": area.get("title"),
                "weight": weight,
                "score": score,
                "state": state,
                "checks": evaluated_checks,
                "missing_checks": [item["label"] for item in evaluated_checks if not item["passed"]],
            }
            evaluated_areas.append(evaluated)
            total_weight += weight
            weighted_sum += score * weight

        overall = 0 if total_weight == 0 else round(weighted_sum / total_weight)
        return {
            "exists": True,
            "path": str(self.config_path(project_root)),
            "score": overall,
            "state": "ready" if overall == 100 else "partial" if overall > 0 else "missing",
            "areas": evaluated_areas,
        }

    def get_area_bundle(self, project_root: Path, area_id: str, limit: int = 20) -> dict[str, object]:
        model = self.read_model(project_root)
        if model is None:
            return {
                "exists": False,
                "area": None,
                "status": None,
                "bundle": None,
            }

        evaluated = self.evaluate(project_root)
        area = next((item for item in model.get("areas", []) if str(item.get("id")) == area_id), None)
        status_area = next((item for item in evaluated.get("areas", []) if str(item.get("id")) == area_id), None)
        if area is None:
            return {
                "exists": True,
                "area": None,
                "status": None,
                "bundle": None,
            }

        first_target = None
        for check in area.get("checks", []):
            target = str(check.get("target", "")).strip()
            if target:
                first_target = target
                break

        bundle = None
        if first_target:
            bundle = self.hub.code.get_subsystem_bundle(project_root, concept=first_target, limit=limit)

        return {
            "exists": True,
            "area": area,
            "status": status_area,
            "bundle": bundle,
        }

    def _evaluate_check(self, project_root: Path, check: dict[str, object]) -> dict[str, object]:
        check_type = str(check.get("type", ""))
        target = str(check.get("target", ""))
        label = str(check.get("label") or f"{check_type}:{target}")

        passed = False
        evidence = None

        if check_type == "schema_entity":
            entities = self.hub.schema.find_schema_entities(project_root, query=target, limit=50)
            passed = any(str(item["entity_name"]).lower() == target.lower() for item in entities)
            evidence = entities[:5]
        elif check_type == "schema_field":
            fields = self.hub.schema.find_schema_field(project_root, target, limit=50)
            passed = any(str(item["field_name"]).lower() == target.lower() for item in fields)
            evidence = fields[:5]
        elif check_type == "symbol":
            symbols = self.hub.code.search_symbols(project_root, query=target, limit=50)
            passed = any(str(item["symbol"]).lower() == target.lower() for item in symbols)
            evidence = symbols[:5]
        elif check_type == "route_match":
            routes = self.hub.code.find_routes(project_root, query=target, limit=50)
            passed = len(routes.get("matches", [])) > 0
            evidence = routes.get("matches", [])[:5]
        elif check_type == "ui_touchpoint":
            touch = self.hub.code.find_ui_backend_touchpoints(project_root, concept=target, limit=50)
            passed = any(item.get("layer") == "ui" for item in touch.get("matches", []))
            evidence = touch.get("matches", [])[:5]
        elif check_type == "policy_surface":
            policy = self.hub.code.find_policy_surfaces(project_root, concept=target, limit=50)
            passed = len(policy.get("matches", [])) > 0
            evidence = policy.get("matches", [])[:5]
        elif check_type == "domain_cluster":
            cluster = self.hub.code.find_domain_clusters(project_root, concept=target, limit=50)
            passed = len(cluster.get("cluster", [])) > 0
            evidence = cluster.get("cluster", [])[:5]
        elif check_type == "file_exists":
            candidate = project_root / target.replace("/", "\\")
            passed = candidate.exists()
            evidence = str(candidate)

        return {
            "type": check_type,
            "target": target,
            "label": label,
            "passed": passed,
            "evidence": evidence,
        }
