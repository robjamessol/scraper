# Newsletter scrapers
from .base import BaseScraper, SponsorInfo, AffiliateLink, AFFILIATE_NETWORKS
from .healthcare_brew import HealthcareBrewScraper, MorningBrewScraper
from .generic import GenericNewsletterScraper

__all__ = [
    "BaseScraper",
    "SponsorInfo",
    "AffiliateLink",
    "AFFILIATE_NETWORKS",
    "HealthcareBrewScraper",
    "MorningBrewScraper",
    "GenericNewsletterScraper",
]
