"""Core scraping engine using Playwright with anti-detection."""

import asyncio
import logging
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, BrowserContext

from backend.config import BROWSER_ARGS, REQUEST_TIMEOUT, NAV_TIMEOUT
from backend.utils.anti_detect import (
    STEALTH_JS, get_random_viewport, get_random_user_agent,
)

logger = logging.getLogger(__name__)


class ScraperEngine:
    """Manages browser instances with anti-detection measures."""

    _instance = None
    _browser: Browser | None = None
    _playwright = None

    @classmethod
    async def get_instance(cls) -> "ScraperEngine":
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._init_browser()
        return cls._instance

    async def _init_browser(self):
        logger.info("Starting browser engine...")
        self._playwright = await async_playwright().start()
        try:
            self._browser = await asyncio.wait_for(
                self._playwright.chromium.launch(
                    headless=True,
                    args=BROWSER_ARGS,
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            logger.error("Browser launch timed out after 30s, retrying...")
            # Kill any stale chromium processes and retry
            import subprocess
            subprocess.run(["pkill", "-f", "chromium"], capture_output=True)
            await asyncio.sleep(2)
            self._browser = await asyncio.wait_for(
                self._playwright.chromium.launch(
                    headless=True,
                    args=BROWSER_ARGS,
                ),
                timeout=30,
            )
        logger.info("Browser engine started")

    async def shutdown(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        ScraperEngine._instance = None
        logger.info("Browser engine stopped")

    @asynccontextmanager
    async def new_context(self):
        """Create a new browser context with anti-detection."""
        viewport = get_random_viewport()
        ua = get_random_user_agent()

        context = await self._browser.new_context(
            viewport=viewport,
            user_agent=ua,
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            geolocation={"latitude": 52.2297, "longitude": 21.0122},
            permissions=["geolocation"],
            java_script_enabled=True,
            bypass_csp=True,
            extra_http_headers={
                "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        )
        context.set_default_timeout(REQUEST_TIMEOUT)
        context.set_default_navigation_timeout(NAV_TIMEOUT)

        # Inject stealth script into every new page
        await context.add_init_script(STEALTH_JS)

        try:
            yield context
        finally:
            await context.close()

    @asynccontextmanager
    async def new_page(self):
        """Create a new stealth page."""
        async with self.new_context() as context:
            page = await context.new_page()

            # Block unnecessary resources for speed
            await page.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                lambda route: route.abort(),
            )

            try:
                yield page
            finally:
                await page.close()

    @asynccontextmanager
    async def new_page_with_images(self):
        """Create a new stealth page that loads images (for getting image URLs)."""
        async with self.new_context() as context:
            page = await context.new_page()

            # Only block fonts
            await page.route(
                "**/*.{woff,woff2,ttf,eot}",
                lambda route: route.abort(),
            )

            try:
                yield page
            finally:
                await page.close()
