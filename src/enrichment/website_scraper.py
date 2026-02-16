"""Website contact scraper - Unchained High Performance.

Fixes:
1. REMOVED BROWSER_LOCK (was causing parallel threads to block)
2. Increased max_browser_time to 60s (was 25s) for Deep Drilling
3. Increased max_domain_time to 100s to match App timeout
"""

import re
import logging
import base64
import random
import time
import threading
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from contextlib import contextmanager

import httpx
from bs4 import BeautifulSoup

# --- THREAD-LOCAL BROWSER MANAGER ---
class ThreadLocalBrowserManager:
    _thread_local = threading.local()
    _global_semaphore = threading.Semaphore(8)  # Limit concurrent browsers

    @classmethod
    def get_browser(cls):
        if hasattr(cls._thread_local, 'browser') and cls._thread_local.browser:
            return cls._thread_local.browser

        try:
            from playwright.sync_api import sync_playwright
            cls._thread_local.playwright = sync_playwright().start()
            cls._thread_local.browser = cls._thread_local.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            return cls._thread_local.browser
        except Exception:
            return None

    @classmethod
    def acquire_context_slot(cls, timeout=30):
        return cls._global_semaphore.acquire(timeout=timeout)

    @classmethod
    def release_context_slot(cls):
        try: cls._global_semaphore.release()
        except: pass

    @classmethod
    def close_thread_browser(cls):
        if hasattr(cls._thread_local, 'browser') and cls._thread_local.browser:
            try: cls._thread_local.browser.close()
            except: pass
            cls._thread_local.browser = None

# Alias for backward compatibility
GlobalBrowserManager = ThreadLocalBrowserManager

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeout = Exception

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

from ..utils.helpers import strip_marketing_subdomain, resolve_domain_redirect, guess_alternative_domains

logger = logging.getLogger(__name__)

USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

def get_random_user_agent() -> str:
    return random.choice(USER_AGENT_POOL)

@dataclass
class WebsiteContact:
    email: str
    source_page: str
    email_type: str = "unknown"
    name: str | None = None
    title: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None

@dataclass
class WebsiteScrapeResult:
    domain: str
    contacts: list[WebsiteContact] = field(default_factory=list)
    pages_scraped: int = 0
    errors: list[str] = field(default_factory=list)

SKIP_PATTERNS = [
    'noreply', 'no-reply', 'donotreply', 'unsubscribe', 'bounce', 'billing',
    'invoice', 'accounts', 'legal', 'compliance', 'privacy', 'gdpr',
    'support', 'help', 'desk', 'service', 'returns', 'shipping', 'jobs',
    'career', 'careers', 'recruiting', 'hr', 'talent', 'intern',
    'sentry', 'bugsnag', 'wixpress', 'example.com', 'email.com'
]

EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

class WebsiteScraper:
    def __init__(
        self,
        timeout: float = 15.0,
        max_pages: int = 12,
        max_errors: int = 3,
        use_browser: bool = True,
        use_claude: bool = True,
        verify_emails: bool = True,
        log_callback: callable = None,
        cancel_check: callable = None,
    ):
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_errors = max_errors
        self.use_browser = use_browser and PLAYWRIGHT_AVAILABLE
        self.use_claude = use_claude
        self.verify_emails = verify_emails
        self._log_callback = log_callback
        self._cancel_check = cancel_check
        self._claude_agent = None
        self._http_client = None

        self.headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    def _get_http_client(self):
        if self._http_client is None:
            self._http_client = httpx.Client(
                timeout=self.timeout,
                headers=self.headers,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http_client

    def _get_browser(self):
        if not PLAYWRIGHT_AVAILABLE: return None
        return GlobalBrowserManager.get_browser()

    def _get_claude_agent(self):
        if self._claude_agent is None and self.use_claude:
            try:
                from .claude_agent import ClaudeAgent
                self._claude_agent = ClaudeAgent(log_callback=self._log_callback)
            except ImportError:
                self._claude_agent = None
        return self._claude_agent

    def close(self):
        if self._http_client:
            try: self._http_client.close()
            except: pass
        if self._claude_agent:
            try: self._claude_agent.close()
            except: pass

    def _log(self, message: str, level: str = "info"):
        if self._log_callback: self._log_callback(message, level)
        if level == "error": logger.error(message)
        elif level == "warning": logger.warning(message)
        else: logger.info(message)

    def _is_cancelled(self) -> bool:
        if self._cancel_check:
            try: return self._cancel_check()
            except: return False
        return False

    @contextmanager
    def _browser_context(self):
        if not PLAYWRIGHT_AVAILABLE:
            yield None
            return

        acquired = GlobalBrowserManager.acquire_context_slot(timeout=30)
        if not acquired:
            self._log("Could not acquire browser slot", "warning")
            yield None
            return

        context = None
        try:
            browser = GlobalBrowserManager.get_browser()
            if not browser:
                GlobalBrowserManager.release_context_slot()
                yield None
                return

            context = browser.new_context(
                user_agent=get_random_user_agent(),
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
            )
            yield context
        except Exception as e:
            self._log(f"Context error: {e}", "warning")
            GlobalBrowserManager.close_thread_browser() # Reset
            yield None
        finally:
            if context:
                try: context.close()
                except: pass
            GlobalBrowserManager.release_context_slot()

    def _find_links_with_browser(self, base_url: str, domain: str) -> list[str]:
        """
        Load homepage with browser and find real links to contact/about/advertise pages.

        Specifically looks in:
        - Navigation menus (header, nav elements)
        - Footer links (where contact info is most common)
        - Hamburger/mobile menu contents
        - Body content links
        """
        if not PLAYWRIGHT_AVAILABLE:
            return []

        contact_urls = []
        keywords = [
            "contact", "about", "team", "advertise", "advertising",
            "partner", "press", "media", "sponsor", "leadership",
            "get in touch", "reach us", "media kit", "work with us",
        ]

        with self._browser_context() as context:
            if not context:
                return []
            try:
                page = context.new_page()
                page.set_default_timeout(15000)

                # Block heavy assets for speed
                def handle_route(route):
                    if route.request.resource_type in ["image", "media", "font"]:
                        route.abort()
                    else:
                        route.continue_()
                try:
                    page.route("**/*", handle_route)
                except Exception:
                    pass

                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)  # Wait for JS hydration

                # Scroll to bottom to reveal footer content
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)

                # Extract ALL links from the page
                links = page.query_selector_all('a[href]')
                for link in links:
                    try:
                        href = link.get_attribute("href") or ""
                        text = (link.inner_text() or "").lower().strip()
                        href_lower = href.lower()

                        # Match by link text or href path
                        matched = any(
                            k in text or k in href_lower
                            for k in keywords
                        )
                        if not matched:
                            continue

                        full = urljoin(base_url, href)
                        parsed = urlparse(full)
                        # Only follow internal links
                        if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
                            continue
                        # Skip anchors and javascript
                        if not parsed.scheme or parsed.scheme not in ("http", "https"):
                            continue

                        contact_urls.append(full)
                    except Exception:
                        continue

            except Exception as e:
                self._log(f"Link discovery error: {e}", "warning")

        return list(set(contact_urls))[:20]

    def _scrape_with_browser(self, domain: str, urls: list[str]) -> list[WebsiteContact]:
        """
        Browser-based page scraper. Visits each URL, scrolls to footer,
        and extracts emails from rendered HTML.
        """
        if not PLAYWRIGHT_AVAILABLE:
            return []

        import time
        browser_start = time.time()
        max_browser_time = 60

        all_emails: dict[str, WebsiteContact] = {}

        with self._browser_context() as context:
            if not context:
                return []

            page = context.new_page()
            page.set_default_timeout(20000)

            # Block heavy assets for speed
            def handle_route(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()
            try:
                page.route("**/*", handle_route)
            except Exception:
                pass

            for url in urls[:self.max_pages]:
                if (time.time() - browser_start) > max_browser_time:
                    self._log("Browser time limit reached", "warning")
                    break

                if self._is_cancelled():
                    break

                try:
                    self._log(f"  Browser: {url}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000)

                    # Scroll to bottom to reveal footer content
                    # (many emails are only in the footer)
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

                    content = page.content()
                    contacts = self._extract_contacts_from_html(content, url, domain)
                    for c in contacts:
                        all_emails[c.email.lower()] = c

                except Exception as e:
                    logger.debug(f"Browser error on {url}: {e}")
                    continue

        return list(all_emails.values())

    # Common paths where contact/advertising emails are found
    CONTACT_PATHS = [
        "/contact", "/contact-us", "/get-in-touch",
        "/about", "/about-us", "/about/contact",
        "/advertise", "/advertising", "/media-kit", "/mediakit",
        "/partnerships", "/sponsors", "/sponsor",
        "/press", "/press-room", "/newsroom", "/media",
        "/team", "/leadership", "/our-team", "/about/team",
    ]

    def scrape_domain(self, domain: str, company_name: str | None = None) -> WebsiteScrapeResult:
        """
        Comprehensive email scraper for a company domain.

        Phase 1: HTTP scan — homepage + common contact/about/advertise pages
        Phase 2: Browser discovery — find real navigation links (footer, nav)
        Phase 3: Browser deep scrape — visit all discovered URLs with rendering
        Phase 4: Claude extraction — catch obfuscated emails if available

        Args:
            domain: Company domain (e.g., 'spartan.com')
            company_name: Optional company name for Claude context

        Returns:
            WebsiteScrapeResult with all found contacts
        """
        import time
        domain_start_time = time.time()
        max_domain_time = 100

        result = WebsiteScrapeResult(domain=domain)
        if domain.startswith("www."):
            domain = domain[4:]
        domain = strip_marketing_subdomain(domain)
        base_url = f"https://{domain}"

        all_emails: dict[str, WebsiteContact] = {}
        pages_scraped = 0
        visited_urls = set()

        def _time_remaining():
            return max_domain_time - (time.time() - domain_start_time)

        def _add_contacts(contacts: list[WebsiteContact]):
            for c in contacts:
                if c.email.lower() not in all_emails:
                    all_emails[c.email.lower()] = c

        # === PHASE 1: HTTP scan of homepage + common contact pages ===
        client = self._get_http_client()
        valid_paths = set()  # Track paths that returned 200 (for browser phase)
        consecutive_404s = 0

        # Build the list of URLs to try via HTTP
        http_urls = [base_url]
        for path in self.CONTACT_PATHS:
            http_urls.append(f"{base_url}{path}")

        for url in http_urls:
            if self._is_cancelled() or _time_remaining() < 5:
                break
            if url in visited_urls:
                continue
            # Stop probing paths if the site consistently returns 404
            if consecutive_404s >= 5 and url != base_url:
                break

            try:
                resp = client.get(url, follow_redirects=True)
                visited_urls.add(url)

                if resp.status_code == 200:
                    consecutive_404s = 0
                    pages_scraped += 1
                    valid_paths.add(url)
                    contacts = self._extract_contacts_from_html(resp.text, url, domain)
                    _add_contacts(contacts)
                    if contacts:
                        self._log(f"  HTTP {url} -> {len(contacts)} email(s)")
                else:
                    consecutive_404s += 1
            except Exception as e:
                logger.debug(f"HTTP failed for {url}: {e}")
                consecutive_404s += 1
                continue

            # Stop HTTP phase early if we have enough
            if len(all_emails) >= 5 and pages_scraped >= 3:
                break

        self._log(f"Phase 1 (HTTP): {len(all_emails)} email(s) from {pages_scraped} pages")

        # === PHASE 2: Browser discovery + deep scrape ===
        # Always use browser when available — finds JS-rendered emails and
        # discovers real navigation links (footer, hamburger menu, etc.)
        if self.use_browser and _time_remaining() > 10:
            # Collect browser URLs: common paths + discovered links
            browser_urls = []

            # Start with pages we haven't visited yet
            for path in self.CONTACT_PATHS:
                url = f"{base_url}{path}"
                if url not in visited_urls:
                    browser_urls.append(url)

            # Always include homepage for link discovery even if HTTP visited it
            browser_urls.insert(0, base_url)

            # Find real links via browser (footer links, nav links, etc.)
            try:
                real_links = self._find_links_with_browser(base_url, domain)
                for link in real_links:
                    if link not in visited_urls and link not in browser_urls:
                        browser_urls.append(link)
            except Exception as e:
                logger.debug(f"Browser link discovery failed: {e}")

            # Use Claude to pick best URLs if available
            claude = self._get_claude_agent()
            if claude and claude.is_configured and browser_urls:
                try:
                    ai_picks = claude.select_best_urls_from_list(
                        browser_urls[:30], domain,
                        goal="find advertising/marketing contact email addresses"
                    )
                    if ai_picks:
                        # Put AI picks first, then remaining URLs
                        seen = set(ai_picks)
                        browser_urls = ai_picks + [u for u in browser_urls if u not in seen]
                except Exception as e:
                    logger.debug(f"Claude URL selection failed: {e}")

            # Deduplicate and limit
            unique_browser = []
            seen_browser = set(visited_urls)
            for u in browser_urls:
                if u not in seen_browser:
                    seen_browser.add(u)
                    unique_browser.append(u)
            # Always include homepage for footer scraping even if HTTP visited it
            if base_url not in unique_browser:
                unique_browser.insert(0, base_url)

            # Browser scrape all discovered URLs
            if unique_browser:
                browser_contacts = self._scrape_with_browser(domain, unique_browser)
                _add_contacts(browser_contacts)
                pages_scraped += len(unique_browser)

            self._log(f"Phase 2 (Browser): {len(all_emails)} total email(s)")

        # === PHASE 3: Claude AI extraction for obfuscated emails ===
        if self.use_claude and _time_remaining() > 5:
            claude = self._get_claude_agent()
            if claude and claude.is_configured:
                # Re-fetch homepage for Claude analysis if we have few emails
                if len(all_emails) < 3:
                    try:
                        resp = client.get(base_url, follow_redirects=True)
                        if resp.status_code == 200:
                            ai_contacts = claude.extract_contacts_from_page(
                                resp.text, base_url,
                                company_name=company_name or domain,
                                skip_filter=True,
                            )
                            for ac in ai_contacts:
                                email = ac.get("email", "").lower().strip()
                                if email and "@" in email and email not in all_emails:
                                    contact = WebsiteContact(
                                        email=email,
                                        source_page=base_url,
                                        email_type=ac.get("type", "generic"),
                                        name=ac.get("name"),
                                        title=ac.get("title"),
                                    )
                                    all_emails[email] = contact
                    except Exception as e:
                        logger.debug(f"Claude extraction failed: {e}")

                self._log(f"Phase 3 (Claude): {len(all_emails)} total email(s)")

        # Prioritize: advertising/media emails first, then generic
        contacts = sorted(
            all_emails.values(),
            key=lambda c: (0 if c.email_type == "advertising" else 1, c.email),
        )
        result.contacts = contacts
        result.pages_scraped = pages_scraped

        self._log(f"Done: {len(result.contacts)} email(s) for {domain}")
        return result

    # Obfuscated email patterns (name [at] domain [dot] com, etc.)
    OBFUSCATED_PATTERNS = [
        # name [at] domain [dot] com
        re.compile(r'([\w.+-]+)\s*\[at\]\s*([\w.-]+)\s*\[dot\]\s*(\w{2,})', re.IGNORECASE),
        # name (at) domain (dot) com
        re.compile(r'([\w.+-]+)\s*\(at\)\s*([\w.-]+)\s*\(dot\)\s*(\w{2,})', re.IGNORECASE),
        # name {at} domain {dot} com
        re.compile(r'([\w.+-]+)\s*\{at\}\s*([\w.-]+)\s*\{dot\}\s*(\w{2,})', re.IGNORECASE),
        # name AT domain DOT com
        re.compile(r'([\w.+-]+)\s+AT\s+([\w.-]+)\s+DOT\s+(\w{2,})'),
    ]

    def _extract_contacts_from_html(self, html: str, source_url: str, domain: str) -> list[WebsiteContact]:
        """
        Extract email contacts from HTML content.

        Searches:
        1. Standard email regex across full page
        2. mailto: links
        3. Obfuscated emails (name [at] domain [dot] com)
        4. Footer sections specifically (where many emails hide)
        5. HTML entity decoded emails (&#64; for @)
        """
        contacts = []
        found_emails: set[str] = set()

        # Decode HTML entities that might hide @ symbols
        decoded_html = html.replace("&#64;", "@").replace("&#x40;", "@")
        decoded_html = decoded_html.replace("(at)", "@").replace("[at]", "@")

        # 1. Standard email regex
        emails = set(EMAIL_PATTERN.findall(decoded_html))

        # 2. Extract from mailto: links (most reliable signal)
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if href.startswith("mailto:"):
                email = href.replace("mailto:", "").split("?")[0].strip().lower()
                if email and "@" in email:
                    emails.add(email)

        # 3. Obfuscated email patterns
        for pattern in self.OBFUSCATED_PATTERNS:
            for match in pattern.finditer(html):
                email = f"{match.group(1)}@{match.group(2)}.{match.group(3)}".lower()
                emails.add(email)

        # 4. Specifically parse footer content (emails often only in footer)
        footer_elements = soup.find_all(["footer"]) or []
        # Also look for elements with footer-like classes
        for el in soup.find_all(True, class_=re.compile(r'footer|bottom|colophon', re.I)):
            footer_elements.append(el)
        # Also try id-based footer
        footer_by_id = soup.find(id=re.compile(r'footer|bottom', re.I))
        if footer_by_id:
            footer_elements.append(footer_by_id)

        for footer in footer_elements:
            footer_text = str(footer)
            footer_decoded = footer_text.replace("&#64;", "@").replace("&#x40;", "@")
            footer_emails = EMAIL_PATTERN.findall(footer_decoded)
            emails.update(footer_emails)
            # Also check footer mailto links
            for link in footer.find_all("a", href=True):
                href = link.get("href", "")
                if href.startswith("mailto:"):
                    email = href.replace("mailto:", "").split("?")[0].strip().lower()
                    if email and "@" in email:
                        emails.add(email)

        # Filter and classify emails
        for email in emails:
            email = email.lower().strip()

            # Skip obvious spam/system emails
            if any(s in email for s in SKIP_PATTERNS):
                continue

            # Skip obviously invalid emails
            if len(email) < 5 or len(email) > 80:
                continue
            if not re.match(r'^[\w.+-]+@[\w.-]+\.\w{2,}$', email):
                continue

            # Classify by prefix
            prefix = email.split('@')[0]
            priority_prefixes = [
                "ads", "ad", "adops", "adsales",
                "media", "mediasales", "mediakit",
                "advertising", "sponsor", "sponsorship", "sponsors",
                "partner", "partnerships", "bizdev",
                "marketing", "press", "pr", "communications",
            ]
            is_priority = any(p == prefix or prefix.startswith(p) for p in priority_prefixes)
            is_generic_business = prefix in (
                "info", "hello", "contact", "sales", "business",
                "general", "inquiries", "team",
            )

            # Accept: emails matching the domain, priority prefixes, or generic business
            email_domain = email.split("@")[1] if "@" in email else ""
            domain_match = domain in email_domain or email_domain.endswith(f".{domain}")

            if not domain_match and not is_priority and not is_generic_business:
                continue

            if email in found_emails:
                continue
            found_emails.add(email)

            etype = "advertising" if is_priority else "generic"
            contacts.append(WebsiteContact(
                email=email, source_page=source_url, email_type=etype,
            ))

        return contacts
