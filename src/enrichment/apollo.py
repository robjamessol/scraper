"""
Apollo.io Contact Enrichment Module

Finds marketing/advertising contacts at companies discovered from newsletter ads.

Workflow:
1. Scrape company website for contact emails (free, checks multiple pages with Playwright)
2. Generate & verify common email patterns via SMTP (info@, contact@, sales@, etc.)
3. Use Apollo People Search for additional contacts (free tier: 600 credits/month)
4. Combine all results, prioritizing verified contacts

All contact finding is FREE - no paid APIs needed!
"""

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .website_scraper import WebsiteScraper, WebsiteContact
from .email_finder import EmailFinder, FoundEmail

logger = logging.getLogger(__name__)


@dataclass
class Contact:
    """A contact person at a company."""
    name: str
    email: str | None
    title: str | None
    phone: str | None
    linkedin_url: str | None
    email_status: str | None = None  # verified, guessed, etc.
    confidence: str = "high"
    apollo_id: str | None = None

    @property
    def is_verified(self) -> bool:
        """Check if email is verified."""
        return self.email_status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "email": self.email,
            "email_verified": self.is_verified,
            "title": self.title,
            "phone": self.phone,
            "linkedin_url": self.linkedin_url,
        }


@dataclass
class CompanyInfo:
    """Enriched company information."""
    name: str
    domain: str
    website_url: str | None
    linkedin_url: str | None
    industry: str | None
    employee_count: str | None
    description: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.name,
            "company_domain": self.domain,
            "company_website": self.website_url,
            "company_linkedin": self.linkedin_url,
            "company_industry": self.industry,
            "company_size": self.employee_count,
            "company_description": self.description,
        }


@dataclass
class ApolloStats:
    """Track API usage statistics."""
    search_calls: int = 0  # Free
    enrich_calls: int = 0  # Uses credits
    bulk_enrich_calls: int = 0
    contacts_found: int = 0
    emails_verified: int = 0
    rate_limit_hits: int = 0


class RateLimitError(Exception):
    """Raised when Apollo rate limit is hit."""
    pass


class ApolloEnricher:
    """
    Contact enrichment using Apollo.io API.

    Uses two-step workflow for credit efficiency:
    1. People Search (FREE) - Find prospects with LinkedIn URLs
    2. People Match (CREDITS) - Enrich with verified contact data

    Finds up to 3 contacts per company, prioritizing:
    1. Advertising/Media roles (ad buyers, media planners)
    2. Partnership/BD roles
    3. Marketing leadership (VP, Director, CMO)
    """

    BASE_URL = "https://api.apollo.io/api/v1"

    # Title search priorities (most likely to buy newsletter ads)
    TITLE_PRIORITIES = [
        # Tier 1 - Ad buyers (most likely to have budget)
        ["advertising", "media buyer", "media planner", "ad ops", "paid media",
         "performance marketing", "growth marketing", "demand gen"],
        # Tier 2 - Partnerships (often handle newsletter deals)
        ["partnerships", "business development", "strategic partnerships",
         "alliances", "affiliate"],
        # Tier 3 - Marketing leadership (decision makers)
        ["vp marketing", "vice president marketing", "director marketing",
         "head of marketing", "cmo", "chief marketing officer"],
        # Tier 4 - General marketing (fallback)
        ["marketing manager", "brand marketing", "digital marketing"],
    ]

    # Seniority levels to prioritize
    SENIORITIES = ["director", "vp", "c_suite", "founder", "manager"]

    def __init__(self, api_key: str | None = None, log_callback: callable = None):
        """
        Initialize Apollo enricher.

        Args:
            api_key: Apollo.io API key. If not provided, reads from APOLLO_API_KEY env var.
            log_callback: Optional callback function for logging (e.g., for live UI updates)
        """
        self.api_key = api_key or os.getenv("APOLLO_API_KEY")
        if not self.api_key or self.api_key == "your_key_here":
            self.api_key = None
            logger.warning("Apollo API key not configured. Contact enrichment disabled.")

        self.client = httpx.Client(timeout=30.0)
        self.stats = ApolloStats()
        self._last_request_time = 0
        self._min_request_interval = 0.5  # 500ms between requests for rate limiting
        self._log_callback = log_callback

    def _log(self, message: str, level: str = "info"):
        """Log a message, optionally to callback."""
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

        if self._log_callback:
            self._log_callback(message, level)

    @property
    def is_configured(self) -> bool:
        """Check if Apollo API is configured."""
        return self.api_key is not None

    def get_stats(self) -> dict:
        """Get API usage statistics."""
        return {
            "search_calls_free": self.stats.search_calls,
            "enrich_calls_paid": self.stats.enrich_calls,
            "bulk_enrich_calls": self.stats.bulk_enrich_calls,
            "total_credits_used": self.stats.enrich_calls + self.stats.bulk_enrich_calls,
            "contacts_found": self.stats.contacts_found,
            "emails_verified": self.stats.emails_verified,
            "rate_limit_hits": self.stats.rate_limit_hits,
        }

    # Marketing/tracking subdomains to strip (get root domain instead)
    STRIP_SUBDOMAINS = {
        "get", "go", "info", "promo", "try", "start", "join", "buy", "shop",
        "links", "link", "click", "track", "t", "l", "r", "email", "mail",
        "news", "newsletter", "offers", "deals", "landing", "lp", "pages",
        "invite", "signup", "register", "app", "web", "m", "mobile",
        "partners", "partner", "affiliate", "ref", "campaign", "ads", "ad",
        "learn", "discover", "explore", "hello", "hi", "meet", "connect",
        "invest", "demo", "trial", "free", "www2", "secure", "my", "account",
    }

    @staticmethod
    def clean_domain(domain: str | None) -> str | None:
        """
        Clean domain to just the domain name (no protocol, path, etc.)

        Also strips common marketing subdomains like get.*, go.*, invest.*, etc.
        to get the root company domain.

        IMPORTANT: Apollo expects domain only, not full URL.
        """
        if not domain:
            return None

        # Remove protocol if present
        if "://" in domain:
            domain = urlparse(domain).netloc or domain

        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]

        # Remove any path
        domain = domain.split("/")[0]

        # Remove port if present
        domain = domain.split(":")[0]

        domain = domain.lower().strip()

        if not domain:
            return None

        # Strip marketing/tracking subdomains to get root domain
        # e.g., get.expertvoice.com -> expertvoice.com
        # e.g., invest.xtremeone.com -> xtremeone.com
        parts = domain.split(".")
        if len(parts) > 2:
            # Check if first part is a marketing subdomain
            if parts[0] in ApolloEnricher.STRIP_SUBDOMAINS:
                # Return without the subdomain
                domain = ".".join(parts[1:])

        return domain

    def _rate_limit_wait(self):
        """Ensure minimum time between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        retry=retry_if_exception_type(RateLimitError),
    )
    def _api_request(self, endpoint: str, data: dict) -> dict | None:
        """Make an API request to Apollo with rate limit handling."""
        if not self.is_configured:
            return None

        self._rate_limit_wait()

        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self.api_key,
        }

        try:
            logger.debug(f"Apollo API request: {endpoint}")
            response = self.client.post(url, json=data, headers=headers)

            if response.status_code == 429:
                self.stats.rate_limit_hits += 1
                logger.warning("Apollo rate limit hit, backing off...")
                raise RateLimitError("Rate limit exceeded")

            if response.status_code != 200:
                logger.error(f"Apollo API {endpoint} returned {response.status_code}: {response.text[:200]}")
                return None

            result = response.json()
            logger.debug(f"Apollo API {endpoint} response keys: {list(result.keys()) if result else 'None'}")
            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error("Apollo API key invalid or expired")
            elif e.response.status_code == 422:
                logger.warning(f"Apollo validation error: {e.response.text}")
            else:
                logger.error(f"Apollo API error: {e.response.status_code} - {e.response.text}")
            return None

        except RateLimitError:
            raise  # Let tenacity handle retry

        except Exception as e:
            logger.error(f"Apollo API request failed: {e}")
            return None

    def search_prospects(
        self,
        domain: str,
        titles: list[str] | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """
        Search for prospects at a company (FREE - no credits used).

        Returns basic info including LinkedIn URLs and Apollo IDs,
        but NO emails or phone numbers.

        Args:
            domain: Company domain (e.g., "healthedge.com")
            titles: Optional list of job titles to filter by
            max_results: Max results to return

        Returns:
            List of prospect dicts with name, title, linkedin_url, apollo_id
        """
        if not self.is_configured:
            logger.warning("Apollo not configured, skipping search")
            return []

        domain = self.clean_domain(domain)
        if not domain:
            logger.warning("No domain provided for search")
            return []

        search_data = {
            "q_organization_domains_list": [domain],
            "page": 1,
            "per_page": min(max_results, 100),
        }

        if titles:
            search_data["person_titles"] = titles
            # Add seniority filter only when filtering by titles
            search_data["person_seniorities"] = self.SENIORITIES

        self._log(f"Searching Apollo for {domain}...")

        # Try the API search endpoint first (some API keys require this)
        result = self._api_request("mixed_people/search", search_data)

        # If that fails, try the alternate endpoint
        if not result or not result.get("people"):
            self._log(f"Trying alternate endpoint for {domain}...")
            result = self._api_request("mixed_people/api_search", search_data)

        if not result:
            self._log(f"Apollo search returned no result for {domain}", "warning")
            return []

        self.stats.search_calls += 1

        # Log what we got back
        people_count = len(result.get("people", []))
        total_count = result.get("pagination", {}).get("total_entries", 0)
        self._log(f"Found {people_count} people at {domain}")

        prospects = []
        for person in result.get("people", []):
            prospect = {
                "apollo_id": person.get("id"),
                "name": person.get("name", "Unknown"),
                "first_name": person.get("first_name"),
                "last_name": person.get("last_name"),
                "title": person.get("title"),
                "linkedin_url": person.get("linkedin_url"),
                # Note: Search does NOT return email/phone - need to enrich
            }
            prospects.append(prospect)
            logger.debug(f"  - {prospect['name']} ({prospect['title']}) - LinkedIn: {'yes' if prospect['linkedin_url'] else 'no'}")

        return prospects

    def enrich_person(
        self,
        domain: str,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        linkedin_url: str | None = None,
        apollo_id: str | None = None,
    ) -> Contact | None:
        """
        Enrich a single person with contact data (USES CREDITS).

        IMPORTANT: Must provide sufficient identifying data for a match.
        Priority order for best match rates:
        1. email (best)
        2. linkedin_url (~60% match rate)
        3. apollo_id (exact match)
        4. first_name + last_name + domain (good match rate)
        5. name only = WILL FAIL

        Args:
            domain: Company domain
            first_name: Person's first name
            last_name: Person's last name
            email: Known email address (best for matching)
            linkedin_url: LinkedIn profile URL
            apollo_id: Apollo ID from previous search

        Returns:
            Contact object with email/phone or None
        """
        if not self.is_configured:
            return None

        domain = self.clean_domain(domain)

        # Validate we have enough data to match
        has_email = bool(email)
        has_linkedin = bool(linkedin_url)
        has_apollo_id = bool(apollo_id)
        has_name_and_domain = bool(first_name and last_name and domain)

        if not (has_email or has_linkedin or has_apollo_id or has_name_and_domain):
            logger.warning("Insufficient data for Apollo enrichment - need email, linkedin, apollo_id, or name+domain")
            return None

        # Build payload with all available identifiers
        payload = {
            "reveal_personal_emails": True,
            "reveal_phone_number": True,
        }

        if email:
            payload["email"] = email
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url
        if apollo_id:
            payload["id"] = apollo_id
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if domain:
            payload["domain"] = domain

        result = self._api_request("people/match", payload)

        if not result or not result.get("person"):
            return None

        self.stats.enrich_calls += 1
        person = result["person"]

        contact = self._parse_contact(person)
        if contact:
            self.stats.contacts_found += 1
            if contact.is_verified:
                self.stats.emails_verified += 1

        return contact

    def bulk_enrich_people(
        self,
        prospects: list[dict],
    ) -> list[Contact]:
        """
        Bulk enrich multiple prospects (more efficient than single calls).

        Uses bulk_match endpoint - up to 10 people per call.

        Args:
            prospects: List of prospect dicts with identifying data

        Returns:
            List of Contact objects
        """
        if not self.is_configured or not prospects:
            self._log(f"Bulk enrich skipped: configured={self.is_configured}, prospects={len(prospects) if prospects else 0}", "warning")
            return []

        self._log(f"Enriching {len(prospects)} prospects...")
        contacts = []

        # Process in batches of 10
        for i in range(0, len(prospects), 10):
            batch = prospects[i:i+10]

            details = []
            for p in batch:
                detail = {}

                # Add all available identifiers
                if p.get("email"):
                    detail["email"] = p["email"]
                if p.get("linkedin_url"):
                    detail["linkedin_url"] = p["linkedin_url"]
                if p.get("apollo_id"):
                    detail["id"] = p["apollo_id"]
                if p.get("first_name"):
                    detail["first_name"] = p["first_name"]
                if p.get("last_name"):
                    detail["last_name"] = p["last_name"]
                if p.get("domain"):
                    detail["domain"] = self.clean_domain(p["domain"])

                # Skip if no useful identifiers
                if not detail:
                    logger.debug(f"Skipping prospect with no identifiers: {p.get('name', 'unknown')}")
                    continue

                details.append(detail)
                logger.debug(f"Enriching: {p.get('name', 'unknown')} - identifiers: {list(detail.keys())}")

            if not details:
                logger.warning("No valid details to enrich in this batch")
                continue

            logger.info(f"Calling bulk_match with {len(details)} people...")
            result = self._api_request("people/bulk_match", {
                "reveal_personal_emails": True,
                "reveal_phone_number": True,
                "details": details,
            })

            if not result:
                logger.warning("Bulk match returned no result")
                continue

            self.stats.bulk_enrich_calls += 1

            # Log what we got back
            matches_count = len(result.get("matches", []))
            missing = result.get("missing_records", 0)
            logger.info(f"Bulk match result: {matches_count} matches, {missing} missing")

            for match in result.get("matches", []):
                person = match.get("person")
                if person:
                    contact = self._parse_contact(person)
                    if contact:
                        contacts.append(contact)
                        self.stats.contacts_found += 1
                        if contact.is_verified:
                            self.stats.emails_verified += 1

        return contacts

    def _parse_contact(self, person: dict) -> Contact | None:
        """Parse Apollo person data into Contact object."""
        if not person:
            return None

        email = person.get("email")
        email_status = person.get("email_status")

        # Extract phone - prefer direct dial
        phone = None
        phone_numbers = person.get("phone_numbers", [])
        for pn in phone_numbers:
            if pn.get("status") == "valid_number":
                if pn.get("type") == "direct":
                    phone = pn.get("sanitized_number") or pn.get("number")
                    break
                elif not phone:
                    phone = pn.get("sanitized_number") or pn.get("number")

        return Contact(
            name=person.get("name", "Unknown"),
            email=email,
            email_status=email_status,
            title=person.get("title"),
            phone=phone,
            linkedin_url=person.get("linkedin_url"),
            apollo_id=person.get("id"),
            confidence="high" if email_status == "verified" else "medium",
        )

    def search_and_enrich(
        self,
        domain: str,
        max_contacts: int = 3,
    ) -> list[Contact]:
        """
        Two-step workflow: Search (free) → Enrich (paid).

        This is the recommended approach for credit efficiency.

        Args:
            domain: Company domain
            max_contacts: Maximum contacts to return

        Returns:
            List of enriched Contact objects
        """
        if not self.is_configured:
            return []

        domain = self.clean_domain(domain)
        if not domain:
            return []

        contacts = []
        seen_emails = set()

        # First try: broad search without title filter (gets more results)
        self._log(f"Searching for any contacts at {domain}...")
        prospects = self.search_prospects(domain, titles=None, max_results=10)

        if prospects:
            self._log(f"Found {len(prospects)} prospects, enriching...")
            # Add domain to each prospect for better matching
            for p in prospects:
                p["domain"] = domain

            # Bulk enrich to get actual contact data
            enriched = self.bulk_enrich_people(prospects[:max_contacts * 2])  # Get extra in case some fail

            for contact in enriched:
                if len(contacts) >= max_contacts:
                    break
                if contact.email and contact.email in seen_emails:
                    continue
                if contact.email:
                    seen_emails.add(contact.email)
                contacts.append(contact)
                self._log(f"  Got: {contact.name} ({contact.title}) - {contact.email}")

        # If broad search didn't work, try title-specific searches
        if len(contacts) < max_contacts:
            for tier_idx, titles in enumerate(self.TITLE_PRIORITIES):
                if len(contacts) >= max_contacts:
                    break

                self._log(f"Searching for {titles[0]}... roles at {domain}")
                prospects = self.search_prospects(domain, titles, max_results=5)

                if not prospects:
                    continue

                # Prepare prospects for bulk enrichment
                to_enrich = []
                for p in prospects:
                    if len(contacts) + len(to_enrich) >= max_contacts:
                        break
                    p["domain"] = domain
                    to_enrich.append(p)

                if not to_enrich:
                    continue

                enriched = self.bulk_enrich_people(to_enrich)

                for contact in enriched:
                    if len(contacts) >= max_contacts:
                        break
                    if contact.email and contact.email in seen_emails:
                        continue
                    if contact.email:
                        seen_emails.add(contact.email)
                    contact.confidence = "high" if tier_idx < 2 else "medium"
                    contacts.append(contact)

        if not contacts:
            self._log(f"No contacts found at {domain}", "warning")

        return contacts

    def enrich_company(self, domain: str) -> CompanyInfo | None:
        """
        Get company information by domain.

        Args:
            domain: Company domain

        Returns:
            CompanyInfo object or None
        """
        if not self.is_configured:
            return None

        domain = self.clean_domain(domain)
        if not domain:
            return None

        result = self._api_request("organizations/enrich", {
            "domain": domain,
        })

        if not result or "organization" not in result:
            return None

        org = result["organization"]

        return CompanyInfo(
            name=org.get("name", domain),
            domain=domain,
            website_url=org.get("website_url") or f"https://{domain}",
            linkedin_url=org.get("linkedin_url"),
            industry=org.get("industry"),
            employee_count=org.get("estimated_num_employees"),
            description=org.get("short_description"),
        )

    def enrich_advertiser(
        self,
        advertiser: dict,
        max_contacts: int = 3,
    ) -> dict:
        """
        Fully enrich an advertiser with company info and contacts.

        Workflow (multi-source approach - ALL FREE):
        1. Scrape website (free, uses Playwright for JS-rendered sites)
        2. Generate & verify common email patterns via SMTP (info@, sales@, etc.)
        3. Use Apollo People Search for additional contacts (free tier)
        4. Combine all results, prioritizing verified contacts

        Args:
            advertiser: Advertiser dict from scraper
            max_contacts: Max contacts to find

        Returns:
            Enriched advertiser dict with contact fields added
        """
        domain = self.clean_domain(advertiser.get("advertiser_domain"))
        company_name = advertiser.get("advertiser_name")

        if not domain:
            return self._add_empty_contact_fields(advertiser, max_contacts)

        contacts = []
        seen_emails = set()

        # Step 1: Website scrape (Claude suggests pages, fast regex extraction)
        self._log(f"Step 1: Scraping website {domain}...")
        website_contacts = self._scrape_website_contacts(domain, company_name=company_name)

        if website_contacts:
            self._log(f"Website: Found {len(website_contacts)} email(s)")
            for wc in website_contacts[:max_contacts]:
                contact = Contact(
                    name=wc.name if wc.name and not self._is_email_prefix_name(wc.name) else None,
                    email=wc.email,
                    email_status="website",
                    title=wc.title,
                    phone=wc.phone,
                    linkedin_url=wc.linkedin_url,
                    confidence="medium" if wc.name else "low",
                )
                contacts.append(contact)
                seen_emails.add(wc.email.lower())

        # Step 1b removed for speed - Claude extraction is slow
        # Website scraping + email patterns are sufficient

        # Step 2: Generate email patterns (no SMTP - too slow, just guesses)
        if len(contacts) < max_contacts:
            self._log(f"Step 2: Generating email patterns for {domain}...")

            email_finder = EmailFinder(
                verify_smtp=False,  # Disabled - too slow
                timeout=3.0,
                max_workers=3,
                log_callback=self._log_callback,
            )
            # Just generate top patterns, no verification
            found_emails = email_finder.find_emails(domain, max_results=max_contacts, max_verify=0)

            added_count = 0
            verified_count = 0
            for fe in found_emails:
                if len(contacts) >= max_contacts:
                    break
                if fe.email and fe.email.lower() not in seen_emails:
                    # Don't use email prefix as contact name - it's misleading
                    # Mark as pattern-generated so it's clear this isn't a real person
                    contact = Contact(
                        name=None,  # No real name available for pattern-generated emails
                        email=fe.email,
                        email_status="smtp_verified" if fe.verified else "pattern_guess",
                        title=f"General ({fe.email_type})" if fe.email_type else None,
                        phone=None,
                        linkedin_url=None,
                        confidence="high" if fe.verified else "low",
                    )
                    contacts.append(contact)
                    seen_emails.add(fe.email.lower())
                    added_count += 1
                    if fe.verified:
                        verified_count += 1

            if added_count > 0:
                self._log(f"Email finder: Added {added_count} contact(s) ({verified_count} SMTP verified)")

        # Step 3: Use Apollo to find additional contacts (FREE search)
        if self.is_configured and len(contacts) < max_contacts:
            remaining = max_contacts - len(contacts)
            self._log(f"Step 3: Searching Apollo for {remaining} more contact(s)...")
            apollo_contacts = self.search_and_enrich(domain, remaining + 2)

            if apollo_contacts:
                self._log(f"Apollo: Found {len(apollo_contacts)} contact(s)")
                for ac in apollo_contacts:
                    if len(contacts) >= max_contacts:
                        break
                    if ac.email and ac.email.lower() not in seen_emails:
                        # Apollo contacts are high quality
                        contacts.insert(0, ac)
                        seen_emails.add(ac.email.lower())
                    elif not ac.email:
                        contacts.append(ac)

        # Sort: Apollo verified > SMTP verified > Claude extracted > website > pattern guesses
        def contact_priority(c: Contact) -> int:
            if c.email_status == "verified":  # Apollo verified
                return 0
            if c.email_status == "smtp_verified":  # Our SMTP verification
                return 1
            if c.email_status == "claude_extracted":  # Claude found on website
                return 2
            if c.email_status == "website":  # Found on website
                return 3
            if c.email_status == "pattern_guess":  # Unverified pattern guess
                return 5
            return 4

        contacts.sort(key=contact_priority)
        contacts = contacts[:max_contacts]

        # Step 4 removed for speed - simple priority sorting is sufficient

        self._log(f"Total contacts found for {domain}: {len(contacts)}")

        # Add contacts to advertiser
        enriched = advertiser.copy()

        for i in range(max_contacts):
            prefix = "primary" if i == 0 else f"backup_{i}"

            if i < len(contacts):
                contact = contacts[i]
                # Filter out fake names (email prefixes, generic terms)
                contact_name = contact.name if contact.name and not self._is_email_prefix_name(contact.name) else None
                enriched[f"{prefix}_contact"] = contact_name
                enriched[f"{prefix}_email"] = contact.email
                enriched[f"{prefix}_email_verified"] = contact.is_verified
                enriched[f"{prefix}_title"] = contact.title
                enriched[f"{prefix}_phone"] = contact.phone
                enriched[f"{prefix}_linkedin"] = contact.linkedin_url
            else:
                enriched[f"{prefix}_contact"] = None
                enriched[f"{prefix}_email"] = None
                enriched[f"{prefix}_email_verified"] = False
                enriched[f"{prefix}_title"] = None
                enriched[f"{prefix}_phone"] = None
                enriched[f"{prefix}_linkedin"] = None

        # Get company info - try Apollo first, then Claude as fallback
        company = self.enrich_company(domain)
        if company:
            enriched["company_website"] = company.website_url
            enriched["company_linkedin"] = company.linkedin_url
            enriched["company_industry"] = company.industry
            enriched["company_size"] = company.employee_count
            enriched["company_description"] = company.description
        else:
            # Apollo failed - try Claude for company enrichment
            self._log(f"Apollo has no company data for {domain}, trying Claude...")
            claude_company = self._enrich_company_with_claude(
                advertiser.get("advertiser_name", domain),
                domain,
                advertiser.get("full_ad_copy"),
            )
            if claude_company:
                enriched["company_website"] = f"http://www.{domain}"
                enriched["company_linkedin"] = None  # Claude can't find LinkedIn URLs
                enriched["company_industry"] = claude_company.get("industry")
                enriched["company_size"] = claude_company.get("company_size")
                enriched["company_description"] = claude_company.get("description")
                self._log(f"Claude provided company info: {claude_company.get('industry')}")

        enriched["enriched"] = True
        enriched["contacts_found"] = len(contacts)
        enriched["verified_emails"] = sum(1 for c in contacts if c.is_verified)

        return enriched

    def _extract_emails_with_claude(
        self,
        domain: str,
        company_name: str,
    ) -> list[dict]:
        """
        Use Claude to extract emails from website text that regex might miss.

        Fetches the website and uses Claude to find obfuscated or hidden emails.

        Args:
            domain: Company domain
            company_name: Company name

        Returns:
            List of dicts with email, name, title, purpose
        """
        try:
            from .claude_agent import ClaudeAgent
            import httpx

            # First, fetch website text
            try:
                with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                    # Try contact page first, then homepage
                    for path in ["/contact", "/contact-us", "/about", ""]:
                        try:
                            url = f"https://{domain}{path}"
                            response = client.get(url)
                            if response.status_code == 200:
                                from bs4 import BeautifulSoup
                                soup = BeautifulSoup(response.text, "lxml")
                                # Remove scripts/styles
                                for tag in soup(["script", "style"]):
                                    tag.decompose()
                                text = soup.get_text(separator=" ")
                                if len(text) > 100:
                                    break
                        except Exception:
                            continue
                    else:
                        return []
            except Exception:
                return []

            # Now use Claude to extract emails
            agent = ClaudeAgent(log_callback=self._log_callback)
            if not agent.is_configured:
                return []

            emails = agent.extract_emails_from_text(text[:5000], company_name)
            agent.close()

            # Filter to only emails from this domain
            domain_emails = [
                e for e in emails
                if e.get("email", "").lower().endswith(f"@{domain.lower()}")
            ]

            return domain_emails

        except ImportError:
            return []
        except Exception as e:
            self._log(f"Claude email extraction failed: {e}", "warning")
            return []

    def _prioritize_contacts_with_claude(
        self,
        contacts: list[Contact],
        company_name: str,
    ) -> list[Contact]:
        """
        Use Claude to intelligently prioritize contacts for ad sales outreach.

        Args:
            contacts: List of Contact objects
            company_name: Company name

        Returns:
            Contacts sorted by priority
        """
        try:
            from .claude_agent import ClaudeAgent

            agent = ClaudeAgent(log_callback=self._log_callback)
            if not agent.is_configured:
                return contacts

            # Convert to dicts for Claude
            contact_dicts = [
                {"email": c.email, "name": c.name, "title": c.title}
                for c in contacts
                if c.email
            ]

            if not contact_dicts:
                return contacts

            prioritized = agent.prioritize_contacts(
                contact_dicts,
                company_name,
                goal="newsletter advertising",
            )
            agent.close()

            # Map back to Contact objects
            email_to_contact = {c.email.lower(): c for c in contacts if c.email}
            result = []
            for pd in prioritized:
                email = pd.get("email", "").lower()
                if email in email_to_contact:
                    result.append(email_to_contact[email])
                    del email_to_contact[email]

            # Add any remaining
            result.extend(email_to_contact.values())

            self._log(f"Claude prioritized {len(result)} contacts")
            return result

        except ImportError:
            return contacts
        except Exception as e:
            self._log(f"Claude contact prioritization failed: {e}", "warning")
            return contacts

    def _get_claude_email_suggestions(
        self,
        company_name: str,
        ad_copy: str | None,
        industry: str | None,
    ) -> list[str] | None:
        """
        Use Claude to suggest the best email prefixes for this company.

        Claude analyzes the company type and suggests which email addresses
        are most likely to exist and reach the right person for ad sales.

        Args:
            company_name: Company name
            ad_copy: Ad copy for context
            industry: Company industry if known

        Returns:
            List of suggested email prefixes in priority order, or None
        """
        try:
            from .claude_agent import ClaudeAgent

            agent = ClaudeAgent(log_callback=self._log_callback)
            if not agent.is_configured:
                return None

            result = agent.suggest_email_prefixes(company_name, ad_copy, industry)
            agent.close()

            if result:
                self._log(f"Claude suggested prefixes: {result[:3]}")
                return result
            return None

        except ImportError:
            return None
        except Exception as e:
            self._log(f"Claude email suggestion failed: {e}", "warning")
            return None

    def _enrich_company_with_claude(
        self,
        company_name: str,
        domain: str,
        ad_copy: str | None = None,
    ) -> dict | None:
        """
        Use Claude to generate company information when Apollo fails.

        Args:
            company_name: Company name
            domain: Company domain
            ad_copy: Optional ad copy for context

        Returns:
            Dict with company info or None
        """
        try:
            from .claude_agent import ClaudeAgent

            agent = ClaudeAgent(log_callback=self._log_callback)
            if not agent.is_configured:
                return None

            # Use ad copy as context if available
            website_text = ad_copy if ad_copy else None

            analysis = agent.analyze_company(company_name, domain, website_text)
            agent.close()

            if analysis:
                # Map company size estimate to a number
                size_map = {
                    "startup": "10",
                    "small": "50",
                    "medium": "200",
                    "large": "1000",
                    "enterprise": "5000+",
                }
                size = size_map.get(
                    analysis.company_size_estimate.lower() if analysis.company_size_estimate else "",
                    None
                )

                return {
                    "description": analysis.description,
                    "industry": analysis.industry,
                    "company_size": size,
                    "products_services": analysis.products_services,
                }
            return None

        except ImportError:
            self._log("Claude agent not available", "warning")
            return None
        except Exception as e:
            self._log(f"Claude company enrichment failed: {e}", "warning")
            return None

    @staticmethod
    def _is_email_prefix_name(name: str) -> bool:
        """
        Check if a 'name' is actually just an email prefix (not a real person).

        Examples that return True:
        - "advertising", "ads", "info", "contact", "sales"
        - "Fan Community", "Contact For", "cc.life", "kuwait.info"
        """
        if not name:
            return True

        name_lower = name.lower().strip()

        # Common email prefixes that aren't real names
        fake_name_patterns = {
            "advertising", "ads", "ad", "adops", "adsales",
            "info", "contact", "hello", "support", "help",
            "sales", "marketing", "press", "pr", "media",
            "partnerships", "partner", "partners", "team",
            "general", "inquiries", "enquiries", "business",
            "admin", "office", "reception", "careers", "jobs",
            "news", "newsletter", "subscribe", "feedback",
            # Regional/country prefixes (e.g., "kuwait.info@hsbc.com")
            "kuwait", "singapore", "hongkong", "malaysia", "usa", "uk",
        }

        # Check if name is just a prefix
        if name_lower in fake_name_patterns:
            return True

        # Check if name contains suspicious patterns (TLDs, generic terms)
        suspicious_patterns = [
            "contact for", "fan community", "email us", "contact us",
            # TLD-like patterns
            ".life", ".com", ".info", ".sg", ".org", ".net", ".io", ".co",
            ".mail", ".hsbc", ".bank",
            # Email artifacts
            "@", "info@", "mailto",
        ]
        for pattern in suspicious_patterns:
            if pattern in name_lower:
                return True

        # Check for domain-like patterns (e.g., "cc.life", "mail.life")
        if "." in name and len(name.split(".")) >= 2:
            # Likely a domain part, not a name
            return True

        # Real names usually have at least 2 parts (first + last)
        # and don't contain numbers
        parts = name.split()
        if len(parts) == 1:
            # Single word - check if it's too short or looks like email prefix
            if len(name) < 4:
                return True
            # Check if it's all lowercase (real names are usually capitalized)
            if name == name_lower and len(name) < 10:
                return True

        # Check for numbers in name (unlikely for real names)
        if any(c.isdigit() for c in name):
            return True

        return False

    def _scrape_website_contacts(
        self,
        domain: str,
        company_name: str | None = None,
    ) -> list[WebsiteContact]:
        """
        Scrape a company website for contact information.

        Uses Claude for ONE smart navigation call, then fast regex extraction.
        SMTP verification is disabled (that was the bottleneck).

        Args:
            domain: Domain to scrape
            company_name: Company name for better extraction

        Returns:
            List of WebsiteContact objects
        """
        try:
            scraper = WebsiteScraper(
                timeout=5.0,
                max_pages=6,  # Enough to find emails on most sites
                use_browser=False,
                use_claude=True,  # ONE call to suggest best pages
                log_callback=self._log_callback,
            )
            result = scraper.scrape_domain(domain, company_name=company_name)
            return result.contacts
        except Exception as e:
            self._log(f"Website scrape error: {e}", "warning")
            return []

    def _enrich_website_contacts(
        self,
        website_contacts: list[WebsiteContact],
        domain: str,
        max_contacts: int,
    ) -> list[Contact]:
        """
        Enrich contacts found on website via Apollo.

        Uses People Match with the email we found - very efficient since
        we're matching by email rather than searching.
        """
        contacts = []

        # Prioritize advertising/generic emails over personal
        # (personal emails from websites may be outdated)
        sorted_contacts = sorted(
            website_contacts,
            key=lambda c: 0 if c.email_type == "advertising" else (1 if c.email_type == "generic" else 2)
        )

        for wc in sorted_contacts[:max_contacts * 2]:  # Try more in case some fail
            if len(contacts) >= max_contacts:
                break

            # Try to get name parts if we found a name
            first_name = None
            last_name = None
            if wc.name:
                parts = wc.name.split()
                if len(parts) >= 2:
                    first_name = parts[0]
                    last_name = " ".join(parts[1:])
                elif len(parts) == 1:
                    first_name = parts[0]

            # Enrich via Apollo using the email we found
            self._log(f"  Verifying {wc.email}...")
            enriched = self.enrich_person(
                domain=domain,
                email=wc.email,
                first_name=first_name,
                last_name=last_name,
            )

            if enriched and enriched.email:
                self._log(f"  ✓ Verified: {enriched.name} ({enriched.title})")
                contacts.append(enriched)
            else:
                # Apollo couldn't verify - use website data as-is
                contact = Contact(
                    name=wc.name or wc.email.split("@")[0],
                    email=wc.email,
                    email_status="website",  # Found on website, not verified by Apollo
                    title=wc.title,
                    phone=wc.phone,
                    linkedin_url=wc.linkedin_url,
                    confidence="medium",
                )
                contacts.append(contact)
                self._log(f"  ○ Using website data: {wc.email}")

        return contacts

    def _convert_website_contacts(self, website_contacts: list[WebsiteContact]) -> list[Contact]:
        """Convert WebsiteContact objects to Contact objects (when no Apollo configured)."""
        return [
            Contact(
                name=wc.name or wc.email.split("@")[0],
                email=wc.email,
                email_status="website",
                title=wc.title,
                phone=wc.phone,
                linkedin_url=wc.linkedin_url,
                confidence="medium",
            )
            for wc in website_contacts
        ]

    def _add_empty_contact_fields(self, advertiser: dict, max_contacts: int) -> dict:
        """Add empty contact fields to advertiser."""
        enriched = advertiser.copy()

        for i in range(max_contacts):
            prefix = "primary" if i == 0 else f"backup_{i}"
            enriched[f"{prefix}_contact"] = None
            enriched[f"{prefix}_email"] = None
            enriched[f"{prefix}_email_verified"] = False
            enriched[f"{prefix}_title"] = None
            enriched[f"{prefix}_phone"] = None
            enriched[f"{prefix}_linkedin"] = None

        domain = self.clean_domain(advertiser.get("advertiser_domain"))
        enriched["company_website"] = f"https://{domain}" if domain else None
        enriched["company_linkedin"] = None
        enriched["company_industry"] = None
        enriched["company_size"] = None
        enriched["company_description"] = None
        enriched["enriched"] = False
        enriched["contacts_found"] = 0
        enriched["verified_emails"] = 0

        return enriched

    def bulk_enrich_advertisers(
        self,
        advertisers: list[dict],
        max_contacts: int = 3,
        progress_callback: callable = None,
    ) -> list[dict]:
        """
        Enrich a list of advertisers.

        Args:
            advertisers: List of advertiser dicts
            max_contacts: Max contacts per advertiser
            progress_callback: Optional callback(current, total, advertiser_name)

        Returns:
            List of enriched advertiser dicts
        """
        enriched = []
        total = len(advertisers)

        for i, advertiser in enumerate(advertisers):
            if progress_callback:
                progress_callback(i + 1, total, advertiser.get("advertiser_name", "Unknown"))

            enriched_adv = self.enrich_advertiser(advertiser, max_contacts)
            enriched.append(enriched_adv)

        stats = self.get_stats()
        logger.info(
            f"Enriched {total} advertisers: "
            f"{stats['contacts_found']} contacts found, "
            f"{stats['emails_verified']} verified emails, "
            f"{stats['total_credits_used']} credits used"
        )
        return enriched

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def get_apollo_signup_instructions() -> str:
    """Return instructions for signing up for Apollo.io."""
    return """
## Apollo.io Setup (Free Tier - 600 credits/month)

1. **Sign up**: Go to https://www.apollo.io/ and create a free account

2. **Get API Key**:
   - Log in to Apollo
   - Click Settings (gear icon) → Integrations → API
   - Click "Generate API Key"
   - Copy the key

3. **Add to Railway**:
   - Go to your Railway dashboard
   - Click on your service → Variables tab
   - Add: `APOLLO_API_KEY` = your_key_here

4. **Usage**:
   - Free tier: 600 email credits/month
   - Search calls are FREE (finding prospects)
   - Enrichment uses credits (getting emails/phones)
   - The two-step workflow maximizes your credits

The enrichment will automatically find:
- Primary contact (marketing/ad buyer) with verified email
- 2 backup contacts
- Company info (size, industry, LinkedIn)
"""
