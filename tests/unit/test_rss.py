"""Tests for antigona.core.rss."""

from __future__ import annotations

from antigona.core.rss import FeedItem, fetch_feed, translate_items


def _item(title="Hello", summary="World", link="http://x"):
    return FeedItem(title=title, link=link, summary=summary)


def test_fetch_empty_url():
    assert fetch_feed("") == []


def test_fetch_bad_url_no_raise():
    # must not raise on bad url / network fail
    assert fetch_feed("http://127.0.0.1:1/feed.xml", limit=3, timeout=1) == []


def test_translate_items_fields():
    items = [_item(title="Bonjour", summary="Monde")]
    out = translate_items(items, translate=lambda s: s.upper())
    assert out[0].title == "BONJOUR"
    assert out[0].summary == "MONDE"


def test_translate_skip_short():
    items = [_item(title="ab", summary="cdef")]
    # skip_short=3 => only summary (len>=3) translated, title left
    out = translate_items(items, translate=lambda s: s.upper(), skip_short=3)
    assert out[0].title == "ab"  # untouched
    assert out[0].summary == "CDEF"  # translated


def test_translate_empty_result_keeps_original():
    items = [_item(title="Keep")]
    out = translate_items(items, translate=lambda s: "")  # empty => keep
    assert out[0].title == "Keep"


def test_translate_raises_is_safe():
    def boom(s):
        raise RuntimeError("no llm")

    items = [_item(title="T", summary="S")]
    out = translate_items(items, translate=boom)
    assert out[0].title == "T"  # original kept, no crash
