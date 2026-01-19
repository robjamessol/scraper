"""Helper utilities for text processing and data extraction."""

import re
from urllib.parse import urlparse


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
