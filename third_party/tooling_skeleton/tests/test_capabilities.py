from pathlib import Path

from antigona.tools import ToolContext, build_capability_snapshot, build_default_registry


def test_snapshot_exposes_registered_tools(tmp_path):
    snapshot = build_capability_snapshot(
        build_default_registry(),
        ToolContext("op", Path(tmp_path), platform="telegram"),
    )
    assert "run_pytest" in snapshot.available_tools
    assert all(item["type"] == "function" for item in snapshot.schemas_for_model)
