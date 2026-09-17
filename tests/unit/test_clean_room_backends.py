from __future__ import annotations

import re
from pathlib import Path

from antigona.workspace import BaseWorkspace, WorkspaceFactory


def test_agent_core_does_not_access_local_tools_directly() -> None:
    agent_core_path = Path("src/antigona/worker/agent_core.py")
    content = agent_core_path.read_text(encoding="utf-8")

    # Verify self.file_tools and self.shell_tool are not present in agent_core.py
    assert "self.file_tools" not in content, "WorkerAgentCore must not access self.file_tools directly"
    assert "self.shell_tool" not in content, "WorkerAgentCore must not access self.shell_tool directly"


def test_clean_room_no_klio_tech_or_agpl_borrowing() -> None:
    source_files = [
        Path("src/antigona/workspace.py"),
        Path("src/antigona/worker/agent_core.py"),
        Path("src/antigona/config.py"),
    ]

    forbidden_patterns = [
        r"\bklio\b",
        r"\bklio-tech\b",
        r"\bopenclaw\b",
        r"\bhermes\b",
    ]

    for file_path in source_files:
        content = file_path.read_text(encoding="utf-8").lower()
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, content)
            assert not matches, f"Forbidden clean-room pattern '{pattern}' found in {file_path}"


def test_mock_backends_do_not_import_remote_sdks() -> None:
    import sys

    # Save original modules
    sdks = ["paramiko", "asyncssh", "modal", "daytona_sdk", "docker"]
    for sdk in sdks:
        sys.modules.pop(sdk, None)

    for backend in ["ssh", "modal", "daytona", "docker"]:
        ws = WorkspaceFactory.create_workspace(backend=backend, workspace_mock=True)
        assert isinstance(ws, BaseWorkspace)
        ws.write_file("dummy.txt", "content")
        res = ws.execute_command(["echo", "test"])
        assert res.exit_code == 0

    # Ensure none of the real SDKs were imported by mock execution
    for sdk in sdks:
        assert sdk not in sys.modules, f"Mock backend unexpectedly imported real SDK '{sdk}'"
