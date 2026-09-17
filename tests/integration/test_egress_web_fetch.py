from __future__ import annotations

import os
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.egress import Allowlist, EgressProxy, EgressUnavailableError
from antigona.repository import TaskRepository
from antigona.worker.agent_core import AgentCoreConfig, ScriptedConversation, WorkerAgentCore
from antigona.worker.quarantine import MockQuarantineProvider, QuarantineModel
from antigona.worker.tools import WebFetchTool
from antigona.workspace import LocalWorkspace


def _build_core_with_proxy(
    tmp_path: Path,
    proxy: EgressProxy,
) -> WorkerAgentCore:
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)
    persistence = tmp_path / "persistence"
    persistence.mkdir(parents=True, exist_ok=True)

    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    session = db.session_factory()

    config = AgentCoreConfig(
        workspace=LocalWorkspace(root_path=ws_dir),
        persistence_dir=persistence,
        conversation_id="test-egress-web-fetch",
        quarantine_model="mock-quarantine",
        model_primary="openrouter/anthropic/claude-3.5-sonnet",
        quarantine=QuarantineModel(
            primary_model="openrouter/anthropic/claude-3.5-sonnet",
            quarantine_model="mock-quarantine",
            provider=MockQuarantineProvider(),
        ),
        egress_proxy=proxy,
    )
    repo = TaskRepository(session)
    conversation = ScriptedConversation(persistence, "test-egress-web-fetch")
    return WorkerAgentCore(config=config, conversation=conversation, repository=repo)


def test_web_fetch_in_allowlist_succeeds(tmp_path: Path) -> None:
    class MockSuccessProxy(EgressProxy):
        def fetch(self, url: str) -> str:
            if not self.allowlist.contains("example.com"):
                raise EgressUnavailableError("domain not in allowlist")
            return "<h1>Title</h1><p>Fetched content from example.com</p>"

    proxy = MockSuccessProxy(allowlist=Allowlist.from_list(["example.com"]))
    core = _build_core_with_proxy(tmp_path, proxy)

    result = core._tool_web_fetch({"url": "https://example.com/docs"})

    assert result["url"] == "https://example.com/docs"
    assert result["enabled"] is True
    assert "Fetched content from example.com" in result["detail"]
    assert result["untrusted"] is True
    assert core.untrusted_context is True


def test_web_fetch_outside_allowlist_blocked(tmp_path: Path) -> None:
    proxy = EgressProxy(allowlist=Allowlist.from_list(["trusted.com"]))
    core = _build_core_with_proxy(tmp_path, proxy)

    result = core._tool_web_fetch({"url": "https://untrusted-domain.org/data"})

    assert result["enabled"] is False
    assert "egress blocked: domain not in allowlist: untrusted-domain.org" in result["detail"]
    assert result["untrusted"] is True


def test_web_fetch_proxy_unavailable_fail_closed(tmp_path: Path) -> None:
    class UnreachableProxy(EgressProxy):
        def fetch(self, url: str) -> str:
            raise EgressUnavailableError("proxy backend 502 Bad Gateway")

    proxy = UnreachableProxy(allowlist=Allowlist.from_list(["example.com"]))
    core = _build_core_with_proxy(tmp_path, proxy)

    result = core._tool_web_fetch({"url": "https://example.com/page"})

    assert result["enabled"] is False
    assert "egress blocked: proxy backend 502 Bad Gateway" in result["detail"]
    assert result["untrusted"] is True


@pytest.mark.skipif(
    "ANTIGONA_EGRESS_REAL" not in os.environ,
    reason="Real network fetch tests require ANTIGONA_EGRESS_REAL=1 environment variable",
)
def test_real_egress_fetch_optional() -> None:
    proxy = EgressProxy(allowlist=Allowlist.from_list(["example.com"]))
    tool = WebFetchTool(proxy=proxy)
    result = tool.fetch("https://example.com")
    assert result.enabled is True
    assert "Example Domain" in result.detail
