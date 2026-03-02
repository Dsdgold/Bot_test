"""Scraper for Google Shopping - covers many stores at once."""

import logging
from urllib.parse import quote_plus

from backend.scrapers.base import BaseScraper
from backend.utils.anti_detect import human_delay, human_scroll, human_mouse_move

logger = logging.getLogger(__name__)


class GoogleShoppingScraper(BaseScraper):
    name = "Google Shopping"
    base_url = "https://www.google.pl"

    async def search(self, query: str, engine, *, max_results: int = 30) -> list[dict]:
        products = []
        search_url = (
            f"{self.base_url}/search?q={quote_plus(query)}"
            f"&tbm=shop&hl=pl&gl=pl"
        )

        try:
            async with engine.new_page_with_images() as page:
                logger.info(f"[GOOGLE] Navigating to {search_url}")

                # Go to google first
                await page.goto(self.base_url, wait_until="domcontentloaded")
                await human_delay(page, 500, 1500)

                # Cookie consent for Google
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

                # Navigate to shopping results
                await page.goto(search_url, wait_until="domcontentloaded")
                await human_delay(page, 2000, 4000)
                await human_scroll(page)

                # Google Shopping selectors
                selectors = [
                    ".sh-dgr__grid-result",
                    ".sh-dgr__content",
                    "[data-docid]",
                    ".sh-pr__product-results .sh-dlr__list-result",
                    ".KZmu8e",
                    ".i0X6df",
                    ".xcR77",
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

                logger.info(f"[GOOGLE] Found {len(product_elements)} products")

                for elem in product_elements[:max_results]:
                    try:
                        product = await self._extract_product(elem)
                        if product and product.get("nazwa"):
                            products.append(product)
                    except Exception as e:
                        logger.debug(f"[GOOGLE] Error: {e}")
                        continue

        except Exception as e:
            logger.error(f"[GOOGLE] Scraping error: {e}")

        logger.info(f"[GOOGLE] Scraped {len(products)} products")
        return products

    async def _extract_product(self, elem) -> dict | None:
        try:
            # Name
            name = ""
            for sel in ["h3", "h4", "[class*='name']", "[class*='title']",
                        "a[class*='shntl']", ".Xjkr3b", ".tAxDx"]:
                try:
                    el = elem.locator(sel).first
                    if await el.is_visible(timeout=500):
                        name = (await el.inner_text()).strip()
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
                href = await link.get_attribute("href") or ""
                url = href if href.startswith("http") else self.base_url + href
            except Exception:
                pass

            # Price
            price = ""
            for sel in ["[class*='price']", "span[class*='a8Pemb']", ".kHxwFf",
                        "b", "strong"]:
                try:
                    el = elem.locator(sel).first
                    if await el.is_visible(timeout=500):
                        text = (await el.inner_text()).strip()
                        if "zł" in text or any(c.isdigit() for c in text):
                            price = text
                            break
                except Exception:
                    continue

            # Store name
            source = self.name
            try:
                store_el = elem.locator(
                    "[class*='merchant'], [class*='store'], .aULzUe, .IuHnof"
                ).first
                if await store_el.is_visible(timeout=500):
                    store = (await store_el.inner_text()).strip()
                    if store:
                        source = f"Google Shopping ({store})"
            except Exception:
                pass

            # Rating
            rating = ""
            try:
                rating_el = elem.locator("[class*='rating'], [aria-label*='rating']").first
                if await rating_el.is_visible(timeout=500):
                    rating = await rating_el.get_attribute("aria-label") or (await rating_el.inner_text()).strip()
            except Exception:
                pass

            # Image
            image = ""
            try:
                img = elem.locator("img").first
                image = await img.get_attribute("src") or ""
            except Exception:
                pass

            return self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=source,
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

        for item in soup.select("[data-docid], .sh-dgr__grid-result, .sh-dgr__content"):
            name_el = item.select_one("h3, h4, [class*='name']")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)

            link_el = item.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            url = href if href.startswith("http") else self.base_url + href

            # Find price - look for text with "zł"
            price = ""
            for el in item.find_all(["span", "b", "strong"]):
                text = el.get_text(strip=True)
                if "zł" in text or ("," in text and any(c.isdigit() for c in text)):
                    price = text
                    break

            img_el = item.select_one("img")
            img = img_el.get("src", "") if img_el else ""

            products.append(self._build_product(
                nazwa=name,
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=img,
            ))

        return products[:max_results]
