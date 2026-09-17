"""Built-in external integrations for Antigona (owner-requested, 2026-08-10).

Real, credential-backed tools:
  * email_list / email_read — Gmail IMAP inbox read/triage (creds from
    ``~/.antigona/secrets/gmail_creds.txt``, same source as ``email_sender``).

Graceful-fallback tools (registered always; return a clear "not configured"
error until the required key is provided):
  * google_drive_list, slack_post, notion_query, firecrawl_scrape, serpapi_search.
  Key lookup order: env var -> ``~/.antigona/secrets/<name>.json``. This satisfies
  the "per-module graceful-fallback" rule: registering the tool never breaks a
  runtime that lacks the key — it simply reports the missing config.

Everything composes with the single Antigona core (AntigonaBrain :8090) through
``register()``, wired once from ``tools/registry.py::register_builtins``.
"""

from __future__ import annotations

import email as email_mod
import imaplib
import json
import os
import re
from collections.abc import Callable
from typing import Any

from antigona.core import paths

SECRETS_DIR = paths.secrets_dir()

_BUILTIN_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Фильтр/запрос"},
        "limit": {"type": "integer", "description": "Максимум результатов"},
    },
}


# ── Credential helpers ──────────────────────────────────────────────────


def _load_gmail_creds() -> tuple[str, str]:
    from antigona.core.email_sender import _load_creds
    return _load_creds()


def _resolve_key(env_name: str, secret_file: str) -> str | None:
    val = os.environ.get(env_name)
    if val:
        return val
    path = SECRETS_DIR / secret_file
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for cand in ("api_key", "token", env_name):
                if data.get(cand):
                    return str(data[cand])
        except (OSError, ValueError):
            pass
    return None


def _not_configured(tool: str, env_name: str, secret_file: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": (
                f"{tool} not configured: set {env_name} env var or "
                f"~/.antigona/secrets/{secret_file} with api_key/token"
            ),
        },
        ensure_ascii=False,
    )


def _http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
               payload: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))

def _http_text(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20) -> str:
    import urllib.request
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return str(resp.read().decode("utf-8", "ignore"))


def _clean_html(text: str) -> str:
    import html as html_mod
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_mod.unescape(re.sub(r"\s+", " ", text)).strip()


# ── Email (real, Gmail IMAP) ────────────────────────────────────────────


async def _handle_email_list(*, limit: int = 10, folder: str = "INBOX", **kwargs: Any) -> str:
    try:
        addr, pw = _load_gmail_creds()
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as m:
            m.login(addr, pw)
            m.select(folder)
            _status, data = m.search(None, "ALL")
            ids = (data[0] or b"").split()
            ids = ids[-max(1, min(int(limit or 10), 50)):]
            items: list[dict[str, Any]] = []
            for i in ids:
                st, msgdata = m.fetch(i.decode(), "(RFC822.HEADER)")
                if st == "OK" and msgdata and msgdata[0]:
                    mhead = msgdata[0]
                    mraw = mhead[1] if isinstance(mhead, (tuple, list)) and len(mhead) > 1 else b""
                    mraw = mraw if isinstance(mraw, bytes) else (mraw.encode() if isinstance(mraw, str) else b"")
                    msg = email_mod.message_from_bytes(mraw)
                    items.append({
                        "id": i.decode(),
                        "subject": str(msg.get("Subject", ""))[:120],
                        "from": str(msg.get("From", ""))[:120],
                        "date": str(msg.get("Date", ""))[:60],
                    })
            m.logout()
        return json.dumps({"success": True, "count": len(items), "items": items}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"email list failed: {exc}"}, ensure_ascii=False)


async def _handle_email_read(*, message_id: str = "", folder: str = "INBOX", **kwargs: Any) -> str:
    try:
        addr, pw = _load_gmail_creds()
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as m:
            m.login(addr, pw)
            m.select(folder)
            st, data = m.fetch(message_id, "(RFC822)")
            m.logout()
        if st != "OK" or not data or not data[0]:
            return json.dumps({"success": False, "error": "message not found"})
        head = data[0]
        raw = head[1] if isinstance(head, (tuple, list)) and len(head) > 1 else b""
        raw = raw if isinstance(raw, bytes) else (raw.encode() if isinstance(raw, str) else b"")
        msg = email_mod.message_from_bytes(raw)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    praw = part.get_payload(decode=True)
                    body = praw.decode("utf-8", "ignore") if isinstance(praw, bytes) else str(praw or "")
                    break
        else:
            praw = msg.get_payload(decode=True)
            body = praw.decode("utf-8", "ignore") if isinstance(praw, bytes) else str(praw or "")
        return json.dumps({
            "success": True,
            "id": message_id,
            "subject": str(msg.get("Subject", "")),
            "from": str(msg.get("From", "")),
            "date": str(msg.get("Date", "")),
            "body": body[:4000],
        }, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"email read failed: {exc}"}, ensure_ascii=False)


# ── Graceful-fallback external tools ────────────────────────────────────


async def _handle_google_drive_list(*, query: str = "", limit: int = 10, **kwargs: Any) -> str:
    key = _resolve_key("GOOGLE_API_KEY", "google_drive.json")
    if not key:
        return _not_configured("google_drive_list", "GOOGLE_API_KEY", "google_drive.json")
    return json.dumps({"success": False, "error": "google_drive_list requires OAuth (service account) — configure before use"})


async def _handle_slack_post(*, channel: str = "", text: str = "", **kwargs: Any) -> str:
    token = _resolve_key("SLACK_TOKEN", "slack.json")
    if not token:
        return _not_configured("slack_post", "SLACK_TOKEN", "slack.json")
    if not channel or not text:
        return json.dumps({"success": False, "error": "slack_post requires channel and text"})
    try:
        res = _http_json("https://slack.com/api/chat.postMessage", method="POST",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         payload={"channel": channel, "text": text})
        return json.dumps({"success": bool(res.get("ok")), "error": res.get("error"), "ts": res.get("ts")})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"slack_post failed: {exc}"})


async def _handle_notion_query(*, database_id: str = "", query: str = "", limit: int = 10, **kwargs: Any) -> str:
    token = _resolve_key("NOTION_API_KEY", "notion.json")
    if not token:
        return _not_configured("notion_query", "NOTION_API_KEY", "notion.json")
    if not database_id:
        return json.dumps({"success": False, "error": "notion_query requires database_id"})
    try:
        payload = {"query": query or ""} if query else {}
        res = _http_json(f"https://api.notion.com/v1/databases/{database_id}/query",
                         method="POST",
                         headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
                                  "Content-Type": "application/json"},
                         payload=payload or None)
        return json.dumps({"success": True, "count": len(res.get("results", [])),
                           "results": [{"id": r.get("id")} for r in res.get("results", [])][:int(limit or 10)]}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"notion_query failed: {exc}"})


async def _handle_firecrawl_scrape(*, url: str = "", **kwargs: Any) -> str:
    key = _resolve_key("FIRECRAWL_API_KEY", "firecrawl.json")
    if not key:
        return _not_configured("firecrawl_scrape", "FIRECRAWL_API_KEY", "firecrawl.json")
    if not url:
        return json.dumps({"success": False, "error": "firecrawl_scrape requires url"})
    try:
        res = _http_json("https://api.firecrawl.dev/v1/scrape", method="POST",
                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                         payload={"url": url})
        return json.dumps({"success": True, "data": res.get("data", {})}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"firecrawl_scrape failed: {exc}"})


async def _handle_serpapi_search(*, query: str = "", **kwargs: Any) -> str:
    key = _resolve_key("SERPAPI_API_KEY", "serpapi.json")
    if not key:
        return _not_configured("serpapi_search", "SERPAPI_API_KEY", "serpapi.json")
    if not query:
        return json.dumps({"success": False, "error": "serpapi_search requires query"})
    try:
        from urllib.parse import urlencode
        res = _http_json(f"https://serpapi.com/search.json?{urlencode({'q': query, 'engine': 'google'})}",
                         headers={"X-API-Key": key})
        return json.dumps({"success": True, "organic_results": len(res.get("organic_results", [])),
                           "top": [r.get("title") for r in res.get("organic_results", [])[:5]]}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"serpapi_search failed: {exc}"})



# ── Web search / fetch (real, no API key — DuckDuckGo Lite) ──────────────


def _decode_ddg_url(href: str) -> str:
    """Extract the real URL from a DDG redirect href (uddg=param)."""
    import urllib.parse
    if "uddg=" in href:
        qs = urllib.parse.urlsplit(href).query
        params = urllib.parse.parse_qs(qs)
        if params.get("uddg"):
            return params["uddg"][0]
    return href


async def _handle_web_search(*, query: str = "", max_results: int = 5, **kwargs: Any) -> str:
    """Search the web (DuckDuckGo Lite, no API key). Returns titles+urls+snippets."""
    import urllib.parse
    if not query.strip():
        return json.dumps({"success": False, "error": "web_search requires query"}, ensure_ascii=False)
    try:
        url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
        html = _http_text(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        # match anchors by the result-link class (href before class in DDG Lite)
        all_anchors = re.findall(r"<a[^>]*>.*?</a>", html, re.S)
        blocks = [a for a in all_anchors if "result-link" in a]
        snippets = re.findall(r"<td class=[^>]*result-snippet[^>]*>(.*?)</td>", html, re.S)
        items = []
        for idx, blk in enumerate(blocks[: max(1, min(int(max_results or 5), 20))]):
            href = re.search(r"href=[\"']([^\"\']+)[\"']", blk)
            title = re.sub(r"<[^>]+>", " ", blk)
            items.append({
                "title": _clean_html(title),
                "url": _decode_ddg_url(href.group(1)) if href else "",
                "snippet": _clean_html(snippets[idx]) if idx < len(snippets) else "",
            })
        return json.dumps({"success": True, "query": query, "count": len(items), "items": items}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": "web_search failed: " + str(exc)}, ensure_ascii=False)



async def _handle_web_fetch(*, url: str = "", max_chars: int = 4000, **kwargs: Any) -> str:
    """Fetch a web page and return its readable text (HTML tags stripped)."""
    if not url.strip():
        return json.dumps({"success": False, "error": "web_fetch requires url"}, ensure_ascii=False)
    try:
        html = _http_text(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        text = _clean_html(html)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return json.dumps({"success": True, "url": url, "text": text[: max(200, min(int(max_chars or 4000), 20000))]}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"web_fetch failed: {exc}"}, ensure_ascii=False)


_TOOLS: list[tuple[str, str, dict[str, Any], Callable[..., Any]]] = [
    ("email_list", "integration",
     {"type": "object", "properties": {"limit": {"type": "integer", "description": "Сколько писем"}, "folder": {"type": "string", "description": "Папка (INBOX)"}},
      "required": []}, _handle_email_list),
    ("email_read", "integration",
     {"type": "object", "properties": {"message_id": {"type": "string", "description": "ID письма из email_list"}, "folder": {"type": "string"}},
      "required": ["message_id"]}, _handle_email_read),
    ("google_drive_list", "integration", dict(_BUILTIN_SCHEMA), _handle_google_drive_list),
    ("slack_post", "integration",
     {"type": "object", "properties": {"channel": {"type": "string", "description": "Канал (например #general)"}, "text": {"type": "string", "description": "Текст сообщения"}},
      "required": ["channel", "text"]}, _handle_slack_post),
    ("notion_query", "integration",
     {"type": "object", "properties": {"database_id": {"type": "string", "description": "ID базы Notion"}, "query": {"type": "string"}},
      "required": ["database_id"]}, _handle_notion_query),
    ("firecrawl_scrape", "integration",
     {"type": "object", "properties": {"url": {"type": "string", "description": "URL для скрапинга"}},
      "required": ["url"]}, _handle_firecrawl_scrape),
    ("serpapi_search", "integration",
     {"type": "object", "properties": {"query": {"type": "string", "description": "Поисковый запрос"}},
      "required": ["query"]}, _handle_serpapi_search),
    ("web_search", "web",
     {"type": "object", "properties": {"query": {"type": "string", "description": "Поисковый запрос"}, "max_results": {"type": "integer", "description": "Сколько результатов"}},
      "required": ["query"]}, _handle_web_search),
    ("web_fetch", "web",
     {"type": "object", "properties": {"url": {"type": "string", "description": "URL страницы"}, "max_chars": {"type": "integer", "description": "Сколько текста вернуть"}},
      "required": ["url"]}, _handle_web_fetch),
]


def register(registry: Any) -> None:
    """Register all integration tools on the given ToolRegistry."""
    for name, toolset, schema, handler in _TOOLS:
        registry.register(name, toolset=toolset, schema=schema, handler=handler, replace=True)
    from antigona.tools import ollama_tool

    ollama_tool.register(registry)
