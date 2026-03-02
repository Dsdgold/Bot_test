"""Base scraper class."""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Base class for all store scrapers."""

    name: str = "base"
    base_url: str = ""

    @abstractmethod
    async def search(self, query: str, engine) -> list[dict]:
        """Search for products. Returns list of product dicts."""
        ...

    def _normalize_price(self, price_text: str) -> str:
        """Normalize price string to a consistent format."""
        if not price_text:
            return ""
        price_text = price_text.strip()
        price_text = price_text.replace("\xa0", " ")
        price_text = price_text.replace("zł", "").replace("PLN", "").strip()
        # Handle "1 234,56" format
        price_text = price_text.replace(" ", "")
        # Standardize decimal separator
        price_text = price_text.replace(",", ".")
        try:
            val = float(price_text)
            return f"{val:.2f}"
        except ValueError:
            return price_text

    def _build_product(self, **kwargs) -> dict:
        """Build a standardized product dict."""
        return {
            "nazwa": kwargs.get("nazwa", ""),
            "cena": kwargs.get("cena", ""),
            "cena_regularna": kwargs.get("cena_regularna", ""),
            "waluta": kwargs.get("waluta", "PLN"),
            "dostepnosc": kwargs.get("dostepnosc", ""),
            "zrodlo": kwargs.get("zrodlo", self.name),
            "url": kwargs.get("url", ""),
            "zdjecie": kwargs.get("zdjecie", ""),
            "ocena": kwargs.get("ocena", ""),
            "liczba_opinii": kwargs.get("liczba_opinii", ""),
        }
