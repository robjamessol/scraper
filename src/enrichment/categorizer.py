"""Advertiser categorization and niche fit scoring."""

import re
import logging
from dataclasses import dataclass
from typing import Any

from ..utils.helpers import normalize_company_name
from ..utils.config import load_config


logger = logging.getLogger(__name__)


@dataclass
class CategoryScore:
    """Category scoring result."""
    category: str
    confidence: float  # 0.0 to 1.0
    matched_keywords: list[str]


@dataclass
class NicheFitScore:
    """Niche fit scoring result for Renewal Weekly audience."""
    score: str  # "High", "Medium", "Low"
    emoji: str  # Unicode emoji
    display: str  # Combined display (e.g., "🟢 High")
    reason: str  # Why this score was assigned
    confidence: float


class AdvertiserCategorizer:
    """
    Categorizes advertisers and scores their fit for Renewal Weekly.

    Renewal Weekly audience:
    - Ages 55-68
    - $150K+ household income
    - Researching stem cell treatments and regenerative medicine
    - Many have spent $15K+ on health journey
    - Not casual browsers - comparing options for $20-50K procedures

    Categories:
    - clinic: Stem cell clinics, regenerative medicine providers
    - supplement: Health supplements, vitamins, nutraceuticals
    - diagnostic: Health testing, biomarkers, genetic tests
    - health_tech: Wearables, health devices, apps
    - longevity: Anti-aging, lifespan extension products
    - pharma: Pharmaceutical companies
    - insurance: Health insurance, coverage solutions
    - other: Everything else
    """

    # Category definitions with keywords and domain patterns
    CATEGORIES = {
        "clinic": {
            "keywords": [
                "stem cell", "regenerative", "clinic", "treatment center",
                "medical center", "therapy center", "infusion", "iv therapy",
                "wellness clinic", "functional medicine", "integrative medicine",
                "prp", "exosome", "peptide therapy", "hormone therapy",
                "anti-aging clinic", "rejuvenation", "biohacking center",
            ],
            "domain_patterns": [
                r"stemcell", r"regen", r"clinic", r"medical",
                r"therapy", r"wellness", r"health\s*center",
            ],
            "weight": 1.0,
        },
        "supplement": {
            "keywords": [
                "supplement", "vitamin", "probiotic", "collagen", "omega",
                "nootropic", "adaptogen", "nutraceutical", "natural health",
                "herbal", "botanical", "mineral", "amino acid", "protein powder",
                "fish oil", "multivitamin", "d3", "magnesium", "zinc",
            ],
            "domain_patterns": [
                r"supplement", r"vitamin", r"nutrition", r"nutra",
                r"health", r"natural",
            ],
            "weight": 0.9,
        },
        "diagnostic": {
            "keywords": [
                "test", "testing", "diagnostic", "lab", "biomarker",
                "screening", "blood test", "dna", "genetic", "at-home test",
                "health panel", "hormone test", "metabolic", "gut test",
                "microbiome", "food sensitivity", "allergy test",
            ],
            "domain_patterns": [
                r"test", r"lab", r"diagnostic", r"bio", r"gene",
                r"screen", r"panel",
            ],
            "weight": 0.95,
        },
        "health_tech": {
            "keywords": [
                "wearable", "tracker", "monitor", "app", "device",
                "sensor", "smart", "connected", "health tech", "digital health",
                "cgm", "glucose monitor", "hrv", "sleep tracker", "fitness",
                "recovery", "oura", "whoop", "garmin", "apple watch",
            ],
            "domain_patterns": [
                r"health", r"tech", r"fit", r"wearable", r"device",
                r"smart", r"digital",
            ],
            "weight": 0.85,
        },
        "longevity": {
            "keywords": [
                "longevity", "anti-aging", "lifespan", "healthspan",
                "aging", "senolytic", "nad", "rapamycin", "metformin",
                "biohacking", "life extension", "telomere", "mitochondria",
                "cellular health", "autophagy", "fasting", "cryotherapy",
                "red light", "hyperbaric", "peptides",
            ],
            "domain_patterns": [
                r"longev", r"age", r"life", r"bio", r"health",
            ],
            "weight": 1.0,
        },
        "pharma": {
            "keywords": [
                "pharmaceutical", "drug", "medication", "prescription",
                "fda", "clinical trial", "pharma", "rx", "medicine",
                "therapeutic", "treatment", "therapy",
            ],
            "domain_patterns": [
                r"pharma", r"rx", r"med", r"drug", r"therapeutic",
            ],
            "weight": 0.7,
        },
        "insurance": {
            "keywords": [
                "insurance", "coverage", "health plan", "medicare",
                "medicaid", "hsa", "fsa", "benefits", "claims",
                "premium", "deductible", "copay",
            ],
            "domain_patterns": [
                r"insurance", r"health", r"care", r"benefit",
                r"coverage", r"plan",
            ],
            "weight": 0.5,
        },
    }

    # Niche fit scoring rules for Renewal Weekly
    NICHE_FIT = {
        "high": {
            "categories": ["clinic", "longevity", "diagnostic"],
            "keywords": [
                "stem cell", "regenerative", "longevity", "anti-aging",
                "biohacking", "nad", "peptide", "biomarker", "health testing",
                "functional medicine", "exosome", "prp", "hormone optimization",
                "mitochondria", "cellular health", "healthspan", "telomere",
            ],
            "emoji": "\U0001F7E2",  # 🟢
        },
        "medium": {
            "categories": ["supplement", "health_tech", "pharma"],
            "keywords": [
                "supplement", "vitamin", "wellness", "health device",
                "tracker", "wearable", "nutrition", "collagen",
            ],
            "emoji": "\U0001F7E1",  # 🟡
        },
        "low": {
            "categories": ["insurance"],
            "keywords": [
                "b2b", "hospital", "enterprise", "healthcare it",
                "ehr", "emr", "pediatric", "children", "acute care",
                "hospital equipment", "medical supplies", "physician",
            ],
            "emoji": "\U0001F534",  # 🔴
        },
    }

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Initialize the categorizer.

        Args:
            config: Optional configuration override
        """
        if config is None:
            try:
                full_config = load_config()
                config = full_config.get("categorization", {})
            except FileNotFoundError:
                config = {}

        self.config = config

    def categorize(
        self,
        company_name: str,
        domain: str | None = None,
        ad_copy: str | None = None,
    ) -> CategoryScore:
        """
        Determine the category for an advertiser.

        Args:
            company_name: Company name
            domain: Company domain/website
            ad_copy: Ad copy text (if available)

        Returns:
            CategoryScore with category, confidence, and matched keywords
        """
        # Combine all text for analysis
        text = " ".join(filter(None, [
            company_name.lower(),
            domain.lower() if domain else "",
            ad_copy.lower() if ad_copy else "",
        ]))

        best_category = "other"
        best_score = 0.0
        best_keywords = []

        for category, rules in self.CATEGORIES.items():
            score = 0.0
            matched = []

            # Check keywords
            for keyword in rules["keywords"]:
                if keyword.lower() in text:
                    score += 1.0
                    matched.append(keyword)

            # Check domain patterns
            if domain:
                for pattern in rules["domain_patterns"]:
                    if re.search(pattern, domain, re.IGNORECASE):
                        score += 0.5
                        matched.append(f"domain:{pattern}")

            # Apply category weight
            score *= rules.get("weight", 1.0)

            if score > best_score:
                best_score = score
                best_category = category
                best_keywords = matched

        # Calculate confidence (0-1 scale)
        confidence = min(1.0, best_score / 3.0) if best_score > 0 else 0.3

        return CategoryScore(
            category=best_category,
            confidence=confidence,
            matched_keywords=best_keywords[:5],  # Top 5 matches
        )

    def score_niche_fit(
        self,
        category: str,
        company_name: str | None = None,
        domain: str | None = None,
        ad_copy: str | None = None,
    ) -> NicheFitScore:
        """
        Score how well an advertiser fits Renewal Weekly's audience.

        Args:
            category: Advertiser category
            company_name: Company name
            domain: Company domain
            ad_copy: Ad copy text

        Returns:
            NicheFitScore with score, emoji, and reason
        """
        # Combine text for keyword matching
        text = " ".join(filter(None, [
            company_name.lower() if company_name else "",
            domain.lower() if domain else "",
            ad_copy.lower() if ad_copy else "",
        ]))

        # Check for explicit low-fit indicators first
        for low_keyword in self.NICHE_FIT["low"]["keywords"]:
            if low_keyword.lower() in text:
                return NicheFitScore(
                    score="Low",
                    emoji=self.NICHE_FIT["low"]["emoji"],
                    display=f"{self.NICHE_FIT['low']['emoji']} Low",
                    reason=f"B2B/enterprise indicator: '{low_keyword}'",
                    confidence=0.8,
                )

        # Check for high-fit indicators
        high_matches = []
        for keyword in self.NICHE_FIT["high"]["keywords"]:
            if keyword.lower() in text:
                high_matches.append(keyword)

        if high_matches or category in self.NICHE_FIT["high"]["categories"]:
            reason = (
                f"Matches high-fit keywords: {', '.join(high_matches[:3])}"
                if high_matches
                else f"High-fit category: {category}"
            )
            return NicheFitScore(
                score="High",
                emoji=self.NICHE_FIT["high"]["emoji"],
                display=f"{self.NICHE_FIT['high']['emoji']} High",
                reason=reason,
                confidence=0.85 if high_matches else 0.7,
            )

        # Check for medium-fit indicators
        medium_matches = []
        for keyword in self.NICHE_FIT["medium"]["keywords"]:
            if keyword.lower() in text:
                medium_matches.append(keyword)

        if medium_matches or category in self.NICHE_FIT["medium"]["categories"]:
            reason = (
                f"Matches medium-fit keywords: {', '.join(medium_matches[:3])}"
                if medium_matches
                else f"Medium-fit category: {category}"
            )
            return NicheFitScore(
                score="Medium",
                emoji=self.NICHE_FIT["medium"]["emoji"],
                display=f"{self.NICHE_FIT['medium']['emoji']} Medium",
                reason=reason,
                confidence=0.7 if medium_matches else 0.5,
            )

        # Default to medium if category is known, low if unknown
        if category == "other":
            return NicheFitScore(
                score="Low",
                emoji=self.NICHE_FIT["low"]["emoji"],
                display=f"{self.NICHE_FIT['low']['emoji']} Low",
                reason="Unknown category, no matching keywords",
                confidence=0.4,
            )

        return NicheFitScore(
            score="Medium",
            emoji=self.NICHE_FIT["medium"]["emoji"],
            display=f"{self.NICHE_FIT['medium']['emoji']} Medium",
            reason=f"Default for category: {category}",
            confidence=0.5,
        )

    def enrich_sponsor(
        self,
        sponsor_info: dict[str, Any],
        use_claude: bool = False,
    ) -> dict[str, Any]:
        """
        Add sector to a sponsor record based on company name and domain.

        Args:
            sponsor_info: Dictionary with sponsor information
            use_claude: Ignored (kept for compatibility)

        Returns:
            Updated dictionary with sector field
        """
        # If sector is already set to a meaningful value, don't overwrite
        existing_sector = sponsor_info.get("sector", "").lower()
        if existing_sector and existing_sector != "other":
            return sponsor_info

        # Support both old and new field names
        company_name = sponsor_info.get("company_name") or sponsor_info.get("advertiser_name", "")
        domain = sponsor_info.get("domain") or sponsor_info.get("advertiser_domain", "")

        # Get sector using keyword matching on company name and domain
        cat_score = self.categorize(company_name, domain, "")
        sponsor_info["sector"] = cat_score.category

        return sponsor_info

    def _score_with_claude(
        self,
        company_name: str,
        ad_copy: str | None,
        company_description: str | None,
    ) -> dict | None:
        """
        Use Claude for intelligent niche fit scoring.

        Only called when keyword matching has low confidence.
        """
        try:
            from .claude_agent import ClaudeAgent

            agent = ClaudeAgent()
            if not agent.is_configured:
                return None

            # Define the target niche for Claude
            target_niche = """
Renewal Weekly audience:
- Ages 55-68, affluent ($150K+ household income)
- Researching stem cell treatments and regenerative medicine
- Many have spent $15K+ on their health journey
- Comparing options for $20-50K medical procedures
- Interested in longevity, anti-aging, biohacking, health optimization

HIGH FIT: Stem cell clinics, regenerative medicine, longevity products, biomarker testing, NAD+, peptides
MEDIUM FIT: Health supplements, wearables, health tech, wellness products
LOW FIT: B2B healthcare IT, hospital equipment, pediatrics, acute care, general consumer products
"""

            analysis = agent.analyze_niche_fit(
                company_name=company_name,
                ad_copy=ad_copy or "",
                company_description=company_description,
                target_niche=target_niche,
            )
            agent.close()

            if analysis:
                return {
                    "score": analysis.fit_score,
                    "emoji": analysis.fit_emoji,
                    "category": analysis.category,
                    "reasoning": analysis.reasoning,
                }
            return None

        except ImportError:
            logger.debug("Claude agent not available")
            return None
        except Exception as e:
            logger.warning(f"Claude niche fit scoring failed: {e}")
            return None


def categorize_and_score(
    company_name: str,
    domain: str | None = None,
    ad_copy: str | None = None,
) -> tuple[str, str]:
    """
    Convenience function to categorize and score an advertiser.

    Args:
        company_name: Company name
        domain: Company domain
        ad_copy: Ad copy text

    Returns:
        Tuple of (category, niche_fit_display)
    """
    categorizer = AdvertiserCategorizer()

    cat_score = categorizer.categorize(company_name, domain, ad_copy)
    fit_score = categorizer.score_niche_fit(
        cat_score.category, company_name, domain, ad_copy
    )

    return cat_score.category, fit_score.display
