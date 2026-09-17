"""RSS feed fetching and translation (RSS Translator inspired).

Fetches RSS/Atom feeds with ``feedparser`` and optionally translates item
titles/summaries via an injected LLM callable. The translate step is decoupled
(any ``fn(text) -> str``), so it composes with Antigona's provider layer.

Usage::

    from antigona.core.rss import fetch_feed, translate_items

    items = fetch_feed("https://example.com/feed.xml", limit=5)
    translated = translate_items(items, translate=lambda s: provider.generate(...))
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["FeedItem", "fetch_feed", "translate_items"]


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "published": self.published,
            "source": self.source,
        }


def fetch_feed(url: str, limit: int = 10, timeout: int = 15) -> list[FeedItem]:
    """Fetch an RSS/Atom feed and return up to ``limit`` items.

    Returns an empty list on any fetch/parse error (never raises) so callers
    can degrade gracefully.
    """
    if not url:
        return []
    try:
        # Bounded network fetch: urllib honors the socket timeout (feedparser's
        # own timeout kwarg is dead and urlopen defaults to None = hang forever
        # on a stalled peer). Read with a real timeout, then parse in-memory.
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "antigona-rss/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()

        import feedparser

        parsed = feedparser.parse(raw)
        items: list[FeedItem] = []
        for entry in parsed.entries[:limit]:
            items.append(
                FeedItem(
                    title=getattr(entry, "title", "") or "",
                    link=getattr(entry, "link", "") or "",
                    summary=getattr(entry, "summary", "") or "",
                    published=getattr(entry, "published", "") or "",
                    source=url,
                )
            )
        return items
    except Exception:
        return []


def translate_items(
    items: list[FeedItem],
    translate: Callable[[str], str],
    fields: tuple[str, ...] = ("title", "summary"),
    skip_short: int = 0,
) -> list[FeedItem]:
    """Translate selected text fields of each item via ``translate``.

    ``translate`` receives one string and returns the translated string. Empty
    or very short inputs (``len < skip_short``) are left untouched.
    """
    out: list[FeedItem] = []
    for item in items:
        new = FeedItem(
            title=item.title,
            link=item.link,
            summary=item.summary,
            published=item.published,
            source=item.source,
        )
        if "title" in fields and len(item.title) >= skip_short:
            try:
                t = translate(item.title)
                if t:
                    new.title = t
            except Exception:
                pass
        if "summary" in fields and len(item.summary) >= skip_short:
            try:
                s = translate(item.summary)
                if s:
                    new.summary = s
            except Exception:
                pass
        out.append(new)
    return out
