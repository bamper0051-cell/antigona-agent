"""Web Search — DuckDuckGo search and page extraction for Antigona.

Provides:
    search(query, limit) — search via DuckDuckGo (free, no API key).
    extract(url) — fetch and extract readable page content.
    WebSearchTool — class-based interface with DDGS.

Integration:
    ActionType.WEB_SEARCH
    /search command handler
    ContextBuilder integration: agent can respond with search results.

Usage:
    searcher = WebSearchTool()
    results = searcher.search("python async programming")
    content = searcher.extract("https://example.com")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_SEARCH_LIMIT: int = 5
_MAX_SEARCH_LIMIT: int = 20
_EXTRACT_TIMEOUT: int = 15


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class SearchResult:
    """A single search result."""

    title: str = ""
    url: str = ""
    description: str = ""
    snippet: str = ""


@dataclass
class ExtractResult:
    """Result from extracting a web page."""

    url: str = ""
    title: str = ""
    content: str = ""
    success: bool = True
    error: str = ""


# ─── WebSearchTool ───────────────────────────────────────────────────────────


class WebSearchTool:
    """Search the web via DuckDuckGo (free, no API key) and extract pages.

    Uses the ``duckduckgo_search`` library for search and ``httpx`` for
    page extraction.

    Attributes:
        search_limit: Default number of search results to return.
        extract_timeout: HTTP timeout for page extraction (seconds).
    """

    def __init__(
        self,
        search_limit: int = _DEFAULT_SEARCH_LIMIT,
        extract_timeout: int = _EXTRACT_TIMEOUT,
    ) -> None:
        self.search_limit = min(search_limit, _MAX_SEARCH_LIMIT)
        self.extract_timeout = extract_timeout

    def search(
        self,
        query: str,
        limit: int | None = None,
        region: str = "ru-ru",
    ) -> list[SearchResult]:
        """Search the web via DuckDuckGo.

        Args:
            query: The search query.
            limit: Max results (default: self.search_limit).
            region: Region for results (default: ru-ru).

        Returns:
            List of SearchResult namedtuples.

        Raises:
            RuntimeError: If DuckDuckGo search fails.
        """
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.error("duckduckgo_search not installed. Run: pip install duckduckgo_search")
            raise RuntimeError(
                "duckduckgo_search library not available. "
                "Install with: pip install duckduckgo_search"
            ) from None

        actual_limit = limit or self.search_limit
        results: list[SearchResult] = []

        try:
            with DDGS() as ddgs:
                for i, r in enumerate(ddgs.text(query, region=region)):
                    if i >= actual_limit:
                        break
                    title = r.get("title", "")
                    url = r.get("href", "")
                    desc = r.get("body", "")
                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            description=desc,
                            snippet=desc[:200] if desc else "",
                        )
                    )
        except Exception as exc:
            logger.warning("DuckDuckGo search failed: %s", exc)
            raise RuntimeError(f"DuckDuckGo search error: {exc}") from exc

        return results

    def extract(self, url: str) -> ExtractResult:
        """Fetch and extract readable content from a URL.

        Args:
            url: The web page URL.

        Returns:
            ExtractResult with page content (truncated to ~5000 chars).
        """
        import httpx

        try:
            with httpx.Client(
                timeout=self.extract_timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            ) as client:
                response = client.get(url)

            if response.status_code != 200:
                return ExtractResult(
                    url=url,
                    success=False,
                    error=f"HTTP {response.status_code}: {response.reason_phrase}",
                )

            content = response.text

            # Try to extract title
            title = ""
            import re

            title_match = re.search(
                r"<title[^>]*>(.*?)</title>",
                content,
                re.IGNORECASE | re.DOTALL,
            )
            if title_match:
                title = title_match.group(1).strip()

            # Strip HTML tags for plain text
            plain = re.sub(r"<[^>]+>", " ", content)
            plain = re.sub(r"\s+", " ", plain).strip()

            # Truncate
            max_chars = 5000
            truncated = plain[:max_chars]
            if len(plain) > max_chars:
                truncated += "\n\n...[content truncated]"

            return ExtractResult(url=url, title=title, content=truncated)

        except httpx.TimeoutException:
            return ExtractResult(
                url=url, success=False, error="Request timed out"
            )
        except httpx.RequestError as exc:
            return ExtractResult(
                url=url, success=False, error=f"Request error: {exc}"
            )
        except Exception as exc:
            return ExtractResult(
                url=url, success=False, error=str(exc)
            )

    def search_and_format(
        self,
        query: str,
        limit: int | None = None,
    ) -> str:
        """Search and return results as a formatted text block.

        Args:
            query: The search query.
            limit: Max results.

        Returns:
            Formatted text with numbered results.
        """
        try:
            results = self.search(query, limit=limit)
        except RuntimeError as exc:
            return f"❌ Search error: {exc}"

        if not results:
            return f"🔍 По запросу «{query}» ничего не найдено."

        lines: list[str] = [f"🔍 Результаты поиска по «{query}»:", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.title}")
            lines.append(f"   {r.url}")
            if r.description:
                desc = r.description[:150]
                if len(r.description) > 150:
                    desc += "…"
                lines.append(f"   {desc}")
            lines.append("")

        return "\n".join(lines)


# ─── Convenience ────────────────────────────────────────────────────────────


def search(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> list[SearchResult]:
    """One-shot web search via DuckDuckGo.

    Args:
        query: The search query.
        limit: Max results.

    Returns:
        List of SearchResult.
    """
    tool = WebSearchTool(search_limit=limit)
    return tool.search(query, limit=limit)


def search_and_format(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """One-shot web search with formatted output.

    Args:
        query: The search query.
        limit: Max results.

    Returns:
        Formatted text with numbered results.
    """
    tool = WebSearchTool(search_limit=limit)
    return tool.search_and_format(query, limit=limit)


def extract(url: str) -> ExtractResult:
    """One-shot page extraction.

    Args:
        url: The web page URL.

    Returns:
        ExtractResult with page content.
    """
    tool = WebSearchTool()
    return tool.extract(url)
