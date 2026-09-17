"""Browser package — headless browser automation via Playwright.

Provides:
- ``BrowserManager`` — singleton managing a single Chromium instance
- ``/browser navigate <url>`` — open a page
- ``/browser snapshot`` — show a text snapshot of the current page
- ``/browser click <selector>`` — click an element by CSS selector
"""

from antigona.browser.browser import BrowserManager, get_browser_manager

__all__ = [
    "BrowserManager",
    "get_browser_manager",
]
