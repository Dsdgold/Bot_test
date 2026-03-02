"""Scraper for sig.pl - building materials store."""

import logging
from urllib.parse import quote_plus

from backend.scrapers.base import BaseScraper
from backend.utils.anti_detect import human_delay, human_scroll, human_mouse_move

logger = logging.getLogger(__name__)


class SigScraper(BaseScraper):
    name = "sig.pl"
    base_url = "https://www.sig.pl"

    async def search(self, query: str, engine) -> list[dict]:
        products = []
        search_url = f"{self.base_url}/search?q={quote_plus(query)}"

        try:
            async with engine.new_page_with_images() as page:
                logger.info(f"[SIG] Navigating to {search_url}")

                # First visit the homepage to get cookies
                await page.goto(self.base_url, wait_until="domcontentloaded")
                await human_delay(page, 1000, 2000)

                # Handle cookie consent if present
                try:
                    cookie_btn = page.locator(
                        "button:has-text('Akceptuję'), "
                        "button:has-text('Zgadzam'), "
                        "button:has-text('Zaakceptuj'), "
                        "button:has-text('Accept'), "
                        "[id*='cookie'] button, "
                        "[class*='cookie'] button, "
                        "[class*='consent'] button"
                    ).first
                    if await cookie_btn.is_visible(timeout=3000):
                        await cookie_btn.click()
                        await human_delay(page, 500, 1000)
                except Exception:
                    pass

                await human_mouse_move(page)

                # Navigate to search
                await page.goto(search_url, wait_until="domcontentloaded")
                await human_delay(page, 2000, 4000)
                await human_scroll(page)

                # Wait for product list to load
                await page.wait_for_load_state("networkidle", timeout=15000)

                # Try different selectors for product cards
                selectors = [
                    "[class*='product-card']",
                    "[class*='product-item']",
                    "[class*='product-tile']",
                    "[class*='ProductCard']",
                    "[data-product]",
                    ".product",
                    "[class*='search-result'] [class*='item']",
                    "article[class*='product']",
                    "[class*='listing'] [class*='item']",
                ]

                product_elements = []
                used_selector = ""
                for sel in selectors:
                    elements = await page.locator(sel).all()
                    if elements:
                        product_elements = elements
                        used_selector = sel
                        break

                if not product_elements:
                    # Try to extract from page content directly
                    logger.info("[SIG] No product cards found with known selectors, trying generic extraction")
                    content = await page.content()
                    return await self._extract_from_html(content, query)

                logger.info(f"[SIG] Found {len(product_elements)} products with selector '{used_selector}'")

                for elem in product_elements[:30]:
                    try:
                        product = await self._extract_product(elem, page)
                        if product and product.get("nazwa"):
                            products.append(product)
                    except Exception as e:
                        logger.debug(f"[SIG] Error extracting product: {e}")
                        continue

                # Scroll down and check for more products
                for _ in range(3):
                    await human_scroll(page)
                    await human_delay(page, 500, 1000)

        except Exception as e:
            logger.error(f"[SIG] Scraping error: {e}")

        logger.info(f"[SIG] Scraped {len(products)} products")
        return products

    async def _extract_product(self, elem, page) -> dict | None:
        """Extract product data from a card element."""
        try:
            # Name
            name = ""
            for sel in ["a[class*='name']", "h2", "h3", "a[class*='title']",
                        "[class*='name']", "[class*='title']", "a"]:
                try:
                    name_el = elem.locator(sel).first
                    if await name_el.is_visible(timeout=500):
                        name = (await name_el.inner_text()).strip()
                        if name:
                            break
                except Exception:
                    continue

            if not name:
                return None

            # URL
            url = ""
            try:
                link = elem.locator("a").first
                url = await link.get_attribute("href") or ""
                if url and not url.startswith("http"):
                    url = self.base_url + url
            except Exception:
                pass

            # Price
            price = ""
            for sel in ["[class*='price']", "[class*='Price']", "span[class*='amount']",
                        "[data-price]"]:
                try:
                    price_el = elem.locator(sel).first
                    if await price_el.is_visible(timeout=500):
                        price = (await price_el.inner_text()).strip()
                        if price:
                            break
                except Exception:
                    continue

            # Image
            image = ""
            try:
                img = elem.locator("img").first
                image = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
                if image and not image.startswith("http"):
                    image = self.base_url + image
            except Exception:
                pass

            # Availability
            availability = ""
            try:
                avail_el = elem.locator("[class*='avail'], [class*='stock']").first
                if await avail_el.is_visible(timeout=500):
                    availability = (await avail_el.inner_text()).strip()
            except Exception:
                pass

            return self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=image,
                dostepnosc=availability,
            )
        except Exception as e:
            logger.debug(f"[SIG] Product extraction error: {e}")
            return None

    async def _extract_from_html(self, html: str, query: str) -> list[dict]:
        """Fallback: extract products from raw HTML using BeautifulSoup."""
        from bs4 import BeautifulSoup

        products = []
        soup = BeautifulSoup(html, "lxml")

        # Look for any links that might be products
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"]
            if query.split()[0].lower() in text.lower() and len(text) > 10:
                url = href if href.startswith("http") else self.base_url + href

                # Try to find a price nearby
                parent = a.find_parent()
                price = ""
                if parent:
                    price_el = parent.find(class_=lambda c: c and "price" in c.lower()) if parent else None
                    if price_el:
                        price = price_el.get_text(strip=True)

                img = ""
                img_el = parent.find("img") if parent else None
                if img_el:
                    img = img_el.get("src", "") or img_el.get("data-src", "")

                products.append(self._build_product(
                    nazwa=text[:200],
                    cena=self._normalize_price(price),
                    zrodlo=self.name,
                    url=url,
                    zdjecie=img,
                ))

        return products[:30]
