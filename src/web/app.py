"""
FastAPI web application for Newsletter Advertiser Intelligence System.

Provides:
- Web dashboard to view and trigger scans
- REST API for n8n/automation integration
- Background job scheduling
- Live progress logging
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd

from ..scrapers import HealthcareBrewScraper, MorningBrewScraper, SponsorInfo
from ..enrichment import AdvertiserCategorizer, ApolloEnricher, get_apollo_signup_instructions


logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent.parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = Path(__file__).parent / "templates"
DATA_FILE = OUTPUT_DIR / "latest_scan.json"

OUTPUT_DIR.mkdir(exist_ok=True)

# Thread pool for running sync Playwright code
executor = ThreadPoolExecutor(max_workers=2)

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
        "enabled": False,  # Disabled for now - focus on Healthcare Brew
    },
}

# Global state with live logs
scan_status = {
    "is_running": False,
    "current_newsletter": None,
    "current_action": None,
    "progress": 0,
    "issues_total": 0,
    "issues_scanned": 0,
    "advertisers_found": 0,
    "last_scan": None,
    "last_error": None,
    "total_advertisers": 0,
    "logs": [],  # Live log entries
}

# Lock for thread-safe status updates
status_lock = threading.Lock()

scheduler = AsyncIOScheduler()


def add_log(message: str, level: str = "info"):
    """Add a log entry to the live log."""
    with status_lock:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        scan_status["logs"].append(entry)
        # Keep only last 100 log entries
        if len(scan_status["logs"]) > 100:
            scan_status["logs"] = scan_status["logs"][-100:]

    # Also log to standard logger
    if level == "error":
        logger.error(message)
    else:
        logger.info(message)


def update_status(**kwargs):
    """Thread-safe status update."""
    with status_lock:
        scan_status.update(kwargs)


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
    executor.shutdown(wait=False)
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


def run_scan_sync(newsletters: list[str] | None = None, limit: int | None = None):
    """
    Run newsletter scan synchronously (called from thread pool).
    This runs Playwright in a separate thread to avoid async conflicts.
    """
    # Clear logs and reset status
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["progress"] = 0
        scan_status["last_error"] = None
        scan_status["advertisers_found"] = 0
        scan_status["issues_scanned"] = 0
        scan_status["issues_total"] = 0

    all_sponsors = []
    newsletters_to_scan = newsletters or [k for k, v in SCRAPERS.items() if v["enabled"]]

    add_log(f"Starting scan of {len(newsletters_to_scan)} newsletter(s)...")

    try:
        categorizer = AdvertiserCategorizer()

        for i, newsletter_id in enumerate(newsletters_to_scan):
            if newsletter_id not in SCRAPERS:
                continue

            newsletter_name = SCRAPERS[newsletter_id]["name"]
            update_status(
                current_newsletter=newsletter_name,
                current_action="Initializing browser",
                progress=int((i / len(newsletters_to_scan)) * 100),
            )

            add_log(f"📰 Scanning {newsletter_name}...")

            scraper_class = SCRAPERS[newsletter_id]["class"]

            try:
                add_log("🌐 Starting browser...")
                update_status(current_action="Starting browser")

                with scraper_class(headless=True) as scraper:
                    # Discover issues
                    add_log("🔍 Discovering newsletter issues...")
                    update_status(current_action="Discovering issues")

                    issues = scraper.discover_all_issues(limit=limit)

                    update_status(issues_total=len(issues))
                    add_log(f"📋 Found {len(issues)} issues to scan")

                    if not issues:
                        add_log("⚠️ No issues found", level="warning")
                        continue

                    # Scan each issue
                    for j, issue_url in enumerate(issues):
                        issue_num = j + 1
                        update_status(
                            current_action=f"Scanning issue {issue_num}/{len(issues)}",
                            issues_scanned=issue_num,
                            progress=int(((i + (j / len(issues))) / len(newsletters_to_scan)) * 100),
                        )

                        # Extract slug for display
                        slug = issue_url.split("/")[-1][:30]
                        add_log(f"  📄 [{issue_num}/{len(issues)}] {slug}...")

                        try:
                            sponsors = scraper.scrape_issue(issue_url)

                            if not sponsors:
                                add_log(f"    ⚪ No sponsors found in this issue")

                            for sponsor in sponsors:
                                data = sponsor.to_dict()
                                data = categorizer.enrich_sponsor(data, use_claude=False)
                                all_sponsors.append(data)

                                update_status(advertisers_found=len(all_sponsors))
                                add_log(f"    ✅ {sponsor.advertiser_name} @ {sponsor.advertiser_domain or '(no domain)'}")

                        except Exception as e:
                            add_log(f"    ❌ Error: {str(e)[:50]}", level="error")
                            continue

                add_log(f"✅ Finished {newsletter_name}")

            except Exception as e:
                add_log(f"❌ Error scanning {newsletter_name}: {e}", level="error")
                continue

        # Deduplicate by domain
        add_log("🔄 Deduplicating results...")
        update_status(current_action="Deduplicating results")

        seen = set()
        unique = []
        for s in all_sponsors:
            key = s.get("advertiser_domain") or s.get("advertiser_name", "").lower()
            if key not in seen:
                seen.add(key)
                unique.append(s)

        # Contact enrichment DISABLED - just get company info from newsletters
        # User will create separate email finder later
        add_log("ℹ️ Contact enrichment disabled - outputting company info only")

        # Save results
        add_log("💾 Saving results...")
        update_status(current_action="Saving results")
        save_scan_data(unique)

        update_status(
            last_scan=datetime.now().isoformat(),
            total_advertisers=len(unique),
            progress=100,
        )

        add_log(f"🎉 Scan complete! Found {len(unique)} unique advertisers")

        # Summary by fit
        high = len([a for a in unique if "High" in a.get("niche_fit", "")])
        medium = len([a for a in unique if "Medium" in a.get("niche_fit", "")])
        low = len([a for a in unique if "Low" in a.get("niche_fit", "")])
        add_log(f"   🟢 High fit: {high}  |  🟡 Medium: {medium}  |  🔴 Low: {low}")

    except Exception as e:
        add_log(f"❌ Scan failed: {e}", level="error")
        update_status(last_error=str(e))
        raise

    finally:
        update_status(
            is_running=False,
            current_newsletter=None,
            current_action=None,
        )


async def run_scan(newsletters: list[str] | None = None, limit: int | None = None):
    """Run a newsletter scan in a thread pool to avoid async/sync conflicts."""
    if scan_status["is_running"]:
        raise HTTPException(400, "Scan already in progress")

    # Run the synchronous scraper in a thread pool
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, run_scan_sync, newsletters, limit)


async def run_scheduled_scan():
    """Run the scheduled daily scan."""
    add_log("⏰ Starting scheduled daily scan...")
    try:
        await run_scan(limit=50)  # Limit to 50 issues per newsletter for scheduled runs
    except Exception as e:
        add_log(f"❌ Scheduled scan failed: {e}", level="error")


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
    """Get current scan status with live logs."""
    with status_lock:
        return {
            "status": "running" if scan_status["is_running"] else "idle",
            "current_newsletter": scan_status["current_newsletter"],
            "current_action": scan_status["current_action"],
            "progress": scan_status["progress"],
            "issues_total": scan_status["issues_total"],
            "issues_scanned": scan_status["issues_scanned"],
            "advertisers_found": scan_status["advertisers_found"],
            "last_scan": scan_status["last_scan"],
            "last_error": scan_status["last_error"],
            "total_advertisers": scan_status["total_advertisers"],
            "logs": scan_status["logs"][-50:],  # Last 50 log entries
        }


@app.get("/api/logs")
async def api_get_logs(since: int = 0):
    """Get logs since a specific index (for polling)."""
    with status_lock:
        logs = scan_status["logs"][since:]
        return {
            "logs": logs,
            "next_index": len(scan_status["logs"]),
            "is_running": scan_status["is_running"],
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


# ============== Apollo Enrichment ==============

@app.get("/api/apollo/status")
async def api_apollo_status():
    """Check if Apollo.io is configured."""
    apollo = ApolloEnricher()
    return {
        "configured": apollo.is_configured,
        "message": "Apollo.io is configured and ready" if apollo.is_configured
                   else "Apollo.io API key not set. Add APOLLO_API_KEY to environment variables.",
    }


@app.get("/api/apollo/setup")
async def api_apollo_setup():
    """Get Apollo.io setup instructions."""
    return {
        "instructions": get_apollo_signup_instructions(),
    }


@app.post("/api/enrich")
async def api_enrich_contacts(background_tasks: BackgroundTasks):
    """
    Enrich existing advertisers with contact information.
    Runs Apollo.io enrichment on advertisers that haven't been enriched yet.
    """
    apollo = ApolloEnricher()
    if not apollo.is_configured:
        raise HTTPException(400, detail="Apollo.io not configured. Set APOLLO_API_KEY environment variable.")

    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    # Get existing advertisers
    advertisers = get_advertisers()
    unenriched = [a for a in advertisers if not a.get("enriched")]

    if not unenriched:
        return {"message": "All advertisers already enriched", "count": 0}

    background_tasks.add_task(run_enrichment_only, unenriched)

    return {
        "message": f"Starting enrichment for {len(unenriched)} advertisers",
        "count": len(unenriched),
    }


def run_enrichment_only(advertisers: list[dict]):
    """Run contact enrichment only (no scraping)."""
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["current_action"] = "Enriching contacts"
        scan_status["progress"] = 0

    add_log(f"📇 Starting contact enrichment for {len(advertisers)} advertisers...")

    try:
        apollo = ApolloEnricher()
        enriched = []
        all_advertisers = get_advertisers()

        for idx, adv in enumerate(advertisers):
            name = adv.get("advertiser_name", "Unknown")
            update_status(
                current_action=f"Enriching {idx+1}/{len(advertisers)}",
                progress=int((idx / len(advertisers)) * 100),
            )

            add_log(f"  👤 [{idx+1}/{len(advertisers)}] {name}...")

            try:
                enriched_adv = apollo.enrich_advertiser(adv, max_contacts=3)
                contacts = enriched_adv.get("contacts_found", 0)
                if contacts > 0:
                    add_log(f"    ✅ Found {contacts} contact(s)")
                else:
                    add_log(f"    ⚪ No contacts found")
                enriched.append(enriched_adv)
            except Exception as e:
                add_log(f"    ❌ Error: {str(e)[:40]}", level="error")
                enriched.append(adv)

        # Merge enriched data back with all advertisers
        enriched_domains = {a.get("advertiser_domain") for a in enriched if a.get("advertiser_domain")}
        merged = []
        for adv in all_advertisers:
            domain = adv.get("advertiser_domain")
            if domain in enriched_domains:
                # Find the enriched version
                for e in enriched:
                    if e.get("advertiser_domain") == domain:
                        merged.append(e)
                        break
            else:
                merged.append(adv)

        save_scan_data(merged)
        stats = apollo.get_stats()
        add_log(f"🎉 Enrichment complete! {stats['contacts_found']} contacts, {stats['emails_verified']} verified, {stats['total_credits_used']} credits")

        update_status(progress=100, total_advertisers=len(merged))

    except Exception as e:
        add_log(f"❌ Enrichment failed: {e}", level="error")
        update_status(last_error=str(e))

    finally:
        update_status(is_running=False, current_action=None)


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
