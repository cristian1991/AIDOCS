from pathlib import Path

from aidocs_mcp.plan_conductor import PlanConductor
from aidocs_mcp.runtime_service import RuntimeService
from aidocs_mcp.service_hub import AidocsServiceHub


def _write_templates(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n- active\n\n"
        "## Status\n- active\n\n"
        "## Owner\n- agent\n\n"
        "## Goal\n- goal\n\n"
        "## Scope\n- scope\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- 2026-03-30 00:00\n",
        encoding="utf-8",
    )
    (root / "context.md").write_text("# Context\n", encoding="utf-8")


def _make_runtime(tmp_path: Path, plan_text: str) -> tuple[RuntimeService, Path, str]:
    templates = tmp_path / "templates"
    _write_templates(templates)
    hub = AidocsServiceHub(templates_root=templates)
    runtime = RuntimeService(hub=hub)
    project = tmp_path / "project"
    session_id = "2026-03-30-conductor-runtime"
    runtime.hub.sessions.create_session(
        project, session_id, "Conductor Runtime", "user", "Run lanes safely"
    )
    runtime.hub.sessions.plan_file(project, session_id).write_text(
        plan_text, encoding="utf-8"
    )
    return runtime, project, session_id


def _make_conductor(tmp_path: Path, plan_text: str) -> PlanConductor:
    runtime, project, session_id = _make_runtime(tmp_path, plan_text)
    return PlanConductor(runtime.hub, project, session_id)


def _assert_shared_file_overlap_blocks_parallel_lane(tmp_path: Path) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Shared file work\n"
        "- Lane: lane-a\n"
        "- Files: src/pages/index.tsx\n"
        "- [ ] Update shared page shell\n"
        "- Lane: lane-b\n"
        "- Files: src/pages/index.tsx\n"
        "- [ ] Add new homepage section\n",
    )

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == []
    assert result["blocked_reasons"]["lane-a"] == [
        "shared-file-overlap:src/pages/index.tsx:lane-b"
    ]
    assert result["blocked_reasons"]["lane-b"] == [
        "shared-file-overlap:src/pages/index.tsx:lane-a"
    ]


def test_conductor_marks_dependency_free_non_overlapping_lanes_runnable(
    tmp_path: Path,
) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Homepage foundation\n"
        "- Lane: homepage-hero\n"
        "- Files: src/components/home/Hero.tsx\n"
        "- [ ] Build hero component\n"
        "- Lane: homepage-feature-grid\n"
        "- Files: src/components/home/FeatureGrid.tsx\n"
        "- [ ] Build feature grid\n"
        "- Lane: homepage-integration\n"
        "- Files: src/pages/index.tsx\n"
        "- depends_on: homepage-hero, homepage-feature-grid\n"
        "- [ ] Integrate homepage\n",
    )

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == ["homepage-hero", "homepage-feature-grid"]
    assert result["blocked_reasons"]["homepage-integration"] == [
        "waiting-on:homepage-hero",
        "waiting-on:homepage-feature-grid",
    ]


def test_conductor_blocks_parallel_lanes_that_share_a_file(tmp_path: Path) -> None:
    _assert_shared_file_overlap_blocks_parallel_lane(tmp_path)


def test_conductor_overlap_blocks_parallel_lanes_that_share_a_file(
    tmp_path: Path,
) -> None:
    _assert_shared_file_overlap_blocks_parallel_lane(tmp_path)


def test_conductor_respects_sparse_hard_depends_on(tmp_path: Path) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: API work\n"
        "- Lane: api-contract\n"
        "- Files: src/api/contracts.py\n"
        "- [ ] Define API contract\n"
        "- Lane: docs\n"
        "- Files: docs/api.md\n"
        "- [ ] Draft docs\n"
        "- Lane: integration\n"
        "- Files: src/api/integration.py\n"
        "- depends_on: api-contract\n"
        "- [ ] Build integration\n",
    )

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == ["api-contract", "docs"]
    assert "integration" not in result["runnable_lane_ids"]
    assert result["waiting_on"] == {"integration": ["api-contract"]}


def test_conductor_blocks_later_phases_until_earlier_phase_is_resolved(
    tmp_path: Path,
) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: foundation-a\n"
        "- Files: src/foundation/a.py\n"
        "- [ ] Build foundation a\n"
        "- Lane: foundation-b\n"
        "- Files: src/foundation/b.py\n"
        "- [ ] Build foundation b\n"
        "- Phase: Integration\n"
        "- Lane: integration\n"
        "- Files: src/integration.py\n"
        "- [ ] Build integration\n",
    )

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == ["foundation-a", "foundation-b"]
    assert result["blocked_reasons"]["integration"] == ["waiting-on-phase:foundation"]


def test_conductor_unblocks_next_phase_when_earlier_phases_are_completed(
    tmp_path: Path,
) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Foundation\n"
        "- Lane: foundation-a\n"
        "- Files: src/foundation/a.py\n"
        "- [x] Build foundation a\n"
        "- Lane: foundation-b\n"
        "- Files: src/foundation/b.py\n"
        "- [x] Build foundation b\n"
        "- Phase: Integration\n"
        "- Lane: integration\n"
        "- Files: src/integration.py\n"
        "- [ ] Build integration\n",
    )

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == ["integration"]
    assert "integration" not in result["blocked_reasons"]


def test_conductor_blocks_same_phase_overlap_unconditionally(tmp_path: Path) -> None:
    _assert_shared_file_overlap_blocks_parallel_lane(tmp_path)


def test_conductor_allows_dependent_lane_with_shared_file_when_dependency_is_unresolved(
    tmp_path: Path,
) -> None:
    """When lane B depends on lane A and they share files, lane A should still be runnable.
    The overlap only prevents simultaneous execution, not the first lane from starting."""
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Dependent shared file work\n"
        "- Lane: lane-a\n"
        "- Files: src/shared.py\n"
        "- [ ] Build lane a\n"
        "- Lane: lane-b\n"
        "- Files: src/shared.py\n"
        "- depends_on: lane-a\n"
        "- [ ] Build lane b\n",
    )

    result = conductor.runnable_lanes()

    # lane-a should be runnable (no dependencies)
    assert "lane-a" in result["runnable_lane_ids"]
    # lane-b should be blocked by dependency, not by overlap
    assert "lane-b" not in result["runnable_lane_ids"]
    assert "waiting-on:lane-a" in result["blocked_reasons"]["lane-b"]


def test_conductor_canonicalizes_file_paths_before_overlap_checks(
    tmp_path: Path,
) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Canonicalization\n"
        "- Lane: lane-a\n"
        "- Files: src/foo.py\n"
        "- [ ] Update foo\n"
        "- Lane: lane-b\n"
        "- Files: src\\FOO.py\n"
        "- [ ] Update foo via alternate path form\n",
    )

    result = conductor.runnable_lanes()

    assert result["runnable_lane_ids"] == []
    assert result["blocked_reasons"]["lane-a"] == [
        "shared-file-overlap:src/foo.py:lane-b"
    ]
    assert result["blocked_reasons"]["lane-b"] == [
        "shared-file-overlap:src/foo.py:lane-a"
    ]


def test_conductor_graph_uses_canonical_file_owner_keys(tmp_path: Path) -> None:
    conductor = _make_conductor(
        tmp_path,
        "# Plan\n"
        "\n## Steps\n"
        "- Phase: Canonicalization\n"
        "- Lane: lane-a\n"
        "- Files: src/foo.py\n"
        "- [ ] Update foo\n"
        "- Lane: lane-b\n"
        "- Files: src\\FOO.py\n"
        "- [ ] Update foo via alternate path form\n",
    )

    graph = conductor.graph()

    assert graph["file_owners"] == {"src/foo.py": ["lane-a", "lane-b"]}
