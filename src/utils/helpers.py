"""Helper utilities for text processing and data extraction."""

import re
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Known tracking/redirect domains that should be resolved
TRACKING_DOMAINS = {
    "links.morningbrew.com",
    "link.morningbrew.com",
    "links.healthcare-brew.com",
    "link.healthcare-brew.com",
    "email.morningbrew.com",
    "t.co",
    "bit.ly",
    "tinyurl.com",
    "ow.ly",
    "buff.ly",
    "mailtrack.io",
    "click.convertkit-mail.com",
    "click.convertkit-mail2.com",
}


def resolve_redirect_url(url: str, timeout: float = 5.0) -> str | None:
    """
    Follow redirects to get the final destination URL.

    This is critical for newsletter tracking links like:
    links.morningbrew.com/c/xxx → actual-advertiser.com

    Args:
        url: URL that may redirect
        timeout: Request timeout in seconds

    Returns:
        Final destination URL, or original URL if no redirects
    """
    if not url:
        return None

    try:
        # Use HEAD request to follow redirects without downloading content
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            response = client.head(url)
            final_url = str(response.url)

            # If we got redirected somewhere useful, return it
            if final_url and final_url != url:
                logger.debug(f"Resolved redirect: {url[:50]}... → {final_url[:50]}...")
                return final_url

            return url

    except httpx.TimeoutException:
        logger.debug(f"Timeout resolving redirect for {url[:50]}...")
        return url
    except Exception as e:
        logger.debug(f"Error resolving redirect for {url[:50]}...: {e}")
        return url


def is_tracking_domain(url: str) -> bool:
    """
    Check if URL is from a known tracking/redirect domain.

    Args:
        url: URL to check

    Returns:
        True if this is a tracking domain that should be resolved
    """
    domain = extract_domain(url)
    if not domain:
        return False
    return domain.lower() in TRACKING_DOMAINS


def extract_domain(url: str) -> str | None:
    """
    Extract the base domain from a URL.

    Args:
        url: Full URL

    Returns:
        Domain without www prefix, or None if invalid

    Examples:
        >>> extract_domain("https://www.healthedge.com/landing?utm=123")
        'healthedge.com'
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

        return domain if domain else None

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

    # Normalize: lowercase, remove spaces and special chars
    name = company_name.lower().strip()

    # Remove common suffixes
    name = re.sub(r'\s*(inc|llc|ltd|corp|co|company|technologies|labs|health|medical)\.?$', '', name, flags=re.IGNORECASE)

    # Remove spaces and special characters for domain
    domain_name = re.sub(r'[^a-z0-9]', '', name)

    if not domain_name or len(domain_name) < 2:
        return None

    # Return guessed .com domain
    return f"{domain_name}.com"


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
