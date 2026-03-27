from aidocs_mcp.mcp_server import _apply_trace_depth


def test_apply_trace_depth_filters_service_matches_by_source_level() -> None:
    payload = {
        "matches": [
            {"source": "definition", "symbol": "Service"},
            {"source": "reference", "symbol": "Service"},
            {"source": "file_match", "symbol": None},
        ]
    }

    result = _apply_trace_depth(payload, "service", 2)

    assert len(result["matches"]) == 2
    assert {item["source"] for item in result["matches"]} == {"definition", "reference"}


def test_apply_trace_depth_trims_api_to_ui_layers() -> None:
    payload = {
        "api": [{"path": "server/routes/session.ts"}],
        "logic": [{"path": "core/session.ts"}],
        "ui": [{"path": "app/session.tsx"}],
    }

    result = _apply_trace_depth(payload, "api_to_ui", 2)

    assert result["api"]
    assert result["logic"]
    assert result["ui"] == []
