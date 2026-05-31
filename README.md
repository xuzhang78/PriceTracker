# B&H Photo MacBook Price Tracker

Automated daily price monitor for Apple MacBook Pro M5 listings on B&H Photo.
Uses **ScrapeGraphAI** (AI extraction) + **GitHub Actions** (scheduling) + **ntfy.sh** (push notifications).

Sends a push notification to your phone when the price drops below your configured threshold.

---

## How It Works

```
GitHub Actions (daily 09:00 UTC)
    └── check_prices.py
            ├── sgai.extract(silver_url)      ← AI reads the B&H page
            ├── sgai.extract(space_black_url) ← AI reads the B&H page
            └── price < threshold → ntfy.sh → 📱 your phone
```

No server required. Runs entirely on GitHub's free infrastructure.

---

## Prerequisites

- Python 3.12+ (`scrapegraph-py` v2.x requires 3.12)
- A [ScrapeGraphAI](https://scrapegraphai.com/dashboard) account (free tier is sufficient)
- The [ntfy app](https://ntfy.sh) installed on your phone

---

## Setup

### Step 1 — Install Dependencies (local testing only)

```bash
pip install -r requirements.txt
```

### Step 2 — Configure `.env` (local testing only)

Edit `.env` and fill in your values:

```bash
SGAI_API_KEY=sgai-xxxxxxxxxxxxxxxxxxxxxxxx   # from scrapegraphai.com/dashboard
NTFY_TOPIC=your-ntfy-topic            # already set — use as-is
```

### Step 3 — Subscribe to Push Notifications

1. Install the **ntfy** app: [iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
2. Tap **+** → enter topic: `your-ntfy-topic`
3. Tap Subscribe ✅

### Step 4 — Test Locally

**Test that ntfy works (no API key needed):**
```bash
curl -d "Test from price tracker" -H "Title: Test Alert" https://ntfy.sh/your-ntfy-topic
```
You should receive a notification on your phone immediately.

**Simulate a price drop (tests the full pipeline):**
```bash
PRICE_OVERRIDE=2499.00 python check_prices.py
```
Expected output: both products trigger alerts, ntfy notifications arrive on phone.

**Test no-alert path (price above threshold):**
```bash
PRICE_OVERRIDE=2799.00 python check_prices.py
```
Expected output: "Above threshold — no alert."

**Run a real live check:**
```bash
python check_prices.py
```
Calls the SGAI API with your key and extracts live prices from B&H Photo.

### Step 5 — Push to GitHub (Private Repo)

```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git add .
git commit -m "feat: initial price tracker setup"
git push -u origin main
```

### Step 6 — Add GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `SGAI_API_KEY` | Your ScrapeGraphAI API key |
| `NTFY_TOPIC` | `your-ntfy-topic` |

### Step 7 — Test GitHub Actions

1. Go to **Actions → B&H MacBook Price Check → Run workflow**
2. In the `price_override` field, enter `2499.00`
3. Click **Run workflow**
4. Watch the run log — prices printed, ntfy notification fires on your phone ✅
5. Repeat with the field blank to verify a real live extraction ✅

The daily cron (`0 9 * * *`) activates automatically after the first push.

---

## Managing Products

Products are configured in [`products.json`](./products.json). No code changes needed.

### Add a new product
```json
{
  "name": "Sony A7C II (Amazon)",
  "url": "https://www.amazon.com/dp/B0CG5KG7GS",
  "threshold": 2000.00,
  "active": true
}
```

### Pause tracking after purchase (don't delete — preserve history)
```json
{
  "name": "MacBook Pro M5 Silver 14\"",
  "url": "https://...",
  "threshold": 2700.00,
  "active": false,
  "purchased_at": "2026-06-15",
  "purchased_price": 2649.00
}
```

---

## Credit Usage (Free Plan)

| Products | Calls/month | Est. credits | Free tier (500/mo) |
|---|---|---|---|
| 2 | 62 | 310–496 | ✅ Covered |
| 4 | 124 | 620–992 | ❌ Upgrade needed |

A **$5 one-time top-up** (1,000 non-expiring credits) extends the free plan to ~4 products for several months.

---

## Switching to Telegram Notifications

1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → copy **Bot Token**
2. Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy **Chat ID**
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env` and GitHub Secrets
4. In `check_prices.py`, uncomment the `send_telegram()` function and replace calls to `notify_price_drop()` and `notify_extraction_error()` with it

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `result.status == "error"` | SGAI credits exhausted or API key invalid | Check dashboard for credit balance |
| Price is 0.0 or very wrong | Cloudflare blocked the request | Sanity guard fires an ntfy error alert automatically |
| No ntfy notification | Wrong topic name | Confirm topic in ntfy app matches `NTFY_TOPIC` in secrets |
| GitHub Actions not running | Workflow not triggered yet | Run manually via Actions tab first; daily cron starts automatically |
| Actions tab shows no runs | Repo was just created | Push a commit to activate; then trigger manually |
