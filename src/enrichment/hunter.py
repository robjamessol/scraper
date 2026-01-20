"""Hunter.io API integration for finding emails by domain.

Hunter.io is specifically designed to find email addresses for any company domain.
It aggregates publicly available emails from the web.

Free tier: 25 searches/month
API docs: https://hunter.io/api-documentation
"""

import os
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class HunterContact:
    """Contact found via Hunter.io."""
    email: str
    first_name: str | None = None
    last_name: str | None = None
    position: str | None = None
    department: str | None = None
    linkedin: str | None = None
    phone_number: str | None = None
    confidence: int = 0  # 0-100 confidence score
    source: str = "hunter.io"


class HunterEnricher:
    """Find email addresses using Hunter.io API."""

    BASE_URL = "https://api.hunter.io/v2"

    def __init__(
        self,
        api_key: str | None = None,
        log_callback: callable = None,
    ):
        """
        Initialize Hunter.io client.

        Args:
            api_key: Hunter.io API key (or set HUNTER_API_KEY env var)
            log_callback: Optional callback for live logging
        """
        self.api_key = api_key or os.environ.get("HUNTER_API_KEY")
        self._log_callback = log_callback

    @property
    def is_configured(self) -> bool:
        """Check if Hunter.io is configured with an API key."""
        return bool(self.api_key)

    def _log(self, message: str, level: str = "info"):
        """Log a message."""
        if self._log_callback:
            self._log_callback(message, level)
        logger.info(message) if level == "info" else logger.warning(message)

    def domain_search(
        self,
        domain: str,
        limit: int = 10,
        department: str | None = None,
    ) -> list[HunterContact]:
        """
        Search for emails at a domain.

        Args:
            domain: Company domain (e.g., "healthedge.com")
            limit: Max results to return (max 100)
            department: Filter by department (executive, marketing, sales, etc.)

        Returns:
            List of HunterContact objects
        """
        if not self.is_configured:
            self._log("Hunter.io not configured (no API key)", "warning")
            return []

        # Clean domain
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]

        self._log(f"Hunter.io: Searching for emails at {domain}...")

        params = {
            "domain": domain,
            "api_key": self.api_key,
            "limit": min(limit, 100),
        }
        if department:
            params["department"] = department

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{self.BASE_URL}/domain-search", params=params)

                if response.status_code == 401:
                    self._log("Hunter.io: Invalid API key", "error")
                    return []
                elif response.status_code == 429:
                    self._log("Hunter.io: Rate limit exceeded", "warning")
                    return []
                elif response.status_code != 200:
                    self._log(f"Hunter.io: API error {response.status_code}", "warning")
                    return []

                data = response.json()

                # Check for errors in response
                if "errors" in data:
                    self._log(f"Hunter.io error: {data['errors']}", "warning")
                    return []

                emails = data.get("data", {}).get("emails", [])
                contacts = []

                for email_data in emails:
                    contact = HunterContact(
                        email=email_data.get("value", ""),
                        first_name=email_data.get("first_name"),
                        last_name=email_data.get("last_name"),
                        position=email_data.get("position"),
                        department=email_data.get("department"),
                        linkedin=email_data.get("linkedin"),
                        phone_number=email_data.get("phone_number"),
                        confidence=email_data.get("confidence", 0),
                    )
                    if contact.email:
                        contacts.append(contact)

                self._log(f"Hunter.io: Found {len(contacts)} emails at {domain}")
                return contacts

        except httpx.TimeoutException:
            self._log(f"Hunter.io: Timeout searching {domain}", "warning")
            return []
        except Exception as e:
            self._log(f"Hunter.io error: {str(e)}", "error")
            return []

    def find_email(
        self,
        domain: str,
        first_name: str,
        last_name: str,
    ) -> HunterContact | None:
        """
        Find a specific person's email at a domain.

        Args:
            domain: Company domain
            first_name: Person's first name
            last_name: Person's last name

        Returns:
            HunterContact if found, None otherwise
        """
        if not self.is_configured:
            return None

        params = {
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
            "api_key": self.api_key,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{self.BASE_URL}/email-finder", params=params)

                if response.status_code != 200:
                    return None

                data = response.json().get("data", {})
                if data.get("email"):
                    return HunterContact(
                        email=data["email"],
                        first_name=first_name,
                        last_name=last_name,
                        position=data.get("position"),
                        confidence=data.get("score", 0),
                    )
                return None

        except Exception:
            return None

    def get_account_info(self) -> dict | None:
        """Get Hunter.io account info including remaining credits."""
        if not self.is_configured:
            return None

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(
                    f"{self.BASE_URL}/account",
                    params={"api_key": self.api_key}
                )
                if response.status_code == 200:
                    return response.json().get("data", {})
                return None
        except Exception:
            return None


def find_emails_for_domain(
    domain: str,
    api_key: str | None = None,
    limit: int = 5,
) -> list[HunterContact]:
    """
    Convenience function to find emails for a domain using Hunter.io.

    Args:
        domain: Company domain
        api_key: Hunter.io API key (optional, uses env var if not provided)
        limit: Max results

    Returns:
        List of HunterContact objects
    """
    hunter = HunterEnricher(api_key=api_key)
    return hunter.domain_search(domain, limit=limit)
