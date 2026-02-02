"""
FastAPI web application for Newsletter Advertiser Intelligence System.

Provides:
- Web dashboard to view and trigger scans
- REST API for n8n/automation integration
- Background job scheduling
- Live progress logging
- Generic newsletter scanning (any domain)
- SQLite persistent storage
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

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd

from ..scrapers import HealthcareBrewScraper, MorningBrewScraper, SponsorInfo, GenericNewsletterScraper
from ..enrichment import AdvertiserCategorizer
from ..enrichment.website_scraper import WebsiteScraper
from ..storage.database import Database


logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent.parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = Path(__file__).parent / "templates"

OUTPUT_DIR.mkdir(exist_ok=True)

# Thread pool for running sync Playwright code
executor = ThreadPoolExecutor(max_workers=2)

# Available scrapers (built-in newsletter sources)
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
    "cancelled": False,
    "current_newsletter": None,
    "current_action": None,
    "progress": 0,
    "issues_total": 0,
    "issues_scanned": 0,
    "advertisers_found": 0,
    "last_scan": None,
    "last_error": None,
    "total_advertisers": 0,
    "logs": [],
}

# Lock for thread-safe status updates
status_lock = threading.Lock()

# Database instance
db = Database()

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
        if len(scan_status["logs"]) > 2000:
            scan_status["logs"] = scan_status["logs"][-2000:]

    if level == "error":
        logger.error(message)
    else:
        logger.info(message)


def update_status(**kwargs):
    """Thread-safe status update."""
    with status_lock:
        scan_status.update(kwargs)


def is_scan_cancelled() -> bool:
    """Check if scan has been cancelled (thread-safe)."""
    with status_lock:
        return scan_status.get("cancelled", False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    logger.info("Starting Newsletter Advertiser Intelligence System...")

    # Migrate from JSON files if database is empty
    if db.get_advertiser_count() == 0:
        db.import_from_json(OUTPUT_DIR)

    # Update total count
    update_status(total_advertisers=db.get_advertiser_count())

    # Start scheduler
    schedule_hour = int(os.getenv("SCAN_SCHEDULE_HOUR", "6"))
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

    if scheduler.running:
        scheduler.shutdown()
    executor.shutdown(wait=False)
    db.close()
    logger.info("Application shutdown complete")


app = FastAPI(
    title="Newsletter Advertiser Intelligence",
    description="Discover and track advertisers from competitor newsletters",
    version="2.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def save_csv_export(advertisers: list[dict]):
    """Save advertisers to CSV for download."""
    try:
        if advertisers:
            df = pd.DataFrame(advertisers)
            csv_path = OUTPUT_DIR / f"advertisers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(csv_path, index=False)
            df.to_csv(OUTPUT_DIR / "latest.csv", index=False)
            add_log(f"  Saved CSV to {csv_path.name}")
    except Exception as e:
        add_log(f"Failed to save CSV: {e}", level="error")


def run_scan_sync(newsletters: list[str] | None = None, limit: int | None = None):
    """
    Run newsletter scan synchronously (called from thread pool).
    """
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["cancelled"] = False
        scan_status["progress"] = 0
        scan_status["last_error"] = None
        scan_status["advertisers_found"] = 0
        scan_status["issues_scanned"] = 0
        scan_status["issues_total"] = 0

    all_sponsors = []
    newsletters_to_scan = newsletters or [k for k, v in SCRAPERS.items() if v["enabled"]]

    add_log(f"Starting scan of {len(newsletters_to_scan)} newsletter(s), limit={limit} issues...")

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

            add_log(f"Scanning {newsletter_name}...")

            scraper_class = SCRAPERS[newsletter_id]["class"]

            try:
                add_log("Starting browser...")
                update_status(current_action="Starting browser")

                with scraper_class(headless=True) as scraper:
                    add_log("Discovering newsletter issues...")
                    update_status(current_action="Discovering issues")

                    issues = scraper.discover_all_issues(limit=limit)

                    # Filter out already-scanned issues
                    scanned_issues = db.get_scanned_issues()
                    new_issues = [url for url in issues if url not in scanned_issues]
                    skipped_count = len(issues) - len(new_issues)

                    update_status(issues_total=len(new_issues))

                    if skipped_count > 0:
                        add_log(f"Found {len(issues)} issues, skipping {skipped_count} already scanned")
                    add_log(f"Scanning {len(new_issues)} new issues")

                    if not new_issues:
                        add_log("All issues already scanned - nothing new to process")
                        continue

                    for j, issue_url in enumerate(new_issues):
                        if is_scan_cancelled():
                            add_log("Scan cancelled by user")
                            break

                        issue_num = j + 1
                        update_status(
                            current_action=f"Scanning issue {issue_num}/{len(new_issues)}",
                            issues_scanned=issue_num,
                            progress=int(((i + (j / max(len(new_issues), 1))) / len(newsletters_to_scan)) * 100),
                        )

                        slug = issue_url.split("/")[-1][:30]
                        add_log(f"  [{issue_num}/{len(new_issues)}] {slug}...")

                        try:
                            sponsors = scraper.scrape_issue(issue_url)

                            if not sponsors:
                                add_log(f"    No sponsors found in this issue")

                            for sponsor in sponsors:
                                data = sponsor.to_dict()
                                data = categorizer.enrich_sponsor(data, use_claude=False)
                                all_sponsors.append(data)

                                update_status(advertisers_found=len(all_sponsors))
                                add_log(f"    Found: {sponsor.advertiser_name} @ {sponsor.advertiser_domain or '(no domain)'}")

                            db.mark_issue_scanned(issue_url, newsletter_id)

                        except Exception as e:
                            add_log(f"    Error: {str(e)[:50]}", level="error")
                            continue

                add_log(f"Finished {newsletter_name}")

            except Exception as e:
                add_log(f"Error scanning {newsletter_name}: {e}", level="error")
                continue

            if is_scan_cancelled():
                add_log("Scan cancelled by user")
                break

        # ===== PHASE 1 COMPLETE: Deduplicate sponsors =====
        add_log("Deduplicating sponsors...")
        update_status(current_action="Deduplicating")

        seen = set()
        unique = []
        for s in all_sponsors:
            key = s.get("domain") or s.get("company_name", "").lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(s)

        add_log(f"Phase 1 complete: {len(unique)} unique companies from {len(all_sponsors)} sponsor mentions")

        # Save Phase 1 results to database
        add_log("Saving Phase 1 results...")
        db.bulk_upsert_advertisers(unique)

        # ===== PHASE 2: Scrape company websites for contacts (PARALLEL) =====
        if unique:
            # Check which companies already have contacts
            existing_advertisers = db.get_advertisers()
            existing_contacts = {}
            for adv in existing_advertisers:
                domain = (adv.get("domain") or "").lower()
                if domain and adv.get("email_1"):
                    existing_contacts[domain] = adv

            to_scrape = []
            already_enriched = []
            for idx, company in enumerate(unique):
                domain = (company.get("domain") or "").lower()
                if domain in existing_contacts:
                    old = existing_contacts[domain]
                    for ci in range(1, 6):
                        company[f"email_{ci}"] = old.get(f"email_{ci}", "")
                        company[f"title_{ci}"] = old.get(f"title_{ci}", "")
                        company[f"name_{ci}"] = old.get(f"name_{ci}", "")
                    already_enriched.append(company)
                else:
                    to_scrape.append((idx, company))

            if already_enriched:
                add_log(f"Skipping {len(already_enriched)} companies with existing contacts")

            total_to_scrape = len(to_scrape)
            total = len(unique)
            add_log(f"Phase 2: Finding contact info for {total_to_scrape} new companies (parallel)...")
            if total_to_scrape == 0:
                add_log("  All companies already have contacts from previous scans!")
            update_status(current_action="Finding contacts")

            completed = [len(already_enriched)]
            results_lock = threading.Lock()
            DOMAIN_TIMEOUT = 120

            def scrape_company(idx_company):
                if is_scan_cancelled():
                    return

                idx, company = idx_company
                domain = company.get("domain")
                name = company.get("company_name", "Unknown")

                if not domain:
                    add_log(f"  [{idx+1}/{total}] {name} - no domain, skipping")
                    for ci in range(5):
                        company[f"email_{ci+1}"] = ""
                        company[f"title_{ci+1}"] = ""
                        company[f"name_{ci+1}"] = ""
                    return

                add_log(f"  [{idx+1}/{total}] {domain}...")

                scraper = None
                try:
                    scraper = WebsiteScraper(
                        timeout=10.0,
                        max_pages=10,
                        use_browser=True,
                        use_claude=True,
                        log_callback=add_log,
                        cancel_check=is_scan_cancelled,
                    )
                    result = scraper.scrape_domain(domain, company_name=name)

                    contact_count = 0
                    if result and result.contacts:
                        for ci, contact in enumerate(result.contacts[:5]):
                            if contact.email:
                                company[f"email_{ci+1}"] = contact.email
                                company[f"title_{ci+1}"] = contact.title or ""
                                company[f"name_{ci+1}"] = contact.name or ""
                                contact_count += 1

                    for ci in range(contact_count, 5):
                        company[f"email_{ci+1}"] = ""
                        company[f"title_{ci+1}"] = ""
                        company[f"name_{ci+1}"] = ""

                    if contact_count > 0:
                        add_log(f"    {domain}: Found {contact_count} contact(s)")
                    else:
                        add_log(f"    {domain}: No contacts found")
                        db.add_to_retry_queue(domain, name, "No contacts found", company.copy())

                except Exception as e:
                    add_log(f"    {domain}: {str(e)[:50]}", level="error")
                    db.add_to_retry_queue(domain, name, str(e)[:100], company.copy())
                    for ci in range(5):
                        company[f"email_{ci+1}"] = ""
                        company[f"title_{ci+1}"] = ""
                        company[f"name_{ci+1}"] = ""
                finally:
                    if scraper:
                        try:
                            scraper.close()
                            scraper.close_thread_browser()
                        except Exception:
                            pass

                    with results_lock:
                        completed[0] += 1
                        update_status(
                            current_action=f"Finding contacts ({completed[0]}/{total})",
                            progress=50 + int((completed[0] / max(total, 1)) * 50),
                        )

            from concurrent.futures import as_completed, wait, FIRST_COMPLETED, TimeoutError as FuturesTimeoutError
            import time

            MAX_PHASE2_TIME = max(720, total_to_scrape * 30)
            add_log(f"  Phase 2 timeout: {MAX_PHASE2_TIME}s for {total_to_scrape} companies")
            phase2_start = time.time()

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(scrape_company, (idx, company)): (idx, company)
                    for idx, company in to_scrape
                }
                pending = set(futures.keys())

                cancelled_during_phase2 = False
                timed_out = False

                while pending:
                    if is_scan_cancelled():
                        add_log("Stopping contact scraping due to cancellation...")
                        cancelled_during_phase2 = True
                        for f in pending:
                            f.cancel()
                        break

                    elapsed = time.time() - phase2_start
                    if elapsed > MAX_PHASE2_TIME:
                        add_log(f"Phase 2 time limit reached ({int(elapsed)}s), finishing up...", level="warning")
                        timed_out = True
                        for f in pending:
                            f.cancel()
                        for f in pending:
                            idx, company = futures[f]
                            domain = company.get("domain", "unknown")
                            name = company.get("company_name", "Unknown")
                            db.add_to_retry_queue(domain, name, "Phase 2 time limit reached", company.copy())
                            for ci in range(5):
                                company[f"email_{ci+1}"] = ""
                                company[f"title_{ci+1}"] = ""
                                company[f"name_{ci+1}"] = ""
                        break

                    remaining_time = max(10, MAX_PHASE2_TIME - elapsed)
                    task_timeout = min(DOMAIN_TIMEOUT, remaining_time)

                    done, pending = wait(pending, timeout=task_timeout, return_when=FIRST_COMPLETED)

                    if not done:
                        add_log(f"No tasks completed in {task_timeout}s, continuing...", level="warning")
                        continue

                    for future in done:
                        idx, company = futures[future]
                        domain = company.get("domain", "unknown")
                        name = company.get("company_name", "Unknown")
                        try:
                            future.result(timeout=1)
                        except FuturesTimeoutError:
                            add_log(f"    {domain}: Timed out", level="warning")
                            db.add_to_retry_queue(domain, name, f"Timeout after {DOMAIN_TIMEOUT}s", company.copy())
                            for ci in range(5):
                                company[f"email_{ci+1}"] = ""
                                company[f"title_{ci+1}"] = ""
                                company[f"name_{ci+1}"] = ""
                        except Exception as e:
                            add_log(f"    {domain}: {str(e)[:50]}", level="error")
                            db.add_to_retry_queue(domain, name, str(e)[:100], company.copy())

            if cancelled_during_phase2:
                add_log(f"Phase 2 stopped: processed {completed[0]}/{total} companies before cancellation")
            elif timed_out:
                add_log(f"Phase 2 timed out: processed {completed[0]}/{total} companies (remaining added to retry queue)")
            else:
                add_log(f"Phase 2 complete: processed {total} companies")

            # Save results to database
            add_log("Saving results with contacts...")
            update_status(current_action="Saving results")
            db.bulk_upsert_advertisers(unique)
            save_csv_export(db.get_advertisers())
        else:
            add_log("No companies found in Phase 1, skipping Phase 2")

        total_count = db.get_advertiser_count()
        update_status(
            last_scan=datetime.now().isoformat(),
            total_advertisers=total_count,
            progress=100,
        )

        with_emails = db.get_advertiser_count(with_email=True)
        if is_scan_cancelled():
            add_log(f"Scan stopped by user. {total_count} companies total, {with_emails} with contact info")
        else:
            add_log(f"Complete! {total_count} companies total, {with_emails} with contact info")

    except Exception as e:
        add_log(f"Scan failed: {e}", level="error")
        update_status(last_error=str(e))
        raise

    finally:
        update_status(
            is_running=False,
            cancelled=False,
            current_newsletter=None,
            current_action=None,
        )


async def run_scan(newsletters: list[str] | None = None, limit: int | None = None):
    """Run a newsletter scan in a thread pool."""
    if scan_status["is_running"]:
        raise HTTPException(400, "Scan already in progress")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, run_scan_sync, newsletters, limit)


async def run_scheduled_scan():
    """Run the scheduled daily scan."""
    add_log("Starting scheduled daily scan...")
    try:
        await run_scan(limit=50)
    except Exception as e:
        add_log(f"Scheduled scan failed: {e}", level="error")


# ============== Web Routes ==============

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    stats = db.get_stats()
    advertisers = db.get_advertisers({"limit": 50})

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "categories": stats.get("categories", {}),
            "sources": stats.get("sources", {}),
            "advertisers": advertisers,
            "scan_status": scan_status,
            "newsletters": SCRAPERS,
            "scanned_issues_count": stats.get("scanned_issues", 0),
        },
    )


@app.get("/advertisers", response_class=HTMLResponse)
async def advertisers_page(request: Request, fit: str | None = None, category: str | None = None, has_email: str | None = None):
    """Full advertisers list with filtering."""
    filters = {}
    if has_email == "yes":
        filters["has_email"] = True
    elif has_email == "no":
        filters["has_email"] = False
    if category:
        filters["sector"] = category

    advertisers = db.get_advertisers(filters)

    return templates.TemplateResponse(
        "advertisers.html",
        {
            "request": request,
            "advertisers": advertisers,
            "filter_fit": fit,
            "filter_category": category,
            "filter_has_email": has_email,
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
            "cancelled": scan_status.get("cancelled", False),
            "current_newsletter": scan_status["current_newsletter"],
            "current_action": scan_status["current_action"],
            "progress": scan_status["progress"],
            "issues_total": scan_status["issues_total"],
            "issues_scanned": scan_status["issues_scanned"],
            "advertisers_found": scan_status["advertisers_found"],
            "last_scan": scan_status["last_scan"],
            "last_error": scan_status["last_error"],
            "total_advertisers": scan_status["total_advertisers"],
            "logs": scan_status["logs"],
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


@app.get("/api/logs/text")
async def api_get_logs_text():
    """Get all logs as plain text."""
    with status_lock:
        lines = []
        for entry in scan_status["logs"]:
            lines.append(f"[{entry['time']}] {entry['message']}")
        return Response(content="\n".join(lines), media_type="text/plain")


@app.get("/api/logs/clear")
async def api_clear_logs():
    """Clear all logs."""
    with status_lock:
        scan_status["logs"] = []
    return {"status": "cleared"}


@app.post("/api/advertisers/clear")
async def api_clear_advertisers():
    """Clear all advertiser data."""
    try:
        db.clear_advertisers()
        db.clear_scanned_issues()

        latest_csv = OUTPUT_DIR / "latest.csv"
        if latest_csv.exists():
            latest_csv.unlink()

        with status_lock:
            scan_status["logs"] = []
            scan_status["total_advertisers"] = 0
            scan_status["advertisers_found"] = 0

        return {"status": "cleared", "message": "All advertiser data and scanned issues cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scanned-issues/clear")
async def api_clear_scanned_issues():
    """Clear scanned issues tracking (allows re-scanning)."""
    db.clear_scanned_issues()
    return {"status": "cleared", "message": "Scanned issues tracking cleared - all issues will be re-scanned"}


@app.get("/api/scanned-issues")
async def api_get_scanned_issues():
    """Get count of scanned issues."""
    return {"count": db.get_scanned_issues_count()}


@app.get("/api/dashboard-stats")
async def api_dashboard_stats():
    """Get live dashboard stats."""
    stats = db.get_stats()
    return {
        "total": stats["total"],
        "with_emails": stats["with_emails"],
        "without_emails": stats["without_emails"],
        "scanned_issues": stats["scanned_issues"],
    }


@app.post("/api/scan")
async def api_start_scan(
    background_tasks: BackgroundTasks,
    newsletters: list[str] | None = Query(default=None),
    limit: int | None = Query(default=None),
):
    """Start a new scan."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="Scan already in progress")

    background_tasks.add_task(run_scan, newsletters, limit)

    return {
        "message": "Scan started",
        "newsletters": newsletters or list(SCRAPERS.keys()),
    }


@app.post("/api/scan/cancel")
async def api_cancel_scan():
    """Cancel the currently running scan."""
    with status_lock:
        if not scan_status["is_running"]:
            raise HTTPException(400, detail="No scan is currently running")
        scan_status["cancelled"] = True
        scan_status["current_action"] = "Cancelling..."

    add_log("Cancel requested - stopping scan...")
    return {"message": "Cancel requested. Scan will stop shortly."}


@app.get("/api/advertisers")
async def api_get_advertisers(
    category: str | None = None,
    sponsor_type: str | None = None,
    has_email: bool | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Get advertisers with optional filtering."""
    filters = {}
    if has_email is not None:
        filters["has_email"] = has_email
    if category:
        filters["sector"] = category
    if sponsor_type:
        filters["sponsor_type"] = sponsor_type
    filters["limit"] = limit
    filters["offset"] = offset

    advertisers = db.get_advertisers(filters)
    total = db.get_advertiser_count()

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
        advertisers = db.get_advertisers()
        if not advertisers:
            raise HTTPException(404, "No scan data available")
        df = pd.DataFrame(advertisers)
        df.to_csv(csv_path, index=False)

    return FileResponse(
        csv_path,
        media_type="text/csv",
        filename=f"advertisers_{datetime.now().strftime('%Y%m%d')}.csv",
    )


@app.get("/api/newsletters")
async def api_get_newsletters():
    """Get available newsletter sources (built-in + custom)."""
    built_in = [
        {"id": k, "name": v["name"], "enabled": v["enabled"], "type": "built_in"}
        for k, v in SCRAPERS.items()
    ]

    custom = db.get_newsletter_sources()
    custom_list = [
        {
            "id": f"custom_{ns['domain']}",
            "name": ns["name"],
            "domain": ns["domain"],
            "archive_url": ns.get("archive_url"),
            "enabled": bool(ns.get("active", 1)),
            "type": "custom",
            "last_scanned": ns.get("last_scanned"),
            "total_issues": ns.get("total_issues_found", 0),
            "total_sponsors": ns.get("total_sponsors_found", 0),
        }
        for ns in custom
    ]

    return {"newsletters": built_in + custom_list}


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


# ============== Retry Queue Endpoints ==============

@app.get("/api/retry-queue")
async def get_retry_queue():
    """Get the current retry queue."""
    queue = db.get_retry_queue()
    return {"count": len(queue), "domains": queue}


@app.post("/api/retry-queue/run")
async def run_retry_queue(background_tasks: BackgroundTasks):
    """Process all domains in the retry queue."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    queue = db.get_retry_queue()
    if not queue:
        return {"message": "Retry queue is empty", "count": 0}

    background_tasks.add_task(process_retry_queue, queue)
    return {"message": f"Retrying {len(queue)} failed domains", "count": len(queue)}


@app.post("/api/retry-queue/clear")
async def api_clear_retry_queue():
    """Clear the retry queue."""
    db.clear_retry_queue()
    return {"message": "Retry queue cleared"}


@app.delete("/api/retry-queue/{domain}")
async def remove_from_queue(domain: str):
    """Remove a specific domain from retry queue."""
    db.remove_from_retry_queue(domain)
    return {"message": f"Removed {domain} from retry queue"}


def process_retry_queue(queue: list[dict]):
    """Process all domains in the retry queue."""
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["current_action"] = "Retrying failed domains"
        scan_status["progress"] = 0

    add_log(f"Retrying {len(queue)} failed domains...")

    try:
        total = len(queue)
        success_count = 0

        for idx, item in enumerate(queue):
            if is_scan_cancelled():
                add_log("Retry cancelled", level="warning")
                break

            domain = item["domain"]
            name = item["company_name"]
            company = item.get("company_data", {})

            update_status(
                current_action=f"Retrying ({idx+1}/{total}): {domain}",
                progress=int((idx / total) * 100),
            )

            add_log(f"  [{idx+1}/{total}] Retrying {domain}...")

            scraper = None
            try:
                scraper = WebsiteScraper(
                    timeout=5.0,
                    max_pages=10,
                    use_browser=True,
                    use_claude=True,
                    log_callback=add_log,
                )
                result = scraper.scrape_domain(domain, company_name=name)

                contact_count = 0
                if result and result.contacts:
                    for ci, contact in enumerate(result.contacts[:5]):
                        if contact.email:
                            company[f"email_{ci+1}"] = contact.email
                            company[f"title_{ci+1}"] = contact.title or ""
                            company[f"name_{ci+1}"] = contact.name or ""
                            contact_count += 1

                if contact_count > 0:
                    add_log(f"    {domain}: Found {contact_count} contact(s)")
                    db.remove_from_retry_queue(domain)
                    success_count += 1
                    company["domain"] = domain
                    company["company_name"] = name
                    db.upsert_advertiser(company)
                else:
                    add_log(f"    {domain}: Still no contacts")

            except Exception as e:
                add_log(f"    {domain}: {str(e)[:50]}", level="error")

            finally:
                if scraper:
                    try:
                        scraper.close()
                    except Exception:
                        pass

        remaining = len(db.get_retry_queue())
        add_log(f"Retry complete! {success_count}/{total} succeeded, {remaining} still pending")
        update_status(progress=100, total_advertisers=db.get_advertiser_count())

    except Exception as e:
        add_log(f"Retry failed: {e}", level="error")
        update_status(last_error=str(e))

    finally:
        update_status(is_running=False, current_action=None)


# ============== Custom Domains Endpoints (Company Contact Scanning) ==============

@app.get("/api/custom-domains")
async def get_custom_domains_list():
    """Get all custom domains (company type)."""
    domains = db.get_custom_domains(domain_type="company")
    return {"count": len(domains), "domains": domains}


@app.post("/api/custom-domains")
async def api_add_custom_domain(request: Request):
    """Add a custom company domain to scan for contacts."""
    data = await request.json()
    domain = (data.get("domain") or "").strip()
    company_name = (data.get("company_name") or "").strip() or None

    if not domain:
        raise HTTPException(400, detail="Domain is required")

    # Clean domain the same way the DB does, so we return the stored value
    if domain.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        domain = urlparse(domain).netloc
    if domain.startswith("www."):
        domain = domain[4:]

    if db.add_custom_domain(domain, company_name, domain_type="company"):
        return {"message": f"Added {domain}", "domain": domain}
    else:
        raise HTTPException(400, detail=f"{domain} already exists")


@app.delete("/api/custom-domains/{domain}")
async def api_remove_custom_domain(domain: str):
    """Remove a custom domain."""
    db.remove_custom_domain(domain)
    return {"message": f"Removed {domain}"}


@app.post("/api/custom-domains/scan")
async def scan_custom_domains(background_tasks: BackgroundTasks):
    """Scan custom company domains for contacts."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    domains = db.get_custom_domains(domain_type="company")
    unscanned = [d for d in domains if not d.get("scanned")]

    if not unscanned:
        return {"message": "All custom domains already scanned", "count": 0}

    background_tasks.add_task(process_custom_domains, unscanned)
    return {"message": f"Scanning {len(unscanned)} custom domains", "count": len(unscanned)}


@app.post("/api/custom-domains/{domain}/scan")
async def scan_single_custom_domain(domain: str, background_tasks: BackgroundTasks):
    """Scan a single custom company domain for contacts."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    # Find this domain in the database
    all_domains = db.get_custom_domains(domain_type="company")
    match = next((d for d in all_domains if d["domain"] == domain), None)

    if not match:
        raise HTTPException(404, detail=f"Domain {domain} not found")

    background_tasks.add_task(process_custom_domains, [match])
    return {"message": f"Scanning {domain}"}


def process_custom_domains(domains: list[dict]):
    """Scan custom company domains for contacts."""
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["current_action"] = "Scanning custom domains"
        scan_status["progress"] = 0

    add_log(f"Scanning {len(domains)} custom domains...")

    try:
        total = len(domains)

        for idx, item in enumerate(domains):
            if is_scan_cancelled():
                add_log("Scan cancelled", level="warning")
                break

            domain = item["domain"]
            name = item["company_name"]

            update_status(
                current_action=f"Scanning ({idx+1}/{total}): {domain}",
                progress=int((idx / total) * 100),
            )

            add_log(f"  [{idx+1}/{total}] {domain}...")

            company = {
                "domain": domain,
                "company_name": name,
                "source": "custom",
            }

            scraper = None
            try:
                scraper = WebsiteScraper(
                    timeout=5.0,
                    max_pages=10,
                    use_browser=True,
                    use_claude=True,
                    log_callback=add_log,
                )
                result = scraper.scrape_domain(domain, company_name=name)

                contact_count = 0
                if result and result.contacts:
                    for ci, contact in enumerate(result.contacts[:5]):
                        if contact.email:
                            company[f"email_{ci+1}"] = contact.email
                            company[f"title_{ci+1}"] = contact.title or ""
                            company[f"name_{ci+1}"] = contact.name or ""
                            contact_count += 1

                for ci in range(contact_count, 5):
                    company[f"email_{ci+1}"] = ""
                    company[f"title_{ci+1}"] = ""
                    company[f"name_{ci+1}"] = ""

                if contact_count > 0:
                    add_log(f"    {domain}: Found {contact_count} contact(s)")
                else:
                    add_log(f"    {domain}: No contacts found")

                db.upsert_advertiser(company)
                db.mark_custom_domain_scanned(domain)

            except Exception as e:
                add_log(f"    {domain}: {str(e)[:50]}", level="error")
                db.add_to_retry_queue(domain, name, str(e)[:100], company)

            finally:
                if scraper:
                    try:
                        scraper.close()
                    except Exception:
                        pass

        add_log(f"Custom domain scan complete!")
        update_status(progress=100, total_advertisers=db.get_advertiser_count())

    except Exception as e:
        add_log(f"Custom scan failed: {e}", level="error")
        update_status(last_error=str(e))

    finally:
        update_status(is_running=False, current_action=None)


# ============== Newsletter Source Scanning (scan any newsletter for sponsors) ==============

@app.get("/api/newsletter-sources")
async def get_newsletter_sources():
    """Get all custom newsletter sources."""
    sources = db.get_newsletter_sources(active_only=False)
    return {"count": len(sources), "sources": sources}


@app.post("/api/newsletter-sources")
async def api_add_newsletter_source(request: Request):
    """Add a custom newsletter domain to scan for sponsors."""
    data = await request.json()
    domain = (data.get("domain") or "").strip()
    name = (data.get("name") or "").strip()
    archive_url = (data.get("archive_url") or "").strip() or None

    if not domain:
        raise HTTPException(400, detail="Domain is required")

    # Clean domain
    if domain.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        domain = urlparse(domain).netloc
    if domain.startswith("www."):
        domain = domain[4:]

    if not name:
        name = domain.split(".")[0].replace("-", " ").title()

    if db.add_newsletter_source(name, domain, archive_url):
        return {"message": f"Added newsletter source: {name} ({domain})", "domain": domain}
    else:
        raise HTTPException(400, detail=f"Newsletter source {domain} already exists")


@app.post("/api/newsletter-sources/add-and-scan")
async def api_add_and_scan_newsletter(request: Request, background_tasks: BackgroundTasks):
    """Add a newsletter source and immediately start scanning it for sponsors.

    Accepts either a full archive URL or just a domain.
    The URL is used as the archive page to discover issues.
    """
    data = await request.json()
    url = (data.get("url") or "").strip()
    name = (data.get("name") or "").strip()
    limit = data.get("limit")

    if not url:
        raise HTTPException(400, detail="URL is required")

    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    # Parse the URL to get the domain and preserve the full URL as archive_url
    from urllib.parse import urlparse

    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    domain = parsed.netloc
    if domain.startswith("www."):
        domain = domain[4:]

    archive_url = url  # Use the full URL as the archive page

    if not name:
        name = domain.split(".")[0].replace("-", " ").title()

    # Add to database (ignore if already exists)
    db.add_newsletter_source(name, domain, archive_url)
    # Update archive URL in case it already existed with a different one
    db.update_newsletter_source(domain, archive_url=archive_url)

    # Build the source dict and start scanning
    source = {
        "name": name,
        "domain": domain,
        "archive_url": archive_url,
    }

    background_tasks.add_task(process_newsletter_sources, [source], limit)
    return {"message": f"Scanning {name} ({archive_url})", "domain": domain}


@app.delete("/api/newsletter-sources/{domain}")
async def api_remove_newsletter_source(domain: str):
    """Remove a newsletter source."""
    db.remove_newsletter_source(domain)
    return {"message": f"Removed newsletter source {domain}"}


@app.post("/api/newsletter-sources/scan")
async def scan_newsletter_sources(
    background_tasks: BackgroundTasks,
    limit: int | None = Query(default=None),
):
    """Scan custom newsletter sources for sponsors."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    sources = db.get_newsletter_sources()
    if not sources:
        return {"message": "No newsletter sources configured", "count": 0}

    background_tasks.add_task(process_newsletter_sources, sources, limit)
    return {"message": f"Scanning {len(sources)} newsletter source(s)", "count": len(sources)}


@app.post("/api/newsletter-sources/{domain}/scan")
async def scan_single_newsletter_source(
    domain: str,
    background_tasks: BackgroundTasks,
    limit: int | None = Query(default=None),
):
    """Scan a single newsletter source for sponsors."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    sources = db.get_newsletter_sources(active_only=False)
    source = next((s for s in sources if s["domain"] == domain), None)
    if not source:
        raise HTTPException(404, detail=f"Newsletter source {domain} not found")

    background_tasks.add_task(process_newsletter_sources, [source], limit)
    return {"message": f"Scanning {source['name']}"}


def process_newsletter_sources(sources: list[dict], limit: int | None = None):
    """Scan newsletter sources for sponsors using the generic scraper."""
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["cancelled"] = False
        scan_status["current_action"] = "Scanning newsletter sources"
        scan_status["progress"] = 0
        scan_status["advertisers_found"] = 0

    add_log(f"Scanning {len(sources)} newsletter source(s)...")

    try:
        categorizer = AdvertiserCategorizer()
        all_sponsors = []
        total_sources = len(sources)

        for si, source in enumerate(sources):
            if is_scan_cancelled():
                add_log("Scan cancelled by user")
                break

            source_name = source["name"]
            source_domain = source["domain"]
            archive_url = source.get("archive_url") or f"https://{source_domain}"

            update_status(
                current_newsletter=source_name,
                current_action="Starting browser",
                progress=int((si / total_sources) * 50),
            )

            add_log(f"Scanning newsletter: {source_name} ({source_domain})...")

            try:
                scraper = GenericNewsletterScraper(
                    domain=source_domain,
                    archive_url=archive_url,
                    name=source_name,
                    headless=True,
                    log_callback=add_log,
                    cancel_check=is_scan_cancelled,
                )

                with scraper:
                    add_log("Discovering issues...")
                    update_status(current_action="Discovering issues")

                    issues = scraper.discover_all_issues(limit=limit)

                    # Filter already scanned
                    scanned = db.get_scanned_issues()
                    new_issues = [url for url in issues if url not in scanned]
                    skipped = len(issues) - len(new_issues)

                    if skipped > 0:
                        add_log(f"Found {len(issues)} issues, skipping {skipped} already scanned")
                    add_log(f"Scanning {len(new_issues)} new issues")

                    update_status(issues_total=len(new_issues))

                    if not new_issues:
                        add_log("All issues already scanned")
                        continue

                    source_sponsors = []
                    for ji, issue_url in enumerate(new_issues):
                        if is_scan_cancelled():
                            break

                        issue_num = ji + 1
                        slug = issue_url.split("/")[-1][:40]
                        add_log(f"  [{issue_num}/{len(new_issues)}] {slug}...")
                        update_status(
                            current_action=f"Scanning issue {issue_num}/{len(new_issues)}",
                            issues_scanned=issue_num,
                        )

                        try:
                            sponsors = scraper.scrape_issue(issue_url)

                            if not sponsors:
                                add_log(f"    No sponsors found")

                            for sponsor in sponsors:
                                data = sponsor.to_dict()
                                data = categorizer.enrich_sponsor(data, use_claude=False)
                                source_sponsors.append(data)
                                all_sponsors.append(data)
                                update_status(advertisers_found=len(all_sponsors))
                                add_log(f"    Found: {sponsor.advertiser_name} @ {sponsor.advertiser_domain or '(no domain)'}")

                            db.mark_issue_scanned(issue_url, source_domain)

                        except Exception as e:
                            add_log(f"    Error: {str(e)[:50]}", level="error")
                            continue

                    # Update newsletter source stats
                    db.update_newsletter_source(
                        source_domain,
                        last_scanned=datetime.now().isoformat(),
                        total_issues_found=len(issues),
                        total_sponsors_found=len(source_sponsors),
                    )

                add_log(f"Finished {source_name}: {len(source_sponsors)} sponsors found")

            except Exception as e:
                add_log(f"Error scanning {source_name}: {e}", level="error")
                continue

        # Deduplicate and save
        if all_sponsors:
            add_log("Deduplicating sponsors...")
            seen = set()
            unique = []
            for s in all_sponsors:
                key = s.get("domain") or s.get("company_name", "").lower()
                if key and key not in seen:
                    seen.add(key)
                    unique.append(s)

            add_log(f"Found {len(unique)} unique companies from {len(all_sponsors)} sponsor mentions")
            db.bulk_upsert_advertisers(unique)
            save_csv_export(unique)

            # Phase 2: Contact scraping for new sponsors
            add_log(f"Phase 2: Finding contacts for new companies...")
            update_status(current_action="Finding contacts")

            # Only scrape companies we don't have contacts for yet
            existing = {a.get("domain"): a for a in db.get_advertisers() if a.get("email_1")}
            companies_needing_contacts = [c for c in unique if c.get("domain") and c["domain"] not in existing]

            for idx, company in enumerate(companies_needing_contacts):
                if is_scan_cancelled():
                    break

                domain = company.get("domain")
                name = company.get("company_name", "Unknown")

                add_log(f"  [{idx+1}/{len(companies_needing_contacts)}] {domain}...")
                update_status(
                    current_action=f"Finding contacts ({idx+1}/{len(companies_needing_contacts)})",
                    progress=50 + int((idx / max(len(companies_needing_contacts), 1)) * 50),
                )

                scraper = None
                try:
                    scraper = WebsiteScraper(
                        timeout=10.0,
                        max_pages=10,
                        use_browser=True,
                        use_claude=True,
                        log_callback=add_log,
                        cancel_check=is_scan_cancelled,
                    )
                    result = scraper.scrape_domain(domain, company_name=name)

                    contact_count = 0
                    if result and result.contacts:
                        for ci, contact in enumerate(result.contacts[:5]):
                            if contact.email:
                                company[f"email_{ci+1}"] = contact.email
                                company[f"title_{ci+1}"] = contact.title or ""
                                company[f"name_{ci+1}"] = contact.name or ""
                                contact_count += 1

                    for ci in range(contact_count, 5):
                        company[f"email_{ci+1}"] = ""
                        company[f"title_{ci+1}"] = ""
                        company[f"name_{ci+1}"] = ""

                    db.upsert_advertiser(company)

                    if contact_count > 0:
                        add_log(f"    {domain}: Found {contact_count} contact(s)")
                    else:
                        add_log(f"    {domain}: No contacts found")
                        db.add_to_retry_queue(domain, name, "No contacts found", company.copy())

                except Exception as e:
                    add_log(f"    {domain}: {str(e)[:50]}", level="error")
                    db.add_to_retry_queue(domain, name, str(e)[:100], company.copy())
                finally:
                    if scraper:
                        try:
                            scraper.close()
                            scraper.close_thread_browser()
                        except Exception:
                            pass

        total_count = db.get_advertiser_count()
        with_emails = db.get_advertiser_count(with_email=True)
        add_log(f"Newsletter source scan complete! {total_count} companies total, {with_emails} with contacts")
        update_status(progress=100, total_advertisers=total_count)

    except Exception as e:
        add_log(f"Newsletter source scan failed: {e}", level="error")
        update_status(last_error=str(e))

    finally:
        update_status(
            is_running=False,
            cancelled=False,
            current_newsletter=None,
            current_action=None,
        )


# ============== Health Check ==============

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


def start_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
