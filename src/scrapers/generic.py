"""
Generic Newsletter Scraper — works with ANY newsletter.

Uses a two-phase approach:
1. Archive Discovery: Playwright crawls a newsletter domain, finds archive/issue pages
2. Sponsor Extraction: Claude AI identifies sponsors from each issue's content

This replaces the need for newsletter-specific scrapers for new sources.
Existing Healthcare Brew / Morning Brew scrapers still work as before.
"""

import re
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlaywrightTimeout
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import BaseScraper, SponsorInfo, NewsletterConfig
from ..utils.helpers import (
    extract_domain,
    clean_text,
    normalize_company_name,
    truncate_text,
    resolve_redirect_url,
    guess_domain_from_name,
    is_tracking_domain,
)

logger = logging.getLogger(__name__)


class GenericNewsletterScraper(BaseScraper):
    """
    Generic newsletter scraper that works with any newsletter domain.

    Uses Claude AI for:
    - Identifying archive/issue listing pages
    - Extracting sponsor information from newsletter content

    Usage:
        config = {
            "name": "My Newsletter",
            "archive_url": "https://newsletter.example.com/archive",
        }
        with GenericNewsletterScraper(config) as scraper:
            issues = scraper.discover_all_issues(limit=20)
            for url in issues:
                sponsors = scraper.scrape_issue(url)
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        domain: str | None = None,
        archive_url: str | None = None,
        name: str | None = None,
        headless: bool = True,
        timeout: int = 30000,
        log_callback: callable = None,
        cancel_check: callable = None,
    ):
        """
        Initialize generic scraper.

        Can be initialized with either:
        - A config dict (like existing scrapers)
        - Just a domain (will auto-discover archive)
        - A specific archive_url

        Args:
            config: Newsletter configuration dict
            domain: Newsletter domain (e.g., "newsletter.example.com")
            archive_url: Direct URL to the archive page
            name: Newsletter name for display
            headless: Run browser headless
            timeout: Page timeout in ms
            log_callback: Optional logging callback
            cancel_check: Optional cancellation check callback
        """
        if config is None:
            config = {}

        # Allow domain/archive_url/name overrides
        if domain:
            if not archive_url:
                archive_url = f"https://{domain}"
            config.setdefault("name", name or domain.split(".")[0].title())
            config.setdefault("archive_url", archive_url)
        elif archive_url:
            parsed = urlparse(archive_url)
            config.setdefault("name", name or parsed.netloc.split(".")[0].title())
            config.setdefault("archive_url", archive_url)

        config.setdefault("issue_url_pattern", "")
        config.setdefault("name", "Generic Newsletter")
        config.setdefault("archive_url", "")

        super().__init__(config, headless=headless, timeout=timeout)
        self._log_callback = log_callback
        self._cancel_check = cancel_check
        self._domain = domain or urlparse(config.get("archive_url", "")).netloc

    def _log(self, message: str, level: str = "info"):
        if self._log_callback:
            self._log_callback(message, level)
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _is_cancelled(self) -> bool:
        if self._cancel_check:
            return self._cancel_check()
        return False

    def discover_all_issues(self, limit: int | None = None) -> list[str]:
        """
        Discover newsletter issue URLs from the archive page.

        Strategy:
        1. Load the archive URL
        2. Look for common archive patterns (links to issues)
        3. Use Claude to identify issue links if patterns don't work
        4. Scroll/paginate to find more

        Args:
            limit: Max issues to return

        Returns:
            List of issue URLs
        """
        archive_url = self.config.archive_url
        if not archive_url:
            self._log("No archive URL configured", "error")
            return []

        self._log(f"Discovering issues from {archive_url}...")
        page = self._new_page()

        try:
            html = self._load_page(page, archive_url)

            # Only look for a different archive page if the user didn't
            # already give us a URL that looks like an archive
            current_path = urlparse(archive_url).path.lower()
            is_already_archive = any(
                kw in current_path
                for kw in ["/archive", "/issues", "/past-issues", "/all-issues", "/editions"]
            )

            if not is_already_archive:
                archive_page_url = self._find_archive_page(html, archive_url)
                if archive_page_url and archive_page_url != archive_url:
                    self._log(f"Found archive page: {archive_page_url}")
                    html = self._load_page(page, archive_page_url)
            else:
                archive_page_url = None

            # Extra wait for JS-heavy pages (like Morning Brew)
            page.wait_for_timeout(2000)

            # Scroll to load more content (generous wait for lazy loading)
            self._scroll_to_load_all(page, max_scrolls=30, wait_ms=1500)

            # Try clicking "Load More" / "Show More" buttons
            self._click_load_more(page)

            # If we clicked load-more, scroll again to catch new content
            self._scroll_to_load_all(page, max_scrolls=10, wait_ms=1500)

            # Re-get HTML after scrolling/clicking
            html = page.content()

            # Extract issue links
            effective_url = archive_page_url or archive_url
            issues = self._extract_issue_links(html, effective_url)

            if not issues:
                self._log("No issues found via pattern matching, trying Claude...", "warning")
                issues = self._discover_issues_with_claude(html, effective_url)

            # Deduplicate and limit
            seen = set()
            unique_issues = []
            for url in issues:
                if url not in seen:
                    seen.add(url)
                    unique_issues.append(url)

            if limit:
                unique_issues = unique_issues[:limit]

            self._log(f"Found {len(unique_issues)} issue URLs")
            return unique_issues

        except Exception as e:
            self._log(f"Error discovering issues: {e}", "error")
            return []
        finally:
            page.context.close()

    def _find_archive_page(self, html: str, base_url: str) -> str | None:
        """Look for a link to an archive/all-issues page (not individual issues)."""
        soup = BeautifulSoup(html, "lxml")
        # Only match archive listing pages, NOT individual issue URLs
        archive_patterns = [
            r"/archive", r"/past-issues", r"/all-issues",
            r"/newsletter/archive", r"/newsletters", r"/editions",
            r"/back-issues", r"/previous", r"/history",
        ]

        # Patterns that indicate an individual issue (not an archive listing)
        individual_issue_indicators = re.compile(
            r'/(?:issues?|p|posts?)/[a-z0-9][\w-]{5,}', re.IGNORECASE
        )

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            text = a.get_text().lower().strip()
            full_url = urljoin(base_url, href)

            # Skip if this looks like a single issue URL
            if individual_issue_indicators.search(href):
                continue

            # Check URL patterns
            for pattern in archive_patterns:
                if pattern in href.lower():
                    return full_url

            # Check link text
            if any(kw in text for kw in ["archive", "all issues", "past issues", "previous issues", "back issues"]):
                return full_url

        return None

    def _click_load_more(self, page: Page, max_clicks: int = 10):
        """Click Load More / Show More buttons to reveal all issues."""
        load_more_selectors = [
            "button:has-text('Load More')",
            "button:has-text('Show More')",
            "button:has-text('More Issues')",
            "a:has-text('Load More')",
            "a:has-text('Show More')",
            "button:has-text('View More')",
            "[class*='load-more']",
            "[class*='show-more']",
            "[data-action*='load-more']",
        ]

        for _ in range(max_clicks):
            clicked = False
            for selector in load_more_selectors:
                try:
                    btn = page.query_selector(selector)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1500)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                break

    def _extract_issue_links(self, html: str, base_url: str) -> list[str]:
        """
        Extract issue links using common newsletter archive patterns.

        Uses two strategies:
        1. Regex patterns for known issue URL structures
        2. Heuristic: find the most common link path prefix (archives tend to
           have many links sharing the same prefix like /daily/issues/)
        """
        soup = BeautifulSoup(html, "lxml")
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc

        # Common issue URL patterns (applied via search, so they match anywhere in the path)
        issue_patterns = [
            # Date-based: /2024/01/15/title or /issues/2024-01-15
            re.compile(r'/\d{4}/\d{1,2}/\d{1,2}/'),
            re.compile(r'/\d{4}-\d{1,2}-\d{1,2}'),
            # Issue/edition/post slugs (with optional path prefix like /daily/issues/slug)
            re.compile(r'/(?:issues?|editions?|posts?|p|newsletters?)/[a-z0-9][\w-]{3,}', re.IGNORECASE),
            # Substack pattern: /p/slug-title
            re.compile(r'/p/[\w-]+'),
            # Ghost/WordPress pattern
            re.compile(r'/(?:ghost|blog)/[\w-]+'),
        ]

        skip_patterns = [
            "/tag/", "/category/", "/author/", "/page/",
            "/login", "/signup", "/subscribe", "/account",
            "/about", "/contact", "/privacy", "/terms",
            "/search", "/feed", "/rss", "/archive",
        ]

        # Collect all same-domain internal links with their paths
        all_internal_links = []  # (full_url, path)
        seen = set()

        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            # Must be same domain (or subdomain)
            if not parsed.netloc.endswith(base_domain.replace("www.", "")):
                continue

            # Skip obvious non-issue pages
            if any(skip in parsed.path.lower() for skip in skip_patterns):
                continue

            # Skip very short paths (like "/" or "/daily")
            path = parsed.path.rstrip("/")
            if path.count("/") < 2:
                continue

            if full_url not in seen:
                seen.add(full_url)
                all_internal_links.append((full_url, path))

        # Strategy 1: Pattern matching
        issue_urls = []
        pattern_matched = set()

        for full_url, path in all_internal_links:
            for pattern in issue_patterns:
                if pattern.search(path):
                    pattern_matched.add(full_url)
                    issue_urls.append(full_url)
                    break

        # Strategy 2: Path prefix heuristic
        # If many links share the same prefix (e.g., /daily/issues/), those are likely issues
        if len(issue_urls) < 5:
            prefix_counts = {}
            for full_url, path in all_internal_links:
                # Extract prefix: everything up to and including the second-to-last "/"
                # e.g., "/daily/issues/some-slug" -> "/daily/issues/"
                parts = path.rstrip("/").rsplit("/", 1)
                if len(parts) == 2 and parts[0]:
                    prefix = parts[0] + "/"
                    if prefix not in prefix_counts:
                        prefix_counts[prefix] = []
                    prefix_counts[prefix].append(full_url)

            # Find the most common prefix with at least 3 links
            best_prefix = None
            best_count = 0
            for prefix, urls in prefix_counts.items():
                if len(urls) > best_count and len(urls) >= 3:
                    best_count = len(urls)
                    best_prefix = prefix

            if best_prefix and best_count > len(issue_urls):
                self._log(f"Heuristic: found {best_count} links with prefix '{best_prefix}'")
                # Use prefix-based links, adding any not already found
                for full_url in prefix_counts[best_prefix]:
                    if full_url not in pattern_matched:
                        issue_urls.append(full_url)

        return issue_urls

    def _discover_issues_with_claude(self, html: str, archive_url: str) -> list[str]:
        """Use Claude to identify issue links when pattern matching fails."""
        try:
            from ..enrichment.claude_agent import ClaudeAgent

            agent = ClaudeAgent()
            if not agent.is_configured:
                return []

            # Clean HTML to reduce tokens
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            # Extract all links for Claude to analyze
            links = []
            for a in soup.find_all("a", href=True):
                href = a.get("href", "").strip()
                text = clean_text(a.get_text())[:100]
                if href and not href.startswith(("#", "javascript:", "mailto:")):
                    full_url = urljoin(archive_url, href)
                    links.append(f"{text} -> {full_url}")

            if not links:
                return []

            links_text = "\n".join(links[:200])  # Limit to 200 links

            system_prompt = """You are analyzing a newsletter archive page.
Your task: identify which links point to individual newsletter issues/editions.

Respond ONLY with a JSON array of URLs that are newsletter issue pages:
["https://example.com/issues/my-issue", "https://example.com/p/another-issue"]

Newsletter issues typically:
- Have date-based or slug-based URLs
- Link text contains issue titles, dates, or topics
- Are NOT category/tag/author/about/contact pages

Return [] if no issue links found. Return maximum 100 URLs."""

            user_prompt = f"""Archive URL: {archive_url}

Links found on page:
{links_text}

Which of these are links to individual newsletter issues?"""

            response = agent._call_api(system_prompt, user_prompt, max_tokens=2000)
            agent.close()

            if response:
                from ..enrichment.claude_agent import extract_json_from_response
                result = extract_json_from_response(response)
                if isinstance(result, list):
                    return [url for url in result if isinstance(url, str)]

        except Exception as e:
            self._log(f"Claude issue discovery failed: {e}", "warning")

        return []

    def scrape_issue(self, issue_url: str) -> list[SponsorInfo]:
        """
        Scrape a single newsletter issue for sponsors.

        Uses a hybrid approach:
        1. Try regex pattern matching first (fast, free)
        2. Use Claude for intelligent extraction (catches more sponsors)

        Args:
            issue_url: URL of the newsletter issue

        Returns:
            List of SponsorInfo objects
        """
        if self._is_cancelled():
            return []

        page = self._new_page()
        sponsors = []

        try:
            html = self._load_page(page, issue_url)

            # Extract issue date from URL or page
            issue_date = self._extract_issue_date(html, issue_url)

            # Phase 1: Regex-based detection (fast, free)
            regex_sponsors = self.auto_detect_sponsors(html, issue_url)
            for s in regex_sponsors:
                s.issue_date = issue_date

            # Phase 2: Claude-based detection (catches more sponsors)
            claude_sponsors = self._extract_sponsors_with_claude(html, issue_url, issue_date)

            # Merge: deduplicate by company name
            seen_names = set()
            for s in regex_sponsors:
                key = normalize_company_name(s.advertiser_name)
                if key not in seen_names:
                    seen_names.add(key)
                    sponsors.append(s)

            for s in claude_sponsors:
                key = normalize_company_name(s.advertiser_name)
                if key not in seen_names:
                    seen_names.add(key)
                    sponsors.append(s)

        except Exception as e:
            self._log(f"Error scraping {issue_url}: {e}", "error")
        finally:
            page.context.close()

        return sponsors

    def _extract_sponsors_with_claude(
        self,
        html: str,
        issue_url: str,
        issue_date: str | None,
    ) -> list[SponsorInfo]:
        """Use Claude to identify sponsors in newsletter content."""
        try:
            from ..enrichment.claude_agent import ClaudeAgent, extract_json_from_response

            agent = ClaudeAgent()
            if not agent.is_configured:
                return []

            # Clean HTML to reduce tokens
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript", "head"]):
                tag.decompose()

            # Get clean text
            text = soup.get_text(separator="\n")
            # Also get link context
            links_context = []
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                link_text = clean_text(a.get_text())
                if link_text and href:
                    links_context.append(f"[{link_text}]({href})")

            links_text = "\n".join(links_context[:200])

            system_prompt = """You are an expert at identifying sponsors and advertisers in newsletter emails.

Analyze the newsletter issue and find ALL sponsors/advertisers. Look for:
1. "Presented by", "Sponsored by", "Together with", "Brought to you by" sections
2. Clearly marked ad sections or sponsored content blocks
3. "A message from [Company]" or "A word from [Company]" blocks
4. Partner/sponsor logos or callouts
5. Sections that pitch a product or service with a call-to-action (CTA) link
6. Native advertising — advertorial content that promotes a product/service
7. Affiliate product recommendations with tracking links
8. Short ad blocks between editorial sections (common in daily digest newsletters)

IMPORTANT: Many newsletters have 2-5 sponsors per issue placed between editorial sections.
Each sponsor section typically has: a company name, a pitch paragraph, and a CTA link.
Do NOT skip sponsor blocks just because they look like editorial content — if they
promote a specific product/service with a link, they are likely sponsors.

For each sponsor, extract ALL of these fields:
- company_name: The advertiser's name (the company being promoted, NOT the newsletter)
- placement_type: One of: presented_by, together_with, sponsored_by, partnership, sponsored_message, powered_by, native_ad, affiliate_link, inline_mention
- ad_headline: The main headline or hook of the ad (the attention-grabbing first line)
- product_service: What specific product, service, or offer they're promoting (be specific, e.g., "AI-powered CRM platform", "Employee wellness program", "Marketing analytics tool")
- category: The industry/sector (one of: technology, healthcare, finance, marketing, hr/recruiting, saas, ecommerce, education, media, consulting, other)
- ad_copy: The FULL ad text/copy (include the entire sponsor section text)
- call_to_action: The CTA text if present (e.g., "Learn More", "Get Started", "Try Free")
- landing_url: The URL the ad links to (if found in the links list)

Respond ONLY with valid JSON:
{
    "sponsors": [
        {
            "company_name": "Example Corp",
            "placement_type": "presented_by",
            "ad_headline": "Transform your workflow with AI",
            "product_service": "AI-powered project management platform",
            "category": "technology",
            "ad_copy": "Full ad copy text here...",
            "call_to_action": "Start Free Trial",
            "landing_url": "https://example.com/landing"
        }
    ]
}

Return {"sponsors": []} if no sponsors found. Do NOT hallucinate sponsors."""

            # Send more content to Claude — newsletters can be long
            content_preview = text[:10000]
            if len(text) > 10000:
                content_preview += "\n...[middle content truncated]...\n" + text[-3000:]

            user_prompt = f"""Newsletter issue URL: {issue_url}

=== NEWSLETTER TEXT CONTENT ===
{content_preview}

=== LINKS FOUND IN ISSUE ===
{links_text}

Identify all sponsors and advertisers in this newsletter issue. Look carefully for ad blocks between editorial sections."""

            response = agent._call_api(system_prompt, user_prompt, max_tokens=3000)
            agent.close()

            if not response:
                return []

            data = extract_json_from_response(response)
            if not data or not isinstance(data, dict):
                return []

            sponsors = []
            for item in data.get("sponsors", []):
                name = item.get("company_name", "").strip()
                if not name or len(name) < 2:
                    continue

                # Try to resolve domain from landing URL or name
                landing_url = item.get("landing_url")
                domain = None
                if landing_url:
                    if is_tracking_domain(landing_url):
                        resolved = resolve_redirect_url(landing_url, timeout=4.0)
                        if resolved and resolved != landing_url:
                            domain = extract_domain(resolved)
                            # Check if we still got a tracking domain (resolution failed/partial)
                            if domain and is_tracking_domain(f"https://{domain}"):
                                domain = None  # Discard, will fall back to search
                    else:
                        domain = extract_domain(landing_url)

                # Fallback 1: Web search for company domain (more accurate than guessing)
                if not domain and name:
                    try:
                        from ..enrichment.website_scraper import _search_for_domain
                        searched_domain = _search_for_domain(name, timeout=3.0)
                        if searched_domain:
                            self._log(f"    Web search found domain for {name}: {searched_domain}")
                            domain = searched_domain
                    except Exception:
                        pass

                # Fallback 2: Guess from company name (fast but less accurate)
                if not domain:
                    domain = guess_domain_from_name(name)

                sponsors.append(SponsorInfo(
                    advertiser_name=name,
                    advertiser_domain=domain,
                    placement_type=item.get("placement_type", "unknown"),
                    ad_copy_snippet=truncate_text(item.get("ad_copy", ""), 200),
                    issue_url=issue_url,
                    issue_date=issue_date,
                    source_newsletter=self.config.name.lower().replace(" ", "_"),
                    category=item.get("category", "other"),
                    confidence="high",
                    landing_page_url=landing_url,
                    product_service=item.get("product_service"),
                    ad_headline=item.get("ad_headline"),
                    call_to_action=item.get("call_to_action"),
                    full_ad_copy=item.get("ad_copy", ""),
                ))

            return sponsors

        except Exception as e:
            self._log(f"Claude sponsor extraction failed: {e}", "warning")
            return []

    def _extract_issue_date(self, html: str, url: str) -> str | None:
        """Try to extract the issue date from URL or page content."""
        # Try URL date patterns
        date_patterns = [
            # /2024/01/15/
            (r'/(\d{4})/(\d{1,2})/(\d{1,2})/', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
            # /2024-01-15
            (r'/(\d{4}-\d{1,2}-\d{1,2})', lambda m: m.group(1)),
        ]

        for pattern, formatter in date_patterns:
            match = re.search(pattern, url)
            if match:
                return formatter(match)

        # Try page content for date meta tags
        soup = BeautifulSoup(html[:5000], "lxml")

        # Check meta tags
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "") or meta.get("name", "")
            if "date" in prop.lower() or "published" in prop.lower():
                content = meta.get("content", "")
                if content and len(content) >= 10:
                    return content[:10]

        # Check time tags
        time_tag = soup.find("time")
        if time_tag:
            dt = time_tag.get("datetime", "")
            if dt:
                return dt[:10]

        return None

    @classmethod
    def from_domain(cls, domain: str, **kwargs) -> "GenericNewsletterScraper":
        """
        Create a scraper from just a domain name.

        Will auto-discover the archive page.

        Args:
            domain: Newsletter domain (e.g., "morningbrew.com")
            **kwargs: Additional arguments passed to constructor
        """
        # Clean domain
        if domain.startswith(("http://", "https://")):
            parsed = urlparse(domain)
            domain = parsed.netloc
        if domain.startswith("www."):
            domain = domain[4:]

        return cls(
            domain=domain,
            archive_url=f"https://{domain}",
            name=domain.split(".")[0].replace("-", " ").title(),
            **kwargs,
        )
