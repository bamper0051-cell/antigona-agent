from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.workspace import (
    BaseWorkspace,
    DaytonaWorkspace,
    ModalWorkspace,
    SSHWorkspace,
    WorkspaceConnectionError,
    WorkspaceFactory,
    WorkspaceFileResult,
    WorkspaceShellResult,
)


def test_factory_builds_all_five_backends(tmp_path: Path) -> None:
    backends = ["local", "docker", "ssh", "modal", "daytona"]
    for backend in backends:
        ws = WorkspaceFactory.create_workspace(backend=backend, workspace_dir=tmp_path / backend)
        assert ws.backend_type == backend
        assert ws.is_connected is True


def test_factory_config_passes_backend_kwargs(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="ssh",
        ssh_host="ssh.example.com",
        ssh_port=2222,
        ssh_username="deploy_user",
        ssh_key_path="/path/to/key",
    )
    ws_ssh = WorkspaceFactory.create_workspace(config=settings)
    assert isinstance(ws_ssh, SSHWorkspace)
    assert ws_ssh.host == "ssh.example.com"
    assert ws_ssh.port == 2222
    assert ws_ssh.username == "deploy_user"
    assert ws_ssh.key_path == "/path/to/key"

    settings_modal = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="modal",
        modal_app_name="my-modal-app",
        modal_environment="prod",
    )
    ws_modal = WorkspaceFactory.create_workspace(config=settings_modal)
    assert isinstance(ws_modal, ModalWorkspace)
    assert ws_modal.app_name == "my-modal-app"
    assert ws_modal.environment == "prod"

    settings_daytona = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="daytona",
        daytona_api_key="secret_api_key",
        daytona_region="us-east",
        daytona_workspace_id="ws-999",
    )
    ws_daytona = WorkspaceFactory.create_workspace(config=settings_daytona)
    assert isinstance(ws_daytona, DaytonaWorkspace)
    assert ws_daytona.api_key == "secret_api_key"
    assert ws_daytona.region == "us-east"
    assert ws_daytona.workspace_id == "ws-999"


def test_mock_flag_selects_mock_class(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="daytona",
        workspace_mock=True,
    )
    ws = WorkspaceFactory.create_workspace(config=settings)
    assert isinstance(ws, DaytonaWorkspace)
    assert ws.is_connected is True


def test_real_degrades_to_mock_without_sdk(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="ssh",
        workspace_mock=False,
    )
    ws = WorkspaceFactory.create_workspace(config=settings)
    assert ws.is_connected is False
    with pytest.raises(WorkspaceConnectionError) as exc_info:
        ws.connect()
    assert "SSH connection failed" in str(exc_info.value)


def test_task_scoped_workspace_isolation(tmp_path: Path) -> None:
    ws_parent = WorkspaceFactory.create_workspace(
        backend="local", workspace_dir=tmp_path, task_id="flow-parent"
    )
    ws_child = WorkspaceFactory.create_workspace(
        backend="local", workspace_dir=tmp_path, task_id="flow-child-1"
    )

    assert ws_parent.root_path == (tmp_path / "flow-parent").resolve()
    assert ws_child.root_path == (tmp_path / "flow-child-1").resolve()

    ws_parent.write_file("data.txt", "parent data")
    ws_child.write_file("data.txt", "child data")

    assert ws_parent.read_file("data.txt").content == "parent data"
    assert ws_child.read_file("data.txt").content == "child data"

    ws_child.cleanup()
    assert not (tmp_path / "flow-child-1").exists()


class CustomWorkspace(BaseWorkspace):
    def __init__(self, root_path: Path | str = "./workspace") -> None:
        self._root_path = Path(root_path).resolve()

    @property
    def backend_type(self) -> str:
        return "custom"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return True

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        return WorkspaceFileResult(path=str(path), content=content, sha256="dummy")

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        return WorkspaceFileResult(path=str(path), content="", sha256="dummy")

    def execute_command(
        self, command: Sequence[str], timeout: int = 30
    ) -> WorkspaceShellResult:
        return WorkspaceShellResult(command=tuple(command), exit_code=0, stdout="custom", stderr="")


def test_register_adds_new_backend(tmp_path: Path) -> None:
    WorkspaceFactory.register("custom", CustomWorkspace)
    ws = WorkspaceFactory.create_workspace(backend="custom", workspace_dir=tmp_path)
    assert isinstance(ws, CustomWorkspace)
    assert ws.backend_type == "custom"
    assert ws.execute_command(["test"]).stdout == "custom"
