#!/usr/bin/env python3
"""Narrowly-scoped Docker socket proxy for the Antigona sandbox (least privilege).

WHY
---
``antigona-svc`` executes ``sandbox.shell`` / sandbox writes through the ``docker``
CLI, which needs the Docker daemon.  The main service units must NOT be granted
the ``docker`` group (that is host-root-equivalent and would widen every service).
Instead this dedicated, hardened unit is the ONLY component with Docker socket
access; it listens on a unix socket inside the service-private runtime dir
(``/run/antigona``, mode 0700 ``antigona-svc``) and forwards ONLY the container
lifecycle requests the sandbox uses.  Every other Docker API path is denied
before a byte reaches the daemon (deny-by-default).

HONEST ISOLATION STATEMENT
--------------------------
This proxy narrows the *API surface*, not the *container configuration rights*:
``POST /containers/create`` is inherently powerful (a container may bind-mount
arbitrary host paths, and the daemon runs as root).  The proxy therefore is a
privilege-BOUNDARY component, not a sandbox of the Docker daemon.  The value it
adds: 1) the six Antigona services never hold the docker group; 2) only the
endpoints the sandbox actually issues are reachable; 3) exec/build/volume/network/
image-pull/swarm endpoints are unreachable, so the daemon cannot be used as a
general host-root API through this path.

Runtime selection is FAIL-CLOSED (runsc/runc): containers are launched with the
runtime explicitly chosen by the caller (``--runtime=<runc|runsc>``), never left
to a daemon default.  gVisor ``runsc`` is REQUIRED.  When it is not usable
(missing binary / not registered with the daemon) the runtime resolver RAISES
``SandboxIsolationError`` and the command is REFUSED — the weaker ``runc``
runtime (shared host kernel) is never substituted silently.  The weakened
runtime is reachable only via the explicit escape hatch
``ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK=1`` (default OFF), which is logged loudly
and reported as a non-``gvisor`` isolation level (``degraded``/``runc``) by the
live probe in ``/health``, ``/status`` and the runtime state file
``<ANTIGONA_STATE_ROOT>/sandbox_isolation.json``.

PROTOCOL
--------
HTTP/1.x over unix sockets.  For every accepted connection the first request head
is parsed and validated against the allowlist; the head is forwarded to the
daemon with ``Connection: close`` (except hijacked ``/attach``), then both
directions are blind-piped until EOF.  Because every forwarded request forces the
daemon to close, the client must open a NEW connection per request, so no request
can bypass validation.  Fail-closed: an unparsable head or a non-allowlisted
method/path is answered with 403 and the upstream connection is never opened.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
from pathlib import Path
from typing import Any

UPSTREAM = os.environ.get("ANTIGONA_DOCKER_UPSTREAM", "/var/run/docker.sock")
LISTEN = os.environ.get("ANTIGONA_DOCKER_PROXY_SOCKET", "/run/antigona/docker.sock")
RECV_CAP = 64 * 1024
BODY_CAP = 1024 * 1024

_CREATE_RX = re.compile(r"^(/v1\.[0-9]+)?/containers/create$")

#: method + path allowlist.  Paths may carry a ``/v1.<n>`` API prefix.
_ALLOW: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/(v1\.[0-9]+/)?_ping$")),
    ("HEAD", re.compile(r"^/(v1\.[0-9]+/)?_ping$")),
    ("GET", re.compile(r"^/(v1\.[0-9]+/)?version$")),
    ("GET", re.compile(r"^/v1\.[0-9]+/info$")),
    # list/inspect containers (worker orphan cleanup + wait/inspect)
    ("GET", re.compile(r"^/v1\.[0-9]+/containers/json$")),
    ("GET", re.compile(r"^/v1\.[0-9]+/containers/[A-Za-z0-9_.-]+/json$")),
    ("GET", re.compile(r"^/v1\.[0-9]+/containers/[A-Za-z0-9_.-]+/logs$")),
    # lifecycle used by the sandbox runner
    ("POST", re.compile(r"^/v1\.[0-9]+/containers/create$")),
    ("POST", re.compile(r"^/v1\.[0-9]+/containers/[A-Za-z0-9_.-]+/(start|stop|kill|wait|restart)$")),
    ("POST", re.compile(r"^/v1\.[0-9]+/containers/[A-Za-z0-9_.-]+/attach$")),
    ("DELETE", re.compile(r"^/v1\.[0-9]+/containers/[A-Za-z0-9_.-]+$")),
    # image presence check only (never image create/pull/build)
    ("GET", re.compile(r"^/v1\.[0-9]+/images/[A-Za-z0-9_.:/@-]+/json$")),
    ("GET", re.compile(r"^/v1\.[0-9]+/images/json$")),
)


def _allowed(method: str, target: str) -> bool:
    path = target.split("?", 1)[0]
    return any(method == m and rx.match(path) for m, rx in _ALLOW)


def _is_container_create(method: str, target: str) -> bool:
    path = target.split("?", 1)[0]
    return method == "POST" and bool(_CREATE_RX.match(path))


ALLOW_RUNC_FALLBACK_ENV = "ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_runc_fallback_allowed() -> bool:
    """True only when the operator EXPLICITLY opted into the weaker runtime."""
    return os.environ.get(ALLOW_RUNC_FALLBACK_ENV, "").strip().lower() in _TRUTHY


def _get_allowed_runtimes() -> set[str]:
    allowed = {"runsc"}
    if _is_runc_fallback_allowed():
        allowed.add("runc")
    return allowed


def _get_allowed_workspace() -> Path:
    raw = (
        os.environ.get("ANTIGONA_WORKSPACE")
        or os.environ.get("ANTIGONA_WORKSPACE_ROOT")
        or "/var/lib/antigona/workspace"
    )
    return Path(raw).resolve()


def _get_allowed_images() -> set[str]:
    defaults = {"python:3.12-alpine", "python:3.12-slim"}
    env_img = os.environ.get("ANTIGONA_SANDBOX_IMAGE") or os.environ.get("ANTIGONA_DOCKER_IMAGE")
    if env_img:
        defaults.add(env_img.strip())
    allow_raw = os.environ.get("ANTIGONA_ALLOWED_IMAGES")
    if allow_raw:
        for item in allow_raw.split(","):
            clean = item.strip()
            if clean:
                defaults.add(clean)
    return defaults


def _get_allowed_caps() -> set[str]:
    defaults = {"DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "CHOWN"}
    allow_raw = os.environ.get("ANTIGONA_ALLOWED_CAPS")
    if allow_raw:
        for item in allow_raw.split(","):
            clean = item.strip().upper()
            if clean.startswith("CAP_"):
                clean = clean[4:]
            if clean:
                defaults.add(clean)
    return defaults


def _validate_create_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    """Inspect the container-create JSON payload and enforce sandbox boundaries (Wave 9)."""
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"

    if "HostConfig" in payload and payload["HostConfig"] is not None and not isinstance(payload["HostConfig"], dict):
        return False, "HostConfig must be a dict"
    host_cfg = payload.get("HostConfig") if isinstance(payload.get("HostConfig"), dict) else {}

    # 1. Privileged mode forbidden
    if payload.get("Privileged") is True or host_cfg.get("Privileged") is True:
        return False, "privileged container forbidden (HostConfig.Privileged: true)"
    if payload.get("Privileged") or host_cfg.get("Privileged"):
        return False, "privileged container forbidden"

    # 2. Image must be in allowlist and contain no whitespace (W9-4)
    raw_image = payload.get("Image")
    if raw_image is None or not isinstance(raw_image, str):
        return False, "Image must be a string"
    if not raw_image.strip():
        return False, f"image '{raw_image}' is empty"
    if any(c.isspace() for c in raw_image):
        return False, f"image '{raw_image}' contains whitespace"
    if raw_image not in _get_allowed_images():
        return False, f"image '{raw_image}' is not in the allowed images list"

    # 3. Runtime must be runsc (or runc when ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK is truthy) (W9-1)
    runtime = host_cfg.get("Runtime") if "Runtime" in host_cfg else payload.get("Runtime")
    if runtime is None or not isinstance(runtime, str) or str(runtime).strip().lower() not in _get_allowed_runtimes():
        return False, (
            f"runtime '{runtime}' is not allowed (must be 'runsc', or set "
            f"{ALLOW_RUNC_FALLBACK_ENV}=1 to allow 'runc')"
        )

    # 4. Sysctls forbidden (any non-empty Sysctls denied; must be dict if present) (W9-2, W9-3)
    sysctls = host_cfg.get("Sysctls") if "Sysctls" in host_cfg else payload.get("Sysctls")
    if sysctls is not None:
        if not isinstance(sysctls, dict):
            return False, "Sysctls must be a dict"
        if len(sysctls) > 0:
            return False, f"Sysctls is forbidden (got {sysctls})"

    # 5. CapAdd / CapDrop / CapPrivileged (W9-3)
    if host_cfg.get("CapPrivileged") or payload.get("CapPrivileged"):
        return False, "CapPrivileged is forbidden"

    cap_add = host_cfg.get("CapAdd") if "CapAdd" in host_cfg else payload.get("CapAdd")
    if cap_add is not None:
        if not isinstance(cap_add, (list, tuple)):
            return False, "CapAdd must be a list"
        allowed_caps = _get_allowed_caps()
        for cap in cap_add:
            if not isinstance(cap, str):
                return False, "CapAdd entries must be strings"
            cap_str = cap.strip().upper()
            if cap_str.startswith("CAP_"):
                cap_str = cap_str[4:]
            if cap_str not in allowed_caps:
                return False, f"CapAdd capability '{cap}' is forbidden (allowed: {sorted(allowed_caps)})"

    cap_drop = host_cfg.get("CapDrop") if "CapDrop" in host_cfg else payload.get("CapDrop")
    if cap_drop is not None:
        if not isinstance(cap_drop, (list, tuple)):
            return False, "CapDrop must be a list"
        for cap in cap_drop:
            if not isinstance(cap, str):
                return False, "CapDrop entries must be strings"

    # 6. NetworkMode: allow none, bridge, default; deny host and container:<id>
    network_mode = host_cfg.get("NetworkMode") if "NetworkMode" in host_cfg else payload.get("NetworkMode")
    if network_mode is not None and str(network_mode).strip() != "":
        net_val = str(network_mode).strip().lower()
        if net_val == "host" or net_val.startswith("container:"):
            return False, f"NetworkMode '{network_mode}' is forbidden"
        if net_val not in ("none", "bridge", "default"):
            return False, f"NetworkMode '{network_mode}' is not allowed (only 'none', 'bridge', 'default' permitted)"

    # 7. Host namespace sharing modes forbidden
    for mode_key in ("PidMode", "IpcMode", "UsernsMode", "UTSMode", "CgroupnsMode"):
        mode_val = host_cfg.get(mode_key) if mode_key in host_cfg else payload.get(mode_key)
        if mode_val is not None:
            val = str(mode_val).strip().lower()
            if val == "host" or val.startswith("container:"):
                return False, f"{mode_key}: {val} is forbidden"

    # 8. Devices / DeviceRequests / DeviceCgroupRules forbidden (W9-3)
    devices = host_cfg.get("Devices") if "Devices" in host_cfg else payload.get("Devices")
    if devices is not None:
        if not isinstance(devices, (list, tuple)):
            return False, "Devices must be a list"
        if len(devices) > 0:
            return False, "Devices is forbidden"

    dev_reqs = host_cfg.get("DeviceRequests") if "DeviceRequests" in host_cfg else payload.get("DeviceRequests")
    if dev_reqs is not None:
        if not isinstance(dev_reqs, (list, tuple)):
            return False, "DeviceRequests must be a list"
        if len(dev_reqs) > 0:
            return False, "DeviceRequests is forbidden"

    dev_cgroup = host_cfg.get("DeviceCgroupRules") if "DeviceCgroupRules" in host_cfg else payload.get("DeviceCgroupRules")
    if dev_cgroup is not None:
        if not isinstance(dev_cgroup, (list, tuple)):
            return False, "DeviceCgroupRules must be a list"
        if len(dev_cgroup) > 0:
            return False, "DeviceCgroupRules is forbidden"

    # 9. GroupAdd forbidden if it contains docker / sudo / root / 0 (W9-3)
    group_add = host_cfg.get("GroupAdd") if "GroupAdd" in host_cfg else payload.get("GroupAdd")
    if group_add is not None:
        if not isinstance(group_add, (list, tuple)):
            return False, "GroupAdd must be a list"
        for grp in group_add:
            if str(grp).strip().lower() in {"docker", "sudo", "root", "0"} or grp == 0:
                return False, f"GroupAdd contains forbidden group '{grp}'"

    # 10. SecurityOpt & AppArmorProfile weakening isolation forbidden (W9-3)
    sec_opts = host_cfg.get("SecurityOpt") if "SecurityOpt" in host_cfg else payload.get("SecurityOpt")
    if sec_opts is not None:
        if not isinstance(sec_opts, (list, tuple)):
            return False, "SecurityOpt must be a list"
        for opt in sec_opts:
            if not isinstance(opt, str):
                return False, "SecurityOpt entries must be strings"
            opt_clean = opt.lower().replace(" ", "")
            for bad in (
                "seccomp=unconfined",
                "seccomp:unconfined",
                "apparmor=unconfined",
                "apparmor:unconfined",
                "label=disable",
                "label:disable",
                "label=unconfined",
                "label:unconfined",
            ):
                if bad in opt_clean:
                    return False, f"SecurityOpt '{opt}' weakens isolation"

    apparmor_profile = host_cfg.get("AppArmorProfile") if "AppArmorProfile" in host_cfg else payload.get("AppArmorProfile")
    if apparmor_profile is not None:
        if not isinstance(apparmor_profile, str):
            return False, "AppArmorProfile must be a string"
        aa_clean = apparmor_profile.strip().lower()
        if aa_clean in ("unconfined", "disable"):
            return False, f"AppArmorProfile '{apparmor_profile}' weakens isolation"

    # 11. Binds & Mounts isolation: host paths outside configured workspace forbidden (W9-3)
    forbidden_roots = {
        "/",
        "/root",
        "/etc",
        "/var",
        "/var/run",
        "/run",
        "/bin",
        "/sbin",
        "/usr",
        "/lib",
        "/lib64",
        "/proc",
        "/sys",
        "/dev",
        "/home",
        "/opt",
        "/boot",
        "/srv",
        "/mnt",
        "/media",
        "/tmp",
        "/var/lib/antigona",
    }
    allowed_ws = _get_allowed_workspace()

    # Inspect Binds
    binds = host_cfg.get("Binds") if "Binds" in host_cfg else payload.get("Binds")
    if binds is not None:
        if not isinstance(binds, (list, tuple)):
            return False, "Binds must be a list"
        for bind in binds:
            if not isinstance(bind, str):
                return False, "invalid bind entry"
            parts = bind.split(":")
            if len(parts) < 2:
                return False, f"invalid bind format '{bind}'"
            host_src = Path(parts[0]).resolve()
            container_dst = parts[1]
            host_src_str = str(host_src)

            if host_src_str in forbidden_roots or "docker.sock" in host_src_str or host_src_str.startswith("/run/antigona"):
                return False, f"host path '{host_src_str}' is strictly forbidden in binds"
            if host_src != allowed_ws and not host_src.is_relative_to(allowed_ws):
                return False, f"host path '{host_src}' is outside allowed workspace '{allowed_ws}'"
            if not container_dst.startswith("/workspace"):
                return False, f"container mount target '{container_dst}' is not /workspace"

    # Inspect Mounts (validate EVERY mount type)
    mounts = host_cfg.get("Mounts") if "Mounts" in host_cfg else payload.get("Mounts")
    if mounts is not None:
        if not isinstance(mounts, (list, tuple)):
            return False, "Mounts must be a list"
        for m in mounts:
            if not isinstance(m, dict):
                return False, "invalid mount object"
            m_type = str(m.get("Type") or "bind").strip().lower()
            if m_type == "bind":
                raw_src = m.get("Source")
                if not raw_src or not isinstance(raw_src, str):
                    return False, "bind mount missing Source"
                source = Path(raw_src).resolve()
                target = str(m.get("Target", ""))
                source_str = str(source)
                if source_str in forbidden_roots or "docker.sock" in source_str or source_str.startswith("/run/antigona"):
                    return False, f"mount source '{source_str}' is strictly forbidden"
                if source != allowed_ws and not source.is_relative_to(allowed_ws):
                    return False, f"mount source '{source}' is outside allowed workspace '{allowed_ws}'"
                if not target.startswith("/workspace"):
                    return False, f"mount target '{target}' is not /workspace"
            elif m_type == "volume":
                # Validate volume driver config (W8-1 / B1 fix)
                vol_opts = m.get("VolumeOptions")
                if vol_opts is not None:
                    if not isinstance(vol_opts, dict):
                        return False, "invalid VolumeOptions in mount"
                    driver_cfg = vol_opts.get("DriverConfig")
                    if driver_cfg is not None:
                        if not isinstance(driver_cfg, dict):
                            return False, "invalid DriverConfig in VolumeOptions"
                        driver_name = str(driver_cfg.get("Name") or "").strip().lower()
                        if driver_name not in ("", "local"):
                            return False, f"volume driver '{driver_name}' is forbidden"
                        driver_options = driver_cfg.get("Options")
                        if driver_options:
                            return False, f"volume DriverConfig Options is forbidden (got {driver_options})"
                source = m.get("Source")
                if source and isinstance(source, str):
                    if "/" in source or "\\" in source or ".." in source:
                        return False, f"volume Source '{source}' cannot contain path separators"
                target = str(m.get("Target", ""))
                if not target:
                    return False, "volume mount missing Target"
            elif m_type == "tmpfs":
                target = str(m.get("Target", ""))
                if not target:
                    return False, "tmpfs mount missing Target"
            else:
                return False, f"mount Type '{m_type}' is forbidden"

    return True, "ok"


def _rebuild_head(head: bytes) -> bytes:
    """Rebuild a forwarded request head with a forced ``Connection: close``.

    *head* MUST contain only the request head (headers + blank-line terminator,
    no body — see :func:`_read_head`).  Client ``Connection``/``Proxy-Connection``
    headers are dropped line-wise so every forwarded request makes the daemon
    close, forcing the client to reconnect (and therefore be re-validated).
    """
    crlf = b"\r\n" if b"\r\n" in head else b"\n"
    lines = head.rstrip(b"\r\n").split(crlf)
    lines = [
        ln for ln in lines
        if not ln.lower().startswith((b"connection:", b"proxy-connection:"))
    ]
    lines.append(b"Connection: close")
    return crlf.join(lines) + crlf + crlf


def _read_head(conn: socket.socket) -> tuple[bytes, bytes] | None:
    """Read the request head through its blank-line terminator.

    Returns ``(head, remainder)`` where *head* ends exactly at the header
    terminator (``CRLFCRLF``/``LFLF``) and *remainder* is any body bytes that
    already arrived in the same read.  ``None`` on EOF/oversize.
    """
    buf = bytearray()
    while True:
        for sep in (b"\r\n\r\n", b"\n\n"):
            idx = buf.find(sep)
            if idx != -1:
                end = idx + len(sep)
                return bytes(buf[:end]), bytes(buf[end:])
        try:
            chunk = conn.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None if not buf else (bytes(buf), b"")
        buf.extend(chunk)
        if len(buf) > RECV_CAP:
            return None


def _get_content_length(head: bytes) -> int | None:
    for line in head.split(b"\n"):
        line_str = line.decode("latin-1", errors="replace").strip()
        if line_str.lower().startswith("content-length:"):
            try:
                return int(line_str.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _deny(conn: socket.socket, message: str = "antigona docker proxy: request not allowed by allowlist") -> None:
    body = json.dumps({"message": message}).encode("utf-8")
    conn.sendall(
        b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
    )


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(conn: socket.socket) -> None:
    read = _read_head(conn)
    if not read:
        conn.close()
        return
    head, body = read
    line = head.split(b"\r\n", 1)[0].split(b"\n", 1)[0]
    parts = line.decode("latin-1").split()
    if len(parts) < 3:
        _deny(conn, "malformed request")
        conn.close()
        return
    method, target = parts[0].upper(), parts[1]
    if not _allowed(method, target):
        sys.stderr.write(f"antigona docker proxy: DENY {method} {target}\n")
        sys.stderr.flush()
        _deny(conn)
        conn.close()
        return

    # For container create: inspect body before opening upstream socket
    if _is_container_create(method, target):
        content_len = _get_content_length(head)
        if content_len is None:
            content_len = len(body)
        if content_len > BODY_CAP:
            sys.stderr.write(f"antigona docker proxy: DENY {method} {target} payload too large\n")
            sys.stderr.flush()
            _deny(conn, "create payload exceeds size cap")
            conn.close()
            return

        body_buf = bytearray(body)
        while len(body_buf) < content_len:
            try:
                chunk = conn.recv(min(4096, content_len - len(body_buf)))
                if not chunk:
                    break
                body_buf.extend(chunk)
            except OSError:
                break

        body = bytes(body_buf[:content_len])
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception as exc:
            sys.stderr.write(f"antigona docker proxy: DENY invalid JSON create payload: {exc}\n")
            sys.stderr.flush()
            _deny(conn, f"invalid JSON create payload: {exc}")
            conn.close()
            return

        is_valid, reason = _validate_create_payload(payload)
        if not is_valid:
            sys.stderr.write(f"antigona docker proxy: DENY {method} {target} reason={reason}\n")
            sys.stderr.flush()
            _deny(conn, f"container create denied: {reason}")
            conn.close()
            return

    sys.stderr.write(f"antigona docker proxy: ALLOW {method} {target}\n")
    sys.stderr.flush()
    hijack = target.split("?", 1)[0].endswith("/attach")
    # Force the daemon to close after this response (except hijacked attach), so
    # the client reconnects per request and every request is re-validated.
    headers = head
    if not hijack:
        headers = _rebuild_head(head)

    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.connect(UPSTREAM)
    except OSError:
        up.close()
        _deny(conn, "docker daemon unavailable")
        conn.close()
        return
    if os.environ.get("ANTIGONA_DOCKER_PROXY_DEBUG"):
        sys.stderr.write(f"FWD>>> {headers!r}\n")
        sys.stderr.flush()
    try:
        up.sendall(headers)
        if body:
            # Forward the request body that arrived with the head; everything
            # after it keeps streaming through the conn->up pipe.
            up.sendall(body)
        t = threading.Thread(target=_pipe, args=(conn, up), daemon=True)
        t.start()
        _pipe(up, conn)
        t.join(timeout=5)
    finally:
        try:
            up.close()
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    parent = os.path.dirname(LISTEN)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.exists(LISTEN):
        os.unlink(LISTEN)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(LISTEN)
    os.chmod(LISTEN, 0o660)
    srv.listen(64)
    if _is_runc_fallback_allowed():
        sys.stderr.write(
            f"antigona docker proxy: WARNING: {ALLOW_RUNC_FALLBACK_ENV} is active; "
            f"admitting weaker runtime 'runc' in addition to 'runsc'\n"
        )
    sys.stderr.write(f"antigona docker proxy: {LISTEN} -> {UPSTREAM}\n")
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())

