"""Headless browser automation via Playwright.

Manages a single persistent Chromium context. All navigation and interaction
happens in headless mode — no GUI, no display required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class BrowserManager:
    """Singleton that manages a single headless Chromium browser.

    Usage::

        mgr = get_browser_manager()
        await mgr.ensure_browser()
        await mgr.navigate("https://example.com")
        snapshot = await mgr.snapshot()
        await mgr.click("#my-button")
        await mgr.close()
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._context: Any = None

    async def ensure_browser(self) -> None:
        """Start browser if not already running."""
        if self._page is not None:
            return

        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            logger.info("Browser started (headless Chromium)")
        except Exception as exc:
            logger.error("Failed to start browser: %s", exc)
            self._page = None
            raise

    @property
    def is_running(self) -> bool:
        """Check if the browser is currently running."""
        return self._page is not None

    @property
    def current_url(self) -> str:
        """Get the current page URL, or empty string."""
        if self._page is None:
            return ""
        try:
            return str(self._page.url)
        except Exception:
            return ""

    async def navigate(self, url: str, timeout: float = 30.0) -> str:
        """Navigate to a URL and return the page title.

        Raises:
            RuntimeError: If browser not started.
            Exception: On navigation failure.
        """
        if self._page is None:
            raise RuntimeError("Browser not started. Call ensure_browser() first.")

        # Normalise URL
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            title = await self._page.title()
            logger.info("Navigated to %s — title: %s", url, title)
            return str(title)
        except Exception as exc:
            logger.warning("Navigation to %s failed: %s", url, exc)
            raise

    async def snapshot(self, max_chars: int = 3000) -> str:
        """Get a text representation of the current page.

        Returns page title + URL + visible text content (truncated).
        """
        if self._page is None:
            return "⚠️ Browser not started. Use /browser navigate <url> first."

        try:
            title = await self._page.title()
            url = self._page.url
            # Get visible text
            body_text = await self._page.evaluate("""
                () => {
                    const el = document.body;
                    if (!el) return '';
                    // Clone to avoid modifying live DOM
                    const clone = el.cloneNode(true);
                    // Remove scripts, styles, hidden elements
                    clone.querySelectorAll('script, style, svg, [hidden], [aria-hidden="true"]')
                        .forEach(e => e.remove());
                    return clone.innerText || clone.textContent || '';
                }
            """)
            body_text = (body_text or "").strip()
            if len(body_text) > max_chars:
                body_text = body_text[:max_chars] + "\n... (truncated)"

            lines = [
                f"🌐 <b>{title}</b>",
                f"📎 {url}",
                "",
                body_text,
            ]
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Snapshot failed: %s", exc)
            return f"⚠️ Snapshot error: {exc}"

    async def click(self, selector: str, timeout: float = 10.0) -> str:
        """Click an element identified by CSS selector.

        Returns a status message.
        """
        if self._page is None:
            raise RuntimeError("Browser not started. Use /browser navigate <url> first.")

        try:
            # Wait for the element to be visible and stable
            await self._page.wait_for_selector(
                selector, state="visible", timeout=int(timeout * 1000)
            )
            await self._page.click(selector)
            # Brief wait for any navigation/AJAX
            await asyncio.sleep(0.5)
            logger.info("Clicked element: %s", selector)
            return f"✅ Clicked <code>{selector}</code>"
        except Exception as exc:
            logger.warning("Click '%s' failed: %s", selector, exc)
            raise

    async def get_html(self) -> str:
        """Return the full page HTML (useful for debugging)."""
        if self._page is None:
            return ""
        return str(await self._page.content())

    async def close(self) -> None:
        """Close the browser and release resources."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning("Error closing browser: %s", exc)
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            logger.info("Browser closed")

    def status(self) -> str:
        """Return a human-readable status."""
        if self._page is None:
            return "⏹️ Browser stopped"
        url = self.current_url
        if url:
            return f"🌐 Browser active — <code>{url}</code>"
        return "🌐 Browser active (no page)"


# ── Module-level singleton ────────────────────────────────────────────────

_BROWSER_MANAGER: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    """Get the module-level singleton BrowserManager."""
    global _BROWSER_MANAGER
    if _BROWSER_MANAGER is None:
        _BROWSER_MANAGER = BrowserManager()
    return _BROWSER_MANAGER
