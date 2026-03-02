"""Scraper for sig.pl - building materials store."""

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
        products = []
        search_url = f"{self.base_url}/szukaj-produktow?searchquery={quote_plus(query)}"

        try:
            async with engine.new_page_with_images() as page:
                # Collect API responses that might contain product data
                api_products = []

                async def handle_response(response):
                    """Intercept XHR/API responses to capture product data."""
                    try:
                        url = response.url
                        ct = response.headers.get("content-type", "")
                        if "json" in ct and response.status == 200:
                            body = await response.text()
                            if any(kw in body.lower() for kw in [
                                "product", "produkt", "nazwa", "price", "cena",
                            ]):
                                logger.info(f"[SIG] Intercepted API: {url[:120]}")
                                try:
                                    data = json.loads(body)
                                    extracted = self._extract_from_api(data)
                                    if extracted:
                                        api_products.extend(extracted)
                                        logger.info(f"[SIG] Got {len(extracted)} products from API")
                                except json.JSONDecodeError:
                                    pass
                    except Exception:
                        pass

                page.on("response", handle_response)

                # --- Strategy A: Use the search bar like a real user ---
                logger.info(f"[SIG] Going to homepage to use search bar")
                await page.goto(self.base_url, wait_until="domcontentloaded")
                await human_delay(page, 1500, 2500)

                # Handle cookie consent
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

                # Find and use the search input field
                search_submitted = False
                search_selectors = [
                    "input[name='searchquery']",
                    "input[name='search']",
                    "input[name='q']",
                    "input[type='search']",
                    "input[placeholder*='szukaj' i]",
                    "input[placeholder*='Szukaj' i]",
                    "input[placeholder*='search' i]",
                    "input[placeholder*='Wpisz' i]",
                    "input[placeholder*='produkt' i]",
                    "input[class*='search']",
                    "input[id*='search']",
                    "[class*='search'] input[type='text']",
                    "[class*='search-bar'] input",
                    "header input[type='text']",
                    "nav input[type='text']",
                ]

                for sel in search_selectors:
                    try:
                        search_input = page.locator(sel).first
                        if await search_input.is_visible(timeout=1500):
                            logger.info(f"[SIG] Found search input: {sel}")
                            # Click and type like a human
                            await search_input.click()
                            await human_delay(page, 300, 600)
                            await search_input.fill("")
                            await human_delay(page, 200, 400)
                            # Type character by character for realism
                            await search_input.type(query, delay=50)
                            await human_delay(page, 500, 1000)
                            # Submit with Enter
                            await search_input.press("Enter")
                            search_submitted = True
                            logger.info(f"[SIG] Search submitted via search bar")
                            break
                    except Exception:
                        continue

                if not search_submitted:
                    # Fallback: navigate directly to search URL
                    logger.info(f"[SIG] No search input found, navigating to {search_url}")
                    await page.goto(search_url, wait_until="domcontentloaded")

                # Wait for results to load
                await human_delay(page, 3000, 5000)
                await human_scroll(page)

                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # Extra wait for JS rendering
                await human_delay(page, 2000, 3000)

                # Log current state for debugging
                current_url = page.url
                title = await page.title()
                logger.info(f"[SIG] Page URL: {current_url}")
                logger.info(f"[SIG] Page title: {title}")

                # Check API interception results first
                if api_products:
                    logger.info(f"[SIG] {len(api_products)} products from API interception")
                    products.extend(api_products)

                if not products:
                    # Try CSS selectors for product cards
                    products = await self._try_css_selectors(page)

                if not products:
                    # Fallback: extract from full page HTML
                    content = await page.content()
                    logger.info(f"[SIG] HTML length: {len(content)}")
                    products = await self._extract_from_html(content, query)

                if not products:
                    # Last resort: scan all links for product URL pattern
                    products = await self._scan_links(page)

                # Scroll for lazy-loaded content
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

        logger.info(f"[SIG] Scraped {len(products)} products total")
        return products

    async def _try_css_selectors(self, page) -> list[dict]:
        """Try to find product cards using various CSS selectors."""
        selectors = [
            "[class*='product-card']",
            "[class*='product-item']",
            "[class*='product-tile']",
            "[class*='ProductCard']",
            "[data-product]",
            "[data-productid]",
            "[data-product-id]",
            ".product",
            "[class*='search-result'] [class*='item']",
            "article[class*='product']",
            "[class*='listing'] [class*='item']",
            "[class*='productBox']",
            "[class*='product-box']",
            "[class*='prod-']",
            "[class*='search'] [class*='row'] > div",
            "[class*='result'] [class*='card']",
        ]

        for sel in selectors:
            elements = await page.locator(sel).all()
            if elements:
                logger.info(f"[SIG] Found {len(elements)} products with '{sel}'")
                products = []
                for elem in elements[:30]:
                    try:
                        product = await self._extract_product(elem)
                        if product and product.get("nazwa"):
                            products.append(product)
                    except Exception:
                        continue
                if products:
                    return products

        return []

    async def _scan_links(self, page) -> list[dict]:
        """Scan all links on page for SIG product URL patterns."""
        products = []
        all_links = await page.locator("a[href]").all()
        logger.info(f"[SIG] Scanning {len(all_links)} links for product URLs")

        for link_el in all_links[:200]:
            try:
                href = await link_el.get_attribute("href") or ""
                if not SIG_PRODUCT_URL_RE.search(href):
                    continue
                text = (await link_el.inner_text()).strip()
                if not text or len(text) < 5:
                    continue
                url = href if href.startswith("http") else self.base_url + href

                # Walk up DOM to find price/image
                price = ""
                image = ""
                current = link_el
                for _ in range(5):
                    parent = current.locator("..")
                    try:
                        price_el = parent.locator(
                            "[class*='price'], [class*='cena'], [class*='Price']"
                        ).first
                        if await price_el.is_visible(timeout=200):
                            price = (await price_el.inner_text()).strip()
                            break
                    except Exception:
                        pass
                    current = parent

                current = link_el
                for _ in range(5):
                    parent = current.locator("..")
                    try:
                        img = parent.locator("img").first
                        src = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
                        if src:
                            image = src if src.startswith("http") else self.base_url + src
                            break
                    except Exception:
                        pass
                    current = parent

                products.append(self._build_product(
                    nazwa=text[:200],
                    cena=self._normalize_price(price),
                    zrodlo=self.name,
                    url=url,
                    zdjecie=image,
                ))
            except Exception:
                continue

        return products

    def _extract_from_api(self, data) -> list[dict]:
        """Extract products from intercepted API JSON response."""
        products = []

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ["products", "items", "results", "data", "hits",
                        "produkty", "wyniki", "content", "records",
                        "searchResults", "productList"]:
                if key in data:
                    val = data[key]
                    if isinstance(val, list):
                        items = val
                        break
                    elif isinstance(val, dict):
                        for subkey in ["products", "items", "results", "hits", "content"]:
                            if subkey in val and isinstance(val[subkey], list):
                                items = val[subkey]
                                break
                        if items:
                            break

        for item in items[:30]:
            if not isinstance(item, dict):
                continue

            name = ""
            for key in ["name", "nazwa", "title", "productName", "label",
                        "description", "shortName", "displayName"]:
                if key in item and isinstance(item[key], str):
                    name = item[key].strip()
                    if name:
                        break

            if not name:
                continue

            price = ""
            for key in ["price", "cena", "unitPrice", "grossPrice", "netPrice",
                        "currentPrice", "finalPrice", "priceValue"]:
                if key in item:
                    val = item[key]
                    if isinstance(val, (int, float)):
                        price = f"{val:.2f}"
                    elif isinstance(val, str):
                        price = val
                    elif isinstance(val, dict):
                        for subkey in ["value", "amount", "gross", "net", "formatted"]:
                            if subkey in val:
                                sv = val[subkey]
                                if isinstance(sv, (int, float)):
                                    price = f"{sv:.2f}"
                                elif isinstance(sv, str):
                                    price = sv
                                if price:
                                    break
                    if price:
                        break

            url = ""
            for key in ["url", "link", "href", "slug", "productUrl", "seoUrl"]:
                if key in item and isinstance(item[key], str):
                    url = item[key]
                    if url and not url.startswith("http"):
                        url = self.base_url + ("/" + url).replace("//", "/")
                    break

            image = ""
            for key in ["image", "imageUrl", "img", "thumbnail", "photo",
                        "zdjecie", "mainImage", "pictureUrl"]:
                if key in item:
                    val = item[key]
                    if isinstance(val, str):
                        image = val
                    elif isinstance(val, dict):
                        image = val.get("url", "") or val.get("src", "")
                    elif isinstance(val, list) and val:
                        v0 = val[0]
                        if isinstance(v0, str):
                            image = v0
                        elif isinstance(v0, dict):
                            image = v0.get("url", "") or v0.get("src", "")
                    if image:
                        if not image.startswith("http"):
                            image = self.base_url + image
                        break

            products.append(self._build_product(
                nazwa=name[:200],
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=image,
            ))

        return products

    async def _extract_product(self, elem) -> dict | None:
        """Extract product data from a card element."""
        try:
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

            url = ""
            try:
                link = elem.locator("a").first
                url = await link.get_attribute("href") or ""
                if url and not url.startswith("http"):
                    url = self.base_url + url
            except Exception:
                pass

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

            image = ""
            try:
                img = elem.locator("img").first
                image = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
                if image and not image.startswith("http"):
                    image = self.base_url + image
            except Exception:
                pass

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
        product_links = soup.find_all("a", href=SIG_PRODUCT_URL_RE)
        logger.info(f"[SIG] Found {len(product_links)} links matching product URL pattern")

        for a in product_links:
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if not text or len(text) < 5:
                continue

            url = href if href.startswith("http") else self.base_url + href

            price = ""
            img = ""
            parent = a.find_parent()
            for _ in range(5):
                if not parent:
                    break
                if not price:
                    price_el = parent.find(
                        class_=lambda c: c and any(
                            x in c.lower() for x in ["price", "cena"]
                        )
                    )
                    if price_el:
                        price = price_el.get_text(strip=True)
                if not img:
                    img_el = parent.find("img")
                    if img_el:
                        img = img_el.get("src", "") or img_el.get("data-src", "")
                        if img and not img.startswith("http"):
                            img = self.base_url + img
                if price and img:
                    break
                parent = parent.find_parent()

            products.append(self._build_product(
                nazwa=text[:200],
                cena=self._normalize_price(price),
                zrodlo=self.name,
                url=url,
                zdjecie=img,
            ))

        # Strategy 2: JSON-LD structured data
        if not products:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, list):
                        for item in data:
                            p = self._parse_jsonld(item)
                            if p:
                                products.append(p)
                    elif isinstance(data, dict):
                        p = self._parse_jsonld(data)
                        if p:
                            products.append(p)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Strategy 3: Inline JSON data (__NEXT_DATA__, window.data, etc.)
        if not products:
            for script in soup.find_all("script"):
                if not script.string:
                    continue
                text = script.string
                for pattern in [
                    r'(?:__NEXT_DATA__|__NUXT__|window\.__data|window\.products)\s*=\s*(\{.+?\});',
                    r'JSON\.parse\([\'"](.+?)[\'"]\)',
                ]:
                    match = re.search(pattern, text, re.DOTALL)
                    if match:
                        try:
                            raw = match.group(1)
                            raw = raw.encode().decode("unicode_escape") if "\\u" in raw else raw
                            data = json.loads(raw)
                            extracted = self._extract_from_api(data)
                            if extracted:
                                products.extend(extracted)
                                logger.info(f"[SIG] Found {len(extracted)} products in inline JSON")
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass

        # Strategy 4: Query-word matching (last resort)
        if not products:
            query_words = [w.lower() for w in query.split() if len(w) > 2]
            for a in soup.find_all("a", href=True):
                text = a.get_text(strip=True)
                href = a["href"]
                if len(text) < 10:
                    continue
                text_lower = text.lower()
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

    def _parse_jsonld(self, data: dict) -> dict | None:
        """Parse a JSON-LD Product object."""
        if not isinstance(data, dict):
            return None
        dtype = data.get("@type", "")
        if dtype not in ("Product", "IndividualProduct"):
            return None
        name = data.get("name", "")
        if not name:
            return None

        url = data.get("url", "")
        if url and not url.startswith("http"):
            url = self.base_url + url

        price = ""
        offers = data.get("offers", {})
        if isinstance(offers, dict):
            price = str(offers.get("price", ""))
        elif isinstance(offers, list) and offers:
            price = str(offers[0].get("price", ""))

        image = data.get("image", "")
        if isinstance(image, list) and image:
            image = image[0]

        return self._build_product(
            nazwa=name[:200],
            cena=self._normalize_price(price),
            zrodlo=self.name,
            url=url,
            zdjecie=image if isinstance(image, str) else "",
        )
