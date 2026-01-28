"""Website contact scraper - Ultimate Edition v5 (AI-Verified Domain + Better Filtering).

v5 Improvements:
1. AI-verified domain resolution: Uses Claude to verify correct company match
2. Relaxed email verification: More permissive SMTP + keep high-confidence emails
3. Better domain matching: Smarter company-to-domain correlation
4. Debug logging: Track exactly why emails are filtered out
5. Multiple search result evaluation: Check top 3 results, not just first

v4 Detection Improvements:
1. Expanded PRIORITY_PREFIXES: hello, contact, info, inquiries, newsletter, etc.
2. Multiple obfuscation patterns: [at], (at), {at}, HTML entities (&#64;)
3. Explicit mailto: link extraction
4. Original domain tracking for redirect matching (PMI case)
5. Less aggressive early exit: Collect 3+ ad emails before stopping
6. Expanded page paths: /team, /leadership, /advertising, /sponsor, etc.
7. Expanded deep drill keywords: advertise, sponsor, partner with us
8. Acronym-based domain guessing (projectmanagementinstitute.com -> pmi.org)
9. Structured contact text extraction ("Media Contact:", "Press Contact:")
10. Additional paths: /investor-relations, /media/contacts, /corporate

v3 Speed Optimizations:
- Fast-path exit after 2+ ad emails on homepage, 3+ overall
- Reduced timeouts: HTTP 5s, browser 10s, per-domain 100s total
- Browser time limit: 60s

Previous fixes (v2):
- PMI Redirect: Browser-based redirect detection when HTTP times out
- Indeed Blocking: Track blocked pages, skip Vision if all blocked
- Link Discovery Bug: Ensure href/text are strings before .lower()
- Deep Drill: Filter out query-string URLs, look for real press paths
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
    _global_semaphore = threading.Semaphore(6)

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

PRIORITY_PREFIXES = [
    # Core advertising
    "ads", "ad", "advert", "advertising", "advertise",
    # Media/PR
    "media", "press", "pr", "newsroom", "editorial", "comms", "communications",
    # Marketing
    "marketing", "brand", "brands", "branding", "growth", "demand",
    # Partnerships/Business
    "partner", "partners", "partnerships", "sponsor", "sponsorship", "sponsorships",
    "business", "biz", "bizdev", "sales", "commercial", "enterprise", "revenue",
    # Inquiries/Contact
    "hello", "contact", "info", "inquiries", "inquiry", "reach", "team",
    # Newsletter-specific
    "newsletter", "digest", "subscribe", "editor",
    # Leadership (often respond to ad inquiries)
    "ceo", "cmo", "founder", "cofounder",
]

# Additional patterns that indicate high-value business contacts
HIGH_VALUE_PATTERNS = [
    "head of", "director", "vp of", "chief", "manager",
]

EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

# Multiple obfuscation patterns
EMAIL_OBFUSCATED_PATTERNS = [
    re.compile(r'([a-zA-Z0-9._-]+)\s*\[at\]\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'),  # [at]
    re.compile(r'([a-zA-Z0-9._-]+)\s*\(at\)\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'),  # (at)
    re.compile(r'([a-zA-Z0-9._-]+)\s*\{at\}\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'),  # {at}
    re.compile(r'([a-zA-Z0-9._-]+)\s+at\s+([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})', re.IGNORECASE),  # "at" with spaces
    re.compile(r'([a-zA-Z0-9._-]+)\s*\[dot\]\s*([a-zA-Z0-9.-]+)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'),  # [dot] in local part
]

# mailto: link pattern
MAILTO_PATTERN = re.compile(r'mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})', re.IGNORECASE)

# Structured contact patterns (e.g., "Media Contact: email@domain.com")
CONTACT_LABEL_PATTERN = re.compile(
    r'(?:media\s*contact|press\s*contact|pr\s*contact|for\s*(?:media\s*)?inquiries|'
    r'contact\s*us|advertising\s*contact|sponsor\s*contact)[:\s]*'
    r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})',
    re.IGNORECASE
)


def _search_for_domain(company_name: str, timeout: float = 3.0, api_key: str = None) -> str | None:
    """Search the web to find a company's actual domain with AI verification.

    E.g., "Project Management Institute" -> pmi.org (not pmi.com)

    Uses Claude AI to verify that the found domain actually matches the company
    to avoid incorrect matches like pmi.com (power management) vs pmi.org (Project Management Institute).
    """
    try:
        # Use DuckDuckGo HTML search (no API key needed)
        search_query = f"{company_name} official website"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        # DuckDuckGo HTML search
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": search_query},
            headers=headers,
            timeout=timeout,
            follow_redirects=True
        )

        if resp.status_code != 200:
            return None

        # Parse results - get TOP 5 candidates (not just first)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        candidates = []
        skip_domains = ['google.', 'bing.', 'yahoo.', 'duckduckgo.',
                       'facebook.', 'twitter.', 'linkedin.', 'wikipedia.',
                       'youtube.', 'instagram.', 'reddit.', 'quora.']

        # DuckDuckGo results are in <a class="result__a"> tags
        for result in soup.select("a.result__a")[:10]:  # Check first 10 results
            href = result.get("href", "")
            title = result.get_text(strip=True) or ""
            if href and "http" in href:
                # Extract domain from the URL
                parsed = urlparse(href)
                domain = parsed.netloc
                if domain.startswith("www."):
                    domain = domain[4:]
                # Skip search engines and social media
                if not any(skip in domain for skip in skip_domains):
                    candidates.append({"domain": domain, "title": title, "url": href})
                    if len(candidates) >= 5:  # Get top 5 candidates
                        break

        if not candidates:
            return None

        # If only 1 candidate, return it directly
        if len(candidates) == 1:
            return candidates[0]["domain"]

        # Use AI to select the correct domain if we have Claude API key
        if api_key:
            best_domain = _ai_verify_domain(company_name, candidates, api_key)
            if best_domain:
                return best_domain

        # Fallback: Simple heuristic - prefer .org for institutions, acronym matching
        company_lower = company_name.lower()
        company_words = company_lower.split()

        # Check if any candidate domain contains company name or acronym
        acronym = ''.join(w[0] for w in company_words if w)

        for c in candidates:
            domain = c["domain"].lower()
            domain_base = domain.rsplit('.', 1)[0]

            # Exact acronym match (e.g., "pmi" for "project management institute")
            if domain_base == acronym:
                # Prefer .org for institutes/associations
                if 'institute' in company_lower or 'association' in company_lower:
                    if domain.endswith('.org'):
                        return c["domain"]
                # Otherwise return the match
                return c["domain"]

        # Check for company name substring match
        for c in candidates:
            domain = c["domain"].lower()
            title = c.get("title", "").lower()
            # Title contains company name (high confidence)
            if company_lower in title or all(w in title for w in company_words):
                return c["domain"]

        # Last resort: return first candidate
        return candidates[0]["domain"]

    except Exception:
        return None


def _ai_verify_domain(company_name: str, candidates: list[dict], api_key: str) -> str | None:
    """Use Claude AI to verify which domain matches the company.

    Args:
        company_name: The company we're looking for (e.g., "Project Management Institute")
        candidates: List of search result candidates [{domain, title, url}, ...]
        api_key: Anthropic API key

    Returns:
        The correct domain or None if AI verification fails
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        # Format candidates for the prompt
        candidates_text = "\n".join([
            f"{i+1}. {c['domain']} - {c['title']}"
            for i, c in enumerate(candidates)
        ])

        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""I'm looking for the official website of "{company_name}".

Here are the search results:
{candidates_text}

Which domain is MOST LIKELY to be the official website of "{company_name}"?
Reply with ONLY the domain name (e.g., "pmi.org") and nothing else.
If none match, reply "NONE"."""
            }],
        )

        result = response.content[0].text.strip().lower()

        if result == "none":
            return None

        # Validate the response matches one of our candidates
        for c in candidates:
            if c["domain"].lower() == result or result in c["domain"].lower():
                return c["domain"]

        return None
    except Exception:
        return None


def _generate_acronym_domains(domain: str) -> list[str]:
    """Generate acronym-based domain alternatives for long company names.

    E.g., projectmanagementinstitute.com -> pmi.org, pmi.com
    """
    base = domain.rsplit('.', 1)[0]  # Remove TLD

    # Skip if already short (likely already an acronym)
    if len(base) < 15:
        return []

    # Try to extract acronym from camelCase or word boundaries
    # Split on common word boundaries
    words = re.split(r'(?=[A-Z])|[-_]', base)
    words = [w for w in words if w and len(w) > 0]

    # If no clear word boundaries, try splitting on common words
    if len(words) <= 1:
        # Common word patterns in company names
        word_patterns = [
            'project', 'management', 'institute', 'international', 'association',
            'american', 'national', 'health', 'medical', 'software', 'technology',
            'solutions', 'services', 'group', 'corporation', 'company', 'systems'
        ]
        temp_base = base.lower()
        words = []
        for pattern in word_patterns:
            if pattern in temp_base:
                words.append(pattern)

    if len(words) < 2:
        return []

    # Generate acronym from first letters
    acronym = ''.join(w[0].lower() for w in words if w)

    if len(acronym) < 2 or len(acronym) > 6:
        return []

    # Return possible domain variations
    return [
        f"{acronym}.org",
        f"{acronym}.com",
        f"{acronym}.io",
        f"{acronym}.net",
    ]


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

    def _resolve_domain_with_browser(self, domain: str) -> tuple[str, str] | None:
        """Use browser to detect redirects (handles JS/meta redirects)."""
        if not PLAYWRIGHT_AVAILABLE:
            return None

        with self._browser_context() as context:
            if not context:
                return None
            try:
                page = context.new_page()
                page.set_default_timeout(10000)  # Optimized: Reduced from 15000
                page.goto(f"https://{domain}", wait_until="domcontentloaded")
                page.wait_for_timeout(1500)  # Optimized: Reduced from 2000

                final_url = page.url
                final_host = urlparse(final_url).netloc
                if final_host.startswith("www."):
                    final_host = final_host[4:]

                if final_host != domain:
                    self._log(f"Browser detected redirect: {domain} -> {final_host}")
                    return (final_host, final_url)
                return (domain, f"https://{domain}")
            except Exception as e:
                self._log(f"Browser redirect check failed: {e}", "warning")
                return None

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
                        page.wait_for_timeout(150)
                except: pass
        except: pass

    def _find_contact_links_http(self, html: str, base_url: str) -> list[str]:
        """Fast HTTP-based link discovery."""
        try:
            soup = BeautifulSoup(html, "lxml")
        except:
            soup = BeautifulSoup(html, "html.parser")

        links = []
        keywords = ["contact", "about", "team", "advertise", "partner", "press",
                    "media", "news", "newsroom", "company", "sponsor",
                    # Additional corporate keywords
                    "investor", "corporate", "who-we-are", "leadership", "reach"]

        for a in soup.find_all("a", href=True):
            href = a.get('href', '')
            if not isinstance(href, str):
                continue
            text = (a.get_text() or '').lower()
            if any(k in text or k in href.lower() for k in keywords):
                full = urljoin(base_url, href)
                if urlparse(full).netloc == urlparse(base_url).netloc:
                    links.append(full)

        return list(set(links))

    def _find_links_with_browser(self, base_url: str, domain: str) -> list[str]:
        """SAFE Link Discovery using page.evaluate()."""
        if not PLAYWRIGHT_AVAILABLE: return []

        contact_urls = []
        keywords = ["contact", "about", "team", "advertise", "partner", "press",
                    "media", "news", "newsroom", "brand", "sales", "sponsorship",
                    # Additional corporate keywords
                    "investor", "corporate", "who-we-are", "leadership", "reach"]

        with self._browser_context() as context:
            if not context: return []
            try:
                page = context.new_page()
                page.set_default_timeout(8000)  # Optimized: Reduced from 12000
                page.goto(base_url, wait_until="domcontentloaded")

                raw_links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        href: a.href || '',
                        text: (a.innerText || '').toLowerCase()
                    }));
                }""")

                for link in raw_links:
                    href = link.get('href', '')
                    text = link.get('text', '')
                    # FIX: Ensure href and text are strings
                    if not isinstance(href, str) or not isinstance(text, str):
                        continue
                    if any(k in text or k in href.lower() for k in keywords):
                        if urlparse(href).netloc == urlparse(base_url).netloc:
                            contact_urls.append(href)

            except Exception as e:
                self._log(f"Link discovery error: {e}", "warning")

        return list(set(contact_urls))[:15]

    def _is_valid_deep_drill_url(self, url: str, base_domain: str) -> bool:
        """Check if URL is a valid deep drill target (not a query param mess)."""
        parsed = urlparse(url)
        # Reject URLs with query params that look like tracking
        if parsed.query and any(x in parsed.query for x in ['from=', 'utm_', 'ref=', 'source=']):
            return False
        # Must be same domain
        if parsed.netloc != base_domain and parsed.netloc != f"www.{base_domain}":
            return False
        # Path must look like a real page
        path = parsed.path.lower()
        good_paths = [
            '/press', '/newsroom', '/media', '/news', '/about', '/contact',
            '/advertise', '/advertising', '/sponsor', '/partner', '/team',
            '/leadership', '/company', '/inquiries'
        ]
        return any(p in path for p in good_paths)

    def _scrape_with_browser(self, domain: str, urls: list[str], original_domain: str = None) -> tuple[list[WebsiteContact], int]:
        """Deep Drilling Browser Scraper. Returns (contacts, blocked_count)."""
        if not PLAYWRIGHT_AVAILABLE: return [], 0

        browser_start = time.time()
        max_browser_time = 60  # Optimized: Reduced from 90
        all_emails = {}
        visited_deep_links = set()
        blocked_count = 0

        with self._browser_context() as context:
            if not context: return [], 0

            page = context.new_page()
            page.set_default_timeout(10000)  # Optimized: Reduced from 15000

            def handle_route(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    route.abort()
                else:
                    route.continue_()
            try: page.route("**/*", handle_route)
            except: pass

            queue = list(urls[:6])  # Optimized: Reduced from 8
            processed = 0

            while queue and processed < 8:  # Optimized: Reduced from 12
                if (time.time() - browser_start) > max_browser_time:
                    self._log("Browser time limit reached", "warning")
                    break

                if self._is_cancelled(): break

                # If too many blocks, give up
                if blocked_count >= 5:
                    self._log("Too many blocks, stopping browser scan", "warning")
                    break

                url = queue.pop(0)
                if url in visited_deep_links:
                    continue
                visited_deep_links.add(url)

                try:
                    self._log(f"Browser visiting: {url}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000)  # Optimized: Reduced from 1500

                    self._click_reveal_email_buttons(page)

                    content = page.content()

                    # Check for Cloudflare/bot block
                    if "cf-browser-verification" in content or "just a moment" in content.lower():
                        self._log(f"Block detected on {url}", "warning")
                        blocked_count += 1
                        continue

                    # Extract emails
                    found = self._extract_contacts_from_html(content, url, domain, original_domain)
                    for c in found:
                        all_emails[c.email] = c

                    # Only early exit if we have 3+ advertising emails (collect more contacts)
                    ad_count = sum(1 for c in all_emails.values() if c.email_type == "advertising")
                    if ad_count >= 3:
                        self._log(f"Found {ad_count} advertising contacts, stopping")
                        break

                    # DEEP DRILL: Look for press/newsroom links
                    has_good_email = any(c.email_type == "advertising" for c in all_emails.values())
                    if not has_good_email and processed < 8:
                        try:
                            soup = BeautifulSoup(content, "lxml")
                        except:
                            soup = BeautifulSoup(content, "html.parser")

                        base_domain = urlparse(url).netloc
                        if base_domain.startswith("www."):
                            base_domain = base_domain[4:]

                        for a in soup.find_all("a", href=True):
                            txt = (a.get_text() or '').lower()
                            href = a.get('href', '')
                            if not isinstance(href, str):
                                continue

                            # Expanded deep drill keywords
                            deep_drill_keywords = [
                                "press", "newsroom", "media kit", "media-kit", "news room",
                                "advertise", "advertising", "sponsor", "sponsorship",
                                "partner with us", "media inquiries", "contact us"
                            ]
                            if any(k in txt for k in deep_drill_keywords):
                                full = urljoin(url, href)
                                if self._is_valid_deep_drill_url(full, base_domain) and full not in visited_deep_links:
                                    self._log(f"  Deep Drill target found: {full}")
                                    queue.insert(0, full)
                                    break

                    processed += 1

                except Exception as e:
                    self._log(f"Browser error on {url}: {str(e)[:60]}", "warning")
                    continue

        return list(all_emails.values()), blocked_count

    def _try_vision_fallback(self, base_url: str, domain: str) -> list[WebsiteContact]:
        """Use Claude Vision to find contacts - scrolls to reveal footer emails."""
        contacts = []
        if not self.use_claude: return []

        with self._browser_context() as context:
            if not context: return []
            try:
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)  # Optimized: Reduced from 2500

                # Scroll to bottom
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)  # Optimized: Reduced from 800

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
        """Main scraping method with improved redirect handling."""
        domain_start_time = time.time()
        max_domain_time = 100  # Optimized: Reduced from 150

        # Clean domain
        if domain.startswith("www."): domain = domain[4:]
        domain = strip_marketing_subdomain(domain)
        original_domain = domain  # Track for redirect matching (e.g., projectmanagementinstitute.com)
        final_domain = domain
        base_url = f"https://{domain}"

        client = self._get_http_client()
        resp = None
        http_failed = False

        # Phase 0: Resolve domain (handle redirects and TLD fallback)
        try:
            resp = client.get(base_url, timeout=5.0)  # Optimized: Reduced from 8
            final_host = urlparse(str(resp.url)).netloc
            if final_host.startswith("www."): final_host = final_host[4:]

            if final_host != domain:
                self._log(f"Redirect detected: {domain} -> {final_host}")
                final_domain = final_host
                base_url = str(resp.url)

        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            http_failed = True
            self._log(f"HTTP failed for {domain}: {type(e).__name__}")

            # Try with www. prefix first (some domains only work with www)
            try:
                self._log(f"Trying www.{domain}...")
                resp = client.get(f"https://www.{domain}", timeout=4.0)
                if resp.status_code == 200:
                    final_host = urlparse(str(resp.url)).netloc
                    if final_host.startswith("www."): final_host = final_host[4:]
                    self._log(f"Success with www prefix -> {final_host}")
                    final_domain = final_host
                    base_url = str(resp.url)
                    http_failed = False
            except:
                pass

            # If www didn't work, try web search first (fastest way to find real domain)
            if http_failed:
                # Generate search name from domain or use company_name
                search_name = company_name
                if not search_name:
                    # Convert domain to readable name: projectmanagementinstitute -> Project Management Institute
                    base = domain.rsplit('.', 1)[0]
                    # Try to split on common word patterns
                    import re as re_mod
                    words = re_mod.findall(r'[A-Z][a-z]+|[a-z]+', base)
                    if words:
                        search_name = ' '.join(w.capitalize() for w in words)
                    else:
                        search_name = base

                if search_name:
                    self._log(f"Searching for '{search_name}' domain...")
                    # Get API key for AI verification if Claude is enabled
                    api_key = None
                    if self.use_claude:
                        agent = self._get_claude_agent()
                        if agent and hasattr(agent, 'api_key'):
                            api_key = agent.api_key
                    search_domain = _search_for_domain(search_name, api_key=api_key)
                    if search_domain and search_domain != domain:
                        try:
                            self._log(f"  AI-verified domain: {search_domain}, testing...")
                            resp = client.get(f"https://{search_domain}", timeout=4.0)
                            if resp.status_code == 200:
                                self._log(f"  Success! Using {search_domain}")
                                final_domain = search_domain
                                base_url = f"https://{search_domain}"
                                http_failed = False
                        except:
                            pass

            # If search didn't work, try browser-based redirect detection
            if http_failed and self.use_browser:
                self._log(f"Trying browser redirect detection for {domain}...")
                browser_result = self._resolve_domain_with_browser(domain)
                if browser_result:
                    final_domain, base_url = browser_result
                    http_failed = False
                    # Try HTTP again with resolved domain
                    try:
                        resp = client.get(base_url, timeout=5.0)
                    except:
                        pass

            # If browser didn't help, try acronym domains (e.g., pmi.org for projectmanagementinstitute.com)
            if http_failed:
                acronym_domains = _generate_acronym_domains(domain)
                if acronym_domains:
                    self._log(f"Trying acronym-based domains...")
                    for alt in acronym_domains:
                        try:
                            self._log(f"  Trying {alt}...")
                            resp = client.get(f"https://{alt}", timeout=3.0)
                            if resp.status_code == 200:
                                self._log(f"  Success! Using {alt}")
                                final_domain = alt
                                base_url = f"https://{alt}"
                                http_failed = False
                                break
                        except: continue

            # If acronym didn't help, try standard TLD fallback
            if http_failed:
                self._log(f"Trying TLD alternatives...")
                alternatives = [domain.rsplit('.', 1)[0] + ext for ext in ['.org', '.co', '.io', '.net']]

                for alt in alternatives:
                    try:
                        self._log(f"  Trying {alt}...")
                        resp = client.get(f"https://{alt}", timeout=3.0)
                        if resp.status_code == 200:
                            self._log(f"  Success! Using {alt}")
                            final_domain = alt
                            base_url = f"https://{alt}"
                            http_failed = False
                            break
                    except: continue

            if http_failed:
                return WebsiteScrapeResult(domain=domain, errors=["Domain unreachable"])

        result = WebsiteScrapeResult(domain=final_domain)
        all_emails = {}
        visited_urls = set()

        # Phase 1: Fast HTTP Scan
        try:
            if resp and resp.status_code == 200:
                for c in self._extract_contacts_from_html(resp.text, base_url, final_domain, original_domain):
                    all_emails[c.email] = c

                contact_urls = self._find_contact_links_http(resp.text, base_url)

                # Expanded contact page paths (including corporate/investor pages for large companies)
                for p in ["/contact", "/about", "/advertise", "/media-kit", "/press",
                          "/partners", "/company", "/newsroom", "/about-us", "/contact-us",
                          "/team", "/leadership", "/our-team", "/get-in-touch", "/reach-us",
                          "/inquiries", "/media-inquiries", "/advertising", "/sponsor",
                          # Corporate/investor pages (for large companies like HSBC, Lilly)
                          "/investor-relations", "/investors", "/corporate", "/media",
                          "/media/contacts", "/news/media-contacts", "/press-releases",
                          "/who-we-are", "/about/contact", "/corporate/contact"]:
                    contact_urls.append(urljoin(base_url, p))

                contact_urls = list(set(contact_urls))

                # Fast-path: check if homepage has 2+ advertising emails (we have enough)
                ad_email_count = sum(1 for c in all_emails.values() if c.email_type == "advertising")
                if ad_email_count >= 2:
                    self._log(f"Fast-path: Found {ad_email_count} advertising emails on homepage")
                else:
                    for url in contact_urls[:10]:  # Increased back to 10 for better coverage
                        if self._is_cancelled(): break
                        if url in visited_urls: continue
                        visited_urls.add(url)

                        try:
                            r = client.get(url, timeout=4.0)
                            if r.status_code == 200:
                                for c in self._extract_contacts_from_html(r.text, url, final_domain, original_domain):
                                    all_emails[c.email] = c
                                # Only exit early if we have 3+ advertising emails
                                ad_count = sum(1 for c in all_emails.values() if c.email_type == "advertising")
                                if ad_count >= 3:
                                    self._log(f"Fast-path: Found {ad_count} advertising emails in HTTP scan")
                                    break
                        except: continue
        except Exception as e:
            self._log(f"HTTP scan error: {e}", "warning")

        # Phase 2: Browser Scan (if needed)
        has_good_email = any(c.email_type in ["advertising", "marketing"] for c in all_emails.values())
        blocked_count = 0

        if (len(all_emails) < 2 or not has_good_email) and self.use_browser:
            self._log(f"Deep scraping {final_domain} with browser...")

            browser_urls = [base_url]
            # Expanded browser paths (including corporate pages for large companies)
            for p in ["/contact", "/about", "/press", "/advertise", "/media-kit",
                      "/partners", "/newsroom", "/company", "/news", "/team",
                      "/leadership", "/advertising", "/sponsor", "/media-inquiries",
                      # Corporate/investor pages
                      "/investor-relations", "/investors", "/corporate", "/media",
                      "/media/contacts", "/press-releases", "/who-we-are"]:
                browser_urls.append(urljoin(base_url, p))

            real_links = self._find_links_with_browser(base_url, final_domain)
            browser_urls.extend(real_links)
            browser_urls = list(set(browser_urls))

            browser_contacts, blocked_count = self._scrape_with_browser(final_domain, browser_urls, original_domain)
            for c in browser_contacts:
                all_emails[c.email] = c

            # Phase 3: Vision Fallback (skip if site is completely blocking us)
            has_good_email = any(c.email_type in ["advertising", "marketing"] for c in all_emails.values())
            if not has_good_email and blocked_count < 5:
                self._log("No advertising contacts found. Trying Vision AI...")
                vision_contacts = self._try_vision_fallback(base_url, final_domain)
                for c in vision_contacts:
                    all_emails[c.email] = c

        # Phase 4: SMTP Verification (more permissive for high-confidence emails)
        contacts_list = list(all_emails.values())
        self._log(f"Found {len(contacts_list)} raw emails before verification")

        if self.verify_emails:
            from .email_finder import verify_email_smtp_permissive
            verified = []
            rejected = []
            for c in contacts_list:
                status = verify_email_smtp_permissive(c.email)

                # Keep email if:
                # 1. SMTP says valid/unknown/catchall (not "invalid")
                # 2. OR it's a high-confidence advertising email (many mail servers block SMTP verification)
                is_high_confidence = c.email_type == "advertising"

                if status != "invalid":
                    verified.append(c)
                elif is_high_confidence:
                    # Keep advertising emails even if SMTP verification failed
                    # (many corporate mail servers block verification)
                    self._log(f"  Keeping {c.email} despite SMTP={status} (advertising email)")
                    verified.append(c)
                else:
                    rejected.append(c.email)

            if rejected:
                self._log(f"  SMTP rejected {len(rejected)} emails: {rejected[:5]}...")

            result.contacts = verified
        else:
            result.contacts = contacts_list

        result.contacts = list({c.email: c for c in result.contacts}.values())
        self._log(f"Finished {final_domain}: Found {len(result.contacts)} verified contacts")
        return result

    def _extract_contacts_from_html(self, html: str, source_url: str, domain: str, original_domain: str = None) -> list[WebsiteContact]:
        """Extract emails with relaxed domain matching and multiple obfuscation patterns.

        v5 improvement: More permissive extraction - if an email is found on the target site,
        it's likely relevant. We filter later during verification.
        """
        contacts = []
        priority_emails = set()  # Track emails found via labeled patterns (higher confidence)

        # Standard email pattern
        emails = set(EMAIL_PATTERN.findall(html))

        # mailto: links (high priority - explicitly linked emails)
        for match in MAILTO_PATTERN.findall(html):
            emails.add(match)
            priority_emails.add(match.lower())  # Mark as high priority

        # Structured contact patterns (e.g., "Media Contact: email@domain.com")
        for match in CONTACT_LABEL_PATTERN.findall(html):
            emails.add(match)
            priority_emails.add(match.lower())  # Mark as high priority

        # Multiple obfuscation patterns
        for pattern in EMAIL_OBFUSCATED_PATTERNS:
            for match in pattern.findall(html):
                if len(match) == 2:
                    emails.add(f"{match[0]}@{match[1]}")
                elif len(match) == 3:
                    # "at" with spaces pattern: local, domain, tld
                    emails.add(f"{match[0]}@{match[1]}.{match[2]}")

        # Decode HTML entities and look for more emails
        html_decoded = html.replace('&#64;', '@').replace('&#46;', '.').replace('&commat;', '@')
        emails.update(EMAIL_PATTERN.findall(html_decoded))

        domain_parts = domain.replace('www.', '').split('.')
        base_name = domain_parts[0] if domain_parts else domain

        # Also consider original domain for redirect cases (e.g., projectmanagementinstitute.com -> pmi.org)
        original_base = None
        if original_domain and original_domain != domain:
            original_parts = original_domain.replace('www.', '').split('.')
            original_base = original_parts[0] if original_parts else None

        # Free webmail providers to always skip
        free_webmail = ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com',
                        'icloud.com', 'protonmail.com', 'mail.com', 'zoho.com']

        for email in emails:
            email = email.lower().strip()

            if any(s in email for s in SKIP_PATTERNS):
                continue

            if any(email.endswith(ext) for ext in ['.png', '.jpg', '.gif', '.svg', '.webp']):
                continue

            prefix = email.split('@')[0]
            email_domain = email.split('@')[1] if '@' in email else ''
            email_base = email_domain.split('.')[0] if email_domain else ''

            # Skip free webmail
            if any(email.endswith(f"@{provider}") for provider in free_webmail):
                continue

            is_priority = any(p in prefix for p in PRIORITY_PREFIXES)
            is_labeled_contact = email in priority_emails  # Found via "Media Contact:" or mailto:

            # Relaxed domain matching - also check original domain for redirects
            is_domain_match = (
                email.endswith(domain) or
                domain in email_domain or
                base_name in email_base or
                email_base in base_name or
                (len(base_name) > 3 and base_name in email) or
                # Original domain matching for redirects
                (original_base and (original_base in email_base or email_base in original_base)) or
                (original_domain and email.endswith(original_domain))
            )

            # v5: More permissive - accept ANY corporate email found on the target site
            # These are likely relevant contacts, even if domain doesn't exactly match
            # (e.g., subsidiary domains, partner emails listed on contact pages)
            is_corporate_email = (
                email_domain and
                '.' in email_domain and
                not any(email.endswith(f"@{provider}") for provider in free_webmail)
            )

            if is_domain_match or is_priority or is_labeled_contact:
                # Mark as advertising if priority prefix OR labeled contact (e.g., "Media Contact:")
                etype = "advertising" if (is_priority or is_labeled_contact) else "generic"
                contacts.append(WebsiteContact(email=email, source_page=source_url, email_type=etype))
            elif is_corporate_email:
                # v5: Keep corporate emails found on the site, mark as discovered
                # These may be partner/subsidiary contacts that are still valuable
                contacts.append(WebsiteContact(email=email, source_page=source_url, email_type="discovered"))

        return contacts


# --- REQUIRED EXPORTS ---

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
                        "text": """Extract ALL email addresses visible in this screenshot. Look carefully for:
1. Email addresses in the footer
2. "Contact us" or "Media Contact" sections
3. Emails next to labels like "Press:", "Media:", "Advertising:", "Sales:"
4. Partially visible or small text emails
5. Emails that may be formatted with spaces or obfuscation like "email [at] domain.com"

Return as JSON array: [{"email": "..."}]. If no emails found, return []."""
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
