"""Tests for P1-01 / Wave 8: Docker socket proxy container create payload inspection & denial."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_proxy():
    path = REPO_ROOT / "deploy" / "sandbox" / "docker_socket_proxy.py"
    spec = importlib.util.spec_from_file_location("antigona_docker_socket_proxy_p1", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proxy():
    return _load_proxy()


def _run_handle_request(proxy, payload: dict | bytes, raw_http: bytes | None = None) -> tuple[int, dict, bytes]:
    """Helper to run _handle through a connected socketpair.
    Returns (status_code, json_body, raw_response_bytes).
    """
    client, server = socket.socketpair()
    try:
        if raw_http is None:
            if isinstance(payload, dict):
                body = json.dumps(payload).encode("utf-8")
            else:
                body = payload
            raw_http = (
                b"POST /v1.41/containers/create HTTP/1.1\r\n"
                b"Host: api.moby.localhost\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
        client.sendall(raw_http)
        client.shutdown(socket.SHUT_WR)
        proxy._handle(server)

        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk

        lines = response.split(b"\r\n")
        status_line = lines[0].decode("latin-1")
        status_code = int(status_line.split()[1])

        body_idx = response.find(b"\r\n\r\n")
        resp_body = response[body_idx + 4 :] if body_idx != -1 else b""
        try:
            resp_json = json.loads(resp_body.decode("utf-8"))
        except Exception:
            resp_json = {}

        return status_code, resp_json, response
    finally:
        client.close()
        server.close()


def _assert_handle_denies(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, expected_snippet: str | tuple[str, ...]):
    """Assert that _validate_create_payload returns False AND _handle returns 403 end-to-end without touching upstream."""
    fake_upstream = tmp_path / f"fake_upstream_{os.getpid()}_{id(payload)}.sock"
    monkeypatch.setattr(proxy, "UPSTREAM", str(fake_upstream))

    valid, reason = proxy._validate_create_payload(payload)
    assert not valid, f"Expected validation failure for payload, got valid: {reason}"

    snippets = (expected_snippet,) if isinstance(expected_snippet, str) else expected_snippet
    matched_reason = any(s.lower() in reason.lower() for s in snippets)
    assert matched_reason, f"Expected one of {snippets} in reason '{reason}'"

    status_code, resp_json, raw = _run_handle_request(proxy, payload)
    assert status_code == 403, f"Expected HTTP 403, got {status_code}. Raw: {raw!r}"
    msg = resp_json.get("message", "").lower()
    matched_msg = any(s.lower() in msg for s in snippets)
    assert matched_msg, f"Expected one of {snippets} in response message '{msg}'"
    assert not fake_upstream.exists(), "Upstream socket must NEVER be created or connected on deny"


def _assert_handle_allows(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict):
    """Assert that _validate_create_payload returns True AND _handle forwards to upstream returning 201 Created."""
    valid, reason = proxy._validate_create_payload(payload)
    assert valid, f"Expected payload to be valid, got rejection: {reason}"

    mock_sock_path = tmp_path / f"mock_upstream_{os.getpid()}_{id(payload)}.sock"
    monkeypatch.setattr(proxy, "UPSTREAM", str(mock_sock_path))

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(str(mock_sock_path))
    server_sock.listen(1)

    upstream_received = []

    def _upstream_server():
        try:
            conn, _ = server_sock.accept()
            req = b""
            content_length = 0
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                req += chunk
                if b"\r\n\r\n" in req:
                    idx = req.find(b"\r\n\r\n")
                    head = req[:idx]
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            try:
                                content_length = int(line.split(b":", 1)[1].strip())
                            except ValueError:
                                pass
                    if len(req) >= idx + 4 + content_length:
                        break
            upstream_received.append(req)
            resp = (
                b"HTTP/1.1 201 Created\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 22\r\n\r\n"
                b'{"Id":"testcontainer"}'
            )
            conn.sendall(resp)
            conn.close()
        except Exception:
            pass
        finally:
            server_sock.close()

    th = threading.Thread(target=_upstream_server, daemon=True)
    th.start()

    status_code, resp_json, raw = _run_handle_request(proxy, payload)
    th.join(timeout=3)

    assert status_code == 201, f"Expected HTTP 201 Created, got {status_code}. Raw: {raw!r}"
    assert resp_json.get("Id") == "testcontainer"
    assert len(upstream_received) == 1, "Upstream mock must have received exactly one forwarded request"


# ---------------------------------------------------------------------------
# W8-3 / W8-2: REAL Allow Cases (Ground Truth Worker Shell & File Tool)
# ---------------------------------------------------------------------------

def test_ground_truth_shell_payload_allowed(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ALLOW: the exact ground-truth shell payload from 09_worker_shell_ground_truth.txt."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-slim",
        "User": "995:985",
        "WorkingDir": "/workspace",
        "HostConfig": {
            "Runtime": "runsc",
            "NetworkMode": "bridge",
            "ReadonlyRootfs": False,
            "CapDrop": ["ALL"],
            "CapAdd": ["DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 1073741824,
            "NanoCpus": 500000000,
            "PidsLimit": 64,
            "Binds": [f"{ws}:/workspace:rw"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
        },
    }

    _assert_handle_allows(proxy, monkeypatch, tmp_path, payload)


def test_file_tool_payload_allowed(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ALLOW: the file-tool payload (python:3.12-alpine, network=none, read_only, cap-drop=ALL, workspace bind)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }

    _assert_handle_allows(proxy, monkeypatch, tmp_path, payload)


# ---------------------------------------------------------------------------
# W8-1: B1 Volume Driver Bypass Denial Tests
# ---------------------------------------------------------------------------

def test_volume_driver_host_bind_bypass_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: VolumeOptions.DriverConfig with Options (e.g. type:none, o:bind, device:/)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Mounts": [
                {
                    "Type": "volume",
                    "Target": "/workspace",
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "local",
                            "Options": {
                                "type": "none",
                                "o": "bind",
                                "device": "/",
                            },
                        }
                    },
                }
            ],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "DriverConfig Options is forbidden")


def test_volume_driver_non_local_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: VolumeOptions.DriverConfig with non-local driver."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Mounts": [
                {
                    "Type": "volume",
                    "Target": "/workspace",
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "sshfs",
                        }
                    },
                }
            ],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "volume driver 'sshfs' is forbidden")


def test_volume_mount_source_path_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Volume mount with host path in Source."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": "/etc",
                    "Target": "/workspace",
                }
            ],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "cannot contain path separators")


def test_mount_forbidden_types_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Mounts with forbidden Types (e.g. npipe, cluster, unknown)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    for bad_type in ("npipe", "cluster", "image", "unknown_type"):
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                "Mounts": [
                    {
                        "Type": bad_type,
                        "Target": "/workspace",
                    }
                ],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, f"mount Type '{bad_type}' is forbidden")


# ---------------------------------------------------------------------------
# W8-1: Binds & Host Paths Denial Tests
# ---------------------------------------------------------------------------

def test_host_binds_outside_workspace_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Host paths outside allowed workspace in Binds."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    bad_paths = [
        "/",
        "/root",
        "/etc",
        "/var/run",
        "/var/run/docker.sock",
        "/run/antigona/docker.sock",
        "/var/lib/antigona",
        str(ws / ".."),
    ]
    for bad_path in bad_paths:
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                "Binds": [f"{bad_path}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, ("forbidden", "outside allowed workspace"))


def test_mounts_type_bind_outside_workspace_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Host paths outside workspace in Mounts Type=bind."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Mounts": [{"Type": "bind", "Source": "/etc", "Target": "/workspace"}],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "forbidden")


# ---------------------------------------------------------------------------
# W8-1 / W8-2: Dangerous Knobs & Capabilities Denial Tests
# ---------------------------------------------------------------------------

def test_privileged_payload_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Privileged mode in top-level or HostConfig."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload1 = {
        "Image": "python:3.12-alpine",
        "Privileged": True,
        "HostConfig": {"Runtime": "runsc", "Binds": [f"{ws}:/workspace:rw"]},
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload1, "privileged container forbidden")

    payload2 = {
        "Image": "python:3.12-alpine",
        "HostConfig": {"Runtime": "runsc", "Privileged": True, "Binds": [f"{ws}:/workspace:rw"]},
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload2, "privileged container forbidden")


def test_cap_add_dangerous_capabilities_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Dangerous capabilities in CapAdd."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    dangerous_caps = [
        "SYS_ADMIN",
        "SYS_PTRACE",
        "NET_ADMIN",
        "NET_RAW",
        "SYS_MODULE",
        "DAC_READ_SEARCH",
        "MKNOD",
        "SETFCAP",
        "SYS_CHROOT",
    ]
    for cap in dangerous_caps:
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                "CapAdd": [cap],
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, f"CapAdd capability '{cap}' is forbidden")


def test_cap_privileged_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: CapPrivileged: true."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "CapPrivileged": True,
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "CapPrivileged is forbidden")


def test_allowed_caps_env_extension(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ALLOW: Custom capabilities when explicitly allowed via ANTIGONA_ALLOWED_CAPS."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))
    monkeypatch.setenv("ANTIGONA_ALLOWED_CAPS", "NET_BIND_SERVICE,CAP_KILL")

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "CapAdd": ["NET_BIND_SERVICE", "KILL"],
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_allows(proxy, monkeypatch, tmp_path, payload)


def test_host_namespace_modes_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Host namespace sharing modes (PidMode, NetworkMode, IpcMode, UsernsMode, UTSMode, CgroupnsMode)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    for mode_key in ("PidMode", "NetworkMode", "IpcMode", "UsernsMode", "UTSMode", "CgroupnsMode"):
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                mode_key: "host",
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "forbidden")

    # container:<id> in NetworkMode
    payload_container_net = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "NetworkMode": "container:12345",
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload_container_net, "NetworkMode 'container:12345' is forbidden")


def test_devices_and_device_requests_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Devices, DeviceRequests, and DeviceCgroupRules."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    for key, val in [
        ("Devices", [{"PathOnHost": "/dev/sda"}]),
        ("DeviceRequests", [{"Driver": "cdi"}]),
        ("DeviceCgroupRules", ["c *:* rmw"]),
    ]:
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                key: val,
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, f"{key} is forbidden")


def test_group_add_forbidden_groups_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: GroupAdd containing docker, sudo, root, 0."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    for bad_grp in ("docker", "sudo", "root", "0", 0):
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                "GroupAdd": [bad_grp],
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, f"GroupAdd contains forbidden group '{bad_grp}'")


def test_security_opt_and_apparmor_weakening_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: SecurityOpt and AppArmorProfile weakening isolation."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    for sec_opt in (
        "seccomp=unconfined",
        "seccomp:unconfined",
        "apparmor=unconfined",
        "apparmor:unconfined",
        "label=disable",
        "label:disable",
        "label=unconfined",
        "label:unconfined",
    ):
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                "SecurityOpt": [sec_opt],
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, f"SecurityOpt '{sec_opt}' weakens isolation")

    for aa in ("unconfined", "disable"):
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                "AppArmorProfile": aa,
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, f"AppArmorProfile '{aa}' weakens isolation")


def test_runtime_enforcement(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Empty or runc runtime with flag off (fail-closed, must be runsc)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))
    monkeypatch.delenv("ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK", raising=False)

    # runc denied when flag is off
    payload_runc = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runc",
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload_runc, "runtime 'runc' is not allowed")

    # empty runtime denied
    payload_empty = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "",
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload_empty, "runtime '' is not allowed")

    # omitted runtime denied
    payload_omitted = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload_omitted, "runtime 'None' is not allowed")


def test_runtime_runc_with_fallback_hatch_allowed(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ALLOW: runtime 'runc' when ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK=1 (W9-1, W9-5)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))
    monkeypatch.setenv("ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK", "1")

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runc",
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_allows(proxy, monkeypatch, tmp_path, payload)


def test_truthy_parsing_agreement_with_runner(proxy, monkeypatch: pytest.MonkeyPatch):
    """Assert proxy truthy parsing agrees with antigona.sandbox.runner (W9-1, W9-5)."""
    from antigona.sandbox.runner import fallback_allowed as runner_fallback_allowed

    test_values = ["1", "true", "yes", "on", "0", "false", "no", "off", "", "True", " 1 ", "YES", "unknown"]
    for val in test_values:
        monkeypatch.setenv("ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK", val)
        proxy_res = proxy._is_runc_fallback_allowed()
        runner_res = runner_fallback_allowed()
        assert proxy_res == runner_res, f"Disagreement for value {val!r}: proxy={proxy_res}, runner={runner_res}"

    monkeypatch.delenv("ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK", raising=False)
    assert proxy._is_runc_fallback_allowed() is False
    assert runner_fallback_allowed() is False


def test_sysctls_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: non-empty Sysctls and wrong-typed Sysctls (W9-2, W9-3, W9-5)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    # non-empty dict denied
    payload_nonempty = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Sysctls": {"net.ipv4.ip_forward": "1"},
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload_nonempty, "Sysctls is forbidden")

    # non-dict (string) denied
    payload_str = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Sysctls": "net.ipv4.ip_forward=1",
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload_str, "Sysctls must be a dict")


def test_empty_sysctls_allowed(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ALLOW: empty dict Sysctls."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "python:3.12-alpine",
        "HostConfig": {
            "Runtime": "runsc",
            "Sysctls": {},
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_allows(proxy, monkeypatch, tmp_path, payload)


def test_type_failclosed_denials(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: wrong JSON types for SecurityOpt, GroupAdd, CapAdd, CapDrop, Mounts, Binds, Devices, DeviceCgroupRules (W9-3, W9-5)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    cases = [
        ("SecurityOpt", "seccomp=unconfined", "SecurityOpt must be a list"),
        ("GroupAdd", "docker", "GroupAdd must be a list"),
        ("CapAdd", "NET_ADMIN", "CapAdd must be a list"),
        ("CapDrop", "ALL", "CapDrop must be a list"),
        ("Mounts", {"Type": "bind", "Source": str(ws), "Target": "/workspace"}, "Mounts must be a list"),
        ("Binds", f"{ws}:/workspace:rw", "Binds must be a list"),
        ("Devices", "/dev/sda", "Devices must be a list"),
        ("DeviceCgroupRules", "c *:* rmw", "DeviceCgroupRules must be a list"),
    ]

    for key, val, expected_msg in cases:
        payload = {
            "Image": "python:3.12-alpine",
            "HostConfig": {
                "Runtime": "runsc",
                key: val,
                "Binds": [f"{ws}:/workspace:rw"] if key != "Binds" else val,
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, expected_msg)


def test_image_whitespace_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Image containing whitespace (W9-4, W9-5)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    for bad_image in ("python:3.12-alpine ", " python:3.12-alpine", "python:3.12 alpine"):
        payload = {
            "Image": bad_image,
            "HostConfig": {
                "Runtime": "runsc",
                "Binds": [f"{ws}:/workspace:rw"],
            },
        }
        _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "contains whitespace")


def test_untrusted_image_denied(proxy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """DENY: Images outside allowlist."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    payload = {
        "Image": "ubuntu:latest",
        "HostConfig": {
            "Runtime": "runsc",
            "Binds": [f"{ws}:/workspace:rw"],
        },
    }
    _assert_handle_denies(proxy, monkeypatch, tmp_path, payload, "image 'ubuntu:latest' is not in the allowed images list")
