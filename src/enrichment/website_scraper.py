"""Website contact scraper - extracts contact info directly from company websites.

Uses a hybrid approach:
1. Try fast httpx requests first
2. Fall back to Playwright (headless browser) for JavaScript-rendered sites
"""

import re
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from contextlib import contextmanager

import httpx
from bs4 import BeautifulSoup

# Playwright is optional - import with fallback
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeout = Exception  # Fallback

from ..utils.helpers import strip_marketing_subdomain, TRACKING_DOMAINS

logger = logging.getLogger(__name__)


# Link services that should not be scraped (not actual company websites)
LINK_SERVICE_DOMAINS = {
    "linkby.com", "go.linkby.com",
    "linktr.ee", "linktree.com",
    "taplink.cc",
    "stan.store",
    "beacons.ai",
    "hoo.be",
    "snipfeed.co",
    "plink.com",
    "bit.ly",
    "tinyurl.com",
}


@dataclass
class WebsiteContact:
    """Contact information found on a website."""
    email: str
    source_page: str
    email_type: str = "unknown"  # generic, personal, support
    name: str | None = None
    title: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None


@dataclass
class WebsiteScrapeResult:
    """Results from scraping a website for contacts."""
    domain: str
    contacts: list[WebsiteContact] = field(default_factory=list)
    pages_scraped: int = 0
    errors: list[str] = field(default_factory=list)


# Email patterns that are likely useful for ad sales outreach
PRIORITY_EMAIL_PREFIXES = [
    # Highest priority - ad/partnership specific
    "advertising", "ads", "ad", "partnerships", "partner", "sponsors", "sponsorship",
    "media", "mediasales", "adsales", "adops",
    # High priority - business/sales
    "marketing", "sales", "business", "bd", "biz", "commercial",
    # Medium priority - general contact
    "hello", "contact", "info", "inquiries", "enquiries",
    # Lower priority - PR/comms (but still useful)
    "press", "pr", "communications", "comms", "news",
]

# Pages likely to contain contact information - OPTIMIZED for speed
# Focus on the 3 most important: advertising, contact, about (95% of contacts are here)
CONTACT_PAGE_PATTERNS = [
    # Advertising/partnership pages - HIGHEST PRIORITY (most relevant for ad sales)
    "/advertise", "/advertising", "/partnerships", "/partners",
    "/media-kit", "/mediakit", "/sponsorship",
    # Contact pages - HIGH PRIORITY
    "/contact", "/contact-us",
    # About pages - has footer with contact info
    "/about", "/about-us",
]

# Email obfuscation patterns to decode
EMAIL_OBFUSCATION_PATTERNS = [
    # Pattern: "email [at] domain [dot] com" or "email(at)domain(dot)com"
    (r'([a-zA-Z0-9._%+-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*([a-zA-Z0-9.-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})', r'\1@\2.\3'),
    # Pattern: "email @ domain . com" (with spaces)
    (r'([a-zA-Z0-9._%+-]+)\s+@\s+([a-zA-Z0-9.-]+)\s+\.\s+([a-zA-Z]{2,})', r'\1@\2.\3'),
    # Pattern: HTML entity encoding
    (r'([a-zA-Z0-9._%+-]+)&#64;([a-zA-Z0-9.-]+)\.([a-zA-Z]{2,})', r'\1@\2.\3'),
    (r'([a-zA-Z0-9._%+-]+)&#x40;([a-zA-Z0-9.-]+)\.([a-zA-Z]{2,})', r'\1@\2.\3'),
]

# Email regex pattern
EMAIL_PATTERN = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
)

# Phone pattern (US-focused but flexible)
PHONE_PATTERN = re.compile(
    r'(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
)

# LinkedIn profile pattern
LINKEDIN_PATTERN = re.compile(
    r'https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?'
)


class WebsiteScraper:
    """Scrapes company websites to find contact information.

    Uses a multi-phase approach:
    1. Analyze homepage with Claude to identify best pages (if enabled)
    2. Fast HTTP requests with httpx
    3. Use Claude for intelligent contact extraction on key pages
    4. Fallback to Playwright for JavaScript-rendered sites
    """

    def __init__(
        self,
        timeout: float = 8.0,  # Increased for reliability
        max_pages: int = 10,   # Increased for more thorough scraping
        max_errors: int = 3,   # More tolerant of errors
        use_browser: bool = True,  # Use Playwright as fallback for JS sites
        use_claude: bool = True,  # Use Claude for intelligent navigation/extraction
        log_callback: callable = None,
    ):
        """
        Initialize the website scraper.

        Args:
            timeout: Request timeout in seconds
            max_pages: Maximum pages to scrape per domain
            max_errors: Stop scraping after this many consecutive errors
            use_browser: Use Playwright for JS-rendered sites (default True)
            use_claude: Use Claude for intelligent page discovery and extraction
            log_callback: Optional callback for live logging
        """
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_errors = max_errors
        self.use_browser = use_browser and PLAYWRIGHT_AVAILABLE
        self.use_claude = use_claude
        self._log_callback = log_callback
        self._claude_agent = None

        # Common headers to avoid being blocked
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        }

    def _get_claude_agent(self):
        """Get or create Claude agent (lazy initialization)."""
        if self._claude_agent is None and self.use_claude:
            try:
                from .claude_agent import ClaudeAgent
                self._claude_agent = ClaudeAgent(log_callback=self._log_callback)
                if not self._claude_agent.is_configured:
                    self._log("Claude API not configured, falling back to regex extraction", "warning")
                    self._claude_agent = None
            except ImportError:
                self._log("Claude agent not available", "warning")
                self._claude_agent = None
        return self._claude_agent

    def _log(self, message: str, level: str = "info"):
        """Log a message, optionally to callback."""
        if self._log_callback:
            self._log_callback(message, level)
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    @contextmanager
    def _browser_context(self):
        """Context manager for Playwright browser."""
        if not PLAYWRIGHT_AVAILABLE:
            yield None
            return

        playwright = None
        browser = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ],
            )
            yield browser
        except Exception as e:
            self._log(f"Failed to start browser: {e}", "error")
            yield None
        finally:
            if browser:
                browser.close()
            if playwright:
                playwright.stop()

    def _scrape_with_browser(self, domain: str, urls: list[str]) -> list[WebsiteContact]:
        """
        Scrape URLs using Playwright for JavaScript-rendered content.

        Args:
            domain: The domain being scraped
            urls: List of URLs to try

        Returns:
            List of contacts found
        """
        if not PLAYWRIGHT_AVAILABLE:
            return []

        all_emails: dict[str, WebsiteContact] = {}

        with self._browser_context() as browser:
            if not browser:
                return []

            context = browser.new_context(
                user_agent=self.headers["User-Agent"],
                viewport={"width": 1280, "height": 720},
            )

            try:
                page = context.new_page()

                for url in urls[:self.max_pages]:
                    try:
                        self._log(f"Browser loading: {url}")
                        page.goto(url, wait_until="networkidle", timeout=12000)

                        # Wait a bit for any dynamic content
                        page.wait_for_timeout(1000)

                        # Get rendered HTML
                        html = page.content()

                        # Extract contacts
                        contacts = self._extract_contacts_from_html(html, url, domain)
                        for contact in contacts:
                            if contact.email not in all_emails:
                                all_emails[contact.email] = contact

                        # Also look for mailto: links in the DOM
                        mailto_links = page.query_selector_all('a[href^="mailto:"]')
                        for link in mailto_links:
                            href = link.get_attribute("href")
                            if href:
                                email = href.replace("mailto:", "").split("?")[0].strip()
                                if EMAIL_PATTERN.match(email) and email not in all_emails:
                                    all_emails[email] = WebsiteContact(
                                        email=email,
                                        source_page=url,
                                        email_type="generic",
                                    )

                        # Stop if we found good contacts
                        if len(all_emails) >= 3:
                            break

                    except PlaywrightTimeout:
                        self._log(f"Browser timeout: {url}", "warning")
                    except Exception as e:
                        self._log(f"Browser error on {url}: {e}", "warning")

            finally:
                context.close()

        return list(all_emails.values())

    def scrape_domain(self, domain: str, company_name: str | None = None) -> WebsiteScrapeResult:
        """
        Scrape a domain for contact information.

        Uses Claude (if available) for intelligent navigation:
        1. Analyze homepage to identify best pages to visit
        2. Use Claude to extract contacts from complex pages
        3. Identify additional relevant links

        Args:
            domain: Domain to scrape (e.g., "healthedge.com")
            company_name: Optional company name for better Claude extraction

        Returns:
            WebsiteScrapeResult with found contacts
        """
        # Clean up domain - strip protocol, www, marketing subdomains
        if domain.startswith(("http://", "https://")):
            domain = urlparse(domain).netloc
        if domain.startswith("www."):
            domain = domain[4:]

        # Strip marketing subdomains (get.expertvoice.com -> expertvoice.com)
        original_domain = domain
        domain = strip_marketing_subdomain(domain)

        if domain != original_domain:
            self._log(f"Stripped subdomain: {original_domain} → {domain}")

        result = WebsiteScrapeResult(domain=domain)
        company_name = company_name or domain.split('.')[0].title()  # Fallback to domain name

        # Skip link service domains (they're not actual company sites)
        if domain.lower() in LINK_SERVICE_DOMAINS or any(domain.lower().endswith(f".{d}") for d in LINK_SERVICE_DOMAINS):
            self._log(f"Skipping link service domain: {domain}", "warning")
            result.errors.append(f"Link service domain, not company website: {domain}")
            return result

        # Build base URL
        base_url = f"https://{domain}"

        self._log(f"Scraping {domain} for contacts...")

        # Track visited URLs to avoid duplicates
        visited_urls: set[str] = set()
        urls_to_visit: list[str] = [base_url]

        # Add key contact page URLs (optimized short list)
        for pattern in CONTACT_PAGE_PATTERNS:
            urls_to_visit.append(urljoin(base_url, pattern))

        # Deduplicate initial URLs
        urls_to_visit = list(dict.fromkeys(urls_to_visit))

        pages_scraped = 0
        consecutive_errors = 0
        all_emails: dict[str, WebsiteContact] = {}  # email -> contact

        def has_good_contacts() -> bool:
            """Check if we have high-quality contacts worth stopping for."""
            if len(all_emails) < 2:
                return False
            # Only stop early if we have advertising/sales emails
            ad_emails = [c for c in all_emails.values() if c.email_type == "advertising"]
            return len(ad_emails) >= 1 and len(all_emails) >= 3

        with httpx.Client(
            timeout=self.timeout,
            headers=self.headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        ) as client:
            # Optimized: Check homepage + key pages directly (no Claude analysis needed)
            # 95% of contact info is in homepage footer, /contact, or /advertise

            for url in urls_to_visit:
                # Stop conditions
                if pages_scraped >= self.max_pages:
                    break
                if consecutive_errors >= self.max_errors:
                    self._log(f"Stopping after {consecutive_errors} errors", "warning")
                    break
                # Early exit only if we have high-quality contacts
                if has_good_contacts():
                    break

                if url in visited_urls:
                    continue

                visited_urls.add(url)

                try:
                    response = client.get(url)

                    # Handle different status codes
                    if response.status_code == 404:
                        continue  # Page not found, try next
                    elif response.status_code == 403:
                        consecutive_errors += 1
                        continue  # Forbidden, site may be blocking
                    elif response.status_code >= 500:
                        consecutive_errors += 1
                        result.errors.append(f"Server error {response.status_code}: {url}")
                        continue
                    elif response.status_code != 200:
                        continue

                    # Success - reset error counter
                    consecutive_errors = 0
                    pages_scraped += 1
                    html = response.text

                    # Extract contacts from this page using regex
                    page_contacts = self._extract_contacts_from_html(html, url, domain)

                    for contact in page_contacts:
                        # Deduplicate by email, keeping the best version
                        if contact.email not in all_emails:
                            all_emails[contact.email] = contact
                        else:
                            # Update if this one has more info
                            existing = all_emails[contact.email]
                            if contact.name and not existing.name:
                                existing.name = contact.name
                            if contact.title and not existing.title:
                                existing.title = contact.title
                            if contact.phone and not existing.phone:
                                existing.phone = contact.phone

                    # Skip Claude per-page extraction for speed - regex is sufficient
                    # Claude is used for homepage navigation analysis only

                    # Look for additional contact page links on EVERY page (not just homepage)
                    # This helps find contact links in navigation, footer, etc.
                    new_urls = self._find_contact_links(html, base_url)
                    for new_url in new_urls:
                        if new_url not in visited_urls and new_url not in urls_to_visit:
                            urls_to_visit.append(new_url)

                except httpx.TimeoutException:
                    consecutive_errors += 1
                    self._log(f"Timeout fetching {url}", "warning")
                except httpx.ConnectError:
                    consecutive_errors += 1
                    self._log(f"Connection failed: {url}", "warning")
                except Exception as e:
                    consecutive_errors += 1
                    result.errors.append(f"Error fetching {url}: {str(e)}")

        result.pages_scraped = pages_scraped

        # Phase 2: If httpx found nothing or few results, try Playwright for JS-rendered sites
        if len(all_emails) < 2 and self.use_browser:
            self._log(f"Few emails via HTTP, trying browser for JS-rendered content...")
            # Prioritize advertising/contact pages for browser scraping
            priority_urls = [base_url]
            for pattern in ["/advertise", "/contact", "/contact-us", "/about", "/team"]:
                priority_urls.append(urljoin(base_url, pattern))

            browser_contacts = self._scrape_with_browser(domain, priority_urls)
            for contact in browser_contacts:
                if contact.email not in all_emails:
                    all_emails[contact.email] = contact

        # Phase 3 removed for speed - Claude extraction is too slow
        # Email patterns will be generated and verified via SMTP in apollo.py instead

        # Clean up Claude agent
        if self._claude_agent:
            try:
                self._claude_agent.close()
            except Exception:
                pass
            self._claude_agent = None

        result.contacts = self._prioritize_contacts(list(all_emails.values()))

        if result.contacts:
            self._log(f"Found {len(result.contacts)} contacts on {domain}")
        else:
            self._log(f"No contacts found on {domain}", "warning")

        return result

    def _extract_contacts_from_html(
        self,
        html: str,
        source_url: str,
        domain: str,
    ) -> list[WebsiteContact]:
        """Extract contact information from HTML content."""
        contacts = []
        soup = BeautifulSoup(html, "lxml")

        # Remove script and style elements
        for element in soup(["script", "style", "noscript"]):
            element.decompose()

        text = soup.get_text(separator=" ")

        # Find all email addresses
        emails = set(EMAIL_PATTERN.findall(text))

        # Also decode obfuscated emails
        for pattern, replacement in EMAIL_OBFUSCATION_PATTERNS:
            obfuscated = re.findall(pattern, text, re.IGNORECASE)
            for match in obfuscated:
                if isinstance(match, tuple):
                    # Reconstruct email from groups
                    decoded = f"{match[0]}@{match[1]}.{match[2]}"
                    if EMAIL_PATTERN.match(decoded):
                        emails.add(decoded)

        # Also check mailto: links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:"):
                email = href[7:].split("?")[0].strip()
                if EMAIL_PATTERN.match(email):
                    emails.add(email)

        # Check onclick handlers for emails (some sites use JS to build email)
        for element in soup.find_all(onclick=True):
            onclick = element.get("onclick", "")
            email_matches = EMAIL_PATTERN.findall(onclick)
            emails.update(email_matches)

        # Check data attributes that might contain emails
        for element in soup.find_all(attrs={"data-email": True}):
            email = element.get("data-email", "")
            if EMAIL_PATTERN.match(email):
                emails.add(email)
        for element in soup.find_all(attrs={"data-contact": True}):
            email = element.get("data-contact", "")
            if EMAIL_PATTERN.match(email):
                emails.add(email)

        # Filter out emails from the same domain (internal emails only)
        # and skip obvious non-contact emails
        skip_patterns = ["noreply", "no-reply", "donotreply", "unsubscribe", "example.com", "test@", "demo@", "wixpress.com"]

        # Skip file extensions that look like emails (image@2x.png, etc.)
        skip_extensions = [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js"]

        for email in emails:
            email_lower = email.lower()

            # Skip emails that match skip patterns
            if any(skip in email_lower for skip in skip_patterns):
                continue

            # Skip file-like patterns (AboutUs@2x.png)
            if any(email_lower.endswith(ext) for ext in skip_extensions):
                continue

            # Skip if it looks like a retina image indicator (@2x, @3x)
            if re.search(r'@\d+x', email_lower):
                continue

            # Skip if domain part has no dots or is too short (likely not real)
            domain_part = email_lower.split("@")[-1]
            if "." not in domain_part or len(domain_part) < 4:
                continue

            # Determine email type
            email_prefix = email_lower.split("@")[0]
            email_type = "unknown"

            if any(prefix in email_prefix for prefix in PRIORITY_EMAIL_PREFIXES[:6]):
                email_type = "advertising"
            elif any(prefix in email_prefix for prefix in PRIORITY_EMAIL_PREFIXES[6:]):
                email_type = "generic"
            elif "." in email_prefix or any(c.isdigit() for c in email_prefix):
                # Likely a personal email (firstname.lastname or with numbers)
                email_type = "personal"

            contact = WebsiteContact(
                email=email,
                source_page=source_url,
                email_type=email_type,
            )

            # Try to find associated name/title near the email
            name, title = self._find_name_near_email(soup, email)
            if name:
                contact.name = name
            if title:
                contact.title = title

            contacts.append(contact)

        # Find phone numbers
        phones = PHONE_PATTERN.findall(text)
        if phones and contacts:
            # Associate first phone with first contact if no phone yet
            for contact in contacts:
                if not contact.phone:
                    contact.phone = phones[0]
                    break

        # Find LinkedIn URLs
        linkedin_urls = LINKEDIN_PATTERN.findall(text)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "linkedin.com/in/" in href:
                linkedin_urls.append(href)

        if linkedin_urls and contacts:
            # Associate LinkedIn URLs with personal contacts
            for contact in contacts:
                if contact.email_type == "personal" and not contact.linkedin_url:
                    contact.linkedin_url = linkedin_urls[0]
                    linkedin_urls = linkedin_urls[1:]
                    if not linkedin_urls:
                        break

        return contacts

    def _find_name_near_email(
        self,
        soup: BeautifulSoup,
        email: str,
    ) -> tuple[str | None, str | None]:
        """Try to find a person's name and title near their email address."""
        name = None
        title = None

        # Look for the email in the HTML and find nearby text
        email_lower = email.lower()

        for element in soup.find_all(string=re.compile(re.escape(email), re.IGNORECASE)):
            parent = element.parent
            if not parent:
                continue

            # Look at siblings and parent for name/title info
            container = parent.parent if parent.parent else parent
            container_text = container.get_text(separator=" ", strip=True)

            # Common title keywords
            title_keywords = [
                "CEO", "CTO", "CFO", "COO", "CMO", "VP", "Director", "Manager",
                "President", "Founder", "Partner", "Head of", "Chief",
                "Marketing", "Sales", "Business Development", "Partnerships",
            ]

            for keyword in title_keywords:
                if keyword.lower() in container_text.lower():
                    # Extract the title portion
                    title_match = re.search(
                        rf'({keyword}[^,\n]*)',
                        container_text,
                        re.IGNORECASE
                    )
                    if title_match:
                        title = title_match.group(1).strip()
                        break

            # Look for a name (capitalized words before the email or title)
            # Simple heuristic: 2-3 capitalized words in a row
            name_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b')
            name_matches = name_pattern.findall(container_text)
            if name_matches:
                # Filter out common non-name phrases
                skip_names = ["Contact Us", "Get In Touch", "Learn More", "Read More"]
                for match in name_matches:
                    if match not in skip_names and len(match.split()) <= 3:
                        name = match
                        break

            if name or title:
                break

        return name, title

    def _find_contact_links(self, html: str, base_url: str) -> list[str]:
        """Find links to contact-related pages."""
        soup = BeautifulSoup(html, "lxml")
        contact_urls = []

        # Expanded keywords - prioritize advertising/sales related
        contact_keywords = [
            # Advertising/sales (highest priority for our use case)
            "advertise", "advertising", "sponsors", "sponsorship", "media-kit",
            "mediakit", "ad-sales", "partnerships", "partner",
            # Contact pages
            "contact", "get-in-touch", "reach-us", "inquiry", "enquiry",
            # Team/about pages
            "about", "team", "leadership", "people", "management", "company",
            # Press/media (often has contacts)
            "press", "newsroom", "media", "pr",
        ]

        base_domain = urlparse(base_url).netloc

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text().lower().strip()

            # Skip empty or javascript links
            if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
                continue

            # Check if link text or href contains contact keywords
            href_lower = href.lower()
            if any(kw in text or kw in href_lower for kw in contact_keywords):
                full_url = urljoin(base_url, href)
                # Only include same-domain links
                try:
                    if urlparse(full_url).netloc == base_domain:
                        contact_urls.append(full_url)
                except Exception:
                    continue

        # Also look specifically in footer and nav sections
        for section in soup.find_all(["footer", "nav"]):
            for a in section.find_all("a", href=True):
                href = a["href"]
                if not href or href.startswith(("javascript:", "#")):
                    continue
                full_url = urljoin(base_url, href)
                try:
                    if urlparse(full_url).netloc == base_domain:
                        # Add footer/nav links that might be contact pages
                        href_lower = href.lower()
                        if any(kw in href_lower for kw in ["contact", "about", "team", "advertise"]):
                            contact_urls.append(full_url)
                except Exception:
                    continue

        return list(dict.fromkeys(contact_urls))  # Deduplicate

    def _prioritize_contacts(
        self,
        contacts: list[WebsiteContact],
    ) -> list[WebsiteContact]:
        """Sort contacts by priority for ad sales outreach."""

        def priority_score(contact: WebsiteContact) -> int:
            score = 0
            email_prefix = contact.email.lower().split("@")[0]

            # Highest priority: advertising/partnership emails
            if contact.email_type == "advertising":
                score += 100
            elif contact.email_type == "personal":
                score += 50
            elif contact.email_type == "generic":
                score += 25

            # Bonus for having additional info
            if contact.name:
                score += 20
            if contact.title:
                score += 15
                # Extra bonus for relevant titles
                title_lower = contact.title.lower()
                if any(kw in title_lower for kw in ["marketing", "sales", "partner", "business"]):
                    score += 30
            if contact.phone:
                score += 10
            if contact.linkedin_url:
                score += 10

            # Specific prefix bonuses
            priority_prefixes = ["advertising", "ads", "partnerships", "marketing", "sales"]
            if any(prefix in email_prefix for prefix in priority_prefixes):
                score += 40

            return score

        return sorted(contacts, key=priority_score, reverse=True)


def scrape_website_for_contacts(
    domain: str,
    company_name: str | None = None,
    max_pages: int = 10,
    use_claude: bool = True,
    log_callback: callable = None,
) -> list[WebsiteContact]:
    """
    Convenience function to scrape a domain for contacts.

    Uses Claude (if available) for intelligent navigation and extraction.

    Args:
        domain: Domain to scrape
        company_name: Optional company name for better Claude extraction
        max_pages: Maximum pages to check (default 10 for thoroughness)
        use_claude: Whether to use Claude for intelligent scraping
        log_callback: Optional logging callback

    Returns:
        List of WebsiteContact objects, prioritized for outreach
    """
    scraper = WebsiteScraper(
        max_pages=max_pages,
        use_claude=use_claude,
        log_callback=log_callback,
    )
    result = scraper.scrape_domain(domain, company_name=company_name)
    return result.contacts
