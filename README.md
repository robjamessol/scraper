# Newsletter Advertiser Intelligence System

Automated system to discover advertisers in competitor newsletters, categorize them, and score their fit for Renewal Weekly's audience.

## Features

- **Web Dashboard** - Beautiful UI to view and manage advertisers
- **API Endpoints** - Integrate with n8n or other automation tools
- **Scheduled Scans** - Automatically scan newsletters daily
- **Niche Fit Scoring** - Prioritize high-fit advertisers for your audience
- **CSV Export** - Download data for your CRM

## Quick Start (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install Playwright browsers
playwright install chromium

# 3. Run the web server
python server.py

# 4. Open http://localhost:8000
```

## Deploy to Railway (Recommended)

Railway offers a generous free tier and handles Playwright/Chromium automatically.

### Manual Deploy

1. **Create Railway Account**: Go to [railway.app](https://railway.app) and sign up

2. **Install Railway CLI**:
   ```bash
   npm install -g @railway/cli
   ```

3. **Login and Deploy**:
   ```bash
   railway login
   railway init
   railway up
   ```

4. **Set Environment Variables** (in Railway dashboard):
   ```
   SCAN_SCHEDULE_ENABLED=true
   SCAN_SCHEDULE_HOUR=6
   ```

5. **Get Your URL**: Railway will give you a URL like `https://your-app.railway.app`

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8000 | Server port (Railway sets this automatically) |
| `SCAN_SCHEDULE_ENABLED` | true | Enable daily scheduled scans |
| `SCAN_SCHEDULE_HOUR` | 6 | Hour (UTC) for daily scan |

## Usage

### Web Dashboard

- **Dashboard** (`/`) - Overview of all advertisers with stats
- **Advertisers** (`/advertisers`) - Full list with filtering
- **Export** - Download CSV from the dashboard

### API Endpoints

Perfect for n8n integration:

```bash
# Start a scan
curl -X POST https://your-app.railway.app/api/scan

# Start scan with options
curl -X POST https://your-app.railway.app/api/scan \
  -H "Content-Type: application/json" \
  -d '{"newsletters": ["healthcare_brew"], "limit": 10}'

# Check scan status
curl https://your-app.railway.app/api/status

# Get advertisers (with filtering)
curl "https://your-app.railway.app/api/advertisers?fit=high&limit=50"

# Download CSV
curl https://your-app.railway.app/api/advertisers/export -o advertisers.csv

# Get available newsletters
curl https://your-app.railway.app/api/newsletters
```

### n8n Integration Example

1. **HTTP Request Node** - POST to `/api/scan` to start a scan
2. **Wait Node** - Wait 5 minutes for scan to complete
3. **HTTP Request Node** - GET `/api/advertisers?fit=high` to get results
4. **Google Sheets Node** - Append new advertisers to your CRM sheet

## CLI Usage

You can still use the command line if you prefer:

```bash
# Scan specific newsletter
python main.py --newsletter healthcare_brew

# Limit issues (for testing)
python main.py --newsletter healthcare_brew --limit 10

# Full scan of all newsletters
python main.py
```

## Niche Fit Scoring

Advertisers are scored based on fit for Renewal Weekly's audience:

| Score | Categories | Examples |
|-------|-----------|----------|
| **High** | clinic, longevity, diagnostic | Stem cell clinics, anti-aging, health testing |
| **Medium** | supplement, health_tech, pharma | Vitamins, wearables, wellness products |
| **Low** | insurance, B2B | Healthcare IT, hospital equipment |

## Project Structure

```
scraper/
├── server.py               # Web server entry point
├── main.py                 # CLI entry point
├── Dockerfile              # Container configuration
├── railway.json            # Railway deployment config
├── config/
│   └── newsletters.yaml    # Newsletter source configs
├── src/
│   ├── scrapers/           # Newsletter scraping logic
│   ├── enrichment/         # Categorization & scoring
│   ├── utils/              # Helper functions
│   └── web/                # FastAPI web application
│       ├── app.py          # Main web app
│       └── templates/      # HTML templates
└── output/                 # CSV/JSON output files
```

## Future Phases

- **Phase 2**: Contact enrichment via Apollo.io API
- **Phase 3**: Google Sheets CRM integration
- **Phase 4**: Email draft generation
- **Phase 5**: n8n automation for follow-up sequences
