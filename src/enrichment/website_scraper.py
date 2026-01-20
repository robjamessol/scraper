"""Website contact scraper - extracts contact info directly from company websites."""

import re
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

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
    "advertising", "ads", "partnerships", "partner", "sponsors", "sponsorship",
    "marketing", "media", "sales", "business", "bd", "biz",
    "hello", "contact", "info", "press", "pr",
]

# Pages likely to contain contact information
CONTACT_PAGE_PATTERNS = [
    "/contact", "/about", "/team", "/leadership", "/people",
    "/advertise", "/advertising", "/partnerships", "/sponsors",
    "/press", "/media", "/about-us", "/our-team", "/company",
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
    """Scrapes company websites to find contact information."""

    def __init__(
        self,
        timeout: float = 10.0,
        max_pages: int = 5,
        log_callback: callable = None,
    ):
        """
        Initialize the website scraper.

        Args:
            timeout: Request timeout in seconds
            max_pages: Maximum pages to scrape per domain
            log_callback: Optional callback for live logging
        """
        self.timeout = timeout
        self.max_pages = max_pages
        self._log_callback = log_callback

        # Common headers to avoid being blocked
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

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

    def scrape_domain(self, domain: str) -> WebsiteScrapeResult:
        """
        Scrape a domain for contact information.

        Args:
            domain: Domain to scrape (e.g., "healthedge.com")

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

        # Add common contact page URLs
        for pattern in CONTACT_PAGE_PATTERNS:
            urls_to_visit.append(urljoin(base_url, pattern))

        # Also try common variations
        urls_to_visit.append(urljoin(base_url, "/contact-us"))
        urls_to_visit.append(urljoin(base_url, "/get-in-touch"))

        # Deduplicate initial URLs
        urls_to_visit = list(dict.fromkeys(urls_to_visit))

        pages_scraped = 0
        all_emails: dict[str, WebsiteContact] = {}  # email -> contact

        with httpx.Client(
            timeout=self.timeout,
            headers=self.headers,
            follow_redirects=True,
        ) as client:
            for url in urls_to_visit:
                if pages_scraped >= self.max_pages:
                    break

                if url in visited_urls:
                    continue

                visited_urls.add(url)

                try:
                    response = client.get(url)
                    if response.status_code != 200:
                        continue

                    pages_scraped += 1
                    html = response.text

                    # Extract contacts from this page
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

                    # Look for additional contact page links on homepage
                    if url == base_url:
                        new_urls = self._find_contact_links(html, base_url)
                        for new_url in new_urls:
                            if new_url not in visited_urls and new_url not in urls_to_visit:
                                urls_to_visit.append(new_url)

                except httpx.TimeoutException:
                    self._log(f"Timeout fetching {url}", "warning")
                except Exception as e:
                    result.errors.append(f"Error fetching {url}: {str(e)}")

        result.pages_scraped = pages_scraped
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

        # Also check mailto: links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:"):
                email = href[7:].split("?")[0].strip()
                if EMAIL_PATTERN.match(email):
                    emails.add(email)

        # Filter out emails from the same domain (internal emails only)
        # and skip obvious non-contact emails
        skip_patterns = ["noreply", "no-reply", "donotreply", "unsubscribe", "privacy", "legal", "support@", "help@"]

        for email in emails:
            email_lower = email.lower()

            # Skip emails that match skip patterns
            if any(skip in email_lower for skip in skip_patterns):
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

        contact_keywords = [
            "contact", "about", "team", "leadership", "advertise",
            "partnerships", "press", "media", "people", "company",
        ]

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text().lower().strip()

            # Check if link text or href contains contact keywords
            href_lower = href.lower()
            if any(kw in text or kw in href_lower for kw in contact_keywords):
                full_url = urljoin(base_url, href)
                # Only include same-domain links
                if urlparse(full_url).netloc == urlparse(base_url).netloc:
                    contact_urls.append(full_url)

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
    max_pages: int = 5,
    log_callback: callable = None,
) -> list[WebsiteContact]:
    """
    Convenience function to scrape a domain for contacts.

    Args:
        domain: Domain to scrape
        max_pages: Maximum pages to check
        log_callback: Optional logging callback

    Returns:
        List of WebsiteContact objects, prioritized for outreach
    """
    scraper = WebsiteScraper(max_pages=max_pages, log_callback=log_callback)
    result = scraper.scrape_domain(domain)
    return result.contacts
