"""Unit tests for web search/fetch and the Ollama tool.

web_search/web_fetch use DuckDuckGo Lite HTML; here _http_text is mocked.
The ollama tool's switch/status paths are mocked to avoid local runtime deps.
"""
import json

import pytest

from antigona.tools import integrations
from antigona.tools.registry import ToolRegistry

DDG_HTML = (
    "<a rel='nofollow' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1' "
    "class='result-link'>Example A</a>"
    "<td class='result-snippet'>snippet A</td>"
    "<a rel='nofollow' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb&amp;rut=2' "
    "class='result-link'>Example B</a>"
)


@pytest.fixture()
def registry():
    r = ToolRegistry()
    integrations.register(r)
    return r


@pytest.mark.asyncio
async def test_web_search_parses_results(monkeypatch, registry):
    monkeypatch.setattr("antigona.tools.integrations._http_text", lambda *a, **k: DDG_HTML)
    res = json.loads(await registry.dispatch("web_search", query="test"))
    assert res["success"] is True
    assert res["count"] == 2
    assert res["items"][0]["title"] == "Example A"
    assert res["items"][0]["url"] == "https://example.com/a"
    assert res["items"][0]["snippet"] == "snippet A"


@pytest.mark.asyncio
async def test_web_search_requires_query(registry):
    res = json.loads(await registry.dispatch("web_search", query="   "))
    assert res["success"] is False


@pytest.mark.asyncio
async def test_web_fetch_extracts_text(monkeypatch, registry):
    monkeypatch.setattr(
        "antigona.tools.integrations._http_text",
        lambda *a, **k: "<html><head><style>a</style></head><body><h1>Title</h1><p>Hello world</p></body></html>",
    )
    res = json.loads(await registry.dispatch("web_fetch", url="https://example.com"))
    assert res["success"] is True
    assert "Title" in res["text"]
    assert "Hello world" in res["text"]


@pytest.mark.asyncio
async def test_ollama_status(monkeypatch, registry):
    monkeypatch.setattr("antigona.tools.ollama_tool._serving", lambda: True)
    monkeypatch.setattr("antigona.tools.ollama_tool._list_models", lambda: ["qwen2.5:1.5b"])
    res = json.loads(await registry.dispatch("ollama", action="status"))
    assert res["success"] is True
    assert res["serving"] is True
    assert res["models"] == ["qwen2.5:1.5b"]


@pytest.mark.asyncio
async def test_ollama_switch(monkeypatch, registry, tmp_path):
    monkeypatch.setattr(
        "antigona.tools.provider_switcher.switch_to_provider",
        lambda name: (True, f"Switched to {name}"),
    )
    # F-20260918T2000Z: ``switch`` mutates the active LLM provider and is now a
    # gated, owner-approved action, so the dispatch must carry a valid one-shot
    # grant.  The ungated read-only actions (``status``/``list``) are unchanged.
    from antigona.security.approval_grant import ApprovalGrantStore

    registry.grant_store = ApprovalGrantStore(tmp_path / "grants.sqlite")
    token = registry.grant_store.issue(
        actor="owner", tool_name="ollama", args={"action": "switch"}, issuer="test"
    )
    res = json.loads(
        await registry.dispatch("ollama", action="switch", approval_token=token)
    )
    assert res["success"] is True


@pytest.mark.asyncio
async def test_ollama_unknown_action(registry):
    res = json.loads(await registry.dispatch("ollama", action="bogus"))
    assert res["success"] is False
