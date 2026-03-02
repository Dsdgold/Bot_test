"""CSV export utilities."""

import csv
import uuid
from datetime import datetime
from pathlib import Path

from backend.config import EXPORTS_DIR


def export_to_csv(products: list[dict], query: str) -> str:
    """Export product list to a CSV file. Returns the filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_query = "".join(c if c.isalnum() or c in " -_" else "" for c in query)
    safe_query = safe_query.replace(" ", "_")[:50]
    filename = f"{safe_query}_{timestamp}_{uuid.uuid4().hex[:6]}.csv"
    filepath = EXPORTS_DIR / filename

    if not products:
        return ""

    fieldnames = [
        "nazwa", "cena", "cena_regularna", "waluta", "dostepnosc",
        "zrodlo", "url", "zdjecie", "ocena", "liczba_opinii",
        "data_pobrania",
    ]

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";",
                                extrasaction="ignore")
        writer.writeheader()
        for product in products:
            product["data_pobrania"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(product)

    return filename


def list_exports() -> list[dict]:
    """List all available CSV exports."""
    exports = []
    for f in sorted(EXPORTS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        stat = f.stat()
        exports.append({
            "filename": f.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "created": datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        })
    return exports


def delete_export(filename: str) -> bool:
    """Delete an export file. Returns True if deleted."""
    filepath = EXPORTS_DIR / filename
    if filepath.exists() and filepath.parent == EXPORTS_DIR:
        filepath.unlink()
        return True
    return False
