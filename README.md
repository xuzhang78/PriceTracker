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
            ├── sgai.extract(clinique_url)  ← AI reads brand page
            ├── sgai.extract(lancome_url)   ← AI reads brand page
            └── BOGO keyword match + new deal → ntfy.sh → 📱 your phone
```

**Key features:**
- 3-attempt retry loop with exponential backoff (15s, 30s) to handle transient HTTP 502/504 errors
- Automatic URL fallback — complex search URL → clean canonical landing URL
- State deduplication — `deals_state.json` tracks notified deals to prevent spam
- `INCLUDE_EXPIRED=true` mode for real-data end-to-end pipeline testing

---

### 2. 💻 B&H Photo MacBook Price Tracker (`check_prices.py`) — *Currently Paused*

Monitors B&H Photo for MacBook Pro M5 price drops below a configured threshold.
All products are currently set to `"active": false` in `products.json`.

```
GitHub Actions (daily 09:00 UTC)
    └── check_prices.py
            └── price < threshold → ntfy.sh → 📱 your phone
```

To re-enable, set `"active": true` for the desired product in `products.json`.

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

Brand configurations are in [`deals_config.json`](./deals_config.json). No code changes needed.

### Add a new brand
```json
{
  "brand": "Estee Lauder",
  "url": "https://www.dealmoon.com/en/top/estee-lauder?type=1&...",
  "fallback_url": "https://www.dealmoon.com/en/top/estee-lauder",
  "keywords": ["buy one get one", "bogo", "b1g1", "buy 1 get 1"],
  "active": true
}
```

### Pause a brand
```json
{
  "brand": "Clinique",
  ...
  "active": false
}
```

---

## Managing Price Products

Products are configured in [`products.json`](./products.json).

### Re-enable a paused MacBook product
```json
{
  "name": "MacBook Pro M5 Space Black 14\"",
  "url": "https://...",
  "threshold": 2700.00,
  "active": true
}
```

---

## Credit Usage

| Tracker | Schedule | Calls/month | Credits/month |
|---|---|---|---|
| BOGO Tracker (2 brands) | Daily | 60 | 600 |
| MacBook Tracker | Paused | 0 | 0 |
| **Total** | | **60** | **600** |

The free plan provides **500 credits/month + 500 one-time bonus**. The one-time buffer covers the first month's overage of 100 credits. A **$5 one-time top-up** (1,000 non-expiring credits) provides a comfortable ongoing buffer.

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
