# Newsletter Advertiser Intelligence System

Automated system to discover advertisers in competitor newsletters, categorize them, and score their fit for Renewal Weekly's audience.

## Overview

This system scrapes health-focused newsletters (Healthcare Brew, Morning Brew) to identify their advertisers, then:
1. Extracts advertiser name, domain, and ad copy
2. Categorizes them (clinic, supplement, diagnostic, health_tech, longevity, pharma, insurance)
3. Scores niche fit for Renewal Weekly's audience (55-68 age, $150K+ HHI, researching stem cell/regenerative medicine)

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install Playwright browsers
playwright install chromium

# 3. Run tests to verify setup
python test_scraper.py

# 4. Run a quick test scan (5 issues)
python main.py --newsletter healthcare_brew --limit 5

# 5. Run full scan
python main.py
```

## Usage

```bash
# Scan specific newsletter
python main.py --newsletter healthcare_brew

# Limit number of issues (useful for testing)
python main.py --newsletter healthcare_brew --limit 10

# Save to custom output file
python main.py --output my_advertisers.csv

# Run with visible browser (debugging)
python main.py --no-headless --limit 5

# List available newsletters
python main.py --list-newsletters

# Verbose output
python main.py -v
```

## Output

Results are saved to `output/advertisers_YYYYMMDD_HHMMSS.csv` with columns:
- `advertiser_name`: Company name
- `advertiser_domain`: Website domain
- `category`: clinic, supplement, diagnostic, health_tech, longevity, pharma, insurance, other
- `niche_fit`: 🟢 High, 🟡 Medium, or 🔴 Low fit for Renewal Weekly
- `placement_type`: Type of ad placement (presented_by, together_with, etc.)
- `ad_copy_snippet`: First 150 chars of ad copy
- `source_newsletter`: Which newsletter the ad was found in
- `issue_date`: Publication date
- `issue_url`: Link to the newsletter issue
- `confidence`: Detection confidence (high/medium/low)

## Niche Fit Scoring

**🟢 High Fit** - Ideal for Renewal Weekly audience:
- Stem cell clinics
- Regenerative medicine providers
- Longevity/anti-aging brands
- Health diagnostics/testing

**🟡 Medium Fit** - Good potential:
- Health supplements
- Wearables/health tech
- Wellness products

**🔴 Low Fit** - Not ideal:
- B2B healthcare IT
- Hospital equipment
- Enterprise software

## Project Structure

```
scraper/
├── main.py                 # Main entry point
├── test_scraper.py         # Test suite
├── requirements.txt        # Python dependencies
├── config/
│   └── newsletters.yaml    # Newsletter configurations
├── src/
│   ├── scrapers/
│   │   ├── base.py         # Base scraper class
│   │   └── healthcare_brew.py  # Newsletter-specific scrapers
│   ├── enrichment/
│   │   └── categorizer.py  # Categorization & scoring
│   └── utils/
│       ├── config.py       # Configuration loading
│       └── helpers.py      # Helper functions
└── output/                 # CSV output files
```

## Future Phases

- **Phase 2**: Contact enrichment via Apollo.io API
- **Phase 3**: Google Sheets CRM integration
- **Phase 4**: Email draft generation
- **Phase 5**: n8n automation for follow-up sequences
