#!/usr/bin/env python3
"""
Newsletter Advertiser Intelligence System - Main Entry Point

Scrapes competitor newsletters to discover advertisers, categorizes them,
and scores their fit for Renewal Weekly's audience.

Usage:
    python main.py                          # Scan all active newsletters
    python main.py --newsletter healthcare_brew --limit 10
    python main.py --domain newsletter.example.com --limit 20
    python main.py --list-newsletters
    python main.py --output my_advertisers.csv

For more options:
    python main.py --help
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.scrapers import HealthcareBrewScraper, MorningBrewScraper, GenericNewsletterScraper, SponsorInfo
from src.enrichment import AdvertiserCategorizer
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

    # Scan all newsletters
    python main.py

    # Scan a custom newsletter domain
    python main.py --domain morningbrew.com --limit 20

    # Scan with an archive URL
    python main.py --domain thehustle.co --archive-url https://thehustle.co/archive --limit 10

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
        help="Custom newsletter domain to scan (e.g., 'morningbrew.com')",
    )

    parser.add_argument(
        "--archive-url",
        type=str,
        help="Archive URL for custom domain (e.g., 'https://example.com/archive')",
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

    print("\n  + Any custom domain via --domain flag")
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


def scan_custom_domain(
    domain: str,
    archive_url: str | None = None,
    limit: int | None = None,
    headless: bool = True,
    timeout: int = 30000,
) -> list[SponsorInfo]:
    """
    Scan a custom newsletter domain for advertisers.

    Args:
        domain: Newsletter domain
        archive_url: Optional archive URL
        limit: Maximum issues to scan
        headless: Run browser headlessly
        timeout: Page load timeout

    Returns:
        List of discovered sponsors
    """
    logger.info(f"Starting scan of custom domain: {domain}...")

    with GenericNewsletterScraper(
        domain=domain,
        archive_url=archive_url,
        headless=headless,
        timeout=timeout,
    ) as scraper:
        issues = scraper.discover_all_issues(limit=limit)
        logger.info(f"Found {len(issues)} issues from {domain}")

        all_sponsors = []
        for i, issue_url in enumerate(issues):
            logger.info(f"  [{i+1}/{len(issues)}] {issue_url.split('/')[-1][:40]}...")
            sponsors = scraper.scrape_issue(issue_url)
            all_sponsors.extend(sponsors)
            if sponsors:
                for s in sponsors:
                    logger.info(f"    Found: {s.advertiser_name}")

    logger.info(f"Found {len(all_sponsors)} total sponsor mentions from {domain}")
    return all_sponsors


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
    # These match SponsorInfo.to_dict() + enrich_sponsor() output field names
    column_order = [
        "company_name",
        "domain",
        "sector",
        "niche_fit",
        "confidence",
        "sponsor_type",
        "ad_headline",
        "product_service",
        "call_to_action",
        "full_ad_copy",
        "source_newsletter",
        "issue_date",
        "issue_url",
        "landing_page_url",
    ]

    # Only include columns that exist, then append any extras
    existing = set(df.columns)
    columns = [c for c in column_order if c in existing]
    remaining = [c for c in df.columns if c not in columns]
    df = df[columns + remaining]

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

    # By sector
    if "sector" in df.columns:
        summary.append("\nBy Sector:")
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
            cat = row.get("sector", "?")
            fit = row.get("niche_fit", "?")
            summary.append(
                f"  - {row['company_name']} ({cat}) - {fit}"
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

    if args.domain:
        # Custom domain scan
        try:
            sponsors = scan_custom_domain(
                domain=args.domain,
                archive_url=args.archive_url,
                limit=args.limit,
                headless=not args.no_headless,
                timeout=args.timeout,
            )
            all_sponsors.extend(sponsors)
        except Exception as e:
            logger.error(f"Error scanning {args.domain}: {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()
            return 1
    else:
        # Built-in newsletter scan
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

    # Save to CSV
    saved_path = save_to_csv(enriched, output_path)

    # Print summary
    summary = generate_summary(enriched)
    print(summary)

    print(f"\nResults saved to: {saved_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
