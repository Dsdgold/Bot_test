"""Scraper for leroymerlin.pl - home improvement store."""

import logging
from urllib.parse import quote_plus

from backend.scrapers.base import BaseScraper
from backend.utils.anti_detect import human_delay, human_scroll, human_mouse_move

logger = logging.getLogger(__name__)


class LeroyMerlinScraper(BaseScraper):
    name = "leroymerlin.pl"
    base_url = "https://www.leroymerlin.pl"

    async def search(self, query: str, engine, *, max_results: int = 30) -> list[dict]:
        products = []
        search_url = f"{self.base_url}/search?q={quote_plus(query)}"

        try:
            async with engine.new_page_with_images() as page:
                logger.info(f"[LEROY] Navigating to {search_url}")

                await page.goto(self.base_url, wait_until="domcontentloaded")
                await human_delay(page, 800, 1500)

                # Cookie consent
                try:
                    consent = page.locator(
                        "button:has-text('Akceptuję'), "
                        "button:has-text('Zgadzam'), "
                        "[class*='cookie'] button, "
                        "#onetrust-accept-btn-handler"
                    ).first
                    if await consent.is_visible(timeout=3000):
                        await consent.click()
                        await human_delay(page, 500, 1000)
                except Exception:
                    pass

                await human_mouse_move(page)

                await page.goto(search_url, wait_until="domcontentloaded")
                await human_delay(page, 2000, 4000)
                await human_scroll(page)
                await page.wait_for_load_state("networkidle", timeout=15000)

                selectors = [
                    "[class*='product-card']",
                    "[class*='ProductCard']",
                    ".product-listing .product",
                    "[data-product-id]",
                    ".mc-product-card",
                ]

                product_elements = []
                for sel in selectors:
                    elements = await page.locator(sel).all()
                    if elements:
                        product_elements = elements
                        break

                if not product_elements:
                    content = await page.content()
                    return await self._extract_from_html(content)

                logger.info(f"[LEROY] Found {len(product_elements)} products")

                for elem in product_elements[:max_results]:
                    try:
                        product = await self._extract_product(elem)
                        if product and product.get("nazwa"):
                            products.append(product)
                    except Exception as e:
                        logger.debug(f"[LEROY] Error: {e}")
                        continue

        except Exception as e:
            logger.error(f"[LEROY] Scraping error: {e}")

        logger.info(f"[LEROY] Scraped {len(products)} products")
        return products

    async def _extract_product(self, elem) -> dict | None:
        try:
            name = ""
            for sel in ["[class*='title']", "[class*='name']", "h2", "h3", "a"]:
                try:
                    el = elem.locator(sel).first
                    if await el.is_visible(timeout=500):
                        name = (await el.inner_text()).strip()
                        if name and len(name) > 3:
                            break
                except Exception:
                    continue

            if not name:
                return None

            url = ""
            try:
                link = elem.locator("a").first
                href = await link.get_attribute("href") or ""
                url = href if href.startswith("http") else self.base_url + href
            except Exception:
                pass

            price = ""
            for sel in ["[class*='price']", "[class*='Price']", "[data-price]"]:
                try:
                    el = elem.locator(sel).first
                    if await el.is_visible(timeout=500):
                        price = (await el.inner_text()).strip()
                        if price:
                            break
                except Exception:
                    continue

            image = ""
            try:
                img = elem.locator("img").first
                image = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
                if image and not image.startswith("http"):
                    image = self.base_url + image
            except Exception:
                pass

            rating = ""
            try:
                r = elem.locator("[class*='rating'], [class*='stars']").first
                if await r.is_visible(timeout=500):
                    rating = (await r.inner_text()).strip()
            except Exception:
                pass

            return self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=image,
                ocena=rating,
            )
        except Exception:
            return None

    async def _extract_from_html(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        products = []
        soup = BeautifulSoup(html, "lxml")

        for card in soup.select("[class*='product-card'], [class*='ProductCard'], [data-product-id]"):
            name_el = card.select_one("[class*='title'], [class*='name'], h2, h3")
            if not name_el:
                continue

            name = name_el.get_text(strip=True)
            link_el = card.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            url = href if href.startswith("http") else self.base_url + href

            price_el = card.select_one("[class*='price']")
            price = price_el.get_text(strip=True) if price_el else ""

            img_el = card.select_one("img")
            img = (img_el.get("src") or img_el.get("data-src", "")) if img_el else ""

            products.append(self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=img,
            ))

        return products[:max_results]
