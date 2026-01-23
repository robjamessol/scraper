"""Website contact scraper - extracts contact info directly from company websites.

Uses a hybrid approach:
1. Try fast httpx requests first
2. Fall back to Playwright (headless browser) for JavaScript-rendered sites
"""

import re
import logging
import base64
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

# Optional: trafilatura for HTML-to-Markdown conversion (much better for LLM input)
try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

# Optional: PDF parsing for Media Kits (often contain valuable contacts)
try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

from ..utils.helpers import strip_marketing_subdomain, TRACKING_DOMAINS, resolve_domain_redirect, guess_alternative_domains

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

# Job titles that are relevant for advertising/partnership outreach
RELEVANT_JOB_TITLES = [
    # Advertising/Media
    "advertising", "media", "ad sales", "ad ops", "media sales",
    # Marketing
    "marketing", "growth", "brand", "content", "digital marketing",
    # Partnerships/Business Development
    "partnership", "affiliate", "business development", "bd", "strategic",
    # Communications/PR
    "communications", "pr", "public relations", "press", "corporate communications",
    # Sales/Commercial
    "sales", "commercial", "revenue", "account",
    # Leadership with relevant focus
    "cmo", "chief marketing", "vp marketing", "vp media", "head of marketing",
    "director of marketing", "director of media", "director of partnerships",
]

# Job titles to EXCLUDE (not relevant for advertising outreach)
EXCLUDED_JOB_TITLES = [
    "engineer", "developer", "software", "technical", "tech",
    "legal", "counsel", "attorney", "lawyer",
    "hr", "human resources", "recruiting", "talent",
    "finance", "accounting", "cfo", "controller",
    "it ", "information technology", "security", "devops",
    "product manager", "product owner", "ux", "design",
    "customer support", "customer service", "support",
    "operations", "logistics", "supply chain",
]

# Default pages to check for contact info (balanced for coverage AND speed)
CONTACT_PAGE_PATTERNS = [
    # Advertising/sales - highest priority
    "/advertise", "/advertising", "/partnerships", "/media-kit", "/mediakit",
    # Contact pages - including language-prefixed versions
    "/contact", "/contact-us", "/connect", "/connect-with-us",
    "/en/contact", "/en/contact-us",  # Language-prefixed (wolterskluwer, etc.)
    # Press/Media - CRITICAL: often has PR/media contact emails
    "/press", "/media", "/press-media", "/newsroom", "/news",
    "/en/press", "/en/news", "/en/newsroom",  # Language-prefixed press
    # About pages - often link to press/media subpages
    "/about", "/about-us", "/team", "/about/press", "/about/press-media",
    "/about/contact", "/about/media",
    # Business pages
    "/sponsors", "/sponsorship", "/partners", "/for-business",
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

# URL patterns to SKIP (irrelevant for contact discovery)
SKIP_URL_PATTERNS = [
    # Job/career pages (not useful for advertising contacts)
    "/jobs", "/careers", "/career", "/hiring", "/job-", "-jobs",
    "/q-", "/l-",  # Indeed job search URLs
    # Blog/news content (rarely has contacts) - but NOT /news or /newsroom itself
    "/blog/", "/article/", "/post/", "/news/20", "/insights/",
    "/expert-insights/", "/resources/",
    # Product/feature pages
    "/products/", "/product/", "/solutions/", "/features/", "/pricing",
    "/demo", "/trial", "/signup", "/sign-up", "/register",
    # Legal/compliance
    "/privacy", "/terms", "/legal", "/cookie", "/gdpr", "/compliance/",
    # Support/help
    "/support", "/help", "/faq", "/knowledge", "/docs/",
    # Auth pages
    "/login", "/signin", "/auth", "/account",
    # E-commerce
    "/cart", "/checkout", "/shop/", "/store/",
    # NOTE: We intentionally do NOT skip /en/, /en-us/, etc. for contact/press pages
    # as many international sites have contacts only at /en/contact-us
    # Only skip deep localized content pages (not top-level contact/press)
    "/de/blog/", "/fr/blog/", "/es/blog/",  # Localized blog content only
]


def is_url_worth_visiting(url: str) -> bool:
    """Check if a URL is likely to have contact information."""
    url_lower = url.lower()

    # Skip URLs matching skip patterns
    for pattern in SKIP_URL_PATTERNS:
        if pattern in url_lower:
            return False

    # Skip very long URLs (usually dynamic/generated content)
    if len(url) > 150:
        return False

    return True


# Block page indicators (soft 403s that return 200 status)
# These sites appear to load but are actually bot challenges
BLOCK_PAGE_INDICATORS = [
    "cloudflare", "captcha", "recaptcha", "hcaptcha",
    "pardon our interruption", "please verify you are human",
    "access denied", "please complete the security check",
    "enable javascript", "browser check", "ddos protection",
    "checking your browser", "ray id", "attention required",
    "just a moment", "please wait while we verify",
    "bot detection", "security challenge",
]


def is_block_page(html: str) -> bool:
    """
    Detect if a page is actually a block/challenge page (soft 403).

    Many anti-bot systems return 200 OK but serve a challenge page.
    This wastes scraping effort and confuses contact extraction.
    """
    if not html or len(html) < 100:
        return True  # Suspiciously short content

    html_lower = html.lower()

    # Check for block page indicators
    indicator_count = sum(1 for ind in BLOCK_PAGE_INDICATORS if ind in html_lower)

    # If multiple indicators or page is very short with one indicator
    if indicator_count >= 2:
        return True
    if indicator_count >= 1 and len(html) < 5000:
        return True

    # Check for Cloudflare-specific patterns
    if "cf-browser-verification" in html_lower or "cf_chl_opt" in html_lower:
        return True

    return False


def html_to_clean_text(html: str, preserve_links: bool = True) -> str:
    """
    Convert HTML to clean text optimized for LLM processing.

    This dramatically reduces token usage while preserving semantic meaning.
    Following best practices from research on LLM-augmented scraping.

    Args:
        html: Raw HTML content
        preserve_links: Whether to preserve href values inline

    Returns:
        Clean text suitable for LLM input
    """
    # Try trafilatura first (best quality, extracts main content only)
    if TRAFILATURA_AVAILABLE:
        try:
            extracted = trafilatura.extract(
                html,
                include_links=preserve_links,
                include_tables=True,
                no_fallback=False,
                favor_precision=False,  # Recall is more important for contacts
            )
            if extracted and len(extracted) > 100:
                return extracted
        except Exception:
            pass  # Fall back to BeautifulSoup

    # Fallback: manual cleaning with BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    # Remove elements that never contain contact info
    for tag in soup(["script", "style", "noscript", "svg", "path", "meta", "link",
                     "head", "iframe", "canvas", "video", "audio", "source"]):
        tag.decompose()

    # Remove common boilerplate sections (nav, footer often have generic links)
    # But be careful - footer sometimes has contact info!
    for tag in soup.find_all(["nav"]):
        tag.decompose()

    # Get text with link preservation
    if preserve_links:
        # Replace links with text + URL in brackets
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if href and text and "mailto:" in href:
                # Preserve mailto links prominently
                a.replace_with(f"{text} [{href}]")
            elif href and text and len(text) > 2:
                # Keep link text with abbreviated URL for context
                a.replace_with(f"{text}")

    # Get clean text
    text = soup.get_text(separator=" ", strip=True)

    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)

    # Remove CSS artifacts that sometimes leak through
    text = re.sub(r'[a-z-]+\s*:\s*[^;]+;', '', text)  # CSS properties
    text = re.sub(r'\{[^}]+\}', '', text)  # CSS blocks

    return text.strip()


def decode_base64_emails(text: str, domain: str) -> list[str]:
    """
    Find and decode Base64-encoded emails in text/HTML.

    Many sites encode emails in data attributes as Base64 to prevent harvesting.
    E.g., data-email="am9obkBleGFtcGxlLmNvbQ==" -> john@example.com

    Args:
        text: Text/HTML content to search
        domain: Target domain to validate emails against

    Returns:
        List of decoded email addresses
    """
    decoded_emails = []

    # Find potential Base64 strings (data attributes, href values, etc.)
    # Base64 email pattern: typically 20-60 chars, ends with = padding
    base64_pattern = re.compile(r'[A-Za-z0-9+/]{20,80}={0,2}')

    for match in base64_pattern.findall(text):
        try:
            # Attempt to decode
            decoded = base64.b64decode(match).decode('utf-8', errors='ignore')

            # Check if it looks like an email
            email_match = EMAIL_PATTERN.search(decoded)
            if email_match:
                email = email_match.group()
                # Validate it's for the target domain
                email_domain = email.split('@')[-1].lower()
                if domain.lower() in email_domain or email_domain in domain.lower():
                    decoded_emails.append(email)

            # Also check for mailto: prefix
            if decoded.startswith('mailto:'):
                email = decoded.replace('mailto:', '').split('?')[0].strip()
                if EMAIL_PATTERN.match(email):
                    email_domain = email.split('@')[-1].lower()
                    if domain.lower() in email_domain or email_domain in domain.lower():
                        decoded_emails.append(email)

        except Exception:
            continue  # Not valid Base64 or not decodable

    return list(set(decoded_emails))


def extract_contacts_from_pdf(pdf_content: bytes, domain: str) -> list[dict]:
    """
    Extract contact information from a PDF file (e.g., Media Kit).

    Media Kits often contain valuable contacts: ad sales directors,
    partnership managers, rate cards with contact info, etc.

    Args:
        pdf_content: Raw PDF bytes
        domain: Target domain for email validation

    Returns:
        List of contact dicts with email, name, title
    """
    if not PDF_AVAILABLE:
        return []

    contacts = []
    seen_emails = set()

    try:
        import io
        pdf_file = io.BytesIO(pdf_content)

        with pdfplumber.open(pdf_file) as pdf:
            full_text = ""

            for page in pdf.pages:
                page_text = page.extract_text() or ""
                full_text += page_text + "\n"

                # Also check tables (often contain contact info)
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            full_text += " ".join(str(cell) for cell in row if cell) + "\n"

            # Extract emails from PDF text
            emails = EMAIL_PATTERN.findall(full_text)

            # Also check for obfuscated patterns
            for pattern, _ in EMAIL_OBFUSCATION_PATTERNS:
                obfuscated = re.findall(pattern, full_text, re.IGNORECASE)
                for match in obfuscated:
                    if isinstance(match, tuple):
                        decoded = f"{match[0]}@{match[1]}.{match[2]}"
                        if EMAIL_PATTERN.match(decoded):
                            emails.append(decoded)

            # Process found emails
            for email in emails:
                email_lower = email.lower()
                email_domain = email_lower.split('@')[-1]

                # Skip non-matching domains and spam patterns
                if domain.lower() not in email_domain and email_domain not in domain.lower():
                    continue
                if any(skip in email_lower for skip in ["noreply", "no-reply", "unsubscribe"]):
                    continue
                if email_lower in seen_emails:
                    continue

                seen_emails.add(email_lower)

                # Try to find name/title near the email
                contact = {"email": email, "name": None, "title": None, "source": "pdf_media_kit"}

                # Search for context around email
                email_pos = full_text.lower().find(email_lower)
                if email_pos != -1:
                    context = full_text[max(0, email_pos - 200):email_pos + 50]

                    # Look for title keywords
                    title_keywords = [
                        "Director", "Manager", "VP", "Vice President", "Head of",
                        "Advertising", "Sales", "Marketing", "Partnerships", "Media",
                        "Contact", "Business Development", "Commercial",
                    ]
                    for keyword in title_keywords:
                        if keyword.lower() in context.lower():
                            # Try to extract full title
                            title_match = re.search(
                                rf'({keyword}[^,\n@]*)',
                                context,
                                re.IGNORECASE
                            )
                            if title_match:
                                contact["title"] = title_match.group(1).strip()[:60]
                                break

                    # Look for name (2-3 capitalized words)
                    name_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b')
                    names = name_pattern.findall(context)
                    if names:
                        # Filter out common non-names
                        skip_names = ["Media Kit", "Contact Us", "Sales Team", "Press Room"]
                        for name in names:
                            if name not in skip_names and len(name) > 4:
                                contact["name"] = name
                                break

                contacts.append(contact)

    except Exception as e:
        logger.warning(f"PDF extraction error: {e}")

    return contacts


# High-value page patterns for tiered prioritization (from research document)
# Tier 1 = most likely to have advertising/media contacts
PAGE_PRIORITY_TIERS = {
    "tier1_critical": [
        "media-kit", "mediakit", "media_kit", "advertise", "advertising",
        "partnership", "press-room", "pressroom", "public-relations",
        "ad-sales", "adsales", "sponsor",
    ],
    "tier2_high": [
        "investor-relations", "corporate", "media-assets", "newsroom",
        "press-release", "press", "news",
    ],
    "tier3_moderate": [
        "about-us", "about", "our-team", "team", "leadership",
        "board-of-directors", "management", "contact",
    ],
    "tier4_low": [
        "support", "help", "faq", "customer-service",
    ],
}


def score_url_priority(url: str) -> int:
    """
    Score a URL's priority for contact discovery (higher = better).

    Based on tiered keyword system from research document.
    """
    url_lower = url.lower()

    for keyword in PAGE_PRIORITY_TIERS["tier1_critical"]:
        if keyword in url_lower:
            return 100

    for keyword in PAGE_PRIORITY_TIERS["tier2_high"]:
        if keyword in url_lower:
            return 75

    for keyword in PAGE_PRIORITY_TIERS["tier3_moderate"]:
        if keyword in url_lower:
            return 50

    for keyword in PAGE_PRIORITY_TIERS["tier4_low"]:
        if keyword in url_lower:
            return 10

    return 25  # Default for unknown pages


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
        timeout: float = 5.0,  # Reduced for speed
        max_pages: int = 12,   # Enough to check all important contact pages
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
        self._http_client = None  # Lazy-initialized, reused across all domains
        self._browser = None      # Lazy-initialized browser for JS fallback
        self._playwright = None

        # Common headers to avoid being blocked
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        }

    def _get_http_client(self):
        """Get or create HTTP client (reused across all domains)."""
        if self._http_client is None:
            self._http_client = httpx.Client(
                timeout=self.timeout,
                headers=self.headers,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http_client

    def _get_browser(self):
        """Get or create browser (reused across all JS-rendered domains)."""
        if not PLAYWRIGHT_AVAILABLE:
            return None

        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--no-sandbox",
                    ],
                )
                self._log("Browser started for JS rendering")
            except Exception as e:
                self._log(f"Failed to start browser: {e}", "error")
                return None
        return self._browser

    def _get_claude_agent(self):
        """Get or create Claude agent (lazy initialization, reused across calls)."""
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

    def close(self):
        """Clean up resources (call when done with all scraping)."""
        if self._claude_agent:
            try:
                self._claude_agent.close()
            except Exception:
                pass
            self._claude_agent = None

        if self._http_client:
            try:
                self._http_client.close()
            except Exception:
                pass
            self._http_client = None

        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

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

    def _find_links_with_browser(self, base_url: str, domain: str) -> list[str]:
        """
        Load homepage in browser and find REAL contact-related links.

        Instead of guessing URLs like /contact, /press etc, this reads the
        actual links from the JS-rendered page.

        Returns:
            List of URLs to contact-related pages (found on the actual page)
        """
        if not PLAYWRIGHT_AVAILABLE:
            return []

        browser = self._get_browser()
        if not browser:
            return []

        contact_urls = []
        contact_keywords = [
            # High priority for advertising/partnerships
            "advertise", "advertising", "partnerships", "partner", "sponsor",
            "media kit", "mediakit", "media-kit",
            # Contact pages
            "contact", "contact us", "connect", "get in touch", "reach us",
            # Press/media (often has contacts) - EXPANDED
            "press", "media", "newsroom", "news room", "press room", "news",
            "media relations", "press & media", "press and media",
            "media inquiries", "press inquiries", "press releases",
            "press contacts", "media contacts", "communications",
            # About/team
            "about", "about us", "team", "leadership", "company", "who we are",
        ]

        context = browser.new_context(
            user_agent=self.headers["User-Agent"],
            viewport={"width": 1280, "height": 720},
        )

        try:
            page = context.new_page()
            self._log(f"Browser reading links from: {base_url}")
            page.goto(base_url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(1000)  # Wait for JS to render

            # Find all links on the page
            links = page.query_selector_all('a[href]')
            base_domain = urlparse(base_url).netloc

            for link in links:
                try:
                    href = link.get_attribute("href") or ""
                    text = (link.inner_text() or "").lower().strip()

                    # Skip empty/external/js links
                    if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
                        continue

                    # Build full URL
                    full_url = urljoin(base_url, href)

                    # Only same-domain links
                    if urlparse(full_url).netloc != base_domain:
                        continue

                    # Check if text or href contains contact keywords
                    href_lower = href.lower()
                    if any(kw in text or kw.replace(" ", "-") in href_lower or kw.replace(" ", "") in href_lower
                           for kw in contact_keywords):
                        if full_url not in contact_urls and is_url_worth_visiting(full_url):
                            contact_urls.append(full_url)

                except Exception:
                    continue

        except Exception as e:
            self._log(f"Browser link discovery failed: {e}", "warning")
        finally:
            context.close()

        return contact_urls[:15]  # Limit to 15 most relevant links

    def _click_reveal_email_buttons(self, page) -> int:
        """
        Find and click buttons/links that reveal hidden email addresses.

        Many sites hide emails behind "Show Email", "Reveal Contact" buttons
        to prevent basic scraping. This mimics human behavior to reveal them.

        Returns:
            Number of buttons clicked
        """
        reveal_keywords = [
            "show email", "reveal email", "view email", "see email",
            "show contact", "reveal contact", "view contact",
            "click to reveal", "click here for email", "get email",
            "email address", "contact info", "show address",
            "unhide", "display email",
        ]

        buttons_clicked = 0
        max_clicks = 3  # Limit to prevent infinite loops

        try:
            # Find clickable elements with reveal-like text
            for keyword in reveal_keywords:
                if buttons_clicked >= max_clicks:
                    break

                # Try buttons
                buttons = page.query_selector_all(f'button:has-text("{keyword}")')
                for btn in buttons[:1]:  # Only click first match per keyword
                    try:
                        btn.click(timeout=2000)
                        page.wait_for_timeout(500)  # Wait for reveal animation
                        buttons_clicked += 1
                        self._log(f"  Clicked reveal button: '{keyword}'")
                    except Exception:
                        continue

                # Try links/spans
                links = page.query_selector_all(f'a:has-text("{keyword}"), span:has-text("{keyword}")')
                for link in links[:1]:
                    try:
                        link.click(timeout=2000)
                        page.wait_for_timeout(500)
                        buttons_clicked += 1
                        self._log(f"  Clicked reveal link: '{keyword}'")
                    except Exception:
                        continue

            # Also check for common CSS classes/data attributes
            reveal_selectors = [
                '[data-reveal-email]', '[data-show-email]',
                '.reveal-email', '.show-email', '.email-reveal',
                '[onclick*="email"]', '[onclick*="reveal"]',
            ]
            for selector in reveal_selectors:
                if buttons_clicked >= max_clicks:
                    break
                try:
                    elements = page.query_selector_all(selector)
                    for elem in elements[:1]:
                        elem.click(timeout=2000)
                        page.wait_for_timeout(500)
                        buttons_clicked += 1
                        self._log(f"  Clicked reveal element: '{selector}'")
                except Exception:
                    continue

        except Exception as e:
            self._log(f"  Reveal button search error: {e}", "warning")

        return buttons_clicked

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

        # Use reusable browser (MUCH faster than starting fresh each time)
        browser = self._get_browser()
        if not browser:
            return []

        context = browser.new_context(
            user_agent=self.headers["User-Agent"],
            viewport={"width": 1280, "height": 720},
        )

        try:
            page = context.new_page()

            # Network interception: block unnecessary resources for faster loads
            # This dramatically speeds up scraping while preserving contact info
            def handle_route(route):
                """Block images, fonts, media, and tracking scripts."""
                resource_type = route.request.resource_type
                url = route.request.url.lower()

                # Block resource types that never contain contact info
                blocked_types = {"image", "media", "font", "stylesheet"}
                if resource_type in blocked_types:
                    route.abort()
                    return

                # Block common tracking/analytics scripts
                tracking_domains = [
                    "google-analytics", "googletagmanager", "facebook.net",
                    "doubleclick", "analytics", "tracking", "pixel",
                    "hotjar", "mixpanel", "segment", "amplitude",
                    "intercom", "crisp", "drift", "hubspot",
                ]
                if any(td in url for td in tracking_domains):
                    route.abort()
                    return

                # Allow everything else
                route.continue_()

            # Enable route interception
            page.route("**/*", handle_route)

            crash_count = 0  # Track consecutive crashes

            # Filter URLs and limit to 8 max for speed
            filtered_urls = [u for u in urls if is_url_worth_visiting(u)][:8]

            for url in filtered_urls:
                # Skip if too many crashes (browser is unstable)
                if crash_count >= 3:
                    self._log(f"Stopping browser - too many crashes", "warning")
                    break

                try:
                    self._log(f"Browser loading: {url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=8000)

                    # Brief wait for dynamic content (reduced for speed)
                    page.wait_for_timeout(500)

                    # Get rendered HTML
                    html = page.content()
                    crash_count = 0  # Reset on success

                    # Check for block page (soft 403)
                    if is_block_page(html):
                        self._log(f"  Block page detected, skipping: {url}", "warning")
                        continue

                    # Try to click "reveal email" buttons before extraction
                    self._click_reveal_email_buttons(page)

                    # Re-get HTML after potential reveals
                    html = page.content()

                    # Try to decode any Base64-encoded emails
                    base64_emails = decode_base64_emails(html, domain)
                    for email in base64_emails:
                        if email not in all_emails:
                            all_emails[email] = WebsiteContact(
                                email=email,
                                source_page=url,
                                email_type="generic",
                            )
                            self._log(f"  Found Base64-encoded email: {email}")

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
                    error_str = str(e).lower()
                    if "crash" in error_str or "detach" in error_str:
                        crash_count += 1
                        self._log(f"Browser crash ({crash_count}/3): {url}", "warning")
                        # Try to create new page after crash
                        try:
                            page = context.new_page()
                        except Exception:
                            break  # Context is dead, exit
                    elif "err_name_not_resolved" in error_str:
                        self._log(f"Domain unreachable: {url}", "warning")
                        break  # Skip entire domain
                    else:
                        self._log(f"Browser error on {url}: {e}", "warning")

        finally:
            context.close()

        return list(all_emails.values())

    def _scrape_with_browser_and_navigate(self, domain: str, start_urls: list[str]) -> list[WebsiteContact]:
        """
        Use browser to navigate pages and CLICK THROUGH to find contact links.

        This mimics human behavior:
        1. Go to About page
        2. Look for Press/Media/Contact links
        3. Click through and extract contacts

        Args:
            domain: The domain being scraped
            start_urls: Starting URLs (e.g., /about, /about-us, /company)

        Returns:
            List of contacts found
        """
        if not PLAYWRIGHT_AVAILABLE:
            return []

        all_emails: dict[str, WebsiteContact] = {}

        browser = self._get_browser()
        if not browser:
            return []

        context = browser.new_context(
            user_agent=self.headers["User-Agent"],
            viewport={"width": 1280, "height": 720},
        )

        try:
            page = context.new_page()

            # Keywords to look for in links (for clicking through) - EXPANDED
            click_keywords = [
                "press", "media", "newsroom", "news", "contact", "contact us",
                "press & media", "press and media", "media relations",
                "press room", "media room", "communications",
                "press releases", "media inquiries", "press inquiries",
                "press contacts", "media contacts", "about", "about us",
            ]

            # Keywords to EXCLUDE from clicking (irrelevant links)
            exclude_keywords = [
                "sign in", "login", "register", "sign up", "signup",
                "job", "career", "hiring", "apply", "cart", "checkout",
                "shop", "buy", "subscribe", "trial", "demo", "pricing",
                "facebook", "twitter", "linkedin", "instagram", "youtube",
                "privacy", "terms", "cookie", "legal",
            ]

            for start_url in start_urls:
                if len(all_emails) >= 2:
                    break  # Found enough

                try:
                    self._log(f"Browser navigating: {start_url}")
                    page.goto(start_url, wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(800)

                    # First extract any contacts on this page
                    html = page.content()
                    contacts = self._extract_contacts_from_html(html, start_url, domain)
                    for contact in contacts:
                        if contact.email not in all_emails:
                            all_emails[contact.email] = contact

                    # Look for mailto links
                    mailto_links = page.query_selector_all('a[href^="mailto:"]')
                    for link in mailto_links:
                        href = link.get_attribute("href")
                        if href:
                            email = href.replace("mailto:", "").split("?")[0].strip()
                            if EMAIL_PATTERN.match(email) and email not in all_emails:
                                all_emails[email] = WebsiteContact(
                                    email=email,
                                    source_page=start_url,
                                    email_type="generic",
                                )

                    # Now look for links to click through
                    all_links = page.query_selector_all('a[href]')
                    links_to_click = []

                    for link in all_links:
                        try:
                            text = (link.inner_text() or "").lower().strip()
                            href = (link.get_attribute("href") or "").lower()

                            # Skip empty text links or excluded keywords
                            if not text or len(text) < 2:
                                continue
                            if any(excl in text or excl in href for excl in exclude_keywords):
                                continue

                            # Check if this link looks like it leads to press/contact
                            for keyword in click_keywords:
                                if keyword in text or keyword.replace(" ", "-") in href or keyword.replace(" ", "") in href:
                                    full_href = link.get_attribute("href")
                                    if full_href and not full_href.startswith(("javascript:", "#", "mailto:", "tel:")):
                                        # Also check URL is worth visiting
                                        if is_url_worth_visiting(urljoin(start_url, full_href)):
                                            links_to_click.append((link, text, full_href))
                                        break
                        except Exception:
                            continue

                    # Click through found links (max 2 for speed)
                    for link, text, href in links_to_click[:2]:
                        if len(all_emails) >= 2:
                            break

                        try:
                            self._log(f"  Clicking: '{text[:30]}' -> {href[:50]}")

                            # Navigate to the link (8s timeout, not 30s default)
                            link.click(timeout=8000)
                            page.wait_for_load_state("domcontentloaded", timeout=8000)
                            page.wait_for_timeout(500)

                            # Extract contacts from new page
                            html = page.content()
                            current_url = page.url
                            contacts = self._extract_contacts_from_html(html, current_url, domain)
                            for contact in contacts:
                                if contact.email not in all_emails:
                                    all_emails[contact.email] = contact
                                    self._log(f"    Found: {contact.email}")

                            # Check mailto links on new page
                            mailto_links = page.query_selector_all('a[href^="mailto:"]')
                            for mailto in mailto_links:
                                href = mailto.get_attribute("href")
                                if href:
                                    email = href.replace("mailto:", "").split("?")[0].strip()
                                    if EMAIL_PATTERN.match(email) and email not in all_emails:
                                        all_emails[email] = WebsiteContact(
                                            email=email,
                                            source_page=current_url,
                                            email_type="generic",
                                        )
                                        self._log(f"    Found: {email}")

                            # Go back to try next link
                            page.go_back(wait_until="domcontentloaded", timeout=5000)
                            page.wait_for_timeout(300)

                        except Exception as e:
                            self._log(f"  Click navigation error: {str(e)[:50]}", "warning")
                            # Try to recover by going to next start URL
                            break

                except PlaywrightTimeout:
                    self._log(f"Browser timeout: {start_url}", "warning")
                except Exception as e:
                    self._log(f"Browser error on {start_url}: {e}", "warning")

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

        # Check if domain redirects to a different domain
        # E.g., projectmanagementinstitute.com → pmi.org
        try:
            resolved_domain = resolve_domain_redirect(domain)
            if resolved_domain and resolved_domain != domain:
                self._log(f"Domain redirects: {domain} → {resolved_domain}")
                domain = resolved_domain
        except Exception:
            pass  # Continue with original domain if resolution fails

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
        domain_unreachable = False  # DNS failure = skip everything
        all_emails: dict[str, WebsiteContact] = {}  # email -> contact

        def has_good_contacts() -> bool:
            """Check if we have high-quality contacts worth stopping for."""
            if len(all_emails) < 2:
                return False
            # Only stop early if we have advertising/sales emails
            ad_emails = [c for c in all_emails.values() if c.email_type == "advertising"]
            return len(ad_emails) >= 1 and len(all_emails) >= 3

        # Use reusable HTTP client (MUCH faster than creating new one per domain)
        client = self._get_http_client()

        # Phase 1: Fetch homepage and use Claude to suggest best pages (ONE call)
        homepage_fetched = False

        if self.use_claude:
            try:
                response = client.get(base_url)
                if response.status_code == 200:
                    homepage_fetched = True
                    pages_scraped += 1
                    html = response.text
                    visited_urls.add(base_url)

                    # Extract contacts from homepage
                    page_contacts = self._extract_contacts_from_html(html, base_url, domain)
                    for contact in page_contacts:
                        if contact.email not in all_emails:
                            all_emails[contact.email] = contact

                    # Find ALL internal links on homepage (real links, not guesses)
                    all_internal_links = self._extract_all_internal_links(html, base_url)

                    # Also get keyword-filtered links
                    keyword_links = self._find_contact_links(html, base_url)
                    for new_url in keyword_links:
                        if new_url not in visited_urls and new_url not in urls_to_visit:
                            urls_to_visit.append(new_url)

                    # Check for PDF Media Kits on the homepage
                    pdf_contacts = self._find_and_process_pdfs(html, base_url, domain)
                    for contact in pdf_contacts:
                        if contact.email not in all_emails:
                            all_emails[contact.email] = contact

                    # Use Claude to pick the BEST links from actual links on the page
                    # This is more accurate than Claude guessing paths from raw HTML
                    agent = self._get_claude_agent()
                    if agent and all_internal_links:
                        self._log(f"Claude selecting from {len(all_internal_links)} real links...")

                        # Pass real links to Claude for intelligent selection
                        best_urls = agent.select_best_urls_from_list(
                            all_internal_links, domain, "find advertising/marketing contact information"
                        )

                        if best_urls:
                            self._log(f"Claude selected {len(best_urls)} priority URLs: {[u.split('/')[-1] or u.split('/')[-2] for u in best_urls[:5]]}")
                            # Add Claude's selections to the FRONT of the queue (highest priority)
                            for url in reversed(best_urls):
                                if url not in visited_urls and url not in urls_to_visit:
                                    urls_to_visit.insert(1, url)
            except Exception as e:
                self._log(f"Homepage/Claude analysis failed: {e}", "warning")

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

                # Check for block page (soft 403)
                if is_block_page(html):
                    self._log(f"Block page detected: {url}", "warning")
                    continue

                # Try to decode any Base64-encoded emails
                base64_emails = decode_base64_emails(html, domain)
                for email in base64_emails:
                    if email not in all_emails:
                        all_emails[email] = WebsiteContact(
                            email=email,
                            source_page=url,
                            email_type="generic",
                        )
                        self._log(f"Found Base64-encoded email: {email}")

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
                # This helps find nested navigation like: About Us → Press & Media
                new_urls = self._find_contact_links(html, base_url)

                # Prioritize important links (press, media, contact) - insert near front
                high_priority_keywords = ["press", "media", "contact", "advertise", "partnership"]
                for new_url in new_urls:
                    if new_url not in visited_urls and new_url not in urls_to_visit:
                        # Check if this is a high-priority link
                        url_lower = new_url.lower()
                        if any(kw in url_lower for kw in high_priority_keywords):
                            # Insert near front (after current position in queue)
                            urls_to_visit.insert(pages_scraped + 1, new_url)
                        else:
                            urls_to_visit.append(new_url)

            except httpx.TimeoutException:
                consecutive_errors += 1
                self._log(f"Timeout fetching {url}", "warning")
            except httpx.ConnectError as e:
                consecutive_errors += 1
                # Check if this is a DNS failure (domain doesn't exist)
                error_str = str(e).lower()
                if "name" in error_str and "not" in error_str and "resolve" in error_str:
                    self._log(f"Domain unreachable (DNS): {domain}", "warning")
                    domain_unreachable = True
                    break  # Skip ALL further attempts
                elif "no address" in error_str:
                    self._log(f"Domain unreachable (no address): {domain}", "warning")
                    domain_unreachable = True
                    break
                else:
                    self._log(f"Connection failed: {url}", "warning")
            except Exception as e:
                consecutive_errors += 1
                result.errors.append(f"Error fetching {url}: {str(e)}")

        result.pages_scraped = pages_scraped

        # Phase 2: If httpx found nothing or few results, try Playwright for JS-rendered sites
        # Check if site appears to block HTTP requests (many errors = likely anti-bot)
        site_blocks_http = consecutive_errors >= 2 and pages_scraped == 0

        # Skip browser if domain is completely unreachable (DNS failure)
        if domain_unreachable:
            self._log(f"Skipping browser - domain unreachable: {domain}", "warning")
            result.errors.append(f"Domain unreachable: {domain}")
            return result

        if len(all_emails) < 2 and self.use_browser:
            self._log(f"Few emails via HTTP, trying browser for JS-rendered content...")

            # First, load homepage in browser and find REAL links (not guessing)
            real_links = self._find_links_with_browser(base_url, domain)
            if real_links:
                self._log(f"Browser found {len(real_links)} contact-related links")

            # Combine real links with fallback patterns (real links first)
            priority_urls = [base_url] + real_links
            browser_patterns = [
                "/advertise", "/contact", "/contact-us", "/about", "/about-us",
                "/press", "/media", "/press-media", "/newsroom",
                "/team", "/partnerships", "/media-kit",
            ]
            for pattern in browser_patterns:
                url = urljoin(base_url, pattern)
                if url not in priority_urls:
                    priority_urls.append(url)

            browser_contacts = self._scrape_with_browser(domain, priority_urls)
            for contact in browser_contacts:
                if contact.email not in all_emails:
                    all_emails[contact.email] = contact

        # Phase 2.5: If we have a contact form but no emails, try press/media pages
        # Sites like floqast.com have contact forms but emails are on press pages
        if len(all_emails) == 0:
            # Check if we visited a contact page with a form
            contact_page_visited = any(
                "/contact" in url.lower() or "contact-us" in url.lower()
                for url in visited_urls
            )

            if contact_page_visited:
                self._log(f"Contact page had form but no email, trying press/media pages...")

                # Priority pages to try when contact form has no email
                press_fallback_patterns = [
                    "/press", "/press-media", "/press-room", "/newsroom",
                    "/media", "/media-room", "/about/press", "/about/press-media",
                    "/about/media", "/news", "/company/press", "/en/press",
                    "/about/newsroom", "/corporate/press",
                ]

                for pattern in press_fallback_patterns:
                    if len(all_emails) > 0:
                        break

                    url = urljoin(base_url, pattern)
                    if url in visited_urls:
                        continue

                    try:
                        response = client.get(url)
                        if response.status_code == 200:
                            visited_urls.add(url)
                            html = response.text
                            page_contacts = self._extract_contacts_from_html(html, url, domain)
                            for contact in page_contacts:
                                if contact.email not in all_emails:
                                    all_emails[contact.email] = contact
                                    self._log(f"  Found contact on press page: {pattern}")
                    except Exception:
                        continue

        # Phase 3: THOROUGH SEARCH - if still no contacts, try nested/uncommon paths
        if len(all_emails) == 0:
            self._log(f"No contacts found, trying thorough nested search...")

            # Common nested paths that sites use (About → Press, Company → Contact, etc.)
            nested_patterns = [
                # About section nested pages
                "/about/press", "/about/media", "/about/press-media", "/about/contact",
                "/about/team", "/about/leadership", "/about-us/press", "/about-us/media",
                "/about-us/contact", "/about-us/team", "/about/news", "/about/newsroom",
                # Company section nested pages
                "/company/press", "/company/media", "/company/contact", "/company/team",
                "/company/about", "/company/newsroom",
                # Press/News variations - EXPANDED (for sites like indeed.com)
                "/press-room", "/press-releases", "/media-room", "/media-center",
                "/news/press", "/news/media", "/newsroom/contact", "/newsroom/press",
                "/press/contact", "/media/contact", "/news/contact",
                # Corporate pages
                "/corporate/press", "/corporate/media", "/corporate/contact",
                # Other common patterns
                "/info/press", "/info/contact", "/resources/press", "/resources/media",
                # Language-prefixed pages - EXPANDED (for sites like wolterskluwer.com)
                "/en/press", "/en/contact", "/en/contact-us", "/en/about/press",
                "/en/about/press-media", "/en/newsroom", "/en/media",
                "/en-us/press", "/en-us/contact", "/en-us/contact-us",
                "/en-gb/contact", "/en-gb/press",
                "/us/press", "/us/contact",  # Regional
                # Partners/Advertise nested
                "/partners/contact", "/partnerships/contact", "/advertise/contact",
            ]

            # If site blocks HTTP, skip straight to browser for nested patterns
            if site_blocks_http and self.use_browser:
                self._log(f"Site blocks HTTP, using browser for nested search...")
                nested_urls = [urljoin(base_url, p) for p in nested_patterns[:15]]  # Try first 15
                browser_contacts = self._scrape_with_browser(domain, nested_urls)
                for contact in browser_contacts:
                    if contact.email not in all_emails:
                        all_emails[contact.email] = contact
            else:
                # Try these patterns with HTTP first (fast)
                for pattern in nested_patterns:
                    if len(all_emails) > 0:
                        break  # Found something, stop

                    url = urljoin(base_url, pattern)
                    if url in visited_urls:
                        continue

                    try:
                        response = client.get(url)
                        if response.status_code == 200:
                            visited_urls.add(url)
                            html = response.text
                            page_contacts = self._extract_contacts_from_html(html, url, domain)
                            for contact in page_contacts:
                                if contact.email not in all_emails:
                                    all_emails[contact.email] = contact
                                    self._log(f"  Found contact on nested page: {pattern}")
                    except Exception:
                        continue

            # If STILL nothing, use browser to navigate About page and find links (CLICK THROUGH)
            # Reduced to 2 URLs max to prevent hanging
            if len(all_emails) == 0 and self.use_browser:
                self._log(f"Trying browser click-through navigation...")
                about_urls = [
                    urljoin(base_url, "/about"),
                    urljoin(base_url, "/about-us"),
                ]
                browser_contacts = self._scrape_with_browser_and_navigate(domain, about_urls)
                for contact in browser_contacts:
                    if contact.email not in all_emails:
                        all_emails[contact.email] = contact

        # Phase 4: If STILL no contacts and domain is long, try alternative domains
        # E.g., projectmanagementinstitute.com → pmi.org
        if len(all_emails) == 0 and len(domain.split('.')[0]) > 15:
            self._log(f"Long domain with no results, trying alternatives...")
            alternative_domains = guess_alternative_domains(domain)

            for alt_domain in alternative_domains[:3]:  # Limit to 3 alternatives
                if len(all_emails) > 0:
                    break

                alt_base_url = f"https://{alt_domain}"
                try:
                    response = client.get(alt_base_url)
                    if response.status_code == 200:
                        self._log(f"  Trying alternative domain: {alt_domain}")
                        html = response.text

                        # Extract contacts from homepage
                        page_contacts = self._extract_contacts_from_html(html, alt_base_url, alt_domain)
                        for contact in page_contacts:
                            if contact.email not in all_emails:
                                all_emails[contact.email] = contact

                        # Also try /contact and /press on alternative domain
                        for pattern in ["/contact", "/contact-us", "/press", "/about/press-media"]:
                            if len(all_emails) > 0:
                                break
                            alt_url = urljoin(alt_base_url, pattern)
                            try:
                                response = client.get(alt_url)
                                if response.status_code == 200:
                                    html = response.text
                                    page_contacts = self._extract_contacts_from_html(html, alt_url, alt_domain)
                                    for contact in page_contacts:
                                        if contact.email not in all_emails:
                                            all_emails[contact.email] = contact
                                            self._log(f"  Found contact on {alt_domain}: {contact.email}")
                            except Exception:
                                continue

                        if all_emails:
                            # Update result domain to the working alternative
                            result.domain = alt_domain
                            self._log(f"  Using alternative domain: {alt_domain}")
                            break
                except Exception:
                    continue

        # NOTE: Claude agent is NOT closed here - reused across multiple scrape_domain() calls
        # Call scraper.close() when done with all scraping to clean up

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

        # Also check mailto: links - enhanced to find more mailto patterns
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:"):
                email = href[7:].split("?")[0].strip()
                if EMAIL_PATTERN.match(email):
                    emails.add(email)

        # Check buttons and other elements that might have mailto (forms, etc.)
        for element in soup.find_all(href=True):
            href = element.get("href", "")
            if "mailto:" in href:
                # Extract email from mailto: link
                email_part = href.split("mailto:")[-1].split("?")[0].strip()
                if EMAIL_PATTERN.match(email_part):
                    emails.add(email_part)

        # Check form actions for mailto (some contact forms use mailto: action)
        for form in soup.find_all("form", action=True):
            action = form.get("action", "")
            if action.startswith("mailto:"):
                email = action[7:].split("?")[0].strip()
                if EMAIL_PATTERN.match(email):
                    emails.add(email)

        # Check for emails in any href attribute (spans, divs with href, etc.)
        for element in soup.find_all(attrs={"href": True}):
            href = element.get("href", "")
            if "mailto:" in href.lower():
                email_part = href.lower().split("mailto:")[-1].split("?")[0].strip()
                if EMAIL_PATTERN.match(email_part):
                    emails.add(email_part)

        # Check onclick handlers for emails (some sites use JS to build email)
        for element in soup.find_all(onclick=True):
            onclick = element.get("onclick", "")
            email_matches = EMAIL_PATTERN.findall(onclick)
            emails.update(email_matches)

        # Check for emails in JavaScript code blocks (common for obfuscation)
        for script in soup.find_all("script"):
            script_text = script.get_text() or ""
            # Look for email patterns in JS (often concatenated or in variables)
            email_matches = EMAIL_PATTERN.findall(script_text)
            # Also check for obfuscated patterns
            for pattern, replacement in EMAIL_OBFUSCATION_PATTERNS:
                obfuscated = re.findall(pattern, script_text, re.IGNORECASE)
                for match in obfuscated:
                    if isinstance(match, tuple):
                        decoded = f"{match[0]}@{match[1]}.{match[2]}"
                        if EMAIL_PATTERN.match(decoded):
                            email_matches.append(decoded)
            emails.update(email_matches)

        # Check for emails in input field placeholders/values (contact forms sometimes show example)
        for input_elem in soup.find_all("input"):
            placeholder = input_elem.get("placeholder", "")
            value = input_elem.get("value", "")
            for text in [placeholder, value]:
                email_matches = EMAIL_PATTERN.findall(text)
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

            # IMPORTANT: Only accept emails from the target domain
            # This prevents picking up random emails from subdomains or other sites
            # e.g., for hsbc.com, accept info@hsbc.com but not cc.life@mail.life.hsbc.com.sg
            target_domain = domain.lower()
            if not (domain_part == target_domain or domain_part.endswith("." + target_domain)):
                # Check if it's the same root domain (e.g., hsbc.com vs hsbc.co.uk)
                target_parts = target_domain.split(".")
                email_parts = domain_part.split(".")
                if len(target_parts) >= 2 and len(email_parts) >= 2:
                    # Compare the main domain name (e.g., "hsbc")
                    if target_parts[-2] != email_parts[-2]:
                        continue
                    # If email has too many subdomains, skip (likely regional subdomain)
                    if len(email_parts) > 3:
                        continue
                else:
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

    def _has_contact_form(self, html: str) -> bool:
        """
        Check if a page has a contact form (without a mailto: email).

        This is used to detect form-only contact pages where we should look
        for alternative pages that might have actual email addresses.
        """
        soup = BeautifulSoup(html, "lxml")

        # Look for forms that appear to be contact forms
        for form in soup.find_all("form"):
            form_html = str(form).lower()
            action = form.get("action", "").lower()

            # Skip if it's a mailto: form (we can extract email from those)
            if "mailto:" in action:
                return False

            # Check if form looks like a contact form
            contact_indicators = [
                "contact", "message", "inquiry", "enquiry", "get in touch",
                "name", "email", "phone", "subject", "send",
            ]
            form_text = form.get_text().lower()

            # Count how many indicators are present
            matches = sum(1 for ind in contact_indicators if ind in form_text or ind in form_html)

            # If form has multiple contact indicators, it's likely a contact form
            if matches >= 3:
                return True

        # Also check for common contact form containers
        contact_containers = [
            '[class*="contact-form"]', '[class*="contact_form"]',
            '[id*="contact-form"]', '[id*="contact_form"]',
            '[class*="inquiry"]', '[class*="enquiry"]',
        ]
        for selector in contact_containers:
            if soup.select(selector):
                return True

        return False

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
                    # Extract the title portion - but limit length to avoid garbage
                    title_match = re.search(
                        rf'({keyword}[^,\n@(]*)',
                        container_text,
                        re.IGNORECASE
                    )
                    if title_match:
                        extracted_title = title_match.group(1).strip()
                        # Only accept titles that are reasonable length
                        if len(extracted_title) <= 50:
                            title = extracted_title
                            break

            # Look for a name (capitalized words before the email or title)
            # Simple heuristic: 2-3 capitalized words in a row
            name_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b')
            name_matches = name_pattern.findall(container_text)
            if name_matches:
                # Filter out common non-name phrases and department names
                skip_names = [
                    "Contact Us", "Get In Touch", "Learn More", "Read More",
                    "About Us", "Our Team", "Meet The Team", "The Team",
                    # Department/role names that aren't people
                    "Small Business", "Business Development", "Customer Service",
                    "Human Resources", "Public Relations", "Media Relations",
                    "Investor Relations", "Corporate Communications", "Sales Team",
                    "Marketing Team", "Support Team", "Press Office",
                    "Minority Business", "Development Center", "Chamber Of Commerce",
                ]
                skip_patterns = ["Center", "Centre", "Office", "Team", "Department", "Division", "Relations"]
                for match in name_matches:
                    # Skip if it matches known non-names
                    if match in skip_names:
                        continue
                    # Skip if it contains department-like words
                    if any(pattern in match for pattern in skip_patterns):
                        continue
                    # Skip if it's too long (likely not a person's name)
                    if len(match.split()) > 3 or len(match) > 30:
                        continue
                    name = match
                    break

            if name or title:
                break

        return name, title

    def _extract_all_internal_links(self, html: str, base_url: str) -> list[str]:
        """
        Extract ALL internal links from HTML page.

        Used to give Claude real links to choose from instead of guessing paths.

        Args:
            html: Page HTML content
            base_url: Base URL for resolving relative links

        Returns:
            List of unique internal URLs found on the page
        """
        soup = BeautifulSoup(html, "lxml")
        links = []
        base_domain = urlparse(base_url).netloc

        # Skip patterns - these are never useful for contact discovery
        skip_patterns = [
            "#", "javascript:", "mailto:", "tel:",
            ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js",
            "/wp-content/", "/wp-includes/", "/assets/",
        ]

        for a in soup.find_all("a", href=True):
            href = a["href"]

            # Skip empty or problematic links
            if not href or any(href.startswith(p) or p in href.lower() for p in skip_patterns):
                continue

            # Build full URL
            try:
                full_url = urljoin(base_url, href)
                parsed = urlparse(full_url)

                # Only internal links (same domain)
                if parsed.netloc != base_domain:
                    continue

                # Clean up URL (remove fragments, normalize)
                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if parsed.query:
                    # Keep simple query params, skip tracking params
                    if not any(p in parsed.query.lower() for p in ["utm_", "ref=", "source="]):
                        clean_url += f"?{parsed.query}"

                # Skip if already in list
                if clean_url not in links:
                    links.append(clean_url)

            except Exception:
                continue

        return links[:50]  # Limit to 50 links to avoid overwhelming Claude

    def _find_and_process_pdfs(self, html: str, base_url: str, domain: str) -> list[WebsiteContact]:
        """
        Find PDF links (especially Media Kits) and extract contacts from them.

        Media Kits are gold mines for ad sales contacts but are often overlooked
        by HTML-only scrapers.
        """
        if not PDF_AVAILABLE:
            return []

        contacts = []
        soup = BeautifulSoup(html, "lxml")

        # Keywords that indicate valuable PDFs
        pdf_keywords = [
            "media kit", "mediakit", "media-kit", "rate card", "ratecard",
            "advertising", "ad specs", "press kit", "presskit",
            "partnership", "sponsor", "media guide",
        ]

        # Find PDF links
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").lower()
            text = a.get_text().lower().strip()

            # Check if it's a PDF link
            if ".pdf" not in href:
                continue

            # Check if it's a high-value PDF (media kit, rate card, etc.)
            is_valuable = any(kw in href or kw in text for kw in pdf_keywords)
            if not is_valuable:
                continue

            # Build full URL
            pdf_url = urljoin(base_url, a.get("href", ""))

            self._log(f"Found potential Media Kit PDF: {pdf_url}")

            try:
                # Download PDF
                client = self._get_http_client()
                response = client.get(pdf_url, timeout=10.0)

                if response.status_code == 200 and len(response.content) < 10_000_000:  # Max 10MB
                    # Extract contacts from PDF
                    pdf_contacts = extract_contacts_from_pdf(response.content, domain)

                    for pc in pdf_contacts:
                        contact = WebsiteContact(
                            email=pc["email"],
                            source_page=pdf_url,
                            email_type="advertising" if any(
                                kw in (pc.get("title") or "").lower()
                                for kw in ["advertising", "sales", "media", "partnership"]
                            ) else "generic",
                            name=pc.get("name"),
                            title=pc.get("title"),
                        )
                        contacts.append(contact)
                        self._log(f"  Found in PDF: {contact.email}")

            except Exception as e:
                self._log(f"  PDF download/parse error: {e}", "warning")

        return contacts

    def _find_contact_links(self, html: str, base_url: str) -> list[str]:
        """Find links to contact-related pages."""
        soup = BeautifulSoup(html, "lxml")
        contact_urls = []

        # Expanded keywords - prioritize advertising/sales related
        # NOTE: These match partial strings, so "press" matches "press-media", "press-room", etc.
        contact_keywords = [
            # Advertising/sales (highest priority for our use case)
            "advertise", "advertising", "sponsors", "sponsorship", "media-kit",
            "mediakit", "ad-sales", "partnerships", "partner", "affiliate",
            # Contact pages - EXPANDED
            "contact", "connect", "get-in-touch", "reach-us", "reach-out",
            "talk-to-us", "inquiry", "enquiry", "inquiries", "enquiries",
            # Team/about pages
            "about", "team", "leadership", "people", "management", "company",
            # Press/media (often has contacts) - EXPANDED for nested nav
            "press", "newsroom", "media", "news", "pr", "communications",
            "media-relations", "press-room", "press-media", "media-center",
            "public-relations", "corporate-communications",
            # Business
            "for-business", "business", "enterprise", "commercial",
        ]

        # Footer-specific keywords (text that often appears in footer links)
        footer_link_texts = [
            "press", "press room", "press releases", "newsroom", "news room",
            "media", "media room", "media relations", "media inquiries",
            "contact", "contact us", "get in touch", "reach us",
            "about", "about us", "our company", "who we are",
            "partnerships", "partner with us", "advertise", "advertising",
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

        # Also look specifically in footer and nav sections - ENHANCED
        for section in soup.find_all(["footer", "nav"]):
            for a in section.find_all("a", href=True):
                href = a["href"]
                text = a.get_text().lower().strip()
                if not href or href.startswith(("javascript:", "#")):
                    continue
                full_url = urljoin(base_url, href)
                try:
                    if urlparse(full_url).netloc == base_domain:
                        href_lower = href.lower()
                        # Check both href and link text for footer keywords
                        if any(kw in href_lower or kw in text for kw in ["contact", "about", "team", "advertise", "press", "media", "news", "newsroom"]):
                            if full_url not in contact_urls:
                                contact_urls.append(full_url)
                        # Also check for exact footer text matches
                        if any(text == ft or text.startswith(ft) for ft in footer_link_texts):
                            if full_url not in contact_urls:
                                contact_urls.append(full_url)
                except Exception:
                    continue

        # Look for links in elements with footer-like class/id names
        footer_selectors = [
            '[class*="footer"]', '[id*="footer"]',
            '[class*="bottom-nav"]', '[class*="site-footer"]',
            '[role="contentinfo"]',  # Accessibility role for footer
        ]
        for selector in footer_selectors:
            for container in soup.select(selector):
                for a in container.find_all("a", href=True):
                    href = a["href"]
                    text = a.get_text().lower().strip()
                    if not href or href.startswith(("javascript:", "#")):
                        continue
                    full_url = urljoin(base_url, href)
                    try:
                        if urlparse(full_url).netloc == base_domain:
                            # Any link with press/contact keywords in footer areas
                            if any(kw in text or kw in href.lower() for kw in ["press", "media", "contact", "news", "about"]):
                                if full_url not in contact_urls:
                                    contact_urls.append(full_url)
                    except Exception:
                        continue

        return list(dict.fromkeys(contact_urls))  # Deduplicate

    def _is_relevant_title(self, title: str | None) -> bool:
        """Check if a job title is relevant for advertising/partnership outreach."""
        if not title:
            return True  # No title = can't exclude, keep it

        title_lower = title.lower()

        # Check for excluded titles first
        for excluded in EXCLUDED_JOB_TITLES:
            if excluded in title_lower:
                return False

        # Check if it matches relevant titles
        for relevant in RELEVANT_JOB_TITLES:
            if relevant in title_lower:
                return True

        # If we have a title but it doesn't match relevant ones,
        # only keep if it's a generic/department email
        return False

    def _prioritize_contacts(
        self,
        contacts: list[WebsiteContact],
    ) -> list[WebsiteContact]:
        """
        Filter and sort contacts for ad sales outreach.

        Only keeps:
        - Contacts with relevant job titles (marketing, media, partnerships, etc.)
        - Generic department emails (info@, contact@, advertising@)
        - Removes irrelevant roles (engineering, legal, HR, etc.)
        """
        filtered = []

        for contact in contacts:
            email_prefix = contact.email.lower().split("@")[0]

            # Always keep advertising/partnership specific emails
            if contact.email_type == "advertising":
                filtered.append(contact)
                continue

            # Always keep generic contact emails (info@, contact@, hello@)
            generic_prefixes = ["info", "contact", "hello", "inquiries", "enquiries",
                              "advertising", "ads", "partnerships", "media", "press",
                              "marketing", "sales", "business"]
            if any(email_prefix.startswith(p) for p in generic_prefixes):
                filtered.append(contact)
                continue

            # For personal emails, check if the job title is relevant
            if contact.title:
                if self._is_relevant_title(contact.title):
                    filtered.append(contact)
                else:
                    self._log(f"    Filtered out: {contact.email} ({contact.title}) - not relevant role")
            else:
                # No title - keep it but lower priority
                filtered.append(contact)

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
                for relevant in RELEVANT_JOB_TITLES:
                    if relevant in title_lower:
                        score += 30
                        break
            if contact.phone:
                score += 10
            if contact.linkedin_url:
                score += 10

            # Specific prefix bonuses
            priority_prefixes = ["advertising", "ads", "partnerships", "marketing", "sales", "media"]
            if any(prefix in email_prefix for prefix in priority_prefixes):
                score += 40

            return score

        return sorted(filtered, key=priority_score, reverse=True)


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
