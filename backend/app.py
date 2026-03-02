"""FastAPI application - Product Scraper Bot."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import EXPORTS_DIR, MAX_CONCURRENT_SCRAPERS
from backend.scraper_engine import ScraperEngine
from backend.scrapers.sig_scraper import SigScraper
from backend.scrapers.ceneo_scraper import CeneoScraper
from backend.scrapers.google_shopping_scraper import GoogleShoppingScraper
from backend.scrapers.leroy_scraper import LeroyMerlinScraper
from backend.utils.csv_export import export_to_csv, list_exports, delete_export

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Available scrapers
SCRAPERS = {
    "sig": SigScraper(),
    "ceneo": CeneoScraper(),
    "google_shopping": GoogleShoppingScraper(),
    "leroy_merlin": LeroyMerlinScraper(),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    engine = await ScraperEngine.get_instance()
    logger.info("Scraper Bot started")
    yield
    await engine.shutdown()
    logger.info("Scraper Bot stopped")


app = FastAPI(
    title="Product Scraper Bot",
    description="Web scraper for product data collection",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# Active jobs tracking
active_jobs: dict[str, dict] = {}


# --- Models ---

class SearchRequest(BaseModel):
    query: str
    sources: list[str] | None = None  # None = all sources


class SearchResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatus(BaseModel):
    job_id: str
    status: str  # pending, running, completed, failed
    query: str
    progress: int
    total_sources: int
    products_found: int
    current_source: str
    results: list[dict]
    csv_file: str
    errors: list[str]
    started_at: str
    completed_at: str


# --- API Routes ---

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the frontend."""
    index_path = frontend_dir / "index.html"
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.get("/api/sources")
async def get_sources():
    """List available scraper sources."""
    return {
        "sources": [
            {"id": key, "name": scraper.name, "url": scraper.base_url}
            for key, scraper in SCRAPERS.items()
        ]
    }


@app.post("/api/search", response_model=SearchResponse)
async def start_search(request: SearchRequest):
    """Start a new scraping job."""
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    job_id = uuid.uuid4().hex[:12]
    sources = request.sources or list(SCRAPERS.keys())

    # Validate sources
    valid_sources = [s for s in sources if s in SCRAPERS]
    if not valid_sources:
        raise HTTPException(status_code=400, detail="No valid sources selected")

    active_jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "query": request.query.strip(),
        "progress": 0,
        "total_sources": len(valid_sources),
        "products_found": 0,
        "current_source": "",
        "results": [],
        "csv_file": "",
        "errors": [],
        "sources": valid_sources,
        "started_at": datetime.now().isoformat(),
        "completed_at": "",
    }

    # Run scraping in background
    asyncio.create_task(_run_scraping_job(job_id))

    return SearchResponse(
        job_id=job_id,
        status="pending",
        message=f"Scraping started for '{request.query}' across {len(valid_sources)} sources",
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    """Get the status of a scraping job."""
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = active_jobs[job_id]
    return JobStatus(
        job_id=job["job_id"],
        status=job["status"],
        query=job["query"],
        progress=job["progress"],
        total_sources=job["total_sources"],
        products_found=job["products_found"],
        current_source=job["current_source"],
        results=job["results"],
        csv_file=job["csv_file"],
        errors=job["errors"],
        started_at=job["started_at"],
        completed_at=job["completed_at"],
    )


@app.get("/api/exports")
async def get_exports():
    """List all CSV exports."""
    return {"exports": list_exports()}


@app.get("/api/exports/{filename}")
async def download_export(filename: str):
    """Download a CSV export file."""
    filepath = EXPORTS_DIR / filename
    if not filepath.exists() or filepath.parent != EXPORTS_DIR:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/exports/{filename}")
async def remove_export(filename: str):
    """Delete a CSV export file."""
    if delete_export(filename):
        return {"message": "File deleted"}
    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/jobs")
async def list_jobs():
    """List all jobs."""
    jobs = []
    for job in active_jobs.values():
        jobs.append({
            "job_id": job["job_id"],
            "status": job["status"],
            "query": job["query"],
            "products_found": job["products_found"],
            "started_at": job["started_at"],
            "csv_file": job["csv_file"],
        })
    return {"jobs": sorted(jobs, key=lambda j: j["started_at"], reverse=True)}


# --- Background job runner ---

async def _run_scraping_job(job_id: str):
    """Execute scraping job across selected sources."""
    job = active_jobs[job_id]
    job["status"] = "running"
    query = job["query"]
    sources = job["sources"]

    engine = await ScraperEngine.get_instance()
    all_products = []

    # Scrape sources with concurrency limit
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPERS)

    async def scrape_source(source_id: str):
        async with semaphore:
            scraper = SCRAPERS[source_id]
            job["current_source"] = scraper.name
            logger.info(f"[JOB {job_id}] Scraping {scraper.name}...")
            try:
                products = await scraper.search(query, engine)
                return source_id, products, None
            except Exception as e:
                logger.error(f"[JOB {job_id}] Error scraping {scraper.name}: {e}")
                return source_id, [], str(e)

    try:
        tasks = [scrape_source(sid) for sid in sources]

        for i, coro in enumerate(asyncio.as_completed(tasks)):
            source_id, products, error = await coro
            job["progress"] = i + 1

            if error:
                job["errors"].append(f"{SCRAPERS[source_id].name}: {error}")
            else:
                all_products.extend(products)
                job["products_found"] = len(all_products)

            logger.info(
                f"[JOB {job_id}] Progress: {job['progress']}/{job['total_sources']} "
                f"- {len(products)} products from {SCRAPERS[source_id].name}"
            )

        # Deduplicate by name (keep the one with a price)
        seen_names = {}
        unique_products = []
        for p in all_products:
            key = p["nazwa"].lower().strip()
            if key not in seen_names:
                seen_names[key] = p
                unique_products.append(p)
            elif not seen_names[key].get("cena") and p.get("cena"):
                # Replace with the one that has a price
                idx = unique_products.index(seen_names[key])
                unique_products[idx] = p
                seen_names[key] = p

        # Sort by price (cheapest first), items without price go last
        def sort_key(p):
            try:
                return float(p["cena"]) if p["cena"] else float("inf")
            except (ValueError, TypeError):
                return float("inf")

        unique_products.sort(key=sort_key)

        job["results"] = unique_products
        job["products_found"] = len(unique_products)

        # Export to CSV
        if unique_products:
            csv_filename = export_to_csv(unique_products, query)
            job["csv_file"] = csv_filename

        job["status"] = "completed"
        job["completed_at"] = datetime.now().isoformat()
        job["current_source"] = ""

        logger.info(
            f"[JOB {job_id}] Completed! {len(unique_products)} unique products found"
        )

    except Exception as e:
        job["status"] = "failed"
        job["errors"].append(f"Job failed: {str(e)}")
        job["completed_at"] = datetime.now().isoformat()
        logger.error(f"[JOB {job_id}] Failed: {e}")
