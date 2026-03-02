"""Scraper for ceneo.pl - price comparison engine."""

import logging
from urllib.parse import quote_plus

from backend.scrapers.base import BaseScraper
from backend.utils.anti_detect import human_delay, human_scroll, human_mouse_move

logger = logging.getLogger(__name__)


class CeneoScraper(BaseScraper):
    name = "ceneo.pl"
    base_url = "https://www.ceneo.pl"

    async def search(self, query: str, engine) -> list[dict]:
        products = []
        search_url = f"{self.base_url}/szukaj-{quote_plus(query)}"

        try:
            async with engine.new_page_with_images() as page:
                logger.info(f"[CENEO] Navigating to {search_url}")

                await page.goto(self.base_url, wait_until="domcontentloaded")
                await human_delay(page, 800, 1500)

                # Cookie consent
                try:
                    consent = page.locator(
                        "button[class*='accept'], "
                        "button:has-text('Akceptuję'), "
                        "button:has-text('Zgadzam się'), "
                        "[class*='cookie'] button"
                    ).first
                    if await consent.is_visible(timeout=3000):
                        await consent.click()
                        await human_delay(page, 500, 1000)
                except Exception:
                    pass

                await human_mouse_move(page)

                # Navigate to search results
                await page.goto(search_url, wait_until="domcontentloaded")
                await human_delay(page, 2000, 3500)
                await human_scroll(page)
                await page.wait_for_load_state("networkidle", timeout=15000)

                # Ceneo product selectors
                selectors = [
                    ".cat-prod-row",
                    ".category-list-body .cat-prod-row",
                    "[class*='product-card']",
                    ".search-results .product",
                    ".js_category-list-body .cat-prod-row",
                    ".category-list__product",
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

                logger.info(f"[CENEO] Found {len(product_elements)} products")

                for elem in product_elements[:30]:
                    try:
                        product = await self._extract_ceneo_product(elem)
                        if product and product.get("nazwa"):
                            products.append(product)
                    except Exception as e:
                        logger.debug(f"[CENEO] Error: {e}")
                        continue

        except Exception as e:
            logger.error(f"[CENEO] Scraping error: {e}")

        logger.info(f"[CENEO] Scraped {len(products)} products")
        return products

    async def _extract_ceneo_product(self, elem) -> dict | None:
        try:
            # Name
            name = ""
            for sel in [".cat-prod-row__name a", "a.go-to-product",
                        "[class*='product-name']", "a[class*='name']", "strong a"]:
                try:
                    el = elem.locator(sel).first
                    if await el.is_visible(timeout=500):
                        name = (await el.inner_text()).strip()
                        if name:
                            break
                except Exception:
                    continue

            if not name:
                # Try getting text from any <a> tag
                try:
                    a_el = elem.locator("a").first
                    name = (await a_el.inner_text()).strip()
                except Exception:
                    return None

            if not name:
                return None

            # URL
            url = ""
            try:
                link = elem.locator("a").first
                href = await link.get_attribute("href") or ""
                url = href if href.startswith("http") else self.base_url + href
            except Exception:
                pass

            # Price
            price = ""
            for sel in [".cat-prod-row__price .price .value",
                        "[class*='price'] .value", ".price-format",
                        "[class*='price']"]:
                try:
                    price_el = elem.locator(sel).first
                    if await price_el.is_visible(timeout=500):
                        price = (await price_el.inner_text()).strip()
                        if price:
                            break
                except Exception:
                    continue

            # Rating
            rating = ""
            try:
                rating_el = elem.locator("[class*='rating'], [class*='score']").first
                if await rating_el.is_visible(timeout=500):
                    rating = (await rating_el.inner_text()).strip()
            except Exception:
                pass

            # Review count
            reviews = ""
            try:
                rev_el = elem.locator("[class*='review'], [class*='opinion']").first
                if await rev_el.is_visible(timeout=500):
                    reviews = (await rev_el.inner_text()).strip()
            except Exception:
                pass

            # Image
            image = ""
            try:
                img = elem.locator("img").first
                image = await img.get_attribute("src") or await img.get_attribute("data-original") or ""
            except Exception:
                pass

            return self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=image,
                ocena=rating,
                liczba_opinii=reviews,
            )
        except Exception:
            return None

    async def _extract_from_html(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        products = []
        soup = BeautifulSoup(html, "lxml")

        for row in soup.select(".cat-prod-row, [class*='product-card']"):
            name_el = row.select_one("a.go-to-product, [class*='name'] a, strong a")
            if not name_el:
                continue

            name = name_el.get_text(strip=True)
            href = name_el.get("href", "")
            url = href if href.startswith("http") else self.base_url + href

            price_el = row.select_one("[class*='price']")
            price = price_el.get_text(strip=True) if price_el else ""

            img_el = row.select_one("img")
            img = img_el.get("src", "") if img_el else ""

            products.append(self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=img,
            ))

        return products[:30]
