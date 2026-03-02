"""Scraper for sig.pl - building materials store.

Uses Google site-search to find SIG products because sig.pl is
protected by Cloudflare which blocks headless browsers.
"""

import json
import logging
import re
from urllib.parse import quote_plus

from backend.scrapers.base import BaseScraper
from backend.utils.anti_detect import human_delay, human_scroll, human_mouse_move

logger = logging.getLogger(__name__)

# Pattern for SIG product URLs: /some-product-slug,p123456
SIG_PRODUCT_URL_RE = re.compile(r",p\d{4,}")


class SigScraper(BaseScraper):
    name = "sig.pl"
    base_url = "https://www.sig.pl"

    async def search(self, query: str, engine) -> list[dict]:
        """Search SIG products via Google site-search to bypass Cloudflare."""
        products = []

        # Use Google to search sig.pl products
        google_query = f"site:sig.pl {query}"
        google_url = (
            f"https://www.google.pl/search?"
            f"q={quote_plus(google_query)}&hl=pl&gl=pl&num=30"
        )

        try:
            async with engine.new_page_with_images() as page:
                logger.info(f"[SIG] Searching via Google: {google_query}")

                # Go to Google first
                await page.goto("https://www.google.pl", wait_until="domcontentloaded")
                await human_delay(page, 800, 1500)

                # Handle Google cookie consent
                try:
                    consent = page.locator(
                        "button:has-text('Zaakceptuj wszystko'), "
                        "button:has-text('Accept all'), "
                        "#L2AGLb"
                    ).first
                    if await consent.is_visible(timeout=3000):
                        await consent.click()
                        await human_delay(page, 500, 1000)
                except Exception:
                    pass

                await human_mouse_move(page)

                # Navigate to Google search results
                await page.goto(google_url, wait_until="domcontentloaded")
                await human_delay(page, 2000, 3500)
                await human_scroll(page)

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                current_url = page.url
                logger.info(f"[SIG] Google results page: {current_url}")

                # Extract Google search results
                # Try multiple selectors for Google result items
                result_selectors = [
                    "#search .g",
                    "#rso .g",
                    "[data-hveid] .g",
                    "#search [data-sokoban-container]",
                    ".MjjYud",
                ]

                result_elements = []
                for sel in result_selectors:
                    elements = await page.locator(sel).all()
                    if elements:
                        result_elements = elements
                        logger.info(f"[SIG] Found {len(elements)} Google results with '{sel}'")
                        break

                if not result_elements:
                    # Fallback: extract from HTML
                    logger.info("[SIG] No results via selectors, trying HTML extraction")
                    content = await page.content()
                    products = self._extract_from_google_html(content)
                else:
                    for elem in result_elements[:30]:
                        try:
                            product = await self._extract_google_result(elem)
                            if product:
                                products.append(product)
                        except Exception as e:
                            logger.debug(f"[SIG] Error extracting result: {e}")
                            continue

                # Scroll for more results
                for _ in range(2):
                    await human_scroll(page)
                    await human_delay(page, 500, 1000)

        except Exception as e:
            logger.error(f"[SIG] Scraping error: {e}")

        # Deduplicate by URL
        seen_urls = set()
        unique = []
        for p in products:
            if p["url"] and p["url"] not in seen_urls:
                seen_urls.add(p["url"])
                unique.append(p)
        products = unique[:30]

        logger.info(f"[SIG] Scraped {len(products)} products total")
        return products

    async def _extract_google_result(self, elem) -> dict | None:
        """Extract product info from a Google search result."""
        try:
            # Get the link
            link = elem.locator("a").first
            href = await link.get_attribute("href") or ""

            # Only keep sig.pl product links
            if "sig.pl" not in href:
                return None

            # Get title
            title = ""
            for sel in ["h3", "h2", "[class*='title']"]:
                try:
                    title_el = elem.locator(sel).first
                    if await title_el.is_visible(timeout=500):
                        title = (await title_el.inner_text()).strip()
                        if title:
                            break
                except Exception:
                    continue

            if not title:
                return None

            # Clean up title - remove " | SIG" or similar suffixes
            title = re.sub(r'\s*[|–-]\s*SIG.*$', '', title).strip()
            title = re.sub(r'\s*-\s*sig\.pl.*$', '', title, flags=re.IGNORECASE).strip()

            # Get snippet text (may contain price or description)
            snippet = ""
            try:
                # Google snippet is usually in a div after the link
                snippet_el = elem.locator(
                    "[data-sncf], [class*='VwiC3b'], .IsZvec, [class*='snippet']"
                ).first
                if await snippet_el.is_visible(timeout=500):
                    snippet = (await snippet_el.inner_text()).strip()
            except Exception:
                pass

            # Try to extract price from snippet
            price = ""
            if snippet:
                price_match = re.search(
                    r'(\d[\d\s]*[.,]\d{2})\s*(?:zł|PLN|pln)',
                    snippet
                )
                if price_match:
                    price = price_match.group(1)

            url = href if href.startswith("http") else self.base_url + href

            return self._build_product(
                nazwa=title,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                dostepnosc=snippet[:150] if snippet else "",
            )

        except Exception as e:
            logger.debug(f"[SIG] Google result extraction error: {e}")
            return None

    def _extract_from_google_html(self, html: str) -> list[dict]:
        """Fallback: extract SIG results from Google HTML."""
        from bs4 import BeautifulSoup

        products = []
        soup = BeautifulSoup(html, "lxml")

        # Find all links pointing to sig.pl
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "sig.pl" not in href:
                continue

            # Look for h3 inside the link (Google result title)
            h3 = a.find("h3")
            if not h3:
                continue

            title = h3.get_text(strip=True)
            if not title:
                continue

            # Clean title
            title = re.sub(r'\s*[|–-]\s*SIG.*$', '', title).strip()
            title = re.sub(r'\s*-\s*sig\.pl.*$', '', title, flags=re.IGNORECASE).strip()

            url = href if href.startswith("http") else href

            # Try to find snippet (price info)
            price = ""
            parent = a.find_parent()
            if parent:
                parent = parent.find_parent()
            if parent:
                text = parent.get_text()
                price_match = re.search(
                    r'(\d[\d\s]*[.,]\d{2})\s*(?:zł|PLN|pln)',
                    text
                )
                if price_match:
                    price = price_match.group(1)

            products.append(self._build_product(
                nazwa=title[:200],
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
            ))

        return products[:30]
