# Price & Deal Tracker

Automated monitors for product prices and sale events.
Uses **Firecrawl** (primary scraper + AI extraction), **ScraperAPI** / **Scrape.do** proxies with **Google Gemini** extraction as fallbacks, **GitHub Actions** (scheduling) and **ntfy.sh** (push notifications).

> ScrapeGraphAI was the original extraction provider. It was replaced by Firecrawl once its one-time free credits ran out; `REFERENCE.md` keeps the history.

---

## Trackers

### 1. 🛍️ Dealmoon BOGO Deal Tracker (`check_deals.py`)

Monitors Dealmoon brand pages for "Buy One Get One" (BOGO) sale events on **Clinique** and **Lancome** (both currently paused in config).
Sends a high-priority push notification the moment a new active BOGO deal is detected.

```
GitHub Actions (daily 10:00 UTC)
    └── check_deals.py
            ├── select provider (Firecrawl primary; ScraperAPI / Scrape.do + Gemini fallbacks)
            ├── extract page deals (structured JSON + raw HTML)
            ├── parse Unix timestamps from HTML to resolve dates
            └── BOGO keyword match + new deal → ntfy.sh → 📱 your phone
```

**Key features:**
- **Exact Date Parsing**: Raw HTML timestamps (`t="..."`) are mapped onto LLM-extracted deals by deal ID.
- **Dynamic Testing Pagination**: Automatically paginates up to 3 pages in E2E testing mode if no BOGO deal is found on page 1.
- **Provider fallback**: Firecrawl first; if it fails, ScraperAPI then Scrape.do fetch the HTML and Gemini extracts the deals.
- **State deduplication**: `deals_state.json` tracks notified deals to prevent spam. It is gitignored and the workflow does not persist it between runs, so dedup is effective for local runs only.

---

### 2. 💻 B&H Photo MacBook Price Tracker (`check_prices.py`)

Monitors B&H Photo for MacBook Pro M5 price drops below a configured threshold.

```
GitHub Actions (daily 10:00 UTC)
    └── check_prices.py
            ├── select provider (Firecrawl primary; ScraperAPI / Scrape.do + Gemini fallbacks)
            ├── extract MacBook price details (HTML regex + LLM fallback)
            └── price < threshold → ntfy.sh → 📱 your phone
```

---

### 3. 📱 iPhone Price Tracker (`check_prices.py`)

Monitors **BestBuy** and **Verizon** for iPhone 17 Pro Max 512GB Deep Blue price drops below $1,399.
Prompts are engineered to capture the **single full payment price** (not monthly installments) for the 512GB variant.

```
GitHub Actions (daily 10:00 UTC)
    └── check_prices.py
            ├── select provider (Firecrawl primary; ScraperAPI / Scrape.do + Gemini fallbacks)
            ├── Verizon: direct HTTP fetch (0 credits) → parse embedded catalog JSON → LLM fallback
            ├── BestBuy: LLM structured extraction
            └── price < threshold → ntfy.sh → 📱 your phone
```

**Key features:**
- **start_date gating**: Tracker entries support a `start_date` field; the script skips entries until that date. The iPhone trackers started on **2026-09-01**.
- **Full-price extraction**: Prompt explicitly requests the one-time purchase price, avoiding monthly installment amounts.
- **Verizon catalog parser**: Verizon's Next.js payload is parsed directly for the target SKU's price and shipping stock status before any LLM call.

All configurations are managed in the unified config file `tracker_config.json`.

---

## Prerequisites

- Python 3.12+ (the workflow runs 3.13)
- A [Firecrawl](https://firecrawl.dev/) API key (free tier available)
- A [Google Gemini](https://aistudio.google.com/) API key (used for extraction when a proxy fallback fetches the HTML)
- Optional: [ScraperAPI](https://www.scraperapi.com/) and/or [Scrape.do](https://scrape.do/) API keys (backup proxies, free tiers available)
- The [ntfy app](https://ntfy.sh) installed on your phone, subscribed to your topic

---

## Setup

### Step 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Configure `.env` (local testing only)

Copy `.env.example` to `.env` and fill in:

```bash
FIRECRAWL_API_KEY=fc-xxxxxxxxxxxxxxxxxxxxxxxx   # from firecrawl.dev
SCRAPERAPI_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # optional backup proxy from scraperapi.com
SCRAPE_DO_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # optional backup proxy from scrape.do
GEMINI_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxxxxxxx # from aistudio.google.com
NTFY_TOPIC=your-ntfy-topic                       # choose a long random topic name
```

`.env` is gitignored. Never commit it.

### Step 3 — Add GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `FIRECRAWL_API_KEY` | Your Firecrawl API key |
| `SCRAPERAPI_KEY` | (Optional) Your ScraperAPI API key (backup proxy) |
| `SCRAPE_DO_KEY` | (Optional) Your Scrape.do API key (backup proxy) |
| `GEMINI_API_KEY` | Your Google Gemini API key (extraction for proxy fallbacks) |
| `NTFY_TOPIC` | Your ntfy topic name |

---

## Running Locally

### Dealmoon BOGO Tracker

**Normal run (active deals only):**
```bash
python check_deals.py
```

**Real-data E2E test (includes expired BOGO deals, verifies the full pipeline):**
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
INCLUDE_EXPIRED=true python check_deals.py
```
Expected: "Already notified — skipping." No push notification fires again.

### Price Tracker (MacBook + iPhone)

```bash
# Real live price check
python check_prices.py

# Real extraction, always notify with the actual price (pipeline verification)
REPORT_MODE=true python check_prices.py

# Simulate a price drop (no scraper calls; still sends the ntfy alert)
PRICE_OVERRIDE=1299.00 python check_prices.py
```

Test modes (`PRICE_OVERRIDE`, `REPORT_MODE`, `TESTING`, `INCLUDE_EXPIRED`) cap retries at 1, bypass `start_date` gating, and skip the 2-hour full-flow retry.

---

## Testing on GitHub Actions

Both trackers run from a single workflow, **Price & Deal Tracker** (`.github/workflows/tracker_check.yml`), as two jobs. Each job exits early if `tracker_config.json` has no active entry of its type.

### Manual run
1. Go to **Actions → Price & Deal Tracker → Run workflow**
2. Choose `run_tracker`: `auto` (respect `active` flags), `dealmoon`, `prices` or `both`
3. Optional inputs: `include_expired=true` (Dealmoon E2E), `price_override` (simulate a drop), `report_mode=true` (real extraction, always notify)
4. Click **Run workflow** and confirm the ntfy alert arrives on your phone ✅

### Scheduled run
The daily cron (`0 10 * * *`) triggers automatically.
When a new active BOGO deal or a price below threshold is found, you receive a push notification.

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

Firecrawl is the only metered primary provider. Per request it charges 1 credit for an HTML-only scrape and 5 credits for a scrape with JSON (LLM) extraction. The trackers minimise LLM use:

* **BOGO Tracker**: One JSON + HTML scrape per brand per day (5 credits). Two brands: 10 credits/day.
* **MacBook Price Tracker**: HTML-only scrape plus regex parsing (1 credit). JSON extraction (5 credits) only if regex fails. Two products: 2 credits/day.
* **iPhone Price Tracker**: Verizon tries a free direct fetch first, then a 1-credit HTML scrape, then JSON extraction (5 credits). BestBuy uses JSON extraction (5 credits). Worst case for two stores: 10 credits/day.

| Tracker | Schedule | Requests/month | Credits/month (worst case) | Status |
|---|---|---|---|---|
| BOGO Tracker (2 brands) | Daily | 60 | 300 | Paused |
| MacBook Tracker (2 models) | Daily | 60 | 60 | Paused |
| iPhone Tracker (2 stores) | Daily | 60 | 300 | Active since 2026-09-01 |

> **Note:** If Firecrawl fails for a product, the script falls back to **ScraperAPI** and then **Scrape.do** (each only if its key is set). The fetched HTML is parsed with regex where possible, otherwise sent to **Gemini** for extraction. Those services have their own quotas; Gemini `gemini-2.5-flash` is used.

### Optimization Highlights
1. **B&H HTML Scraping**: The MacBook tracker fetches HTML only (1 credit) and parses it with regex, falling back to LLM structured extraction only on failure.
2. **Verizon direct fetch**: A plain HTTP request to Verizon is attempted first (0 credits). The embedded catalog JSON is parsed for the 512GB SKU before any provider or LLM call.
3. **Capped Testing Retries**: In test modes, retries are capped at 1 instead of 3 to prevent credit burn during failure debugging.
4. **Smart Pagination**: Dealmoon crawls subsequent pages (up to page 3) only during testing mode when looking for historical BOGO matches. Daily production runs check page 1 only.
5. **Start-date gating**: Tracker entries use `start_date` to avoid burning credits before the product is available for purchase. The workflow also skips a job entirely when it has no active, started entries.
6. **HTML minimisation**: Styles, SVGs, comments and non-catalog scripts are stripped before HTML is sent to Gemini, cutting token usage.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| Transient timeouts or `502/504` during extraction | Proxy or anti-bot delay | Retry logic handles this automatically (3 attempts per provider, then provider fallback, then a 2-hour full-flow retry) |
| Firecrawl exception or empty `json` field | Firecrawl credits exhausted, invalid key, or page layout change | Check the Firecrawl dashboard credit balance; the script falls back to proxies + Gemini if keys are set |
| `Extraction Error` alert with price out of range | Blocked page or layout change | Sanity guard rejects prices outside $100–$10,000. Inspect the page and adjust parsers or prompts |
| No ntfy notification | Wrong topic name | Confirm topic in ntfy app matches `NTFY_TOPIC` in secrets |
| BOGO deal not detected | Keywords don't match | Check `tracker_config.json` keywords vs. actual deal text |
| Duplicate alerts | State file reset or deleted | `deals_state.json` tracks notified deals locally; do not delete manually |
| Credits running out | Daily checks using too many credits | Pause trackers in `tracker_config.json`, or set a `start_date` to delay them |
