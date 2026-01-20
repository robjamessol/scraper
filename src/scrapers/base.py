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
    is_tracking_domain,
    resolve_redirect_url,
    guess_domain_from_name,
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
    landing_page_url: str | None = None  # The actual ad landing page
    product_service: str | None = None   # What they're selling
    ad_headline: str | None = None       # The main headline
    call_to_action: str | None = None    # CTA text (e.g., "Learn More", "Get Started")
    full_ad_copy: str = ""               # Complete ad text

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for DataFrame/CSV export."""
        return {
            "advertiser_name": self.advertiser_name,
            "advertiser_domain": self.advertiser_domain,
            "placement_type": self.placement_type,
            "ad_copy_snippet": self.ad_copy_snippet,
            "full_ad_copy": self.full_ad_copy,
            "ad_headline": self.ad_headline,
            "product_service": self.product_service,
            "call_to_action": self.call_to_action,
            "landing_page_url": self.landing_page_url,
            "sponsor_url": self.sponsor_url,
            "issue_url": self.issue_url,
            "issue_date": self.issue_date,
            "source_newsletter": self.source_newsletter,
            "category": self.category,
            "niche_fit": self.niche_fit,
            "confidence": self.confidence,
        }


@dataclass
class AffiliateLink:
    """Information about an embedded affiliate/product link."""

    advertiser_name: str
    advertiser_domain: str | None
    link_url: str
    link_text: str
    affiliate_network: str | None  # e.g., "Amazon", "ShareASale", "Impact"
    issue_url: str
    issue_date: str | None
    source_newsletter: str
    placement_type: str = "affiliate_link"
    context_snippet: str = ""  # Text around the link for context

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for DataFrame/CSV export."""
        return {
            "advertiser_name": self.advertiser_name,
            "advertiser_domain": self.advertiser_domain,
            "placement_type": self.placement_type,
            "ad_copy_snippet": self.context_snippet,
            "full_ad_copy": self.context_snippet,
            "ad_headline": self.link_text,
            "product_service": None,
            "call_to_action": self.link_text,
            "landing_page_url": self.link_url,
            "sponsor_url": self.link_url,
            "issue_url": self.issue_url,
            "issue_date": self.issue_date,
            "source_newsletter": self.source_newsletter,
            "category": "affiliate",
            "niche_fit": "Unknown",
            "confidence": "low",  # Affiliate links are less certain
            "affiliate_network": self.affiliate_network,
        }


# Known affiliate network domains and their names
AFFILIATE_NETWORKS = {
    # Amazon
    "amzn.to": "Amazon",
    "amazon.com": "Amazon",
    "amazon.co.uk": "Amazon",
    # ShareASale
    "shrsl.com": "ShareASale",
    "shareasale.com": "ShareASale",
    # CJ Affiliate (Commission Junction)
    "dpbolvw.net": "CJ Affiliate",
    "jdoqocy.com": "CJ Affiliate",
    "tkqlhce.com": "CJ Affiliate",
    "anrdoezrs.com": "CJ Affiliate",
    "cj.com": "CJ Affiliate",
    # Impact
    "pntrs.com": "Impact",
    "pntra.com": "Impact",
    "prf.hn": "Impact",
    "impactradius.com": "Impact",
    # Rakuten
    "linksynergy.com": "Rakuten",
    "click.linksynergy.com": "Rakuten",
    # Skimlinks
    "go.redirectingat.com": "Skimlinks",
    "go.skimresources.com": "Skimlinks",
    # Awin
    "awin1.com": "Awin",
    "zenaps.com": "Awin",
    # PartnerStack
    "partnerstack.com": "PartnerStack",
    # Refersion
    "refersion.com": "Refersion",
    # Other affiliate indicators
    "pxf.io": "PartnerStack",
    "geni.us": "Geniuslink",
    "howl.me": "Howl",
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

        IMPORTANT: This method follows tracking/redirect links to get
        the actual advertiser domain, not the newsletter's tracking domain.

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

        # Filter out newsletter's own domain and tracking domains
        skip_domains = {
            "morningbrew.com",
            "healthcare-brew.com",
            "healthcarebrew.com",
            "morning-brew.com",
            "links.morningbrew.com",
            "link.morningbrew.com",
            "links.healthcare-brew.com",
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

        normalized_sponsor = normalize_company_name(sponsor_name)

        # First pass: look for links that clearly relate to sponsor
        for link in links:
            href = link.get("href", "")
            domain = link.get("domain")
            link_text = normalize_company_name(link.get("text", ""))

            if not href:
                continue

            # Check if link relates to sponsor by name
            is_relevant = (
                normalized_sponsor in link_text or
                (domain and normalized_sponsor in domain.lower())
            )

            # Or has tracking params (indicates it's an ad link)
            is_tracking = is_tracking_link(href) or "mbadid" in href.lower()

            if not is_relevant and not is_tracking:
                continue

            # If this is a tracking domain, resolve the redirect
            if is_tracking_domain(href):
                logger.debug(f"Resolving tracking link for {sponsor_name}: {href[:60]}...")
                resolved_url = resolve_redirect_url(href)
                if resolved_url:
                    resolved_domain = extract_domain(resolved_url)
                    if resolved_domain and resolved_domain.lower() not in skip_domains:
                        logger.debug(f"Resolved {sponsor_name} to: {resolved_domain}")
                        return resolved_domain

            # If it's already a good domain, use it
            if domain and domain.lower() not in skip_domains:
                return domain

        # Second pass: try any external link with tracking params
        for link in links:
            href = link.get("href", "")
            domain = link.get("domain")

            if not href:
                continue

            # Check if it's a tracking link we should resolve
            if is_tracking_domain(href):
                resolved_url = resolve_redirect_url(href)
                if resolved_url:
                    resolved_domain = extract_domain(resolved_url)
                    if resolved_domain and resolved_domain.lower() not in skip_domains:
                        return resolved_domain

            # Or if it's just an external link
            if domain and domain.lower() not in skip_domains:
                return domain

        # Last resort: guess domain from company name
        guessed = guess_domain_from_name(sponsor_name)
        if guessed:
            logger.debug(f"Guessed domain for {sponsor_name}: {guessed}")
            return guessed

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

    def extract_product_service(self, ad_copy: str, company_name: str) -> dict[str, str | None]:
        """
        Extract product/service information from ad copy.

        Args:
            ad_copy: The full ad copy text
            company_name: Company name (to filter out)

        Returns:
            Dict with 'product_service', 'headline', 'call_to_action'
        """
        result = {
            "product_service": None,
            "headline": None,
            "call_to_action": None,
        }

        if not ad_copy:
            return result

        # Clean up the ad copy
        text = clean_text(ad_copy)
        lines = [l.strip() for l in text.split('.') if l.strip()]

        # Extract headline (usually first substantive line)
        for line in lines[:3]:
            # Skip if it's just the company name or "Presented By" header
            if len(line) > 10 and company_name.lower() not in line.lower()[:20]:
                if not line.lower().startswith(('presented', 'together', 'sponsored')):
                    result["headline"] = truncate_text(line, 100)
                    break

        # Extract CTA (call to action) - look for common patterns
        cta_patterns = [
            r'(Learn [Mm]ore)',
            r'(Get [Ss]tarted)',
            r'(Sign [Uu]p)',
            r'(Download)',
            r'(Try [Ii]t [Ff]ree)',
            r'(Start [Yy]our)',
            r'(Read [Mm]ore)',
            r'(Discover)',
            r'(Explore)',
            r'(Join)',
            r'(Register)',
            r'(Book [Aa]|Schedule)',
            r'(Get [Yy]our)',
            r'(Claim [Yy]our)',
            r'(See [Hh]ow)',
            r'(Find [Oo]ut)',
        ]

        for pattern in cta_patterns:
            match = re.search(pattern, text)
            if match:
                result["call_to_action"] = match.group(1)
                break

        # Extract product/service - look for what they're offering
        product_patterns = [
            # Software/Platform
            r'(?:our|the|new)\s+([\w\s]+(?:platform|software|tool|app|solution|system))',
            # Service
            r'(?:our|the|new)\s+([\w\s]+(?:service|services|program|plan))',
            # Product categories
            r'(?:our|the|new)\s+([\w\s]+(?:supplement|device|test|kit|treatment))',
            # Generic "offering"
            r'(?:introducing|announcing|meet)\s+([\w\s]+)',
            # White paper / resource
            r'(?:free|new)\s+([\w\s]*(?:guide|report|white\s*paper|ebook|webinar))',
        ]

        for pattern in product_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                product = clean_text(match.group(1))
                if len(product) > 5 and len(product) < 100:
                    result["product_service"] = product
                    break

        # If no specific product found, try to summarize what they're offering
        if not result["product_service"] and result["headline"]:
            # Use the headline as a fallback description
            result["product_service"] = result["headline"]

        return result

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

    def extract_affiliate_links(
        self,
        html: str,
        issue_url: str,
        issue_date: str | None = None,
    ) -> list[AffiliateLink]:
        """
        Extract embedded affiliate/product links from newsletter content.

        Finds links to known affiliate networks and product pages that may
        indicate advertising relationships not captured by sponsor patterns.

        Args:
            html: Full HTML content of the newsletter
            issue_url: URL of the newsletter issue
            issue_date: Date of the issue

        Returns:
            List of AffiliateLink objects
        """
        soup = BeautifulSoup(html, "lxml")
        affiliate_links = []
        seen_domains = set()

        # Skip newsletter's own domain and common non-affiliate links
        skip_domains = {
            "morningbrew.com", "healthcare-brew.com", "healthcarebrew.com",
            "twitter.com", "x.com", "facebook.com", "linkedin.com",
            "instagram.com", "youtube.com", "google.com", "apple.com",
            "spotify.com", "mailto:", "tel:",
        }

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            link_text = clean_text(a.get_text())

            # Skip empty or internal links
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            # Extract domain
            domain = extract_domain(href, strip_marketing=False)
            if not domain:
                continue

            # Skip newsletter's own links
            if any(skip in domain.lower() for skip in skip_domains):
                continue

            # Check if this is an affiliate network link
            affiliate_network = None
            for network_domain, network_name in AFFILIATE_NETWORKS.items():
                if network_domain in domain.lower():
                    affiliate_network = network_name
                    break

            # Also check for affiliate indicators in URL params
            href_lower = href.lower()
            if not affiliate_network:
                if "tag=" in href_lower and "amazon" in href_lower:
                    affiliate_network = "Amazon"
                elif any(param in href_lower for param in ["?ref=", "&ref=", "affiliate", "partner_id", "aff_id"]):
                    affiliate_network = "Unknown Affiliate"

            # If it's an affiliate link, process it
            if affiliate_network:
                # Try to resolve to get actual advertiser domain
                actual_domain = domain
                if affiliate_network != "Amazon":  # Amazon links are the actual product
                    try:
                        resolved = resolve_redirect_url(href, timeout=2.0)
                        if resolved:
                            actual_domain = extract_domain(resolved, strip_marketing=True) or domain
                    except Exception:
                        pass

                # Skip if we've already seen this domain
                if actual_domain in seen_domains:
                    continue
                seen_domains.add(actual_domain)

                # Extract context (text around the link)
                parent = a.parent
                context = ""
                if parent:
                    context = clean_text(parent.get_text())[:200]

                # Try to determine advertiser name from link text or domain
                advertiser_name = link_text if link_text and len(link_text) > 2 else actual_domain.split(".")[0].title()

                affiliate_links.append(AffiliateLink(
                    advertiser_name=advertiser_name,
                    advertiser_domain=actual_domain,
                    link_url=href,
                    link_text=link_text,
                    affiliate_network=affiliate_network,
                    issue_url=issue_url,
                    issue_date=issue_date,
                    source_newsletter=self.config.name.lower().replace(" ", "_"),
                    context_snippet=context,
                ))

        return affiliate_links
