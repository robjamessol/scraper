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

# Import HTML cleaning and few-shot examples from website_scraper
from .website_scraper import html_to_clean_text, EMAIL_CLASSIFICATION_EXAMPLES

logger = logging.getLogger(__name__)


def extract_json_from_response(text: str) -> dict | list | None:
    """
    Extract JSON from Claude's response, handling extra text before/after.

    Claude sometimes adds explanation text before or after the JSON.
    This function finds and extracts the JSON portion.
    """
    if not text:
        return None

    text = text.strip()

    # Try direct parsing first (fastest path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find JSON object (starts with { ends with })
    obj_start = text.find('{')
    if obj_start != -1:
        # Find matching closing brace
        depth = 0
        for i, char in enumerate(text[obj_start:], obj_start):
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[obj_start:i+1])
                    except json.JSONDecodeError:
                        break

    # Find JSON array (starts with [ ends with ])
    arr_start = text.find('[')
    if arr_start != -1:
        depth = 0
        for i, char in enumerate(text[arr_start:], arr_start):
            if char == '[':
                depth += 1
            elif char == ']':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[arr_start:i+1])
                    except json.JSONDecodeError:
                        break

    return None


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

    Model selection:
    - Haiku: Fast & cheap - used for navigation, link finding, simple tasks
    - Sonnet: Balanced - used for contact extraction, complex analysis
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    # Use Haiku by default (cheapest & fastest) - good for navigation tasks
    MODEL_HAIKU = "claude-3-5-haiku-20241022"
    MODEL_SONNET = "claude-sonnet-4-20250514"
    DEFAULT_MODEL = MODEL_HAIKU  # Use Haiku for speed/cost

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
            model: Model to use. Defaults to claude-3-5-haiku (fastest/cheapest).
            log_callback: Optional callback for logging.
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or self.DEFAULT_MODEL
        self._log_callback = log_callback

        if not self.api_key:
            logger.warning("Claude API key not configured. AI extraction disabled.")

        self.client = httpx.Client(timeout=12.0)  # Reduced for speed
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

    def analyze_website_navigation(
        self,
        html: str,
        domain: str,
        goal: str = "find advertising/marketing contact information",
    ) -> dict | None:
        """
        Use Claude to analyze a website and suggest navigation paths like a human would.

        This is the "click through" intelligence - Claude identifies the best paths
        to find contacts, including nested navigation (e.g., About Us → Press & Media).

        Args:
            html: Page HTML content
            domain: Domain being scraped
            goal: What we're looking for

        Returns:
            Dict with prioritized navigation paths and tips
        """
        if not self.is_configured or not html:
            return None

        # Expert web navigator prompt (inspired by user's suggestion)
        system_prompt = """You are an expert web navigator specializing in finding business contact information.

Your task: Analyze the HTML and suggest the BEST navigation paths to find contact details, prioritizing:
1. Advertising/media buying contacts (ads@, advertising@, media@)
2. Press/media relations (press@, pr@, communications@)
3. Partnerships/sponsorships (partnerships@, sponsors@)
4. General business inboxes (info@, business@, hello@)

**Common patterns to look for:**
- Direct: /contact, /contact-us, /advertise, /advertising, /media-kit, /partnerships
- Nested: /about → /about/press, /company → /company/contact, /about-us → /press-media
- Footer links: Often contain Contact, Press, Media Kit links
- Subdomains: newsroom.domain.com, press.domain.com, ads.domain.com

**Respond ONLY with valid JSON:**
{
    "priorities": {
        "advertising_media": ["/advertise", "/media-kit", "/partnerships"],
        "press_communications": ["/press", "/newsroom", "/about/media"],
        "general_contact": ["/contact", "/about-us", "/team"]
    },
    "suggested_paths": ["/advertise", "/about/press", "/contact"],
    "nested_navigation": [
        {"parent": "/about", "likely_children": ["/about/press", "/about/team", "/about/contact"]}
    ],
    "navigation_notes": "Site has mega menu with nested About section containing Press & Media link",
    "subdomains_to_check": ["newsroom", "press", "ads"],
    "has_email_visible": false,
    "has_contact_form": true
}

Return maximum 10 paths in suggested_paths, ordered by priority.
Only suggest paths that appear to exist based on the HTML (links, navigation, sitemap)."""

        # Extract key navigation areas (header, nav, footer, sitemap links)
        html_sample = html[:10000]

        user_prompt = f"""Analyze {domain} to find: {goal}

HTML content:
{html_sample}

Identify the best navigation paths to find advertising/partnership contacts.
Look for nested navigation (e.g., About Us containing Press & Media submenu)."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=500)

        if not response:
            return None

        result = extract_json_from_response(response)
        if result is None:
            self._log(f"Failed to parse navigation analysis JSON", "warning")
        return result

    def extract_contacts_from_page(
        self,
        html: str,
        page_url: str,
        company_name: str,
    ) -> list[dict]:
        """
        Use Claude to extract ALL contact information from a page.

        Optimized for finding advertising/marketing contacts with:
        - Email priority hierarchy for ad sales outreach
        - Obfuscated email detection
        - Name/title extraction
        - Confidence scoring

        Args:
            html: Page HTML content
            page_url: URL of the page
            company_name: Company name for context

        Returns:
            List of contact dicts with email, name, title, type, confidence
        """
        if not self.is_configured or not html:
            return []

        system_prompt = """You are an expert at identifying contact emails for advertising and sponsorship outreach.

**Your Task:**
1. Identify ALL email addresses in the content (including obfuscated ones)
2. Classify each email by its likely purpose
3. Rank them by relevance for ad sales outreach
4. Provide a confidence score for each

**Email Priority Hierarchy (most to least preferred):**
1. Advertising/Ad Sales (ads@, advertising@, adsales@, media@, mediakit@)
2. Partnerships/Sponsorships (partnerships@, sponsors@, sponsorship@)
3. Business Development (bizdev@, business@, opportunities@, bd@)
4. Marketing (marketing@, press@, pr@, communications@)
5. General inquiries (info@, hello@, contact@)
6. Named individuals in relevant roles (Ad Sales Manager, Partnership Director, VP Marketing, Head of BD)

**Exclude these (do not return):**
- Customer support (support@, help@, customerservice@)
- Technical (tech@, engineering@, dev@)
- HR/Careers (jobs@, careers@, hr@, recruiting@)
- Legal (legal@, compliance@)
- Unsubscribe/noreply addresses

**Handle obfuscated emails:**
- "name [at] domain [dot] com" → name@domain.com
- "name(at)domain.com" → name@domain.com
- HTML entities (&#64; for @)
- JavaScript concatenation

Respond ONLY with valid JSON:
{
    "contacts": [
        {
            "email": "email@domain.com",
            "name": "Person Name or null",
            "title": "Job Title or null",
            "type": "advertising|partnerships|business|marketing|general|personal",
            "confidence": "high|medium|low",
            "source_context": "brief note about where found"
        }
    ],
    "advertise_page_url": "URL if /advertise or media kit page found, else null",
    "contact_form_url": "URL if contact form found for advertising, else null"
}

Return {"contacts": []} if no suitable emails found. DO NOT make up emails.

**Few-Shot Classification Examples:**
{EMAIL_CLASSIFICATION_EXAMPLES}"""

        # Convert HTML to clean text (removes scripts, styles, reduces tokens significantly)
        # This typically reduces 20KB of HTML to ~3-5KB of relevant text
        clean_text = html_to_clean_text(html, preserve_links=True)

        # If clean text extraction failed or is too short, fall back to raw HTML
        if not clean_text or len(clean_text) < 100:
            html_sample = html[:20000]
        else:
            # Use clean text (much more efficient) - can include more content
            html_sample = clean_text[:15000]

        user_prompt = f"""Analyze this page from {company_name} and extract contact information for ad sales outreach:

**Source URL:** {page_url}

**Page Content:**
{html_sample}

Find every relevant email, especially advertising/partnership contacts."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=1200)

        if not response:
            return []

        try:
            data = json.loads(response)
            contacts = data.get("contacts", [])
            if isinstance(contacts, list):
                # Add any page-level info to first contact as notes
                if contacts and data.get("advertise_page_url"):
                    contacts[0]["advertise_page"] = data["advertise_page_url"]
                return contacts
            return []
        except json.JSONDecodeError:
            return []

    def select_best_urls_from_list(
        self,
        urls: list[str],
        domain: str,
        goal: str = "find advertising/marketing contact information",
    ) -> list[str]:
        """
        Given a list of REAL URLs found on a page, select the best ones for contact discovery.

        This is more accurate than guessing paths - Claude sees actual links that exist.

        Args:
            urls: List of real URLs extracted from the page
            domain: Domain being scraped
            goal: What we're looking for

        Returns:
            Prioritized list of URLs to visit (max 5)
        """
        if not self.is_configured or not urls:
            return []

        # Limit input to avoid token bloat
        urls = urls[:30]

        system_prompt = """You are an expert at finding business contact information on websites.

Given a list of REAL URLs from a website, select the TOP 5 most likely to have advertising/partnership contact info.

**Priority order (most to least valuable):**
1. Advertising/media pages (/advertise, /media-kit, /advertising, /partnerships)
2. Press/PR/newsroom (/press, /newsroom, /media, /news, /media-relations)
3. Contact pages (/contact, /contact-us, /get-in-touch)
4. About/team pages (/about, /team, /leadership, /about-us)
5. Company info (/company, /corporate)

**Skip these (low value for contact finding):**
- Blog/article pages (/blog/, /news/2024, /article/)
- Product pages (/products, /solutions, /features, /pricing)
- Career/job pages (/careers, /jobs, /hiring)
- Legal pages (/privacy, /terms, /legal)
- Login/auth pages (/login, /signin, /register)
- Support pages (/help, /support, /faq)

Respond ONLY with a JSON array of the best URLs (maximum 5):
["/advertise", "/about/press", "/contact-us"]

Return paths only (not full URLs). Order by priority."""

        urls_text = "\n".join(f"- {url}" for url in urls)

        user_prompt = f"""Select the best URLs for finding contacts on {domain}:

{urls_text}

Goal: {goal}

Return the top 5 URLs most likely to have advertising/partnership contact information."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=300)

        if not response:
            return []

        result = extract_json_from_response(response)
        if isinstance(result, list):
            # Return the selected URLs (Claude returns paths, we may need to resolve)
            from urllib.parse import urljoin
            base = f"https://{domain}"
            return [urljoin(base, p) if not p.startswith("http") else p for p in result[:5]]
        return []

    def identify_best_contact_links(
        self,
        html: str,
        base_url: str,
        already_visited: list[str] | None = None,
    ) -> list[str]:
        """
        Use Claude to identify which links on a page are most likely to lead to contact info.

        Smarter than keyword matching - understands context and link text.

        Args:
            html: Page HTML
            base_url: Base URL for resolving relative links
            already_visited: URLs already visited (to avoid duplicates)

        Returns:
            List of URLs to visit, in priority order
        """
        if not self.is_configured or not html:
            return []

        already_visited = already_visited or []

        system_prompt = """Analyze this webpage and identify links most likely to lead to contact information.

Respond ONLY with a JSON array of paths/URLs in priority order:
["/advertise", "/team", "/contact-us"]

Prioritize:
1. Advertising/partnership/media pages (most valuable for ad sales)
2. Team/leadership/about pages (often list people with emails)
3. Contact pages
4. Press/media pages (often have media contact emails)

Return maximum 8 URLs. Only return paths that exist in the HTML."""

        html_sample = html[:12000]
        visited_note = f"\n\nAlready visited (skip these): {already_visited[:10]}" if already_visited else ""

        user_prompt = f"""Find the best links to contact information on this page ({base_url}):

{html_sample}{visited_note}

Return JSON array of paths to visit."""

        response = self._call_api(system_prompt, user_prompt, max_tokens=300)

        if not response:
            return []

        paths = extract_json_from_response(response)
        if isinstance(paths, list):
            from urllib.parse import urljoin
            return [urljoin(base_url, p) for p in paths if isinstance(p, str)]
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
