"""Email Finder - Find and verify emails for any domain.

This replaces paid services like Hunter.io/Snov.io by:
1. Generating common business email patterns (info@, contact@, sales@, etc.)
2. Verifying emails via SMTP (checking if recipient exists without sending)
3. Detecting company email patterns from any known emails

Free and unlimited - no API keys needed!
"""

import re
import socket
import smtplib
import dns.resolver
import logging
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


@dataclass
class FoundEmail:
    """An email found/verified for a domain."""
    email: str
    email_type: str  # generic, advertising, personal
    verified: bool = False  # True if SMTP verified
    confidence: str = "medium"  # low, medium, high
    source: str = "pattern"  # pattern, website, smtp_verified


# Common business email prefixes, ordered by relevance for ad sales
ADVERTISING_PREFIXES = [
    "advertising", "ads", "ad", "adops", "adsales", "ad-sales",
    "partnerships", "partner", "partners", "sponsorship", "sponsors",
    "media", "mediasales", "mediakit",
]

SALES_MARKETING_PREFIXES = [
    "marketing", "sales", "business", "bd", "biz", "commercial",
    "growth", "revenue",
]

GENERAL_PREFIXES = [
    "hello", "contact", "info", "inquiries", "enquiries",
    "general", "team", "support",
]

PR_PREFIXES = [
    "press", "pr", "communications", "comms", "media", "news",
]

# All prefixes in priority order for ad sales outreach
ALL_PREFIXES = ADVERTISING_PREFIXES + SALES_MARKETING_PREFIXES + GENERAL_PREFIXES + PR_PREFIXES


class EmailFinder:
    """Find and verify emails for any domain - no API needed."""

    def __init__(
        self,
        verify_smtp: bool = True,
        timeout: float = 3.0,  # Reduced from 5s for speed
        max_workers: int = 5,
        log_callback: callable = None,
        priority_prefixes: list[str] | None = None,
    ):
        """
        Initialize the email finder.

        Args:
            verify_smtp: Whether to verify emails via SMTP
            timeout: Socket timeout for SMTP checks
            max_workers: Max concurrent SMTP verification threads
            log_callback: Optional callback for live logging
            priority_prefixes: Optional list of prefixes to try first (e.g., from Claude)
        """
        self.verify_smtp = verify_smtp
        self.timeout = timeout
        self.max_workers = max_workers
        self._log_callback = log_callback
        self._mx_cache: dict[str, list[str]] = {}
        self._priority_prefixes = priority_prefixes or []

    def _log(self, message: str, level: str = "info"):
        """Log a message."""
        if self._log_callback:
            self._log_callback(message, level)
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _get_mx_records(self, domain: str) -> list[str]:
        """Get MX records for a domain (cached)."""
        if domain in self._mx_cache:
            return self._mx_cache[domain]

        try:
            mx_records = dns.resolver.resolve(domain, 'MX')
            # Sort by priority (lower is better)
            sorted_mx = sorted(mx_records, key=lambda x: x.preference)
            mx_hosts = [str(mx.exchange).rstrip('.') for mx in sorted_mx]
            self._mx_cache[domain] = mx_hosts
            return mx_hosts
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            self._mx_cache[domain] = []
            return []
        except Exception as e:
            self._log(f"MX lookup failed for {domain}: {e}", "warning")
            self._mx_cache[domain] = []
            return []

    def _verify_email_smtp(self, email: str) -> bool:
        """
        Verify if an email exists via SMTP.

        This checks if the mail server accepts the recipient without
        actually sending an email. Many servers support this.

        Returns True if verified, False if rejected or couldn't verify.
        """
        domain = email.split('@')[-1]
        mx_hosts = self._get_mx_records(domain)

        if not mx_hosts:
            return False

        # Try each MX server
        for mx_host in mx_hosts[:2]:  # Try top 2 MX servers
            try:
                # Connect to SMTP server
                smtp = smtplib.SMTP(timeout=self.timeout)
                smtp.connect(mx_host, 25)
                smtp.helo('verify.local')

                # Try MAIL FROM
                smtp.mail('verify@verify.local')

                # Check RCPT TO - this is where we find out if email exists
                code, _ = smtp.rcpt(email)
                smtp.quit()

                # 250 = OK, email exists
                # 251 = User not local, will forward
                # 550 = User not found
                # 552 = Mailbox full (but exists)
                if code in [250, 251, 552]:
                    return True
                elif code == 550:
                    return False

            except smtplib.SMTPServerDisconnected:
                # Server disconnected - might be blocking verification
                continue
            except smtplib.SMTPRecipientsRefused:
                # Email definitely doesn't exist
                return False
            except socket.timeout:
                continue
            except Exception as e:
                self._log(f"SMTP check failed for {email}: {e}", "warning")
                continue

        # Couldn't verify - return None to indicate unknown
        return False

    def generate_emails(self, domain: str) -> list[FoundEmail]:
        """
        Generate common business email patterns for a domain.

        If priority_prefixes are set (e.g., from Claude), those are tried first.

        Args:
            domain: Company domain (e.g., "healthedge.com")

        Returns:
            List of potential emails, prioritized for ad sales
        """
        # Clean domain
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]

        emails = []
        seen = set()

        # First, add priority prefixes (from Claude suggestions)
        for prefix in self._priority_prefixes:
            prefix = prefix.lower().strip()
            email = f"{prefix}@{domain}"
            if email not in seen:
                seen.add(email)
                # Determine email type
                if prefix in ADVERTISING_PREFIXES:
                    email_type = "advertising"
                elif prefix in SALES_MARKETING_PREFIXES:
                    email_type = "sales"
                elif prefix in PR_PREFIXES:
                    email_type = "pr"
                else:
                    email_type = "generic"

                emails.append(FoundEmail(
                    email=email,
                    email_type=email_type,
                    verified=False,
                    confidence="medium",  # Higher confidence - Claude suggested
                    source="claude_suggested",
                ))

        # Then add standard prefixes
        for prefix in ALL_PREFIXES:
            email = f"{prefix}@{domain}"
            if email not in seen:
                seen.add(email)
                # Determine email type
                if prefix in ADVERTISING_PREFIXES:
                    email_type = "advertising"
                elif prefix in SALES_MARKETING_PREFIXES:
                    email_type = "sales"
                elif prefix in PR_PREFIXES:
                    email_type = "pr"
                else:
                    email_type = "generic"

                emails.append(FoundEmail(
                    email=email,
                    email_type=email_type,
                    verified=False,
                    confidence="low",  # Unverified pattern
                    source="pattern",
                ))

        return emails

    def find_emails(
        self,
        domain: str,
        max_results: int = 5,
        verify: bool = None,
        max_verify: int = 8,  # Limit SMTP checks for speed
    ) -> list[FoundEmail]:
        """
        Find and optionally verify emails for a domain.

        Args:
            domain: Company domain
            max_results: Max emails to return
            verify: Whether to SMTP verify (defaults to self.verify_smtp)
            max_verify: Max emails to SMTP verify (for speed)

        Returns:
            List of FoundEmail objects, verified ones first
        """
        if verify is None:
            verify = self.verify_smtp

        # Clean domain
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]

        self._log(f"Finding emails for {domain}...")

        # Generate candidate emails
        candidates = self.generate_emails(domain)

        if not verify:
            # Return top candidates without verification
            return candidates[:max_results]

        # Check if domain has MX records
        mx_hosts = self._get_mx_records(domain)
        if not mx_hosts:
            self._log(f"No MX records for {domain}, skipping verification", "warning")
            return candidates[:max_results]

        # Only verify top candidates for speed (Claude suggestions + top standard prefixes)
        to_verify = candidates[:max_verify]
        remaining = candidates[max_verify:]

        self._log(f"Verifying top {len(to_verify)} email patterns via SMTP...")

        verified = []
        unverified = []

        # Verify emails in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit verification jobs for LIMITED candidates only
            future_to_email = {
                executor.submit(self._verify_email_smtp, e.email): e
                for e in to_verify
            }

            for future in as_completed(future_to_email):
                email_obj = future_to_email[future]
                try:
                    is_valid = future.result()
                    if is_valid:
                        email_obj.verified = True
                        email_obj.confidence = "high"
                        email_obj.source = "smtp_verified"
                        verified.append(email_obj)
                        self._log(f"Verified: {email_obj.email}")

                        # Stop early if we have enough verified emails
                        if len(verified) >= max_results:
                            break
                    else:
                        unverified.append(email_obj)
                except Exception as e:
                    unverified.append(email_obj)

        # Combine results: verified first, then unverified from checked, then remaining
        results = verified + unverified + remaining
        results = results[:max_results]

        verified_count = sum(1 for e in results if e.verified)
        self._log(f"Found {len(results)} emails ({verified_count} verified) for {domain}")

        return results

    def verify_email(self, email: str) -> bool:
        """
        Verify a single email address.

        Args:
            email: Email to verify

        Returns:
            True if verified, False otherwise
        """
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            return False

        return self._verify_email_smtp(email)


def find_emails_for_domain(
    domain: str,
    max_results: int = 5,
    verify: bool = True,
    log_callback: callable = None,
) -> list[FoundEmail]:
    """
    Convenience function to find emails for a domain.

    Args:
        domain: Company domain
        max_results: Max emails to return
        verify: Whether to SMTP verify
        log_callback: Optional logging callback

    Returns:
        List of FoundEmail objects
    """
    finder = EmailFinder(verify_smtp=verify, log_callback=log_callback)
    return finder.find_emails(domain, max_results=max_results)
