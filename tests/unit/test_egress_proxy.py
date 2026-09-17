from __future__ import annotations

import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from antigona.config import Settings
from antigona.egress import Allowlist, EgressProxy, EgressUnavailableError


def test_allowlist_empty_and_whitespace_entries() -> None:
    allowlist = Allowlist.from_list(["  ", "", "*.", "example.com"])
    assert allowlist.contains("example.com") is True
    assert allowlist.contains("other.com") is False


def test_allowlist_deny_by_default() -> None:
    allowlist = Allowlist(deny_by_default=True)
    assert allowlist.contains("example.com") is False
    assert allowlist.contains("localhost") is False

    proxy = EgressProxy(allowlist=allowlist)
    with pytest.raises(EgressUnavailableError, match="domain not in allowlist"):
        proxy.fetch("https://example.com")


def test_allowlist_exact_and_suffix_match() -> None:
    allowlist = Allowlist.from_list(["example.com", "*.wikipedia.org"])
    assert allowlist.contains("example.com") is True
    assert allowlist.contains("api.example.com") is False
    assert allowlist.contains("wikipedia.org") is True
    assert allowlist.contains("en.wikipedia.org") is True
    assert allowlist.contains("other.org") is False
    assert allowlist.contains("") is False


def test_allowlist_from_file_non_existent() -> None:
    allowlist = Allowlist.from_file(Path("/non/existent/path/allowlist.txt"))
    assert allowlist.contains("example.com") is False


def test_allowlist_from_file(tmp_path: Path) -> None:
    allow_file = tmp_path / "allowlist.txt"
    allow_file.write_text(
        "# Comment line\n\nexample.com\n*.github.com\n",
        encoding="utf-8",
    )

    allowlist = Allowlist.from_file(allow_file)
    assert allowlist.contains("example.com") is True
    assert allowlist.contains("api.github.com") is True
    assert allowlist.contains("github.com") is True
    assert allowlist.contains("forbidden.com") is False


def test_proxy_blocks_when_unavailable() -> None:
    class FailingTransportProxy(EgressProxy):
        def fetch(self, url: str) -> str:
            raise EgressUnavailableError("proxy endpoint connection refused")

    proxy = FailingTransportProxy(allowlist=Allowlist.from_list(["example.com"]))
    with pytest.raises(EgressUnavailableError, match="connection refused"):
        proxy.fetch("https://example.com")


def test_egress_proxy_from_settings_disabled() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspace=Path("/tmp"),
        egress_enabled=False,
    )
    proxy = EgressProxy.from_settings(settings)
    assert proxy.allowlist.contains("example.com") is False
    with pytest.raises(EgressUnavailableError, match="domain not in allowlist"):
        proxy.fetch("https://example.com")


def test_egress_proxy_from_settings_with_file_and_list(tmp_path: Path) -> None:
    allow_file = tmp_path / "allowlist.txt"
    allow_file.write_text("file-domain.com\n", encoding="utf-8")

    settings = Settings(
        database_url="sqlite:///:memory:",
        workspace=Path("/tmp"),
        egress_enabled=True,
        egress_allowlist=["list-domain.com"],
        egress_allowlist_file=str(allow_file),
    )
    proxy = EgressProxy.from_settings(settings)
    assert proxy.allowlist.contains("file-domain.com") is True
    assert proxy.allowlist.contains("list-domain.com") is True
    assert proxy.allowlist.contains("unlisted.com") is False


def test_fetch_validation_errors() -> None:
    proxy = EgressProxy(allowlist=Allowlist.from_list(["example.com"]))

    with pytest.raises(EgressUnavailableError, match="url must not be empty"):
        proxy.fetch("")

    with pytest.raises(EgressUnavailableError, match="unsupported URL scheme"):
        proxy.fetch("ftp://example.com/file")


def test_fetch_direct_urllib_success() -> None:
    proxy = EgressProxy(allowlist=Allowlist.from_list(["example.com"]))

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html>Success Page</html>"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        content = proxy.fetch("https://example.com/path")
        assert content == "<html>Success Page</html>"


def test_fetch_direct_urllib_failure() -> None:
    proxy = EgressProxy(allowlist=Allowlist.from_list(["example.com"]))

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        with pytest.raises(EgressUnavailableError, match="egress proxy direct request failed"):
            proxy.fetch("https://example.com/path")


def test_fetch_proxy_url_httpx_success() -> None:
    proxy = EgressProxy(
        allowlist=Allowlist.from_list(["example.com"]),
        proxy_url="http://127.0.0.1:8080",
    )

    mock_resp = MagicMock()
    mock_resp.text = "Proxy fetched content"
    mock_resp.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    with patch("httpx.Client", return_value=mock_client):
        content = proxy.fetch("https://example.com/path")
        assert content == "Proxy fetched content"


def test_fetch_proxy_url_httpx_failure() -> None:
    proxy = EgressProxy(
        allowlist=Allowlist.from_list(["example.com"]),
        proxy_url="http://127.0.0.1:8080",
    )

    mock_client = MagicMock()
    mock_client.get.side_effect = TimeoutError("Proxy connect timeout")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(EgressUnavailableError, match="egress proxy request failed"):
            proxy.fetch("https://example.com/path")


def test_no_network_in_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Network connection attempt detected in unit test!")

    monkeypatch.setattr(socket, "create_connection", forbidden_connect)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_connect)

    proxy = EgressProxy(allowlist=Allowlist.from_list(["example.com"]))
    with pytest.raises(EgressUnavailableError, match="domain not in allowlist"):
        proxy.fetch("https://forbidden.org")
