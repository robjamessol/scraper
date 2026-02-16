"""Generic newsletter scraper that works with any website domain."""

import re
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import httpx

from .base import BaseScraper, SponsorInfo, AffiliateLink, AFFILIATE_NETWORKS
from ..utils.helpers import (
    extract_domain,
    clean_text,
    normalize_company_name,
    truncate_text,
    resolve_redirect_url,
    is_tracking_domain,
    is_tracking_link,
    guess_domain_from_name,
    get_http_client,
    extract_links_from_html,
)


logger = logging.getLogger(__name__)


# Common archive/newsletter paths to probe on any domain
ARCHIVE_PATHS = [
    "/email-archive",
    "/email-archive/",
    "/archive",
    "/archive/",
    "/newsletter/archive",
    "/newsletter/archive/",
    "/newsletters",
    "/newsletters/",
    "/newsletter",
    "/newsletter/",
    "/past-issues",
    "/past-issues/",
    "/issues",
    "/issues/",
    "/emails",
    "/emails/",
    "/blog",
    "/blog/",
    "/articles",
    "/articles/",
    "/posts",
    "/posts/",
]

# URL path segments that indicate non-content pages (should be excluded from issue lists)
EXCLUDE_PATH_KEYWORDS = {
    "login", "logout", "signup", "sign-up", "register", "subscribe",
    "account", "settings", "profile", "preferences", "members",
    "contact", "about", "privacy", "terms", "cookie", "disclaimer",
    "faq", "help", "support", "search", "cart", "checkout",
    "sitemap", "robots", "feed", "rss", "xml", "json",
    "wp-admin", "wp-login", "wp-content", "wp-includes",
    "tag/", "category/", "author/", "page/",
    "cdn-cgi", "assets", "static", "media", "images",
    ".pdf", ".jpg", ".png", ".gif", ".svg", ".css", ".js",
    "do-not-share", "disclosures", "comment-policy",
    "thank-you", "thanks", "confirmation", "download",
}

# Patterns that suggest a URL is a newsletter issue or content post
CONTENT_URL_PATTERNS = [
    # Date-based patterns in URLs
    r'/\d{4}/\d{2}/',                         # /2024/01/
    r'/\d{4}-\d{2}-\d{2}',                    # /2024-01-15
    r'email.?roundup',                         # email-roundup-*
    r'research.?worth.?sharing',               # research-worth-sharing-*
    r'worth.?(?:sharing|checking)',            # worth-sharing-*, worth-checking-out-*
    r'weekly.?(?:digest|update|roundup|recap)', # weekly-digest-*
    r'monthly.?(?:digest|update|roundup|recap)', # monthly-digest-*
    r'newsletter.?\d',                         # newsletter-123
    r'issue.?\d',                              # issue-123
    r'edition.?\d',                            # edition-123
    r'vol(?:ume)?.?\d',                        # vol-1, volume-2
    r'roundup',                                # *-roundup-*
    r'(?:january|february|march|april|may|june|july|august|september|october|november|december).?\d{4}',  # month-2024
]


class GenericNewsletterScraper(BaseScraper):
    """
    Universal newsletter scraper that works with any website domain.

    Uses multiple strategies to discover archive pages and newsletter issues:
    1. Probe common archive URL paths
    2. Parse sitemap.xml for content URLs
    3. Crawl the homepage/archive for links
    4. Score and filter discovered URLs to find newsletter issues

    Sponsor extraction uses the generic auto_detect_sponsors() method
    from BaseScraper, which matches common sponsor/ad patterns in any
    newsletter content.
    """

    def __init__(
        self,
        domain: str,
        config: dict[str, Any] | None = None,
        log_callback=None,
        **kwargs,
    ):
        """
        Initialize generic scraper for any domain.

        Args:
            domain: The website domain (e.g., "peterattiamd.com")
            config: Optional config override
            log_callback: Optional callback for live logging (used by web app)
            **kwargs: Additional arguments passed to BaseScraper
        """
        # Normalize domain
        self.domain = domain.lower().strip()
        if self.domain.startswith(("http://", "https://")):
            parsed = urlparse(self.domain)
            self.domain = parsed.netloc
        if self.domain.startswith("www."):
            self.domain = self.domain[4:]

        self.base_url = f"https://{self.domain}"
        self.log_callback = log_callback

        if config is None:
            config = self._build_config()

        super().__init__(config, **kwargs)

    def _log(self, message: str, level: str = "info"):
        """Log a message via callback and standard logger."""
        if self.log_callback:
            self.log_callback(message, level)
        if level == "error":
            logger.error(message)
        else:
            logger.info(message)

    def _build_config(self) -> dict[str, Any]:
        """Build a config dict for this domain."""
        return {
            "name": self.domain,
            "archive_url": self.base_url,  # Will be discovered dynamically
            "issue_url_pattern": "",
            "requires_js": True,
            "active": True,
            "sponsor_patterns": {},  # Will use auto_detect_sponsors instead
            "selectors": {
                "issue_content": "article, .post-content, .entry-content, .newsletter-content, main, body",
            },
        }

    # ===== ARCHIVE DISCOVERY =====

    def _fetch_sitemap_urls(self) -> list[str]:
        """
        Fetch and parse sitemap.xml to find content URLs.

        Handles both sitemap indexes and regular sitemaps.

        Returns:
            List of content URLs found in sitemaps
        """
        urls = []
        sitemap_urls_to_check = [
            f"{self.base_url}/sitemap.xml",
            f"{self.base_url}/sitemap_index.xml",
            f"{self.base_url}/wp-sitemap.xml",
        ]

        try:
            client = get_http_client()

            for sitemap_url in sitemap_urls_to_check:
                try:
                    response = client.get(sitemap_url, timeout=5.0)
                    if response.status_code != 200:
                        continue

                    content = response.text

                    # Check if this is a sitemap index (contains other sitemaps)
                    if "<sitemapindex" in content:
                        # Extract child sitemap URLs - prefer post sitemaps
                        child_sitemaps = re.findall(r'<loc>(.*?)</loc>', content)
                        # Prioritize post/article sitemaps
                        priority_sitemaps = [
                            s for s in child_sitemaps
                            if any(kw in s.lower() for kw in ["post", "article", "page", "newsletter"])
                        ]
                        other_sitemaps = [s for s in child_sitemaps if s not in priority_sitemaps]

                        for child_url in (priority_sitemaps + other_sitemaps)[:5]:
                            try:
                                child_resp = client.get(child_url, timeout=5.0)
                                if child_resp.status_code == 200:
                                    child_urls = re.findall(r'<loc>(.*?)</loc>', child_resp.text)
                                    urls.extend(child_urls)
                            except Exception:
                                continue
                    else:
                        # Regular sitemap - extract URLs directly
                        found = re.findall(r'<loc>(.*?)</loc>', content)
                        urls.extend(found)

                    if urls:
                        break  # Found a working sitemap

                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Error fetching sitemaps for {self.domain}: {e}")

        # Filter to only URLs from this domain
        domain_urls = [u for u in urls if self.domain in u.lower()]
        logger.info(f"Sitemap: found {len(domain_urls)} URLs for {self.domain}")
        return domain_urls

    def _probe_archive_pages(self, page) -> list[dict]:
        """
        Probe common archive paths to find ones that exist and have content.

        Args:
            page: Playwright page instance

        Returns:
            List of dicts with 'url', 'link_count', and 'links' for each
            archive page that returned content.
        """
        results = []

        for path in ARCHIVE_PATHS:
            url = f"{self.base_url}{path}"
            try:
                response = page.goto(url, wait_until="networkidle", timeout=15000)
                if not response or response.status >= 400:
                    continue

                page.wait_for_timeout(2000)

                # Count links on the page that point to content on this domain
                html = page.content()
                soup = BeautifulSoup(html, "lxml")
                content_links = self._extract_content_links(soup)

                if content_links:
                    results.append({
                        "url": url,
                        "path": path,
                        "link_count": len(content_links),
                        "links": content_links,
                    })
                    logger.info(f"Archive probe: {path} -> {len(content_links)} content links")

            except Exception as e:
                logger.debug(f"Archive probe failed for {path}: {e}")
                continue

        # Sort by number of content links (more links = more likely the archive)
        results.sort(key=lambda x: x["link_count"], reverse=True)
        return results

    def _crawl_homepage_for_archive(self, page) -> list[str]:
        """
        Crawl the homepage to find links to archive or newsletter pages.

        Args:
            page: Playwright page instance

        Returns:
            List of discovered content URLs
        """
        content_urls = []

        try:
            page.goto(self.base_url, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(2000)

            html = page.content()
            soup = BeautifulSoup(html, "lxml")

            # Look for navigation links that might point to archive/newsletter
            nav_keywords = [
                "archive", "newsletter", "email", "past issue", "all issue",
                "blog", "articles", "posts", "digest", "weekly", "latest",
            ]

            for link in soup.find_all("a", href=True):
                href = link.get("href", "")
                text = clean_text(link.get_text()).lower()

                # Check if link text or href suggests archive/newsletter content
                is_nav_link = any(kw in text for kw in nav_keywords) or \
                              any(kw in href.lower() for kw in nav_keywords)

                if is_nav_link:
                    full_url = urljoin(self.base_url, href)
                    if self.domain in full_url.lower():
                        content_urls.append(full_url)

            # Also extract any content-looking links directly from homepage
            content_links = self._extract_content_links(soup)
            content_urls.extend(content_links)

        except Exception as e:
            logger.debug(f"Homepage crawl failed: {e}")

        return list(set(content_urls))

    def _extract_content_links(self, soup: BeautifulSoup) -> list[str]:
        """
        Extract links that look like newsletter issues or content posts.

        Filters out navigation, login, static asset, and other non-content links.

        Args:
            soup: Parsed page

        Returns:
            List of content URLs
        """
        content_links = []

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")

            # Build full URL
            if href.startswith("/"):
                full_url = urljoin(self.base_url, href)
            elif href.startswith("http"):
                full_url = href
            else:
                continue

            # Must be on our domain
            if self.domain not in full_url.lower():
                continue

            # Parse the path
            parsed = urlparse(full_url)
            path = parsed.path.lower().strip("/")

            # Skip empty paths (homepage)
            if not path:
                continue

            # Skip excluded paths
            if any(exc in path for exc in EXCLUDE_PATH_KEYWORDS):
                continue

            # Skip very short paths that are likely section pages, not content
            # e.g., /about, /blog (but keep /blog/some-article)
            segments = [s for s in path.split("/") if s]
            if len(segments) == 1 and len(segments[0]) < 15:
                # Short single-segment paths are usually section pages
                # Unless they look like content (have date patterns, numbers, etc.)
                if not re.search(r'\d{4}|\d+-', segments[0]):
                    continue

            # Normalize to avoid duplicates
            normalized = full_url.rstrip("/")
            if normalized not in content_links:
                content_links.append(normalized)

        return content_links

    def _score_url_as_newsletter(self, url: str) -> float:
        """
        Score how likely a URL is to be a newsletter issue (0-1).

        Higher scores mean more likely to be a newsletter/email issue.

        Args:
            url: URL to score

        Returns:
            Score from 0.0 to 1.0
        """
        score = 0.0
        path = urlparse(url).path.lower()

        # Bonus for matching content URL patterns
        for pattern in CONTENT_URL_PATTERNS:
            if re.search(pattern, path):
                score += 0.3
                break

        # Bonus for date-like slugs
        if re.search(r'\d{4}', path):
            score += 0.1

        # Bonus for newsletter-related keywords in path
        newsletter_keywords = [
            "email", "newsletter", "issue", "edition", "digest",
            "roundup", "recap", "weekly", "monthly", "update",
        ]
        if any(kw in path for kw in newsletter_keywords):
            score += 0.3

        # Bonus for longer slugs (content tends to have descriptive slugs)
        slug = path.strip("/").split("/")[-1] if "/" in path else path.strip("/")
        if len(slug) > 20:
            score += 0.1

        # Penalty for very short paths
        if len(path.strip("/")) < 10:
            score -= 0.2

        return max(0.0, min(1.0, score))

    # ===== MAIN INTERFACE =====

    def discover_all_issues(self, limit: int | None = None) -> list[str]:
        """
        Discover newsletter issue URLs using multiple strategies.

        Strategy order:
        1. Fetch sitemap.xml for all content URLs
        2. Probe common archive paths with browser
        3. Crawl homepage for content links
        4. If archive pages found, load them and scrape for more links
        5. Score and rank all discovered URLs

        Args:
            limit: Maximum number of issues to return

        Returns:
            List of discovered issue URLs, ranked by relevance
        """
        page = self._new_page()
        all_urls = set()

        try:
            # Strategy 1: Sitemap
            self._log(f"    Checking sitemaps for {self.domain}...")
            sitemap_urls = self._fetch_sitemap_urls()
            for url in sitemap_urls:
                # Quick filter - skip obviously non-content URLs
                path = urlparse(url).path.lower()
                if not any(exc in path for exc in EXCLUDE_PATH_KEYWORDS):
                    all_urls.add(url.rstrip("/"))
            self._log(f"    Sitemap: {len(all_urls)} candidate URLs")

            # Strategy 2: Probe archive paths
            self._log(f"    Probing archive paths for {self.domain}...")
            archive_results = self._probe_archive_pages(page)
            for result in archive_results:
                for link_url in result["links"]:
                    all_urls.add(link_url.rstrip("/"))
                self._log(f"    Archive '{result['path']}': {result['link_count']} links")

            # If archive pages found with good content, try scrolling/loading more
            if archive_results:
                best_archive = archive_results[0]
                self._log(f"    Best archive: {best_archive['path']} ({best_archive['link_count']} links)")

                try:
                    page.goto(best_archive["url"], wait_until="networkidle", timeout=15000)
                    page.wait_for_timeout(2000)

                    # Try loading more content
                    self._try_load_more_content(page)

                    # Re-extract links after loading more
                    html = page.content()
                    soup = BeautifulSoup(html, "lxml")
                    extra_links = self._extract_content_links(soup)
                    for link_url in extra_links:
                        all_urls.add(link_url.rstrip("/"))

                    self._log(f"    After load-more: {len(all_urls)} total candidate URLs")
                except Exception as e:
                    logger.debug(f"Error loading more from archive: {e}")

            # Strategy 3: Crawl homepage
            if len(all_urls) < 10:
                self._log(f"    Crawling homepage for {self.domain}...")
                homepage_urls = self._crawl_homepage_for_archive(page)
                for url in homepage_urls:
                    all_urls.add(url.rstrip("/"))
                self._log(f"    Homepage crawl: found {len(homepage_urls)} additional URLs")

            # Remove the homepage itself
            all_urls.discard(self.base_url)
            all_urls.discard(f"{self.base_url}/")
            all_urls.discard(f"https://www.{self.domain}")
            all_urls.discard(f"https://www.{self.domain}/")

            # Score and rank URLs
            scored_urls = []
            for url in all_urls:
                score = self._score_url_as_newsletter(url)
                scored_urls.append((url, score))

            # Sort by score descending
            scored_urls.sort(key=lambda x: x[1], reverse=True)

            # Take URLs with score > 0 first, then the rest
            issue_urls = [url for url, score in scored_urls if score > 0]
            remaining = [url for url, score in scored_urls if score == 0]

            # If we have very few high-scoring URLs, include some low-scoring ones
            if len(issue_urls) < 5:
                issue_urls.extend(remaining)

            self._log(f"    Discovered {len(issue_urls)} content URLs for {self.domain}")

            # Apply limit
            if limit:
                issue_urls = issue_urls[:limit]
                self._log(f"    Applied limit: {len(issue_urls)} URLs")

        except Exception as e:
            self._log(f"    Error discovering issues for {self.domain}: {e}", level="error")
            raise

        finally:
            page.context.close()

        return issue_urls

    def _try_load_more_content(self, page):
        """
        Try to load more content on the current page via buttons or scrolling.

        Args:
            page: Playwright page instance
        """
        # Try common "Load More" buttons
        load_more_selectors = [
            'button:has-text("Load More")',
            'button:has-text("Show More")',
            'button:has-text("Load more")',
            'button:has-text("Show more")',
            'button:has-text("View More")',
            'button:has-text("See More")',
            'button:has-text("Older")',
            'a:has-text("Load More")',
            'a:has-text("Show More")',
            'a:has-text("View More")',
            'a:has-text("See More")',
            'a:has-text("Older Posts")',
            'a:has-text("Next")',
            '[class*="load-more"]',
            '[class*="show-more"]',
            '[class*="view-more"]',
        ]

        clicked = False
        for _ in range(10):  # Try up to 10 times
            button_found = False
            for selector in load_more_selectors:
                try:
                    button = page.query_selector(selector)
                    if button and button.is_visible():
                        button.click()
                        clicked = True
                        button_found = True
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue

            if not button_found:
                break

        # If no button found, try scroll loading
        if not clicked:
            self._scroll_to_load_all(page, max_scrolls=20, wait_ms=1000)

    # ===== SPONSOR / ADVERTISER EXTRACTION =====

    # Domains to always skip (not advertisers)
    SOCIAL_AND_UTILITY_DOMAINS = {
        # Social media
        "twitter.com", "x.com", "facebook.com", "linkedin.com",
        "instagram.com", "youtube.com", "youtu.be", "google.com",
        "google.co.uk", "pinterest.com", "reddit.com", "tiktok.com",
        "threads.net", "mastodon.social", "snapchat.com",
        # App stores & music
        "apple.com", "apps.apple.com", "play.google.com",
        "spotify.com", "open.spotify.com", "music.apple.com",
        "itunes.apple.com", "podcasts.apple.com",
        # Email/newsletter platforms
        "substack.com", "beehiiv.com", "convertkit.com",
        "mailchimp.com", "hubspot.com", "constantcontact.com",
        "campaignmonitor.com", "sendinblue.com", "mailerlite.com",
        # CMS / web infrastructure
        "gravatar.com", "wp.com", "wordpress.com", "wordpress.org",
        "w3.org", "schema.org", "creativecommons.org",
        "squarespace.com", "wix.com", "shopify.com", "weebly.com",
        # Dev / code
        "github.com", "gitlab.com", "stackexchange.com",
        "stackoverflow.com", "bitbucket.org",
        # Academic / research
        "en.wikipedia.org", "wikipedia.org",
        "nih.gov", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
        "doi.org", "arxiv.org", "nature.com", "sciencedirect.com",
        "scholar.google.com", "researchgate.net", "jstor.org",
        "pmc.ncbi.nlm.nih.gov",
        # Major news & media (editorial links, not sponsors)
        "nytimes.com", "washingtonpost.com", "wsj.com", "bbc.com",
        "bbc.co.uk", "cnn.com", "reuters.com", "apnews.com",
        "nbc.com", "nbcnews.com", "cbs.com", "cbsnews.com",
        "abc.com", "abcnews.go.com", "fox.com", "foxnews.com",
        "npr.org", "pbs.org", "usatoday.com", "bloomberg.com",
        "forbes.com", "time.com", "theatlantic.com", "wired.com",
        "vice.com", "vox.com", "theguardian.com", "huffpost.com",
        # Streaming / entertainment (editorial references)
        "netflix.com", "hbo.com", "hulu.com", "disneyplus.com",
        "primevideo.com", "peacocktv.com",
        # URL shorteners (handled by tracking resolution)
        "amzn.to", "bit.ly", "t.co", "goo.gl", "ow.ly", "tinyurl.com",
        "buff.ly", "is.gd",
        # Referral/affiliate platforms (handled by affiliate strategy)
        "isrefer.com", "refersion.com", "tapfiliate.com",
        # Ad tech & CDNs
        "cloudflare.com", "jsdelivr.net", "googleapis.com",
        "googlesyndication.com", "googletagmanager.com",
        "doubleclick.net", "googleadservices.com",
        "gstatic.com", "cdnjs.cloudflare.com",
        "googleanalytics.com", "google-analytics.com",
        "facebook.net", "fbcdn.net", "akamaized.net",
        # Government
        "gov", "fda.gov", "cdc.gov", "who.int",
    }

    # CSS class/id patterns that indicate sponsor sections
    SPONSOR_SECTION_PATTERNS = [
        r'sponsor', r'partner', r'advertis', r'promo',
        r'promoted', r'paid', r'branded', r'commercial',
        r'ad-slot', r'ad_slot', r'advert', r'brought-to-you',
    ]

    def _get_skip_domains(self) -> set[str]:
        """Build the dynamic set of domains to skip for this newsletter."""
        skip = set(self.SOCIAL_AND_UTILITY_DOMAINS)
        skip.add(self.domain)
        skip.add(f"www.{self.domain}")
        # Also skip common subdomains of this newsletter
        parts = self.domain.split(".")
        if len(parts) >= 2:
            root = ".".join(parts[-2:])
            skip.add(root)
            skip.add(f"www.{root}")
            # Skip domains sharing the same brand name
            # e.g., for bengreenfieldfitness.com also skip bengreenfield*.com variants
            brand = parts[0]  # e.g., "bengreenfieldfitness"
            # Common name variations: strip common suffixes to get core brand
            for suffix in ["fitness", "life", "health", "media", "news",
                           "daily", "weekly", "blog", "online", "digital",
                           "hq", "co", "inc", "labs", "studio", "group"]:
                if brand.endswith(suffix) and len(brand) > len(suffix) + 2:
                    core = brand[:-len(suffix)]
                    tld = parts[-1]  # e.g., "com"
                    skip.add(f"{core}.{tld}")
                    skip.add(f"www.{core}.{tld}")
        return skip

    def scrape_issue(self, issue_url: str) -> list[SponsorInfo]:
        """
        Scrape a single page for sponsor/advertiser information.

        Uses a comprehensive multi-strategy approach:
        1. Traditional pattern matching (Presented By, Sponsored By, etc.)
        2. Sponsor-labeled HTML sections (CSS classes with sponsor/ad/partner)
        3. UTM-tagged external links (almost always paid placements)
        4. Known affiliate network links
        5. External promotional links (company websites linked with context)

        Args:
            issue_url: URL of the newsletter issue or content page

        Returns:
            List of SponsorInfo objects found
        """
        page = self._new_page()
        sponsors = []
        seen_keys = set()  # Track by domain to avoid duplicates

        try:
            logger.debug(f"Scraping: {issue_url}")

            # Load the page
            html = self._load_page(page, issue_url)
            soup = BeautifulSoup(html, "lxml")

            # Extract issue date
            issue_date = self._extract_issue_date(soup, issue_url)

            # Get the main content area
            content_selectors = self.config.selectors.get(
                "issue_content",
                "article, .post-content, .entry-content, .newsletter-content, main, body"
            ).split(", ")

            content_soup = None
            content_html = ""
            for selector in content_selectors:
                content = soup.select_one(selector)
                if content:
                    content_html = str(content)
                    content_soup = content
                    break

            if not content_html:
                content_html = html
                content_soup = soup

            skip_domains = self._get_skip_domains()

            def _add_sponsor(sponsor: SponsorInfo):
                """Deduplicate and add a sponsor, filtering self-domain."""
                # Filter out the newsletter's own domain
                if sponsor.advertiser_domain:
                    d = sponsor.advertiser_domain.lower()
                    if d in skip_domains or self.domain in d:
                        return

                key = sponsor.advertiser_domain or normalize_company_name(sponsor.advertiser_name)
                if key and key not in seen_keys and len(key) > 1:
                    seen_keys.add(key)
                    sponsor.issue_date = issue_date
                    sponsor.source_newsletter = self.domain
                    sponsors.append(sponsor)

            # === Strategy 1: Traditional text pattern matching ===
            auto_sponsors = self.auto_detect_sponsors(content_html, issue_url)
            for s in auto_sponsors:
                _add_sponsor(s)

            # === Strategy 2: Sponsor-labeled HTML sections ===
            section_sponsors = self._extract_from_sponsor_sections(content_soup, issue_url)
            for s in section_sponsors:
                _add_sponsor(s)

            # === Strategy 3: UTM-tagged external links ===
            utm_sponsors = self._extract_utm_tagged_links(content_soup, issue_url)
            for s in utm_sponsors:
                _add_sponsor(s)

            # === Strategy 4: Affiliate network links ===
            aff_sponsors = self._extract_affiliate_sponsors(content_html, issue_url, issue_date)
            for s in aff_sponsors:
                _add_sponsor(s)

            # === Strategy 5: Promotional external links ===
            promo_sponsors = self._extract_promotional_links(content_soup, issue_url)
            for s in promo_sponsors:
                _add_sponsor(s)

        except Exception as e:
            logger.error(f"Error scraping {issue_url}: {e}")

        finally:
            page.context.close()

        return sponsors

    def _extract_from_sponsor_sections(
        self,
        content_soup: BeautifulSoup,
        issue_url: str,
    ) -> list[SponsorInfo]:
        """
        Find sponsors from HTML elements with sponsor/ad/partner CSS classes.

        Many newsletters wrap sponsor content in divs with class names like
        "sponsor-section", "ad-slot", "partner-content", etc.
        """
        sponsors = []
        skip_domains = self._get_skip_domains()

        # Find elements with sponsor-related classes or IDs
        for pattern in self.SPONSOR_SECTION_PATTERNS:
            sections = content_soup.select(
                f'[class*="{pattern}"], [id*="{pattern}"]'
            )
            for section in sections:
                # Extract all links in this section
                links = section.find_all("a", href=True)
                section_text = clean_text(section.get_text())

                for link in links:
                    href = link.get("href", "")
                    link_text = clean_text(link.get_text())

                    if not href or href.startswith(("#", "javascript:", "mailto:")):
                        continue

                    domain = extract_domain(href, strip_marketing=True)
                    if not domain or domain in skip_domains:
                        continue

                    # Resolve tracking links
                    if is_tracking_domain(href):
                        resolved = resolve_redirect_url(href)
                        if resolved:
                            domain = extract_domain(resolved, strip_marketing=True)
                            if not domain or domain in skip_domains:
                                continue

                    name = link_text if link_text and len(link_text) > 2 else domain.split(".")[0].title()
                    # Clean name of CTA phrases
                    name = re.sub(
                        r'^(Learn More|Get Started|Sign Up|Try|Click|Visit|Read More|Shop|Buy|Discover)\b.*',
                        '', name, flags=re.IGNORECASE
                    ).strip()
                    if not name or len(name) < 2:
                        name = domain.split(".")[0].title()

                    sponsors.append(SponsorInfo(
                        advertiser_name=name,
                        advertiser_domain=domain,
                        placement_type="sponsor_section",
                        ad_copy_snippet=truncate_text(section_text, 200),
                        issue_url=issue_url,
                        issue_date=None,
                        source_newsletter=self.domain,
                        confidence="high",
                        sponsor_url=href,
                        landing_page_url=href,
                        full_ad_copy=section_text[:500],
                    ))
                    break  # One sponsor per section

        return sponsors

    def _extract_utm_tagged_links(
        self,
        content_soup: BeautifulSoup,
        issue_url: str,
    ) -> list[SponsorInfo]:
        """
        Extract sponsors from links with UTM tracking parameters.

        Links with utm_source, utm_medium, or utm_campaign are almost
        always paid placements or tracked sponsor links. This is the #1
        signal for identifying advertising in newsletters.
        """
        sponsors = []
        skip_domains = self._get_skip_domains()
        seen_domains = set()

        for link in content_soup.find_all("a", href=True):
            href = link.get("href", "")
            href_lower = href.lower()

            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            # Check for UTM parameters
            has_utm = any(param in href_lower for param in [
                "utm_source=", "utm_medium=", "utm_campaign=",
                "utm_content=", "utm_term=",
            ])

            if not has_utm:
                continue

            # Get the domain
            domain = extract_domain(href, strip_marketing=True)
            if not domain or domain in skip_domains:
                continue

            # Resolve tracking links to actual domain
            if is_tracking_domain(href):
                resolved = resolve_redirect_url(href)
                if resolved:
                    domain = extract_domain(resolved, strip_marketing=True)
                    if not domain or domain in skip_domains:
                        continue

            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            # Extract company name and context
            link_text = clean_text(link.get_text())
            name = self._extract_company_name_from_link(link, domain)

            # Get surrounding context for ad copy
            ad_copy = self._get_link_context(link)

            # Determine what they sell from context
            product = self._extract_product_from_context(ad_copy, name)

            sponsors.append(SponsorInfo(
                advertiser_name=name,
                advertiser_domain=domain,
                placement_type="utm_tracked",
                ad_copy_snippet=truncate_text(ad_copy, 200),
                issue_url=issue_url,
                issue_date=None,
                source_newsletter=self.domain,
                confidence="high",
                sponsor_url=href,
                landing_page_url=href,
                full_ad_copy=ad_copy[:500],
                product_service=product,
            ))

        return sponsors

    def _extract_affiliate_sponsors(
        self,
        content_html: str,
        issue_url: str,
        issue_date: str | None,
    ) -> list[SponsorInfo]:
        """
        Extract sponsors from affiliate network links.

        Uses the base class extract_affiliate_links() but with
        the dynamic skip domain list for this newsletter.
        """
        sponsors = []
        skip_domains = self._get_skip_domains()

        soup = BeautifulSoup(content_html, "lxml")

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            link_text = clean_text(a.get_text())

            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            domain = extract_domain(href, strip_marketing=False)
            if not domain:
                continue

            # Skip newsletter's own links
            if any(skip in domain.lower() for skip in skip_domains):
                continue

            # Check known affiliate networks
            affiliate_network = None
            for network_domain, network_name in AFFILIATE_NETWORKS.items():
                if network_domain in domain.lower():
                    affiliate_network = network_name
                    break

            # Check URL params for affiliate indicators
            href_lower = href.lower()
            if not affiliate_network:
                if "tag=" in href_lower and "amazon" in href_lower:
                    affiliate_network = "Amazon"
                elif any(p in href_lower for p in ["?ref=", "&ref=", "affiliate", "partner_id", "aff_id"]):
                    affiliate_network = "Affiliate"

            if not affiliate_network:
                continue

            # Resolve to actual domain
            actual_domain = domain
            if affiliate_network != "Amazon":
                try:
                    resolved = resolve_redirect_url(href, timeout=2.0)
                    if resolved:
                        actual_domain = extract_domain(resolved, strip_marketing=True) or domain
                except Exception:
                    pass

            # Get context
            parent = a.parent
            context = clean_text(parent.get_text())[:200] if parent else link_text
            name = link_text if link_text and len(link_text) > 2 else actual_domain.split(".")[0].title()

            sponsors.append(SponsorInfo(
                advertiser_name=name,
                advertiser_domain=actual_domain,
                placement_type="affiliate_link",
                ad_copy_snippet=context,
                issue_url=issue_url,
                issue_date=issue_date,
                source_newsletter=self.domain,
                confidence="medium",
                sponsor_url=href,
                landing_page_url=href,
                full_ad_copy=context,
            ))

        return sponsors

    def _extract_promotional_links(
        self,
        content_soup: BeautifulSoup,
        issue_url: str,
    ) -> list[SponsorInfo]:
        """
        Extract sponsors from external links that look promotional.

        Finds external links to company websites (not news, academic, social)
        that appear in a promotional context (near discount codes, CTA
        buttons, product descriptions, or recommendation language).
        """
        sponsors = []
        skip_domains = self._get_skip_domains()
        seen_domains = set()

        # Promotional context indicators
        promo_indicators = [
            r'discount', r'coupon', r'promo\s*code', r'code[:\s]+\w+',
            r'use\s+code', r'save\s+\d+%', r'\d+%\s+off',
            r'exclusive\s+(?:offer|deal|discount)',
            r'special\s+(?:offer|deal|discount|pricing)',
            r'free\s+(?:trial|shipping|sample|gift)',
            r'limited\s+time', r'act\s+now', r'don\'?t\s+miss',
            r'check\s+(?:it\s+)?out', r'highly\s+recommend',
            r'i\s+(?:use|love|recommend|suggest|swear\s+by)',
            r'my\s+(?:favorite|go-to|preferred)',
            r'game[\s-]changer', r'must[\s-]have', r'worth\s+(?:trying|checking)',
        ]

        for link in content_soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            domain = extract_domain(href, strip_marketing=True)
            if not domain or domain in skip_domains or domain in seen_domains:
                continue

            # Skip links already captured by other strategies (UTM, affiliate)
            href_lower = href.lower()
            has_utm = any(p in href_lower for p in ["utm_source=", "utm_medium="])
            if has_utm:
                continue  # Already handled by UTM strategy
            is_affiliate = any(net in domain.lower() for net in AFFILIATE_NETWORKS)
            if is_affiliate:
                continue  # Already handled by affiliate strategy

            # Get context around this link
            context = self._get_link_context(link)
            context_lower = context.lower()

            # Check if context is promotional
            is_promo = any(re.search(pat, context_lower) for pat in promo_indicators)

            # Also check if the link is styled as a button (CTA)
            link_classes = " ".join(link.get("class", []))
            is_button = any(kw in link_classes.lower() for kw in [
                "btn", "button", "cta", "action",
            ])

            # Check if link has a promotional parent container
            parent = link.parent
            parent_classes = " ".join(parent.get("class", [])).lower() if parent and parent.get("class") else ""
            in_promo_section = any(
                kw in parent_classes
                for kw in ["recommend", "product", "offer", "feature", "pick", "tool"]
            )

            if not (is_promo or is_button or in_promo_section):
                continue

            seen_domains.add(domain)

            # Resolve tracking if needed
            if is_tracking_domain(href):
                resolved = resolve_redirect_url(href)
                if resolved:
                    domain = extract_domain(resolved, strip_marketing=True) or domain

            name = self._extract_company_name_from_link(link, domain)
            product = self._extract_product_from_context(context, name)

            sponsors.append(SponsorInfo(
                advertiser_name=name,
                advertiser_domain=domain,
                placement_type="promotional_link",
                ad_copy_snippet=truncate_text(context, 200),
                issue_url=issue_url,
                issue_date=None,
                source_newsletter=self.domain,
                confidence="medium",
                sponsor_url=href,
                landing_page_url=href,
                full_ad_copy=context[:500],
                product_service=product,
            ))

        return sponsors

    def _is_valid_company_name(self, name: str) -> bool:
        """Check if a string looks like a real company/brand name (not a CTA or headline)."""
        if not name or len(name) < 2:
            return False

        name_lower = name.lower().strip()

        # Reject names starting with punctuation/dashes (date fragments, list items)
        if name_lower[0] in "-–—•·|/\\#*>":
            return False

        # Reject date strings ("February 7-11:", "March 2024", "Jan 1-5")
        months = (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
        )
        if any(name_lower.startswith(m) for m in months):
            return False
        # Also reject "11-15 February" style
        if re.match(r'^\d{1,2}[\s-]', name_lower):
            return False

        # Reject URLs or domain-like strings
        if name_lower.startswith(("http://", "https://", "www.")):
            return False
        if re.match(r'^[\w.-]+\.(com|org|net|io|co|ai|app|dev|xyz|info)$', name_lower):
            return False

        # Reject discount/offer phrases ("20% off everything", "50% off", "Save $10")
        if re.match(r'^\d+%', name_lower):
            return False
        if re.match(r'^(save|free|\$)\s*\d', name_lower):
            return False

        # Exact CTA phrases that are never company names
        cta_phrases = {
            "learn more", "get started", "sign up", "try it", "click here",
            "read more", "shop now", "buy now", "visit", "check it out",
            "see more", "discover", "explore", "start", "join", "register",
            "download", "subscribe", "watch", "listen", "view", "here",
            "apply", "apply now", "apply here", "join me", "join us",
            "take the quiz", "try now", "try free", "try it free",
            "click here to register", "register here", "register now",
            "check out", "find out more", "get it", "get yours",
            "order now", "order here", "grab yours", "claim", "claim now",
            "enroll", "enroll now", "book now", "reserve", "reserve now",
            "go here", "go now", "see it", "see here", "see details",
            "more info", "more details", "full details", "details here",
            "do the race yourself", "do it yourself", "start here",
            "get it here", "available here", "find it here",
        }
        if name_lower in cta_phrases:
            return False

        # Names starting with action verbs are likely CTAs, not company names
        action_prefixes = [
            "get ", "click ", "apply ", "take ", "join ", "check ",
            "never ", "register ", "sign ", "try ", "buy ", "shop ",
            "order ", "grab ", "claim ", "enroll ", "book ", "reserve ",
            "download ", "watch ", "listen ", "start ", "discover ",
            "find ", "view ", "see ", "do ", "go ", "visit ",
            "available ", "subscribe ", "save ", "free ", "use ",
            "enter ", "redeem ", "unlock ",
        ]
        if any(name_lower.startswith(prefix) for prefix in action_prefixes):
            return False

        # Names containing these words are likely CTAs or descriptions, not company names
        cta_keywords = {"click", "register", " here"}
        if any(kw in name_lower for kw in cta_keywords):
            return False

        # Too many words = likely a headline or sentence, not a company name
        # Most company names are 1-4 words
        word_count = len(name.split())
        if word_count > 6:
            return False

        # Contains sentence-ending punctuation mid-string = likely a headline
        if any(c in name[:-1] for c in ['–', '—', '?', '!']):
            return False

        return True

    @staticmethod
    def _clean_company_name(name: str) -> str:
        """Strip promotional suffixes, discount codes, and noise from company names."""
        # Remove parenthetical discount codes: "(code: BEN)", "(use code BEN20)", "(20% off)"
        name = re.sub(r'\s*\((?:code|use code|promo|discount|coupon)[:\s]+\w+\)', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\s*\(\d+%\s*off\)', '', name, flags=re.IGNORECASE)
        # Remove trailing colons, commas, dashes
        name = name.rstrip(":,-–—").strip()
        # Remove leading dashes or bullet points
        name = name.lstrip("-–—•·").strip()
        return name

    def _extract_company_name_from_link(self, link, domain: str) -> str | None:
        """
        Extract a clean company name from a link element and its context.

        Returns None if no valid company name can be determined (the caller
        should skip this link).
        """
        link_text = clean_text(link.get_text())

        # Try link text first
        if link_text and len(link_text) > 1:
            name = self._clean_company_name(link_text.rstrip(".,!?:;").strip())
            if self._is_valid_company_name(name) and len(name) <= 50:
                return name

        # Check for a nearby heading or strong text
        parent = link.parent
        if parent:
            # Look for strong/bold text in the same parent
            strong = parent.find(["strong", "b", "h3", "h4"])
            if strong:
                strong_text = clean_text(strong.get_text())
                if strong_text and self._is_valid_company_name(strong_text) and len(strong_text) <= 40:
                    return strong_text

        # Fall back to domain name (always valid as a company name)
        return domain.split(".")[0].replace("-", " ").title()

    def _get_link_context(self, link) -> str:
        """Get the text context surrounding a link (for ad copy extraction)."""
        # Try to get the containing paragraph or div
        for parent in link.parents:
            if parent.name in ("p", "div", "td", "li", "section", "blockquote"):
                text = clean_text(parent.get_text())
                if text and len(text) > 20:
                    return text[:500]
            # Don't go too far up
            if parent.name in ("article", "main", "body"):
                break

        # Fall back to immediate parent
        parent = link.parent
        if parent:
            return clean_text(parent.get_text())[:500]

        return clean_text(link.get_text())

    def _extract_product_from_context(self, context: str, company_name: str) -> str | None:
        """Extract what a company sells/offers from surrounding text."""
        if not context:
            return None

        # Escape company name for regex, handle multi-word names
        esc_name = re.escape(company_name)

        # Patterns that describe products/services (ordered by specificity)
        product_patterns = [
            # "Company is the/a X" - direct product description
            rf'{esc_name}\s+is\s+(?:the|a|an)\s+([\w\s,\-]+?)(?:\.|!|\?|$)',
            # "Company, the X that..." - appositive description
            rf'{esc_name},?\s+the\s+([\w\s,\-]+?)(?:\bthat\b|\bwhich\b|\bfor\b|\.|!)',
            # "Company offers/provides/sells X"
            rf'{esc_name}\s+(?:offers?|provides?|sells?|makes?|delivers?|creates?)\s+([\w\s]+?)(?:\.|,|!|\?|$)',
            # "X from/by Company"
            rf'([\w\s]+?)\s+(?:by|from)\s+{esc_name}',
            # "Company for X" - what it's used for
            rf'{esc_name}\s+for\s+([\w\s]+?)(?:\.|,|!|\?|$)',
            # "their/the X platform/tool/service/product"
            r'(?:their|the|a|an)\s+([\w\s]+?(?:platform|tool|service|product|app|device|supplement|solution|software|system|program|kit|test|testing|course|monitor|monitoring|tracker|tracking|drink|wearable))',
            # "for continuous/comprehensive/advanced X"
            r'for\s+((?:continuous|comprehensive|advanced|real-time|daily|personalized)\s+[\w\s]+?)(?:\.|,|!|\?|$)',
            # "for your X" (what problem it solves)
            r'for\s+your\s+([\w\s]+?)(?:\.|,|!|\?|$)',
            # "game-changer for X" / "great for X"
            r'(?:game[\s-]changer|great|perfect|essential|must[\s-]have)\s+for\s+([\w\s]+?)(?:\.|,|!|\?|$)',
            # Discount/offer descriptions
            r'(?:get|save|enjoy)\s+(.+?)(?:\.|,|!|\?|$)',
        ]

        for pattern in product_patterns:
            match = re.search(pattern, context, re.IGNORECASE)
            if match:
                product = clean_text(match.group(1))
                if 5 < len(product) < 100:
                    return product

        return None

    def _extract_issue_date(
        self,
        soup: BeautifulSoup,
        issue_url: str,
    ) -> str | None:
        """
        Extract the publication date using multiple strategies.

        Args:
            soup: Parsed page
            issue_url: URL (may contain date info)

        Returns:
            Date string in YYYY-MM-DD format, or None
        """
        # Strategy 1: Meta tags
        date_selectors = [
            ('meta[property="article:published_time"]', "content"),
            ('meta[property="og:published_time"]', "content"),
            ('meta[name="date"]', "content"),
            ('meta[name="pubdate"]', "content"),
            ('meta[name="publish-date"]', "content"),
            ('meta[name="DC.date.issued"]', "content"),
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

        # Strategy 2: Date-related classes
        date_containers = soup.select('[class*="date"], [class*="time"], [class*="publish"], [class*="posted"]')
        for container in date_containers:
            text = container.get_text()
            date = self._parse_date(text)
            if date:
                return date

        # Strategy 3: URL patterns
        url_date_patterns = [
            r'/(\d{4})-(\d{2})-(\d{2})',
            r'/(\d{4})/(\d{2})/(\d{2})',
            r'-(\d{4})(\d{2})(\d{2})(?:[/-]|$)',
        ]

        for pattern in url_date_patterns:
            match = re.search(pattern, issue_url)
            if match:
                try:
                    groups = match.groups()
                    return f"{groups[0]}-{groups[1]}-{groups[2]}"
                except (ValueError, IndexError):
                    continue

        # Strategy 4: Month names in URL (e.g., email-roundup-december-2024)
        month_map = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12",
        }
        url_lower = issue_url.lower()
        for month_name, month_num in month_map.items():
            if month_name in url_lower:
                year_match = re.search(r'(\d{4})', url_lower)
                if year_match:
                    return f"{year_match.group(1)}-{month_num}-01"

        # Strategy 5: Date patterns in page text
        body_text = soup.get_text()[:2000]  # Only check beginning of page
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

        # ISO format with timezone
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

        # Common formats
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

    def _find_sponsor_domain(
        self,
        html: str,
        sponsor_name: str,
        match_position: int,
        search_window: int = 2000,
    ) -> str | None:
        """
        Find the sponsor's domain from links near their mention.

        Overrides base class to dynamically skip the source newsletter's
        own domain instead of hardcoding Morning Brew / Healthcare Brew.

        Args:
            html: Full HTML content
            sponsor_name: Name of the sponsor
            match_position: Position where sponsor was found
            search_window: Characters to search before/after

        Returns:
            Domain string or None
        """
        from ..utils.helpers import extract_links_from_html, is_tracking_link

        # Get HTML section around the match
        start = max(0, match_position - search_window // 2)
        end = min(len(html), match_position + search_window // 2)
        section = html[start:end]

        # Parse links in this section
        links = extract_links_from_html(section)

        # Dynamic skip domains - include the SOURCE newsletter's domain
        skip_domains = {
            self.domain,
            f"www.{self.domain}",
            # Common social/non-advertiser domains
            "twitter.com", "x.com", "facebook.com", "linkedin.com",
            "instagram.com", "youtube.com", "google.com",
            "bit.ly", "t.co", "apple.com", "spotify.com",
            "substack.com", "beehiiv.com", "convertkit.com",
            "mailchimp.com",
        }

        normalized_sponsor = normalize_company_name(sponsor_name)

        # First pass: links that relate to sponsor
        for link in links:
            href = link.get("href", "")
            domain = link.get("domain")
            link_text = normalize_company_name(link.get("text", ""))

            if not href:
                continue

            is_relevant = (
                normalized_sponsor in link_text or
                (domain and normalized_sponsor in domain.lower())
            )

            is_tracking = is_tracking_link(href) or "utm_" in href.lower()

            if not is_relevant and not is_tracking:
                continue

            if is_tracking_domain(href):
                resolved_url = resolve_redirect_url(href)
                if resolved_url:
                    resolved_domain = extract_domain(resolved_url)
                    if resolved_domain and resolved_domain.lower() not in skip_domains:
                        return resolved_domain

            if domain and domain.lower() not in skip_domains:
                return domain

        # Second pass: any external link
        for link in links:
            href = link.get("href", "")
            domain = link.get("domain")

            if not href:
                continue

            if is_tracking_domain(href):
                resolved_url = resolve_redirect_url(href)
                if resolved_url:
                    resolved_domain = extract_domain(resolved_url)
                    if resolved_domain and resolved_domain.lower() not in skip_domains:
                        return resolved_domain

            if domain and domain.lower() not in skip_domains:
                return domain

        # Last resort: guess from name
        guessed = guess_domain_from_name(sponsor_name)
        if guessed:
            return guessed

        return None
