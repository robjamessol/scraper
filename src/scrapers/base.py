"""Base scraper class for newsletter advertiser discovery."""

import re
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlaywrightTimeout
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from ..utils.helpers import (
    extract_domain,
    clean_text,
    normalize_company_name,
    truncate_text,
    extract_links_from_html,
    is_tracking_link,
)


logger = logging.getLogger(__name__)


@dataclass
class SponsorInfo:
    """Information about a discovered sponsor/advertiser."""

    advertiser_name: str
    advertiser_domain: str | None
    placement_type: str
    ad_copy_snippet: str
    issue_url: str
    issue_date: str | None
    source_newsletter: str
    category: str = "other"
    niche_fit: str = "Medium"
    confidence: str = "medium"
    sponsor_url: str | None = None
    raw_html: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for DataFrame/CSV export."""
        return {
            "advertiser_name": self.advertiser_name,
            "advertiser_domain": self.advertiser_domain,
            "placement_type": self.placement_type,
            "ad_copy_snippet": self.ad_copy_snippet,
            "issue_url": self.issue_url,
            "issue_date": self.issue_date,
            "source_newsletter": self.source_newsletter,
            "category": self.category,
            "niche_fit": self.niche_fit,
            "confidence": self.confidence,
            "sponsor_url": self.sponsor_url,
        }


@dataclass
class NewsletterConfig:
    """Configuration for a newsletter source."""

    name: str
    archive_url: str
    issue_url_pattern: str
    requires_js: bool = True
    active: bool = True
    sponsor_patterns: dict = field(default_factory=dict)
    selectors: dict = field(default_factory=dict)


class BaseScraper(ABC):
    """
    Base class for newsletter scrapers.

    Handles Playwright browser management and provides common scraping utilities.
    Subclasses implement newsletter-specific extraction logic.
    """

    def __init__(
        self,
        config: dict[str, Any],
        headless: bool = True,
        timeout: int = 30000,
    ):
        """
        Initialize the scraper.

        Args:
            config: Newsletter configuration dictionary
            headless: Run browser in headless mode
            timeout: Page load timeout in milliseconds
        """
        self.config = NewsletterConfig(
            name=config.get("name", "Unknown"),
            archive_url=config.get("archive_url", ""),
            issue_url_pattern=config.get("issue_url_pattern", ""),
            requires_js=config.get("requires_js", True),
            active=config.get("active", True),
            sponsor_patterns=config.get("sponsor_patterns", {}),
            selectors=config.get("selectors", {}),
        )
        self.headless = headless
        self.timeout = timeout
        self._browser: Browser | None = None
        self._playwright = None

    def __enter__(self):
        """Context manager entry - start browser."""
        self.start_browser()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close browser."""
        self.close_browser()

    def start_browser(self):
        """Start Playwright browser instance."""
        if self._browser is not None:
            return

        logger.info("Starting Playwright browser...")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        logger.info("Browser started successfully")

    def close_browser(self):
        """Close browser and cleanup."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        logger.info("Browser closed")

    def _new_page(self) -> Page:
        """Create a new browser page with common settings."""
        if not self._browser:
            raise RuntimeError("Browser not started. Call start_browser() first.")

        context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(self.timeout)
        return page

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _load_page(self, page: Page, url: str, wait_for: str | None = None) -> str:
        """
        Load a page and return its HTML content.

        Args:
            page: Playwright page instance
            url: URL to load
            wait_for: Optional selector to wait for before returning

        Returns:
            Page HTML content
        """
        logger.debug(f"Loading page: {url}")

        try:
            page.goto(url, wait_until="networkidle")

            if wait_for:
                page.wait_for_selector(wait_for, timeout=10000)

            # Give JS a moment to finish rendering
            page.wait_for_timeout(1000)

            return page.content()

        except PlaywrightTimeout as e:
            logger.warning(f"Timeout loading {url}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error loading {url}: {e}")
            raise

    def _scroll_to_load_all(self, page: Page, max_scrolls: int = 50):
        """
        Scroll page to trigger lazy loading of all content.

        Args:
            page: Playwright page instance
            max_scrolls: Maximum number of scroll attempts
        """
        previous_height = 0

        for i in range(max_scrolls):
            # Scroll to bottom
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)

            # Check if we've reached the end
            current_height = page.evaluate("document.body.scrollHeight")
            if current_height == previous_height:
                logger.debug(f"Finished scrolling after {i + 1} scrolls")
                break

            previous_height = current_height

        # Scroll back to top
        page.evaluate("window.scrollTo(0, 0)")

    @abstractmethod
    def discover_all_issues(self, limit: int | None = None) -> list[str]:
        """
        Discover all issue URLs from the archive.

        Args:
            limit: Maximum number of issues to return

        Returns:
            List of issue URLs
        """
        pass

    @abstractmethod
    def scrape_issue(self, issue_url: str) -> list[SponsorInfo]:
        """
        Scrape a single issue for sponsor information.

        Args:
            issue_url: URL of the newsletter issue

        Returns:
            List of SponsorInfo objects found in the issue
        """
        pass

    def extract_sponsor_from_pattern(
        self,
        html: str,
        pattern_config: dict,
        issue_url: str,
    ) -> SponsorInfo | None:
        """
        Extract sponsor info using a regex pattern.

        Args:
            html: HTML content to search
            pattern_config: Pattern configuration with 'pattern' and 'placement_type'
            issue_url: URL of the issue being scraped

        Returns:
            SponsorInfo if pattern matched, None otherwise
        """
        pattern = pattern_config.get("pattern", "")
        placement_type = pattern_config.get("placement_type", "unknown")

        match = re.search(pattern, html, re.IGNORECASE)
        if not match:
            return None

        # Get the sponsor name from the match
        sponsor_name = match.group(1) if match.groups() else match.group(0)
        sponsor_name = clean_text(sponsor_name)

        if not sponsor_name or len(sponsor_name) < 2:
            return None

        # Try to find the sponsor's URL near the match
        sponsor_domain = self._find_sponsor_domain(html, sponsor_name, match.start())

        # Extract ad copy snippet from context around the match
        ad_copy = self._extract_ad_copy(html, match.start(), match.end())

        return SponsorInfo(
            advertiser_name=sponsor_name,
            advertiser_domain=sponsor_domain,
            placement_type=placement_type,
            ad_copy_snippet=truncate_text(ad_copy, 150),
            issue_url=issue_url,
            issue_date=None,  # To be filled by subclass
            source_newsletter=self.config.name.lower().replace(" ", "_"),
            confidence="high",
        )

    def _find_sponsor_domain(
        self,
        html: str,
        sponsor_name: str,
        match_position: int,
        search_window: int = 2000,
    ) -> str | None:
        """
        Find the sponsor's domain from links near their mention.

        Args:
            html: Full HTML content
            sponsor_name: Name of the sponsor
            match_position: Position where sponsor was found
            search_window: Characters to search before/after

        Returns:
            Domain string or None
        """
        # Get HTML section around the match
        start = max(0, match_position - search_window // 2)
        end = min(len(html), match_position + search_window // 2)
        section = html[start:end]

        # Parse links in this section
        links = extract_links_from_html(section)

        # Filter out newsletter's own domain and common tracking domains
        skip_domains = {
            "morningbrew.com",
            "healthcare-brew.com",
            "healthcarebrew.com",
            "morning-brew.com",
            "twitter.com",
            "x.com",
            "facebook.com",
            "linkedin.com",
            "instagram.com",
            "youtube.com",
            "google.com",
            "bit.ly",
            "t.co",
        }

        # Look for links that might be the sponsor
        for link in links:
            domain = link.get("domain")
            if not domain or domain in skip_domains:
                continue

            # Check if link text or domain relates to sponsor name
            normalized_sponsor = normalize_company_name(sponsor_name)
            link_text = normalize_company_name(link.get("text", ""))

            if normalized_sponsor in link_text or normalized_sponsor in domain:
                return domain

            # If it's a tracking link, it's likely the sponsor
            if is_tracking_link(link.get("href", "")):
                return domain

        # Fallback: return first external link that's not in skip list
        for link in links:
            domain = link.get("domain")
            if domain and domain not in skip_domains:
                return domain

        return None

    def _extract_ad_copy(
        self,
        html: str,
        start: int,
        end: int,
        max_length: int = 500,
    ) -> str:
        """
        Extract readable text around a sponsor mention.

        Args:
            html: Full HTML content
            start: Start position of match
            end: End position of match
            max_length: Maximum characters to extract

        Returns:
            Clean text snippet
        """
        # Expand window to get context
        window_start = max(0, start - 100)
        window_end = min(len(html), end + max_length)
        section = html[window_start:window_end]

        # Parse and get text
        soup = BeautifulSoup(section, "lxml")
        text = soup.get_text(separator=" ")
        text = clean_text(text)

        return text

    def auto_detect_sponsors(self, html: str, issue_url: str) -> list[SponsorInfo]:
        """
        Auto-detect sponsors using common patterns (for unknown newsletters).

        Args:
            html: HTML content
            issue_url: Issue URL

        Returns:
            List of detected sponsors
        """
        sponsors = []

        # Common sponsor keywords to look for
        keywords = [
            r"Presented\s+[Bb]y\s+([A-Z][A-Za-z0-9\s]+)",
            r"Together\s+[Ww]ith\s+([A-Z][A-Za-z0-9\s]+)",
            r"Sponsored\s+[Bb]y\s+([A-Z][A-Za-z0-9\s]+)",
            r"Brought\s+to\s+you\s+by\s+([A-Z][A-Za-z0-9\s]+)",
            r"Partner:\s*([A-Z][A-Za-z0-9\s]+)",
        ]

        seen_names = set()

        for pattern in keywords:
            for match in re.finditer(pattern, html):
                name = clean_text(match.group(1))
                normalized = normalize_company_name(name)

                if normalized in seen_names or len(name) < 2 or len(name) > 50:
                    continue

                seen_names.add(normalized)

                domain = self._find_sponsor_domain(html, name, match.start())
                ad_copy = self._extract_ad_copy(html, match.start(), match.end())

                sponsors.append(SponsorInfo(
                    advertiser_name=name,
                    advertiser_domain=domain,
                    placement_type="auto_detected",
                    ad_copy_snippet=truncate_text(ad_copy, 150),
                    issue_url=issue_url,
                    issue_date=None,
                    source_newsletter=self.config.name.lower().replace(" ", "_"),
                    confidence="low",
                ))

        return sponsors

    def run_full_scan(
        self,
        limit: int | None = None,
        show_progress: bool = True,
    ) -> list[SponsorInfo]:
        """
        Run a complete scan of all newsletter issues.

        Args:
            limit: Maximum number of issues to scan
            show_progress: Show progress bar

        Returns:
            List of all discovered sponsors
        """
        all_sponsors = []

        # Discover all issues
        logger.info(f"Discovering issues from {self.config.name}...")
        issues = self.discover_all_issues(limit=limit)
        logger.info(f"Found {len(issues)} issues to scan")

        if not issues:
            logger.warning("No issues found!")
            return []

        # Scan each issue
        iterator = tqdm(issues, desc="Scanning issues") if show_progress else issues

        for issue_url in iterator:
            try:
                sponsors = self.scrape_issue(issue_url)
                all_sponsors.extend(sponsors)

                if sponsors and show_progress:
                    names = [s.advertiser_name for s in sponsors]
                    logger.debug(f"Found sponsors in {issue_url}: {names}")

            except Exception as e:
                logger.error(f"Error scraping {issue_url}: {e}")
                continue

        # Deduplicate by domain
        seen_domains = set()
        unique_sponsors = []

        for sponsor in all_sponsors:
            key = sponsor.advertiser_domain or normalize_company_name(sponsor.advertiser_name)
            if key not in seen_domains:
                seen_domains.add(key)
                unique_sponsors.append(sponsor)

        logger.info(
            f"Scan complete. Found {len(all_sponsors)} total mentions, "
            f"{len(unique_sponsors)} unique advertisers"
        )

        return unique_sponsors
