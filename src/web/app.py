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
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas as pd

from ..scrapers import HealthcareBrewScraper, MorningBrewScraper, GenericNewsletterScraper, SponsorInfo
from ..enrichment import AdvertiserCategorizer, ApolloEnricher, get_apollo_signup_instructions
from ..enrichment.website_scraper import WebsiteScraper
from ..enrichment.email_finder import EmailFinder


logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent.parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = Path(__file__).parent / "templates"
DATA_FILE = OUTPUT_DIR / "latest_scan.json"
RETRY_QUEUE_FILE = OUTPUT_DIR / "retry_queue.json"
CUSTOM_DOMAINS_FILE = OUTPUT_DIR / "custom_domains.json"
SCANNED_ISSUES_FILE = OUTPUT_DIR / "scanned_issues.json"

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
    "cancelled": False,  # Flag to signal scan cancellation
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
        # Keep last 500 log entries (increased for debugging)
        if len(scan_status["logs"]) > 500:
            scan_status["logs"] = scan_status["logs"][-500:]

    # Also log to standard logger
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


# ===== RETRY QUEUE MANAGEMENT =====
def load_retry_queue() -> list[dict]:
    """Load the retry queue from disk."""
    if RETRY_QUEUE_FILE.exists():
        try:
            return json.loads(RETRY_QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def save_retry_queue(queue: list[dict]):
    """Save the retry queue to disk."""
    RETRY_QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def add_to_retry_queue(domain: str, company_name: str, reason: str, company_data: dict | None = None):
    """Add a failed domain to the retry queue."""
    queue = load_retry_queue()

    # Check if already in queue
    existing = next((item for item in queue if item["domain"] == domain), None)
    if existing:
        existing["attempts"] = existing.get("attempts", 1) + 1
        existing["last_reason"] = reason
        existing["last_attempt"] = datetime.now().isoformat()
    else:
        queue.append({
            "domain": domain,
            "company_name": company_name,
            "reason": reason,
            "attempts": 1,
            "added": datetime.now().isoformat(),
            "last_attempt": datetime.now().isoformat(),
            "company_data": company_data,  # Preserve original data for retry
        })

    save_retry_queue(queue)


def remove_from_retry_queue(domain: str):
    """Remove a domain from retry queue (after successful retry)."""
    queue = load_retry_queue()
    queue = [item for item in queue if item["domain"] != domain]
    save_retry_queue(queue)


def clear_retry_queue():
    """Clear the entire retry queue."""
    save_retry_queue([])


# ===== CUSTOM DOMAINS MANAGEMENT =====
def load_custom_domains() -> list[dict]:
    """Load custom domains to scan."""
    if CUSTOM_DOMAINS_FILE.exists():
        try:
            return json.loads(CUSTOM_DOMAINS_FILE.read_text())
        except Exception:
            return []
    return []


def save_custom_domains(domains: list[dict]):
    """Save custom domains list."""
    CUSTOM_DOMAINS_FILE.write_text(json.dumps(domains, indent=2))


def add_custom_domain(domain: str, company_name: str | None = None):
    """Add a custom domain to scan."""
    domains = load_custom_domains()

    # Clean domain
    if domain.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        domain = urlparse(domain).netloc
    if domain.startswith("www."):
        domain = domain[4:]

    # Check if already exists
    if any(d["domain"] == domain for d in domains):
        return False

    domains.append({
        "domain": domain,
        "company_name": company_name or domain.split(".")[0].title(),
        "added": datetime.now().isoformat(),
        "scanned": False,
    })
    save_custom_domains(domains)
    return True


def remove_custom_domain(domain: str):
    """Remove a custom domain."""
    domains = load_custom_domains()
    domains = [d for d in domains if d["domain"] != domain]
    save_custom_domains(domains)


# ===== SCANNED ISSUES TRACKING =====
def load_scanned_issues() -> set[str]:
    """Load the set of already-scanned issue URLs."""
    if SCANNED_ISSUES_FILE.exists():
        try:
            data = json.loads(SCANNED_ISSUES_FILE.read_text())
            return set(data.get("issues", []))
        except Exception:
            return set()
    return set()


def save_scanned_issues(issues: set[str]):
    """Save the set of scanned issue URLs."""
    data = {
        "issues": list(issues),
        "last_updated": datetime.now().isoformat(),
    }
    SCANNED_ISSUES_FILE.write_text(json.dumps(data, indent=2))


def mark_issue_scanned(issue_url: str):
    """Mark a single issue as scanned."""
    issues = load_scanned_issues()
    issues.add(issue_url)
    save_scanned_issues(issues)


def clear_scanned_issues():
    """Clear all scanned issues (allows re-scanning everything)."""
    save_scanned_issues(set())


def get_scanned_issues_count() -> int:
    """Get count of scanned issues."""
    return len(load_scanned_issues())


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
    try:
        # Ensure output directory exists
        OUTPUT_DIR.mkdir(exist_ok=True)

        data = {
            "timestamp": datetime.now().isoformat(),
            "advertisers": advertisers,
        }

        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

        add_log(f"  Saved {len(advertisers)} advertisers to {DATA_FILE.name}")

        # Also save as CSV
        if advertisers:
            df = pd.DataFrame(advertisers)
            csv_path = OUTPUT_DIR / f"advertisers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(csv_path, index=False)

            # Also update latest.csv
            df.to_csv(OUTPUT_DIR / "latest.csv", index=False)
            add_log(f"  Saved CSV to {csv_path.name}")
        else:
            add_log("  No advertisers to save to CSV", level="warning")

    except Exception as e:
        add_log(f"❌ Failed to save scan data: {e}", level="error")
        logger.error(f"Save failed: {e}")
        raise


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
        scan_status["cancelled"] = False  # Reset cancellation flag
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

                    # Filter out already-scanned issues
                    scanned_issues = load_scanned_issues()
                    new_issues = [url for url in issues if url not in scanned_issues]
                    skipped_count = len(issues) - len(new_issues)

                    update_status(issues_total=len(new_issues))

                    if skipped_count > 0:
                        add_log(f"📋 Found {len(issues)} issues, skipping {skipped_count} already scanned")
                    add_log(f"📋 Scanning {len(new_issues)} new issues")

                    if not new_issues:
                        add_log("✅ All issues already scanned - nothing new to process")
                        continue

                    # Scan each NEW issue
                    for j, issue_url in enumerate(new_issues):
                        # Check for cancellation
                        if is_scan_cancelled():
                            add_log("⛔ Scan cancelled by user")
                            break

                        issue_num = j + 1
                        update_status(
                            current_action=f"Scanning issue {issue_num}/{len(new_issues)}",
                            issues_scanned=issue_num,
                            progress=int(((i + (j / max(len(new_issues), 1))) / len(newsletters_to_scan)) * 100),
                        )

                        # Extract slug for display
                        slug = issue_url.split("/")[-1][:30]
                        add_log(f"  📄 [{issue_num}/{len(new_issues)}] {slug}...")

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

                            # Mark issue as scanned (even if no sponsors found)
                            mark_issue_scanned(issue_url)

                        except Exception as e:
                            add_log(f"    ❌ Error: {str(e)[:50]}", level="error")
                            continue

                add_log(f"✅ Finished {newsletter_name}")

            except Exception as e:
                add_log(f"❌ Error scanning {newsletter_name}: {e}", level="error")
                continue

            # Check for cancellation between newsletters
            if is_scan_cancelled():
                add_log("⛔ Scan cancelled by user")
                break

        # ===== PHASE 1 COMPLETE: Deduplicate sponsors =====
        add_log("🔄 Deduplicating sponsors...")
        update_status(current_action="Deduplicating")

        seen = set()
        unique = []
        for s in all_sponsors:
            key = s.get("domain") or s.get("company_name", "").lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(s)

        add_log(f"📊 Phase 1 complete: {len(unique)} unique companies from {len(all_sponsors)} sponsor mentions")

        # Save Phase 1 results immediately (before Phase 2 which might hang/fail)
        # This ensures we always have SOMETHING saved even if Phase 2 crashes
        add_log("💾 Saving Phase 1 results...")
        save_scan_data(unique)

        # ===== PHASE 2: Scrape company websites for contacts (PARALLEL) =====
        if unique:
            add_log(f"🔍 Phase 2: Finding contact info for {len(unique)} companies (parallel)...")
            update_status(current_action="Finding contacts")

            total = len(unique)
            completed = [0]  # Use list to allow mutation in nested function
            results_lock = threading.Lock()

            # FIX: Increased from 25s to 120s to allow Deep Drill to finish
            DOMAIN_TIMEOUT = 120

            def scrape_company(idx_company):
                """Scrape a single company - runs in thread pool."""
                # Check for cancellation
                if is_scan_cancelled():
                    return

                idx, company = idx_company
                domain = company.get("domain")
                name = company.get("company_name", "Unknown")

                if not domain:
                    add_log(f"  ⚪ [{idx+1}/{total}] {name} - no domain, skipping")
                    for i in range(5):
                        company[f"email_{i+1}"] = ""
                        company[f"title_{i+1}"] = ""
                        company[f"name_{i+1}"] = ""
                    return

                add_log(f"  🌐 [{idx+1}/{total}] {domain}...")

                # Use a nested function with timeout tracking
                scraper = None
                try:
                    # FIX: Increased timeout from 2.5 to 10.0
                    scraper = WebsiteScraper(
                        timeout=10.0,
                        max_pages=10,
                        use_browser=True,
                        use_claude=True,
                        log_callback=add_log,
                        cancel_check=is_scan_cancelled,
                    )
                    result = scraper.scrape_domain(domain)

                    # Store contacts in separate columns
                    contact_count = 0
                    found_emails = set()
                    if result and result.contacts:
                        for i, contact in enumerate(result.contacts[:5]):
                            if contact.email:
                                company[f"email_{i+1}"] = contact.email
                                company[f"title_{i+1}"] = contact.title or ""
                                company[f"name_{i+1}"] = contact.name or ""
                                found_emails.add(contact.email.lower())
                                contact_count += 1

                    # Fallback: EmailFinder pattern generation + SMTP verification
                    if contact_count < 3:
                        try:
                            finder = EmailFinder(verify_smtp=True, timeout=3.0)
                            pattern_emails = finder.find_emails(domain, max_results=5, verify=True)
                            for fe in pattern_emails:
                                if fe.email and fe.email.lower() not in found_emails and contact_count < 5:
                                    contact_count += 1
                                    company[f"email_{contact_count}"] = fe.email
                                    company[f"title_{contact_count}"] = ""
                                    company[f"name_{contact_count}"] = ""
                                    found_emails.add(fe.email.lower())
                        except Exception as ef_err:
                            logger.debug(f"EmailFinder failed for {domain}: {ef_err}")

                    # Fill empty columns
                    for i in range(contact_count, 5):
                        company[f"email_{i+1}"] = ""
                        company[f"title_{i+1}"] = ""
                        company[f"name_{i+1}"] = ""

                    if contact_count > 0:
                        add_log(f"    ✅ {domain}: Found {contact_count} contact(s)")
                    else:
                        add_log(f"    ⚪ {domain}: No contacts found")
                        # Add to retry queue so user can retry later
                        add_to_retry_queue(domain, name, "No contacts found", company.copy())

                except Exception as e:
                    add_log(f"    ❌ {domain}: {str(e)[:50]}", level="error")
                    # Add to retry queue for later
                    add_to_retry_queue(domain, name, str(e)[:100], company.copy())
                    for i in range(5):
                        company[f"email_{i+1}"] = ""
                        company[f"title_{i+1}"] = ""
                        company[f"name_{i+1}"] = ""
                finally:
                    # Always close scraper to release browser resources
                    if scraper:
                        try:
                            scraper.close()
                        except Exception:
                            pass

                    with results_lock:
                        completed[0] += 1
                        update_status(
                            current_action=f"Finding contacts ({completed[0]}/{total})",
                            progress=50 + int((completed[0] / max(total, 1)) * 50),
                        )

            # Run in parallel with proper timeout handling
            from concurrent.futures import as_completed, wait, FIRST_COMPLETED, TimeoutError as FuturesTimeoutError
            import time

            # FIX: Increased total phase time to 15 minutes
            MAX_PHASE2_TIME = 900
            phase2_start = time.time()

            with ThreadPoolExecutor(max_workers=5) as pool:  # Increased to 5 workers for better throughput
                # Submit all tasks
                futures = {
                    pool.submit(scrape_company, (idx, company)): (idx, company)
                    for idx, company in enumerate(unique)
                }
                pending = set(futures.keys())

                # Process tasks as they complete, with overall time limit
                cancelled_during_phase2 = False
                timed_out = False

                while pending:
                    # Check for cancellation
                    if is_scan_cancelled():
                        add_log("⛔ Stopping contact scraping due to cancellation...")
                        cancelled_during_phase2 = True
                        for f in pending:
                            f.cancel()
                        break

                    # Check total time limit
                    elapsed = time.time() - phase2_start
                    if elapsed > MAX_PHASE2_TIME:
                        add_log(f"⏰ Phase 2 time limit reached ({int(elapsed)}s), finishing up...", level="warning")
                        timed_out = True
                        for f in pending:
                            f.cancel()
                        # Add remaining domains to retry queue
                        for f in pending:
                            idx, company = futures[f]
                            domain = company.get("domain", "unknown")
                            name = company.get("company_name", "Unknown")
                            add_log(f"    ⏰ {domain}: Skipped (time limit)", level="warning")
                            add_to_retry_queue(domain, name, "Phase 2 time limit reached", company.copy())
                            for i in range(5):
                                company[f"email_{i+1}"] = ""
                                company[f"title_{i+1}"] = ""
                                company[f"name_{i+1}"] = ""
                        break

                    # Wait for next task to complete (with per-task timeout)
                    remaining_time = max(10, MAX_PHASE2_TIME - elapsed)
                    task_timeout = min(DOMAIN_TIMEOUT, remaining_time)

                    done, pending = wait(pending, timeout=task_timeout, return_when=FIRST_COMPLETED)

                    if not done:
                        # No task completed within timeout - likely stuck
                        # This shouldn't happen often with proper timeouts in scrape_company
                        add_log(f"⚠️ No tasks completed in {task_timeout}s, continuing...", level="warning")
                        continue

                    for future in done:
                        idx, company = futures[future]
                        domain = company.get("domain", "unknown")
                        name = company.get("company_name", "Unknown")
                        try:
                            future.result(timeout=1)  # Should be instant since task is done
                        except FuturesTimeoutError:
                            add_log(f"    ⏰ {domain}: Timed out", level="warning")
                            add_to_retry_queue(domain, name, f"Timeout after {DOMAIN_TIMEOUT}s", company.copy())
                            for i in range(5):
                                company[f"email_{i+1}"] = ""
                                company[f"title_{i+1}"] = ""
                                company[f"name_{i+1}"] = ""
                        except Exception as e:
                            add_log(f"    ❌ {domain}: {str(e)[:50]}", level="error")
                            add_to_retry_queue(domain, name, str(e)[:100], company.copy())

            if cancelled_during_phase2:
                add_log(f"📊 Phase 2 stopped: processed {completed[0]}/{total} companies before cancellation")
            elif timed_out:
                add_log(f"📊 Phase 2 timed out: processed {completed[0]}/{total} companies (remaining added to retry queue)")
            else:
                add_log(f"📊 Phase 2 complete: processed {total} companies")

            # Save results with contacts (updates the Phase 1 save)
            add_log("💾 Saving results with contacts...")
            update_status(current_action="Saving results")
            save_scan_data(unique)
        else:
            add_log("⚠️ No companies found in Phase 1, skipping Phase 2")

        update_status(
            last_scan=datetime.now().isoformat(),
            total_advertisers=len(unique),
            progress=100,
        )

        # Summary - different message for cancelled vs completed
        with_emails = len([c for c in unique if c.get("email_1")])
        if is_scan_cancelled():
            add_log(f"⛔ Scan stopped by user. Saved {len(unique)} companies, {with_emails} with contact info")
            add_log("💡 You can start a new scan or use 'Retry Failed' to complete contact scraping")
        else:
            add_log(f"🎉 Complete! {len(unique)} companies, {with_emails} with contact info")

    except Exception as e:
        add_log(f"❌ Scan failed: {e}", level="error")
        update_status(last_error=str(e))
        raise

    finally:
        # Reset cancelled flag so next scan can start fresh
        update_status(
            is_running=False,
            cancelled=False,
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
        await run_scan(limit=365)  # Limit to 365 issues per newsletter for scheduled runs
    except Exception as e:
        add_log(f"❌ Scheduled scan failed: {e}", level="error")


# ============== Web Routes ==============

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    advertisers = get_advertisers()

    # Calculate stats
    with_emails = len([a for a in advertisers if a.get("email_1")])
    stats = {
        "total": len(advertisers),
        "with_emails": with_emails,
        "without_emails": len(advertisers) - with_emails,
        "high_fit": 0,  # Deprecated
        "medium_fit": 0,
        "low_fit": 0,
    }

    # Group by sector
    categories = {}
    for a in advertisers:
        cat = a.get("sector") or a.get("category", "other")
        categories[cat] = categories.get(cat, 0) + 1

    # Group by sponsor type
    sources = {}
    for a in advertisers:
        src = a.get("sponsor_type") or a.get("source_newsletter", "unknown")
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
            "scanned_issues_count": get_scanned_issues_count(),
        },
    )


@app.get("/advertisers", response_class=HTMLResponse)
async def advertisers_page(request: Request, fit: str | None = None, category: str | None = None, has_email: str | None = None):
    """Full advertisers list with filtering."""
    advertisers = get_advertisers()

    # Apply filters
    if has_email == "yes":
        advertisers = [a for a in advertisers if a.get("email_1")]
    elif has_email == "no":
        advertisers = [a for a in advertisers if not a.get("email_1")]
    if category:
        advertisers = [a for a in advertisers if a.get("sector") == category or a.get("category") == category]

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
            "logs": scan_status["logs"],  # Return ALL logs (up to 500)
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
    """Get all logs as plain text (for copying/debugging)."""
    with status_lock:
        lines = []
        for entry in scan_status["logs"]:
            level_marker = "❌" if entry["level"] == "error" else "⚠️" if entry["level"] == "warning" else ""
            lines.append(f"[{entry['time']}] {level_marker} {entry['message']}")
        return Response(
            content="\n".join(lines),
            media_type="text/plain",
        )


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
        # Clear the main data file
        if DATA_FILE.exists():
            DATA_FILE.unlink()

        # Also clear the latest CSV
        latest_csv = OUTPUT_DIR / "latest.csv"
        if latest_csv.exists():
            latest_csv.unlink()

        # Also clear scanned issues tracking (so they can be re-scanned)
        clear_scanned_issues()

        # Also clear logs
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
    clear_scanned_issues()
    return {"status": "cleared", "message": "Scanned issues tracking cleared - all issues will be re-scanned"}


@app.get("/api/scanned-issues")
async def api_get_scanned_issues():
    """Get count of scanned issues."""
    count = get_scanned_issues_count()
    return {"count": count}


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


@app.post("/api/scan/cancel")
async def api_cancel_scan():
    """
    Cancel the currently running scan.

    Sets a flag that the scan loop checks periodically.
    The scan will stop gracefully at the next check point.
    """
    with status_lock:
        if not scan_status["is_running"]:
            raise HTTPException(400, detail="No scan is currently running")

        scan_status["cancelled"] = True
        scan_status["current_action"] = "Cancelling..."

    add_log("⛔ Cancel requested - stopping scan...")

    return {"message": "Cancel requested. Scan will stop shortly."}


@app.post("/api/scan-domain")
async def api_scan_domain(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Scan any website domain for newsletter sponsors/advertisers.

    This uses the GenericNewsletterScraper which can discover archives
    and newsletter issues from any website, not just Healthcare/Morning Brew.

    Request body:
    - **domain**: Website domain to scan (e.g., "peterattiamd.com")
    - **limit**: Maximum issues to scan (default: 20)

    Returns immediately; check /api/status for progress.
    """
    if scan_status["is_running"]:
        raise HTTPException(400, detail="Scan already in progress")

    data = await request.json()
    domain = data.get("domain", "").strip()
    limit = data.get("limit") or None

    if not domain:
        raise HTTPException(400, detail="Domain is required")

    # Clean the domain
    if domain.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        domain = urlparse(domain).netloc
    if domain.startswith("www."):
        domain = domain[4:]

    background_tasks.add_task(run_domain_scan, domain, limit)

    return {
        "message": f"Scanning {domain} for newsletter sponsors",
        "domain": domain,
        "limit": limit,
    }


def run_domain_scan(domain: str, limit: int = None):
    """
    Run a generic domain scan using GenericNewsletterScraper.

    This discovers newsletter archives and issues from any website,
    then extracts sponsor/advertiser information.
    """
    # Clear logs and reset status
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

    add_log(f"Starting domain scan of {domain}, limit={limit} issues...")

    try:
        categorizer = AdvertiserCategorizer()

        update_status(
            current_newsletter=domain,
            current_action="Initializing browser",
            progress=5,
        )

        add_log(f"Scanning {domain}...")

        try:
            add_log("Starting browser...")
            update_status(current_action="Starting browser")

            with GenericNewsletterScraper(
                domain=domain,
                headless=True,
                log_callback=add_log,
            ) as scraper:
                # Discover issues
                add_log(f"Discovering newsletter issues from {domain}...")
                update_status(current_action="Discovering issues")

                issues = scraper.discover_all_issues(limit=limit)

                # Filter out already-scanned issues
                scanned_issues = load_scanned_issues()
                new_issues = [url for url in issues if url not in scanned_issues]
                skipped_count = len(issues) - len(new_issues)

                update_status(issues_total=len(new_issues))

                if skipped_count > 0:
                    add_log(f"Found {len(issues)} issues, skipping {skipped_count} already scanned")
                add_log(f"Scanning {len(new_issues)} new issues")

                if not new_issues:
                    add_log("No new issues found to scan")

                # Scan each issue
                for j, issue_url in enumerate(new_issues):
                    if is_scan_cancelled():
                        add_log("Scan cancelled by user")
                        break

                    issue_num = j + 1
                    update_status(
                        current_action=f"Scanning issue {issue_num}/{len(new_issues)}",
                        issues_scanned=issue_num,
                        progress=int((j / max(len(new_issues), 1)) * 50),
                    )

                    slug = issue_url.split("/")[-1][:40]
                    add_log(f"  [{issue_num}/{len(new_issues)}] {slug}...")

                    try:
                        sponsors = scraper.scrape_issue(issue_url)

                        if not sponsors:
                            add_log(f"    No sponsors found")

                        for sponsor in sponsors:
                            data = sponsor.to_dict()
                            data = categorizer.enrich_sponsor(data, use_claude=False)
                            all_sponsors.append(data)

                            update_status(advertisers_found=len(all_sponsors))
                            add_log(f"    Found: {sponsor.advertiser_name} @ {sponsor.advertiser_domain or '(no domain)'}")

                        mark_issue_scanned(issue_url)

                    except Exception as e:
                        add_log(f"    Error: {str(e)[:80]}", level="error")
                        continue

            add_log(f"Finished scanning {domain}")

        except Exception as e:
            add_log(f"Error scanning {domain}: {e}", level="error")

        # Deduplicate and filter self-domain
        add_log("Deduplicating sponsors...")
        update_status(current_action="Deduplicating")

        def _is_skip_domain(sponsor_domain: str, source_domain: str) -> bool:
            """Check if a sponsor domain should be filtered out."""
            if not sponsor_domain:
                return False
            sd = sponsor_domain.lower()
            # Self-domain check
            if sd == source_domain or source_domain in sd:
                return True
            if sd.replace("www.", "") == source_domain.replace("www.", ""):
                return True
            # Check if root domain is in the known skip list
            from ..scrapers.generic import GenericNewsletterScraper
            skip_set = GenericNewsletterScraper.SOCIAL_AND_UTILITY_DOMAINS
            parts = sd.split(".")
            for i in range(len(parts) - 1):
                root = ".".join(parts[i:])
                if root in skip_set:
                    return True
            return False

        seen = set()
        unique = []
        for s in all_sponsors:
            key = s.get("domain") or s.get("company_name", "").lower()
            if key and key not in seen:
                seen.add(key)
                if _is_skip_domain(s.get("domain", ""), domain):
                    continue
                unique.append(s)

        add_log(f"Phase 1 complete: {len(unique)} unique companies from {len(all_sponsors)} mentions")

        # Merge with existing data instead of overwriting
        # Also filter self-domain from existing data (cleans stale entries)
        existing = get_advertisers()
        existing = [a for a in existing if not _is_skip_domain(a.get("domain", ""), domain)]
        existing_domains = {a.get("domain") for a in existing if a.get("domain")}

        new_advertisers = []
        for company in unique:
            if company.get("domain") not in existing_domains:
                new_advertisers.append(company)
            else:
                # Update existing entry
                for e in existing:
                    if e.get("domain") == company.get("domain"):
                        e.update(company)
                        break

        existing.extend(new_advertisers)

        add_log("Saving results...")
        save_scan_data(existing)

        # Phase 2: Contact enrichment for new advertisers
        if new_advertisers:
            add_log(f"Phase 2: Finding contacts for {len(new_advertisers)} new companies...")
            update_status(current_action="Finding contacts")

            total = len(new_advertisers)
            completed = [0]
            results_lock = threading.Lock()
            DOMAIN_TIMEOUT = 120

            def scrape_company(idx_company):
                if is_scan_cancelled():
                    return

                idx, company = idx_company
                company_domain = company.get("domain")
                name = company.get("company_name", "Unknown")

                if not company_domain:
                    add_log(f"  [{idx+1}/{total}] {name} - no domain, skipping")
                    for i in range(5):
                        company[f"email_{i+1}"] = ""
                        company[f"title_{i+1}"] = ""
                        company[f"name_{i+1}"] = ""
                    return

                add_log(f"  [{idx+1}/{total}] {company_domain}...")

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
                    result = scraper.scrape_domain(company_domain)

                    contact_count = 0
                    found_emails = set()
                    if result and result.contacts:
                        for i, contact in enumerate(result.contacts[:5]):
                            if contact.email:
                                company[f"email_{i+1}"] = contact.email
                                company[f"title_{i+1}"] = contact.title or ""
                                company[f"name_{i+1}"] = contact.name or ""
                                found_emails.add(contact.email.lower())
                                contact_count += 1

                    # Fallback: EmailFinder pattern generation + SMTP verification
                    # This catches emails when website scraping fails (Cloudflare, JS-heavy)
                    if contact_count < 3:
                        try:
                            finder = EmailFinder(verify_smtp=True, timeout=3.0)
                            pattern_emails = finder.find_emails(company_domain, max_results=5, verify=True)
                            for fe in pattern_emails:
                                if fe.email and fe.email.lower() not in found_emails and contact_count < 5:
                                    contact_count += 1
                                    company[f"email_{contact_count}"] = fe.email
                                    company[f"title_{contact_count}"] = ""
                                    company[f"name_{contact_count}"] = ""
                                    found_emails.add(fe.email.lower())
                        except Exception as ef_err:
                            logger.debug(f"EmailFinder failed for {company_domain}: {ef_err}")

                    for i in range(contact_count, 5):
                        company[f"email_{i+1}"] = ""
                        company[f"title_{i+1}"] = ""
                        company[f"name_{i+1}"] = ""

                    if contact_count > 0:
                        add_log(f"    Found {contact_count} contact(s) for {company_domain}")
                    else:
                        add_log(f"    No contacts for {company_domain}")
                        add_to_retry_queue(company_domain, name, "No contacts found", company.copy())

                except Exception as e:
                    add_log(f"    Error for {company_domain}: {str(e)[:50]}", level="error")
                    add_to_retry_queue(company_domain, name, str(e)[:100], company.copy())
                    for i in range(5):
                        company[f"email_{i+1}"] = ""
                        company[f"title_{i+1}"] = ""
                        company[f"name_{i+1}"] = ""
                finally:
                    if scraper:
                        try:
                            scraper.close()
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

            MAX_PHASE2_TIME = 900
            phase2_start = time.time()

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {
                    pool.submit(scrape_company, (idx, company)): (idx, company)
                    for idx, company in enumerate(new_advertisers)
                }
                pending = set(futures.keys())

                while pending:
                    if is_scan_cancelled():
                        for f in pending:
                            f.cancel()
                        break

                    elapsed = time.time() - phase2_start
                    if elapsed > MAX_PHASE2_TIME:
                        add_log(f"Phase 2 time limit reached ({int(elapsed)}s)", level="warning")
                        for f in pending:
                            f.cancel()
                        break

                    remaining_time = max(10, MAX_PHASE2_TIME - elapsed)
                    task_timeout = min(DOMAIN_TIMEOUT, remaining_time)
                    done, pending = wait(pending, timeout=task_timeout, return_when=FIRST_COMPLETED)

                    for future in done:
                        try:
                            future.result(timeout=1)
                        except Exception:
                            pass

            add_log(f"Phase 2 complete: processed {completed[0]}/{total} companies")

            # Save final results
            add_log("Saving final results...")
            save_scan_data(existing)

        update_status(
            last_scan=datetime.now().isoformat(),
            total_advertisers=len(existing),
            progress=100,
        )

        with_emails = len([c for c in existing if c.get("email_1")])
        add_log(f"Done! {len(unique)} companies from {domain}, {with_emails} total with contacts")

    except Exception as e:
        add_log(f"Scan failed: {e}", level="error")
        update_status(last_error=str(e))

    finally:
        update_status(
            is_running=False,
            cancelled=False,
            current_newsletter=None,
            current_action=None,
        )


@app.get("/api/advertisers")
async def api_get_advertisers(
    category: str | None = None,
    sponsor_type: str | None = None,
    has_email: bool | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """
    Get advertisers with optional filtering.

    - **category**: Filter by sector/category
    - **sponsor_type**: Filter by sponsor type
    - **has_email**: Filter by whether has email (true/false)
    - **limit**: Max results (default 100)
    - **offset**: Pagination offset
    """
    advertisers = get_advertisers()

    # Apply filters
    if has_email is not None:
        if has_email:
            advertisers = [a for a in advertisers if a.get("email_1")]
        else:
            advertisers = [a for a in advertisers if not a.get("email_1")]
    if category:
        advertisers = [a for a in advertisers if a.get("sector") == category]
    if sponsor_type:
        advertisers = [a for a in advertisers if a.get("sponsor_type") == sponsor_type]

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


# ============== Retry Queue Endpoints ==============

@app.get("/api/retry-queue")
async def get_retry_queue():
    """Get the current retry queue."""
    queue = load_retry_queue()
    return {
        "count": len(queue),
        "domains": queue,
    }


@app.post("/api/retry-queue/run")
async def run_retry_queue(background_tasks: BackgroundTasks):
    """Process all domains in the retry queue."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    queue = load_retry_queue()
    if not queue:
        return {"message": "Retry queue is empty", "count": 0}

    background_tasks.add_task(process_retry_queue, queue)

    return {
        "message": f"Retrying {len(queue)} failed domains",
        "count": len(queue),
    }


@app.post("/api/retry-queue/clear")
async def api_clear_retry_queue():
    """Clear the retry queue."""
    clear_retry_queue()
    return {"message": "Retry queue cleared"}


@app.delete("/api/retry-queue/{domain}")
async def remove_from_queue(domain: str):
    """Remove a specific domain from retry queue."""
    remove_from_retry_queue(domain)
    return {"message": f"Removed {domain} from retry queue"}


def process_retry_queue(queue: list[dict]):
    """Process all domains in the retry queue."""
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["current_action"] = "Retrying failed domains"
        scan_status["progress"] = 0

    add_log(f"🔄 Retrying {len(queue)} failed domains...")

    try:
        total = len(queue)
        success_count = 0
        results = []

        for idx, item in enumerate(queue):
            if is_scan_cancelled():
                add_log("⚠️ Retry cancelled", level="warning")
                break

            domain = item["domain"]
            name = item["company_name"]
            company = item.get("company_data", {})

            update_status(
                current_action=f"Retrying ({idx+1}/{total}): {domain}",
                progress=int((idx / total) * 100),
            )

            add_log(f"  🔄 [{idx+1}/{total}] Retrying {domain}...")

            scraper = None
            try:
                scraper = WebsiteScraper(
                    timeout=10.0,
                    max_pages=10,
                    use_browser=True,
                    use_claude=True,
                    log_callback=add_log,
                )
                result = scraper.scrape_domain(domain)

                contact_count = 0
                found_emails = set()
                if result and result.contacts:
                    for i, contact in enumerate(result.contacts[:5]):
                        if contact.email:
                            company[f"email_{i+1}"] = contact.email
                            company[f"title_{i+1}"] = contact.title or ""
                            company[f"name_{i+1}"] = contact.name or ""
                            found_emails.add(contact.email.lower())
                            contact_count += 1

                # Fallback: EmailFinder pattern generation + SMTP verification
                if contact_count < 3:
                    try:
                        finder = EmailFinder(verify_smtp=True, timeout=3.0)
                        pattern_emails = finder.find_emails(domain, max_results=5, verify=True)
                        for fe in pattern_emails:
                            if fe.email and fe.email.lower() not in found_emails and contact_count < 5:
                                contact_count += 1
                                company[f"email_{contact_count}"] = fe.email
                                company[f"title_{contact_count}"] = ""
                                company[f"name_{contact_count}"] = ""
                                found_emails.add(fe.email.lower())
                    except Exception as ef_err:
                        logger.debug(f"EmailFinder failed for {domain}: {ef_err}")

                if contact_count > 0:
                    add_log(f"    ✅ {domain}: Found {contact_count} contact(s)")
                    # Remove from retry queue on success
                    remove_from_retry_queue(domain)
                    success_count += 1
                    company["domain"] = domain
                    company["company_name"] = name
                    results.append(company)
                else:
                    add_log(f"    ⚪ {domain}: Still no contacts")
                    # Update retry count but keep in queue
                    add_to_retry_queue(domain, name, "No contacts found on retry", company)

            except Exception as e:
                add_log(f"    ❌ {domain}: {str(e)[:50]}", level="error")
                add_to_retry_queue(domain, name, str(e)[:100], company)

            finally:
                if scraper:
                    try:
                        scraper.close()
                    except Exception:
                        pass

        # Merge results with existing data
        if results:
            existing = get_advertisers()
            existing_domains = {a.get("domain") for a in existing}

            for r in results:
                if r.get("domain") not in existing_domains:
                    existing.append(r)
                else:
                    # Update existing entry with new contact info
                    for e in existing:
                        if e.get("domain") == r.get("domain"):
                            e.update(r)
                            break

            save_scan_data(existing)

        remaining = len(load_retry_queue())
        add_log(f"🎉 Retry complete! {success_count}/{total} succeeded, {remaining} still pending")

        update_status(progress=100)

    except Exception as e:
        add_log(f"❌ Retry failed: {e}", level="error")
        update_status(last_error=str(e))

    finally:
        update_status(is_running=False, current_action=None)


# ============== Custom Domains Endpoints ==============

@app.get("/api/custom-domains")
async def get_custom_domains_list():
    """Get all custom domains."""
    domains = load_custom_domains()
    return {
        "count": len(domains),
        "domains": domains,
    }


@app.post("/api/custom-domains")
async def api_add_custom_domain(request: Request):
    """Add a custom domain to scan."""
    data = await request.json()
    domain = data.get("domain", "").strip()
    company_name = data.get("company_name", "").strip() or None

    if not domain:
        raise HTTPException(400, detail="Domain is required")

    if add_custom_domain(domain, company_name):
        return {"message": f"Added {domain}", "domain": domain}
    else:
        raise HTTPException(400, detail=f"{domain} already exists")


@app.delete("/api/custom-domains/{domain}")
async def api_remove_custom_domain(domain: str):
    """Remove a custom domain."""
    remove_custom_domain(domain)
    return {"message": f"Removed {domain}"}


@app.post("/api/custom-domains/scan")
async def scan_custom_domains(background_tasks: BackgroundTasks):
    """Scan all custom domains for contacts."""
    if scan_status["is_running"]:
        raise HTTPException(400, detail="A scan is already in progress")

    domains = load_custom_domains()
    unscanned = [d for d in domains if not d.get("scanned")]

    if not unscanned:
        return {"message": "All custom domains already scanned", "count": 0}

    background_tasks.add_task(process_custom_domains, unscanned)

    return {
        "message": f"Scanning {len(unscanned)} custom domains",
        "count": len(unscanned),
    }


def process_custom_domains(domains: list[dict]):
    """Scan custom domains for contacts."""
    with status_lock:
        scan_status["logs"] = []
        scan_status["is_running"] = True
        scan_status["current_action"] = "Scanning custom domains"
        scan_status["progress"] = 0

    add_log(f"🌐 Scanning {len(domains)} custom domains...")

    try:
        total = len(domains)
        results = []

        for idx, item in enumerate(domains):
            if is_scan_cancelled():
                add_log("⚠️ Scan cancelled", level="warning")
                break

            domain = item["domain"]
            name = item["company_name"]

            update_status(
                current_action=f"Scanning ({idx+1}/{total}): {domain}",
                progress=int((idx / total) * 100),
            )

            add_log(f"  🌐 [{idx+1}/{total}] {domain}...")

            company = {
                "domain": domain,
                "company_name": name,
                "source": "custom",
            }

            scraper = None
            try:
                scraper = WebsiteScraper(
                    timeout=10.0,
                    max_pages=10,
                    use_browser=True,
                    use_claude=True,
                    log_callback=add_log,
                )
                result = scraper.scrape_domain(domain)

                contact_count = 0
                found_emails = set()
                if result and result.contacts:
                    for i, contact in enumerate(result.contacts[:5]):
                        if contact.email:
                            company[f"email_{i+1}"] = contact.email
                            company[f"title_{i+1}"] = contact.title or ""
                            company[f"name_{i+1}"] = contact.name or ""
                            found_emails.add(contact.email.lower())
                            contact_count += 1

                # Fallback: EmailFinder pattern generation + SMTP verification
                if contact_count < 3:
                    try:
                        finder = EmailFinder(verify_smtp=True, timeout=3.0)
                        pattern_emails = finder.find_emails(domain, max_results=5, verify=True)
                        for fe in pattern_emails:
                            if fe.email and fe.email.lower() not in found_emails and contact_count < 5:
                                contact_count += 1
                                company[f"email_{contact_count}"] = fe.email
                                company[f"title_{contact_count}"] = ""
                                company[f"name_{contact_count}"] = ""
                                found_emails.add(fe.email.lower())
                    except Exception as ef_err:
                        logger.debug(f"EmailFinder failed for {domain}: {ef_err}")

                for i in range(contact_count, 5):
                    company[f"email_{i+1}"] = ""
                    company[f"title_{i+1}"] = ""
                    company[f"name_{i+1}"] = ""

                if contact_count > 0:
                    add_log(f"    ✅ {domain}: Found {contact_count} contact(s)")
                else:
                    add_log(f"    ⚪ {domain}: No contacts found")

                results.append(company)

                # Mark as scanned
                all_domains = load_custom_domains()
                for d in all_domains:
                    if d["domain"] == domain:
                        d["scanned"] = True
                        d["scanned_at"] = datetime.now().isoformat()
                        break
                save_custom_domains(all_domains)

            except Exception as e:
                add_log(f"    ❌ {domain}: {str(e)[:50]}", level="error")
                add_to_retry_queue(domain, name, str(e)[:100], company)

            finally:
                if scraper:
                    try:
                        scraper.close()
                    except Exception:
                        pass

        # Merge results with existing data
        if results:
            existing = get_advertisers()
            existing_domains = {a.get("domain") for a in existing}

            for r in results:
                if r.get("domain") not in existing_domains:
                    existing.append(r)

            save_scan_data(existing)

        with_emails = len([r for r in results if r.get("email_1")])
        add_log(f"🎉 Custom scan complete! {len(results)} domains, {with_emails} with contacts")

        update_status(progress=100, total_advertisers=len(get_advertisers()))

    except Exception as e:
        add_log(f"❌ Custom scan failed: {e}", level="error")
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
