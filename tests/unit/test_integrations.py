"""Unit tests for the external integration tools (email read/triage + fallbacks).

The email tools hit real Gmail IMAP; here they are mocked. The graceful-fallback
cloud tools must return a clear "not configured" error when no key is present —
registering them must never break a runtime that lacks credentials.
"""
import json

import pytest

from antigona.tools import integrations
from antigona.tools.registry import ToolRegistry


@pytest.fixture()
def registry():
    r = ToolRegistry()
    integrations.register(r)
    return r


class _FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def get_payload(self, decode=False):
        return self.payload


def _fake_imap(monkeypatch, items):
    """Patch antigona.tools.integrations.imaplib with a minimal stub.

    Also stubs credential loading: without it these tests silently depend on a
    real ``~/.antigona/secrets/gmail_creds.txt`` on the host and fail wherever
    it is absent.
    """

    class FakeIMAP:
        def __init__(self, *a, **k):
            self.msgs = items
            self.logged_in = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            self.logged_in = True
            return "OK"

        def select(self, folder):
            return ("OK", [b"3"])

        def search(self, *a):
            ids = b" ".join(str(i).encode() for i in range(1, len(self.msgs) + 1))
            return ("OK", [ids])

        def fetch(self, i, part):
            n = int(i)
            hdr = b"Subject: Test %d\r\nFrom: a@b.c\r\nDate: 2026-08-10\r\n\r\n" % n
            if "HEADER" in part:
                return ("OK", [(hdr, hdr)])
            return ("OK", [(hdr + b"body %d" % n, hdr + b"body %d" % n)])

        def logout(self):
            return "BYE"

    import antigona.tools.integrations as _integ
    monkeypatch.setattr(_integ.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(_integ, "_load_gmail_creds", lambda: ("test@example.com", "test-password"))


@pytest.mark.asyncio
async def test_email_list_parses_messages(monkeypatch, registry):
    _fake_imap(monkeypatch, [b"x", b"y"])
    res = json.loads(await registry.dispatch("email_list", limit=10))
    assert res["success"] is True
    assert res["count"] == 2
    assert res["items"][0]["subject"] == "Test 1"


@pytest.mark.asyncio
async def test_email_read_returns_body(monkeypatch, registry):
    _fake_imap(monkeypatch, [b"x"])
    res = json.loads(await registry.dispatch("email_read", message_id="1"))
    assert res["success"] is True
    assert res["subject"] == "Test 1"


@pytest.mark.asyncio
async def test_email_list_without_creds_returns_error(monkeypatch, registry):
    # gmail creds missing -> the IMAP login raises -> graceful error, not crash
    from antigona.core import email_sender

    def boom(*a, **k):
        raise RuntimeError("cannot read gmail creds")

    monkeypatch.setattr(email_sender, "_load_creds", boom)
    monkeypatch.setattr("antigona.tools.integrations._load_gmail_creds", boom)
    res = json.loads(await registry.dispatch("email_list", limit=5))
    assert res["success"] is False
    assert "failed" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("google_drive_list", {"query": ""}),
    ("slack_post", {"channel": "#x", "text": "hi"}),
    ("notion_query", {"database_id": "d"}),
    ("firecrawl_scrape", {"url": "https://example.com"}),
    ("serpapi_search", {"query": "test"}),
])
async def test_cloud_tools_report_not_configured(monkeypatch, registry, tool, args):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    res = json.loads(await registry.dispatch(tool, **args))
    assert res["success"] is False
    assert "not configured" in res["error"]
