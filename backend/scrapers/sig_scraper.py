"""Scraper for sig.pl - building materials store.

Uses DuckDuckGo site-search via HTTP to find SIG products because
sig.pl is protected by Cloudflare which blocks headless browsers.
Uses sync HTTP POST in a subprocess to avoid bot detection.
"""

import json
import logging
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse, parse_qs

from backend.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


# Standalone script that runs in a subprocess to fetch DDG results
_DDG_FETCH_SCRIPT = '''
import json
import sys
import httpx
from bs4 import BeautifulSoup

query = sys.argv[1]
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8",
    "Referer": "https://duckduckgo.com/",
}

try:
    resp = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "b": "", "kl": "pl-pl"},
        headers=headers,
        follow_redirects=True,
        timeout=15,
    )
    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for r in soup.select(".result"):
        title_el = r.select_one(".result__a")
        snippet_el = r.select_one(".result__snippet")
        if not title_el:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "href": title_el.get("href", ""),
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
    print(json.dumps(results))
except Exception as e:
    print(json.dumps({"error": str(e)}))
'''


class SigScraper(BaseScraper):
    name = "sig.pl"
    base_url = "https://www.sig.pl"

    async def search(self, query: str, engine) -> list[dict]:
        """Search SIG products via DuckDuckGo to bypass Cloudflare."""
        products = []
        ddg_query = f"site:sig.pl {query}"

        try:
            logger.info(f"[SIG] Searching DuckDuckGo: {ddg_query}")

            # Run HTTP fetch in a subprocess to avoid async bot detection
            result = subprocess.run(
                [sys.executable, "-c", _DDG_FETCH_SCRIPT, ddg_query],
                capture_output=True,
                text=True,
                timeout=25,
            )

            if result.returncode != 0:
                logger.error(f"[SIG] Subprocess error: {result.stderr[:200]}")
                return []

            data = json.loads(result.stdout)

            if isinstance(data, dict) and "error" in data:
                logger.error(f"[SIG] Fetch error: {data['error']}")
                return []

            logger.info(f"[SIG] DuckDuckGo returned {len(data)} results")

            for item in data:
                try:
                    product = self._parse_result(item)
                    if product:
                        products.append(product)
                except Exception as e:
                    logger.debug(f"[SIG] Error parsing result: {e}")
                    continue

        except subprocess.TimeoutExpired:
            logger.error("[SIG] DuckDuckGo request timed out")
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

    def _parse_result(self, item: dict) -> dict | None:
        """Parse a DuckDuckGo result dict into a product."""
        title = item.get("title", "")
        raw_href = item.get("href", "")
        snippet = item.get("snippet", "")

        if not title:
            return None

        # Extract actual URL
        url = self._extract_ddg_url(raw_href)

        # Only keep sig.pl links
        if "sig.pl" not in url:
            return None

        # Clean title - remove " - SIG" or " | SIG" suffix
        title = re.sub(r'\s*[-|–]\s*SIG.*$', '', title).strip()

        # Try to extract price from snippet
        price = ""
        if snippet:
            price_match = re.search(
                r'(\d[\d\s]*[.,]\d{2})\s*(?:zł|PLN|pln)',
                snippet,
            )
            if price_match:
                price = price_match.group(1)

        return self._build_product(
            nazwa=title,
            cena=self._normalize_price(price),
            zrodlo=self.name,
            url=url,
            dostepnosc=snippet[:200] if snippet else "",
        )

    def _extract_ddg_url(self, href: str) -> str:
        """Extract the real URL from a DuckDuckGo redirect link."""
        if not href:
            return ""

        # DuckDuckGo wraps URLs: //duckduckgo.com/l/?uddg=https%3A%2F%2F...
        if "uddg=" in href:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                return unquote(params["uddg"][0])

        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href

        return href
