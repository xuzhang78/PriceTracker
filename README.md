# Price & Deal Tracker

Automated monitors for product prices and sale events.
Uses **ScrapeGraphAI** (AI extraction) + **GitHub Actions** (scheduling) + **ntfy.sh** (push notifications).

---

## Trackers

### 1. 🛍️ Dealmoon BOGO Deal Tracker (`check_deals.py`)

Monitors Dealmoon brand pages for "Buy One Get One" (BOGO) sale events on **Clinique** and **Lancome**.
Sends a high-priority push notification the moment a new active BOGO deal is detected.

```
GitHub Actions (daily 10:00 UTC)
    └── check_deals.py
            ├── select provider (Firecrawl primary)
            ├── extract page deals (including raw HTML on Firecrawl)
            ├── parse Unix timestamps from HTML to resolve dates
            └── BOGO keyword match + new deal → ntfy.sh → 📱 your phone
```

**Key features:**
- **Exact Date Parsing**: Fallback to raw HTML parsing to capture real timestamps (`t="..."`) and map them to LLM-extracted deals.
- **Dynamic Testing Pagination**: Automatically paginates up to 3 pages in E2E testing mode if no BOGO deal is found on page 1.
- **Robust Provider Gating**: Runs via Firecrawl with automatic fallback options.
- **State deduplication**: `deals_state.json` tracks notified deals to prevent spam.

---

### 2. 💻 B&H Photo MacBook Price Tracker (`check_prices.py`)

Monitors B&H Photo for MacBook Pro M5 price drops below a configured threshold.

```
GitHub Actions (daily 09:00 UTC)
    └── check_prices.py
            ├── select provider (Firecrawl primary)
            ├── extract MacBook price details (HTML regex + LLM fallback)
            └── price < threshold → ntfy.sh → 📱 your phone
```

---

### 3. 📱 iPhone Price Tracker (`check_prices.py`)

Monitors **BestBuy** and **Verizon** for iPhone 17 Pro Max 512GB Deep Blue price drops below $1,399.
Uses LLM-only extraction (no regex) with a prompt engineered to capture the **single full payment price** (not monthly installments).

```
GitHub Actions (daily 09:00 UTC)
    └── check_prices.py
            ├── select provider (Firecrawl primary)
            ├── extract iPhone price details (LLM structured extraction)
            └── price < threshold → ntfy.sh → 📱 your phone
```

**Key features:**
- **start_date gating**: Tracker entries support a `start_date` field — the script skips entries until that date. The iPhone trackers start on **2026-09-01**.
- **Full-price extraction**: Prompt explicitly requests the one-time purchase price, avoiding monthly installment amounts.

All configurations are managed in the unified config file `tracker_config.json`.

---

## Prerequisites

- Python 3.12+ (since scrapegraph/firecrawl libraries require it)
- A [Firecrawl](https://firecrawl.dev/) API key (free tier available)
- Optional: A [ScraperAPI](https://www.scraperapi.com/) API key (backup proxy service, free tier available)
- The [ntfy app](https://ntfy.sh) installed on your phone, subscribed to your topic

---

## Setup

### Step 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Configure `.env` (local testing only)

```bash
FIRECRAWL_API_KEY=fc-xxxxxxxxxxxxxxxxxxxxxxxx   # from firecrawl.dev
SCRAPERAPI_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # optional backup from scraperapi.com
NTFY_TOPIC=your-ntfy-topic                 # already set — use as-is
```

### Step 3 — Add GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `FIRECRAWL_API_KEY` | Your Firecrawl API key |
| `SCRAPERAPI_KEY` | (Optional) Your ScraperAPI API key (backup proxy) |
| `NTFY_TOPIC` | Your ntfy topic name |

---

## Running Locally

### Dealmoon BOGO Tracker

**Normal run (active deals only):**
```bash
python check_deals.py
```

**Real-data E2E test (includes expired BOGO deals — verifies full pipeline):**
```bash
# Linux / macOS
INCLUDE_EXPIRED=true python check_deals.py

# Windows PowerShell
$env:INCLUDE_EXPIRED="true"; python check_deals.py
```
Expected: Historical Clinique/Lancome BOGO events are matched, `deals_state.json` is updated,
and a high-priority push notification fires on your phone.

**Re-run E2E test to verify deduplication:**
```bash
$env:INCLUDE_EXPIRED="true"; python check_deals.py
```
Expected: "Already notified — skipping duplicate alert." No push notification fires again.

### MacBook Price Tracker

```bash
# Real live price check
python check_prices.py

# Simulate a price drop (no API calls)
PRICE_OVERRIDE=2499.00 python check_prices.py
```

---

## Testing on GitHub Actions

### BOGO Tracker — Real-Data E2E Test
1. Go to **Actions → Dealmoon BOGO Daily Check → Run workflow**
2. Set `include_expired` input to `true`
3. Click **Run workflow**
4. Confirm: run log shows extracted deals, BOGO keyword matches, and ntfy alert fires on phone ✅

### BOGO Tracker — Normal Scheduled Run
The daily cron (`0 10 * * *`) triggers automatically.
When a new active BOGO deal is found, you will receive a push notification.

---

## Managing Deals Configuration

Brand configurations are in [`tracker_config.json`](./tracker_config.json). No code changes needed.

### Add a new brand
```json
{
  "type": "dealmoon_bogo",
  "brand": "Estee Lauder",
  "url": "https://www.dealmoon.com/en/top/estee-lauder?type=1&...",
  "keywords": ["buy one get one", "bogo", "b1g1", "buy 1 get 1"],
  "active": true
}
```

### Pause a brand
```json
{
  "type": "dealmoon_bogo",
  "brand": "Clinique",
  ...
  "active": false
}
```

---

## Managing Price Products

Products are configured in [`tracker_config.json`](./tracker_config.json).

### Re-enable a paused MacBook product
```json
{
  "type": "bh_price",
  "name": "MacBook Pro M5 Space Black 14\"",
  "url": "https://...",
  "threshold": 2700.00,
  "active": true
}
```

### Add an iPhone product with delayed start
```json
{
  "type": "iphone_price",
  "name": "iPhone 17 Pro Max 512GB Deep Blue (BestBuy)",
  "url": "https://www.bestbuy.com/product/...",
  "threshold": 1399.00,
  "start_date": "2026-09-01",
  "active": true
}
```

The `start_date` field (format: `YYYY-MM-DD`) delays tracking until that date. The entry is `active: true` but the script will skip it until the start date arrives.

---

## Credit Usage

Thanks to **Optimizations A & B**, our daily checks are highly cost-efficient:

* **BOGO Tracker**: Uses structured JSON extraction (5 credits per request on Firecrawl). Checking 2 brands daily consumes $2 \times 5 = 10$ credits/day.
* **MacBook Price Tracker**: Uses HTML-only scraping and regex parsing (1 credit per request on Firecrawl). Checking 2 products daily consumes $2 \times 1 = 2$ credits/day.
* **iPhone Price Tracker**: Uses LLM structured extraction (10 credits per request on Firecrawl). Checking 2 stores daily consumes $2 \times 10 = 20$ credits/day. **Dormant until 2026-09-01.**

| Tracker | Schedule | Requests/month | Credits/month | Status |
|---|---|---|---|---|
| BOGO Tracker (2 brands) | Daily | 60 | 300 | Active |
| MacBook Tracker (2 models) | Daily | 60 | 60 | Paused |
| iPhone Tracker (2 stores) | Daily | 60 | 600 | Starts Sep 1 |
| **Total (when all active)** | | **180** | **960** | |

> **Note:** ScrapeGraphAI (SGAI) is no longer checked or used due to one-time-only free credits. Firecrawl is the primary provider. If Firecrawl fails, the script automatically attempts **ScraperAPI** (if key is set) followed by completely free **local direct-request parsing** fallbacks.

### Optimization Highlights
1. **B&H HTML Scraping**: The MacBook tracker queries Firecrawl in html-only mode (1 credit instead of 5-10 credits) and parses data using regex, falling back to LLM structured extraction only on failure.
2. **Capped Testing Retries (Optimization A)**: In test modes, retries are capped at `1` instead of `3` to prevent credit burn during failure debugging.
3. **Smart Pagination**: Dealmoon crawls subsequent pages (up to page 3) only during testing mode when looking for historical BOGO matches. Daily production runs check page 1 only.
4. **Start-date gating**: iPhone tracker entries use `start_date` to avoid burning credits before the product is available for purchase.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `HTTP 502/504` on extraction | Transient proxy timeout | Retry logic handles this automatically (3 attempts + fallback URL) |
| `result.status == "error"` | Firecrawl credits exhausted or invalid key | Check Firecrawl dashboard credit balance |
| No ntfy notification | Wrong topic name | Confirm topic in ntfy app matches `NTFY_TOPIC` in secrets |
| BOGO deal not detected | Keywords don't match | Check `tracker_config.json` keywords vs. actual deal text |
| Duplicate alerts | State file reset or deleted | `deals_state.json` tracks notified deals — do not delete manually |
| GitHub Actions running out of credits | Daily checks using too many credits | Pause MacBook trackers, set longer interval for iPhone trackers, or rely on zero-cost local fallbacks |
