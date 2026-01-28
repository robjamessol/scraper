"""Website contact scraper - Unified Master Version.

Combines:
1. Thread-Safe High Performance (ThreadLocalBrowser)
2. Smart Logic (Click-Reveal, Vision, Redirects, TLD Fallback)
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
                bypass_csp=True,
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

    def _click_reveal_email_buttons(self, page):
        """Click 'Show Email' buttons."""
        try:
            selectors = ['button:has-text("Show Email")', 'a:has-text("Show Email")', '[class*="reveal"]', '[id*="reveal"]']
            for sel in selectors:
                if page.is_visible(sel):
                    page.click(sel, timeout=500)
                    page.wait_for_timeout(200)
        except: pass

    def _find_links_with_browser(self, base_url: str, domain: str) -> list[str]:
        """Load homepage and find real links. NO LOCKING."""
        if not PLAYWRIGHT_AVAILABLE: return []

        contact_urls = []
        keywords = ["contact", "about", "team", "advertise", "partner", "press", "media", "news"]

        with self._browser_context() as context:
            if not context: return []
            try:
                page = context.new_page()
                page.set_default_timeout(15000)
                page.goto(base_url, wait_until="domcontentloaded")

                # Extract links
                links = page.query_selector_all('a[href]')
                for link in links:
                    href = link.get_attribute("href") or ""
                    text = (link.inner_text() or "").lower()
                    if any(k in text or k in href.lower() for k in keywords):
                        full = urljoin(base_url, href)
                        if urlparse(full).netloc == urlparse(base_url).netloc:
                            contact_urls.append(full)
            except Exception as e:
                self._log(f"Link discovery error: {e}", "warning")

        return list(set(contact_urls))[:15]

    def _scrape_with_browser(self, domain: str, urls: list[str]) -> list[WebsiteContact]:
        """Main browser scraper with Click-Reveal logic."""
        if not PLAYWRIGHT_AVAILABLE: return []

        import time
        browser_start = time.time()
        max_browser_time = 60 # 60s for Deep Drill

        all_emails = {}

        with self._browser_context() as context:
            if not context: return []

            page = context.new_page()
            page.set_default_timeout(20000)

            # Block heavy assets
            def handle_route(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()
            try: page.route("**/*", handle_route)
            except: pass

            for url in urls[:10]:
                if (time.time() - browser_start) > max_browser_time:
                    self._log("Browser time limit reached", "warning")
                    break

                if self._is_cancelled(): break

                try:
                    self._log(f"Browser visiting: {url}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000) # Hydration wait

                    # RESTORED: Click "Show Email" buttons
                    self._click_reveal_email_buttons(page)

                    content = page.content()
                    contacts = self._extract_contacts_from_html(content, url, domain)
                    for c in contacts: all_emails[c.email] = c

                    # Stop if we found good emails
                    if any(c.email_type == "advertising" for c in contacts):
                        break

                except Exception:
                    continue

        return list(all_emails.values())

    def _try_vision_fallback(self, base_url: str, domain: str) -> list[WebsiteContact]:
        """Use Claude Vision to find contacts on hard sites."""
        contacts = []
        if not self.use_claude: return []

        with self._browser_context() as context:
            if not context: return []
            try:
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

                # RESTORED: Vision Fallback
                screenshot = page.screenshot(type='jpeg', quality=60)
                b64_img = base64.b64encode(screenshot).decode('utf-8')

                # Use the module-level function
                agent = self._get_claude_agent()
                if agent and hasattr(agent, 'api_key'):
                    data = extract_contacts_from_screenshot(b64_img, base_url, agent.api_key)
                else:
                    data = []

                for c in data:
                    email = c.get('email')
                    if email:
                        contacts.append(WebsiteContact(email=email, source_page=base_url, email_type="vision"))
            except Exception as e:
                pass

        return contacts

    def scrape_domain(self, domain: str, company_name: str | None = None) -> WebsiteScrapeResult:
        import time
        domain_start_time = time.time()
        max_domain_time = 100

        # RESTORED: Redirect & TLD Handling
        if domain.startswith("www."): domain = domain[4:]
        domain = strip_marketing_subdomain(domain)
        final_domain = domain
        base_url = f"https://{domain}"

        client = self._get_http_client()
        try:
            resp = client.get(base_url, timeout=10.0)
            final_host = urlparse(resp.url).netloc
            if final_host.startswith("www."): final_host = final_host[4:]
            if final_host != domain:
                self._log(f"Redirect detected: {domain} -> {final_host}")
                final_domain = final_host
                base_url = str(resp.url)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # TLD Fallback
            alternatives = [domain.rsplit('.', 1)[0] + ext for ext in ['.co', '.io', '.org']]
            for alt in alternatives:
                try:
                    resp = client.get(f"https://{alt}", timeout=5.0)
                    if resp.status_code == 200:
                        self._log(f"Swapping {domain} -> {alt}")
                        final_domain = alt
                        base_url = f"https://{alt}"
                        break
                except: continue

        result = WebsiteScrapeResult(domain=final_domain)
        all_emails = {}

        # 1. HTTP Scan
        try:
            if resp and resp.status_code == 200:
                contacts = self._extract_contacts_from_html(resp.text, base_url, final_domain)
                for c in contacts: all_emails[c.email] = c
        except: pass

        # 2. Browser Scan (if needed)
        has_good_email = any(c.email_type in ["advertising", "marketing"] for c in all_emails.values())
        if (len(all_emails) < 2 or not has_good_email) and self.use_browser:
            browser_urls = [base_url]
            for p in ["/contact", "/about", "/press", "/advertise", "/media-kit"]:
                browser_urls.append(urljoin(base_url, p))

            real_links = self._find_links_with_browser(base_url, final_domain)
            browser_urls.extend(real_links)

            browser_contacts = self._scrape_with_browser(final_domain, list(set(browser_urls)))
            for c in browser_contacts: all_emails[c.email] = c

            # 3. Vision Fallback (RESTORED)
            if not all_emails:
                self._log("Vision AI Fallback...")
                vision_contacts = self._try_vision_fallback(base_url, final_domain)
                for c in vision_contacts: all_emails[c.email] = c

        # 4. SMTP Verification (RESTORED)
        contacts_list = list(all_emails.values())
        if self.verify_emails:
            from .email_finder import verify_email_smtp_permissive
            verified = []
            for c in contacts_list:
                status = verify_email_smtp_permissive(c.email)
                if status != "invalid": # Keep valid + unknown
                    verified.append(c)
            result.contacts = verified
        else:
            result.contacts = contacts_list

        result.contacts = list({c.email: c for c in result.contacts}.values()) # Dedup
        self._log(f"Finished {final_domain}: Found {len(result.contacts)} contacts")
        return result

    def _extract_contacts_from_html(self, html: str, source_url: str, domain: str) -> list[WebsiteContact]:
        contacts = []
        emails = set(EMAIL_PATTERN.findall(html))

        for email in emails:
            email = email.lower()
            if any(s in email for s in SKIP_PATTERNS): continue

            # Relaxed matching
            prefix = email.split('@')[0]
            is_priority = any(p in prefix for p in ["ads", "media", "press", "marketing", "partner"])

            if not email.endswith(domain) and domain not in email and not is_priority:
                continue

            etype = "advertising" if is_priority else "generic"
            contacts.append(WebsiteContact(email=email, source_page=source_url, email_type=etype))

        return contacts


def scrape_website_for_contacts(domain: str, company_name: str | None = None) -> WebsiteScrapeResult:
    """
    Convenience function to scrape a website for contacts.

    Args:
        domain: The domain to scrape
        company_name: Optional company name for context

    Returns:
        WebsiteScrapeResult with found contacts
    """
    scraper = WebsiteScraper()
    try:
        return scraper.scrape_domain(domain, company_name)
    finally:
        scraper.close()


def html_to_clean_text(html: str, max_length: int = 5000) -> str:
    """Convert HTML to clean text for Claude processing."""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        # Remove script and style elements
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        # Collapse whitespace
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
        # Try to parse JSON from response
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        return []
    except Exception:
        return []
