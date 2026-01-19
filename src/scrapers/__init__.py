# Newsletter scrapers
from .base import BaseScraper, SponsorInfo
from .healthcare_brew import HealthcareBrewScraper, MorningBrewScraper

__all__ = [
    "BaseScraper",
    "SponsorInfo",
    "HealthcareBrewScraper",
    "MorningBrewScraper",
]
