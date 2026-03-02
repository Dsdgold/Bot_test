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
        # Add spaces between bold tags to prevent word merging
        snippet_text = ""
        if snippet_el:
            for child in snippet_el.children:
                txt = child.get_text(strip=True) if hasattr(child, "get_text") else str(child).strip()
                if txt:
                    snippet_text += txt + " "
            snippet_text = snippet_text.strip()
        results.append({
            "title": title_el.get_text(strip=True),
            "href": title_el.get("href", ""),
            "snippet": snippet_text,
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

    # Known SIG manufacturers (extracted from product names/snippets)
    _KNOWN_PRODUCERS = [
        "WAVIN", "PROLEX", "GALECO", "GEBERIT", "PIPELIFE", "UPONOR",
        "REHAU", "TECE", "VIEGA", "PURMO", "KERMI", "KORAD", "NICZUK",
        "KESSEL", "ROCKWOOL", "ISOVER", "KNAUF", "RIGIPS", "BAUMIT",
        "WEBER", "CERESIT", "ATLAS", "POLBRUK", "STYROPMIN", "AUSTROTHERM",
        "TERMA", "FERRO", "GROHE", "HANSGROHE", "ROCA", "CERSANIT",
        "KOŁO", "DEANTE", "INVENA", "KAN", "ARMACELL", "PREFIX",
        "PAROC", "STEINBACHER", "SANHA", "GIACOMINI", "OVENTROP",
        "IMI", "DANFOSS", "WILO", "GRUNDFOS", "BOSCH", "JUNKERS",
        "VAILLANT", "BUDERUS", "VIESSMANN", "STIEBEL ELTRON",
    ]

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

        # Skip non-product pages (homepage, categories, blog, etc.)
        skip_patterns = [
            r'sig\.pl/?$',
            r'sig\.pl/marki',
            r'sig\.pl/kontakt',
            r'sig\.pl/kariera',
            r'sig\.pl/oferta$',
            r'sig\.pl/outlet$',
            r'sig\.pl/oddzialy',
            r'sig\.pl/media',
        ]
        for pattern in skip_patterns:
            if re.search(pattern, url):
                return None

        # --- Extract product code from URL ---
        # Pattern: /product-name,pCODE or /product-name,CODE
        kod_produktu = ""
        url_code_match = re.search(r',p?(\d{4,})\s*$', url)
        if url_code_match:
            kod_produktu = url_code_match.group(1)

        # Detect category pages (URL with ,c prefix = category)
        if re.search(r',c\d+', url):
            return None

        # Clean title - remove " - SIG" or " | SIG" or " - sig.pl" suffix
        original_title = title
        title = re.sub(r'\s*[-|–]\s*(?:SIG|sig\.pl).*$', '', title, flags=re.IGNORECASE).strip()
        # Remove trailing product codes like "826297"
        title = re.sub(r'\s+\d{5,}$', '', title).strip()
        # Remove trailing "..."
        title = re.sub(r'\s*\.{2,}\s*$', '', title).strip()

        if not title or len(title) < 5:
            return None

        # --- Extract data from snippet ---
        combined = f"{original_title} {snippet}"

        # Indeks SIG (e.g., "Indeks SIG T084330")
        indeks_sig = ""
        idx_match = re.search(r'Indeks\s+SIG\s+([A-Z]?\d+)', combined)
        if idx_match:
            indeks_sig = idx_match.group(1)

        # Indeks producenta (e.g., "Indeks producenta H RUP110/4,0")
        indeks_producenta = ""
        idx_prod_match = re.search(
            r'Indeks\s+producenta\s+(.+?)(?=\s+Indeks|\s+Brak|\s+Producent|$)',
            combined,
        )
        if idx_prod_match:
            indeks_producenta = idx_prod_match.group(1).strip()[:60]

        # Producer/manufacturer
        producent = self._extract_producer(combined)

        # Price from snippet
        price = ""
        jednostka = ""
        if snippet:
            price_match = re.search(
                r'(?<!\d)\b(\d{1,6}[.,]\d{2})\s*(?:zł|PLN)(?:/(\w+))?',
                snippet,
            )
            if price_match:
                price = price_match.group(1)
                if price_match.group(2):
                    jednostka = price_match.group(2)  # szt, mb, m, kg etc.

        # Category from snippet (e.g., "/ Rury drenażowe" or "/ Strony kategorii")
        kategoria = ""
        cat_match = re.search(r'/\s*([A-ZŻŹĆĄŚĘŁÓŃ][a-ząćęłńóśźż\s-]+?)(?:\s*$|\s*/)', snippet)
        if cat_match:
            kat = cat_match.group(1).strip()
            if kat.lower() not in ('strony kategorii', 'strona główna'):
                kategoria = kat

        # Description - clean snippet, remove parsed fields
        opis = self._clean_description(snippet)

        return self._build_product(
            nazwa=title,
            cena=self._normalize_price(price),
            zrodlo=self.name,
            url=url,
            producent=producent,
            indeks=indeks_sig or kod_produktu,
            indeks_producenta=indeks_producenta,
            kod_produktu=kod_produktu,
            jednostka=jednostka,
            kategoria=kategoria,
            opis=opis,
        )

    def _extract_producer(self, text: str) -> str:
        """Try to find manufacturer name in text."""
        text_upper = text.upper()
        for producer in self._KNOWN_PRODUCERS:
            # Match whole word
            if re.search(r'\b' + re.escape(producer) + r'\b', text_upper):
                return producer.title()
        return ""

    def _clean_description(self, snippet: str) -> str:
        """Clean snippet text for use as description."""
        if not snippet:
            return ""
        # Remove index info
        desc = re.sub(r'Indeks\s+SIG\s+\S+', '', snippet)
        desc = re.sub(r'Indeks\s+producenta\s+\S+', '', desc)
        # Remove "Brak opinii"
        desc = re.sub(r'Brak\s+opinii', '', desc)
        # Remove category suffixes
        desc = re.sub(r'/\s*[A-ZŻŹĆĄŚĘŁÓŃ][a-ząćęłńóśźż\s-]+$', '', desc)
        # Remove LiveChat text
        desc = re.sub(r'Have any questions.*$', '', desc, flags=re.IGNORECASE)
        # Collapse whitespace
        desc = re.sub(r'\s+', ' ', desc).strip()
        # Remove leading " - "
        desc = re.sub(r'^[\s\-/]+', '', desc).strip()
        return desc[:200] if desc else ""

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
