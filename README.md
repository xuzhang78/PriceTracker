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
            ├── select provider (SGAI or Firecrawl)
            ├── extract page deals (including raw HTML on Firecrawl)
            ├── parse Unix timestamps from HTML to resolve dates
            └── BOGO keyword match + new deal → ntfy.sh → 📱 your phone
```

**Key features:**
- **Exact Date Parsing**: Fallback to raw HTML parsing to capture real timestamps (`t="..."`) and map them to LLM-extracted deals.
- **Dynamic Testing Pagination**: Automatically paginates up to 3 pages in E2E testing mode if no BOGO deal is found on page 1.
- **Robust Provider Gating**: Intelligent fallback from SGAI to Firecrawl when credits are depleted.
- **State deduplication**: `deals_state.json` tracks notified deals to prevent spam.

---

### 2. 💻 B&H Photo MacBook Price Tracker (`check_prices.py`)

Monitors B&H Photo for MacBook Pro M5 price drops below a configured threshold.

```
GitHub Actions (daily 09:00 UTC)
    └── check_prices.py
            ├── select provider (SGAI or Firecrawl)
            ├── extract MacBook price details
            └── price < threshold → ntfy.sh → 📱 your phone
```

All configurations are managed in the unified config file `tracker_config.json`.

---

## Prerequisites

- Python 3.11+
- A [ScrapeGraphAI](https://scrapegraphai.com/dashboard) account (free tier)
- The [ntfy app](https://ntfy.sh) installed on your phone, subscribed to your topic

---

## Setup

### Step 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Configure `.env` (local testing only)

```bash
SGAI_API_KEY=sgai-xxxxxxxxxxxxxxxxxxxxxxxx   # from scrapegraphai.com/dashboard
NTFY_TOPIC=your-ntfy-topic            # already set — use as-is
```

### Step 3 — Add GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `SGAI_API_KEY` | Your ScrapeGraphAI API key |
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

# Simulate a price drop (no SGAI call)
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

---

## Credit Usage

Thanks to **Optimizations A & B**, our daily checks are highly cost-efficient:

* **BOGO Tracker**: Uses structured JSON extraction (5 credits per request on Firecrawl fallback). Checking 2 brands daily consumes $2 \times 5 = 10$ credits/day.
* **MacBook Price Tracker**: Uses HTML-only scraping and regex parsing (1 credit per request on Firecrawl/SGAI). Checking 2 products daily consumes $2 \times 1 = 2$ credits/day.

| Tracker | Schedule | Requests/month | Credits/month |
|---|---|---|---|
| BOGO Tracker (2 brands) | Daily | 60 | 300 |
| MacBook Tracker (2 models) | Daily | 60 | 60 |
| **Total** | | **120** | **360** |

This total of **360 credits/month** fits comfortably within the **500 free credits/month** plan.

### Optimization Highlights
1. **B&H HTML Scraping**: The MacBook tracker queries Firecrawl or SGAI in html-only mode (1 credit instead of 5-10 credits) and parses data using regex, falling back to LLM structured extraction only on failure.
2. **Capped Testing Retries (Optimization A)**: In test modes, retries are capped at `1` instead of `3` to prevent credit burn during failure debugging.
3. **Smart Pagination**: Dealmoon crawls subsequent pages (up to page 3) only during testing mode when looking for historical BOGO matches. Daily production runs check page 1 only.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `HTTP 502/504` on extraction | Transient proxy timeout | Retry logic handles this automatically (3 attempts + fallback URL) |
| `result.status == "error"` | SGAI credits exhausted or invalid key | Check dashboard credit balance |
| No ntfy notification | Wrong topic name | Confirm topic in ntfy app matches `NTFY_TOPIC` in secrets |
| BOGO deal not detected | Keywords don't match | Check `deals_config.json` keywords vs. actual deal text |
| Duplicate alerts | State file reset or deleted | `deals_state.json` tracks notified deals — do not delete manually |
| GitHub Actions not running | Workflow not triggered yet | Run manually via Actions tab first |
