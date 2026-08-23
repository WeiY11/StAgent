"""Web automation tools package.

This module provides Playwright-based tools for web automation including
scraping, form filling, clicking, screenshots, and authentication.

Example:
    >>> from StAgent.tools.web import (
    ...     BrowserPool,
    ...     WebScraperTool,
    ...     WebFormFillTool,
    ...     WebClickTool,
    ...     WebScreenshotTool,
    ...     WebLoginTool,
    ... )
    >>>
    >>> # Get browser pool instance
    >>> pool = await BrowserPool.get_instance()
    >>>
    >>> # Use tools
    >>> scraper = WebScraperTool()
    >>> result = await scraper.run({"url": "https://example.com"}, context)
"""

from StAgent.tools.web.browser_pool import (
    BrowserConfig,
    BrowserPool,
    cleanup_browser_pool,
    get_browser_pool,
)
from StAgent.tools.web.click import WebClickTool
from StAgent.tools.web.form_fill import WebFormFillTool
from StAgent.tools.web.login import WebLoginTool
from StAgent.tools.web.scraper import WebScraperTool
from StAgent.tools.web.screenshot import WebScreenshotTool

__all__ = [
    # Browser pool management
    "BrowserConfig",
    "BrowserPool",
    "get_browser_pool",
    "cleanup_browser_pool",
    # Web automation tools
    "WebScraperTool",
    "WebFormFillTool",
    "WebClickTool",
    "WebScreenshotTool",
    "WebLoginTool",
]
