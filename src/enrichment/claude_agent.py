"""
Claude AI Agent for Intelligent Ad Content Extraction and Enrichment.

Uses Claude API for tasks that benefit from AI understanding:
1. Ad copy analysis - Extract headline, product, CTA from messy HTML text
2. Company enrichment - Summarize what a company does when Apollo fails
3. Niche fit scoring - Determine advertiser relevance to target audience
4. Contact name validation - Identify if a "name" is actually a real person

This provides significant quality improvements over regex-based extraction.
"""

import os
import logging
import json
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AdAnalysis:
    """Structured analysis of ad copy."""
    headline: str | None
    product_service: str | None
    call_to_action: str | None
    value_proposition: str | None
    target_audience: str | None
    confidence: float  # 0-1 confidence in extraction


@dataclass
class CompanyAnalysis:
    """Structured analysis of a company."""
    description: str | None
    industry: str | None
    products_services: list[str]
    target_market: str | None
    company_size_estimate: str | None


@dataclass
class NicheFitAnalysis:
    """Analysis of how well an advertiser fits a target niche."""
    fit_score: str  # "High", "Medium", "Low"
    fit_emoji: str  # "🟢", "🟡", "🔴"
    reasoning: str
    category: str
    confidence: float


class ClaudeAgent:
    """
    Claude-powered agent for intelligent content extraction and analysis.

    Uses Claude API for tasks where AI understanding provides significant
    value over rule-based extraction.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-sonnet-4-20250514"  # Fast and capable

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        log_callback: callable = None,
    ):
        """
        Initialize Claude agent.

        Args:
            api_key: Anthropic API key. If not provided, reads from ANTHROPIC_API_KEY env var.
            model: Model to use. Defaults to claude-sonnet-4-20250514.
            log_callback: Optional callback for logging.
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or self.DEFAULT_MODEL
        self._log_callback = log_callback

        if not self.api_key:
            logger.warning("Claude API key not configured. AI extraction disabled.")

        self.client = httpx.Client(timeout=30.0)
        self._request_count = 0
        self._total_tokens = 0

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

    @property
    def is_configured(self) -> bool:
        """Check if Claude API is configured."""
        return bool(self.api_key)

    def get_stats(self) -> dict:
        """Get usage statistics."""
        return {
            "requests": self._request_count,
            "total_tokens": self._total_tokens,
        }

    def _call_api(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1024,
    ) -> str | None:
        """
        Make a call to Claude API.

        Args:
            system_prompt: System prompt defining Claude's role
            user_prompt: User message with the task
            max_tokens: Maximum tokens in response

        Returns:
            Claude's response text or None on error
        """
        if not self.is_configured:
            return None

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt}
            ],
        }

        try:
            response = self.client.post(self.API_URL, json=payload, headers=headers)

            if response.status_code != 200:
                self._log(f"Claude API error: {response.status_code} - {response.text[:200]}", "error")
                return None

            result = response.json()
            self._request_count += 1

            # Track token usage
            usage = result.get("usage", {})
            self._total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            # Extract text from response
            content = result.get("content", [])
            if content and content[0].get("type") == "text":
                return content[0].get("text", "")

            return None

        except Exception as e:
            self._log(f"Claude API request failed: {e}", "error")
            return None

    def analyze_ad_copy(self, ad_copy: str, company_name: str) -> AdAnalysis | None:
        """
        Use Claude to intelligently extract ad components from messy text.

        This is much more accurate than regex for:
        - Understanding context to find the real headline
        - Identifying what product/service is being advertised
        - Finding CTAs even when phrased unusually

        Args:
            ad_copy: Raw ad copy text (may include noise)
            company_name: Company name for context

        Returns:
            AdAnalysis with extracted components
        """
        if not self.is_configured or not ad_copy:
            return None

        system_prompt = """You are an expert at analyzing advertising copy. Extract structured information from newsletter ads.

Respond ONLY with valid JSON in this exact format:
{
    "headline": "main headline or hook (first attention-grabbing statement)",
    "product_service": "what they're selling/offering (be specific)",
    "call_to_action": "the CTA text (e.g., 'Learn More', 'Get Started')",
    "value_proposition": "main benefit promised",
    "target_audience": "who this ad targets",
    "confidence": 0.8
}

Use null for any field you cannot determine. Confidence should be 0-1."""

        user_prompt = f"""Analyze this ad from {company_name}:

---
{ad_copy[:2000]}
---

Extract the headline, product/service, CTA, value proposition, and target audience."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=500)

        if not response:
            return None

        try:
            # Parse JSON response
            data = json.loads(response)
            return AdAnalysis(
                headline=data.get("headline"),
                product_service=data.get("product_service"),
                call_to_action=data.get("call_to_action"),
                value_proposition=data.get("value_proposition"),
                target_audience=data.get("target_audience"),
                confidence=float(data.get("confidence", 0.5)),
            )
        except (json.JSONDecodeError, ValueError) as e:
            self._log(f"Failed to parse Claude response: {e}", "warning")
            return None

    def analyze_company(
        self,
        company_name: str,
        domain: str,
        website_text: str | None = None,
    ) -> CompanyAnalysis | None:
        """
        Use Claude to understand what a company does.

        Useful as a fallback when Apollo doesn't have company data.

        Args:
            company_name: Company name
            domain: Company domain
            website_text: Optional scraped website text for analysis

        Returns:
            CompanyAnalysis with company information
        """
        if not self.is_configured:
            return None

        system_prompt = """You are a business analyst. Analyze company information and provide a structured summary.

Respond ONLY with valid JSON in this exact format:
{
    "description": "2-3 sentence company description",
    "industry": "primary industry (e.g., 'healthcare technology', 'financial services')",
    "products_services": ["list", "of", "main", "offerings"],
    "target_market": "who they sell to",
    "company_size_estimate": "startup/small/medium/large/enterprise"
}

Use null for fields you cannot determine. Base analysis on provided information only."""

        context = f"Company: {company_name}\nDomain: {domain}"
        if website_text:
            context += f"\n\nWebsite content:\n{website_text[:3000]}"

        user_prompt = f"""Analyze this company and extract key information:

{context}

Provide a structured analysis."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=600)

        if not response:
            return None

        try:
            data = json.loads(response)
            return CompanyAnalysis(
                description=data.get("description"),
                industry=data.get("industry"),
                products_services=data.get("products_services", []),
                target_market=data.get("target_market"),
                company_size_estimate=data.get("company_size_estimate"),
            )
        except (json.JSONDecodeError, ValueError) as e:
            self._log(f"Failed to parse company analysis: {e}", "warning")
            return None

    def analyze_niche_fit(
        self,
        company_name: str,
        ad_copy: str,
        company_description: str | None,
        target_niche: str,
    ) -> NicheFitAnalysis | None:
        """
        Use Claude to intelligently score how well an advertiser fits a target niche.

        Much more nuanced than keyword matching - understands context and intent.

        Args:
            company_name: Advertiser name
            ad_copy: Ad copy text
            company_description: Company description if available
            target_niche: Description of target audience/niche

        Returns:
            NicheFitAnalysis with fit assessment
        """
        if not self.is_configured:
            return None

        system_prompt = """You are a media buyer analyzing advertiser fit for a newsletter audience.

Respond ONLY with valid JSON in this exact format:
{
    "fit_score": "High" or "Medium" or "Low",
    "reasoning": "1-2 sentence explanation",
    "category": "most specific category (e.g., 'supplement', 'health tech', 'financial services')",
    "confidence": 0.8
}

High = directly relevant, audience would be interested
Medium = tangentially relevant or some audience overlap
Low = not relevant to this audience"""

        context = f"""Company: {company_name}

Ad Copy:
{ad_copy[:1500] if ad_copy else 'No ad copy available'}

Company Description:
{company_description[:500] if company_description else 'No description available'}

Target Audience/Niche:
{target_niche}"""

        user_prompt = f"""Analyze how well this advertiser fits the target audience:

{context}

Rate the fit and explain your reasoning."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=300)

        if not response:
            return None

        try:
            data = json.loads(response)
            fit_score = data.get("fit_score", "Medium")

            # Map fit score to emoji
            emoji_map = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}
            fit_emoji = emoji_map.get(fit_score, "🟡")

            return NicheFitAnalysis(
                fit_score=fit_score,
                fit_emoji=fit_emoji,
                reasoning=data.get("reasoning", ""),
                category=data.get("category", "other"),
                confidence=float(data.get("confidence", 0.5)),
            )
        except (json.JSONDecodeError, ValueError) as e:
            self._log(f"Failed to parse niche fit analysis: {e}", "warning")
            return None

    def validate_contact_name(self, name: str, context: str | None = None) -> bool:
        """
        Use Claude to determine if a string is likely a real person's name.

        More accurate than heuristics for edge cases like:
        - Foreign names
        - Unusual spellings
        - Names that look like words (e.g., "Summer", "Hunter")

        Args:
            name: Potential name to validate
            context: Optional context (e.g., where the name was found)

        Returns:
            True if likely a real name, False otherwise
        """
        if not self.is_configured or not name:
            return False

        # Quick heuristic check first (save API calls)
        obvious_fakes = {
            "advertising", "ads", "info", "contact", "sales",
            "marketing", "support", "hello", "team", "press",
        }
        if name.lower().strip() in obvious_fakes:
            return False

        system_prompt = """Determine if the given text is a real person's name.

Respond with ONLY "true" or "false".

true = This appears to be a real person's name (first name, full name, etc.)
false = This is NOT a name (email prefix, department, generic term, gibberish)"""

        user_prompt = f"Is this a real person's name? \"{name}\""
        if context:
            user_prompt += f"\n\nContext: {context}"

        response = self._call_api(system_prompt, user_prompt, max_tokens=10)

        if response:
            return response.strip().lower() == "true"
        return False

    def extract_contacts_from_text(self, text: str, company_name: str) -> list[dict]:
        """
        Use Claude to extract contact information from unstructured text.

        Useful for parsing messy "Contact Us" or "About" pages.

        Args:
            text: Raw text from website
            company_name: Company name for context

        Returns:
            List of contact dicts with name, title, email, phone
        """
        if not self.is_configured or not text:
            return []

        system_prompt = """Extract contact information from text. Find real people with their roles.

Respond ONLY with valid JSON array:
[
    {
        "name": "Full Name",
        "title": "Job Title",
        "email": "email@example.com or null",
        "phone": "phone number or null"
    }
]

Only include entries where you found a real person's name. Return [] if no contacts found."""

        user_prompt = f"""Extract contact information for {company_name} from this text:

{text[:4000]}

Return structured contact data."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=800)

        if not response:
            return []

        try:
            contacts = json.loads(response)
            if isinstance(contacts, list):
                return contacts
            return []
        except json.JSONDecodeError:
            return []

    def suggest_email_prefixes(
        self,
        company_name: str,
        ad_copy: str | None,
        industry: str | None,
    ) -> list[str] | None:
        """
        Suggest the best email prefixes for reaching ad/marketing contacts.

        Claude analyzes the company type and suggests which email addresses
        are most likely to exist and reach decision makers.

        Args:
            company_name: Company name
            ad_copy: Ad copy for context
            industry: Company industry if known

        Returns:
            List of suggested email prefixes in priority order
        """
        if not self.is_configured:
            return None

        system_prompt = """You are an expert at B2B outreach. Given a company, suggest the best email prefixes to reach their advertising/marketing decision makers.

Respond ONLY with a JSON array of email prefixes (just the part before @), in priority order.
Maximum 6 prefixes. Consider:
- Company size (startups often use hello@, enterprises use formal prefixes)
- Industry norms (tech companies: hello@, growth@; traditional: info@, contact@)
- Who handles ad buying (marketing@, partnerships@, advertising@)

Example response: ["partnerships", "marketing", "advertising", "hello", "info", "contact"]"""

        context = f"Company: {company_name}"
        if industry:
            context += f"\nIndustry: {industry}"
        if ad_copy:
            context += f"\nAd context: {ad_copy[:500]}"

        user_prompt = f"""Suggest the best email prefixes to reach ad/marketing contacts at this company:

{context}

Return a JSON array of prefixes."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=150)

        if not response:
            return None

        try:
            prefixes = json.loads(response)
            if isinstance(prefixes, list):
                # Clean up prefixes
                return [p.lower().strip() for p in prefixes if isinstance(p, str)]
            return None
        except json.JSONDecodeError:
            return None

    def extract_emails_from_text(self, text: str, company_name: str) -> list[dict]:
        """
        Use Claude to extract and identify emails from messy website text.

        More accurate than regex for:
        - Obfuscated emails (name [at] domain [dot] com)
        - Emails hidden in JavaScript
        - Identifying which email is for what purpose

        Args:
            text: Raw text from website
            company_name: Company name for context

        Returns:
            List of dicts with email, name, title, purpose
        """
        if not self.is_configured or not text:
            return []

        system_prompt = """Extract email addresses from text. Identify the purpose of each email.

Respond ONLY with valid JSON array:
[
    {
        "email": "email@domain.com",
        "name": "Person Name or null",
        "title": "Job Title or null",
        "purpose": "advertising|sales|support|general|press|careers"
    }
]

Handle obfuscated emails like "name [at] domain [dot] com" or "name(at)domain.com".
Return [] if no emails found."""

        user_prompt = f"""Extract emails for {company_name} from this text:

{text[:5000]}

Return structured email data."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=600)

        if not response:
            return []

        try:
            emails = json.loads(response)
            if isinstance(emails, list):
                return emails
            return []
        except json.JSONDecodeError:
            return []

    def prioritize_contacts(
        self,
        contacts: list[dict],
        company_name: str,
        goal: str = "newsletter advertising",
    ) -> list[dict]:
        """
        Use Claude to rank contacts by likelihood to handle ad buying.

        Args:
            contacts: List of contact dicts with name, email, title
            company_name: Company name
            goal: What we're trying to sell

        Returns:
            Contacts sorted by priority with reasoning
        """
        if not self.is_configured or not contacts:
            return contacts

        system_prompt = """You are a sales expert. Rank these contacts by who is most likely to handle the purchasing decision for the stated goal.

Respond ONLY with valid JSON array of contact emails in priority order:
["best@example.com", "second@example.com", ...]

Consider:
- Job titles that indicate ad buying authority
- Marketing/partnerships roles over general roles
- Seniority (VP/Director over Manager over generic)"""

        contacts_str = json.dumps(contacts[:10], indent=2)  # Limit to 10

        user_prompt = f"""Rank these contacts at {company_name} for outreach about {goal}:

{contacts_str}

Return emails in priority order."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=300)

        if not response:
            return contacts

        try:
            priority_emails = json.loads(response)
            if isinstance(priority_emails, list):
                # Reorder contacts based on Claude's ranking
                email_to_contact = {c.get("email", "").lower(): c for c in contacts}
                sorted_contacts = []
                for email in priority_emails:
                    email_lower = email.lower()
                    if email_lower in email_to_contact:
                        sorted_contacts.append(email_to_contact[email_lower])
                        del email_to_contact[email_lower]
                # Add any contacts Claude didn't rank
                sorted_contacts.extend(email_to_contact.values())
                return sorted_contacts
            return contacts
        except json.JSONDecodeError:
            return contacts

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Convenience function for quick analysis
def analyze_ad_with_claude(
    ad_copy: str,
    company_name: str,
    api_key: str | None = None,
) -> AdAnalysis | None:
    """
    Quick function to analyze ad copy with Claude.

    Args:
        ad_copy: Ad copy text
        company_name: Company name
        api_key: Optional API key

    Returns:
        AdAnalysis or None
    """
    agent = ClaudeAgent(api_key=api_key)
    try:
        return agent.analyze_ad_copy(ad_copy, company_name)
    finally:
        agent.close()
