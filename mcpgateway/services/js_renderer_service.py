# -*- coding: utf-8 -*-
"""JS Renderer Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

Optional Playwright-based JavaScript rendering for SPA documentation sites.
Gracefully degrades when Playwright is not installed.
"""

# Standard
from typing import Optional

# First-Party
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class JSRendererService:
    """Optional Playwright-based JS rendering for SPA documentation sites."""

    def __init__(self):
        """Initialize the JS renderer service (lazy Playwright init)."""
        self._playwright = None
        self._browser = None
        self._available: Optional[bool] = None

    async def initialize(self) -> bool:
        """Lazily initialize Playwright. Returns True if available."""
        if self._available is not None:
            return self._available

        try:
            # Third-Party
            from playwright.async_api import async_playwright  # pylint: disable=import-outside-toplevel

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._available = True
            logger.info("Playwright JS renderer initialized successfully")
        except ImportError:
            self._available = False
            logger.info("Playwright not installed - JS rendering disabled")
        except Exception as e:
            self._available = False
            logger.warning(f"Playwright initialization failed: {e}")

        return self._available

    async def render_page(self, url: str, timeout_ms: int = 30000) -> Optional[str]:
        """Render a page with headless Chromium and return the HTML.

        Args:
            url: URL to render.
            timeout_ms: Navigation timeout in milliseconds.

        Returns:
            Rendered HTML string or None on failure.
        """
        if not await self.initialize():
            return None

        try:
            page = await self._browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            html = await page.content()
            await page.close()
            return html
        except Exception as e:
            logger.warning(f"JS rendering failed for {url}: {e}")
            return None

    def detect_js_rendering_needed(self, html: str) -> bool:
        """Heuristic to detect if a page needs JS rendering.

        Checks for:
        - Body text < 500 chars but many <script> tags
        - SPA root elements (#root, #app, #__next) with no content
        - #swagger-ui or <redoc> with minimal text
        """
        # Third-Party
        from bs4 import BeautifulSoup  # pylint: disable=import-outside-toplevel

        soup = BeautifulSoup(html, "html.parser")

        # Count script tags
        scripts = soup.find_all("script")
        num_scripts = len(scripts)

        # Get body text length (excluding scripts)
        for s in soup(["script", "style"]):
            s.decompose()
        body = soup.find("body")
        body_text = body.get_text(strip=True) if body else ""

        # Heuristic 1: Many scripts but little text
        if num_scripts >= 5 and len(body_text) < 500:
            return True

        # Heuristic 2: SPA root elements with no content
        spa_roots = ["#root", "#app", "#__next", "#__nuxt", ".app-root"]
        for selector in spa_roots:
            el = soup.select_one(selector) if soup else None
            if el:
                el_text = el.get_text(strip=True)
                if len(el_text) < 100:
                    return True

        # Heuristic 3: Swagger UI or Redoc containers with minimal text
        swagger_el = soup.select_one("#swagger-ui")
        if swagger_el and len(swagger_el.get_text(strip=True)) < 200:
            return True

        redoc_el = soup.find("redoc")
        if redoc_el and len(body_text) < 500:
            return True

        return False

    async def close(self):
        """Clean up Playwright resources."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._available = None
