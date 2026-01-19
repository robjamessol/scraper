"""
FastAPI web application for Newsletter Advertiser Intelligence System.

Provides:
- Web dashboard to view and trigger scans
- REST API for n8n/automation integration
- Background job scheduling
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd

from ..scrapers import HealthcareBrewScraper, MorningBrewScraper, SponsorInfo
from ..enrichment import AdvertiserCategorizer


logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent.parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = Path(__file__).parent / "templates"
DATA_FILE = OUTPUT_DIR / "latest_scan.json"

OUTPUT_DIR.mkdir(exist_ok=True)

# Available scrapers
SCRAPERS = {
    "healthcare_brew": {
        "name": "Healthcare Brew",
        "class": HealthcareBrewScraper,
        "enabled": True,
    },
    "morning_brew": {
        "name": "Morning Brew",
        "class": MorningBrewScraper,
        "enabled": True,
    },
}

# Global state
scan_status = {
    "is_running": False,
    "current_newsletter": None,
    "progress": 0,
    "last_scan": None,
    "last_error": None,
    "total_advertisers": 0,
}

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    # Startup
    logger.info("Starting Newsletter Advertiser Intelligence System...")

    # Load last scan data if exists
    load_scan_data()

    # Start scheduler
    schedule_hour = int(os.getenv("SCAN_SCHEDULE_HOUR", "6"))  # Default 6 AM
    schedule_enabled = os.getenv("SCAN_SCHEDULE_ENABLED", "true").lower() == "true"

    if schedule_enabled:
        scheduler.add_job(
            run_scheduled_scan,
            CronTrigger(hour=schedule_hour, minute=0),
            id="daily_scan",
            name="Daily Newsletter Scan",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(f"Scheduler started - daily scan at {schedule_hour}:00 UTC")

    yield

    # Shutdown
    if scheduler.running:
        scheduler.shutdown()
    logger.info("Application shutdown complete")


app = FastAPI(
    title="Newsletter Advertiser Intelligence",
    description="Discover and track advertisers from competitor newsletters",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def load_scan_data():
    """Load the latest scan data from disk."""
    global scan_status

    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
                scan_status["last_scan"] = data.get("timestamp")
                scan_status["total_advertisers"] = len(data.get("advertisers", []))
        except Exception as e:
            logger.error(f"Error loading scan data: {e}")


def save_scan_data(advertisers: list[dict]):
    """Save scan data to disk."""
    data = {
        "timestamp": datetime.now().isoformat(),
        "advertisers": advertisers,
    }

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

    # Also save as CSV
    if advertisers:
        df = pd.DataFrame(advertisers)
        csv_path = OUTPUT_DIR / f"advertisers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.to_csv(csv_path, index=False)

        # Also update latest.csv
        df.to_csv(OUTPUT_DIR / "latest.csv", index=False)


def get_advertisers() -> list[dict]:
    """Get advertisers from the latest scan."""
    if not DATA_FILE.exists():
        return []

    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
            return data.get("advertisers", [])
    except Exception:
        return []


async def run_scan(newsletters: list[str] | None = None, limit: int | None = None):
    """Run a newsletter scan."""
    global scan_status

    if scan_status["is_running"]:
        raise HTTPException(400, "Scan already in progress")

    scan_status["is_running"] = True
    scan_status["progress"] = 0
    scan_status["last_error"] = None

    all_sponsors = []
    newsletters_to_scan = newsletters or [k for k, v in SCRAPERS.items() if v["enabled"]]

    try:
        categorizer = AdvertiserCategorizer()

        for i, newsletter_id in enumerate(newsletters_to_scan):
            if newsletter_id not in SCRAPERS:
                continue

            scan_status["current_newsletter"] = SCRAPERS[newsletter_id]["name"]
            scan_status["progress"] = int((i / len(newsletters_to_scan)) * 100)

            scraper_class = SCRAPERS[newsletter_id]["class"]

            # Run scraper (this is blocking, but we're in a background task)
            with scraper_class(headless=True) as scraper:
                sponsors = scraper.run_full_scan(limit=limit, show_progress=False)

                for sponsor in sponsors:
                    data = sponsor.to_dict()
                    data = categorizer.enrich_sponsor(data)
                    all_sponsors.append(data)

        # Deduplicate by domain
        seen = set()
        unique = []
        for s in all_sponsors:
            key = s.get("advertiser_domain") or s.get("advertiser_name", "").lower()
            if key not in seen:
                seen.add(key)
                unique.append(s)

        # Save results
        save_scan_data(unique)

        scan_status["last_scan"] = datetime.now().isoformat()
        scan_status["total_advertisers"] = len(unique)
        scan_status["progress"] = 100

        logger.info(f"Scan complete: found {len(unique)} unique advertisers")

    except Exception as e:
        logger.error(f"Scan error: {e}")
        scan_status["last_error"] = str(e)
        raise

    finally:
        scan_status["is_running"] = False
        scan_status["current_newsletter"] = None


async def run_scheduled_scan():
    """Run the scheduled daily scan."""
    logger.info("Starting scheduled scan...")
    try:
        await run_scan(limit=50)  # Limit to 50 issues per newsletter for scheduled runs
    except Exception as e:
        logger.error(f"Scheduled scan failed: {e}")


# ============== Web Routes ==============

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    advertisers = get_advertisers()

    # Calculate stats
    stats = {
        "total": len(advertisers),
        "high_fit": len([a for a in advertisers if "High" in a.get("niche_fit", "")]),
        "medium_fit": len([a for a in advertisers if "Medium" in a.get("niche_fit", "")]),
        "low_fit": len([a for a in advertisers if "Low" in a.get("niche_fit", "")]),
    }

    # Group by category
    categories = {}
    for a in advertisers:
        cat = a.get("category", "other")
        categories[cat] = categories.get(cat, 0) + 1

    # Group by source
    sources = {}
    for a in advertisers:
        src = a.get("source_newsletter", "unknown")
        sources[src] = sources.get(src, 0) + 1

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "categories": categories,
            "sources": sources,
            "advertisers": advertisers[:50],  # Show top 50
            "scan_status": scan_status,
            "newsletters": SCRAPERS,
        },
    )


@app.get("/advertisers", response_class=HTMLResponse)
async def advertisers_page(request: Request, fit: str | None = None, category: str | None = None):
    """Full advertisers list with filtering."""
    advertisers = get_advertisers()

    # Apply filters
    if fit:
        advertisers = [a for a in advertisers if fit.lower() in a.get("niche_fit", "").lower()]
    if category:
        advertisers = [a for a in advertisers if a.get("category") == category]

    return templates.TemplateResponse(
        "advertisers.html",
        {
            "request": request,
            "advertisers": advertisers,
            "filter_fit": fit,
            "filter_category": category,
            "scan_status": scan_status,
        },
    )


# ============== API Routes ==============

@app.get("/api/status")
async def api_status():
    """Get current scan status."""
    return {
        "status": "running" if scan_status["is_running"] else "idle",
        "current_newsletter": scan_status["current_newsletter"],
        "progress": scan_status["progress"],
        "last_scan": scan_status["last_scan"],
        "last_error": scan_status["last_error"],
        "total_advertisers": scan_status["total_advertisers"],
    }


@app.post("/api/scan")
async def api_start_scan(
    background_tasks: BackgroundTasks,
    newsletters: list[str] | None = None,
    limit: int | None = None,
):
    """
    Start a new scan.

    - **newsletters**: List of newsletter IDs to scan (default: all enabled)
    - **limit**: Maximum issues per newsletter (default: no limit)

    Returns immediately; check /api/status for progress.
    """
    if scan_status["is_running"]:
        raise HTTPException(400, detail="Scan already in progress")

    background_tasks.add_task(run_scan, newsletters, limit)

    return {
        "message": "Scan started",
        "newsletters": newsletters or list(SCRAPERS.keys()),
    }


@app.get("/api/advertisers")
async def api_get_advertisers(
    fit: str | None = None,
    category: str | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """
    Get advertisers with optional filtering.

    - **fit**: Filter by niche fit (high, medium, low)
    - **category**: Filter by category
    - **source**: Filter by source newsletter
    - **limit**: Max results (default 100)
    - **offset**: Pagination offset
    """
    advertisers = get_advertisers()

    # Apply filters
    if fit:
        advertisers = [a for a in advertisers if fit.lower() in a.get("niche_fit", "").lower()]
    if category:
        advertisers = [a for a in advertisers if a.get("category") == category]
    if source:
        advertisers = [a for a in advertisers if a.get("source_newsletter") == source]

    total = len(advertisers)
    advertisers = advertisers[offset:offset + limit]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "advertisers": advertisers,
    }


@app.get("/api/advertisers/export")
async def api_export_csv():
    """Download advertisers as CSV."""
    csv_path = OUTPUT_DIR / "latest.csv"

    if not csv_path.exists():
        raise HTTPException(404, "No scan data available")

    return FileResponse(
        csv_path,
        media_type="text/csv",
        filename=f"advertisers_{datetime.now().strftime('%Y%m%d')}.csv",
    )


@app.get("/api/newsletters")
async def api_get_newsletters():
    """Get available newsletter sources."""
    return {
        "newsletters": [
            {"id": k, "name": v["name"], "enabled": v["enabled"]}
            for k, v in SCRAPERS.items()
        ]
    }


@app.get("/api/schedule")
async def api_get_schedule():
    """Get scheduled jobs."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })

    return {"jobs": jobs}


@app.post("/api/schedule/run-now")
async def api_run_scheduled_now(background_tasks: BackgroundTasks):
    """Manually trigger the scheduled scan."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="Scan already in progress")

    background_tasks.add_task(run_scheduled_scan)
    return {"message": "Scheduled scan triggered"}


# ============== Health Check ==============

@app.get("/health")
async def health_check():
    """Health check endpoint for Railway/deployment."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


def start_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
