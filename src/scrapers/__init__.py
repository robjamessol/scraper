# Newsletter scrapers
from .base import BaseScraper, SponsorInfo, AffiliateLink, AFFILIATE_NETWORKS
from .healthcare_brew import HealthcareBrewScraper, MorningBrewScraper

__all__ = [
    "BaseScraper",
    "SponsorInfo",
    "AffiliateLink",
    "AFFILIATE_NETWORKS",
    "HealthcareBrewScraper",
    "MorningBrewScraper",
]
