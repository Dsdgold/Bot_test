"""Scraper for sig.pl - building materials store."""

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
        products = []
        search_url = f"{self.base_url}/szukaj-produktow?searchquery={quote_plus(query)}"

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
                    "[data-productid]",
                    ".product",
                    "[class*='search-result'] [class*='item']",
                    "article[class*='product']",
                    "[class*='listing'] [class*='item']",
                    "[class*='productBox']",
                    "[class*='product-box']",
                    "[class*='prod-']",
                ]

                product_elements = []
                used_selector = ""
                for sel in selectors:
                    elements = await page.locator(sel).all()
                    if elements:
                        product_elements = elements
                        used_selector = sel
                        break

                if product_elements:
                    logger.info(f"[SIG] Found {len(product_elements)} products with selector '{used_selector}'")
                    for elem in product_elements[:30]:
                        try:
                            product = await self._extract_product(elem, page)
                            if product and product.get("nazwa"):
                                products.append(product)
                        except Exception as e:
                            logger.debug(f"[SIG] Error extracting product: {e}")
                            continue

                if not products:
                    # Fallback: extract product links from HTML by URL pattern
                    logger.info("[SIG] No products via selectors, trying HTML extraction")
                    content = await page.content()
                    products = await self._extract_from_html(content, query)

                if not products:
                    # Last resort: look for any links matching SIG product URL pattern
                    logger.info("[SIG] Trying product URL pattern matching")
                    all_links = await page.locator("a[href]").all()
                    for link in all_links[:100]:
                        try:
                            href = await link.get_attribute("href") or ""
                            if not SIG_PRODUCT_URL_RE.search(href):
                                continue
                            text = (await link.inner_text()).strip()
                            if not text or len(text) < 5:
                                continue
                            url = href if href.startswith("http") else self.base_url + href

                            # Try to find price near the link
                            parent = link.locator("..")
                            price = ""
                            try:
                                price_el = parent.locator(
                                    "[class*='price'], [class*='cena'], [class*='Price']"
                                ).first
                                if await price_el.is_visible(timeout=300):
                                    price = (await price_el.inner_text()).strip()
                            except Exception:
                                pass

                            # Try to find image near the link
                            image = ""
                            try:
                                img = parent.locator("img").first
                                image = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
                                if image and not image.startswith("http"):
                                    image = self.base_url + image
                            except Exception:
                                pass

                            products.append(self._build_product(
                                nazwa=text[:200],
                                cena=self._normalize_price(price),
                                zrodlo=self.name,
                                url=url,
                                zdjecie=image,
                            ))
                        except Exception:
                            continue

                # Scroll down and check for more products
                for _ in range(3):
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
            elif not p["url"]:
                unique.append(p)
        products = unique[:30]

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
            for sel in ["[class*='price']", "[class*='Price']", "[class*='cena']",
                        "span[class*='amount']", "[data-price]"]:
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
                avail_el = elem.locator("[class*='avail'], [class*='stock'], [class*='dostep']").first
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

        # Strategy 1: Find product links by SIG URL pattern (,p123456)
        for a in soup.find_all("a", href=SIG_PRODUCT_URL_RE):
            text = a.get_text(strip=True)
            href = a["href"]
            if not text or len(text) < 5:
                continue

            url = href if href.startswith("http") else self.base_url + href

            # Try to find a price nearby
            parent = a.find_parent()
            price = ""
            if parent:
                # Go up a few levels to find price
                for _ in range(4):
                    price_el = parent.find(
                        class_=lambda c: c and any(
                            x in c.lower() for x in ["price", "cena"]
                        )
                    )
                    if price_el:
                        price = price_el.get_text(strip=True)
                        break
                    parent = parent.find_parent()
                    if not parent:
                        break

            img = ""
            # Go back to the original parent
            parent = a.find_parent()
            if parent:
                for _ in range(4):
                    img_el = parent.find("img")
                    if img_el:
                        img = img_el.get("src", "") or img_el.get("data-src", "")
                        if img and not img.startswith("http"):
                            img = self.base_url + img
                        break
                    parent = parent.find_parent()
                    if not parent:
                        break

            products.append(self._build_product(
                nazwa=text[:200],
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=img,
            ))

        # Strategy 2: If no products by URL pattern, try query-based matching
        if not products:
            query_words = [w.lower() for w in query.split() if len(w) > 2]
            for a in soup.find_all("a", href=True):
                text = a.get_text(strip=True)
                href = a["href"]
                if len(text) < 10:
                    continue
                text_lower = text.lower()
                # At least 2 query words must match
                matches = sum(1 for w in query_words if w in text_lower)
                if matches < min(2, len(query_words)):
                    continue

                url = href if href.startswith("http") else self.base_url + href

                parent = a.find_parent()
                price = ""
                if parent:
                    price_el = parent.find(
                        class_=lambda c: c and any(
                            x in c.lower() for x in ["price", "cena"]
                        )
                    )
                    if price_el:
                        price = price_el.get_text(strip=True)

                img = ""
                if parent:
                    img_el = parent.find("img")
                    if img_el:
                        img = img_el.get("src", "") or img_el.get("data-src", "")

                products.append(self._build_product(
                    nazwa=text[:200],
                    cena=self._normalize_price(price),
                    zrodlo=self.name,
                    url=url,
                    zdjecie=img,
                ))

        # Deduplicate
        seen = set()
        unique = []
        for p in products:
            key = p["url"] or p["nazwa"]
            if key not in seen:
                seen.add(key)
                unique.append(p)

        return unique[:30]
