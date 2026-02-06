"""Helper utilities for text processing and data extraction."""

import re
import logging
import atexit
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Global HTTP client with connection pooling (reused across all calls)
# This dramatically improves performance at scale (500+ items)
_HTTP_CLIENT: httpx.Client | None = None


def get_http_client() -> httpx.Client:
    """Get or create the global HTTP client with connection pooling."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.Client(
            timeout=2.5,  # Reduced from 3.0 for faster failures
            follow_redirects=True,
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),  # Increased for more parallelism
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        # Register cleanup on exit
        atexit.register(_cleanup_http_client)
    return _HTTP_CLIENT


def _cleanup_http_client():
    """Clean up the global HTTP client."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        _HTTP_CLIENT.close()
        _HTTP_CLIENT = None


# Known tracking/redirect domains that should be resolved
TRACKING_DOMAINS = {
    # Morning Brew / Healthcare Brew
    "links.morningbrew.com",
    "link.morningbrew.com",
    "links.healthcare-brew.com",
    "link.healthcare-brew.com",
    "email.morningbrew.com",
    # Link shorteners
    "t.co",
    "bit.ly",
    "tinyurl.com",
    "ow.ly",
    "buff.ly",
    "goo.gl",
    "is.gd",
    "v.gd",
    "rebrand.ly",
    "short.io",
    "cutt.ly",
    # Link/affiliate services (NOT the actual company)
    "go.linkby.com",
    "linkby.com",
    "linktr.ee",
    "linktree.com",
    "linkin.bio",
    "taplink.cc",
    "stan.store",
    "beacons.ai",
    "hoo.be",
    "snipfeed.co",
    "plink.com",
    "about.me",
    "carrd.co",
    "bento.me",
    "bio.link",
    "lnk.bio",
    # Email tracking
    "mailtrack.io",
    "click.convertkit-mail.com",
    "click.convertkit-mail2.com",
    "mailchimp.com",
    "list-manage.com",
    "hubspotlinks.com",
    "track.customer.io",
    "email.mg.substack.com",
    # Affiliate/referral tracking
    "shrsl.com",  # ShareASale
    "jdoqocy.com",  # CJ Affiliate
    "tkqlhce.com",  # CJ Affiliate
    "anrdoezrs.com",  # CJ Affiliate
    "pntrs.com",  # Impact
    "pntra.com",  # Impact
    "prf.hn",  # Impact
    "sjv.io",  # Skimlinks
}

# Marketing/tracking subdomains to strip to get root company domain
MARKETING_SUBDOMAINS = {
    # Common marketing landing pages
    "get", "go", "info", "promo", "try", "start", "join", "buy", "shop",
    "links", "link", "click", "track", "t", "l", "r", "email", "mail",
    "news", "newsletter", "offers", "deals", "landing", "lp", "pages",
    "invite", "signup", "register", "app", "web", "m", "mobile",
    "partners", "partner", "affiliate", "ref", "campaign", "ads", "ad",
    "learn", "discover", "explore", "hello", "hi", "meet", "connect",
    "invest", "demo", "trial", "free", "www2", "secure", "my", "account",
    # Technical subdomains (not the main company site)
    "api", "cdn", "static", "assets", "img", "images", "media",
    "dev", "staging", "test", "sandbox", "beta", "preview",
    "docs", "doc", "documentation", "help", "support", "faq",
    "blog", "community", "forum", "status", "mail", "smtp",
    # Regional/localized
    "us", "uk", "eu", "au", "ca", "de", "fr", "es", "it", "jp",
}


def resolve_redirect_url(url: str, timeout: float = 3.0) -> str | None:
    """
    Follow redirects to get the final destination URL.

    This is critical for newsletter tracking links like:
    links.morningbrew.com/c/xxx → actual-advertiser.com

    Uses a global connection pool for efficiency at scale.
    Falls back from HEAD to GET if HEAD returns 405 (Method Not Allowed).

    Args:
        url: URL that may redirect
        timeout: Request timeout in seconds (default 1.5s for fast failures)

    Returns:
        Final destination URL, or original URL if no redirects
    """
    if not url:
        return None

    try:
        client = get_http_client()

        # Try HEAD first (faster, no body download)
        try:
            response = client.head(url, timeout=timeout)
            # If HEAD returns 405, fall back to GET
            if response.status_code == 405:
                raise httpx.HTTPStatusError("HEAD not allowed", request=response.request, response=response)
            final_url = str(response.url)
        except (httpx.HTTPStatusError, httpx.RequestError):
            # Fall back to GET with stream=True to avoid downloading body
            response = client.get(url, timeout=timeout)
            final_url = str(response.url)

        # If we got redirected somewhere useful, return it
        if final_url and final_url != url:
            logger.debug(f"Resolved redirect: {url[:50]}... → {final_url[:50]}...")
            return final_url

        return url

    except httpx.TimeoutException:
        logger.debug(f"Timeout resolving redirect for {url[:50]}...")
        return None
    except Exception as e:
        logger.debug(f"Error resolving redirect for {url[:50]}...: {e}")
        return None


def resolve_redirect_with_browser(url: str, timeout: float = 8.0) -> str | None:
    """
    Use Playwright browser to resolve JavaScript-based tracking redirects.

    Some tracking links (like linkby.com) use JavaScript to redirect,
    which won't work with simple HTTP requests.

    Args:
        url: URL to resolve
        timeout: Browser timeout in seconds

    Returns:
        Final URL after JS redirects, or None on failure
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(int(timeout * 1000))

            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)  # Wait for JS redirects
                final_url = page.url

                if final_url and final_url != url:
                    logger.debug(f"Browser resolved: {url[:40]}... → {final_url[:40]}...")
                    return final_url
            except Exception as e:
                logger.debug(f"Browser redirect failed: {e}")
            finally:
                browser.close()

    except Exception as e:
        logger.debug(f"Browser not available for redirect: {e}")

    return None


def is_tracking_domain(url: str) -> bool:
    """
    Check if URL is from a known tracking/redirect domain.

    Args:
        url: URL to check (or just a domain)

    Returns:
        True if this is a tracking domain that should be resolved
    """
    # Don't strip marketing subdomains - we need to match links.morningbrew.com etc.
    domain = extract_domain(url, strip_marketing=False)
    if not domain:
        return False
    return domain.lower() in TRACKING_DOMAINS


# Common multi-part TLDs that should be preserved
MULTI_PART_TLDS = {
    "co.uk", "com.au", "co.nz", "co.za", "com.br", "co.jp", "co.kr",
    "com.mx", "co.in", "com.sg", "com.hk", "co.il", "com.ar", "com.tw",
    "org.uk", "net.au", "gov.uk", "ac.uk", "edu.au",
}


def strip_marketing_subdomain(domain: str) -> str:
    """
    Strip common marketing/tracking subdomains to get the root company domain.

    Handles:
    - Simple subdomains: get.expertvoice.com → expertvoice.com
    - Multiple subdomains: get.try.example.com → example.com
    - Multi-part TLDs: get.example.co.uk → example.co.uk
    - Preserves valid domains: healthedge.com → healthedge.com

    Examples:
        >>> strip_marketing_subdomain("get.expertvoice.com")
        'expertvoice.com'
        >>> strip_marketing_subdomain("invest.xtremeone.com")
        'xtremeone.com'
        >>> strip_marketing_subdomain("get.example.co.uk")
        'example.co.uk'
        >>> strip_marketing_subdomain("healthedge.com")
        'healthedge.com'

    Args:
        domain: Domain that may have marketing subdomain

    Returns:
        Domain with marketing subdomain stripped
    """
    if not domain:
        return domain

    domain = domain.lower().strip()
    parts = domain.split(".")

    if len(parts) <= 2:
        return domain

    # Check for multi-part TLD
    potential_tld = ".".join(parts[-2:])
    has_multi_part_tld = potential_tld in MULTI_PART_TLDS

    # Calculate minimum parts needed for valid domain
    # example.com = 2 parts, example.co.uk = 3 parts
    min_parts = 3 if has_multi_part_tld else 2

    # Strip marketing subdomains from the front
    while len(parts) > min_parts:
        if parts[0] in MARKETING_SUBDOMAINS:
            parts = parts[1:]
        else:
            break

    return ".".join(parts)


def is_valid_domain(domain: str) -> bool:
    """
    Check if a domain appears to be valid.

    Args:
        domain: Domain to validate

    Returns:
        True if domain appears valid
    """
    if not domain:
        return False

    # Basic checks
    if len(domain) < 4:  # Minimum: a.co
        return False
    if ".." in domain:
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False

    parts = domain.split(".")
    if len(parts) < 2:
        return False

    # Check TLD is reasonable (2-10 chars)
    tld = parts[-1]
    if len(tld) < 2 or len(tld) > 10:
        return False

    # Check each part is alphanumeric with hyphens
    for part in parts:
        if not part:
            return False
        if not all(c.isalnum() or c == "-" for c in part):
            return False
        if part.startswith("-") or part.endswith("-"):
            return False

    return True


def extract_domain(url: str, strip_marketing: bool = True) -> str | None:
    """
    Extract the base domain from a URL.

    Args:
        url: Full URL
        strip_marketing: If True, also strips marketing subdomains like get.*, go.*, etc.

    Returns:
        Domain without www prefix, or None if invalid

    Examples:
        >>> extract_domain("https://www.healthedge.com/landing?utm=123")
        'healthedge.com'
        >>> extract_domain("https://get.expertvoice.com/promo")
        'expertvoice.com'
        >>> extract_domain("http://example.org/page")
        'example.org'
    """
    if not url:
        return None

    try:
        # Add scheme if missing
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]

        # Remove port if present
        if ":" in domain:
            domain = domain.split(":")[0]

        if not domain:
            return None

        # Strip marketing subdomains
        if strip_marketing:
            domain = strip_marketing_subdomain(domain)

        return domain

    except Exception:
        return None


def clean_text(text: str) -> str:
    """
    Clean and normalize text content.

    Args:
        text: Raw text

    Returns:
        Cleaned text with normalized whitespace
    """
    if not text:
        return ""

    # Replace multiple whitespace with single space
    text = re.sub(r"\s+", " ", text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def normalize_company_name(name: str) -> str:
    """
    Normalize a company name for comparison and deduplication.

    Args:
        name: Raw company name

    Returns:
        Normalized name (lowercase, stripped, common suffixes removed)

    Examples:
        >>> normalize_company_name("HealthEdge, Inc.")
        'healthedge'
        >>> normalize_company_name("The Wellness Company LLC")
        'wellness company'
    """
    if not name:
        return ""

    name = name.lower().strip()

    # Remove common suffixes
    suffixes = [
        r",?\s*inc\.?$",
        r",?\s*llc\.?$",
        r",?\s*ltd\.?$",
        r",?\s*corp\.?$",
        r",?\s*corporation$",
        r",?\s*co\.?$",
        r",?\s*company$",
        r"^the\s+",
    ]

    for suffix in suffixes:
        name = re.sub(suffix, "", name, flags=re.IGNORECASE)

    # Remove extra whitespace
    name = re.sub(r"\s+", " ", name).strip()

    return name


def truncate_text(text: str, max_length: int = 150, suffix: str = "...") -> str:
    """
    Truncate text to a maximum length, adding suffix if truncated.

    Args:
        text: Text to truncate
        max_length: Maximum length including suffix
        suffix: String to append if truncated

    Returns:
        Truncated text
    """
    if not text or len(text) <= max_length:
        return text or ""

    return text[: max_length - len(suffix)] + suffix


def extract_links_from_html(html: str, base_url: str = "") -> list[dict[str, str]]:
    """
    Extract all links from HTML content.

    Args:
        html: HTML content
        base_url: Base URL for resolving relative links

    Returns:
        List of dicts with 'href', 'text', and 'domain' keys
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Resolve relative URLs
        if base_url and not href.startswith(("http://", "https://")):
            href = urljoin(base_url, href)

        text = clean_text(a.get_text())
        domain = extract_domain(href)

        links.append({
            "href": href,
            "text": text,
            "domain": domain,
        })

    return links


def is_tracking_link(url: str) -> bool:
    """
    Check if a URL appears to be a tracking/affiliate link.

    Args:
        url: URL to check

    Returns:
        True if URL contains tracking parameters
    """
    tracking_indicators = [
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "ref=",
        "affiliate",
        "partner",
        "click",
        "track",
        "redirect",
    ]

    url_lower = url.lower()
    return any(indicator in url_lower for indicator in tracking_indicators)


def parse_date_from_url(url: str) -> str | None:
    """
    Try to extract a date from a URL slug.

    Args:
        url: URL that may contain date information

    Returns:
        Date string in YYYY-MM-DD format, or None
    """
    # Common date patterns in URLs
    patterns = [
        r"(\d{4})-(\d{2})-(\d{2})",  # 2024-01-15
        r"(\d{4})(\d{2})(\d{2})",     # 20240115
        r"(\d{2})-(\d{2})-(\d{4})",   # 01-15-2024
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            groups = match.groups()
            if len(groups[0]) == 4:
                return f"{groups[0]}-{groups[1]}-{groups[2]}"
            else:
                return f"{groups[2]}-{groups[0]}-{groups[1]}"

    return None


def get_parent_company_domain(company_name: str) -> str | None:
    """
    Get the parent company domain for subsidiaries/products.

    Some products (like iShares) are owned by parent companies (BlackRock).
    For contact discovery, we need the parent company's domain since that's
    where advertising/media contacts are located.

    Args:
        company_name: Company or product name

    Returns:
        Parent company domain if this is a known subsidiary, None otherwise
    """
    if not company_name:
        return None

    # Product/subsidiary -> Parent company domain mappings
    PARENT_COMPANY_DOMAINS = {
        # BlackRock products
        "ishares": "blackrock.com",
        "ishares by blackrock": "blackrock.com",
        # Add more as discovered
    }

    name_lower = company_name.lower().strip()
    return PARENT_COMPANY_DOMAINS.get(name_lower)


def guess_domain_from_name(company_name: str) -> str | None:
    """
    Guess a company's domain from their name.

    This is a fallback when we can't find a link.
    Common patterns: "HealthEdge" → "healthedge.com"

    Args:
        company_name: Company name

    Returns:
        Guessed domain or None
    """
    if not company_name:
        return None

    # Known domain mappings for companies whose domain doesn't match their name
    # This is critical for contact discovery - wrong domain = no contacts found
    KNOWN_DOMAINS = {
        # Tech/SaaS
        "at&t": "att.com",
        "at&t connected car": "att.com",
        "linkedin talent solutions": "linkedin.com",
        "bamboohr": "bamboohr.com",
        "bamboo hr": "bamboohr.com",
        "onetrust": "onetrust.com",
        "one trust": "onetrust.com",
        "slack": "slack.com",
        "dell": "dell.com",
        "dell technologies": "dell.com",
        "indeed": "indeed.com",
        "miso robotics": "misorobotics.com",

        # Finance/Insurance/Investment
        "fisher investments": "fisherinvestments.com",
        "fisher": "fisherinvestments.com",
        "new york life": "newyorklife.com",
        "northwestern mutual": "northwesternmutual.com",
        "global x etfs": "globalxetfs.com",
        "global x": "globalxetfs.com",
        "pendulum": "pendulumlife.com",
        "ishares": "blackrock.com",  # iShares is a BlackRock product
        "ishares by blackrock": "blackrock.com",
        "blackrock": "blackrock.com",
        "tastytrade": "tastytrade.com",
        "tasty trade": "tastytrade.com",

        # Healthcare/Wellness
        "garden of life": "gardenoflife.com",
        "dr. kellyann": "drkellyann.com",
        "dr kellyann": "drkellyann.com",
        "thermo fisher scientific": "thermofisher.com",
        "ge healthcare": "gehealthcare.com",
        "wolters kluwer health": "wolterskluwer.com",
        "wolters kluwer": "wolterskluwer.com",

        # Education/Institutes
        "project management institute": "pmi.org",
        "the ohio state university": "osu.edu",
        "ohio state university": "osu.edu",
        "the national union of healthcare workers": "nuhw.org",
        "national union of healthcare workers": "nuhw.org",

        # Travel/Hospitality
        "sandals resorts": "sandals.com",
        "sandals": "sandals.com",

        # Media
        "golf digest": "golfdigest.com",
        "cnbc": "cnbc.com",

        # Retail/Ecommerce
        "amazon": "amazon.com",
        "flavcity": "flavcity.com",
        "shop flavcity": "flavcity.com",

        # Government/Economic Development
        "jobsohio": "jobsohio.com",
        "jobs ohio": "jobsohio.com",

        # Other common advertisers
        "elf labs": "elflabs.com",
        "rad": "rad.com",
        "rad security": "rad.com",
        "rad intel": "radintel.ai",
        "radintel": "radintel.ai",
        "layermor": "layermor.com",  # Sometimes comes through linkby.com tracking
    }

    name_lower = company_name.lower().strip()
    if name_lower in KNOWN_DOMAINS:
        return KNOWN_DOMAINS[name_lower]

    # Also check without common prefixes/suffixes
    for prefix in ["the ", "a "]:
        if name_lower.startswith(prefix):
            check_name = name_lower[len(prefix):]
            if check_name in KNOWN_DOMAINS:
                return KNOWN_DOMAINS[check_name]

    # Normalize: lowercase, remove spaces and special chars
    name = company_name.lower().strip()

    # Remove common suffixes
    name = re.sub(r'\s*(inc|llc|ltd|corp|co|company|technologies|labs|health|medical)\.?$', '', name, flags=re.IGNORECASE)

    # Remove spaces and special characters for domain
    domain_name = re.sub(r'[^a-z0-9]', '', name)

    if not domain_name or len(domain_name) < 3:
        return None

    # Return guessed .com domain
    return f"{domain_name}.com"


def guess_alternative_domains(domain: str) -> list[str]:
    """
    Generate alternative domain variations to try.

    Useful when main domain doesn't work or is wrong.
    E.g., projectmanagementinstitute.com → [pmi.org, pmi.com, ...]

    Args:
        domain: Original domain to generate alternatives for

    Returns:
        List of alternative domains to try (INITIALS FIRST for long names)
    """
    alternatives = []
    initials_alternatives = []  # Higher priority for long names

    if not domain:
        return alternatives

    # Strip TLD to get base name
    parts = domain.lower().split(".")
    if len(parts) < 2:
        return alternatives

    base_name = parts[0]
    original_tld = ".".join(parts[1:])

    # TLDs to try - org first since many institutes use .org
    tlds_to_try = ["org", "com", "io", "co", "net"]

    # For long company names, try common abbreviations FIRST
    # E.g., "projectmanagementinstitute" → "pmi"
    if len(base_name) > 10:
        # Try extracting initials from known words
        # Common word patterns in company names
        known_words = [
            "project", "management", "institute", "international", "association",
            "american", "national", "global", "world", "united", "society",
            "health", "heart", "care", "healthcare", "medical", "financial", "technology",
            "tech", "software", "solutions", "services", "systems", "group",
            "enterprise", "business", "company", "corporation", "corp", "inc",
            "digital", "media", "marketing", "consulting", "network", "online",
        ]

        # Find words in order of their position in the domain name
        found_words_with_pos = []
        name_lower = base_name.lower()
        for word in known_words:
            pos = name_lower.find(word)
            if pos != -1:
                found_words_with_pos.append((pos, word))

        # Sort by position to get words in correct order
        found_words_with_pos.sort(key=lambda x: x[0])
        found_words = [w for _, w in found_words_with_pos]

        # If we found multiple words, generate initials
        if len(found_words) >= 2:
            initials = "".join(w[0] for w in found_words)
            if len(initials) >= 2 and len(initials) <= 5:
                # Add initials alternatives FIRST (highest priority)
                for tld in tlds_to_try:
                    initials_alternatives.append(f"{initials}.{tld}")

        # Also try splitting on common patterns (CamelCase or word boundaries)
        # E.g., "healthEdge" or hyphenated names
        camel_words = re.findall(r'[A-Z][a-z]*|[a-z]+', base_name)
        if len(camel_words) >= 2:
            initials = "".join(w[0].lower() for w in camel_words)
            if len(initials) >= 2 and len(initials) <= 5 and initials != base_name:
                for tld in tlds_to_try:
                    alt = f"{initials}.{tld}"
                    if alt not in initials_alternatives:
                        initials_alternatives.append(alt)

    # Try different TLDs for original name (lower priority)
    for tld in tlds_to_try:
        if tld != original_tld:
            alternatives.append(f"{base_name}.{tld}")

    # Return initials first (most likely to work), then TLD variations
    return initials_alternatives + alternatives


def verify_domain_accessible(domain: str, timeout: float = 3.0) -> bool:
    """
    Check if a domain is accessible (returns 200 or redirects).

    Args:
        domain: Domain to check
        timeout: Request timeout

    Returns:
        True if domain is accessible
    """
    if not domain:
        return False

    try:
        client = get_http_client()
        url = f"https://{domain}"
        response = client.head(url, timeout=timeout)
        return response.status_code < 400
    except Exception:
        return False


def resolve_domain_redirect(domain: str, timeout: float = 2.0) -> str | None:
    """
    Check if a domain redirects to a different domain.

    E.g., projectmanagementinstitute.com might redirect to pmi.org

    Falls back from HEAD to GET if HEAD returns 405 (Method Not Allowed).
    Also strips marketing subdomains from the redirected domain.

    Args:
        domain: Domain to check
        timeout: Request timeout (reduced to 2.0s for speed)

    Returns:
        Final domain after redirects, or original domain if no redirect/error
    """
    if not domain:
        return None

    try:
        client = get_http_client()
        url = f"https://{domain}"

        # Try HEAD first (faster, no body download)
        try:
            response = client.head(url, timeout=timeout)
            # If HEAD returns 405, fall back to GET
            if response.status_code == 405:
                raise httpx.HTTPStatusError("HEAD not allowed", request=response.request, response=response)
            final_url = str(response.url)
        except (httpx.HTTPStatusError, httpx.RequestError):
            # Fall back to GET
            response = client.get(url, timeout=timeout)
            final_url = str(response.url)

        # Extract domain from final URL
        final_domain = extract_domain(final_url, strip_marketing=True)

        # Check if we actually redirected to a different domain (not just path change)
        if final_domain and final_domain.lower() != domain.lower():
            # Also strip marketing subdomain from original for fair comparison
            original_stripped = strip_marketing_subdomain(domain.lower())
            if final_domain.lower() != original_stripped:
                logger.info(f"Domain redirect detected: {domain} → {final_domain}")
                return final_domain

        return domain
    except httpx.TimeoutException:
        logger.debug(f"Timeout checking domain redirect for {domain}")
        return domain
    except Exception as e:
        logger.debug(f"Error checking domain redirect for {domain}: {e}")
        return domain


def resolve_sponsor_domain(
    tracking_url: str | None,
    company_name: str | None,
    resolve_redirects: bool = True,
) -> str | None:
    """
    Get the actual sponsor domain from a tracking URL or company name.

    Priority:
    1. Follow redirects on tracking URLs to get real domain
    2. Extract domain from non-tracking URL
    3. Guess domain from company name

    Args:
        tracking_url: URL that may be a tracking link
        company_name: Company name as fallback
        resolve_redirects: Whether to follow redirects (slower but more accurate)

    Returns:
        Sponsor's actual domain or None
    """
    # Skip newsletter/tracking domains
    skip_domains = {
        "morningbrew.com", "healthcare-brew.com", "healthcarebrew.com",
        "links.morningbrew.com", "link.morningbrew.com",
        "twitter.com", "x.com", "facebook.com", "linkedin.com",
        "instagram.com", "youtube.com", "google.com",
    }

    if tracking_url:
        # Check if this is a tracking domain that needs resolving
        current_domain = extract_domain(tracking_url)

        if current_domain and current_domain.lower() in skip_domains:
            # This is a tracking link - try to resolve it
            if resolve_redirects and is_tracking_domain(tracking_url):
                resolved_url = resolve_redirect_url(tracking_url)
                if resolved_url:
                    resolved_domain = extract_domain(resolved_url)
                    if resolved_domain and resolved_domain.lower() not in skip_domains:
                        return resolved_domain
        elif current_domain and current_domain.lower() not in skip_domains:
            # This is already a real domain
            return current_domain

    # Fallback: guess from company name
    if company_name:
        return guess_domain_from_name(company_name)

    return None
