"""
Apollo.io Contact Enrichment Module

Finds marketing/advertising contacts at companies discovered from newsletter ads.
Uses Apollo.io's People Search API to find decision makers.

Free tier: 600 email credits/month
Sign up at: https://www.apollo.io/
"""

import os
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


@dataclass
class Contact:
    """A contact person at a company."""
    name: str
    email: str | None
    title: str | None
    phone: str | None
    linkedin_url: str | None
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "email": self.email,
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


class ApolloEnricher:
    """
    Contact enrichment using Apollo.io API.

    Finds up to 3 contacts per company, prioritizing:
    1. Advertising/Media roles (ad buyers, media planners)
    2. Partnership/BD roles
    3. Marketing leadership (VP, Director, CMO)
    4. Any marketing role (fallback)
    """

    BASE_URL = "https://api.apollo.io/v1"

    # Title search priorities (most likely to buy newsletter ads)
    TITLE_PRIORITIES = [
        # Tier 1 - Ad buyers (most likely to have budget for newsletter ads)
        ["advertising", "media buyer", "media planner", "ad ops", "paid media",
         "performance marketing", "growth marketing", "demand gen"],
        # Tier 2 - Partnerships (often handle newsletter deals)
        ["partnerships", "business development", "strategic partnerships",
         "alliances", "affiliate"],
        # Tier 3 - Marketing leadership (decision makers)
        ["vp marketing", "vice president marketing", "director marketing",
         "head of marketing", "cmo", "chief marketing officer", "marketing director"],
        # Tier 4 - General marketing (fallback)
        ["marketing manager", "brand marketing", "content marketing",
         "digital marketing", "marketing lead"],
    ]

    def __init__(self, api_key: str | None = None):
        """
        Initialize Apollo enricher.

        Args:
            api_key: Apollo.io API key. If not provided, reads from APOLLO_API_KEY env var.
        """
        self.api_key = api_key or os.getenv("APOLLO_API_KEY")
        if not self.api_key or self.api_key == "your_key_here":
            self.api_key = None
            logger.warning("Apollo API key not configured. Contact enrichment disabled.")

        self.client = httpx.Client(timeout=30.0)
        self._credits_used = 0

    @property
    def is_configured(self) -> bool:
        """Check if Apollo API is configured."""
        return self.api_key is not None

    def get_credits_used(self) -> int:
        """Get number of API credits used this session."""
        return self._credits_used

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _api_request(self, endpoint: str, data: dict) -> dict | None:
        """Make an API request to Apollo."""
        if not self.is_configured:
            return None

        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self.api_key,
        }

        try:
            response = self.client.post(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error("Apollo API key invalid or expired")
            elif e.response.status_code == 429:
                logger.warning("Apollo rate limit hit, backing off...")
                raise  # Let tenacity retry
            else:
                logger.error(f"Apollo API error: {e.response.status_code} - {e.response.text}")
            return None

        except Exception as e:
            logger.error(f"Apollo API request failed: {e}")
            return None

    def search_contacts(
        self,
        domain: str,
        max_contacts: int = 3,
    ) -> list[Contact]:
        """
        Find contacts at a company by domain.

        Args:
            domain: Company domain (e.g., "healthedge.com")
            max_contacts: Maximum contacts to return (default 3)

        Returns:
            List of Contact objects, prioritized by title relevance
        """
        if not self.is_configured:
            logger.debug("Apollo not configured, skipping contact search")
            return []

        if not domain:
            return []

        contacts = []
        seen_emails = set()

        # Search each title tier until we have enough contacts
        for tier_idx, titles in enumerate(self.TITLE_PRIORITIES):
            if len(contacts) >= max_contacts:
                break

            result = self._api_request("mixed_people/search", {
                "q_organization_domains": domain,
                "person_titles": titles,
                "page": 1,
                "per_page": 5,
            })

            if not result or "people" not in result:
                continue

            self._credits_used += 1

            for person in result.get("people", []):
                if len(contacts) >= max_contacts:
                    break

                email = person.get("email")
                if email and email in seen_emails:
                    continue

                if email:
                    seen_emails.add(email)

                contact = Contact(
                    name=person.get("name", "Unknown"),
                    email=email,
                    title=person.get("title"),
                    phone=self._extract_phone(person),
                    linkedin_url=person.get("linkedin_url"),
                    confidence="high" if tier_idx < 2 else "medium",
                )
                contacts.append(contact)

                logger.debug(f"Found contact: {contact.name} ({contact.title}) at {domain}")

        return contacts

    def _extract_phone(self, person: dict) -> str | None:
        """Extract phone number from Apollo person data."""
        # Try direct phone
        if person.get("phone_number"):
            return person["phone_number"]

        # Try phone numbers array
        phones = person.get("phone_numbers", [])
        if phones:
            # Prefer direct dial
            for phone in phones:
                if phone.get("type") == "direct":
                    return phone.get("number")
            # Fall back to any phone
            return phones[0].get("number")

        return None

    def enrich_company(self, domain: str) -> CompanyInfo | None:
        """
        Get company information by domain.

        Args:
            domain: Company domain

        Returns:
            CompanyInfo object or None
        """
        if not self.is_configured or not domain:
            return None

        result = self._api_request("organizations/enrich", {
            "domain": domain,
        })

        if not result or "organization" not in result:
            return None

        self._credits_used += 1
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

        Args:
            advertiser: Advertiser dict from scraper
            max_contacts: Max contacts to find

        Returns:
            Enriched advertiser dict with contact fields added
        """
        domain = advertiser.get("advertiser_domain")

        if not domain:
            # Add empty contact fields
            return self._add_empty_contact_fields(advertiser, max_contacts)

        # Get contacts
        contacts = self.search_contacts(domain, max_contacts)

        # Add contacts to advertiser
        enriched = advertiser.copy()

        for i in range(max_contacts):
            prefix = "primary" if i == 0 else f"backup_{i}"

            if i < len(contacts):
                contact = contacts[i]
                enriched[f"{prefix}_contact"] = contact.name
                enriched[f"{prefix}_email"] = contact.email
                enriched[f"{prefix}_title"] = contact.title
                enriched[f"{prefix}_phone"] = contact.phone
                enriched[f"{prefix}_linkedin"] = contact.linkedin_url
            else:
                enriched[f"{prefix}_contact"] = None
                enriched[f"{prefix}_email"] = None
                enriched[f"{prefix}_title"] = None
                enriched[f"{prefix}_phone"] = None
                enriched[f"{prefix}_linkedin"] = None

        # Get company info
        company = self.enrich_company(domain)
        if company:
            enriched["company_website"] = company.website_url
            enriched["company_linkedin"] = company.linkedin_url
            enriched["company_industry"] = company.industry
            enriched["company_size"] = company.employee_count
            enriched["company_description"] = company.description

        enriched["enriched"] = True
        enriched["contacts_found"] = len(contacts)

        return enriched

    def _add_empty_contact_fields(self, advertiser: dict, max_contacts: int) -> dict:
        """Add empty contact fields to advertiser."""
        enriched = advertiser.copy()

        for i in range(max_contacts):
            prefix = "primary" if i == 0 else f"backup_{i}"
            enriched[f"{prefix}_contact"] = None
            enriched[f"{prefix}_email"] = None
            enriched[f"{prefix}_title"] = None
            enriched[f"{prefix}_phone"] = None
            enriched[f"{prefix}_linkedin"] = None

        enriched["company_website"] = f"https://{advertiser.get('advertiser_domain', '')}" if advertiser.get('advertiser_domain') else None
        enriched["company_linkedin"] = None
        enriched["company_industry"] = None
        enriched["company_size"] = None
        enriched["company_description"] = None
        enriched["enriched"] = False
        enriched["contacts_found"] = 0

        return enriched

    def bulk_enrich(
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

        logger.info(f"Enriched {total} advertisers, used {self._credits_used} API credits")
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
   - Each contact lookup uses ~1 credit
   - Enough for ~200 companies with 3 contacts each

The enrichment will automatically find:
- Primary contact (marketing/ad buyer)
- 2 backup contacts
- Company info (size, industry, LinkedIn)
"""
