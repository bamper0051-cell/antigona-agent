"""Docker socket proxy protocol regressions (Phase C.3).

Live finding: ``docker run`` through the proxy printed

    Unsolicited response received on idle HTTP channel starting with
    "HTTP/1.1 400 Bad Request"

while output/exit code stayed correct.  Root cause: the request HEAD and BODY
arrive in the same read; the proxy treated the whole buffer as the head and
appended ``Connection: close`` AFTER the body, leaving a stray pseudo-request on
the wire that the daemon answered with 400 on the next (idle) read.

These tests lock the head/body split and the head rebuild in place.
"""
from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_proxy():
    path = REPO_ROOT / "deploy" / "sandbox" / "docker_socket_proxy.py"
    spec = importlib.util.spec_from_file_location("antigona_docker_socket_proxy_t", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pair():
    a, b = socket.socketpair()
    return a, b


def test_read_head_splits_head_from_a_body_sent_in_one_write() -> None:
    proxy = _load_proxy()
    body = b'{"Image":"python:3.12-alpine"}'
    raw = (
        b"POST /v1.55/containers/create HTTP/1.1\r\n"
        b"Host: api.moby.localhost\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Content-Type: application/json\r\n\r\n" + body
    )
    client, server = _pair()
    try:
        client.sendall(raw)
        read = proxy._read_head(server)
        assert read is not None
        head, remainder = read
        assert head.endswith(b"\r\n\r\n")
        assert body not in head, "body must not be part of the head"
        assert remainder == body
    finally:
        client.close()
        server.close()


def test_read_head_excludes_connection_close_from_the_body() -> None:
    """The core regression: no header bytes may trail the body."""
    proxy = _load_proxy()
    body = b'{"a":1}'
    head = (
        b"POST /v1.55/containers/create HTTP/1.1\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    client, server = _pair()
    try:
        client.sendall(head + body)
        read = proxy._read_head(server)
        assert read is not None
        got_head, got_body = read
        rebuilt = proxy._rebuild_head(got_head)
        assert b"Connection: close" in rebuilt
        assert body not in rebuilt
        assert got_body == body
    finally:
        client.close()
        server.close()


def test_rebuild_head_keeps_request_line_and_drops_client_connection() -> None:
    proxy = _load_proxy()
    head = (
        b"POST /v1.55/containers/create HTTP/1.1\r\n"
        b"Host: api.moby.localhost\r\n"
        b"Connection: keep-alive\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"Content-Length: 10\r\n\r\n"
    )
    rebuilt = proxy._rebuild_head(head)
    assert rebuilt.startswith(b"POST /v1.55/containers/create HTTP/1.1\r\n")
    assert b"keep-alive" not in rebuilt
    assert rebuilt.rstrip(b"\r\n").endswith(b"Connection: close")
    # exactly one blank-line terminator; no stray lines after it
    assert rebuilt.endswith(b"\r\n\r\n")
    assert rebuilt.count(b"Connection: close") == 1


def test_read_head_accepts_lf_only_terminator() -> None:
    proxy = _load_proxy()
    client, server = _pair()
    try:
        client.sendall(b"GET /_ping HTTP/1.1\nHost: x\n\nBODY")
        read = proxy._read_head(server)
        assert read is not None
        head, remainder = read
        assert head.endswith(b"\n\n")
        assert remainder == b"BODY"
    finally:
        client.close()
        server.close()
