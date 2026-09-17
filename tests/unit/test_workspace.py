from __future__ import annotations

import socket
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.workspace import (
    BaseWorkspace,
    DaytonaWorkspace,
    DockerWorkspace,
    LocalWorkspace,
    ModalWorkspace,
    SSHWorkspace,
    WorkspaceFactory,
)


def test_workspace_factory_defaults_to_local(tmp_path: Path) -> None:
    ws = WorkspaceFactory.create_workspace(workspace_dir=tmp_path)
    assert isinstance(ws, LocalWorkspace)
    assert ws.backend_type == "local"
    assert ws.is_connected is True
    assert ws.root_path == tmp_path.resolve()


def test_workspace_factory_creates_all_backends(tmp_path: Path) -> None:
    docker_ws = WorkspaceFactory.create_workspace(backend="docker", workspace_dir=tmp_path)
    assert isinstance(docker_ws, DockerWorkspace)
    assert docker_ws.backend_type == "docker"
    assert docker_ws.is_connected is True

    ssh_ws = WorkspaceFactory.create_workspace(backend="ssh", workspace_dir=tmp_path)
    assert isinstance(ssh_ws, SSHWorkspace)
    assert ssh_ws.backend_type == "ssh"
    assert ssh_ws.is_connected is True

    modal_ws = WorkspaceFactory.create_workspace(backend="modal", workspace_dir=tmp_path)
    assert isinstance(modal_ws, ModalWorkspace)
    assert modal_ws.backend_type == "modal"
    assert modal_ws.is_connected is True

    daytona_ws = WorkspaceFactory.create_workspace(backend="daytona", workspace_dir=tmp_path)
    assert isinstance(daytona_ws, DaytonaWorkspace)
    assert daytona_ws.backend_type == "daytona"
    assert daytona_ws.is_connected is True


def test_workspace_factory_raises_on_unknown_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        WorkspaceFactory.create_workspace(backend="nonexistent", workspace_dir=tmp_path)
    assert "Unknown workspace backend" in str(exc_info.value)


@pytest.mark.parametrize(
    "backend_name, expected_cls",
    [
        ("local", LocalWorkspace),
        ("docker", DockerWorkspace),
        ("ssh", SSHWorkspace),
        ("modal", ModalWorkspace),
        ("daytona", DaytonaWorkspace),
    ],
)
def test_workspace_operations(
    tmp_path: Path, backend_name: str, expected_cls: type[BaseWorkspace]
) -> None:
    ws_dir = tmp_path / backend_name
    ws = WorkspaceFactory.create_workspace(backend=backend_name, workspace_dir=ws_dir)
    assert isinstance(ws, expected_cls)

    # Test file write
    write_res = ws.write_file("sub/file.txt", "hello workspace")
    assert write_res.path == "sub/file.txt"
    assert write_res.content == "hello workspace"
    assert len(write_res.sha256) == 64

    # Test file read
    read_res = ws.read_file("sub/file.txt")
    assert read_res.path == "sub/file.txt"
    assert read_res.content == "hello workspace"

    # Test command execution
    exec_res = ws.execute_command(["echo", "hi"])
    assert exec_res.exit_code == 0
    assert isinstance(exec_res.stdout, str)


def test_workspace_selection_via_settings(tmp_path: Path) -> None:
    settings_docker = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="docker",
    )
    ws_docker = WorkspaceFactory.create_workspace(config=settings_docker)
    assert isinstance(ws_docker, DockerWorkspace)

    settings_ssh = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="ssh",
    )
    ws_ssh = WorkspaceFactory.create_workspace(config=settings_ssh)
    assert isinstance(ws_ssh, SSHWorkspace)

    settings_modal = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="modal",
    )
    ws_modal = WorkspaceFactory.create_workspace(config=settings_modal)
    assert isinstance(ws_modal, ModalWorkspace)

    settings_daytona = Settings(
        database_url="sqlite:///:memory:",
        workspace=tmp_path,
        workspace_backend="daytona",
    )
    ws_daytona = WorkspaceFactory.create_workspace(config=settings_daytona)
    assert isinstance(ws_daytona, DaytonaWorkspace)


def test_mock_backends_require_no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_socket(*args: object, **kwargs: object) -> None:
        pytest.fail("Socket access attempted by mock workspace backend!")

    monkeypatch.setattr(socket, "socket", fail_socket)

    for backend in ["docker", "ssh", "modal", "daytona"]:
        ws = WorkspaceFactory.create_workspace(backend=backend, workspace_dir=tmp_path / backend)
        ws.connect()
        res = ws.execute_command(["echo", "test"])
        assert res.exit_code == 0


def test_mock_exec_tagged_output(tmp_path: Path) -> None:
    ws_docker = WorkspaceFactory.create_workspace(backend="docker", workspace_dir=tmp_path / "docker")
    res_docker = ws_docker.execute_command(["echo", "hello"])
    assert "[mock docker" in res_docker.stdout

    ws_ssh = WorkspaceFactory.create_workspace(backend="ssh", workspace_dir=tmp_path / "ssh")
    res_ssh = ws_ssh.execute_command(["echo", "hello"])
    assert "[mock ssh" in res_ssh.stdout

    ws_modal = WorkspaceFactory.create_workspace(backend="modal", workspace_dir=tmp_path / "modal")
    res_modal = ws_modal.execute_command(["echo", "hello"])
    assert "[mock modal" in res_modal.stdout

    ws_daytona = WorkspaceFactory.create_workspace(backend="daytona", workspace_dir=tmp_path / "daytona")
    res_daytona = ws_daytona.execute_command(["echo", "hello"])
    assert "[mock daytona" in res_daytona.stdout
