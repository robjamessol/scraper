"""Healthcare Brew newsletter scraper."""

import re
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, SponsorInfo
from ..utils.helpers import (
    extract_domain,
    clean_text,
    normalize_company_name,
    truncate_text,
    resolve_sponsor_domain,
    is_tracking_domain,
    resolve_redirect_url,
    guess_domain_from_name,
)


logger = logging.getLogger(__name__)


class HealthcareBrewScraper(BaseScraper):
    """
    Scraper for Healthcare Brew newsletter.

    Verified structure (Jan 2026):
    - Archive: https://www.healthcare-brew.com/archive (JS-rendered)
    - Issues: https://www.healthcare-brew.com/issues/{slug}

    Sponsor placements:
    - "Presented By | [SponsorName](link)" - logo at top
    - "Presented By SponsorName" - full advertorial section
    - "Together With SponsorName" - secondary sponsor
    - "*A message from our sponsor." - in-content mention
    """

    BASE_URL = "https://www.healthcare-brew.com"

    def __init__(self, config: dict[str, Any] | None = None, **kwargs):
        """
        Initialize Healthcare Brew scraper.

        Args:
            config: Optional config override. If None, uses defaults for Healthcare Brew.
            **kwargs: Additional arguments passed to BaseScraper
        """
        if config is None:
            config = self._default_config()

        super().__init__(config, **kwargs)

    def _click_load_more_buttons(self, page, max_clicks: int = 50, target_count: int = 300) -> bool:
        """
        Click "Load More" or "Show More" buttons to load additional issues.

        Many archive pages use buttons instead of infinite scroll.

        Args:
            page: Playwright page instance
            max_clicks: Maximum button clicks to attempt
            target_count: Stop when this many issues are loaded

        Returns:
            True if a load more button was found and clicked at least once
        """
        # Common selectors for "Load More" buttons
        load_more_selectors = [
            'button:has-text("Load More")',
            'button:has-text("Show More")',
            'button:has-text("Load more")',
            'button:has-text("Show more")',
            'a:has-text("Load More")',
            'a:has-text("Show More")',
            '[class*="load-more"]',
            '[class*="show-more"]',
            '[data-action="load-more"]',
            'button:has-text("View More")',
            'button:has-text("See More")',
        ]

        clicked = False

        for click_num in range(max_clicks):
            # Check how many issues we have
            issue_links = page.query_selector_all('a[href*="/issues/"]')
            current_count = len(issue_links)

            if current_count >= target_count:
                logger.info(f"Reached target: {current_count} issues loaded")
                break

            # Try each selector
            button_found = False
            for selector in load_more_selectors:
                try:
                    button = page.query_selector(selector)
                    if button and button.is_visible():
                        logger.info(f"Clicking load more button ({current_count} issues so far)...")
                        button.click()
                        clicked = True
                        button_found = True
                        # Wait for new content to load
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue

            if not button_found:
                # No button found, stop trying
                if clicked:
                    logger.info(f"No more 'Load More' buttons, loaded {current_count} issues")
                break

            # Log progress every 10 clicks
            if (click_num + 1) % 10 == 0:
                logger.info(f"Load more: {click_num + 1} clicks, {current_count} issues loaded")

        return clicked

    @staticmethod
    def _default_config() -> dict[str, Any]:
        """Return default configuration for Healthcare Brew."""
        return {
            "name": "Healthcare Brew",
            "archive_url": "https://www.healthcare-brew.com/archive",
            "issue_url_pattern": "https://www.healthcare-brew.com/issues/{slug}",
            "requires_js": True,
            "active": True,
            "sponsor_patterns": {
                "presented_by_logo": {
                    "pattern": r'Presented By \| \[([^\]]+)\]',
                    "placement_type": "presented_by_logo",
                    "priority": 1,
                },
                "presented_by_section": {
                    "pattern": r'Presented By\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z]?[A-Za-z0-9]+)*)',
                    "placement_type": "presented_by_section",
                    "priority": 1,
                },
                "together_with": {
                    "pattern": r'Together With\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z]?[A-Za-z0-9]+)*)',
                    "placement_type": "together_with",
                    "priority": 2,
                },
            },
            "selectors": {
                "archive_links": 'a[href*="/issues/"]',
                "issue_content": "article, .newsletter-content, main, body",
            },
        }

    def discover_all_issues(self, limit: int | None = None) -> list[str]:
        """
        Discover all issue URLs from the Healthcare Brew archive.

        The archive page loads via JavaScript and may require scrolling
        or clicking "Load More" to load all issues.

        Args:
            limit: Maximum number of issues to return

        Returns:
            List of issue URLs
        """
        page = self._new_page()
        issue_urls = []

        try:
            logger.info(f"Loading archive: {self.config.archive_url}")

            # Load the archive page
            page.goto(self.config.archive_url, wait_until="networkidle")

            # Wait for content to render
            page.wait_for_timeout(2000)

            # Try to load more issues - some archives use buttons, some use scroll
            # First, try clicking "Load More" / "Show More" buttons repeatedly
            load_more_clicked = self._click_load_more_buttons(page, max_clicks=50, target_count=limit or 300)

            # If no load more button, try scrolling for infinite scroll archives
            if not load_more_clicked:
                self._scroll_to_load_all(page, max_scrolls=100, wait_ms=800)

            # Get page content
            html = page.content()
            soup = BeautifulSoup(html, "lxml")

            # Find all issue links
            # Look for links containing /issues/ in the href
            for link in soup.find_all("a", href=True):
                href = link["href"]

                if "/issues/" not in href:
                    continue

                # Build full URL
                if href.startswith("/"):
                    full_url = urljoin(self.BASE_URL, href)
                elif href.startswith("http"):
                    full_url = href
                else:
                    continue

                # Avoid duplicates
                if full_url not in issue_urls:
                    issue_urls.append(full_url)

            logger.info(f"Found {len(issue_urls)} issues in archive")

            # Apply limit if specified
            if limit:
                logger.info(f"Applying limit: {limit} (from {len(issue_urls)} total)")
                issue_urls = issue_urls[:limit]
                logger.info(f"After limit: {len(issue_urls)} issues")

        except Exception as e:
            logger.error(f"Error discovering issues: {e}")
            raise

        finally:
            page.context.close()

        return issue_urls

    def scrape_issue(self, issue_url: str) -> list[SponsorInfo]:
        """
        Scrape a single issue for sponsor information.

        Args:
            issue_url: URL of the newsletter issue

        Returns:
            List of SponsorInfo objects found in the issue
        """
        page = self._new_page()
        sponsors = []
        seen_sponsors = set()

        try:
            logger.debug(f"Scraping issue: {issue_url}")

            # Load the issue page
            html = self._load_page(page, issue_url)
            soup = BeautifulSoup(html, "lxml")

            # Try to extract issue date from the page
            issue_date = self._extract_issue_date(soup, issue_url)

            # Get the main content area
            content_selectors = self.config.selectors.get(
                "issue_content",
                "article, main, body"
            ).split(", ")

            content_html = ""
            for selector in content_selectors:
                content = soup.select_one(selector)
                if content:
                    content_html = str(content)
                    break

            if not content_html:
                content_html = html

            # Extract sponsors using defined patterns
            for pattern_name, pattern_config in self.config.sponsor_patterns.items():
                sponsor = self._extract_sponsor_with_context(
                    content_html,
                    soup,
                    pattern_config,
                    issue_url,
                    issue_date,
                )

                if sponsor:
                    # Deduplicate by normalized name
                    normalized = normalize_company_name(sponsor.advertiser_name)
                    if normalized not in seen_sponsors and len(normalized) > 1:
                        seen_sponsors.add(normalized)
                        sponsors.append(sponsor)
                        logger.debug(
                            f"Found sponsor: {sponsor.advertiser_name} "
                            f"({sponsor.placement_type})"
                        )

            # If no sponsors found with patterns, try auto-detection
            if not sponsors:
                auto_sponsors = self.auto_detect_sponsors(content_html, issue_url)
                for sponsor in auto_sponsors:
                    normalized = normalize_company_name(sponsor.advertiser_name)
                    if normalized not in seen_sponsors:
                        seen_sponsors.add(normalized)
                        sponsor.issue_date = issue_date
                        sponsors.append(sponsor)

            # Also extract affiliate links embedded in the content
            affiliate_links = self.extract_affiliate_links(content_html, issue_url, issue_date)
            for aff in affiliate_links:
                normalized = normalize_company_name(aff.advertiser_name)
                if normalized not in seen_sponsors and aff.advertiser_domain:
                    seen_sponsors.add(normalized)
                    # Convert to SponsorInfo for consistent output
                    sponsor = SponsorInfo(
                        advertiser_name=aff.advertiser_name,
                        advertiser_domain=aff.advertiser_domain,
                        placement_type="affiliate_link",
                        ad_copy_snippet=aff.context_snippet,
                        issue_url=issue_url,
                        issue_date=issue_date,
                        source_newsletter=self.config.name.lower().replace(" ", "_"),
                        confidence="low",
                        sponsor_url=aff.link_url,
                        landing_page_url=aff.link_url,
                        full_ad_copy=aff.context_snippet,
                    )
                    sponsors.append(sponsor)
                    logger.debug(f"Found affiliate link: {aff.advertiser_name} via {aff.affiliate_network}")

        except Exception as e:
            logger.error(f"Error scraping issue {issue_url}: {e}")

        finally:
            page.context.close()

        return sponsors

    def _extract_sponsor_with_context(
        self,
        html: str,
        soup: BeautifulSoup,
        pattern_config: dict,
        issue_url: str,
        issue_date: str | None,
    ) -> SponsorInfo | None:
        """
        Extract sponsor with enhanced context extraction.

        Args:
            html: HTML content string
            soup: Parsed BeautifulSoup object
            pattern_config: Pattern configuration
            issue_url: Issue URL
            issue_date: Extracted issue date

        Returns:
            SponsorInfo or None
        """
        pattern = pattern_config.get("pattern", "")
        placement_type = pattern_config.get("placement_type", "unknown")

        # Search for the pattern
        match = re.search(pattern, html, re.IGNORECASE)
        if not match:
            return None

        # Extract sponsor name
        sponsor_name = match.group(1) if match.groups() else match.group(0)
        sponsor_name = clean_text(sponsor_name)

        # Clean up sponsor name - remove trailing common words that got captured
        sponsor_name = re.sub(
            r'\s+(Take|Learn|Get|Read|Check|Click|See|Visit|Discover).*$',
            '',
            sponsor_name,
            flags=re.IGNORECASE
        )
        sponsor_name = sponsor_name.strip()

        if not sponsor_name or len(sponsor_name) < 2 or len(sponsor_name) > 50:
            return None

        # Find sponsor URL and domain
        sponsor_url, sponsor_domain = self._find_sponsor_url(
            html, soup, sponsor_name, match.start()
        )

        # Extract ad copy - look for the sponsored content section
        ad_copy = self._extract_sponsor_content(html, soup, match.start(), sponsor_name)

        # Extract product/service info from ad copy
        product_info = self.extract_product_service(ad_copy, sponsor_name)

        # Find landing page URL (the main CTA link)
        landing_page = self._find_landing_page(html, soup, sponsor_name, match.start())

        return SponsorInfo(
            advertiser_name=sponsor_name,
            advertiser_domain=sponsor_domain,
            placement_type=placement_type,
            ad_copy_snippet=truncate_text(ad_copy, 150),
            full_ad_copy=ad_copy,
            ad_headline=product_info.get("headline"),
            product_service=product_info.get("product_service"),
            call_to_action=product_info.get("call_to_action"),
            landing_page_url=landing_page,
            issue_url=issue_url,
            issue_date=issue_date,
            source_newsletter="healthcare_brew",
            confidence="high",
            sponsor_url=sponsor_url,
        )

    def _find_sponsor_url(
        self,
        html: str,
        soup: BeautifulSoup,
        sponsor_name: str,
        match_position: int,
    ) -> tuple[str | None, str | None]:
        """
        Find the sponsor's URL from nearby links.

        IMPORTANT: This method now follows tracking redirects to get the
        actual advertiser domain, not the newsletter's tracking domain.

        Args:
            html: HTML content
            soup: Parsed soup
            sponsor_name: Sponsor name to look for
            match_position: Position in HTML where sponsor was found

        Returns:
            Tuple of (sponsor_url, sponsor_domain)
        """
        # Skip these domains - they're not the actual advertisers
        skip_domains = {
            "healthcare-brew.com",
            "healthcarebrew.com",
            "morningbrew.com",
            "morning-brew.com",
            "links.morningbrew.com",
            "link.morningbrew.com",
            "twitter.com",
            "x.com",
            "facebook.com",
            "linkedin.com",
            "instagram.com",
            "youtube.com",
            "google.com",
            "bit.ly",
            "t.co",
            "mailto:",
        }

        # Get section around the match
        window_start = max(0, match_position - 500)
        window_end = min(len(html), match_position + 2000)
        section = html[window_start:window_end]

        section_soup = BeautifulSoup(section, "lxml")
        normalized_sponsor = normalize_company_name(sponsor_name)

        best_url = None
        best_domain = None

        # Look for links in the sponsor section
        for link in section_soup.find_all("a", href=True):
            href = link.get("href", "")
            text = clean_text(link.get_text())

            if not href or href.startswith("mailto:"):
                continue

            # Check if this link relates to the sponsor
            link_domain = extract_domain(href)
            is_relevant = (
                normalized_sponsor in normalize_company_name(text) or
                (link_domain and normalized_sponsor in link_domain.lower()) or
                "utm_" in href.lower() or  # Tracking link = likely sponsor
                "mbadid" in href.lower()   # Morning Brew ad ID
            )

            if not is_relevant:
                continue

            # Check if this is a tracking link that needs resolving
            if is_tracking_domain(href):
                logger.debug(f"Resolving tracking link for {sponsor_name}: {href[:60]}...")
                resolved_url = resolve_redirect_url(href)
                if resolved_url:
                    resolved_domain = extract_domain(resolved_url)
                    if resolved_domain and resolved_domain.lower() not in skip_domains:
                        logger.debug(f"Resolved to: {resolved_domain}")
                        return resolved_url, resolved_domain

            # If it's already a good domain, use it
            if link_domain and link_domain.lower() not in skip_domains:
                return href, link_domain

            # Keep the first tracking URL as fallback (we'll try to resolve it later)
            if not best_url and href:
                best_url = href
                best_domain = link_domain

        # If we found a tracking URL but couldn't resolve, try one more time
        if best_url and is_tracking_domain(best_url):
            resolved_url = resolve_redirect_url(best_url)
            if resolved_url:
                resolved_domain = extract_domain(resolved_url)
                if resolved_domain and resolved_domain.lower() not in skip_domains:
                    return resolved_url, resolved_domain

        # Last resort: guess domain from company name
        guessed_domain = guess_domain_from_name(sponsor_name)
        if guessed_domain:
            logger.debug(f"Guessed domain for {sponsor_name}: {guessed_domain}")
            return None, guessed_domain

        return best_url, best_domain

    def _find_landing_page(
        self,
        html: str,
        soup: BeautifulSoup,
        sponsor_name: str,
        match_position: int,
    ) -> str | None:
        """
        Find the main landing page URL (CTA link) for the sponsor.

        Args:
            html: HTML content
            soup: Parsed soup
            sponsor_name: Sponsor name
            match_position: Position where sponsor was found

        Returns:
            Landing page URL or None
        """
        skip_domains = {
            "healthcare-brew.com", "morningbrew.com", "twitter.com",
            "x.com", "facebook.com", "linkedin.com", "instagram.com",
        }

        # Get section around the match
        window_start = max(0, match_position - 200)
        window_end = min(len(html), match_position + 3000)
        section = html[window_start:window_end]

        section_soup = BeautifulSoup(section, "lxml")

        # Look for CTA-style links (buttons, "Learn More", etc.)
        cta_keywords = [
            "learn more", "get started", "sign up", "download", "try",
            "read more", "discover", "explore", "register", "join",
            "book", "schedule", "claim", "start", "see how", "find out",
        ]

        for link in section_soup.find_all("a", href=True):
            href = link.get("href", "")
            text = clean_text(link.get_text()).lower()
            domain = extract_domain(href)

            if not domain or domain in skip_domains:
                continue

            # Check if link text matches CTA patterns
            if any(cta in text for cta in cta_keywords):
                return href

            # Check for tracking parameters (indicates ad link)
            if "utm_" in href.lower() or "?ref=" in href.lower():
                return href

        return None

    def _extract_sponsor_content(
        self,
        html: str,
        soup: BeautifulSoup,
        match_position: int,
        sponsor_name: str,
    ) -> str:
        """
        Extract the sponsored content/ad copy.

        Args:
            html: HTML content
            soup: Parsed soup
            match_position: Position where sponsor header was found
            sponsor_name: Name of sponsor

        Returns:
            Ad copy text
        """
        # Get a window after the match (where the ad copy would be)
        window_start = match_position
        window_end = min(len(html), match_position + 1500)
        section = html[window_start:window_end]

        section_soup = BeautifulSoup(section, "lxml")

        # Get text content
        text = section_soup.get_text(separator=" ")
        text = clean_text(text)

        # Remove the sponsor header part
        text = re.sub(
            rf'^.*?{re.escape(sponsor_name)}\s*',
            '',
            text,
            flags=re.IGNORECASE
        )

        # Take first ~200 chars as the ad copy snippet
        if len(text) > 200:
            # Try to break at a sentence
            sentences = re.split(r'[.!?]\s+', text[:300])
            if len(sentences) > 1:
                text = '. '.join(sentences[:2]) + '.'
            else:
                text = text[:200]

        return text.strip()

    def _extract_issue_date(
        self,
        soup: BeautifulSoup,
        issue_url: str,
    ) -> str | None:
        """
        Try to extract the issue publication date with multiple fallback strategies.

        Args:
            soup: Parsed page
            issue_url: Issue URL (may contain date info)

        Returns:
            Date string in YYYY-MM-DD format, or None
        """
        # Strategy 1: Try common date meta tags
        date_selectors = [
            ('meta[property="article:published_time"]', "content"),
            ('meta[property="og:published_time"]', "content"),
            ('meta[name="date"]', "content"),
            ('meta[name="pubdate"]', "content"),
            ('meta[name="publish-date"]', "content"),
            ('meta[name="article:published"]', "content"),
            ('time[datetime]', "datetime"),
            ('time', "datetime"),
        ]

        for selector, attr in date_selectors:
            element = soup.select_one(selector)
            if element:
                date_str = element.get(attr)
                if date_str:
                    date = self._parse_date(date_str)
                    if date:
                        return date

        # Strategy 2: Look for date in elements with date-related classes
        date_containers = soup.select('[class*="date"], [class*="time"], [class*="publish"]')
        for container in date_containers:
            text = container.get_text()
            date = self._parse_date(text)
            if date:
                return date

        # Strategy 3: Try to extract from URL
        url_date_patterns = [
            r'/(\d{4})-(\d{2})-(\d{2})/',
            r'/(\d{4})/(\d{2})/(\d{2})/',
            r'-(\d{4})(\d{2})(\d{2})(?:[/-]|$)',
            r'(\d{4})-(\d{2})-(\d{2})',
        ]

        for pattern in url_date_patterns:
            match = re.search(pattern, issue_url)
            if match:
                try:
                    groups = match.groups()
                    return f"{groups[0]}-{groups[1]}-{groups[2]}"
                except (ValueError, IndexError):
                    continue

        # Strategy 4: Look for date patterns in page text
        body_text = soup.get_text()
        date_patterns = [
            r'((?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[.,]?\s+\d{1,2}[,.]?\s+\d{4})',
            r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, body_text, re.IGNORECASE)
            if match:
                date = self._parse_date(match.group(1))
                if date:
                    return date

        return None

    def _parse_date(self, date_str: str | None) -> str | None:
        """Parse various date formats to YYYY-MM-DD."""
        if not date_str:
            return None

        date_str = date_str.strip()

        # Common formats to try
        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d",
            "%B %d, %Y",
            "%b %d, %Y",
            "%m/%d/%Y",
            "%d/%m/%Y",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(date_str[:len(date_str)], fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None


class MorningBrewScraper(BaseScraper):
    """
    Scraper for Morning Brew daily newsletter.

    Similar structure to Healthcare Brew (same parent company).
    """

    BASE_URL = "https://www.morningbrew.com"

    def __init__(self, config: dict[str, Any] | None = None, **kwargs):
        """Initialize Morning Brew scraper."""
        if config is None:
            config = self._default_config()
        super().__init__(config, **kwargs)

    @staticmethod
    def _default_config() -> dict[str, Any]:
        """Return default configuration for Morning Brew."""
        return {
            "name": "Morning Brew",
            "archive_url": "https://www.morningbrew.com/daily/archive",
            "issue_url_pattern": "https://www.morningbrew.com/daily/issues/{slug}",
            "requires_js": True,
            "active": True,
            "sponsor_patterns": {
                "presented_by_logo": {
                    "pattern": r'Presented By \| \[([^\]]+)\]',
                    "placement_type": "presented_by_logo",
                    "priority": 1,
                },
                "presented_by_section": {
                    "pattern": r'Presented By\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z]?[A-Za-z0-9]+)*)',
                    "placement_type": "presented_by_section",
                    "priority": 1,
                },
                "together_with": {
                    "pattern": r'Together With\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z]?[A-Za-z0-9]+)*)',
                    "placement_type": "together_with",
                    "priority": 2,
                },
            },
            "selectors": {
                "archive_links": 'a[href*="/issues/"]',
                "issue_content": "article, .newsletter-content, main, body",
            },
        }

    def discover_all_issues(self, limit: int | None = None) -> list[str]:
        """Discover all issue URLs from the Morning Brew archive."""
        # Implementation similar to Healthcare Brew
        page = self._new_page()
        issue_urls = []

        try:
            logger.info(f"Loading archive: {self.config.archive_url}")
            page.goto(self.config.archive_url, wait_until="networkidle")
            page.wait_for_timeout(2000)
            self._scroll_to_load_all(page, max_scrolls=30)

            html = page.content()
            soup = BeautifulSoup(html, "lxml")

            for link in soup.find_all("a", href=True):
                href = link["href"]
                if "/daily/issues/" not in href and "/issues/" not in href:
                    continue

                if href.startswith("/"):
                    full_url = urljoin(self.BASE_URL, href)
                elif href.startswith("http"):
                    full_url = href
                else:
                    continue

                if full_url not in issue_urls:
                    issue_urls.append(full_url)

            logger.info(f"Found {len(issue_urls)} issues in archive")

            if limit:
                issue_urls = issue_urls[:limit]

        except Exception as e:
            logger.error(f"Error discovering issues: {e}")
            raise

        finally:
            page.context.close()

        return issue_urls

    def scrape_issue(self, issue_url: str) -> list[SponsorInfo]:
        """Scrape a single Morning Brew issue for sponsors."""
        # Reuse Healthcare Brew's implementation since structure is similar
        page = self._new_page()
        sponsors = []
        seen_sponsors = set()

        try:
            html = self._load_page(page, issue_url)
            soup = BeautifulSoup(html, "lxml")

            # Extract date
            issue_date = self._extract_issue_date(soup, issue_url)

            # Get content
            for selector in ["article", "main", "body"]:
                content = soup.select_one(selector)
                if content:
                    content_html = str(content)
                    break
            else:
                content_html = html

            # Extract using patterns
            for pattern_name, pattern_config in self.config.sponsor_patterns.items():
                sponsor = self._extract_sponsor(
                    content_html, soup, pattern_config, issue_url, issue_date
                )
                if sponsor:
                    normalized = normalize_company_name(sponsor.advertiser_name)
                    if normalized not in seen_sponsors and len(normalized) > 1:
                        seen_sponsors.add(normalized)
                        sponsors.append(sponsor)

            # Auto-detect if nothing found
            if not sponsors:
                auto_sponsors = self.auto_detect_sponsors(content_html, issue_url)
                for sponsor in auto_sponsors:
                    normalized = normalize_company_name(sponsor.advertiser_name)
                    if normalized not in seen_sponsors:
                        seen_sponsors.add(normalized)
                        sponsor.issue_date = issue_date
                        sponsor.source_newsletter = "morning_brew"
                        sponsors.append(sponsor)

        except Exception as e:
            logger.error(f"Error scraping issue {issue_url}: {e}")

        finally:
            page.context.close()

        return sponsors

    def _extract_sponsor(
        self,
        html: str,
        soup: BeautifulSoup,
        pattern_config: dict,
        issue_url: str,
        issue_date: str | None,
    ) -> SponsorInfo | None:
        """Extract a sponsor using the pattern config."""
        pattern = pattern_config.get("pattern", "")
        placement_type = pattern_config.get("placement_type", "unknown")

        match = re.search(pattern, html, re.IGNORECASE)
        if not match:
            return None

        sponsor_name = match.group(1) if match.groups() else match.group(0)
        sponsor_name = clean_text(sponsor_name)
        sponsor_name = re.sub(
            r'\s+(Take|Learn|Get|Read|Check|Click|See|Visit|Discover).*$',
            '', sponsor_name, flags=re.IGNORECASE
        ).strip()

        if not sponsor_name or len(sponsor_name) < 2 or len(sponsor_name) > 50:
            return None

        sponsor_domain = self._find_sponsor_domain(html, sponsor_name, match.start())

        # Extract ad copy with larger window for full context
        ad_copy = self._extract_ad_copy(html, match.start(), match.end(), max_length=1500)

        # Extract product/service info from ad copy (was missing!)
        product_info = self.extract_product_service(ad_copy, sponsor_name)

        # Find landing page URL
        landing_page = self._find_landing_page(html, soup, sponsor_name, match.start())

        return SponsorInfo(
            advertiser_name=sponsor_name,
            advertiser_domain=sponsor_domain,
            placement_type=placement_type,
            ad_copy_snippet=truncate_text(ad_copy, 150),
            full_ad_copy=ad_copy,
            ad_headline=product_info.get("headline"),
            product_service=product_info.get("product_service"),
            call_to_action=product_info.get("call_to_action"),
            landing_page_url=landing_page,
            issue_url=issue_url,
            issue_date=issue_date,
            source_newsletter="morning_brew",
            confidence="high",
        )

    def _find_landing_page(
        self,
        html: str,
        soup: BeautifulSoup,
        sponsor_name: str,
        match_position: int,
    ) -> str | None:
        """Find the main landing page URL for the sponsor."""
        skip_domains = {
            "morningbrew.com", "morning-brew.com", "twitter.com",
            "x.com", "facebook.com", "linkedin.com", "instagram.com",
        }

        window_start = max(0, match_position - 200)
        window_end = min(len(html), match_position + 3000)
        section = html[window_start:window_end]

        section_soup = BeautifulSoup(section, "lxml")

        cta_keywords = [
            "learn more", "get started", "sign up", "download", "try",
            "read more", "discover", "explore", "register", "join",
        ]

        for link in section_soup.find_all("a", href=True):
            href = link.get("href", "")
            text = clean_text(link.get_text()).lower()
            domain = extract_domain(href)

            if not domain or domain in skip_domains:
                continue

            if any(cta in text for cta in cta_keywords):
                return href

            if "utm_" in href.lower() or "?ref=" in href.lower():
                return href

        return None

    def _extract_issue_date(self, soup: BeautifulSoup, issue_url: str) -> str | None:
        """Extract issue date with multiple fallback strategies."""
        # Strategy 1: Try common meta tags
        date_selectors = [
            ('meta[property="article:published_time"]', "content"),
            ('meta[property="og:published_time"]', "content"),
            ('meta[name="date"]', "content"),
            ('meta[name="pubdate"]', "content"),
            ('meta[name="publish-date"]', "content"),
            ('time[datetime]', "datetime"),
            ('time', "datetime"),
        ]

        for selector, attr in date_selectors:
            element = soup.select_one(selector)
            if element:
                date_str = element.get(attr)
                if date_str:
                    parsed = self._parse_date_string(date_str)
                    if parsed:
                        return parsed

        # Strategy 2: Look for date patterns in visible text
        date_containers = soup.select('[class*="date"], [class*="time"], [class*="publish"]')
        for container in date_containers:
            text = container.get_text()
            parsed = self._parse_date_string(text)
            if parsed:
                return parsed

        # Strategy 3: Extract from URL
        # Patterns: /issues/slug-2026-01-15, /issues/2026/01/15/slug, etc.
        url_patterns = [
            r'/(\d{4})-(\d{2})-(\d{2})/',
            r'/(\d{4})/(\d{2})/(\d{2})/',
            r'-(\d{4})(\d{2})(\d{2})(?:[/-]|$)',
            r'(\d{4})-(\d{2})-(\d{2})',
        ]
        for pattern in url_patterns:
            match = re.search(pattern, issue_url)
            if match:
                try:
                    year, month, day = match.groups()
                    return f"{year}-{month}-{day}"
                except (ValueError, IndexError):
                    continue

        # Strategy 4: Look for date in page text content
        body_text = soup.get_text()
        date_patterns = [
            # "January 15, 2026" or "Jan 15, 2026"
            r'((?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[.,]?\s+\d{1,2}[,.]?\s+\d{4})',
            # "15 January 2026"
            r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, body_text, re.IGNORECASE)
            if match:
                parsed = self._parse_date_string(match.group(1))
                if parsed:
                    return parsed

        return None

    def _parse_date_string(self, date_str: str | None) -> str | None:
        """Parse various date string formats to YYYY-MM-DD."""
        if not date_str:
            return None

        date_str = date_str.strip()

        # ISO format with timezone
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

        # Common date formats
        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d",
            "%B %d, %Y",
            "%B %d %Y",
            "%b %d, %Y",
            "%b %d %Y",
            "%d %B %Y",
            "%m/%d/%Y",
            "%m-%d-%Y",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None
