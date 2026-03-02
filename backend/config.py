import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--window-size=1920,1080",
    "--disable-extensions",
    "--disable-gpu",
    "--lang=pl-PL,pl",
]

REQUEST_TIMEOUT = 30000
NAV_TIMEOUT = 45000

MAX_CONCURRENT_SCRAPERS = 3
MAX_RESULTS_PER_SOURCE = 50
