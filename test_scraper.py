#!/usr/bin/env python3
"""
Quick test script for the Newsletter Scraper.

Tests basic functionality:
1. Configuration loading
2. Categorizer functionality
3. Scraper initialization (without browser)

Usage:
    python test_scraper.py
"""

import sys


def test_config():
    """Test configuration loading."""
    print("Testing configuration loading...")

    from src.utils.config import load_config, get_newsletter_config

    config = load_config()
    assert "newsletters" in config, "Config missing 'newsletters' key"
    assert "healthcare_brew" in config["newsletters"], "Config missing healthcare_brew"

    hb_config = get_newsletter_config("healthcare_brew")
    assert hb_config["name"] == "Healthcare Brew"
    assert "archive_url" in hb_config

    print("  OK - Configuration loads correctly")
    return True


def test_helpers():
    """Test helper functions."""
    print("Testing helper functions...")

    from src.utils.helpers import (
        extract_domain,
        clean_text,
        normalize_company_name,
        truncate_text,
    )

    # Test domain extraction
    assert extract_domain("https://www.example.com/page") == "example.com"
    assert extract_domain("http://test.org") == "test.org"
    assert extract_domain("https://www.healthedge.com/landing?utm=123") == "healthedge.com"

    # Test text cleaning
    assert clean_text("  hello   world  ") == "hello world"
    assert clean_text("test\n\n\nvalue") == "test value"

    # Test company name normalization
    assert normalize_company_name("HealthEdge, Inc.") == "healthedge"
    assert normalize_company_name("The Wellness Company LLC") == "wellness company"

    # Test truncation
    assert truncate_text("short", 100) == "short"
    assert len(truncate_text("a" * 200, 50)) == 50
    assert truncate_text("a" * 200, 50).endswith("...")

    print("  OK - Helper functions work correctly")
    return True


def test_categorizer():
    """Test advertiser categorization."""
    print("Testing categorizer...")

    from src.enrichment import AdvertiserCategorizer

    categorizer = AdvertiserCategorizer()

    # Test stem cell clinic (should be high fit)
    cat = categorizer.categorize("Stem Cell Institute", "stemcellinstitute.com", "regenerative medicine")
    assert cat.category == "clinic", f"Expected 'clinic', got '{cat.category}'"

    fit = categorizer.score_niche_fit(cat.category, "Stem Cell Institute", "stemcellinstitute.com")
    assert fit.score == "High", f"Expected 'High', got '{fit.score}'"
    assert "🟢" in fit.emoji or fit.emoji == "\U0001F7E2"

    # Test supplement company (should be medium fit)
    cat = categorizer.categorize("Vitamin World", "vitaminworld.com", "supplements and vitamins")
    assert cat.category == "supplement", f"Expected 'supplement', got '{cat.category}'"

    fit = categorizer.score_niche_fit(cat.category)
    assert fit.score == "Medium", f"Expected 'Medium', got '{fit.score}'"

    # Test B2B healthcare IT (should be low fit)
    cat = categorizer.categorize("Hospital IT Solutions", "hospitalit.com", "enterprise EHR system for hospitals")
    fit = categorizer.score_niche_fit(cat.category, "Hospital IT", "hospitalit.com", "enterprise EHR")
    assert fit.score == "Low", f"Expected 'Low', got '{fit.score}'"

    print("  OK - Categorizer works correctly")
    return True


def test_scraper_class():
    """Test scraper class initialization (no browser)."""
    print("Testing scraper class initialization...")

    from src.scrapers import HealthcareBrewScraper, MorningBrewScraper

    # Test Healthcare Brew scraper init
    hb = HealthcareBrewScraper.__new__(HealthcareBrewScraper)
    config = HealthcareBrewScraper._default_config()
    assert config["name"] == "Healthcare Brew"
    assert "healthcare-brew.com" in config["archive_url"]

    # Test Morning Brew scraper init
    mb = MorningBrewScraper.__new__(MorningBrewScraper)
    config = MorningBrewScraper._default_config()
    assert config["name"] == "Morning Brew"
    assert "morningbrew.com" in config["archive_url"]

    print("  OK - Scraper classes initialize correctly")
    return True


def test_sponsor_info():
    """Test SponsorInfo dataclass."""
    print("Testing SponsorInfo dataclass...")

    from src.scrapers.base import SponsorInfo

    sponsor = SponsorInfo(
        advertiser_name="Test Company",
        advertiser_domain="test.com",
        placement_type="presented_by",
        ad_copy_snippet="Test ad copy here...",
        issue_url="https://example.com/issues/test",
        issue_date="2026-01-19",
        source_newsletter="healthcare_brew",
        category="health_tech",
        niche_fit="🟡 Medium",
        confidence="high",
    )

    data = sponsor.to_dict()
    assert data["advertiser_name"] == "Test Company"
    assert data["advertiser_domain"] == "test.com"
    assert data["niche_fit"] == "🟡 Medium"

    print("  OK - SponsorInfo works correctly")
    return True


def main():
    """Run all tests."""
    print("\n" + "=" * 50)
    print("Newsletter Scraper Test Suite")
    print("=" * 50 + "\n")

    tests = [
        test_config,
        test_helpers,
        test_categorizer,
        test_scraper_class,
        test_sponsor_info,
    ]

    passed = 0
    failed = 0

    for test_fn in tests:
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"  FAILED - {e}")
            failed += 1

    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50 + "\n")

    if failed > 0:
        print("Some tests failed. Please check the errors above.")
        return 1

    print("All tests passed! Ready to run the scraper.")
    print("\nNext steps:")
    print("  1. Install Playwright: playwright install chromium")
    print("  2. Run a quick test: python main.py --newsletter healthcare_brew --limit 5")
    print("  3. Or run full scan: python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
