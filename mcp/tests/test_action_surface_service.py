from pathlib import Path

from aidocs_mcp.action_surface_service import ActionSurfaceService


class _DummySessions:
    def read_session(self, project_root: Path, session_id: str):
        class S:
            sections = {"Goal": ["- use task_begin and code_find"], "Scope": [], "State": [], "Upcoming": []}

        return S()

    def read_context(self, project_root: Path, session_id: str):
        class C:
            sections = {"Relevant Commands": [], "Session Facts": [], "Constraints": []}

        return C()


class _DummyCapabilities:
    def find_capabilities(self, project_root: Path, query=None, limit=1000):
        return [
            {"name": "task_begin", "capability_family": "task_lifecycle", "aliases": []},
            {"name": "execution_runs_get", "capability_family": "execution", "aliases": []},
            {"name": "project_status", "capability_family": "project_status", "aliases": []},
            {"name": "code_find", "capability_family": "code", "aliases": []},
        ]


class _DummyProcedures:
    def find_procedures(self, project_root: Path, query=None, limit=1000):
        return []


class _DummyExecution:
    def list_runs(self, project_root: Path, session_id: str | None = None, limit: int = 100):
        return [{"capability_name": "execution_runs_get"}, {"capability_name": "task_begin"}]

    def list_events(self, project_root: Path, session_id: str | None = None, limit: int = 100):
        return [{"capability_name": "project_status"}, {"capability_name": "code_find"}]


class _DummyHub:
    capabilities = _DummyCapabilities()
    procedures = _DummyProcedures()
    execution = _DummyExecution()
    sessions = _DummySessions()
    managed_mode = None


def test_derive_session_queries_filters_noisy_families() -> None:
    service = ActionSurfaceService(_DummyHub())

    queries = service._derive_session_queries(Path("."), session_id="s1", max_queries=10)

    assert "task_begin" in queries
    assert "code_find" in queries
    assert "execution_runs_get" not in queries
    assert "project_status" not in queries
