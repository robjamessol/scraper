"""Website contact scraper - Ultimate Edition.

Combines ALL working features:
1. Thread-Safe Browser (ThreadLocalBrowserManager)
2. Safe Link Discovery (page.evaluate - no ElementHandle crash)
3. Vision with Scroll (reveals footer emails)
4. Redirect Detection (PMI.org case)
5. TLD Fallback (.org first for PMI, then .co, .io, .net)
6. Deep Drill Navigation (Indeed case - 3 clicks deep)
7. [at] Obfuscation Detection
8. Fast HTTP Scan before Browser fallback
9. Click-Reveal Buttons
10. Relaxed Domain Matching with Priority Prefixes
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
    """Thread-safe browser management - each thread gets its own browser."""
    _thread_local = threading.local()
    _global_semaphore = threading.Semaphore(6)  # Max 6 concurrent browser contexts

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
        if hasattr(cls._thread_local, 'playwright') and cls._thread_local.playwright:
            try: cls._thread_local.playwright.stop()
            except: pass
            cls._thread_local.playwright = None

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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
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

# Priority prefixes that indicate advertising/marketing contacts
PRIORITY_PREFIXES = [
    "ads", "ad", "advert", "advertising", "media", "press", "pr",
    "marketing", "partner", "partnerships", "sponsor", "sponsorship",
    "business", "biz", "sales", "commercial", "brand", "brands",
]

EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
# Pattern for obfuscated emails like "email [at] domain.com"
EMAIL_OBFUSCATED = re.compile(r'([a-zA-Z0-9._-]+)\s*\[at\]\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})')


class WebsiteScraper:
    def __init__(
        self,
        timeout: float = 15.0,
        max_pages: int = 15,
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

    def close_thread_browser(self):
        """Force close thread browser to prevent memory leaks."""
        GlobalBrowserManager.close_thread_browser()

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
        """Get a browser context with semaphore protection."""
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
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
                bypass_csp=True,
            )
            yield context
        except Exception as e:
            self._log(f"Context error: {e}", "warning")
            GlobalBrowserManager.close_thread_browser()
            yield None
        finally:
            if context:
                try: context.close()
                except: pass
            GlobalBrowserManager.release_context_slot()

    def _click_reveal_email_buttons(self, page):
        """Click 'Show Email' / 'Reveal' buttons to uncover hidden emails."""
        try:
            selectors = [
                'button:has-text("Show Email")', 'a:has-text("Show Email")',
                'button:has-text("View Email")', 'a:has-text("View Email")',
                'button:has-text("Reveal")', 'a:has-text("Reveal")',
                'button:has-text("Contact")',
                '[class*="reveal"]', '[id*="reveal"]',
                '[class*="show-email"]', '[class*="email-btn"]',
                '[data-email]', '[data-contact]',
            ]
            for sel in selectors:
                try:
                    if page.is_visible(sel):
                        page.click(sel, timeout=500)
                        page.wait_for_timeout(200)
                except: pass
        except: pass

    def _find_contact_links_http(self, html: str, base_url: str) -> list[str]:
        """Fast HTTP-based link discovery (no browser needed)."""
        try:
            soup = BeautifulSoup(html, "lxml")
        except:
            soup = BeautifulSoup(html, "html.parser")

        links = []
        keywords = ["contact", "about", "team", "advertise", "partner", "press",
                    "media", "news", "newsroom", "company", "sponsor"]

        for a in soup.find_all("a", href=True):
            href = a['href']
            text = a.get_text().lower()
            if any(k in text or k in href.lower() for k in keywords):
                full = urljoin(base_url, href)
                # Same domain check
                if urlparse(full).netloc == urlparse(base_url).netloc:
                    links.append(full)

        return list(set(links))

    def _find_links_with_browser(self, base_url: str, domain: str) -> list[str]:
        """SAFE Link Discovery using page.evaluate() - prevents ElementHandle crash."""
        if not PLAYWRIGHT_AVAILABLE: return []

        contact_urls = []
        keywords = ["contact", "about", "team", "advertise", "partner", "press",
                    "media", "news", "newsroom", "brand", "sales", "sponsorship"]

        with self._browser_context() as context:
            if not context: return []
            try:
                page = context.new_page()
                page.set_default_timeout(15000)
                page.goto(base_url, wait_until="domcontentloaded")

                # CRITICAL: Use JS evaluation instead of Python ElementHandle loop
                # This prevents "ElementHandle.inner_text: Node is not an HTMLElement" crash
                raw_links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        href: a.href,
                        text: (a.innerText || '').toLowerCase()
                    }));
                }""")

                for link in raw_links:
                    href = link.get('href', '')
                    text = link.get('text', '')
                    if any(k in text or k in href.lower() for k in keywords):
                        if urlparse(href).netloc == urlparse(base_url).netloc:
                            contact_urls.append(href)

            except Exception as e:
                self._log(f"Link discovery error: {e}", "warning")

        return list(set(contact_urls))[:20]

    def _scrape_with_browser(self, domain: str, urls: list[str]) -> list[WebsiteContact]:
        """Deep Drilling Browser Scraper - follows press/newsroom links."""
        if not PLAYWRIGHT_AVAILABLE: return []

        browser_start = time.time()
        max_browser_time = 120
        all_emails = {}
        visited_deep_links = set()

        with self._browser_context() as context:
            if not context: return []

            page = context.new_page()
            page.set_default_timeout(20000)

            # Block heavy resources
            def handle_route(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()
            try: page.route("**/*", handle_route)
            except: pass

            queue = list(urls[:10])
            processed = 0

            while queue and processed < 15:
                if (time.time() - browser_start) > max_browser_time:
                    self._log("Browser time limit reached", "warning")
                    break

                if self._is_cancelled(): break

                url = queue.pop(0)
                if url in visited_deep_links:
                    continue
                visited_deep_links.add(url)

                try:
                    self._log(f"Browser visiting: {url}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)  # Hydration wait

                    self._click_reveal_email_buttons(page)

                    content = page.content()

                    # Check for Cloudflare block
                    if "cf-browser-verification" in content or "just a moment" in content.lower():
                        self._log(f"Block detected on {url}", "warning")
                        continue

                    # Extract emails
                    found = self._extract_contacts_from_html(content, url, domain)
                    for c in found:
                        all_emails[c.email] = c

                    # DEEP DRILL: If we haven't found good emails, look for press/newsroom links
                    has_good_email = any(c.email_type == "advertising" for c in all_emails.values())
                    if not has_good_email and processed < 10:
                        try:
                            soup = BeautifulSoup(content, "lxml")
                        except:
                            soup = BeautifulSoup(content, "html.parser")

                        for a in soup.find_all("a", href=True):
                            txt = a.get_text().lower()
                            href = a['href']
                            # Look for press/newsroom/media-kit links
                            if any(k in txt or k in href.lower() for k in ["press", "newsroom", "media kit", "media-kit", "news room"]):
                                full = urljoin(url, href)
                                if urlparse(full).netloc == urlparse(url).netloc and full not in visited_deep_links:
                                    self._log(f"  Deep Drill target found: {full}")
                                    queue.insert(0, full)  # Priority visit
                                    break  # Only add one deep link per page

                    # Early exit if we found advertising email
                    if any(c.email_type == "advertising" for c in found):
                        self._log("Found advertising contact, stopping early")
                        break

                    processed += 1

                except Exception as e:
                    self._log(f"Browser error on {url}: {str(e)[:80]}", "warning")
                    continue

        return list(all_emails.values())

    def _try_vision_fallback(self, base_url: str, domain: str) -> list[WebsiteContact]:
        """Use Claude Vision to find contacts - scrolls to reveal footer emails."""
        contacts = []
        if not self.use_claude: return []

        with self._browser_context() as context:
            if not context: return []
            try:
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                # CRITICAL: Scroll to bottom to reveal footer emails
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)

                screenshot = page.screenshot(type='jpeg', quality=60)
                b64_img = base64.b64encode(screenshot).decode('utf-8')

                agent = self._get_claude_agent()
                if agent and hasattr(agent, 'api_key'):
                    data = extract_contacts_from_screenshot(b64_img, base_url, agent.api_key)
                    for c in data:
                        email = c.get('email')
                        if email:
                            contacts.append(WebsiteContact(email=email, source_page=base_url, email_type="vision"))
            except Exception as e:
                self._log(f"Vision fallback error: {e}", "warning")

        return contacts

    def scrape_domain(self, domain: str, company_name: str | None = None) -> WebsiteScrapeResult:
        """Main scraping method with redirect handling and TLD fallback."""
        domain_start_time = time.time()
        max_domain_time = 160

        # Clean domain
        if domain.startswith("www."): domain = domain[4:]
        domain = strip_marketing_subdomain(domain)
        final_domain = domain
        base_url = f"https://{domain}"

        client = self._get_http_client()
        resp = None

        # Phase 0: Resolve domain (handle redirects and TLD fallback)
        try:
            resp = client.get(base_url, timeout=10.0)
            # CRITICAL: Cast httpx URL to string before urlparse
            final_host = urlparse(str(resp.url)).netloc
            if final_host.startswith("www."): final_host = final_host[4:]

            if final_host != domain:
                self._log(f"Redirect detected: {domain} -> {final_host}")
                final_domain = final_host
                base_url = str(resp.url)

        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            # TLD Fallback - check .org first (for PMI), then others
            self._log(f"Domain unreachable: {domain}. Trying alternatives...")
            alternatives = [domain.rsplit('.', 1)[0] + ext for ext in ['.org', '.co', '.io', '.net']]

            for alt in alternatives:
                try:
                    self._log(f"  Trying {alt}...")
                    resp = client.get(f"https://{alt}", timeout=5.0)
                    if resp.status_code == 200:
                        self._log(f"  Success! Using {alt}")
                        final_domain = alt
                        base_url = f"https://{alt}"
                        break
                except: continue

            if resp is None or resp.status_code != 200:
                return WebsiteScrapeResult(domain=domain, errors=["Domain unreachable"])

        result = WebsiteScrapeResult(domain=final_domain)
        all_emails = {}
        visited_urls = set()

        # Phase 1: Fast HTTP Scan
        try:
            if resp and resp.status_code == 200:
                # Extract from homepage
                for c in self._extract_contacts_from_html(resp.text, base_url, final_domain):
                    all_emails[c.email] = c

                # Find contact page links
                contact_urls = self._find_contact_links_http(resp.text, base_url)

                # Add common contact paths
                for p in ["/contact", "/about", "/advertise", "/media-kit", "/press",
                          "/partners", "/company", "/newsroom", "/about-us", "/contact-us"]:
                    contact_urls.append(urljoin(base_url, p))

                contact_urls = list(set(contact_urls))

                # HTTP scan contact pages
                for url in contact_urls[:12]:
                    if self._is_cancelled(): break
                    if url in visited_urls: continue
                    visited_urls.add(url)

                    try:
                        r = client.get(url, timeout=8.0)
                        if r.status_code == 200:
                            for c in self._extract_contacts_from_html(r.text, url, final_domain):
                                all_emails[c.email] = c
                    except: continue
        except Exception as e:
            self._log(f"HTTP scan error: {e}", "warning")

        # Phase 2: Browser Scan (if needed)
        has_good_email = any(c.email_type in ["advertising", "marketing"] for c in all_emails.values())

        if (len(all_emails) < 2 or not has_good_email) and self.use_browser:
            self._log(f"Deep scraping {final_domain} with browser...")

            browser_urls = [base_url]
            for p in ["/contact", "/about", "/press", "/advertise", "/media-kit",
                      "/partners", "/newsroom", "/company", "/news"]:
                browser_urls.append(urljoin(base_url, p))

            # Also find links via browser (catches JS-rendered links)
            real_links = self._find_links_with_browser(base_url, final_domain)
            browser_urls.extend(real_links)
            browser_urls = list(set(browser_urls))

            browser_contacts = self._scrape_with_browser(final_domain, browser_urls)
            for c in browser_contacts:
                all_emails[c.email] = c

            # Phase 3: Vision Fallback
            has_good_email = any(c.email_type in ["advertising", "marketing"] for c in all_emails.values())
            if not has_good_email:
                self._log("No advertising contacts found. Trying Vision AI...")
                vision_contacts = self._try_vision_fallback(base_url, final_domain)
                for c in vision_contacts:
                    all_emails[c.email] = c

        # Phase 4: SMTP Verification
        contacts_list = list(all_emails.values())
        if self.verify_emails:
            from .email_finder import verify_email_smtp_permissive
            verified = []
            for c in contacts_list:
                status = verify_email_smtp_permissive(c.email)
                if status != "invalid":
                    verified.append(c)
            result.contacts = verified
        else:
            result.contacts = contacts_list

        # Deduplicate
        result.contacts = list({c.email: c for c in result.contacts}.values())
        self._log(f"Finished {final_domain}: Found {len(result.contacts)} contacts")
        return result

    def _extract_contacts_from_html(self, html: str, source_url: str, domain: str) -> list[WebsiteContact]:
        """Extract emails with relaxed domain matching and [at] obfuscation detection."""
        contacts = []

        # Find normal emails
        emails = set(EMAIL_PATTERN.findall(html))

        # Find obfuscated emails like "email [at] domain.com"
        for match in EMAIL_OBFUSCATED.findall(html):
            emails.add(f"{match[0]}@{match[1]}")

        # Extract base domain name for partial matching
        domain_parts = domain.replace('www.', '').split('.')
        base_name = domain_parts[0] if domain_parts else domain

        for email in emails:
            email = email.lower().strip()

            # Skip garbage
            if any(s in email for s in SKIP_PATTERNS):
                continue

            # Skip image extensions that got picked up
            if any(email.endswith(ext) for ext in ['.png', '.jpg', '.gif', '.svg', '.webp']):
                continue

            prefix = email.split('@')[0]
            email_domain = email.split('@')[1] if '@' in email else ''
            email_base = email_domain.split('.')[0] if email_domain else ''

            # Check if this is a priority/advertising email
            is_priority = any(p in prefix for p in PRIORITY_PREFIXES)

            # Relaxed domain matching
            is_domain_match = (
                email.endswith(domain) or           # Exact: @company.com
                domain in email_domain or           # Partial: @corp.company.com
                base_name in email_base or          # Base match: company in company.co
                email_base in base_name or          # Reverse: co in company
                (len(base_name) > 3 and base_name in email)  # Company name anywhere
            )

            # Accept if: domain matches OR priority prefix (ads@anything)
            if is_domain_match or is_priority:
                etype = "advertising" if is_priority else "generic"
                contacts.append(WebsiteContact(email=email, source_page=source_url, email_type=etype))
            # Also accept corporate emails found on company site (not gmail/yahoo/etc)
            elif email_domain and not any(skip in email_domain for skip in ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com']):
                contacts.append(WebsiteContact(email=email, source_page=source_url, email_type="discovered"))

        return contacts


# --- REQUIRED EXPORTS (prevent 502 crash) ---

def scrape_website_for_contacts(domain: str, company_name: str | None = None) -> WebsiteScrapeResult:
    """Convenience function to scrape a website for contacts."""
    scraper = WebsiteScraper()
    try:
        return scraper.scrape_domain(domain, company_name)
    finally:
        scraper.close()


def html_to_clean_text(html: str, max_length: int = 5000) -> str:
    """Convert HTML to clean text for Claude processing."""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        text = ' '.join(text.split())
        return text[:max_length]
    except Exception:
        return html[:max_length]


EMAIL_CLASSIFICATION_EXAMPLES = [
    {"email": "ads@example.com", "type": "advertising", "reason": "ads prefix indicates advertising department"},
    {"email": "media@example.com", "type": "advertising", "reason": "media prefix for media buying"},
    {"email": "press@example.com", "type": "advertising", "reason": "press/PR contact for media"},
    {"email": "partnerships@example.com", "type": "advertising", "reason": "partnerships for business development"},
    {"email": "marketing@example.com", "type": "marketing", "reason": "general marketing department"},
    {"email": "john.smith@example.com", "type": "personal", "reason": "personal name format"},
    {"email": "info@example.com", "type": "generic", "reason": "generic catch-all"},
    {"email": "support@example.com", "type": "skip", "reason": "customer support, not decision maker"},
    {"email": "careers@example.com", "type": "skip", "reason": "HR/recruiting, not relevant"},
]


def extract_contacts_from_screenshot(b64_image: str, source_url: str, api_key: str) -> list[dict]:
    """Use Claude Vision to extract contacts from a screenshot."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract any email addresses visible in this screenshot. Return as JSON array: [{\"email\": \"...\"}]. If none found, return []."
                    }
                ],
            }],
        )

        import json
        text = response.content[0].text
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        return []
    except Exception:
        return []
