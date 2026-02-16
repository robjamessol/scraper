#!/usr/bin/env python3
"""
Newsletter Advertiser Intelligence System - Main Entry Point

Scrapes competitor newsletters to discover advertisers, categorizes them,
and scores their fit for Renewal Weekly's audience.

Usage:
    python main.py                          # Scan all active newsletters
    python main.py --newsletter healthcare_brew --limit 10
    python main.py --list-newsletters
    python main.py --output my_advertisers.csv

For more options:
    python main.py --help
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.scrapers import HealthcareBrewScraper, MorningBrewScraper, GenericNewsletterScraper, SponsorInfo
from src.enrichment import AdvertiserCategorizer, WebsiteScraper, EmailFinder
from src.utils.config import load_config, get_env


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# Available scrapers
SCRAPERS = {
    "healthcare_brew": HealthcareBrewScraper,
    "morning_brew": MorningBrewScraper,
}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Newsletter Advertiser Intelligence System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Scan Healthcare Brew (first 10 issues for testing)
    python main.py --newsletter healthcare_brew --limit 10

    # Scan any website domain for newsletter sponsors
    python main.py --domain peterattiamd.com --limit 10

    # Scan all newsletters
    python main.py

    # Save to custom output file
    python main.py --output my_advertisers.csv

    # Run with visible browser (for debugging)
    python main.py --no-headless --limit 5
        """,
    )

    parser.add_argument(
        "--newsletter", "-n",
        type=str,
        choices=list(SCRAPERS.keys()),
        help="Specific newsletter to scan (default: all active)",
    )

    parser.add_argument(
        "--domain", "-d",
        type=str,
        default=None,
        help="Scan any website domain for newsletter sponsors (e.g., peterattiamd.com)",
    )

    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Maximum number of issues to scan per newsletter",
    )

    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output CSV file path (default: output/advertisers_YYYYMMDD.csv)",
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible mode (useful for debugging)",
    )

    parser.add_argument(
        "--list-newsletters",
        action="store_true",
        help="List available newsletters and exit",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="Page load timeout in milliseconds (default: 30000)",
    )

    parser.add_argument(
        "--skip-emails",
        action="store_true",
        help="Skip email enrichment (faster scan, no contact lookup)",
    )

    return parser.parse_args()


def list_newsletters():
    """List available newsletter scrapers."""
    print("\nAvailable Newsletter Sources:")
    print("-" * 50)

    for key, scraper_class in SCRAPERS.items():
        # Get default config
        config = scraper_class._default_config()
        print(f"\n  {key}:")
        print(f"    Name: {config.get('name', 'Unknown')}")
        print(f"    Archive: {config.get('archive_url', 'N/A')}")
        print(f"    Active: {config.get('active', True)}")

    print("\n")


def scan_newsletter(
    newsletter_id: str,
    limit: int | None = None,
    headless: bool = True,
    timeout: int = 30000,
) -> list[SponsorInfo]:
    """
    Scan a single newsletter for advertisers.

    Args:
        newsletter_id: Newsletter identifier
        limit: Maximum issues to scan
        headless: Run browser headlessly
        timeout: Page load timeout

    Returns:
        List of discovered sponsors
    """
    if newsletter_id not in SCRAPERS:
        raise ValueError(f"Unknown newsletter: {newsletter_id}")

    scraper_class = SCRAPERS[newsletter_id]

    logger.info(f"Starting scan of {newsletter_id}...")

    with scraper_class(headless=headless, timeout=timeout) as scraper:
        sponsors = scraper.run_full_scan(limit=limit, show_progress=True)

    logger.info(f"Found {len(sponsors)} unique advertisers from {newsletter_id}")
    return sponsors


def enrich_sponsors(sponsors: list[SponsorInfo]) -> list[dict]:
    """
    Add categorization and niche fit scoring to sponsors.

    Args:
        sponsors: List of SponsorInfo objects

    Returns:
        List of enriched sponsor dictionaries
    """
    categorizer = AdvertiserCategorizer()
    enriched = []

    for sponsor in sponsors:
        data = sponsor.to_dict()
        data = categorizer.enrich_sponsor(data)
        enriched.append(data)

    return enriched


def _find_emails_for_domain(domain: str) -> list[str]:
    """
    Find contact emails for a single domain using multiple strategies.

    Strategy 1: Scrape the company website (HTTP only, fast) for real emails
    Strategy 2: Generate common business email patterns + SMTP verify

    Returns up to 3 emails, ordered by quality (real > verified pattern > unverified).
    """
    if not domain:
        return []

    emails = []
    seen = set()

    # Strategy 1: Quick HTTP website scrape (no browser, no AI — fast)
    try:
        scraper = WebsiteScraper(
            timeout=5.0,
            max_pages=5,
            use_browser=False,
            use_claude=False,
        )
        result = scraper.scrape_domain(domain)
        scraper.close()
        if result and result.contacts:
            for contact in result.contacts:
                if contact.email and contact.email.lower() not in seen:
                    seen.add(contact.email.lower())
                    emails.append(contact.email)
    except Exception as e:
        logger.debug(f"Website scrape failed for {domain}: {e}")

    # Strategy 2: Pattern generation + SMTP verification
    if len(emails) < 3:
        try:
            finder = EmailFinder(verify_smtp=True, timeout=3.0)
            found = finder.find_emails(domain, max_results=5, verify=True)
            for fe in found:
                if fe.email and fe.email.lower() not in seen:
                    seen.add(fe.email.lower())
                    emails.append(fe.email)
                    if len(emails) >= 3:
                        break
        except Exception as e:
            logger.debug(f"Email pattern search failed for {domain}: {e}")

    return emails[:3]


def find_sponsor_emails(sponsors: list[dict], verbose: bool = False) -> list[dict]:
    """
    Find contact emails for all sponsor domains in parallel.

    For each unique domain, scrapes the website and generates/verifies
    email patterns. Merges results back into each sponsor dict.

    Args:
        sponsors: List of enriched sponsor dicts
        verbose: Show per-domain progress

    Returns:
        Same list with contact_email, contact_email_2, contact_email_3 added
    """
    # Collect unique domains
    unique_domains = set()
    for s in sponsors:
        domain = s.get("domain")
        if domain:
            unique_domains.add(domain)

    if not unique_domains:
        return sponsors

    logger.info(f"Finding contact emails for {len(unique_domains)} unique domains...")

    # Parallel email lookup
    domain_emails: dict[str, list[str]] = {}

    def _lookup(domain: str) -> tuple[str, list[str]]:
        if verbose:
            logger.info(f"  Looking up emails for {domain}...")
        result = _find_emails_for_domain(domain)
        if verbose and result:
            logger.info(f"  Found {len(result)} email(s) for {domain}: {', '.join(result)}")
        return domain, result

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_lookup, d): d for d in unique_domains}
        for future in as_completed(futures):
            try:
                domain, found_emails = future.result(timeout=30)
                domain_emails[domain] = found_emails
            except Exception as e:
                domain = futures[future]
                logger.debug(f"Email lookup failed for {domain}: {e}")
                domain_emails[domain] = []

    # Merge emails into sponsor dicts
    found_count = sum(1 for emails in domain_emails.values() if emails)
    logger.info(f"Found emails for {found_count}/{len(unique_domains)} domains")

    for sponsor in sponsors:
        domain = sponsor.get("domain")
        emails = domain_emails.get(domain, [])
        sponsor["contact_email"] = emails[0] if len(emails) > 0 else ""
        sponsor["contact_email_2"] = emails[1] if len(emails) > 1 else ""
        sponsor["contact_email_3"] = emails[2] if len(emails) > 2 else ""

    return sponsors


def save_to_csv(sponsors: list[dict], output_path: str) -> str:
    """
    Save sponsor data to CSV file.

    Args:
        sponsors: List of sponsor dictionaries
        output_path: Path to save CSV

    Returns:
        Path to saved file
    """
    # Ensure output directory exists
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Create DataFrame
    df = pd.DataFrame(sponsors)

    # Reorder columns for better readability
    column_order = [
        "company_name",
        "domain",
        "contact_email",
        "contact_email_2",
        "contact_email_3",
        "source_newsletter",
        "sector",
        "niche_fit",
        "sponsor_type",
        "confidence",
        "product_service",
        "ad_copy_snippet",
        "landing_page_url",
        "issue_date",
        "issue_url",
        "sponsor_url",
    ]

    # Only include columns that exist
    columns = [c for c in column_order if c in df.columns]
    df = df[columns]

    # Save to CSV
    df.to_csv(output_path, index=False)
    logger.info(f"Saved {len(df)} advertisers to {output_path}")

    return str(output_path)


def generate_summary(sponsors: list[dict]) -> str:
    """Generate a summary report of the scan results."""
    if not sponsors:
        return "No advertisers found."

    df = pd.DataFrame(sponsors)

    summary = []
    summary.append("\n" + "=" * 60)
    summary.append("SCAN SUMMARY")
    summary.append("=" * 60)

    # Total count
    summary.append(f"\nTotal unique advertisers: {len(df)}")

    # By source newsletter
    summary.append("\nBy Source Newsletter:")
    for source, count in df["source_newsletter"].value_counts().items():
        summary.append(f"  - {source}: {count}")

    # By category
    if "sector" in df.columns:
        summary.append("\nBy Category:")
        for cat, count in df["sector"].value_counts().items():
            summary.append(f"  - {cat}: {count}")

    # By niche fit
    if "niche_fit" in df.columns:
        summary.append("\nBy Niche Fit:")
        for fit, count in df["niche_fit"].value_counts().items():
            summary.append(f"  - {fit}: {count}")

    # Top advertisers by confidence
    if "confidence" in df.columns:
        summary.append("\nTop High-Confidence Advertisers:")
        high_conf = df[df["confidence"] == "high"].head(10)
        for _, row in high_conf.iterrows():
            summary.append(
                f"  - {row['company_name']} ({row.get('sector', 'other')}) - {row.get('product_service', 'N/A')}"
            )

    # High-fit advertisers
    if "niche_fit" in df.columns:
        high_fit = df[df["niche_fit"].str.contains("High", na=False)]
        if len(high_fit) > 0:
            summary.append(f"\nHigh-Fit Advertisers ({len(high_fit)} total):")
            for _, row in high_fit.head(10).iterrows():
                summary.append(f"  - {row['company_name']} ({row['domain']})")

    # Email stats
    if "contact_email" in df.columns:
        with_email = df[df["contact_email"].astype(str).str.len() > 0]
        summary.append(f"\nContact Emails Found: {len(with_email)}/{len(df)} sponsors")
        if len(with_email) > 0:
            summary.append("\nSponsors with emails:")
            for _, row in with_email.head(15).iterrows():
                emails = [row.get("contact_email", "")]
                if row.get("contact_email_2"):
                    emails.append(row["contact_email_2"])
                if row.get("contact_email_3"):
                    emails.append(row["contact_email_3"])
                email_str = ", ".join(e for e in emails if e)
                summary.append(
                    f"  - {row['company_name']} ({row['domain']}) "
                    f"[from: {row.get('source_newsletter', 'N/A')}] -> {email_str}"
                )

    summary.append("\n" + "=" * 60)

    return "\n".join(summary)


def main():
    """Main entry point."""
    args = parse_args()

    # Set log level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Handle list command
    if args.list_newsletters:
        list_newsletters()
        return 0

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"output/advertisers_{timestamp}.csv"

    # Scan newsletters
    all_sponsors = []

    # If --domain is specified, use the GenericNewsletterScraper
    if args.domain:
        logger.info(f"Scanning domain: {args.domain}")
        try:
            with GenericNewsletterScraper(
                domain=args.domain,
                headless=not args.no_headless,
                timeout=args.timeout,
            ) as scraper:
                sponsors = scraper.run_full_scan(limit=args.limit, show_progress=True)
                all_sponsors.extend(sponsors)
        except Exception as e:
            logger.error(f"Error scanning domain {args.domain}: {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()
    else:
        # Determine which newsletters to scan
        if args.newsletter:
            newsletters_to_scan = [args.newsletter]
        else:
            newsletters_to_scan = list(SCRAPERS.keys())

        for newsletter_id in newsletters_to_scan:
            try:
                sponsors = scan_newsletter(
                    newsletter_id,
                    limit=args.limit,
                    headless=not args.no_headless,
                    timeout=args.timeout,
                )
                all_sponsors.extend(sponsors)

            except Exception as e:
                logger.error(f"Error scanning {newsletter_id}: {e}")
                if args.verbose:
                    import traceback
                    traceback.print_exc()
                continue

    if not all_sponsors:
        logger.warning("No advertisers found!")
        return 1

    # Enrich with categorization and scoring
    logger.info("Enriching sponsors with categorization and niche fit scoring...")
    enriched = enrich_sponsors(all_sponsors)

    # Find contact emails for sponsors
    if not args.skip_emails:
        enriched = find_sponsor_emails(enriched, verbose=args.verbose)
    else:
        logger.info("Skipping email enrichment (--skip-emails)")

    # Save to CSV
    saved_path = save_to_csv(enriched, output_path)

    # Print summary
    summary = generate_summary(enriched)
    print(summary)

    print(f"\nResults saved to: {saved_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
